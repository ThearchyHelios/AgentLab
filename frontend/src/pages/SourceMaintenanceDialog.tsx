import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { AlertTriangle, CalendarClock, Info } from 'lucide-react'
import { ApiError, api } from '../api/client'
import type { SchedulePatch, SchedulePreview, ScheduleTargetCreate } from '../types'
import type { MaintenanceSchedule, MaintenanceStep, SourceMaintenance } from '../typesSource'
import { FreqPicker } from '../components/FreqPicker'
import { ErrorState, Modal, Skeleton, Spinner, toast, useTicker } from '../components/ui'
import { formatDateTime, parseServerTime } from '../lib/format'
import { DEFAULT_FREQ, DEFAULT_GRACE_S, browserTimezone, cronOf, freqOf, freqProblem } from '../lib/freq'
import type { Freq } from '../lib/freq'
import { MAINTENANCE_TEXT as T } from '../lib/terms'
import { EnableSwitch, FireStatusChip, NextRun } from './schedules/parts'

// ===========================================================================
// 数据源的定时维护（docs/design/next-round/03-数据接入与目录维护.md B4，目录-D5）：按数据源开、缺省关、只在设定的时段跑。
//
// 定时只有一套（总方案 X4）：维护就是一条 target_kind=source_maintenance、target_id=数据源 id 的定时任务，这里是它的快捷入口。
// 频率用定时任务同一个选择器（components/FreqPicker），保存成 POST /api/schedules（ScheduleTargetCreate）；已经有了的改频率、
// 开关走 PATCH（SchedulePatch，带读到的版本）。做哪几步存在数据源上（PUT /api/datasources/{id}/maintenance）。下一次什么
// 时候、最近一次建了哪些任务、哪几步没执行和原因都照服务端给的写（触发记录的 inputs）；剖析没开、调度没开时照实说到点会怎样。
//
// 放在数据源卡片上的独立对话框（和「探查范围」一样），不塞进编辑表单：编辑表单只有一个「保存」，存的是连接配置；维护是一条
// 单独的定时任务，存法不同，两个保存混在一起出错时说不清哪一样没存上。
// ===========================================================================

const STEPS: readonly MaintenanceStep[] = ['introspect', 'profile_needed', 'observe']
/** 新开的维护缺省在凌晨三点：低峰时段 */
const DEFAULT_MAINTENANCE_FREQ: Freq = { ...DEFAULT_FREQ, preset: 'daily', time: '03:00' }
const PREVIEW_DEBOUNCE_MS = 400

interface Form {
  enabled: boolean
  steps: MaintenanceStep[]
  freq: Freq
  timezone: string
}

const formOf = (m: SourceMaintenance, s: MaintenanceSchedule | null): Form => ({
  enabled: s?.enabled ?? false,
  steps: m.steps,
  freq: s ? freqOf(s.cron) : DEFAULT_MAINTENANCE_FREQ,
  timezone: s?.timezone ?? browserTimezone(),
})

export function SourceMaintenanceDialog({ source, onClose, onSaved }: {
  source: { id: string; name: string }
  onClose: () => void
  /** 保存过了：卡片上的「定时维护」标签跟着变 */
  onSaved: () => void
}) {
  const navigate = useNavigate()
  const [data, setData] = useState<SourceMaintenance | null>(null)
  const [loadError, setLoadError] = useState<unknown>(null)
  const [form, setForm] = useState<Form | null>(null)
  const initial = useRef('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<{ field?: string; message: string } | null>(null)
  const [preview, setPreview] = useState<{ cron: string; tz: string; data?: SchedulePreview; error?: { field?: string; message: string } } | null>(null)
  useTicker(30_000)

  const schedule = data?.schedules[0] ?? null
  const extra = Math.max(0, (data?.schedules.length ?? 0) - 1)

  const load = async () => {
    const got = await api.sourceChanges.maintenance(source.id)
    setData(got)
    const f = formOf(got, got.schedules[0] ?? null)
    initial.current = JSON.stringify({ ...f, cron: cronOf(f.freq) })
    setForm(f)
    setLoadError(null)
  }
  useEffect(() => { load().catch(setLoadError) }, [source.id])

  const cron = form ? cronOf(form.freq) : ''
  const freqIncomplete = form ? freqProblem(form.freq) : null
  const sig = form ? JSON.stringify({ ...form, cron }) : ''
  const dirty = !!form && sig !== initial.current
  const noSteps = !!form && !form.steps.length

  // 频率的说法和接下来几次：服务端算（前端不解析 cron），停手 400 毫秒再问，新的一次发出时作废上一次
  useEffect(() => {
    if (!form || !cron || freqIncomplete) {
      setPreview(null)
      return
    }
    const ctrl = new AbortController()
    const tz = form.timezone
    const timer = setTimeout(() => {
      api.schedules.preview({ cron, timezone: tz, n: 3 }, { signal: ctrl.signal })
        .then((d) => setPreview({ cron, tz, data: d }))
        .catch((e) => {
          if ((e as { name?: string })?.name === 'AbortError') return
          setPreview({ cron, tz, error: { field: e instanceof ApiError ? e.field : undefined, message: (e as Error).message } })
        })
    }, PREVIEW_DEBOUNCE_MS)
    return () => { clearTimeout(timer); ctrl.abort() }
  }, [cron, form?.timezone, freqIncomplete])

  const freqError = error?.field === 'cron' ? error.message : preview?.error && preview.error.field !== 'timezone' ? preview.error.message
    : form?.enabled ? freqIncomplete ?? undefined : undefined
  const tzError = error?.field === 'timezone' ? error.message : preview?.error?.field === 'timezone' ? preview.error.message : undefined

  const save = async () => {
    if (!form || !data || saving) return
    if (noSteps || (form.enabled && (freqIncomplete || preview?.error))) return
    setSaving(true)
    setError(null)
    try {
      const was = JSON.parse(initial.current) as Form & { cron: string }
      if (JSON.stringify(form.steps) !== JSON.stringify(was.steps)) await api.sourceChanges.putMaintenance(source.id, form.steps)
      if (schedule) {
        const body: SchedulePatch = { version: schedule.version }
        if (cron && cron !== schedule.cron) body.cron = cron
        if (form.timezone !== schedule.timezone) body.timezone = form.timezone
        if (form.enabled !== schedule.enabled) body.enabled = form.enabled
        if (Object.keys(body).length > 1) await api.schedules.update(schedule.id, body)
      } else if (form.enabled) {
        const body: ScheduleTargetCreate = {
          name: T.scheduleName(source.name), target_kind: 'source_maintenance', target_id: source.id, cron, timezone: form.timezone,
          enabled: true, overlap: 'skip', misfire_grace_s: DEFAULT_GRACE_S,
        }
        await api.schedules.create(body)
      }
      await load()
      toast.ok(form.enabled ? T.saved : T.savedOff)
      onSaved()
    } catch (e) {
      // 频率、时区不合规（422 带 field）落到那一项；版本不符（别人刚改过，409）、别的原因写在底部
      if (e instanceof ApiError && e.kind === 'http' && (e.status === 422 || e.status === 409)) setError({ field: e.field, message: e.message })
      else toast.error(e)
    } finally {
      setSaving(false)
    }
  }

  const set = (patch: Partial<Form>) => {
    setError(null)
    setForm((f) => f && ({ ...f, ...patch }))
  }
  const toggleStep = (step: MaintenanceStep) => {
    if (!form) return
    const on = form.steps.includes(step)
    set({ steps: STEPS.filter((s) => (s === step ? !on : form.steps.includes(s))) })
  }

  const profileSteps = !!form && form.steps.some((s) => s !== 'introspect')
  const blocked = !form || noSteps || (form.enabled && (!!freqIncomplete || !!preview?.error))

  return (
    <Modal open onClose={onClose} dirty={dirty} width={600} title={T.title(source.name)}
           footer={(
             <>
               {error && !error.field && <p className="mr-auto min-w-0 text-2xs text-[var(--err)]" role="alert" data-maintenance-error="">{error.message}</p>}
               <button className="btn" onClick={onClose}>{T.cancel}</button>
               <button className="btn btn-primary" disabled={saving || blocked || !dirty || !data?.maintained} onClick={() => void save()}
                       title={!dirty ? T.nothing : noSteps ? T.stepsNone : undefined} data-maintenance-save="">
                 {saving && <Spinner size={11} />} {T.save}
               </button>
             </>
           )}>
      {!data || !form
        ? (loadError ? <ErrorState error={loadError} onRetry={() => void load().catch(setLoadError)} /> : <Skeleton rows={5} height={24} gap={10} />)
        : !data.maintained
          ? <p className="text-xs text-dim" data-maintenance-not="">{T.notMaintained}</p>
          : (
            <div className="space-y-3" data-maintenance-dialog={form.enabled ? 'on' : 'off'}>
              <p className="text-xs leading-relaxed text-dim">{T.intro}</p>

              <section className="space-y-2 rounded-lg border bg-panel p-3.5" aria-labelledby="maint-enable-title">
                <div className="flex items-center gap-2">
                  <h2 id="maint-enable-title" className="text-xs font-semibold">{T.enable}</h2>
                  <span className="flex-1" />
                  <span data-maintenance-enabled={form.enabled ? 'on' : 'off'}>
                    <EnableSwitch checked={form.enabled} label={T.enable} onChange={(v) => set({ enabled: v })} />
                  </span>
                </div>
                <p className="text-2xs leading-relaxed text-faint">{T.enableHint}</p>
                {form.enabled && (
                  <p className="flex items-start gap-1.5 text-2xs leading-relaxed text-[var(--warn)]" data-maintenance-offpeak="">
                    <AlertTriangle size={11} className="mt-0.5 shrink-0" aria-hidden /> {T.offPeak}
                  </p>
                )}
                {!data.scheduler_on && (
                  <p className="flex items-start gap-1.5 text-2xs leading-relaxed text-[var(--warn)]" data-maintenance-scheduler-off="">
                    <Info size={11} className="mt-0.5 shrink-0" aria-hidden /> {T.schedulerOff}
                  </p>
                )}
              </section>

              <fieldset className="space-y-2 rounded-lg border bg-panel p-3.5" aria-describedby="maint-steps-hint">
                <legend className="px-1 text-xs font-semibold">{T.stepsTitle}</legend>
                <p id="maint-steps-hint" className={noSteps ? 'text-2xs text-[var(--err)]' : 'text-2xs text-faint'} role={noSteps ? 'alert' : undefined}>
                  {noSteps ? T.stepsNone : T.stepsHint}
                </p>
                {STEPS.map((step) => (
                  <label key={step} className="flex items-start gap-2 text-xs" data-maintenance-step={step}>
                    <input type="checkbox" className="mt-0.5" checked={form.steps.includes(step)} onChange={() => toggleStep(step)} />
                    <span className="min-w-0">
                      <span className="font-medium">{T.step[step]}</span>
                      <span className="block text-2xs leading-relaxed text-faint">{T.stepHint[step]}</span>
                    </span>
                  </label>
                ))}
                {!data.profile_enabled && profileSteps && (
                  <p className="text-2xs leading-relaxed text-[var(--warn)]" data-maintenance-profile-off="">{T.profileOff}</p>
                )}
              </fieldset>

              <div className={form.enabled ? undefined : 'opacity-70'} data-maintenance-freq="">
                <FreqPicker idPrefix="maint" title={T.freqTitle} freq={form.freq} timezone={form.timezone}
                            setFreq={(patch) => set({ freq: { ...form.freq, ...patch } })} setTimezone={(tz) => set({ timezone: tz })}
                            freqError={freqError} tzError={tzError} />
                {preview?.data && (
                  <p className="mt-1.5 text-2xs text-dim" data-maintenance-preview="">
                    {preview.data.description_text}
                    {preview.data.next.length > 0 && <span className="tnum text-faint"> · {preview.data.next.map((n) => formatDateTime(n)).join('、')}</span>}
                  </p>
                )}
              </div>

              {schedule && <LastRun schedule={schedule} onOpenProfileJob={(id) => {
                onClose()
                navigate(`/data/catalog/${encodeURIComponent(source.id)}?pjob=${encodeURIComponent(id)}`)
              }} />}
              {extra > 0 && <p className="text-2xs text-faint" data-maintenance-extra={extra}>{T.extra(extra)}</p>}
            </div>
          )}
    </Modal>
  )
}

/** 已经有维护定时：下一次什么时候、最近一次建了哪些任务、哪几步没执行和原因（触发记录的 inputs），到定时任务页看全部触发 */
function LastRun({ schedule: s, onOpenProfileJob }: {
  schedule: MaintenanceSchedule; onOpenProfileJob: (id: string) => void
}) {
  const fire = s.last_fire
  const inputs = fire?.inputs ?? {}
  const at = fire ? parseServerTime(fire.fire_at) : null
  return (
    <section className="space-y-1.5 rounded-lg border bg-panel p-3.5 text-xs" aria-labelledby="maint-last-title" data-maintenance-last={fire?.status ?? 'none'}>
      <p className="flex flex-wrap items-center gap-1.5 text-dim" data-maintenance-next="">
        <CalendarClock size={12} aria-hidden />
        <span>{s.description_text}</span>
        <span className="text-faint">·</span>
        <NextRun schedule={s} now={Date.now()} />
      </p>
      <h3 id="maint-last-title" className="text-2xs font-semibold">{T.lastTitle}</h3>
      {!fire
        ? <p className="text-2xs text-faint">{T.lastNone}</p>
        : (
          <>
            <p className="flex flex-wrap items-center gap-1.5">
              <span className="tnum">{at ? formatDateTime(at) : fire.fire_at}</span>
              <FireStatusChip fire={fire} />
            </p>
            {(inputs.jobs ?? []).length > 0 && (
              <ul className="space-y-0.5">
                {(inputs.jobs ?? []).map((j) => (
                  <li key={j.id} className="flex flex-wrap items-center gap-2 text-2xs" data-maintenance-job={j.id} data-maintenance-job-kind={j.kind}>
                    <span className="mono">{T.lastJob(j.step.split('+').map((x) => T.step[x as MaintenanceStep] ?? x).join('、'),
                      T.lastJobKind[j.kind] ?? j.kind, j.id, j.tables ?? null)}</span>
                    {/* 剖析任务点得进进度和结果（数据目录页的剖析对话框）；探查任务进行中时进度就在这张数据源卡片上，没有另外的页面 */}
                    {j.kind === 'profile' && (
                      <button type="button" className="btn btn-xs" onClick={() => onOpenProfileJob(j.id)} data-maintenance-job-open="">{T.viewJob}</button>
                    )}
                  </li>
                ))}
              </ul>
            )}
            {(inputs.skipped ?? []).map((k) => (
              <p key={k.step} className="text-2xs leading-relaxed text-[var(--warn)]" data-maintenance-skipped={k.step}>
                {T.lastSkipped(T.step[k.step] ?? k.step, k.reason)}
              </p>
            ))}
            {!(inputs.jobs ?? []).length && !(inputs.skipped ?? []).length && fire.reason && (
              <p className="text-2xs leading-relaxed text-dim" data-maintenance-reason="">{T.lastReason(fire.reason)}</p>
            )}
          </>
        )}
      <Link to={`/schedules/${encodeURIComponent(s.id)}`} className="inline-block text-2xs text-[var(--accent)] underline-offset-2 hover:underline"
            data-maintenance-history="">{T.history}</Link>
    </section>
  )
}

/** 卡片上的「定时维护」标签：开着时写频率、暂停时照实写；没有维护定时不画 */
export function MaintenanceChip({ maintenance }: { maintenance: { enabled: boolean; description_text: string }[] }) {
  const first = maintenance[0]
  const text = useMemo(() => (first ? (first.enabled ? T.chip(first.description_text) : T.chipPaused) : ''), [first])
  if (!first) return null
  return (
    <span className="chip" title={T.chipHint} data-maintenance-chip={first.enabled ? 'on' : 'paused'}>
      <CalendarClock size={10} aria-hidden /> {text}
    </span>
  )
}
