# RPent 与服务端中转 RPC 接口

RPent 向同主机的服务端中转发送 HTTP RPC。服务端中转维护机器人在线状态，通过本体主动建立的 WebSocket 连接转发命令。
本体注册和业务心跳由通信程序处理，其接入地址、周期、阈值及 WebSocket 消息封装仍需通信双方确定。

## 1. 请求入口

- 地址：`http://127.0.0.1:<服务端中转端口>/call`
- 方法：`POST`
- Content-Type：`application/json`，编码 UTF-8。
- 所有请求使用 `method`、`args`、`kwargs`、`session_id`；`session_id` 固定为 `null`，无需 RPent 会话注册。
- 已进入 RPC 处理流程的成功和失败均返回 HTTP 200，业务结果由响应中的 `ok` 判断。

## 2. 在线机器人列表：`env.list_robots`

请求：

```json
{
  "method": "env.list_robots",
  "args": [],
  "kwargs": {},
  "session_id": null
}
```

响应：

```json
{
  "ok": true,
  "result": {
    "robots": [
      {"robot_index": 1, "status": "online"},
      {"robot_index": 3, "status": "online"}
    ],
    "total": 2
  }
}
```

`robot_index` 为正整数。列表仅包含在线机器人，按序号严格升序排列；`total` 为列表长度。
无在线机器人时返回 `robots: []`、`total: 0`，查询仍成功。
序号按注册顺序分配，单次服务运行期间离线序号保留；重连、重复注册和服务重启后的映射规则待通信方确认。

RPent 启动时使用本接口探测中转服务，不调用无参数的 `healthz`；单次启动探测超时为 1 秒。

## 3. 指定机器人状态：`healthz`

请求：

```json
{
  "method": "healthz",
  "args": [],
  "kwargs": {"robot_index": 1},
  "session_id": null
}
```

响应：

```json
{
  "ok": true,
  "result": {"robot_index": 1, "status": "online"}
}
```

`status` 为 `online` 或 `offline`。在线要求已注册、WebSocket 通道可用且业务心跳有效。
已注册但离线时返回 `ok: true`、`status: "offline"`；不存在的序号返回错误。
本接口应快速读取中转服务维护的状态，不执行运动，RPent 单次查询超时为 1 秒。
在线状态不代表本体允许运动；动作执行前由中转和本体检查。

## 4. 同步移动：`env.execute_command`

请求：

```json
{
  "method": "env.execute_command",
  "args": [{
    "type": "dis_move",
    "payload": {"dx_m": 0.4, "dy_m": 0.3, "arrival_turn_deg": 30.0}
  }],
  "kwargs": {"robot_index": 1},
  "session_id": null
}
```

本版固定使用 `dis_move`，仅接受三个有限数字参数，不接受布尔值、速度、持续时间或坐标序列。
`dx_m`、`dy_m` 为执行开始时身体坐标系中的目标偏移，单位米，正 x 向前、正 y 向左。
合成位移 `hypot(dx_m, dy_m)` 不超过 5 米，目标在本次执行期间固定。
本体先朝向目标并向前行走，到达停稳后，以实际到达朝向为基准追加 `arrival_turn_deg` 度，正值左转。
零位移表示原地转向；450° 必须执行完整圈数，不归一化。负 x 表示转向后方再前进，不表示直接倒退。
行走阶段横移速度为零，可根据位置反馈修正朝向，实际轨迹不保证严格为直线。默认容差内的小位移或小转角可能跳过相应动作。

成功响应只在动作、停止处理和最终验收完成后返回：

```json
{
  "ok": true,
  "result": {
    "robot_index": 1,
    "command": {
      "type": "dis_move",
      "payload": {"dx_m": 0.4, "dy_m": 0.3, "arrival_turn_deg": 30.0}
    },
    "received": true,
    "completed": true,
    "motion_result": {
      "actual_dx_m": 0.338774,
      "actual_dy_m": 0.278366,
      "actual_arrival_turn_deg": 31.1027,
      "total_angle_deg": 72.6875,
      "position_error_m": 0.0649355,
      "angle_error_deg": -1.10272,
      "stop_acknowledged": true
    }
  }
}
```

机器人序号和命令回显必须匹配请求；数字回显的 `1` 和 `1.0` 视为等值，但不接受布尔值代替数字。
`received`、`completed`、`stop_acknowledged` 必须为布尔值 true，不能用字符串或数字代替。
六个结果数值均须有限。`actual_dx_m`、`actual_dy_m` 是起始身体坐标系中的最终位移分量；
`actual_arrival_turn_deg` 是实际追加转角，`total_angle_deg` 包括预转向、行走修正及追加转角。
默认最终位置误差不超过 0.07 米，追加转角误差绝对值不超过 3°，并获得停止确认和近静止最终状态。
角度误差为请求追加转角减实际追加转角，不与整段累计转角比较。
RPent 检查报告误差及依据实际结果推算的误差；近静止状态由本体侧验收。

## 5. 错误、超时和并发

非法参数、未知序号、本体离线、未就绪、忙碌、控制失败、控制超时或验收不通过时返回实际原因：

```json
{"ok": false, "error": "robot is busy"}
```

同一本体同一时刻最多执行一个运动任务；忙碌时拒绝新请求，不排队、不覆盖。不同本体可独立执行。
RPent 同一个 Toolkit 的动作工具仍串行调度；中转服务需要机器人级并发控制。

按每条动作估算等待时间：

```text
L = hypot(dx_m, dy_m)
bearing = atan2(dy_m, dx_m)，零位移时取 0
控制预算 = 30 + L / 0.07 + (abs(bearing) + abs(arrival_turn_deg × π / 180)) / 0.10
RPC 等待建议下限 = 控制预算 + 30
```

`bearing` 单位为弧度。RPent 默认使用上述 RPC 等待时间；显式 `--env-timeout-s` 精确覆盖该值及启动等待预算。
显式值过短仍会超时。中转服务与 WebSocket 的等待超时必须配套设置。
通信超时不自动取消或停止动作，失败时可能已部分执行。当前没有命令去重标识，RPent 不自动重发。
本体负责执行边界、无进展和控制超时检查，以及正常或异常结束时的停止处理。
停止请求、客户端退出或连接断开不保证本体实际已经停止。本版尚未约定云端主动停止指令。

## 6. RPent 配置与验证范围

`--env-endpoint` 指向中转服务；`--robot-index N` 可设置默认目标并在启动时确认它在线。
工具调用显式传入 `robot_index` 可选择其他机器人。未配置默认目标时必须显式选择，列表为空不阻止只读查询任务。
该默认序号不是持久设备身份，重连或中转重启后的目标映射必须重新确认。

自动模式的启动等待预算为 10 秒；单次启动探测及状态查询为 1 秒，列表查询为 10 秒。
`--planner-timeout-s` 是会话总预算，应覆盖所有动作和 LLM 调用。启动后校验不计入服务探测预算。

RPent 根据本体报告判断单次动作完成，不提供外部定位验证，也不把单步完成自动视为整项任务成功。
随附服务仅模拟完成结果，`mode: "mock"` 不表示真实运动。
本体侧文档报告斜向目标已真机验证，5 米满程尚未实测，后方目标完整执行尚未通过；RPC／WebSocket 到控制程序仍需联调。
