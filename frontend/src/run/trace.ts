import type { RunEvent, RunStatus } from '../types'
import { endingOf, nextPhase, type Ending } from './decode'
import { applyDerived, type GraphLike } from './derive'

/**
 * 运行航迹：事件 → 航迹 → 投影(t)。
 *
 * 画布卡片、运行胶囊、时间轴、右栏高亮、运行记录页都读这一份，不再各自解读
 * 事件。以前 studio 的 applyEvent 是覆盖式快照：durationMs、iteration 被下一轮
 * 覆盖，没有任何按时间索引的视图，于是"哪几个同时在跑、等了多久、时间花在
 * 哪"说不出来；终态也没人收尾，取消之后节点永远在转圈。
 *
 * 和 decode.ts 并列：decode 管"事件 → 人话"，这里只管时间和状态。收尾规则
 * （终态时进行中的东西收成什么）两边共用 decode 里的 endingOf / nextPhase，
 * 同一次运行在画布和右栏不会讲出两种结局。
 *
 * 纯函数，不依赖 React 和 store：回放就是把同一份航迹投影到另一个时刻。
 * 时间一律是毫秒时间戳（事件的 ts 是秒）。
 */

export type RunPhase =
  | 'idle' | 'queued' | 'running' | 'waiting' | 'succeeded' | 'failed' | 'cancelled' | 'suspended'

export type NodeState =
  | 'idle' | 'queued' | 'running' | 'waiting' | 'done' | 'failed' | 'skipped'
  | 'cancelled' | 'suspended' | 'blocked' | 'unreached'

export interface Segment {
  start: number
  /** null = 还开着 */
  end: number | null
  kind: 'run' | 'wait' | 'retry' | 'dispatch' | 'member'
  /** 开着时是 running / waiting；闭合后是这一段的结局 */
  status: NodeState
  /** 循环第几轮（从 1 数）；成员段、调度段是协作的第几轮 */
  iteration?: number
  agent?: string
  /** 由事件间隙推算的（调度者在想的那段），不是测量值 */
  estimated?: boolean
  /** 这一段走的出口：分支命中的 case、循环的 body / done */
  handle?: string
  /** 审批恢复后接回的那一段。和中断前是同一次执行，不另算次数 */
  resumed?: boolean
}

export interface NodeTrace {
  id: string
  state: NodeState
  /** 执行了几次（循环、重试后接着跑都会让它 > 1；审批恢复的重放不算） */
  count: number
  /** 循环节点：第几轮（从 1 数）。走 done 出口时不再变 */
  iteration?: number
  /** 循环节点：foreach 的总项数（edge.taken.total）。while 没有总数 */
  iterTotal?: number
  attempt?: number
  startedAt?: number
  endedAt?: number
  lastDurationMs?: number
  tokensIn: number
  tokensOut: number
  costUsd: number
  tools: number
  toolsRunning: number
  error?: string
  skippedReason?: string
  model?: string
  segments: Segment[]
  /** 循环节点一轮跑完、出口是 body：容器还在进行中，不是完成 */
  looping?: boolean
  /** 循环节点走了 done 出口 */
  loopDone?: boolean
  /** 最近一次走的出口；taken 是走过的全部出口（循环里会有好几个） */
  takenHandle?: string
  taken?: string[]
  /** 分支命中的理由 */
  reason?: string
}

export interface Trace {
  phase: RunPhase
  startedAt?: number
  /** 终态或挂起的时刻。续跑、恢复后清掉 */
  endedAt?: number
  /** 已结束的等人时长之和；正在等的那段看 waitingSince */
  waitMs: number
  waitingSince?: number
  nodes: Record<string, NodeTrace>
  tokensIn: number
  tokensOut: number
  costUsd: number
  failedNodeId?: string
  waitingNodeId?: string
  issuance?: {
    tier: string
    missing?: string[]
    unmatched?: string[]
    /** 必需指标缺了哪些（missing 里包含必需和期望两类） */
    missingRequired?: string[]
    /** 后端只给了数量时在这里 */
    unmatchedCount?: number
  }
  runClass?: 'formal' | 'exploratory'
  lastSeq: number
  /** [时刻, 此刻同时在跑的数量]，只在数量变化时记一条 */
  parallelSeries: [number, number][]

  /** false = 老数据没有 ts：段按顺序排、宽度只来自 duration_ms，墙钟和等人时长不可知 */
  timed: boolean
  /** run.started.nodes：图上一共几个节点 */
  nodesTotal?: number
  /** 每一段执行（run.started → 中断 / 终态）的起止。执行时长 = 各段之和 */
  drives: [number, number | null][]
  /** 每一段等人（run.interrupted → run.resumed）的起止 */
  waits: [number, number | null][]
  /** 相位变化记录，回放按它取某一刻的相位 */
  phases: [number, RunPhase][]
  /** 后端给的权威时长（终态事件的 timing）。有它就不用自己算的 */
  timing?: { wallMs: number; activeMs: number; waitMs: number }
  /** 最后一条事件是 WS 补发的历史（replay:true）。一次性时刻只由实时事件触发 */
  lastReplay: boolean
  /** 客户端时钟减服务端时钟（取实时事件里最小的那次），由 store 维护。见 liveAt */
  skewMs?: number
  /** 折叠用的簿记。消费方不要读 */
  book: TraceBook
}

interface TraceBook {
  /** 最后一条事件的时刻。没有 ts 的老数据拿它当虚拟时钟往前推 */
  clock: number
  /** 有没有停在审批上（和 decodePhase 用同一个 nextPhase 推） */
  awaiting: boolean
  /** 最近一次失败、之后没有新节点开跑的节点：run.failed 时它就是失败的那个 */
  lastFailed?: string
  /** 最近一次循环 body 的轮次，给循环体节点的段标号 */
  loopIter?: number
  /** supervisor 节点：推算调度段的起点（节点开始或上一个成员交回的时刻） */
  dispatchFrom: Record<string, number>
  /** 发过 agent.route.* 的节点：调度段有精确值，不再推算 */
  exactDispatch: Record<string, true>
  /**
   * 还开着的审批配对（和 decodeRun 的 openInterrupts 同一套规则）：开着就是同一次
   * 中断，恢复后 LangGraph 重放的那条 human.requested 不算新的一次
   */
  asking: Record<string, true>
}

export interface Projection {
  phase: RunPhase
  /** 墙钟：从第一次开始到结束（或此刻） */
  elapsedMs: number
  /** 执行：各段执行时长之和 */
  activeMs: number
  /** 等人 */
  waitMs: number
  nodesDone: number
  nodesTotal: number
  parallelNow: number
  nodes: Record<string, { state: NodeState; elapsedMs?: number; count: number; iteration?: number }>
}

// 流式增量：后端不落库，刷新后也不会回来。航迹不能建立在它们之上
const EPHEMERAL = new Set(['llm.token', 'llm.thinking.delta'])
const TERMINAL = new Set<RunPhase>(['succeeded', 'failed', 'cancelled'])

export const isTerminal = (phase: RunPhase): boolean => TERMINAL.has(phase)
/** 终态或挂起：这次运行眼下不会再自己动了 */
export const isSettled = (phase: RunPhase): boolean => TERMINAL.has(phase) || phase === 'suspended'
export const isActivePhase = (phase: RunPhase): boolean =>
  phase === 'queued' || phase === 'running' || phase === 'waiting'

/** 这个节点此刻真的在执行（循环容器在两轮之间不算，它只是"还没完"） */
export const isExecuting = (n: NodeTrace | undefined): boolean =>
  !!n && n.state === 'running' && !n.looping

/**
 * 相位写回 run.status 用的值。
 *
 * waiting 对应后端的 interrupted；suspended 后端也写 interrupted，前端单独叫它，
 * 因为"停在审批上"和"服务重启后挂着"要做的事完全不同。idle 没有对应值。
 */
export function runStatusOf(phase: RunPhase): RunStatus | null {
  switch (phase) {
    case 'idle': return null
    case 'waiting': return 'interrupted'
    default: return phase
  }
}

/**
 * 实时投影用的时刻。
 *
 * ts 是服务端时钟，now 是客户端时钟。两边差几秒时直接拿 now 去投影，刚开跑的
 * 节点会因为"还没到开始时刻"显示成没开始、计时器是负数。同机部署时偏差是 0。
 */
export const liveAt = (t: Trace, now: number): number => now - (t.skewMs ?? 0)

export function emptyTrace(): Trace {
  return {
    phase: 'idle', waitMs: 0, nodes: {}, tokensIn: 0, tokensOut: 0, costUsd: 0,
    lastSeq: 0, parallelSeries: [], timed: true, drives: [], waits: [], phases: [],
    lastReplay: false,
    book: { clock: 0, awaiting: false, dispatchFrom: {}, exactDispatch: {}, asking: {} },
  }
}

function blankNode(id: string, state: NodeState = 'idle'): NodeTrace {
  return {
    id, state, count: 0, tokensIn: 0, tokensOut: 0, costUsd: 0, tools: 0, toolsRunning: 0,
    segments: [],
  }
}

const num = (v: unknown): number | undefined =>
  typeof v === 'number' && Number.isFinite(v) ? v : undefined

/** 最后一段还开着的某类段的下标 */
function openIndex(segs: Segment[], kind: Segment['kind'], agent?: string): number {
  for (let i = segs.length - 1; i >= 0; i -= 1) {
    const s = segs[i]
    if (s.kind === kind && s.end == null && (agent == null || s.agent === agent)) return i
  }
  return -1
}

/**
 * 此刻同时在跑的数量。
 *
 * 有成员在跑的协作节点按成员数算，容器本身不计——三个人同时在干活，说"并行 4"
 * 就把调度者的空等也算成了干活。循环容器在两轮之间没有开着的段，自然不计。
 */
function parallelOf(nodes: Record<string, NodeTrace>): number {
  let total = 0
  for (const n of Object.values(nodes)) {
    let inner = 0
    let run = 0
    for (const s of n.segments) {
      if (s.end != null) continue
      if (s.kind === 'member' || s.kind === 'dispatch') inner += 1
      else if (s.kind === 'run') run = 1
    }
    total += inner || run
  }
  return total
}

/**
 * 写时复制的草稿：只复制这一次碰到的节点和簿记，其余沿用原引用。
 *
 * 不可变是必须的：画布那份挂在 zustand 上，原地改的话引用不变，React 收不到更新。
 */
interface Draft {
  next: Trace
  at: number
  node: (id: string) => NodeTrace
  book: () => TraceBook
  patchSeg: (n: NodeTrace, i: number, patch: Partial<Segment>) => void
  setPhase: (phase: RunPhase) => void
}

function draftOf(t: Trace, at: number): Draft {
  const next: Trace = { ...t }
  let nodesCopied = false
  const touched = new Set<string>()
  let bookCopied = false
  const dr: Draft = {
    next,
    at,
    node: (id) => {
      if (!nodesCopied) { next.nodes = { ...next.nodes }; nodesCopied = true }
      if (!touched.has(id)) {
        const prev = next.nodes[id]
        next.nodes[id] = prev ? { ...prev, segments: [...prev.segments] } : blankNode(id)
        touched.add(id)
      }
      return next.nodes[id]
    },
    book: () => {
      if (!bookCopied) {
        next.book = {
          ...next.book,
          dispatchFrom: { ...next.book.dispatchFrom },
          exactDispatch: { ...next.book.exactDispatch },
          asking: { ...next.book.asking },
        }
        bookCopied = true
      }
      return next.book
    },
    patchSeg: (n, i, patch) => { n.segments[i] = { ...n.segments[i], ...patch } },
    setPhase: (phase) => {
      if (next.phase === phase) return
      next.phase = phase
      next.phases = [...next.phases, [dr.at, phase]]
    },
  }
  return dr
}

function closeDrive(dr: Draft): void {
  const { next } = dr
  const last = next.drives[next.drives.length - 1]
  if (last && last[1] == null) next.drives = [...next.drives.slice(0, -1), [last[0], dr.at]]
}

function closeWait(dr: Draft): void {
  const { next } = dr
  const last = next.waits[next.waits.length - 1]
  if (last && last[1] == null) {
    next.waits = [...next.waits.slice(0, -1), [last[0], dr.at]]
    next.waitMs += Math.max(0, dr.at - last[0])
  }
  next.waitingSince = undefined
  next.waitingNodeId = undefined
}

/**
 * 终态 / 挂起时的收尾：还挂着"进行中"的东西各自收成 ending 指定的样子。
 *
 * 协作成员、调度段、开着的等待段一起收——只收节点的话，画布上协作团队的
 * 光弧和"进行中"会一直在，入边的光点也还在流，而运行其实早就停了。
 */
function settle(dr: Draft, ending: Ending): void {
  const { next, at } = dr
  for (const [id, cur] of Object.entries(next.nodes)) {
    const running = cur.state === 'running'
    const waiting = cur.state === 'waiting'
    const holdWait = waiting && ending.waiting === 'waiting'
    const openSegs = cur.segments.some((s) => s.end == null && !(s.kind === 'wait' && holdWait))
    if (!running && !(waiting && !holdWait) && !openSegs && cur.state !== 'queued') continue
    const n = dr.node(id)
    n.segments.forEach((s, i) => {
      if (s.end != null || (s.kind === 'wait' && holdWait)) return
      dr.patchSeg(n, i, { end: at, status: s.kind === 'wait' ? ending.waiting : ending.running })
    })
    if (running) {
      n.state = ending.running
      n.looping = false
      n.toolsRunning = 0
      n.endedAt = at
    } else if (waiting) {
      n.state = ending.waiting
    } else if (n.state === 'queued') {
      n.state = 'idle'
    }
  }
  closeDrive(dr)
  if (ending.phase !== 'waiting') {
    closeWait(dr)
    next.endedAt = at
    // 不再有人等着答复：之后同一个节点再来的请求是新的一次
    if (Object.keys(next.book.asking).length) dr.book().asking = {}
  }
  dr.setPhase(ending.phase)
}

/**
 * 把一条事件折进航迹，返回新的航迹；和这条事件无关时原样返回同一个引用。
 *
 * llm.token / llm.thinking.delta 一律原样返回：它们不落库，刷新后不会回来，
 * 航迹不能建立在它们之上；而且一次运行有几千条，每条都换引用会让订阅航迹的
 * 组件跟着逐字重渲染。
 *
 * type 为 stream.end 的是客户端合成的对账事件（WS 结束标记、GET /runs/{id}
 * 对出来的状态），data 为 {status, pending?}：漏了终态事件（断线、服务被强杀）
 * 时靠它收尾。
 */
export function foldEvent(t: Trace, ev: RunEvent): Trace {
  const type = String(ev.type)
  if (EPHEMERAL.has(type)) return t
  const seq = num(ev.seq) ?? 0
  // 断线重连、审批恢复后重接都可能把已经收过的再推一遍：同一条不折两遍
  if (seq > 0 && seq <= t.lastSeq) return t

  const d: Record<string, any> = ev.data ?? {}
  const synthetic = type === 'stream.end'
  if (synthetic) {
    // 对账只在"它其实已经停了"时才有话说；还在跑、或者早已收过同一个结局，原样返回
    const ending = endingOf(ev, t.book.awaiting)
    if (!ending || (ending.phase === t.phase && isSettled(t.phase))) return t
  }
  const tsOk = num(ev.ts) != null
  const dr = draftOf(t, tsOk ? ev.ts * 1000 : t.book.clock)
  const { next, node, book, patchSeg, setPhase } = dr
  let at = dr.at
  if (seq > next.lastSeq) next.lastSeq = seq
  if (!synthetic) next.lastReplay = !!ev.replay
  if (!tsOk && !synthetic) next.timed = false

  const openDrive = () => {
    const last = next.drives[next.drives.length - 1]
    if (!last || last[1] != null) next.drives = [...next.drives, [dr.at, null]]
  }
  /** 续跑、恢复之后：上一回合的失败、推出来的"被阻断 / 没走到"都不再成立 */
  const reopen = () => {
    next.endedAt = undefined
    next.timing = undefined
    // 留着的话，接着跑成功了还会有人报"失败于 X"
    next.failedNodeId = undefined
    if (next.book.lastFailed) book().lastFailed = undefined
    for (const [id, n] of Object.entries(next.nodes)) {
      if (n.state === 'blocked' || n.state === 'unreached') node(id).state = 'idle'
    }
  }

  const prevPhase = t.phase
  const phaseStep = nextPhase({ phase: t.phase, awaiting: t.book.awaiting }, ev)
  if (phaseStep.awaiting !== t.book.awaiting) book().awaiting = phaseStep.awaiting
  const nodeId = ev.node_id ?? undefined

  switch (type) {
    case 'run.started':
    case 'run.resumed': {
      if (type === 'run.resumed') {
        // 人已经答过了：等人的那段闭合，等待节点回到"在跑"。紧接着 LangGraph 会
        // 对它重发 node.started——那是同一次执行接着走，这里先把段接上，
        // 那条重放进来时看到开着的段就不会再算一次
        closeWait(dr)
        for (const [id, cur] of Object.entries(next.nodes)) {
          if (cur.state !== 'waiting') continue
          const n = node(id)
          n.segments.forEach((s, i) => {
            if (s.kind === 'wait' && s.end == null) patchSeg(n, i, { end: at, status: 'done' })
          })
          n.segments.push({ start: at, end: null, kind: 'run', status: 'running', resumed: true })
          n.state = 'running'
        }
      }
      if (next.startedAt == null) next.startedAt = at
      if (prevPhase !== 'running' && prevPhase !== 'queued' && prevPhase !== 'idle') reopen()
      if (type === 'run.started') next.nodesTotal = num(d.nodes) ?? next.nodesTotal
      openDrive()
      setPhase(phaseStep.phase)
      break
    }

    case 'run.interrupted': {
      const id = nodeId ?? (d.payload?.node_id ? String(d.payload.node_id) : undefined)
      const key = String(d.payload?.node_id ?? nodeId ?? '_')
      if (!next.book.asking[key]) book().asking[key] = true
      closeDrive(dr)
      const last = next.waits[next.waits.length - 1]
      if (!last || last[1] != null) {
        next.waits = [...next.waits, [at, null]]
        next.waitingSince = at
      }
      if (id) {
        next.waitingNodeId ??= id
        const n = node(id)
        const open = openIndex(n.segments, 'run')
        if (open >= 0) patchSeg(n, open, { end: at, status: 'waiting' })
        if (openIndex(n.segments, 'wait') < 0) {
          n.segments.push({ start: at, end: null, kind: 'wait', status: 'waiting' })
        }
        n.state = 'waiting'
      }
      setPhase(phaseStep.phase)
      break
    }

    case 'node.started': {
      if (!nodeId) break
      const n = node(nodeId)
      const open = openIndex(n.segments, 'run')
      const lastSeg = n.segments[n.segments.length - 1]
      if (open >= 0) {
        // 审批恢复后的重放：段在 run.resumed 时已经接上了
      } else if (d.resumed === true && lastSeg?.kind === 'wait') {
        // 后端标明是恢复重放、但前面漏了 run.resumed：一样接回去
        if (lastSeg.end == null) patchSeg(n, n.segments.length - 1, { end: at, status: 'done' })
        n.segments.push({ start: at, end: null, kind: 'run', status: 'running', resumed: true })
      } else {
        const iteration = num(d.iteration) ?? next.book.loopIter
        n.segments.push({
          start: at, end: null, kind: 'run', status: 'running',
          ...(iteration != null ? { iteration } : {}),
        })
        n.count += 1
        n.startedAt = at
        n.endedAt = undefined
        n.lastDurationMs = undefined
        n.error = undefined
        n.toolsRunning = 0
        if (num(d.iteration) != null) n.iteration = num(d.iteration)
        book().dispatchFrom[nodeId] = at
      }
      n.state = 'running'
      if (next.book.lastFailed) book().lastFailed = undefined
      if (prevPhase === 'idle' || prevPhase === 'queued') {
        if (next.startedAt == null) next.startedAt = at
        openDrive()
      }
      setPhase(phaseStep.phase)
      break
    }

    case 'human.requested': {
      // 右栏在这一刻就把宿主节点标成"等你"，画布同一时刻变。并行时 run.interrupted
      // 要等同一超步里别的分支都跑完才来，只认它的话，右栏已经在说等你确认，
      // 画布上这个节点还在转、入边的光点还在往里流。相位不动：流水线还没停
      const key = String(d.node_id ?? nodeId ?? '_')
      if (next.book.asking[key]) break
      book().asking[key] = true
      if (!nodeId || next.nodes[nodeId]?.state !== 'running') break
      const n = node(nodeId)
      const open = openIndex(n.segments, 'run')
      if (open >= 0) patchSeg(n, open, { end: at, status: 'waiting' })
      if (openIndex(n.segments, 'wait') < 0) {
        n.segments.push({ start: at, end: null, kind: 'wait', status: 'waiting' })
      }
      n.state = 'waiting'
      break
    }

    case 'human.resolved':
      if (next.book.asking[String(nodeId ?? '_')]) delete book().asking[String(nodeId ?? '_')]
      break

    case 'node.finished':
    case 'node.failed': {
      if (!nodeId) break
      const n = node(nodeId)
      const failed = type === 'node.failed'
      // 节点跑完了，决定已经生效（兜底 human.resolved 没发出来的老运行）
      if (!failed && next.book.asking[nodeId]) delete book().asking[nodeId]
      const ms = num(d.duration_ms)
      const open = openIndex(n.segments, 'run')
      const decision = !failed && d.preview && typeof d.preview === 'object'
        ? d.preview.__decision__ : undefined
      // 人工审批的通过 / 驳回只在这里；分支、循环在 edge.taken 里已经记过同一个值
      if (decision != null) recordTaken(n, String(decision))
      // 循环一轮跑完、下一步回到 body：容器还没完，不能打勾
      const tickHandle = open >= 0 ? n.segments[open].handle : undefined
      const looping = !failed && tickHandle === 'body'
      let end = at
      if (open >= 0) {
        const seg = n.segments[open]
        if (!tsOk && ms != null) end = seg.start + ms
        patchSeg(n, open, { end, status: failed ? 'failed' : 'done' })
      }
      // 容器里还开着的成员、调度段：节点都结束了，它们不可能还在跑
      n.segments.forEach((s, i) => {
        if (s.end == null && (s.kind === 'member' || s.kind === 'dispatch')) {
          patchSeg(n, i, { end, status: failed ? 'cancelled' : 'done' })
        }
      })
      if (!tsOk) at = dr.at = Math.max(at, end)
      n.endedAt = end
      n.lastDurationMs = ms ?? (open >= 0 ? Math.max(0, end - n.segments[open].start) : undefined)
      n.toolsRunning = 0
      n.looping = looping
      n.state = failed ? 'failed' : looping ? 'running' : 'done'
      if (failed) {
        n.error = String(d.error ?? '')
        book().lastFailed = nodeId
      } else {
        n.attempt = num(d.attempt) ?? n.attempt
      }
      break
    }

    case 'node.skipped': {
      if (!nodeId) break
      const n = node(nodeId)
      n.segments.push({ start: at, end: at, kind: 'run', status: 'skipped' })
      n.state = 'skipped'
      n.skippedReason = d.reason ? String(d.reason) : undefined
      break
    }

    case 'edge.taken': {
      if (!nodeId) break
      const n = node(nodeId)
      const handle = String(d.branch ?? '')
      recordTaken(n, handle)
      n.reason = d.reason ? String(d.reason) : undefined
      const open = openIndex(n.segments, 'run')
      const cursor = num(d.iteration)
      if (cursor != null) {
        // 循环。iteration 是后端的游标，从 0 数：走 body 时它是这一轮的序号，
        // 走 done 时它等于已经跑完的轮数——那一次不能再 +1，否则 5 项跑完显示第 6 轮
        const total = num(d.total)
        if (total != null) n.iterTotal = total
        if (handle === 'body') {
          n.iteration = cursor + 1
          n.loopDone = false
          book().loopIter = cursor + 1
          if (open >= 0) patchSeg(n, open, { handle, iteration: cursor + 1 })
        } else {
          n.loopDone = true
          // 循环之后开跑的节点不属于任何一轮，别给它们挂上最后那一轮的标号
          book().loopIter = undefined
          if (open >= 0) patchSeg(n, open, { handle })
        }
      } else if (open >= 0) {
        patchSeg(n, open, { handle })
      }
      break
    }

    case 'tool.start':
      if (nodeId) {
        const n = node(nodeId)
        n.tools += 1
        n.toolsRunning += 1
      }
      break

    case 'tool.end':
    case 'tool.error':
      if (nodeId && next.nodes[nodeId]?.toolsRunning) node(nodeId).toolsRunning -= 1
      break

    case 'llm.start':
      if (nodeId && d.model) node(nodeId).model = String(d.model)
      break

    case 'llm.end': {
      const inTok = num(d.input_tokens) ?? 0
      const outTok = num(d.output_tokens) ?? 0
      const cost = num(d.cost_usd) ?? 0
      next.tokensIn += inTok
      next.tokensOut += outTok
      next.costUsd += cost
      if (nodeId) {
        const n = node(nodeId)
        n.tokensIn += inTok
        n.tokensOut += outTok
        n.costUsd += cost
        if (d.model) n.model = String(d.model)
      }
      break
    }

    case 'agent.step.start': {
      if (!nodeId) break
      const n = node(nodeId)
      const round = num(d.round)
      n.segments.push({
        start: at, end: null, kind: 'member', status: 'running', agent: String(d.agent ?? ''),
        ...(round != null ? { iteration: round + 1 } : {}),
      })
      break
    }

    case 'agent.step.end': {
      if (!nodeId) break
      const n = node(nodeId)
      const i = openIndex(n.segments, 'member', String(d.agent ?? ''))
      if (i >= 0) {
        // 结束时刻用 duration 还原：成员交回和 end 事件发出之间可能隔着别人
        // （后端以前等整轮 gather 完才发 end），end 事件自己的 ts 会晚
        const ms = num(d.duration_ms)
        const start = n.segments[i].start
        patchSeg(n, i, {
          end: ms != null ? start + ms : at,
          status: d.error ? 'failed' : 'done',
        })
      }
      const b = book()
      b.dispatchFrom[nodeId] = Math.max(b.dispatchFrom[nodeId] ?? at, at)
      break
    }

    case 'agent.route.start': {
      if (!nodeId) break
      const n = node(nodeId)
      book().exactDispatch[nodeId] = true
      const round = num(d.round)
      // 有了精确的调度段，之前推算的那几段作废
      n.segments = n.segments.filter((s) => !(s.kind === 'dispatch' && s.estimated))
      n.segments.push({
        start: at, end: null, kind: 'dispatch', status: 'running',
        ...(round != null ? { iteration: round + 1 } : {}),
      })
      break
    }

    case 'agent.route.end': {
      if (!nodeId) break
      const n = node(nodeId)
      const i = openIndex(n.segments, 'dispatch')
      if (i >= 0) {
        const ms = num(d.duration_ms)
        patchSeg(n, i, { end: ms != null && !tsOk ? n.segments[i].start + ms : at, status: 'done' })
      }
      break
    }

    case 'log': {
      const ending = endingOf(ev, t.book.awaiting)
      if (ending) {
        settle(dr, ending)
        break
      }
      if (!nodeId) break
      if (d.code === 'node_retry') {
        const n = node(nodeId)
        n.segments.push({ start: at, end: at, kind: 'retry', status: 'running' })
        n.attempt = (n.attempt ?? 1) + 1
        break
      }
      // 带 round 的 info 日志是调度决策落地：从上一个时刻到这里是调度者在想。
      // 推算值，标 estimated；后端发了 agent.route.* 就用精确的
      if (String(d.level ?? 'info') === 'info' && d.round != null
          && !next.book.exactDispatch[nodeId]) {
        const from = next.book.dispatchFrom[nodeId]
        const round = num(d.round)
        if (from != null && next.nodes[nodeId]?.state === 'running') {
          node(nodeId).segments.push({
            start: Math.min(from, at), end: at, kind: 'dispatch', status: 'done', estimated: true,
            ...(round != null ? { iteration: round + 1 } : {}),
          })
        }
        book().dispatchFrom[nodeId] = at
      }
      break
    }

    case 'issuance': {
      const strs = (v: unknown): string[] =>
        Array.isArray(v) ? v.map((x) => (typeof x === 'string' ? x
          : String((x as any)?.token ?? (x as any)?.raw ?? JSON.stringify(x)))) : []
      const required = strs(d.missing_required)
      const unmatched = Array.isArray(d.unmatched) ? strs(d.unmatched) : undefined
      next.issuance = {
        tier: String(d.tier ?? ''),
        missing: [...required, ...strs(d.missing_expected)],
        missingRequired: required,
        ...(unmatched ? { unmatched } : {}),
        unmatchedCount: unmatched ? unmatched.length : num(d.unmatched) ?? 0,
      }
      break
    }

    case 'run.finished':
    case 'run.failed':
    case 'run.cancelled':
    case 'stream.end': {
      const ending = endingOf(ev, t.book.awaiting)
      if (!ending) break
      if (type === 'run.failed' || (type === 'stream.end' && ending.phase === 'failed')) {
        // 失败的是哪个节点：后端给了就用后端的；没给就是最后一个报错、之后没有
        // 新节点开跑的那个。超时这类没有节点报错的失败，两样都没有
        const culprit = d.error_node_id ?? d.node_id ?? nodeId ?? next.book.lastFailed
        if (culprit) {
          next.failedNodeId = String(culprit)
          const n = next.nodes[next.failedNodeId]
          if (n && (n.state === 'running' || n.state === 'waiting')) {
            const m = node(next.failedNodeId)
            const open = openIndex(m.segments, 'run')
            if (open >= 0) patchSeg(m, open, { end: at, status: 'failed' })
            m.state = 'failed'
            m.error = m.error || String(d.error ?? '')
            m.endedAt = at
          }
        }
      }
      settle(dr, ending)
      if (type === 'run.finished') {
        // 权威总数以后端累计为准：agent / 协作的调用老后端不发 llm.end，实时累加会少
        const u = d.usage ?? {}
        next.tokensIn = num(u.input_tokens) ?? next.tokensIn
        next.tokensOut = num(u.output_tokens) ?? next.tokensOut
        next.costUsd = num(u.cost_usd) ?? next.costUsd
      }
      const timing = d.timing
      if (timing && num(timing.wall_ms) != null) {
        next.timing = {
          wallMs: num(timing.wall_ms)!,
          activeMs: num(timing.active_ms) ?? num(timing.wall_ms)!,
          waitMs: num(timing.wait_ms) ?? 0,
        }
      }
      break
    }

    default:
      break
  }

  // 相位只有一个来源：nextPhase（decodePhase 用的也是它）。上面各分支管的是节点和时长
  setPhase(phaseStep.phase)
  if (at > next.book.clock) book().clock = at

  const parallel = parallelOf(next.nodes)
  const lastPar = next.parallelSeries[next.parallelSeries.length - 1]
  if ((lastPar ? lastPar[1] : 0) !== parallel) {
    next.parallelSeries = [...next.parallelSeries, [at, parallel]]
  }
  return next
}

function recordTaken(n: NodeTrace, handle: string): void {
  n.takenHandle = handle
  if (!n.taken?.includes(handle)) n.taken = [...(n.taken ?? []), handle]
}

/**
 * 终态收尾 + 推导 blocked / unreached。
 *
 * foldEvent 在终态事件到达时已经把进行中的收掉了；这里再兜一次底（终态事件
 * 之后又漏进来的段），然后对照图把"没有事件"的节点分成：因为上游失败被堵住的
 * （blocked）和压根没走到的（unreached，分支落空、运行被取消）。
 *
 * 没到终态时只做推导（排队中），不收尾——运行还在进行，没法替它下结论。
 */
export function finalizeTrace(t: Trace, graph?: GraphLike): Trace {
  let out = t
  if (isSettled(t.phase)) {
    const lingering = Object.values(t.nodes).some((n) =>
      n.state === 'running' || n.state === 'queued' || n.state === 'waiting'
      || n.segments.some((s) => s.end == null))
    if (lingering) {
      const dr = draftOf(t, t.endedAt ?? t.book.clock)
      settle(dr, endingFor(t.phase))
      out = dr.next
    }
  }
  if (graph) {
    out = applyDerived(out, graph)
    if (out.nodesTotal == null && graph.nodes.length) out = { ...out, nodesTotal: graph.nodes.length }
  }
  return out
}

/** 已经处在某个结局时，还没收干净的东西该收成什么（和 endingOf 同一套规则） */
function endingFor(phase: RunPhase): Ending {
  return endingOf({ seq: 0, type: 'stream.end', node_id: null, ts: 0, data: { status: phase } }, false)
    ?? { phase, running: 'cancelled', waiting: 'cancelled', drive: 'cancelled' }
}

/** 这一段结束后节点处在什么状态。循环跑完一轮回到 body 时容器还在进行中 */
function stateAfter(s: Segment): NodeState {
  if (s.kind === 'wait') return 'running'
  if (s.handle === 'body') return 'running'
  return s.status
}

function phaseAt(t: Trace, at: number): RunPhase {
  let phase: RunPhase | null = null
  for (const [ts, p] of t.phases) {
    if (ts > at) break
    phase = p
  }
  if (phase) return phase
  return t.phases.length ? 'queued' : t.phase
}

const clipped = (spans: [number, number | null][], at: number): number =>
  spans.reduce((acc, [s, e]) => (s > at ? acc : acc + Math.max(0, Math.min(e ?? at, at) - s)), 0)

/**
 * 把航迹投影到某一刻：实时时 at 取 liveAt(trace, now)，回放时取游标。
 *
 * 只折算 at 之前的部分。at 不早于最后一条事件时就是实时状态，直接读节点的
 * 当前状态（含推导出的排队中 / 被阻断 / 没走到）；早于它时按段重建。
 */
export function project(t: Trace, at: number): Projection {
  const live = at >= t.book.clock
  const phase = live ? t.phase : phaseAt(t, at)
  const authoritative = live && isTerminal(t.phase) && t.timing

  const end = t.endedAt != null && t.endedAt <= at ? t.endedAt : t.timed ? at : t.book.clock
  const elapsedMs = authoritative ? t.timing!.wallMs
    : t.startedAt == null ? 0 : Math.max(0, end - t.startedAt)
  const waitMs = authoritative ? t.timing!.waitMs : t.timed ? clipped(t.waits, at) : 0
  const activeMs = authoritative ? t.timing!.activeMs
    : t.timed ? clipped(t.drives, Math.min(at, end)) : elapsedMs

  const nodes: Projection['nodes'] = {}
  let nodesDone = 0
  for (const n of Object.values(t.nodes)) {
    let view: Projection['nodes'][string]
    if (live) {
      const open = n.segments[openIndex(n.segments, 'run')]
      view = {
        state: n.state,
        count: n.count,
        ...(n.iteration != null ? { iteration: n.iteration } : {}),
        ...(open && n.state === 'running'
          ? { elapsedMs: Math.max(0, (t.timed ? at : t.book.clock) - open.start) }
          : n.lastDurationMs != null ? { elapsedMs: n.lastDurationMs } : {}),
      }
    } else {
      let state: NodeState = 'idle'
      let count = 0
      let iteration: number | undefined
      let elapsedMs: number | undefined
      for (const s of n.segments) {
        if (s.start > at || s.kind === 'member' || s.kind === 'dispatch' || s.kind === 'retry') continue
        if (s.kind === 'wait') {
          state = s.end == null || s.end > at ? 'waiting' : stateAfter(s)
          continue
        }
        if (s.status === 'skipped' && s.end === s.start) { state = 'skipped'; continue }
        if (!s.resumed) count += 1
        if (s.iteration != null) iteration = s.iteration
        if (s.end == null || s.end > at) {
          state = 'running'
          elapsedMs = at - s.start
        } else {
          state = stateAfter(s)
          elapsedMs = s.end - s.start
        }
      }
      view = { state, count, ...(iteration != null ? { iteration } : {}),
               ...(elapsedMs != null ? { elapsedMs } : {}) }
    }
    if (view.state === 'done' || view.state === 'skipped') nodesDone += 1
    nodes[n.id] = view
  }

  let parallelNow = 0
  if (!(live && isSettled(t.phase))) {
    for (const [ts, v] of t.parallelSeries) {
      if (ts > at) break
      parallelNow = v
    }
  }

  return {
    phase, elapsedMs, activeMs, waitMs, nodesDone,
    nodesTotal: t.nodesTotal ?? Object.keys(t.nodes).length,
    parallelNow, nodes,
  }
}
