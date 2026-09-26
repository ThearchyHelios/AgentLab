import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import type { ReactNode, RefObject } from 'react'
import { Link, NavLink, Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { Bell, BellOff, Moon, RotateCw, Sun, UserRound } from 'lucide-react'
import clsx from 'clsx'
import { ApiError, NETWORK_MESSAGE, api } from './api/client'
import { useCatalog, useOnReconnect } from './store/catalog'
import type { CatalogCheck } from './store/catalog'
import { useStudio } from './store/studio'
import { useChat } from './store/chat'
import { useConversations } from './store/conversations'
import { ChatPage } from './pages/ChatPage'
import { StudioPage } from './pages/StudioPage'
import { RunsPage } from './pages/RunsPage'
import { DataPage } from './pages/DataPage'
import { ToolsPage } from './pages/ToolsPage'
import { KnowledgePage } from './pages/KnowledgePage'
import { SettingsPage } from './pages/SettingsPage'
import { NotFound } from './pages/NotFound'
import { Logo } from './components/Logo'
import {
  CommandPalette, PAGES, ShortcutHelp, flushThemePref, openShortcutHelp, toggleCommandPalette, toggleNotify,
  toggleTheme, useTheme, waitedFor,
} from './components/CommandPalette'
import { ErrorBoundary, Kbd, OfflineBanner, Spinner, StatusBadge, isComposing, toast } from './components/ui'
import { applyTheme, normalizeTheme } from './lib/theme'
import { ariaShortcut, isTypingTarget, matchShortcut } from './lib/keys'
import { formatDateTime, formatDuration, formatRelative, shortId } from './lib/format'
import { humanizeError } from './lib/errors'
import { isActivePhase } from './run/trace'
import {
  composeTitle, flagAttention, notifyInBackground, notifySupported, paintFavicon, useSignals,
} from './lib/notify'
import type { Attention, FaviconDot } from './lib/notify'

export default function App() {
  const loaded = useCatalog((s) => s.loaded)
  const backend = useCatalog((s) => s.backend)
  const everOk = useCatalog((s) => s.lastOkAt != null)
  const allFailed = useCatalog((s) => s.checks.length > 0 && s.checks.every((c) => c.state === 'error'))
  const location = useLocation()
  const [offlineOk, setOfflineOk] = useState(false)

  useEffect(() => { void useCatalog.getState().refresh() }, [])
  // 连着时每 4 秒拉一次待审批，顺带当心跳；断开后的退避重试由 catalog 自己排
  useEffect(() => useCatalog.getState().startHeartbeat(), [])

  // 主题的真源在后端设置里。启动时没连上就等恢复再读一次，不然这次会话一直用缓存。
  // 读到过、或者人在这台设备上换过（导航、⌘K 都派发 agentlab:theme），就不再读，
  // 路上的那一次读回来也不应用：断线时换的主题不能在重连时被服务端的旧值翻回去
  const themeSettled = useRef(false)
  const loadTheme = () => {
    if (themeSettled.current) return
    api.settings.get().then((s) => {
      if (themeSettled.current) return
      themeSettled.current = true
      applyTheme(normalizeTheme(s?.ui?.theme))
    }).catch(() => {})
  }
  useEffect(loadTheme, [])
  useEffect(() => {
    const onLocal = () => { themeSettled.current = true }
    window.addEventListener('agentlab:theme', onLocal)
    return () => window.removeEventListener('agentlab:theme', onLocal)
  }, [])
  // 断线时换的那一下当时没存上：恢复后补存，而不是去读服务端
  useOnReconnect(() => { if (!flushThemePref()) loadTheme() })

  useBootReport(loaded)
  // 在 App 这一层算，从启动那一刻起计时：从启动页「先进去看看」时已经卡了好几秒，
  // 进门那一下就得是琥珀，不能进了门再从头等
  const stalled = useStalledChecks()

  // 从没连上过就断着：停在启动页说清楚原因，而不是放人进去看一屏「还没有…」
  const offlineAtBoot = !everOk && (backend === 'down' || (allFailed && backend === 'checking'))
  const booting = !offlineOk && (!loaded || offlineAtBoot)
  // 启动页的清单自己会说哪几项还在等；进了外壳才需要当面提醒
  useStallWarning(booting ? NO_CHECKS : stalled)
  if (booting) return <BootScreen onEnterOffline={() => setOfflineOk(true)} />

  return (
    <div className="flex h-full">
      <Nav stalled={stalled} />

      <main className="flex min-w-0 flex-1 flex-col">
        {/* 横幅必须是 main 的第一个孩子：它把自己的高度写给 toast，toast 才会跟着下移 */}
        <OfflineBanner />
        <div className="relative min-h-0 flex-1">
          {/* 「在看哪个」属于 URL，不属于 store。
              每页写成两条显式路由（带 id / 不带 id），而不是可选参数 `:id?`：
              「没带 id」是真要单独处理的一种情况（落到上次那个、或列表第一个），
              藏在一个问号里下次就没人记得它存在了。
              导航高亮不用动——NavLink 默认前缀匹配，/chat/abc 照样点亮「问数据」。*/}
          <ErrorBoundary resetKey={location.pathname}>
            <Routes>
              {/* 默认落到对话页：多数人要的是答案，画布留给要自己编排的人 */}
              <Route path="/" element={<Navigate to="/chat" replace />} />
              <Route path="/chat" element={<ChatPage />} />
              <Route path="/chat/:conversationId" element={<ChatPage />} />
              <Route path="/studio" element={<StudioPage />} />
              <Route path="/studio/:workflowId" element={<StudioPage />} />
              <Route path="/runs" element={<RunsPage />} />
              <Route path="/runs/:runId" element={<RunsPage />} />
              <Route path="/data" element={<DataPage />} />
              <Route path="/data/:tab" element={<DataPage />} />
              <Route path="/tools" element={<ToolsPage />} />
              <Route path="/tools/:tab" element={<ToolsPage />} />
              <Route path="/tools/:tab/:id" element={<ToolsPage />} />
              <Route path="/knowledge" element={<KnowledgePage />} />
              <Route path="/knowledge/:tab" element={<KnowledgePage />} />
              <Route path="/knowledge/:tab/:id" element={<KnowledgePage />} />
              {/* 数据源搬到了顶级「数据」页；旧书签和各处「去设置 → 数据源」的链接还在 */}
              <Route path="/settings/datasources" element={<Navigate to="/data" replace />} />
              <Route path="/settings" element={<SettingsPage />} />
              <Route path="/settings/:tab" element={<SettingsPage />} />
              <Route path="*" element={<NotFound />} />
            </Routes>
          </ErrorBoundary>
        </div>
      </main>

      <CommandPalette />
      <ShortcutHelp />
      <ShellKeys />
      <ShellSignals />
    </div>
  )
}

// -------------------------------------------------------------------------
// 全局快捷键
// -------------------------------------------------------------------------

/** 启动页上不挂：那时按下的 ⌘K 会在加载完之后才突然弹出面板 */
function ShellKeys() {
  const navigate = useNavigate()
  const navRef = useRef(navigate)
  navRef.current = navigate

  useEffect(() => {
    // 捕获阶段注册：比各页面自己挂在 window 上的监听先到，? 只弹一层
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || isComposing(e)) return
      // 别的弹窗开着时键盘归它：在弹窗上再叠一层命令面板，Esc 该关谁就乱了
      const modal = document.querySelector('[aria-modal="true"]:not([data-command-palette])')
      if (matchShortcut(e, 'Mod+K')) {
        if (modal) return
        e.preventDefault()
        // 按住不放会连发：面板开了又关、关了又开
        if (!e.repeat) toggleCommandPalette()
        return
      }
      // 其余的都让给打字：输入框里的 ? 是问号，⌥1 在 Mac 上是「¡」
      if (modal || document.querySelector('[data-command-palette]') || isTypingTarget(e.target)) return
      if (matchShortcut(e, '?')) {
        e.preventDefault()
        e.stopPropagation()
        openShortcutHelp()
        return
      }
      const page = PAGES.find((p) => matchShortcut(e, p.shortcut))
      if (page) {
        e.preventDefault()
        navRef.current(page.to)
      }
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [])
  return null
}

// -------------------------------------------------------------------------
// 导航
// -------------------------------------------------------------------------

/** 悬停提示：挂在导航右侧，读屏不念（导航项自己有文字，提示只是补充） */
function Tip({ children, badge = false }: { children: ReactNode; badge?: boolean }) {
  return (
    <span
      aria-hidden
      className={clsx(
        'pointer-events-none invisible absolute left-full top-1/2 z-40 ml-2.5 flex -translate-y-1/2 items-center gap-2',
        'whitespace-nowrap rounded-md border bg-elev px-2 py-1 text-xs font-normal text-fg opacity-0 shadow-elev-2',
        'transition-opacity duration-150',
        badge
          ? 'group-hover/badge:visible group-hover/badge:opacity-100 group-focus-visible/badge:visible group-focus-visible/badge:opacity-100'
          : 'group-hover:visible group-hover:opacity-100 group-hover:delay-300 group-focus-visible:visible group-focus-visible:opacity-100',
      )}
    >
      {children}
    </span>
  )
}

/** 有没有运行在跑：品牌标的起点节点亮起、标题加前缀都看它 */
function useStudioActivity(): string {
  return useStudio((s) => {
    if (s.runPhase === 'running' || s.runPhase === 'queued') {
      const total = s.trace.nodesTotal ?? s.nodes.length
      let done = 0
      for (const n of Object.values(s.trace.nodes)) if (n.state === 'done' || n.state === 'skipped') done++
      return total ? `run:${Math.min(done, total)}/${total}` : 'run'
    }
    return s.copilot.active ? 'gen' : ''
  })
}

function useChatActivity(): '' | 'run' | 'gen' {
  return useChat((s) => {
    let gen = false
    for (const turns of Object.values(s.byConversation)) {
      for (const t of turns) {
        if (t.phase === 'running') return 'run'
        if (t.phase === 'planning' || t.phase === 'building') gen = true
      }
    }
    return gen ? 'gen' : ''
  })
}

function Nav({ stalled }: { stalled: CatalogCheck[] }) {
  const studio = useStudioActivity()
  const chat = useChatActivity()
  const live = studio.startsWith('run') || chat === 'run'

  // 整条导航约 520px 高，窗口再矮，最底下的遥测点和署名就被切掉、够不着。矮窗口
  // （≤600px）按高度收紧到 450px 左右，而不是让导航滚动：导航一旦 overflow，
  // 挂在右侧的悬停提示和遥测浮层也会被一起裁掉
  return (
    <nav aria-label="主导航" className="relative z-30 flex w-16 shrink-0 flex-col border-r bg-panel">
      <Link
        to="/"
        aria-label={live ? 'AgentLab 首页（有运行在跑）' : 'AgentLab 首页'}
        className="group relative mx-auto mb-2 mt-3 flex h-10 w-10 shrink-0 items-center justify-center rounded-lg outline-none hover:bg-hover focus-visible:ring-2 focus-visible:ring-[var(--accent)] [@media(max-height:600px)]:mb-1 [@media(max-height:600px)]:mt-2 [@media(max-height:600px)]:h-9"
      >
        <Logo size={22} live={live} className="text-accent" title="" />
        <Tip>
          <span className="font-semibold">AgentLab</span>
          <span className="text-faint">受限动态编排{live ? ' · 有运行在跑' : ''}</span>
        </Tip>
      </Link>
      <div aria-hidden className="mx-4 mb-2 h-px shrink-0 bg-hairline [@media(max-height:600px)]:mb-1" />

      <ul className="flex flex-col gap-0.5">
        {PAGES.map((p) => (
          <li key={p.to} className="relative px-1">
            <NavLink
              to={p.to}
              aria-keyshortcuts={ariaShortcut(p.shortcut)}
              className={({ isActive }) => clsx(
                'group relative flex h-12 w-full flex-col items-center justify-center gap-1 rounded-md text-2xs leading-none outline-none',
                '[@media(max-height:600px)]:h-10',
                'transition-colors focus-visible:ring-2 focus-visible:ring-[var(--accent)]',
                isActive ? 'bg-hover font-medium text-fg' : 'text-dim hover:bg-hover hover:text-fg',
              )}
            >
              {({ isActive }) => (
                <>
                  {/* 选中态三条通道：左缘竖条、图标变强调色、底色——只靠淡灰底根本认不出在哪一页 */}
                  {isActive && <span aria-hidden className="absolute -left-1 bottom-2 top-2 w-0.5 rounded-r bg-accent" />}
                  <p.icon size={17} strokeWidth={isActive ? 2 : 1.75} className={isActive ? 'text-accent' : undefined} />
                  <span>{p.label}</span>
                  <Tip>
                    <span className="font-medium">{p.label}</span>
                    <span className="text-faint">{p.hint}</span>
                    <Kbd combo={p.shortcut} />
                  </Tip>
                </>
              )}
            </NavLink>
            {p.to === '/runs' && <ApprovalBadge />}
          </li>
        ))}
      </ul>

      <div className="mt-auto flex flex-col items-center gap-1.5 pb-2 pt-3 [@media(max-height:600px)]:gap-1 [@media(max-height:600px)]:pb-1 [@media(max-height:600px)]:pt-2">
        <div className="flex items-center gap-0.5">
          <NotifyToggle />
          <ThemeToggle />
        </div>
        <ActorButton />
        <div aria-hidden className="h-px w-8 bg-hairline" />
        <Telemetry stalled={stalled} />
      </div>
    </nav>
  )
}

/**
 * 待审批徽标。
 *
 * 它是一个单独的链接（直达记录页的待审批页签），不是「记录」的一部分：导航
 * 项本身永远进 /runs，否则同一个入口时而进列表、时而进某条详情，人就摸不准
 * 点下去会到哪。挂在图标右上角外侧，外圈一道面板底色的描边把它从图标上抠开；
 * 浅色下用 warn-solid 配 on-warn，不再是黑字压在深琥珀上糊成一团。
 */
function ApprovalBadge() {
  const approvals = useCatalog((s) => s.approvals)
  const pending = approvals.filter((a) => a.status === 'pending')
  if (!pending.length) return null
  const oldest = pending.reduce((a, b) => ((a.created_at ?? '') <= (b.created_at ?? '') ? a : b))
  const waited = waitedFor(oldest.created_at)
  const text = `${pending.length} 项待审批${waited ? `，最久已等 ${waited}` : ''}`
  return (
    <Link
      to="/runs?tab=approvals"
      aria-label={`${text}。打开待审批`}
      data-approval-badge=""
      className={clsx(
        'group/badge absolute left-[calc(50%+7px)] top-[3px] z-10 flex h-4 min-w-4 items-center justify-center rounded-full px-1',
        '[@media(max-height:600px)]:top-0',
        'bg-warn-solid text-[10px] font-semibold leading-none text-on-warn tnum ring-2 ring-[var(--bg-panel)]',
        'outline-none focus-visible:ring-[var(--accent)]',
      )}
    >
      {pending.length > 99 ? '99+' : pending.length}
      <Tip badge>
        <span className="font-medium">{text}</span>
        <span className="text-faint">点开去处理</span>
      </Tip>
    </Link>
  )
}

function NotifyToggle() {
  const notify = useSignals((s) => s.notify)
  const permission = useSignals((s) => s.permission)
  if (!notifySupported()) return null
  const Icon = notify ? Bell : BellOff
  const tip = notify
    ? '后台提醒：开 · 运行结束、失败或来了新的待审批会发系统通知'
    : permission === 'denied'
      ? '后台提醒：浏览器拒绝了通知权限，要先在站点设置里允许'
      : '后台提醒：关 · 点开后浏览器会询问通知权限'
  return (
    <button
      type="button"
      aria-label="后台提醒"
      aria-pressed={notify}
      onClick={() => void toggleNotify()}
      className={clsx(
        'group relative flex h-8 w-7 items-center justify-center rounded-md outline-none hover:bg-hover [@media(max-height:600px)]:h-7',
        'focus-visible:ring-2 focus-visible:ring-[var(--accent)]',
        notify ? 'text-accent' : 'text-faint hover:text-fg',
      )}
    >
      <Icon size={15} aria-hidden />
      <Tip>{tip}</Tip>
    </button>
  )
}

function ThemeToggle() {
  const { pref, resolved } = useTheme()
  const now = resolved === 'dark' ? '深色' : '浅色'
  const next = resolved === 'dark' ? '浅色' : '深色'
  const Icon = resolved === 'dark' ? Moon : Sun
  return (
    <button
      type="button"
      aria-label={`切换到${next}主题`}
      onClick={toggleTheme}
      className="group relative flex h-8 w-7 items-center justify-center rounded-md text-faint outline-none hover:bg-hover hover:text-fg focus-visible:ring-2 focus-visible:ring-[var(--accent)] [@media(max-height:600px)]:h-7"
    >
      <Icon size={15} aria-hidden />
      <Tip>
        切换到{next}主题
        <span className="text-faint">当前{pref === 'system' ? `跟随系统（${now}）` : now}</span>
      </Tip>
    </button>
  )
}

const ACTOR_KEY = 'agentlab_actor'

function readActor(): string {
  try {
    return localStorage.getItem(ACTOR_KEY)?.trim() ?? ''
  } catch {
    return ''
  }
}

function subscribeActor(cb: () => void): () => void {
  // 设置页在同一个标签页里改署名不会触发 storage 事件；它若派发 agentlab:actor
  // 就立刻跟上，否则下一次导航重绘时也会重新读到
  const events = ['storage', 'agentlab:actor', 'focus']
  events.forEach((e) => window.addEventListener(e, cb))
  return () => events.forEach((e) => window.removeEventListener(e, cb))
}

/** 操作者署名的首字：发布、审批、正式运行都记在这个名下，得一直看得见是谁 */
function ActorButton() {
  const actor = useSyncExternalStore(subscribeActor, readActor, () => '')
  useLocation()
  const initial = actor ? Array.from(actor)[0].toUpperCase() : ''
  return (
    <Link
      to="/settings/prefs"
      aria-label={actor ? `署名：${actor}。去设置修改` : '还没有署名。去设置填写'}
      className={clsx(
        'group relative flex h-7 w-7 items-center justify-center rounded-full border text-xs font-semibold outline-none',
        'hover:border-[var(--border-strong)] hover:bg-hover focus-visible:ring-2 focus-visible:ring-[var(--accent)]',
        actor ? 'bg-elev text-fg' : 'border-dashed text-faint',
      )}
    >
      {initial || <UserRound size={13} aria-hidden />}
      <Tip>
        {actor ? <span>署名 <span className="font-medium">{actor}</span></span> : <span>未署名</span>}
        <span className="text-faint">发布、审批、正式运行记在这个名下 · 点开修改</span>
      </Tip>
    </Link>
  )
}

/** 延迟超过这个数就算慢：本机和内网通常是几十毫秒 */
const SLOW_MS = 1500

interface Tone { color: string; text: string; label: string; word: string }

/**
 * 五档：离线（红）、加载中（琥珀：连得上，但请求卡着没回来）、降级（琥珀：连得上，
 * 但有几张表报错）、偏慢（琥珀）、在线（绿）。加载中和降级单列出来：那几张表对应
 * 的下拉和列表是空的，不说就像"没有"
 */
function backendTone(backend: string, latency: number | null, failing: number, stalled: number): Tone {
  if (backend === 'down') return { color: 'var(--st-failed)', text: '离线', label: '后端未连接', word: '未连接' }
  if (backend === 'checking') return { color: 'var(--text-faint)', text: '检测中', label: '正在检测后端', word: '检测中' }
  if (stalled > 0) {
    return { color: 'var(--st-waiting)', text: '加载中', label: `后端在线，但还有 ${stalled} 项没取回来`, word: '加载中' }
  }
  if (failing > 0) {
    return { color: 'var(--st-waiting)', text: '降级', label: `后端在线，但有 ${failing} 项没加载成功`, word: '降级' }
  }
  if (latency != null && latency >= SLOW_MS) {
    return { color: 'var(--st-waiting)', text: `${latency}ms`, label: `后端响应慢，延迟 ${latency} 毫秒`, word: '偏慢' }
  }
  return {
    color: 'var(--st-done)',
    text: latency != null ? `${latency}ms` : '在线',
    label: latency != null ? `后端在线，延迟 ${latency} 毫秒` : '后端在线',
    word: '在线',
  }
}

const useFailingChecks = () => useCatalog((s) => s.checks.filter((c) => c.state === 'error').length)

const NO_CHECKS: CatalogCheck[] = []

/** 请求发出去这么久还没回来，就不算「还在加载」，算卡住了 */
const STALL_MS = 3000

/**
 * 卡着没回来的 catalog 请求（后端没断时）。
 *
 * refresh 是一个 Promise.all：一张表卡住（后端连得上、就是不回），六张表全都还是
 * 空的，backend 却是 ok——遥测点一片绿，编排页一屏「还没有工作流 / 新建工作流」，
 * 正是离线横幅要消灭的那种假空态，只是换了个来路。正常刷新几十毫秒就回，不到
 * STALL_MS 不算，免得每次刷新遥测点都闪一下琥珀。
 */
function useStalledChecks(): CatalogCheck[] {
  const waiting = useCatalog((s) => s.checks.some((c) => c.state === 'pending'))
  const down = useCatalog((s) => s.backend === 'down')
  const checks = useCatalog((s) => s.checks)
  const [stalled, setStalled] = useState(false)
  useEffect(() => {
    if (!waiting) return
    const t = setTimeout(() => setStalled(true), STALL_MS)
    return () => { clearTimeout(t); setStalled(false) }
  }, [waiting])
  // 断开时归离线横幅说，这里不再叠一层
  return stalled && waiting && !down ? checks.filter((c) => c.state === 'pending') : NO_CHECKS
}

/**
 * 进了外壳还有请求卡着：挂一条常驻提示，点名是哪几项；全部回来（或者断开，归
 * 横幅管）就撤掉。遥测点只是一个小琥珀点，人眼前是「还没有工作流」和一个「新建」
 * 按钮，得当面说一句。点名的是卡住那一刻的几项，实时的看遥测浮层。
 */
function useStallWarning(stalled: CatalogCheck[]) {
  const any = stalled.length > 0
  const latest = useRef(stalled)
  latest.current = stalled
  useEffect(() => {
    if (!any) return
    const labels = latest.current.map((c) => c.label)
    const id = toast.warn(`还有 ${labels.length} 项没取回来（${labels.join('、')}）：列表可能不全，先别新建`, {
      sticky: true,
      key: 'boot-pending',
      detail: '后端连得上，但这几个请求发出去好几秒了还没回来。在它们回来之前，工作流、知识库这些列表可能是空的，不代表数据没了。导航最底下的连接指示灯里能看到哪一项回来了。',
      action: { label: '重试', onClick: () => void useCatalog.getState().refresh() },
    })
    return () => toast.dismiss(id)
  }, [any])
}

/** 连接遥测点：导航最底下，像仪表盘上的指示灯。点开看地址、延迟、最近一次错误 */
function Telemetry({ stalled }: { stalled: CatalogCheck[] }) {
  const backend = useCatalog((s) => s.backend)
  const latency = useCatalog((s) => s.latencyMs)
  const failing = useFailingChecks()
  const [open, setOpen] = useState(false)
  const btnRef = useRef<HTMLButtonElement>(null)
  const tone = backendTone(backend, latency, failing, stalled.length)
  return (
    <div className="relative">
      <button
        ref={btnRef}
        type="button"
        aria-label={`${tone.label}。查看连接详情`}
        aria-expanded={open}
        aria-haspopup="dialog"
        data-telemetry={backend}
        data-tone={tone.word}
        onClick={() => setOpen((o) => !o)}
        className={clsx(
          'group relative flex h-6 w-14 items-center justify-center gap-1.5 rounded-md text-2xs outline-none',
          'hover:bg-hover focus-visible:ring-2 focus-visible:ring-[var(--accent)]',
          backend === 'down' ? 'text-[var(--st-failed)]' : 'text-faint hover:text-dim',
        )}
      >
        <span aria-hidden className="h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: tone.color }} />
        <span className="mono tnum">{tone.text}</span>
        {!open && <Tip>{tone.label}<span className="text-faint">点开看详情</span></Tip>}
      </button>
      {open && (
        <TelemetryPanel
          stalled={stalled}
          anchor={btnRef}
          onClose={(refocus) => { setOpen(false); if (refocus) btnRef.current?.focus() }}
        />
      )}
    </div>
  )
}

function useTicker(active: boolean, ms = 1000): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    setNow(Date.now())
    const t = setInterval(() => setNow(Date.now()), ms)
    return () => clearInterval(t)
  }, [active, ms])
  return now
}

function TelemetryPanel({ onClose, anchor, stalled }: {
  onClose: (refocus: boolean) => void
  anchor: RefObject<HTMLButtonElement | null>
  stalled: CatalogCheck[]
}) {
  const backend = useCatalog((s) => s.backend)
  const latency = useCatalog((s) => s.latencyMs)
  const lastOkAt = useCatalog((s) => s.lastOkAt)
  const backendError = useCatalog((s) => s.backendError)
  const retryAt = useCatalog((s) => s.retryAt)
  const checks = useCatalog((s) => s.checks)
  const [busy, setBusy] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const down = backend === 'down'
  const now = useTicker(down)
  const tone = backendTone(backend, latency, useFailingChecks(), stalled.length)

  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose
  useEffect(() => { ref.current?.focus() }, [])
  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node
      if (!ref.current?.contains(t) && !anchor.current?.contains(t)) onCloseRef.current(false)
    }
    window.addEventListener('mousedown', onDown)
    return () => window.removeEventListener('mousedown', onDown)
  }, [anchor])

  const secs = down && retryAt ? Math.max(0, Math.ceil((retryAt - now) / 1000)) : null
  const retry = async () => {
    setBusy(true)
    try {
      const ok = await useCatalog.getState().checkBackend()
      if (ok && !down) await useCatalog.getState().refresh()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      ref={ref}
      role="dialog"
      aria-label="后端连接"
      tabIndex={-1}
      onKeyDown={(e) => {
        if (e.key === 'Escape' && !isComposing(e)) {
          e.stopPropagation()
          onClose(true)
        }
      }}
      className="fade-up absolute bottom-0 left-full z-40 ml-2.5 w-80 rounded-lg border bg-panel text-xs shadow-elev-3 outline-none"
    >
      <div className="flex items-center justify-between border-b px-3 py-2">
        <span className="font-semibold text-fg">后端连接</span>
        <span className="flex items-center gap-1.5" style={{ color: tone.color }}>
          <span aria-hidden className="h-1.5 w-1.5 rounded-full" style={{ background: tone.color }} />
          {tone.word}
        </span>
      </div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 px-3 py-2.5">
        <dt className="text-faint">地址</dt>
        <dd className="mono truncate text-dim" title={`${window.location.origin}/api`}>{window.location.origin}/api</dd>
        <dt className="text-faint">延迟</dt>
        <dd className="mono tnum text-dim">{latency != null ? `${latency} ms` : '—'}</dd>
        <dt className="text-faint">最后连通</dt>
        <dd className="text-dim" title={lastOkAt ? formatDateTime(lastOkAt) : undefined}>
          {lastOkAt ? formatRelative(lastOkAt) : '—'}
        </dd>
        {secs != null && (
          <>
            <dt className="text-faint">自动重试</dt>
            <dd className="tnum text-dim">{secs} 秒后</dd>
          </>
        )}
      </dl>
      {backendError && (
        <div className="mx-3 mb-2.5 rounded-md border px-2 py-1.5 text-2xs leading-relaxed"
             style={{ borderColor: 'color-mix(in srgb, var(--err) 35%, var(--border))', background: 'var(--st-failed-soft)' }}>
          <div className="text-[var(--st-failed)]">最近一次错误</div>
          <div className="mono mt-0.5 break-all text-dim">{backendError}</div>
        </div>
      )}
      {stalled.length > 0 && (
        <div className="mx-3 mb-2.5 rounded-md border px-2 py-1.5 text-2xs leading-relaxed"
             style={{ borderColor: 'color-mix(in srgb, var(--warn) 35%, var(--border))', background: 'var(--st-waiting-soft)' }}>
          <div className="text-[var(--st-waiting)]">{stalled.map((c) => c.label).join('、')}还没回来</div>
          <div className="mt-0.5 text-dim">在那之前各页的列表可能不全，不代表数据没了，先别急着新建。</div>
        </div>
      )}
      {checks.length > 0 && (
        <div className="border-t px-3 py-2">
          <div className="mb-1 text-2xs text-faint">最近一次全量加载</div>
          <CheckList checks={checks} />
        </div>
      )}
      <div className="flex justify-end border-t px-3 py-2">
        <button type="button" className="btn btn-sm" disabled={busy} onClick={() => void retry()}>
          {busy ? <Spinner size={12} /> : <RotateCw size={12} aria-hidden />} {down ? '立即重试' : '重新检测'}
        </button>
      </div>
    </div>
  )
}

/** catalog.refresh 的逐项结果：每一行都是一个真实请求，✓ 带耗时，✕ 带状态码 */
function CheckList({ checks, lead }: { checks: CatalogCheck[]; lead?: ReactNode }) {
  return (
    <ul className="flex flex-col">
      {lead}
      {checks.map((c) => (
        <li key={c.key} className="flex h-6 items-center gap-2 text-xs" data-check={c.key} data-state={c.state}>
          <CheckGlyph state={c.state} />
          <span className={c.state === 'error' ? 'text-fg' : 'text-dim'}>{c.label}</span>
          <span className="mono ml-auto tnum text-2xs" style={{ color: c.state === 'error' ? 'var(--st-failed)' : 'var(--text-faint)' }}
                title={c.error}>
            {/* 勾叉是装饰图形，读屏靠这一个词分辨成败 */}
            <span className="sr-only">{c.state === 'pending' ? '还在等' : c.state === 'ok' ? '已取回 ' : '失败 '}</span>
            {c.state === 'pending'
              ? <span aria-hidden>…</span>
              : c.state === 'ok' ? `${c.ms ?? '—'} ms` : c.status ? String(c.status) : '连不上'}
          </span>
        </li>
      ))}
    </ul>
  )
}

/** 启动慢的时候读屏念什么：还在等哪几项、哪几项失败了 */
function bootProgress(checks: CatalogCheck[]): string | null {
  const waiting = checks.filter((c) => c.state === 'pending').map((c) => c.label)
  const failed = checks.filter((c) => c.state === 'error').map((c) => c.label)
  return [waiting.length && `还在等${waiting.join('、')}`, failed.length && `${failed.join('、')}没取回来`]
    .filter(Boolean).join('；') || null
}

function CheckGlyph({ state }: { state: CatalogCheck['state'] }) {
  if (state === 'pending') return <span className="flex w-3.5 justify-center text-faint"><Spinner size={11} /></span>
  return <StatusBadge status={state === 'ok' ? 'done' : 'failed'} size={13} decorative animate={false} />
}

// -------------------------------------------------------------------------
// 启动页
// -------------------------------------------------------------------------

/** 这么久还没加载完就不再只是「正在连接」：主动探一次、给出重试 */
const SLOW_BOOT_MS = 5000

/**
 * 启动页：品牌标 + 启动清单。
 *
 * 之前只有一个转圈和「正在连接后端…」，后端没起就永远转下去，看不出卡在哪。
 * 清单的每一行都是 catalog.refresh 的一个真实请求——✓ 带耗时，✕ 带状态码；
 * 从没连上过就停在这里说清原因和怎么办，给重试，也允许先进去（离线横幅会一直
 * 挂着），不放人进一屏「还没有工作流」。
 */
function BootScreen({ onEnterOffline }: { onEnterOffline: () => void }) {
  const checks = useCatalog((s) => s.checks)
  const backend = useCatalog((s) => s.backend)
  const latency = useCatalog((s) => s.latencyMs)
  const backendError = useCatalog((s) => s.backendError)
  const retryAt = useCatalog((s) => s.retryAt)
  const loaded = useCatalog((s) => s.loaded)
  const [t0] = useState(() => Date.now())
  const now = useTicker(true, 250)
  const [busy, setBusy] = useState(false)
  const elapsed = now - t0
  const down = backend === 'down'
  const slow = !down && !loaded && elapsed > SLOW_BOOT_MS
  // 一闪而过的启动（本机几十毫秒）不画清单，免得整屏跳一下
  const reveal = down || elapsed > 300

  // 请求没有超时，后端卡死（连得上但不回）时会一直挂着：到点主动探一次 /health，
  // 它带超时，卡死和断开都能在这里判出来
  useEffect(() => { if (slow) void useCatalog.getState().checkBackend() }, [slow])

  const retry = async () => {
    setBusy(true)
    try {
      const ok = await useCatalog.getState().checkBackend()
      // 从断开恢复时 catalog 自己会重拉；本来就连着（只是慢）就再拉一遍
      if (ok && !down) await useCatalog.getState().refresh()
    } finally {
      setBusy(false)
    }
  }

  const done = checks.filter((c) => c.state !== 'pending').length
  const secs = down && retryAt ? Math.max(0, Math.ceil((retryAt - now) / 1000)) : null
  const h = down ? humanizeError(new ApiError(0, backendError || NETWORK_MESSAGE, { kind: 'network' })) : null
  const headline = down ? '连不上后端服务' : slow ? '比平时慢' : '正在连接后端'
  // 读屏只播这一句：阶段变了（连接中 → 比平时慢 → 连不上）、慢的时候哪几项回来了。
  // 计时器和倒计时一秒一跳，放进播报区就是一秒念一遍
  const spoken = down ? h?.reason : slow ? bootProgress(checks) : null

  return (
    <div className="flex h-full items-center justify-center overflow-auto bg-bg p-6" data-boot={down ? 'down' : slow ? 'slow' : 'loading'}>
      <div className="w-full max-w-[360px]">
        <div className="flex items-center gap-3">
          <Logo size={34} className="text-accent" title="AgentLab" />
          <div>
            <div className="text-lg font-semibold leading-tight text-fg">AgentLab</div>
            <div className="text-2xs text-faint">受限动态编排</div>
          </div>
        </div>

        <div className={clsx('mt-6 overflow-hidden rounded-lg border bg-panel shadow-elev-1 transition-opacity duration-200', reveal ? 'opacity-100' : 'opacity-0')}>
          {/* 进度条只按「已经回来了几个请求」走，不插值、不做假进度 */}
          <div aria-hidden className="h-px bg-hairline">
            <div
              className="h-px origin-left transition-transform duration-200"
              style={{
                transform: `scaleX(${checks.length ? done / checks.length : 0})`,
                background: down ? 'var(--st-failed)' : 'var(--accent)',
              }}
            />
          </div>
          <div className="flex items-center justify-between px-3 pb-1.5 pt-2.5">
            <span role="status" className="flex items-center gap-2 text-sm font-medium" style={{ color: down ? 'var(--st-failed)' : slow ? 'var(--st-waiting)' : undefined }}>
              {!down && <Spinner size={13} />}
              {headline}
              {spoken && <span className="sr-only">：{spoken}</span>}
            </span>
            {!down && <span aria-hidden className="mono text-2xs text-faint tnum">{formatDuration(elapsed)}</span>}
          </div>
          <div className="px-3 pb-2.5">
            <CheckList
              checks={checks}
              lead={
                <li className="flex h-6 items-center gap-2 text-xs" data-check="backend" data-state={backend}>
                  {backend === 'checking'
                    ? <span className="flex w-3.5 justify-center text-faint"><Spinner size={11} /></span>
                    : <StatusBadge status={down ? 'failed' : 'done'} size={13} decorative animate={false} />}
                  <span className={down ? 'text-fg' : 'text-dim'}>后端服务</span>
                  <span className="mono ml-auto text-2xs tnum" style={{ color: down ? 'var(--st-failed)' : 'var(--text-faint)' }}>
                    {down ? '连不上' : backend === 'ok' && latency != null ? `${latency} ms` : '/api'}
                  </span>
                </li>
              }
            />
          </div>

          {(down || slow) && (
            <div className="border-t px-3 py-3 text-xs leading-relaxed">
              {down ? (
                <>
                  <div className="text-fg">{h?.reason ?? backendError ?? '请求没有到达后端'}</div>
                  <div className="mt-1 text-dim">
                    确认后端已经启动（开发环境用 <span className="mono">./scripts/dev.sh</span>），并且前端代理的端口和它一致。
                  </div>
                  <div className="mono mt-1 break-all text-2xs text-faint">
                    {window.location.origin}/api
                    {backendError && backendError !== NETWORK_MESSAGE && <> · {backendError}</>}
                  </div>
                </>
              ) : (
                <>
                  <div className="text-dim">
                    请求已经发出 {Math.floor(elapsed / 1000)} 秒还没全部回来：后端可能正在启动，或者卡在某个请求上。
                  </div>
                  {/* 后端连得上时「先进去」也还是一条路：一个请求卡死就把人永远关在门外更糟。
                      但进去之前先说清楚代价，进去以后遥测点和提示会接着说 */}
                  <div className="mt-1 text-faint">先进去的话，没回来的那几项在页面上会是空的，不代表没有数据。</div>
                </>
              )}
              <div className="mt-3 flex items-center gap-2">
                <button type="button" className="btn btn-sm btn-primary" disabled={busy} onClick={() => void retry()}>
                  {busy ? <Spinner size={12} /> : <RotateCw size={12} aria-hidden />} 立即重试
                </button>
                <button type="button" className="btn btn-sm btn-ghost" onClick={onEnterOffline}>先进去看看</button>
                {secs != null && !busy && <span className="ml-auto text-2xs text-faint tnum">{secs} 秒后自动重试</span>}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

/**
 * 启动时部分请求失败（后端连着，但某几张表报错）：进页面，但要说出来。
 * 不说的话，那几张表对应的下拉、列表就是空的，看着像"没有"。
 */
function useBootReport(loaded: boolean) {
  const reported = useRef(false)
  useEffect(() => {
    if (!loaded || reported.current) return
    reported.current = true
    // 只报后端回了错误码的那几项：够不着后端（没有状态码）归启动页和离线横幅说，
    // 这里再弹一条「6 项没加载成功」就是同一件事说两遍
    const bad = useCatalog.getState().checks.filter((c) => c.state === 'error' && c.status)
    if (!bad.length) return
    toast.warn(
      `有 ${bad.length} 项没加载成功：${bad.map((c) => (c.status ? `${c.label}（${c.status}）` : c.label)).join('、')}`,
      {
        detail: bad.map((c) => `${c.label}：${c.error ?? ''}`).join('\n'),
        action: { label: '重试', onClick: () => void useCatalog.getState().refresh() },
        key: 'boot-partial',
      },
    )
  }, [loaded])
}

// -------------------------------------------------------------------------
// 后台信号：标题、favicon、系统通知
// -------------------------------------------------------------------------

const PAGE_LABEL: Record<string, string> = Object.fromEntries(PAGES.map((p) => [p.to.slice(1), p.label]))

function describePath(
  pathname: string, search: string, ctx: { workflowName: string | null; conversationTitle: string | null },
): [string, string | null] {
  const [seg, id] = pathname.split('/').filter(Boolean)
  if (!seg) return ['问数据', null]
  const page = PAGE_LABEL[seg]
  if (!page) return ['页面不存在', null]
  if (seg === 'studio' && id) return [page, ctx.workflowName]
  if (seg === 'chat' && id) return [page, ctx.conversationTitle || null]
  if (seg === 'runs') {
    if (id) return [page, `运行 ${shortId(id)}`]
    return [page, new URLSearchParams(search).get('tab') === 'approvals' ? '待审批' : null]
  }
  return [page, null]
}

type Go = (to: string | null) => void

/** 在后台时结束的事：挂到标题和 favicon 上，开了提醒就再发一条系统通知 */
function report(go: Go, n: { attention: Attention; title: string; body?: string; to: string | null; tag: string }) {
  if (!document.hidden) return
  flagAttention(n.attention)
  notifyInBackground({ title: n.title, body: n.body, tag: n.tag, onClick: () => go(n.to) })
}

/** 回放、打开历史运行时事件是一口气灌进来的：活跃不到这么久就结束的，不算"刚跑完" */
const MIN_LIVE_MS = 1000

function watchStudio(go: Go): () => void {
  let watch: { runId: string; since: number } | null = null
  return useStudio.subscribe((s, p) => {
    if (s.runPhase === p.runPhase && s.run?.id === p.run?.id) return
    const id = s.run?.id ?? null
    if (!id) { watch = null; return }
    if (isActivePhase(s.runPhase)) {
      if (watch?.runId !== id) watch = { runId: id, since: Date.now() }
      return
    }
    const w = watch
    watch = null
    if (!w || w.runId !== id || s.trace.lastReplay || Date.now() - w.since < MIN_LIVE_MS) return
    const name = s.workflow?.name || s.run?.workflow_name || '工作流'
    // 人多半还停在这张画布上：点通知回来就行，不跳走
    const here = s.workflow && window.location.pathname.startsWith(`/studio/${s.workflow.id}`)
    const to = here ? null : `/runs/${id}`
    const tag = `run:${id}`
    if (s.runPhase === 'succeeded') {
      const t = s.trace
      const wall = t.timing?.wallMs ?? (t.startedAt && t.endedAt ? t.endedAt - t.startedAt : null)
      report(go, {
        attention: { tone: 'ok', text: '✓ 运行完成' }, title: `「${name}」运行完成`,
        body: wall != null ? `用时 ${formatDuration(wall)}` : undefined, to, tag,
      })
    } else if (s.runPhase === 'failed') {
      const nodeId = s.trace.failedNodeId
      const label = nodeId ? s.nodes.find((n) => n.id === nodeId)?.data?.label || nodeId : null
      const error = (nodeId && s.trace.nodes[nodeId]?.error) || s.run?.error || ''
      report(go, {
        attention: { tone: 'err', text: '✕ 运行失败' }, title: `「${name}」运行失败`,
        body: [label && `停在「${label}」`, error.split('\n')[0]].filter(Boolean).join('：') || undefined, to, tag,
      })
    } else if (s.runPhase === 'suspended') {
      report(go, {
        attention: { tone: 'warn', text: '‖ 运行中断' }, title: `「${name}」运行被中断`,
        body: '服务重启打断了这次运行，可以从断点接着跑', to, tag,
      })
    }
  })
}

function watchChat(go: Go): () => void {
  const seen = new Map<string, number | null>()
  return useChat.subscribe((s, p) => {
    if (s.byConversation === p.byConversation) return
    for (const [cid, turns] of Object.entries(s.byConversation)) {
      for (const t of turns) {
        const active = t.phase === 'planning' || t.phase === 'building' || t.phase === 'running'
        const since = seen.get(t.id) ?? null
        if (active) {
          if (since == null) seen.set(t.id, Date.now())
          continue
        }
        if (since == null) continue
        seen.set(t.id, null)
        if (t.restored || Date.now() - since < MIN_LIVE_MS) continue
        const to = window.location.pathname === `/chat/${cid}` ? null : `/chat/${cid}`
        const question = t.question.length > 60 ? `${t.question.slice(0, 60)}…` : t.question
        if (t.phase === 'done') {
          report(go, { attention: { tone: 'ok', text: '✓ 回答好了' }, title: '问数据：回答好了', body: question, to, tag: `turn:${t.id}` })
        } else if (t.phase === 'error') {
          report(go, { attention: { tone: 'err', text: '✕ 出错了' }, title: '问数据：这一问出错了', body: question, to, tag: `turn:${t.id}` })
        }
      }
    }
  })
}

function watchApprovals(go: Go): () => void {
  const pendingIds = (list: { id: string; status: string }[]) => list.filter((a) => a.status === 'pending').map((a) => a.id)
  // 已有的那些不算「新来的」：拿到过一次真列表才记底。启动时那一拉若失败了，
  // 列表是兜底的空数组，拿它记底，下一拍心跳就会把老的全当成新的
  const listed = (s: ReturnType<typeof useCatalog.getState>) =>
    s.loaded && s.checks.find((c) => c.key === 'approvals')?.state === 'ok'
  let seen: Set<string> | null = listed(useCatalog.getState()) ? new Set(pendingIds(useCatalog.getState().approvals)) : null
  return useCatalog.subscribe((s, p) => {
    if (s.approvals === p.approvals) return
    if (!seen) {
      if (listed(s) || (s.loaded && s.backend === 'ok')) seen = new Set(pendingIds(s.approvals))
      return
    }
    const fresh = s.approvals.filter((a) => a.status === 'pending' && !seen!.has(a.id))
    fresh.forEach((a) => seen!.add(a.id))
    for (const a of fresh) {
      notifyInBackground({
        title: `新的待审批：${a.title || '有一步在等你处理'}`,
        body: [a.workflow_name, a.node_label].filter(Boolean).join(' · ') || undefined,
        tag: `approval:${a.id}`,
        onClick: () => go(`/runs/${a.run_id}`),
      })
    }
  })
}

function ShellSignals() {
  const { pathname, search } = useLocation()
  const navigate = useNavigate()
  const pending = useCatalog((s) => s.approvals.filter((a) => a.status === 'pending').length)
  const studio = useStudioActivity()
  const chat = useChatActivity()
  const attention = useSignals((s) => s.attention)
  const pageTitle = useSignals((s) => s.pageTitle)
  const workflowName = useStudio((s) => s.workflow?.name ?? null)
  const conversationTitle = useConversations((s) => s.list.find((c) => c.id === s.currentId)?.title ?? null)

  const [page, object] = describePath(pathname, search, { workflowName, conversationTitle })
  const progress = studio.startsWith('run:') ? ` ${studio.slice(4)}` : ''
  const activity = studio.startsWith('run') || chat === 'run'
    ? `● 运行中${progress}`
    : studio === 'gen' || chat === 'gen' ? '● 生成中' : null

  useEffect(() => {
    document.title = composeTitle({
      pending, activity, attention, page: pageTitle ?? page, object: pageTitle ? null : object,
    })
  })

  // 点的优先级：失败 > 等人 > 在跑 > 后台时跑完了
  const dot: FaviconDot | null = attention?.tone === 'err'
    ? 'failed'
    : pending > 0 || attention?.tone === 'warn'
      ? 'waiting'
      : activity ? 'running' : attention?.tone === 'ok' ? 'done' : null
  useEffect(() => { paintFavicon(dot) }, [dot])

  const navRef = useRef(navigate)
  navRef.current = navigate
  useEffect(() => {
    const go: Go = (to) => { if (to) navRef.current(to) }
    const stops = [watchStudio(go), watchChat(go), watchApprovals(go)]
    return () => stops.forEach((stop) => stop())
  }, [])
  return null
}
