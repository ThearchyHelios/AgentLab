import { useStudio } from '../store/studio'
import { applyDerived, type GraphLike } from './derive'
import { project, type NodeState, type NodeTrace, type Projection, type Trace } from './trace'
import type { NodeRuntime, RunEvent } from '../types'

/** 某一刻为止的用量：实时是航迹上的累计，回放是那一刻的刻度 */
export interface NodeUsage {
  tokensIn: number
  tokensOut: number
  costUsd: number
  tools: number
  toolsRunning: number
}

const NO_USAGE: NodeUsage = { tokensIn: 0, tokensOut: 0, costUsd: 0, tools: 0, toolsRunning: 0 }

/**
 * 节点卡取状态的唯一入口。
 *
 * 卡片以前直接读 runtime[id].status，于是推导出来的几种（排队、被阻断、没走到）
 * 永远显示成"未运行"——runtime 里它们一律是 idle，只有航迹知道；回放时拖到
 * 某一刻，卡片也还是停在最后的样子。这里统一成：实时看航迹（没有航迹记录的
 * 退回 runtime），回放看 project(trace, replayAt) 在那一刻的投影，再和实时一样
 * 按图推导一遍（见 replayTraceCached）。
 *
 * runtime 上的正文、工具调用、协作矩阵照样给出去：那些是"内容"不是"状态"，
 * 航迹不存它们。回放时它们是最后的样子，卡片据此只在实时运行中显示流式正文。
 */
export interface NodeView {
  state: NodeState
  /** 在看回放（replayAt 不为 null）：计时用投影给的定值，不走时钟 */
  replay: boolean
  runtime: NodeRuntime | undefined
  trace: NodeTrace | undefined
  /** 执行了几次（循环、接着跑都会 > 1）。回放时是那一刻之前的次数 */
  count: number
  iteration?: number
  /** token 和工具次数。回放时是游标那一刻的读数，不拿终值冒充 */
  usage: NodeUsage
  /** 回放时这一刻的执行用时（进行中是已经用了多久，结束了是那一段的用时） */
  replayElapsedMs?: number
  /** 回放游标（毫秒时间戳）；实时为 null。卡片据此把计时、协作矩阵截到那一刻 */
  at: number | null
  /**
   * 循环容器在两轮之间：状态是 running，真正在跑的是循环体。卡片不转圈、不挂光弧。
   * 回放时按游标那一刻算——航迹上的 looping 是最后的值，跑完了就是 false
   */
  looping: boolean
}

/**
 * 同一个游标只投影一次。
 *
 * 几十张卡片各自 project 一遍整条航迹是 O(卡片数 × 事件数)，拖时间轴时每一帧
 * 都要来一次。所有卡片共用最近一次的结果：航迹引用和游标都没变就直接拿。
 */
let last: { trace: Trace; at: number; proj: Projection } | null = null

export function projectCached(trace: Trace, at: number): Projection {
  if (last && last.trace === trace && last.at === at) return last.proj
  const proj = project(trace, at)
  last = { trace, at, proj }
  return proj
}

/**
 * 回放游标那一刻的整份航迹：投影出的状态、那一刻为止走过的出口、循环容器是否
 * 在两轮之间，再按图推导排队 / 阻断 / 未到达。
 *
 * 卡片和画布（边、小地图）读同一份：只投影不推导的话，拖到半路时小地图上某个
 * 节点在排队、卡片却写着「未运行」。和 projectCached 一样只缓存最近一次，航迹、
 * 游标、图三样都没变就直接拿。
 */
let lastReplay: { trace: Trace; at: number; nodes: object; edges: object; out: Trace } | null = null

export function replayTraceCached(trace: Trace, at: number, graph: GraphLike): Trace {
  const hit = lastReplay
  if (hit && hit.trace === trace && hit.at === at && hit.nodes === graph.nodes && hit.edges === graph.edges) {
    return hit.out
  }
  const proj = projectCached(trace, at)
  const nodes: Record<string, NodeTrace> = {}
  for (const [id, n] of Object.entries(trace.nodes)) {
    const p = proj.nodes[id]
    const taken: string[] = []
    let looping = false
    for (const s of n.segments) {
      if (s.kind !== 'run' || s.start > at) continue
      if (s.handle && !taken.includes(s.handle)) taken.push(s.handle)
      looping = s.handle === 'body' && s.end != null && s.end <= at
    }
    const state = p?.state ?? 'idle'
    // 人工审批的通过 / 驳回只记在节点上，不在段上：它结束了就用节点上的
    const finished = state === 'done' || state === 'failed'
    const decided = taken.length ? taken : finished ? n.taken ?? [] : []
    nodes[id] = {
      ...n, state, count: p?.count ?? 0, iteration: p?.iteration, taken: decided,
      takenHandle: decided[decided.length - 1], looping: state === 'running' && looping,
    }
  }
  const out = applyDerived({ ...trace, phase: proj.phase, nodes }, graph)
  lastReplay = { trace, at, nodes: graph.nodes, edges: graph.edges, out }
  return out
}

export function useNodeView(id: string): NodeView {
  const runtime = useStudio((s) => s.runtime[id])
  const node = useStudio((s) => s.trace.nodes[id])
  // 拖时间轴时游标每帧都变，但投影本来就每帧重算、每张卡都会重渲染一次，
  // 多订阅这一个数不增加渲染次数
  const at = useStudio((s) => s.replayAt)
  // 投影里单个节点的那一项：同一份投影里引用稳定，别的卡片动了不会连带这张重渲染
  const projected = useStudio((s) => (s.replayAt == null ? undefined
    : projectCached(s.trace, s.replayAt).nodes[id]))
  const derived = useStudio((s) => (s.replayAt == null ? undefined
    : replayTraceCached(s.trace, s.replayAt, { nodes: s.nodes, edges: s.edges }).nodes[id]))

  if (at != null) {
    return {
      state: derived?.state ?? projected?.state ?? 'idle',
      replay: true,
      runtime,
      trace: node,
      count: projected?.count ?? 0,
      iteration: projected?.iteration,
      usage: projected ?? NO_USAGE,
      replayElapsedMs: projected?.elapsedMs,
      at,
      looping: !!derived?.looping,
    }
  }
  return {
    state: node?.state ?? runtime?.status ?? 'idle',
    replay: false,
    runtime,
    trace: node,
    count: node?.count ?? 0,
    iteration: node?.iteration ?? runtime?.iteration,
    usage: node ?? NO_USAGE,
    at: null,
    looping: !!node?.looping,
  }
}

// -------------------------------------------------------------------------
// 节点上的几件"事"：航迹只记状态和用量，这些要从事件里捞
// -------------------------------------------------------------------------

/** 一次工具调用的起止。时刻都是毫秒，和航迹同一条时间轴 */
export interface ToolSpan {
  tool: string
  agent?: string
  callId?: string
  start: number
  end?: number
  /** 后端声明的时限（tool.start.timeout_s）。没声明的工具不掐时 */
  limitS?: number
  ok?: boolean
  timedOut?: boolean
}

/** 某件事什么时候发生、原话怎么说 */
export interface FactMark {
  at: number
  message: string
}

/**
 * 模型把工具调用写成了文字（log code=tool_markup_leak）。后端在两种时候发它：
 * 写成文字被提醒、重答了（nudge，「…已提醒它重试一次」），成果是重答的那一次；
 * 步数用完后的收尾轮还想要工具（settle，「…收尾轮仍想调用工具」），交出来的是
 * 它前面写的内容，可能不完整。两件事对成果的意思不一样，卡片分开说
 */
export interface MarkupMark extends FactMark {
  settle: boolean
}

/**
 * 协作团队的收尾判定：轮数用完之后调度者只判定、不派活的那一次决定。它不是新的
 * 一轮，矩阵不给它加一列。at 是开始判定（agent.route.start），end 是给出结论
 */
export interface ClosingMark {
  at: number
  end?: number
  round: number
  done: boolean
  reason: string
}

/** 协作成员失败（agent.step.end 的 failed / error） */
export interface MemberFail {
  at: number
  agent: string
  error: string
}

/**
 * 卡片要说清的几件事。它们都藏在事件里：log 的 code、agent.route 的 closing、
 * tool.start 的 timeout_s——航迹不存，runtime 也不存。按节点归好，卡片直接取。
 *
 * 一次运行里同一个节点会执行好几次（循环体每一轮、失败后接着跑），每件事都按
 * 先后留着：卡片只说游标所在的那一次执行里发生的（见 execStart、lastIn），上一次
 * 用完轮数、收尾时还想调工具，不能挂到这一次干干净净的结果上
 */
export interface NodeFacts {
  /** 整次运行里的工具调用，按开始先后。回放按游标截 */
  calls: ToolSpan[]
  /**
   * 最近一次执行从什么时候开始（审批恢复的重放不算新的一次，接着跑算）。
   * 卡片取执行的起点用 execStart：它和卡片上的执行次数同出航迹，回放也认得
   */
  since: number
  /** 在等审批：这之后再来的 node.started 是恢复重放，不是新的一次执行 */
  paused?: boolean
  /** 下面三个是整次运行里最后的那一次，给只看整次运行的地方；卡片用对应的列表 */
  markup?: MarkupMark
  exhausted?: FactMark
  invented?: FactMark
  /** 模型把工具调用写成了文字，每次都留着 */
  markups: MarkupMark[]
  /** 协作团队轮数用完、按降档交付（log code=team_exhausted） */
  exhausts: FactMark[]
  /** 修复被拒：结果里出现了原文没有的值（log code=repair_invented） */
  inventions: FactMark[]
  closings: ClosingMark[]
  memberErrors: MemberFail[]
  /** 每一次失败的报错（node.failed）。回放到更早的一次失败时取那一次的原话 */
  failures: FactMark[]
  /** 校验修复调模型的时刻（llm.end purpose=repair），按先后 */
  repairs: number[]
}

const EMPTY_FACTS: NodeFacts = {
  calls: [], since: 0, markups: [], exhausts: [], inventions: [], closings: [], memberErrors: [], failures: [], repairs: [],
}

const tsOf = (e: RunEvent): number | undefined =>
  (typeof e.ts === 'number' && Number.isFinite(e.ts) ? e.ts * 1000 : undefined)

/**
 * 事件落到哪个节点上。等审批的两条有时不带 node_id，节点写在 data 里
 */
function factNode(e: RunEvent, d: Record<string, any>): string | undefined {
  if (e.node_id) return e.node_id
  if (e.type === 'run.interrupted' && d.payload?.node_id) return String(d.payload.node_id)
  if (e.type === 'human.requested' && d.node_id) return String(d.node_id)
  return undefined
}

/** clock：没带时间戳的事件记在最近一个时间戳上，和航迹的时间轴一致 */
function foldFacts(map: Record<string, NodeFacts>, e: RunEvent, clock: number): void {
  const d = (e.data ?? {}) as Record<string, any>
  const id = factNode(e, d)
  if (!id) return
  const at = tsOf(e) ?? clock
  const cur = map[id] ?? EMPTY_FACTS
  let next: NodeFacts | null = null
  switch (e.type) {
    case 'node.started':
      // 和航迹同一条规则：只有等审批之后的那次重放接着算同一次执行。接着跑时
      // 后端也给失败的节点标 resumed，但那是重新执行，上一次的事不再算它的
      next = cur.paused ? { ...cur, paused: false } : { ...cur, since: at }
      break
    case 'human.requested':
    case 'run.interrupted':
      if (!cur.paused) next = { ...cur, paused: true }
      break
    case 'node.finished':
    case 'node.skipped':
      if (cur.paused) next = { ...cur, paused: false }
      break
    case 'node.failed':
      next = {
        ...cur, paused: false,
        failures: [...cur.failures, { at, message: typeof d.error === 'string' ? d.error : '' }],
      }
      break
    case 'tool.start': {
      const limit = Number(d.timeout_s)
      next = {
        ...cur,
        calls: [...cur.calls, {
          tool: String(d.tool ?? ''), start: at,
          ...(d.agent != null ? { agent: String(d.agent) } : {}),
          ...(d.call_id != null ? { callId: String(d.call_id) } : {}),
          ...(Number.isFinite(limit) && limit > 0 ? { limitS: limit } : {}),
        }],
      }
      break
    }
    case 'tool.end':
    case 'tool.error': {
      // 有 call_id 按它配；老事件没有就按「同一个成员、同名、还开着的最后一次」
      const tool = String(d.tool ?? '')
      let i = -1
      for (let k = cur.calls.length - 1; k >= 0; k -= 1) {
        const c = cur.calls[k]
        if (c.end != null) continue
        if (d.call_id != null ? c.callId === String(d.call_id)
          : c.tool === tool && (d.agent == null || c.agent === String(d.agent))) { i = k; break }
      }
      if (i < 0) break
      const calls = [...cur.calls]
      calls[i] = { ...calls[i], end: at, ok: e.type === 'tool.end', ...(d.timed_out ? { timedOut: true } : {}) }
      next = { ...cur, calls }
      break
    }
    case 'log': {
      const message = String(d.message ?? '')
      if (d.code === 'tool_markup_leak') {
        const mark = { at, message, settle: /收尾/.test(message) }
        next = { ...cur, markup: mark, markups: [...cur.markups, mark] }
      } else if (d.code === 'team_exhausted') {
        next = { ...cur, exhausted: { at, message }, exhausts: [...cur.exhausts, { at, message }] }
      } else if (d.code === 'repair_invented') {
        next = { ...cur, invented: { at, message }, inventions: [...cur.inventions, { at, message }] }
      }
      break
    }
    case 'agent.route.start':
      if (d.closing) {
        next = { ...cur, closings: [...cur.closings, { at, round: Number(d.round) || 0, done: false, reason: '' }] }
      }
      break
    case 'agent.route.end':
      if (d.closing) {
        // 收掉开着的那一次；老后端没发 route.start 时起止记成同一刻
        const last = cur.closings[cur.closings.length - 1]
        const open = last && last.end == null ? last : undefined
        const mark: ClosingMark = {
          at: open?.at ?? at, end: at, round: Number(d.round) || 0, done: d.done === true, reason: String(d.reason ?? ''),
        }
        next = { ...cur, closings: open ? [...cur.closings.slice(0, -1), mark] : [...cur.closings, mark] }
      }
      break
    case 'agent.step.end':
      if (d.failed || d.error) {
        const agent = String(d.agent ?? '')
        const error = typeof d.error === 'string' && d.error.trim() ? d.error.trim() : '此步骤失败，未返回原因'
        next = { ...cur, memberErrors: [...cur.memberErrors, { at, agent, error }] }
      }
      break
    case 'llm.end':
      if (d.purpose === 'repair') next = { ...cur, repairs: [...cur.repairs, at] }
      break
  }
  if (next) map[id] = next
}

/**
 * 事件只会往后追加：上一次折到第几条就从第几条接着折，每来一条只做一条的事。
 * 没变的节点沿用原对象，卡片的选择器拿到的引用不变，不会跟着别的节点重渲染
 */
let factsMemo: { events: RunEvent[]; map: Record<string, NodeFacts>; clock: number } | null = null

export function factsOf(events: RunEvent[]): Record<string, NodeFacts> {
  const memo = factsMemo
  if (memo && memo.events === events) return memo.map
  const n = memo?.events.length ?? 0
  const appended = !!memo && events.length >= n && (n === 0 || events[n - 1] === memo.events[n - 1])
  const map: Record<string, NodeFacts> = appended ? { ...memo!.map } : {}
  let clock = appended ? memo!.clock : 0
  for (let i = appended ? n : 0; i < events.length; i += 1) {
    const ts = tsOf(events[i])
    if (ts != null && ts > clock) clock = ts
    foldFacts(map, events[i], clock)
  }
  factsMemo = { events, map, clock }
  return map
}

export function useNodeFacts(id: string): NodeFacts | undefined {
  return useStudio((s) => factsOf(s.events)[id])
}

/**
 * 卡片在说的那一次执行从什么时候开始：游标那一刻（实时是最后）所在的那一次。
 *
 * 取航迹上最后一段已经开始的执行段——卡片头上的「×N」、回放时的状态都出自航迹，
 * 事实跟着同一个划分走，才不会说成两次执行。审批恢复接回的段（resumed）和中断前
 * 是同一次，不算起点；接着跑、循环的下一轮是新的一次。航迹里没有这个节点时退回
 * 事件里记的起点，回放时就不截
 */
export function execStart(n: NodeTrace | undefined, at: number | null, facts?: NodeFacts): number {
  const segs = n?.segments ?? []
  for (let i = segs.length - 1; i >= 0; i -= 1) {
    const s = segs[i]
    if (s.kind !== 'run' || s.resumed || (at != null && s.start > at)) continue
    return s.start
  }
  return at == null ? facts?.since ?? -Infinity : -Infinity
}

/** 从 since 到游标那一刻（实时到最后）之间的那几件，按先后 */
export function within<T extends { at: number }>(list: readonly T[] | undefined, at: number | null, since = -Infinity): T[] {
  return (list ?? []).filter((x) => x.at >= since && (at == null || x.at <= at))
}

/** 从 since 到游标那一刻之间最后的那一件 */
export function lastIn<T extends { at: number }>(list: readonly T[] | undefined, at: number | null, since = -Infinity): T | undefined {
  const all = list ?? []
  for (let i = all.length - 1; i >= 0; i -= 1) {
    const x = all[i]
    if (at != null && x.at > at) continue
    return x.at >= since ? x : undefined
  }
  return undefined
}

/**
 * 某一刻在跑的那次工具调用（回放给游标，实时给 null）。多个同时开着时取最后开的，
 * 卡片槽里只放得下一个名字。since 是这一次执行的起点：上一次撒手没收尾的调用不算
 */
export function openCall(facts: NodeFacts | undefined, at: number | null,
                         since = at == null ? facts?.since ?? 0 : -Infinity): ToolSpan | undefined {
  const calls = facts?.calls ?? []
  for (let i = calls.length - 1; i >= 0; i -= 1) {
    const c = calls[i]
    if (c.start < since) continue
    if (at == null ? c.end == null : c.start <= at && (c.end == null || c.end > at)) return c
  }
  return undefined
}

/** 这一次执行里校验修复了几次。回放时数到游标为止 */
export function repairsOf(facts: NodeFacts | undefined, at: number | null,
                          since = at == null ? facts?.since ?? 0 : -Infinity): number {
  return (facts?.repairs ?? []).filter((t) => t >= since && (at == null || t <= at)).length
}

/**
 * 某件事在游标那一刻发生了没有（实时一律算发生了）。给了 since 就只认这一次执行里的。
 * 这里只有最后那一件：回放到更早的一次执行时它可能还没发生，要逐次看就用 lastIn
 */
export const happened = <T extends { at: number }>(fact: T | undefined, at: number | null, since = -Infinity): T | undefined =>
  (fact && (at == null || fact.at <= at) && fact.at >= since ? fact : undefined)

/**
 * 最后一段还开着的某类段从什么时候开始。实时计时从这里起算。
 * 给了回放游标就只看那一刻：那时已经开始、还没结束的段
 */
export function openSince(n: NodeTrace | undefined, kind: 'run' | 'wait', at?: number | null): number | undefined {
  if (!n) return undefined
  for (let i = n.segments.length - 1; i >= 0; i -= 1) {
    const s = n.segments[i]
    if (s.kind !== kind) continue
    if (at == null) {
      if (s.end == null) return s.start
      continue
    }
    if (s.start > at) continue
    return s.end == null || s.end > at ? s.start : undefined
  }
  return undefined
}

/**
 * 循环容器这一次进入是从什么时候开始的。
 *
 * 循环节点每一轮都会重新 node.started，startedAt 只是这一轮的起点；容器的计时
 * 要从这一次进入的第一轮算起。往回找到上一次走 done 出口的那一段为止——嵌套
 * 在外层循环里时，外层每进来一次，内层都是新的一次。
 */
export function loopSince(n: NodeTrace | undefined, at?: number | null): number | undefined {
  if (!n) return undefined
  let since: number | undefined
  for (let i = n.segments.length - 1; i >= 0; i -= 1) {
    const s = n.segments[i]
    if (s.kind !== 'run' || (at != null && s.start > at)) continue
    if (s.handle === 'done' && since != null) break
    since = s.start
  }
  return since
}

/** 循环容器这一次进入到走 done 出口一共用了多久（回放时截到游标那一刻） */
export function loopSpan(n: NodeTrace | undefined, at?: number | null): number | undefined {
  const since = loopSince(n, at)
  if (!n || since == null) return undefined
  let end: number | undefined
  for (const s of n.segments) {
    if (s.kind !== 'run' || s.start < since || (at != null && s.start > at)) continue
    if (s.end != null && (at == null || s.end <= at)) end = s.end
  }
  return end != null ? end - since : undefined
}
