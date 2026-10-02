import { useEffect, useId, useMemo, useState } from 'react'
import { AlertTriangle } from 'lucide-react'
import clsx from 'clsx'
import type { CheckResult, ConfirmItem, FixProposal, PriorAcceptance } from '../../types'
import { Spinner } from '../../components/ui'
import { localActor } from '../../lib/actor'
import { CONFIRM_SOURCE_LABEL, PROBLEM_CATEGORY_LABEL, RECIPE_TEXT } from '../../lib/terms'
import { FixButtons } from './FixPanel'
import { CheckStatusChip, PriorAcceptances, renameFixIds } from './ImportReceipt'

// ===========================================================================
// 启用前的确认清单：逐条勾选（不提供「全选」），可接受的核对逐条写理由，署名（未认证）。
// 必勾的集合由服务端按试运行结果重算，少勾一项服务端照样拒绝（422 confirm_required）
// ===========================================================================

/** 和破坏性变更同一档的确认项（P3-SPEC 9.5）：都会改变当前版本的内容或口径 */
const TIER1 = ['breaking:', 'mode_switch:', 'retire:', 'period_replace:']
const TIER1_IDS = new Set(['switch_from_simple', 'accumulate_restart', 'accumulate_unit_risk', 'redraft_adopted'])

/**
 * 排序（P3-SPEC 9.5）：单位变化排最前（数量级变了，引用它的报告口径会变）；破坏性变更、切换、重新开始累积、
 * 模式切换、退役、替换该期、单位口径风险、采用重新起草同一档；然后是差异卡里需确认的项；其余在后
 */
export function rank(id: string): number {
  if (id.startsWith('unit_changed:')) return 0
  if (TIER1_IDS.has(id) || TIER1.some((p) => id.startsWith(p))) return 1
  if (id.startsWith('diff:')) return 2
  return 3
}

/** 分组的先后：组里最靠前的那一项的档次在前，同档按配方、修改、累积、本期、差异、切换 */
const GROUP_ORDER = ['recipe', 'edit', 'accumulate', 'current', 'diff', 'switch']
/** 区域外文字、工作表、统计期都是「本期」才有的项，合成一组 */
const groupOf = (it: ConfirmItem) => {
  const s = it.source ?? 'recipe'
  return s === 'outside' || s === 'sheet' || s === 'context' ? 'current' : GROUP_ORDER.includes(s) ? s : 'recipe'
}
const GROUP_LABEL: Record<string, string> = {
  recipe: CONFIRM_SOURCE_LABEL.recipe, edit: CONFIRM_SOURCE_LABEL.edit, accumulate: CONFIRM_SOURCE_LABEL.accumulate,
  current: CONFIRM_SOURCE_LABEL.outside, diff: CONFIRM_SOURCE_LABEL.diff, switch: CONFIRM_SOURCE_LABEL.switch,
}

export interface CommitBody {
  confirmations: string[]
  acceptances: { check_id: string; reason: string }[]
  signed_by: string | null
}

export function ConfirmList({ items, checks, acceptable, reupload, busy, missing, error, blocked = false, prior, fixes, sheets, onFix, onSubmit }: {
  items: ConfirmItem[]
  checks: CheckResult[]
  /** 可写理由接受、本次未通过的核对 id */
  acceptable: string[]
  /** 上传新一期：按钮写「启用」 */
  reupload: boolean
  busy: boolean
  /** 服务端说还没勾的项（422 confirm_required / acceptance_required 的 detail 里点名的） */
  missing: Set<string>
  error: string | null
  /** 重试也会失败（build_conflict）：「确认并启用」一直禁用，等管理员处理 */
  blocked?: boolean
  /** 同一份构建以前被接受过的理由：只读显示在对应的核对旁，理由框不预填 */
  prior?: PriorAcceptance[]
  /** 工作表改名的确认项旁放「更新工作表名」（修复提议的 anchor.kind=sheet_renamed） */
  fixes?: FixProposal[]
  /**
   * 试运行回执的 sheets：matched 是「配方里的工作表 id → 本期工作表名」，renamed 是「配方里的名字 → 本期的名字」。
   * 确认项 sheet_renamed:<sid> 的 sid 一般是配方里的工作表 id，查 matched；配方里找不到那张表时服务端退回用旧名当 sid，
   * 查 renamed。查到的本期表名再和提议的 anchor.sheet 对，只放这张表的那一个按钮
   */
  sheets?: { matched?: Record<string, string>; renamed?: Record<string, string> } | null
  onFix?: (id: string) => void
  onSubmit: (body: CommitBody) => void
}) {
  // 先按来源分组，组内按档次、再按服务端给的先后；组的先后按组里最靠前的一项（单位变化所在的组排第一）
  const groups = useMemo(() => {
    const by = new Map<string, { it: ConfirmItem; i: number }[]>()
    items.forEach((it, i) => { const g = groupOf(it); by.set(g, [...(by.get(g) ?? []), { it, i }]) })
    return [...by.entries()]
      .map(([g, list]) => ({ g, list: list.sort((a, b) => rank(a.it.id) - rank(b.it.id) || a.i - b.i).map((x) => x.it) }))
      .sort((a, b) => rank(a.list[0].id) - rank(b.list[0].id) || GROUP_ORDER.indexOf(a.g) - GROUP_ORDER.indexOf(b.g))
  }, [items])
  const sorted = useMemo(() => groups.flatMap((x) => x.list), [groups])
  const renameFix = useMemo(() => new Map((fixes ?? []).map((f) => [f.id, f])), [fixes])
  const renameIds = (sid: string) => renameFixIds(fixes, sheets?.matched?.[sid] ?? sheets?.renamed?.[sid])
  const [checked, setChecked] = useState<Set<string>>(() => new Set())
  const [reasons, setReasons] = useState<Record<string, string>>({})
  const [signer, setSigner] = useState(() => localActor() ?? '')
  const signId = useId()
  // 换了一次试运行（确认项变了）：勾过的里面只留还在的
  useEffect(() => {
    setChecked((cur) => new Set([...cur].filter((id) => items.some((it) => it.id === id))))
  }, [items])

  const required = sorted.filter((it) => it.required !== false)
  const accepts = acceptable.map((id) => ({ id, check: checks.find((c) => c.id === id) }))
  const allChecked = required.every((it) => checked.has(it.id))
  const allReasons = accepts.every(({ id }) => {
    const v = (reasons[id] ?? '').trim()
    return v.length > 0 && v.length <= 500
  })
  const ready = allChecked && allReasons && !busy && !blocked

  const submit = () => {
    if (!ready) return
    onSubmit({
      confirmations: sorted.filter((it) => checked.has(it.id)).map((it) => it.id),
      acceptances: accepts.map(({ id }) => ({ check_id: id, reason: reasons[id].trim() })),
      signed_by: signer.trim() || null,
    })
  }

  return (
    <div className="space-y-3" data-confirm-list>
      <div>
        <h3 className="text-sm font-semibold">{RECIPE_TEXT.confirmTitle}</h3>
        <p className="mt-0.5 text-2xs text-faint">{RECIPE_TEXT.confirmLead}</p>
      </div>
      {error && (
        <div role="alert" className="flex gap-2 rounded-lg border px-3 py-2 text-xs leading-relaxed" data-confirm-error
             style={{ borderColor: 'color-mix(in srgb, var(--err) 35%, var(--border))', background: 'color-mix(in srgb, var(--err) 6%, transparent)' }}>
          <AlertTriangle size={13} className="mt-0.5 shrink-0 text-[var(--err)]" aria-hidden />
          <span>{error}</span>
        </div>
      )}
      {groups.map(({ g, list }) => (
        <section key={g} className="space-y-1.5" data-confirm-group={g}>
          <h4 className="text-2xs font-semibold text-dim" data-confirm-group-title>{GROUP_LABEL[g]}</h4>
          <ul className="space-y-1.5">
            {list.map((it) => {
              const miss = missing.has(it.id)
              return (
                <li key={it.id} data-confirm-item={it.id} data-required={it.required !== false} data-missing={miss || undefined}
                    className={clsx('rounded-md border px-2.5 py-2 text-xs')}
                    style={miss ? { borderColor: 'var(--err)' } : undefined}>
                  <label className="flex cursor-pointer items-start gap-2">
                    <input type="checkbox" className="mt-0.5" checked={checked.has(it.id)} disabled={busy}
                           onChange={(e) => setChecked((cur) => {
                             const next = new Set(cur)
                             if (e.target.checked) next.add(it.id)
                             else next.delete(it.id)
                             return next
                           })} />
                    <span className="min-w-0 flex-1">
                      <span className="leading-relaxed">{it.label}</span>
                      {it.required === false && <span className="ml-1.5 text-2xs text-faint" data-confirm-optional>{RECIPE_TEXT.confirmOptional}</span>}
                      {it.detail && <span className="mt-0.5 block text-2xs leading-relaxed text-dim">{it.detail}</span>}
                      {miss && <span className="mt-0.5 block text-2xs text-[var(--err)]">{RECIPE_TEXT.missing}</span>}
                    </span>
                  </label>
                  {it.id.startsWith('sheet_renamed:') && (
                    <div className="mt-1 pl-5">
                      <FixButtons ids={renameIds(it.id.slice('sheet_renamed:'.length))} fixes={renameFix} onFix={onFix} />
                    </div>
                  )}
                </li>
              )
            })}
          </ul>
        </section>
      ))}
      {accepts.length > 0 && (
        <section className="space-y-1.5" data-acceptances>
          <h4 className="text-xs font-semibold">{RECIPE_TEXT.acceptTitle}</h4>
          {accepts.map(({ id, check }) => {
            const fieldId = `${signId}-accept-${id}`
            const miss = missing.has(id)
            return (
              <div key={id} className="space-y-1 rounded-md border px-2.5 py-2" data-acceptance={id} data-missing={miss || undefined}
                   style={miss ? { borderColor: 'var(--err)' } : undefined}>
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  {check && <CheckStatusChip status={check.status} />}
                  <span className="mono text-faint">{id}</span>
                  <span className="min-w-0 flex-1">{check?.title ?? id}</span>
                </div>
                {check && <div className="text-2xs text-faint">{PROBLEM_CATEGORY_LABEL[check.category] ?? ''}</div>}
                <PriorAcceptances items={(prior ?? []).filter((p) => p.check_id === id)} />
                <label className="label" htmlFor={fieldId}>{RECIPE_TEXT.acceptLabel}</label>
                <textarea id={fieldId} className="field text-xs" rows={2} maxLength={500} value={reasons[id] ?? ''} disabled={busy}
                          placeholder={RECIPE_TEXT.acceptPlaceholder}
                          onChange={(e) => setReasons((cur) => ({ ...cur, [id]: e.target.value }))} />
              </div>
            )
          })}
        </section>
      )}
      <div className="flex flex-wrap items-end gap-3 border-t pt-3">
        <div className="min-w-0">
          <label className="label" htmlFor={signId}>{RECIPE_TEXT.signLabel}</label>
          <div className="flex items-center gap-2">
            <input id={signId} className="field !w-48" value={signer} onChange={(e) => setSigner(e.target.value)} disabled={busy} />
            <span className="text-2xs text-faint" data-sign-note>{RECIPE_TEXT.signNote}</span>
          </div>
        </div>
        <span className="flex-1" />
        {!ready && !busy && !blocked && <span className="text-2xs text-faint">{RECIPE_TEXT.commitBlocked}</span>}
        {blocked && <span className="text-2xs text-[var(--err)]" data-commit-conflict>{RECIPE_TEXT.commitConflict}</span>}
        <button type="button" className="btn btn-primary" disabled={!ready} onClick={submit} data-commit>
          {busy ? <><Spinner size={11} /> {RECIPE_TEXT.committing}</> : reupload ? RECIPE_TEXT.commitReupload : RECIPE_TEXT.commitFirst}
        </button>
      </div>
    </div>
  )
}
