#!/usr/bin/env python3
"""侧车单二进制入口：一个可执行文件承载本地栈的两种角色（PyInstaller 打包）。

用法：
  weave-server pg start --data-dir DIR --state-file FILE [--database N] [--port N]
  weave-server pg exec  --state-file FILE --sql "SQL" [--database N]
  weave-server pg stop  --state-file FILE
  weave-server gen-config           # env: LOCALSTACK_SERVER_DIR/LOCALSTACK_CONFIG_DIR/LOCALSTACK_HOME
  weave-server backend                # 需 cwd 含 config.toml；端口取 LOCALSTACK_BACKEND_PORT（默认 8159）

打包：script/linux/build_sidecar.sh（PyInstaller --onedir，embedding embedded_postgres）。
"""
import os
import sys
from pathlib import Path


def _pg(argv):
    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    import pg_server  # noqa: PLC0415 — 冻结后与入口同目录收集
    sys.argv = [sys.argv[0]] + argv
    return pg_server.main()


def _gen_config(argv):
    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    import gen_config  # noqa: PLC0415
    sys.argv = [sys.argv[0]] + argv
    return gen_config.main()


def _backend():
    port = int(os.environ.get('LOCALSTACK_BACKEND_PORT', '8159'))
    import uvicorn  # noqa: PLC0415
    from main import app  # noqa: PLC0415 — 由 --paths server 收集
    uvicorn.run(app, host='127.0.0.1', port=port)
    return 0


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == 'pg':
        return _pg(argv[1:])
    if argv and argv[0] == 'gen-config':
        return _gen_config(argv[1:])
    if argv and argv[0] == 'backend':
        return _backend()
    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main())
