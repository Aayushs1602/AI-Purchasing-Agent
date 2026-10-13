import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The UI calls /api/...; in development Vite forwards it to the FastAPI backend.
// API_TARGET points it at a backend on another port (default: localhost:8000).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': process.env.API_TARGET ?? 'http://localhost:8000',
    },
  },
})
