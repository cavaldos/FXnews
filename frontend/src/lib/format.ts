import type { Impact } from '../api/types'

const TZ_SUFFIX = /(Z|[+-]\d{2}:?\d{2})$/i

/**
 * Parse an API timestamp to epoch ms. The contract says "UTC with Z", but be lenient:
 * a naive ISO string (no zone, as SQLite-backed models often serialize) is treated as UTC.
 */
export function toMs(iso?: string | null): number | null {
  if (!iso) return null
  let s = iso.trim()
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) s += 'T00:00:00Z'
  else if (!TZ_SUFFIX.test(s)) s = `${s.replace(' ', 'T')}Z`
  const ms = Date.parse(s)
  return Number.isNaN(ms) ? null : ms
}

export function timeAgo(ms: number, now: number): string {
  const diff = Math.round((now - ms) / 1000)
  if (diff < -60) return `in ${formatSpan(-diff * 1000)}`
  if (diff < 45) return 'just now'
  const m = Math.round(diff / 60)
  if (m < 60) return `${m} min ago`
  const h = Math.round(diff / 3600)
  if (h < 24) return `${h} h ago`
  const d = Math.round(diff / 86400)
  if (d < 30) return `${d} d ago`
  return fmtDate(ms)
}

/** "2h 15m", "12m 30s", "45s" */
export function formatSpan(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000))
  const d = Math.floor(total / 86400)
  const h = Math.floor((total % 86400) / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  if (d > 0) return `${d}d ${h}h`
  if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m`
  if (m >= 10) return `${m}m`
  if (m > 0) return `${m}m ${String(s).padStart(2, '0')}s`
  return `${s}s`
}

const pad = (n: number) => String(n).padStart(2, '0')

/** HH:mm in the viewer's timezone. */
export function fmtTime(ms: number): string {
  const d = new Date(ms)
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** dd/MM in the viewer's timezone. */
export function fmtDate(ms: number): string {
  const d = new Date(ms)
  return `${pad(d.getDate())}/${pad(d.getMonth() + 1)}`
}

export function fmtDateTime(ms: number): string {
  return `${fmtDate(ms)} ${fmtTime(ms)}`
}

const dayFmt = new Intl.DateTimeFormat('en-GB', { weekday: 'long', day: '2-digit', month: '2-digit' })

/** "Wednesday 12/11" in local time. */
export function fmtDayHeading(ms: number): string {
  return dayFmt.format(new Date(ms))
}

/** Local calendar-day key, used to group the calendar. */
export function dayKey(ms: number): string {
  const d = new Date(ms)
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

export function fmtPrice(price: number, digits: number): string {
  if (!Number.isFinite(price)) return '—'
  return price.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

export function fmtPct(pct: number): string {
  if (!Number.isFinite(pct)) return '—'
  return `${pct > 0 ? '+' : ''}${pct.toFixed(2)}%`
}

/** "anthropic/claude-sonnet-5-5" -> "claude-sonnet-5-5" */
export function shortModel(id: string): string {
  const parts = id.split('/')
  return parts[parts.length - 1] || id
}

const FLAGS: Record<string, string> = {
  USD: '🇺🇸',
  EUR: '🇪🇺',
  GBP: '🇬🇧',
  JPY: '🇯🇵',
  AUD: '🇦🇺',
  NZD: '🇳🇿',
  CAD: '🇨🇦',
  CHF: '🇨🇭',
  CNY: '🇨🇳',
}

export function flag(currency?: string | null): string {
  return (currency && FLAGS[currency]) || ''
}

export function impactClass(impact?: Impact | null): string {
  switch (impact) {
    case 'High':
      return 'imp-high'
    case 'Medium':
      return 'imp-medium'
    case 'Low':
      return 'imp-low'
    case 'Holiday':
      return 'imp-holiday'
    default:
      return 'imp-none'
  }
}

export const KIND_LABEL: Record<string, string> = {
  pre_event: 'Pre-event',
  headline: 'Headline',
  ad_hoc: 'Ask AI',
}
