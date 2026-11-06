#!/usr/bin/env bash
# 上游三方合并（conflict 件）：base = upstream baseline blob, theirs = upstream target blob。
# 用法：bash script/linux/upstream_merge.sh <base_sha> <target_sha> <up_path>:<local_path>
#   干净合并 → 写回本地并打印 sha；有冲突标记 → **不动本地文件**，打印冲突块位置（人工裁决）。
# 上游仓只读：仅 `git show`。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
UP_PATH="$(node -e 'const fs=require("fs");const t=fs.readFileSync("upstream.toml","utf8");const m=t.match(/^\s*path\s*=\s*"([^"]+)"/m);process.stdout.write(m[1])')"
BASE="${1:?usage: upstream_merge.sh <base_sha> <target_sha> <up>:<local>}"
TARGET="${2:?}"
SPEC="${3:?}"
up="${SPEC%%:*}"
lo="${SPEC#*:}"
LOG="$ROOT/logs/upstream_merge.log"
mkdir -p logs
{
  echo "== upstream three-way merge =="
  echo "base:   $BASE   target: $TARGET"
  echo "file:   $up -> $lo"
  ours="$(mktemp)"; base="$(mktemp)"; theirs="$(mktemp)"
  cp "$lo" "$ours"
  git -C "$UP_PATH" show "$BASE:$up" > "$base"
  git -C "$UP_PATH" show "$TARGET:$up" > "$theirs"
  echo "sha256 ours(local)      : $(shasum -a 256 "$ours" | awk '{print $1}')"
  echo "sha256 base(upstream)   : $(shasum -a 256 "$base" | awk '{print $1}')"
  echo "sha256 theirs(upstream) : $(shasum -a 256 "$theirs" | awk '{print $1}')"
  set +e
  git merge-file -L "local($lo)" -L "base($up@$BASE)" -L "upstream($up@$TARGET)" "$ours" "$base" "$theirs"
  rc=$?
  set -e
  n_conf="$(grep -c '^<<<<<<< ' "$ours" || true)"
  echo "merge-file exit: $rc   conflict blocks: $n_conf"
  if [ "$n_conf" -eq 0 ]; then
    cp "$ours" "$lo"
    echo "RESULT: merged (clean)  sha256(local) = $(shasum -a 256 "$lo" | awk '{print $1}')"
  else
    echo "RESULT: CONFLICT — local file left UNCHANGED; conflict regions:"
    grep -n '^<<<<<<< \|^=======$\|^>>>>>>> ' "$ours" | head -40
  fi
  rm -f "$ours" "$base" "$theirs"
} 2>&1 | tee -a "$LOG"
