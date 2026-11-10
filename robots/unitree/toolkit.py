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

"""Planner-facing Unitree discovery, status, and structured command tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from robots.unitree.env_client import UnitreeEnvClient
from robots.unitree.protocol import VALIDATION_SCOPE
from rpent.dashboard.events import DashboardEventSink
from rpent.memory import MemoryManager
from rpent.session import EnvState
from rpent.tools import Toolkit, ToolResult, tool


class UnitreeToolkit(Toolkit):
    """Expose gateway queries and synchronous, locally serialized motion tools."""

    def __init__(
        self,
        *,
        env: UnitreeEnvClient,
        dashboard_events: DashboardEventSink,
        memory: MemoryManager,
        state_output_dir: Path,
    ) -> None:
        super().__init__(
            dashboard_events=dashboard_events,
            memory=memory,
            state=EnvState(state_output_dir),
        )
        self._env = env
        self._acknowledged = 0
        self._acknowledged_by_robot: dict[str, int] = {}
        self._completed = 0
        self._completed_by_robot: dict[str, int] = {}
        self.add_tools((self.list_robots, self.get_robot_status, self.execute_command))
        with self.state.record_step(
            state={
                "acknowledged_commands": 0,
                "acknowledged_by_robot": {},
                "completed_commands": 0,
                "completed_by_robot": {},
            }
        ):
            pass
        self._publish_step(self.state.get())

    # 2. list_robots()：给 LLM 查询在线机器人
    @tool(readonly=True)
    def list_robots(self) -> ToolResult:
        """List currently online robots and their gateway-assigned indices."""
        return ToolResult(data=self._env.list_robots())

    # 3. get_robot_status()：给 LLM 查询指定机器人的状态/
    @tool(readonly=True)
    def get_robot_status(self, robot_index: int | None = None) -> ToolResult:
        """Query a robot's last known connectivity, without moving it.

        Args:
            robot_index: Positive gateway index, or omit for the configured default.
        """
        return ToolResult(data=self._env.get_robot_status(robot_index))

    # 4. execute_command()：把 LLM 参数转换为运动命令
    @tool
    def execute_command(
        self,
        dx_m: float,
        dy_m: float,
        arrival_turn_deg: float,
        robot_index: int | None = None,
    ) -> ToolResult:
        """Move to a relative target, then turn, waiting for final acceptance.

        Args:
            dx_m: Target forward (+) or rear (-) offset in the starting body frame, in meters.
            dy_m: Target left (+) or right (-) offset in that same frame, in meters.
            arrival_turn_deg: Extra left (+) or right (-) turn after arrival; preserve full turns.
            robot_index: Positive gateway index, or omit for the configured default.
        """
        command = {
            "type": "dis_move",
            "payload": {
                "dx_m": dx_m,
                "dy_m": dy_m,
                "arrival_turn_deg": arrival_turn_deg,
            },
        }
        return ToolResult(
            data=self._env.execute_command(command, robot_index=robot_index)
        )

    # 5. get_env_state()：记录动作结果和完成次数
    # 这个方法没有 @tool，不是由 LLM 直接调用，而是公共 Toolkit 在执行非只读工具之后调用。
    def get_env_state(
        self,
        *,
        command: dict[str, Any],
        result: dict[str, Any],
        elapsed_s: float,
    ) -> ToolResult:
        """Record validated per-action completion without marking the whole task solved."""
        if result.get("completed") is True:
            self._acknowledged += 1
            self._completed += 1
            key = str(result["robot_index"])
            self._acknowledged_by_robot[key] = (
                self._acknowledged_by_robot.get(key, 0) + 1
            )
            self._completed_by_robot[key] = self._completed_by_robot.get(key, 0) + 1
        with self.state.record_step(
            state={
                "acknowledged_commands": self._acknowledged,
                "acknowledged_by_robot": dict(self._acknowledged_by_robot),
                "completed_commands": self._completed,
                "completed_by_robot": dict(self._completed_by_robot),
            },
            command=command,
            result=result,
            elapsed_s=elapsed_s,
            extras={"validation_scope": VALIDATION_SCOPE},
        ) as step:
            pass
        return ToolResult(data={**result, "step": step})

    def close(self) -> None:
        """Drain tool calls and release the client to the external server."""
        super().close()
        self._env.close()
