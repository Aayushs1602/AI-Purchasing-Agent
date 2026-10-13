import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The UI calls /api/...; in development Vite forwards it to the FastAPI backend.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
})
