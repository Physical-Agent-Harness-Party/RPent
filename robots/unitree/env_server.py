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

"""Local gateway mock; records routed commands without robot connections or motion."""

from __future__ import annotations

import argparse
import json
import logging
import math
import threading
from pathlib import Path
from typing import Any

from robots.unitree.protocol import validate_command, validate_robot_index
from rpent.utils.logging import get_logger
from rpent.utils.rpc import RpcFacade

logger = get_logger("unitree_env_server")


class MockUnitreeEnvFacade(RpcFacade):
    """Simulate online robots and accepted dis_move completion."""

    def __init__(
        self,
        commands_file: Path | None = None,
        *,
        robot_indices: tuple[int, ...] = (1, 2),
    ) -> None:
        super().__init__()
        for index in robot_indices:
            validate_robot_index(index)
        if len(set(robot_indices)) != len(robot_indices):
            raise ValueError("robot indices must be unique")
        self.robot_statuses = dict.fromkeys(robot_indices, "online")
        self._robot_locks = {index: threading.Lock() for index in robot_indices}
        self._journal_lock = threading.Lock()
        self.received_commands: list[dict[str, Any]] = []
        self._commands_file = commands_file
        if commands_file is not None:
            commands_file.parent.mkdir(parents=True, exist_ok=True)
        self._rpc["env.execute_command"] = self.execute_command
        self._rpc["env.list_robots"] = self.list_robots
        self._readonly_methods.add("env.list_robots")

    def _builtin_dispatch(self, method: str, args: tuple, kwargs: dict) -> Any:
        if method == "healthz":
            return self.get_robot_status(*args, **kwargs)
        return super()._builtin_dispatch(method, args, kwargs)

    def _dispatch_nosession(self, method: str, args: tuple, kwargs: dict) -> Any:
        if method == "env.execute_command":
            # The handler owns per-robot admission; a global write lock would
            # queue busy requests and block other robots for the whole motion.
            return self._rpc[method](*args, **kwargs)
        return super()._dispatch_nosession(method, args, kwargs)

    def list_robots(self) -> dict[str, Any]:
        """Return online simulated robots in ascending index order."""
        robots = [
            {"robot_index": index, "status": status}
            for index, status in sorted(self.robot_statuses.items())
            if status == "online"
        ]
        return {"robots": robots, "total": len(robots)}

    def get_robot_status(self, *, robot_index: int) -> dict[str, Any]:
        """Return a known robot's online/offline status without moving it."""
        validate_robot_index(robot_index)
        if robot_index not in self.robot_statuses:
            raise ValueError(f"unknown robot_index: {robot_index}")
        return {"robot_index": robot_index, "status": self.robot_statuses[robot_index]}

    def execute_command(
        self, command: dict[str, Any], *, robot_index: int
    ) -> dict[str, Any]:
        """Return an idealized completion record without moving physical hardware."""
        status = self.get_robot_status(robot_index=robot_index)
        if status["status"] != "online":
            raise ValueError(f"robot {robot_index} is offline")
        validate_command(command)
        lock = self._robot_locks[robot_index]
        if not lock.acquire(blocking=False):
            raise ValueError("robot is busy")
        try:
            motion = self._simulate_motion(command)
            with self._journal_lock:
                receipt = {
                    "robot_index": robot_index,
                    "command": json.loads(json.dumps(command, allow_nan=False)),
                    "received": True,
                    "completed": True,
                    "motion_result": motion,
                    "sequence": len(self.received_commands) + 1,
                    "mode": "mock",
                }
                if self._commands_file is not None:
                    with self._commands_file.open("a", encoding="utf-8") as file:
                        file.write(json.dumps(receipt, ensure_ascii=False) + "\n")
                self.received_commands.append(receipt)
            logger.info(
                "mock gateway completed command for robot %s: %s", robot_index, command
            )
            return receipt
        finally:
            lock.release()

    def _simulate_motion(self, command: dict[str, Any]) -> dict[str, Any]:
        """Provide exact synthetic results; no control or sensor interface is used."""
        payload = command["payload"]
        distance = math.hypot(payload["dx_m"], payload["dy_m"])
        bearing_deg = (
            math.degrees(math.atan2(payload["dy_m"], payload["dx_m"]))
            if distance
            else 0.0
        )
        return {
            "actual_dx_m": payload["dx_m"],
            "actual_dy_m": payload["dy_m"],
            "actual_arrival_turn_deg": payload["arrival_turn_deg"],
            "total_angle_deg": bearing_deg + payload["arrival_turn_deg"],
            "position_error_m": 0.0,
            "angle_error_deg": 0.0,
            "stop_acknowledged": True,
        }


def main() -> None:
    """Start the mock receiver using an existing RPent RPC transport."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=["http", "socket"], default="http")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8115)
    parser.add_argument("--commands-file", type=Path, default=None)
    parser.add_argument("--robot-indices", nargs="+", type=int, default=[1, 2])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")
    logger.info(
        "starting mock gateway; motion results are synthetic, no hardware is controlled"
    )
    MockUnitreeEnvFacade(
        args.commands_file, robot_indices=tuple(args.robot_indices)
    ).serve(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
