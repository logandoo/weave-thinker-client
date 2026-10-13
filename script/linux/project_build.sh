#!/usr/bin/env bash
# 构建：config 同步 → 依赖安装（首次）→ vite 构建。全平台客户端共用的 web 产物。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
mkdir -p "$ROOT/logs"
LOG="$ROOT/logs/build.log"
cd "$ROOT"
{
  echo "== [1/3] sync config =="
  node script/sync_config.mjs
  echo "== [2/3] npm install (if needed) =="
  if [ ! -d node_modules ]; then npm install --no-audit --no-fund; else echo "node_modules present — skip"; fi
  echo "== [3/3] vite build =="
  npm --workspace app run build
  echo "== BUILD OK =="
} 2>&1 | tee "$LOG"
