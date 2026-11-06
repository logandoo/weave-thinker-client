#!/usr/bin/env bash
# 合并残差审计（merge-file 假干净防线 · W35 教训）：
#   [A] merged vs LOCAL-BEFORE-MERGE → 只应含**上游**增量面（本地 hunk 若在此出现 = 上游吞掉本地 = 危险信号）
#   [B] merged vs UPSTREAM-TARGET    → 只应含**本地** hunk 面（上游改动若残留 = 合并漏采）
# 用法：bash script/linux/upstream_residual_audit.sh <base_sha> <target_sha> <pre_local_ref> <out_file> <up>:<local> ...
#   <pre_local_ref> = 合并前的本仓提交（如 abd629b）；该提交里的 <local> 路径即合并前本地面。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
UP_PATH="$(node -e 'const fs=require("fs");const t=fs.readFileSync("upstream.toml","utf8");const m=t.match(/^\s*path\s*=\s*"([^"]+)"/m);process.stdout.write(m[1])')"
BASE="${1:?usage: upstream_residual_audit.sh <base_sha> <target_sha> <pre_local_ref> <out_file> <up>:<local> ...}"
TARGET="${2:?}"
PRE_REF="${3:?}"
OUT="${4:?}"
shift 4
S="$(mktemp -d)"
trap 'rm -rf "$S"' EXIT
{
  echo "# 合并残差审计 — upstream base=$BASE target=$TARGET · 本仓合并前=$PRE_REF"
  echo "# 生成：bash script/linux/upstream_residual_audit.sh $BASE $TARGET $PRE_REF $(basename "$OUT") $*"
  echo "# 判读：[A] 应=上游增量面；[B] 应=本地 hunk 面（与 upstream.toml 登记一致）"
  echo
  for spec in "$@"; do
    up="${spec%%:*}"
    lo="${spec#*:}"
    git -C "$UP_PATH" show "$BASE:$up" > "$S/base"
    git -C "$UP_PATH" show "$TARGET:$up" > "$S/theirs"
    git show "$PRE_REF:$lo" > "$S/ours_before"
    cp "$lo" "$S/merged"
    echo "===== $up -> $lo ====="
    echo "--- [A] merged vs LOCAL-BEFORE-MERGE（应=上游增量面）---"
    diff -u "$S/ours_before" "$S/merged" | sed -n '1,400p' || true
    echo "--- [B] merged vs UPSTREAM-TARGET（应=本地 hunk 面）---"
    diff -u "$S/theirs" "$S/merged" | sed -n '1,400p' || true
    echo
  done
} > "$OUT" 2>&1
echo "residual audit written: $OUT ($(wc -l < "$OUT" | tr -d ' ') lines)"
