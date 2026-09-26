import { useEffect, useId, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import type { ComponentType, KeyboardEvent as ReactKeyboardEvent, ReactNode } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import type { NavigateFunction } from 'react-router-dom'
import { create } from 'zustand'
import clsx from 'clsx'
import {
  Bell, BellOff, BookOpen, Database, FlaskConical, History, Hourglass, Keyboard, MessageSquare,
  MessageSquarePlus, Monitor, Plus, Search, Settings, SunMoon, Workflow as WorkflowIcon, Wrench,
} from 'lucide-react'
import { ApiError, api } from '../api/client'
import { hasPendingApproval, useCatalog } from '../store/catalog'
import { useConversations } from '../store/conversations'
import { createWorkflow } from '../canvas/WorkflowPicker'
import { Kbd, Modal, StatusBadge, isComposing, toast } from './ui'
import { Logo } from './Logo'
import { applyTheme, readThemePref, resolvedTheme } from '../lib/theme'
import type { ThemePref } from '../lib/theme'
import { isMac } from '../lib/keys'
import { formatDateTime, formatTime, parseServerTime, shortId } from '../lib/format'
import { statusLabel } from '../lib/status'
import { WORKFLOW_STATUS_LABEL, runClassLabel } from '../lib/terms'
import { errorMessage } from '../lib/errors'
import { disableNotify, enableNotify, notifySupported, useSignals } from '../lib/notify'
import type { Run } from '../types'

/**
 * 外壳的命令表：导航、⌘K 命令面板、快捷键说明共用这一份。
 *
 * 工作流、对话、运行都能深链，之前却只能点导航再翻列表去找；界面上还写着
 * 一个根本不存在的 ⌥V。命令面板把"去哪、做什么"收到一个键盘入口里，快捷键
 * 的显示和判定都出自同一份描述（lib/keys），提示写什么就真能按什么。
 */

// -------------------------------------------------------------------------
// 页面表
// -------------------------------------------------------------------------

export interface PageDef {
  to: string
  label: string
  /** 一句话说这一页是干什么的：导航悬停提示和命令面板里都用 */
  hint: string
  icon: ComponentType<{ size?: number; className?: string; strokeWidth?: number }>
  shortcut: string
  /** 命令面板的搜索词：英文路由名、旧叫法、同义词 */
  keywords: string
}

// ⌥ + 数字切页：不和浏览器的 ⌘1–9（切标签页，页面拦不住）撞，也不和画布上的
// Backspace / Delete 以及各处输入框打架——「g c」这类两键序列就是栽在这上面
export const PAGES: PageDef[] = [
  { to: '/chat', label: '问数据', hint: '说需求，自动接数据源、建流程、跑出结论', icon: MessageSquare, shortcut: 'Alt+1', keywords: 'chat ask 对话 提问 首页' },
  { to: '/studio', label: '编排', hint: '在画布上搭工作流、调试、发布', icon: FlaskConical, shortcut: 'Alt+2', keywords: 'studio canvas 画布 工作流 workflow' },
  { to: '/runs', label: '记录', hint: '每次运行的过程、成果和待审批', icon: History, shortcut: 'Alt+3', keywords: 'runs history 运行 运行记录 历史 审批' },
  { to: '/data', label: '数据', hint: '接入数据库和表格，问数据和工作流从这里取数', icon: Database, shortcut: 'Alt+4', keywords: 'data datasource 数据源 数据库 表格 excel csv' },
  { to: '/tools', label: '工具', hint: '工具库、沙箱试验台、自定义工具、MCP', icon: Wrench, shortcut: 'Alt+5', keywords: 'tools mcp sandbox 沙箱 函数' },
  { to: '/knowledge', label: '知识', hint: '知识库、长期记忆、方法论 Skill', icon: BookOpen, shortcut: 'Alt+6', keywords: 'knowledge kb memory skill 知识库 记忆 文档' },
  { to: '/settings', label: '设置', hint: '模型接入、偏好、运行环境', icon: Settings, shortcut: 'Alt+7', keywords: 'settings provider 模型 署名 主题 偏好' },
]

// -------------------------------------------------------------------------
// 外壳浮层的开关
// -------------------------------------------------------------------------

const useShell = create<{ palette: boolean; help: boolean }>(() => ({ palette: false, help: false }))

export const openCommandPalette = () => useShell.setState({ palette: true, help: false })
export const closeCommandPalette = () => useShell.setState({ palette: false })
export const toggleCommandPalette = () => useShell.setState((s) => ({ palette: !s.palette, help: false }))
export const openShortcutHelp = () => useShell.setState({ help: true, palette: false })
export const closeShortcutHelp = () => useShell.setState({ help: false })
export const useShellOverlay = () => useShell((s) => s.palette || s.help)

// -------------------------------------------------------------------------
// 命令：导航和面板共用
// -------------------------------------------------------------------------

function subscribeTheme(cb: () => void): () => void {
  // 设置页的下拉、这里的快速切换、系统换深浅，三条路都会改画出来的主题
  const mo = new MutationObserver(cb)
  mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
  const mq = window.matchMedia('(prefers-color-scheme: light)')
  mq.addEventListener('change', cb)
  return () => {
    mo.disconnect()
    mq.removeEventListener('change', cb)
  }
}
const themeSnapshot = () => `${readThemePref()}:${resolvedTheme()}`

/** 当前的主题偏好和实际画的那一套，谁改了都跟得上 */
export function useTheme(): { pref: ThemePref; resolved: 'light' | 'dark' } {
  const snap = useSyncExternalStore(subscribeTheme, themeSnapshot, () => 'system:dark')
  const [pref, resolved] = snap.split(':') as [ThemePref, 'light' | 'dark']
  return { pref, resolved }
}

/** 最后一次选的主题，和它还没存进设置时的那个值（断线时换的） */
let latestTheme: ThemePref | null = null
let unsavedTheme: ThemePref | null = null

/**
 * 换主题并存进设置。主题的真源是后端 settings 的 ui.theme（App 启动时按它
 * 应用），只写 localStorage 的话下次打开又被改回去。
 */
export async function setThemePref(pref: ThemePref): Promise<void> {
  applyTheme(pref)
  latestTheme = pref
  // 设置页的下拉若开着，听这个事件同步，免得它手上的旧值被再存回去；App 听它
  // 记下「人在这台设备上换过」，重连时就不再拿服务端的旧值翻回去
  window.dispatchEvent(new CustomEvent('agentlab:theme', { detail: pref }))
  try {
    await saveTheme(pref)
  } catch (e) {
    const offline = e instanceof ApiError && e.kind === 'network'
    toast.warn(
      offline ? '主题先在这台设备上生效：连上后端后自动存进设置' : '主题只在这台设备上生效：没能存进设置',
      { detail: errorMessage(e), key: 'theme-save' },
    )
  }
}

/** PUT 按组整体替换，所以先取回 ui 这一组、只改 theme，别把画布吸附、自动保存这些冲掉 */
async function saveTheme(pref: ThemePref): Promise<void> {
  try {
    const current = await api.settings.get()
    await api.settings.put({ ui: { ...(current?.ui ?? {}), theme: pref } })
    if (latestTheme === pref) unsavedTheme = null
  } catch (e) {
    // 连点两下时先发的那次可能后失败：只记最后选的那个
    if (latestTheme === pref) unsavedTheme = pref
    throw e
  }
}

/**
 * 断线时换的主题当时没存上：后端恢复后补存。有要补的就返回 true，这时 App 不该
 * 再去读服务端——那里还是换之前的旧值。补存又失败就留到下一次恢复，不再弹提示：
 * 这一下不是人点的。
 */
export function flushThemePref(): boolean {
  if (!unsavedTheme) return false
  void saveTheme(unsavedTheme).catch(() => {})
  return true
}

/** 在深浅之间翻：按眼下实际画的那一套翻，「跟随系统」时也一样 */
export const toggleTheme = () => void setThemePref(resolvedTheme() === 'dark' ? 'light' : 'dark')

export async function toggleNotify(): Promise<void> {
  if (useSignals.getState().notify) {
    disableNotify()
    toast.info('后台提醒已关闭')
    return
  }
  const permission = await enableNotify()
  if (permission === 'granted') toast.ok('后台提醒已开启：标签页在后台时，运行结束、失败或来了新的待审批会发系统通知')
  else if (permission === 'denied') {
    toast.warn('浏览器拒绝了通知权限', {
      detail: '在地址栏左侧的站点设置里把「通知」改成允许，再回来打开这个开关。',
      key: 'notify-denied',
    })
  } else if (permission === 'unsupported') toast.warn('这个浏览器不支持系统通知')
}

export async function newConversation(navigate: NavigateFunction): Promise<void> {
  try {
    const id = await useConversations.getState().create()
    navigate(`/chat/${id}`)
  } catch (e) {
    toast.error(e)
  }
}

/** 等了多久，给待审批用：「12 分钟」「3 小时」「8 天」。粗粒度就够，要的是一眼看出晾了多久 */
export function waitedFor(createdAt: string | null | undefined, now = Date.now()): string | null {
  const d = parseServerTime(createdAt)
  if (!d) return null
  const mins = Math.max(0, Math.floor((now - d.getTime()) / 60_000))
  if (mins < 1) return '不到 1 分钟'
  if (mins < 60) return `${mins} 分钟`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours} 小时`
  return `${Math.floor(hours / 24)} 天`
}

// -------------------------------------------------------------------------
// ⌘K 命令面板
// -------------------------------------------------------------------------

type GroupKey = 'approvals' | 'pages' | 'actions' | 'conversations' | 'runs' | 'workflows'

const GROUPS: { key: GroupKey; label: string; idle: number }[] = [
  { key: 'approvals', label: '待审批', idle: 3 },
  { key: 'pages', label: '页面', idle: 9 },
  { key: 'actions', label: '动作', idle: 6 },
  { key: 'conversations', label: '最近会话', idle: 4 },
  { key: 'runs', label: '最近运行', idle: 4 },
  { key: 'workflows', label: '工作流', idle: 6 },
]
/** 有搜索词时每组最多几条 */
const QUERY_LIMIT = 8

interface Command {
  id: string
  group: GroupKey
  label: string
  hint?: string
  keywords?: string
  icon: ReactNode
  /** 右侧：时间、状态之类 */
  meta?: ReactNode
  shortcut?: string
  run: () => void
}

/** 子序列匹配：「多协作」能找到「⑥ 多 Agent 协作」，记不全名字时也搜得到 */
function subsequence(text: string, q: string): boolean {
  let i = 0
  for (const ch of text) if (ch === q[i]) i++
  return i === q.length
}

function score(c: Command, tokens: string[]): number {
  const label = c.label.toLowerCase()
  const rest = `${c.hint ?? ''} ${c.keywords ?? ''}`.toLowerCase()
  let total = 0
  for (const t of tokens) {
    if (label === t) total += 100
    else if (label.startsWith(t)) total += 80
    else if (label.includes(t)) total += 60 - Math.min(label.indexOf(t), 20)
    else if (rest.includes(t)) total += 25
    else if (t.length > 1 && subsequence(label, t)) total += 8
    else return 0
  }
  return total
}

export function CommandPalette() {
  const open = useShell((s) => s.palette)
  return open ? <PaletteView /> : null
}

function PaletteView() {
  const navigate = useNavigate()
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)
  const listId = useId()
  const workflows = useCatalog((s) => s.workflows)
  const approvals = useCatalog((s) => s.approvals)
  const backend = useCatalog((s) => s.backend)
  const conversations = useConversations((s) => s.list)
  const [runs, setRuns] = useState<Run[] | null>(null)
  const theme = useTheme()
  const notify = useSignals((s) => s.notify)

  // 打开前焦点在哪，在渲染时就记下：开发模式的 StrictMode 会把 effect 跑两遍，
  // 第二遍时焦点已经在面板的输入框里了
  const [opener] = useState(() => (document.activeElement instanceof HTMLElement ? document.activeElement : null))

  useEffect(() => {
    inputRef.current?.focus()
    return () => {
      // 焦点还给打开前的那个元素；选中的命令已经把焦点带去别处（跳了页、开了弹窗）就不抢
      const now = document.activeElement
      if (opener?.isConnected && (!now || now === document.body)) opener.focus({ preventScroll: true })
    }
  }, [opener])

  // 最近运行、最近会话是打开面板时才取：常驻轮询它们不值得
  useEffect(() => {
    if (backend === 'down') return
    let alive = true
    api.runs.list({ limit: 6 }).then((r) => { if (alive) setRuns(r) }, () => {})
    const conv = useConversations.getState()
    if (!conv.list.length && !conv.loading) void conv.load()
    return () => { alive = false }
    // 只在打开的那一刻取一次
  }, [])

  const commands = useMemo<Command[]>(() => {
    const go = (to: string) => () => navigate(to)
    const now = Date.now()
    const list: Command[] = []
    const pending = approvals
      .filter((a) => a.status === 'pending')
      .sort((a, b) => (parseServerTime(a.created_at)?.getTime() ?? 0) - (parseServerTime(b.created_at)?.getTime() ?? 0))

    for (const a of pending) {
      const waited = waitedFor(a.created_at, now)
      list.push({
        id: `approval:${a.id}`, group: 'approvals', label: a.title || '待审批',
        // 同一个审批节点跑过几次，标题会一模一样：带上运行的短 id 才分得开
        hint: [a.workflow_name, a.node_label, shortId(a.run_id)].filter(Boolean).join(' · '),
        keywords: `审批 approval 待办 ${a.run_id}`,
        icon: <Hourglass size={14} />,
        meta: waited && <span title={formatDateTime(a.created_at)}>已等 {waited}</span>,
        run: go(`/runs/${a.run_id}`),
      })
    }

    for (const p of PAGES) {
      list.push({
        id: `page:${p.to}`, group: 'pages', label: p.label, hint: p.hint, keywords: p.keywords,
        icon: <p.icon size={14} />, shortcut: p.shortcut, run: go(p.to),
      })
      if (p.to === '/runs' && pending.length) {
        list.push({
          id: 'page:approvals', group: 'pages', label: `待审批（${pending.length}）`,
          hint: '所有等人处理的审批卡', keywords: 'approvals 审批 待办 记录',
          icon: <Hourglass size={14} />, run: go('/runs?tab=approvals'),
        })
      }
    }

    list.push(
      {
        id: 'act:new-chat', group: 'actions', label: '新对话', hint: '在问数据里从头开始问',
        keywords: 'new chat conversation 会话 对话 新建', icon: <MessageSquarePlus size={14} />,
        run: () => void newConversation(navigate),
      },
      {
        id: 'act:new-workflow', group: 'actions', label: '新建工作流', hint: '起手是一个输入、一个成果',
        keywords: 'new workflow 画布 编排 创建', icon: <Plus size={14} />,
        // 和编排页的「新建工作流」同一条路：同名校验、画布上有没保存的改动先问
        run: () => void createWorkflow((to, opts) => navigate(to, opts), () => useCatalog.getState().refresh()),
      },
      {
        id: 'act:theme', group: 'actions',
        label: theme.resolved === 'dark' ? '切换到浅色主题' : '切换到深色主题',
        hint: theme.pref === 'system' ? '当前跟随系统' : undefined,
        keywords: 'theme dark light 主题 深色 浅色 暗色 亮色 切换主题', icon: <SunMoon size={14} />,
        run: toggleTheme,
      },
    )
    if (theme.pref !== 'system') {
      list.push({
        id: 'act:theme-system', group: 'actions', label: '主题跟随系统', keywords: 'theme system 主题 系统 自动',
        icon: <Monitor size={14} />, run: () => void setThemePref('system'),
      })
    }
    list.push({
      id: 'act:shortcuts', group: 'actions', label: '查看快捷键', hint: '全局和编排页的键盘操作',
      keywords: 'shortcuts keyboard help 键盘 帮助 热键', icon: <Keyboard size={14} />, shortcut: '?',
      run: openShortcutHelp,
    })
    if (notifySupported()) {
      list.push({
        id: 'act:notify', group: 'actions', label: notify ? '关闭后台提醒' : '开启后台提醒',
        hint: '标签页在后台时，运行结束、失败或来了新的待审批就发系统通知',
        keywords: 'notification 通知 提醒 后台', icon: notify ? <BellOff size={14} /> : <Bell size={14} />,
        run: () => void toggleNotify(),
      })
    }

    for (const c of conversations.slice(0, 30)) {
      list.push({
        id: `conv:${c.id}`, group: 'conversations', label: c.title || '新对话',
        hint: c.last_question || undefined, keywords: 'chat 会话 对话',
        icon: <MessageSquare size={14} />,
        meta: c.last_active_at && <span title={formatDateTime(c.last_active_at)}>{formatTime(c.last_active_at)}</span>,
        run: go(`/chat/${c.id}`),
      })
    }

    for (const r of runs ?? []) {
      const pendingHere = r.status === 'interrupted' ? hasPendingApproval(approvals, r.id) : undefined
      list.push({
        id: `run:${r.id}`, group: 'runs', label: r.workflow_name || '未命名工作流',
        hint: `${statusLabel(r.status, { pendingApproval: pendingHere })} · ${runClassLabel(r.run_class, r.version)} · ${shortId(r.id)}`,
        keywords: `run 运行 ${r.id}`,
        icon: <StatusBadge status={r.status} pendingApproval={pendingHere} size={14} animate={false} decorative />,
        meta: r.created_at && <span title={formatDateTime(r.created_at)}>{formatTime(r.created_at)}</span>,
        run: go(`/runs/${r.id}`),
      })
    }

    // 自己的工作流按最近改动排在前，模板（①…⑧）垫在后面：搜名字时两种都要找得到
    const byRecent = [...workflows].sort((a, b) =>
      Number(a.is_template) - Number(b.is_template)
      || (parseServerTime(b.updated_at)?.getTime() ?? 0) - (parseServerTime(a.updated_at)?.getTime() ?? 0))
    for (const w of byRecent) {
      const status = w.status && w.status !== 'draft' ? WORKFLOW_STATUS_LABEL[w.status] : null
      list.push({
        id: `wf:${w.id}`, group: 'workflows', label: w.name || '未命名工作流',
        hint: w.description || undefined,
        keywords: `workflow 工作流 ${w.is_template ? '模板 template' : ''} ${w.tags?.join(' ') ?? ''}`,
        icon: <WorkflowIcon size={14} />,
        meta: [w.is_template && '模板', status].filter(Boolean).join(' · ') || undefined,
        run: go(`/studio/${w.id}`),
      })
    }
    return list
  }, [approvals, conversations, navigate, notify, runs, theme.pref, theme.resolved, workflows])

  const sections = useMemo(() => {
    const tokens = query.trim().toLowerCase().split(/\s+/).filter(Boolean)
    return GROUPS.flatMap(({ key, label, idle }) => {
      let items = commands.filter((c) => c.group === key)
      if (tokens.length) {
        items = items
          .map((c) => [c, score(c, tokens)] as const)
          .filter(([, s]) => s > 0)
          .sort((a, b) => b[1] - a[1])
          .map(([c]) => c)
      }
      items = items.slice(0, tokens.length ? QUERY_LIMIT : idle)
      return items.length ? [{ key, label, items }] : []
    })
  }, [commands, query])

  const flat = useMemo(() => sections.flatMap((s) => s.items), [sections])
  const current = Math.min(active, Math.max(flat.length - 1, 0))
  const optionId = (i: number) => `${listId}-opt-${i}`

  useEffect(() => { setActive(0) }, [query])
  useEffect(() => {
    document.getElementById(optionId(current))?.scrollIntoView({ block: 'nearest' })
    // 只跟着选中项走：用户拿滚轮翻列表时，别因为别的数据刷新把他拽回去
  }, [current, query])

  const execute = (c: Command | undefined) => {
    if (!c) return
    closeCommandPalette()
    c.run()
  }
  const move = (delta: number) => {
    if (!flat.length) return
    setActive((i) => (Math.min(i, flat.length - 1) + delta + flat.length) % flat.length)
  }

  const onKeyDown = (e: ReactKeyboardEvent) => {
    if (isComposing(e)) return
    const emacs = e.ctrlKey && !e.metaKey && !e.altKey
    if (e.key === 'ArrowDown' || (emacs && e.key === 'n')) { e.preventDefault(); move(1) }
    else if (e.key === 'ArrowUp' || (emacs && e.key === 'p')) { e.preventDefault(); move(-1) }
    else if (e.key === 'PageDown') { e.preventDefault(); setActive((i) => Math.min(i + 5, flat.length - 1)) }
    else if (e.key === 'PageUp') { e.preventDefault(); setActive((i) => Math.max(i - 5, 0)) }
    else if (e.key === 'Enter') { e.preventDefault(); execute(flat[current]) }
    else if (e.key === 'Escape') {
      e.preventDefault()
      // 画布的属性面板、运行表单都在 window 上听 Esc，别让它们跟着一起关
      e.stopPropagation()
      closeCommandPalette()
    } else if (e.key === 'Tab') e.preventDefault()   // 面板里只有一个输入框，焦点不出去
  }

  let index = -1
  return (
    <div
      className="fixed inset-0 z-[60] flex items-start justify-center bg-black/50 px-4 pt-[12vh]"
      onMouseDown={(e) => { if (e.target === e.currentTarget) closeCommandPalette() }}
      onKeyDown={onKeyDown}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="命令面板"
        data-command-palette=""
        className="fade-up flex max-h-[76vh] w-full max-w-xl flex-col overflow-hidden rounded-xl border bg-panel shadow-elev-3"
      >
        <div aria-hidden className="h-px shrink-0 opacity-70" style={{ background: 'linear-gradient(90deg, transparent, var(--accent), transparent)' }} />
        <div className="flex h-12 shrink-0 items-center gap-2.5 border-b px-3.5">
          <Search size={16} className="shrink-0 text-faint" aria-hidden />
          <input
            ref={inputRef}
            role="combobox"
            aria-expanded="true"
            aria-controls={listId}
            aria-autocomplete="list"
            aria-activedescendant={flat.length ? optionId(current) : undefined}
            aria-label="搜索页面、工作流、运行、会话或命令"
            placeholder="去哪、做什么……搜页面、工作流、运行、会话"
            className="h-full min-w-0 flex-1 bg-transparent text-base text-fg outline-none placeholder:text-faint"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            autoComplete="off"
            spellCheck={false}
          />
          <Kbd combo="Esc" />
        </div>

        <div
          id={listId}
          role="listbox"
          aria-label="结果"
          className="min-h-0 flex-1 overflow-y-auto py-1.5"
          // 点在列表上不让输入框失焦：键盘要一直能用
          onMouseDown={(e) => e.preventDefault()}
        >
          {sections.map((section) => {
            const labelId = `${listId}-${section.key}`
            return (
              <div key={section.key} role="group" aria-labelledby={labelId}>
                <div id={labelId} className="flex items-center justify-between px-3.5 pb-1 pt-2.5 text-2xs font-medium text-faint">
                  <span>{section.label}</span>
                </div>
                {section.items.map((c) => {
                  index += 1
                  const i = index
                  const on = i === current
                  return (
                    <div
                      key={c.id}
                      id={optionId(i)}
                      role="option"
                      aria-selected={on}
                      data-command={c.id}
                      className={clsx(
                        'relative mx-1.5 flex h-10 cursor-pointer items-center gap-3 rounded-md px-2.5 text-sm',
                        on ? 'bg-hover text-fg' : 'text-dim',
                      )}
                      onMouseMove={() => { if (!on) setActive(i) }}
                      onClick={() => execute(c)}
                    >
                      {on && <span aria-hidden className="absolute bottom-2 left-0 top-2 w-0.5 rounded-r bg-accent" />}
                      <span
                        aria-hidden
                        className={clsx(
                          'flex h-6 w-6 shrink-0 items-center justify-center rounded-md border bg-elev',
                          on ? 'text-accent' : 'text-faint',
                        )}
                      >
                        {c.icon}
                      </span>
                      <span className="flex min-w-0 flex-1 items-baseline gap-2">
                        <span className={clsx('shrink-0 truncate', on && 'text-fg')} style={{ maxWidth: '60%' }}>{c.label}</span>
                        {c.hint && <span className="min-w-0 truncate text-xs text-faint">{c.hint}</span>}
                      </span>
                      {c.meta && <span className="shrink-0 text-2xs text-faint tnum">{c.meta}</span>}
                      {c.shortcut && <Kbd combo={c.shortcut} className="shrink-0" />}
                    </div>
                  )
                })}
              </div>
            )
          })}
          {!flat.length && (
            <div className="px-4 py-10 text-center">
              <div className="text-sm text-dim">没有匹配「{query.trim()}」的结果</div>
              <div className="mt-1 text-xs text-faint">试试页面名、工作流名，或者「主题」「快捷键」「新对话」</div>
            </div>
          )}
        </div>

        <div className="flex h-9 shrink-0 items-center gap-3 border-t px-3.5 text-2xs text-faint">
          <span className="flex items-center gap-1"><Kbd>↑</Kbd><Kbd>↓</Kbd> 选择</span>
          <span className="flex items-center gap-1"><Kbd combo="Enter" /> 打开</span>
          <span className="flex items-center gap-1"><Kbd combo="?" /> 快捷键</span>
          <span className="ml-auto flex items-center gap-1.5">
            <Logo size={12} className="text-accent" title="AgentLab" />
            <span className="tnum" aria-live="polite">{flat.length} 项</span>
          </span>
        </div>
      </div>
    </div>
  )
}

// -------------------------------------------------------------------------
// ? 快捷键说明
// -------------------------------------------------------------------------

interface ShortcutRow {
  keys: string[]
  label: string
  group?: string
}

/**
 * 编排页的清单归编排页自己维护（canvas/shortcuts.ts），这里只负责显示。
 * 按可选模块取：那个文件还没有的时候不能让整个外壳编译失败，回落到下面这份
 * 按现有代码核对过的清单。
 */
const studioModule = import.meta.glob('../canvas/shortcuts.ts', { eager: true }) as Record<string, Record<string, unknown>>

const STUDIO_FALLBACK: ShortcutRow[] = [
  { keys: ['Mod+S'], label: '保存工作流' },
  { keys: ['Mod+Enter'], label: '在运行表单里发起探索运行' },
  { keys: ['Backspace', 'Delete'], label: '删除选中的节点或连线' },
  { keys: ['Esc'], label: '关闭属性面板、收起弹层' },
]

function toRows(list: unknown): ShortcutRow[] {
  const items = Array.isArray(list) ? list : list && typeof list === 'object' ? Object.values(list) : []
  return items.flatMap((raw) => {
    if (!raw || typeof raw !== 'object') return []
    const r = raw as Record<string, unknown>
    const keys = [r.combo, r.combos, r.keys, r.key, r.shortcut, r.alt].flatMap((v) =>
      typeof v === 'string' ? [v] : Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : [])
    const label = [r.label, r.desc, r.description, r.title, r.name]
      .find((v): v is string => typeof v === 'string' && !!v.trim())
    if (!keys.length || !label) return []
    return [{ keys: [...new Set(keys)], label, group: typeof r.group === 'string' ? r.group : undefined }]
  })
}

function studioShortcuts(): { rows: ShortcutRow[]; fromStudio: boolean } {
  const mod = Object.values(studioModule)[0]
  if (mod) {
    const rows = toRows(mod.STUDIO_SHORTCUTS ?? mod.SHORTCUTS ?? mod.shortcuts ?? mod.default)
    if (rows.length) return { rows, fromStudio: true }
  }
  return { rows: STUDIO_FALLBACK, fromStudio: false }
}

const GLOBAL_ROWS: ShortcutRow[] = [
  { keys: ['Mod+K'], label: '命令面板：跳页面、打开工作流 / 运行 / 会话' },
  { keys: ['?'], label: '快捷键说明（就是这里）' },
  ...PAGES.map((p) => ({ keys: [p.shortcut], label: `去「${p.label}」` })),
  { keys: ['Esc'], label: '关闭弹窗和浮层' },
]

const CHAT_ROWS: ShortcutRow[] = [
  { keys: ['Enter'], label: '发送问题' },
  { keys: ['Shift+Enter'], label: '换行' },
]

function KeyCombo({ keys }: { keys: string[] }) {
  return (
    <span className="flex shrink-0 items-center gap-1">
      {keys.map((k, i) => (
        <span key={k} className="flex items-center gap-1">
          {i > 0 && <span className="text-2xs text-faint">/</span>}
          <Kbd combo={k} />
        </span>
      ))}
    </span>
  )
}

function ShortcutSection({ title, rows, here, note }: { title: string; rows: ShortcutRow[]; here?: boolean; note?: string }) {
  // 编排页的清单可能自己分了组（编辑、视图……），按组排，组内保持原顺序
  const groups: [string | undefined, ShortcutRow[]][] = []
  for (const r of rows) {
    const last = groups[groups.length - 1]
    if (last && last[0] === r.group) last[1].push(r)
    else groups.push([r.group, [r]])
  }
  return (
    <section aria-label={title} className="min-w-0">
      <h3 className="mb-1.5 flex items-center gap-2 text-xs font-semibold text-fg">
        {title}
        {here && <span className="rounded-full bg-accent-soft px-1.5 text-2xs font-normal text-accent">当前页</span>}
      </h3>
      {groups.map(([group, items], gi) => (
        <div key={gi}>
          {group && <div className="mt-2 text-2xs text-faint">{group}</div>}
          <ul>
            {items.map((r) => (
              <li key={`${r.label}-${r.keys.join()}`} className="flex items-center justify-between gap-3 border-b border-hairline py-1.5 text-xs last:border-0">
                <span className="min-w-0 text-dim">{r.label}</span>
                <KeyCombo keys={r.keys} />
              </li>
            ))}
          </ul>
        </div>
      ))}
      {note && <p className="mt-1.5 text-2xs leading-relaxed text-faint">{note}</p>}
    </section>
  )
}

export function ShortcutHelp() {
  const open = useShell((s) => s.help)
  const { pathname } = useLocation()
  const studio = useMemo(studioShortcuts, [])
  const onStudio = pathname.startsWith('/studio')
  const onChat = pathname.startsWith('/chat')

  const studioSection = (
    <ShortcutSection
      title="编排页"
      rows={studio.rows}
      here={onStudio}
      note={studio.fromStudio ? undefined : '画布上的快捷键以按钮提示为准；这里列的是编排页目前已经支持的。'}
    />
  )
  return (
    <Modal
      open={open}
      onClose={closeShortcutHelp}
      width={680}
      title={<span className="flex items-center gap-2"><Keyboard size={14} className="text-accent" aria-hidden /> 键盘快捷键</span>}
    >
      <div className="grid gap-x-8 gap-y-5 sm:grid-cols-2">
        {onStudio && studioSection}
        <ShortcutSection title="全局" rows={GLOBAL_ROWS} />
        {!onStudio && studioSection}
        <ShortcutSection title="问数据" rows={CHAT_ROWS} here={onChat} />
      </div>
      <p className="mt-5 border-t border-hairline pt-3 text-2xs leading-relaxed text-faint">
        按 {isMac ? 'macOS' : 'Windows / Linux'} 的键位显示。焦点在输入框里时，除了 <Kbd combo="Mod+K" /> 之外的快捷键都让给打字。
      </p>
    </Modal>
  )
}
