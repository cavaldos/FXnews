import { useMemo, type ReactNode } from 'react'
import type { Impact, NewsOut, SymbolOut } from '../api/types'
import { useStoredBool, useStoredString } from '../hooks/useLocalStorage'
import { flag } from '../lib/format'
import { currenciesOf, filterNews, symbolCurrencies } from '../lib/news'
import { CalendarList } from './CalendarList'
import { NewsFeed } from './NewsFeed'
import { UiSelect } from './UI/UiSelect'

export type NewsTab = 'headlines' | 'calendar'

interface Source {
  items: NewsOut[]
  loading: boolean
  error?: Error
  reload: () => void
}

interface Props {
  tab: NewsTab
  onTab: (tab: NewsTab) => void
  sym?: SymbolOut
  headlines: Source
  calendar: Source
  freshIds: ReadonlySet<number>
  selectedId?: number
  onSelect: (item: NewsOut) => void
  /** Detail pane rendered under the list when a news item is selected. */
  children?: ReactNode
}

const CURRENCY_ORDER = ['USD', 'EUR', 'GBP', 'JPY', 'AUD', 'NZD', 'CAD', 'CHF', 'CNY']

export function NewsColumn({ tab, onTab, sym, headlines, calendar, freshIds, selectedId, onSelect, children }: Props) {
  const [onlyRelated, setOnlyRelated] = useStoredBool('fxnews.onlyRelated', true)
  const [currency, setCurrency] = useStoredString('fxnews.currency', '')
  const [impact, setImpact] = useStoredString('fxnews.impact', '')

  const source = tab === 'headlines' ? headlines : calendar

  const currencyOptions = useMemo(() => {
    const set = new Set<string>()
    for (const n of [...headlines.items, ...calendar.items]) for (const c of currenciesOf(n)) set.add(c)
    return [...set].sort((a, b) => {
      const ia = CURRENCY_ORDER.indexOf(a)
      const ib = CURRENCY_ORDER.indexOf(b)
      return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib) || a.localeCompare(b)
    })
  }, [headlines.items, calendar.items])

  const filtered = useMemo(
    () => filterNews(source.items, { sym, onlyRelated, currency, impact: impact as Impact | '' }),
    [source.items, sym, onlyRelated, currency, impact],
  )

  const pair = symbolCurrencies(sym)

  return (
    <section className="panel news-col" aria-label="News">
      <div className="tabs" role="tablist">
        <button type="button" role="tab" aria-selected={tab === 'headlines'} className={tab === 'headlines' ? 'active' : ''} onClick={() => onTab('headlines')}>
          Headlines
        </button>
        <button type="button" role="tab" aria-selected={tab === 'calendar'} className={tab === 'calendar' ? 'active' : ''} onClick={() => onTab('calendar')}>
          Economic Calendar
        </button>
      </div>

      <div className="filters">
        <label className="toggle" title={sym ? `Only headlines for ${pair.join(' & ')}` : 'Only headlines for the selected symbol'}>
          <input type="checkbox" checked={onlyRelated} onChange={(e) => setOnlyRelated(e.target.checked)} />
          <span>{onlyRelated && pair.length ? `By pair ${pair.map((c) => `${flag(c)}${c}`).join(' ')}` : 'By pair'}</span>
        </label>
        {onlyRelated ? null : (
          <UiSelect
            size="xs"
            value={currency}
            onChange={setCurrency}
            ariaLabel="Filter by currency"
            options={[{ value: '', label: 'All currencies' }, ...currencyOptions.map((c) => ({ value: c, label: `${flag(c)} ${c}` }))]}
          />
        )}
        <UiSelect
          size="xs"
          value={impact}
          onChange={setImpact}
          ariaLabel="Filter by impact level"
          options={[
            { value: '', label: 'All impact levels' },
            { value: 'High', label: 'High' },
            { value: 'Medium', label: 'Medium' },
            { value: 'Low', label: 'Low' },
          ]}
        />
        <span className="count">{filtered.length}</span>
      </div>

      <div className="list-area">
        {tab === 'headlines' ? (
          <NewsFeed
            items={filtered}
            selectedId={selectedId}
            freshIds={freshIds}
            loading={source.loading}
            error={source.error}
            onRetry={source.reload}
            onSelect={onSelect}
          />
        ) : (
          <CalendarList
            items={filtered}
            selectedId={selectedId}
            loading={source.loading}
            error={source.error}
            onRetry={source.reload}
            onSelect={onSelect}
          />
        )}
      </div>

      {children}
    </section>
  )
}
