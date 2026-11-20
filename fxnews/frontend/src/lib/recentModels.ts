// Recently used LLM models ("provider:model"), shared by the LLM Lab and the Backtest page (same localStorage key).
import { useCallback, useEffect, useState } from 'react'

export const MAX_RECENT = 8
const KEY = 'llm.recent'

export function readRecent(): string[] {
  try {
    const v: unknown = JSON.parse(localStorage.getItem(KEY) || '[]')
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string' && x.includes(':')).slice(0, MAX_RECENT) : []
  } catch {
    return []
  }
}

function writeRecent(list: string[]) {
  try {
    localStorage.setItem(KEY, JSON.stringify(list))
  } catch {
    /* storage unavailable */
  }
}

/** Move `specs` to the front (the first spec ends up first), dropping duplicates and capping at MAX_RECENT. */
export function pushRecent(list: string[], specs: string[]): string[] {
  const valid = specs.filter((s) => s.includes(':'))
  return [...valid, ...list.filter((x) => !valid.includes(x))].filter((x, i, a) => a.indexOf(x) === i).slice(0, MAX_RECENT)
}

/** Recent models, persisted on every change. */
export function useRecentModels() {
  const [recent, setRecent] = useState<string[]>(readRecent)
  useEffect(() => writeRecent(recent), [recent])
  const push = useCallback((specs: string[]) => setRecent((prev) => pushRecent(prev, specs)), [])
  const clear = useCallback(() => setRecent([]), [])
  return { recent, push, clear }
}
