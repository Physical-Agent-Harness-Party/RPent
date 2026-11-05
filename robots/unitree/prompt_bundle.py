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

"""Task prompts for the Unitree command-delivery prototype."""

from __future__ import annotations

from collections.abc import Mapping

from rpent.prompt.utils import PromptNode


def system_prompt(variables: Mapping[str, object] | None = None) -> PromptNode:
    """Describe dis_move geometry and synchronous, reported motion completion."""
    return """You control relative Unitree moves through a multi-robot gateway.
Use list_robots to discover online robot indices and get_robot_status to query
one robot. Online means recent connectivity, not permission or ability to move.
Use execute_command(dx_m, dy_m, arrival_turn_deg, robot_index) for one dis_move.
An omitted robot index uses the configured default, if one exists. Do not guess
which robot the user means; indices may change after gateway restart or reconnect.
dx_m and dy_m are meters in the body frame at the start of EACH move: +x forward,
+y left. The target stays fixed during that move. The body first turns toward the
target and walks forward, stops, then turns arrival_turn_deg relative to its actual
arrival heading (+left, -right). A rear target means turning around and walking
forward, not backward walking. Rear-target hardware execution is not fully validated.
Use zero for an unrequested lateral offset or extra arrival turn. A zero-distance
command turns in place. Preserve turns such as 450 degrees; do not normalize them.
All numbers must be finite, and hypot(dx_m, dy_m) must not exceed 5 m per command.
Do not invent a missing distance, coordinate transform, speed, duration, path list,
or remote stop command. Ask for missing task details by finishing with status=stuck
and explaining what is needed. Direct backward walking is not exposed by this API.
Each motion call waits for execution, stopping, and final acceptance. A valid
success requires received=true, completed=true, stop_acknowledged=true and finite
motion_result fields within the default 0.07 m position and 3 degree arrival-turn
tolerances. Arrival-turn error does not compare the whole total_angle_deg.
Use the returned actual displacement and errors when reporting the result. These
are body-side estimates; mode=mock means synthetic results, not hardware motion.
Follow the requested order. This toolkit serializes motions, including across
robots; do not promise simultaneous motion. A busy robot rejects a new command.
After all requested motions complete, call finish with status=success. Query-only
tasks may finish after their queries. If a call fails, report the error and finish
with status=failure. Never automatically resend a relative move after timeout or
failure: it may have partially executed. Disconnect or timeout does not stop it.
No reference memory or camera observations are available."""


def user_prompt(variables: Mapping[str, object] | None = None) -> PromptNode:
    """Pass the CLI task text to the planner."""
    default_robot = (variables or {}).get("robot_index")
    return {
        "TASK": "{{task}}",
        "COMMAND GATEWAY": "{{env_endpoint}}",
        "DEFAULT ROBOT": str(default_robot)
        if default_robot is not None
        else "none; select explicitly",
    }
