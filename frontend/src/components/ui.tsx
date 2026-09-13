import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { AlertCircle, CheckCircle2, Info, Loader2, X } from 'lucide-react'
import clsx from 'clsx'

// -------------------------------------------------------------------------
// Toast
// -------------------------------------------------------------------------

type ToastKind = 'info' | 'ok' | 'error'
interface ToastItem { id: number; kind: ToastKind; text: string }

const ToastCtx = createContext<(text: string, kind?: ToastKind) => void>(() => {})
export const useToast = () => useContext(ToastCtx)

export function ToastHost({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([])
  const push = useCallback((text: string, kind: ToastKind = 'info') => {
    const id = Date.now() + Math.random()
    setItems((prev) => [...prev, { id, kind, text }])
    setTimeout(() => setItems((prev) => prev.filter((t) => t.id !== id)), 4200)
  }, [])

  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="fixed bottom-4 right-4 z-[100] flex flex-col gap-2 max-w-md">
        {items.map((t) => (
          <div
            key={t.id}
            className="fade-up flex items-start gap-2 rounded-lg border bg-panel px-3 py-2 shadow-xl"
            style={{ borderColor: t.kind === 'error' ? 'var(--err)' : 'var(--border)' }}
          >
            {t.kind === 'ok' && <CheckCircle2 size={14} className="mt-0.5 shrink-0 text-[var(--ok)]" />}
            {t.kind === 'error' && <AlertCircle size={14} className="mt-0.5 shrink-0 text-[var(--err)]" />}
            {t.kind === 'info' && <Info size={14} className="mt-0.5 shrink-0 text-dim" />}
            <span className="text-xs leading-relaxed whitespace-pre-wrap break-words">{t.text}</span>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  )
}

// -------------------------------------------------------------------------
// Modal
// -------------------------------------------------------------------------

export function Modal({
  open, onClose, title, children, footer, width = 560,
}: {
  open: boolean; onClose: () => void; title: ReactNode
  children: ReactNode; footer?: ReactNode; width?: number
}) {
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/55 p-4"
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <div
        className="fade-up flex max-h-[88vh] w-full flex-col overflow-hidden rounded-xl border bg-panel shadow-2xl"
        style={{ maxWidth: width }}
      >
        <div className="flex items-center justify-between border-b px-4 py-3">
          <div className="text-sm font-semibold">{title}</div>
          <button className="btn btn-ghost btn-sm" onClick={onClose}><X size={14} /></button>
        </div>
        <div className="flex-1 overflow-y-auto p-4">{children}</div>
        {footer && <div className="flex justify-end gap-2 border-t px-4 py-3">{footer}</div>}
      </div>
    </div>
  )
}

// -------------------------------------------------------------------------
// 杂项
// -------------------------------------------------------------------------

export const Spinner = ({ size = 14 }: { size?: number }) => (
  <Loader2 size={size} className="animate-spin" />
)

export function Empty({ icon, title, hint, action }: {
  icon?: ReactNode; title: string; hint?: string; action?: ReactNode
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-14 text-center">
      {icon && <div className="text-faint opacity-50">{icon}</div>}
      <div className="text-sm text-dim">{title}</div>
      {hint && <div className="max-w-sm text-xs text-faint leading-relaxed">{hint}</div>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  )
}

export function Tabs({ tabs, active, onChange }: {
  tabs: { key: string; label: string; badge?: number }[]
  active: string
  onChange: (key: string) => void
}) {
  return (
    <div className="flex gap-1 border-b px-2">
      {tabs.map((t) => (
        <button
          key={t.key}
          onClick={() => onChange(t.key)}
          className={clsx(
            'relative px-3 py-2 text-xs transition-colors',
            active === t.key ? 'text-fg' : 'text-faint hover:text-dim',
          )}
        >
          {t.label}
          {t.badge != null && t.badge > 0 && (
            <span className="ml-1.5 rounded-full bg-[var(--accent)] px-1.5 text-[10px] text-white">
              {t.badge}
            </span>
          )}
          {active === t.key && (
            <span className="absolute inset-x-2 -bottom-px h-0.5 rounded bg-[var(--accent)]" />
          )}
        </button>
      ))}
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
          className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-faint hover:text-dim"
        >
          <span className={clsx('transition-transform', open ? 'rotate-90' : '')}>›</span>
          {title}
        </button>
        {right}
      </div>
      {open && <div className="px-3 pb-3">{children}</div>}
    </div>
  )
}

export function StatusDot({ status }: { status: string }) {
  const color =
    status === 'succeeded' ? 'var(--ok)'
    : status === 'failed' ? 'var(--err)'
    : status === 'running' ? 'var(--accent)'
    : status === 'interrupted' ? 'var(--warn)'
    : 'var(--text-faint)'
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        className={clsx('h-1.5 w-1.5 rounded-full', status === 'running' && 'animate-pulse')}
        style={{ background: color }}
      />
      <span className="text-[11px]" style={{ color }}>{STATUS_LABEL[status] ?? status}</span>
    </span>
  )
}

export const STATUS_LABEL: Record<string, string> = {
  queued: '排队中', running: '运行中', interrupted: '等待人工',
  succeeded: '成功', failed: '失败', cancelled: '已取消',
}

/** 受控的 JSON 编辑框：输入过程中允许非法 JSON，失焦或合法时才回写。 */
export function JsonInput({ value, onChange, rows = 5, placeholder }: {
  value: any; onChange: (v: any) => void; rows?: number; placeholder?: string
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
        className="field mono text-[11px]"
        rows={rows}
        value={text}
        placeholder={placeholder}
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
      {bad && <div className="mt-1 text-[10px] text-[var(--err)]">JSON 格式不对，还没保存</div>}
    </div>
  )
}

export function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false)
  return (
    <button
      className="btn btn-ghost btn-sm"
      onClick={() => {
        navigator.clipboard.writeText(text)
        setDone(true)
        setTimeout(() => setDone(false), 1200)
      }}
    >
      {done ? '已复制' : '复制'}
    </button>
  )
}
