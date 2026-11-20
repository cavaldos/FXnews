import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import { LinkPreviewProvider } from './components/LinkPreviewProvider'
import { applyStoredAppearance } from './lib/appearance'
import './style/global.css'

// Apply the saved theme before the first render so the chart picks up the right colours (no flash).
applyStoredAppearance()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <LinkPreviewProvider>
      <App />
    </LinkPreviewProvider>
  </StrictMode>,
)
