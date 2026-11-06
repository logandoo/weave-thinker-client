# AUDIT 2026-10-31 — 客户端残留「无 query 的空回答」根因

## Scope

只读诊断 conv `d7ea65b8-9241-4112-bd5d-afe87338b400`（三款AI编程工具离线可用性对比）
在桌面客户端出现、网页端不出现的空 assistant 消息。

- 云端：`https://<cloud-host>:8158`，账号 `alice`
- 本地：`weavethinker` DB（桌面客户端 sidecar）
- 结论级别：**Critical**（同步删除事件缺失，影响所有编辑/重新生成路径）

## Findings

### F1 (Critical) 消息切片删除绕过 ORM 事件，同步端收不到 delete

`server/app/api/chat_persistence.py:176-195`：

```python
async def _delete_message_slice(db, slice_rows, user_id=None):
    ...
    for i in range(0, len(ids), 500):
        await db.execute(sa_delete(Message).where(Message.id.in_(ids[i:i + 500])))
```

`sync_capture` 只通过 SQLAlchemy mapper 事件发射同步事件
（`server/app/services/sync_capture.py:426-441`，`after_insert/after_update/after_delete`）。
Core `DELETE` 不触发 mapper 事件 → 不写 `sync_events`，也不写 `sync_tombstones`。

`sync_apply` 对 messages 是追加型语义：`create` 幂等插入、`update` 恒 SKIPPED、
只有显式 `delete` 事件才删除（`server/app/services/sync_apply.py:216, 273-291, 307-330`）。

`_resync` 只做「先墓碑、再快照 upsert」，不删除快照中不存在的本地行
（`server/app/services/sync_client.py:337-443`）。因此即使手动全量 resync，
孤儿消息也不会被清除。

### F2 (Important) stop-cancel 会把「0 正文 + 仅 reasoning」落成 final assistant 消息

`server/app/api/chat_stream_transport.py:1166-1200`：用户主动 stop 时，只要
reasoning 非空就走 `_save_assistant_terminal(... status="final")`，即使
`content == ""`。零内容且零 reasoning 的分支才会删除 streaming 空行
（同文件 1201-1226）。

本次生产日志（服务器本地时间 UTC+8）：

```
22:34:50 Stop-cancel conv=d7ea65b8... content=0 reasoning=419 segments=0 tools=0 state_content=0 dm=False
22:34:51 Persisted partial reply after stop for conversation d7ea65b8... (0 chars)
```

这解释了「空回答」本身：正文为空，只有 419 字思考内容。

### F3 (Important) 编辑重发的删除没有到达客户端

云端随后只剩 2 条消息（Q2/A2）；本地仍有 3 条：

```
f697cf2d assistant 2026-10-30 14:34:48 content="" reasoning=419 delivery=final
148ae62c user      2026-10-30 14:35:07 content="workbuddy，codex 和 claude code..."
c7c2371f assistant 2026-10-30 14:36:20 content="结论先说..."
```

本地同步事件只有 `messages create`：

```
14:34:54 create f697cf2d
14:35:24 create 148ae62c
14:36:25 create c7c2371f
```

云端 `sync_events` 中同样没有任何该会话的 `messages delete`。
云端全量 resync 快照（2026-10-31 复跑）：该会话只有 2 条消息，
131 条 tombstone 中没有 `f697cf2d`。即：**删除事实从未进入同步域**。

### F4 (Minor) 客户端的 blank 过滤器救不了这条消息

`app/src/stores/chat.ts:105-117` 的 `isBlankPersistedMessage` 只在
`content`、`reasoning_content`、`tool_results`、`tool_calls` 全为空时过滤。
F2 的消息带 419 字 reasoning，因此不会被当作 blank；它按 `created_at`
排在 Q2 之前，显示为首条「没有 query 的回答」。

## Root cause chain

1. 14:34:43 重新生成旧回答，旧 assistant 行被 F1 的批量删除移除（无同步事件）。
2. 14:34:50 用户开关死磕模式触发 stop；F2 把 0 正文/419 字 reasoning 落成 final。
3. 14:34:54 客户端通过增量同步收到该行的 `create`（这是它第一次见到该会话的消息）。
4. 14:35:07 用户编辑 query 重发；F1 删除旧 query + 空回答（无同步事件），插入 Q2。
5. 客户端永远收不到 delete；F1 的 resync 也不做「快照差集删除」→ 空回答永久残留。
6. 网页端直接读云端 DB，物理行已删 → 网页看不到；客户端读本地同步库 → 看得到。

## Attribution (upstream vs client)

与上游 `../chatbot/backend` 逐文件比对（sha256）：

| 文件 | 本地 | 上游 | 判定 |
|---|---|---|---|
| `app/api/chat_persistence.py` | `bb866d23…` | `bb866d23…` | **完全相同（上游）** |
| `app/services/sync_capture.py` | `664341eb…` | `664341eb…` | **完全相同（上游）** |
| `app/services/sync_apply.py` | `e245efa3…` | `e245efa3…` | **完全相同（上游）** |
| `app/api/chat_stream_transport.py` | `b18d1888…` | `6bd09d08…` | 仅 12 行差异，位于 2754 行 vibeweaver active 赋值；stop-cancel 分支相同 |

- **P0 delete 事件缺失、P1 stop-cancel 空回答**：属于**上游 + 部署 60**。上游先修，60 部署后
  web 与客户端同时受益。
- **客户端永久保留**：本地 `sync_client.py` 是本地新增的 S2 同步引擎（上游没有该文件），
  resync 只做「墓碑 + 快照 upsert」，不做快照差集删除 → 属于**仅本地**的韧性缺口，
  但它只是放大器；服务端从未下发 delete/tombstone，客户端无法凭事件自愈。
- **UI blank 过滤**：`isBlankPersistedMessage` 与上游 `frontend/src/stores/chat.ts`
  逐行相同，不是移植回归；它按设计保留「有 reasoning 的消息」。
- 结论：**根因在上游；客户端是暴露者 + 次因（不做差集清理）**。完整修复 = 双端：
  上游补 delete/tombstone + 本地补 orphan 兜底/存量清理。

## Remediation (future C2 tasks)

1. **P0**：`_delete_message_slice` 在 Core DELETE 前按 id 显式写
   `sync_events(op="delete")` + `sync_tombstones`（复用 `_emit` 的 delete 分支语义，
   分批写入；与新用户消息插入保持同一事务）。
2. **P1**：stop-cancel 对「content 为空且无 tool_results/tool_calls」的行不要落
   final；按空壳删除，或标 `interrupted` 并在 UI 明确显示「已停止」。
3. **P2**：客户端加载时增加 orphan 兜底：无正文、无工具、且前一条不是 user 的
   assistant 行不渲染（只作兜底，不替代 1）。
4. **数据修复**：对已污染客户端删除 `f697cf2d-91ee-4072-b821-a94a9aad1986`
   （或提供「强制以云端快照替换本会话消息」的一次性工具）。

## Not covered

- 未验证其它消息批量删除点是否也缺少同步事件（`chat_stream_transport.py`
  1583/3169/3293 的空 streaming 行删除不发射 create，按现有契约无需墓碑）。
- 未在测试环境复现 UI 截图；结论基于生产 DB、同步事件流、resync 快照与代码路径。
