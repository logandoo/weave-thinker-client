#!/usr/bin/env bash
# 本地全栈冒烟：Electron --smoke --local（自动拉起 PG+后端 → 健康+挂载+截图 → 清理退出）
set -euo pipefail
unset WEAVE_SKIP_LOCAL_STACK
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
mkdir -p "$ROOT/logs" "$ROOT/tests/screenshots" "$ROOT/tests/fixtures"
FIXTURE="$ROOT/tests/fixtures/bridge_fixture.txt"
printf 'weave-bridge-ok\n' > "$FIXTURE"
SHOT="$ROOT/tests/screenshots/desktop_local_mode.png"
[ -d "$ROOT/app/dist" ] || { echo "run script/linux/project_build.sh first"; exit 1; }
cd "$ROOT"
set +e
"$ROOT/node_modules/.bin/electron" "$ROOT/desktop" --smoke --local --screenshot="$SHOT" --fixture="$FIXTURE" 2>&1 | tee "$ROOT/logs/smoke_local.log"
RC="${PIPESTATUS[0]:-1}"
set -e
echo "smoke-local exit code: $RC" | tee -a "$ROOT/logs/smoke_local.log"
exit "$RC"
