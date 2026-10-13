// 平台能力访问层 — SPA 内唯一允许直接触碰 window.NativeBridge 的位置。
// Web（浏览器）下返回 null，调用方走既有 web 回退路径。
export interface NativeBridgePlatform {
  os: string
  kind: 'desktop' | 'mobile' | 'web'
  shell: string
}

export interface NativeBridgeFs {
  pickDirectory(): Promise<string | null>
  pickFile(filters?: Array<{ name: string; extensions: string[] }>): Promise<string | null>
  readTextFile(path: string): Promise<string>
  writeTextFile(path: string, content: string): Promise<boolean>
  listDir(path: string): Promise<Array<{ name: string; isDir: boolean }>>
  reveal(path: string): Promise<boolean>
  saveBase64(base64: string, filename: string): Promise<string>
}

export interface NativeBridgeApp {
  info(): Promise<{ name: string; version: string; platform: string; electron?: string }>
  openExternal(url: string): Promise<boolean>
}

export interface NativeBridgeWindow {
  isFullScreen(): Promise<boolean>
  onFullScreenChanged(cb: (isFullScreen: boolean) => void): () => void
}

export interface NativeBridgeApi {
  platform: NativeBridgePlatform
  fs: NativeBridgeFs
  app: NativeBridgeApp
  window?: NativeBridgeWindow
}

declare global {
  interface Window {
    NativeBridge?: NativeBridgeApi
  }
}

export function getNativeBridge(): NativeBridgeApi | null {
  return typeof window !== 'undefined' ? (window.NativeBridge ?? null) : null
}

export function isDesktopShell(): boolean {
  return getNativeBridge()?.platform.kind === 'desktop'
}

export function isNativeShell(): boolean {
  const kind = getNativeBridge()?.platform.kind
  return kind === 'desktop' || kind === 'mobile'
}
