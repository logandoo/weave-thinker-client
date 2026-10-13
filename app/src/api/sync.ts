import api from './client'

export interface SyncStatus {
  server_enabled: boolean
  client_enabled: boolean
  device_id: string | null
  push_cursor: number | null
  pull_cursor: number | null
  pending_events: number
  last_sync_at: string | null
  last_error: string | null
  conflicts_count: number
  /** 全量 resync 请求是否仍待消费（A4.9 评审 B#2 完成因果锚） */
  resync_pending?: boolean
}

export interface SyncDevice {
  id: string
  name: string | null
  platform: string | null
  last_seen_at: string | null
  revoked: boolean
  created_at: string | null
}

export interface SyncConflict {
  id: string
  entity_type: string
  entity_id: string
  direction: 'push' | 'pull'
  reason: string
  local_updated_at: string | null
  remote_updated_at: string | null
  created_at: string | null
}

export async function getSyncStatus(): Promise<SyncStatus> {
  // 超时上限（A4.9 评审 B#8）：轮询/等待路径单请求不悬挂——挂死会把按钮态卡在「同步中」
  const { data } = await api.get('/sync/status', { timeout: 10000 })
  return data
}

export async function getSyncDevices(): Promise<SyncDevice[]> {
  const { data } = await api.get('/sync/devices')
  return data
}

export async function revokeSyncDevice(id: string): Promise<void> {
  await api.post(`/sync/devices/${id}/revoke`)
}

export async function getSyncConflicts(limit = 50): Promise<SyncConflict[]> {
  const { data } = await api.get('/sync/conflicts', { params: { limit } })
  return data
}

export interface SyncAccount {
  bound: boolean
  server_url?: string
  cloud_username?: string
  device_id?: string
  last_sync_at?: string | null
  last_error?: string | null
}

export async function getSyncAccount(): Promise<SyncAccount> {
  const { data } = await api.get('/sync/account')
  return data
}

export async function bindSyncAccount(payload: {
  server_url: string
  cloud_username: string
  cloud_password: string
  tls_verify?: boolean
}): Promise<SyncAccount> {
  const { data } = await api.put('/sync/account', payload)
  return data
}

export async function unbindSyncAccount(): Promise<void> {
  await api.delete('/sync/account')
}

export async function resyncSyncAccount(): Promise<void> {
  await api.post('/sync/account/resync')
}

export interface SyncNowResult {
  scheduled: boolean
  /** 判据#8 限频：true=冷却内被节流（近期已有触发在途，非未绑定） */
  throttled?: boolean
}

export async function postSyncNow(): Promise<SyncNowResult> {
  const { data } = await api.post('/sync/account/sync-now')
  return data
}
