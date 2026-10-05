import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { AlertTriangle } from 'lucide-react'
import { ErrorState } from '../../../components/ui'
import { RUN_COMPARE_TEXT as T } from '../../../lib/terms'
import { classTone, type CompareErrorKind } from './model'

/** 对比页的一节：带标题的卡片。count 写在标题旁（「11 个」），hint 是一句说明 */
export function Block({ id, title, count, hint, children, attr }: {
  id: string; title: string; count?: string; hint?: ReactNode; children: ReactNode
  attr?: Record<string, string>
}) {
  return (
    <section aria-labelledby={`${id}-title`} className="min-w-0 rounded-lg border bg-panel" {...attr}>
      <header className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 border-b px-3 py-2">
        <h2 id={`${id}-title`} className="text-sm font-semibold">{title}</h2>
        {count && <span className="tnum text-2xs text-faint">{count}</span>}
        {hint && <p className="min-w-0 text-2xs leading-relaxed text-faint">{hint}</p>}
      </header>
      <div className="min-w-0 p-3">{children}</div>
    </section>
  )
}

/**
 * 标签的配色：字和边框用这个颜色；soft 时再铺一层 12% 的底。铺了底的字往正文色里调一成半：浅色主题下警示色的字压在
 * 自己的浅底上只有 4.4:1，调过之后深浅两套都在 4.5 以上（深色里正文色是浅的，调完字更亮）
 */
function toneStyle(color: string, soft: boolean) {
  return {
    color: soft ? `color-mix(in srgb, ${color} 85%, var(--text))` : color,
    borderColor: `color-mix(in srgb, ${color} 40%, transparent)`,
    background: soft ? `color-mix(in srgb, ${color} 12%, transparent)` : undefined,
  }
}

/** 指标的分类标签。要多看一眼的（口径、SQL 结构变了）铺一层底色，和出具档位的标签同一种画法 */
export function ClassChip({ value }: { value: string }) {
  const tone = classTone(value)
  return (
    <span className="chip shrink-0" data-compare-chip="" title={T.classHint[value]} style={toneStyle(tone.color, tone.soft)}>
      {T.classLabel[value] ?? T.classOther}
    </span>
  )
}

/** 查询的结构状态。结构变了用警示色并铺底（口径不同），新增用强调色，其余中性色 */
export function StructureChip({ value }: { value: string }) {
  const warn = value === 'changed'
  const color = warn ? 'var(--warn)' : value === 'added' ? 'var(--accent)' : 'var(--text-dim)'
  return (
    <span className="chip shrink-0" data-compare-structure="" title={T.structureHint[value]} style={toneStyle(color, warn)}>
      {T.structure[value] ?? value}
    </span>
  )
}

const ERROR_TEXT: Record<Exclude<CompareErrorKind, 'other'>, { title: string; hint: string }> = {
  '409': { title: T.errSealed, hint: T.errSealedHint },
  '422': { title: T.errMismatch, hint: T.errMismatchHint },
  '404': { title: T.errMissing, hint: T.errMissingHint },
  'no-base': { title: T.errNoBase, hint: T.errNoBaseHint },
}

/**
 * 对比失败时的说明：先说是哪一种（未封存、不是同一工作流、运行不存在、找不到基准），再原样带上服务端的原因，
 * 最后一句下一步。未封存、封存核对不一致的，给两次运行详情的入口：封存凭证条上能核对。别的错误按通用的出错态画
 */
export function CompareError({ kind, error, onRetry, target, base }: {
  kind: CompareErrorKind; error: unknown; onRetry: () => void; target: string | null; base: string | null
}) {
  if (kind === 'other') {
    return (
      <div data-compare-error="other">
        <ErrorState error={error} onRetry={onRetry} compact />
      </div>
    )
  }
  const { title, hint } = ERROR_TEXT[kind]
  const reason = error instanceof Error ? error.message : ''
  return (
    <div
      role="alert"
      data-compare-error={kind}
      className="flex items-start gap-2 rounded-lg border px-3 py-2.5 text-xs leading-relaxed"
      style={{
        borderColor: 'color-mix(in srgb, var(--warn) 40%, var(--border))',
        background: 'color-mix(in srgb, var(--warn) 7%, transparent)',
      }}
    >
      <AlertTriangle size={14} className="mt-0.5 shrink-0" style={{ color: 'var(--warn)' }} aria-hidden />
      <div className="min-w-0 flex-1 [overflow-wrap:anywhere]">
        <div className="font-medium text-fg" data-compare-error-title="">{title}</div>
        {reason && reason !== title && <div className="mt-0.5 text-dim">{reason}</div>}
        <div className="mt-0.5 text-faint">{hint}</div>
        {kind === '409' && (target || base) && (
          <div className="mt-1.5 flex flex-wrap gap-2">
            {base && <Link className="btn btn-sm" to={`/runs/${base}`}>{T.viewBase}</Link>}
            {target && <Link className="btn btn-sm" to={`/runs/${target}`}>{T.viewTarget}</Link>}
          </div>
        )}
      </div>
    </div>
  )
}
