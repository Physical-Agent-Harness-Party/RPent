#!/usr/bin/env bash
# Build a Unitree (MuJoCo) memory corpus by running exploration tasks.
#
# Required environment:
#   NAV_ENDPOINT / SIM_ENDPOINT   e.g. http://192.168.104.191:8765 , :8770
#   OPENAI_API_KEY                planner key (also used by resolve-conflicts)
# Optional:
#   OPENAI_BASE_URL (default https://api.deepseek.com/v1)
#   MODEL (default openai-chat:deepseek-flash)
#   TASKS (newline-separated; defaults to three waypoint round trips)
#   MAX_TURNS (12) SESSIONS (2) ATTEMPTS (2)
#   MEM_DIR (memory/unitree_mem) OUT_ROOT (runs/memory_build)
#   AUTO_RESOLVE (0/1, default 1)  run resolve-conflicts --apply at the end
set -euo pipefail
REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"
MEMBER="${NAV_ENDPOINT:?set NAV_ENDPOINT}"; SIM="${SIM_ENDPOINT:?set SIM_ENDPOINT}"
: "${OPENAI_API_KEY:?set OPENAI_API_KEY}"
BASE_URL="${OPENAI_BASE_URL:-https://api.deepseek.com/v1}"
MODEL="${MODEL:-openai-chat:deepseek-flash}"
MAX_TURNS="${MAX_TURNS:-12}"; SESSIONS="${SESSIONS:-2}"; ATTEMPTS="${ATTEMPTS:-2}"
MEM_DIR="${MEM_DIR:-$REPO_ROOT/memory/unitree_mem}"
OUT_ROOT="${OUT_ROOT:-$REPO_ROOT/runs/memory_build}"
AUTO_RESOLVE="${AUTO_RESOLVE:-1}"
run_rpent() {
  if command -v rpent >/dev/null 2>&1; then rpent "$@"; else python -m rpent.cli.main "$@"; fi
}
run_mem() {
  if command -v rpent-memory >/dev/null 2>&1; then rpent-memory "$@"; else python -m rpent.cli.memory "$@"; fi
}
mkdir -p "$MEM_DIR" "$OUT_ROOT"
SUMMARY="$OUT_ROOT/summary.tsv"
printf "task\tsessions\telapsed_s\tnotes\n" > "$SUMMARY"

DEFAULT_TASKS=$'先去广告牌，确认到达后，再去厕所通道\n去充电桩，确认到达后，再去广告牌\n先去厕所通道，确认到达后，再去充电桩'
TASKS_TEXT="${TASKS:-$DEFAULT_TASKS}"
i=0
while IFS= read -r task; do
  [ -z "$task" ] && continue
  i=$((i+1))
  OUT="$OUT_ROOT/run_$(printf '%02d' "$i")"
  rm -rf "$OUT"
  start=$(date +%s)
  run_rpent --robot unitree --unitree-mode mujoco \
    --nav-endpoint "$MEMBER" --sim-endpoint "$SIM" \
    --planner api --model "$MODEL" --base-url "$BASE_URL" \
    --task "$task" \
    --explore --explore-sessions "$SESSIONS" --explore-attempts-per-session "$ATTEMPTS" \
    --max-turns "$MAX_TURNS" \
    --memory-dir "$MEM_DIR" --output-dir "$OUT" --verbose \
    > "$OUT.log" 2>&1 || true
  end=$(date +%s)
  notes=$(ls "$MEM_DIR/global" 2>/dev/null | wc -l)
  printf "%s\t%s\t%s\t%s\n" "$task" "$SESSIONS" "$((end-start))" "$notes" >> "$SUMMARY"
  echo "[$i] done in $((end-start))s, global notes=$notes"
done <<< "$TASKS_TEXT"

run_mem --memory-dir "$MEM_DIR" build-index >/dev/null || true
echo "--- validate ---"; run_mem --memory-dir "$MEM_DIR" validate
if [ "$AUTO_RESOLVE" = "1" ]; then
  echo "--- resolve conflicts ---"
  run_mem --memory-dir "$MEM_DIR" resolve-conflicts --apply || true
fi
echo "--- summary ($SUMMARY) ---"; cat "$SUMMARY"
