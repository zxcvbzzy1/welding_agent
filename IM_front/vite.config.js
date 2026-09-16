import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import vueDevTools from 'vite-plugin-vue-devtools'

// https://vite.dev/config/
export default defineConfig({
  server: {
    proxy: { '/api': { target: process.env.IM_API_PROXY_TARGET || 'http://127.0.0.1:8010', changeOrigin: false } },
  },
  preview: {
    proxy: { '/api': { target: process.env.IM_API_PROXY_TARGET || 'http://127.0.0.1:8010', changeOrigin: false } },
  },
  plugins: [
    vue(),
    vueDevTools(),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url))
    },
  },
  server:{
    host:'0.0.0.0',
    port:8000,
  }
})
