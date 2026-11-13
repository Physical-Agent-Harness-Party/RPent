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

"""Exercise sourced preparation with a fake interpreter and in-memory HTTP."""

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[4] / "robots/unitree/prepare_mujoco.sh"

RUNNER = r"""
import json
import os
import sys
from pathlib import Path

events = Path(os.environ["PREP_TEST_EVENTS"])
scenario = os.environ.get("PREP_TEST_SCENARIO", "ready")

def record(event):
    with events.open("a") as handle:
        handle.write(json.dumps(event) + "\n")

if sys.argv[1:3] == ["-m", "pip"]:
    record({"command": "install"})
    raise SystemExit(1 if scenario == "install-error" else 0)
if sys.argv[1:3] == ["-m", "rpent.cli.check_llm"]:
    record({"command": "model"})
    raise SystemExit(1 if scenario == "model-error" else 0)
if sys.argv[1] == "-c":
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
if sys.argv[1] != "-":
    raise AssertionError(sys.argv)

import httpx
import robots.unitree.navigation_client
import robots.unitree.simulation_client

identity = {"map_id": "test_room", "map_version": "v1"}
running = scenario not in ("stopped", "map")

def respond(request):
    global running
    host, path = request.url.host, request.url.path
    record({"method": request.method, "host": host, "path": path})
    if path == "/health":
        result = {"ok": True, **identity}
        if host == "sim.test":
            result.update(
                map_version="v2" if scenario == "map" else "v1",
                simulator_running=running,
                other_controllers=["controller"] if scenario == "controller" else [],
                walk={"state": "running" if scenario == "busy" else "finished"},
            )
        return httpx.Response(200, json=result)
    if host == "sim.test" and path == "/simulator/start":
        assert request.method == "POST"
        running = True
        return httpx.Response(200, json={"ok": True})
    if host == "sim.test" and path == "/pose":
        return httpx.Response(200, json={
            "ok": True, "x": 0.0, "y": 0.0, "yaw": 0.0,
            "fallen": scenario == "fallen",
        })
    if host == "nav.test" and path == "/waypoints":
        goals = [] if scenario == "empty" else [{
            "id": "wp_01", "name": "广告牌",
            "stand_pose": {"x": 1.0, "y": 0.0, "yaw": 0.0},
        }]
        return httpx.Response(200, json={"ok": True, **identity, "waypoints": goals})
    raise AssertionError(f"Unexpected request: {request.method} {request.url}")

real_client = httpx.Client

def client(*args, **kwargs):
    kwargs["transport"] = httpx.MockTransport(respond)
    return real_client(*args, **kwargs)

httpx.Client = client
sys.argv = sys.argv[1:]
exec(compile(sys.stdin.read(), "prepare-preflight", "exec"), {"__name__": "__main__"})
"""

SOURCE = """
prepare_script=$1
shift
source "$prepare_script" "$@"
prepare_status=$?
printf '\nACTIVE_ENV=%s\n' "${VIRTUAL_ENV:-}"
printf 'NAV=%s\nSIM=%s\nMODEL=%s\n' "$RPENT_NAV_ENDPOINT" "$RPENT_SIM_ENDPOINT" "$RPENT_MODEL"
exit "$prepare_status"
"""


class PrepareMujocoTests(unittest.TestCase):
    """Check caller environment, readiness rejection, and explicit startup."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.venv = root / "venv with spaces"
        self.events_path = root / "events.jsonl"
        bin_dir = self.venv / "bin"
        bin_dir.mkdir(parents=True)
        interpreter = bin_dir / "python"
        interpreter.write_text(f"#!{sys.executable}\n" + RUNNER)
        interpreter.chmod(0o755)
        (bin_dir / "activate").write_text(
            f"export VIRTUAL_ENV={shlex.quote(str(self.venv))}\n"
            f'export PATH={shlex.quote(str(bin_dir))}:"$PATH"\n'
        )
        self.env = os.environ.copy()
        self.env.pop("VIRTUAL_ENV", None)
        self.env.pop("OPENAI_API_KEY", None)
        self.env.update(
            RPENT_VENV_DIR=str(self.venv),
            RPENT_NAV_ENDPOINT="http://nav.test:8765",
            RPENT_SIM_ENDPOINT="http://sim.test:8770",
            RPENT_MODEL="openai-chat:offline",
            PREP_TEST_EVENTS=str(self.events_path),
        )

    def prepare(self, *options, scenario="ready"):
        return subprocess.run(
            ["bash", "-c", SOURCE, "prepare-test", str(SCRIPT), *options],
            env={**self.env, "PREP_TEST_SCENARIO": scenario},
            capture_output=True,
            text=True,
            timeout=30,
        )

    def events(self):
        if not self.events_path.exists():
            return []
        return [json.loads(line) for line in self.events_path.read_text().splitlines()]

    def test_preparation_exports_configuration_and_activates_in_caller(self):
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"ACTIVE_ENV={self.venv}", result.stdout)
        self.assertIn("NAV=http://nav.test:8765", result.stdout)
        self.assertIn("SIM=http://sim.test:8770", result.stdout)
        self.assertIn("MODEL=openai-chat:offline", result.stdout)
        self.assertEqual(
            [event["command"] for event in self.events() if "command" in event],
            ["install", "model"],
        )
        self.assertFalse(any(event.get("method") == "POST" for event in self.events()))

    def test_skip_options_keep_service_checks(self):
        result = self.prepare("--skip-install", "--skip-model-check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any("command" in event for event in self.events()))
        self.assertTrue(any(event.get("path") == "/pose" for event in self.events()))

    def test_stopped_simulator_requires_explicit_start_option(self):
        result = self.prepare(
            "--skip-install", "--skip-model-check", scenario="stopped"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--start-simulator", result.stderr)
        self.assertIn("ACTIVE_ENV=\n", result.stdout)
        self.assertFalse(any(event.get("method") == "POST" for event in self.events()))

    def test_start_option_starts_once_then_checks_pose(self):
        result = self.prepare("--start-simulator", scenario="stopped")
        self.assertEqual(result.returncode, 0, result.stderr)
        posts = [
            event["path"] for event in self.events() if event.get("method") == "POST"
        ]
        self.assertEqual(posts, ["/simulator/start"])
        self.assertTrue(any(event.get("path") == "/pose" for event in self.events()))

    def test_start_option_keeps_running_simulator(self):
        result = self.prepare("--start-simulator")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(event.get("method") == "POST" for event in self.events()))

    def test_map_mismatch_prevents_start(self):
        result = self.prepare("--start-simulator", scenario="map")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("map_id/map_version", result.stderr)
        self.assertFalse(any(event.get("method") == "POST" for event in self.events()))

    def test_occupied_or_fallen_simulator_is_rejected(self):
        for scenario in ("busy", "controller", "fallen"):
            with self.subTest(scenario=scenario):
                result = self.prepare("--start-simulator", scenario=scenario)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("准备失败", result.stderr)
                self.assertIn("ACTIVE_ENV=\n", result.stdout)

    def test_empty_waypoints_are_rejected(self):
        result = self.prepare(scenario="empty")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("没有可用点位", result.stderr)

    def test_model_failure_prevents_service_requests_and_activation(self):
        result = self.prepare(scenario="model-error")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            [event["command"] for event in self.events() if "command" in event],
            ["install", "model"],
        )
        self.assertFalse(any("method" in event for event in self.events()))
        self.assertIn("ACTIVE_ENV=\n", result.stdout)

    def test_install_failure_prevents_model_and_service_requests(self):
        result = self.prepare(scenario="install-error")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.events(), [{"command": "install"}])

    def test_executing_instead_of_sourcing_is_rejected(self):
        result = subprocess.run(
            ["bash", str(SCRIPT)], env=self.env, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("source", result.stderr)
        self.assertEqual(self.events(), [])

    def test_help_is_available_without_installation_or_network(self):
        result = subprocess.run(
            ["bash", str(SCRIPT), "--help"],
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--start-simulator", result.stdout)
        self.assertEqual(self.events(), [])
