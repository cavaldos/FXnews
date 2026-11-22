import type { ReactNode } from 'react'
import type { SymbolOut } from '../api/types'
import { useStoredString } from '../hooks/useLocalStorage'
import { ForexCrossRates, ForexHeatmap, MarketOverview } from './MarketWidgets'
import { TabbedColumn } from './TabbedColumn'

interface Props {
  symbols: SymbolOut[]
  /** The price chart panel (kept mounted while other tabs are open). */
  children: ReactNode
}

function WidgetPanel({ title, hint, children }: { title: string; hint: string; children: ReactNode }) {
  return (
    <section className="panel" aria-label={title}>
      <div className="feed-head">
        <span>{title}</span>
        <span className="feed-hint">{hint}</span>
      </div>
      <div className="widget-body">{children}</div>
    </section>
  )
}

/** Centre column: price chart + TradingView market widgets (watchlist, cross rates, heatmap) as tabs. */
export function CenterColumn({ symbols, children }: Props) {
  const [tab, setTab] = useStoredString('fxnews.centerTab', 'chart')
  return (
    <TabbedColumn
      className="center-col"
      ariaLabel="Center view"
      active={tab}
      onChange={setTab}
      tabs={[
        { key: 'chart', label: 'Chart', content: children },
        {
          key: 'watch',
          label: 'Watchlist',
          content: (
            <WidgetPanel title="Market overview" hint="price + sparkline · TradingView">
              <MarketOverview symbols={symbols} />
            </WidgetPanel>
          ),
        },
        {
          key: 'cross',
          label: 'Cross rates',
          content: (
            <WidgetPanel title="Cross rates" hint="major currencies · TradingView">
              <ForexCrossRates />
            </WidgetPanel>
          ),
        },
        {
          key: 'heat',
          label: 'Heatmap',
          content: (
            <WidgetPanel title="Currency strength" hint="forex heatmap · TradingView">
              <ForexHeatmap />
            </WidgetPanel>
          ),
        },
      ]}
    />
  )
}
