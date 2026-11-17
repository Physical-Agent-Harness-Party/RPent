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

"""Synchronous client for upstream LightNav's connection-scoped VLN protocol."""

from __future__ import annotations

import json
import math
import threading
from typing import Any
from urllib.parse import urlsplit

from robots.unitree.http_client import ServiceError, finite_number


def validate_vln_endpoint(endpoint: str) -> None:
    """Require a credential-free WebSocket URL before any network access."""
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme not in ("ws", "wss")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "VLN endpoint must be a ws:// or wss:// URL without credentials"
        )
    _ = parsed.port


class VlnClient:
    """Own one episode connection; failed or timed-out requests are never replayed."""

    def __init__(self, endpoint: str, *, timeout_s: float = 3.0) -> None:
        validate_vln_endpoint(endpoint)
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("VLN timeout must be finite and positive")
        self.endpoint, self.timeout_s = endpoint, timeout_s
        self._lock = threading.Lock()
        self._requests = threading.Lock()
        self._connection: Any = None
        self._sequence = 0

    @staticmethod
    def _exchange(
        connection: Any, action: str, payload: dict[str, Any], timeout_s: float
    ) -> dict[str, Any]:
        connection.send(json.dumps({"action": action, "data": payload}))
        response = json.loads(connection.recv(timeout=timeout_s))
        if not isinstance(response, dict) or response.get("action") != action:
            raise ServiceError(f"VLN response action does not match {action}")
        data = response.get("data")
        if (
            not isinstance(data, dict)
            or type(data.get("rc")) is not int
            or data["rc"] != 0
        ):
            raise ServiceError(f"VLN {action} failed: {data}")
        return data

    def start_episode(self, episode_id: str) -> None:
        """Open a new connection and explicitly clear its model frame history."""
        from websockets.sync.client import connect

        self.close()
        try:
            connection = connect(
                self.endpoint,
                proxy=None,
                open_timeout=min(self.timeout_s, 5.0),
                close_timeout=1.0,
                max_size=8 * 1024 * 1024,
            )
            with self._lock:
                self._connection, self._sequence = connection, 0
            self._exchange(
                connection, "login", {"clientId": episode_id}, self.timeout_s
            )
            self._exchange(connection, "reset", {}, self.timeout_s)
        except Exception as exc:
            self.close()
            raise ServiceError(f"VLN episode initialization failed: {exc}") from exc

    def predict(
        self, image_base64: str, instruction: str, *, timeout_s: float | None = None
    ) -> dict[str, Any]:
        """Predict one waypoint chunk, preserving request/response sequence ownership."""
        with self._requests:
            with self._lock:
                connection = self._connection
                self._sequence += 1
                sequence = self._sequence
            if connection is None:
                raise ServiceError("VLN episode is not connected")
            try:
                data = self._exchange(
                    connection,
                    "next",
                    {
                        "seq": sequence,
                        "image": image_base64,
                        "instruction": instruction,
                    },
                    self.timeout_s if timeout_s is None else timeout_s,
                )
                if type(data.get("seq")) is not int or data["seq"] != sequence:
                    raise ServiceError("VLN response sequence does not match its image")
                if type(data.get("stop")) is not bool:
                    raise ServiceError("VLN prediction must contain boolean stop")
                actions = data.get("actions")
                points = actions.get("actions") if isinstance(actions, dict) else None
                if (
                    not isinstance(points, list)
                    or len(points) > 64
                    or (not points and not data["stop"])
                ):
                    raise ServiceError(
                        "VLN prediction must contain 1–64 waypoint rows, or STOP"
                    )
                for index, row in enumerate(points):
                    if not isinstance(row, list) or len(row) != 3:
                        raise ServiceError(f"VLN waypoint {index} must be [x, y, yaw]")
                    for value in row:
                        finite_number(value, "VLN waypoint")
                return data
            except Exception as exc:
                self.close()
                raise ServiceError(
                    f"VLN prediction failed; request not retried: {exc}"
                ) from exc

    def health(self) -> dict[str, Any]:
        """Probe a separate connection without resetting an active model episode."""
        other = VlnClient(self.endpoint, timeout_s=min(self.timeout_s, 3.0))
        try:
            other.start_episode("rpent-health")
            return {"ok": True, "endpoint": self.endpoint}
        finally:
            other.close()

    def close(self) -> None:
        """Interrupt any pending receive and close only this client's connection."""
        with self._lock:
            connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()
