#!/usr/bin/env bash
# 本地全栈：PostgreSQL+pgvector（embedded-postgres 轮子）生命周期。
# 用法：localstack.sh start|stop|status|psql "SQL"|bootstrap
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LS="${LOCALSTACK_HOME:-${WEAVE_LOCALSTACK_HOME:-$ROOT/.localstack}}"
LEGACY_LOGDIR="$ROOT/logs"
SERVER="${LOCALSTACK_SERVER_DIR:-$ROOT/server}"
CONFDIR="${LOCALSTACK_CONFIG_DIR:-$SERVER}"
LOGDIR="$LS/logs"
VENV="$LS/venv"
PY="$VENV/bin/python"
PIP="$VENV/bin/pip"
DATA="$LS/pgdata"
STATE="$LS/pg_state.json"
# pid 文件解析：优先 HOME，其次 legacy（迁移兼容，防止旧路径进程失控）
pidfile_of() {  # $1=basename -> echo path (may be empty)
  if [ -f "$LOGDIR/$1.pid" ]; then echo "$LOGDIR/$1.pid"; return; fi
  if [ -f "$LEGACY_LOGDIR/$1.pid" ]; then echo "$LEGACY_LOGDIR/$1.pid"; return; fi
}
adopt_legacy() {  # $1=basename: 将 legacy pid 文件迁移到 HOME（存在且运行中时）
  if [ -f "$LEGACY_LOGDIR/$1.pid" ] && [ ! -f "$LOGDIR/$1.pid" ] && kill -0 "$(cat "$LEGACY_LOGDIR/$1.pid")" 2>/dev/null; then
    mv "$LEGACY_LOGDIR/$1.pid" "$LOGDIR/$1.pid"
    echo "adopted legacy $1 pid $(cat "$LOGDIR/$1.pid")"
  fi
}
port_owner_pid() { lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | head -1; }

PGLOG="$LOGDIR/localstack_pg.log"
PGPID="$LOGDIR/localstack_pg.pid"
mkdir -p "$LOGDIR" "$LS"

bootstrap() {
  if [ ! -x "$PY" ]; then
    local base=""
    for c in /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 python3.13 python3.12 python3.11 python3; do
      if command -v "$c" >/dev/null 2>&1; then base="$c"; break; fi
    done
    [ -n "$base" ] || { echo "no python3.11+ found"; exit 1; }
    echo "creating venv with $base"
    "$base" -m venv "$VENV"
  fi
  "$PIP" install --disable-pip-version-check --quiet -r "$ROOT/localstack/requirements-pg.txt"
}

BPID="$LOGDIR/backend_local.pid"
BLOG="$LOGDIR/backend_local.log"
BPORT="${LOCALSTACK_BACKEND_PORT:-8161}"

backend_start() {
  PY_BIN="${LOCALSTACK_PYTHON:-$VENV/bin/python}"
  export LOCALSTACK_HOME="$LS"
  [ -x "$PY_BIN" ] || { echo "backend python not found: $PY_BIN（设置 LOCALSTACK_PYTHON 或先跑 backend-bootstrap）"; exit 1; }
  if ! "$PY_BIN" -c 'import fastapi, uvicorn, sse_starlette, asyncpg' >/dev/null 2>&1; then
    if [ -n "${LOCALSTACK_PYTHON:-}" ]; then
      echo "LOCALSTACK_PYTHON 缺少后端依赖（fastapi）：$PY_BIN"; exit 1
    fi
    echo "首次运行：安装后端依赖到 .localstack/venv（可能需要几分钟）..."
    bootstrap
    "$PIP" install --disable-pip-version-check --quiet -r "$SERVER/requirements.txt"
  fi
  adopt_legacy backend_local
  if [ -f "$BPID" ] && kill -0 "$(cat "$BPID")" 2>/dev/null; then echo "backend already running (pid $(cat "$BPID"))"; exit 0; fi
  LOCALSTACK_CONFIG_DIR="$CONFDIR" LOCALSTACK_SERVER_DIR="$SERVER" python3 "$ROOT/localstack/gen_config.py" >/dev/null
  # exec 让 $! 即 uvicorn 主进程（避免包装子壳 pid 与真实进程不符）
  ( cd "$CONFDIR" && exec env LOCALSTACK_CONFIG_PATH="$CONFDIR/config.toml" nohup "$PY_BIN" -m uvicorn main:app --app-dir "$SERVER" --host 127.0.0.1 --port "$BPORT" >"$BLOG" 2>&1 ) &
  echo $! > "$BPID"
  echo "backend starting (pid $(cat "$BPID")) — waiting health..."
  for _ in $(seq 1 60); do
    if curl -sf -m 2 "http://127.0.0.1:$BPORT/" >/dev/null 2>&1; then
      SELF="$(cat "$BPID")"
      OWNER="$(port_owner_pid "$BPORT" || true)"
      if [ -n "$OWNER" ] && [ "$OWNER" != "$SELF" ]; then
        echo "backend port $BPORT held by FOREIGN pid $OWNER（非本栈进程；拒绝以他人健康冒充成功）"; exit 1
      fi
      echo "backend healthy: http://127.0.0.1:$BPORT (pid $SELF, owner_verified=${OWNER:-lsof-na})"
      exit 0
    fi
    if ! kill -0 "$(cat "$BPID")" 2>/dev/null; then echo "backend died — see $BLOG"; tail -5 "$BLOG"; exit 1; fi
    sleep 1
  done
  echo "backend health timeout — see $BLOG"; exit 1
}
# 部署排空（2026-10-06 W4，上游 scripts/stop.sh 契约移植，D-64）：
# 先 drain 拒收新 run，再轮询在途归零或超时（WT_DRAIN_TIMEOUT，默认 120s），
# 随后照常按 pid kill。端点不可达/无令牌/超时 → fail-open 不阻塞停止。
_drain_wait() {
  local base tok_header active deadline
  base="http://127.0.0.1:$BPORT"
  tok_header="X-Drain-Token: $(cat "$SERVER/.drain_token" 2>/dev/null || echo '')"
  curl -s -m 5 -X POST "$base/api/server/drain" \
    -H 'Content-Type: application/json' -H "$tok_header" -d '{"on": true}' >/dev/null 2>&1 || true
  deadline=$(( $(date +%s) + ${WT_DRAIN_TIMEOUT:-120} ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    active=$(curl -s -m 5 -H "$tok_header" "$base/api/server/active-runs" 2>/dev/null \
      | "$PY" -c "import sys,json;print(json.load(sys.stdin).get('active',''))" 2>/dev/null || echo "")
    if [ "$active" = "0" ]; then
      echo "backend drain 完成（交互 chat 无在途 run；后台/死磕/voice 另有持久化兜底）"
      return 0
    fi
    [ -z "$active" ] && return 0  # 端点不可达（旧版本/已死）→ 不阻塞
    sleep 3
  done
  echo "backend drain 超时（${WT_DRAIN_TIMEOUT:-120}s，仍有在途 run）——按既定流程停止"
  return 0
}

backend_stop() {
  adopt_legacy backend_local
  if [ -f "$BPID" ] && kill -0 "$(cat "$BPID")" 2>/dev/null; then
    _drain_wait
    kill "$(cat "$BPID")"
    for _ in $(seq 1 20); do kill -0 "$(cat "$BPID")" 2>/dev/null || break; sleep 0.5; done
    kill -0 "$(cat "$BPID")" 2>/dev/null && kill -9 "$(cat "$BPID")" || true
    echo "stopped backend"
  else echo "backend not running"; fi
  rm -f "$BPID"
}
backend_health() {
  curl -sf -m 3 "http://127.0.0.1:$BPORT/" && echo || { echo "backend unhealthy"; exit 1; }
}
backend_bootstrap() {
  bootstrap
  "$PIP" install --disable-pip-version-check --quiet -r "$SERVER/requirements.txt"
}

case "${1:-}" in
  bootstrap) bootstrap ;;
  backend-start) backend_start ;;
  backend-stop) backend_stop ;;
  backend-health) backend_health ;;
  backend-bootstrap) backend_bootstrap ;;
  start)
    bootstrap
    adopt_legacy localstack_pg
    if [ -f "$PGPID" ] && kill -0 "$(cat "$PGPID")" 2>/dev/null; then echo "pg already running (pid $(cat "$PGPID"))"; exit 0; fi
    nohup "$PY" "$ROOT/localstack/pg_server.py" start --data-dir "$DATA" --state-file "$STATE" \
      --port "${LOCALSTACK_PG_PORT:-55433}" >"$PGLOG" 2>&1 &
    echo $! > "$PGPID"
    for _ in $(seq 1 60); do
      if grep -q LOCALSTACK_PG_READY "$PGLOG" 2>/dev/null; then
        grep LOCALSTACK_PG_READY "$PGLOG" | tail -1
        exit 0
      fi
      if ! kill -0 "$(cat "$PGPID")" 2>/dev/null; then echo "pg failed to start — see $PGLOG"; tail -5 "$PGLOG"; exit 1; fi
      sleep 0.5
    done
    echo "timeout waiting for PG ready — see $PGLOG"; exit 1 ;;
  stop)
    backend_stop  # 先排空+停后端（drain 契约；未运行则 no-op）
    adopt_legacy localstack_pg
    if [ -f "$PGPID" ]; then
      PID="$(cat "$PGPID")"
      if kill -0 "$PID" 2>/dev/null; then
        kill "$PID"
        for _ in $(seq 1 20); do kill -0 "$PID" 2>/dev/null || break; sleep 0.5; done
        kill -0 "$PID" 2>/dev/null && kill -9 "$PID" || true
        echo "stopped pg (pid $PID)"
      else echo "pg not running"; fi
      rm -f "$PGPID"
    else echo "no pid file"; fi
    # 崩溃残留 reconcile：supervisor 已死但 postmaster 仍在时，用 pg_ctl 停库
    if [ -f "$DATA/postmaster.pid" ]; then
      "$PY" "$ROOT/localstack/pg_server.py" stop --state-file "$STATE" >/dev/null 2>&1 || true
    fi ;;
  status)
    if [ -f "$PGPID" ] && kill -0 "$(cat "$PGPID")" 2>/dev/null; then
      echo "pg running (pid $(cat "$PGPID"))"; cat "$STATE" 2>/dev/null || true
    else echo "pg not running"; fi ;;
  psql)
    [ $# -ge 2 ] || { echo 'usage: localstack.sh psql "SQL" [database]'; exit 1; }
    bootstrap
    "$PY" "$ROOT/localstack/pg_server.py" exec --state-file "$STATE" --sql "$2" ${3:+--database "$3"} ;;
  *) echo "usage: $0 start|stop|status|psql \"SQL\" [db]|bootstrap"; exit 1 ;;
esac
