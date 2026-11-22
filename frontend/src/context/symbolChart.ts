import { createContext, useContext } from 'react'

export interface OpenSymbolChartOptions {
  /** Event time (epoch ms): the chart scrolls to it and marks it. */
  time?: number | null
}

export interface SymbolChartApi {
  /** Open the chart modal for a symbol of the backend catalog (unknown symbols are ignored). */
  open: (symbol: string, opts?: OpenSymbolChartOptions) => void
  /** True when `symbol` is in the backend symbol catalog (/api/symbols). */
  isKnown: (symbol: string) => boolean
}

export const SymbolChartContext = createContext<SymbolChartApi>({ open: () => {}, isKnown: () => false })

export function useSymbolChart(): SymbolChartApi {
  return useContext(SymbolChartContext)
}
