import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent, ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Check, CircleCheck, CircleDashed, CircleX, RotateCcw, ShieldCheck, Trash2, X } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import clsx from 'clsx'
import type { CatalogCounts, CatalogReviewAction, CatalogSource, CatalogStatus } from '../../types'
import { Notice } from '../../components/ui'
import { formatDateTime, formatNumber } from '../../lib/format'
import {
  CATALOG_SOURCE_LABEL, CATALOG_STATUS_HINT, CATALOG_STATUS_LABEL, CATALOG_TEXT as CT,
} from '../../lib/terms'
import { STATUSES, initialStatus } from './model'

// ===========================================================================
// 数据目录的公共小件：状态标识（四种状态一套样式，颜色走语义令牌）、清单上的状态计数、图例、
// 单项的来源和状态弹层（确认 / 驳回 / 恢复）。
// ===========================================================================

/**
 * 四种状态的颜色和图标。推断要人去看，用「等待」的琥珀；已验证（外键约束、数据剖析）是机器给的确证，用强调蓝；
 * 已确认是人工拍板，用「完成」的绿；已驳回用「失败」的红，值划掉
 */
export const STATUS_TONE: Record<CatalogStatus, string> = {
  proposed: 'var(--st-waiting)',
  verified: 'var(--accent)',
  confirmed: 'var(--st-done)',
  rejected: 'var(--st-failed)',
}
const STATUS_ICON: Record<CatalogStatus, LucideIcon> = {
  proposed: CircleDashed,
  verified: ShieldCheck,
  confirmed: CircleCheck,
  rejected: CircleX,
}

export function StatusIcon({ status, size = 12 }: { status: CatalogStatus; size?: number }) {
  const Icon = STATUS_ICON[status]
  return <Icon size={size} className="shrink-0" style={{ color: STATUS_TONE[status] }} aria-hidden />
}

/** 状态的文字标识：图标 + 「推断」，可带来源「· 命名推断」 */
export function StatusChip({ status, source, className }: { status: CatalogStatus; source?: CatalogSource; className?: string }) {
  const tone = STATUS_TONE[status]
  return (
    <span
      className={clsx('inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-full border px-1.5 text-2xs leading-[18px]', className)}
      style={{ color: tone, borderColor: `color-mix(in srgb, ${tone} 40%, var(--border))`, background: `color-mix(in srgb, ${tone} 8%, transparent)` }}
      data-status={status}
    >
      <StatusIcon status={status} size={11} />
      {CATALOG_STATUS_LABEL[status]}
      {source && <span className="text-faint">· {CATALOG_SOURCE_LABEL[source]}</span>}
    </span>
  )
}

/** 清单一行上的状态计数：只列有项的状态，读屏念「推断 12 项」 */
export function CountBadges({ counts, className }: { counts: CatalogCounts; className?: string }) {
  const shown = STATUSES.filter((s) => counts[s] > 0)
  if (!shown.length) return null
  return (
    <span className={clsx('inline-flex items-center gap-2 text-2xs', className)} data-counts={STATUSES.map((s) => counts[s]).join(',')}>
      {shown.map((s) => (
        <span key={s} className="tnum inline-flex items-center gap-0.5" style={{ color: STATUS_TONE[s] }}
              title={CT.countTitle(CATALOG_STATUS_LABEL[s], counts[s])}>
          <StatusIcon status={s} size={11} />
          <span aria-hidden>{formatNumber(counts[s])}</span>
          <span className="sr-only">{CT.countTitle(CATALOG_STATUS_LABEL[s], counts[s])}</span>
        </span>
      ))}
    </span>
  )
}

/** 四种状态的图例：列表格里只画图标，图例就近说明每种状态意味着什么 */
export function StatusLegend({ className, detailed = false }: { className?: string; detailed?: boolean }) {
  return (
    <ul className={clsx(detailed ? 'space-y-1.5' : 'flex flex-wrap items-center gap-x-3 gap-y-1', 'text-2xs text-faint', className)}
        aria-label={CT.legend} data-status-legend="">
      {STATUSES.map((s) => (
        <li key={s} className="flex items-start gap-1.5">
          <span className="mt-0.5"><StatusIcon status={s} size={11} /></span>
          <span>
            <span style={{ color: STATUS_TONE[s] }}>{CATALOG_STATUS_LABEL[s]}</span>
            {detailed && <span>：{CATALOG_STATUS_HINT[s]}</span>}
          </span>
        </li>
      ))}
    </ul>
  )
}

export interface ReviewTarget {
  /** 审阅路径 */
  path: string
  /** 给人看的位置：「表的粒度」「列 amount 的度量类型」 */
  where: string
  source: CatalogSource
  status: CatalogStatus
  note?: string
  updated_at?: string
}

/**
 * 单项的来源和状态。触发按钮就是状态标识（compact 时只有图标，列表格里每格一个）；点开是一个小弹层：
 * 位置、来源、状态说明、更新时间，以及确认 / 驳回 / 恢复。弹层挂在 body 上、按触发按钮的位置固定定位，
 * 不会被表格的横向滚动裁掉；滚动时跟着触发按钮走，滚出视野或改窗口大小时收起。键盘：Enter 打开，焦点落到第一个可点的操作，
 * ←→↑↓ 在操作之间移动，Tab 在弹层里循环，Esc 收起并把焦点还给触发按钮
 */
export function ItemMark({ target, compact = false, disabled = false, disabledHint, onReview }: {
  target: ReviewTarget
  compact?: boolean
  /** 正在提交别的操作、正在编辑：弹层照常能看，操作按钮禁用 */
  disabled?: boolean
  /** 禁用的原因，写在操作按钮下面 */
  disabledHint?: string
  onReview: (action: CatalogReviewAction) => void
}) {
  const [open, setOpen] = useState(false)
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLDivElement>(null)
  const titleId = useId()
  const { status, source } = target
  const human = source === 'human'
  // 非人工的项：已经是来源的初始状态时「恢复」没有意义
  const canReset = human || status !== initialStatus(source)

  const close = (focusBack = true) => {
    setOpen(false)
    setPos(null)
    if (focusBack) trigger.current?.focus()
  }

  // 先按触发按钮下方放，量过弹层的高度后，下方放不下就翻到上方。返回 false 表示触发按钮已经滚出视野
  const place = (): boolean => {
    const r = trigger.current?.getBoundingClientRect()
    const el = panel.current
    if (!r || !el) return false
    if (r.bottom < 0 || r.top > window.innerHeight) return false
    const w = el.offsetWidth
    const h = el.offsetHeight
    const left = Math.max(8, Math.min(r.left, window.innerWidth - w - 8))
    const below = r.bottom + 4
    const top = below + h > window.innerHeight - 8 && r.top - h - 4 > 8 ? r.top - h - 4 : below
    setPos((p) => (p && p.top === top && p.left === left ? p : { top, left }))
    return true
  }
  const placeRef = useRef(place)
  placeRef.current = place

  useLayoutEffect(() => {
    if (open) placeRef.current()
  }, [open])

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node
      if (!panel.current?.contains(t) && !trigger.current?.contains(t)) close(false)
    }
    // 页面或表格滚动时跟着触发按钮走；滚出视野就收起
    const onScroll = (e: Event) => {
      if (panel.current?.contains(e.target as Node)) return
      if (!placeRef.current()) close(false)
    }
    const onResize = () => close(false)
    document.addEventListener('mousedown', onDown)
    window.addEventListener('scroll', onScroll, true)
    window.addEventListener('resize', onResize)
    return () => {
      document.removeEventListener('mousedown', onDown)
      window.removeEventListener('scroll', onScroll, true)
      window.removeEventListener('resize', onResize)
    }
  }, [open])

  // 定好位置以后把焦点交给第一个能点的操作（只在打开那一下）
  const placed = !!pos
  useEffect(() => {
    if (!open || !placed) return
    const first = panel.current?.querySelector<HTMLButtonElement>('[data-review-action]:not([disabled])')
    ;(first ?? panel.current)?.focus({ preventScroll: true })
  }, [open, placed])

  const onKey = (e: ReactKeyboardEvent) => {
    if (e.key === 'Escape') {
      e.preventDefault()
      e.stopPropagation()
      close()
      return
    }
    const els = [...(panel.current?.querySelectorAll<HTMLButtonElement>('button:not([disabled])') ?? [])]
    if (!els.length) return
    const i = els.indexOf(document.activeElement as HTMLButtonElement)
    if (e.key === 'Tab') {
      e.preventDefault()
      els[(i + (e.shiftKey ? -1 : 1) + els.length) % els.length]?.focus()
    } else if (['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp'].includes(e.key)) {
      e.preventDefault()
      const step = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : -1
      els[(i + step + els.length) % els.length]?.focus()
    }
  }

  const act = (a: CatalogReviewAction) => {
    close()
    onReview(a)
  }

  const label = `${target.where}：${CATALOG_STATUS_LABEL[status]}，${CT.itemSource(CATALOG_SOURCE_LABEL[source])}`
  const tone = STATUS_TONE[status]
  return (
    <>
      <button
        ref={trigger}
        type="button"
        className={clsx(
          'inline-flex shrink-0 items-center gap-1 rounded-full border text-2xs leading-[18px] outline-none',
          'focus-visible:ring-2 focus-visible:ring-[var(--accent)] hover:brightness-110',
          compact ? 'h-[18px] w-[18px] justify-center' : 'px-1.5',
        )}
        style={{ color: tone, borderColor: `color-mix(in srgb, ${tone} 40%, var(--border))`, background: `color-mix(in srgb, ${tone} 8%, transparent)` }}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={label}
        title={label}
        data-item-mark={target.path}
        data-status={status}
        onClick={() => (open ? close() : setOpen(true))}
      >
        <StatusIcon status={status} size={11} />
        {!compact && (
          <>
            {CATALOG_STATUS_LABEL[status]}
            <span className="text-faint">· {CATALOG_SOURCE_LABEL[source]}</span>
          </>
        )}
      </button>
      {open && createPortal(
        <div
          ref={panel}
          role="dialog"
          aria-labelledby={titleId}
          tabIndex={-1}
          onKeyDown={onKey}
          className="fade-up fixed z-[60] w-72 rounded-lg border bg-elev p-3 text-xs shadow-elev-2 outline-none"
          style={{ top: pos?.top ?? -9999, left: pos?.left ?? -9999 }}
          data-item-panel={target.path}
        >
          <div id={titleId} className="font-medium text-fg">{CT.itemTitle(target.where)}</div>
          <div className="mt-2 space-y-1.5 text-2xs leading-relaxed">
            <p className="flex min-w-0 flex-wrap items-center gap-1.5">
              <StatusChip status={status} />
              <span className="text-dim">{CATALOG_STATUS_HINT[status]}</span>
            </p>
            <p className="text-dim">{CT.itemSource(CATALOG_SOURCE_LABEL[source])}</p>
            {target.updated_at && <p className="tnum text-faint">{CT.itemUpdated(formatDateTime(target.updated_at))}</p>}
            {target.note && <p className="whitespace-pre-wrap break-words rounded border bg-bg px-2 py-1 text-dim">{target.note}</p>}
          </div>
          <div className="mt-3 flex flex-wrap gap-1.5">
            <ActionButton icon={<Check size={11} />} disabled={disabled || status === 'confirmed'} onClick={() => act('confirm')} action="confirm">
              {CT.confirm}
            </ActionButton>
            <ActionButton icon={<X size={11} />} disabled={disabled || status === 'rejected'} onClick={() => act('reject')} action="reject">
              {CT.reject}
            </ActionButton>
            <ActionButton icon={human ? <Trash2 size={11} /> : <RotateCcw size={11} />} disabled={disabled || !canReset}
                          onClick={() => act('reset')} action="reset" title={human ? CT.removeHint : CT.resetHint}>
              {human ? CT.remove : CT.reset}
            </ActionButton>
          </div>
          {disabled && disabledHint
            ? <p className="mt-2 text-2xs leading-relaxed text-faint" data-review-disabled="">{disabledHint}</p>
            : canReset && <p className="mt-2 text-2xs leading-relaxed text-faint">{human ? CT.removeHint : CT.resetHint}</p>}
        </div>,
        document.body,
      )}
    </>
  )
}

function ActionButton({ icon, children, disabled, onClick, action, title }: {
  icon: ReactNode; children: ReactNode; disabled: boolean; onClick: () => void; action: CatalogReviewAction; title?: string
}) {
  return (
    <button type="button" className="btn btn-sm" disabled={disabled} onClick={onClick} data-review-action={action} title={title}>
      <span aria-hidden>{icon}</span>
      {children}
    </button>
  )
}

/** 导入表格的源：说明是系统生成的，只读 */
export function SystemNotesNotice() {
  return (
    <Notice tone="info" attr={{ 'data-catalog-system-notes': '' }}>
      <p className="font-medium">{CT.systemTitle}</p>
      <p className="mt-0.5 text-dim">{CT.systemBody}</p>
      <p className="mt-0.5 text-dim">{CT.systemCovered}</p>
    </Notice>
  )
}
