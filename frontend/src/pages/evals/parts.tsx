import { useMemo } from 'react'
import clsx from 'clsx'
import { StatusPill } from '../../components/ui'
import { modelOptions, useCatalog } from '../../store/catalog'
import { formatCost, formatDateTime, formatNumber } from '../../lib/format'
import { EVAL_TEXT as T, pct, rateText } from '../../lib/termsEval'
import { runCostText } from '../../lib/terms'
import type {
  EvalExpectation, EvalGoldValue, EvalModelPair, EvalRate, EvalStatus, EvalTrendPoint, EvalUsage, EvalVerdict,
} from '../../typesEval'

/*
 * 评测页几处共用的小件：状态、结论、比率、趋势、模型选择、期望的说法、文件下载。只负责显示：比率、结论、能不能做都是
 * 服务端交来的（界面层不放逻辑）。
 */

/** 评测、目录快照的状态 → 外壳状态剪影（颜色和图标）；文字用评测自己的叫法 */
const PILL: Record<string, string> = {
  queued: 'queued', running: 'running', succeeded: 'succeeded', partial: 'skipped', failed: 'failed',
  cancelled: 'cancelled', interrupted: 'suspended',
}

export function EvalStatusPill({ status, label, plain = false }: { status: EvalStatus | string; label?: string; plain?: boolean }) {
  return (
    <span data-eval-status={status} className="inline-flex">
      <StatusPill status={PILL[status] ?? 'idle'} label={label ?? T.status[status as EvalStatus] ?? status} plain={plain} />
    </span>
  )
}

const VERDICT_TONE: Record<EvalVerdict, string> = {
  pass: 'var(--st-done)', fail: 'var(--st-failed)', ungradable: 'var(--st-waiting)', skipped: 'var(--text-faint)',
}

/** 题目结论：通过 / 不通过 / 不能评分 / 跳过。没有结论（还没评）写这一项的进度 */
export function VerdictChip({ verdict, fallback }: { verdict: EvalVerdict | null | undefined; fallback?: string }) {
  if (!verdict) return <span className="chip shrink-0 text-faint" data-eval-verdict="none">{fallback ?? '—'}</span>
  const color = VERDICT_TONE[verdict]
  return (
    <span className="chip shrink-0" data-eval-verdict={verdict}
          style={{ color, borderColor: `color-mix(in srgb, ${color} 40%, transparent)`,
            background: `color-mix(in srgb, ${color} 10%, transparent)` }}>
      {T.verdict[verdict]}
    </span>
  )
}

/** 卡片里的一个比率：大字是百分数，下面写「3 / 4 题（95% 区间 …）」 */
export function RateCard({ title, hint, rate, attr }: { title: string; hint?: string; rate: EvalRate | null | undefined; attr?: string }) {
  return (
    <div className="min-w-0 rounded-lg border bg-panel p-3" title={hint} data-eval-card={attr}>
      <div className="text-2xs text-faint">{title}</div>
      <div className="tnum mt-1 text-lg font-semibold">{rate && rate.n ? pct(rate.rate) : '—'}</div>
      <div className="mt-0.5 text-2xs text-dim">{rateText(rate)}</div>
    </div>
  )
}

/** 一张小卡片：标题、主值、补充 */
export function StatCard({ title, value, sub, hint, attr }: { title: string; value: string; sub?: string; hint?: string; attr?: string }) {
  return (
    <div className="min-w-0 rounded-lg border bg-panel p-3" title={hint} data-eval-card={attr}>
      <div className="text-2xs text-faint">{title}</div>
      <div className="tnum mt-1 truncate text-lg font-semibold">{value}</div>
      {sub && <div className="mt-0.5 text-2xs leading-relaxed text-dim">{sub}</div>}
    </div>
  )
}

/**
 * 同一变体最近几次的准确率（服务端挑好、旧的在前）：一排小竖条，高度就是那一次的准确率。悬停写每一次的时间和比率
 */
export function Trend({ points }: { points: EvalTrendPoint[] | undefined }) {
  const list = points ?? []
  if (!list.length) return <span className="text-2xs text-faint">—</span>
  const title = T.list.trendTitle(list.map((p) => T.list.trendPoint(formatDateTime(p.created_at ?? null), pct(p.rate ?? null))).join('，'))
  return (
    <span className="inline-flex h-5 items-end gap-0.5" title={title} aria-label={title} role="img" data-eval-trend={list.length}>
      {list.map((p) => (
        <span key={p.id} className="w-1.5 rounded-sm bg-[var(--accent)]"
              style={{ height: `${Math.max(8, Math.round((p.rate ?? 0) * 100))}%`, opacity: p.rate == null ? 0.3 : 1 }} />
      ))}
    </span>
  )
}

/** 一份用量的金额：估不出的照实写（同运行的写法） */
export function usageCost(u: EvalUsage | null | undefined): string {
  if (!u) return '—'
  return runCostText(u.cost_usd ?? null, u.unpriced_calls ?? 0) ?? formatCost(0)
}

/**
 * 模型选择：缺省（按设置）或某个启用接入的某个模型。值是 {provider, model}，两项都空就是缺省
 */
export function ModelPick({ id, value, onChange, defaultLabel }: {
  id: string; value: EvalModelPair; onChange: (v: EvalModelPair) => void; defaultLabel: string
}) {
  const providers = useCatalog((s) => s.providers)
  const options = useMemo(() => modelOptions(providers), [providers])
  const key = value.provider || value.model ? `${value.provider ?? ''}::${value.model ?? ''}` : ''
  const groups = [...new Set(options.map((o) => o.group))]
  const known = !key || options.some((o) => `${o.group}::${o.value}` === key)
  return (
    <select id={id} className="field" value={key} data-model-pick={id}
            onChange={(e) => {
              const v = e.target.value
              if (!v) return onChange({ provider: null, model: null })
              const at = v.indexOf('::')
              onChange({ provider: v.slice(0, at) || null, model: v.slice(at + 2) || null })
            }}>
      <option value="">{defaultLabel}</option>
      {!known && <option value={key}>{value.model ?? value.provider}</option>}
      {groups.map((g) => (
        <optgroup key={g} label={g}>
          {options.filter((o) => o.group === g).map((o) => <option key={`${g}::${o.value}`} value={`${g}::${o.value}`}>{o.label}</option>)}
        </optgroup>
      ))}
    </select>
  )
}

/** 一个格子里的值写成字：数字照原样加千分位，别的原样 */
export function cellText(v: unknown): string {
  if (v == null) return '—'
  if (typeof v === 'number') return Number.isInteger(v) ? formatNumber(v) : String(v)
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

/** 一个期望是从哪里取什么：「「gmv」第 1 行」「指标「gmv」」「按「地区」配对的多行」…… */
export function expectationText(e: EvalExpectation): string {
  const E = T.expectation
  let text: string
  switch (e.kind) {
    case 'scalar':
      text = e.metric ? E.scalarMetric(e.metric)
        : e.where && Object.keys(e.where).length
          ? E.scalarWhere(e.column ?? '?', Object.entries(e.where).map(([k, v]) => `${k}=${v}`).join('、'))
          : E.scalarCell(e.column ?? '?', e.row ?? 0)
      break
    case 'set': text = E.set(e.column ?? '?'); break
    case 'table': text = E.table((e.keys ?? []).join('、')); break
    case 'none': text = E.none; break
    case 'ask': text = E.ask; break
    case 'text': text = E.text(e.points?.length ?? 0); break
    default: text = T.expectation.kind[e.kind as keyof typeof T.expectation.kind] ?? String(e.kind)
  }
  const extra = [
    e.label,
    e.required === false && e.kind !== 'text' ? E.optional : null,
    e.tolerance != null ? E.tolerance(e.tolerance) : null,
  ].filter(Boolean)
  return extra.length ? `${text}（${extra.join('，')}）` : text
}

/** 试算算出来的一份期望值写成字（值都是服务端算好的） */
export function goldValueText(v: EvalGoldValue | undefined): string {
  if (!v) return '—'
  const G = T.goldValue
  switch (v.kind) {
    case 'scalar': {
      const dims = v.dims && Object.keys(v.dims).length
        ? G.dims(Object.entries(v.dims).map(([k, x]) => `${k}=${cellText(x)}`).join('、')) : ''
      return `${v.rendered || G.scalar(cellText(v.value), v.unit)}${dims}`
    }
    case 'set': return `${G.set(Array.isArray(v.values) ? v.values.length : 0)}：${(v.values ?? []).slice(0, 8).map(cellText).join('、')}`
    case 'table': return G.table(Array.isArray(v.rows) ? v.rows.length : 0)
    case 'none': return G.none
    default: return '—'
  }
}

/** 存成文件（同运行记录、数据目录的下载）：浏览器下载，文件名照给的 */
export function saveFile(name: string, blob: Blob) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = name
  a.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

/** 一行「标签：值」的小表 */
export function Facts({ rows, className, attr }: { rows: [string, string][]; className?: string; attr?: string }) {
  return (
    <dl className={clsx('grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-xs', className)} data-eval-facts={attr}>
      {rows.map(([label, value]) => (
        <div key={label} className="contents">
          <dt className="text-faint">{label}</dt>
          <dd className="min-w-0 break-words text-fg">{value}</dd>
        </div>
      ))}
    </dl>
  )
}
