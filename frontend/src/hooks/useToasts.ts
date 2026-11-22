import { useCallback, useRef, useState } from 'react'
import type { Impact } from '../api/types'

export interface Toast {
  id: number
  tone: 'info' | 'error' | 'news'
  title: string
  text?: string
  impact?: Impact | null
  /** Optional click action (e.g. select the news item). */
  onClick?: () => void
}

const MAX_TOASTS = 4

export interface ToastApi {
  toasts: Toast[]
  push: (toast: Omit<Toast, 'id'>, ttlMs?: number) => void
  dismiss: (id: number) => void
}

export function useToasts(): ToastApi {
  const [toasts, setToasts] = useState<Toast[]>([])
  const nextId = useRef(1)

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id))
  }, [])

  const push = useCallback(
    (toast: Omit<Toast, 'id'>, ttlMs = 8000) => {
      const id = nextId.current++
      setToasts((prev) => [...prev, { ...toast, id }].slice(-MAX_TOASTS))
      setTimeout(() => dismiss(id), ttlMs)
    },
    [dismiss],
  )

  return { toasts, push, dismiss }
}
