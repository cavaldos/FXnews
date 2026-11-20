import { UiSelect } from '../UI/UiSelect'
import { PAGE_SIZES } from './btFormat'

interface Props {
  total: number
  page: number
  pageSize: number
  onPage: (page: number) => void
  onPageSize: (size: number) => void
}

/** Page-size select, First / Prev / "Page X of Y" / Next / Last and the "rows a–b of N" range. */
export function Pager({ total, page, pageSize, onPage, onPageSize }: Props) {
  const pages = Math.max(1, Math.ceil(total / pageSize))
  const from = total === 0 ? 0 : (page - 1) * pageSize + 1
  const to = Math.min(total, page * pageSize)
  return (
    <div className="bt-pager" role="navigation" aria-label="Pagination">
      <span className="bt-pager-size">
        <span>Rows per page</span>
        <UiSelect size="xs" ariaLabel="Rows per page" className="bt-pager-select" value={String(pageSize)} onChange={(v) => onPageSize(Number(v))} options={PAGE_SIZES.map((n) => ({ value: String(n), label: String(n) }))} />
      </span>
      <span className="bt-pager-range">
        rows {from}–{to} of {total}
      </span>
      <span className="bt-pager-nav">
        <button type="button" className="bt-mini" disabled={page <= 1} onClick={() => onPage(1)}>
          « First
        </button>
        <button type="button" className="bt-mini" disabled={page <= 1} onClick={() => onPage(page - 1)}>
          ‹ Prev
        </button>
        <span className="bt-pager-page">
          Page {page} of {pages}
        </span>
        <button type="button" className="bt-mini" disabled={page >= pages} onClick={() => onPage(page + 1)}>
          Next ›
        </button>
        <button type="button" className="bt-mini" disabled={page >= pages} onClick={() => onPage(pages)}>
          Last »
        </button>
      </span>
    </div>
  )
}
