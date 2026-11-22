import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type { BtEvent, BtPrediction, Horizon } from '../../api/backtest'
import { isHit } from '../../lib/backtestCsv'
import { ARROW, modelLabel, primarySymbol } from './btFormat'
import type { RowState } from './useRowActions'

interface Props {
  event: BtEvent
  horizon: Horizon
  /** Models selected in the toolbar (preselected in the menu). */
  picked: string[]
  /** Recently used models (offered in the menu too). */
  recent: string[]
  state: RowState | undefined
  onPredict: (models: string[], force: boolean) => void
  onOpen: () => void
}

/** The model's call for the row's primary symbol (falls back to its first call). */
function primaryPrediction(e: BtEvent, preds: BtPrediction[]): BtPrediction | undefined {
  const sym = primarySymbol(e)
  return preds.find((p) => p.symbol === sym) ?? preds[0]
}

function provider(id: string): string {
  const i = id.indexOf(':')
  return i > 0 ? id.slice(0, i) : ''
}

/** Floating model menu, portaled to <body> so the table's scroll box never clips it. */
function ModelMenu({
  anchor,
  candidates,
  done,
  initial,
  onRun,
  onDetails,
  onClose,
}: {
  anchor: HTMLElement
  candidates: string[]
  done: Set<string>
  initial: string[]
  onRun: (models: string[], force: boolean) => void
  onDetails: () => void
  onClose: () => void
}) {
  const [sel, setSel] = useState<string[]>(initial)
  const [force, setForce] = useState(false)
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null)
  const ref = useRef<HTMLDivElement>(null)

  useLayoutEffect(() => {
    const place = () => {
      const r = anchor.getBoundingClientRect()
      const w = ref.current?.offsetWidth ?? 280
      const h = ref.current?.offsetHeight ?? 240
      const below = r.bottom + 4 + h <= window.innerHeight
      setPos({
        top: below ? r.bottom + 4 : Math.max(8, r.top - 4 - h),
        left: Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8)),
      })
    }
    place()
    window.addEventListener('resize', place)
    window.addEventListener('scroll', place, true)
    return () => {
      window.removeEventListener('resize', place)
      window.removeEventListener('scroll', place, true)
    }
  }, [anchor])

  useEffect(() => {
    const onDown = (e: PointerEvent) => {
      const t = e.target as Node
      if (!ref.current?.contains(t) && !anchor.contains(t)) onClose()
    }
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('pointerdown', onDown)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('pointerdown', onDown)
      window.removeEventListener('keydown', onKey)
    }
  }, [anchor, onClose])

  const toggle = (m: string) => setSel((s) => (s.includes(m) ? s.filter((x) => x !== m) : [...s, m]))
  // Without "re-run", models that already answered are skipped by the backend.
  const effective = force ? sel : sel.filter((m) => !done.has(m))

  return createPortal(
    <div
      ref={ref}
      className="bt-pmenu"
      role="dialog"
      aria-label="Choose AI models"
      style={pos ? { top: pos.top, left: pos.left } : { visibility: 'hidden', top: 0, left: 0 }}
      onClick={(e) => e.stopPropagation()}
    >
      <div className="bt-pmenu-head">Predict with</div>
      {candidates.length === 0 ? (
        <div className="bt-pmenu-empty">No models yet. Add one with “+ Add model” in the toolbar.</div>
      ) : (
        <ul className="bt-pmenu-list">
          {candidates.map((m) => (
            <li key={m}>
              <label title={m}>
                <input type="checkbox" checked={sel.includes(m)} onChange={() => toggle(m)} />
                <span className="bt-pmenu-name">{modelLabel(m)}</span>
                <small>{provider(m)}</small>
                {done.has(m) ? <small className="bt-pmenu-done">done</small> : null}
              </label>
            </li>
          ))}
        </ul>
      )}
      <label className="bt-pmenu-force">
        <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} />
        Re-run models that already answered
      </label>
      <div className="bt-pmenu-actions">
        <button type="button" className="bt-mini" onClick={onDetails}>
          Details
        </button>
        <button
          type="button"
          className="bt-mini primary"
          disabled={effective.length === 0}
          title={sel.length > 0 && effective.length === 0 ? 'All selected models already answered; tick “Re-run” to ask again' : undefined}
          onClick={() => {
            onRun(effective, force)
            onClose()
          }}
        >
          Run {effective.length > 0 ? `(${effective.length})` : ''}
        </button>
      </div>
      <div className="bt-pmenu-note">Models only see information published before the release.</div>
    </div>,
    document.body,
  )
}

/** "AI predict" cell: every model's call as a chip (hit/miss colored), running models with a spinner, and a Predict menu to choose models. */
export function PredictCell({ event, horizon, picked, recent, state, onPredict, onOpen }: Props) {
  // The Predict button the menu is anchored to (null = menu closed).
  const [anchor, setAnchor] = useState<HTMLElement | null>(null)
  const menuOpen = anchor !== null
  const models = Object.keys(event.predictions).filter((m) => event.predictions[m].length > 0).sort()
  const done = new Set(models)
  const running = state?.predicting ? (state.pendingModels ?? []) : []
  const failed = state?.runs?.filter((r) => r.status === 'error') ?? []
  const failedSet = new Set(failed.map((r) => r.model))
  const candidates = [...new Set([...picked, ...recent, ...models])]

  return (
    <td className="bt-predict">
      <div className="bt-predict-in">
        {models.length > 0 || running.length > 0 || failed.length > 0 ? (
          <button type="button" className="bt-chips" onClick={(e) => { e.stopPropagation(); onOpen() }} title="Open the AI prediction details">
            {models.map((m) => {
              if (running.includes(m)) return null
              const pred = primaryPrediction(event, event.predictions[m])
              if (!pred) return null
              const outcome = event.outcomes.find((o) => o.symbol === pred.symbol)
              const hit = isHit(pred.direction, outcome?.directions[horizon])
              const cls = hit === null ? 'pending' : hit ? 'hit' : 'miss'
              return (
                <span
                  key={m}
                  className={`bt-mchip ${cls}`}
                  title={`${m}\n${pred.symbol} ${pred.direction}${pred.confidence !== null ? ` · ${pred.confidence}%` : ''}${hit === null ? ' (not scored yet)' : hit ? ' (hit)' : ' (miss)'}${pred.reasoning ? `\n${pred.reasoning}` : ''}`}
                >
                  <span className="bt-mchip-name">{modelLabel(m)}</span>
                  <span className="bt-mchip-dir">
                    {ARROW[pred.direction]}
                    {pred.confidence !== null ? ` ${pred.confidence}` : ''}
                  </span>
                </span>
              )
            })}
            {running.map((m) => (
              <span key={`run-${m}`} className="bt-mchip running" title={`${m}: predicting…`}>
                <span className="bt-mchip-name">{modelLabel(m)}</span>
                <span className="bt-mchip-spin" aria-label="running" />
              </span>
            ))}
            {failed
              .filter((r) => !running.includes(r.model) && !done.has(r.model))
              .map((r) => (
                <span key={`err-${r.model}`} className="bt-mchip miss" title={`${r.model}: ${r.error ?? 'error'}`}>
                  <span className="bt-mchip-name">{modelLabel(r.model)}</span> ⚠
                </span>
              ))}
          </button>
        ) : null}
        <button
          type="button"
          className={`bt-mini${menuOpen ? ' open' : ''}`}
          aria-haspopup="dialog"
          aria-expanded={menuOpen}
          title="Choose AI models to predict this event"
          onClick={(e) => {
            e.stopPropagation()
            const target = e.currentTarget
            setAnchor((a) => (a ? null : target))
          }}
        >
          {models.length ? '+ Predict' : 'Predict'} ▾
        </button>
        {state?.predictError ? (
          <span className="bt-err" title={state.predictError}>
            ⚠ failed
          </span>
        ) : failedSet.size > 0 && !state?.predicting ? (
          <span className="bt-err" title={failed.map((r) => `${r.model}: ${r.error ?? 'error'}`).join('\n')}>
            ⚠ {failedSet.size}
          </span>
        ) : null}
      </div>
      {anchor ? (
        <ModelMenu
          anchor={anchor}
          candidates={candidates}
          done={done}
          initial={picked.length ? picked : candidates.slice(0, 1)}
          onRun={onPredict}
          onDetails={() => {
            setAnchor(null)
            onOpen()
          }}
          onClose={() => setAnchor(null)}
        />
      ) : null}
    </td>
  )
}
