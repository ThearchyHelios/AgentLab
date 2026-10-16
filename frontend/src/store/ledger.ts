import { useEffect } from 'react'
import { create } from 'zustand'
import { api } from '../api/client'
import type { LedgerIssueAction, RunLedger } from '../typesLedger'

/**
 * 一次运行的口径账本（GET /api/runs/{id}/ledger）：证据正文里数字上的小标记、证据面板的「口径」一步、问数据轮次底部的
 * 摘要和动作都读这一份，同一次运行只取一份。
 *
 * **比对结果不靠实时流。** 比对在封存之后放到后台做，结果是一条封存后追加的 ledger.matched——那时这次运行的事件总线已经
 * 关了，打开着的页面收不到（第 2 波遗留）。所以这里按退避轮询：服务端答 pending（正在比对，或者刚跑完、封存完就比）时
 * 隔 1、2、3、5、8、13 秒、之后每 15 秒再取一次，等满 MATCH_WAIT_MS 还没结论就停下、照实说（timedOut），给「再取一次」。
 * 比对完、要自动按口径出具的（动作写着 auto、还没有发起的那一次）再等一会儿，等到出具的正式运行出现；出具、对账的运行
 * 还没结束的，隔一阵再取，等它们结束（档位、三个数要等运行结束才有）。
 *
 * 只在有人看着（有组件挂着 useRunLedger）时轮询；不比对的（ineligible）答复只当 INELIGIBLE_TTL_MS 内的结论：运行状态会变
 * （还在跑的运行、审批停着的运行之后才成功结束）。界面层不放逻辑：要不要接着等只看服务端给的 state、actions、运行状态。
 */

export interface LedgerSlot {
  status: 'loading' | 'ok' | 'error'
  data?: RunLedger
  error?: unknown
  /** 等满了还没有比对结论（或者自动出具一直没出现）：停下来照实说 */
  timedOut?: boolean
  /** 这一轮等待从什么时候开始（毫秒） */
  since?: number
  fetchedAt?: number
}

/** 等比对出结论最多多久 */
export const MATCH_WAIT_MS = 120_000
/** 比对完、动作写着要自动出具的：再等这么久，等出具的那一次运行出现 */
export const AUTO_WAIT_MS = 60_000
/** 出具、对账的运行还没结束：最多再等这么久 */
export const RUN_WAIT_MS = 15 * 60_000
const INELIGIBLE_TTL_MS = 20_000
const BACKOFF = [1000, 2000, 3000, 5000, 8000, 13000]
const SLOW = 15_000
/** 出具、对账运行还在跑时隔多久取一次 */
const RUN_POLL = 5000

/** 运行还没结束的几种状态（interrupted 是停在审批或挂起，照样算没结束） */
const LIVE = new Set(['queued', 'running', 'interrupted'])

/** 动作写着要自动按口径出具、还没有发起的那一次（服务端判的能不能自动，这里只看它给的字段） */
export function autoIssuePending(data: RunLedger | undefined): LedgerIssueAction | undefined {
  if (!data || data.state !== 'matched') return undefined
  const action = (data.actions ?? []).find((a): a is LedgerIssueAction => a.kind === 'issue' && a.auto && !a.issued_run)
  return action && !(data.issued ?? []).some((i) => i.workflow_id === action.workflow_id && i.version === action.version)
    ? action : undefined
}

/** 出具、对账的运行里还有没结束的 */
export const runsLive = (data: RunLedger | undefined): boolean =>
  !!data && [...(data.issued ?? []), ...(data.reconciled ?? [])].some((r) => LIVE.has(r.status))

interface LedgerState {
  slots: Record<string, LedgerSlot>
  /** 有人看着这次运行：没取过、取失败了、不比对的结论过期了就取，按需接着轮询 */
  acquire: (runId: string) => void
  release: (runId: string) => void
  /** 刚发起了出具、对账，或者用户点了「再取一次」：马上取，重新开始等 */
  refresh: (runId: string) => void
}

const watchers = new Map<string, number>()
const timers = new Map<string, ReturnType<typeof setTimeout>>()
const attempts = new Map<string, number>()
const inflight = new Map<string, Promise<void>>()

const clear = (runId: string) => {
  const t = timers.get(runId)
  if (t) clearTimeout(t)
  timers.delete(runId)
}

export const useLedger = create<LedgerState>((set, get) => {
  const patch = (runId: string, next: Partial<LedgerSlot>) =>
    set((s) => ({ slots: { ...s.slots, [runId]: { ...(s.slots[runId] ?? { status: 'loading' }), ...next } } }))

  /** 下一次什么时候取（毫秒）；不用再取了为 null。到点了还没结论的，顺手记 timedOut */
  const nextDelay = (runId: string): number | null => {
    const slot = get().slots[runId]
    const data = slot?.data
    const since = slot?.since ?? Date.now()
    const waited = Date.now() - since
    const n = attempts.get(runId) ?? 0
    const backoff = BACKOFF[n] ?? SLOW
    if (slot?.status === 'error') return null
    if (data?.state === 'pending') {
      if (waited >= MATCH_WAIT_MS) { patch(runId, { timedOut: true }); return null }
      return backoff
    }
    if (autoIssuePending(data)) {
      if (waited >= AUTO_WAIT_MS) { patch(runId, { timedOut: true }); return null }
      return backoff
    }
    if (runsLive(data)) return waited >= RUN_WAIT_MS ? null : Math.max(RUN_POLL, Math.min(backoff, SLOW))
    return null
  }

  const schedule = (runId: string) => {
    clear(runId)
    if (!watchers.get(runId)) return
    const delay = nextDelay(runId)
    if (delay == null) return
    attempts.set(runId, (attempts.get(runId) ?? 0) + 1)
    timers.set(runId, setTimeout(() => { timers.delete(runId); void load(runId) }, delay))
  }

  const load = (runId: string): Promise<void> => {
    const going = inflight.get(runId)
    if (going) return going
    const before = get().slots[runId]
    if (!before?.data) patch(runId, { status: 'loading' })
    const p = api.caliberLedger.run(runId).then(
      (data) => {
        const prev = get().slots[runId]?.data
        // 等的东西变了（比对出了结论、自动出具出现了）：等待从这一刻重新算
        const moved = !!prev && (prev.state !== data.state
          || (prev.issued?.length ?? 0) !== (data.issued?.length ?? 0))
        if (moved) attempts.set(runId, 0)
        patch(runId, { status: 'ok', data, error: undefined, fetchedAt: Date.now(), timedOut: false,
                       ...(moved ? { since: Date.now() } : {}) })
      },
      (error) => { patch(runId, { status: 'error', error, fetchedAt: Date.now() }) },
    ).finally(() => {
      inflight.delete(runId)
      schedule(runId)
    })
    inflight.set(runId, p)
    return p
  }

  return {
    slots: {},
    acquire: (runId) => {
      watchers.set(runId, (watchers.get(runId) ?? 0) + 1)
      const slot = get().slots[runId]
      const stale = !slot || slot.status === 'error'
        || (slot.data?.state === 'ineligible' && Date.now() - (slot.fetchedAt ?? 0) > INELIGIBLE_TTL_MS)
      if (stale) {
        attempts.set(runId, 0)
        patch(runId, { since: Date.now(), timedOut: false })
        void load(runId)
      } else if (!timers.has(runId) && !inflight.has(runId) && !slot.timedOut) {
        schedule(runId)
      }
    },
    release: (runId) => {
      const n = (watchers.get(runId) ?? 1) - 1
      if (n > 0) { watchers.set(runId, n); return }
      watchers.delete(runId)
      clear(runId)
    },
    refresh: (runId) => {
      clear(runId)
      attempts.set(runId, 0)
      patch(runId, { since: Date.now(), timedOut: false })
      void load(runId)
    },
  }
})

/**
 * 这次运行的口径账本。enabled 为假（运行还没结束、正式运行、没有运行）时不取：正式运行本身就是某一版口径的出具，不比对。
 * 同一次运行挂几处（正文、面板、轮次摘要）只取一份、只轮询一份
 */
export function useRunLedger(runId: string | null | undefined, enabled = true): LedgerSlot | undefined {
  const acquire = useLedger((s) => s.acquire)
  const release = useLedger((s) => s.release)
  const on = !!runId && enabled
  useEffect(() => {
    if (!on || !runId) return
    acquire(runId)
    return () => release(runId)
  }, [on, runId, acquire, release])
  return useLedger((s) => (on && runId ? s.slots[runId] : undefined))
}
