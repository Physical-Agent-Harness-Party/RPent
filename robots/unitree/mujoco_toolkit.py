# Copyright 2026 The RPent Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Agent tools for map-frame planning and external MuJoCo route following."""

from __future__ import annotations

import copy
import math
import threading
import time
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from robots.unitree.http_client import ServiceError, finite_number, map_identity
from robots.unitree.navigation_client import NavigationClient, NavigationGoal
from robots.unitree.simulation_client import SimulationClient, validate_pose
from rpent.dashboard.events import DashboardEventSink
from rpent.evaluation import write_json_atomic
from rpent.memory import MemoryManager
from rpent.session import EnvState
from rpent.tools import Toolkit, ToolResult, tool
from rpent.tools.toolkit import ToolCancelled
from rpent.utils.logging import get_logger

logger = get_logger("unitree_navigation")


class UnitreeMujocoToolkit(Toolkit):
    """Plan routes and await single-goal walks on an exclusively used sim service.

    The service may omit walk_id in status replies. In that case ownership is
    local to this toolkit, so sharing one simulation endpoint between runners
    is unsupported. No simulator restart or other-controller stop is exposed.
    """

    def __init__(
        self,
        *,
        nav: NavigationClient,
        sim: SimulationClient,
        dashboard_events: DashboardEventSink,
        memory: MemoryManager,
        state_output_dir: Path,
        navigation_timeout_s: float = 300.0,
        poll_interval_s: float = 5.0,
    ) -> None:
        for value in (navigation_timeout_s, poll_interval_s):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("navigation waits must be finite and positive")
        self._nav, self._sim = nav, sim
        self._timeout_s, self._poll_s = navigation_timeout_s, poll_interval_s
        self._artifacts = state_output_dir / "navigation"
        self._plans: dict[str, dict[str, Any]] = {}
        self._completed_plans: set[str] = set()
        self._failed_previews: set[str] = set()
        self._lock = threading.RLock()
        self._cancelled = threading.Event()
        self._active_walk_id: str | None = None
        self._submission_uncertain = False
        self._stop_requested = False
        self._motion_attempted = False
        self._completed = 0
        self._last_pose: dict[str, Any] | None = None
        self._last_walk: dict[str, Any] | None = None
        self._last_error: str | None = None
        self._stop_error: str | None = None
        super().__init__(
            dashboard_events=dashboard_events,
            memory=memory,
            state=EnvState(state_output_dir),
        )
        self.add_tools(
            (
                self.list_navigation_goals,
                self.get_navigation_pose,
                self.plan_navigation,
                self.preview_navigation,
                self.follow_navigation,
                self.get_navigation_status,
                self.stop_navigation,
            )
        )
        self.add_tool(self.finish_navigation, replace=True)
        with self.state.record_step(state=self._state_summary()):
            pass
        self._publish_step(self.state.get())

    def _state_summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "completed_navigation_goals": self._completed,
                "active_walk_id": self._active_walk_id,
                "submission_uncertain": self._submission_uncertain,
                "pose": copy.deepcopy(self._last_pose),
                "error": self._last_error,
                "stop_error": self._stop_error,
            }

    def _ready(self) -> tuple[str, str]:
        nav = self._nav.health()
        sim = self._sim.health()
        identity = map_identity(nav)
        if map_identity(sim) != identity:
            raise ServiceError(
                "planning and simulation map_id/map_version do not match"
            )
        if not sim["simulator_running"]:
            raise ServiceError("simulator is not running; start it on the server first")
        if sim.get("other_controllers"):
            raise ServiceError(
                "other controllers are active; no navigation was submitted"
            )
        return identity

    def _pose(self) -> dict[str, Any]:
        pose = self._sim.get_pose()
        with self._lock:
            self._last_pose = pose
        return pose

    def _plan(self, plan_id: str) -> dict[str, Any]:
        with self._lock:
            if plan_id not in self._plans:
                raise ValueError("unknown plan_id; call plan_navigation first")
            return copy.deepcopy(self._plans[plan_id])

    @tool(readonly=True)
    def list_navigation_goals(self) -> ToolResult:
        """Read current waypoint names, ids, and map-frame standing poses."""
        return ToolResult(data=self._nav.list_waypoints())

    @tool(readonly=True)
    def get_navigation_pose(self) -> ToolResult:
        """Read the simulator's current map-frame position and yaw in radians."""
        return ToolResult(data=self._pose())

    @tool(readonly=True)
    def plan_navigation(
        self, goals: list[NavigationGoal], step: float | None = None
    ) -> ToolResult:
        """Plan from a fresh simulator pose and save the unmodified route.

        Args:
            goals: Ordered waypoint names/ids or map-coordinate objects. Use one goal per executable plan.
            step: Optional dense-path spacing in meters; not a motion speed.
        """
        identity = self._ready()
        pose = self._pose()
        if pose["fallen"]:
            raise ServiceError("robot has fallen; simulator recovery is required")
        plan = self._nav.plan(pose, goals, step=step)
        if map_identity(plan) != identity:
            raise ServiceError("map changed during planning; query the services again")
        plan_id = "plan_" + uuid4().hex[:12]
        destination = self._artifacts / "plans" / f"{plan_id}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(destination, plan)
        with self._lock:
            self._plans[plan_id] = plan
        return ToolResult(
            data={
                "ok": True,
                "plan_id": plan_id,
                "map_id": identity[0],
                "map_version": identity[1],
                "goals": [leg["goal"] for leg in plan["legs"]],
                "total_length": plan["total_length"],
                "warnings": plan.get("warnings", []),
                "plan_file": str(destination),
                "executable": len(plan["legs"]) == 1,
            }
        )

    @tool(readonly=True)
    def preview_navigation(self, plan_id: str) -> ToolResult:
        """Preview a saved route in independent simulation without moving the live dog.

        Args:
            plan_id: Identifier returned by plan_navigation.
        """
        plan = self._plan(plan_id)
        if self._ready() != map_identity(plan):
            raise ServiceError("saved plan uses a different map; replan")
        with self._lock:
            self._failed_previews.add(plan_id)
        result = self._sim.preview(plan, timeout_s=self._timeout_s)
        destination = self._artifacts / "previews" / f"{plan_id}_{uuid4().hex[:8]}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(destination, result)
        preview_result = result.get("result")
        # The preview API guarantees top-level ok, not a nested result.ok field.
        # Still reject an explicitly reported nested failure when provided.
        if isinstance(preview_result, dict) and (
            preview_result.get("ok") is False
            or any(
                isinstance(leg, dict) and leg.get("reached") is False
                for leg in preview_result.get("legs", [])
            )
        ):
            raise ServiceError(f"preview did not confirm arrival; see {destination}")
        with self._lock:
            self._failed_previews.discard(plan_id)
        return ToolResult(
            data={
                "ok": True,
                "plan_id": plan_id,
                "result": result.get("result"),
                "image": result.get("image"),
                "report_file": str(destination),
            }
        )

    def _observe_walk(self) -> dict[str, Any]:
        with self._lock:
            expected_id = self._active_walk_id
        status = self._sim.get_walk_status()
        with self._lock:
            observed_id = status.get("walk_id")
            if (
                expected_id is not None
                and observed_id is not None
                and observed_id != expected_id
            ):
                # Never stop a different client's task when ownership has changed.
                self._active_walk_id = None
                raise ServiceError(
                    "simulation walk_id changed; task ownership was lost"
                )
            self._last_walk = status
            if isinstance(status.get("pose"), dict):
                self._last_pose = status["pose"]
            if status["state"] == "finished":
                self._active_walk_id = None
        return status

    def _request_stop(self) -> bool:
        with self._lock:
            if self._active_walk_id is None or self._stop_requested:
                return True
        try:
            self._observe_walk()
        except ServiceError as exc:
            if self._active_walk_id is None:
                # An observed id change invalidates our ownership.
                self._stop_error = f"stop was not confirmed: {exc}"
                return False
            # A failed observation does not relinquish an accepted owned task.
            # The endpoint is exclusively used by this runner; still send stop.
            logger.warning("Cannot query owned walk before stopping: %s", exc)
        with self._lock:
            if self._active_walk_id is None:
                self._stop_error = None
                return True
            if self._stop_requested:
                return True
            self._stop_requested = True
        try:
            self._sim.stop_walk()
        except ServiceError as exc:
            with self._lock:
                self._stop_error = f"stop was not confirmed: {exc}"
            logger.error("%s", self._stop_error)
            return False
        return True

    def _stop_and_confirm(self) -> bool:
        requested = self._request_stop()
        if self._active_walk_id is None:
            return requested
        deadline = time.monotonic() + 10.0
        while self._active_walk_id is not None and time.monotonic() < deadline:
            try:
                self._observe_walk()
            except ServiceError as exc:
                with self._lock:
                    self._stop_error = f"stop was not confirmed: {exc}"
                return False
            if self._active_walk_id is not None:
                time.sleep(0.05)
        if self._active_walk_id is not None:
            with self._lock:
                self._stop_error = "stop was not confirmed before cleanup timeout"
            return False
        self._stop_error = None
        return True

    @staticmethod
    def _validate_arrival(status: dict[str, Any], plan: dict[str, Any]) -> None:
        result = status.get("result")
        if (
            status.get("fell") is not False
            or not isinstance(result, dict)
            or result.get("ok") is not True
            or not isinstance(result.get("legs"), list)
            or len(result["legs"]) != 1
            or not isinstance(result["legs"][0], dict)
            or result["legs"][0].get("reached") is not True
        ):
            raise ServiceError("walk finished without confirmed arrival at the goal")
        leg = result["legs"][0]
        position_error = finite_number(leg.get("position_error"), "position_error")
        if not 0 <= position_error <= 0.15 + 1e-9:
            raise ServiceError("navigation position error exceeds 0.15 m")
        pose = status.get("pose")
        validate_pose(pose)
        if pose["fallen"]:
            raise ServiceError("robot has fallen")
        goal = plan["legs"][0]["goal"]["stand_pose"]
        if math.hypot(pose["x"] - goal["x"], pose["y"] - goal["y"]) > 0.15 + 1e-9:
            raise ServiceError("final pose is outside the navigation goal tolerance")
        if goal.get("yaw") is not None:
            yaw_error = finite_number(leg.get("yaw_error"), "yaw_error")
            actual_error = math.atan2(
                math.sin(pose["yaw"] - goal["yaw"]),
                math.cos(pose["yaw"] - goal["yaw"]),
            )
            if max(abs(yaw_error), abs(actual_error)) > 0.2 + 1e-9:
                raise ServiceError("navigation yaw error exceeds 0.2 radians")

    @tool
    def follow_navigation(self, plan_id: str) -> ToolResult:
        """Execute one saved single-goal route and wait for settled arrival.

        Args:
            plan_id: An unexecuted single-goal plan from plan_navigation. Plan and follow successive goals separately.
        """
        self._motion_attempted = True
        self._last_error = None
        plan = self._plan(plan_id)
        if len(plan["legs"]) != 1:
            raise ValueError("follow one goal at a time to preserve ordered arrival")
        with self._lock:
            if plan_id in self._completed_plans:
                raise ValueError(
                    "plan already completed; read the current pose and replan"
                )
            if plan_id in self._failed_previews:
                raise ServiceError("preview failed; do not follow this plan")
            if self._active_walk_id is not None or self._submission_uncertain:
                raise ServiceError(
                    "a prior walk is active or its submission is uncertain"
                )
        if self._ready() != map_identity(plan):
            raise ServiceError("saved plan uses a different map; replan")
        health = self._sim.health()
        if (health.get("walk") or {}).get("state") == "running":
            raise ServiceError("simulation service already has a running walk")
        pose = self._pose()
        if pose["fallen"]:
            raise ServiceError("robot has fallen")
        if (
            math.hypot(pose["x"] - plan["start"]["x"], pose["y"] - plan["start"]["y"])
            > 0.3
        ):
            raise ServiceError("robot moved away from the saved start; replan")
        destination = self._artifacts / "walks" / f"{plan_id}_{uuid4().hex[:8]}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self._timeout_s
        try:
            self.raise_if_cancelled()
            # A timeout or malformed acceptance may follow a remotely started walk.
            self._submission_uncertain = True
            accepted = self._sim.start_walk(plan)
            with self._lock:
                self._active_walk_id = accepted["walk_id"]
                self._submission_uncertain = False
                self._stop_requested = False
                self._stop_error = None
                self._last_walk = None
            while True:
                self.raise_if_cancelled()
                if time.monotonic() >= deadline:
                    raise TimeoutError("navigation timed out; the walk is not retried")
                status = self._observe_walk()
                self.raise_if_cancelled()
                if status["state"] == "finished":
                    self._validate_arrival(status, plan)
                    with self._lock:
                        self._completed += 1
                        self._completed_plans.add(plan_id)
                    return ToolResult(
                        data={
                            "ok": True,
                            "navigation_completed": True,
                            "plan_id": plan_id,
                            "walk_id": accepted["walk_id"],
                            "state": "finished",
                            "result": status["result"],
                            "pose": status["pose"],
                            "report_file": str(destination),
                        }
                    )
                self._cancelled.wait(
                    min(self._poll_s, max(0, deadline - time.monotonic()))
                )
        except (ServiceError, TimeoutError, ToolCancelled) as exc:
            self._last_error = str(exc)
            raise
        finally:
            if self._active_walk_id is not None:
                self._stop_and_confirm()
            write_json_atomic(
                destination,
                {
                    "plan_id": plan_id,
                    **self._state_summary(),
                    "walk": self._last_walk,
                },
            )

    @tool(readonly=True)
    def get_navigation_status(self) -> ToolResult:
        """Read both services and any walk still owned by this toolkit."""
        nav, sim = self._nav.health(), self._sim.health()
        if self._active_walk_id is not None:
            self._observe_walk()
        pose = self._pose() if sim["simulator_running"] else None
        return ToolResult(
            data={
                "planning": nav,
                "simulation": sim,
                "pose": pose,
                "navigation": self._state_summary(),
            }
        )

    @tool
    def stop_navigation(self) -> ToolResult:
        """Stop only a walk owned by this toolkit and confirm it has finished."""
        if self._submission_uncertain:
            raise ServiceError(
                "submission is uncertain without a walk_id; verify the service before retrying"
            )
        if self._active_walk_id is None:
            return ToolResult(data={"ok": True, "stopped": False})
        if not self._stop_and_confirm():
            raise ServiceError(self._stop_error or "stop was not confirmed")
        return ToolResult(data={"ok": True, "stopped": True})

    @tool(name="finish", readonly=True)
    def finish_navigation(
        self, status: Literal["success", "failure", "stuck"], summary: str
    ) -> ToolResult:
        """Finish a query task or report verified arrival after navigation.

        Args:
            status: success, failure, or stuck.
            summary: Explain the result and any remaining goals.
        """
        if (
            status == "success"
            and self._motion_attempted
            and (
                self._active_walk_id is not None
                or self._submission_uncertain
                or self._last_error is not None
                or self._stop_error is not None
                or self._completed == 0
            )
        ):
            return ToolResult(
                data={"error": "navigation has not completed successfully"}
            )
        return ToolResult(data={"_finish": True, "status": status, "summary": summary})

    def get_env_state(
        self, *, command: dict[str, Any], result: dict[str, Any], elapsed_s: float
    ) -> ToolResult:
        """Record navigation outcomes without inventing physical task success."""
        if result.get("error"):
            self._last_error = str(result["error"])
        with self.state.record_step(
            state=self._state_summary(),
            command=command,
            result=result,
            elapsed_s=elapsed_s,
            extras={"validation_scope": "mujoco_navigation"},
        ) as step:
            pass
        return ToolResult(data={**result, "step": step})

    def cancel_active_and_wait(self) -> None:
        """Request remote stopping before waiting for the local tool to drain."""
        self._cancelled.set()
        calls = self._scheduler.cancel()
        try:
            self._request_stop()
        finally:
            self._scheduler.wait(calls)
        if self._active_walk_id is not None:
            self._stop_and_confirm()

    def resume_calls(self) -> None:
        """Resume only after cancellation cleanup has drained the tools."""
        super().resume_calls()
        self._cancelled.clear()

    def close(self) -> None:
        """Drain tools and close owned clients, leaving both external services up."""
        try:
            super().close()
        finally:
            try:
                self._nav.close()
            finally:
                self._sim.close()
