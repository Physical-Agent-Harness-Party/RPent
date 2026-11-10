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


# 提供 Unitree 的基本信息、提示词和接入函数。
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


# 用运行时客户端和本次配置创建工具集。
def get_toolkit(
    *,
    runtime_kwargs: dict[str, Any],
    dashboard_events: DashboardEventSink,
    config: RunConfig,
):
    """Bind the runtime client to the planner's movement tools."""
    from robots.unitree.toolkit import UnitreeToolkit

    return UnitreeToolkit(
        # 展开为构造参数，例如 env=客户端对象。
        **runtime_kwargs,
        dashboard_events=dashboard_events,
        memory=MemoryManager(config.prompt_vars["memory_dir"]),
        state_output_dir=config.output_dir,
    )


# 添加 Unitree 专用命令行参数。
def _add_cli_args(parser: argparse.ArgumentParser, use_dashboard: bool) -> None:
    # 默认使用本地记忆模式。
    parser.set_defaults(memory_profile="local")
    parser.add_argument(
        "--task", required=not use_dashboard, help="Natural-language robot task."
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


# 检查参数并生成本次运行配置。
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
    # 未指定输出目录时，使用带时间戳的默认目录。
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = Path(
        args.output_dir or get_repo_root() / "logs" / f"{timestamp}_unitree"
    )
    memory_dir = (
        Path(args.memory_dir).expanduser().resolve()
        if args.memory_dir
        else get_memory_dir("unitree")
    )
    # prompt_vars 用于提示词和工具配置，task_desc 写入运行记录。
    return RunConfig(
        recipe_tag=f"unitree_{uuid4().hex[:12]}",
        output_dir=output_dir,
        prompt_vars={
            "task": task,
            "env_endpoint": args.env_endpoint,
            "robot_index": args.robot_index,
            "memory_dir": str(memory_dir),
        },
        task_desc={
            "task": task,
            "env_endpoint": args.env_endpoint,
            "robot_index": args.robot_index,
            "validation_scope": VALIDATION_SCOPE,
        },
    )


# 连接外部通信服务并检查机器人状态。
def _init_runtime(
    args: argparse.Namespace,
    output_dir: Path,
    dashboard_events: DashboardEventSink,
    components: set[str] | None,
) -> tuple[list[ProcessDaemon], dict[str, Any]]:
    del output_dir  # 本函数不使用该参数，不会删除磁盘目录。
    from robots.unitree.env_client import UnitreeEnvClient

    # None 表示初始化默认的 env 组件。
    selected = {"env"} if components is None else components
    if selected.difference({"env"}):
        raise ValueError(f"unknown Unitree runtime components: {sorted(selected)}")
    if not selected:
        return [], {}
    rpc = make_rpc_client(args.env_endpoint)
    env = UnitreeEnvClient(
        rpc, timeout_s=args.env_timeout_s, robot_index=args.robot_index
    )

    # 服务就绪后校验列表；配置了默认机器人时，还要确认其在线。
    def check_robots() -> None:
        env.list_robots()
        if args.robot_index is not None:
            status = env.get_robot_status()
            if status["status"] != "online":
                raise RuntimeError(f"robot {args.robot_index} is offline")

    # 使用列表接口探测服务，成功后调用 check_robots。
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
        # 检查失败时释放客户端资源，再向上抛出错误。
        rpc.close()
        raise
    # 服务由外部启动，无需返回本地服务进程；env 随后传给 Toolkit。
    return [], {"env": env}
