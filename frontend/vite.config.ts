import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { defineConfig } from 'vite'

const srcDir = fileURLToPath(new URL('./src', import.meta.url))

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: [
      { find: /^~\//, replacement: srcDir + '/' },
      { find: '~', replacement: srcDir },
      { find: '@components', replacement: path.resolve(srcDir, 'components') },
      { find: '@pages', replacement: path.resolve(srcDir, 'pages') },
      { find: '@routes', replacement: path.resolve(srcDir, 'routes') },
      { find: '@types', replacement: path.resolve(srcDir, 'types') },
      { find: '@assets', replacement: path.resolve(srcDir, 'assets') },
    ],
  },
})
