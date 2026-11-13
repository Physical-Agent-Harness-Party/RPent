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

"""Exercise HTTP requests and real owned subprocesses without network or motion."""

import asyncio
import json
import os
import shlex
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from robots.unitree.task_server import (
    RunRequest,
    TaskConflict,
    TaskRunner,
    create_app,
    main,
)

CHILD = r"""
import hashlib
import json
import os
import signal
import sys
import time
from pathlib import Path

root = Path.cwd()
mode = os.environ.get("RPENT_TEST_MODE", "success")
output = root / "outputs" / hashlib.sha256(sys.argv[1].encode()).hexdigest()
output.mkdir(parents=True)
print("RPent output: " + str(output.relative_to(root)), flush=True)
with (root / "launches.jsonl").open("a") as handle:
    handle.write(json.dumps({"args": sys.argv[1:], "tty": sys.stdin.isatty()}) + "\n")
navigation = {
    "completed_navigation_goals": 3,
    "active_walk_id": None,
    "submission_uncertain": mode == "uncertain",
    "stop_error": None,
    "frontend": {"task_id": sys.argv[1], "sub_tasks": [{"sub_task_id": "child-1"}]},
}
(output / "states.json").write_text(json.dumps({"steps": [{"state": navigation}]}))
if mode in ("wait", "force"):
    def stop(sig, frame):
        print("SIGINT cleanup completed", flush=True)
        raise SystemExit(130)
    signal.signal(signal.SIGINT, signal.SIG_IGN if mode == "force" else stop)
    if mode == "force":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    (root / "ready").touch()
    while True:
        time.sleep(.05)
finish = None if mode in ("budget", "incomplete", "crash") else {
    "status": "failure" if mode == "failure" else "success", "summary": "done"
}
(output / "transcript_unitree_fake.json").write_text(json.dumps({
    "finish": finish, "error": None, "stats": {"turns_used": 16 if mode == "budget" else 9}
}))
if mode == "budget":
    print("Request budget of 16 reached.", flush=True)
if mode == "crash":
    raise SystemExit(7)
"""


class TaskServerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rpent runner ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "child.py").write_text(CHILD)
        (self.root / "run_rpent.sh").write_text(
            f'exec {shlex.quote(sys.executable)} {shlex.quote(str(self.root / "child.py"))} "$@"\n'
        )
        self.jobs = self.root / "jobs"
        self.mode = patch.dict(os.environ, {"RPENT_TEST_MODE": "success"})
        self.mode.start()
        self.addCleanup(self.mode.stop)

    def runner(self, **kwargs):
        runner = TaskRunner(self.root, self.jobs, **kwargs)
        self.addCleanup(runner.close)
        return runner

    def wait_finished(self, runner, task_id="parent-1"):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            result = runner.get(task_id)
            if result["status"] not in {"starting", "running", "stopping"}:
                return result
            time.sleep(0.01)
        self.fail("owned child did not finish")

    def wait_ready(self):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if (self.root / "ready").exists():
                return
            time.sleep(0.01)
        self.fail("owned child did not become ready")

    def test_real_child_has_tty_literal_arguments_and_success_artifacts(self):
        runner = self.runner()
        text = '去广告牌 "quoted"; $(touch injected); `touch injected2`\n再去母婴室'
        request = RunRequest(task_id="parent-1", task_name=text)
        started, created = runner.start(request)
        self.assertTrue(created)
        self.assertEqual(started["status"], "running")
        result = self.wait_finished(runner)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["navigation"]["completed_navigation_goals"], 3)
        self.assertEqual(result["finish"]["summary"], "done")
        self.assertEqual(result["stats"]["turns_used"], 9)
        launch = json.loads((self.root / "launches.jsonl").read_text())
        self.assertTrue(launch["tty"])
        self.assertEqual(launch["args"], ["parent-1", text])
        self.assertFalse((self.root / "injected").exists())
        self.assertFalse((self.root / "injected2").exists())
        self.assertEqual(runner.start(request), (result, False))
        with self.assertRaises(TaskConflict):
            runner.start(RunRequest(task_id="parent-1", task_name="different"))
        runner.close()
        restored = self.runner()
        self.assertEqual(restored.start(request), (result, False))
        self.assertEqual(
            len((self.root / "launches.jsonl").read_text().splitlines()), 1
        )

    def test_exit_code_zero_is_not_automatically_success(self):
        for mode, expected in (
            ("budget", "budget_exhausted"),
            ("incomplete", "incomplete"),
            ("failure", "failed"),
            ("crash", "failed"),
            ("uncertain", "requires_attention"),
        ):
            with self.subTest(mode=mode):
                os.environ["RPENT_TEST_MODE"] = mode
                runner = self.runner()
                task_id = "parent-" + mode
                runner.start(RunRequest(task_id=task_id, task_name="test"))
                result = self.wait_finished(runner, task_id)
                self.assertEqual(result["status"], expected)
                if mode == "crash":
                    self.assertEqual(result["exit_code"], 7)
                    self.assertIn("7", runner.get(task_id)["error"])
                runner.close()

    def test_http_runner_uses_the_existing_launch_script(self):
        repo = Path(__file__).resolve().parents[4]
        shutil.copyfile(repo / "run_rpent.sh", self.root / "run_rpent.sh")
        python = self.root / ".venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text(
            f"#!{sys.executable}\n"
            + r"""
import argparse, json, os, sys
from pathlib import Path
if sys.argv[1] == "-c":
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
assert sys.stdin.isatty()
assert sys.argv[1:3] == ["-m", "rpent.cli.main"]
parser = argparse.ArgumentParser()
for option in ("--output-dir", "--task", "--frontend-task-id"):
    parser.add_argument(option)
args, _ = parser.parse_known_args(sys.argv[3:])
Path("actual-args.json").write_text(json.dumps(vars(args)))
(Path(args.output_dir) / "transcript_unitree_fake.json").write_text(json.dumps({
    "finish": {"status": "success", "summary": "done"}, "error": None
}))
"""
        )
        python.chmod(0o755)
        config = self.root / "test.env"
        config.write_text("# No model or network is used in this test.\n")
        with patch.dict(
            os.environ,
            {
                "RPENT_CONFIG_FILE": str(config),
                "RPENT_VENV_DIR": str(python.parent.parent),
                "RPENT_RUNS_DIR": str(self.root / "actual-runs"),
            },
        ):
            runner = self.runner()
            text = '先去广告牌；保留 "引号" 和空格'
            runner.start(RunRequest(task_id="parent-1", task_name=text))
            result = self.wait_finished(runner)
        self.assertEqual(result["status"], "succeeded")
        args = json.loads((self.root / "actual-args.json").read_text())
        self.assertEqual(args["task"], text)
        self.assertEqual(args["frontend_task_id"], "parent-1")
        self.assertEqual(args["output_dir"], result["output_dir"])

    def test_only_one_child_and_stop_uses_graceful_sigint(self):
        os.environ["RPENT_TEST_MODE"] = "wait"
        runner = self.runner()
        request = RunRequest(task_id="parent-1", task_name="test")
        started, _ = runner.start(request)
        self.wait_ready()
        existing, created = runner.start(request)
        self.assertFalse(created)
        self.assertEqual(existing["pid"], started["pid"])
        with self.assertRaises(TaskConflict):
            runner.start(RunRequest(task_id="parent-2", task_name="test"))
        self.assertTrue(runner.health()["busy"])
        self.assertEqual(runner.stop("parent-1")["status"], "stopping")
        result = self.wait_finished(runner)
        self.assertEqual(result["status"], "stopped")
        self.assertFalse(result["forced_stop"])
        self.assertIn("SIGINT cleanup completed", runner.logs("parent-1"))
        self.assertEqual(runner.stop("parent-1"), result)
        self.assertFalse(runner.health()["busy"])
        os.environ["RPENT_TEST_MODE"] = "success"
        runner.start(RunRequest(task_id="parent-2", task_name="test"))
        self.assertEqual(self.wait_finished(runner, "parent-2")["status"], "succeeded")

    def test_progress_uses_newest_state_or_cleanup_report(self):
        os.environ["RPENT_TEST_MODE"] = "wait"
        runner = self.runner()
        runner.start(RunRequest(task_id="parent-1", task_name="test"))
        self.wait_ready()
        output = Path(runner.get("parent-1")["output_dir"])
        state_path = output / "states.json"
        state = json.loads(state_path.read_text())
        state_time = state_path.stat().st_mtime_ns
        report_path = output / "navigation" / "walks" / "latest.json"
        report_path.parent.mkdir(parents=True)
        report_path.write_text(
            json.dumps({"completed_navigation_goals": 4, "error": None})
        )
        os.utime(report_path, ns=(state_time + 1, state_time + 1))
        navigation = runner.get("parent-1")["navigation"]
        self.assertEqual(navigation["completed_navigation_goals"], 4)
        self.assertEqual(navigation["frontend"]["task_id"], "parent-1")
        state["steps"][-1]["state"].update(
            completed_navigation_goals=5, error="new error"
        )
        state_path.write_text(json.dumps(state))
        os.utime(state_path, ns=(state_time + 2, state_time + 2))
        navigation = runner.get("parent-1")["navigation"]
        self.assertEqual(navigation["completed_navigation_goals"], 5)
        self.assertEqual(navigation["error"], "new error")
        runner.stop("parent-1")
        self.wait_finished(runner)

    def test_forced_stop_requires_attention_and_blocks_new_motion(self):
        os.environ["RPENT_TEST_MODE"] = "force"
        runner = self.runner(stop_timeout_s=0.1)
        runner.start(RunRequest(task_id="parent-1", task_name="test"))
        self.wait_ready()
        runner.stop("parent-1")
        result = self.wait_finished(runner)
        self.assertEqual(result["status"], "requires_attention")
        self.assertTrue(result["forced_stop"])
        with self.assertRaises(TaskConflict):
            runner.start(RunRequest(task_id="parent-2", task_name="test"))

    def test_shutdown_drains_owned_child_and_releases_lease(self):
        os.environ["RPENT_TEST_MODE"] = "wait"
        runner = self.runner()
        runner.start(RunRequest(task_id="parent-1", task_name="test"))
        self.wait_ready()
        with self.assertRaises(BlockingIOError):
            TaskRunner(self.root, self.jobs)
        runner.close()
        self.assertEqual(runner.get("parent-1")["status"], "stopped")
        restored = self.runner()
        self.assertEqual(restored.get("parent-1")["status"], "stopped")

    def test_interrupted_persistent_id_is_not_replayed(self):
        os.environ["RPENT_TEST_MODE"] = "wait"
        runner = self.runner()
        request = RunRequest(task_id="parent-1", task_name="test")
        runner.start(request)
        self.wait_ready()
        runner.close()
        path = next(self.jobs.glob("*/job.json"))
        record = json.loads(path.read_text())
        record["status"] = "running"
        path.write_text(json.dumps(record))
        restored = self.runner()
        result, created = restored.start(request)
        self.assertFalse(created)
        self.assertEqual(result["status"], "interrupted")
        with self.assertRaises(TaskConflict):
            restored.start(RunRequest(task_id="parent-2", task_name="test"))
        self.assertEqual(restored.health()["attention_task_ids"], ["parent-1"])

    def test_http_validation_auth_status_logs_and_idempotency(self):
        async def scenario():
            # Periodic timers also let this run in sandboxes without socketpair wakeups.
            loop = asyncio.get_running_loop()

            def tick():
                loop.call_later(0.01, tick)

            tick()
            app = create_app(
                repo_root=self.root, jobs_dir=self.jobs, token="test-token"
            )
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://runner.test",
                ) as client:
                    self.assertEqual((await client.get("/healthz")).status_code, 200)
                    body = {"task_id": "parent-1", "task_name": "广告牌 → 母婴室"}
                    self.assertEqual(
                        (await client.post("/api/rpend/run", json=body)).status_code,
                        401,
                    )
                    client.headers["Authorization"] = "Bearer test-token"
                    self.assertEqual(
                        (await client.get("/api/rpend/run/missing")).status_code, 404
                    )
                    for invalid in (
                        {**body, "task_id": "../escape"},
                        {**body, "task_id": 123},
                        {**body, "task_name": "  "},
                        {**body, "command": "touch injected"},
                    ):
                        self.assertEqual(
                            (
                                await client.post("/api/rpend/run", json=invalid)
                            ).status_code,
                            422,
                        )
                    started = await client.post("/api/rpend/run", json=body)
                    self.assertEqual(started.status_code, 202)
                    duplicate = await client.post("/api/rpend/run", json=body)
                    self.assertEqual(duplicate.status_code, 200)
                    self.assertEqual(duplicate.json()["pid"], started.json()["pid"])
                    conflict = await client.post(
                        "/api/rpend/run", json={**body, "task_name": "different"}
                    )
                    self.assertEqual(conflict.status_code, 409)
                    for _ in range(300):
                        status = await client.get(started.json()["status_url"])
                        if status.json()["status"] == "succeeded":
                            break
                        await asyncio.sleep(0.01)
                    self.assertEqual(status.json()["status"], "succeeded")
                    logs = await client.get(
                        started.json()["logs_url"] + "?tail_bytes=8"
                    )
                    self.assertEqual(logs.status_code, 200)
                    self.assertLessEqual(len(logs.json()["text"]), 8)
                    self.assertEqual(
                        (
                            await client.get(
                                started.json()["logs_url"] + "?tail_bytes=999999"
                            )
                        ).status_code,
                        422,
                    )
                    stopped = await client.post(started.json()["stop_url"])
                    self.assertEqual(stopped.json()["status"], "succeeded")
                    os.environ["RPENT_TEST_MODE"] = "wait"
                    second = await client.post(
                        "/api/rpend/run", json={**body, "task_id": "parent-2"}
                    )
                    self.assertEqual(second.status_code, 202)
                    for _ in range(300):
                        if (self.root / "ready").exists():
                            break
                        await asyncio.sleep(0.01)
                    self.assertTrue((self.root / "ready").exists())
                    busy = await client.post(
                        "/api/rpend/run", json={**body, "task_id": "parent-3"}
                    )
                    self.assertEqual(busy.status_code, 409)
                    stopping = await client.post(second.json()["stop_url"])
                    self.assertEqual(stopping.json()["status"], "stopping")
                    for _ in range(300):
                        ended = await client.get(second.json()["status_url"])
                        if ended.json()["status"] == "stopped":
                            break
                        await asyncio.sleep(0.01)
                    self.assertEqual(ended.json()["status"], "stopped")
                    with patch(
                        "subprocess.Popen", side_effect=OSError("test launch failure")
                    ):
                        failed = await client.post(
                            "/api/rpend/run", json={**body, "task_id": "parent-3"}
                        )
                    self.assertEqual(failed.status_code, 503)
                    failed_again = await client.post(
                        "/api/rpend/run", json={**body, "task_id": "parent-3"}
                    )
                    self.assertEqual(failed_again.status_code, 200)
                    self.assertEqual(failed_again.json()["status"], "failed")

        asyncio.run(scenario())

    def test_public_binding_requires_server_side_token(self):
        with patch.dict(os.environ, {"RPENT_RUNNER_TOKEN": ""}):
            with self.assertRaises(SystemExit) as caught:
                main(["--host", "0.0.0.0"])
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
