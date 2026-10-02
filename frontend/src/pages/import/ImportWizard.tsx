import { useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { ChevronRight, FileSpreadsheet, Info, RotateCcw, Upload, Wand2 } from 'lucide-react'
import clsx from 'clsx'
import { ApiError, api } from '../../api/client'
import type { UploadProgress } from '../../api/client'
import type {
  CommitOut, DataSource, EditPreview, QuestionAnswer, Recipe, RedraftRulesOut, SelectionAs, Staging,
} from '../../types'
import { Field, Modal, Notice, Spinner, confirmDialog, toast } from '../../components/ui'
import { localActor } from '../../lib/actor'
import { errorMessage } from '../../lib/errors'
import { formatBytes, formatDateTime, formatDuration, formatNumber } from '../../lib/format'
import { RECIPE_TEXT, UPLOAD_TEXT } from '../../lib/terms'
import { useRunClock } from '../../run/useRunClock'
import { UploadMeter } from '../DataSourcesTab'
import { AccumulatePlan } from './AccumulatePlan'
import { AiDraftOffer } from './AiDraftDialog'
import { ConfirmList } from './ConfirmList'
import type { CommitBody } from './ConfirmList'
import { FixButtons, FixPanel } from './FixPanel'
import type { EditFlowHandlers } from './FixPanel'
import { ImportReceipt, ProblemList, TrialStatusLine, fixMap } from './ImportReceipt'
import { RecipeCompare } from './RecipeCompare'
import { RecipePanel } from './RecipePanel'
import { ReuploadDiff } from './ReuploadDiff'
import { SelectionPanel } from './SelectionPanel'
import { SheetGrid, parseRef } from './SheetGrid'
import type { GridAfter, GridFocus, GridReplay, GridSelection } from './SheetGrid'
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
/** 提交时遇到这些：试运行库已被消耗、作废或被改过，回到配方步骤重新试运行（期 3 加了并集、各期被改过的两种） */
const RERUN_CODES = new Set(['trial_required', 'trial_tampered', 'base_changed', 'name_taken', 'union_tampered', 'part_tampered'])
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
  // 期 3：修复面板、框选、预览在网格上的「修改后」、按规则重新起草
  const [fixOpen, setFixOpen] = useState<string | null>(null)
  const [selection, setSelection] = useState<GridSelection | null>(null)
  const [selAs, setSelAs] = useState<SelectionAs | null>(null)
  const [after, setAfter] = useState<GridAfter | null>(null)
  const [replay, setReplay] = useState<GridReplay | null>(null)
  const [problemsBar, setProblemsBar] = useState(false)
  const [redraft, setRedraft] = useState<{ out: RedraftRulesOut | null; error: string | null; hidden: boolean }>(
    { out: null, error: null, hidden: false })
  const [commitBlocked, setCommitBlocked] = useState(false)
  // 配方面板连续保存时，排在后面的那几次拿到的还是发起时的 staging：撤销栈是否还在要看最新的那一份。
  // 渲染时同步一次；saveRecipe 存上以后还要当场改（见那里），不能只等下一次渲染
  const stagingRef = useRef<Staging | null>(null)
  stagingRef.current = staging
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

  /**
   * 写请求失败的统一去向：暂存区已结束就关掉向导；只读进程（503 store_unavailable）照写服务端原话、不自动重试
   * （原话已说明是数据目录被另一个进程占用）；其余弹出报错
   */
  const report = (e: unknown) => {
    if (guard(e)) return
    if (e instanceof ApiError && e.code === 'store_unavailable') { setNotice({ tone: 'err', text: errorMessage(e) }); return }
    toast.error(e)
  }

  /** 关掉修复面板和框选面板，网格回到「修改前」 */
  const closePanels = () => {
    setFixOpen(null)
    setSelAs(null)
    setAfter(null)
    setReplay(null)
  }

  /** 问题旁的修复按钮：回到起草，在右栏打开修复面板（网格仍然可见） */
  const openFix = (id: string) => {
    setSelAs(null)
    setAfter(null)
    setReplay(null)
    setFixOpen(id)
    setPhase('work')
    setView('draft')
  }

  /** 未被覆盖的修改：整份替换工作配方（PUT）之前要先说清楚它们会被覆盖、不能再撤销 */
  const liveEdits = (s: Staging | null) => (s?.edits ?? []).filter((x) => !x.superseded).length
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
      report(e)
      return false
    } finally {
      setBusy('')
    }
  }

  /**
   * 存上了返回服务端存下的配方：配方面板以它为底接着发排在后面的修改。还有未被覆盖的修复、框选时先确认：
   * PUT 会清空撤销栈，之前的修改标「已被覆盖」
   */
  const saveRecipe = async (recipe: Recipe): Promise<{ recipe: Recipe | null } | null> => {
    const cur = stagingRef.current
    if (!cur) return null
    const n = liveEdits(cur)
    if (n > 0) {
      const ok = await confirmDialog({
        title: RECIPE_TEXT.saveRecipeTitle, consequences: [RECIPE_TEXT.replaceEdits(n), RECIPE_TEXT.replaceUndo],
        confirmLabel: RECIPE_TEXT.saveRecipeConfirm,
      })
      if (!ok) return null
    }
    setBusy('recipe')
    try {
      const s = await api.tableImports.putRecipe(cur.id, recipe)
      // 当场更新 ref：配方面板在这次 await 返回后的同一段续体里就为排在后面的修改再调 saveRecipe，而 await 之后的
      // setStaging 要到下一个任务才重新渲染。不改的话那一次读到的还是 PUT 之前的 staging，已被覆盖的修改仍算
      // 「未被覆盖」，又弹一次「保存对配方的修改？」，点了取消，排在后面的那处修改就丢了
      stagingRef.current = s
      setStaging(s)
      return { recipe: s.recipe }
    } catch (e) {
      report(e)
      return null
    } finally {
      setBusy('')
    }
  }

  /** 撤销上一次修改：撤销之后的回答按服务端的规则延续，试运行作废 */
  const undo = async () => {
    if (!staging) return
    setBusy('undo')
    try {
      setStaging(await api.tableImports.editUndo(staging.id))
      closePanels()
      setNotice({ tone: 'info', text: RECIPE_TEXT.editUndone })
    } catch (e) {
      const code = e instanceof ApiError ? e.code : undefined
      if (code === 'nothing_to_undo' || code === 'undo_stale') {
        // 原话已给出出路（undo_stale：在配方面板中手工改回）；撤销栈的样子以服务端为准
        setNotice({ tone: 'warn', text: errorMessage(e) })
        await refetch(staging.id)
      } else report(e)
    } finally {
      setBusy('')
    }
  }

  /** 按规则重新起草（不调用模型、不改工作配方）：给出对照和名字对齐，人看过再决定采用 */
  const runRedraft = async () => {
    if (!staging) return
    setBusy('redraft')
    setRedraft((r) => ({ ...r, out: null, error: null }))
    try {
      const out = await api.tableImports.redraftRules(staging.id)
      setRedraft((r) => ({ ...r, out }))
    } catch (e) {
      if (e instanceof ApiError && e.code === 'redraft_not_offered') {
        setRedraft({ out: null, error: null, hidden: true })
        setNotice({ tone: 'warn', text: errorMessage(e) })
      } else report(e)
    } finally {
      setBusy('')
    }
  }

  /** 采用重新起草的配方：先确认工作配方（含已做的修改）将被替换，再 PUT（origin: rules_redraft，服务端核对哈希） */
  const adoptRedraft = async () => {
    const aligned = redraft.out?.aligned_recipe
    if (!staging || !aligned) return
    const ok = await confirmDialog({
      title: RECIPE_TEXT.redraftAdoptTitle,
      consequences: [RECIPE_TEXT.replaceEdits(liveEdits(staging)), RECIPE_TEXT.redraftAdoptAfter],
      confirmLabel: RECIPE_TEXT.redraftAdopt,
    })
    if (!ok) return
    setBusy('recipe')
    try {
      setStaging(await api.tableImports.putRecipe(staging.id, aligned, 'rules_redraft'))
      setRedraft((r) => ({ ...r, out: null, error: null }))
      closePanels()
    } catch (e) {
      // 采用的配方与重新起草的结果对不上：留在对照上，请人重新起草
      if (e instanceof ApiError && e.code === 'redraft_mismatch') setRedraft((r) => ({ ...r, error: RECIPE_TEXT.redraftMismatch }))
      else report(e)
    } finally {
      setBusy('')
    }
  }

  /** 修复面板、框选面板的预览与应用。kind 只决定应用之后的那句提示 */
  const editHandlers = (kind: 'fix' | 'selection', s: Staging): EditFlowHandlers => ({
    stagingId: s.id,
    onApplied: (next) => {
      setStaging(next)
      closePanels()
      setSelection(null)
      setView('draft')
      setNotice({ tone: 'info', text: kind === 'fix' ? RECIPE_TEXT.fixApplied : RECIPE_TEXT.selectionApplied })
    },
    onGone: (detail) => {
      closePanels()
      setNotice({ tone: 'warn', text: RECIPE_TEXT.fixStale, detail })
      void refetch(s.id)
    },
    onPreview: (p: EditPreview | null) => {
      setAfter(p?.dry_run ? { marks: p.dry_run.marks ?? [], problems: p.dry_run.problems ?? [], partial: p.dry_run.partial } : null)
      setReplay(kind === 'selection' && p?.ok && p.replay && selection ? { sheet: selection.sheet, actual: p.replay.actual ?? {} } : null)
    },
    onError: (e) => {
      if (guard(e)) return true
      if (e instanceof ApiError && e.code === 'store_unavailable') { setNotice({ tone: 'err', text: errorMessage(e) }); return true }
      return false
    },
  })

  const trial = async (context?: { start: string; end: string }) => {
    if (!staging) return
    setBusy('trial')
    setNotice(null)
    setPeriodError(null)
    closePanels()
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
      // 某一期的数据文件不在了：原话指向数据源卡片上的「版本」（移除那一期或启用更早的版本），这次不能继续
      else if (code === 'part_missing' || code === 'store_unavailable') setNotice({ tone: 'err', text: errorMessage(e) })
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
      } else if (code && RERUN_CODES.has(code)) {
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
      } else if (code === 'build_conflict') {
        // 服务端已有的同版本数据文件与登记的不一致、又无法恢复：重试也会失败，留在确认页、禁用启用，等管理员处理
        setConfirmError(message)
        setCommitBlocked(true)
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
      report(e)
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
  const fixes = useMemo(() => fixMap(staging?.fixes), [staging?.fixes])
  const proposal = fixOpen ? fixes.get(fixOpen) ?? null : null
  const drafting = phase === 'work' && view === 'draft'
  // 提议已不在当前列表里（重新评估之后问题变了）：面板跟着关掉，不对着一个不存在的提议预览
  useEffect(() => { if (fixOpen && staging && !fixes.has(fixOpen)) closePanels() }, [fixOpen, fixes])
  // 离开起草（试运行结果、确认清单）：选区和框选面板一并清掉
  useEffect(() => { if (!drafting) { setSelection(null); setSelAs(null); setReplay(null); if (!fixOpen) setAfter(null) } }, [drafting])

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
        {!done.unchanged && done.snapshot_reused && <p className="text-xs text-dim" data-done-reused-version>{RECIPE_TEXT.doneSnapshotReused}</p>}
        {!done.unchanged && !done.snapshot_reused && done.build_reused && <p className="text-xs text-faint">{RECIPE_TEXT.doneReused}</p>}
        {done.build_restored && (
          <Notice tone="warn" className="text-left" attr={{ 'data-build-restored': '' }}>{RECIPE_TEXT.doneRestored}</Notice>
        )}
      </div>
    )
    footer = <button className="btn btn-primary" onClick={onClose} data-autofocus>{RECIPE_TEXT.finish}</button>
  } else if (staging && (phase === 'work' || phase === 'confirm')) {
    const problemsForGrid = view === 'result' && t ? t.problems ?? [] : staging.draft_problems ?? []
    let right: ReactNode
    if (phase === 'confirm' && t && !trialStale) {
      right = (
        <div className="space-y-3">
          {/* 新旧配方对照也放在确认清单顶部：勾选之前再看一眼这次改了什么 */}
          {t.recipe_compare && <RecipeCompare compare={t.recipe_compare} attr="data-confirm-compare" />}
          <ConfirmList items={t.confirm_items ?? []} checks={t.checks ?? []} acceptable={t.acceptable ?? []} reupload={isReupload}
                       busy={busy === 'commit'} missing={missing} error={confirmError} blocked={commitBlocked}
                       prior={t.prior_acceptances} fixes={staging.fixes} sheets={t.receipt?.sheets} onFix={openFix}
                       onSubmit={(b) => void commit(b)} />
        </div>
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
          {/* 配方与现行的不同：对照放在回执顶部，破坏性的项排在最前 */}
          {t.recipe_compare && <RecipeCompare compare={t.recipe_compare} />}
          {t.accumulate && <AccumulatePlan plan={t.accumulate} checks={t.union_checks} />}
          {/* 拒收、需要录入时服务端有意不算差异（diff 为 null）：不显示差异卡 */}
          {(Array.isArray(t.diff) || t.same_as_import) && <ReuploadDiff diff={t.diff} sameAsImport={t.same_as_import} />}
          <ImportReceipt trial={t} onFocusCell={focusCell} fixes={staging.fixes} onFix={openFix}
                         expectedSheets={Array.isArray(staging.recipe?.sheets) ? staging.recipe.sheets.length : undefined} />
        </div>
      )
    } else if (proposal) {
      right = (
        <FixPanel key={proposal.id} proposal={proposal} handlers={editHandlers('fix', staging)} onFocus={focusCell}
                  onClose={closePanels} />
      )
    } else if (selection && selAs) {
      right = (
        <SelectionPanel key={`${selection.sheet}!${selection.ref}|${selAs}`} selection={selection} as={selAs} recipe={staging.recipe}
                        handlers={editHandlers('selection', staging)} onFocus={focusCell} onClose={closePanels} />
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
                           busy={!!busy} applying={busy === 'answer'} dropped={staging.answers_dropped}
                           onAnswer={answer} onFocusCell={focusCell} />
          {(staging.draft_problems ?? []).some((p) => p.category !== 'confirm') && (
            <section className="space-y-1.5">
              <h3 className="text-xs font-semibold">{RECIPE_TEXT.problems}</h3>
              <ProblemList problems={(staging.draft_problems ?? []).filter((p) => p.category !== 'confirm')} onFocus={focusCell}
                           fixes={fixes} onFix={openFix} />
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
                     partial={view === 'draft' && staging.draft_partial} focus={focus}
                     selectable={drafting} selection={selection} highlight={proposal?.cells} after={after} replay={replay}
                     onSelection={(sel) => {
                       // 选区变了：之前的框选预览不再对应这个框，面板关掉，重新选「框选为…」
                       setSelection(sel)
                       if (selAs) { setSelAs(null); setAfter(null); setReplay(null) }
                     }}
                     onSelectAs={(as) => { setFixOpen(null); setAfter(null); setReplay(null); setSelAs(as) }} />
          <div className="min-h-0 overflow-y-auto pr-1" style={{ maxHeight: '60vh' }} data-wizard-side>{right}</div>
        </div>
        {drafting && !!staging.recipe_problems?.length && (
          // 配方有问题时「试运行」是禁用的：原因和出路（修复按钮）放在抽屉外，不用先展开配方面板才看得到
          <section className="rounded-lg border" data-recipe-problems-bar={staging.recipe_problems.length}
                   style={{ borderColor: 'color-mix(in srgb, var(--err) 35%, var(--border))' }}>
            <button type="button" className="flex w-full items-center gap-1.5 px-3 py-2 text-left text-xs hover:bg-hover"
                    aria-expanded={problemsBar} onClick={() => setProblemsBar((v) => !v)} data-recipe-problems-toggle>
              <ChevronRight size={12} className={clsx('transition-transform', problemsBar && 'rotate-90')} aria-hidden />
              <span className="font-medium text-[var(--err)]">{RECIPE_TEXT.recipeProblemsBar(formatNumber(staging.recipe_problems.length))}</span>
              <span className="text-2xs text-faint">{RECIPE_TEXT.recipeProblemsHint}</span>
            </button>
            {problemsBar && (
              <ul className="space-y-1.5 border-t px-3 py-2" data-recipe-problems-list>
                {staging.recipe_problems.map((p, i) => (
                  <li key={i} className="flex flex-wrap items-center gap-2 text-xs" data-recipe-problem={p.code}>
                    <span className="min-w-0 flex-1 leading-relaxed">{p.message}</span>
                    <FixButtons ids={p.fix_ids} fixes={fixes} onFix={openFix} />
                  </li>
                ))}
              </ul>
            )}
          </section>
        )}
        {drafting && !!staging.edits?.length && (
          <section className="space-y-1.5 rounded-lg border px-3 py-2" data-edits={staging.edits.length}>
            <h3 className="text-xs font-semibold">{RECIPE_TEXT.editsTitle}</h3>
            <ul className="space-y-1">
              {staging.edits.map((x) => (
                <li key={x.seq} className={clsx('flex flex-wrap items-center gap-2 text-xs', x.superseded && 'text-faint')}
                    data-edit={x.seq} data-superseded={x.superseded || undefined}
                    title={x.superseded ? RECIPE_TEXT.editSupersededHint : undefined}>
                  <span className={clsx('min-w-0 flex-1', x.superseded && 'line-through')}>{x.title}</span>
                  {x.superseded && <span className="chip" data-edit-superseded>{RECIPE_TEXT.editSuperseded}</span>}
                  <span className="text-2xs text-faint">
                    {formatDateTime(x.at)}{x.signed_by ? ` · ${RECIPE_TEXT.editSigned(x.signed_by)}` : ''}
                  </span>
                  {x.undoable && !x.superseded && (
                    <button type="button" className="btn btn-xs" disabled={!!busy} onClick={() => void undo()} data-edit-undo>
                      <RotateCcw size={10} aria-hidden /> {RECIPE_TEXT.editUndo}
                    </button>
                  )}
                </li>
              ))}
            </ul>
          </section>
        )}
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
              <div className="space-y-3 border-t p-3">
                {(kind === 'reupload' || kind === 'redraft') && !redraft.hidden && (
                  <div className="space-y-2" data-redraft-block>
                    <div className="flex flex-wrap items-center gap-2">
                      <button type="button" className="btn btn-sm" disabled={!!busy} onClick={() => void runRedraft()} data-redraft-rules>
                        {busy === 'redraft' ? <Spinner size={11} /> : <Wand2 size={11} aria-hidden />} {RECIPE_TEXT.redraftRules}
                      </button>
                      <span className="text-2xs text-faint">{RECIPE_TEXT.redraftRulesHint}</span>
                    </div>
                    {redraft.out && (
                      <RecipeCompare compare={redraft.out.compare} attr="data-redraft-compare" title={RECIPE_TEXT.redraftTitle}
                                     notes={redraft.out.alignment} notesTitle={RECIPE_TEXT.redraftAlignment}>
                        {!redraft.out.aligned_recipe && (
                          <div className="text-xs text-[var(--warn)]" data-redraft-incomplete>
                            <div>{RECIPE_TEXT.redraftNoRecipe}</div>
                            {!!redraft.out.draft?.failures?.length && (
                              <ul className="mt-0.5 list-disc pl-4 text-dim">{redraft.out.draft.failures.map((f, i) => <li key={i}>{f}</li>)}</ul>
                            )}
                          </div>
                        )}
                        {redraft.error && <p className="text-xs text-[var(--err)]" role="alert" data-redraft-error>{redraft.error}</p>}
                        <div className="flex flex-wrap gap-2 pt-1">
                          <button type="button" className="btn btn-sm btn-primary" disabled={!redraft.out.aligned_recipe || !!busy}
                                  onClick={() => void adoptRedraft()} data-redraft-adopt>
                            {RECIPE_TEXT.redraftAdopt}
                          </button>
                          <button type="button" className="btn btn-sm" disabled={!!busy}
                                  onClick={() => setRedraft((r) => ({ ...r, out: null, error: null }))} data-redraft-discard>
                            {RECIPE_TEXT.redraftDiscard}
                          </button>
                        </div>
                      </RecipeCompare>
                    )}
                  </div>
                )}
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
            <button className="btn btn-primary" onClick={() => { setMissing(new Set()); setConfirmError(null); setCommitBlocked(false); setPhase('confirm') }}
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
