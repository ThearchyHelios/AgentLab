import { useCallback, useEffect, useState } from 'react'
import { api } from '../../../api/client'
import type { RunCompareBase, RunComparison, RunWithSchedule } from '../../../types'

/**
 * 对比页要的几份数据：默认基准、两次运行各自的记录（运行卡、封存与否、所属工作流）、同一工作流最近的运行（换基准的
 * 下拉）、对比本身。每份按键取：键变了（换了基准、互换）就丢掉上一份，过时的答复回来也不认。
 */

export interface Loaded<T> {
  /** idle：键是空的，不取 */
  state: 'idle' | 'loading' | 'ok' | 'error'
  data?: T
  error?: unknown
  reload: () => void
}

type Slot<T> = { key: string; state: 'loading' | 'ok' | 'error'; data?: T; error?: unknown }

/**
 * 按键取一份数据。请求晚一拍（下一个宏任务）才发：开发模式的 StrictMode 会把挂载时的 effect 先跑一遍、立即撤掉再跑，
 * 撤掉时把还没发的那一拍清掉，同一份对比就只算一次（对比要读两次运行的工件、核对封存，不便宜）。键变了、卸载了
 * 都会取消在路上的请求；答复对不上当前的键就丢掉
 */
function useKeyed<T>(key: string | null, load: (signal: AbortSignal) => Promise<T>): Loaded<T> {
  const [slot, setSlot] = useState<Slot<T> | null>(null)
  const [nonce, setNonce] = useState(0)
  useEffect(() => {
    if (!key) return
    const ctrl = new AbortController()
    const timer = setTimeout(() => {
      setSlot({ key, state: 'loading' })
      load(ctrl.signal).then(
        (data) => { if (!ctrl.signal.aborted) setSlot({ key, state: 'ok', data }) },
        (error: unknown) => {
          if (ctrl.signal.aborted || (error as { name?: string } | null)?.name === 'AbortError') return
          setSlot({ key, state: 'error', error })
        },
      )
    }, 0)
    return () => {
      clearTimeout(timer)
      ctrl.abort()
    }
    // load 由键决定：键里已经写着它用到的全部参数
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, nonce])
  const reload = useCallback(() => setNonce((n) => n + 1), [])
  if (!key) return { state: 'idle', reload }
  // 键刚变、请求还没发出去的那一下也算加载中：不拿上一份（另一对运行的）充数
  if (!slot || slot.key !== key) return { state: 'loading', reload }
  return { state: slot.state, data: slot.data, error: slot.error, reload }
}

/** 默认基准（只带了本次时才找）。找不到是 404，交给页面照实说 */
export function useDefaultBase(target: string | null): Loaded<RunCompareBase> {
  return useKeyed(target, (signal) => api.compare.base(target!, { signal }))
}

/** 一次运行的记录：运行卡上的名字、时间、版本、类别、封存与否，和它属于哪个工作流 */
export function useRunRecord(id: string | null): Loaded<RunWithSchedule> {
  return useKeyed(id, () => api.runs.get(id!) as Promise<RunWithSchedule>)
}

/** 换基准的下拉：同一工作流最近的运行（新的在前）。没保存的工作流（workflow_id 为空）不取 */
export const CANDIDATE_LIMIT = 50

export function useCandidates(workflowId: string | null | undefined): Loaded<RunWithSchedule[]> {
  return useKeyed(workflowId || null, () =>
    api.runs.list({ workflow_id: workflowId!, limit: CANDIDATE_LIMIT }) as Promise<RunWithSchedule[]>)
}

/** 对比本身 */
export function useComparison(target: string | null, base: string | null): Loaded<RunComparison> {
  return useKeyed(target && base ? JSON.stringify([target, base]) : null,
    (signal) => api.compare.get(target!, base!, { signal }))
}
