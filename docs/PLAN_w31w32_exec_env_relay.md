# W31 上游同步 + W32 执行环境云端联动（显示/菜单/中继）Implementation Plan

**Goal:** 客户端执行环境与云端真联动——显示（device:<self>=本地）、菜单三选一（本地/云端/其他绑定客户端）、免配对码（仅网页端）、云端中继执行（方案 A）。
**Architecture:** 执行目标语义全端统一（`null`=云端 · `device:<self>`=本地 · `device:<other>`=其他客户端）。客户端发起 turn 时：本地目标由本地栈执行（设备目标=自身时本地直执行短路）；云端/其他目标由本地后端经 ASGI 中继代理到云端 `/api/chat/stream*`（云端跑 agent，工具落云端 workspace/派发目标设备），SSE 原样回流；消息行经同步落本地。设备列表经本地后端代理云端执行器注册表。
**Tech Stack:** FastAPI/ASGI middleware + httpx streaming（服务端中继）· Vue3/Pinia（前端显示/菜单）· Playwright（UI 实证）· pytest（后端 TDD）。

## Global Constraints

- **上游仓只读红线**：`/home/user/Documents/DEV/chatbot` 零写入；同步域文件（`[[map]]`）波末归上游语义或登记 DECISION_MATRIX §3 本地 hunk。
- **执行目标取值域**：`device:<id>`（`^[A-Za-z0-9_-]{4,64}$`）或空（`normalize_execution_target`）——不新增取值形态。
- **set-once 不放松**：已有消息的会话禁止换执行环境（「执行环境已固定」）。
- **fail-closed 不放松**（D-8）：目标=设备永不静默落回本地执行；中继失败不假成功、不吞错误。
- 配置一律读 `config.toml`（`server/config.toml`）；测试经 `../.localstack/venv/bin/python -m pytest`。
- 生命周期一律 `script/linux/*.sh`（COV-2）；测试/构建命令见各任务块。

## Consistency Hub (broadcast — write once, read many)

| Entity | Canonical spelling / value / type | Source of truth |
|---|---|---|
| 目标语义映射 | `null`→云端 · `device:<self_id>`→本地 · `device:<other>`→其他客户端 | `server/API.md:1545`（null=恢复云端执行）+ 本计划用户裁定（判据 #94-#97） |
| 本机设备 id 来源 | `load_binding()["device_id"]`（server）· `GET /api/client-exec/status → device_id`（前端） | `server/app/api/client_exec.py:73-92` |
| 自执行短路判定 | `target == f"device:{own_device_id}"` → 本地执行（不走 WS 派发） | 本计划 Task 2（`remote_dispatch_router`） |
| 中继路由谓词 | `cloud_linked(user) and (target is None or (target is device:X and X != own and X not in local_registry))` | 本计划 Task 4（`cloud_relay_service.route`） |
| 云端会话存在性保障 | `ensure_cloud_conversation(conv_id)`：404→`sync_client.wake()`→轮询云端 `GET /api/conversations/{id}` ≤3×2s→重试一次 | 本计划 Task 4 |
| 云端凭据 | `SyncState.{server_url, cloud_username, cloud_password}`（本机同盒 D-13） | `server/app/db/database.py:1101-1112` |
| 云端注册表代理 | `GET /api/client-exec/cloud-devices` · `POST /api/client-exec/cloud-devices/{device_id}/revoke` | 本计划 Task 3（`client_exec.py` hunk） |
| 菜单选项绑定值 | 本地=`device:<self_id>` · 云端=`""`（null）· 其他=`device:<id>` | 本计划 Task 5（ChatInput `selectExecTarget`） |
| 显示映射 | `device:<self>`→「本地」· `device:<other>`→注册表名（回落「客户端」）· `null`→「云端」 | 判据 #94；Task 5 `execTargetLabel` |
| 无云端关联退化 | 菜单仅「本地（本机）」（绑定 null）+ 云端关联引导；W26 文案语义保留 | ADR D-121（本波记） |
| 中继后可见性 | 非本地目标 turn 终态后 `postSyncNow()`+`waitSyncComplete()` 再 `refreshConversation` | 本计划 Task 5（chat.ts） |
| test-change 登记 | `- test-change: <path> — <reason>` 行（assert group 15） | 判据 #100；W26 守卫更新 |

---

### Task 0: W31 上游同步（req 3，先行防基线漂移）

**Files:**
- Modify: `server/app/core/config.py` · `server/app/services/code_execution_service.py` · `server/app/services/process_session_service.py` · `server/app/tools/terminal.py` · `server/config_model.toml.example` · `upstream.toml` · `docs/upstream/upstream_state.json`
- Create: `server/app/services/sandbox_env.py` · `server/app/services/spawn_registry.py` · `server/tests/test_sandbox_env_f1.py` · `server/tests/test_spawn_registry_f2.py`
- Test: `server/tests/`（全量）+ `script/upstream_report.mjs` 复跑

**Interfaces:**
- Consumes: 上游 `chatbot@615548e39` 对应文件（只读）；`docs/upstream/upstream_state.json` 分类账。
- Produces: 同步域与上游目标一致（或登记 local-mod）；`upstream.toml base` = 波末同步目标。

**Steps:**
1. 逐件三方对照（`upstream_report.mjs` 分类为准）：`new-upstream` 4 件直采（sha 全等断言）；`ff-safe` 1 件直采；`conflict` 5 件逐 hunk 并集裁决（保留本仓既有本地修订，如 W28 D-118 的 config 模板修复）。
2. 验证：`cd server && ../.localstack/venv/bin/python -m pytest tests/ -q` = 0 failed（含新采 2 件测试可收集可跑）。
3. `upstream.toml` `base` 推进至 `615548e39`（或波末实证等价提交）；`node script/upstream_report.mjs --write` 重生成分类账；复跑对同步域零未决（in-sync/登记 local-mod）。
4. `docs/DECISION_MATRIX_local_vs_upstream.md` §3 复核（W28 教训：归属判定先查分类账）。

### Task 1: 本机设备身份 + 本地直执行短路（server）

**Files:**
- Modify: `server/app/services/remote_dispatch_router.py`（hunk：自目标短路）
- Test: `server/tests/test_exec_env_self_target.py`（新建）

**Interfaces:**
- Consumes: `app.api.client_exec.load_binding() -> dict | None`（含 `device_id`）。
- Produces: `resolve_local_self_target(target: str | None) -> bool`（target==device:<own> 时 True）；`try_remote_dispatch` 对自目标返回 None（本地执行，工具落本机 workspace）。

**Steps (TDD §A4.8):**
1. RED：`test_self_target_executes_locally` — 绑定 device_id="dev-self"、conversation.execution_target="device:dev-self" → `registry.dispatch("terminal", ...)` 走本地执行（不发 WS、结果为本地产物）；对照腿 `device:dev-other` 仍 fail-closed。跑 → 看 FAIL。
2. GREEN：`try_remote_dispatch` 入口加自目标短路（`load_binding()` 快照比对；无绑定=无自目标）。跑 → PASS + 既有 `test_device_exec_seam`/`test_device_exec_routing` 全绿。
3. 边界：绑定为空/绑定 device_id 为空/目标非法 → 不短路（fail-closed 原样）。

### Task 2: 云端链路服务（server，凭据/令牌/请求）

**Files:**
- Create: `server/app/services/cloud_link_service.py`
- Test: `server/tests/test_cloud_link_service.py`（新建）

**Interfaces:**
- Consumes: `SyncState(server_url, cloud_username, cloud_password)`（`app.db.database`）。
- Produces: `class CloudLinkService`：
  - `async def request(method: str, path: str, *, json: Any = None, params: dict | None = None, timeout: float = 30.0, stream: bool = False) -> httpx.Response`（自动登录/401 重登一次；`path` 以 `/api/` 开头）
  - `async def is_linked(user_id: str) -> bool`
  - `async def ensure_conversation(conv_id: str, user_id: str) -> bool`（404→wake 同步→轮询 ≤3×2s→再试；返回云端是否可达该会话）

**Steps (TDD):**
1. RED：假云端（httpx MockTransport / stub app）三腿——①登录取 token 后带 Bearer 请求；②401 重登一次重试；③`ensure_conversation` 对 404 先 wake 再轮询成功。跑 → FAIL。
2. GREEN：实现（token 内存缓存 per user；密码不出本模块；异常分类=LinkUnavailable/LinkAuthError）。跑 → PASS。
3. 单测全绿 + 无真实网络（MockTransport 证明）。

### Task 3: 云端注册表代理端点（server）

**Files:**
- Modify: `server/app/api/client_exec.py`（hunk：+2 端点）· `server/API.md`（契约文档，§A4.7 oracle）
- Test: `server/tests/test_cloud_devices_proxy.py`（新建）

**Interfaces:**
- Consumes: `CloudLinkService.request`。
- Produces:
  - `GET /api/client-exec/cloud-devices` → `[{device_id, name, platform, online, revoked, last_seen_at}]`（云端 `GET /api/devices` 透传裁剪）；无链接 → `{"devices": [], "linked": false}` 形状以 API.md 定稿。
  - `POST /api/client-exec/cloud-devices/{device_id}/revoke` → 云端 `POST /api/devices/{id}/revoke` 透传（2xx 空体归一 `{revoked: true}`）。

**Steps (A4.7):** API.md 增补两端点（方法/路径/响应/错误码）→ doc↔code 一致性审计一遍 → 用例从文档写（happy + 401 链路失效 + 云端 5xx 透传）→ 假云端跑绿。首跑 RED（端点不存在）在案。

### Task 4: 云端中继中间件（server，方案 A 核心）

**Files:**
- Create: `server/app/services/cloud_relay_service.py`
- Modify: `server/main.py`（hunk：注册 ASGI 中继中间件，一行挂载）
- Test: `server/tests/test_cloud_relay_service.py`（新建）

**Interfaces:**
- Consumes: `CloudLinkService.request/stream` · `load_binding()["device_id"]` · `client_device_service.list_devices`（本地注册表）。
- Produces: `async def relay_asgi(scope, receive, send) -> bool`（True=已代理完成；False=放行本地）＋路由谓词 `should_relay(user_id: str, conversation_target: str | None) -> bool`（Consistency Hub 行）。
- 拦截面：`POST /api/chat/stream` · `POST /api/chat/stream/resume` · `GET /api/chat/stream/status/{id}` · `POST /api/chat/stream/stop/{id}` · `POST /api/chat/stream/interject/{id}` · `GET /api/devices` · `POST /api/devices/{id}/revoke` · `GET /api/devices/{id}/fs/*`（后两族=设备无本地会话且有云端链接时回退云端）。

**Steps (TDD + A4.7b):**
1. RED-1（路由谓词）：四象限用例——(null, linked)=relay · (device:self)=local · (device:other, 无本地会话)=relay · (null, 无链接)=local。跑 → FAIL。
2. GREEN-1：谓词实现。PASS。
3. RED-2（代理行为）：假云端 SSE（`data: {...}\n\n` 帧 + `[DONE]`）→ 中继响应逐帧等价透传（含状态码/Content-Type/断流传播）；404 会话 → `ensure_conversation` 后重试一次；云端不可达 → 502 detail 明确（fail-closed，不落本地）。跑 → FAIL。
4. GREEN-2：ASGI 中继实现（body 缓冲重放；SSE 双向泵；请求头剥离 host/length，注入云端 Bearer）。PASS。
5. A4.7b 工作流（real-HTTP，`tests/workflows/exec_env_relay.trace.log`）：起本地栈（script/linux）+ 假云端 → ①绑定云端目标→发送→SSE 回流→消息行经同步落本地可见；②本地目标→本地执行断言；③云端不可达→错误提示断言。三腿状态迁移全断言。
6. `server/main.py` 挂载 hunk + DECISION_MATRIX §3 登记行。

### Task 5: 前端显示 + 菜单三选一 + 免配对码 + 中继后可见性（req 1/2 核心）

**Files:**
- Modify: `app/src/api/base.ts`（+`hasCloudLink` 供给位）· `app/src/api/clientExec.ts`（+`getCloudDevices`）· `app/src/api/clientDevices.ts`（self 判定导出）· `app/src/components/ChatInput.vue`（显示/菜单/配对码门控）· `app/src/stores/chat.ts`（默认目标 + 中继后同步等待）· `app/src/types/index.ts`（ClientDevice 补 `is_self?`）
- Test: `app/tests/workflows/test_exec_env_menu_w32.ts`（test:unit 链）+ `tests/ui_exec_env_relay.py`（Playwright 实证）

**Interfaces:**
- Consumes: `GET /api/client-exec/status`（self id）· Task 3 云端注册表 · `saveUiPreferences({default_execution_target})`。
- Produces: `execTargetLabel` 映射（Consistency Hub）；菜单 `selectExecTarget` 绑定值映射（Hub）；`needsPostTurnSync(target)`（非本地目标 → turn 后 postSyncNow+waitSyncComplete）。

**Steps:**
1. RED（判别守卫）：test:unit 断言 ①`device:<self>` 标签=「本地」②null=「云端」③设备名回落链 ④`EXECUTION_MODE=local` 时模板无「生成配对码」区块源码分支（配对码渲染门控=cloud-only）⑤菜单「本地」点击绑定值=`device:<self>`、「云端」=null。跑 → FAIL。
2. GREEN：ChatInput 显示/菜单/门控实现 + chat.ts 中继后同步等待。PASS。
3. **test-change 登记（判据 #100）**：`test_exec_mode_wiring`/`tests/ui_exec_mode.py` 中「空目标=本地（本机）」断言改为「null=云端、device:<self>=本地」新语义；`- test-change:` 行入 log；更新后判别有效（对旧实现报红=回归验证一次）。
4. Playwright `tests/ui_exec_env_relay.py`：注入 `weaveRuntime.execMode='local'` + 假 API（self id + 云端注册表）→ 截图菜单三选项 + 按钮「执行：本地」态 + 无配对码区块；model-native 判读（§A4.1.1 逐判据 verdict）。
5. 默认目标：客户端新会话默认「本地」（`device:<self>`，无 self 身份回落 null）；偏好恢复沿用 `default_execution_target`。

### Task 6: 文件访问面联动（判据 #99）

**Files:**
- Modify: `app/src/components/MessageBubble.vue`（self 目标→本地文件 API）· `app/src/utils/deviceUrls.ts`（`deviceIdFromTarget` 兼容 self 判定）· `app/src/api/workspaceFiles.ts`（self 不走设备代理）
- Test: `app/tests/workflows/test_device_url_apiwrap.ts` 扩展 + `server/tests/test_cloud_relay_service.py` fs 回退腿

**Steps:**
1. RED：`device:<self>` 会话文件引用 URL 不含 `/api/devices/`（走本地 workspace 下载）；`device:<other>` 保持设备代理路径。跑 → FAIL。
2. GREEN：MessageBubble/workspaceFiles self 判定（比对 self id）。PASS。
3. server fs 回退腿（Task 4 拦截面）：本地无设备会话 + 有云端链接 → 代理云端 `/api/devices/{id}/fs/*`；无链接 → 404 原样（fail-closed）。

### Task 7: 收口（证据/评审/FCV/记忆）

**Steps:**
1. 全量：`server/tests` 0 failed + `app` test:unit all pass + `script/linux/project_build.sh` BUILD OK（独占树）。
2. A4.9 对抗评审 ×2（Lane L §V4）：`tests/review_package.md`（git diff da66ee8..HEAD 全量）→ 两评审代理 → 修 Critical/Important → scoped re-review。
3. FCV（§V1）：`tests/fcv_brief.md` → 独立代理复跑验收命令 → `tests/fcv_report.md`。
4. 覆盖矩阵 `tests/coverage_matrix.md`（判据 #91-#101 逐行）。
5. 记忆（A7.9）+ Memory Gate（A7.10）+ 波末 upstream.toml/DECISION_MATRIX 复核 + `assert_artifacts.py --final`。

## Plan self-review

1. **Coverage**：判据 #91-93→Task 0；#94/#100→Task 5；#95→Task 5；#96→Task 1+4+5；#97→Task 5（绑定值）+ 既有 set-once；#98→Task 3+5；#99→Task 6；#101→Task 4（fail-closed）+5（提示）。全覆盖。
2. **Placeholder scan**：无 TBD/占位；每步含具体断言或命令。
3. **Type consistency**：`CloudLinkService.request` · `should_relay` · `ensure_conversation` · `resolve_local_self_target` 全文一致（Hub 单一真源）。
