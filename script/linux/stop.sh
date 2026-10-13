#!/usr/bin/env bash
# 停止桌面客户端：只杀 start.sh 记录的 PID。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PIDF="$ROOT/logs/desktop.pid"
if [ ! -f "$PIDF" ]; then echo "no pid file"; exit 0; fi
PID="$(cat "$PIDF")"
if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  for _ in 1 2 3 4 5; do kill -0 "$PID" 2>/dev/null || break; sleep 1; done
  if kill -0 "$PID" 2>/dev/null; then kill -9 "$PID"; fi
  echo "stopped pid $PID"
else
  echo "pid $PID not running"
fi
rm -f "$PIDF"
