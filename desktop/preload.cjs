// Weave Thinker 桌面壳 — preload。sandbox 模式下仅依赖 electron 内置 ipcRenderer。
// 暴露：
//   window.NativeBridge  — 新统一接口（fs/app/platform）
//   window.WeaverNoteApp — 兼容既有 Android 壳桥（useDownload.ts 等按此探测）
const { contextBridge, ipcRenderer } = require('electron')

const invoke = (channel, ...args) => ipcRenderer.invoke(channel, ...args)

// 运行时 API base（主进程 additionalArguments 注入；与构建期 VITE_API_BASE
// 互补：本地栈端口动态选择时以运行时为准）
const apiArg = (process.argv || []).find((a) => String(a).startsWith('--weave-api-base='))
const rawApiBase = apiArg ? String(apiArg).slice('--weave-api-base='.length) : ''
// 只接受 loopback http(s) base（评审 Minor：注入值不得指向任意主机）
const safeApiBase = /^https?:\/\/(127\.0\.0\.1|localhost)(:\d{1,5})?$/.test(rawApiBase) ? rawApiBase : ''
contextBridge.exposeInMainWorld('weaveRuntime', { apiBase: safeApiBase })

contextBridge.exposeInMainWorld('NativeBridge', {
  platform: { os: process.platform, kind: 'desktop', shell: 'electron' },
  fs: {
    pickDirectory: () => invoke('bridge:pickDirectory'),
    pickFile: (filters) => invoke('bridge:pickFile', filters),
    readTextFile: (p) => invoke('bridge:readTextFile', p),
    writeTextFile: (p, content) => invoke('bridge:writeTextFile', p, content),
    listDir: (p) => invoke('bridge:listDir', p),
    reveal: (p) => invoke('bridge:reveal', p),
    saveBase64: (base64, filename) => invoke('bridge:saveFileBase64', base64, filename)
  },
  app: {
    info: () => invoke('bridge:appInfo'),
    openExternal: (url) => invoke('bridge:openExternal', url)
  },
  window: {
    // 真全屏波（2026-09-28）：自绘 topbar 需随系统全屏状态隐藏/恢复
    isFullScreen: () => invoke('bridge:isFullScreen'),
    onFullScreenChanged: (cb) => {
      const listener = (_e, value) => cb(Boolean(value))
      ipcRenderer.on('bridge:fullScreenChanged', listener)
      return () => ipcRenderer.removeListener('bridge:fullScreenChanged', listener)
    }
  }
})

contextBridge.exposeInMainWorld('WeaverNoteApp', {
  onThemeChanged: () => {},
  openUrl: (url) => {
    ipcRenderer.send('bridge:openExternalFire', url)
    return true
  },
  isDownloadSupported: () => true,
  saveFile: (base64Data, filename) => {
    invoke('bridge:saveFileBase64', base64Data, filename).catch((e) => console.error('saveFile failed:', e))
    return true
  },
  downloadFile: (url, filename) => {
    invoke('bridge:downloadToFile', url, filename).catch((e) => console.error('downloadFile failed:', e))
    return true
  },
  downloadFileWithProgress: (url, filename) => {
    invoke('bridge:downloadToFile', url, filename).catch((e) => console.error('downloadFile failed:', e))
    return true
  }
})
