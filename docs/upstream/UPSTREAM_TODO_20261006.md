# 上游（chatbot）侧待办 — W3 新发现（2026-10-06）

> **2026-10-06 W4 勾稽：项 1/2/3 已由上游修复完成**（736dddfcf：恢复锚任意角色+refresh 门 / backfill 本消息行+孤儿修复器 / 收窗保锚+终局复核；vendor 已直采验证）。下列新增 W4 发现项 4/5/6。

> 来源：W3 同步波（98f3d26e2→deceebaca）双对抗评审的 parked 项。
> **红线**：`/home/user/Documents/DEV/chatbot` 只读——本清单交付用户在 chatbot 会话执行；涉及部署面按 deploy_60 流程。
> 判定依据：docs/DECISION_MATRIX_local_vs_upstream.md；上期清单（2026-10-04，6/7 完成，项 4 维持双端裁定）见 UPSTREAM_TODO_20261004.md。

---

## 1. abort/重连恢复锚 caught-up 假阳性（A4.9 W3-B I1）— P2

- **现象**：`_recoveryAnchor` 只找最近一条**已持久化 user 消息**；当前轮 user 行不在本地列表时**回退到上一轮的 user 行**，`caughtUpAfterAnchor` 随即把上一轮 assistant 视为「最后已持久化尾部」→ 判 caught-up=true。此时 `syncAfterAbort` Phase-1 忽略 refresh 结果直接 `endStreaming` + `flushUnconfirmedInterjections('auto-send')`——活跃中的 run 被误判「已收线」，插话可能在答案未落库时被自动发送/重复气泡。
- **证据（评审实证）**：`frontend/src/stores/chat.ts:152-165, 896-911` · `frontend/src/stores/streamReducer.ts:269-279`；直接执行 `caughtUpAfterAnchor([{u1,user},{a1,assistant}], {id:'u1',need:1}) === true`。可达路径：watchdog/startup/tab-switch 任一带 `_recoveryAnchor` 的调用在 refresh 失败时，或 `reserve()`→`db.commit()` 窗口（`backend/app/api/chat.py:1095` vs `:1205`；`stream_status` 在 user 行落库前报 `setup_in_progress`，`:4831`）。
- **建议修法**：锚改为「任意角色的最新已持久化消息」（其缺失/`after=[]` → false），或 Phase-1 要求 refresh 成功方可信任 caught-up。
- **本地处置**：D-62 parked（上游新机制语义，本地改=发明分叉；上游对本类响应快）。vendor 已随波发布——用户侧影响=极端时序下可能重复气泡/插话误发，非数据损坏。

## 2. backfill_message_payloads.py sha 跨消息去重 → 路由 404 孤儿（A4.9 W3-B I2）— P2

- **现象**：backfill 脚本 `process_row` 命中相同 sha 时返回 `skipped_dup`，**调用方仍写 stub**（payload_ref 指向他消息的行）；而 W3 路由按 `(message_id, field)` 归属寻址 → 这些消息永久 `404 Payload not found`，前端「全文取回失败」+ 重试永不成功（route 测试只覆盖 forged/orphan 形态，`test_sha_dedup` 断言 stub 被写=现有行为）。
- **证据**：`backend/scripts/backfill_message_payloads.py:35-40,92-101` · `backend/app/api/message_payloads.py:94-98` · `backend/app/services/message_payload_service.py:260-281`。
- **建议修法**：backfill 跨消息去重时改为「复制行到本消息 (message_id, field)」或停止跨消息去重（每消息独立行，schema 唯一键 `uq_message_payloads_msg_field` 已支持）。
- **本地处置**：D-62 parked；vendor 脚本随上游，修复后下波归位。

## 3. fullWindow 收窗极端边界可致重复气泡残余（A4.9 W3-A m4）— P3

- **现象**：一次性 `fullWindow` 回退（`fullRefetchUsed`）+ 收窗可能把锚点消息逐出窗口；此后轮询无法再判 caught-up，Phase-2 3 连陈旧轮破可照旧追加 `local-abort-*` 局部气泡（答案已落库）——仅 ≥整页新行的退化时序。
- **证据**：`frontend/src/stores/chat.ts:860-872, 1000-1013`。
- **建议修法**：锚被逐出时置「不可判」态（保守不叠气泡）或收窗保锚（pin 锚点消息）。
- **本地处置**：D-62 parked（同 1 域，一并修）。

---

## 归属判定（DECISION_MATRIX 评估必答项）

```
[归属判定]
- 运行时: 云端+客户端共担（1/3=客户端同步机制设计，2=服务端脚本+路由契约）
- 文件类别: C 共享镜像（chat.ts/streamReducer=双端同源候选）· B 红条面（不涉 sync_apply/sync_capture 改动）
- 结论: 仅上游+同步波（chatbot 修复 → deploy_60（如触服务面）→ 下波 vendor 归位）
- 交付动作: chatbot 会话修 1/2/3 → 本仓下轮同步波直采/converged
- 验证面: 上游侧补回归（锚=任意角色最新消息的 caught-up 反例腿 · backfill 去重后 404 面 · 收窗保锚）
```


---

## 4. W4 迁移落 pgvector 边界之后（无 pgvector 库升级路径）— P2（评审 B Critical-1）
- **现象**：W4 的 `messages_delivery_status`/`idx_messages_delivery_status`（`backend/app/db/migrations.py:693-696`）位于 `pgvector_extension`（:451）之后；`run_startup_migrations`（:889）在 `PGVECTOR_AVAILABLE=False` 时跳过该后缀全部条目 → 无 pgvector 库（§9.5 降级模式）升级后 `delivery_status` 列缺失 → conversation/session_search/voice/sync 全部 `UndefinedColumn`。文件自身约定（:256/:397 注释）要求非 memory-v2 迁移置于 pgvector 标记之前；所引守卫测试 `tests/memory_md_round4_test.py` 不存在。
- **建议修法**：把这两条迁移移到 pgvector_extension 之前（与同区约定一致）或拆分跳过窗口；补在位守卫测试。
- **本地处置**：D-65 parked（vendor 桌面包带 pgvector 不受影响；deploy-60 面=上游修复）。vendor 侧不受影响（评审 B 也确认 fresh/embedded PG 无碍）。

## 5. boot sweep 绕过 sync capture（跨端不可见直至全量）— P2（评审 B-4）
- **现象**：`mark_interrupted_streams`（`backend/app/api/chat.py:755`，`main.py:174` 调用）以 Core `sa_update` 翻 streaming→interrupted；`sync_capture` 只监听 mapper 事件（`sync_capture.py:252-266`，文档自述 bulk DML=捕获缺口）→ 硬杀（部署/SIGKILL）后的 interrupted 残稿**无 delta 事件**，跨设备直至手动全量才可见；与 `sync_capture.py:171-175`（翻 terminal 后由当次提交发射）承诺不符。
- **建议修法**：翻转走 ORM 实例更新（触发 mapper 事件）或在 sweep 后显式补发捕获；red-line B 面需双端一致。
- **本地处置**：D-65 parked。

## 6. 次要项（登记）
- 迁移双索引（model `index=True` 建 `ix_messages_delivery_status` + 迁移建 `idx_messages_delivery_status`）→ 建议统一命名或去重
- `test_sync_capture_coerces_terminal_update_to_insert` 命名误（断言 create）
- drain 门未覆盖 resume_stream/voice/后台（上游语义披露；如意图扩展另立）

## 7. bulk_export_notes 未定义 `endnote_enabled`（全格式 500）— P2（评审 A Critical-1）
- **现象**：`POST /notes/notes/bulk-export` 引用未定义名（签名无此参；`key_parts` 无条件求值连带 md 路径）→ 端点必 500；W6 导出注册表对应的同步端点从未成功跑通。上游 2669eec5d 同缺陷；其 W7 工作树在修（export_note 的查询参数模式）。
- **本地处置**：**本地热修已落**（镜像 export_note：`endnote_enabled: bool = True` 查询参数；registry 在案）——上游修复落地后下波收敛。

## 8. 共享内容寻址导出文件可被跨任务删除（W6 引入）— P2（评审 B m3）
- **现象**：W6 起导出产物用共享 hash 命名；`export_worker.py` 取消/清理路径按行删除 `file_path`，可能删掉被其它任务/单飞复用命中的共享文件 → 下载 404 且无重渲染。W6 前按任务命名无此面。
- **建议**：引用计数或「仅删除独占产物」；清理前查注册表引用。

## 9. 前端徽标伪造面（上游未采纳的守卫）— P3（评审 B m4）
- 后端 M5 只覆盖桩路径整值覆盖；前端 `_itemChars` 信任非桩内容上的 `__size_bytes__`。上游工作树有未提交的 strip-marker 守卫（MessageBubble.vue:834-848 一带），建议提交并同步。

## 10. resume_stream drain 503：客户端无视 Retry-After — P3（评审 B m6）
- drain 门（chat.py）回 `Retry-After: 5`，客户端把 503 当硬错误红错提示；建议按 Retry-After 做瞬时重试/轻提示。

## 11. conversation.py mojibake（工��区分片）— P3（评审 A m6）
- `_ws_fp` docstring 含 U+FFFD 替换符（上游带入），建议修字。

## 12. 嵌套 dict 路径 __size_bytes__ 累加可伪造 — P3（评审 A m5）
- `message_payload_service.py` dict 路径 `out.get("__size_bytes__",0)+own["hidden"]` 累加内容自带同名键（与 A4.9 M5 整值覆盖契约相悖）；建议改整值覆盖或剔除源键。

> **2026-10-07 W6 勾稽：项 7-12 全部由上游修复完成**——项 7 bulk endnote（b92544fd0 同款热修，注释引用本仓清单）；项 8 导出共享删除面（export_registry「仅删独占产物」）；项 9 徽标守卫（stubMarkersTrusted 前后端镜像）；项 10 resume 503（fetchWithDrainRetry+Retry-After）；项 11 mojibake 清零；项 12 marker 整值+源键剥除。以下新增 W6 发现项 13-14。

## 13. CORSMiddleware 未 expose Retry-After（跨源读不到）— P3（W6 评审 B-m1）
- `server/main.py:56` CORSMiddleware 无 `expose_headers`——打包客户端跨源读 `Retry-After` 恒 null，回退 5000ms（drain 门本身发 5s，影响有界）。建议 `expose_headers=["Retry-After"]`。

## 14. healing-guard 单位漂移（UTF-16 vs 码点）— P3（W6 评审 B-m2）
- `streamReducer.ts:365-368` 以 Array.from(full).length（码点）对比 `__size_bytes__`（W6 起 UTF-16 单位）；文本体豁免使现值潜伏，emoji+legacy 路径可能多一次取回。建议对齐单位。

## 15. export_registry `_file_locks` 无回收 — P3（W6 评审 A-m6 补登记）
- `server/app/services/export_registry.py:33` 进程内 dict 只增不减；键=workspace/用户/文件量级，单机可忽略，长驻多租户进程建议 LRU/定时清理。

## 16. API.md 缺「List one directory inside the user's workspace」— P3（W6 评审 A-m8 补登记）
- `backend/API.md` 工作区目录列表端点说明缺失（pre-existing，生成器/手写缺口）；建议补文档条目。

## 17.（已闭环，保留核验记录）测试命名误 `..._coerces_terminal_update_to_insert` — 06 档项 6b
- **2026-10-07 核验：上游已更名** `test_sync_capture_coerces_terminal_update_to_create`（test_stream_persistence.py:382，随 W6 波采纳），旧名全仓零残留——本项关闭（原补登记误判，状态修订保留痕迹）。
