# 在现有 Go2 仿真中运行 LightNav VLN

LightNav 根据第一视角图像和文字指令预测局部航点。RPent 工具内部持续取图、
推理并转发航点，由现有 Go2 仿真服务完成坐标转换、MPC 和步态控制。
A* 继续负责地图路径规划；同一台 Go2 每次只执行一种导航方式。

## 服务与环境

本机源码、权重、地图和 Python 环境均位于 `/home/yanlinjie/robot_project`。
四个组件共用 `.venv`；MuJoCo 保留 3.3.6，没有运行 LightNav 的 TurtleBot 演示。

| 服务 | 地址 | 职责 |
| --- | --- | --- |
| LightNav | `ws://127.0.0.1:8050` | `login/reset/next`，返回局部航点和模型 STOP |
| A* | `http://127.0.0.1:8765` | 点位查询、地图路径规划 |
| Go2 控制 | `http://127.0.0.1:8770` | `/walk` 或持续航点会话 |
| Go2 观测 | `http://127.0.0.1:8771` | `/observation`、`/camera/front` |
| RPent HTTP | `http://127.0.0.1:8780` | 提交任务、查询进度、日志和取消 |

现有推理依赖、Go2 仿真和 A* 应已准备好。已有共享环境时，仅需注册本地包：

```bash
cd /home/yanlinjie/robot_project
uv pip install --offline --no-deps --no-build-isolation --python .venv/bin/python \
  --editable ./lightnav --editable ./rpent
uv pip check --python .venv/bin/python
```

RPent 客户端只需要 HTTP 和 WebSocket；`unitree-vln` extra 声明其可选 WebSocket
依赖。模型加载和 GPU 运算只发生在独立的 LightNav 服务进程。

## 启动与停止模型

在项目根目录执行：

```bash
.venv/bin/python lightnav/scripts/run_vln_service.py start
.venv/bin/python lightnav/scripts/run_vln_service.py status
```

默认使用物理 GPU 1、本地 `lightnav/checkpoints/LightNav-0`，监听回环地址 8050。
首次加载和预热可能需要数分钟；`status` 的 `ready:true` 表示预热后 WebSocket
探测成功。加载中返回退出码 1。日志为 `lightnav/output/vln-local/service.log`。
启动器强制使用离线权重，沿用上游 `local_serve.py` 的单进程 HashStore 初始化。

可在启动前设置 `VLN_CUDA_VISIBLE_DEVICES`、`VLN_MODEL_PATH`、`VLN_PORT` 和
`VLN_GPU_MEMORY_UTILIZATION`；默认显存比例为 0.4。选择其他端口时，状态检查及
RPent 的 `--vln-endpoint` 也要使用相同配置。停止命令只结束本启动器记录的模型进程：

```bash
.venv/bin/python lightnav/scripts/run_vln_service.py stop
```

仿真、A* 和 RPent 使用各自原有启停入口。模型服务关闭时，活动 VLN 工具会尝试
确认停车；不能确认时保留待核对状态，禁止自动重放运动。

## 接入 RPent 和前端

原有父任务、子任务和 Bearer 认证协议保持不变，见
[前端接口说明](FRONTEND_USAGE.zh-CN.md)。本机提供一个配置片段，复用原有
`.env.frontend-local` 和私有 `.env.local`，再增加 VLN 与观测地址：

```bash
cd /home/yanlinjie/robot_project/rpent
set -a
source robots/unitree/vln_local.env
set +a
../.venv/bin/python -m robots.unitree.task_server \
  --host 127.0.0.1 --port 8780 --jobs-dir output/frontend-integration/jobs
```

已有任务服务时应先正常结束旧进程，避免争抢端口和 jobs 目录锁。服务只监听
本机；远端访问沿用已有的私有连接方式。
前台启动的任务服务使用 Ctrl+C 关闭，会先清理自有控制。当前后台实例的 PID
保存在 `output/frontend-integration/task-server.pid`，日志在项目根目录
`output/vln-integration/task-server.log`；关闭服务前应让正在执行的任务正常停止。

前端仍向 `POST /api/rpend/run` 提交 `task_id`、`task_name`，并轮询
`GET /api/rpend/run/{task_id}`。例如“使用 VLN 保持当前地图位置并确认停车”会让
RPent 读取当前位置，将它作为已知验收坐标，再调用 VLN。

默认调度先解析目标区域、物体和验收条件：已知地图站位使用 A*；
“前往某个房间寻找物体”拆成 A* 到房间、确认停车、VLN 局部搜索两个阶段。
例如“前往客厅找垃圾桶”先解析实时表中的完整名称“客厅”，再执行视觉搜索。
完整名称的唯一匹配优先于其他名称的部分匹配；客厅东区和客厅西区不会使
一个已有唯一完整名称的“客厅”变成歧义。确实无法解析区域时报告缺少信息。

明确要求“仅使用 VLN”时保留完整视觉路线；明确要求“仅使用 A*”时不会增加
视觉搜索。两个阶段使用不同的前端子任务编号，在运动前一次登记。
传给 LightNav 的是当前 VLN 阶段的指令，省略已经完成的区域导航。
局部搜索通常先给 30 秒，且不超过配置的单次 VLN 上限；一次失败后不重复
提交同一阶段，也不切换算法绕过失败状态。

这些规则由 `prompt_bundle.py` 的 MuJoCo system prompt 提供。
每个 HTTP 新任务会启动新的 RPent 子进程并读取提示词，修改后对新任务生效；
正在运行的任务仍使用原提示词，8780 任务服务无需为此重启。
本地配置同时设置单段 A* 预算 300 秒、整任务规划预算 900 秒、最多 16 次模型请求；
这些值由每个新任务的启动脚本重新读取。VLN 单次最大预算仍为 120 秒。
房间边界监控和视觉目标的独立验收仍需要工具层支持；提示词中的局部搜索要求
不能保证机器人始终留在指定房间，地图站位验收也不能代替视觉识别证据。

不使用 HTTP 启动服务时，CLI 在原 MuJoCo 参数上增加：

```text
--vln-endpoint ws://127.0.0.1:8050
--observation-endpoint http://127.0.0.1:8771
--vln-timeout-s 120
--vln-inference-timeout-s 3
```

两个 endpoint 必须成对提供。省略时保留原 A* 工具集。

## 工具和闭环

| 工具 | 行为 |
| --- | --- |
| `run_vln_navigation(instruction, expected_goal=None, timeout_s=None, sub_task_id=None)` | 一次调用内部循环，直到目标验收、模型 STOP、取消或失败 |
| `get_vln_status()` | 最近一次会话、预测数量、控制权和停车证据 |
| `stop_vln_navigation()` | 结束本工具拥有的控制会话并确认停车 |

`expected_goal` 是已知地图坐标 `{x,y,yaw?}`，不是给模型的局部航点。只能使用
任务或实时点位表提供的坐标；位置验收阈值为 0.25 米，可选朝向阈值为 0.2 弧度。
工具会在经过至少一轮真实预测后检查已知目标，并在停车后重新验收。

模型每次返回的全部 `[前向米, 左向米, 逆时针弧度]` 航点，与该次输入图像的
`stamp_ns`、`base_pose` 一起交给 `/control/trajectory`。RPent 不生成速度命令，
也不拿推理结束后的位姿替换采集位姿。默认按训练配置的 4 Hz 循环，每个连接
只保留一个在途推理请求；新任务使用新的连接并执行 reset。

轨迹有效期仍是采集后 2 秒。超时预测被丢弃，不修改时间戳续期；连续三次过期、
断连、MPC 错误、跌倒或任务超时均结束本次执行并尝试停车。旧会话不能停止新会话，
无法确认的创建或停车结果需要核对，不能重复提交运动。

模型 STOP、物理停车和目标验证分别记录：

- `model_stop`：模型是否输出 STOP。
- `stop_confirmed`：仿真是否确认站稳并释放控制权。
- `goal_verified`：是否通过独立地图位姿验收。

没有已知验收坐标时，模型 STOP 后会停车并返回 `verification_required`，
不会把它计入已验证目标或允许 RPent 报成功。上层任务应说明目标仍待验证。

## 前端状态字段

活动工具会持续原子写入 `navigation/vln/latest.json`，HTTP 查询将它合并到
`navigation.vln`，无需等待长工具调用结束：

```json
{
  "navigation": {
    "active_control_id": "control_...",
    "vln": {
      "episode_id": "vln_...",
      "state": "tracking",
      "predictions": 7,
      "accepted_references": 7,
      "latency_ms": 250.0,
      "capture_age_s": 0.31,
      "model_stop": false,
      "stop_confirmed": false,
      "goal_verified": false
    }
  }
}
```

`state` 可为 `connecting`、`starting_control`、`observing`、`inferencing`、
`tracking`、`waiting_fresh_prediction`、`stopping`、`finished`、
`verification_required`、`cancelled` 或 `failed`。`finished` 表示已知目标验收
及停车均通过。`control` 内有仿真步态、MPC 和实测运动反馈；`latest_frame` 是
本地诊断文件路径，浏览器实时画面仍使用 8771 的第一视角接口。

已有子任务回调增加 `vln_started`、`vln_finished`、`vln_error` 三个 stage，
仍使用 `{data: {...}, source_type: "sil"}`。取消事件的 stage 为 `vln_error`，
其 `result.state=cancelled`；前端应按任务顶层 `stopped` 显示为已取消。
逐帧进度通过任务查询读取，回调只报告生命周期，避免阻塞轨迹更新。

任务顶层新增 `verification_required`：已停车但目标未验证，可以与真正成功区分。
控制权或停车状态不明仍为 `requires_attention`，并阻止新任务。

## 验证与限制

离线测试使用项目现有 pytest：

```bash
cd /home/yanlinjie/robot_project/rpent
../.venv/bin/python -m pytest tests/unit_tests/robots/unitree -q
```

实际联调需要已经启动的 A*、Go2 和 LightNav，且仿真与现有 RPent 任务均空闲。
下面的检查会实际控制仿真 Go2，并通过本地接收端验证前端回调：

```bash
RPENT_RUN_UNITREE_VLN=1 ../.venv/bin/python -m pytest \
  tests/e2e_tests/unitree/test_vln_policy_chain.py -q -s
```

它复用仓库的本地脚本规划器，只固定高层工具调用；LightNav 推理、Go2 图像、
航点转换、MPC 和 DDS 均实际运行。覆盖保持当前已知地图位置并停车，以及运动中
通过 HTTP 取消后释放控制权。产物位于项目 `output/vln-integration/`。
这证明服务与控制链路接通，不代表视觉语义导航基准成功；外部浏览器页面需按上述
状态字段接入，测试未覆盖操作者电脑上的页面显示。
另已使用现有配置的 RPent 模型完成“读取当前位姿 → 调用 VLN 保持该位姿 →
停车并验收 → finish”的真实调用链。验证汇总位于项目
`output/vln-integration/verification.json`。
