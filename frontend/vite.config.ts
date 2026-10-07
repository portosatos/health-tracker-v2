import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // cookie-сессия и SSE идут через тот же origin (:5173) → без CORS
      '/api': { target: 'http://localhost:8000', changeOrigin: false, ws: false },
    },
  },
  test: { environment: 'node', include: ['src/**/*.test.ts'] },
})
