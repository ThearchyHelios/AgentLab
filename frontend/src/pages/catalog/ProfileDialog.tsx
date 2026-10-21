import { useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import {
  ArrowDown, ArrowUp, ArrowUpRight, ChevronRight, CircleCheck, CircleDashed, CircleMinus, CircleSlash, CircleX, Equal, Info, Minus,
  RotateCcw, ScanSearch, ShieldAlert, TriangleAlert, UserCheck,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import clsx from 'clsx'
import { ApiError, api } from '../../api/client'
import type {
  CatalogProfileCheck, CatalogProfileCodesFinding, CatalogProfileDateFinding, CatalogProfileJob, CatalogProfileQuery,
  CatalogProfileRelationFinding, CatalogProfileSettings, CatalogProfileSkip, CatalogProfileTaskDetail, CatalogProfileTaskRow,
  CatalogProfileTaskStatus, CatalogTableRow,
} from '../../types'
import { Modal, Notice, Spinner, useRadioGroup, useTicker } from '../../components/ui'
import { errorMessage } from '../../lib/errors'
import { formatDateTime, formatNumber, formatTime } from '../../lib/format'
import {
  CATALOG_CARDINALITY_LABEL, CATALOG_TEXT as CT, CATALOG_UI_TEXT as UT, PROFILE_SKIP_KIND_LABEL, PROFILE_SKIP_REASON_LABEL, PROFILE_TEXT as PT,
} from '../../lib/terms'
import { STATUS_TONE, StatusChip } from './parts'
import {
  PROFILE_MAX_TABLES, PROFILE_OBSERVE_EVERY_H, PROFILE_RETRYABLE, UNFINISHED_REASONS, coverageText, fillKey, isProfilable, profileBlockOf,
  profileScopeTables, secondsText, sizeText, spanText,
} from './profile'
import type { ProfileBlock } from './profile'
import { isJobActive } from './draftJob'
import { isProfileBusy, retryableTasks } from './profileJob'
import type { ProfileStartBody } from './profileJob'

// ===========================================================================
// 数据目录页的「数据剖析」。剖析是服务端的后台任务（profileJob.ts）：这里选范围、看预算、建任务；之后显示页面跟着的
// 那个任务（进行中的逐表进度、结束后的结果），任务本身的状态在页面上，关掉对话框照常进行。照起草对话框的结构：
//
// 开始前：范围（已选 / 当前筛选 / 需要剖析的表（默认）/ 全部表 / 取值观察，个数按清单上每行的 profile_due、profile_checks、
// 有没有数据目录算；取值观察只观察不剖析，最近观察过的服务端这次不观察，张数是上限）、按表写的预算和这次最多约几条查询、
// 醒目一句「只读查询、一条一条发（同时剖析几张表时写明张数）、可随时停止」；同一个数据源有起草任务时按钮写「排队剖析」。
// 进行中：几张 / 共几张、已发几条查询、用时，按表数的真进度条，停止；逐表一行（状态、各类检查的进度、查询数、用时、正在做
// 哪一项），点一行展开这张表的检查清单、发现和发过的查询。
// 结束后：汇总（各状态几张、核实了什么）、止损和停止的原因照实写、按「有发现 / 有跳过 / 失败」筛、重试没做完的表；码值的
// 「填写含义」照旧（填完回到这里，接着下一列）。
// 开始不了（409）：每种情况一句说法和下一步（未开启就地打开剖析设置；正忙写清是谁的、到哪了，可以去看它的进度；取值观察
// 挑不出表时说「没有要观察的表」，不给「改选全部表」）。
// ===========================================================================

type Scope = 'selected' | 'filtered' | 'needed' | 'all' | 'observe'
type Filter = 'all' | 'findings' | 'skipped' | 'failed'
const FILTERS: readonly Filter[] = ['all', 'findings', 'skipped', 'failed']

/** 结果里每类最多逐条列几项，其余折起来 */
const SKIP_SHOWN = 6
const CODES_SHOWN = 12

export function ProfileDialog({
  sourceId, sourceName, settings, rows, visible, selected, filtered, job, initial, initialScope = null, draftActive, stopping, filled,
  returnTo = null, onStart, onCancel, onRetry, onShowActive, onTrack, onClose, onSettings, onGoSource, onOpenTable, onFillCodes,
}: {
  sourceId: string
  sourceName: string
  /** 这个数据源此刻的剖析设置（数据源列表里读的） */
  settings: CatalogProfileSettings
  /** 全部表（服务端的顺序） */
  rows: CatalogTableRow[]
  /** 当前筛选结果 */
  visible: CatalogTableRow[]
  selected: string[]
  /** 清单上有没有生效的搜索或筛选：有时才给「当前筛选结果」这一项 */
  filtered: boolean
  /** 页面跟着的剖析任务（进行中的、刚结束的） */
  job: CatalogProfileJob | null
  /** 打开时显示什么：setup 选范围、开始；job 显示页面跟着的那个任务 */
  initial: 'setup' | 'job'
  /** 从表清单的「剖析所选」打开：范围默认是已选的表 */
  initialScope?: 'selected' | null
  /** 同一个数据源有起草任务在排队、进行中：剖析会排在它后面 */
  draftActive: boolean
  /** 点了停止、服务端还没停下来 */
  stopping: boolean
  /** 从结果去填了含义的码值列（fillKey）→ 保存后还剩几个含义待填写 */
  filled: Record<string, number>
  /** 填完一列码值的含义、回到结果：展开那张表，焦点落在下一列还有待填写的「填写含义」上 */
  returnTo?: { table: string; column: string } | null
  onStart: (body: ProfileStartBody) => Promise<CatalogProfileJob>
  onCancel: () => Promise<void>
  onRetry: (from: CatalogProfileJob) => Promise<CatalogProfileJob>
  /** 正忙（409）时改去跟着进行中的那一个 */
  onShowActive: () => Promise<CatalogProfileJob | null>
  /** 设置阶段点开了上一次剖析的结果（或者发现别处建的进行中的任务）：页面改跟这个任务 */
  onTrack: (job: CatalogProfileJob) => void
  onClose: () => void
  /** 打开这个数据源的剖析设置 */
  onSettings: () => void
  /** 去数据源卡片（停用、没有表结构、快照被改动时） */
  onGoSource: () => void
  onOpenTable: (table: string) => void
  onFillCodes: (table: string, column: string) => void
}) {
  const scopes = useMemo(() => {
    const out: { key: Scope; label: string; hint?: string; tables: string[]; leftOut: number; body: ProfileStartBody }[] = []
    const byName = new Map(rows.map((r) => [r.table_name, r]))
    // 已选、筛选结果里剖析不了的表（视图、表结构里没有、没有可剖析的项）不交给服务端，写明有几张
    const explicit = (names: string[]) => {
      const tables = names.filter((n) => { const r = byName.get(n); return !!r && isProfilable(r) })
      return { tables, leftOut: names.length - tables.length }
    }
    if (selected.length) {
      const s = explicit(selected)
      out.push({ key: 'selected', label: PT.scopeSelected(s.tables.length), ...s, body: { tables: s.tables } })
    }
    if (filtered && visible.length && visible.length !== rows.length) {
      const f = explicit(visible.map((r) => r.table_name))
      out.push({ key: 'filtered', label: PT.scopeFiltered(f.tables.length), ...f, body: { tables: f.tables } })
    }
    const needed = profileScopeTables(rows, 'needed')
    out.push({ key: 'needed', label: PT.scopeNeeded(needed.length), hint: needed.length ? PT.scopeNeededHint : PT.scopeNeededNone,
      tables: needed, leftOut: 0, body: { scope: 'needed' } })
    const all = profileScopeTables(rows, 'all')
    out.push({ key: 'all', label: PT.scopeAll(all.length), hint: PT.scopeAllHint, tables: all, leftOut: 0, body: { scope: 'all' } })
    const watched = profileScopeTables(rows, 'observe')
    out.push({ key: 'observe', label: PT.scopeObserve(watched.length),
      hint: watched.length ? PT.scopeObserveHint(PROFILE_OBSERVE_EVERY_H) : PT.scopeObserveNone, tables: watched, leftOut: 0,
      body: { scope: 'observe' } })
    return out
  }, [rows, visible, selected, filtered])
  const [scope, setScope] = useState<Scope>(() => {
    if (initialScope === 'selected' && scopes.some((s) => s.key === 'selected')) return 'selected'
    // 默认「需要剖析的表」；一张都没有时选「全部表」（选一个开始不了的范围没有意义）
    return scopes.find((s) => s.key === 'needed')!.tables.length ? 'needed' : 'all'
  })
  const picked = scopes.find((s) => s.key === scope) ?? scopes[0]
  const tooMany = !!picked.body.tables && picked.tables.length > PROFILE_MAX_TABLES

  const [mode, setMode] = useState<'setup' | 'job' | 'blocked'>(initial === 'job' && job ? 'job' : 'setup')
  /** 开始不了（409）：哪种情况、服务端原话、正忙时进行中的那一个、当时的范围和剖析设置（就地开启后据此回到选范围） */
  const [blocked, setBlocked] = useState<{
    kind: ProfileBlock; message: string; active: CatalogProfileJob | null; body: ProfileStartBody; settings: CatalogProfileSettings
  } | null>(null)
  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState<string | null>(null)
  /** 是 409（正忙）转过来显示的那一个 */
  const [busy, setBusy] = useState(false)
  const [acting, setActing] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  /** 上一次剖析（没做完的才提），设置阶段写一行、可以点开 */
  const [last, setLast] = useState<CatalogProfileJob | null>(null)
  const onTrackRef = useRef(onTrack)
  onTrackRef.current = onTrack

  const inSetup = mode === 'setup' || (mode === 'job' && !job)
  // 设置阶段：取上一次剖析。别处（另一个标签页、表详情）建的任务还在进行：直接显示它
  useEffect(() => {
    if (!inSetup) return
    let alive = true
    api.dataCatalog.profileJobs.latest(sourceId).then(({ job: latest }) => {
      if (!alive || !latest) return
      if (isJobActive(latest)) {
        onTrackRef.current(latest)
        setBusy(true)
        setMode('job')
      } else if (latest.status !== 'succeeded') {
        setLast(latest)
      }
    }, () => {})
    return () => { alive = false }
  }, [inSetup, sourceId])

  /** 开始不了：409 各有说法；正忙时顺手取进行中的那一个（写清是谁的、到哪了）；别的错误留在设置里写明 */
  const fail = async (e: unknown, body: ProfileStartBody) => {
    if (e instanceof ApiError && e.status === 409) {
      const kind = profileBlockOf(e)
      const active = isProfileBusy(e) ? await api.dataCatalog.profileJobs.active(sourceId).then((r) => r.job, () => null) : null
      setBlocked({ kind, message: e.message, active, body, settings })
      setMode('blocked')
    } else {
      setStartError(PT.startFailed(errorMessage(e)))
    }
  }

  const begin = async (body: ProfileStartBody = picked.body) => {
    if (starting) return
    setStarting(true)
    setStartError(null)
    try {
      await onStart(body)
      setBusy(false)
      setBlocked(null)
      setMode('job')
    } catch (e) {
      await fail(e, body)
    } finally {
      setStarting(false)
    }
  }

  const act = async (fn: () => Promise<void>) => {
    setActing(true)
    setActionError(null)
    try {
      await fn()
    } catch (e) {
      setActionError(errorMessage(e))
    } finally {
      setActing(false)
    }
  }
  const stop = () => act(onCancel)
  // 重试是新建一个任务：正忙（别处又建了一个）时和开始一样转到「开始不了」，写清是谁的
  const retry = (from: CatalogProfileJob) => act(async () => {
    try {
      await onRetry(from)
      setBusy(false)
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) await fail(e, { tables: retryableTasks(from).map((t) => t.table_name) })
      else throw e
    }
  })
  const viewActive = async () => {
    const current = await onShowActive().catch(() => null)
    if (current) {
      setBusy(true)
      setBlocked(null)
      setMode('job')
    }
  }
  const backToSetup = () => {
    setBlocked(null)
    setStartError(null)
    setActionError(null)
    setBusy(false)
    setLast(null)
    setMode('setup')
  }
  const openLast = (j: CatalogProfileJob) => {
    onTrack(j)
    setBusy(false)
    setMode('job')
  }

  // 因为没开启被拒的：就地开了（被拒之后设置存过、这里拿到的是新的一份，并且开着）就回到选范围，可以直接开始
  useEffect(() => {
    if (mode === 'blocked' && blocked?.kind === 'disabled' && settings !== blocked.settings && settings.enabled) {
      setBlocked(null)
      setMode('setup')
    }
  }, [mode, blocked, settings])

  const phase: 'setup' | 'blocked' | 'running' | 'done' = inSetup ? 'setup' : mode === 'blocked' ? 'blocked' : isJobActive(job) ? 'running' : 'done'
  const canStart = settings.enabled && picked.tables.length > 0 && !tooMany && !starting
  const retryable = job && phase === 'done' ? retryableTasks(job).length : 0

  let body: ReactNode
  let footer: ReactNode
  if (phase === 'setup') {
    body = (
      <>
        <p className="text-xs leading-relaxed text-dim">{PT.intro}</p>
        {!settings.enabled && (
          <Notice tone="warn" attr={{ 'data-profile-disabled': '' }}>
            <p className="font-medium">{PT.disabledTitle}</p>
            <p className="mt-0.5 text-dim">{PT.disabledBody}</p>
            <button type="button" className="btn btn-sm mt-2" onClick={onSettings} data-profile-enable="">{PT.enable}</button>
          </Notice>
        )}
        <fieldset>
          <legend className="label">{PT.scope}</legend>
          <div className="space-y-1.5" role="radiogroup" aria-label={PT.scope}>
            {scopes.map((s) => {
              const over = !!s.body.tables && s.tables.length > PROFILE_MAX_TABLES
              const note = over ? PT.scopeTooMany(PROFILE_MAX_TABLES)
                : !s.tables.length && s.key !== 'needed' && s.key !== 'observe' ? PT.scopeEmpty
                  : s.leftOut ? PT.scopeLeftOut(s.leftOut) : s.hint
              return (
                <label key={s.key} className={clsx('flex cursor-pointer items-start gap-2 rounded-md border px-3 py-2 text-xs',
                  picked.key === s.key ? 'border-[var(--accent)] bg-accent-soft' : 'hover:bg-hover')} data-profile-scope={s.key}
                       data-count={s.tables.length}>
                  <input type="radio" className="mt-0.5" name="catalog-profile-scope" checked={picked.key === s.key} onChange={() => setScope(s.key)} />
                  <span className="min-w-0 break-words">
                    <span>{s.label}</span>
                    {note && (
                      <span className={clsx('mt-0.5 block text-2xs', over ? 'text-[var(--err)]' : 'text-faint')} data-profile-scope-note="">{note}</span>
                    )}
                  </span>
                </label>
              )
            })}
          </div>
        </fieldset>
        {settings.enabled && <Budget settings={settings} sourceName={sourceName} tables={picked.tables.length} onSettings={onSettings} />}
        <p className="flex items-start gap-1.5 rounded-md px-2 py-1.5 text-2xs leading-relaxed text-dim" data-profile-readonly=""
           style={{ background: 'var(--st-waiting-soft)' }}>
          <ShieldAlert size={12} className="mt-px shrink-0" style={{ color: 'var(--st-waiting)' }} aria-hidden /> <span>{PT.readOnly(settings.table_concurrency)}</span>
        </p>
        {draftActive && settings.enabled && (
          <p className="flex items-start gap-1.5 text-2xs leading-relaxed text-dim" data-profile-queue="">
            <Info size={11} className="mt-px shrink-0" style={{ color: 'var(--accent)' }} aria-hidden /> <span>{PT.queueHint}</span>
          </p>
        )}
        {last && (
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-md border px-3 py-2 text-2xs text-dim" data-profile-last={last.status}>
            <TriangleAlert size={11} className="shrink-0" style={{ color: 'var(--warn)' }} aria-hidden />
            <span className="min-w-0 flex-1">{PT.last(PT.jobTitle[last.status], formatTime(last.finished_at ?? last.created_at))}</span>
            <button type="button" className="btn btn-sm" onClick={() => openLast(last)} data-profile-last-open="">{PT.lastOpen}</button>
          </div>
        )}
        {startError && <Notice tone="err" attr={{ 'data-profile-start-error': '' }}>{startError}</Notice>}
      </>
    )
    footer = (
      <>
        <button type="button" className="btn" onClick={onClose}>{CT.cancel}</button>
        <button type="button" className="btn btn-primary" disabled={!canStart} onClick={() => void begin()} data-profile-start={draftActive ? 'queue' : 'start'}>
          {starting ? <Spinner size={11} /> : <ScanSearch size={12} aria-hidden />} {draftActive ? PT.queue : PT.start}
        </button>
      </>
    )
  } else if (phase === 'blocked' && blocked) {
    body = <Blocked blocked={blocked} observing={blocked.body.scope === 'observe'} onSettings={onSettings} onGoSource={onGoSource}
                    onViewActive={() => void viewActive()}
                    onChooseAll={() => { setScope('all'); void begin({ scope: 'all' }) }} onRetry={() => void begin(blocked.body)} />
    footer = (
      <>
        <button type="button" className="btn btn-ghost mr-auto" onClick={backToSetup} data-profile-back="">{PT.backToSetup}</button>
        <button type="button" className="btn" onClick={onClose}>{PT.close}</button>
      </>
    )
  } else if (job) {
    const running = phase === 'running'
    body = (
      <>
        <JobView key={job.id} sourceId={sourceId} job={job} running={running} stopping={stopping} busy={busy} filled={filled}
                 returnTo={returnTo} onSettings={onSettings} onOpenTable={onOpenTable} onFillCodes={onFillCodes} />
        {actionError && <Notice tone="err" attr={{ 'data-profile-action-error': '' }}>{actionError}</Notice>}
      </>
    )
    footer = running
      ? (
        <>
          <button type="button" className="btn" onClick={onClose} title={PT.hideHint} data-profile-hide="">{PT.hide}</button>
          <button type="button" className="btn" onClick={() => void stop()} disabled={stopping || acting} data-profile-stop="">{PT.stop}</button>
        </>
      )
      : (
        <>
          <button type="button" className="btn btn-ghost mr-auto" onClick={backToSetup} data-profile-again="">{PT.again}</button>
          {retryable > 0 && (
            <button type="button" className="btn" onClick={() => void retry(job)} disabled={acting} title={PT.retryHint} data-profile-retry="">
              {acting ? <Spinner size={11} /> : <RotateCcw size={11} aria-hidden />} {PT.retry(retryable)}
            </button>
          )}
          <button type="button" className="btn btn-primary" onClick={onClose} data-autofocus data-profile-close="">{PT.close}</button>
        </>
      )
  }

  return (
    <Modal open onClose={onClose} title={PT.title} width={680} footer={footer}>
      <div className="min-w-0 space-y-4" data-profile-dialog={phase}>{body}</div>
    </Modal>
  )
}

/**
 * 确认前写明预算：按表的查询数和时长、单条时限、抽样规模、整表统计的门槛、同时剖析几张表，再按这次的表数估一个上限
 * （时长按同时剖析的张数分几轮算）
 */
function Budget({ settings: s, sourceName, tables, onSettings }: {
  settings: CatalogProfileSettings; sourceName: string; tables: number; onSettings: () => void
}) {
  const lines = [PT.budgetPerTable(s.table_max_queries, s.table_max_s), PT.budgetQuery(s.query_timeout_s), PT.budgetSample(s.sample_size),
    PT.budgetScan(s.max_scan_rows), PT.budgetPace(s.table_concurrency)]
  const rounds = Math.ceil(tables / Math.max(1, s.table_concurrency))
  return (
    <div className="rounded-lg border px-3 py-2.5" data-profile-budget={`${s.table_max_queries},${s.table_max_s},${s.query_timeout_s}`}>
      <div className="mb-1.5 flex flex-wrap items-center gap-2 text-2xs">
        <span className="font-medium text-dim">{PT.budget}</span>
        <span className="text-faint">{PT.budgetFrom(sourceName)}</span>
        <span className="flex-1" />
        <button type="button" className="rounded px-1 text-2xs text-dim underline decoration-dotted underline-offset-2 hover:text-fg"
                onClick={onSettings} data-profile-settings-open="">{PT.editSettings}</button>
      </div>
      <ul className="space-y-0.5 text-xs">
        {lines.map((l) => (
          <li key={l} className="flex items-start gap-1.5">
            <span className="mt-[7px] h-1 w-1 shrink-0 rounded-full bg-[var(--text-faint)]" aria-hidden />
            <span className="tnum">{l}</span>
          </li>
        ))}
      </ul>
      {tables > 0 && (
        <p className="tnum mt-1.5 text-xs font-medium" data-profile-budget-total={tables * s.table_max_queries}>
          {PT.budgetTotal(tables, tables * s.table_max_queries, spanText(rounds * s.table_max_s))}
        </p>
      )}
    </div>
  )
}

/** 开始不了（409）：是哪种情况、服务端的原话、下一步和对应的按钮 */
function Blocked({ blocked: b, observing, onSettings, onGoSource, onViewActive, onChooseAll, onRetry }: {
  blocked: { kind: ProfileBlock; message: string; active: CatalogProfileJob | null }
  /** 选的是取值观察：挑不出表时说「没有要观察的表」，不给「改选全部表」（那是剖析，不是观察） */
  observing: boolean
  onSettings: () => void
  onGoSource: () => void
  onViewActive: () => void
  onChooseAll: () => void
  onRetry: () => void
}) {
  const nothingObserve = observing && b.kind === 'nothing'
  const action = b.kind === 'disabled'
    ? <button type="button" className="btn btn-sm btn-primary" onClick={onSettings} data-profile-enable="">{PT.enable}</button>
    : b.kind === 'busy'
      ? <button type="button" className="btn btn-sm btn-primary" onClick={onViewActive} data-profile-view-active="">{PT.viewActive}</button>
      : b.kind === 'nothing'
        ? (observing ? null : <button type="button" className="btn btn-sm" onClick={onChooseAll} data-profile-choose-all="">{PT.chooseAll}</button>)
        : b.kind === 'other'
          ? <button type="button" className="btn btn-sm" onClick={onRetry} data-profile-retry-start="">{PT.retryStart}</button>
          : <button type="button" className="btn btn-sm" onClick={onGoSource} data-profile-go-source="">{PT.goSource}</button>
  return (
    <Notice tone={b.kind === 'busy' || b.kind === 'nothing' ? 'info' : 'warn'} attr={{ 'data-profile-blocked': b.kind }}>
      <p className="font-medium">{PT.blocked[nothingObserve ? 'nothingObserve' : b.kind]}</p>
      <p className="mt-0.5 break-words text-dim">{b.message}</p>
      {b.kind === 'busy' && b.active && (
        <p className="tnum mt-0.5 text-dim" data-profile-busy-at="">{PT.busyAt(b.active.created_by, b.active.done, b.active.total)}</p>
      )}
      <p className="mt-0.5 text-dim">{PT.blockedNext[nothingObserve ? 'nothingObserve' : b.kind]}</p>
      {action && <div className="mt-2">{action}</div>}
    </Notice>
  )
}

// ---------------------------------------------------------------------------
// 任务：进度、汇总、逐表
// ---------------------------------------------------------------------------

const hasFindings = (t: CatalogProfileTaskRow) => t.summary.relations_checked + t.summary.codes + t.summary.business_dates > 0
const FILTER_TEST: Record<Filter, (t: CatalogProfileTaskRow) => boolean> = {
  all: () => true,
  findings: hasFindings,
  skipped: (t) => t.skipped_count > 0 || t.status === 'skipped',
  failed: (t) => t.status === 'failed',
}

function JobView({ sourceId, job, running, stopping, busy, filled, returnTo, onSettings, onOpenTable, onFillCodes }: {
  sourceId: string
  job: CatalogProfileJob
  running: boolean
  stopping: boolean
  busy: boolean
  filled: Record<string, number>
  returnTo: { table: string; column: string } | null
  onSettings: () => void
  onOpenTable: (table: string) => void
  onFillCodes: (table: string, column: string) => void
}) {
  const now = useTicker(1000, running)
  const queued = job.status === 'queued'
  const title = running ? (stopping ? PT.stopping : PT.jobTitle[job.status]) : PT.jobTitle[job.status]
  const started = job.started_at ? Date.parse(job.started_at) : NaN
  const ended = job.finished_at ? Date.parse(job.finished_at) : NaN
  const elapsedMs = Number.isFinite(started) ? (running ? Math.max(now, Date.now()) : Number.isFinite(ended) ? ended : started) - started : null
  // 进行中按已结束的表数走（真进度）；结束以后写做完了几张（停止、失败、跳过的不算「完成」），同起草
  const ok = job.counts.ok ?? job.tasks.filter((t) => t.status === 'ok').length
  const value = running ? job.done : ok
  const pct = job.total ? (value / job.total) * 100 : 0
  const bad = job.tasks.some((t) => PROFILE_RETRYABLE.has(t.status))
  const tone = job.status === 'failed' ? 'var(--err)' : running ? 'var(--accent)' : bad ? 'var(--warn)' : 'var(--st-done)'
  const progress = running ? PT.progress(job.done, job.total) : PT.progressDone(ok, job.total)
  const badQueries = PT.queriesBad(job.timeouts, job.errors)
  const notice = running ? job.notice?.trim() ?? '' : ''
  const changed = job.added + job.updated + job.removed
  const findings = PT.findings(job.summary)
  // 停下以后写进了什么按逐表照实说：没来得及收尾的表点名；收了尾的表（完成、部分完成）做完的项写进了目录
  const unsaved = job.tasks.filter(isUnsaved).map((t) => t.table_name)
  const written = job.tasks.some((t) => t.status === 'ok' || t.status === 'partial')
  const [filter, setFilter] = useState<Filter>('all')
  const radio = useRadioGroup(FILTERS, filter, setFilter)
  const tasks = running ? job.tasks : job.tasks.filter(FILTER_TEST[filter])
  const counts = useMemo(() => Object.fromEntries(FILTERS.map((f) => [f, job.tasks.filter(FILTER_TEST[f]).length])) as Record<Filter, number>,
    [job.tasks])
  // 展开的表：填完码值回到结果时，那张表照旧展开
  const [open, setOpen] = useState<Set<number>>(() => {
    const t = returnTo ? job.tasks.find((x) => x.table_name === returnTo.table) : null
    return new Set(t ? [t.seq] : [])
  })
  const toggle = (seq: number) => setOpen((cur) => {
    const next = new Set(cur)
    if (next.has(seq)) next.delete(seq)
    else next.add(seq)
    return next
  })
  return (
    <div className="min-w-0 space-y-3">
      {busy && <Notice tone="info" attr={{ 'data-profile-busy': '' }}>{PT.busyShown}</Notice>}
      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs" role="status" aria-live="polite">
        {running && <Spinner size={13} />}
        <span className="font-medium" data-profile-title="">{title}</span>
        <span className="flex-1" />
        <span className="tnum text-2xs text-faint">
          <span data-profile-progress={`${job.done}/${job.total}`}>{progress}</span>
          {!queued && <span data-profile-queries={job.queries}> · {PT.queriesSent(job.queries)}</span>}
          {badQueries && <span className="text-[var(--warn)]" data-profile-queries-bad=""> （{badQueries}）</span>}
          {elapsedMs != null && <span data-profile-elapsed={Math.floor(elapsedMs / 1000)}> · {PT.elapsed(secondsText(elapsedMs))}</span>}
        </span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-hover" role="progressbar" aria-label={progress}
           aria-valuemin={0} aria-valuemax={job.total} aria-valuenow={value}>
        <div className="h-full rounded-full transition-[width] duration-300" style={{ width: `${pct}%`, background: tone }} />
      </div>
      {queued && (job.waiting_for === 'draft' || job.waiting_for === 'introspect') && (
        <p className="flex items-start gap-1.5 text-2xs leading-relaxed text-dim" data-profile-waiting={job.waiting_for}>
          <Info size={11} className="mt-px shrink-0" style={{ color: 'var(--accent)' }} aria-hidden />
          <span>{job.waiting_for === 'draft' ? PT.waitingDraft : PT.waitingIntrospect}</span>
        </p>
      )}
      {notice && <Notice tone="warn" className="break-words" attr={{ 'data-profile-notice': '' }}>{notice}</Notice>}
      {job.created_by && <p className="text-2xs text-faint" data-profile-created-by="">{PT.createdBy(job.created_by)}</p>}
      {running && <p className="text-2xs leading-relaxed text-faint">{PT.hideHint}</p>}
      {!running && (
        <div className="space-y-0.5">
          <p className="tnum text-sm" data-profile-counts="">{PT.counts(job.total, job.counts)}</p>
          <p className="tnum text-xs text-dim" data-profile-findings-summary="">{findings || PT.noFindings}</p>
          <p className="tnum text-xs text-dim" data-profile-changes={`${job.added},${job.updated},${job.removed}`}>
            {changed ? PT.changes(job.added, job.updated, job.removed) : PT.noChange}
          </p>
        </div>
      )}
      {job.stop_reason && (
        <Notice tone="warn" attr={{ 'data-profile-stopped': job.stop_reason }}>
          <p className="font-medium">{PT.stopTitle[job.stop_reason]}</p>
          {job.error && <p className="mt-0.5 break-words text-dim" data-profile-stop-error="">{job.error}</p>}
          <p className="mt-0.5 text-dim">{PT.stopNext[job.stop_reason]}</p>
          {job.stop_reason === 'slow' && (
            <button type="button" className="btn btn-sm mt-2" onClick={onSettings} data-profile-settings-open="">{PT.editSettings}</button>
          )}
        </Notice>
      )}
      {!job.stop_reason && job.status === 'failed' && job.error && (
        <Notice tone="err" className="break-words" attr={{ 'data-profile-job-error': '' }}>{job.error}</Notice>
      )}
      {!running && job.status === 'cancelled' && <p className="text-2xs leading-relaxed text-dim" data-profile-cancelled="">{PT.cancelledNote(unsaved, written)}</p>}
      {job.status === 'interrupted' && <Notice tone="warn" attr={{ 'data-profile-interrupted': '' }}>{PT.interruptedNote(unsaved, written)}</Notice>}
      <div>
        <div className="mb-1 flex flex-wrap items-center gap-2">
          <span className="text-2xs font-medium text-dim" id="catalog-profile-tasks-label">{PT.tasks}</span>
          <span className="flex-1" />
          {!running && job.tasks.length > 1 && (
            <div role="radiogroup" aria-label={PT.filterLabel} className="flex flex-wrap gap-1" data-profile-filter={filter}>
              {FILTERS.map((f) => (
                <button key={f} type="button" {...radio(f)} onClick={() => setFilter(f)} data-profile-filter-key={f}
                        className={clsx('inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-2xs outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]',
                          filter === f ? 'border-[var(--accent)] bg-accent-soft text-fg' : 'text-dim hover:bg-hover hover:text-fg')}>
                  {PT.filter[f]} <span className="tnum text-faint">{formatNumber(counts[f])}</span>
                </button>
              ))}
            </div>
          )}
        </div>
        <ul className="max-h-[22rem] divide-y divide-[var(--hairline)] overflow-y-auto overflow-x-hidden rounded-md border"
            aria-labelledby="catalog-profile-tasks-label" data-profile-tasks="">
          {tasks.map((t) => (
            <TaskItem key={t.seq} sourceId={sourceId} job={job} task={t} open={open.has(t.seq)} onToggle={() => toggle(t.seq)} filled={filled}
                      focusAfter={returnTo && returnTo.table === t.table_name ? returnTo.column : null}
                      onOpenTable={onOpenTable} onFillCodes={onFillCodes} />
          ))}
          {!tasks.length && <li className="px-3 py-3 text-center text-2xs text-faint" data-profile-filter-none="">{PT.filterNone}</li>}
        </ul>
      </div>
    </div>
  )
}

export const PROFILE_TASK_ICON: Record<CatalogProfileTaskStatus, { Icon: LucideIcon | null; color: string }> = {
  pending: { Icon: CircleDashed, color: 'var(--text-faint)' },
  running: { Icon: null, color: 'var(--accent)' },
  ok: { Icon: CircleCheck, color: 'var(--st-done)' },
  partial: { Icon: TriangleAlert, color: 'var(--warn)' },
  failed: { Icon: CircleX, color: 'var(--err)' },
  skipped: { Icon: CircleMinus, color: 'var(--text-faint)' },
  cancelled: { Icon: CircleSlash, color: 'var(--text-faint)' },
}

/** 各类检查的进度：「关系 3/4 · 码值 5/6 · 日期 1/1 · 取值观察 4/4」，这类没有检查的不写 */
export function checksText(t: Pick<CatalogProfileTaskRow, 'checks_by_kind'>): string {
  return (['relation', 'codes', 'date', 'observe'] as const)
    .map((k) => {
      const [done, total] = t.checks_by_kind?.[k] ?? [0, 0]
      return total > 0 ? PT.kindProgress(PT.checkKind[k], done, total) : ''
    })
    .filter(Boolean).join(' · ')
}

/**
 * 没来得及收尾的表：开始剖析了（有开始时刻），却以「已停止」结束。正常收尾的表状态按逐表算（完成、部分完成、失败、跳过），
 * 不会是已停止；没开始就停下的表没有开始时刻。只有进程被杀（下次启动时才标中断）或停止时服务端没在跑它，正在做的那几张才以
 * 已停止结束——目录只在整张表结束时和它的结果一起写，它这次一项结论都没写进去（服务端把查完的项改记跳过，这张表的原因
 * 写明查完的几项没写进目录；一项都还没查完时原因就是任务的那一句）
 */
export const isUnsaved = (t: Pick<CatalogProfileTaskRow, 'status' | 'started_at'>): boolean => t.status === 'cancelled' && !!t.started_at

/**
 * 一张表的结论：进行中写正在做哪一项；结束了写发现（核实了几条关系、取到几列码值……），没查成的写几项，整张表的原因照服务端
 * 的原话。jobError：任务整体的原因（止损、服务重启时服务端把后面的表也记上同一句），和它一样的不再逐表重复。
 * 没来得及收尾的表写明这次的结论没写进目录（服务端给了自己的原因时用原话）
 */
export function taskNote(t: CatalogProfileTaskRow, jobError: string | null): string {
  if (t.status === 'pending') return ''
  if (t.status === 'running') return t.current ?? ''
  const own = t.error && t.error !== jobError ? t.error : ''
  if (isUnsaved(t)) return own || PT.notSaved
  if (t.status === 'skipped' || t.status === 'failed') return own
  const parts = [PT.findings(t.summary, true), t.unfinished ? PT.unfinished(t.unfinished) : '', own]
  if (t.status === 'ok' && !parts.some(Boolean)) return PT.noFindings
  return parts.filter(Boolean).join('；')
}

function TaskItem({ sourceId, job, task: t, open, onToggle, filled, focusAfter, onOpenTable, onFillCodes }: {
  sourceId: string
  job: CatalogProfileJob
  task: CatalogProfileTaskRow
  open: boolean
  onToggle: () => void
  filled: Record<string, number>
  focusAfter: string | null
  onOpenTable: (table: string) => void
  onFillCodes: (table: string, column: string) => void
}) {
  const { Icon, color } = PROFILE_TASK_ICON[t.status]
  const checks = checksText(t)
  const size = t.status !== 'skipped' ? sizeText(t.row_estimate) : null
  const usedUp = t.queries > 0 && t.queries >= job.settings.table_max_queries
  const note = taskNote(t, job.error)
  // 没开始就停下的表（停止、止损、服务重启时还在排队）不写各类检查的 0/N：它一项都没做
  const started = !!t.started_at || t.queries > 0
  const panelId = `catalog-profile-task-${t.seq}`
  return (
    <li className="min-w-0" data-profile-task={t.table_name} data-status={t.status} data-seq={t.seq}>
      <button type="button" className="block w-full min-w-0 px-3 py-1.5 text-left outline-none hover:bg-hover focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--accent)]"
              aria-expanded={open} aria-controls={panelId} aria-label={PT.expandLabel(t.table_name)} onClick={onToggle} data-profile-task-toggle="">
        <span className="flex min-w-0 items-center gap-2 text-xs">
          <ChevronRight size={11} className={clsx('shrink-0 text-faint transition-transform', open && 'rotate-90')} aria-hidden />
          {Icon ? <Icon size={12} className="shrink-0" style={{ color }} aria-hidden /> : <Spinner size={12} />}
          <span className="mono min-w-0 flex-1 truncate" title={t.table_name}>{t.table_name}</span>
          <span className="shrink-0 text-2xs" style={{ color: t.status === 'pending' || t.status === 'skipped' || t.status === 'cancelled' ? undefined : color }}
                data-profile-task-status="">
            {PT.taskStatus[t.status]}
          </span>
        </span>
        {started && (checks || t.queries > 0 || size) && (
          <span className="tnum mt-0.5 flex flex-wrap gap-x-2 gap-y-0.5 pl-[2.375rem] text-2xs text-faint">
            {checks && <span data-profile-task-checks="">{checks}</span>}
            {t.queries > 0 && (
              <span className={usedUp ? 'text-[var(--warn)]' : undefined} data-profile-task-queries={t.queries}>
                {usedUp ? PT.tableQueriesUsedUp(t.queries) : PT.tableQueries(t.queries)}
              </span>
            )}
            {t.elapsed_ms > 0 && <span data-profile-task-elapsed="">{secondsText(t.elapsed_ms)}</span>}
            {size && <span title={size.hint}>{size.text}</span>}
            {t.skipped_count > 0 && t.status !== 'skipped' && <span data-profile-task-skipped={t.skipped_count}>{PT.skippedN(t.skipped_count)}</span>}
          </span>
        )}
        {note && (
          <span className={clsx('mt-0.5 block break-words pl-[2.375rem] text-2xs leading-relaxed', t.status === 'failed' ? 'text-[var(--err)]' : 'text-dim')}
                data-profile-task-note="">
            {note}
          </span>
        )}
      </button>
      {open && (
        <div id={panelId} className="border-t border-hairline bg-elev px-3 py-2.5 pl-[2.375rem]" data-profile-task-detail={t.table_name}>
          <TaskDetailLoader sourceId={sourceId} jobId={job.id} task={t} filled={filled} focusAfter={focusAfter}
                            onOpenTable={onOpenTable} onFillCodes={onFillCodes} />
        </div>
      )}
    </li>
  )
}

/**
 * 展开的一张表：按 seq 取完整结果（检查清单、发现、发过的查询）。还在剖析的表只在一项检查开始、结束时重取：服务端这时才
 * 写检查清单、正在做哪一项和发过的查询（开始时 current 换成这一项，结束时结束的项数加一、current 清空）；其间每发一条
 * 查询只加查询数和用时，这两样逐表一行上就有，跟着它重取只会每次轮询都多取一份没变的结果
 */
function TaskDetailLoader({ sourceId, jobId, task: t, filled, focusAfter, onOpenTable, onFillCodes }: {
  sourceId: string
  jobId: string
  task: CatalogProfileTaskRow
  filled: Record<string, number>
  focusAfter: string | null
  onOpenTable: (table: string) => void
  onFillCodes: (table: string, column: string) => void
}) {
  const [detail, setDetail] = useState<CatalogProfileTaskDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [again, setAgain] = useState(0)
  const sig = `${t.status}:${t.checks_done}:${t.current ?? ''}`
  useEffect(() => {
    let alive = true
    api.dataCatalog.profileJobs.task(sourceId, jobId, t.seq).then(({ task }) => {
      if (!alive) return
      setDetail(task)
      setError(null)
    }, (e) => { if (alive) setError(errorMessage(e)) })
    return () => { alive = false }
  }, [sourceId, jobId, t.seq, sig, again])
  if (!detail) {
    return error
      ? (
        <p className="text-2xs text-[var(--err)]" data-profile-task-load-error="">
          {PT.loadFailed(error)}
          <button type="button" className="ml-2 underline" onClick={() => setAgain((n) => n + 1)}>{PT.retryStart}</button>
        </p>
      )
      : <p className="flex items-center gap-1.5 text-2xs text-faint"><Spinner size={10} /> {PT.loading}</p>
  }
  return <ProfileTaskBody detail={detail} mode="job" filled={filled} focusAfter={focusAfter} onOpenTable={onOpenTable}
                          onFill={(column) => onFillCodes(t.table_name, column)} />
}

// ---------------------------------------------------------------------------
// 一张表的剖析结果：逐表展开里、表详情的「数据剖析」一节共用
// ---------------------------------------------------------------------------

/**
 * mode job：任务里展开的一行，写检查清单（每项的状态、原因、用了几条查询）、发现、发过的查询；
 * mode detail：表详情里，写行数和来源、日期范围、发现、跳过的项、发过的查询（检查清单在任务里看）。
 * onFill 不给时码值不带「填写含义」
 */
export function ProfileTaskBody({ detail: d, mode, filled = {}, focusAfter = null, onOpenTable, onFill, canFill }: {
  detail: CatalogProfileTaskDetail
  mode: 'job' | 'detail'
  filled?: Record<string, number>
  focusAfter?: string | null
  onOpenTable?: (table: string) => void
  onFill?: (column: string) => void
  /** 这一列能不能填含义（码值候选还在目录里）；不给就是都能填 */
  canFill?: (column: string) => boolean
}) {
  const relations = d.findings.filter((f): f is CatalogProfileRelationFinding => f.kind === 'relation')
  const codes = d.findings.filter((f): f is CatalogProfileCodesFinding => f.kind === 'codes')
  const date = d.findings.find((f): f is CatalogProfileDateFinding => f.kind === 'business_date')
  const size = sizeText(d.row_estimate)
  // 检查清单里没有的跳过项（行数估算、整张表）：任务里也要看得到
  const skipped = mode === 'detail' ? d.skipped : d.skipped.filter((s) => s.kind !== 'relation' && s.kind !== 'codes' && s.kind !== 'date')
  const root = useRef<HTMLDivElement>(null)
  // 填完一列回到结果：焦点交给它后面第一个还有含义待填写的「填写含义」，没有了就落回刚填的那一列。弹窗自己的初始焦点在
  // 下一帧，这里先聚焦，它看到焦点已在弹窗里就不再动
  useEffect(() => {
    if (!focusAfter) return
    const buttons = [...(root.current?.querySelectorAll<HTMLButtonElement>('[data-profile-fill-key]') ?? [])]
    const at = buttons.findIndex((b) => b.dataset.profileFillKey === fillKey(d.table_name, focusAfter))
    const target = buttons.slice(at + 1).find((b) => Number(b.dataset.pending) > 0) ?? buttons[at]
    target?.focus()
    target?.scrollIntoView({ block: 'nearest' })
  }, [focusAfter, d.table_name])
  return (
    <div ref={root} className="min-w-0 space-y-2.5" data-profile-body={mode}>
      {(size || (mode === 'job' && onOpenTable && d.status !== 'skipped')) && (
        <div className="flex flex-wrap items-center gap-x-2 text-2xs text-dim">
          {size && (
            <span className="tnum" data-profile-rows="">
              <span className="text-faint">{PT.rowsTitle}：</span>{size.text}{size.source && <span className="text-faint">（{size.source}）</span>}
            </span>
          )}
          <span className="flex-1" />
          {mode === 'job' && onOpenTable && (
            <button type="button" className="inline-flex items-center gap-0.5 rounded px-1 text-2xs text-dim hover:bg-hover hover:text-fg"
                    onClick={() => onOpenTable(d.table_name)} aria-label={PT.openTableLabel(d.table_name)} data-profile-open={d.table_name}>
              {PT.openTable} <ArrowUpRight size={10} aria-hidden />
            </button>
          )}
        </div>
      )}
      {d.error && (
        <p className="flex items-start gap-1.5 text-xs text-dim" data-profile-table-error="">
          <Info size={12} className="mt-0.5 shrink-0 text-faint" aria-hidden /> <span className="break-words">{d.error}</span>
        </p>
      )}
      {mode === 'job' && d.checks.length > 0 && <Checks checks={d.checks} />}
      {relations.length > 0 && (
        <Group title={PT.findingRelations}>
          {relations.map((f) => <RelationLine key={f.path} f={f} />)}
        </Group>
      )}
      {codes.length > 0 && (
        <Group title={PT.findingCodes}>
          {codes.map((f) => (
            <CodesLine key={f.path} f={f} fillKey={fillKey(d.table_name, f.column)} filled={filled[fillKey(d.table_name, f.column)]}
                       onFill={onFill && (!canFill || canFill(f.column)) ? () => onFill(f.column) : undefined} />
          ))}
        </Group>
      )}
      {date && (
        <Group title={PT.findingDate}>
          <li className="text-xs" data-profile-finding="business_date">
            <StatusChip status={date.status} className="mr-1.5 align-middle" />
            {PT.dateProposal(date.column)}
            <span className="tnum text-dim">，{PT.dateSpan(date.min.slice(0, 19), date.max.slice(0, 19))}</span>
            {(() => {
              const invalid = d.date_ranges.find((r) => r.column === date.column)?.invalid
              return invalid ? <span className="tnum text-dim">，{PT.dateInvalid(invalid)}</span> : null
            })()}
          </li>
        </Group>
      )}
      {d.date_ranges.length > 0 && (!date || d.date_ranges.length > 1) && (
        <Group title={PT.dateRanges}>
          {d.date_ranges.map((r) => (
            <li key={r.column} className="tnum text-xs text-dim" data-profile-date-range={r.column}>
              <span className="mono text-fg">{r.column}</span>：{PT.dateSpan(r.min.slice(0, 19), r.max.slice(0, 19))}
              {r.invalid ? `，${PT.dateInvalid(r.invalid)}` : null}
            </li>
          ))}
        </Group>
      )}
      {!d.findings.length && !d.error && (d.status === 'ok' || d.status === 'partial') && (
        <p className="text-2xs text-faint">{PT.noFindings}</p>
      )}
      {skipped.length > 0 && <Skipped list={skipped} />}
      {d.query_log.length > 0 && <QueryLog list={d.query_log} />}
    </div>
  )
}

const CHECK_ICON: Record<CatalogProfileCheck['state'], { Icon: LucideIcon | null; color: string }> = {
  pending: { Icon: CircleDashed, color: 'var(--text-faint)' },
  running: { Icon: null, color: 'var(--accent)' },
  done: { Icon: CircleCheck, color: 'var(--st-done)' },
  skipped: { Icon: CircleMinus, color: 'var(--text-faint)' },
}

/** 检查清单：每项的状态、原因（没查成的用警告色：不是结论错了，旧结论保留）、用了几条查询 */
function Checks({ checks }: { checks: CatalogProfileCheck[] }) {
  const done = checks.filter((c) => c.state === 'done' || c.state === 'skipped').length
  return (
    <div data-profile-checks={checks.length}>
      <div className="mb-1 text-2xs font-medium text-faint">{PT.checksTitle(done, checks.length)}</div>
      <ul className="space-y-1">
        {checks.map((c, i) => {
          const { Icon, color } = CHECK_ICON[c.state]
          const unfinished = c.state === 'skipped' && !!c.reason && UNFINISHED_REASONS.has(c.reason)
          return (
            <li key={`${c.kind}:${c.target}:${i}`} className="flex min-w-0 items-start gap-1.5 text-2xs" data-profile-check={c.kind} data-state={c.state}
                data-reason={c.reason ?? undefined}>
              <span className="mt-px shrink-0">{Icon ? <Icon size={11} style={{ color }} aria-hidden /> : <Spinner size={11} />}</span>
              <span className="min-w-0 flex-1 leading-relaxed">
                <span className="text-faint">{PT.checkKind[c.kind] ?? c.kind} </span>
                <span className="mono text-dim [overflow-wrap:anywhere]">{c.target}</span>
                <span className="text-faint"> · {PT.checkState[c.state] ?? c.state}</span>
                {c.reason && (
                  <span className="chip ml-1.5 !px-1.5 !py-0" style={{ color: unfinished ? 'var(--warn)' : 'var(--text-dim)' }} data-profile-check-reason="">
                    {PROFILE_SKIP_REASON_LABEL[c.reason] ?? c.reason}
                  </span>
                )}
                {c.detail && <span className="block break-words text-dim">{c.detail}</span>}
              </span>
              {c.queries > 0 && <span className="tnum shrink-0 text-faint">{PT.checkQueries(c.queries)}</span>}
            </li>
          )
        })}
      </ul>
    </div>
  )
}

/** 发过的查询（字面量换成了 ?）：折起来，点开看每一条的类别、目标、结果、用时、行数和 SQL */
function QueryLog({ list }: { list: CatalogProfileQuery[] }) {
  return (
    <details className="text-2xs" data-profile-queries-log={list.length}>
      <summary className="cursor-pointer select-none text-dim hover:text-fg" data-profile-queries-open="">{PT.queriesTitle(list.length)}</summary>
      <p className="mt-1 leading-relaxed text-faint">{PT.queriesHint}</p>
      <ol className="mt-1.5 space-y-1.5">
        {list.map((q, i) => (
          <li key={i} className="min-w-0 rounded border bg-bg px-2 py-1.5" data-profile-query={q.kind} data-outcome={q.outcome}>
            <div className="tnum flex flex-wrap items-center gap-x-2 gap-y-0.5 text-faint">
              <span className="text-dim">{PT.queryKind[q.kind] ?? q.kind}</span>
              <span className="mono min-w-0 [overflow-wrap:anywhere]">{q.target}</span>
              <span className="flex-1" />
              <span style={{ color: q.outcome === 'ok' ? undefined : q.outcome === 'timeout' ? 'var(--warn)' : 'var(--err)' }} data-profile-query-outcome="">
                {PT.queryOutcome[q.outcome] ?? q.outcome}
              </span>
              <span>{PT.queryMs(q.ms)}</span>
              {q.rows != null && <span>{PT.queryRows(q.rows)}</span>}
              <span title={formatDateTime(q.at)}>{formatTime(q.at)}</span>
            </div>
            <pre className="mono mt-1 whitespace-pre-wrap break-all text-[11px] leading-relaxed text-dim" data-profile-query-sql="">{q.sql}</pre>
            {q.detail && <p className="mt-0.5 break-words text-dim">{q.detail}</p>}
          </li>
        ))}
      </ol>
    </details>
  )
}

function Group({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <div className="mb-1 text-2xs font-medium text-faint">{title}</div>
      <ul className="space-y-1.5">{children}</ul>
    </div>
  )
}

type RelationOutcome = 'confirmed' | 'raised' | 'same' | 'lowered' | 'kept'

/**
 * 一条关系这次剖析的结论，按实际变化说：原本就是已验证、这次照样核实的写「仍为已验证，覆盖率 x」，不能说「升为」——
 * 重新剖析、目录没变时汇总写「目录没有变化」，下面却说升了，自相矛盾。服务端不给剖析前的状态时按升级说
 */
function relationOutcome(f: CatalogProfileRelationFinding): { kind: RelationOutcome; text: string; tone: string; icon: LucideIcon } {
  const reason = f.coverage < 0.95 ? PT.relationLowCoverage : PT.relationNotUnique
  if (f.confirmed) return { kind: 'confirmed', text: PT.relationConfirmed, tone: STATUS_TONE.confirmed, icon: UserCheck }
  if (f.status === 'verified') {
    return f.previous_status === 'verified'
      ? { kind: 'same', text: UT.relationStillVerified(coverageText(f.coverage)), tone: STATUS_TONE.verified, icon: Equal }
      : { kind: 'raised', text: PT.relationVerified, tone: STATUS_TONE.verified, icon: ArrowUp }
  }
  return f.previous_status === 'verified'
    ? { kind: 'lowered', text: UT.relationLowered(reason), tone: STATUS_TONE.proposed, icon: ArrowDown }
    : { kind: 'kept', text: `${PT.relationKept}：${reason}`, tone: STATUS_TONE.proposed, icon: Minus }
}

function RelationLine({ f }: { f: CatalogProfileRelationFinding }) {
  const outcome = relationOutcome(f)
  const Icon = outcome.icon
  return (
    <li className="text-xs" data-profile-finding="relation" data-status={f.status} data-path={f.path}>
      <div className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
        <StatusChip status={f.status} />
        <span className="mono min-w-0 [overflow-wrap:anywhere]">{f.target}</span>
      </div>
      <div className="tnum mt-0.5 text-2xs text-dim">
        <span data-profile-coverage="">{PT.coverage(coverageText(f.coverage))}</span>
        {' · '}
        <span data-profile-cardinality="" data-checked={f.cardinality ? String(!!f.cardinality_checked) : undefined}>
          {f.cardinality ? CATALOG_CARDINALITY_LABEL[f.cardinality] : PT.cardinalityUnknown}
          {/* 基数用数据核实过没有：读服务端的 cardinality_checked，不从说明的原话里认 */}
          {f.cardinality && f.cardinality_checked !== undefined && (
            <span className="text-faint">（{f.cardinality_checked ? UT.cardinalityChecked : UT.cardinalityByStructure}）</span>
          )}
        </span>
        {' · '}
        {PT.sampled(f.sample, f.matched)}
      </div>
      {/* 结论是状态说明，不是链接：用状态标识的样子（带框的小标签、图标着状态色），正文用次要文字色 */}
      <div className="mt-1">
        <span className="chip !text-2xs" data-profile-outcome={outcome.kind}>
          <Icon size={10} className="shrink-0" style={{ color: outcome.tone }} aria-hidden />
          {outcome.text}
        </span>
      </div>
    </li>
  )
}

function CodesLine({ f, fillKey: key, filled, onFill }: {
  f: CatalogProfileCodesFinding
  fillKey: string
  /** 从结果去填过含义：保存后还剩几个待填写。没去填过为 undefined，按剖析时的说 */
  filled: number | undefined
  onFill?: () => void
}) {
  // 几个含义待填写读服务端的 pending（别的来源写过的含义会沿用），不从 summary 的原话里认
  const pending = filled ?? f.pending ?? 0
  const done = filled != null && filled === 0
  return (
    <li className="text-xs" data-profile-finding="codes" data-column={f.column}>
      <div className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
        <StatusChip status={f.status} />
        <span className="mono font-medium">{f.column}</span>
        <span className="tnum text-2xs text-faint">{PT.codesSummary(f.values.length, f.rows)}</span>
      </div>
      <div className="mt-1 flex flex-wrap gap-1">
        {f.values.slice(0, CODES_SHOWN).map((v) => (
          <span key={v.value} className="inline-flex items-center gap-1 rounded border bg-bg px-1 text-2xs">
            <span className="mono">{v.value}</span>
            <span className="tnum text-faint">{PT.codeRows(v.rows)}</span>
          </span>
        ))}
        {f.values.length > CODES_SHOWN && <span className="text-2xs text-faint">+{f.values.length - CODES_SHOWN}</span>}
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-2">
        {pending > 0 && <span className="text-2xs" style={{ color: 'var(--st-waiting)' }} data-codes-pending={pending}>{PT.codesPendingN(pending)}</span>}
        {done && (
          <span className="inline-flex items-center gap-1 text-2xs text-dim" data-codes-filled="">
            <CircleCheck size={11} className="shrink-0" style={{ color: 'var(--st-done)' }} aria-hidden /> {UT.codesFilled}
          </span>
        )}
        {onFill && (
          <button type="button" className="btn btn-xs" onClick={onFill} aria-label={done ? UT.editMeaningsLabel(f.column) : PT.fillMeaningsLabel(f.column)}
                  data-profile-fill={f.column} data-profile-fill-key={key} data-pending={pending}>
            {done ? UT.editMeanings : PT.fillMeanings}
          </button>
        )}
      </div>
    </li>
  )
}

/** 跳过的项：原因（短）+ 哪一项 + 服务端给的整句。多了折起来 */
function Skipped({ list }: { list: CatalogProfileSkip[] }) {
  const [all, setAll] = useState(false)
  const shown = all ? list : list.slice(0, SKIP_SHOWN)
  return (
    <div data-profile-skipped={list.length}>
      <div className="mb-1 text-2xs font-medium text-faint">{PT.skippedTitle(list.length)}</div>
      <ul className="space-y-1">
        {shown.map((s, i) => (
          <li key={`${s.kind}:${s.target}:${i}`} className="flex items-start gap-1.5 text-2xs" data-profile-skip={s.reason}>
            <span className="chip mt-px shrink-0 !px-1.5 !py-0"
                  style={{ color: UNFINISHED_REASONS.has(s.reason) ? 'var(--warn)' : 'var(--text-dim)' }}>
              {PROFILE_SKIP_REASON_LABEL[s.reason] ?? s.reason}
            </span>
            <span className="min-w-0 leading-relaxed">
              <span className="text-faint">{PROFILE_SKIP_KIND_LABEL[s.kind] ?? s.kind} </span>
              <span className="mono text-dim [overflow-wrap:anywhere]">{s.target}</span>
              <span className="block break-words text-dim">{s.detail}</span>
            </span>
          </li>
        ))}
      </ul>
      {list.length > SKIP_SHOWN && !all && (
        <button type="button" className="mt-1 rounded px-1 text-2xs text-dim underline decoration-dotted underline-offset-2 hover:text-fg"
                onClick={() => setAll(true)}>
          {PT.moreSkipped(list.length - SKIP_SHOWN)}
        </button>
      )}
    </div>
  )
}
