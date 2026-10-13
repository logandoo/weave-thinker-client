# PLAN.md — 设备执行身份绑定修复（实施计划，零上下文可执行）

> 依据 docs/PLAN_device_workspace_identity_fix.md（方案 A+B fallback，用户已授权）· 2026-10-12 · Class: CODE · Lane: L

## Consistency Hub（写一次，处处引用）

| entity | canonical 值/类型 | source of truth |
|---|---|---|
| 名门期望公式 | `f"{_slugify(cloud_username)}-{cloud_user_id[:8]}"` | chatbot `remote_dispatch_router.py:145-155`（云端唯一事实源） |
| 新错误码 | `identity_unbound` | 本计划 T3；`server/API.md` 错误码表 |
| 配对记录新键 | `local_user_id: str` · `cloud_user_id: str` · `cloud_username: str` | `device_exec.json`（`device_pairing_service.store_path`） |
| SyncState 新列 | `cloud_user_id VARCHAR(64) NULL` | `server/app/db/database.py` SyncState + `migrations.STARTUP_MIGRATIONS` |
| `_resolve_identity` 签名 | `async def _resolve_identity(self) -> Tuple[Any, str, Optional[str]]` → `(owner, ws_root, expected_ctx)` | `device_executor_service.py:1022`（改形，第三元=名门期望值，None=无云端身份） |
| 身份源优先级 | 配对 `local_user_id` → sync 绑定（`cloud_username` 匹配）→ fail-closed | PLAN_device_workspace_identity_fix §2 裁决 |
| 云端身份取数 | `GET /api/auth/me`（Bearer → UserResponse.id/username） | chatbot `auth.py:174` |
| 名门缺省语义 | `workspace_ctx` 缺省（旧云端）→ 放行；有值 → 期望值 None 则 `identity_unbound`、不等则 `workspace_mismatch` | 本计划 T3 |

## T1 SyncState.cloud_user_id + 绑定取数

**Files**：Modify `server/app/db/database.py`（SyncState 加列）· `server/app/db/migrations.py`（一条启动迁移）· `server/app/api/sync.py`（PUT /account 核验后 GET /api/auth/me 落 cloud_user_id）；Test：`server/tests/test_sync_account_cloud_identity.py`（新建）。
**Interfaces**：Produces `SyncState.cloud_user_id: Optional[str]`；`put_sync_account` 语义=绑定行同时携带云端身份。
**Test seam**：`put_sync_account` 经 FastAPI TestClient + 假云端（login/status/me 三端点）；迁移经 `STARTUP_MIGRATIONS` 名单断言。
**Steps**：
1. RED：假云端含 `/api/auth/me` → PUT /account 后 `SyncState.cloud_user_id` 断言（先红）；迁移名单含 `sync_state_cloud_user_id` 断言（先红）。验证：`.localstack/venv/bin/python -m pytest server/tests/test_sync_account_cloud_identity.py -q` 红。
2. `database.py` SyncState 加 `cloud_user_id = Column(String(36), nullable=True)`；`migrations.py` 加 `("sync_state_cloud_user_id", "ALTER TABLE sync_state ADD COLUMN IF NOT EXISTS cloud_user_id VARCHAR(64)")`。
3. `put_sync_account` 在 sync 探针 200 后 `GET /api/auth/me` → `row.cloud_user_id = str(data.get("id") or "")`（容错：me 失败不阻断绑定，置空）。
4. GREEN：同 pytest 命令绿。

## T2 配对记录身份字段 + status.identity_bound

**Files**：Modify `server/app/services/device_pairing_service.py`（load_binding 兼容/文档）· `server/app/api/client_exec.py`（pair 落 `local_user_id`/`cloud_user_id`/`cloud_username`；status 加 `identity_bound`）；Test：`server/tests/test_client_exec_pairing.py`（扩展）。
**Interfaces**：Consumes `pair` 的 `current_user.id`（本地属主）与 pair 响应可选 `user_id`/`username`（上游未来字段）；Produces binding 记录三键 + `status.identity_bound: bool`。
**Test seam**：既有 `_client()` TestClient + `_FakeCloudHandler`（pair 响应可带/不带 user_id/username 两形）。
**Steps**：
1. RED：pair 后记录含 `local_user_id=="u1"` 断言（先红）；pair 响应带 `user_id/username` 时记录含 `cloud_user_id/cloud_username` 断言（先红）；status 含 `identity_bound` 断言（先红）。
2. 实现：`client_exec.pair` record 增三键（`local_user_id=str(current_user.id)`；`cloud_user_id/username` 取 `data.get("user_id"/"username")` 可选）；`status()` 增 `identity_bound=bool(record.local_user_id or (cloud_username and cloud_user_id))`。
3. GREEN：`pytest server/tests/test_client_exec_pairing.py -q` 绿。

## T3 _resolve_identity 重写 + 名门改云端身份期望值

**Files**：Modify `server/app/services/device_executor_service.py`；Test：`server/tests/test_device_executor_service.py`（扩展）。
**Interfaces**：Consumes T2 binding 键 + T1 `SyncState.cloud_user_id`；Produces `resolve_execution_identity(binding: dict, db) -> (owner, expected_ctx|None)` 模块函数 + `_resolve_identity()` 包装（含 ensure_user_workspace）；名门错误码 `identity_unbound`。
**Test seam**：`resolve_execution_identity` 直测（SQLite `_Bridge` 会话 + 用户/sync_state 行，仿 test_edit_resend_trim.py）；名门行为经 FakeCloud 回路（`_resolve_identity` 打桩返回 (owner, root, expected)）。
**Steps**：
1. RED 三腿：①`resolve_execution_identity` 优先配对 local_user_id（非首用户）②无 local_user_id 时按 cloud_username 匹配 sync 绑定 ③都无 → 抛 identity_unbound；④名门 expected=None + ws_ctx 有值 → `identity_unbound`（FakeCloud 回路）；⑤expected 与 ws_ctx 不等 → `workspace_mismatch`、相等 → 执行成功。
2. 实现：新模块函数 `resolve_execution_identity`（优先级链，Hub 语义）；`_resolve_identity` 改包装（load_binding → AsyncSessionLocal → resolve_execution_identity → ensure_user_workspace，返回第三元=expected）；`_do_tools_call` 名门三分支（缺省放行 / expected None→identity_unbound / 不等→workspace_mismatch）；`_reply_call`/error 面不回显路径（W16 教训 19）。
3. GREEN：`pytest server/tests/test_device_executor_service.py -q` 全绿（既有 23 腿不回归）。
4. `- test-change:` 登记：`test_workspace_ctx_mismatch_or_abs_path_rejected` 中 `/etc`·`../escape` 两输入仍走 `workspace_mismatch`（打桩 expected=root.name 时语义不变，断言不改形）——如断言改动在此登记。

## T4 契约/面板/登记

**Files**：Modify `server/API.md`（workspace_ctx 语义+`identity_unbound` 错误码）· `app/src/components/DeviceExecPanel.vue`（identity_bound=false 提示）· `upstream.toml` · `docs/DECISION_MATRIX_local_vs_upstream.md` §3。
**Steps**：
1. API.md「设备执行器」节：workspace_ctx 句改为「期望值=云端身份导出 `{slug(cloud_username)}-{cloud_user_id[:8]}`（两侧同源云端身份；跨栈不要求 uuid 相等）；设备无云端身份时 `identity_unbound` fail-closed」；错误码表补 `identity_unbound`。
2. DeviceExecPanel：`status.identity_bound === false` 显示「设备未绑定执行身份——请重新配对（或先绑定同步账号）」。
3. upstream.toml W16 行追加本波 local-mod 注记 + handoff 三项；DECISION_MATRIX §3 增行。
4. 验证：API.md read-back + `pytest server/tests -q` 回归 + app `test:unit`（若面板有单测）/构建 `script/linux/project_build.sh`。

## Plan self-review
Coverage：方案书 §3 八文件全覆盖（device_pairing/client_exec/device_executor/API.md/Panel/测试/登记=T1-T4）✓ · Placeholder scan：无 TBD ✓ · Type consistency：`Tuple[Any, str, Optional[str]]` 与 Hub 一致 ✓。
