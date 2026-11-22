import { useCallback, useMemo, useState, type ReactNode } from 'react'
import type { SymbolOut } from '../api/types'
import { SymbolChartContext, type OpenSymbolChartOptions, type SymbolChartApi } from '../context/symbolChart'
import { SymbolChartModal } from './SymbolChartModal'

interface Target {
  symbol: string
  time: number | null
  /** Bumped on every open so re-opening the same symbol resets the modal. */
  n: number
}

/** Global "click any symbol -> chart modal". Only symbols of the backend catalog can be opened. */
export function SymbolChartProvider({ symbols, children }: { symbols: SymbolOut[]; children: ReactNode }) {
  const [target, setTarget] = useState<Target | null>(null)
  const bySymbol = useMemo(() => new Map(symbols.map((s) => [s.symbol, s])), [symbols])

  const open = useCallback(
    (symbol: string, opts?: OpenSymbolChartOptions) => {
      if (!bySymbol.has(symbol)) return
      setTarget((prev) => ({ symbol, time: opts?.time ?? null, n: (prev?.n ?? 0) + 1 }))
    },
    [bySymbol],
  )
  const api = useMemo<SymbolChartApi>(() => ({ open, isKnown: (s) => bySymbol.has(s) }), [open, bySymbol])
  const close = useCallback(() => setTarget(null), [])

  return (
    <SymbolChartContext.Provider value={api}>
      {children}
      {target ? <SymbolChartModal key={target.n} symbol={target.symbol} sym={bySymbol.get(target.symbol)} time={target.time} onClose={close} /> : null}
    </SymbolChartContext.Provider>
  )
}
