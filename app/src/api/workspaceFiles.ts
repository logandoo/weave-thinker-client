/**
 * Workspace file API helpers (2026-09-19).
 *
 * - `fetchDirectory` lists one folder (folder manager dialog).
 * - URL builders append the JWT as `?token=` so the browser can fetch/open
 *   zip and converted-PDF responses without custom headers (same pattern as
 *   the existing download cards).
 */
import api from './client'
import { apiUrl } from './base'
import { resolveDownloadUrl, normalizeDeviceEntries } from '@/utils/deviceUrls'

export interface WorkspaceEntry {
  name: string
  rel_path: string
  is_dir: boolean
  size: number
  mtime: number
  type: string
}

export interface DirectoryListing {
  path: string
  entries: WorkspaceEntry[]
  truncated: boolean
}

function tokenParam(): string {
  const token = localStorage.getItem('chatllm_token')
  return token ? `&token=${encodeURIComponent(token)}` : ''
}

export function buildDownloadUrl(target: string, deviceId?: string | null): string {
  // W18 G3/G4：设备上下文 → 设备文件代理（仅 workspace 相对路径）；
  // 绝对路径/无设备上下文 → 云端下载（原语义）。纯派生在 utils/deviceUrls（单测缝）。
  return apiUrl(resolveDownloadUrl(target, deviceId, localStorage.getItem('chatllm_token')))
}

// W19 P1：设备上下文 → 设备端 zip/office 转换代理（fs/zip · fs/office-* RPC）
export function buildZipUrl(relPath: string, deviceId?: string | null): string {
  if (deviceId) {
    return apiUrl(`/api/devices/${encodeURIComponent(deviceId)}/fs/zip?path=${encodeURIComponent(relPath)}${tokenParam()}`)
  }
  return apiUrl(`/api/files/zip?path=${encodeURIComponent(relPath)}${tokenParam()}`)
}

export function buildOfficePdfUrl(relPath: string, deviceId?: string | null): string {
  if (deviceId) {
    return apiUrl(`/api/devices/${encodeURIComponent(deviceId)}/fs/office-pdf?path=${encodeURIComponent(relPath)}${tokenParam()}`)
  }
  return apiUrl(`/api/files/office-pdf?path=${encodeURIComponent(relPath)}${tokenParam()}`)
}

export function buildOfficeHtmlUrl(relPath: string, deviceId?: string | null): string {
  if (deviceId) {
    return apiUrl(`/api/devices/${encodeURIComponent(deviceId)}/fs/office-html?path=${encodeURIComponent(relPath)}${tokenParam()}`)
  }
  return apiUrl(`/api/files/office-html?path=${encodeURIComponent(relPath)}${tokenParam()}`)
}

export async function fetchDirectory(
  relPath: string,
  signal?: AbortSignal,
  deviceId?: string | null,
): Promise<DirectoryListing> {
  // W18 G4：设备上下文 → 设备 fs/list 代理（响应形状归一，宽容目录枚举）
  if (deviceId) {
    const { data } = await api.get<{
      ok?: boolean
      entries?: Array<Record<string, unknown>>
    }>(`/devices/${encodeURIComponent(deviceId)}/fs/list`, {
      params: { path: relPath },
      signal,
    })
    return { path: relPath, entries: normalizeDeviceEntries(data.entries, relPath), truncated: false }
  }
  const { data } = await api.get<DirectoryListing>('/files/list', {
    params: { path: relPath },
    signal,
  })
  return data
}

/** 云端 workspace 文件同步状态（2026-09-29 波，预览前查询）。 */
export interface WorkspaceFileSyncStatus {
  bound: boolean
  local_exists: boolean
  local_size: number | null
  /** null = 未知（未绑定/远端探测失败）；false = 远端无此文件 */
  remote_exists: boolean | null
  remote_size: number | null
  auto_max_mb: number
  max_mb: number
}

export async function getWorkspaceFileSyncStatus(
  relPath: string,
): Promise<WorkspaceFileSyncStatus> {
  const { data } = await api.get<WorkspaceFileSyncStatus>('/sync/workspace/status', {
    params: { path: relPath },
  })
  return data
}

export interface WorkspaceFileSyncResult {
  ok: boolean
  path: string
  size: number
}

/** 按需把单个云端 workspace 文件同步到本地（大文件预览的「同步」按钮）。 */
export async function syncWorkspaceFile(relPath: string): Promise<WorkspaceFileSyncResult> {
  const { data } = await api.post<WorkspaceFileSyncResult>('/sync/workspace/fetch', {
    path: relPath,
  })
  return data
}

export function baseName(relPath: string): string {
  const seg = (relPath || '').split('/').filter(Boolean)
  return seg.length ? seg[seg.length - 1] : relPath || ''
}

export function formatMtime(seconds: number): string {
  if (!seconds) return ''
  const d = new Date(seconds * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}
