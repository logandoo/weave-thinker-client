// W32 执行环境云端联动：本机设备身份（device:<self>）的共享缓存。
// 文件引用面对 device:<self> 必须走本地 workspace API（设备代理无本地会话=404）；
// 显示面对 device:<self> 呈现「本地」。两处同源取 id（client-exec 绑定记录）。
import { getDeviceExecStatus } from '@/api/clientExec'

let cached: string | null = null
let pending: Promise<string | null> | null = null

export function selfDeviceId(): string | null {
  return cached
}

export function isSelfDeviceTarget(target: string | null | undefined): boolean {
  return !!target && !!cached && target === `device:${cached}`
}

export function primeSelfDeviceId(): Promise<string | null> {
  if (cached !== null) return Promise.resolve(cached)
  pending ??= getDeviceExecStatus()
    .then((st) => { cached = st.device_id || ''; return cached })
    .catch(() => { cached = ''; return cached })
  return pending
}
