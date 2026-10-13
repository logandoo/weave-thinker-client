# PLAN — 设备执行器改造清单（Device Executor）

> 状态：方案定稿 v2（含 A4.9 复审修订；Class: DOC，仅方案不含实现）· 2026-10-09
> 主方案（协议/云端/安全/分期）见 chatbot 仓 `docs/PLAN_device_execution_mode.md`；本文只列
> **本客户端仓需要修改/新增的地方**。定谳：切换粒度=会话级、多设备=多注册单绑定、离线 fail-fast
> （chatbot 仓 `tests/decisions.md` D-6/D-7/D-8；注意本仓 `tests/decisions.md` 的同号条目为其他主题）。

## 0. 现状结论（只读探查，已验证）

本仓**不是薄壳**：Electron（`desktop/main.cjs`）+ vendored 全量 FastAPI（`server/`，chatbot 后端
基线 `1f6c3d4e7`）+ 内嵌 PG + 全套 agent 工具（terminal/git/workspace_*/code_execution/process/
browser/skill_tools + 权限栈 + vibeweaver hooks）——**工具在本机执行的能力已全部存在**（本地 agent
今天即用）。云端链路现状仅 HTTP 轮询 sync（`server/app/services/sync_client.py` + `SyncDevice`），
无任何 WS 通道、无 remote_exec/device_exec 概念（两仓 grep 确认）。

因此本改造=**给既有工具栈加一个"被云端 agent 驱动"的入口**，不重写任何工具实现。

## 1. 需要新增

### 1.1 device executor worker（核心，client-only）
- 新文件 `server/app/services/device_executor_service.py`：
  - **出站 WSS 连接到云端** `/api/device/ws`，握手用 **`Authorization: Bearer <device token>` 头**
    （长效凭据不进 URL/日志）——骨架仿 `sync_client.py`（绑定、重试、唤醒节流），
    WS 重连模式仿 `server/app/services/asr_service.py`；网络切换主动重连。
  - JSON-RPC 循环：收 `initialize`/`tools/call`/`tools/cancel`/`fs/*` →
    **本地 `server/app/tools/registry.py` 的 `registry.dispatch`** 执行 → 回 `result`/`error`；
    大文件按 `chunk {transfer_id, seq, data, sha256}` 分块回传。
  - **身份/工作区绑定**（复审 I-2）：executor 以本地单用户栈的属主账号执行（首次启动建立的本地用户），
    `workspace_path` = 既有 `ensure_user_workspace(owner)` 结果（`{slug}-{uid8}` 约定不变）；
    RPC 的 `workspace_ctx` 仅作逻辑名对齐，不接受云端任意绝对路径。
  - **skill_run_script**：RPC 载荷内联脚本内容（`script?` 字段），executor 物化到
    `{workspace}/skill_scripts/` 后走既有 `_exec_skill_script` 路径——设备不依赖云端用户技能库。
  - **超时**：`tools/call` 携带 `timeout_seconds`，executor 采用 `max(本地默认, 请求值)` 作为
    dispatch 等待上限（避免本地 `tool_call_timeout` 掐死长 terminal/execute_code）。
  - **设备侧白名单镜像（fail-closed）**：executor 自带 `allowed_tools`（与云端 §7 矩阵同源），
    未命中即拒——云端 bug/失陷不能驱动任意工具（复审 I-6）。
  - `tools/cancel` 映射：asyncio 任务取消 → 经既有会话/任务管理器杀进程
    （terminal 持久任务→job_cancel 路径；process 会话→session kill；execute_code→子进程终止）。
  - heartbeat 应答 + 掉线指数退避重连。
- 启动点：`server/main.py`（仿 `sync_client` 启动位点，main.py:253 附近）；受 `[client_exec] enabled` 开关。

### 1.2 配对/凭据（v2 方向定稿：云端签发配对码；复审 C-4）
- 流程：云端 web「添加设备」生成一次性短 TTL 配对码（绑定云端 user_id）→ 用户输入本客户端
  「设备执行」面板（仅本地 sidecar 配置动作）→ sidecar 凭码调云端 `POST /api/devices` 注册（TLS）→
  云端换发长效设备 token，仅此一次明文回传 → token 存
  `~/Library/Application Support/weave-thinker-client/localstack/app/` 下 0600 配置
  （仿 `localstack/gen_config.py`/`import_config.py` 的密钥落盘模式）。
- 新 UI 面板「设备执行」（仿 `app/src/components/SyncPanel.vue` 的绑定表单/设备表），
  挂 `SystemSettingsDialog.vue:154`：**P0 只读本地 sidecar 状态**（已连接/最后心跳/在线灯）、
  配对码输入、撤销/重置 token、云端地址配置。设备列表/撤销管理 UI 在云端 web（同源，无 CORS 问题）；
  客户端渲染器不直接调云端 API（P0），避免既有跨源缺口（README ⏳ CORS 待办）扩大。

## 2. 需要修改

### 2.1 安全收紧（必改；复审 I-7 范围修正）
- **`super_admin_bypass` 对 remote_call 强制失效**：包含性/逃逸检查不止 `server/app/tools/terminal.py:221`
  一处——terminal.py 内多处读全局 config（:130/:211/:220/:228/:286），process/git/code_execution
  亦有各自的 cwd/路径包含性检查（`process_session_service.py`、`git.py`/`git_binary.py`、
  `code_execution_service.py`）。实现：引入执行上下文（contextvar 或 dispatch kwarg `remote_call=True`），
  各包含性检查点按该上下文强制收紧（无视 config 的 `super_admin_bypass=true`）；本地 UI 主路径行为不变。
- executor 为远端调用构造 **strict 权限上下文**（`registry.dispatch` 的 `permission_context` 必须非空，
  否则 registry.py:206 权限检查整体跳过）；配合既有 `permission_manager`/`pre_tool_gate` 面。
- **`_permission_granted` 哨兵剥离**（复审 r2）：云端 voice（voice_service.py:5802）与 agent_loop
  自动放行路径会向 args 注入 `_permission_granted=True` 并随 RPC 过境，而 registry 权限闸
  （registry.py:206）与 terminal 包含性检查（terminal.py:286）均被该哨兵旁路——
  **executor 丢弃/无视入站 args 的 `_permission_granted`，`remote_call` 上下文下权限与包含性检查无条件执行**。
- 双侧白名单：本地 `allowed_tools` 镜像（§1.1）+ 云端路由白名单，任一不命中即拒。

### 2.2 文件代理支撑（复审 I-1 路径契约定稿）
- `fs/list|read|download` 复用既有 `workspace_files_service`/`workspace_paths` 读能力；
  **路径契约**：云端只传 workspace 相对路径，executor 用 `workspace_paths.is_within` 做包含性校验，
  逻辑 workspace 名与云端 `workspace_ctx`（`{slug}-{uid8}`）对齐——同名不同根由相对路径 + 逻辑名消解。

## 3. 不需要改动

- 全部工具实现（`server/app/tools/*`）、权限管理器本体、vibeweaver hooks——整套复用（仅加执行上下文收紧）。
- `app/` 通用 UI（新增面板除外）、sync 语义（**红线**：sync 契约两仓 byte-identical，本功能零触碰
  `sync_capture`/`sync_apply`/`SyncDevice`/`SyncEvent`）。
- 打包链（`electron-builder.yml`/`build_sidecar.sh`）：executor 在既有 Python sidecar 内，
  无新二进制、无新 extraResources。

## 4. 配置（与主方案键拼写统一，复审 M-1）

`server/config.toml` 新增：

```toml
[client_exec]
enabled = true
cloud_url = ""              # 留空=复用 sync 绑定的 server_url；否则独立云端 origin
heartbeat_seconds = 30
call_timeout_seconds = 120
max_payload_mb = 10
allowed_tools = [...]       # 与云端 §7 矩阵同源的镜像列表
```

`localstack/gen_config.py` 模板同步。`upstream.toml` 登记本项与 §1.1 为 local-mod（不回传上游）。

## 5. 生命周期/包装建议（非阻塞）

- 当前无托盘/登录自启/headless 守护——「客户端开着」= Electron 前台存活，约束偏硬。
  建议（P2）：菜单栏托盘 + `openAtLogin` + 崩溃自拉起，使设备可无人值守。
- Windows/Linux 安装器仍为 W6 缺口（现仅 macOS arm64 DMG），影响本功能可用人群。

## 6. 与 upstream sync 的关系

- 云端侧（chatbot）先行落地协议/`client_executor_devices` 表/`/api/device/ws`/路由缝后，
  本仓经一次 upstream 同步波取回 SPA 改动（输入框按钮/ChatRequest 字段/下载卡 URL 重写）与协议常量；
  executor worker 本体为本仓独有（不回传上游）。
- `upstream.toml` base 推进 + `script/linux/upstream_report.sh` 归零是同步波收尾动作。

## 7. 未来实现时的验证缝（供执行者参考，非本次交付）

- executor 回路冒烟：起本地 sidecar + 假云端 WS 端（测试夹具）→ 发 `tools/call terminal` →
  断言设备侧真实执行 + result 回传；内联 skill 脚本执行腿。
- 配对流程：配对码换 token → 连接成功 → revoke → 断连并拒绝新调用；错码/过期码拒绝。
- 安全腿：executor 路径 workspace 逃逸命令被拒（`super_admin_bypass` 失效实证，terminal/process/git/code_execution 四面）+
  白名单镜像拒绝未知工具。
- 断线语义：在途调用 failed（at-most-once）、重连后心跳恢复、离线 fail-fast、cancel 杀进程实证。
