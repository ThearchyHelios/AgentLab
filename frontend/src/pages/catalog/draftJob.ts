import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, api } from '../../api/client'
import type { CatalogActiveDraftJob, CatalogDraftJob, CatalogDraftTask, CatalogDraftTaskStatus } from '../../types'

// ===========================================================================
// 起草任务（/api/datasources/{id}/catalog/draft-jobs）在页面上的状态：建、停、重试，进行中每 1.5 秒取一次进度，
// 结束就停。放在页面上而不是对话框里：关掉对话框任务照常进行，目录页上方的进度、结束时的提示都靠它；再打开对话框
// 看到的还是同一个任务。
//
// 取回来的状态只往前走：已经结束的任务不会被一个晚到的「进行中」翻回去，已结束的表数不减少（停止接口的回答和
// 正在路上的那次取进度谁先到都一样）。
// ===========================================================================

/** 进行中多久取一次进度 */
export const DRAFT_POLL_MS = 1500
/** 一次最多起草几张表（服务端同一个上限） */
export const DRAFT_MAX_TABLES = 500
/** 不选表时起草使用次数最多的前几张（服务端不给 tables 时也是这个数） */
export const DRAFT_TOP_N = 20

/** 服务端的机读码：同一个数据源已有排队、进行中的任务；起草模型用不了 */
export const DRAFT_BUSY = 'catalog_draft_busy'
export const DRAFT_MODEL_UNAVAILABLE = 'catalog_draft_model_unavailable'
const DRAFT_FINISHED = 'catalog_draft_finished'

export const isJobActive = (j: { status: string } | null | undefined): boolean =>
  !!j && (j.status === 'queued' || j.status === 'running')

const TASK_ENDED = new Set<CatalogDraftTaskStatus>(['ok', 'partial', 'failed', 'skipped', 'cancelled'])
/** 「只重试失败的表」带上的：失败、部分完成、已停止 */
export const RETRYABLE = new Set<CatalogDraftTaskStatus>(['failed', 'partial', 'cancelled'])

export const isTaskEnded = (t: CatalogDraftTask): boolean => TASK_ENDED.has(t.status)
export const retryableCount = (j: CatalogDraftJob): number => j.tasks.filter((t) => RETRYABLE.has(t.status)).length

/** 一张表这次为什么没写全：整张表没起草的原因在前，模型那部分的原因在后 */
export const taskReason = (t: { error: string | null; model_error: string | null }): string => t.error ?? t.model_error ?? ''

/**
 * 一项都没写进去的任务该怎么说（只对写完了的任务：完成、部分完成）。null：照常写合计。
 * 用模型、却没有一张表拿到模型的可用内容：目录没有更新、模型没给内容；有表没写全：目录没有更新、原因见下方；
 * 都写完了只是和现有目录一致：没有变化
 */
export function nothingWritten(j: CatalogDraftJob): 'model' | 'failed' | 'same' | null {
  if (j.added + j.updated + j.removed > 0 || (j.status !== 'succeeded' && j.status !== 'partial')) return null
  const live = j.tasks.filter((t) => t.status !== 'skipped')
  if (j.use_model && live.length && !live.some((t) => (t.model_items ?? 0) > 0)) return 'model'
  if (j.tasks.some((t) => RETRYABLE.has(t.status))) return 'failed'
  return 'same'
}

export interface DraftStartBody { tables?: string[]; use_model: boolean; note?: string }

export function useDraftJob(sourceId: string, active: CatalogActiveDraftJob | null | undefined, on: {
  /** 这一次取回来新结束的表（写进目录了，或者失败、停止了）：重取清单，正在看的表在里面就重新载入 */
  onTablesEnded: (tables: string[]) => void
  /** 看着它从排队、进行中变成结束（这一页上亲眼看到的，进页面时已经结束的不算） */
  onFinished: (job: CatalogDraftJob) => void
}) {
  // 任务挂在数据源上：换了数据源，上一个源的任务不再显示（那边照常进行）
  const [held, setHeld] = useState<{ src: string; job: CatalogDraftJob } | null>(null)
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
  const accept = useCallback((next: CatalogDraftJob, src: string): boolean => {
    if (src !== sourceRef.current) return false
    const cur = jobRef.current
    const same = cur?.id === next.id
    if (same && cur) {
      if (!isJobActive(cur) && isJobActive(next)) return false
      if (isJobActive(next) && next.done < cur.done) return false
    }
    // 新结束的表：同一个任务比上一次多结束的；头一回见到的进行中任务（别处建的、刷新页面前建的）已经结束的那些也算，
    // 清单可能还没跟上。头一回见到的已结束任务（点开上一次的结果）不算，那些早就写进去了
    const before = new Map((same && cur ? cur.tasks : []).map((t) => [t.table_name, t.status]))
    const ended = same || isJobActive(next)
      ? next.tasks
        .filter((t) => isTaskEnded(t) && t.status !== 'skipped' && !TASK_ENDED.has(before.get(t.table_name) ?? 'pending'))
        .map((t) => t.table_name)
      : []
    jobRef.current = next
    setHeld({ src, job: next })
    if (ended.length) onRef.current.onTablesEnded(ended)
    if (same && cur && isJobActive(cur) && !isJobActive(next)) onRef.current.onFinished(next)
    return true
  }, [])

  /** 换成另一个任务（刚建的、重试建的、409 时进行中的那个、对话框里点开的上一次）。同一个任务照常只往前走 */
  const track = useCallback((next: CatalogDraftJob) => {
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
        const { job: next } = await api.dataCatalog.draftJob(src, jobId)
        if (alive && !accept(next, src)) setTick((n) => n + 1)
      } catch (e) {
        if (!alive) return
        // 任务没了（不属于这个数据源、库被换掉了）：不再跟着它；别的失败（网络抖动、服务重启中）下一拍再取
        if (e instanceof ApiError && e.status === 404) setHeld((h) => (h?.job.id === jobId ? null : h))
        else setTick((n) => n + 1)
      }
    }, DRAFT_POLL_MS)
    return () => {
      alive = false
      clearTimeout(t)
    }
  }, [sourceId, jobId, jobActive, job, tick, accept])

  // 清单说有进行中的任务、这一页还不知道（别的标签页建的、刷新了页面）：取来跟着它
  const activeId = isJobActive(active) ? active?.id : undefined
  useEffect(() => {
    if (!activeId) return
    const cur = jobRef.current
    if (cur?.id === activeId) return
    const src = sourceId
    let alive = true
    api.dataCatalog.draftJob(src, activeId).then(({ job: next }) => { if (alive) accept(next, src) }, () => {})
    return () => { alive = false }
  }, [sourceId, activeId, accept])

  /** 建任务。409 已有任务时改去取进行中的那一个（busy 为真）；其余的错误照原样抛给调用方 */
  const start = useCallback(async (body: DraftStartBody): Promise<{ job: CatalogDraftJob; busy: boolean }> => {
    const src = sourceRef.current
    try {
      const { job: next } = await api.dataCatalog.createDraftJob(src, body)
      accept(next, src)
      return { job: next, busy: false }
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 409 && e.code === DRAFT_BUSY)) throw e
      const { job: current } = await api.dataCatalog.activeDraftJob(src)
      if (!current) throw e
      accept(current, src)
      return { job: current, busy: true }
    }
  }, [accept])

  /**
   * 只重试失败的表：同 start，409 已有任务时改去显示进行中的那一个；起草模型用不了（409）、没有可重试的表（422）等
   * 照原样抛给调用方
   */
  const retry = useCallback(async (from: CatalogDraftJob): Promise<{ job: CatalogDraftJob; busy: boolean }> => {
    const src = sourceRef.current
    try {
      const { job: next } = await api.dataCatalog.retryDraftJob(src, from.id)
      accept(next, src)
      return { job: next, busy: false }
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 409 && e.code === DRAFT_BUSY)) throw e
      const { job: current } = await api.dataCatalog.activeDraftJob(src)
      if (!current) throw e
      accept(current, src)
      return { job: current, busy: true }
    }
  }, [accept])

  /** 停止。服务端停下正在进行的调用之前任务还是进行中：这期间 stopping 为这个任务。已经结束了（409）就取一次最新的 */
  const cancel = useCallback(async () => {
    const cur = jobRef.current
    if (!cur || !isJobActive(cur)) return
    const src = sourceRef.current
    setStopping(cur.id)
    try {
      const { job: next } = await api.dataCatalog.cancelDraftJob(src, cur.id)
      accept(next, src)
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && e.code === DRAFT_FINISHED) {
        const { job: next } = await api.dataCatalog.draftJob(src, cur.id)
        accept(next, src)
        return
      }
      setStopping(null)
      throw e
    }
  }, [accept])

  return { job, track, start, retry, cancel, stopping: !!job && stopping === job.id && isJobActive(job) }
}
