#!/usr/bin/env bash
# Run a bounded RPent navigation check against the two local robot services.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
RPENT_ROOT="${RPENT_ROOT:-$ROOT}"
CONFIG="${RPENT_CONFIG_FILE:-$RPENT_ROOT/.env.local}"
PYTHON="${RPENT_PYTHON:-$RPENT_ROOT/.venv/bin/python}"
if [[ ! -f "$CONFIG" || ! -x "$PYTHON" ]]; then
  printf 'RPent environment or private configuration is missing: %s\n' "$RPENT_ROOT" >&2
  exit 1
fi
set -a
source "$CONFIG"
set +a
export NO_PROXY="localhost,127.0.0.1,::1${NO_PROXY:+,$NO_PROXY}"
export no_proxy="$NO_PROXY"
export PYDANTIC_AI_NO_BANNER=1
RUNS_DIR="$ROOT/output/rpent-local"
mkdir -p "$RUNS_DIR"
RUN_DIR="$(mktemp -d "$RUNS_DIR/run-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")"
printf '%s\n' "$RUN_DIR" > "$RUNS_DIR/latest-run.txt"
TASK="${1:-这是本机 Go2 MuJoCo 集成测试。先查询实时点位和机器人位姿，记住最初的起始位置与朝向；沿起始朝向前进 0.6 米，确认到达后，再回到最初的起始位置与朝向。每段均从实时位姿独立规划并执行，核对到达误差。任何失败或超时都停止并如实报告；全部完成后结束任务并报告结果。}"
printf '%s\n' "$TASK" > "$RUN_DIR.task.txt"
printf 'RPent test output: %s\n' "$RUN_DIR"
cd "$RPENT_ROOT"
exec "$PYTHON" -m rpent.cli.main \
  --robot unitree --unitree-mode mujoco \
  --nav-endpoint http://127.0.0.1:8765 --sim-endpoint http://127.0.0.1:8770 \
  --planner api --model "${RPENT_MODEL:-openai:deepseek-flash}" \
  --task "$TASK" --navigation-timeout-s 90 --planner-timeout-s 240 \
  --max-turns 12 --output-dir "$RUN_DIR" --memory-dir "$RUN_DIR/memory"
