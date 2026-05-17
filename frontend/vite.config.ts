import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const BACKEND = 'http://localhost:8769'

const proxyConfig = {
  '/api': {
    target: BACKEND,
    changeOrigin: true,
    rewrite: (path: string) => path.replace(/^\/api/, ''),
  },
}

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: proxyConfig,
  },
  preview: {
    port: 5174,
    proxy: proxyConfig,
  },
})
