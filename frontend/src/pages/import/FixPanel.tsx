import { useEffect, useId, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { ArrowLeft, Wrench } from 'lucide-react'
import { ApiError, api } from '../../api/client'
import type { EditPreview, EditRequest, FixProposal, ImportProblem, RecipeProblem, Staging } from '../../types'
import { Spinner } from '../../components/ui'
import { localActor } from '../../lib/actor'
import { errorMessage } from '../../lib/errors'
import { FIX_KIND_LABEL, RECIPE_TEXT } from '../../lib/terms'
import { RecipeCompare } from './RecipeCompare'
import { CellChips, ChoiceGroup } from './SuggestionCards'

// ===========================================================================
// 修复面板（P3-SPEC 3.1、10.2）：向导右栏里的内联面板，不用 Modal——Modal 的根节点是全屏遮罩，会挡住网格，
// 人就看不到要求标出的格子。流程：选项（封闭、无默认）→ 预览（服务端算补丁、干跑）→ 应用（带预览给的配方哈希）。
// 补丁 ops 是 /sheets/0/blocks/0/… 这类键名，不上界面
// ===========================================================================

/** 预览请求的序号：修复和框选共用一个递增的计数，回包的序号不是最新一次的就丢弃（切换选项时会连发几次） */
let previewSeq = 0

/**
 * 提议或框选对应的内容已不在当前工作配方里（P3-SPEC 10.2 错误表）：刷新暂存区、关掉面板。
 * 两条路都会遇到：409 fix_stale、422 edit_not_applicable，以及预览里 ok=false 的原因（fix_ops 换算不上时
 * 返回 edit_not_applicable，按 4.4 第 2 步 200 返回）。后者要是只把原因摆在面板里，人对着一个禁用的
 * 「应用」却不知道该重新打开问题，所以同样按错误表处理
 */
const GONE_CODES = new Set(['fix_stale', 'edit_not_applicable'])

/** edits/preview、edits/apply 的请求体（不含 seq） */
export type EditBody = Omit<Extract<EditRequest, { fix: unknown }>, 'seq'> | Omit<Extract<EditRequest, { selection: unknown }>, 'seq'>

export interface EditFlowHandlers {
  stagingId: string
  /** 应用成功：新的暂存区（试运行已失效，要重新试运行） */
  onApplied: (s: Staging) => void
  /** fix_stale / edit_not_applicable：问题已经变了，向导刷新暂存区、关掉面板。detail 是服务端的原话 */
  onGone: (detail?: string) => void
  /** 最近一次采用的预览（网格的「修改后」按它着色）；null 表示没有 */
  onPreview?: (p: EditPreview | null) => void
  /** 暂存区已结束等向导统一处理的错误；返回 true 表示处理了 */
  onError?: (e: unknown) => boolean
}

/**
 * 预览与应用的状态机，修复面板和框选面板共用。sent 是最近一次被采用的预览用的请求：应用时原样发回（理由会写进
 * 配方、进配方哈希，必须与预览时逐字相同），界面拿它和当前的选项、理由比，不同就说预览已作废
 */
export function useEditFlow({ stagingId, onApplied, onGone, onPreview, onError }: EditFlowHandlers) {
  const [preview, setPreview] = useState<EditPreview | null>(null)
  const [sent, setSent] = useState<EditBody | null>(null)
  const [busy, setBusy] = useState<'' | 'preview' | 'apply'>('')
  const [stale, setStale] = useState(false)
  const [reasonError, setReasonError] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const latest = useRef(0)
  // 面板关掉以后晚到的回包不再改状态。开发模式的 StrictMode 会把挂载的 effect 先清理再跑一遍，所以挂载时要重新置 true
  const alive = useRef(true)
  useEffect(() => {
    alive.current = true
    return () => { alive.current = false }
  }, [])

  const fail = (e: unknown) => {
    const code = e instanceof ApiError ? e.code : undefined
    if (code && GONE_CODES.has(code)) { onGone(errorMessage(e)); return }
    if (code === 'edit_stale') { setStale(true); return }
    if (code === 'reason_required') { setReasonError(errorMessage(e)); return }
    if (onError?.(e)) return
    setError(errorMessage(e))
  }

  const run = async (body: EditBody) => {
    const seq = ++previewSeq
    latest.current = seq
    setBusy('preview')
    setError(null)
    setReasonError(null)
    setStale(false)
    try {
      const res = await api.tableImports.editPreview(stagingId, { ...body, seq } as EditRequest)
      // 不是最新一次的回包（切换选项时连发，回包乱序）：丢弃
      if (!alive.current || latest.current !== seq || (res.seq != null && res.seq !== seq)) return
      const gone = !res.ok ? (res.problems ?? []).find((p) => GONE_CODES.has(p.code)) : undefined
      if (gone) { onGone(gone.message); return }
      setPreview(res)
      setSent(body)
      onPreview?.(res)
    } catch (e) {
      if (!alive.current || latest.current !== seq) return
      fail(e)
    } finally {
      if (alive.current && latest.current === seq) setBusy('')
    }
  }

  const apply = async () => {
    if (!preview?.recipe_sha256_after || !sent) return
    setBusy('apply')
    setError(null)
    try {
      const s = await api.tableImports.editApply(stagingId, {
        ...sent, expected_sha256: preview.recipe_sha256_after, signed_by: localActor(),
      } as EditRequest & { expected_sha256: string; signed_by: string | null })
      if (!alive.current) return
      onApplied(s)
    } catch (e) {
      if (alive.current) fail(e)
    } finally {
      if (alive.current) setBusy('')
    }
  }

  return { preview, sent, busy, stale, reasonError, error, run, apply }
}

/** 问题列表（修改后的检查结果、换算不了的原因）：一条一句，坐标可点 */
function MessageList({ items, onFocus, attr }: {
  items: { message: string; cells?: string[] }[]; onFocus: (cell: string) => void; attr: Record<string, string>
}) {
  return (
    <ul className="space-y-1" {...attr}>
      {items.map((p, i) => (
        <li key={i} className="space-y-0.5 rounded-md border px-2.5 py-1.5 text-xs leading-relaxed">
          <div>{p.message}</div>
          <CellChips cells={p.cells} onFocus={onFocus} max={8} />
        </li>
      ))}
    </ul>
  )
}

/**
 * 预览没通过（ok=false）的原因：逐条显示，坐标可点（P3-SPEC 4.3）。修复和框选共用：「应用」禁用时，
 * 人必须看得到为什么，不能只剩一块空的预览区
 */
function BlockedReasons({ problems, prefix, onFocus }: {
  problems: ImportProblem[]; prefix: 'fix' | 'selection'; onFocus: (cell: string) => void
}) {
  return (
    <section className="space-y-1" {...{ [`data-${prefix}-problems`]: String(problems.length) }}>
      <h4 className="text-xs font-semibold text-[var(--err)]">{prefix === 'fix' ? RECIPE_TEXT.fixCannot : RECIPE_TEXT.selectionCannot}</h4>
      {problems.length ? (
        <ul className="space-y-1">
          {problems.map((p, i) => (
            <li key={i} className="space-y-0.5 rounded-md border px-2.5 py-1.5 text-xs leading-relaxed" {...{ [`data-${prefix}-problem`]: p.code }}
                style={{ borderColor: 'color-mix(in srgb, var(--err) 40%, var(--border))' }}>
              <div>{p.message}</div>
              <CellChips cells={p.cells} onFocus={onFocus} max={8} />
            </li>
          ))}
        </ul>
      ) : <p className="text-xs text-dim">{RECIPE_TEXT.editCannotUnknown}</p>}
    </section>
  )
}

/**
 * 预览结果：没通过的原因、摘要、修改后的检查结果（静态校验加干跑的问题）、对现行配方的破坏性变化、配方前后对照。
 * prefix 区分修复（data-fix-…）和框选（data-selection-…）的定位属性；children 排在原因之后、摘要之前
 * （框选面板的定位文字、重放比对放在这里）
 */
export function EditPreviewBody({ preview, prefix, onFocus, dim = false, children }: {
  preview: EditPreview; prefix: 'fix' | 'selection'; onFocus: (cell: string) => void
  /** 预览已作废（之后改过选项、理由，或配方变了）：照样显示，但淡一些 */
  dim?: boolean
  children?: ReactNode
}) {
  const after: { message: string; cells?: string[] }[] = [
    ...(preview.recipe_problems ?? []).map((p: RecipeProblem) => ({ message: p.message })),
    ...(preview.dry_run?.problems ?? []).filter((p: ImportProblem) => p.category !== 'confirm'),
  ]
  // breaking 是「表名 → 人话列表」；收到布尔（服务端漏了覆盖）时当作没有，不报错
  const breaking = preview.breaking && typeof preview.breaking === 'object' ? Object.entries(preview.breaking).filter(([, v]) => v?.length) : []
  return (
    <div className="space-y-2" style={dim ? { opacity: 0.6 } : undefined} data-edit-preview={preview.ok ? 'ok' : 'blocked'}>
      {!preview.ok && <BlockedReasons problems={preview.problems ?? []} prefix={prefix} onFocus={onFocus} />}
      {children}
      {!!preview.summary?.length && (
        <section className="space-y-1">
          <h4 className="text-xs font-semibold">{RECIPE_TEXT.fixSummary}</h4>
          <ul className="list-disc space-y-0.5 pl-4 text-xs leading-relaxed" {...{ [`data-${prefix}-summary`]: '' }}>
            {preview.summary.map((s, i) => <li key={i}>{s}</li>)}
          </ul>
        </section>
      )}
      {preview.ok && (
        <section className="space-y-1">
          <h4 className="text-xs font-semibold">{RECIPE_TEXT.fixAfter}</h4>
          {after.length
            ? <MessageList items={after} onFocus={onFocus} attr={{ 'data-edit-after': String(after.length) }} />
            : <p className="text-xs text-[var(--ok)]" data-edit-after="0">{RECIPE_TEXT.fixAfterClean}</p>}
        </section>
      )}
      {breaking.length > 0 && (
        <section className="space-y-1" data-edit-breaking>
          <h4 className="text-xs font-semibold text-[var(--warn)]">{RECIPE_TEXT.fixBreaking}</h4>
          <ul className="list-disc space-y-0.5 pl-4 text-xs leading-relaxed">
            {breaking.flatMap(([table, msgs]) => msgs.map((m, i) => <li key={`${table}-${i}`}>「{table}」：{m}</li>))}
          </ul>
        </section>
      )}
      {preview.compare && <RecipeCompare compare={preview.compare} attr={`data-${prefix}-compare`} />}
    </div>
  )
}

/** 问题旁的修复按钮：问题的 fix_ids 指向哪几个提议就放哪几个（界面不靠 code 和坐标去猜） */
export function FixButtons({ ids, fixes, onFix }: {
  ids?: string[]; fixes?: Map<string, FixProposal>; onFix?: (id: string) => void
}) {
  if (!ids?.length || !fixes || !onFix) return null
  const shown = ids.map((id) => fixes.get(id)).filter((f): f is FixProposal => !!f)
  if (!shown.length) return null
  return (
    <span className="inline-flex flex-wrap gap-1" data-fix-buttons>
      {shown.map((f) => (
        <button key={f.id} type="button" className="btn btn-xs" data-fix={f.kind} data-fix-id={f.id} title={f.title}
                onClick={() => onFix(f.id)}>
          <Wrench size={10} aria-hidden /> {FIX_KIND_LABEL[f.kind] ?? RECIPE_TEXT.fixOptions}
        </button>
      ))}
    </span>
  )
}

/** 面板头：返回、标题、相关的格 */
export function PanelHead({ title, cells, onBack, onFocus, children }: {
  title: ReactNode; cells?: string[]; onBack: () => void; onFocus: (cell: string) => void; children?: ReactNode
}) {
  return (
    <div className="space-y-1">
      <div className="flex items-start gap-2">
        <button type="button" className="btn btn-xs btn-ghost shrink-0" onClick={onBack} data-panel-back>
          <ArrowLeft size={11} aria-hidden /> {RECIPE_TEXT.fixBack}
        </button>
        <h3 className="min-w-0 flex-1 text-xs font-semibold leading-relaxed" data-panel-title>{title}</h3>
      </div>
      {children}
      <CellChips cells={cells} onFocus={onFocus} max={10} />
    </div>
  )
}

/** 修复面板。换一个提议时由调用方换 key 重新挂载：选项、理由、预览都从头来 */
export function FixPanel({ proposal, handlers, onClose, onFocus }: {
  proposal: FixProposal
  handlers: EditFlowHandlers
  onClose: () => void
  onFocus: (cell: string) => void
}) {
  const flow = useEditFlow(handlers)
  const name = useId()
  const [option, setOption] = useState<string | null>(null)
  const [reason, setReason] = useState('')
  const chosen = proposal.options.find((o) => o.value === option)
  const needsReason = !!chosen?.needs_reason
  const { preview, sent, busy, stale, reasonError, error } = flow
  const sentFix = sent && 'fix' in sent ? sent.fix : null
  const current = option ? { id: proposal.id, option, ...(needsReason ? { reason: reason.trim() } : {}) } : null
  const matches = !!sentFix && !!current && sentFix.option === current.option && (sentFix.reason ?? '') === (current.reason ?? '')
  const canApply = !!preview?.ok && matches && !stale && !busy
  const staleHint = preview && sentFix && current && !matches
    ? (sentFix.option !== current.option ? RECIPE_TEXT.fixOptionChanged : RECIPE_TEXT.fixReasonChanged) : null

  const choose = (v: string) => {
    setOption(v)
    const o = proposal.options.find((x) => x.value === v)
    // 不需要理由的选项选了就预览；需要理由的等理由填好、点「预览」才发
    if (!o?.needs_reason) void flow.run({ fix: { id: proposal.id, option: v } })
  }

  return (
    <section className="space-y-3" data-fix-panel={proposal.id} data-fix-kind={proposal.kind}>
      <PanelHead title={proposal.title} cells={proposal.cells} onBack={onClose} onFocus={onFocus} />
      <fieldset className="space-y-1.5 rounded-md border bg-bg px-2.5 py-2">
        <legend className="px-1 text-xs font-medium">{RECIPE_TEXT.fixOptions}</legend>
        <ChoiceGroup name={name} label={RECIPE_TEXT.fixOptions} attr="data-fix-option" value={option} disabled={busy === 'apply'}
                     vertical
                     options={proposal.options.map((o) => ({
                       value: o.value, label: o.label, detail: o.detail || undefined,
                       badge: o.breaking
                         ? <span className="chip ml-1.5" style={{ color: 'var(--warn)', borderColor: 'var(--warn)' }}>{RECIPE_TEXT.fixBreakingBadge}</span>
                         : undefined,
                     }))}
                     onChange={choose} />
        {needsReason && (
          <div className="space-y-1 pt-1">
            <label className="label" htmlFor={`${name}-reason`}>{RECIPE_TEXT.fixReasonLabel}</label>
            <textarea id={`${name}-reason`} className="field text-xs" rows={2} maxLength={200} value={reason} data-fix-reason
                      placeholder={RECIPE_TEXT.fixReasonPlaceholder} disabled={busy === 'apply'}
                      aria-invalid={!!reasonError || undefined}
                      style={reasonError ? { borderColor: 'var(--err)' } : undefined}
                      onChange={(e) => setReason(e.target.value)} />
            {reasonError && <div className="text-2xs text-[var(--err)]" role="alert" data-fix-reason-error>{reasonError}</div>}
          </div>
        )}
        {(needsReason || stale) && (
          <div className="flex flex-wrap items-center gap-2 pt-1">
            <button type="button" className="btn btn-sm" data-fix-preview
                    disabled={!current || !!busy || (needsReason && !reason.trim())}
                    title={needsReason && !reason.trim() ? RECIPE_TEXT.fixReasonFirst : undefined}
                    onClick={() => current && void flow.run({ fix: current })}>
              {busy === 'preview' ? <Spinner size={11} /> : null} {preview ? RECIPE_TEXT.fixPreviewAgain : RECIPE_TEXT.fixPreview}
            </button>
          </div>
        )}
      </fieldset>
      {busy === 'preview' && (
        <p className="inline-flex items-center gap-1 text-2xs text-faint" role="status" data-fix-previewing>
          <Spinner size={10} /> {RECIPE_TEXT.fixPreviewing}
        </p>
      )}
      {staleHint && <p className="text-2xs text-[var(--warn)]" role="status" data-fix-stale-hint>{staleHint}</p>}
      {stale && <p className="text-2xs text-[var(--warn)]" role="alert" data-edit-stale>{RECIPE_TEXT.editStale}</p>}
      {preview && <EditPreviewBody preview={preview} prefix="fix" onFocus={onFocus} dim={!matches || stale} />}
      {error && <p className="text-xs text-[var(--err)]" role="alert" data-fix-error>{error}</p>}
      <div className="flex flex-wrap items-center gap-2 border-t pt-2">
        <button type="button" className="btn btn-sm btn-primary" disabled={!canApply} onClick={() => void flow.apply()} data-fix-apply>
          {busy === 'apply' ? <><Spinner size={11} /> {RECIPE_TEXT.fixApplying}</> : RECIPE_TEXT.fixApply}
        </button>
        <button type="button" className="btn btn-sm" onClick={onClose} disabled={busy === 'apply'}>{RECIPE_TEXT.selectionCancel}</button>
      </div>
    </section>
  )
}
