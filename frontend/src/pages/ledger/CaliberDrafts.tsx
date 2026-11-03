/**
 * 从业务文档起草口径（/ledger/drafts；服务端 app/ledger/docdraft.py、docattach.py）。
 *
 * 主路径一次到位：选文档 → 模型读文档、对照数据目录产出定义 + 取数 + 入口字段 + 指标表达式 → 服务端校验后落成草稿图
 * → 后台自动只读试运行 → 这里展示「定义 + 数 + 原文引用」→ 人去画布审改并发布。发布才需要人签字。
 * - 发起：选知识库文档，可选数据源（只写进适用范围）和一段起草要求，页面轮询详情直到状态离开 drafting（最多一分钟）；
 * - 试运行：草稿顶部显示这次自动试运行的状态和每个指标的值，running 时每两秒取一次，最多等一分钟；失败照实写原因；
 * - 审读：每条指标带单位、粒度、指标表达式、定义和逐字核对过的引用原文（第 N 段），逐条的未决问题可以勾掉；被丢弃
 *   的条目单独列出原因；入口字段列出名字、类型、必填和缺省值；
 * - 发布：主按钮去画布审改并发布。「接上一次运行」只是辅助入口：已经有答案时让它认这份定义。
 *
 * 页面不显示 SQL，取数到画布上看。状态由服务端算（还有问题 → questions_open，否则 ready；旧路径可能停在 path_pending），
 * 页面只按状态画。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { ChevronLeft, FileText, PenLine, RefreshCw, ScrollText } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../../api/client'
import { EmptyState, ErrorState, Notice, PageHeader, Skeleton, Spinner, promptDialog, toast } from '../../components/ui'
import { formatDateTime, formatTime } from '../../lib/format'
import { LEDGER_UI } from '../../lib/termsLedger'
import type { DataSourceName, KbDocument, Run } from '../../types'
import type {
  CaliberDraft, CaliberDraftStatus, CaliberDraftSummary, DraftAlignment, DraftCitation, DraftMetric, DraftTrialOut,
} from '../../typesLedger'

const D = LEDGER_UI.drafts

/** 列表的状态筛：全部加六种落点，起草中的只出现在全部里 */
type Filter = '' | Exclude<CaliberDraftStatus, 'drafting'>
const FILTERS: Filter[] = ['', 'ready', 'questions_open', 'path_pending', 'attached', 'rejected', 'failed']

/** 状态标签：起草中 / 待签字发布 / 待确认问题 / 待接数据路径 / 已接上 / 已驳回 / 起草失败 */
function StatusPill({ status }: { status: CaliberDraftStatus }) {
  const color = status === 'ready' || status === 'attached' ? 'var(--ok)' : status === 'rejected' ? 'var(--text-faint)'
    : status === 'drafting' ? 'var(--accent)' : status === 'failed' ? 'var(--err)' : 'var(--warn)'
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

/** 缺省值直接读：字符串照写，其他转成 JSON 文本 */
function defaultText(value: unknown): string {
  if (typeof value === 'string') return value
  const text = JSON.stringify(value)
  return text ?? String(value)
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

  /** 这次自动试运行的结果；running 时单独轮询，和草稿抽取的轮询互不相干 */
  const [trial, setTrial] = useState<DraftTrialOut | null>(null)
  const [trialPollId, setTrialPollId] = useState<string | null>(null)
  const [trialTimedOut, setTrialTimedOut] = useState(false)
  const [trialError, setTrialError] = useState<unknown>(null)

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
      (all) => {
        const enabled = all.filter((s) => s.enabled !== false)
        setSources(enabled)
        // 数据源必选（取数 SQL 要对照这个库的表和列写），默认选中第一个，别让人对着 422 才知道要填
        setDatasourceId((cur) => cur || (enabled[0]?.id ?? ''))
      },
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
    setTrial(null)
    setTrialPollId(null)
    setTrialTimedOut(false)
    setTrialError(null)
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

  /** 草稿有了试运行就取一次结果；还在 running 就交给下面的轮询 */
  useEffect(() => {
    const id = detail?.id
    const run = detail?.trial?.run_id
    if (!id || !run) return
    let alive = true
    setTrial((cur) => (cur?.run_id === run ? cur : {
      run_id: run, status: detail?.trial?.status ?? 'running', metrics: [], error: detail?.trial?.error ?? '',
    }))
    api.caliberLedger.draftTrial(id).then((out) => {
      if (!alive) return
      setTrial(out)
      setTrialError(null)
      setTrialTimedOut(false)
      if (out.status === 'running') setTrialPollId(id)
    }, (e) => {
      if (!alive) return
      setTrialError(e)
      setTrialPollId(null)
    })
    return () => { alive = false }
  }, [detail?.id, detail?.trial?.run_id])

  /** 试运行还在跑：每两秒取一次，超过一分钟就停；收尾时顺带刷新详情（状态可能已变成待签字发布） */
  useEffect(() => {
    if (!trialPollId) return
    const until = Date.now() + 60_000
    let alive = true
    const timer = setInterval(() => {
      api.caliberLedger.draftTrial(trialPollId).then((out) => {
        if (!alive) return
        setTrial(out)
        setTrialError(null)
        if (out.status === 'running') {
          if (Date.now() > until) {
            setTrialPollId(null)
            setTrialTimedOut(true)
          }
          return
        }
        setTrialPollId(null)
        setTrialTimedOut(false)
        loadList()
        const seq = openSeq.current
        api.caliberLedger.draft(trialPollId).then((fresh) => {
          if (seq !== openSeq.current) return
          setDetail((cur) => (cur && cur.id === fresh.id ? fresh : cur))
        }, () => undefined)
      }, (e) => {
        if (!alive) return
        setTrialPollId(null)
        setTrialError(e)
      })
    }, 2000)
    return () => { alive = false; clearInterval(timer) }
  }, [trialPollId, loadList])

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
    if (typeof citation.ordinal === 'number') return citation.ordinal
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

  /** 自动试运行：状态、每个指标的值（名字和值成对）、失败原因；没有试运行就整块不画 */
  const renderTrial = (draft: CaliberDraft) => {
    const status = trial?.status ?? draft.trial?.status
    if (!status) return null
    const rows = trial?.metrics ?? []
    const color = status === 'succeeded' ? 'var(--ok)' : status === 'failed' ? 'var(--err)' : 'var(--accent)'
    const label = status === 'succeeded' ? D.trialSucceeded : status === 'failed' ? D.trialFailed : D.trialRunning
    const unitOf = (id: string) => (draft.metrics ?? []).find((m) => m.id === id)?.unit
    return (
      <div className="rounded-md border bg-elev px-3 py-2" data-draft-trial={status}>
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <h3 className="text-2xs font-semibold text-dim">{D.trial}</h3>
          <span className="text-2xs" style={{ color }}>{label}</span>
          {trial?.run_id && (
            <Link to={`/runs/${trial.run_id}`} className="text-2xs text-accent hover:underline" data-draft-trial-run="">
              {D.trialRun}
            </Link>
          )}
          <span className="text-[10px] text-faint">{D.trialHint}</span>
        </div>
        {status === 'running' && (
          <p className="mt-1 flex items-center gap-1.5 text-2xs text-dim">
            <Spinner size={11} /> {D.trialRunning}
          </p>
        )}
        {status === 'running' && trialTimedOut && <div className="mt-1"><Notice tone="warn">{D.trialTimeout}</Notice></div>}
        {status === 'failed' && (
          <div className="mt-1">
            <Notice tone="err">{D.trialFailed}：{trial?.error || draft.trial?.error || D.loadFailed}</Notice>
          </div>
        )}
        {status === 'succeeded' && (rows.length > 0 ? (
          <ul className="mt-1 grid gap-x-4 gap-y-0.5 text-2xs sm:grid-cols-[1fr_auto]">
            {rows.map((row) => {
              const unit = unitOf(row.id)
              return (
                <li key={row.id} className="contents" data-draft-trial-metric={row.id}>
                  <span className="text-dim [overflow-wrap:anywhere]">{row.name || row.id}</span>
                  <span className="tnum text-right font-medium">
                    {row.rendered ?? (row.value == null ? '—' : String(row.value))}{unit ? ` ${unit}` : ''}
                  </span>
                </li>
              )
            })}
          </ul>
        ) : (
          <p className="mt-1 text-2xs text-faint">{D.trialNoMetrics}</p>
        ))}
        {trialError != null && <div className="mt-1"><Notice tone="warn">{D.trialUnavailable}</Notice></div>}
      </div>
    )
  }

  /** 入口字段：发布后按这些字段收参数 */
  const renderEntryFields = (draft: CaliberDraft) => {
    const fields = draft.entry_fields
    if (!fields) return null
    return (
      <div data-draft-entry-fields="">
        <h3 className="mb-1 text-2xs font-semibold text-dim">{D.entryFields}</h3>
        <p className="mb-1 text-2xs text-faint">{D.entryFieldsHint}</p>
        {fields.length > 0 ? (
          <ul className="space-y-0.5 text-2xs leading-relaxed">
            {fields.map((field) => {
              const hasDefault = field.default !== undefined && field.default !== null && field.default !== ''
              return (
                <li key={field.name} className="flex flex-wrap items-baseline gap-x-2" data-draft-entry-field={field.name}>
                  <code className="font-medium text-dim">{field.name}</code>
                  <span className="text-faint">{field.type || 'string'}</span>
                  <span className="text-faint">{field.required ? D.required : D.optional}</span>
                  {hasDefault && <span className="text-faint">{D.defaultValue(defaultText(field.default))}</span>}
                  {field.description && <span className="text-dim [overflow-wrap:anywhere]">{field.description}</span>}
                </li>
              )
            })}
          </ul>
        ) : <p className="text-2xs text-faint">{D.entryFieldNone}</p>}
      </div>
    )
  }

  const renderMetric = (metric: DraftMetric, index: number) => {
    const definition = metric.definition ?? {}
    const citations = definition.citations ?? []
    const rows = [
      [D.unit, metric.unit],
      [D.decimals, metric.decimals != null ? String(metric.decimals) : ''],
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
      <li key={`${metric.id ?? metric.name}:${index}`} className="rounded-md border bg-panel px-3 py-2" data-draft-metric={metric.name}>
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
        {metric.expression && (
          <div className="mt-1.5" data-draft-expression="">
            <h4 className="text-2xs font-semibold text-faint">{D.expression}</h4>
            <code className="mt-0.5 block whitespace-pre-wrap rounded bg-elev px-1.5 py-1 text-2xs text-dim [overflow-wrap:anywhere]">
              {metric.expression}
            </code>
          </div>
        )}
        {definition.text && (
          <div className="mt-1.5">
            <h4 className="text-2xs font-semibold text-faint">{D.definition}</h4>
            <p className="text-2xs leading-relaxed text-dim">{definition.text}</p>
          </div>
        )}
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
          {draft.workflow_id && draft.status !== 'rejected' && (
            <Link to={`/studio/${draft.workflow_id}`} className="text-accent hover:underline" data-draft-workflow="">
              {D.openWorkflow}{draft.workflow_name ? `：${draft.workflow_name}` : ''}
              {draft.version != null ? ` · ${D.workflowVersion(draft.version)}` : ''}
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

        {renderTrial(draft)}
        {renderEntryFields(draft)}

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

        {!drafting && !terminal && draft.workflow_id && (
          <div className="flex flex-wrap items-center gap-2" data-draft-publish="">
            <Link to={`/studio/${draft.workflow_id}`} className="btn btn-primary btn-sm">
              <PenLine size={12} aria-hidden /> {D.publish}
            </Link>
            <span className="text-2xs text-faint">{D.publishHint}</span>
          </div>
        )}

        {!drafting && !terminal && (
          <details className="rounded-md border bg-elev px-3 py-2" data-draft-attach="">
            <summary className="cursor-pointer text-2xs font-semibold text-dim">{D.attach}</summary>
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
              <button type="button" className="btn btn-sm" disabled={busy === `attach:${draft.id}`}
                      onClick={() => void attach(draft)} data-draft-attach-submit="">
                {busy === `attach:${draft.id}` ? <Spinner size={12} /> : null} {busy === `attach:${draft.id}` ? D.attaching : D.attachAction}
              </button>
            </div>
          </details>
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
                  <select className="field w-full" value={datasourceId} required
                          onChange={(e) => setDatasourceId(e.target.value)}>
                    {sources.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                  </select>
                  <span className="mt-0.5 block">{D.datasourceHint}</span>
                </label>
                <label className="block text-2xs text-faint">
                  <span className="mb-0.5 block font-semibold text-dim">{D.guidance}</span>
                  <textarea className="field min-h-[64px] w-full text-xs" rows={3} value={guidance}
                            placeholder={D.guidancePlaceholder} onChange={(e) => setGuidance(e.target.value)} />
                </label>
                <button type="button" className="btn btn-primary btn-sm" disabled={submitting || !datasourceId}
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
                              setTrial(null)
                              setTrialPollId(null)
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
