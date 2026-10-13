<template>
  <div class="app-root">
    <AppTitleBar />
    <div class="sr-only" role="status" aria-live="polite" aria-atomic="true">
      {{ liveAnnouncement }}
    </div>
    <router-view v-slot="{ Component, route }">
      <!-- out-in（2026-10-03 遗留#1）：旧组件卸载完毕才挂载新组件——路由过渡期
           两个笔记组件的 document keydown 不得同时存活（⌘S 双存） -->
      <Transition :name="transitionName" mode="out-in">
        <component :is="Component" :key="route.meta.layout || route.name" />
      </Transition>
    </router-view>
  </div>
  <ConfirmDialog />
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, provide, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { useAuth } from '@/composables/useAuth'
import { useSkinStore } from '@/stores/skin'
import { handleAndroidBack } from '@/composables/useBackNavigation'
import { startupSync } from '@/composables/useDataRefresh'
import ConfirmDialog from '@/components/ConfirmDialog.vue'
import AppTitleBar from '@/components/AppTitleBar.vue'

const auth = useAuth()
const skinStore = useSkinStore()
const router = useRouter()
const transitionName = ref('')
const liveAnnouncement = ref('')

// 皮肤/明暗在 Vue 挂载前同步应用，避免首帧闪烁（FOUC）
skinStore.initFromStorage()

function announce(message: string) {
  liveAnnouncement.value = ''
  nextTick(() => {
    liveAnnouncement.value = message
  })
}

provide('announce', announce)

function isZenRoute(path: string) {
  return path === '/zen'
}

function isAppRoute(path: string) {
  return path !== '/login'
}

function updateZenTransition(toPath: string, fromPath: string) {
  const toZen = isZenRoute(toPath)
  const fromZen = isZenRoute(fromPath)
  const toApp = isAppRoute(toPath)
  const fromApp = isAppRoute(fromPath)

  if (toApp && fromApp && ((toZen && !fromZen) || (!toZen && fromZen))) {
    transitionName.value = 'zen-mode'
  } else {
    transitionName.value = ''
  }
}

const unregisterBeforeEach = router.beforeEach((to, from) => {
  updateZenTransition(to.path, from.path)
})

router.onError((error) => {
  console.error('Router error:', error)
  const message = '页面加载失败，请刷新重试'
  if (typeof (window as any).$toast === 'function') {
    ;(window as any).$toast(message)
  } else {
    alert(message)
  }
})

// Expose to Android WebView JS bridge
;(window as any).handleAndroidBack = handleAndroidBack

onMounted(() => {
  auth.initAuth()
  // Sliding session (wave-3): a valid stored token silently rolls its
  // window forward so users active within [security] token_expire_days
  // never re-enter their password. Fire-and-forget (errors are no-ops;
  // an expired token falls through to the normal 401 → login flow).
  if (auth.isAuthenticated.value) {
    // 判据#5（2026-09-30 用户追加）：启动优先异步同步，失败静默离线（D-39）。
    // Fire-and-forget：绝不阻断首屏；完成则刷新三模式数据（startupSync 内部吞异常）。
    // 双重门控（iter 7 ERR_ABORTED 实证 + A4.9 评审 A#3）：
    //   1) 未登录不发同步请求（401 拦截器整页跳转与 loadURL 竞态崩壳）；
    //   2) token 已过期时 refreshSession 失败同样跳过——过期 token 发
    //      /sync/status 会走同一崩溃路径。
    void auth.refreshSession().then((ok) => {
      if (ok) void startupSync()
    })
  }
})

onUnmounted(() => {
  unregisterBeforeEach()
})
</script>

<style scoped>
.app-root {
  position: relative;
  width: 100vw;
  height: 100vh;
  height: 100dvh;
  overflow: hidden;
  /* topbar 波：纵向 flex——标题栏（darwin 自绘）置顶，路由视图占余量 */
  display: flex;
  flex-direction: column;
}

.app-root > .sr-only {
  position: absolute;
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

.zen-mode-enter-active,
.zen-mode-leave-active {
  position: absolute;
  inset: 0;
  overflow: hidden;
  will-change: opacity, transform;
  transition: opacity 0.28s cubic-bezier(0.4, 0, 0.2, 1),
              transform 0.28s cubic-bezier(0.4, 0, 0.2, 1);
}

.zen-mode-enter-from,
.zen-mode-leave-to {
  opacity: 0;
  transform: scale(0.98);
}
</style>
