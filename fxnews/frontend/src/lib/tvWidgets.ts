import type { Interval, SymbolOut } from '../api/types'
import { tvSymbol } from './tradingview'

/** Free TradingView embed scripts (https://www.tradingview.com/widget-docs/widgets/). */
export const TV_SCRIPT = {
  marketOverview: 'embed-widget-market-overview.js',
  technicalAnalysis: 'embed-widget-technical-analysis.js',
  forexCrossRates: 'embed-widget-forex-cross-rates.js',
  forexHeatMap: 'embed-widget-forex-heat-map.js',
  symbolInfo: 'embed-widget-symbol-info.js',
} as const

export type TvScript = (typeof TV_SCRIPT)[keyof typeof TV_SCRIPT]

/** Interval strings expected by the Technical Analysis widget. */
export const TV_TA_INTERVAL: Record<Interval, string> = {
  '1m': '1m',
  '5m': '5m',
  '15m': '15m',
  '30m': '30m',
  '1h': '1h',
  '4h': '4h',
  '1d': '1D',
}

export const FX_MAJORS = ['EUR', 'USD', 'JPY', 'GBP', 'CHF', 'AUD', 'CAD', 'NZD']

const KIND_TITLE: Record<SymbolOut['kind'], string> = {
  fx: 'Forex',
  metal: 'Metals',
  energy: 'Energy',
  index: 'Indices',
  crypto: 'Crypto',
}

const KIND_ORDER: SymbolOut['kind'][] = ['fx', 'metal', 'index', 'energy', 'crypto']

/** Market-overview tabs (one per asset class) for our symbol catalog. */
export function overviewTabs(symbols: SymbolOut[]): { title: string; symbols: { s: string; d: string }[] }[] {
  return KIND_ORDER.map((kind) => ({
    title: KIND_TITLE[kind],
    symbols: symbols.filter((s) => s.kind === kind).map((s) => ({ s: tvSymbol(s.symbol, s), d: s.symbol })),
  })).filter((t) => t.symbols.length > 0)
}

/** "#rrggbb" -> "rgba(r, g, b, a)"; any other colour string is returned unchanged. */
export function withAlpha(color: string, alpha: number): string {
  const m = /^#([0-9a-f]{6})$/i.exec(color.trim())
  if (!m) return color
  const n = parseInt(m[1], 16)
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`
}
