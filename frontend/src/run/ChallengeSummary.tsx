import { Fragment, useCallback, useEffect, useId, useMemo, useState, type CSSProperties, type ReactNode } from 'react'
import { AlertTriangle, ChevronRight, ShieldAlert } from 'lucide-react'
import { api } from '../api/client'
import { EVIDENCE_STATE, segmentLabel, segmentState, sourceOf, unitText, type EvidenceStateCode } from '../lib/evidence'
import { CHALLENGE_TEXT, EVIDENCE_STATE_LABEL, EVIDENCE_TEXT } from '../lib/terms'
import { asDoc, useEvidence } from '../store/evidence'
import type {
  ApprovalChallenge, ApprovalReport, ChallengeArtifact, ChallengeClaim, ChallengeCounts, ChallengeItem, ChallengeQuery,
  ChallengeVerdict, EvidenceBlock, EvidenceDocData, EvidenceSegment, EvidenceUnit,
} from '../types'
import { BlockView, CopyChip, Markdown, StyledSlice, inlineStyles, numericColumns } from './Markdown'

export interface ChallengeSummaryProps {
  /** 这张审批卡审的报告（payload.reports，和 ApprovalReports 拿到的是同一份） */
  reports: ApprovalReport[]
  runId: string
}

/**
 * 审批卡里反方审稿的摘要，紧跟在报告核对概况（ApprovalReports）后面：一行概况（四种判断各几句），逐句列出结论原文和判断，
 * 点开一句再按 challenge_artifact 取完整内容，看反方的理由和它执行过的查询。
 *
 * 判断是反方模型给的，不是系统核对：颜色照结论句裁判的那一套，「经受住」也不用确定性的绿。
 * 「出具会降档」只在正式运行、被推翻的结论按降档处理时才写——只标注的不写降档，探索运行不出具，和出具有关的话一句不说。
 * 运行类别不由审批卡传进来，按运行 id 从证据 store 取（loadRunClass），取到之前不说。
 *
 * 审的报告都没有经过反方审稿时什么都不画，也不发任何请求，卡片和以前一样。
 */
export function ChallengeSummary({ reports, runId }: ChallengeSummaryProps) {
  const reviewed = useMemo(() => reports.flatMap((r) => {
    const challenge = challengeOf(r)
    return challenge ? [{ report: r, challenge }] : []
  }), [reports])
  if (!reviewed.length) return null
  return <ChallengeList reviewed={reviewed} runId={runId} />
}

function ChallengeList({ reviewed, runId }: {
  reviewed: { report: ApprovalReport; challenge: ApprovalChallenge }[]; runId: string
}) {
  const runClass = useRunClass(runId)
  return (
    <>
      {reviewed.map(({ report, challenge }) => (
        <ChallengeBlock key={report.node_id} report={report} challenge={challenge} runClass={runClass} />
      ))}
    </>
  )
}

// ---------------------------------------------------------------- 认 payload

const VERDICTS: readonly ChallengeVerdict[] = ['stands', 'weakened', 'refuted', 'inconclusive']
const isVerdict = (v: unknown): v is ChallengeVerdict => typeof v === 'string' && (VERDICTS as readonly string[]).includes(v)

/** 颜色同结论句裁判（lib/evidence 的 EVIDENCE_STATE）：经受住同「有依据」，被削弱同「部分有依据」，被推翻同「证据相矛盾」 */
const VERDICT_COLOR: Record<ChallengeVerdict, string> = {
  stands: 'var(--st-running)',
  weakened: 'var(--st-waiting)',
  refuted: 'var(--st-failed)',
  inconclusive: 'var(--st-cancelled)',
}

const verdictLabel = (v: string) => (isVerdict(v) ? CHALLENGE_TEXT.verdict[v] : CHALLENGE_TEXT.verdictUnknown(v))

function isItem(x: unknown): x is ChallengeItem {
  return !!x && typeof x === 'object' && typeof (x as ChallengeItem).text === 'string' && typeof (x as ChallengeItem).verdict === 'string'
}

const count = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) && v > 0 ? Math.floor(v) : 0)

/**
 * payload.reports[i].challenge 认不认得：要么有计数、要么有条目。计数缺了按条目数（条目最多 10 条，计数才是全部）；
 * 条目缺了 unit 的按序号补一个键，只用来区分行
 */
function challengeOf(report: ApprovalReport): ApprovalChallenge | null {
  const raw = report.challenge as unknown
  if (!raw || typeof raw !== 'object') return null
  const c = raw as Record<string, unknown>
  const items = (Array.isArray(c.items) ? c.items.filter(isItem) : [])
    .map((it, i) => ({ unit: typeof it.unit === 'string' && it.unit ? it.unit : `#${i}`, text: it.text, verdict: it.verdict }))
  // 计数里一个数都没有（格式不对）也按条目数，免得概况写四个 0、下面却列着几句
  const given = c.counts && typeof c.counts === 'object' && VERDICTS.some((v) => typeof (c.counts as any)[v] === 'number')
    ? c.counts as Record<string, unknown> : null
  const counts = Object.fromEntries(VERDICTS.map((v) => [v, given
    ? count(given[v]) : items.filter((it) => it.verdict === v).length])) as ChallengeCounts
  if (!given && !items.length) return null
  return {
    counts, items, artifact: typeof c.artifact === 'string' ? c.artifact : '',
    on_refuted: typeof c.on_refuted === 'string' ? c.on_refuted : undefined,
  }
}

function useRunClass(runId: string): 'formal' | 'exploratory' | null {
  const slot = useEvidence((s) => (runId ? s.runClasses[runId] : undefined))
  const load = useEvidence((s) => s.loadRunClass)
  useEffect(() => { if (runId) void load(runId) }, [runId, load])
  if (slot?.status !== 'ok') return null
  return slot.data === 'formal' || slot.data === 'exploratory' ? slot.data : null
}

// ---------------------------------------------------------------- 取工件

/** 工件 id → 取回的内容。工件内容寻址、不可变，取到的永久缓存；出错的不缓存成定论，下次点开再取 */
const fetched = new Map<string, Promise<ChallengeArtifact>>()

function loadChallenge(id: string): Promise<ChallengeArtifact> {
  let pending = fetched.get(id)
  if (!pending) {
    pending = api.artifact(id).then((res) => {
      const content = asChallenge(res?.content)
      if (!content) throw new Error(CHALLENGE_TEXT.failed)
      return content
    })
    pending.catch(() => { fetched.delete(id) })
    fetched.set(id, pending)
  }
  return pending
}

const text = (v: unknown) => (typeof v === 'string' ? v : '')

function asQuery(x: unknown): ChallengeQuery | null {
  if (!x || typeof x !== 'object') return null
  const q = x as Record<string, unknown>
  return {
    alias: text(q.alias), artifact: text(q.artifact), sql: text(q.sql),
    row_count: typeof q.row_count === 'number' && Number.isFinite(q.row_count) ? q.row_count : null,
    truncated: q.truncated === true, source: typeof q.source === 'string' && q.source ? q.source : null,
  }
}

/** 工件内容认不认得：至少得有 claims 列表；每一句缺的字段按空的补，界面照实少显示 */
function asChallenge(content: unknown): ChallengeArtifact | null {
  if (!content || typeof content !== 'object' || !Array.isArray((content as ChallengeArtifact).claims)) return null
  const c = content as Record<string, any>
  const claims: ChallengeClaim[] = (c.claims as unknown[]).filter(isItem).map((it) => {
    const raw = it as unknown as Record<string, unknown>
    const doc = raw.rationale_doc
    return {
      unit: text(raw.unit), text: it.text, verdict: it.verdict, rationale: text(raw.rationale),
      rationale_doc: typeof doc === 'string' || (doc && typeof doc === 'object') ? doc as ChallengeClaim['rationale_doc'] : null,
      queries: (Array.isArray(raw.queries) ? raw.queries : []).map(asQuery).filter((q): q is ChallengeQuery => !!q),
    }
  })
  return { report: text(c.report), doc_artifact: text(c.doc_artifact), claims, model: typeof c.model === 'string' && c.model ? c.model : null }
}

type Fetch = { status: 'idle' | 'loading' | 'ok' | 'error'; data?: ChallengeArtifact }
/** retry 没有时不给重试：审批里根本没带工件 id，再取也是一样 */
type Detail = Fetch & { retry?: () => void }

/** 整份反方审稿：want 为真（点开过一句、要看全部）之后才取，一张卡里各句共用这一次 */
function useChallengeArtifact(id: string, want: boolean): Detail {
  const [state, setState] = useState<Fetch>({ status: 'idle' })
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    if (!want || !id) return
    let alive = true
    setState((s) => (s.status === 'ok' ? s : { status: 'loading' }))
    loadChallenge(id).then(
      (data) => { if (alive) setState({ status: 'ok', data }) },
      () => { if (alive) setState({ status: 'error' }) },
    )
    return () => { alive = false }
  }, [id, want, attempt])
  const retry = useCallback(() => setAttempt((n) => n + 1), [])
  return id ? { ...state, retry } : { status: 'error' }
}

/** 这一句在工件里的完整内容：按 unit 认，unit 对不上（老服务端没给）再按原文认 */
function claimOf(artifact: ChallengeArtifact | undefined, item: ChallengeItem): ChallengeClaim | undefined {
  if (!artifact) return undefined
  return artifact.claims.find((c) => c.unit && c.unit === item.unit) ?? artifact.claims.find((c) => c.text === item.text)
}

// ---------------------------------------------------------------- 卡片

function ChallengeBlock({ report, challenge, runClass }: {
  report: ApprovalReport; challenge: ApprovalChallenge; runClass: 'formal' | 'exploratory' | null
}) {
  const [open, setOpen] = useState<Set<string>>(() => new Set())
  const [all, setAll] = useState(false)
  const detail = useChallengeArtifact(challenge.artifact, all || open.size > 0)
  const { counts, items } = challenge
  const total = VERDICTS.reduce((n, v) => n + counts[v], 0)
  const refuted = counts.refuted > 0 && runClass === 'formal'
  // 没给 on_refuted（老服务端）按降档；报错的节点到不了审批，不会是 fail
  const flagged = challenge.on_refuted === 'flag'
  // 条目之外的几句（条目最多 10 条）：要看全部时从工件里补上，排在后面，顺序照工件
  const extra = all && detail.data
    ? detail.data.claims.filter((c) => !items.some((it) => (c.unit && c.unit === it.unit) || c.text === it.text))
    : []
  const rows: ChallengeItem[] = [...items, ...extra]
  const hidden = Math.max(0, total - items.length)
  const toggle = (key: string) => setOpen((cur) => {
    const next = new Set(cur)
    if (next.has(key)) next.delete(key)
    else next.add(key)
    return next
  })
  return (
    <div className="mb-2 rounded border bg-bg p-2" data-challenge={report.node_id}>
      <div className="mb-1 flex items-center gap-1 text-2xs font-medium text-faint">
        <ShieldAlert size={11} aria-hidden /> {CHALLENGE_TEXT.head(report.label || report.node_id)}
      </div>
      {total > 0 || items.length ? (
        <div className="tnum flex flex-wrap items-center gap-x-1.5 gap-y-1 text-2xs" data-challenge-counts="">
          {VERDICTS.map((v, i) => (
            <Fragment key={v}>
              {i > 0 && <span className="text-faint" aria-hidden>·</span>}
              <span data-challenge-count={v} style={{ color: counts[v] ? VERDICT_COLOR[v] : 'var(--text-faint)' }}>
                {CHALLENGE_TEXT.count(CHALLENGE_TEXT.verdict[v], counts[v])}
              </span>
            </Fragment>
          ))}
          {refuted && !flagged && (
            <span className="chip shrink-0 font-semibold" data-challenge-degrade="" title={CHALLENGE_TEXT.degradeHint}
                  style={{ color: 'var(--st-failed)', borderColor: 'var(--st-failed)' }}>
              <AlertTriangle size={11} aria-hidden /> {CHALLENGE_TEXT.degrade}
              <span className="sr-only">：{CHALLENGE_TEXT.degradeHint}</span>
            </span>
          )}
        </div>
      ) : (
        <div className="text-2xs text-dim" data-challenge-empty="">{CHALLENGE_TEXT.none}</div>
      )}
      {refuted && flagged && (
        <div className="mt-0.5 text-2xs leading-relaxed text-dim" data-challenge-flag="">{CHALLENGE_TEXT.flagNote}</div>
      )}
      {rows.length > 0 && (
        <ul className="mt-1.5 space-y-0.5" data-challenge-items="">
          {rows.map((item, i) => {
            const key = `${item.unit}\u0000${i}`
            return (
              <ClaimRow key={key} item={item} open={open.has(key)} onToggle={() => toggle(key)}>
                <ClaimDetail item={item} detail={detail} />
              </ClaimRow>
            )
          })}
        </ul>
      )}
      {/* 工件取回来了、却没有条目之外的句子（计数和工件对不上）：没什么可补的，这一行也不留 */}
      {hidden > 0 && !extra.length && challenge.artifact && !(all && detail.status === 'ok') && (
        <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-2xs text-dim" data-challenge-more={hidden}>
          <span>{CHALLENGE_TEXT.more(hidden)}</span>
          {detail.status === 'error' && all ? (
            <>
              <span style={{ color: 'var(--st-waiting)' }} role="alert">{CHALLENGE_TEXT.failed}</span>
              {detail.retry && <button type="button" className="btn btn-xs" onClick={detail.retry}>{CHALLENGE_TEXT.retry}</button>}
            </>
          ) : (
            <button type="button" className="btn btn-xs" disabled={all && detail.status === 'loading'}
                    data-challenge-show-all="" onClick={() => setAll(true)}>
              {all && detail.status === 'loading' ? CHALLENGE_TEXT.loading : CHALLENGE_TEXT.showAll}
            </button>
          )}
        </div>
      )}
      {/* 判断标签看着像结论，得说清是谁判的：整块说一次，工件取回来之后带上模型名 */}
      {(total > 0 || items.length > 0) && (
        <p className="mt-1 text-2xs leading-relaxed text-faint" data-challenge-judged="">
          {CHALLENGE_TEXT.judgedBy(detail.data?.model ?? null)}
        </p>
      )}
    </div>
  )
}

/** 判断标签：文字加颜色（去掉颜色也读得出），不铺底色——浅色主题下底色会把小字的对比度压到 4.5 以下 */
function VerdictTag({ verdict }: { verdict: string }) {
  const color = isVerdict(verdict) ? VERDICT_COLOR[verdict] : 'var(--text-dim)'
  return (
    <span className="chip shrink-0 font-medium" data-challenge-tag={verdict}
          style={{ color, borderColor: `color-mix(in srgb, ${color} 45%, transparent)` }}>
      {verdictLabel(verdict)}
    </span>
  )
}

function ClaimRow({ item, open, onToggle, children }: {
  item: ChallengeItem; open: boolean; onToggle: () => void; children: ReactNode
}) {
  const detailId = useId()
  return (
    <li data-challenge-item={item.unit} data-challenge-verdict={item.verdict}>
      <button type="button" aria-expanded={open} aria-controls={open ? detailId : undefined} onClick={onToggle} data-challenge-toggle=""
              className="flex w-full min-w-0 items-start gap-1.5 rounded px-1 py-0.5 text-left text-2xs leading-relaxed hover:bg-hover focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[color:var(--accent)]">
        <ChevronRight size={11} aria-hidden className="mt-[3px] shrink-0 text-faint"
                      style={{ transform: open ? 'rotate(90deg)' : 'none', transition: 'transform .15s' }} />
        <VerdictTag verdict={item.verdict} />
        {/* 原文取自报告文档，是 Markdown：粗体、行内代码照样画出来；链接不画成链接，按钮里不能再套一个可点的东西 */}
        <span className="min-w-0 flex-1 [overflow-wrap:anywhere]" data-challenge-text="">
          <StyledSlice text={item.text} styles={inlineStyles(item.text)} from={0} to={item.text.length} links={false} />
        </span>
      </button>
      {open && (
        <div id={detailId} className="mb-1 ml-4 mt-1 min-w-0 space-y-2 rounded border p-2" data-challenge-detail={item.unit}>
          {children}
        </div>
      )}
    </li>
  )
}

/** 点开的一句：工件取回来之前说正在加载；取不到给重试；取到了画理由和查询 */
function ClaimDetail({ item, detail }: { item: ChallengeItem; detail: Detail }) {
  const baseId = useId()
  const claim = claimOf(detail.data, item)
  // 理由里的数字点一下：焦点落到那次查询上，描一下边（减少动效时也看得见）
  const jump = useCallback((alias: string) => {
    const part = document.getElementById(`${baseId}-q-${alias}`)
    const head = part?.querySelector<HTMLElement>('[data-challenge-query-head]')
    if (!part || !head) return
    head.focus({ preventScroll: true })
    head.scrollIntoView({ block: 'nearest' })
    part.dataset.flash = 'focus'
    setTimeout(() => { if (part.dataset.flash === 'focus') delete part.dataset.flash }, 1600)
  }, [baseId])
  if (detail.status === 'error') {
    return (
      <div className="flex flex-wrap items-center gap-2 text-2xs" data-challenge-detail-state="error">
        <span role="alert" style={{ color: 'var(--st-waiting)' }}>{CHALLENGE_TEXT.failed}</span>
        {detail.retry && <button type="button" className="btn btn-xs" onClick={detail.retry}>{CHALLENGE_TEXT.retry}</button>}
      </div>
    )
  }
  if (detail.status !== 'ok') {
    return <div className="text-2xs text-faint" data-challenge-detail-state="loading">{CHALLENGE_TEXT.loading}</div>
  }
  if (!claim) return <div className="text-2xs text-dim" data-challenge-detail-state="missing">{CHALLENGE_TEXT.claimMissing}</div>
  const aliases = new Set(claim.queries.map((q) => q.alias))
  return (
    <>
      <section data-challenge-detail-state="ok" data-challenge-rationale="">
        <div className="mb-0.5 text-2xs font-medium text-faint">{CHALLENGE_TEXT.rationale}</div>
        <Rationale claim={claim} onJump={(alias) => (aliases.has(alias) ? jump(alias) : undefined)} />
      </section>
      <section data-challenge-queries={claim.queries.length}>
        <div className="mb-1 text-2xs font-medium text-faint">
          {claim.queries.length ? CHALLENGE_TEXT.queries(claim.queries.length) : CHALLENGE_TEXT.noQueries}
        </div>
        <div className="space-y-1.5">
          {claim.queries.map((q, i) => <QueryCard key={`${q.alias}-${i}`} id={`${baseId}-q-${q.alias}`} query={q} />)}
        </div>
      </section>
    </>
  )
}

function QueryCard({ id, query }: { id: string; query: ChallengeQuery }) {
  return (
    <div id={id} data-challenge-query={query.alias}
         className="min-w-0 rounded data-[flash]:outline data-[flash]:outline-solid data-[flash]:outline-2 data-[flash]:outline-offset-2 data-[flash=focus]:outline-[color:var(--accent)]">
      <div className="mb-1 flex min-w-0 items-center gap-2">
        {/* 从理由里的数字跳过来时焦点落在这里：得看得见落在哪 */}
        <span tabIndex={-1} data-challenge-query-head=""
              className="min-w-0 flex-1 truncate rounded-sm text-2xs font-medium text-faint focus:outline-none focus-visible:outline focus-visible:outline-solid focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[color:var(--accent)]">
          {EVIDENCE_TEXT.query(query.alias)}
          {query.source && <span className="mono ml-1.5 font-normal">{query.source}</span>}
        </span>
        {query.sql && <CopyChip label={EVIDENCE_TEXT.copySql} text={() => query.sql} />}
      </div>
      {query.sql && (
        <pre className="mono mb-1 max-h-28 overflow-auto whitespace-pre-wrap rounded bg-panel px-1.5 py-1 text-2xs leading-relaxed text-dim [overflow-wrap:anywhere]"
             data-challenge-sql="">
          {query.sql}
        </pre>
      )}
      <div className="tnum flex flex-wrap gap-x-2 text-2xs text-faint">
        {query.row_count != null && <span data-challenge-rows={query.row_count}>{CHALLENGE_TEXT.rows(query.row_count)}</span>}
        {query.truncated && (
          <span data-challenge-truncated="" style={{ color: 'var(--st-waiting)' }}>{CHALLENGE_TEXT.truncated}</span>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------- 理由

/**
 * 理由正文。有理由文档时按文档画：数字的出处照报告里的样子（有出处的实线、裸数字点状线加「无证据」），有出处的数字后面
 * 挂查询编号，点一下跳到那次查询。文档可能直接放在工件里，也可能是另一个工件的 id（约定没定死，两种都认）；
 * 都没有、或者取不到时按渲染后的正文显示，数字照写，只是不标出处
 */
function Rationale({ claim, onJump }: { claim: ChallengeClaim; onJump: (alias: string) => void }) {
  const ref = claim.rationale_doc
  const inline = ref && typeof ref === 'object' ? asDoc(ref) : null
  const artifact = typeof ref === 'string' ? ref : ''
  const slot = useEvidence((s) => (artifact ? s.docs[artifact] : undefined))
  const loadDoc = useEvidence((s) => s.loadDoc)
  useEffect(() => { if (artifact) void loadDoc(artifact) }, [artifact, loadDoc])
  const doc = inline ?? (slot?.status === 'ok' ? slot.data ?? null : null)
  if (doc && (doc.blocks ?? []).length) return <RationaleDoc doc={doc} onJump={onJump} />
  if (artifact && slot?.status !== 'error' && !doc) {
    return <div className="text-2xs text-faint" data-challenge-doc-loading="">{CHALLENGE_TEXT.docLoading}</div>
  }
  if (!claim.rationale.trim()) return <div className="text-2xs text-faint">{CHALLENGE_TEXT.rationaleEmpty}</div>
  return <div data-challenge-rationale-text=""><Markdown text={claim.rationale} dense /></div>
}

/** 列表项从哪一个片段开始：同 EvidenceDoc（行首的「- 」「1. 」是结构片段） */
const ITEM_START = /(^|\n)[ \t]*([-*+]|\d+[.)])[ \t]+$/

function RationaleDoc({ doc, onJump }: { doc: EvidenceDocData; onJump: (alias: string) => void }) {
  return (
    <div className="min-w-0 space-y-1.5 text-[11.5px] leading-relaxed" data-challenge-doc="">
      {doc.blocks.map((block, b) => <RationaleBlock key={block.id ?? b} block={block} doc={doc} onJump={onJump} />)}
    </div>
  )
}

/** 一块：外观借 Markdown 的 BlockView，和报告文档、普通答案长得一样（块的分法同 EvidenceDoc 的 DocBlock） */
function RationaleBlock({ block, doc, onJump }: { block: EvidenceBlock; doc: EvidenceDocData; onJump: (alias: string) => void }) {
  const units = block.units ?? []
  const unit = (u: EvidenceUnit, code = false) => <RationaleUnit key={u.id} unit={u} doc={doc} code={code} onJump={onJump} />
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

/** 引用的是查询结果里的一格：返回查询编号 */
function cellAlias(seg: EvidenceSegment, doc: EvidenceDocData): string | null {
  const cite = seg.cite
  if (!cite?.alias || segmentState(seg) !== 'deterministic') return null
  const entry = doc.catalog?.[cite.alias]
  return cite.kind === 'cell' || (cite.kind !== 'metric' && cite.kind !== 'input' && entry?.kind === 'query') ? cite.alias : null
}

/**
 * 一句话：文字按行内 Markdown 渲染（整句算一次样式，同 EvidenceDoc 的 UnitView）。有出处的数字是按钮，后面挂查询编号；
 * 裸数字、疑似不存在的名称画点状线，句末挂「无证据」「疑似不存在的名称」。别的片段（表名、字段名的链接）照普通文字
 */
function RationaleUnit({ unit, doc, code, onJump }: {
  unit: EvidenceUnit; doc: EvidenceDocData; code: boolean; onJump: (alias: string) => void
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
    const alias = cellAlias(s, doc)
    const body = styles ? <StyledSlice text={whole} styles={styles} from={from} to={pos} links={!state} /> : s.text
    if (alias) {
      const source = sourceOf(s, doc)
      return (
        <button key={s.id} type="button" className="ev-seg" data-ev-state="deterministic" data-ev-line="solid"
                data-challenge-cite={alias} style={evStyle('deterministic')} title={source}
                aria-label={CHALLENGE_TEXT.cited(s.text, source)} onClick={() => onJump(alias)}>
          {body}
          {/* inline-block：不吃上面那条下划线 */}
          <sup className="mono ml-px inline-block select-none text-[9px] font-semibold text-faint" aria-hidden>{alias}</sup>
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
