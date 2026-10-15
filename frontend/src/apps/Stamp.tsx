/**
 * 印章（应用平台 §2.3）：每个视图都带的「截至 / 口径 / 出具档位 / 来源」，服务端算好、这里只照写（P1）。
 *
 * 截至那一项服务端给的是整句（as_of_text：「截至 2026-10-15 07:29；统计期 …」，没结束的写「尚未结束」），紧凑和完整两种
 * 都照写这一句、不再另加「截至」的标签：标签加上值里的「截至」就成了「截至 截至 …」。
 *
 * 出具档位按三档配色（完整出具绿、降档琥珀、不予出具红），旁边总有字，不只靠颜色；还没封存的写明「尚未封存」。
 * 服务端照实标出的错位（定时任务已改运行新版本、应用还固定在旧版本，引用的版本已退役……）逐条列在下面，琥珀色。
 */
import type { ReactNode } from 'react'
import { Stamp as StampIcon } from 'lucide-react'
import clsx from 'clsx'
import { STAMP_TEXT } from '../lib/termsApp'
import type { Stamp as StampData } from '../typesApp'

const TIER_COLOR: Record<string, string> = {
  formal: 'var(--st-done)',
  degraded: 'var(--st-waiting)',
  withheld: 'var(--err)',
}

export function Stamp({ stamp, compact = false, className }: { stamp: StampData; compact?: boolean; className?: string }) {
  const caliber = [
    STAMP_TEXT.pinned(stamp.pin, stamp.graph_hash.slice(0, 8)),
    ...stamp.calibers.map((c) => `${c.name} ${c.version}`),
  ].join(' · ')
  const tier = stamp.tier ? TIER_COLOR[stamp.tier] : undefined
  const notes = stamp.notes ?? []
  if (compact) {
    return (
      <div className={clsx('flex flex-wrap items-center gap-x-2 gap-y-0.5 text-2xs text-dim', className)}
           data-stamp="compact" data-stamp-tier={stamp.tier ?? ''} data-stamp-sealed={stamp.sealed ? '1' : '0'}>
        <StampIcon size={11} aria-hidden className="text-faint" />
        <span>{stamp.as_of_text}</span>
        <span aria-hidden>·</span>
        <span className="mono">{caliber}</span>
        {stamp.tier_text && <><span aria-hidden>·</span><span style={tier ? { color: tier } : undefined}>{stamp.tier_text}</span></>}
        <span aria-hidden>·</span>
        <span>{stamp.from}</span>
        {!stamp.sealed && <span className="chip" style={{ color: 'var(--st-waiting)' }}>{STAMP_TEXT.unsealed}</span>}
        {notes.map((n, i) => <span key={i} style={{ color: 'var(--st-waiting)' }} data-stamp-note="">{n}</span>)}
      </div>
    )
  }
  const rows: [string, ReactNode, string][] = [
    [STAMP_TEXT.caliber, <span className="mono">{caliber}</span>, 'caliber'],
    [STAMP_TEXT.tier, <span style={tier ? { color: tier, fontWeight: 600 } : undefined}>{stamp.tier_text || '—'}</span>, 'tier'],
    [STAMP_TEXT.from, stamp.from, 'from'],
  ]
  return (
    <div className={clsx('rounded-md border border-dashed px-2.5 py-2 text-2xs', className)}
         data-stamp="" data-stamp-tier={stamp.tier ?? ''} data-stamp-sealed={stamp.sealed ? '1' : '0'}>
      {/* 截至：服务端的整句自己带着「截至」，单独一行写，不进下面带标签的三项 */}
      <p className="mb-0.5 min-w-0 break-words text-fg" data-stamp-row="as_of">{stamp.as_of_text}</p>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
        {rows.map(([label, value, key]) => (
          <div key={key} className="contents" data-stamp-row={key}>
            <dt className="text-faint">{label}</dt>
            <dd className="min-w-0 break-words text-fg">{value}</dd>
          </div>
        ))}
      </dl>
      <div className="mt-1 flex items-center gap-1 text-faint">
        <StampIcon size={10} aria-hidden />
        <span style={stamp.sealed ? undefined : { color: 'var(--st-waiting)' }}>{stamp.sealed ? STAMP_TEXT.sealed : STAMP_TEXT.unsealed}</span>
      </div>
      {notes.length > 0 && (
        <ul className="mt-1 space-y-0.5" style={{ color: 'var(--st-waiting)' }}>
          {notes.map((n, i) => <li key={i} data-stamp-note="">{n}</li>)}
        </ul>
      )}
    </div>
  )
}
