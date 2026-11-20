import { useEffect, useMemo, useRef, useState } from 'react'
import type { BtEvent, BtImpact, Horizon } from '../../api/backtest'
import { useNowSlow } from '../../hooks/useClock'
import { useStoredString } from '../../hooks/useLocalStorage'
import { flag } from '../../lib/format'
import { Badge, type BadgeTone } from '../UI/Badge'
import { EmptyState } from '../widgets'
import { PAGE_SIZES, eventMs, fmtUtc, isReleased, sourceLabel } from './btFormat'
import { Pager } from './Pager'
import { PredictCell } from './PredictCell'
import type { RowActions } from './useRowActions'
import { VerifyCell } from './VerifyCell'

type SortKey = 'time' | 'currency' | 'title' | 'impact' | 'source' | 'kind'
type Dir = 'asc' | 'desc'

const IMPACT_TONE: Record<BtImpact, BadgeTone> = { high: 'red', medium: 'orange', low: 'yellow' }
const IMPACT_RANK: Record<BtImpact, number> = { high: 3, medium: 2, low: 1 }
const DEFAULT_PAGE_SIZE = 50

interface Col {
  key: SortKey | null
  label: string
  cls?: string
}

function sortValue(e: BtEvent, key: SortKey): number | string | null {
  switch (key) {
    case 'time':
      return eventMs(e)
    case 'currency':
      return e.currency || null
    case 'title':
      return e.title.toLowerCase()
    case 'impact':
      return IMPACT_RANK[e.impact]
    case 'source':
      return sourceLabel(e.source).toLowerCase()
    case 'kind':
      return e.kind
  }
}

function compare(a: number | string, b: number | string): number {
  if (typeof a === 'number' && typeof b === 'number') return a - b
  return String(a).localeCompare(String(b))
}

function sortEvents(events: BtEvent[], key: SortKey, dir: Dir): BtEvent[] {
  const sign = dir === 'asc' ? 1 : -1
  return [...events].sort((x, y) => {
    const a = sortValue(x, key)
    const b = sortValue(y, key)
    // Empty cells always sink to the bottom, whichever way we sort.
    if (a === null || b === null) return a === b ? eventMs(x) - eventMs(y) : a === null ? 1 : -1
    return sign * compare(a, b) || eventMs(y) - eventMs(x) || x.id - y.id
  })
}

function parseSort(raw: string): { key: SortKey; dir: Dir } {
  const [key, dir] = raw.split(':')
  const keys: SortKey[] = ['time', 'currency', 'title', 'impact', 'source', 'kind']
  return { key: keys.includes(key as SortKey) ? (key as SortKey) : 'time', dir: dir === 'asc' ? 'asc' : 'desc' }
}

interface Props {
  /** Already filtered; sorted here. */
  events: BtEvent[]
  /** Changes whenever the filters or the date range change: back to page 1. */
  resetKey: string
  loading: boolean
  horizon: Horizon
  picked: string[]
  actions: RowActions
  onOpenPrice: (id: number) => void
  onOpenPredict: (id: number) => void
}

/** One row per event, styled like the Calendar table (ct-* classes), plus Source / Verify history / AI predict columns. */
export function BacktestTable({ events, resetKey, loading, horizon, picked, actions, onOpenPrice, onOpenPredict }: Props) {
  const [sortRaw, setSortRaw] = useStoredString('fxnews.bt.sort', 'time:desc')
  const { key: sortKey, dir } = parseSort(sortRaw)
  const now = useNowSlow()
  const [sizeRaw, setSizeRaw] = useStoredString('fxnews.bt.pageSize', String(DEFAULT_PAGE_SIZE))
  const pageSize = PAGE_SIZES.includes(Number(sizeRaw)) ? Number(sizeRaw) : DEFAULT_PAGE_SIZE
  // The page is only valid for the filters/sort/size it was chosen under; anything else is page 1.
  const pageKey = `${resetKey}|${sortRaw}|${pageSize}`
  const [pageState, setPageState] = useState({ key: pageKey, n: 1 })
  const wrapRef = useRef<HTMLDivElement>(null)

  const sorted = useMemo(() => sortEvents(events, sortKey, dir), [events, sortKey, dir])
  const pages = Math.max(1, Math.ceil(sorted.length / pageSize))
  const page = Math.min(pageState.key === pageKey ? pageState.n : 1, pages)
  const shown = useMemo(() => sorted.slice((page - 1) * pageSize, page * pageSize), [sorted, page, pageSize])
  const goTo = (n: number) => setPageState({ key: pageKey, n })
  useEffect(() => {
    wrapRef.current?.scrollTo({ top: 0 })
  }, [pageKey, page])

  const cols: Col[] = [
    { key: 'time', label: 'Time (UTC)' },
    { key: 'currency', label: 'Currency' },
    { key: 'title', label: 'Event' },
    { key: 'kind', label: 'Kind' },
    { key: 'source', label: 'Source' },
    { key: 'impact', label: 'Impact' },
    { key: null, label: 'Actual', cls: 'num' },
    { key: null, label: 'Forecast', cls: 'num' },
    { key: null, label: 'Previous', cls: 'num' },
    { key: null, label: `Verify history · ${horizon}` },
    { key: null, label: 'AI predict' },
  ]

  const sortBy = (key: SortKey) =>
    setSortRaw(key === sortKey ? `${key}:${dir === 'asc' ? 'desc' : 'asc'}` : `${key}:${key === 'time' || key === 'impact' ? 'desc' : 'asc'}`)

  return (
    <div className="ct-wrap bt-tablewrap" ref={wrapRef}>
      <table className="ct-table bt-table">
        <thead>
          <tr>
            {cols.map((c) => (
              <th key={c.label} className={c.cls} aria-sort={c.key && c.key === sortKey ? (dir === 'asc' ? 'ascending' : 'descending') : undefined}>
                {c.key ? (
                  <button type="button" className={c.key === sortKey ? 'on' : ''} onClick={() => sortBy(c.key as SortKey)}>
                    {c.label}
                    <span className="ct-arrow" aria-hidden>
                      {c.key === sortKey ? (dir === 'asc' ? '▲' : '▼') : ''}
                    </span>
                  </button>
                ) : (
                  <span className={`ct-th-static${c.cls === 'num' ? ' right' : ''}`}>{c.label}</span>
                )}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {shown.map((e) => {
            const t = fmtUtc(e.time)
            const released = isReleased(e, now)
            const cal = e.kind === 'calendar'
            const state = actions.state[e.id]
            return (
              <tr key={e.id} className={`imp-${e.impact}${released ? '' : ' future'}`}>
                <td className="ct-time">
                  <span className="ct-date">{t.date}</span> <span className="ct-hm">{t.hm}</span>
                </td>
                <td className="ct-cur">{e.currency ? `${flag(e.currency)} ${e.currency}`.trim() : <span className="dim">—</span>}</td>
                <td className="ct-title" title={e.summary ?? e.title} data-preview-url={e.url && /^https?:\/\//.test(e.url) ? e.url : undefined}>
                  {e.title}
                </td>
                <td>
                  <span className={`ct-kind${cal ? '' : ' headline'}`} title={e.category ?? undefined}>
                    {cal ? 'Economic' : 'Headline'}
                  </span>
                </td>
                <td className="ct-src" title={e.source}>
                  {sourceLabel(e.source)}
                </td>
                <td>
                  <Badge tone={IMPACT_TONE[e.impact]}>{e.impact}</Badge>
                </td>
                <td className="num strong">{cal && e.actual ? e.actual : <span className="dim">—</span>}</td>
                <td className="num">{cal && e.forecast ? e.forecast : <span className="dim">—</span>}</td>
                <td className="num">{cal && e.previous ? e.previous : <span className="dim">—</span>}</td>
                <VerifyCell event={e} released={released} horizon={horizon} state={state} onVerify={() => void actions.verify(e.id)} onOpen={() => onOpenPrice(e.id)} />
                <PredictCell event={e} horizon={horizon} picked={picked} state={state} onPredict={() => void actions.predict(e.id, picked)} onOpen={() => onOpenPredict(e.id)} />
              </tr>
            )
          })}
        </tbody>
      </table>
      {sorted.length > 0 ? <Pager total={sorted.length} page={page} pageSize={pageSize} onPage={goTo} onPageSize={(n) => setSizeRaw(String(n))} /> : null}
      {sorted.length === 0 ? <EmptyState>{loading ? 'Loading…' : 'No events match the filters in this range. Try “Backfill range”.'}</EmptyState> : null}
    </div>
  )
}
