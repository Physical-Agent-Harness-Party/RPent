# 前端与 HTTP 服务

网页后端先创建父任务，再调用 RPent 的 HTTP 服务。RPent 启动模型会话，登记子任务，调用 A* 和 MuJoCo，并把结果写回网页后端。

```text
网页 → 网页后端创建父任务 → RPent HTTP 服务 → run_rpent.sh → 模型 → A*／MuJoCo
             ↑                                              │
             └────────── 子任务登记与导航结果回调 ────────────┘
```

| 地址／配置 | 职责 |
| --- | --- |
| `RPENT_FRONTEND_ENDPOINT`，默认 `http://121.36.57.9:80` | 网页后端，创建父任务、接收子任务和数据 |
| RPent 主机 `8780` 端口 | 接收启动、查询、日志和停止请求 |
| `RPENT_NAV_ENDPOINT`／`RPENT_SIM_ENDPOINT` | A*／MuJoCo 服务，不接收前端任务编号 |

## 1. 准备与启动服务

按 [MuJoCo 使用指南](MUJOCO_USAGE.zh-CN.md) 配置 `.env.local` 并完成准备检查。脚本自动加载私有配置，文件中的赋值覆盖同名环境变量；HTTP 服务自身的令牌需在启动前 `source`。

如果尚未配置，在 `.env.local` 中设置独立的访问令牌，并将它配置给网页后端：

```bash
export RPENT_RUNNER_TOKEN='替换为随机长字符串'
```

在 RPent 主机执行：

```bash
cd /home/yanlinjie/rpent/RPent
source .env.local
source robots/unitree/prepare_mujoco.sh --skip-install --start-simulator
.venv/bin/python -m robots.unitree.task_server --host 0.0.0.0 --port 8780
```

保持服务进程运行。省略 `--host` 时仅监听 `127.0.0.1`；非本机监听必须设置令牌。远端请求使用 RPent 主机的可达 IP，`0.0.0.0` 仅为监听地址。共享令牌保存在服务器端，浏览器调用自己的网页后端；跨公网连接通过 HTTPS 反向代理并限制访问来源。

服务自动分配 TTY，复用现有 FastAPI／Uvicorn 依赖。默认记录目录为 `logs/http-runner/`，可用 `--jobs-dir` 修改。使用单 worker，不启用 reload；同一记录目录有文件锁、一次只执行一个任务。锁不覆盖手动 CLI 或其他记录目录，同一 MuJoCo 端点仍须保持单个调用方。

## 2. 发送一次完整请求

在 RPent 主机另开终端执行。下面会创建真实父任务并触发导航；网页后端实现同样的两个 POST 即可。

```bash
cd /home/yanlinjie/rpent/RPent
source .env.local
RUNNER_URL='http://127.0.0.1:8780'
TASK_TEXT='先去广告牌，确认到达后，再去厕所通道，再去母婴室。失败或超时则结束并报告。'

curl --noproxy '*' -fsS --max-time 10 "$RUNNER_URL/healthz"

CREATE_BODY="$(.venv/bin/python -c 'import json,sys; print(json.dumps({"task_name":sys.argv[1]}, ensure_ascii=False))' "$TASK_TEXT")"
CREATE_RESP="$(curl -fsS --max-time 15 -X POST "${RPENT_FRONTEND_ENDPOINT:-http://121.36.57.9:80}/api/rpend" \
  -H 'Content-Type: application/json' --data-binary "$CREATE_BODY")"
TASK_ID="$(.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<<"$CREATE_RESP")"
echo "task_id=$TASK_ID"

RUN_BODY="$(.venv/bin/python -c 'import json,sys; print(json.dumps({"task_id":sys.argv[1],"task_name":sys.argv[2]}, ensure_ascii=False))' "$TASK_ID" "$TASK_TEXT")"
curl --noproxy '*' -i --max-time 15 -X POST "$RUNNER_URL/api/rpend/run" \
  -H "Authorization: Bearer $RPENT_RUNNER_TOKEN" \
  -H 'Content-Type: application/json' --data-binary "$RUN_BODY"
```

从网页后端主机请求时，将 `RUNNER_URL` 改为 RPent 主机可达地址，并配置相同的令牌。启动请求只接受 `task_id`、`task_name`：编号须来自父任务响应 `id`，文本与原任务相同。编号为长度 1–256 的字符串，不能含空白或路径分隔符，也不能是 `.`、`..`；文本非空白且最长 20000 字符。命令、路径、模型和服务地址由 RPent 主机配置。

新任务返回 HTTP `202`，表示子进程已启动，尚未代表到达。响应含 `status_url`、`logs_url`、`stop_url`、`pid` 和运行目录；`output_dir` 刚启动时可能为 `null`。相同编号和文本重试返回 `200` 及原记录，不重复执行，正常重启后仍有效。不同文本复用编号、其他任务正在运行或存在待核对记录时返回 `409`。启动请求可以按原编号重试核对；失败任务重新执行应先核对记录，再创建新父任务。

## 3. 查询、日志和停止

以下业务接口使用相同的 Bearer 认证，`/healthz` 无需认证。健康响应的 `ok` 只表示启动服务在运行，不验证模型和仿真状态；接口说明在 `/docs`。

```bash
curl --noproxy '*' -fsS --max-time 10 \
  -H "Authorization: Bearer $RPENT_RUNNER_TOKEN" \
  "$RUNNER_URL/api/rpend/run/$TASK_ID" | .venv/bin/python -m json.tool

curl --noproxy '*' -fsS --max-time 10 \
  -H "Authorization: Bearer $RPENT_RUNNER_TOKEN" \
  "$RUNNER_URL/api/rpend/run/$TASK_ID/logs?tail_bytes=16384"

curl --noproxy '*' -fsS --max-time 10 -X POST \
  -H "Authorization: Bearer $RPENT_RUNNER_TOKEN" \
  "$RUNNER_URL/api/rpend/run/$TASK_ID/stop"
```

网页后端可每 1–2 秒查询状态。`navigation` 是最近已落盘的工具状态，行走过程中可能尚未更新；`finish.summary`、`error`、`stats` 来自 transcript。日志接口返回 `{"task_id":"...","text":"..."}`，默认末尾 16384 字节，上限 65536 字节。

| `status` | 含义 |
| --- | --- |
| `starting`／`running`／`stopping` | 启动中／执行中／停止清理中 |
| `succeeded` | 正常退出、transcript 明确成功且无错误 |
| `budget_exhausted` | 达到 `max-turns` 停止，不等于全部目标完成 |
| `failed`／`incomplete` | 执行失败／正常退出但缺少明确完成证据 |
| `stopped` | 停止请求后本地进程已结束，远端状态仍看导航记录 |
| `requires_attention`／`interrupted` | 强制结束、运动状态不确定，或重启发现未记录结果；阻止新任务 |

未知编号返回 `404`，令牌错误返回 `401`，字段错误返回 `422`，无法创建进程返回 `503`，错误体为 `{"detail":...}`。首次启动的 `202` 和进程退出码 `0` 均不能单独证明任务成功。

停止接口先向自有进程组发送 `SIGINT`，等待 toolkit 清理；30 秒未退出时升级到 `SIGTERM`，再等 2 秒后使用 `SIGKILL`。正常关闭服务也会清理子进程。强制结束不证明远端运动停止。

异常记录须先核对旧进程、仿真状态及回调。确认无遗留执行后，停止服务，将对应的 `logs/http-runner/<编号哈希>/` 目录移出记录目录归档，再重启服务。归档同时移走该编号的幂等记录，后续任务使用新父任务编号。

## 4. 子任务与结果回调

以下请求由 RPent 发给 `RPENT_FRONTEND_ENDPOINT`，不是发给 `8780` 启动服务。

模型先查询实时点位，再通过 `register_navigation_subtasks` 一次登记完整有序列表：

```http
POST /api/rpend/{task_id}
Content-Type: application/json
```

```json
[{"sub_task_1":"前往广告牌并确认到达"},{"sub_task_2":"前往厕所通道并确认到达"}]
```

网页后端按请求顺序返回相同数量的唯一编号：

```json
{"items":[{"id":"sub-101"},{"id":"sub-102"}]}
```

模型用下一子任务编号规划，RPent 在本地关联 `task_id → sub_task_id → plan_id → walk_id`，A* 和 MuJoCo 请求体沿用原协议。每次结果上传为：

```http
POST /api/rpend/{task_id}/{sub_task_id}
Content-Type: application/json
```

```json
{
  "source_type":"sil",
  "data":{
    "task_id":"parent-001","sub_task_id":"sub-101",
    "stage":"walk_finished","plan_id":"plan_example",
    "result":{"ok":true},"navigation_completed":true
  }
}
```

`result` 示例已精简，实际保留服务完整响应。失败时可为 `null` 并携带 `error`。

| `stage` | 含义 |
| --- | --- |
| `astar` | 规划结果；失败时 `plan_id` 可能为 `null` |
| `walk_accepted` | 行走已受理，`navigation_completed=false` |
| `walk_finished` | 行走结束且通过验收，`navigation_completed=true` |
| `walk_error` | 行走、验收、超时或取消失败，带错误和最近状态 |

正常每次访问有三条结果，不逐次回传内部轮询。网页按父任务、子任务、`plan_id` 保存阶段历史，避免覆盖前一阶段。结果接口返回 HTTP 2xx 即确认接收，也兼容 204；JSON 中 `ok:false` 视为拒绝。实测数据集响应的整数 `id` 可用；顶层 `task_id` 指子任务，原请求数据放在 `extra_meta`，`source_type` 可能规范为 `sim`，`status:ready` 仅表示记录已创建。

子任务和数据集响应编号可为字符串或整数，RPent 统一转为字符串。仍兼容旧 `task_id/sub_tasks` 编号映射，以及 `RPENT_FRONTEND_SUBTASKS_FORMAT=object` 的旧登记体；当前对接使用上述数组与 `items` 即可。

持续往返只登记一次：两点循环用 `repeat_from=1`，先到 A 再循环 B/C 用 `repeat_from=2`。该参数由模型工具设置，不是 HTTP 启动请求字段；每次访问产生新计划，复用子任务编号，直到预算、超时、取消或失败结束。父任务整体结果暂不单独回调，网页后端通过第 3 节查询后更新页面或数据库。

## 5. 配置、排错与终端入口

| 环境变量 | 默认值／用途 |
| --- | --- |
| `RPENT_CONFIG_FILE` | 启动脚本默认加载仓库 `.env.local`；可指定其他 Bash 配置文件 |
| `RPENT_FRONTEND_ENDPOINT`／`RPENT_FRONTEND_SUBTASKS_FORMAT` | 网页后端根地址／登记格式，默认 `array` |
| `RPENT_RUNNER_TOKEN` | HTTP 服务访问令牌，修改后重启服务 |
| `RPENT_VENV_DIR`／`RPENT_RUNS_DIR` | 仓库 `.venv`／导航输出父目录 `logs` |
| `RPENT_NAVIGATION_TIMEOUT_S`／`RPENT_PLANNER_TIMEOUT_S`／`RPENT_MAX_TURNS` | `300`／`900`／`16` |

前端回调沿用代理环境；A* 和 MuJoCo 直接连接，示例对本机启动服务使用 `--noproxy '*'`。模型 API Key 只保存在 RPent 主机配置中，不放入业务请求。

子任务登记与结果回调单次超时默认 10 秒，不自动重试。失败会阻止后续运动；行走已受理时尝试停止自有任务，已经到达时保留到达记录，不能通过重走来补发回调。证据在运行目录的 `frontend/subtasks.json`、`frontend/requests/*.json` 和导航记录中。

只检查网页后端接口、不请求模型或运动时运行 `.venv/bin/python -m robots.unitree.check_frontend`，会创建标有测试用途的父任务、三个子任务和一条数据。成功输出 `ok:true`，报告保存在 `logs/frontend-check-*/frontend_check.json`；可用 `--timeout-s 30` 调整超时。

终端可用 `bash run_rpent.sh "$TASK_ID" "$TASK_TEXT"` 代替 HTTP 启动，仍须使用已创建的父任务编号。`run_rpend.sh` 是兼容入口，两者均自动加载配置并创建独立运行目录。已经通过 HTTP 提交的任务不要再手动启动相同任务。
