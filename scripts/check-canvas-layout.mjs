// 画布排版 + 连线走线的自动检查：真后端排版，真前端走线，然后按几何算。
//
// 这一层守的是"图好不好看"里唯一能算的那部分：线有没有叠在一起、长边有没有
// 从卡片中间穿过去、线有没有接在节点外面。check-ui / e2e-check 只看得到
// "页面没报错、节点在、能点"，连线叠成一团它们一个字都不会说。
//
// 跑之前前端得起着（./scripts/dev.sh），因为走线模块是 Vite 现编的 TS：
// 检查脚本直接 import('/src/canvas/routing.ts')，用的是页面上跑的那份代码，
// 不是抄一遍逻辑——抄一遍就等于只测了抄的那份。
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const API = process.env.AGENTLAB_API ?? 'http://localhost:8000/api'
const CHROME = process.env.CHROME_PATH
  ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'

let failed = 0
const check = (name, cond, detail = '') => {
  console.log(`  ${cond ? '✓' : '✗'} ${name}${detail ? ` — ${detail}` : ''}`)
  if (!cond) failed++
}

// ---------------------------------------------------------------- 夹具

const node = (id, type, config = {}) => ({ id, type, data: { label: id, config } })
const edge = (source, target, sourceHandle) => ({ source, target, sourceHandle })

/** 三路并行 → 归并 → 分支，再带一条跨层长边 */
const PARALLEL = {
  nodes: [
    node('in', 'input'), node('a1', 'agent'), node('a2', 'agent'), node('a3', 'agent'),
    node('m1', 'llm'), node('m2', 'llm'), node('join', 'llm'),
    node('gate', 'branch', { cases: [{ key: 'yes' }, { key: 'no' }] }),
    node('fix', 'llm'), node('ok', 'output'), node('bad', 'output'),
  ],
  edges: [
    edge('in', 'a1'), edge('in', 'a2'), edge('in', 'a3'),
    edge('a1', 'm1'), edge('a2', 'm1'), edge('a3', 'm2'),
    edge('m1', 'join'), edge('m2', 'join'), edge('a3', 'join'),
    edge('join', 'gate'),
    edge('gate', 'fix', 'no'), edge('gate', 'ok', 'yes'),
    edge('fix', 'ok'), edge('fix', 'bad'),
  ],
}

/** 一路扇出 8 条并行支路再全部汇进一个节点：车道压力最大的形状 */
const WIDE = {
  nodes: [node('in', 'input'), node('join', 'llm'),
    ...Array.from({ length: 8 }, (_, i) => node(`m${i}`, 'llm'))],
  edges: [
    ...Array.from({ length: 8 }, (_, i) => edge('in', `m${i}`)),
    ...Array.from({ length: 8 }, (_, i) => edge(`m${i}`, 'join')),
  ],
}

/** 循环：一条回边要从整张图外面兜回去 */
const LOOP = {
  nodes: [
    node('start', 'input'), node('each', 'loop'), node('body', 'llm'),
    node('collect', 'transform'), node('done', 'output'),
  ],
  edges: [
    edge('start', 'each'), edge('each', 'body', 'body'), edge('body', 'collect'),
    edge('collect', 'each'), edge('each', 'done', 'done'),
  ],
}

/** 分支的两个出口又汇到同一个节点：入端口错开得对不对就看它 */
const MERGE_BRANCH = {
  nodes: [
    node('start', 'input'),
    node('gate', 'branch', { cases: [{ key: 'yes' }, { key: 'no' }] }),
    node('left', 'llm'), node('right', 'llm'), node('done', 'output'),
  ],
  edges: [
    edge('start', 'gate'), edge('gate', 'left', 'yes'), edge('gate', 'right', 'no'),
    edge('left', 'done'), edge('right', 'done'),
  ],
}

/** 两个出口直接连到同一个节点：端口错开最极端的情况（起点终点都只差一点） */
const BOTH_WAYS = {
  nodes: [
    node('start', 'input'),
    node('gate', 'branch', { cases: [{ key: 'yes' }, { key: 'no' }] }),
    node('done', 'output'),
  ],
  edges: [
    edge('start', 'gate'), edge('gate', 'done', 'yes'), edge('gate', 'done', 'no'),
  ],
}

/** case 的 key 用了保留名 default：和兜底出口合并成一个出口，线照样接得上 */
const DEFAULT_CASE = {
  nodes: [
    node('start', 'input'),
    node('gate', 'branch', { cases: [{ key: 'fast', label: '快速' }, { key: 'default', label: '协作' }] }),
    node('quick', 'llm'), node('team', 'llm'), node('done', 'output'),
  ],
  edges: [
    edge('start', 'gate'), edge('gate', 'quick', 'fast'), edge('gate', 'team', 'default'),
    edge('quick', 'done'), edge('team', 'done'),
  ],
}

const FIXTURES = { PARALLEL, WIDE, LOOP, MERGE_BRANCH, BOTH_WAYS, DEFAULT_CASE }

// ---------------------------------------------------------------- 几何

/** 把 M/L/Q 路径拆成横平竖直的线段。Q 是拐角的圆弧，取端点即可。 */
function segmentsOf(path) {
  const tokens = path.match(/[MLQ][^MLQ]*/g) ?? []
  const points = []
  for (const token of tokens) {
    const nums = token.slice(1).trim().split(/[\s,]+/).map(Number)
    if (token[0] === 'Q') points.push({ x: nums[2], y: nums[3] })
    else points.push({ x: nums[0], y: nums[1] })
  }
  const segments = []
  for (let i = 1; i < points.length; i++) {
    const a = points[i - 1]
    const b = points[i]
    if (Math.abs(a.x - b.x) < 0.6 && Math.abs(a.y - b.y) < 0.6) continue
    segments.push({ a, b })
  }
  return segments
}

const isHorizontal = (s) => Math.abs(s.a.y - s.b.y) < 0.6
const isVertical = (s) => Math.abs(s.a.x - s.b.x) < 0.6
const span = (s, axis) => {
  const lo = Math.min(s.a[axis], s.b[axis])
  const hi = Math.max(s.a[axis], s.b[axis])
  return [lo, hi]
}
const overlapOf = (s1, s2, axis) => {
  const [a0, a1] = span(s1, axis)
  const [b0, b1] = span(s2, axis)
  return Math.min(a1, b1) - Math.max(a0, b0)
}

/** 两条线段是不是压在同一条线上 */
function collides(s1, s2) {
  if (isHorizontal(s1) && isHorizontal(s2)) {
    if (Math.abs(s1.a.y - s2.a.y) > 1) return 0
    return overlapOf(s1, s2, 'x')
  }
  if (isVertical(s1) && isVertical(s2)) {
    if (Math.abs(s1.a.x - s2.a.x) > 1) return 0
    return overlapOf(s1, s2, 'y')
  }
  return 0
}

function boxCrossings(segments, boxes, ownIds) {
  const hits = []
  for (const s of segments) {
    for (const box of boxes) {
      if (ownIds.has(box.id)) continue
      const [x0, x1] = span(s, 'x')
      const [y0, y1] = span(s, 'y')
      const pad = 2
      if (x0 < box.right - pad && x1 > box.left + pad
          && y0 < box.bottom - pad && y1 > box.top + pad) {
        hits.push(box.id)
      }
    }
  }
  return hits
}

// ---------------------------------------------------------------- 跑

const browser = await chromium.launch({ executablePath: CHROME })
const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } })
const page = await ctx.newPage()
const pageErrors = []
page.on('pageerror', (e) => pageErrors.push(e.message))
await page.goto(`${WEB}/studio`, { waitUntil: 'networkidle' })

// 走线模块是从 dev server 现编现取的。打包产物里没有这个路径，先探一下，
// 免得后面报一句看不懂的 import 失败
try {
  await page.evaluate(async () => { await import('/src/canvas/routing.ts') })
} catch (e) {
  console.error(`拿不到 /src/canvas/routing.ts：这个检查跑在 Vite dev server 上，`
    + `先 ./scripts/dev.sh（当前 ${WEB}）。${e.message}`)
  await browser.close()
  process.exit(1)
}

for (const [name, graph] of Object.entries(FIXTURES)) {
  console.log(`=== ${name} ===`)

  const res = await fetch(`${API}/copilot/layout`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ graph }),
  })
  if (!res.ok) {
    check('后端排版可用', false, `${res.status}`)
    continue
  }
  const laid = await res.json()

  const report = await page.evaluate(async ({ graph, laid }) => {
    const mod = await import('/src/canvas/routing.ts')
    // 节点高度用实测范围里偏大的一档：卡片带摘要、带运行态时会更高，
    // 用最小值去检查等于给自己放水
    const heights = [74, 92, 108]
    const nodes = laid.nodes.map((n, i) => ({
      id: n.id,
      position: n.position,
      measured: { width: 238, height: heights[i % heights.length] },
      data: { nodeType: n.type, config: n.data?.config ?? {} },
    }))
    const routes = mod.buildRoutes(nodes, laid.edges.map((e, i) => ({
      id: e.id || `e${i}`, source: e.source, target: e.target,
      sourceHandle: e.sourceHandle ?? null,
    })))
    const out = []
    for (const [id, route] of routes) out.push({ id, path: route.path })
    return out
  }, { graph, laid })

  const boxes = laid.nodes.map((n) => ({
    id: n.id,
    left: n.position.x,
    right: n.position.x + 238,
    top: n.position.y,
    bottom: n.position.y + 108,
  }))

  check('每条边都算出了路径', report.length === laid.edges.length,
    `${report.length}/${laid.edges.length}`)

  const segments = new Map()
  for (const item of report) segments.set(item.id, segmentsOf(item.path))

  // 1. 不同边之间不能有压在一起的线段
  const overlaps = []
  const ids = [...segments.keys()]
  for (let i = 0; i < ids.length; i++) {
    for (let j = i + 1; j < ids.length; j++) {
      for (const s1 of segments.get(ids[i])) {
        for (const s2 of segments.get(ids[j])) {
          if (collides(s1, s2) > 2) overlaps.push(`${ids[i]} × ${ids[j]}`)
        }
      }
    }
  }
  check('没有两条线压在同一段上', overlaps.length === 0,
    overlaps.slice(0, 4).join('、'))

  // 2. 长边不能从别的卡片中间穿过去
  const crossings = []
  for (const item of report) {
    const e = laid.edges.find((x, i) => (x.id || `e${i}`) === item.id) ?? {}
    const hits = boxCrossings(segments.get(item.id), boxes, new Set([e.source, e.target]))
    if (hits.length) crossings.push(`${item.id}→${[...new Set(hits)].join(',')}`)
  }
  check('没有线穿过别人的卡片', crossings.length === 0, crossings.slice(0, 3).join('、'))

  // 3. 起点终点要落在节点边缘上（差一点没关系，掉在节点里或图外面就是错的）
  const byId = new Map(laid.nodes.map((n) => [n.id, n]))
  const dangling = []
  for (const item of report) {
    const e = laid.edges.find((x, i) => (x.id || `e${i}`) === item.id)
    if (!e) continue
    const segs = segments.get(item.id)
    if (!segs.length) continue
    const start = segs[0].a
    const end = segs[segs.length - 1].b
    const src = byId.get(e.source)
    const dst = byId.get(e.target)
    if (Math.abs(start.x - (src.position.x + 238)) > 12) {
      dangling.push(`${item.id} 起点 x=${start.x.toFixed(0)}`)
    }
    if (Math.abs(end.x - dst.position.x) > 12) {
      dangling.push(`${item.id} 终点 x=${end.x.toFixed(0)}`)
    }
  }
  check('每条线的两端都接在节点上', dangling.length === 0, dangling.slice(0, 3).join('、'))
}

check('页面没有运行时错误', pageErrors.length === 0, pageErrors.join(' | '))

// 手拖过的节点位置是随机的：坐标乱七八糟时不能算出 NaN 路径。
// NaN 的 path 什么都不画，界面上就是"线没了"，而且一声不吭。
console.log('=== 随机坐标（手拖过的图） ===')
{
  const fuzz = await page.evaluate(async () => {
    const mod = await import('/src/canvas/routing.ts')
    let seed = 20240924
    const rnd = () => (seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648
    const types = ['input', 'llm', 'agent', 'branch', 'loop', 'output', 'human', 'transform']
    let total = 0
    let broken = 0
    const examples = []
    for (let trial = 0; trial < 200; trial++) {
      const n = 3 + Math.floor(rnd() * 9)
      const nodes = Array.from({ length: n }, (_, i) => ({
        id: `n${i}`,
        position: { x: Math.round(rnd() * 1800) - 200, y: Math.round(rnd() * 900) - 300 },
        measured: { width: 238, height: 40 + Math.round(rnd() * 90) },
        data: {
          nodeType: types[Math.floor(rnd() * types.length)],
          config: { cases: [{ key: 'yes' }, { key: 'no' }] },
        },
      }))
      const edges = []
      for (let i = 0; i < n; i++) {
        for (let k = 0, m = Math.floor(rnd() * 3); k < m; k++) {
          edges.push({
            id: `e${i}_${k}`, source: `n${i}`,
            target: `n${Math.floor(rnd() * n)}`,
            sourceHandle: rnd() < 0.4 ? 'yes' : null,
          })
        }
      }
      const routes = mod.buildRoutes(nodes, edges)
      for (const e of edges) {
        total++
        const route = routes.get(e.id)
        const ok = route && route.path.length >= 4 && !/NaN|Infinity|undefined/.test(route.path)
          && Number.isFinite(route.labelX) && Number.isFinite(route.labelY)
        if (!ok) {
          broken++
          if (examples.length < 3) examples.push(`${e.id}: ${route?.path?.slice(0, 60)}`)
        }
      }
    }
    return { total, broken, examples }
  })
  check(`随机坐标下每条边都画得出来（${fuzz.total} 条）`, fuzz.broken === 0,
    fuzz.examples.join('、'))
}

// 分支出口由 case 算出来。同 id 的两个出口会让 React Flow 出两个 handle、跑完两条一起
// 亮，React 还会报 key 重复；key=default 和兜底出口在运行时本来就是同一个出口
console.log('=== 分支出口：保留名 default、重复标识 ===')
{
  const r = await page.evaluate(async () => {
    const { sourceHandles } = await import('/src/canvas/nodeDefs.ts')
    const merged = sourceHandles('branch', { cases: [{ key: 'fast', label: '快速' }, { key: 'default', label: '协作' }] })
    const dup = sourceHandles('branch', { cases: [{ key: 'a' }, { key: 'a' }, { key: 'b' }] })
    const plain = sourceHandles('branch', { cases: [{ key: 'yes' }] })
    return {
      merged: merged.map((h) => `${h.id}:${h.label}`),
      dup: dup.map((h) => h.id),
      plain: plain.map((h) => h.id),
      colors: merged.map((h) => h.color ?? ''),
    }
  })
  check('key=default 的 case 和「其他」合并成一个出口', r.merged.length === 2
    && r.merged[1] === 'default:协作（兜底）', r.merged.join('、'))
  check('重复的 key 只出一个出口', r.dup.join(',') === 'a,b,default', r.dup.join(','))
  check('普通分支照常追加「其他」出口', r.plain.join(',') === 'yes,default', r.plain.join(','))
  check('出口颜色是中性的（ok 色只留给状态）', r.colors.every((c) => !c.includes('--ok')), r.colors.join('、'))
}

console.log(failed ? `\n${failed} 项未通过` : '\n全部通过')
await browser.close()
process.exit(failed ? 1 : 0)
