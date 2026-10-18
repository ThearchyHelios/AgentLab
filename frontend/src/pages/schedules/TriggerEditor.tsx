import { useEffect, useMemo, useState } from 'react'
import clsx from 'clsx'
import { api } from '../../api/client'
import { Field, Notice, Skeleton, useRadioGroup } from '../../components/ui'
import { errorMessage } from '../../lib/errors'
import { timezoneOptions } from '../../lib/freq'
import { TRIGGER_TEXT as TT } from '../../lib/termsTrigger'
import type { ScheduleEventKind, ScheduleTriggerKind, ScheduleTriggerOptions } from '../../types'
import {
  BACKFILL_ORIGIN, DEBOUNCE_OPTIONS, EVENT_KINDS, TRIGGER_KINDS, graceLabel, switchEvent, type TriggerForm,
} from './model'
import { isUnsupported } from './parts'

/**
 * 数据事件触发的表单部件（docs/design/orchestration-round/00-总方案.md §4）：新建时选「定时 / 事件触发」（TriggerModePicker），
 * 选了事件触发时用 EventSection 替换频率那一节——事件种类、对象、过滤、去抖、时区。
 *
 * 能选哪些对象、不能选的为什么不能选，都是服务端给的（GET /api/schedules/trigger-options）：这里只照着画，不自己判「是不是
 * 按期累积」「发没发布」；会不会串成环保存时由服务端核对，422 的说明落到这一节（error）。
 */

/** 新建时选触发方式；修改时只读地写出是哪一种（触发方式建好之后不能改） */
export function TriggerModePicker({ value, onChange, fixed }: {
  value: ScheduleTriggerKind
  onChange: (v: ScheduleTriggerKind) => void
  /** 修改中的定时任务：只读 */
  fixed?: boolean
}) {
  const radio = useRadioGroup(TRIGGER_KINDS, value, onChange)
  if (fixed) {
    return <p className="text-2xs leading-relaxed text-faint" data-trigger-mode-fixed={value}>{TT.modeFixed(TT.mode[value])}</p>
  }
  return (
    <div data-trigger-modes="">
      <div className="label" id="schedule-trigger-mode">{TT.modeLabel}</div>
      <div role="radiogroup" aria-labelledby="schedule-trigger-mode" className="flex flex-wrap gap-1">
        {TRIGGER_KINDS.map((k) => (
          <button key={k} type="button" {...radio(k)} onClick={() => onChange(k)} data-trigger-mode={k}
                  className={clsx('rounded-md border px-2.5 py-1 text-xs transition-colors',
                    value === k ? 'border-[var(--accent)] bg-accent-soft text-fg' : 'text-dim hover:bg-hover')}>
            {TT.mode[k]}
          </button>
        ))}
      </div>
      <p className="mt-1 text-2xs leading-relaxed text-faint">{TT.modeHint[value]}</p>
    </div>
  )
}

type OptionsState =
  | { status: 'loading' }
  | { status: 'ok'; data: ScheduleTriggerOptions }
  | { status: 'error'; error: unknown; retry: () => void }

/** 可选的对象：各处共用一份，失败时清掉、下次再取 */
let optionsOnce: Promise<ScheduleTriggerOptions> | null = null
function useTriggerOptions(): OptionsState {
  const [state, setState] = useState<OptionsState>({ status: 'loading' })
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    let alive = true
    setState({ status: 'loading' })
    optionsOnce ??= api.schedules.triggerOptions().catch((e) => {
      optionsOnce = null
      throw e
    })
    optionsOnce.then(
      (data) => { if (alive) setState({ status: 'ok', data }) },
      (error) => { if (alive) setState({ status: 'error', error, retry: () => setAttempt((n) => n + 1) }) },
    )
    return () => { alive = false }
  }, [attempt])
  return state
}

export function EventSection({ value, onChange, timezone, setTimezone, savedName, error, debounceError }: {
  value: TriggerForm
  onChange: (t: TriggerForm) => void
  timezone: string
  setTimezone: (tz: string) => void
  /** 修改时对象的名字（不在可选列表里时写它，标「已不存在」） */
  savedName?: string | null
  /** 这一节的问题（还没选对象）或保存被拒时服务端的说明（对象不合用、会循环触发） */
  error?: string
  debounceError?: string
}) {
  const options = useTriggerOptions()
  const radio = useRadioGroup(EVENT_KINDS, value.event, (event) => onChange(switchEvent(value, event)))
  const zones = useMemo(() => timezoneOptions(timezone), [timezone])
  const set = (patch: Partial<TriggerForm>) => onChange({ ...value, ...patch })
  const kind = value.event
  const label = TT.subject[kind]
  const entry = options.status === 'ok' ? options.data.events.find((e) => e.event === kind) : undefined
  const objects = entry?.objects ?? []
  const listed = objects.some((o) => o.id === value.subjectId)

  let picker
  if (options.status === 'loading') picker = <Skeleton rows={1} height={30} />
  else if (options.status === 'error') {
    picker = (
      <Notice tone="err">
        {isUnsupported(options.error) ? TT.optionsUnsupported : `${TT.optionsFailed}：${errorMessage(options.error)}`}
        {!isUnsupported(options.error) && (
          <button type="button" className="btn btn-xs ml-2" onClick={options.retry}>{TT.retry}</button>
        )}
      </Notice>
    )
  } else {
    picker = (
      <select id="schedule-trigger-subject" className="field" value={value.subjectId} data-trigger-subject={kind}
              aria-invalid={!!error || undefined}
              aria-describedby={error ? 'schedule-trigger-subject-error' : undefined}
              onChange={(e) => set({ subjectId: e.target.value })}>
        <option value="">{objects.length ? TT.subjectPlaceholder(label) : TT.subjectEmpty[kind]}</option>
        {value.subjectId && !listed && <option value={value.subjectId}>{TT.subjectGone(savedName || value.subjectId)}</option>}
        {objects.map((o) => (
          <option key={o.id} value={o.id} disabled={!o.ok && o.id !== value.subjectId} data-trigger-object-ok={String(o.ok)}>
            {o.ok || !o.reason ? o.name : TT.subjectNotOk(o.name, o.reason)}
          </option>
        ))}
      </select>
    )
  }

  return (
    <section className="space-y-3 rounded-lg border bg-panel p-3.5" aria-labelledby="schedule-trigger-title" data-trigger-section="">
      <h2 id="schedule-trigger-title" className="text-xs font-semibold">{TT.section}</h2>
      <div>
        <div className="label" id="schedule-trigger-event">{TT.eventLabel}</div>
        <div role="radiogroup" aria-labelledby="schedule-trigger-event" className="flex flex-wrap gap-1">
          {EVENT_KINDS.map((e: ScheduleEventKind) => (
            <button key={e} type="button" {...radio(e)} onClick={() => onChange(switchEvent(value, e))} data-trigger-event={e}
                    className={clsx('rounded-md border px-2.5 py-1 text-xs transition-colors',
                      kind === e ? 'border-[var(--accent)] bg-accent-soft text-fg' : 'text-dim hover:bg-hover')}>
              {TT.event[e]}
            </button>
          ))}
        </div>
        <p className="mt-1 text-2xs leading-relaxed text-faint">{TT.eventHint[kind]}</p>
      </div>

      <Field label={label} htmlFor="schedule-trigger-subject" required>
        {picker}
      </Field>
      {error && (
        <div id="schedule-trigger-subject-error" className="-mt-1.5 text-2xs leading-relaxed text-[var(--err)]" role="alert"
             data-trigger-error="">
          {error}
        </div>
      )}

      {kind === 'table.version' && (
        <Field label={TT.periodLabel} htmlFor="schedule-trigger-period" hint={TT.periodHint}>
          <select id="schedule-trigger-period" className="field w-auto max-w-full" value={value.period} data-trigger-period=""
                  aria-describedby="schedule-trigger-period-hint"
                  onChange={(e) => set({ period: e.target.value === 'new' ? 'new' : 'any' })}>
            <option value="any">{TT.period.any}</option>
            <option value="new">{TT.period.new}</option>
          </select>
        </Field>
      )}
      {kind === 'workflow.finished' && options.status === 'ok' && (
        <div className="space-y-1">
          <div className="flex flex-wrap gap-3">
            <Field label={TT.appLabel} htmlFor="schedule-trigger-app" className="min-w-[12rem] flex-1">
              <select id="schedule-trigger-app" className="field" value={value.appId} data-trigger-app=""
                      onChange={(e) => set({ appId: e.target.value })}>
                <option value="">{TT.appAny}</option>
                {value.appId && !options.data.apps.some((a) => a.id === value.appId) && (
                  <option value={value.appId}>{TT.subjectGone(value.appId)}</option>
                )}
                {options.data.apps.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
              </select>
            </Field>
            <Field label={TT.originLabel} htmlFor="schedule-trigger-origin" className="min-w-[12rem] flex-1">
              <select id="schedule-trigger-origin" className="field" value={value.originKind} data-trigger-origin=""
                      onChange={(e) => set({ originKind: e.target.value })}>
                <option value="">{TT.originAny}</option>
                {options.data.origins.map((o) => <option key={o.kind} value={o.kind}>{o.label}</option>)}
              </select>
            </Field>
          </div>
          <p className="text-2xs leading-relaxed text-faint">{TT.filterHint}</p>
          {/* 缺省不认的两种成功结束：途经驳回的、应用补算的（服务端 triggers.matches），要认的勾上 */}
          <div className="space-y-1 pt-1">
            <label className="flex items-start gap-2 text-xs">
              <input type="checkbox" className="mt-0.5" checked={value.includeRejected} data-trigger-include-rejected=""
                     onChange={(e) => set({ includeRejected: e.target.checked })} />
              <span>{TT.includeRejected}<span className="block text-2xs leading-relaxed text-faint">{TT.includeRejectedHint}</span></span>
            </label>
            {value.originKind !== BACKFILL_ORIGIN && (
              <label className="flex items-start gap-2 text-xs">
                <input type="checkbox" className="mt-0.5" checked={value.includeBackfill} data-trigger-include-backfill=""
                       onChange={(e) => set({ includeBackfill: e.target.checked })} />
                <span>{TT.includeBackfill}<span className="block text-2xs leading-relaxed text-faint">{TT.includeBackfillHint}</span></span>
              </label>
            )}
          </div>
          <p className="text-2xs leading-relaxed text-faint" data-trigger-chain="">{TT.chainHint(options.data.max_depth)}</p>
        </div>
      )}

      <Field label={TT.debounce} htmlFor="schedule-trigger-debounce" hint={TT.debounceHint} error={debounceError}>
        <select id="schedule-trigger-debounce" className="field w-auto max-w-full" value={value.debounceS} data-trigger-debounce=""
                aria-invalid={!!debounceError || undefined}
                aria-describedby={debounceError ? 'schedule-trigger-debounce-error' : 'schedule-trigger-debounce-hint'}
                onChange={(e) => set({ debounceS: Number(e.target.value) })}>
          {[...new Set([...DEBOUNCE_OPTIONS, value.debounceS])].sort((a, b) => a - b).map((s) => (
            <option key={s} value={s}>{TT.debounceOption(s ? graceLabel(s) : '')}</option>
          ))}
        </select>
      </Field>

      <Field label={TT.timezone} htmlFor="schedule-trigger-timezone" hint={TT.timezoneHint}>
        <select id="schedule-trigger-timezone" className="field" value={timezone} data-timezone=""
                aria-describedby="schedule-trigger-timezone-hint" onChange={(e) => setTimezone(e.target.value)}>
          {zones.map((z) => <option key={z.tz} value={z.tz}>{z.label}</option>)}
        </select>
      </Field>
    </section>
  )
}
