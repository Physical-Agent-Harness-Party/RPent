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

"""Real localhost RPC checks; the host must allow binding loopback sockets."""

from __future__ import annotations

import json
import threading

import pytest

from robots.unitree.env_client import UnitreeEnvClient
from robots.unitree.env_server import MockUnitreeEnvFacade
from rpent.utils.rpc import RpcError, make_rpc_client
from rpent.utils.rpc.http_rpc import HttpRpcServer
from rpent.utils.rpc.socket_rpc import SocketRpcServer


@pytest.mark.parametrize("transport", ["http", "socket"])
def test_real_rpc_delivers_exact_command_strings(tmp_path, transport):
    path = tmp_path / "commands.jsonl"
    facade = MockUnitreeEnvFacade(path)
    server_type = HttpRpcServer if transport == "http" else SocketRpcServer
    server = server_type(("127.0.0.1", 0), facade._dispatch)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    endpoint = f"{transport}://127.0.0.1:{server.server_address[1]}"
    client = UnitreeEnvClient(make_rpc_client(endpoint))
    try:
        for index, command in enumerate(("前进", "后退"), start=1):
            receipt = client.execute_command(command)
            assert receipt["received"] is True
            assert receipt["command"] == command
            assert receipt["sequence"] == index
        with pytest.raises(RpcError, match="unsupported command"):
            client.execute_command("向左")
        assert facade.received_commands == ["前进", "后退"]
        journal = [json.loads(line) for line in path.read_text().splitlines()]
        assert [item["command"] for item in journal] == ["前进", "后退"]
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert not thread.is_alive()
