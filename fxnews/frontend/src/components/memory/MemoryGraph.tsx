import { useMemo, useRef, useState, type PointerEvent as ReactPointerEvent, type WheelEvent } from 'react'
import type { MemoryEdge, MemoryGraph as Graph, MemoryNode } from '../../api/memory'

export type Horizon = '15m' | '1h' | '4h' | '1d'

interface Props {
  graph: Graph
  horizon: Horizon
  selectedId: string | null
  onSelect: (id: string | null) => void
}

interface P {
  x: number
  y: number
  vx: number
  vy: number
  fixed: boolean
}

const W = 1000
const H = 700

/** Small deterministic hash -> [0, 1), so the layout is stable between renders. */
function rand(s: string): number {
  let h = 2166136261
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619)
  return ((h >>> 0) % 10000) / 10000
}

/**
 * Force layout: the symbol is pinned in the middle, each indicator pulls its releases into a cluster,
 * one-off headlines float on an outer ring. O(n²) repulsion is fine for the page's ≤ 600 events.
 */
function layout(nodes: MemoryNode[], edges: MemoryEdge[]): Map<string, P> {
  const pos = new Map<string, P>()
  const indicators = nodes.filter((n) => n.label === 'Indicator')
  const parent = new Map<string, string>()
  for (const e of edges) if (e.type === 'INSTANCE_OF') parent.set(e.source, e.target)

  indicators.forEach((n, i) => {
    const a = (i / Math.max(1, indicators.length)) * Math.PI * 2
    pos.set(n.id, { x: Math.cos(a) * 230, y: Math.sin(a) * 230, vx: 0, vy: 0, fixed: false })
  })
  for (const n of nodes) {
    if (n.label === 'Symbol') pos.set(n.id, { x: 0, y: 0, vx: 0, vy: 0, fixed: true })
    else if (n.label === 'MarketEvent') {
      const p = parent.get(n.id)
      const base = p ? pos.get(p) : undefined
      const a = rand(n.id) * Math.PI * 2
      const r = base ? 25 + rand(n.id + 'r') * 25 : 360 + rand(n.id + 'r') * 60
      pos.set(n.id, { x: (base?.x ?? 0) + Math.cos(a) * r, y: (base?.y ?? 0) + Math.sin(a) * r, vx: 0, vy: 0, fixed: false })
    }
  }

  const list = [...pos.entries()]
  const springs: [string, string, number, number][] = []
  for (const e of edges) {
    if (e.type === 'INSTANCE_OF') springs.push([e.source, e.target, 30, 0.08])
    else if (!parent.has(e.source)) springs.push([e.source, e.target, 380, 0.01])
  }
  const iters = 220
  for (let it = 0; it < iters; it++) {
    const cool = 1 - it / iters
    for (let i = 0; i < list.length; i++) {
      const a = list[i][1]
      for (let j = i + 1; j < list.length; j++) {
        const b = list[j][1]
        let dx = a.x - b.x
        let dy = a.y - b.y
        let d2 = dx * dx + dy * dy
        if (d2 < 0.01) {
          dx = rand(list[i][0]) - 0.5
          dy = rand(list[j][0]) - 0.5
          d2 = 0.25
        }
        if (d2 > 90000) continue
        const f = 260 / d2
        a.vx += dx * f
        a.vy += dy * f
        b.vx -= dx * f
        b.vy -= dy * f
      }
    }
    for (const [s, t, len, k] of springs) {
      const a = pos.get(s)
      const b = pos.get(t)
      if (!a || !b) continue
      const dx = b.x - a.x
      const dy = b.y - a.y
      const d = Math.sqrt(dx * dx + dy * dy) || 1
      const f = (d - len) * k
      const fx = (dx / d) * f
      const fy = (dy / d) * f
      a.vx += fx
      a.vy += fy
      b.vx -= fx
      b.vy -= fy
    }
    for (const [, p] of list) {
      if (p.fixed) {
        p.vx = p.vy = 0
        continue
      }
      p.vx -= p.x * 0.004 // gentle gravity keeps clusters on screen
      p.vy -= p.y * 0.004
      const step = Math.min(12, Math.hypot(p.vx, p.vy)) / (Math.hypot(p.vx, p.vy) || 1)
      p.x += p.vx * step * cool
      p.y += p.vy * step * cool
      p.vx *= 0.5
      p.vy *= 0.5
    }
  }
  return pos
}

function dirOf(e: MemoryEdge | undefined, h: Horizon): string {
  const v = e?.attributes[`dir_${h}`]
  return typeof v === 'string' ? v : 'none'
}

function retOf(e: MemoryEdge | undefined, h: Horizon): number | null {
  const v = e?.attributes[`ret_${h}`]
  return typeof v === 'number' ? v : null
}

export function MemoryGraph({ graph, horizon, selectedId, onSelect }: Props) {
  const positions = useMemo(() => layout(graph.nodes, graph.edges), [graph])
  const reactionOf = useMemo(() => {
    const m = new Map<string, MemoryEdge>()
    for (const e of graph.edges) if (e.type === 'MARKET_REACTION') m.set(e.source, e)
    return m
  }, [graph])
  const neighbours = useMemo(() => {
    const m = new Map<string, Set<string>>()
    for (const e of graph.edges) {
      if (!m.has(e.source)) m.set(e.source, new Set())
      if (!m.has(e.target)) m.set(e.target, new Set())
      m.get(e.source)!.add(e.target)
      m.get(e.target)!.add(e.source)
    }
    return m
  }, [graph])
  const indicatorSize = useMemo(() => {
    const m = new Map<string, number>()
    for (const e of graph.edges) if (e.type === 'INSTANCE_OF') m.set(e.target, (m.get(e.target) ?? 0) + 1)
    return m
  }, [graph])

  // ── view transform (pan / zoom); reset to "fit" whenever a new layout arrives ──
  const fit = useMemo(() => {
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
    for (const p of positions.values()) {
      minX = Math.min(minX, p.x)
      minY = Math.min(minY, p.y)
      maxX = Math.max(maxX, p.x)
      maxY = Math.max(maxY, p.y)
    }
    if (!Number.isFinite(minX)) return { x: 0, y: 0, k: 1 }
    const k = Math.min(W / (maxX - minX + 120), H / (maxY - minY + 120), 2.5)
    return { k, x: W / 2 - ((minX + maxX) / 2) * k, y: H / 2 - ((minY + maxY) / 2) * k }
  }, [positions])
  const [viewState, setViewState] = useState<{ of: Map<string, P>; v: { x: number; y: number; k: number } } | null>(null)
  const view = viewState?.of === positions ? viewState.v : fit
  const setView = (f: (v: { x: number; y: number; k: number }) => { x: number; y: number; k: number }) =>
    setViewState((cur) => ({ of: positions, v: f(cur?.of === positions ? cur.v : fit) }))
  const svgRef = useRef<SVGSVGElement>(null)
  const drag = useRef<{ x: number; y: number; vx: number; vy: number; moved: boolean } | null>(null)

  const toSvg = (clientX: number, clientY: number) => {
    const r = svgRef.current!.getBoundingClientRect()
    const s = Math.min(r.width / W, r.height / H)
    return { x: (clientX - r.left - (r.width - W * s) / 2) / s, y: (clientY - r.top - (r.height - H * s) / 2) / s, s }
  }
  const onWheel = (e: WheelEvent<SVGSVGElement>) => {
    const p = toSvg(e.clientX, e.clientY)
    const factor = Math.exp(-e.deltaY * 0.0015)
    setView((v) => {
      const k = Math.min(12, Math.max(0.15, v.k * factor))
      return { k, x: p.x - ((p.x - v.x) * k) / v.k, y: p.y - ((p.y - v.y) * k) / v.k }
    })
  }
  const onPointerDown = (e: ReactPointerEvent<SVGSVGElement>) => {
    drag.current = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y, moved: false }
  }
  const onPointerMove = (e: ReactPointerEvent<SVGSVGElement>) => {
    const d = drag.current
    if (!d) return
    const s = toSvg(e.clientX, e.clientY).s
    const dx = (e.clientX - d.x) / s
    const dy = (e.clientY - d.y) / s
    if (Math.abs(dx) + Math.abs(dy) > 3) {
      d.moved = true
      e.currentTarget.setPointerCapture(e.pointerId)
    }
    if (d.moved) setView((v) => ({ ...v, x: d.vx + dx, y: d.vy + dy }))
  }
  const onPointerUp = (e: ReactPointerEvent<SVGSVGElement>) => {
    const d = drag.current
    drag.current = null
    if (d && !d.moved) {
      const id = (e.target as Element).closest('[data-id]')?.getAttribute('data-id') ?? null
      onSelect(id)
    }
  }

  const focus = selectedId ? neighbours.get(selectedId) : undefined
  const dim = (id: string) => (selectedId && id !== selectedId && !focus?.has(id) ? ' dim' : '')
  const showLabels = view.k > 0.55

  return (
    <svg
      ref={svgRef}
      className="mem-graph"
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="xMidYMid meet"
      onWheel={onWheel}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerLeave={() => (drag.current = null)}
      role="img"
      aria-label={`Memory graph of ${graph.symbol}`}
    >
      <g transform={`translate(${view.x} ${view.y}) scale(${view.k})`}>
        {graph.edges.map((e) => {
          const a = positions.get(e.source)
          const b = positions.get(e.target)
          if (!a || !b) return null
          const hot = selectedId !== null && (e.source === selectedId || e.target === selectedId)
          const cls = e.type === 'INSTANCE_OF' ? 'mem-edge inst' : `mem-edge react ${dirOf(e, horizon)}`
          return (
            <line
              key={e.id}
              className={`${cls}${hot ? ' hot' : ''}${selectedId && !hot ? ' dim' : ''}`}
              x1={a.x}
              y1={a.y}
              x2={b.x}
              y2={b.y}
              strokeWidth={1 / Math.sqrt(view.k)}
            />
          )
        })}
        {graph.nodes.map((n) => {
          const p = positions.get(n.id)
          if (!p) return null
          const sel = n.id === selectedId ? ' sel' : ''
          if (n.label === 'Symbol') {
            return (
              <g key={n.id} data-id={n.id} className={`mem-node sym${sel}${dim(n.id)}`}>
                <circle cx={p.x} cy={p.y} r={18} />
                <text x={p.x} y={p.y + 4} textAnchor="middle" className="mem-sym-text">
                  {n.name}
                </text>
              </g>
            )
          }
          if (n.label === 'Indicator') {
            const r = 7 + Math.sqrt(indicatorSize.get(n.id) ?? 1) * 2.5
            return (
              <g key={n.id} data-id={n.id} className={`mem-node ind${sel}${dim(n.id)}`}>
                <title>{n.name}</title>
                <circle cx={p.x} cy={p.y} r={r} />
                {showLabels ? (
                  <text x={p.x} y={p.y - r - 4} textAnchor="middle" className="mem-label" style={{ fontSize: 11 / Math.sqrt(view.k) }}>
                    {n.name.length > 34 ? `${n.name.slice(0, 33)}…` : n.name}
                  </text>
                ) : null}
              </g>
            )
          }
          const re = reactionOf.get(n.id)
          const ret = retOf(re, horizon)
          const r = 5 + Math.min(9, Math.abs(ret ?? 0) * 8)
          const cls = `mem-node ev ${dirOf(re, horizon)}${sel}${dim(n.id)}`
          const headline = n.attributes.kind === 'headline'
          return (
            <g key={n.id} data-id={n.id} className={cls}>
              <title>{re?.fact ?? n.name}</title>
              {headline ? (
                <rect x={p.x - r} y={p.y - r} width={r * 2} height={r * 2} transform={`rotate(45 ${p.x} ${p.y})`} />
              ) : (
                <circle cx={p.x} cy={p.y} r={r} />
              )}
            </g>
          )
        })}
      </g>
    </svg>
  )
}
