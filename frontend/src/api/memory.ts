import { request } from './client'

export type MemoryDirection = 'up' | 'down' | 'flat'
export type MemoryNodeLabel = 'Symbol' | 'MarketEvent' | 'Indicator'

export interface MemorySymbolStat {
  symbol: string
  group_id: string
  reactions: number
  up: number
  down: number
  flat: number
  first: string | null
  last: string | null
}

export interface MemoryOverview {
  symbols: MemorySymbolStat[]
  nodes: number
  edges: number
  /** Finished backtest outcomes not yet turned into facts. */
  pending: number
}

export interface MemoryNode {
  id: string
  label: MemoryNodeLabel
  name: string
  summary: string
  attributes: Record<string, string | number | boolean | null>
}

export interface MemoryEdge {
  id: string
  type: 'MARKET_REACTION' | 'INSTANCE_OF'
  source: string
  target: string
  fact: string
  valid_at: string
  attributes: Record<string, string | number | boolean | null>
}

export interface MemoryGraph {
  symbol: string
  total: number
  truncated: boolean
  nodes: MemoryNode[]
  edges: MemoryEdge[]
}

export interface MemoryGraphQuery {
  symbol: string
  limit?: number
  q?: string
  from?: string
  to?: string
  kind?: 'calendar' | 'headline'
  direction?: MemoryDirection
}

export interface MemorySyncResult {
  facts: number
  edges: number
  graph_edges: number
  skipped_bias: number
  graph_error: string | null
  errors: string[]
}

export const memoryApi = {
  overview: (signal?: AbortSignal) => request<MemoryOverview>('GET', '/memory/overview', { signal }),
  graph: (q: MemoryGraphQuery, signal?: AbortSignal) =>
    request<MemoryGraph>('GET', '/memory/graph', { params: { ...q }, signal }),
  sync: (pushGraph: boolean, signal?: AbortSignal) =>
    request<MemorySyncResult>('POST', '/memory/sync', { body: { push_graph: pushGraph }, signal }),
}
