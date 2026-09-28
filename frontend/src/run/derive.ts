import type { NodeState, NodeTrace, Trace } from './trace'

/**
 * 图结构上的推导：拓扑序、哪些边真的走过、哪些节点在排队 / 被阻断 / 没走到。
 *
 * 和 trace.ts 分开：航迹只认事件，这里只认图。"没亮的那几个节点"光看事件分不
 * 出是还没轮到、被分支绕开了，还是因为上游失败被堵住了——它们都只是"没有事件"。
 * 三种情况要做的事完全不同，所以得对照图推出来。
 *
 * 规则跟后端编译器对齐（compiler.py）：
 * - 回边从入口起 DFS 认，和 back_edges 一致。循环体连回循环节点的那条不算依赖。
 * - 路由节点（分支、循环、带通过/驳回出口的人工审批）只放行它选中的那个出口；
 *   选中的出口没连线时走 default。其余节点的出边全部放行。
 * - 有两个以上上游的节点是 defer 汇合：等其余任务都跑完再跑一次，没走的那路不等。
 */

export interface GraphLike {
  nodes: { id: string; type?: string; data?: { nodeType?: string; config?: Record<string, any> } }[]
  edges: { id?: string; source: string; target: string; sourceHandle?: string | null }[]
}

type GraphEdgeLike = GraphLike['edges'][number]

export interface Topology {
  /** 去掉回边后的拓扑序 */
  order: string[]
  /** 拓扑层级：入口是 0，往下每层 +1（取最长路径）。一次性时刻按它错开 */
  rank: Record<string, number>
  /** 回边的 edgeKey */
  back: Set<string>
  entries: string[]
}

export const edgeKey = (e: GraphEdgeLike): string =>
  `${e.source}|${e.target}|${e.sourceHandle ?? ''}`

/** 画布上的边都有 id；GraphSpec 里的边可能没有，退到 edgeKey */
export const edgeIdOf = (e: GraphEdgeLike): string => e.id ?? edgeKey(e)

/**
 * 节点类型。画布上的 FlowNode 的 type 是 React Flow 的渲染器键（一律 'card'），
 * 真正的类型在 data.nodeType；GraphSpec 的节点没有 data.nodeType，type 就是类型
 */
const typeOf = (n: GraphLike['nodes'][number]): string | undefined => n.data?.nodeType ?? n.type

const topoCache = new WeakMap<object, { nodes: object; topo: Topology }>()

/**
 * 拓扑序与回边。
 *
 * 缓存按边数组的引用：运行期间图不变，每条事件都重算一遍没有必要；
 * 改图会换数组，缓存自然失效。
 */
export function topology(graph: GraphLike): Topology {
  const hit = topoCache.get(graph.edges)
  if (hit && hit.nodes === graph.nodes) return hit.topo

  const ids = graph.nodes.map((n) => n.id)
  const known = new Set(ids)
  const edges = graph.edges.filter((e) => known.has(e.source) && known.has(e.target))
  const hasIncoming = new Set(edges.map((e) => e.target))
  const inputs = graph.nodes.filter((n) => typeOf(n) === 'input').map((n) => n.id)
  const entries = inputs.length ? inputs : ids.filter((id) => !hasIncoming.has(id))

  const adj = new Map<string, GraphEdgeLike[]>()
  for (const e of edges) {
    const list = adj.get(e.source)
    if (list) list.push(e)
    else adj.set(e.source, [e])
  }

  // 迭代式 DFS（和后端 back_edges 同一个算法）：几百个节点的图不该因为递归深度出事
  const WHITE = 0, GREY = 1, BLACK = 2
  const color = new Map(ids.map((id) => [id, WHITE]))
  const back = new Set<string>()
  for (const root of [...entries, ...ids]) {
    if (color.get(root) !== WHITE) continue
    const stack: [string, number][] = [[root, 0]]
    color.set(root, GREY)
    while (stack.length) {
      const top = stack[stack.length - 1]
      const out = adj.get(top[0]) ?? []
      if (top[1] >= out.length) {
        color.set(top[0], BLACK)
        stack.pop()
        continue
      }
      const e = out[top[1]]
      top[1] += 1
      const c = color.get(e.target)
      if (c === GREY) back.add(edgeKey(e))
      else if (c === WHITE) {
        color.set(e.target, GREY)
        stack.push([e.target, 0])
      }
    }
  }

  const indeg = new Map(ids.map((id) => [id, 0]))
  for (const e of edges) if (!back.has(edgeKey(e))) indeg.set(e.target, (indeg.get(e.target) ?? 0) + 1)
  const rank: Record<string, number> = {}
  const queue = ids.filter((id) => !indeg.get(id))
  queue.forEach((id) => { rank[id] = 0 })
  const order: string[] = []
  while (queue.length) {
    const id = queue.shift()!
    order.push(id)
    for (const e of adj.get(id) ?? []) {
      if (back.has(edgeKey(e))) continue
      rank[e.target] = Math.max(rank[e.target] ?? 0, rank[id] + 1)
      const left = (indeg.get(e.target) ?? 0) - 1
      indeg.set(e.target, left)
      if (left === 0) queue.push(e.target)
    }
  }
  // 去掉回边后必然无环；万一有漏网的，按原顺序补在后面，不丢节点
  for (const id of ids) if (!order.includes(id)) { order.push(id); rank[id] ??= 0 }

  const topo = { order, rank, back, entries }
  topoCache.set(graph.edges, { nodes: graph.nodes, topo })
  return topo
}

const continueOnError = (n: GraphLike['nodes'][number] | undefined): boolean =>
  n?.data?.config?.on_error === 'continue'

/**
 * 这个节点是不是按出口路由的。和 compiler.py 的 needs_routing 一致：分支、循环，
 * 以及出口里有通过/驳回的人工审批。图里没带类型时，看它做过的决定能不能对上
 * 某个出口。
 */
function isRouting(node: GraphLike['nodes'][number] | undefined, outHandles: Set<string>,
                   n: NodeTrace | undefined): boolean {
  const type = node ? typeOf(node) : undefined
  if (type === 'branch' || type === 'loop') return true
  if (type === 'human') return outHandles.has('approved') || outHandles.has('rejected')
  if (type) return false
  const decision = n?.takenHandle
  return decision != null && (outHandles.has(decision) || outHandles.has('default'))
}

interface Ctx {
  byId: Map<string, GraphLike['nodes'][number]>
  out: Map<string, GraphEdgeLike[]>
  handles: Map<string, Set<string>>
}

const ctxCache = new WeakMap<object, { nodes: object; ctx: Ctx }>()

function contextOf(graph: GraphLike): Ctx {
  const hit = ctxCache.get(graph.edges)
  if (hit && hit.nodes === graph.nodes) return hit.ctx
  const byId = new Map(graph.nodes.map((n) => [n.id, n]))
  const out = new Map<string, GraphEdgeLike[]>()
  const handles = new Map<string, Set<string>>()
  for (const e of graph.edges) {
    const list = out.get(e.source)
    if (list) list.push(e)
    else out.set(e.source, [e])
    const set = handles.get(e.source) ?? new Set<string>()
    set.add(e.sourceHandle || 'default')
    handles.set(e.source, set)
  }
  const ctx = { byId, out, handles }
  ctxCache.set(graph.edges, { nodes: graph.nodes, ctx })
  return ctx
}

/** 出口 handle 是否放行：decision 是节点做的某一次决定（undefined = 没做决定） */
function handleOpen(e: GraphEdgeLike, routing: boolean, decision: string | undefined,
                    outHandles: Set<string>): boolean {
  if (!routing) return true
  const h = e.sourceHandle || 'default'
  // 路由节点没做出决定（被 skip_if 跳过）：路由函数拿到 None，只走 default
  if (decision == null) return h === 'default'
  if (h === decision) return true
  return h === 'default' && !outHandles.has(decision)
}

/** 源节点已经把数据送出来了：跑完、被跳过、容错失败，或者循环容器正放行 body */
function sourcePassed(n: NodeTrace | undefined, node: GraphLike['nodes'][number] | undefined): boolean {
  if (!n) return false
  if (n.looping) return true
  return n.state === 'done' || n.state === 'skipped'
    || (n.state === 'failed' && continueOnError(node))
}

/** 这条边此刻是否"已送达"：源已放行，且（路由节点的话）当前的决定选的就是它 */
export function edgeFired(trace: Trace, graph: GraphLike, e: GraphEdgeLike): boolean {
  const ctx = contextOf(graph)
  const src = trace.nodes[e.source]
  const node = ctx.byId.get(e.source)
  if (!sourcePassed(src, node)) return false
  const hs = ctx.handles.get(e.source) ?? new Set<string>()
  return handleOpen(e, isRouting(node, hs, src), src?.takenHandle, hs)
}

/**
 * 当前正在流动的边：目标此刻真的在执行、这条边确实送达了。
 *
 * 只算执行中的目标——指向等待审批节点的边不算：流程是停住的，边上还有光点
 * 往里流，看着像"数据在流"，和事实相反。循环容器在两轮之间也不算。
 */
export function activeEdgesOf(trace: Trace, graph: GraphLike): string[] {
  const out: string[] = []
  for (const e of graph.edges) {
    const target = trace.nodes[e.target]
    if (!target || target.state !== 'running' || target.looping) continue
    if (edgeFired(trace, graph, e)) out.push(edgeIdOf(e))
  }
  return out
}

/**
 * 停住的边：目标正停在审批上、这条边确实送达了。画布把它画成静止的闸门，
 * 和"正在流动"分开——流程在等人，不是在跑。
 */
export function heldEdgesOf(trace: Trace, graph: GraphLike): string[] {
  const out: string[] = []
  for (const e of graph.edges) {
    if (trace.nodes[e.target]?.state !== 'waiting') continue
    if (edgeFired(trace, graph, e)) out.push(edgeIdOf(e))
  }
  return out
}

const STARTED = new Set<NodeState>(['running', 'waiting', 'done', 'failed', 'skipped', 'cancelled', 'suspended'])

/**
 * 这次运行实际走过的边：源放行过这条出口（循环里任何一轮都算），目标也确实开跑过。
 * 跑完之后用它留下执行路径，而不是只点亮分支出口那一条。
 */
export function walkedEdges(trace: Trace, graph: GraphLike): Set<string> {
  const ctx = contextOf(graph)
  const walked = new Set<string>()
  for (const e of graph.edges) {
    const src = trace.nodes[e.source]
    const dst = trace.nodes[e.target]
    if (!src?.count && src?.state !== 'skipped') continue
    if (!dst || !(dst.count > 0 || STARTED.has(dst.state))) continue
    const hs = ctx.handles.get(e.source) ?? new Set<string>()
    const node = ctx.byId.get(e.source)
    const routing = isRouting(node, hs, src)
    const decisions = src.taken?.length ? src.taken : [undefined]
    if (decisions.some((dec) => handleOpen(e, routing, dec, hs))) walked.add(edgeIdOf(e))
  }
  return walked
}

const PENDING = new Set<NodeState>(['idle', 'queued', 'blocked', 'unreached'])

/**
 * 推导每个节点的状态，覆盖图上的全部节点（没有事件的也在）。
 *
 * - 终态（成功 / 失败 / 取消）：没跑过的节点，在某个失败节点下游的是 blocked
 *   （on_error=continue 的失败不往下传），其余是 unreached（分支落空、运行被
 *   停在半路）。
 * - 进行中：没跑过、但至少有一条入边已送达的是 queued——单入边的是马上要跑，
 *   汇合节点是在等另一路（defer：等其余任务跑完再跑一次）。
 * - 挂起 / 空闲：不推导。挂起的运行还能接着跑，这时候说"没走到"为时过早。
 */
export function deriveStates(trace: Trace, graph: GraphLike): Record<string, NodeState> {
  const ctx = contextOf(graph)
  const states: Record<string, NodeState> = {}
  for (const n of graph.nodes) states[n.id] = trace.nodes[n.id]?.state ?? 'idle'
  const phase = trace.phase

  if (phase === 'succeeded' || phase === 'failed' || phase === 'cancelled') {
    const blocked = new Set<string>()
    const seeds = graph.nodes.filter((n) =>
      trace.nodes[n.id]?.state === 'failed' && !continueOnError(n)).map((n) => n.id)
    // 顺着出边一路往下（含回边：循环体里失败了，循环的 done 出口之后也走不到了）。
    // 跑过的节点保持原样，只标没跑过的
    const queue = [...seeds]
    const seen = new Set(queue)
    while (queue.length) {
      const id = queue.shift()!
      for (const e of ctx.out.get(id) ?? []) {
        if (seen.has(e.target) || !(e.target in states)) continue
        seen.add(e.target)
        if (PENDING.has(states[e.target])) blocked.add(e.target)
        queue.push(e.target)
      }
    }
    for (const id of Object.keys(states)) {
      if (PENDING.has(states[id])) states[id] = blocked.has(id) ? 'blocked' : 'unreached'
    }
    return states
  }

  if (phase === 'queued' || phase === 'running' || phase === 'waiting') {
    const incoming = new Map<string, GraphEdgeLike[]>()
    for (const e of graph.edges) {
      const list = incoming.get(e.target)
      if (list) list.push(e)
      else incoming.set(e.target, [e])
    }
    for (const id of Object.keys(states)) {
      if (states[id] !== 'idle' && states[id] !== 'queued') continue
      const fired = (incoming.get(id) ?? []).some((e) => edgeFired(trace, graph, e))
      states[id] = fired ? 'queued' : 'idle'
    }
    return states
  }

  for (const id of Object.keys(states)) if (states[id] === 'queued') states[id] = 'idle'
  return states
}

/**
 * 把推导结果写回航迹。只动推导类状态（idle / queued / blocked / unreached）
 * 之间的切换，事件给出的状态原样不动；没有变化时返回同一个引用。
 */
export function applyDerived(trace: Trace, graph: GraphLike): Trace {
  const states = deriveStates(trace, graph)
  let nodes: Record<string, NodeTrace> | null = null
  for (const [id, state] of Object.entries(states)) {
    const cur = trace.nodes[id]
    const before = cur?.state ?? 'idle'
    if (before === state || !PENDING.has(before) || !PENDING.has(state)) continue
    if (!cur && state === 'idle') continue
    nodes ??= { ...trace.nodes }
    nodes[id] = cur
      ? { ...cur, state }
      : { id, state, count: 0, tokensIn: 0, tokensOut: 0, costUsd: 0, tools: 0, toolsRunning: 0,
          segments: [] }
  }
  return nodes ? { ...trace, nodes } : trace
}
