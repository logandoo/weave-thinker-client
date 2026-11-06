/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<{}, {}, any>
  export default component
}

declare module 'vue-router' {
  interface RouteMeta {
    layout?: string
  }
}

interface ImportMetaEnv {
  readonly VITE_API_BASE?: string
  /** W39：构建世代（打包脚本注入；dev 构建为 'dev'） */
  readonly VITE_BUILD_ID?: string
}
