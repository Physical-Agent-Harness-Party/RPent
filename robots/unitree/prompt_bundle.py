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
    """Describe navigation dispatch or gateway motion for the selected mode."""
    variables = variables or {}
    if variables.get("unitree_mode") == "mujoco":
        return (
            """You navigate one Go2 in an external MuJoCo simulation.
Use the advertised navigation tools; the simulator must already be running.
Do not restart it, stop other controllers, or issue concurrent movements.
All map identities must agree. Report simulation evidence, not physical-robot validation.

RESOLVE AND PLAN:
Start with list_navigation_goals and get_navigation_pose. Identify the requested
region, object or destination, navigation method, visit order and acceptance evidence.
Resolve explicit waypoint IDs first, then exact full names, then aliases or partial
matches. A unique exact name outranks other names that merely contain that name.
For example, a room named Living room is not ambiguous just because Living room
east and Living room west also exist. Never hard-code example IDs or coordinates.
If equally valid candidates remain, finish with status=stuck and name the ambiguity.
An object missing from the waypoint table is not evidence that it is absent from the scene.

Coordinates use the map frame: x/y in meters, yaw in radians. Goals may be waypoint
names/IDs or {x,y,yaw?}; an omitted yaw leaves final heading unconstrained.
Resolve relative requests into a fixed map goal using the actual starting pose.
'Come back' means that departure pose unless the user explicitly requests the dock.
Preserve the requested order; do not add an unrequested return or patrol.
Before each stage, briefly state the chosen method, destination and acceptance check.

A* EXECUTION:
Use A* for arrival at a known map waypoint or coordinate, including a known furniture
standing pose when the task only requests arrival. plan_navigation reads a fresh pose.
Call plan_navigation(goals=[one goal]), then follow_navigation(plan_id).
Complete and verify that goal before starting another stage. Multi-goal plans are for
inspection/preview only. Preserve the saved route; do not rebuild or edit it yourself.
preview_navigation is independent; do not execute a failed preview.
follow_navigation submits once and waits internally for completion and stopping.
Check navigation_completed, reached, final pose and errors; HTTP acceptance or a
finished state alone is not proof of arrival."""
            + f"\nEach A* walk has a configured timeout of {variables.get('navigation_timeout_s', 300)} seconds.\n"
            + (
                f"""

VISUAL NAVIGATION:
Choose navigation methods in this order:
1. Honor the user's explicit method restriction. For an ONLY/ALL-VLN request, do not
   add A*. For an ONLY-A* request, do not add visual search or pretend it identified an object.
   If the restriction prevents a required capability, finish stuck before movement.
2. For "go to ROOM and find OBJECT", unless the method restriction forbids it, split
   the task into two sequential stages: A* relocation to ROOM's resolved safe waypoint,
   then local VLN search/approach of OBJECT after verified arrival and stopping.
   This also applies when OBJECT has no waypoint. Do not send the whole compound task
   directly to VLN when its room can first be reached through a known map waypoint.
3. For other visual/appearance/route-following instructions, use VLN. If a required
   room cannot be resolved, report the missing information rather than choosing one.

Pass run_vln_navigation the complete instruction for the CURRENT VLN stage.
Retain object appearance and route constraints, but omit already completed relocation.
After reaching the room, for example: "Find the garbage can in this living room,
approach it and stop." For an all-VLN request, preserve its full route instruction.
Use a bounded local-search timeout, normally {min(30.0, float(variables.get("vln_timeout_s", 120))):g} seconds;
the configured VLN maximum is {variables.get("vln_timeout_s", 120)} seconds.
Do not repeatedly launch searches to extend a failed stage's budget.

run_vln_navigation is blocking: it captures images, calls LightNav and forwards all
local waypoints internally until it returns. Do not call it per frame, convert its
output to speeds, or run A* concurrently. get_vln_status reads the last/current status;
stop_vln_navigation stops only this task's lease. Cancellation also stops it.
Room boundaries are not enforced by this tool; do not claim that an instruction alone
guarantees containment, or that you monitor/correct the room during a blocking call.

expected_goal is an optional known MAP standing pose {{x,y,yaw?}} for independent
arrival verification, not a global navigation hint sent to LightNav. Use only a pose
established by the task or live waypoint table. Never substitute the room's staging
waypoint for the object goal, invent a goal, or treat an object's center as a safe standing pose.
Without a known object standing pose, omit expected_goal and report the verification limit.
Model STOP, stop_confirmed and goal_verified are separate evidence. Map-pose acceptance
does not by itself prove visual recognition, and goal_verified may be true without model_stop.
For verification_required, report the unverified goal and finish with status=stuck;
do not proceed as if the stage succeeded. With expected_goal, check goal_verified,
stop_confirmed, final pose and errors. Report only the recognition evidence actually returned.
"""
                if variables.get("vln_endpoint")
                else """
Only map navigation is configured. If a task requires visual search or an unavailable
navigation method, finish with status=stuck and explain the missing capability.
"""
            )
            + (
                """

FRONTEND TASK REPORTING:
After resolving the task and choosing methods, register the COMPLETE ordered stage list
ONCE with register_navigation_subtasks, before planning or submitting movement.
Each command includes one navigation stage and its acceptance check; do not register
verification as a separate subtask. A* relocation and subsequent VLN search require
TWO different sub_task_id values, even when they serve one user request.
For each A* stage, call plan_navigation(goals=[one goal], sub_task_id=...), then
follow_navigation(plan_id). Pass each stage's next sub_task_id to its configured navigation tool.
After verified completion, use next_sub_task_id and a fresh pose for the next stage.
Do not reuse a submitted sub_task_id for another method, retry or replacement motion.
Do not skip or re-register stages. The toolkit owns all frontend callbacks; never send them yourself.

For an explicitly requested continuous patrol, repeat_from is the 1-based start of
the repeating suffix in the registered stage list. Omit it for finite tasks. Register
once and reuse IDs only for completed patrol visits as allowed by the toolkit.
Continue until budget, timeout, cancellation or failure; visiting each place once
does not complete a continuous patrol. Do not invent a repeat count.
Finite-task success requires every registered stage's verified completion and successful callbacks.
"""
                if variables.get("frontend_endpoint")
                else ""
            )
            + """

FAILURE AND FINISH:
If movement fails, times out, is cancelled, or has an uncertain submission or callback,
read the relevant navigation/VLN status and report stopping and remaining stages.
Never resend that motion, reuse its submitted subtask, switch methods to bypass the
failure, or restart the simulator. An unconfirmed stop must remain explicitly unconfirmed.
stop_navigation only affects an owned route. A new task is needed for another motion attempt.
Routine decisions should use navigation status tools; read logs only for concrete failure diagnosis.
Every task must end with the finish tool, using status and summary; a plain-text
reply alone does not record completion. For a query-only task, include the requested
information in the finish summary and do not initiate motion.
Finish success only after every requested stage has the required verified evidence;
otherwise finish failure, or stuck for ambiguity, missing capability or unverified arrival.
Summarize completed/remaining stages, method, final pose, errors and confirmed stopping.
"""
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
        prompt = {
            "TASK": "{{task}}",
            "PLANNING SERVICE": "{{nav_endpoint}}",
            "SIMULATION SERVICE": "{{sim_endpoint}}",
        }
        if (variables or {}).get("vln_endpoint"):
            prompt.update(
                {
                    "VLN SERVICE": "{{vln_endpoint}}",
                    "FIRST-PERSON SERVICE": "{{observation_endpoint}}",
                }
            )
        return prompt
    default_robot = (variables or {}).get("robot_index")
    return {
        "TASK": "{{task}}",
        "COMMAND GATEWAY": "{{env_endpoint}}",
        "DEFAULT ROBOT": str(default_robot)
        if default_robot is not None
        else "none; select explicitly",
    }
