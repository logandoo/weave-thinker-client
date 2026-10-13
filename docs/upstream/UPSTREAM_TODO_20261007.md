# 上游（chatbot）待办 — 当前汇总（2026-10-08 W10 波后更新）

> **2026-10-08 W10 勾稽：项 9/10 已由上游 3966e1422 完成并随波采纳**——项 9 墓碑自然键载荷（`payload` 列+迁移+resync 回放+coalesce 补写/回填确定性/JSON null 守卫）**且本仓 W10 波按用户指令落地客户端转发**（`_resync` → `t.get("payload")`，TDD 1红→2绿，守卫 test_sync_client_w10_tombstone_payload.py）；项 10 skill_files owns 分支（含 NULL skill_id 回归钉）同步入境。**历史项 1-10 全部闭环；当前开放项=项 11（W10 双评审共同实证，见下）**。

> **2026-10-08 W7 勾稽：项 1-5 全部由上游完成并已部署（deploy 60 烟测 9/9），本仓 W7 同步波已收敛**——项 1 按方案 D 实现（含 spec-v2 同 flush 修订；红线 B `sync_capture.py` 逐字节核验通过）；项 2/3/4/5 同款采纳。本文件保留历史记录。**2026-10-08 W8 勾稽：项 6（6.1 resync 解析 + 6.2 heal-on-create）与项 7（per-flush 缓存）已由上游 1e7c83e1f 完成并随波采纳（含复审 Critical heal 归属修复）；项 8 工具已发货但存在端到端安全缺口 → 重写为项 8（见下，P1/P2）**。

> **2026-10-08 W12 客户端波新增上游清单：见 [UPSTREAM_TODO_20261008_w12.md](UPSTREAM_TODO_20261008_w12.md)**（vibeweaver 完整运行 P0–P2：技能描述解析/收尾门禁/项目根归属/资源根物化/镜像刷新 + 工作区根覆盖产品裁决；含逐文件 1:1 套用说明与验收清单）。
>
> **用途**：本文件可直接交给上游 chatbot 会话执行。内容=截至 2026-10-07（W6 波末、基线 `b92544fd0`）**仍然开放**的全部上游侧事项，此前各波清单已闭环部分不再重复（闭环对照见文末附录）。
> **路径约定**：本文件用**上游路径**（`backend/...`、`frontend/...`）；本仓 vendor 对应为 `server/...`、`app/...`（行号在 ff-safe 件上同款，main.py 等含本仓补丁件标注了 vendor 行号）。
> **红线**：本仓不改上游；上游修复落地后由下一轮同步波逐字节移植/收敛（含红线 B：sync 事件语义必须双端同改）。

## 概览

| # | 项 | 优先级 | 性质 | 预估成本 |
|---|---|---|---|---|
| 1 | **stub 外置 × sync_capture 交互**（04 档项 4，D-57） | **P2（唯一功能级）** | **实现（方案已裁定=D，规格见下）**（红线 B） | 小（`_emit` 一处函数扩展） |
| 2 | CORSMiddleware 未 expose `Retry-After`（06 档项 13） | P3 | 一行修复 | 极小 |
| 3 | healing-guard 单位漂移 UTF-16 vs 码点（06 档项 14） | P3 | 前后端口径对齐 | 小 |
| 4 | `export_registry._file_locks` 无回收（06 档项 15） | P3 | 健壮性 | 小 |
| 5 | API.md 缺工作区目录端点说明（06 档项 16） | P3 | 文档 | 极小 |
| 6 | **resync 基线路径未覆盖 + 存量孤儿桩**（W7 双评审） | **P2** | 解析未覆盖 resync 序列化面 | 中 |
| 7 | `_resolve_payload_fields` resync 路径 O(N²) 扫描（W7 评审 B） | **P2** | 性能/阻塞 | 小-中 |
| 8 | 事件体无尺寸闸 + 存储放大（W7 评审 A/B） | P3 | 运维/隔离 | 中 |

---

## 1. stub 外置 × sync_capture 交互 — P2【本仓已裁定：**方案 D（快照期解析）**；上游照规格实现】

### 现象（用户可感知）
超过内联上限（`[agent] payload_inline_max_chars`，默认 32768）的 `tool_results` / `reasoning_content` 会被 `before_flush` 钩子外置成桩（全文进 `message_payloads` 表）。但同步事件里 **`row_snapshot` 带着"桩"发出**，而 `message_payloads` **不在 `SYNC_ENTITIES`**、payload 行零同步处理 → **超限字段静默变成"单设备本地数据"**；其它设备（以及云端）只拿到孤儿桩——对端 UI 会显式"全文取回失败 + 可重试"，但**内容永远拿不到**（重试打向的仍是 404）。

### 证据（基线 b92544fd0）
- 钩子安装：`backend/main.py:125-126`（vendor `server/main.py` 同区）→ `backend/app/services/message_payload_service.py:287` `install_message_payload_hook()`
- 阈值：`backend/app/services/message_payload_service.py:29` `DEFAULT_INLINE_MAX_CHARS = 32_768`；配置 `backend/app/core/config.py:1146` `agent_payload_inline_max_chars`
- 同步域：`backend/app/services/sync_capture.py:51` `SYNC_ENTITIES`（**无 message_payloads**）；`:106` `row_snapshot`；`:254-264` 监听器注册（after_insert/update/delete）
- 路由硬化（已做，让失败可感知但不解决可得性）：`backend/app/api/message_payloads.py` 按 `(message_id, field)` 归属寻址，孤儿/伪造/完整性不符一律 404
- 本仓兜底现状：`frontend/src/stores/chat.ts` 载荷双保险（200-桩→失败态）+ `MessageBubble.vue` per-field 可重试 UI——**只把"静默"变成"显式失败"，不解决跨设备内容缺失**

### 裁定：方案 D — 同步快照期解析（capture-time resolution）

**结论**：不新增同步实体、不新增端点——在 `sync_capture.py` 的 `_emit` 对 messages 的出站载荷做**快照期解析**：行内某字段是外置桩时，从本端 `message_payloads` 按 `(message_id, field)` 查出全文、校验 sha256 与桩的 `payload_ref` 一致后**以全文替换**再入事件；接收端**零改动**（ORM apply 触发既有 `before_flush` 钩子自动重外置；即使钩子挂起也只是本地内联存储，内容仍可得）。

**裁定理由（D 优于 A/B/C）**：
1. **与既有架构同构**：`_emit` 已有快照期变换先例（streaming 跳过、terminal 翻转 update→create、记忆动态列剥离、Vector 列排除）——D 是同一变换面的一处扩展。
2. **双向天然成立**：两端跑同一份代码；云端（deploy-60）无需额外工作。
3. **零新面**：无新端点/鉴权/迁移/GC；现有路由硬化（归属寻址+完整性 404）、前端双保险与可重试 UI 全部保持。
4. **红线面最小**：仅 `sync_capture.py` 一处函数内扩展——红线 B「双端逐字节」照旧（本仓下波核验）。
5. 被拒方案：**B（事件内联受限全文）**=截断即静默数据丢失，违反「全端一致」根本原则；**C（桩+按需拉取）**=新端点+鉴权+离线对端仍不可得，复杂度与缺口更大；**A（payload 行进同步域）**=需新增实体（`_resolve_user_id` 需 message→conversation 级联查询、apply 排序/缺父守卫/内容寻址表的更新与墓碑语义），机器更重而用户可见结果与 D 等价。

### 实现规格（字段集单一真源=`message_payload_service._EXTERNALIZED_FIELDS`，现为 `tool_results` / `reasoning_content`）

1. `backend/app/services/sync_capture.py` `_emit()` 的 messages 分支（位于 streaming 跳过与 op 翻转之后），对 `_EXTERNALIZED_FIELDS` 逐字段处理**外置桩**（`is_externalized_stub(v)` → 解析 `payload_ref`）：
   - **⚠ 同 flush 可见性（本规格修订点，实现必读）**：`_emit` 运行在 Message 的 `after_insert/after_update`（flush 中途），而 payload 行由钩子在**同一 flush** 创建/更新（FK 依赖排序：`messages` 先于 `message_payloads` 落库）——**纯 `connection.execute(SELECT …)` 看不到尚未 INSERT 的行**（Core 执行绕过 ORM session），新消息路径会恒查空而静默保桩；更新路径 DB 也可能仍是旧值（sha 不符的假阴性）。**必须按以下顺序取值**：
     1) **session 优先**：`sess = object_session(target)`；在 `sess.new` + `sess.identity_map.values()` 中找 `MessagePayload` 且 `message_id == target.id and field == field`，**优先取 `compute_sha256(content) == payload_ref` 的候选**（同 flush 的新值；identity_map 覆盖原地更新的行）；
     2) **DB 兜底**：session 无命中时再 `connection.execute(select(MessagePayload.content).where(MessagePayload.message_id == target.id, MessagePayload.field == field))`（已持久化且本次未变的行）；
     3) 命中且 `compute_sha256(content) == payload_ref` → **以全文替换该字段**；否则**保持桩**并 `logger.warning`（legacy 孤儿/完整性不符——绝不外发未校验内容，与展开路由 404 同立场）。
   - 仅在 op 携带 payload 时执行（delete 无载荷，无需解析）。
2. 接收侧不改：`sync_apply` 对 messages 为 append-only `create` → `model(**data)` + `db.add` + `flush`（ORM 路径；AsyncSession 的 sync session 就是钩子监听的那个 Session 类，与线上消息写入同路径）→ 既有 `before_flush` 钩子按本端阈值重外置（本端 `message_payloads` 行自动生成）。当前无调用方挂起该钩子（`_hook_suspended_ctx` 无外部使用者）；若未来被挂起，内容以内联形态落库（展开路由对非桩存储值直接返回）——**两种落库形态都保证内容可得**。
3. **不做静默截断**：事件体=全文且仅随携带桩的事件发生一次；未来如需传输上限治理单独立项（不得截断）。
4. 性能注记：session 扫描为内存操作（常见路径零额外 DB 查询；DB 兜底仅限已持久化行）；事件重发会重复携带全文——消息追加型、更新稀少，可接受。
5. 实现者注记：`tool_calls` **不在** `_EXTERNALIZED_FIELDS`（钩子不外置它）——读路径的 `slim_message_dict` 可能动态给超限 `tool_calls` 造桩，但其**存储值=全文**，展开路由对非外置存储值直接返回全文，故无同款同步问题，无需处理。

### 验收/回归（上游侧）

1. **捕获单测（必须覆盖「新建」路径以捕捉同 flush 陷阱）**：新消息+超限字段 → 事件载荷含全文（sha 校验通过；此腿在错误的纯-DB 实现下会红）；孤儿/篡改 → 桩保留 + warning（不外发）；更新重外置路径（原地更新行）同样取到新值。
2. **跨设备 E2E**：A 端产生超限字段 → B 端 apply 后经展开面**取到全文**（200 + 全文；非孤儿桩失败态）。
3. **回显/幂等**：apply 后重发事件按既有 op 词表无害（create 撞行 SKIPPED）。
4. **本仓同步波核验**：`sync_capture.py` 逐字节一致 + `server/tests` 全量 + 跨设备组件腿 + DMG scratch。

---

## 2. CORSMiddleware 未 expose `Retry-After` — P3

- **位置**：`backend/main.py:57` CORSMiddleware（vendor `server/main.py`）。当前只配 `allow_origins/credentials/methods`，无 `expose_headers`。
- **现象**：客户端跨源（桌面 SPA origin=`app://weave` → `http://127.0.0.1:<协商端口>`）**读不到 `Retry-After`**（不在 CORS 安全清单）——drain 门 503 时恒回退客户端内置 5000ms。当前服务端建议值恰为 5s，行为等价；上调建议值时两端将不同步。测试注入响应头故测不出。
- **建议修法**：`expose_headers=["Retry-After"]`。
- **验收**：一条 CORS 断言测试（跨源响应含 `Access-Control-Expose-Headers: Retry-After`）。

## 3. healing-guard 单位漂移（UTF-16 vs 码点）— P3

- **位置**：`frontend/src/stores/streamReducer.ts`（愈合守卫用 `Array.from(full).length`=**码点**）vs `backend/app/services/message_payload_service.py:64-73` `_item_text_total`（W6 改口径为 **UTF-16 码元**，与前端徽标 JS `.length` 同构）。
- **现象**：同一 `__size_bytes__` 字段，徽标消费者口径一致 ✓，愈合守卫口径过期 ✗——含 emoji 等 astral 字符的文本项比对必不相等 → 守卫误判"未愈合"→ 多发一次 payload 取回请求。潜伏（文本正文内联豁免，仅 legacy 桩+emoji 踩到），无正确性问题。
- **建议修法**：守卫度量与 `_item_text_total` 同源（改 UTF-16，或两侧共用一个字段语义）；顺带更新该处"Python len(v)=码位"的过期注释。
- **验收**：含 astral 字符的愈合守卫行为腿（现 A2-8 测试伪造码点值，测不出）。

## 4. `export_registry._file_locks` 无回收 — P3

- **位置**：`backend/app/services/export_registry.py:33`（进程内 dict 只增不减；键=workspace/任务/文件组合）。
- **影响**：单机桌面可忽略；长驻多租户进程缓慢增长。
- **建议修法**：LRU/引用计数/定时清理；或随任务 TTL 一并回收。

## 5. API.md 缺工作区目录端点说明 — P3

- **位置**：`backend/API.md`——缺 `List one directory inside the user's workspace (folder manager)` 条目（对照文件列表端点说明）。pre-existing 缺口。
- **建议修法**：补一条（或修生成器）。

---

## 附录 A：无需上游处理（本地侧备忘）

- `app/tests/workflows/test_refresh_shrink_guard.cjs` / `test_stream_true_guard.cjs` 在盘未接入 `test:unit`——**本地决策**（是否接线由本仓定，与上游无关）。
- 本仓引入的 `test_local_patch_imports.cjs`（登记补丁 import 守卫）为 vendor 专有测试，不需上游对齐。

## 附录 B：已闭环对照（勿重复处理）

- 04 档（2026-10-04）：项 1（mock 桩）/2（payload 路由）/3（abort×paged）/5（exportNotebook blob）/6（composables 类型）/7（3 测试入库）→ 已由上游修复并同步收敛。
- 06 档（2026-10-06）：项 1/2/3（恢复锚/backfill 孤儿/收窗）→ W4 修复；项 4（迁移排序）/5（boot sweep 同步）→ W5 修复；项 6a（重复索引统一，W6 `*_idx_unify_drop` 迁移）/6b（测试更名 `…to_create`，已闭环）/7（bulk endnote）/8（共享导出删除面）/9（徽标守卫 `stubMarkersTrusted`）/10（resume 503 重试）/11（mojibake）/12（marker 整值+源键剥除）→ W6 修复。
- W6 评审项 13/14 已在本文档项 2/3；15/16 在项 4/5。


---

## 6. resync 基线路径未覆盖 + 存量孤儿桩不自愈 — **P2【2026-10-08 W8 已闭环】**
> 上游 1e7c83e1f：6.1 `resolve_payload_stubs_batch`（resync 快照解析，sha 校验+分块 IN）· 6.2 `_heal_broken_payload_fields`（heal-on-create，含复审 Critical 行归属修复）；本仓 W8 波双件逐字节采纳。以下为历史记录。

- **现象（两段，均实证）**：
  1. **resync 基线（更严重，且不限于存量）**：`/api/sync/resync` 序列化的是**行快照**（`backend/app/api/sync.py:741` → `row_snapshot`，桩原样；非事件日志重放），客户端逐条应用后推进 `pull_cursor`（`sync_client.py:328`）——**新设备首连/全量 resync 对所有外置字段（含 W7 修复后新消息）都拿到孤儿桩**，展开面永久 404，重试不可成功（`resolve_message_fields`/接收侧钩子见桩即跳过，无行可建）。
  2. **存量孤儿桩**：修复前已产生的双向存量（本地→云的云端无 payload 行；云→本地的客户端无行）不会自愈。
- **建议修法**：
  1. resync 序列化侧复用 `_resolve_payload_fields` 同款解析（或在 resync 响应内联全文）；需评估 resync 载荷体积（见项 8）。
  2. 存量：一次性修复通道（带解析重发/backfill orphan 修复式重放；注意 LWW/echo）；或明确文档化「resync 路径修复前，跨设备可得性以事件路径为限」。
- **影响面**：resync 项影响**所有含超限字段的消息**（新老皆然）在新设备引导/全量重同步场景；事件路径（日常同步）自 W7 起正确。展开面为"显式失败+可重试"非静默。


## 7. `_resolve_payload_fields` 在 resync 应用路径 O(N²) 扫描 — P2【2026-10-08 W8 已闭环】
> 上游 1e7c83e1f：per-flush 缓存（hook 写入 `(id,field)→content`，解析 O(1) 优先+sha 复核+回退），并附「批量应用零候选扫描」证伪测试。以下为历史记录。

- **现象**：解析器每次按 `sess.new + identity_map` 全表扫描找候选（`sync_capture.py` 实现；正确性面经 v2 规格验证）。客户端 resync 在**单 session 批量应用全史**后一次提交（`sync_client.py:307-329`）：identity_map 随应用单调增长，含桩消息为 S、全量为 N 时总扫描量 ≈ O(S×N)——评审估算 5k 桩/50k 史≈数亿次比较，**阻塞 sidecar 事件循环**（API/UI 卡顿）。现测试仅覆盖单消息 flush，结构性测不到。
- **建议修法（三候选，上游裁定）**：① 捕获侧 per-flush 索引缓存（钩子外置时把 `(message_id, field)→content` 存入 session 挂载缓存，`_resolve_payload_fields` O(1) 取）；② resync 应用分批提交（缩小 identity_map；注意游标/幂等语义）；③ resync 应用路径显式挂起解析（echo 事件本就会被幂等 SKIP，无需解析——需证明所有消费面无损失）。
- **附带小坑**：`object_session(target) is None` 时退回同 flush 不可见的 DB 查询 = 静默 v1 行为；当前无 detach 路径，建议补断言/告警而非静默。

## 8. retention 端到端安全缺口（工具已发货，**消费方/语义未闭环**）— **P1/P2（2026-10-08 W8 双评审判定；含运营禁令）**

> **⛔→✅ 运营禁令（2026-10-08 W9 解禁）**：解禁条件已满足——上游 525b42ee3 修复 8a-8d **且**本仓 W9 波落地三项客户端消费方（8a `_pull` 缺口信号/8b `_resync` 先墓碑后快照/8d 超时+字节预算分块；tests/test_sync_client_w9_consumers.py 11/11，RED→GREEN）并经双评审+FCV 复核。**部署覆盖面前提**：仅当参与该服务器同步的**所有客户端均已 ≥W9**（消费方在位）才可 `--execute`；旧客户端仍会静默漏事件——prune 前确认覆盖面（脚本 preview/dry-run 先行）。——缺口信号无消费方，prune 会使滞后客户端**静默漏事件**（脚本自注仅告警"old clients"，而本仓/本包客户端恰是那个 old client）。

- **8a（P1）缺口信号无消费方 + 空日志假阳性（两处必须同修）**：
  - `resync_required`/`min_available_seq` 已产出（`backend/app/api/sync.py:527-540`）但 `sync_client._pull` 只读 events/cursor/has_more 并**无条件推进游标**（`sync_client.py:454-502,484`）；契约档要求客户端遇信号时放弃增量并全量 resync——未实现。修法：`_pull` 遇 `resync_required` → 置 `_resync_pending`、**不推进终游标**，交 `_resync` 重座。
  - 假阳性：`resync_required = cursor>0 and (min_seq is None or cursor<min_seq-1)` 对**零事件用户**恒真（resync 后游标=全局 max seq，空日志 min=None）→ 遵约客户端将每 tick 全量 resync（风暴）。修法：`min_seq is not None and cursor < min_seq-1`，或引入 per-user prune watermark；同步修 `tests/test_sync_w7_followups.py:568-571` 固化行为。
- **8b（P2）resync 不能恢复删除（墓碑只写不读）**：`_resync` 仅并快照行（`sync_client.py:304-329`），resync 端点不返回墓碑（`sync.py:719-784`）；`SyncTombstone` 写入后全仓无**同步语义消费方**（resync/delta/push 均不回放；仅保留工具计数与测试断言引用）——滞后于裁剪下限的设备上，已删实体将**复活且永不再传播**（`_push` 过滤 `rx:`）。契约的"full resync 可恢复"表述需修正或补墓碑回放。
- **8c（P2）resync 快照/游标竞态（被 6.1 加宽）**：快照逐表 SELECT（READ COMMITTED），解析批量查询位于其中，`max_seq` 最后计算（`sync.py:752-761,783`）——表 SELECT 之后、`max_seq` 之前提交的实体拿到 `seq ≤ 返回游标` 但无快照行 → 客户端永久漏之至手动全量。修法：**先取游标再拍快照**（或事务内快照隔离）。
- **8d（P3，随修）**：
  - heal 用宽松 `is_externalized_stub` 而非 `stub_markers_trusted`（`sync_apply.py:141`）——合法内容恰形如 `{"_externalized": true}` 的 ≤阈值字段会被误判孤儿而覆盖，破 append-only；且字段集不一致（heal 走 `_EXTERNALIZED_FIELDS`，resync 解析含 legacy `tool_calls`）。
  - `sync_client.py:233-234` 事件通道 15s 超时 + 无尺寸闸：6.1 现把全文内联进单次 resync 响应——弱网头阻塞仍存（W7 项 8 的"失败隔离/尺寸治理"半未实现）。
  - per-flush 缓存 flush 结束未清（sha 复核保正确，仅注释与卫生）；分块解析峰值内存约翻倍+孤儿告警逐字段刷屏（大史）。
  - `sync_events` 裁剪无 push 侧感知（客户端实例上跑 prune 会删未推送事件且无 push 侧缺口信号）；`server/API.md` 未记录 DeltaResponse 新字段与 retention 工具（API.md 是 sync.py 自述的契约事实源）。


## 9. user_model_providers 自然键墓碑在 resync 回放中无法删除（复活）— P2（W9 双评审 I2）
- **现象**：A/B 端独立创建同 `(kind, provider)`（id 不同，服务端自然键归并，B 保留本地 id）。A 端删除后 B 离线越过保留沿 → resync 墓碑回放：`_resync` 以 `payload=None` 走 delete，而 `apply_change` 的自然键兜底要求 payload（`sync_apply.py:230`），且上游墓碑载荷只有 `{entity_type, entity_id, deleted_at}`（`sync.py:757-765`；capture 不写自然键）→ **本地行删不掉、永久复活**；用户重建同 provider 可撞 `uq_user_model_providers_user_kind_provider`。若本地已有行 id 恰等于服务端 id 则正常——恰是 I4 自然键合并场景必坏。
- **修法（上游）**：墓碑载荷补自然键（kind/provider）供 apply_change 兜底；或 resync 侧对该类特化。

## 10. skill_files delete 恒 SKIP（owns() 缺分支，审计面）— P3（W9 双评审 M4，red-line B）
- `owns()`（`sync_apply.py:83-94`）无 `skill_files` 分支而 `parents_owned` 有；直接 delete 恒 SKIP（当前仅父子级联可达=潜伏）。补 `skill_files` 归属分支（双端逐字节）。


## 11. delta 窗口压缩抹掉自然键载荷（update→delete 组合）— P2（W10 双评审共同实证，上游面）
- **现象**：`backend/app/api/sync.py` `_compress_window` 的 `("update","delete")` 分支把幸存 delete 事件的 `payload` 置 None（`("create","delete")` 直接消对）。场景：B 离线期间用户先编辑后删除同一 `user_model_providers`，两事件落入 B 同一 delta 窗口 → 压缩后自然键载荷丢失 → B 端异 id 归并行 `db.get` 未中、兜底因 payload falsy 不可达 → **行长期存活**（增量路径复活面仍在；仅 resync/墓碑路径已由 W10 修复）。双评审各自 `_compress_window` 实测复现（update+delete → delete payload=None；单独 delete 保留）。
- **修法**：该分支幸存事件应使用 delete 事件自身的 payload（`prev["payload"] = ev["payload"]`，普通类型本为 None 无副作用）；补 `_compress_window` provider-payload 测试腿。
- **本地处置**：W10 波登记（上游面直采；全仓无该组合测试，正因此逃逸）。
- **附（W10 scoped 复审 N1，同域残余）**：跨周期在途推送排序——已推送的 create/update 可能在服务端晚于他端 delete 落地（push 不查墓碑、delete 无时间戳），推送端因回显抑制不可见 → 本地/服务端分叉直至手动全量。客户端不可修（缺服务端 seq/排序反馈）；建议 push 面墓碑/自然键仲裁或事件序反馈随项 11 一并考虑。

## 附：W10 本仓客户端已修两项（评审 I2/M1，无需上游）
- **I2 清队列（删除权威）**：`_resync` 墓碑删除后清该实体本地待推队列（自然键命中本地行一并清）——防待推事件把删除复活到服务端+回显抑制致静默分叉（与 `_merge_default_assistant` 同款模式）；测试 `test_resync_tombstone_clears_queued_events_for_natural_key_row`。
- **M1 健壮**：非 dict 墓碑项跳过+告警（异常处理器不再二次解引用毒项）；测试 `test_resync_tombstone_non_dict_entry_isolated`。
