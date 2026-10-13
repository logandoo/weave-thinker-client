<p align="center">
  <img src="app/public/logo.png" width="160" alt="WeaveThinker logo">
</p>

# WeaveThinker Client（罗航）

本地优先的 AI 工作台桌面客户端：Electron 内嵌 FastAPI + PostgreSQL 侧车，对话、笔记、记忆与工作区文件离线可用，绑定云账号后多端同步。

## 功能

- 本地全栈：启动即拉起内嵌 PostgreSQL 与后端，数据落在本机；断网可用，恢复联网后自动同步。
- 云-本地同步：对话、笔记、记忆、工作区文件按账号同步到云端，多端一致。
- 设备执行：把本机注册为云端 agent 的执行设备，云端工具调用（terminal、文件读写、代码执行等）经 WS JSON-RPC 下发到本机执行，工具白名单与路径沙箱 fail-closed。
- **文档与语音**：Office 文档服务端预览（随包 LibreOffice 转换）、Markdown / WYSIWYG 笔记、语音听写。
- **桌面体验**：三皮肤、自定义标题栏、全屏、窗口状态记忆；移动端（Capacitor）共享同一份 Vue SPA。

## 快速开始

### 安装（macOS）

从 [Releases](https://github.com/alice/weave-thinker-client/releases) 下载 `WeaveThinker-<version>-arm64.dmg`，拖入「应用程序」。首次启动会初始化本地数据库，稍等片刻。

### 从源码构建

要求：Node.js ≥ 18、Python 3.13、macOS arm64（打包面；开发运行不限平台）。

```bash
npm install                          # 顶层依赖（electron 等）
bash script/linux/localstack.sh backend-bootstrap   # 建本地 venv + 装后端依赖
bash script/linux/project_build.sh   # 配置注入 + 前端构建
bash script/linux/start.sh           # 启动桌面客户端
bash script/linux/smoke_desktop.sh   # 冒烟（退出码即结论）
bash script/linux/stop.sh
```

Windows 用 `script\windows\*.bat` 同名脚本。

### 打包

```bash
bash script/linux/package_macos.sh   # SPA + 侧车 + DMG，产物在 dist_installer/
```

## 目录结构与架构

```
app/         共享 Vue 3 + Vite 5 SPA（桌面与移动端同一份）
desktop/     Electron 壳：app:// 协议、原生桥 IPC、本地侧车编排
server/      内嵌后端：FastAPI + agent 工具栈 + 同步客户端 + 设备执行器
localstack/  本地栈引导：内嵌 PostgreSQL、配置生成
script/      构建与生命周期脚本（linux/ + windows/）
docs/        设计文档、审计报告与实现计划
```

运行形态是两层：Electron 主进程负责窗口与原生能力，Python 侧车（`server/`）负责全部业务 API；打包时侧车冻结为单二进制（PyInstaller），与 SPA 一起进 DMG。

## 配置

- `config.toml`（仓库根）：构建/运行事实源，`api_base`、本地栈端口等；`project_build.sh` 会把它注入前端环境。
- `server/config.toml.example`：后端配置模板（模型、检索、工具、记忆等）。复制为 `server/config.toml` 后填入自己的密钥；本地栈模式下由 `localstack.sh` 在 `.localstack/` 下生成等价配置。
- `server/config_model.toml.example`：模型端点池与采样参数模板。

密钥不会进入版本库，也不随安装包分发。

## 开发与测试

```bash
bash script/linux/localstack.sh backend-start   # 起本地后端（默认 127.0.0.1:8161）
bash script/linux/localstack.sh backend-stop

# 后端测试
.localstack/venv/bin/python -m pytest server/tests -q

# 前端单测
cd app && npm run test:unit
```

设备执行的协议契约见 `server/API.md`「设备执行器」节。

## 许可

Apache License 2.0，见 [LICENSE](LICENSE)。随安装包分发的第三方组件（FFmpeg、LibreOffice、Chromium Headless Shell、字体）授权说明见 [NOTICE.md](NOTICE.md)。
