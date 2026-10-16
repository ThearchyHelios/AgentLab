import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { Field, Notice, SectionBar, Spinner, confirmDialog } from '../components/ui'
import { errorMessage } from '../lib/errors'
import { formatCost } from '../lib/format'
import { useLeaveGuard } from '../lib/leave'
import { EVAL_TEXT } from '../lib/termsEval'
import { atLeast, usePrincipal } from '../store/auth'
import type { EvalSpend } from '../typesEval'

const T = EVAL_TEXT.settings

/** 设置里 eval 一组的几项（服务端 api/settings.py 的 EVAL_DEFAULTS，叫法同它的 EVAL_SETTING_LABEL） */
const NUMBERS = ['max_parallel', 'per_case_timeout_s', 'per_run_max_usd', 'daily_max_usd', 'keep_runs'] as const
type NumberKey = (typeof NUMBERS)[number]
interface Draft { numbers: Record<NumberKey, string>; judge: boolean }

const draftOf = (group: Record<string, unknown> | undefined): Draft => ({
  numbers: Object.fromEntries(NUMBERS.map((k) => [k, group?.[k] == null ? '' : String(group[k])])) as Record<NumberKey, string>,
  judge: group?.judge === true,
})

/**
 * 设置页「偏好设置」里「评测」一节（docs/design/next-round/01 做法 17；评测-D9 的缺省：同时 2 题、每题 900 秒、单次 2 美元、
 * 每日 5 美元，每个题集保留 20 次）：全站的预算缺省、保留几次、发起时缺省开不开结论裁判，旁边写今天评测已花多少（评测单独
 * 一本账，不挤占裁判、核查的额度）。
 *
 * 自己读、自己存（PUT /settings 只带 eval 一组，服务端别的组不动），带自己的离开前确认：和偏好设置那条保存条分开，免得一处
 * 没存好拖着另一处。取值范围、单次不高于每日这些规矩只在服务端判（422 的原话落在这一节下面），界面不另判一套。搭建者只读、
 * 管理员写（整个设置接口的规矩）
 */
export function EvalSettingsSection() {
  const me = usePrincipal()
  const admin = atLeast(me, 'admin')
  const [saved, setSaved] = useState<Draft | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [spend, setSpend] = useState<EvalSpend | null>(null)
  const [loadError, setLoadError] = useState<unknown>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [flash, setFlash] = useState(false)

  const loadSpend = () => api.evals.spend().then(setSpend, () => setSpend(null))
  useEffect(() => {
    api.settings.get().then((v) => {
      setSaved(draftOf(v?.eval))
      setLoadError(null)
    }, setLoadError)
    void loadSpend()
  }, [])

  const cur = draft ?? saved
  const changed = !!(cur && saved) && (cur.judge !== saved.judge || NUMBERS.some((k) => cur.numbers[k].trim() !== saved.numbers[k]))
  useLeaveGuard(changed && !saving, () => confirmDialog({
    title: T.leaveTitle(NUMBERS.filter((k) => cur && saved && cur.numbers[k].trim() !== saved.numbers[k]).length
      + (cur && saved && cur.judge !== saved.judge ? 1 : 0)),
    consequences: T.leaveConsequences, confirmLabel: EVAL_TEXT.settings.discard, danger: true,
  }))

  const save = async () => {
    if (!cur || !changed) return
    setSaving(true)
    setError(null)
    try {
      const value = { ...Object.fromEntries(NUMBERS.map((k) => [k, Number(cur.numbers[k].trim())])), judge: cur.judge }
      const out = await api.settings.put({ eval: value })
      setSaved(draftOf(out?.eval ?? value))
      setDraft(null)
      setFlash(true)
      setTimeout(() => setFlash(false), 1600)
      void loadSpend()
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      setSaving(false)
    }
  }

  if (loadError) return null
  if (!cur) return null
  const edit = (patch: Partial<Draft>) => {
    setError(null)
    setDraft({ ...cur, ...patch, numbers: { ...cur.numbers, ...(patch.numbers ?? {}) } })
  }
  return (
    <section data-eval-settings="">
      <SectionBar title={T.title} hint={T.hint} />
      {spend && (
        <p className="mb-3 text-2xs text-dim" data-eval-spend="">
          {T.spend(formatCost(spend.usd ?? 0), formatCost(spend.daily_max_usd ?? null), spend.unpriced_calls ?? 0)}
        </p>
      )}
      <fieldset disabled={!admin || saving} className="m-0 min-w-0 space-y-3 border-0 p-0">
        <div className="grid grid-cols-2 gap-3 max-sm:grid-cols-1">
          {NUMBERS.map((k) => (
            <Field key={k} label={T[k]} htmlFor={`pref-eval-${k}`} hint={k === 'keep_runs' ? T.keepHint : undefined}>
              {(p) => (
                <input {...p} className="field tnum max-w-40" inputMode="decimal" value={cur.numbers[k]}
                       onChange={(e) => edit({ numbers: { [k]: e.target.value } as Record<NumberKey, string> })} data-eval-setting={k} />
              )}
            </Field>
          ))}
        </div>
        <label className="flex items-start gap-2 text-xs">
          <input type="checkbox" className="mt-0.5" checked={cur.judge} onChange={(e) => edit({ judge: e.target.checked })}
                 data-eval-setting="judge" />
          <span>{T.judge}<span className="mt-0.5 block text-2xs leading-relaxed text-faint">{T.judgeHint}</span></span>
        </label>
      </fieldset>
      {!admin && <p className="mt-2 text-2xs text-faint">{T.readOnly}</p>}
      {error && <Notice tone="err" className="mt-2" attr={{ 'data-eval-settings-error': '' }}>{error}</Notice>}
      {admin && (changed || flash) && (
        <div className="mt-3 flex items-center justify-end gap-2" data-eval-settings-bar="">
          {flash && !changed && <span className="text-2xs text-[var(--st-done)]">{T.saved}</span>}
          {changed && (
            <>
              <button type="button" className="btn btn-sm" onClick={() => { setDraft(null); setError(null) }} disabled={saving}>{T.discard}</button>
              <button type="button" className="btn btn-primary btn-sm" onClick={() => void save()} disabled={saving} data-eval-settings-save="">
                {saving && <Spinner size={11} />} {saving ? T.saving : T.save}
              </button>
            </>
          )}
        </div>
      )}
    </section>
  )
}
