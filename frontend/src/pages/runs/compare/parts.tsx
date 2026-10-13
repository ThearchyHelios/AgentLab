import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { AlertTriangle } from 'lucide-react'
import { ApiError } from '../../../api/client'
import { ErrorState } from '../../../components/ui'
import { RUN_COMPARE_TEXT as T } from '../../../lib/terms'
import { SEAL_BROKEN, UNSEALED, classTone, type CompareErrorKind } from './model'

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

/** 查询的结构状态。结构变了用警示色并铺底（数不能直接比），新增用强调色，其余中性色 */
export function StructureChip({ value }: { value: string }) {
  const warn = value === 'changed'
  const color = warn ? 'var(--warn)' : value === 'added' ? 'var(--accent)' : 'var(--text-dim)'
  return (
    <span className="chip shrink-0" data-compare-structure="" title={T.structureHint[value]} style={toneStyle(color, warn)}>
      {T.structure[value] ?? value}
    </span>
  )
}

/** 页面上已经知道的几件事：错误说明的下一步按它挑，只指向这一刻真走得通的路 */
export interface CompareErrorContext {
  /** 本次的运行记录读到了 */
  targetKnown: boolean
  /** 本次不属于已保存的工作流（没保存过、或者工作流已删除）：和哪一次都比不了 */
  targetUnsaved: boolean
  /** 本次的运行记录上没有封存清单 */
  targetUnsealed: boolean
  /** 本次、基准的运行记录是不是 404 */
  targetGone: boolean
  baseGone: boolean
  /** 换基准的下拉里有没有能换的运行（已封存、不是本次、也不是当前的基准）；还不知道（在读、读失败）时为 null */
  pickable: boolean | null
}

/**
 * 「可在上方选择」这类下一步只在下拉里真有能换的运行时说：一次都没有时照实说没有（找默认基准时说「还没有」，换基准时说
 * 「没有其他可换的」），还不知道（在读、读失败）时先不说
 */
function pickHint(ctx: CompareErrorContext, hint: string, none: string): string | null {
  return ctx.pickable == null ? null : ctx.pickable ? hint : none
}

/**
 * 标题和下一步。标题说是哪一种；服务端的原因原样跟在标题后面（CompareError 里），下一步只写界面上真有的入口、
 * 服务端已经写了的不再重复：
 * - 409：按机读码分没结束封存、封存核对不一致。本次自己没封存时换基准也没用，不给下一步；否则说出在基准上时可以
 *   在上方换（服务端的原因里写着是哪一次）。去记录页核对的入口是下面两个「查看」按钮，服务端的原因里也写了「核对」；
 * - 422：本次不属于已保存的工作流时哪一次都比不了，标题直说、不给下一步；否则在上方另选同一工作流的一次；
 * - 404：缺的是本次（targetGone）才是链接失效；缺的是基准（基准的记录 404，或者本次的记录读得到）就在上方另选；
 *   还不知道是哪一边时先不说；
 * - 找不到默认基准：本次不属于已保存的工作流同 422；下拉里有能选的才说可以在上方选，一次都没有就照实说
 * 叫人去上方换基准的几句（409、422、缺基准的 404、找不到默认基准）都先看下拉里有没有能换的（pickHint）
 */
function errorCopy(kind: Exclude<CompareErrorKind, 'other'>, error: unknown, targetGone: boolean, ctx: CompareErrorContext): {
  title: string; hint: string | null
} {
  const code = error instanceof ApiError ? error.code : undefined
  switch (kind) {
    case '409': {
      const title = code === SEAL_BROKEN ? T.errSealBroken : code === UNSEALED ? T.errUnsealed : T.errSealed
      return { title, hint: code === UNSEALED && ctx.targetUnsealed ? null : pickHint(ctx, T.errSealedHint, T.errNoOtherBase) }
    }
    case '422':
      return ctx.targetUnsaved ? { title: T.errUnsaved, hint: null }
        : { title: T.errMismatch, hint: pickHint(ctx, T.errMismatchHint, T.errNoOtherBase) }
    case '404':
      return {
        title: T.errMissing,
        hint: targetGone ? T.errMissingHint
          : ctx.baseGone || ctx.targetKnown ? pickHint(ctx, T.errBaseMissingHint, T.errNoOtherBase) : null,
      }
    case 'no-base':
      if (ctx.targetUnsaved) return { title: T.errUnsaved, hint: null }
      return { title: T.errNoBase, hint: pickHint(ctx, T.errNoBaseHint, T.errNoBaseNone) }
  }
}

/**
 * 对比失败时的说明：先说是哪一种（未封存、封存核对不一致、不是同一工作流、运行不存在、找不到基准），再原样带上
 * 服务端的原因，最后一句下一步（errorCopy）。未封存、封存核对不一致的，给两次运行详情的入口（运行详情上有「核对」）；
 * 本次的记录不存在时给去记录页的入口。别的错误按通用的出错态画，带重试
 */
export function CompareError({ kind, error, onRetry, target, base, context }: {
  kind: CompareErrorKind; error: unknown; onRetry: () => void; target: string | null; base: string | null
  context: CompareErrorContext
}) {
  if (kind === 'other') {
    return (
      <div data-compare-error="other">
        <ErrorState error={error} onRetry={onRetry} compact />
      </div>
    )
  }
  // 缺的是不是本次：只带本次的地址（还没有基准）出的错来自找默认基准，那里的 404 只会是本次
  const targetGone = !base || context.targetGone
  const { title, hint } = errorCopy(kind, error, targetGone, context)
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
        {hint && <div className="mt-0.5 text-faint" data-compare-error-hint="">{hint}</div>}
        {kind === '409' && (target || base) && (
          <div className="mt-1.5 flex flex-wrap gap-2">
            {base && <Link className="btn btn-sm" to={`/runs/${base}`}>{T.viewBase}</Link>}
            {target && <Link className="btn btn-sm" to={`/runs/${target}`}>{T.viewTarget}</Link>}
          </div>
        )}
        {kind === '404' && targetGone && (
          <div className="mt-1.5"><Link className="btn btn-sm" to="/runs">{T.toRuns}</Link></div>
        )}
      </div>
    </div>
  )
}
