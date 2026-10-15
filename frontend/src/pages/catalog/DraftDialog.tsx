import { useEffect, useMemo, useRef, useState } from 'react'
import { CircleCheck, CircleDashed, CircleMinus, CircleSlash, CircleX, Info, RotateCcw, Sparkles, TriangleAlert } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import clsx from 'clsx'
import { ApiError, api } from '../../api/client'
import type { CatalogDraftJob, CatalogDraftTask, CatalogDraftTaskStatus, CatalogTableRow } from '../../types'
import { Field, Modal, Notice, Spinner } from '../../components/ui'
import { errorMessage } from '../../lib/errors'
import { formatTime } from '../../lib/format'
import { CATALOG_DRAFT_TEXT as DT, CATALOG_TEXT as CT, DRAFT_GUIDANCE_MAX, DRAFT_NOTE_MAX } from '../../lib/terms'
import {
  DRAFT_MAX_TABLES, DRAFT_MODEL_UNAVAILABLE, DRAFT_PART_COLUMNS, DRAFT_TOP_N, RETRYABLE, blankOf, chunksOf, isJobActive, jobBlank, needsLook,
  nothingWritten, retryableCount, taskReason, waitsForProfile, writtenOf,
} from './draftJob'
import type { DraftPreset, DraftStartBody } from './draftJob'

// ===========================================================================
// 起草数据目录。起草是服务端的后台任务：这里选范围、写起草要求和本次补充说明，建任务；之后显示页面跟着的那个任务
// （进行中的进度、结束后的结果），任务本身的状态在页面上（draftJob.ts），关掉对话框照常进行。
//
// 三段：设置（范围、用不用模型；用模型时写起草要求、补充说明，写明用哪个起草模型）→ 进行中（总进度、合计、调用接连
// 没写出来时的提醒、逐表的状态和宽表的段进度、没写全的原因、留空的列；可以停止、在后台继续）→ 结束（汇总、没写全的表
// 和原因、留空的列合计、只重试失败的表）。
// 已经写进目录的内容不回滚——起草只增改推断项，人工确认、驳回过的项服务端不会动。
//
// 留空的列（模型列出了、中文名和含义都没给）不算没写全，表照样是已完成；结果里写合计和办法——在起草要求里补充缩写和
// 术语的含义，再重新起草这几张表。「重新起草这几张表」回到设置：范围就是那几张、勾着用模型、焦点在起草要求，
// 补完再开始。表详情里的「补充起草要求」也是这样打开（范围是那一张，见 preset）。预先选好的范围里的表已经不在表结构
// 里时不退回别的范围：那一项照旧选着、写明原因，不能开始。
//
// 同一个数据源的起草和剖析按建立顺序排队：有剖析任务在进行时，设置里写明起草会排在它后面；任务排在剖析后面
// （waiting_for 为 profile）时写「排在剖析任务后面，剖析结束后自动开始」。
// ===========================================================================

type Scope = 'preset' | 'selected' | 'filtered' | 'top' | 'all'

/** 字数按服务端的算法：去掉首尾空白后的字符数（按码位，不按 UTF-16 的长度） */
const charCount = (s: string) => [...s.trim()].length

/** 起草要求输入框的 id：预先选好范围回到设置时焦点放在这里 */
const GUIDANCE_ID = 'catalog-draft-guidance'

/**
 * 建任务（开始、只重试失败的表）没建成怎么说。起草模型用不了（409，建任务和重试都会回）：服务端那句话已经写了原因和
 * 去处，照原样，长句也不拆成「标题：原因」；别的错误前面写明没有开始
 */
const startErrorText = (e: unknown) =>
  (e instanceof ApiError && e.code === DRAFT_MODEL_UNAVAILABLE ? e.message : DT.startFailed(errorMessage(e)))

export function DraftDialog({
  sourceId, sourceDescription, guidance, rows, visible, selected, filtered, job, initial, initialBusy = false, preset: initialPreset = null,
  stopping, profileActive = false, onStart, onCancel, onRetry, onTrack, onGuidanceSaved, onClose,
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
  /**
   * 打开设置时预先选好的范围（表详情里「补充起草要求」：只有那张表）。给了就选它；里面有可起草的表时勾上用模型、
   * 焦点放在起草要求上（打开它就是为了先补起草要求再起草）。一张可起草的都没有（已不在表结构里）时照旧选着它、
   * 写明原因、不能开始，也不勾用模型——不退回别的范围
   */
  preset?: DraftPreset | null
  /** 点了停止、服务端还没停下来 */
  stopping: boolean
  /** 同一个数据源有剖析任务在排队、进行中：起草会排在它后面 */
  profileActive?: boolean
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
  /** 预先选好的范围：打开时给的，或者结果里点了「重新起草这几张表」 */
  const [preset, setPreset] = useState<DraftPreset | null>(initialPreset)
  const scopes = useMemo(() => {
    const out: { key: Scope; label: string; tables: string[]; gone?: number }[] = []
    // 预先选好的范围总在第一项：里面的表已经不在表结构里的（打开表详情以后别处重新探查了结构）不起草、写明有几张；
    // 一张都不剩也留着这一项，不退回别的范围——以为只起草那一张，实际按别的范围用模型起草了
    if (preset) {
      const pre = preset.tables.filter((t) => liveSet.has(t))
      out.push({ key: 'preset', label: preset.label, tables: pre, gone: preset.tables.length - pre.length })
    }
    const sel = selected.filter((t) => liveSet.has(t))
    if (sel.length) out.push({ key: 'selected', label: CT.scopeSelected(sel.length), tables: sel })
    const vis = visible.filter((r) => r.in_schema).map((r) => r.table_name)
    if (filtered && vis.length && vis.length !== live.length) out.push({ key: 'filtered', label: CT.scopeFiltered(vis.length), tables: vis })
    if (live.length > DRAFT_TOP_N) out.push({ key: 'top', label: CT.scopeTop(DRAFT_TOP_N), tables: live.slice(0, DRAFT_TOP_N) })
    out.push({ key: 'all', label: CT.scopeAll(live.length), tables: live })
    return out
  }, [preset, selected, visible, filtered, live, liveSet])
  const [scope, setScope] = useState<Scope>(() => scopes[0].key)
  /** 起草的就是这一项：选的那一项不在了（所选、筛选变了）就是第一项，单选框也照它勾，看得见起草的是哪些 */
  const current = scopes.find((s) => s.key === scope) ?? scopes[0]
  const tables = current.tables
  const tooMany = tables.length > DRAFT_MAX_TABLES
  /** 打开时给的范围里有可起草的表：勾上用模型、焦点放进起草要求（打开它就是为了先补起草要求）；一张都没有就都不做 */
  const presetLive = !!initialPreset && initialPreset.tables.some((t) => liveSet.has(t))
  const [useModel, setUseModel] = useState(presetLive)
  /** 回到设置以后把焦点放进起草要求（预先选好范围的两种入口都是为了先补起草要求） */
  const [focusGuidance, setFocusGuidance] = useState(presetLive)
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
  /** 上一次起草（没写全、有留空列的才提），设置阶段写一行、可以点开 */
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
      } else if (latest.status !== 'succeeded' || jobBlank(latest) > 0) {
        setLast(latest)
      }
    }, () => {})
    return () => { alive = false }
  }, [inSetup, sourceId])

  // 预先选好范围回到设置（打开时给的、结果里点的）：焦点放进起草要求
  useEffect(() => {
    if (!focusGuidance || mode !== 'setup' || !useModel) return
    const el = document.getElementById(GUIDANCE_ID)
    if (!el) return
    el.focus()
    setFocusGuidance(false)
  }, [focusGuidance, mode, useModel])

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
      // 起草模型用不了（没有接入、已停用、只有演示接入）时留在这一步，写明原因
      setStartError(startErrorText(e))
    } finally {
      setStarting(false)
    }
  }

  const act = async (fn: () => Promise<void>, describe: (e: unknown) => string = errorMessage) => {
    setActing(true)
    setActionError(null)
    try {
      await fn()
    } catch (e) {
      setActionError(describe(e))
    } finally {
      setActing(false)
    }
  }
  const stop = () => act(onCancel)
  // 重试是新建一个任务：没建成（起草模型用不了、没有表结构……）和开始时同一个说法，留在结果里
  const retry = (from: CatalogDraftJob) => act(async () => {
    const r = await onRetry(from)
    setBusy(r.busy)
  }, startErrorText)
  const openLast = (j: CatalogDraftJob) => {
    onTrack(j)
    setBusy(false)
    setMode('job')
  }
  /**
   * 结果里「重新起草这几张表」（有留空列的表）：回到设置，范围就是这几张，勾上用模型，焦点放进起草要求——先补上
   * 缩写和术语的含义再开始。任务本身不动（页面照旧跟着它，开始新的起草才换）
   */
  const redraftBlank = (names: string[]) => {
    setPreset({ tables: names, label: DT.scopeBlank(names.length) })
    setScope('preset')
    setUseModel(true)
    setFocusGuidance(true)
    setStartError(null)
    setActionError(null)
    setBusy(false)
    setLast(null)
    setMode('setup')
  }
  /** 结果里有留空列、还在表结构里的表（「重新起草这几张表」的范围） */
  const blankTables = useMemo(
    () => (job ? job.tasks.filter((t) => blankOf(t) > 0 && liveSet.has(t.table_name)).map((t) => t.table_name) : []),
    [job, liveSet],
  )

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
                    current.key === s.key ? 'border-[var(--accent)] bg-accent-soft' : 'hover:bg-hover')} data-draft-scope={s.key}>
                    <input type="radio" name="catalog-draft-scope" checked={current.key === s.key} onChange={() => setScope(s.key)} />
                    <span className="min-w-0 break-words">
                      {s.label}
                      {!!s.gone && (
                        <span className={clsx('block text-2xs', s.tables.length ? 'text-faint' : 'text-[var(--warn)]')} data-draft-preset-missing="">
                          {DT.presetMissing(s.gone, s.gone + s.tables.length)}
                        </span>
                      )}
                    </span>
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
                    <Field label={DT.guidance} htmlFor={GUIDANCE_ID} hint={DT.guidanceHint}
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
              // 没写全的用警告色；都写完了、只是有留空列的是提示
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-md border px-3 py-2 text-2xs text-dim" data-draft-last={last.status}>
                {last.status === 'succeeded'
                  ? <Info size={11} className="shrink-0" style={{ color: 'var(--accent)' }} aria-hidden />
                  : <TriangleAlert size={11} className="shrink-0" style={{ color: 'var(--warn)' }} aria-hidden />}
                <span className="min-w-0 flex-1">
                  {DT.last(DT.lastState(DT.title[last.status], jobBlank(last)), formatTime(last.finished_at ?? last.created_at))}
                </span>
                <button type="button" className="btn btn-sm" onClick={() => openLast(last)} data-draft-last-open="">{DT.lastOpen}</button>
              </div>
            )}
            {profileActive && (
              <p className="flex items-start gap-1.5 text-2xs leading-relaxed text-dim" data-draft-queue="">
                <Info size={11} className="mt-px shrink-0" style={{ color: 'var(--accent)' }} aria-hidden /> <span>{DT.queueHint}</span>
              </p>
            )}
            {startError && <Notice tone="err" attr={{ 'data-draft-start-error': '' }}>{startError}</Notice>}
          </>
        )}

        {phase !== 'setup' && job && (
          <JobView key={job.id} job={job} running={phase === 'running'} stopping={stopping} busy={busy}
                   blankTables={blankTables} onRedraftBlank={redraftBlank} />
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

/**
 * 任务的进度或结果：标题、总进度、提醒（进行中）、合计、模型和用量（超时、没写出来的次数）、没写全的表、留空的列合计
 * （结束后）、逐表列表
 */
function JobView({ job, running, stopping, busy, blankTables, onRedraftBlank }: {
  job: CatalogDraftJob
  running: boolean
  stopping: boolean
  busy: boolean
  /** 有留空列、还在表结构里的表：结束后给「重新起草这几张表」 */
  blankTables: string[]
  onRedraftBlank: (tables: string[]) => void
}) {
  const problems = job.tasks.filter((t) => RETRYABLE.has(t.status))
  // 结束后默认只列要看的表：没写全的、有留空列的（几百张表里找它们太费劲），可以展开全部
  const attention = job.tasks.filter(needsLook)
  const blankCount = job.tasks.filter((t) => blankOf(t) > 0).length
  const [showAll, setShowAll] = useState(false)
  const onlyProblems = !running && attention.length > 0 && !showAll
  const tasks = onlyProblems ? attention : job.tasks
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
  const { usage } = job
  const timeouts = usage.timeouts ?? 0
  const failures = DT.usageFailures(timeouts, usage.failed_calls ?? 0)
  // 调用接连没写出来时服务端给的提醒：进行中写在进度条下面（提醒，不是错误）；好转后服务端清掉
  const notice = running ? job.notice?.trim() ?? '' : ''
  // 额度用完、超时多了，之后的段缩小了：进行中写一句，段数为什么变多看得明白
  const partColumns = running && job.use_model && job.part_columns != null && job.part_columns < DRAFT_PART_COLUMNS ? job.part_columns : null
  const blankTotal = jobBlank(job)
  // 停下来时已经写进了一部分段的表（宽表每写完一段就写进目录）：几张、合计几段
  const stopped = job.tasks.filter((t) => t.status === 'cancelled' && writtenOf(t) > 0)
  const stoppedParts = { tables: stopped.length, parts: stopped.reduce((n, t) => n + writtenOf(t), 0) }
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
      {waitsForProfile(job) && (
        <p className="flex items-start gap-1.5 text-2xs leading-relaxed text-dim" data-draft-waiting="profile">
          <Info size={11} className="mt-px shrink-0" style={{ color: 'var(--accent)' }} aria-hidden /> <span>{DT.waitingProfile}</span>
        </p>
      )}
      {notice && <Notice tone="warn" className="break-words" attr={{ 'data-draft-notice': '' }}>{notice}</Notice>}
      <p className="tnum text-sm" data-draft-summary="">{summary}</p>
      {(job.model || usage.calls > 0 || failures) && (
        <div className="space-y-0.5 text-2xs leading-relaxed text-faint">
          <p className="flex flex-wrap gap-x-3 gap-y-0.5" data-draft-usage-line="">
            {job.model && <span className="break-all" data-draft-model-used="">{DT.modelUsed(job.model)}</span>}
            {usage.calls > 0 && (
              <span className="tnum" data-draft-usage="">{DT.usage(usage.calls, usage.input_tokens, usage.output_tokens)}</span>
            )}
            {failures && (
              <span className="tnum text-[var(--warn)]" title={DT.usageFailuresHint} data-draft-usage-failures="">{failures}</span>
            )}
          </p>
          {timeouts > 0 && <p data-draft-timeout-note="">{DT.timeoutNote}</p>}
          {partColumns != null && <p data-draft-part-columns={partColumns}>{DT.partColumns(partColumns)}</p>}
        </div>
      )}
      {running && <p className="text-2xs leading-relaxed text-faint">{DT.hideHint}</p>}
      {job.status === 'failed' && job.error && <Notice tone="err" attr={{ 'data-draft-job-error': '' }}>{job.error}</Notice>}
      {job.status === 'interrupted' && <Notice tone="warn" attr={{ 'data-draft-interrupted': '' }}>{DT.interrupted}</Notice>}
      {job.status === 'cancelled' && (
        <p className="text-2xs leading-relaxed text-dim" data-draft-cancelled="">
          {DT.cancelled}
          {stoppedParts.tables > 0 && <span className="block">{DT.cancelledParts(stoppedParts.tables, stoppedParts.parts)}</span>}
        </p>
      )}
      {problemText && (
        <p className="text-xs leading-relaxed" data-draft-problems="">{problemText}</p>
      )}
      {!running && blankTotal > 0 && (
        // 留空的列不算没写全（表照样是已完成）：写合计和办法，给「重新起草这几张表」回到设置先补起草要求
        <Notice tone="info" attr={{ 'data-draft-blank-summary': '' }}>
          <p className="font-medium">{DT.blankTotal(blankTotal, blankCount)}</p>
          <p className="mt-0.5 text-dim">{DT.blankAdvice}</p>
          {blankTables.length > 0 && (
            <button type="button" className="btn btn-sm mt-2" title={DT.blankRedraftHint} onClick={() => onRedraftBlank(blankTables)}
                    data-draft-blank-redraft="">
              <Sparkles size={11} aria-hidden /> {DT.blankRedraft(blankTables.length)}
            </button>
          )}
        </Notice>
      )}
      <div>
        <div className="mb-1 flex items-center gap-2">
          <span className="text-2xs font-medium text-dim" id="catalog-draft-tasks-label">{DT.tasks}</span>
          <span className="flex-1" />
          {!running && attention.length > 0 && attention.length < job.tasks.length && (
            <button type="button" className="rounded px-1 text-2xs text-[var(--accent)] outline-none hover:underline focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
                    aria-pressed={showAll} onClick={() => setShowAll((v) => !v)} data-draft-show-all="">
              {showAll ? (blankCount ? DT.showProblemsBlank : DT.showProblems) : DT.showAll(job.tasks.length)}
            </button>
          )}
        </div>
        <ul className="max-h-64 divide-y divide-[var(--hairline)] overflow-y-auto overflow-x-hidden rounded-md border"
            aria-labelledby="catalog-draft-tasks-label" data-draft-tasks="">
          {tasks.map((t) => <TaskRow key={t.table_name} task={t} jobError={job.error} />)}
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

/**
 * 逐表的一行。jobError：任务整体的原因（整体出错、服务重启中断时服务端把没结束的表也记上同一句）。和它一样的不再逐表
 * 重复——几百张表各写一遍同一句，真正各有原因的那几张反而找不到；任务的原因已经写在上面（标题、提示）。
 * 有留空的列时再写一行「N 列模型拿不准、留空了」：列名悬停看得到，也可以展开（服务端最多给 5 个）
 */
function TaskRow({ task: t, jobError }: { task: CatalogDraftTask; jobError: string | null }) {
  const { Icon, color } = TASK_ICON[t.status]
  const reason = jobError && t.error === jobError ? (t.model_error ?? '') : taskReason(t)
  const started = t.status !== 'pending' && t.status !== 'skipped'
  const parts = t.parts_total != null && t.parts_total > 1 && started
  // 段被缩小、拆半后实际的块（比段多时才写）；已经写进目录的段：起草中、部分完成时写着（停止也不丢，部分完成的
  // 段进度是「都结束了」，看不出写进了几段），停下来时补一句剩下几段没起草。服务端的原因里已经写了的不重复，写完了的不写
  const chunks = started ? chunksOf(t) : null
  const written = writtenOf(t)
  const writtenText = written <= 0 || reason.includes(DT.partsWritten(written)) ? ''
    : t.status === 'running' || t.status === 'partial' ? DT.partsWritten(written)
      : t.status === 'cancelled' ? DT.partsWrittenStopped(written, Math.max(0, (t.parts_total ?? written) - written)) : ''
  const changes = DT.taskAdded(t.added, t.updated)
  const blank = blankOf(t)
  const sample = t.blank_sample?.filter(Boolean) ?? []
  // 起草时记下的几句话（服务端原话，不算失败）：没收下的有效记录条件和原因，照原样列在这张表下面
  const notes = t.log?.filter(Boolean) ?? []
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
        {chunks && (
          <span className="tnum shrink-0 text-2xs text-faint" title={DT.chunksHint(chunks.total)} data-draft-task-chunks="">
            {DT.chunks(chunks.done, chunks.total)}
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
      {writtenText && (
        <p className="mt-0.5 break-words pl-5 text-2xs leading-relaxed text-faint" title={DT.partsWrittenHint} data-draft-task-written="">
          {writtenText}
        </p>
      )}
      {reason && (
        <p className="mt-0.5 break-words pl-5 text-2xs leading-relaxed text-dim" data-draft-task-reason="">{reason}</p>
      )}
      {blank > 0 && (sample.length
        ? (
          <details className="mt-0.5 pl-5 text-2xs leading-relaxed text-dim" data-draft-task-blank={blank}>
            <summary className="cursor-pointer select-none hover:text-fg" title={DT.blankSample(sample, blank)}>{DT.blank(blank)}</summary>
            <p className="break-words text-faint" data-draft-task-blank-sample="">{DT.blankSample(sample, blank)}</p>
          </details>
        )
        : <p className="mt-0.5 pl-5 text-2xs leading-relaxed text-dim" data-draft-task-blank={blank}>{DT.blank(blank)}</p>)}
      {notes.map((line, i) => (
        <p key={i} className="mt-0.5 break-words pl-5 text-2xs leading-relaxed text-faint" data-draft-task-log="">{line}</p>
      ))}
    </li>
  )
}
