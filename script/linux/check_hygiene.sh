#!/usr/bin/env bash
# 机械校验（A2/COV-2）：提交物内禁止任何"按模式杀进程"形态（仅允许 .pid 定向 kill）。
# 覆盖：pkill/killall/pgrep/pidof、xargs …kill、Windows taskkill …/IM（任意 flag 顺序）；
# 大小写不敏感；去换行+反斜杠接续扁平化扫描；任何扫描面/工具错误 fail-closed（含 BSD 语义）。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

fail() { echo "hygiene FAIL: $1"; exit 1; }

for d in script desktop localstack .github; do
  [ -d "$d" ] || fail "scan dir missing: $d"
done

FILES="$(find script desktop localstack .github -type f \
  \( -name '*.sh' -o -name '*.cjs' -o -name '*.js' -o -name '*.mjs' -o -name '*.py' \
     -o -name '*.bat' -o -name '*.ps1' -o -name '*.yml' -o -name '*.yaml' \) \
  ! -path 'script/linux/check_hygiene.sh' 2>/dev/null)" || fail "find error"

PATTERN='pkill|killall|pgrep|pidof|taskkill[^|]*/[Ii][Mm]|taskkill[^|]*/[Ff][Ii]|Stop-Process[^|]*-Name|xargs[^|]*kill'
FLAT_PATTERN='pkill|killall|pgrep|pidof|taskkill[^|]*/[Ii][Mm]|taskkill[^|]*/[Ff][Ii]|Stop-Process[^|]*-Name|xargs[^|]*kill'

while IFS= read -r f; do
  [ -n "$f" ] || continue
  [ -r "$f" ] || fail "unreadable file: $f"

  rc=0
  out="$(grep -Ein "$PATTERN" "$f" 2>/dev/null)" || rc=$?
  if [ "$rc" -gt 1 ]; then fail "grep error rc=$rc on $f"; fi
  if [ -n "$out" ]; then echo "FORBIDDEN pattern-kill found:"; echo "$out"; exit 1; fi

  flat="$(tr -d '\n\r\\' < "$f" | tr -d "\"'")" || fail "read error: $f"
  frc=0
  printf '%s' "$flat" | grep -Eio "$FLAT_PATTERN" >/dev/null 2>&1 || frc=$?
  if [ "$frc" -gt 1 ]; then fail "flatten grep error rc=$frc on $f"; fi
  if [ "$frc" -eq 0 ]; then fail "split-token pattern in: $f"; fi
done <<< "$FILES"

echo "hygiene OK: no pattern-kill across script/desktop/localstack/.github (pid-directed kills only)"
