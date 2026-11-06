import { TOKEN_KEY } from './client'
import api from './client'
import { apiWsUrl } from './base'

export type VoiceState =
  | 'idle'
  | 'connecting'
  | 'listen'
  | 'think'
  | 'speak'
  | 'dual'
  | 'error'

export interface VoiceEvent {
  event: string
  state?: VoiceState
  text?: string
  done?: boolean
  tts?: boolean
  level?: string
  suggestion?: string
  reason?: string
  name?: string
  error?: string
  session_id?: string
  assistant_id?: string
  emotion?: string
  raw_text?: string
  task_id?: string
  title?: string
  status?: string
  speech_kind?: 'answer' | 'aux' | 'notice'
  turn_epoch?: number
  sequence?: number
  audio?: string
  notification_id?: string
  notice_burst_seq?: number
  execution_conversation_id?: string | null
  logical_task_id?: string | null
  assistant_name?: string
  reasoning_policy?: string
  requested_reasoning_effort?: string | null
  deduplicated?: boolean
  count?: number
  items?: Array<Record<string, unknown>>
}

export interface VoiceSession {
  id: string
  title: string
  updated_at?: string | null
  created_at?: string | null
}

export function getVoiceWebSocketUrl(
  token = localStorage.getItem(TOKEN_KEY) || '',
  conversationId?: string | null,
): string {
  const url = new URL(apiWsUrl('/api/voice/ws'))
  if (token) {
    url.searchParams.set('token', token)
  }
  if (conversationId) {
    url.searchParams.set('conversation_id', conversationId)
  }
  url.searchParams.set('audio_protocol', 'epoch_v1')
  return url.toString()
}

export async function getVoiceSessions(): Promise<VoiceSession[]> {
  const { data } = await api.get('/voice/sessions')
  return data
}

export async function createVoiceSession(): Promise<VoiceSession> {
  const { data } = await api.post('/voice/sessions')
  return data
}

export async function getVoiceSessionMessages(sessionId: string): Promise<{ role: string; content: string }[]> {
  const { data } = await api.get(`/voice/sessions/${sessionId}/messages`)
  return data
}

