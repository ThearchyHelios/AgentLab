import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ApiError, api, streamRun } from '../../api/client'
import { useCatalog, useOnReconnect } from '../../store/catalog'
import { emptyTrace, finalizeTrace, foldEvent, type Trace } from '../../run/trace'
import type { Approval, GraphSpec, Run, RunEvent } from '../../types'
import { isLiveRun } from './model'

const idsOf = (list: Approval[] | null) => (list ? list.map((a) => a.id).sort().join(',') : null)

// 流式增量不落库，刷新后也不会回来；时间线和航迹都不建立在它们之上。留着只会
// 让每个 token 触发一次整条时间线的重新解码
const EPHEMERAL = new Set(['llm.token', 'llm.thinking.delta'])

export interface RunDetail {
  run: Run | null
  events: RunEvent[]
  /** 运行时的那张图（快照）。取不到是 null：节点名退回 id */
  graph: GraphSpec | null
  /** 这条运行的待审批。null = 还没查到，别急着下"没有审批"的结论 */
  pending: Approval[] | null
  state: 'loading' | 'ok' | 'missing' | 'error'
  error: unknown
  /** 正接着实时流 */
  streaming: boolean
  trace: Trace
  reload: () => Promise<void>
  /**
   * 动作之后：拿后端回的最新 run 换上，并接上实时流。接着跑、恢复、审批处理
   * 完都走这里——批完之后要能看着它往下跑，而不是停在恢复的那一刻。
   */
  follow: (run?: Run | null) => void
  refreshPending: () => Promise<void>
}

/**
 * 一条运行的详情：run、完整事件、图快照、待审批，以及运行中的实时流。
 *
 * 实时流复用 client 的 streamRun，带上已有的最大 seq，后端只补这之后的；事件按
 * 动画帧合批再落进 state——解码是 O(n) 的，1860 条事件 5 秒内逐条 setState
 * 会退化成 O(n²)。
 */
export function useRunDetail(runId: string, onChange: (run: Run) => void): RunDetail {
  const [run, setRun] = useState<Run | null>(null)
  const [events, setEvents] = useState<RunEvent[]>([])
  const [graph, setGraph] = useState<GraphSpec | null>(null)
  const [pending, setPending] = useState<Approval[] | null>(null)
  const [state, setState] = useState<RunDetail['state']>('loading')
  const [error, setError] = useState<unknown>(null)
  const [streaming, setStreaming] = useState(false)
  const lastSeq = useRef(0)
  const buffer = useRef<RunEvent[]>([])
  const frame = useRef(0)
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange
  const refreshApprovals = useCatalog((s) => s.refreshApprovals)
  const approvalSig = useCatalog((s) => s.approvals.filter((a) => a.run_id === runId).map((a) => a.id).join(','))

  const refreshPending = useCallback(async () => {
    try {
      const list = await api.approvals.list({ run_id: runId, status: 'pending' })
      setPending(list.filter((a) => a.status === 'pending' && a.run_id === runId))
    } catch {
      /* 查不到就保持原样：pending 为 null 时按"不知道"处理 */
    }
  }, [runId])

  const adopt = useCallback((next: Run) => {
    setRun(next)
    onChangeRef.current(next)
  }, [])

  const reload = useCallback(async () => {
    try {
      const r = await api.runs.get(runId)
      const evs = await api.runs.events(runId)
      const kept = evs.filter((e) => !EPHEMERAL.has(e.type))
      lastSeq.current = kept.reduce((m, e) => Math.max(m, e.seq ?? 0), 0)
      setEvents(kept)
      adopt(r)
      setState('ok')
      setError(null)
      if (isLiveRun(r.status)) setStreaming(true)
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) setState('missing')
      else {
        setError(e)
        setState((s) => (s === 'ok' ? s : 'error'))
      }
    }
  }, [runId, adopt])

  useEffect(() => {
    void reload()
    void refreshPending()
    let live = true
    api.runs.graph(runId).then((g) => { if (live) setGraph(g.graph?.nodes ? g.graph : null) }, () => {})
    return () => { live = false }
  }, [runId, reload, refreshPending])

  // 停在审批上的运行，别处（画布、问数据、另一个标签页）一动，这里就得重查。
  // run 和审批要一起查、同一拍换上：只换审批的话，别处刚批掉的那一刻这里是
  // 「interrupted、没有审批」，会被当成挂起。
  // 别处发生的事件这里一条也没收到，所以运行只要离开了原来停着的样子（接着跑了、
  // 已经跑完或失败、又停到了新的审批上），就整条重拉、在跑的再接上流。只看
  // 「还在跑才接流」的话，审批靠近末尾的那种等下一轮轮询时早就跑完了：头上
  // 变成已完成，时间线却停在审批卡上，连「放行」那一行都没有
  const runRef = useRef(run)
  runRef.current = run
  const pendingRef = useRef(pending)
  pendingRef.current = pending
  const syncEpoch = useRef(0)
  const syncTimer = useRef(0)
  const resync = useCallback((force: boolean) => {
    const epoch = ++syncEpoch.current
    window.clearTimeout(syncTimer.current)
    const attempt = (retry: boolean) => Promise.all([
      api.runs.get(runId),
      api.approvals.list({ run_id: runId, status: 'pending' }),
    ]).then(([r, list]) => {
      if (epoch !== syncEpoch.current) return
      const mine = list.filter((a) => a.status === 'pending' && a.run_id === runId)
      // 后端先把审批记成已回复、再把运行改成 running，中间有一瞬两头都不是。
      // 正好撞上就隔一会儿再看一次，不急着下「挂起」的结论
      if (retry && r.status === 'interrupted' && !mine.length) {
        syncTimer.current = window.setTimeout(() => void attempt(false), 1200)
        return
      }
      const moved = force || r.status !== 'interrupted' || idsOf(mine) !== idsOf(pendingRef.current)
      setPending(mine)
      adopt(r)
      if (moved) void reload()
    }, () => {})
    void attempt(true)
  }, [runId, adopt, reload])
  useEffect(() => () => { syncEpoch.current++; window.clearTimeout(syncTimer.current) }, [runId])

  // 全局待审批变了（别处批掉了、新挂起了一个）
  const sigSeen = useRef(approvalSig)
  useEffect(() => {
    if (sigSeen.current === approvalSig) return
    sigSeen.current = approvalSig
    if (runRef.current?.status === 'interrupted') resync(false)
    else void refreshPending()
  }, [approvalSig, refreshPending, resync])

  // 断网恢复：事件可能漏了一截，整条重拉。停在审批上的走同一条重查——断开期间
  // 多半就是别处批掉了它，恢复这一刻重拉，同样会撞上那个两头都不是的空档
  useOnReconnect(() => {
    if (runRef.current?.status === 'interrupted') resync(true)
    else { void reload(); void refreshPending() }
  })

  const flush = useCallback(() => {
    frame.current = 0
    const batch = buffer.current
    if (!batch.length) return
    buffer.current = []
    setEvents((prev) => {
      const last = prev.length ? prev[prev.length - 1].seq : 0
      const fresh = batch.filter((e) => !e.seq || e.seq > last)
      return fresh.length ? [...prev, ...fresh] : prev
    })
  }, [])

  useEffect(() => {
    if (!streaming) return
    const onEvent = (ev: RunEvent) => {
      if (EPHEMERAL.has(ev.type)) return
      if (ev.seq) {
        if (ev.seq <= lastSeq.current) return
        lastSeq.current = ev.seq
      }
      buffer.current.push(ev)
      if (!frame.current) frame.current = requestAnimationFrame(flush)
      // 停到审批上：审批卡要马上出来，不等 4 秒一轮的轮询
      if (ev.type === 'run.interrupted' || ev.type === 'human.requested' || ev.type === 'run.resumed') {
        void refreshPending()
        void refreshApprovals()
      }
    }
    const onClose = () => {
      setStreaming(false)
      // 终态以后端为准：状态、用量、封存凭证都在 run 上
      void api.runs.get(runId).then(adopt, () => {})
      void refreshPending()
      void refreshApprovals()
    }
    const stop = streamRun(runId, onEvent, onClose, lastSeq.current)
    return () => {
      stop()
      if (frame.current) cancelAnimationFrame(frame.current)
      flush()
    }
  }, [streaming, runId, flush, adopt, refreshPending, refreshApprovals])

  const follow = useCallback((next?: Run | null) => {
    if (next) adopt(next)
    setStreaming(true)
  }, [adopt])

  const trace = useMemo(() => {
    let t = events.reduce(foldEvent, emptyTrace())
    // 不在接流时，事件之外的事实以 run 为准：服务被强杀时连 server_shutdown 都
    // 不会发，只有 interrupted 且没有待审批才知道它其实挂起了
    // 停下的时刻不知道，记在最后一条事件上（ts 给 0 会被当成 1970 年）
    if (!streaming && run && !isLiveRun(run.status) && pending) {
      t = foldEvent(t, {
        seq: 0, type: 'stream.end', node_id: null, ts: events[events.length - 1]?.ts ?? Number.NaN,
        data: { status: run.status, pending: pending.length > 0 },
      })
    }
    return finalizeTrace(t, graph ?? undefined)
  }, [events, streaming, run, pending, graph])

  return { run, events, graph, pending, state, error, streaming, trace, reload, follow, refreshPending }
}
