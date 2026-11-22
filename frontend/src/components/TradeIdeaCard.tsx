import type { TradeIdea } from '../api/types'
import { SymbolLink } from './SymbolLink'
import { BiasBadge, Chip } from './widgets'

export function TradeIdeaCard({ trade, focus }: { trade: TradeIdea; focus?: boolean }) {
  const conf = Math.max(0, Math.min(100, Math.round(trade.confidence)))
  return (
    <div className={`trade-card bias-${trade.bias}${focus ? ' focus' : ''}`}>
      <div className="trade-head">
        <strong className="trade-symbol">
          <SymbolLink symbol={trade.symbol} />
        </strong>
        <BiasBadge bias={trade.bias} />
        {trade.horizon ? <Chip tone="muted">{trade.horizon}</Chip> : null}
        <span className="trade-conf" title="Confidence">
          {conf}%
        </span>
      </div>
      <div className="conf-bar" aria-hidden>
        <span style={{ width: `${conf}%` }} />
      </div>
      <dl className="trade-fields">
        {trade.entry_idea ? (
          <>
            <dt>Entry</dt>
            <dd>{trade.entry_idea}</dd>
          </>
        ) : null}
        {trade.invalidation ? (
          <>
            <dt>Invalid</dt>
            <dd>{trade.invalidation}</dd>
          </>
        ) : null}
        {trade.targets ? (
          <>
            <dt>Target</dt>
            <dd>{trade.targets}</dd>
          </>
        ) : null}
      </dl>
    </div>
  )
}
