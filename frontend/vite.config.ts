import { defineConfig, searchForWorkspaceRoot } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// 后端端口跟着 dev.sh 走：8000 被别的项目占了它会自动挪，挪到哪就从
// 环境变量告诉这边，否则前端会把请求代理到别人的服务上去。
const API_PORT = process.env.AGENTLAB_PORT || '8000'

// 下面两个只给检查用的隔离环境（scripts/check-all.mjs --stacks）设，平时不设、行为不变：
// - AGENTLAB_VITE_NO_WATCH=1：不监听文件、不开热更新。边改代码边跑检查时，热更新会把页面
//   整个重载、或者把 store 模块换掉，检查就偶发失败
// - AGENTLAB_VITE_CACHE_DIR：依赖预构建的缓存放到这里（每套一份）。关了热更新，React 插件就不挂
//   Fast Refresh，预构建的指纹跟着变：要是和平时的开发服务器共用 node_modules/.vite，它会把那边
//   的缓存整个重建一遍。所以关热更新时缓存一定另放，没给目录就放 node_modules/.vite-no-watch
const NO_WATCH = process.env.AGENTLAB_VITE_NO_WATCH === '1'
const CACHE_DIR = process.env.AGENTLAB_VITE_CACHE_DIR || (NO_WATCH ? 'node_modules/.vite-no-watch' : '')

export default defineConfig({
  plugins: [react(), tailwindcss()],
  ...(CACHE_DIR ? { cacheDir: CACHE_DIR } : {}),
  server: {
    // 5173 太容易和别的项目撞车，挪开一点
    port: 5273,
    ...(NO_WATCH ? { watch: null, hmr: false } : {}),
    // 缓存放到项目外面（/tmp 下）时要放行它：默认只放行项目目录
    ...(CACHE_DIR ? { fs: { allow: [searchForWorkspaceRoot(process.cwd()), CACHE_DIR] } } : {}),
    proxy: {
      // 前端开发服务器代理到后端，省掉跨域配置
      '/api': { target: `http://127.0.0.1:${API_PORT}`, changeOrigin: true, ws: true },
    },
  },
})
