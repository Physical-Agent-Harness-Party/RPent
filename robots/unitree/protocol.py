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

"""Unitree dis_move command, completion, and timeout contracts."""

from __future__ import annotations

import math
from typing import Any

MAX_DISTANCE_M = 5.0
POSITION_TOLERANCE_M = 0.07
ANGLE_TOLERANCE_DEG = 3.0
DEFAULT_QUERY_TIMEOUT_S = 10.0
VALIDATION_SCOPE = "reported_motion_completion"
MOTION_FIELDS = ("dx_m", "dy_m", "arrival_turn_deg")
RESULT_NUMBER_FIELDS = (
    "actual_dx_m",
    "actual_dy_m",
    "actual_arrival_turn_deg",
    "total_angle_deg",
    "position_error_m",
    "angle_error_deg",
)


def validate_robot_index(robot_index: int) -> None:
    """Require a positive integer robot identifier, excluding booleans."""
    if type(robot_index) is not int or robot_index <= 0:
        raise ValueError("robot_index must be a positive integer")


def _finite_number(value: Any, field: str) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{field} must be a finite number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    return number


def validate_command(command: dict[str, Any]) -> None:
    """Require the documented dis_move payload and a displacement of at most 5 m."""
    if not isinstance(command, dict):
        raise ValueError("command must be a dis_move object")
    if set(command) != {"type", "payload"} or command.get("type") != "dis_move":
        raise ValueError("command must contain type='dis_move' and payload only")
    payload = command["payload"]
    if not isinstance(payload, dict) or set(payload) != set(MOTION_FIELDS):
        raise ValueError(
            "command.payload must contain exactly dx_m, dy_m, arrival_turn_deg"
        )
    values = {
        name: _finite_number(payload[name], f"command.payload.{name}")
        for name in MOTION_FIELDS
    }
    if math.hypot(values["dx_m"], values["dy_m"]) > MAX_DISTANCE_M:
        raise ValueError("command displacement must not exceed 5 m in total")


def motion_timeout_s(command: dict[str, Any]) -> float:
    """Return the documented control budget plus 30 s for a validated command."""
    payload = command["payload"]
    distance = math.hypot(payload["dx_m"], payload["dy_m"])
    bearing = math.atan2(payload["dy_m"], payload["dx_m"]) if distance else 0.0
    return (
        60.0
        + distance / 0.07
        + (abs(bearing) + abs(math.radians(payload["arrival_turn_deg"]))) / 0.10
    )


def validate_motion_result(motion: Any, command: dict[str, Any]) -> None:
    """Check reported completion data against the default acceptance tolerances."""
    if not isinstance(motion, dict):
        raise ValueError("motion_result must be an object")
    values = {
        name: _finite_number(motion.get(name), f"motion_result.{name}")
        for name in RESULT_NUMBER_FIELDS
    }
    if motion.get("stop_acknowledged") is not True:
        raise ValueError("motion_result.stop_acknowledged must be true")
    if values["position_error_m"] < 0:
        raise ValueError("motion_result.position_error_m must not be negative")
    payload = command["payload"]
    position_error = max(
        values["position_error_m"],
        math.hypot(
            payload["dx_m"] - values["actual_dx_m"],
            payload["dy_m"] - values["actual_dy_m"],
        ),
    )
    angle_error = max(
        abs(values["angle_error_deg"]),
        abs(payload["arrival_turn_deg"] - values["actual_arrival_turn_deg"]),
    )
    # Allow numeric roundoff at a tolerance boundary, not a wider motion tolerance.
    if position_error > POSITION_TOLERANCE_M and not math.isclose(
        position_error, POSITION_TOLERANCE_M, abs_tol=1e-9, rel_tol=0
    ):
        raise ValueError("motion_result exceeds the 0.07 m position tolerance")
    if angle_error > ANGLE_TOLERANCE_DEG and not math.isclose(
        angle_error, ANGLE_TOLERANCE_DEG, abs_tol=1e-9, rel_tol=0
    ):
        raise ValueError("motion_result exceeds the 3 degree arrival-turn tolerance")
