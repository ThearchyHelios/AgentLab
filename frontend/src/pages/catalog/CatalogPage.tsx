import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { ArrowLeft, BookMarked, Database, ListFilter as ListFilterIcon, ScanSearch, Sparkles } from 'lucide-react'
import clsx from 'clsx'
import { ApiError, api } from '../../api/client'
import type { CatalogDetail, CatalogList, CatalogTableRow } from '../../types'
import { EmptyState, ErrorState, PageHeader, Skeleton, Spinner, toast, useTicker } from '../../components/ui'
import { useCatalog, useDatasources, useOnReconnect } from '../../store/catalog'
import { CATALOG_TEXT as CT, PROFILE_STOP_LABEL, PROFILE_TEXT as PT, SCHEMA_PARTIAL_TEXT as SPT } from '../../lib/terms'
import { StatusLegend, SystemNotesNotice } from './parts'
import { DraftDialog } from './DraftDialog'
import { ProfileDialog } from './ProfileDialog'
import type { ProfileJob } from './ProfileDialog'
import { ProfileSettingsDialog } from './ProfileSettings'
import { fillKey, profileBlockOf, profileSettingsOf, secondsText } from './profile'
import { TableDetail } from './TableDetail'
import { TableIndex } from './TableIndex'
import { filterCounts, listParamsOf, progressOf, rowFromDetail, usageSummary, visibleRows, writeListParams } from './model'
import type { ListFilter, ListParams, ListSort } from './model'

// ===========================================================================
// 数据目录页（/data/catalog/:sourceId，选中一张表时 /data/catalog/:sourceId/:table）。
//
// 用独立页面而不是弹窗：一个库 150 多张表、一张表 50 列，审阅是按使用次数一张接一张做的长活，要整屏的宽度
// 放左栏的表清单和右栏的列表格；地址里带着表名，可以把某张表直接发给同事核对，浏览器的前进后退、刷新都不丢
// 位置；编辑到一半换表、离开页面，走全站统一的离开前确认。
//
// 宽屏左右两栏；窄屏（< lg）一次只显示一栏：没选表时是清单，选了表是详情（详情里有「返回表清单」）。
//
// 清单的搜索词、筛选、排序和「只看运行中查询过的表」记在地址的查询参数里（q / filter / sort / used），刷新、前进后退
// 都不丢；在清单和表详情之间跳转时带着它们走。
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

  // 清单的状态以页面上的为准（点了立刻生效），再写进地址：搜索词停手 200ms 再写，其余马上写，都替换当前这条历史
  // （筛选不该占满后退键）。地址被别处改了（前进后退、点了带参数的链接）时按地址重来
  const [params, setParams] = useSearchParams()
  const fromUrl = useMemo(() => listParamsOf(params), [params])
  const urlSig = useMemo(() => writeListParams(new URLSearchParams(), fromUrl).toString(), [fromUrl])
  const [list, setList] = useState<ListParams>(fromUrl)
  const wrote = useRef(urlSig)
  useEffect(() => {
    if (urlSig === wrote.current) return
    wrote.current = urlSig
    setList(fromUrl)
  }, [urlSig, fromUrl])
  const listSig = useMemo(() => writeListParams(new URLSearchParams(), list).toString(), [list])
  const typedAt = useRef(0)
  useEffect(() => {
    if (listSig === wrote.current) return
    const t = setTimeout(() => {
      wrote.current = listSig
      setParams((prev) => writeListParams(prev, list), { replace: true })
    }, Date.now() - typedAt.current < 200 ? 200 : 0)
    return () => clearTimeout(t)
  }, [listSig, list, setParams])
  const { query, filter, sort } = list
  /** 只看运行中查询过的表：从顶部摘要点进来时打开，筛选结果和摘要的数字一致 */
  const usedOnly = list.used
  const setQuery = useCallback((q: string) => { typedAt.current = Date.now(); setList((c) => ({ ...c, query: q })) }, [])
  const setFilter = useCallback((f: ListFilter) => setList((c) => ({ ...c, filter: f })), [])
  const setSort = useCallback((v: ListSort) => setList((c) => ({ ...c, sort: v })), [])
  const setUsedOnly = useCallback((v: boolean) => setList((c) => ({ ...c, used: v })), [])
  /** 跳到别的表、回到清单时带上清单的查询参数（按页面上的算：搜索词可能还没写进地址） */
  const searchRef = useRef('')
  const search = writeListParams(params, list).toString()
  searchRef.current = search ? `?${search}` : ''
  const [selected, setSelected] = useState<Set<string>>(() => new Set())
  const [drafting, setDrafting] = useState(false)
  /** 起草写进了这些表：详情据此重新载入（编辑中的不动，保存时服务端会用版本号拦下） */
  const [drafted, setDrafted] = useState<{ seq: number; tables: Set<string> }>({ seq: 0, tables: new Set() })
  const [profiling, setProfiling] = useState(false)
  const [profileSettingsOpen, setProfileSettingsOpen] = useState(false)
  /** 一次剖析的状态。关掉弹窗剖析照常进行：状态留在页面上，页头有入口，完成后提示并可再打开报告 */
  const [job, setJob] = useState<(ProfileJob & { sourceId: string }) | null>(null)
  /** 剖析设置刚存过：数据源列表重取回来之前按存下的这份算（预算、开没开） */
  const [savedOptions, setSavedOptions] = useState<{ sourceId: string; options: Record<string, unknown> } | null>(null)
  /**
   * 从剖析报告点「填写含义」：报告先收起，打开那张表并弹出那一列的码值；码值弹窗关上（保存或取消）后回到报告，
   * 焦点落在下一列的「填写含义」上，一列接一列地填
   */
  const [fillCodes, setFillCodes] = useState<{ table: string; column: string; seq: number } | null>(null)
  /** 回到报告时从哪一列接着往下 */
  const [returnTo, setReturnTo] = useState<{ table: string; column: string } | null>(null)
  const profilingRef = useRef(profiling)
  profilingRef.current = profiling
  const alive = useRef(true)
  // 开发模式的严格模式会先卸载再挂载一次：挂载时要重新置为真
  useEffect(() => {
    alive.current = true
    return () => { alive.current = false }
  }, [])
  const sourceRef = useRef(sourceId)
  sourceRef.current = sourceId

  const rows = useMemo(() => data?.tables ?? [], [data])
  const visible = useMemo(() => visibleRows(rows, query, filter, sort, usedOnly), [rows, query, filter, sort, usedOnly])
  const counts = useMemo(() => filterCounts(rows, query, usedOnly), [rows, query, usedOnly])
  const base = `/data/catalog/${encodeURIComponent(sourceId)}`
  const openTable = useCallback((t: string) => navigate(`${base}/${encodeURIComponent(t)}${searchRef.current}`), [navigate, base])
  const backToList = useCallback(() => navigate(`${base}${searchRef.current}`), [navigate, base])
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

  const options = savedOptions?.sourceId === sourceId ? savedOptions.options : source?.options
  const profileSettings = useMemo(() => profileSettingsOf(options), [options])
  const myJob = job?.sourceId === sourceId ? job : null
  const jobRunning = myJob?.phase === 'running'
  useTicker(1000, jobRunning)

  /**
   * 剖析：同步请求，做完才返回。期间关掉弹窗不影响；回来时页面已经换了数据源或卸载了，只提示、不再改这一页。
   * 写进去的表重新载入清单和正在看的详情（编辑中的不动，同起草）
   */
  const startProfile = useCallback(async (tables: string[] | null) => {
    const src = sourceId
    const startedAt = Date.now()
    setJob({ sourceId: src, phase: 'running', startedAt, tables, settings: profileSettings })
    let next: ProfileJob
    try {
      const report = await api.dataCatalog.profile(src, tables ? { tables } : {})
      next = { phase: 'done', report, ms: Date.now() - startedAt }
    } catch (e) {
      next = e instanceof ApiError && e.status === 409
        ? { phase: 'blocked', kind: profileBlockOf(e.message), message: e.message, tables }
        : { phase: 'failed', error: e, tables }
    }
    const here = alive.current && sourceRef.current === src
    if (here) setJob({ ...next, sourceId: src })
    if (next.phase === 'done') {
      const r = next.report
      const touched = r.tables.filter((t) => !t.error && t.added + t.updated + t.removed > 0).map((t) => t.table_name)
      if (here) {
        if (touched.length) setDrafted((cur) => ({ seq: cur.seq + 1, tables: new Set(touched) }))
        void load()
      }
      // 弹窗开着就看报告；关了（在后台继续）才提示，并给「查看报告」
      if (!here || !profilingRef.current) {
        const summary = PT.summary(r.queries_used, r.settings.max_queries, r.tables.length)
        const text = r.stopped ? PT.toastStopped(PROFILE_STOP_LABEL[r.stopped] ?? r.stopped) : PT.toastDone(summary)
        const show = r.stopped ? toast.warn : toast.ok
        show(text, here ? { key: `profile:${src}`, duration: 10000, action: { label: PT.viewReport, onClick: () => setProfiling(true) } }
          : { key: `profile:${src}` })
      }
    } else if (!here || !profilingRef.current) {
      toast.error(next.phase === 'blocked' ? next.message : next.error, here
        ? { key: `profile:${src}`, action: { label: PT.viewReport, onClick: () => setProfiling(true) } } : { key: `profile:${src}` })
    }
  }, [sourceId, profileSettings, load])

  const openProfile = useCallback(() => {
    // 上一次的结果（报告、被拒）看过了，再点就是重新开始；正在剖析时打开的是进度
    setJob((cur) => (cur && cur.phase === 'running' && cur.sourceId === sourceId ? cur : null))
    setProfiling(true)
  }, [sourceId])

  const onProfileSettingsSaved = useCallback((row: any) => {
    setProfileSettingsOpen(false)
    setSavedOptions({ sourceId, options: row?.options ?? {} })
    void useCatalog.getState().reload('datasources')
    // 因为没开启被拒的：开了就回到选范围，可以直接开始
    setJob((cur) => (cur && cur.phase === 'blocked' && cur.kind === 'disabled' ? null : cur))
  }, [sourceId])

  const openFromReport = useCallback((t: string) => {
    setProfiling(false)
    openTable(t)
  }, [openTable])

  const fillFromReport = useCallback((t: string, column: string) => {
    setProfiling(false)
    setFillCodes((cur) => ({ table: t, column, seq: (cur?.seq ?? 0) + 1 }))
    openTable(t)
  }, [openTable])

  const jobRef = useRef(job)
  jobRef.current = job
  /** 从报告打开的码值弹窗关上了：记下这一列还剩几个含义待填写（报告里跟着改），回到报告 */
  const onFillDone = useCallback((seq: number, pending: number | null) => {
    const fill = fillCodes?.seq === seq ? fillCodes : null
    const cur = jobRef.current
    if (!fill || !alive.current || cur?.phase !== 'done' || cur.sourceId !== sourceRef.current) return
    if (pending != null) {
      setJob((j) => (j && j.phase === 'done' ? { ...j, filled: { ...j.filled, [fillKey(fill.table, fill.column)]: pending } } : j))
    }
    setReturnTo({ table: fill.table, column: fill.column })
    setProfiling(true)
  }, [fillCodes])

  // 上一张 / 下一张按清单当前的筛选和排序走；正在看的表不在筛选结果里（刚确认完、从关联关系跳过来）时按全部表走
  const [prev, next] = useMemo(() => {
    if (!table) return [null, null]
    const list = visible.some((r) => r.table_name === table) ? visible : visibleRows(rows, '', 'all', sort)
    const i = list.findIndex((r) => r.table_name === table)
    if (i < 0) return [null, null]
    return [list[i - 1]?.table_name ?? null, list[i + 1]?.table_name ?? null]
  }, [table, visible, rows, sort])

  const usage = useMemo(() => usageSummary(rows), [rows])
  /**
   * 顶部摘要点进来：清掉搜索词，切到对应的筛选、只看运行中查询过的表（和摘要同一个口径：摘要说「1 张还没有目录」，
   * 点进去就是那 1 张，不是全部没有目录的表），按使用次数排——用到最多的排在最前。窄屏一次只显示一栏，
   * 正在看表详情时回到清单才看得到筛选结果；宽屏两栏都在，正在看的表不动
   */
  const focusUsed = useCallback((f: ListFilter) => {
    const next: ListParams = { query: '', filter: f, used: true, sort: 'usage' }
    setList(next)
    if (table && typeof matchMedia === 'function' && matchMedia('(max-width: 1023px)').matches) {
      const q = writeListParams(params, next).toString()
      navigate(`${base}${q ? `?${q}` : ''}`)
    }
  }, [table, navigate, base, params])

  const filtered = !!query.trim() || filter !== 'all' || usedOnly
  const empty = rows.length > 0 && rows.every((r) => progressOf(r.counts) === 'none')
  const hasTables = rows.length > 0
  const draftButton = (primary = false) => (
    <button type="button" className={clsx('btn btn-sm', primary && 'btn-primary')} onClick={() => setDrafting(true)} data-catalog-draft="">
      <Sparkles size={11} aria-hidden /> {CT.draft}
    </button>
  )
  // 页头的「数据剖析」：剖析进行中换成计时，点开看进度（关掉弹窗剖析照常进行）
  const headerProfile = jobRunning && myJob?.phase === 'running'
    ? (
      <button type="button" className="btn btn-sm" onClick={() => setProfiling(true)} title={PT.headerRunningHint}
              aria-label={PT.headerRunning(secondsText(Date.now() - myJob.startedAt))} data-catalog-profile="running">
        <Spinner size={11} /> <span className="tnum hidden sm:inline">{PT.headerRunning(secondsText(Date.now() - myJob.startedAt))}</span>
      </button>
    )
    : (
      <button type="button" className="btn btn-sm" onClick={openProfile} aria-label={PT.action} title={PT.actionHint} data-catalog-profile="">
        <ScanSearch size={11} aria-hidden /> <span className="hidden sm:inline">{PT.action}</span>
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
            usedOnly={usedOnly}
            onUsedOnly={setUsedOnly}
            sort={sort}
            onSort={setSort}
            selected={selected}
            onSelected={setSelected}
            active={table}
            onOpen={openTable}
            onDraft={() => setDrafting(true)}
            onProfile={openProfile}
          />
        </aside>
        <section className={clsx('min-h-0 min-w-0 flex-1 flex-col lg:flex', table ? 'flex' : 'hidden')} aria-label={table ?? CT.overviewTitle}>
          {table
            ? (
              <TableDetail sourceId={sourceId} table={table} rows={rows} drafted={drafted} fillCodes={fillCodes} prev={prev} next={next}
                           onFillDone={onFillDone} onOpen={openTable} onBack={backToList} onDetail={onDetail} />
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
            {hasTables && headerProfile}
            {hasTables && headerDraft}
            <button type="button" className="btn btn-sm btn-ghost" onClick={() => navigate(back)} aria-label={CT.back} data-catalog-back="">
              <ArrowLeft size={12} aria-hidden /> <span className="hidden sm:inline">{CT.back}</span>
            </button>
          </div>
        )}
      />
      {hasTables && data?.schema_truncated && (
        <SchemaPartialNotice total={data.schema_total ?? 0} explored={rows.filter((r) => r.in_schema).length}
                             onGoSource={() => navigate(back)} />
      )}
      {hasTables && usage.used > 0 && !empty && <UsageSummary {...usage} active={usedOnly ? filter : null} onFocus={focusUsed} />}
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
      {profiling && data && (
        <ProfileDialog
          sourceName={source?.name ?? sourceId}
          settings={profileSettings}
          rows={rows}
          visible={visible}
          selected={[...selected]}
          filtered={filtered}
          job={myJob}
          onStart={(tables) => void startProfile(tables)}
          onClose={() => { setProfiling(false); setReturnTo(null) }}
          returnTo={returnTo}
          onSettings={() => setProfileSettingsOpen(true)}
          onGoSource={() => navigate(back)}
          onOpenTable={openFromReport}
          onFillCodes={fillFromReport}
          onReset={() => setJob(null)}
        />
      )}
      {profileSettingsOpen && source && (
        <ProfileSettingsDialog row={{ id: source.id, name: source.name, options }} onClose={() => setProfileSettingsOpen(false)}
                               onSaved={onProfileSettingsSaved} />
      )}
    </div>
  )
}

/**
 * 表结构只探查了一部分（每个数据源最多取 200 张表）：没探查到的表不在清单里，助手也看不到。顶部常驻一条说明，
 * 写清一共几张、探查了几张、为什么只取了这些，以及下一步——回到数据源卡片换 schema 或缩小范围后重新探查
 */
function SchemaPartialNotice({ total, explored, onGoSource }: { total: number; explored: number; onGoSource: () => void }) {
  const shownTotal = Math.max(total, explored)
  return (
    <div className="flex shrink-0 items-start gap-2 border-b px-4 py-2 text-xs"
         style={{ background: 'color-mix(in srgb, var(--st-waiting) 6%, var(--bg-panel))' }}
         data-catalog-schema-partial={`${shownTotal},${explored}`}>
      <Database size={12} className="mt-0.5 shrink-0" style={{ color: 'var(--st-waiting)' }} aria-hidden />
      <div className="min-w-0 flex-1 leading-relaxed">
        <p className="font-medium">{SPT.title(shownTotal, explored)}</p>
        <p className="text-2xs text-dim">{SPT.body(Math.max(shownTotal - explored, 0))}</p>
        <p className="text-2xs text-dim">{SPT.next}</p>
      </div>
      <button type="button" className="btn btn-sm shrink-0" onClick={onGoSource} data-catalog-schema-partial-go="">
        {CT.back}
      </button>
    </div>
  )
}

/**
 * 「用到但没确认」：运行中查询过的表里还有几张有推断项、几张没有目录。这些表的推断项会被助手当真用，最该先审。
 * 两段各是一个按钮，点了切到清单的对应筛选、按使用次数排；都审完了只写一句，不给按钮
 */
function UsageSummary({ used, pending, none, active, onFocus }: {
  used: number
  pending: number
  none: number
  /** 清单正按摘要的哪一段筛（只看运行中查询过的表时）；没有按摘要筛为 null */
  active: ListFilter | null
  onFocus: (f: ListFilter) => void
}) {
  const part = (f: 'pending' | 'none', text: string, hint: string) => (
    <button type="button" onClick={() => onFocus(f)} title={hint} aria-pressed={active === f} data-usage-focus={f}
            className={clsx('rounded px-1 font-medium underline decoration-dotted underline-offset-2 outline-none',
              'hover:bg-hover focus-visible:ring-2 focus-visible:ring-[var(--accent)]',
              active === f ? 'text-[var(--accent)]' : 'text-fg')}>
      {text}
    </button>
  )
  const todo = pending > 0 || none > 0
  return (
    <div className="flex shrink-0 items-start gap-2 border-b px-4 py-2 text-xs"
         style={todo ? { background: 'color-mix(in srgb, var(--st-waiting) 6%, var(--bg-panel))' } : undefined}
         data-catalog-usage-summary={`${used},${pending},${none}`}>
      <ListFilterIcon size={12} className="mt-0.5 shrink-0" style={{ color: todo ? 'var(--st-waiting)' : 'var(--st-done)' }} aria-hidden />
      {todo
        ? (
          <p className="min-w-0 leading-relaxed text-dim">
            {CT.usageLead(used)}
            {pending > 0 && part('pending', CT.usagePending(pending), CT.usageHintPending)}
            {pending > 0 && none > 0 && '，'}
            {none > 0 && part('none', CT.usageNone(none), CT.usageHintNone)}
          </p>
        )
        : <p className="min-w-0 text-dim">{CT.usageAllDone(used)}</p>}
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

/**
 * 审阅进度：有待确认项 / 没有待确认项 / 没有目录的表各占多少。色段的先后和上面那句话一致（有待确认项在前），
 * 下面挂图例说明每种颜色。数字已经写在那句话里，色条和图例只给眼睛看，读屏不重复念
 */
const PROGRESS: { key: 'pending' | 'done' | 'none'; color: string }[] = [
  { key: 'pending', color: 'var(--st-waiting)' },
  { key: 'done', color: 'var(--st-done)' },
  { key: 'none', color: 'var(--border-strong)' },
]

function ProgressBar(counts: { pending: number; done: number; none: number }) {
  const total = counts.pending + counts.done + counts.none || 1
  return (
    <div aria-hidden>
      <div className="flex h-1.5 overflow-hidden rounded-full bg-hover" data-catalog-progress={PROGRESS.map((p) => p.key).join(',')}>
        {PROGRESS.map(({ key, color }) => (counts[key] > 0
          ? <span key={key} className="h-full" style={{ width: `${(counts[key] / total) * 100}%`, background: color }}
                  title={`${CT.filter[key]} ${counts[key]}`} data-progress-seg={key} />
          : null))}
      </div>
      <ul className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-2xs text-faint" data-catalog-progress-legend="">
        {PROGRESS.map(({ key, color }) => (
          <li key={key} className="inline-flex items-center gap-1.5" data-legend={key}>
            <span className="h-2 w-2 shrink-0 rounded-sm" style={{ background: color }} />
            {CT.filter[key]}
          </li>
        ))}
      </ul>
    </div>
  )
}
