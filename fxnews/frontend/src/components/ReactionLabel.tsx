import type { ReactionHorizonOut } from '../api/types'
import { fmtPct, LABEL_GLYPH, LABEL_TEXT } from '../lib/reaction'

/** "▲ UP +0.12%" coloured by direction; "pending" / "n/a" when the horizon has no result. */
export function ReactionLabel({ h, compact }: { h: ReactionHorizonOut | undefined; compact?: boolean }) {
  if (!h) return <span className="rx-label dim">—</span>
  if (!h.label || h.return_pct === null) {
    return <span className="rx-label dim">{h.status === 'pending' ? 'pending' : 'n/a'}</span>
  }
  return (
    <span className={`rx-label ${h.label}`}>
      <b>
        {LABEL_GLYPH[h.label]}
        {compact ? '' : ` ${LABEL_TEXT[h.label]}`}
      </b>
      <span className="rx-pct">{fmtPct(h.return_pct)}</span>
    </span>
  )
}
