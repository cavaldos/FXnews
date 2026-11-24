// Mirror of backend/app/schemas.py — keep in sync with that file.
// Every datetime is an ISO-8601 string (UTC); candle times are unix seconds.

export type NewsKind = 'calendar' | 'headline'
export type Impact = 'High' | 'Medium' | 'Low' | 'Holiday'
export type Bias = 'bullish' | 'bearish' | 'neutral'
export type AnalysisKind = 'pre_event' | 'headline' | 'ad_hoc'
export type SignalSource = 'rules' | 'llm'
export type Interval = '1m' | '5m' | '15m' | '30m' | '1h' | '4h' | '1d'
export type SymbolKind = 'fx' | 'metal' | 'energy' | 'index' | 'crypto'

export const INTERVALS: Interval[] = ['1m', '5m', '15m', '30m', '1h', '4h', '1d']

// ── symbols / market ────────────────────────────────────────────────

export interface SymbolOut {
  symbol: string
  base: string
  quote: string
  kind: SymbolKind
  digits: number
  yahoo: string
  tv: string // TradingView ticker from the backend catalog (e.g. OANDA:XAUUSD)
}

export interface Candle {
  time: number
  open: number
  high: number
  low: number
  close: number
  volume?: number | null
}

export interface CandlesOut {
  symbol: string
  interval: Interval
  source: string
  candles: Candle[]
  stale?: boolean
  /** unix seconds the stale data was fetched; set only when stale */
  as_of?: number | null
}

export interface QuoteOut {
  symbol: string
  price: number
  change: number
  change_pct: number
  time: number
}

// ── news ────────────────────────────────────────────────────────────

export interface NewsOut {
  id: number
  kind: NewsKind
  source: string
  title: string
  summary?: string | null
  url?: string | null
  currency?: string | null
  currencies: string[]
  symbols: string[]
  impact?: Impact | null
  event_time?: string | null
  actual?: string | null
  forecast?: string | null
  previous?: string | null
  rule_bias?: Bias | null
  fetched_at: string
  analysis_id?: number | null
}

// ── analysis / signals ──────────────────────────────────────────────

export interface TradeIdea {
  symbol: string
  bias: Bias
  confidence: number
  horizon?: string | null
  entry_idea?: string | null
  invalidation?: string | null
  targets?: string | null
}

export interface Scenario {
  condition: string
  outcome: string
}

export interface CurrencyImpact {
  currency: string
  direction: 'stronger' | 'weaker' | 'neutral'
  strength: number
}

export interface AnalysisOut {
  id: number
  kind: AnalysisKind
  news_id?: number | null
  symbol?: string | null
  model: string
  summary: string
  currency_impact: CurrencyImpact[]
  trades: TradeIdea[]
  scenarios: Scenario[]
  risk_notes?: string | null
  question?: string | null
  error?: string | null
  latency_ms?: number | null
  notified: boolean
  created_at: string
}

export interface SignalOut {
  id: number
  analysis_id?: number | null
  news_id?: number | null
  symbol: string
  bias: Bias
  confidence: number
  source: SignalSource
  model?: string | null
  horizon?: string | null
  entry_idea?: string | null
  invalidation?: string | null
  title?: string | null
  created_at: string
}

export interface AnalyzeRequest {
  model?: string | null
}

export interface AdviceRequest {
  symbol: string
  question?: string | null
  model?: string | null
  interval?: Interval
}

export interface ModelOut {
  id: string
  name: string
  is_default: boolean
  context_length?: number | null
  prompt_price?: number | null
  completion_price?: number | null
}

export interface HealthOut {
  status: 'ok'
  version: string
  llm_enabled: boolean
  discord_enabled: boolean
  telegram_enabled?: boolean
  default_model: string
  last_ingest_at?: string | null
}

// ── notifications (GET/POST /api/notify/settings) ─────────────────────

export interface NotifyRules {
  min_impact: Impact
  pre_event_lead_minutes: number
  notify_analyses: boolean
  notify_pre_event: boolean
  min_confidence: number
}

export type NotifyChannel = 'telegram' | 'discord'

export interface NotifyAccount {
  /** "<channel>:<name>" */
  id: string
  channel: NotifyChannel
  name: string
  enabled: boolean
  chat_id: string
  /** The secret is stored in backend/notify.yaml and never sent back to the browser. */
  secret_set: boolean
  /** "file" = backend/notify.yaml, "env" = the .env fallback (not written to the file). */
  source: 'file' | 'env'
}

export interface NotifySettingsOut {
  rules: NotifyRules
  accounts: NotifyAccount[]
  /** The YAML file the settings come from ("" = none yet). */
  file: string
}

/** Partial update: a secret left out keeps its value, a secret set to '' is cleared. */
export interface NotifyAccountIn {
  /** The id of the stored account (so renaming it keeps its secret). */
  id?: string
  channel: NotifyChannel
  name: string
  enabled: boolean
  chat_id?: string
  bot_token?: string | null
  webhook_url?: string | null
  source?: 'file' | 'env'
}

export interface NotifySettingsIn {
  rules?: Partial<NotifyRules>
  accounts?: NotifyAccountIn[]
}

export interface NotifyTestOut {
  sent: boolean
  accounts: Record<string, boolean>
}

export interface IngestResult {
  inserted: number
  analyzed: number
  errors: string[]
  /** informational, e.g. a rate-limited source skipped on purpose */
  notes?: string[]
}

// ── market reaction (GET /api/reaction, POST /api/reaction/batch) ──────────

export type ReactionHorizon = '5m' | '15m' | '1h' | '4h' | '1d'
export type ReactionLabel = 'up' | 'down' | 'sideways'

export interface ReactionHorizonOut {
  horizon: ReactionHorizon
  price: number | null
  return_pct: number | null
  label: ReactionLabel | null
  /** pending = not elapsed yet; unavailable = no candle that fine / market closed. */
  status: 'ok' | 'pending' | 'unavailable'
}

export interface ReactionOut {
  symbol: string
  time: string
  status: 'ok' | 'pending' | 'no_data' | 'unsupported_symbol' | 'error'
  source?: 'backtest' | 'live' | null
  interval?: string | null
  price_at_release?: number | null
  horizons: ReactionHorizonOut[]
  max_up_1h?: number | null
  max_down_1h?: number | null
  max_status?: 'ok' | 'pending' | 'unavailable'
  complete?: boolean
  message?: string
}

export interface LinkPreviewOut {
  ok: boolean
  url: string
  final_url: string
  title: string
  site_name: string
  favicon: string
  image: string
  description: string
  published: string
  author: string
  excerpt: string
  /** The page allows being embedded in an <iframe> (no X-Frame-Options / CSP frame-ancestors). */
  frameable: boolean
  error: string
}

export interface ReactionBatchItem {
  id: string
  symbol: string
  time: string
}
