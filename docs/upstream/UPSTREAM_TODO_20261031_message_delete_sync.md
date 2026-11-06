# UPSTREAM_TODO — 2026-10-31 消息删除同步缺口（客户端残留空回答）

> 交付对象：上游 `chatbot` 仓（`backend/`）。本仓 `weave-thinker-client` 对上游只读，
> 不代改；本文件只登记上游需要做的修改与审核结论。
>
> 事故会话：`d7ea65b8-9241-4112-bd5d-afe87338b400`（三款AI编程工具离线可用性对比）
> 现场：云端 2 条消息（Q2/A2），桌面客户端本地 3 条（A1/Q2/A2）；
> A1 = `f697cf2d-91ee-4072-b821-a94a9aad1986`，`content=""`、`reasoning=419`、
> `delivery_status=final`、`created_at=2026-10-30 14:34:48Z`。

## 0. 结论（先看这里）

| 编号 | 修改 | 级别 | 归属 |
|---|---|---|---|
| P0 | `_delete_message_slice` 删除消息时补发 `messages` delete 事件 + tombstone | **必须** | 上游 + 部署 60 |
| P1 | 所有 final 落库面：不把「0 正文 + 仅 reasoning」落成 `final` 助手消息 | **必须** | 上游 + 部署 60 |
| P2 | （可选）前端对「仅 reasoning、无正文」消息显示占位/折叠，不显示空气泡 | 建议 | 上游 frontend |
| P3 | 一次性 backfill：用 `sync_events` 里残留的 create 反查已不存在的消息，补 delete+tombstone | **必须（存量）** | 上游脚本 + 部署 60 |
| L1 | 客户端本地孤儿清理（无 tombstone 的历史行） | 必须但**非上游** | 本地客户端 |
| L2 | 客户端 delta delete 清待推队列 + 服务端拒绝 tombstoned id 的 create（防复活） | 建议 | 本地 + 上游 |

P0 是根因：编辑重发 / 重新生成走 `_delete_message_slice` 的 Core `DELETE`，
绕过 SQLAlchemy mapper 事件，`sync_capture` 收不到 `after_delete`，因此没有
`sync_events` delete、也没有 `sync_tombstones`。客户端只收到过 A1 的 `create`，
永远收不到 delete；resync 又只做 upsert + 墓碑，不做快照差集删除，所以 A1 永久残留。

P1 是「空回答」本体的来源：stop 分支只要 reasoning 非空就落 final，即使正文为 0；
legacy resume 路径还有第二个同样的落库面（见 §3）。

P3 是存量修复：服务端可以从 `sync_events` 的 `messages/create` 反查
`messages` 中已不存在的 id，补发 delete + tombstone，让**所有**客户端（不只是本地）
在 delta/resync 后收敛；这比只清本地库更彻底。能否覆盖取决于 create 事件是否仍在
保留期内。

## 1. 上游代码位置（当前 HEAD）

- 删除切片：`backend/app/api/chat_persistence.py:176-230`
  - Core 删除：`:195` `await db.execute(sa_delete(Message).where(Message.id.in_(...)))`
  - 调用方：`backend/app/api/chat.py:903`（regenerate）、`:932`（edit）
- 同步捕获：`backend/app/services/sync_capture.py:324-401`（`_emit`）
  - 监听注册：`:404-446`，只注册 `after_insert/after_update/after_delete`
  - delete 分支写 `SyncEvent` + `SyncTombstone`：`:369-401`
- 同步应用：`backend/app/services/sync_apply.py:216,273-291,307-330`
  - messages 追加型：create 幂等、update SKIPPED、只有显式 delete 才删
- stop-cancel：`backend/app/api/chat_stream_transport.py:1097-1226`
  - 落库门槛：`:1150` `if _stop_content.strip() or _stop_tool_results or _stop_reasoning:`
  - 空行清道夫：`:1201-1226`（`_delete_empty_streaming_row`，只删空 streaming 行）
- legacy resume 落库面：`backend/app/api/chat.py:1411-1435`
  - 守卫：`if not content.strip() and not reasoning.strip() and not tr_json: skip`
  - 也就是说 reasoning-only 仍会落 `status="final"`
- 全量 resync：`backend/app/api/sync.py:735-796` 返回墓碑 + 快照；
  客户端侧 `server/app/services/sync_client.py:337-443` 只做 upsert，不做差集删除。

## 2. P0 — `_delete_message_slice` 补 delete 事件 + 墓碑

### 2.1 目标契约

编辑重发删除的**每一条**消息（旧 user query + 旧 assistant + 其后所有消息）都必须产生：

```text
sync_events      : entity_type=messages, entity_id=<msg id>, op=delete, payload=NULL
sync_tombstones  : (user_id, entity_type=messages, entity_id=<msg id>)
```

**edit 路径**：删除 + 事件 + 新 user 消息插入在**同一个事务**提交；新消息插入失败
→ 删除和事件一起回滚，旧消息不丢（现有 `test_trim_edit_defers_commit_atomic_replace`
契约不得破坏）。

**regenerate 路径**：删除事件随调用方的 sort_order 提交点先落库，替代回答由
agent 循环稍后生成；生成失败时旧回答不恢复（现有 D-q6 语义，本次不改）。
不要把「删除 + 新消息同事务」错误套到 regenerate 上。

### 2.2 方案 A（推荐：最小改动，直接复用 mapper 事件）

把 Core 批量 DELETE 改成「按 id 重新取 ORM 实例 → 逐行 `db.delete`」。
`slice_rows` 可能来自 `resolve_message_fields` 的 `SimpleNamespace` 拷贝，
不能直接 `db.delete(拷贝)`（2026-10-04 conv 4e159a79 的 500 根因）；
**按 id 重新查询 ORM 行**即可同时避开 UnmappedInstanceError 与丢失事件：

```python
async def _delete_message_slice(db, slice_rows, user_id=None):
    ids = list(dict.fromkeys(
        str(row.id) for row in slice_rows if getattr(row, "id", None)
    ))
    if not ids:
        return
    _cid = next(
        (getattr(row, "conversation_id", None) for row in slice_rows
         if getattr(row, "conversation_id", None)),
        None,
    )
    # 重新取 ORM 实例：slice_rows 可能是 SimpleNamespace 拷贝。
    # 带 conversation_id 过滤，防脏 slice 跨会话误删。
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        stmt = select(Message).where(Message.id.in_(chunk))
        if _cid:
            stmt = stmt.where(Message.conversation_id == _cid)
        rows = (await db.execute(stmt)).scalars().all()
        for row in rows:
            await db.delete(row)
    # 显式 flush：保证 delete 事件 seq 早于随后插入的新 user 消息 create。
    await db.flush()
    # 下面保持原样：invalidate_continuity + SubconsciousLog purge
```

优点：
- 复用现有 `after_delete → _emit`，事件/tombstone 形状自动与全站一致；
- 不新增同步协议，不改表结构；
- 与 `conversation.py:399-403`（会话批量删除已显式 ORM 逐行）的先例一致；
- 现有 `test_edit_resend_trim.py` 的 7 个测试经独立复跑可通过（含 payload cascade
  与 rollback 用例）。

代价/注意：
- 1000 条消息的切片 ≈ 每条 1 次 ORM DELETE + 1 次 `Conversation` SELECT
  （`_emit` 解析 user_id）+ 1 次 `sync_events` INSERT + 1 次 tombstone upsert，
  约 3–4 条语句/消息；需在长会话上压测。
- **`await db.flush()` 是必须的**：否则 UOW 可能把新 user 消息 INSERT 排在
  pending DELETE 之前，create 的 seq 低于 delete；最终状态仍对，但对端会短暂
  看到「新消息 + 旧消息」并存。
- `db.flush()` 不会提交；edit 路径仍由调用方统一 commit，原子性不变。
- 现有 `test_continuity_invalidation_p1.py` 的 fake DB（`tests/test_continuity_invalidation_p1.py:35-43`）
  只实现 `execute()` 且返回 `None`，Option A 会调用 `.scalars()` 和 `db.delete`，
  **该测试必须同步升级 fake DB**（返回可 `.scalars()` 的结果、提供 `delete`/`flush`）。
- 新事件断言需要 `SyncEvent`/`SyncTombstone` 表、SQLite 的 JSONB/BigInteger 编译
  与 `sync_enabled` 设置；可参考 `tests/test_sync_w9_followups.py:26-33,70-101`。

### 2.3 方案 B（大批量优化：批量写事件/墓碑）

保留 Core 批量 DELETE，但在删除前显式批量写事件与墓碑。建议在
`sync_capture` 增加可复用的批量 helper，`_emit` 的单条 delete 分支与它共用同一段
tombstone upsert 逻辑：

```python
# sync_capture.py
async def emit_delete_batch(db, *, entity_type, user_id, ids):
    # 1) config.sync_enabled 门控；2) ids 去重；3) 500/批
    # 4) insert SyncEvent(op="delete", payload=None, origin_device=_origin_device.get())
    # 5) pg_insert(SyncTombstone).values([...]).on_conflict_do_update(...)
```

关键点（也是最容易写错的地方）：
- **先读后删**：批量 helper 必须在 Core DELETE 之前读取
  `Message.delivery_status`/`conversation_id`；删完再查就拿不到行，streaming 守卫失效。
- **去重**：同一批里重复 id 会让 PostgreSQL 的
  `ON CONFLICT DO UPDATE command cannot affect row a second time` 直接报错；
  `ids = list(dict.fromkeys(ids))` 必须做。
- **不要另开 session**：必须复用调用方的 `db`/connection，否则删除、事件、新消息
  不在同一事务，回滚语义被破坏。
- **origin_device**：沿用 `_origin_device.get()`，不要硬编码；但要注意
  `ChatRequest` 没有 device id（`schemas/chat.py:10-31`），web/客户端直接
  `/chat/stream` 编辑时它本来就是 NULL——这是预期，不是 bug。回显抑制发生在
  `/api/sync/push` 重新发射时（`sync.py:532`）。
- **user_id 解析**：`_delete_message_slice` 已有 `user_id` 参数，优先直接用；
  为空时从 `Conversation.user_id` 解析一次，不要逐行 SELECT。
- **分批 500**：对齐现有 `_delete_message_slice` 的 IN 分块，避免 PG 绑定参数上限。
- **payload**：messages 的 delete payload 恒 NULL（tombstone payload 也 NULL）。

方案 B 需要扩展 `test_edit_resend_trim.py` 的 SQLite fixture（创建
`sync_events`/`sync_tombstones` 表或 mock helper），否则新事件断言会在 INSERT 处失败。

### 2.4 两个方案都必须保持的不变量

1. **edit**：删除 + 事件 + 新 user 消息同一事务；失败回滚后旧消息仍在。
2. regenerate：删除事件随调用方提交点先落库；旧回答不恢复（D-q6）。
3. `slice_rows` 含外置 payload stub（SimpleNamespace）时不得抛
   `UnmappedInstanceError`。
4. 空 streaming 行（`delivery_status='streaming'`）不发 delete 事件/墓碑。
5. `invalidate_continuity` 与 `SubconsciousLog` 按 `source_ids` purge 必须保留。
6. 多租户：事件 user_id 必须来自当前会话 owner，不能从请求体/id 推断。
7. 重复删除幂等：同一 id 的事件重复无妨；tombstone 必须 upsert 不报错。
8. delete 事件 seq 早于新消息 create（Option A 用显式 `flush()`；Option B 先写事件）。
9. `[sync].enabled=false` 时删除照常、不写事件；这是已知的“同步关闭窗口”分歧
   （见 §6.3），需要有接受或补偿策略。

### 2.5 P0 测试清单（上游）

- [ ] `test_edit_resend_emits_delete_events_for_slice`：编辑第一条消息，断言切片内每个
      id 都有 `messages/delete` 事件 + tombstone，且新 user 消息 create 也在同一事务。
- [ ] `test_regenerate_emits_delete_event_for_old_assistant`。
- [ ] `test_delete_events_atomic_with_new_user_message`：删除后模拟新消息插入失败并
      rollback，断言旧消息与事件都未落库。
- [ ] `test_delete_events_order_before_new_user_create`：`seq(delete) < seq(new create)`。
- [ ] `test_delete_message_slice_deletes_only_slice`：切片的 conversation_id 过滤生效，
      不误删别的会话同 id（防御性）。
- [ ] `test_delete_event_user_id_is_owner`：事件 user_id=会话 owner，不是请求体。
- [ ] `test_externalized_payload_stub_slice_no_unmapped_error`（现有测试的加强版）。
- [ ] `test_large_slice_chunks_500`：1200 条消息切片，无参数上限错误，事件数=1200，
      tombstone 数=1200。
- [ ] `test_delete_event_not_emitted_for_streaming_row`。
- [ ] `test_duplicate_ids_tombstone_upsert_no_error`（方案 B 必测）。
- [ ] `test_delete_event_origin_device_null_for_chat_edit`：不做 device 注入时
      origin=NULL；如需回显抑制，另立任务把 sync device id 传进 `/chat/stream`。
- [ ] `test_conversation_delete_still_emits_message_deletes`（防未来给
      `Conversation.messages` 加 `passive_deletes=True` 回归）。
- [ ] `test_sync_disabled_no_events`（`[sync].enabled=false` 时删除照常、事件不写）。
- [ ] 回归：`test_continuity_invalidation_p1.py` 的 fake DB 升级后仍绿。

## 3. P1 — 所有 final 落库面：不落「0 正文 + 仅 reasoning」

### 3.1 现状与证据

**面 1：stop-cancel**（`chat_stream_transport.py:1150`）

```python
if _stop_content.strip() or _stop_tool_results or _stop_reasoning:
    # 落 _save_assistant_terminal(..., status="final")
else:
    # _delete_empty_streaming_row(...)
```

本次事故：`content=0 reasoning=419`，走 save 分支，落库后 `delivery_status=final`，
产生一条只有思考内容、没有正文的助手消息。生产日志：

```text
22:34:50 Stop-cancel ... content=0 reasoning=419 segments=0 tools=0 state_content=0 dm=False
22:34:51 Persisted partial reply after stop ... (0 chars)
```

**面 2：legacy resume `_completed`**（`chat.py:1411`）

```python
if not (_cap_content or "").strip() and not (_cap_reasoning or "").strip() and not _cap_tr_json:
    skip
else:
    _save_assistant_terminal(..., status="final")
```

reasoning-only 同样会落 final。只改 `chat_stream_transport.py` 会留下第二个空回答面。

**面 3：主流程 / SSE-cancel**：主流程门槛是
`if content.strip() or (tr_json and not _dm_skip_empty)`（`chat_stream_transport.py:1463`），
reasoning 不触发落库；SSE-cancel/self-save 也有空 payload 守卫。这两个面目前是对的，
P1 只需防止未来回退。

### 3.2 建议修改（最小）

1. stop-cancel：从落库门槛去掉 `or _stop_reasoning`，只保留正文/工具结果：
   ```python
   if _stop_content.strip() or _stop_tool_results:
       save(...)
   else:
       delete empty streaming row
   ```
2. legacy resume：把守卫改成「无正文且无工具结果就跳过」，reasoning 不参与：
   ```python
   if not (_cap_content or "").strip() and not _cap_tr_json:
       skip
   ```
3. **必须在 sanitize 之后重新判定**：stop 路径的 `strip_stop_citation_echo` /
   `_localize_media_for_persist` 在门槛之后执行，可能把正文清空；如果只改了门槛，
   “引用回声-only + reasoning” 仍会落 final。建议在 `_localize_media_for_persist`
   之后、`_save_assistant_terminal` 之前再判一次：
   ```python
   if not _stop_content.strip() and not _stop_tool_results:
       # 走空行清理，不落 final
   ```
4. **whitespace-only**：`_delete_empty_streaming_row` 只匹配 `content == ""` / NULL
   （`chat_persistence.py:104-110`）；如果 streaming 行的 content 是 `"  "` / `"\n"`，
   删除会失败，启动 sweep 又因为 `if row.content:` 把它翻成 `interrupted`，同样出现
   空气泡。建议把清理守卫生效为 `func.trim(Message.content) == ""`
   （SQLite/PG 均可用），并同步把 `mark_interrupted_streams` 的判据改成
   `(row.content or "").strip()`。
5. **`_save_assistant_terminal` 绑定当前 run**（建议同步做，防重复/错行）：
   该函数按“最新 streaming 行”反查（`chat_persistence.py:31-37`），不是按
   `run.state._stream_buf.streaming_message_id`；加一个可选 `row_id` 参数，stop 路径
   传入本 run 的 id。另在 `_handle_agent_loop_cancel` 开头检查
   `run.state.response_saved`，已保存则跳过，避免迟到 stop 插入重复 final。

### 3.3 语义选择（需要产品确认）

- **推荐**：删除 reasoning-only 行（与 `mark_interrupted_streams` 对空 content 的
  “直接删除”一致；与主流程/SSE-cancel 的空 payload 守卫一致）。reasoning 仍可在
  日志/流式阶段被用户看到，但不作为聊天消息持久化。
- **替代**：落 `status="interrupted"` + 前端「已停止（仅思考内容）」占位。
  注意 `_load_conversation_messages` 会把 `interrupted` 行重新注入下一轮模型上下文
  （`chat.py:833-835`）；若选这条，必须加 context 过滤，否则模型会看到空 assistant 轮。

### 3.4 P1 测试清单（上游）

- [ ] `test_stop_cancel_reasoning_only_deletes_streaming_row`：content=""、reasoning>0
      → 行被删除，不产生 final 消息，不发 create 事件。
- [ ] `test_stop_cancel_content_with_reasoning_persists`（负控）：content 非空时仍保存，
      reasoning 一并落库。
- [ ] `test_stop_cancel_tool_results_only_persists`：无正文但有工具结果时仍保存。
- [ ] `test_stop_cancel_whitespace_only_content_deletes_row`。
- [ ] `test_stop_cancel_citation_echo_only_after_sanitize_deletes_row`。
- [ ] `test_legacy_resume_reasoning_only_skips_save`。
- [ ] `test_stop_cancel_after_response_saved_no_duplicate`。
- [ ] `test_stop_cancel_dm_active_does_not_persist_empty_final`（死磕 stop 分支）。
- [ ] `test_passive_disconnect_reasoning_only_deletes_row`。
- [ ] 源码钉：`_handle_agent_loop_cancel` 的 save 分支不再包含 `or _stop_reasoning`
      （或替代方案的前端占位契约）。

## 4. P2 — 前端兜底（可选）

如果 P1 选择「interrupted + 占位」而不是删除，上游 frontend 需要：

- `filterBlankPersistedMessages` 目前只过滤「content/reasoning/tool 全空」
  （`frontend/src/stores/chat.ts:102-114`）。仅 reasoning 的消息不会被过滤。
- 建议：`content` 为空且无 tool 时显示「已停止，无正文」占位，或默认折叠 reasoning，
  不要显示空气泡。
- 如果 P1 直接删除，则新事故不再产生此类行；P2 只对存量数据有意义。
- **实时路径**：P1 删除 streaming 行后，stop/done 事件必须让前端移除已渲染的
  reasoning-only 瞬时气泡；load-time `filterBlankPersistedMessages` 覆盖不到实时流。

## 5. 存量数据修复

### 5.1 P3 — 服务端一次性 backfill（推荐，覆盖所有客户端）

服务端不必“无法回溯”。`sync_events` 里保留了 `messages/create` 的 id 和
`conversation_id`，可以反查 `messages` 中已不存在的 id，补 delete + tombstone：

```sql
-- dry-run 口径（示意）
SELECT DISTINCT e.entity_id, e.payload->>'conversation_id' AS cid
FROM sync_events e
JOIN conversations c ON c.id = (e.payload->>'conversation_id')
LEFT JOIN messages m ON m.id = e.entity_id
LEFT JOIN sync_tombstones t
  ON t.entity_type = 'messages' AND t.entity_id = e.entity_id
WHERE e.entity_type = 'messages' AND e.op = 'create'
  AND m.id IS NULL AND t.id IS NULL;
```

执行要点：
- 只处理**当前仍存在**的 conversation（`JOIN conversations`），避免给已删会话刷墓碑。
- 只处理 `messages` 中不存在的 id（`m.id IS NULL`）；已存在的跳过。
- 已有 tombstone 的跳过（幂等）；重复跑不产生重复墓碑。
- **本会话可修复的不只 A1**：同一会话更早的 Q0（create seq 19701）、A0（19703）
  和 A1（19742）都满足「create 事件在、messages 行不在、无 tombstone」；
  P3 应把这三条都补成 delete+tombstone。客户端本地即使没有 Q0/A0，也会因 delete
  幂等而安全跳过。
- 分批 500，与事件/墓碑插入同事务；origin 用 `NULL`（服务端下发，不是设备回显）。
- **必须在 sync_events 保留期内跑**：`sync_retention` 会按龄删事件；create 事件被裁掉后
  就无法反查该 id，只能退回 L1 本地清理。
- 先 dry-run 出计数与样本，再 `--execute`；上线前在 60 备份库演练。
- **半完成态**：如果上一次执行只写了 tombstone、没写 delete event（或 event 被保留策略
  裁掉），当前查询会因 `t.id IS NULL` 跳过。建议另加一个受限的“墓碑已存在但无 delete
  event”检查（只针对仍有 create event 的 id，且 `--since` 限定），按需补 event；
  否则在线客户端可能只靠 resync 才能删。
- **性能**：`sync_events` 只有 `(user_id, seq)` 索引；按 `entity_type/op/entity_id`
  扫描大表可能慢。按 user_id 分批、必要时建临时索引，不要一次性全表 join。
- 执行前在低峰期，并在每个批次内重新检查 `messages` 是否仍不存在（防执行期间新消息
  恰好使用同 id 的极小概率；UUID 复用概率可忽略，但脚本仍应 fail-safe）。
- 这是一次性脚本（`backend/scripts/backfill_message_tombstones.py`），不要挂启动钩子。

### 5.2 L1 — 客户端本地清理（兜底）

- 最小：删除本地 `f697cf2d-91ee-4072-b821-a94a9aad1986`。
- 通用：本地加载会话时，丢弃「assistant、content 为空、无 tool_results/tool_calls、
  且前一条不是 user」的消息；或提供「以云端快照强制替换本会话消息」的一次性工具
  （仅当该会话没有本地待推事件时）。
- 不建议只靠 `isBlankPersistedMessage` 放宽过滤：它有 reasoning 就不算 blank，
  放宽会把「用户确实想看的思考内容」也隐藏掉。

### 5.3 L2 — 防复活（客户端 + 服务端）

- 客户端 `_apply_remote` 收到 messages delete 后，应像 `_resync` 一样调用
  `_clear_queued_events`（`server/app/services/sync_client.py:305,385`），清掉该 id
  的待推 create/update；否则“push 响应丢失 → B 删除 → A 重试 push create”可能把
  已删消息重新插回云端。
- 服务端 `sync_apply` 对 `messages` 的 create 可检查 `SyncTombstone`：若该 id 已有
  墓碑则 SKIPPED（message id 是 UUID，不复用；删除后同 id create 视为复活）。
- 服务端 `sync_apply` 对 `messages` 的 **delete 且 row 不存在** 时，不应只返回
  SKIPPED（`sync_apply.py:285-286`）；应显式写一条 delete 事件 + tombstone（origin=
  推送设备）——这正是“旧 bug 已删云端行、无墓碑”场景下，另一台设备推 delete 时
  唯一能把删除事实扩散给第三台设备的路径。为避免伪造随机 id 刷墓碑，写入前可检查
  该 id 在 `sync_events` 中是否有过 create（或已有 tombstone）；无证据则仍 SKIPPED。
- E2E：A 有 queued create/update，B 删除，A 拉 delete 后再 push，断言不复活；
  再加“云端已无该行 + 另一设备推 delete”场景，断言 tombstone 生成且第三设备收敛。

## 6. 审核：遗漏与盲区

### 6.1 已确认「不需要改」的同类路径（防止误改）

| 路径 | 证据 | 结论 |
|---|---|---|
| 单会话删除 | `conversation.py:1050` + `Conversation.messages` cascade（`database.py:243`） | ORM cascade 逐条触发 message delete，OK |
| 会话批量删除 | `conversation.py:399-403` 显式 `select(Message)` + `db.delete(msg)` | 已经是正确范式，可作为 P0 参照 |
| 分组删除 | `conversation.py:790-793,851-854` 显式逐行删 messages/conversations | OK |
| 笔记本删除 | `notes.py:2702` + `Notebook.notes` cascade（`database.py:700`） | ORM cascade notes，OK |
| 技能删除 | `skills.py:407` + `UserSkill.files` cascade（`database.py:672`） | ORM cascade skill_files，OK |
| 助手删除 | `assistant.py:141` + `Assistant.conversations/conversation_groups` cascade（`database.py:140-141`） | ORM cascade 会话/分组/消息，OK |
| 记忆概念/事件删除 | `memory.py:102,138,152` 的 `db.delete(...)` | ORM，OK |
| stop 空 streaming 行 | `chat_persistence.py:91-111`、`chat_stream_transport.py:1583/3175/3299` 附近的 `sa_delete(Message)` | 只删 `delivery_status='streaming'`，从未发过 create，OK |
| 启动 sweep | `chat_persistence.py:60-89` | ORM + streaming 守卫，OK（whitespace 判据见 §3.2） |
| 客户端应用 delete | `sync_apply.py:273-291` `db.delete(row)` | ORM 删除，会写本地 rx 事件（push 侧过滤），OK |
| delta 压缩 | `sync.py:399,416-453` | create+delete 同窗丢弃；update+delete 保留 delete，OK |
| 事件裁剪 | `sync_retention.py:18-20,88-137` | 只裁 events，墓碑不裁；delta 有 `resync_required`，OK |
| 回显抑制 | `sync.py:532` push 设 origin；delta `is_distinct_from(device_id)` | OK |

### 6.2 相邻缺口（同一类，但不要混进 P0/P1 的补丁）

- **F-A `DELETE /api/memory/all` 的技能擦除**：
  `memory_forget_service.erase_extended_surfaces`（`:305-312`）用
  `DELETE FROM skill_files` / `DELETE FROM user_skills`，而这两个表都在
  `SYNC_ENTITIES` 里。结果：一台设备「清空记忆」后，其它设备仍保留技能/技能文件。
  建议改成 ORM 逐行删除（与 `memory.py` 的 concepts/episodes 同款），或显式批量发 delete。
- **F-B `POST /api/memory/forget` 的按词遗忘**：
  `memory_forget_service.execute_forget`（`:230-245`）用 text() 更新/删除
  `memory_concepts` / `memory_episodes`（两者在 `MEMORY_SYNC_ENTITIES`），同样零事件。
  需要单独评估「遗忘」是否应跨设备镜像；如果应镜像，改成 ORM 或显式事件。
- **F-C 记忆后台批处理**：`sync_capture.py` 顶部已自认
  「memory 域后台维护批处理仍走 Core-SQL/text() 写同步列（~30 点）」——
  已知缺口，本工单不扩大处理。
- **F-D 迁移/维护脚本**：`migrations.py:373,387` 用 raw SQL 去重
  `user_model_providers`；`memory_migration_service.py:646,650` raw 删除
  `memory_concepts`/`memory_episodes`。这些是维护路径，零事件；若删除/合并需要跨设备
  收敛，需另立 ticket。
- **F-E 客户端应用 delete 不清理本地派生数据**：本地同步客户端
  `server/app/services/sync_client.py:_apply_remote` 调用 `apply_change` 删除消息后，
  只删 `messages` 行；不会像服务端 `_delete_message_slice` 那样 purge
  `SubconsciousLog` / 失效 `conversation_rolling_summaries` / `resume_snapshots` /
  检索缓存。被删消息的正文仍可能留在本机记忆与摘要里，后续检索会重放。
  建议在 messages delete 应用后调用同源 purge/失效 helper（按 message id 精确匹配，
  注意本地 user id 与云端 user id 不同，但 message id 相同）。
- **F-F 会话删除与编辑删除的清理面不对称**：`DELETE /api/conversations/{id}`
  与批量删除走 ORM cascade 删 messages，但没有调用 `_delete_message_slice` 的
  `SubconsciousLog` purge / `invalidate_continuity`（`conversation.py` 内无这两个调用）。
  这是既有的隐私/上下文一致性缺口，和本工单的同步事件不是同一问题，但建议同波评估。
- 这六项与消息删除是同一类风险，但修复面/产品语义不同，不应塞进 P0 的
  `_delete_message_slice` 补丁里，否则评审面失控。

### 6.3 协议 / 运维盲区

- **墓碑增长**：每次编辑早期消息都会给整个尾部生成 tombstone；墓碑永不裁剪
  （`sync_retention` 的 D-w4 裁决）。长会话反复编辑会让 `sync_tombstones` 与
  resync 响应持续膨胀。**实测（2026-10-31，账号 alice）**：60 的 resync 响应
  约 144 MB，2571 messages / 131 tombstones；P0 之后每次编辑都会放大这块。
  需要监控；未来可考虑「按会话的消息区间截断事件」替代逐消息墓碑。
- **resync 体积/时延**：墓碑 + 快照全量下发，客户端 `_resync` 串行 apply；
  大量新墓碑会放大首连/重同步耗时。P0 上线后观察 resync 大小与耗时。
- **长事务**：一次编辑删除 1000+ 条消息 + 1000+ 事件/墓碑，事务持有时间变长；
  需要压测锁等待与 WAL 增长。方案 B 的批量 INSERT 比方案 A 的逐行 ORM 更稳。
- **事件页大小**：一次编辑可能产生上千条 delete 事件；delta 单页 200/1000，
  客户端需多轮 `has_more`。现有分页契约支持，但要在 E2E 中覆盖。
- **同步关闭窗口**：`[sync].enabled=false` 期间删除不写事件；重新开启后 resync
  不做差集删除，消息会永久分叉。需要明确“接受”还是增加 repair/对账路径。
- **防复活竞态**：见 §5.3；push 重试 + 对端删除可能把消息插回云端。
- **delta 压缩 create+delete 的假设**：`_compress_window` 对同一 key 的
  `("create","delete")` 直接丢弃（`sync.py:399,429-431`），前提是「客户端状态=事件流」。
  如果客户端通过会话打开/云直连拿到过该消息、而全局 cursor 还没推进到该 create，
  同一窗口里的 create+delete 会被整体丢掉，delete 永远不到达，客户端保留该消息。
  建议：messages 的 delete 不参与 create+delete 丢弃（对没拿到 create 的客户端，
  多收一条 delete 是幂等无害的），或在会话打开时先对齐 cursor/tombstone。
- **离线编辑的切片边界**：`_delete_message_slice` 只删除本地快照中目标位置之后的
  消息；如果编辑发生在离线期间、云端又新增了消息，这些云端新消息不在切片里，
  P0 不会删除它们（它们随后会同步回来）。这是编辑冲突策略问题，不是本 bug；
  但测试/E2E 要明确预期，避免把“云端新消息仍在”误判为删除失败。
- **旧客户端**：当前客户端的 `sync_apply` 已支持 messages delete；修复不需要客户端
  升级即可防新增。但要确认部署面没有比 S2 更老的、不认识 delete 的客户端。
- **单实例约束**：`mark_interrupted_streams` 的注释明确同一 DB 只支持单实例；
  本修复不改变该约束。
- **回滚**：回滚代码不会撤销已写入的 delete 事件/墓碑；这些墓碑对已删消息是
  正确事实，保留无害。

### 6.4 测试 / 工程盲区

- 现有 `test_edit_resend_trim.py` 用 SQLite 内存库、只建 messages/conversations/
  users 相关表；**Option A 的 7 个现有测试可通过**，但**新增事件断言**需要补
  `sync_events`/`sync_tombstones` 表、SQLite JSONB/BigInteger 编译与 `sync_enabled`
  设置。
- `test_continuity_invalidation_p1.py` 的 fake DB（`:35-43`）只实现 `execute()`；
  Option A 需要它返回可 `.scalars()` 的结果并提供 `delete`/`flush`，否则会 AttributeError。
- 现有 sync 测试若断言“编辑/重生成不产生事件”或固定事件条数/seq，需要同步更新。
- `_emit` 的 delete 分支会为每条消息 SELECT Conversation.user_id；方案 A 在
  1000 条切片上会放大查询数，压测要覆盖。
- `SimpleNamespace` 拷贝回归必须保留（2026-10-04 的 500 事故）。
- `SubconsciousLog` purge 与 `invalidate_continuity` 是删除切片的一部分，
  新实现不得漏掉；现有 `test_trim_purges_memory.py` 应继续绿。
- 事件/tombstone 的 `origin_device`、`sync_enabled=false`、重复 id、streaming 行
  四个边界，方案 B 必须逐项测试。
- **stale docstring 会再次埋雷**：`sync_capture.py:6-10` 仍声称同步域没有 bulk
  写缺口；`_delete_message_slice` 的 docstring 说“不碰 ORM 实例”；
  `test_edit_resend_trim.py` 的 module docstring 说“删除按 id 批量 DELETE”。
  P0 必须同步更新这三处，否则未来评审会信任错误的不变量。
- 多设备 E2E：A 设备编辑 → B 设备在线收到 delete → B 本地消息消失；
  B 离线跨过事件裁剪 → delta `resync_required` → resync 墓碑删除。两条都要有。
- 会话删除的 cascade 回归（防止未来加 `passive_deletes=True` 把 message delete
  事件再次吞掉）应纳入守护测试。
- 笔记本/技能/助手删除的 cascade 回归同样建议纳入（同类事件路径）。

### 6.5 本次未覆盖 / 需要产品确认

- P1 的语义选择：删除 reasoning-only 行 vs 落 `interrupted` + 前端占位。
  前者干净，后者保留思考痕迹；需要产品拍板。若选后者，还要处理
  `_load_conversation_messages` 把空 `interrupted` 行注入下一轮的问题。
- 关闭死磕模式是否必须立即 stop 当前 run？当前 UI 的 off 会触发 stop；如果产品
  允许「只关闭后续轮次、不中断当前回答」，可以从触发面减少这类截断。但 stop 本身
  仍需 P1 兜底，不能只靠 UI 行为。
- 死磕进行中（`run.dm is not None`）的 stop 分支：本次事故 `dm=False`，未覆盖；
  需确认死磕自己的 terminal status 不会落空回答。
- 「清空记忆」是否应跨设备删除技能（F-A）——产品语义未确认。
- 消息区间截断事件（替代逐消息墓碑）是架构级优化，不在本工单。
- 客户端 UI 在收到远端 delete 后是否实时刷新当前打开的会话（本地侧问题，
  不属于上游；但会影响用户“多久看到干净”）。
- 客户端 delta delete 清队列 / 服务端 tombstone-create 拒绝（L2）是防复活加固，
  不是本次空回答的直接修复，但建议同波登记。

## 7. 验收清单（上游交付时逐项打勾）

- [ ] P0 代码合并；编辑/重生成两条路径都有 delete 事件 + tombstone。
- [ ] P1 代码合并；stop 与 legacy resume 两条 final 面都不再产生 reasoning-only final。
- [ ] P3 backfill dry-run 报告 + 60 演练；执行后至少 A1 在所有在线/重同步客户端消失。
- [ ] 新增测试全绿；`test_edit_resend_trim.py` / `test_trim_purges_memory.py` /
      `test_stop_streaming_row_cleanup.py` / `test_continuity_invalidation_p1.py` 不回归
      （continuity fake DB 需随方案升级）。
- [ ] 全量后端测试通过；sync 相关测试无事件计数回归。
- [ ] 三处 stale docstring 已更新。
- [ ] 60 部署后冒烟：编辑一条消息 → 云端 `sync_events` 出现 delete、
      `sync_tombstones` 出现对应 id；第二客户端 delta/resync 后本地消息消失。
- [ ] 观察 24h：resync 体积、编辑请求时延、`sync_tombstones` 增长。
- [ ] 本地客户端执行 L1 存量清理 + L2 队列/复活测试，确认该会话不再显示空回答。

## 8. 明确不做

- 不修改 `sync_apply` 的 messages 追加型语义（create 幂等/update SKIPPED/delete 显式）。
- 不把 `_delete_empty_streaming_row` 改成对 streaming 行发 delete 事件
  （streaming 行从未同步；只修 whitespace 判据）。
- 不引入 DB trigger 来捕获删除（origin/user 解析与回显抑制会失控）。
- 不在本工单处理 F-A/F-B/F-C/F-D（另开票）。
- 不要求客户端升级才能获得 P0 的防新增效果；但**存量清理 + L2 防复活**需要客户端
  侧动作，不能只用服务端 P0 解决所有设备的历史数据。
