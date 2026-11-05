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

"""An in-memory HTTP boundary for environments where loopback sockets are blocked."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from robots.unitree.env_server import MockUnitreeEnvFacade
from rpent.utils.rpc.http_rpc import _HttpRpcHandler


@pytest.fixture(autouse=True)
def tool_output_dir(tmp_path, monkeypatch):
    """Provide the run directory expected by shared tool-description templates."""
    monkeypatch.setattr("rpent.utils.logging._output_dir", tmp_path / "run")


@pytest.fixture
def http_bridge(tmp_path, monkeypatch):
    """Run HTTP request handling and RPC dispatch while replacing network I/O."""
    facade = MockUnitreeEnvFacade(tmp_path / "commands.jsonl")
    requests = []
    timeouts = []

    def open_request(request, timeout=None):
        assert request.get_method() == "POST"
        assert request.full_url == "http://unitree.test:8115/call"
        requests.append(json.loads(request.data))
        timeouts.append((requests[-1]["method"], timeout))
        output = io.BytesIO()
        handler = SimpleNamespace(
            path="/call",
            headers={"Content-Length": str(len(request.data))},
            rfile=io.BytesIO(request.data),
            wfile=output,
            server=SimpleNamespace(dispatch=facade._dispatch),
            send_response=lambda code: None,
            send_header=lambda *args: None,
            end_headers=lambda: None,
        )
        _HttpRpcHandler.do_POST(handler)
        return io.BytesIO(output.getvalue())

    monkeypatch.setattr("urllib.request.urlopen", open_request)
    return SimpleNamespace(
        facade=facade,
        requests=requests,
        timeouts=timeouts,
        endpoint="http://unitree.test:8115",
        commands_file=tmp_path / "commands.jsonl",
    )


@pytest.fixture
def move_command():
    """Use the movement example from the current interface document."""
    return {
        "type": "dis_move",
        "payload": {"dx_m": 0.4, "dy_m": 0.3, "arrival_turn_deg": 30.0},
    }


@pytest.fixture
def completed_response(move_command):
    """Use the documented, rounded body-side completion measurements."""
    return {
        "robot_index": 1,
        "command": move_command,
        "received": True,
        "completed": True,
        "motion_result": {
            "actual_dx_m": 0.338774,
            "actual_dy_m": 0.278366,
            "actual_arrival_turn_deg": 31.1027,
            "total_angle_deg": 72.6875,
            "position_error_m": 0.0649355,
            "angle_error_deg": -1.10272,
            "stop_acknowledged": True,
        },
    }
