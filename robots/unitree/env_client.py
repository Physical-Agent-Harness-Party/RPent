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

"""Client for the Unitree multi-robot gateway RPC contract.

This command-only interface uses RpcClient directly: it has no Gym step,
action arrays, or automatic environment reset.
"""

from __future__ import annotations

import math
from typing import Any

from robots.unitree.protocol import (
    DEFAULT_QUERY_TIMEOUT_S,
    motion_timeout_s,
    validate_command,
    validate_motion_result,
    validate_robot_index,
)
from rpent.utils.rpc import RpcClient, RpcError


class UnitreeEnvClient:
    """Query gateway-managed robots and validate synchronous motion completion."""

    def __init__(
        self,
        client: RpcClient,
        *,
        timeout_s: float | None = None,
        robot_index: int | None = None,
    ) -> None:
        if robot_index is not None:
            validate_robot_index(robot_index)
        if timeout_s is not None and (not math.isfinite(timeout_s) or timeout_s <= 0):
            raise ValueError("timeout_s must be finite and positive")
        self._client = client
        self._timeout_s = timeout_s
        self.robot_index = robot_index

    # _resolve_robot_index()：确定本次操作哪台机器人
    def _resolve_robot_index(self, robot_index: int | None) -> int:
        index = self.robot_index if robot_index is None else robot_index
        validate_robot_index(index)
        return index

    # 3. list_robots()：查询在线机器人
    def list_robots(self) -> dict[str, Any]:
        """Return the sorted online list, rejecting malformed gateway responses."""
        method = "env.list_robots"
        result = self._client.call(method, timeout_s=DEFAULT_QUERY_TIMEOUT_S)
        if not isinstance(result, dict) or not isinstance(result.get("robots"), list):
            raise RpcError(method, "invalid online robot list")
        robots = result["robots"]
        if type(result.get("total")) is not int or result["total"] != len(robots):
            raise RpcError(method, "total does not match the online robot list")
        previous = 0
        for robot in robots:
            if (
                not isinstance(robot, dict)
                or type(robot.get("robot_index")) is not int
                or robot["robot_index"] <= previous
                or robot.get("status") != "online"
            ):
                raise RpcError(method, "online robot entries must be sorted and unique")
            previous = robot["robot_index"]
        return result

    # 4. get_robot_status()：查询指定机器人的在线状态
    def get_robot_status(self, robot_index: int | None = None) -> dict[str, Any]:
        """Query one robot; a successful query may report that it is offline."""
        index = self._resolve_robot_index(robot_index)
        result = self._client.call(
            "healthz", kwargs={"robot_index": index}, timeout_s=1.0
        )
        if (
            not isinstance(result, dict)
            or type(result.get("robot_index")) is not int
            or result["robot_index"] != index
            or result.get("status") not in ("online", "offline")
        ):
            raise RpcError("healthz", "invalid status for the requested robot")
        return result

    # 5. execute_command()：发送动作，等待并检查完成回执
    def execute_command(
        self, command: dict[str, Any], *, robot_index: int | None = None
    ) -> dict[str, Any]:
        """Wait for dis_move completion; never retry a failed or timed-out motion."""
        index = self._resolve_robot_index(robot_index)
        validate_command(command)
        receipt = self._client.call(
            "env.execute_command",
            args=(command,),
            kwargs={"robot_index": index},
            timeout_s=(
                self._timeout_s
                if self._timeout_s is not None
                else motion_timeout_s(command)
            ),
        )
        if (
            not isinstance(receipt, dict)
            or type(receipt.get("robot_index")) is not int
            or receipt["robot_index"] != index
            or receipt.get("received") is not True
            or receipt.get("completed") is not True
        ):
            raise RpcError(
                "env.execute_command",
                "server did not confirm completed motion for the requested robot",
            )
        try:
            validate_command(receipt.get("command"))
            # JSON numbers may be echoed as int or float; booleans are rejected above.
            if receipt["command"] != command:
                raise ValueError("receipt command does not match the sent command")
            validate_motion_result(receipt.get("motion_result"), command)
        except ValueError as exc:
            raise RpcError("env.execute_command", str(exc)) from exc
        return receipt

    def close(self) -> None:
        """Release the RPC client while leaving the external server running."""
        self._client.close()
