import type { ReactNode } from 'react'
import type { Interval, SymbolOut } from '../api/types'
import { useStoredString } from '../hooks/useLocalStorage'
import { SymbolInfo, TechnicalGauge } from './MarketWidgets'
import { SymbolLink } from './SymbolLink'
import { TabbedColumn } from './TabbedColumn'

interface Props {
  symbol: string
  sym?: SymbolOut
  interval: Interval
  /** The AI advice panel (kept mounted while the technical tab is open). */
  children: ReactNode
}

/** Right column: AI advice + technical analysis (gauge + symbol info of the selected symbol) as tabs. */
export function SideColumn({ symbol, sym, interval, children }: Props) {
  const [tab, setTab] = useStoredString('fxnews.sideTab', 'ai')
  return (
    <TabbedColumn
      className="side-col"
      ariaLabel="Right panel view"
      active={tab}
      onChange={setTab}
      tabs={[
        { key: 'ai', label: 'AI Advisor', content: children },
        {
          key: 'ta',
          label: 'Technicals',
          content: (
            <section className="panel" aria-label="Technical analysis">
              <div className="feed-head">
                <span>
                  Technicals · <span className="sym-tag">
                    <SymbolLink symbol={symbol} />
                  </span>
                </span>
                <span className="feed-hint">TradingView · {interval}</span>
              </div>
              <div className="scroll ta-stack">
                <div className="ta-info">
                  <SymbolInfo key={symbol} symbol={symbol} sym={sym} />
                </div>
                <div className="ta-gauge">
                  <TechnicalGauge key={`${symbol}:${interval}`} symbol={symbol} sym={sym} interval={interval} />
                </div>
              </div>
            </section>
          ),
        },
      ]}
    />
  )
}
