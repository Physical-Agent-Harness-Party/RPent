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

"""Unitree command schemas, HTTP serialization, receipts, and state capture."""

from __future__ import annotations

import json

import pytest

from robots.unitree.env_client import UnitreeEnvClient
from robots.unitree.env_server import MockUnitreeEnvFacade
from robots.unitree.toolkit import UnitreeToolkit
from rpent.dashboard.events import NullDashboardEventSink
from rpent.memory import MemoryManager
from rpent.utils.rpc import RpcError, make_rpc_client


def test_tools_send_exact_chinese_commands_and_record_receipts(tmp_path, http_bridge):
    toolkit = UnitreeToolkit(
        env=UnitreeEnvClient(make_rpc_client(http_bridge.endpoint)),
        dashboard_events=NullDashboardEventSink(),
        memory=MemoryManager(tmp_path / "memory"),
        state_output_dir=tmp_path / "run",
    )
    try:
        tools = {item.name: item for item in toolkit.list_tools()}
        for name in ("move_forward", "move_backward"):
            assert not tools[name].readonly
            assert tools[name].input_schema["properties"] == {}
        for name, command in (
            ("move_forward", "前进"),
            ("move_backward", "后退"),
        ):
            result = toolkit.execute_tool(name, {})
            assert not result.is_error
            assert result.data["command"] == command
            assert result.data["received"] is True
            assert result.data["mode"] == "mock"
        assert http_bridge.facade.received_commands == ["前进", "后退"]
        assert [item["args"] for item in http_bridge.requests] == [["前进"], ["后退"]]
        assert all(
            item["method"] == "env.execute_command" for item in http_bridge.requests
        )
        records = toolkit.state.records()
        assert len(records) == 3
        assert records[-1].state["acknowledged_commands"] == 2
        assert records[-1].result["command"] == "后退"
        assert all(not record.terminated for record in records)
        journal = [
            json.loads(line)
            for line in http_bridge.commands_file.read_text().splitlines()
        ]
        assert [item["command"] for item in journal] == ["前进", "后退"]
    finally:
        toolkit.close()


def test_unknown_command_is_rejected_by_the_server(http_bridge):
    client = UnitreeEnvClient(make_rpc_client(http_bridge.endpoint))
    with pytest.raises(RpcError, match="unsupported command"):
        client.execute_command("向左")
    assert http_bridge.facade.received_commands == []
    assert not http_bridge.commands_file.exists()


@pytest.mark.parametrize(
    "receipt",
    [
        {"command": "前进", "received": False},
        {"command": "后退", "received": True},
        True,
        None,
    ],
)
def test_client_requires_matching_acknowledgement(http_bridge, receipt):
    http_bridge.facade._rpc["env.execute_command"] = lambda command: receipt
    client = UnitreeEnvClient(make_rpc_client(http_bridge.endpoint))
    with pytest.raises(RpcError, match="did not acknowledge"):
        client.execute_command("前进")


def test_failed_send_remains_an_error_in_the_recorded_state(
    tmp_path, http_bridge, monkeypatch
):
    def unavailable(request, timeout=None):
        raise OSError("receiver unavailable")

    monkeypatch.setattr("urllib.request.urlopen", unavailable)
    toolkit = UnitreeToolkit(
        env=UnitreeEnvClient(make_rpc_client(http_bridge.endpoint)),
        dashboard_events=NullDashboardEventSink(),
        memory=MemoryManager(tmp_path / "memory"),
        state_output_dir=tmp_path / "run",
    )
    try:
        result = toolkit.execute_tool("move_forward", {})
        assert result.is_error
        assert "receiver unavailable" in result.data["error"]
        assert toolkit.state.get().state["acknowledged_commands"] == 0
        assert toolkit.state.get().result["error"]
        assert http_bridge.facade.received_commands == []
    finally:
        toolkit.close()


def test_mock_server_journals_delivery_without_a_robot(tmp_path):
    path = tmp_path / "receiver/commands.jsonl"
    facade = MockUnitreeEnvFacade(path)
    receipt = facade._dispatch("env.execute_command", ("前进",), {})
    assert json.loads(path.read_text()) == receipt
    assert receipt == {
        "command": "前进",
        "received": True,
        "sequence": 1,
        "mode": "mock",
    }
