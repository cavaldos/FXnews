import { useCallback, useEffect, useMemo, useState } from 'react'
import { backtestApi, HORIZONS, type BtEvent, type BtEventsOut, type BtImpact, type BtJob, type Horizon } from '../api/backtest'
import { AccuracyStrip } from '../components/backtest/AccuracyStrip'
import { BacktestTable } from '../components/backtest/BacktestTable'
import { BacktestToolbar, type BtFilters } from '../components/backtest/BacktestToolbar'
import { IMPACTS, errMessage, sourceLabel } from '../components/backtest/btFormat'
import { PredictionsModal } from '../components/backtest/PredictionsModal'
import { PriceHistoryModal } from '../components/backtest/PriceHistoryModal'
import { useRowActions } from '../components/backtest/useRowActions'
import { ErrorNote, Spinner } from '../components/widgets'
import { useFetch } from '../hooks/useFetch'
import { llmApi } from '../api/llm'
import { useStoredString } from '../hooks/useLocalStorage'
import { downloadCsv, eventsToCsv } from '../lib/backtestCsv'
import { useRecentModels } from '../lib/recentModels'
import '../style/backtest.css'

const pad = (n: number) => String(n).padStart(2, '0')

function monthStart(): string {
  const d = new Date()
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-01`
}

/** Events replaced locally (after a per-row verify / predict), valid only for the list they were made against. */
interface Overrides {
  base: BtEventsOut | undefined
  map: Record<number, BtEvent>
}
const NO_OVERRIDES: Record<number, BtEvent> = {}

function options(all: string, values: Iterable<string>, label: (v: string) => string = (v) => v): { value: string; label: string }[] {
  return [{ value: '', label: all }, ...[...new Set(values)].sort().map((v) => ({ value: v, label: label(v) }))]
}

export function BacktestPage() {
  const [from, setFrom] = useStoredString('fxnews.bt.from', monthStart())
  const [to, setTo] = useStoredString('fxnews.bt.to', '')
  const [horizonPref, setHorizonPref] = useStoredString('fxnews.bt.horizon', '1h')
  const [impactPref, setImpactPref] = useStoredString('fxnews.bt.impact', 'high,medium,low')
  const [modelsPref, setModelsPref] = useStoredString('fxnews.bt.models', '')
  const [minImpact, setMinImpact] = useStoredString('fxnews.bt.minimpact', 'medium')
  const [limitPref, setLimitPref] = useStoredString('fxnews.bt.limit', '50')
  const [accOpen, setAccOpen] = useStoredString('fxnews.bt.accuracy', '0')
  const [filters, setFilters] = useState<Omit<BtFilters, 'impacts'>>({ kind: '', source: '', currency: '', symbol: '', search: '' })
  const [job, setJob] = useState<BtJob | null>(null)
  const [stopping, setStopping] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [overrides, setOverrides] = useState<Overrides>({ base: undefined, map: NO_OVERRIDES })
  const [priceId, setPriceId] = useState<number | null>(null)
  const [predictId, setPredictId] = useState<number | null>(null)

  const horizon: Horizon = (HORIZONS as readonly string[]).includes(horizonPref) ? (horizonPref as Horizon) : '1h'
  const impacts = useMemo(() => impactPref.split(',').filter((i): i is BtImpact => (IMPACTS as string[]).includes(i)), [impactPref])
  const picked = useMemo(() => modelsPref.split('|').filter(Boolean), [modelsPref])
  const impactParam = impacts.length === IMPACTS.length || impacts.length === 0 ? undefined : impacts

  const query = { from, to: to || undefined, impact: impactParam }
  const key = JSON.stringify(query)
  const events = useFetch(`bt-events:${key}`, (signal) => backtestApi.events(query, signal))
  const accuracyOpen = accOpen === '1'
  const summary = useFetch(accuracyOpen ? `bt-summary:${key}:${filters.symbol}:${horizon}` : null, (signal) =>
    backtestApi.summary({ ...query, symbol: filters.symbol || undefined, horizon }, signal),
  )
  const catalog = useFetch('llm:models', (signal) => llmApi.models(signal))
  const { recent, push: pushRecent, clear: clearRecent } = useRecentModels()
  const reloadEvents = events.reload
  const reloadSummary = summary.reload
  const reloadAll = useCallback(() => {
    reloadEvents()
    reloadSummary()
  }, [reloadEvents, reloadSummary])

  const data = events.data
  const applyEvent = useCallback(
    (ev: BtEvent) => setOverrides((prev) => ({ base: data, map: { ...(prev.base === data ? prev.map : NO_OVERRIDES), [ev.id]: ev } })),
    [data],
  )
  const actions = useRowActions(applyEvent, reloadSummary, pushRecent)

  // Resume a job that was running before a page reload.
  useEffect(() => {
    const ctrl = new AbortController()
    backtestApi.jobs(ctrl.signal).then(
      (list) => {
        const running = list.find((j) => j.status === 'running')
        if (running) setJob((cur) => cur ?? running)
      },
      () => undefined,
    )
    return () => ctrl.abort()
  }, [])

  // Poll a running job (also while a stop is pending); refresh the table whenever it ends.
  useEffect(() => {
    if (!job || job.status !== 'running') return
    const timer = setTimeout(() => {
      backtestApi.job(job.id).then(
        (next) => {
          setJob(next)
          if (next.status !== 'running') {
            setStopping(false)
            reloadAll()
          }
        },
        (err: unknown) => {
          setStopping(false)
          setJob({ ...job, status: 'error', error: errMessage(err), message: 'failed' })
        },
      )
    }, 1500)
    return () => clearTimeout(timer)
  }, [job, reloadAll])

  const start = (promise: Promise<BtJob>) => {
    setActionError(null)
    promise.then(setJob, (err: unknown) => setActionError(errMessage(err)))
  }
  const stop = () => {
    if (!job || job.status !== 'running') return
    setStopping(true)
    backtestApi.cancelJob(job.id).then(
      (next) => {
        setJob(next)
        if (next.status !== 'running') {
          setStopping(false)
          reloadAll()
        }
      },
      (err: unknown) => {
        setStopping(false)
        setActionError(errMessage(err))
      },
    )
  }
  const busy = job?.status === 'running'

  const ov = overrides.base === data ? overrides.map : NO_OVERRIDES
  const all = useMemo(() => (data?.events ?? []).map((e) => ov[e.id] ?? e), [data, ov])

  const sourceOptions = useMemo(() => options('All sources', all.map((e) => e.source), sourceLabel), [all])
  const currencyOptions = useMemo(() => options('All currencies', all.flatMap((e) => (e.currency ? [e.currency] : []))), [all])
  const symbolOptions = useMemo(() => options('All symbols', all.flatMap((e) => e.symbols)), [all])

  const visible = useMemo(() => {
    const q = filters.search.trim().toLowerCase()
    return all.filter(
      (e) =>
        (impacts.length === 0 || impacts.includes(e.impact)) &&
        (!filters.kind || e.kind === filters.kind) &&
        (!filters.source || e.source === filters.source) &&
        (!filters.currency || e.currency === filters.currency) &&
        (!filters.symbol || e.symbols.includes(filters.symbol) || e.outcomes.some((o) => o.symbol === filters.symbol)) &&
        (!q || e.title.toLowerCase().includes(q)),
    )
  }, [all, filters, impacts])

  const problems = useMemo(() => {
    const byProvider = new Map((catalog.data?.providers ?? []).map((p) => [p.name, p.problem]))
    return Object.fromEntries(picked.map((id) => [id, byProvider.get(id.slice(0, id.indexOf(':'))) ?? null]))
  }, [catalog.data, picked])
  const resetKey = `${key}|${impactPref}|${JSON.stringify(filters)}`

  const runAll = () => {
    pushRecent(picked)
    start(backtestApi.predict({ from, to, models: picked, min_impact: minImpact as BtImpact, limit: Number(limitPref) > 0 ? Number(limitPref) : undefined }))
  }

  const exportCsv = () => {
    if (visible.length === 0) return
    const models = [...new Set(visible.flatMap((e) => Object.keys(e.predictions)))].sort()
    downloadCsv(`backtest_${from}_${to || 'now'}.csv`, eventsToCsv(visible, models, horizon))
  }

  const priceEvent = priceId !== null ? all.find((e) => e.id === priceId) : undefined
  const predictEvent = predictId !== null ? all.find((e) => e.id === predictId) : undefined

  return (
    <main className="bt-page">
      <BacktestToolbar
        from={from}
        to={to}
        onFrom={setFrom}
        onTo={setTo}
        busy={busy}
        onBackfill={() => start(backtestApi.backfill(from, to))}
        job={job}
        stopping={stopping}
        onStop={stop}
        onDismissJob={() => setJob(null)}
        actionError={actionError}
        onExport={exportCsv}
        canExport={visible.length > 0}
        filters={{ ...filters, impacts }}
        onFilters={(patch) => {
          const { impacts: nextImpacts, ...rest } = patch
          if (nextImpacts) setImpactPref(nextImpacts.join(','))
          if (Object.keys(rest).length) setFilters((f) => ({ ...f, ...rest }))
        }}
        sourceOptions={sourceOptions}
        currencyOptions={currencyOptions}
        symbolOptions={symbolOptions}
        horizon={horizon}
        onHorizon={setHorizonPref}
        providers={catalog.data?.providers ?? []}
        models={catalog.data?.models ?? []}
        catalogLoading={catalog.loading}
        catalogError={catalog.error}
        onRetryCatalog={catalog.reload}
        recent={recent}
        onClearRecent={clearRecent}
        picked={picked}
        onPicked={(next) => setModelsPref(next.join('|'))}
        visibleCount={visible.length}
        minImpact={(IMPACTS as string[]).includes(minImpact) ? (minImpact as BtImpact) : 'medium'}
        onMinImpact={setMinImpact}
        maxCalls={limitPref}
        onMaxCalls={setLimitPref}
        onPredictAll={runAll}
        countText={data ? `${visible.length} shown · ${data.events.length}/${data.total} events` : ''}
      />

      <AccuracyStrip
        open={accuracyOpen}
        onToggle={() => setAccOpen(accuracyOpen ? '0' : '1')}
        summary={summary.data}
        loading={summary.loading}
        error={summary.error}
        onRetry={reloadSummary}
        horizon={horizon}
      />

      {events.error ? <ErrorNote error={events.error} onRetry={reloadEvents} /> : null}
      {events.loading ? <Spinner label="Loading events" /> : null}
      {data && data.events.length === 0 && !events.error ? <div className="bt-hint">No events in this range. Press “Backfill range” to collect them.</div> : null}

      {data && data.events.length > 0 ? (
        <BacktestTable
          events={visible}
          resetKey={resetKey}
          loading={events.loading}
          horizon={horizon}
          picked={picked}
          actions={actions}
          onOpenPrice={setPriceId}
          onOpenPredict={setPredictId}
        />
      ) : null}

      {priceEvent ? (
        <PriceHistoryModal event={priceEvent} horizon={horizon} state={actions.state[priceEvent.id]} onVerify={(force) => void actions.verify(priceEvent.id, force)} onClose={() => setPriceId(null)} />
      ) : null}
      {predictEvent ? (
        <PredictionsModal
          key={predictEvent.id}
          event={predictEvent}
          horizon={horizon}
          problems={problems}
          initialPicked={picked}
          state={actions.state[predictEvent.id]}
          onRun={(models, force) => void actions.predict(predictEvent.id, models, force)}
          onClose={() => setPredictId(null)}
        />
      ) : null}
    </main>
  )
}
