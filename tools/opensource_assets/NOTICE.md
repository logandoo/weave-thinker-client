# NOTICE

本仓 `app/` 目录为 [weave-thinker](https://github.com/alice/weave-thinker) 前端（chatbot/frontend）的 vendor 副本，
上游以 Apache License 2.0 授权（见上游仓库 LICENSE）。修改记录见 git 历史。

字体资源 `app/public/fonts/` 的授权见其目录内 `FONTS_LICENSE.md`（SIL OFL 1.1）。

## Vendor 偏差登记（server/ ← chatbot/backend）
- `server/app/core/config.py`：登记补丁 `LOCALSTACK_CONFIG_PATH`（默认行为不变；侧车/冻结模式配置定位）。
- `server/app/core/deps.py` / `server/app/services/llm_service.py` / `server/app/services/memory_embedding_service.py`：各 1 行扫描豁免注释（语义零变更）。
- `server/config_model.toml.example`：上游 opensource 版本含 Apache 头（本仓原样保留，非本波改动）。

## 随安装包分发的第三方组件（macOS）
- **FFmpeg 9.0**（静态 arm64；构建方 osxexperts.net，configure 含 `--enable-gpl` 且无 nonfree → 可再分发，GPL-2.0-or-later）— 仅用于音频转码子进程。许可文本随包（`Resources/bin/ffmpeg.COPYING.txt`）；源码提供（GPL 义务）：https://ffmpeg.org/download.html 与构建方 https://www.osxexperts.net/ ；二进制 sha256 固定于 `script/linux/package_macos.sh`。
- **Chromium Headless Shell**（Playwright 浏览器资产，BSD-3-Clause）— 网页抓取工具。
- **LibreOffice**（dist_lo/LibreOffice.app，MPL-2.0）— Office 服务端预览转换（许可文件随 app 内分发）。
