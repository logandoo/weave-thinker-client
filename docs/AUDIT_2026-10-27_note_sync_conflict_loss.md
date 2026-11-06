# AUDIT 2026-10-27 — 笔记同步/冲突机制评估：弱网下本地编辑丢失面（对照 Evernote 症状）

> 类型：C4 Audit（read-only）· **仅评估，不修改任何源码**。
> 范围：本仓 `app/src`（Vue 编辑器/存储）+ `server/app`（本地侧车 FastAPI + PG）+ `server/app/services/sync_client.py`（同步 worker）+ 上游本体 `/home/user/Documents/DEV/chatbot`（对照）。
> 证据：`tests/evidence_note_sync_conflict_20261027.log`（E1–E17）+ **两个独立评审代理的对抗性核实**（`tests/fcv_report.md`，Critical/Important 逐条 CONFIRMED/REFUTED/DOWNGRADE）。
> 同源性前提（E14）：`server/app/services/sync_apply.py`、`sync_capture.py` 与上游 `backend/app/services/` **shasum 逐字节相同**（红线 B）；`sync_client.py` 为**本仓独有**（上游无此件，E15）。
> ⚠ 阅读注意：本文 §3 的定级与措辞已经双评审对抗修正（F1/F5 降级、F7 归属纠正、F3 加限定与加重、F4 划界上提）；修正记录见 §8。

---

## 0. 结论先行（TL;DR）

| 问题 | 结论 |
|---|---|
| 线上和本地笔记冲突会怎样？ | **LWW 整行覆盖，无合并、无冲突副本**：`updated_at` 严格大者胜，平局本端胜；败方内容从 `notes` 表消失，留痕最多一条**仅含时间戳**的 `sync_conflicts` 记录（且存在**完全零留痕的覆盖窗口**，见 F10）。败方全文残存于 `sync_events.payload`（**用户不可见、SQL 可查**；仅当运维手动跑裁剪脚本才消失）。**本项目不存在"冲突笔记"副本实体**（E13=0 命中）。 |
| Evernote「三处全无」可能原因？ | 共同结构=**被丢的编辑从未获得持久身份** + **冲突副本判据与覆盖判据共享同一份同步账本**。四个机制假说 H1–H4（§2），弱网放大每个假说的触发概率。 |
| 当前项目会不会有？ | **会，但必须分两类说清**：① 同步机制类——F1（resync 删除权威清队列→**零留痕**蒸发，需"对端删除"等前置）、F3/F10（LWW 覆盖无副本+留痕缺失）、F2（delete 无 LWW）；② 未保存类——F4（5 分钟自动保存窗口/无草稿/PUT 挂死楔死保存，**非同步丢失**，但用户观感同构）。**纯弱网编辑（无对端删除、运维未裁剪）在本仓不会丢**（§1.4 保护面）。 |
| 上游代码会不会有？ | **会**。LWW 无副本（E2/E14）、delete 无留痕（E1）、编辑器无快照契约（E11/E12，**本仓已修、上游未修**）、图片 flush 15s 放行（E17，**双侧共享镜像同在**）全部成立。上游无 sync_client（E15）；F1 的清队列缺口是本仓 worker 独有实现面。 |

**严重度总表（评审后定级）**：F1/F2/F3/F4/F10 = Important · F5/F6/F7/F8/F9/F11 = Minor · 无 Critical（F1 原评 Critical，双评审一致降级——触发需多重前置，见 §3 F1）。

---

## 1. 问题一：线上（云端）和本地笔记冲突时，会发生什么

### 1.1 冲突前：保存与同步的链路

1. **本地保存（local-first）**：编辑器 PUT → 本地 PG `notes` 行更新 + **同一事务**写 `sync_events` 出站事件（`sync_capture.py:404-442` mapper 事件，`connection.execute` 与业务行同一事务，`database.py:976-978`；`api/notes.py:3001` commit）。**PUT 路径原子性经对抗评审独立核实**：commit 前任何失败=整体不提交+HTTP 500，前端保脏重试（`NoteEditor.vue:1446-1449`）——不存在"行落库而事件丢"的部分写（评审 B 方向 1）。
2. **后台周期（`sync_client.py:198-228`）**：固定 **push → pull**。push 逐块 HTTP 成功才推进 `push_cursor`（`:569-586`）；失败抛异常→游标不动→原位重试（`:156-167`）。弱网断在半途不丢本地内容。
3. **仲裁入口唯一**：两侧都走 `apply_change`（`sync_apply.py` docstring："否则双端会得出不同的胜出版本"）。

### 1.2 冲突时：LWW 判定与后果（核心答案）

- **判定**（`sync_apply.py:365-369`，E2）：incoming `updated_at` **严格大于**本地行才写；`new_ts <= current_ts` 一律 `STALE`（**平局=本端胜**）。
- **胜负后果**：赢方**整行覆盖**（`for key, value in data.items(): setattr(...)` + `row.updated_at = new_ts`，E2）——title/content/notebook_id 全替换，**无字段级合并**。
- **败方内容去向**：本地 `notes` 行即刻失去全文；全文残存仅 `sync_events.payload`（JSONB 全行快照，`database.py:988`）。随后 push 被 STALE 拒收 → `SyncConflict(reason="skipped_remote")`（`sync_client.py:571-584`）→ `push_cursor` 照常推进 → 事件失去"未推送保护"（`sync_retention.py:53-61`）→ **仅当运维手动执行裁剪**（`server/scripts/prune_sync_events.py`，默认 90 天、默认 dry-run、`server/app` 内无任何调度器调用）才消失。
- **冲突留痕只有时间戳**：`SyncConflict` = entity/direction/reason/双时间戳（`database.py:1135-1150`，E5），**无内容列**；且存在**完全零留痕的覆盖窗口**（F10）。
- **UI 面**：「云端关联」面板冲突表格（`CloudLinkPanel.vue:199-218`）只显上述记录。**同步域 grep「冲突副本/conflict note」= 0**（E13）——本项目**没有冲突笔记副本机制**（不是没触发，是不存在）。用户在"冲突的笔记里"不可能找到内容；这直接回答「冲突的笔记里也完全没有」。

### 1.3 delete 是另一条通道（无 LWW）

- 删除**不看时间戳**："存在即删"（`sync_apply.py:273-291`，E1），且**应用删除不产生任何 SyncConflict**（`_apply_remote` 冲突预读仅 create/update，`sync_client.py:653-666`，E3）。后果见 F1/F2。

### 1.4 保护面（"不会丢"的机制——对抗评审补录，报告初稿遗漏）

1. **per-event SAVEPOINT**（`sync_client.py:603-606`、`sync.py:599-603`）：单条毒事件只回滚自身，不连坐前序。
2. **apply 失败留痕** `apply_error`（`sync_client.py:614-619`）：create/update 应用失败非零留痕。
3. **墓碑永不裁剪** + resync 先墓碑后快照（`sync_retention.py:18-20`、`sync_client.py:348-350`）。
4. **未推送事件 retention 保护**（`sync_retention.py:53-61`）：未编辑 payload 任何情况下不被按龄清掉。
5. **push 失败原位重试**（游标不动，`sync_client.py:585-586`）+ 单账号异常隔离（`:156-167`）。
6. **前端**：保存在途去重（`NoteEditor.vue:1400-1408`）· hasChanges 快照契约（`:1428-1445`，PUT 在途键入不误清脏）· 离场守卫二次拦截（`:1380-1384`）· 显式 `updated_at` bump（`notes.py:2996-2999`，堵死等值 STALE 丢改）。
7. **纯弱网编辑（无对端删除、运维未跑裁剪）= 双重持有（本地行+队列事件），原位重试至成功，不丢**。

---

## 2. 问题二：Evernote「弱网下本地编辑完全丢失（本地/云端/冲突笔记都没有）」可能的原因

外部资料（websearch 弱证据；exa 配额 402 已降级）：Evernote 论坛 "Old version of note synced over new one"（旧版覆盖新版）· "Note conflicts in iOS"（unsaved changes 状态机与实际内容不一致）· r/Evernote "Syncing issues and possible data loss?"（间歇、难复现=典型时序窗口缺陷）。

### 机制假说（共同结构：被丢的编辑从未获得持久身份）

| # | 假说 | 机制 | 为何"三处全无" |
|---|---|---|---|
| H1 | **保存未确认即清脏标志（ack-before-commit）** | 服务器/本地库确认前就标"已同步/已保存"；随后同步刷新把"干净的"服务器版本回灌本地库 | 编辑从未进持久层→本地无；从未上传→云端无；冲突副本只在"检测到双改"时生成→冲突里无 |
| H2 | **整篇替换式同步 + 冲突副本判据共享同一份同步账本** | "生成冲突副本"的条件（自上次同步以来双方都改）与"覆盖本地"的判定依赖同一份 last-sync/USN 账本；半途同步被标记完成或账本损坏后，服务器版本被当权威覆盖本地 | 账本一坏，覆盖发生而副本判据同时失效——**三处同时消失是同一根因的三个面** |
| H3 | **弱网半途失败 + 响应丢失后的回滚/重载** | 上传超时/响应丢失→客户端回滚到服务器版本，或重载笔记丢弃内存编辑 | 编辑只存在于请求体/内存缓冲，失败即蒸发 |
| H4 | **未保存缓冲随崩溃/重启/同步刷新蒸发** | 自动保存间隔（分钟级）内键入只在内存/临时草稿 | 从未落持久层→三处全无，且无任何记录 |

**为什么"冲突的笔记里也完全没有"**：冲突副本是**事后补救**，生成判据是"检测到双端都改"。丢失发生在"账本认为没冲突"（H1/H2）或"内容从未进入同步流"（H3/H4）时，补救层触发前提从未成立——不是副本功能坏了，是**它的判据从未满足**。

---

## 3. 问题三：当前项目会不会有这个问题

**会。** 按机制类（同步丢失）与未保存类（编辑器丢失）分列。每条：severity · dimension · file:line · why · PoC（证据摘录=`tests/evidence_note_sync_conflict_20261027.log`）。

### 同步机制类

#### F1 [Important · Bugs]（原评 Critical，评审降级）resync「删除权威」清队列 × delete 零留痕 = 本地未推送编辑**零留痕**蒸发

- **file:line**：`sync_apply.py:273-291`（E1）+ `sync_client.py:653-666`（E3，delete 零留痕）+ `sync_client.py:305-335`（E4，`_clear_queued_events` 无 op 过滤删事件、无 payload 归档）+ `sync_client.py:385`（墓碑循环无条件调用）。
- **why**：时序=①本地编辑 X（已落库+入队）②**对端删除 X**（云端墓碑）③ resync 被触发 → 墓碑先应用（本地行硬删）→ 清队列把携带 X 新内容的事件**直接删除**。终态：本地无（行删）、云端无（删除）、冲突记录无（delete 零留痕）——与症状逐字同构。评审 A 独立反证尝试全部失败（墓碑 payload 对 notes 恒 NULL `sync_capture.py:359-364`；`DeletionManifest` 明载不含正文；日志仅计数）→ **零留痕终态成立**。
- **触发链（评审修正，原稿"弱网特有"有误）**：需四重前置=**对端删除该笔记** + 本地有未推送修改 + resync 触发 +（若走缺口路径）服务端已裁剪事件。resync 的现实触发面：**手动"重新全量同步"**（`sync.py:279-305`）· 换绑/首连基线（`sync_client.py:206-207`）· delta 缺口 `resync_required`（`sync.py:558-563`——**缺口只能由服务端裁剪删行产生**，即设备离线超保留窗+运维跑了 prune；**瞬时弱网不产生缺口**）。弱网的准确角色=放大 push 失败概率（使①长期滞留未推送）。
- **加重事实**：`sync.py:283-284` 手动 resync 的 docstring 自称"不丢本地"——**文档在主动邀请用户踩此坑**（弱网事故后用户恰会点"重新同步"）。
- **PoC**：E1+E3+E4；对照组=同时序走 delta 路径不零留痕（见 F2）。
- **降级理由（评审 A/B 一致）**：机制唯一且恶性，但触发需多重前置叠加；对照 F4（同"三处全无"、触发面更宽）的一致性。**升级条款**：若部署侧存在 prune/resync 自动化（cron 裁剪、自动全量），触发面显著扩大→回升 Critical。修复建议 §6.1 仍应最高优先。

#### F2 [Important · Bugs] delete 通道无 LWW、无冲突留痕：本地带未推送修改的行被云端删除无条件抹掉

- **file:line**：`sync_apply.py:273-291`（E1）；`sync_client.py:653-666`（E3）。
- **why**：云端删除"存在即删"不比时间戳——本地未推送编辑随行蒸发且 delete 应用零留痕。delta 路径分支（评审 A 核实均属实）：(a) 本地待推 update 经 push → 服务端 upsert **复活**已删行（`sync_apply.py:350-363`，幽灵复活/分叉）；(b) 父 notebook 已删 → `parents_owned`（`:136-160`）拒 → `skipped_remote` 留痕后 cursor 照常推进（E7）。另有 FK CASCADE 连带抹除路径（父删子行）。两分支均不产生"冲突笔记"。
- **PoC**：E1+E3+E7。

#### F3 [Important · Bugs] LWW 整行覆盖、无冲突副本：用户视角「内容消失，冲突里没有」

- **file:line**：`sync_apply.py:365-381`（E2）；`database.py:1135-1150`（E5）；`sync_retention.py:53-61`（E6）；E13。
- **why**：双端同改、云端更新（含本地时钟偏慢）→ 本地行整行覆盖，编辑从本地消失；云端从未收到败方版本（push 已 STALE 拒收）；"冲突记录"仅时间戳。**限定（评审修正）**：败方全文留存于 `sync_events.payload`（JSONB，**SQL 可查回**，无任何用户可见 surface——`/api/sync/conflicts` 与笔记导出均不读它）；**仅当运维手动 `prune_sync_events.py --days N --execute`**（默认 90 天、默认 dry-run、无调度器、历史运营曾明令禁跑）后才消失。故严格表述="**用户可见三处无，DB 事件日志有**（直到手动裁剪）"。与 Evernote 差异：Evernote 有副本机制但判据失效；本项目**无副本机制**（E13）。
- **PoC**：E2/E5/E6/E13。

#### F10 [Important · Bugs]（评审发现的新缺口）跨周期覆盖零留痕窗口：`lww_remote_newer` 判据漏记

- **file:line**：`sync_client.py:686-695`（留痕条件 `local_ts > state.last_sync_at`）× `sync_client.py:225`（`last_sync_at` 每成功周期尾无条件盖章）。
- **why**（评审 A 推演，我方复核成立）：本地编辑 T1 于周期 N push 成功（服务端 APPLIED）→ 周期 N 尾章 > T1 → 周期 N+1 pull 到远端 T2>T1 整行覆盖 → `T1 > last_sync_at` 不成立 → **push/pull 双向均无任何 SyncConflict**。即"覆盖必留时间戳"不成立——**存在完全无记录的整行覆盖**，内容只剩 payload。这是"冲突的笔记里也完全没有（连记录都没有）"最贴近的机制面。积压场景（编辑产生于上周期、本周期才推出）同样漏记。
- **PoC**：`sync_client.py:686-695` + `:225` 条件推演（代码级，无运行时反例）。

### 未保存类（与同步机制区分——评审要求划界上提）

#### F4 [Important · Bugs] 未落盘编辑窗口：5 分钟自动保存 + ZenNotePanel 无草稿 + axios 无全局超时

- **定性（先划界）**：**属"未保存内容"的崩溃/挂死丢失，非同步机制丢失**（任何编辑器的通义面）；仅在与 Evernote 用户观感对照时同构。频率按触发面推断（未经测量）。
- **file:line**：`NoteEditor.vue:632`/`ZenNotePanel.vue:620`（`AUTO_SAVE_INTERVAL = 5min`，E9/E10）；NoteEditor 有 sessionStorage 草稿（`:1157-1178`）而 **ZenNotePanel grep=0**（E10）；`app/src/api/client.ts` 无全局 timeout（E16）。
- **why（评审 A 修正，比原稿更重）**：崩溃/强退（sessionStorage 随会话亡）→ 三处全无；工作台刷新（ZenNotePanel 无草稿）→ 三处全无；弱网 PUT 挂死时 `savePromise` **永不 settle → 一切后续保存楔死**（共用同一在途 Promise，请求体发出时已定格，`NoteEditor.vue:1400-1408`/`ZenNotePanel.vue:1668-1673`）→ 此时退出=三处全无。
- **PoC**：E9/E10/E16。

### 次级发现（Minor）

#### F5 [Minor · Bugs]（原评 Important，评审降级）skipped_remote 无重发自愈通道
- `sync_client.py:569-586`（E7）：skipped 只留痕、cursor 照常推进；`_push` 只取 `seq > push_cursor`，手动 resync 只清 pull_cursor，换绑只前移 push_cursor → 确无重发通道（评审 A 核实）。
- **降级理由**：原稿"本地保留内容、云端永久分叉"被反证——同周期 pull 即把远端版本覆盖本地（无持久分叉），STALE=严格旧时间戳重发也必再败，剩余危害窄（归属拒绝/时钟漂移修复面）。**明示：F5 不丢内容**（本地行保留），是云端可见性/设计缺口，不与 F1/F3 并列计数。

#### F6 [Minor · Bugs]（上游缺陷，本仓已修）上游编辑器无 hasChanges 快照契约：PUT 在途键入被误标干净而丢改
- 上游 `NoteEditor.vue:1374-1385`、`ZenNotePanel.vue:1313-1330`（E11/E12）：`await updateNote(...)` 后无条件 `hasChanges=false; clearDraft()`。本仓已修：`NoteEditor.vue:1428-1445` **及 `ZenNotePanel.vue:1696-1708`**（评审 A 补证，原稿只引 NoteEditor）。弱网 PUT 慢把丢改窗口放大。上游未修；同步波有吞契约回归风险。

#### F7 [Minor · Bugs]（归属纠正：C 共享镜像**双侧同在**，非"上游自有"）WysiwygEditor 待传图片 flush 15s 超时放行
- 上游 `WysiwygEditor.vue:4410-4419`（`Promise.race([pending, setTimeout 15000])`）**与本仓镜像 `WysiwygEditor.vue:4529-4536` 逐字同款**（评审 A 纠正）。
- **评审 REFUTED 部分**：单图失败**不**内吞——会 `emit('image-upload-failed')` 且两仓编辑器均 toast"图片上传失败，保存后将丢失，请重试"。真正静默的只有 **15s 挂死超时放行**路径（Promise 永不 settle→race 超时→保存继续→blob 图丢失无提示）。

#### F8 [Minor · Bugs] content 可写 NULL（列无 NOT NULL）
- `database.py:705-716`（E8）；agent 工具 `tools/notes.py:463-464` `args["content"]=None` 原样写入（`_maybe_render_note_citations` 透传 None）。窄面（agent 工具路径）。

#### F9 [Minor · Bugs] 标题清不掉（反向症状，同写点契约族）
- `NoteEditor.vue:1433`（`title: savedTitle || null`）+ `api/notes.py:2990`（`if note_data.title is not None` 跳过）；**`ZenNotePanel.vue:1700` 同款写点**（评审 A 补证）。

#### F11 [Minor · Bugs]（评审 B 发现的新窄口）切笔记路径草稿恢复被立即抹脏
- `NoteEditor.vue:1188-1196` 恢复草稿置 `hasChanges=true` 后，`:1200` 无条件 `hasChanges.value = false`（watch 回调层、`if (note.value)` 块外）→ 存在"未入库但 UI 显示干净"的一条窄路径（onMounted 路径 `:1237-1244` 无此问题）。脏态账本非无懈可击的实证。

### 3.1 与症状的映射

| 症状要素 | 本仓对应 | 限定 |
|---|---|---|
| 内容不在本地 | F3/F10（覆盖）· F1/F2（行删除）· F4（未落盘） | F4=未保存类 |
| 内容不在云端 | F3/F10（push 被拒/从未收到）· F4（从未入队） | F5 仅可见性，不丢 |
| 冲突笔记里也没有 | **结构性必然**（E13 无副本机制）+ F10 零留痕覆盖 + delete 零留痕（E3） | `sync_conflicts` 仅时间戳（E5） |
| 弱网下发生 | F4（PUT 挂死楔死）· F7（15s 放行丢图）· F1（弱网使未推送编辑滞留，配合对端删除+resync） | 弱网**不**产生 resync 缺口（评审修正） |
| 难复现 | 全部为时序窗口型 | F3 全文在 payload（SQL 可取回） |

---

## 4. 问题四：当前项目的上游代码会不会有这个问题

**会，分层回答**（上游本体=`/home/user/Documents/DEV/chatbot`，只读红线，未动）：

| 层 | 归属 | 上游是否有同类问题 | 证据 |
|---|---|---|---|
| 仲裁语义（LWW 整行覆盖、无冲突副本、delete 无 LWW） | **红线 B，双端 byte-identical** | **有，且是设计本身**（F1/F2/F3/F10 语义根源） | E1/E2/E14 |
| 冲突留痕（仅时间戳、delete 零留痕） | 红线 B schema + worker 侧留痕 | **有**（schema 同源）；delete 零留痕实现面在 `sync_client._apply_remote`（本仓独有），服务端等价行为=删除应用不产生记录（E1 路径） | E3/E5/E14 |
| resync 清队列（F1） | A client-only（`sync_client.py`） | **上游无此代码**（E15=0）；上游 Web 直连用户的等价面=F3/F10 覆盖无副本 + F2 删除无留痕 | E4/E15 |
| 编辑器保存契约（F4/F6/F11） | C 共享镜像 | **有**：上游无快照契约（E11/E12）；5 分钟自动保存上游同款；ZenNotePanel 无草稿上游同款 | E9-E12 |
| 图片 flush（F7） | C 共享镜像 | **有，双侧逐字同款**（非上游独有） | E17 + 评审 A |

> 一句话：**"编辑会静默消失"的机制面（LWW 无副本 + delete/覆盖零留痕）与编辑器面（在途丢改、丢图）上游都在**；F6 本仓已修/上游未修；F1 清队列缺口是本仓 worker 独有（上游无 worker，等价面在 F2/F3）。

---

## 5. [归属判定]（docs/DECISION_MATRIX_local_vs_upstream.md §2 模板）

```
[归属判定]
- 运行时: 双端（本地侧车+worker / 云端服务端仲裁与 Web 端）
- 文件类别: B 红线同源（sync_apply/sync_capture/SyncConflict schema）+ A client-only（sync_client）+ C 共享镜像（NoteEditor/ZenNotePanel/WysiwygEditor）
- 结论（按发现）:
  · F1 resync 清队列零留痕 → 仅本地（sync_client.py，A 类；补留痕不动仲裁语义）；sync.py:283-284 误导 docstring → 双端（同步 API 面=红线 B 文档口径）
  · F2 delete 无留痕 → 留痕修复=仅本地（sync_client._apply_remote）；改 delete 仲裁语义（删除撞未推送编辑生成副本）= 双端（红线 B）
  · F3/F10 冲突副本/败方快照/留痕判据 → 双端（schema+仲裁语义红线 B，两端 byte-identical；F10 的 last_sync_at 判据在 sync_client=仅本地可修）
  · F4 未落盘窗口 → 仅本地可修（app/src UX）；NoteEditor/ZenNotePanel 属共享镜像→上游同步波 conflict，建议上游同修或登记常驻本地补丁
  · F5 重发自愈通道 → 仅本地（sync_client.py）
  · F6 上游快照契约 → 仅上游+部署60（本仓已修可反哺）
  · F7 图片 flush → 双端（C 共享镜像逐字同款，建议上游同修）
  · F8 content NULL → 双端（tools/notes.py + schema 红线）
  · F9/F11 → F9 双端（共享镜像写点+notes.py 跳过语义）；F11 仅本地可修
- 交付动作: 无（本波仅评估）。若立项：F1/F5/F10/F11 仅本地重打包 DMG · F3/F8 双端同步波+deploy_60+重打包 · F6 上游 handoff · F7/F4 双端或登记本地补丁
- 验证面: F1/F2/F5/F10=server/tests 同步域单测+时序集成（含 resync/删除/跨周期覆盖场景）· F3=双端仲裁一致性（红线 B 全等）· F4/F11=app/tests/workflows+UI E2E · F6/F7=上游测试
```

---

## 6. 修复方向建议（仅建议，未实施——用户指令"仅评估，不修改"）

1. **F1 最小修复（仅本地，最高优先）**：`_clear_queued_events` 删除事件前把被清事件落 `SyncConflict`（新 reason 如 `dropped_by_delete`）或"被清除的本地版本"留档——零留痕→可追溯；并纠正 `sync.py:283-284`"不丢本地"误导 docstring（双端口径）。
2. **F3/F10 冲突副本或败方快照（双端）**：Joplin 式冲突副本笔记，或 `SyncConflict` 增 title/content 快照列（截断）；**F10 判据修复**（`lww_remote_newer` 的 `local_ts > last_sync_at` 窗口，仅本地可先修）。
3. **已存在恢复面先用起来**：prune 执行前败方内容可经 `sync_events.payload` 直查恢复——最低成本补救可为"裁剪前导出被裁事件快照再删"而非新表。
4. **F5 重发自愈（仅本地）**：`skipped_remote` 事件进重发候选，下次编辑前比对远端 `updated_at` 决定重发/确认放弃（决策矩阵 Q3 既定原则）。
5. **F4 保存面**：自动保存 30–60s + `beforeunload` 落盘 + ZenNotePanel 对齐 NoteEditor 草稿 + axios 全局 timeout（根治 PUT 挂死楔死）。
6. **F6/F7 上游 handoff**：快照契约、图片 flush 挂死路径（15s race 的 catch 应显式失败而非放行）反哺上游。

## 7. 证据索引

`tests/evidence_note_sync_conflict_20261027.log`：E1 delete 无 LWW · E2 LWW 整行覆盖 · E3 delete 零留痕 · E4 清队列 · E5 SyncConflict 仅时间戳 · E6 retention 保护/裁剪 · E7 push cursor/skipped · E8 Note schema · E9 本仓编辑器契约 · E10 ZenNotePanel 无草稿 · E11/E12 上游编辑器缺陷 · E13 冲突副本 0 命中 · E14 shasum 同源 · E15 上游无 sync_client · E16 axios 无超时 · E17 图片 15s 放行。
评审独立复核增补：`server/scripts/prune_sync_events.py`（手动/90 天/dry-run）· `sync.py:558-563`（缺口=裁剪删行）· `sync.py:283-284`（误导 docstring）· `NoteEditor.vue:1200`（抹脏窄口）· 本仓 `WysiwygEditor.vue:4529-4536`（F7 双侧同款）· `ZenNotePanel.vue:1696-1708`（快照契约亦已修）。

## 8. 评审与裁决记录

两个 fresh-context 独立评审（详见 `tests/fcv_report.md`）：
- **评审 A（核实型）**：机制逐环 CONFIRMED（F1/F2/F3/F4/F8/F9），反证尝试均失败处=零留痕终态/复活分支/parents_owned/草稿缺失；命中修正=F1 触发链（缺口须服务端裁剪）、F5 降级（同周期 pull 覆盖、无持久分叉）、F7 归属（双侧同款）+失败有 toast、F3 加重（F10 漏记窗口）。
- **评审 B（对抗型）**：判定 **mostly-sound-with-overclaims**；命中=裁剪手动/dry-run/无调度器（F3 限定）、gap 仅由裁剪产生（F1 事实错误）、保护面缺录（§1.4 补齐）、F4 划界、F11 窄口、F5 不属丢失面。
- **本方裁决（AUTO）**：全部采纳；F1 Critical→Important（升级条款入档）、F5 Important→Minor、F7 归属纠正并降述、F3 补限定+新增 F10、F4 划界上提、新增 F11、§1.4 保护面补录。**零源码改动**（`git diff` 仅审计产物）。
