# PLAN — Device Executor P0 实现分解（本仓，零上下文执行者可读）

> 依据：docs/PLAN_device_executor.md（本仓需求）+ chatbot:docs/PLAN_device_execution_mode.md §5/§7/§8/§9（协议/安全/分期，上游只读）。
> 云端侧（chatbot 仓）由并行会话开发；本计划只含客户端（weave-thinker-client）改动。P1/P2 显式不做（托盘/排队/browser_*/job_*/E2EE）。

## Consistency Hub（写一次，处处引用；改名先改本表再 grep 旧拼写至零命中）

| entity | canonical 值/类型 | source of truth |
|---|---|---|
| WS 端点 | `/api/device/ws`（出站 WSS，`Authorization: Bearer <device token>` 头，token 不进 URL/日志） | 主方案 §4/§5 |
| 信封 | JSON-RPC 2.0 风格：请求 `{id, method, params}`；响应 `{id, result\|error}`；通知 `{method, params}`（无 id） | 主方案 §5（id/method/params/result/error 五键） |
| 云→设备 method | `initialize` · `tools/call` · `tools/cancel` · `fs/list` · `fs/read` · `fs/download` | 主方案 §5 |
| 设备→云 message | `result` · `error` · `chunk` · `heartbeat` · `progress`（可选） | 主方案 §5 |
| result 形状 | `{"call_id": str, "ok": bool, "payload": str}` | 主方案 §5 |
| error 形状 | `{"call_id": str\|null, "code": str, "message": str}` | 主方案 §5 |
| tools/call params | `{call_id: str, tool: str, args: dict, timeout_seconds: int, workspace_ctx: str\|null, script: {filename, content}\|null}` | 主方案 §5 + 本仓方案 §1.1 |
| chunk 形状 | `{transfer_id: str, seq: int, data: str(base64), sha256: str(本块字节摘要)}`；**末块追加** `final: true, total_sha256: str(整体摘要)` | 主方案 §5（「末块带整体摘要」）+ D-81 |
| workspace 逻辑名 | `{slug}-{uid8}`（`ensure_user_workspace` 结果 root_path 的目录名）；`workspace_ctx` 仅做该逻辑名严格对齐，绝不接受绝对路径 | 本仓方案 §1.1/§2.2 |
| 路径契约 | `fs/*` 的 `path` 一律 workspace 相对路径；`workspace_paths.is_within` 包含性校验 fail-closed | 本仓方案 §2.2 |
| config 段 | `[client_exec]`：`enabled` `cloud_url` `heartbeat_seconds` `call_timeout_seconds` `max_payload_mb` `allowed_tools` | 主方案 §6.5（M-1 键拼写统一） |
| allowed_tools 缺省 | `["workspace_read","workspace_write","workspace_edit","workspace_glob","grep","diff","word_count","terminal","process","git","execute_code","skill_run_script","provide_file","provide_folder"]` | 主方案 §6.5/§7 |
| token 落盘 | `$LOCALSTACK_HOME/app/device_exec.json`（0600）：`{cloud_url, device_id, name, platform, token, tls_verify, created_at}` | 本仓方案 §1.2 + D-82 |
| 本地面板 REST | `/api/client-exec/status` · `POST /api/client-exec/pair` · `POST /api/client-exec/reset`（用户 JWT，同 sync.py 面） | D-83 |
| 执行上下文 | `app.tools.execution_context`：`remote_call_scope()` · `is_remote_call()` · `super_admin_bypass_effective()` · `permission_sentinel_effective(args)` | 本仓方案 §2.1（contextvar 方案，Design Gate A） |
| 控制键剥离 | 入站 args 丢弃一切 `_` 前缀键（`_permission_granted` 等） | agent_loop.py:10848 `_strip_internal_tool_args` 先例（A4.9 C-1） |
| dispatch kwargs | `db` `user` `workspace_path` `permission_context`（必非空）`allowed_tools` | agent_loop.py:11125-11150 现场 |
| 超时 | `effective_timeout = max(config.client_exec_call_timeout_seconds, params.timeout_seconds or 0)`，为 dispatch 等待上限 | 本仓方案 §1.1 |
| 断线语义 | at-most-once：断线即取消在途 task（云端标记 failed），不重放、不补发 | 主方案 §5/§9 |
| 心跳 | 设备→云 `heartbeat` 通知，间隔 `heartbeat_seconds`；对云端带 id 的 `heartbeat` 请求回 pong result | 主方案 §5/§9 + D-84 |

## Task 1 — 执行上下文 + 四面安全收紧（§2.1）

**Files**：CREATE `server/app/tools/execution_context.py`；MODIFY `server/app/tools/terminal.py`（:130/:211/:213/:220/:228/:286）、`server/app/tools/registry.py`（:206-208）、`server/app/tools/git.py`（:152）、`server/app/tools/process_tool.py`（:41）、`server/app/services/code_execution_service.py`（:118/:197/:261）；TEST `server/tests/test_execution_context.py`。

**Interfaces**（Produces）：`remote_call_scope() -> ContextManager`；`is_remote_call() -> bool`；`super_admin_bypass_effective() -> bool`（remote_call 下恒 False）；`permission_sentinel_effective(args: dict) -> bool`（remote_call 下恒 False）。

**Test seam**：`server/tests/test_execution_context.py`（in-process pytest；四面逃逸断言 + 本地路径不变断言）。

**Steps**：
1. RED：写 `test_execution_context.py` 四面逃逸用例（super_admin_bypass=True + `_permission_granted=True` 注入，terminal/process/git/code_execution 各一腿，断言被拒）——此时必 FAIL（现行为放行）。跑：`../.localstack/venv/bin/python -m pytest tests/test_execution_context.py -x -q`。
2. 建 `execution_context.py`（contextvar + 4 helper），先令 helper 测试绿。
3. 逐面替换检查点：`config.super_admin_bypass` → `super_admin_bypass_effective()`；`args.get("_permission_granted")` → `permission_sentinel_effective(args)`（registry 权限闸 + terminal 2 处 + git + process_tool）；`check_code_safety`/cwd 校验/preexec 同理。每面替换后跑该面用例。
4. 本地路径不变腿：default（无 remote_call）下 `super_admin_bypass=True` 仍放行（回归现有语义）+ server 全量 0 failed。
5. 四面全绿 → commit。

## Task 2 — device executor worker（§1.1 + §2.2 + skill 内联腿）

**Files**：CREATE `server/app/services/device_executor_service.py`；TEST `server/tests/test_device_executor_service.py`（内含假云端 WS 夹具）。

**Interfaces**（Consumes）：Task 1 `remote_call_scope()`；`app.tools.registry.registry.dispatch`；`app.services.workspace_service.ensure_user_workspace`；`app.services.workspace_files_service.list_directory`；`app.services.workspace_paths.is_within`/`WorkspacePathError`；`app.tools.skill_tools._exec_skill_script`/`_INTERP_BY_EXT`；config `client_exec_*` 属性（Task 4 可先行 stub 属性于本文件读 dict）；`app.services.agent_permissions.PermissionContext`。
（Produces）：`DeviceExecutorWorker`：`start()` `stop()` `status() -> dict` `wake()`；模块单例 `device_executor`。

**Test seam**：`tests/test_device_executor_service.py` —— 假云端 WS 服务（真实 `websockets.serve` 于临时端口，非 mock）驱动 worker：initialize/tools/call(terminal·execute_code·skill 内联)/tools/cancel/fs/*（含逃逸）/白名单/超时/chunk/heartbeat/断线重连。真实 HTTP 轨迹写 `tests/workflows/device_executor.trace.log`。

**Steps**：
1. RED：initialize+tools/call terminal 回路用例（假云端发 `tools/call {tool:"terminal", args:{command:"echo hi"}}`，断言设备侧 result 回传含 `hi`）——FAIL（服务不存在）。
2. 实现 worker 骨架：token/URL 解析 → `websockets.connect(additional_headers={"Authorization": ...})` → recv 分发循环 → `_run` 重连指数退避 + heartbeat task。
3. `tools/call`：白名单镜像（fail-closed）→ 剥 `_` 键 → workspace 解析+`workspace_ctx` 逻辑名对齐 → `remote_call_scope()` 内 `registry.dispatch(..., permission_context=strict)`，`asyncio.wait_for(max(本地默认, 请求值))` → `result`/`error` 回传。
4. `script?` 内联腿：物化 `{workspace}/skill_scripts/{filename}` → `_exec_skill_script`。
5. `tools/cancel`：in-flight 表 `call_id→task`；cancel → `task.cancel()` + process 会话 kill + execute_code/terminal 子进程终止（服务内补 `except asyncio.CancelledError: proc.kill()` 防漏，属 §2.1/§1.1 语义）。
6. `fs/list|read|download`：相对路径 + `is_within` 校验；download 分块 `chunk`（base64 256KiB/块，末块 `final+total_sha256`）。
7. 断线语义：断连取消在途；重连心跳恢复用例。
8. 全绿 → commit。

## Task 3 — 配对/凭据 + 本地面板 API（§1.2）

**Files**：CREATE `server/app/services/device_pairing_service.py`、`server/app/api/client_exec.py`；MODIFY `server/main.py`（router 注册 + `device_executor.start()/stop()` 位点，仿 sync_client）；TEST `server/tests/test_client_exec_pairing.py`、`server/tests/test_main_device_executor_wiring.py`。

**Interfaces**（Consumes）：Task 2 `device_executor.wake()`/`status()`；httpx（仓库已有）。
（Produces）：`device_pairing_store`：`load() -> dict|None`、`save(record)`（0600）、`reset()`；`register_with_cloud(cloud_url, pairing_code, name, platform, tls_verify) -> dict`（POST `/api/devices`）；REST 面见 Hub 行。

**Test seam**：pytest + 假云端 HTTP（uvicorn 临时端口收 `POST /api/devices`）+ FastAPI TestClient（真实路由/中间件）；错码/过期码/0600 落盘/reset 断言。

**Steps**：
1. RED：pair→token 落盘 0600 用例（假云端回 token）——FAIL。
2. 实现 store（LOCALSTACK_HOME 解析同 local-stack.cjs：WEAVE_LOCALSTACK_HOME→LOCALSTACK_HOME→ROOT/.localstack）+ `register_with_cloud`。
3. `client_exec.py` 三端点（`get_current_user` 同 sync.py）；main.py 接线 + 启停（`[client_exec] enabled` 门控）。
4. `test_main_device_executor_wiring.py` 防合并删行守卫（源码钉 + 路由钉，仿 test_main_sync_wiring）。
5. 全绿 → commit。

## Task 4 — [client_exec] 配置（§4）

**Files**：MODIFY `server/config.toml`、`server/config.toml.example`、`server/app/core/config.py`（property 对，仿 sync_client_*, config.py:1450）、`localstack/gen_config.py`（模板同步=example 已含段则无需 set_kv；核对 `_MERGE_SECTIONS` 语义不动）。

**Interfaces**（Produces）：`config.client_exec_enabled/client_exec_cloud_url/client_exec_heartbeat_seconds/client_exec_call_timeout_seconds/client_exec_max_payload_mb/client_exec_allowed_tools`。

**Test seam**：`server/tests/test_config_client_exec.py`（属性读取/缺省/allowed_tools 缺省镜像主方案 §6.5）。

**Steps**：example+toml 加段 → config.py property 对 → 属性测试 → gen_config 冒烟（`python3 localstack/gen_config.py --force` 到临时 LOCALSTACK_HOME，断言 `[client_exec]` 在产物中）→ commit。

## Task 5 — UI 面板「设备执行」（§1.2）

**Files**：CREATE `app/src/components/DeviceExecPanel.vue`、`app/src/api/clientExec.ts`；MODIFY `app/src/components/SystemSettingsDialog.vue`（tabs 加 `device-exec`，仿 `sync` 行）。

**Interfaces**（Consumes）：Task 3 REST；`app/src/api/client`（axios 实例，同 sync.ts）。

**Test seam**：Playwright 脚本 `tests/ui_device_exec_panel.py`（仿 tests/ui_*.py 现有范式；截图 `tests/device_exec_panel_final.png`）+ `app` test:unit 链无涉（纯新增组件）。

**Steps**：api 模块 → 面板（状态卡/配对表单/重置/云端地址）→ 挂 tab → `bash script/linux/project_build.sh` 构建 → Playwright 打开 SystemSettingsDialog 设备执行 tab 截图 + DOM 断言（data-testid）→ commit。

## Task 6 — 文档与登记

**Files**：MODIFY `server/API.md`（本地 REST 面 + WS 协议摘要）、`docs/DECISION_MATRIX_local_vs_upstream.md` §3（local-mod 登记：device_executor_service/execution_context/client_exec.py/面板/守卫测试）、`upstream.toml` 注记。

**Steps**：API.md 端点文档（A4.7 Step 2/3 doc↔code 一致性审计一次）→ §3 登记 → commit。

## 验证缝映射（本仓方案 §7 → 用例）

| §7 缝 | 用例所在 |
|---|---|
| executor 回路冒烟（tools/call terminal 真实执行+result 回传；内联 skill 腿） | Task 2 step 1/4 |
| 配对流程（换 token→连接→reset→断连拒调用；错码/过期码拒绝） | Task 3 + Task 2 状态腿 |
| 安全腿（四面逃逸被拒 + 白名单镜像拒未知工具） | Task 1 step 1 + Task 2 step 3 |
| 断线语义（在途 failed at-most-once、重连心跳、cancel 杀进程） | Task 2 step 5/7 |

## 计划自审

- Coverage：§1.1→Task 2 · §1.2→Task 3/5 · §2.1→Task 1 · §2.2→Task 2 step 6 · §4→Task 4 · §7→映射表全覆盖。
- Placeholder scan：无 TBD/无「类似 Task N」；每步有验证命令。
- Type consistency：Hub 统一 result/error/chunk 形状与 helper 命名；后续 Task 引用不重推导。

---

# W16b 线格式对齐波（上游波落地后双端修约，2026-10-10）

事实源：chatbot 仓已落地代码（只读抽取）——`backend/app/services/device_session_service.py`
（rpc_call/信封/chunk 缓冲）、`remote_dispatch_router.py`（tools/call params/结果解析）、
`api/client_devices.py`（pair 端点/fs 代理契约）、`frontend/src/api/clientDevices.ts`。
上游=部署现实，本侧改说上游方言（D-81/D-83/D-85 revisit-if 命中）。

## Consistency Hub（W16b 修订行——覆盖 W16 同名行；改名先改此表）

| entity | canonical（上游现实） | source of truth |
|---|---|---|
| 信封 | 全部出站消息带 `"jsonrpc": "2.0"`；请求 `{jsonrpc,id,method,params}` 且 **id≡call_id**（params 不含 call_id） | device_session_service.py:132 |
| tools/call 结果 | ok 路 `{"ok": true, "content": "<工具结果串>"}`；败路 `{"ok": false, "error": "<msg>"}`（协议级败仍走 `{id,error:{message}}` 信封） | remote_dispatch_router.py:265-272 |
| tools/cancel | 云端 `{jsonrpc,id(新),method:"tools/cancel",params:{call_id:<原请求 id>}}`（发后不候应答）；设备应答可选 | device_session_service.py:146 |
| skill 内联 | `params.script` = 脚本**纯内容字符串**；`params.script_ext` = 扩展名（".py"）；args 继续在 `params.args` | remote_dispatch_router.py:250-252 |
| fs/download | result `{ok:true, data_b64}`（单包）或 `{ok:true, transfer_id, sha256?}`（分块）；chunk `{transfer_id, seq, data_b64, sha256(本块), last:true(末块)}` | client_devices.py:288-345 |
| fs/list·read | 透传字典（list 读 `entries`；read 读 `content`） | client_devices.py:258,276 |
| 配对 | `POST /api/devices/pair` body `{code, name?, platform?, device_id?}` → `{device_id, token, name?}` | client_devices.py:46-56,112 |
| inflight 键 | JSON-RPC `id`（=云端 call_id）；cancel 按 `params.call_id` 查同一键 | device_session_service.py:127,146 |

## Task A — executor 线格式（device_executor_service.py）
**Steps**：① inflight/去重/_reply_call 以 `msg["id"]` 为键（cancel 参数对齐）② `_reply_result_call`
改为 `{ok,content}` / `{ok:false,error}` ③ script/script_ext 字符串形态（interpreter 由 script_ext
映射）④ chunk `data_b64`+`last`；download result `{ok,transfer_id|data_b64,sha256}` ⑤ 全出站消息带
`jsonrpc:"2.0"` ⑥ 跑 tests/test_device_executor_service.py（oracle 随契约改）。

## Task B — 配对线（device_pairing_service.py + client_exec.py）
**Steps**：① register_with_cloud → POST `/api/devices/pair` body `{code,...}`，读 `{device_id,token,name}`
② 面板入参 pairing_code→wire code 映射 ③ 跑 tests/test_client_exec_pairing.py（假云端端点/字段随契约改）。

## Task C — 契约文档与 ADR
**Steps**：① API.md「WS 协议契约」节重写为上表（doc↔code 一致性复审一次）② tests/decisions.md
D-81/D-83/D-85 追加 revise 行（原地修订指向上表）③ 本表 Hub 生效。

## 验证缝映射
| 面 | 用例 |
|---|---|
| result{ok,content} / error 信封 | tools/call 腿断言改形状 + 新增契约钉测试 |
| chunk data_b64/last + result sha256 | download 腿改断言 + 空文件单包腿 |
| pair /api/devices/pair + code | pairing 腿改假云端 |
| cancel 按 id | cancel 腿（inflight 键改后回归）|

## 计划自审
Coverage：交接 7 点全映射（握手✓已符/信封=Task A①⑤/结果形状=A②/fs=A④/五防线=已符回归）·
Placeholder 扫描：无 TBD · Type 一致性：Hub 修订行覆盖 W16 行（同名实体以 W16b 为准）。
