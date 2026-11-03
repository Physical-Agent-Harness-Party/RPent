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

"""Local mock Unitree server that records commands without controlling hardware."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

from rpent.utils.logging import get_logger
from rpent.utils.rpc import RpcFacade

logger = get_logger("unitree_env_server")


class MockUnitreeEnvFacade(RpcFacade):
    """Acknowledge the two supported strings and optionally journal receipts."""

    def __init__(self, commands_file: Path | None = None) -> None:
        super().__init__()
        self.received_commands: list[str] = []
        self._commands_file = commands_file
        if commands_file is not None:
            commands_file.parent.mkdir(parents=True, exist_ok=True)
        self._rpc["env.execute_command"] = self.execute_command

    def execute_command(self, command: str) -> dict[str, Any]:
        """Record one accepted command and return a delivery acknowledgement.

        Args:
            command: The exact string ``前进`` or ``后退``.
        """
        if command not in ("前进", "后退"):
            raise ValueError("unsupported command; expected '前进' or '后退'")
        receipt = {
            "command": command,
            "received": True,
            "sequence": len(self.received_commands) + 1,
            "mode": "mock",
        }
        if self._commands_file is not None:
            with self._commands_file.open("a", encoding="utf-8") as file:
                file.write(json.dumps(receipt, ensure_ascii=False) + "\n")
        self.received_commands.append(command)
        logger.info("mock receiver received command: %s", command)
        return receipt


def main() -> None:
    """Start the mock receiver using an existing RPent RPC transport."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=["http", "socket"], default="http")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8115)
    parser.add_argument("--commands-file", type=Path, default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")
    logger.info("starting mock receiver; successful calls confirm command delivery")
    MockUnitreeEnvFacade(args.commands_file).serve(
        transport=args.transport, host=args.host, port=args.port
    )


if __name__ == "__main__":
    main()
