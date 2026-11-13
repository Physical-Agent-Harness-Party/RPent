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

"""Test the documented frontend endpoints using RPent's actual callback client."""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import httpx

from robots.unitree.frontend_client import (
    DEFAULT_FRONTEND_ENDPOINT,
    FrontendTaskClient,
    parse_response_id,
)
from robots.unitree.http_client import ServiceError, validate_endpoint
from rpent.evaluation import write_json_atomic
from rpent.utils.config import get_repo_root


def check_frontend(
    endpoint: str, output_dir: Path, *, timeout_s: float = 10, progress: bool = False
) -> dict:
    """Create a labelled test task, subtasks and one dataset without issuing motion.

    Args:
        endpoint: Frontend API base URL without /api/rpend.
        output_dir: Directory for exact callback requests and the final report.
        timeout_s: HTTP timeout for each request.
        progress: Print each stage before sending its request.

    Returns:
        A report containing the created IDs and any error.
    """
    validate_endpoint(endpoint)
    endpoint = endpoint.rstrip("/")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "ok": False,
        "endpoint": endpoint,
        "motion_submitted": False,
        "timeout_s": timeout_s if math.isfinite(timeout_s) else None,
        "stage": "validate_config",
        "requests": [],
    }
    started = time.monotonic()
    stage_started = started

    def begin(stage: str, method: str, path: str) -> None:
        nonlocal stage_started
        stage_started = time.monotonic()
        request = {"method": method, "url": endpoint + path}
        report.update(stage=stage, request=request)
        report["requests"].append({"stage": stage, **request, "ok": False})
        write_json_atomic(output_dir / "frontend_check.json", report)
        if progress:
            print(
                f"[{stage}] {method} {endpoint + path} (timeout={timeout_s:g}s)",
                flush=True,
            )

    def complete(status_code: int | None = None) -> None:
        record = report["requests"][-1]
        record.update(ok=True, elapsed_s=round(time.monotonic() - stage_started, 3))
        if status_code is not None:
            record["status_code"] = status_code
        write_json_atomic(output_dir / "frontend_check.json", report)

    try:
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout_s must be finite and positive")
        with httpx.Client(trust_env=True, timeout=timeout_s) as http:
            begin("query_before", "GET", "/api/rpend")
            response = http.get(endpoint + "/api/rpend")
            response.raise_for_status()
            response.json()
            complete(response.status_code)
            task_name = "RPent frontend smoke (no robot motion) " + uuid4().hex[:8]
            report["task_name"] = task_name
            begin("create_parent", "POST", "/api/rpend")
            response = http.post(endpoint + "/api/rpend", json={"task_name": task_name})
            response.raise_for_status()
            task_id = parse_response_id(response.json().get("id"))
            report["task_id"] = task_id
            complete(response.status_code)
            frontend = FrontendTaskClient(
                endpoint,
                task_id,
                output_dir=output_dir,
                timeout_s=timeout_s,
                client=http,
            )
            begin("register_subtasks", "POST", f"/api/rpend/{task_id}")
            subtasks = frontend.register_subtasks(
                ["接口测试：go forward", "接口测试：turn left", "接口测试：stop"]
            )
            report["sub_task_ids"] = [task["sub_task_id"] for task in subtasks]
            complete()
            begin(
                "upload_dataset",
                "POST",
                f"/api/rpend/{task_id}/{subtasks[0]['sub_task_id']}",
            )
            dataset = frontend.report(
                subtasks[0]["sub_task_id"],
                "astar",
                {"steps": 3, "note": "RPent frontend smoke; no robot motion"},
                plan_id=None,
            )
            if not isinstance(dataset, dict):
                raise ServiceError("dataset creation must return a JSON object with id")
            report["dataset_id"] = parse_response_id(dataset.get("id"))
            complete()
            begin("query_after", "GET", "/api/rpend")
            response = http.get(endpoint + "/api/rpend")
            response.raise_for_status()
            response.json()
            complete(response.status_code)
            report["ok"] = True
            report["stage"] = "done"
    except (httpx.HTTPError, ServiceError, ValueError, AttributeError) as exc:
        cause = exc
        while not isinstance(cause, httpx.HTTPError) and cause.__cause__ is not None:
            cause = cause.__cause__
        report["error_type"] = type(cause).__name__
        report["error"] = str(exc) or type(cause).__name__
        if report["requests"]:
            report["requests"][-1].update(
                error_type=report["error_type"],
                error=report["error"],
                elapsed_s=round(time.monotonic() - stage_started, 3),
            )
        if isinstance(cause, httpx.HTTPStatusError):
            report["status_code"] = cause.response.status_code
    finally:
        report["elapsed_s"] = round(time.monotonic() - started, 3)
        write_json_atomic(output_dir / "frontend_check.json", report)
    return report


def main() -> int:
    """Run the standalone frontend HTTP check and print the created IDs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("RPENT_FRONTEND_ENDPOINT") or DEFAULT_FRONTEND_ENDPOINT,
    )
    parser.add_argument("--timeout-s", type=float, default=10)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    output_dir = args.output_dir or get_repo_root() / "logs" / (
        "frontend-check-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:6]
    )
    report = check_frontend(
        args.endpoint, output_dir, timeout_s=args.timeout_s, progress=True
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Report: {output_dir / 'frontend_check.json'}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
