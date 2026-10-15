/**
 * 记录列表 records_list（docs/design/normal-user-apps.md N8、§3.7、§4.7）：填报登记每次提交是记录表里的一行。
 *
 * 不走封存：每次打开按看的人实时取，行级可见在服务端做——使用（use）只看自己提交的，查看全部（observe）和管理（manage）
 * 看到全部，这里只照服务端说的 mine_only 写标题和说明（「我的记录」「全部记录」），不在浏览器里按人过滤（P4）。第一页用
 * 服务端解析页面时已经取好的（node.data，apps/resolve._records_list），不再取一遍；重新读取、下一页、更正历史才调
 * GET /api/apps/{id}/records。没写 table 的取这个应用的第一张记录表（同服务端）；who 照组件属性带上（只看自己的）。
 * 别人提交的行里遮罩的列服务端已经写成「已遮罩」，照原样写。
 *
 * 列表、筛选（按内容或提交人）、按时间排序（序号就是提交的先后）；每行可以展开看全部字段和附件。更正不改原行：更正是
 * 一次新的提交，指向被更正的那一行（_supersedes），默认只列当前有效的行；更正过的行标「已更正」，展开能看更正历史
 * （带 history 再取一次，顺着指向往前找）。值照服务端交来的原样写，这里不格式化数、不合计（叙述层无算术）。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { ChevronDown, ChevronRight, History, Paperclip, RotateCw, Search } from 'lucide-react'
import { api } from '../../api/client'
import { FileLink } from '../../components/FileInput'
import { ErrorState, Spinner } from '../../components/ui'
import { formatDateTime } from '../../lib/format'
import { isFileRef } from '../../lib/schemaForm'
import { RECORDS_TEXT as T } from '../../lib/termsApp'
import type { RecordRow, RecordsPage } from '../../typesBlueprint'
import { str, useView, type NodeProps } from '../context'

/** 服务端可能给的下一页游标、状态（typesBlueprint.RecordsPage 还没有这两项，契约待补）：empty 是这个应用还没有记录表 */
type Page = RecordsPage & { next_cursor?: string | null; state?: string }

/** 服务端解析页面时给的第一页（node.data）：形状对得上才用 */
function pageOf(data: unknown): Page | null {
  return data && typeof data === 'object' && Array.isArray((data as Page).items) && Array.isArray((data as Page).columns)
    ? data as Page : null
}

/** 一格的值写给人看：文件是可下载的链接，数组用顿号连，布尔写是或否，其余原样 */
function Value({ v }: { v: unknown }) {
  if (v == null || v === '') return <span className="text-faint">—</span>
  if (isFileRef(v)) return <FileLink file={v} />
  if (Array.isArray(v)) {
    return v.some(isFileRef)
      ? <span className="flex flex-col gap-0.5">{v.map((x, i) => <Value key={i} v={x} />)}</span>
      : <span>{v.map((x) => String(x)).join('、')}</span>
  }
  if (typeof v === 'boolean') return <span>{v ? T.yes : T.no}</span>
  if (typeof v === 'object') return <span className="mono text-2xs">{JSON.stringify(v)}</span>
  return <span className="whitespace-pre-wrap break-words">{String(v)}</span>
}

/** 筛选用的文字：各列的值和提交人（文件按文件名） */
function haystack(row: RecordRow): string {
  const parts = Object.values(row.values ?? {}).map((v) => (isFileRef(v) ? v.name : Array.isArray(v) ? v.join(' ') : v == null ? '' : String(v)))
  return [...parts, row._submitted_by ?? ''].join(' ').toLowerCase()
}

export function RecordsList({ node }: NodeProps) {
  const { appId } = useView()
  const table = str(node.props.table) || undefined
  const who = node.props.who === 'mine' || node.props.who === 'all' ? node.props.who : undefined
  const initial = pageOf(node.data)
  const [page, setPage] = useState<Page | null>(initial)
  const [error, setError] = useState<unknown>(null)
  const [more, setMore] = useState(false)
  const [query, setQuery] = useState('')
  const [order, setOrder] = useState<'newest' | 'oldest'>('newest')
  const [open, setOpen] = useState<Set<string>>(new Set())
  // 更正历史：带 history 取一次（含已被更正的行），按 _id 找
  const [history, setHistory] = useState<Map<string, RecordRow> | null>(null)
  const [historyError, setHistoryError] = useState<unknown>(null)
  const [historyFor, setHistoryFor] = useState<Set<string>>(new Set())

  const load = useCallback(() => {
    let alive = true
    setError(null)
    api.personal.records(appId, { table, who }).then((p) => { if (alive) setPage(p) }, (e) => { if (alive) setError(e) })
    return () => { alive = false }
  }, [appId, table, who])
  // 页面重新解析过（node.data 换了）就用新的那一页；服务端没给（老服务端）才自己取
  useEffect(() => {
    if (initial) { setPage(initial); return }
    return load()
  }, [initial, load])
  const loadMore = async () => {
    if (!page?.next_cursor) return
    setMore(true)
    try {
      const next: Page = await api.personal.records(appId, { table, who, cursor: page.next_cursor })
      setPage({ ...next, items: [...page.items, ...next.items] })
    } catch (e) {
      setError(e)
    } finally {
      setMore(false)
    }
  }
  const showHistory = (id: string) => {
    setHistoryFor((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n })
    if (history || historyError) return
    api.personal.records(appId, { table, who, history: true }).then(
      (p) => setHistory(new Map(p.items.map((r) => [r._id, r]))),
      setHistoryError,
    )
  }

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase()
    const list = (page?.items ?? []).filter((r) => !q || haystack(r).includes(q))
    return [...list].sort((a, b) => (order === 'newest' ? b._seq - a._seq : a._seq - b._seq))
  }, [page, query, order])
  const columns = page?.columns ?? []
  const mine = !!page?.mine_only
  const title = mine ? T.mine : str(node.props.title) || T.all

  return (
    <div className="rounded-lg border bg-panel" data-component="records_list" data-records-scope={page ? (mine ? 'mine' : 'all') : ''}>
      <div className="flex flex-wrap items-center gap-2 border-b px-3 py-2">
        <div className="min-w-0 flex-1">
          <div className="text-xs font-medium" data-records-title="">{title}</div>
          {page && (
            <div className="text-2xs text-faint" data-records-hint="">
              {mine ? T.mineHint : T.allHint}
              {mine && str(node.props.title) ? ` · ${str(node.props.title)}` : ''}
              {page.version > 1 ? ` · ${T.version(page.version)}` : ''}
            </div>
          )}
        </div>
        {page && page.items.length > 0 && (
          <span className="tnum shrink-0 text-2xs text-faint" data-records-count="">{T.count(rows.length, page.items.length)}</span>
        )}
        <button type="button" className="btn btn-xs btn-ghost shrink-0" onClick={() => { setHistory(null); setHistoryError(null); load() }}
                aria-label={T.reload} title={T.reload}>
          <RotateCw size={11} aria-hidden />
        </button>
      </div>
      {page?.state === 'empty' && !page.items.length ? <div className="px-3 py-3 text-2xs text-faint">{T.tableMissing}</div>
        : error && !page ? <ErrorState compact error={error} onRetry={load} className="m-2" />
        : !page ? <div className="flex items-center gap-2 px-3 py-3 text-2xs text-faint"><Spinner size={11} /></div>
        : (
          <>
            {page.items.length > 0 && (
              <div className="flex flex-wrap items-center gap-2 border-b px-3 py-1.5">
                <label className="relative min-w-0 flex-1">
                  <span className="sr-only">{T.filter}</span>
                  <Search size={11} aria-hidden className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-faint" />
                  <input className="field h-7 py-0 pl-6 text-xs" value={query} placeholder={T.filterPlaceholder}
                         onChange={(e) => setQuery(e.target.value)} data-records-filter="" />
                </label>
                <select className="field h-7 w-32 py-0 text-xs" value={order} aria-label={T.sort}
                        onChange={(e) => setOrder(e.target.value as 'newest' | 'oldest')} data-records-sort="">
                  <option value="newest">{T.newest}</option>
                  <option value="oldest">{T.oldest}</option>
                </select>
              </div>
            )}
            {!page.items.length ? <div className="px-3 py-3 text-2xs text-faint">{mine ? T.emptyMine : T.empty}</div>
              : !rows.length ? <div className="px-3 py-3 text-2xs text-faint">{T.emptyFiltered}</div>
              : (
                <ul>
                  {rows.map((r) => (
                    <RecordItem key={r._id} row={r} columns={columns} mine={mine} open={open.has(r._id)}
                                onToggle={() => setOpen((s) => { const n = new Set(s); if (n.has(r._id)) n.delete(r._id); else n.add(r._id); return n })}
                                historyOpen={historyFor.has(r._id)} onHistory={() => showHistory(r._id)}
                                history={history} historyError={historyError} />
                  ))}
                </ul>
              )}
            {page.next_cursor && (
              <div className="border-t px-3 py-2">
                <button type="button" className="btn btn-xs" disabled={more} onClick={() => void loadMore()} data-records-more="">
                  {more && <Spinner size={10} />} {T.more}
                </button>
              </div>
            )}
          </>
        )}
    </div>
  )
}

function RecordItem({ row, columns, mine, open, onToggle, historyOpen, onHistory, history, historyError }: {
  row: RecordRow; columns: RecordsPage['columns']; mine: boolean; open: boolean; onToggle: () => void
  historyOpen: boolean; onHistory: () => void; history: Map<string, RecordRow> | null; historyError: unknown
}) {
  // 收起时写前两列，够认出是哪一条；全部字段展开看
  const brief = columns.slice(0, 2)
  const corrected = !!row._supersedes
  const panelId = `record-${row._id}`
  return (
    <li className="border-b last:border-0" data-record={row._id} data-record-corrected={corrected ? '' : undefined}
        data-record-superseded={row.superseded ? '' : undefined}>
      <button type="button" className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs hover:bg-hover"
              aria-expanded={open} aria-controls={panelId} title={open ? T.collapse : T.expand} onClick={onToggle}>
        {open ? <ChevronDown size={12} aria-hidden className="shrink-0 text-faint" /> : <ChevronRight size={12} aria-hidden className="shrink-0 text-faint" />}
        <span className="min-w-0 flex-1 truncate">
          {brief.length
            ? brief.map((c, i) => (
              <span key={c.name}>{i > 0 && <span className="text-faint"> · </span>}<BriefValue v={row.values?.[c.name]} /></span>
            ))
            : T.seq(row._seq)}
        </span>
        {corrected && <span className="chip shrink-0" style={{ color: 'var(--st-waiting)' }} title={T.correctedHint}>{T.corrected}</span>}
        {row.superseded && <span className="chip shrink-0">{T.superseded}</span>}
        {!mine && row._submitted_by && <span className="hidden shrink-0 text-2xs text-dim sm:inline">{T.by(row._submitted_by)}</span>}
        <span className="shrink-0 text-2xs text-faint">{formatDateTime(row._submitted_at)}</span>
      </button>
      {open && (
        <div id={panelId} className="space-y-2 px-3 pb-3 pl-8 text-xs" data-record-detail="">
          <Fields row={row} columns={columns} />
          <div className="flex flex-wrap items-center gap-x-2 text-2xs text-faint">
            <span>{T.seq(row._seq)}</span>
            {row._submitted_by && <span>{T.by(row._submitted_by)}</span>}
            <span>{formatDateTime(row._submitted_at)}</span>
          </div>
          {corrected && (
            <div>
              <button type="button" className="btn btn-xs" aria-expanded={historyOpen} onClick={onHistory} data-record-history-toggle="">
                <History size={10} aria-hidden /> {historyOpen ? T.hideHistory : T.history}
              </button>
              {historyOpen && <HistoryChain row={row} columns={columns} history={history} error={historyError} />}
            </div>
          )}
        </div>
      )}
    </li>
  )
}

/** 收起时的一格：文件只写文件名，长文字截断交给外层 */
function BriefValue({ v }: { v: unknown }) {
  if (v == null || v === '') return <span className="text-faint">—</span>
  if (isFileRef(v)) return <span><Paperclip size={10} aria-hidden className="mr-0.5 inline text-faint" />{v.name}</span>
  if (Array.isArray(v)) return <span>{v.map((x) => (isFileRef(x) ? x.name : String(x))).join('、')}</span>
  if (typeof v === 'boolean') return <span>{v ? T.yes : T.no}</span>
  return <span>{typeof v === 'object' ? JSON.stringify(v) : String(v)}</span>
}

function Fields({ row, columns }: { row: RecordRow; columns: RecordsPage['columns'] }) {
  return (
    <dl className="grid grid-cols-[minmax(6rem,auto)_1fr] gap-x-3 gap-y-1">
      {columns.map((c) => (
        <div key={c.name} className="contents" data-record-field={c.name}>
          <dt className="text-faint">{c.type === 'file' ? <><Paperclip size={10} aria-hidden className="mr-0.5 inline" />{c.label || c.name}</> : c.label || c.name}</dt>
          <dd className="min-w-0"><Value v={row.values?.[c.name]} /></dd>
        </div>
      ))}
    </dl>
  )
}

/** 更正历史：顺着 _supersedes 往前找，最近的一次更正前在最上面 */
function HistoryChain({ row, columns, history, error }: {
  row: RecordRow; columns: RecordsPage['columns']; history: Map<string, RecordRow> | null; error: unknown
}) {
  if (error) return <ErrorState compact error={error} className="mt-2" />
  if (!history) return <div className="mt-2 flex items-center gap-2 text-2xs text-faint"><Spinner size={10} /> {T.historyLoading}</div>
  const chain: RecordRow[] = []
  const seen = new Set<string>([row._id])
  for (let at = row._supersedes; at && !seen.has(at); at = history.get(at)?._supersedes ?? null) {
    seen.add(at)
    const prev = history.get(at)
    if (!prev) break
    chain.push(prev)
  }
  if (!chain.length) return <div className="mt-2 text-2xs text-faint">{T.historyEmpty}</div>
  return (
    <ol className="mt-2 space-y-2 border-l pl-3" data-record-history={chain.length}>
      {chain.map((r) => (
        <li key={r._id} className="opacity-80" data-record-history-item={r._id}>
          <div className="mb-1 text-2xs text-faint">
            {T.historyItem(r._seq, formatDateTime(r._submitted_at))}{r._submitted_by ? ` · ${T.by(r._submitted_by)}` : ''}
          </div>
          <Fields row={r} columns={columns} />
        </li>
      ))}
    </ol>
  )
}
