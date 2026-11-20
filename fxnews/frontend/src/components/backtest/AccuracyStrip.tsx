import { HORIZONS, type BtSummary, type Horizon, type ModelSummary, type Tally } from '../../api/backtest'
import { ErrorNote, Spinner } from '../widgets'
import { IMPACTS, modelLabel, pct } from './btFormat'

function TallyLine({ label, t }: { label: string; t: Tally | undefined }) {
  return (
    <span className="bt-tally" title={t ? `${t.hits}/${t.n} hits · directional ${t.directional_hits}/${t.directional_n}` : undefined}>
      <i>{label}</i> {t ? pct(t.hit_rate) : '—'}
      {t ? <small> n{t.n}</small> : null}
    </span>
  )
}

function ModelCard({ m }: { m: ModelSummary }) {
  return (
    <div className="bt-card">
      <div className="bt-card-head">
        <span className="bt-card-name" title={m.model}>
          {modelLabel(m.model)}
        </span>
        <small>{m.model.split(':')[0]}</small>
      </div>
      <div className="bt-card-rate">{pct(m.overall.hit_rate, 1)}</div>
      <div className="bt-card-sub">
        {m.overall.hits}/{m.overall.n} hits · directional {pct(m.overall.directional_hit_rate)} ({m.overall.directional_n}) · {m.predictions} preds
        {m.avg_confidence !== null ? ` · avg conf ${m.avg_confidence}` : ''}
      </div>
      <div className="bt-card-line">
        {IMPACTS.map((i) => (
          <TallyLine key={i} label={i[0].toUpperCase()} t={m.by_impact[i]} />
        ))}
      </div>
      <div className="bt-card-line">
        {HORIZONS.map((h) => (
          <TallyLine key={h} label={h} t={m.by_horizon[h]} />
        ))}
      </div>
    </div>
  )
}

function Cards({ summary, horizon }: { summary: BtSummary; horizon: Horizon }) {
  const base = summary.baseline_by_horizon[horizon]
  return (
    <div className="bt-cards">
      <div className="bt-card base">
        <div className="bt-card-head">
          <span className="bt-card-name">Dataset</span>
          <small>baseline @ {horizon}</small>
        </div>
        <div className="bt-card-rate">{summary.events}</div>
        <div className="bt-card-sub">
          events · {summary.outcomes} outcomes ({summary.events_with_outcomes} events) · {IMPACTS.map((i) => `${i[0].toUpperCase()}${summary.events_by_impact[i] ?? 0}`).join(' ')}
        </div>
        {summary.outcomes_by_interval && Object.keys(summary.outcomes_by_interval).length ? (
          <div className="bt-card-sub" title="Candle interval each outcome was priced from; coarse intervals have no +5m/+15m numbers">
            candles:{' '}
            {Object.entries(summary.outcomes_by_interval)
              .map(([iv, n]) => `${iv} ×${n}`)
              .join(' · ')}
          </div>
        ) : null}
        <div className="bt-card-line">
          {base ? (
            <span className="bt-tally" title={JSON.stringify(base.distribution)}>
              <i>always “{base.majority}”</i> {pct(base.hit_rate)}
              <small> n{base.n}</small>
            </span>
          ) : (
            <span className="bt-tally">no outcomes yet</span>
          )}
        </div>
      </div>
      {summary.models.length === 0 ? <div className="bt-card empty">No predictions yet. Pick AI models and press Predict on a row.</div> : summary.models.map((m) => <ModelCard key={m.model} m={m} />)}
    </div>
  )
}

interface Props {
  open: boolean
  onToggle: () => void
  summary: BtSummary | undefined
  loading: boolean
  error: Error | undefined
  onRetry: () => void
  horizon: Horizon
}

/** Collapsible accuracy section: dataset baseline + one card per model. */
export function AccuracyStrip({ open, onToggle, summary, loading, error, onRetry, horizon }: Props) {
  return (
    <section className="bt-accuracy" aria-label="Accuracy">
      <button type="button" className="bt-toggle bt-acc-head" aria-expanded={open} onClick={onToggle}>
        {open ? '▾' : '▸'} Accuracy
        {summary && !open ? (
          <span className="dim">
            {' '}
            · {summary.events} events · {summary.models.length} model(s)
          </span>
        ) : null}
      </button>
      {open ? error ? <ErrorNote error={error} onRetry={onRetry} /> : summary ? <Cards summary={summary} horizon={horizon} /> : loading ? <Spinner label="Loading summary" /> : null : null}
    </section>
  )
}
