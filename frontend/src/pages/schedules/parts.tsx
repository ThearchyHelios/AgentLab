import { useEffect, useSyncExternalStore, type MouseEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { CalendarClock } from 'lucide-react'
import clsx from 'clsx'
import { ApiError, api } from '../../api/client'
import { StatusPill, confirmDialog, toast } from '../../components/ui'
import { formatDateTime, parseServerTime } from '../../lib/format'
import { SCHEDULE_TEXT as T } from '../../lib/terms'
import type { Schedule, ScheduleFire, ScheduleFireStatus, ScheduleRunResult } from '../../types'
import { browserTimezone, formatUntil, formatZoned } from './model'

// -------------------------------------------------------------------------
// 启用开关
// -------------------------------------------------------------------------

/**
 * 启用 / 暂停的开关（role=switch，空格、回车切换）。点了就 PATCH、立刻生效，不等表单保存：
 * 暂停是「别再到点发起了」，拖到保存时才生效就晚了。可见的字固定写「启用」，开没开看开关本身和 aria-checked
 */
export function EnableSwitch({ checked, busy, label, onChange }: {
  checked: boolean; busy?: boolean; label: string; onChange: (next: boolean) => void
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      title={label}
      disabled={busy}
      aria-busy={busy || undefined}
      onClick={() => onChange(!checked)}
      data-schedule-toggle=""
      className="inline-flex shrink-0 items-center gap-1.5 rounded-md px-1 py-0.5 text-2xs text-dim outline-none transition-colors hover:text-fg focus-visible:ring-2 focus-visible:ring-[var(--accent)] disabled:cursor-wait disabled:opacity-60"
    >
      <span
        aria-hidden
        data-switch-track=""
        className="relative inline-block h-4 w-7 shrink-0 rounded-full transition-colors"
        style={{ background: checked ? 'var(--accent-solid)' : 'var(--border-strong)' }}
      >
        <span
          className="absolute top-0.5 h-3 w-3 rounded-full shadow-elev-1 transition-[left]"
          style={{ left: checked ? 14 : 2, background: 'var(--on-accent)' }}
        />
      </span>
      <span>{T.enable}</span>
    </button>
  )
}

// -------------------------------------------------------------------------
// 时间
// -------------------------------------------------------------------------

/**
 * 按定时任务的时区写一个时刻；relative 时前面加「3 小时后」。悬停写完整时间和时区，本机时区不同时另给本机时间：
 * 只写一个钟点，人在别的时区打开会把它当本地时间
 */
export function ZonedTime({ date, tz, now, relative = false, className }: {
  date: Date; tz: string; now: number; relative?: boolean; className?: string
}) {
  const abs = formatZoned(date, tz, 'short', new Date(now))
  const title = [T.zoneTitle(formatZoned(date, tz, 'full'), tz),
    tz !== browserTimezone() ? T.localTitle(formatDateTime(date)) : null].filter(Boolean).join('\n')
  const rel = relative ? formatUntil(date, now) ?? T.nextOverdue : null
  return (
    <time dateTime={date.toISOString()} title={title} className={clsx('tnum', className)}>
      {rel ? <>{rel}<span className="text-faint"> · {abs}</span></> : abs}
    </time>
  )
}

/** 下次运行：暂停中、往后不再触发都照实说，不写「—」让人猜 */
export function NextRun({ schedule, now }: { schedule: Schedule; now: number }) {
  if (!schedule.enabled) return <span className="text-faint">{T.nextPaused}</span>
  const d = parseServerTime(schedule.next_run_at)
  if (!d) return <span className="text-faint">{T.nextNone}</span>
  return <ZonedTime date={d} tz={schedule.timezone} now={now} relative className="text-dim" />
}

// -------------------------------------------------------------------------
// 触发
// -------------------------------------------------------------------------

/** 触发结局的颜色：发起了是安静的完成色，跳过是琥珀，发起失败是红 */
const FIRE_TONE: Record<ScheduleFireStatus, string> = {
  started: 'var(--st-done)',
  skipped_overlap: 'var(--st-waiting)',
  skipped_misfire: 'var(--st-waiting)',
  failed_to_start: 'var(--st-failed)',
}

/** 触发结局的小标签。跳过、发起失败铺淡底，悬停写原因（没有原因写这种结局的一般说明） */
export function FireStatusChip({ fire }: { fire: ScheduleFire }) {
  const color = FIRE_TONE[fire.status] ?? 'var(--text-dim)'
  return (
    <span
      className="chip shrink-0"
      title={fire.reason || (fire.eval_run_id && fire.status === 'started' ? T.evalStarted : T.fireStatusHint[fire.status])
        || undefined}
      data-fire-status-chip={fire.status}
      style={{
        color,
        borderColor: `color-mix(in srgb, ${color} 40%, transparent)`,
        background: fire.status === 'started' ? undefined : `color-mix(in srgb, ${color} 10%, transparent)`,
      }}
    >
      {T.fireStatus[fire.status] ?? fire.status}
    </span>
  )
}

/**
 * 触发类型：定时 / 手动 / 补发。服务端把错过的触发都记成 kind=misfire：宽限内补发的那一次照常有结局，没有补发的
 * 是 skipped_misfire。后者没有「补发」这回事，按「定时」写，结局由旁边的「错过已跳过」说。没有补发有两种原因（超出
 * 补发宽限；宽限内恢复时积压的那几次不补），悬停不替服务端认定是哪一种，否则会和本条记的原因自相矛盾
 */
export function FireKindChip({ fire }: { fire: ScheduleFire }) {
  const missed = fire.kind === 'misfire' && fire.status === 'skipped_misfire'
  const kind = missed ? 'scheduled' : fire.kind
  return (
    <span className="chip shrink-0" title={missed ? T.fireKindMissedHint(!!fire.reason) : T.fireKindHint[kind]} data-fire-kind-chip={fire.kind}>
      {T.fireKind[kind] ?? kind}
    </span>
  )
}

/** 列表里的「上次触发」：发起了运行的链到那次运行、写运行现在的状态；跳过、发起失败的写结局 */
export function LastFire({ fire, tz, now }: { fire: ScheduleFire | null; tz: string; now: number }) {
  if (!fire) return <span className="text-faint">{T.neverFired}</span>
  const d = parseServerTime(fire.fire_at)
  const when = d ? <ZonedTime date={d} tz={tz} now={now} className="text-faint" /> : null
  if (fire.run_id) {
    return (
      <Link to={`/runs/${encodeURIComponent(fire.run_id)}`} className="inline-flex min-w-0 items-center gap-1.5 hover:underline">
        {fire.run_status ? <StatusPill status={fire.run_status} plain /> : <FireStatusChip fire={fire} />}
        {when}
      </Link>
    )
  }
  return (
    <span className="inline-flex min-w-0 flex-wrap items-center gap-1.5">
      <FireStatusChip fire={fire} />
      {when}
    </span>
  )
}

// -------------------------------------------------------------------------
// 列表行和详情页头共用的操作
// -------------------------------------------------------------------------

/**
 * 启用 / 暂停：带读到的版本 PATCH，成功交回新的定时任务。版本不符（409）时说清是别处改的、返回 'conflict'，
 * 由调用方重新载入；别的错误弹提示、返回 null。409 的原话是写给表单保存的（「请刷新后再保存」），开关不照搬，
 * 放进详情
 */
export async function setEnabled(s: Schedule, enabled: boolean): Promise<Schedule | 'conflict' | null> {
  try {
    const next = await api.schedules.update(s.id, { enabled, version: s.version })
    toast.ok(enabled ? T.enabledOk(s.name) : T.pausedOk(s.name))
    return next
  } catch (e) {
    if (e instanceof ApiError && e.status === 409) {
      toast.warn(T.toggleConflict(s.name), e.message ? { detail: e.message } : undefined)
      return 'conflict'
    }
    toast.error(e)
    return null
  }
}

/**
 * 立即运行：发起了就给「查看运行」直达，跳过、被拒照实说原因。返回这次触发（出错时 null），
 * 调用方拿它更新「上次触发」和触发历史
 */
export async function runNow(s: Schedule, navigate: (to: string) => void): Promise<ScheduleRunResult | null> {
  try {
    const r = await api.schedules.run(s.id)
    const runId = r.run_id ?? r.fire?.run_id ?? null
    if (runId) {
      toast.ok(T.runStarted, { action: { label: T.openRun, onClick: () => navigate(`/runs/${encodeURIComponent(runId)}`) } })
    } else if (r.fire?.status === 'started' && r.fire.eval_run_id) {
      // 目标是评测题集：发起的是一次评测，没有一次运行可看
      toast.ok(T.evalStarted)
    } else if (r.fire?.status === 'skipped_overlap' || r.fire?.status === 'skipped_misfire') {
      toast.warn(r.fire.status === 'skipped_overlap' ? T.runSkipped : T.fireStatus.skipped_misfire,
        r.fire.reason ? { detail: r.fire.reason } : undefined)
    } else {
      toast.error(T.runFailed(r.fire?.reason ?? null))
    }
    return r
  } catch (e) {
    toast.error(e)
    return null
  }
}

/** 删除：先用站内对话框确认、写清后果。删掉了返回 true */
export async function removeSchedule(s: Schedule): Promise<boolean> {
  const ok = await confirmDialog({
    title: T.deleteTitle(s.name), consequences: T.deleteConsequences, confirmLabel: T.deleteConfirm, danger: true,
  })
  if (!ok) return false
  try {
    await api.schedules.remove(s.id)
    rememberScheduleNames([], [s.id])
    toast.ok(T.deleted(s.name))
    return true
  } catch (e) {
    toast.error(e)
    return false
  }
}

/** 定时任务的目标种类：老服务端不给的都是工作流 */
export const targetKindOf = (s: Schedule) => s.target_kind ?? 'workflow'

/** 目标的名字（不在了标出来）：工作流用 workflow_name，别的种类用 target_name */
export function targetText(s: Schedule): string {
  const name = (targetKindOf(s) === 'workflow' ? s.workflow_name : s.target_name ?? s.workflow_name) || s.target_id || ''
  return s.target_exists === false ? T.targetGone(name) : name
}

/** 老服务端没有定时任务接口：404 且没有机读码（有码的 404 是服务端自己说的「不存在」） */
export const isUnsupported = (e: unknown) => e instanceof ApiError && e.status === 404 && !e.code
  && /^not found$/i.test(String(e.detail ?? ''))

// -------------------------------------------------------------------------
// 记录页的「定时」标记
// -------------------------------------------------------------------------

/**
 * 定时任务的名字，给记录页的「定时」标记写悬停提示。模块里记一份：定时任务页读过列表就顺手记下，记录页遇到
 * 不认识的 id 再取一次列表（一分钟内最多一次）。取不到（老服务端、暂时连不上）不要紧，标记照样显示，只是不写名字
 */
const names = new Map<string, string>()
/**
 * 已删除的定时任务：取回的列表里没有的（列表是全部定时任务，服务端不分页），或者刚在本页删掉的。运行详情页头据此不再
 * 链过去。列表没取到时谁都不算删除：只是不知道
 */
const missing = new Set<string>()
let fetchedAt = 0
let inflight: Promise<void> | null = null
let namesVersion = 0
const nameListeners = new Set<() => void>()
const NAMES_RETRY_MS = 60_000

const emitNames = () => {
  namesVersion++
  nameListeners.forEach((l) => l())
}

/** 记下读到的定时任务名字（gone 里的是刚删掉的） */
export function rememberScheduleNames(items: Pick<Schedule, 'id' | 'name'>[], gone: string[] = []) {
  for (const s of items) {
    names.set(s.id, s.name)
    missing.delete(s.id)
  }
  for (const id of gone) {
    names.delete(id)
    missing.add(id)
  }
  emitNames()
}

function ensureNames(ids: string[]) {
  if (inflight || !ids.some((id) => !names.has(id) && !missing.has(id)) || Date.now() - fetchedAt < NAMES_RETRY_MS) return
  inflight = api.schedules.list({ timeoutMs: 10_000 })
    .then((r) => {
      const items = r.items ?? []
      for (const s of items) {
        names.set(s.id, s.name)
        missing.delete(s.id)
      }
      const listed = new Set(items.map((s) => s.id))
      for (const id of ids) if (!listed.has(id)) missing.add(id)
    })
    .catch(() => { /* 取不到就不写名字，也不算删除 */ })
    .finally(() => {
      inflight = null
      fetchedAt = Date.now()
      emitNames()
    })
}

/** 这个定时任务已删除（取回的列表里没有它、或者刚在本页删掉）。和 useScheduleNames 一起用：取回来后会重渲染 */
export const scheduleGone = (id: string) => missing.has(id)

/** 这些定时任务的名字（id → 名字）。没记过的去取一次，取回来后重渲染 */
export function useScheduleNames(ids: (string | null | undefined)[]): ReadonlyMap<string, string> {
  useSyncExternalStore(
    (l) => { nameListeners.add(l); return () => { nameListeners.delete(l) } },
    () => namesVersion,
  )
  const key = [...new Set(ids.filter((x): x is string => !!x))].sort().join(',')
  useEffect(() => { if (key) ensureNames(key.split(',')) }, [key])
  return names
}

/**
 * 运行名字旁的「定时」标记：悬停写是哪个定时任务，点一下去那个定时任务。记录页的整行是打开运行的按钮，
 * 这里拦住冒泡，只去定时任务、不打开运行。
 *
 * 按钮里放不了可聚焦的链接（读屏会把按钮里的内容压平，Tab 也到不了），所以键盘到不了这个标记：读屏改念
 * 「由定时任务「X」发起」，至少在整行的名字里听到来源。键盘可达的入口在运行详情的页头（RunScheduleSource）：
 * 打开这次运行之后，Tab 就能到那条链接
 */
export function ScheduleTag({ scheduleId, name }: { scheduleId: string; name?: string | null }) {
  const navigate = useNavigate()
  const go = (e: MouseEvent) => {
    e.preventDefault()
    e.stopPropagation()
    navigate(`/schedules/${encodeURIComponent(scheduleId)}`)
  }
  return (
    <span
      className="chip shrink-0 cursor-pointer hover:border-[var(--border-strong)] hover:text-fg"
      title={T.runTagTitle(name)}
      data-run-schedule={scheduleId}
      onClick={go}
    >
      <CalendarClock size={10} aria-hidden /> <span aria-hidden>{T.runTag}</span>
      <span className="sr-only">{T.runTagSr(name)}</span>
    </span>
  )
}

// -------------------------------------------------------------------------
// 运行详情页头的来源
// -------------------------------------------------------------------------

/**
 * 运行详情页头里「谁发起的」那一段，定时任务发起的运行用它：「由定时任务「X」发起」是真的链接，Tab 到得了、回车能
 * 打开，去那个定时任务的设置和触发历史。记录页的「定时」标记在整行按钮里，只能用鼠标点，键盘的入口在这里。
 *
 * 到点发起的运行（以及没有署名的立即运行）记在「定时任务：X」名下：原来那句「定时任务：X 发起」整句换成这条链接，
 * 不写两遍。有人署名点「立即运行」的，署名照写，后面接「来自定时任务「X」」。名字取定时任务现在的名字（改过名的写
 * 新名字），取不到时用署名里记的那个，再没有就不写名字。
 *
 * 前面带和「定时」标记一样的日历图标：页头这一行都是灰字，不带记号看不出这一段能点。页头窄时这一行换行，链接至少留
 * 几个字宽（以前这一行不换行，只有链接能缩，窄窗口里被挤成 0 宽，Tab 停在一个看不见的链接上），再长截断，全名在悬停里。
 * 定时任务已经删除的写成纯文本（链过去只有「不存在」页），同一行的「工作流已删除」也是这样
 */
export function RunScheduleSource({ scheduleId, startedBy }: { scheduleId: string; startedBy?: string | null }) {
  const names = useScheduleNames([scheduleId])
  const recorded = startedBy?.startsWith(T.actorPrefix) ? startedBy.slice(T.actorPrefix.length).trim() : null
  const auto = !startedBy || recorded != null
  const name = names.get(scheduleId) || recorded || null
  const body = (text: string) => (
    <>
      <CalendarClock size={10} aria-hidden className="shrink-0" />
      <span className="min-w-0 truncate">{text}</span>
    </>
  )
  const source = scheduleGone(scheduleId) ? (
    <span title={T.runSourceGoneTitle} data-run-schedule-gone={scheduleId}
          className="inline-flex min-w-[7em] max-w-full items-center gap-1">
      {body(auto ? T.runSourceGone(name) : T.runSourceManualGone(name))}
    </span>
  ) : (
    <Link
      to={`/schedules/${encodeURIComponent(scheduleId)}`}
      title={T.runSourceTitle(name)}
      data-run-schedule-link={scheduleId}
      className="inline-flex min-w-[7em] max-w-full items-center gap-1 rounded-sm text-dim outline-none hover:text-fg hover:underline focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
    >
      {body(auto ? T.runSource(name) : T.runSourceManual(name))}
    </Link>
  )
  if (auto || !startedBy) return source
  return (
    <>
      <span className="shrink-0">{T.runStarter(startedBy)}</span>
      <span>·</span>
      {source}
    </>
  )
}
