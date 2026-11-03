# 宇树命令 RPC 接入验证

[English](README.md)

这个后端把自然语言任务接到两个 Planner 工具：
`move_forward()` 发送 `前进`，`move_backward()` 发送 `后退`。
随附的本地假 Server 记录字符串并确认收到命令。
当前验证范围是命令送达；机器人运动、ROS2 控制、Docker 初始化、VLA 推理和相机观测尚未实现。

## 运行条件

在已激活的环境中安装 RPent 基础依赖：

```bash
pip install -e .
```

规划器示例沿用你已有的 API 配置。离线测试使用确定性的测试模型，不会请求外部模型服务。
Unitree 后端标记为真机接入，保留 RPent 的终端要求，请在普通终端中运行机器人 CLI。

## 启动本地接收端

在仓库根目录的一个终端中执行：

```bash
python -m robots.unitree.env_server \
  --host 127.0.0.1 --port 8115 \
  --commands-file ./logs/unitree-receiver/commands.jsonl
```

服务会打印监听地址。每条成功接收的命令都会向文件追加一条 JSON 回执，
支持的字符串只有 `前进` 和 `后退`。

## 运行 Planner

在另一个终端中激活 RPent 环境后执行：

```bash
rpent --planner api --model openai:deepseek-flash \
  --robot unitree --task "让机器狗往前走" \
  --env-endpoint http://127.0.0.1:8115 \
  --output-dir ./logs/unitree
```

默认接收端地址为 `http://127.0.0.1:8115`，连接上述本地服务时可以省略该参数。
`--env-timeout-s` 指定服务就绪和命令响应的超时，默认 10 秒。
Unitree 默认使用本地记忆，不会下载 HF 语料。当前后端支持普通终端运行。

通过接收端的日志文件确认命令送达：

```json
{"command": "前进", "received": true, "sequence": 1, "mode": "mock"}
```

Planner 输出目录还会生成 `states.json` 和
`transcript_unitree_<run-id>.json`，其中的工具结果包含回执。
因为没有测量实际运动，环境终止标志保持 false。

## RPC 接口

业务方法为 `env.execute_command(command)`，参数是上述两个中文字符串之一。
成功响应必须包含一致的 `command` 和 `received: true`。
假 Server 还返回 `mode` 和 `sequence`。

适配层直接复用 RPent 的 `RpcClient` 和 `RpcFacade`。
命令接口不包含 Gym 观测元组，也不会在连接时复位环境。
后续明确运动和停止行为后，硬件侧 Server 可以实现相同方法；随附的 Server 始终只模拟接收。

默认使用 HTTP，也支持 socket RPC：

```bash
python -m robots.unitree.env_server --transport socket --port 8115
rpent --planner api --model openai:deepseek-flash \
  --robot unitree --task "让机器狗往后退" \
  --env-endpoint socket://127.0.0.1:8115 \
  --output-dir ./logs/unitree-backward
```

## 本地检查

安装项目已有的测试依赖后运行：

```bash
pip install -e ".[test]"
python -m pytest tests/unit_tests/robots/unitree -v
```

`test_rpc_loopback.py` 使用真实的 localhost HTTP/socket 服务，需要环境允许创建和绑定网络 Socket。
其余测试覆盖工具声明、JSON 请求与响应处理、服务端分发、回执，以及使用原生 API 规划器和脚本化测试模型的公共 CLI。
这些测试替换了网络 I/O，不代表已验证真实 Socket 连接。
