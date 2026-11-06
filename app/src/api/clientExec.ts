import api from './client'

export interface DeviceExecStatus {
  enabled: boolean
  paired: boolean
  connected: boolean
  auth_failed: boolean
  device_id: string
  name: string
  platform: string
  cloud_url: string
  last_heartbeat_at: string | null
  last_error: string
  inflight_calls: number
  identity_bound: boolean
  name_gate_ready: boolean
  build_id: string
  impl_marker: string
  cloud_username: string
  cloud_user_id: string
  workspace_ctx_expected: string
}

export interface DeviceExecPairResult {
  device_id: string
  name: string
  platform: string
  cloud_url: string
  device_id_changed?: boolean
}

export async function getDeviceExecStatus(): Promise<DeviceExecStatus> {
  const { data } = await api.get('/client-exec/status', { timeout: 10000 })
  return data
}

export async function pairDeviceExec(payload: {
  pairing_code: string
  cloud_url?: string
  name?: string
  platform?: string
  device_id?: string
  tls_skip?: boolean
}): Promise<DeviceExecPairResult> {
  const { data } = await api.post('/client-exec/pair', payload, { timeout: 20000 })
  return data
}

export async function resetDeviceExec(): Promise<{ reset: boolean }> {
  const { data } = await api.post('/client-exec/reset', {}, { timeout: 10000 })
  return data
}

export interface DeviceExecRenameResult {
  device_id: string
  name: string
  renamed: boolean
  renamed_cloud?: boolean
  cloud_note?: string
}

// W32 执行环境云端联动：云端执行器注册表（「其他绑定的客户端」菜单数据源）。
// 契约=server/API.md「Cloud Device Registry」节；is_self=本机设备（「本地」语义）。
export interface CloudDevicesResult {
  linked: boolean
  devices: import('@/types').ClientDevice[]
}

export async function getCloudDevices(): Promise<CloudDevicesResult> {
  const { data } = await api.get('/client-exec/cloud-devices', { timeout: 10000 })
  return data
}

export async function revokeCloudDevice(deviceId: string): Promise<{ revoked: boolean }> {
  const { data } = await api.post(
    `/client-exec/cloud-devices/${encodeURIComponent(deviceId)}/revoke`, {}, { timeout: 15000 },
  )
  return data
}

export async function renameDeviceExec(payload: { name: string }): Promise<DeviceExecRenameResult> {
  const { data } = await api.post('/client-exec/rename', payload, { timeout: 20000 })
  return data
}
