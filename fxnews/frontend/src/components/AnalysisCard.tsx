import { useState } from 'react'
import type { AnalysisOut } from '../api/types'
import { KIND_LABEL, flag, shortModel } from '../lib/format'
import { TradeIdeaCard } from './TradeIdeaCard'
import { UiButton } from './UI/UiButton'
import { Chip, TimeAgo } from './widgets'

const DIRECTION_GLYPH = { stronger: '▲', weaker: '▼', neutral: '◆' } as const
const DEFAULT_VISIBLE_TRADES = 3

interface Props {
  analysis: AnalysisOut
  /** Currently selected symbol: its trade ideas are listed first and highlighted. */
  symbol?: string
  /** Title of the related news item, when known. */
  newsTitle?: string
  fresh?: boolean
}

export function AnalysisCard({ analysis: a, symbol, newsTitle, fresh }: Props) {
  const [showAll, setShowAll] = useState(false)

  const trades = [...a.trades].sort((x, y) => Number(y.symbol === symbol) - Number(x.symbol === symbol))
  const visible = showAll ? trades : trades.slice(0, DEFAULT_VISIBLE_TRADES)

  return (
    <article className={`analysis-card kind-${a.kind}${fresh ? ' fresh' : ''}`}>
      <header className="analysis-head">
        <Chip tone={a.kind === 'ad_hoc' ? 'sym' : 'cur'}>{KIND_LABEL[a.kind] ?? a.kind}</Chip>
        <span className="analysis-model" title={a.model}>
          {a.model === 'rules' ? 'rule-based' : shortModel(a.model)}
        </span>
        <span className="analysis-meta">
          {a.latency_ms ? <span>{(a.latency_ms / 1000).toFixed(1)}s · </span> : null}
          <TimeAgo iso={a.created_at} />
        </span>
      </header>

      {a.error ? (
        <div className="warn-note" role="alert" title={a.error}>
          ⚠ LLM error, using rule-based results
          <span className="warn-detail">{a.error}</span>
        </div>
      ) : null}

      {newsTitle ? <div className="analysis-news">📰 {newsTitle}</div> : null}
      {a.question ? <blockquote className="analysis-question">“{a.question}”</blockquote> : null}

      <p className="analysis-summary">{a.summary}</p>

      {a.currency_impact.length > 0 ? (
        <div className="cur-impact">
          {a.currency_impact.map((c) => (
            <span key={c.currency} className={`cur-chip dir-${c.direction}`} title={`Strength ${Math.round(c.strength * 100)}%`}>
              {flag(c.currency)} {c.currency} {DIRECTION_GLYPH[c.direction]} {Math.round(c.strength * 100)}%
            </span>
          ))}
        </div>
      ) : null}

      {visible.length > 0 ? (
        <div className="trade-list">
          {visible.map((t, i) => (
            <TradeIdeaCard key={`${t.symbol}-${i}`} trade={t} focus={t.symbol === symbol} />
          ))}
          {trades.length > DEFAULT_VISIBLE_TRADES ? (
            <UiButton tone="ghost" size="xs" className="self-start" onClick={() => setShowAll((v) => !v)}>
              {showAll ? 'Collapse' : `+ ${trades.length - DEFAULT_VISIBLE_TRADES} more pairs`}
            </UiButton>
          ) : null}
        </div>
      ) : null}

      {a.scenarios.length > 0 ? (
        <div className="scenarios">
          <div className="section-label">Scenarios</div>
          <ul>
            {a.scenarios.map((s, i) => (
              <li key={i}>
                <span className="sc-cond">{s.condition}</span>
                <span className="sc-arrow">→</span>
                <span className="sc-out">{s.outcome}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {a.risk_notes ? <p className="risk-notes">⚠ {a.risk_notes}</p> : null}
    </article>
  )
}
