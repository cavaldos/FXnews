import type { BtEvent, BtPrediction, Horizon } from '../../api/backtest'
import { isHit } from '../../lib/backtestCsv'
import { Spinner } from '../widgets'
import { ARROW, modelLabel, primarySymbol } from './btFormat'
import type { RowState } from './useRowActions'

interface Props {
  event: BtEvent
  horizon: Horizon
  picked: string[]
  state: RowState | undefined
  onPredict: () => void
  onOpen: () => void
}

const MAX_CHIPS = 3

/** The model's call for the row's primary symbol (falls back to its first call). */
function primaryPrediction(e: BtEvent, preds: BtPrediction[]): BtPrediction | undefined {
  const sym = primarySymbol(e)
  return preds.find((p) => p.symbol === sym) ?? preds[0]
}

/** "AI predict" cell: one chip per model that has predicted (colored by hit/miss), a Predict button, and a click-through to the details. */
export function PredictCell({ event, horizon, picked, state, onPredict, onOpen }: Props) {
  const models = Object.keys(event.predictions).filter((m) => event.predictions[m].length > 0).sort()
  const failed = state?.runs?.filter((r) => r.status === 'error') ?? []
  const errText = state?.predictError ?? (failed.length ? failed.map((r) => `${modelLabel(r.model)}: ${r.error ?? 'error'}`).join('\n') : undefined)

  return (
    <td className="bt-predict">
      <span className="bt-predict-in">
        <button type="button" className="bt-chips" onClick={(e) => { e.stopPropagation(); onOpen() }} title="Open the AI predictions" aria-label="Open the AI predictions">
          {models.length === 0 ? (
            <span className="dim">—</span>
          ) : (
            <>
              {models.slice(0, MAX_CHIPS).map((m) => {
                const pred = primaryPrediction(event, event.predictions[m])
                if (!pred) return null
                const outcome = event.outcomes.find((o) => o.symbol === pred.symbol)
                const hit = isHit(pred.direction, outcome?.directions[horizon])
                const cls = hit === null ? 'pending' : hit ? 'hit' : 'miss'
                return (
                  <span key={m} className={`bt-mchip ${cls}`} title={`${m}: ${pred.symbol} ${pred.direction}${pred.confidence !== null ? ` ${pred.confidence}%` : ''}${hit === null ? ' (not scored yet)' : hit ? ' (hit)' : ' (miss)'}`}>
                    <span className="bt-mchip-name">{modelLabel(m)}</span> {ARROW[pred.direction]}
                  </span>
                )
              })}
              {models.length > MAX_CHIPS ? <span className="bt-more">+{models.length - MAX_CHIPS}</span> : null}
            </>
          )}
        </button>
        {state?.predicting ? (
          <Spinner />
        ) : (
          <button
            type="button"
            className="bt-mini"
            disabled={picked.length === 0}
            title={picked.length === 0 ? 'Pick AI models in the toolbar first' : `Ask ${picked.length} model(s) to predict this event (uses only information published before the release)`}
            onClick={(e) => { e.stopPropagation(); onPredict() }}
          >
            Predict
          </button>
        )}
        {errText ? (
          <span className="bt-err" title={errText}>
            ⚠ {failed.length || 1} failed
          </span>
        ) : null}
      </span>
    </td>
  )
}
