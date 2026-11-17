# 使用 DSH 调度 RPent 仿真任务

DSH 通过 MCP 将完整任务交给机器人侧 RPent，RPent 在后台调用 A*、VLN 和 MuJoCo。
任务记录保存在 RPent 的 jobs 目录中，查询、等待和取消共用同一个 runner。
当前配置连接本机 `go2-01`，验证范围是 Go2 仿真。

## 准备运行环境

本部署使用以下目录：

- `/home/yanlinjie/robot_project/deepseek-harness`：克隆的 DSH 官方源码。
- `/home/yanlinjie/robot_project/dsh-tools`：项目内 Node 和 pnpm。
- `/home/yanlinjie/robot_project/dsh-runtime`：DSH 配置、会话及运行数据。
- `/home/yanlinjie/robot_project/.venv`：RPent 和仿真的共享 Python 环境。

官方源码地址为 `https://github.com/deepseek-ai/deepseek-harness.git`。
当前检出提交为 `39163dd30f703a79079e2d2446545cff3c453738`。
源码运行需要带开发头文件的 Node >= 24.9、pnpm 11.28.5，并先在源码目录执行
`pnpm install --frozen-lockfile` 和 `pnpm run build`。本机已完成安装和构建。

`dsh_local.env` 复用 `vln_local.env` 和现有私有 `.env.local`，将已有的
`OPENAI_API_KEY` 作为 DSH 的 `DEEPSEEK_API_KEY` 回退来源。
`RPENT_RUNNER_TOKEN` 也从现有配置读取；补丁文件使用凭据引用，不写入密钥值。
自定义 Messages 兼容模型服务时，设置 `DEEPSEEK_BASE_URL`。
模型权重和 MuJoCo 环境沿用 `robot_project` 的现有部署。

## 启动 RPent MCP 服务

先按项目的 [本地部署说明](../../../docs/LOCAL_DEPLOYMENT.md) 启动 A*、MuJoCo、观测和需要的 VLN 服务。
确认旧 RPent runner 没有活动任务，再关闭旧 runner，避免两个服务争用机器人。

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

服务的 MCP 地址为 `http://127.0.0.1:8780/mcp/`，健康检查为 `/healthz`。
原有 `/api/rpend/run` 提交、状态、日志和停止接口仍可用，和 MCP 共用任务状态。
使用 `local` 模式时，调度端直接提供稳定的任务 ID，不调用网页后端登记或回调。
CLI 默认仍为 `frontend` 模式；原有前端使用方式参见 [前端说明](FRONTEND_USAGE.zh-CN.md)。

## 通过 DSH 下发任务

另开终端执行一次任务。启动脚本使用官方 `dsh` profile，并加载 `dsh_mcp.patch.yml`：

```bash
cd /home/yanlinjie/robot_project/rpent
bash robots/unitree/run_dsh.sh headless \
  '将任务分配给 go2-01：仅使用 A* 前往客厅餐桌旁 wp_diningtable_4，到达并停稳后结束。等待最终结果并报告执行证据。'
```

需要机器可读事件时加 `--json`。DSH 先发现机器人，再提交和分段等待任务。
只有 RPent 的业务状态为 `succeeded` 且执行证据满足目标时，才能报告成功。
`202`、子进程退出码 0、普通文本回答均不单独证明任务成功。

需要 DSH 网页界面时，在终端运行：

```bash
bash robots/unitree/run_dsh.sh web --host 127.0.0.1 --port 3080 --no-open
```

终端会打印 `http://127.0.0.1:3080/` 下带访问令牌的完整链接，浏览器使用该链接访问。
本次后台实例的完整链接保存在 `output/dsh-integration/dsh-web-access.url`。
其他电脑使用已有的私有访问方式或经批准的隧道。
网页 profile 可以使用相同的 MCP 配置；本次真实调度验证使用的是 headless profile。

## MCP 工具和任务约束

| 工具 | 用途 |
| --- | --- |
| `list_robots()` | 当前端点的机器人 ID、忙闲状态和待核对任务 |
| `submit_robot_task(robot_id, task_id, task_name)` | 提交后台任务，快速返回 `created` 和任务记录 |
| `get_robot_task(robot_id, task_id)` | 查询业务状态、完成结果和导航证据 |
| `wait_robot_task(robot_id, task_id, timeout_s)` | 最长等待 30 秒；`terminal=false` 时任务仍在执行 |
| `get_robot_logs(robot_id, task_id, tail_bytes)` | 读取最多 65536 字节的日志尾部 |
| `stop_robot_task(robot_id, task_id)` | 请求取消；随后查询到停止或待核对状态 |

同一 runner 同时执行一个任务。同一 ID 和完全相同的文本重复提交会返回原记录，
不会再次执行；同一 ID 换文本或 runner 忙时会报错。
任务在 MCP 连接关闭后继续执行。关闭 DSH 进程不会取消 RPent 中已经接收的任务。
RPent runner 关闭时会清理它持有的任务；异常退出后的活动记录标为 `interrupted`，需核对后再使用。

DSH 工具名称使用 `mcp__rpent__` 前缀，例如 `mcp__rpent__stop_robot_task`。
取消后必须检查业务终态和停车证据，不能把通信超时或收到取消请求当作已停稳。

## 停止和排查

前台启动的服务使用 Ctrl+C。停止后台服务前，核对 PID 对应的命令；本次 MCP runner 的 PID
记录在 `output/dsh-integration/runner.pid`，日志为同目录 `runner.log`。
停止 MCP runner 不会关闭 A*、MuJoCo 或 VLN。

本次后台实例可以先核对 PID 和进程组，再停止：

```bash
ps -o pid,pgid,args -p "$(cat output/dsh-integration/runner.pid)"
ps -o pid,pgid,args -p "$(cat output/dsh-integration/dsh-web.pid)"
kill -TERM "$(cat output/dsh-integration/runner.pid)"
kill -TERM -- "-$(cat output/dsh-integration/dsh-web.pid)"
```

- `401`：检查 `RPENT_RUNNER_TOKEN` 在 RPent 和 DSH 启动环境中是否一致。
- `421`：MCP 的 Host 检查拒绝了访问地址。私网部署时，绑定对应私网地址，
  并显式增加 `--mcp-allowed-host go2-01.lan:8780` 等实际 Host。
- `409` 或工具冲突：检查活动任务和 `attention_task_ids`，先核对已有执行，避免重复运动。
- MCP 工具未出现：检查 DSH stderr 和 MCP 服务；凭据模板的插件需要 `inject: [credentials]`。
- `incomplete`：检查运行日志是否实际调用 `finish`，不能从普通文本推断成功。

接入其他机器人时，每台运行一个 runner，并分配唯一 `--robot-id`。
在 DSH 增加对应 MCP 配置，使用不同 `serverName` 区分各端点。
当前端点只列出自身机器人，尚未实现中心任务队列或跨机器人任务依赖管理。

本机验证和完整执行记录位于 `output/dsh-integration/`。
