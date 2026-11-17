# Copyright 2026 The RPent Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Capture, predict and forward waypoint references under one Go2 control lease."""

from __future__ import annotations

import base64
import copy
import json
import math
import threading
import time
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from robots.unitree.http_client import ServiceError, finite_number, map_identity
from robots.unitree.observation_client import ObservationClient
from robots.unitree.simulation_client import SimulationClient, validate_pose
from robots.unitree.vln_client import VlnClient
from rpent.evaluation import write_json_atomic
from rpent.tools.toolkit import ToolCancelled


class VlnNavigation:
    """Own one bounded visual-navigation episode and its confirmed-stop cleanup."""

    def __init__(
        self,
        *,
        sim: SimulationClient,
        observation: ObservationClient,
        model: VlnClient,
        artifacts: Path,
        period_s: float = 0.25,
        trajectory_timeout_s: float = 2.0,
    ) -> None:
        if not math.isfinite(period_s) or period_s <= 0:
            raise ValueError("VLN period must be finite and positive")
        if not 0.1 <= trajectory_timeout_s <= 2.0:
            raise ValueError("trajectory timeout must be between 0.1 and 2 seconds")
        self.sim, self.observation, self.model = sim, observation, model
        self.artifacts = artifacts
        self.period_s, self.trajectory_timeout_s = period_s, trajectory_timeout_s
        self._lock = threading.RLock()
        self._run_lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._cancelled = threading.Event()
        self._running = False
        self._control_id: str | None = None
        self._uncertain = False
        self._stop_error: str | None = None
        self._status: dict[str, Any] = {"state": "idle", "goal_verified": False}

    @property
    def active(self) -> bool:
        """Include uncertain submissions and unconfirmed stops in admission checks."""
        with self._lock:
            return self._running or self._control_id is not None or self._uncertain

    def snapshot(self) -> dict[str, Any]:
        """Read compact state without transporting images or loading model packages."""
        with self._lock:
            return copy.deepcopy(
                self._status
                | {
                    "active_control_id": self._control_id,
                    "submission_uncertain": self._uncertain,
                    "stop_error": self._stop_error,
                }
            )

    def _publish(self, **fields: Any) -> dict[str, Any]:
        with self._lock:
            self._status.update(fields, updated_at_ns=time.time_ns())
            state = self.snapshot()
            self.artifacts.mkdir(parents=True, exist_ok=True)
            write_json_atomic(self.artifacts / "latest.json", state)
            return state

    def _check_cancelled(self, check_cancelled: Callable[[], None]) -> None:
        if self._cancelled.is_set():
            raise ToolCancelled("VLN navigation was cancelled")
        check_cancelled()

    def _stop_owned(self) -> dict[str, Any] | None:
        with self._stop_lock:
            with self._lock:
                control_id = self._control_id
                uncertain = self._uncertain
            if control_id is None:
                if uncertain:
                    self._stop_error = (
                        "control acceptance is uncertain; reconcile the simulator"
                    )
                    self._publish()
                return None
            try:
                # The simulation atomically checks this ID before touching its writer.
                result = self.sim.stop_control(control_id)
            except Exception as exc:
                with self._lock:
                    self._stop_error = f"VLN stop was not confirmed: {exc}"
                self._publish()
                raise ServiceError(self._stop_error) from exc
            with self._lock:
                self._control_id = None
                self._stop_error = None
            self._publish(stop_confirmed=True, pose=result.get("pose"), control=result)
            return result

    def cancel(self) -> None:
        """Stop the owned lease before waiting for a pending model receive to drain."""
        self._cancelled.set()
        if self.active:
            self._publish(state="stopping")
        try:
            self._stop_owned()
        finally:
            self.model.close()

    @staticmethod
    def goal_errors(pose: dict[str, Any], goal: dict[str, float]) -> dict[str, Any]:
        """Check independent map-pose evidence using the existing route tolerances."""
        validate_pose(pose)
        distance = math.hypot(pose["x"] - goal["x"], pose["y"] - goal["y"])
        yaw_error = (
            None
            if "yaw" not in goal
            else abs(
                math.atan2(
                    math.sin(pose["yaw"] - goal["yaw"]),
                    math.cos(pose["yaw"] - goal["yaw"]),
                )
            )
        )
        return {
            "position_error_m": distance,
            "yaw_error_rad": yaw_error,
            "goal_verified": not pose["fallen"]
            and distance <= 0.25 + 1e-9
            and (yaw_error is None or yaw_error <= 0.2 + 1e-9),
        }

    def run(
        self,
        instruction: str,
        *,
        timeout_s: float,
        expected_goal: dict[str, float] | None,
        check_cancelled: Callable[[], None],
        on_started: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Execute one episode; model STOP alone leaves spatial success unverified."""
        if (
            not isinstance(instruction, str)
            or not instruction.strip()
            or len(instruction) > 20000
        ):
            raise ValueError("VLN instruction must contain 1–20000 characters")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("VLN budget must be finite and positive")
        if expected_goal is not None:
            expected_goal = {
                k: finite_number(v, f"expected_goal.{k}")
                for k, v in expected_goal.items()
            }
            if not {"x", "y"} <= expected_goal.keys() or expected_goal.keys() - {
                "x",
                "y",
                "yaw",
            }:
                raise ValueError("expected_goal must contain x/y and optional yaw")
        if not self._run_lock.acquire(blocking=False):
            raise ServiceError("a VLN episode is already running")
        try:
            if self.active:
                raise ServiceError(
                    "previous VLN control is active or uncertain; do not repeat motion"
                )
            self._cancelled.clear()
            self._running = True
            episode = "vln_" + uuid4().hex[:16]
            self._status = {
                "episode_id": episode,
                "instruction": instruction,
                "state": "connecting",
                "model_stop": False,
                "stop_confirmed": False,
                "goal_verified": False,
                "expected_goal": expected_goal,
                "predictions": 0,
                "accepted_references": 0,
                "stale_predictions": 0,
                "error": None,
                "validation_scope": "model_stop_and_simulator_stop",
                "model_endpoint": getattr(self.model, "endpoint", None),
                "observation_endpoint": getattr(self.observation, "endpoint", None),
                "started_at_ns": time.time_ns(),
            }
            self._publish()
            deadline = time.monotonic() + timeout_s
            try:
                self._check_cancelled(check_cancelled)
                health = self.sim.health()
                identity = map_identity(health)
                if (
                    not health["simulator_running"]
                    or health.get("control_mode", "idle") != "idle"
                    or health.get("other_controllers")
                    or (health.get("walk") or {}).get("state") == "running"
                ):
                    raise ServiceError("simulation must be idle before VLN navigation")
                self.model.start_episode(episode)
                self._check_cancelled(check_cancelled)
                with self._lock:
                    self._uncertain = True
                self._publish(state="starting_control")
                accepted = self.sim.start_control(self.trajectory_timeout_s)
                with self._lock:
                    self._control_id = accepted["control_id"]
                    self._uncertain = False
                    self._stop_error = None
                self._publish(control_id=self._control_id)
                while True:
                    self._check_cancelled(check_cancelled)
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            "VLN timed out while waiting for the controller"
                        )
                    control = self.sim.get_control(self._control_id)
                    if control["state"] == "failed":
                        raise ServiceError(
                            f"trajectory controller failed: {control.get('error')}"
                        )
                    if control["ready"]:
                        break
                    self._cancelled.wait(0.05)
                self._publish(state="observing", control=control)
                if on_started is not None:
                    on_started(self.snapshot())
                predictions = references = stale = consecutive_stale = 0
                last_stamp = None
                sequence = 0
                model_stop = False
                frames = self.artifacts / "frames"
                frames.mkdir(parents=True, exist_ok=True)
                records = self.artifacts / f"{episode}.jsonl"
                while True:
                    self._check_cancelled(check_cancelled)
                    tick = time.monotonic()
                    if tick >= deadline:
                        raise TimeoutError(
                            "VLN navigation timed out; predictions are not replayed"
                        )
                    control = self.sim.get_control(self._control_id)
                    if (
                        control["state"] not in {"starting", "running"}
                        or not control["ready"]
                    ):
                        raise ServiceError(
                            "VLN trajectory controller is not running and ready"
                        )
                    validate_pose(control.get("pose"))
                    if (control.get("tracking") or {}).get("state") == "solver_error":
                        raise ServiceError(
                            f"trajectory MPC failed: {control['tracking'].get('error')}"
                        )
                    if control["pose"]["fallen"]:
                        raise ServiceError("Go2 fell during VLN navigation")
                    if (
                        expected_goal is not None
                        and references
                        and self.goal_errors(control["pose"], expected_goal)[
                            "goal_verified"
                        ]
                    ):
                        break
                    self._publish(
                        state="observing", control=control, pose=control["pose"]
                    )
                    observation = self.observation.observe()
                    self._check_cancelled(check_cancelled)
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            "VLN observation arrived after the episode budget"
                        )
                    if map_identity(observation) != identity:
                        raise ServiceError(
                            "camera and simulation maps changed or do not match"
                        )
                    if last_stamp is not None and observation["stamp_ns"] <= last_stamp:
                        self._cancelled.wait(self.period_s)
                        continue
                    last_stamp = observation["stamp_ns"]
                    self._publish(state="inferencing", capture_stamp_ns=last_stamp)
                    began = time.monotonic()
                    prediction = self.model.predict(
                        observation["image"]["data"],
                        instruction,
                        timeout_s=min(
                            self.model.timeout_s, max(0.01, deadline - time.monotonic())
                        ),
                    )
                    self._check_cancelled(check_cancelled)
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            "VLN prediction arrived after the episode budget"
                        )
                    predictions += 1
                    age = (time.time_ns() - last_stamp) / 1e9
                    latency_ms = (time.monotonic() - began) * 1000
                    frame_path = frames / f"{episode}-{predictions:06d}.jpg"
                    frame_path.write_bytes(
                        base64.b64decode(observation["image"]["data"])
                    )
                    payload = {
                        "control_id": self._control_id,
                        "seq": sequence + 1,
                        "frame": "base_link",
                        "stamp_ns": last_stamp,
                        "capture_pose": {
                            k: observation["base_pose"][k] for k in ("x", "y", "yaw")
                        },
                        "waypoints": prediction["actions"]["actions"],
                        "stop": prediction["stop"],
                    }
                    record = {
                        "capture_stamp_ns": last_stamp,
                        "capture_age_s": age,
                        "input_frame": str(frame_path),
                        "prediction": prediction,
                        "request": payload,
                    }
                    if age < -0.05 or age >= self.trajectory_timeout_s - 0.1:
                        stale += 1
                        consecutive_stale += 1
                        record["discarded"] = (
                            "prediction exceeded capture-time validity"
                        )
                        self._publish(
                            state="waiting_fresh_prediction",
                            predictions=predictions,
                            stale_predictions=stale,
                            capture_age_s=age,
                            latency_ms=latency_ms,
                        )
                    else:
                        consecutive_stale = 0
                        sequence += 1
                        response = self.sim.submit_trajectory(payload)
                        references += 1
                        record["response"] = response
                        self._publish(
                            state="tracking",
                            predictions=predictions,
                            accepted_references=references,
                            stale_predictions=stale,
                            latency_ms=latency_ms,
                            capture_age_s=age,
                            sequence=sequence,
                            model_sequence=prediction["seq"],
                            waypoints=payload["waypoints"],
                            latest_frame=str(frame_path),
                            control=response,
                            model_stop=prediction["stop"],
                        )
                    with records.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                    if consecutive_stale >= 3:
                        raise ServiceError(
                            "three VLN predictions expired; optimize inference latency before retrying"
                        )
                    if prediction["stop"] and not record.get("discarded"):
                        model_stop = True
                        break
                    self._cancelled.wait(
                        max(0, self.period_s - (time.monotonic() - tick))
                    )
                self._publish(state="stopping", model_stop=model_stop)
                sequence += 1
                self.sim.submit_trajectory(
                    {"control_id": self._control_id, "seq": sequence, "stop": True}
                )
                stopped = self._stop_owned()
                if stopped is None:
                    raise ServiceError("VLN ended without a confirmed owned stop")
                pose = self.sim.get_pose()
                errors = (
                    {}
                    if expected_goal is None
                    else self.goal_errors(pose, expected_goal)
                )
                if expected_goal is not None and not errors["goal_verified"]:
                    raise ServiceError(
                        f"VLN stopped outside the expected map goal: {errors}"
                    )
                verified = errors.get("goal_verified", False)
                return self._publish(
                    state="finished" if verified else "verification_required",
                    navigation_completed=verified,
                    goal_verified=verified,
                    pose=pose,
                    validation_scope="map_pose_and_simulator_stop"
                    if verified
                    else "model_stop_and_simulator_stop",
                    report_file=str(self.artifacts / f"{episode}.json"),
                    **{k: v for k, v in errors.items() if k != "goal_verified"},
                )
            except BaseException as exc:
                cancelled = isinstance(exc, ToolCancelled) or self._cancelled.is_set()
                self._publish(
                    state="cancelled" if cancelled else "failed", error=str(exc)
                )
                if cancelled and not isinstance(exc, ToolCancelled):
                    raise ToolCancelled("VLN navigation was cancelled") from exc
                raise
            finally:
                try:
                    self._stop_owned()
                finally:
                    self.model.close()
                    with self._lock:
                        self._running = False
                    self._publish()
                    write_json_atomic(
                        self.artifacts / f"{episode}.json", self.snapshot()
                    )
        finally:
            self._run_lock.release()
