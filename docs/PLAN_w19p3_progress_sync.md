# PLAN — W19 补波 3：progress 发报对齐 + 预算随续期同调（2026-10-11）

> 规格源：`chatbot/docs/DOWNSTREAM_TODO_20261011_device_progress.md`（下游任务单）。
> 事实源：chatbot 已部署代码只读抽取（教训 #11）——`device_session_service.py`
> `_renew_progress_deadline`/`rpc_call`/`_call_max_extension_seconds`、`remote_dispatch_router.py:379`。

## Consistency Hub

| entity | canonical 值 | source of truth |
|---|---|---|
| progress 通知形状 | `{"jsonrpc","method":"progress","params":{"call_id","note"}}`（无 id；W16 既有面） | 任务单 §一·发报规范 + 上游 `handle_message`（method=="progress" → `_renew_progress_deadline`） |
| call_id 匹配 | `str(params.call_id)` 对 `pending_waits` 键（=请求 id 原文；上游 `rpc_call` 用 `uuid4().hex`） | 上游 `_renew_progress_deadline:217-233`（`str(params.get("call_id") or "")`）|
| 续期语义 | `deadline = now + window`，硬顶 `min(deadline, started + call_max_extension_seconds)`；未注册/未知 id 一律忽略 | 上游 `:228-233` + `rpc_call:147-152` |
| 间隔公式 | `min(30s, 窗/3)`，下限 1s（≤1 次/秒）；首 tick ≤ 窗/2 | 任务单 §一.2 |
| note 规范 | 单行 ≤60 无控制字符（发报前消毒；上游亦消毒 `[\x00-\x1f\x7f]→" "`+[:60]） | 任务单 §一.3 + 上游 `:236` |
| 窗口取值 | fs/\* = `[client_exec] call_timeout_seconds`（云端同键默认 120）；tools/call = `max(请求 timeout_seconds, 120)`（本仓 `_effective_timeout` 同源） | 任务单 §一背景 + 上游 `remote_dispatch_router:379`（tools/call 传 timeout → 注册 pending_waits）|
| 生产预算 | `[client_exec] call_max_extension_seconds`（缺省 **3600**，0=不限制，非法/负/NaN 回落 3600） | 云端 `config.py:1526-1543` 同名属性（镜像；键名一致便于两侧同调）|
| zip 打包预算 | 同「生产预算」（0=无限 wait） | 本设计；否则补波 2 的 120s 自裁与验收 #1（3 万文件打包）冲突 |
| office 转换超时（RPC 路径） | 同「生产预算」（0 时回退 `office_preview_timeout_seconds` 兜底——soffice 必须有限超时杀进程组） | 本设计；**本地 HTTP `/api/files/office-*` 仍用 `office_preview_timeout_seconds` 不动** |
| 覆盖面 | 必发：fs/zip·fs/office-\*；建议：fs/download·tools/call；不发：fs/list·fs/read（完成早于首 tick 自然不发） | 任务单 §一.4 |
| 停止条件 | 终态（result/error/超时/取消）即停发，无「结束 progress」消息 | 任务单 §一.6 |

## Task 1 — 发报规范（device_executor_service.py）
**Files**：Modify `server/app/services/device_executor_service.py` · Test `server/tests/test_device_executor_service.py`。
**Interfaces**：Produces `_progress_interval(window: float) -> float`（Hub 间隔公式）·
`_sanitize_note(note) -> str` · `_progress_ticker(ws, call_id, note_fn, window)`（note_fn: () -> str 动态进度）。
Consumes：既有 `_send`/`_swallow_late`。
**Steps**：
1. RED：间隔公式腿（120→30 / 30→10 / 9→3 / 0.5→1）· note 消毒腿（换行/控制字符/超长）· download 覆盖腿 · tools/call 覆盖腿（现无 ticker→红）。
2. GREEN：`PROGRESS_INTERVAL_SECONDS` 常量退役→`_progress_interval`；ticker 改 `note_fn`+消毒；`_handle_fs` download 分支与 `_do_tools_call` 包 ticker；zip note=产物大小、office/tools=已耗时。

## Task 2 — 预算随续期同调（config 4 点 + 两处消费）
**Files**：Modify `server/app/core/config.py`（`client_exec_call_max_extension_seconds` 属性，镜像云端语义）·
`server/config.toml` · `server/config.toml.example` · Test `server/tests/test_config_client_exec.py`（SECTION_KEYS+钉）·
`server/app/services/device_executor_service.py`（zip 预算 + office RPC 超时改读新键）· `server/API.md`。
**Steps**：
1. RED：config 钉（缺省 3600/0=不限制/非法回落 3600）先红 · zip 预算取值腿（timeout 消息带 3600 尺度）。
2. GREEN：属性+两 toml+SECTION_KEYS；`_send_zip_dir` budget/`_send_office_converted` timeout 改读新键（0 语义按 Hub）；API.md 注记。

## Task 3 — 核对项（任务单 §二）
- 同步协议：引文波仅 `messages.tool_results` 列内 JSON 字段 → 同步端零改动 ✓（不触碰）。
- vendored 前端：本仓 `app/src/utils/citations.ts`·`MessageBubble.vue`·`types/index.ts` 存在 → **归下次同步/发版波拉取**（任务单明文「下次同步」），本波不动。
- WS 协议：无新方法/字段 ✓（progress 为 W16 既有面）。

## 联调项（⏳ 留待真云端）
3 万文件 zip 云端 renewal 日志+无 504 · 不发 progress 负例 120s 504 · 长 PDF >120s · 硬上限→tools/cancel 终止本地任务。

## 自评
覆盖：任务单一.1-.6 = Task 1；预算冲突（否则验收不可达）= Task 2；§二 = Task 3 核对。占位符扫描：无。类型一致：`_progress_interval`/`_sanitize_note`/`_progress_ticker` 签名在 Interfaces 与 Steps 同名同参。
