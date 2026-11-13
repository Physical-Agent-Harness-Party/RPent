# MuJoCo 导航使用指南

RPent 调用外部 A* 和 MuJoCo 服务，逐段规划、执行并确认到达。本文给出两点持续往返命令；已有网页通过 HTTP 提交任务的方法见 [前端与 HTTP 服务](FRONTEND_USAGE.zh-CN.md)。

## 1. 配置与准备

在仓库根目录创建私有配置，已有文件时保留：

```bash
cd /home/yanlinjie/rpent/RPent
[ -f .env.local ] || cp .env.example .env.local
chmod 600 .env.local
```

编辑 `.env.local`，填写 `OPENAI_API_KEY`。DeepSeek 配置使用 `OPENAI_BASE_URL='https://api.deepseek.com'`、`RPENT_MODEL='openai:deepseek-flash'`，认证变量仍叫 `OPENAI_API_KEY`。Key 必须与服务地址配套。配置示例见 [.env.example](../../.env.example)，其他模型见 [规划器配置](../../docs/source-zh/rst_source/guides/configure_planner.rst)。

在同一个 Bash 终端执行：

```bash
source .env.local
source robots/unitree/prepare_mujoco.sh --start-simulator
```

脚本创建或复用虚拟环境，安装基础依赖，检查模型、地图、控制器占用、活动行走、位姿和实时点位；准备成功后激活环境并导出以下变量：

| 变量 | 默认值 |
| --- | --- |
| `RPENT_NAV_ENDPOINT` | `http://192.168.104.191:8765` |
| `RPENT_SIM_ENDPOINT` | `http://192.168.104.191:8770` |
| `RPENT_MODEL` | `openai:deepseek-flash` |

可在配置中覆盖默认值。客户端使用 Python 3.10–3.12，无需本地安装 MuJoCo 或 Torch。

| 选项／变量 | 用途 |
| --- | --- |
| `--skip-install` | 环境已安装时跳过安装，仍检查服务 |
| `--skip-model-check` | 跳过会产生真实模型请求的 API 检查 |
| `--start-simulator` | 仿真器未运行时调用 `/simulator/start`；默认仅检查 |
| `RPENT_VENV_DIR`、`RPENT_PYTHON` | 自定义虚拟环境目录、创建环境所用 Python |

必须 `source` 准备脚本，才能保留环境变量；脚本不发送行走指令。已安装环境时，日常使用 `source robots/unitree/prepare_mujoco.sh --skip-install --start-simulator`。

## 2. 服务检查

准备脚本已执行检查。排查问题时可直接查询：

```bash
curl --noproxy '*' "$RPENT_NAV_ENDPOINT/health"
curl --noproxy '*' "$RPENT_NAV_ENDPOINT/waypoints"
curl --noproxy '*' "$RPENT_SIM_ENDPOINT/health"
curl --noproxy '*' "$RPENT_SIM_ENDPOINT/pose"
```

两个服务应 `ok: true`、`map_id/map_version` 相同；仿真器已运行，无其他控制器或活动行走，位姿有限且 `fallen: false`。点位名称、编号和坐标以实时 `/waypoints` 为准，一个仿真端点只由一个任务控制。

`GET /pose` 报“仿真器没有运行”说明控制 HTTP 服务已响应，需用准备脚本的 `--start-simulator` 启动仿真器。窗口显示在服务端桌面；外部 A* 服务部署见 [导航服务参考](NAVIGATION.zh-CN.md)。

## 3. 运行两点持续往返

以下命令先去广告牌，再去厕所通道，随后持续往返。输出目录每次独立创建：

```bash
mkdir -p logs
export RPENT_RUN_DIR="$(mktemp -d ./logs/mujoco-navigation-XXXXXX)"
rpent --robot unitree --unitree-mode mujoco \
  --nav-endpoint "$RPENT_NAV_ENDPOINT" \
  --sim-endpoint "$RPENT_SIM_ENDPOINT" \
  --planner api --model "$RPENT_MODEL" \
  --task '先去广告牌，确认到达后，再去厕所通道。之后在两个地点之间持续来回走，每次确认到达后再前往另一个地点。正常到达后继续执行，不主动结束任务，由程序在达到max-turns时停止；失败或超时则结束并报告。' \
  --navigation-timeout-s 300 \
  --planner-timeout-s 900 \
  --max-turns 16 \
  --output-dir "$RPENT_RUN_DIR"
```

有限三点任务可将 `--task` 替换为“先去广告牌，确认到达后，再去厕所通道，再去母婴室。失败或超时则结束并报告。”。

| 参数 | 含义 |
| --- | --- |
| `--navigation-timeout-s 300` | 每次行走或独立预演最多 300 秒 |
| `--planner-timeout-s 900` | 整个模型会话预算，包含工具等待 |
| `--max-turns 16` | 最多 16 次模型请求，不是工具调用数或往返次数 |

程序在下一次模型请求前检查上限，日志显示 `Request budget of 16 reached.`。已提交行走通常先结束或触发自身超时，工具内部轮询不消耗模型请求次数。总超时、失败、取消也可能先结束会话；需要更长会话时，同时增加 `--max-turns` 和 `--planner-timeout-s`。

直接 CLI 的持续往返由任务文本引导模型，可能提前结束。启用前端回调后，`repeat_from` 会约束已登记的循环任务不得成功提前结束，详见 [前端说明](FRONTEND_USAGE.zh-CN.md)。MuJoCo 模式不接受网关参数 `--robot-index` 或 `--env-timeout-s`。

## 4. 到达验收与结果

`follow_navigation` 将完整保存路线交给 `/walk`，默认每 5 秒查询一次。每次只执行一个目标，下一次访问必须从实时位姿重新规划；已完成、预演失败、地图变化或起点漂移超过 0.3 米的计划不能执行。

成功需要同时满足：

- `state=finished`、服务结果 `ok: true`、本段 `reached: true`，机器人未摔倒。
- 服务报告的位置误差及由结束位姿计算的误差均不超过 **0.25 米**。
- 指定目标朝向时，报告及按位姿计算的朝向误差均不超过 **0.2 弧度**；未指定朝向则不检查。

所有位置采用地图坐标，`yaw` 单位为弧度。阈值在 [mujoco_toolkit.py](mujoco_toolkit.py) 的 `_validate_arrival` 中设置，无 CLI 参数；它决定 RPent 是否接受结果，实际运动精度由服务端控制器决定。

| 运行目录中的文件 | 查看内容 |
| --- | --- |
| `run.log` | 预算停止、模型与工具错误 |
| `transcript_unitree_*.json` | `finish`、`error`、`stats.turns_used` 和调用过程 |
| `states.json` | 最后一个 `steps[].state` 的完成数、位姿、活动任务和错误 |
| `navigation/plans/`、`navigation/previews/`、`navigation/walks/` | 完整路线、预演报告、每次行走结果 |

有限任务需明确 `finish.status=success` 并核对完成目标；巡逻按预算结束不等于全部任务成功。单次到达查看 `navigation_completed: true`，并确认状态中 `active_walk_id=null`、`submission_uncertain=false`、`stop_error=null`。退出码 `0` 或模型文字不能单独证明成功；`environment_success` 暂不设置。当前模式不生成 recipe，也不代表真机运动验证。

## 5. 取消与排错

CLI 按 `Ctrl+C` 请求取消；已受理的自有行走会请求停止并核对。接受响应丢失时不重发或盲停未知任务。必要时查询 `curl --noproxy '*' "$RPENT_SIM_ENDPOINT/walk"` 核对服务状态。外部 HTTP 服务和仿真器由服务端管理，RPent 退出不会关闭它们。

| 现象 | 处理 |
| --- | --- |
| 模型 `401` | 检查 `.env.local` 中 Key 与 `OPENAI_BASE_URL` 是否配套 |
| 无法连接 8765／8770 | 检查端点、网络和服务进程 |
| 地图版本不同、控制器占用或摔倒 | 先在服务端处理，再执行准备检查 |
| 到达误差超限 | 对比结束位姿和目标站位，并检查服务端位置保持 |
| `plan already completed` | 查询当前位姿并重新规划 |
| TTY 错误 | 在终端运行 CLI，或使用前端 HTTP 启动服务 |
| 超时或停止未确认 | 核对远端运动和日志，再决定下一次任务，不自动重发 |
