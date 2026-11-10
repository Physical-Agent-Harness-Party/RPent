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

"""JSON-over-HTTP transport for the external Unitree navigation services."""

from __future__ import annotations

import math
from typing import Any
from urllib.parse import urlsplit

import httpx


class ServiceError(RuntimeError):
    """An external service rejected a request or returned an invalid response."""


def validate_endpoint(endpoint: str) -> None:
    """Require an HTTP service URL without credentials, query, or fragment."""
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("navigation service endpoints must be HTTP(S) base URLs")
    # Accessing port also validates malformed and out-of-range port numbers.
    _ = parsed.port


def finite_number(value: Any, field: str) -> float:
    """Read a finite JSON number, excluding booleans."""
    if type(value) not in (int, float):
        raise ServiceError(f"{field} must be a finite number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ServiceError(f"{field} must be a finite number") from exc
    if not math.isfinite(number):
        raise ServiceError(f"{field} must be a finite number")
    return number


def map_identity(response: dict[str, Any]) -> tuple[str, str]:
    """Require the map identity shared by planning and simulation responses."""
    for field in ("map_id", "map_version"):
        if not isinstance(response.get(field), str) or not response[field]:
            raise ServiceError(f"{field} must be a nonempty string")
    return response["map_id"], response["map_version"]


class JsonServiceClient:
    """Read top-level service JSON, without the RPent RPC result envelope.

    An injected HTTP client is borrowed. Otherwise this object owns its client.
    Requests bypass environment proxies and are never automatically retried.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        timeout_s: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        validate_endpoint(endpoint)
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("HTTP timeout must be finite and positive")
        self.endpoint = endpoint.rstrip("/")
        self._timeout_s = timeout_s
        self._owns_client = client is None
        self._http = client or httpx.Client(trust_env=False)

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Send one request and preserve structured HTTP 400/409 errors."""
        try:
            response = self._http.request(
                method,
                self.endpoint + path,
                json=payload,
                timeout=self._timeout_s if timeout_s is None else timeout_s,
            )
        except httpx.HTTPError as exc:
            raise ServiceError(f"{method} {path}: {exc}") from exc
        try:
            result = response.json()
        except ValueError as exc:
            raise ServiceError(f"{method} {path}: response is not valid JSON") from exc
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise ServiceError(f"{method} {path}: response must contain boolean ok")
        if not response.is_success or not result["ok"]:
            raise ServiceError(
                f"{method} {path}: {result.get('error') or response.reason_phrase}"
            )
        return result

    def close(self) -> None:
        """Close the owned HTTP client; keep borrowed services running."""
        if self._owns_client:
            self._http.close()
