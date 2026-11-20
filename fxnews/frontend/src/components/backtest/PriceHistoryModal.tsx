import { HORIZONS, horizonTooFine, type BtEvent, type Horizon } from '../../api/backtest'
import { flag } from '../../lib/format'
import { Modal } from '../Modal'
import { SymbolLink } from '../SymbolLink'
import { UiButton } from '../UI/UiButton'
import { Spinner } from '../widgets'
import { ARROW, fmtUtc, ret, retTone, sourceLabel } from './btFormat'
import type { RowState } from './useRowActions'

interface Props {
  event: BtEvent
  horizon: Horizon
  state: RowState | undefined
  onVerify: (force: boolean) => void
  onClose: () => void
}

export function EventSummary({ event }: { event: BtEvent }) {
  const t = fmtUtc(event.time)
  return (
    <div className="bt-evsum">
      <div className="bt-evsum-title">{event.title}</div>
      <div className="bt-evsum-meta">
        <span>
          {t.date} {t.hm} UTC
        </span>
        {event.currency ? (
          <span>
            {flag(event.currency)} {event.currency}
          </span>
        ) : null}
        <span>{sourceLabel(event.source)}</span>
        <span className={`bt-imp ${event.impact}`}>{event.impact}</span>
        {event.kind === 'calendar' ? (
          <span>
            act <b>{event.actual ?? '—'}</b> · fcst {event.forecast ?? '—'} · prev {event.previous ?? '—'}
          </span>
        ) : (
          <span>headline</span>
        )}
      </div>
    </div>
  )
}

/** Per-symbol price reaction around the release, with links to the chart marked at the release time. */
export function PriceHistoryModal({ event, horizon, state, onVerify, onClose }: Props) {
  return (
    <Modal title="Price history" onClose={onClose}>
      <div className="bt-modal">
        <EventSummary event={event} />
        {event.outcomes.length === 0 ? (
          <p className="dim">No price outcomes yet.</p>
        ) : (
          <div className="bt-modal-scroll">
            <table className="ct-table bt-table bt-detail">
              <thead>
                <tr>
                  <th>
                    <span className="ct-th-static">Symbol</span>
                  </th>
                  <th>
                    <span className="ct-th-static">Candles</span>
                  </th>
                  <th className="num">
                    <span className="ct-th-static">At release</span>
                  </th>
                  {HORIZONS.map((h) => (
                    <th key={h} className="num">
                      <span className={`ct-th-static${h === horizon ? ' bt-selh' : ''}`}>+{h}</span>
                    </th>
                  ))}
                  <th className="num">
                    <span className="ct-th-static">Max ↑ / ↓ 1h</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {event.outcomes.map((o) => (
                  <tr key={o.symbol} className={`imp-${event.impact}`}>
                    <td className="ct-title">
                      <SymbolLink symbol={o.symbol} time={event.time} title={`Open the ${o.symbol} chart at the release time`} />
                    </td>
                    <td title={`Priced from ${o.interval} candles (the finest interval whose history reaches this event)`}>{o.interval}</td>
                    <td className="num">{o.price_at_release}</td>
                    {HORIZONS.map((h) => {
                      const dir = o.directions[h]
                      const fine = horizonTooFine(o.interval, h)
                      return (
                        <td key={h} className={`num${h === horizon ? ' bt-selcol' : ''}`} title={fine ? `n/a: ${o.interval} candles are coarser than ${h}` : o.prices[h] !== null ? `price ${o.prices[h]}` : undefined}>
                          {fine ? (
                            <span className="dim">n/a</span>
                          ) : dir ? (
                            <span className={`bt-retv ${retTone(o.returns[h])}`}>
                              <span className={`bt-dir ${dir}`}>{ARROW[dir]}</span> {ret(o.returns[h])}
                            </span>
                          ) : (
                            <span className="dim">pending</span>
                          )}
                        </td>
                      )
                    })}
                    <td className="num">
                      <span className="bt-retv up">{ret(o.max_up_1h)}</span> / <span className="bt-retv down">{ret(o.max_down_1h)}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {state?.verifyError ? <div className="bt-err block">⚠ {state.verifyError}</div> : null}
        <div className="bt-modal-foot">
          <span className="dim">Click a symbol to open its chart at the release time.</span>
          <span className="bt-grow" />
          {state?.verifying ? <Spinner label="Verifying" /> : null}
          <UiButton tone="ghost" size="xs" disabled={state?.verifying} onClick={() => onVerify(true)} title="Recompute the outcomes from fresh candles">
            Re-verify
          </UiButton>
        </div>
      </div>
    </Modal>
  )
}
