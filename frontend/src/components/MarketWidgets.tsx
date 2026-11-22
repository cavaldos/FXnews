import { useMemo } from 'react'
import type { Interval, SymbolOut } from '../api/types'
import { useChartTheme } from '../lib/theme'
import { tvSymbol } from '../lib/tradingview'
import { FX_MAJORS, TV_SCRIPT, TV_TA_INTERVAL, overviewTabs, withAlpha } from '../lib/tvWidgets'
import { TvEmbed } from './TvEmbed'

/** Watchlist with sparklines, grouped by asset class (TradingView Market Overview). */
export function MarketOverview({ symbols }: { symbols: SymbolOut[] }) {
  const theme = useChartTheme()
  const config = useMemo(
    () => ({
      dateRange: '1D',
      showChart: true,
      showSymbolLogo: false,
      showFloatingTooltip: true,
      plotLineColorGrowing: theme.up,
      plotLineColorFalling: theme.down,
      gridLineColor: 'rgba(0, 0, 0, 0)',
      scaleFontColor: theme.text,
      belowLineFillColorGrowing: withAlpha(theme.up, 0.12),
      belowLineFillColorFalling: withAlpha(theme.down, 0.12),
      belowLineFillColorGrowingBottom: withAlpha(theme.up, 0),
      belowLineFillColorFallingBottom: withAlpha(theme.down, 0),
      symbolActiveColor: withAlpha(theme.accent, 0.14),
      tabs: overviewTabs(symbols),
    }),
    [symbols, theme.up, theme.down, theme.text, theme.accent],
  )
  return <TvEmbed script={TV_SCRIPT.marketOverview} config={config} />
}

const CURRENCIES = { currencies: FX_MAJORS }

/** Currency cross-rate matrix (TradingView Forex Cross Rates). */
export function ForexCrossRates() {
  return <TvEmbed script={TV_SCRIPT.forexCrossRates} config={CURRENCIES} blendSurface />
}

/** Currency strength heatmap (TradingView Forex Heat Map). */
export function ForexHeatmap() {
  return <TvEmbed script={TV_SCRIPT.forexHeatMap} config={CURRENCIES} />
}

/** Price / change / range summary of the selected symbol (TradingView Symbol Info). */
export function SymbolInfo({ symbol, sym }: { symbol: string; sym?: SymbolOut }) {
  const config = useMemo(() => ({ symbol: tvSymbol(symbol, sym) }), [symbol, sym])
  return <TvEmbed script={TV_SCRIPT.symbolInfo} config={config} />
}

/** Buy / sell gauge from oscillators + moving averages (TradingView Technical Analysis). */
export function TechnicalGauge({ symbol, sym, interval }: { symbol: string; sym?: SymbolOut; interval: Interval }) {
  const config = useMemo(
    () => ({
      symbol: tvSymbol(symbol, sym),
      interval: TV_TA_INTERVAL[interval],
      showIntervalTabs: true,
      displayMode: 'single',
    }),
    [symbol, sym, interval],
  )
  return <TvEmbed script={TV_SCRIPT.technicalAnalysis} config={config} />
}
