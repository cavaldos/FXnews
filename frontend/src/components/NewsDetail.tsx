import type { AnalysisOut, NewsOut } from '../api/types'
import { useReactionPanel } from '../context/reaction'
import { fmtDateTime, flag, toMs } from '../lib/format'
import { itemTime } from '../lib/news'
import { AnalysisCard } from './AnalysisCard'
import { SymbolLink } from './SymbolLink'
import { UiButton } from './UI/UiButton'
import { BiasBadge, Chip, ImpactBadge, Spinner, TimeAgo } from './widgets'

interface Props {
  item: NewsOut
  symbol?: string
  analysis?: AnalysisOut
  /** An analysis exists (analysis_id) but is still being fetched. */
  analysisLoading: boolean
  analyzing: boolean
  analyzeError?: string
  onAnalyze: () => void
  onClose: () => void
}

export function NewsDetail({ item, symbol, analysis, analysisLoading, analyzing, analyzeError, onAnalyze, onClose }: Props) {
  const t = toMs(item.event_time) ?? itemTime(item)
  const reaction = useReactionPanel()
  const hasValues = item.kind === 'calendar' && (item.actual || item.forecast || item.previous)

  return (
    <aside className="news-detail" aria-label="Headline details">
      <div className="detail-top">
        <Chip tone="muted">{item.kind === 'calendar' ? 'Calendar' : 'Headline'}</Chip>
        <span className="news-source">{item.source}</span>
        <ImpactBadge impact={item.impact} />
        {item.rule_bias ? <BiasBadge bias={item.rule_bias} label={`rule ${item.rule_bias}`} /> : null}
        <span className="spacer" />
        <UiButton tone="ghost" size="xs" onClick={onClose} aria-label="Close details" title="Close">
          ×
        </UiButton>
      </div>

      <h3 className="detail-title">
        {item.url ? (
          <a href={item.url} target="_blank" rel="noopener noreferrer">
            {item.title} <span aria-hidden>↗</span>
          </a>
        ) : (
          item.title
        )}
      </h3>
      <div className="detail-time">
        {fmtDateTime(t)} · <TimeAgo iso={item.event_time ?? item.fetched_at} />
      </div>

      {hasValues ? (
        <div className="detail-values">
          <div>
            <span>Actual</span>
            <b>{item.actual || '—'}</b>
          </div>
          <div>
            <span>Forecast</span>
            <b>{item.forecast || '—'}</b>
          </div>
          <div>
            <span>Previous</span>
            <b>{item.previous || '—'}</b>
          </div>
        </div>
      ) : null}

      {item.summary ? <p className="detail-summary">{item.summary}</p> : null}

      <div className="chips">
        {item.currencies.map((c) => (
          <Chip key={c} tone="cur">
            {flag(c)} {c}
          </Chip>
        ))}
        {item.symbols.map((s) => (
          <Chip key={s} tone="sym">
            <SymbolLink symbol={s} time={t}>
              {s}
            </SymbolLink>
          </Chip>
        ))}
      </div>

      <div className="detail-actions">
        <UiButton tone="primary" size="sm" onClick={onAnalyze} disabled={analyzing}>
          {analyzing ? <Spinner label="Analyzing…" /> : analysis ? '✦ Re-analyze' : '✦ AI Analysis'}
        </UiButton>
        {analyzing ? <span className="hint">The LLM may take 10–60s</span> : null}
        <UiButton
          tone="ghost"
          size="sm"
          disabled={item.symbols.length === 0}
          title={item.symbols.length ? 'Price reaction of each related symbol after the release' : 'No related symbols'}
          onClick={() => reaction.open({ time: t, symbols: item.symbols, title: `Check reaction · ${item.title}`.slice(0, 90) })}
        >
          Check reaction
        </UiButton>
      </div>
      {analyzeError ? (
        <div className="error-note" role="alert">
          ⚠ {analyzeError}
        </div>
      ) : null}

      {analysisLoading && !analysis ? (
        <div className="empty">
          <Spinner label="Loading analysis…" />
        </div>
      ) : null}
      {analysis ? <AnalysisCard analysis={analysis} symbol={symbol} /> : null}
    </aside>
  )
}
