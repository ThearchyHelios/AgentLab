import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // 5173 太容易和别的项目撞车，挪开一点
    port: 5273,
    proxy: {
      // 后端跑在 8000，前端开发服务器代理过去，省掉跨域配置
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true, ws: true },
    },
  },
})
