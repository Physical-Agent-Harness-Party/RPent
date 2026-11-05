# Copyright 2026 The RPent Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""CLI startup and native API Planner integration with a multi-robot gateway."""

from __future__ import annotations

import argparse
import io
import json
import sys

import pytest
from pydantic_ai import ModelRequest, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from rpent.dashboard.events import NullDashboardEventSink
from rpent.robots import get_robot_spec


class TerminalInput(io.StringIO):
    def isatty(self):
        return True


def parse_args(tmp_path, *flags):
    spec = get_robot_spec("unitree")
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=str(tmp_path))
    parser.add_argument("--memory-profile", default=None)
    parser.add_argument("--memory-dir", default=None)
    spec.add_cli_args(parser, use_dashboard=False)
    return parser.parse_args(["--task", "查询机器人列表", *flags])


def test_config_keeps_task_default_robot_and_local_memory(tmp_path):
    spec = get_robot_spec("unitree")
    args = parse_args(tmp_path, "--robot-index", "2")
    config = spec.parse_config(args)
    assert args.memory_profile == "local"
    assert config.task_desc["task"] == "查询机器人列表"
    assert config.task_desc["robot_index"] == 2
    assert config.task_desc["validation_scope"] == "reported_motion_completion"
    assert args.env_timeout_s is None
    user = spec.prompts.render("user", variables=config.prompt_vars)
    assert "查询机器人列表" in user
    assert "DEFAULT ROBOT" in user and "2" in user
    assert config.recipe_tag.startswith("unitree_")


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf"])
def test_invalid_timeout_is_rejected_before_connecting(tmp_path, timeout):
    with pytest.raises(ValueError, match="env-timeout-s"):
        get_robot_spec("unitree").parse_config(
            parse_args(tmp_path, "--env-timeout-s", timeout)
        )


@pytest.mark.parametrize("index", ["0", "-1"])
def test_invalid_robot_index_is_rejected_before_connecting(tmp_path, index):
    with pytest.raises(ValueError, match="positive integer"):
        get_robot_spec("unitree").parse_config(
            parse_args(tmp_path, "--robot-index", index)
        )


@pytest.mark.parametrize(
    ("flags", "error"),
    [
        (["--task", "   "], "task description"),
        (["--memory-profile", "hf"], "memory-profile local"),
    ],
)
def test_invalid_task_configuration_is_rejected(tmp_path, flags, error):
    with pytest.raises(ValueError, match=error):
        get_robot_spec("unitree").parse_config(parse_args(tmp_path, *flags))


def test_startup_probes_gateway_without_unbound_healthz(tmp_path, http_bridge):
    spec = get_robot_spec("unitree")
    args = parse_args(tmp_path, "--env-endpoint", http_bridge.endpoint)
    daemons, runtime = spec.init_runtime(args, tmp_path, NullDashboardEventSink(), None)
    try:
        assert daemons == []
        assert http_bridge.requests
        assert all(r["method"] == "env.list_robots" for r in http_bridge.requests)
        assert not http_bridge.facade.received_commands
    finally:
        runtime["env"].close()


def test_startup_accepts_no_online_robots_without_a_default_target(
    tmp_path, http_bridge
):
    http_bridge.facade.robot_statuses.clear()
    spec = get_robot_spec("unitree")
    args = parse_args(tmp_path, "--env-endpoint", http_bridge.endpoint)
    _, runtime = spec.init_runtime(args, tmp_path, NullDashboardEventSink(), None)
    runtime["env"].close()


@pytest.mark.parametrize(
    ("index", "error"), [(1, "offline"), (3, "unknown robot_index")]
)
def test_startup_rejects_unavailable_default_robot_and_closes_client(
    tmp_path, http_bridge, monkeypatch, index, error
):
    from robots.unitree import robot_spec
    from rpent.utils.rpc import make_rpc_client

    rpc = make_rpc_client(http_bridge.endpoint)
    monkeypatch.setattr(robot_spec, "make_rpc_client", lambda endpoint: rpc)
    http_bridge.facade.robot_statuses[1] = "offline"
    args = parse_args(
        tmp_path, "--env-endpoint", http_bridge.endpoint, "--robot-index", str(index)
    )
    with pytest.raises(RuntimeError, match=error):
        robot_spec._init_runtime(args, tmp_path, NullDashboardEventSink(), None)
    assert rpc._closed
    assert not http_bridge.facade.received_commands


@pytest.mark.parametrize("default_robot", [None, 1])
@pytest.mark.parametrize("failed_completion", [False, True])
def test_cli_planner_queries_and_routes_commands_to_two_robots(
    tmp_path, monkeypatch, http_bridge, default_robot, failed_completion
):
    from rpent.cli import main as cli

    steps = [("list_robots", {}), ("get_robot_status", {"robot_index": 1})]
    commands = []
    for index, dx, dy, turn in ((1, 0.4, 0.3, 30), (2, 0, 0, 450)):
        payload = {"dx_m": dx, "dy_m": dy, "arrival_turn_deg": turn}
        command = {"type": "dis_move", "payload": payload}
        args = dict(payload)
        if index != default_robot:
            args["robot_index"] = index
        steps.append(("execute_command", args))
        commands.append(command)
        if failed_completion:
            break
    if failed_completion:
        simulate = http_bridge.facade._simulate_motion

        def without_stop(command):
            return {**simulate(command), "stop_acknowledged": False}

        monkeypatch.setattr(http_bridge.facade, "_simulate_motion", without_stop)
    finish_status = "failure" if failed_completion else "success"
    steps.append(
        (
            "finish",
            {"status": finish_status, "summary": "Synthetic motion completion test"},
        )
    )
    task = "本地模拟：机器人1去前方0.4米、左侧0.3米，停稳后左转30度；成功后机器人2原地左转450度。失败时停止发送后续动作。"
    requests = []

    async def stream(messages, info):
        names = {tool.name for tool in info.function_tools}
        assert {"list_robots", "get_robot_status", "execute_command"} <= names
        assert any(tool.name == "finish" for tool in info.output_tools)
        index = len(requests)
        requests.append(messages)
        name, args = steps[index]
        yield {0: DeltaToolCall(name=name, json_args=json.dumps(args))}

    monkeypatch.setattr(
        "rpent.planner.base.build_api_model",
        lambda *args, **kwargs: FunctionModel(stream_function=stream),
    )
    monkeypatch.setattr(sys, "stdin", TerminalInput())
    output = tmp_path / "run"
    argv = [
        "rpent",
        "--robot",
        "unitree",
        "--task",
        task,
        "--planner",
        "api",
        "--model",
        "openai-chat:offline-unitree",
        "--reasoning-effort",
        "none",
        "--env-endpoint",
        http_bridge.endpoint,
        "--max-turns",
        str(len(steps)),
        "--planner-timeout-s",
        "5",
        "--output-dir",
        str(output),
    ]
    if default_robot is not None:
        argv += ["--robot-index", str(default_robot)]
    monkeypatch.setattr(sys, "argv", argv)
    assert cli.main() == 0
    assert [r["command"] for r in http_bridge.facade.received_commands] == commands
    assert [r["robot_index"] for r in http_bridge.facade.received_commands] == (
        [1] if failed_completion else [1, 2]
    )
    prompt_parts = [
        part.content
        for message in requests[0]
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart)
    ]
    assert any(task in value for value in prompt_parts)
    record = json.loads(next(output.glob("transcript_unitree_*.json")).read_text())
    assert record["task"] == task
    assert record["error"] is None
    assert record["finish"]["status"] == finish_status
    assert record["validation_scope"] == "reported_motion_completion"
    assert record["robot_index"] == default_robot
    tool_messages = [m for m in record["messages"] if m["role"] == "tool"]
    assert [m["name"] for m in tool_messages] == [name for name, _ in steps]
    states = json.loads((output / "states.json").read_text())
    if failed_completion:
        assert states["steps"][-1]["state"]["completed_commands"] == 0
        assert states["steps"][-1]["result"]["error"]
    else:
        assert states["steps"][-1]["state"]["completed_by_robot"] == {"1": 1, "2": 1}
        assert (
            states["steps"][-1]["result"]["motion_result"]["actual_arrival_turn_deg"]
            == 450
        )
    assert all(not step["terminated"] for step in states["steps"])
