import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, api } from '../../api/client'
import type { CatalogActiveProfileJob, CatalogProfileJob, CatalogProfileTaskRow, CatalogProfileTaskStatus } from '../../types'
import { PROFILE_BUSY, PROFILE_FINISHED, PROFILE_RETRYABLE, isProfileTaskEnded } from './profile'
import { isJobActive } from './draftJob'

// ===========================================================================
// 剖析任务（/api/datasources/{id}/catalog/profile-jobs）在页面上的状态：建、停、重试，进行中每 1.5 秒取一次进度，结束就停。
// 照起草任务（draftJob.ts 的 useDraftJob）的做法：放在页面上而不是对话框里，关掉对话框任务照常进行，页头的进度入口、
// 结束时的提示都靠它；再打开对话框看到的还是同一个任务。
//
// 取回来的状态只往前走：已经结束的任务不会被一个晚到的「进行中」翻回去，已结束的表数不减少（停止接口的回答和正在路上的
// 那次取进度谁先到都一样）。
//
// 建任务、重试撞上 409 时照原样抛给调用方：剖析的几种 409（未开启、停用、没有表结构、快照被改动、正忙、挑不出表）各有
// 说法和下一步，由对话框、表详情去说；正忙时可以用 showActive 改去跟着进行中的那一个。
// ===========================================================================

/** 进行中多久取一次进度（同起草） */
export const PROFILE_POLL_MS = 1500

export const retryableTasks = (j: CatalogProfileJob): CatalogProfileTaskRow[] => j.tasks.filter((t) => PROFILE_RETRYABLE.has(t.status))

export interface ProfileStartBody { tables?: string[]; scope?: 'needed' | 'all' }

export function useProfileJob(sourceId: string, active: CatalogActiveProfileJob | null | undefined, on: {
  /** 这一次取回来新结束的表（写进目录了，或者失败、停止了）：重取清单，正在看的表在里面就重新载入 */
  onTablesEnded: (tables: string[]) => void
  /** 看着它从排队、进行中变成结束（这一页上亲眼看到的，进页面时已经结束的不算） */
  onFinished: (job: CatalogProfileJob) => void
}) {
  // 任务挂在数据源上：换了数据源，上一个源的任务不再显示（那边照常进行）
  const [held, setHeld] = useState<{ src: string; job: CatalogProfileJob } | null>(null)
  const job = held?.src === sourceId ? held.job : null
  const jobRef = useRef(job)
  jobRef.current = job
  const sourceRef = useRef(sourceId)
  sourceRef.current = sourceId
  const onRef = useRef(on)
  onRef.current = on
  /** 点了停止、服务端还没停下来的任务 */
  const [stopping, setStopping] = useState<string | null>(null)
  /** 取进度失败（网络抖了一下）、或取回来的是旧状态时，原样再排一拍 */
  const [tick, setTick] = useState(0)

  /** 换上取回来的状态。不往回走；返回是否换上了 */
  const accept = useCallback((next: CatalogProfileJob, src: string): boolean => {
    if (src !== sourceRef.current) return false
    const cur = jobRef.current
    const same = cur?.id === next.id
    if (same && cur) {
      if (!isJobActive(cur) && isJobActive(next)) return false
      if (isJobActive(next) && next.done < cur.done) return false
    }
    // 新结束的表：同一个任务比上一次多结束的；头一回见到的进行中任务（别处建的、刷新页面前建的）已经结束的那些也算，
    // 清单可能还没跟上。头一回见到的已结束任务（点开上一次的结果）不算，那些早就写进去了。跳过的表没写目录，不算
    const before = new Map<string, CatalogProfileTaskStatus>((same && cur ? cur.tasks : []).map((t) => [t.table_name, t.status]))
    const ended = same || isJobActive(next)
      ? next.tasks
        .filter((t) => isProfileTaskEnded(t) && t.status !== 'skipped' && !isProfileTaskEnded({ status: before.get(t.table_name) ?? 'pending' }))
        .map((t) => t.table_name)
      : []
    jobRef.current = next
    setHeld({ src, job: next })
    if (ended.length) onRef.current.onTablesEnded(ended)
    if (same && cur && isJobActive(cur) && !isJobActive(next)) onRef.current.onFinished(next)
    return true
  }, [])

  /** 换成另一个任务（刚建的、重试建的、正忙时进行中的那个、对话框和表详情里点开的上一次）。同一个任务照常只往前走 */
  const track = useCallback((next: CatalogProfileJob) => {
    accept(next, sourceRef.current)
  }, [accept])

  // 进行中：隔 1.5 秒取一次，结束就停
  const jobId = job?.id
  const jobActive = isJobActive(job)
  useEffect(() => {
    if (!jobId || !jobActive) return
    const src = sourceId
    let alive = true
    const t = setTimeout(async () => {
      try {
        const { job: next } = await api.dataCatalog.profileJobs.get(src, jobId)
        if (alive && !accept(next, src)) setTick((n) => n + 1)
      } catch (e) {
        if (!alive) return
        // 任务没了（不属于这个数据源、库被换掉了）：不再跟着它；别的失败（网络抖动、服务重启中）下一拍再取
        if (e instanceof ApiError && e.status === 404) setHeld((h) => (h?.job.id === jobId ? null : h))
        else setTick((n) => n + 1)
      }
    }, PROFILE_POLL_MS)
    return () => {
      alive = false
      clearTimeout(t)
    }
  }, [sourceId, jobId, jobActive, job, tick, accept])

  // 清单说有进行中的任务、这一页还不知道（别的标签页建的、刷新了页面、表详情里建的）：取来跟着它
  const activeId = isJobActive(active) ? active?.id : undefined
  useEffect(() => {
    if (!activeId) return
    const cur = jobRef.current
    if (cur?.id === activeId) return
    const src = sourceId
    let alive = true
    api.dataCatalog.profileJobs.get(src, activeId).then(({ job: next }) => { if (alive) accept(next, src) }, () => {})
    return () => { alive = false }
  }, [sourceId, activeId, accept])

  /** 建任务。409、422 等照原样抛给调用方 */
  const start = useCallback(async (body: ProfileStartBody): Promise<CatalogProfileJob> => {
    const src = sourceRef.current
    const { job: next } = await api.dataCatalog.profileJobs.create(src, body)
    accept(next, src)
    return next
  }, [accept])

  /** 重试没做完的表：新任务只含失败、部分完成、已停止的表。409（正忙）、422（没有可重试的）照原样抛 */
  const retry = useCallback(async (from: CatalogProfileJob): Promise<CatalogProfileJob> => {
    const src = sourceRef.current
    const { job: next } = await api.dataCatalog.profileJobs.retry(src, from.id)
    accept(next, src)
    return next
  }, [accept])

  /** 正忙（409 profile_busy）时改去跟着进行中的那一个；没有了返回 null */
  const showActive = useCallback(async (): Promise<CatalogProfileJob | null> => {
    const src = sourceRef.current
    const { job: current } = await api.dataCatalog.profileJobs.active(src)
    if (current) accept(current, src)
    return current
  }, [accept])

  /**
   * 停止。停止接口等服务端收尾写完才答（不等正在执行的那条查询，它由数据库按单条时限截止），这期间任务还是进行中：
   * stopping 为这个任务。
   * 已经结束了（409）就取一次最新的
   */
  const cancel = useCallback(async () => {
    const cur = jobRef.current
    if (!cur || !isJobActive(cur)) return
    const src = sourceRef.current
    setStopping(cur.id)
    try {
      const { job: next } = await api.dataCatalog.profileJobs.cancel(src, cur.id)
      accept(next, src)
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && e.code === PROFILE_FINISHED) {
        const { job: next } = await api.dataCatalog.profileJobs.get(src, cur.id)
        accept(next, src)
        return
      }
      setStopping(null)
      throw e
    }
  }, [accept])

  return { job, track, start, retry, showActive, cancel, stopping: !!job && stopping === job.id && isJobActive(job) }
}

/** 正忙（409）的错误：这个源已有排队、进行中的剖析任务 */
export const isProfileBusy = (e: unknown): boolean => e instanceof ApiError && e.status === 409 && e.code === PROFILE_BUSY
