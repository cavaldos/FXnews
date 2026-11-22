import type { NewsOut } from '../api/types'
import { flag } from '../lib/format'
import { itemTime } from '../lib/news'
import { SymbolLink } from './SymbolLink'
import { BiasBadge, Chip, EmptyState, ErrorNote, ImpactDot, Spinner, TimeAgo } from './widgets'

const MAX_SYMBOL_CHIPS = 3

interface Props {
  items: NewsOut[]
  selectedId?: number
  freshIds: ReadonlySet<number>
  loading: boolean
  error?: Error
  onRetry: () => void
  onSelect: (item: NewsOut) => void
}

export function NewsFeed({ items, selectedId, freshIds, loading, error, onRetry, onSelect }: Props) {
  return (
    <div className="scroll">
      {error && items.length === 0 ? <ErrorNote error={error} onRetry={onRetry} /> : null}
      {loading && items.length === 0 ? (
        <EmptyState>
          <Spinner label="Loading headlines…" />
        </EmptyState>
      ) : null}
      {!loading && !error && items.length === 0 ? <EmptyState>No headlines match the filter.</EmptyState> : null}
      <ul className="news-list">
        {items.map((n) => {
          const syms = n.symbols ?? []
          return (
            <li key={n.id}>
              <button
                type="button"
                className={`news-row${n.id === selectedId ? ' selected' : ''}${freshIds.has(n.id) ? ' fresh' : ''}`}
                onClick={() => onSelect(n)}
              >
                <span className="news-row-top">
                  <ImpactDot impact={n.impact} />
                  <span className="news-source">{n.source}</span>
                  <span className="dot-sep">·</span>
                  <TimeAgo iso={n.event_time ?? n.fetched_at} />
                  <span className="spacer" />
                  {n.analysis_id ? (
                    <span className="ai-mark" title="AI analysis available">
                      ✦ AI
                    </span>
                  ) : null}
                  {n.rule_bias ? <BiasBadge bias={n.rule_bias} /> : null}
                </span>
                <span className="news-title" data-preview-url={n.url && /^https?:\/\//.test(n.url) ? n.url : undefined}>{n.title}</span>
                {n.currencies.length > 0 || syms.length > 0 ? (
                  <span className="chips">
                    {n.currencies.map((c) => (
                      <Chip key={c} tone="cur">
                        {flag(c)} {c}
                      </Chip>
                    ))}
                    {syms.slice(0, MAX_SYMBOL_CHIPS).map((s) => (
                      <Chip key={s} tone="sym">
                        <SymbolLink symbol={s} time={itemTime(n)}>
                          {s}
                        </SymbolLink>
                      </Chip>
                    ))}
                    {syms.length > MAX_SYMBOL_CHIPS ? (
                      <Chip tone="muted" title={syms.slice(MAX_SYMBOL_CHIPS).join(', ')}>
                        +{syms.length - MAX_SYMBOL_CHIPS}
                      </Chip>
                    ) : null}
                  </span>
                ) : null}
              </button>
            </li>
          )
        })}
      </ul>
    </div>
  )
}
