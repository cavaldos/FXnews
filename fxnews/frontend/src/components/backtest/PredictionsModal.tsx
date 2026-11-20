import { Fragment, useMemo, useState } from 'react'
import { backtestApi, type BtEvent, type BtPrediction, type Horizon } from '../../api/backtest'
import { useFetch } from '../../hooks/useFetch'
import { isHit } from '../../lib/backtestCsv'
import { Modal } from '../Modal'
import { SymbolLink } from '../SymbolLink'
import { UiButton } from '../UI/UiButton'
import { ErrorNote, Spinner } from '../widgets'
import { ARROW, modelLabel } from './btFormat'
import { ModelChecklist } from './ModelPicker'
import { EventSummary } from './PriceHistoryModal'
import type { RowState } from './useRowActions'

interface Props {
  event: BtEvent
  horizon: Horizon
  /** model id -> why it cannot run (null/missing = ready). */
  problems: Record<string, string | null>
  /** Global picker selection: the modal starts from it. */
  initialPicked: string[]
  state: RowState | undefined
  onRun: (models: string[], force: boolean) => void
  onClose: () => void
}

interface Line {
  key: string
  model: string
  pred: BtPrediction
}

function PromptSection({ eventId, horizon }: { eventId: number; horizon: Horizon }) {
  const [open, setOpen] = useState(false)
  const prompt = useFetch(open ? `bt-prompt:${eventId}:${horizon}` : null, (signal) => backtestApi.prompt(eventId, horizon, signal))
  return (
    <div className="bt-prompt">
      <button type="button" className="bt-toggle" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        {open ? '▾' : '▸'} {open ? 'Hide prompt' : 'Show prompt'}
        <span className="dim"> · the exact text the models see (audit for look-ahead leakage)</span>
      </button>
      {open ? (
        prompt.error ? (
          <ErrorNote error={prompt.error} onRetry={prompt.reload} />
        ) : prompt.data ? (
          <div className="bt-prompt-body">
            <div className="bt-prompt-label">System</div>
            <pre>{prompt.data.system}</pre>
            <div className="bt-prompt-label">Prompt</div>
            <pre>{prompt.data.prompt}</pre>
          </div>
        ) : (
          <Spinner label="Loading prompt" />
        )
      ) : null}
    </div>
  )
}

/** All AI calls for one event (rows = model x symbol), model selection, re-run, and the exact prompt. */
export function PredictionsModal({ event, horizon, problems, initialPicked, state, onRun, onClose }: Props) {
  const existing = useMemo(() => Object.keys(event.predictions).filter((m) => event.predictions[m].length > 0).sort(), [event.predictions])
  const ids = useMemo(() => [...new Set([...initialPicked, ...existing])], [initialPicked, existing])
  const [picked, setPicked] = useState<string[]>(initialPicked)
  const [openKeys, setOpenKeys] = useState<Set<string>>(new Set())

  const lines: Line[] = existing.flatMap((m) => event.predictions[m].map((pred) => ({ key: `${m}|${pred.symbol}`, model: m, pred })))
  const toggle = (k: string) =>
    setOpenKeys((s) => {
      const n = new Set(s)
      if (n.has(k)) n.delete(k)
      else n.add(k)
      return n
    })
  const busy = Boolean(state?.predicting)

  return (
    <Modal title="AI predictions" onClose={onClose}>
      <div className="bt-modal">
        <EventSummary event={event} />
        <div className="bt-note">Models only see information published before the release time. The actual result is never part of the prompt.</div>

        <div className="bt-modal-sec">
          <div className="bt-sec-head">Models to run</div>
          <ModelChecklist ids={ids} picked={picked} problems={problems} onChange={setPicked} />
          <div className="bt-modal-actions">
            <UiButton size="xs" disabled={busy || picked.length === 0} onClick={() => onRun(picked, false)} title="Run the selected models that have not predicted this event yet">
              Run
            </UiButton>
            <UiButton tone="ghost" size="xs" disabled={busy || picked.length === 0} onClick={() => onRun(picked, true)} title="Run again even if a model already predicted (replaces its calls)">
              Re-run (force)
            </UiButton>
            {busy ? <Spinner label="Calling models" /> : null}
          </div>
          {state?.predictError ? <div className="bt-err block">⚠ {state.predictError}</div> : null}
          {state?.runs?.length ? (
            <ul className="bt-runs">
              {state.runs.map((r) => (
                <li key={r.model} className={r.status}>
                  <b>{modelLabel(r.model)}</b> {r.status === 'ok' ? `ok · ${r.predictions_created} prediction(s)${r.latency_ms !== null ? ` · ${r.latency_ms} ms` : ''}` : r.status === 'skipped' ? 'skipped (already predicted)' : `error: ${r.error ?? 'unknown'}`}
                </li>
              ))}
            </ul>
          ) : null}
        </div>

        <div className="bt-modal-scroll">
          {lines.length === 0 ? (
            <p className="dim">No predictions for this event yet.</p>
          ) : (
            <table className="ct-table bt-table bt-detail">
              <thead>
                <tr>
                  {['Model', 'Symbol', 'Call', 'Conf', 'Horizon', 'Latency', `Result @ ${horizon}`, ''].map((h, i) => (
                    <th key={i}>
                      <span className="ct-th-static">{h}</span>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {lines.map(({ key, model, pred }) => {
                  const outcome = event.outcomes.find((o) => o.symbol === pred.symbol)
                  const actual = outcome?.directions[horizon] ?? null
                  const hit = isHit(pred.direction, actual)
                  const expanded = openKeys.has(key)
                  return (
                    <Fragment key={key}>
                      <tr className={`imp-${event.impact}`}>
                        <td className="ct-title" title={model}>
                          {modelLabel(model)} <span className="dim">{model.split(':')[0]}</span>
                        </td>
                        <td>
                          <SymbolLink symbol={pred.symbol} time={event.time} />
                        </td>
                        <td>
                          <span className={`bt-dir ${pred.direction}`}>
                            {ARROW[pred.direction]} {pred.direction}
                          </span>
                        </td>
                        <td className="num">{pred.confidence !== null ? `${pred.confidence}%` : '—'}</td>
                        <td>{pred.horizon ?? '—'}</td>
                        <td className="num">{pred.latency_ms !== null ? `${pred.latency_ms} ms` : '—'}</td>
                        <td>
                          {hit === null ? (
                            <span className="dim">{outcome ? 'pending' : 'not verified'}</span>
                          ) : (
                            <span className={`bt-hit ${hit ? 'hit' : 'miss'}`}>
                              {hit ? 'HIT' : 'MISS'} <span className="dim">actual {actual ? `${ARROW[actual]} ${actual}` : '—'}</span>
                            </span>
                          )}
                        </td>
                        <td>
                          {pred.reasoning ? (
                            <button type="button" className="bt-toggle" aria-expanded={expanded} onClick={() => toggle(key)}>
                              {expanded ? '▾ reasoning' : '▸ reasoning'}
                            </button>
                          ) : (
                            <span className="dim">—</span>
                          )}
                        </td>
                      </tr>
                      {expanded && pred.reasoning ? (
                        <tr className="bt-reason-row">
                          <td colSpan={8}>{pred.reasoning}</td>
                        </tr>
                      ) : null}
                    </Fragment>
                  )
                })}
              </tbody>
            </table>
          )}
        </div>

        <PromptSection eventId={event.id} horizon={horizon} />
      </div>
    </Modal>
  )
}
