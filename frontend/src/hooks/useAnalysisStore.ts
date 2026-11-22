import { useCallback, useRef, useState } from 'react'
import { api } from '../api/client'
import type { AnalysisOut } from '../api/types'

export interface AnalysisStore {
  analyses: Record<number, AnalysisOut>
  /** Insert / replace analyses (from SSE, POST responses, ...). */
  add: (items: AnalysisOut[]) => void
  /** Fetch analyses by id unless already known / in flight. */
  ensure: (ids: number[]) => void
}

/** Client-side cache of AnalysisOut keyed by id. */
export function useAnalysisStore(): AnalysisStore {
  const [analyses, setAnalyses] = useState<Record<number, AnalysisOut>>({})
  const knownRef = useRef(new Set<number>())

  const add = useCallback((items: AnalysisOut[]) => {
    if (items.length === 0) return
    for (const a of items) knownRef.current.add(a.id)
    setAnalyses((prev) => {
      const next = { ...prev }
      for (const a of items) next[a.id] = a
      return next
    })
  }, [])

  const ensure = useCallback(
    (ids: number[]) => {
      for (const id of ids) {
        if (knownRef.current.has(id)) continue
        knownRef.current.add(id)
        api.analysis(id).then(
          (a) => add([a]),
          () => knownRef.current.delete(id), // allow retry on the next call
        )
      }
    },
    [add],
  )

  return { analyses, add, ensure }
}
