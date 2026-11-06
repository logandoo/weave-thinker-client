#!/usr/bin/env bash
# 构建本地栈侧车（PyInstaller onedir）：weave-server = pg 管理 + 后端 单二进制。
# 前置：bash script/linux/localstack.sh backend-bootstrap（venv 含完整依赖）。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VENV="$ROOT/.localstack/venv"
PY="$VENV/bin/python"
[ -x "$PY" ] || { echo "venv missing — run: bash script/linux/localstack.sh backend-bootstrap"; exit 1; }

# 工具版本锁定（LINUX_DEB_UPSTREAM_FIXES 缺陷 3）：浮动安装不可复现。
"$PY" -m pip install --disable-pip-version-check --quiet "pyinstaller==6.22.3"

SEP=':'
case "$(uname -s)" in MINGW*|CYGWIN*|MSYS*|Windows*) SEP=';';; esac
OUT="$ROOT/dist_sidecar"
rm -rf "$OUT" "$ROOT/build/pyinstaller"

# embedded_postgres 的 pginstall（PostgreSQL 二进制 + 扩展 .so + share/include）
# 必须整树随包：--collect-all 只收集到顶层 5 个 .so，扩展模块缺失 → initdb FATAL
# （dict_snowball 等 75/80 个扩展 .so 未收，`could not access file "dict_snowball"`）。
EP_DIR="$("$PY" -c 'import embedded_postgres, os; print(os.path.dirname(embedded_postgres.__file__))')"
[ -d "$EP_DIR/pginstall" ] || { echo "embedded_postgres pginstall not found at $EP_DIR/pginstall"; exit 1; }

# PYTHONPATH=server：spec 内 collect_submodules('app.tools') 需可导入该包，
# 否则静默收集 0 个（冻结包缺全部工具模块的根因腿之一）。
PYTHONPATH="$ROOT/server" "$PY" -m PyInstaller --noconfirm --clean --onedir --name weave-server \
  --distpath "$OUT" --workpath "$ROOT/build/pyinstaller" --specpath "$ROOT/build" \
  --paths "$ROOT/server" \
  --collect-all embedded_postgres \
  --add-data "$EP_DIR/pginstall${SEP}embedded_postgres/pginstall" \
  --collect-submodules uvicorn \
  --collect-submodules app.tools \
  --hidden-import app \
  --add-data "$ROOT/server/Fonts${SEP}Fonts" \
  --add-data "$ROOT/server/skills${SEP}skills" \
  --add-data "$ROOT/server/app/vendor_js${SEP}app/vendor_js" \
  --add-data "$ROOT/server/app/services/omml_helper.py${SEP}app/services" \
  --add-data "$ROOT/server/config.toml.example${SEP}." \
  "$ROOT/localstack/sidecar_entry.py"

echo "== SIDECAR OK: $OUT/weave-server =="
