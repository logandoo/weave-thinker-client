# 上游修复清单：让 WeaveThinker Client 可在 Linux 打包为 `.deb`、正常安装运行，并正确关联桌面窗口/图标

> 本文合并两轮工作：① 让 `.deb` 能构建、安装、启动（缺陷 1–6）；② 让 Dash/Taskbar
> 上 pin 的图标能正确显示运行窗口（缺陷 7）。所有改动对**开发态行为不变**（除非注明）。

> 背景：本机（Ubuntu 24.04 / amd64）尝试用 `electron-builder` 增补 Linux `.deb`
> 打包（对齐现有 `package_macos.sh`）时，在**干净系统安装 + 非 root 运行**下暴露
> 出以下**原仓库既有缺陷**。它们多为「只在代码目录只读 / 冻结侧车」场景下触发，
> 面向 macOS「拖入 /Applications（bundle 归用户可写）」时不易复现，因此此前未被发现。
>
> 以下改动均**不改变开发态行为**（`server/` 可写时路径逐字节不变），仅修正打包/安装态。
> 我们已在本地按此修复并用「干净 `ubuntu:24.04` 容器安装 + 非 root 启动」验证通过。

---

## 缺陷 1（严重）：可写运行时目录落在代码安装目录 → 安装版后端启动崩溃

- **文件**：`server/main.py`（`startup_event`，约 148–169 行）
- **症状**：把应用装到只读位置（如 `/opt/WeaveThinker`）后，后端启动即
  `PermissionError: [Errno 13] Permission denied: '/opt/WeaveThinker/resources/sidecar/weave-server/_internal/audio_files'`，
  FastAPI `Application startup failed. Exiting.` → 本地全栈（内嵌 PG + 后端）起不来。
- **根因**：`audio_files` / `output_files` / `.drain_token` 用
  `os.path.join(os.path.dirname(__file__), ...)` 定位。冻结（PyInstaller）后
  `__file__` 指向只读安装目录；开发态 `server/` 可写、macOS bundle 归用户可写，
  故此前未暴露。仓库已有 `config.backend_root` 作为「可写路径」的既定语义
  （见 `config.resolve_resource` 注释：「可写运行时路径不得经此解析」），此处未使用。
- **建议补丁**：

```diff
--- a/server/main.py
+++ b/server/main.py
@@ -145,9 +145,15 @@ async def startup_event():
     if sys.platform != "win32":
         signal.signal(signal.SIGHUP, _on_sighup_reload_config)
 
-    audio_files_dir = os.path.join(os.path.dirname(__file__), "audio_files")
+    # 可写运行时目录基址：冻结/安装形态下 __file__ 位于只读安装目录（如 /opt/…），
+    # 直接落盘会 PermissionError 使后端启动失败。backend_root 是既定的可写基址
+    # （打包态=<userData>/localstack/app；开发态=server/，路径不变）。
+    data_root = str(config.backend_root)
+    os.makedirs(data_root, exist_ok=True)
+    audio_files_dir = os.path.join(data_root, "audio_files")
     os.makedirs(audio_files_dir, exist_ok=True)
-    output_files_dir = os.path.join(os.path.dirname(__file__), "output_files")
+    output_files_dir = os.path.join(data_root, "output_files")
     os.makedirs(output_files_dir, exist_ok=True)
     fonts_dir = os.path.join(os.path.dirname(__file__), "Fonts")
     os.makedirs(fonts_dir, exist_ok=True)
@@ -160,7 +166,7 @@ async def startup_event():
         from app.api import server_admin as _server_admin
         _drain_tok = _server_admin.new_drain_token()
         _server_admin.set_drain_token(_drain_tok)
-        _tok_path = os.path.join(os.path.dirname(__file__), ".drain_token")
+        _tok_path = os.path.join(data_root, ".drain_token")
```

  - `fonts_dir` 保留 `__file__`（它是**只读随包资源**，冻结后位于 `_internal/Fonts`，已存在，`makedirs(exist_ok=True)` 不报错）。
  - 开发态验证：`config.backend_root` = `server/`（`config_path.parent`），与旧路径一致。

---

## 缺陷 2（严重）：录音目录同一类问题

- **文件**：`server/app/services/asr_service.py`（`get_audio_files_dir`，约 330 行）
- **症状**：语音听写落盘失败（安装版）。
- **根因**：`Path(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))) / "audio_files"`，冻结后指向只读目录。
- **建议补丁**：

```diff
--- a/server/app/services/asr_service.py
+++ b/server/app/services/asr_service.py
@@ -2,7 +2,6 @@ import asyncio
 import base64
 import json
 import logging
-import os
 import struct
 import uuid
 import time
@@ -328,8 +327,9 @@ class ASRService:
     def get_audio_files_dir(self) -> Path:
-        backend_dir = Path(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
-        audio_dir = backend_dir / "audio_files"
+        # 可写基址：config.backend_root（打包态=<userData>/localstack/app；开发态=server/，
+        # 路径与旧实现一致）。
+        audio_dir = Path(get_config().backend_root) / "audio_files"
         audio_dir.mkdir(parents=True, exist_ok=True)
         return audio_dir
```

  - 该文件已 `from app.core.config import get_config`；删掉因此不再使用的 `import os`。
  - 开发态路径不变：x3 上溯 = `server/`，`config.backend_root` 亦为 `server/`。

---

## 缺陷 3（严重）：PyInstaller 侧车漏打包 PostgreSQL 扩展 → `initdb` 失败

- **文件**：`script/linux/build_sidecar.sh`
- **症状**：冻结侧车 `pg start` 执行 `initdb` 报
  `FATAL: could not access file "dict_snowball": No such file or directory` →
  `initdb: removing data directory ...`，本地 PG 起不来。
- **根因**：仅用 `--collect-all embedded_postgres`；PyInstaller 在 Linux 上只收集到
  `embedded_postgres/pginstall/lib` 顶层 5 个 `.so`，而 `dict_snowball.so`、`vector.so`
  等 **75/80 个扩展模块未被收集**（源目录 80 `.so` → 冻结包 5 `.so`）。
- **建议补丁**（显式把整棵 `pginstall` 树作为 data 收进去，并对工具版本锁定）：

```diff
--- a/script/linux/build_sidecar.sh
+++ b/script/linux/build_sidecar.sh
@@ -7,19 +7,27 @@ VENV="$ROOT/.localstack/venv"
 PY="$VENV/bin/python"
 [ -x "$PY" ] || { echo "venv missing — run: bash script/linux/localstack.sh backend-bootstrap"; exit 1; }
 
-"$PY" -m pip install --disable-pip-version-check --quiet pyinstaller
+"$PY" -m pip install --disable-pip-version-check --quiet "pyinstaller==6.22.3"
 
 SEP=':'
 case "$(uname -s)" in MINGW*|CYGWIN*|MSYS*|Windows*) SEP=';';; esac
 OUT="$ROOT/dist_sidecar"
 rm -rf "$OUT" "$ROOT/build/pyinstaller"
 
+# embedded_postgres 的 pginstall（PostgreSQL 二进制 + 扩展 .so + share/include）
+# 必须整树随包：--collect-all 只收集到顶层 5 个 .so，扩展模块缺失 → initdb FATAL。
+EP_DIR="$("$PY" -c 'import embedded_postgres, os; print(os.path.dirname(embedded_postgres.__file__))')"
+[ -d "$EP_DIR/pginstall" ] || { echo "embedded_postgres pginstall not found at $EP_DIR/pginstall"; exit 1; }
+
 PYTHONPATH="$ROOT/server" "$PY" -m PyInstaller --noconfirm --clean --onedir --name weave-server \
   --distpath "$OUT" --workpath "$ROOT/build/pyinstaller" --specpath "$ROOT/build" \
   --paths "$ROOT/server" \
   --collect-all embedded_postgres \
+  --add-data "$EP_DIR/pginstall${SEP}embedded_postgres/pginstall" \
   --collect-submodules uvicorn \
```

  - 注：`pyinstaller==6.22.3` 为锁定建议值（当前环境实测）；上游可按自身矩阵选择版本，关键是把浮动 `pip install pyinstaller` 改为可复现的固定版本。
  - **macOS 侧是否同样漏收尚未验证**（`collect_dynamic_libs` 跨平台行为不同）；建议 macOS 打包后同样断言 `pginstall/lib/dict_snowball*` 存在于产物中。

---

## 缺陷 4（建议一并修）：同类的其它写盘路径（本轮未改动，属潜伏项）

同类「用 `__file__` 定位可写输出目录」的写法还有两处，安装版仅在**使用对应功能时**才会失败（不影响启动）：

- `server/app/services/interactive_browser_service.py:619, 665`
  ```python
  output_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output_files")
  ```
- `server/app/services/export_worker.py:128`
  ```python
  output_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "output_files")
  ```

两个文件都已 `from app.core.config import get_config`（`config = get_config()`），建议统一改为：

```python
output_dir = str(get_config().backend_root / "output_files")
```

> 建议：把「可写运行时目录」收敛为 `config` 上的单一访问器（如
> `config.data_path("audio_files" | "output_files")`），全仓统一，避免同类新增。

---

## 缺陷 5（低，卫生）：`.gitignore` 未忽略构建缓存

- `.cache/`（Playwright 浏览器缓存，`package_macos.sh` 亦使用）未忽略，易误入库。建议加入 `.gitignore`。

## 缺陷 6（低，文档）：README 打包段未含 Linux

- `README.md`「打包」段仅列 `package_macos.sh`。新增 Linux `.deb` 构建后建议补一行。

---

## 缺陷 7（中）：桌面窗口关联缺失 → Dash 上 pin 的图标不显示运行，另开一个无图标条目

- **文件**：`package.json`（根，会被打进 `app.asar`）+ Linux 打包配置
  `electron-builder-linux.yml`（本轮新增的 Linux 配置）
- **症状**：`.deb` 安装后把应用 pin 到 Ubuntu/GNOME 的 Dash；启动时被 pin 的
  WeaveThinker 图标**不会**标记为运行中，而是在 Dock 上**新弹出一个没有任何图标的
  条目**（点它才会聚焦窗口）。
- **根因**：Linux 上 Electron 用**根 `package.json` 的 `desktopName`** 推导窗口身份——
  Wayland 的 XDG `app_id` / X11 的 `WM_CLASS`。未设置时回退为「应用名的小写连字符
  slug」，与安装的 `.desktop` 不匹配：
  - 浏览器进程实测（用打包内真实 `package.json` 作 Electron 入口，最小 main 打印
    `process.env.CHROME_DESKTOP`）：
    - 无 `desktopName` → `CHROME_DESKTOP="weave-thinker-client.desktop"`
    - `"desktopName":"WeaveThinker"` → `CHROME_DESKTOP="WeaveThinker"`
  - `electron-builder` 生成的 `/usr/share/applications/WeaveThinker.desktop` 为
    `StartupWMClass=WeaveThinker`、`Icon=WeaveThinker`。
  - 二者不一致 → 桌面环境无法把运行窗口关联到该 launcher（GNOME 50.x 的
    `xdg-desktop-portal` 亦要求 `app_id` 能解析到装好的 `.desktop`），于是显示为
    独立条目、且因无匹配图标而空白。
- **建议补丁**：

```diff
--- a/package.json
+++ b/package.json
@@ -3,6 +3,7 @@
   "private": true,
   "version": "0.1.0",
   "description": "Weave Thinker multi-platform client — Electron desktop + Capacitor mobile, shared Vue SPA",
+  "desktopName": "WeaveThinker",
   "workspaces": [
     "app",
     "desktop"
```

```diff
--- a/electron-builder-linux.yml
+++ b/electron-builder-linux.yml
@@ linux:
   category: Utility
+  # 桌面文件名派生自 package.json 的 desktopName（与 Electron 运行期
+  # app_id / WM_CLASS 同源），避免二者被各自改动后静默漂移、图标再次脱钩。
+  # 当前 desktopName == executableName == productName == "WeaveThinker"。
+  syncDesktopName: true
   icon: build/icon.png
```

  - `desktopName` 的值必须**等于**安装的 `.desktop` 文件名（去 `\.desktop` 后缀）与
    `StartupWMClass`。本仓 `productName = executableName = "WeaveThinker"`，故取同名。
  - 值建议用反 DNS 形式（如 `com.weavethinker.client`）以符合 freedesktop 规范并兼容
    `globalShortcut`/portal；本仓暂用 `WeaveThinker` 以保持已 pin 图标不重复。若改为
    反 DNS，需同时改 `desktopName` 与包名/文件名（`syncDesktopName: true` 会据
    `desktopName` 生成文件名，保持一致）。
  - `desktopName` 仅 Linux 生效，`electron-builder.yml`（macOS）不受影响。

- **验证方法**（无需真实 GPU/窗口，CI 友好）：把打包后的
  `resources/app.asar/package.json` 作为 Electron 入口、用一个只打印
  `process.env.CHROME_DESKTOP` 的 main 运行：

```bash
# 期望 WITH desktopName → "WeaveThinker"；移除 desktopName 的对照 → "weave-thinker-client.desktop"
node -e 'const fs=require("fs");const p=JSON.parse(fs.readFileSync("package.json"));p.main="probe.cjs";fs.writeFileSync("probe-app/package.json",JSON.stringify(p,null,2))'
xvfb-run -a node_modules/electron/dist/electron probe-app --no-sandbox
```

---

## 验证方法（复现/回归）

前置：Node ≥ 22、`npm install` 且 `node_modules/electron/dist` 存在、`bash script/linux/localstack.sh backend-bootstrap`（建 `.localstack/venv`）。

```bash
# 1) 构建 deb
bash script/linux/package_linux.sh          # → dist_installer/WeaveThinker-<ver>-amd64.deb
dpkg-deb -I dist_installer/WeaveThinker-*.deb
dpkg-deb -c dist_installer/WeaveThinker-*.deb | grep -E 'opt/WeaveThinker/WeaveThinker$|resources/sidecar/weave-server/weave-server$'

# 2) 干净系统安装 + 非 root 运行（关键：验证只读安装目录不再导致启动失败）
docker run --rm --shm-size=1g -v "$PWD/dist_installer":/pkg ubuntu:24.04 bash -lc '
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get install -y /pkg/WeaveThinker-*.deb xvfb
  useradd -m appuser; mkdir -p /home/appuser/data; chown -R appuser /home/appuser
  printf "weave-bridge-ok\n" > /home/appuser/fixture.txt; chown appuser /home/appuser/fixture.txt
  su appuser -c "cd /home/appuser && WEAVE_USER_DATA_DIR=/home/appuser/data xvfb-run -a \
    /usr/bin/WeaveThinker --no-sandbox --disable-gpu --disable-dev-shm-usage --smoke \
    --screenshot=/home/appuser/shot.png --fixture=/home/appuser/fixture.txt" | grep -oE "\"pass\":(true|false)|LOCAL_STACK_READY[^}]*\}|runtimeHealth\":\{[^}]*\}"
'
# 期望：LOCAL_STACK_READY {"apiBase":"http://127.0.0.1:8159",...}  "pass":true  runtimeHealth {"status":200,...}
# 且 userData 下生成：audio_files  output_files  config.toml  .drain_token
```

- **注意**：内嵌 PostgreSQL 的 `initdb` 拒绝以 root 运行，故必须在容器内用**非 root 用户**验证；宿主 `--smoke` 因无 root 无法设置 `chrome-sandbox` SUID，需 `ELECTRON_DISABLE_SANDBOX=1`/`--no-sandbox`（正式 deb 的 postinst 会处理）。

---

## 变更范围小结

| # | 文件 | 类型 | 是否原仓库缺陷 |
|---|---|---|---|
| 1 | `server/main.py` | 必须 | 是（只读安装目录下启动失败） |
| 2 | `server/app/services/asr_service.py` | 必须 | 是（同类，录音落盘） |
| 3 | `script/linux/build_sidecar.sh` | 必须 | 是（侧车缺 PG 扩展；含工具版本锁定） |
| 4 | `interactive_browser_service.py` / `export_worker.py` | 建议 | 是（同类潜伏） |
| 5 | `.gitignore`（`.cache/`） | 低 | 是（卫生） |
| 6 | `README.md` | 低 | 否（文档） |
| 7 | `package.json`（`desktopName`）+ `electron-builder-linux.yml`（`syncDesktopName`） | 建议 | 部分（`package.json` 为上游文件；Linux 打包配置为本轮新增） |

以上 1–3 为让 Linux 安装版**能启动、本地栈可用**的最小必要修复；4–6 为一致性/卫生/文档；
7 为 Linux 桌面集成（Dash/Taskbar 图标与运行窗口关联）。
