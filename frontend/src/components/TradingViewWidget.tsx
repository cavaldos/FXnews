import { useEffect, useRef } from 'react'
import { useChartTheme } from '../lib/theme'

const SCRIPT_SRC = 'https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js'

interface Props {
  /** TradingView ticker, e.g. "FX:EURUSD", "OANDA:XAUUSD". */
  symbol: string
  /** TradingView interval: "1", "5", "15", "60", "240", "D". */
  interval: string
}

/** TradingView "Advanced Chart" free embed — fallback when our candle API is unavailable. */
export function TradingViewWidget({ symbol, interval }: Props) {
  const hostRef = useRef<HTMLDivElement>(null)
  const { light, surface, grid } = useChartTheme()

  useEffect(() => {
    const host = hostRef.current
    if (!host) return
    const widget = document.createElement('div')
    widget.className = 'tradingview-widget-container__widget'
    widget.style.height = '100%'
    widget.style.width = '100%'
    const script = document.createElement('script')
    script.src = SCRIPT_SRC
    script.type = 'text/javascript'
    script.async = true
    script.text = JSON.stringify({
      autosize: true,
      symbol,
      interval,
      timezone: 'Etc/UTC',
      theme: light ? 'light' : 'dark',
      style: '1',
      locale: 'en',
      backgroundColor: surface,
      gridColor: grid,
      allow_symbol_change: false,
      withdateranges: true,
      hide_side_toolbar: true,
      hide_top_toolbar: true, // symbol/interval are driven by our own toolbar
      support_host: 'https://www.tradingview.com',
    })
    // Inject on the next tick: the embed script reads its own parent element when it runs, so a
    // script whose mount was already discarded (StrictMode, fast symbol switching) would throw.
    const timer = setTimeout(() => host.append(widget, script), 0)
    return () => {
      clearTimeout(timer)
      host.replaceChildren()
    }
  }, [symbol, interval, light, surface, grid])

  return <div className="tradingview-widget-container tv-widget" ref={hostRef} />
}
