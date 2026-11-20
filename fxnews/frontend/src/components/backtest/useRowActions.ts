import { useCallback, useState } from 'react'
import { backtestApi, type BtEvent, type BtModelRun, type BtVerifyReport } from '../../api/backtest'
import { errMessage, verifyProblem } from './btFormat'

/** Transient per-event state of the in-row Verify / Predict actions. */
export interface RowState {
  verifying?: boolean
  predicting?: boolean
  /** Why the last verify produced nothing or failed. */
  verifyError?: string
  verifyReport?: BtVerifyReport
  /** Per-model results of the last predict run (errors shown inline). */
  runs?: BtModelRun[]
  predictError?: string
}

export interface RowActions {
  state: Record<number, RowState>
  verify: (id: number, force?: boolean) => Promise<void>
  predict: (id: number, models: string[], force?: boolean) => Promise<void>
}

/**
 * Runs the per-event endpoints. `apply` stores the returned event (local override, no list refetch);
 * `changed` is called after every successful call so the page can refresh the summary;
 * `used` receives the models of every predict call (to keep the recent-models list).
 */
export function useRowActions(apply: (event: BtEvent) => void, changed: () => void, used?: (models: string[]) => void): RowActions {
  const [state, setState] = useState<Record<number, RowState>>({})
  const patch = useCallback((id: number, p: RowState) => setState((s) => ({ ...s, [id]: { ...s[id], ...p } })), [])

  const verify = useCallback(
    async (id: number, force = false) => {
      patch(id, { verifying: true, verifyError: undefined })
      try {
        const out = await backtestApi.verifyEvent(id, force)
        apply(out.event)
        patch(id, { verifying: false, verifyReport: out.verify, verifyError: verifyProblem(out.verify, out.event.outcomes.length) ?? undefined })
        changed()
      } catch (err) {
        patch(id, { verifying: false, verifyError: errMessage(err) })
      }
    },
    [apply, changed, patch],
  )

  const predict = useCallback(
    async (id: number, models: string[], force = false) => {
      patch(id, { predicting: true, predictError: undefined })
      used?.(models)
      try {
        const out = await backtestApi.predictEvent(id, models, force)
        apply(out.event)
        patch(id, { predicting: false, runs: out.results })
        changed()
      } catch (err) {
        patch(id, { predicting: false, predictError: errMessage(err) })
      }
    },
    [apply, changed, patch, used],
  )

  return { state, verify, predict }
}
