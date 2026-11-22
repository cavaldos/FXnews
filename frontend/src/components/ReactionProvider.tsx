import { useCallback, useMemo, useState, type ReactNode } from 'react'
import { api } from '../api/client'
import type { SymbolOut } from '../api/types'
import { ReactionPanelContext, type ReactionPanelApi, type ReactionPanelRequest } from '../context/reaction'
import { useFetch } from '../hooks/useFetch'
import { Modal } from './Modal'
import { ReactionPanel } from './ReactionPanel'
import { UiButton } from './UI/UiButton'

const pad = (n: number) => String(n).padStart(2, '0')

/** Value for <input type="datetime-local"> in the viewer's zone. */
function toLocalInput(ms: number): string {
  const d = new Date(ms)
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function CheckerBody({ symbols }: { symbols: SymbolOut[] }) {
  const [symbol, setSymbol] = useState(symbols.find((s) => s.symbol === 'XAUUSD')?.symbol ?? symbols[0]?.symbol ?? '')
  const [when, setWhen] = useState(() => toLocalInput(Date.now() - 24 * 3600_000))
  const [target, setTarget] = useState<{ symbol: string; timeMs: number } | null>(null)
  const ms = new Date(when).getTime()
  const valid = Boolean(symbol) && !Number.isNaN(ms)

  return (
    <>
      <form
        className="rx-form"
        onSubmit={(e) => {
          e.preventDefault()
          if (valid) setTarget({ symbol, timeMs: ms })
        }}
      >
        <label>
          Symbol
          <select value={symbol} onChange={(e) => setSymbol(e.target.value)}>
            {symbols.map((s) => (
              <option key={s.symbol} value={s.symbol}>
                {s.symbol}
              </option>
            ))}
          </select>
        </label>
        <label>
          Date and time (local)
          <input type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} />
        </label>
        <UiButton type="submit" size="sm" disabled={!valid}>
          Check
        </UiButton>
      </form>
      {target ? <ReactionPanel timeMs={target.timeMs} symbols={[target.symbol]} /> : null}
    </>
  )
}

/** Hosts the "Check reaction" modals (event panel + manual tool) so any component can open them. */
export function ReactionProvider({ children }: { children: ReactNode }) {
  const [req, setReq] = useState<ReactionPanelRequest | null>(null)
  const [checker, setChecker] = useState(false)
  const symbolsQ = useFetch<SymbolOut[]>(checker ? 'symbols' : null, (signal) => api.symbols(signal))

  const open = useCallback((r: ReactionPanelRequest) => setReq(r), [])
  const openChecker = useCallback(() => setChecker(true), [])
  const api2 = useMemo<ReactionPanelApi>(() => ({ open, openChecker }), [open, openChecker])

  return (
    <ReactionPanelContext.Provider value={api2}>
      {children}
      {req ? (
        <Modal onClose={() => setReq(null)} title={req.title ?? 'Check reaction'}>
          <ReactionPanel timeMs={req.time} symbols={req.symbols} />
        </Modal>
      ) : null}
      {checker ? (
        <Modal onClose={() => setChecker(false)} title="Check reaction">
          {symbolsQ.data ? (
            <CheckerBody symbols={symbolsQ.data} />
          ) : (
            <div className="rx-panel">{symbolsQ.error ? symbolsQ.error.message : 'Loading symbols…'}</div>
          )}
        </Modal>
      ) : null}
    </ReactionPanelContext.Provider>
  )
}
