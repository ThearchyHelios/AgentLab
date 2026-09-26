import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { ChevronDown, ChevronUp, FoldHorizontal, Pause, Play, Radio } from 'lucide-react'
import clsx from 'clsx'
import { create } from 'zustand'
import { StatusBadge } from '../components/ui'
import { formatClock, formatDuration, formatTokens, NONE } from '../lib/format'
import { statusMeta } from '../lib/status'
import { topology, type GraphLike } from '../run/derive'
import {
  isActivePhase, isTerminal, liveAt, project, type NodeState, type NodeTrace, type Projection, type Segment,
  type Trace,
} from '../run/trace'
import { projectCached } from '../run/useNodeView'
import { useRunClock } from '../run/useRunClock'

/**
 * 航迹坞：一次运行按时间摊开。
 *
 * 画布回答"在哪"，右栏回答"说了什么"，这里回答"什么时候、多久、谁和谁同时"——
 * 慢在机器还是慢在等人、哪一步最慢、并行到底省了多少，只有时间轴说得清。
 *
 * 数据全部来自航迹（run/trace.ts），不另解读事件：画布、胶囊、这里、记录页
 * 对同一次运行讲同一个故事。游标就是 replayAt：拖到哪一刻，画布就回到哪一刻。
 *
 * 做成纯 props 组件：画布下面接 useStudio，记录页（第三波）接自己的局部状态。
 */

export interface TimelineGraph {
  nodes: { id: string; type?: string; data?: { nodeType?: string; label?: string; config?: Record<string, any> } }[]
  edges: GraphLike['edges']
}

/** 坞的界面状态。收起 / 展开、高度、播放速度这些，调用方不管就由组件自己记 */
export interface DockUi {
  open: boolean
  height: number
  playing: boolean
  speed: number
  /** 超过两分钟没有事件的空档压成 48px，免得一次 25 分钟的等人把整条轴挤成一条线 */
  compress: boolean
}

export interface RunTimelineProps {
  trace: Trace
  graph: TimelineGraph
  /** 运行名头，如「探索运行 #a41f2c」 */
  title?: string
  /** 回放游标（毫秒时间戳）；null = 实时 */
  replayAt: number | null
  onReplayAt: (at: number | null) => void
  hoveredNodeId?: string | null
  onHoverNode?: (id: string | null) => void
  onFocusNode?: (id: string) => void
  ui?: DockUi
  onUi?: (patch: Partial<DockUi>) => void
  /** 头部右侧额外的开关（画布放「跟随执行」「只看执行路径」，记录页不放） */
  extra?: ReactNode
  className?: string
}

const COLLAPSED = 30
const MIN_H = 132
const LABEL_W = 136
const SUM_W = 116
const ROW_H = 22
const SUB_H = 18
const AXIS_H = 22
/** 游标头在刻度行里占 2–20px */
const CURSOR_TOP = 20
const PAR_H = 26
const RUN_H = 22
/** 吸顶的三行（刻度、并行度、运行本身）一共多高：泳道从它下面开始 */
const HEAD_H = AXIS_H + PAR_H + RUN_H
const FOLD_MIN_MS = 120_000
const FOLD_PX = 48
/** 超过这么多泳道只画看得见的那些 */
const VIRTUAL_AFTER = 60

const DEFAULT_UI: DockUi = { open: true, height: 232, playing: false, speed: 1, compress: true }
const STORE_KEY = 'agentlab.dock'

function readUi(): DockUi {
  try {
    const raw = JSON.parse(localStorage.getItem(STORE_KEY) ?? 'null')
    if (raw && typeof raw === 'object') {
      return {
        ...DEFAULT_UI,
        open: typeof raw.open === 'boolean' ? raw.open : DEFAULT_UI.open,
        height: Number.isFinite(raw.height) ? raw.height : DEFAULT_UI.height,
        compress: typeof raw.compress === 'boolean' ? raw.compress : DEFAULT_UI.compress,
      }
    }
  } catch { /* 隐私窗口、禁用存储：用默认值 */ }
  return DEFAULT_UI
}

function saveUi(ui: DockUi): void {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify({ open: ui.open, height: ui.height, compress: ui.compress }))
  } catch { /* 记不住就算了，下次按默认开 */ }
}

/**
 * 画布这边的坞状态。运行胶囊的「回放」要能把坞打开并开始播，所以放在模块级，
 * 而不是组件自己的 state。pathOnly 是画布的「只看执行路径」，开关放在坞头上。
 */
interface DockStore extends DockUi {
  pathOnly: boolean
  /** 画布上鼠标停着的节点：只给坞高亮泳道用，不进 studio（卡片会跟着变暗） */
  canvasHover: string | null
  set: (patch: Partial<DockUi & { pathOnly: boolean; canvasHover: string | null }>) => void
}

export const useDock = create<DockStore>((set, get) => ({
  ...readUi(),
  pathOnly: false,
  canvasHover: null,
  set: (patch) => {
    set(patch)
    if ('open' in patch || 'height' in patch || 'compress' in patch) saveUi(get())
  },
}))

// -------------------------------------------------------------------------
// 时间 → 像素
// -------------------------------------------------------------------------

interface Fold { a: number; b: number; x0: number; x1: number }
interface Scale {
  t0: number
  t1: number
  width: number
  x: (t: number) => number
  t: (x: number) => number
  folds: Fold[]
  ticks: { t: number; x: number; label: string }[]
}

const STEPS = [100, 200, 500, 1000, 2000, 5000, 10_000, 15_000, 30_000, 60_000, 120_000, 300_000,
  600_000, 900_000, 1_800_000, 3_600_000, 7_200_000]

const pad2 = (n: number) => String(n).padStart(2, '0')

/** 刻度上的相对时刻：「00:05」「1:02:00」，步长不到一秒时带十分位 */
function relLabel(ms: number, fine: boolean): string {
  if (fine) return formatClock(ms)
  const s = Math.round(ms / 1000)
  const h = Math.floor(s / 3600)
  const m = Math.floor(s / 60) % 60
  return h > 0 ? `${h}:${pad2(m)}:${pad2(s % 60)}` : `${pad2(m)}:${pad2(s % 60)}`
}

/**
 * 窄处的时长：一分钟以内照常（「14.3 s」），以上写钟面（「1:28」「1:02:05」）。
 * trunc：还在走的数和计时器一样截断——四舍五入的话同一刻这里写 3.8、计时器写 00:03.7
 */
function span(ms: number, trunc = false): string {
  if (ms < 60_000) return trunc ? `${(Math.floor(ms / 100) / 10).toFixed(1)} s` : formatDuration(ms)
  const s = trunc ? Math.floor(ms / 1000) : Math.round(ms / 1000)
  const h = Math.floor(s / 3600)
  const m = Math.floor(s / 60) % 60
  return h > 0 ? `${h}:${pad2(m)}:${pad2(s % 60)}` : `${m}:${pad2(s % 60)}`
}

/**
 * 折叠处的小标签只有 48px：写整数分钟 / 小时，完整时长在 title 里。
 * 不再两头加「⋯」——11px 下它们放不下、被截成半个，斜纹本身已经说明这里折起来了
 */
function foldLabel(ms: number): string {
  const m = Math.round(ms / 60_000)
  return m < 60 ? `${m} 分` : `${Math.floor(m / 60)} 时 ${pad2(m % 60)}`
}

/**
 * 分段线性的时间轴：长时间没有任何事件的空档压成固定宽度，其余按比例。
 * 空档不是删掉——斜纹里写着真实时长，刻度在折叠处跳变，时间不被静默变形。
 */
function buildScale(t0: number, t1: number, width: number, stamps: number[], compress: boolean): Scale {
  const span = Math.max(1, t1 - t0)
  const gaps: [number, number][] = []
  if (compress && width > 0) {
    const pts = [t0, ...stamps.filter((s) => s > t0 && s < t1), t1].sort((a, b) => a - b)
    for (let i = 0; i < pts.length - 1; i += 1) {
      if (pts[i + 1] - pts[i] > FOLD_MIN_MS) gaps.push([pts[i], pts[i + 1]])
    }
  }
  const foldPx = gaps.length ? Math.min(FOLD_PX, (width * 0.3) / gaps.length) : 0
  const folded = gaps.reduce((acc, [a, b]) => acc + (b - a), 0)
  const k = Math.max(0, width - foldPx * gaps.length) / Math.max(1, span - folded)

  const knots: [number, number][] = [[t0, 0]]
  const folds: Fold[] = []
  let x = 0
  let cur = t0
  for (const [a, b] of gaps) {
    x += (a - cur) * k
    knots.push([a, x])
    folds.push({ a, b, x0: x, x1: x + foldPx })
    x += foldPx
    knots.push([b, x])
    cur = b
  }
  knots.push([t1, width])

  const toX = (t: number): number => {
    if (t <= t0) return 0
    if (t >= t1) return width
    for (let i = 1; i < knots.length; i += 1) {
      const [tb, xb] = knots[i]
      if (t <= tb) {
        const [ta, xa] = knots[i - 1]
        return tb === ta ? xb : xa + ((t - ta) / (tb - ta)) * (xb - xa)
      }
    }
    return width
  }
  const toT = (px: number): number => {
    if (px <= 0) return t0
    if (px >= width) return t1
    for (let i = 1; i < knots.length; i += 1) {
      const [tb, xb] = knots[i]
      if (px <= xb) {
        const [ta, xa] = knots[i - 1]
        return xb === xa ? tb : ta + ((px - xa) / (xb - xa)) * (tb - ta)
      }
    }
    return t1
  }

  // 刻度：线性部分里相邻两格至少 72px
  const step = STEPS.find((s) => s * k >= 72) ?? STEPS[STEPS.length - 1]
  const ticks: Scale['ticks'] = []
  if (k > 0) {
    for (let rel = 0; rel <= span && ticks.length < 200; rel += step) {
      const t = t0 + rel
      if (folds.some((f) => t > f.a && t < f.b)) continue
      ticks.push({ t, x: toX(t), label: relLabel(rel, step < 1000) })
    }
  }
  return { t0, t1, width, x: toX, t: toT, folds, ticks }
}

// -------------------------------------------------------------------------
// 泳道
// -------------------------------------------------------------------------

interface Row {
  key: string
  kind: 'node' | 'dispatch' | 'member'
  nodeId: string
  label: string
  type?: string
  segs: Segment[]
  state: NodeState
  count: number
  summary: string
  /**
   * 只执行过一段的节点，用后端量的耗时（事件时间戳之间还夹着转发的延迟）。
   * 段上的数字和右边的摘要都用它，同一行不出现两个数
   */
  measuredMs?: number
  /** 泳道里写一句话（阻断、未到达），代替空白 */
  note?: string
  /** 读屏念的名字：子泳道带上所属节点 */
  name: string
  top: number
  height: number
}

const nodeTypeOf = (n: TimelineGraph['nodes'][number] | undefined): string | undefined =>
  n?.data?.nodeType ?? n?.type

/** 只跑了一段、已经结束的节点：后端量的耗时。人工审批恢复后会再开一段，那种不算 */
function measuredOf(n: NodeTrace | undefined, at: number): number | undefined {
  if (!n || n.lastDurationMs == null || n.count > 1) return undefined
  const runs = n.segments.filter((s) => s.kind === 'run' && s.start <= at)
  return runs.length === 1 && runs[0].end != null && runs[0].end <= at ? n.lastDurationMs : undefined
}

/** 一条泳道右侧的摘要：执行了几次、一共多久、等了多久、用了多少 token */
function summarize(n: NodeTrace | undefined, state: NodeState, at: number): string {
  if (!n || state === 'idle' || state === 'queued') return state === 'queued' ? '排队' : NONE
  if (state === 'blocked') return '阻断'
  if (state === 'unreached') return '未到达'
  if (state === 'skipped') return '跳过'
  let run = 0
  let wait = 0
  for (const s of n.segments) {
    if (s.start > at) continue
    const end = Math.min(s.end ?? at, at)
    if (s.kind === 'run') run += Math.max(0, end - s.start)
    if (s.kind === 'wait') wait += Math.max(0, end - s.start)
  }
  const parts: string[] = []
  if (n.count > 1) parts.push(`×${n.count}`)
  const measured = measuredOf(n, at)
  if (run > 0 || state === 'done') parts.push(formatDuration(measured ?? (run || n.lastDurationMs)))
  if (wait > 0) parts.push(`等 ${formatClock(wait).replace(/\.\d$/, '')}`)
  const tok = n.tokensIn + n.tokensOut
  if (tok > 0 && parts.length < 3) parts.push(formatTokens(tok, { compact: true }))
  return parts.join(' · ') || NONE
}

function buildRows(trace: Trace, graph: TimelineGraph, at: number, labels: Map<string, string>,
                   states: Projection['nodes'] | null): Row[] {
  const topo = topology(graph as GraphLike)
  const byId = new Map(graph.nodes.map((n) => [n.id, n]))
  const ids = [...topo.order, ...Object.keys(trace.nodes).filter((id) => !byId.has(id))]
  const failedLabel = trace.failedNodeId ? labels.get(trace.failedNodeId) ?? trace.failedNodeId : ''
  const rows: Row[] = []
  let top = 0
  for (const id of ids) {
    const n = trace.nodes[id]
    // 回放时状态取游标那一刻的投影：摘要和泳道底色要和画布上的卡片一致
    const state: NodeState = states ? states[id]?.state ?? 'idle' : n?.state ?? 'idle'
    const type = nodeTypeOf(byId.get(id))
    const label = labels.get(id) ?? id
    rows.push({
      key: id, kind: 'node', nodeId: id, label, type,
      segs: (n?.segments ?? []).filter((s) => s.kind === 'run' || s.kind === 'wait' || s.kind === 'retry'),
      state, count: n?.count ?? 0, summary: summarize(n, state, at), measuredMs: measuredOf(n, at),
      note: state === 'blocked' ? (failedLabel ? `阻断：上游「${failedLabel}」失败` : '阻断：上游失败')
        : state === 'unreached' ? '未到达' : undefined,
      name: label, top, height: ROW_H,
    })
    top += ROW_H
    if (!n) continue
    // 协作团队展开成子泳道：调度者一条，每个成员一条
    const dispatch = n.segments.filter((s) => s.kind === 'dispatch')
    if (dispatch.length) {
      const total = dispatch.reduce((acc, s) => acc + Math.max(0, Math.min(s.end ?? at, at) - s.start), 0)
      const estimated = dispatch.every((s) => s.estimated)
      rows.push({
        key: `${id}::dispatch`, kind: 'dispatch', nodeId: id, label: '调度者',
        segs: dispatch, state, count: dispatch.length,
        summary: `${formatDuration(total)}${estimated ? ' · 推算' : ''}`,
        name: `${label} · 调度者`, top, height: SUB_H,
      })
      top += SUB_H
    }
    const agents: string[] = []
    for (const s of n.segments) if (s.kind === 'member' && s.agent && !agents.includes(s.agent)) agents.push(s.agent)
    for (const agent of agents) {
      const segs = n.segments.filter((s) => s.kind === 'member' && s.agent === agent)
      const total = segs.reduce((acc, s) => acc + Math.max(0, Math.min(s.end ?? at, at) - s.start), 0)
      rows.push({
        key: `${id}::${agent}`, kind: 'member', nodeId: id, label: agent, segs, state,
        count: segs.length, summary: `${segs.length > 1 ? `×${segs.length} · ` : ''}${formatDuration(total)}`,
        name: `${label} · ${agent}`, top, height: SUB_H,
      })
      top += SUB_H
    }
  }
  return rows
}

/** 所有有意义的时刻：折叠空档时，只有这些之间的空白才算"什么都没发生" */
function stampsOf(trace: Trace): number[] {
  const out: number[] = []
  for (const n of Object.values(trace.nodes)) {
    for (const s of n.segments) {
      out.push(s.start)
      if (s.end != null) out.push(s.end)
    }
  }
  for (const [ts] of trace.phases) out.push(ts)
  for (const [ts] of trace.parallelSeries) out.push(ts)
  return out
}

/**
 * 停下的运行的「现在」。停下（终态、挂起）时航迹一定写了 endedAt；没有的只剩
 * 还没开始的空航迹，取相位变化、段起止里最晚的那个。Trace.book 是内部簿记，不读
 */
export function lastStampOf(trace: Trace, stamps: number[] = stampsOf(trace)): number {
  if (trace.endedAt != null) return trace.endedAt
  let last = trace.startedAt ?? 0
  for (const t of stamps) if (t > last) last = t
  return last
}

/**
 * 停下的运行此刻的样子。project 在「不早于最后一条事件」时读节点的当前状态（含推导出的
 * 阻断、未到达）和后端给的权威时长，早于它就按段重建、这两样都丢了。最后一条事件的时刻
 * 只记在内部簿记里，所以有 endedAt 时取一个一定不早于它的时刻；结束时刻照样按 endedAt 算
 */
export function projectSettled(trace: Trace): Projection {
  return project(trace, trace.endedAt != null ? Number.MAX_SAFE_INTEGER : lastStampOf(trace))
}

/**
 * 这个节点在 at 那一刻已经等了多久（毫秒），不在等就是 null。胶囊、面板、坞头、泳道都用它，
 * 同一刻写同一个数。运行停在它上面时从 run.interrupted 算：审批卡那时才出现，后端的 wait_ms
 * 和「等人」合计也从那一刻起。运行还没停下（并行分支还在跑）时按它自己的等待段
 */
export function waitedMs(trace: Trace, nodeId: string, at: number): number | null {
  const seg = trace.nodes[nodeId]?.segments.find((s) => s.kind === 'wait' && s.start <= at && (s.end == null || s.end > at))
  if (!seg) return null
  const stopped = trace.waits.find(([a, b]) => a >= seg.start && a <= at && (b == null || b > at))
  return Math.max(0, at - (stopped ? stopped[0] : seg.start))
}

function segClass(s: Segment): string {
  if (s.kind === 'wait') return s.end == null ? 'tl-s-waiting tl-hatch' : 'tl-s-waited tl-hatch'
  if (s.kind === 'dispatch') return clsx('tl-s-dispatch', s.estimated && 'tl-estimated')
  return `tl-s-${s.status}`
}

function segTitle(row: Row, s: Segment, t0: number, at: number): string {
  const end = s.end ?? at
  const what = s.kind === 'wait' ? '等待审批'
    : s.kind === 'dispatch' ? (s.estimated ? '调度决策（推算：由事件间隙推出，不是测量值）' : '调度决策')
      : s.kind === 'retry' ? '重试'
        : statusMeta(s.status).label
  return [
    `${row.label}${s.iteration != null ? ` · 第 ${s.iteration} ${row.kind === 'node' ? '轮' : '轮协作'}` : ''}`,
    `${what}${s.handle && s.kind === 'run' ? ` → ${s.handle}` : ''}`,
    `开始 T+${formatClock(s.start - t0)}${s.end == null ? ' · 进行中' : ''}`,
    s.kind === 'retry' ? '' : `耗时 ${formatDuration(end - s.start)}`,
  ].filter(Boolean).join('\n')
}

// -------------------------------------------------------------------------

export function RunTimeline({
  trace, graph, title, replayAt, onReplayAt, hoveredNodeId, onHoverNode, onFocusNode,
  ui: uiProp, onUi, extra, className,
}: RunTimelineProps) {
  const [own, setOwn] = useState<DockUi>(DEFAULT_UI)
  const ui = uiProp ?? own
  const patch = useCallback((p: Partial<DockUi>) => {
    if (onUi) onUi(p)
    else setOwn((cur) => ({ ...cur, ...p }))
  }, [onUi])

  const live = replayAt == null
  const running = isActivePhase(trace.phase)
  // 实时且还在跑：游标跟着现在走。播放回放时也要节拍
  const now = useRunClock((live && running) || ui.playing)
  const liveNow = liveAt(trace, now)

  const stamps = useMemo(() => stampsOf(trace), [trace])
  const t0 = trace.startedAt ?? (stamps.length ? Math.min(...stamps) : liveNow)
  const tEnd = trace.endedAt ?? (running ? liveNow : lastStampOf(trace, stamps))
  // 在跑时右边留 4% 的余量，"现在"不贴着边
  const tAxisEnd = Math.max(tEnd, t0 + 1000) + (running && live ? Math.max(1000, (tEnd - t0) * 0.04) : 0)
  const cursor = replayAt ?? (running ? liveNow : tEnd)

  const labels = useMemo(
    () => new Map(graph.nodes.map((n) => [n.id, n.data?.label || n.id])),
    [graph.nodes],
  )

  // 轨道宽度随坞宽变。量滚动容器的 clientWidth：泳道多到出滚动条时，轨道要让出那 9px
  const body = useRef<HTMLDivElement>(null)
  const scroller = useRef<HTMLDivElement>(null)
  const [trackW, setTrackW] = useState(0)
  useLayoutEffect(() => {
    const el = scroller.current
    if (!el) return
    const measure = () => setTrackW(Math.max(0, el.clientWidth - LABEL_W - SUM_W))
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [ui.open])

  // 布局只在 200ms 一格时重算：泳道和段的位置用不着跟着 100ms 的时钟走
  const quant = (t: number) => Math.floor(t / 200) * 200
  const layoutEnd = running && live ? quant(tAxisEnd) : tAxisEnd
  const scale = useMemo(
    () => buildScale(t0, layoutEnd, trackW, stamps, ui.compress),
    [t0, layoutEnd, trackW, stamps, ui.compress],
  )
  // 开着的段画到"现在"为止，不画进右边留的余量里。回放时段照样画全（整次运行的
  // 形状一直看得见），游标之后压暗；右边的数字按游标那一刻算
  const drawAt = running ? quant(liveNow) : tEnd
  const layoutAt = replayAt ?? drawAt
  const replayStates = useMemo(() => (replayAt == null ? null : projectCached(trace, replayAt).nodes), [trace, replayAt])
  const rows = useMemo(
    () => buildRows(trace, graph, layoutAt, labels, replayStates),
    [trace, graph, layoutAt, labels, replayStates],
  )
  const proj = useMemo(
    () => (live && !running ? projectSettled(trace) : project(trace, cursor)),
    [trace, cursor, live, running],
  )

  // ---- 播放：游标按倍速往前走，折叠的空档一跃而过 ----
  const lastTick = useRef<number | null>(null)
  useEffect(() => {
    if (!ui.playing) { lastTick.current = null; return }
    const prev = lastTick.current
    lastTick.current = now
    if (prev == null) return
    let at = (replayAt ?? t0) + (now - prev) * ui.speed
    const fold = scale.folds.find((f) => at > f.a && at < f.b)
    if (fold) at = fold.b
    if (at >= tEnd) {
      at = tEnd
      patch({ playing: false })
    }
    onReplayAt(at)
    // 只按时钟推进：游标、刻度变了不该额外再走一步
  }, [now, ui.playing])

  // ---- 拖动游标 ----
  const scrub = useRef<{ id: number; raf: number; x: number } | null>(null)
  const setFromX = useCallback((clientX: number) => {
    const el = body.current
    if (!el) return
    const rect = el.getBoundingClientRect()
    const x = clientX - rect.left - LABEL_W
    onReplayAt(Math.round(scale.t(Math.max(0, Math.min(scale.width, x)))))
  }, [scale, onReplayAt])

  const onScrubDown = (e: React.PointerEvent) => {
    if (e.button !== 0) return
    e.preventDefault()
    ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
    patch({ playing: false })
    scrub.current = { id: e.pointerId, raf: 0, x: e.clientX }
    setFromX(e.clientX)
  }
  const onScrubMove = (e: React.PointerEvent) => {
    const s = scrub.current
    if (!s || s.id !== e.pointerId) return
    s.x = e.clientX
    if (s.raf) return
    s.raf = requestAnimationFrame(() => {
      if (!scrub.current) return
      scrub.current.raf = 0
      setFromX(scrub.current.x)
    })
  }
  const onScrubUp = (e: React.PointerEvent) => {
    const s = scrub.current
    if (!s || s.id !== e.pointerId) return
    if (s.raf) cancelAnimationFrame(s.raf)
    scrub.current = null
  }

  const onCursorKey = (e: React.KeyboardEvent) => {
    const span = scale.t1 - scale.t0
    const step = e.shiftKey ? span / 10 : span / 100
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      e.preventDefault()
      const next = Math.max(t0, Math.min(tEnd, cursor + (e.key === 'ArrowLeft' ? -step : step)))
      patch({ playing: false })
      onReplayAt(Math.round(next))
    } else if (e.key === 'Home') {
      e.preventDefault(); onReplayAt(t0)
    } else if (e.key === 'End') {
      e.preventDefault(); onReplayAt(null)
    }
  }

  // ---- 拖高度（键盘上下键也能调）----
  const section = useRef<HTMLElement>(null)
  const maxH = () => Math.max(MIN_H, (section.current?.parentElement?.clientHeight ?? 800) * 0.7)
  const setHeight = (h: number) => patch({ height: Math.round(Math.max(MIN_H, Math.min(maxH(), h))) })
  const grip = useRef<{ id: number; y: number; h: number } | null>(null)
  const onGripDown = (e: React.PointerEvent) => {
    if (e.button !== 0) return
    ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
    grip.current = { id: e.pointerId, y: e.clientY, h: ui.height }
  }
  const onGripMove = (e: React.PointerEvent) => {
    const g = grip.current
    if (!g || g.id !== e.pointerId) return
    setHeight(g.h + (g.y - e.clientY))
  }
  const onGripUp = () => { grip.current = null }
  const onGripKey = (e: React.KeyboardEvent) => {
    const step = e.shiftKey ? 64 : 16
    const next = e.key === 'ArrowUp' ? ui.height + step : e.key === 'ArrowDown' ? ui.height - step
      : e.key === 'Home' ? MIN_H : e.key === 'End' ? maxH() : null
    if (next == null) return
    e.preventDefault()
    setHeight(next)
  }

  // ---- 虚拟化：泳道很多时只画看得见的 ----
  const [view, setView] = useState({ top: 0, height: 400 })
  const onScroll = () => {
    const el = scroller.current
    if (el && rows.length > VIRTUAL_AFTER) setView({ top: el.scrollTop, height: el.clientHeight })
  }
  useEffect(() => {
    const el = scroller.current
    if (el) setView({ top: el.scrollTop, height: el.clientHeight })
  }, [ui.open, ui.height])
  const totalH = rows.length ? rows[rows.length - 1].top + rows[rows.length - 1].height : 0
  const visible = rows.length > VIRTUAL_AFTER
    ? rows.filter((r) => r.top + r.height >= view.top - 120 && r.top <= view.top + view.height + 120)
    : rows

  // ---- 泳道的键盘操作：整片泳道只占一个 Tab 位，上下键换泳道，回车 / 空格在画布上取景 ----
  const [laneKey, setLaneKey] = useState<string | null>(null)
  const tabLane = rows.some((r) => r.key === laneKey) ? laneKey : rows[0]?.key ?? null
  const pendingLane = useRef<string | null>(null)
  const moveLane = (from: string, to: number | 'first' | 'last') => {
    const i = rows.findIndex((r) => r.key === from)
    const next = rows[to === 'first' ? 0 : to === 'last' ? rows.length - 1 : Math.max(0, Math.min(rows.length - 1, i + to))]
    if (!next || next.key === from) return
    setLaneKey(next.key)
    pendingLane.current = next.key
    // 目标可能在可见区外（虚拟化时还没画出来）：先滚过去，吸顶的三行会盖住最上面那截
    const el = scroller.current
    if (!el) return
    const room = el.clientHeight - HEAD_H
    if (next.top < el.scrollTop) el.scrollTop = next.top
    else if (next.top + next.height > el.scrollTop + room) el.scrollTop = next.top + next.height - room
    if (rows.length > VIRTUAL_AFTER) setView({ top: el.scrollTop, height: el.clientHeight })
  }
  useEffect(() => {
    const key = pendingLane.current
    if (!key) return
    const lane = scroller.current?.querySelector<HTMLElement>(`[data-lane="${CSS.escape(key)}"]`)
    if (!lane) return
    pendingLane.current = null
    lane.focus({ preventScroll: true })
  })

  const hovered = hoveredNodeId ?? null
  const cursorX = scale.x(cursor)
  // 游标头大约 96px 宽，盖住的刻度只留竖线、不写字，免得数字从它两边漏出半截
  const flip = cursorX > scale.width - 90
  const headL = flip ? cursorX - 100 : cursorX - 52
  const headR = flip ? cursorX + 4 : cursorX + 52
  const failed = trace.phase === 'failed'
  const phaseMeta = statusMeta(trace.phase === 'idle' ? 'idle' : trace.phase)
  const cursorLabel = live
    ? (running ? `实时 ${formatClock(cursor - t0)}` : `结束 ${formatClock(tEnd - t0)}`)
    : `回放 ${formatClock(cursor - t0)}`
  const peak = trace.parallelSeries.reduce((m, [, v]) => Math.max(m, v), 0)
  // 实时在跑时和游标、胶囊用同一种写法（mm:ss.s），数字才对得上；有等人就两段都写，
  // 用紧凑的钟面写法，不然「1 分 28 秒 · 等 25 分 00 秒」塞不进这一列。还在走的那一段截断
  const liveRun = running && live
  const drive = trace.drives[trace.drives.length - 1]
  const wait = trace.waits[trace.waits.length - 1]
  const runSum = proj.waitMs > 0
    ? `${span(proj.activeMs, liveRun && !!drive && drive[1] == null)} · 等 ${span(proj.waitMs, liveRun && !!wait && wait[1] == null)}`
    : liveRun ? formatClock(proj.activeMs) : formatDuration(proj.activeMs)
  // 停在哪个节点、已经等了多久：和胶囊、面板、泳道、运行那一行同一个数（见 waitedMs）
  const waitedNow = trace.phase === 'waiting' && trace.waitingNodeId
    ? waitedMs(trace, trace.waitingNodeId, liveNow) : null

  const enterReplay = () => { patch({ playing: false }); onReplayAt(t0) }
  const goLive = () => { patch({ playing: false }); onReplayAt(null) }
  const togglePlay = () => {
    if (ui.playing) { patch({ playing: false }); return }
    // 播到头了再按播放：从头来
    if (replayAt == null || replayAt >= tEnd) onReplayAt(t0)
    patch({ playing: true })
  }
  const cycleSpeed = () => patch({ speed: ({ 1: 2, 2: 4, 4: 8 } as Record<number, number>)[ui.speed] ?? 1 })

  if (!ui.open) {
    return (
      <section className={clsx('tl tl-collapsed', className)} style={{ height: COLLAPSED }}
               data-live={live ? '1' : undefined} aria-label="航迹">
        <button type="button" className="tl-title" onClick={() => patch({ open: true })} title="展开航迹">
          <ChevronUp size={12} />
          <span>航迹</span>
        </button>
        <StatusBadge status={trace.phase === 'idle' ? 'idle' : trace.phase} size={12} animate={false} />
        <span className="tl-mini-clock tnum">{cursorLabel}</span>
        <MiniTrack trace={trace} t0={t0} t1={tAxisEnd} stamps={stamps} compress={ui.compress} at={cursor} />
        {!live && <span className="tl-tag">回放</span>}
        <button type="button" className="btn btn-ghost btn-xs" onClick={() => patch({ open: true })}>展开</button>
      </section>
    )
  }

  return (
    <section
      ref={section}
      className={clsx('tl', className)}
      style={{ height: ui.height }}
      data-live={live ? '1' : undefined}
      aria-label="航迹"
    >
      <div className="tl-grip" role="separator" aria-orientation="horizontal"
           aria-label="航迹高度（拖动或按上下键调整）" tabIndex={0}
           aria-valuenow={ui.height} aria-valuemin={MIN_H} aria-valuemax={Math.round(maxH())}
           onKeyDown={onGripKey}
           onPointerDown={onGripDown} onPointerMove={onGripMove} onPointerUp={onGripUp} onPointerCancel={onGripUp} />

      <header className="tl-head">
        <span className="tl-name">航迹</span>
        <div className="tl-seg" role="group" aria-label="实时或回放">
          <button type="button" className={clsx(live && 'is-on')} aria-pressed={live} onClick={goLive}
                  title="游标回到现在（End）">
            <Radio size={11} /> 实时
          </button>
          <button type="button" className={clsx(!live && 'is-on')} aria-pressed={!live} onClick={enterReplay}
                  title="从头回放：画布回到那一刻的样子（Home）">
            回放
          </button>
        </div>
        <button type="button" className="btn btn-ghost btn-xs" onClick={togglePlay}
                title={ui.playing ? '暂停' : '按时间顺序重放'} aria-label={ui.playing ? '暂停回放' : '播放回放'}>
          {ui.playing ? <Pause size={11} /> : <Play size={11} />}
        </button>
        <button type="button" className="btn btn-ghost btn-xs tnum" onClick={cycleSpeed} title="回放倍速">
          {ui.speed}×
        </button>
        <label className="tl-toggle" title="超过 2 分钟没有事件的空档压成一小段，斜纹里写着真实时长">
          <input type="checkbox" checked={ui.compress} aria-label="压缩空闲"
                 onChange={(e) => patch({ compress: e.target.checked })} />
          <FoldHorizontal size={11} /> <span className="tl-tt">压缩空闲</span>
        </label>
        {extra}
        <span className="flex-1" />
        {failed && trace.failedNodeId && (
          <button type="button" className="tl-alert" onClick={() => onFocusNode?.(trace.failedNodeId!)}
                  title="画布取景到失败的节点">
            <StatusBadge status="failed" size={11} animate={false} decorative />
            <span className="min-w-0 truncate">失败于「{labels.get(trace.failedNodeId) ?? trace.failedNodeId}」</span>
          </button>
        )}
        {trace.phase === 'waiting' && trace.waitingNodeId && (
          <button type="button" className="tl-alert is-warn" onClick={() => onFocusNode?.(trace.waitingNodeId!)}
                  title="画布取景到等审批的节点">
            <StatusBadge status="waiting" size={11} animate={false} decorative />
            <span className="min-w-0 truncate">
              停在「{labels.get(trace.waitingNodeId) ?? trace.waitingNodeId}」
              {waitedNow != null && <> · 已等 <span className="tnum">{formatClock(waitedNow)}</span></>}
            </span>
          </button>
        )}
        {!trace.timed && <span className="tl-note" title="这条运行的事件没有时间戳，段按先后顺序排、宽度只来自耗时">老数据 · 无时间戳</span>}
        <button type="button" className="btn btn-ghost btn-xs" onClick={() => patch({ open: false, playing: false })}
                title="收起航迹" aria-label="收起航迹">
          <span className="tl-tt">收起</span> <ChevronDown size={11} />
        </button>
      </header>

      <div className="tl-body" ref={body}>
        <div className="tl-scroll" ref={scroller} onScroll={onScroll} style={{ scrollPaddingTop: HEAD_H }}>
          {/* 刻度 + 并行度 + 运行本身：拖这三行就是拖游标 */}
          <div className="tl-sticky">
            <div className="tl-row tl-axis" style={{ height: AXIS_H }}>
              <div className="tl-label tl-dim">墙钟{scale.folds.length ? ' · 折叠处跳变' : ''}</div>
              <div className="tl-track tl-scrub" onPointerDown={onScrubDown} onPointerMove={onScrubMove}
                   onPointerUp={onScrubUp} onPointerCancel={onScrubUp}>
                {scale.ticks.map((tk) => (
                  <span key={tk.t} className={clsx('tl-tick tnum', tk.x + 44 > headL && tk.x < headR && 'is-covered')}
                        style={{ transform: `translateX(${tk.x}px)` }}>{tk.label}</span>
                ))}
                {scale.folds.map((f) => (
                  <span key={f.a} className="tl-fold-tag tnum" style={{ left: f.x0, width: f.x1 - f.x0 }}
                        title={`这 ${formatDuration(f.b - f.a)} 里没有任何事件，已压缩`}>
                    {foldLabel(f.b - f.a)}
                  </span>
                ))}
                <span
                  className={clsx('tl-cursor-head tnum', live ? 'is-live' : 'is-replay', flip && 'is-flip')}
                  style={{ transform: `translateX(${cursorX}px)` }}
                  role="slider"
                  tabIndex={0}
                  aria-label="回放游标"
                  aria-valuemin={0}
                  aria-valuemax={Math.round(tEnd - t0)}
                  aria-valuenow={Math.round(cursor - t0)}
                  aria-valuetext={cursorLabel}
                  onKeyDown={onCursorKey}
                >
                  {cursorLabel}
                </span>
              </div>
              {/* 回放时右边这一列按游标那一刻算，段本身画的是整次运行 */}
              <div className="tl-sum tl-dim">{live ? '耗时 · 用量' : '截至游标'}</div>
            </div>
            <div className="tl-row tl-par" style={{ height: PAR_H }}>
              <div className="tl-label tl-dim">并行度{peak > 0 && <span className="tnum"> · 峰值 {peak}</span>}</div>
              <div className="tl-track tl-scrub" onPointerDown={onScrubDown} onPointerMove={onScrubMove}
                   onPointerUp={onScrubUp} onPointerCancel={onScrubUp}>
                <ParallelCurve trace={trace} scale={scale} end={drawAt} height={PAR_H} peak={peak} />
              </div>
              <div className="tl-sum tnum">{proj.parallelNow ? `${proj.parallelNow} 路` : NONE}</div>
            </div>
            <div className="tl-row tl-run" style={{ height: RUN_H }}>
              <div className="tl-label" title={title}>{title ?? '运行'}</div>
              <div className="tl-track tl-scrub" onPointerDown={onScrubDown} onPointerMove={onScrubMove}
                   onPointerUp={onScrubUp} onPointerCancel={onScrubUp}>
                {trace.drives.map(([a, b], i) => {
                  const x0 = scale.x(a)
                  const w = Math.max(2, scale.x(b ?? drawAt) - x0)
                  // 宽度按 200ms 一格排，字按时钟走：还开着的这一段和右边、游标写同一个数
                  const text = b == null && running && live ? formatClock(liveNow - a) : formatDuration((b ?? drawAt) - a)
                  return (
                    <span key={`d${i}`} className="tl-bar tl-drive" style={{ left: x0, width: w }}>
                      {w > 70 && <em className="tnum">执行 {text}</em>}
                    </span>
                  )
                })}
                {trace.waits.map(([a, b], i) => {
                  const x0 = scale.x(a)
                  const w = Math.max(2, scale.x(b ?? drawAt) - x0)
                  // 还开着的这一段和执行段一样按时钟写：和坞头「已等」、右边合计、胶囊同一个数
                  const text = b == null && liveRun ? formatClock(liveNow - a) : formatDuration((b ?? drawAt) - a)
                  return (
                    <span key={`w${i}`} className="tl-bar tl-s-waiting tl-hatch" style={{ left: x0, width: w }}
                          title={`等人审批 ${text}`}>
                      {w > 70 && <em className="tnum">等人 {text}</em>}
                    </span>
                  )
                })}
                {isTerminal(trace.phase) && trace.endedAt != null && (
                  <span className={clsx('tl-end', `tl-end-${trace.phase}`)} style={{ left: scale.x(trace.endedAt) }}
                        title={`${phaseMeta.label} · T+${formatClock(trace.endedAt - t0)}`} />
                )}
              </div>
              <div className="tl-sum tnum" title={`执行 ${formatDuration(proj.activeMs)}${proj.waitMs > 0 ? ` · 等人 ${formatDuration(proj.waitMs)}` : ''}`}>
                {runSum}
              </div>
            </div>
            {/* 游标和"还没发生"的压暗分两截画：吸顶这三行一截、泳道一截。画成一整条的话，
                泳道滚动时它跟着滚上来，盖在游标头和刻度上 */}
            {!live && (
              <span className="tl-future" aria-hidden
                    style={{ left: LABEL_W + cursorX, width: Math.max(0, scale.width - cursorX), top: AXIS_H, height: PAR_H + RUN_H }} />
            )}
            {/* 竖线从游标头下沿开始：回放时游标头是描边的，线穿过去会划掉上面的数字 */}
            <div className={clsx('tl-cursor', live ? 'is-live' : 'is-replay')} aria-hidden
                 style={{ top: CURSOR_TOP, transform: `translateX(${LABEL_W + cursorX}px)`, height: HEAD_H - CURSOR_TOP }} />
          </div>

          <div className="tl-lanes" style={{ height: totalH }} role="group" aria-label="泳道：上下键切换，回车在画布上定位">
            {visible.map((row) => (
              <Lane key={row.key} row={row} scale={scale} at={drawAt} t0={t0}
                    hovered={hovered === row.nodeId} tabbable={row.key === tabLane}
                    waitNow={row.kind === 'node' && liveRun ? waitedMs(trace, row.nodeId, liveNow) : null}
                    onHover={onHoverNode} onPick={onFocusNode}
                    onTake={setLaneKey} onMove={moveLane} />
            ))}
          </div>

          {/* 折叠的空档：斜纹贯穿所有泳道——整张图在这段时间里都停着 */}
          {scale.folds.map((f) => (
            <span key={`f${f.a}`} className="tl-fold" aria-hidden
                  style={{ left: LABEL_W + f.x0, width: f.x1 - f.x0, height: HEAD_H + totalH }} />
          ))}
          {!live && (
            <span className="tl-future" aria-hidden
                  style={{ left: LABEL_W + cursorX, width: Math.max(0, scale.width - cursorX), top: HEAD_H, height: totalH }} />
          )}
          <div className={clsx('tl-cursor', live ? 'is-live' : 'is-replay')} aria-hidden
               style={{ top: HEAD_H, transform: `translateX(${LABEL_W + cursorX}px)`, height: totalH }} />
        </div>
      </div>
    </section>
  )
}

// -------------------------------------------------------------------------

function Lane({ row, scale, at, t0, hovered, tabbable, waitNow, onHover, onPick, onTake, onMove }: {
  row: Row
  scale: Scale
  at: number
  t0: number
  hovered: boolean
  /** 整片泳道只有这一条在 Tab 顺序里 */
  tabbable: boolean
  /** 这个节点此刻已经等了多久（实时、在等时才有）：开着的等待段按它写字 */
  waitNow: number | null
  onHover?: (id: string | null) => void
  onPick?: (id: string) => void
  onTake: (key: string) => void
  onMove: (from: string, to: number | 'first' | 'last') => void
}) {
  const sub = row.kind !== 'node'
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      onPick?.(row.nodeId)
    } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault()
      onMove(row.key, e.key === 'ArrowDown' ? 1 : -1)
    } else if (e.key === 'Home' || e.key === 'End') {
      e.preventDefault()
      onMove(row.key, e.key === 'Home' ? 'first' : 'last')
    }
  }
  return (
    <div
      className={clsx('tl-row tl-lane', sub && 'tl-sub', hovered && 'is-hover', `tl-lane-${row.state}`)}
      style={{ top: row.top, height: row.height }}
      data-node-id={row.nodeId}
      data-lane={row.key}
      role="button"
      tabIndex={tabbable ? 0 : -1}
      aria-label={`${row.name}：${statusMeta(row.state).label}${row.summary !== NONE ? ` · ${row.summary}` : ''}`}
      onMouseEnter={() => onHover?.(row.nodeId)}
      onMouseLeave={() => onHover?.(null)}
      onFocus={() => { onTake(row.key); onHover?.(row.nodeId) }}
      onBlur={() => onHover?.(null)}
      onKeyDown={onKeyDown}
      onClick={() => onPick?.(row.nodeId)}
    >
      <div className="tl-label" title={row.label}>
        {sub
          ? <span className="tl-sub-mark">{row.kind === 'dispatch' ? '◇' : '·'}</span>
          : <span className="tl-type" style={{ background: row.type ? `var(--nt-${row.type}, var(--text-faint))` : undefined }} />}
        <span className="truncate">{row.label}</span>
        {!sub && row.count > 1 && <span className="tl-count tnum">×{row.count}</span>}
      </div>
      <div className="tl-track">
        {row.note && <span className={clsx('tl-lane-note', row.state === 'blocked' && 'is-err')}>{row.note}</span>}
        {row.segs.map((s, i) => {
          const x0 = scale.x(s.start)
          if (s.kind === 'retry') {
            return <span key={i} className="tl-retry" style={{ left: x0 }} title={segTitle(row, s, t0, at)} />
          }
          if (s.status === 'skipped' && s.end === s.start) {
            return <span key={i} className="tl-skip" style={{ left: x0 }} title={`${row.label} · 已跳过`} />
          }
          const end = s.end == null ? at : Math.min(s.end, Math.max(at, s.start))
          const w = Math.max(3, scale.x(end) - x0)
          const ms = s.kind === 'run' && s.end != null && row.measuredMs != null ? row.measuredMs : end - s.start
          const text = s.kind === 'wait'
            ? `等待审批 ${s.end == null && waitNow != null ? formatClock(waitNow) : formatClock(end - s.start).replace(/\.\d$/, '')}`
            : s.kind === 'dispatch'
              ? `调度 ${formatDuration(end - s.start)}${s.estimated ? ' · 推算' : ''}`
              : `${s.iteration != null && row.kind === 'node' ? `#${s.iteration} · ` : ''}${formatDuration(ms)}`
          return (
            <span key={i} className={clsx('tl-bar', segClass(s), s.end == null && 'is-open')}
                  style={{ left: x0, width: w }} title={segTitle(row, s, t0, at)}>
              {w > 64 && <em className="tnum">{text}</em>}
            </span>
          )
        })}
      </div>
      <div className="tl-sum tnum" title={row.summary}>{row.summary}</div>
    </div>
  )
}

/** 并行度曲线：任一时刻开着的执行段数的阶梯面积图 */
function ParallelCurve({ trace, scale, end, height, peak }: {
  trace: Trace; scale: Scale; end: number; height: number; peak: number
}) {
  if (!trace.parallelSeries.length || !scale.width) return null
  const top = Math.max(1, peak)
  const y = (v: number) => height - 3 - (v / top) * (height - 7)
  let d = `M0 ${y(0)}`
  for (const [ts, v] of trace.parallelSeries) d += ` H${scale.x(ts).toFixed(1)} V${y(v).toFixed(1)}`
  d += ` H${scale.x(end).toFixed(1)}`
  const area = `${d} V${height} H0 Z`
  return (
    <svg className="tl-par-svg" width={scale.width} height={height} aria-hidden>
      <path d={area} className="tl-par-area" />
      <path d={d} className="tl-par-line" />
    </svg>
  )
}

/** 收起时那条缩略：所有泳道的段叠成一条，看得出形状、等人和失败 */
function MiniTrack({ trace, t0, t1, stamps, compress, at }: {
  trace: Trace; t0: number; t1: number; stamps: number[]; compress: boolean; at: number
}) {
  const box = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(0)
  useLayoutEffect(() => {
    const el = box.current
    if (!el) return
    const measure = () => setWidth(el.clientWidth)
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  const scale = useMemo(() => buildScale(t0, t1, width, stamps, compress), [t0, t1, width, stamps, compress])
  const bars: { x: number; w: number; cls: string }[] = []
  for (const n of Object.values(trace.nodes)) {
    for (const s of n.segments) {
      if ((s.kind !== 'run' && s.kind !== 'wait') || s.status === 'skipped') continue
      const x = scale.x(s.start)
      bars.push({ x, w: Math.max(2, scale.x(s.end ?? at) - x), cls: segClass(s) })
    }
  }
  return (
    <div className="tl-mini" ref={box} aria-hidden>
      {bars.map((b, i) => <span key={i} className={clsx('tl-mini-bar', b.cls)} style={{ left: b.x, width: b.w }} />)}
      <span className="tl-mini-cursor" style={{ transform: `translateX(${scale.x(at)}px)` }} />
    </div>
  )
}
