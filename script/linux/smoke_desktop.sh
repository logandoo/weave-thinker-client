#!/usr/bin/env bash
# 桌面冒烟：Electron 以 --smoke 启动 → 校验挂载/桥/截图 → 退出码即结论。
# W39：默认仍跳过本地栈（快，~10s）。要跑**行为级探针**（语音路由 101/403、
# BUILD_ID 同代）必须带本地栈：
#   WEAVE_SMOKE_WITH_LOCAL=1 bash script/linux/smoke_desktop.sh
# 打包产物的同类探针在 DMG 实跑时默认开启（localRequested=true）。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
mkdir -p "$ROOT/logs" "$ROOT/tests/screenshots" "$ROOT/tests/fixtures"
FIXTURE="$ROOT/tests/fixtures/bridge_fixture.txt"
printf 'weave-bridge-ok\n' > "$FIXTURE"
SHOT="$ROOT/tests/screenshots/desktop_login.png"
[ -d "$ROOT/app/dist" ] || { echo "run script/linux/project_build.sh first"; exit 1; }
cd "$ROOT"
set +e
if [ "${WEAVE_SMOKE_WITH_LOCAL:-0}" = "1" ]; then
  unset WEAVE_SKIP_LOCAL_STACK
  echo "smoke mode: WITH local stack (behaviour probes active)"
else
  export WEAVE_SKIP_LOCAL_STACK=1
  echo "smoke mode: skip local stack (probes will report localProbes=skipped)"
fi
WEAVE_USER_DATA_DIR="${WEAVE_USER_DATA_DIR:-/tmp/wt-smoke-profile}" "$ROOT/node_modules/.bin/electron" "$ROOT/desktop" --smoke --screenshot="$SHOT" --fixture="$FIXTURE" 2>&1 | tee "$ROOT/logs/smoke.log"
RC="${PIPESTATUS[0]:-1}"
set -e
echo "smoke exit code: $RC"
exit "$RC"
