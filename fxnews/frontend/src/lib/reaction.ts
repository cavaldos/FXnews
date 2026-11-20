import { useCallback, useEffect, useSyncExternalStore } from 'react'
import { api } from '../api/client'
import type { ReactionHorizon, ReactionHorizonOut, ReactionLabel, ReactionOut } from '../api/types'

export const HORIZONS: ReactionHorizon[] = ['5m', '15m', '1h', '4h', '1d']
export const HORIZON_STORAGE_KEY = 'fxnews.cal.reactionHorizon'

export function parseHorizon(raw: string | null | undefined): ReactionHorizon {
  return HORIZONS.includes(raw as ReactionHorizon) ? (raw as ReactionHorizon) : '1h'
}

export interface ReactionEntry {
  state: 'loading' | 'done' | 'error'
  data?: ReactionOut
  error?: string
  at: number
}

const BATCH_MAX = 200
const FLUSH_MS = 60
const LIVE_REFRESH_MS = 60_000
const RETRY_MS = 30_000

const cache = new Map<string, ReactionEntry>()
const listeners = new Set<() => void>()
const queue: { key: string; symbol: string; timeMs: number }[] = []
let timer: ReturnType<typeof setTimeout> | undefined
let version = 0

const keyOf = (symbol: string, timeMs: number) => `${symbol}|${Math.round(timeMs / 1000)}`

function emit() {
  version++
  for (const l of listeners) l()
}

function subscribe(l: () => void): () => void {
  listeners.add(l)
  return () => listeners.delete(l)
}

/** Whether a finished entry may be fetched again (a result that can still change, or an old failure). */
function stale(e: ReactionEntry, now: number): boolean {
  if (e.state === 'loading') return false
  if (e.state === 'error') return now - e.at > RETRY_MS
  const d = e.data
  if (!d) return true
  if (d.status === 'ok' && !d.complete) return now - e.at > LIVE_REFRESH_MS
  if (d.status === 'no_data' || d.status === 'pending') return now - e.at > 5 * LIVE_REFRESH_MS
  return false
}

async function flush() {
  timer = undefined
  const batch = queue.splice(0, BATCH_MAX)
  if (queue.length) timer = setTimeout(flush, FLUSH_MS)
  if (!batch.length) return
  try {
    const res = await api.reactionBatch(
      batch.map((b) => ({ id: b.key, symbol: b.symbol, time: new Date(b.timeMs).toISOString() })),
    )
    const byId = new Map(res.results.map((r) => [r.id, r]))
    const now = Date.now()
    for (const b of batch) {
      const r = byId.get(b.key)
      cache.set(
        b.key,
        r ? { state: 'done', data: r, at: now } : { state: 'error', error: 'No result', at: now },
      )
    }
  } catch (err) {
    const now = Date.now()
    for (const b of batch) {
      cache.set(b.key, { state: 'error', error: err instanceof Error ? err.message : String(err), at: now })
    }
  }
  emit()
}

/** Queue reaction lookups (deduplicated, cached, sent to the batch endpoint in chunks). */
export function ensureReactions(items: { symbol: string; timeMs: number }[]): void {
  const now = Date.now()
  let added = false
  for (const { symbol, timeMs } of items) {
    if (!symbol || timeMs > now) continue // future events: nothing to look up
    const key = keyOf(symbol, timeMs)
    const cur = cache.get(key)
    if (cur && !stale(cur, now)) continue
    cache.set(key, { state: 'loading', data: cur?.data, at: cur?.at ?? now })
    queue.push({ key, symbol, timeMs })
    added = true
  }
  if (added) {
    emit()
    timer ??= setTimeout(flush, FLUSH_MS)
  }
}

/** Re-check one pair now, ignoring the cache (the "Check reaction" tool's refresh). */
export function refreshReaction(symbol: string, timeMs: number): void {
  cache.delete(keyOf(symbol, timeMs))
  ensureReactions([{ symbol, timeMs }])
}

export function peekReaction(symbol: string, timeMs: number): ReactionEntry | undefined {
  return cache.get(keyOf(symbol, timeMs))
}

/** Subscribe to the reaction cache; re-renders whenever any entry changes. Returns a lookup. */
export function useReactionLookup(): (symbol: string, timeMs: number) => ReactionEntry | undefined {
  useSyncExternalStore(subscribe, () => version)
  return peekReaction
}

/** Cache version: changes whenever any entry changes (use as a memo dependency for derived data). */
export function useReactionVersion(): number {
  return useSyncExternalStore(subscribe, () => version)
}

/** One pair's reaction, fetched when `enabled`. */
export function useReaction(symbol: string | undefined, timeMs: number, enabled = true): ReactionEntry | undefined {
  useSyncExternalStore(subscribe, () => version)
  useEffect(() => {
    if (enabled && symbol) ensureReactions([{ symbol, timeMs }])
  }, [enabled, symbol, timeMs])
  return symbol ? cache.get(keyOf(symbol, timeMs)) : undefined
}

export function useRefresh(): (symbol: string, timeMs: number) => void {
  return useCallback((s, t) => refreshReaction(s, t), [])
}

// ── presentation helpers ────────────────────────────────────────────────

export const LABEL_GLYPH: Record<ReactionLabel, string> = { up: '▲', down: '▼', sideways: '◆' }
export const LABEL_TEXT: Record<ReactionLabel, string> = { up: 'UP', down: 'DOWN', sideways: 'SIDEWAYS' }
export const LABEL_RANK: Record<ReactionLabel, number> = { up: 1, sideways: 0, down: -1 }

export function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined) return '—'
  return `${v > 0 ? '+' : ''}${v.toFixed(digits)}%`
}

export function horizonOf(r: ReactionOut | undefined, h: ReactionHorizon): ReactionHorizonOut | undefined {
  return r?.horizons.find((x) => x.horizon === h)
}

/** Plain-text tooltip listing every horizon. */
export function reactionTooltip(r: ReactionOut | undefined, symbol: string): string {
  if (!r) return symbol
  const lines = [`${symbol} reaction`]
  if (r.status !== 'ok') return `${lines[0]}: ${r.message ?? r.status}`
  for (const h of r.horizons) {
    const txt =
      h.label && h.return_pct !== null
        ? `${LABEL_GLYPH[h.label]} ${LABEL_TEXT[h.label]} ${fmtPct(h.return_pct)}`
        : h.status === 'pending'
          ? 'pending'
          : 'n/a'
    lines.push(`${h.horizon.padEnd(3)} ${txt}`)
  }
  if (r.max_up_1h != null && r.max_down_1h != null) {
    lines.push(`max 1h: ${fmtPct(r.max_up_1h)} / ${fmtPct(r.max_down_1h)}`)
  }
  return lines.join('\n')
}
