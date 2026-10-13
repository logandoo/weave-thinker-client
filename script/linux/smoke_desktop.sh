#!/usr/bin/env bash
# 桌面冒烟：Electron 以 --smoke 启动 → 校验挂载/桥/截图 → 退出码即结论。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
mkdir -p "$ROOT/logs" "$ROOT/tests/screenshots" "$ROOT/tests/fixtures"
FIXTURE="$ROOT/tests/fixtures/bridge_fixture.txt"
printf 'weave-bridge-ok\n' > "$FIXTURE"
SHOT="$ROOT/tests/screenshots/desktop_login.png"
[ -d "$ROOT/app/dist" ] || { echo "run script/linux/project_build.sh first"; exit 1; }
cd "$ROOT"
set +e
WEAVE_SKIP_LOCAL_STACK=1 WEAVE_USER_DATA_DIR="${WEAVE_USER_DATA_DIR:-/tmp/wt-smoke-profile}" "$ROOT/node_modules/.bin/electron" "$ROOT/desktop" --smoke --screenshot="$SHOT" --fixture="$FIXTURE" 2>&1 | tee "$ROOT/logs/smoke.log"
RC="${PIPESTATUS[0]:-1}"
set -e
echo "smoke exit code: $RC"
exit "$RC"
