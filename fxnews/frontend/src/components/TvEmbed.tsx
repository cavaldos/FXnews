import { useEffect, useRef } from 'react'
import { useChartTheme } from '../lib/theme'
import type { TvScript } from '../lib/tvWidgets'

interface Props {
  /** One of TV_SCRIPT. */
  script: TvScript
  /**
   * Widget options. `colorTheme`, `isTransparent`, `locale` and 100% sizing are filled in here;
   * pass a new object only when the options really change (they are compared by JSON).
   */
  config: Record<string, unknown>
  className?: string
  /** Blend the widget's opaque black (dark) / white (light) cells into the theme surface (Forex Cross Rates). */
  blendSurface?: boolean
}

/**
 * Generic TradingView free embed (same mechanism as TradingViewWidget): drops the widget div + the
 * `external-embedding` script into a container and re-creates it when the options or the app theme change.
 * Transparent by default, so the widget shows the frosted panel behind it.
 */
export function TvEmbed({ script, config, className, blendSurface }: Props) {
  const hostRef = useRef<HTMLDivElement>(null)
  const { light } = useChartTheme()
  const payload = JSON.stringify(config)
  // An explicit colorTheme in config wins, so the app theme must not rebuild the widget then.
  const colorTheme = typeof config.colorTheme === 'string' ? config.colorTheme : light ? 'light' : 'dark'

  useEffect(() => {
    const host = hostRef.current
    if (!host) return
    // Each run owns its wrapper; cleanup removes only that wrapper, never the host's children.
    const wrapper = document.createElement('div')
    wrapper.className = 'tradingview-widget-container__widget'
    wrapper.style.height = '100%'
    wrapper.style.width = '100%'
    host.appendChild(wrapper)
    const el = document.createElement('script')
    el.src = `https://s3.tradingview.com/external-embedding/${script}`
    el.type = 'text/javascript'
    el.async = true
    el.text = JSON.stringify({
      width: '100%',
      height: '100%',
      locale: 'en',
      isTransparent: true,
      colorTheme,
      ...JSON.parse(payload),
    })
    // Next tick: the embed script reads its own parent when it runs (StrictMode / fast switching safe).
    const timer = setTimeout(() => wrapper.appendChild(el), 0)
    return () => {
      clearTimeout(timer)
      wrapper.remove()
    }
  }, [script, payload, colorTheme])

  // Pin the embedding element to `color-scheme: light`: the widget iframe's document declares no color-scheme
  // (= light), and when the embedder's scheme differs Chrome paints an opaque white backdrop behind the
  // transparent iframe (even for colorTheme 'dark'). The scheme only affects UA chrome, not the widget's colours.
  return (
    <div
      className={`tradingview-widget-container tv-embed${className ? ` ${className}` : ''}${
        blendSurface ? ` tv-blend-${colorTheme === 'light' ? 'light' : 'dark'}` : ''
      }`}
      style={{ colorScheme: 'light' }}
      ref={hostRef}
    />
  )
}
