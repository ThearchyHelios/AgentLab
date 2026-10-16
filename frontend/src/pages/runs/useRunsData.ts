import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../../api/client'
import { toast } from '../../components/ui'
import type { StatusCode } from '../../lib/status'
import type { Approval, Run } from '../../types'
import type { Task, UnlistedRuns } from '../../typesApp'
import { useCatalog } from '../../store/catalog'
import { runCode, serverStatuses } from './model'

/** 一页多少条。翻页走 before 游标，不再一次拉 100 条然后在里面找 */
export const PAGE_SIZE = 50

export interface ListFilters {
  /** 显示码。空 = 不限状态 */
  codes: StatusCode[]
  runClass: '' | 'formal' | 'exploratory'
  workflowId: string
  q: string
  /** 运行来源（X3）。none = 来源未记录的老运行；空 = 不限 */
  origin?: string
}

export interface RunList {
  rows: Run[]
  state: 'loading' | 'ok' | 'error'
  error: unknown
  hasMore: boolean
  loadingMore: boolean
  /** 重新拉第一页。silent：不闪加载态，已经翻出来的页数一起刷新（轮询用） */
  reload: (opts?: { silent?: boolean }) => Promise<void>
  loadMore: () => Promise<void>
  /** 详情页那条运行变了：原地换掉那一行，不必整表重拉 */
  patchRow: (run: Run) => void
  removeRow: (id: string) => void
}

const paramsOf = (f: ListFilters) => ({
  status: f.codes.length ? serverStatuses(f.codes) : undefined,
  run_class: f.runClass || undefined,
  workflow_id: f.workflowId || undefined,
  q: f.q.trim() || undefined,
  origin_kind: f.origin || undefined,
})

/**
 * 运行列表。筛选全走服务端：以前只取最近 100 条再在里面过滤，待审批的运行排在
 * 第 145 位，点了徽标也找不到，筛选还会说「还没有运行记录」。
 */
export function useRunList(filters: ListFilters, enabled: boolean): RunList {
  const [rows, setRows] = useState<Run[]>([])
  const [state, setState] = useState<RunList['state']>('loading')
  const [error, setError] = useState<unknown>(null)
  const [hasMore, setHasMore] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const key = JSON.stringify(paramsOf(filters))
  // 慢请求晚回来不能盖掉新筛选的结果
  const epoch = useRef(0)
  const rowsRef = useRef<Run[]>([])
  rowsRef.current = rows

  const reload = useCallback(async (opts?: { silent?: boolean }) => {
    const my = ++epoch.current
    const params = JSON.parse(key)
    if (!opts?.silent) setState('loading')
    // 轮询时把已经翻出来的几页一起刷新，列表不会因为刷新缩回第一页
    const limit = opts?.silent ? Math.min(200, Math.max(PAGE_SIZE, rowsRef.current.length)) : PAGE_SIZE
    try {
      const page = await api.runs.list({ ...params, limit })
      if (my !== epoch.current) return
      setRows(page)
      setHasMore(page.length === limit)
      setError(null)
      setState('ok')
    } catch (e) {
      if (my !== epoch.current) return
      if (opts?.silent) return
      setError(e)
      setState('error')
    }
  }, [key])

  useEffect(() => {
    if (enabled) void reload()
  }, [reload, enabled])

  const loadMore = useCallback(async () => {
    const last = rowsRef.current[rowsRef.current.length - 1]
    if (!last?.created_at) return
    const my = epoch.current
    setLoadingMore(true)
    try {
      const page = await api.runs.list({ ...JSON.parse(key), limit: PAGE_SIZE, before: last.created_at })
      if (my !== epoch.current) return
      setRows((prev) => {
        const seen = new Set(prev.map((r) => r.id))
        return [...prev, ...page.filter((r) => !seen.has(r.id))]
      })
      setHasMore(page.length === PAGE_SIZE)
    } catch (e) {
      toast.error(e)
    } finally {
      setLoadingMore(false)
    }
  }, [key])

  const patchRow = useCallback((run: Run) => {
    setRows((prev) => {
      const i = prev.findIndex((r) => r.id === run.id)
      if (i < 0) return prev
      const cur = prev[i]
      if (cur.status === run.status && cur.finished_at === run.finished_at
          && JSON.stringify(cur.usage) === JSON.stringify(run.usage) && cur.error === run.error) return prev
      const next = prev.slice()
      next[i] = { ...cur, ...run }
      return next
    })
  }, [])

  const removeRow = useCallback((id: string) => {
    setRows((prev) => prev.filter((r) => r.id !== id))
  }, [])

  return { rows, state, error, hasMore, loadingMore, reload, loadMore, patchRow, removeRow }
}

// -------------------------------------------------------------------------
// 没列出的系统运行
// -------------------------------------------------------------------------

/**
 * 同样的筛选下，列表没列出的系统派生运行（评测、口径对账、口径召回）有几次。点名了来源的什么都不藏，不用问；老后端
 * 没有这个接口时当作 0（不报错：列表本身照常）
 */
export function useUnlistedRuns(filters: ListFilters, epoch: number, enabled: boolean): UnlistedRuns | null {
  const [got, setGot] = useState<UnlistedRuns | null>(null)
  const params = paramsOf(filters)
  const key = JSON.stringify(params)
  const ask = enabled && !params.origin_kind

  useEffect(() => {
    if (!ask) { setGot(null); return }
    let live = true
    api.runs.unlisted(JSON.parse(key))
      .then((r) => { if (live) setGot(r) }, () => { if (live) setGot(null) })
    return () => { live = false }
  }, [key, epoch, ask])

  return ask ? got : null
}

// -------------------------------------------------------------------------
// 计数
// -------------------------------------------------------------------------

/**
 * 数的是"需要看一眼"的几类；已完成不数——它是常态，数字只会分散注意力。被驳回以前记成失败、算在「失败」里，现在单独
 * 一类，不数就哪个计数里都没有它
 */
const COUNTED: StatusCode[] = ['running', 'queued', 'waiting', 'held', 'failed', 'cancelled', 'rejected']
const COUNT_LIMIT = 200

export interface StatusCounts {
  counts: Partial<Record<StatusCode, number>>
  /** 撞到上限：数字是下限，显示成「200+」 */
  saturated: boolean
  loaded: boolean
}

/**
 * 分段和页签上的计数。一次请求取回所有非「已完成」的运行（同样的分级、工作流、
 * 名称条件），在前端按显示码数——interrupted 要靠审批列表再分成等待审批和
 * 已挂起，服务端数不了。
 */
export function useStatusCounts(
  filters: Omit<ListFilters, 'codes'>, approvals: Approval[] | null, epoch: number, enabled = true,
): StatusCounts {
  const [runs, setRuns] = useState<Run[] | null>(null)
  const key = JSON.stringify(paramsOf({ ...filters, codes: [] }))

  useEffect(() => {
    if (!enabled) return
    let live = true
    api.runs.list({ ...JSON.parse(key), status: serverStatuses(COUNTED), limit: COUNT_LIMIT })
      .then((list) => { if (live) setRuns(list) }, () => { if (live) setRuns(null) })
    return () => { live = false }
  }, [key, epoch, enabled])

  if (!runs || !enabled) return { counts: {}, saturated: false, loaded: false }
  const counts: Partial<Record<StatusCode, number>> = {}
  for (const r of runs) {
    const code = runCode(r, approvals)
    counts[code] = (counts[code] ?? 0) + 1
  }
  for (const c of COUNTED) counts[c] ??= 0
  return { counts, saturated: runs.length >= COUNT_LIMIT, loaded: true }
}

// -------------------------------------------------------------------------
// 待审批
// -------------------------------------------------------------------------

export interface ApprovalQueue {
  items: Approval[]
  state: 'loading' | 'ok' | 'error'
  error: unknown
  reload: () => Promise<void>
}

/**
 * 全局待审批。catalog 里也有一份（导航徽标用，4 秒一轮询），这里单独取一份
 * 上限更高的，并在 catalog 那份变化时跟着刷新——两边说的数一致，徽标变了列表
 * 马上跟上。
 */
export function useApprovalQueue(epoch: number): ApprovalQueue {
  const [items, setItems] = useState<Approval[]>([])
  const [state, setState] = useState<ApprovalQueue['state']>('loading')
  const [error, setError] = useState<unknown>(null)
  const signature = useCatalog((s) => s.approvals.map((a) => a.id).join(','))
  const first = useRef(true)

  const reload = useCallback(async () => {
    if (first.current) setState('loading')
    try {
      const list = await api.approvals.list({ status: 'pending', limit: 500 })
      setItems(list.filter((a) => a.status === 'pending'))
      setError(null)
      setState('ok')
      first.current = false
    } catch (e) {
      setError(e)
      setState('error')
    }
  }, [])

  useEffect(() => { void reload() }, [reload, signature, epoch])

  return { items, state, error, reload }
}

// -------------------------------------------------------------------------
// 待办
// -------------------------------------------------------------------------

export interface TaskQueue {
  items: Task[]
  state: 'loading' | 'ok' | 'error'
  error: unknown
  reload: () => Promise<void>
}

/**
 * 记录页的「待办」页签：不属于应用的运行里待处理的待办（画布、问数据、定时任务、正式运行）。应用里的待办在
 * 「应用 → 待办」里处理，导航徽标也按来源分开数，这里不重复列。核实结果未知的动作没有审批，靠手动刷新和断网恢复。
 *
 * 待审批变了（catalog 那份 4 秒一轮询）就跟着变：一次中断对应一条审批和一条待办。审批上带着 task_id（D-r）时不再整表
 * 重取——处理掉的审批，它的待办直接拿掉；新来的审批按 task_id 只取那几条待办。审批不带 task_id（老服务端）、或者
 * 审批列表撞到了上限（看不全哪些还在），退回整表重取
 */
const APPROVALS_LIMIT = 100

export function useTaskQueue(epoch: number): TaskQueue {
  const [items, setItems] = useState<Task[]>([])
  const [state, setState] = useState<TaskQueue['state']>('loading')
  const [error, setError] = useState<unknown>(null)
  const approvals = useCatalog((s) => s.approvals)
  const signature = approvals.map((a) => a.id).join(',')
  const first = useRef(true)
  const itemsRef = useRef(items)
  itemsRef.current = items

  const keep = (t: Task) => !t.app_id && (t.status === 'pending' || t.status === 'resolving')

  const reload = useCallback(async () => {
    if (first.current) setState('loading')
    try {
      const { items: list } = await api.tasks.list({ assigned: 'all', status: 'pending', limit: 500 })
      setItems(list.filter(keep))
      setError(null)
      setState('ok')
      first.current = false
    } catch (e) {
      setError(e)
      setState('error')
    }
  }, [])

  // 手动刷新、断网恢复（epoch 变了）：整表重取
  useEffect(() => { void reload() }, [reload, epoch])

  // 待审批变了：能按 task_id 对上的只补差，对不上的整表重取
  const seenSig = useRef<string | null>(null)
  useEffect(() => {
    if (seenSig.current === null) { seenSig.current = signature; return }
    if (seenSig.current === signature) return
    seenSig.current = signature
    const pending = approvals.filter((a) => a.status === 'pending')
    if (first.current || pending.some((a) => !a.task_id) || approvals.length >= APPROVALS_LIMIT) {
      void reload()
      return
    }
    const live = new Set(pending.map((a) => a.id))
    const have = new Set(itemsRef.current.map((t) => t.id))
    const missing = [...new Set(pending.map((a) => a.task_id as string))].filter((id) => !have.has(id))
    // 审批处理掉了：它的待办一起拿掉（没有审批的核实、等外部系统的待办留着）
    setItems((cur) => cur.filter((t) => !t.approval_id || live.has(t.approval_id)))
    if (!missing.length) return
    void Promise.all(missing.map((id) => api.tasks.get(id).catch(() => null))).then((got) => {
      const add = got.filter((t): t is Task => !!t && keep(t))
      if (got.some((t) => t == null)) { void reload(); return }
      if (add.length) setItems((cur) => [...cur.filter((t) => !add.some((x) => x.id === t.id)), ...add])
    })
  }, [signature]) // eslint-disable-line react-hooks/exhaustive-deps

  return { items, state, error, reload }
}
