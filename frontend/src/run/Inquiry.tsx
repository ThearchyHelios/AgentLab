import {
  useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent, type ReactNode, type RefObject,
} from 'react'
import { create } from 'zustand'
import { AlertTriangle, ChevronRight, MessageCircleQuestion } from 'lucide-react'
import { ApiError, api } from '../api/client'
import { Spinner, isComposing, useTicker } from '../components/ui'
import { EVIDENCE_STATE, segmentLabel, segmentState, sourceOf, unitText, type EvidenceStateCode } from '../lib/evidence'
import { humanizeError } from '../lib/errors'
import { formatCost, formatDateTime, formatNumber, formatTime } from '../lib/format'
import { EVIDENCE_STATE_LABEL, EVIDENCE_TEXT, INQUIRY_TEXT } from '../lib/terms'
import { useEvidence } from '../store/evidence'
import type {
  EvidenceBlock, EvidenceDocData, EvidenceSegment, EvidenceUnit, Inquiry, InquiryQuery, InquiryRequest, InquirySnapshot, Run,
} from '../types'
import { ResultTable } from './AssistantStream'
import type { EvidenceDocHandle } from './EvidenceDoc'
import { BlockView, CopyChip, Markdown, StyledSlice, inlineStyles, numericColumns } from './Markdown'

/**
 * 审批质询：审批人就待审的报告提问——选一句（句末的「质询」）、划一段原文（浮出的「质询」），或者就整份报告——
 * 核查模型只读查库作答。问答写进运行记录、随运行一并封存，不改动报告；回答里的数字照报告的样子标出处，点开看它查了
 * 什么。
 *
 * 分三处：
 * - 入口在报告文档上（EvidenceDoc 的 inquiry 属性，askAbout 把要问的那句交到这里的 store）；
 * - 问答串和提问框在 ReportInquiry 里，由页面挂在报告下面（审批卡：报告核对概况下面；运行详情：报告下面），
 *   问答按句子归拢，同一句下按时间排；进入时按列表接口恢复；
 * - 回答期间（这一次运行有一条在问）审批卡的批准 / 驳回暂停（useInquiryBusy）：服务端在回答时也会拒绝审批回复。
 *
 * 一次运行同一时刻只有一条在回答（服务端回 409 inquiry_busy）：请求挂在 store 上，不随组件卸载作废，回答回来时
 * 卡片收起了也照样记进问答串。没写进运行记录的（recorded 为假）醒目地说，它只留在这一页里。
 */

/** 问题、原文的长度上限（同服务端，按字数） */
export const QUESTION_MAX = 1000
export const QUOTE_MAX = 500

/** 页面传给报告文档、问答串的「能不能问」：给了就是能问；approvalId 是运行停在的那张审批（封存后的探索运行为 null） */
export interface InquiryAccess {
  approvalId: string | null
}

/** 封存了的探索运行：能问，不带审批（同一个对象，报告文档不因此重画） */
export const NO_APPROVAL: InquiryAccess = Object.freeze({ approvalId: null })

/** 要问的是哪儿：一句（unit）、划选的原文（quote，同时带上它起头的那一句），或者整份报告（两个都是 null） */
export interface InquiryTarget {
  unit: string | null
  quote: string | null
  /** 提问框里给人看的：那句话或那段原文 */
  text: string
}

/**
 * 运行详情的报告上给不给「质询」：只有封存了的探索运行。正式运行封存之后不再开放（同封存后的按需裁判）；
 * 还在执行、停在审批上的运行在审批卡上问（问的时候带着那张审批）
 */
export function inquiryOpen(run: Pick<Run, 'run_class' | 'manifest_hash' | 'status'> | null | undefined): boolean {
  return !!run && run.run_class === 'exploratory' && !!run.manifest_hash
    && (run.status === 'succeeded' || run.status === 'failed' || run.status === 'cancelled')
}

// ---------------------------------------------------------------- 认服务端给的

const str = (v: unknown) => (typeof v === 'string' ? v.trim() : '')
const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : null)

function asQuery(x: unknown): InquiryQuery | null {
  if (!x || typeof x !== 'object') return null
  const q = x as Record<string, unknown>
  return {
    alias: str(q.alias), artifact: str(q.artifact), sql: typeof q.sql === 'string' ? q.sql : '',
    row_count: num(q.row_count), truncated: q.truncated === true, source: str(q.source) || null,
  }
}

/**
 * 一条问答认不认得：至少得有问题。别的字段缺了、类型不对的按没有算——服务端由另一个工作包按同一份约定实现，
 * 字段名对得上，取值照样可能缺
 */
export function asInquiry(x: unknown): Inquiry | null {
  if (!x || typeof x !== 'object') return null
  const r = x as Record<string, any>
  const question = str(r.question)
  if (!question) return null
  const a = r.answer && typeof r.answer === 'object' ? r.answer as Record<string, unknown> : {}
  return {
    seq: num(r.seq), at: typeof r.at === 'string' || typeof r.at === 'number' ? r.at : null, actor: str(r.actor) || null,
    report: str(r.report), unit: str(r.unit) || null, quote: str(r.quote) || null, question,
    answer: {
      text: str(a.text), doc_artifact: str(a.doc_artifact) || null, ok: a.ok === true, limit: str(a.limit) || null,
      error: str(a.error) || null,
    },
    queries: (Array.isArray(r.queries) ? r.queries : []).map(asQuery).filter((q): q is InquiryQuery => !!q),
    model: str(r.model) || null, usage: r.usage && typeof r.usage === 'object' ? r.usage : {},
    post_seal: r.post_seal === true, approval_id: str(r.approval_id) || null,
  }
}

/**
 * 质询工件接口给的查询快照。约定只说是快照：可能是快照本身，也可能包在 {id, content} 里，两种都认。遮掉的列以接口
 * 给的 masked 为准，快照里记着的 mask_columns 也算进去（接口万一给了原值，界面照样不显示）
 */
export function snapshotOf(body: unknown): InquirySnapshot | null {
  if (!body || typeof body !== 'object') return null
  const outer = body as Record<string, unknown>
  const c = (outer.content && typeof outer.content === 'object' ? outer.content : outer) as Record<string, unknown>
  if (!Array.isArray(c.columns) || !Array.isArray(c.rows)) return null
  const columns = c.columns.map(String)
  const rows = c.rows.map((r) => (Array.isArray(r) ? r
    : columns.map((col) => (r && typeof r === 'object' ? (r as Record<string, unknown>)[col] ?? null : null))))
  const list = (v: unknown) => (Array.isArray(v) ? v.map(String) : [])
  const masked = [...new Set([...list(c.masked), ...list(outer.masked), ...list(c.mask_columns)])]
  return {
    columns, rows, sql: typeof c.sql === 'string' ? c.sql : null, truncated: c.truncated === true,
    total: num(c.total_rows) ?? num(c.row_count), masked, source: str(c.source) || null,
  }
}

/** 出错时给人看的一句：服务端的 409 / 422 照它的中文说明；连不上照通用的说法 */
function problemOf(e: unknown): Problem {
  if (e instanceof ApiError) {
    return { message: e.kind === 'network' ? humanizeError(e).title : e.message, code: e.code ?? (e.status ? String(e.status) : 'network'),
             field: e.field }
  }
  return { message: humanizeError(e).title || INQUIRY_TEXT.submitFailed, code: 'client' }
}

// ---------------------------------------------------------------- store

interface Problem { message: string; code: string; field?: string }

/** 问答串里的一条：服务端列出来的都写进了运行记录；刚问的照答复的 recorded 记 */
export interface InquiryEntry {
  /** React 的键：有事件序号用序号，没写进运行记录的另编一个 */
  id: string
  inquiry: Inquiry
  recorded: boolean
  note: string | null
}

interface Thread {
  status: 'loading' | 'ok' | 'error'
  /** 服务端列出来的 */
  listed: Inquiry[]
  /** 这一页里问过的（列表重新取回来之后，写进运行记录的按序号去重，没写进的一直留在这里） */
  local: InquiryEntry[]
  error?: Problem
}

interface Draft {
  target: InquiryTarget
  approvalId: string | null
  question: string
  error: Problem | null
  /** 每次点「质询」加一：提问框据此把焦点送进输入框，换了一句也一样 */
  nonce: number
}

interface Pending {
  key: string
  target: InquiryTarget
  question: string
  startedAt: number
}

interface InquiryState {
  threads: Record<string, Thread>
  /** 开着的提问框，按（运行、报告）各一个 */
  drafts: Record<string, Draft | undefined>
  /** 正在回答的，按运行：一次运行同一时刻只有一条 */
  pending: Record<string, Pending | undefined>
  load: (runId: string, report: string) => Promise<void>
  compose: (runId: string, report: string, target: InquiryTarget, approvalId: string | null) => void
  edit: (key: string, question: string) => void
  close: (key: string) => void
  submit: (runId: string, report: string) => Promise<void>
}

export const inquiryKey = (runId: string, report: string) => `${runId}@${report}`

/** 打开提问框的那个元素（句末的「质询」、划选浮出的按钮已经没了时退回整份报告那个）：Esc、取消之后焦点回到这里 */
const openers = new Map<string, HTMLElement>()
let localSeq = 0

export const useInquiry = create<InquiryState>((set, get) => ({
  threads: {},
  drafts: {},
  pending: {},

  load: async (runId, report) => {
    const key = inquiryKey(runId, report)
    if (get().threads[key]?.status === 'loading') return
    set((s) => {
      const cur = s.threads[key]
      return { threads: { ...s.threads, [key]: { listed: cur?.listed ?? [], local: cur?.local ?? [], status: 'loading' } } }
    })
    try {
      const res = await api.inquiry.list(runId, { report })
      const listed = (Array.isArray(res?.items) ? res.items : []).map(asInquiry)
        .filter((x): x is Inquiry => !!x && (!x.report || x.report === report))
      set((s) => ({ threads: { ...s.threads, [key]: { ...s.threads[key], listed, status: 'ok' } } }))
    } catch (e) {
      // 老服务端没有这个接口（404）：当作还没有问答，不报错
      const missing = e instanceof ApiError && e.status === 404
      set((s) => ({ threads: { ...s.threads, [key]: {
        ...s.threads[key], status: missing ? 'ok' : 'error', error: missing ? undefined : problemOf(e),
      } } }))
    }
  },

  compose: (runId, report, target, approvalId) => {
    const key = inquiryKey(runId, report)
    set((s) => {
      const cur = s.drafts[key]
      // 换了一句：问题留着（多半是同一个问题挪个地方问），上一次的报错不留
      return { drafts: { ...s.drafts, [key]: {
        target, approvalId, question: cur?.question ?? '', error: null, nonce: (cur?.nonce ?? 0) + 1,
      } } }
    })
  },

  edit: (key, question) => set((s) => {
    const cur = s.drafts[key]
    return cur ? { drafts: { ...s.drafts, [key]: { ...cur, question, error: cur.error?.field === 'quote' ? cur.error : null } } } : s
  }),

  close: (key) => set((s) => ({ drafts: { ...s.drafts, [key]: undefined } })),

  submit: async (runId, report) => {
    const key = inquiryKey(runId, report)
    const draft = get().drafts[key]
    if (!draft) return
    const question = draft.question.trim()
    const fail = (error: Problem) => set((s) => ({ drafts: { ...s.drafts, [key]: { ...draft, error } } }))
    if (!question) return fail({ message: INQUIRY_TEXT.empty, code: 'local', field: 'question' })
    if ([...question].length > QUESTION_MAX) return fail({ message: INQUIRY_TEXT.tooLong(QUESTION_MAX), code: 'local', field: 'question' })
    if (get().pending[runId]) return fail({ message: INQUIRY_TEXT.busyOther, code: 'local' })
    const quote = draft.target.quote ? [...draft.target.quote].slice(0, QUOTE_MAX).join('') : ''
    // 约定：unit 可为 null；quote 可选，没划选就不带；approval_id 只在运行停在审批上时带
    const body: InquiryRequest = {
      report, unit: draft.target.unit, ...(quote ? { quote } : {}), question,
      ...(draft.approvalId ? { approval_id: draft.approvalId } : {}),
    }
    set((s) => ({
      drafts: { ...s.drafts, [key]: undefined },
      pending: { ...s.pending, [runId]: { key, target: draft.target, question, startedAt: Date.now() } },
    }))
    try {
      const res = await api.inquiry.ask(runId, body)
      const inquiry = asInquiry(res?.inquiry) ?? asInquiry({ ...body, answer: {} })!
      const entry: InquiryEntry = {
        id: inquiry.seq != null ? `s${inquiry.seq}` : `l${++localSeq}`, inquiry: { ...inquiry, report: inquiry.report || report },
        recorded: res?.recorded !== false, note: str(res?.note) || null,
      }
      set((s) => {
        const cur = s.threads[key] ?? { status: 'ok' as const, listed: [], local: [] }
        return {
          threads: { ...s.threads, [key]: { ...cur, local: [...cur.local, entry] } },
          pending: { ...s.pending, [runId]: undefined },
        }
      })
    } catch (e) {
      // 没问成（409 / 422 / 连不上）：提问框带着原来的问题重新打开，照服务端的说明显示原因
      set((s) => ({
        pending: { ...s.pending, [runId]: undefined },
        drafts: { ...s.drafts, [key]: { ...draft, question: draft.question, error: problemOf(e), nonce: draft.nonce + 1 } },
      }))
    }
  },
}))

/**
 * 句末的「质询」、划选浮出的「质询」点下去：打开这份报告的提问框。opener 是点下去的那个元素，Esc、取消之后焦点回到它
 */
export function askAbout(runId: string, report: string, access: InquiryAccess, target: InquiryTarget, opener?: HTMLElement | null) {
  const key = inquiryKey(runId, report)
  if (opener) openers.set(key, opener)
  else openers.delete(key)
  useInquiry.getState().compose(runId, report, target, access.approvalId)
}

/** 这次运行是不是有一条质询正在回答：审批卡据此暂停批准 / 驳回（服务端这时也回 409） */
export const useInquiryBusy = (runId: string | null | undefined): boolean =>
  useInquiry((s) => !!runId && !!s.pending[runId])

/** 问答串：服务端列出来的，加上这一页里问过、列表里还没有的（没写进运行记录的一直只在这里），按提问时间排 */
function entriesOf(thread: Thread | undefined): InquiryEntry[] {
  if (!thread) return []
  const listed: InquiryEntry[] = thread.listed.map((inquiry, i) => ({
    id: inquiry.seq != null ? `s${inquiry.seq}` : `g${i}`, inquiry, recorded: true, note: null,
  }))
  const seen = new Set(thread.listed.map((x) => x.seq).filter((s) => s != null))
  const local = thread.local.filter((e) => e.inquiry.seq == null || !seen.has(e.inquiry.seq))
  const at = (e: InquiryEntry) => {
    const t = typeof e.inquiry.at === 'number' ? e.inquiry.at * (e.inquiry.at < 1e12 ? 1000 : 1)
      : e.inquiry.at ? Date.parse(/[zZ]|[+-]\d\d:?\d\d$/.test(e.inquiry.at) ? e.inquiry.at : `${e.inquiry.at}Z`) : NaN
    return Number.isFinite(t) ? t : Infinity
  }
  // 稳定排序：时间相同、没有时间的保持原来的先后（列表在前，刚问的在后）
  return [...listed, ...local].map((e, i) => ({ e, i, t: at(e) }))
    .sort((a, b) => (a.t === b.t ? a.i - b.i : a.t - b.t)).map((x) => x.e)
}

// ---------------------------------------------------------------- 问答串

/** 问答按句子归拢：有 unit 的按句子，只有原文的按原文，都没有的是整份报告 */
const groupKey = (t: { unit: string | null; quote: string | null }) =>
  t.unit ? `u:${t.unit}` : t.quote ? `q:${t.quote}` : 'report'

interface Group {
  key: string
  unit: string | null
  quote: string | null
  entries: InquiryEntry[]
}

/** 焦点丢了（按过的按钮被拿掉，落回 body）才替它接住；用户已经去了别处的不抢 */
const focusLost = () => !document.activeElement || document.activeElement === document.body

/** 接住焦点的几处本身不在 Tab 顺序里（tabIndex -1），键盘用户得看得见落在哪 */
const FOCUS_RING = 'rounded-sm focus:outline-none focus-visible:outline focus-visible:outline-solid focus-visible:outline-2 '
  + 'focus-visible:outline-offset-1 focus-visible:outline-[color:var(--accent)]'

/**
 * 一份报告的审批质询：问答串（按句子归拢，同一句下按时间排）、正在回答的那一条、提问框，以及「就整份报告提问」。
 * access 为空时只读：只列已有的问答，不给提问（没有问答时什么都不画）。onLocate：回答里引用了报告已有出处的数字，
 * 交给页面在报告里打开那一处的证据面板，找不到时返回 false，这里照文档记下的出处说一句
 */
export function ReportInquiry({ runId, report, docArtifact, access, onLocate, postSeal = false }: {
  runId: string
  report: string
  /** 报告文档的工件 id：问答串的句子取原文 */
  docArtifact?: string | null
  access: InquiryAccess | null
  onLocate?: (seg: EvidenceSegment, doc: EvidenceDocData) => boolean
  /** 运行已封存（运行详情）：提问框写明问答追加在封存之后 */
  postSeal?: boolean
}) {
  const key = inquiryKey(runId, report)
  const thread = useInquiry((s) => s.threads[key])
  const draft = useInquiry((s) => s.drafts[key])
  const pending = useInquiry((s) => s.pending[runId])
  const load = useInquiry((s) => s.load)
  useEffect(() => { void load(runId, report) }, [runId, report, load])
  const entries = useMemo(() => entriesOf(thread), [thread])
  const mine = pending?.key === key ? pending : undefined

  // 句子的原文从报告文档取（审批卡上逐段证据没展开时也要取：问答串得写出问的是哪一句）
  const docSlot = useEvidence((s) => (docArtifact ? s.docs[docArtifact] : undefined))
  const loadDoc = useEvidence((s) => s.loadDoc)
  const needDoc = !!docArtifact && (entries.some((e) => e.inquiry.unit) || !!draft?.target.unit || !!mine?.target.unit)
  useEffect(() => { if (needDoc && docArtifact) void loadDoc(docArtifact) }, [needDoc, docArtifact, loadDoc])
  const units = useMemo(() => {
    const map = new Map<string, string>()
    const doc = docSlot?.status === 'ok' ? docSlot.data : undefined
    for (const b of doc?.blocks ?? []) for (const u of b.units ?? []) map.set(u.id, unitText(u).trim())
    return map
  }, [docSlot])

  const groups = useMemo(() => {
    const out = new Map<string, Group>()
    const add = (unit: string | null, quote: string | null) => {
      const k = groupKey({ unit, quote })
      if (!out.has(k)) out.set(k, { key: k, unit, quote, entries: [] })
      return out.get(k)!
    }
    for (const e of entries) add(e.inquiry.unit, e.inquiry.quote).entries.push(e)
    // 正在回答的、开着的提问框挂在它们问的那一句下面：那句还没有问答时另起一组，排在最后
    if (mine) add(mine.target.unit, mine.target.quote)
    if (draft) add(draft.target.unit, draft.target.quote)
    return [...out.values()]
  }, [entries, mine, draft])

  // 提交之后提问框拿掉了，焦点接到「正在回答」那一条上；回答回来那一条换掉了，再接到回答上
  const pendingRef = useRef<HTMLElement | null>(null)
  const lastRef = useRef<HTMLElement | null>(null)
  const held = useRef(false)
  useLayoutEffect(() => {
    if (mine && focusLost()) { pendingRef.current?.focus({ preventScroll: true }); held.current = true }
  }, [mine])
  const count = entries.length
  const [heard, setHeard] = useState('')
  const seen = useRef(count)
  useLayoutEffect(() => {
    if (count > seen.current) {
      setHeard(INQUIRY_TEXT.arrived)
      if (held.current && focusLost()) lastRef.current?.focus({ preventScroll: true })
    }
    held.current = false
    seen.current = count
  }, [count])

  const reportBtn = useRef<HTMLButtonElement>(null)
  if (!access && !entries.length && thread?.status !== 'error') return null
  const latest = entries[entries.length - 1]?.id

  return (
    <section className="mt-2 min-w-0 border-t pt-2" data-inquiry={report} aria-label={INQUIRY_TEXT.title}>
      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
        <span className="flex min-w-0 items-center gap-1 text-2xs font-medium text-faint">
          <MessageCircleQuestion size={11} aria-hidden /> {INQUIRY_TEXT.title}
          {count > 0 && <span className="tnum font-normal" data-inquiry-count={count}>· {INQUIRY_TEXT.headCount(count)}</span>}
        </span>
        <span className="flex-1" />
        {access && (
          <button ref={reportBtn} type="button" className="btn btn-xs" data-inquiry-ask-report=""
                  onClick={(e) => askAbout(runId, report, access, { unit: null, quote: null, text: '' }, e.currentTarget)}>
            <MessageCircleQuestion size={11} aria-hidden /> {INQUIRY_TEXT.askReport}
          </button>
        )}
      </div>
      {access && !count && !mine && !draft && (
        <p className="mt-1 text-2xs leading-relaxed text-dim" data-inquiry-intro="">
          {postSeal ? INQUIRY_TEXT.introRun : INQUIRY_TEXT.intro}
        </p>
      )}
      {thread?.status === 'error' && (
        <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-2xs" data-inquiry-load-failed="">
          <span style={{ color: 'var(--st-waiting)' }}>{INQUIRY_TEXT.loadFailed}</span>
          <button type="button" className="btn btn-xs" onClick={() => void load(runId, report)}>{INQUIRY_TEXT.retry}</button>
        </div>
      )}
      <div className="sr-only" aria-live="polite" aria-atomic="true">{heard}</div>
      {groups.length > 0 && (
        <div className="mt-1.5 space-y-2">
          {groups.map((g) => (
            <div key={g.key} className="min-w-0" data-inquiry-group={g.unit ?? (g.quote ? 'quote' : 'report')}>
              <GroupHead group={g} units={units} />
              <div className="mt-1 space-y-2 border-l pl-2">
                {g.entries.map((e) => (
                  <EntryView key={e.id} runId={runId} entry={e} groupQuote={g.unit ? null : g.quote} onLocate={onLocate}
                             focusRef={e.id === latest ? (el) => { lastRef.current = el } : undefined} />
                ))}
                {mine && groupKey(mine.target) === g.key && (
                  <PendingView pending={mine} focusRef={(el) => { pendingRef.current = el }} />
                )}
                {draft && groupKey(draft.target) === g.key && (
                  <Composer runId={runId} report={report} draft={draft} busy={!!pending} postSeal={postSeal}
                            fallback={reportBtn} />
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

/** 一组的抬头：问的是哪一句（原文从报告文档取，取不到写「报告中的一句」）、哪段原文，或者整份报告 */
function GroupHead({ group, units }: { group: Group; units: Map<string, string> }) {
  const text = group.unit ? units.get(group.unit) || group.entries.find((e) => e.inquiry.quote)?.inquiry.quote || ''
    : group.quote ?? ''
  return (
    <div className="flex min-w-0 items-start gap-1.5 text-2xs leading-relaxed" data-inquiry-about="">
      {text ? (
        <span className="min-w-0 flex-1 text-dim [overflow-wrap:anywhere]" data-inquiry-about-text="">
          「<StyledSlice text={text} styles={inlineStyles(text)} from={0} to={text.length} links={false} />」
        </span>
      ) : (
        <span className="text-faint">{group.unit ? INQUIRY_TEXT.unitMissing : INQUIRY_TEXT.wholeReport}</span>
      )}
    </div>
  )
}

/** 问、答前面的小标签 */
function Tag({ children }: { children: ReactNode }) {
  return <span className="chip shrink-0 px-1.5 py-0 font-medium leading-relaxed" aria-hidden>{children}</span>
}

/** 一问一答 */
function EntryView({ runId, entry, groupQuote, onLocate, focusRef }: {
  runId: string
  entry: InquiryEntry
  /** 组抬头已经写着这段原文：问题下面不再写一遍 */
  groupQuote: string | null
  onLocate?: (seg: EvidenceSegment, doc: EvidenceDocData) => boolean
  focusRef?: (el: HTMLElement | null) => void
}) {
  const { inquiry, recorded, note } = entry
  const baseId = useId()
  /** 展开了结果的查询：别名 → 要高亮的那一格（从回答里的数字点过来的）或 null */
  const [shown, setShown] = useState<Record<string, { row: number; column: string } | null>>({})
  /** 查询列表展开没有：从回答里的数字点过来时一并展开（同一次渲染，焦点才跳得过去） */
  const [listOpen, setListOpen] = useState(false)
  const [elsewhere, setElsewhere] = useState<string | null>(null)
  const jumpTo = useRef<string | null>(null)
  useEffect(() => {
    const alias = jumpTo.current
    if (!alias) return
    jumpTo.current = null
    const part = document.getElementById(`${baseId}-q-${alias}`)
    const head = part?.querySelector<HTMLElement>('[data-inquiry-query-head]')
    if (!part || !head) return
    head.focus({ preventScroll: true })
    part.scrollIntoView({ block: 'nearest' })
    part.dataset.flash = 'focus'
    setTimeout(() => { if (part.dataset.flash === 'focus') delete part.dataset.flash }, 1600)
  }, [shown, baseId])
  const onCite = useCallback((cite: Cite) => {
    if (cite.kind === 'query') {
      jumpTo.current = cite.alias
      setElsewhere(null)
      setListOpen(true)
      setShown((s) => ({ ...s, [cite.alias]: cite.cell }))
      return
    }
    const found = onLocate?.(cite.seg, cite.doc) ?? false
    setElsewhere(found ? null : sourceOf(cite.seg, cite.doc) || cite.seg.text)
  }, [onLocate])
  const who = inquiry.actor || INQUIRY_TEXT.unsigned
  const when = inquiry.at != null ? formatTime(inquiry.at) : ''
  const answer = inquiry.answer
  const limit = answer.limit ? INQUIRY_TEXT.limit[answer.limit] ?? INQUIRY_TEXT.limitOther : ''
  const quote = inquiry.quote && inquiry.quote !== groupQuote ? inquiry.quote : null
  // 一次模型调用都没有（没配好模型、当日金额已到上限）就不写花费，写「$0」像是调用过
  const calls = typeof inquiry.usage?.calls === 'number' ? inquiry.usage.calls : null
  const cost = typeof inquiry.usage?.cost_usd === 'number' && (inquiry.usage.cost_usd > 0 || !!calls) ? inquiry.usage.cost_usd : null
  const answered = !!answer.text || !!answer.doc_artifact
  return (
    <article ref={focusRef} tabIndex={-1} className={`min-w-0 space-y-1 text-2xs leading-relaxed ${FOCUS_RING}`}
             data-inquiry-entry={inquiry.seq ?? ''} data-inquiry-recorded={recorded ? 'true' : 'false'}
             data-inquiry-ok={answer.ok ? 'true' : 'false'} aria-label={INQUIRY_TEXT.entryLabel(inquiry.question)}>
      <div className="flex min-w-0 items-start gap-1.5">
        <Tag>{INQUIRY_TEXT.q}</Tag>
        <div className="min-w-0 flex-1">
          <p className="whitespace-pre-wrap font-medium text-fg [overflow-wrap:anywhere]" data-inquiry-question="">{inquiry.question}</p>
          <p className="tnum text-faint" data-inquiry-meta="">
            <span title={inquiry.at != null ? formatDateTime(inquiry.at) : undefined}>{[who, when].filter(Boolean).join(' · ')}</span>
            {inquiry.post_seal && <> · <span data-inquiry-post-seal="">{INQUIRY_TEXT.postSeal}</span></>}
          </p>
          {quote && (
            <p className="mt-0.5 text-dim [overflow-wrap:anywhere]" data-inquiry-quote="">{INQUIRY_TEXT.quoteLine(quote)}</p>
          )}
        </div>
      </div>
      {/* 没写进运行记录：醒目地说，它只留在这一页，不随运行封存 */}
      {!recorded && (
        <p className="flex items-start gap-1 rounded border px-1.5 py-1 font-semibold" role="note" data-inquiry-unrecorded=""
           style={{ color: 'var(--st-waiting)', borderColor: 'color-mix(in srgb, var(--st-waiting) 45%, transparent)' }}>
          <AlertTriangle size={11} aria-hidden className="mt-[3px] shrink-0" />
          <span className="min-w-0 [overflow-wrap:anywhere]">
            {INQUIRY_TEXT.notRecorded}{note ? `：${note}` : ''}
            <span className="block font-normal">{INQUIRY_TEXT.notRecordedHint}</span>
          </span>
        </p>
      )}
      <div className="flex min-w-0 items-start gap-1.5">
        <Tag>{INQUIRY_TEXT.a}</Tag>
        <div className="min-w-0 flex-1 space-y-1">
          <AnswerBody inquiry={inquiry} onCite={onCite} />
          {elsewhere != null && (
            <p className="text-dim" data-inquiry-elsewhere="">{INQUIRY_TEXT.reportSource(elsewhere)}</p>
          )}
          {answer.ok && limit && (
            <p style={{ color: 'var(--st-waiting)' }} data-inquiry-limit={answer.limit}>{INQUIRY_TEXT.answerIncomplete(limit)}</p>
          )}
          {!answer.ok && (
            <p style={{ color: 'var(--st-waiting)' }} data-inquiry-failed={answer.limit ?? ''}>
              {[INQUIRY_TEXT.failed(limit || null), answer.error].filter(Boolean).join('：')}
            </p>
          )}
        </div>
      </div>
      <QueryList runId={runId} baseId={baseId} queries={inquiry.queries} shown={shown} open={listOpen} onOpen={setListOpen}
                 onToggle={(alias) => setShown((s) => {
                   const next = { ...s }
                   if (alias in next) delete next[alias]
                   else next[alias] = null
                   return next
                 })} />
      {/* 回答是核查模型给的、不是系统核对：有回答才说 */}
      {answered && (
        <p className="text-faint" data-inquiry-judged="">
          {INQUIRY_TEXT.answeredBy(inquiry.model)}
          {cost != null && <span className="tnum"> · {INQUIRY_TEXT.cost(formatCost(cost))}</span>}
        </p>
      )}
    </article>
  )
}

/** 正在回答的那一条：问题、转圈和等了多久。回答期间审批卡的批准 / 驳回暂停（审批卡读 useInquiryBusy） */
function PendingView({ pending, focusRef }: { pending: Pending; focusRef: (el: HTMLElement | null) => void }) {
  const now = useTicker(1000)
  const waited = Math.max(0, Math.floor((now - pending.startedAt) / 1000))
  return (
    <div ref={focusRef} tabIndex={-1} className={`min-w-0 space-y-1 text-2xs leading-relaxed ${FOCUS_RING}`} data-inquiry-pending=""
         aria-busy="true">
      <div className="flex min-w-0 items-start gap-1.5">
        <Tag>{INQUIRY_TEXT.q}</Tag>
        <p className="min-w-0 flex-1 whitespace-pre-wrap font-medium text-fg [overflow-wrap:anywhere]">{pending.question}</p>
      </div>
      <p className="flex items-center gap-1.5 text-dim" role="status">
        <Spinner size={11} />
        <span>{INQUIRY_TEXT.pending}</span>
        {waited >= 3 && <span className="tnum text-faint">· {INQUIRY_TEXT.waited(waited)}</span>}
      </p>
    </div>
  )
}

/**
 * 提问框：问的是哪一句（或哪段原文、整份报告）、问题、提交。Esc 关掉、焦点回到点开它的那个「质询」；Ctrl / ⌘ + 回车提交。
 * 字数超了、空着不让提交；这次运行另有一条在回答时也不让（服务端同样会拒绝）
 */
function Composer({ runId, report, draft, busy, postSeal, fallback }: {
  runId: string
  report: string
  draft: Draft
  busy: boolean
  postSeal: boolean
  fallback: RefObject<HTMLButtonElement | null>
}) {
  const key = inquiryKey(runId, report)
  const edit = useInquiry((s) => s.edit)
  const close = useInquiry((s) => s.close)
  const submit = useInquiry((s) => s.submit)
  const id = useId()
  const area = useRef<HTMLTextAreaElement>(null)
  const form = useRef<HTMLFormElement>(null)
  // 每点一次「质询」（换了一句也算）都把焦点送进输入框，并把提问框滚进视野
  useEffect(() => {
    area.current?.focus({ preventScroll: true })
    form.current?.scrollIntoView({ block: 'nearest' })
  }, [draft.nonce])
  const chars = [...draft.question].length
  const over = chars > QUESTION_MAX
  const empty = !draft.question.trim()
  const cut = !!draft.target.quote && [...draft.target.quote].length > QUOTE_MAX
  const target = draft.target.quote ? 'quote' : draft.target.unit ? 'unit' : 'report'
  const shown = draft.target.quote ?? draft.target.text
  const dismiss = () => {
    close(key)
    const back = openers.get(key)
    openers.delete(key)
    requestAnimationFrame(() => {
      if (back?.isConnected) back.focus({ preventScroll: true })
      else fallback.current?.focus({ preventScroll: true })
    })
  }
  const onKeyDown = (e: ReactKeyboardEvent) => {
    if (isComposing(e)) return
    if (e.key === 'Escape') {
      // 截住：画布上的审批浮层、证据面板也听 Esc，关提问框不该连带把它们收起
      e.preventDefault()
      e.stopPropagation()
      dismiss()
    } else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault()
      if (!empty && !over && !busy) void submit(runId, report)
    }
  }
  const problem = draft.error
  return (
    <form ref={form} className="min-w-0 space-y-1.5 rounded border bg-panel p-2" data-inquiry-composer={target}
          onKeyDown={onKeyDown} onSubmit={(e) => { e.preventDefault(); if (!empty && !over && !busy) void submit(runId, report) }}>
      <div className="text-2xs font-medium text-faint" id={`${id}-target`}>{INQUIRY_TEXT.target[target]}</div>
      {shown && (
        <blockquote className="line-clamp-3 border-l-2 pl-2 text-2xs leading-relaxed text-dim [overflow-wrap:anywhere]"
                    data-inquiry-target="" title={shown}>
          {shown}
        </blockquote>
      )}
      {cut && <p className="text-2xs" style={{ color: 'var(--st-waiting)' }} data-inquiry-quote-cut="">{INQUIRY_TEXT.quoteCut(QUOTE_MAX)}</p>}
      <label htmlFor={`${id}-q`} className="sr-only">{INQUIRY_TEXT.questionLabel}</label>
      <textarea ref={area} id={`${id}-q`} className="field text-xs" rows={3} value={draft.question}
                placeholder={INQUIRY_TEXT.placeholder} data-inquiry-input=""
                aria-describedby={`${id}-target ${id}-hint${problem ? ` ${id}-error` : ''}`}
                aria-invalid={over || problem?.field === 'question' ? true : undefined}
                onChange={(e) => edit(key, e.target.value)} />
      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
        <span className="tnum text-2xs" data-inquiry-chars={chars}
              style={{ color: over ? 'var(--st-failed)' : 'var(--text-faint)' }}>
          {INQUIRY_TEXT.chars(chars, QUESTION_MAX)}
        </span>
        <span className="hidden text-2xs text-faint sm:inline">{INQUIRY_TEXT.keys}</span>
        <span className="flex-1" />
        <button type="button" className="btn btn-xs btn-ghost" onClick={dismiss}>{INQUIRY_TEXT.cancel}</button>
        <button type="submit" className="btn btn-xs btn-primary" disabled={empty || over || busy} data-inquiry-submit="">
          {INQUIRY_TEXT.submit}
        </button>
      </div>
      <p id={`${id}-hint`} className="text-2xs leading-relaxed text-dim">
        {busy ? INQUIRY_TEXT.busyOther : postSeal ? INQUIRY_TEXT.hintPostSeal : INQUIRY_TEXT.hint}
      </p>
      {over && <p className="text-2xs" style={{ color: 'var(--st-failed)' }}>{INQUIRY_TEXT.tooLong(QUESTION_MAX)}</p>}
      {problem && (
        <p id={`${id}-error`} role="alert" className="text-2xs leading-relaxed [overflow-wrap:anywhere]" data-inquiry-error={problem.code}
           style={{ color: 'var(--st-failed)' }}>
          {problem.message}
        </p>
      )}
    </form>
  )
}

// ---------------------------------------------------------------- 回答

/** 回答里点了一个有出处的数字：质询自己的查询（看快照里那一格），或者报告已有的出处（交给页面在报告里打开） */
type Cite =
  | { kind: 'query'; alias: string; cell: { row: number; column: string } | null }
  | { kind: 'report'; seg: EvidenceSegment; doc: EvidenceDocData }

/**
 * 回答正文。有回答文档时按文档画：数字的出处照报告里的样子（有出处的实线、裸数字点状线加「无证据」）；文档取到之前、
 * 取不到时按渲染后的正文显示，数字照写，只是不标出处
 */
function AnswerBody({ inquiry, onCite }: { inquiry: Inquiry; onCite: (c: Cite) => void }) {
  const artifact = inquiry.answer.doc_artifact ?? ''
  const slot = useEvidence((s) => (artifact ? s.docs[artifact] : undefined))
  const loadDoc = useEvidence((s) => s.loadDoc)
  useEffect(() => { if (artifact) void loadDoc(artifact) }, [artifact, loadDoc])
  const doc = slot?.status === 'ok' ? slot.data ?? null : null
  // 别名对得上、工件也对得上（工件缺的按别名）才算这次质询自己的查询
  const own = useMemo(() => {
    const map = new Map<string, InquiryQuery>()
    for (const q of inquiry.queries) if (q.alias) map.set(q.alias, q)
    return map
  }, [inquiry.queries])
  if (doc && (doc.blocks ?? []).length) {
    return <AnswerDoc doc={doc} own={own} onCite={onCite} />
  }
  if (inquiry.answer.text) {
    return <div className="min-w-0 text-[11.5px]" data-inquiry-answer-text=""><Markdown text={inquiry.answer.text} dense /></div>
  }
  // 没答成的下面另有一行说原因，这里不再说一遍「没有给出回答」
  if (!inquiry.answer.ok) return null
  return <p className="text-faint" data-inquiry-answer-empty="">{INQUIRY_TEXT.noAnswer}</p>
}

/** 列表项从哪一个片段开始：同 EvidenceDoc（行首的「- 」「1. 」是结构片段） */
const ITEM_START = /(^|\n)[ \t]*([-*+]|\d+[.)])[ \t]+$/

function AnswerDoc({ doc, own, onCite }: {
  doc: EvidenceDocData; own: Map<string, InquiryQuery>; onCite: (c: Cite) => void
}) {
  return (
    <div className="min-w-0 space-y-1.5 text-[11.5px] leading-relaxed" data-inquiry-answer-doc="">
      {doc.blocks.map((block, b) => <AnswerBlock key={block.id ?? b} block={block} doc={doc} own={own} onCite={onCite} />)}
    </div>
  )
}

/** 一块：外观借 Markdown 的 BlockView，和报告文档、普通答案长得一样（块的分法同 EvidenceDoc 的 DocBlock） */
function AnswerBlock({ block, doc, own, onCite }: {
  block: EvidenceBlock; doc: EvidenceDocData; own: Map<string, InquiryQuery>; onCite: (c: Cite) => void
}) {
  const units = block.units ?? []
  const unit = (u: EvidenceUnit, code = false) => <AnswerUnit key={u.id} unit={u} doc={doc} own={own} code={code} onCite={onCite} />
  switch (block.type) {
    case 'heading':
      return <BlockView dense shape={{ kind: 'h', level: block.level ?? 3, body: units.map((u) => unit(u)) }} />
    case 'hr':
      return <BlockView dense shape={{ kind: 'hr' }} />
    case 'quote':
      return <BlockView dense shape={{ kind: 'quote', body: units.map((u) => unit(u)) }} />
    case 'code':
      return <BlockView dense shape={{ kind: 'code', lang: block.lang ?? '', code: units.map(unitText).join(''), body: units.map((u) => unit(u, true)) }} />
    case 'list': {
      const items: { key: string; depth: number; units: EvidenceUnit[] }[] = []
      for (const u of units) {
        const lead = u.segments?.[0]
        if (!items.length || (lead?.kind === 'structural' && ITEM_START.test(lead.text))) {
          items.push({ key: u.id, depth: u.depth ?? 0, units: [u] })
        } else items[items.length - 1].units.push(u)
      }
      return (
        <BlockView dense shape={{
          kind: 'list', ordered: !!block.ordered, start: block.start, fitMarkers: true,
          items: items.map((it) => ({ key: it.key, depth: it.depth, body: it.units.map((u) => unit(u)) })),
        }} />
      )
    }
    case 'table': {
      const cells = new Map<string, EvidenceUnit[]>()
      let cols = 0
      const rowIds: number[] = []
      for (const u of units) {
        const loc = u.loc ?? { row: 0, col: 0 }
        cells.set(`${loc.row}:${loc.col}`, [...(cells.get(`${loc.row}:${loc.col}`) ?? []), u])
        cols = Math.max(cols, loc.col + 1)
        if (loc.row >= 0 && !rowIds.includes(loc.row)) rowIds.push(loc.row)
      }
      rowIds.sort((a, b) => a - b)
      const plain = (r: number, c: number) => (cells.get(`${r}:${c}`) ?? []).map(unitText).join('')
      const node = (r: number, c: number) => (cells.get(`${r}:${c}`) ?? []).map((u) => unit(u))
      const head = Array.from({ length: cols }, (_, c) => plain(-1, c))
      const body = rowIds.map((r) => Array.from({ length: cols }, (_, c) => plain(r, c)))
      return (
        <BlockView dense shape={{
          kind: 'table',
          head: Array.from({ length: cols }, (_, c) => node(-1, c)),
          rows: rowIds.map((r) => Array.from({ length: cols }, (_, c) => node(r, c))),
          numeric: numericColumns(head, body),
          csv: () => [head, ...body].map((r) => r.join(',')).join('\n'),
        }} />
      )
    }
    default:
      return <BlockView dense shape={{ kind: 'p', body: units.map((u) => unit(u)) }} />
  }
}

/** 片段的颜色写成变量交给 index.css 的 .ev-seg / .ev-mark（同 EvidenceDoc） */
const evStyle = (state: EvidenceStateCode) => ({
  '--ev-line': EVIDENCE_STATE[state].decoration, '--ev-soft': EVIDENCE_STATE[state].soft,
}) as CSSProperties

/**
 * 回答里有出处的一个数字指向哪：引用的是质询自己某次查询里的一格（目录条目的工件就是那次查询的快照，工件缺的按别名
 * 认），还是报告已有的出处（口径卡上的指标、报告的查询……）
 */
function citeOf(seg: EvidenceSegment, doc: EvidenceDocData, own: Map<string, InquiryQuery>): Cite | null {
  const cite = seg.cite
  if (!cite || segmentState(seg) !== 'deterministic') return null
  const entry = cite.alias ? doc.catalog?.[cite.alias] : undefined
  const query = cite.alias ? own.get(cite.alias) : undefined
  const artifact = typeof entry?.artifact === 'string' ? entry.artifact : ''
  const mine = !!query && (!artifact || !query.artifact || artifact === query.artifact)
  if (mine && (cite.kind === 'cell' || entry?.kind === 'query')) {
    const loc = cite.locator
    const cell = typeof loc?.row === 'number' && typeof loc.column === 'string' ? { row: loc.row, column: loc.column } : null
    return { kind: 'query', alias: cite.alias!, cell }
  }
  return { kind: 'report', seg, doc }
}

/**
 * 一句话：文字按行内 Markdown 渲染（整句算一次样式，同 EvidenceDoc 的 UnitView）。有出处的数字是按钮：质询自己查询里的
 * 后面挂查询编号，点一下展开那次查询的结果、标出那一格；报告已有的出处点一下在报告里打开它的证据。裸数字、疑似不存在的
 * 名称画点状线，句末挂「无证据」「疑似不存在的名称」
 */
function AnswerUnit({ unit, doc, own, code, onCite }: {
  unit: EvidenceUnit; doc: EvidenceDocData; own: Map<string, InquiryQuery>; code: boolean; onCite: (c: Cite) => void
}) {
  const segs = (unit.segments ?? []).filter((s) => s.kind !== 'structural')
  const whole = segs.map((s) => s.text).join('')
  const styles = useMemo(() => (code ? null : inlineStyles(whole)), [whole, code])
  const alerts = new Set<EvidenceStateCode>()
  let pos = 0
  const nodes = segs.map((s) => {
    const from = pos
    pos += s.text.length
    const state = segmentState(s)
    const body = styles ? <StyledSlice text={whole} styles={styles} from={from} to={pos} links={!state} /> : s.text
    const cite = citeOf(s, doc, own)
    if (cite) {
      const source = sourceOf(s, doc)
      return (
        <button key={s.id} type="button" className="ev-seg" data-ev-state="deterministic" data-ev-line="solid"
                data-inquiry-cite={cite.kind === 'query' ? cite.alias : 'report'} style={evStyle('deterministic')} title={source}
                aria-label={cite.kind === 'query' ? INQUIRY_TEXT.cited(s.text, source) : INQUIRY_TEXT.citedReport(s.text, source)}
                onClick={() => onCite(cite)}>
          {body}
          {cite.kind === 'query' && (
            // inline-block：不吃上面那条下划线
            <sup className="mono ml-px inline-block select-none text-[9px] font-semibold text-faint" aria-hidden>{cite.alias}</sup>
          )}
        </button>
      )
    }
    if (state === 'none' || state === 'suspect') {
      alerts.add(state)
      return (
        <span key={s.id} className="ev-mark" data-ev-state={state} data-ev-line="dotted" style={evStyle(state)}
              title={segmentLabel(s, doc)}>
          {body}<span className="sr-only">（{EVIDENCE_STATE_LABEL[state]}）</span>
        </span>
      )
    }
    return <span key={s.id}>{s.strong ? <strong className="font-semibold">{body}</strong> : body}</span>
  })
  return (
    <>
      {nodes}
      {!code && [...alerts].map((state) => (
        <span key={state} className="ev-tag" aria-hidden="true" title={EVIDENCE_STATE[state].hint} data-ev-tag={state}>
          {EVIDENCE_STATE[state].glyph}{EVIDENCE_STATE_LABEL[state]}
        </span>
      ))}
    </>
  )
}

// ---------------------------------------------------------------- 查询

/** 核查模型执行过的查询：SQL、行数、截断；每次可以展开结果（按质询工件接口取快照，遮罩列已遮掉） */
function QueryList({ runId, baseId, queries, shown, open, onOpen, onToggle }: {
  runId: string
  baseId: string
  queries: InquiryQuery[]
  shown: Record<string, { row: number; column: string } | null>
  open: boolean
  onOpen: (open: boolean) => void
  onToggle: (alias: string) => void
}) {
  const listId = useId()
  if (!queries.length) return <p className="text-faint" data-inquiry-queries="0">{INQUIRY_TEXT.noQueries}</p>
  return (
    <section className="min-w-0" data-inquiry-queries={queries.length}>
      <button type="button" className="flex items-center gap-1 rounded text-faint hover:text-fg focus-visible:outline focus-visible:outline-2 focus-visible:outline-[color:var(--accent)]"
              aria-expanded={open} aria-controls={open ? listId : undefined} onClick={() => onOpen(!open)}
              data-inquiry-queries-toggle="">
        <ChevronRight size={11} aria-hidden style={{ transform: open ? 'rotate(90deg)' : 'none', transition: 'transform .15s' }} />
        {INQUIRY_TEXT.queriesHead(queries.length)}
      </button>
      {open && (
        <div id={listId} className="mt-1 space-y-1.5">
          {queries.map((q, i) => (
            <QueryCard key={`${q.alias}-${i}`} id={`${baseId}-q-${q.alias}`} runId={runId} query={q}
                       open={q.alias in shown} cell={shown[q.alias] ?? null} onToggle={() => onToggle(q.alias)} />
          ))}
        </div>
      )}
    </section>
  )
}

function QueryCard({ id, runId, query, open, cell, onToggle }: {
  id: string; runId: string; query: InquiryQuery; open: boolean; cell: { row: number; column: string } | null; onToggle: () => void
}) {
  const resultId = useId()
  return (
    <div id={id} data-inquiry-query={query.alias}
         className="min-w-0 rounded border p-1.5 data-[flash]:outline data-[flash]:outline-solid data-[flash]:outline-2 data-[flash]:outline-offset-2 data-[flash=focus]:outline-[color:var(--accent)]">
      <div className="mb-1 flex min-w-0 items-center gap-2">
        {/* 从回答里的数字跳过来时焦点落在这里：得看得见落在哪 */}
        <span tabIndex={-1} data-inquiry-query-head=""
              className={`min-w-0 flex-1 truncate text-2xs font-medium text-faint ${FOCUS_RING}`}>
          {EVIDENCE_TEXT.query(query.alias)}
          {query.source && <span className="mono ml-1.5 font-normal">{query.source}</span>}
        </span>
        {query.sql && <CopyChip label={EVIDENCE_TEXT.copySql} text={() => query.sql} />}
      </div>
      {query.sql && (
        <pre className="mono mb-1 max-h-28 overflow-auto whitespace-pre-wrap rounded bg-panel px-1.5 py-1 text-2xs leading-relaxed text-dim [overflow-wrap:anywhere]"
             data-inquiry-sql="">
          {query.sql}
        </pre>
      )}
      <div className="tnum flex flex-wrap items-center gap-x-2 gap-y-1 text-2xs text-faint">
        {query.row_count != null && <span data-inquiry-rows={query.row_count}>{INQUIRY_TEXT.rows(query.row_count)}</span>}
        {query.truncated && <span data-inquiry-truncated="" style={{ color: 'var(--st-waiting)' }}>{INQUIRY_TEXT.truncated}</span>}
        <span className="flex-1" />
        {query.artifact && (
          <button type="button" className="btn btn-xs btn-ghost" aria-expanded={open} aria-controls={open ? resultId : undefined}
                  onClick={onToggle} data-inquiry-result-toggle="">
            {open ? INQUIRY_TEXT.hideResult : INQUIRY_TEXT.showResult}
          </button>
        )}
      </div>
      {open && query.artifact && (
        <div id={resultId} className="mt-1.5 min-w-0">
          <Snapshot runId={runId} artifact={query.artifact} cell={cell} />
        </div>
      )}
    </div>
  )
}

/** 查询快照：内容寻址、不可变，取到的永久缓存；出错的不缓存成定论，下次展开再取 */
const snapshots = new Map<string, Promise<InquirySnapshot>>()

function loadSnapshot(runId: string, artifact: string): Promise<InquirySnapshot> {
  const key = `${runId} ${artifact}`
  let pending = snapshots.get(key)
  if (!pending) {
    pending = api.inquiry.snapshot(runId, artifact).then((body) => {
      const snap = snapshotOf(body)
      if (!snap) throw new Error(INQUIRY_TEXT.snapshotFailed)
      return snap
    })
    pending.catch(() => { snapshots.delete(key) })
    snapshots.set(key, pending)
  }
  return pending
}

/** 一次查询的结果：整份快照的表格，从回答里的数字点过来的那一格标出来（滚到它那里） */
function Snapshot({ runId, artifact, cell }: { runId: string; artifact: string; cell: { row: number; column: string } | null }) {
  const [state, setState] = useState<{ status: 'loading' | 'ok' | 'error'; data?: InquirySnapshot; error?: unknown }>({ status: 'loading' })
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    let alive = true
    setState((s) => (s.status === 'ok' ? s : { status: 'loading' }))
    loadSnapshot(runId, artifact).then(
      (data) => { if (alive) setState({ status: 'ok', data }) },
      (error) => { if (alive) setState({ status: 'error', error }) },
    )
    return () => { alive = false }
  }, [runId, artifact, attempt])
  const box = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (state.status !== 'ok' || !cell) return
    box.current?.querySelector<HTMLElement>('[data-highlight="cell"], [data-highlight="row"]')?.scrollIntoView({ block: 'nearest' })
  }, [state.status, cell])
  if (state.status === 'loading') {
    return <p className="flex items-center gap-1.5 text-2xs text-faint" data-inquiry-snapshot="loading"><Spinner size={11} /> {INQUIRY_TEXT.snapshotLoading}</p>
  }
  if (state.status === 'error' || !state.data) {
    const why = state.error instanceof ApiError && state.error.kind === 'http' ? state.error.message : ''
    return (
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-2xs" data-inquiry-snapshot="error">
        <span role="alert" style={{ color: 'var(--st-waiting)' }}>{[INQUIRY_TEXT.snapshotFailed, why].filter(Boolean).join('：')}</span>
        <button type="button" className="btn btn-xs" onClick={() => setAttempt((n) => n + 1)}>{INQUIRY_TEXT.retry}</button>
      </div>
    )
  }
  const snap = state.data
  return (
    <div ref={box} className="min-w-0 space-y-1" data-inquiry-snapshot="ok">
      {/* 截断在查询卡上已经写了，表格脚注不再说一遍 */}
      <ResultTable full table={{ columns: snap.columns, rows: snap.rows, truncated: false, total: snap.total ?? undefined }}
                   highlight={cell ? { rows: [cell.row], cols: [cell.column], cells: [[cell.row, cell.column]] } : undefined}
                   masked={snap.masked} />
      {snap.masked.length > 0 && (
        <p className="text-2xs text-faint" data-inquiry-masked="">{INQUIRY_TEXT.masked(snap.masked.join('、'))}</p>
      )}
    </div>
  )
}

// ---------------------------------------------------------------- 报告里打开已有的出处

/**
 * 回答里引用了报告已有的出处（口径卡上的指标、报告的查询……）：在报告文档里找引用同一出处的片段，打开它的证据面板。
 * 按 eid 认（同一格、同一个指标在两份文档里是同一个标识），没有 eid 的按原始引用认。找到返回片段 id，否则 null
 */
export function reportSegmentFor(report: EvidenceDocData | null | undefined, seg: EvidenceSegment): string | null {
  const eid = seg.cite?.eid
  const ref = seg.ref
  for (const b of report?.blocks ?? []) {
    for (const u of b.units ?? []) {
      for (const s of u.segments ?? []) {
        if (segmentState(s) !== 'deterministic') continue
        if (eid ? s.cite?.eid === eid : !!ref && s.ref === ref) return s.id
      }
    }
  }
  return null
}

/** 运行详情的报告：在这份文档里打开回答引用的那一处，找不到返回 false（同 ApprovalReport 的做法，文档已经在页面上） */
export function locateIn(handle: EvidenceDocHandle | null | undefined, report: EvidenceDocData | null | undefined,
  seg: EvidenceSegment): boolean {
  const id = reportSegmentFor(report, seg)
  return !!id && !!handle && handle.open(id, { focus: true })
}
