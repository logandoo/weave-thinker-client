#!/usr/bin/env bash
# Linux 安装包（.deb）：SPA 构建 → 侧车构建 → electron-builder（deb, amd64）
# 产物：dist_installer/WeaveThinker-<ver>-amd64.deb
# 对齐 script/linux/package_macos.sh 的流程；mac 专属随包资源（ffmpeg/git/LibreOffice）
# 不随 Linux 包——目标机用系统 ffmpeg/git/soffice（随包裁剪 deferred）。
# 前置：Node ≥ 22、npm install、bash script/linux/localstack.sh backend-bootstrap。
# 验证方法（干净系统安装 + 非 root 运行）见 docs/LINUX_DEB_UPSTREAM_FIXES.md。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
# 架构守卫（W23 评审 Minor：非 amd64 主机交叉产出的 Electron 是异物，静默上船最危险）
if [ "$(uname -s)" != "Linux" ]; then echo "package_linux.sh must run on a Linux host (PyInstaller sidecar is not cross-platform)"; exit 1; fi
if [ "$(uname -m)" != "x86_64" ]; then echo "target arch is amd64 but host is $(uname -m) — refusing to ship a foreign Electron"; exit 1; fi
mkdir -p logs dist_installer build
if [ -f "$ROOT/assets/icon.png" ]; then cp "$ROOT/assets/icon.png" "$ROOT/build/icon.png"; fi
LOG="$ROOT/logs/package_linux.log"
{
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
  # W39：BID 在 [0/4] 生成一次并 export WEAVE_BUILD_ID，SPA 侧同值烘入 VITE_BUILD_ID。
  printf '%s' "$BID" > "$ROOT/dist_sidecar/weave-server/_internal/BUILD_ID"
  echo "BUILD_ID=$BID"

  echo "== [2.5/4] playwright headless shell staging =="
  # 与 package_macos.sh 同款：只留本次要的 chromium-headless-shell（平台自带口径）。
  PW_CACHE="${PLAYWRIGHT_BROWSERS_PATH:-$ROOT/.cache/playwright-browsers}"
  mkdir -p "$PW_CACHE"
  PLAYWRIGHT_BROWSERS_PATH="$PW_CACHE" "$ROOT/.localstack/venv/bin/python" -m playwright install chromium-headless-shell
  PW_SHELL_DIR="$(PLAYWRIGHT_BROWSERS_PATH="$PW_CACHE" "$ROOT/.localstack/venv/bin/python" -m playwright install --dry-run chromium-headless-shell 2>/dev/null | awk '/Headless Shell/{f=1} f && /Install location:/{print $NF; exit}')"
  [ -n "$PW_SHELL_DIR" ] && [ -d "$PW_SHELL_DIR" ] || { echo "cannot resolve headless-shell install dir (got: $PW_SHELL_DIR)"; exit 1; }
  rm -rf "$ROOT/dist_sidecar/browsers"
  mkdir -p "$ROOT/dist_sidecar/browsers"
  cp -R "$PW_SHELL_DIR" "$ROOT/dist_sidecar/browsers/"
  PW_SHELL_BIN="$(find "$ROOT/dist_sidecar/browsers" -type f -name 'chrome-headless-shell' -perm -u+x 2>/dev/null | head -1)"
  [ -n "$PW_SHELL_BIN" ] || { echo "headless shell binary missing/not executable"; exit 1; }
  echo "playwright headless shell staged: $(basename "$PW_SHELL_DIR") ($(du -sh "$ROOT/dist_sidecar/browsers" | awk '{print $1}')) cache=$PW_CACHE"

  echo "== [2.6/4] skipped (mac-only bundles: ffmpeg/git/LibreOffice — system deps on Linux, deferred) =="

  echo "== [3/4] electron-builder (linux deb: amd64) =="
  "$ROOT/node_modules/.bin/electron-builder" --linux deb --x64 --config electron-builder-linux.yml --publish never

  echo "== [3.5/4] desktop identity assertions (LINUX_DEB 缺陷 7) =="
  # 打包内 app.asar 的 package.json desktopName 必须在场（Electron app_id/WM_CLASS
  # 派生源）——断言读**打包产物**而非源文件（W23 评审 I4：源文件断言无法感知打包丢改）。
  node -e '
    const { extractFile } = require("@electron/asar");
    const fs = require("fs");
    const candidates = ["dist_installer/linux-unpacked/resources/app.asar"];
    const asar = candidates.find((p) => fs.existsSync(p));
    if (!asar) { console.error("app.asar not found in dist_installer/*-unpacked"); process.exit(1); }
    const pkg = JSON.parse(extractFile(asar, "package.json").toString("utf8"));
    if (pkg.desktopName !== "WeaveThinker") { console.error("packaged desktopName missing/wrong:", pkg.desktopName); process.exit(1); }
    console.log("packaged desktopName OK:", pkg.desktopName, "in", asar);
  '
  if command -v dpkg-deb >/dev/null 2>&1; then
    dpkg-deb -I dist_installer/WeaveThinker-*.deb | grep -E 'Package|Version' || true
    # .desktop 文件名/StartupWMClass 与 desktopName 同源（syncDesktopName: true）
    dpkg-deb -c dist_installer/WeaveThinker-*.deb | grep -E 'usr/share/applications/WeaveThinker.desktop$' \
      || { echo "WeaveThinker.desktop missing from deb"; exit 1; }
    dpkg-deb -c dist_installer/WeaveThinker-*.deb | grep -E 'opt/WeaveThinker/WeaveThinker$|resources/sidecar/weave-server/weave-server$' \
      || { echo "main binary / sidecar missing from deb"; exit 1; }
  else
    echo "dpkg-deb not available (non-Debian build host) — deb content assertions skipped; verify on target per docs/LINUX_DEB_UPSTREAM_FIXES.md"
  fi

  echo "== PACKAGE OK =="
  ls -lh dist_installer/*.deb 2>/dev/null || true
  # 校验和：Linux 最小系统只有 sha256sum（shasum 是 perl 工具，未必在）
  if command -v sha256sum >/dev/null 2>&1; then
    ( cd dist_installer && sha256sum *.deb ) | tee dist_installer/SHA256SUMS.txt
  else
    ( cd dist_installer && shasum -a 256 *.deb ) | tee dist_installer/SHA256SUMS.txt
  fi
} 2>&1 | tee "$LOG"
