/**
 * 从业务文档起草口径（/ledger/drafts；服务端 app/ledger/docdraft.py、docattach.py）。
 *
 * 业务文档写的是业务说法（名称、粒度、分子分母、过滤），不是库里的表和 SQL。这一页做三段：
 * - 发起：选知识库文档，可选数据源（只写进适用范围）和一段起草要求。模型在后台读文档、核对引用，页面轮询详情直到状态离开
 *   drafting（最多一分钟），期间显示抽取中；
 * - 审读：每条指标带单位、粒度、定义和逐字核对过的引用原文（第 N 段），逐条的未决问题可以勾掉；被丢弃的条目单独列出原因。
 *   引用的段落正文按需从知识库取，省得一次读几十份文档的全部段落；
 * - 接上：草稿只到定义为止，取数路径必须来自一次真实运行。填运行 id（也可以从最近成功的运行里选），attach 之后产出可试运行
 *   的工作流版本，不发布；不是口径就驳回并记一条审计。
 *
 * 状态由服务端算（还有问题 → questions_open，否则 path_pending；attach 之后 attached），页面只按状态画。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { ChevronLeft, FileText, RefreshCw, ScrollText } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../../api/client'
import { EmptyState, ErrorState, Notice, PageHeader, Skeleton, Spinner, promptDialog, toast } from '../../components/ui'
import { formatDateTime, formatTime } from '../../lib/format'
import { LEDGER_UI } from '../../lib/termsLedger'
import type { DataSourceName, KbDocument, Run } from '../../types'
import type {
  CaliberDraft, CaliberDraftStatus, CaliberDraftSummary, DraftAlignment, DraftCitation, DraftMetric,
} from '../../typesLedger'

const D = LEDGER_UI.drafts

/** 列表的状态筛：全部加四种落点，起草中的只出现在全部里 */
type Filter = '' | Exclude<CaliberDraftStatus, 'drafting'>
const FILTERS: Filter[] = ['', 'questions_open', 'path_pending', 'attached', 'rejected']

/** 状态标签：起草中 / 待确认问题 / 待接数据路径 / 已接上 / 已驳回 */
function StatusPill({ status }: { status: CaliberDraftStatus }) {
  const color = status === 'attached' ? 'var(--ok)' : status === 'rejected' ? 'var(--text-faint)'
    : status === 'drafting' ? 'var(--accent)' : 'var(--warn)'
  return (
    <span className="shrink-0 rounded border px-1.5 py-0.5 text-[10px]"
          style={{ color, borderColor: `color-mix(in srgb, ${color} 40%, var(--border))` }}>
      {D.status[status] ?? status}
    </span>
  )
}

/** span 是引用在段落原文里的 [start, end)：切成「前 / 引用 / 后」三段高亮 */
function spanParts(content: string, span?: [number, number]): [string, string, string] | null {
  if (!span) return null
  const [start, end] = span
  if (start < 0 || end > content.length || start >= end) return null
  return [content.slice(0, start), content.slice(start, end), content.slice(end)]
}

function QuoteBody({ content, span, quote }: { content: string; span?: [number, number]; quote: string }) {
  const parts = spanParts(content, span)
  if (!content) return <>{quote}</>
  if (!parts) return <>{content}</>
  return (
    <>
      {parts[0]}
      <mark className="rounded px-0.5 text-fg" style={{ background: 'color-mix(in srgb, var(--warn) 22%, transparent)' }}>
        {parts[1]}
      </mark>
      {parts[2]}
    </>
  )
}

/** 草稿和已发布口径的对齐：新的、已有出处的、定义不同的 */
function AlignmentBlock({ alignment }: { alignment: DraftAlignment }) {
  const added = alignment.new ?? []
  const duplicate = alignment.duplicate ?? []
  const conflict = alignment.conflict ?? []
  if (!added.length && !duplicate.length && !conflict.length) return null
  const row = (label: string, items: { name: string; reason?: string }[]) => items.length > 0 && (
    <li className="flex flex-wrap items-baseline gap-x-1.5">
      <span className="text-faint">{label}</span>
      {items.map((item) => (
        <span key={item.name}>
          {item.name}
          {item.reason ? `（${item.reason}）` : ''}
        </span>
      ))}
    </li>
  )
  return (
    <div data-draft-alignment="">
      <h3 className="mb-1 text-2xs font-semibold text-dim">{D.alignment}</h3>
      <ul className="space-y-1 text-2xs leading-relaxed">
        {row(D.alignmentNew, added.map((name) => ({ name })))}
        {row(D.alignmentDuplicate, duplicate)}
        {row(D.alignmentConflict, conflict)}
      </ul>
    </div>
  )
}

export function CaliberDrafts() {
  const [drafts, setDrafts] = useState<CaliberDraftSummary[] | null>(null)
  const [listError, setListError] = useState<unknown>(null)
  const [filter, setFilter] = useState<Filter>('')

  const [openId, setOpenId] = useState<string | null>(null)
  const [detail, setDetail] = useState<CaliberDraft | null>(null)
  const [detailError, setDetailError] = useState<unknown>(null)
  const [detailLoading, setDetailLoading] = useState(false)

  const [docs, setDocs] = useState<KbDocument[] | null>(null)
  const [docsError, setDocsError] = useState<unknown>(null)
  const [sources, setSources] = useState<DataSourceName[]>([])
  const [pickedDocs, setPickedDocs] = useState<string[]>([])
  const [datasourceId, setDatasourceId] = useState('')
  const [guidance, setGuidance] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const [pickedQuestions, setPickedQuestions] = useState<string[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [runId, setRunId] = useState('')
  const [recentRuns, setRecentRuns] = useState<Run[] | null>(null)
  const [attachOut, setAttachOut] = useState<{ workflow_id: string; version: number; warnings: string[] } | null>(null)

  /** 起草中的草稿：每两秒取一次详情，状态离开 drafting 或超过一分钟就停 */
  const [pollingId, setPollingId] = useState<string | null>(null)
  const [pollTimedOut, setPollTimedOut] = useState(false)

  /** 引用所在段落的正文和段号：按文档懒取，取过的记在这里 */
  const [chunks, setChunks] = useState<Record<string, { ordinal: number; content: string }>>({})
  const asked = useRef(new Set<string>())
  /** 连续点开两份草稿时，晚到的那次详情不覆盖现在打开的那份 */
  const openSeq = useRef(0)

  const loadList = useCallback(() => {
    setListError(null)
    api.caliberLedger.drafts().then(
      (body) => setDrafts(body.drafts),
      (e) => { setDrafts(null); setListError(e) },
    )
  }, [])
  useEffect(loadList, [loadList])

  useEffect(() => {
    api.kb.documents().then((rows) => { setDocs(rows); setDocsError(null) },
      (e) => { setDocs([]); setDocsError(e) })
    api.datasources.names().then(
      (all) => setSources(all.filter((s) => s.enabled !== false)),
      () => setSources([]),
    )
  }, [])

  const ensureChunks = useCallback((documentId: string) => {
    if (!documentId || asked.current.has(documentId)) return
    asked.current.add(documentId)
    api.kb.document(documentId).then(({ chunks: rows }) => {
      setChunks((cur) => {
        const next = { ...cur }
        for (const row of rows) next[row.id] = { ordinal: row.ordinal, content: row.content }
        return next
      })
    }, () => { asked.current.delete(documentId) })
  }, [])

  const open = async (id: string) => {
    const seq = ++openSeq.current
    setOpenId(id)
    setDetail(null)
    setDetailError(null)
    setDetailLoading(true)
    setPickedQuestions([])
    setAttachOut(null)
    setRunId('')
    setRecentRuns(null)
    setPollingId(null)
    setPollTimedOut(false)
    try {
      const draft = await api.caliberLedger.draft(id)
      if (seq !== openSeq.current) return
      setDetail(draft)
      if (draft.status === 'drafting') { setPollTimedOut(false); setPollingId(id) }
    } catch (e) {
      if (seq !== openSeq.current) return
      setDetailError(e)
    } finally {
      if (seq === openSeq.current) setDetailLoading(false)
    }
    api.runs.list({ status: 'succeeded', limit: 20 }).then(
      (runs) => { if (seq === openSeq.current) setRecentRuns(runs) },
      () => { if (seq === openSeq.current) setRecentRuns([]) },
    )
  }

  useEffect(() => {
    if (!pollingId) return
    const until = Date.now() + 60_000
    let alive = true
    const timer = setInterval(() => {
      api.caliberLedger.draft(pollingId).then((draft) => {
        if (!alive) return
        setDetail((cur) => (cur && cur.id === draft.id ? draft : cur))
        if (draft.status !== 'drafting') {
          setPollingId(null)
          setPollTimedOut(false)
          loadList()
        } else if (Date.now() > until) {
          setPollingId(null)
          setPollTimedOut(true)
          loadList()
        }
      }, (e) => {
        if (!alive) return
        setPollingId(null)
        setDetailError(e)
      })
    }, 2000)
    return () => { alive = false; clearInterval(timer) }
  }, [pollingId, loadList])

  useEffect(() => {
    for (const doc of detail?.documents ?? []) ensureChunks(doc.document_id)
  }, [detail, ensureChunks])

  const shown = useMemo(
    () => (drafts ?? []).filter((d) => !filter || d.status === filter),
    [drafts, filter],
  )

  const chunkDoc = useMemo(() => {
    const map: Record<string, string> = {}
    for (const doc of detail?.documents ?? []) for (const chunk of doc.chunk_ids) map[chunk] = doc.document_id
    return map
  }, [detail])

  const paragraphOf = (citation: DraftCitation): number | null => {
    const ordinal = chunks[citation.chunk]?.ordinal
    if (typeof ordinal === 'number') return ordinal
    const documentId = citation.document_id || chunkDoc[citation.chunk]
    const doc = detail?.documents?.find((d) => d.document_id === documentId)
    const at = doc?.chunk_ids.indexOf(citation.chunk) ?? -1
    return at >= 0 ? at + 1 : null
  }

  const submit = async () => {
    if (!pickedDocs.length) { toast.warn(D.needDoc); return }
    setSubmitting(true)
    try {
      const out = await api.caliberLedger.extractDraft({
        documents: pickedDocs,
        ...(datasourceId ? { datasource_id: datasourceId } : {}),
        ...(guidance.trim() ? { guidance: guidance.trim() } : {}),
      })
      toast.ok(D.started)
      setPickedDocs([])
      setGuidance('')
      setDatasourceId('')
      setFilter('')
      loadList()
      void open(out.draft_id)
    } catch (e) {
      toast.error(e)
    } finally {
      setSubmitting(false)
    }
  }

  const resolve = async (draft: CaliberDraft) => {
    if (!pickedQuestions.length) { toast.warn(D.resolveNeed); return }
    setBusy(`resolve:${draft.id}`)
    try {
      const fresh = await api.caliberLedger.patchDraft(draft.id, { resolved: pickedQuestions })
      setDetail(fresh)
      toast.ok(D.resolved(pickedQuestions.length))
      setPickedQuestions([])
      loadList()
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(null)
    }
  }

  const attach = async (draft: CaliberDraft) => {
    const id = runId.trim()
    if (!id) { toast.warn(D.attachNeedRun); return }
    setBusy(`attach:${draft.id}`)
    try {
      const out = await api.caliberLedger.attachDraft(draft.id, id)
      setAttachOut(out)
      setDetail(await api.caliberLedger.draft(draft.id))
      toast.ok(D.attached(out.version))
      loadList()
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(null)
    }
  }

  const dismiss = async (draft: CaliberDraft) => {
    const note = await promptDialog({
      title: D.dismissTitle, label: D.dismissNote, placeholder: D.dismissPlaceholder, confirmLabel: D.dismissConfirm,
    })
    if (note == null) return
    setBusy(`dismiss:${draft.id}`)
    try {
      await api.caliberLedger.dismissDraft(draft.id, note.trim() || undefined)
      setDetail(await api.caliberLedger.draft(draft.id))
      toast.ok(D.dismissed)
      loadList()
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(null)
    }
  }

  const toggleQuestion = (question: string, checked: boolean) => {
    setPickedQuestions((cur) => (checked ? [...cur, question] : cur.filter((q) => q !== question)))
  }

  const questionList = (questions: string[] | undefined, prefix: string) => {
    const list = (questions ?? []).filter((q) => q.trim())
    if (!list.length) return null
    return (
      <ul className="mt-1 space-y-0.5">
        {list.map((q, i) => (
          <li key={`${prefix}:${i}`}>
            <label className="flex items-start gap-1.5 text-2xs leading-relaxed text-dim">
              <input type="checkbox" className="mt-0.5 accent-[var(--accent)]" checked={pickedQuestions.includes(q)}
                     onChange={(e) => toggleQuestion(q, e.target.checked)} />
              <span className="min-w-0 [overflow-wrap:anywhere]">{q}</span>
            </label>
          </li>
        ))}
      </ul>
    )
  }

  const citationRow = (citation: DraftCitation, key: string, metricName: string) => {
    const chunk = chunks[citation.chunk]
    const ordinal = paragraphOf(citation)
    return (
      <li key={key}>
        <details className="rounded border bg-elev px-2 py-1">
          <summary className="cursor-pointer text-2xs leading-relaxed text-dim">
            <span className="[overflow-wrap:anywhere]">「{citation.quote}」</span>
            {ordinal != null && <span className="ml-1.5 text-faint">· {D.paragraph(ordinal)}</span>}
          </summary>
          <p className="mt-1 whitespace-pre-wrap text-2xs leading-relaxed text-faint">
            {chunk
              ? <QuoteBody content={chunk.content} span={citation.span} quote={citation.quote} />
              : D.citationMissing}
          </p>
          <p className="mt-0.5 text-[10px] text-faint">{metricName}</p>
        </details>
      </li>
    )
  }

  const renderMetric = (metric: DraftMetric, index: number) => {
    const definition = metric.definition ?? {}
    const citations = definition.citations ?? []
    const rows = [
      [D.unit, metric.unit],
      [D.grain, metric.grain],
      [D.timeField, metric.time_field],
      [D.numerator, metric.numerator],
      [D.denominator, metric.denominator],
      [D.scope, definition.scope],
      [D.owner, definition.owner],
      [D.effectiveFrom, definition.effective_from],
      [D.dimensions, metric.dimensions?.join('、')],
      [D.filters, metric.filters?.join('；')],
      [D.exclusions, definition.exclusions?.join('；')],
    ].filter(([, value]) => typeof value === 'string' && value.trim()) as [string, string][]
    return (
      <li key={`${metric.name}:${index}`} className="rounded-md border bg-panel px-3 py-2" data-draft-metric={metric.name}>
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <span className="text-xs font-medium">{metric.name}</span>
          {!!metric.synonyms?.length && <span className="text-2xs text-faint">{D.synonyms(metric.synonyms.join('、'))}</span>}
        </div>
        {rows.length > 0 && (
          <dl className="mt-1 grid gap-x-3 gap-y-0.5 text-2xs sm:grid-cols-[auto_1fr]">
            {rows.map(([label, value]) => (
              <div key={label} className="contents">
                <dt className="text-faint">{label}</dt>
                <dd className="text-dim">{value}</dd>
              </div>
            ))}
          </dl>
        )}
        {definition.text && <p className="mt-1.5 text-2xs leading-relaxed text-dim">{definition.text}</p>}
        {citations.length > 0 && (
          <div className="mt-1.5">
            <h4 className="mb-1 text-2xs font-semibold text-faint">{D.citations}</h4>
            <ul className="space-y-1">{citations.map((c, i) => citationRow(c, `${index}:${i}`, metric.name))}</ul>
          </div>
        )}
        {!!metric.open_questions?.length && (
          <div className="mt-1.5" data-draft-questions="">
            <h4 className="text-2xs font-semibold text-faint">{D.questions}</h4>
            {questionList(metric.open_questions, `${index}`)}
          </div>
        )}
      </li>
    )
  }

  const renderDetail = (draft: CaliberDraft) => {
    const drafting = draft.status === 'drafting'
    const terminal = draft.status === 'attached' || draft.status === 'rejected'
    const questions = draft.open_questions ?? []
    const dropped = draft.dropped ?? []
    const metrics = draft.metrics ?? []
    return (
      <div className="space-y-3 border-t px-3 py-3" data-draft-detail="">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-2xs text-faint">
          <StatusPill status={draft.status} />
          {!!draft.documents?.length && <span>{D.documentsUsed(draft.documents.length)}</span>}
          {draft.created_at && <span title={formatDateTime(draft.created_at)}>{formatTime(draft.created_at)}</span>}
          {draft.created_by && <span>{D.createdBy(draft.created_by)}</span>}
          {draft.model_id && <span title={draft.model_id}>{D.model(draft.model_id)}</span>}
          {draft.workflow_id && draft.status === 'attached' && (
            <Link to={`/studio/${draft.workflow_id}`} className="text-accent hover:underline" data-draft-workflow="">
              {D.openWorkflow}{draft.workflow_name ? `：${draft.workflow_name}` : ''}
            </Link>
          )}
        </div>

        {draft.error && (
          <Notice tone="err">
            {drafting ? D.draftingFailed : D.loadFailed}：{draft.error}
          </Notice>
        )}

        {drafting && pollingId === draft.id && (
          <Notice tone="info">
            <span className="inline-flex items-center gap-1.5"><Spinner size={12} /> {D.drafting}</span>
            <span className="mt-0.5 block text-faint">{D.draftingHint}</span>
          </Notice>
        )}
        {drafting && pollTimedOut && <Notice tone="warn">{D.draftingTimeout}</Notice>}
        {draft.truncated && <Notice tone="warn">{D.truncated}</Notice>}

        <div>
          <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-2xs font-semibold text-dim">{D.metrics}</h3>
            {pickedQuestions.length > 0 && (
              <div className="flex items-center gap-2">
                <span className="text-2xs text-faint">{D.resolveHint}</span>
                <button type="button" className="btn btn-xs" disabled={busy === `resolve:${draft.id}`}
                        onClick={() => void resolve(draft)} data-draft-resolve="">
                  {busy === `resolve:${draft.id}` ? <Spinner size={11} /> : null} {D.resolve}
                </button>
              </div>
            )}
          </div>
          {metrics.length ? (
            <ul className="space-y-2">{metrics.map(renderMetric)}</ul>
          ) : !drafting ? (
            <EmptyState offline={false} icon={<ScrollText size={20} />} title={D.noMetrics} body={D.noMetricsBody} />
          ) : (
            <Skeleton rows={3} height={16} />
          )}
        </div>

        {questions.length > 0 && (
          <div data-draft-questions-all="">
            <h3 className="mb-1 text-2xs font-semibold text-dim">{D.questionsOther}</h3>
            {questionList(questions, 'top')}
          </div>
        )}

        {dropped.length > 0 && (
          <div data-draft-dropped="">
            <h3 className="text-2xs font-semibold text-dim">{D.dropped}</h3>
            <p className="mb-1 text-2xs leading-relaxed text-faint">{D.droppedHint}</p>
            <ul className="space-y-0.5 text-2xs leading-relaxed">
              {dropped.map((item, i) => (
                <li key={`${item.name}:${i}`} className="text-dim">
                  <span className="font-medium">{item.name}</span>
                  <span className="text-faint">：{item.reason}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {draft.alignment && <AlignmentBlock alignment={draft.alignment} />}

        {!drafting && !terminal && (
          <div className="rounded-md border bg-elev px-3 py-2" data-draft-attach="">
            <h3 className="text-2xs font-semibold text-dim">{D.attach}</h3>
            <p className="mt-0.5 text-2xs leading-relaxed text-faint">{D.attachHint}</p>
            <div className="mt-1.5 flex flex-wrap items-end gap-2">
              <label className="min-w-40 flex-1 text-2xs text-faint">
                <span className="mb-0.5 block">{D.runId}</span>
                <input className="input w-full" value={runId} placeholder={D.runIdPlaceholder}
                       onChange={(e) => setRunId(e.target.value)} aria-label={D.runId} data-draft-run-id="" />
              </label>
              <label className="w-56 text-2xs text-faint">
                <span className="mb-0.5 block">{D.recentRuns}</span>
                <select className="field h-[30px] w-full" value="" aria-label={D.recentRuns}
                        onChange={(e) => { if (e.target.value) setRunId(e.target.value) }}>
                  <option value="">
                    {recentRuns == null ? D.recentRunsPick : recentRuns.length ? D.recentRunsPick : D.recentRunsNone}
                  </option>
                  {(recentRuns ?? []).map((run) => (
                    <option key={run.id} value={run.id}>
                      {run.workflow_name || run.id}{run.created_at ? ` · ${formatTime(run.created_at)}` : ''}
                    </option>
                  ))}
                </select>
              </label>
              <button type="button" className="btn btn-primary btn-sm" disabled={busy === `attach:${draft.id}`}
                      onClick={() => void attach(draft)} data-draft-attach-submit="">
                {busy === `attach:${draft.id}` ? <Spinner size={12} /> : null} {busy === `attach:${draft.id}` ? D.attaching : D.attachAction}
              </button>
            </div>
          </div>
        )}

        {attachOut && (
          <div className="rounded-md border bg-elev px-3 py-2 text-2xs leading-relaxed text-dim" data-draft-attached="">
            <p>{D.attached(attachOut.version)}</p>
            <Link to={`/studio/${attachOut.workflow_id}`} className="text-accent hover:underline">
              {D.openWorkflow}
            </Link>
            {attachOut.warnings.length > 0 && (
              <div className="mt-1">
                <span className="text-faint">{D.attachWarnings}</span>
                <ul className="mt-0.5 list-disc pl-4">
                  {attachOut.warnings.map((w) => <li key={w}>{w}</li>)}
                </ul>
              </div>
            )}
          </div>
        )}

        {!terminal && (
          <div className="flex flex-wrap items-center gap-2">
            <button type="button" className="btn btn-xs btn-ghost" disabled={!!busy}
                    onClick={() => void dismiss(draft)} data-draft-dismiss="">
              {busy === `dismiss:${draft.id}` ? <Spinner size={11} /> : null} {D.dismiss}
            </button>
          </div>
        )}
      </div>
    )
  }

  const pendingDocs = !docs ? <Skeleton rows={4} height={22} />
    : docs.length === 0 ? <p className="py-2 text-2xs leading-relaxed text-faint">{D.noDocuments}：{D.noDocumentsBody}</p>
      : (
        <ul className="max-h-40 space-y-0.5 overflow-y-auto pr-1">
          {docs.map((doc) => {
            const ready = doc.status !== 'processing' && doc.status !== 'failed'
            return (
              <li key={doc.id}>
                <label className={clsx('flex items-start gap-1.5 text-2xs leading-relaxed',
                  ready ? 'text-dim' : 'text-faint')}>
                  <input type="checkbox" className="mt-0.5 accent-[var(--accent)]" disabled={!ready}
                         checked={pickedDocs.includes(doc.id)}
                         onChange={(e) => setPickedDocs((cur) => (e.target.checked
                           ? [...cur, doc.id] : cur.filter((id) => id !== doc.id)))} />
                  <span className="min-w-0 flex-1 [overflow-wrap:anywhere]">{doc.title || doc.id}</span>
                  <span className="shrink-0 text-faint">{D.documentMeta(doc.collection, doc.chunk_count)}</span>
                </label>
              </li>
            )
          })}
        </ul>
      )

  return (
    <div className="flex h-full flex-col" data-caliber-drafts="">
      <PageHeader icon={<FileText size={14} />} title={D.title} subtitle={D.subtitle}
                  actions={<>
                    <Link to="/ledger/calibers" className="btn btn-xs btn-ghost">
                      <ChevronLeft size={12} aria-hidden /> {D.back}
                    </Link>
                    <button type="button" className="btn btn-xs btn-ghost" onClick={loadList} title={D.refresh}>
                      <RefreshCw size={12} aria-hidden /> {D.refresh}
                    </button>
                  </>} />
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-5xl space-y-3 p-4">
          <section className="rounded-lg border bg-panel px-3 py-2.5" data-draft-start="">
            <h2 className="text-xs font-semibold">{D.start}</h2>
            <div className="mt-2 grid gap-3 md:grid-cols-2">
              <div>
                <h3 className="text-2xs font-semibold text-dim">{D.documents}</h3>
                <p className="mb-1 text-2xs text-faint">{D.documentsHint}</p>
                {docsError ? <ErrorState error={docsError} compact /> : pendingDocs}
              </div>
              <div className="space-y-2">
                <label className="block text-2xs text-faint">
                  <span className="mb-0.5 block font-semibold text-dim">{D.datasource}</span>
                  <select className="field w-full" value={datasourceId} onChange={(e) => setDatasourceId(e.target.value)}>
                    <option value="">{D.datasourceNone}</option>
                    {sources.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                  </select>
                  <span className="mt-0.5 block">{D.datasourceHint}</span>
                </label>
                <label className="block text-2xs text-faint">
                  <span className="mb-0.5 block font-semibold text-dim">{D.guidance}</span>
                  <textarea className="field min-h-[64px] w-full text-xs" rows={3} value={guidance}
                            placeholder={D.guidancePlaceholder} onChange={(e) => setGuidance(e.target.value)} />
                </label>
                <button type="button" className="btn btn-primary btn-sm" disabled={submitting}
                        onClick={() => void submit()} data-draft-submit="">
                  {submitting ? <Spinner size={12} /> : <FileText size={12} aria-hidden />} {submitting ? D.submitting : D.submit}
                </button>
              </div>
            </div>
          </section>

          <div className="flex flex-wrap items-center gap-1" role="group" aria-label={D.filterLabel}>
            {FILTERS.map((f) => (
              <button key={f || 'all'} type="button"
                      className={clsx('btn btn-xs', filter === f ? 'btn-primary' : 'btn-ghost')}
                      aria-pressed={filter === f} onClick={() => setFilter(f)} data-draft-filter={f || 'all'}>
                {f ? D.filter[f] : D.filter.all}
                {drafts && <span className="tnum ml-1 text-[10px] opacity-70">
                  {f ? drafts.filter((d) => d.status === f).length : drafts.length}
                </span>}
              </button>
            ))}
          </div>

          {listError ? <ErrorState error={listError} onRetry={loadList} compact />
            : !drafts ? <Skeleton rows={5} height={38} />
            : !shown.length ? (
              <EmptyState offline={false} icon={<FileText size={22} />} title={D.empty}
                          body={filter || drafts.length ? D.emptyFiltered : D.emptyBody} />
            ) : shown.map((draft) => {
              const expanded = openId === draft.id
              return (
                <section key={draft.id} className="overflow-hidden rounded-lg border bg-panel" data-draft={draft.id}>
                  <button type="button" className="flex w-full items-start gap-3 px-3 py-2.5 text-left hover:bg-hover"
                          onClick={() => {
                            if (expanded) {
                              openSeq.current++
                              setOpenId(null)
                              setDetail(null)
                              setPollingId(null)
                            } else {
                              void open(draft.id)
                            }
                          }}
                          aria-expanded={expanded} data-draft-toggle="">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                        <span className="text-sm font-medium [overflow-wrap:anywhere]">{draft.title}</span>
                        <StatusPill status={draft.status} />
                        {draft.error && <span className="text-2xs text-[var(--err)]">{D.draftingFailed}</span>}
                      </div>
                      <div className="mt-0.5 text-2xs text-faint">
                        {D.summary(draft.metric_count, draft.question_count)}
                        {draft.created_at ? ` · ${formatTime(draft.created_at)}` : ''}
                      </div>
                    </div>
                  </button>
                  {expanded && (
                    detailLoading ? <div className="border-t px-3 py-3"><Skeleton rows={4} height={16} /></div>
                      : detailError ? <div className="border-t px-3 py-3"><ErrorState error={detailError} compact
                                                                    onRetry={() => void open(draft.id)} /></div>
                        : detail && detail.id === draft.id ? renderDetail(detail) : null
                  )}
                </section>
              )
            })}
        </div>
      </div>
    </div>
  )
}
