import type { Toast } from '../hooks/useToasts'
import { impactClass } from '../lib/format'

interface Props {
  toasts: Toast[]
  onDismiss: (id: number) => void
}

export function Toasts({ toasts, onDismiss }: Props) {
  if (toasts.length === 0) return null
  return (
    <div className="toasts" role="status" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className={`toast toast-${t.tone} ${t.tone === 'news' ? impactClass(t.impact) : ''}`}>
          <button
            type="button"
            className="toast-body"
            onClick={() => {
              t.onClick?.()
              onDismiss(t.id)
            }}
            disabled={!t.onClick}
          >
            <strong>{t.title}</strong>
            {t.text ? <span>{t.text}</span> : null}
          </button>
          <button type="button" className="toast-x" onClick={() => onDismiss(t.id)} aria-label="Close">
            ×
          </button>
        </div>
      ))}
    </div>
  )
}
