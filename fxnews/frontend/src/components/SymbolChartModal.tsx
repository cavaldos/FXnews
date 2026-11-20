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
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts'
import { api } from '../api/client'
import { INTERVALS, type Candle, type Interval, type SymbolOut } from '../api/types'
import { useFetch } from '../hooks/useFetch'
import { useStoredString } from '../hooks/useLocalStorage'
import { makeTimeFormatters } from '../lib/chartTime'
import { fmtDateTime, fmtPct, fmtPrice, fmtTime } from '../lib/format'
import { INTERVAL_SECONDS, buildMarkers, snapToBar, type MarkerInfo } from '../lib/markers'
import { readTheme, useChartTheme } from '../lib/theme'
import { TV_INTERVAL, tvSymbol } from '../lib/tradingview'
import { Modal } from './Modal'
import { TradingViewWidget } from './TradingViewWidget'
import { UiButton } from './UI/UiButton'
import { Spinner } from './widgets'

const VISIBLE_BARS = 120
const FOCUS_BARS = 50
const FOCUS_ID = 'focus:event'
const DAY_MS = 86_400_000
const API_RETRY_MS = 60_000

interface Props {
  symbol: string
  sym?: SymbolOut
  /** Event time (epoch ms) to scroll to and mark. */
  time?: number | null
  onClose: () => void
}

interface Tip {
  style: { top: number; left?: number; right?: number }
  info: MarkerInfo
}

function toBar(c: Candle): CandlestickData<Time> {
  return { time: c.time as UTCTimestamp, open: c.open, high: c.high, low: c.low, close: c.close }
}

/** Backend range covering `time` when it is older than the interval's default window. */
function rangeFor(time: number | null | undefined, now: number): string | undefined {
  if (!time || time > now - 2 * DAY_MS) return undefined
  const days = Math.min(9999, Math.ceil((now - time) / DAY_MS) + 3)
  return `${days}d`
}

/** Large modal with a candlestick chart (backend candles) for one symbol; TradingView embed when the backend has no data. */
export function SymbolChartModal({ symbol, sym, time, onClose }: Props) {
  const [intervalPref, setIntervalPref] = useStoredString('fxnews.modalInterval', '15m')
  const interval: Interval = INTERVALS.includes(intervalPref as Interval) ? (intervalPref as Interval) : '15m'
  const digits = sym?.digits ?? 5
  const fast = interval === '1m' || interval === '5m'
  // Fixed at open time so polling doesn't change the key.
  const [openedAt] = useState(() => Date.now())
  const range = rangeFor(time, openedAt)

  const candlesQ = useFetch(
    `sc-candles:${symbol}:${interval}:${range ?? ''}`,
    (signal) => api.candles(symbol, interval, range, signal),
    fast ? 15_000 : 60_000,
    API_RETRY_MS,
  )
  const quoteQ = useFetch(`sc-quote:${symbol}`, (signal) => api.quotes([symbol], signal), 15_000)
  const newsQ = useFetch(`sc-news:${symbol}`, (signal) => api.news({ symbol, limit: 200 }, signal), 120_000)

  const data = candlesQ.data
  const empty = Boolean(data) && data!.candles.length === 0
  const fallback = (Boolean(candlesQ.error) && !data) || empty
  const news = useMemo(() => newsQ.data ?? [], [newsQ.data])

  const canvasRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null)
  const loadedKeyRef = useRef('')
  const lastTimeRef = useRef(0)
  const infoRef = useRef<Map<string, MarkerInfo>>(new Map())
  const hoverTimeRef = useRef<number | null>(null)
  const timeRef = useRef(time)

  const [tip, setTip] = useState<Tip | null>(null)
  const [hoverBar, setHoverBar] = useState<CandlestickData<Time> | null>(null)
  const theme = useChartTheme()

  useLayoutEffect(() => {
    timeRef.current = time
  })

  // ── create the chart once (autoSize follows the modal's size, so it is correct on open) ──
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const t = readTheme()
    const chart = createChart(canvas, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: 'transparent' }, textColor: t.text, fontFamily: t.fontFamily, fontSize: 11 },
      grid: { vertLines: { color: t.grid }, horzLines: { color: t.grid } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: t.border, scaleMargins: { top: 0.12, bottom: 0.12 } },
      timeScale: { borderColor: t.border, timeVisible: true, secondsVisible: false, rightOffset: 6 },
    })
    const series = chart.addSeries(CandlestickSeries, {
      upColor: t.up,
      downColor: t.down,
      wickUpColor: t.up,
      wickDownColor: t.down,
      borderVisible: false,
    })
    chartRef.current = chart
    seriesRef.current = series
    markersRef.current = createSeriesMarkers(series, [])

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
      const bt = bar && typeof bar.time === 'number' ? bar.time : null
      if (bt !== hoverTimeRef.current) {
        hoverTimeRef.current = bt
        setHoverBar(bar ?? null)
      }
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

  useEffect(() => {
    chartRef.current?.applyOptions({
      layout: { textColor: theme.text, fontFamily: theme.fontFamily },
      grid: { vertLines: { color: theme.grid }, horzLines: { color: theme.grid } },
      rightPriceScale: { borderColor: theme.border },
      timeScale: { borderColor: theme.border },
    })
    seriesRef.current?.applyOptions({ upColor: theme.up, downColor: theme.down, wickUpColor: theme.up, wickDownColor: theme.down })
  }, [theme])

  useEffect(() => {
    const f = makeTimeFormatters(interval === '1d')
    chartRef.current?.applyOptions({ localization: { timeFormatter: f.timeFormatter }, timeScale: { tickMarkFormatter: f.tickMarkFormatter } })
  }, [interval])

  useEffect(() => {
    seriesRef.current?.applyOptions({ priceFormat: { type: 'price', precision: digits, minMove: 1 / 10 ** digits } })
  }, [digits])

  // ── candles: full load on symbol/interval change (scrolling to the event), incremental update on refresh ──
  useEffect(() => {
    const chart = chartRef.current
    const series = seriesRef.current
    if (!chart || !series || !data) return
    const key = `${data.symbol}:${data.interval}`
    const bars = data.candles.map(toBar)
    if (loadedKeyRef.current !== key) {
      series.setData(bars)
      loadedKeyRef.current = key
      const n = bars.length
      lastTimeRef.current = n ? (bars[n - 1].time as number) : 0
      const span = INTERVAL_SECONDS[data.interval] ?? 900
      const focus = timeRef.current ? snapToBar(data.candles.map((c) => c.time), Math.floor(timeRef.current / 1000), span) : null
      const idx = focus === null ? -1 : bars.findIndex((b) => b.time === focus)
      if (idx >= 0) {
        // Centre the event, but never leave empty space past the latest bar beyond the usual right offset.
        const to = Math.min(n + 6, idx + FOCUS_BARS)
        chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, Math.min(idx - FOCUS_BARS, to - 2 * FOCUS_BARS)), to })
      } else {
        chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - VISIBLE_BARS), to: n + 6 })
      }
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

  // ── markers: news + the clicked event time ──
  const built = useMemo(() => {
    if (!data) return { markers: [] as SeriesMarker<Time>[], info: new Map<string, MarkerInfo>(), focusFound: false }
    const span = INTERVAL_SECONDS[data.interval] ?? 900
    const b = buildMarkers(data.candles, span, news, [], theme)
    const markers = [...b.markers]
    const info = new Map(b.info)
    let focusFound = false
    if (time) {
      const bar = snapToBar(
        data.candles.map((c) => c.time),
        Math.floor(time / 1000),
        span,
      )
      if (bar !== null) {
        focusFound = true
        markers.push({ time: bar as UTCTimestamp, position: 'belowBar', shape: 'arrowUp', color: theme.accent, text: 'Event', id: FOCUS_ID, size: 2 })
        info.set(FOCUS_ID, { title: 'Event time', lines: [fmtDateTime(time)] })
        markers.sort((a, c) => (a.time as number) - (c.time as number))
      }
    }
    return { markers, info, focusFound }
  }, [data, news, theme, time])
  useEffect(() => {
    infoRef.current = built.info
    markersRef.current?.setMarkers(built.markers)
  }, [built])

  // ── header numbers ──
  const quote = quoteQ.data?.find((q) => q.symbol === symbol)
  const lastBar = data && data.candles.length > 0 ? toBar(data.candles[data.candles.length - 1]) : null
  const price = quote?.price ?? lastBar?.close
  const dir = quote ? (quote.change > 0 ? 'up' : quote.change < 0 ? 'down' : 'flat') : 'flat'
  const bar = hoverBar ?? lastBar
  const up = bar ? bar.close >= bar.open : true
  const p = (v: number) => fmtPrice(v, digits)

  return (
    <Modal onClose={onClose} size="wide" ariaLabel={`${symbol} chart`}>
      <div className="sc-modal">
        <div className="chart-head sc-head">
          <div className="chart-title">
            <strong>{symbol}</strong>
            {sym ? <span className="sc-sub">{sym.base}/{sym.quote}</span> : null}
            {price !== undefined ? <span className="sc-price">{fmtPrice(price, digits)}</span> : null}
            {quote ? (
              <span className={`sc-chg ${dir}`}>
                {quote.change > 0 ? '+' : ''}
                {fmtPrice(quote.change, digits)} ({fmtPct(quote.change_pct)})
              </span>
            ) : null}
            {bar && !fallback ? (
              <span className={`ohlc ${up ? 'up' : 'down'}`}>
                <span>O <b>{p(bar.open)}</b></span>
                <span>H <b>{p(bar.high)}</b></span>
                <span>L <b>{p(bar.low)}</b></span>
                <span>C <b>{p(bar.close)}</b></span>
              </span>
            ) : null}
          </div>
          <div className="sc-tools">
            <div className="seg" role="group" aria-label="Timeframe">
              {INTERVALS.map((iv) => (
                <button key={iv} type="button" className={iv === interval ? 'active' : ''} onClick={() => setIntervalPref(iv)}>
                  {iv}
                </button>
              ))}
            </div>
            <button type="button" className="modal-x" onClick={onClose} aria-label="Close">
              ×
            </button>
          </div>
        </div>

        <div className="chart-box sc-box">
          <div className="chart-canvas" ref={canvasRef} />
          {!data && !candlesQ.error ? (
            <div className="chart-overlay">
              <Spinner label={`Loading ${symbol} ${interval}…`} />
            </div>
          ) : null}
          {tip ? (
            <div className="chart-tip" style={tip.style}>
              <strong>{tip.info.title}</strong>
              {tip.info.lines.map((l, i) => (
                <div key={i}>{l}</div>
              ))}
            </div>
          ) : null}
          {fallback ? (
            <div className="chart-fallback">
              <div className="fallback-note">
                <span>
                  {empty ? 'API has no candles for this timeframe' : `Candle API error: ${candlesQ.error?.message}`}. Using TradingView embed —{' '}
                  <b>no news markers in this mode</b>.
                </span>
                <UiButton tone="ghost" size="xs" onClick={candlesQ.reload}>
                  Retry
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
          {time ? (
            <span className="lg lg-event" title={fmtDateTime(time)}>
              ▲ event {fmtDateTime(time)}
              {data && !built.focusFound && !fallback ? ' (outside candle data)' : ''}
            </span>
          ) : null}
          <span className="spacer" />
          {data ? <span className="lg-src">{data.source}</span> : null}
          {data?.stale ? (
            <span className="lg-err" title="All providers failed; showing last-good cached candles">
              stale{data.as_of ? ` · data as of ${fmtTime(data.as_of * 1000)}` : ''}
            </span>
          ) : null}
          {candlesQ.error && data ? (
            <span className="lg-err" title={candlesQ.error.message}>
              ⚠ refresh error
            </span>
          ) : null}
        </div>
      </div>
    </Modal>
  )
}
