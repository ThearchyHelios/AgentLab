import { useStudio } from '../store/studio'
import { applyDerived, type GraphLike } from './derive'
import { project, type NodeState, type NodeTrace, type Projection, type Trace } from './trace'
import type { NodeRuntime } from '../types'

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
    at: null,
    looping: !!node?.looping,
  }
}

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
