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

"""Dis_move validation, routed synchronous completion, and state capture."""

from __future__ import annotations

import copy
import json

import pytest

from robots.unitree.env_client import UnitreeEnvClient
from robots.unitree.env_server import MockUnitreeEnvFacade
from robots.unitree.toolkit import UnitreeToolkit
from rpent.dashboard.events import NullDashboardEventSink
from rpent.memory import MemoryManager
from rpent.utils.rpc import RpcError, make_rpc_client


def test_tools_route_moves_and_record_completed_actions_per_robot(
    tmp_path, http_bridge
):
    toolkit = UnitreeToolkit(
        env=UnitreeEnvClient(make_rpc_client(http_bridge.endpoint)),
        dashboard_events=NullDashboardEventSink(),
        memory=MemoryManager(tmp_path / "memory"),
        state_output_dir=tmp_path / "run",
    )
    try:
        tools = {item.name: item for item in toolkit.list_tools()}
        assert tools["list_robots"].readonly
        assert tools["get_robot_status"].readonly
        assert not tools["execute_command"].readonly
        assert set(tools["execute_command"].input_schema["properties"]) == {
            "dx_m",
            "dy_m",
            "arrival_turn_deg",
            "robot_index",
        }
        assert toolkit.execute_tool("list_robots", {}).data["total"] == 2
        assert toolkit.execute_tool("get_robot_status", {"robot_index": 1}).data == {
            "robot_index": 1,
            "status": "online",
        }
        assert len(toolkit.state.records()) == 1
        expected = []
        for index, dx, dy, turn in ((1, 0.4, 0.3, 30), (2, 0, 0, 450), (1, 3, 4, -30)):
            payload = {"dx_m": dx, "dy_m": dy, "arrival_turn_deg": turn}
            result = toolkit.execute_tool(
                "execute_command", {**payload, "robot_index": index}
            )
            assert not result.is_error
            assert result.data["completed"] is True
            assert result.data["motion_result"]["actual_arrival_turn_deg"] == turn
            expected.append((index, {"type": "dis_move", "payload": payload}))
        calls = [
            r for r in http_bridge.requests if r["method"] == "env.execute_command"
        ]
        assert [(r["kwargs"]["robot_index"], r["args"][0]) for r in calls] == expected
        assert all(r["session_id"] is None for r in calls)
        records = toolkit.state.records()
        assert len(records) == 4
        assert records[-1].state == {
            "acknowledged_commands": 3,
            "acknowledged_by_robot": {"1": 2, "2": 1},
            "completed_commands": 3,
            "completed_by_robot": {"1": 2, "2": 1},
        }
        assert records[-1].extras["validation_scope"] == "reported_motion_completion"
        assert records[-1].result["motion_result"]["stop_acknowledged"] is True
        assert all(not record.terminated for record in records)
        journal = [
            json.loads(line)
            for line in http_bridge.commands_file.read_text().splitlines()
        ]
        assert [(item["robot_index"], item["command"]) for item in journal] == expected
    finally:
        toolkit.close()


def test_empty_list_offline_and_unknown_robot_have_distinct_results(
    http_bridge, move_command
):
    client = UnitreeEnvClient(make_rpc_client(http_bridge.endpoint))
    http_bridge.facade.robot_statuses = {1: "offline", 3: "online"}
    assert client.list_robots() == {
        "robots": [{"robot_index": 3, "status": "online"}],
        "total": 1,
    }
    assert client.get_robot_status(1) == {"robot_index": 1, "status": "offline"}
    with pytest.raises(RpcError, match="offline"):
        client.execute_command(move_command, robot_index=1)
    with pytest.raises(RpcError, match="unknown robot_index"):
        client.get_robot_status(2)
    http_bridge.facade.robot_statuses[3] = "offline"
    assert client.list_robots() == {"robots": [], "total": 0}
    assert not http_bridge.facade.received_commands


@pytest.mark.parametrize("index", [0, -1, True, 1.0, "1", None])
def test_invalid_robot_index_is_rejected_without_sending(
    http_bridge, move_command, index
):
    client = UnitreeEnvClient(make_rpc_client(http_bridge.endpoint))
    with pytest.raises(ValueError, match="positive integer"):
        client.execute_command(move_command, robot_index=index)
    assert not http_bridge.requests


@pytest.mark.parametrize(
    "command",
    [
        "前进",
        {},
        {"type": "demo", "payload": {}},
        {"type": "dis_move", "payload": []},
        {"type": "dis_move", "payload": {"dx_m": 1, "dy_m": 0}},
        {
            "type": "dis_move",
            "payload": {"dx_m": 1, "dy_m": 0, "arrival_turn_deg": 0, "speed": 1},
        },
    ],
)
def test_invalid_command_shape_is_rejected_at_both_boundaries(http_bridge, command):
    rpc = make_rpc_client(http_bridge.endpoint)
    client = UnitreeEnvClient(rpc, robot_index=1)
    with pytest.raises(ValueError, match="command"):
        client.execute_command(command)
    assert not http_bridge.requests
    with pytest.raises(RpcError, match="command"):
        rpc.call("env.execute_command", args=(command,), kwargs={"robot_index": 1})
    assert not http_bridge.facade.received_commands


@pytest.mark.parametrize("field", ["dx_m", "dy_m", "arrival_turn_deg"])
@pytest.mark.parametrize(
    "value", [True, "1", None, float("nan"), float("inf"), -float("inf")]
)
def test_motion_numbers_must_be_finite_and_not_booleans(
    http_bridge, move_command, field, value
):
    move_command["payload"][field] = value
    client = UnitreeEnvClient(make_rpc_client(http_bridge.endpoint), robot_index=1)
    with pytest.raises(ValueError, match="finite number"):
        client.execute_command(move_command)
    assert not http_bridge.requests


@pytest.mark.parametrize(("dx", "dy"), [(4, 4), (5.001, 0), (-5, 1)])
def test_combined_displacement_over_5m_is_rejected(http_bridge, move_command, dx, dy):
    move_command["payload"].update(dx_m=dx, dy_m=dy)
    client = UnitreeEnvClient(make_rpc_client(http_bridge.endpoint), robot_index=1)
    with pytest.raises(ValueError, match="5 m"):
        client.execute_command(move_command)
    assert not http_bridge.requests


@pytest.mark.parametrize(
    ("payload", "timeout"),
    [
        ({"dx_m": 0.4, "dy_m": 0.3, "arrival_turn_deg": 30}, 78.813856299642),
        ({"dx_m": -1, "dy_m": 0, "arrival_turn_deg": 0}, 105.701640821612),
        ({"dx_m": 3, "dy_m": 4, "arrival_turn_deg": 0}, 140.701523608588),
        ({"dx_m": 0, "dy_m": 0, "arrival_turn_deg": 450}, 138.539816339745),
    ],
)
def test_default_wait_covers_motion_budget_and_preserves_full_turns(
    http_bridge, payload, timeout
):
    client = UnitreeEnvClient(make_rpc_client(http_bridge.endpoint), robot_index=1)
    receipt = client.execute_command({"type": "dis_move", "payload": payload})
    assert receipt["completed"] is True
    assert (
        receipt["motion_result"]["actual_arrival_turn_deg"]
        == payload["arrival_turn_deg"]
    )
    assert http_bridge.timeouts[-1] == ("env.execute_command", pytest.approx(timeout))


def test_explicit_timeout_override_does_not_inflate_query_timeouts(
    http_bridge, move_command
):
    client = UnitreeEnvClient(
        make_rpc_client(http_bridge.endpoint), robot_index=1, timeout_s=150
    )
    client.list_robots()
    client.get_robot_status()
    client.execute_command(move_command)
    assert http_bridge.timeouts == [
        ("env.list_robots", 10),
        ("healthz", 1),
        ("env.execute_command", 150),
    ]


def test_client_accepts_documented_rounded_motion_measurements(
    http_bridge, move_command, completed_response
):
    http_bridge.facade._rpc["env.execute_command"] = lambda *args, **kwargs: (
        completed_response
    )
    client = UnitreeEnvClient(make_rpc_client(http_bridge.endpoint), robot_index=1)
    assert client.execute_command(move_command) == completed_response


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("received",), False),
        (("received",), 1),
        (("received",), "true"),
        (("completed",), False),
        (("completed",), 1),
        (("completed",), None),
        (("robot_index",), 2),
        (("robot_index",), True),
        (("command", "payload", "dx_m"), True),
        (("command", "payload", "dx_m"), 0.5),
        (("motion_result",), None),
        (("motion_result", "actual_dx_m"), None),
        (("motion_result", "actual_dy_m"), float("nan")),
        (("motion_result", "total_angle_deg"), float("inf")),
        (("motion_result", "position_error_m"), -0.01),
        (("motion_result", "position_error_m"), 0.071),
        (("motion_result", "angle_error_deg"), -3.1),
        (("motion_result", "stop_acknowledged"), False),
        (("motion_result", "stop_acknowledged"), 1),
        (("motion_result", "actual_dx_m"), 2),
        (("motion_result", "actual_arrival_turn_deg"), 90),
    ],
)
def test_client_rejects_incomplete_mismatched_or_unaccepted_motion(
    http_bridge, move_command, completed_response, path, value
):
    receipt = copy.deepcopy(completed_response)
    parent = receipt
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value
    http_bridge.facade._rpc["env.execute_command"] = lambda *args, **kwargs: receipt
    client = UnitreeEnvClient(make_rpc_client(http_bridge.endpoint), robot_index=1)
    with pytest.raises(RpcError):
        client.execute_command(move_command)


def test_receipt_without_completed_field_is_not_a_success(
    http_bridge, move_command, completed_response
):
    completed_response.pop("completed")
    http_bridge.facade._rpc["env.execute_command"] = lambda *args, **kwargs: (
        completed_response
    )
    client = UnitreeEnvClient(make_rpc_client(http_bridge.endpoint), robot_index=1)
    with pytest.raises(RpcError, match="completed motion"):
        client.execute_command(move_command)


def test_equivalent_json_numbers_in_echo_are_accepted(
    http_bridge, move_command, completed_response
):
    move_command = copy.deepcopy(move_command)
    move_command["payload"]["arrival_turn_deg"] = 30
    http_bridge.facade._rpc["env.execute_command"] = lambda *args, **kwargs: (
        completed_response
    )
    client = UnitreeEnvClient(make_rpc_client(http_bridge.endpoint), robot_index=1)
    assert client.execute_command(move_command)["completed"] is True


def test_full_turn_cannot_be_reported_as_only_its_modulo(
    http_bridge, move_command, completed_response
):
    move_command["payload"].update(dx_m=0, dy_m=0, arrival_turn_deg=450)
    completed_response["motion_result"].update(
        actual_dx_m=0,
        actual_dy_m=0,
        actual_arrival_turn_deg=90,
        total_angle_deg=90,
        position_error_m=0,
        angle_error_deg=0,
    )
    http_bridge.facade._rpc["env.execute_command"] = lambda *args, **kwargs: (
        completed_response
    )
    client = UnitreeEnvClient(make_rpc_client(http_bridge.endpoint), robot_index=1)
    with pytest.raises(RpcError, match="3 degree"):
        client.execute_command(move_command)


@pytest.mark.parametrize(
    "result",
    [
        None,
        {"robots": [], "total": True},
        {"robots": [], "total": 1},
        {"robots": [{"robot_index": 0, "status": "online"}], "total": 1},
        {"robots": [{"robot_index": 1, "status": "offline"}], "total": 1},
        {
            "robots": [
                {"robot_index": 2, "status": "online"},
                {"robot_index": 1, "status": "online"},
            ],
            "total": 2,
        },
        {"robots": [{"robot_index": 1, "status": "online"}] * 2, "total": 2},
    ],
)
def test_client_rejects_malformed_robot_lists(http_bridge, result):
    http_bridge.facade._rpc["env.list_robots"] = lambda: result
    with pytest.raises(RpcError):
        UnitreeEnvClient(make_rpc_client(http_bridge.endpoint)).list_robots()


@pytest.mark.parametrize(
    "status",
    [
        None,
        {"status": "online"},
        {"robot_index": 2, "status": "online"},
        {"robot_index": 1, "status": "ok"},
    ],
)
def test_client_rejects_mismatched_robot_status(http_bridge, monkeypatch, status):
    monkeypatch.setattr(http_bridge.facade, "_dispatch", lambda *args, **kwargs: status)
    with pytest.raises(RpcError, match="invalid status"):
        UnitreeEnvClient(make_rpc_client(http_bridge.endpoint)).get_robot_status(1)


def test_failed_send_is_not_retried_or_counted_as_completed(
    tmp_path, http_bridge, monkeypatch
):
    attempts = []

    def unavailable(request, timeout=None):
        attempts.append(json.loads(request.data))
        raise TimeoutError("completion response timed out")

    monkeypatch.setattr("urllib.request.urlopen", unavailable)
    toolkit = UnitreeToolkit(
        env=UnitreeEnvClient(make_rpc_client(http_bridge.endpoint), robot_index=1),
        dashboard_events=NullDashboardEventSink(),
        memory=MemoryManager(tmp_path / "memory"),
        state_output_dir=tmp_path / "run",
    )
    try:
        result = toolkit.execute_tool(
            "execute_command", {"dx_m": 0.4, "dy_m": 0, "arrival_turn_deg": 0}
        )
        assert result.is_error
        assert "timed out" in result.data["error"]
        assert len(attempts) == 1
        assert toolkit.state.get().state["completed_commands"] == 0
        assert not http_bridge.facade.received_commands
    finally:
        toolkit.close()


def test_mock_rejects_legacy_strings_and_journals_completed_moves(
    tmp_path, move_command
):
    path = tmp_path / "receiver/commands.jsonl"
    facade = MockUnitreeEnvFacade(path)
    with pytest.raises(ValueError, match="dis_move object"):
        facade._dispatch("env.execute_command", ("前进",), {"robot_index": 1})
    receipt = facade._dispatch(
        "env.execute_command", (move_command,), {"robot_index": 2}
    )
    assert json.loads(path.read_text()) == receipt
    assert receipt["completed"] is True
    assert receipt["motion_result"]["stop_acknowledged"] is True
    assert receipt["mode"] == "mock"
