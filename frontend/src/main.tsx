import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { RouterProvider, createBrowserRouter } from 'react-router-dom'
import App from './App'
import { ErrorBoundary, ToastHost } from './components/ui'
import './index.css'

// data router：离开前确认（lib/leave）靠它的 useBlocker 才拦得住 navigate() 和浏览器
// 后退。路由表仍在 App 里用 <Routes> 写，这里只挂一条通配。
// ErrorBoundary 兜住外壳自己（导航、信号）出的错：页面的错 App 里那一层先接住；
// 不兜的话落到 react-router 默认的英文报错页
const router = createBrowserRouter([
  {
    path: '*',
    element: (
      <ToastHost>
        <ErrorBoundary>
          <App />
        </ErrorBoundary>
      </ToastHost>
    ),
  },
])

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>,
)
