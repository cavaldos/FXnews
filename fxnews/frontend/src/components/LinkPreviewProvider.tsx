import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { api, isAbortError } from '../api/client'
import type { LinkPreviewOut } from '../api/types'
import { LinkPreviewContext } from '../context/linkPreview'
import { useStoredBool } from '../hooks/useLocalStorage'
import { fmtDateTime, toMs } from '../lib/format'
import '../style/link-preview.css'

const OPEN_DELAY_MS = 400
const CLOSE_GRACE_MS = 250
const CARD_W = 380
const CARD_H = 420
const GAP = 10
const MARGIN = 12

interface Target {
  url: string
  rect: DOMRect
}

/** Preview target for an element: an external http(s) link, unless opted out or it is a symbol/chart control. */
function targetOf(el: Element | null): { el: Element; url: string } | null {
  const host = el?.closest('a[href], [data-preview-url]')
  if (!host || host.closest('[data-preview="off"], .link-preview-card')) return null
  if (el!.closest('.symbol-link')) return null // SymbolLink opens a chart on click, never a page preview
  const raw = host.getAttribute('data-preview-url') ?? (host as HTMLAnchorElement).href
  try {
    const u = new URL(raw, location.href)
    if ((u.protocol !== 'http:' && u.protocol !== 'https:') || u.origin === location.origin) return null
    return { el: host, url: u.href }
  } catch {
    return null
  }
}

const cache = new Map<string, LinkPreviewOut>()

/**
 * Global "hover any external link -> floating page preview". Uses delegated pointer listeners, so any
 * `<a href="http...">` (or element with `data-preview-url`) works with no per-component wiring.
 * Opt out with `data-preview="off"` on the link or an ancestor. Mouse only; the setting is persisted.
 */
export function LinkPreviewProvider({ children }: { children: ReactNode }) {
  const [enabled, setEnabled] = useStoredBool('fxnews.linkPreview', true)
  const [target, setTarget] = useState<Target | null>(null)
  const openTimer = useRef<number | undefined>(undefined)
  const closeTimer = useRef<number | undefined>(undefined)
  const anchor = useRef<Element | null>(null)
  const settings = useMemo(() => ({ enabled, setEnabled }), [enabled, setEnabled])

  const hide = useCallback(() => {
    window.clearTimeout(openTimer.current)
    window.clearTimeout(closeTimer.current)
    anchor.current = null
    setTarget(null)
  }, [])

  useEffect(() => {
    if (!enabled || !window.matchMedia('(hover: hover) and (pointer: fine)').matches) return
    const scheduleClose = () => {
      window.clearTimeout(openTimer.current)
      window.clearTimeout(closeTimer.current)
      closeTimer.current = window.setTimeout(hide, CLOSE_GRACE_MS)
    }
    const onOver = (e: PointerEvent) => {
      if (e.pointerType !== 'mouse' || !(e.target instanceof Element)) return
      if (e.target.closest('.link-preview-card')) {
        window.clearTimeout(closeTimer.current)
        return
      }
      const t = targetOf(e.target)
      if (!t) return
      window.clearTimeout(closeTimer.current)
      if (anchor.current === t.el) return
      window.clearTimeout(openTimer.current)
      anchor.current = t.el
      openTimer.current = window.setTimeout(() => setTarget({ url: t.url, rect: t.el.getBoundingClientRect() }), OPEN_DELAY_MS)
    }
    const onOut = (e: PointerEvent) => {
      if (!(e.target instanceof Element)) return
      const to = e.relatedTarget instanceof Element ? e.relatedTarget : null
      if (e.target.closest('.link-preview-card')) {
        if (!to?.closest('.link-preview-card') && !(anchor.current && to && anchor.current.contains(to))) scheduleClose()
        return
      }
      const a = anchor.current
      if (a && a.contains(e.target) && !(to && (a.contains(to) || to.closest('.link-preview-card')))) scheduleClose()
    }
    const onDown = (e: PointerEvent) => {
      if (e.target instanceof Element && !e.target.closest('.link-preview-card')) hide()
    }
    const onScroll = (e: Event) => {
      if (!(e.target instanceof Element && e.target.closest('.link-preview-card'))) hide()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') hide()
    }
    document.addEventListener('pointerover', onOver)
    document.addEventListener('pointerout', onOut)
    document.addEventListener('pointerdown', onDown, true)
    window.addEventListener('scroll', onScroll, true)
    window.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerover', onOver)
      document.removeEventListener('pointerout', onOut)
      document.removeEventListener('pointerdown', onDown, true)
      window.removeEventListener('scroll', onScroll, true)
      window.removeEventListener('keydown', onKey)
      hide()
    }
  }, [enabled, hide])

  return (
    <LinkPreviewContext.Provider value={settings}>
      {children}
      {enabled && target ? <PreviewCard key={target.url} target={target} /> : null}
    </LinkPreviewContext.Provider>
  )
}

function placement(rect: DOMRect): React.CSSProperties {
  const vw = window.innerWidth
  const vh = window.innerHeight
  const width = Math.min(CARD_W, vw - MARGIN * 2)
  const left = Math.max(MARGIN, Math.min(rect.left, vw - width - MARGIN))
  const below = vh - rect.bottom - GAP - MARGIN
  const above = rect.top - GAP - MARGIN
  if (below >= CARD_H || below >= above) {
    return { left, width, top: rect.bottom + GAP, maxHeight: Math.min(CARD_H, Math.max(below, 160)) }
  }
  return { left, width, bottom: vh - rect.top + GAP, maxHeight: Math.min(CARD_H, Math.max(above, 160)) }
}

function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, '')
  } catch {
    return url
  }
}

function PreviewCard({ target }: { target: Target }) {
  const { url, rect } = target
  const [data, setData] = useState<LinkPreviewOut | null>(() => cache.get(url) ?? null)
  const [failed, setFailed] = useState(false)
  const [live, setLive] = useState(false)
  const [imgOk, setImgOk] = useState(true)
  const [iconOk, setIconOk] = useState(true)
  const style = useMemo(() => placement(rect), [rect])

  useEffect(() => {
    if (cache.has(url)) return
    const ctl = new AbortController()
    api
      .linkPreview(url, ctl.signal)
      .then((d) => {
        if (d.ok) cache.set(url, d)
        setData(d)
      })
      .catch((err) => {
        if (!isAbortError(err)) setFailed(true)
      })
    return () => ctl.abort()
  }, [url])

  const unavailable = failed || (data !== null && !data.ok)
  const pubMs = toMs(data?.published)
  const open = (
    <a className="lp-open" href={url} target="_blank" rel="noopener noreferrer" data-preview="off">
      Open ↗
    </a>
  )

  return (
    <div className={`link-preview-card${live ? ' is-live' : ''}`} style={style} role="dialog" aria-label="Link preview">
      {unavailable ? (
        <div className="lp-body lp-error">
          <div className="lp-site">{hostOf(url)}</div>
          <p>Preview unavailable — open in new tab</p>
          <div className="lp-foot">{open}</div>
        </div>
      ) : !data ? (
        <div className="lp-body" aria-busy="true" aria-label="Loading preview">
          <div className="lp-skel lp-skel-img" />
          <div className="lp-skel lp-skel-line" style={{ width: '40%' }} />
          <div className="lp-skel lp-skel-line" style={{ width: '90%' }} />
          <div className="lp-skel lp-skel-line" style={{ width: '75%' }} />
          <div className="lp-skel lp-skel-line" />
          <div className="lp-skel lp-skel-line" style={{ width: '60%' }} />
        </div>
      ) : (
        <>
          {live ? (
            <iframe
              className="lp-frame"
              src={data.final_url || url}
              title={data.title || 'Live page'}
              sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
              referrerPolicy="no-referrer"
              loading="lazy"
            />
          ) : (
            <div className="lp-body">
              {data.image && imgOk ? <img className="lp-img" src={data.image} alt="" referrerPolicy="no-referrer" onError={() => setImgOk(false)} /> : null}
              <div className="lp-site">
                {data.favicon && iconOk ? <img src={data.favicon} alt="" width={14} height={14} referrerPolicy="no-referrer" onError={() => setIconOk(false)} /> : null}
                <span>{data.site_name || hostOf(url)}</span>
                {pubMs ? <span className="lp-time">· {fmtDateTime(pubMs)}</span> : null}
              </div>
              <h4 className="lp-title">{data.title || hostOf(url)}</h4>
              {data.author ? <div className="lp-author">By {data.author}</div> : null}
              {data.excerpt ? <div className="lp-excerpt">{data.excerpt}</div> : null}
            </div>
          )}
          <div className="lp-foot">
            {data.frameable ? (
              <button type="button" className="lp-live" aria-pressed={live} onClick={() => setLive((v) => !v)}>
                {live ? 'Reader' : 'Live'}
              </button>
            ) : null}
            <span className="lp-spacer" />
            {open}
          </div>
        </>
      )}
    </div>
  )
}
