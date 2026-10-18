import { useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Check } from 'lucide-react'
import clsx from 'clsx'
import { ApiError, api } from '../../api/client'
import { FreqPicker } from '../../components/FreqPicker'
import { Field, Notice, Skeleton, Spinner, confirmDialog, toast, useRadioGroup } from '../../components/ui'
import { errorMessage } from '../../lib/errors'
import { parseServerTime } from '../../lib/format'
import { useLeaveGuard } from '../../lib/leave'
import { SCHEDULE_TEXT as T } from '../../lib/terms'
import { TRIGGER_TEXT as TT } from '../../lib/termsTrigger'
import { useCatalog, useCatalogListState } from '../../store/catalog'
import type { Schedule, ScheduleEventKind, SchedulePreview, ScheduleRunClass, ScheduleTrigger, ScheduleVariable, Workflow } from '../../types'
import {
  GRACE_OPTIONS, createBody, cronOf, formOf, formProblems, formatZoned, freqProblem, graceLabel, graphFields, patchBody,
  rebaseForm, serverFieldValue, templateOf, triggerBody, type Freq, type FormField, type InputField, type ScheduleForm,
} from './model'
import { isUnsupported } from './parts'
import { EventSection, TriggerModePicker } from './TriggerEditor'
import { WebhookFields } from './WebhookFields'
import { WorkflowPicker } from './WorkflowPicker'

/**
 * 新建、修改定时任务的表单。宽屏左边是表单、右边是接下来 5 次触发的预览（跟着滚动停在顶上）；窄屏预览排在
 * 频率下面，改了频率当场看得到。
 *
 * 新建时先选「定时 / 事件触发」（docs/design/orchestration-round/00-总方案.md §4）：事件触发的没有频率，那一节换成事件
 * （TriggerEditor 的 EventSection），预览按一次事件渲染输入（不列触发时间），变量多出 {{ event.* }}。触发方式建好之后不能改。
 *
 * schedule 为 null 是新建。修改时只交改了的字段，带上 version：详情页页头的开关改过之后，version 是开关交回的
 * 新版本，表单照常保存；别人在这期间改过就 409，表单不动，给「重新载入」。
 */
export function ScheduleEditor({ schedule, version, onSaved, onReload }: {
  schedule: Schedule | null
  /** 保存时带的版本；不给用 schedule 自己的 */
  version?: number
  onSaved: (saved: Schedule, created: boolean) => void
  /** 版本不符时重新读服务端的设置（详情页重置表单） */
  onReload?: () => void
}) {
  const workflows = useCatalog((s) => s.workflows)
  const workflowsState = useCatalogListState('workflows')
  const [base, setBase] = useState<Schedule | null>(schedule)
  const [initial, setInitial] = useState<ScheduleForm>(() => formOf(schedule))
  const [form, setForm] = useState<ScheduleForm>(initial)
  const [touched, setTouched] = useState<Partial<Record<FormField, boolean>>>({})
  /** 新建时没动过运行类别：选工作流时按它发没发布替人选上 */
  const runClassTouched = useRef(!!schedule)
  /** 名称是选工作流时替人填的哪个名字：人没改过它时，换工作流名称跟着换；人改过就不再动 */
  const autoName = useRef<string | null>(null)
  const [saving, setSaving] = useState(false)
  /**
   * 上一次保存的失败。field 是服务端 422 指的那一项，at 是交上去时那一项的值（serverFieldValue）：
   * 人改了那一项，说明就不再摆着，免得看上去新值也不行
   */
  const [saveError, setSaveError] = useState<{
    message: string; detail?: string; conflict?: boolean; field?: string; at?: string | null
  } | null>(null)

  const workflow = workflows.find((w) => w.id === form.workflowId)
  const event = form.triggerKind === 'event'
  const cron = event ? '' : cronOf(form.freq)
  const template = useMemo(() => templateOf(form.inputs), [form.inputs])
  const trigger = useMemo(() => (event ? triggerBody(form.trigger) : null), [event, form.trigger])
  const preview = usePreview(cron, form.timezone, template, form.workflowId, trigger)
  const fieldsOf = useInputFields(workflow, form.runClass)
  // 修改时工作流列表还没取回来：输入字段是「正在读取」，不是「请先选择工作流」
  const fields: FieldsState = !workflow && form.workflowId && workflowsState !== 'ok' ? { status: 'loading' } : fieldsOf
  const variables = useVariables(event ? form.trigger.event : null)

  const changes = base ? patchBody(form, initial, base) : null
  // 服务端保存时只在新建、或者改了工作流、运行类别、输入时才核对这三样：照着拦，不多拦也不少拦
  const recheck = !base || (!!changes && ('workflow_id' in changes || 'run_class' in changes || 'input_template' in changes
    || 'trigger' in changes))
  const problems = formProblems(form, base, {
    published: workflow ? workflow.published_version ?? null : undefined,
    fields: fields.status === 'ok' ? fields.fields : undefined,
    recheck,
  })
  const dirty = base ? changes != null : JSON.stringify(form) !== JSON.stringify(initial)
  const previewField = preview.status === 'invalid' ? preview.field : undefined
  /** 预览判定非法、并指明是这一项时，服务端的说明 */
  const previewSays = (field: string) => (preview.status === 'invalid' && preview.field === field ? preview.message : undefined)
  const previewBlocks = previewField === 'cron' || previewField === 'timezone' || previewField === 'input_template'
    || previewField === 'trigger' || previewField === 'debounce_s'
  const missing = (Object.keys(problems) as FormField[]).map((k) => T.problemField[k])
  const hint = missing.length ? T.missing(missing.join('、')) : previewBlocks ? T.previewBlocks : ''
  const canSave = !saving && !missing.length && !previewBlocks && dirty

  useLeaveGuard(dirty && !saving, () => confirmDialog({
    title: T.leaveTitle, consequences: T.leaveConsequences, confirmLabel: T.leaveConfirm, danger: true,
  }))

  const set = (patch: Partial<ScheduleForm>) => setForm((f) => ({ ...f, ...patch }))
  const setFreq = (patch: Partial<Freq>) => setForm((f) => ({ ...f, freq: { ...f.freq, ...patch } }))
  const touch = (k: FormField) => setTouched((t) => (t[k] ? t : { ...t, [k]: true }))

  const pickWorkflow = (w: Workflow) => {
    if (w.id === form.workflowId) return
    // 名字空着、或者还是上次替人填的工作流名：跟着换成新工作流的名字。在这里判、不在 setForm 的回调里改 ref：
    // 回调在严格模式下会调两遍
    const follow = !form.name.trim() || form.name === autoName.current
    if (follow) autoName.current = w.name
    setForm((f) => {
      // 换了工作流：新工作流里没有的字段的模板丢掉（同名的留着）
      const keep = new Set(graphFields(w.graph).map((x) => x.name))
      const inputs = Object.fromEntries(Object.entries(f.inputs).filter(([k]) => keep.has(k)))
      const runClass: ScheduleRunClass = runClassTouched.current ? f.runClass : w.published_version ? 'formal' : 'exploratory'
      return { ...f, workflowId: w.id, name: follow ? w.name : f.name, inputs, runClass }
    })
  }

  const save = async () => {
    if (!canSave) return
    const sent = form
    setSaving(true)
    setSaveError(null)
    try {
      let saved: Schedule
      if (!base) {
        saved = await api.schedules.create(createBody(sent))
      } else {
        const body = patchBody(sent, initial, base)
        if (!body) return
        saved = await api.schedules.update(base.id, { ...body, version: version ?? base.version })
      }
      const next = formOf(saved)
      setBase(saved)
      setInitial(next)
      // 保存的这段时间里人又改了：只留途中改的那几项（相对刚存下的设置照样算未保存），其余换成刚存下的。
      // 整份留着的话，交上去的替换地址会以完整明文留在框里，下次保存再发一遍
      setForm((cur) => rebaseForm(cur, sent, next))
      setTouched({})
      if (base) toast.ok(T.saved)
      onSaved(saved, !base)
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setSaveError({ message: T.conflict, detail: e.message, conflict: true })
      } else if (isUnsupported(e)) {
        setSaveError({ message: T.unsupportedHint })
      } else if (e instanceof ApiError && e.kind === 'http' && e.status >= 400 && e.status < 500) {
        setSaveError({ message: e.message, field: e.field, at: e.field ? serverFieldValue(e.field, sent) : null })
      } else {
        toast.error(e)
      }
    } finally {
      setSaving(false)
    }
  }

  const reload = async () => {
    if (dirty && !(await confirmDialog({
      title: T.reloadTitle, consequences: T.reloadConsequences, confirmLabel: T.reloadConfirm, danger: true,
    }))) return
    onReload?.()
  }

  const discard = () => {
    setForm(initial)
    setTouched({})
    setSaveError(null)
  }

  // 保存被 422 拒了、服务端指了是哪一项：写在那一项下面；那一项改过之后不再摆着（认不出的项留到下次保存）
  const stillSays = !!saveError && (saveError.at == null || !saveError.field || serverFieldValue(saveError.field, form) === saveError.at)
  const serverError = (keys: string[]) => (stillSays && saveError?.field && keys.includes(saveError.field) ? saveError.message : undefined)
  const shown = (k: FormField) => (touched[k] ? problems[k] : undefined)
  const nameError = shown('name') ?? serverError(['name'])
  const misfireError = serverError(['misfire_grace_s'])

  return (
    <form
      className="mx-auto grid max-w-5xl gap-4 p-4 lg:grid-cols-[minmax(0,1fr)_300px]"
      noValidate
      onSubmit={(e) => { e.preventDefault(); void save() }}
      data-schedule-form=""
    >
      <div className="min-w-0 space-y-4 lg:col-start-1">
        <section className="space-y-3 rounded-lg border bg-panel p-3.5">
          <Field label={T.name} htmlFor="schedule-name" required error={nameError}>
            <input id="schedule-name" className="field" value={form.name} placeholder={T.namePlaceholder} autoComplete="off"
                   maxLength={200} aria-invalid={!!nameError || undefined}
                   aria-describedby={nameError ? 'schedule-name-error' : undefined}
                   onChange={(e) => set({ name: e.target.value })} onBlur={() => touch('name')} data-field="name" />
          </Field>
          <WorkflowField form={form} workflows={workflows} state={workflowsState} workflow={workflow} base={base}
                         error={shown('workflow') ?? serverError(['workflow_id'])}
                         onPick={pickWorkflow} onBlur={() => touch('workflow')} />
          <TriggerModePicker value={form.triggerKind} fixed={!!base} onChange={(triggerKind) => set({ triggerKind })} />
        </section>

        {event ? (
          <EventSection value={form.trigger} onChange={(t) => set({ trigger: t })} timezone={form.timezone}
                        setTimezone={(timezone) => set({ timezone })} savedName={base?.trigger?.subject_name ?? null}
                        error={previewSays('trigger') ?? serverError(['trigger', 'trigger_kind'])}
                        debounceError={previewSays('debounce_s') ?? serverError(['debounce_s'])} />
        ) : (
          <FreqPicker freq={form.freq} timezone={form.timezone} setFreq={setFreq} setTimezone={(timezone) => set({ timezone })}
                      freqError={freqProblem(form.freq) ?? previewSays('cron') ?? serverError(['cron'])}
                      tzError={previewSays('timezone') ?? serverError(['timezone'])} />
        )}
      </div>

      <aside className="min-w-0 lg:col-start-2 lg:row-span-2 lg:row-start-1">
        <div className="lg:sticky lg:top-4">
          <PreviewBox preview={preview} event={event} />
        </div>
      </aside>

      <div className="min-w-0 space-y-4 lg:col-start-1">
        <RunClassSection value={form.runClass} workflow={workflow} problem={problems.runClass}
                         error={serverError(['run_class'])}
                         onChange={(runClass) => { runClassTouched.current = true; set({ runClass }) }} />
        <InputsSection workflow={workflow} fields={fields} inputs={form.inputs} variables={variables}
                       preview={preview} recheck={recheck} event={event}
                       error={previewSays('input_template') ?? serverError(['input_template'])}
                       onChange={(inputs) => set({ inputs })} />
        <section className="space-y-3 rounded-lg border bg-panel p-3.5" aria-labelledby="schedule-overlap-title">
          <h2 id="schedule-overlap-title" className="text-xs font-semibold">{T.overlap}</h2>
          <p className="text-xs leading-relaxed text-dim">{T.overlapText}</p>
          <Field label={T.misfire} htmlFor="schedule-misfire" hint={event ? TT.misfireHint : T.misfireHint} error={misfireError}>
            <select id="schedule-misfire" className="field w-auto max-w-full" value={form.misfireGraceS}
                    aria-invalid={!!misfireError || undefined}
                    aria-describedby={misfireError ? 'schedule-misfire-error' : 'schedule-misfire-hint'}
                    onChange={(e) => set({ misfireGraceS: Number(e.target.value) })} data-misfire="">
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
        <WebhookFields value={form.webhook} onChange={(webhook) => set({ webhook })} saved={base?.webhook ?? null}
                       problems={{ ...problems, url: problems.url ?? serverError(['webhook', 'webhook.url']) }}
                       scheduleId={base?.id ?? null} unsaved={!!changes?.webhook} />
      </div>

      {/* 有要保存的、或者有要看的报错时才贴在底部：没改动时一条空的保存栏压在表单上只是挡字 */}
      <div className={clsx('z-10 space-y-2 rounded-lg border bg-panel px-3 py-2.5 lg:col-span-2',
             (dirty || saveError) && 'sticky bottom-0 shadow-elev-2')} data-schedule-savebar="">
        {saveError?.conflict && (
          <Notice tone="err" attr={{ 'data-schedule-conflict': '' }}>
            <div>{saveError.message}</div>
            {saveError.detail && <div className="text-faint">{saveError.detail}</div>}
            <button type="button" className="btn btn-sm mt-1.5" onClick={() => void reload()} data-schedule-reload="">{T.reload}</button>
          </Notice>
        )}
        {saveError && !saveError.conflict && stillSays && (
          <Notice tone="err" attr={{ 'data-schedule-save-error': '' }}>{T.saveFailed}：{saveError.message}</Notice>
        )}
        <div className="flex flex-wrap items-center justify-end gap-2">
          {hint && <span className="mr-auto min-w-0 text-2xs leading-relaxed text-faint" data-schedule-hint="">{hint}</span>}
          {base
            ? dirty && <button type="button" className="btn" onClick={discard} disabled={saving}>{T.discard}</button>
            : <Link to="/schedules" className="btn">{T.cancel}</Link>}
          <button type="submit" className="btn btn-primary" disabled={!canSave} title={hint || undefined} data-schedule-save="">
            {saving && <Spinner size={11} />} {saving ? T.saving : base ? T.save : T.createSubmit}
          </button>
        </div>
      </div>
    </form>
  )
}

// ---------------------------------------------------------------------------
// 工作流
// ---------------------------------------------------------------------------

function WorkflowField({ form, workflows, state, workflow, base, error, onPick, onBlur }: {
  form: ScheduleForm; workflows: Workflow[]; state: string; workflow: Workflow | undefined; base: Schedule | null
  error?: string; onPick: (w: Workflow) => void; onBlur: () => void
}) {
  // 指向的工作流已经不在列表里（被删了）：照实说，不让人以为选的还是它
  const gone = state === 'ok' && !!form.workflowId && !workflow
  const message = error ?? (gone ? T.workflowGone(base?.workflow_name ?? form.workflowId) : undefined)
  return (
    <Field label={T.workflow} htmlFor="schedule-workflow" required error={message}
           hint={state === 'ok' && !workflows.length ? <>{T.workflowEmpty} <Link to="/studio" className="underline">{T.toStudio}</Link></> : undefined}>
      <WorkflowPicker id="schedule-workflow" value={form.workflowId} workflows={workflows} onChange={onPick} onBlur={onBlur}
                      fallbackLabel={base?.workflow_name} invalid={!!message} describedBy={message ? 'schedule-workflow-error' : undefined} />
    </Field>
  )
}

// ---------------------------------------------------------------------------
// 频率与时区
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// 预览
// ---------------------------------------------------------------------------

export type PreviewState =
  | { status: 'idle' }
  | { status: 'loading'; data?: SchedulePreview; timezone?: string }
  | { status: 'ok'; data: SchedulePreview; timezone: string }
  | { status: 'invalid'; message: string; field?: string }
  | { status: 'error'; error: unknown }

/** 预览防抖：停手 400ms 再问，新的一次发出时作废上一次 */
const PREVIEW_DEBOUNCE_MS = 400

/**
 * 接下来 5 次触发和渲染出的输入（服务端算，前端不自己解析 cron）。改动中保留上一次的结果并标「正在计算」；
 * 非法时清掉上一次的时间，只留服务端的说明，免得人照着旧的时间以为新的频率没问题。
 *
 * trigger 不为空是事件触发：交触发条件、不交 cron，服务端按一次示例事件渲染输入（没有触发时间）；对象还没选时不问
 */
export function usePreview(cron: string, timezone: string, template: Record<string, string>, workflowId: string,
  trigger: ScheduleTrigger | null = null): PreviewState {
  const [state, setState] = useState<PreviewState>({ status: 'idle' })
  const key = JSON.stringify([cron, timezone, Object.entries(template).sort(([a], [b]) => a.localeCompare(b)), workflowId,
    trigger])
  useEffect(() => {
    const subject = trigger ? trigger.source_id ?? trigger.workflow_id : null
    if (trigger ? !subject || !timezone : !cron || !timezone) {
      setState({ status: 'idle' })
      return
    }
    setState((s) => (s.status === 'ok' ? { status: 'loading', data: s.data, timezone: s.timezone }
      : s.status === 'loading' ? s : { status: 'loading' }))
    const ctrl = new AbortController()
    const timer = setTimeout(() => {
      api.schedules.preview({
        ...(trigger ? { trigger_kind: 'event' as const, trigger, cron: '' } : { cron, n: 5 }), timezone,
        ...(Object.keys(template).length ? { input_template: template } : {}),
        ...(workflowId ? { workflow_id: workflowId } : {}),
      }, { signal: ctrl.signal })
        .then((data) => setState({ status: 'ok', data, timezone }))
        .catch((e) => {
          if ((e as { name?: string })?.name === 'AbortError') return
          if (e instanceof ApiError && e.status === 422 && !isUnsupported(e)) {
            setState({ status: 'invalid', message: e.message, field: e.field })
          } else {
            setState({ status: 'error', error: e })
          }
        })
    }, PREVIEW_DEBOUNCE_MS)
    return () => {
      clearTimeout(timer)
      ctrl.abort()
    }
    // key 已经把五项都编进去了
  }, [key])
  return state
}

export function PreviewBox({ preview, event = false }: { preview: PreviewState; event?: boolean }) {
  const data = preview.status === 'ok' || preview.status === 'loading' ? preview.data : undefined
  const tz = preview.status === 'ok' || preview.status === 'loading' ? preview.timezone : undefined
  return (
    <section className="rounded-lg border bg-panel p-3.5" aria-labelledby="schedule-preview-title" aria-live="polite"
             data-schedule-preview="" data-status={preview.status} data-preview-mode={event ? 'event' : 'cron'}>
      <div className="mb-2 flex items-center gap-2">
        <h2 id="schedule-preview-title" className="text-xs font-semibold">{event ? TT.preview : T.preview}</h2>
        {preview.status === 'loading' && <Spinner size={11} />}
      </div>
      {data && event && <EventPreview data={data} loading={preview.status === 'loading'} />}
      {data && !event && (
        <>
          {data.description_text && <div className="mb-2 text-xs text-dim" data-preview-desc="">{data.description_text}</div>}
          {data.next?.length ? (
            <ol className={clsx('space-y-1', preview.status === 'loading' && 'opacity-60')}>
              {data.next.map((at, i) => {
                // 和列表、触发历史一个口径：不带时区后缀的按 UTC 读（new Date 会当成本机时间），读不了的照原文写
                const d = parseServerTime(at)
                return (
                  <li key={`${at}-${i}`} className="flex items-baseline gap-2 text-xs" data-preview-time="">
                    <span className="tnum w-4 shrink-0 text-right text-faint">{i + 1}</span>
                    <time className="tnum" dateTime={d?.toISOString()}>
                      {d && tz ? formatZoned(d, tz, 'full') : at}
                    </time>
                  </li>
                )
              })}
            </ol>
          ) : <p className="text-xs text-faint">{T.previewNone}</p>}
          {tz && <p className="mt-2 text-2xs text-faint">{T.previewZone(tz)}</p>}
        </>
      )}
      {!data && preview.status === 'loading' && <p className="text-xs text-faint">{T.previewLoading}</p>}
      {preview.status === 'idle' && <p className="text-xs text-faint">{event ? TT.previewIdle : T.previewIdle}</p>}
      {preview.status === 'invalid' && <p className="text-xs leading-relaxed text-[var(--err)]" data-preview-error="">{preview.message}</p>}
      {preview.status === 'error' && (
        <p className="text-xs leading-relaxed text-[var(--err)]" data-preview-error="">
          {isUnsupported(preview.error) ? T.unsupported : `${T.previewFailed}：${errorMessage(preview.error)}`}
        </p>
      )}
    </section>
  )
}

/**
 * 事件触发的预览：触发说明、按哪一次事件渲染（对象最近一次的事件，没有时是示例值、照实说），渲染出的输入逐项列出。
 * 没有触发时间可列：事件什么时候来不由时间定
 */
function EventPreview({ data, loading }: { data: SchedulePreview; loading: boolean }) {
  const inputs = Object.entries(data.inputs ?? {})
  return (
    <div className={clsx('space-y-2', loading && 'opacity-60')}>
      {data.description_text && <div className="text-xs text-dim" data-preview-desc="">{data.description_text}</div>}
      {data.event && (
        <div className="text-2xs leading-relaxed" data-preview-event={data.event.real ? 'real' : 'example'}>
          <div className="text-fg">{data.event.text}</div>
          <div className={data.event.real ? 'text-faint' : 'text-[var(--warn)]'}>
            {data.event.real ? TT.previewLatest : TT.previewExample}
          </div>
        </div>
      )}
      <div>
        <div className="text-2xs text-faint">{TT.previewInputs}</div>
        {inputs.length ? (
          <dl className="mt-0.5 space-y-0.5 text-2xs">
            {inputs.map(([k, v]) => (
              <div key={k} className="flex min-w-0 gap-1.5" data-preview-input={k}>
                <dt className="mono shrink-0 text-dim">{k}</dt>
                <dd className="mono min-w-0 break-all text-fg">{v === '' ? T.renderedEmpty : v}</dd>
              </div>
            ))}
          </dl>
        ) : <p className="text-2xs text-faint">{TT.previewNoInputs}</p>}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// 运行类别
// ---------------------------------------------------------------------------

const RUN_CLASSES: readonly ScheduleRunClass[] = ['formal', 'exploratory']

/**
 * 运行类别。正式运行要工作流已发布：新建、或者这次改了工作流 / 运行类别时服务端保存就会拒，problem 写在这里、
 * 保存按钮跟着禁用；已保存的定时任务没动这两样、工作流后来撤了发布时保存照常，只提醒到点会发起失败
 */
function RunClassSection({ value, workflow, problem, error, onChange }: {
  value: ScheduleRunClass; workflow: Workflow | undefined
  /** 保存前要处理的问题（formProblems 的 runClass） */
  problem?: string
  /** 保存被拒、服务端指明是运行类别时的说明 */
  error?: string
  onChange: (v: ScheduleRunClass) => void
}) {
  const radio = useRadioGroup(RUN_CLASSES, value, onChange)
  const unpublished = !!workflow && !workflow.published_version
  const describedBy = [problem ? 'schedule-class-problem' : null, error ? 'schedule-class-error' : null].filter(Boolean).join(' ')
  return (
    <section className="space-y-2.5 rounded-lg border bg-panel p-3.5" aria-labelledby="schedule-class-title">
      <h2 id="schedule-class-title" className="text-xs font-semibold">{T.runClass}</h2>
      <div role="radiogroup" aria-labelledby="schedule-class-title" aria-describedby={describedBy || undefined}
           className="grid gap-2 sm:grid-cols-2">
        {RUN_CLASSES.map((rc) => {
          const on = value === rc
          return (
            <button key={rc} type="button" {...radio(rc)} onClick={() => onChange(rc)} data-run-class={rc}
                    className={clsx('relative flex flex-col justify-start rounded-lg border px-3 py-2.5 text-left transition-colors',
                      on ? 'border-[var(--accent)] bg-accent-soft' : 'hover:bg-hover')}>
              {on && (
                <span className="absolute right-2 top-2 flex h-4 w-4 items-center justify-center rounded-full bg-accent-solid text-on-accent" aria-hidden>
                  <Check size={10} strokeWidth={3} />
                </span>
              )}
              <span className="pr-5 text-xs font-semibold">{rc === 'formal' ? T.formal : T.exploratory}</span>
              <span className="mt-1 text-2xs leading-relaxed text-faint">{rc === 'formal' ? T.formalHint : T.exploratoryHint}</span>
            </button>
          )
        })}
      </div>
      {problem ? (
        <Notice tone="err" attr={{ id: 'schedule-class-problem', 'data-run-class-problem': '' }}>{problem}</Notice>
      ) : value === 'formal' && unpublished && (
        <Notice tone="warn" attr={{ 'data-not-published': '' }}>{T.notPublished}</Notice>
      )}
      {value === 'formal' && workflow?.published_version != null && (
        <p className="text-2xs leading-relaxed text-faint">{T.formalVersion(workflow.published_version)}</p>
      )}
      {error && (
        <div id="schedule-class-error" className="text-2xs leading-relaxed text-[var(--err)]" role="alert" data-run-class-error="">{error}</div>
      )}
    </section>
  )
}

// ---------------------------------------------------------------------------
// 输入
// ---------------------------------------------------------------------------

/** 已发布版本的输入字段，按（工作流, 版本）记一份：发布之后那一版就不会再变 */
const versionFields = new Map<string, InputField[]>()

type FieldsState =
  | { status: 'none' }
  | { status: 'loading' }
  | { status: 'error'; error: unknown; retry: () => void }
  | { status: 'ok'; fields: InputField[]; version: number | null }

/**
 * 这个工作流的输入字段。正式运行到点按当时的发布版本发起，字段取已发布的那一版（和画布可能不同）；
 * 探索运行用工作流当前保存的内容，字段取它的入口节点
 */
function useInputFields(workflow: Workflow | undefined, runClass: ScheduleRunClass): FieldsState {
  const published = runClass === 'formal' && workflow?.published_version ? workflow.published_version : null
  const key = workflow && published != null ? `${workflow.id}@${published}` : ''
  const [fetched, setFetched] = useState<{ key: string; state: FieldsState } | null>(null)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    if (!key || !workflow || published == null || versionFields.has(key)) return
    let alive = true
    setFetched({ key, state: { status: 'loading' } })
    api.workflows.version(workflow.id, published)
      .then((v) => {
        const list = v.input_fields?.length ? (v.input_fields as InputField[]) : graphFields(v.graph)
        versionFields.set(key, list)
        if (alive) setFetched({ key, state: { status: 'ok', fields: list, version: published } })
      })
      .catch((error) => {
        if (alive) setFetched({ key, state: { status: 'error', error, retry: () => setAttempt((n) => n + 1) } })
      })
    return () => { alive = false }
  }, [key, attempt])

  if (!workflow) return { status: 'none' }
  if (published == null) return { status: 'ok', fields: graphFields(workflow.graph), version: null }
  const hit = versionFields.get(key)
  if (hit) return { status: 'ok', fields: hit, version: published }
  return fetched?.key === key ? fetched.state : { status: 'loading' }
}

/**
 * 输入模板里能用的变量：服务端给，各处共用一份（按 cron 的一份，事件触发按事件种类各一份）；取不到时为空表，变量按钮
 * 照样在，只是说没有可用的
 */
const variablesOnce = new Map<string, Promise<ScheduleVariable[]>>()
function useVariables(event: ScheduleEventKind | null = null): ScheduleVariable[] | null {
  const [vars, setVars] = useState<{ key: string; list: ScheduleVariable[] } | null>(null)
  const key = event ?? 'cron'
  useEffect(() => {
    let alive = true
    let once = variablesOnce.get(key)
    if (!once) {
      once = (event ? api.schedules.eventVariables(event) : api.schedules.variables())
        .then((v) => (Array.isArray(v) ? v : []), (e) => {
          variablesOnce.delete(key)
          throw e
        })
      variablesOnce.set(key, once)
    }
    once.then((v) => { if (alive) setVars({ key, list: v }) }, () => { if (alive) setVars({ key, list: [] }) })
    return () => { alive = false }
  }, [key]) // eslint-disable-line react-hooks/exhaustive-deps
  return vars?.key === key ? vars.list : null
}

const shownValue = (v: unknown) => (typeof v === 'string' ? v : v == null ? '' : JSON.stringify(v))

function InputsSection({ workflow, fields, inputs, variables, preview, recheck, event = false, error, onChange }: {
  workflow: Workflow | undefined
  fields: FieldsState
  inputs: Record<string, string>
  variables: ScheduleVariable[] | null
  preview: PreviewState
  /** 保存时服务端会不会核对输入（新建，或者改了工作流、运行类别、输入）：会的话必填没填、字段对不上都要先处理 */
  recheck: boolean
  /** 事件触发：模板变量另按事件计算（说明不同） */
  event?: boolean
  error?: string
  onChange: (inputs: Record<string, string>) => void
}) {
  const refs = useRef<Record<string, HTMLInputElement | null>>({})
  const [open, setOpen] = useState<string | null>(null)
  const rendered = preview.status === 'ok' ? preview.data.inputs ?? {} : null
  const names = fields.status === 'ok' ? new Set(fields.fields.map((f) => f.name)) : null
  // 对不上的字段：只列填了值的（留空的本来就不交）
  const stale = names ? Object.keys(inputs).filter((k) => !names.has(k) && inputs[k]?.trim()) : []
  // 正式运行的字段取已发布的版本：画布上有、那一版里没有的，说清是哪一版里没有，不说成工作流里已经删了
  const draft = useMemo(() => new Set(graphFields(workflow?.graph).map((f) => f.name)), [workflow])
  const staleText = (name: string) => {
    const reason = fields.status === 'ok' && fields.version != null && draft.has(name) ? T.staleNotInVersion(fields.version) : T.staleField
    // 入口声明了字段时服务端只收这些，交上去会被拒；入口什么都没声明时什么键都收
    return recheck && fields.status === 'ok' && fields.fields.length ? `${reason}，${T.staleRemove}` : reason
  }

  const setValue = (name: string, value: string) => onChange({ ...inputs, [name]: value })
  /** 变量插在光标处（选中了文字就替换它），插完光标落在变量后面 */
  const insert = (name: string, token: string) => {
    const el = refs.current[name]
    const cur = inputs[name] ?? ''
    const start = el?.selectionStart ?? cur.length
    const end = el?.selectionEnd ?? cur.length
    setValue(name, cur.slice(0, start) + token + cur.slice(end))
    requestAnimationFrame(() => {
      el?.focus()
      el?.setSelectionRange(start + token.length, start + token.length)
    })
  }

  let body
  if (fields.status === 'none') body = <p className="text-xs text-faint">{T.inputsPick}</p>
  else if (fields.status === 'loading') body = <Skeleton rows={2} height={28} />
  else if (fields.status === 'error') {
    body = (
      <Notice tone="err">
        {T.inputsFailed}：{errorMessage(fields.error)}
        <button type="button" className="btn btn-xs ml-2" onClick={fields.retry}>{T.retry}</button>
      </Notice>
    )
  } else if (!fields.fields.length && !stale.length) {
    body = <p className="text-xs text-faint">{T.inputsNone}</p>
  } else {
    body = (
      <div className="space-y-3">
        {fields.fields.map((field) => {
          const value = inputs[field.name] ?? ''
          const id = `schedule-input-${field.name}`
          const varsId = `${id}-vars`
          const noDefault = field.required && (field.default == null || shownValue(field.default) === '')
          const out = rendered && value.trim() && field.name in rendered ? shownValue(rendered[field.name]) : null
          return (
            <div key={field.name} className="min-w-0" data-input-field={field.name}>
              <label className="label flex flex-wrap items-baseline gap-x-1.5" htmlFor={id}>
                <span className="mono text-fg">{field.name}</span>
                {field.required && <span className="text-[var(--err)]" aria-label={T.required} data-required="">*</span>}
                {field.description && <span className="font-normal text-faint">{field.description}</span>}
              </label>
              <div className="flex flex-wrap gap-1.5">
                <input id={id} ref={(el) => { refs.current[field.name] = el }} className="field mono min-w-0 flex-1 basis-48" value={value}
                       placeholder={field.default != null && shownValue(field.default) !== '' ? T.defaultValue(shownValue(field.default)) : ''}
                       autoComplete="off" spellCheck={false} onChange={(e) => setValue(field.name, e.target.value)}
                       aria-describedby={[out != null ? `${id}-rendered` : null, noDefault && !value.trim() ? `${id}-required` : null]
                         .filter(Boolean).join(' ') || undefined} />
                <button type="button" className="btn btn-sm" aria-expanded={open === field.name} aria-controls={varsId}
                        onClick={() => setOpen((o) => (o === field.name ? null : field.name))} data-input-vars={field.name}>
                  {open === field.name ? T.variablesClose : T.variables}
                </button>
              </div>
              {open === field.name && (
                <div id={varsId} role="group" aria-label={T.variablesLabel(field.name)} className="mt-1.5">
                  <div className="flex flex-wrap gap-1">
                    {variables === null ? <Spinner size={11} /> : variables.length ? variables.map((v) => (
                      <button key={v.name} type="button" className="btn btn-xs" title={T.variableTitle(v.name, v.example)}
                              onClick={() => insert(field.name, `{{ ${v.name} }}`)} data-var={v.name}>
                        {v.label}
                      </button>
                    )) : <span className="text-2xs text-faint">{T.variablesEmpty}</span>}
                  </div>
                  {/* 补充说明（ISO 周的跨年规则）：几个变量同一句的只写一遍，写在按钮下面，触屏也看得到 */}
                  {[...new Set((variables ?? []).map((v) => v.note?.trim() ?? '').filter(Boolean))].map((note) => (
                    <p key={note} className="mt-1 text-2xs leading-relaxed text-faint" data-var-note="">{note}</p>
                  ))}
                </div>
              )}
              {out != null && (
                <div id={`${id}-rendered`} className="mt-1 break-all text-2xs text-dim" data-input-rendered={field.name}>
                  {out === '' ? T.renderedEmpty : T.rendered(out)}
                </div>
              )}
              {noDefault && !value.trim() && (
                <div id={`${id}-required`} className="mt-1 text-2xs" style={{ color: 'var(--warn)' }}>
                  {recheck ? T.requiredFill : T.requiredNoDefault}
                </div>
              )}
            </div>
          )
        })}
        {stale.map((name) => (
          <div key={name} className="flex min-w-0 flex-wrap items-center gap-1.5 text-2xs" data-input-stale={name}>
            <span className="mono text-dim">{name}</span>
            <span className="mono min-w-0 truncate text-faint">{inputs[name]}</span>
            <span style={{ color: 'var(--warn)' }}>{staleText(name)}</span>
            <button type="button" className="btn btn-xs" onClick={() => {
              const { [name]: _, ...rest } = inputs
              onChange(rest)
            }}>{T.removeField}</button>
          </div>
        ))}
      </div>
    )
  }

  return (
    <section className="space-y-2.5 rounded-lg border bg-panel p-3.5" aria-labelledby="schedule-inputs-title">
      <div>
        <h2 id="schedule-inputs-title" className="text-xs font-semibold">{T.inputs}</h2>
        <p className="mt-0.5 text-2xs leading-relaxed text-faint">{event ? TT.inputsHint : T.inputsHint}</p>
        {workflow && fields.status === 'ok' && (
          <p className="mt-0.5 text-2xs text-faint" data-inputs-source="">
            {fields.version != null ? T.inputsFromVersion(fields.version) : T.inputsFromDraft}
          </p>
        )}
      </div>
      {body}
      {error && <div className="text-2xs leading-relaxed text-[var(--err)]" role="alert" data-inputs-error="">{error}</div>}
    </section>
  )
}
