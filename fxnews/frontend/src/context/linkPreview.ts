import { createContext, useContext } from 'react'

export interface LinkPreviewSettings {
  /** Hover previews of external links on/off (persisted). */
  enabled: boolean
  setEnabled: (v: boolean) => void
}

export const LinkPreviewContext = createContext<LinkPreviewSettings>({ enabled: false, setEnabled: () => {} })

export function useLinkPreviewSettings(): LinkPreviewSettings {
  return useContext(LinkPreviewContext)
}
