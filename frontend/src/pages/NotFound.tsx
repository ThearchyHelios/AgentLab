import { Link, useLocation, useNavigate } from 'react-router-dom'
import { ArrowLeft, Search } from 'lucide-react'
import { Kbd } from '../components/ui'
import { openCommandPalette } from '../components/CommandPalette'
import { usePageTitle } from '../lib/notify'

/**
 * 未知地址。之前路由表没有兜底，打错地址或点了过期链接看到的是一整块空白，
 * 导航也不高亮任何一项——不知道是页面坏了、还是自己走错了。
 */
export function NotFound() {
  const { pathname, search } = useLocation()
  const navigate = useNavigate()
  usePageTitle('页面不存在')
  // 从站外直接打开的过期链接没有「上一页」可回
  const canGoBack = typeof window !== 'undefined' && (window.history.state?.idx ?? 0) > 0

  return (
    <div className="flex h-full items-center justify-center overflow-auto p-8">
      <div className="flex max-w-md flex-col items-center text-center">
        <BrokenRoute />
        <div className="mono mt-5 text-2xs tracking-[0.2em] text-faint">404 · NOT FOUND</div>
        <h1 className="mt-1.5 text-xl font-semibold text-fg">这个地址不存在</h1>
        <p className="mt-2 text-sm leading-relaxed text-dim">链接可能已经过期，或者地址打错了。</p>
        <code
          className="mono mt-3 max-w-full truncate rounded-md border bg-elev px-2 py-1 text-xs text-dim"
          title={pathname + search}
        >
          {pathname}{search}
        </code>
        <div className="mt-6 flex flex-wrap items-center justify-center gap-2">
          <Link to="/chat" className="btn btn-primary">回到问数据</Link>
          <button type="button" className="btn" onClick={openCommandPalette}>
            <Search size={13} aria-hidden /> 搜索去处 <Kbd combo="Mod+K" className="ml-1" />
          </button>
          {canGoBack && (
            <button type="button" className="btn btn-ghost" onClick={() => navigate(-1)}>
              <ArrowLeft size={13} aria-hidden /> 返回上一页
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

/**
 * 走线断在半路：和品牌标同一种正交折线，终点那个节点是虚线框——
 * 画布上「未到达」就是这副样子。静态，不做动画。
 */
function BrokenRoute() {
  return (
    <svg width="148" height="64" viewBox="0 0 148 64" fill="none" aria-hidden className="text-faint">
      <rect x="6" y="22" width="20" height="20" rx="4" stroke="var(--accent)" strokeWidth="1.75" fill="var(--accent-soft)" />
      <path d="M26 32H58V14H84" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
      <rect x="84" y="4" width="20" height="20" rx="4" stroke="currentColor" strokeWidth="1.75" />
      <path d="M58 32V50H78" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeDasharray="3 4" />
      <rect x="116" y="40" width="20" height="20" rx="4" stroke="var(--st-unreached, currentColor)" strokeWidth="1.5" strokeDasharray="3 3" />
      <path d="M122 46l8 8M130 46l-8 8" stroke="var(--st-unreached, currentColor)" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  )
}
