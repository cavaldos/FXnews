// Small formatting + data helpers shared by the Backtest components.
import { horizonTooFine, type BtEvent, type BtImpact, type BtOutcome, type BtVerifyReport, type Direction, type Horizon } from '../../api/backtest'
import { toMs } from '../../lib/format'

export const IMPACTS: BtImpact[] = ['high', 'medium', 'low']
export const PAGE_SIZES = [25, 50, 100, 200]
export const ARROW: Record<Direction, string> = { up: '▲', down: '▼', flat: '◆' }

const pad = (n: number) => String(n).padStart(2, '0')

/** "dd/MM HH:mm" in UTC. */
export function fmtUtc(iso: string): { date: string; hm: string } {
  const ms = toMs(iso)
  if (ms === null) return { date: iso, hm: '' }
  const d = new Date(ms)
  return { date: `${pad(d.getUTCDate())}/${pad(d.getUTCMonth() + 1)}`, hm: `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}` }
}

export function pct(v: number | null | undefined, digits = 0): string {
  return v === null || v === undefined ? '—' : `${(v * 100).toFixed(digits)}%`
}

export function ret(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return `${v > 0 ? '+' : ''}${v.toFixed(Math.abs(v) < 0.1 ? 3 : 2)}%`
}

export function retTone(v: number | null | undefined): string {
  if (v === null || v === undefined || v === 0) return ''
  return v > 0 ? 'up' : 'down'
}

/** "openai:gpt-4o-mini" -> "gpt-4o-mini"; "openrouter:anthropic/claude-x" -> "claude-x". */
export function modelLabel(id: string): string {
  const [provider, ...rest] = id.split(':')
  const name = rest.join(':') || provider
  return name.split('/').pop() ?? name
}

export function errMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

const SOURCE_NAMES: Record<string, string> = {
  forexfactory: 'ForexFactory',
  fxstreet: 'FXStreet',
  cnbc: 'CNBC',
  tradingview: 'TradingView',
  investing: 'Investing.com',
  forexlive: 'ForexLive',
  reuters: 'Reuters',
  dailyfx: 'DailyFX',
  marketwatch: 'MarketWatch',
}

export function sourceLabel(source: string): string {
  const key = source.trim().toLowerCase()
  if (SOURCE_NAMES[key]) return SOURCE_NAMES[key]
  return source.trim().replace(/[_-]+/g, ' ').replace(/\b[a-z]/g, (c) => c.toUpperCase())
}

export function eventMs(e: BtEvent): number {
  return toMs(e.time) ?? 0
}

export function isReleased(e: BtEvent, now: number): boolean {
  return eventMs(e) <= now
}

/** The symbol shown in compact cells: the first priced one, else the first related one. */
export function primarySymbol(e: BtEvent): string | undefined {
  return e.outcomes[0]?.symbol ?? e.symbols[0]
}

/** Direction label of an outcome at `horizon`, or why there is none. */
export function outcomeState(o: BtOutcome, horizon: Horizon): { dir: Direction | null; text: string; note?: string } {
  const dir = o.directions[horizon]
  if (dir) return { dir, text: dir }
  if (horizonTooFine(o.interval, horizon)) return { dir: null, text: 'n/a', note: `${horizon} is finer than the ${o.interval} candles` }
  return { dir: null, text: 'pending', note: 'no price yet at this horizon' }
}

/** A short explanation of why a verify run produced nothing (or what went wrong), or null if it worked. */
export function verifyProblem(r: BtVerifyReport, outcomesNow: number): string | null {
  if (r.errors.length) return r.errors[0]
  if (outcomesNow > 0) return null
  if (r.symbols_without_candles.length) return `no candles for ${r.symbols_without_candles.join(', ')}`
  if (r.outcomes_skipped_not_covered) return 'market closed / not covered by candle history'
  if (r.outcomes_skipped_no_data) return 'no candle data'
  if (r.outcomes_skipped_future) return 'not released yet'
  return 'no price outcomes could be computed'
}
