import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import { Check, Copy, MoreHorizontal, ShieldCheck } from 'lucide-react'
import clsx from 'clsx'
import { toast } from '../../components/ui'
import { formatClock, parseServerTime } from '../../lib/format'
import { ISSUANCE_LABEL, runClassLabel } from '../../lib/terms'
import { useRunClock } from '../../run/useRunClock'
import { formatSpan } from './model'

// -------------------------------------------------------------------------
// 页签
// -------------------------------------------------------------------------

export interface TabItem<K extends string> {
  key: K
  label: string
  /** 右侧的计数。tone 决定它醒不醒目：待审批要人处理，失败是历史，不该一样响 */
  count?: number | null
  countLabel?: string
  tone?: 'alert' | 'live' | 'quiet'
  title?: string
}

/**
 * 页签条。和 ui.tsx 的 Tabs 同一套语义（tablist / tab、←→ Home End 切换），
 * 多了一样：计数分轻重。Tabs 的徽标只有一种强调色，「失败 32」天天亮着就成了
 * 噪音，而「待审批 2」必须醒目。
 */
export function RunTabs<K extends string>({ tabs, active, onChange, label, idPrefix }: {
  tabs: TabItem<K>[]; active: K; onChange: (k: K) => void; label: string; idPrefix: string
}) {
  const refs = useRef<Record<string, HTMLButtonElement | null>>({})
  const move = (e: KeyboardEvent, i: number) => {
    let next = -1
    if (e.key === 'ArrowRight') next = (i + 1) % tabs.length
    else if (e.key === 'ArrowLeft') next = (i - 1 + tabs.length) % tabs.length
    else if (e.key === 'Home') next = 0
    else if (e.key === 'End') next = tabs.length - 1
    if (next < 0) return
    e.preventDefault()
    onChange(tabs[next].key)
    refs.current[tabs[next].key]?.focus()
  }
  return (
    <div className="flex shrink-0 gap-0.5 border-b px-2" role="tablist" aria-label={label}>
      {tabs.map((t, i) => {
        const selected = t.key === active
        const n = t.count ?? 0
        return (
          <button
            key={t.key}
            ref={(el) => { refs.current[t.key] = el }}
            type="button"
            role="tab"
            id={`${idPrefix}-tab-${t.key}`}
            aria-selected={selected}
            aria-controls={`${idPrefix}-panel`}
            tabIndex={selected ? 0 : -1}
            title={t.title}
            data-tab={t.key}
            onClick={() => onChange(t.key)}
            onKeyDown={(e) => move(e, i)}
            className={clsx(
              'relative flex items-center gap-1.5 px-2.5 py-2 text-xs transition-colors',
              selected ? 'text-fg' : 'text-faint hover:text-dim',
            )}
          >
            {t.label}
            {n > 0 && (
              <span
                className={clsx(
                  'tnum rounded-full text-2xs leading-4',
                  t.tone === 'alert' && 'bg-warn-solid px-1.5 font-medium text-on-warn',
                  t.tone === 'live' && 'text-st-running',
                  (!t.tone || t.tone === 'quiet') && 'text-faint',
                )}
                aria-label={`${t.countLabel ?? n} 条`}
              >
                {t.countLabel ?? n}
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

// -------------------------------------------------------------------------
// 小标签
// -------------------------------------------------------------------------

/** 正式运行才标：探索运行是默认，每行都标只会让真正的正式运行被淹没 */
export function ClassChip({ runClass, version, always = false }: {
  runClass?: string | null; version?: number | null; always?: boolean
}) {
  if (runClass !== 'formal') {
    return always ? <span className="chip shrink-0">{runClassLabel('exploratory')}</span> : null
  }
  return (
    <span className="chip shrink-0 text-fg" style={{ borderColor: 'var(--border-strong)' }}
          title="正式运行：跑的是已发布的不可变版本，结果进正式归档">
      <ShieldCheck size={10} aria-hidden /> {runClassLabel('formal', version)}
    </span>
  )
}

const TIER_TONE: Record<string, { color: string; soft: boolean; hint: string }> = {
  formal: { color: 'var(--st-done)', soft: false, hint: '指标齐全，叙述里的数字都能回指口径卡' },
  degraded: { color: 'var(--st-waiting)', soft: true, hint: '有缺口，结论要对照声明使用' },
  withheld: { color: 'var(--st-failed)', soft: true, hint: '必需指标缺失或数字无法溯源，这次结论不作数' },
}

/** 出具档位。完整出具安静（只有字色），降档和不予才铺底色 */
export function TierChip({ tier }: { tier?: string | null }) {
  if (!tier) return null
  const tone = TIER_TONE[tier] ?? { color: 'var(--text-dim)', soft: false, hint: '' }
  const label = (ISSUANCE_LABEL as Record<string, string>)[tier] ?? tier
  return (
    <span
      className="chip shrink-0"
      title={tone.hint || label}
      data-tier={tier}
      style={{
        color: tone.color,
        borderColor: `color-mix(in srgb, ${tone.color} 40%, transparent)`,
        background: tone.soft ? `color-mix(in srgb, ${tone.color} 12%, transparent)` : undefined,
      }}
    >
      {label}
    </span>
  )
}

// -------------------------------------------------------------------------
// 计时
// -------------------------------------------------------------------------

/**
 * 「已运行 00:37.2」。只有这一小块跟着时钟重画，整行不动。
 *
 * 过了一小时改写「8 天 21 小时」、按分钟刷新：从第一次开始算起，等了八天的审批
 * 批掉之后接着跑的那条，秒表会读成「213:46:45.6」，谁也读不出那是多久
 */
export function LiveElapsed({ since, prefix = '已运行 ' }: { since?: string | number | null; prefix?: string }) {
  const start = parseServerTime(since ?? null)?.getTime()
  const long = start != null && Date.now() - start >= CLOCK_MAX_MS
  const tick = useRunClock(!long)
  const slow = useNow(60_000, long)
  if (start == null) return <span className="tnum">{prefix}—</span>
  const ms = Math.max(0, (long ? Math.max(slow, Date.now()) : tick) - start)
  return <span className="tnum">{prefix}{long ? formatSpan(ms) : formatClock(ms)}</span>
}

/** 秒表读数只用到一小时以内；再长就换成「X 小时 YY 分」「X 天 YY 小时」 */
export const CLOCK_MAX_MS = 3_600_000

/**
 * 慢节拍的「现在」：等了几天的审批按分钟刷新就够了，用不着 100ms 一拍的运行
 * 时钟。页面不可见时不刷。
 */
export function useNow(intervalMs: number, active = true): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    const t = setInterval(() => { if (!document.hidden) setNow(Date.now()) }, intervalMs)
    return () => clearInterval(t)
  }, [intervalMs, active])
  return now
}

// -------------------------------------------------------------------------
// 复制
// -------------------------------------------------------------------------

export async function copyText(text: string, what: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text)
    toast.ok(`已复制${what}`)
  } catch {
    toast.error('复制失败：浏览器没有给剪贴板权限')
  }
}

/** 一段可复制的等宽文字（哈希、id）。复制后图标变勾一下 */
export function CopyValue({ value, label, display, className }: {
  value: string; label: string; display?: ReactNode; className?: string
}) {
  const [done, setDone] = useState(false)
  return (
    <button
      type="button"
      className={clsx('group inline-flex min-w-0 items-center gap-1 rounded px-1 text-left hover:bg-hover', className)}
      title={`${value}\n点击复制${label}`}
      aria-label={`复制${label}`}
      onClick={() => {
        void navigator.clipboard?.writeText(value).then(() => {
          setDone(true)
          setTimeout(() => setDone(false), 1200)
        }, () => toast.error('复制失败：浏览器没有给剪贴板权限'))
      }}
    >
      <span className="mono min-w-0 truncate">{display ?? value}</span>
      {done
        ? <Check size={11} className="shrink-0 text-[var(--ok)]" aria-hidden />
        : <Copy size={11} className="shrink-0 opacity-0 transition-opacity group-hover:opacity-70 group-focus-visible:opacity-70" aria-hidden />}
    </button>
  )
}

// -------------------------------------------------------------------------
// 「⋯」菜单
// -------------------------------------------------------------------------

export interface MenuItem {
  key: string
  label: string
  icon?: ReactNode
  onSelect: () => void
  disabled?: boolean
  /** 禁用时说明为什么 */
  hint?: string
  danger?: boolean
}

/**
 * 低频和危险的动作收在这里：删除审计记录不该紧挨着常用按钮、一眼就能误点。
 * 键盘：↑↓ 移动，Esc 关闭并把焦点还给按钮。
 */
export function MoreMenu({ items, label = '更多操作' }: { items: MenuItem[]; label?: string }) {
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const list = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (!root.current?.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    // 打开后焦点落到第一个能点的项
    list.current?.querySelector<HTMLButtonElement>('[role=menuitem]:not([disabled])')?.focus()
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  const onKey = (e: KeyboardEvent) => {
    if (e.key === 'Escape') {
      e.preventDefault()
      setOpen(false)
      trigger.current?.focus()
      return
    }
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
    e.preventDefault()
    const els = [...(list.current?.querySelectorAll<HTMLButtonElement>('[role=menuitem]:not([disabled])') ?? [])]
    const i = els.indexOf(document.activeElement as HTMLButtonElement)
    const next = e.key === 'ArrowDown' ? (i + 1) % els.length : (i - 1 + els.length) % els.length
    els[next]?.focus()
  }

  return (
    <div ref={root} className="relative" onKeyDown={onKey}>
      <button
        ref={trigger}
        type="button"
        className="btn btn-sm btn-ghost"
        aria-label={label}
        title={label}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <MoreHorizontal size={13} aria-hidden />
      </button>
      {open && (
        <div
          ref={list}
          role="menu"
          aria-label={label}
          className="fade-up absolute right-0 top-full z-30 mt-1 w-52 rounded-lg border bg-elev py-1 shadow-elev-2"
        >
          {items.map((it) => (
            <button
              key={it.key}
              type="button"
              role="menuitem"
              disabled={it.disabled}
              title={it.disabled ? it.hint : undefined}
              data-menu={it.key}
              onClick={() => { setOpen(false); it.onSelect() }}
              className={clsx(
                'flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs outline-none',
                'hover:bg-hover focus-visible:bg-hover disabled:cursor-not-allowed disabled:opacity-45',
                it.danger ? 'text-[var(--err)]' : 'text-fg',
              )}
            >
              <span className="flex w-3.5 shrink-0 justify-center" aria-hidden>{it.icon}</span>
              <span className="min-w-0 flex-1">
                {it.label}
                {it.disabled && it.hint && <span className="block text-2xs text-faint">{it.hint}</span>}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
