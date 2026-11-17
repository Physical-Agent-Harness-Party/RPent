# Copyright 2026 The RPent Authors.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# https://www.apache.org/licenses/LICENSE-2.0

"""Task-level MCP tools sharing the HTTP runner's persistence and motion owner."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

if TYPE_CHECKING:
    from robots.unitree.task_server import TaskRunner


def create_task_mcp(
    get_runner: Callable[[], TaskRunner],
    *,
    robot_id: str,
    allowed_hosts: list[str] | None = None,
) -> FastMCP:
    """Expose one configured robot; callers retain IDs across reconnects and retries.

    Args:
        get_runner: Accessor for the runner owned by the enclosing HTTP lifespan.
        robot_id: Stable robot identity advertised to the dispatcher.
        allowed_hosts: Additional trusted Host headers for private-network access.

    Returns:
        A server whose session manager is owned by the enclosing application.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", robot_id):
        raise ValueError("robot_id must be 1–64 ASCII letters, digits, _, - or .")
    security = None
    if allowed_hosts:
        security = TransportSecuritySettings(
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", *allowed_hosts],
            allowed_origins=[
                "http://127.0.0.1:*",
                "http://localhost:*",
                "http://[::1]:*",
            ],
        )
    server = FastMCP(
        "rpent-tasks",
        instructions=(
            "Delegate complete robot tasks through submit_robot_task, then query or "
            "wait for their recorded results. An accepted task is still running. "
            "Reuse the same task_id and exact task text after a lost acknowledgement; "
            "query an uncertain outcome before dispatching replacement motion. "
            "A tool timeout or disconnection does not stop a robot task. "
            "Use stop_robot_task and check the final status. requires_attention and "
            "interrupted require reconciliation before another task can run. "
            "Only succeeded with supporting execution evidence means success."
        ),
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        transport_security=security,
    )
    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False)

    def runner_for(requested: str) -> TaskRunner:
        if requested != robot_id:
            raise ValueError(
                f"unknown robot_id: {requested}; configured robot: {robot_id}"
            )
        return get_runner()

    @server.tool(annotations=readonly)
    async def list_robots() -> dict[str, Any]:
        """List this endpoint's robot and runner availability, including blocked IDs."""
        runner = get_runner()
        health = await asyncio.to_thread(runner.health)
        return {
            "robots": [
                {
                    "robot_id": robot_id,
                    "task_reporting": runner.task_reporting,
                    **health,
                }
            ]
        }

    @server.tool(annotations=ToolAnnotations(idempotentHint=True))
    async def submit_robot_task(
        robot_id: str, task_id: str, task_name: str
    ) -> dict[str, Any]:
        """Start one complete task and return immediately with its persistent record.

        Args:
            robot_id: Exact identity returned by list_robots.
            task_id: Stable execution ID. Reuse it for the same submission only.
            task_name: Complete natural-language instruction, including acceptance.

        Returns:
            created and task; created=false means this ID already owns the same task.
        """
        from robots.unitree.task_server import RunRequest

        runner = runner_for(robot_id)
        request = RunRequest(task_id=task_id, task_name=task_name)
        record, created = await asyncio.to_thread(runner.start, request)
        return {"robot_id": robot_id, "created": created, "task": record}

    @server.tool(annotations=readonly)
    async def get_robot_task(robot_id: str, task_id: str) -> dict[str, Any]:
        """Read a recorded task, its business outcome, and available navigation evidence."""
        return await asyncio.to_thread(runner_for(robot_id).get, task_id)

    @server.tool(annotations=readonly)
    async def wait_robot_task(
        robot_id: str,
        task_id: str,
        timeout_s: Annotated[float, Field(ge=0, le=30, allow_inf_nan=False)] = 10,
    ) -> dict[str, Any]:
        """Wait up to 30 seconds for a terminal state; timing out leaves the task running."""
        from robots.unitree.task_server import ACTIVE_STATUSES

        runner = runner_for(robot_id)
        deadline = time.monotonic() + timeout_s
        while True:
            record = await asyncio.to_thread(runner.get, task_id)
            terminal = record["status"] not in ACTIVE_STATUSES
            remaining = deadline - time.monotonic()
            if terminal or remaining <= 0:
                return {"task": record, "terminal": terminal}
            await asyncio.sleep(min(0.25, remaining))

    @server.tool(annotations=readonly)
    async def get_robot_logs(
        robot_id: str,
        task_id: str,
        tail_bytes: Annotated[int, Field(ge=1, le=65536)] = 16384,
    ) -> dict[str, Any]:
        """Read a bounded task log tail; logs are diagnostic evidence, not completion."""
        text = await asyncio.to_thread(runner_for(robot_id).logs, task_id, tail_bytes)
        return {"task_id": task_id, "text": text}

    @server.tool(annotations=ToolAnnotations(idempotentHint=True))
    async def stop_robot_task(robot_id: str, task_id: str) -> dict[str, Any]:
        """Request owned-task cleanup; query until stopped or requires_attention."""
        return await asyncio.to_thread(runner_for(robot_id).stop, task_id)

    return server
