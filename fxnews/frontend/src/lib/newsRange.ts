import { api } from '../api/client'
import type { NewsKind, NewsOut } from '../api/types'

const PAGE = 500
const MAX_PAGES = 6

/**
 * All items of one kind with event_time in [fromMs, toMs).
 * The range is filtered server-side (`from` inclusive, `to` exclusive). A full page means there may be more rows,
 * so keep paging back through the range with the (before, before_id) keyset cursor (sorted by event_time DESC).
 */
export async function fetchNewsRange(kind: NewsKind, fromMs: number, toMs_: number, signal?: AbortSignal): Promise<NewsOut[]> {
  const out: NewsOut[] = []
  const from = new Date(fromMs).toISOString()
  const to = new Date(toMs_).toISOString()
  let before: string | undefined
  let beforeId: number | undefined
  for (let page = 0; page < MAX_PAGES; page++) {
    const rows = await api.news({ kind, from, to, limit: PAGE, before, before_id: beforeId }, signal)
    out.push(...rows)
    const last = rows[rows.length - 1]
    if (rows.length < PAGE || !last?.event_time) break
    // The cursor wants naive UTC: drop the offset the API sends back.
    before = last.event_time.replace(/(Z|[+-]\d{2}:?\d{2})$/i, '').slice(0, 26)
    beforeId = last.id
  }
  return out
}
