import { useEffect, useLayoutEffect, useRef, type ReactNode } from 'react'

interface Props {
  onClose: () => void
  /** Optional heading bar; omit when the content brings its own header (e.g. NewsDetail). */
  title?: string
  /** `wide`: large chart-style dialog (~90vw x 80vh). */
  size?: 'wide'
  /** Accessible name when there is no visible `title`. */
  ariaLabel?: string
  children: ReactNode
}

// Open modals, oldest first: Esc closes only the topmost one (e.g. a chart opened over a news detail).
const stack: symbol[] = []

/** Centered frosted dialog with backdrop; closes on Esc or backdrop click. */
export function Modal({ onClose, title, size, ariaLabel, children }: Props) {
  const closeRef = useRef(onClose)
  useLayoutEffect(() => {
    closeRef.current = onClose
  })

  useEffect(() => {
    const id = Symbol('modal')
    stack.push(id)
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && stack[stack.length - 1] === id) closeRef.current()
    }
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('keydown', onKey)
      const i = stack.indexOf(id)
      if (i >= 0) stack.splice(i, 1)
    }
  }, [])

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className={`modal-card${size === 'wide' ? ' wide' : ''}`}
        role="dialog"
        aria-modal="true"
        aria-label={title ?? ariaLabel}
        onClick={(e) => e.stopPropagation()}
      >
        {title ? (
          <div className="modal-head">
            <b>{title}</b>
            <button type="button" className="modal-x" onClick={onClose} aria-label="Close">
              ×
            </button>
          </div>
        ) : null}
        {children}
      </div>
    </div>
  )
}
