import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent, ReactNode } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import {
  ArrowLeft, BookMarked, Clock, Database, Download, ListFilter as ListFilterIcon, ScanSearch, Sparkles, Upload,
} from 'lucide-react'
import clsx from 'clsx'
import { ApiError, api } from '../../api/client'
import type {
  CatalogDetail, CatalogList, CatalogProfileTaskStatus, CatalogReviewItem, CatalogReviewKind, CatalogTableRow,
} from '../../types'
import { EmptyState, ErrorState, PageHeader, Skeleton, Spinner, rovingTarget, toast } from '../../components/ui'
import { useCatalog, useDatasources, useOnReconnect } from '../../store/catalog'
import { errorMessage } from '../../lib/errors'
import { formatNumber } from '../../lib/format'
import {
  CATALOG_DRAFT_TEXT as DT, CATALOG_REVIEW_TEXT as RT, CATALOG_TEXT as CT, CATALOG_TRANSFER_TEXT as XT, JOB_STRIP_TEXT as JT,
  PROFILE_TEXT as PT, SCHEMA_PARTIAL_TEXT as SPT, SOURCE_CHANGE_TEXT as GT,
} from '../../lib/terms'
import { StatusLegend, SystemNotesNotice } from './parts'
import { DraftDialog } from './DraftDialog'
import { isJobActive, jobBlank, retryableCount, useDraftJob } from './draftJob'
import type { DraftPreset, DraftStartBody } from './draftJob'
import { ProfileDialog } from './ProfileDialog'
import { ProfileSettingsDialog } from './ProfileSettings'
import { fillKey, profileSettingsOf } from './profile'
import { retryableTasks, useProfileJob } from './profileJob'
import type { ProfileStartBody } from './profileJob'
import { TableDetail } from './TableDetail'
import type { TableProfileProps } from './TableDetail'
import { TableIndex } from './TableIndex'
import { ReviewOverview, ReviewQueue } from './ReviewQueue'
import { ImportDialog } from './TransferDialog'
import { itemKey, reviewParamsOf, writeReviewParams } from './review'
import { ChangesQueue } from './ChangesQueue'
import { ChangeDetail, ChangesOverview } from './ChangeDetail'
import { changesParamsOf, clearChanges, isChangesView, writeChangesParams, writtenTables } from './changes'
import { JobStrip, stripItems, useSourceJobs } from './JobStrip'
import type { StripItem } from './JobStrip'
import type { SourceChangeCategory, SourceChangeCounts, SourceChangeResolveOut, SourceChangeStatusFilter } from '../../typesSource'
import { dueCount, filterCounts, listParamsOf, progressOf, rowFromDetail, usageSummary, visibleRows, writeListParams } from './model'
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
//
// 左栏有三个视图：表清单（逐表看）、审阅队列（view=review：跨表按优先级列出推断项，成批确认、驳回，筛选记在 kind /
// rtable 里）和变化（view=changes：探查、取值观察测到的结构变化、取值变化，按要处理 / 提醒 / 说明分组，筛选记在 cstatus /
// ccat 里）。从队列点开一项，右栏是所在表的详情，地址带着那一项（item），详情滚到那一项、列出用过这张表的运行；从变化里点开
// 一条（change），右栏是它的依据、影响面和处理。
// 页头的「导出目录」「导入目录」把整理好的目录带到另一个实例（导入先预览，确认后才写）。
//
// 起草、剖析都是服务端的后台任务（draftJob.ts、profileJob.ts），状态在这一页上：关掉对话框照常进行；结束时右下角提示，可以
// 回到结果。同一个数据源的探查、起草、剖析在同一条队里排队：一个在排队、进行中时，另一个建出来是排队，前一个结束后自动开始。
// 进度只在页头下方的后台任务条上（JobStrip，按服务端给的队列先后列出，next-round 05 (g)）；页头的「起草」「数据剖析」只是
// 入口，进行中换转圈、排队中换时钟，读屏名称写状态、不写数字。
// 地址里带 pjob / djob 时打开那个剖析、起草任务的进度（定时维护的「最近一次」从数据源页链过来）。
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
  /** 只看需要剖析的表（和剖析范围「需要剖析的表」同一个口径） */
  const dueOnly = !!list.due
  const setQuery = useCallback((q: string) => { typedAt.current = Date.now(); setList((c) => ({ ...c, query: q })) }, [])
  const setFilter = useCallback((f: ListFilter) => setList((c) => ({ ...c, filter: f })), [])
  const setSort = useCallback((v: ListSort) => setList((c) => ({ ...c, sort: v })), [])
  const setUsedOnly = useCallback((v: boolean) => setList((c) => ({ ...c, used: v })), [])
  const setDueOnly = useCallback((v: boolean) => setList((c) => ({ ...c, due: v })), [])
  /**
   * 跳到别的表、回到清单时带上清单的查询参数（按页面上的算：搜索词可能还没写进地址）。审阅视图的参数（view、kind、rtable）
   * 照旧带着，从审阅队列打开的那一项（item）不带：换了表就不是那一项了
   */
  const searchRef = useRef('')
  const carried = writeListParams(params, list)
  carried.delete('item')
  const search = carried.toString()
  searchRef.current = search ? `?${search}` : ''
  const carriedRef = useRef(carried)
  carriedRef.current = carried
  /** 审阅视图：view=review 时左栏是审阅队列；kind、rtable 是队列的筛选；item 是右栏从队列打开的那一项 */
  const review = useMemo(() => reviewParamsOf(params), [params])
  /** 变化视图：view=changes 时左栏是变化列表；cstatus、ccat 是筛选；change 是右栏打开的那一条 */
  const inChanges = isChangesView(params)
  const changesParams = useMemo(() => changesParamsOf(params), [params])
  const pageView: PageView = inChanges ? 'changes' : review.view
  const inReview = pageView === 'review'
  const openChangeId = inChanges ? changesParams.change || null : null
  const [selected, setSelected] = useState<Set<string>>(() => new Set())
  /**
   * 起草对话框开着没有、打开时显示什么：setup 选范围开始；job 显示页面跟着的任务（进行中的进度、刚结束的结果）。
   * busy：是撞上已有任务（409）转过去显示的；preset：设置阶段预先选好的范围（表详情里「补充起草要求」）
   */
  const [draftOpen, setDraftOpen] = useState<{ view: 'setup' | 'job'; busy?: boolean; preset?: DraftPreset } | null>(null)
  /** 起草写进了这些表：详情据此重新载入（编辑中的不动，保存时服务端会用版本号拦下） */
  const [drafted, setDrafted] = useState<{ seq: number; tables: Set<string> }>({ seq: 0, tables: new Set() })
  /**
   * 剖析对话框开着没有、打开时显示什么：setup 选范围开始；job 显示页面跟着的任务（进行中的进度、刚结束的结果）。
   * scope：从表清单的「剖析所选」打开，范围默认是已选的表
   */
  const [profileOpen, setProfileOpen] = useState<{ view: 'setup' | 'job'; scope?: 'selected' } | null>(null)
  const [profileSettingsOpen, setProfileSettingsOpen] = useState(false)
  /** 剖析设置刚存过：数据源列表重取回来之前按存下的这份算（预算、开没开） */
  const [savedOptions, setSavedOptions] = useState<{ sourceId: string; options: Record<string, unknown> } | null>(null)
  /**
   * 从剖析结果点「填写含义」：对话框先收起，打开那张表并弹出那一列的码值；码值弹窗关上（保存或取消）后回到结果，
   * 那张表照旧展开，焦点落在下一列的「填写含义」上，一列接一列地填
   */
  const [fillCodes, setFillCodes] = useState<{ table: string; column: string; seq: number } | null>(null)
  /** 回到结果时从哪一列接着往下 */
  const [returnTo, setReturnTo] = useState<{ table: string; column: string } | null>(null)
  /** 从结果去填了含义的码值列：保存后还剩几个待填写（按任务记，换了任务就不算） */
  const [filled, setFilled] = useState<{ jobId: string; map: Record<string, number> } | null>(null)
  const profileOpenRef = useRef(profileOpen)
  profileOpenRef.current = profileOpen
  const alive = useRef(true)
  // 开发模式的严格模式会先卸载再挂载一次：挂载时要重新置为真
  useEffect(() => {
    alive.current = true
    return () => { alive.current = false }
  }, [])
  const sourceRef = useRef(sourceId)
  sourceRef.current = sourceId

  const rows = useMemo(() => data?.tables ?? [], [data])
  const visible = useMemo(() => visibleRows(rows, query, filter, sort, usedOnly, dueOnly), [rows, query, filter, sort, usedOnly, dueOnly])
  const counts = useMemo(() => filterCounts(rows, query, usedOnly, dueOnly), [rows, query, usedOnly, dueOnly])
  const dueTotal = useMemo(() => dueCount(rows, query, usedOnly), [rows, query, usedOnly])
  /** 服务端给不给「需要剖析」的口径（老服务端每行没有 profile_due）：不给就不出「需要剖析」开关 */
  const dueKnown = useMemo(() => rows.some((r) => r.profile_due !== undefined), [rows])
  const base = `/data/catalog/${encodeURIComponent(sourceId)}`
  const openTable = useCallback((t: string) => navigate(`${base}/${encodeURIComponent(t)}${searchRef.current}`), [navigate, base])
  const backToList = useCallback(() => navigate(`${base}${searchRef.current}`), [navigate, base])
  const back = source?.origin === 'upload' ? '/data/tables' : '/data/databases'

  /** 右栏的表详情载入、改过之后的目录：审阅队列里这张表的项跟着换状态 */
  const [detailSync, setDetailSync] = useState<{ seq: number; detail: CatalogDetail } | null>(null)
  /** 写入、审阅之后按返回的单表目录更新清单里那一行 */
  const onDetail = useCallback((d: CatalogDetail) => {
    setData((cur) => cur && ({
      ...cur,
      tables: cur.tables.map((r) => (r.table_name === d.table_name ? rowFromDetail(d, r) : r)),
    }))
    setDetailSync((cur) => ({ seq: (cur?.seq ?? 0) + 1, detail: d }))
  }, [])

  /**
   * 起草任务（后台进行，状态在页面上）：每张表一结束就重取清单、正在看的表在里面就重新载入（编辑中的不动，保存时服务端
   * 会用版本号拦下）；结束时重取一次清单。对话框关着时结束的，弹提示并给「查看结果」
   */
  const draftOpenRef = useRef(draftOpen)
  draftOpenRef.current = draftOpen
  const draft = useDraftJob(sourceId, data?.active_job, {
    onTablesEnded: (tables) => {
      setDrafted((cur) => ({ seq: cur.seq + 1, tables: new Set(tables) }))
      void load()
    },
    onFinished: (job) => {
      void load()
      if (draftOpenRef.current) return
      const src = job.source_id
      const opts = {
        key: `catalog-draft:${src}`, duration: 10000,
        action: { label: DT.viewResult, onClick: () => { if (sourceRef.current === src) setDraftOpen({ view: 'job' }) } },
      }
      // 留空的列不算没写全，提示里接着写一句，点「查看结果」看是哪几张表、怎么办
      const blank = jobBlank(job)
      if (job.status === 'succeeded') toast.ok(DT.toast.withBlank(DT.toast.succeeded(CT.draftSummary(job.added, job.updated, job.removed)), blank), opts)
      else if (job.status === 'partial') toast.warn(DT.toast.withBlank(DT.toast.partial(retryableCount(job)), blank), opts)
      else if (job.status === 'failed') toast.error(DT.toast.failed(job.error ?? ''), opts)
      else if (job.status === 'interrupted') toast.warn(DT.toast.interrupted, opts)
      else toast.warn(DT.toast.cancelled, opts)
    },
  })
  const draftJob = draft.job
  const draftActive = isJobActive(draftJob)
  /** 打开起草对话框：有进行中的任务就直接显示它；上一次的结果看过了，再点就是重新开始 */
  const openDraft = useCallback(() => setDraftOpen({ view: draftActive ? 'job' : 'setup' }), [draftActive])
  const startDraft = useCallback(async (body: DraftStartBody) => {
    const r = await draft.start(body)
    // 选中的表已经交给起草任务了：清空，免得接着点「起草所选」又起草一遍
    if (!r.busy) setSelected(new Set())
    return r
  }, [draft.start])
  /** 详情里「重新起草这张表」：只含这张表、用模型、不带补充说明。模型用不了等开始不了的，直接提示原因 */
  const redraftTable = useCallback(async (t: string) => {
    try {
      const r = await draft.start({ tables: [t], use_model: true })
      setDraftOpen({ view: 'job', busy: r.busy })
    } catch (e) {
      toast.error(e)
    }
  }, [draft.start])
  /** 详情里「补充起草要求」（上次起草有留空的列）：打开起草设置，范围只有这张表，先补起草要求再开始 */
  const draftWithGuidance = useCallback((t: string) => {
    setDraftOpen({ view: 'setup', preset: { tables: [t], label: DT.scopeTable(t) } })
  }, [])
  const onGuidanceSaved = useCallback((g: string | null) => {
    setData((cur) => cur && ({ ...cur, guidance: g }))
  }, [])
  // 起草任务现在的样子：跟着的任务在进行就按它；跟着的就是清单说的那一个、已经结束了，就不信清单（清单还没重取回来）
  const listActive = isJobActive(data?.active_job) ? data?.active_job ?? null : null
  const stripJob: { id: string; done: number; total: number; status: string; waiting_for?: string | null } | null = draftJob && (draftActive || draftJob.id === listActive?.id)
    ? (draftActive ? draftJob : null)
    : listActive
  // 调用接连没写出来的提醒：关着对话框时也要看得到（后台任务条上起草那一行带着它）
  const stripNotice = draftActive ? draftJob?.notice?.trim() || null : null

  const options = savedOptions?.sourceId === sourceId ? savedOptions.options : source?.options
  const profileSettings = useMemo(() => profileSettingsOf(options), [options])

  /**
   * 剖析任务（后台进行，状态在页面上）：每张表一结束就重取清单、正在看的表在里面就重新载入（编辑中的不动，保存时服务端
   * 会用版本号拦下）；结束时重取一次清单。对话框关着时结束的，右下角提示并给「查看报告」
   */
  const profile = useProfileJob(sourceId, data?.active_profile_job, {
    onTablesEnded: (tables) => {
      setDrafted((cur) => ({ seq: cur.seq + 1, tables: new Set(tables) }))
      void load()
    },
    onFinished: (job) => {
      void load()
      if (profileOpenRef.current) return
      const src = job.source_id
      const opts = {
        key: `catalog-profile:${src}`, duration: 10000,
        action: { label: PT.viewReport, onClick: () => { if (sourceRef.current === src) setProfileOpen({ view: 'job' }) } },
      }
      if (job.stop_reason) toast.warn(PT.toast.stopped(PT.stopShort[job.stop_reason] ?? ''), opts)
      else if (job.status === 'succeeded') toast.ok(PT.toast.succeeded(PT.findings(job.summary)), opts)
      else if (job.status === 'partial') toast.warn(PT.toast.partial(retryableTasks(job).length), opts)
      else if (job.status === 'failed') toast.error(PT.toast.failed(job.error ?? ''), opts)
      else if (job.status === 'interrupted') toast.warn(PT.toast.interrupted, opts)
      else toast.warn(PT.toast.cancelled, opts)
    },
  })
  const profileJob = profile.job
  const profileActive = isJobActive(profileJob)
  // 页头的剖析进度：同起草，跟着的任务在进行就按它；跟着的就是清单说的那一个、已经结束了，就不信清单
  const listProfile = isJobActive(data?.active_profile_job) ? data?.active_profile_job ?? null : null
  const profileStrip: { id: string; done: number; total: number; status: string; waiting_for?: string | null } | null =
    profileJob && (profileActive || profileJob.id === listProfile?.id) ? (profileActive ? profileJob : null) : listProfile
  /** 跟着的剖析任务里各表的状态（表清单的「剖析」一格据此写排队、剖析中） */
  const profileLive = useMemo(() => new Map<string, CatalogProfileTaskStatus>(
    profileActive && profileJob ? profileJob.tasks.map((t) => [t.table_name, t.status]) : []), [profileActive, profileJob])
  /** 打开剖析对话框：有进行中的任务就直接显示它；上一次的结果看过了，再点就是重新开始 */
  const openProfile = useCallback(() => setProfileOpen({ view: profileActive ? 'job' : 'setup' }), [profileActive])
  const openProfileSelected = useCallback(() => setProfileOpen({ view: profileActive ? 'job' : 'setup', scope: 'selected' }), [profileActive])
  const startProfile = useCallback(async (body: ProfileStartBody) => {
    const next = await profile.start(body)
    // 选中的表已经交给剖析任务了：清空，免得接着点「剖析所选」又剖析一遍
    if (body.tables) setSelected(new Set())
    return next
  }, [profile.start])
  /** 表详情里「剖析这张表」：只含这张表 */
  const profileTable = useCallback((t: string) => profile.start({ tables: [t] }), [profile.start])
  /** 打开剖析对话框显示某个任务：页面跟着的那一个直接打开；别的（表详情里「查看这次剖析」）先取来 */
  const openProfileJob = useCallback(async (jobId: string | null) => {
    if (!jobId || profile.job?.id === jobId) {
      setProfileOpen({ view: 'job' })
      return
    }
    const src = sourceId
    try {
      const { job } = await api.dataCatalog.profileJobs.get(src, jobId)
      if (sourceRef.current !== src) return
      profile.track(job)
      setProfileOpen({ view: 'job' })
    } catch (e) {
      toast.error(e)
    }
  }, [sourceId, profile.job?.id, profile.track])
  /** 打开起草对话框显示某个任务：页面跟着的那一个直接打开；别的（变化里「重新起草」建的）先取来 */
  const openDraftJob = useCallback(async (jobId: string) => {
    if (draft.job?.id === jobId) {
      setDraftOpen({ view: 'job' })
      return
    }
    const src = sourceId
    try {
      const { job } = await api.dataCatalog.draftJob(src, jobId)
      if (sourceRef.current !== src) return
      draft.track(job)
      setDraftOpen({ view: 'job' })
    } catch (e) {
      toast.error(e)
    }
  }, [sourceId, draft.job?.id, draft.track])
  const openJob = useCallback((kind: 'draft' | 'profile', id: string) => {
    if (kind === 'draft') void openDraftJob(id)
    else void openProfileJob(id)
  }, [openDraftJob, openProfileJob])
  // 地址里带着 pjob / djob（定时维护的「最近一次」从数据源页链过来）：打开那个任务的进度，参数用过就去掉
  const linkedJob = params.get('pjob') ? `profile:${params.get('pjob')}` : params.get('djob') ? `draft:${params.get('djob')}` : ''
  const linkedSeen = useRef('')
  useEffect(() => {
    if (!linkedJob || linkedSeen.current === linkedJob || !data) return
    linkedSeen.current = linkedJob
    const [kind, id] = linkedJob.split(':')
    openJob(kind as 'draft' | 'profile', id)
    const next = new URLSearchParams(window.location.search)
    next.delete('pjob')
    next.delete('djob')
    navigate({ search: next.toString() ? `?${next.toString()}` : '' }, { replace: true })
  }, [linkedJob, data, openJob, navigate])

  /**
   * 后台任务条：服务端排好的队列（探查、起草、剖析），起草、剖析换上页面跟着的任务的数和提醒。页面上的任务换了状态就重取一次
   * 队列（排在谁后面跟着变）
   */
  const profileNotice = profileActive ? profileJob?.notice?.trim() || null : null
  const jobsKick = `${stripJob?.id ?? ''}:${stripJob?.status ?? ''}|${profileStrip?.id ?? ''}:${profileStrip?.status ?? ''}`
  const sourceJobs = useSourceJobs(sourceId, jobsKick)
  const jobItems = useMemo(() => stripItems(sourceJobs.items, {
    draft: stripJob ? { kind: 'draft', id: stripJob.id, status: stripJob.status, done: stripJob.done, total: stripJob.total,
      waiting_for: stripJob.waiting_for ?? null, position: null, notice: stripNotice } : null,
    profile: profileStrip ? { kind: 'profile', id: profileStrip.id, status: profileStrip.status, done: profileStrip.done,
      total: profileStrip.total, waiting_for: profileStrip.waiting_for ?? null, position: null, notice: profileNotice } : null,
  }), [sourceJobs.items, stripJob, stripNotice, profileStrip, profileNotice])
  const openStripItem = useCallback((item: StripItem) => {
    if (item.kind === 'introspect') navigate(`${back}#source-${encodeURIComponent(sourceId)}`)
    else openJob(item.kind, item.id)
  }, [navigate, back, sourceId, openJob])

  // 变化视图：标签上的「N 条要处理」、处理过一条之后左栏重取
  const [changeCounts, setChangeCounts] = useState<{ src: string; counts: SourceChangeCounts } | null>(null)
  const [changesSeq, setChangesSeq] = useState(0)
  const loadChangeCounts = useCallback(() => {
    const src = sourceId
    api.sourceChanges.list(src, { limit: 1 }).then((r) => setChangeCounts({ src, counts: r.counts }), () => {})
  }, [sourceId])
  useEffect(() => { loadChangeCounts() }, [loadChangeCounts])
  useOnReconnect(loadChangeCounts)
  const actionCount = changeCounts?.src === sourceId ? changeCounts.counts.by_severity.action : 0
  const onChangeCounts = useCallback((counts: SourceChangeCounts) => setChangeCounts({ src: sourceId, counts }), [sourceId])

  const tableProfile: TableProfileProps = useMemo(() => ({
    job: profileJob, onStart: profileTable, onShowActive: profile.showActive, onSettings: () => setProfileSettingsOpen(true),
    onOpenJob: (id: string | null) => void openProfileJob(id),
  }), [profileJob, profileTable, profile.showActive, openProfileJob])

  const onProfileSettingsSaved = useCallback((row: any) => {
    setProfileSettingsOpen(false)
    setSavedOptions({ sourceId, options: row?.options ?? {} })
    void useCatalog.getState().reload('datasources')
  }, [sourceId])

  const openFromReport = useCallback((t: string) => {
    setProfileOpen(null)
    openTable(t)
  }, [openTable])

  const fillFromReport = useCallback((t: string, column: string) => {
    setProfileOpen(null)
    setFillCodes((cur) => ({ table: t, column, seq: (cur?.seq ?? 0) + 1 }))
    openTable(t)
  }, [openTable])

  const profileJobRef = useRef(profileJob)
  profileJobRef.current = profileJob
  /**
   * 码值弹窗关上了：是从剖析结果打开的，就记下这一列还剩几个含义待填写（结果里跟着改），回到结果。表详情的「数据剖析」
   * 一节里点的「填写含义」不回对话框
   */
  const onFillDone = useCallback((seq: number, pending: number | null) => {
    const fill = fillCodes?.seq === seq ? fillCodes : null
    const cur = profileJobRef.current
    if (!fill || !alive.current || !cur || cur.source_id !== sourceRef.current) return
    if (pending != null) {
      setFilled((f) => ({ jobId: cur.id, map: { ...(f?.jobId === cur.id ? f.map : {}), [fillKey(fill.table, fill.column)]: pending } }))
    }
    setReturnTo({ table: fill.table, column: fill.column })
    setProfileOpen({ view: 'job' })
  }, [fillCodes])

  // 上一张 / 下一张按清单当前的筛选和排序走；正在看的表不在筛选结果里（刚确认完、从关联关系跳过来）时按全部表走
  const [prev, next] = useMemo(() => {
    if (!table) return [null, null]
    const list = visible.some((r) => r.table_name === table) ? visible : visibleRows(rows, '', 'all', sort)
    const i = list.findIndex((r) => r.table_name === table)
    if (i < 0) return [null, null]
    return [list[i - 1]?.table_name ?? null, list[i + 1]?.table_name ?? null]
  }, [table, visible, rows, sort])

  /**
   * 改地址的查询参数，按地址栏现在的样子改：setSearchParams 的函数式写法拿的是上一次渲染时的参数，连着改两样（先点
   * 「全部」、紧接着换表）时后一次会把前一次盖掉
   */
  const patchSearch = useCallback((fn: (p: URLSearchParams) => URLSearchParams, opts?: { replace?: boolean }) => {
    const next = fn(new URLSearchParams(window.location.search)).toString()
    navigate({ search: next ? `?${next}` : '' }, opts)
  }, [navigate])
  /** 换视图（表清单 / 审阅队列 / 变化）：记进地址（后退回到上一个视图），右栏正在看的表不动 */
  const setView = useCallback((v: PageView) => {
    patchSearch((prev) => (v === 'changes'
      ? writeChangesParams(writeReviewParams(prev, { item: '' }), { open: true })
      : clearChanges(writeReviewParams(prev, { view: v, item: '' }))))
  }, [patchSearch])
  /** 变化视图的筛选：替换当前这条历史 */
  const setChangesFilter = useCallback((p: { status?: SourceChangeStatusFilter; category?: SourceChangeCategory | null }) => {
    patchSearch((prev) => writeChangesParams(prev, p), { replace: true })
  }, [patchSearch])
  /** 打开一条变化：右栏是它，地址带着它（表详情里点「去处理」也走这里，切到变化视图） */
  const openChange = useCallback((id: string) => {
    const q = writeChangesParams(clearChanges(carriedRef.current), { open: true, change: id, status: changesParams.status,
      category: changesParams.category }).toString()
    navigate(`${base}${q ? `?${q}` : ''}`)
  }, [navigate, base, changesParams.status, changesParams.category])
  /** 窄屏：从一条变化回到变化列表 */
  const closeChange = useCallback(() => patchSearch((prev) => writeChangesParams(prev, { change: '' })), [patchSearch])
  /** 从一条变化去看它说的那张表：切回表清单 */
  const openChangeTable = useCallback((t: string) => {
    const q = clearChanges(writeReviewParams(carriedRef.current, { view: 'list', item: '' })).toString()
    navigate(`${base}/${encodeURIComponent(t)}${q ? `?${q}` : ''}`)
  }, [navigate, base])
  /** 审阅队列的筛选：替换当前这条历史（筛选不该占满后退键） */
  const setReviewParams = useCallback((p: { kind?: CatalogReviewKind; table?: string }) => {
    patchSearch((prev) => writeReviewParams(prev, p), { replace: true })
  }, [patchSearch])
  /** 从审阅队列打开一项：右栏是所在表的详情，地址带着这一项 */
  const openReviewItem = useCallback((i: CatalogReviewItem) => {
    const q = writeReviewParams(carriedRef.current, { view: 'review', item: i.path }).toString()
    navigate(`${base}/${encodeURIComponent(i.table_name)}${q ? `?${q}` : ''}`)
  }, [navigate, base])
  /** 成批审阅、导入写进了这些表：重取清单，正在看的表重新载入（编辑中的不动，保存时服务端会用版本号拦下） */
  const onTablesWritten = useCallback((tables: string[]) => {
    setDrafted((cur) => ({ seq: cur.seq + 1, tables: new Set(tables) }))
    void load()
  }, [load])
  /** 处理过一条变化：左栏重取、计数跟着变；动了目录的重取清单、重新载入那几张表；建了任务的跟上（清单带着进行中的任务） */
  const onChangeResolved = useCallback((out: SourceChangeResolveOut) => {
    setChangesSeq((n) => n + 1)
    loadChangeCounts()
    const tables = writtenTables(out.change, out.catalog_versions)
    if (tables.length) onTablesWritten(tables)
    else if (out.job) void load()
    if (out.job) sourceJobs.refresh()
  }, [loadChangeCounts, onTablesWritten, load, sourceJobs.refresh])
  /** 右栏有内容（打开了一张表、一条变化）：窄屏一次只显示一栏，这时显示右栏 */
  const rightOpen = !!table || !!openChangeId
  const activeReviewKey = inReview && table && review.item ? itemKey({ table_name: table, path: review.item }) : null
  const proposedTotal = useMemo(() => rows.reduce((n, r) => n + (r.in_schema ? r.counts.proposed : 0), 0), [rows])

  /** 导出目录：下载服务端给的文件，文件名按服务端的，没给就按数据源名补 */
  const [exporting, setExporting] = useState(false)
  const [importing, setImporting] = useState(false)
  const exportCatalog = useCallback(async () => {
    setExporting(true)
    try {
      const { blob, name } = await api.dataCatalog.exportFile(sourceId)
      const file = name ?? XT.fileName(source?.name ?? sourceId)
      saveFile(file, blob)
      toast.ok(XT.exported(file))
    } catch (e) {
      toast.error(XT.exportFailed(errorMessage(e)))
    } finally {
      setExporting(false)
    }
  }, [sourceId, source?.name])

  const usage = useMemo(() => usageSummary(rows), [rows])
  const wideHeader = useMedia('(min-width: 640px)')
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

  const filtered = !!query.trim() || filter !== 'all' || usedOnly || dueOnly
  const empty = rows.length > 0 && rows.every((r) => progressOf(r.counts) === 'none')
  const hasTables = rows.length > 0
  const draftButton = (primary = false) => (
    <button type="button" className={clsx('btn btn-sm', primary && 'btn-primary')} onClick={openDraft} data-catalog-draft="">
      <Sparkles size={11} aria-hidden /> {CT.draft}
    </button>
  )
  // 页头的「数据剖析」「起草」只是入口（进度在下方的后台任务条上，05 (g)）：进行中换转圈图标、排队中换时钟，读屏名称写状态、
  // 不写数字；点开对应的对话框（关掉对话框任务照常进行）。窄屏只留图标，标题才放得下
  const headState = (job: { status: string } | null) => (job?.status === 'queued' ? 'queued' : job?.status === 'running' ? 'running' : null)
  const headLabel = (label: string, state: 'queued' | 'running' | null) =>
    (state === 'running' ? JT.headRunning(label) : state === 'queued' ? JT.headQueued(label) : label)
  const headIcon = (state: 'queued' | 'running' | null, idle: ReactNode) =>
    (state === 'running' ? <Spinner size={11} /> : state === 'queued' ? <Clock size={11} aria-hidden /> : idle)
  const profileState = headState(profileStrip)
  const profileLabel = headLabel(PT.action, profileState)
  const headerProfile = (
    <button type="button" className="btn btn-sm" onClick={openProfile} aria-label={profileLabel} title={profileState ? profileLabel : PT.actionHint}
            data-catalog-profile={profileStrip?.status ?? ''}>
      {headIcon(profileState, <ScanSearch size={11} aria-hidden />)}
      <span className="hidden sm:inline">{PT.action}</span>
    </button>
  )
  const draftState = headState(stripJob)
  const draftLabel = headLabel(CT.draft, draftState)
  const headerDraft = (
    <button type="button" className="btn btn-sm" onClick={openDraft} aria-label={draftLabel} title={draftLabel} data-catalog-draft=""
            data-catalog-draft-head={stripJob?.status ?? ''}>
      {headIcon(draftState, <Sparkles size={11} aria-hidden />)} <span className="hidden sm:inline">{CT.draft}</span>
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
        <aside className={clsx('min-h-0 w-full flex-col border-r lg:flex lg:w-[380px] lg:shrink-0 xl:w-[420px]', rightOpen ? 'hidden' : 'flex')}>
          <ViewTabs view={pageView} proposed={proposedTotal} changes={actionCount} onChange={setView} />
          <div role="tabpanel" id="catalog-view-panel" aria-labelledby={`catalog-view-tab-${pageView}`} className="flex min-h-0 flex-1 flex-col">
            {inChanges
              ? (
                <ChangesQueue sourceId={sourceId} params={changesParams} activeId={openChangeId} refreshKey={changesSeq}
                              onParams={setChangesFilter} onOpen={(c) => openChange(c.id)} onCounts={onChangeCounts} />
              )
              : inReview
              ? (
                <ReviewQueue sourceId={sourceId} rows={rows} kind={review.kind} table={review.table} activeKey={activeReviewKey}
                             detailSync={detailSync} onParams={setReviewParams} onOpenItem={openReviewItem} onChanged={onTablesWritten} />
              )
              : (
                <>
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
                    onDraft={openDraft}
                    onProfile={openProfileSelected}
                    profileOn={profileSettings.enabled}
                    profileLive={profileLive}
                    dueOnly={dueOnly}
                    onDueOnly={dueKnown || dueOnly ? setDueOnly : undefined}
                    dueTotal={dueTotal}
                    sourceId={sourceId}
                    outside={data?.scope?.out_of_scope ?? 0}
                    onAdded={() => { void load(); void useCatalog.getState().reload('datasources') }}
                  />
                </>
              )}
          </div>
        </aside>
        <section className={clsx('min-h-0 min-w-0 flex-1 flex-col lg:flex', rightOpen ? 'flex' : 'hidden')} aria-label={table ?? CT.overviewTitle}>
          {openChangeId
            ? (
              <ChangeDetail sourceId={sourceId} changeId={openChangeId} onBack={closeChange} onResolved={onChangeResolved}
                            onOpenTable={openChangeTable} onOpenJob={openJob} />
            )
            : table
            ? (
              <TableDetail sourceId={sourceId} table={table} rows={rows} drafted={drafted} fillCodes={fillCodes} prev={prev} next={next}
                           onFillDone={onFillDone} onOpen={openTable} onBack={backToList} onDetail={onDetail}
                           draftBusy={!!stripJob} onRedraft={redraftTable} onDraftGuidance={draftWithGuidance}
                           profileEnabled={profileSettings.enabled} focusPath={inReview ? review.item || null : null} usageOpen={inReview}
                           profile={tableProfile} onOpenChange={openChange} />
            )
            : inChanges
              ? <ChangesOverview />
            : inReview
              ? <ReviewOverview />
              : <Overview rows={rows} empty={empty} systemNotes={data.system_notes} draft={draftButton(true)} onOpen={openTable}
                          onReview={() => setView('review')} />}
        </section>
      </div>
    )
  }

  return (
    <div className="flex h-full flex-col" data-catalog-page={sourceId}>
      <PageHeader
        icon={<BookMarked size={13} />}
        title={CT.title(source?.name ?? sourceId)}
        // 窄屏放不下说明：硬塞进去会被挤成一个字的宽度，标题后面露出说明的头一个字（「先」）。窄屏只留标题
        subtitle={wideHeader ? CT.subtitle : undefined}
        actions={(
          <div className="flex shrink-0 items-center gap-1.5">
            {hasTables && headerProfile}
            {hasTables && headerDraft}
            {hasTables && (
              <button type="button" className="btn btn-sm" onClick={() => void exportCatalog()} disabled={exporting} aria-label={XT.export}
                      title={XT.exportHint} data-catalog-export="">
                {exporting ? <Spinner size={11} /> : <Download size={11} aria-hidden />} <span className="hidden md:inline">{XT.export}</span>
              </button>
            )}
            {hasTables && (
              <button type="button" className="btn btn-sm" onClick={() => setImporting(true)} aria-label={XT.import} title={XT.importHint}
                      data-catalog-import="">
                <Upload size={11} aria-hidden /> <span className="hidden md:inline">{XT.import}</span>
              </button>
            )}
            <button type="button" className="btn btn-sm btn-ghost" onClick={() => navigate(back)} aria-label={CT.back} data-catalog-back="">
              <ArrowLeft size={12} aria-hidden /> <span className="hidden sm:inline">{CT.back}</span>
            </button>
          </div>
        )}
      />
      {hasTables && data?.schema_truncated && (
        <SchemaPartialNotice total={data.schema_total ?? 0} explored={rows.filter((r) => r.in_schema).length}
                             scoped={!!data.scope} onGoSource={() => navigate(back)} />
      )}
      {hasTables && usage.used > 0 && !empty && <UsageSummary {...usage} active={usedOnly ? filter : null} onFocus={focusUsed} />}
      {hasTables && <JobStrip items={jobItems} onOpen={openStripItem} />}
      {body}
      {draftOpen && data && (
        <DraftDialog
          sourceId={sourceId}
          sourceDescription={source?.description ?? ''}
          guidance={data.guidance ?? ''}
          rows={rows}
          visible={visible}
          selected={[...selected]}
          filtered={filtered}
          job={draftJob}
          initial={draftOpen.view}
          initialBusy={!!draftOpen.busy}
          preset={draftOpen.preset ?? null}
          stopping={draft.stopping}
          profileActive={!!profileStrip}
          onStart={startDraft}
          onCancel={draft.cancel}
          onRetry={draft.retry}
          onTrack={draft.track}
          onGuidanceSaved={onGuidanceSaved}
          onClose={() => setDraftOpen(null)}
        />
      )}
      {profileOpen && data && (
        <ProfileDialog
          sourceId={sourceId}
          sourceName={source?.name ?? sourceId}
          settings={profileSettings}
          rows={rows}
          visible={visible}
          selected={[...selected]}
          filtered={filtered}
          job={profileJob}
          initial={profileOpen.view}
          initialScope={profileOpen.scope ?? null}
          draftActive={!!stripJob}
          stopping={profile.stopping}
          filled={filled && filled.jobId === profileJob?.id ? filled.map : {}}
          returnTo={returnTo}
          onStart={startProfile}
          onCancel={profile.cancel}
          onRetry={profile.retry}
          onShowActive={profile.showActive}
          onTrack={profile.track}
          onClose={() => { setProfileOpen(null); setReturnTo(null) }}
          onSettings={() => setProfileSettingsOpen(true)}
          onGoSource={() => navigate(back)}
          onOpenTable={openFromReport}
          onFillCodes={fillFromReport}
        />
      )}
      {importing && (
        <ImportDialog sourceId={sourceId} onClose={() => setImporting(false)} onImported={onTablesWritten} />
      )}
      {profileSettingsOpen && source && (
        <ProfileSettingsDialog row={{ id: source.id, name: source.name, options }} onClose={() => setProfileSettingsOpen(false)}
                               onSaved={onProfileSettingsSaved} />
      )}
    </div>
  )
}

/** 存成文件（同运行记录里的下载）：浏览器下载，文件名照给的 */
function saveFile(name: string, blob: Blob) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = name
  a.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

/** 数据目录页左栏的三个视图 */
type PageView = 'list' | 'review' | 'changes'
const VIEWS: PageView[] = ['list', 'review', 'changes']

/**
 * 左栏上方的三个视图：表清单（逐表看）、审阅队列（跨表按优先级看推断项，带着还有几项待确认）、变化（探查、取值观察测到的
 * 结构变化和取值变化，带着几条要处理）。标签页语义，←→ Home End 切换
 */
function ViewTabs({ view, proposed, changes, onChange }: {
  view: PageView; proposed: number; changes: number; onChange: (v: PageView) => void
}) {
  const refs = useRef<Record<string, HTMLButtonElement | null>>({})
  const move = (e: ReactKeyboardEvent, i: number) => {
    const j = rovingTarget(e, i, VIEWS.length, 'horizontal')
    if (j < 0) return
    e.preventDefault()
    onChange(VIEWS[j])
    refs.current[VIEWS[j]]?.focus()
  }
  return (
    <div className="flex shrink-0 gap-1 border-b px-2" role="tablist" aria-label={RT.viewLabel} data-catalog-view={view}>
      {VIEWS.map((v, i) => {
        const selected = view === v
        return (
          <button key={v} ref={(el) => { refs.current[v] = el }} type="button" role="tab" id={`catalog-view-tab-${v}`} aria-selected={selected}
                  aria-controls="catalog-view-panel" tabIndex={selected ? 0 : -1} title={v === 'changes' ? GT.viewHint : RT.viewHint[v]}
                  onClick={() => onChange(v)} onKeyDown={(e) => move(e, i)} data-catalog-view-tab={v}
                  className={clsx('relative px-3 py-2 text-xs outline-none transition-colors focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--accent)]',
                    selected ? 'text-fg' : 'text-faint hover:text-dim')}>
            {v === 'changes' ? GT.view : RT.view[v]}
            {v === 'review' && proposed > 0 && <span className="tnum ml-1.5 text-2xs text-faint">{formatNumber(proposed)}</span>}
            {v === 'changes' && changes > 0 && (
              <span className="tnum ml-1.5 text-2xs text-[var(--err)]" data-catalog-changes-count={changes}>
                <span aria-hidden>{formatNumber(changes)}</span><span className="sr-only">{GT.badge(changes)}</span>
              </span>
            )}
            {selected && <span className="absolute inset-x-2 -bottom-px h-0.5 rounded bg-[var(--accent)]" aria-hidden />}
          </button>
        )
      })}
    </div>
  )
}

/** 媒体查询的当前结果，跟着窗口大小变 */
function useMedia(query: string): boolean {
  const [on, setOn] = useState(() => typeof matchMedia !== 'function' || matchMedia(query).matches)
  useEffect(() => {
    if (typeof matchMedia !== 'function') return
    const mq = matchMedia(query)
    const sync = () => setOn(mq.matches)
    sync()
    mq.addEventListener('change', sync)
    return () => mq.removeEventListener('change', sync)
  }, [query])
  return on
}

/**
 * 有探查范围外的表（next-round 03 A1）：清单只列范围内的，顶部常驻一条说明，写清一共几张、范围内几张、范围外的去哪找
 * （搜索框搜到后「加入范围」，或回数据源卡片调整探查范围）。升级前只探查了一部分的老缓存（没有对象索引，scoped 为假）
 * 照旧说「只探查了前 N 张」、请重新探查
 */
function SchemaPartialNotice({ total, explored, scoped, onGoSource }: {
  total: number; explored: number; scoped: boolean; onGoSource: () => void
}) {
  const shownTotal = Math.max(total, explored)
  return (
    <div className="flex shrink-0 items-start gap-2 border-b px-4 py-2 text-xs"
         style={{ background: 'color-mix(in srgb, var(--st-waiting) 6%, var(--bg-panel))' }}
         data-catalog-schema-partial={`${shownTotal},${explored}`}>
      <Database size={12} className="mt-0.5 shrink-0" style={{ color: 'var(--st-waiting)' }} aria-hidden />
      <div className="min-w-0 flex-1 leading-relaxed">
        <p className="font-medium">{(scoped ? SPT.title : SPT.legacyTitle)(shownTotal, explored)}</p>
        <p className="text-2xs text-dim">{(scoped ? SPT.body : SPT.legacyBody)(Math.max(shownTotal - explored, 0))}</p>
        <p className="text-2xs text-dim">{scoped ? SPT.next : SPT.legacyNext}</p>
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

/** 没有选中表时的右栏：空目录时引导起草；否则写审阅进度，给出下一张该审的表，也可以去审阅队列跨表成批审阅 */
function Overview({ rows, empty, systemNotes, draft, onOpen, onReview }: {
  rows: CatalogTableRow[]
  empty: boolean
  systemNotes: boolean
  draft: ReactNode
  onOpen: (table: string) => void
  onReview: () => void
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
              {pendingNext && (
                <div className="flex flex-wrap items-center gap-2">
                  <button type="button" className="btn btn-sm" onClick={onReview} data-catalog-open-review="">{RT.openQueue}</button>
                  <span className="text-2xs text-faint">{RT.openQueueHint}</span>
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
