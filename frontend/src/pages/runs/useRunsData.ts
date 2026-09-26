import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../../api/client'
import { toast } from '../../components/ui'
import type { StatusCode } from '../../lib/status'
import type { Approval, Run } from '../../types'
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
// 计数
// -------------------------------------------------------------------------

/** 数的是"需要看一眼"的几类；已完成不数——它是常态，数字只会分散注意力 */
const COUNTED: StatusCode[] = ['running', 'queued', 'waiting', 'held', 'failed', 'cancelled']
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
