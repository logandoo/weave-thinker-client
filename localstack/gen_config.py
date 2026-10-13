#!/usr/bin/env python3
"""Generate server/config.toml for local mode from the public template.

本地全栈：数据库指向 localstack 的 PG（127.0.0.1:55432，见 pg_state.json 可覆盖），
JWT secret 随机生成并落盘（仅本机文件，git 忽略）。幂等：已存在时不覆盖（--force 例外）。
"""
import argparse
import json
import re
import secrets
import sys
from pathlib import Path

import os

ROOT = Path(__file__).resolve().parent.parent
SERVER = Path(os.environ.get('LOCALSTACK_SERVER_DIR', ROOT / 'server'))
CONFIG_DIR = Path(os.environ.get('LOCALSTACK_CONFIG_DIR', SERVER))
_TEMPLATE_FALLBACK = Path(__file__).resolve().parent / 'config.toml.example'
TEMPLATE = SERVER / 'config.toml.example'
if not TEMPLATE.exists() and _TEMPLATE_FALLBACK.exists():
    TEMPLATE = _TEMPLATE_FALLBACK
TARGET = CONFIG_DIR / 'config.toml'
LOCAL_HOME = Path(os.environ.get('LOCALSTACK_HOME', ROOT / '.localstack'))
STATE = LOCAL_HOME / 'pg_state.json'


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--force', action='store_true', help='overwrite existing config.toml')
    args = ap.parse_args()

    if not TEMPLATE.exists():
        print(f'missing template: {TEMPLATE}', file=sys.stderr)
        return 1
    if TARGET.exists() and not args.force:
        print(f'{TARGET} exists — skip (use --force to regenerate)')
        return 0

    try:
        pg = json.loads(STATE.read_text(encoding='utf-8'))
        pg_host, pg_port, pg_db = '127.0.0.1', int(pg.get('port', 55432)), pg.get('database', 'weavethinker')
    except Exception:
        pg_host, pg_port, pg_db = '127.0.0.1', 55432, 'weavethinker'
    import os
    pg_port = int(os.environ.get('LOCALSTACK_PG_PORT', pg_port))
    backend_port = int(os.environ.get('LOCALSTACK_BACKEND_PORT', 8161))

    text = TEMPLATE.read_text(encoding='utf-8')
    jwt = secrets.token_hex(32)

    def sub(pattern: str, repl: str, s: str, count: int = 1) -> str:
        out, n = re.subn(pattern, repl, s, count=count)
        assert n > 0, f'pattern not found: {pattern}'
        return out

    # 顺序：先通用占位清零（其余 key/secret），再做定向赋值（行首锚定，避免
    # 'username' 内的 'name' 子串误撞）。
    text = sub(r'(jwt_secret_key\s*=\s*)"[^"]*"', rf'\1"{jwt}"', text)
    text = text.replace('"<YOUR-HOME>', f'"{LOCAL_HOME.as_posix()}')
    text = re.sub(r'"<YOUR-[A-Z-]+>"', '""', text)
    text = re.sub(r'<YOUR-[A-Z-]+>', '', text)

    def set_kv(section: str, key: str, value: str, s: str) -> str:
        # 仅替换 section 标题之后第一处行首 `key = ...`
        pat = rf'(?m)^({key}\s*=).*$'
        idx = s.index(f'[{section}]')
        head, tail = s[:idx], s[idx:]
        out, n = re.subn(pat, rf'\1 {value}', tail, count=1)
        assert n > 0, f'{section}.{key} not found'
        return head + out

    text = set_kv('database', 'host', f'"{pg_host}"', text)
    text = set_kv('database', 'port', str(pg_port), text)
    text = set_kv('database', 'username', '"postgres"', text)
    text = set_kv('database', 'password', '""', text)
    text = set_kv('database', 'name', f'"{pg_db}"', text)
    text = set_kv('server', 'host', '"127.0.0.1"', text)
    text = set_kv('server', 'port', str(backend_port), text)

    leftovers = re.findall(r'<YOUR-[A-Z-]+>', text)
    if leftovers:
        print(f'unreplaced placeholders: {sorted(set(leftovers))}', file=sys.stderr)
        return 1

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(text, encoding='utf-8')
    TARGET.chmod(0o600)
    print(f'wrote {TARGET} (db={pg_host}:{pg_port}/{pg_db}, server port {backend_port})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
