// 同步后数据刷新（2026-09-30 判据#4/#5，用户追加准则）。
//   refreshAllData()     — 全量同步完成后不切页刷新三模式可见数据（sidebar/对话/笔记）
//   waitSyncComplete()   — 轮询 /sync/status：last_sync_at 前进=done；last_error=failed；超时=timeout
//   startupSync()        — 启动期优先异步同步（sync-now），失败静默=离线模式继续用本地数据
// 完成信号选型见 tests/decisions.md D-38（轮询 last_sync_at，非 WebSocket 推送）。
import { useAssistantStore } from '../stores/assistant'
import { useChatStore } from '../stores/chat'
import { useGroupStore } from '../stores/groups'
import { useNotesStore } from '../stores/notes'
import { useZenStore } from '../stores/zen'
import { getSyncStatus, postSyncNow } from '../api/sync'

export type SyncWaitResult = 'done' | 'failed' | 'timeout'

/** 轮询 /sync/status 等待一个同步周期完成。baselineTs=null 表示等待首次盖章（resync 语义）。
 *  opts.expectResync=true（resync 语义）：完成还须 resync_pending=false——把「完成」锚定到
 *  真正跑完全量 resync 的周期（A4.9 评审 B#2：在途周期的时间戳盖章不再假满足）。
 *  错误判定基线感知（A4.9 评审 A#1）：仅「相对 baseline 新出现的 last_error」算失败，
 *  历史陈旧错误不触发误报。 */
export async function waitSyncComplete(
  baselineTs: string | null,
  timeoutMs = 60000,
  opts?: { expectResync?: boolean; baselineError?: string | null },
): Promise<SyncWaitResult> {
  const deadline = Date.now() + timeoutMs
  const baselineError = opts?.baselineError ?? null
  while (Date.now() < deadline) {
    try {
      const st = await getSyncStatus()
      if (st.last_error && st.last_error !== baselineError) return 'failed'
      const stamped = Boolean(st.last_sync_at && st.last_sync_at !== baselineTs)
      const resyncSettled = !opts?.expectResync || !st.resync_pending
      if (stamped && resyncSettled) return 'done'
    } catch {
      // 状态面暂时不可达：继续轮询直至超时（本地服务重启窗口自愈）
    }
    await new Promise((r) => setTimeout(r, 2000))
  }
  return 'timeout'
}

/** 全量同步完成后统一刷新：三模式 sidebar（会话/分组/笔记本）、对话内容、笔记/笔记本内容。
 *  流式进行中的对话跳过 refreshConversation（防打断进行中的渲染）。
 *  笔记列表腿覆盖工作台/深链（A4.9 评审 A#4/B#3）：refreshFromExternalChange 刷全部
 *  已缓存笔记本 + zen 当前笔记本补拉，currentNotebookId 为 null 的深链不再漏刷。 */
export async function refreshAllData(): Promise<void> {
  const assistantStore = useAssistantStore()
  const chatStore = useChatStore()
  const groupStore = useGroupStore()
  const notesStore = useNotesStore()
  const zenStore = useZenStore()

  await assistantStore.loadAssistants().catch(() => {})
  await Promise.all([
    chatStore.loadConversations(assistantStore.currentAssistantId).catch(() => {}),
    groupStore.loadGroups(assistantStore.currentAssistantId || undefined).catch(() => {}),
    notesStore.refreshFromExternalChange().catch(() => {}),
  ])

  const convId = chatStore.currentConversationId
  if (convId && !chatStore.isStreamingCurrentConversation) {
    await chatStore.refreshConversation(convId).catch(() => {})
  }

  const nbId = notesStore.currentNotebookId || zenStore.currentNotebookId
  if (nbId && !(notesStore.notes || {})[nbId]) {
    await notesStore.loadNotes(nbId).catch(() => {})
  }
  const noteId = notesStore.currentNote?.id
  if (noteId) {
    await notesStore.loadNote(noteId).catch(() => {})
  }
}

/** 启动期优先异步同步（判据#5）：永不抛——失败/超时=离线模式静默继续用本地数据。 */
export async function startupSync(): Promise<void> {
  try {
    let baseline: string | null = null
    let baselineError: string | null = null
    try {
      const st = await getSyncStatus()
      baseline = st.last_sync_at
      baselineError = st.last_error
    } catch {
      return // 状态面不可达=本地服务未就绪，本就离线态，零打扰
    }
    const { scheduled, throttled } = await postSyncNow()
    // 未绑定=本地数据即全量，无需刷新；节流（A4.9 B#5）=近期已有触发在途——
    // 照常等待该轮完成（周期即将跑），不等价于未绑定
    if (!scheduled && !throttled) return
    const r = await waitSyncComplete(baseline, 45000, { baselineError })
    if (r === 'done') await refreshAllData()
    // failed/timeout → 离线模式：本地数据照常（产品原则 断网全功能），last_error 在同步面板可见
  } catch {
    // 判据#5「同步失败直接使用离线模式」：静默降级（tests/decisions.md D-39）
  }
}
