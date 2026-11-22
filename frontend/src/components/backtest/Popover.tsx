import { useEffect, useRef, useState, type ReactNode } from 'react'

interface Props {
  /** Trigger button content. */
  label: ReactNode
  title?: string
  ariaLabel: string
  /** Extra class for the trigger button (e.g. "on" styling). */
  triggerClass?: string
  align?: 'left' | 'right'
  children: ReactNode
}

/** Button that toggles a small floating panel; closes on outside click or Esc. */
export function Popover({ label, title, ariaLabel, triggerClass, align = 'left', children }: Props) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (e: PointerEvent) => {
      const target = e.target as Element
      // Select menus are portaled to <body>; clicking in one is not an outside click.
      if (!ref.current?.contains(target) && !target.closest?.('.ui-selectbox-menu')) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    window.addEventListener('pointerdown', onDown)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('pointerdown', onDown)
      window.removeEventListener('keydown', onKey)
    }
  }, [open])

  return (
    <div className="bt-pop" ref={ref}>
      <button type="button" className={`bt-popbtn${triggerClass ? ` ${triggerClass}` : ''}${open ? ' open' : ''}`} title={title} aria-label={ariaLabel} aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        {label}
        <span className="bt-caret" aria-hidden>
          ▾
        </span>
      </button>
      {open ? <div className={`bt-popbody ${align}`}>{children}</div> : null}
    </div>
  )
}
