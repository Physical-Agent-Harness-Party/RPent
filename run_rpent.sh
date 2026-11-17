#!/usr/bin/env bash
# Copyright 2026 The RPent Authors.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# https://www.apache.org/licenses/LICENSE-2.0

set -euo pipefail

if [[ $# -eq 1 && "$1" == '--help' ]]; then
    cat <<'HELP'
用法：bash run_rpent.sh TASK_ID '自然语言任务'

先准备 .venv。默认 frontend 模式需要在网页后端创建父任务；local 模式接受调度端的任务 ID。
脚本自动加载仓库的 .env.local。
前端 API 默认 http://121.36.57.9:80，可用 RPENT_FRONTEND_ENDPOINT 覆盖。

可选环境变量：
  RPENT_CONFIG_FILE               自定义 Bash 配置文件
  RPENT_VENV_DIR                  已安装 RPent 的虚拟环境目录
  RPENT_NAV_ENDPOINT              A* 服务根地址
  RPENT_SIM_ENDPOINT              MuJoCo 控制服务根地址
  RPENT_VLN_ENDPOINT              可选 LightNav WebSocket 地址
  RPENT_OBSERVATION_ENDPOINT      VLN 第一视角观测服务根地址
  RPENT_VLN_TIMEOUT_S             单次 VLN 预算，默认 120 秒
  RPENT_VLN_INFERENCE_TIMEOUT_S   单次模型请求预算，默认 3 秒
  RPENT_MODEL                     模型，默认 openai:deepseek-flash
  RPENT_FRONTEND_SUBTASKS_FORMAT   array（默认）或 object
  RPENT_TASK_REPORTING            frontend（默认）或 local；local 不调用网页后端
  RPENT_NAVIGATION_TIMEOUT_S      单次行走预算，默认 300 秒
  RPENT_PLANNER_TIMEOUT_S         模型会话预算，默认 900 秒
  RPENT_MAX_TURNS                 模型请求上限，默认 16
  RPENT_RUNS_DIR                  运行输出父目录，默认 logs
  RPENT_MEMORY_DIR                可选独立本地记忆目录

完整说明：robots/unitree/FRONTEND_USAGE.zh-CN.md。
HELP
    exit 0
fi

if [[ $# -ne 2 || -z "${1//[[:space:]]/}" || -z "${2//[[:space:]]/}" ]]; then
    echo "用法：bash run_rpent.sh TASK_ID '自然语言任务'" >&2
    exit 2
fi

rpent_repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
rpent_task_reporting="${RPENT_TASK_REPORTING:-}"
rpent_config_file="${RPENT_CONFIG_FILE:-$rpent_repo_dir/.env.local}"
if [[ -f "$rpent_config_file" ]]; then
    source "$rpent_config_file"
elif [[ -n "${RPENT_CONFIG_FILE:-}" ]]; then
    echo "RPent configuration file not found: $rpent_config_file" >&2
    exit 2
fi

rpent_frontend_endpoint="${RPENT_FRONTEND_ENDPOINT:-http://121.36.57.9:80}"
rpent_frontend_args=()
case "${rpent_task_reporting:-${RPENT_TASK_REPORTING:-frontend}}" in
    frontend)
        rpent_frontend_args=(
            --frontend-task-id "$1"
            --frontend-endpoint "$rpent_frontend_endpoint"
            --frontend-subtasks-format "${RPENT_FRONTEND_SUBTASKS_FORMAT:-array}"
        )
        ;;
    local) ;;
    *) echo 'RPENT_TASK_REPORTING must be frontend or local.' >&2; exit 2 ;;
esac
rpent_python="${RPENT_VENV_DIR:-$rpent_repo_dir/.venv}/bin/python"
if [[ ! -x "$rpent_python" ]]; then
    echo "RPent Python not found: $rpent_python; run the MuJoCo preparation script first." >&2
    exit 2
fi

# 从其他目录调用时，也以仓库为基准解析路径。
rpent_python=$(cd -- "$(dirname -- "$rpent_python")" && pwd)/python
cd -- "$rpent_repo_dir"
"$rpent_python" -c 'import sys; from robots.unitree.frontend_client import validate_task_id; validate_task_id(sys.argv[1])' "$1"
mkdir -p -- "${RPENT_RUNS_DIR:-logs}"
rpent_run_dir=$(mktemp -d "${RPENT_RUNS_DIR:-logs}/rpend-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")
echo "RPent output: $rpent_run_dir"

rpent_vln_args=()
if [[ -n "${RPENT_VLN_ENDPOINT:-}" ]]; then
    rpent_vln_args=(
        --vln-endpoint "$RPENT_VLN_ENDPOINT"
        --observation-endpoint "${RPENT_OBSERVATION_ENDPOINT:-http://127.0.0.1:8771}"
        --vln-timeout-s "${RPENT_VLN_TIMEOUT_S:-120}"
        --vln-inference-timeout-s "${RPENT_VLN_INFERENCE_TIMEOUT_S:-3}"
    )
fi
rpent_memory_args=()
if [[ -n "${RPENT_MEMORY_DIR:-}" ]]; then
    rpent_memory_args=(--memory-dir "$RPENT_MEMORY_DIR")
fi

exec "$rpent_python" -m rpent.cli.main \
    --robot unitree --unitree-mode mujoco \
    --nav-endpoint "${RPENT_NAV_ENDPOINT:-http://192.168.104.191:8765}" \
    --sim-endpoint "${RPENT_SIM_ENDPOINT:-http://192.168.104.191:8770}" \
    "${rpent_vln_args[@]}" \
    "${rpent_memory_args[@]}" \
    --planner api --model "${RPENT_MODEL:-openai:deepseek-flash}" \
    --task="$2" "${rpent_frontend_args[@]}" \
    --navigation-timeout-s "${RPENT_NAVIGATION_TIMEOUT_S:-300}" \
    --planner-timeout-s "${RPENT_PLANNER_TIMEOUT_S:-900}" \
    --max-turns "${RPENT_MAX_TURNS:-16}" \
    --output-dir "$rpent_run_dir"
