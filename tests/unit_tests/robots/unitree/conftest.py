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
import math
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
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


@pytest.fixture
def navigation_services():
    """Document-shaped REST services with asynchronous walk and stop behavior."""
    from robots.unitree.navigation_client import NavigationClient
    from robots.unitree.simulation_client import SimulationClient

    identity = {"map_id": "test_room", "map_version": "v1"}
    state = SimpleNamespace(
        requests=[],
        submissions=[],
        pose={"x": 0.0, "y": 0.0, "yaw": 0.0, "fallen": False},
        sim_version="v1",
        running=False,
        hold=False,
        stopped=False,
        failed=False,
        simulator_running=True,
        other_controllers=[],
        remaining=0,
        polls_before_finish=1,
        walk_id=None,
        status_walk_id=None,
        submit_timeout=False,
        preview_failed=False,
        plan_mutator=None,
        final_mutator=None,
        status_unavailable=False,
        started=threading.Event(),
    )
    waypoints = [
        {
            "id": "wp_01",
            "name": "广告牌",
            "stand_pose": {"x": 1.0, "y": 0.0, "yaw": 0.0},
        },
        {
            "id": "wp_02",
            "name": "厕所",
            "stand_pose": {"x": 1.0, "y": 1.0, "yaw": 1.57},
        },
    ]
    lock = threading.RLock()

    def response(request):
        with lock:
            payload = json.loads(request.content) if request.content else None
            path = request.url.path
            state.requests.append((request.method, request.url.host, path, payload))
            if request.url.host == "nav.test":
                if path == "/health":
                    return httpx.Response(
                        200, json={"ok": True, **identity, "waypoints": 2}
                    )
                if path == "/waypoints":
                    return httpx.Response(
                        200, json={"ok": True, **identity, "waypoints": waypoints}
                    )
                if path == "/plan":
                    legs = []
                    start = payload["start"]
                    previous = [start["x"], start["y"]]
                    for query in payload["goals"]:
                        if isinstance(query, str):
                            goal = next(
                                (g for g in waypoints if query in (g["id"], g["name"])),
                                None,
                            )
                            if goal is None:
                                return httpx.Response(
                                    400,
                                    json={"ok": False, "error": "unknown destination"},
                                )
                        else:
                            goal = {
                                "id": "xy",
                                "name": "coordinate",
                                "stand_pose": {**query, "yaw": query.get("yaw")},
                            }
                        target = goal["stand_pose"]
                        dx, dy = target["x"] - previous[0], target["y"] - previous[1]
                        path_points = [previous, [target["x"], target["y"]]]
                        legs.append(
                            {
                                "query": query,
                                "goal": goal,
                                "path": path_points,
                                "headings": [math.atan2(dy, dx)],
                                "length": math.hypot(dx, dy),
                            }
                        )
                        previous = path_points[-1]
                    result = {
                        "ok": True,
                        **identity,
                        "frame": "map",
                        "units": {"length": "meter", "angle": "radian"},
                        "start": start,
                        "goals": [g["goal"]["id"] for g in legs],
                        "legs": legs,
                        "total_length": sum(g["length"] for g in legs),
                        "warnings": [],
                    }
                    if state.plan_mutator:
                        state.plan_mutator(result)
                    return httpx.Response(200, json=result)
            if request.url.host == "sim.test":
                if path == "/health":
                    return httpx.Response(
                        200,
                        json={
                            "ok": True,
                            **identity,
                            "map_version": state.sim_version,
                            "simulator_running": state.simulator_running,
                            "other_controllers": state.other_controllers,
                            "walk": {
                                "state": "running" if state.running else "finished"
                            },
                        },
                    )
                if path == "/pose":
                    return httpx.Response(200, json={"ok": True, **state.pose})
                if path == "/preview":
                    return httpx.Response(
                        200,
                        json={
                            "ok": True,
                            "result": {"ok": not state.preview_failed},
                            "image": "/files/preview.png",
                        },
                    )
                if path == "/walk" and request.method == "POST":
                    if state.running:
                        return httpx.Response(
                            409, json={"ok": False, "error": "walk already running"}
                        )
                    state.submissions.append(payload)
                    state.running, state.stopped, state.remaining = (
                        True,
                        False,
                        state.polls_before_finish,
                    )
                    state.walk_id = f"walk_{len(state.submissions)}"
                    state.started.set()
                    if state.submit_timeout:
                        raise httpx.ReadTimeout("acceptance timed out", request=request)
                    return httpx.Response(
                        200, json={"ok": True, "walk_id": state.walk_id}
                    )
                if path == "/walk/stop":
                    state.stopped = True
                    return httpx.Response(200, json={"ok": True})
                if path == "/walk" and request.method == "GET":
                    if state.status_unavailable:
                        return httpx.Response(
                            503,
                            json={
                                "ok": False,
                                "error": "status temporarily unavailable",
                            },
                        )
                    walk_id = state.status_walk_id or state.walk_id
                    if not state.stopped and (state.hold or state.remaining > 0):
                        state.remaining -= 1
                        return httpx.Response(
                            200,
                            json={"ok": True, "state": "running", "walk_id": walk_id},
                        )
                    state.running = False
                    reached = not state.failed and not state.stopped
                    goal = state.submissions[-1]["legs"][0]["goal"]["stand_pose"]
                    if reached:
                        state.pose.update(
                            x=goal["x"], y=goal["y"], yaw=goal.get("yaw") or 0.0
                        )
                    result = {
                        "ok": True,
                        "state": "finished",
                        "walk_id": walk_id,
                        "fell": False,
                        "pose": state.pose.copy(),
                        "result": {
                            "ok": reached,
                            "legs": [
                                {
                                    "reached": reached,
                                    "position_error": 0.0 if reached else 1.0,
                                    "yaw_error": 0.0,
                                    "time": 1.0,
                                }
                            ],
                        },
                        "log": ["arrived" if reached else "stopped or skipped"],
                    }
                    if state.final_mutator:
                        state.final_mutator(result)
                    return httpx.Response(200, json=result)
            raise AssertionError(
                f"unexpected service request: {request.method} {request.url}"
            )

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        yield SimpleNamespace(
            state=state,
            nav=NavigationClient("http://nav.test", client=client),
            sim=SimulationClient("http://sim.test", client=client),
            handle=response,
        )


@pytest.fixture
def navigation_loopback(navigation_services):
    """Expose the document-shaped service over actual localhost HTTP sockets."""
    from robots.unitree.navigation_client import NavigationClient
    from robots.unitree.simulation_client import SimulationClient

    def handler_for(host):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.dispatch("GET")

            def do_POST(self):
                self.dispatch("POST")

            def dispatch(self, method):
                size = int(self.headers.get("Content-Length", "0"))
                request = httpx.Request(
                    method, f"http://{host}{self.path}", content=self.rfile.read(size)
                )
                response = navigation_services.handle(request)
                self.send_response(response.status_code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(response.content)

            def log_message(self, *args):
                pass

        return Handler

    servers = [
        ThreadingHTTPServer(("127.0.0.1", 0), handler_for(host))
        for host in ("nav.test", "sim.test")
    ]
    threads = [
        threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        for server in servers
    ]
    for thread in threads:
        thread.start()
    nav = NavigationClient(f"http://127.0.0.1:{servers[0].server_port}")
    sim = SimulationClient(f"http://127.0.0.1:{servers[1].server_port}")
    try:
        yield SimpleNamespace(state=navigation_services.state, nav=nav, sim=sim)
    finally:
        nav.close()
        sim.close()
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)
            assert not thread.is_alive()
