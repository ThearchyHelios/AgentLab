import { useCallback, useEffect, useRef, useState } from 'react'
import clsx from 'clsx'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { ArrowLeft, CalendarClock, Play, Trash2 } from 'lucide-react'
import { ApiError, api } from '../../api/client'
import {
  EmptyState, ErrorState, IconButton, PageHeader, Skeleton, Spinner, Tabs, toast, useRadioGroup, useTicker,
} from '../../components/ui'
import { leavePass } from '../../lib/leave'
import { SCHEDULE_TEXT as T } from '../../lib/terms'
import { useOnReconnect } from '../../store/catalog'
import type { Schedule, ScheduleFire, ScheduleTargetKind } from '../../types'
import { FireHistory } from './FireHistory'
import {
  EnableSwitch, LastFire, NextRun, isUnsupported, rememberScheduleNames, removeSchedule, runNow, setEnabled, targetKindOf,
  targetText,
} from './parts'
import { CREATABLE_TARGETS, graceLabel } from './model'
import { ScheduleEditor } from './ScheduleEditor'
import { TargetEditor } from './TargetEditor'

const BACK = (
  <Link to="/schedules" className="btn btn-sm btn-ghost" aria-label={T.back} title={T.back} data-schedule-back="">
    <ArrowLeft size={12} aria-hidden /> <span className="hidden md:inline">{T.back}</span>
  </Link>
)

/**
 * 新建定时任务（/schedules/new）：先选定时什么——工作流（发起运行）还是评测题集（评一次）；数据源的定时维护在数据源页建，
 * 不在这里。?target_kind=eval_suite&target_id=… 直接落到评测题集、选好题集（评测页的「设定时」）。创建成功进它的详情页，
 * 替换掉这一条历史：后退不回到一张空表单
 */
export function ScheduleCreatePage() {
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const [kind, setKind] = useState<ScheduleTargetKind>(params.get('target_kind') === 'eval_suite' ? 'eval_suite' : 'workflow')
  const radio = useRadioGroup(CREATABLE_TARGETS, kind, setKind)
  const onSaved = (s: Schedule) => {
    rememberScheduleNames([s])
    toast.ok(T.created(s.name))
    // 表单刚保存完、离开守卫还没来得及撤：这一跳带通行证
    navigate(`/schedules/${encodeURIComponent(s.id)}`, leavePass({ replace: true }))
  }
  return (
    <div className="flex h-full flex-col" data-schedules-page="new">
      <PageHeader icon={<CalendarClock size={13} />} title={T.newTitle} subtitle={T.hint} actions={BACK} />
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-5xl px-4 pt-4" data-schedule-target-kinds="">
          <div className="label" id="schedule-target-kind">{T.targetKindLabel}</div>
          <div role="radiogroup" aria-labelledby="schedule-target-kind" className="flex flex-wrap gap-1">
            {CREATABLE_TARGETS.map((k) => (
              <button key={k} type="button" {...radio(k)} onClick={() => setKind(k)} data-schedule-target-kind-pick={k}
                      className={clsx('rounded-md border px-2.5 py-1 text-xs transition-colors',
                        kind === k ? 'border-[var(--accent)] bg-accent-soft text-fg' : 'text-dim hover:bg-hover')}>
                {T.targetKind[k]}
              </button>
            ))}
          </div>
          <p className="mt-1 text-2xs leading-relaxed text-faint">{T.targetKindHint[kind]}</p>
        </div>
        {kind === 'workflow'
          ? <ScheduleEditor key="workflow" schedule={null} onSaved={onSaved} />
          : <TargetEditor key="eval_suite" schedule={null} init={{ targetId: params.get('target_id') ?? undefined }} onSaved={onSaved} />}
      </div>
    </div>
  )
}

type TabKey = 'settings' | 'history'

/**
 * 页头的「上次触发」原地刷新（页面不可见时不刷）：平时同列表 30 秒一次；上次触发发起的运行还在排队、运行中时 5 秒一次，
 * 运行一结束页头就跟着变——不然触发历史里已经「已完成」，页头还挂着「运行中」
 */
const IDLE_REFRESH_MS = 30_000
const LIVE_REFRESH_MS = 5_000
const liveFire = (f: ScheduleFire | null | undefined) => !!f?.run_id && (f.run_status === 'queued' || f.run_status === 'running')

/**
 * 一个定时任务的详情（/schedules/:id）：页头是名字、频率和启用开关、立即运行、删除；下面两个标签，设置（表单）
 * 和触发历史（?tab=history）。切到触发历史时表单不卸载，改到一半的内容还在。
 *
 * 两份状态分开记：schedule 是页头和摘要显示的、随时可以刷新的最新值；version 是表单保存时带的版本，只跟着自己的
 * 写操作（开关、保存）和明确的「重新载入」走。别人在这期间改过时表单保存就该 409，不能因为页头顺手刷新过就悄悄覆盖。
 * 唯一的例外是表单第一次挂上的那一刻：表单基于的就是那时的 schedule，版本对齐到它
 */
export function ScheduleDetailPage({ id }: { id: string }) {
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const tab: TabKey = params.get('tab') === 'history' ? 'history' : 'settings'
  const [schedule, setSchedule] = useState<Schedule | null>(null)
  const [version, setVersion] = useState<number | undefined>(undefined)
  const [error, setError] = useState<unknown>(null)
  const [resetKey, setResetKey] = useState(0)
  const [historyKey, setHistoryKey] = useState(0)
  // 两个标签都是第一次打开时才挂上，之后切走也不卸载：直接打开触发历史时不必先把表单和预览请求一遍
  const [historySeen, setHistorySeen] = useState(tab === 'history')
  const [settingsSeen, setSettingsSeen] = useState(tab === 'settings')
  const [busy, setBusy] = useState<null | 'toggle' | 'run' | 'delete'>(null)
  const now = useTicker(30_000)
  const loaded = useRef(false)
  /**
   * 读的序号，两个：seq 记写（开关、保存成功）和 adopt 的读（首次打开、重新载入），tick 记原地刷新。
   * - 写作废还在路上的一切读：那次读可能在写之前读到旧值、在写的答复之后才回来，落地会把页头的开关、名字盖回旧的；
   * - adopt 的读作废在它之前发出的刷新（读得更早），后发的 adopt 胜过先发的；
   * - 刷新只作废更早的刷新，不碰 adopt 的读：运行中页头每 5 秒原地刷新一次，和「重新载入」撞上时要是把它作废了，
   *   表单不重置、版本不换，按钮像没反应
   */
  const seq = useRef(0)
  const tick = useRef(0)
  const wrote = () => { seq.current++ }

  /** 读最新的定时任务，读到了返回 true。adopt：表单保存时也改用它的版本（首次打开、重新载入）；不 adopt 的是原地刷新 */
  const load = useCallback(async (adopt: boolean): Promise<boolean> => {
    const n = adopt ? ++seq.current : seq.current
    const m = ++tick.current
    const current = () => n === seq.current && (adopt || m === tick.current)
    try {
      const s = await api.schedules.get(id)
      if (!current()) return false
      rememberScheduleNames([s])
      setSchedule(s)
      setError(null)
      if (adopt) setVersion(s.version)
      loaded.current = true
      return true
    } catch (e) {
      if (!current()) return false
      // 读到过之后刷新失败：页头照旧显示手上的，只在首次打开时整页报错
      if (!loaded.current || (e instanceof ApiError && e.status === 404)) setError(e)
      else if (adopt) toast.error(e)
      return false
    }
  }, [id])
  useEffect(() => { void load(true) }, [load])
  /** 版本不符之后的「重新载入」：表单换成服务端现在的设置（换 key 重新挂载） */
  const reload = async () => {
    if (await load(true)) setResetKey((k) => k + 1)
  }
  useOnReconnect(() => { void load(false) })
  const live = liveFire(schedule?.last_fire)
  useEffect(() => {
    const t = setInterval(() => { if (!document.hidden) void load(false) }, live ? LIVE_REFRESH_MS : IDLE_REFRESH_MS)
    return () => clearInterval(t)
  }, [load, live])
  /**
   * 触发历史取回了第一页：最新的那一条就是页头的「上次触发」（服务端同一个口径：记录时间最新的那一次）。同一次触发
   * 照它的状态换上（点了触发历史的「刷新」，页头一起变）；不是同一次（这期间又触发过）就重读页头
   */
  const lastFireId = useRef<string | undefined>(undefined)
  lastFireId.current = schedule?.last_fire?.id
  const onNewest = useCallback((f: ScheduleFire | null) => {
    if (!f) return
    if (lastFireId.current !== f.id) {
      void load(false)
      return
    }
    setSchedule((cur) => (cur && cur.last_fire?.id === f.id ? { ...cur, last_fire: f } : cur))
  }, [load])
  useEffect(() => {
    if (tab === 'history') setHistorySeen(true)
    else setSettingsSeen(true)
  }, [tab])
  // 表单第一次挂上时版本对齐到此刻的 schedule（渲染中改状态，React 当场重渲染，表单挂上时两者已经一致）。
  // 直接打开触发历史时表单要等切过来才挂，这期间页头可能已经刷新过（立即运行之后、重连、开关冲突之后），
  // 表单显示的是刷新后的内容，还拿首次打开时的版本保存就会撞上并不存在的 409
  const [formBased, setFormBased] = useState(false)
  if (settingsSeen && schedule && !formBased) {
    setFormBased(true)
    if (version !== schedule.version) setVersion(schedule.version)
  }

  const pickTab = (k: string) => setParams((p) => {
    const next = new URLSearchParams(p)
    if (k === 'history') next.set('tab', 'history')
    else next.delete('tab')
    return next
  }, { replace: true })

  const act = async (what: 'toggle' | 'run' | 'delete', fn: (s: Schedule) => Promise<void>) => {
    if (!schedule || busy) return
    setBusy(what)
    try {
      await fn(schedule)
    } finally {
      setBusy(null)
    }
  }
  const toggle = (enabled: boolean) => act('toggle', async (s) => {
    const out = await setEnabled(s, enabled)
    if (out === 'conflict') await load(false)
    else if (out) {
      wrote()
      setSchedule(out)
      // 表单的版本只在开关前后没有别人插进来时跟着走：页头是在别人改过之后刷新出来的，表单还是旧的那份，
      // 照旧带旧版本，保存时由服务端说清
      setVersion((v) => (v === s.version ? out.version : v))
    }
  })
  const run = () => act('run', async (s) => {
    const out = await runNow(s, navigate)
    if (!out) return
    setSchedule((cur) => (cur ? { ...cur, last_fire: out.fire } : cur))
    setHistoryKey((k) => k + 1)
    void load(false)
  })
  const remove = () => act('delete', async (s) => {
    if (await removeSchedule(s)) navigate('/schedules', leavePass({ replace: true }))
  })

  const missing = error instanceof ApiError && error.status === 404 && !isUnsupported(error)
  let content
  if (missing) {
    content = (
      <div data-schedule-missing="">
        <EmptyState icon={<CalendarClock size={22} />} title={T.notFound} body={T.notFoundHint} offline={false}
                    action={<Link to="/schedules" className="btn btn-sm">{T.back}</Link>} />
      </div>
    )
  } else if (isUnsupported(error)) {
    content = <EmptyState icon={<CalendarClock size={22} />} title={T.unsupported} body={T.unsupportedHint} offline={false} />
  } else if (!schedule) {
    content = error
      ? <ErrorState error={error} onRetry={() => void load(true)} />
      : <div className="mx-auto max-w-5xl p-4"><Skeleton rows={6} height={40} gap={10} /></div>
  }

  const actions = (
    <div className="flex shrink-0 items-center gap-1">
      {schedule && (
        <>
          <EnableSwitch checked={schedule.enabled} busy={busy === 'toggle'} label={T.enableLabel(schedule.name)} onChange={(v) => void toggle(v)} />
          <button type="button" className="btn btn-sm" onClick={() => void run()} disabled={!!busy}
                  aria-label={T.runNowLabel(schedule.name)} title={T.runNowLabel(schedule.name)} data-schedule-run="">
            {busy === 'run' ? <Spinner size={11} /> : <Play size={11} aria-hidden />}
            <span className="hidden sm:inline">{T.runNow}</span>
          </button>
          <IconButton label={T.deleteLabel(schedule.name)} onClick={() => void remove()} disabled={!!busy} data-schedule-delete=""
                      className="text-faint hover:bg-st-failed/10 hover:text-[var(--err)]" icon={<Trash2 size={12} />} />
        </>
      )}
      {BACK}
    </div>
  )

  return (
    <div className="flex h-full flex-col" data-schedules-page="detail">
      <PageHeader icon={<CalendarClock size={13} />} title={schedule?.name ?? T.title} subtitle={schedule?.description_text}
                  actions={actions} />
      {content ?? (schedule && (
        <>
          <div className="flex shrink-0 flex-wrap gap-x-5 gap-y-1 border-b bg-panel px-4 py-2 text-2xs" data-schedule-summary="">
            <div className="flex min-w-0 items-center gap-1.5">
              <span className="shrink-0 text-faint">{T.nextRun}</span>
              <NextRun schedule={schedule} now={now} />
            </div>
            <div className="flex min-w-0 items-center gap-1.5">
              <span className="shrink-0 text-faint">{T.lastFire}</span>
              <LastFire fire={schedule.last_fire} tz={schedule.timezone} now={now} />
            </div>
          </div>
          <Tabs tabs={[{ key: 'settings', label: T.tabs.settings }, { key: 'history', label: T.tabs.history }]}
                active={tab} onChange={pickTab} label={T.title} idPrefix="schedule" />
          <div className="min-h-0 flex-1 overflow-y-auto">
            {settingsSeen && targetKindOf(schedule) === 'source_maintenance' && (
              <div role="tabpanel" id="schedule-panel-settings" aria-labelledby="schedule-tab-settings" hidden={tab !== 'settings'}>
                <TargetSettings schedule={schedule} />
              </div>
            )}
            {settingsSeen && targetKindOf(schedule) === 'eval_suite' && (
              <div role="tabpanel" id="schedule-panel-settings" aria-labelledby="schedule-tab-settings" hidden={tab !== 'settings'}>
                <TargetEditor
                  key={resetKey}
                  schedule={schedule}
                  version={version}
                  onSaved={(s) => {
                    wrote()
                    rememberScheduleNames([s])
                    setSchedule(s)
                    setVersion(s.version)
                  }}
                  onReload={() => void reload()}
                />
              </div>
            )}
            {settingsSeen && targetKindOf(schedule) === 'workflow' && (
              <div role="tabpanel" id="schedule-panel-settings" aria-labelledby="schedule-tab-settings" hidden={tab !== 'settings'}>
                <ScheduleEditor
                  key={resetKey}
                  schedule={schedule}
                  version={version}
                  onSaved={(s) => {
                    wrote()
                    rememberScheduleNames([s])
                    setSchedule(s)
                    setVersion(s.version)
                  }}
                  onReload={() => void reload()}
                />
              </div>
            )}
            {historySeen && (
              <div role="tabpanel" id="schedule-panel-history" aria-labelledby="schedule-tab-history" hidden={tab !== 'history'}>
                <FireHistory schedule={schedule} refreshKey={historyKey} latest={schedule.last_fire} onNewest={onNewest} />
              </div>
            )}
          </div>
        </>
      ))}
    </div>
  )
}

/**
 * 数据源维护的定时任务的设置：只读地列出名称、目标、频率、时区、补发宽限，链到数据源页（维护要做哪几步、频率和时段在数据源页的
 * 「定时维护」里改，next-round 03 B4）。页头的启用开关、立即运行、删除和触发历史照常可用
 */
function TargetSettings({ schedule }: { schedule: Schedule }) {
  const kind = targetKindOf(schedule)
  const rows: [string, string][] = [
    [T.name, schedule.name],
    [T.targetField, `${T.targetKind[kind]} · ${targetText(schedule)}`],
    [T.freq, schedule.description_text],
    [T.timezone, schedule.timezone],
    [T.graceField, graceLabel(schedule.misfire_grace_s)],
  ]
  return (
    <div className="mx-auto max-w-3xl space-y-3 p-4" data-schedule-target-settings={kind}>
      <p className="text-xs text-dim">{T.maintenanceNote}</p>
      <Link to={`/data/databases?source=${encodeURIComponent(schedule.target_id ?? '')}`} className="btn btn-sm"
            data-schedule-to-source={schedule.target_id ?? ''}>
        {T.toDataSource}
      </Link>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 rounded-lg border bg-panel p-3 text-xs">
        {rows.map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="text-faint">{label}</dt>
            <dd className="min-w-0 break-words text-fg">{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  )
}
