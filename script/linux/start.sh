#!/usr/bin/env bash
# 启动桌面客户端（后台）。COV-2/A2：.pid 文件管理，禁止模式杀进程。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
mkdir -p "$ROOT/logs"
PIDF="$ROOT/logs/desktop.pid"
if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
  echo "already running (pid $(cat "$PIDF"))"; exit 0
fi
[ -d "$ROOT/app/dist" ] || { echo "run script/linux/project_build.sh first (app/dist missing)"; exit 1; }
[ -x "$ROOT/node_modules/.bin/electron" ] || { echo "electron missing — run script/linux/project_build.sh"; exit 1; }
cd "$ROOT"
nohup "$ROOT/node_modules/.bin/electron" "$ROOT/desktop" >"$ROOT/logs/desktop.log" 2>&1 &
echo $! > "$PIDF"
sleep 1
if kill -0 "$(cat "$PIDF")" 2>/dev/null; then
  echo "started pid $(cat "$PIDF") — log: logs/desktop.log"
else
  echo "start failed — see logs/desktop.log"; exit 1
fi
