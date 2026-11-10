# Unitree 真机测试方法

按“只读检查 → 单次动作 → LLM 调用”测试。接口字段见 [中转 RPC 接口](INTERFACE.zh-CN.md)。

## 1. 测试前准备

- 通信方启动真实中转，并接通本体控制程序。仓库中的 `robots.unitree.env_server` 只用于模拟测试。
- 确认中转端口、`robot_index` 对应的实体机器人，以及本体已进入可运动状态。`online` 只表示通信在线。
- 现场留出运动空间，准备本体侧的本地停止方式，并持续观察机器人。

在同一个终端执行后续步骤，端口和序号按实际值修改。端点不加 `/call`：

```bash
conda activate rpent-unitree
cd /home/linjie/work/RPent
export UNITREE_RPC_ENDPOINT='http://127.0.0.1:8115'
export UNITREE_ROBOT_INDEX='1'
export UNITREE_TEST_DIR="./logs/unitree-real-$(date +%Y%m%d-%H%M%S)"
```

## 2. 只读检查

不需要模型 API，不发送动作：

```bash
python - <<'PY'
import os
from robots.unitree.env_client import UnitreeEnvClient
from rpent.utils.rpc import make_rpc_client

env = UnitreeEnvClient(
    make_rpc_client(os.environ["UNITREE_RPC_ENDPOINT"]),
    robot_index=int(os.environ["UNITREE_ROBOT_INDEX"]),
)
try:
    print("在线机器人：", env.list_robots())
    print("目标状态：", env.get_robot_status())
finally:
    env.close()
PY
```

目标应在列表中且状态为 `online`。空列表、离线或报错时，先由通信方检查注册、WebSocket、心跳和序号映射。

## 3. 单次动作测试

现场确认后执行：**向本次起点前方移动 0.2 米，无横向偏移，不追加转向**。重复运行会再移动一次。

```bash
python - <<'PY'
import json
import os
from robots.unitree.env_client import UnitreeEnvClient
from rpent.utils.rpc import make_rpc_client

env = UnitreeEnvClient(
    make_rpc_client(os.environ["UNITREE_RPC_ENDPOINT"]),
    robot_index=int(os.environ["UNITREE_ROBOT_INDEX"]),
)
try:
    if env.get_robot_status()["status"] != "online":
        raise RuntimeError("目标离线，未发送动作")
    result = env.execute_command({
        "type": "dis_move",
        "payload": {"dx_m": 0.2, "dy_m": 0.0, "arrival_turn_deg": 0.0},
    })
    print(json.dumps(result, ensure_ascii=False, indent=2))
finally:
    env.close()
PY
```

调用会等待运动、停止和验收后返回一次结果，不通过轮询 `healthz` 判断完成。默认等待约 62.9 秒；中转与 WebSocket 的等待时间也需配套。

## 4. LLM 调用测试

直接动作通过后，重新确认位置和空间，再运行以下命令。这会新增一次前进动作：

```bash
source ~/.config/rpent/deepseek.env
rpent --robot unitree --planner api --model openai:deepseek-flash \
  --reasoning-effort none --memory-profile local \
  --env-endpoint "$UNITREE_RPC_ENDPOINT" --robot-index "$UNITREE_ROBOT_INDEX" \
  --planner-timeout-s 180 --max-turns 8 \
  --task '先查询默认机器人状态。在线时只执行一次：前进0.2米，无横向偏移，不追加转向。确认完成后结束；失败或超时则结束，不重发。' \
  --output-dir "$UNITREE_TEST_DIR"
```

在普通终端运行，不加 `--dashboard`、`--interactive` 或 `--explore`。任务中的“只执行一次”是模型指令，当前没有强制单次动作限制或逐动作人工批准开关。

## 5. 验收与异常处理

- 指定真机完成运动并停稳，其他机器人未被误触发。
- 回执序号和命令匹配，`received`、`completed`、`motion_result.stop_acknowledged` 均为布尔值 `true`。
- 位置误差不超过 0.07 米，追加转角误差绝对值不超过 3°。客户端会检查回执；本体估计值仍需结合现场观察，`mode: "mock"` 表示模拟数据。
- LLM 测试检查输出目录中的 `run.log`、`transcript_unitree_*.json` 和 `states.json`：动作参数正确、请求仅一次，最后状态的 `completed_commands` 为 `1`，目标在 `completed_by_robot` 中计数为 `1`。不能只看模型说“成功”。
- 保存终端输出、中转和本体日志。忙碌、超时或断连时先核对本体状态，必要时本地停止；**不要直接重发**。Ctrl+C、关闭客户端或超时都不保证机器人停止，当前没有云端主动停止接口。

多机器人先逐台测试：核对实体映射，修改 `UNITREE_ROBOT_INDEX`，使用新的输出目录重复上述步骤。各机器人共用中转端点，由中转负责路由；`--robot-index` 只是默认目标，不限制工具选择其他机器人。单个 Toolkit 的动作串行执行。

首次测试使用短距离前进。后方目标完整真机执行尚未通过，5 米只是输入上限；多圈转向留待后续专项测试。
