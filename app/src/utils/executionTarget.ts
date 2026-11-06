export interface ResolveExecutionTargetInput {
  conversationId: string | null | undefined
  currentConversationId: string | null | undefined
  storeTarget: string | null
  metaTarget: string | null | undefined
  /** Snapshot taken before adding an optimistic first user message. */
  hasMessages: boolean
}

export type ExecutionTargetVersions = Record<string, number>

/** Allocate a per-conversation request/mutation sequence, never a response sequence. */
export function advanceExecutionTargetVersion(
  versions: ExecutionTargetVersions,
  conversationId: string,
): number {
  const next = (versions[conversationId] || 0) + 1
  versions[conversationId] = next
  return next
}

export function isExecutionTargetVersionCurrent(
  versions: ExecutionTargetVersions,
  conversationId: string,
  version: number,
): boolean {
  return (versions[conversationId] || 0) === version
}

/** Row/meta is authoritative; the store is only an empty-current-row fallback. */
export function resolveExecutionTarget({
  conversationId,
  currentConversationId,
  storeTarget,
  metaTarget,
  hasMessages,
}: ResolveExecutionTargetInput): string | null {
  if (!conversationId) return storeTarget
  if (metaTarget !== undefined) return metaTarget
  if (hasMessages) return null
  return conversationId === currentConversationId ? storeTarget : null
}

/** 400/422 from conversation create/bind meaning the requested device target is
 *  unavailable (stale local binding vs server registry). New-chat must fall back
 *  to cloud rather than leaving the button dead. */
export function isExecutionTargetRejection(error: unknown): boolean {
  const response = (error as { response?: { status?: number; data?: { detail?: unknown } } })
    ?.response
  const status = response?.status
  if (status !== 400 && status !== 422) return false
  const detail = String(response?.data?.detail ?? '')
  return /执行环境|execution_target|设备不存在|已解绑|目标设备/.test(detail)
}
