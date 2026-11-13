#!/usr/bin/env bash
# Source this script to prepare MuJoCo navigation in the current Bash terminal.

if [[ "${BASH_SOURCE[0]}" == "$0" && "${1:-}" != "--help" && "${1:-}" != "-h" ]]; then
  printf '请使用 source robots/unitree/prepare_mujoco.sh，以保留环境变量和虚拟环境。\n' >&2
  exit 2
fi

_rpent_prepare_mujoco() {
  local skip_install=0 skip_model_check=0 start_simulator=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --skip-install) skip_install=1 ;;
      --skip-model-check) skip_model_check=1 ;;
      --start-simulator) start_simulator=1 ;;
      -h|--help)
        cat <<'HELP'
用法：source robots/unitree/prepare_mujoco.sh [选项]

  --skip-install       使用已经安装好 RPent 的虚拟环境，跳过 pip 安装
  --skip-model-check   跳过模型 API 的真实连通性请求
  --start-simulator    仿真器未运行时，允许调用 /simulator/start
  -h, --help          显示帮助

环境变量（已设置的值会沿用）：
  RPENT_NAV_ENDPOINT  默认 http://192.168.104.191:8765
  RPENT_SIM_ENDPOINT  默认 http://192.168.104.191:8770
  RPENT_MODEL         默认 openai:deepseek-flash
  RPENT_VENV_DIR      默认仓库根目录下的 .venv
  RPENT_PYTHON        创建虚拟环境的解释器，默认 python3（需要 3.10～3.12）

模型认证由 rpent-check-llm 读取，例如 OPENAI_API_KEY、OPENAI_BASE_URL。
脚本只做准备和检查，不发送行走指令。
HELP
        return 0
        ;;
      *) printf '未知选项：%s\n' "$1" >&2; return 2 ;;
    esac
    shift
  done

  local repo_root venv_dir python_bin
  repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd) || return 1
  venv_dir="${RPENT_VENV_DIR:-$repo_root/.venv}"
  if [[ "$venv_dir" != /* ]]; then
    venv_dir="$repo_root/$venv_dir"
  fi
  python_bin="$venv_dir/bin/python"

  if [[ ! -x "$python_bin" ]]; then
    if [[ "$skip_install" == 1 ]]; then
      printf '虚拟环境不存在：%s；首次运行请去掉 --skip-install。\n' "$venv_dir" >&2
      return 1
    fi
    printf '[prepare] 创建虚拟环境：%s\n' "$venv_dir"
    "${RPENT_PYTHON:-python3}" -m venv "$venv_dir" || return 1
  fi
  "$python_bin" -c 'import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] < (3, 13) else "RPent 需要 Python 3.10～3.12")' || return 1
  if [[ "$skip_install" == 0 ]]; then
    printf '[prepare] 安装 RPent 基础依赖\n'
    "$python_bin" -m pip install -e "$repo_root" || return 1
  fi

  local nav_endpoint="${RPENT_NAV_ENDPOINT:-http://192.168.104.191:8765}"
  local sim_endpoint="${RPENT_SIM_ENDPOINT:-http://192.168.104.191:8770}"
  local model="${RPENT_MODEL:-openai:deepseek-flash}"
  (
    cd -- "$repo_root" || exit 1
    if [[ "$skip_model_check" == 0 ]]; then
      printf '[prepare] 检查模型 API\n'
      "$python_bin" -m rpent.cli.check_llm --planner api --model "$model" || exit 1
    fi
    "$python_bin" - "$nav_endpoint" "$sim_endpoint" "$start_simulator" <<'PY'
import sys
from contextlib import ExitStack

from robots.unitree.http_client import ServiceError, map_identity
from robots.unitree.navigation_client import NavigationClient
from robots.unitree.simulation_client import SimulationClient


try:
    with ExitStack() as stack:
        nav = NavigationClient(sys.argv[1], timeout_s=5.0)
        stack.callback(nav.close)
        sim = SimulationClient(sys.argv[2], timeout_s=5.0)
        stack.callback(sim.close)

        def check_health():
            planning, simulation = nav.health(), sim.health()
            if map_identity(planning) != map_identity(simulation):
                raise ServiceError("规划和仿真服务的 map_id/map_version 不一致")
            if simulation.get("other_controllers"):
                raise ServiceError("存在其他控制器，请先在服务端处理占用")
            if (simulation.get("walk") or {}).get("state") == "running":
                raise ServiceError("仿真服务已有行走任务，请等待或核对该任务")
            return simulation

        print("[prepare] 检查规划和仿真服务")
        health = check_health()
        if not health["simulator_running"]:
            if sys.argv[3] != "1":
                raise ServiceError("仿真器未运行；可重新 source 脚本并添加 --start-simulator")
            print("[prepare] 启动仿真器")
            sim.request("POST", "/simulator/start", timeout_s=30.0)
            health = check_health()
            if not health["simulator_running"]:
                raise ServiceError("启动请求已返回，但仿真器仍未运行，请检查服务端日志")
        pose = sim.get_pose()
        if pose["fallen"]:
            raise ServiceError("机器人已摔倒，请先在服务端恢复仿真")
        waypoints = nav.list_waypoints()
        if map_identity(waypoints) != map_identity(health):
            raise ServiceError("查询点位期间地图发生变化，请重新检查服务")
        if not waypoints["waypoints"]:
            raise ServiceError("规划服务没有可用点位")
        print("[prepare] 地图：", *map_identity(health))
        print("[prepare] 位姿：", {key: pose[key] for key in ("x", "y", "yaw")})
        print("[prepare] 点位：", ", ".join(goal["name"] for goal in waypoints["waypoints"]))
except (ServiceError, ValueError) as exc:
    raise SystemExit(f"准备失败：{exc}") from exc
PY
  ) || return 1

  # Activate only after all checks pass; failures keep the caller's environment.
  source "$venv_dir/bin/activate" || return 1
  export RPENT_NAV_ENDPOINT="$nav_endpoint"
  export RPENT_SIM_ENDPOINT="$sim_endpoint"
  export RPENT_MODEL="$model"
  printf '[prepare] 准备完成，当前终端已激活虚拟环境并设置 RPENT_NAV_ENDPOINT、RPENT_SIM_ENDPOINT、RPENT_MODEL。\n'
  printf '[prepare] 请核对上面的点位名称，然后运行文档第 3 节的导航命令。\n'
}

if _rpent_prepare_mujoco "$@"; then
  unset -f _rpent_prepare_mujoco
else
  unset -f _rpent_prepare_mujoco
  return 1
fi
