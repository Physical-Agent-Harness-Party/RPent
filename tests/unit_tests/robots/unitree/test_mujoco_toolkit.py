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

"""Arrival verification, plan ownership, cancellation, and tool state capture."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from robots.unitree.mujoco_toolkit import UnitreeMujocoToolkit
from rpent.dashboard.events import NullDashboardEventSink
from rpent.memory import MemoryManager


@pytest.fixture
def toolkit(tmp_path, navigation_services):
    result = UnitreeMujocoToolkit(
        nav=navigation_services.nav,
        sim=navigation_services.sim,
        dashboard_events=NullDashboardEventSink(),
        memory=MemoryManager(tmp_path / "memory"),
        state_output_dir=tmp_path / "run",
        navigation_timeout_s=1,
        poll_interval_s=0.01,
    )
    yield result
    result.close()


def plan(toolkit, goals=None):
    result = toolkit.execute_tool("plan_navigation", {"goals": goals or ["广告牌"]})
    assert not result.is_error, result.data
    return result.data["plan_id"]


def test_real_tool_dispatch_preserves_plan_and_waits_for_arrival(
    toolkit, navigation_services
):
    assert not toolkit.execute_tool("list_navigation_goals", {}).is_error
    plan_id = plan(toolkit)
    assert len(toolkit.state.records()) == 1
    saved = toolkit._artifacts / "plans" / f"{plan_id}.json"
    original = json.loads(saved.read_text())
    result = toolkit.execute_tool("follow_navigation", {"plan_id": plan_id})
    assert not result.is_error, result.data
    assert result.data["navigation_completed"] is True
    assert navigation_services.state.submissions == [original]
    assert result.data["pose"]["x"] == 1.0
    assert toolkit.state.get().state["completed_navigation_goals"] == 1
    assert not toolkit.state.get().terminated
    assert not toolkit.execute_tool(
        "finish", {"status": "success", "summary": "arrived"}
    ).is_error
    repeated = toolkit.execute_tool("follow_navigation", {"plan_id": plan_id})
    assert repeated.is_error
    assert len(navigation_services.state.submissions) == 1


def test_model_coordinate_inputs_use_fresh_pose(toolkit, navigation_services):
    navigation_services.state.pose.update(x=0.2, y=0.4, yaw=0.5)
    plan_id = plan(toolkit, [{"x": 2, "y": 3, "yaw": 1.57}])
    result = toolkit.execute_tool("follow_navigation", {"plan_id": plan_id})
    assert not result.is_error, result.data
    sent = navigation_services.state.submissions[0]
    assert sent["start"] == {"x": 0.2, "y": 0.4, "yaw": 0.5}
    assert sent["legs"][0]["goal"]["stand_pose"]["yaw"] == 1.57


@pytest.mark.parametrize("condition", ["map", "fallen", "controller", "simulator"])
def test_invalid_environment_never_submits_motion(
    toolkit, navigation_services, condition
):
    state = navigation_services.state
    if condition == "map":
        state.sim_version = "different"
    elif condition == "fallen":
        state.pose["fallen"] = True
    elif condition == "controller":
        state.other_controllers = ["another controller"]
    else:
        state.simulator_running = False
    result = toolkit.execute_tool("plan_navigation", {"goals": ["广告牌"]})
    assert result.is_error
    assert not state.submissions


def test_multi_goal_plan_is_inspectable_but_cannot_skip_targets(
    toolkit, navigation_services
):
    plan_id = plan(toolkit, ["广告牌", "厕所"])
    result = toolkit.execute_tool("follow_navigation", {"plan_id": plan_id})
    assert result.is_error
    assert "one goal" in result.data["error"]
    assert not navigation_services.state.submissions


def test_changed_map_or_departure_requires_replanning(toolkit, navigation_services):
    plan_id = plan(toolkit)
    navigation_services.state.pose["x"] = 0.5
    assert toolkit.execute_tool("follow_navigation", {"plan_id": plan_id}).is_error
    navigation_services.state.pose["x"] = 0.0
    navigation_services.state.sim_version = "v2"
    assert toolkit.execute_tool("follow_navigation", {"plan_id": plan_id}).is_error
    assert not navigation_services.state.submissions


def test_failed_preview_blocks_following_same_plan(toolkit, navigation_services):
    plan_id = plan(toolkit)
    navigation_services.state.preview_failed = True
    assert toolkit.execute_tool("preview_navigation", {"plan_id": plan_id}).is_error
    assert toolkit.execute_tool("follow_navigation", {"plan_id": plan_id}).is_error
    assert not navigation_services.state.submissions


def test_preview_accepts_documented_top_level_success_without_nested_ok(
    toolkit, monkeypatch
):
    monkeypatch.setattr(
        toolkit._sim,
        "preview",
        lambda *args, **kwargs: {
            "ok": True,
            "result": {"legs": [{"reached": True}]},
            "image": "/files/preview.png",
        },
    )
    plan_id = plan(toolkit)
    assert not toolkit.execute_tool("preview_navigation", {"plan_id": plan_id}).is_error
    assert not toolkit.execute_tool("follow_navigation", {"plan_id": plan_id}).is_error


@pytest.mark.parametrize("error_source", ["reported", "pose"])
@pytest.mark.parametrize(
    ("position_error", "accepted"), [(0.2, True), (0.25, True), (0.251, False)]
)
def test_position_tolerance_checks_reported_error_and_final_pose(
    toolkit, navigation_services, error_source, position_error, accepted
):
    def mutate(result):
        if error_source == "reported":
            result["result"]["legs"][0]["position_error"] = position_error
        else:
            result["pose"]["x"] += position_error

    navigation_services.state.final_mutator = mutate
    result = toolkit.execute_tool("follow_navigation", {"plan_id": plan(toolkit)})
    assert result.is_error == (not accepted), result.data
    assert toolkit.state.get().state["completed_navigation_goals"] == int(accepted)
    if accepted:
        assert result.data["navigation_completed"] is True


@pytest.mark.parametrize("failure", ["skipped", "position", "yaw", "fallen"])
def test_finished_does_not_mean_success(toolkit, navigation_services, failure):
    if failure == "skipped":
        navigation_services.state.failed = True
    else:

        def mutate(result):
            if failure == "position":
                result["pose"]["x"] += 1
            elif failure == "yaw":
                result["result"]["legs"][0]["yaw_error"] = 0.3
            else:
                result["fell"] = True

        navigation_services.state.final_mutator = mutate
    result = toolkit.execute_tool("follow_navigation", {"plan_id": plan(toolkit)})
    assert result.is_error
    assert toolkit.state.get().state["completed_navigation_goals"] == 0
    assert toolkit.execute_tool(
        "finish", {"status": "success", "summary": "done"}
    ).is_error


def test_timed_out_acceptance_is_not_retried_or_blindly_stopped(
    toolkit, navigation_services
):
    navigation_services.state.submit_timeout = True
    result = toolkit.execute_tool("follow_navigation", {"plan_id": plan(toolkit)})
    assert result.is_error
    assert toolkit.state.get().state["submission_uncertain"] is True
    assert len(navigation_services.state.submissions) == 1
    assert not navigation_services.state.stopped
    assert toolkit.execute_tool("stop_navigation", {}).is_error


def test_timeout_requests_stop_and_confirms_finished(toolkit, navigation_services):
    navigation_services.state.hold = True
    toolkit._timeout_s = 0.03
    result = toolkit.execute_tool("follow_navigation", {"plan_id": plan(toolkit)})
    assert result.is_error
    assert "timed out" in result.data["error"]
    assert navigation_services.state.stopped
    assert toolkit.state.get().state["active_walk_id"] is None
    assert len(navigation_services.state.submissions) == 1


def test_cancel_reaches_remote_before_waiting_for_exclusive_tool(
    toolkit, navigation_services
):
    navigation_services.state.hold = True
    plan_id = plan(toolkit)
    with ThreadPoolExecutor(max_workers=1) as pool:
        active = pool.submit(
            toolkit.execute_tool, "follow_navigation", {"plan_id": plan_id}
        )
        assert navigation_services.state.started.wait(1)
        toolkit.cancel_active_and_wait()
        result = active.result(timeout=2)
    assert result.is_error
    assert result.data["interrupted"] is True
    assert navigation_services.state.stopped
    assert toolkit._active_walk_id is None


def test_lost_task_ownership_never_stops_another_walk(toolkit, navigation_services):
    navigation_services.state.hold = True
    navigation_services.state.status_walk_id = "someone_else"
    result = toolkit.execute_tool("follow_navigation", {"plan_id": plan(toolkit)})
    assert result.is_error
    assert "ownership" in result.data["error"]
    assert not navigation_services.state.stopped


def test_status_failure_still_requests_stop_of_known_owned_task(
    toolkit, navigation_services
):
    navigation_services.state.status_unavailable = True
    result = toolkit.execute_tool("follow_navigation", {"plan_id": plan(toolkit)})
    assert result.is_error
    assert "status temporarily unavailable" in result.data["error"]
    assert navigation_services.state.stopped
    assert toolkit.state.get().state["stop_error"]
    assert len(navigation_services.state.submissions) == 1
    # Allow teardown to verify that the accepted stop has taken effect.
    navigation_services.state.status_unavailable = False
