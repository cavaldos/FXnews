import { useCallback, useState } from 'react'

function read(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

/** String preference persisted in localStorage (silently in-memory when storage is unavailable). */
export function useStoredString(key: string, fallback: string): [string, (v: string) => void] {
  const [value, setValue] = useState(() => read(key) ?? fallback)
  const set = useCallback(
    (v: string) => {
      setValue(v)
      try {
        localStorage.setItem(key, v)
      } catch {
        /* ignore */
      }
    },
    [key],
  )
  return [value, set]
}

export function useStoredBool(key: string, fallback: boolean): [boolean, (v: boolean) => void] {
  const [value, setValue] = useStoredString(key, fallback ? '1' : '0')
  return [value === '1', (v: boolean) => setValue(v ? '1' : '0')]
}
