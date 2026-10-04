import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 产物用相对路径，便于被 FastAPI 从任意挂载点直接托管。
export default defineConfig({
  plugins: [vue()],
  base: './',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // 面板是本地工具，不需要为老浏览器降级
    target: 'es2022',
  },
  server: {
    port: 5173,
    // `npm run dev` 时把接口请求转发给 Python 后端
    proxy: {
      '/api': { target: 'http://127.0.0.1:8787', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8787', ws: true },
    },
  },
})
