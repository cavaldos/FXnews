import type { SeriesMarker, Time, UTCTimestamp } from 'lightweight-charts'
import type { Candle, Impact, NewsOut, SignalOut } from '../api/types'
import { fmtDateTime, toMs } from './format'
import { impactRank, itemTime } from './news'
import type { ChartTheme } from './theme'

export interface MarkerInfo {
  title: string
  lines: string[]
  newsId?: number
}

export interface BuiltMarkers {
  markers: SeriesMarker<Time>[]
  info: Map<string, MarkerInfo>
}

const MAX_NEWS_MARKERS = 150

export const INTERVAL_SECONDS: Record<string, number> = {
  '1m': 60,
  '5m': 300,
  '15m': 900,
  '30m': 1800,
  '1h': 3600,
  '4h': 14400,
  '1d': 86400,
}

/**
 * Snap `tSec` to the bar that contains it: the last bar opening at or before `tSec`.
 * Returns null when the time is before the data, or too far past the bar (market closed
 * gap, or in the future beyond the latest bar).
 */
export function snapToBar(times: number[], tSec: number, spanSec: number): number | null {
  if (times.length === 0 || tSec < times[0]) return null
  let lo = 0
  let hi = times.length - 1
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1
    if (times[mid] <= tSec) lo = mid
    else hi = mid - 1
  }
  const bar = times[lo]
  return tSec - bar < spanSec * 2 ? bar : null
}

function impactColor(impact: Impact | null | undefined, theme: ChartTheme): string {
  switch (impact) {
    case 'High':
      return theme.high
    case 'Medium':
      return theme.medium
    case 'Low':
      return theme.low
    case 'Holiday':
      return theme.dim
    default:
      return theme.accent
  }
}

function truncate(s: string, max: number): string {
  return s.length > max ? `${s.slice(0, max - 1).trimEnd()}…` : s
}

/** Short marker label, e.g. "USD Core CPI m/m". */
export function markerLabel(n: NewsOut): string {
  if (n.kind === 'calendar') return truncate(`${n.currency ?? ''} ${n.title}`.trim(), 18)
  return truncate(n.title, 20)
}

export function buildMarkers(
  candles: Candle[],
  intervalSec: number,
  news: NewsOut[],
  signals: SignalOut[],
  theme: ChartTheme,
): BuiltMarkers {
  const markers: SeriesMarker<Time>[] = []
  const info = new Map<string, MarkerInfo>()
  if (candles.length === 0) return { markers, info }
  const times = candles.map((c) => c.time)

  // ── news: one marker per bar (highest impact wins, "+N" for the rest) ──
  const groups = new Map<number, NewsOut[]>()
  for (const n of news) {
    const bar = snapToBar(times, Math.floor(itemTime(n) / 1000), intervalSec)
    if (bar === null) continue
    const list = groups.get(bar)
    if (list) list.push(n)
    else groups.set(bar, [n])
  }
  const newsMarkers: { rank: number; marker: SeriesMarker<Time>; info: MarkerInfo; id: string }[] = []
  for (const [bar, list] of groups) {
    list.sort((a, b) => impactRank(b.impact) - impactRank(a.impact) || itemTime(b) - itemTime(a))
    const rep = list[0]
    // Text only for calendar events (a handful per week); headlines are numerous, so they get
    // a bare dot and the title shows in the hover tooltip instead.
    const loud = rep.kind === 'calendar' && (rep.impact === 'High' || rep.impact === 'Medium')
    const id = `news:${rep.id}`
    newsMarkers.push({
      rank: impactRank(rep.impact) * 1e9 + bar,
      id,
      marker: {
        time: bar as UTCTimestamp,
        position: 'aboveBar',
        shape: rep.kind === 'calendar' ? 'square' : 'circle',
        color: impactColor(rep.impact, theme),
        text: loud ? `${markerLabel(rep)}${list.length > 1 ? ` +${list.length - 1}` : ''}` : undefined,
        id,
      },
      info: {
        title: rep.title,
        newsId: rep.id,
        lines: list.slice(0, 6).map((n) => {
          const t = toMs(n.event_time)
          const head = [t ? fmtDateTime(t) : null, n.impact, n.currency].filter(Boolean).join(' · ')
          return `${head ? `${head} — ` : ''}${n.title}`
        }),
      },
    })
  }
  newsMarkers.sort((a, b) => b.rank - a.rank)
  for (const nm of newsMarkers.slice(0, MAX_NEWS_MARKERS)) {
    markers.push(nm.marker)
    info.set(nm.id, nm.info)
  }

  // ── signals: arrows (neutral ignored); best confidence per bar+bias ──
  const best = new Map<string, { bar: number; s: SignalOut }>()
  for (const s of signals) {
    if (s.bias === 'neutral') continue
    const t = toMs(s.created_at)
    if (t === null) continue
    const bar = snapToBar(times, Math.floor(t / 1000), intervalSec)
    if (bar === null) continue
    const key = `${bar}:${s.bias}`
    const prev = best.get(key)
    if (!prev || s.confidence > prev.s.confidence) best.set(key, { bar, s })
  }
  for (const { bar, s } of best.values()) {
    const bull = s.bias === 'bullish'
    const id = `signal:${s.id}`
    markers.push({
      time: bar as UTCTimestamp,
      position: bull ? 'belowBar' : 'aboveBar',
      shape: bull ? 'arrowUp' : 'arrowDown',
      color: bull ? theme.up : theme.down,
      text: `${s.confidence}%`,
      id,
    })
    info.set(id, {
      title: `${bull ? 'Bullish' : 'Bearish'} ${s.symbol} · ${s.confidence}%`,
      lines: [s.title, s.entry_idea ? `Entry: ${s.entry_idea}` : null, s.invalidation ? `Invalid: ${s.invalidation}` : null]
        .filter((x): x is string => Boolean(x))
        .slice(0, 3),
    })
  }

  markers.sort((a, b) => (a.time as number) - (b.time as number))
  return { markers, info }
}
