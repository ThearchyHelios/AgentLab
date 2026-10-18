import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { CalendarClock, Play, Plus, Trash2 } from 'lucide-react'
import { api } from '../../api/client'
import { EmptyState, ErrorState, IconButton, PageHeader, Skeleton, Spinner, toast, useTicker } from '../../components/ui'
import { useOnReconnect } from '../../store/catalog'
import { SCHEDULE_TEXT as T } from '../../lib/terms'
import type { Schedule } from '../../types'
import { ClassChip } from '../runs/parts'
import { ScheduleCreatePage, ScheduleDetailPage } from './ScheduleDetail'
import {
  EnableSwitch, EventChip, LastFire, NextRun, isUnsupported, rememberScheduleNames, removeSchedule, runNow, setEnabled,
  targetKindOf, targetText,
} from './parts'

/**
 * 定时任务：按设定的周期自动发起工作流运行。
 *
 * /schedules 是列表；/schedules/new 新建；/schedules/:id 是一个定时任务的详情（设置、触发历史两个标签，
 * ?tab=history 直接打开触发历史）。路由表在 App 里只有带 id、不带 id 两条，「new」在这里认。
 */
export function SchedulesPage() {
  const { id } = useParams()
  if (!id) return <ScheduleListPage />
  if (id === 'new') return <ScheduleCreatePage />
  return <ScheduleDetailPage key={id} id={id} />
}

/** 列表每 30 秒原地刷新一次（页面不可见时不刷）：到点触发之后，下次运行和上次触发要跟着变 */
const LIST_REFRESH_MS = 30_000

function ScheduleListPage() {
  const navigate = useNavigate()
  const [rows, setRows] = useState<Schedule[] | null>(null)
  const [error, setError] = useState<unknown>(null)
  /** 每行正在做的事：开关、立即运行、删除各自禁用，不让同一行连点出两个请求 */
  const [busy, setBusy] = useState<Record<string, 'toggle' | 'run' | 'delete'>>({})
  const now = useTicker(30_000)
  /**
   * 读列表的序号：后发的读胜过先发的读；写操作成功时也加一，作废还在路上的读。那次读可能在写之前就读到了旧值、
   * 却在写的答复之后才回来，落地会把刚换上的那一行（开关的结果、删掉的行）整行盖回去
   */
  const seq = useRef(0)
  const hasRows = useRef(false)
  const wrote = () => { seq.current++ }

  const load = useCallback(async (opts?: { silent?: boolean }) => {
    const n = ++seq.current
    try {
      const r = await api.schedules.list()
      if (n !== seq.current) return
      const items = r.items ?? []
      rememberScheduleNames(items)
      hasRows.current = true
      setRows(items)
      setError(null)
    } catch (e) {
      if (n !== seq.current) return
      // 读到过列表之后失败不把整页换成错误，手上的照旧是真数据：定时刷新不吭声，别的（重新载入）弹提示
      if (!hasRows.current) setError(e)
      else if (!opts?.silent) toast.error(e)
    }
  }, [])
  useEffect(() => { void load() }, [load])
  useOnReconnect(() => { void load() })
  useEffect(() => {
    const t = setInterval(() => { if (!document.hidden) void load({ silent: true }) }, LIST_REFRESH_MS)
    return () => clearInterval(t)
  }, [load])

  const replace = (next: Schedule) => setRows((rs) => rs && rs.map((r) => (r.id === next.id ? next : r)))
  const act = async (s: Schedule, what: 'toggle' | 'run' | 'delete', fn: () => Promise<void>) => {
    if (busy[s.id]) return
    setBusy((b) => ({ ...b, [s.id]: what }))
    try {
      await fn()
    } finally {
      setBusy((b) => {
        const { [s.id]: _, ...rest } = b
        return rest
      })
    }
  }
  const toggle = (s: Schedule, enabled: boolean) => act(s, 'toggle', async () => {
    const out = await setEnabled(s, enabled)
    if (out === 'conflict') await load()
    else if (out) {
      wrote()
      replace(out)
    }
  })
  const run = (s: Schedule) => act(s, 'run', async () => {
    const out = await runNow(s, navigate)
    if (!out) return
    wrote()
    // 只换上次触发：这一行在请求途中可能被开关改过，不拿点下去那一刻的旧值整行盖回去
    setRows((rs) => rs && rs.map((r) => (r.id === s.id ? { ...r, last_fire: out.fire } : r)))
    // 下次运行、运行状态以服务端为准，再静默取一遍
    void load({ silent: true })
  })
  const remove = (s: Schedule) => act(s, 'delete', async () => {
    if (!(await removeSchedule(s))) return
    wrote()
    setRows((rs) => rs && rs.filter((r) => r.id !== s.id))
  })

  const unsupported = isUnsupported(error)
  const createButton = (
    <Link to="/schedules/new" className="btn btn-primary btn-sm" data-schedule-create="">
      <Plus size={12} aria-hidden /> {T.create}
    </Link>
  )

  let body
  if (unsupported) {
    body = (
      <div data-schedules-unsupported="">
        <EmptyState icon={<CalendarClock size={22} />} title={T.unsupported} body={T.unsupportedHint} offline={false} />
      </div>
    )
  } else if (rows === null) {
    body = error ? <ErrorState error={error} onRetry={() => void load()} /> : <Skeleton rows={3} height={64} gap={8} />
  } else if (!rows.length) {
    body = (
      <div data-schedules-empty="">
        <EmptyState icon={<CalendarClock size={22} />} title={T.emptyTitle} body={T.emptyBody} action={createButton} />
      </div>
    )
  } else {
    body = (
      <ul className="space-y-2" aria-label={T.listLabel} data-schedule-list="">
        {rows.map((s) => (
          <ScheduleRow key={s.id} s={s} now={now} busy={busy[s.id]}
                       onToggle={(v) => void toggle(s, v)} onRun={() => void run(s)} onDelete={() => void remove(s)} />
        ))}
      </ul>
    )
  }

  return (
    <div className="flex h-full flex-col" data-schedules-page="list">
      <PageHeader icon={<CalendarClock size={13} />} title={T.title} subtitle={T.hint} actions={unsupported ? null : createButton} />
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-4xl p-4">{body}</div>
      </div>
    </div>
  )
}

/**
 * 一行：名字（链到详情）、目标和频率；下次运行、上次触发；右边启用开关、立即运行、编辑、删除。
 * 窄屏上右边的按钮整组折到下一行，不挤名字。目标不是工作流的（评测题集、数据源维护）标出种类、不标运行类别；数据源维护的
 * 「编辑」写「查看」：它的设置在数据源页改，这里只读
 */
function ScheduleRow({ s, now, busy, onToggle, onRun, onDelete }: {
  s: Schedule; now: number; busy?: 'toggle' | 'run' | 'delete'
  onToggle: (enabled: boolean) => void; onRun: () => void; onDelete: () => void
}) {
  const kind = targetKindOf(s)
  const workflow = kind === 'workflow'
  /** 在这一页改得了设置的：工作流、评测题集；数据源维护在数据源页改 */
  const editable = kind !== 'source_maintenance'
  const target = targetText(s)
  return (
    <li className="rounded-lg border bg-panel px-3 py-2.5" data-schedule-row={s.id} data-enabled={String(s.enabled)}>
      <div className="flex flex-wrap items-start gap-x-3 gap-y-2">
        <div className="min-w-0 flex-1 basis-56">
          <div className="flex min-w-0 flex-wrap items-center gap-1.5">
            <Link to={`/schedules/${encodeURIComponent(s.id)}`} data-schedule-name=""
                  className="min-w-0 truncate text-sm font-medium text-fg hover:underline">
              {s.name}
            </Link>
            {workflow
              ? <ClassChip runClass={s.run_class} />
              : <span className="chip shrink-0" data-schedule-target-kind={kind}>{T.targetKind[kind]}</span>}
            {s.trigger_kind === 'event' && <EventChip />}
            {!s.enabled && <span className="chip shrink-0" data-schedule-paused="">{T.pausedChip}</span>}
          </div>
          <div className="mt-0.5 truncate text-xs text-dim" title={`${target} · ${s.description_text}`} data-schedule-desc="">
            {target}<span className="text-faint"> · </span>{s.description_text}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <EnableSwitch checked={s.enabled} busy={busy === 'toggle'} label={T.enableLabel(s.name)} onChange={onToggle} />
          <button type="button" className="btn btn-sm" onClick={onRun} disabled={!!busy} aria-label={T.runNowLabel(s.name)}
                  title={T.runNowLabel(s.name)} data-schedule-run="">
            {busy === 'run' ? <Spinner size={11} /> : <Play size={11} aria-hidden />} {T.runNow}
          </button>
          <Link to={`/schedules/${encodeURIComponent(s.id)}`} className="btn btn-sm btn-ghost"
                aria-label={editable ? T.editLabel(s.name) : T.viewLabel(s.name)} data-schedule-edit="">
            {editable ? T.edit : T.view}
          </Link>
          <IconButton label={T.deleteLabel(s.name)} onClick={onDelete} disabled={!!busy} data-schedule-delete=""
                      className="text-faint hover:bg-st-failed/10 hover:text-[var(--err)]" icon={<Trash2 size={12} />} />
        </div>
      </div>
      <div className="mt-1.5 flex flex-wrap gap-x-5 gap-y-1 text-2xs">
        <div className="flex min-w-0 items-center gap-1.5" data-schedule-next="">
          <span className="shrink-0 text-faint">{T.nextRun}</span>
          <NextRun schedule={s} now={now} />
        </div>
        <div className="flex min-w-0 items-center gap-1.5" data-schedule-last="">
          <span className="shrink-0 text-faint">{T.lastFire}</span>
          <LastFire fire={s.last_fire} tz={s.timezone} now={now} />
        </div>
      </div>
    </li>
  )
}
