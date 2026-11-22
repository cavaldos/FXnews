import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'

const srcDir = fileURLToPath(new URL('./src', import.meta.url))

// Backend FastAPI. Override with `FXNEWS_API=http://host:port npm run dev` if needed.
const target = process.env.FXNEWS_API ?? 'http://127.0.0.1:8000'

// `/api` (incl. the SSE stream at `/api/stream`) is proxied to the backend.
const proxy = {
  '/api': { target, changeOrigin: true },
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: [
      { find: /^~\//, replacement: srcDir + '/' },
      { find: '~', replacement: srcDir },
    ],
  },
  server: { port: 5173, proxy },
  preview: { port: 4173, proxy },
})
