#!/usr/bin/env bash
# Copyright 2026 The RPent Authors.
# Licensed under the Apache License, Version 2.0 (the "License");
# https://www.apache.org/licenses/LICENSE-2.0

# 兼容旧入口；启动逻辑统一维护在 run_rpent.sh。
set -euo pipefail
rpent_launcher_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec bash "$rpent_launcher_dir/run_rpent.sh" "$@"
