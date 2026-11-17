# Unitree 接入说明

RPent 提供两种 Unitree 模式，默认使用 `gateway`。

| 模式 | 连接对象 | 执行方式 |
| --- | --- | --- |
| `mujoco` | 外部 A*、MuJoCo，及可选 LightNav 服务 | 地图路线执行或图像驱动的航点闭环 |
| `gateway` | 通信中转 RPC 服务 | `dis_move` 身体坐标相对移动，由本体执行 |

安装 RPent 基础依赖并配置模型即可；MuJoCo 模式的客户端无需安装 MuJoCo 或 Torch。Unitree CLI 要求 TTY，不接入内置 Dashboard，也不支持交互模式和探索模式。HTTP 启动服务自动为子进程分配 TTY。记忆默认保存在本地。

## 使用文档

| 文档 | 内容 |
| --- | --- |
| [MuJoCo 使用指南](MUJOCO_USAGE.zh-CN.md) | 环境准备、持续往返、预算、到达验收和结果检查 |
| [前端与 HTTP 服务](FRONTEND_USAGE.zh-CN.md) | 配置、启动服务、提交任务、查询、停止和结果回调 |
| [LightNav VLN 接入](VLN_USAGE.zh-CN.md) | 模型服务、第一视角、航点闭环、停车与前端状态 |
| [中转 RPC 接口](INTERFACE.zh-CN.md) | 真机网关的请求、回执、误差和超时约定 |
| [真机测试](REAL_ROBOT_TEST.zh-CN.md) | 只读检查、单次动作和模型调用测试 |
| [外部导航服务参考](NAVIGATION.zh-CN.md) | A* 服务部署、坐标系及 HTTP／WebSocket 协议 |

## MuJoCo 工具

一个仿真端点只交给一个任务使用。RPent 按“实时位姿规划 → 执行一个目标 → 确认到达 → 下一个目标”串行运行，完整路线原样交给 `/walk`，不转换成 `dis_move`。

| 工具 | 用途 |
| --- | --- |
| `list_navigation_goals()` | 查询实时点位名称、编号和站位 |
| `get_navigation_pose()` | 查询地图位姿，单位米、弧度 |
| `plan_navigation(goals, step=None, sub_task_id=None)` | 从实时位姿规划并保存 `plan_id` |
| `preview_navigation(plan_id)` | 预演路线并保存报告 |
| `follow_navigation(plan_id)` | 执行单目标路线，内部轮询并验收到达 |
| `get_navigation_status()` | 查询服务、位姿及本次任务状态 |
| `stop_navigation()` | 请求停止并核对本 Toolkit 的行走任务 |
| `run_vln_navigation(instruction, expected_goal=None, timeout_s=None, sub_task_id=None)` | 启用 VLN 时执行取图、推理、航点转发与停车闭环 |
| `get_vln_status()`／`stop_vln_navigation()` | 查询 VLN 状态／停止自有控制会话 |
| `register_navigation_subtasks(commands, repeat_from=None)` | 启用前端回调时登记有序子任务及循环起点 |
| `finish(status, summary)` | 结束有限任务；成功前检查已登记目标和导航状态 |

目标支持名称、编号，以及规划服务支持的 `{x, y, yaw?}` 坐标。多目标计划可以查看或预演，执行只接受单目标计划。每次访问都要重新规划，已完成的 `plan_id` 不能复用。

VLN 通过 `--vln-endpoint` 与 `--observation-endpoint` 成对启用，省略时保留原 A* 工具集。
VLN 与 A* 串行使用同一 Go2。模型 STOP、物理停车和独立目标验收分别记录；未提供已知
验收坐标的模型 STOP 返回 `verification_required`，不能报告已验证到达。

## 网关模式

通信中转管理机器人注册、WebSocket、心跳和机器人级忙碌状态；RPent 通过本地 RPC 调用，不直接访问本体 SDK。

| 工具 | 用途 |
| --- | --- |
| `list_robots()` | 查询在线机器人 |
| `get_robot_status(robot_index=None)` | 查询指定机器人的通信状态 |
| `execute_command(dx_m, dy_m, arrival_turn_deg, robot_index=None)` | 等待相对移动、停止及验收 |

省略工具参数 `robot_index` 时使用 `--robot-index` 的默认目标；未配置默认值时必须显式指定。显式参数可选择其他机器人，默认目标不构成访问限制。配置默认目标时启动会检查其在线；未配置时，空列表仍允许只读查询。`online` 不代表本体允许运动。

`dx_m`、`dy_m` 是执行起点的身体坐标偏移，正 x 向前、正 y 向左，合成位移不超过 5 米。本体先朝向目标再前进，停稳后追加 `arrival_turn_deg` 转角，正值左转；负 x 不表示直接倒退。零位移可原地转向，450° 保留完整圈数。参数必须是有限数字，不接受速度、持续时间或坐标序列。

默认 RPC 等待时间按每条动作估算；`--env-timeout-s` 可覆盖该值和启动等待预算，`--planner-timeout-s` 是整个会话预算。公式、查询预算和验收字段见 [RPC 接口](INTERFACE.zh-CN.md)。客户端核对序号、命令回显、完成与停止回执，以及报告和实际结果推算的误差：位置 0.07 米、追加转角 3°。超时可能发生在动作部分执行之后，不自动重发；关闭连接不保证本体停止。

### 本地模拟

模拟服务生成理想回执，不连接本体 SDK、不实现机器人 WebSocket 或心跳。在两个终端分别执行：

```bash
python -m robots.unitree.env_server \
  --host 127.0.0.1 --port 8115 --robot-indices 1 2 \
  --commands-file ./logs/unitree-receiver/commands.jsonl
```

```bash
rpent --robot unitree --planner api --model openai:deepseek-flash \
  --env-endpoint http://127.0.0.1:8115 \
  --planner-timeout-s 300 \
  --task '本地模拟：机器人1到起点前方0.4米、左侧0.3米的位置，停稳后左转30度；完成后机器人2原地左转450度。' \
  --output-dir ./logs/unitree-multi
```

模拟端也支持 `--transport socket`，客户端对应 `socket://127.0.0.1:8115`；生产对接使用 HTTP。同一机器人忙碌时拒绝新任务，不排队；不同机器人可由不同客户端并发调用，单个 Toolkit 内的动作仍串行。

输出的 `states.json` 包含 `acknowledged_commands`、`completed_commands`、`completed_by_robot`；transcript 保存调用过程，`validation_scope=reported_motion_completion`。模拟回执中的 `mode: "mock"` 和单步完成均不证明真机或整个任务成功。真机测试与已知限制见 [真机测试](REAL_ROBOT_TEST.zh-CN.md)。

## 文件组织与测试

| 文件 | 职责 |
| --- | --- |
| [robot_spec.py](robot_spec.py)、[prompt_bundle.py](prompt_bundle.py) | 模式注册、参数、客户端及模型提示词 |
| [mujoco_toolkit.py](mujoco_toolkit.py) | 规划、预演、执行、验收、取消和状态记录 |
| [navigation_client.py](navigation_client.py)、[simulation_client.py](simulation_client.py)、[http_client.py](http_client.py) | A*／仿真 HTTP 协议与响应校验 |
| [vln_client.py](vln_client.py)、[observation_client.py](observation_client.py)、[vln_navigation.py](vln_navigation.py) | LightNav 协议、采集观测和有期限的控制闭环 |
| [frontend_client.py](frontend_client.py)、[check_frontend.py](check_frontend.py) | 前端回调、编号映射及独立接口检查 |
| [task_server.py](task_server.py) | HTTP 接收、子进程、TTY、幂等记录和停止 |
| [prepare_mujoco.sh](prepare_mujoco.sh)、[run_rpent.sh](../../run_rpent.sh) | 准备检查与两参数启动脚本 |
| [env_client.py](env_client.py)、[toolkit.py](toolkit.py)、[protocol.py](protocol.py)、[env_server.py](env_server.py) | 网关客户端、工具、校验和模拟服务 |

在仓库根目录运行离线测试，无需模型 API、机器人或 GPU：

```bash
pip install -e '.[test]'
python -m pytest tests/unit_tests/robots/unitree -v
```
