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

"""Client for the Unitree string-command RPC contract.

This command-only interface uses RpcClient directly: it has no Gym step,
action arrays, or automatic environment reset.
"""

from __future__ import annotations

from typing import Any

from rpent.utils.rpc import RpcClient, RpcError


class UnitreeEnvClient:
    """Send command strings and require a matching server acknowledgement."""

    def __init__(self, client: RpcClient, *, timeout_s: float = 10.0) -> None:
        self._client = client
        self._timeout_s = timeout_s

    def execute_command(self, command: str) -> dict[str, Any]:
        """Send one command and return its confirmed receipt.

        Args:
            command: The exact string ``前进`` or ``后退``.
        """
        receipt = self._client.call(
            "env.execute_command", args=(command,), timeout_s=self._timeout_s
        )
        if (
            not isinstance(receipt, dict)
            or receipt.get("received") is not True
            or receipt.get("command") != command
        ):
            raise RpcError(
                "env.execute_command", "server did not acknowledge the sent command"
            )
        return receipt

    def close(self) -> None:
        """Release the RPC client while leaving the external server running."""
        self._client.close()
