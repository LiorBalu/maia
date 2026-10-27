import { fileURLToPath, URL } from 'node:url'

import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Dev server: the UI is served on 5173 and every /api and /management call is
// proxied to the FastAPI backend on 8000, so the app can use relative URLs.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    port: 5173,
    strictPort: false,
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true, ws: true },
      '/management': { target: 'http://localhost:8000', changeOrigin: true, ws: true },
    },
  },
})
