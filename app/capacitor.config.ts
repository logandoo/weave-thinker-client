import type { CapacitorConfig } from '@capacitor/cli'

// Weave Thinker 移动壳（Capacitor）— 资产本地打包（dist 进包，符合 Apple 2.5.2 自包含；
// 不使用 server.url 远程加载）。远程 API 经 VITE_API_BASE 注入（见 config.toml）。
// 平台生成：npm --workspace app exec -- cap add ios / cap add android
const config: CapacitorConfig = {
  appId: 'com.weavethinker.client',
  appName: 'WeaveThinker',
  webDir: 'dist',
  server: {
    androidScheme: 'https'
  }
}

export default config
