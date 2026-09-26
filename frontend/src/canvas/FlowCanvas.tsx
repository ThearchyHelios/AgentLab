import './surface.css'
import {
  createContext, memo, useCallback, useContext, useEffect, useMemo, useRef, useState, type CSSProperties,
} from 'react'
import {
  Background, BackgroundVariant, ControlButton, Controls, MiniMap, ReactFlow, ReactFlowProvider,
  useReactFlow, useStoreApi, type AriaLabelConfig, type Edge, type MiniMapNodeProps, type Viewport,
} from '@xyflow/react'
import { Crosshair, Lock, Route as RouteIcon } from 'lucide-react'
import clsx from 'clsx'
import { FlowEdge, MomentContext, RoutingContext, type EdgeRunState, type EdgeView } from './FlowEdge'
import { NodeCard } from './NodeCard'
import { buildRoutes, NODE_WIDTH } from './routing'
import { RunTimeline, useDock } from './RunTimeline'
import { toast } from '../components/ui'
import { runClassLabel } from '../lib/terms'
import { shortId } from '../lib/format'
import {
  activeEdgesOf, applyDerived, edgeIdOf, edgeKey, heldEdgesOf, topology, walkedEdges, type GraphLike,
} from '../run/derive'
import {
  isActivePhase, isTerminal, type NodeState, type NodeTrace, type RunPhase, type Trace,
} from '../run/trace'
import { projectCached } from '../run/useNodeView'
import { useStudio, type FlowNode } from '../store/studio'
import type { NodeType } from '../types'

const nodeTypes = { card: NodeCard }
const edgeTypes = { flow: FlowEdge }

/** 读得清卡片字的最低缩放。低于它，12px 的标题落到 7px 以下，光弧也细到看不见 */
const READABLE_ZOOM = 0.6
/** 语义缩放三档的门槛，上下各留 0.02 的回滞，免得停在门槛上来回跳 */
const LOD_FULL = 0.6
const LOD_SIGNAL = 0.35
const HYSTERESIS = 0.02
/** 手动平移、缩放之后，跟随执行让出这么久 */
const FOLLOW_PAUSE_MS = 10_000
/** 一次性时刻挂在根元素上的时长：够卡片按层级错开播完，之后撤掉，免得后来的变化再播一遍 */
const MOMENT_MS = 2600

type Lod = 'full' | 'compact' | 'signal'

/**
 * cur 为 null 表示视口已经停稳：按落点直接分档，不带回滞。回滞只防"停在门槛上
 * 来回跳"，停稳了还带着它，从 0.5 取景回 0.6 就会一直卡在 compact（要 0.62 才回 full），
 * 同样的 60% 打开时是完整卡片、定位过去却只剩标题
 */
function lodFor(zoom: number, cur: Lod | null): Lod {
  const full = cur == null ? LOD_FULL - 1e-3 : cur === 'full' ? LOD_FULL - HYSTERESIS : LOD_FULL + HYSTERESIS
  const signal = cur == null ? LOD_SIGNAL : cur === 'signal' ? LOD_SIGNAL + HYSTERESIS : LOD_SIGNAL - HYSTERESIS
  return zoom >= full ? 'full' : zoom < signal ? 'signal' : 'compact'
}

const reducedMotion = (): boolean =>
  typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches

/** 系统「减少动效」的开关，跟着系统设置实时变 */
function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(reducedMotion)
  useEffect(() => {
    if (typeof matchMedia !== 'function') return
    const mq = matchMedia('(prefers-reduced-motion: reduce)')
    const sync = () => setReduced(mq.matches)
    mq.addEventListener('change', sync)
    return () => mq.removeEventListener('change', sync)
  }, [])
  return reduced
}

/**
 * 程序取景一律线性插值。d3 默认的 interpolateZoom 走"先拉远再推近"的弧线，
 * 半路会缩到 0.43–0.5，把画布切进 compact 档：定位到失败节点，落地时错误正文被藏了
 */
const LINEAR = 'linear' as const

// 「只看执行路径」看的是这一次运行的路径：清掉结果、开始下一次运行（含接着跑）时复位。
// 放在模块级而不是画布组件里：画布不在屏上时（去了别的页）清掉的结果也要复位
useStudio.subscribe((s, prev) => {
  if (s.runPhase === prev.runPhase || !useDock.getState().pathOnly) return
  if (s.runPhase === 'idle' || (isActivePhase(s.runPhase) && !isActivePhase(prev.runPhase))) {
    useDock.getState().set({ pathOnly: false })
  }
})

/** React Flow 自带控件和读屏描述默认是英文 */
const ARIA: Partial<AriaLabelConfig> = {
  'node.a11yDescription.default': '按回车或空格选中节点，按删除键删掉，Esc 取消。',
  'node.a11yDescription.keyboardDisabled': '按回车或空格选中节点，再用方向键移动，按删除键删掉，Esc 取消。',
  'node.a11yDescription.ariaLiveMessage': ({ x, y }: { direction: string; x: number; y: number }) =>
    `已移动选中的节点，新位置 x ${x}，y ${y}`,
  'edge.a11yDescription.default': '按回车或空格选中连线，再按删除键删掉，Esc 取消。',
  'controls.ariaLabel': '画布控件',
  'controls.zoomIn.ariaLabel': '放大',
  'controls.zoomOut.ariaLabel': '缩小',
  'controls.fitView.ariaLabel': '适配全图',
  'minimap.ariaLabel': '小地图',
  'handle.ariaLabel': '连接点',
}

/** 已经响应过的取景请求 */
let lastFocus: object | null = null

/** StudioState 的 getViewportCenter 由 W-studio 声明；这里只负责把实现挂上去 */
const setStudio = useStudio.setState as (patch: Record<string, unknown>) => void

// -------------------------------------------------------------------------
// 回放：把航迹投影到游标那一刻，边和小地图按那一刻的样子画
// -------------------------------------------------------------------------

function replayTrace(t: Trace, at: number, graph: GraphLike): Trace {
  const proj = projectCached(t, at)
  const nodes: Record<string, NodeTrace> = {}
  for (const [id, n] of Object.entries(t.nodes)) {
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
      ...n, state, count: p?.count ?? 0, taken: decided, takenHandle: decided[decided.length - 1],
      looping: state === 'running' && looping,
    }
  }
  return applyDerived({ ...t, phase: proj.phase, nodes }, graph)
}

/** 每条边此刻的运行态 */
function classifyEdges(
  trace: Trace, phase: RunPhase, graph: GraphLike, flowing: string[],
): Map<string, EdgeRunState> {
  const out = new Map<string, EdgeRunState>()
  if (phase === 'idle' && !Object.keys(trace.nodes).length) return out
  const flow = new Set(flowing)
  const held = new Set(heldEdgesOf(trace, graph))
  const walked = walkedEdges(trace, graph)
  const settled = isTerminal(phase)
  const stateOf = (id: string): NodeState => trace.nodes[id]?.state ?? 'idle'
  for (const e of graph.edges) {
    const id = edgeIdOf(e)
    const target = stateOf(e.target)
    const source = stateOf(e.source)
    let s: EdgeRunState = 'idle'
    if (flow.has(id)) s = 'flow'
    else if (held.has(id)) s = 'held'
    else if (target === 'blocked' && (source === 'failed' || source === 'blocked')) s = 'cut'
    else if (walked.has(id)) s = 'walked'
    else if (settled) s = 'unwalked'
    out.set(id, s)
  }
  return out
}

// -------------------------------------------------------------------------
// 小地图：有运行时按状态着色，失败和等人放大；没有运行时按类型色
// -------------------------------------------------------------------------

/**
 * states：每个节点此刻的状态（没有运行时为 null，按类型色画）。
 * unit：小地图上 1px 合多少画布单位。失败、等人的那一圈要按屏幕像素定大小——
 * 按节点尺寸定的话，70 个节点的长图里它会缩成不到 1px，正好在最需要的时候看不见
 */
const MiniStates = createContext<{ states: Record<string, NodeState>; unit: number } | null>(null)

const MINI_FILL: Partial<Record<NodeState, string>> = {
  running: 'var(--st-running)',
  waiting: 'var(--st-waiting)',
  failed: 'var(--st-failed)',
  done: 'color-mix(in srgb, var(--st-done) 70%, transparent)',
  skipped: 'var(--st-skipped)',
  cancelled: 'var(--st-cancelled)',
  suspended: 'var(--st-suspended)',
  blocked: 'var(--st-blocked)',
  unreached: 'color-mix(in srgb, var(--st-unreached) 45%, transparent)',
  queued: 'var(--st-queued)',
}

const MiniNode = memo(function MiniNode({ id, x, y, width, height, color, borderRadius, shapeRendering, className }: MiniMapNodeProps) {
  const mini = useContext(MiniStates)
  const state = mini?.states[id]
  const alert = state === 'failed' || state === 'waiting'
  const fill = mini ? MINI_FILL[state ?? 'idle'] ?? 'var(--border-strong)' : color
  // 圈：比节点每边大 3px，至少 12px 见方，线宽 2px（都是小地图上的屏幕像素）
  const u = mini?.unit ?? 1
  const rw = Math.max(width + 6 * u, 12 * u)
  const rh = Math.max(height + 6 * u, 12 * u)
  return (
    <>
      <rect className={clsx('react-flow__minimap-node', className, state && `sf-mm-${state}`)}
            x={x} y={y} rx={borderRadius} ry={borderRadius} width={width} height={height}
            style={{ fill }} shapeRendering={shapeRendering} />
      {/* 失败、等人在小地图上放大一圈：大图里它们是唯一需要人去处理的地方 */}
      {alert && (
        <rect className={clsx('sf-mm-ring', `sf-mm-${state}`)}
              x={x + width / 2 - rw / 2} y={y + height / 2 - rh / 2} width={rw} height={rh}
              rx={3 * u} ry={3 * u} style={{ strokeWidth: 2 * u }} />
      )}
    </>
  )
})

const typeColor = (n: { data?: unknown }): string => {
  const type = (n.data as { nodeType?: NodeType } | undefined)?.nodeType
  return type ? `var(--nt-${type})` : 'var(--text-faint)'
}

// -------------------------------------------------------------------------

function CanvasInner() {
  const rf = useRef<HTMLDivElement>(null)
  const { screenToFlowPosition, fitView, setViewport, setCenter, getViewport, getNodes, getNodesBounds,
    getInternalNode, zoomTo } = useReactFlow()
  const rfStore = useStoreApi()
  const nodes = useStudio((s) => s.nodes)
  const edges = useStudio((s) => s.edges)
  const activeEdges = useStudio((s) => s.activeEdges)
  const fitRequest = useStudio((s) => s.fitRequest)
  const trace = useStudio((s) => s.trace)
  const runPhase = useStudio((s) => s.runPhase)
  const runClass = useStudio((s) => s.trace.runClass ?? s.run?.run_class)
  const runVersion = useStudio((s) => s.run?.version ?? null)
  const replayAt = useStudio((s) => s.replayAt)
  const focusRequest = useStudio((s) => s.focusRequest)
  const selectedId = useStudio((s) => s.selectedId)
  const follow = useStudio((s) => s.follow)
  const pathOnly = useDock((s) => s.pathOnly)
  // 动作从 getState 取：整个 useStudio() 解构会订阅全部字段，每个 token 都让画布重渲染一次
  const actions = useStudio.getState()

  const graph = useMemo<GraphLike>(() => ({ nodes, edges }), [nodes, edges])
  const topo = useMemo(() => topology(graph), [graph])
  const hasRun = runPhase !== 'idle' || Object.keys(trace.nodes).length > 0
  const active = isActivePhase(runPhase)
  const reduced = useReducedMotion()
  // 正式运行期间画布只读：它跑的是已发布的不可变版本，此刻改图既改不了它，
  // 还会让事件对到已经被删掉的节点上。探索运行只关删除和连线，拖动照常
  const formalLock = active && runClass === 'formal'

  // 走线方案整张图算一次。节点尺寸要等 React Flow 量完才有，量到之后
  // nodes 会变，这里跟着重算，端口和车道就落到实测尺寸上。
  const routes = useMemo(() => buildRoutes(nodes, edges), [nodes, edges])

  // ---- 此刻（实时或回放游标那一刻）的航迹 ----
  const view = useMemo(() => {
    if (replayAt == null) return { trace, phase: runPhase, flowing: activeEdges }
    const t = replayTrace(trace, replayAt, graph)
    return { trace: t, phase: t.phase, flowing: activeEdgesOf(t, graph) }
  }, [trace, runPhase, activeEdges, replayAt, graph])

  const edgeStates = useMemo(
    () => classifyEdges(view.trace, view.phase, graph, view.flowing),
    [view, graph],
  )
  // 「只看执行路径」只在运行真正结束、看的也是结束那一刻时压暗：运行中、等人、挂起时
  // 下游还没走到不等于"不在路径上"，压暗会把正要执行的那一截藏起来
  const pathView = pathOnly && isTerminal(runPhase) && isTerminal(view.phase)
  const visited = useMemo(() => {
    const out = new Set<string>()
    for (const [id, n] of Object.entries(view.trace.nodes)) {
      if (n.count > 0 || n.state === 'running' || n.state === 'waiting' || n.state === 'skipped') out.add(id)
    }
    return out
  }, [view.trace])

  // ---- 一次性时刻：只由实时事件触发，回放和补发的历史直接落终态 ----
  const [moment, setMoment] = useState<{ kind: 'start' | 'success' | 'failed'; seq: number; origin: number } | null>(null)
  useEffect(() => {
    let last = 0
    let seq = 0
    let timer: ReturnType<typeof setTimeout> | null = null
    const unsub = useStudio.subscribe((s, prev) => {
      if (s.runPhase === prev.runPhase || s.trace.lastReplay || s.replayAt != null) return
      let kind: 'start' | 'success' | 'failed' | null = null
      if ((prev.runPhase === 'idle' || prev.runPhase === 'queued') && s.runPhase === 'running') {
        // 大图的开场点亮会变成一场烟花，跳过
        kind = s.nodes.length <= 60 ? 'start' : null
      } else if (isActivePhase(prev.runPhase) && s.runPhase === 'succeeded') kind = 'success'
      else if (isActivePhase(prev.runPhase) && s.runPhase === 'failed') kind = 'failed'
      const now = Date.now()
      // 同一秒最多一个时刻：接连来的两个只留第一个
      if (!kind || now - last < 1000) return
      last = now
      seq += 1
      const failedId = s.trace.failedNodeId
      const origin = kind === 'failed' && failedId ? topology({ nodes: s.nodes, edges: s.edges }).rank[failedId] ?? 0 : 0
      setMoment({ kind, seq, origin })
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => setMoment(null), MOMENT_MS)
    })
    return () => {
      unsub()
      if (timer) clearTimeout(timer)
    }
  }, [])
  const momentCtx = useMemo(() => (moment ? { kind: moment.kind, seq: moment.seq } : null), [moment])

  // ---- 边：运行态 + 回边 + 选中时的入边 / 出边 ----
  const decorated = useMemo<Edge[]>(() => {
    const editing = !hasRun && selectedId != null
    return edges.map((e) => {
      const state = edgeStates.get(e.id) ?? 'idle'
      const back = topo.back.has(edgeKey(e))
      const target = view.trace.nodes[e.target]
      const sf: EdgeView = {
        state, back, rank: topo.rank[e.target] ?? 0,
        loops: back ? Math.max(0, target?.iteration ?? (target?.count ?? 1) - 1) : 0,
      }
      const className = clsx(
        `sf-e-${state}`,
        back && 'sf-e-back',
        editing && (e.source === selectedId ? 'sf-e-out' : e.target === selectedId ? 'sf-e-in' : 'sf-e-dim'),
        pathView && (state === 'flow' || state === 'walked' || state === 'held' ? 'sf-e-onpath' : 'sf-e-offpath'),
      )
      // animated 一律关掉：React Flow 的 animated 是一条永远在走的虚线，方向和"是否真在流"都说不清
      return { ...e, type: 'flow', className, animated: false, data: { ...e.data, sf } }
    })
  }, [edges, edgeStates, topo, view.trace, hasRun, selectedId, pathView])

  // ---- 节点：注入拓扑层级（一次性时刻按它错开），只看执行路径时压暗没走的 ----
  // 按原节点对象缓存：拖一个节点只换那一个的副本，其余卡片的 data 引用不变，memo 生效
  const nodeCache = useRef(new WeakMap<FlowNode, { key: string; out: FlowNode }>())
  const flowNodes = useMemo(() => nodes.map((n) => {
    const rank = topo.rank[n.id] ?? 0
    const off = pathView && !visited.has(n.id)
    const key = `${rank}|${off ? 1 : 0}`
    const hit = nodeCache.current.get(n)
    if (hit && hit.key === key) return hit.out
    const out: FlowNode = {
      ...n,
      className: clsx(n.className, off && 'sf-offpath') || undefined,
      style: { ...n.style, '--rank': rank } as CSSProperties,
      data: { ...n.data, rank } as FlowNode['data'],
    }
    nodeCache.current.set(n, { key, out })
    return out
  }), [nodes, topo, pathView, visited])

  // ---- 小地图的状态表 ----
  // 小地图按节点和视口的并集缩进 150×100。这里只按节点算（视口一动就重算不值得），
  // 得到的比例偏小一点，圈在屏幕上也就小一点，不影响看见
  const miniUnit = useMemo(() => {
    if (!hasRun || !nodes.length) return 1
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity
    for (const n of nodes) {
      x0 = Math.min(x0, n.position.x)
      y0 = Math.min(y0, n.position.y)
      x1 = Math.max(x1, n.position.x + (n.measured?.width ?? NODE_WIDTH))
      y1 = Math.max(y1, n.position.y + (n.measured?.height ?? 80))
    }
    return Math.max(1, (x1 - x0) / 150, (y1 - y0) / 100)
  }, [hasRun, nodes])
  const miniStates = useMemo(() => {
    if (!hasRun) return null
    const states: Record<string, NodeState> = {}
    for (const n of nodes) states[n.id] = view.trace.nodes[n.id]?.state ?? 'idle'
    return { states, unit: miniUnit }
  }, [hasRun, nodes, view.trace, miniUnit])

  // ---- 语义缩放：只在跨档时写一次属性，--zoom 和缩放读数直接写到 DOM，不经过 React ----
  // 移动途中带回滞；停稳后（onMoveEnd）按落点重新分档
  const zoomText = useRef<HTMLSpanElement>(null)
  const lod = useRef<Lod>('full')
  const applyZoom = useCallback((zoom: number, settled = false) => {
    const el = rf.current
    if (!el) return
    el.style.setProperty('--zoom', String(Math.round(zoom * 1000) / 1000))
    if (zoomText.current) zoomText.current.textContent = `${Math.round(zoom * 100)}%`
    const next = lodFor(zoom, settled ? null : lod.current)
    if (next !== lod.current || !el.dataset.lod) {
      lod.current = next
      el.dataset.lod = next
    }
  }, [])
  useEffect(() => {
    applyZoom(rfStore.getState().transform[2], true)
    return rfStore.subscribe((s, prev) => {
      if (s.transform[2] !== prev.transform[2]) applyZoom(s.transform[2])
    })
  }, [rfStore, applyZoom])

  // 画布尺寸变了（航迹坞出现、变量抽屉挤进来）时保持视口中心不动。React Flow
  // 默认钉住左上角，坞一升起来，画面下半截的节点就被它盖住了
  const box = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const el = box.current
    if (!el) return
    let last = { w: el.clientWidth, h: el.clientHeight }
    const ro = new ResizeObserver(() => {
      const w = el.clientWidth
      const h = el.clientHeight
      if (last.w && last.h && (w !== last.w || h !== last.h)) {
        const { x, y, zoom } = getViewport()
        void setViewport({ x: x + (w - last.w) / 2, y: y + (h - last.h) / 2, zoom })
      }
      last = { w, h }
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [getViewport, setViewport])

  // ---- 取景 ----
  const pausedUntil = useRef(0)
  /**
   * 人手动平移、缩放的那一下：开始时记下视口，结束时真的动过才让跟随让路。
   * 只是按一下（点空白处取消选择、正式运行时点节点）d3 也报开始和结束，那不算
   */
  const userMove = useRef<Viewport | null>(null)
  const [followPaused, setFollowPaused] = useState(false)
  const pauseFollow = useCallback(() => {
    pausedUntil.current = Date.now() + FOLLOW_PAUSE_MS
    setFollowPaused(true)
  }, [])
  useEffect(() => {
    if (!followPaused) return
    const t = setTimeout(() => setFollowPaused(false), FOLLOW_PAUSE_MS)
    return () => clearTimeout(t)
  }, [followPaused])

  /**
   * 打开一张图时的取景。整图放得下、缩放不低于可读下限就整图居中；放不下就
   * 不再硬塞——以入口为左锚、按可读缩放只取前几列，其余交给小地图。以前一律
   * 整图塞进来，12 节点的图缩到 0.2，标题只剩 3px。
   */
  const frame = useCallback((duration: number) => {
    const el = rf.current
    const all = getNodes()
    if (!el || !all.length) return
    const bounds = getNodesBounds(all)
    const W = el.clientWidth
    const H = el.clientHeight
    const PAD = 40
    if (!W || !H || !bounds.width) return
    const fit = Math.min((W - 2 * PAD) / bounds.width, (H - 2 * PAD) / bounds.height)
    if (fit >= READABLE_ZOOM) {
      void fitView({ padding: 0.12, maxZoom: 1.1, minZoom: READABLE_ZOOM, duration, interpolate: LINEAR })
      return
    }
    // 按列聚类，取前四列的跨度定缩放
    const xs = [...new Set(all.map((n) => Math.round(n.position.x)))].sort((a, b) => a - b)
    const cols: number[] = []
    for (const x of xs) if (!cols.length || x - cols[cols.length - 1] > 60) cols.push(x)
    const firstCols = cols.slice(0, 4)
    const span = (firstCols[firstCols.length - 1] ?? bounds.x) + NODE_WIDTH - bounds.x
    let zoom = Math.min(1, Math.max(READABLE_ZOOM, (W - 2 * PAD) / Math.max(span, NODE_WIDTH)))
    // 竖直方向放得下就别再放大：宁可多看几列
    const vfit = (H - 2 * PAD) / bounds.height
    if (vfit >= READABLE_ZOOM) zoom = Math.min(zoom, vfit)
    const entries = all.filter((n) => (n.data as FlowNode['data']).nodeType === 'input')
    const anchor = entries[0] ?? all.reduce((a, b) => (a.position.x <= b.position.x ? a : b))
    const anchorH = anchor.measured?.height ?? 80
    const x = PAD - bounds.x * zoom
    // 竖直方向按"这一屏看得见的那几列"居中，不按整张图：斜着往下长的图按整体居中，
    // 入口那几列会贴在顶上，下面空一大块
    const reach = bounds.x + (W - PAD) / zoom
    const seen = all.filter((n) => n.position.x <= reach)
    const top = Math.min(...seen.map((n) => n.position.y))
    const bottom = Math.max(...seen.map((n) => n.position.y + (n.measured?.height ?? 80)))
    const y = (bottom - top) * zoom <= H - 2 * PAD
      ? (H - (bottom - top) * zoom) / 2 - top * zoom
      : H / 2 - (anchor.position.y + anchorH / 2) * zoom
    void setViewport({ x, y, zoom }, { duration, interpolate: LINEAR })
  }, [fitView, getNodes, getNodesBounds, setViewport])

  // 打开、换图、自动排版、Copilot 生成完之后取景。位置全变了却不重新取景，
  // 人会盯着一片空白以为图没了。要等 React Flow 量完节点尺寸才算得准
  const framed = useRef(false)
  useEffect(() => {
    let raf = 0
    let tries = 0
    const duration = framed.current && !reducedMotion() ? 280 : 0
    const attempt = () => {
      const all = getNodes()
      if (all.length && !all.every((n) => n.measured?.width) && tries++ < 40) {
        raf = requestAnimationFrame(attempt)
        return
      }
      framed.current = true
      frame(duration)
    }
    raf = requestAnimationFrame(attempt)
    return () => cancelAnimationFrame(raf)
  }, [fitRequest, frame, getNodes])

  /** 把一组节点摆进视口。都已经在视口里就不动：画面不该无缘无故地跳 */
  const bringIntoView = useCallback((ids: string[], opts: { force?: boolean; zoomAtLeast?: number } = {}) => {
    const el = rf.current
    if (!el) return false
    const rects = ids.map((id) => getInternalNode(id)).filter(Boolean).map((n) => ({
      x: n!.internals.positionAbsolute.x, y: n!.internals.positionAbsolute.y,
      w: n!.measured.width ?? NODE_WIDTH, h: n!.measured.height ?? 80,
    }))
    if (!rects.length) return false
    const { x: vx, y: vy, zoom } = getViewport()
    const M = 32
    const inside = rects.every((r) => r.x * zoom + vx >= M && r.y * zoom + vy >= M
      && (r.x + r.w) * zoom + vx <= el.clientWidth - M && (r.y + r.h) * zoom + vy <= el.clientHeight - M)
    if (inside && !opts.force) return false
    const minX = Math.min(...rects.map((r) => r.x))
    const minY = Math.min(...rects.map((r) => r.y))
    const maxX = Math.max(...rects.map((r) => r.x + r.w))
    const maxY = Math.max(...rects.map((r) => r.y + r.h))
    const z = Math.max(zoom, opts.zoomAtLeast ?? 0)
    // 几个节点同时开跑、包围盒放不下时，只追第一个，免得视口来回跳
    const fits = (maxX - minX) * z <= el.clientWidth - 2 * M && (maxY - minY) * z <= el.clientHeight - 2 * M
    const target = fits ? { cx: (minX + maxX) / 2, cy: (minY + maxY) / 2 }
      : { cx: rects[0].x + rects[0].w / 2, cy: rects[0].y + rects[0].h / 2 }
    void setCenter(target.cx, target.cy, { zoom: z, duration: reducedMotion() ? 0 : 320, interpolate: LINEAR })
    return true
  }, [getInternalNode, getViewport, setCenter])

  // 跟随执行：有节点实时开跑、而它在视口外时平移过去。只由 node.started 驱动，
  // 不新增任何动画循环；同一帧里一起开跑的并成一次
  useEffect(() => {
    let raf = 0
    let pending: string[] = []
    const unsub = useStudio.subscribe((s, prev) => {
      if (s.trace === prev.trace || !s.follow || s.replayAt != null || s.trace.lastReplay) return
      for (const [id, n] of Object.entries(s.trace.nodes)) {
        if (n.state === 'running' && !n.looping && n.count > (prev.trace.nodes[id]?.count ?? 0)) pending.push(id)
      }
      if (!pending.length || raf) return
      raf = requestAnimationFrame(() => {
        raf = 0
        const ids = pending
        pending = []
        // 人正拖着画布时也不抢：松手之后再按"动过没有"决定暂不暂停
        if (Date.now() < pausedUntil.current || userMove.current) return
        bringIntoView(ids, { zoomAtLeast: 0.5 })
      })
    })
    return () => {
      unsub()
      cancelAnimationFrame(raf)
    }
  }, [bringIntoView])

  // 右栏、时间轴、胶囊请求取景：点名要看它，就算已在视口里也居中一下。
  // focusRequest 每次请求都是新对象（seq +1），同一个节点连点两次也会再取景
  useEffect(() => {
    // 画布重新挂载（从别的页回来）时 store 里还留着上一次的请求，不该再取景一次
    if (!focusRequest || focusRequest === lastFocus) return
    lastFocus = focusRequest
    let raf = 0
    let tries = 0
    const go = () => {
      if (!getInternalNode(focusRequest.id)?.measured?.width && tries++ < 20) {
        raf = requestAnimationFrame(go)
        return
      }
      if (bringIntoView([focusRequest.id], { force: true, zoomAtLeast: READABLE_ZOOM })) pauseFollow()
    }
    go()
    return () => cancelAnimationFrame(raf)
  }, [focusRequest, bringIntoView, getInternalNode, pauseFollow])

  // 调色板点击添加节点时落在视口中心。ReactFlowProvider 在这里面，外面拿不到 useReactFlow
  useEffect(() => {
    const getViewportCenter = () => {
      const r = rf.current?.getBoundingClientRect()
      if (!r) return { x: 0, y: 0 }
      return screenToFlowPosition({ x: r.left + r.width / 2, y: r.top + r.height / 2 })
    }
    setStudio({ getViewportCenter })
    return () => setStudio({ getViewportCenter: null })
  }, [screenToFlowPosition])

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault()
      const type = event.dataTransfer.getData('application/agentlab-node') as NodeType
      if (!type) return
      if (formalLock) {
        toast.warn('正式运行进行中，画布只读。等它结束再改')
        return
      }
      actions.addNode(type, screenToFlowPosition({ x: event.clientX, y: event.clientY }))
    },
    [actions, screenToFlowPosition, formalLock],
  )

  const rootStyle = useMemo(
    () => (moment?.kind === 'failed' ? { '--moment-origin': moment.origin } as CSSProperties : undefined),
    [moment],
  )
  // 「适配全图」是人主动要看全：不设可读下限。打开时的取景策略在 frame() 里
  const fitAll = useMemo(
    () => ({ padding: 0.12, maxZoom: 1.1, duration: reduced ? 0 : 240, interpolate: LINEAR }),
    [reduced],
  )
  const showFollow = active && replayAt == null && follow

  return (
    <div className="relative min-h-0 flex-1" ref={box}>
      <RoutingContext.Provider value={routes}>
        <MomentContext.Provider value={momentCtx}>
          <MiniStates.Provider value={miniStates}>
            <ReactFlow
              ref={rf}
              className="sf-canvas"
              style={rootStyle}
              data-run-phase={view.phase}
              data-run-class={hasRun ? runClass ?? 'exploratory' : undefined}
              data-replay={replayAt != null ? '1' : undefined}
              data-path-only={pathView ? '1' : undefined}
              data-readonly={formalLock ? '1' : undefined}
              data-moment={moment?.kind}
              data-moment-seq={moment?.seq}
              nodes={flowNodes}
              edges={decorated}
              nodeTypes={nodeTypes}
              edgeTypes={edgeTypes}
              onNodesChange={actions.onNodesChange}
              onEdgesChange={actions.onEdgesChange}
              onConnect={actions.onConnect}
              onNodeClick={(_, node) => actions.select(node.id)}
              onPaneClick={() => actions.select(null)}
              onNodeMouseEnter={(_, node) => { if (hasRun) useDock.getState().set({ canvasHover: node.id }) }}
              onNodeMouseLeave={() => { if (hasRun) useDock.getState().set({ canvasHover: null }) }}
              onMoveStart={(event, vp) => {
                // 程序取景（跟随、聚焦）没有 event；有 event 的是人在拖、在滚轮
                if (event) userMove.current = vp
              }}
              onMoveEnd={(_, vp) => {
                applyZoom(vp.zoom, true)
                const from = userMove.current
                if (!from) return
                userMove.current = null
                const moved = Math.abs(vp.x - from.x) > 2 || Math.abs(vp.y - from.y) > 2
                  || Math.abs(vp.zoom - from.zoom) > 1e-3
                if (moved && active) pauseFollow()
              }}
              onDrop={onDrop}
              onDragOver={(e) => {
                e.preventDefault()
                e.dataTransfer.dropEffect = formalLock ? 'none' : 'move'
              }}
              nodesDraggable={!formalLock}
              nodesConnectable={!active}
              edgesReconnectable={!active}
              deleteKeyCode={active ? null : ['Backspace', 'Delete']}
              minZoom={0.2}
              maxZoom={2}
              defaultEdgeOptions={{ type: 'flow' }}
              ariaLabelConfig={ARIA}
              proOptions={{ hideAttribution: true }}
            >
              {/* 两级网格：细点每格一个，粗点每五格一个，像工程图纸的坐标纸 */}
              <Background id="minor" variant={BackgroundVariant.Dots} gap={18} size={1} color="var(--grid-dot)" />
              <Background id="major" variant={BackgroundVariant.Dots} gap={90} size={1.6}
                          color="var(--grid-dot-major)" bgColor="transparent" />
              <Controls showInteractive={false} fitViewOptions={fitAll}>
                <ControlButton className="sf-zoom-btn"
                               onClick={() => void zoomTo(1, { duration: reduced ? 0 : 240, interpolate: LINEAR })}
                               title="当前缩放 · 点一下回到 100%" aria-label="回到 100% 缩放">
                  <span ref={zoomText} className="tnum" />
                </ControlButton>
              </Controls>
              <MiniMap
                pannable
                zoomable
                // 遮罩压得太狠会把视口外的失败、等人一起压暗——那恰好是最需要看见的地方
                maskColor="color-mix(in srgb, var(--bg) 58%, transparent)"
                maskStrokeColor="var(--text-faint)"
                maskStrokeWidth={1}
                nodeColor={typeColor}
                nodeComponent={MiniNode}
                style={{ width: 150, height: 100 }}
                ariaLabel={hasRun ? '小地图（按运行状态着色）' : '小地图'}
              />
            </ReactFlow>
          </MiniStates.Provider>
        </MomentContext.Provider>
      </RoutingContext.Provider>

      {/* 画布顶上的浮条排成一行：跟随在左，只读横幅居中；放不下时横幅折到下一行、
          再不够就截掉说明文字。各自绝对定位的话，笔记本宽度下横幅会盖住「恢复」 */}
      {(formalLock || showFollow) && (
        <div className="sf-float">
          {showFollow && (
            <div className={clsx('sf-follow', followPaused && 'is-paused')}>
              <Crosshair size={11} />
              {followPaused ? (
                <>
                  <span>已暂停跟随</span>
                  <button type="button" onClick={() => { pausedUntil.current = 0; setFollowPaused(false) }}>
                    恢复
                  </button>
                </>
              ) : (
                <span>跟随执行</span>
              )}
            </div>
          )}
          {formalLock && (
            <div className="sf-banner" role="status">
              <Lock size={12} />
              <span className="sf-banner-main">{runClassLabel('formal', runVersion)} 进行中 · 画布只读</span>
              <span className="sf-banner-dim">跑的是已发布的不可变版本，结束后可以继续编辑</span>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/** 画布下方的航迹坞：有运行时才出现，没有运行不占地方 */
function RunDock() {
  const trace = useStudio((s) => s.trace)
  const runPhase = useStudio((s) => s.runPhase)
  const nodes = useStudio((s) => s.nodes)
  const edges = useStudio((s) => s.edges)
  const replayAt = useStudio((s) => s.replayAt)
  const hovered = useStudio((s) => s.hoveredNodeId)
  const follow = useStudio((s) => s.follow)
  const run = useStudio((s) => s.run)
  const canvasHover = useDock((s) => s.canvasHover)
  const pathOnly = useDock((s) => s.pathOnly)
  const open = useDock((s) => s.open)
  const height = useDock((s) => s.height)
  const playing = useDock((s) => s.playing)
  const speed = useDock((s) => s.speed)
  const compress = useDock((s) => s.compress)
  const setDock = useDock((s) => s.set)
  const { setReplayAt, setHoveredNode, focusNode, setFollow } = useStudio.getState()

  const graph = useMemo(() => ({ nodes, edges }), [nodes, edges])
  const ui = useMemo(() => ({ open, height, playing, speed, compress }), [open, height, playing, speed, compress])
  if (runPhase === 'idle' && trace.startedAt == null && !Object.keys(trace.nodes).length) return null

  const title = run
    ? `${runClassLabel(run.run_class ?? trace.runClass ?? 'exploratory', run.version)} ${shortId(run.id)}`
    : runClassLabel(trace.runClass ?? 'exploratory')
  return (
    <RunTimeline
      trace={trace}
      graph={graph}
      title={title}
      replayAt={replayAt}
      onReplayAt={setReplayAt}
      hoveredNodeId={hovered ?? canvasHover}
      onHoverNode={setHoveredNode}
      onFocusNode={focusNode}
      ui={ui}
      onUi={setDock}
      extra={(
        <>
          {/* 跟随只对还在跑的运行有意义；执行路径要等运行结束才定下来 */}
          {isActivePhase(runPhase) && (
            <label className="tl-toggle" title="跟着正在执行的节点平移画布；手动平移后暂停 10 秒">
              <input type="checkbox" checked={follow} aria-label="跟随执行"
                     onChange={(e) => setFollow(e.target.checked)} />
              <Crosshair size={11} /> <span className="tl-tt">跟随执行</span>
            </label>
          )}
          {isTerminal(runPhase) && (
            <label className="tl-toggle" title="把这次没走到的节点和边压暗，只留执行路径">
              <input type="checkbox" checked={pathOnly} aria-label="只看执行路径"
                     onChange={(e) => setDock({ pathOnly: e.target.checked })} />
              <RouteIcon size={11} /> <span className="tl-tt">只看执行路径</span>
            </label>
          )}
        </>
      )}
    />
  )
}

export function FlowCanvas() {
  return (
    <ReactFlowProvider>
      <div className="flex h-full w-full flex-col">
        <CanvasInner />
        <RunDock />
      </div>
    </ReactFlowProvider>
  )
}
