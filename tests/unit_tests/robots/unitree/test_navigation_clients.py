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

"""REST payloads and error handling for navigation and simulation services."""

import httpx
import pytest
from pydantic import ValidationError

from robots.unitree.http_client import ServiceError, validate_endpoint
from robots.unitree.navigation_client import CoordinateGoal, NavigationClient


def test_map_coordinates_goals_and_direct_response_are_preserved(navigation_services):
    nav = navigation_services.nav
    assert nav.list_waypoints()["waypoints"][0]["name"] == "广告牌"
    plan = nav.plan(
        {"x": 0, "y": 0, "yaw": 0},
        ["广告牌", CoordinateGoal(x=2, y=3, yaw=1.57)],
        step=0.2,
    )
    assert plan["goals"] == ["wp_01", "xy"]
    request = navigation_services.state.requests[-1]
    assert request[:3] == ("POST", "nav.test", "/plan")
    assert request[3] == {
        "start": {"x": 0.0, "y": 0.0, "yaw": 0.0},
        "goals": ["广告牌", {"x": 2.0, "y": 3.0, "yaw": 1.57}],
        "step": 0.2,
    }
    assert "method" not in request[3]


@pytest.mark.parametrize(
    "goal",
    [{"x": True, "y": 0}, {"x": float("inf"), "y": 0}, {"x": 1, "y": 0, "speed": 1}],
)
def test_invalid_coordinate_goal_is_rejected_before_http(navigation_services, goal):
    with pytest.raises(ValidationError):
        navigation_services.nav.plan({"x": 0, "y": 0}, [goal])
    assert navigation_services.state.requests == []


def test_http_400_retains_planning_error(navigation_services):
    with pytest.raises(ServiceError, match="unknown destination"):
        navigation_services.nav.plan({"x": 0, "y": 0}, ["不存在"])


@pytest.mark.parametrize("payload", [{"ok": "true"}, {"ok": 1}, [], {"ok": True}])
def test_malformed_health_is_not_accepted(payload):
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        nav = NavigationClient("http://nav.test", client=client)
        with pytest.raises(ServiceError):
            nav.health()


def test_non_json_error_is_reported_without_retry():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(502, text="bad gateway")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ServiceError, match="not valid JSON"):
            NavigationClient("http://nav.test", client=client).health()
    assert len(calls) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("frame", "body"),
        ("units", {"length": "meter", "angle": "degree"}),
        ("legs", []),
    ],
)
def test_invalid_route_cannot_be_saved(navigation_services, field, value):
    navigation_services.state.plan_mutator = lambda result: result.update(
        {field: value}
    )
    with pytest.raises(ServiceError):
        navigation_services.nav.plan({"x": 0, "y": 0}, ["广告牌"])


@pytest.mark.parametrize(
    "endpoint",
    [
        "socket://localhost:1",
        "http://",
        "http://user:password@localhost",
        "http://localhost?token=x",
        "http://localhost:99999",
    ],
)
def test_service_endpoints_are_validated_without_connecting(endpoint):
    with pytest.raises(ValueError):
        validate_endpoint(endpoint)


def test_borrowed_http_client_is_not_closed():
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"ok": True, "map_id": "m", "map_version": "v"}
            )
        )
    ) as client:
        nav = NavigationClient("http://nav.test", client=client)
        nav.close()
        assert not client.is_closed
        assert nav.health()["map_id"] == "m"
