#!/usr/bin/env bash
# 上游直采（ff-safe + new-upstream）：写本地 + sha256 对账 + **可失败的断言**。
# 用法：bash script/linux/upstream_adopt.sh <upstream_target_sha> <file...>
#   <file> = "<upstream_path>:<local_path>"（映射取自 upstream.toml 的 [[map]]/[[filemap]]）
# 上游仓只读：本脚本只对上游执行 `git show` / `git cat-file`。
# 断言设计（评审 A4.9 Minor #5 修正）：上游 blob 的 sha **独立读取两次**
# （`git cat-file blob` 与 `git show`）→ 与落盘后本地文件的 sha 三方比对；
# 不一致即中止（非 0 退出）而不是「打印一个 MISMATCH 表格后照样 exit 0」。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
UP_PATH="$(node -e 'const fs=require("fs");const t=fs.readFileSync("upstream.toml","utf8");const m=t.match(/^\s*path\s*=\s*"([^"]+)"/m);process.stdout.write(m[1])')"
TARGET="${1:?usage: upstream_adopt.sh <target_sha> <up>:<local> ...}"
shift
LOG="$ROOT/logs/upstream_adopt.log"
mkdir -p logs
fail=0
{
  echo "== upstream adopt =="
  echo "upstream: $UP_PATH"
  echo "target:   $TARGET"
  echo "files:    $#"
  echo
  printf '%-52s %-8s %-64s %-64s %s\n' "local" "status" "sha256(local after)" "sha256(upstream blob)" "sha256(local before)"
  for spec in "$@"; do
    up="${spec%%:*}"
    lo="${spec#*:}"
    mkdir -p "$(dirname "$lo")"
    tmp="$(mktemp)"
    git -C "$UP_PATH" show "$TARGET:$up" > "$tmp"
    # 独立读取面：`git cat-file blob`（不经 tmp 文件）→ 上游 blob 真值
    up_sha="$(git -C "$UP_PATH" cat-file blob "$TARGET:$up" | shasum -a 256 | awk '{print $1}')"
    tmp_sha="$(shasum -a 256 "$tmp" | awk '{print $1}')"
    if [ -f "$lo" ]; then
      lo_sha_pre="$(shasum -a 256 "$lo" | awk '{print $1}')"
    else
      lo_sha_pre="(absent)"
    fi
    cp "$tmp" "$lo"
    rm -f "$tmp"
    lo_sha="$(shasum -a 256 "$lo" | awk '{print $1}')"
    if [ "$lo_sha" = "$up_sha" ] && [ "$tmp_sha" = "$up_sha" ]; then
      st="OK"
    else
      st="FAIL"
      fail=1
    fi
    printf '%-52s %-8s %-64s %-64s %s\n' "$lo" "$st" "$lo_sha" "$up_sha" "$lo_sha_pre"
  done
  echo
  if [ "$fail" -ne 0 ]; then
    echo "== ADOPT FAILED (sha mismatch: local != upstream blob) =="
    exit 1
  fi
  echo "== ADOPT OK — all $# files byte-identical to upstream blobs (asserted) =="
} 2>&1 | tee "$LOG"
exit "${PIPESTATUS[0]}"
