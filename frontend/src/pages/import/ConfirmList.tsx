import { useEffect, useId, useMemo, useState } from 'react'
import { AlertTriangle } from 'lucide-react'
import clsx from 'clsx'
import type { CheckResult, ConfirmItem } from '../../types'
import { Spinner } from '../../components/ui'
import { localActor } from '../../lib/actor'
import { PROBLEM_CATEGORY_LABEL, RECIPE_TEXT } from '../../lib/terms'
import { CheckStatusChip } from './ImportReceipt'

// ===========================================================================
// 启用前的确认清单：逐条勾选（不提供「全选」），可接受的核对逐条写理由，署名（未认证）。
// 必勾的集合由服务端按试运行结果重算，少勾一项服务端照样拒绝（422 confirm_required）
// ===========================================================================

/** 单位变化排最前（数量级变了，引用它的报告口径会变），其次是破坏性变更和差异卡里需确认的项 */
function rank(id: string): number {
  if (id.startsWith('unit_changed:')) return 0
  if (id.startsWith('breaking:') || id === 'switch_from_simple') return 1
  if (id.startsWith('diff:')) return 2
  return 3
}

export interface CommitBody {
  confirmations: string[]
  acceptances: { check_id: string; reason: string }[]
  signed_by: string | null
}

export function ConfirmList({ items, checks, acceptable, reupload, busy, missing, error, onSubmit }: {
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
  onSubmit: (body: CommitBody) => void
}) {
  const sorted = useMemo(() => items.map((it, i) => ({ it, i }))
    .sort((a, b) => rank(a.it.id) - rank(b.it.id) || a.i - b.i).map((x) => x.it), [items])
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
  const ready = allChecked && allReasons && !busy

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
      <ul className="space-y-1.5">
        {sorted.map((it) => {
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
            </li>
          )
        })}
      </ul>
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
        {!ready && !busy && <span className="text-2xs text-faint">{RECIPE_TEXT.commitBlocked}</span>}
        <button type="button" className="btn btn-primary" disabled={!ready} onClick={submit} data-commit>
          {busy ? <><Spinner size={11} /> {RECIPE_TEXT.committing}</> : reupload ? RECIPE_TEXT.commitReupload : RECIPE_TEXT.commitFirst}
        </button>
      </div>
    </div>
  )
}
