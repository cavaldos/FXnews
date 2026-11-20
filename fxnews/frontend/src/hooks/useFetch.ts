import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'

interface FetchState<T> {
  key: string
  data?: T
  error?: Error
  /** When `data` was last received (epoch ms). */
  at?: number
}

export interface FetchResult<T> {
  /** Data for the *current* key only (never stale data from a previous key). */
  data: T | undefined
  error: Error | undefined
  /** Epoch ms of the last successful response for the current key. */
  updatedAt: number | undefined
  /** True until the first response (or error) for the current key arrives. */
  loading: boolean
  /** Force a refetch now. */
  reload: () => void
}

/**
 * Minimal data hook: fetch on mount / key change, optional polling, abort on change.
 * `key === null` disables the query. `fetcher` may close over anything encoded in `key`.
 *
 * Polling re-arms after each response: every `refreshMs` normally, every `errorRefreshMs`
 * after a failure (defaults to `refreshMs`) — lets a rate-limited API be retried more slowly.
 */
export function useFetch<T>(
  key: string | null,
  fetcher: (signal: AbortSignal) => Promise<T>,
  refreshMs = 0,
  errorRefreshMs?: number,
): FetchResult<T> {
  const [state, setState] = useState<FetchState<T> | null>(null)
  const [tick, setTick] = useState(0)
  const fetcherRef = useRef(fetcher)

  useLayoutEffect(() => {
    fetcherRef.current = fetcher
  })

  useEffect(() => {
    if (key === null) return
    const k: string = key
    const ctrl = new AbortController()
    const polling = refreshMs > 0 || (errorRefreshMs ?? 0) > 0
    let timer: ReturnType<typeof setTimeout> | undefined

    const schedule = (ms: number) => {
      if (timer) clearTimeout(timer)
      if (ms > 0 && !ctrl.signal.aborted) {
        timer = setTimeout(() => {
          // Skip background tabs; the visibilitychange handler refetches on return.
          if (document.hidden) schedule(ms)
          else run()
        }, ms)
      }
    }

    function run() {
      if (timer) clearTimeout(timer)
      fetcherRef.current(ctrl.signal).then(
        (data) => {
          if (ctrl.signal.aborted) return
          setState({ key: k, data, at: Date.now() })
          schedule(refreshMs)
        },
        (err: unknown) => {
          if (ctrl.signal.aborted) return
          const error = err instanceof Error ? err : new Error(String(err))
          setState((prev) => (prev?.key === k ? { ...prev, error } : { key: k, error }))
          schedule(errorRefreshMs ?? refreshMs)
        },
      )
    }

    const onVisible = () => {
      if (!document.hidden) run()
    }

    run()
    if (polling) document.addEventListener('visibilitychange', onVisible)
    return () => {
      ctrl.abort()
      if (timer) clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [key, tick, refreshMs, errorRefreshMs])

  const reload = useCallback(() => setTick((t) => t + 1), [])
  const current = state && state.key === key ? state : null
  return {
    data: current?.data,
    error: current?.error,
    updatedAt: current?.at,
    loading: key !== null && current === null,
    reload,
  }
}
