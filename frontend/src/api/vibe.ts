import { request } from './client'

/** GET /api/vibe/status — always 200; `enabled` / `reachable` / `error` describe the sidecar state. */
export interface VibeStatus {
  enabled: boolean
  reachable: boolean
  base_url: string
  server_name: string | null
  server_version: string | null
  tool_count: number | null
  error: string | null
}

export interface SwarmVariable {
  name: string
  description: string
  required: boolean
}

export interface SwarmPreset {
  name: string
  title: string
  description: string
  agent_count: number
  variables: SwarmVariable[]
  source: string
}

export interface SwarmTask {
  id?: string | null
  agent_id?: string | null
  status?: string | null
  summary?: string | null
  error?: string | null
}

export interface SwarmRun {
  run_id: string
  /** pending | running | completed | failed | cancelled */
  status: string
  preset?: string | null
  created_at?: string | null
  completed_at?: string | null
  error?: string | null
  tasks: SwarmTask[]
  final_report?: string | null
  report_is_partial: boolean
  total_input_tokens?: number | null
  total_output_tokens?: number | null
  is_stale: boolean
  /** True once the status is completed / failed / cancelled — stop polling when set. */
  ready: boolean
}

export type StrategyKind = 'sma_cross' | 'rsi_reversion' | 'donchian_breakout' | 'custom'

export interface StrategySpec {
  kind: StrategyKind
  params: Record<string, number>
  /** kind='custom': Python source defining class SignalEngine. */
  code?: string | null
}

export interface BacktestRequest {
  codes: string[]
  start_date: string
  end_date: string
  interval: string
  source: string
  strategy: StrategySpec
  initial_cash: number
  leverage: number
  spread_pips?: number
  slippage_pips: number
  swap_enabled: boolean
}

export interface BacktestResult {
  metrics: Record<string, unknown>
  /** Numbers or {date/time, value/equity} objects — normalized defensively before charting. */
  equity_preview: unknown[]
  artifact_paths: Record<string, unknown>
  run_dir?: string | null
  raw: Record<string, unknown>
}

export interface BacktestJob {
  id: string
  status: 'running' | 'completed' | 'failed'
  created_at: string
  finished_at?: string | null
  error?: string | null
  request?: BacktestRequest | null
  result?: BacktestResult | null
}

export interface MacroObservation {
  date: string
  value: number | string | null
}

export interface MacroSeries {
  series_id: string
  source: string
  count: number
  observations_available?: number | null
  truncated: boolean
  observations: MacroObservation[]
}

export interface MacroQuery {
  start?: string
  end?: string
  limit?: number
}

export const vibeApi = {
  status: (signal?: AbortSignal) => request<VibeStatus>('GET', '/vibe/status', { signal }),
  presets: (signal?: AbortSignal) => request<SwarmPreset[]>('GET', '/vibe/presets', { signal }),
  startSwarm: (presetName: string, variables: Record<string, string>, signal?: AbortSignal) =>
    request<SwarmRun>('POST', '/vibe/swarm', { body: { preset_name: presetName, variables }, signal }),
  swarmRun: (runId: string, signal?: AbortSignal) => request<SwarmRun>('GET', `/vibe/swarm/${runId}`, { signal }),
  startBacktest: (body: BacktestRequest, signal?: AbortSignal) => request<BacktestJob>('POST', '/vibe/backtest', { body, signal }),
  backtestJob: (jobId: string, signal?: AbortSignal) => request<BacktestJob>('GET', `/vibe/backtest/${jobId}`, { signal }),
  macro: (seriesId: string, q: MacroQuery = {}, signal?: AbortSignal) =>
    request<MacroSeries>('GET', `/vibe/macro/${encodeURIComponent(seriesId)}`, { params: { ...q }, signal }),
}

/** Normalize the sidecar's loose equity preview into chartable {time, value} points (skips non-numeric). */
export function equityPoints(preview: unknown[]): { time: string | number; value: number }[] {
  const points: { time: string | number; value: number }[] = []
  let i = 0
  for (const item of preview) {
    if (typeof item === 'number' && Number.isFinite(item)) {
      // Bare numbers carry no timestamps; use the index so the curve stays monotonic.
      points.push({ time: i, value: item })
    } else if (item && typeof item === 'object') {
      const o = item as Record<string, unknown>
      const raw = (o.date ?? o.time ?? o.datetime ?? o.timestamp) as unknown
      const value = o.equity ?? o.value ?? o.equity_value ?? o.balance
      const num = typeof value === 'string' ? Number(value) : typeof value === 'number' ? value : NaN
      let time: string | number | null = null
      if (typeof raw === 'string') {
        time = /^\d{4}-\d{2}-\d{2}/.test(raw) ? raw.slice(0, 10) : Number.isFinite(Date.parse(raw)) ? Math.floor(Date.parse(raw) / 1000) : null
      } else if (typeof raw === 'number' && Number.isFinite(raw)) {
        time = raw > 1e12 ? Math.floor(raw / 1000) : raw // ms vs s epoch
      }
      if (time !== null && Number.isFinite(num)) points.push({ time, value: num })
    }
    i++
  }
  return points
}
