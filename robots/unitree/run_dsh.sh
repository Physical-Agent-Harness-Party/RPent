#!/usr/bin/env bash
# Launch the official DSH source profile with the local RPent MCP configuration.
set -euo pipefail
if [[ "${1:-}" == '--help' || "${1:-}" == '-h' ]]; then
    echo 'Usage: bash robots/unitree/run_dsh.sh {headless|web} [DSH profile options and task]'
    exit 0
fi
unitree_dsh_profile="${1:-web}"
[[ $# -eq 0 ]] || shift
case "$unitree_dsh_profile" in
    headless|web) ;;
    *) echo 'Choose the headless or web profile.' >&2; exit 2 ;;
esac
unitree_dsh_runner_repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
source "$unitree_dsh_runner_repo/robots/unitree/dsh_local.env"
unitree_dsh_project=$(cd -- "$unitree_dsh_runner_repo/.." && pwd)
export PATH="$unitree_dsh_project/dsh-tools/node_modules/node/bin:$unitree_dsh_project/dsh-tools/node_modules/.bin:$PATH"
export NO_PROXY="localhost,127.0.0.1,::1${NO_PROXY:+,$NO_PROXY}"
export no_proxy="$NO_PROXY"
export NODE_USE_ENV_PROXY=1
[[ -n "${DEEPSEEK_API_KEY:-}" ]] || { echo 'Configure the existing private model API key first.' >&2; exit 2; }
[[ -n "${RPENT_RUNNER_TOKEN:-}" ]] || { echo 'Set RPENT_RUNNER_TOKEN in the private configuration.' >&2; exit 2; }
cd -- "$unitree_dsh_project/deepseek-harness"
exec pnpm dsh --profile "$unitree_dsh_profile" --patch "$unitree_dsh_runner_repo/robots/unitree/dsh_mcp.patch.yml" "$@"
