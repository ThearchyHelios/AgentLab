import { useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { AlertTriangle, ChevronRight, FileSpreadsheet, Info, Upload } from 'lucide-react'
import clsx from 'clsx'
import { ApiError, api } from '../../api/client'
import type { UploadProgress } from '../../api/client'
import type { CommitOut, DataSource, QuestionAnswer, Recipe, Staging } from '../../types'
import { Field, Modal, Spinner, confirmDialog, toast } from '../../components/ui'
import { localActor } from '../../lib/actor'
import { errorMessage } from '../../lib/errors'
import { formatBytes, formatDuration, formatNumber } from '../../lib/format'
import { RECIPE_TEXT, UPLOAD_TEXT } from '../../lib/terms'
import { useRunClock } from '../../run/useRunClock'
import { UploadMeter } from '../DataSourcesTab'
import { AiDraftOffer } from './AiDraftDialog'
import { ConfirmList } from './ConfirmList'
import type { CommitBody } from './ConfirmList'
import { ImportReceipt, ProblemList, TrialStatusLine } from './ImportReceipt'
import { RecipePanel } from './RecipePanel'
import { ReuploadDiff } from './ReuploadDiff'
import { SheetGrid, parseRef } from './SheetGrid'
import type { GridFocus } from './SheetGrid'
import { SuggestionCards } from './SuggestionCards'

// ===========================================================================
// 按配方导入的向导：选文件 → 起草（网格 + 建议 + 配方面板）→ 试运行 → 逐条确认 → 启用。
// 进度都在服务端的暂存区里：关掉向导不丢，从卡片上的「有未完成的导入」继续；提交成功之前
// 不动数据源的当前版本
// ===========================================================================

/** 从哪进来的 */
export type WizardEntry =
  /** 表格标签页头部：在向导里选文件、填名字 */
  | { kind: 'new' }
  /** 上传弹窗的交叉表决定页：带着同一个文件、名字和说明直接开始 */
  | { kind: 'stage'; file: File; name: string; description: string }
  /** 卡片「上传新一期」：选文件后按当前配方试运行 */
  | { kind: 'reupload'; source: DataSource }
  /** 卡片「修改配方」：不换文件 */
  | { kind: 'redraft'; source: DataSource }
  /** 卡片「有未完成的导入」 */
  | { kind: 'resume'; stagingId: string; source?: DataSource }

type Phase = 'pick' | 'uploading' | 'loading' | 'work' | 'confirm' | 'done' | 'failed'
type View = 'draft' | 'result'

const NAME_RE = /^[a-z][a-z0-9_]{0,40}$/
/** 选择框收的扩展名，与 RECIPE_TEXT.pickFormats 的说法一致 */
const PICK_ACCEPT = '.xlsx,.xlsm'
const ENDED = new Set(['committed', 'discarded', 'expired'])

/** 文件名里的两个日期（「月报导出_2026-09-01_2026-09-30.xlsx」）：只作统计期录入框的建议，服务端会再核对 */
function periodFromFileName(name: string): { start: string; end: string } | null {
  const re = /(\d{4})\s*[-./年]\s*(\d{1,2})\s*[-./月]\s*(\d{1,2})\s*日?/g
  const found: string[] = []
  for (const m of name.matchAll(re)) {
    const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])]
    if (mo < 1 || mo > 12 || d < 1 || d > 31) continue
    found.push(`${y}-${String(mo).padStart(2, '0')}-${String(d).padStart(2, '0')}`)
  }
  return found.length >= 2 && found[0] <= found[1] ? { start: found[0], end: found[1] } : null
}

/**
 * 标签候选：网格里标签列（有行标签、合计标签、分段标题的那几列）的文字，去掉已知是统计期、区域外文字、
 * 表头的格；还没有干跑过（没有区域标记）就取全部文字格
 */
function labelOptionsOf(staging: Staging | null): string[] {
  if (!staging) return []
  const LABEL_ROLES = ['row_label', 'derived_label', 'section_title', 'total_label']
  const out = new Set<string>()
  for (const grid of staging.grids ?? []) {
    const cols = new Set<number>()
    const other = new Set<string>()
    for (const m of staging.marks ?? []) {
      if (m.sheet !== grid.sheet) continue
      const range = parseRef(m.ref)
      if (!range) continue
      if (LABEL_ROLES.includes(m.role)) {
        for (let c = range.c1; c <= range.c2; c++) cols.add(c)
      } else if (range.c1 === range.c2 || range.r1 === range.r2) {
        // 标签列上的统计期、区域外文字、表头格：只可能是单格或一行一列的小区域，逐格记下排除
        for (let r = range.r1; r <= Math.min(range.r2, range.r1 + 2000); r++) {
          for (let c = range.c1; c <= Math.min(range.c2, range.c1 + 200); c++) other.add(`${r},${c}`)
        }
      }
    }
    for (const [r, c, text, kind] of grid.cells) {
      if (kind !== 'text' || !String(text).trim()) continue
      if (cols.size && (!cols.has(c) || other.has(`${r},${c}`))) continue
      out.add(String(text).trim())
    }
  }
  return [...out].slice(0, 300)
}

/** 回答原样发回：值，加上需要理由的选项的理由 */
const asAnswer = (a: QuestionAnswer): QuestionAnswer => (a.reason ? { value: a.value, reason: a.reason } : { value: a.value })

const STEP_KEYS = ['pick', 'draft', 'trial', 'confirm', 'done'] as const

function Steps({ current, reupload }: { current: (typeof STEP_KEYS)[number]; reupload: boolean }) {
  const keys = reupload && current !== 'draft' ? STEP_KEYS.filter((k) => k !== 'draft') : STEP_KEYS
  const at = keys.indexOf(current as never)
  return (
    <ol className="flex flex-wrap items-center gap-1 text-2xs" data-wizard-steps aria-label={RECIPE_TEXT.titleFirst}>
      {keys.map((k, i) => (
        <li key={k} className="flex items-center gap-1" data-wizard-step={k} aria-current={k === current ? 'step' : undefined}>
          {i > 0 && <ChevronRight size={10} className="text-faint" aria-hidden />}
          <span className={clsx('rounded px-1.5 py-0.5',
            k === current ? 'bg-accent-soft text-fg' : i < at ? 'text-dim' : 'text-faint')}>
            {RECIPE_TEXT.steps[k]}
          </span>
        </li>
      ))}
    </ol>
  )
}

function Notice({ tone, children, attr }: { tone: 'warn' | 'err' | 'info'; children: ReactNode; attr?: Record<string, string> }) {
  const color = tone === 'err' ? 'var(--err)' : tone === 'warn' ? 'var(--warn)' : 'var(--accent)'
  return (
    <div role={tone === 'info' ? 'status' : 'alert'} className="flex gap-2 rounded-lg border px-3 py-2 text-xs leading-relaxed" {...attr}
         style={{ borderColor: `color-mix(in srgb, ${color} 40%, var(--border))`, background: `color-mix(in srgb, ${color} 7%, transparent)` }}>
      {tone === 'info'
        ? <Info size={13} className="mt-0.5 shrink-0" style={{ color }} aria-hidden />
        : <AlertTriangle size={13} className="mt-0.5 shrink-0" style={{ color }} aria-hidden />}
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  )
}

/** 统计期录入：两个日期框，文件名里的区间作建议。日期是否合法、起点是否晚于终点，服务端还会再查 */
function PeriodForm({ fileName, busy, error, onSubmit }: {
  fileName: string; busy: boolean; error: string | null; onSubmit: (start: string, end: string) => void
}) {
  const suggest = useMemo(() => periodFromFileName(fileName), [fileName])
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const valid = !!start && !!end && start <= end
  return (
    <div className="space-y-2 rounded-lg border bg-bg px-3 py-2.5" data-period-form>
      {suggest && (
        <div className="flex flex-wrap items-center gap-2 text-2xs text-dim" data-period-suggest>
          {RECIPE_TEXT.periodSuggest(suggest.start, suggest.end)}
          <button type="button" className="btn btn-xs" onClick={() => { setStart(suggest.start); setEnd(suggest.end) }}>
            {RECIPE_TEXT.periodUseSuggest}
          </button>
        </div>
      )}
      <div className="grid grid-cols-2 gap-2">
        <Field label={RECIPE_TEXT.periodStart}>
          {(p) => <input {...p} type="date" className="field" value={start} data-period-start onChange={(e) => setStart(e.target.value)} />}
        </Field>
        <Field label={RECIPE_TEXT.periodEnd}>
          {(p) => <input {...p} type="date" className="field" value={end} data-period-end onChange={(e) => setEnd(e.target.value)} />}
        </Field>
      </div>
      {error && <div className="text-2xs text-[var(--err)]" role="alert">{error}</div>}
      {!valid && (start || end) && <div className="text-2xs text-faint">{RECIPE_TEXT.periodInvalid}</div>}
      <button type="button" className="btn btn-sm btn-primary" disabled={!valid || busy} onClick={() => onSubmit(start, end)}>
        {busy ? <Spinner size={11} /> : null} {RECIPE_TEXT.periodSubmit}
      </button>
    </div>
  )
}

export function ImportWizard({ entry, taken = [], onClose, onCommitted, onChanged }: {
  entry: WizardEntry
  /** 已被数据库占用的名字（新导入不能用） */
  taken?: string[]
  onClose: () => void
  /** 启用成功：新建或更新后的数据源 */
  onCommitted: (source: DataSource) => void
  /** 暂存区开始、放弃、结束：卡片上的「有未完成的导入」要跟着变 */
  onChanged?: () => void
}) {
  const source = entry.kind === 'reupload' || entry.kind === 'redraft' ? entry.source : entry.kind === 'resume' ? entry.source : undefined
  const reuploadEntry = entry.kind === 'reupload'
  const [phase, setPhase] = useState<Phase>(entry.kind === 'new' || entry.kind === 'reupload' ? 'pick' : entry.kind === 'stage' ? 'uploading' : 'loading')
  const [view, setView] = useState<View>('draft')
  const [staging, setStaging] = useState<Staging | null>(null)
  const [busy, setBusy] = useState('')
  const [notice, setNotice] = useState<{ tone: 'warn' | 'err' | 'info'; text: string; detail?: string } | null>(null)
  const [failure, setFailure] = useState<string | null>(null)
  const [focus, setFocus] = useState<GridFocus | null>(null)
  const [drawer, setDrawer] = useState(false)
  const [missing, setMissing] = useState<Set<string>>(new Set())
  const [confirmError, setConfirmError] = useState<string | null>(null)
  const [periodError, setPeriodError] = useState<string | null>(null)
  const [done, setDone] = useState<CommitOut | null>(null)
  // 选文件
  const [file, setFile] = useState<File | null>(entry.kind === 'stage' ? entry.file : null)
  const [name, setName] = useState(entry.kind === 'stage' ? entry.name : '')
  const [description, setDescription] = useState(entry.kind === 'stage' ? entry.description : '')
  const [nameRejected, setNameRejected] = useState<{ name: string; message: string } | null>(null)
  const [progress, setProgress] = useState<{ p: UploadProgress; sentAt?: number } | null>(null)
  const [dragging, setDragging] = useState(false)
  const upload = useRef<{ abort: () => void; sent: boolean } | null>(null)
  const startedAt = useRef(0)
  const clock = useRunClock(phase === 'uploading' || !!busy)
  const started = useRef(false)

  const focusCell = (cell: string) => setFocus({ cell, seq: Date.now() })

  /** 暂存区已结束（过期或已放弃）：说清楚、关掉向导。返回 true 表示处理了 */
  const guard = (e: unknown): boolean => {
    if (e instanceof ApiError && e.code === 'staging_closed') {
      toast.warn(RECIPE_TEXT.closed)
      onChanged?.()
      onClose()
      return true
    }
    return false
  }

  const enter = (s: Staging) => {
    if (ENDED.has(s.status)) {
      toast.warn(RECIPE_TEXT.closed)
      onChanged?.()
      onClose()
      return
    }
    setStaging(s)
    setView((s.status === 'trialed' || s.status === 'rejected') && s.trial ? 'result' : 'draft')
    setPhase('work')
  }

  const refetch = async (id: string) => {
    try { setStaging(await api.tableImports.get(id)) } catch (e) { if (!guard(e)) toast.error(e) }
  }

  /** 选中文件（点选或拖入）：新导入拿文件名当默认数据源名，只留 ASCII；中文文件名清洗完可能什么都不剩，那就让用户自己填 */
  const pickFile = (f: File | null) => {
    // 拖进来的文件不经过选择框的 accept：不是 Excel 的在这里挡下，免得传完才被服务端拒收
    if (f && !PICK_ACCEPT.split(',').some((ext) => f.name.toLowerCase().endsWith(ext))) {
      toast.warn(RECIPE_TEXT.pickWrongType(f.name))
      return
    }
    setFile(f)
    if (f && !name && !reuploadEntry) {
      const slug = f.name.replace(/\.[^.]+$/, '').toLowerCase().replace(/[^a-z0-9_]+/g, '_').replace(/^_+|_+$/g, '')
      setName(/^[a-z]/.test(slug) ? slug.slice(0, 40) : '')
    }
  }

  // ---- 开始：上传 / 读取
  const sendFile = async () => {
    if (!file) return
    const isReupload = reuploadEntry && source
    if (!isReupload && (!name || !NAME_RE.test(name))) return
    const ctl = new AbortController()
    const handle = { abort: () => ctl.abort(), sent: false }
    upload.current = handle
    startedAt.current = Date.now()
    setProgress(null)
    setPhase('uploading')
    const opts = {
      signal: ctl.signal,
      onProgress: (p: UploadProgress) => {
        if (p.sent) handle.sent = true
        setProgress((cur) => ({ p, sentAt: cur?.sentAt ?? (p.sent ? Date.now() : undefined) }))
      },
    }
    try {
      const s = isReupload
        ? await api.tableImports.reupload(source.id, file, opts)
        : await api.tableImports.stage(file, { name, description }, opts)
      onChanged?.()
      enter(s)
    } catch (e) {
      setPhase('pick')
      if (e instanceof DOMException && e.name === 'AbortError') { toast.info(UPLOAD_TEXT.cancelHint); return }
      const code = e instanceof ApiError ? e.code : undefined
      if (code && ['name_taken', 'name_invalid', 'recipe_source_exists'].includes(code)) setNameRejected({ name, message: errorMessage(e) })
      else toast.error(e)
    } finally {
      if (upload.current === handle) upload.current = null
      setProgress(null)
    }
  }

  useEffect(() => {
    if (started.current) return
    started.current = true
    if (entry.kind === 'stage') void sendFile()
    if (entry.kind === 'redraft') {
      void (async () => {
        try {
          enter(await api.tableImports.redraft(entry.source.id))
          onChanged?.()
        } catch (e) {
          setFailure(e instanceof ApiError && e.code === 'raw_missing' ? `${errorMessage(e)}（${RECIPE_TEXT.rawMissing}）` : errorMessage(e))
          setPhase('failed')
        }
      })()
    }
    if (entry.kind === 'resume') {
      void (async () => {
        try { enter(await api.tableImports.get(entry.stagingId)) } catch (e) { setFailure(errorMessage(e)); setPhase('failed') }
      })()
    }
  }, [])

  const close = () => {
    if (upload.current && !upload.current.sent) upload.current.abort()
    if (staging) onChanged?.()
    onClose()
  }

  // ---- 起草：回答、改配方、AI、试运行
  const answer = async (qid: string, a: QuestionAnswer): Promise<boolean> => {
    if (!staging) return false
    const answers: Record<string, QuestionAnswer> = {}
    for (const [k, v] of Object.entries(staging.answers ?? {})) answers[k] = asAnswer(v)
    answers[qid] = asAnswer(a)
    setBusy('answer')
    try {
      setStaging(await api.tableImports.answers(staging.id, answers))
      return true
    } catch (e) {
      if (!guard(e)) toast.error(e)
      return false
    } finally {
      setBusy('')
    }
  }

  /** 存上了返回服务端存下的配方：配方面板以它为底接着发排在后面的修改 */
  const saveRecipe = async (recipe: Recipe): Promise<{ recipe: Recipe | null } | null> => {
    if (!staging) return null
    setBusy('recipe')
    try {
      const s = await api.tableImports.putRecipe(staging.id, recipe)
      setStaging(s)
      return { recipe: s.recipe }
    } catch (e) {
      if (!guard(e)) toast.error(e)
      return null
    } finally {
      setBusy('')
    }
  }

  const trial = async (context?: { start: string; end: string }) => {
    if (!staging) return
    setBusy('trial')
    setNotice(null)
    setPeriodError(null)
    try {
      const s = await api.tableImports.trial(staging.id, {
        ...(context ? { context_inputs: { 统计期: context } } : {}), signed_by: localActor(),
      })
      setStaging(s)
      setView('result')
      setMissing(new Set())
      setConfirmError(null)
    } catch (e) {
      if (guard(e)) return
      const code = e instanceof ApiError ? e.code : undefined
      if (code === 'context_invalid' || code === 'context_not_needed') setPeriodError(errorMessage(e))
      else toast.error(e)
    } finally {
      setBusy('')
    }
  }

  // ---- 提交
  const commit = async (body: CommitBody) => {
    if (!staging?.trial) return
    setBusy('commit')
    setConfirmError(null)
    try {
      const out = await api.tableImports.commit(staging.id, { trial_id: staging.trial.trial_id, ...body })
      setDone(out)
      setPhase('done')
      onCommitted(out.source)
    } catch (e) {
      if (guard(e)) return
      const code = e instanceof ApiError ? e.code : undefined
      const message = errorMessage(e)
      if (code === 'confirm_required' || code === 'acceptance_required') {
        // 服务端在 detail 里点名缺的项：按 id 或文案认出来标红
        const ids = new Set<string>()
        for (const it of staging.trial.confirm_items ?? []) if (message.includes(it.id) || message.includes(it.label)) ids.add(it.id)
        for (const id of staging.trial.acceptable ?? []) if (message.includes(id)) ids.add(id)
        setMissing(ids)
        setConfirmError(message)
      } else if (code === 'trial_required' || code === 'trial_tampered' || code === 'base_changed' || code === 'name_taken') {
        // 试运行库已被消耗或作废：回到配方步骤重新试运行
        const text = code === 'base_changed' ? RECIPE_TEXT.baseChanged : code === 'name_taken' ? RECIPE_TEXT.nameTaken : RECIPE_TEXT.rerunTrial
        setNotice({ tone: 'warn', text, detail: message })
        setPhase('work')
        setView('draft')
        await refetch(staging.id)
      } else if (code === 'trial_not_passed') {
        setNotice({ tone: 'warn', text: message })
        setPhase('work')
        setView('result')
      } else {
        setConfirmError(message)
      }
    } finally {
      setBusy('')
    }
  }

  const discard = async () => {
    if (!staging) { close(); return }
    const ok = await confirmDialog({
      title: RECIPE_TEXT.discardTitle, danger: true, consequences: [...RECIPE_TEXT.discardConsequences], confirmLabel: RECIPE_TEXT.discard,
    })
    if (!ok) return
    setBusy('discard')
    try {
      await api.tableImports.discard(staging.id)
      toast.ok(RECIPE_TEXT.discarded)
      onChanged?.()
      onClose()
    } catch (e) {
      if (!guard(e)) toast.error(e)
    } finally {
      setBusy('')
    }
  }

  // ---- 标题
  const srcName = staging?.source?.name || source?.name || name
  const kind = staging?.kind ?? (entry.kind === 'reupload' ? 'reupload' : entry.kind === 'redraft' ? 'redraft' : 'first')
  const isReupload = kind === 'reupload'
  const title = kind === 'reupload' ? RECIPE_TEXT.titleReupload(srcName)
    : kind === 'redraft' ? RECIPE_TEXT.titleRedraft(srcName)
      : srcName ? RECIPE_TEXT.titleNamed(srcName) : RECIPE_TEXT.titleFirst
  const step: (typeof STEP_KEYS)[number] = phase === 'pick' || phase === 'uploading' ? 'pick'
    : phase === 'confirm' ? 'confirm' : phase === 'done' ? 'done' : view === 'result' ? 'trial' : 'draft'

  const labelOptions = useMemo(() => labelOptionsOf(staging), [staging])
  const t = staging?.trial ?? null
  const trialStale = !!t && staging?.status === 'drafting'
  const canTrial = !!staging?.recipe && !staging.recipe_problems?.length && !busy

  // ---- 正文
  let body: ReactNode = null
  let footer: ReactNode = null

  if (phase === 'pick' || phase === 'uploading') {
    const nameError = reuploadEntry ? null
      : name && !NAME_RE.test(name) ? RECIPE_TEXT.nameInvalid
        : taken.includes(name) ? RECIPE_TEXT.nameTakenDb(name)
          : nameRejected && nameRejected.name === name ? nameRejected.message : null
    const uploading = phase === 'uploading'
    const sent = !!progress?.p.sent
    body = (
      <div className="space-y-3" data-wizard-pick>
        {reuploadEntry && <p className="text-xs leading-relaxed text-dim">{RECIPE_TEXT.pickReupload}</p>}
        {/* 同上传弹窗：拖进来的文件在这里接住（不接的话浏览器会直接打开或下载这个文件） */}
        <label className={clsx('flex flex-col items-center justify-center gap-1.5 rounded-lg border border-dashed px-4 py-5 text-center transition-colors',
          uploading ? 'opacity-60' : 'cursor-pointer',
          dragging ? 'border-[var(--accent)] bg-accent-soft' : !uploading && 'hover:border-[var(--border-strong)] hover:bg-hover')}
               data-wizard-drop
               onDragOver={(e) => { e.preventDefault(); if (!uploading) setDragging(true) }}
               onDragLeave={() => setDragging(false)}
               onDrop={(e) => { e.preventDefault(); setDragging(false); if (!uploading) pickFile(e.dataTransfer.files?.[0] ?? null) }}>
          <FileSpreadsheet size={20} className={file ? 'text-[var(--accent)]' : 'text-faint'} aria-hidden />
          {file
            ? <span className="text-xs" data-wizard-file><span className="mono">{file.name}</span> <span className="tnum text-faint">· {formatBytes(file.size)}</span></span>
            : <span className="text-xs text-dim">{RECIPE_TEXT.pickDrop}</span>}
          <span className="text-2xs text-faint">{RECIPE_TEXT.pickFormats}</span>
          <input type="file" className="sr-only" accept={PICK_ACCEPT} aria-label={RECIPE_TEXT.pickAria} disabled={uploading}
                 onChange={(e) => pickFile(e.target.files?.[0] ?? null)} />
        </label>
        <p className="flex gap-1.5 text-2xs leading-relaxed text-faint" data-raw-notice>
          <Info size={11} className="mt-0.5 shrink-0" aria-hidden />
          <span>{UPLOAD_TEXT.rawNotice}</span>
        </p>
        {uploading && (
          <div className="rounded-lg border bg-bg px-3 py-2 text-2xs" data-upload-progress>
            <UploadMeter progress={progress?.p ?? null} startedAt={startedAt.current || Date.now()} sentAt={progress?.sentAt}
                         processing={reuploadEntry ? RECIPE_TEXT.reuploading : RECIPE_TEXT.staging} now={clock} />
          </div>
        )}
        {!reuploadEntry && (
          <>
            <Field label={RECIPE_TEXT.nameLabel} required error={nameError} hint={RECIPE_TEXT.nameHint}>
              {(p) => (
                <input {...p} className="field mono" value={name} placeholder="passenger_flow" autoComplete="off" spellCheck={false}
                       disabled={uploading} onChange={(e) => setName(e.target.value)} data-wizard-name />
              )}
            </Field>
            <Field label={RECIPE_TEXT.descriptionLabel}>
              {(p) => <input {...p} className="field" value={description} disabled={uploading} onChange={(e) => setDescription(e.target.value)} />}
            </Field>
          </>
        )}
      </div>
    )
    footer = (
      <>
        {uploading && !sent
          ? <button className="btn" onClick={() => upload.current?.abort()}>{RECIPE_TEXT.cancelUpload}</button>
          : <button className="btn" onClick={close} disabled={uploading}>{RECIPE_TEXT.cancel}</button>}
        <button className="btn btn-primary tnum" disabled={uploading || !file || (!reuploadEntry && (!name || !!nameError))}
                onClick={() => void sendFile()} data-wizard-start>
          {uploading
            ? <><Spinner size={11} /> {formatDuration(clock - startedAt.current)}</>
            : <><Upload size={12} aria-hidden /> {reuploadEntry ? RECIPE_TEXT.reupload : RECIPE_TEXT.entry}</>}
        </button>
      </>
    )
  } else if (phase === 'loading') {
    body = <div className="flex items-center justify-center gap-2 py-12 text-xs text-dim"><Spinner size={12} /> {RECIPE_TEXT.loading}</div>
  } else if (phase === 'failed') {
    body = <Notice tone="err" attr={{ 'data-wizard-failed': '' }}>{failure}</Notice>
    footer = <button className="btn" onClick={close}>{RECIPE_TEXT.close}</button>
  } else if (phase === 'done' && done) {
    const seq = t?.same_as_import?.seq
    body = (
      <div className="space-y-2 py-4 text-center" data-import-done={done.unchanged ? 'unchanged' : 'committed'}>
        <div className="text-sm font-medium">{RECIPE_TEXT.done(done.source?.name ?? srcName)}</div>
        {done.unchanged && seq != null && <p className="text-xs text-dim">{RECIPE_TEXT.doneUnchanged(formatNumber(seq))}</p>}
        {!done.unchanged && done.build_reused && <p className="text-xs text-faint">{RECIPE_TEXT.doneReused}</p>}
      </div>
    )
    footer = <button className="btn btn-primary" onClick={onClose} data-autofocus>{RECIPE_TEXT.finish}</button>
  } else if (staging && (phase === 'work' || phase === 'confirm')) {
    const problemsForGrid = view === 'result' && t ? t.problems ?? [] : staging.draft_problems ?? []
    let right: ReactNode
    if (phase === 'confirm' && t && !trialStale) {
      right = (
        <ConfirmList items={t.confirm_items ?? []} checks={t.checks ?? []} acceptable={t.acceptable ?? []} reupload={isReupload}
                     busy={busy === 'commit'} missing={missing} error={confirmError} onSubmit={(b) => void commit(b)} />
      )
    } else if (view === 'result' && t) {
      right = (
        <div className="space-y-3">
          <TrialStatusLine status={t.status} />
          {trialStale && <Notice tone="warn" attr={{ 'data-trial-stale': 'result' }}>{RECIPE_TEXT.trialStaleConfirm}</Notice>}
          {t.status === 'needs_input' && (
            <PeriodForm fileName={staging.file?.name ?? ''} busy={busy === 'trial'} error={periodError}
                        onSubmit={(start, end) => void trial({ start, end })} />
          )}
          {/* 拒收、需要录入时服务端有意不算差异（diff 为 null）：不显示差异卡 */}
          {(Array.isArray(t.diff) || t.same_as_import) && <ReuploadDiff diff={t.diff} sameAsImport={t.same_as_import} />}
          <ImportReceipt trial={t} onFocusCell={focusCell}
                         expectedSheets={Array.isArray(staging.recipe?.sheets) ? staging.recipe.sheets.length : undefined} />
        </div>
      )
    } else {
      const failures = staging.draft && !staging.draft.complete ? staging.draft.failures ?? [] : []
      right = (
        <div className="space-y-3">
          <AiDraftOffer staging={staging} busy={!!busy} setBusy={(v) => setBusy(v ? 'ai' : '')} onStaging={setStaging} onError={guard} />
          {failures.length > 0 && (
            <Notice tone="warn" attr={{ 'data-draft-failures': '' }}>
              <div className="font-medium">{RECIPE_TEXT.draftIncomplete}</div>
              <ul className="mt-0.5 list-disc pl-4 text-dim">{failures.map((f, i) => <li key={i}>{f}</li>)}</ul>
            </Notice>
          )}
          {staging.draft_partial && (
            <p className="text-2xs text-faint" data-draft-partial title={RECIPE_TEXT.partialHint}>{RECIPE_TEXT.partial}</p>
          )}
          {trialStale && <Notice tone="info" attr={{ 'data-trial-stale': '' }}>{RECIPE_TEXT.trialStale}</Notice>}
          <SuggestionCards cards={staging.cards ?? []} questions={staging.questions ?? []} answers={staging.answers ?? {}}
                           busy={!!busy} applying={busy === 'answer'} onAnswer={answer} onFocusCell={focusCell} />
          {(staging.draft_problems ?? []).some((p) => p.category !== 'confirm') && (
            <section className="space-y-1.5">
              <h3 className="text-xs font-semibold">{RECIPE_TEXT.problems}</h3>
              <ProblemList problems={(staging.draft_problems ?? []).filter((p) => p.category !== 'confirm')} onFocus={focusCell} />
            </section>
          )}
        </div>
      )
    }
    body = (
      <div className="space-y-3" data-import-wizard data-staging={staging.id} data-view={phase === 'confirm' ? 'confirm' : view}>
        <Steps current={step} reupload={isReupload} />
        {notice && (
          <Notice tone={notice.tone} attr={{ 'data-wizard-notice': '' }}>
            <div>{notice.text}</div>
            {notice.detail && notice.detail !== notice.text && <div className="mt-0.5 text-2xs text-dim">{notice.detail}</div>}
          </Notice>
        )}
        <div className="grid min-h-0 grid-cols-1 gap-3 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
          <SheetGrid grids={staging.grids ?? []} marks={staging.marks ?? []} problems={problemsForGrid}
                     partial={view === 'draft' && staging.draft_partial} focus={focus} />
          <div className="min-h-0 overflow-y-auto pr-1" style={{ maxHeight: '60vh' }} data-wizard-side>{right}</div>
        </div>
        {view === 'draft' && phase === 'work' && (
          <section className="rounded-lg border" data-recipe-drawer>
            <button type="button" className="flex w-full items-center gap-1.5 px-3 py-2 text-xs font-medium hover:bg-hover"
                    aria-expanded={drawer} onClick={() => setDrawer((v) => !v)} data-recipe-drawer-toggle>
              <ChevronRight size={12} className={clsx('transition-transform', drawer && 'rotate-90')} aria-hidden />
              {RECIPE_TEXT.panel}
              {!!staging.recipe_problems?.length && (
                <span className="chip" style={{ color: 'var(--err)', borderColor: 'var(--err)' }}>
                  {formatNumber(staging.recipe_problems.length)}
                </span>
              )}
            </button>
            {drawer && (
              <div className="border-t p-3">
                <RecipePanel recipe={staging.recipe} problems={staging.recipe_problems ?? []} units={staging.units ?? []}
                             candidates={staging.candidates ?? {}} labelOptions={labelOptions} saving={busy === 'recipe'}
                             onSave={saveRecipe} />
              </div>
            )}
          </section>
        )}
      </div>
    )
    const discardBtn = (
      <button className="btn btn-ghost mr-auto" onClick={() => void discard()} disabled={!!busy} data-wizard-discard>
        {RECIPE_TEXT.discard}
      </button>
    )
    if (phase === 'confirm') {
      footer = (
        <>
          {discardBtn}
          <button className="btn" onClick={() => setPhase('work')} disabled={busy === 'commit'}>{RECIPE_TEXT.backToReceipt}</button>
        </>
      )
    } else if (view === 'result' && t) {
      // 试运行已失效（之后改过配方或录入）：不能拿它去确认、启用
      const next = (t.status === 'passed' || t.status === 'needs_decision') && !trialStale
      footer = (
        <>
          {discardBtn}
          <button className="btn" onClick={() => { setView('draft'); setDrawer(true); setNotice(null) }} disabled={!!busy} data-back-to-recipe>
            {RECIPE_TEXT.backToRecipe}
          </button>
          {next && (
            <button className="btn btn-primary" onClick={() => { setMissing(new Set()); setConfirmError(null); setPhase('confirm') }}
                    disabled={!!busy} data-to-confirm>
              {RECIPE_TEXT.toConfirm}
            </button>
          )}
        </>
      )
    } else {
      footer = (
        <>
          {discardBtn}
          {/* 新源在启用之前没有卡片，关掉就没有「有未完成的导入」可点：不给「稍后继续」 */}
          {staging.source?.exists && (
            <button className="btn" onClick={close} title={RECIPE_TEXT.laterHint}>{RECIPE_TEXT.later}</button>
          )}
          <button className="btn btn-primary tnum" disabled={!canTrial} onClick={() => void trial()} data-trial-run
                  title={staging.recipe_problems?.length ? RECIPE_TEXT.trialBlocked : undefined}>
            {busy === 'trial' ? <><Spinner size={11} /> {RECIPE_TEXT.trialRunning}</> : RECIPE_TEXT.trialRun}
          </button>
        </>
      )
    }
  }

  return (
    <Modal open onClose={close} width={phase === 'pick' || phase === 'uploading' || phase === 'loading' || phase === 'failed' || phase === 'done' ? 560 : 1280}
           // 新源的导入关掉以后没有入口可以继续：和没保存的表单一样先问一句
           dirty={(phase === 'pick' && !!file) || (!!staging && !staging.source?.exists && (phase === 'work' || phase === 'confirm'))}
           title={title} footer={footer}>
      {body}
    </Modal>
  )
}
