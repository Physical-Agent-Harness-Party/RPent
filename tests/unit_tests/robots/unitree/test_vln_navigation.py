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

"""Exercise VLN wire contracts, captured poses, lease ownership and cancellation."""

from __future__ import annotations

import argparse
import base64
import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from websockets.sync.server import serve

from robots.unitree.frontend_client import FrontendTaskClient
from robots.unitree.http_client import ServiceError
from robots.unitree.mujoco_toolkit import UnitreeMujocoToolkit
from robots.unitree.observation_client import ObservationClient
from robots.unitree.robot_spec import get_robot_spec
from robots.unitree.simulation_client import SimulationClient
from robots.unitree.vln_client import VlnClient, validate_vln_endpoint
from robots.unitree.vln_navigation import VlnNavigation
from rpent.dashboard.events import NullDashboardEventSink
from rpent.memory import MemoryManager
from rpent.tools.toolkit import ToolCancelled

IDENTITY = {"map_id": "test_room", "map_version": "v1"}


class FakeSimulation:
    def __init__(self):
        self.pose = {"x": 0.0, "y": 0.0, "yaw": 0.0, "fallen": False}
        self.control_id = None
        self.requests = []
        self.stops = []
        self.uncertain_start = False
        self.fail_stop = False
        self.route_active = False

    def health(self):
        return {
            "ok": True,
            **IDENTITY,
            "simulator_running": True,
            "control_mode": "route"
            if self.route_active
            else "trajectory"
            if self.control_id
            else "idle",
            "other_controllers": [],
        }

    def get_pose(self):
        return copy.deepcopy(self.pose)

    def get_control(self, control_id):
        if control_id != self.control_id:
            raise ServiceError("control_id changed")
        return {
            "ok": True,
            "control_id": control_id,
            "mode": "trajectory",
            "state": "running",
            "ready": True,
            "stop_confirmed": False,
            "pose": self.get_pose(),
        }

    def start_control(self, timeout_s):
        self.control_id = "control_owned"
        if self.uncertain_start:
            raise ServiceError("acceptance acknowledgement was lost")
        return self.get_control(self.control_id)

    def submit_trajectory(self, payload):
        if payload["control_id"] != self.control_id:
            raise ServiceError("control_id changed")
        self.requests.append(copy.deepcopy(payload))
        return self.get_control(self.control_id)

    def stop_control(self, control_id):
        if control_id != self.control_id:
            raise ServiceError("control_id changed")
        if self.fail_stop:
            raise ServiceError("could not confirm stop")
        self.stops.append(control_id)
        result = self.get_control(control_id)
        result.update(state="finished", ready=False, stop_confirmed=True)
        self.control_id = None
        return result

    def close(self):
        pass


class FakeObservation:
    def observe(self):
        return {
            "ok": True,
            **IDENTITY,
            "stamp_ns": time.time_ns(),
            "base_pose": {"x": 10.0, "y": 20.0, "yaw": 0.5},
            "image": {
                "encoding": "jpeg",
                "data": base64.b64encode(b"test-frame").decode(),
            },
        }

    def close(self):
        pass


class FakeModel:
    timeout_s = 1.0

    def __init__(self, *, stop_flags=(False, True), callback=None):
        self.flags = stop_flags
        self.calls = 0
        self.callback = callback
        self.started = threading.Event()
        self.closed = threading.Event()

    def start_episode(self, episode):
        self.closed.clear()

    def predict(self, image, instruction, *, timeout_s=None):
        self.calls += 1
        self.started.set()
        if self.callback is not None:
            self.callback(self)
        if self.closed.is_set():
            raise ServiceError("connection closed")
        return {
            "seq": self.calls,
            "stop": self.flags[min(self.calls - 1, len(self.flags) - 1)],
            "actions": {"actions": [[0.2, 0.0, 0.0], [0.4, 0.0, 0.0]]},
            "latency_ms": 1.0,
        }

    def close(self):
        self.closed.set()


def controller(tmp_path, *, sim=None, model=None, timeout=2.0):
    return VlnNavigation(
        sim=sim or FakeSimulation(),
        observation=FakeObservation(),
        model=model or FakeModel(),
        artifacts=tmp_path / "vln",
        period_s=0.001,
        trajectory_timeout_s=timeout,
    )


def run(control, *, goal=None):
    return control.run(
        "Go to the doorway",
        timeout_s=3.0,
        expected_goal=goal,
        check_cancelled=lambda: None,
    )


def test_closed_loop_preserves_capture_pose_and_requires_spatial_verification(tmp_path):
    control = controller(tmp_path)
    result = run(control)
    assert result["state"] == "verification_required"
    assert result["model_stop"] and result["stop_confirmed"]
    assert not result["goal_verified"] and not control.active
    sent = control.sim.requests
    assert [p["seq"] for p in sent] == [1, 2, 3]
    assert sent[0]["capture_pose"] == {"x": 10.0, "y": 20.0, "yaw": 0.5}
    assert sent[0]["stamp_ns"] < sent[1]["stamp_ns"]
    assert all("vx" not in p and "wz" not in p for p in sent)
    assert len(control.sim.stops) == 1
    assert (
        json.loads((tmp_path / "vln/latest.json").read_text())["state"]
        == "verification_required"
    )


def test_known_map_goal_is_checked_after_confirmed_stopping(tmp_path):
    control = controller(tmp_path)
    result = run(control, goal={"x": 0.0, "y": 0.0})
    assert result["goal_verified"] and result["navigation_completed"]
    assert result["stop_confirmed"] and result["state"] == "finished"
    assert result["validation_scope"] == "map_pose_and_simulator_stop"


def test_model_stop_outside_expected_goal_does_not_claim_arrival(tmp_path):
    control = controller(tmp_path)
    with pytest.raises(ServiceError, match="outside the expected"):
        run(control, goal={"x": 2.0, "y": 0.0})
    assert control.snapshot()["state"] == "failed"
    assert control.snapshot()["stop_confirmed"] and not control.active


def test_expired_predictions_are_discarded_without_retimestamping(tmp_path):
    model = FakeModel(callback=lambda model: time.sleep(0.12))
    control = controller(tmp_path, model=model, timeout=0.2)
    with pytest.raises(ServiceError, match="three VLN predictions expired"):
        run(control)
    assert not control.sim.requests
    assert control.snapshot()["stale_predictions"] == 3
    assert control.snapshot()["stop_confirmed"]


def test_uncertain_start_is_not_retried_or_stopped_without_an_id(tmp_path):
    sim = FakeSimulation()
    sim.uncertain_start = True
    control = controller(tmp_path, sim=sim)
    with pytest.raises(ServiceError, match="acknowledgement"):
        run(control)
    assert control.snapshot()["submission_uncertain"] and control.active
    assert sim.stops == []
    with pytest.raises(ServiceError, match="active or uncertain"):
        run(control)


def test_replaced_lease_is_never_stopped_by_the_old_owner(tmp_path):
    sim = FakeSimulation()
    control = controller(
        tmp_path,
        sim=sim,
        model=FakeModel(
            callback=lambda model: setattr(sim, "control_id", "control_foreign")
        ),
    )
    with pytest.raises(ServiceError, match="stop was not confirmed"):
        run(control)
    assert sim.control_id == "control_foreign" and not sim.stops
    assert control.snapshot()["active_control_id"] == "control_owned"


def test_pending_prediction_is_cancelled_and_owned_control_is_released(tmp_path):
    model = FakeModel(callback=lambda model: model.closed.wait(2.0))
    control = controller(tmp_path, model=model)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(run, control)
        assert model.started.wait(1.0)
        control.cancel()
        with pytest.raises(ToolCancelled):
            future.result(timeout=2)
    assert not control.active and control.snapshot()["state"] == "cancelled"
    assert control.snapshot()["stop_confirmed"] and not control.sim.requests


def test_stop_failure_keeps_control_blocked(tmp_path):
    sim = FakeSimulation()
    sim.fail_stop = True
    control = controller(tmp_path, sim=sim, model=FakeModel(stop_flags=(True,)))
    with pytest.raises(ServiceError, match="stop was not confirmed"):
        run(control)
    assert control.active and control.snapshot()["stop_error"]


def test_active_route_cannot_create_vln_control(tmp_path):
    sim = FakeSimulation()
    sim.route_active = True
    control = controller(tmp_path, sim=sim)
    with pytest.raises(ServiceError, match="must be idle"):
        run(control)
    assert sim.control_id is None and not sim.requests


def test_mpc_failure_stops_the_episode_before_another_prediction(tmp_path):
    sim = FakeSimulation()
    original = sim.get_control

    def failed_tracker(control_id):
        return original(control_id) | {
            "tracking": {"state": "solver_error", "error": "solver failed"}
        }

    sim.get_control = failed_tracker
    control = controller(tmp_path, sim=sim)
    with pytest.raises(ServiceError, match="MPC failed"):
        run(control)
    assert control.model.calls == 0 and control.snapshot()["stop_confirmed"]


@pytest.fixture
def websocket_model():
    state = SimpleNamespace(messages=[], bad_sequence=False, missing_stop=False)

    def handler(connection):
        for message in connection:
            request = json.loads(message)
            state.messages.append(request)
            data = {"rc": 0}
            if request["action"] == "next":
                data.update(
                    seq=request["data"]["seq"] + int(state.bad_sequence),
                    stop=False,
                    actions={"actions": [[0.3, 0.0, 0.0]]},
                )
                if state.missing_stop:
                    data.pop("stop")
            connection.send(json.dumps({"action": request["action"], "data": data}))

    with serve(handler, "127.0.0.1", 0) as server:
        state.endpoint = f"ws://127.0.0.1:{server.socket.getsockname()[1]}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield state
        server.shutdown()
        thread.join(timeout=3)


def test_websocket_login_reset_and_sequence_contract(websocket_model):
    client = VlnClient(websocket_model.endpoint)
    try:
        client.start_episode("test-episode")
        assert client.predict("jpeg", "go")["seq"] == 1
        assert client.predict("jpeg", "go")["seq"] == 2
        assert [m["action"] for m in websocket_model.messages] == [
            "login",
            "reset",
            "next",
            "next",
        ]
    finally:
        client.close()


@pytest.mark.parametrize("attribute", ["bad_sequence", "missing_stop"])
def test_invalid_model_reply_closes_connection_without_replay(
    websocket_model, attribute
):
    setattr(websocket_model, attribute, True)
    client = VlnClient(websocket_model.endpoint)
    client.start_episode("test-episode")
    with pytest.raises(ServiceError):
        client.predict("jpeg", "go")
    assert len([m for m in websocket_model.messages if m["action"] == "next"]) == 1
    with pytest.raises(ServiceError, match="not connected"):
        client.predict("jpeg", "go")


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:8050",
        "ws://user:secret@localhost:8050",
        "ws://localhost:8050?a=b",
    ],
)
def test_invalid_model_endpoints_are_rejected_without_network(endpoint):
    with pytest.raises(ValueError):
        validate_vln_endpoint(endpoint)


def test_observation_http_client_requires_capture_pose():
    observation = FakeObservation().observe()
    observation.pop("base_pose")
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=observation)
        )
    ) as http:
        client = ObservationClient("http://camera.test", client=http)
        with pytest.raises(ServiceError, match="base_pose"):
            client.observe()


def test_control_http_client_forwards_waypoints_and_uses_owned_stop_id():
    requests = []

    def handler(request):
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(
            200,
            json={
                "ok": True,
                "control_id": "control_1",
                "mode": "trajectory",
                "state": "finished" if request.url.path.endswith("stop") else "running",
                "ready": not request.url.path.endswith("stop"),
                "stop_confirmed": request.url.path.endswith("stop"),
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = SimulationClient("http://sim.test", client=http)
        accepted = client.start_control()
        client.submit_trajectory(
            {"control_id": accepted["control_id"], "seq": 1, "stop": True}
        )
        client.stop_control(accepted["control_id"])
    assert requests[0] == ("/control/start", {"trajectory_timeout_s": 2.0})
    assert requests[-1] == ("/control/stop", {"control_id": "control_1"})


def test_toolkit_exposes_vln_and_cannot_finish_unverified_success(
    tmp_path, navigation_services
):
    sim = FakeSimulation()
    toolkit = UnitreeMujocoToolkit(
        nav=navigation_services.nav,
        sim=sim,
        vln=FakeModel(),
        observation=FakeObservation(),
        dashboard_events=NullDashboardEventSink(),
        memory=MemoryManager(tmp_path / "memory"),
        state_output_dir=tmp_path / "run",
    )
    try:
        result = toolkit.execute_tool(
            "run_vln_navigation", {"instruction": "go to the doorway"}
        )
        assert not result.is_error, result.data
        assert result.data["state"] == "verification_required"
        assert toolkit.execute_tool(
            "finish", {"status": "success", "summary": "arrived"}
        ).is_error
        assert toolkit.state.get().state["vln"]["stop_confirmed"]
    finally:
        toolkit.close()


def test_frontend_vln_callback_correlates_the_registered_subtask(
    tmp_path, navigation_services
):
    requests = []

    def handler(request):
        data = json.loads(request.content)
        requests.append((request.url.path, data))
        return httpx.Response(
            200,
            json={"items": [{"id": "sub-1"}]}
            if request.url.path == "/api/rpend/task-1"
            else {"ok": True},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        frontend = FrontendTaskClient(
            "http://frontend.test", "task-1", output_dir=tmp_path, client=http
        )
        toolkit = UnitreeMujocoToolkit(
            nav=navigation_services.nav,
            sim=FakeSimulation(),
            vln=FakeModel(),
            observation=FakeObservation(),
            frontend=frontend,
            dashboard_events=NullDashboardEventSink(),
            memory=MemoryManager(tmp_path / "memory"),
            state_output_dir=tmp_path / "run",
        )
        try:
            toolkit.execute_tool(
                "register_navigation_subtasks", {"commands": ["hold this map position"]}
            )
            result = toolkit.execute_tool(
                "run_vln_navigation",
                {
                    "instruction": "stop here",
                    "expected_goal": {"x": 0.0, "y": 0.0},
                    "sub_task_id": "sub-1",
                },
            )
            assert not result.is_error, result.data
            assert result.data["goal_verified"]
            assert [r[1]["data"]["stage"] for r in requests[1:]] == [
                "vln_started",
                "vln_finished",
            ]
            assert requests[-1][1]["data"]["navigation_completed"]
            assert not toolkit.execute_tool(
                "finish", {"status": "success", "summary": "verified"}
            ).is_error
        finally:
            toolkit.close()


def test_frontend_start_callback_failure_releases_the_owned_lease(
    tmp_path, navigation_services
):
    def handler(request):
        if request.url.path == "/api/rpend/task-1":
            return httpx.Response(200, json={"items": [{"id": "sub-1"}]})
        return httpx.Response(503, json={"error": "callback unavailable"})

    sim = FakeSimulation()
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        frontend = FrontendTaskClient(
            "http://frontend.test", "task-1", output_dir=tmp_path, client=http
        )
        toolkit = UnitreeMujocoToolkit(
            nav=navigation_services.nav,
            sim=sim,
            vln=FakeModel(),
            observation=FakeObservation(),
            frontend=frontend,
            dashboard_events=NullDashboardEventSink(),
            memory=MemoryManager(tmp_path / "memory"),
            state_output_dir=tmp_path / "run",
        )
        try:
            toolkit.execute_tool(
                "register_navigation_subtasks", {"commands": ["VLN test"]}
            )
            result = toolkit.execute_tool(
                "run_vln_navigation", {"instruction": "go", "sub_task_id": "sub-1"}
            )
            assert result.is_error and "callback unavailable" in result.data["error"]
            assert sim.control_id is None and sim.stops == ["control_owned"]
            assert not sim.requests
        finally:
            toolkit.close()


def test_cli_vln_configuration_registers_optional_tools_without_gpu_imports(tmp_path):
    parser = argparse.ArgumentParser()
    for name in ("output-dir", "memory-dir"):
        parser.add_argument("--" + name, default=None)
    get_robot_spec().add_cli_args(parser, False)
    args = parser.parse_args(
        [
            "--task",
            "go",
            "--unitree-mode",
            "mujoco",
            "--vln-endpoint",
            "ws://127.0.0.1:8050",
            "--observation-endpoint",
            "http://127.0.0.1:8771",
        ]
    )
    config = get_robot_spec().parse_config(args)
    assert config.prompt_vars["vln_endpoint"] == "ws://127.0.0.1:8050"
    assert "run_vln_navigation" in get_robot_spec().prompts.render(
        "system", variables=config.prompt_vars
    )
    args.observation_endpoint = None
    with pytest.raises(ValueError, match="provided together"):
        get_robot_spec().parse_config(args)
