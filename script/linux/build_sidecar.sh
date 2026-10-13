#!/usr/bin/env bash
# 构建本地栈侧车（PyInstaller onedir）：weave-server = pg 管理 + 后端 单二进制。
# 前置：bash script/linux/localstack.sh backend-bootstrap（venv 含完整依赖）。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VENV="$ROOT/.localstack/venv"
PY="$VENV/bin/python"
[ -x "$PY" ] || { echo "venv missing — run: bash script/linux/localstack.sh backend-bootstrap"; exit 1; }

"$PY" -m pip install --disable-pip-version-check --quiet pyinstaller

SEP=':'
case "$(uname -s)" in MINGW*|CYGWIN*|MSYS*|Windows*) SEP=';';; esac
OUT="$ROOT/dist_sidecar"
rm -rf "$OUT" "$ROOT/build/pyinstaller"

# PYTHONPATH=server：spec 内 collect_submodules('app.tools') 需可导入该包，
# 否则静默收集 0 个（冻结包缺全部工具模块的根因腿之一）。
PYTHONPATH="$ROOT/server" "$PY" -m PyInstaller --noconfirm --clean --onedir --name weave-server \
  --distpath "$OUT" --workpath "$ROOT/build/pyinstaller" --specpath "$ROOT/build" \
  --paths "$ROOT/server" \
  --collect-all embedded_postgres \
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
