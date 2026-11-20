import { HORIZONS, type BtEvent, type Direction, type Horizon } from '../api/backtest'

function cell(v: unknown): string {
  if (v === null || v === undefined) return ''
  const s = String(v)
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
}

export function isHit(pred: Direction | undefined, actual: Direction | null | undefined): boolean | null {
  if (!pred || !actual) return null
  return pred === actual
}

/** One CSV row per (event, symbol): event facts, outcome at every horizon, and each model's call + hit at `horizon`. Events with no outcome yet still get a row per related symbol (empty price columns). */
export function eventsToCsv(events: BtEvent[], models: string[], horizon: Horizon): string {
  const head = [
    'event_id', 'time_utc', 'currency', 'kind', 'source', 'impact', 'title', 'actual', 'forecast', 'previous', 'category',
    'symbol', 'candle_interval', 'price_at_release',
    ...HORIZONS.flatMap((h) => [`ret_${h}_pct`, `dir_${h}`]),
    'max_up_1h_pct', 'max_down_1h_pct',
    ...models.flatMap((m) => [`${m}|direction`, `${m}|confidence`, `${m}|hit@${horizon}`]),
  ]
  const lines = [head.map(cell).join(',')]
  for (const e of events) {
    const symbols = e.outcomes.length ? e.outcomes.map((o) => o.symbol) : e.symbols.length ? e.symbols : ['']
    for (const sym of symbols) {
      const o = e.outcomes.find((x) => x.symbol === sym)
      const row: unknown[] = [e.id, e.time, e.currency, e.kind, e.source, e.impact, e.title, e.actual, e.forecast, e.previous, e.category, sym, o?.interval, o?.price_at_release]
      for (const h of HORIZONS) row.push(o?.returns[h] ?? '', o?.directions[h] ?? '')
      row.push(o?.max_up_1h ?? '', o?.max_down_1h ?? '')
      for (const m of models) {
        const p = (e.predictions[m] ?? []).find((x) => x.symbol === sym)
        const hit = isHit(p?.direction, o?.directions[horizon])
        row.push(p?.direction ?? '', p?.confidence ?? '', hit === null ? '' : hit ? 1 : 0)
      }
      lines.push(row.map(cell).join(','))
    }
  }
  return lines.join('\n')
}

export function downloadCsv(filename: string, csv: string): void {
  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }))
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}
