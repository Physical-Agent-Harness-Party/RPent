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
import hashlib
import math
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from robots.unitree.prompt_bundle import system_prompt, user_prompt
from robots.unitree.protocol import (
    DEFAULT_QUERY_TIMEOUT_S,
    VALIDATION_SCOPE,
    validate_robot_index,
)
from rpent.dashboard.events import DashboardEventSink
from rpent.memory import MemoryManager
from rpent.robots.prompt_bundle import PromptBundle
from rpent.robots.robot_spec import RobotSpec, RunConfig
from rpent.robots.runtime import try_wait_server
from rpent.utils.config import get_memory_dir, get_repo_root
from rpent.utils.daemon import ProcessDaemon
from rpent.utils.rpc import make_rpc_client


_EXPLORE_INSTRUCTIONS = """
EXPLORE MODE (build memory for later runs).
After the navigation task is verified successful:
1. Write ONE global lesson per file into {{memory_inbox}} with write_text_file:
   `new_global_<kind>_<slug>.md`, kind in primitive|perception|strategy|failure.
   Frontmatter:
     ---
     id: <slug>
     scope: global
     kind: <kind>
     title: <one imperative sentence>
     applies_when: <when a future agent should open this>
     symptom: [<search words>]
     evidence:
       cells: [{{recipe_tag}}]
       attempts: 1
     confidence: single-shot
     ---
   Body: one-sentence claim, **Why:**, **How to apply:**, **Falsify:**.
   Only record mechanisms you actually observed (map geometry, A* behaviour,
   timeouts, replanning rules). Do not invent causes.
2. Write the audit JSON to `{{output_dir}}/{{recipe_tag}}.json` BEFORE finish:
   {"task": "...", "map_id": "...", "waypoints": [...], "attempts": 1,
    "final_pose": {...}, "solved": true, "notes": "..."}
3. Then call finish(status="success").
If the task is not solved, do not write memory; report and finish with failure.
"""


def get_robot_spec() -> RobotSpec:
    """Register a terminal-only Unitree backend with free-form task input."""
    return RobotSpec(
        name="unitree",
        is_real_robot=False,
        supports_exploration=True,
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
    mode: str = "evaluation",
    attempts_per_session: int = 0,
    state_output_dir: Path | str | None = None,
):
    """Bind the runtime client to the planner's movement tools."""
    if config.prompt_vars.get("unitree_mode", "gateway") == "mujoco":
        from robots.unitree.mujoco_toolkit import UnitreeMujocoToolkit

        explore = mode == "exploration"
        try:
            return UnitreeMujocoToolkit(
                **runtime_kwargs,
                dashboard_events=dashboard_events,
                memory=MemoryManager(
                    config.prompt_vars["memory_dir"],
                    memory_access="inbox_write" if explore else "read_only",
                    inbox_cell_tag=config.recipe_tag if explore else None,
                ),
                state_output_dir=state_output_dir or config.output_dir,
                recipe_output_dir=config.output_dir,
                navigation_timeout_s=config.prompt_vars["navigation_timeout_s"],
            )
        except Exception:
            for client in runtime_kwargs.values():
                client.close()
            raise

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
        "--auto-merge-memory",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Merge exploration drafts into layered memory (default: enabled).",
    )
    parser.add_argument("--explore-sessions", type=int, default=1)
    parser.add_argument("--explore-attempts-per-session", type=int, default=1)
    parser.add_argument(
        "--task", required=not use_dashboard, help="Natural-language robot task."
    )
    parser.add_argument(
        "--unitree-mode",
        choices=("gateway", "mujoco"),
        default="gateway",
        help="Use the dis_move gateway (default) or external MuJoCo navigation services.",
    )
    parser.add_argument(
        "--nav-endpoint",
        default="http://192.168.104.191:8765",
        help="A* planning service base URL, for --unitree-mode mujoco.",
    )
    parser.add_argument(
        "--sim-endpoint",
        default="http://192.168.104.191:8770",
        help="Simulation control base URL, for --unitree-mode mujoco.",
    )
    parser.add_argument(
        "--navigation-timeout-s",
        type=float,
        default=300.0,
        help="Per-walk or preview budget in MuJoCo mode (default: 300 seconds).",
    )
    parser.add_argument(
        "--env-endpoint",
        default="http://127.0.0.1:8115",
        help="Existing Unitree gateway RPC server (HTTP or socket).",
    )
    parser.add_argument(
        "--robot-index",
        type=int,
        default=None,
        help="Default gateway robot index; omit to select a robot in each tool call.",
    )
    parser.add_argument(
        "--env-timeout-s",
        type=float,
        default=None,
        help="Override command RPC and readiness waits in seconds; by default, command waits use the motion budget plus 30 s and readiness uses 10 s.",
    )


def _parse_config(args: argparse.Namespace) -> RunConfig:
    task = (args.task or "").strip()
    if not task:
        raise ValueError("--task must contain a task description")
    if args.memory_profile != "local":
        raise ValueError("Unitree gateway runs require --memory-profile local")
    if args.env_timeout_s is not None and (
        not math.isfinite(args.env_timeout_s) or args.env_timeout_s <= 0
    ):
        raise ValueError("--env-timeout-s must be finite and greater than zero")
    if args.robot_index is not None:
        validate_robot_index(args.robot_index)
    unitree_mode = getattr(args, "unitree_mode", "gateway")
    if unitree_mode == "mujoco":
        from robots.unitree.http_client import validate_endpoint

        if args.robot_index is not None or args.env_timeout_s is not None:
            raise ValueError(
                "--robot-index and --env-timeout-s apply to gateway mode only"
            )
        validate_endpoint(args.nav_endpoint)
        validate_endpoint(args.sim_endpoint)
        if (
            not math.isfinite(args.navigation_timeout_s)
            or args.navigation_timeout_s <= 0
        ):
            raise ValueError("--navigation-timeout-s must be finite and positive")
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = Path(
        args.output_dir or get_repo_root() / "logs" / f"{timestamp}_unitree"
    )
    memory_dir = (
        Path(args.memory_dir).expanduser().resolve()
        if args.memory_dir
        else get_memory_dir("unitree")
    )
    mode_vars = {"unitree_mode": unitree_mode}
    if unitree_mode == "mujoco":
        mode_vars.update(
            nav_endpoint=args.nav_endpoint,
            sim_endpoint=args.sim_endpoint,
            navigation_timeout_s=args.navigation_timeout_s,
        )
    # 同一任务的多次运行共享 cell 标签，便于证据合并
    recipe_tag = "unitree_" + hashlib.sha1(task.encode("utf-8")).hexdigest()[:8]
    return RunConfig(
        recipe_tag=recipe_tag,
        output_dir=output_dir,
        prompt_vars={
            "task": task,
            "env_endpoint": args.env_endpoint,
            "robot_index": args.robot_index,
            "memory_dir": str(memory_dir),
            "recipe_tag": recipe_tag,
            "output_dir": str(output_dir),
            "memory_inbox": str(memory_dir / "_internal" / "inbox" / recipe_tag),
            "explore_instructions": _EXPLORE_INSTRUCTIONS if args.explore else "",
            **mode_vars,
        },
        task_desc={
            "task": task,
            "env_endpoint": args.env_endpoint,
            "robot_index": args.robot_index,
            "validation_scope": "mujoco_navigation"
            if unitree_mode == "mujoco"
            else VALIDATION_SCOPE,
            **mode_vars,
        },
    )


def _init_runtime(
    args: argparse.Namespace,
    output_dir: Path,
    dashboard_events: DashboardEventSink,
    components: set[str] | None,
) -> tuple[list[ProcessDaemon], dict[str, Any]]:
    if getattr(args, "unitree_mode", "gateway") == "mujoco":
        return _init_mujoco_runtime(args, components)
    from robots.unitree.env_client import UnitreeEnvClient

    selected = {"env"} if components is None else components
    if selected.difference({"env"}):
        raise ValueError(f"unknown Unitree runtime components: {sorted(selected)}")
    if not selected:
        return [], {}
    rpc = make_rpc_client(args.env_endpoint)
    env = UnitreeEnvClient(
        rpc, timeout_s=args.env_timeout_s, robot_index=args.robot_index
    )

    def check_robots() -> None:
        env.list_robots()
        if args.robot_index is not None:
            status = env.get_robot_status()
            if status["status"] != "online":
                raise RuntimeError(f"robot {args.robot_index} is offline")

    try:
        try_wait_server(
            {},
            dashboard_events,
            "env",
            rpc,
            None,
            args.env_timeout_s
            if args.env_timeout_s is not None
            else DEFAULT_QUERY_TIMEOUT_S,
            probe_method="env.list_robots",
            post_fn=check_robots,
        )
    except Exception:
        rpc.close()
        raise
    return [], {"env": env}


def _init_mujoco_runtime(
    args: argparse.Namespace, components: set[str] | None
) -> tuple[list[ProcessDaemon], dict[str, Any]]:
    """Borrow the external REST services, cleaning partial startup on failure."""
    from robots.unitree.http_client import map_identity
    from robots.unitree.navigation_client import NavigationClient
    from robots.unitree.simulation_client import SimulationClient

    selected = {"nav", "sim"} if components is None else components
    if selected.difference({"nav", "sim"}):
        raise ValueError(f"unknown MuJoCo runtime components: {sorted(selected)}")
    clients: dict[str, Any] = {}
    try:
        if "nav" in selected:
            clients["nav"] = NavigationClient(args.nav_endpoint)
        if "sim" in selected:
            clients["sim"] = SimulationClient(args.sim_endpoint, timeout_s=5.0)
        identities = [map_identity(client.health()) for client in clients.values()]
        if len(set(identities)) > 1:
            raise ValueError("planning and simulation map_id/map_version do not match")
    except Exception:
        for client in clients.values():
            client.close()
        raise
    return [], clients
