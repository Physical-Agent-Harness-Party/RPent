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

"""Offline model/tool/callback correlation and the two-argument launch script."""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from robots.unitree.frontend_client import FrontendTaskClient
from robots.unitree.http_client import ServiceError
from robots.unitree.mujoco_toolkit import UnitreeMujocoToolkit
from robots.unitree.navigation_client import NavigationClient
from robots.unitree.robot_spec import get_robot_spec
from robots.unitree.simulation_client import SimulationClient
from rpent.dashboard.events import NullDashboardEventSink
from rpent.memory import MemoryManager

REPO = Path(__file__).resolve().parents[4]


class FrontendNavigationTests(unittest.TestCase):
    """Exercise real HTTP clients and tools without network or a model service."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.requests = []
        self.pose = {"x": 0.0, "y": 0.0, "yaw": 0.0, "fallen": False}
        self.identity = {"map_id": "room", "map_version": "v1"}
        self.plan = None
        self.started = 0
        self.stopped = False
        self.failed_stage = None
        self.failed_plan = False
        self.failed_arrival = False
        self.registration_response = None
        self.registration_timeout = False
        self.goals = ["广告牌", "厕所通道", "母婴室"]
        self.http = httpx.Client(transport=httpx.MockTransport(self.respond))
        self.addCleanup(self.http.close)
        self.frontend = self.new_frontend()
        self.toolkit = UnitreeMujocoToolkit(
            nav=NavigationClient("http://nav.test", client=self.http),
            sim=SimulationClient("http://sim.test", client=self.http),
            frontend=self.frontend,
            dashboard_events=NullDashboardEventSink(),
            memory=MemoryManager(self.root / "memory"),
            state_output_dir=self.root / "run",
            poll_interval_s=0.001,
        )
        self.addCleanup(self.toolkit.close)
        output_patch = patch("rpent.utils.logging._output_dir", self.root / "run")
        output_patch.start()
        self.addCleanup(output_patch.stop)

    def new_frontend(self, **kwargs):
        return FrontendTaskClient(
            "http://frontend.test",
            "task-123",
            output_dir=self.root,
            client=self.http,
            **kwargs,
        )

    def waypoint(self, name):
        return {
            "id": f"wp_{self.goals.index(name) + 1}",
            "name": name,
            "stand_pose": {"x": float(self.goals.index(name) + 1), "y": 0.0},
        }

    def respond(self, request):
        host, path = request.url.host, request.url.path
        body = json.loads(request.content) if request.content else None
        self.requests.append((host, request.method, path, body))
        if host == "frontend.test":
            if path == "/api/rpend/task-123":
                if self.registration_timeout:
                    raise httpx.ReadTimeout(
                        "registration acknowledgement lost", request=request
                    )
                items = body["sub_tasks"] if isinstance(body, dict) else body
                response = (
                    self.registration_response
                    if self.registration_response is not None
                    else {
                        "items": [{"id": f"sub-{i}"} for i in range(1, len(items) + 1)],
                    }
                )
                return httpx.Response(200, json=response)
            self.assertEqual(body["source_type"], "sil")
            if body["data"]["stage"] == self.failed_stage:
                return httpx.Response(503, json={"error": "callback unavailable"})
            return httpx.Response(201, json={"id": "dataset-test"})
        if path == "/health":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    **self.identity,
                    "simulator_running": True,
                    "walk": {"state": "finished"},
                },
            )
        if path == "/pose":
            return httpx.Response(200, json={"ok": True, **self.pose})
        if path == "/waypoints":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    **self.identity,
                    "waypoints": [self.waypoint(name) for name in self.goals],
                },
            )
        if path == "/plan":
            if self.failed_plan:
                return httpx.Response(400, json={"ok": False, "error": "no path"})
            goal = self.waypoint(body["goals"][0])
            start, target = body["start"], goal["stand_pose"]
            self.plan = {
                "ok": True,
                **self.identity,
                "frame": "map",
                "units": {"length": "meter", "angle": "radian"},
                "start": start,
                "total_length": abs(target["x"] - start["x"]),
                "legs": [
                    {
                        "goal": goal,
                        "length": abs(target["x"] - start["x"]),
                        "path": [[start["x"], start["y"]], [target["x"], target["y"]]],
                        "headings": [0.0],
                    }
                ],
            }
            return httpx.Response(200, json=self.plan)
        if path == "/walk/stop":
            self.stopped = True
            return httpx.Response(200, json={"ok": True})
        if path == "/walk" and request.method == "POST":
            self.started += 1
            return httpx.Response(
                200, json={"ok": True, "walk_id": f"walk-{self.started}"}
            )
        if path == "/walk":
            if self.failed_stage == "walk_accepted" and not self.stopped:
                return httpx.Response(200, json={"ok": True, "state": "running"})
            self.pose.update(self.plan["legs"][0]["goal"]["stand_pose"])
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "state": "finished",
                    "fell": False,
                    "pose": self.pose,
                    "result": {
                        "ok": True,
                        "legs": [
                            {
                                "reached": not self.failed_arrival,
                                "position_error": 0.0,
                            }
                        ],
                    },
                },
            )
        raise AssertionError(f"Unexpected request {request.method} {request.url}")

    def execute(self, name, **kwargs):
        return self.toolkit.execute_tool(name, kwargs)

    def register(self, count=1):
        result = self.execute(
            "register_navigation_subtasks", commands=self.goals[:count]
        )
        self.assertFalse(result.is_error, result.data)
        return result.data["sub_tasks"]

    def create_plan(self, sub_task_id="sub-1", goal="广告牌"):
        result = self.execute("plan_navigation", goals=[goal], sub_task_id=sub_task_id)
        self.assertFalse(result.is_error, result.data)
        return result.data["plan_id"]

    def callbacks(self):
        return [
            body["data"]
            for host, _, _, body in self.requests
            if host == "frontend.test" and isinstance(body, dict) and "data" in body
        ]

    def test_model_decomposes_three_destinations_then_reports_real_tool_results(self):
        from pydantic_ai.messages import ToolReturnPart
        from pydantic_ai.models.function import DeltaToolCall, FunctionModel

        from rpent.planner.api_loop import ApiAgentLoop

        calls = []

        async def stream(messages, info):
            self.assertIn(
                "register_navigation_subtasks",
                {tool.name for tool in info.function_tools},
            )
            index = len(calls)
            calls.append(index)
            if index == 0:
                name, arguments = "list_navigation_goals", {}
            elif index == 1:
                name, arguments = (
                    "register_navigation_subtasks",
                    {"commands": self.goals},
                )
            elif 2 <= index < 8:
                step = (index - 2) // 2
                if index % 2 == 0:
                    name, arguments = (
                        "plan_navigation",
                        {
                            "goals": [self.goals[step]],
                            "sub_task_id": f"sub-{step + 1}",
                        },
                    )
                else:
                    returns = [
                        part
                        for message in messages
                        for part in message.parts
                        if isinstance(part, ToolReturnPart)
                        and part.tool_name == "plan_navigation"
                    ]
                    content = returns[-1].content
                    data = json.loads(
                        content[0] if isinstance(content, list) else content
                    )
                    name, arguments = "follow_navigation", {"plan_id": data["plan_id"]}
            else:
                name, arguments = (
                    "finish",
                    {"status": "success", "summary": "三个地点均已到达"},
                )
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(arguments))}

        spec = get_robot_spec()
        system = spec.prompts.render(
            "system",
            variables={
                "unitree_mode": "mujoco",
                "frontend_endpoint": "http://frontend.test",
            },
        )
        self.assertIn("FRONTEND TASK REPORTING", system)
        planner = ApiAgentLoop(
            model=FunctionModel(stream_function=stream),
            dashboard_events=NullDashboardEventSink(),
            timeout_s=10,
        )

        async def drive_model():
            # Restricted sandboxes can forbid asyncio's socket-pair wakeups.
            # Keep a timer until asyncio.run closes the loop, including executor
            # shutdown, so real thread-backed tool completions are consumed.
            loop = asyncio.get_running_loop()

            def tick():
                loop.call_later(0.01, tick)

            tick()
            return await planner._solve(
                system,
                "先去广告牌，再去厕所通道，再去母婴室",
                self.toolkit,
                16,
                None,
            )

        result = asyncio.run(drive_model())
        self.assertIsNone(result.error)
        self.assertEqual(result.finish_result["status"], "success")
        self.assertEqual(
            self.toolkit.state.get().state["completed_navigation_goals"], 3
        )
        self.assertEqual(self.started, 3)
        events = self.callbacks()
        self.assertEqual(
            [event["stage"] for event in events],
            ["astar", "walk_accepted", "walk_finished"] * 3,
        )
        for index in range(3):
            chunk = events[index * 3 : (index + 1) * 3]
            self.assertTrue(
                all(
                    event["task_id"] == "task-123"
                    and event["sub_task_id"] == f"sub-{index + 1}"
                    for event in chunk
                )
            )
            self.assertTrue(chunk[-1]["navigation_completed"])
        plans = [
            body
            for host, _, path, body in self.requests
            if host == "nav.test" and path == "/plan"
        ]
        walks = [
            body
            for host, method, path, body in self.requests
            if host == "sim.test" and method == "POST" and path == "/walk"
        ]
        self.assertTrue(all(set(body) == {"start", "goals"} for body in plans))
        self.assertEqual(
            walks, [event["result"] for event in events if event["stage"] == "astar"]
        )
        mapping = json.loads((self.root / "frontend/subtasks.json").read_text())
        self.assertEqual(
            [task["sub_task_id"] for task in mapping["sub_tasks"]],
            ["sub-1", "sub-2", "sub-3"],
        )

    def test_unregistered_or_wrong_order_cannot_plan_or_finish_success(self):
        self.assertTrue(self.execute("plan_navigation", goals=["广告牌"]).is_error)
        self.assertEqual(self.requests, [])
        self.register(2)
        self.assertTrue(
            self.execute(
                "plan_navigation", goals=["厕所通道"], sub_task_id="sub-2"
            ).is_error
        )
        self.assertTrue(
            self.execute("finish", status="success", summary="done").is_error
        )
        self.assertEqual(self.started, 0)

    def test_registration_is_not_repeated(self):
        self.register()
        self.assertTrue(
            self.execute("register_navigation_subtasks", commands=["广告牌"]).is_error
        )
        self.assertEqual(len(self.requests), 1)

    def test_invalid_registration_mapping_blocks_movement(self):
        for response in [
            {"task_id": "wrong", "sub_tasks": [{"1": "a"}]},
            {"task_id": "task-123", "sub_tasks": [{"2": "a"}]},
            {"task_id": "task-123", "sub_tasks": []},
            {"task_id": "task-123", "sub_tasks": [{"1": "../a"}]},
            {"task_id": "task-123", "sub_tasks": [{"1": "a"}, {"2": "a"}]},
        ]:
            with self.subTest(response=response):
                self.registration_response = response
                frontend = self.new_frontend()
                with self.assertRaises(ServiceError):
                    frontend.register_subtasks(
                        ["广告牌", "厕所通道"]
                        if len(response["sub_tasks"]) == 2
                        else ["广告牌"]
                    )
                self.assertIsNotNone(frontend.error)
        self.assertEqual(self.started, 0)

    def test_array_contract_and_lost_registration_acknowledgement(self):
        frontend = self.new_frontend(subtasks_format="array")
        frontend.register_subtasks(["去广告牌"])
        self.assertEqual(self.requests[-1][3], [{"sub_task_1": "去广告牌"}])
        self.registration_timeout = True
        frontend = self.new_frontend()
        before = len(self.requests)
        for _ in range(2):
            with self.assertRaises(ServiceError):
                frontend.register_subtasks(["去广告牌"])
        self.assertEqual(len(self.requests), before + 1)
        records = [
            json.loads(path.read_text())
            for path in (self.root / "frontend/requests").glob("*.json")
        ]
        self.assertTrue(
            any(not record["delivered"] and "error" in record for record in records)
        )

    def test_items_response_validates_every_id_before_navigation(self):
        for response in (
            {"items": []},
            {"items": [{"id": "same"}, {"id": "same"}]},
            {"items": [{"id": "valid"}, {}]},
            {"items": [{"id": "valid"}, {"id": "../invalid"}]},
            {"task_id": "wrong-task", "items": [{"id": "a"}, {"id": "b"}]},
        ):
            with self.subTest(response=response):
                self.registration_response = response
                frontend = self.new_frontend()
                with self.assertRaises(ServiceError):
                    frontend.register_subtasks(["广告牌", "厕所通道"])
                self.assertIsNotNone(frontend.error)
        self.assertEqual(self.started, 0)

    def test_legacy_object_mapping_remains_supported(self):
        self.registration_response = {
            "task_id": "task-123",
            "sub_tasks": [{"2": "second"}, {"1": "first"}],
        }
        frontend = self.new_frontend(subtasks_format="object")
        tasks = frontend.register_subtasks(["广告牌", "厕所通道"])
        self.assertEqual([task["sub_task_id"] for task in tasks], ["first", "second"])
        self.assertEqual(
            self.requests[-1][3],
            {
                "sub_tasks": [
                    {"sub_task_1": "广告牌"},
                    {"sub_task_2": "厕所通道"},
                ]
            },
        )

    def test_repeat_suffix_reuses_ids_but_submits_only_fresh_plans(self):
        result = self.execute(
            "register_navigation_subtasks", commands=self.goals, repeat_from=2
        )
        self.assertFalse(result.is_error, result.data)
        for index in (0, 1, 2, 1, 2):
            plan_id = self.create_plan(f"sub-{index + 1}", self.goals[index])
            result = self.execute("follow_navigation", plan_id=plan_id)
            self.assertFalse(result.is_error, result.data)
            repeated = self.execute("follow_navigation", plan_id=plan_id)
            self.assertTrue(repeated.is_error)
        self.assertEqual(self.started, 5)
        self.assertEqual(result.data["next_sub_task_id"], "sub-2")
        registrations = [
            path
            for host, _, path, _ in self.requests
            if host == "frontend.test" and path == "/api/rpend/task-123"
        ]
        self.assertEqual(len(registrations), 1)
        self.assertTrue(
            self.execute("finish", status="success", summary="visited once").is_error
        )

    def test_invalid_repeat_index_is_rejected_before_registration(self):
        for repeat_from in (0, 3, -1):
            with self.subTest(repeat_from=repeat_from):
                self.assertTrue(
                    self.execute(
                        "register_navigation_subtasks",
                        commands=self.goals[:2],
                        repeat_from=repeat_from,
                    ).is_error
                )
        self.assertEqual(self.requests, [])

    def test_continuous_patrol_reaches_model_budget_without_finishing_early(self):
        from pydantic_ai.messages import ToolReturnPart
        from pydantic_ai.models.function import DeltaToolCall, FunctionModel

        from rpent.planner.api_loop import ApiAgentLoop

        calls = []

        async def stream(messages, info):
            index = len(calls)
            calls.append(index)
            if index == 0:
                name, arguments = "list_navigation_goals", {}
            elif index == 1:
                name, arguments = (
                    "register_navigation_subtasks",
                    {
                        "commands": self.goals[:2],
                        "repeat_from": 1,
                    },
                )
            elif index % 2 == 0:
                goal_index = ((index - 2) // 2) % 2
                name, arguments = (
                    "plan_navigation",
                    {
                        "goals": [self.goals[goal_index]],
                        "sub_task_id": f"sub-{goal_index + 1}",
                    },
                )
            else:
                returns = [
                    part
                    for message in messages
                    for part in message.parts
                    if isinstance(part, ToolReturnPart)
                    and part.tool_name == "plan_navigation"
                ]
                content = returns[-1].content
                planned = json.loads(
                    content[0] if isinstance(content, list) else content
                )
                name, arguments = "follow_navigation", {"plan_id": planned["plan_id"]}
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(arguments))}

        planner = ApiAgentLoop(
            model=FunctionModel(stream_function=stream),
            dashboard_events=NullDashboardEventSink(),
            timeout_s=10,
        )
        system = get_robot_spec().prompts.render(
            "system",
            variables={
                "unitree_mode": "mujoco",
                "frontend_endpoint": "http://frontend.test",
            },
        )

        async def drive():
            loop = asyncio.get_running_loop()

            def tick():
                loop.call_later(0.01, tick)

            tick()
            return await planner._solve(
                system,
                "先去广告牌，再去厕所通道，之后持续往返，由max-turns停止。",
                self.toolkit,
                10,
                None,
            )

        result = asyncio.run(drive())
        self.assertIsNone(result.error)
        self.assertIsNone(result.finish_result)
        self.assertEqual(result.stats["turns_used"], 10)
        self.assertEqual(self.started, 4)
        events = [
            event for event in self.callbacks() if event["stage"] == "walk_finished"
        ]
        self.assertEqual(
            [event["sub_task_id"] for event in events],
            ["sub-1", "sub-2", "sub-1", "sub-2"],
        )
        self.assertEqual(len({event["plan_id"] for event in events}), 4)

    def test_astar_callback_failure_blocks_following_and_replanning(self):
        self.register()
        self.failed_stage = "astar"
        result = self.execute("plan_navigation", goals=["广告牌"], sub_task_id="sub-1")
        self.assertTrue(result.is_error)
        plan_id = next(iter(self.toolkit._plans))
        self.assertTrue(self.execute("follow_navigation", plan_id=plan_id).is_error)
        self.assertTrue(
            self.execute(
                "plan_navigation", goals=["广告牌"], sub_task_id="sub-1"
            ).is_error
        )
        self.assertEqual(self.started, 0)

    def test_callback_failure_after_acceptance_stops_owned_walk(self):
        self.register()
        plan_id = self.create_plan()
        self.failed_stage = "walk_accepted"
        self.assertTrue(self.execute("follow_navigation", plan_id=plan_id).is_error)
        self.assertTrue(self.stopped)
        self.assertTrue(self.execute("follow_navigation", plan_id=plan_id).is_error)
        self.assertEqual(self.started, 1)

    def test_callback_failure_after_arrival_cannot_repeat_motion(self):
        self.register()
        plan_id = self.create_plan()
        self.failed_stage = "walk_finished"
        self.assertTrue(self.execute("follow_navigation", plan_id=plan_id).is_error)
        self.assertEqual(self.toolkit._completed, 1)
        self.assertTrue(self.execute("follow_navigation", plan_id=plan_id).is_error)
        self.assertEqual(self.started, 1)
        self.assertTrue(
            self.execute("finish", status="success", summary="done").is_error
        )

    def test_planning_and_failed_arrival_are_reported_as_failures(self):
        self.register()
        self.failed_plan = True
        self.assertTrue(
            self.execute(
                "plan_navigation", goals=["广告牌"], sub_task_id="sub-1"
            ).is_error
        )
        self.assertIn("no path", self.callbacks()[-1]["error"])
        self.failed_plan = False
        plan_id = self.create_plan()
        self.failed_arrival = True
        self.assertTrue(self.execute("follow_navigation", plan_id=plan_id).is_error)
        event = self.callbacks()[-1]
        self.assertEqual(event["stage"], "walk_error")
        self.assertFalse(event["navigation_completed"])
        self.assertEqual(event["result"]["state"], "finished")
        self.assertEqual(self.toolkit._completed, 0)

    def test_cli_pair_validation_and_opt_in_prompt(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("--output-dir", default=str(self.root))
        parser.add_argument("--memory-profile", default="local")
        parser.add_argument("--memory-dir", default=None)
        parser.add_argument("--explore", action="store_true")
        spec = get_robot_spec()
        spec.add_cli_args(parser, False)
        for flags in [
            ["--frontend-task-id", "1"],
            ["--frontend-endpoint", "http://frontend.test"],
            [
                "--frontend-endpoint",
                "http://frontend.test",
                "--frontend-task-id",
                "../x",
            ],
            [
                "--frontend-endpoint",
                "http://frontend.test",
                "--frontend-task-id",
                "1",
                "--unitree-mode",
                "gateway",
            ],
        ]:
            with self.subTest(flags=flags), self.assertRaises(ValueError):
                spec.parse_config(
                    parser.parse_args(
                        ["--task", "去广告牌", "--unitree-mode", "mujoco", *flags]
                    )
                )
        args = parser.parse_args(
            [
                "--task",
                "去广告牌",
                "--unitree-mode",
                "mujoco",
                "--frontend-task-id",
                "task-123",
                "--frontend-endpoint",
                "http://frontend.test",
            ]
        )
        config = spec.parse_config(args)
        self.assertEqual(config.prompt_vars["frontend_task_id"], "task-123")
        self.assertIn(
            "register_navigation_subtasks",
            spec.prompts.render("system", variables=config.prompt_vars),
        )
        legacy = UnitreeMujocoToolkit(
            nav=NavigationClient("http://nav.test", client=self.http),
            sim=SimulationClient("http://sim.test", client=self.http),
            dashboard_events=NullDashboardEventSink(),
            memory=MemoryManager(self.root / "legacy-memory"),
            state_output_dir=self.root / "legacy-run",
            poll_interval_s=0.001,
        )
        self.addCleanup(legacy.close)
        self.assertEqual(len(legacy.list_tools()), 11)
        result = legacy.execute_tool("plan_navigation", {"goals": ["广告牌"]})
        self.assertFalse(result.is_error, result.data)
        self.assertFalse(
            legacy.execute_tool(
                "follow_navigation", {"plan_id": result.data["plan_id"]}
            ).is_error
        )
        self.assertNotIn("frontend", legacy._state_summary())

    def test_factory_wires_and_closes_its_callback_client(self):
        from robots.unitree.robot_spec import get_toolkit
        from rpent.robots.robot_spec import RunConfig

        config = RunConfig(
            recipe_tag="test_frontend",
            output_dir=self.root / "factory",
            prompt_vars={
                "unitree_mode": "mujoco",
                "nav_endpoint": "http://nav.test",
                "sim_endpoint": "http://sim.test",
                "frontend_endpoint": "http://frontend.test",
                "frontend_task_id": "task-123",
                "frontend_subtasks_format": "array",
                "navigation_timeout_s": 1,
                "memory_dir": str(self.root / "factory-memory"),
            },
            task_desc={},
        )
        with patch("httpx.Client", return_value=self.http):
            wired = get_toolkit(
                runtime_kwargs={
                    "nav": NavigationClient("http://nav.test", client=self.http),
                    "sim": SimulationClient("http://sim.test", client=self.http),
                },
                config=config,
                dashboard_events=NullDashboardEventSink(),
            )
        try:
            registered = wired.execute_tool(
                "register_navigation_subtasks", {"commands": ["去广告牌并确认到达"]}
            )
            self.assertFalse(registered.is_error, registered.data)
            planned = wired.execute_tool(
                "plan_navigation", {"goals": ["广告牌"], "sub_task_id": "sub-1"}
            )
            self.assertFalse(planned.is_error, planned.data)
            followed = wired.execute_tool(
                "follow_navigation", {"plan_id": planned.data["plan_id"]}
            )
            self.assertFalse(followed.is_error, followed.data)
        finally:
            wired.close()
        self.assertTrue(self.http.is_closed)
        self.assertTrue((config.output_dir / "frontend/subtasks.json").is_file())


class RunRpentScriptTests(unittest.TestCase):
    """Verify literal argv forwarding, new artifact directories, and usage errors."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        venv = self.root / "venv with spaces"
        (venv / "bin").mkdir(parents=True)
        python = venv / "bin/python"
        python.write_text(
            f"#!{sys.executable}\n"
            + """
import json
import os
import sys
from pathlib import Path
if sys.argv[1] == "-c":
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
if "LAUNCH_TEST_EXPECTED_KEY" in os.environ:
    assert os.environ.get("OPENAI_API_KEY") == os.environ["LAUNCH_TEST_EXPECTED_KEY"], "config key was not exported"
Path(os.environ["LAUNCH_TEST_ARGS"]).write_text(json.dumps(sys.argv[1:]))
"""
        )
        python.chmod(0o755)
        self.config = self.root / "test.env"
        self.config.write_text("# Isolate launch tests from real local credentials.\n")
        self.env = os.environ.copy()
        self.env.pop("OPENAI_API_KEY", None)
        self.env.pop("LAUNCH_TEST_EXPECTED_KEY", None)
        self.env.update(
            RPENT_CONFIG_FILE=str(self.config),
            RPENT_FRONTEND_ENDPOINT="http://frontend.test",
            RPENT_VENV_DIR=str(venv),
            RPENT_RUNS_DIR=str(self.root / "logs"),
            LAUNCH_TEST_ARGS=str(self.root / "args.json"),
        )

    def launch(self, *args, script=None):
        return subprocess.run(
            ["bash", str(script or REPO / "run_rpent.sh"), *args],
            env=self.env,
            cwd=self.root,
            text=True,
            capture_output=True,
            timeout=15,
        )

    def test_two_arguments_preserve_literal_task_and_create_fresh_run(self):
        marker = self.root / "must-not-exist"
        task = f'先去广告牌，确认到达后，再去厕所通道。\n不要执行 $(touch {marker}) `touch {marker}` "引号"'
        directories = []
        for _ in range(2):
            result = self.launch("task-123", task)
            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads((self.root / "args.json").read_text())
            self.assertEqual(args[:2], ["-m", "rpent.cli.main"])
            parser = argparse.ArgumentParser()
            parser.add_argument("--task")
            parser.add_argument("--frontend-task-id")
            parser.add_argument("--output-dir")
            parsed, _ = parser.parse_known_args(args[2:])
            self.assertEqual(parsed.task, task)
            self.assertEqual(parsed.frontend_task_id, "task-123")
            directories.append(parsed.output_dir)
        self.assertNotEqual(*directories)
        self.assertTrue(all(Path(path).is_dir() for path in directories))
        self.assertFalse(marker.exists())

    def test_legacy_run_rpend_entry_point_forwards_both_arguments(self):
        result = self.launch(
            "task-123", "先去广告牌，再去厕所通道", script=REPO / "run_rpend.sh"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads((self.root / "args.json").read_text())
        self.assertIn("--task=先去广告牌，再去厕所通道", args)
        self.assertIn("--frontend-task-id=task-123", args)

    def test_task_text_starting_with_dashes_stays_one_argument(self):
        result = self.launch("task-123", "--max-turns 999 这是任务文本")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads((self.root / "args.json").read_text())
        parser = argparse.ArgumentParser()
        parser.add_argument("--task")
        parsed, _ = parser.parse_known_args(args[2:])
        self.assertEqual(parsed.task, "--max-turns 999 这是任务文本")

    def test_missing_arguments_fail_before_launch(self):
        self.assertEqual(self.launch("task-only").returncode, 2)
        self.assertEqual(self.launch("task", " ").returncode, 2)
        self.assertFalse((self.root / "args.json").exists())

    def test_frontend_endpoint_and_array_format_default_to_documented_contract(self):
        self.env.pop("RPENT_FRONTEND_ENDPOINT")
        result = self.launch("task", "去广告牌")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads((self.root / "args.json").read_text())
        self.assertEqual(
            args[args.index("--frontend-endpoint") + 1], "http://121.36.57.9:80"
        )
        self.assertEqual(args[args.index("--frontend-subtasks-format") + 1], "array")

    def test_custom_config_exports_credentials_without_printing_them(self):
        key = "test-only-key-from-private-config"
        self.config.write_text(
            f"export OPENAI_API_KEY='{key}'\n"
            "export RPENT_MODEL='openai-chat:test-config-model'\n"
        )
        self.env["OPENAI_API_KEY"] = "test-only-parent-key"
        self.env["LAUNCH_TEST_EXPECTED_KEY"] = key
        result = self.launch("task-123", "去广告牌")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(key, result.stdout + result.stderr)
        args = json.loads((self.root / "args.json").read_text())
        self.assertEqual(
            args[args.index("--model") + 1], "openai-chat:test-config-model"
        )
        self.assertNotIn(key, json.dumps(args))

    def test_default_config_is_relative_to_script_from_another_working_directory(self):
        repo = self.root / "isolated repo"
        repo.mkdir()
        script = repo / "run_rpent.sh"
        script.write_bytes((REPO / "run_rpent.sh").read_bytes())
        key = "test-only-key-in-repository-config"
        (repo / ".env.local").write_text(
            f"export OPENAI_API_KEY='{key}'\n"
            "export RPENT_FRONTEND_ENDPOINT='http://frontend-from-config.test'\n"
        )
        self.env.pop("RPENT_CONFIG_FILE")
        self.env.pop("RPENT_FRONTEND_ENDPOINT")
        self.env["LAUNCH_TEST_EXPECTED_KEY"] = key
        result = self.launch("task-123", "去广告牌", script=script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(key, result.stdout + result.stderr)
        args = json.loads((self.root / "args.json").read_text())
        self.assertEqual(
            args[args.index("--frontend-endpoint") + 1],
            "http://frontend-from-config.test",
        )

    def test_missing_explicit_config_fails_before_launch(self):
        self.config.unlink()
        result = self.launch("task-123", "去广告牌")
        self.assertEqual(result.returncode, 2)
        self.assertIn("configuration file not found", result.stderr)
        self.assertFalse((self.root / "args.json").exists())


class FrontendSmokeCheckTests(unittest.TestCase):
    """Check the real smoke command against the supplied frontend API contract."""

    def run_check(
        self,
        *,
        missing_dataset_id=False,
        unavailable=False,
        timeout_on=None,
        dataset_response=None,
    ):
        from robots.unitree.check_frontend import check_frontend

        requests = []

        def respond(request):
            if unavailable:
                raise httpx.ConnectError("network unavailable", request=request)
            body = json.loads(request.content) if request.content else None
            requests.append((request.method, request.url.path, body))
            if (request.method, request.url.path) == timeout_on:
                raise httpx.ReadTimeout("timed out", request=request)
            if request.method == "GET":
                return httpx.Response(200, json={"items": []})
            if request.url.path == "/api/rpend":
                self.assertIn("no robot motion", body["task_name"])
                return httpx.Response(201, json={"id": "task-smoke"})
            if request.url.path == "/api/rpend/task-smoke":
                self.assertIsInstance(body, list)
                self.assertEqual(len(body), 3)
                return httpx.Response(
                    201,
                    json={
                        "items": [
                            {"id": "smoke-1"},
                            {"id": "smoke-2"},
                            {"id": "smoke-3"},
                        ]
                    },
                )
            self.assertEqual(request.url.path, "/api/rpend/task-smoke/smoke-1")
            self.assertEqual(body["source_type"], "sil")
            self.assertEqual(body["data"]["task_id"], "task-smoke")
            result = (
                dataset_response
                if dataset_response is not None
                else ({} if missing_dataset_id else {"id": "dataset-smoke"})
            )
            return httpx.Response(201, json=result)

        with tempfile.TemporaryDirectory() as directory:
            http = httpx.Client(transport=httpx.MockTransport(respond))
            with patch("httpx.Client", return_value=http):
                report = check_frontend("http://frontend.test", Path(directory))
            saved = json.loads((Path(directory) / "frontend_check.json").read_text())
            self.assertEqual(report, saved)
            self.assertTrue(http.is_closed)
        self.assertFalse(report["motion_submitted"])
        return report, requests

    def test_create_register_report_and_readback_use_actual_callback_client(self):
        report, requests = self.run_check()
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["task_id"], "task-smoke")
        self.assertEqual(report["sub_task_ids"], ["smoke-1", "smoke-2", "smoke-3"])
        self.assertEqual(report["dataset_id"], "dataset-smoke")
        self.assertEqual(
            [(method, path) for method, path, _ in requests],
            [
                ("GET", "/api/rpend"),
                ("POST", "/api/rpend"),
                ("POST", "/api/rpend/task-smoke"),
                ("POST", "/api/rpend/task-smoke/smoke-1"),
                ("GET", "/api/rpend"),
            ],
        )

    def test_missing_dataset_id_is_a_failed_check_not_navigation_success(self):
        report, _ = self.run_check(missing_dataset_id=True)
        self.assertFalse(report["ok"])
        self.assertIn("error", report)

    def test_integer_dataset_id_from_frontend_is_accepted_and_readback_continues(self):
        report, requests = self.run_check(
            dataset_response={
                "id": 5,
                "dataset_id": "5",
                "status": "ready",
                "source_type": "sim",
            }
        )
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["dataset_id"], "5")
        self.assertEqual(report["stage"], "done")
        self.assertEqual(requests[-1][:2], ("GET", "/api/rpend"))
        self.assertEqual(len(requests), 5)

    def test_invalid_dataset_response_ids_are_still_rejected(self):
        for value in (True, None, "", "..", "invalid/id", []):
            with self.subTest(value=value):
                report, requests = self.run_check(dataset_response={"id": value})
                self.assertFalse(report["ok"])
                self.assertEqual(report["stage"], "upload_dataset")
                self.assertEqual(len(requests), 4)

    def test_network_failure_is_recorded_before_any_task_is_created(self):
        report, requests = self.run_check(unavailable=True)
        self.assertFalse(report["ok"])
        self.assertEqual(requests, [])
        self.assertNotIn("task_id", report)
        self.assertEqual(report["stage"], "query_before")
        self.assertEqual(report["error_type"], "ConnectError")

    def test_parent_timeout_identifies_post_and_retains_name_without_retrying(self):
        report, requests = self.run_check(timeout_on=("POST", "/api/rpend"))
        self.assertFalse(report["ok"])
        self.assertEqual(report["stage"], "create_parent")
        self.assertEqual(report["error_type"], "ReadTimeout")
        self.assertEqual(report["request"]["method"], "POST")
        self.assertIn("task_name", report)
        self.assertEqual(len(requests), 2)
        self.assertTrue(report["requests"][0]["ok"])
        self.assertFalse(report["requests"][1]["ok"])

    def test_wrapped_callback_timeout_preserves_http_error_kind_and_existing_ids(self):
        report, requests = self.run_check(
            timeout_on=("POST", "/api/rpend/task-smoke/smoke-1")
        )
        self.assertFalse(report["ok"])
        self.assertEqual(report["stage"], "upload_dataset")
        self.assertEqual(report["error_type"], "ReadTimeout")
        self.assertEqual(report["task_id"], "task-smoke")
        self.assertEqual(len(report["sub_task_ids"]), 3)
        self.assertEqual(len(requests), 4)

    def test_frontend_uses_environment_proxy_while_navigation_stays_direct(self):
        from robots.unitree.check_frontend import check_frontend

        routes = []

        def transport(*args, proxy=None, **kwargs):
            def respond(request):
                routes.append((request.url.host, str(proxy.url) if proxy else None))
                identity = {"ok": True, "map_id": "room", "map_version": "v1"}
                if request.url.host in {"nav.test", "sim.test"}:
                    return httpx.Response(
                        200, json={**identity, "simulator_running": True}
                    )
                if request.method == "GET":
                    return httpx.Response(200, json={"items": []})
                if request.url.path == "/api/rpend":
                    return httpx.Response(201, json={"id": "proxy-task"})
                if request.url.path == "/api/rpend/proxy-task":
                    return httpx.Response(
                        201,
                        json={
                            "items": [
                                {"id": "sub-1"},
                                {"id": "sub-2"},
                                {"id": "sub-3"},
                            ]
                        },
                    )
                return httpx.Response(201, json={"id": "proxy-dataset"})

            return httpx.MockTransport(respond)

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(
                os.environ,
                {"http_proxy": "http://proxy.test:7897", "no_proxy": ""},
                clear=True,
            ):
                with patch("httpx._client.HTTPTransport", side_effect=transport):
                    report = check_frontend("http://frontend.test", Path(directory))
                    self.assertTrue(report["ok"], report)
                    owned = FrontendTaskClient(
                        "http://frontend.test", "proxy-task", output_dir=Path(directory)
                    )
                    try:
                        owned.register_subtasks(["广告牌", "厕所通道", "母婴室"])
                    finally:
                        owned.close()
                    nav = NavigationClient("http://nav.test")
                    sim = SimulationClient("http://sim.test")
                    try:
                        nav.health()
                        sim.health()
                    finally:
                        nav.close()
                        sim.close()
        self.assertTrue(
            all(
                proxy == "http://proxy.test:7897"
                for host, proxy in routes
                if host == "frontend.test"
            )
        )
        self.assertEqual(
            [(host, proxy) for host, proxy in routes if host != "frontend.test"],
            [("nav.test", None), ("sim.test", None)],
        )


if __name__ == "__main__":
    unittest.main()
