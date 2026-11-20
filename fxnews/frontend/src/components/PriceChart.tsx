import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  createChart,
  createSeriesMarkers,
  type CandlestickData,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type MouseEventParams,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts'
import { api } from '../api/client'
import { INTERVALS, type Candle, type Interval, type NewsOut, type SignalOut, type SymbolOut } from '../api/types'
import { useFetch } from '../hooks/useFetch'
import { useNowSlow } from '../hooks/useClock'
import { makeTimeFormatters } from '../lib/chartTime'
import { flag, fmtPrice, fmtTime } from '../lib/format'
import { INTERVAL_SECONDS, buildMarkers, type MarkerInfo } from '../lib/markers'
import { itemTime } from '../lib/news'
import { readTheme, useChartTheme } from '../lib/theme'
import { TV_INTERVAL, tvSymbol } from '../lib/tradingview'
import { TradingViewWidget } from './TradingViewWidget'
import { UiButton } from './UI/UiButton'
import { Countdown, Spinner } from './widgets'

const VISIBLE_BARS = 120
const UPCOMING_WINDOW_MS = 24 * 3600 * 1000
const API_RETRY_MS = 60_000

interface Props {
  sym?: SymbolOut
  symbol: string
  interval: Interval
  onInterval: (interval: Interval) => void
  /** News + calendar related to the symbol (markers + upcoming strip). */
  news: NewsOut[]
  signals: SignalOut[]
  onSelectNews: (item: NewsOut) => void
}

interface Tip {
  /** CSS offsets inside the chart box (computed in the event handler, not at render). */
  style: { top: number; left?: number; right?: number }
  info: MarkerInfo
}

function toBar(c: Candle): CandlestickData<Time> {
  return { time: c.time as UTCTimestamp, open: c.open, high: c.high, low: c.low, close: c.close }
}

function fmtClock(ms: number): string {
  const d = new Date(ms)
  return d.toLocaleTimeString('en-GB', { hour12: false })
}

export function PriceChart({ sym, symbol, interval, onInterval, news, signals, onSelectNews }: Props) {
  const digits = sym?.digits ?? 5
  const fast = interval === '1m' || interval === '5m'
  const candlesQ = useFetch(
    `candles:${symbol}:${interval}`,
    (signal) => api.candles(symbol, interval, undefined, signal),
    fast ? 15_000 : 60_000,
    API_RETRY_MS,
  )
  const data = candlesQ.data
  const fallback = Boolean(candlesQ.error) && !data
  const now = useNowSlow()

  const canvasRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null)
  const loadedKeyRef = useRef('')
  const lastTimeRef = useRef(0)
  const infoRef = useRef<Map<string, MarkerInfo>>(new Map())
  const hoverTimeRef = useRef<number | null>(null)
  const newsRef = useRef(news)
  const onSelectRef = useRef(onSelectNews)

  const [tip, setTip] = useState<Tip | null>(null)
  const [hoverBar, setHoverBar] = useState<CandlestickData<Time> | null>(null)

  const theme = useChartTheme()

  useLayoutEffect(() => {
    newsRef.current = news
    onSelectRef.current = onSelectNews
  })

  // ── create the chart once ──
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const theme = readTheme() // initial colours; later theme switches are applied by the effect below
    const chart = createChart(canvas, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: theme.text,
        fontFamily: theme.fontFamily,
        fontSize: 11,
      },
      grid: { vertLines: { color: theme.grid }, horzLines: { color: theme.grid } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: theme.border, scaleMargins: { top: 0.12, bottom: 0.12 } },
      timeScale: { borderColor: theme.border, timeVisible: true, secondsVisible: false, rightOffset: 6 },
    })
    const series = chart.addSeries(CandlestickSeries, {
      upColor: theme.up,
      downColor: theme.down,
      wickUpColor: theme.up,
      wickDownColor: theme.down,
      borderVisible: false,
    })
    const markers = createSeriesMarkers(series, [])
    chartRef.current = chart
    seriesRef.current = series
    markersRef.current = markers

    chart.subscribeCrosshairMove((param: MouseEventParams<Time>) => {
      const hovered = param.hoveredInfo?.objectId ?? param.hoveredObjectId
      const info = typeof hovered === 'string' ? infoRef.current.get(hovered) : undefined
      if (info && param.point) {
        const { x, y } = param.point
        const flip = x > canvas.clientWidth * 0.55
        setTip({ style: { top: y + 14, ...(flip ? { right: canvas.clientWidth - x + 14 } : { left: x + 14 }) }, info })
      } else {
        setTip(null)
      }

      const bar = param.time !== undefined ? (param.seriesData.get(series) as CandlestickData<Time> | undefined) : undefined
      const t = bar && typeof bar.time === 'number' ? bar.time : null
      if (t !== hoverTimeRef.current) {
        hoverTimeRef.current = t
        setHoverBar(bar ?? null)
      }
    })

    chart.subscribeClick((param: MouseEventParams<Time>) => {
      const hovered = param.hoveredInfo?.objectId ?? param.hoveredObjectId
      if (typeof hovered !== 'string' || !hovered.startsWith('news:')) return
      const id = infoRef.current.get(hovered)?.newsId
      const item = id === undefined ? undefined : newsRef.current.find((n) => n.id === id)
      if (item) onSelectRef.current(item)
    })

    return () => {
      chart.remove()
      chartRef.current = null
      seriesRef.current = null
      markersRef.current = null
      loadedKeyRef.current = ''
      lastTimeRef.current = 0
      hoverTimeRef.current = null
    }
  }, [])

  // ── follow theme / font switches without recreating the chart ──
  useEffect(() => {
    chartRef.current?.applyOptions({
      layout: { textColor: theme.text, fontFamily: theme.fontFamily },
      grid: { vertLines: { color: theme.grid }, horzLines: { color: theme.grid } },
      rightPriceScale: { borderColor: theme.border },
      timeScale: { borderColor: theme.border },
    })
    seriesRef.current?.applyOptions({
      upColor: theme.up,
      downColor: theme.down,
      wickUpColor: theme.up,
      wickDownColor: theme.down,
    })
  }, [theme])

  // ── axis labels in the viewer's timezone ──
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    const f = makeTimeFormatters(interval === '1d')
    chart.applyOptions({
      localization: { timeFormatter: f.timeFormatter },
      timeScale: { tickMarkFormatter: f.tickMarkFormatter },
    })
  }, [interval])

  // ── price precision ──
  useEffect(() => {
    seriesRef.current?.applyOptions({
      priceFormat: { type: 'price', precision: digits, minMove: 1 / 10 ** digits },
    })
  }, [digits])

  // ── candle data: full load on symbol/interval change, incremental update on refresh ──
  useEffect(() => {
    const chart = chartRef.current
    const series = seriesRef.current
    if (!chart || !series || !data) return
    const key = `${data.symbol}:${data.interval}`
    const bars = data.candles.map(toBar)
    if (loadedKeyRef.current !== key) {
      series.setData(bars)
      loadedKeyRef.current = key
      lastTimeRef.current = bars.length ? (bars[bars.length - 1].time as number) : 0
      const n = bars.length
      chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - VISIBLE_BARS), to: n + 6 })
      hoverTimeRef.current = null
      setHoverBar(null)
    } else {
      for (const bar of bars) {
        const t = bar.time as number
        if (t >= lastTimeRef.current) {
          series.update(bar)
          lastTimeRef.current = t
        }
      }
    }
  }, [data])

  // ── markers: news (by impact) + signals (arrows) ──
  const built = useMemo(
    () =>
      data
        ? buildMarkers(data.candles, INTERVAL_SECONDS[data.interval] ?? 900, news, signals, theme)
        : { markers: [], info: new Map<string, MarkerInfo>() },
    [data, news, signals, theme],
  )
  useEffect(() => {
    infoRef.current = built.info
    markersRef.current?.setMarkers(built.markers)
  }, [built])

  // ── derived UI ──
  const upcoming = useMemo(
    () =>
      news
        .filter((n) => {
          if (n.kind !== 'calendar' || (n.impact !== 'High' && n.impact !== 'Medium')) return false
          const t = itemTime(n)
          return t > now && t - now < UPCOMING_WINDOW_MS
        })
        .sort((a, b) => itemTime(a) - itemTime(b))
        .slice(0, 3),
    [news, now],
  )

  const lastBar = data && data.candles.length > 0 ? toBar(data.candles[data.candles.length - 1]) : null
  const bar = hoverBar ?? lastBar
  const up = bar ? bar.close >= bar.open : true
  const p = (v: number) => fmtPrice(v, digits)

  return (
    <section className="panel chart-col" aria-label="Price chart">
      <div className="chart-head">
        <div className="chart-title">
          <strong>{symbol}</strong>
          {bar ? (
            <span className={`ohlc ${up ? 'up' : 'down'}`}>
              <span>
                O <b>{p(bar.open)}</b>
              </span>
              <span>
                H <b>{p(bar.high)}</b>
              </span>
              <span>
                L <b>{p(bar.low)}</b>
              </span>
              <span>
                C <b>{p(bar.close)}</b>
              </span>
            </span>
          ) : null}
        </div>
        <div className="seg" role="group" aria-label="Timeframe">
          {INTERVALS.map((iv) => (
            <button key={iv} type="button" className={iv === interval ? 'active' : ''} onClick={() => onInterval(iv)}>
              {iv}
            </button>
          ))}
        </div>
      </div>

      {upcoming.length > 0 ? (
        <div className="upcoming" aria-label="Upcoming events">
          <span className="upcoming-label">Upcoming</span>
          {upcoming.map((n) => (
            <button key={n.id} type="button" className={`upcoming-item imp-${n.impact?.toLowerCase()}`} onClick={() => onSelectNews(n)}>
              <span>
                {flag(n.currency)} {n.currency} {n.title}
              </span>
              <Countdown targetMs={itemTime(n)} />
            </button>
          ))}
        </div>
      ) : null}

      <div className="chart-box">
        <div className="chart-canvas" ref={canvasRef} />

        {tip ? (
          <div className="chart-tip" style={tip.style}>
            <strong>{tip.info.title}</strong>
            {tip.info.lines.map((l, i) => (
              <div key={i}>{l}</div>
            ))}
          </div>
        ) : null}

        {candlesQ.loading ? (
          <div className="chart-overlay">
            <Spinner label="Loading candles…" />
          </div>
        ) : null}
        {data && data.candles.length === 0 ? <div className="chart-overlay">No candle data for {symbol} ({interval}).</div> : null}

        {fallback ? (
          <div className="chart-fallback">
            <div className="fallback-note" role="alert">
              <span>
                ⚠ Candle API error: {candlesQ.error?.message}. Using TradingView embed — <b>no news markers in this mode</b>; retrying the API in 60s.
              </span>
              <UiButton tone="ghost" size="xs" onClick={candlesQ.reload}>
                Retry now
              </UiButton>
            </div>
            <TradingViewWidget key={`${symbol}:${interval}`} symbol={tvSymbol(symbol, sym)} interval={TV_INTERVAL[interval]} />
          </div>
        ) : null}
      </div>

      <div className="chart-legend">
        <span className="lg lg-high">■ High</span>
        <span className="lg lg-medium">■ Medium</span>
        <span className="lg lg-low">■ Low</span>
        <span className="lg lg-news">● headline</span>
        <span className="lg lg-up">▲ bullish</span>
        <span className="lg lg-down">▼ bearish</span>
        <span className="spacer" />
        {data ? <span className="lg-src">{data.source}</span> : null}
        {data?.stale ? (
          <span className="lg-err" title="All providers failed; showing last-good cached candles">
            stale{data.as_of ? ` · data as of ${fmtTime(data.as_of * 1000)}` : ''}
          </span>
        ) : null}
        {candlesQ.updatedAt ? <span className="lg-src">updated {fmtClock(candlesQ.updatedAt)}</span> : null}
        {candlesQ.error && data ? <span className="lg-err" title={candlesQ.error.message}>⚠ refresh error</span> : null}
      </div>
    </section>
  )
}
