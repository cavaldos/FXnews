export type Page = 'dashboard' | 'calendar' | 'backtest' | 'llm' | 'vibe' | 'memory' | 'data' | 'notifications'

export const PAGES: { id: Page; label: string }[] = [
  { id: 'dashboard', label: 'Dashboard' },
  { id: 'calendar', label: 'Calendar' },
  { id: 'backtest', label: 'Backtest' },
  { id: 'llm', label: 'LLM Lab' },
  { id: 'vibe', label: 'Vibe Lab' },
  { id: 'memory', label: 'Memory' },
  { id: 'data', label: 'Data' },
  { id: 'notifications', label: 'Notifications' },
]

export function parsePage(v: string): Page {
  return PAGES.some((p) => p.id === v) ? (v as Page) : 'dashboard'
}
