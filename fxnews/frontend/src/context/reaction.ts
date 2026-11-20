import { createContext, useContext } from 'react'

export interface ReactionPanelRequest {
  /** Event time, epoch ms. */
  time: number
  /** Symbols to check (the event's related symbols). */
  symbols: string[]
  title?: string
}

export interface ReactionPanelApi {
  /** Open the reaction panel for an event: horizons x symbols. */
  open: (req: ReactionPanelRequest) => void
  /** Open the manual tool: any symbol + date/time. */
  openChecker: () => void
}

export const ReactionPanelContext = createContext<ReactionPanelApi>({ open: () => {}, openChecker: () => {} })

export function useReactionPanel(): ReactionPanelApi {
  return useContext(ReactionPanelContext)
}
