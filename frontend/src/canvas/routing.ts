/**
 * 画布的连线走线。
 *
 * 背景：以前所有边都交给 React Flow 的 smoothstep，它的竖直段一律落在
 * 源和目标的正中间。于是同一列的边全都挤在同一个 x 上：一个节点扇出三条，
 * 三条线的竖段完全重合；两条边汇进同一个节点，末段又重合。看上去就是
 * 一团线糊在一起，分不清哪条连到哪。
 *
 * 这里的做法是给每条线安排自己的车道：
 *
 * 1. **端口**：一个节点的多条出边沿它的右边缘错开，多条入边沿左边缘错开。
 *    线不再从同一个点出发、也不在同一个点结束。入边的顺序按上游的纵向
 *    位置排，所以同一列里的线不会互相交叉。
 * 2. **走廊与车道**：相邻两列之间是一条走廊，走廊里竖直走线按区间染色
 *    分配车道——竖直区间真的叠在一起的才各占一条，首尾相接的（同一个扇出
 *    点）共用一条，看起来就是一条总线，而不是硬生生拆成三条平行线。
 * 3. **长边与回边**：跨多列的长边不走直线穿堂，而是找一条横向上没有节点的
 *    空档绕过去；循环的回边同样从图的上方或下方兜一圈。
 *
 * 走廊宽度由后端 `engine/layout.py` 按车道数预留，两边算车道用的是同一套
 * 贪心（见 `_lane_count`），所以留出来的宽度够用。
 */

import { sourceHandles, type HandleDef } from './nodeDefs'
import type { NodeType } from '../types'

/** 节点宽度。NodeCard 里写死的 `width: 238`，两边必须一致。 */
export const NODE_WIDTH = 238
/** 端口在节点边缘外的偏移。和 NodeCard 里 Handle 的 `right/left: -5` 对齐。 */
const SOURCE_PORT_DX = 5
const TARGET_PORT_DX = -5
/** 竖直车道间距，与 layout.py 的 LANE 一致。 */
const LANE = 18
/** 连线离开节点后至少先走这么远再拐弯。 */
const STUB = 14
/** 圆角半径。 */
const RADIUS = 8
/** 竖直区间重叠多少以内算同一条道（和 layout.py 的 TOLERANCE 对齐）。 */
const LANE_TOLERANCE = 6
/** 绕行时离节点框至少留的空隙。 */
const BAND_PAD = 12
/** 两列的中心距离小于它就算同一列。用户手拖过的节点不至于被拆成两列。 */
const COLUMN_TOLERANCE = 60

export interface Point {
  x: number
  y: number
}

export interface Route {
  path: string
  labelX: number
  labelY: number
}

/** buildRoutes 需要的节点信息，结构上就是 store 里的 FlowNode。 */
export interface RouteNodeLike {
  id: string
  position: { x: number; y: number }
  measured?: { width?: number; height?: number }
  data: { nodeType: NodeType; config: Record<string, any> }
}

export interface RouteEdgeLike {
  id: string
  source: string
  target: string
  sourceHandle?: string | null
}

interface Geometry {
  id: string
  x: number
  y: number
  width: number
  height: number
  centerY: number
  right: number
  left: number
  bottom: number
  nodeType: NodeType
  config: Record<string, any>
  handles: HandleDef[]
}

interface Column {
  left: number
  right: number
  /** 列里最大的节点 x，用来判断下一个节点还算不算这一列 */
  lastX: number
  members: Geometry[]
}

interface Segment {
  edgeId: string
  start: number
  end: number
  lane: number
}

/** 端口在一组边里的纵向偏移。一条边时就是原位，多了才错开。 */
function spreadOffsets(count: number, height: number): number[] {
  if (count <= 1) return [0]
  const spacing = Math.max(7, Math.min(14, (height - 34) / (count - 1)))
  const total = spacing * (count - 1)
  return Array.from({ length: count }, (_, i) => -total / 2 + i * spacing)
}

function toGeometry(node: RouteNodeLike): Geometry {
  // 用 || 而不是 ??：React Flow 还没量到尺寸时会先给 0，那会把端口算到
  // 节点外面去。宁可退回写死的默认值。
  const width = node.measured?.width || NODE_WIDTH
  const height = node.measured?.height || 74
  const x = node.position.x
  const y = node.position.y
  return {
    id: node.id,
    x,
    y,
    width,
    height,
    centerY: y + height / 2,
    right: x + width,
    left: x,
    bottom: y + height,
    nodeType: node.data.nodeType,
    config: node.data.config ?? {},
    handles: sourceHandles(node.data.nodeType, node.data.config ?? {}),
  }
}

/**
 * 出口的纵向位置。多出口的节点（branch / loop / human）每个出口在卡片右侧
 * 各有各的位置，单出口的节点所有出边共用一个点——和 NodeCard 画 Handle 的
 * 算式必须一致，不然线会从卡片外面接进来。
 */
function handleBaseY(node: Geometry, handleId: string): number {
  const handles = node.handles
  if (handles.length <= 1) return node.centerY
  const index = Math.max(0, handles.findIndex((h) => h.id === handleId))
  return node.y + (node.height * (index + 1)) / (handles.length + 1)
}

function resolveHandleId(node: Geometry, raw?: string | null): string {
  if (raw && node.handles.some((h) => h.id === raw)) return raw
  return node.handles[0]?.id ?? 'out'
}

/** 合并靠得太近的 x，得到"列"。用户随手拖过的节点不该被当成新的一列。 */
function buildColumns(geoms: Geometry[]): { columns: Column[]; colOf: Map<string, number> } {
  const columns: Column[] = []
  const colOf = new Map<string, number>()
  for (const g of [...geoms].sort((a, b) => a.x - b.x)) {
    const last = columns[columns.length - 1]
    if (last && g.x - last.lastX < COLUMN_TOLERANCE) {
      last.right = Math.max(last.right, g.right)
      last.left = Math.min(last.left, g.left)
      last.lastX = Math.max(last.lastX, g.x)
      last.members.push(g)
    } else {
      columns.push({ left: g.left, right: g.right, lastX: g.x, members: [g] })
    }
    colOf.set(g.id, columns.length - 1)
  }
  return { columns, colOf }
}

/**
 * 走廊里的车道分配：按起点排序做首次适应染色。
 *
 * 竖直区间真的叠在一起才换道；首尾相接（同一个扇出点的三条线）共用一条，
 * 这样扇出看起来是一条总线，而不是三条贴在一起的平行线。
 */
function assignLanes(segments: Segment[]): number {
  const lanes: number[] = []
  for (const seg of [...segments].sort((a, b) => a.start - b.start)) {
    let placed = -1
    for (let i = 0; i < lanes.length; i++) {
      if (seg.start >= lanes[i] - LANE_TOLERANCE) {
        lanes[i] = Math.max(lanes[i], seg.end)
        placed = i
        break
      }
    }
    if (placed < 0) {
      lanes.push(seg.end)
      placed = lanes.length - 1
    }
    seg.lane = placed
  }
  return lanes.length
}

/** 车道在走廊里的 x。整束车道居中，走廊窄的时候自动压缩间距。 */
function laneX(column: Column, next: Column, lane: number, total: number): number {
  const left = column.right
  const avail = Math.max(0, next.left - left)
  if (total <= 1) return left + avail / 2
  const gap = Math.max(6, Math.min(LANE, (avail - STUB * 2) / (total - 1)))
  const span = gap * (total - 1)
  return left + (avail - span) / 2 + lane * gap
}

/**
 * 找一条横着穿过去不会撞到节点的空档。
 *
 * 长边如果直接从源的高度平推过去，会横穿中间那些列的卡片——这正是以前
 * 画布上最难看的地方。所以在中间各列的纵向占用里找空隙，挑离理想高度最近
 * 的那条走。
 */
function freeBand(
  columns: Column[], from: number, to: number, ideal: number, used: number[],
): number {
  const lo = Math.min(from, to)
  const hi = Math.max(from, to)
  const occupied: [number, number][] = []
  for (let c = lo; c <= hi; c++) {
    for (const g of columns[c]?.members ?? []) {
      occupied.push([g.y - BAND_PAD, g.bottom + BAND_PAD])
    }
  }
  occupied.sort((a, b) => a[0] - b[0])
  const merged: [number, number][] = []
  for (const iv of occupied) {
    const last = merged[merged.length - 1]
    if (last && iv[0] <= last[1]) last[1] = Math.max(last[1], iv[1])
    else merged.push([iv[0], iv[1]])
  }

  const gaps: [number, number][] = []
  let cursor = (merged[0]?.[0] ?? ideal) - 260
  for (const iv of merged) {
    gaps.push([cursor, iv[0]])
    cursor = Math.max(cursor, iv[1])
  }
  gaps.push([cursor, cursor + 260])

  const pick = (y: number): number | null => {
    for (const [g0, g1] of gaps) {
      if (g1 - g0 < 24) continue
      if (y >= g0 + BAND_PAD && y <= g1 - BAND_PAD) return y
    }
    return null
  }

  // 从理想高度出发，先在最近的空档里找位置；和别的绕行线撞了就顺着空档挪开
  const candidates = [ideal]
  for (let step = 1; step <= 6; step++) {
    candidates.push(ideal + step * 22, ideal - step * 22)
  }
  for (const y of candidates) {
    const spot = pick(y)
    if (spot === null) continue
    if (used.every((u) => Math.abs(u - spot) >= 16)) return spot
  }
  for (const y of candidates) {
    const spot = pick(y)
    if (spot !== null) return spot
  }
  return ideal
}

/** 把折线画成带圆角的路径。 */
function roundedPath(points: Point[]): string {
  const pts: Point[] = []
  for (const p of points) {
    const last = pts[pts.length - 1]
    if (!last || Math.abs(last.x - p.x) > 0.5 || Math.abs(last.y - p.y) > 0.5) pts.push(p)
  }
  if (pts.length < 2) return ''
  let d = `M${pts[0].x} ${pts[0].y}`
  for (let i = 1; i < pts.length - 1; i++) {
    const prev = pts[i - 1]
    const cur = pts[i]
    const next = pts[i + 1]
    const inLen = Math.hypot(cur.x - prev.x, cur.y - prev.y)
    const outLen = Math.hypot(next.x - cur.x, next.y - cur.y)
    const r = Math.min(RADIUS, inLen / 2, outLen / 2)
    if (r < 0.5) {
      d += `L${cur.x} ${cur.y}`
      continue
    }
    const a = {
      x: cur.x - ((cur.x - prev.x) / inLen) * r,
      y: cur.y - ((cur.y - prev.y) / inLen) * r,
    }
    const b = {
      x: cur.x + ((next.x - cur.x) / outLen) * r,
      y: cur.y + ((next.y - cur.y) / outLen) * r,
    }
    d += `L${a.x} ${a.y}Q${cur.x} ${cur.y} ${b.x} ${b.y}`
  }
  const last = pts[pts.length - 1]
  d += `L${last.x} ${last.y}`
  return d
}

/** 折线中点，标签摆这儿。 */
function polylineMidpoint(points: Point[]): Point {
  const lengths: number[] = []
  let total = 0
  for (let i = 1; i < points.length; i++) {
    const len = Math.hypot(points[i].x - points[i - 1].x, points[i].y - points[i - 1].y)
    lengths.push(len)
    total += len
  }
  if (total === 0) return points[0]
  let target = total / 2
  for (let i = 0; i < lengths.length; i++) {
    if (target <= lengths[i]) {
      const t = lengths[i] === 0 ? 0 : target / lengths[i]
      return {
        x: points[i].x + (points[i + 1].x - points[i].x) * t,
        y: points[i].y + (points[i + 1].y - points[i].y) * t,
      }
    }
    target -= lengths[i]
  }
  return points[points.length - 1]
}

export function buildRoutes(
  nodes: RouteNodeLike[], edges: RouteEdgeLike[],
): Map<string, Route> {
  const geoms = nodes.map(toGeometry)
  const byId = new Map(geoms.map((g) => [g.id, g]))
  const routes = new Map<string, Route>()
  if (!geoms.length) return routes

  const { columns, colOf } = buildColumns(geoms)

  // ---- 1. 端口：出边沿右边缘错开，入边沿左边缘错开 ----
  const sourceY = new Map<string, number>()
  const outGroups = new Map<string, RouteEdgeLike[]>()
  const inGroups = new Map<string, RouteEdgeLike[]>()
  for (const e of edges) {
    const source = byId.get(e.source)
    const target = byId.get(e.target)
    if (!source || !target) continue
    const key = `${e.source}\u0000${resolveHandleId(source, e.sourceHandle)}`
    outGroups.set(key, [...(outGroups.get(key) ?? []), e])
    inGroups.set(e.target, [...(inGroups.get(e.target) ?? []), e])
  }

  for (const [key, group] of outGroups) {
    const [nodeId, handleId] = key.split('\u0000')
    const node = byId.get(nodeId)!
    const base = handleBaseY(node, handleId)
    // 同一条出边上，目标在上面的先出去——扇出的顺序才不会交叉
    const ordered = [...group].sort((a, b) => {
      const ta = byId.get(a.target)!
      const tb = byId.get(b.target)!
      return ta.centerY - tb.centerY || a.target.localeCompare(b.target)
    })
    const offsets = spreadOffsets(ordered.length, node.height)
    ordered.forEach((e, i) => sourceY.set(e.id, base + offsets[i]))
  }

  const targetY = new Map<string, number>()
  for (const [nodeId, group] of inGroups) {
    const node = byId.get(nodeId)!
    const ordered = [...group].sort((a, b) => {
      const sa = sourceY.get(a.id) ?? byId.get(a.source)!.centerY
      const sb = sourceY.get(b.id) ?? byId.get(b.source)!.centerY
      return sa - sb || a.source.localeCompare(b.source)
    })
    const offsets = spreadOffsets(ordered.length, node.height)
    ordered.forEach((e, i) => targetY.set(e.id, node.centerY + offsets[i]))
  }

  // ---- 2. 分类 + 绕行高度 ----
  interface Plan {
    edge: RouteEdgeLike
    kind: 'short' | 'long' | 'back' | 'self'
    colS: number
    colT: number
    sx: number
    sy: number
    tx: number
    ty: number
    band: number
  }
  const plans: Plan[] = []
  const usedBands: number[] = []
  // 先算跨得最远的边：它们的绕行高度要避开彼此
  const bySpan = [...edges].sort((a, b) => {
    const ca = Math.abs((colOf.get(a.target) ?? 0) - (colOf.get(a.source) ?? 0))
    const cb = Math.abs((colOf.get(b.target) ?? 0) - (colOf.get(b.source) ?? 0))
    return cb - ca
  })
  for (const e of bySpan) {
    const source = byId.get(e.source)
    const target = byId.get(e.target)
    if (!source || !target) continue
    const colS = colOf.get(e.source)!
    const colT = colOf.get(e.target)!
    const sy = sourceY.get(e.id) ?? source.centerY
    const ty = targetY.get(e.id) ?? target.centerY
    const kind: Plan['kind'] =
      e.source === e.target ? 'self'
        : colT === colS ? 'self'
          : colT < colS ? 'back'
            : colT === colS + 1 ? 'short' : 'long'
    const ideal = (sy + ty) / 2
    let band = 0
    if (kind === 'long' || kind === 'back') {
      // 横着穿过去的那一段到底压过哪几列：长边从源列右侧出发、停在目标列
      // 左侧的车道里，压到的是中间那几列；回边往左走，连源列和目标列一起压。
      // 算准了才不会为了躲开根本没压到的卡片，白白绕一大圈。
      const from = kind === 'long' ? colS + 1 : colT
      const to = kind === 'long' ? colT - 1 : colS
      band = freeBand(columns, from, to, ideal, usedBands)
      usedBands.push(band)
    }
    plans.push({
      edge: e, kind, colS, colT,
      sx: source.right + SOURCE_PORT_DX, sy,
      tx: target.left + TARGET_PORT_DX, ty,
      band,
    })
  }

  // ---- 3. 车道：每条竖直段登记到它所在的走廊 ----
  const corridorSegments = new Map<number, Segment[]>()
  const push = (corridor: number, edgeId: string, start: number, end: number) => {
    if (corridor < 0 || corridor >= columns.length - 1) return
    corridorSegments.set(corridor, [
      ...(corridorSegments.get(corridor) ?? []),
      { edgeId, start: Math.min(start, end), end: Math.max(start, end), lane: 0 },
    ])
  }
  for (const plan of plans) {
    if (plan.kind === 'short') {
      push(plan.colS, plan.edge.id, plan.sy, plan.ty)
    } else if (plan.kind === 'long' || plan.kind === 'back') {
      push(plan.colS, plan.edge.id, plan.sy, plan.band)
      push(plan.colT - 1, plan.edge.id, plan.band, plan.ty)
    }
  }
  const corridorLanes = new Map<number, number>()
  const laneOf = new Map<string, number>()
  for (const [corridor, segments] of corridorSegments) {
    const total = assignLanes(segments)
    corridorLanes.set(corridor, total)
    for (const seg of segments) laneOf.set(`${seg.edgeId}\u0000${corridor}`, seg.lane)
  }

  const laneAt = (corridor: number, edgeId: string): number | null => {
    if (corridor < 0 || corridor >= columns.length - 1) return null
    const lane = laneOf.get(`${edgeId}\u0000${corridor}`) ?? 0
    return laneX(columns[corridor], columns[corridor + 1], lane, corridorLanes.get(corridor) ?? 1)
  }

  // ---- 4. 出路径 ----
  for (const plan of plans) {
    let points: Point[]
    if (plan.kind === 'short') {
      const x = laneAt(plan.colS, plan.edge.id) ?? (plan.sx + plan.tx) / 2
      points = [
        { x: plan.sx, y: plan.sy },
        { x, y: plan.sy },
        { x, y: plan.ty },
        { x: plan.tx, y: plan.ty },
      ]
    } else if (plan.kind === 'long' || plan.kind === 'back') {
      const outX = laneAt(plan.colS, plan.edge.id)
      const inX = laneAt(plan.colT - 1, plan.edge.id)
      const startX = outX ?? plan.sx + STUB
      const endX = inX ?? plan.tx - 26
      points = [
        { x: plan.sx, y: plan.sy },
        { x: startX, y: plan.sy },
        { x: startX, y: plan.band },
        { x: endX, y: plan.band },
        { x: endX, y: plan.ty },
        { x: plan.tx, y: plan.ty },
      ]
    } else {
      // 自环：贴着卡片右边出去，从下面兜回来
      const node = byId.get(plan.edge.source)!
      const outX = node.right + 26
      const inX = node.left - 26
      const band = node.bottom + 34
      points = [
        { x: plan.sx, y: plan.sy },
        { x: outX, y: plan.sy },
        { x: outX, y: band },
        { x: inX, y: band },
        { x: inX, y: plan.ty },
        { x: plan.tx, y: plan.ty },
      ]
    }
    const mid = polylineMidpoint(points)
    routes.set(plan.edge.id, {
      path: roundedPath(points),
      labelX: mid.x,
      labelY: mid.y,
    })
  }

  return routes
}
