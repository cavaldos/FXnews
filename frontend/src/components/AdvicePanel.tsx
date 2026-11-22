import { useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import type { AnalysisOut, HealthOut, ModelOut } from '../api/types'
import { useNowFast } from '../hooks/useClock'
import { AnalysisCard } from './AnalysisCard'
import { SymbolLink } from './SymbolLink'
import { UiButton } from './UI/UiButton'
import { UiSelect } from './UI/UiSelect'
import type { SelectOption } from './UI/types'
import { EmptyState, ErrorNote, Spinner } from './widgets'

const REQUEST_TIMEOUT_MS = 120_000

interface Props {
  symbol: string
  models: ModelOut[]
  /** Selected model id; '' = backend default. */
  model: string
  onModel: (id: string) => void
  health?: HealthOut
  /** Analyses relevant to `symbol`, newest first. */
  feed: AnalysisOut[]
  feedLoading: boolean
  feedError?: Error
  onReloadFeed: () => void
  freshIds: ReadonlySet<number>
  newsTitleOf: (newsId: number) => string | undefined
  /** Performs POST /advice and stores the result; rejects on failure. */
  onAsk: (question: string | undefined, signal: AbortSignal) => Promise<void>
}

function errorText(err: unknown): string {
  if (err instanceof DOMException && err.name === 'TimeoutError') {
    return `LLM request timed out (${REQUEST_TIMEOUT_MS / 1000}s). Retry or pick a faster model.`
  }
  return err instanceof Error ? err.message : String(err)
}

export function AdvicePanel({
  symbol,
  models,
  model,
  onModel,
  health,
  feed,
  feedLoading,
  feedError,
  onReloadFeed,
  freshIds,
  newsTitleOf,
  onAsk,
}: Props) {
  const [question, setQuestion] = useState('')
  const [startedAt, setStartedAt] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const now = useNowFast()

  const pending = startedAt !== null
  const elapsed = startedAt === null ? 0 : Math.max(0, Math.floor((now - startedAt) / 1000))

  const defaultId = models.find((m) => m.is_default)?.id ?? health?.default_model ?? ''
  const selected = model || defaultId
  const modelOptions: SelectOption[] = models.map((m) => ({
    value: m.id,
    label: `${m.name}${m.is_default ? ' ★' : ''}`,
    title: m.prompt_price != null ? `$${m.prompt_price}/1M in · $${m.completion_price ?? '?'}/1M out` : m.id,
  }))
  if (selected && !models.some((m) => m.id === selected)) modelOptions.unshift({ value: selected, label: selected })

  async function submit(e?: FormEvent) {
    e?.preventDefault()
    if (pending) return
    const ctrl = new AbortController()
    abortRef.current = ctrl
    const timer = setTimeout(() => ctrl.abort(new DOMException('timeout', 'TimeoutError')), REQUEST_TIMEOUT_MS)
    setStartedAt(Date.now())
    setError(null)
    try {
      await onAsk(question.trim() || undefined, ctrl.signal)
      setQuestion('')
    } catch (err) {
      const cancelled = err instanceof DOMException && err.name === 'AbortError'
      if (!cancelled) setError(errorText(err))
    } finally {
      clearTimeout(timer)
      abortRef.current = null
      setStartedAt(null)
    }
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) void submit()
  }

  return (
    <section className="panel advice-col" aria-label="AI Advisor">
      <form className="advice-form" onSubmit={submit}>
        <div className="panel-title">
          <span>✦ Ask AI</span>
          <strong className="sym-tag">
            <SymbolLink symbol={symbol} />
          </strong>
        </div>
        <textarea
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={onKeyDown}
          rows={3}
          placeholder={`Question (optional), e.g. "Should I buy or sell ${symbol} before the CPI release?"`}
          disabled={pending}
        />
        <div className="advice-row">
          {models.length > 0 ? (
            <UiSelect
              size="sm"
              value={selected}
              onChange={onModel}
              disabled={pending}
              ariaLabel="Model LLM"
              options={modelOptions}
            />
          ) : (
            <span className="hint">model: {health?.default_model ?? 'default'}</span>
          )}
          {pending ? (
            <UiButton tone="ghost" size="sm" onClick={() => abortRef.current?.abort()}>
              Cancel
            </UiButton>
          ) : null}
          <UiButton type="submit" tone="primary" size="sm" disabled={pending}>
            {pending ? <Spinner label={`Asking… ${elapsed}s`} /> : `Ask AI about ${symbol}`}
          </UiButton>
        </div>
        {pending ? <div className="hint">The LLM may take 10–60s, please wait…</div> : null}
        {health && !health.llm_enabled && !pending ? (
          <div className="hint warn">LLM is not enabled (OPENROUTER_API_KEY missing) — results will be rule-based.</div>
        ) : null}
        {error ? (
          <div className="error-note" role="alert">
            ⚠ {error}
          </div>
        ) : null}
      </form>

      <div className="feed-head">
        <span>Latest analysis · {symbol}</span>
        <span className="count">{feed.length}</span>
      </div>
      <div className="scroll advice-feed">
        {feedError && feed.length === 0 ? <ErrorNote error={feedError} onRetry={onReloadFeed} /> : null}
        {feedLoading && feed.length === 0 ? (
          <EmptyState>
            <Spinner label="Loading analysis…" />
          </EmptyState>
        ) : null}
        {!feedLoading && !feedError && feed.length === 0 ? (
          <EmptyState>
            No analysis yet for {symbol}. Ask a question above or pick a headline and click “AI Analysis”.
          </EmptyState>
        ) : null}
        {feed.map((a) => (
          <AnalysisCard
            key={a.id}
            analysis={a}
            symbol={symbol}
            newsTitle={a.news_id != null ? newsTitleOf(a.news_id) : undefined}
            fresh={freshIds.has(a.id)}
          />
        ))}
      </div>
    </section>
  )
}
