import { useEffect, useRef, useState } from 'react'

import { FONT_FAMILY_CONFIG, fontFamilyKeys, type FontFamilyKey } from '~/config/font-family-config'
import { FONT_SIZE_CONFIG, fontSizeKeys, type FontSizeKey } from '~/config/font-size-config'
import { THEME_CONFIG, themeKeys, type ThemeKey } from '~/config/theme-config'
import { useLinkPreviewSettings } from '~/context/linkPreview'
import { useAppearance } from '~/lib/appearance'

import { UiButton } from './UI/UiButton'
import { UiSelect } from './UI/UiSelect'

const THEME_OPTIONS = themeKeys.map((key) => ({ value: key, label: THEME_CONFIG.themes[key].label }))
const FONT_OPTIONS = fontFamilyKeys.map((key) => ({ value: key, label: FONT_FAMILY_CONFIG.families[key].label }))

/** Theme / font size / font family dropdown (same controls as the main NewsTrade header). */
export function SettingsMenu() {
  const { theme, setTheme, fontSize, setFontSize, fontFamily, setFontFamily } = useAppearance()
  const linkPreview = useLinkPreviewSettings()
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onPointerDown = (e: PointerEvent) => {
      // The select menus are portalled to <body>, so ignore clicks inside them too.
      const target = e.target as Element
      if (rootRef.current?.contains(target) || target.closest('.ui-selectbox-menu')) return
      setOpen(false)
    }
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    window.addEventListener('pointerdown', onPointerDown)
    window.addEventListener('keydown', onKeyDown)
    return () => {
      window.removeEventListener('pointerdown', onPointerDown)
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  return (
    <div className="app-settings" ref={rootRef}>
      <UiButton tone={open ? 'primary' : 'ghost'} size="sm" onClick={() => setOpen((v) => !v)} aria-expanded={open} aria-haspopup="true">
        Settings
      </UiButton>

      <div className={`app-settings-panel${open ? ' is-open' : ''}`}>
        <div className="app-setting-row">
          <span className="app-setting-label">Theme</span>
          <UiSelect value={theme} ariaLabel="Theme setting" size="sm" options={THEME_OPTIONS} onChange={(v) => setTheme(v as ThemeKey)} />
        </div>

        <div className="app-setting-row">
          <span className="app-setting-label">Font size</span>
          <div className="app-font-size-group" role="group" aria-label="Font size setting">
            {fontSizeKeys.map((key: FontSizeKey) => (
              <UiButton key={key} tone={fontSize === key ? 'primary' : 'ghost'} size="xs" onClick={() => setFontSize(key)}>
                {FONT_SIZE_CONFIG.sizes[key].label}
              </UiButton>
            ))}
          </div>
        </div>

        <div className="app-setting-row">
          <span className="app-setting-label">Font</span>
          <UiSelect
            value={fontFamily}
            ariaLabel="Font family setting"
            size="sm"
            options={FONT_OPTIONS}
            onChange={(v) => setFontFamily(v as FontFamilyKey)}
          />
        </div>

        <div className="app-setting-row">
          <span className="app-setting-label">Link previews</span>
          <div className="app-font-size-group" role="group" aria-label="Link preview setting">
            <UiButton tone={linkPreview.enabled ? 'primary' : 'ghost'} size="xs" onClick={() => linkPreview.setEnabled(true)}>
              On
            </UiButton>
            <UiButton tone={linkPreview.enabled ? 'ghost' : 'primary'} size="xs" onClick={() => linkPreview.setEnabled(false)}>
              Off
            </UiButton>
          </div>
        </div>
      </div>
    </div>
  )
}
