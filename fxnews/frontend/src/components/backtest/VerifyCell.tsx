import { type BtEvent, type Horizon } from '../../api/backtest'
import { Spinner } from '../widgets'
import { ARROW, outcomeState, ret, retTone } from './btFormat'
import type { RowState } from './useRowActions'

interface Props {
  event: BtEvent
  released: boolean
  horizon: Horizon
  state: RowState | undefined
  onVerify: () => void
  onOpen: () => void
}

/** "Verify history" cell: the verify button, or the compact price result with a click-through to the details. */
export function VerifyCell({ event, released, horizon, state, onVerify, onOpen }: Props) {
  const stop = (e: { stopPropagation: () => void }) => e.stopPropagation()

  if (state?.verifying) {
    return (
      <td className="bt-verify">
        <Spinner label="Verifying" />
      </td>
    )
  }

  if (event.outcomes.length === 0) {
    if (!released) {
      return (
        <td className="bt-verify">
          <span className="dim">pending</span>
        </td>
      )
    }
    return (
      <td className="bt-verify">
        <button type="button" className="bt-mini" title="Fetch candles at the release time and check whether price went up or down" onClick={(e) => { stop(e); onVerify() }}>
          Verify
        </button>
        {state?.verifyError ? (
          <span className="bt-err" title={state.verifyError}>
            {state.verifyError}
          </span>
        ) : null}
      </td>
    )
  }

  const o = event.outcomes[0]
  const st = outcomeState(o, horizon)
  const more = event.outcomes.length - 1
  return (
    <td className="bt-verify has">
      <button type="button" className="bt-result-btn" onClick={(e) => { stop(e); onOpen() }} title={`${o.symbol} · ${horizon} · priced from ${o.interval} candles. Click for the price history`}>
        <span className={`bt-dir ${st.dir ?? 'pending'}`}>
          {st.dir ? ARROW[st.dir] : '·'}
        </span>
        <span className={`bt-retv ${retTone(o.returns[horizon])}`}>{st.dir ? ret(o.returns[horizon]) : st.text}</span>
        <span className="bt-sym-tag">{o.symbol}</span>
        {more > 0 ? <span className="bt-more">+{more}</span> : null}
      </button>
    </td>
  )
}
