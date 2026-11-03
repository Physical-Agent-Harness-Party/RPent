# Unitree command RPC prototype

[简体中文](README.zh-CN.md)

This backend connects natural-language tasks to two planner tools:
`move_forward()` sends `前进`, and `move_backward()` sends `后退`.
The included mock server records the command strings and acknowledges receipt.
This prototype verifies command delivery; it does not implement robot motion,
ROS2 control, Docker setup, VLA inference, or camera observations.

## Requirements

Install RPent's base dependencies in the active environment:

```bash
pip install -e .
```

The planner example uses your existing API configuration. The offline tests use
a deterministic test model and do not contact an external model service.
Run the robot CLI in a normal terminal: the Unitree backend is marked as a
real-robot integration and keeps RPent's terminal requirement.

## Start the local receiver

From the repository root, run this in one terminal:

```bash
python -m robots.unitree.env_server \
  --host 127.0.0.1 --port 8115 \
  --commands-file ./logs/unitree-receiver/commands.jsonl
```

The server prints its listening address. Each accepted command appends a JSON
receipt to the commands file. It accepts only `前进` and `后退`.

## Run the planner

In another terminal, with RPent's environment activated:

```bash
rpent --planner api --model openai:deepseek-flash \
  --robot unitree --task "让机器狗往前走" \
  --env-endpoint http://127.0.0.1:8115 \
  --output-dir ./logs/unitree
```

The endpoint defaults to `http://127.0.0.1:8115`, so that flag is optional for
this local receiver. `--env-timeout-s` sets the readiness and command response
timeout, defaulting to 10 seconds. Unitree defaults to local memory and does not
download an HF corpus. The backend currently supports plain terminal runs.

Verify the receiver's journal, not just the planner's final statement:

```json
{"command": "前进", "received": true, "sequence": 1, "mode": "mock"}
```

The planner output directory contains `states.json` and
`transcript_unitree_<run-id>.json`. Their tool results include the receipt.
Environment termination remains false because physical motion was not measured.

## RPC contract

The business method is `env.execute_command(command)`. Its argument is exactly
one of the two Chinese strings. A successful response must include the same
`command` and `received: true`. The mock also returns `mode` and `sequence`.

The adapter uses RPent's existing `RpcClient` and `RpcFacade` directly. The
command-only contract has no Gym observation tuple or connection-time reset.
A hardware-side server can implement this same method when its motion and
stopping behavior have been defined. The included server remains a mock.

HTTP is the default transport; socket RPC is also available:

```bash
python -m robots.unitree.env_server --transport socket --port 8115
rpent --planner api --model openai:deepseek-flash \
  --robot unitree --task "让机器狗往后退" \
  --env-endpoint socket://127.0.0.1:8115 \
  --output-dir ./logs/unitree-backward
```

## Local checks

Install the existing test dependencies, then run:

```bash
pip install -e ".[test]"
python -m pytest tests/unit_tests/robots/unitree -v
```

`test_rpc_loopback.py` uses actual localhost HTTP/socket servers and requires
permission to create and bind network sockets. The other tests cover command
schemas, JSON request/response handling, server dispatch, receipts, and the
public CLI with the native API planner and a scripted test model. Those tests
replace network I/O and do not establish real socket connectivity.
