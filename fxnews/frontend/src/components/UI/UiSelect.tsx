import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

import { cn } from '~/lib/utils'

import type { SelectOption } from './types'

export type UiSelectSize = 'xs' | 'sm' | 'md' | 'lg'

type UiSelectProps = {
  value: string
  options: SelectOption[]
  onChange: (value: string) => void
  ariaLabel: string
  size?: UiSelectSize
  className?: string
  disabled?: boolean
  /** Show a filter box at the top of the menu (for long lists). */
  searchable?: boolean
  /** Dim the trigger text (e.g. a placeholder-like "default" choice is selected). */
  muted?: boolean
}

type MenuPosition = {
  top: number
  left: number
  width: number
  maxHeight: number
}

const MENU_GAP = 6
const MENU_MAX_HEIGHT = 320
const VIEWPORT_MARGIN = 12

const triggerSizeClassMap: Record<UiSelectSize, string> = {
  xs: 'px-2 py-1.5 pr-6 text-[calc(10px+var(--font-size-offset))] bg-[length:4px_4px,4px_4px] bg-[position:calc(100%-12px)_50%,calc(100%-8px)_50%]',
  sm: 'px-2.5 py-2 pr-7 text-[calc(11px+var(--font-size-offset))] bg-[length:5px_5px,5px_5px] bg-[position:calc(100%-14px)_50%,calc(100%-10px)_50%]',
  md: 'px-3 py-2.5 pr-8 text-[calc(12px+var(--font-size-offset))] bg-[length:6px_6px,6px_6px] bg-[position:calc(100%-16px)_50%,calc(100%-11px)_50%]',
  lg: 'px-3.5 py-3.5 pr-9 text-[calc(13px+var(--font-size-offset))] bg-[length:7px_7px,7px_7px] bg-[position:calc(100%-18px)_50%,calc(100%-12px)_50%]',
}

const optionSizeClassMap: Record<UiSelectSize, string> = {
  xs: 'px-1.5 py-1 text-[calc(10px+var(--font-size-offset))]',
  sm: 'px-2 py-1.5 text-[calc(11px+var(--font-size-offset))]',
  md: 'px-2.5 py-2 text-[calc(12px+var(--font-size-offset))]',
  lg: 'px-3 py-2.5 text-[calc(13px+var(--font-size-offset))]',
}

const markerSizeClassMap: Record<UiSelectSize, string> = {
  xs: 'w-1.5',
  sm: 'w-2',
  md: 'w-2.5',
  lg: 'w-3',
}

export const UiSelect = ({ value, options, onChange, ariaLabel, size = 'md', className, disabled = false, searchable = false, muted = false }: UiSelectProps) => {
  const [query, setQuery] = useState('')
  const [isOpen, setIsOpen] = useState(false)
  const [menuPosition, setMenuPosition] = useState<MenuPosition | null>(null)
  const selectRef = useRef<HTMLDivElement | null>(null)
  const menuRef = useRef<HTMLDivElement | null>(null)
  const triggerRef = useRef<HTMLButtonElement | null>(null)
  const activeRef = useRef<HTMLButtonElement | null>(null)

  const open = isOpen && !disabled

  useEffect(() => {
    if (!open) {
      return
    }

    const updateMenuPosition = () => {
      if (!triggerRef.current) {
        return
      }

      const rect = triggerRef.current.getBoundingClientRect()
      const below = window.innerHeight - rect.bottom - MENU_GAP - VIEWPORT_MARGIN
      const above = rect.top - MENU_GAP - VIEWPORT_MARGIN
      // Open upward when there is clearly more room above than below.
      const openUp = below < 160 && above > below
      const maxHeight = Math.max(120, Math.min(MENU_MAX_HEIGHT, openUp ? above : below))

      setMenuPosition({
        top: openUp ? Math.max(VIEWPORT_MARGIN, rect.top - MENU_GAP - maxHeight) : rect.bottom + MENU_GAP,
        left: rect.left,
        width: rect.width,
        maxHeight,
      })
    }

    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node
      const isInsideTrigger = selectRef.current?.contains(target)
      const isInsideMenu = menuRef.current?.contains(target)

      if (!isInsideTrigger && !isInsideMenu) {
        setIsOpen(false)
      }
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setIsOpen(false)
        triggerRef.current?.focus()
      }
    }

    // Scrolling the menu itself must not re-position (or close) it.
    const handleScroll = (event: Event) => {
      if (menuRef.current?.contains(event.target as Node)) {
        return
      }
      updateMenuPosition()
    }

    updateMenuPosition()

    window.addEventListener('pointerdown', handlePointerDown)
    window.addEventListener('keydown', handleKeyDown)
    window.addEventListener('resize', updateMenuPosition)
    window.addEventListener('scroll', handleScroll, true)

    return () => {
      window.removeEventListener('pointerdown', handlePointerDown)
      window.removeEventListener('keydown', handleKeyDown)
      window.removeEventListener('resize', updateMenuPosition)
      window.removeEventListener('scroll', handleScroll, true)
    }
  }, [open])

  // Bring the selected option into view when the menu opens.
  useEffect(() => {
    if (open && menuPosition) {
      activeRef.current?.scrollIntoView({ block: 'nearest' })
    }
  }, [open, menuPosition])

  const q = query.trim().toLowerCase()
  const shown = searchable && q
    ? options.filter((o) => `${o.label} ${o.value} ${o.group ?? ''}`.toLowerCase().includes(q))
    : options
  const pick = (v: string) => {
    onChange(v)
    setIsOpen(false)
    triggerRef.current?.focus()
  }

  const selected = options.find((option) => option.value === value)
  const selectedLabel = selected?.label ?? value

  return (
    <div className={cn('ui-selectbox relative z-[120] min-w-0', open && 'is-open z-[1300]', className)} ref={selectRef}>
      <button
        ref={triggerRef}
        type="button"
        disabled={disabled}
        title={selected?.title ?? selectedLabel}
        className={cn(
          'ui-selectbox-trigger block w-full truncate border border-[var(--accent-dim)] bg-[var(--bg-card)] bg-[linear-gradient(45deg,transparent_50%,var(--accent)_50%),linear-gradient(135deg,var(--accent)_50%,transparent_50%)] bg-no-repeat text-left font-[var(--mono)] uppercase tracking-[0.16em] transition-[border-color,box-shadow,background-color] duration-200 enabled:hover:border-[var(--accent)] enabled:hover:bg-[var(--bg-card-hover)] enabled:hover:shadow-[0_0_10px_var(--accent-glow),inset_0_0_10px_var(--accent-glow)] disabled:cursor-not-allowed disabled:opacity-50',
          muted ? 'text-[var(--text-dim)]' : 'text-[var(--text-primary)]',
          triggerSizeClassMap[size],
        )}
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => { setQuery(''); setIsOpen((prev) => !prev) }}
      >
        {selectedLabel}
      </button>

      {open &&
        menuPosition &&
        typeof document !== 'undefined' &&
        createPortal(
          <div
            ref={menuRef}
            className="ui-selectbox-menu z-[1310] flex flex-col overflow-y-auto overscroll-contain border border-[var(--accent-dim)] bg-[linear-gradient(180deg,var(--bg-card),var(--bg-base))] shadow-[0_0_10px_var(--accent-glow),0_14px_28px_rgba(2,8,16,0.45)]"
            role="listbox"
            aria-label={ariaLabel}
            style={{
              position: 'fixed',
              top: menuPosition.top,
              left: menuPosition.left,
              minWidth: menuPosition.width,
              maxWidth: `min(420px, calc(100vw - ${VIEWPORT_MARGIN * 2}px))`,
              maxHeight: menuPosition.maxHeight,
            }}
          >
            {searchable ? (
              <div className="sticky top-0 z-[2] border-b border-[var(--accent-dim)] bg-[var(--bg-base)] p-1.5">
                <input
                  autoFocus
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      const first = shown.find((o) => !o.disabled)
                      if (first) pick(first.value)
                    }
                  }}
                  placeholder="Search…"
                  aria-label={`Search ${ariaLabel}`}
                  className="w-full border border-[var(--border)] bg-[var(--bg-input)] px-2 py-1 font-[var(--mono)] text-[calc(11px+var(--font-size-offset))] text-[var(--text-primary)] outline-none focus:border-[var(--accent-dim)]"
                />
              </div>
            ) : null}
            {searchable && shown.length === 0 ? (
              <div className="px-2 py-2 font-[var(--mono)] text-[calc(10px+var(--font-size-offset))] uppercase tracking-[0.16em] text-[var(--text-dim)]">No matches</div>
            ) : null}
            {shown.map((option, index) => {
              const isActive = option.value === value
              const showGroup = option.group !== undefined && option.group !== shown[index - 1]?.group

              return (
                <div key={option.value} className="contents">
                  {showGroup ? (
                    <div className={cn(searchable ? 'static' : 'sticky top-0 z-[1]', 'border-b border-[var(--table-border-soft)] bg-[var(--bg-base)] px-2 py-1 text-[calc(9px+var(--font-size-offset))] uppercase tracking-[0.24em] text-[var(--text-dim)]')}>
                      {option.group}
                    </div>
                  ) : null}
                  <button
                    ref={isActive ? activeRef : undefined}
                    type="button"
                    role="option"
                    aria-selected={isActive}
                    title={option.title}
                    disabled={option.disabled}
                    className={cn(
                      'ui-selectbox-option flex w-full items-center gap-2 border-b border-[var(--table-border-soft)] bg-transparent text-left font-[var(--mono)] uppercase tracking-[0.16em] text-[var(--text-secondary)] transition-colors duration-150 last:border-b-0 hover:bg-[var(--table-row-hover)] hover:text-[var(--text-primary)]',
                      optionSizeClassMap[size],
                      isActive && 'is-active bg-[var(--accent-glow)] text-[var(--accent)]',
                      option.disabled && 'cursor-not-allowed opacity-40 hover:bg-transparent hover:text-[var(--text-secondary)]',
                    )}
                    onClick={() => pick(option.value)}
                  >
                    <span className={cn('ui-selectbox-marker shrink-0 text-[var(--accent)]', markerSizeClassMap[size])}>{isActive ? '>' : ''}</span>
                    <span className="min-w-0 truncate">{option.label}</span>
                  </button>
                </div>
              )
            })}
          </div>,
          document.body,
        )}
    </div>
  )
}
