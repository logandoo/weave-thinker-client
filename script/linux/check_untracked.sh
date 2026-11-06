#!/usr/bin/env bash
# 未跟踪件守卫（W39；审计 §3.5「规则只有落到检查里才算数」的落地）。
#
# WHY：`git add -u` **不收录 untracked**。W31/W32/W38 三次把「上游新增件」漏出提交，
# 而分类账（`upstream_state.json` 比对**磁盘** blob）已把它们标成 in-sync ⇒
# 「账本说已同步、提交里却没有」。规则写在 memory 里两次仍第三次犯，故做成可执行检查。
#
# 用法：
#   bash script/linux/check_untracked.sh              # 检查本仓（提交前/波末）
#   bash script/linux/check_untracked.sh --root DIR   # 检查任意 git 根
#   bash script/linux/check_untracked.sh --allow '^local/'  # 追加白名单（ERE）
#   bash script/linux/check_untracked.sh --self-test  # 自测矩阵（临时 git 仓，必须红/绿各一次）
#
# 判定：`git ls-files --others --exclude-standard` 的结果（**已排除 .gitignore**）
# 必须为空或全部命中白名单。未跟踪 = 提交后才会消失；本脚本从不删改文件。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

# 默认白名单：本仓已知的「合法未跟踪」面（本地草稿/缓存；均已被 .gitignore 覆盖，
# 此处只是双保险——将来有人误改 .gitignore 时不会静默放行）。
DEFAULT_ALLOW='^(local/|\.cache/|\.localstack/|node_modules/|dist_installer/|dist_sidecar/|dist_lo/|build/|logs/)'

guard() {
  local root="$1" allow="$2"
  if ! git -C "$root" rev-parse --git-dir >/dev/null 2>&1; then
    echo "check_untracked: not a git repo: $root" >&2
    return 2
  fi
  local untracked bad=""
  untracked="$(git -C "$root" ls-files --others --exclude-standard)"
  [ -z "$untracked" ] && { echo "check_untracked OK: no untracked files"; return 0; }
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    if [ -n "$allow" ] && printf '%s' "$p" | grep -qE "$allow"; then
      continue
    fi
    bad="${bad}${p}"$'\n'
  done <<< "$untracked"
  if [ -n "$bad" ]; then
    echo "check_untracked FAIL: untracked files must be committed (or added to the allowlist):"
    printf '%s' "$bad" | sed 's/^/  - /'
    return 1
  fi
  echo "check_untracked OK: all untracked files are allowlisted"
  return 0
}

self_test() {
  local tmp; tmp="$(mktemp -d)"
  local fails=0
  _mk() {
    rm -rf "$tmp/repo" && mkdir -p "$tmp/repo" && cd "$tmp/repo"
    git init -q
    printf 'a\n' > tracked.txt
    git add tracked.txt
    git -c user.email=st@w39 -c user.name=st commit -q -m baseline
  }
  echo "== self-test: clean tree must pass =="
  _mk
  if guard "$tmp/repo" "$DEFAULT_ALLOW" >/dev/null; then echo "  clean=0 OK"; else echo "  clean=0 FAILED"; fails=1; fi

  echo "== self-test: stray untracked file must fail =="
  _mk
  printf 'b\n' > stray.txt
  if guard "$tmp/repo" "$DEFAULT_ALLOW" >/dev/null; then echo "  stray=1 FAILED (guard blinded)"; fails=1; else echo "  stray=1 OK"; fi

  echo "== self-test: allowlisted untracked file must pass =="
  _mk
  mkdir -p local && printf 'c\n' > local/keep.txt
  if guard "$tmp/repo" "$DEFAULT_ALLOW" >/dev/null; then echo "  allowlisted=0 OK"; else echo "  allowlisted=0 FAILED"; fails=1; fi

  echo "== self-test: ignored file must pass (git exclude-standard) =="
  _mk
  printf '*.log\n' > .gitignore && printf 'x\n' > run.log && git add .gitignore && git -c user.email=st@w39 -c user.name=st commit -q -m ignore
  if guard "$tmp/repo" "$DEFAULT_ALLOW" >/dev/null; then echo "  ignored=0 OK"; else echo "  ignored=0 FAILED"; fails=1; fi

  rm -rf "$tmp"
  if [ "$fails" -ne 0 ]; then echo "SELFTEST FAILED"; return 1; fi
  echo "SELFTEST OK (matrix: clean=0 · stray=1 · allowlisted=0 · ignored=0)"
  return 0
}

MODE="guard"; ROOT_OVERRIDE=""; ALLOW=""
while [ $# -gt 0 ]; do
  case "$1" in
    --self-test) MODE="selftest"; shift ;;
    --root) ROOT_OVERRIDE="${2:?--root needs a path}"; shift 2 ;;
    --allow) ALLOW="${2:?--allow needs a regex}"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "check_untracked: unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [ "$MODE" = "selftest" ]; then
  self_test
  exit $?
fi

TARGET="${ROOT_OVERRIDE:-$ROOT}"
if [ -n "$ALLOW" ]; then
  guard "$TARGET" "$DEFAULT_ALLOW|$ALLOW"
else
  guard "$TARGET" "$DEFAULT_ALLOW"
fi
