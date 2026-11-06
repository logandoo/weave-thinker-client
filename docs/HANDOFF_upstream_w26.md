# 上游 handoff ④：W26 插话重复气泡 + 拖拽卡死修复回传 + echo 通道缺陷（chatbot 仓待办）

> **背景**（用户报障 2026-10-17，客户端 W26 波已修完，基线 `976b17b` → 终态 `8bfeb04`）：
> ① 插话只发一次却显示两条 query 气泡；② agent 模式 sidebar 拖 session 入分组卡死 +
> 拖拽时所有折叠分组自动全展开；③ 客户端执行未做云端/本地区分（客户端层已解决，见 §C）。
>
> 本文三部分：**A = 象限 C 共享镜像修复的逐字回传**（`frontend/src` ↔ `app/src`，上游采纳后
> 客户端下波同步直采、本地 hunk 销账）；**B = 上游自有缺陷**（本波调研查实、客户端未改，
> 是 ① 的服务端侧根因，需上游决策）；**C = 客户端专属，明确不回传**。
>
> **红线**：`/home/user/Documents/DEV/chatbot` 对本仓只读——本清单交付用户在 chatbot
> 会话执行。行号为客户端 `app/src` 镜像实测位点，上游行号可能漂移，**以函数名定位**。
> 判定依据：`docs/DECISION_MATRIX_local_vs_upstream.md` §3（本波 5 行登记，A 部分采纳即销账）。

---

## A. 前端修复回传（`frontend/src`，逐字可用）

### A1. `stores/chat.ts` — 插话重复气泡根治（D-62 同族根治版）

**根因链**：run 终态时 `flushUnconfirmedInterjections('auto-send')` 与 `refreshConversation`
**并行**触发。commit-echo（`interjection_committed`）不把服务器行插入 `messages`——去重门
`_findCommittedInterjection` 扫的是**刷新前的本地列表**，把「echo 丢失但服务端已落库」的
插话误判「未确认」→ `sendMessage` 再落一行 → 双气泡。评审曾尝试在 flush 内部
`await refreshConversation`，但被 **`refreshSeqs` 最后写获胜**机制作废（竞对 refresh 后发
→ 本 flush 的快照被丢弃 → 判重仍扫旧列表 = 修复失效）。

**修法 = 两个不变量**：
1. auto-send 冲刷**绝不与竞对 refresh 并行**——调用点全部重排为「先 refresh（应用权威
   快照）后 flush」串行；
2. flush 内部刷新用 `{ reuseFresh: true }`（`refreshConversation` **既有**选项，基线 :675）
   ——TTL 内直接复用调用方刚应用的快照（不发新请求、不 bump seq），无竞对路径才真拉取。

**hunk 1 — `flushUnconfirmedInterjections`（本地 :3148）**，函数体开头、drain 之前插入：

```ts
    if (mode === 'auto-send') {
      // 重复气泡根治：auto-send 冲刷前确保判重门扫的是权威快照。
      // commit-echo 不把服务器行插入 messages —— 去重门扫刷新前的本地列表
      // 会把 echo 丢失但服务端已落库的插话误判「未确认」→ sendMessage 再落
      // 一行 → 双气泡。reuseFresh：调用点已重排为「先 refresh 后 flush」
      // （裸 refresh 会被紧随的竞对 refresh 以 refreshSeqs 最后写获胜作废，
      // 快照永不应用=修复失效）；TTL 内直接复用已应用的快照，无竞对
      // （syncAfterAbort/selectConversation 路径）时才真拉取。
      try {
        await refreshConversation(convId, { reuseFresh: true })
      } catch {
        // 刷新失败：保持既有本地快照判重语义
      }
    }
```

**hunk 2 — 主发送流 `onDone` 回调**（本地 :1906）：**删除**回调体内的
`void flushUnconfirmedInterjections(conversationId, 'auto-send')`（不替换为任何调用）。

**hunk 3 — 主发送流 promise 结算后**（本地 :2592，`await refreshConversation(conversationId)`
之后、`catch` 之前）追加：

```ts
      // 自然终态的插话冲刷锚定在此处（权威 refresh 已应用，判重门扫
      // 的是服务器最新行）——onDone 回调内的并发 refresh 已被本调用吸收
      // （refreshSeqs 最后写获胜），flush 内 reuseFresh 直接复用本快照。
      await flushUnconfirmedInterjections(conversationId, 'auto-send')
```

**hunk 4 — 四处「auto-send 冲刷与 refresh 并行」改为串行**：
resume `onDoneCallback`（本地 :1150）、resume `onError` 非当前会话腿（:1242）、
resume `'none'` 非当前会话腿（:1279）、主发送流 `onError` 非当前会话腿（:1921）——
四处形态相同：

```ts
// 旧形态（并行，判重快照会被作废）：
//   void flushUnconfirmedInterjections(conversationId, 'auto-send')
//   void refreshConversation(conversationId)
// 新形态（串行）：
        if (conversationId === currentConversationId.value) {
          void flushUnconfirmedInterjections(conversationId, 'restore')
          void refreshConversation(conversationId)
        } else {
          // 后台会话 auto-send 必须先 refresh 再 flush 判重
          void (async () => {
            await refreshConversation(conversationId)
            await flushUnconfirmedInterjections(conversationId, 'auto-send')
          })()
        }
```

注意 resume `onDoneCallback` 处原本两行都无 `currentConversationId` 分支（resume 只跑在
当前会话语义下），直接换成串行 IIFE 即可（restore 腿语义不变，勿引入分支）。

**语义红线**：
1. `restore` 模式（用户主动停止→回填输入框）**不得**触发刷新——刷新必须在
   `mode === 'auto-send'` 守卫内、且在 restore 提前 return 之后。
2. 刷新失败**不得阻断**冲刷（宁发不丢语义不退化）——try 包裹。
3. 全局搜索确认并行形态绝迹：`void flushUnconfirmedInterjections(...'auto-send')` 相邻
   `void refreshConversation` 的组合不得存在（守卫测试 R4d 即此断言）。

### A2. `components/SortableList.vue` — 拖拽卡死根治

**根因**：sortablejs@1.14.0 在拖拽中 `destroy()` → `_onDrop(undefined)` → `_nulling()` 清空
**模块级全局**拖拽态（`dragEl`/`tapEvt`/`Sortable.active`）→ `onEnd` 永不派发 → 上层
`isDragging` 永真 → 拖拽卡死且「拖拽中」CSS 永驻。上游 Vue.Draggable#1199 同源缺陷。
三个触发点：内联 `:group` 对象引用不稳（props.group watcher 重建）、拖拽中 CSS 全展开
触发 ResizeObserver 重建、悬停展开 forceReinit。

**hunk 1 — `draggedItemStore` 移模块级 + 导出复位出口**。当前声明在
`<script setup>` 顶层 = **每实例一份**（注释声称全局不实），跨实例守卫全部失效。改为
SFC 双 script 块：

```vue
<script lang="ts">
// 拖拽全局态必须是**模块级**单例 —— <script setup> 顶层=每实例一份，
// 跨实例 destroy 守卫全失效，且 failsafe 无从复位。模块级才与 SortableJS
// 的模块级 _nulling 全局态对齐。
const draggedItemStore: { item: any | null; sourceKey: string | null; itemById: Map<string, any>; isDragging: boolean } = {
  item: null,
  sourceKey: null,
  itemById: new Map(),
  isDragging: false,
}

/** failsafe 复位出口：SortableJS 的 end 事件未到达时（destroy-during-drag
 *  等）onEnd 的清理也不会跑 —— Sidebar 的 forceEndDrag 经此复位全局态，
 *  否则 isDragging 永真会永久锁死全部实例的 re-init 与变更持久化。 */
export function resetGlobalDragState() {
  draggedItemStore.isDragging = false
  draggedItemStore.item = null
  draggedItemStore.sourceKey = null
  draggedItemStore.itemById.clear()
}
</script>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch, nextTick } from 'vue'
import Sortable from 'sortablejs'
// …（其余 setup 内容不变，删除原 draggedItemStore 声明）
```

**hunk 2 — `initSortable()` 顶部扼流守卫**（`if (!containerRef.value) return` 之后）：

```ts
  // 拖拽卡死根治（扼流守卫）：拖拽进行中禁止重建已有实例 ——
  // SortableJS 1.14.0 destroy() → _onDrop(undefined) → _nulling() 清空
  // **模块级全局**拖拽态，onEnd 永不派发，isDragging 永真。
  // destroy 的唯一合法时机 = 非拖拽期与组件卸载。尚无实例的懒初始化
  // 列表（折叠组首次展开）允许纯创建 —— 无 destroy 即无 _nulling。
  if (draggedItemStore.isDragging && sortableInstance) return
```

**hunk 3 — `watch(() => props.forceReinit, …)` 移除自带 destroy**（守卫收敛进
initSortable，且旧守卫只护**源列表**——`_nulling()` 清的是全局态，毁目标列表同样杀死
在途拖拽）：

```ts
watch(() => props.forceReinit, () => {
  if (lazyObserver) {
    lazyObserver.disconnect()
    lazyObserver = null
  }
  nextTick(() => initSortable())
})
```

### A3. `components/Sidebar.vue` — 稳定引用 + failsafe

**hunk 1** — 模板 :268 内联 `:group="{ name: 'groups', pull: false, put: false }"` →
`:group="groupsDragGroup"`，script 中：

```ts
// groups 目录拖拽组引用必须稳定 —— 内联对象字面量每次渲染都产生新引用，
// 触发 SortableList 的 props.group watcher 重建实例；拖拽中重建 = 卡死。
const groupsDragGroup = { name: 'groups', pull: false, put: false }
```

**hunk 2** — import 改 `import SortableList, { resetGlobalDragState } from './SortableList.vue'`。

**hunk 3** — failsafe 块（插在 `const collapsedBeforeDrag = …` 之后，逐字见客户端
`app/src/components/Sidebar.vue` :2053-2122）。要点：

```ts
let dragGeneration = 0  // 世代令牌：stale 计时器不得扼杀新拖拽

function onDragFailsafePointer(e?: Event) {
  // 多点触控：第二根手指抬起≠拖拽结束（SortableJS 只跟踪首触）
  if (e && 'touches' in e && (e as TouchEvent).touches.length > 0) return
  const gen = dragGeneration
  window.setTimeout(() => {
    if (gen !== dragGeneration) return
    if (isDragging.value) forceEndDrag('pointer-released-without-end-event')
  }, 150)
  removeDragFailsafeListeners()
}
function onDragFailsafeKey(e: KeyboardEvent) {
  if (e.key !== 'Escape') return
  if (!isDragging.value) return
  removeDragFailsafeListeners()
  forceEndDrag('escape')
}
function forceEndDrag(reason: string) {
  if (!isDragging.value) return
  document.body.style.userSelect = ''
  document.body.style.cursor = ''
  resetGlobalDragState()               // end 未达时 onEnd 的全局态清理也缺失
  const isCancel = reason === 'escape' // Escape=取消：不 reconcile、不 persist
  if (!isCancel) { deduplicateDragElements(); reconcileDragState() }
  isDragging.value = false
  if (!isCancel) { processDragChanges() }
  // …stopEdgeScroll / removeEventListener / cancelDragMoveRaf / hover 计时器
  //  / groupsDirectoryWasCollapsed 还原 / nextTick(syncDragLists) 全量清理，
  //  逐字见客户端文件。
}
```

**hunk 4** — `onDragStart` 开头：`dragGeneration += 1` + 注册三个 capture 兜底监听
（`window` `mouseup`/`touchend`/`keydown`）。

**hunk 5** — `onConversationDragEnd` 与 `onGroupDragEnd` **函数体第一行**：

```ts
  // failsafe 已复位后迟到的真实 end 不得二次 reconcile+persist
  // （Escape 取消后松手会把会话落到光标所在处）。
  if (!isDragging.value) return
```

并在两函数的正常清理段各加 `removeDragFailsafeListeners()`。

**hunk 6** — `onUnmounted` 加 `removeDragFailsafeListeners()` + 拖拽期 document 监听摘除。

### A4. `styles/sidebar-panels.css` — 折叠组拖拽中=放置条（不再全展开）

`.sidebar-dragging .group-convs-collapsed` **删除** `grid-template-rows: 1fr !important`
（这行把每个折叠组的全部 session 渲染出来：列表爆炸、目标组被挤出可视区，且经
ResizeObserver 触发 A2 的重建风暴），并新增内层命中区规则：

```css
/* 折叠组保持 0fr 折叠，仅呈现固定高度放置条；悬停 300ms 的目标组由
   dragExpandedGroupId 机制单独展开（Sidebar.vue 移除该组的折叠类）。 */
.sidebar-dragging .group-convs-collapsed {
  opacity: 1 !important;
  overflow: hidden !important;
  padding: 0 !important;
  min-height: 44px !important;
  border: 2px dashed var(--color-primary) !important;
  border-radius: var(--radius-sm) !important;
  margin: 4px 0 !important;
  background-color: color-mix(in srgb, var(--color-primary) 5%, transparent) !important;
}

/* 内层拖拽列表须有真实可视高度（grid 0fr 行钳制下靠 min-height 撑出
   44px 命中区），超出的 session 行由容器 overflow:hidden 裁剪。
   注意选择器优先级：必须 > .sidebar-dragging .group-conv-drag-list（24px
   旧规，同权重且位置靠后会覆盖），故用子代组合器加一级 class。 */
.sidebar-dragging .group-convs-collapsed > .sortable-list {
  min-height: 44px !important;
}
```

### A5. `components/ZenHistoryDrawer.vue` — 同类残留

内联 `:group="{ name: 'zen-groups', pull: false, put: false }"`（:131）→ 稳定常量
`const zenGroupsDragGroup = { name: 'zen-groups', pull: false, put: false }`。
（该组件的 store 级全展开与 failsafe 缺失**未**在本波处理，属后续波次。）

### A6. 守卫测试（`frontend/tests/workflows`，逐字复制客户端文件即可跑）

| 文件 | 钉住的不变量 |
|---|---|
| `test_interjection_flush_refresh.cjs` | R1-R3 flush 内 reuseFresh 时序 + **R4a-d 调用点串行形态**（并行形态绝迹断言） |
| `test_sortable_drag_guard.cjs` | 扼流守卫 / draggedItemStore 模块级 / resetGlobalDragState / 稳定引用 / failsafe 三件套 / end 守卫 / CSS 放置条（31 项） |

并接入 `frontend` 的 `test:unit` 链（客户端为 `app/package.json` 追加两条 `node …`）。

---

## B. 上游自有缺陷（客户端本波查实、未改——重复气泡的服务端侧根因）

### B1. `backend/app/services/active_agent_registry.py` — broadcast drop-oldest 无差别丢契约事件 — **P1**

- **现象**：`broadcast()`（:100-124）订阅队列（maxsize=512）满时 drop 队首 backlog 保新事件。
  `interjection_committed` echo 与普通 delta 同队列同权重——慢客户端恰好可能在 commit
  瞬间丢掉这条**唯一确认信号**（丢了没有任何补偿通道，见 B2）。
- **证据**：`active_agent_registry.py:100-124`；事件产生点 `api/chat.py:3168-3205`
  （commit 即落库 `role=user` 行 + `_put` 广播）。
- **建议修法**：契约事件（`interjection_committed`/`done`/`error`）不可丢——队列满时优先
  淘汰最旧的 delta 类 backlog；无可淘汰项则将该订阅者标记 lagged 并断开（客户端走
  resume，前提是 B2 落地使 replay 能补 echo）。

### B2. `backend/app/services/stream_buffer.py` — resume replay 载荷不含插话 echo — **P1**

- **现象**：`_put` 先写 buffer 再广播（buffer 是 resume-replay 权威），但 `append()` 对
  `interjection_committed` **无归一化分支**（全文 0 处 interjection），`to_replay()` 各字段
  （content/display_sequence/…）均不含它 → **断连期 commit 的插话 echo 永久丢失**；重连
  后前端 pending 条目永不确认 → 终态冲刷重发 → 双气泡。这是「echo 丢失」的主通道。
- **证据**：`stream_buffer.py`（append 分派 :266 起 / to_replay :171-222）·
  `frontend/src/api/streamDispatch.ts` `ReplayPayload`（无插话字段）·
  `api/chat.py:4860 resume_stream` 消费 `subscribe_with_snapshot()`。
- **建议修法**：`ConversationBuffer` 增 `interjections_committed: List[dict]`，`append()`
  识别 `interjection_committed` 入列，`to_replay()` 暴露同名字段；前端 `ReplayPayload`
  增该字段，resume 时把匹配条目 `committed: true` 并把 id 登记入 `_echoConfirmedCommittedIds`
  （A1 的冲刷判重即可凭权威快照去重，双保险闭合）。

### B3. `frontend/src/stores/chat.ts` — `onInterjectionCommitted` 按 content 配对 + 无连接令牌守卫 — **P2**

- **现象**：echo 确认按 `pend.findIndex(p => p.content === payload.content)`（本地 :2050）
  ——同会话同文两条插话会错标第一条；handler 无 `s.abortController !== connection` 令牌
  守卫（onError/onDone 均有），旧连接迟到的 echo 会确认新连接的 pending 条目。
- **建议修法**：插话 POST（`/chat/stream/interject/{id}`）请求体带客户端生成的
  `client_message_id`，服务端在 `interjection_committed` 载荷回带，前端按 id 配对；
  handler 加连接令牌守卫。B2 落地后此项降为精度问题。

### B4. D-62 原项：caught-up 锚假阳性 — **P2**（沿用旧清单）

`docs/upstream/UPSTREAM_TODO_20261006.md` §1（`_recoveryAnchor` 回退上一轮 user 行 →
`caughtUpAfterAnchor` 假阳性 → `syncAfterAbort` Phase-1 误判收线）。**A1 与本项兼容**
（A1 只保证冲刷判重扫权威快照；锚语义仍按原清单修）。

---

## C. 客户端专属——**不**回传上游

| 件 | 原因 |
|---|---|
| `desktop/main.cjs` / `desktop/preload.cjs`（`--weave-exec-mode=local` 注入链） | 上游无 `desktop/` |
| `app/src/api/base.ts` `EXECUTION_MODE` | 上游 `frontend/src/api/` 无此文件（已核实），属客户端 API 基址协商基础设施 |
| `app/src/components/ChatInput.vue` 空目标文案「本地（本机）/云端服务器」分流 hunk | **上游勿直采**——import `@/api/base` 在上游不存在，直采即编译失败；登记为本地长期 hunk（DECISION_MATRIX §3 已录） |
| `test_exec_mode_wiring.cjs` / `tests/ui_sidebar_drag.py` / `tests/ui_exec_mode.py` | 断言对象含 desktop/* 与客户端 Playwright 卷宗 |

---

## 验证方法（上游采纳 A 后）

1. **A1**：跑 A6 守卫测试。手工场景：run 进行中发插话 →（模拟 echo 丢失：DevTools 断网
   数秒令 resume 接管）→ run 终态后该插话文本的用户气泡**恰一条**。
2. **A2-A5**：建一个 30+ session 的分组并折叠，拖另一 session 向其拖动——
   修复前：拖起瞬间全部折叠组展开、松手后拖拽态卡死（幽灵行+禁选永久）；
   修复后：折叠组仅呈现 44px 虚线放置条、松手落组、拖拽中按 Escape 取消（不落组、
   UI 还原、不发 reorder 请求）。
3. **B1/B2**（修复前复现=确认根因）：慢客户端（另一 tab 限速）+ 插话 commit 瞬间断连
   → resume 后终态冲刷重发插话=双气泡；B2 落地后同场景 resume 即确认、不重发。

## 与本地仓的收口关系

- A 部分上游合入后，客户端下波同步直采并**销**`docs/DECISION_MATRIX_local_vs_upstream.md`
  §3 的 W26 对应行（chat.ts / SortableList / Sidebar / css / ZenHistoryDrawer）。
- B 部分若上游修复 echo 通道（B1+B2），A1 的 reuseFresh 冲刷仍保留（双保险，无害）。
- 客户端侧证据卷宗：`tests/fcv_report.md`、`tests/ui_sidebar_drag.trace.log`、
  `tests/screenshots/sidebar_drag_*.png`、`memory/fix_w26_interject_drag_execmode_20261017.md`。

---

> **收口（2026-10-24 W27 同步波实证）**：A1-A6 已被上游采纳（A1 逐字+增强，守卫 test_interjection_flush_refresh R4e 反为上游超集；A2-A5 增强版全数入 vendor）；B1-B4 echo 通道上游已修（test_w26_echo_channel 随波直采，27/27）；C 清单（desktop/* · base.ts EXECUTION_MODE · ChatInput 模式分流）按约未回传且合并中全保留。§3 W26 对应行已销账。
