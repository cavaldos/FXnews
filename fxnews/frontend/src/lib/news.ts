import type { Impact, NewsOut, SymbolOut } from '../api/types'
import { toMs } from './format'

export const IMPACT_RANK: Record<string, number> = { High: 3, Medium: 2, Low: 1, Holiday: 0 }

export function impactRank(impact?: Impact | null): number {
  return impact ? (IMPACT_RANK[impact] ?? 0) : 0
}

/** Time the item "happened": calendar release / headline publish time, else when we fetched it. */
export function itemTime(n: NewsOut): number {
  return toMs(n.event_time) ?? toMs(n.fetched_at) ?? 0
}

export function currenciesOf(n: NewsOut): string[] {
  const set = new Set<string>(n.currencies ?? [])
  if (n.currency) set.add(n.currency)
  return [...set]
}

/** Currencies that matter for a symbol (its base and quote). */
export function symbolCurrencies(sym?: SymbolOut): string[] {
  return sym ? [sym.base, sym.quote] : []
}

/** News relates to a symbol if mapped to it or touches its base/quote currency. */
export function isRelated(n: NewsOut, sym?: SymbolOut): boolean {
  if (!sym) return true
  if (n.symbols?.includes(sym.symbol)) return true
  const cur = currenciesOf(n)
  return cur.includes(sym.base) || cur.includes(sym.quote)
}

export interface NewsFilter {
  sym?: SymbolOut
  /** Restrict to the selected symbol's currencies. */
  onlyRelated: boolean
  /** Explicit currency ('' = any); only used when `onlyRelated` is off. */
  currency: string
  /** Minimum-exact impact ('' = any). */
  impact: Impact | ''
}

export function filterNews(items: NewsOut[], f: NewsFilter): NewsOut[] {
  return items.filter((n) => {
    if (f.impact && n.impact !== f.impact) return false
    if (f.onlyRelated) return isRelated(n, f.sym)
    if (f.currency) return currenciesOf(n).includes(f.currency)
    return true
  })
}

/** Merge lists by id; earlier lists win on conflict. */
export function mergeNews(...lists: NewsOut[][]): NewsOut[] {
  const seen = new Map<number, NewsOut>()
  for (const list of lists) {
    for (const n of list) if (!seen.has(n.id)) seen.set(n.id, n)
  }
  return [...seen.values()]
}

export function sortNewest(items: NewsOut[]): NewsOut[] {
  return [...items].sort((a, b) => itemTime(b) - itemTime(a))
}

export function sortOldest(items: NewsOut[]): NewsOut[] {
  return [...items].sort((a, b) => itemTime(a) - itemTime(b))
}
