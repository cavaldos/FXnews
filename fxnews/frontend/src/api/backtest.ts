// Backtest dataset API (backend/app/routers/backtest.py). Kept apart from client.ts so the shared client stays small.
import { ApiError } from './client'

export type BtImpact = 'high' | 'medium' | 'low'
export type Direction = 'up' | 'down' | 'flat'
export const HORIZONS = ['5m', '15m', '1h', '4h', '1d'] as const
export type Horizon = (typeof HORIZONS)[number]

const HORIZON_MINUTES: Record<Horizon, number> = { '5m': 5, '15m': 15, '1h': 60, '4h': 240, '1d': 1440 }
const INTERVAL_MINUTES: Record<string, number> = { '1m': 1, '5m': 5, '15m': 15, '30m': 30, '1h': 60, '4h': 240, '1d': 1440 }

/** True when the outcome was priced from candles coarser than the horizon (e.g. +5m on 1h candles): it is always null. */
export function horizonTooFine(interval: string | null | undefined, horizon: Horizon): boolean {
  const step = interval ? INTERVAL_MINUTES[interval] : undefined
  return step !== undefined && HORIZON_MINUTES[horizon] < step
}

export interface BtOutcome {
  symbol: string
  /** candle interval the outcome was priced from: the finest one whose history reached the event (5m, 15m, 1h, 1d) */
  interval: string
  price_at_release: number
  returns: Record<Horizon, number | null>
  prices: Record<Horizon, number | null>
  directions: Record<Horizon, Direction | null>
  max_up_1h: number | null
  max_down_1h: number | null
  direction: Direction | null
  direction_horizon: string | null
  complete: boolean
}

export interface BtPrediction {
  id: number
  symbol: string
  direction: Direction
  confidence: number | null
  horizon: string | null
  reasoning: string | null
  latency_ms: number | null
  created_at: string | null
}

export interface BtEvent {
  id: number
  time: string
  kind: 'calendar' | 'headline'
  source: string
  currency: string | null
  country: string | null
  title: string
  summary: string | null
  url: string | null
  impact: BtImpact
  category: string | null
  actual: string | null
  forecast: string | null
  previous: string | null
  symbols: string[]
  outcomes: BtOutcome[]
  /** model id ("provider:model") -> one prediction per symbol */
  predictions: Record<string, BtPrediction[]>
}

export interface BtEventsOut {
  total: number
  models: string[]
  events: BtEvent[]
}

export interface Tally {
  n: number
  hits: number
  hit_rate: number | null
  directional_n: number
  directional_hits: number
  directional_hit_rate: number | null
}

export interface ModelSummary {
  model: string
  predictions: number
  avg_confidence: number | null
  overall: Tally
  by_impact: Partial<Record<BtImpact, Tally>>
  by_horizon: Partial<Record<Horizon, Tally>>
  by_impact_horizon: Partial<Record<BtImpact, Partial<Record<Horizon, Tally>>>>
}

export interface BtSummary {
  primary_horizon: Horizon
  events: number
  events_by_impact: Partial<Record<BtImpact, number>>
  events_with_outcomes: number
  outcomes: number
  /** candle interval -> number of outcomes priced from it */
  outcomes_by_interval?: Record<string, number>
  baseline_by_horizon: Partial<Record<Horizon, { n: number; majority: Direction; hit_rate: number | null; distribution: Record<string, number> }>>
  models: ModelSummary[]
}

export interface BtModelOption {
  id: string
  provider: string
  model: string
  problem: string | null
}

export interface BtModelsOut {
  models: BtModelOption[]
  providers: { name: string; label: string | null; problem: string | null }[]
}

export interface BtJob {
  id: string
  kind: 'backfill' | 'predict'
  status: 'running' | 'done' | 'error' | 'cancelled'
  message: string
  result: Record<string, unknown> | null
  error: string | null
  started_at: string
  finished_at: string | null
}

/** Report of POST /events/{id}/verify: what the price-outcome pass did for one event. */
export interface BtVerifyReport {
  outcomes_created: number
  outcomes_updated: number
  outcomes_skipped_future: number
  outcomes_skipped_no_data: number
  outcomes_skipped_not_covered: number
  outcomes_skipped_complete: number
  outcomes_by_interval: Record<string, number>
  symbols_without_candles: string[]
  errors: string[]
}

export interface BtVerifyOut {
  event: BtEvent
  verify: BtVerifyReport
}

export interface BtModelRun {
  model: string
  status: 'ok' | 'skipped' | 'error'
  error: string | null
  predictions_created: number
  latency_ms: number | null
}

export interface BtPredictEventOut {
  event: BtEvent
  results: BtModelRun[]
}

/** Exact text a model is shown for one event (only information published before the release). */
export interface BtPromptOut {
  system: string
  prompt: string
}

export interface BtQuery {
  from?: string
  to?: string
  impact?: BtImpact[]
  symbol?: string
  horizon?: Horizon
  limit?: number
}

function qs(q: BtQuery): string {
  const p = new URLSearchParams()
  if (q.from) p.set('from', q.from)
  if (q.to) p.set('to', q.to)
  if (q.impact && q.impact.length) p.set('impact', q.impact.join(','))
  if (q.symbol) p.set('symbol', q.symbol)
  if (q.horizon) p.set('horizon', q.horizon)
  if (q.limit) p.set('limit', String(q.limit))
  const s = p.toString()
  return s ? `?${s}` : ''
}

async function call<T>(method: 'GET' | 'POST', path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  let res: Response
  try {
    res = await fetch(`/api/backtest${path}`, {
      method,
      signal,
      headers: { Accept: 'application/json', ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}) },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
  } catch (err) {
    if (signal?.aborted) throw signal.reason ?? err
    throw new ApiError('Cannot reach the backend', 0)
  }
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`.trim()
    try {
      const json = (await res.json()) as { detail?: unknown }
      if (typeof json.detail === 'string') detail = json.detail
      else if (Array.isArray(json.detail)) detail = json.detail.map((d) => (d as { msg?: string }).msg ?? String(d)).join('; ')
    } catch {
      /* not JSON */
    }
    throw new ApiError(detail, res.status)
  }
  return (await res.json()) as T
}

export const backtestApi = {
  events: (q: BtQuery, signal?: AbortSignal) => call<BtEventsOut>('GET', `/events${qs({ limit: 2000, ...q })}`, undefined, signal),
  summary: (q: BtQuery, signal?: AbortSignal) => call<BtSummary>('GET', `/summary${qs(q)}`, undefined, signal),
  event: (id: number, signal?: AbortSignal) => call<BtEvent>('GET', `/events/${id}`, undefined, signal),
  verifyEvent: (id: number, force = false) => call<BtVerifyOut>('POST', `/events/${id}/verify`, { force }),
  predictEvent: (id: number, models: string[], force = false) => call<BtPredictEventOut>('POST', `/events/${id}/predict`, { models, force }),
  prompt: (id: number, horizon: Horizon, signal?: AbortSignal) => call<BtPromptOut>('GET', `/events/${id}/prompt?horizon=${horizon}`, undefined, signal),
  models: (signal?: AbortSignal) => call<BtModelsOut>('GET', '/models', undefined, signal),
  job: (id: string, signal?: AbortSignal) => call<BtJob>('GET', `/jobs/${id}`, undefined, signal),
  /** Recent jobs, newest first. */
  jobs: (signal?: AbortSignal) => call<BtJob[]>('GET', '/jobs', undefined, signal),
  /** Ask a running job to stop; stored predictions/outcomes are kept. */
  cancelJob: (id: string) => call<BtJob>('POST', `/jobs/${id}/cancel`),
  backfill: (from: string, to: string) => call<BtJob>('POST', '/backfill', { from, to: to || undefined }),
  predict: (body: { from: string; to?: string; models: string[]; min_impact: BtImpact; limit?: number }) =>
    call<BtJob>('POST', '/predict', { ...body, to: body.to || undefined }),
}
