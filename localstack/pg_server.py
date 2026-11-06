#!/usr/bin/env python3
"""Local PostgreSQL supervisor for weave-thinker-client.

基于 PyPI `embedded-postgres` 轮子（PostgreSQL 18 + pgvector 0.8.6 预编译，六平台）。
零系统依赖：无需 Docker/root/系统 PostgreSQL。

实现要点：轮子自带的自动启动强制 unix-socket-only（`-h ""` 覆盖 postgresql.conf），
而后端 SQLAlchemy/asyncpg 需要 host:port —— 本 supervisor 直接用轮子内的
initdb/pg_ctl 二进制自管：监听 127.0.0.1:<port>（回环）+ 数据目录内 socket。

用法（由 script/*/localstack.* 调用）：
  pg_server.py start   --data-dir DIR --state-file FILE [--database NAME] [--port N]
  pg_server.py exec    --state-file FILE --sql "SQL" [--database NAME]
  pg_server.py stop    --state-file FILE

start 为常驻进程（前台），收到 SIGTERM/SIGINT 时 pg_ctl fast stop；PID 由外层脚本记录。
"""
import argparse
import json
import signal
import sys
import time
from pathlib import Path

import embedded_postgres as ep


def _state_path(p: str) -> Path:
    return Path(p).expanduser().resolve()


def _base_uri(port: int, database: str) -> str:
    return f'postgresql://postgres@127.0.0.1:{port}/{database}'


def _psql(uri: str, sql: str) -> str:
    # 注意：不要传 pgdata= —— 包装器会注入 psql 不支持的 -D 参数；连接信息全在 URI。
    return ep.psql(['-v', 'ON_ERROR_STOP=1', '-X', '-A', '-t', uri, '-c', sql])


def _pg_ctl(data_dir: Path, args: list, timeout: int = 30) -> str:
    return ep.pg_ctl(args, pgdata=data_dir, timeout=timeout)


def cmd_start(args) -> int:
    data_dir = Path(args.data_dir).expanduser().resolve()
    data_dir.parent.mkdir(parents=True, exist_ok=True)

    stopping = {'flag': False}

    def _handler(signum, frame):  # noqa: ARG001
        stopping['flag'] = True

    # 先装信号处理器，再进入可能较久的 initdb/start（消除就绪前窗口孤儿进程）
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)

    if not (data_dir / 'PG_VERSION').exists():
        print('initdb...', flush=True)
        ep.initdb(['-U', 'postgres', '-A', 'trust', '--encoding=UTF8', '--no-locale'],
                  pgdata=data_dir, timeout=60)

    # 已在运行则复用（reconcile：崩溃残留的 supervisor 不再导致 start 失败）
    running = True
    try:
        _pg_ctl(data_dir, ['status'], timeout=10)
    except Exception:
        running = False
    if not running:
        log = data_dir / 'postgres.log'
        # macOS `sun_path` 上限 104 字节：Unix socket 目录必须**短**。旧实现把 socket
        # 放在 pgdata 内，长 userData（/private/var/folders/... 或长用户名）会让
        # initdb/pg_ctl 以「端口占用」式的误导性错误失败（W39 实测：短路径即通）。
        # 注意 tempfile.gettempdir() 在 macOS 返回 /var/folders/...（同样长）→ 固定 /tmp。
        sockdir = Path('/tmp') / f'wt-pg-{args.port}'
        try:
            sockdir.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            sockdir = Path('/tmp')
        sock = str(sockdir)
        _pg_ctl(data_dir, [
            '-w',
            '-o', f'-p {args.port} -c listen_addresses=127.0.0.1 -k {sock}',
            '-l', str(log),
            'start',
        ])

    db = args.database
    if not _psql(_base_uri(args.port, 'postgres'),
                 f"SELECT 1 FROM pg_database WHERE datname = '{db.replace(chr(39), chr(39)*2)}'").strip():
        _psql(_base_uri(args.port, 'postgres'), f'CREATE DATABASE "{db.replace(chr(34), chr(34)*2)}"')

    # 就绪校验（TCP）
    _psql(_base_uri(args.port, db), 'SELECT 1')

    pid_raw = (data_dir / 'postmaster.pid').read_text(encoding='utf-8').splitlines()[0]
    state = {
        'ok': True,
        'uri': _base_uri(args.port, db),
        'pid': int(pid_raw),
        'data_dir': str(data_dir),
        'database': db,
        'port': args.port,
        'started_at': time.time(),
    }
    sf = _state_path(args.state_file)
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps(state, indent=2), encoding='utf-8')
    print('LOCALSTACK_PG_READY ' + json.dumps(state), flush=True)

    stopping = {'flag': False}

    def _handler(signum, frame):  # noqa: ARG001
        stopping['flag'] = True

    while not stopping['flag']:
        time.sleep(0.5)
    _pg_ctl(data_dir, ['-w', '-m', 'fast', 'stop'])
    _state_path(args.state_file).unlink(missing_ok=True)
    print('LOCALSTACK_PG_STOPPED', flush=True)
    return 0


def cmd_exec(args) -> int:
    sf = _state_path(args.state_file)
    state = json.loads(sf.read_text(encoding='utf-8'))
    db = args.database or state.get('database', 'weavethinker')
    out = _psql(_base_uri(int(state['port']), db), args.sql)
    print(out, end='' if out.endswith('\n') else '\n')
    return 0


def cmd_stop(args) -> int:
    sf = _state_path(args.state_file)
    if not sf.exists():
        print('no state file')
        return 0
    state = json.loads(sf.read_text(encoding='utf-8'))
    data_dir = Path(state['data_dir'])
    try:
        _pg_ctl(data_dir, ['-w', '-m', 'fast', 'stop'])
        sf.unlink(missing_ok=True)
        print('stopped')
        return 0
    except Exception as e:
        print(f'stop failed: {e}', file=sys.stderr)
        return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('start')
    p.add_argument('--data-dir', required=True)
    p.add_argument('--state-file', required=True)
    p.add_argument('--database', default='weavethinker')
    p.add_argument('--port', type=int, default=55432)
    p.set_defaults(fn=cmd_start)

    p = sub.add_parser('exec')
    p.add_argument('--state-file', required=True)
    p.add_argument('--sql', required=True)
    p.add_argument('--database', default=None)
    p.set_defaults(fn=cmd_exec)

    p = sub.add_parser('stop')
    p.add_argument('--state-file', required=True)
    p.set_defaults(fn=cmd_stop)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == '__main__':
    sys.exit(main())
