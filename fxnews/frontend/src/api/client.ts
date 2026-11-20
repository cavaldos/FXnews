import type {
  AdviceRequest,
  AnalysisOut,
  CandlesOut,
  HealthOut,
  Impact,
  IngestResult,
  LinkPreviewOut,
  Interval,
  ModelOut,
  NewsKind,
  NewsOut,
  QuoteOut,
  ReactionBatchItem,
  ReactionOut,
  SignalOut,
  SymbolOut,
} from './types'

const BASE = '/api'

export class ApiError extends Error {
  status: number
  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

type Params = Record<string, string | number | boolean | null | undefined>

interface RequestOptions {
  params?: Params
  body?: unknown
  signal?: AbortSignal
}

function buildUrl(path: string, params?: Params): string {
  const qs = new URLSearchParams()
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== '') qs.set(k, String(v))
    }
  }
  const query = qs.toString()
  return `${BASE}${path}${query ? `?${query}` : ''}`
}

/** FastAPI errors: `{detail: "msg"}` or `{detail: [{msg, loc}]}`; proxies may return plain text. */
function errorMessage(status: number, statusText: string, text: string): string {
  try {
    const json = JSON.parse(text) as { detail?: unknown; message?: unknown }
    if (typeof json.detail === 'string') return json.detail
    if (Array.isArray(json.detail)) {
      return json.detail
        .map((d) => (d && typeof d === 'object' && 'msg' in d ? String((d as { msg: unknown }).msg) : String(d)))
        .join('; ')
    }
    if (typeof json.message === 'string') return json.message
  } catch {
    /* not JSON */
  }
  const trimmed = text.trim()
  if (trimmed && trimmed.length < 200 && !trimmed.startsWith('<')) return trimmed
  if (status === 502 || status === 503 || status === 504) return 'Backend not responding (is uvicorn running on :8000?)'
  return `${status} ${statusText}`.trim()
}

async function request<T>(method: 'GET' | 'POST', path: string, opts: RequestOptions = {}): Promise<T> {
  const hasBody = opts.body !== undefined
  let res: Response
  try {
    res = await fetch(buildUrl(path, opts.params), {
      method,
      signal: opts.signal,
      headers: {
        Accept: 'application/json',
        ...(hasBody ? { 'Content-Type': 'application/json' } : {}),
      },
      body: hasBody ? JSON.stringify(opts.body) : undefined,
    })
  } catch (err) {
    // Aborted by us (user cancel / timeout / unmount): surface the original reason.
    if (opts.signal?.aborted) throw opts.signal.reason ?? err
    throw new ApiError('Could not reach the backend', 0)
  }
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new ApiError(errorMessage(res.status, res.statusText, text), res.status)
  }
  return (await res.json()) as T
}

export function isAbortError(err: unknown): boolean {
  return err instanceof DOMException && err.name === 'AbortError'
}

export interface NewsQuery {
  kind?: NewsKind
  currency?: string
  impact?: Impact
  symbol?: string
  limit?: number
  /** event_time >= from (inclusive). ISO datetime or date; naive values are UTC. */
  from?: string
  /** event_time < to (exclusive). ISO datetime or date; naive values are UTC. */
  to?: string
  before?: string
  /** Keyset tie-break, sent together with `before`. */
  before_id?: number
}

export interface CalendarQuery {
  days?: number
  /** Include events that already happened in the last N days (backend default 1). */
  past_days?: number
  currency?: string
  impact?: Impact
}

export const api = {
  health: (signal?: AbortSignal) => request<HealthOut>('GET', '/health', { signal }),
  symbols: (signal?: AbortSignal) => request<SymbolOut[]>('GET', '/symbols', { signal }),
  models: (signal?: AbortSignal) => request<ModelOut[]>('GET', '/models', { signal }),
  quotes: (symbols?: string[], signal?: AbortSignal) =>
    request<QuoteOut[]>('GET', '/quotes', { params: { symbols: symbols?.join(',') }, signal }),
  candles: (symbol: string, interval: Interval, range?: string, signal?: AbortSignal) =>
    request<CandlesOut>('GET', '/candles', { params: { symbol, interval, range }, signal }),
  news: (q: NewsQuery = {}, signal?: AbortSignal) =>
    request<NewsOut[]>('GET', '/news', { params: { ...q }, signal }),
  calendar: (q: CalendarQuery = {}, signal?: AbortSignal) =>
    request<NewsOut[]>('GET', '/calendar', { params: { ...q }, signal }),
  newsItem: (id: number, signal?: AbortSignal) => request<NewsOut>('GET', `/news/${id}`, { signal }),
  signals: (symbol?: string, limit = 50, signal?: AbortSignal) =>
    request<SignalOut[]>('GET', '/signals', { params: { symbol, limit }, signal }),
  /** Latest analyses for a symbol (ad-hoc ones + any analysis with a trade idea for it), newest first. */
  analyses: (symbol?: string, limit = 20, signal?: AbortSignal) =>
    request<AnalysisOut[]>('GET', '/analyses', { params: { symbol, limit }, signal }),
  analysis: (id: number, signal?: AbortSignal) => request<AnalysisOut>('GET', `/analyses/${id}`, { signal }),
  analyzeNews: (id: number, model?: string | null, signal?: AbortSignal) =>
    request<AnalysisOut>('POST', `/news/${id}/analyze`, { body: { model: model || null }, signal }),
  advice: (body: AdviceRequest, signal?: AbortSignal) =>
    request<AnalysisOut>('POST', '/advice', { body, signal }),
  ingestRun: (signal?: AbortSignal) => request<IngestResult>('POST', '/ingest/run', { signal }),
  reaction: (symbol: string, time: string, signal?: AbortSignal) =>
    request<ReactionOut>('GET', '/reaction', { params: { symbol, time }, signal }),
  reactionBatch: (items: ReactionBatchItem[], signal?: AbortSignal) =>
    request<{ results: (ReactionOut & { id: string })[] }>('POST', '/reaction/batch', { body: items, signal }),
  linkPreview: (url: string, signal?: AbortSignal) => request<LinkPreviewOut>('GET', '/link-preview', { params: { url }, signal }),
  notifyTest: (signal?: AbortSignal) => request<unknown>('POST', '/notify/test', { signal }),
}

export const STREAM_URL = `${BASE}/stream`
