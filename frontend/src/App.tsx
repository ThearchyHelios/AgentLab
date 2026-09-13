import { useEffect } from 'react'
import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { BookOpen, FlaskConical, History, Settings, Wrench } from 'lucide-react'
import clsx from 'clsx'
import { api } from './api/client'
import { useCatalog } from './store/catalog'
import { StudioPage } from './pages/StudioPage'
import { RunsPage } from './pages/RunsPage'
import { ToolsPage } from './pages/ToolsPage'
import { KnowledgePage } from './pages/KnowledgePage'
import { SettingsPage, applyTheme } from './pages/SettingsPage'
import { Spinner } from './components/ui'

const NAV = [
  { to: '/studio', label: '编排', icon: FlaskConical },
  { to: '/runs', label: '运行', icon: History },
  { to: '/tools', label: '工具', icon: Wrench },
  { to: '/knowledge', label: '知识', icon: BookOpen },
  { to: '/settings', label: '设置', icon: Settings },
]

export default function App() {
  const { loaded, refresh, refreshApprovals, approvals } = useCatalog()

  useEffect(() => {
    void refresh()
    void api.settings.get().then((s) => applyTheme(s?.ui?.theme ?? 'system')).catch(() => {})
  }, [refresh])

  // 待审批是全局待办：别的页面也要能看到有东西等着人处理
  useEffect(() => {
    const timer = setInterval(() => void refreshApprovals(), 4000)
    return () => clearInterval(timer)
  }, [refreshApprovals])

  const pending = approvals.filter((a) => a.status === 'pending').length

  if (!loaded) {
    return (
      <div className="flex h-full items-center justify-center gap-2 text-dim">
        <Spinner size={16} /> 正在连接后端…
      </div>
    )
  }

  return (
    <div className="flex h-full">
      <nav className="flex w-14 shrink-0 flex-col items-center gap-1 border-r bg-panel py-2">
        <div className="mb-2 flex h-8 w-8 items-center justify-center rounded-lg bg-[var(--accent)] text-[13px] font-bold text-white">
          A
        </div>
        {NAV.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            className={({ isActive }) =>
              clsx(
                'relative flex w-11 flex-col items-center gap-0.5 rounded-lg py-2 text-[9.5px] transition-colors',
                isActive ? 'bg-hover text-fg' : 'text-faint hover:bg-hover hover:text-dim',
              )
            }
          >
            <item.icon size={15} />
            {item.label}
            {item.to === '/runs' && pending > 0 && (
              <span className="absolute right-1.5 top-1 flex h-3.5 min-w-3.5 items-center justify-center rounded-full bg-[var(--warn)] px-1 text-[8.5px] font-bold text-black">
                {pending}
              </span>
            )}
          </NavLink>
        ))}
      </nav>

      <main className="min-w-0 flex-1">
        <Routes>
          <Route path="/" element={<Navigate to="/studio" replace />} />
          <Route path="/studio" element={<StudioPage />} />
          <Route path="/runs" element={<RunsPage />} />
          <Route path="/tools" element={<ToolsPage />} />
          <Route path="/knowledge" element={<KnowledgePage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Routes>
      </main>
    </div>
  )
}
