import { HORIZONS, type BtImpact, type BtJob, type Horizon } from '../../api/backtest'
import type { LlmModel, LlmProvider } from '../../api/llm'
import { UiButton } from '../UI/UiButton'
import { UiSelect } from '../UI/UiSelect'
import { Spinner } from '../widgets'
import { IMPACTS } from './btFormat'
import { ModelPicker } from './ModelPicker'
import { Popover } from './Popover'

export type KindFilter = '' | 'calendar' | 'headline'

export interface BtFilters {
  impacts: BtImpact[]
  kind: KindFilter
  source: string
  currency: string
  symbol: string
  search: string
}

interface Option {
  value: string
  label: string
}

interface Props {
  from: string
  to: string
  onFrom: (v: string) => void
  onTo: (v: string) => void
  busy: boolean
  onBackfill: () => void
  job: BtJob | null
  /** A stop request was sent and the job has not finished yet. */
  stopping: boolean
  onStop: () => void
  onDismissJob: () => void
  actionError: string | null
  onExport: () => void
  canExport: boolean

  filters: BtFilters
  onFilters: (patch: Partial<BtFilters>) => void
  sourceOptions: Option[]
  currencyOptions: Option[]
  symbolOptions: Option[]
  horizon: Horizon
  onHorizon: (h: string) => void

  providers: LlmProvider[]
  models: LlmModel[]
  catalogLoading: boolean
  catalogError: Error | undefined
  onRetryCatalog: () => void
  recent: string[]
  onClearRecent: () => void
  picked: string[]
  onPicked: (next: string[]) => void
  visibleCount: number
  minImpact: BtImpact
  onMinImpact: (v: BtImpact) => void
  maxCalls: string
  onMaxCalls: (v: string) => void
  onPredictAll: () => void

  countText: string
}

function JobStatus({ job, stopping, onStop, onDismiss }: { job: BtJob; stopping: boolean; onStop: () => void; onDismiss: () => void }) {
  const result = job.result
  const brief = result
    ? Object.entries(result)
        .filter(([, v]) => typeof v === 'number')
        .map(([k, v]) => `${k.replace(/_/g, ' ')}: ${String(v)}`)
        .join(' · ')
    : ''
  const errors = result && Array.isArray(result.errors) ? (result.errors as string[]) : []
  const name = job.kind === 'backfill' ? 'Backfill' : 'Predict'
  const running = job.status === 'running'
  const detail = running
    ? null
    : job.status === 'error'
      ? `failed: ${job.error}`
      : job.status === 'cancelled'
        ? `Stopped — partial results kept${brief ? ` (${brief})` : ''}`
        : brief || 'done'
  return (
    <span className={`bt-job ${job.status}`} role="status" title={[job.message, detail, ...errors].filter(Boolean).join('\n')}>
      <strong>{name}</strong>
      {running ? <Spinner label={stopping ? 'Stopping…' : job.message || 'running…'} /> : <span className="bt-job-text">{detail}</span>}
      {errors.length ? <span className="bt-err">{errors.length} error(s)</span> : null}
      {running ? (
        <UiButton tone="danger" size="xs" className="bt-stop" disabled={stopping} onClick={onStop} title="Stop this job. Results already stored are kept.">
          {stopping ? 'Stopping…' : '■ Stop'}
        </UiButton>
      ) : (
        <button type="button" className="bt-x" onClick={onDismiss} aria-label="Dismiss">
          ×
        </button>
      )}
    </span>
  )
}

/** Three compact rows: dates + actions + job status, filters, then the AI models + Predict all. */
export function BacktestToolbar(p: Props) {
  const { filters: f } = p
  const toggleImpact = (i: BtImpact) => p.onFilters({ impacts: f.impacts.includes(i) ? f.impacts.filter((x) => x !== i) : [...f.impacts, i] })
  return (
    <div className="bt-toolbars">
      <div className="bt-toolbar">
        <h2 className="bt-title">Backtest</h2>
        <label className="bt-field">
          <span>From</span>
          <input type="date" value={p.from} max={p.to || undefined} onChange={(e) => p.onFrom(e.target.value)} />
        </label>
        <label className="bt-field">
          <span>To</span>
          <input type="date" value={p.to} min={p.from} onChange={(e) => p.onTo(e.target.value)} title="Empty = now" />
          <UiButton tone="ghost" size="xs" onClick={() => p.onTo('')} disabled={!p.to} title="Up to now">
            Now
          </UiButton>
        </label>
        <UiButton size="xs" disabled={p.busy || !p.from} onClick={p.onBackfill} title="Collect news + calendar for the range and compute price outcomes from candles">
          Backfill range
        </UiButton>
        {p.job ? <JobStatus job={p.job} stopping={p.stopping} onStop={p.onStop} onDismiss={p.onDismissJob} /> : null}
        {p.actionError ? <span className="bt-err">⚠ {p.actionError}</span> : null}
        <span className="bt-grow" />
        <span className="bt-count">{p.countText} · times in UTC</span>
        <UiButton tone="ghost" size="xs" onClick={p.onExport} disabled={!p.canExport}>
          Export CSV
        </UiButton>
      </div>

      <div className="bt-toolbar">
        {IMPACTS.map((i) => (
          <button key={i} type="button" className={`bt-chip imp-${i}${f.impacts.includes(i) ? ' on' : ''}`} onClick={() => toggleImpact(i)} aria-pressed={f.impacts.includes(i)}>
            {i}
          </button>
        ))}
        <UiSelect size="xs" ariaLabel="Kind" className="bt-fsel" value={f.kind} onChange={(v) => p.onFilters({ kind: v as KindFilter })} options={[{ value: '', label: 'All kinds' }, { value: 'calendar', label: 'Calendar' }, { value: 'headline', label: 'Headline' }]} />
        <UiSelect size="xs" ariaLabel="Source" className="bt-fsel" value={f.source} onChange={(v) => p.onFilters({ source: v })} options={p.sourceOptions} />
        <UiSelect size="xs" ariaLabel="Currency" className="bt-fsel" value={f.currency} onChange={(v) => p.onFilters({ currency: v })} options={p.currencyOptions} />
        <UiSelect size="xs" ariaLabel="Symbol" className="bt-fsel" value={f.symbol} onChange={(v) => p.onFilters({ symbol: v })} options={p.symbolOptions} searchable />
        <input className="bt-search" type="search" placeholder="Search title…" aria-label="Search title" value={f.search} onChange={(e) => p.onFilters({ search: e.target.value })} />
        <label className="bt-field">
          <span>Score at</span>
          <UiSelect size="xs" ariaLabel="Horizon used to score predictions" value={p.horizon} onChange={p.onHorizon} options={HORIZONS.map((h) => ({ value: h, label: h }))} />
        </label>
      </div>

      <div className="bt-toolbar">
        <ModelPicker
          providers={p.providers}
          models={p.models}
          loading={p.catalogLoading}
          error={p.catalogError}
          onRetry={p.onRetryCatalog}
          picked={p.picked}
          onChange={p.onPicked}
          recent={p.recent}
          onClearRecent={p.onClearRecent}
        />
        <span className="bt-sep" />
        <UiButton size="xs" disabled={p.busy || p.picked.length === 0 || !p.from} onClick={p.onPredictAll} title={`Run the picked models over the date range (min impact ${p.minImpact}, up to ${p.maxCalls || 'all'} calls per model), skipping events they already predicted. The server works on the date range, so extra filters here are not applied. Costs LLM calls.`}>
          Predict all visible ({p.visibleCount})
        </UiButton>
        <Popover ariaLabel="Bulk predict options" title="Options for Predict all" label="Options" align="right">
          <div className="bt-pop-head">
            <span>Predict all options</span>
          </div>
          <label className="bt-opt">
            <span>Min impact</span>
            <select value={p.minImpact} onChange={(e) => p.onMinImpact(e.target.value as BtImpact)}>
              {IMPACTS.map((i) => (
                <option key={i} value={i}>
                  {i}
                </option>
              ))}
            </select>
          </label>
          <label className="bt-opt">
            <span>Max calls / model</span>
            <input type="number" min={1} max={5000} value={p.maxCalls} onChange={(e) => p.onMaxCalls(e.target.value)} />
          </label>
        </Popover>
      </div>
    </div>
  )
}
