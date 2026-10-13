import api from './client'
import { apiWsUrl } from './base'
import { TOKEN_KEY } from './client'

export interface TimestampInfo {
  start_time: number
  end_time: number
  text: string
}

export interface SegmentInfo {
  speaker: string
  speaker_confidence: number | null
  start_time: number
  end_time: number
  text: string
}

export interface ASRResponse {
  text: string
  language?: string
  timestamps?: TimestampInfo[]
  segments?: SegmentInfo[]
  hotwords_used?: string[]
  speaker_mode?: string
  duration?: number
}

export interface HotwordItem {
  text: string
  weight: number
  lang?: string
}

export interface ASRStreamStartPayload {
  event: 'start'
  language?: string
  use_hotwords?: boolean
  custom_hotwords?: HotwordItem[]
  context?: string
  chunk_size_sec?: number
  unfixed_chunk_num?: number
  unfixed_token_num?: number
}

export interface ASRStreamEventPayload extends ASRResponse {
  event: 'ready' | 'partial' | 'segment' | 'final' | 'error'
  processed_seconds?: number | null
  total_seconds?: number | null
  chunk_index?: number
  final?: boolean
  backend?: string
  error?: string
  /** error 事件专用：true=致命（task-failed/鉴权/配置），重试必复现 ⇒ 客户端立刻
   *  拆会话；缺省/false=瞬断（上游 WS 掉线），交 scheduleReconnect 收编。 */
  fatal?: boolean
}

export function getAsrWebSocketUrl(token = localStorage.getItem(TOKEN_KEY) || ''): string {
  const url = new URL(apiWsUrl('/api/asr/ws/transcribe/stream'))

  if (token) {
    url.searchParams.set('token', token)
  }

  return url.toString()
}

export const asrApi = {
  async transcribe(audioBlob: Blob, customHotwords?: HotwordItem[]): Promise<ASRResponse> {
    const formData = new FormData()
    formData.append('file', audioBlob, 'audio.wav')
    if (customHotwords && customHotwords.length > 0) {
      formData.append('custom_hotwords', JSON.stringify(customHotwords))
    }

    const { data } = await api.post<ASRResponse>('/asr/transcribe', formData, {
      headers: {
        'Content-Type': undefined
      }
    })

    return data
  },

  async getHotwords(): Promise<HotwordItem[]> {
    const { data } = await api.get<{ hotwords: HotwordItem[] }>('/asr/hotwords')
    return data.hotwords || []
  },

  async saveHotwords(hotwords: HotwordItem[]): Promise<HotwordItem[]> {
    const { data } = await api.post<{ hotwords: HotwordItem[] }>('/asr/hotwords', { hotwords })
    return data.hotwords || []
  }
}
