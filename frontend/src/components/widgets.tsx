import type { ReactNode } from 'react'
import type { Bias, Impact } from '../api/types'
import { useNowFast, useNowSlow } from '../hooks/useClock'
import { fmtDateTime, formatSpan, impactClass, timeAgo, toMs } from '../lib/format'
import { Badge, type BadgeTone } from './UI/Badge'
import { UiButton } from './UI/UiButton'

export function TimeAgo({ iso }: { iso?: string | null }) {
  const now = useNowSlow()
  const t = toMs(iso)
  if (t === null) return null
  return (
    <time dateTime={iso ?? undefined} title={fmtDateTime(t)}>
      {timeAgo(t, now)}
    </time>
  )
}

/** Live countdown to `targetMs`; shows "đã ra" once passed. */
export function Countdown({ targetMs }: { targetMs: number }) {
  const now = useNowFast()
  const diff = targetMs - now
  if (diff <= 0) return <span className="countdown past">released</span>
  return <span className={`countdown${diff < 30 * 60_000 ? ' soon' : ''}`}>trong {formatSpan(diff)}</span>
}

const IMPACT_TONE: Record<string, BadgeTone> = { high: 'red', medium: 'orange', low: 'yellow', holiday: 'dim' }

export function ImpactBadge({ impact }: { impact?: Impact | null }) {
  if (!impact) return null
  return <Badge tone={IMPACT_TONE[impact.toLowerCase()] ?? 'dim'}>{impact}</Badge>
}

export function ImpactDot({ impact }: { impact?: Impact | null }) {
  return <span className={`impact-dot ${impactClass(impact)}`} title={impact ?? 'n/a'} />
}

const BIAS_GLYPH: Record<Bias, string> = { bullish: '▲', bearish: '▼', neutral: '◆' }

const BIAS_TONE: Record<Bias, BadgeTone> = { bullish: 'green', bearish: 'red', neutral: 'dim' }

export function BiasBadge({ bias, label }: { bias: Bias; label?: string }) {
  return (
    <Badge tone={BIAS_TONE[bias]}>
      {BIAS_GLYPH[bias]} {label ?? bias}
    </Badge>
  )
}

export function Chip({ children, tone, title }: { children: ReactNode; tone?: 'cur' | 'sym' | 'muted'; title?: string }) {
  return (
    <Badge tone={tone === 'cur' ? 'blue' : 'dim'} title={title} className={tone === 'sym' ? 'text-[var(--text-primary)]' : undefined}>
      {children}
    </Badge>
  )
}

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="spinner-wrap" role="status">
      <span className="spinner" aria-hidden />
      {label ? <span>{label}</span> : null}
    </span>
  )
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>
}

export function ErrorNote({ error, onRetry }: { error: Error; onRetry?: () => void }) {
  return (
    <div className="error-note" role="alert">
      <span>⚠ {error.message}</span>
      {onRetry ? (
        <UiButton tone="danger" size="xs" onClick={onRetry}>
          Retry
        </UiButton>
      ) : null}
    </div>
  )
}
