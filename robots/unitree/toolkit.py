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

"""Planner-facing Unitree movement tools and command receipt records."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from robots.unitree.env_client import UnitreeEnvClient
from rpent.dashboard.events import DashboardEventSink
from rpent.memory import MemoryManager
from rpent.session import EnvState
from rpent.tools import Toolkit, ToolResult, tool


class UnitreeToolkit(Toolkit):
    """Expose two exclusive movement tools using the shared Toolkit scheduler."""

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
        self.add_tools((self.move_forward, self.move_backward))
        with self.state.record_step(state={"acknowledged_commands": 0}):
            pass
        self._publish_step(self.state.get())

    @tool
    def move_forward(self) -> ToolResult:
        """Send one forward command (前进) and return the server acknowledgement."""
        return ToolResult(data=self._env.execute_command("前进"))

    @tool
    def move_backward(self) -> ToolResult:
        """Send one backward command (后退) and return the server acknowledgement."""
        return ToolResult(data=self._env.execute_command("后退"))

    def get_env_state(
        self,
        *,
        command: dict[str, Any],
        result: dict[str, Any],
        elapsed_s: float,
    ) -> ToolResult:
        """Record command delivery without assigning physical task success."""
        if result.get("received") is True:
            self._acknowledged += 1
        with self.state.record_step(
            state={"acknowledged_commands": self._acknowledged},
            command=command,
            result=result,
            elapsed_s=elapsed_s,
            extras={"validation_scope": "command_delivery"},
        ) as step:
            pass
        return ToolResult(data={**result, "step": step})

    def close(self) -> None:
        """Drain tool calls and release the client to the external server."""
        super().close()
        self._env.close()
