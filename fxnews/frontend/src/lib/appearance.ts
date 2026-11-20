import { useEffect, useState } from 'react'

import { FONT_FAMILY_CONFIG, FONT_FAMILY_STORAGE_KEY, applyFontFamily, normalizeFontFamily, type FontFamilyKey } from '~/config/font-family-config'
import { FONT_SIZE_CONFIG, FONT_SIZE_STORAGE_KEY, applyFontSize, normalizeFontSize, type FontSizeKey } from '~/config/font-size-config'
import { THEME_CONFIG, THEME_STORAGE_KEY, applyTheme, normalizeTheme, type ThemeKey } from '~/config/theme-config'

function read(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

function write(key: string, value: string) {
  try {
    localStorage.setItem(key, value)
  } catch {
    /* storage unavailable: the choice just doesn't persist */
  }
}

/** Expose the active theme key as data-theme on <html>, for the few CSS rules that need a per-theme override. */
function markTheme(theme: ThemeKey) {
  document.documentElement.dataset.theme = theme
}

/** Apply the saved theme / font family / font size. Call once before the first render to avoid a flash of the default theme. */
export function applyStoredAppearance() {
  const theme = normalizeTheme(read(THEME_STORAGE_KEY))
  applyTheme(theme)
  markTheme(theme)
  applyFontFamily(normalizeFontFamily(read(FONT_FAMILY_STORAGE_KEY)))
  applyFontSize(normalizeFontSize(read(FONT_SIZE_STORAGE_KEY)))
}

export interface Appearance {
  theme: ThemeKey
  setTheme: (theme: ThemeKey) => void
  fontSize: FontSizeKey
  setFontSize: (size: FontSizeKey) => void
  fontFamily: FontFamilyKey
  setFontFamily: (family: FontFamilyKey) => void
}

/** Theme / font settings state, persisted to localStorage and applied to :root. */
export function useAppearance(): Appearance {
  const [theme, setTheme] = useState<ThemeKey>(() => normalizeTheme(read(THEME_STORAGE_KEY) ?? THEME_CONFIG.defaultTheme))
  const [fontSize, setFontSize] = useState<FontSizeKey>(() => normalizeFontSize(read(FONT_SIZE_STORAGE_KEY) ?? FONT_SIZE_CONFIG.defaultSize))
  const [fontFamily, setFontFamily] = useState<FontFamilyKey>(() =>
    normalizeFontFamily(read(FONT_FAMILY_STORAGE_KEY) ?? FONT_FAMILY_CONFIG.defaultFamily),
  )

  useEffect(() => {
    applyTheme(theme)
    markTheme(theme)
    write(THEME_STORAGE_KEY, theme)
  }, [theme])

  useEffect(() => {
    applyFontSize(fontSize)
    write(FONT_SIZE_STORAGE_KEY, fontSize)
  }, [fontSize])

  useEffect(() => {
    applyFontFamily(fontFamily)
    write(FONT_FAMILY_STORAGE_KEY, fontFamily)
  }, [fontFamily])

  return { theme, setTheme, fontSize, setFontSize, fontFamily, setFontFamily }
}
