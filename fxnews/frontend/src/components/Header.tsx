import { useLayoutEffect, useRef, useState, type RefObject } from 'react'
import type { HealthOut, QuoteOut, SymbolOut } from '../api/types'
import type { StreamStatus } from '../hooks/useEventStream'
import { PAGES, type Page } from '../lib/pages'
import { fmtPct, fmtPrice, shortModel } from '../lib/format'
import { SettingsMenu } from './SettingsMenu'
import { SymbolLink } from './SymbolLink'
import { UiButton } from './UI/UiButton'
import { UiSelect } from './UI/UiSelect'
import type { SelectOption } from './UI/types'
import { Spinner, TimeAgo } from './widgets'

const KIND_ORDER: SymbolOut['kind'][] = ['fx', 'metal', 'energy', 'index', 'crypto']
const KIND_LABEL: Record<SymbolOut['kind'], string> = {
  fx: 'Forex',
  metal: 'Metals',
  energy: 'Energy',
  index: 'Indices',
  crypto: 'Crypto',
}

interface Props {
  symbols: SymbolOut[]
  symbol: string
  onSymbol: (symbol: string) => void
  quotes: QuoteOut[]
  quotesError?: Error
  health?: HealthOut
  healthError?: Error
  stream: StreamStatus
  ingesting: boolean
  onIngest: () => void
  page: Page
  onPage: (page: Page) => void
}

/** Header quote chip: the price cell flashes green / red whenever a refresh moves it. */
function QuoteChip({ quote, digits, active, onSelect }: { quote: QuoteOut; digits: number; active: boolean; onSelect: () => void }) {
  const [prevPrice, setPrevPrice] = useState(quote.price)
  const [flash, setFlash] = useState<{ dir: 'up' | 'down'; n: number } | null>(null)
  // Derived-state pattern: compare with the previous render's price and bump a key to restart the CSS animation.
  if (quote.price !== prevPrice) {
    setPrevPrice(quote.price)
    setFlash({ dir: quote.price > prevPrice ? 'up' : 'down', n: (flash?.n ?? 0) + 1 })
  }
  const dir = quote.change > 0 ? 'up' : quote.change < 0 ? 'down' : 'flat'
  return (
    <button
      type="button"
      className={`quote ${dir}${active ? ' active' : ''}`}
      onClick={onSelect}
      title={`${quote.symbol}: ${quote.change > 0 ? '+' : ''}${quote.change}`}
    >
      <SymbolLink symbol={quote.symbol} className="quote-sym" />
      <span key={flash?.n ?? 0} className={`quote-price${flash ? ` quote-flash ${flash.dir}` : ''}`}>
        {fmtPrice(quote.price, digits)}
      </span>
      <span className="quote-chg">{fmtPct(quote.change_pct)}</span>
    </button>
  )
}

/**
 * Hides whole quote pills that don't fit the strip, so none is ever cut mid-pill.
 * Only applies while the strip is a single non-scrolling row (desktop); the narrow layout
 * wraps the strip with its own horizontal scroll (see layout.css), so every pill stays visible there.
 */
function fitQuotePills(strip: HTMLElement) {
  const pills = Array.from(strip.querySelectorAll<HTMLElement>('.quote'))
  pills.forEach((p) => {
    p.hidden = false
  })
  if (getComputedStyle(strip).overflowX !== 'hidden') return

  const max = strip.clientWidth
  const gap = parseFloat(getComputedStyle(strip).columnGap) || 0
  let used = 0
  let overflowing = false
  for (const p of pills) {
    const next = used === 0 ? p.offsetWidth : used + gap + p.offsetWidth
    if (overflowing || next > max) {
      overflowing = true
      p.hidden = true
    } else {
      used = next
    }
  }
}

function useFitQuotePills(stripRef: RefObject<HTMLDivElement | null>, quotes: QuoteOut[]) {
  // Re-fit whenever prices/symbols change (pill widths change with the digits shown).
  useLayoutEffect(() => {
    if (stripRef.current) fitQuotePills(stripRef.current)
  }, [stripRef, quotes])

  // Re-fit when the strip's width changes (window resize, font-size setting, status-bar width changes).
  useLayoutEffect(() => {
    const el = stripRef.current
    if (!el) return
    const ro = new ResizeObserver(() => fitQuotePills(el))
    ro.observe(el)
    return () => ro.disconnect()
  }, [stripRef])
}

function Pill({ on, label, title }: { on: boolean; label: string; title?: string }) {
  return (
    <span className={`pill ${on ? 'on' : 'off'}`} title={title}>
      <span className="pill-dot" /> {label}
    </span>
  )
}

export function Header({ symbols, symbol, onSymbol, quotes, quotesError, health, healthError, stream, ingesting, onIngest, page, onPage }: Props) {
  const digitsOf = new Map(symbols.map((s) => [s.symbol, s.digits]))
  const options: SelectOption[] = KIND_ORDER.flatMap((kind) =>
    symbols.filter((s) => s.kind === kind).map((s) => ({ value: s.symbol, label: s.symbol, group: KIND_LABEL[kind] })),
  )
  if (!options.some((o) => o.value === symbol)) options.unshift({ value: symbol, label: symbol })

  const stripRef = useRef<HTMLDivElement>(null)
  useFitQuotePills(stripRef, quotes)

  return (
    <header className="app-header">
      <div className="app-header-inner">
        <div className="app-brand">
          <span className="app-brand-mark" aria-hidden>
            ▮▮▯
          </span>
          <span className="app-brand-text">FXNews</span>
        </div>

        <nav className="page-nav" aria-label="Pages">
          {PAGES.map((p) => (
            <button key={p.id} type="button" className={p.id === page ? 'active' : ''} aria-current={p.id === page ? 'page' : undefined} onClick={() => onPage(p.id)}>
              {p.label}
            </button>
          ))}
        </nav>

        <UiSelect className="symbol-select" size="sm" value={symbol} options={options} onChange={onSymbol} ariaLabel="Select symbol" />

        <div className="quote-strip" ref={stripRef} aria-label="Quotes">
          {quotes.length === 0 && quotesError ? (
            <span className="quote-empty" title={quotesError.message}>
              No quotes yet — data source is failing, retrying shortly
            </span>
          ) : null}
          {quotes.map((q) => (
            <QuoteChip key={q.symbol} quote={q} digits={digitsOf.get(q.symbol) ?? 5} active={q.symbol === symbol} onSelect={() => onSymbol(q.symbol)} />
          ))}
        </div>

        <div className="status">
          {healthError ? (
            <Pill on={false} label="Backend offline" title={healthError.message} />
          ) : (
            <>
              <Pill on={health?.llm_enabled ?? false} label="LLM" title={health?.llm_enabled ? 'OpenRouter enabled' : 'OPENROUTER_API_KEY not configured'} />
              {health?.default_model ? (
                <span className="model-tag" title={`Default model: ${health.default_model}`}>
                  {shortModel(health.default_model)}
                </span>
              ) : null}
            </>
          )}
          <Pill
            on={stream === 'open'}
            label={stream === 'open' ? 'Live' : stream === 'connecting' ? 'Connecting…' : 'Disconnected'}
            title="SSE /api/stream"
          />
          {health?.last_ingest_at ? (
            <span className="last-ingest" title="Last fetch">
              ⟳ <TimeAgo iso={health.last_ingest_at} />
            </span>
          ) : null}
          <UiButton className="ingest-btn" tone="primary" size="sm" onClick={onIngest} disabled={ingesting}>
            {ingesting ? <Spinner label="Fetching…" /> : 'Fetch now'}
          </UiButton>
        </div>

        <SettingsMenu />
      </div>
    </header>
  )
}
