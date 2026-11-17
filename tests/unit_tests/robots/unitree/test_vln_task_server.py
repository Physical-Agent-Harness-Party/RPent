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

"""Check live VLN progress and terminal verification states through task records."""

from __future__ import annotations

import shlex
import sys
import time
from pathlib import Path

from robots.unitree.task_server import RunRequest, TaskRunner

CHILD = r"""
import json, time
from pathlib import Path
root = Path.cwd()
out = root / 'out'
vln = out / 'navigation/vln'
vln.mkdir(parents=True)
print('RPent output: out', flush=True)
def write(state):
    p = vln / 'latest.json'
    t = p.with_suffix('.tmp')
    t.write_text(json.dumps(state))
    t.replace(p)
def wait(name):
    end = time.monotonic() + 8
    while not (root / name).exists():
        if time.monotonic() > end: raise RuntimeError('test parent did not release child')
        time.sleep(.01)
write({'state':'starting_control', 'submission_uncertain':True})
(root / 'first-ready').touch()
wait('continue')
write({'state':'tracking', 'submission_uncertain':False, 'active_control_id':'control_1', 'predictions':3})
(root / 'second-ready').touch()
wait('finish')
active = (root / 'leave-active').exists()
write({'state':'finished' if active else 'verification_required', 'submission_uncertain':False,
       'active_control_id':'control_1' if active else None, 'control_id':'control_1',
       'stop_confirmed':not active, 'goal_verified':False, 'model_stop':True})
(out / 'transcript_unitree_fake.json').write_text(json.dumps({
    'finish':{'status':'success','summary':'a planner claim is not independent arrival evidence'}}))
"""


def wait_path(path: Path) -> None:
    end = time.monotonic() + 5
    while not path.exists():
        assert time.monotonic() < end, path
        time.sleep(0.01)


def finish(runner: TaskRunner) -> dict:
    end = time.monotonic() + 5
    while time.monotonic() < end:
        result = runner.get("task-1")
        if result["status"] not in {"starting", "running", "stopping"}:
            return result
        time.sleep(0.01)
    raise AssertionError("test child did not finish")


def make_runner(tmp_path: Path) -> TaskRunner:
    (tmp_path / "child.py").write_text(CHILD)
    (tmp_path / "run_rpent.sh").write_text(
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(tmp_path / 'child.py'))}\n"
    )
    return TaskRunner(tmp_path, tmp_path / "jobs")


def test_live_vln_progress_clears_uncertain_acceptance_and_preserves_verification(
    tmp_path,
):
    runner = make_runner(tmp_path)
    try:
        runner.start(RunRequest(task_id="task-1", task_name="VLN test"))
        wait_path(tmp_path / "first-ready")
        assert runner.get("task-1")["navigation"]["submission_uncertain"]
        (tmp_path / "continue").touch()
        wait_path(tmp_path / "second-ready")
        snapshot = runner.get("task-1")["navigation"]
        assert not snapshot["submission_uncertain"]
        assert snapshot["active_control_id"] == "control_1"
        assert snapshot["vln"]["predictions"] == 3
        (tmp_path / "finish").touch()
        result = finish(runner)
        assert result["status"] == "verification_required"
        assert result["navigation"]["vln"]["stop_confirmed"]
    finally:
        runner.close()


def test_child_exit_with_an_active_trajectory_lease_requires_reconciliation(tmp_path):
    (tmp_path / "leave-active").touch()
    runner = make_runner(tmp_path)
    try:
        runner.start(RunRequest(task_id="task-1", task_name="VLN test"))
        wait_path(tmp_path / "first-ready")
        (tmp_path / "continue").touch()
        wait_path(tmp_path / "second-ready")
        (tmp_path / "finish").touch()
        assert finish(runner)["status"] == "requires_attention"
    finally:
        runner.close()
