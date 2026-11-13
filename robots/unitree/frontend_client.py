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

"""Register model-produced subtasks and report navigation to an existing UI API."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote
from uuid import uuid4

import httpx

from robots.unitree.http_client import ServiceError, validate_endpoint
from rpent.evaluation import write_json_atomic

DEFAULT_FRONTEND_ENDPOINT = "http://121.36.57.9:80"


def validate_task_id(value: str) -> str:
    """Validate one opaque identifier used as an HTTP path segment."""
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or any(char.isspace() or ord(char) < 32 for char in value)
        or any(char in value for char in ("/", "\\", "\x7f"))
    ):
        raise ValueError("task identifiers must be nonempty single path segments")
    return value


def parse_response_id(value: Any) -> str:
    """Read a JSON string or integer identifier as a path-safe string."""
    if type(value) is int:
        value = str(value)
    try:
        return validate_task_id(value)
    except ValueError as exc:
        raise ServiceError("frontend returned an invalid identifier") from exc


class FrontendTaskClient:
    """Own callbacks and their artifacts for one task, without automatic POST retries.

    A failed or ambiguous registration/callback blocks further navigation. The
    frontend does not promise idempotency, so replay requires operator handling.
    An injected HTTP client is borrowed; otherwise this object owns its client.
    """

    def __init__(
        self,
        endpoint: str,
        task_id: str,
        *,
        output_dir: Path,
        subtasks_format: Literal["object", "array"] = "array",
        timeout_s: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        validate_endpoint(endpoint)
        self.task_id = validate_task_id(task_id)
        if subtasks_format not in {"object", "array"}:
            raise ValueError("subtasks_format must be object or array")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("frontend timeout must be finite and positive")
        self._endpoint = endpoint.rstrip("/")
        self._path = "/api/rpend/" + quote(self.task_id, safe="")
        self._format = subtasks_format
        self._timeout_s = timeout_s
        self._artifacts = Path(output_dir) / "frontend"
        self._owns_client = client is None
        # Match the supplied curl scripts, which honor HTTP(S)_PROXY/NO_PROXY.
        # Navigation and simulation clients keep their separate direct routing.
        self._http = client or httpx.Client(trust_env=True)
        self._registration_attempted = False
        self.subtasks: list[dict[str, Any]] = []
        self.error: str | None = None

    def raise_if_failed(self) -> None:
        """Block further work after a callback whose delivery was not confirmed."""
        if self.error is not None:
            raise ServiceError(
                f"frontend reporting failed; do not repeat motion: {self.error}"
            )

    def _post(self, path: str, payload: Any, *, require_json: bool = False) -> Any:
        record: dict[str, Any] = {"path": path, "request": payload, "delivered": False}
        destination = self._artifacts / "requests" / f"{uuid4().hex}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Keep the exact body even if the connection or acknowledgement is lost.
        write_json_atomic(destination, record)
        try:
            response = self._http.post(
                self._endpoint + path, json=payload, timeout=self._timeout_s
            )
            record["status_code"] = response.status_code
            try:
                result = response.json()
            except ValueError:
                result = None
            record["response"] = result
            if not response.is_success or (
                isinstance(result, dict) and result.get("ok") is False
            ):
                message = result.get("error") if isinstance(result, dict) else None
                raise ServiceError(str(message or response.reason_phrase))
            if require_json and not isinstance(result, dict):
                raise ServiceError("expected a JSON object")
            record["delivered"] = True
            return result
        except (httpx.HTTPError, ServiceError) as exc:
            self.error = f"POST {path}: {exc}"
            record["error"] = self.error
            raise ServiceError(self.error) from exc
        finally:
            write_json_atomic(destination, record)

    def register_subtasks(self, commands: list[str]) -> list[dict[str, Any]]:
        """Register an ordered finite list and validate the frontend's ID mapping.

        Args:
            commands: One command including arrival verification per destination.
        """
        self.raise_if_failed()
        if self._registration_attempted:
            raise ServiceError("subtasks have already been submitted; reuse their IDs")
        if not commands or any(
            not isinstance(command, str) or not command.strip() for command in commands
        ):
            raise ValueError("subtasks must contain nonempty command strings")
        commands = [command.strip() for command in commands]
        items = [
            {f"sub_task_{index}": command} for index, command in enumerate(commands, 1)
        ]
        payload = {"sub_tasks": items} if self._format == "object" else items
        self._registration_attempted = True
        try:
            result = self._post(self._path, payload, require_json=True)
            if (
                "task_id" in result
                and parse_response_id(result["task_id"]) != self.task_id
            ):
                raise ServiceError("frontend task_id does not match this run")
            ids = self._subtask_ids(result, len(commands))
            if len(set(ids)) != len(ids):
                raise ServiceError("frontend returned duplicate subtask IDs")
            self.subtasks = [
                {"index": i, "command": command, "sub_task_id": ids[i - 1]}
                for i, command in enumerate(commands, 1)
            ]
            write_json_atomic(
                self._artifacts / "subtasks.json",
                {"task_id": self.task_id, "sub_tasks": self.subtasks},
            )
        except ServiceError as exc:
            self.error = str(exc)
            raise
        return self.subtasks

    @staticmethod
    def _subtask_ids(result: dict[str, Any], count: int) -> list[str]:
        # The frontend smoke scripts map request order to items[i].id.
        if "items" in result:
            items = result["items"]
            if not isinstance(items, list) or len(items) != count:
                raise ServiceError("frontend must return one item for each subtask")
            ids = []
            for item in items:
                if not isinstance(item, dict):
                    raise ServiceError("frontend subtask items must be objects")
                ids.append(parse_response_id(item.get("id")))
            return ids

        # Preserve the mapping contract supported by existing integrations.
        if "task_id" not in result:
            raise ServiceError(
                "frontend response must contain items or task_id/sub_tasks"
            )
        mapping = result.get("sub_tasks")
        if not isinstance(mapping, list) or len(mapping) != count:
            raise ServiceError("frontend must return one ID for each subtask")
        indexed_ids = {}
        for entry in mapping:
            if not isinstance(entry, dict) or len(entry) != 1:
                raise ServiceError(
                    "subtask mapping entries must contain one index and ID"
                )
            key, value = next(iter(entry.items()))
            if key in indexed_ids:
                raise ServiceError("frontend returned a duplicate subtask index")
            indexed_ids[key] = parse_response_id(value)
        if set(indexed_ids) != {str(i) for i in range(1, count + 1)}:
            raise ServiceError("frontend subtask indices must cover 1 through N")
        return [indexed_ids[str(i)] for i in range(1, count + 1)]

    def report(
        self,
        sub_task_id: str,
        stage: Literal["astar", "walk_accepted", "walk_finished", "walk_error"],
        result: dict[str, Any] | None,
        *,
        plan_id: str | None,
        error: str | None = None,
        navigation_completed: bool | None = None,
    ) -> Any:
        """POST a service result with local task correlation and explicit phase."""
        self.raise_if_failed()
        if sub_task_id not in {task["sub_task_id"] for task in self.subtasks}:
            raise ValueError("sub_task_id was not registered for this task")
        data: dict[str, Any] = {
            "task_id": self.task_id,
            "sub_task_id": sub_task_id,
            "stage": stage,
            "plan_id": plan_id,
            "result": result,
        }
        if error is not None:
            data["error"] = error
        if navigation_completed is not None:
            data["navigation_completed"] = navigation_completed
        return self._post(
            self._path + "/" + quote(sub_task_id, safe=""),
            {"data": data, "source_type": "sil"},
        )

    def close(self) -> None:
        """Close only the HTTP client owned by this callback integration."""
        if self._owns_client:
            self._http.close()
