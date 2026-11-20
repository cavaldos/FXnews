import { useState, type ReactNode } from 'react'

export interface ColumnTab {
  key: string
  label: string
  content: ReactNode
}

interface Props {
  tabs: ColumnTab[]
  active: string
  onChange: (key: string) => void
  className?: string
  ariaLabel: string
}

/**
 * Segmented tab strip + panes. A pane is mounted the first time its tab is opened and then only hidden,
 * so the price chart keeps its state and each embedded widget loads once (and only when it is wanted).
 */
export function TabbedColumn({ tabs, active, onChange, className, ariaLabel }: Props) {
  const current = tabs.some((t) => t.key === active) ? active : tabs[0].key
  const [visited, setVisited] = useState<ReadonlySet<string>>(() => new Set([current]))

  const select = (key: string) => {
    setVisited((prev) => (prev.has(key) ? prev : new Set(prev).add(key)))
    onChange(key)
  }

  return (
    <div className={`tab-col${className ? ` ${className}` : ''}`}>
      <div className="col-tabs" role="tablist" aria-label={ariaLabel}>
        {tabs.map((t) => (
          <button key={t.key} type="button" role="tab" aria-selected={t.key === current} className={t.key === current ? 'active' : ''} onClick={() => select(t.key)}>
            {t.label}
          </button>
        ))}
      </div>
      {tabs.map((t) => (
        <div key={t.key} className="tab-pane" role="tabpanel" hidden={t.key !== current}>
          {t.key === current || visited.has(t.key) ? t.content : null}
        </div>
      ))}
    </div>
  )
}
