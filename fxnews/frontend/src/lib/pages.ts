export type Page = 'dashboard' | 'calendar' | 'backtest' | 'llm' | 'data'

export const PAGES: { id: Page; label: string }[] = [
  { id: 'dashboard', label: 'Dashboard' },
  { id: 'calendar', label: 'Calendar' },
  { id: 'backtest', label: 'Backtest' },
  { id: 'llm', label: 'LLM Lab' },
  { id: 'data', label: 'Data' },
]

export function parsePage(v: string): Page {
  return PAGES.some((p) => p.id === v) ? (v as Page) : 'dashboard'
}
