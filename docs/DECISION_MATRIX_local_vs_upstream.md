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
| `app/src/components/SystemSettingsDialog.vue` | 加 `device-exec` tab（W16；上游共享镜像文件的本地 hunk：tabs 数组+图标分支+内容分支+import 4 处） | 保留本地 hunk（冲突区手工回植） |
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

## 4. 实例校准（已发生案例）

| 案例 | 判定 | 依据 |
|---|---|---|
| 事故② 全量同步永挂（worker 不启动） | **仅本地** | 行为在本地 sidecar；main.py/config 属 client-only（A） |
| 事故① 仲裁拒绝×24（reorder 同值赋值旧 ts） | **仅本地**（修发射侧） | 云端仲裁规则正确；本地事件发射缺 bump（C 类文件但修复语义仅本地生效；上游后由其自行同化——已发生） |
| 云端 memory 路径 text() 写漏捕获（chatbot 会话修复） | **仅上游+部署** | 服务端捕获缺口，Web 用户行为（B/C 类上游侧） |
| ASR qwen3.1 迁移 + 部署 60 | **上游+部署，且 client vendor 跟随**（C 类） | 云端新功能 + vendor 镜像漂移由同步波收敛 |
| sync_apply LWW 规则调整 | **双端**（红线 B） | 仲裁双端逐字节一致是契约 |
