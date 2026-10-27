import { useEffect, useRef, useState } from 'react'

import type { ApiConversation } from '@/api/conversations'
import { getToken } from '@/auth/session'

export type StreamStatus = 'connecting' | 'open' | 'closed'

/**
 * Live updates for one tenant. The backend pushes a `message_update` event with the
 * full conversation (messages + customer) every time a conversation is saved:
 * new inbound message, bot answer, manual reply, status change.
 * Reconnects with backoff; the token travels as a query parameter (EventSource has no headers).
 */
export function useConversationStream(tenant: string | undefined, onConversation: (c: ApiConversation) => void): StreamStatus {
  const callbackRef = useRef(onConversation)
  callbackRef.current = onConversation
  const [status, setStatus] = useState<StreamStatus>('connecting')

  useEffect(() => {
    if (!tenant) return
    const token = getToken()
    if (!token) return

    let source: EventSource | null = null
    let stopped = false
    let retryMs = 1000
    let timer: number | undefined

    const connect = () => {
      const base = import.meta.env.VITE_API_URL || ''
      const url = `${base}/management/v1/sse/${encodeURIComponent(tenant)}/conversations?token=${encodeURIComponent(token)}`
      source = new EventSource(url)
      setStatus('connecting')
      source.onopen = () => {
        setStatus('open')
        retryMs = 1000
      }
      source.onmessage = (event) => {
        if (!event.data) return
        try {
          const data = JSON.parse(event.data) as { type?: string; conversation?: ApiConversation | null }
          if (data?.type === 'message_update' && data.conversation?.obj_id) callbackRef.current(data.conversation)
        } catch {
          /* malformed frame: ignore */
        }
      }
      source.onerror = () => {
        source?.close()
        setStatus('closed')
        if (stopped) return
        timer = window.setTimeout(connect, retryMs)
        retryMs = Math.min(retryMs * 2, 30_000)
      }
    }

    connect()
    return () => {
      stopped = true
      if (timer) window.clearTimeout(timer)
      source?.close()
    }
  }, [tenant])

  return status
}
