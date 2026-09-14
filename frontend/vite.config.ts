import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// 后端端口跟着 dev.sh 走：8000 被别的项目占了它会自动挪，挪到哪就从
// 环境变量告诉这边，否则前端会把请求代理到别人的服务上去。
const API_PORT = process.env.AGENTLAB_PORT || '8000'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // 5173 太容易和别的项目撞车，挪开一点
    port: 5273,
    proxy: {
      // 前端开发服务器代理到后端，省掉跨域配置
      '/api': { target: `http://127.0.0.1:${API_PORT}`, changeOrigin: true, ws: true },
    },
  },
})
