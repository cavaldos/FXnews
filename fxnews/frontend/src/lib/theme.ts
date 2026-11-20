import { useEffect, useState } from 'react'

export interface ChartTheme {
  text: string
  grid: string
  border: string
  up: string
  down: string
  accent: string
  high: string
  medium: string
  low: string
  dim: string
  /** Card background, used to blend third-party widgets into the panel. */
  surface: string
  /** True for light themes (e.g. Latte). */
  light: boolean
  fontFamily: string
}

function luminance(color: string): number | null {
  const m = /^#([0-9a-f]{6})$/i.exec(color.trim())
  if (!m) return null
  const n = parseInt(m[1], 16)
  const lin = (c: number) => {
    const s = c / 255
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * lin((n >> 16) & 255) + 0.7152 * lin((n >> 8) & 255) + 0.0722 * lin(n & 255)
}

/** Read the CSS custom properties so canvas / third-party widgets share the page theme. */
export function readTheme(): ChartTheme {
  const root = document.documentElement
  const cs = getComputedStyle(root)
  const v = (name: string, fallback: string) => cs.getPropertyValue(name).trim() || fallback
  const surface = v('--bg-card', '#0b1017')
  return {
    text: v('--text-secondary', '#6a8a9a'),
    grid: v('--table-border-soft', 'rgba(30, 52, 72, 0.5)'),
    border: v('--border', '#1e3448'),
    up: v('--green', '#2aff8a'),
    down: v('--red', '#ff4a4a'),
    accent: v('--accent', '#4a9eff'),
    high: v('--red', '#ff4a4a'),
    medium: v('--orange', '#ff9a3c'),
    low: v('--yellow', '#ffcc44'),
    dim: v('--text-dim', '#3a5a6a'),
    surface,
    light: (luminance(v('--bg-base', '#080c10')) ?? 0) > 0.4,
    fontFamily: v('--mono', "'Geist Mono', ui-monospace, monospace"),
  }
}

function sameTheme(a: ChartTheme, b: ChartTheme): boolean {
  return (Object.keys(a) as (keyof ChartTheme)[]).every((k) => a[k] === b[k])
}

/** Live chart theme: re-read whenever `applyTheme` / `applyFontFamily` change the inline :root variables. */
export function useChartTheme(): ChartTheme {
  const [theme, setTheme] = useState(readTheme)

  useEffect(() => {
    const sync = () => setTheme((prev) => {
      const next = readTheme()
      return sameTheme(prev, next) ? prev : next
    })
    sync()
    const observer = new MutationObserver(sync)
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['style'] })
    return () => observer.disconnect()
  }, [])

  return theme
}
