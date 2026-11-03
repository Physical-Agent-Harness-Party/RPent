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

    def open_request(request, timeout=None):
        assert request.get_method() == "POST"
        assert request.full_url == "http://unitree.test:8115/call"
        requests.append(json.loads(request.data))
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
        endpoint="http://unitree.test:8115",
        commands_file=tmp_path / "commands.jsonl",
    )
