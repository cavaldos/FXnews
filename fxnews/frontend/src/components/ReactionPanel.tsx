import { useEffect } from 'react'
import { useNowSlow } from '../hooks/useClock'
import { useSymbolChart } from '../context/symbolChart'
import { fmtDateTime } from '../lib/format'
import { ensureReactions, fmtPct, HORIZONS, horizonOf, useReactionLookup, refreshReaction } from '../lib/reaction'
import { ReactionLabel } from './ReactionLabel'
import { UiButton } from './UI/UiButton'
import { Spinner } from './widgets'

/** Reaction of each symbol to an event at `timeMs`: horizons x symbols, first-hour excursions, chart buttons. */
export function ReactionPanel({ timeMs, symbols }: { timeMs: number; symbols: string[] }) {
  const lookup = useReactionLookup()
  const now = useNowSlow()
  const chart = useSymbolChart()
  const key = symbols.join(',')
  useEffect(() => {
    ensureReactions(key.split(',').filter(Boolean).map((symbol) => ({ symbol, timeMs })))
  }, [key, timeMs])

  const future = timeMs > now
  const entries = symbols.map((s) => lookup(s, timeMs))
  const loading = entries.some((e) => !e || (e.state === 'loading' && !e.data))

  if (!symbols.length) return <div className="rx-panel">No related symbols for this event.</div>

  return (
    <div className="rx-panel">
      <div className="rx-when">
        Event time {fmtDateTime(timeMs)} (local) · price = close of the last finished candle before the event
      </div>
      {future ? <div className="rx-note">This event has not happened yet: reaction is pending.</div> : null}
      <table className="rx-table">
        <thead>
          <tr>
            <th />
            {symbols.map((s) => (
              <th key={s}>{s}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {HORIZONS.map((h) => (
            <tr key={h}>
              <th scope="row">+{h}</th>
              {entries.map((e, i) => (
                <td key={symbols[i]}>
                  {e?.data ? (
                    e.data.status === 'ok' || e.data.status === 'pending' ? (
                      <ReactionLabel h={horizonOf(e.data, h)} />
                    ) : (
                      <span className="rx-label dim">—</span>
                    )
                  ) : e?.state === 'error' ? (
                    <span className="rx-label dim">error</span>
                  ) : (
                    <span className="rx-label dim">…</span>
                  )}
                </td>
              ))}
            </tr>
          ))}
          <tr className="sep">
            <th scope="row">Max up (1h)</th>
            {entries.map((e, i) => (
              <td key={symbols[i]}>
                <span className="rx-label up">{e?.data?.max_up_1h != null ? fmtPct(e.data.max_up_1h) : '—'}</span>
              </td>
            ))}
          </tr>
          <tr>
            <th scope="row">Max down (1h)</th>
            {entries.map((e, i) => (
              <td key={symbols[i]}>
                <span className="rx-label down">{e?.data?.max_down_1h != null ? fmtPct(e.data.max_down_1h) : '—'}</span>
              </td>
            ))}
          </tr>
          <tr>
            <th scope="row">Price at release</th>
            {entries.map((e, i) => (
              <td key={symbols[i]}>{e?.data?.price_at_release ?? '—'}</td>
            ))}
          </tr>
          <tr>
            <th scope="row">Data</th>
            {entries.map((e, i) => (
              <td key={symbols[i]} className="rx-note" title={e?.data?.message ?? e?.error}>
                {e?.data
                  ? e.data.status === 'ok'
                    ? `${e.data.interval} · ${e.data.source}`
                    : (e.data.message ?? e.data.status)
                  : (e?.error ?? '')}
              </td>
            ))}
          </tr>
          <tr className="sep">
            <th scope="row" />
            {symbols.map((s) => (
              <td key={s}>
                <UiButton
                  tone="ghost"
                  size="xs"
                  disabled={!chart.isKnown(s)}
                  onClick={() => chart.open(s, { time: timeMs })}
                >
                  Open chart
                </UiButton>
              </td>
            ))}
          </tr>
        </tbody>
      </table>
      <div className="detail-actions">
        {loading ? <Spinner label="Loading…" /> : null}
        <UiButton
          tone="ghost"
          size="xs"
          onClick={() => symbols.forEach((s) => refreshReaction(s, timeMs))}
          disabled={future}
        >
          Refresh
        </UiButton>
      </div>
    </div>
  )
}
