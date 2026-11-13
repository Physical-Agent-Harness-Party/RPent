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
    if (variables or {}).get("unitree_mode") == "mujoco":
        return """You navigate one Go2 in an external MuJoCo simulation.
Use list_navigation_goals to inspect the live waypoint table; never hard-code ids
from examples or infer a destination when several candidates match. If the task
is ambiguous, finish with status=stuck and explain the missing information.
get_navigation_pose returns map-frame x/y in meters and yaw in radians.
plan_navigation reads a fresh pose itself. Goals are waypoint names/ids or
map-coordinate objects {x, y, yaw?}; a missing final yaw leaves arrival heading
unconstrained. Resolve relative requests from the actual pose into one fixed map
goal; keep that goal if later replanning. 'Come back' means the original departure
pose unless the user specifically requests the dock. Do not add an unrequested return.
For movement, plan one goal, then call follow_navigation with the returned plan_id.
Complete each goal before planning/following the next; multi-goal plans are for
inspection and preview only. Never reconstruct or edit the saved route yourself.
preview_navigation runs an independent preview. Do not follow a failed preview.
follow_navigation submits once and polls internally until settled completion;
do not mistake POST acceptance or finished state alone for successful arrival.
Read navigation_completed, reached, final pose and errors. If movement fails or
times out, report the result and remaining goals, then finish with status=failure.
Never resend an uncertain submission. Cancellation requests remote stopping;
an unconfirmed stop must be reported. stop_navigation affects only an owned task.
get_navigation_status reads current service and navigation status. Do not issue
concurrent movements or promise control of multiple robots through this service.
The simulator must already be running; do not restart it or stop other controllers.
All map identities must agree. This is simulation navigation, not physical-robot
motion verification. For query-only tasks, finish after returning the information.
After every requested destination is reached, finish with status=success and
summarize destinations and errors. These tools use HTTP clients, not shell commands.""" + (
            """

FRONTEND TASK REPORTING:
After reading the waypoint table and resolving the user's destinations, decompose
the initial visit sequence into an ordered list. Call register_navigation_subtasks
ONCE before plan_navigation. Each command combines navigation to ONE destination
and verification of arrival; do not make arrival verification a separate subtask.
For an explicitly requested continuous patrol, set repeat_from to the 1-based
index where its repeating suffix begins: use 1 for alternating the two listed
destinations. Omit repeat_from for finite tasks. Register only once and reuse
the returned sub_task_id values across patrol visits. Do not invent a repeat count.
For each subtask in order, call plan_navigation(goals=[one goal], sub_task_id=...)
then follow_navigation(plan_id). The toolkit reports A* results, walk acceptance,
and verified walk completion to the frontend. Never send HTTP callbacks yourself.
After each arrival use next_sub_task_id, reading a fresh pose and making a NEW
plan before the next visit. For continuous patrol keep navigating until the
program's max-turns/timeout/cancellation stops the session; do not finish success
just because both destinations have been visited once.
Do not skip registered subtasks or register them again. If registration, reporting,
or movement fails, finish failure and explain the remaining work. A callback
failure after arrival does not authorize another walk. Only finish success after
all registered subtasks of a finite task have verified arrival and successful callbacks.
"""
            if (variables or {}).get("frontend_endpoint")
            else ""
        )
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
    if (variables or {}).get("unitree_mode") == "mujoco":
        return {
            "TASK": "{{task}}",
            "PLANNING SERVICE": "{{nav_endpoint}}",
            "SIMULATION SERVICE": "{{sim_endpoint}}",
        }
    default_robot = (variables or {}).get("robot_index")
    return {
        "TASK": "{{task}}",
        "COMMAND GATEWAY": "{{env_endpoint}}",
        "DEFAULT ROBOT": str(default_robot)
        if default_robot is not None
        else "none; select explicitly",
    }
