import type { KeyboardEvent, MouseEvent, ReactNode } from 'react'
import { useSymbolChart } from '../context/symbolChart'
import { toMs } from '../lib/format'

interface Props {
  symbol: string
  /** Event time (epoch ms or ISO string) the chart should scroll to / mark. */
  time?: number | string | null
  className?: string
  title?: string
  children?: ReactNode
}

/**
 * A symbol that opens the chart modal on click. Symbols outside the backend catalog (and
 * anything rendered before the catalog loads) stay plain text. Rendered as a span[role=button]
 * so it can sit inside rows/buttons without nesting <button> elements.
 */
export function SymbolLink({ symbol, time, className, title, children }: Props) {
  const { open, isKnown } = useSymbolChart()
  const label = children ?? symbol
  const base = className ?? ''
  if (!isKnown(symbol)) return <span className={base || undefined}>{label}</span>

  const ms = typeof time === 'string' ? toMs(time) : (time ?? null)
  const go = (e: MouseEvent | KeyboardEvent) => {
    e.stopPropagation()
    e.preventDefault()
    open(symbol, { time: ms })
  }
  return (
    <span
      role="button"
      tabIndex={0}
      className={`symbol-link${base ? ` ${base}` : ''}`}
      title={title ?? `View ${symbol} chart`}
      onClick={go}
      onKeyDown={(e) => {
        // Only swallow the activation keys (rows handle Enter/Space themselves); Esc etc. must still reach the modal.
        if (e.key === 'Enter' || e.key === ' ') go(e)
      }}
    >
      {label}
    </span>
  )
}
