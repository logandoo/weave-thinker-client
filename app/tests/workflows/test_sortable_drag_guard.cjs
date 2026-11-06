/* W26 sidebar 拖拽卡死 + 全展开 接线不变量。
 *
 * 症状（用户）：agent 模式 sidebar 拖 session 进分组时 ①一旦拖动就无法释放
 * （卡死）；②拖动时自动展开所有分组，组内 session 多时无法放入目标分组。
 *
 * 根因：SortableJS 1.14.0 destroy() → _onDrop(undefined) → _nulling() 清
 * **模块级全局**拖拽态（上游 Vue.Draggable#1199 同源）→ onEnd 永不派发 →
 * isDragging 永真 → .sidebar-dragging 永驻 → CSS 全展开永久化。
 * 拖拽中 destroy 的三个触发点：
 *   T1 Sidebar groups 目录 :group 内联对象每渲染新引用 → props.group watcher 重建
 *   T2 折叠组拖拽中被 CSS 撑开 → 高度 0→N → ResizeObserver 重建（无守卫）
 *   T3 悬停 300ms forceReinitCounter++ → 目标列表 destroy+重建（守卫只护源列表）
 *
 * 本测试钉住的接线不变量（AST/源码结构断言，沿用 test_interjection_queue.cjs
 * 范式；行为级证据由 Playwright E2E 承担）：
 *   D1 initSortable 扼流守卫：isDragging 且实例已存在时禁止 destroy 重建
 *   D2 forceReinit watcher 不再自带 destroy（destroy 只能经 initSortable 扼流）
 *   D3 groups 目录拖拽组引用稳定（模板无内联对象字面量，改用模块级常量）
 *   D4 failsafe：onDragStart 注册 window mouseup/touchend/keydown 兜底，
 *      end 事件未到达时强制复位拖拽态（含 body 样式还原）
 *   D5 CSS：拖拽中折叠组不再 grid-template-rows:1fr 全展开，仅固定高放置条
 *
 * Run: node tests/workflows/test_sortable_drag_guard.cjs */
const fs = require('fs');
const path = require('path');

let failed = 0;
let passed = 0;
function check(name, cond, detail) {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${detail !== undefined && !cond ? ' | ' + JSON.stringify(detail) : ''}`);
  if (!cond) failed += 1; else passed += 1;
}

const SORTABLE = path.resolve(__dirname, '..', '..', 'src', 'components', 'SortableList.vue');
const SIDEBAR = path.resolve(__dirname, '..', '..', 'src', 'components', 'Sidebar.vue');
const CSS = path.resolve(__dirname, '..', '..', 'src', 'styles', 'sidebar-panels.css');
const sortableSrc = fs.readFileSync(SORTABLE, 'utf8');
const sidebarSrc = fs.readFileSync(SIDEBAR, 'utf8');
const cssSrc = fs.readFileSync(CSS, 'utf8');

// ── D1 initSortable 扼流守卫 ─────────────────────────────────────────────
const initIdx = sortableSrc.indexOf('function initSortable()');
check('D1a initSortable 存在', initIdx > 0);
const initBody = initIdx > 0 ? sortableSrc.slice(initIdx, initIdx + 3000) : '';
check('D1b initSortable 扼流守卫：拖拽中且实例已存在时直接返回（禁 destroy）',
  /draggedItemStore\.isDragging\s*&&\s*sortableInstance/.test(initBody) &&
  /if\s*\(\s*draggedItemStore\.isDragging\s*&&\s*sortableInstance\s*\)\s*return/.test(initBody));

// ── D1c 拖拽全局态必须模块级（评审 B-I1）─────────────────────────────────
// <script setup> 顶层 = 每实例一份 —— 跨实例 destroy 守卫全失效。必须声明在
// 独立 <script>（非 setup）块，并导出 failsafe 复位出口。
const plainScript = sortableSrc.match(/<script lang="ts">([\s\S]*?)<\/script>/);
check('D1c draggedItemStore 在模块级（非 setup）script 块声明',
  !!plainScript && /const draggedItemStore/.test(plainScript[1]));
check('D1d 导出 resetGlobalDragState（failsafe 复位出口）',
  !!plainScript && /export function resetGlobalDragState/.test(plainScript[1]) &&
  /resetGlobalDragState[\s\S]{0,300}draggedItemStore\.isDragging\s*=\s*false/.test(plainScript[1]));
const setupBlock = sortableSrc.match(/<script setup lang="ts">([\s\S]*?)<\/script>/);
check('D1e setup 块不再重复声明 draggedItemStore（防回退到每实例）',
  !!setupBlock && !/const draggedItemStore/.test(setupBlock[1]));

// ── D2 forceReinit watcher 不自带 destroy ────────────────────────────────
const frIdx = sortableSrc.indexOf("watch(() => props.forceReinit");
check('D2a forceReinit watcher 存在', frIdx > 0);
const frBody = frIdx > 0 ? sortableSrc.slice(frIdx, frIdx + 800) : '';
// watcher 体内不得出现 sortableInstance.destroy()（destroy 唯一合法路径=
// initSortable 扼流之后 / onUnmounted）
check('D2b forceReinit watcher 不再自带 destroy（destroy 只能经扼流）',
  !/sortableInstance\.destroy\s*\(\s*\)/.test(frBody));

// ── D3 groups 目录拖拽组引用稳定 ─────────────────────────────────────────
check('D3a groups 目录不再传内联 :group 对象字面量',
  !/:group="\{\s*name:\s*'groups'/.test(sidebarSrc));
check('D3b groups 目录使用模块级稳定常量 groupsDragGroup',
  /const groupsDragGroup\s*=\s*\{\s*name:\s*'groups',\s*pull:\s*false,\s*put:\s*false\s*\}/.test(sidebarSrc) &&
  /:group="groupsDragGroup"/.test(sidebarSrc));

// ── D4 failsafe ──────────────────────────────────────────────────────────
const dragStartIdx = sidebarSrc.indexOf('function onDragStart(');
check('D4a onDragStart 存在', dragStartIdx > 0);
const dragStartBody = dragStartIdx > 0 ? sidebarSrc.slice(dragStartIdx, dragStartIdx + 1600) : '';
check('D4b onDragStart 注册 window mouseup 兜底', /window\.addEventListener\('mouseup'/.test(dragStartBody));
check('D4c onDragStart 注册 window touchend 兜底', /window\.addEventListener\('touchend'/.test(dragStartBody));
check('D4d onDragStart 注册 Escape 兜底', /window\.addEventListener\('keydown'/.test(dragStartBody) && /Escape/.test(sidebarSrc));
// failsafe 函数本体：复位 isDragging + 还原 body 样式 + 清理 document 监听
const failsafeMatch = sidebarSrc.match(/function forceEndDrag[\s\S]{0,1800}/);
check('D4e forceEndDrag 兜底函数存在', !!failsafeMatch);
if (failsafeMatch) {
  const fb = failsafeMatch[0];
  check('D4f failsafe 复位 isDragging', /isDragging\.value\s*=\s*false/.test(fb));
  check('D4g failsafe 还原 body userSelect/cursor（onEnd 未达时残留）',
    /document\.body\.style\.userSelect\s*=\s*''/.test(fb) && /document\.body\.style\.cursor\s*=\s*''/.test(fb));
  check('D4h failsafe 清理拖拽监听器与边缘滚动',
    /removeEventListener\('mousemove', handleDragMouseMove\)/.test(fb) && /stopEdgeScroll\(\)/.test(fb));
  // 评审 B-I1：必须复位模块级全局拖拽态，否则全部实例 re-init 永久锁死
  check('D4i failsafe 复位模块级全局拖拽态 resetGlobalDragState',
    /resetGlobalDragState\(\)/.test(fb) && /import SortableList, \{ resetGlobalDragState \} from '.\/SortableList.vue'/.test(sidebarSrc));
  // 评审 A-I1/B-I1：Escape=取消语义，不得 reconcile+persist 用户想取消的拖拽；
  // 且取消请求期间必须保持模块级 mutation 守卫到真实 end（SortableJS 1.14
  // 无 cancel API，提前复位会让松手时的 add/remove/update 写进列表）。
  check('D4j Escape 取消语义：cancel 分支保持守卫到真实 end，不 reconcile/persist',
    /dragCancelRequested = true/.test(fb) &&
    /if \(reason === 'escape'\)/.test(fb) &&
    /function finishCancelledDrag\(\)/.test(sidebarSrc) &&
    /if \(dragCancelRequested\) \{\s*\n\s*finishCancelledDrag\(\)\s*\n\s*return/.test(sidebarSrc));
  const cancelFn = sidebarSrc.match(/function finishCancelledDrag\(\)[\s\S]{0,1400}/);
  check('D4k 取消收尾不 reconcile/不持久化（只按 Vue 状态还原）',
    !!cancelFn && !/reconcileDragState\(/.test(cancelFn[0]) && !/processDragChanges\(/.test(cancelFn[0]) &&
    /resetGlobalDragState\(\)/.test(cancelFn[0]) && /syncDragLists\(\)/.test(cancelFn[0]));
  check('D4l 指针兜底忽略非主键（右键/中键抬起不得终止拖拽）',
    /'button' in e && \(e as MouseEvent\)\.button !== 0/.test(sidebarSrc));
  check('D4m 取消兜底计时器有界（end 未达也复位，不永久锁死）', (() => {
    const timer = sidebarSrc.match(/dragCancelTimer = window\.setTimeout\([\s\S]{0,900}/);
    return !!timer && /isDragging\.value = false/.test(timer[0]) && /resetGlobalDragState\(\)/.test(timer[0]);
  })());
  // 评审 re-review NEW-1：event.from 是 SortableList 根，分组目录外层不在其中；
  // 分组拖拽必须以被拖项形态判定，否则 dragIsGroup 恒 false、缺失 end 分支不可达。
  check('D4n 分组拖拽以被拖项形态判定（.conversation-group）',
    /dragIsGroup = event\?\.item\?\.classList\?\.contains\('conversation-group'\) === true/.test(sidebarSrc));
  check('D4o 分组拖拽缺失 end 不走会话 reconcile/persist',
    /if \(dragIsGroup\) \{[\s\S]{0,900}?nextTick\(\(\) => syncDragLists\(\)\)[\s\S]{0,40}?return/.test(sidebarSrc));
}

// ── D5b2 failsafe 世代守卫与多点触控（评审 B-I2/B-M1）────────────────────
const pointerMatch = sidebarSrc.match(/function onDragFailsafePointer[\s\S]{0,700}/);
check('D5b2a pointer failsafe 存在', !!pointerMatch);
if (pointerMatch) {
  const pb = pointerMatch[0];
  check('D5b2b 世代守卫：stale 计时器不得扼杀新拖拽', /gen !== dragGeneration/.test(pb));
  check('D5b2c 多点触控守卫：剩余触点≠拖拽结束', /touches.*length > 0/.test(pb) || /'touches' in e/.test(pb));
}
check('D5b2d dragGeneration 在 onDragStart 自增',
  /function onDragStart[\s\S]{0,300}dragGeneration \+= 1/.test(sidebarSrc));

// ── D5b3 end 处理器守卫（评审 A-I1）：failsafe 复位后迟到的 end 直接丢弃 ──
check('D5b3a onConversationDragEnd 有 isDragging 早退守卫',
  /function onConversationDragEnd[\s\S]{0,220}?if \(!isDragging\.value\) return/.test(sidebarSrc));
check('D5b3b onGroupDragEnd 有 isDragging 早退守卫',
  /function onGroupDragEnd[\s\S]{0,200}?if \(!isDragging\.value\) return/.test(sidebarSrc));

// ── D5b4 onUnmounted 摘除 failsafe 监听（评审 B-M3）─────────────────────
check('D5b4 onUnmounted 清理 failsafe/拖拽监听',
  /onUnmounted\(\(\) => \{[\s\S]{0,400}removeDragFailsafeListeners\(\)/.test(sidebarSrc));

// ── D5b5 ZenHistoryDrawer parity（评审 A-M3）─────────────────────────────
const zenSrc = fs.readFileSync(path.resolve(__dirname, '..', '..', 'src', 'components', 'ZenHistoryDrawer.vue'), 'utf8');
check('D5b5 ZenHistoryDrawer 不再传内联 :group 对象字面量',
  !/:group="\{\s*name:\s*'zen-groups'/.test(zenSrc) && /zenGroupsDragGroup/.test(zenSrc));

// ── D5 CSS 放置条（不再全展开）───────────────────────────────────────────
const dragCollapsedRule = cssSrc.match(/\.sidebar-dragging\s+\.group-convs-collapsed\s*\{[^}]*\}/);
check('D5a 拖拽中折叠组规则存在', !!dragCollapsedRule);
if (dragCollapsedRule) {
  check('D5b 不再 grid-template-rows:1fr 全展开（渲染全部 session 的根因）',
    !/grid-template-rows:\s*1fr/.test(dragCollapsedRule[0]));
  check('D5c 保留固定高放置条（min-height）', /min-height:\s*\d+px/.test(dragCollapsedRule[0]));
}
check('D5d 折叠组内层列表拖拽中有固定可视高度（可投放的条）',
  /\.sidebar-dragging\s+\.group-convs-collapsed\s*>\s*[\.\*][\w-]*\s*\{[\s\S]{0,120}?min-height:\s*\d+px/.test(cssSrc));

console.log(`\nRESULT: ${passed} passed, ${failed} failed`);
if (failed) { console.log('RESULT: FAILED'); process.exit(1); }
console.log('RESULT: ALL CHECKS PASS');
