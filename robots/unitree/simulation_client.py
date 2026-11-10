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
