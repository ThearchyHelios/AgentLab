/**
 * 循环体底框：循环节点和它的循环体圈在一块淡色区域里，左上角写「循环体 · 最多 N 轮」。
 *
 * 回边有了虚线和 ↺，读得出「从哪回到循环头」；可哪些节点每轮都要跑一遍、最多跑几轮，
 * 以前只能顺着回边一个个倒推。嵌套循环框套框，外层留得宽一圈。
 *
 * 框不是一个大矩形：循环体旁边常有同列、却不在循环里的节点（循环结束后才跑的那一支），
 * 包围盒会把它们也圈进去。这里按列各圈各的，列与列之间用它们纵向重叠的那一段接起来，
 * 得到一块阶梯形的区域。
 */
import type { NodeType } from '../types'
import { NODE_WIDTH } from './routing'

export interface LoopNodeLike {
  id: string
  position: { x: number; y: number }
  measured?: { width?: number; height?: number }
  data: { nodeType: NodeType; config: Record<string, any> }
}

export interface LoopEdgeLike {
  source: string
  target: string
  sourceHandle?: string | null
}

export interface LoopFrame {
  /** 循环节点的 id */
  id: string
  /** 轮廓，画布坐标 */
  path: string
  /** 标签落点：轮廓左上角那条上边 */
  labelX: number
  labelY: number
  /** 每进来一次最多几轮（后端 control.run_loop 的 max_iterations，缺省 10） */
  max: number
  /** 圈在里面的节点，含循环节点本身 */
  members: string[]
}

/** 最内层的框离卡片留多宽；每多包一层再外扩这么多 */
const PAD = 16
const NEST = 14
/** 两列的 x 差在这以内算同一列（和 routing 的 COLUMN_TOLERANCE 一致） */
const COLUMN_TOLERANCE = 60
/** 相邻两列纵向不重叠时，接起来的那一段至少伸进两边这么多 */
const BRIDGE = 24
const RADIUS = 10

/**
 * 每个循环节点的循环体（不含它自己），和后端 schema.loop_bodies 同一个口径：
 * 从非 done 出口出发、不经过循环节点能走到，又能绕回它的那些节点
 */
export function loopBodies(nodes: LoopNodeLike[], edges: LoopEdgeLike[]): Map<string, Set<string>> {
  const out = new Map<string, Set<string>>()
  const loops = nodes.filter((n) => n.data.nodeType === 'loop')
  if (!loops.length) return out
  const succ = new Map<string, string[]>()
  const pred = new Map<string, string[]>()
  for (const e of edges) {
    succ.set(e.source, [...(succ.get(e.source) ?? []), e.target])
    pred.set(e.target, [...(pred.get(e.target) ?? []), e.source])
  }
  const reach = (starts: string[], step: Map<string, string[]>, avoid: string): Set<string> => {
    const seen = new Set<string>()
    const stack = starts.filter((s) => s !== avoid)
    while (stack.length) {
      const id = stack.pop()!
      if (seen.has(id)) continue
      seen.add(id)
      for (const x of step.get(id) ?? []) if (x !== avoid) stack.push(x)
    }
    return seen
  }
  for (const loop of loops) {
    const entries = edges.filter((e) => e.source === loop.id && e.sourceHandle !== 'done').map((e) => e.target)
    const forward = reach(entries, succ, loop.id)
    const back = reach(pred.get(loop.id) ?? [], pred, loop.id)
    out.set(loop.id, new Set([...forward].filter((id) => back.has(id))))
  }
  return out
}

interface Rect { l: number; r: number; t: number; b: number }

/** 闭合折线画成带圆角的路径 */
function closedRounded(points: { x: number; y: number }[]): string {
  const n = points.length
  let d = ''
  for (let i = 0; i < n; i++) {
    const prev = points[(i - 1 + n) % n]
    const cur = points[i]
    const next = points[(i + 1) % n]
    const inLen = Math.hypot(cur.x - prev.x, cur.y - prev.y)
    const outLen = Math.hypot(next.x - cur.x, next.y - cur.y)
    const r = Math.min(RADIUS, inLen / 2, outLen / 2)
    const a = { x: cur.x - ((cur.x - prev.x) / inLen) * r, y: cur.y - ((cur.y - prev.y) / inLen) * r }
    const b = { x: cur.x + ((next.x - cur.x) / outLen) * r, y: cur.y + ((next.y - cur.y) / outLen) * r }
    d += `${i ? 'L' : 'M'}${a.x} ${a.y}Q${cur.x} ${cur.y} ${b.x} ${b.y}`
  }
  return `${d}Z`
}

/** 一组矩形（按 x 排好、相邻的已经接上）的并集轮廓。每个竖直切片里只有一段 */
function outline(rects: Rect[]): { path: string; x: number; y: number } | null {
  const xs = [...new Set(rects.flatMap((r) => [r.l, r.r]))].sort((a, b) => a - b)
  const slabs: Rect[] = []
  for (let i = 0; i + 1 < xs.length; i++) {
    const l = xs[i]
    const r = xs[i + 1]
    const over = rects.filter((x) => x.l <= l + 0.01 && x.r >= r - 0.01)
    if (!over.length) continue
    const t = Math.min(...over.map((x) => x.t))
    const b = Math.max(...over.map((x) => x.b))
    const last = slabs[slabs.length - 1]
    if (last && Math.abs(last.r - l) < 0.01 && Math.abs(last.t - t) < 0.01 && Math.abs(last.b - b) < 0.01) last.r = r
    else slabs.push({ l, r, t, b })
  }
  if (!slabs.length) return null
  const pts: { x: number; y: number }[] = []
  for (const s of slabs) pts.push({ x: s.l, y: s.t }, { x: s.r, y: s.t })
  for (let i = slabs.length - 1; i >= 0; i--) pts.push({ x: slabs[i].r, y: slabs[i].b }, { x: slabs[i].l, y: slabs[i].b })
  // 去掉重合点和共线的中间点，圆角才算得对
  const clean: { x: number; y: number }[] = []
  for (const p of pts) {
    const last = clean[clean.length - 1]
    if (!last || Math.abs(last.x - p.x) > 0.01 || Math.abs(last.y - p.y) > 0.01) clean.push(p)
  }
  if (clean.length > 1 && Math.abs(clean[0].x - clean[clean.length - 1].x) < 0.01
      && Math.abs(clean[0].y - clean[clean.length - 1].y) < 0.01) clean.pop()
  const corners = clean.filter((p, i) => {
    const a = clean[(i - 1 + clean.length) % clean.length]
    const b = clean[(i + 1) % clean.length]
    return !((Math.abs(a.x - p.x) < 0.01 && Math.abs(b.x - p.x) < 0.01) || (Math.abs(a.y - p.y) < 0.01 && Math.abs(b.y - p.y) < 0.01))
  })
  if (corners.length < 4) return null
  return { path: closedRounded(corners), x: slabs[0].l, y: slabs[0].t }
}

export function loopFrames(nodes: LoopNodeLike[], edges: LoopEdgeLike[]): LoopFrame[] {
  const bodies = loopBodies(nodes, edges)
  if (!bodies.size) return []
  const byId = new Map(nodes.map((n) => [n.id, n]))
  // 每个循环里面还包着几层循环：外层按它往外扩，框套框不贴在一起
  const inner = new Map<string, number>()
  const depthOf = (id: string, seen: Set<string>): number => {
    if (inner.has(id)) return inner.get(id)!
    let d = 0
    for (const m of bodies.get(id) ?? []) {
      if (m !== id && bodies.has(m) && !seen.has(m)) d = Math.max(d, depthOf(m, new Set([...seen, id])) + 1)
    }
    inner.set(id, d)
    return d
  }
  const frames: LoopFrame[] = []
  for (const [id, body] of bodies) {
    if (!body.size) continue
    const loop = byId.get(id)
    if (!loop) continue
    const pad = PAD + depthOf(id, new Set()) * NEST
    const members = [id, ...body].map((m) => byId.get(m)).filter((n): n is LoopNodeLike => !!n)
    // 按列圈：同一列的上下合成一段
    const cols: Rect[] = []
    for (const n of [...members].sort((a, b) => a.position.x - b.position.x)) {
      const w = n.measured?.width || NODE_WIDTH
      const h = n.measured?.height || 80
      const box = { l: n.position.x, r: n.position.x + w, t: n.position.y, b: n.position.y + h }
      const last = cols[cols.length - 1]
      if (last && box.l - last.l < COLUMN_TOLERANCE) {
        last.r = Math.max(last.r, box.r)
        last.t = Math.min(last.t, box.t)
        last.b = Math.max(last.b, box.b)
      } else cols.push(box)
    }
    const boxes: Rect[] = cols.map((c) => ({ l: c.l - pad, r: c.r + pad, t: c.t - pad, b: c.b + pad }))
    // 相邻两列之间接一段：纵向有重叠就取重叠那段，没有就跨过缺口、两头各伸进去一点
    const bridges: Rect[] = []
    for (let i = 0; i + 1 < boxes.length; i++) {
      const a = boxes[i]
      const b = boxes[i + 1]
      const t = Math.max(a.t, b.t)
      const bottom = Math.min(a.b, b.b)
      bridges.push(t < bottom
        ? { l: a.r, r: b.l, t, b: bottom }
        : { l: a.r, r: b.l, t: Math.min(a.b, b.b) - BRIDGE, b: Math.max(a.t, b.t) + BRIDGE })
    }
    const shape = outline([...boxes, ...bridges.filter((r) => r.r > r.l)])
    if (!shape) continue
    const max = Number(loop.data.config?.max_iterations) || 10
    frames.push({ id, path: shape.path, labelX: shape.x + 12, labelY: shape.y, max, members: members.map((m) => m.id) })
  }
  // 外层先画：里层的框叠在上面
  return frames.sort((a, b) => b.members.length - a.members.length)
}
