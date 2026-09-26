import { Component, useCallback, useEffect, useId, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import type {
  ButtonHTMLAttributes, CSSProperties, ErrorInfo, KeyboardEvent as ReactKeyboardEvent, ReactNode, RefObject,
} from 'react'
import {
  AlertCircle, AlertTriangle, Check, CheckCircle2, CloudOff, Copy, Info, Loader2, RotateCw, X,
} from 'lucide-react'
import clsx from 'clsx'
import { useCatalog } from '../store/catalog'
import { humanizeError } from '../lib/errors'
import { ariaShortcut, formatShortcut } from '../lib/keys'
import { STATUS, statusMeta } from '../lib/status'
import type { StatusShape } from '../lib/status'
import { formatDateTime, formatTime } from '../lib/format'

/**
 * 输入法正在组字吗。组字期间的回车是"选词"，不是"提交"——不判这一条，中文
 * 用户每选一个词就把半句话发出去一次（问数据页就这么把半句问题送去建图、跑图）。
 * Safari 在结束组字的那一下 keydown 上 isComposing 已经是 false，keyCode 还是
 * 229，两个都得看。所有"回车即提交"的输入框都要过这一关，Esc 关弹窗也一样：
 * 组字时按 Esc 是在取消候选词。
 */
export function isComposing(e: ReactKeyboardEvent | KeyboardEvent): boolean {
  const native = 'nativeEvent' in e ? e.nativeEvent : e
  return native.isComposing || e.keyCode === 229
}

/**
 * 页面级错误边界。没有它，任何一个组件渲染时抛错，React 会卸掉整棵树——整站
 * 白屏，导航栏也没了，用户只能刷新，没保存的编辑跟着丢。
 *
 * 包在路由外面：导航栏还在，换一页（resetKey 变了）就自动恢复。编辑中的图在
 * store 里、不在组件树上，点「重试」重新渲染时它还在。
 *
 * 技术信息（error.message）收进「技术细节」折叠区并能复制：对用户是噪音，对
 * 排障是全部线索。
 */
export class ErrorBoundary extends Component<
  { resetKey?: string; children: ReactNode }, { error: Error | null }
> {
  state: { error: Error | null } = { error: null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('页面渲染出错', error, info.componentStack)
  }

  componentDidUpdate(prev: { resetKey?: string }) {
    if (prev.resetKey !== this.props.resetKey && this.state.error) this.setState({ error: null })
  }

  render() {
    const { error } = this.state
    if (!error) return this.props.children
    // V8 的 stack 第一行就是「类名: 消息」，再拼一遍就重复了
    const raw = error.stack?.includes(error.message)
      ? error.stack
      : `${error.name}: ${error.message}${error.stack ? `\n\n${error.stack}` : ''}`
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 p-8 text-center">
        <AlertCircle size={22} className="text-[var(--err)]" aria-hidden />
        <div className="text-sm font-medium">这一页出错了</div>
        <div className="text-[11.5px] text-dim">没保存的编辑还在内存里：先点「重试」，恢复了就尽快保存。</div>
        <div className="flex gap-2">
          <button className="btn btn-sm" onClick={() => this.setState({ error: null })}>重试</button>
          <button className="btn btn-sm btn-ghost" onClick={() => location.reload()}>刷新页面</button>
        </div>
        <TechDetails raw={raw} summary={error.message} className="max-w-lg" />
      </div>
    )
  }
}

/** 「技术细节」折叠区：原文给运维复制用，默认收起 */
function TechDetails({ raw, summary, className }: { raw: string; summary?: string; className?: string }) {
  return (
    <details className={clsx('w-full text-left text-[11px] text-faint', className)}>
      <summary className="cursor-pointer select-none hover:text-dim">
        技术细节{summary ? <span className="ml-1.5 opacity-80">· {summary.length > 60 ? `${summary.slice(0, 60)}…` : summary}</span> : null}
      </summary>
      <div className="mt-1.5 flex items-start gap-1.5">
        <pre className="mono max-h-40 min-w-0 flex-1 overflow-auto whitespace-pre-wrap break-all rounded-md border bg-[var(--bg)] p-2 text-[11px] leading-relaxed text-dim">
          {raw}
        </pre>
        <CopyButton text={raw} />
      </div>
    </details>
  )
}

// -------------------------------------------------------------------------
// Toast
// -------------------------------------------------------------------------

/**
 * 全站最主要的提示手段，所以错误和成功不能一样权重、一样寿命：
 * - 普通和成功的约 4 秒消失，悬停或聚焦时暂停计时；
 * - 出错的常驻，直到点 ×，并且能复制详情——长错误 4 秒读不完，也要能贴给运维；
 * - 带动作的（「打开」「撤销」）多留一会儿；
 * - 同样的内容不叠，计数 ×N；最多露 3 条，其余收成「还有 N 条」。
 *
 * 状态放在模块里而不是 Context：store、lib 里也要能弹（toast.error(e)），
 * 不必把 hook 一路传下去。useToast() 照旧可用，返回的就是这个 toast。
 */

export type ToastKind = 'info' | 'ok' | 'warn' | 'error'

export interface ToastAction {
  label: string
  onClick: () => void
}

export interface ToastOptions {
  /** 详情：出错时的原文、长说明。折叠显示，复制按钮复制它 */
  detail?: string
  action?: ToastAction
  /** 常驻到手动关闭。error 默认常驻 */
  sticky?: boolean
  /** 自动消失前停留多久（ms）。默认 4000，带动作的 6000 */
  duration?: number
  /** 去重键。默认按「类型 + 文字」去重 */
  key?: string
}

interface ToastItem {
  id: number
  kind: ToastKind
  text: string
  detail?: string
  action?: ToastAction
  sticky: boolean
  duration: number
  count: number
  key: string
  /** 重复触发时加一，计时从头算 */
  bump: number
}

let toastSeq = 0
let toastItems: ToastItem[] = []
const toastListeners = new Set<() => void>()
const emitToasts = () => toastListeners.forEach((l) => l())
const subscribeToasts = (l: () => void) => {
  toastListeners.add(l)
  return () => { toastListeners.delete(l) }
}

function pushToast(text: string, kind: ToastKind = 'info', opts: ToastOptions = {}): number {
  const key = opts.key ?? `${kind}:${text}`
  const same = toastItems.find((t) => t.key === key)
  if (same) {
    toastItems = toastItems.map((t) => (t === same
      ? { ...t, count: t.count + 1, bump: t.bump + 1, detail: opts.detail ?? t.detail, action: opts.action ?? t.action }
      : t))
    emitToasts()
    return same.id
  }
  const id = ++toastSeq
  toastItems = [...toastItems, {
    id, kind, text, key, count: 1, bump: 0,
    detail: opts.detail,
    action: opts.action,
    sticky: opts.sticky ?? kind === 'error',
    duration: opts.duration ?? (opts.action ? 6000 : 4000),
  }]
  emitToasts()
  return id
}

function dismissToast(id?: number) {
  toastItems = id == null ? [] : toastItems.filter((t) => t.id !== id)
  emitToasts()
}

export interface ToastFn {
  /** 老写法：toast(text, kind) 照旧可用 */
  (text: string, kind?: ToastKind, opts?: ToastOptions): number
  ok: (text: string, opts?: ToastOptions) => number
  info: (text: string, opts?: ToastOptions) => number
  warn: (text: string, opts?: ToastOptions) => number
  /**
   * 出错提示。msg 可以直接给捕获到的异常：会翻成人话（humanizeError），原文放进
   * detail。常驻、可关、可复制。
   */
  error: (msg: unknown, opts?: ToastOptions) => number
  dismiss: (id?: number) => void
}

export const toast: ToastFn = Object.assign(
  (text: string, kind?: ToastKind, opts?: ToastOptions) => pushToast(text, kind, opts),
  {
    ok: (text: string, opts?: ToastOptions) => pushToast(text, 'ok', opts),
    info: (text: string, opts?: ToastOptions) => pushToast(text, 'info', opts),
    warn: (text: string, opts?: ToastOptions) => pushToast(text, 'warn', opts),
    error: (msg: unknown, opts?: ToastOptions) => {
      if (typeof msg === 'string') return pushToast(msg, 'error', opts)
      const h = humanizeError(msg)
      const text = h.reason ? `${h.title}：${h.reason}` : h.title
      return pushToast(text, 'error', { ...opts, detail: opts?.detail ?? h.raw })
    },
    dismiss: dismissToast,
  },
)

export const useToast = () => toast

const TOAST_VISIBLE = 3

/**
 * 挂在应用根部，包住整个应用（main.tsx 里已经这样挂着）。顺带渲染 DialogHost：
 * 两者都是"根部浮层"，挂一处就都有了；App 里再显式挂一个 DialogHost 也不会
 * 重复弹——只有第一个挂上的会渲染。
 *
 * 位置在内容区顶部居中、各页工具栏的下沿之下：右下角正好压在助手栏的输入区
 * 上，问数据页的输入框又在底部居中；贴着顶边又会盖住工具栏右侧的按钮（1280 宽
 * 时画布的 Copilot 按钮），而出错的 toast 是常驻的，一直盖着。工具栏最高 48px，
 * 离线横幅出现时再让出它的高度。左边让出 56px 的导航栏，在内容区里居中。
 */
export function ToastHost({ children }: { children?: ReactNode }) {
  const items = useSyncExternalStore(subscribeToasts, () => toastItems, () => toastItems)
  const [expanded, setExpanded] = useState(false)
  const hidden = expanded ? 0 : Math.max(0, items.length - TOAST_VISIBLE)
  const shown = items.slice(hidden)
  useEffect(() => { if (items.length <= TOAST_VISIBLE) setExpanded(false) }, [items.length])

  return (
    <>
      {children}
      <DialogHost />
      <div
        className="pointer-events-none fixed left-14 right-0 z-[100] flex justify-center px-4"
        style={{ top: 'calc(3.5rem + var(--offline-banner-h, 0px))' }}
      >
        <div className="flex w-full max-w-md flex-col gap-2">
          {hidden > 0 && (
            <button
              className="pointer-events-auto self-center rounded-full border bg-panel px-3 py-1 text-[11px] text-dim shadow-md hover:text-fg"
              onClick={() => setExpanded(true)}
            >
              还有 {hidden} 条 · 展开
            </button>
          )}
          {/* 两个播报区要一直在：读屏只播报「已存在的 live 区域里新增的内容」 */}
          <div role="alert" aria-live="assertive" className="flex flex-col gap-2">
            {shown.filter((t) => t.kind === 'error').map((t) => <ToastCard key={t.id} item={t} />)}
          </div>
          <div role="status" aria-live="polite" className="flex flex-col gap-2">
            {shown.filter((t) => t.kind !== 'error').map((t) => <ToastCard key={t.id} item={t} />)}
          </div>
          {expanded && items.length > TOAST_VISIBLE && (
            <button
              className="pointer-events-auto self-center text-[11px] text-faint hover:text-dim"
              onClick={() => { dismissToast(); setExpanded(false) }}
            >
              全部关闭
            </button>
          )}
        </div>
      </div>
    </>
  )
}

const TOAST_ICON: Record<ToastKind, { Icon: typeof Info; color: string }> = {
  ok: { Icon: CheckCircle2, color: 'var(--ok)' },
  info: { Icon: Info, color: 'var(--text-dim)' },
  warn: { Icon: AlertTriangle, color: 'var(--warn)' },
  error: { Icon: AlertCircle, color: 'var(--err)' },
}

function ToastCard({ item }: { item: ToastItem }) {
  const [paused, setPaused] = useState(false)
  const remaining = useRef(item.duration)
  const startedAt = useRef(0)

  // 重复触发时计时从头算：同一条提示又来了一次，说明它仍然是新鲜的
  useEffect(() => { remaining.current = item.duration }, [item.bump, item.duration])

  useEffect(() => {
    if (item.sticky || paused) return
    startedAt.current = Date.now()
    const timer = setTimeout(() => dismissToast(item.id), remaining.current)
    return () => {
      clearTimeout(timer)
      remaining.current = Math.max(800, remaining.current - (Date.now() - startedAt.current))
    }
  }, [item.sticky, item.id, paused, item.bump])

  const { Icon, color } = TOAST_ICON[item.kind]
  const copyText = item.detail ? `${item.text}\n\n${item.detail}` : item.text
  return (
    <div
      className="fade-up pointer-events-auto flex items-start gap-2 rounded-lg border bg-panel px-3 py-2 shadow-elev-2"
      style={{ borderColor: item.kind === 'error' ? 'var(--err)' : item.kind === 'warn' ? 'var(--warn)' : 'var(--border)' }}
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocus={() => setPaused(true)}
      onBlur={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node)) setPaused(false) }}
    >
      <Icon size={14} className="mt-0.5 shrink-0" style={{ color }} aria-hidden />
      <div className="min-w-0 flex-1">
        <div className="text-xs leading-relaxed whitespace-pre-wrap break-words">
          {item.text}
          {item.count > 1 && <span className="ml-1.5 tabular-nums text-faint">×{item.count}</span>}
        </div>
        {item.detail && (
          <details className="mt-1 text-[11px] text-faint">
            <summary className="cursor-pointer select-none hover:text-dim">详情</summary>
            <pre className="mono mt-1 max-h-32 overflow-auto whitespace-pre-wrap break-all text-[11px] leading-relaxed">
              {item.detail}
            </pre>
          </details>
        )}
      </div>
      {item.action && (
        <button
          className="btn btn-sm shrink-0"
          onClick={() => { item.action!.onClick(); dismissToast(item.id) }}
        >
          {item.action.label}
        </button>
      )}
      {item.kind === 'error' && <CopyIconButton text={copyText} />}
      <button
        className="btn btn-ghost btn-sm -mr-1.5 shrink-0 px-1"
        aria-label="关闭提示"
        title="关闭"
        onClick={() => dismissToast(item.id)}
      >
        <X size={13} />
      </button>
    </div>
  )
}

function CopyIconButton({ text, label = '复制详情' }: { text: string; label?: string }) {
  const [done, setDone] = useState(false)
  return (
    <button
      className="btn btn-ghost btn-sm shrink-0 px-1"
      aria-label={done ? '已复制' : label}
      title={done ? '已复制' : label}
      onClick={() => {
        void navigator.clipboard?.writeText(text).then(() => {
          setDone(true)
          setTimeout(() => setDone(false), 1200)
        }, () => {})
      }}
    >
      {done ? <Check size={13} className="text-[var(--ok)]" /> : <Copy size={13} />}
    </button>
  )
}

// -------------------------------------------------------------------------
// Modal
// -------------------------------------------------------------------------

/** 打开着的弹窗，后开的在上。键盘只归最上面那个管，嵌套时 Esc 只关一层 */
const modalStack: object[] = []
const isTopModal = (token: object) => modalStack[modalStack.length - 1] === token

const FOCUSABLE = [
  'a[href]', 'button:not([disabled])', 'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])', 'textarea:not([disabled])', 'summary', '[tabindex]:not([tabindex="-1"])',
  '[contenteditable="true"]',
].join(',')

function focusables(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE))
    .filter((el) => el.getClientRects().length > 0 && !el.closest('[inert]'))
}

/**
 * 弹窗。
 *
 * - 焦点困在弹窗里（Tab / Shift+Tab 循环），关闭后回到打开前的那个元素；
 * - 打开时聚焦：initialFocus → 带 data-autofocus 的元素 → 第一个输入框 → 弹窗本身；
 * - Esc 过输入法组字判断：中文输入法里按 Esc 是在取消候选词，不是要关窗；
 * - 点遮罩关闭要求按下和松开都落在遮罩上：在输入框里拖选文字拖出了界，不该关；
 * - dirty 时 Esc、点遮罩、点 × 都先在底部问一句「放弃 / 继续编辑」。Skill 指令、
 *   自定义工具代码、数据源连接信息都是难重填的长内容，误触一下就全没了。
 *   footer 里调用方自己的「取消」按钮是明确意图，不拦。
 */
export function Modal({
  open, onClose, title, children, footer, width = 560, dirty = false, initialFocus, describedBy,
}: {
  open: boolean; onClose: () => void; title: ReactNode
  children: ReactNode; footer?: ReactNode; width?: number
  /** 有未保存的改动：关闭前先问 */
  dirty?: boolean
  /** 打开时要聚焦的元素 */
  initialFocus?: RefObject<HTMLElement | null>
  /** 正文说明的元素 id，接到 aria-describedby */
  describedBy?: string
}) {
  const titleId = useId()
  const rootRef = useRef<HTMLDivElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const keepRef = useRef<HTMLButtonElement>(null)
  const tokenRef = useRef<object>({})
  const downOnBackdrop = useRef(false)
  const upOnBackdrop = useRef(false)
  const lastInside = useRef<HTMLElement | null>(null)
  const [asking, setAsking] = useState(false)

  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose
  const dirtyRef = useRef(dirty)
  dirtyRef.current = dirty

  // 改动撤回了（比如用户把内容删回原样），询问条就没有意义了
  useEffect(() => { if (!dirty) setAsking(false) }, [dirty])
  useEffect(() => { if (!open) setAsking(false) }, [open])

  const requestClose = useCallback(() => {
    if (!dirtyRef.current) { onCloseRef.current(); return }
    const active = document.activeElement
    if (active instanceof HTMLElement && panelRef.current?.contains(active)) lastInside.current = active
    setAsking(true)
  }, [])

  // 询问条出现时把焦点给「继续编辑」：这时按回车应当是最安全的那个选择
  useEffect(() => { if (asking) keepRef.current?.focus() }, [asking])

  const keepEditing = () => {
    setAsking(false)
    const back = lastInside.current
    requestAnimationFrame(() => (back?.isConnected ? back : panelRef.current)?.focus())
  }

  useEffect(() => {
    if (!open) return
    const token = tokenRef.current
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
    modalStack.push(token)

    const frame = requestAnimationFrame(() => {
      const panel = panelRef.current
      if (!panel || panel.contains(document.activeElement)) return  // 调用方自己 autoFocus 过了
      const target = initialFocus?.current
        ?? panel.querySelector<HTMLElement>('[data-autofocus]')
        ?? panel.querySelector<HTMLElement>(
          '[data-modal-body] input:not([disabled]):not([type="hidden"]):not([type="checkbox"]):not([type="radio"]), [data-modal-body] textarea:not([disabled]), [data-modal-body] select:not([disabled])')
        ?? panel
      target.focus({ preventScroll: true })
    })

    // 焦点落在弹窗外（比如点了遮罩上的空白，焦点掉到 body）时，键盘仍然归它管
    const onWindowKey = (e: KeyboardEvent) => {
      if (!isTopModal(token) || rootRef.current?.contains(e.target as Node)) return
      if (e.key === 'Escape' && !isComposing(e) && !e.defaultPrevented) {
        e.preventDefault()
        requestClose()
      } else if (e.key === 'Tab') {
        e.preventDefault()
        const panel = panelRef.current
        if (panel) (focusables(panel)[0] ?? panel).focus()
      }
    }
    window.addEventListener('keydown', onWindowKey)

    return () => {
      cancelAnimationFrame(frame)
      window.removeEventListener('keydown', onWindowKey)
      const i = modalStack.lastIndexOf(token)
      if (i >= 0) modalStack.splice(i, 1)
      // 焦点还给打开它的那个元素；如果焦点已经被别处拿走（比如关窗后跳了页），不抢
      const active = document.activeElement
      const stillOurs = !active || active === document.body || rootRef.current?.contains(active)
      if (opener?.isConnected && stillOurs) opener.focus({ preventScroll: true })
    }
    // initialFocus 只在打开那一刻用，不进依赖
  }, [open, requestClose])

  const onKeyDown = (e: ReactKeyboardEvent) => {
    if (!isTopModal(tokenRef.current)) return
    if (e.key === 'Escape') {
      // 里面的组件（补全弹层、下拉）自己处理过 Esc 的，不再关窗
      if (isComposing(e) || e.defaultPrevented) return
      e.preventDefault()
      // 拦住冒泡：画布属性面板之类在 window 上监听 Esc 的，不该跟着一起关
      e.stopPropagation()
      if (asking) keepEditing()
      else requestClose()
      return
    }
    if (e.key === 'Tab') {
      const panel = panelRef.current
      if (!panel) return
      const list = focusables(panel)
      if (!list.length) { e.preventDefault(); panel.focus(); return }
      const first = list[0]
      const last = list[list.length - 1]
      const active = document.activeElement
      if (e.shiftKey && (active === first || active === panel || !panel.contains(active))) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && (active === last || !panel.contains(active))) {
        e.preventDefault()
        first.focus()
      }
    }
  }

  if (!open) return null
  return (
    <div
      ref={rootRef}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/55 p-4"
      // 按下和松开都要落在遮罩上才算点遮罩。只看 click 的 target 不够：在两个不同
      // 元素上按下、松开时，浏览器把 click 派给二者的公共祖先——从面板里拖选文字
      // 拖到遮罩上，或者反过来从遮罩按下拖进面板，click 的 target 都是遮罩
      onMouseDown={(e) => {
        downOnBackdrop.current = e.target === e.currentTarget
        // 按在遮罩上不让焦点掉到 body：弹窗还开着，键盘（回车、Tab）得还归它
        if (downOnBackdrop.current) e.preventDefault()
      }}
      onMouseUp={(e) => { upOnBackdrop.current = e.target === e.currentTarget }}
      onClick={(e) => {
        const both = downOnBackdrop.current && upOnBackdrop.current && e.target === e.currentTarget
        downOnBackdrop.current = false
        upOnBackdrop.current = false
        if (both) requestClose()
      }}
      onKeyDown={onKeyDown}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={describedBy}
        tabIndex={-1}
        className="fade-up flex max-h-[88vh] w-full flex-col overflow-hidden rounded-xl border bg-panel shadow-elev-3 outline-none"
        style={{ maxWidth: width }}
      >
        <div className="flex items-center justify-between border-b px-4 py-3">
          <h2 id={titleId} className="text-sm font-semibold">{title}</h2>
          <button className="btn btn-ghost btn-sm" onClick={requestClose} aria-label="关闭" title="关闭（Esc）">
            <X size={14} />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto p-4" data-modal-body>{children}</div>
        {asking && (
          <div
            role="alert"
            className="flex items-center gap-2 border-t px-4 py-2.5 text-xs"
            style={{ background: 'color-mix(in srgb, var(--warn) 10%, transparent)' }}
          >
            <AlertTriangle size={13} className="shrink-0 text-[var(--warn)]" aria-hidden />
            <span className="flex-1">有未保存的修改，关掉就没了。</span>
            <button className="btn btn-sm btn-danger" onClick={() => { setAsking(false); onCloseRef.current() }}>
              放弃修改
            </button>
            <button ref={keepRef} className="btn btn-sm" onClick={keepEditing}>继续编辑</button>
          </div>
        )}
        {footer && <div className="flex justify-end gap-2 border-t px-4 py-3">{footer}</div>}
      </div>
    </div>
  )
}

// -------------------------------------------------------------------------
// 确认 / 输入对话框（命令式）
// -------------------------------------------------------------------------

export interface DialogOptions {
  /** 用动词写：「删除工作流「⑥ 多 Agent 协作」？」 */
  title: string
  body?: ReactNode
  /** 后果清单：「连同 4 条运行记录一起删除」「不可恢复」 */
  consequences?: string[]
  /** 写具体动作：「删除 5 条记录」，不写「确定」 */
  confirmLabel?: string
  cancelLabel?: string
  /** 危险操作：确认按钮标红，初始焦点放在「取消」上，回车不会误触 */
  danger?: boolean
  /** 要求照抄这段文字才能确认（高代价操作） */
  requireText?: string
  placeholder?: string
  /** promptDialog 的初值 */
  initial?: string
  /** promptDialog 的输入框标签 */
  label?: string
  /** promptDialog：返回错误文字则不让提交 */
  validate?: (value: string) => string | null | undefined
}

interface DialogRequest {
  id: number
  kind: 'confirm' | 'prompt'
  opts: DialogOptions
  resolve: (value: any) => void
}

let dialogSeq = 0
let dialogQueue: DialogRequest[] = []
let dialogHosts: object[] = []
let dialogSnapshot = { queue: dialogQueue, host: dialogHosts[0] as object | undefined }
const dialogListeners = new Set<() => void>()
const emitDialogs = () => {
  dialogSnapshot = { queue: dialogQueue, host: dialogHosts[0] }
  dialogListeners.forEach((l) => l())
}
const subscribeDialogs = (l: () => void) => {
  dialogListeners.add(l)
  return () => { dialogListeners.delete(l) }
}

function plainText(opts: DialogOptions): string {
  const lines = [opts.title]
  if (typeof opts.body === 'string') lines.push(opts.body)
  if (opts.consequences?.length) lines.push(...opts.consequences.map((c) => `· ${c}`))
  return lines.join('\n\n')
}

function enqueue<T>(kind: DialogRequest['kind'], opts: DialogOptions): Promise<T> {
  return new Promise<T>((resolve) => {
    dialogQueue = [...dialogQueue, { id: ++dialogSeq, kind, opts, resolve }]
    emitDialogs()
  })
}

/**
 * 确认对话框，替代 window.confirm：样式跟主题走、能写后果、危险操作回车不误触。
 *
 *   if (!(await confirmDialog({ title: '删除这条运行记录？', danger: true,
 *     consequences: ['事件流和产出物一起删除，不可恢复'], confirmLabel: '删除记录' }))) return
 *
 * 没有挂 DialogHost 时退回原生 confirm，调用方不会因此永远等下去。
 */
export function confirmDialog(opts: DialogOptions): Promise<boolean> {
  if (!dialogHosts.length) return Promise.resolve(window.confirm(plainText(opts)))
  return enqueue<boolean>('confirm', opts)
}

/** 输入对话框，替代 window.prompt。取消返回 null，确定返回去掉首尾空白的文字 */
export function promptDialog(opts: DialogOptions): Promise<string | null> {
  if (!dialogHosts.length) {
    const v = window.prompt(plainText(opts), opts.initial ?? '')
    return Promise.resolve(v == null ? null : v.trim())
  }
  return enqueue<string | null>('prompt', opts)
}

/**
 * 渲染 confirmDialog / promptDialog 的地方。ToastHost 已经带了一个；多挂几个也
 * 只有第一个生效。
 */
export function DialogHost() {
  const [token] = useState(() => ({}))
  useEffect(() => {
    dialogHosts = [...dialogHosts, token]
    emitDialogs()
    return () => {
      dialogHosts = dialogHosts.filter((t) => t !== token)
      emitDialogs()
    }
  }, [token])
  const { queue, host } = useSyncExternalStore(subscribeDialogs, () => dialogSnapshot, () => dialogSnapshot)
  if (host !== token || !queue.length) return null
  const req = queue[0]
  return <DialogView key={req.id} req={req} />
}

function DialogView({ req }: { req: DialogRequest }) {
  const { opts, kind } = req
  const [value, setValue] = useState(opts.initial ?? '')
  const [open, setOpen] = useState(true)
  const inputId = useId()
  const bodyId = useId()

  const finish = (result: unknown) => {
    setOpen(false)
    req.resolve(result)
    dialogQueue = dialogQueue.filter((d) => d !== req)
    emitDialogs()
  }
  const cancel = () => finish(kind === 'prompt' ? null : false)

  const trimmed = value.trim()
  const invalid = kind === 'prompt'
    ? (!trimmed ? '不能为空' : opts.validate?.(trimmed) || null)
    : null
  const textOk = opts.requireText == null || value === opts.requireText
  const canConfirm = kind === 'prompt' ? !invalid && textOk : textOk
  const confirm = () => {
    if (!canConfirm) return
    finish(kind === 'prompt' ? trimmed : true)
  }
  const onEnter = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' && !isComposing(e)) {
      e.preventDefault()
      confirm()
    }
  }

  const confirmLabel = opts.confirmLabel ?? (opts.danger ? '删除' : '确定')
  const showInput = kind === 'prompt' || opts.requireText != null
  return (
    <Modal
      open={open}
      onClose={cancel}
      title={opts.title}
      width={440}
      describedBy={opts.body || opts.consequences?.length ? bodyId : undefined}
      footer={
        <>
          <button className="btn" onClick={cancel} data-autofocus={opts.danger && !showInput ? '' : undefined}>
            {opts.cancelLabel ?? '取消'}
          </button>
          <button
            className={clsx('btn', opts.danger ? 'btn-danger' : 'btn-primary')}
            onClick={confirm}
            disabled={!canConfirm}
            data-autofocus={!opts.danger && !showInput ? '' : undefined}
          >
            {confirmLabel}
          </button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {(opts.body || opts.consequences?.length) && (
          <div id={bodyId} className="flex flex-col gap-2 text-[13px] leading-relaxed text-dim">
            {opts.body && <div>{opts.body}</div>}
            {!!opts.consequences?.length && (
              <ul className="flex flex-col gap-1">
                {opts.consequences.map((c, i) => (
                  <li key={i} className="flex gap-2">
                    <span
                      aria-hidden
                      className="mt-[7px] h-1.5 w-1.5 shrink-0 rounded-full"
                      style={{ background: opts.danger ? 'var(--err)' : 'var(--text-faint)' }}
                    />
                    <span className={opts.danger ? 'text-fg' : undefined}>{c}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
        {showInput && (
          <Field
            label={kind === 'prompt'
              ? (opts.label ?? '名称')
              : <>输入「<span className="mono text-fg">{opts.requireText}</span>」以确认</>}
            htmlFor={inputId}
            error={kind === 'prompt' && value && invalid ? invalid : undefined}
          >
            <input
              id={inputId}
              className="field"
              value={value}
              placeholder={opts.placeholder ?? (kind === 'prompt' ? undefined : opts.requireText)}
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={onEnter}
              onFocus={(e) => { if (kind === 'prompt') e.currentTarget.select() }}
              autoComplete="off"
              spellCheck={false}
              aria-invalid={kind === 'prompt' && !!value && !!invalid}
              data-autofocus=""
            />
          </Field>
        )}
      </div>
    </Modal>
  )
}

// -------------------------------------------------------------------------
// 空态、错误、离线、加载
// -------------------------------------------------------------------------

export const Spinner = ({ size = 14 }: { size?: number }) => (
  <Loader2 size={size} className="animate-spin" aria-hidden />
)

/**
 * 空态。
 *
 * 默认感知连接状态：后端断开时"空"不代表"没有"，改说「暂时拿不到数据」并把
 * 「新建」这类动作收起来——之前后端挂了，整站都在说「还没有工作流」并引导新建，
 * 用户照做就是一堆重复配置。纯本地的空（比如搜索没匹配）传 offline={false}。
 */
export function EmptyState({ icon, title, body, action, offline = 'auto', className }: {
  icon?: ReactNode; title: string; body?: ReactNode; action?: ReactNode
  offline?: 'auto' | false; className?: string
}) {
  const backend = useCatalog((s) => s.backend)
  const checkBackend = useCatalog((s) => s.checkBackend)
  if (offline === 'auto' && backend === 'down') {
    return (
      <div className={clsx('flex flex-col items-center justify-center gap-2 px-6 py-14 text-center', className)}>
        <div className="text-faint"><CloudOff size={22} aria-hidden /></div>
        <div className="text-sm text-dim">暂时拿不到数据</div>
        {/* 不写「连上后会自动刷新」：页面自己拉的列表要接了 useOnReconnect 才会重拉，
            这句话对没接的页面是假的 */}
        <div className="max-w-sm text-xs leading-relaxed text-faint">
          后端没连上。这里显示为空不代表没有数据。
        </div>
        <button className="btn btn-sm mt-2" onClick={() => void checkBackend()}>
          <RotateCw size={12} aria-hidden /> 立即重试
        </button>
      </div>
    )
  }
  return (
    <div className={clsx('flex flex-col items-center justify-center gap-2 px-6 py-14 text-center', className)}>
      {icon && <div className="text-faint opacity-50" aria-hidden>{icon}</div>}
      <div className="text-sm text-dim">{title}</div>
      {body && <div className="max-w-sm text-xs text-faint leading-relaxed">{body}</div>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  )
}

/** 老名字。hint 就是 EmptyState 的 body */
export function Empty({ icon, title, hint, action, offline }: {
  icon?: ReactNode; title: string; hint?: string; action?: ReactNode; offline?: 'auto' | false
}) {
  return <EmptyState icon={icon} title={title} body={hint} action={action} offline={offline} />
}

/**
 * 出错态：先一句人话，再原因和怎么办，原文收进「技术细节」。
 * compact 用在对话轮次、运行流这类行内位置（左对齐、不撑满）。
 */
export function ErrorState({ error, onRetry, compact = false, className }: {
  error: unknown; onRetry?: () => void; compact?: boolean; className?: string
}) {
  const h = humanizeError(error)
  const Icon = h.kind === 'network' ? CloudOff : AlertCircle
  if (compact) {
    return (
      <div
        role="alert"
        className={clsx('flex items-start gap-2 rounded-lg border px-3 py-2 text-xs', className)}
        style={{
          borderColor: 'color-mix(in srgb, var(--err) 35%, var(--border))',
          background: 'color-mix(in srgb, var(--err) 6%, transparent)',
        }}
      >
        <Icon size={14} className="mt-0.5 shrink-0 text-[var(--err)]" aria-hidden />
        <div className="min-w-0 flex-1">
          <div className="font-medium text-fg">{h.title}</div>
          {h.reason && <div className="mt-0.5 leading-relaxed text-dim">{h.reason}</div>}
          {h.action && <div className="mt-0.5 leading-relaxed text-faint">{h.action}</div>}
          {h.raw && h.raw !== h.title && <TechDetails raw={h.raw} className="mt-1" />}
        </div>
        {onRetry && <button className="btn btn-sm shrink-0" onClick={onRetry}><RotateCw size={12} aria-hidden /> 重试</button>}
      </div>
    )
  }
  return (
    <div role="alert" className={clsx('flex flex-col items-center justify-center gap-2 px-6 py-14 text-center', className)}>
      <Icon size={22} className="text-[var(--err)]" aria-hidden />
      <div className="text-sm font-medium">{h.title}</div>
      {h.reason && <div className="max-w-sm text-xs leading-relaxed text-dim">{h.reason}</div>}
      {h.action && <div className="max-w-sm text-xs leading-relaxed text-faint">{h.action}</div>}
      {onRetry && (
        <button className="btn btn-sm mt-1" onClick={onRetry}><RotateCw size={12} aria-hidden /> 重试</button>
      )}
      {h.raw && h.raw !== h.title && <TechDetails raw={h.raw} className="mt-2 max-w-md" />}
    </div>
  )
}

/** 同 ErrorState compact：对话轮次、运行流里轮次级的报错 */
export function ErrorNotice(props: { error: unknown; onRetry?: () => void; className?: string }) {
  return <ErrorState {...props} compact />
}

/** 每 intervalMs 刷新一次的「现在」。只在 active 时计时，不做常驻定时器 */
function useNow(active: boolean, intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    setNow(Date.now())
    const t = setInterval(() => setNow(Date.now()), intervalMs)
    return () => clearInterval(t)
  }, [active, intervalMs])
  return now
}

/**
 * 后端断开时的顶部横幅。连着时什么都不画。
 * 挂在主内容区顶上（App 里 <main> 的第一个孩子），全站共享一份连接状态。
 */
export function OfflineBanner({ className }: { className?: string }) {
  const backend = useCatalog((s) => s.backend)
  const backendError = useCatalog((s) => s.backendError)
  const retryAt = useCatalog((s) => s.retryAt)
  const lastOkAt = useCatalog((s) => s.lastOkAt)
  const checkBackend = useCatalog((s) => s.checkBackend)
  const down = backend === 'down'
  const now = useNow(down)
  const [busy, setBusy] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  // 横幅把工具栏往下推了，toast 也得跟着让：把自己的高度（窄屏会折行）写到
  // --offline-banner-h 上，ToastHost 的 top 加上它
  useLayoutEffect(() => {
    const el = ref.current
    if (!down || !el) return
    const root = document.documentElement
    const sync = () => root.style.setProperty('--offline-banner-h', `${el.offsetHeight}px`)
    sync()
    const ro = new ResizeObserver(sync)
    ro.observe(el)
    return () => { ro.disconnect(); root.style.removeProperty('--offline-banner-h') }
  }, [down])

  if (!down) return null
  const secs = retryAt ? Math.max(0, Math.ceil((retryAt - now) / 1000)) : null
  return (
    <div
      ref={ref}
      role="alert"
      className={clsx('flex min-h-7 flex-wrap items-center gap-x-2 gap-y-0.5 border-b px-3 py-1 text-[11.5px]', className)}
      style={{
        color: 'var(--err)',
        borderColor: 'color-mix(in srgb, var(--err) 30%, var(--border))',
        background: 'color-mix(in srgb, var(--err) 9%, var(--bg-panel))',
      }}
      title={backendError ?? undefined}
    >
      <CloudOff size={13} className="shrink-0" aria-hidden />
      <span className="font-medium">后端未连接</span>
      <span className="text-dim">当前看到的空列表不代表数据丢失</span>
      {lastOkAt && (
        <span className="text-faint tabular-nums" title={formatDateTime(lastOkAt)}>· 最后连通 {formatTime(lastOkAt)}</span>
      )}
      <span className="ml-auto flex items-center gap-2">
        {secs != null && !busy && <span className="text-faint tabular-nums">{secs} 秒后自动重试</span>}
        <button
          className="btn btn-sm"
          disabled={busy}
          onClick={async () => {
            setBusy(true)
            try { await checkBackend() } finally { setBusy(false) }
          }}
        >
          {busy ? <Spinner size={12} /> : <RotateCw size={12} aria-hidden />} 立即重试
        </button>
      </span>
    </div>
  )
}

/**
 * 骨架屏：按真实布局画占位，数据回来时不跳。
 *
 * rows × cols 个占位条；文字行宽度错落，看着像段落而不是一堵墙。动效只有透明度
 * 呼吸，减少动效时停在静态灰块上。
 */
export function Skeleton({ rows = 3, cols = 1, height = 12, gap = 10, className, style }: {
  rows?: number; cols?: number; height?: number; gap?: number; className?: string; style?: CSSProperties
}) {
  const widths = ['100%', '86%', '72%', '94%', '64%']
  return (
    <div
      role="status"
      aria-label="正在加载"
      aria-busy="true"
      className={clsx('grid', className)}
      style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`, gap, ...style }}
    >
      {Array.from({ length: rows * cols }, (_, i) => (
        <div
          key={i}
          className="animate-pulse rounded"
          style={{
            height,
            width: cols === 1 && height <= 16 ? widths[i % widths.length] : '100%',
            background: 'var(--bg-hover)',
          }}
        />
      ))}
    </div>
  )
}

// -------------------------------------------------------------------------
// Tabs
// -------------------------------------------------------------------------

/**
 * 把 Tabs 的选中项接到 URL 的最后一段上：/settings/datasources。
 *
 * 和 Tabs 放在一起而不是单开一个 hooks 文件——tab 和它的地址是同一件事，
 * 拆到两处迟早各改各的。三个 tab 页（知识 / 工具 / 设置）各接一行。
 *
 * 用 replace 而不是 push：切 tab 不该在后退历史里堆一串，按后退是想离开
 * 这一页，不是想把四个标签倒着走一遍。
 *
 * 认不出的 tab 名（链接过期、手输错了）回落到 fallback，并把地址一起纠正过来，
 * 免得地址栏和眼前这一屏说的不是同一件事。
 */
export function useTabRoute(
  valid: readonly string[], fallback: string,
): [string, (key: string) => void] {
  const { tab } = useParams()
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const base = '/' + pathname.split('/').filter(Boolean)[0]
  const active = tab && valid.includes(tab) ? tab : fallback

  useEffect(() => {
    if (tab !== active) navigate(`${base}/${active}`, { replace: true })
  }, [tab, active, base, navigate])

  return [active, (key: string) => navigate(`${base}/${key}`, { replace: true })]
}

/**
 * 标签页。带 tablist / tab 语义，←→ Home End 切换（自动激活）。
 * 给了 idPrefix 时，配套用 <TabPanel idPrefix tabKey> 包内容，两边互相指认。
 */
export function Tabs({ tabs, active, onChange, label, idPrefix }: {
  tabs: { key: string; label: string; badge?: number }[]
  active: string
  onChange: (key: string) => void
  /** 读屏用的整组名字 */
  label?: string
  idPrefix?: string
}) {
  const refs = useRef<Record<string, HTMLButtonElement | null>>({})
  const move = (e: ReactKeyboardEvent, i: number) => {
    let next = -1
    if (e.key === 'ArrowRight') next = (i + 1) % tabs.length
    else if (e.key === 'ArrowLeft') next = (i - 1 + tabs.length) % tabs.length
    else if (e.key === 'Home') next = 0
    else if (e.key === 'End') next = tabs.length - 1
    if (next < 0) return
    e.preventDefault()
    const key = tabs[next].key
    onChange(key)
    refs.current[key]?.focus()
  }
  return (
    <div className="flex gap-1 border-b px-2" role="tablist" aria-label={label}>
      {tabs.map((t, i) => {
        const selected = active === t.key
        return (
          <button
            key={t.key}
            ref={(el) => { refs.current[t.key] = el }}
            role="tab"
            id={idPrefix ? `${idPrefix}-tab-${t.key}` : undefined}
            aria-selected={selected}
            aria-controls={idPrefix ? `${idPrefix}-panel-${t.key}` : undefined}
            tabIndex={selected ? 0 : -1}
            onClick={() => onChange(t.key)}
            onKeyDown={(e) => move(e, i)}
            className={clsx(
              'relative px-3 py-2 text-xs transition-colors',
              selected ? 'text-fg' : 'text-faint hover:text-dim',
            )}
          >
            {t.label}
            {t.badge != null && t.badge > 0 && (
              <span
                className="ml-1.5 rounded-full bg-accent-solid px-1.5 text-[10px] text-on-accent tabular-nums"
                aria-label={`${t.badge} 项`}
              >
                {t.badge}
              </span>
            )}
            {selected && (
              <span className="absolute inset-x-2 -bottom-px h-0.5 rounded bg-[var(--accent)]" aria-hidden />
            )}
          </button>
        )
      })}
    </div>
  )
}

export function TabPanel({ idPrefix, tabKey, children, className }: {
  idPrefix: string; tabKey: string; children: ReactNode; className?: string
}) {
  return (
    <div role="tabpanel" id={`${idPrefix}-panel-${tabKey}`} aria-labelledby={`${idPrefix}-tab-${tabKey}`} className={className}>
      {children}
    </div>
  )
}

export function Section({ title, right, children, defaultOpen = true }: {
  title: string; right?: ReactNode; children: ReactNode; defaultOpen?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className="border-b last:border-b-0">
      <div className="flex items-center justify-between px-3 py-2">
        <button
          onClick={() => setOpen(!open)}
          aria-expanded={open}
          className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-faint hover:text-dim"
        >
          <span className={clsx('transition-transform', open ? 'rotate-90' : '')} aria-hidden>›</span>
          {title}
        </button>
        {right}
      </div>
      {open && <div className="px-3 pb-3">{children}</div>}
    </div>
  )
}

// -------------------------------------------------------------------------
// 状态徽标
// -------------------------------------------------------------------------

/**
 * 状态徽标：每个状态一个剪影，去掉颜色也认得出（灰度打印、色弱、投影偏色）。
 * 圆环=运行中、虚线环=排队、实心圆角方=完成、菱形=等人、三角=失败、斜杠圆=跳过、
 * 实心小方=取消、虚线方加×=阻断、横杠=未到达；挂起画暂停符，未运行画小空心点。
 *
 * 唯一的常驻动效是运行中那段弧的旋转（transform），减少动效时停住，圆环和
 * 中心点照样说明它在运行。
 */
export function StatusBadge({ status, size = 14, pendingApproval, animate = true, decorative = false, className, style }: {
  status: string
  size?: number
  /** 运行状态是 interrupted 时，有没有待审批（决定画菱形还是暂停符） */
  pendingApproval?: boolean
  animate?: boolean
  /** 旁边已经有文字时设为 true，读屏不重复念 */
  decorative?: boolean
  className?: string
  style?: CSSProperties
}) {
  const meta = statusMeta(status, { pendingApproval })
  const maskId = 'sb' + useId().replace(/[^a-zA-Z0-9_-]/g, '')
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      className={clsx('inline-block shrink-0', className)}
      style={{ color: meta.color, ...style }}
      data-status={meta.code}
      data-shape={meta.shape}
      {...(decorative ? { 'aria-hidden': true } : { role: 'img', 'aria-label': meta.label })}
    >
      {!decorative && <title>{meta.label}</title>}
      <BadgeShape shape={meta.shape} maskId={maskId} animate={animate} />
    </svg>
  )
}

function BadgeShape({ shape, maskId, animate }: { shape: StatusShape; maskId: string; animate: boolean }) {
  const c = 'currentColor'
  switch (shape) {
    case 'ring':
      return (
        <>
          <circle cx="8" cy="8" r="6" fill="none" stroke={c} strokeWidth="1.6" opacity="0.35" />
          <g className={animate ? 'animate-spin' : undefined} style={{ transformOrigin: '8px 8px', animationDuration: '1.4s' }}>
            <path d="M8 2 A6 6 0 0 1 14 8" fill="none" stroke={c} strokeWidth="1.9" strokeLinecap="round" />
          </g>
          <circle cx="8" cy="8" r="2.2" fill={c} />
        </>
      )
    case 'dashed-ring':
      return (
        <>
          <circle cx="8" cy="8" r="6" fill="none" stroke={c} strokeWidth="1.5" strokeDasharray="2.4 2" />
          <path d="M8 5.2 V8 L9.9 9.2" fill="none" stroke={c} strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
        </>
      )
    case 'square':
      return (
        <>
          <mask id={maskId}>
            <rect width="16" height="16" fill="white" />
            <path d="M4.9 8.3 L7.1 10.4 L11.2 5.9" fill="none" stroke="black" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
          </mask>
          <rect x="2" y="2" width="12" height="12" rx="3" fill={c} mask={`url(#${maskId})`} />
        </>
      )
    case 'diamond':
      return (
        <>
          <mask id={maskId}>
            <rect width="16" height="16" fill="white" />
            <circle cx="8" cy="8" r="1.7" fill="black" />
          </mask>
          <path d="M8 1.4 L14.6 8 L8 14.6 L1.4 8 Z" fill={c} strokeLinejoin="round" mask={`url(#${maskId})`} />
        </>
      )
    case 'triangle':
      return (
        <>
          <mask id={maskId}>
            <rect width="16" height="16" fill="white" />
            <rect x="7.2" y="5.6" width="1.6" height="4.4" rx="0.8" fill="black" />
            <circle cx="8" cy="11.9" r="0.95" fill="black" />
          </mask>
          <path d="M8 1.6 L15 14 H1 Z" fill={c} stroke={c} strokeWidth="0.8" strokeLinejoin="round" mask={`url(#${maskId})`} />
        </>
      )
    case 'slashed':
      return (
        <>
          <circle cx="8" cy="8" r="5.8" fill="none" stroke={c} strokeWidth="1.6" />
          <path d="M3.9 12.1 L12.1 3.9" stroke={c} strokeWidth="1.6" strokeLinecap="round" />
        </>
      )
    case 'stop':
      return <rect x="4" y="4" width="8" height="8" rx="1.6" fill={c} />
    case 'dashed-x':
      return (
        <>
          <rect x="2.2" y="2.2" width="11.6" height="11.6" rx="2" fill="none" stroke={c} strokeWidth="1.4" strokeDasharray="2.3 1.8" />
          <path d="M5.7 5.7 L10.3 10.3 M10.3 5.7 L5.7 10.3" stroke={c} strokeWidth="1.6" strokeLinecap="round" />
        </>
      )
    case 'bar':
      return <rect x="3" y="7.1" width="10" height="1.8" rx="0.9" fill={c} />
    case 'pause':
      return (
        <>
          <rect x="3.8" y="3.2" width="3" height="9.6" rx="1" fill={c} />
          <rect x="9.2" y="3.2" width="3" height="9.6" rx="1" fill={c} />
        </>
      )
    case 'dot':
    default:
      return <circle cx="8" cy="8" r="3" fill="none" stroke={c} strokeWidth="1.4" />
  }
}

/**
 * 徽标加中文。正常态安静（只有字色），异常态（失败、等人、挂起、阻断）才铺淡底。
 * 文字从 lib/status 取，全站只有这一份说法。
 */
export function StatusPill({ status, pendingApproval, short = false, plain = false, title, className }: {
  status: string
  pendingApproval?: boolean
  /** 用短叫法：「待审批」「完成」 */
  short?: boolean
  /** 不要底色和内边距，行内用 */
  plain?: boolean
  title?: string
  className?: string
}) {
  const meta = statusMeta(status, { pendingApproval })
  const tint = !plain && meta.alert
  return (
    <span
      className={clsx(
        'inline-flex shrink-0 items-center gap-1 whitespace-nowrap text-[11px] leading-4',
        !plain && 'rounded-full px-1.5 py-px',
        className,
      )}
      style={{ color: meta.color, background: tint ? meta.soft : undefined }}
      title={title ?? meta.hint}
      data-status={meta.code}
    >
      <StatusBadge status={status} pendingApproval={pendingApproval} size={12} decorative />
      {short ? meta.short : meta.label}
    </span>
  )
}

/** 老名字，行内的状态标记。新代码用 StatusPill */
export function StatusDot({ status, pendingApproval }: { status: string; pendingApproval?: boolean }) {
  return <StatusPill status={status} pendingApproval={pendingApproval} plain />
}

/** 老名字。状态码 → 中文，来自 lib/status 的同一份表 */
export const STATUS_LABEL: Record<string, string> = {
  ...Object.fromEntries(Object.values(STATUS).map((m) => [m.code, m.label])),
  interrupted: STATUS.waiting.label,
}

// -------------------------------------------------------------------------
// 小件：图标按钮、键帽、表单字段
// -------------------------------------------------------------------------

/**
 * 纯图标按钮。label 必填：同时写进 aria-label 和 title，读屏不再只念「按钮」，
 * 鼠标悬停也有说明。shortcut 会拼进 title，并写进 aria-keyshortcuts。
 */
export function IconButton({ label, shortcut, icon, children, className, variant = 'ghost', size = 'sm', title, ...rest }:
  Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'aria-label'> & {
    label: string
    shortcut?: string
    icon?: ReactNode
    variant?: 'ghost' | 'default' | 'danger'
    size?: 'sm' | 'md'
  }) {
  const tip = title ?? (shortcut ? `${label}（${formatShortcut(shortcut)}）` : label)
  return (
    <button
      type="button"
      aria-label={label}
      aria-keyshortcuts={shortcut ? ariaShortcut(shortcut) : undefined}
      title={tip}
      className={clsx(
        'btn',
        variant === 'ghost' && 'btn-ghost',
        variant === 'danger' && 'btn-danger',
        size === 'sm' && 'btn-sm',
        className,
      )}
      {...rest}
    >
      {icon ?? children}
    </button>
  )
}

/** 键帽。combo 按平台显示：'Mod+K' 在 Mac 上是 ⌘K，其他平台是 Ctrl+K */
export function Kbd({ combo, children, className }: { combo?: string; children?: ReactNode; className?: string }) {
  return (
    <kbd
      className={clsx(
        'mono inline-flex items-center rounded border px-1 text-[10.5px] leading-4 text-dim tabular-nums',
        className,
      )}
      style={{ background: 'var(--bg-elev)', borderColor: 'var(--border-strong)' }}
    >
      {combo ? formatShortcut(combo) : children}
    </kbd>
  )
}

/**
 * 带标签的表单字段：label 用 htmlFor 接到输入框，提示和报错接到 aria-describedby。
 *
 * 两种用法：
 *   <Field label="名称" htmlFor="ds-name" hint="小写英文"><input id="ds-name" …/></Field>
 *   <Field label="名称" hint="小写英文">{(p) => <input className="field" {...p} />}</Field>
 * 第二种自动生成 id，并把 aria-describedby / aria-invalid 一起给到输入框。
 */
export function Field({ label, htmlFor, hint, error, required, children, className }: {
  label: ReactNode
  htmlFor?: string
  hint?: ReactNode
  error?: ReactNode
  required?: boolean
  children: ReactNode | ((props: { id: string; 'aria-describedby'?: string; 'aria-invalid'?: boolean }) => ReactNode)
  className?: string
}) {
  const auto = useId()
  const id = htmlFor ?? `f${auto.replace(/[^a-zA-Z0-9_-]/g, '')}`
  const hintId = `${id}-hint`
  const errorId = `${id}-error`
  const describedBy = error ? errorId : hint ? hintId : undefined
  return (
    <div className={clsx('min-w-0', className)}>
      <label className="label" htmlFor={id}>
        {label}
        {required && <span className="ml-0.5 text-[var(--err)]" aria-hidden>*</span>}
      </label>
      {typeof children === 'function'
        ? children({ id, 'aria-describedby': describedBy, 'aria-invalid': error ? true : undefined })
        : children}
      {error
        ? <div id={errorId} className="mt-1 text-[11px] leading-relaxed text-[var(--err)]">{error}</div>
        : hint
          ? <div id={hintId} className="mt-1 text-[11px] leading-relaxed text-faint">{hint}</div>
          : null}
    </div>
  )
}

/** 受控的 JSON 编辑框：输入过程中允许非法 JSON，失焦或合法时才回写。 */
export function JsonInput({ value, onChange, rows = 5, placeholder, id }: {
  value: any; onChange: (v: any) => void; rows?: number; placeholder?: string; id?: string
}) {
  const [text, setText] = useState(() => (value == null ? '' : JSON.stringify(value, null, 2)))
  const [bad, setBad] = useState(false)
  const touched = useRef(false)

  useEffect(() => {
    if (touched.current) return
    setText(value == null ? '' : JSON.stringify(value, null, 2))
  }, [value])

  return (
    <div>
      <textarea
        id={id}
        className="field mono text-[11px]"
        rows={rows}
        value={text}
        placeholder={placeholder}
        aria-invalid={bad || undefined}
        style={bad ? { borderColor: 'var(--err)' } : undefined}
        onChange={(e) => {
          touched.current = true
          setText(e.target.value)
          if (!e.target.value.trim()) { setBad(false); onChange(undefined); return }
          try {
            onChange(JSON.parse(e.target.value))
            setBad(false)
          } catch {
            setBad(true)
          }
        }}
        onBlur={() => { touched.current = false }}
      />
      {bad && <div className="mt-1 text-[11px] text-[var(--err)]">JSON 格式不对，还没保存</div>}
    </div>
  )
}

export function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false)
  return (
    <button
      className="btn btn-ghost btn-sm"
      onClick={() => {
        void navigator.clipboard?.writeText(text).then(() => {
          setDone(true)
          setTimeout(() => setDone(false), 1200)
        }, () => toast.error('复制失败：浏览器没有给剪贴板权限'))
      }}
    >
      {done ? '已复制' : '复制'}
    </button>
  )
}
