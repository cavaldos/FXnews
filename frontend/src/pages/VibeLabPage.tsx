import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AreaSeries,
  ColorType,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type Time,
} from 'lightweight-charts'
import {
  equityPoints,
  vibeApi,
  type BacktestJob,
  type BacktestRequest,
  type MacroSeries,
  type StrategyKind,
  type SwarmPreset,
  type SwarmRun,
  type VibeStatus,
} from '../api/vibe'
import { UiButton } from '../components/UI/UiButton'
import { UiSelect, type UiSelectSize } from '../components/UI/UiSelect'
import { ErrorNote, Spinner } from '../components/widgets'
import { useFetch } from '../hooks/useFetch'
import { readTheme, useChartTheme } from '../lib/theme'
import '../style/vibe.css'

type Tab = 'swarm' | 'backtest' | 'macro'

const TABS: { id: Tab; label: string }[] = [
  { id: 'swarm', label: 'Research swarm' },
  { id: 'backtest', label: 'FX backtest' },
  { id: 'macro', label: 'Macro' },
]

const HIGHLIGHT_PRESET = 'macro_rates_fx_desk'
const RUN_ID_KEY = 'fxnews.vibe.runId'
const SWARM_POLL_MS = 5000
const JOB_POLL_MS = 3000

const STRATEGY_KINDS: { value: StrategyKind; label: string }[] = [
  { value: 'sma_cross', label: 'SMA cross' },
  { value: 'rsi_reversion', label: 'RSI reversion' },
  { value: 'donchian_breakout', label: 'Donchian breakout' },
  { value: 'custom', label: 'Custom (SignalEngine)' },
]

/** Numeric params per strategy kind (custom takes Python source instead). */
const STRATEGY_PARAMS: Record<Exclude<StrategyKind, 'custom'>, { name: string; label: string; def: number }[]> = {
  sma_cross: [
    { name: 'fast', label: 'Fast SMA', def: 10 },
    { name: 'slow', label: 'Slow SMA', def: 50 },
  ],
  rsi_reversion: [
    { name: 'period', label: 'RSI period', def: 14 },
    { name: 'low', label: 'Oversold level', def: 30 },
    { name: 'high', label: 'Overbought level', def: 70 },
  ],
  donchian_breakout: [{ name: 'lookback', label: 'Lookback', def: 20 }],
}

const BT_INTERVALS = ['1m', '5m', '15m', '30m', '1H', '4H', '1D', '1W'] as const

const MACRO_QUICK = ['DFF', 'CPIAUCSL', 'UNRATE', 'DGS10', 'T10Y2Y'] as const
const MACRO_DEFAULT_SERIES = 'DFF'

function errMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

function readStored(key: string): string {
  try {
    return localStorage.getItem(key) ?? ''
  } catch {
    return ''
  }
}

function writeStored(key: string, value: string) {
  try {
    localStorage.setItem(key, value)
  } catch {
    /* storage unavailable */
  }
}

const pad = (n: number) => String(n).padStart(2, '0')
const isoDate = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`

function num(value: string): number | undefined {
  const n = Number(value)
  return value.trim() !== '' && Number.isFinite(n) ? n : undefined
}

/** Metrics carried as fractions (0.12 = 12%) — the sidecar contract lists these by name. */
const FRACTION_METRICS = new Set(['annual_return', 'max_drawdown', 'win_rate'])

function fmtMetric(key: string, value: number | string): string {
  if (typeof value === 'number') {
    if (FRACTION_METRICS.has(key)) return `${(value * 100).toFixed(2)}%`
    return value.toLocaleString('en-GB', { maximumFractionDigits: 6 })
  }
  return value
}

// ── shared line/area chart (equity curve, macro series) ─────────────────────

interface ChartPoint {
  time: string | number
  value: number
}

function VibeChart({ points, height = 260, area = true }: { points: ChartPoint[]; height?: number; area?: boolean }) {
  const boxRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Area'> | ISeriesApi<'Line'> | null>(null)
  const theme = useChartTheme()

  useEffect(() => {
    const box = boxRef.current
    if (!box) return
    const t = readTheme()
    const chart = createChart(box, {
      autoSize: true,
      height,
      layout: { background: { type: ColorType.Solid, color: 'transparent' }, textColor: t.text, fontFamily: t.fontFamily },
      grid: { vertLines: { color: t.grid }, horzLines: { color: t.grid } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false },
      crosshair: { vertLine: { color: t.border }, horzLine: { color: t.border } },
    })
    seriesRef.current = chart.addSeries(AreaSeries, {
      lineColor: t.accent,
      topColor: area ? t.accent : 'transparent',
      bottomColor: 'transparent',
      lineWidth: 2,
    })
    chartRef.current = chart
    return () => {
      chart.remove()
      chartRef.current = null
      seriesRef.current = null
    }
    // height changes are rare (fixed per use); recreating on theme switch is handled by applyOptions below
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    const t = theme
    chartRef.current?.applyOptions({
      layout: { textColor: t.text, fontFamily: t.fontFamily },
      grid: { vertLines: { color: t.grid }, horzLines: { color: t.grid } },
      crosshair: { vertLine: { color: t.border }, horzLine: { color: t.border } },
    })
    const s = seriesRef.current
    if (s) {
      s.applyOptions({ lineColor: t.accent, topColor: area ? t.accent : 'transparent' })
    }
  }, [theme, area])

  useEffect(() => {
    const s = seriesRef.current
    if (!s) return
    s.setData(points.map((p) => ({ time: p.time as Time, value: p.value })))
    if (points.length > 0) chartRef.current?.timeScale().fitContent()
  }, [points])

  if (points.length === 0) return <div className="vibe-chart-empty">No chartable points.</div>
  return <div ref={boxRef} style={{ height }} className="vibe-chart" />
}

// ── status banner ──────────────────────────────────────────────────────────

function StatusBanner({ status, loading, error, onRetry }: { status?: VibeStatus; loading: boolean; error?: Error; onRetry: () => void }) {
  if (loading) return <div className="vibe-banner loading"><Spinner label="Checking Vibe-Trading status…" /></div>
  if (error && !status) return <ErrorNote error={error} onRetry={onRetry} />
  if (!status) return null
  const hint = 'Start the sidecar (sidecar/vibe-trading/run.sh) and set VIBE_ENABLED=true, then restart the backend.'
  if (!status.enabled) {
    return (
      <div className="vibe-banner warn">
        <b>Vibe-Trading is disabled.</b> {hint} <UiButton tone="ghost" size="xs" onClick={onRetry}>Recheck</UiButton>
      </div>
    )
  }
  if (!status.reachable) {
    return (
      <div className="vibe-banner warn">
        <b>Sidecar unreachable{status.error ? `: ${status.error}` : ''}.</b> {hint}{' '}
        <span className="dim">({status.base_url})</span> <UiButton tone="ghost" size="xs" onClick={onRetry}>Recheck</UiButton>
      </div>
    )
  }
  return (
    <div className="vibe-banner ok">
      Vibe-Trading online{status.server_name ? ` · ${status.server_name}` : ''}
      {status.server_version ? ` v${status.server_version}` : ''} · {status.tool_count ?? '?'} MCP tools
      <span className="dim"> · {status.base_url}</span>
    </div>
  )
}

// ── tab 1: research swarm ───────────────────────────────────────────────────

function SwarmTab({ reachable }: { reachable: boolean }) {
  const presetsQ = useFetch(reachable ? 'vibe:presets' : null, (s) => vibeApi.presets(s))
  const presets: SwarmPreset[] = presetsQ.data ?? []
  const [presetName, setPresetName] = useState('')
  const [variables, setVariables] = useState<Record<string, string>>({})
  const [run, setRun] = useState<SwarmRun | null>(null)
  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState<string | null>(null)
  const [resumeFailed, setResumeFailed] = useState(false)

  // The default is derived (highlighted preset first): no effect needed, the fallback re-picks while unset.
  const preset = presets.find((p) => p.name === presetName) ?? presets.find((p) => p.name === HIGHLIGHT_PRESET) ?? presets[0]

  // Resume the last run after a page refresh.
  useEffect(() => {
    const saved = readStored(RUN_ID_KEY)
    if (!saved || run) return
    const ctrl = new AbortController()
    vibeApi.swarmRun(saved, ctrl.signal).then(
      (r) => setRun(r),
      () => setResumeFailed(true),
    )
    return () => ctrl.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Poll while the swarm is working.
  useEffect(() => {
    if (!run || run.ready) return
    const timer = setTimeout(() => {
      vibeApi.swarmRun(run.run_id).then(
        (next) => setRun(next),
        (err: unknown) => setRun({ ...run, status: 'failed', error: errMessage(err), ready: true }),
      )
    }, SWARM_POLL_MS)
    return () => clearTimeout(timer)
  }, [run])

  const start = () => {
    if (!preset) return
    setStarting(true)
    setStartError(null)
    const missing = preset.variables.filter((v) => v.required && !variables[v.name]?.trim())
    if (missing.length > 0) {
      setStartError(`Required variable missing: ${missing.map((v) => v.name).join(', ')}`)
      setStarting(false)
      return
    }
    const clean = Object.fromEntries(Object.entries(variables).filter(([, v]) => v.trim() !== ''))
    vibeApi
      .startSwarm(preset.name, clean)
      .then((r) => {
        writeStored(RUN_ID_KEY, r.run_id)
        setRun(r)
      })
      .catch((err: unknown) => setStartError(errMessage(err)))
      .finally(() => setStarting(false))
  }

  const busy = starting || (run !== null && !run.ready)

  return (
    <div className="vibe-panel">
      <div className="vibe-panel-head">
        <h2>Research swarm</h2>
        <p>A multi-agent research desk. Pick a preset, fill the variables, and the swarm returns a markdown report.</p>
      </div>
      <div className="vibe-panel-body">
        {presetsQ.error ? <ErrorNote error={presetsQ.error} onRetry={presetsQ.reload} /> : null}
        {presetsQ.loading && presets.length === 0 ? <Spinner label="Loading presets" /> : null}
        {preset ? (
          <div className="vibe-form">
            <div className="vibe-field">
              <label className="vibe-label" htmlFor="vibe-preset">Preset</label>
              <UiSelect
                size="sm"
                value={preset.name}
                muted={!presetName}
                ariaLabel="Swarm preset"
                options={presets.map((p) => ({
                  value: p.name,
                  label: `${p.name === HIGHLIGHT_PRESET ? '★ ' : ''}${p.title || p.name} (${p.agent_count} agents)`,
                }))}
                onChange={(v) => {
                  setPresetName(v)
                  setVariables({}) // preset switch: fresh variable drafts
                }}
              />
              {preset.description ? <p className="vibe-hint">{preset.description}</p> : null}
            </div>
            {preset.variables.map((v) => (
              <div className="vibe-field" key={v.name}>
                <label className="vibe-label" htmlFor={`vibe-var-${v.name}`}>
                  {v.name}
                  {v.required ? <span className="vibe-req"> required</span> : null}
                </label>
                <input
                  id={`vibe-var-${v.name}`}
                  value={variables[v.name] ?? ''}
                  placeholder={v.description || v.name}
                  onChange={(e) => setVariables((prev) => ({ ...prev, [v.name]: e.target.value }))}
                />
                {v.description ? <p className="vibe-hint">{v.description}</p> : null}
              </div>
            ))}
            <div className="vibe-actions">
              <UiButton tone="primary" size="sm" onClick={start} disabled={busy}>
                {busy ? <Spinner label="Running…" /> : 'Run swarm'}
              </UiButton>
              {startError ? <span className="vibe-error" role="alert">{startError}</span> : null}
            </div>
          </div>
        ) : null}

        {resumeFailed ? <p className="vibe-hint">The saved run from a previous session is gone (sidecar restarted?). Start a new one.</p> : null}

        {run ? (
          <div className="vibe-run">
            <div className="vibe-run-head">
              <span className={`vibe-status s-${run.status}`}>{run.status}</span>
              <span className="dim">{run.run_id}</span>
              {run.preset ? <span className="dim">preset {run.preset}</span> : null}
              {!run.ready ? <Spinner /> : null}
              {(run.total_input_tokens ?? 0) + (run.total_output_tokens ?? 0) > 0 ? (
                <span className="dim">
                  tokens: {run.total_input_tokens ?? 0} in · {run.total_output_tokens ?? 0} out
                </span>
              ) : null}
            </div>
            {run.error ? <ErrorNote error={new Error(run.error)} /> : null}
            {run.tasks.length > 0 ? (
              <ul className="vibe-tasks">
                {run.tasks.map((t, i) => (
                  <li key={t.id ?? i}>
                    <span className={`vibe-status s-${(t.status ?? 'pending').toLowerCase()}`}>{t.status ?? 'pending'}</span>
                    <span className="vibe-task-agent">{t.agent_id ?? t.id ?? `task ${i + 1}`}</span>
                    <span className="vibe-task-text">{t.error ? t.error : t.summary}</span>
                  </li>
                ))}
              </ul>
            ) : null}
            {run.final_report ? (
              <div className="vibe-report-wrap">
                <div className="vibe-report-title">
                  Final report{run.report_is_partial ? ' (partial)' : ''}
                  {run.is_stale ? <span className="vibe-req"> · stale snapshot</span> : null}
                </div>
                <pre className="vibe-report">{run.final_report}</pre>
              </div>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  )
}

// ── tab 2: FX backtest ──────────────────────────────────────────────────────

function BacktestTab({ reachable }: { reachable: boolean }) {
  const yearAgo = useMemo(() => {
    const d = new Date()
    d.setFullYear(d.getFullYear() - 1)
    return isoDate(d)
  }, [])
  const [codes, setCodes] = useState('EUR/USD')
  const [start, setStart] = useState(yearAgo)
  const [end, setEnd] = useState(isoDate(new Date()))
  const [interval, setIntervalPref] = useState<string>('1D')
  const [source, setSource] = useState('auto')
  const [kind, setKind] = useState<StrategyKind>('sma_cross')
  const [params, setParams] = useState<Record<string, string>>({})
  const [code, setCode] = useState('')
  const [initialCash, setInitialCash] = useState('100000')
  const [leverage, setLeverage] = useState('1')
  const [spread, setSpread] = useState('')
  const [slippage, setSlippage] = useState('0.3')
  const [swap, setSwap] = useState(true)
  const [job, setJob] = useState<BacktestJob | null>(null)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [rawOpen, setRawOpen] = useState(false)

  // Poll the job while it runs (~3s).
  useEffect(() => {
    if (!job || job.status !== 'running') return
    const timer = setTimeout(() => {
      vibeApi.backtestJob(job.id).then(setJob, (err: unknown) =>
        setJob({ ...job, status: 'failed', error: errMessage(err) }),
      )
    }, JOB_POLL_MS)
    return () => clearTimeout(timer)
  }, [job])

  const submit = () => {
    setSubmitError(null)
    const list = codes.split(/[,\s]+/).map((c) => c.trim().toUpperCase()).filter(Boolean)
    if (list.length === 0) {
      setSubmitError('Enter at least one instrument code, e.g. EUR/USD.')
      return
    }
    if (start >= end) {
      setSubmitError('End date must be after the start date.')
      return
    }
    if (kind === 'custom' && !code.trim()) {
      setSubmitError('Custom strategy needs Python source defining class SignalEngine.')
      return
    }
    const body: BacktestRequest = {
      codes: list,
      start_date: start,
      end_date: end,
      interval,
      source: source.trim() || 'auto',
      strategy: {
        kind,
        params: Object.fromEntries(
          Object.entries(params)
            .map(([k, v]) => [k, num(v)])
            .filter((e): e is [string, number] => e[1] !== undefined),
        ),
        ...(kind === 'custom' ? { code } : {}),
      },
      initial_cash: num(initialCash) ?? 100_000,
      leverage: num(leverage) ?? 1,
      ...(spread.trim() !== '' ? { spread_pips: num(spread) } : {}),
      slippage_pips: num(slippage) ?? 0.3,
      swap_enabled: swap,
    }
    setSubmitting(true)
    vibeApi
      .startBacktest(body)
      .then(setJob)
      .catch((err: unknown) => setSubmitError(errMessage(err)))
      .finally(() => setSubmitting(false))
  }

  const busy = submitting || job?.status === 'running'
  const metrics = job?.result?.metrics
  const metricRows = useMemo<[string, number | string][]>(
    () => (metrics ? Object.entries(metrics).filter((e): e is [string, number | string] => typeof e[1] === 'number' || typeof e[1] === 'string') : []),
    [metrics],
  )
  const points = useMemo(() => equityPoints(job?.result?.equity_preview ?? []), [job])
  const chartable = points.length >= 2
  const leverageWarn = (num(leverage) ?? 1) > 5

  const selectSize: UiSelectSize = 'sm'

  return (
    <div className="vibe-panel">
      <div className="vibe-panel-head">
        <h2>FX backtest</h2>
        <p>Runs on the Vibe-Trading sidecar: SMA cross, RSI reversion, Donchian breakout or your own SignalEngine code.</p>
      </div>
      <div className="vibe-panel-body">
        <div className="vibe-form">
          <div className="vibe-row">
            <div className="vibe-field">
              <label className="vibe-label" htmlFor="vibe-bt-codes">Pairs (comma separated)</label>
              <input id="vibe-bt-codes" value={codes} onChange={(e) => setCodes(e.target.value)} placeholder="EUR/USD, USD/JPY, XAU/USD" />
            </div>
            <div className="vibe-field">
              <label className="vibe-label" htmlFor="vibe-bt-start">Start date</label>
              <input id="vibe-bt-start" type="date" value={start} onChange={(e) => setStart(e.target.value)} />
            </div>
            <div className="vibe-field">
              <label className="vibe-label" htmlFor="vibe-bt-end">End date</label>
              <input id="vibe-bt-end" type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
            </div>
            <div className="vibe-field">
              <label className="vibe-label">Interval</label>
              <UiSelect size={selectSize} value={interval} ariaLabel="Interval"
                options={BT_INTERVALS.map((i) => ({ value: i, label: i }))} onChange={setIntervalPref} />
            </div>
          </div>
          <div className="vibe-row">
            <div className="vibe-field">
              <label className="vibe-label">Strategy</label>
              <UiSelect size={selectSize} value={kind} ariaLabel="Strategy kind"
                options={STRATEGY_KINDS} onChange={(v) => setKind(v as StrategyKind)} />
            </div>
            {kind !== 'custom'
              ? STRATEGY_PARAMS[kind].map((p) => (
                  <div className="vibe-field" key={p.name}>
                    <label className="vibe-label" htmlFor={`vibe-bt-p-${p.name}`}>{p.label}</label>
                    <input
                      id={`vibe-bt-p-${p.name}`}
                      type="number"
                      value={params[p.name] ?? String(p.def)}
                      onChange={(e) => setParams((prev) => ({ ...prev, [p.name]: e.target.value }))}
                    />
                  </div>
                ))
              : null}
            <div className="vibe-field">
              <label className="vibe-label" htmlFor="vibe-bt-cash">Initial cash</label>
              <input id="vibe-bt-cash" type="number" value={initialCash} onChange={(e) => setInitialCash(e.target.value)} />
            </div>
            <div className="vibe-field">
              <label className="vibe-label" htmlFor="vibe-bt-lev">Leverage</label>
              <input id="vibe-bt-lev" type="number" value={leverage} onChange={(e) => setLeverage(e.target.value)} />
              <p className="vibe-hint">Notional = signal × equity × leverage.</p>
              {leverageWarn ? (
                <p className="vibe-warn-note" role="alert">Leverage above 5 can wipe the account in one bad bar.</p>
              ) : null}
            </div>
            <div className="vibe-field">
              <label className="vibe-label" htmlFor="vibe-bt-spread">Spread (pips)</label>
              <input id="vibe-bt-spread" type="number" value={spread} placeholder="pair default" onChange={(e) => setSpread(e.target.value)} />
            </div>
            <div className="vibe-field">
              <label className="vibe-label" htmlFor="vibe-bt-slip">Slippage (pips)</label>
              <input id="vibe-bt-slip" type="number" value={slippage} onChange={(e) => setSlippage(e.target.value)} />
            </div>
            <div className="vibe-field">
              <label className="vibe-label" htmlFor="vibe-bt-swap">Swap</label>
              <label className="vibe-check">
                <input id="vibe-bt-swap" type="checkbox" checked={swap} onChange={(e) => setSwap(e.target.checked)} /> enabled
              </label>
            </div>
          </div>
          {kind === 'custom' ? (
            <div className="vibe-field">
              <label className="vibe-label" htmlFor="vibe-bt-code">SignalEngine source (Python)</label>
              <textarea id="vibe-bt-code" rows={6} value={code} spellCheck={false}
                placeholder={'class SignalEngine:\n    def generate(self, data_map):\n        # return {code: series in [-1, 1]}\n'} onChange={(e) => setCode(e.target.value)} />
              <p className="vibe-hint">The server rejects custom code unless VIBE_ALLOW_CUSTOM_CODE=true is set there; the error detail will say so.</p>
            </div>
          ) : null}
          <div className="vibe-field">
            <label className="vibe-label" htmlFor="vibe-bt-source">Data source</label>
            <input id="vibe-bt-source" value={source} onChange={(e) => setSource(e.target.value)} placeholder="auto" />
          </div>
          <div className="vibe-actions">
            <UiButton tone="primary" size="sm" onClick={submit} disabled={busy}>
              {busy ? <Spinner label="Backtesting…" /> : 'Run backtest'}
            </UiButton>
            {submitError ? <span className="vibe-error" role="alert">{submitError}</span> : null}
          </div>
        </div>

        {job ? (
          <div className="vibe-run">
            <div className="vibe-run-head">
              <span className={`vibe-status s-${job.status}`}>{job.status}</span>
              <span className="dim">{job.id}</span>
              {job.status === 'running' ? <Spinner /> : null}
            </div>
            {job.error ? <ErrorNote error={new Error(job.error)} /> : null}
            {metricRows.length > 0 ? (
              <table className="vibe-metrics">
                <tbody>
                  {metricRows.map(([k, v]) => (
                    <tr key={k}>
                      <th>{k}</th>
                      <td>{fmtMetric(k, v)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : job.status === 'completed' ? <p className="vibe-hint">The job completed but returned no metrics.</p> : null}
            {chartable ? <VibeChart points={points} /> : null}
            {job.result ? (
              <div className="vibe-collapsible">
                <UiButton tone="ghost" size="xs" onClick={() => setRawOpen((o) => !o)} aria-expanded={rawOpen}>
                  {rawOpen ? 'Hide raw JSON' : 'Show raw JSON'}
                </UiButton>
                {rawOpen ? <pre className="vibe-report">{JSON.stringify(job.result.raw, null, 2)}</pre> : null}
              </div>
            ) : null}
          </div>
        ) : null}
        {!reachable ? <p className="vibe-hint">The sidecar must be online to run a backtest.</p> : null}
      </div>
    </div>
  )
}

// ── tab 3: macro ────────────────────────────────────────────────────────────

function MacroTab({ reachable }: { reachable: boolean }) {
  const [series, setSeries] = useState(MACRO_DEFAULT_SERIES)
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [loaded, setLoaded] = useState<MacroSeries | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<Error | null>(null)

  const fetchSeries = useCallback(
    (signal?: AbortSignal) => {
      const id = series.trim()
      if (!id) return
      setLoading(true)
      setError(null)
      vibeApi
        .macro(id, { start: start || undefined, end: end || undefined }, signal)
        .then((r) => setLoaded(r), (err: unknown) => {
          if (signal?.aborted) return
          setError(err instanceof Error ? err : new Error(errMessage(err)))
          setLoaded(null)
        })
        .finally(() => setLoading(false))
    },
    [series, start, end],
  )

  useEffect(() => {
    if (!reachable) return
    const ctrl = new AbortController()
    const timer = setTimeout(() => fetchSeries(ctrl.signal), 0)
    return () => {
      clearTimeout(timer)
      ctrl.abort()
    }
    // auto-load only the initial series; further loads are user-triggered
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reachable])

  const points = useMemo(
    () =>
      (loaded?.observations ?? [])
        .map((o) => {
          const v = o.value === null ? NaN : typeof o.value === 'number' ? o.value : Number(o.value)
          return { time: /^\d{4}-\d{2}-\d{2}/.test(o.date) ? o.date.slice(0, 10) : Math.floor(Date.parse(o.date) / 1000), value: v }
        })
        .filter((p) => Number.isFinite(p.value)),
    [loaded],
  )

  return (
    <div className="vibe-panel">
      <div className="vibe-panel-head">
        <h2>Macro</h2>
        <p>FRED series via the sidecar (needs a FRED key there). Quick picks below are classic rates / inflation / jobs series.</p>
      </div>
      <div className="vibe-panel-body">
        <div className="vibe-form">
          <div className="vibe-row">
            <div className="vibe-field">
              <label className="vibe-label" htmlFor="vibe-macro-id">Series ID</label>
              <input id="vibe-macro-id" value={series} onChange={(e) => setSeries(e.target.value)} placeholder="DFF" />
            </div>
            <div className="vibe-field">
              <label className="vibe-label" htmlFor="vibe-macro-start">From</label>
              <input id="vibe-macro-start" type="date" value={start} onChange={(e) => setStart(e.target.value)} />
            </div>
            <div className="vibe-field">
              <label className="vibe-label" htmlFor="vibe-macro-end">To</label>
              <input id="vibe-macro-end" type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
            </div>
            <div className="vibe-actions">
              <UiButton tone="primary" size="sm" onClick={() => fetchSeries()} disabled={loading || !series.trim()}>
                {loading ? <Spinner label="Loading…" /> : 'Load series'}
              </UiButton>
            </div>
          </div>
          <div className="vibe-quick">
            {MACRO_QUICK.map((s) => (
              <button key={s} type="button" className={`vibe-chip${series === s ? ' on' : ''}`} onClick={() => setSeries(s)}>
                {s}
              </button>
            ))}
          </div>
        </div>
        {error ? <ErrorNote error={error} onRetry={() => fetchSeries()} /> : null}
        {loaded ? (
          <div className="vibe-run">
            <div className="vibe-run-head">
              <b>{loaded.series_id}</b>
              <span className="dim">{loaded.count} observations · source {loaded.source}</span>
              {loaded.truncated ? <span className="vibe-req"> truncated (raise the limit)</span> : null}
            </div>
            {points.length >= 2 ? <VibeChart points={points} /> : <p className="vibe-hint">No numeric observations in this range.</p>}
          </div>
        ) : null}
        {!reachable ? <p className="vibe-hint">The sidecar must be online to load macro data.</p> : null}
      </div>
    </div>
  )
}

// ── page ────────────────────────────────────────────────────────────────────

export function VibeLabPage() {
  const [tab, setTab] = useState<Tab>('swarm')
  const statusQ = useFetch('vibe:status', (s) => vibeApi.status(s), 30_000)
  const status = statusQ.data
  const reachable = Boolean(status?.enabled && status?.reachable)

  return (
    <main className="vibe-page">
      <div className="vibe-wrap">
        <StatusBanner status={status} loading={statusQ.loading} error={statusQ.error} onRetry={statusQ.reload} />
        <div className="vibe-tabs" role="tablist" aria-label="Vibe Lab sections">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              role="tab"
              aria-selected={tab === t.id}
              className={tab === t.id ? 'active' : ''}
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </div>
        {tab === 'swarm' ? <SwarmTab reachable={reachable} /> : null}
        {tab === 'backtest' ? <BacktestTab reachable={reachable} /> : null}
        {tab === 'macro' ? <MacroTab reachable={reachable} /> : null}
      </div>
    </main>
  )
}
