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

"""Public CLI and native API Planner integration with the Unitree command tools."""

from __future__ import annotations

import argparse
import io
import json
import sys

import pytest
from pydantic_ai import ModelRequest, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from rpent.robots import get_robot_spec


class TerminalInput(io.StringIO):
    def isatty(self):
        return True


def test_config_keeps_free_form_task_text_and_local_memory(tmp_path):
    spec = get_robot_spec("unitree")
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=str(tmp_path))
    parser.add_argument("--memory-profile", default=None)
    parser.add_argument("--memory-dir", default=None)
    spec.add_cli_args(parser, use_dashboard=False)
    args = parser.parse_args(["--task", "先前进，再后退"])
    config = spec.parse_config(args)
    assert args.memory_profile == "local"
    assert config.task_desc["task"] == "先前进，再后退"
    assert config.task_desc["validation_scope"] == "command_delivery"
    user = spec.prompts.render("user", variables=config.prompt_vars)
    assert "先前进，再后退" in user
    assert config.recipe_tag.startswith("unitree_")


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf"])
def test_invalid_timeout_is_rejected_before_connecting(tmp_path, timeout):
    spec = get_robot_spec("unitree")
    args = argparse.Namespace(
        task="前进",
        output_dir=str(tmp_path),
        memory_profile="local",
        memory_dir=None,
        env_endpoint="http://unitree.test:8115",
        env_timeout_s=float(timeout),
    )
    with pytest.raises(ValueError, match="env-timeout-s"):
        spec.parse_config(args)


@pytest.mark.parametrize(
    ("task", "profile", "error"),
    [
        ("   ", "local", "task description"),
        ("前进", "hf", "memory-profile local"),
    ],
)
def test_invalid_task_configuration_is_rejected(tmp_path, task, profile, error):
    spec = get_robot_spec("unitree")
    args = argparse.Namespace(
        task=task,
        output_dir=str(tmp_path),
        memory_profile=profile,
        memory_dir=None,
        env_endpoint="http://unitree.test:8115",
        env_timeout_s=10,
    )
    with pytest.raises(ValueError, match=error):
        spec.parse_config(args)


@pytest.mark.parametrize(
    ("task", "actions", "commands"),
    [
        ("让机器狗往前走", ["move_forward"], ["前进"]),
        ("让机器狗往后退", ["move_backward"], ["后退"]),
        ("先前进，再后退", ["move_forward", "move_backward"], ["前进", "后退"]),
    ],
)
def test_cli_exposes_tools_to_api_planner_and_delivers_commands(
    tmp_path, monkeypatch, http_bridge, task, actions, commands
):
    from rpent.cli import main as cli

    requests = []

    async def stream(messages, info):
        names = {tool.name for tool in info.function_tools}
        assert {"move_forward", "move_backward"} <= names
        assert any(tool.name == "finish" for tool in info.output_tools)
        requests.append(messages)
        index = len(requests) - 1
        if index < len(actions):
            yield {0: DeltaToolCall(name=actions[index], json_args="{}")}
        else:
            yield {
                0: DeltaToolCall(
                    name="finish",
                    json_args=json.dumps(
                        {"status": "success", "summary": "Requested commands received"}
                    ),
                )
            }

    monkeypatch.setattr(
        "rpent.planner.base.build_api_model",
        lambda *args, **kwargs: FunctionModel(stream_function=stream),
    )
    monkeypatch.setattr(sys, "stdin", TerminalInput())
    output = tmp_path / "run"
    monkeypatch.setattr(
        sys,
        "argv",
        [
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
            str(len(actions) + 1),
            "--planner-timeout-s",
            "5",
            "--output-dir",
            str(output),
        ],
    )
    assert cli.main() == 0
    assert http_bridge.facade.received_commands == commands
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
    assert record["finish"]["status"] == "success"
    assert record["validation_scope"] == "command_delivery"
    tool_messages = [m for m in record["messages"] if m["role"] == "tool"]
    assert [m["name"] for m in tool_messages] == [*actions, "finish"]
    receipts = [json.loads(m["content"]) for m in tool_messages[:-1]]
    assert [receipt["command"] for receipt in receipts] == commands
    assert all(receipt["received"] is True for receipt in receipts)
    states = json.loads((output / "states.json").read_text())
    assert states["steps"][-1]["state"]["acknowledged_commands"] == len(commands)
    assert all(not step["terminated"] for step in states["steps"])
