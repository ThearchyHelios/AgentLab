import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, BookMarked, Database, Sparkles } from 'lucide-react'
import clsx from 'clsx'
import { ApiError, api } from '../../api/client'
import type { CatalogDetail, CatalogList, CatalogTableRow } from '../../types'
import { EmptyState, ErrorState, PageHeader, Skeleton, toast } from '../../components/ui'
import { useDatasources, useOnReconnect } from '../../store/catalog'
import { CATALOG_TEXT as CT } from '../../lib/terms'
import { StatusLegend, SystemNotesNotice } from './parts'
import { DraftDialog } from './DraftDialog'
import { TableDetail } from './TableDetail'
import { TableIndex } from './TableIndex'
import { filterCounts, progressOf, rowFromDetail, visibleRows } from './model'
import type { ListFilter, ListSort } from './model'

// ===========================================================================
// 数据目录页（/data/catalog/:sourceId，选中一张表时 /data/catalog/:sourceId/:table）。
//
// 用独立页面而不是弹窗：一个库 150 多张表、一张表 50 列，审阅是按使用次数一张接一张做的长活，要整屏的宽度
// 放左栏的表清单和右栏的列表格；地址里带着表名，可以把某张表直接发给同事核对，浏览器的前进后退、刷新都不丢
// 位置；编辑到一半换表、离开页面，走全站统一的离开前确认。
//
// 宽屏左右两栏；窄屏（< lg）一次只显示一栏：没选表时是清单，选了表是详情（详情里有「返回表清单」）。
// ===========================================================================

export function CatalogPage() {
  const { sourceId = '', table } = useParams()
  const navigate = useNavigate()
  const { list: sources } = useDatasources()
  const source = sources.find((s) => s.id === sourceId)
  const [data, setData] = useState<CatalogList | null>(null)
  const [loadError, setLoadError] = useState<unknown>(null)
  const loadSeq = useRef(0)
  const hasData = useRef(false)

  const load = useCallback(async () => {
    const seq = ++loadSeq.current
    try {
      const next = await api.dataCatalog.list(sourceId)
      if (seq !== loadSeq.current) return
      hasData.current = true
      setData(next)
      setLoadError(null)
    } catch (e) {
      if (seq !== loadSeq.current) return
      // 首次没取到才整页换成错误；之后的刷新失败保留旧清单，只弹提示
      if (hasData.current) toast.error(e)
      else setLoadError(e)
    }
  }, [sourceId])
  useEffect(() => {
    hasData.current = false
    setData(null)
    setLoadError(null)
    void load()
  }, [load])
  useOnReconnect(() => { void load() })

  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<ListFilter>('all')
  const [sort, setSort] = useState<ListSort>('usage')
  const [selected, setSelected] = useState<Set<string>>(() => new Set())
  const [drafting, setDrafting] = useState(false)
  /** 起草写进了这些表：详情据此重新载入（编辑中的不动，保存时服务端会用版本号拦下） */
  const [drafted, setDrafted] = useState<{ seq: number; tables: Set<string> }>({ seq: 0, tables: new Set() })

  const rows = useMemo(() => data?.tables ?? [], [data])
  const visible = useMemo(() => visibleRows(rows, query, filter, sort), [rows, query, filter, sort])
  const counts = useMemo(() => filterCounts(rows, query), [rows, query])
  const base = `/data/catalog/${encodeURIComponent(sourceId)}`
  const openTable = useCallback((t: string) => navigate(`${base}/${encodeURIComponent(t)}`), [navigate, base])
  const back = source?.origin === 'upload' ? '/data/tables' : '/data/databases'

  /** 写入、审阅之后按返回的单表目录更新清单里那一行 */
  const onDetail = useCallback((d: CatalogDetail) => {
    setData((cur) => cur && ({
      ...cur,
      tables: cur.tables.map((r) => (r.table_name === d.table_name ? rowFromDetail(d, r) : r)),
    }))
  }, [])

  const onDrafted = useCallback((tables: string[]) => {
    setDrafted((cur) => ({ seq: cur.seq + 1, tables: new Set(tables) }))
    setSelected(new Set())
    void load()
  }, [load])

  // 上一张 / 下一张按清单当前的筛选和排序走；正在看的表不在筛选结果里（刚确认完、从关联关系跳过来）时按全部表走
  const [prev, next] = useMemo(() => {
    if (!table) return [null, null]
    const list = visible.some((r) => r.table_name === table) ? visible : visibleRows(rows, '', 'all', sort)
    const i = list.findIndex((r) => r.table_name === table)
    if (i < 0) return [null, null]
    return [list[i - 1]?.table_name ?? null, list[i + 1]?.table_name ?? null]
  }, [table, visible, rows, sort])

  const filtered = !!query.trim() || filter !== 'all'
  const empty = rows.length > 0 && rows.every((r) => progressOf(r.counts) === 'none')
  const hasTables = rows.length > 0
  const draftButton = (primary = false) => (
    <button type="button" className={clsx('btn btn-sm', primary && 'btn-primary')} onClick={() => setDrafting(true)} data-catalog-draft="">
      <Sparkles size={11} aria-hidden /> {CT.draft}
    </button>
  )
  // 页头的「起草」窄屏只留图标，标题才放得下
  const headerDraft = (
    <button type="button" className="btn btn-sm" onClick={() => setDrafting(true)} aria-label={CT.draft} title={CT.draft} data-catalog-draft="">
      <Sparkles size={11} aria-hidden /> <span className="hidden sm:inline">{CT.draft}</span>
    </button>
  )

  let body: ReactNode
  if (!data) {
    body = loadError
      ? (loadError instanceof ApiError && loadError.status === 404
        ? <EmptyState icon={<BookMarked size={22} />} title={CT.sourceMissing} body={CT.sourceMissingBody} offline={false}
                      action={<button className="btn btn-sm" onClick={() => navigate('/data')}>{CT.back}</button>} />
        : <div className="mx-auto max-w-xl p-6"><ErrorState error={loadError} onRetry={() => void load()} /></div>)
      : <div className="p-4"><Skeleton rows={8} height={36} gap={8} /></div>
  } else if (!hasTables) {
    // 没有表结构：先去探查。上传的表格没有「探查结构」，只能重新上传
    body = (
      <div data-catalog-no-schema="">
        <EmptyState
          icon={<Database size={22} />}
          title={CT.noSchemaTitle}
          body={source?.origin === 'upload' ? CT.noSchemaUploadBody : CT.noSchemaBody(data.schema_note ?? '')}
          offline={false}
          action={<button className="btn btn-sm btn-primary" onClick={() => navigate(back)} data-catalog-go-introspect="">{CT.back}</button>}
        />
      </div>
    )
  } else {
    body = (
      <div className="flex min-h-0 flex-1">
        <aside className={clsx('min-h-0 w-full flex-col border-r lg:flex lg:w-[380px] lg:shrink-0 xl:w-[420px]', table ? 'hidden' : 'flex')}>
          {empty && (
            // 窄屏没有右栏的概览，空目录的下一步写在清单上面
            <div className="border-b px-3 py-2.5 text-xs lg:hidden" data-catalog-empty-banner="">
              <p className="font-medium">{CT.emptyTitle}</p>
              <p className="mt-0.5 text-2xs leading-relaxed text-faint">{CT.emptyBody}</p>
              <div className="mt-2">{draftButton(true)}</div>
            </div>
          )}
          <TableIndex
            rows={visible}
            total={rows.length}
            counts={counts}
            query={query}
            onQuery={setQuery}
            filter={filter}
            onFilter={setFilter}
            sort={sort}
            onSort={setSort}
            selected={selected}
            onSelected={setSelected}
            active={table}
            onOpen={openTable}
            onDraft={() => setDrafting(true)}
          />
        </aside>
        <section className={clsx('min-h-0 min-w-0 flex-1 flex-col lg:flex', table ? 'flex' : 'hidden')} aria-label={table ?? CT.overviewTitle}>
          {table
            ? (
              <TableDetail sourceId={sourceId} table={table} rows={rows} drafted={drafted} prev={prev} next={next}
                           onOpen={openTable} onBack={() => navigate(base)} onDetail={onDetail} />
            )
            : <Overview rows={rows} empty={empty} systemNotes={data.system_notes} draft={draftButton(true)} onOpen={openTable} />}
        </section>
      </div>
    )
  }

  return (
    <div className="flex h-full flex-col" data-catalog-page={sourceId}>
      <PageHeader
        icon={<BookMarked size={13} />}
        title={CT.title(source?.name ?? sourceId)}
        subtitle={CT.subtitle}
        actions={(
          <div className="flex shrink-0 items-center gap-1.5">
            {hasTables && headerDraft}
            <button type="button" className="btn btn-sm btn-ghost" onClick={() => navigate(back)} aria-label={CT.back} data-catalog-back="">
              <ArrowLeft size={12} aria-hidden /> <span className="hidden sm:inline">{CT.back}</span>
            </button>
          </div>
        )}
      />
      {body}
      {drafting && data && (
        <DraftDialog
          sourceId={sourceId}
          rows={rows}
          visible={visible}
          selected={[...selected]}
          filtered={filtered}
          onClose={() => setDrafting(false)}
          onDrafted={onDrafted}
        />
      )}
    </div>
  )
}

/** 没有选中表时的右栏：空目录时引导起草；否则写审阅进度，给出下一张该审的表 */
function Overview({ rows, empty, systemNotes, draft, onOpen }: {
  rows: CatalogTableRow[]
  empty: boolean
  systemNotes: boolean
  draft: ReactNode
  onOpen: (table: string) => void
}) {
  const pendingNext = rows.find((r) => r.in_schema && r.counts.proposed > 0)
  const stats = { pending: 0, done: 0, none: 0 }
  for (const r of rows) stats[progressOf(r.counts)]++
  return (
    <div className="relative min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto max-w-xl space-y-4 p-6" data-catalog-overview="">
        {empty
          ? <EmptyState icon={<BookMarked size={22} />} title={CT.emptyTitle} body={CT.emptyBody} offline={false} action={draft} className="!py-8" />
          : (
            <div className="space-y-3">
              <h2 className="text-sm font-semibold">{CT.overviewTitle}</h2>
              <p className="text-xs leading-relaxed text-dim">{CT.overviewBody}</p>
              <p className="tnum text-xs text-dim" data-catalog-stats={`${stats.pending},${stats.done},${stats.none}`}>
                {CT.overviewStats(rows.length, stats.pending, stats.done, stats.none)}
              </p>
              <ProgressBar pending={stats.pending} done={stats.done} none={stats.none} />
              {pendingNext && (
                <div className="flex flex-wrap items-center gap-2 pt-1">
                  <button type="button" className="btn btn-primary btn-sm" onClick={() => onOpen(pendingNext.table_name)}
                          data-catalog-review-next={pendingNext.table_name}>
                    {CT.reviewNext(pendingNext.label ?? pendingNext.table_name)}
                  </button>
                  <span className="text-2xs text-faint">{CT.reviewNextHint}</span>
                </div>
              )}
            </div>
          )}
        {systemNotes && <SystemNotesNotice />}
        <div className="rounded-lg border bg-panel p-3">
          <div className="mb-2 text-2xs font-medium text-dim">{CT.legend}</div>
          <StatusLegend detailed />
        </div>
      </div>
    </div>
  )
}

/** 审阅进度：有未确认项 / 全部已确认 / 没有目录的表各占多少 */
function ProgressBar({ pending, done, none }: { pending: number; done: number; none: number }) {
  const total = pending + done + none || 1
  const seg = (n: number, color: string, label: string) => (n > 0
    ? <span className="h-full" style={{ width: `${(n / total) * 100}%`, background: color }} title={`${label} ${n}`} />
    : null)
  return (
    <div className="flex h-1.5 overflow-hidden rounded-full bg-hover" aria-hidden>
      {seg(done, 'var(--st-done)', CT.filter.done)}
      {seg(pending, 'var(--st-waiting)', CT.filter.pending)}
      {seg(none, 'var(--border-strong)', CT.filter.none)}
    </div>
  )
}
