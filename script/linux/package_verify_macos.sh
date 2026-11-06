#!/usr/bin/env bash
# 打包内容核验（W38 纪律：四层证据 + 负控 + 双向出处 + **可失败的断言**）。
#   L1 asar 字面扫：SPA 产物内含本波标记（正控 ≥1）+ 必不存在的负控 token（==0）
#   L2 sidecar PYZ 内容面：解档语义判（字节扫在压缩归档内必 0 命中，故字节扫不作为判据）
#   L3 双向出处：标记既在**上游源文件**又在**包内容**中出现（防「包内容=旧缓存」假象）
# 断言（评审 A4.9 Important #4 修正）：每条判据都是**会失败**的门——
#   任一正控标记在声明的作用面命中 <1 / 负控命中 >0 / PYZ 探针缺失或失败 / 探针未命中标记
#   → 退出码非 0（不再「打印表格后照样 exit 0」）。
# 用法：bash script/linux/package_verify_macos.sh <dmg> <out_file> <marker>:<label>:<scope> ...
#   scope ∈ {spa, sidecar, both}（缺省 both）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
DMG="${1:?usage: package_verify_macos.sh <dmg> <out_file> <marker>[:label][:scope] ...}"
OUT="${2:?}"
shift 2
NEG="WT_W38_NEGATIVE_CTRL_TOKEN"
PYI="$ROOT/.localstack/venv/bin/python"
UP_PATH="$(node -e 'const fs=require("fs");const t=fs.readFileSync("upstream.toml","utf8");const m=t.match(/^\s*path\s*=\s*"([^"]+)"/m);process.stdout.write(m[1])')"
MOUNT="$(mktemp -d)"
PROBE_OUT="$(mktemp)"
cleanup() {
  hdiutil detach "$MOUNT" -quiet >/dev/null 2>&1 || true
  rm -rf "$MOUNT" "$PROBE_OUT"
}
trap cleanup EXIT
rc=0
SPA_MARKERS=(); SIDE_MARKERS=()
for spec in "$@"; do
  marker="$(printf '%s' "$spec" | cut -d: -f1)"
  scope="$(printf '%s' "$spec" | awk -F: '{print (NF>=3 ? $NF : "both")}')"
  case "$scope" in
    spa) SPA_MARKERS+=("$marker") ;;
    sidecar) SIDE_MARKERS+=("$marker") ;;
    both) SPA_MARKERS+=("$marker"); SIDE_MARKERS+=("$marker") ;;
    *) echo "ASSERT FAIL: 未知 scope '$scope'（spec=${spec}）"; rc=1 ;;
  esac
done
{
  echo "# 打包内容核验 — $(basename "$DMG")"
  echo "sha256: $(shasum -a 256 "$DMG" | awk '{print $1}')"
  echo "size:   $(du -h "$DMG" | awk '{print $1}')"
  echo
  echo "== [L1/L3] asar 字面扫（正控断言 ≥1 + 负控断言 ==0 + 双向出处）=="
  hdiutil attach "$DMG" -nobrowse -readonly -mountpoint "$MOUNT" >/dev/null
  APP="$(find "$MOUNT" -maxdepth 1 -name '*.app' | head -1)"
  echo "app: $APP"
  ASAR="$APP/Contents/Resources/app.asar"
  [ -f "$ASAR" ] || { echo "FAIL: app.asar missing"; exit 1; }
  for spec in "$@"; do
    marker="$(printf '%s' "$spec" | cut -d: -f1)"
    label="$(printf '%s' "$spec" | cut -d: -f2)"
    scope="$(printf '%s' "$spec" | awk -F: '{print (NF>=3 ? $NF : "both")}')"
    [ "$scope" = "sidecar" ] && continue
    n="$(LC_ALL=C grep -c -a -F -o -- "$marker" "$ASAR" 2>/dev/null || true)"
    n="${n:-0}"
    printf '  asar  %-28s hits=%s  (%s)\n' "$marker" "$n" "$label"
    if [ "$n" -lt 1 ]; then
      echo "  ASSERT FAIL: 正控标记 '$marker' 在 app.asar 内命中 ${n}（期望 ≥1）"
      rc=1
    fi
  done
  neg="$(LC_ALL=C grep -c -a -F -o -- "$NEG" "$ASAR" 2>/dev/null || true)"
  printf '  asar  %-28s hits=%s  (负控·必须为 0)\n' "$NEG" "${neg:-0}"
  if [ "${neg:-0}" -ne 0 ]; then
    echo "  ASSERT FAIL: 负控标记 '$NEG' 命中 ${neg}（期望 0）——匹配器恒真"
    rc=1
  fi
  echo
  echo "== [L2] sidecar PYZ 内容面（解档语义判）+ TOC =="
  SIDECAR="$APP/Contents/Resources/sidecar/weave-server"
  [ -d "$SIDECAR" ] || { echo "FAIL: sidecar missing"; exit 1; }
  echo "sidecar: $SIDECAR"
  echo "-- PYZ TOC 模块清单（voice 相关）--"
  "$PYI" -m PyInstaller.utils.cliutils.archive_viewer -r -b "$SIDECAR/weave-server" 2>/dev/null \
    | grep -E "app\.services\.voice|app\.api\.voice" | head -40 || true
  PROBE="$ROOT/script/linux/sidecar_pyz_probe.py"
  if [ ! -f "$PROBE" ]; then
    echo "  ASSERT FAIL: PYZ 内容探针缺失：$PROBE"
    rc=1
  elif [ "${#SIDE_MARKERS[@]}" -eq 0 ]; then
    echo "  (no sidecar-scoped markers declared)"
  elif ! "$PYI" "$PROBE" "$SIDECAR/weave-server" "${SIDE_MARKERS[@]}" > "$PROBE_OUT" 2>&1; then
    echo "  ASSERT FAIL: PYZ 内容探针执行失败"
    sed -n '1,20p' "$PROBE_OUT"
    rc=1
  else
    echo "-- PYZ 内容面探针输出 --"
    sed -n '1,60p' "$PROBE_OUT"
    for marker in "${SIDE_MARKERS[@]}"; do
      if ! grep -q "^${marker} " "$PROBE_OUT"; then
        echo "  ASSERT FAIL: 标记 '$marker' 在侧车 PYZ 内容面无命中"
        rc=1
      fi
    done
  fi
  echo
  echo "== [L3 双向出处] 标记在**上游源文件**与**包内容**两侧同时出现 =="
  for spec in "$@"; do
    marker="$(printf '%s' "$spec" | cut -d: -f1)"
    label="$(printf '%s' "$spec" | cut -d: -f2)"
    src="$(LC_ALL=C grep -rl -F -- "$marker" "$UP_PATH/backend" "$UP_PATH/frontend" 2>/dev/null | grep -v __pycache__ | head -3 | tr '\n' ' ')"
    printf '  %-28s upstream_src=%s\n' "$marker" "${src:-none}"
    if [ -z "${src:-}" ]; then
      echo "  ASSERT FAIL: 标记 '$marker' 在上游源树（${UP_PATH}）内无出处——双向出处腿失效"
      rc=1
    fi
  done
  echo
  if [ "$rc" -ne 0 ]; then
    echo "== PACKAGE VERIFY FAILED（断言未过）=="
  else
    echo "== PACKAGE VERIFY OK（全部断言通过）=="
  fi
} > "$OUT" 2>&1 || rc=1
cat "$OUT"
exit "$rc"
