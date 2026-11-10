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

"""Client for the A* planner's map-frame route API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from robots.unitree.http_client import (
    JsonServiceClient,
    ServiceError,
    finite_number,
    map_identity,
)


class CoordinateGoal(BaseModel):
    """A map-frame goal in meters, with an optional final yaw in radians."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    x: float
    y: float
    yaw: float | None = None


NavigationGoal = str | CoordinateGoal


def _goal_payload(goal: NavigationGoal | dict[str, Any]) -> str | dict[str, float]:
    if isinstance(goal, str):
        if not goal.strip():
            raise ValueError("navigation goal names must not be empty")
        return goal
    return CoordinateGoal.model_validate(goal).model_dump(exclude_none=True)


def _check_stand_pose(goal: Any) -> None:
    if not isinstance(goal, dict) or not isinstance(goal.get("stand_pose"), dict):
        raise ServiceError("route goal must contain stand_pose")
    pose = goal["stand_pose"]
    finite_number(pose.get("x"), "stand_pose.x")
    finite_number(pose.get("y"), "stand_pose.y")
    if pose.get("yaw") is not None:
        finite_number(pose["yaw"], "stand_pose.yaw")


class NavigationClient(JsonServiceClient):
    """Query named goals and plan routes without moving a robot."""

    def health(self) -> dict[str, Any]:
        """Read the currently loaded map identity."""
        result = self.request("GET", "/health")
        map_identity(result)
        return result

    def list_waypoints(self) -> dict[str, Any]:
        """Read the live waypoint table rather than hard-coding map indices."""
        result = self.request("GET", "/waypoints")
        map_identity(result)
        if not isinstance(result.get("waypoints"), list):
            raise ServiceError("waypoints must be a list")
        for goal in result["waypoints"]:
            _check_stand_pose(goal)
            if not all(
                isinstance(goal.get(key), str) and goal[key] for key in ("id", "name")
            ):
                raise ServiceError("waypoints must have nonempty id and name")
        return result

    def plan(
        self,
        start: dict[str, Any],
        goals: list[NavigationGoal],
        *,
        step: float | None = None,
    ) -> dict[str, Any]:
        """Plan an ordered route from a finite map-frame starting pose."""
        pose = {
            key: finite_number(start.get(key), f"start.{key}") for key in ("x", "y")
        }
        if start.get("yaw") is not None:
            pose["yaw"] = finite_number(start["yaw"], "start.yaw")
        if not goals:
            raise ValueError("at least one navigation goal is required")
        payload: dict[str, Any] = {
            "start": pose,
            "goals": [_goal_payload(g) for g in goals],
        }
        if step is not None:
            payload["step"] = finite_number(step, "step")
        result = self.request("POST", "/plan", payload)
        map_identity(result)
        if result.get("frame") != "map" or result.get("units") != {
            "length": "meter",
            "angle": "radian",
        }:
            raise ServiceError("route must use map coordinates, meters, and radians")
        legs = result.get("legs")
        if not isinstance(legs, list) or len(legs) != len(goals):
            raise ServiceError("route must contain one leg per requested goal")
        if finite_number(result.get("total_length"), "total_length") < 0:
            raise ServiceError("route length must not be negative")
        returned_start = result.get("start")
        if not isinstance(returned_start, dict) or any(
            finite_number(returned_start.get(key), f"route.start.{key}") != pose[key]
            for key in ("x", "y")
        ):
            raise ServiceError("route start does not match the requested pose")
        warnings = result.get("warnings", [])
        if not isinstance(warnings, list) or not all(
            isinstance(item, str) for item in warnings
        ):
            raise ServiceError("route warnings must be a list of strings")
        for leg in legs:
            if not isinstance(leg, dict):
                raise ServiceError("route legs must be objects")
            _check_stand_pose(leg.get("goal"))
            if finite_number(leg.get("length"), "leg.length") < 0:
                raise ServiceError("route leg length must not be negative")
            path = leg.get("path")
            if not isinstance(path, list) or not path:
                raise ServiceError("each route leg must have a nonempty path")
            for point in path:
                if not isinstance(point, list) or len(point) != 2:
                    raise ServiceError("path points must be [x, y] pairs")
                for value in point:
                    finite_number(value, "path coordinate")
            headings = leg.get("headings")
            if not isinstance(headings, list) or len(headings) != len(path) - 1:
                raise ServiceError("headings must correspond to path segments")
            for value in headings:
                finite_number(value, "heading")
        return result
