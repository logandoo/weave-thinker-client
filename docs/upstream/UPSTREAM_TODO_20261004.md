# 上游（chatbot）侧待办清单 — 2026-10-04 同步波产出

> **2026-10-06 W3 勾稽：6/7 已完成（项 1/2/3/5/6/7）· 项 4（stub×sync_capture 交互）未动=**仍为双端裁定待办**（评审 A I1 纠正：archive 原「7/7」过度主张，同步修正）**——项 1 mock 桩/项 6 类型修复=converged（双方同款）；项 2 路由=上游超集同修（归属寻址+sha256 完整性 404+size_bytes 字节语义；登记表已销账、守卫测试整取上游超集版）；项 3 abort×paged=上游修复（fullWindow 收窗/stop 零回答直收 + test_abort_caughtup_paged 随波采纳）；项 5 exportNotebook blob 已恢复（上游注释引用本清单项 5）；项 7 三测试已提交（converged）。**项 4 未动**：deceebaca 对 sync_capture/sync_apply 零改动、MessagePayload 仍不在 SYNC_ENTITIES——维持 D-57 双端裁定。W3 新发现转入 UPSTREAM_TODO_20261006.md。

> **2026-10-05 W2 快照同步勾稽**（target 822637aee）：项 1 未修（test_memory_retrieve_meta.py 未入增量）· 项 2 **未修**（message_payloads.py:90 全局 `sha256==ref` 仍在；上游新增 test_message_payload_api.py 与本地修复兼容、全绿）· 项 3 **未修**（chat.ts 快照零改动）· 项 4 未动（同步域零增量）。四项全部仍开放。

> 来源：weave-thinker-client 同步波 b369f8a9f→a22d29c65 的双对抗评审 + FCV（D-55..D-57，tests/decisions.md）。
> **红线**：`/home/user/Documents/DEV/chatbot` 只读（memory/project_port_mandate.md）——本清单不在上游仓改任何文件，交付用户在 **chatbot 会话**执行（服务端修复 + `deploy_60.sh` 部署面按其流程）。
> 判定依据：docs/DECISION_MATRIX_local_vs_upstream.md（2026-10-03 用户裁定评估必答项）。

---

## 1. test_memory_retrieve_meta.py 桩签名过期（改参忘改 mock）— P2

- **现象**：上游 master（a22d29c65）自身红——`backend/tests/test_memory_retrieve_meta.py` 的 `fake_stage2(db, uid, cc, stage0, qt)` 五参桩与 `memory_retrieval_service._stage2_description_expansion(..., param_overrides: dict | None = None)`（P2 策略路由，ef3f2ebcd 引入）不匹配，跑 `test_meta_returns_ctx_ids_top` / `test_text_channel_unchanged_dual_path` 即 `TypeError: fake_stage2() got an unexpected keyword argument 'param_overrides'`。
- **证据**：`backend/app/services/memory_retrieval_service.py:180`（调用点 `param_overrides=_profile_params`）vs `backend/tests/test_memory_retrieve_meta.py:35`（`async def fake_stage2(db, uid, cc, stage0, qt):`）；上游工作树与提交态一致（`git diff a22d29c65 -- <两文件>` 空）。
- **建议修法**：桩签名加 `param_overrides=None`（断言零动），或改用 `**kwargs` 惯用桩。
- **本地处置**：已按 D-55 放宽桩签名（`- test-change:` 在案）；vendor 侧全绿（server 1864/0）。上游同修后下波直采归位。

## 2. message_payloads 路由全局 sha256 寻址（越权读面）— P1 安全

- **现象**：`GET /api/messages/{message_id}/payload/{field}` 对外置桩按**全局** `MessagePayload.sha256 == ref` 取行，无 `(message_id, field)` 归属、无属主过滤——**伪造桩**（自己消息的 stub 携带他消息 payload_ref）可读到非本消息外置内容（跨消息/跨用户内容替换面）；孤儿桩（行缺失）还会把桩体本身当内容 200 回吐（客户端静默停在预览，违背「截断必须可感知」红线）。
- **证据**：`backend/app/api/message_payloads.py`（修复前 `select(MessagePayload.content).where(MessagePayload.sha256 == ref)`）vs 同波服务层已加固的 `_lookup_payload`（`backend/app/services/message_payload_service.py:260-272`，评审 C2：按 `(message_id, field)` 归属查询，杜绝跨消息内容替换）——**路由是弱路径**；`database.py` 契约注记「sha256 仅作完整性元数据不作寻址」同向。
- **建议修法**：路由改用 `_lookup_payload(db, message_id, field)`（或同款 `(message_id, field)` WHERE），行缺失/伪造一律 404（勿把桩体当内容回吐）；补一条伪造桩越权 404 的回归腿。
- **本地处置**：vendor 侧已修（server/app/api/message_payloads.py + 前端 isExternalizedStub 双保险 + 守卫 `server/tests/test_message_payload_route_scoping.py` RED3→GREEN14），已入本地补丁登记表（DECISION_MATRIX §3，下波 conflict 按登记保留）；**上游同修才覆盖 Web 端/部署 60 面**。

## 3. abort 重连 × paged slim refresh 启发式不相容 — P2

- **现象**：上游新分页瘦身刷新（`messageLimit: MESSAGE_PAGE_SIZE`=50）与 abort/重连的 caught-up 启发式 `expectedMessageCount = 内存长度 + 1` 不相容——≥49 条历史消息的会话上窗口化刷新永不满足长度检查 → Phase-1「答案已落库」快路径失灵，fall-through 在刷新窗口上再叠 `local-abort-*` 局部气泡（重复/陈旧答案 + `endStreaming` 延迟）。
- **证据**：`frontend/src/stores/chat.ts`（expectedMessageCount 启发式 + `ensureMessageFull`/`refreshConversation` 分页路径）；两侧行为均字节镜像自 a22d29c65（本地残差仅 apiUrl 包装）。
- **建议修法**：caught-up 判定改为分页感知（如以最后一条 id/时间戳锚定，或拉满窗口后再判长度），并补一条「≥50 条消息会话 abort 重连」的回归腿。
- **本地处置**：D-56 parked（C 类云端语义，本地改下波被「以上游为准」吞掉或制造分叉）。

## 4. stub 外置钩子 × sync_capture 交互（同步域红条 B 面）— P2（需双端裁定）

- **现象**：`before_flush` 钩子把 >32KB 的 `tool_results`/`reasoning_content` 外置成桩，而 `sync_capture` 的 `row_snapshot` 带**桩**发出（`message_payloads` 不在 `SYNC_ENTITIES`，payload 行零同步处理）→ 超限字段静默变设备本地、对端继承孤儿桩（对端展开面走 404 可感知失败，但内容不可得）。
- **证据**：`backend/main.py`（外置钩子安装）· `backend/app/services/sync_capture.py:106-121,191`（row_snapshot 带桩）· `sync_client.py`/`sync_apply.py`/`api/sync.py` 零 payload 处理。
- **建议修法**：同步域扩展裁定（用户/上游）——候选：payload 行进 `SYNC_ENTITIES`（需 sync_apply 逐字节同改）或事件载荷内联受限全文或桩+按需拉取协议；**属红线 B（sync 事件语义），必须双端同改+逐字节移植**。
- **本地处置**：D-57 记决策项（memory/project_sync_waves_20260924-29.md deferred 清单同域）。

---

## 附录：deferred Minors（上游侧/良性，优先级低）

| 项 | 位置 | 说明 |
|---|---|---|
| 「sha256 不作寻址」注记漂移 | backend/app/api/message_payloads.py / backend/app/db/database.py | 路由面（项 2）修复后，storage 寻址注记与实现语义仍应向 C2 契约对齐 |
| has_more_messages 客户端推断 | frontend/src/api/chat.ts `getMessagesPaged` | 整页边界（恰=limit）多发一次空拉后自愈；建议由服务端字段权威判定 |
| database.py 对本地残差 2 空行 | backend/app/db/database.py | cosmetic，仅影响 vendor local-mod 归类 |

## 归属判定（DECISION_MATRIX 评估必答项）

```
[归属判定]
- 运行时: 云端为主（项 2 Web 端/60 面 · 项 4 同步域）；项 1=测试面 · 项 3=客户端+云端共担（以上游设计为准）
- 文件类别: C 共享镜像（项 2/3/4）· D 测试（项 1）；B 红线同源=项 4 若采纳 schema/实体域改动则双端同改
- 结论: 仅上游+部署60（项 1/2/3 服务端与 Web 面）+ 双端裁定（项 4）
- 交付动作: chatbot 会话修复 → deploy_60（涉及面）→ 下轮同步波 vendor 归位（项 2 本地补丁已被登记表保护）
- 验证面: 上游侧 pytest（项 1 的 2 腿转绿）· Web 端伪造桩 404 回归腿（项 2）· ≥50 条会话 abort 重连回归腿（项 3）· 同步域裁定后两端对测（项 4）
```

---

## W2 新发现（2026-10-05，上游 98f3d26e2 编辑器波）

### 5. exportNotebook 丢失 `responseType: 'blob'` — P3
- **现象**：`frontend/src/api/notes.ts:43` 的 `exportNotebook` 上游移除了 `responseType:'blob'`（base 本地版有）。当前 CSV 经 string→Blob 仍可用，但二进制/大导出语义变弱。
- **建议**：恢复 `responseType: 'blob'` 与 `params` 并存；或明确导出为纯文本契约。
- **本地处置**：以上游为准保留（runging 语义兼容）；本清单登记。

### 6. W2 新 composables 类型缺陷（本地已修，建议上游同修）— P3
- `frontend/src/composables/toolbarTip.ts:1` `import { type Event } from 'vue'`（vue 无此导出，TS2724）→ 删 import 用全局 DOM Event
- `frontend/src/composables/editorFontSize.ts:61` `sel.anchorNode`（Node|null）未收窄（TS2345）→ 守卫后 cast
- `frontend/src/composables/useFindReplace.ts:30` 可选方法调用（TS2722）→ `?.getTextContent?.()`
- **说明**：vite build 不类型检查、vue-tsc 1.8.27 与现装 TS 崩溃，故静默入境；运行时无影响。本地 vendor 已修（下波冲突按规则处理）。

### 7. W2 三件新测试未 add（上游提交遗漏）— P2（流程）
- `backend/tests/test_backfill_message_payloads.py` · `test_message_payload_api.py` · `test_message_payload_hook.py` 在上游仍为 untracked（用户提交 98f3d26e2 未含）；本仓已采为 local-only。建议上游补提交（含将 docstring 的 `Run from backend/: ../.venv` 改为自身布局）。
