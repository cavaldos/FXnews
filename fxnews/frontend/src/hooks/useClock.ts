import { useSyncExternalStore } from 'react'

/** Shared tickers: one setInterval per resolution no matter how many components subscribe. */
function createClock(ms: number) {
  let now = Date.now()
  let timer: ReturnType<typeof setInterval> | undefined
  const subs = new Set<() => void>()
  return {
    subscribe(cb: () => void) {
      subs.add(cb)
      if (subs.size === 1) {
        now = Date.now()
        timer = setInterval(() => {
          now = Date.now()
          subs.forEach((s) => s())
        }, ms)
      }
      return () => {
        subs.delete(cb)
        if (subs.size === 0 && timer) clearInterval(timer)
      }
    },
    getSnapshot: () => now,
  }
}

const slow = createClock(30_000)
const fast = createClock(1_000)

/** Current time in ms, refreshed every 30s (relative timestamps). */
export function useNowSlow(): number {
  return useSyncExternalStore(slow.subscribe, slow.getSnapshot)
}

/** Current time in ms, refreshed every second (countdowns, request timers). */
export function useNowFast(): number {
  return useSyncExternalStore(fast.subscribe, fast.getSnapshot)
}
