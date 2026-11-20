import { useEffect, useMemo, useRef } from 'react'
import type { NewsOut } from '../api/types'
import { useNowSlow } from '../hooks/useClock'
import { dayKey, flag, fmtDayHeading, fmtTime, impactClass } from '../lib/format'
import { itemTime } from '../lib/news'
import { Countdown, EmptyState, ErrorNote, ImpactBadge, Spinner } from './widgets'

const COUNTDOWN_WINDOW_MS = 24 * 3600 * 1000

interface Props {
  items: NewsOut[] // ascending by time
  selectedId?: number
  loading: boolean
  error?: Error
  onRetry: () => void
  onSelect: (item: NewsOut) => void
}

interface DayGroup {
  key: string
  heading: string
  items: NewsOut[]
}

function Value({ label, value, strong }: { label: string; value?: string | null; strong?: boolean }) {
  return (
    <span className={`cal-val${strong ? ' strong' : ''}`}>
      <span className="cal-val-label">{label}</span> {value || '—'}
    </span>
  )
}

export function CalendarList({ items, selectedId, loading, error, onRetry, onSelect }: Props) {
  const now = useNowSlow()
  const scrollRef = useRef<HTMLDivElement>(null)
  const scrolledRef = useRef(false)

  const groups = useMemo<DayGroup[]>(() => {
    const out: DayGroup[] = []
    for (const n of items) {
      const t = itemTime(n)
      const key = dayKey(t)
      const last = out[out.length - 1]
      if (last && last.key === key) last.items.push(n)
      else out.push({ key, heading: fmtDayHeading(t), items: [n] })
    }
    return out
  }, [items])

  const todayKey = dayKey(now)
  const nextId = items.find((n) => itemTime(n) >= now)?.id

  // Scroll once to the next upcoming event when the list first appears.
  useEffect(() => {
    const box = scrollRef.current
    if (!box || scrolledRef.current || items.length === 0) return
    const el = box.querySelector<HTMLElement>('[data-next="1"]')
    if (el) box.scrollTop = Math.max(0, el.offsetTop - 44)
    scrolledRef.current = true
  }, [items.length])

  return (
    <div className="scroll cal-scroll" ref={scrollRef}>
      {error && items.length === 0 ? <ErrorNote error={error} onRetry={onRetry} /> : null}
      {loading && items.length === 0 ? (
        <EmptyState>
          <Spinner label="Loading calendar…" />
        </EmptyState>
      ) : null}
      {!loading && !error && items.length === 0 ? <EmptyState>No events match the filter.</EmptyState> : null}
      {groups.map((g) => (
        <section key={g.key} className="cal-day">
          <h3 className="cal-day-head">
            {g.heading}
            {g.key === todayKey ? <span className="today-pill">Today</span> : null}
          </h3>
          <ul>
            {g.items.map((n) => {
              const t = itemTime(n)
              const upcoming = t > now - 60_000
              const showCountdown = t > now - 60_000 && t - now < COUNTDOWN_WINDOW_MS
              return (
                <li key={n.id}>
                  <button
                    type="button"
                    data-next={n.id === nextId ? '1' : undefined}
                    className={`cal-row ${impactClass(n.impact)}${upcoming ? '' : ' past'}${n.id === selectedId ? ' selected' : ''}`}
                    onClick={() => onSelect(n)}
                  >
                    <span className="cal-time">{fmtTime(t)}</span>
                    <span className="cal-main">
                      <span className="cal-line">
                        <span className="cal-cur">
                          {flag(n.currency)} {n.currency ?? ''}
                        </span>
                        <ImpactBadge impact={n.impact} />
                        {showCountdown ? <Countdown targetMs={t} /> : null}
                      </span>
                      <span className="cal-title" data-preview-url={n.url && /^https?:\/\//.test(n.url) ? n.url : undefined}>{n.title}</span>
                      {n.forecast || n.previous || n.actual ? (
                        <span className="cal-vals">
                          <Value label="Act" value={n.actual} strong={Boolean(n.actual)} />
                          <Value label="Fcst" value={n.forecast} />
                          <Value label="Prev" value={n.previous} />
                        </span>
                      ) : null}
                    </span>
                  </button>
                </li>
              )
            })}
          </ul>
        </section>
      ))}
    </div>
  )
}
