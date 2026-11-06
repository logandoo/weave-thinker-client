# PLAN — W26 插话重复气泡 + Sidebar 拖拽卡死 + 云端/本地执行客户端分层

> 2026-10-17 · Lane L · Class: CODE · 基线 commit `976b17b`（backup: before changes，app test:unit 全绿 exit=0）

## Consistency Hub

| entity | canonical value/type | source of truth |
|---|---|---|
| 插话待确认队列 | `pendingInterjections: Record<convId, {localId, content, committed?, assistantId?}[]>` | app/src/stores/chat.ts:142 |
| 插话去重门 | `_findCommittedInterjection(convId, text, boundaryId, drainTailId, echoConfirmed, excludeIds)` | app/src/stores/chat.ts:3054 |
| 终态冲刷 | `flushUnconfirmedInterjections(convId, 'auto-send'\|'restore', assistantId?)` | app/src/stores/chat.ts:3126 |
| 权威刷新 | `refreshConversation(convId, opts?): Promise<boolean>` | app/src/stores/chat.ts:675 |
| 拖拽全局态 | `draggedItemStore.isDragging`（模块级单例） | app/src/components/SortableList.vue:16 |
| 侧边栏拖拽态 | `isDragging: Ref<boolean>` → `.sidebar-dragging` 类 | app/src/components/Sidebar.vue:2040 |
| 悬停展开目标 | `dragExpandedGroupId: Ref<string\|null>` | app/src/components/Sidebar.vue:2420 |
| groups 目录拖拽组 | 稳定常量 `groupsDragGroup = { name: 'groups', pull: false, put: false }`（本波新建，替换 :268 内联对象） | app/src/components/Sidebar.vue |
| 执行模式 | `EXECUTION_MODE: 'local' \| 'cloud'` | app/src/api/base.ts（本波新增导出） |
| 壳注入 | `window.weaveRuntime.execMode`（'local'\|'' —— 仅本地栈实启成功时 'local'） | desktop/preload.cjs / desktop/main.cjs |

## Task 1 — 插话重复气泡（判据 52）

**Root cause (A4.6)**: `flushUnconfirmedInterjections` 在 onDone / resume-onDone 中先于 `refreshConversation` 执行；commit-echo 不把服务器行插入 messages → 去重门扫的是刷新前本地列表 → 已落库插话被判「未确认」→ `sendMessage` 再落一行 → 双气泡。echo 丢失通道（subscriber drop-oldest / 断连期 replay 不带插话事件）放大。上游已 parked 为 D-62。

- **Files**: Modify `app/src/stores/chat.ts`；Test `app/tests/workflows/test_interjection_flush_refresh.cjs`（新）
- **Interfaces**: Consumes `refreshConversation(convId)`, `_drainUnconfirmedInterjections`, `_findCommittedInterjection`（均既有，签名不动）；Produces 无新导出
- **Steps**:
  1. RED：写 `test_interjection_flush_refresh.cjs`——桩化 store 场景：pendingInterjections 含未确认条目 + 服务端已落库同文本行仅在 refresh 后可见 → 断言 auto-send 冲刷后 `sendMessage` 未被调用（旧代码=调用了，RED）。验证命令：`node --experimental-strip-types app/tests/workflows/test_interjection_flush_refresh.cjs`
  2. GREEN：`flushUnconfirmedInterjections` auto-send 分支在 drain/快照之前 `await refreshConversation(convId)`（刷新失败时保持旧语义宁发不丢？——否：刷新失败=快照不可信，仍走既有门判定，记录诊断）。
  3. 全量 `npm run test:unit --prefix app` 绿。
- **Test seam**: app/tests/workflows 既有 .cjs 模式（node 直跑，桩 import）。

## Task 2 — Sidebar 拖拽卡死 + 全展开（判据 53/54/55）

**Root cause (A4.6)**: SortableJS 1.14.0 `destroy()` → `_onDrop(undefined)` → `_nulling()` 清模块级全局拖拽态（上游 issue Vue.Draggable#1199 同源）。三个拖拽中 destroy 触发点：①Sidebar.vue:268 内联 `:group` 对象每渲染新引用 → SortableList.vue:289 watcher（无守卫）re-init；②折叠组拖拽中 CSS 全展开 → 高度 0→N → ResizeObserver（:99，无守卫）re-init；③悬停 300ms `forceReinitCounter++`（:2422）→ 目标列表重-init（:295 守卫只护源列表）。任一触发 → `end` 永不派发 → `isDragging` 永久 true → `.sidebar-dragging` 永驻 → CSS 全展开（sidebar-panels.css:307 `grid-template-rows: 1fr`）永久化。

- **Files**: Modify `app/src/components/SortableList.vue`、`app/src/components/Sidebar.vue`、`app/src/styles/sidebar-panels.css`；Test `app/tests/workflows/test_sortable_drag_guard.cjs`（新）
- **Interfaces**: Consumes `draggedItemStore`（模块内）；Produces SortableList 无 prop/emits 变化
- **Steps**:
  1. RED：守卫测试——模拟 isDragging=true 时触发三条 re-init 路径，断言不调用 destroy（以源码结构断言 + 行为桩双轨，参照 test_payload_stub_guard.cjs 模式）。
  2. GREEN-a：`SortableList.vue` —— 三条 re-init 路径统一守卫：isDragging 时若实例已存在则跳过（destroy 禁令）；实例不存在（懒初始化名单）允许纯创建。forceReinit watcher 的 sourceKey 守卫泛化为「任何实例拖拽中禁 destroy」。
  3. GREEN-b：`Sidebar.vue` —— groups 目录 `:group` 内联对象提升为模块级常量 `groupsDragGroup`；新增拖拽 failsafe：onDragStart 注册一次性 window `mouseup`/`touchend`/`keydown(Escape)`，回调后 150ms 若 `isDragging` 仍 true → 执行与 onConversationDragEnd 相同的清理（移除监听器/停边缘滚动/清 hover 计时器/`isDragging=false`/`deduplicateDragElements`/`reconcileDragState`），并记 console.warn。
  4. GREEN-c：`sidebar-panels.css` —— `.sidebar-dragging .group-convs-collapsed` 不再 `grid-template-rows: 1fr`；折叠组保持 0fr，容器 min-height 44px 放置条 + 内层列表固定 44px 可视高度（overflow 裁剪，DOM 不变）；悬停组仍由既有 `dragExpandedGroupId` 类移除机制完整展开。
  5. UI E2E（Playwright/Electron，参照 tests/ui_sync_refresh.py 模式）：起本地栈 → 建 2 分组各若干 session + 折叠 → 拖一个 session 到折叠分组 → 断言：拖拽中折叠组数=放置条数（非全长）、drop 后 isDragging 复位、session 落入目标组。截图入 tests/。
- **Test seam**: workflows .cjs（守卫）+ tests/ui_*.py（E2E 证据）。

## Task 3 — 云端/本地执行客户端层语义拆分（判据 56）

**方案（用户裁定 A）**：客户端引入显式执行模式。桌面壳已知本地栈是否实启（`runtimeApiBase` 仅在本地栈成功时设置，main.cjs:795）→ 经 additionalArguments `--weave-exec-mode=local` 注入 → preload 校验后暴露 `weaveRuntime.execMode` → `base.ts` 导出 `EXECUTION_MODE`（runtime 'local' 优先；否则烘焙 base 为 loopback=local、远端/空=cloud——web 同源=cloud）→ ChatInput 执行环境选择器空目标标签/描述/title 按模式显示「本地（本机执行）」/「云端服务器」。server/ 契约零改动（象限 A+B 不动红线）。

- **Files**: Modify `desktop/main.cjs`、`desktop/preload.cjs`、`app/src/api/base.ts`、`app/src/components/ChatInput.vue`；Test `app/tests/workflows/test_exec_mode_wiring.cjs`（新）
- **Interfaces**: Consumes `runtimeApiBase`（main.cjs:63/795）；Produces `EXECUTION_MODE`（base.ts 命名导出）、`weaveRuntime.execMode: 'local'|''`
- **Steps**:
  1. RED：`test_exec_mode_wiring.cjs` —— 桩 globalThis.weaveRuntime 三种形态断言 EXECUTION_MODE 解析 + ChatInput 标签源引用断言（参照 test_exec_env_wiring.cjs 既有模式）。
  2. GREEN：四文件实现（上）。
  3. UI 冒烟：本地栈启动后打开执行环境菜单截图，首项=「本地」语义。
- **Test seam**: workflows .cjs + 截图证据。

## 收尾（判据 57/58）

- 全量回归：app test:unit + project_build.sh。
- `docs/DECISION_MATRIX_local_vs_upstream.md` §3 登记 Task 1/2 的象限 C hunk（chat.ts flush-refresh / SortableList 守卫 / Sidebar 拖拽常量+failsafe / sidebar-panels.css 放置条）。
- 记忆 `memory/fix_w26_*.md` + MEMORY.md 索引。
- Lane L：FCV（tests/fcv_brief.md → fcv_report.md）+ 双对抗评审（§V4）+ coverage_matrix。
