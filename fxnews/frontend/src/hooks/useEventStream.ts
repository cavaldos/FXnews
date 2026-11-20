import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { STREAM_URL } from '../api/client'
import type { AnalysisOut, NewsOut } from '../api/types'

export type StreamStatus = 'connecting' | 'open' | 'reconnecting'

interface Handlers {
  news?: (item: NewsOut) => void
  analysis?: (item: AnalysisOut) => void
}

function parse<T>(ev: Event): T | null {
  try {
    return JSON.parse((ev as MessageEvent<string>).data) as T
  } catch {
    return null
  }
}

/**
 * Subscribe to the backend SSE stream (`news` and `analysis` events).
 * Handlers may change every render; the latest ones are always used.
 * Reconnects automatically (EventSource retries by itself while CONNECTING; when the
 * browser gives up after a proxy 502 we reopen with exponential backoff).
 */
export function useEventStream(handlers: Handlers): StreamStatus {
  const [status, setStatus] = useState<StreamStatus>('connecting')
  const handlersRef = useRef(handlers)

  useLayoutEffect(() => {
    handlersRef.current = handlers
  })

  useEffect(() => {
    let es: EventSource | null = null
    let timer: ReturnType<typeof setTimeout> | undefined
    let attempt = 0
    let stopped = false

    const connect = () => {
      if (stopped) return
      const source = new EventSource(STREAM_URL)
      es = source
      source.onopen = () => {
        attempt = 0
        setStatus('open')
      }
      source.onerror = () => {
        setStatus('reconnecting')
        if (source.readyState === EventSource.CLOSED) {
          source.close()
          if (es === source) es = null
          timer = setTimeout(connect, Math.min(30_000, 1000 * 2 ** attempt))
          attempt += 1
        }
      }
      source.addEventListener('news', (ev) => {
        const item = parse<NewsOut>(ev)
        if (item) handlersRef.current.news?.(item)
      })
      source.addEventListener('analysis', (ev) => {
        const item = parse<AnalysisOut>(ev)
        if (item) handlersRef.current.analysis?.(item)
      })
    }

    connect()
    return () => {
      stopped = true
      if (timer) clearTimeout(timer)
      es?.close()
    }
  }, [])

  return status
}
