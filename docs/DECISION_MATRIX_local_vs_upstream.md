# 评判标准：仅改本地客户端 vs 需要上游同步（评估必答项）

> 用户裁定（2026-10-03）：今后任何问题评估，**必须列出**本判定结论——是「仅改本地客户端代码即可」还是「需要上游同步修改（chatbot + 部署 60）」或「双端」。
> 配套契约：chatbot 仓 `memory/integration_weavethinker_sync_contract_20261002.md`；同步波 SOP：memory/project_upstream_sync_tracking_20260930.md。

## 1. 判定树（按顺序问，首个命中即停）

**Q1 改动的行为发生在哪个运行时？**
- 只影响桌面本地（本地 sidecar API / Electron 壳 / 本地 UI / 本地 worker 循环）→ **仅本地**
- 只影响 60 云端行为（Web 端用户可见、云端数据、服务端捕获、部署侧配置）→ **仅上游 + 部署 60**
- 同步事件语义（事件产生/应用/仲裁/实体域/schema）→ **双端**（红线文件 byte-identical 移植）

**Q2 涉及文件属于哪一类？（四象限）**

| 类别 | 判别 | 修改规则 |
|---|---|---|
| A. client-only | `main.py` 启动项·config 的 sync_client_*/sync_workspace_* 属性·`sync_client.py`·`sync_workspace.py`·`desktop/`·本地 UI 独有组件 | 仅本地改；上游不存在该代码 |
| B. 双端同源红线 | `sync_apply.py`（byte-identical 硬红线）·`sync_capture.py`·同步 API 面（`/api/sync/*` 全部端点，非固定清单）·同步 schema/实体域 | 双侧同改+逐字节移植；动任一侧必须同步另一侧 |
| C. 共享镜像 | `backend/app` ↔ `server/app` 其余镜像文件（conversation/chat/notes/memory/voice…）与 `frontend/src` ↔ `app/src` | 按语义归属：云端语义=上游+部署；本地语义=本地改（下轮同步会 conflict，按裁决规则处理）；用户裁定可「以上游为准」整取 |
| D. 文档/构建 | 文档、打包脚本、无映射的仓内自有测试 | 各自仓内改。⚠ **测试多数属 C**：upstream.toml 映射了 backend/tests→server/tests 与 frontend/tests/workflows→app/tests/workflows，映射内的测试改动=共享镜像，下轮同步会覆盖/冲突 |

**Q3 症状定位速查**
- 症状在本地面板/本地数据/本地启动/打包产物 → 本地
- 症状跨端不一致的**云端判定本身**（如仲裁规则错、云端捕获漏事件、Web 端行为错）→ 上游
- 「被云端仲裁拒绝」类要拆两层：**发射侧**（本地事件的时间戳/载荷/归属——本地修）vs **仲裁侧**（云端 apply 规则——上游修，红线 B）
- 数据漂移修复（本地新、云端旧）：优先设计「重发即自愈」的本地通道；避免依赖单向 DB 手术

**Q4 部署与交付含义**
- 仅本地 → 重打包 DMG（`script/linux/package_macos.sh`）→ 用户重装
- 涉上游 → chatbot 会话 `deploy_60.sh` 部署 → 若同时影响 client vendor → 上游同步波（SOP）→ 重打包 DMG
- 红线文件改动 → 两端都动 = 两端都要验证 + 部署 + 打包

## 2. 评估输出模板（每个问题评估时照抄填写）

```
[归属判定]
- 运行时: 本地 | 云端 | 双端
- 文件类别: A client-only | B 红线同源 | C 共享镜像 | D 文档/测试
- 结论: 仅本地 | 仅上游+部署60 | 双端（顺序: …）
- 交付动作: 重打包DMG | deploy_60 | 同步波 | 无
- 验证面: 本地测试… / 云端烟测… / 打包验证…
```

## 3. 常驻本地补丁登记表（同步波必查——触碰同区域必 conflict）

| 文件 | 补丁 | 同步波动作 |
|---|---|---|
| `app/src/api/chat.ts` | `API_ORIGIN` 运行时基址（协商端口注入；streamChat+resumeStream 双处，2026-10-07 W6 评审 Critical-2 修复） | 保留本地 hunk（守卫 test_local_patch_imports P2） |
| `app/src/stores/chat.ts` | `apiUrl()` 跨源 fetch 包装（桌面跨源必需）+ **载荷取回双保险**（`_fetchFieldFull` 内 `isExternalizedStub(payload.content)→false`，2026-10-06 W3 评审回植；守卫 test_payload_stub_guard G3b） | 保留本地 hunk（merge 非冲突区自动保留；冲突区手工回植） |
| `server/app/services/voice_service.py` | `_keepalive_run_task_params` SSOT（听写断连根治，含 FunASR VAD 四参数） | 保留本地 SSOT 形态（是上游内联参数的超集） |
| `server/main.py` | sync worker start/stop + sync_workspace 路由 | 上游 main.py 无此段——恢复（守卫 test_main_sync_wiring） |
| `server/app/core/config.py` | 12 个 client 专属 sync 属性 | 上游 config 无此段——恢复（守卫同上） |
| `server/tests/test_main_sync_wiring.py` | 接线+属性守卫 | 本地自有，勿被上游同名覆盖 |
| `server/tests/test_server_drain.py` | stop 脚本路径适配 vendor（scripts/stop.sh→script/linux/localstack.sh，契约断言原样；2026-10-06 W4） | 保留本地路径 hunk（下波 conflict 按规则处理） |
| `server/app/api/notes.py` | ~~bulk-export endnote_enabled 热修~~ **已销账（2026-10-07 W6）**：上游 b92544fd0 同款修复（查询参数模式，注释引用本仓 TODO 项 7），vendor 取上游（残差 +0/-0）；测试件=上游超集版（含穿线断言） | 无需本地 hunk |
| `app/src/composables/useVoiceDuplex.ts` | connect() settle 守卫（接通前关闭/1013 排空不再永久挂起；2026-10-06 W5 评审 B I1） | 保留本地 hunk（上游若修同点则取上游） |
| `server/app/api/message_payloads.py` | ~~payload 归属寻址修复~~ **已销账（2026-10-06 W3）**：上游 deceebaca 同修且为超集（归属寻址+sha256 完整性 404+size_bytes 字节语义），vendor 已取上游（残差 +0/-0）；守卫测试=上游超集版（含完整性/字节腿） | 无需保留本地 hunk（双方一致）；守卫测试保留在盘 |
| `app/src/components/ChatInput.vue` | caret-follow 行尾换行兜底（`measureCaretLineRect` ZWSP 行盒测量 + isComposing/非常规选区守卫；2026-10-08 W11） | 保留本地 hunk；E2E `tests/ui_chatinput_shift_enter.py` 钉行为 |
| `app/src/styles/themes/{mono-brutal,verdant-flat,ink-paper}.css` | `.input-text-area .chat-input` `flex: 1` → `flex: 1 0 auto`（行尾空行行盒保留；2026-10-08 W11） | 保留本地 hunk（三皮肤同改）；上游若动此规则按语义合并 |
| `server/app/tools/skill_tools.py` | ~~`_get_skills_dir` → resolve_resource~~ **已销账（2026-10-09 W14）**：上游 2c1db5d50 A5 同化（converged，逐字节一致） | 无需本地 hunk |
| `server/app/services/device_executor_service.py` | **设备执行器 worker 全新（W16，PLAN_device_executor §1.1，client-only 不回传）**：出站 WSS+JSON-RPC+dispatch+分块+cancel 杀进程 | 上游不存在——本地自有，同步波残差预期 +N/-0 |
| `server/app/services/device_pairing_service.py` | 配对/凭据 store 0600（W16 §1.2，全新 client-only） | 同上 |
| `server/app/tools/execution_context.py` | remote_call 执行上下文（W16 §2.1，全新） | 同上 |
| `server/app/api/client_exec.py` | 本地「设备执行」面板 API（W16 §1.2，全新 client-only） | 同上 |
| `app/src/components/DeviceExecPanel.vue` + `app/src/api/clientExec.ts` | 设备执行面板（W16，本地 UI 独有组件） | 同上 |
| `app/src/components/SystemSettingsDialog.vue` | ~~加 `device-exec` tab（W16）~~ **W22 已改**：「云端关联」单 tab（cloud-link，吸收 SyncPanel+DeviceExecPanel，acceptance #20）；**W23/D-109**：上游 W18 `DeviceExecutionPanel`「设备执行」tab **不引入**（保持 W22 形态；上游面板 vendored 未接线） | 保留本地 hunk（cloud-link tabs+图标+内容分支+workspace tab；上游 device-exec tab/面板/import 裁决=不取，D-109） |
| `server/main.py` | device_executor start/stop + client_exec 路由（W16；守卫 test_main_device_executor_wiring） | 上游 main.py 无此段——恢复（同 sync_client 先例） |
| `server/app/core/config.py` | `[client_exec]` 7 个属性（W16；上游 config 无此段） | 上游无此段——恢复 |
| `server/app/tools/{registry,terminal,git,process_tool}.py` + `server/app/services/code_execution_service.py` | remote_call 生效值收紧（W16 §2.1）+ CancelledError 杀进程；registry remote timeout lift | 按语义归属：安全收紧=本地防线（上游如采同化则取上游超集）；合并波逐 hunk 核对 |
| `server/tests/test_{execution_context,device_executor_service,client_exec_pairing,main_device_executor_wiring,websockets_module_hygiene}.py` | W16 守卫/回路测试（本地自有） | 勿被上游同名覆盖 |
| `server/app/services/device_executor_service.py`（W20） | 身份绑定链 `resolve_execution_identity` + 名门期望值改**云端身份导出**（跨栈 uuid 不参与）；`identity_unbound` fail-closed | 本地自有核心；同步波残差 +N/-0 |
| `server/app/api/client_exec.py` + `device_pairing_service.py`（W20） | 配对记录身份三键（local_user_id/cloud_user_id/cloud_username）+ status.identity_bound | 同上 |
| `server/app/db/database.py` + `db/migrations.py` + `api/sync.py`（W20） | SyncState.cloud_user_id 列 + 绑定取 /api/auth/me 落云端身份 | 同上 |
| `app/src/api/clientExec.ts` + `components/DeviceExecPanel.vue`（W20） | identity_bound 类型+面板提示（本地 hunk） | 保留本地 hunk |
| `server/tests/test_asr_dictation_keepalive.py` | `_MP` 假夹具→真 monkeypatch（harness 修复断言不变，W16） | 上游同文件若有同点修复按超集取 |
| `server/app/services/vibeweaver_gate_service.py` | ~~resolve_resource 物化/python3 回退/项目根注册表~~ **已销账（2026-10-09 W14）**：上游 A3 采纳且为超集（hint 可解析无归属→None 防 B 项目误封，复审 A-I1 细化）；合并残差=0 | 无需本地 hunk |
| `server/app/services/vibeweaver_audit_service.py` | ~~skill.activated 消费/terminal 写观察/resolve_session_root~~ **已销账（2026-10-09 W14）**：上游 A4 同化（converged） | 无需本地 hunk |
| `server/app/services/agent_loop.py` | ~~`_completion_guardrails_tail` 抽取/direct_reply 补门禁~~ **已销账（2026-10-09 W14）**：上游 A2 采纳且为超集（reasoning 累积+被拦先 audit_reset 防叠影）；合并残差=0 | 无需本地 hunk |
| `server/app/api/skills.py` | ~~frontmatter 块标量 `_parse_skill_md`~~ **已销账（2026-10-09 W14）**：上游 A1 同化（converged） | 无需本地 hunk |
| `server/app/api/user_settings.py` · `services/workspace_service.py` · `services/user_preferences.py` · `api/skins.py` | 工作区根用户级覆盖（ui_preferences 合并写）+ /api/users/me/workspace | 保留本地 hunk（W12 P2#10） |
| `localstack/import_config.py` | 模板补齐 `[agent.skills]/[hooks]/[vibeweaver]` 缺失段/键 | 本地自有工具（无上游映射） |
| `app/src/api/userSettings.ts` · `components/WorkspaceSettingsPanel.vue` · `components/SystemSettingsDialog.vue` · `app/package.json` | 设置「工作区」页签（查看/选择/保存/恢复默认） | 保留本地 hunk（W12 P2#10；上游同修更佳） |
| `app/src/api/streamDispatch.ts` · `app/tests/workflows/test_stream_true_guard.cjs` | ~~致命分支排除 tool_result 形状 + guard 4 新腿~~ **已销账（2026-10-09 W14）**：上游 734d76426 逐字节采纳（converged）；上游 test:unit 链亦已接 guard | 无需本地 hunk |
| `server/tests/test_config_resource_resolver.py` | 上游超集版 + 本地独有 2 腿 `test_localstack_config_path_is_the_fallback` / `test_weave_wins_over_localstack`（desktop 侧车 LOCALSTACK_CONFIG_PATH 契约与优先级，上游无此变量；2026-10-09 W14 test-change） | 保留本地腿（合并时取上游版+回植本地腿） |
| `app/src/components/MessageBubble.vue` | `fileRefRelPath` + `classifyFile/workspaceRelPath` import + 预览绑定 `:rel-path/:source-path`（下载 URL 工作区相对化） | 保留本地 hunk（合并后 grep import/callsite 成对） |
| `app/src/composables/useMarkdown.ts` | `import { apiUrl }` + 2 调用点 + `src.includes('/api/files/download')` 守卫（跨源下载；W6 吞并事故点） | 保留本地 hunk（合并后必查 import/callsite 成对） |
| `server/app/services/office_preview_service.py` | **`convert_workspace_document` 抽函数共用（W19 S2 支撑）** + `open_regular_nofollow`/`spool_fd_to_temp` 自 files.py 迁入 + `OfficePreviewPreparationError`（转换阶段 I/O 故障≠404≠422） | 上游 files.py 仍内联冻结→转换——保留本地 hunk；上游若同化按超集取 |
| `server/app/api/files.py` | office 两端点改调 `convert_workspace_document`（W19 行为保持重构，404/500/501/422 映射逐句保持）+ `_prune_stale_zips` 委派共用实现 | 保留本地 hunk（冲突区按语义合并） |
| `server/app/services/workspace_files_service.py` | `prune_stale_zips` 上移共用（HTTP /zip 与设备 fs/zip 同一收割面）+ 目录条目 type `"folder"`→`"dir"`（W19 S3 契约规范值） | type 语义=PLAN §5 规范（云端 DIR_TYPES 已收）；prune=本地抽函数 |
| `server/app/services/device_executor_service.py`（W19 增量） | 三 RPC `fs/zip`·`fs/office-html`·`fs/office-pdf` + `_fs_tasks` 请求 id 字典化（tools/cancel 可中止 fs/*）+ `WorkspacePathError` 双码前缀修复 + 白名单镜像 16 名 | 同 W16 行（本地自有，同步波残差预期 +N/-0） |
| `app/src/composables/filePreview.ts` | `BACKEND_TYPE_KIND` 增 `dir:'folder'`（W19 S3 兼容键，保留 `folder`=旧附件模型） | 保留本地 hunk |
| `server/API.md` | 设备执行器节：三方法契约 + 16 白名单 + `office_preview_*` 错误码 + 字节通道等待窗注记（W19 S4） | 契约事实源随实现更新（非漂移）；与上游 PLAN §5 对账 |
| `app/src/components/MessageBubble.vue`（W23） | `fileRefUrl` 设备分支 `apiUrl(buildDeviceDownloadUrl(...))` 包裹（评审 C1：app:// 对 /api/* 404；守卫 test_device_url_apiwrap P1/P2） | 保留本地 hunk（合并后 grep import/callsite 成对） |
| `app/src/components/FilePreviewDialog.vue`（W23） | `deviceIdOfUrl` 容错绝对形式嗅探（`/api/devices/<id>/` 出现在任意 origin path 即设备上下文；守卫 P3/P4） | 保留本地 hunk |
| `server/app/api/{notes,conversation}.py` + `server/app/api/skins.py`（W23） | 可写目录落 `config.backend_root`（output_files×5 / skins_custom；LINUX_DEB 缺陷 4 类全扫，守卫 test_writable_runtime_paths） | 保留本地 hunk（上游若同化按超集取） |
| `script/linux/localstack.sh`（W23） | drain 令牌读侧 CONFDIR 优先（写侧 backend_root 同基址；SERVER 兜底旧布局；评审 I3） | 保留本地 hunk |
| `server/config.toml`（W23） | `[agent.skills] force_inject=["vibeweaver"]` 对齐 config.toml.example 模板默认（D-108；上游测试依赖实例配置） | 本地实例配置，不入同步域 |
| `app/src/api/workspaceFiles.ts`（W23） | 全部 URL builder `apiUrl(...)` 包裹（跨源基址）+ 本地 sync-status 函数块；上游 deviceId 参数并集 | 保留本地 hunk（双 import 并集：apiUrl + deviceUrls） |
| `app/src/components/CloudLinkPanel.vue`（W22+W24） | 云端关联合并面板全件（C1 account 独立取/I5 busy 互斥/I3 名称回灌/B-M4 device_id 仅同云/#21 统一地址）+ W24 冲突分页状态机（pagerBusy 锁+令牌回滚）与 resync 后 loadAll 补刷 | 本地自有组件（上游无此件）；上游 DeviceExecutionPanel 不引入（D-109） |
| `app/src/api/sync.ts`（W24） | `getSyncConflicts(limit, offset)` 消费既有服务端分页契约（红线 B 面零变更） | 保留本地 hunk（合并后核对 callsite 成对） |
| `app/src/components/SystemSettingsDialog.vue`（W24） | `.settings-tabs` flex-shrink:0+overflow-y:hidden / `.settings-tab-content` min-height:0（tab 条防挤压契约） | 保留本地 hunk（见上 W22 行；两行同文件） |
| `server/app/api/client_devices.py` + `services/client_device_service.py`（W25） | `POST /{device_id}/rename`（凭 device token）+ `rename_device`——**handoff ③ 形状落地 vendor**（上游采纳此形状即同形） | 上游无此端点——同步波残差 +N/-0；上游若实现按超集取 |
| `server/app/api/client_exec.py` + `services/device_pairing_service.py`（W25） | rename 推送链 `push_rename_to_cloud`（2xx 防伪锚 renamed:true+降级回执）+ renamed_cloud/cloud_note 回执 | 保留本地 hunk（W22 rename 契约扩展） |
| `app/src/api/clientExec.ts` + `components/CloudLinkPanel.vue`（W25） | DeviceExecRenameResult+renamed_cloud/cloud_note 类型 · 回执如实（云端已同步/未同步+原因） | 保留本地 hunk（合并后 grep import/callsite 成对） |
| `app/src/stores/chat.ts`（W26） | ~~flushUnconfirmedInterjections auto-send 分支 drain/判重前 `await refreshConversation(convId)`~~ **已销账（2026-10-24 W27）**：上游 handoff ④ A1 采纳且增强（refresh→flush 串行+reuseFresh 逐字同源，守卫 test_interjection_flush_refresh R4e 反为上游超集）；同文件 apiUrl/载荷双保险本地 hunk 仍在场（上表 W3 行） | 无需本地 hunk（行内其它 hunk 见 W3 行） |
| `app/src/components/SortableList.vue`（W26） | ~~initSortable 扼流守卫 + forceReinit 不自带 destroy~~ **已销账（2026-10-24 W27）**：上游 A2 采纳并增强（模块级全局态+世代令牌+dragIsGroup）；**W27 新增**：L183 `vw-approved` 标记（`const token = draggedItemStore.dragToken` 计数器引用=模式面误报，D-110 同型） | 保留本地 hunk（vw-approved 标记行；secret-approved 在案） |
| `app/src/components/Sidebar.vue`（W26） | ~~groupsDragGroup 模块级稳定常量 + 拖拽 failsafe~~ **已销账（2026-10-24 W27）**：上游 A3 采纳并增强（dragCancelRequested 延迟取消+2s 有界兜底+finishCancelledDrag+迟到 end 守卫） | 无需本地 hunk |
| `app/src/styles/sidebar-panels.css`（W26） | ~~折叠组拖拽中 0fr+44px 放置条~~ **已销账（2026-10-24 W27）**：上游 A4 采纳（内容全等，注释措辞取上游） | 无需本地 hunk |
| `app/src/components/ZenHistoryDrawer.vue`（W26） | ~~zenGroupsDragGroup 稳定引用（A5 同类残留）~~ **已销账（2026-10-24 W27）**：上游采纳（合并波去重复定义后单件） | 无需本地 hunk |
| `app/src/components/ChatInput.vue`（W27） | caret 链保 ours（W11 三级链+`measureCaretLineRect` IME 守卫；上游 `measureCaretRect` 同算法重复实现删）+ 空目标文案模式分流折入上游 `displayedExecutionTarget` 新结构（W26 #56 语义保持） | 保留本地 hunk（合并后守卫 test_exec_mode_wiring + E2E ui_chatinput_shift_enter 钉行为） |
| `app/package.json`（W27） | test:unit 链并集（上游链为序 + ours-only 7 件追加：asr_reconnect/payload_stub_guard/local_patch_imports/workspace_root_setting_wiring/exec_mode_wiring/linux_packaging_config/device_url_apiwrap） | 保留本地 hunk（同步波零测试丢失） |
| `app/tests/workflows/test_w26_echo_channel.cjs`（W27） | vendor 布局适配：`read('backend'→'server')`×5 · `read('frontend'→'app')`×4 · package.json join×1（上游 repo 布局 vs [[map]] 映射；断言面零改动） | 保留本地 hunk（下波 conflict 按规则回植） |
| `server/app/services/workspace_service.py`（W27） | `default_workspace_root` 采纳上游 tenant `scoped_root`（无租户段恒等回落）+ W12 P2#10 用户覆盖层保留在 `ensure_user_workspace` | 保留本地 hunk（覆盖层） |
| `server/app/tools/terminal.py`（W27） | W16 fail-closed 生效值并集：`super_admin_bypass_effective()`×5 / `permission_sentinel_effective(args)`×2 替换 raw `config.super_admin_bypass`/`args._permission_granted`（remote_call 下恒 False 安全面不随上游放宽） | 保留本地 hunk（合并后 raw 用法 grep=0） |
| `server/app/api/client_devices.py` + `services/client_device_service.py`（W27） | ~~W25 rename 端点~~ **handoff ③ 上游落地即销账**：上游实现为超集（rename vs revoke 竞态→LookupError→404 + StaleDataError 捕获），vendor 取上游单件（重复定义清剿后） | 无需本地 hunk（W25 推送链 hunk 见 W25 行） |
| `server/app/core/config.py`（W27） | `get_config` 改手工单例+脏标记**惰性就地重读**（`get_config.cache_clear` 垫片兼容直呼）——热重载不换对象，杜绝「补丁打新对象/服务读旧对象」身份劈裂（W20 已知类根治，test_tma 全量序污染实证） | 保留本地 hunk（下波上游若改 get_config/lru_cache 形态按此语义并集） |
| `server/app/tools/execution_context.py`（W27） | `super_admin_bypass_effective(cfg=None)` 增注入参（缺省 get_config()；调用方传模块级 config 保 monkeypatch 接缝） | 保留本地 hunk（W16 生效值 SSOT 的参数化） |
| `server/tests/conftest.py`（W27，新） | autouse 逐测排空 async engine 连接池（`engine.sync_engine.pool.dispose()`）——短命 asyncio.run 循环毒化全局引擎池类根治（W20 类） | 本地自有（上游无 conftest.py）；勿被同名覆盖 |
| `server/app/services/workspace_service.py`（W27 修订） | `ensure_user_workspace(..., user=None, allow_repoint=False)`：显式迁移授权（设置 API 两路径置 True 重指 root/node/python_env 三键）∪ 上游 no-silent-move（缺省保持久化根+warning） | 保留本地 hunk（覆盖层+显式迁移语义；守卫 test_workspace_root_override 5/5） |
| `app/src/components/WysiwygEditor.vue`（W28） | 表格右键菜单剪贴板四项（复制/剪切/粘贴/全选 + `_hasSelection` 空选区显式反馈 + 粘贴走编辑器 onPaste 管线）· A#8 macrotask 挂载（`setTimeout 0` 后挂 once 监听）· B#1 开菜单先 `closeTableContextMenu()` · `hasPendingImageUpload` expose（上游 1781 行重构后与上游 6 新 expose **并集共存**） | 保留本地 hunk（运行态守卫 `tests/ui_wysiwyg_table_menu.py` 12 断言 PASS） |
| `app/src/components/NoteEditor.vue` · `app/src/components/ZenNotePanel.vue`（W28） | ⌘S/Ctrl+S 保存谓词（恒 preventDefault 先行 + 组字/repeat 只免落盘 + 面内焦点判定）· `runSave` 在途去重 + `saveNote` 存到干净 ≤3 轮 + `doSaveNote` 快照契约（PUT 在途键入不误清脏态）· `notesStore.currentNote` 同步回灌 watch（initialLoad 门）· 离场 `hasPendingImageUpload` 脏态腿 · Zen `hasPendingUpload` expose | 保留本地 hunk（运行态守卫 `tests/ui_note_cmd_s.py` 39 断言 PASS） |
| `app/package.json`（W28 更新 W27 行） | test:unit 链并集 **47 件** = 上游链为序（40，含本波新 selection_restore/clear_formatting/multi_range）+ ours-only 7 件追加（asr_reconnect/payload_stub_guard/local_patch_imports/workspace_root_setting_wiring/exec_mode_wiring/linux_packaging_config/device_url_apiwrap）；name/version/capacitor 依赖/description 保持本地 | 保留本地 hunk（同步波零测试丢失；链数断言=47） |
| `server/main.py`（W28 补充） | 上游新增 `loop_governor_service.install()` 挂载（Tier A fail-open）与本地 `sync_client`/`device_executor` 启停**并集共存**；本地路由两行 + `backend_root` 可写基址 + drain 令牌 0600 兜底全存活 | 保留本地 hunk（守卫 test_main_sync_wiring / test_main_device_executor_wiring） |
| `server/app/core/config.py`（W28 修订 W16 行） | **口径更正**：上游现已自带 `[client_exec]` 属性段（旧登记「上游 config 无此段」失效）——本地为其**超集**（heartbeat/call_timeout 下限 5 · `call_max_extension_seconds` 非有限值回落 3600 · `allowed_tools` 16 名缺省 + 显式空表 deny-all）→ 三方合并取 ours 单件，同名方法重复定义清剿 0 命中 | 保留本地 hunk（超集语义；下波上游若再扩该段按并集口径） |
| 实例配置校准（**非同步域**·`server/config.toml` gitignored）（W28） | 对齐上游 2026-10-25 审计取值：`max_consecutive_iterations 999→60`（F4）· 停滞覆盖表键 `code_execution→execute_code`（F7：旧键精确查表不命中=看门狗静默失效）——上游新测试 `test_loop_governor.py` 读实例配置实际值断言，同 **D-108** 型（第三次） | 本地实例配置，不入同步域（D-116） |
| `server/config.toml.example`（W28，**本仓 local-mod 打包模板**） | 同两值随实例配置一并修齐（`999→60` · `code_execution→execute_code`）——分类账实证该件 `local-mod`（上游 09679fda 侧仅 10 行 `[sync]` stub），是本仓 `localstack/import_config.py` 的 `_TEMPLATE`：不修则**打包态与干净检出继承 F7 死键**（execute_code 停滞看门狗静默关闭）且 `test_f4`/`test_f7` 在新环境必红（RED→GREEN 证据 tests/w28_iter7_example_proof.log） | 本地自有模板（**不是**上游缺陷——评审 F1 推翻首版 T1 前提，D-118） |
| `app/src/components/ChatArea.vue`（W28 观察，**不修**） | `TOOL_NAME_MAP` 以遗留名 `code_execution` 建键而注册名=`execute_code` → 工具标签回落英文；上游同文件同行同键、本仓分类 in-sync | 无需本地 hunk（上游自有缺陷 → handoff T1，不为显示层回落开 hunk 制造漂移） |

### W31/W32 补登（2026-10-28 W33 登记波，用户指令「做好登记」——W32 波漏登项在此立正）

| 文件（域内共享件） | 本地 hunk（同步波保什么） | 处置 |
|---|---|---|
| `app/src/stores/chat.ts`（W32） | `settleRelayedTurn`（中继 turn 后同步等待+postSyncNow 限频早退）· `resolveDefaultExecutionTarget` 云端注册表腿+客户端默认「本地」· 四条 turn 尾锚（send/regenerate×2/edit-resend）settle 调用 · `EXECUTION_MODE/getCloudDevices/getDeviceExecStatus/getSyncStatus/postSyncNow` import | 保留本地 hunk（守卫 test_exec_env_menu_w32 M9；上游 deathmatch intent 守卫 hunk 并集直采） |
| `app/src/components/ChatInput.vue`（W32） | 执行环境菜单三选一重写（本地/云端/其他绑定客户端）· 显示映射（device:<self>=本地/null=云端/other=注册表名）· `selectLocalTarget` 防误绑守卫 · `refreshExecContext` 挂载预热 · getCloudDevices 形状防御（linked 缺失=保守 false）· 配对码区块 `EXECUTION_MODE==='cloud'` 门控 · `otherDevices` 含撤销态 | 保留本地 hunk（守卫 test_exec_env_menu_w32 M1-M8/M10 + E2E ui_exec_env_relay.py） |
| `app/src/components/MessageBubble.vue`（W32） | `isSelfDeviceTarget` 自目标文件面本地化（不走设备代理）+ `selfReady` 响应式 priming | 保留本地 hunk（守卫 test_exec_env_menu_w32 M11） |
| `app/src/types/index.ts`（W32） | `ClientDevice.is_self?: boolean` 字段 | 保留（向后兼容可选字段） |
| `app/package.json`（W31+W32） | test:unit 链并集追加 `test_deathmatch_review_r2.cjs`、`test_exec_env_menu_w32.cjs` | 并集（上游再加条目同 W31 package.json 处理法） |
| `server/main.py`（W32） | `CloudRelayMiddleware` 注册——**注册序=先于 CORSMiddleware（CORS 包住中继）**，评审 Critical 修复 | 保留本地 hunk（user_middleware 顺序断言在 test_cloud_relay_service 体系；顺序错=中继响应无 ACAO） |
| `server/app/services/remote_dispatch_router.py`（W32） | 自目标短路：`target == device:<load_binding().device_id>` → return None 本地执行（fail-closed 对非自目标原样） | 保留本地 hunk（守卫 test_exec_env_self_target.py） |
| `server/API.md`（W32） | 「Cloud Device Registry」节：`GET /api/client-exec/cloud-devices` + `POST …/{device_id}/revoke` 契约 | 保留本地节（同 client-exec 节先例，client-only 不回传） |
| `app/src/api/clientExec.ts` · `app/src/utils/selfDeviceId.ts` · `server/app/api/client_exec.py` · `server/app/services/cloud_link_service.py` · `server/app/services/cloud_relay_service.py`（W32） | **local-only**（上游无对应文件/功能：客户端专属中继与云端注册表代理） | local-only 不冲突（账本 local-only 类）；cloud_link_service 两行 `vw-approved` 凭据管线（secret-approved×2 在案） |
| `server/app/services/sandbox_env.py` · `spawn_registry.py` · `tests/test_sandbox_env_f1.py` · `test_spawn_registry_f2.py`（W31） | 上游直采件（sha 全等）非本地 hunk | 下波随 ff-safe 直采更新 |

## 4. 实例校准（已发生案例）

| 案例 | 判定 | 依据 |
|---|---|---|
| 事故② 全量同步永挂（worker 不启动） | **仅本地** | 行为在本地 sidecar；main.py/config 属 client-only（A） |
| 事故① 仲裁拒绝×24（reorder 同值赋值旧 ts） | **仅本地**（修发射侧） | 云端仲裁规则正确；本地事件发射缺 bump（C 类文件但修复语义仅本地生效；上游后由其自行同化——已发生） |
| 云端 memory 路径 text() 写漏捕获（chatbot 会话修复） | **仅上游+部署** | 服务端捕获缺口，Web 用户行为（B/C 类上游侧） |
| ASR qwen3.1 迁移 + 部署 60 | **上游+部署，且 client vendor 跟随**（C 类） | 云端新功能 + vendor 镜像漂移由同步波收敛 |
| sync_apply LWW 规则调整 | **双端**（红线 B） | 仲裁双端逐字节一致是契约 |
| `app/src/stores/chat.ts`（W26） | `flushUnconfirmedInterjections` auto-send 分支 drain/判重前 `await refreshConversation(convId)`（try 包裹；插话重复气泡根治——flush 先于周期 refresh 执行致去重门扫旧列表，上游 D-62 parked 同类） | 保留本地 hunk（守卫 test_interjection_flush_refresh）；上游若修 D-62 按超集取 |
| `app/src/components/SortableList.vue`（W26） | initSortable 扼流守卫（`draggedItemStore.isDragging && sortableInstance` → 禁 destroy 重建）+ forceReinit watcher 不再自带 destroy（SortableJS destroy-during-drag=_nulling() 清全局态卡死，Vue.Draggable#1199 同源） | 保留本地 hunk（守卫 test_sortable_drag_guard D1/D2） |
| `app/src/components/Sidebar.vue`（W26） | `groupsDragGroup` 模块级稳定常量（替换内联 :group 字面量）+ 拖拽 failsafe（onDragStart 注册 window mouseup/touchend/Escape → end 未达 150ms 后 forceEndDrag 全清理复位；正常 end 双路径摘除兜底监听） | 保留本地 hunk（守卫 test_sortable_drag_guard D3/D4；E2E tests/ui_sidebar_drag.py） |
| `app/src/styles/sidebar-panels.css`（W26） | `.sidebar-dragging .group-convs-collapsed` 不再 `grid-template-rows:1fr` 全展开 → 保持 0fr + 44px 放置条（`> .sortable-list` min-height 44px 命中区）；悬停组仍由 dragExpandedGroupId 单独展开 | 保留本地 hunk（守卫 test_sortable_drag_guard D5） |
| `desktop/{main.cjs,preload.cjs}` + `app/src/api/base.ts` + `app/src/components/ChatInput.vue`（W26） | 云端/本地执行客户端层语义拆分：main.cjs 仅在 runtimeApiBase 分支注入 `--weave-exec-mode=local` → preload 白名单 → `weaveRuntime.execMode` → base.ts `EXECUTION_MODE`（runtime 优先/烘焙 loopback 推断/默认 cloud）→ ChatInput 空目标文案按模式分流「本地（本机）」/「云端服务器」 | desktop/* 本地自有；base.ts/ChatInput.vue hunk 保留（守卫 test_exec_mode_wiring） |

### W35 合并/直采登记（2026-10-29 W35 同步波，chatbot 4cb33ddaa→477c4263 deathmatch R16）

| 文件 | 补丁 | 同步波动作 |
|---|---|---|
| `app/src/stores/chat.ts`（W35 合并） | 上游 deathmatchIntent 代际语义并集吸收（`_deathmatchIntentSeq`/`deathmatchIntentAtRun`/selectConversation 同步定界/preserveExplicitStart 显式化/陈旧 verdict 不擦新 ON）；本地 hunk 全留存（W32 settleRelayedTurn 定义+4 turn 尾锚 · W3 桩守卫 · apiUrl 包裹×3 · EXECUTION_MODE 默认本地+云端注册表腿） | 保留本地 hunk（守卫 test_payload_stub_guard G3b · test_exec_env_menu_w32 M9/M11 · test_deathmatch_review_r2 21 检查） |
| `app/src/components/Sidebar.vue` + `app/src/components/ZenHistoryDrawer.vue`（W35 合并） | 上游 handleNewChat 空复用路径「点击即任务边界」清 `deathmatchMode/deathmatchAction`（各 +4 行）；本地 hunk 留存（Sidebar 空白×2 · Zen 评审 A-M3 注记） | 保留本地 hunk（守卫 test_deathmatch_review_r2 clearsBeforeEmptySelect + test_sortable_drag_guard） |
| `app/tests/workflows/test_deathmatch_review_r2.cjs`（W35 合并） | 上游 +3 检查（ON A→B 泄漏/armed ON 不旁路/empty-reuse 边界先于 await）并集吸收；本地 `backend/→server/` 路径映射 hunk 留存 | test-change 登记（W31 路径映射行沿用，本波合并=上游测试内容并集） |
| `server/app` 死磕域 11 件 + `server/tests` 7 件 + `app/e2e/deathmatch_feedback_scope.spec.ts`（W35 直采） | 上游直采件（sha 全等 18/18，`tests/w35_directcopy_reconcile.txt`）非本地 hunk；含 new-upstream `test_deathmatch_r16_fail_closed.py` | 下波随 ff-safe 直采更新 |

### W36 合并/直采登记（2026-11-05 上游同步波，chatbot 477c4263→a228359f）

| 文件 | 补丁 | 同步波动作 |
|---|---|---|
| `server/app/api/chat_persistence.py` · `chat_stream_transport.py` · `sync_capture.py` · `server/scripts/backfill_message_tombstones.py`（W36 直采） | 上游 2026-10-31 消息删除同步 P0+P1+P3（`_delete_message_slice` ORM 重查+flush、stop/legacy resume reasoning 守卫、存量 backfill 脚本），sha 全等 | 直采；本仓 `server/tests/test_edit_resend_delete_events.py`·`test_stop_reasoning_only_p1.py`·`test_backfill_message_tombstones.py` 同波直采 |
| `server/app/services/sync_client.py`（W36 本地） | 下游 L2：远端 messages delete 后清待推队列 + 本地 SubconsciousLog/检索缓存/续聊摘要失效（对齐服务端 purge 面） | 本地自有；守卫 `server/tests/test_sync_message_delete_consumers.py` |
| `server/scripts/cleanup_orphan_messages.py`（W36 本地） | 本地孤儿清理（assistant/空正文/无工具/无前置 user；dry-run 默认）；已对 conv d7ea65b8 执行删除 f697cf2d | 本地自有；跨设备存量由上游 P3 backfill 承担 |
| `desktop/main.cjs`（W36 本地） | macOS `activate`/`second-instance` → `focusOrCreateWindow`；closed 清 mainWindow；hidden/minimized show/restore；`--activate-probe` 测试接缝 | 本地自有；守卫 `app/tests/workflows/test_dock_activate_reopen.cjs` + runtime probe |
| `app/src/components/NoteEditor.vue` · `ZenNotePanel.vue`（W36 合并） | 保留本地「存到干净（≤3 轮）/manualRequested/⌘S/刷新回灌/离场上传守卫」；并入上游 `flushPendingImageUploads`/`flushPendingSerialization`；`saveNote` 返回 boolean 修复导出判定 | 保留本地 hunk；构建 + test:unit 全绿 |
| `app/src/stores/chat.ts` · `ChatInput.vue` · `MessageBubble.vue` · `types/index.ts` · 三主题 CSS（W36 合并） | 上游 interjection/MCP/voice delegation/agent task 新增面并集；本地 deathmatch 意图代际、W3 桩守卫、apiUrl 包裹、EXECUTION_MODE、皮肤令牌 hunk 全留存 | 保留本地 hunk；守卫 test_deathmatch_review_r2/test_payload_stub_guard/test_exec_env_menu_w32/test_sortable_drag_guard |
| `server/app/core/config.py` · `db/database.py` · `db/migrations.py` · `main.py` · `tools/registry.py` 等（W36 合并） | 上游 MCP 管理/语音委派/agent task 工具/迁移新增面并集；本地 sync/client-exec/device-exec 配置与路由 hunk 留存 | 保留本地 hunk；server 4126/0 |
| `server/API.md`（W36 合并） | 上游 §13 语音主 Agent 异步委派（改为本仓 §14）+ 本地 §13 同步 Sync + 设备执行器章节并存 | 本地文档 hunk；接口规模注记按「上游 195 + 本仓增量」 |
| `app/package.json`（W36 合并） | test:unit 56 链并集（本地 49 + 上游 vue-router/tool status/interjection inline/note ref/voice delegation 新链 + dock）；补 prebuild（vue-router backport）/postinstall 垫片；patch-package + @playwright/test | 并集；`app/scripts/postinstall.cjs` workspace 垫片；守卫 prebuild + test:unit |
| `app/e2e/_archive/deathmatch_feedback_scope.spec.ts`（W36 归档） | 上游 2026-11-04 清债波把根 e2e spec 归档；本仓随迁，`test_deathmatch_review_r2` 路径改为 archive | 旧根路径删除，test-change 登记 |
| `server/skills/rempilot-mcp/**` · `app/src/components/QuickNoteButton.vue`（W36 上游删除） | 上游 2026-11-01 `chore(mcp): remove global rempilot server and skill` + QuickNoteButton 清理 | 本仓删除；`server/config.toml.example` 同步去掉 rempilot 示例块（live config 不动） |
| `tools/add_license_headers.py` · `scripts/{evidence_lock,evidence_seal,scan_secrets}.py` · `app/patches/vue-router+4.6.4.patch`（W36 直采） | 上游 license-header `--check`、根级 runtime helper parity、vue-router #2704 backport | 直采并补 `upstream.toml` filemap；`app/scripts/postinstall.cjs` 适配 workspace 提升 |

### W38 本地分叉登记（2026-11-06 上游同步波，chatbot a228359f→1c4a3258）

| 文件 | 补丁 | 同步波动作 |
|---|---|---|
| `server/tests/refactor_move_manifest_20261019.json`（W38 本地） | 2026-10-19 move-only 契约的 AST 体哈希 pin **随上游真相前移 14 项**（`_tts_consumer` · `_speak_text`(args +4) · `_mark_speaking_end` · `_asr_pipeline` · `_eot_watchdog` · `_on_user_turn_locked` · `_handle_user_turn` · `_coalesce_fragments` · `_request_interrupt` · `_pause_playback` · `_resume_playback` · `_generate_and_speak_cancellable` · `_speak_interjection` · `_speak_memory_remark`）。逐件核验「pin 原值 == 上游 base 体哈希 且 现值 == 上游 target 体哈希」；其余 132 项仍钉死（守卫检出力不变） | 本地修 + 上游 handoff（T1）：**上游 target 1c4a3258 自身即红**（其 worktree 实跑该测试 = 1 failed / 17 passed）。上游重钉后本行转 in-sync |
| `server/skills/vibeweaver/scripts/assert_artifacts.py` + `.sha256`（W38 本地） | 随包规范包 checker 由 `46af9c20` 前移到 canonical `3af8820d`（含 W25 run-attribution / group 25）；仓内 `tests/assert_artifacts.py`、本 pin、`server/tests/test_vibeweaver_update_20261031.py` 常量四处同值 | 本地修 + 上游 handoff（T2）：上游仓根副本已是 `3af8820d`，但随包副本/常量仍 `46af9c20` → 产品运行时（`vibeweaver_gate_service.py:221` `bundled_first=True`）执行旧 checker。上游统一后本行转 in-sync |
| `server/skills/vibeweaver/UPSTREAM.md`（W38 本地） | 「checker pin」原只记上游源仓锚点值（`5001f816…`），补记随包实际 pin（`3af8820d`）与分叉状态/消解条件 | 本地文档修（宿主说明文件，上游不维护该段） |

