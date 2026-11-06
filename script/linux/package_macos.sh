#!/usr/bin/env bash
# macOS 安装包：SPA 构建 → 侧车构建 → electron-builder（dir + dmg, arm64）
# 产物：dist_installer/WeaveThinker-*.dmg（未签名；本机/内测可用，分发需签名+公证）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
mkdir -p logs dist_installer build
if [ -f "$ROOT/assets/icon.png" ]; then cp "$ROOT/assets/icon.png" "$ROOT/build/icon.png"; fi
LOG="$ROOT/logs/package_macos.log"
{
  echo "== [0/4] 发布前卫生：未跟踪件守卫（W39）=="
  bash "$ROOT/script/linux/check_untracked.sh"
  echo "== [0/4] BUILD_ID（单源：同一 BID 注入 SPA 与侧车，W39 同源核对）=="
  BID="$(date +%Y%m%d%H%M)-$(git rev-parse --short HEAD 2>/dev/null || echo nogit)"
  export WEAVE_BUILD_ID="$BID"
  echo "BUILD_ID=$BID"
  echo "== [1/4] SPA build =="
  bash script/linux/project_build.sh
  echo "== [2/4] sidecar build =="
  bash script/linux/build_sidecar.sh
  echo "== [2.2/4] BUILD_ID (sidecar build generation marker, W21 P1#4) =="
  # 侧车 status 暴露 build_id（env→本文件→dev）；现场判代免靠错误文案。
  # W39：BID 在 [0/4] 生成一次并 export WEAVE_BUILD_ID，SPA 侧同值烘入 VITE_BUILD_ID
  # ⇒ 冒烟可断言「SPA 与侧车同一代」；此处只负责把同一 BID 落成侧车文件。
  printf '%s' "$BID" > "$ROOT/dist_sidecar/weave-server/_internal/BUILD_ID"
  echo "BUILD_ID=$BID"

  echo "== [2.3/4] ffmpeg (bundled, redistributable GPL-2.0+ arm64 build) =="
  FFDIR="$ROOT/.cache/ffmpeg"
  FF_SHA="591260c945d0eef150e3bf82b0ef988bd36a9cecc18ff05d6679617159f0a95e"
  if [ ! -x "$FFDIR/ffmpeg" ] || [ "$(shasum -a 256 "$FFDIR/ffmpeg" | awk '{print $1}')" != "$FF_SHA" ]; then
    mkdir -p "$FFDIR"
    curl -sL --max-time 600 -o "$FFDIR/ffmpeg9arm.zip" "https://www.osxexperts.net/ffmpeg9arm.zip"
    ( cd "$FFDIR" && unzip -o -q ffmpeg9arm.zip ffmpeg && rm -f ffmpeg9arm.zip )
    chmod +x "$FFDIR/ffmpeg"
  fi
  [ "$(shasum -a 256 "$FFDIR/ffmpeg" | awk '{print $1}')" = "$FF_SHA" ] || { echo "ffmpeg sha mismatch"; exit 1; }
  [ -f "$FFDIR/COPYING.GPL-2.0.txt" ] || curl -sL --max-time 60 -o "$FFDIR/COPYING.GPL-2.0.txt" "https://www.gnu.org/licenses/old-licenses/gpl-2.0.txt"

  echo "== [2.4/4] git (bundled, built from source, GPL-2.0) =="
  # 随包 git：目标机无 Xcode CLT 时 /usr/bin/git 是 xcselect shim（弹「安装命令行
  # 工具」对话框且不可用），而 workspace_snapshot_service 的影子 git 快照
  # （init/add/commit/read-tree/checkout-index）与 terminal 工具都硬依赖 git 可执行。
  #
  # 取长合并（D-42）：
  #   构建面 = 官方 kernel.org 源码（SHA-256 与 Homebrew formula 公布值一致）+
  #     RUNTIME_PREFIX=YesPlease / HAVE_NS_GET_EXECUTABLE_PATH=YesPlease ⇒ 装到任意
  #     位置都按二进制相对定位 libexec/git-core 与 share/git-core/templates，整树移动
  #     实证可搬迁；仅链系统库（libz/libiconv/libSystem/CoreFoundation/CoreServices）
  #     ⇒ **零第三方动态库随包**，免 install_name_tool 改写与 ad-hoc codesign 破签风险。
  #   布局面 = 落 Resources/{bin,libexec,share}，复用 desktop/local-stack.cjs 既有
  #     bin/ PATH 注入（零额外 PATH 改动）；COPYING 落 bin/git.COPYING.txt 与
  #     bin/ffmpeg.COPYING.txt 同款。
  G_VER="2.56.0"
  G_SHA="26c56c296b38c0695b26fa95f475f1d01704d2d38e73465ca30b0b2f5dc789d3"
  GDIR="$ROOT/.cache/git"
  GBUNDLE="$GDIR/bundle"
  # stamp 键含**构建面指纹**（flags + 方法），不只 ver-sha：否则改 GMAKEFLAGS 后
  # 旧 bundle 被静默复用，三重自检若恰好仍绿就放行了陈旧产物（A4.9 R2-B I#3）。
  # 更危险的是 .cache/git/ 里可能残留**别的构建法**（bottle+install_name_tool）的
  # bundle——那种带 git-libs/，而 electron-builder 只映射 bin/libexec/share，会被
  # 静默丢弃 → 目标机 git fetch 在 dyld 加载处炸。
  G_FLAGS_FP="runtime_prefix+nsgetexec+nogettext+notcltk+noperl+nopython+noexpat+nohardlinks+skipdashed"
  GSTAMP="$GDIR/stamp-$G_VER-$G_SHA-$G_FLAGS_FP"
  if [ ! -f "$GSTAMP" ] || [ ! -x "$GBUNDLE/bin/git" ]; then
    rm -rf "$GDIR/src" "$GBUNDLE" "$GDIR"/stamp-*
    mkdir -p "$GDIR/src" "$GBUNDLE"
    curl -sL --max-time 600 -o "$GDIR/src/git.tar.xz" \
      "https://mirrors.edge.kernel.org/pub/software/scm/git/git-$G_VER.tar.xz"
    [ "$(shasum -a 256 "$GDIR/src/git.tar.xz" | awk '{print $1}')" = "$G_SHA" ] || { echo "git src sha mismatch"; exit 1; }
    tar -xJf "$GDIR/src/git.tar.xz" -C "$GDIR/src"
    GMAKEFLAGS=(
      prefix=/ DESTDIR="$GBUNDLE"
      RUNTIME_PREFIX=YesPlease HAVE_NS_GET_EXECUTABLE_PATH=YesPlease
      NO_GETTEXT=YesPlease NO_TCLTK=YesPlease NO_PERL=YesPlease
      NO_PYTHON=YesPlease NO_EXPAT=YesPlease
      NO_INSTALL_HARDLINKS=YesPlease SKIP_DASHED_BUILT_INS=YesPlease
    )
    make -C "$GDIR/src/git-$G_VER" -j"$(sysctl -n hw.ncpu)" "${GMAKEFLAGS[@]}" V=0 all
    make -C "$GDIR/src/git-$G_VER" "${GMAKEFLAGS[@]}" install
    cp "$GDIR/src/git-$G_VER/COPYING" "$GBUNDLE/COPYING"
    rm -rf "$GDIR/src"
    touch "$GSTAMP"
  fi
  # 三重自检（失败即中止，绝不放行残缺 runtime）：可执行/零非系统库/命令面真跑
  [ -x "$GBUNDLE/bin/git" ] || { echo "bundled git missing executable"; exit 1; }
  GIT_VER_OUT="$("$GBUNDLE/bin/git" --version)"
  case "$GIT_VER_OUT" in git\ version\ "$G_VER"*) ;; *) echo "bundled git version mismatch: $GIT_VER_OUT"; exit 1;; esac
  [ -f "$GBUNDLE/COPYING" ] || { echo "git COPYING (GPL-2.0) missing"; exit 1; }
  # 布局形状闸：RUNTIME_PREFIX 三点成套，且不得是别的构建法残留（git-libs/）
  [ -d "$GBUNDLE/libexec/git-core" ] || { echo "bundled git missing libexec/git-core"; exit 1; }
  [ -d "$GBUNDLE/share/git-core/templates" ] || { echo "bundled git missing share/git-core/templates (RUNTIME_PREFIX 三点不成套)"; exit 1; }
  if [ -d "$GBUNDLE/git-libs" ]; then
    echo "stale non-source-built git bundle detected (git-libs/ present) — rm -rf $GBUNDLE and rerun"; exit 1
  fi
  # 零非系统动态库：扫**全部** Mach-O（bin + libexec/git-core），缺失探针不静默跳过
  BAD_ANY=""
  while IFS= read -r probe; do
    [ -n "$probe" ] || continue
    if file "$probe" | grep -q "Mach-O"; then
      BAD="$(otool -L "$probe" | awk 'NR>1 {print $1}' | grep -Ev '^(/usr/lib/|/System/Library/)' || true)"
      [ -z "$BAD" ] || BAD_ANY="$BAD_ANY\n$probe: $BAD"
    fi
  done < <(find "$GBUNDLE/bin" "$GBUNDLE/libexec/git-core" -type f -perm -u+x 2>/dev/null)
  if [ -n "$BAD_ANY" ]; then echo "bundled git has non-system dylib deps:$BAD_ANY"; exit 1; fi
  # 影子快照命令面真跑（workspace_snapshot_service 用的就是这一组）
  GSMOKE="$(mktemp -d)"
  trap 'rm -rf "$GSMOKE"' EXIT
  mkdir -p "$GSMOKE/wt" "$GSMOKE/snap" && echo v1 > "$GSMOKE/wt/f.txt"
  # -c 全套钉死：不读构建机全局 gitconfig（如 commit.gpgsign/core.hooksPath 会误伤构建）
  GSNOOP=(-c user.email=smoke@weave -c user.name=smoke -c commit.gpgsign=false
           -c core.hooksPath=/dev/null -c advice.detachedHead=false)
  "$GBUNDLE/bin/git" "${GSNOOP[@]}" --git-dir="$GSMOKE/snap" --work-tree="$GSMOKE/wt" init -q
  "$GBUNDLE/bin/git" "${GSNOOP[@]}" --git-dir="$GSMOKE/snap" --work-tree="$GSMOKE/wt" add -A
  "$GBUNDLE/bin/git" "${GSNOOP[@]}" --git-dir="$GSMOKE/snap" --work-tree="$GSMOKE/wt" commit -qm snapshot
  "$GBUNDLE/bin/git" "${GSNOOP[@]}" --git-dir="$GSMOKE/snap" --work-tree="$GSMOKE/wt" read-tree HEAD
  "$GBUNDLE/bin/git" "${GSNOOP[@]}" --git-dir="$GSMOKE/snap" --work-tree="$GSMOKE/wt" checkout-index -a -f
  rm -rf "$GSMOKE"; trap - EXIT
  echo "git bundled: $GIT_VER_OUT → $GBUNDLE ($(du -sh "$GBUNDLE" | awk '{print $1}'))"

  echo "== [2.5/4] playwright chromium (bundled, headless shell) =="
  # 打包只需 chromium_headless_shell（浏览器服务端预览/截图）。**禁止每次打包重下
  # ~170MB**：playwright 的 install 判「已装即跳过」，但它只认 PLAYWRIGHT_BROWSERS_PATH
  # 指向的缓存——旧写法每次 rm -rf 掉 staging 再把缓存指向空目录，等于强制重下。
  # 改为：持久缓存（.cache/ms-playwright）→ 若空则从系统级 ms-playwright 缓存播种
  # （同构建直接拷，零下载）→ 只装 `chromium-headless-shell` 目标（装 `chromium` 会
  # 连带下 chromium-170MB+ffmpeg，均不上船）→ 按 `--dry-run` 报的**确切**目录裁剪
  # 产物，避免把缓存里其它构建号一起拷进 DMG。
  PW_CACHE="${PLAYWRIGHT_BROWSERS_PATH:-$ROOT/.cache/ms-playwright}"
  mkdir -p "$PW_CACHE"
  if ! ls "$PW_CACHE"/chromium_headless_shell-*/INSTALLATION_COMPLETE >/dev/null 2>&1; then
    SYS_CACHE="$HOME/Library/Caches/ms-playwright"
    if ls "$SYS_CACHE"/chromium_headless_shell-*/INSTALLATION_COMPLETE >/dev/null 2>&1; then
      echo "seeding playwright cache from $SYS_CACHE (reuse existing build, no download)"
      for _d in "$SYS_CACHE"/chromium_headless_shell-*; do
        [ -d "$_d" ] || continue
        rm -rf "$PW_CACHE/$(basename "$_d")"
        cp -R "$_d" "$PW_CACHE/"
      done
    fi
  fi
  PLAYWRIGHT_BROWSERS_PATH="$PW_CACHE" "$ROOT/.localstack/venv/bin/python" -m playwright install chromium-headless-shell
  PW_SHELL_DIR="$(PLAYWRIGHT_BROWSERS_PATH="$PW_CACHE" "$ROOT/.localstack/venv/bin/python" -m playwright install --dry-run chromium-headless-shell 2>/dev/null | awk '/Headless Shell/{f=1} f && /Install location:/{print $NF; exit}')"
  [ -n "$PW_SHELL_DIR" ] && [ -d "$PW_SHELL_DIR" ] || { echo "cannot resolve headless-shell install dir (got: $PW_SHELL_DIR)"; exit 1; }
  # 裁剪产物：只留 playwright 本次要的那一个 headless shell（chromium-*/ffmpeg-* 不上船）
  rm -rf "$ROOT/dist_sidecar/browsers"
  mkdir -p "$ROOT/dist_sidecar/browsers"
  cp -R "$PW_SHELL_DIR" "$ROOT/dist_sidecar/browsers/"
  PW_SHELL_BIN="$(find "$ROOT/dist_sidecar/browsers" -type f -name 'chrome-headless-shell' -perm -u+x 2>/dev/null | head -1)"
  [ -n "$PW_SHELL_BIN" ] || { echo "headless shell binary missing/not executable"; exit 1; }
  if ls "$ROOT"/dist_sidecar/browsers/chromium-[0-9]* >/dev/null 2>&1 \
     || ls "$ROOT"/dist_sidecar/browsers/ffmpeg-* >/dev/null 2>&1; then
    echo "chromium prune failed"; exit 1
  fi
  echo "playwright headless shell staged: $(basename "$PW_SHELL_DIR") ($(du -sh "$ROOT/dist_sidecar/browsers" | awk '{print $1}')) cache=$PW_CACHE"
  echo "== [2.6/4] LibreOffice staging (BUNDLE_LIBREOFFICE=${BUNDLE_LIBREOFFICE:-1}) =="
  LO_SRC="${LIBREOFFICE_SRC:-/Applications/LibreOffice.app}"
  if [ "${BUNDLE_LIBREOFFICE:-1}" = "1" ]; then
    [ -d "$LO_SRC" ] || { echo "LibreOffice not found at $LO_SRC (set LIBREOFFICE_SRC or BUNDLE_LIBREOFFICE=0)"; exit 1; }
    rm -rf "$ROOT/dist_lo" && mkdir -p "$ROOT/dist_lo" && ditto "$LO_SRC" "$ROOT/dist_lo/LibreOffice.app"
  else
    rm -rf "$ROOT/dist_lo" && mkdir -p "$ROOT/dist_lo/LibreOffice.app"
  fi
  echo "== [3/4] electron-builder (mac arm64: dir + dmg) =="
  "$ROOT/node_modules/.bin/electron-builder" --mac --arm64 --config electron-builder.yml --publish never
  echo "== PACKAGE OK =="
  ls -lh dist_installer/*.dmg 2>/dev/null || true
  # 只列**本版本**产物：目录里可能残留历史 DMG；README 的 `shasum -c` 会因缺文件判 FAILED。
  VER="$(node -p "require('./package.json').version")"
  ( cd dist_installer && shasum -a 256 "WeaveThinker-${VER}-"*.dmg ) | tee dist_installer/SHA256SUMS.txt
} 2>&1 | tee "$LOG"
