import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import clsx from 'clsx'
import { ApiError, api } from '../../api/client'
import { FreqPicker } from '../../components/FreqPicker'
import { Field, Notice, Spinner, confirmDialog, toast } from '../../components/ui'
import { useLeaveGuard } from '../../lib/leave'
import { SCHEDULE_TEXT as T } from '../../lib/terms'
import type { Schedule } from '../../types'
import type { EvalSuite } from '../../typesEval'
import {
  GRACE_OPTIONS, cronOf, graceLabel, targetCreateBody, targetFormOf, targetPatchBody, targetProblems, type Freq, type TargetForm,
} from './model'
import { PreviewBox, usePreview } from './ScheduleEditor'
import { isUnsupported } from './parts'

/**
 * 目标不是工作流的定时任务（评测题集）的表单：名称、目标、频率、时区、补发宽限；新建时另有「创建后立即启用」。新建时选题集，
 * 建好之后目标不在这里改（目标种类、题集换了就是另一条定时，新建一条）。修改时只交改了的字段、带上读到的版本（版本不符 409，
 * 给「重新载入」）；启用、暂停走页头的开关。
 *
 * 没有工作流、输入模板、运行类别和 Webhook：评测的通知走通知渠道的订阅（定时评测有退步、定时评测没有评完），不走定时任务的
 * Webhook——服务端带了照实拒（backend/app/api/schedules.py 的模块说明）。
 */
export function TargetEditor({ schedule, version, init, onSaved, onReload }: {
  schedule: Schedule | null
  version?: number
  /** 新建时带过来的目标（评测页「设定时」：?target_kind=eval_suite&target_id=…） */
  init?: { targetId?: string }
  onSaved: (saved: Schedule, created: boolean) => void
  onReload?: () => void
}) {
  const [base, setBase] = useState<Schedule | null>(schedule)
  const [initial, setInitial] = useState<TargetForm>(() => targetFormOf(schedule, { targetKind: 'eval_suite', targetId: init?.targetId }))
  const [form, setForm] = useState<TargetForm>(initial)
  const [touched, setTouched] = useState<Record<string, boolean>>({})
  const [suites, setSuites] = useState<EvalSuite[] | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<{ message: string; detail?: string; conflict?: boolean; field?: string } | null>(null)
  /** 名称是选题集时替人填的哪个名字：人没改过它时，换题集名称跟着换 */
  const autoName = useRef<string | null>(null)

  useEffect(() => {
    if (schedule) return
    api.evals.suites().then((r) => setSuites(r.suites.filter((s) => !s.archived)), () => setSuites([]))
  }, [!!schedule])
  // 带着题集过来的（评测页的「设定时」）：名称按题集名替人填上，连同表单初值一起——人还什么都没改，直接离开不该被问
  useEffect(() => {
    if (schedule || !suites || form.name || !form.targetId) return
    const s = suites.find((x) => x.id === form.targetId)
    if (s) {
      autoName.current = s.name
      setInitial((i) => ({ ...i, name: s.name }))
      setForm((f) => ({ ...f, name: s.name }))
    }
  }, [suites])

  const cron = cronOf(form.freq)
  const preview = usePreview(cron, form.timezone, {}, '')
  const problems = targetProblems(form)
  const changes = base ? targetPatchBody(form, initial) : null
  const dirty = base ? changes != null : JSON.stringify(form) !== JSON.stringify(initial)
  const previewBlocks = preview.status === 'invalid' && (preview.field === 'cron' || preview.field === 'timezone')
  const missing = Object.keys(problems)
  const canSave = !saving && !missing.length && !previewBlocks && dirty

  useLeaveGuard(dirty && !saving, () => confirmDialog({
    title: T.leaveTitle, consequences: T.leaveConsequences, confirmLabel: T.leaveConfirm, danger: true,
  }))

  const set = (patch: Partial<TargetForm>) => setForm((f) => ({ ...f, ...patch }))
  const setFreq = (patch: Partial<Freq>) => setForm((f) => ({ ...f, freq: { ...f.freq, ...patch } }))
  const pickSuite = (id: string) => {
    const s = suites?.find((x) => x.id === id)
    // 名字空着、或者还是上次替人填的题集名：跟着换
    const follow = !form.name.trim() || form.name === autoName.current
    if (follow && s) autoName.current = s.name
    set({ targetId: id, ...(follow && s ? { name: s.name } : {}) })
  }

  const save = async () => {
    if (!canSave) return
    setSaving(true)
    setSaveError(null)
    try {
      let saved: Schedule
      if (!base) saved = await api.schedules.create(targetCreateBody(form))
      else {
        const body = targetPatchBody(form, initial)
        if (!body) return
        saved = await api.schedules.update(base.id, { ...body, version: version ?? base.version })
      }
      const next = targetFormOf(saved)
      setBase(saved)
      setInitial(next)
      setForm(next)
      setTouched({})
      if (base) toast.ok(T.saved)
      onSaved(saved, !base)
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) setSaveError({ message: T.conflict, detail: e.message, conflict: true })
      else if (isUnsupported(e)) setSaveError({ message: T.unsupportedHint })
      else if (e instanceof ApiError && e.kind === 'http' && e.status >= 400 && e.status < 500) setSaveError({ message: e.message, field: e.field })
      else toast.error(e)
    } finally {
      setSaving(false)
    }
  }

  const serverError = (keys: string[]) => (saveError?.field && keys.includes(saveError.field) ? saveError.message : undefined)
  const nameError = (touched.name ? problems.name : undefined) ?? serverError(['name'])
  const targetError = (touched.target ? problems.target : undefined) ?? serverError(['target_id', 'target_kind'])
  const suite = suites?.find((s) => s.id === form.targetId)
  const hint = missing.length ? T.missing(missing.map((k) => (k === 'target' ? T.suite : T.problemField[k as 'name' | 'freq'])).join('、'))
    : previewBlocks ? T.previewBlocks : ''

  return (
    <form className="mx-auto grid max-w-5xl gap-4 p-4 lg:grid-cols-[minmax(0,1fr)_300px]" noValidate
          onSubmit={(e) => { e.preventDefault(); void save() }} data-schedule-target-form={form.targetKind}>
      <div className="min-w-0 space-y-4 lg:col-start-1">
        <Notice tone="info" attr={{ 'data-schedule-eval-note': '' }}>{T.evalTargetNote}</Notice>
        <section className="space-y-3 rounded-lg border bg-panel p-3.5">
          <Field label={T.name} htmlFor="schedule-name" required error={nameError}>
            <input id="schedule-name" className="field" value={form.name} placeholder={T.namePlaceholder} autoComplete="off"
                   maxLength={200} aria-invalid={!!nameError || undefined} onChange={(e) => set({ name: e.target.value })}
                   onBlur={() => setTouched((t) => ({ ...t, name: true }))} data-field="name" />
          </Field>
          {base ? (
            <Field label={T.suite} htmlFor="schedule-suite">
              <div id="schedule-suite" className="flex flex-wrap items-center gap-2 text-xs" data-schedule-suite={base.target_id}>
                <span>{base.target_exists === false ? T.targetGone(base.target_name ?? '') : base.target_name}</span>
                {base.target_exists !== false && base.target_id && (
                  <Link className="underline text-dim hover:text-fg" to={`/evals/suites/${encodeURIComponent(base.target_id)}`}>{T.openSuite}</Link>
                )}
              </div>
            </Field>
          ) : (
            <Field label={T.suite} htmlFor="schedule-suite" required error={targetError}
                   hint={suites && !suites.length ? <>{T.suitesEmpty} <Link to="/evals" className="underline">{T.toEvals}</Link></> : undefined}>
              <select id="schedule-suite" className="field" value={form.targetId} aria-invalid={!!targetError || undefined}
                      onChange={(e) => pickSuite(e.target.value)} onBlur={() => setTouched((t) => ({ ...t, target: true }))}
                      data-schedule-suite-pick="">
                <option value="">{suites === null ? T.suitesLoading : T.suitePlaceholder}</option>
                {(suites ?? []).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
              </select>
            </Field>
          )}
          {suite && <p className="text-2xs text-faint">{suite.description}</p>}
        </section>
        <FreqPicker freq={form.freq} timezone={form.timezone} setFreq={setFreq} setTimezone={(timezone) => set({ timezone })}
                    freqError={problems.freq ?? (preview.status === 'invalid' && preview.field === 'cron' ? preview.message : undefined) ?? serverError(['cron'])}
                    tzError={(preview.status === 'invalid' && preview.field === 'timezone' ? preview.message : undefined) ?? serverError(['timezone'])} />
        <section className="space-y-3 rounded-lg border bg-panel p-3.5" aria-labelledby="schedule-overlap-title">
          <h2 id="schedule-overlap-title" className="text-xs font-semibold">{T.overlap}</h2>
          <p className="text-xs leading-relaxed text-dim">{T.overlapText}</p>
          <Field label={T.misfire} htmlFor="schedule-misfire" hint={T.misfireHint} error={serverError(['misfire_grace_s'])}>
            <select id="schedule-misfire" className="field w-auto max-w-full" value={form.misfireGraceS} data-misfire=""
                    onChange={(e) => set({ misfireGraceS: Number(e.target.value) })}>
              {[...new Set([...GRACE_OPTIONS, form.misfireGraceS])].sort((a, b) => a - b).map((s) => (
                <option key={s} value={s}>{T.misfireOption(graceLabel(s))}</option>
              ))}
            </select>
          </Field>
          {!base && (
            <label className="flex items-center gap-2 text-xs">
              <input type="checkbox" checked={form.enabled} onChange={(e) => set({ enabled: e.target.checked })} data-enabled-on-create="" />
              {T.enabledOnCreate}
            </label>
          )}
        </section>
      </div>
      <aside className="min-w-0 lg:col-start-2 lg:row-start-1">
        <div className="lg:sticky lg:top-4"><PreviewBox preview={preview} /></div>
      </aside>
      <div className={clsx('z-10 space-y-2 rounded-lg border bg-panel px-3 py-2.5 lg:col-span-2', (dirty || saveError) && 'sticky bottom-0 shadow-elev-2')}
           data-schedule-savebar="">
        {saveError?.conflict && (
          <Notice tone="err" attr={{ 'data-schedule-conflict': '' }}>
            <div>{saveError.message}</div>
            {saveError.detail && <div className="text-faint">{saveError.detail}</div>}
            <button type="button" className="btn btn-sm mt-1.5" onClick={() => onReload?.()} data-schedule-reload="">{T.reload}</button>
          </Notice>
        )}
        {saveError && !saveError.conflict && (
          <Notice tone="err" attr={{ 'data-schedule-save-error': '' }}>{T.saveFailed}：{saveError.message}</Notice>
        )}
        <div className="flex flex-wrap items-center justify-end gap-2">
          {hint && <span className="mr-auto min-w-0 text-2xs leading-relaxed text-faint" data-schedule-hint="">{hint}</span>}
          {base
            ? dirty && <button type="button" className="btn" onClick={() => { setForm(initial); setSaveError(null) }} disabled={saving}>{T.discard}</button>
            : <Link to="/schedules" className="btn">{T.cancel}</Link>}
          <button type="submit" className="btn btn-primary" disabled={!canSave} title={hint || undefined} data-schedule-save="">
            {saving && <Spinner size={11} />} {saving ? T.saving : base ? T.save : T.createSubmit}
          </button>
        </div>
      </div>
    </form>
  )
}
