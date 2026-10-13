import api from '@/api/client'

export interface UserProfile {
  id: string
  username: string
  created_at: string
  agent_permissions?: Record<string, boolean>
  nickname?: string | null
  avatar_data?: string | null
}

export interface ProviderOverride {
  enabled: boolean
  base_url: string
  model_name: string
  has_api_key: boolean
  api_key_tail: string
  params: Record<string, number | null>
}

export type ProviderOverrides = Record<string, ProviderOverride | null>

export interface LlmProvider {
  alias: string
  display_name: string
}

export interface ModelProviderStatus {
  kinds: string[]
  /** 系统 LLM 供应商（公共别名 + 显示名；不含 URL/Key/真实模型名） */
  llm_providers: LlmProvider[]
  /** 键：`llm:<alias>`（逐供应商）· `vlm`/`embedding`/`rerank`/`asr`/`tts`（类型级） */
  overrides: ProviderOverrides
}

export interface ProviderOverridePayload {
  enabled?: boolean
  base_url?: string
  /** null = 保持原值；"" = 显式清除；字符串 = 设置新值 */
  api_key?: string | null
  model_name?: string
  params?: Record<string, number | null>
}

export interface WorkspaceRootInfo {
  /** 当前生效的工作区根（用户覆盖或默认布局） */
  workspace_root: string
  /** 默认布局路径（清除覆盖后回到的目录） */
  default_root: string
  /** 是否处于用户自定义覆盖 */
  is_override: boolean
}

export const userSettingsApi = {
  getProfile: () => api.get<UserProfile>('/users/me/profile'),
  updateProfile: (nickname: string | null) =>
    api.put<UserProfile>('/users/me/profile', { nickname }),
  uploadAvatar: (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    // axios 实例默认 Content-Type: application/json 会压掉 multipart 边界
    // （wave-11 已知坑）→ 显式 multipart/form-data。
    return api.post<UserProfile>('/users/me/avatar', fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
  deleteAvatar: () => api.delete<UserProfile>('/users/me/avatar'),
  getWorkspaceRoot: () => api.get<WorkspaceRootInfo>('/users/me/workspace'),
  setWorkspaceRoot: (workspaceRoot: string | null) =>
    api.put<WorkspaceRootInfo>('/users/me/workspace', { workspace_root: workspaceRoot }),
  getModelProvider: () => api.get<ModelProviderStatus>('/users/me/model-provider'),
  updateModelProvider: (payload: Record<string, ProviderOverridePayload | null>) =>
    api.put<ModelProviderStatus>('/users/me/model-provider', payload),
  deleteModelProvider: () => api.delete('/users/me/model-provider'),
}
