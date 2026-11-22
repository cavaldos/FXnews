import type { Interval, SymbolOut } from '../api/types'

/** TradingView tickers for our symbol catalog (used by the embed fallback). Keep in sync with
 *  backend/app/market/symbols.py (`tv`); the backend value wins when /api/symbols provides it. */
const TV_SYMBOLS: Record<string, string> = {
  XAUUSD: 'OANDA:XAUUSD',
  XAGUSD: 'OANDA:XAGUSD',
  USOIL: 'TVC:USOIL',
  US500: 'OANDA:SPX500USD',
  NAS100: 'OANDA:NAS100USD',
  US30: 'OANDA:US30USD',
  DE40: 'OANDA:DE30EUR',
  BTCUSD: 'BITSTAMP:BTCUSD',
  ETHUSD: 'BITSTAMP:ETHUSD',
}

export function tvSymbol(symbol: string, sym?: SymbolOut): string {
  if (sym?.tv) return sym.tv
  const mapped = TV_SYMBOLS[symbol]
  if (mapped) return mapped
  return `FX:${sym ? sym.base + sym.quote : symbol}`
}

export const TV_INTERVAL: Record<Interval, string> = {
  '1m': '1',
  '5m': '5',
  '15m': '15',
  '30m': '30',
  '1h': '60',
  '4h': '240',
  '1d': 'D',
}
