// 客户端（Electron/Capacitor）与 Web 共用的后端地址解析。
// Web（同源部署）时 VITE_API_BASE 为空 —— 全部相对路径照旧；
// 客户端构建时 VITE_API_BASE 指向后端 origin（如 https://host:8158）。
const BAKED = (import.meta as any).env?.VITE_API_BASE as string | undefined
// 桌面壳运行时注入（本地栈端口可能因占用协商而变化）：window.weaveRuntime.apiBase
const RUNTIME_RAW = ((globalThis as any).weaveRuntime?.apiBase as string | undefined) || ''
// 运行时值再校验（只信 loopback http(s)；异常值回退烘焙 base）
const RUNTIME = /^https?:\/\/(127\.0\.0\.1|localhost)(:\d{1,5})?$/.test(RUNTIME_RAW) ? RUNTIME_RAW : ''

export const API_ORIGIN = (RUNTIME || BAKED || '').replace(/\/+$/, '')

export function apiUrl(path: string): string {
  return API_ORIGIN ? `${API_ORIGIN}${path}` : path
}

export function apiWsUrl(path: string): string {
  const base = API_ORIGIN ? new URL(API_ORIGIN) : new URL(window.location.origin)
  base.protocol = base.protocol === 'https:' ? 'wss:' : 'ws:'
  const p = path.startsWith('/') ? path : `/${path}`
  return base.origin + p
}
