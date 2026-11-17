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

"""Opt-in HTTP -> public RPent CLI -> real LightNav -> Go2 integration checks."""

from __future__ import annotations

import json
import os
import shlex
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import uvicorn

from robots.unitree.task_server import create_app
from tests.e2e_tests.offline_planner_server import (
    OFFLINE_MODEL_NAME,
    OfflinePlannerServer,
    ScriptedToolCall,
)

REPO = Path(__file__).resolve().parents[3]
PROJECT = REPO.parent
pytestmark = pytest.mark.skipif(
    os.environ.get("RPENT_RUN_UNITREE_VLN") != "1",
    reason="requires the operator-started Go2, A* and real LightNav services",
)


@contextmanager
def frontend_receiver(task_id):
    """Receive the existing frontend protocol locally, without external callbacks."""
    records = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            value = json.loads(
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
            )
            records.append({"path": self.path, "body": value})
            if self.path == "/api/rpend":
                body = {"id": task_id}
            elif self.path == f"/api/rpend/{task_id}":
                body = {"task_id": task_id, "items": [{"id": "sub-1"}]}
            else:
                assert self.path == f"/api/rpend/{task_id}/sub-1"
                assert value["source_type"] == "sil"
                body = {"ok": True, "id": "dataset-1"}
            payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", records
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@contextmanager
def task_api(jobs):
    """Use the actual HTTP app and its owned TTY child lifecycle."""
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    endpoint = f"http://127.0.0.1:{listener.getsockname()[1]}"
    app = create_app(repo_root=REPO, jobs_dir=jobs)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    thread = threading.Thread(
        target=lambda: server.run(sockets=[listener]), daemon=True
    )
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(0.01)
        yield endpoint
    finally:
        server.should_exit = True
        thread.join(40)
        listener.close()
        assert not thread.is_alive(), "owned task API did not drain its child"


def run_case(monkeypatch, *, cancel):
    """Exercise real inference, capture alignment, callbacks and bounded stopping."""
    sim = "http://127.0.0.1:8770"
    nav = "http://127.0.0.1:8765"
    observation = "http://127.0.0.1:8771"
    model = os.environ.get("RPENT_UNITREE_VLN_ENDPOINT", "ws://127.0.0.1:8050")
    output = Path(
        os.environ.get(
            "RPENT_UNITREE_VLN_OUTPUT",
            str(PROJECT / "output/vln-integration/policy-chain"),
        )
    )
    output = output / (("cancel-" if cancel else "hold-") + uuid4().hex[:10])
    output.mkdir(parents=True)
    task_id = output.name
    with httpx.Client(trust_env=False, timeout=10) as http:
        original_runner = http.get("http://127.0.0.1:8780/healthz").json()
        assert not original_runner["busy"] and not original_runner["attention_task_ids"]
        health = http.get(sim + "/health").json()
        assert health["control_mode"] == "idle" and not health["other_controllers"]
        origin = http.get(sim + "/pose").json()
        goal = {k: origin[k] for k in ("x", "y", "yaw")}
        action = {
            "instruction": "Turn left and continue inspecting the room."
            if cancel
            else "Stop here.",
            "sub_task_id": "sub-1",
            "timeout_s": 35.0,
        }
        if not cancel:
            action["expected_goal"] = goal
        script = (
            ScriptedToolCall(
                "register_navigation_subtasks",
                {
                    "commands": [
                        "VLN integration: inspect and stop"
                        if cancel
                        else "VLN integration: hold current map pose"
                    ]
                },
            ),
            ScriptedToolCall("run_vln_navigation", action),
            ScriptedToolCall(
                "finish",
                {
                    "status": "success" if not cancel else "stuck",
                    "summary": "bounded integration only",
                },
            ),
        )
        samples = []
        with (
            OfflinePlannerServer(script) as planner,
            frontend_receiver(task_id) as (frontend, callbacks),
        ):
            config = {
                "RPENT_VENV_DIR": str(PROJECT / ".venv"),
                "RPENT_NAV_ENDPOINT": nav,
                "RPENT_SIM_ENDPOINT": sim,
                "RPENT_VLN_ENDPOINT": model,
                "RPENT_OBSERVATION_ENDPOINT": observation,
                "RPENT_FRONTEND_ENDPOINT": frontend,
                "RPENT_RUNS_DIR": str(output / "runs"),
                "RPENT_MODEL": f"openai-chat:{OFFLINE_MODEL_NAME}",
                "RPENT_MEMORY_DIR": str(output / "memory"),
                "RPENT_MAX_TURNS": "5",
                "RPENT_PLANNER_TIMEOUT_S": "90",
                "RPENT_VLN_TIMEOUT_S": "40",
                "RPENT_VLN_INFERENCE_TIMEOUT_S": "3",
                "OPENAI_API_KEY": OFFLINE_MODEL_NAME,
                "OPENAI_BASE_URL": planner.base_url,
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
                "PYDANTIC_AI_NO_BANNER": "1",
            }
            config_path = output / "test.env"
            config_path.write_text(
                "".join(
                    f"export {key}={shlex.quote(value)}\n"
                    for key, value in config.items()
                )
            )
            config_path.chmod(0o600)
            monkeypatch.setenv("RPENT_CONFIG_FILE", str(config_path))
            with task_api(output / "jobs") as endpoint:
                parent = http.post(
                    frontend + "/api/rpend",
                    json={"task_name": "bounded VLN integration"},
                ).json()
                response = http.post(
                    endpoint + "/api/rpend/run",
                    json={
                        "task_id": parent["id"],
                        "task_name": "bounded VLN integration",
                    },
                )
                assert response.status_code == 202, response.text
                job = response.json()
                deadline = time.monotonic() + 75
                requested_stop = False
                try:
                    while time.monotonic() < deadline:
                        job = http.get(endpoint + f"/api/rpend/run/{task_id}").json()
                        vln = (job.get("navigation") or {}).get("vln") or {}
                        applied = (vln.get("control") or {}).get("applied") or {}
                        samples.append(
                            {
                                "time": time.time(),
                                "task_status": job["status"],
                                "vln_state": vln.get("state"),
                                "predictions": vln.get("predictions"),
                                "active_control_id": vln.get("active_control_id"),
                                "applied": applied,
                            }
                        )
                        if (
                            cancel
                            and not requested_stop
                            and vln.get("predictions", 0) >= 5
                            and max(
                                abs(applied.get("vx", 0)), abs(applied.get("wz", 0))
                            )
                            > 0.02
                        ):
                            stopped = http.post(
                                endpoint + f"/api/rpend/run/{task_id}/stop"
                            )
                            assert stopped.status_code == 200, stopped.text
                            requested_stop = True
                        if job["status"] not in {"starting", "running", "stopping"}:
                            break
                        time.sleep(0.05)
                    else:
                        raise AssertionError("bounded VLN HTTP task timed out")
                finally:
                    if (http.get(endpoint + f"/api/rpend/run/{task_id}").json())[
                        "status"
                    ] in {"starting", "running"}:
                        http.post(endpoint + f"/api/rpend/run/{task_id}/stop")
                    (output / "samples.json").write_text(
                        json.dumps(samples, ensure_ascii=False, indent=2) + "\n"
                    )
                    (output / "job-result.json").write_text(
                        json.dumps(job, ensure_ascii=False, indent=2) + "\n"
                    )
                vln = job["navigation"]["vln"]
                assert job["status"] == ("stopped" if cancel else "succeeded"), job
                assert vln["predictions"] >= 1 and vln["accepted_references"] >= 1
                assert vln["stop_confirmed"] and vln["active_control_id"] is None
                if cancel:
                    assert requested_stop and vln["state"] == "cancelled", job
                else:
                    assert vln["goal_verified"] and vln["state"] == "finished"
                    planner.assert_complete()
                final_health = http.get(sim + "/health").json()
                assert (
                    final_health["control_mode"] == "idle"
                    and not final_health["other_controllers"]
                )
                assert not http.get(endpoint + "/healthz").json()["busy"]
            (output / "callbacks.json").write_text(
                json.dumps(callbacks, ensure_ascii=False, indent=2) + "\n"
            )
            stages = [
                r["body"]["data"]["stage"]
                for r in callbacks
                if isinstance(r["body"], dict) and "data" in r["body"]
            ]
            assert stages == [
                "vln_started",
                "vln_error" if cancel else "vln_finished",
            ], stages
            (output / "verification.json").write_text(
                json.dumps(
                    {
                        "passed": True,
                        "scope": "local scripted high-level planner and frontend receiver; real LightNav model, Go2 images, MPC and DDS",
                        "goal_scope": "cancellation during motion"
                        if cancel
                        else "hold the measured starting map pose; not a visual navigation benchmark",
                        "task": job,
                        "callback_stages": stages,
                        "samples": len(samples),
                        "final_health": final_health,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
    return output


def test_vln_http_policy_chain_holds_known_pose_and_reports_verified_stop(monkeypatch):
    run_case(monkeypatch, cancel=False)


def test_vln_http_cancellation_stops_real_motion_and_releases_control(monkeypatch):
    run_case(monkeypatch, cancel=True)
