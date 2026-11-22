import { useMemo, useState } from 'react'
import { memoryApi, type MemoryDirection, type MemoryEdge, type MemoryGraph as Graph, type MemoryNode, type MemorySyncResult } from '../api/memory'
import { MemoryGraph, type Horizon } from '../components/memory/MemoryGraph'
import { UiButton } from '../components/UI/UiButton'
import { UiSelect } from '../components/UI/UiSelect'
import { EmptyState, ErrorNote, Spinner } from '../components/widgets'
import { useFetch } from '../hooks/useFetch'
import { useStoredString } from '../hooks/useLocalStorage'
import { fmtDateTime, fmtPct, toMs } from '../lib/format'
import '../style/memory.css'

const HORIZONS: Horizon[] = ['15m', '1h', '4h', '1d']
const LIMITS = ['50', '150', '300', '600']
const fmtN = (n: number) => n.toLocaleString('en-US')

function errMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

function when(iso: string | null | undefined): string {
  const ms = toMs(iso)
  return ms === null ? '—' : fmtDateTime(ms)
}

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

function Returns({ edge }: { edge: MemoryEdge }) {
  return (
    <table className="mem-returns">
      <thead>
        <tr>
          {['5m', ...HORIZONS].map((h) => (
            <th key={h}>{h}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        <tr>
          {['5m', ...HORIZONS].map((h) => {
            const r = num(edge.attributes[`ret_${h}`])
            return (
              <td key={h} className={`mem-dir-text ${String(edge.attributes[`dir_${h}`] ?? 'none')}`}>
                {r === null ? '—' : fmtPct(r)}
              </td>
            )
          })}
        </tr>
      </tbody>
    </table>
  )
}

function Details({ graph, id, horizon, onSelect }: { graph: Graph; id: string | null; horizon: Horizon; onSelect: (id: string) => void }) {
  const byId = useMemo(() => new Map(graph.nodes.map((n) => [n.id, n])), [graph])
  if (!id) {
    return (
      <p className="mem-hint">
        Click a node. Circles are calendar releases, diamonds are headlines; colour = direction at the selected horizon, size =
        size of the move. Blue nodes group repeated releases of the same indicator.
      </p>
    )
  }
  const node = byId.get(id)
  if (!node) return null
  const reactions = graph.edges.filter((e) => e.type === 'MARKET_REACTION')
  const reactionOf = (eventId: string) => reactions.find((e) => e.source === eventId)

  if (node.label === 'MarketEvent') {
    const re = reactionOf(node.id)
    const a = node.attributes
    return (
      <div className="mem-detail">
        <div className="mem-detail-kind">{String(a.kind ?? 'event')} · {String(a.impact ?? '')} impact · {String(a.source ?? '')}</div>
        <h3>{String(a.title ?? node.name)}</h3>
        <div className="mem-detail-time">{when(String(a.time ?? ''))}</div>
        {a.actual != null || a.forecast != null ? (
          <dl className="mem-kv">
            <dt>Actual</dt>
            <dd>{String(a.actual ?? '—')}</dd>
            <dt>Forecast</dt>
            <dd>{String(a.forecast ?? '—')}</dd>
            <dt>Previous</dt>
            <dd>{String(a.previous ?? '—')}</dd>
            <dt>Surprise</dt>
            <dd>{num(a.surprise) === null ? '—' : String(a.surprise)}</dd>
          </dl>
        ) : null}
        {re ? (
          <>
            <div className="mem-section-title">Observed move ({String(re.attributes.symbol ?? graph.symbol)})</div>
            <Returns edge={re} />
            <p className="mem-fact">{re.fact}</p>
          </>
        ) : null}
        {typeof a.url === 'string' && a.url ? (
          <a className="mem-link" href={a.url} target="_blank" rel="noreferrer noopener">
            Source ↗
          </a>
        ) : null}
      </div>
    )
  }

  if (node.label === 'Indicator') {
    const events = graph.edges
      .filter((e) => e.type === 'INSTANCE_OF' && e.target === node.id)
      .map((e) => byId.get(e.source))
      .filter((n): n is MemoryNode => Boolean(n))
      .sort((x, y) => String(y.attributes.time).localeCompare(String(x.attributes.time)))
    const counts = { up: 0, down: 0, flat: 0 }
    for (const ev of events) {
      const d = reactionOf(ev.id)?.attributes[`dir_${horizon}`]
      if (d === 'up' || d === 'down' || d === 'flat') counts[d]++
    }
    return (
      <div className="mem-detail">
        <div className="mem-detail-kind">Indicator · {String(node.attributes.country ?? node.attributes.currency ?? '')}</div>
        <h3>{node.name}</h3>
        <div className="mem-counts">
          {events.length} releases in view · {horizon}: <span className="mem-dir-text up">{counts.up} up</span> ·{' '}
          <span className="mem-dir-text down">{counts.down} down</span> · <span className="mem-dir-text flat">{counts.flat} flat</span>
        </div>
        <table className="mem-list">
          <thead>
            <tr>
              <th>Time</th>
              <th>Actual</th>
              <th>Fcst</th>
              <th>{horizon}</th>
            </tr>
          </thead>
          <tbody>
            {events.map((ev) => {
              const re = reactionOf(ev.id)
              const r = num(re?.attributes[`ret_${horizon}`])
              return (
                <tr key={ev.id} onClick={() => onSelect(ev.id)}>
                  <td>{when(String(ev.attributes.time ?? ''))}</td>
                  <td>{String(ev.attributes.actual ?? '—')}</td>
                  <td>{String(ev.attributes.forecast ?? '—')}</td>
                  <td className={`mem-dir-text ${String(re?.attributes[`dir_${horizon}`] ?? 'none')}`}>{r === null ? '—' : fmtPct(r)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    )
  }

  const counts = { up: 0, down: 0, flat: 0 }
  for (const e of reactions) {
    const d = e.attributes[`dir_${horizon}`]
    if (d === 'up' || d === 'down' || d === 'flat') counts[d]++
  }
  return (
    <div className="mem-detail">
      <div className="mem-detail-kind">Symbol</div>
      <h3>{node.name}</h3>
      <div className="mem-counts">
        {reactions.length} reactions in view · {horizon}: <span className="mem-dir-text up">{counts.up} up</span> ·{' '}
        <span className="mem-dir-text down">{counts.down} down</span> · <span className="mem-dir-text flat">{counts.flat} flat</span>
      </div>
    </div>
  )
}

export function MemoryPage() {
  const [symbolPref, setSymbol] = useStoredString('fxnews.memory.symbol', '')
  const [horizonPref, setHorizon] = useStoredString('fxnews.memory.horizon', '1h')
  const [limit, setLimit] = useStoredString('fxnews.memory.limit', '150')
  const [kind, setKind] = useState('')
  const [direction, setDirection] = useState('')
  const [qInput, setQInput] = useState('')
  const [q, setQ] = useState('')
  const [selected, setSelected] = useState<string | null>(null)
  const [syncing, setSyncing] = useState(false)
  const [syncResult, setSyncResult] = useState<MemorySyncResult | null>(null)
  const [syncError, setSyncError] = useState<string | null>(null)
  const [pushGraph, setPushGraph] = useState(false)

  const horizon: Horizon = HORIZONS.includes(horizonPref as Horizon) ? (horizonPref as Horizon) : '1h'
  const overviewQ = useFetch('memory:overview', (s) => memoryApi.overview(s))
  const symbols = overviewQ.data?.symbols ?? []
  const symbol = symbols.some((s) => s.symbol === symbolPref) ? symbolPref : (symbols[0]?.symbol ?? '')

  const graphKey = symbol ? `memory:graph:${symbol}:${limit}:${kind}:${direction}:${q}` : null
  const graphQ = useFetch(graphKey, (s) =>
    memoryApi.graph(
      {
        symbol,
        limit: Number(limit) || 150,
        q: q || undefined,
        kind: (kind || undefined) as 'calendar' | 'headline' | undefined,
        direction: (direction || undefined) as MemoryDirection | undefined,
      },
      s,
    ),
  )

  const runSync = async () => {
    setSyncing(true)
    setSyncError(null)
    setSyncResult(null)
    try {
      setSyncResult(await memoryApi.sync(pushGraph))
      overviewQ.reload()
      graphQ.reload()
    } catch (err) {
      setSyncError(errMessage(err))
    } finally {
      setSyncing(false)
    }
  }

  const ov = overviewQ.data
  return (
    <div className="mem-page">
      <aside className="mem-side">
        <section className="mem-panel">
          <h2>Memory</h2>
          <p className="mem-hint">
            Observed facts only: each release and the measured price move after it. Model predictions and biases are never
            stored. Import / export the <code>memory_*</code> tables on the Data page.
          </p>
          {ov ? (
            <div className="mem-totals">
              <span>{fmtN(ov.nodes)} nodes</span>
              <span>{fmtN(ov.edges)} facts</span>
              <span className={ov.pending ? 'mem-pending' : ''}>{fmtN(ov.pending)} pending</span>
            </div>
          ) : null}
          <label className="mem-check">
            <input type="checkbox" checked={pushGraph} onChange={(e) => setPushGraph(e.target.checked)} disabled={syncing} />
            Also push to Graphiti (Neo4j must be running)
          </label>
          <UiButton size="sm" onClick={runSync} disabled={syncing}>
            {syncing ? 'Syncing…' : 'Sync outcomes'}
          </UiButton>
          {syncing ? <Spinner label="Building facts (a full first sync can take a minute)" /> : null}
          {syncResult ? (
            <div className="mem-sync-result">
              +{fmtN(syncResult.facts)} outcomes → {fmtN(syncResult.edges)} facts
              {syncResult.graph_edges ? ` · ${fmtN(syncResult.graph_edges)} to Graphiti` : ''}
              {syncResult.skipped_bias ? ` · ${syncResult.skipped_bias} rejected` : ''}
              {syncResult.graph_error ? <div className="mem-error">Graphiti: {syncResult.graph_error}</div> : null}
            </div>
          ) : null}
          {syncError ? <div className="mem-error">{syncError}</div> : null}
        </section>

        <section className="mem-panel mem-symbols">
          <h2>Symbols</h2>
          {overviewQ.error && !ov ? <ErrorNote error={overviewQ.error} onRetry={overviewQ.reload} /> : null}
          {overviewQ.loading && !ov ? <Spinner /> : null}
          {ov && symbols.length === 0 ? <EmptyState>No facts yet. Run a sync.</EmptyState> : null}
          <ul>
            {symbols.map((s) => (
              <li key={s.symbol}>
                <button
                  type="button"
                  className={s.symbol === symbol ? 'active' : ''}
                  onClick={() => {
                    setSymbol(s.symbol)
                    setSelected(null)
                  }}
                >
                  <span className="mem-sym">{s.symbol}</span>
                  <span className="mem-sym-n">{fmtN(s.reactions)}</span>
                  <span className="mem-bar" aria-hidden>
                    <span className="up" style={{ flex: s.up }} />
                    <span className="flat" style={{ flex: s.flat }} />
                    <span className="down" style={{ flex: s.down }} />
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      </aside>

      <section className="mem-main">
        <div className="mem-toolbar">
          <form
            className="mem-search"
            onSubmit={(e) => {
              e.preventDefault()
              setQ(qInput.trim())
              setSelected(null)
            }}
          >
            <input value={qInput} onChange={(e) => setQInput(e.target.value)} placeholder="Filter facts, e.g. Payrolls" aria-label="Filter facts" />
          </form>
          <UiSelect
            size="sm"
            ariaLabel="Event kind"
            value={kind}
            onChange={setKind}
            options={[
              { value: '', label: 'All events' },
              { value: 'calendar', label: 'Calendar' },
              { value: 'headline', label: 'Headlines' },
            ]}
          />
          <UiSelect
            size="sm"
            ariaLabel="Direction"
            value={direction}
            onChange={setDirection}
            options={[
              { value: '', label: 'Any direction' },
              { value: 'up', label: 'Up' },
              { value: 'down', label: 'Down' },
              { value: 'flat', label: 'Flat' },
            ]}
          />
          <UiSelect size="sm" ariaLabel="Colour by horizon" value={horizon} onChange={setHorizon} options={HORIZONS.map((h) => ({ value: h, label: `Colour: ${h}` }))} />
          <UiSelect size="sm" ariaLabel="Events shown" value={limit} onChange={setLimit} options={LIMITS.map((l) => ({ value: l, label: `Last ${l}` }))} />
          {graphQ.data ? (
            <span className="mem-count">
              {fmtN(graphQ.data.edges.filter((e) => e.type === 'MARKET_REACTION').length)} of {fmtN(graphQ.data.total)}
            </span>
          ) : null}
        </div>
        <div className="mem-canvas">
          {graphQ.error && !graphQ.data ? <ErrorNote error={graphQ.error} onRetry={graphQ.reload} /> : null}
          {graphQ.loading && !graphQ.data && symbol ? <Spinner label="Loading graph" /> : null}
          {graphQ.data && graphQ.data.nodes.length === 0 ? <EmptyState>No facts match.</EmptyState> : null}
          {graphQ.data && graphQ.data.nodes.length > 0 ? (
            <MemoryGraph graph={graphQ.data} horizon={horizon} selectedId={selected} onSelect={setSelected} />
          ) : null}
          <div className="mem-legend" aria-hidden>
            <span><i className="sym" /> Symbol</span>
            <span><i className="ind" /> Indicator</span>
            <span><i className="up" /> Up</span>
            <span><i className="down" /> Down</span>
            <span><i className="flat" /> Flat</span>
            <span><i className="diamond" /> Headline</span>
            <span className="mem-legend-hint">Scroll to zoom · drag to pan</span>
          </div>
        </div>
      </section>

      <aside className="mem-panel mem-details">
        <h2>Details</h2>
        {graphQ.data ? <Details graph={graphQ.data} id={selected} horizon={horizon} onSelect={setSelected} /> : null}
      </aside>
    </div>
  )
}
