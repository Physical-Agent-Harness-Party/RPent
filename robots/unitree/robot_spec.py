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

"""Robot registration and CLI hooks for the Unitree command RPC prototype."""

from __future__ import annotations

import argparse
import math
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from robots.unitree.prompt_bundle import system_prompt, user_prompt
from rpent.dashboard.events import DashboardEventSink
from rpent.memory import MemoryManager
from rpent.robots.prompt_bundle import PromptBundle
from rpent.robots.robot_spec import RobotSpec, RunConfig
from rpent.robots.runtime import try_wait_server
from rpent.utils.config import get_memory_dir, get_repo_root
from rpent.utils.daemon import ProcessDaemon
from rpent.utils.rpc import make_rpc_client


def get_robot_spec() -> RobotSpec:
    """Register a terminal-only Unitree backend with free-form task input."""
    return RobotSpec(
        name="unitree",
        is_real_robot=True,
        prompts=PromptBundle(system=system_prompt, user=user_prompt),
        add_cli_args=_add_cli_args,
        parse_config=_parse_config,
        init_runtime=_init_runtime,
    )


def get_toolkit(
    *,
    runtime_kwargs: dict[str, Any],
    dashboard_events: DashboardEventSink,
    config: RunConfig,
):
    """Bind the runtime client to the planner's movement tools."""
    from robots.unitree.toolkit import UnitreeToolkit

    return UnitreeToolkit(
        **runtime_kwargs,
        dashboard_events=dashboard_events,
        memory=MemoryManager(config.prompt_vars["memory_dir"]),
        state_output_dir=config.output_dir,
    )


def _add_cli_args(parser: argparse.ArgumentParser, use_dashboard: bool) -> None:
    parser.set_defaults(memory_profile="local")
    parser.add_argument(
        "--task", required=not use_dashboard, help="Natural-language robot task."
    )
    parser.add_argument(
        "--env-endpoint",
        default="http://127.0.0.1:8115",
        help="Existing Unitree command RPC server (HTTP or socket).",
    )
    parser.add_argument(
        "--env-timeout-s",
        type=float,
        default=10.0,
        help="Server readiness and command RPC timeout in seconds (default: 10).",
    )


def _parse_config(args: argparse.Namespace) -> RunConfig:
    task = (args.task or "").strip()
    if not task:
        raise ValueError("--task must contain a task description")
    if args.memory_profile != "local":
        raise ValueError("Unitree command delivery requires --memory-profile local")
    if not math.isfinite(args.env_timeout_s) or args.env_timeout_s <= 0:
        raise ValueError("--env-timeout-s must be finite and greater than zero")
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = Path(
        args.output_dir or get_repo_root() / "logs" / f"{timestamp}_unitree"
    )
    memory_dir = (
        Path(args.memory_dir).expanduser().resolve()
        if args.memory_dir
        else get_memory_dir("unitree")
    )
    return RunConfig(
        recipe_tag=f"unitree_{uuid4().hex[:12]}",
        output_dir=output_dir,
        prompt_vars={
            "task": task,
            "env_endpoint": args.env_endpoint,
            "memory_dir": str(memory_dir),
        },
        task_desc={
            "task": task,
            "env_endpoint": args.env_endpoint,
            "validation_scope": "command_delivery",
        },
    )


def _init_runtime(
    args: argparse.Namespace,
    output_dir: Path,
    dashboard_events: DashboardEventSink,
    components: set[str] | None,
) -> tuple[list[ProcessDaemon], dict[str, Any]]:
    del output_dir
    from robots.unitree.env_client import UnitreeEnvClient

    selected = {"env"} if components is None else components
    if selected.difference({"env"}):
        raise ValueError(f"unknown Unitree runtime components: {sorted(selected)}")
    if not selected:
        return [], {}
    rpc = make_rpc_client(args.env_endpoint)
    try:
        try_wait_server({}, dashboard_events, "env", rpc, None, args.env_timeout_s)
    except Exception:
        rpc.close()
        raise
    return [], {"env": UnitreeEnvClient(rpc, timeout_s=args.env_timeout_s)}
