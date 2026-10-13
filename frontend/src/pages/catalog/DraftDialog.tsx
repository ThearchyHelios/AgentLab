import { useEffect, useMemo, useRef, useState } from 'react'
import { CircleCheck, CircleDashed, CircleMinus, CircleSlash, CircleX, RotateCcw, Sparkles, TriangleAlert } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import clsx from 'clsx'
import { ApiError, api } from '../../api/client'
import type { CatalogDraftJob, CatalogDraftTask, CatalogDraftTaskStatus, CatalogTableRow } from '../../types'
import { Field, Modal, Notice, Spinner } from '../../components/ui'
import { errorMessage } from '../../lib/errors'
import { formatTime } from '../../lib/format'
import { CATALOG_DRAFT_TEXT as DT, CATALOG_TEXT as CT, DRAFT_GUIDANCE_MAX, DRAFT_NOTE_MAX } from '../../lib/terms'
import {
  DRAFT_MAX_TABLES, DRAFT_MODEL_UNAVAILABLE, DRAFT_TOP_N, RETRYABLE, isJobActive, nothingWritten, retryableCount, taskReason,
} from './draftJob'
import type { DraftStartBody } from './draftJob'

// ===========================================================================
// 起草数据目录。起草是服务端的后台任务：这里选范围、写起草要求和本次补充说明，建任务；之后显示页面跟着的那个任务
// （进行中的进度、结束后的结果），任务本身的状态在页面上（draftJob.ts），关掉对话框照常进行。
//
// 三段：设置（范围、用不用模型；用模型时写起草要求、补充说明，写明用哪个起草模型）→ 进行中（总进度、合计、逐表的
// 状态和宽表的段进度、没写全的原因；可以停止、在后台继续）→ 结束（汇总、没写全的表和原因、只重试失败的表）。
// 已经写进目录的内容不回滚——起草只增改推断项，人工确认、驳回过的项服务端不会动。
// ===========================================================================

type Scope = 'selected' | 'filtered' | 'top' | 'all'

/** 字数按服务端的算法：去掉首尾空白后的字符数（按码位，不按 UTF-16 的长度） */
const charCount = (s: string) => [...s.trim()].length

export function DraftDialog({
  sourceId, sourceDescription, guidance, rows, visible, selected, filtered, job, initial, initialBusy = false, stopping,
  onStart, onCancel, onRetry, onTrack, onGuidanceSaved, onClose,
}: {
  sourceId: string
  /** 数据源说明：用模型起草时连同起草要求一起交给模型 */
  sourceDescription: string
  /** 数据源存着的起草要求（没有为空串） */
  guidance: string
  /** 全部表（服务端的顺序：使用次数多的在前） */
  rows: CatalogTableRow[]
  /** 当前筛选结果 */
  visible: CatalogTableRow[]
  selected: string[]
  /** 清单上有没有生效的搜索或筛选：有时才给「当前筛选结果」这一项 */
  filtered: boolean
  /** 页面跟着的起草任务（进行中的、刚结束的） */
  job: CatalogDraftJob | null
  /** 打开时显示什么：setup 选范围、开始；job 显示页面跟着的那个任务 */
  initial: 'setup' | 'job'
  /** 打开时显示的任务是撞上已有任务（409）转过来的：写明 */
  initialBusy?: boolean
  /** 点了停止、服务端还没停下来 */
  stopping: boolean
  /** 建任务；409 已有任务时页面改取进行中的那一个（busy 为真） */
  onStart: (body: DraftStartBody) => Promise<{ job: CatalogDraftJob; busy: boolean }>
  onCancel: () => Promise<void>
  onRetry: (from: CatalogDraftJob) => Promise<{ job: CatalogDraftJob; busy: boolean }>
  /** 设置阶段点开了上一次起草的结果（或者发现别处建的进行中的任务）：页面改跟这个任务 */
  onTrack: (job: CatalogDraftJob) => void
  /** 起草要求存好了：页面记下新的这一份 */
  onGuidanceSaved: (guidance: string | null) => void
  onClose: () => void
}) {
  // 表结构里已经没有的表起草不了（服务端会记「已跳过」），不算进范围
  const live = useMemo(() => rows.filter((r) => r.in_schema).map((r) => r.table_name), [rows])
  const liveSet = useMemo(() => new Set(live), [live])
  const scopes = useMemo(() => {
    const out: { key: Scope; label: string; tables: string[] }[] = []
    const sel = selected.filter((t) => liveSet.has(t))
    if (sel.length) out.push({ key: 'selected', label: CT.scopeSelected(sel.length), tables: sel })
    const vis = visible.filter((r) => r.in_schema).map((r) => r.table_name)
    if (filtered && vis.length && vis.length !== live.length) out.push({ key: 'filtered', label: CT.scopeFiltered(vis.length), tables: vis })
    if (live.length > DRAFT_TOP_N) out.push({ key: 'top', label: CT.scopeTop(DRAFT_TOP_N), tables: live.slice(0, DRAFT_TOP_N) })
    out.push({ key: 'all', label: CT.scopeAll(live.length), tables: live })
    return out
  }, [selected, visible, filtered, live, liveSet])
  const [scope, setScope] = useState<Scope>(() => scopes[0].key)
  const tables = scopes.find((s) => s.key === scope)?.tables ?? scopes[0].tables
  const tooMany = tables.length > DRAFT_MAX_TABLES
  const [useModel, setUseModel] = useState(false)
  const [guidanceText, setGuidanceText] = useState(guidance)
  const [note, setNote] = useState('')
  const guidanceLen = charCount(guidanceText)
  const noteLen = charCount(note)
  const guidanceOver = guidanceLen > DRAFT_GUIDANCE_MAX
  const noteOver = noteLen > DRAFT_NOTE_MAX
  const guidanceChanged = guidanceText.trim() !== guidance.trim()

  const [mode, setMode] = useState<'setup' | 'job'>(initial === 'job' && job ? 'job' : 'setup')
  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState<string | null>(null)
  /** 是 409（已有任务）转过来显示的那一个 */
  const [busy, setBusy] = useState(initialBusy)
  const [acting, setActing] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  /** 上一次起草（没写全的才提），设置阶段写一行、可以点开 */
  const [last, setLast] = useState<CatalogDraftJob | null>(null)
  /** 起草模型的说法：设置里的起草模型，没配就是跟随助手的模型 */
  const [modelLine, setModelLine] = useState<{ text: string; warn: string | null } | null>(null)
  const onTrackRef = useRef(onTrack)
  onTrackRef.current = onTrack

  const inSetup = mode === 'setup' || !job
  // 设置阶段：取上一次起草。别处（另一个标签页）建的任务还在进行：直接显示它
  useEffect(() => {
    if (!inSetup) return
    let alive = true
    api.dataCatalog.latestDraftJob(sourceId).then(({ job: latest }) => {
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

  // 起草模型：设置里的 catalog_draft 填了就是它；没填跟随助手的模型（写出当前是哪个，用不了时写原因）
  useEffect(() => {
    let alive = true
    void Promise.allSettled([api.settings.get(), api.copilot.getModel()]).then(([s, c]) => {
      if (!alive) return
      const cfg = s.status === 'fulfilled' ? s.value?.catalog_draft : null
      const provider = typeof cfg?.provider === 'string' && cfg.provider.trim() ? cfg.provider.trim() : null
      const model = typeof cfg?.model === 'string' && cfg.model.trim() ? cfg.model.trim() : null
      if (provider || model) {
        setModelLine({ text: DT.modelNamed(model, provider), warn: null })
        return
      }
      const assistant = c.status === 'fulfilled' ? c.value : null
      setModelLine({ text: DT.modelFollow(assistant?.effective_model ?? null), warn: assistant?.error ?? null })
    })
    return () => { alive = false }
  }, [])

  const begin = async () => {
    if (starting || !tables.length || tooMany || (useModel && (guidanceOver || noteOver))) return
    setStarting(true)
    setStartError(null)
    try {
      // 起草要求改过：先存（存不上就不开始，免得模型拿着旧的那份起草）
      if (useModel && guidanceChanged) {
        try {
          const saved = await api.dataCatalog.putGuidance(sourceId, guidanceText.trim())
          onGuidanceSaved(saved.guidance)
        } catch (e) {
          setStartError(DT.saveGuidanceFailed(errorMessage(e)))
          return
        }
      }
      const extra = useModel && note.trim() ? { note: note.trim() } : {}
      const r = await onStart({ tables, use_model: useModel, ...extra })
      setBusy(r.busy)
      setMode('job')
    } catch (e) {
      // 起草模型用不了（没有接入、已停用、只有演示接入）：服务端那句话已经写了原因和去处
      setStartError(e instanceof ApiError && e.code === DRAFT_MODEL_UNAVAILABLE ? e.message : DT.startFailed(errorMessage(e)))
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
  const retry = (from: CatalogDraftJob) => act(async () => {
    const r = await onRetry(from)
    setBusy(r.busy)
  })
  const openLast = (j: CatalogDraftJob) => {
    onTrack(j)
    setBusy(false)
    setMode('job')
  }

  const phase: 'setup' | 'running' | 'done' = inSetup ? 'setup' : isJobActive(job) ? 'running' : 'done'
  const canStart = !!tables.length && !tooMany && !(useModel && (guidanceOver || noteOver)) && !starting
  const retryable = job && phase === 'done' ? retryableCount(job) : 0

  const footer = phase === 'setup'
    ? (
      <>
        <button className="btn" onClick={onClose}>{CT.cancel}</button>
        <button className="btn btn-primary" onClick={() => void begin()} disabled={!canStart} data-draft-start="">
          {starting ? <Spinner size={11} /> : <Sparkles size={12} aria-hidden />} {CT.draftStart(tables.length)}
        </button>
      </>
    )
    : phase === 'running'
      ? (
        <>
          <button className="btn" onClick={onClose} title={DT.hideHint} data-draft-hide="">{DT.hide}</button>
          <button className="btn" onClick={() => void stop()} disabled={stopping || acting} data-draft-stop="">{DT.stop}</button>
        </>
      )
      : (
        <>
          {retryable > 0 && job && (
            <button className="btn" onClick={() => void retry(job)} disabled={acting} title={DT.retryHint} data-draft-retry="">
              {acting ? <Spinner size={11} /> : <RotateCcw size={11} aria-hidden />} {DT.retry(retryable)}
            </button>
          )}
          <button className="btn btn-primary" onClick={onClose} data-autofocus data-draft-close="">{CT.draftClose}</button>
        </>
      )

  return (
    <Modal open onClose={onClose} title={CT.draftTitle} width={600} footer={footer}
           dirty={phase === 'setup' && useModel && (guidanceChanged || !!note.trim())}>
      <div className="min-w-0 space-y-4" data-draft-dialog={phase}>
        {phase === 'setup' && (
          <>
            <fieldset>
              <legend className="label">{CT.draftScope}</legend>
              <div className="space-y-1.5" role="radiogroup" aria-label={CT.draftScope}>
                {scopes.map((s) => (
                  <label key={s.key} className={clsx('flex cursor-pointer items-center gap-2 rounded-md border px-3 py-2 text-xs',
                    scope === s.key ? 'border-[var(--accent)] bg-accent-soft' : 'hover:bg-hover')} data-draft-scope={s.key}>
                    <input type="radio" name="catalog-draft-scope" checked={scope === s.key} onChange={() => setScope(s.key)} />
                    <span>{s.label}</span>
                  </label>
                ))}
              </div>
              {tooMany && <p className="mt-1 text-2xs text-[var(--err)]" data-draft-too-many="">{DT.tooMany(DRAFT_MAX_TABLES)}</p>}
            </fieldset>
            <p className="text-2xs leading-relaxed text-faint">{CT.draftBase}</p>
            <div className="rounded-md border">
              <label className="flex cursor-pointer items-start gap-2 rounded-md px-3 py-2.5 text-xs hover:bg-hover" data-draft-model="">
                <input type="checkbox" className="mt-0.5" checked={useModel} onChange={(e) => setUseModel(e.target.checked)} />
                <span className="min-w-0">
                  <span className="font-medium">{CT.useModel}</span>
                  <span className="mt-0.5 block text-2xs leading-relaxed text-faint">{CT.useModelHint}</span>
                </span>
              </label>
              {useModel && (
                <div className="space-y-3 border-t px-3 py-3">
                  <div className="text-2xs leading-relaxed" data-draft-model-name="">
                    <span className="text-dim">{DT.model}：</span>
                    <span className="break-words font-medium">{modelLine ? modelLine.text : '…'}</span>
                    <span className="block text-faint">{DT.modelWhere}</span>
                    {modelLine?.warn && <span className="block text-[var(--warn)]" data-draft-model-warn="">{modelLine.warn}</span>}
                  </div>
                  <div data-draft-guidance="">
                    <Field label={DT.guidance} htmlFor="catalog-draft-guidance" hint={DT.guidanceHint}
                           error={guidanceOver ? DT.tooLong(DRAFT_GUIDANCE_MAX) : undefined}>
                      {(p) => (
                        <textarea {...p} className="field min-h-[4.5rem] resize-y text-xs leading-relaxed" rows={3} value={guidanceText}
                                  placeholder={DT.guidancePlaceholder} onChange={(e) => setGuidanceText(e.target.value)} />
                      )}
                    </Field>
                    <Counter n={guidanceLen} max={DRAFT_GUIDANCE_MAX} />
                  </div>
                  <div data-draft-note="">
                    <Field label={DT.note} htmlFor="catalog-draft-note" hint={DT.noteHint}
                           error={noteOver ? DT.tooLong(DRAFT_NOTE_MAX) : undefined}>
                      {(p) => (
                        <textarea {...p} className="field min-h-[3rem] resize-y text-xs leading-relaxed" rows={2} value={note}
                                  placeholder={DT.notePlaceholder} onChange={(e) => setNote(e.target.value)} />
                      )}
                    </Field>
                    <Counter n={noteLen} max={DRAFT_NOTE_MAX} />
                  </div>
                  {sourceDescription.trim() && (
                    <p className="break-words text-2xs leading-relaxed text-dim" data-draft-source-description="">
                      {DT.sourceDescription(sourceDescription.trim())}
                    </p>
                  )}
                  <p className="text-2xs leading-relaxed text-faint">{DT.guidanceRule}</p>
                </div>
              )}
            </div>
            {last && (
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-md border px-3 py-2 text-2xs text-dim" data-draft-last={last.status}>
                <TriangleAlert size={11} className="shrink-0" style={{ color: 'var(--warn)' }} aria-hidden />
                <span className="min-w-0 flex-1">{DT.last(DT.title[last.status], formatTime(last.finished_at ?? last.created_at))}</span>
                <button type="button" className="btn btn-sm" onClick={() => openLast(last)} data-draft-last-open="">{DT.lastOpen}</button>
              </div>
            )}
            {startError && <Notice tone="err" attr={{ 'data-draft-start-error': '' }}>{startError}</Notice>}
          </>
        )}

        {phase !== 'setup' && job && (
          <JobView key={job.id} job={job} running={phase === 'running'} stopping={stopping} busy={busy} />
        )}
        {phase !== 'setup' && actionError && <Notice tone="err" attr={{ 'data-draft-action-error': '' }}>{actionError}</Notice>}
      </div>
    </Modal>
  )
}

function Counter({ n, max }: { n: number; max: number }) {
  return (
    <div className={clsx('mt-0.5 text-right text-2xs tnum', n > max ? 'text-[var(--err)]' : 'text-faint')} aria-hidden>
      {DT.count(n, max)}
    </div>
  )
}

/** 任务的进度或结果：标题、总进度、合计、模型和用量、没写全的表、逐表列表 */
function JobView({ job, running, stopping, busy }: { job: CatalogDraftJob; running: boolean; stopping: boolean; busy: boolean }) {
  const problems = job.tasks.filter((t) => RETRYABLE.has(t.status))
  const [showAll, setShowAll] = useState(false)
  // 结束后有没写全的表时默认只列这些（几百张表里找失败的太费劲），可以展开全部
  const onlyProblems = !running && problems.length > 0 && !showAll
  const tasks = onlyProblems ? problems : job.tasks
  const title = running ? (stopping ? DT.stopping : DT.title[job.status]) : DT.title[job.status]
  const empty = nothingWritten(job)
  const summary = empty === 'model' ? CT.draftNothingWritten
    : empty === 'failed' ? CT.draftNothingWrittenFailed
      : empty === 'same' ? CT.draftNoChange
        : CT.draftSummary(job.added, job.updated, job.removed)
  const problemText = !running ? DT.problems({
    failed: job.tasks.filter((t) => t.status === 'failed').length,
    partial: job.tasks.filter((t) => t.status === 'partial').length,
    cancelled: job.tasks.filter((t) => t.status === 'cancelled').length,
    skipped: job.tasks.filter((t) => t.status === 'skipped').length,
  }) : ''
  // 进行中按已结束的表数走；结束以后写全部写出来的有几张（停止、失败的不算「完成」）
  const ok = job.tasks.filter((t) => t.status === 'ok').length
  const value = running ? job.done : ok
  const progress = running ? DT.progress(job.done, job.total) : DT.progressDone(ok, job.total)
  const tone = job.status === 'failed' ? 'var(--err)' : running ? 'var(--accent)' : problems.length ? 'var(--warn)' : 'var(--st-done)'
  const pct = job.total ? (value / job.total) * 100 : 0
  return (
    <div className="min-w-0 space-y-3">
      {busy && <Notice tone="info" attr={{ 'data-draft-busy': '' }}>{DT.busy}</Notice>}
      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs" role="status" aria-live="polite">
        {running && <Spinner size={13} />}
        <span className="font-medium" data-draft-title="">{title}</span>
        <span className="flex-1" />
        <span className="tnum text-2xs text-faint" data-draft-progress={`${job.done}/${job.total}`}>{progress}</span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-hover" role="progressbar" aria-label={progress}
           aria-valuemin={0} aria-valuemax={job.total} aria-valuenow={value}>
        <div className="h-full rounded-full transition-[width] duration-300" style={{ width: `${pct}%`, background: tone }} />
      </div>
      <p className="tnum text-sm" data-draft-summary="">{summary}</p>
      {(job.model || job.usage.calls > 0) && (
        <p className="flex flex-wrap gap-x-3 gap-y-0.5 text-2xs text-faint">
          {job.model && <span className="break-all" data-draft-model-used="">{DT.modelUsed(job.model)}</span>}
          {job.usage.calls > 0 && (
            <span className="tnum" data-draft-usage="">{DT.usage(job.usage.calls, job.usage.input_tokens, job.usage.output_tokens)}</span>
          )}
        </p>
      )}
      {running && <p className="text-2xs leading-relaxed text-faint">{DT.hideHint}</p>}
      {job.status === 'failed' && job.error && <Notice tone="err" attr={{ 'data-draft-job-error': '' }}>{DT.jobFailed(job.error)}</Notice>}
      {job.status === 'interrupted' && <Notice tone="warn" attr={{ 'data-draft-interrupted': '' }}>{DT.interrupted}</Notice>}
      {job.status === 'cancelled' && <p className="text-2xs text-dim" data-draft-cancelled="">{DT.cancelled}</p>}
      {problemText && (
        <p className="text-xs leading-relaxed" data-draft-problems="">{problemText}</p>
      )}
      <div>
        <div className="mb-1 flex items-center gap-2">
          <span className="text-2xs font-medium text-dim" id="catalog-draft-tasks-label">{DT.tasks}</span>
          <span className="flex-1" />
          {!running && problems.length > 0 && problems.length < job.tasks.length && (
            <button type="button" className="rounded px-1 text-2xs text-[var(--accent)] outline-none hover:underline focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
                    aria-pressed={showAll} onClick={() => setShowAll((v) => !v)} data-draft-show-all="">
              {showAll ? DT.showProblems : DT.showAll(job.tasks.length)}
            </button>
          )}
        </div>
        <ul className="max-h-64 divide-y divide-[var(--hairline)] overflow-y-auto overflow-x-hidden rounded-md border"
            aria-labelledby="catalog-draft-tasks-label" data-draft-tasks="">
          {tasks.map((t) => <TaskRow key={t.table_name} task={t} />)}
        </ul>
      </div>
    </div>
  )
}

const TASK_ICON: Record<CatalogDraftTaskStatus, { Icon: LucideIcon | null; color: string }> = {
  pending: { Icon: CircleDashed, color: 'var(--text-faint)' },
  running: { Icon: null, color: 'var(--accent)' },
  ok: { Icon: CircleCheck, color: 'var(--st-done)' },
  partial: { Icon: TriangleAlert, color: 'var(--warn)' },
  failed: { Icon: CircleX, color: 'var(--err)' },
  skipped: { Icon: CircleMinus, color: 'var(--text-faint)' },
  cancelled: { Icon: CircleSlash, color: 'var(--text-faint)' },
}

function TaskRow({ task: t }: { task: CatalogDraftTask }) {
  const { Icon, color } = TASK_ICON[t.status]
  const reason = taskReason(t)
  const parts = t.parts_total != null && t.parts_total > 1 && t.status !== 'pending' && t.status !== 'skipped'
  const changes = DT.taskAdded(t.added, t.updated)
  return (
    <li className="min-w-0 px-3 py-1.5" data-draft-task={t.table_name} data-status={t.status}>
      <div className="flex min-w-0 items-center gap-2 text-xs">
        {Icon ? <Icon size={12} className="shrink-0" style={{ color }} aria-hidden /> : <Spinner size={12} />}
        <span className="mono min-w-0 flex-1 truncate" title={t.table_name}>{t.table_name}</span>
        {parts && (
          <span className="tnum shrink-0 text-2xs text-faint" title={DT.partsHint} data-draft-task-parts="">
            {DT.parts(t.parts_done, t.parts_total!)}
          </span>
        )}
        {changes && (
          <span className="tnum shrink-0 text-2xs text-faint" title={DT.taskAddedHint(t.added, t.updated, t.removed)}>{changes}</span>
        )}
        <span className="shrink-0 text-2xs" style={{ color: t.status === 'pending' || t.status === 'skipped' || t.status === 'cancelled' ? undefined : color }}
              data-draft-task-status="">
          {DT.taskStatus[t.status]}
        </span>
      </div>
      {reason && (
        <p className="mt-0.5 break-words pl-5 text-2xs leading-relaxed text-dim" data-draft-task-reason="">{reason}</p>
      )}
    </li>
  )
}
