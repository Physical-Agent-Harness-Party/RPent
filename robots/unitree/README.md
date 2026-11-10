# Unitree gateway and MuJoCo navigation

[简体中文](README.zh-CN.md)

`--unitree-mode gateway` is the default and uses the multi-robot `dis_move`
protocol described below. `--unitree-mode mujoco` selects the external A* and
simulation HTTP services; it does not connect to the gateway.

## MuJoCo navigation

For setup, a complete two-waypoint patrol command, `--max-turns` termination,
and result inspection, see the [MuJoCo patrol guide (Chinese)](MUJOCO_USAGE.zh-CN.md).
The external A* service's name-based API and deployment examples are documented
in the [navigation service reference (Chinese)](NAVIGATION.zh-CN.md).

Install RPent's base dependencies and configure a planner API. Navigation and
simulation run on the service machine; the RPent client does not need local
MuJoCo or Torch. Unitree requires a normal terminal with a TTY and does not
support Dashboard, interactive, or exploration modes.

The A* service and simulation control service must already be running with the
same `map_id` and `map_version`. Start the simulator on the service machine
before requesting movement. The toolkit does not restart it or stop other
controllers. Give each simulation endpoint to only one RPent runner: status
replies are not documented to always carry a task id, so sharing an endpoint
between runners is unsupported.

```bash
rpent --robot unitree --unitree-mode mujoco \
  --nav-endpoint http://192.168.104.191:8765 \
  --sim-endpoint http://192.168.104.191:8770 \
  --planner api --model openai:deepseek-flash \
  --task 'Go to the billboard, verify arrival, then go to the restroom passage.' \
  --navigation-timeout-s 300 --planner-timeout-s 900 --max-turns 16 \
  --output-dir ./logs/unitree-navigation
```

Replace both addresses with the reachable service endpoints. Gateway flags
`--robot-index` and `--env-timeout-s` are rejected in MuJoCo mode. The
`--navigation-timeout-s` budget applies to each walk or independent preview;
`--planner-timeout-s` must cover the complete task and model calls. HTTP requests
bypass environment proxies and do not automatically retry movements.

`UnitreeMujocoToolkit` exposes:

| Tool | Behavior |
| --- | --- |
| `list_navigation_goals()` | Read live waypoint names, indices, and standing poses |
| `get_navigation_pose()` | Read the current map-frame pose (meters and radians) |
| `plan_navigation(goals, step=None)` | Plan from a fresh pose; return a saved `plan_id` and summary |
| `preview_navigation(plan_id)` | Preview independently and retain its report |
| `follow_navigation(plan_id)` | Submit one single-goal route and poll until settled arrival |
| `get_navigation_status()` | Read service, pose, and owned navigation status |
| `stop_navigation()` | Request and verify stopping of a task owned by this toolkit |

Goals can be waypoint names/ids or `{x, y, yaw?}` objects supported by the newer
planning service. Coordinate goals require that service capability; the older
name-only planner returns an error. An omitted coordinate yaw leaves the final
heading unconstrained. Scene-graph querying and simulator administration are
not exposed. Multi-goal plans can be inspected or previewed, but following
requires a single goal: plan and verify each destination before the next so the
service cannot skip a failed destination and visit a later one first.

The toolkit passes the saved route to `/walk`; it does not reconstruct paths or
convert map points to body-frame `dis_move` commands. Acceptance is not arrival:
it waits for `state=finished`, successful per-leg results, no fall, and final
pose/error checks (0.25 m and, when requested, 0.2 radians). Failed previews,
changed maps, stale starts beyond 0.3 m, and already-completed plans are rejected.

On cancellation or timeout after acceptance, the toolkit requests stopping and
checks finished state. A lost acceptance response leaves submission uncertain;
it does not resend or blindly stop an unidentified task. Verify the simulation
service before retrying. An unconfirmed stop is retained as an error. The normal
`stop_navigation` tool cannot interrupt a currently exclusive tool call; planner
cancellation reaches the dedicated cancellation hook before waiting for it.

Artifacts are stored under `navigation/plans/`, `navigation/previews/`, and
`navigation/walks/`. `states.json` records the last pose, active task, errors,
and `completed_navigation_goals`; the transcript records tool calls. Navigation
success is reported by the tool's `navigation_completed` field. Overall
`environment_success` remains unset; simulation arrival is not physical motion
verification. No finalizer or exploration capability is enabled for this mode.

## Gateway mode

This backend connects to a local HTTP RPC gateway, lists online robots, and waits
for synchronous `dis_move` completion. The communication programs own body-initiated
WebSocket connections, registration, heartbeats, and per-robot busy admission.
RPent reuses its RPC clients, tool scheduler, and state artifacts without calling
a body SDK directly.

## Tools and motion semantics

| Tool | Purpose |
| --- | --- |
| `list_robots()` | List online robots |
| `get_robot_status(robot_index=None)` | Query recent robot connectivity |
| `execute_command(dx_m, dy_m, arrival_turn_deg, robot_index=None)` | Wait for a relative move, stopping, and final acceptance |

An omitted index uses `--robot-index`. Without a default, specify the target in
each call. An explicit index may select another robot; the default is not an
access restriction.

`dx_m` and `dy_m` are target offsets in meters in the body frame at the start of
this move: +x forward, +y left. The body turns toward that fixed target and walks
forward, stops, then turns `arrival_turn_deg` relative to its actual arrival
heading (+left). A rear target requires turning around and walking forward,
not direct backward walking; its full hardware execution has not passed testing.
Zero displacement turns in place. Preserve 450-degree turns without normalization.
All three values must be finite numbers and combined displacement must be at most
5 m. Speed, duration, waypoint lists, and remote stop commands are not exposed.
The 5 m limit is an input bound, not evidence of full-distance hardware testing.

The tool constructs `{"type":"dis_move","payload":{...}}` internally. Tasks can
specify relative movement directly, such as going 0.4 m forward and 0.3 m left,
then turning left 30 degrees. Supply enough distance and target information.

## Requirements and read-only queries

Install base dependencies in the active environment and configure your planner API:

```bash
pip install -e .
```

Use a normal terminal: Unitree retains the physical-robot terminal requirement.
It defaults to local memory without an HF corpus download. The communication team
provides the local port; the default is `http://127.0.0.1:8115`. See the
[RPC interface specification (Chinese)](INTERFACE.zh-CN.md).
For physical deployment, follow the [real-robot test guide (Chinese)](REAL_ROBOT_TEST.zh-CN.md)
for read-only checks, one motion, LLM tool calls, and multi-robot routing.

```bash
rpent --robot unitree --planner api --model openai:deepseek-flash \
  --env-endpoint http://127.0.0.1:8115 \
  --task 'List online robots and query robot 1 status. Do not send actions.' \
  --output-dir ./logs/unitree-status
```

Startup probes `env.list_robots` without an unbound `healthz` call. With
`--robot-index N`, the target must also be online; unknown or offline targets fail
startup. Without a default target, an empty list still allows read-only tasks.

## Wait budgets and completion

By default each command gets a calculated RPC wait:

```text
L = hypot(dx_m, dy_m)
bearing = atan2(dy_m, dx_m), or 0 for zero displacement
wait_seconds = 60 + L / 0.07 + (abs(bearing) + abs(arrival_turn_deg * pi / 180)) / 0.10
```

This includes the body control budget plus 30 seconds for communication and
stopping. For `(0.4, 0.3, 30)` the wait is about 78.8 seconds. An explicit
`--env-timeout-s N` overrides the command wait and service readiness budget exactly;
an insufficient override can still time out. Automatic readiness uses 10 seconds.
Individual startup probes and status queries use 1 second; list queries use 10.
Post-readiness list validation and default-target checks are outside that budget.
Gateway and WebSocket waits must also cover execution. `--planner-timeout-s`
covers the whole session, including all moves and LLM calls.

The client requires boolean `received`, `completed`, and `stop_acknowledged` to be
true, matching target and command, and six finite motion-result numbers. Both
reported errors and errors derived from actual displacement/turn must meet the
default 0.07 m position and 3 degree arrival-turn tolerances. The body verifies
near-rest state; RPent has no independent sensor verification. A timeout or failure
may follow partial motion. The client does not automatically resend, and
connection closure does not stop the robot.

## Local mock test

The included server simulates online robots and ideal completion measurements.
It has no physical connection, SDK calls, WebSocket implementation, or heartbeats.

Start it in one terminal:

```bash
python -m robots.unitree.env_server \
  --host 127.0.0.1 --port 8115 --robot-indices 1 2 \
  --commands-file ./logs/unitree-receiver/commands.jsonl
```

In another terminal:

```bash
rpent --robot unitree --planner api --model openai:deepseek-flash \
  --env-endpoint http://127.0.0.1:8115 \
  --planner-timeout-s 300 \
  --task 'Local mock: robot 1 goes to 0.4 m forward and 0.3 m left, stops and turns left 30 degrees; after completion robot 2 turns left 450 degrees in place.' \
  --output-dir ./logs/unitree-multi
```

For one target, add `--robot-index 1` and refer to the default robot in the task.
The mock also supports `--transport socket` and a `socket://127.0.0.1:8115` client
endpoint. Production communication uses the documented HTTP transport.

Mock receipts include `mode: "mock"`, `completed: true`, and `motion_result`.
These are synthetic values, not evidence of hardware motion. Busy robots reject
new commands without replacing or queuing them; different clients may move
different simulated robots concurrently. A single Toolkit still serializes all
motion tools, including moves for different robots.

Outputs include `states.json` and `transcript_unitree_<run-id>.json`. State retains
`acknowledged_commands` and adds `completed_commands` and `completed_by_robot`;
full motion results appear in action records. The `validation_scope` is
`reported_motion_completion`. One completed action does not automatically mark
the whole task or physical scene as solved.

## Local checks

```bash
pip install -e ".[test]"
python -m pytest tests/unit_tests/robots/unitree -v
```

Tests cover input bounds, completion and stop receipts, error acceptance, calculated
waits, startup cleanup, state recording, and the public CLI with a scripted API
planner. Real localhost HTTP/socket tests also cover per-robot busy rejection,
independent robots, and remote completion after client timeout. No external model,
hardware, or GPU is required.
