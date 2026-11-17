# Copyright 2026 The RPent Authors.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# https://www.apache.org/licenses/LICENSE-2.0

"""Exercise real MCP messages and shared runner ownership without robot motion."""

import asyncio
import json
import shlex
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from robots.unitree.task_server import create_app

TOKEN = "offline-test-token"
URL = "http://127.0.0.1:8780"
CHILD = """
import json, os, signal, sys, time
from pathlib import Path
root = Path.cwd()
output = root / 'outputs' / sys.argv[1]
output.mkdir(parents=True)
print('RPent output: ' + str(output), flush=True)
with (root / 'launches.jsonl').open('a') as f:
    f.write(json.dumps({'id': sys.argv[1], 'reporting': os.environ['RPENT_TASK_REPORTING']}) + '\\n')
(output / 'states.json').write_text(json.dumps({'steps': [{'state': {}}]}))
if sys.argv[2] == 'wait':
    signal.signal(signal.SIGINT, lambda *_: sys.exit(130))
    print('waiting for stop', flush=True)
    while True:
        time.sleep(.05)
finish = {'status': 'failure' if sys.argv[2] == 'failure' else 'success', 'summary': 'checked'}
(output / 'transcript_unitree_test.json').write_text(json.dumps({'finish': finish, 'error': None}))
"""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "child.py").write_text(CHILD)
    (tmp_path / "run_rpent.sh").write_text(
        f'exec {shlex.quote(sys.executable)} {shlex.quote(str(tmp_path / "child.py"))} "$@"\n'
    )
    return tmp_path


def make_app(root: Path, **kwargs):
    return create_app(
        repo_root=root,
        jobs_dir=root / "jobs",
        token=TOKEN,
        enable_mcp=True,
        robot_id="go2-test",
        task_reporting="local",
        **kwargs,
    )


@asynccontextmanager
async def connect(app):
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            headers={"Authorization": "Bearer " + TOKEN},
            base_url=URL,
        ) as http:
            async with streamable_http_client(URL + "/mcp/", http_client=http) as (
                read,
                write,
                _,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session, http


async def call(session, name, **args):
    result = await session.call_tool(name, args)
    assert not result.isError, result
    return result.structuredContent


def test_http_and_mcp_share_one_idempotent_task_owner(root):
    async def run():
        async with connect(make_app(root)) as (session, http):
            tools = {tool.name for tool in (await session.list_tools()).tools}
            assert tools == {
                "list_robots",
                "submit_robot_task",
                "get_robot_task",
                "wait_robot_task",
                "get_robot_logs",
                "stop_robot_task",
            }
            robots = await call(session, "list_robots")
            assert robots["robots"][0]["task_reporting"] == "local"
            args = {"robot_id": "go2-test", "task_id": "task-1", "task_name": "success"}
            started = await call(session, "submit_robot_task", **args)
            assert started["created"]
            finished = await call(
                session,
                "wait_robot_task",
                robot_id="go2-test",
                task_id="task-1",
                timeout_s=3,
            )
            assert finished["terminal"]
            assert finished["task"]["status"] == "succeeded"
            assert (await http.get("/api/rpend/run/task-1")).json() == finished["task"]
            assert not (await call(session, "submit_robot_task", **args))["created"]
            response = await http.post(
                "/api/rpend/run", json={"task_id": "task-1", "task_name": "success"}
            )
            assert response.status_code == 200
            changed = await session.call_tool(
                "submit_robot_task", args | {"task_name": "different"}
            )
            assert changed.isError
        launches = [
            json.loads(line)
            for line in (root / "launches.jsonl").read_text().splitlines()
        ]
        assert launches == [{"id": "task-1", "reporting": "local"}]

    asyncio.run(run())


def test_wait_timeout_keeps_task_running_and_stop_releases_owner(root):
    async def run():
        async with connect(make_app(root)) as (session, _):
            await call(
                session,
                "submit_robot_task",
                robot_id="go2-test",
                task_id="task-wait",
                task_name="wait",
            )
            waiting = await call(
                session,
                "wait_robot_task",
                robot_id="go2-test",
                task_id="task-wait",
                timeout_s=0.2,
            )
            assert not waiting["terminal"]
            assert waiting["task"]["status"] == "running"
            blocked = await session.call_tool(
                "submit_robot_task",
                {
                    "robot_id": "go2-test",
                    "task_id": "task-other",
                    "task_name": "success",
                },
            )
            assert blocked.isError
            logs = await call(
                session, "get_robot_logs", robot_id="go2-test", task_id="task-wait"
            )
            assert "waiting for stop" in logs["text"]
            await call(
                session, "stop_robot_task", robot_id="go2-test", task_id="task-wait"
            )
            stopped = await call(
                session,
                "wait_robot_task",
                robot_id="go2-test",
                task_id="task-wait",
                timeout_s=3,
            )
            assert stopped["task"]["status"] == "stopped"
            await call(
                session,
                "submit_robot_task",
                robot_id="go2-test",
                task_id="task-after",
                task_name="failure",
            )
            failed = await call(
                session,
                "wait_robot_task",
                robot_id="go2-test",
                task_id="task-after",
                timeout_s=3,
            )
            assert failed["task"]["status"] == "failed"

    asyncio.run(run())


def test_restart_retains_task_id_without_motion_replay(root):
    async def run():
        args = {
            "robot_id": "go2-test",
            "task_id": "task-persist",
            "task_name": "success",
        }
        async with connect(make_app(root)) as (session, _):
            await call(session, "submit_robot_task", **args)
            await call(
                session,
                "wait_robot_task",
                robot_id="go2-test",
                task_id="task-persist",
                timeout_s=3,
            )
        async with connect(make_app(root)) as (session, _):
            result = await call(session, "submit_robot_task", **args)
            assert not result["created"]
            assert result["task"]["status"] == "succeeded"
        assert len((root / "launches.jsonl").read_text().splitlines()) == 1

    asyncio.run(run())


def test_wrong_robot_and_invalid_arguments_never_launch(root):
    async def run():
        async with connect(make_app(root)) as (session, _):
            for args in (
                {"robot_id": "other", "task_id": "valid", "task_name": "success"},
                {
                    "robot_id": "go2-test",
                    "task_id": "../invalid",
                    "task_name": "success",
                },
                {"robot_id": "go2-test", "task_id": "valid", "task_name": " "},
            ):
                assert (await session.call_tool("submit_robot_task", args)).isError
            assert (
                await session.call_tool(
                    "wait_robot_task",
                    {"robot_id": "go2-test", "task_id": "valid", "timeout_s": 31},
                )
            ).isError
            assert (
                await session.call_tool(
                    "get_robot_logs",
                    {"robot_id": "go2-test", "task_id": "valid", "tail_bytes": 65537},
                )
            ).isError
        assert not (root / "launches.jsonl").exists()

    asyncio.run(run())


def test_mcp_and_http_require_token_and_reject_untrusted_host(root):
    async def run():
        app = make_app(root, mcp_allowed_hosts=["go2.internal:8780"])
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=URL
            ) as http:
                assert (await http.post("/mcp/", json={})).status_code == 401
                assert (await http.get("/api/rpend/run/task")).status_code == 401
                body = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                }
                response = await http.post(
                    "/mcp/",
                    json=body,
                    headers={
                        "Authorization": "Bearer " + TOKEN,
                        "Host": "untrusted.example",
                        "Accept": "application/json, text/event-stream",
                    },
                )
                assert response.status_code == 421
                response = await http.post(
                    "/mcp/",
                    json=body,
                    headers={
                        "Authorization": "Bearer " + TOKEN,
                        "Host": "go2.internal:8780",
                        "Accept": "application/json, text/event-stream",
                    },
                )
                assert response.status_code == 200

    asyncio.run(run())
