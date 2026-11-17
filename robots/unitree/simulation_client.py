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

"""Client for the external MuJoCo route-following service."""

from __future__ import annotations

from typing import Any

from robots.unitree.http_client import (
    JsonServiceClient,
    ServiceError,
    finite_number,
    map_identity,
)


def validate_pose(pose: Any) -> None:
    """Require finite map-frame position and heading observations."""
    if not isinstance(pose, dict):
        raise ServiceError("simulation pose must be an object")
    for key in ("x", "y", "yaw"):
        finite_number(pose.get(key), f"pose.{key}")
    if type(pose.get("fallen")) is not bool:
        raise ServiceError("pose.fallen must be boolean")


class SimulationClient(JsonServiceClient):
    """Submit routes and query asynchronous walks, without managing simulators."""

    def health(self) -> dict[str, Any]:
        """Read map identity, simulator availability, and active walk state."""
        result = self.request("GET", "/health")
        map_identity(result)
        if type(result.get("simulator_running")) is not bool:
            raise ServiceError("simulator_running must be boolean")
        return result

    def get_pose(self) -> dict[str, Any]:
        """Read a fresh pose; callers decide whether a fallen robot may proceed."""
        result = self.request("GET", "/pose")
        validate_pose(result)
        return result

    def preview(self, plan: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        """Run the service's independent preview without moving the live simulator."""
        return self.request("POST", "/preview", plan, timeout_s=timeout_s)

    def start_walk(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Submit once; an accepted response is not a completed walk."""
        result = self.request("POST", "/walk", plan)
        if not isinstance(result.get("walk_id"), str) or not result["walk_id"]:
            raise ServiceError("accepted walk must contain a nonempty walk_id")
        return result

    def get_walk_status(self) -> dict[str, Any]:
        """Read a running or finished walk without inferring arrival success."""
        result = self.request("GET", "/walk")
        if result.get("state") not in ("running", "finished"):
            raise ServiceError("walk state must be running or finished")
        return result

    def stop_walk(self) -> dict[str, Any]:
        """Request a stop; the caller must subsequently confirm finished state."""
        return self.request("POST", "/walk/stop", {})

    @staticmethod
    def validate_control(result: dict[str, Any], control_id: str | None = None) -> None:
        """Require an identified trajectory lease and an explicit lifecycle state."""
        if not isinstance(result.get("control_id"), str) or not result["control_id"]:
            raise ServiceError("control response must contain control_id")
        if control_id is not None and result["control_id"] != control_id:
            raise ServiceError(
                "simulation control_id changed; control ownership was lost"
            )
        if result.get("mode") != "trajectory" or result.get("state") not in {
            "starting",
            "running",
            "stopping",
            "finished",
            "failed",
        }:
            raise ServiceError("control response must describe a trajectory session")
        if (
            type(result.get("ready")) is not bool
            or type(result.get("stop_confirmed")) is not bool
        ):
            raise ServiceError("control response must contain ready and stop_confirmed")

    def start_control(self, trajectory_timeout_s: float = 2.0) -> dict[str, Any]:
        """Create a trajectory lease once; an uncertain acceptance is not retried."""
        result = self.request(
            "POST", "/control/start", {"trajectory_timeout_s": trajectory_timeout_s}
        )
        self.validate_control(result)
        return result

    def get_control(self, control_id: str) -> dict[str, Any]:
        """Read the lease while checking that another session has not replaced it."""
        result = self.request("GET", "/control")
        self.validate_control(result, control_id)
        return result

    def submit_trajectory(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Forward capture-aligned waypoints, never external velocity commands."""
        result = self.request("POST", "/control/trajectory", payload)
        self.validate_control(result, payload["control_id"])
        return result

    def stop_control(self, control_id: str) -> dict[str, Any]:
        """Wait for the simulation's physical stop confirmation before release."""
        result = self.request(
            "POST", "/control/stop", {"control_id": control_id}, timeout_s=12.0
        )
        self.validate_control(result, control_id)
        if result["state"] != "finished" or not result["stop_confirmed"]:
            raise ServiceError("trajectory control stop was not confirmed")
        return result
