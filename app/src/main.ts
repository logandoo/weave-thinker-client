import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import './styles/main.css'
// 皮肤令牌覆盖层：必须在 main.css 之后引入（同优先级按序覆盖）
import './styles/themes/index.css'
import { installGlobalCodeBlockCopy } from '@/composables/useCodeBlockCopy'
import { installGlobalCleanCopy } from '@/composables/cleanCopy'
import Sortable from 'sortablejs'
// Tab-恢复重连监听器（模块副作用注册）
import '@/stores/chatVisibility'

// Make SortableJS available globally for vuedraggable
;(window as any).Sortable = Sortable

// W39 构建世代（与侧车 _internal/BUILD_ID 同源；打包脚本在构建前注入 VITE_BUILD_ID）。
// 打包冒烟据此断言「SPA 与侧车来自同一代产物」，而非仅凭 BUILD_ID 文件存在于侧车。
;(window as any).__WEAVE_BUILD_ID__ = import.meta.env.VITE_BUILD_ID || 'dev'

const app = createApp(App)
app.config.errorHandler = (err, vm, info) => {
  console.error('Vue error:', err, info, vm)
}
window.addEventListener('unhandledrejection', (event) => {
  console.error('Unhandled promise rejection:', event.reason)
})
window.addEventListener('error', (event) => {
  console.error('Global error:', event.error)
})
app.use(createPinia())
app.use(router)
// One-click copy buttons inside markdown code blocks (delegated listener).
installGlobalCodeBlockCopy()
// 笔记复制净化（判据 #28/#31）：作用域内 copy 事件写入 Word 友好 HTML。
installGlobalCleanCopy()
app.mount('#app')
