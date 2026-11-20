import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from './api/client'
import { INTERVALS, type AnalysisOut, type Interval, type NewsOut } from './api/types'
import { AdvicePanel } from './components/AdvicePanel'
import { CenterColumn } from './components/CenterColumn'
import { Header } from './components/Header'
import { NewsColumn, type NewsTab } from './components/NewsColumn'
import { NewsDetail } from './components/NewsDetail'
import { PriceChart } from './components/PriceChart'
import { SideColumn } from './components/SideColumn'
import { Toasts } from './components/Toasts'
import { useAnalysisStore } from './hooks/useAnalysisStore'
import { useEventStream } from './hooks/useEventStream'
import { useFetch } from './hooks/useFetch'
import { useStoredString } from './hooks/useLocalStorage'
import { useToasts } from './hooks/useToasts'
import { Modal } from './components/Modal'
import { ReactionProvider } from './components/ReactionProvider'
import { SymbolChartProvider } from './components/SymbolChartProvider'
import { parsePage } from './lib/pages'
import { BacktestPage } from './pages/BacktestPage'
import { CalendarPage } from './pages/CalendarPage'
import { DataPage } from './pages/DataPage'
import LlmLabPage from './pages/LlmLabPage'
import { fmtTime, toMs } from './lib/format'
import { isRelated, itemTime, mergeNews, sortNewest, sortOldest } from './lib/news'

const MAX_LIVE_NEWS = 100
const MAX_FEED = 30
const FRESH_MS = 6000

function errMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

function tabOf(n: NewsOut): NewsTab {
  return n.kind === 'calendar' ? 'calendar' : 'headlines'
}

export default function App() {
  const [symbolPref, setSymbolPref] = useStoredString('fxnews.symbol', 'EURUSD')
  const [intervalPref, setIntervalPref] = useStoredString('fxnews.interval', '15m')
  const [model, setModel] = useStoredString('fxnews.model', '')
  const [pagePref, setPagePref] = useStoredString('fxnews.page', 'dashboard')
  const page = parsePage(pagePref)
  const [tab, setTab] = useState<NewsTab>('headlines')
  const [selectedNews, setSelectedNews] = useState<NewsOut | null>(null)
  const [liveNews, setLiveNews] = useState<NewsOut[]>([])
  const [freshNews, setFreshNews] = useState<ReadonlySet<number>>(new Set())
  const [freshAnalyses, setFreshAnalyses] = useState<ReadonlySet<number>>(new Set())
  const [analysisOverride, setAnalysisOverride] = useState<Record<number, number>>({})
  const [analyzing, setAnalyzing] = useState<number | null>(null)
  const [analyzeError, setAnalyzeError] = useState<{ id: number; message: string } | null>(null)
  const [ingesting, setIngesting] = useState(false)
  const [calendarRev, setCalendarRev] = useState(0)

  const toasts = useToasts()
  const store = useAnalysisStore()
  const { ensure } = store

  const interval: Interval = INTERVALS.includes(intervalPref as Interval) ? (intervalPref as Interval) : '15m'

  // ── data ──
  // The catalog gates every SymbolLink (unknown => plain text), so a failed load must be retried, not left empty for the session.
  const symbolsQ = useFetch('symbols', (s) => api.symbols(s), 0, 15_000)
  const symbols = symbolsQ.data
  const sym = symbols?.find((s) => s.symbol === symbolPref) ?? symbols?.[0]
  const symbol = sym?.symbol ?? symbolPref

  const healthQ = useFetch('health', (s) => api.health(s), 30_000)
  const quotesQ = useFetch('quotes', (s) => api.quotes(undefined, s), 15_000)
  const modelsQ = useFetch('models', (s) => api.models(s))
  const headlinesQ = useFetch('news:headline', (s) => api.news({ kind: 'headline', limit: 200 }, s), 60_000)
  const calendarQ = useFetch('calendar', (s) => api.calendar({ days: 7, past_days: 5 }, s), 5 * 60_000)
  const signalsQ = useFetch(`signals:${symbol}`, (s) => api.signals(symbol, 50, s), 60_000)
  const analysesQ = useFetch(`analyses:${symbol}`, (s) => api.analyses(symbol, 20, s), 60_000)

  const { reload: reloadCalendar } = calendarQ
  const { reload: reloadSignals } = signalsQ
  const { reload: reloadHeadlines } = headlinesQ
  const { reload: reloadHealth } = healthQ
  const { reload: reloadAnalyses } = analysesQ

  // ── analyses known to the client (list endpoint + SSE + POST responses) ──
  const allAnalyses = useMemo(() => {
    const m = new Map<number, AnalysisOut>()
    for (const a of analysesQ.data ?? []) m.set(a.id, a)
    for (const a of Object.values(store.analyses)) m.set(a.id, a)
    return m
  }, [analysesQ.data, store.analyses])

  /** news id -> latest analysis id (so rows show "AI" before the next list refetch). */
  const analysisIdByNews = useMemo(() => {
    const best = new Map<number, AnalysisOut>()
    for (const a of allAnalyses.values()) {
      if (a.news_id == null) continue
      const prev = best.get(a.news_id)
      if (!prev || (toMs(a.created_at) ?? 0) >= (toMs(prev.created_at) ?? 0)) best.set(a.news_id, a)
    }
    const out: Record<number, number> = {}
    for (const [newsId, a] of best) out[newsId] = a.id
    return { ...out, ...analysisOverride }
  }, [allAnalyses, analysisOverride])

  const withAnalysis = useCallback(
    (n: NewsOut): NewsOut => (n.analysis_id || !analysisIdByNews[n.id] ? n : { ...n, analysis_id: analysisIdByNews[n.id] }),
    [analysisIdByNews],
  )

  // ── news lists (fetched + live SSE) ──
  const headlines = useMemo(
    () => sortNewest(mergeNews(headlinesQ.data ?? [], liveNews).map(withAnalysis)),
    [headlinesQ.data, liveNews, withAnalysis],
  )
  const calendar = useMemo(() => sortOldest((calendarQ.data ?? []).map(withAnalysis)), [calendarQ.data, withAnalysis])
  const allNews = useMemo(() => mergeNews(headlines, calendar), [headlines, calendar])
  const newsById = useMemo(() => new Map(allNews.map((n) => [n.id, n])), [allNews])
  const chartNews = useMemo(() => allNews.filter((n) => isRelated(n, sym)), [allNews, sym])

  // ── selected news (refreshed from GET /news/{id}) ──
  const selectedId = selectedNews?.id ?? null
  const itemQ = useFetch(selectedId === null ? null : `news-item:${selectedId}`, (s) => api.newsItem(selectedId as number, s))
  const { reload: reloadItem } = itemQ
  const selected = selectedNews ? withAnalysis(itemQ.data ?? newsById.get(selectedNews.id) ?? selectedNews) : null
  const selectedAnalysisId = selected?.analysis_id ?? undefined
  const selectedAnalysis = selectedAnalysisId ? allAnalyses.get(selectedAnalysisId) : undefined

  useEffect(() => {
    if (selectedAnalysisId && !allAnalyses.has(selectedAnalysisId)) ensure([selectedAnalysisId])
  }, [selectedAnalysisId, allAnalyses, ensure])

  // ── AI panel feed: analyses relevant to the symbol ──
  const feed = useMemo(() => {
    const m = new Map<number, AnalysisOut>()
    for (const a of analysesQ.data ?? []) m.set(a.id, a)
    for (const a of Object.values(store.analyses)) {
      if (m.has(a.id)) continue
      const relevant =
        a.symbol === symbol ||
        a.trades.some((t) => t.symbol === symbol) ||
        (a.news_id != null && Boolean(newsById.get(a.news_id)?.symbols.includes(symbol)))
      if (relevant) m.set(a.id, a)
    }
    return [...m.values()]
      .sort((a, b) => (toMs(b.created_at) ?? 0) - (toMs(a.created_at) ?? 0))
      .slice(0, MAX_FEED)
  }, [analysesQ.data, store.analyses, symbol, newsById])

  // ── realtime ──
  const timers = useRef<{ calendar?: ReturnType<typeof setTimeout>; signals?: ReturnType<typeof setTimeout> }>({})
  useEffect(() => {
    const t = timers.current
    return () => {
      clearTimeout(t.calendar)
      clearTimeout(t.signals)
    }
  }, [])

  const markFresh = useCallback((setter: typeof setFreshNews, id: number) => {
    setter((prev) => new Set(prev).add(id))
    setTimeout(
      () =>
        setter((prev) => {
          const next = new Set(prev)
          next.delete(id)
          return next
        }),
      FRESH_MS,
    )
  }, [])

  const selectNews = useCallback((n: NewsOut) => {
    setSelectedNews(n)
    setTab(tabOf(n))
  }, [])

  const stream = useEventStream({
    news: (n) => {
      if (n.kind === 'headline') {
        setLiveNews((prev) => [n, ...prev.filter((x) => x.id !== n.id)].slice(0, MAX_LIVE_NEWS))
        markFresh(setFreshNews, n.id)
      } else {
        clearTimeout(timers.current.calendar)
        timers.current.calendar = setTimeout(() => {
          reloadCalendar()
          setCalendarRev((r) => r + 1)
        }, 1500)
      }
      const nearby = n.kind === 'headline' || Math.abs(itemTime(n) - Date.now()) < 12 * 3600 * 1000
      if (n.impact === 'High' && nearby) {
        toasts.push({
          tone: 'news',
          impact: n.impact,
          title: `${n.currency ? `${n.currency} · ` : ''}${n.title}`,
          text: n.kind === 'calendar' ? `Calendar ${fmtTime(itemTime(n))}` : n.source,
          onClick: () => selectNews(n),
        })
      }
    },
    analysis: (a) => {
      store.add([a])
      markFresh(setFreshAnalyses, a.id)
      clearTimeout(timers.current.signals)
      timers.current.signals = setTimeout(reloadSignals, 1000)
    },
  })

  // ── actions ──
  const analyzeSelected = async () => {
    if (!selected) return
    const id = selected.id
    setAnalyzing(id)
    setAnalyzeError(null)
    try {
      const a = await api.analyzeNews(id, model || undefined)
      store.add([a])
      setAnalysisOverride((prev) => ({ ...prev, [id]: a.id }))
      markFresh(setFreshAnalyses, a.id)
      reloadItem()
      reloadSignals()
      reloadAnalyses()
    } catch (err) {
      setAnalyzeError({ id, message: errMessage(err) })
    } finally {
      setAnalyzing((cur) => (cur === id ? null : cur))
    }
  }

  const askAdvice = async (question: string | undefined, signal: AbortSignal) => {
    const a = await api.advice({ symbol, question, model: model || undefined, interval }, signal)
    store.add([a])
    markFresh(setFreshAnalyses, a.id)
    reloadSignals()
  }

  const runIngest = async () => {
    setIngesting(true)
    try {
      const r = await api.ingestRun()
      const notes = r.notes ?? []
      toasts.push({
        tone: 'info',
        title: 'Fetch complete',
        text: [`+${r.inserted} new headlines, ${r.analyzed} analyzed`, ...notes].join(' · '),
      })
      if (r.errors.length > 0) toasts.push({ tone: 'error', title: `${r.errors.length} source errors`, text: r.errors.slice(0, 2).join(' | ') }, 12_000)
      reloadHeadlines()
      reloadCalendar()
      reloadHealth()
      reloadSignals()
      reloadAnalyses()
    } catch (err) {
      toasts.push({ tone: 'error', title: 'Fetch failed', text: errMessage(err) })
    } finally {
      setIngesting(false)
    }
  }

  useEffect(() => {
    document.title = `${symbol} · FXNews`
  }, [symbol])

  const newsTitleOf = useCallback((id: number) => newsById.get(id)?.title, [newsById])
  const selectToggle = (n: NewsOut) => (selectedId === n.id ? setSelectedNews(null) : selectNews(n))

  return (
    <SymbolChartProvider symbols={symbols ?? []}>
    <ReactionProvider>
    <div className="app">
      <Header
        symbols={symbols ?? []}
        symbol={symbol}
        onSymbol={setSymbolPref}
        quotes={quotesQ.data ?? []}
        quotesError={quotesQ.error}
        health={healthQ.data}
        healthError={healthQ.error && !healthQ.data ? healthQ.error : undefined}
        stream={stream}
        ingesting={ingesting}
        onIngest={runIngest}
        page={page}
        onPage={(p) => {
          setSelectedNews(null)
          setPagePref(p)
        }}
      />

      {page === 'calendar' ? <CalendarPage onSelect={setSelectedNews} calendarRev={calendarRev} /> : null}
      {page === 'backtest' ? <BacktestPage /> : null}
      {page === 'llm' ? <LlmLabPage /> : null}
      {page === 'data' ? <DataPage /> : null}

      {page === 'dashboard' ? (
      <main className="grid">
        <NewsColumn
          tab={tab}
          onTab={setTab}
          sym={sym}
          headlines={{ items: headlines, loading: headlinesQ.loading, error: headlinesQ.error, reload: headlinesQ.reload }}
          calendar={{ items: calendar, loading: calendarQ.loading, error: calendarQ.error, reload: calendarQ.reload }}
          freshIds={freshNews}
          selectedId={selectedId ?? undefined}
          onSelect={selectToggle}
        >
          {selected ? (
            <NewsDetail
              item={selected}
              symbol={symbol}
              analysis={selectedAnalysis}
              analysisLoading={Boolean(selectedAnalysisId) && !selectedAnalysis}
              analyzing={analyzing === selected.id}
              analyzeError={analyzeError?.id === selected.id ? analyzeError.message : undefined}
              onAnalyze={analyzeSelected}
              onClose={() => setSelectedNews(null)}
            />
          ) : null}
        </NewsColumn>

        <CenterColumn symbols={symbols ?? []}>
          <PriceChart
            sym={sym}
            symbol={symbol}
            interval={interval}
            onInterval={setIntervalPref}
            news={chartNews}
            signals={signalsQ.data ?? []}
            onSelectNews={selectNews}
          />
        </CenterColumn>

        <SideColumn symbol={symbol} sym={sym} interval={interval}>
          <AdvicePanel
            symbol={symbol}
            models={modelsQ.data ?? []}
            model={model}
            onModel={setModel}
            health={healthQ.data}
            feed={feed}
            feedLoading={analysesQ.loading}
            feedError={analysesQ.error}
            onReloadFeed={analysesQ.reload}
            freshIds={freshAnalyses}
            newsTitleOf={newsTitleOf}
            onAsk={askAdvice}
          />
        </SideColumn>
      </main>
      ) : null}

      {page === 'calendar' && selected ? (
        <Modal onClose={() => setSelectedNews(null)}>
          <NewsDetail
            item={selected}
            symbol={symbol}
            analysis={selectedAnalysis}
            analysisLoading={Boolean(selectedAnalysisId) && !selectedAnalysis}
            analyzing={analyzing === selected.id}
            analyzeError={analyzeError?.id === selected.id ? analyzeError.message : undefined}
            onAnalyze={analyzeSelected}
            onClose={() => setSelectedNews(null)}
          />
        </Modal>
      ) : null}

      <Toasts toasts={toasts.toasts} onDismiss={toasts.dismiss} />
    </div>
    </ReactionProvider>
    </SymbolChartProvider>
  )
}
