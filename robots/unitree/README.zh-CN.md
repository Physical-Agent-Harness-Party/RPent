# 宇树网关与 MuJoCo 导航接入

[English](README.md)

默认 `--unitree-mode gateway` 使用下文的多机器人 `dis_move` 协议。
新增 `--unitree-mode mujoco` 连接外部 A* 规划与仿真控制 HTTP 服务，不连接网关。

## MuJoCo 导航模式

安装 RPent 基础依赖并配置 Planner API 即可。导航与仿真运行在服务端，RPent 客户端无需本地安装 MuJoCo 或 Torch。
首版沿用 Unitree 的普通终端／TTY 要求，不支持 Dashboard、交互模式或探索模式。

A* 服务和仿真控制服务应已运行，且 `map_id`、`map_version` 一致；运动前需在服务端启动仿真器。
Toolkit 不会重启仿真器或停止其他控制程序。一个仿真端点只交给一个 RPent 进程使用：状态响应尚未约定必带任务编号，当前不支持多个调用方共享端点。

```bash
rpent --robot unitree --unitree-mode mujoco \
  --nav-endpoint http://192.168.104.191:8765 \
  --sim-endpoint http://192.168.104.191:8770 \
  --planner api --model openai:deepseek-flash \
  --task '先去广告牌，确认到达后，再去厕所通道。' \
  --navigation-timeout-s 300 --planner-timeout-s 900 --max-turns 16 \
  --output-dir ./logs/unitree-navigation
```

将地址改为当前可达的服务地址。MuJoCo 模式不接受网关参数 `--robot-index` 和 `--env-timeout-s`。
`--navigation-timeout-s` 是每次行走或独立预演的预算，`--planner-timeout-s` 应覆盖整个任务与模型调用。
HTTP 请求绕过环境代理，不自动重发运动。

`UnitreeMujocoToolkit` 提供：

| 工具 | 功能 |
| --- | --- |
| `list_navigation_goals()` | 查询实际点位名称、编号和站位 |
| `get_navigation_pose()` | 查询当前地图位姿，单位米、弧度 |
| `plan_navigation(goals, step=None)` | 从实时位姿规划，返回已保存的 `plan_id` 和摘要 |
| `preview_navigation(plan_id)` | 独立预演并保存报告 |
| `follow_navigation(plan_id)` | 提交单目标路线，在 Python 内轮询到停稳和到达验收 |
| `get_navigation_status()` | 查询服务、位姿和本次导航状态 |
| `stop_navigation()` | 请求停止本 Toolkit 发起的任务并检查是否结束 |

目标支持点位名称／编号，以及新版规划服务支持的 `{x, y, yaw?}` 对象。旧版服务不支持坐标目标时会返回错误。
坐标目标省略朝向时，不要求最终朝向。当前不提供场景图查询或仿真运维工具。
多目标计划可以查看或预演，执行时只接受单目标计划：按“规划一个目标 → 执行 → 验收 → 下一个目标”运行，避免服务跳过失败目标后先访问后面的目标。

Toolkit 将保存的完整路线发送给 `/walk`，不重写路径，也不把地图点直接当成身体坐标 `dis_move`。
任务接受不等于到达；工具等待 `state=finished`，检查每段到达、未摔倒，以及最终位姿和误差（0.25 米，指定终点朝向时 0.2 弧度）。
预演失败、地图变化、起点偏移超过 0.3 米或计划已经执行完成时，会拒绝执行该计划。

接受任务后取消或超时，Toolkit 请求停止并查询结束状态。接受响应丢失时，提交状态不确定，不会重发或盲目停止无法识别的任务，需先核对仿真服务。
停止未确认会保留为错误。普通 `stop_navigation` 工具不能抢占正在执行的排他工具；Planner 取消通过专门的取消钩子先请求远端停止，再等待工具退出。

完整记录保存在 `navigation/plans/`、`navigation/previews/`、`navigation/walks/`。
`states.json` 记录最近位姿、活动任务、错误和 `completed_navigation_goals`；transcript 保存工具调用。
导航到达成功见工具返回的 `navigation_completed`。整体 `environment_success` 暂不设置；仿真到达不代表真机运动验证，本模式没有启用 finalizer 或探索能力。

## 网关模式

这个后端通过本地 HTTP RPC 连接通信中转服务，查询在线机器人，并同步执行 `dis_move` 相对移动指令。
通信程序负责本体主动建立的 WebSocket 连接、注册、心跳和机器人级忙碌判断。
RPent 复用现有 RPC 客户端、工具调度和状态记录，不直接调用本体 SDK。

## 工具和运动语义

| 工具 | 功能 |
| --- | --- |
| `list_robots()` | 查询在线机器人列表 |
| `get_robot_status(robot_index=None)` | 查询指定机器人最近的在线状态 |
| `execute_command(dx_m, dy_m, arrival_turn_deg, robot_index=None)` | 等待相对移动、停止和最终验收 |

省略 `robot_index` 时使用 `--robot-index` 配置的默认目标；没有默认值时必须显式指定。
显式参数可以选择其他机器人，默认目标不构成访问限制。

`dx_m`、`dy_m` 为本次执行起始身体坐标系中的目标偏移，单位米；正 x 向前、正 y 向左。
本体先朝向固定目标点并向前行走，到达并停稳后，以实际到达朝向为基准转动 `arrival_turn_deg` 度，正值左转。
因此，负 x 目标表示转向后方再向前走，不表示直接倒退；该分支的完整真机执行尚未通过。
零位移可原地转向；450° 保留一圈再加 90°，不归一化。三个参数均须为有限数字，合成位移不得超过 5 米。
速度、持续时间、坐标序列和云端停止命令不在本版接口中。5 米是输入上限，不表示已完成满程真机验证。

工具内部固定构造 `{"type":"dis_move","payload":{...}}`；无需在任务中再提供编码。
可以输入“机器人1到起点前方0.4米、左侧0.3米的位置，停稳后左转30度”。任务仍需提供足够的距离和目标信息。

## 运行条件与只读查询

在已激活的环境中安装基础依赖，并配置使用的 Planner API：

```bash
pip install -e .
```

Unitree 保留真机后端的终端要求，请在普通终端运行 CLI。默认使用本地记忆，不下载 HF 语料。
通信方提供本地端口，默认端点为 `http://127.0.0.1:8115`。完整格式见 [中转 RPC 接口](INTERFACE.zh-CN.md)。
连接真实本体时，按 [真实机器人联调测试指南](REAL_ROBOT_TEST.zh-CN.md) 完成只读检查、单次动作、LLM 调用及多机器人路由测试。

```bash
rpent --robot unitree --planner api --model openai:deepseek-flash \
  --env-endpoint http://127.0.0.1:8115 \
  --task '列出在线机器人，并查询机器人1的状态，不发送动作。' \
  --output-dir ./logs/unitree-status
```

启动通过 `env.list_robots` 探测服务，不发送无机器人序号的 `healthz`。
配置 `--robot-index N` 时，还会检查目标在线；未知或离线目标导致启动失败。
未配置默认目标时，列表为空仍可进入 Planner 做只读查询。

## 等待时间与完成结果

默认按每条动作计算 RPC 等待时间：

```text
L = hypot(dx_m, dy_m)
bearing = atan2(dy_m, dx_m)，零位移时取 0
等待秒数 = 60 + L / 0.07 + (abs(bearing) + abs(arrival_turn_deg × π / 180)) / 0.10
```

其中包含本体控制预算和 30 秒通信、停止处理余量。例如 `(0.4, 0.3, 30)` 的默认等待约 78.8 秒。
显式设置 `--env-timeout-s N` 会精确覆盖动作等待时间及服务启动等待预算，过短时仍可能发生超时。
自动模式的启动等待预算为 10 秒；单次启动探测和状态查询为 1 秒，列表查询为 10 秒。
启动后的列表校验及默认目标状态检查不计入服务探测预算。
中转和 WebSocket 层也需允许相应的等待时间。`--planner-timeout-s` 是整个会话的预算，应覆盖所有动作与 LLM 调用。

客户端要求 `received`、`completed`、`stop_acknowledged` 均为布尔值 true，机器人序号和命令回显一致，
六个运动结果数值均有限，报告及实际位移推算的位置误差不超过 0.07 米，追加转角误差绝对值不超过 3°。
停止后的近静止状态由本体侧验收，RPent 无独立的传感器验证。
超时或失败可能发生在部分动作已经执行之后，客户端不自动重发；关闭连接不等于机器人停止。

## 本地模拟测试

随附服务只用于接口测试：模拟在线机器人及理想的完成结果，不连接真实本体、不调用运动 SDK，也不实现 WebSocket 和心跳。

一个终端启动模拟服务：

```bash
python -m robots.unitree.env_server \
  --host 127.0.0.1 --port 8115 --robot-indices 1 2 \
  --commands-file ./logs/unitree-receiver/commands.jsonl
```

另一个终端运行：

```bash
rpent --robot unitree --planner api --model openai:deepseek-flash \
  --env-endpoint http://127.0.0.1:8115 \
  --planner-timeout-s 300 \
  --task '本地模拟：机器人1到起点前方0.4米、左侧0.3米的位置，停稳后左转30度；完成后机器人2原地左转450度。' \
  --output-dir ./logs/unitree-multi
```

单目标可增加 `--robot-index 1`，并在任务中指定默认机器人。
模拟端也支持 `--transport socket`，客户端使用 `socket://127.0.0.1:8115`；生产对接按 HTTP 协议执行。

模拟回执包含 `mode: "mock"`、`completed: true` 和 `motion_result`；结果为合成数据，不证明真实运动。
同一机器人忙碌时拒绝新请求，不覆盖或排队；不同机器人可由不同客户端并发请求。
同一个 Toolkit 内的动作工具仍串行执行，包括针对不同机器人的动作。

输出目录包含 `states.json` 和 `transcript_unitree_<run-id>.json`。
状态保留 `acknowledged_commands`，并增加 `completed_commands`、`completed_by_robot`；完整运动结果在动作记录中。
`validation_scope` 为 `reported_motion_completion`。单步完成不自动把整个任务或物理场景标记为成功。

## 本地检查

```bash
pip install -e ".[test]"
python -m pytest tests/unit_tests/robots/unitree -v
```

测试覆盖参数边界、完成和停止回执、误差验收、自动等待时间、失败清理、状态记录及脚本化 API Planner 的公共 CLI。
真实 localhost HTTP/socket 测试还检查机器人级忙碌拒绝、不同机器人并发以及超时后远端仍可能完成。
测试不消耗真实模型 API，不需要机器人或 GPU。
