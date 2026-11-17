# Dispatch RPent simulation tasks with DSH

DSH delegates complete tasks over MCP. The robot-side RPent runner executes A*, VLN,
and MuJoCo operations in the background and persists task records in its jobs directory.
This configuration connects one local simulated Go2, `go2-01`.

## Prerequisites

The local deployment uses:

- `/home/yanlinjie/robot_project/deepseek-harness`: the official source checkout.
- `/home/yanlinjie/robot_project/dsh-tools`: project-local Node and pnpm.
- `/home/yanlinjie/robot_project/dsh-runtime`: DSH profiles, sessions, and runtime data.
- `/home/yanlinjie/robot_project/.venv`: the existing shared Python environment.

The upstream repository is `https://github.com/deepseek-ai/deepseek-harness.git`.
The checked-out revision is `39163dd30f703a79079e2d2446545cff3c453738`.
Source launch requires Node >= 24.9 with development headers and pnpm 11.28.5.
Run `pnpm install --frozen-lockfile` and `pnpm run build` in the checkout before use;
both have been completed on this host.

`dsh_local.env` sources the existing VLN profile and private `.env.local`.
It uses `OPENAI_API_KEY` as the fallback for `DEEPSEEK_API_KEY` and reads the existing
`RPENT_RUNNER_TOKEN`. The DSH patch references credentials instead of embedding values.
Set `DEEPSEEK_BASE_URL` for a custom Messages-compatible endpoint.
Model weights and MuJoCo continue using the existing `robot_project` installation.

## Start the MCP task runner

Start A*, MuJoCo, observation, and any required VLN service using the
[local deployment guide](../../../docs/LOCAL_DEPLOYMENT.md).
Check that the previous runner is idle and stop it before replacing its robot owner.

```bash
cd /home/yanlinjie/robot_project/rpent
set -a
source robots/unitree/dsh_local.env
set +a
../.venv/bin/python -m robots.unitree.task_server \
  --host 127.0.0.1 --port 8780 \
  --jobs-dir output/dsh-integration/jobs \
  --mcp --robot-id go2-01 --task-reporting local
```

The MCP endpoint is `http://127.0.0.1:8780/mcp/`; health is `/healthz`.
The existing `/api/rpend/run` submission, status, log, and stop APIs share this runner.
Local reporting accepts a dispatcher-owned task ID without frontend registration or
callbacks. CLI defaults remain `frontend`; see the [frontend guide](FRONTEND_USAGE.zh-CN.md).

## Dispatch through the official DSH profile

In another terminal:

```bash
cd /home/yanlinjie/robot_project/rpent
bash robots/unitree/run_dsh.sh headless \
  'Delegate to go2-01: use only A* to reach wp_diningtable_4, stop, then report the final outcome and execution evidence.'
```

The wrapper launches the official `dsh` profile with `dsh_mcp.patch.yml`.
Add `--json` for machine-readable run events. DSH discovers the robot, submits the
task, and waits in bounded calls. Only `succeeded` with supporting execution evidence
establishes task success; HTTP 202, exit code zero, and plain text alone do not.

For the Web profile:

```bash
bash robots/unitree/run_dsh.sh web --host 127.0.0.1 --port 3080 --no-open
```

The terminal prints a complete access-token URL under `http://127.0.0.1:3080/`;
open that link. This background instance's link is saved in
`output/dsh-integration/dsh-web-access.url`. Use an existing private access mechanism
or approved tunnel from another computer. The Web profile can use the same MCP patch; actual model-driven
dispatch in this verification uses the headless profile.

## Task tools and lifecycle

| Tool | Purpose |
| --- | --- |
| `list_robots()` | This endpoint's robot, busy state, and IDs requiring reconciliation |
| `submit_robot_task(robot_id, task_id, task_name)` | Start background execution; return `created` and the record |
| `get_robot_task(robot_id, task_id)` | Read business outcome and navigation evidence |
| `wait_robot_task(robot_id, task_id, timeout_s)` | Wait up to 30 seconds; `terminal=false` leaves execution active |
| `get_robot_logs(robot_id, task_id, tail_bytes)` | Read at most 65536 bytes of log tail |
| `stop_robot_task(robot_id, task_id)` | Request cleanup, then query stopping or reconciliation evidence |

One runner owns one task at a time. The same ID and exact text return the previous
record without replay; different text under that ID or a busy runner fails.
Closing MCP or DSH does not cancel an accepted task. Runner shutdown cleans up its
owned child. After an abnormal restart, active records become `interrupted` and
require reconciliation. DSH names tools with `mcp__rpent__`, for example
`mcp__rpent__stop_robot_task`. Verify the task's terminal state and stopping evidence
after cancellation; a timeout or stop acknowledgement does not establish a stop.

## Shutdown and troubleshooting

Use Ctrl+C for foreground services. For a background runner, verify its PID and command
before signalling it; this deployment records the PID in `output/dsh-integration/runner.pid`
and logs in `runner.log` beside it. Stopping the runner leaves A*, MuJoCo, and VLN running.

For this background deployment, inspect the owners before stopping them:

```bash
ps -o pid,pgid,args -p "$(cat output/dsh-integration/runner.pid)"
ps -o pid,pgid,args -p "$(cat output/dsh-integration/dsh-web.pid)"
kill -TERM "$(cat output/dsh-integration/runner.pid)"
kill -TERM -- "-$(cat output/dsh-integration/dsh-web.pid)"
```

- HTTP 401: check that RPent and DSH receive the same `RPENT_RUNNER_TOKEN`.
- HTTP 421: the Host is not trusted. For private-network access, bind the private
  address and add the actual Host with `--mcp-allowed-host go2-01.lan:8780`.
- HTTP 409 or a tool conflict: inspect active and attention IDs before dispatching
  replacement motion.
- Missing MCP tools: inspect DSH stderr and the MCP service. Credential templates
  require the MCP entry to declare `inject: [credentials]`.
- `incomplete`: inspect whether the planner called `finish`; do not infer success
  from a plain-text response.

For additional robots, run a separate runner with a unique `--robot-id` and configure
another DSH MCP entry under a distinct `serverName`. Each endpoint lists itself;
a central queue and cross-robot task dependencies are not implemented here.
Local verification artifacts are under `output/dsh-integration/`.
