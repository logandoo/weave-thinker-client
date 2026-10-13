<template>
  <header v-if="showTitleBar" class="app-titlebar" data-testid="app-titlebar">
    <div class="titlebar-content">
      <span class="titlebar-title">{{ title }}</span>
    </div>
  </header>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue'
import { getNativeBridge } from '@/native/bridge'

// 自定义标题栏（2026-09-25 topbar 波）：仅 Electron darwin 渲染——该宿主经
// titleBarStyle:'hiddenInset' 隐藏了原生栏（desktop/main.cjs）；Web/移动宿主
// 无原生框可替换，不渲染。配色全部走皮肤令牌（--color-bg/--color-text/
// --skin-line*），随 data-skin/data-theme 自动切换，零主题代码。
// 真全屏波（2026-09-28 用户诊断）：进入系统全屏（红绿灯/原生栏均隐藏）后，
// 自绘栏必须同步隐藏并把 --wt-titlebar-h 归零——否则全屏下残留 38px 假栏，
// 「无法展开真实的全屏」。状态经 NativeBridge.window（主进程事件转发）。
// 评审 I1（2026-09-28）：先同步订阅、后异步查询——否则订阅前的事件丢失导致
// 状态失同步；查询结果仅在无事件抢先时应用；卸载守卫防 resolve 后泄漏监听器。
const bridge = getNativeBridge()
const isDarwinDesktop = bridge?.platform.kind === 'desktop' && bridge.platform.os === 'darwin'

const isFullScreen = ref(false)
const showTitleBar = computed(() => isDarwinDesktop && !isFullScreen.value)

function applyTitlebarHeight() {
  if (isDarwinDesktop && typeof document !== 'undefined') {
    document.documentElement.style.setProperty('--wt-titlebar-h', isFullScreen.value ? '0px' : '38px')
  }
}
applyTitlebarHeight()

let offFullScreen: (() => void) | undefined
let disposed = false
if (isDarwinDesktop && bridge?.window) {
  let eventSeen = false
  offFullScreen = bridge.window.onFullScreenChanged((value) => {
    eventSeen = true
    isFullScreen.value = value
    applyTitlebarHeight()
  })
  bridge.window.isFullScreen().then((value) => {
    if (disposed || eventSeen) return // 事件已先行 → 查询结果作废（事件最新）
    isFullScreen.value = value
    applyTitlebarHeight()
  }).catch(() => { /* 查询失败保持窗口态；后续事件仍可纠正 */ })
}
onUnmounted(() => {
  disposed = true
  if (offFullScreen) offFullScreen()
})

const title = '罗航 — Weave Thinker'
</script>

<style scoped>
.app-titlebar {
  height: 38px;
  flex: 0 0 38px;
  display: flex;
  align-items: stretch;
  background: var(--color-bg);
  border-bottom: 1px solid var(--skin-line-soft, var(--color-bg-secondary));
  -webkit-app-region: drag;
  user-select: none;
}

.titlebar-content {
  flex: 1;
  display: flex;
  align-items: center;
  /* 红绿灯避让：hiddenInset 下 mac 交通灯区约 78px（Electron 文档定值） */
  padding: 0 12px 0 80px;
  -webkit-app-region: drag;
}

.titlebar-title {
  font-size: 13px;
  font-weight: 500;
  color: var(--color-text-light);
  letter-spacing: 0.01em;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
