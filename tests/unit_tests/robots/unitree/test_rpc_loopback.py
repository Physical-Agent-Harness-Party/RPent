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

"""Real local dis_move RPCs, per-robot admission, and late completion after timeout."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from robots.unitree.env_client import UnitreeEnvClient
from robots.unitree.env_server import MockUnitreeEnvFacade
from rpent.utils.rpc import RpcError, make_rpc_client, wait_for_ready
from rpent.utils.rpc.http_rpc import HttpRpcServer
from rpent.utils.rpc.socket_rpc import SocketRpcServer


@pytest.fixture(params=["http", "socket"])
def gateway(tmp_path, request):
    path = tmp_path / "commands.jsonl"
    facade = MockUnitreeEnvFacade(path, robot_indices=(1, 3))
    server_type = HttpRpcServer if request.param == "http" else SocketRpcServer
    server = server_type(("127.0.0.1", 0), facade._dispatch)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    endpoint = f"{request.param}://127.0.0.1:{server.server_address[1]}"
    try:
        yield SimpleNamespace(endpoint=endpoint, facade=facade, journal=path)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_real_rpc_routes_queries_and_completed_moves(gateway, move_command):
    rpc = make_rpc_client(gateway.endpoint)
    client = UnitreeEnvClient(rpc)
    try:
        wait_for_ready(rpc, timeout_s=2, probe_method="env.list_robots")
        assert client.list_robots() == {
            "robots": [
                {"robot_index": 1, "status": "online"},
                {"robot_index": 3, "status": "online"},
            ],
            "total": 2,
        }
        assert client.get_robot_status(3) == {"robot_index": 3, "status": "online"}
        for index in (1, 3):
            receipt = client.execute_command(move_command, robot_index=index)
            assert receipt["completed"] is True
            assert receipt["motion_result"]["stop_acknowledged"] is True
            assert receipt["command"] == move_command
            assert receipt["robot_index"] == index
        with pytest.raises(RpcError, match="unknown robot_index"):
            client.execute_command(move_command, robot_index=2)
        journal = [
            json.loads(line) for line in gateway.journal.read_text().splitlines()
        ]
        assert [item["robot_index"] for item in journal] == [1, 3]
    finally:
        client.close()


def test_busy_robot_rejects_immediately_while_other_robot_can_complete(
    gateway, monkeypatch, move_command
):
    started = threading.Event()
    release = threading.Event()
    simulate = gateway.facade._simulate_motion

    def blocking_motion(command):
        if command["payload"]["dx_m"] == 0.4:
            started.set()
            assert release.wait(5), "test motion was not released"
        return simulate(command)

    monkeypatch.setattr(gateway.facade, "_simulate_motion", blocking_motion)
    first = UnitreeEnvClient(make_rpc_client(gateway.endpoint))
    second = UnitreeEnvClient(make_rpc_client(gateway.endpoint))
    other = UnitreeEnvClient(make_rpc_client(gateway.endpoint))
    small_move = {
        "type": "dis_move",
        "payload": {"dx_m": 0.1, "dy_m": 0, "arrival_turn_deg": 0},
    }
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            moving = pool.submit(first.execute_command, move_command, robot_index=1)
            try:
                assert started.wait(2)
                busy = pool.submit(second.execute_command, small_move, robot_index=1)
                with pytest.raises(RpcError, match="robot is busy"):
                    busy.result(timeout=2)
                independent = pool.submit(
                    other.execute_command, small_move, robot_index=3
                )
                assert independent.result(timeout=2)["completed"] is True
                assert other.get_robot_status(1)["status"] == "online"
                assert other.list_robots()["total"] == 2
            finally:
                release.set()
            assert moving.result(timeout=2)["completed"] is True
        assert second.execute_command(small_move, robot_index=1)["completed"] is True
        assert [r["sequence"] for r in gateway.facade.received_commands] == [1, 2, 3]
    finally:
        release.set()
        for client in (first, second, other):
            client.close()


def test_failed_motion_releases_robot_for_the_next_request(
    gateway, monkeypatch, move_command
):
    simulate = gateway.facade._simulate_motion

    def failed_motion(command):
        raise RuntimeError("control failed after partial movement")

    monkeypatch.setattr(gateway.facade, "_simulate_motion", failed_motion)
    client = UnitreeEnvClient(make_rpc_client(gateway.endpoint), robot_index=1)
    try:
        with pytest.raises(RpcError, match="control failed"):
            client.execute_command(move_command)
        assert not gateway.facade.received_commands
        monkeypatch.setattr(gateway.facade, "_simulate_motion", simulate)
        assert client.execute_command(move_command)["completed"] is True
    finally:
        client.close()


def test_timeout_does_not_cancel_motion_or_automatically_retry(
    gateway, monkeypatch, move_command
):
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    simulate = gateway.facade._simulate_motion
    execute = gateway.facade._rpc["env.execute_command"]
    attempts = []

    def blocking_motion(command):
        started.set()
        assert release.wait(5), "test motion was not released"
        return simulate(command)

    def track_execution(command, **kwargs):
        attempts.append(command)
        try:
            return execute(command, **kwargs)
        finally:
            completed.set()

    monkeypatch.setattr(gateway.facade, "_simulate_motion", blocking_motion)
    gateway.facade._rpc["env.execute_command"] = track_execution
    short_wait = UnitreeEnvClient(
        make_rpc_client(gateway.endpoint), robot_index=1, timeout_s=0.05
    )
    observer = UnitreeEnvClient(make_rpc_client(gateway.endpoint), robot_index=1)
    try:
        with pytest.raises((RpcError, TimeoutError)):
            short_wait.execute_command(move_command)
        assert started.wait(2)
        assert not completed.is_set()
        assert observer.get_robot_status()["status"] == "online"
        assert len(attempts) == 1
    finally:
        release.set()
        assert completed.wait(2)
        short_wait.close()
        observer.close()
    assert len(gateway.facade.received_commands) == 1
    assert gateway.facade.received_commands[0]["completed"] is True
