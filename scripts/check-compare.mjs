// 运行对比页（/runs/compare?target=…&base=…，pages/runs/ComparePage.tsx）的回归检查。
//
// 守的是这些「看着没坏、其实用不了」的地方：运行详情「更多」里没有入口、点了不先找基准就乱跳；摘要的个数和服务端
// 给的对不上；分类标签张冠李戴；口径效应和数据效应只摆两个数，不说两者之和等于总变化（或者自己算了一遍、和服务端
// 渲染的写法对不上）；不能拆的一声不吭；展开后归因只给数没有占比时不说为什么；SQL 结构变了只给一条、差异不标；
// 换了基准、互换了却不重新请求，或者慢的旧答复盖掉新的；409（未封存、封存核对不一致）、422（不是同一工作流）、
// 404（找不到基准）只弹一句通用报错；窄屏（390px）被表格撑出横向滚动；浅色下分类标签、差异高亮看不清。
//
// 全部用 page.route 假接口：虚构的「区域销售周报」三次已封存的正式运行和一次没封存的，运行详情、运行列表、
// compare/base、compare 都在浏览器层答掉，别的 GET 放行（外壳要的工作流、待审批），非 GET 一律拦掉。
// 运行对比的两个接口只读，这里的假答复严格照接口约定的字段名和形状。
//
//   node scripts/check-compare.mjs
// 对别的实例跑时带上地址：AGENTLAB_WEB=http://localhost:<前端端口> node scripts/check-compare.mjs
// COMPARE_SHOTS=<目录> 时亮暗、宽窄各截几张，供人眼复核。
import { mkdirSync } from 'node:fs'
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const CHROME = process.env.CHROME_PATH ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const SHOTS = process.env.COMPARE_SHOTS ?? ''

let failed = 0
const check = (name, cond, detail = '') => {
  console.log(`  ${cond ? '✓' : '✗'} ${name}${detail ? ` — ${detail}` : ''}`)
  if (!cond) failed++
}
const opened = new Set()
async function section(name, fn) {
  console.log(`\n=== ${name} ===`)
  try {
    await fn()
  } catch (e) {
    check(`${name} 中途出错`, false, String(e?.message ?? e).split('\n')[0])
  } finally {
    for (const c of opened) await c.close().catch(() => {})
    opened.clear()
  }
}

// ---------------------------------------------------------------- 夹具（虚构）

const WF = 'wf0cmp0sample'
const T = 'c0ffee0target00000000000000000001'
const B = 'c0ffee0base0000000000000000000002'
const B2 = 'c0ffee0older000000000000000000003'
const U = 'c0ffee0open0000000000000000000004'
const NAME = '区域销售周报'
const day = (d, h = 9) => `2026-${d}T0${h - 8}:00:00Z`

const runOf = (id, over = {}) => ({
  id, workflow_id: WF, workflow_name: NAME, status: 'succeeded', input: {}, output: {}, error: null,
  usage: { wall_ms: 4000, active_ms: 4000, wait_ms: 0 }, run_class: 'formal', version: 4, version_hash: 'ab'.repeat(32),
  manifest_hash: 'cd'.repeat(32), manifest_seq: 40, started_by: '值班分析员', error_node_id: null, memory_scope: null,
  collection: null, schedule_id: 'sch0weekly', created_at: day('10-05'), started_at: day('10-05'),
  finished_at: '2026-10-05T01:02:00Z', ...over,
})
const RUNS = {
  [T]: runOf(T),
  [B]: runOf(B, { version: 3, created_at: day('09-28'), started_at: day('09-28'), finished_at: '2026-09-28T01:02:00Z' }),
  [B2]: runOf(B2, { version: 3, created_at: day('09-21'), started_at: day('09-21'), finished_at: '2026-09-21T01:02:00Z' }),
  // 没封存的一次：能出现在下拉里，但不能选
  [U]: runOf(U, { run_class: 'exploratory', version: null, manifest_hash: null, manifest_seq: null, schedule_id: null,
    created_at: day('10-03'), started_at: day('10-03'), finished_at: '2026-10-03T01:02:00Z' }),
}
const refOf = (id) => {
  const r = RUNS[id]
  return { id, workflow_name: r.workflow_name, version: r.version, run_class: r.run_class, started_at: r.started_at,
    finished_at: r.finished_at, schedule_id: r.schedule_id }
}

const REGIONS = ['青禾区', '白石区', '云杉区', '赤松区', '蓝湾区', '金穗区', '银杏区', '翠竹区', '月湖区', '星港区', '石桥区', '柳岸区']
// 各区的变化之和正好是总变化 2,531.5；占比照服务端给小数（0.4 是 40%）。服务端按变化的绝对值只给前 10 项，
// 另有几项写在 note 里（ALL_ROWS 给「服务端多给了」那一节用）
const DELTAS = [1012.6, 506.3, 379.7, 253.2, 126.6, 101.3, 63.3, 38.0, 25.3, 12.6, 7.6, 5.0]
const ALL_ROWS = REGIONS.map((name, i) => ({
  key: [name], base: 3000 + i * 100, target: 3000 + i * 100 + DELTAS[i], delta: DELTAS[i],
  share: Math.round(DELTAS[i] / 2531.5 * 10000) / 10000,
}))
const MORE_KEYS = '共 12 个维度值，列出变化最大的 10 个'

const metric = (over) => ({
  card_id: 'caliber', card_title: '周报口径', unit: '', decimals: 1, reasons: [], decomposition: null,
  decomposition_note: null, inputs: [], attribution: null, delta_pct: null, ...over,
})
const METRICS = [
  metric({ metric_id: 'gmv', name: '销售额', unit: '元', base: 45678.5, target: 48210, base_rendered: '45,678.5元',
    target_rendered: '48,210.0元', delta: 2531.5, delta_rendered: '+2,531.5元', delta_pct: 5.54, classification: 'data_only',
    reasons: ['取数的 SQL 没有变化，查询结果有变化'],
    inputs: [{ path: 'vars.kpi.gmv', base: 45678.5, target: 48210 }],
    attribution: { columns: ['区域'], measure: 'amount', rows: ALL_ROWS.slice(0, 10), total_delta: 2531.5, note: MORE_KEYS } }),
  metric({ metric_id: 'orders', name: '订单数', unit: '单', decimals: 0, base: 1234, target: 1301, base_rendered: '1,234单',
    target_rendered: '1,301单', delta: 67, delta_rendered: '+67单', delta_pct: 5.43, classification: 'data_only' }),
  metric({ metric_id: 'aov', name: '客单价', unit: '元', base: 37.0, target: 41.5, base_rendered: '37.0元', target_rendered: '41.5元',
    delta: 4.5, delta_rendered: '+4.5元', delta_pct: 12.16, classification: 'caliber_and_data',
    reasons: ['表达式由「vars.kpi.gmv / vars.kpi.order_cnt」改为「vars.kpi.gmv / vars.kpi.paid_cnt」'],
    decomposition: { caliber_effect: 5.8, data_effect: -1.3, caliber_rendered: '+5.8元', data_rendered: '-1.3元' },
    // 遮罩列的输入：服务端两边都不给值，另带 masked
    inputs: [{ path: 'vars.kpi.gmv', base: 45678.5, target: 48210 }, { path: 'vars.kpi.order_cnt', base: 1234, target: 1234 },
      { path: 'vars.kpi.cost', base: null, target: null, masked: true }],
    attribution: { columns: ['区域', '渠道'], measure: 'order_cnt', total_delta: 67,
      note: '各区域订单数之和与指标的输入对不上，不给占比',
      rows: [{ key: ['青禾区', '门店'], base: 400, target: 440, delta: 40, share: null },
        { key: ['白石区', '线上'], base: 300, target: 327, delta: 27, share: null }] } }),
  metric({ metric_id: 'refund_rate', name: '退货率', unit: '%', decimals: 2, base: 0.0235, target: 0.0198, base_rendered: '2.35%',
    target_rendered: '1.98%', delta: -0.0037, delta_rendered: '-0.37 个百分点', delta_pct: -15.74, classification: 'sql_changed',
    reasons: ['所依据的查询「退货明细」的筛选条件有改动'],
    decomposition_note: '所依据的查询「退货明细」SQL 结构有改动，口径不同，不做拆分' }),
  metric({ metric_id: 'visitors', name: '日均访客', unit: '人', decimals: 0, base: 5200, target: 5480, base_rendered: '5,200人',
    target_rendered: '5,480人', delta: 280, delta_rendered: '+280人', delta_pct: 5.38, classification: 'params_only',
    reasons: ['「拉取销量」只有日期参数不同'],
    // 归不了因：服务端给空的 rows 和一句说明
    attribution: { columns: [], measure: 'visits', rows: [], total_delta: null,
      note: '查询结果被截断（基准运行只取回了前 500 行），不做维度归因' } }),
  metric({ metric_id: 'new_users', name: '新客数', unit: '人', decimals: 0, base: null, target: 320, base_rendered: '—',
    target_rendered: '320人', delta: null, delta_rendered: null, classification: 'added', reasons: ['本次新增的指标'] }),
  metric({ metric_id: 'members', name: '会员数', unit: '人', decimals: 0, base: 8800, target: null, base_rendered: '8,800人',
    target_rendered: '—', delta: null, delta_rendered: null, classification: 'removed', reasons: ['本次没有这个指标'] }),
  metric({ metric_id: 'margin', name: '毛利率', unit: '%', decimals: 1, base: 0.31, target: 0.33, base_rendered: '31.0%',
    target_rendered: '33.0%', delta: 0.02, delta_rendered: '+2.0 个百分点', delta_pct: 6.45, classification: 'incomparable',
    reasons: ['基准运行的口径卡没有记录输入'], decomposition_note: '基准运行的口径卡没有记录输入，无法拆分' }),
  ...[['stores', '门店数', '家', 56], ['complaints', '客诉数', '件', 12], ['sku', 'SKU 数', '个', 1500]].map(([id, name, unit, v]) =>
    metric({ metric_id: id, name, unit, decimals: 0, base: v, target: v, base_rendered: `${v.toLocaleString('en-US')}${unit}`,
      target_rendered: `${v.toLocaleString('en-US')}${unit}`, delta: 0, delta_rendered: null, delta_pct: 0, classification: 'unchanged' })),
]
const SUMMARY = { unchanged: 3, data_only: 2, caliber_and_data: 1, sql_changed: 1, params_only: 1, added: 1, removed: 1, incomparable: 1 }
const PULL_BASE = "SELECT region, SUM(amount) AS gmv FROM sales WHERE sold_on >= '2026-09-21' AND sold_on < '2026-09-28' GROUP BY region"
const PULL_TARGET = "SELECT region, SUM(amount) AS gmv FROM sales WHERE sold_on >= '2026-09-28' AND sold_on < '2026-10-05' GROUP BY region"
const QUERIES = [
  { node_id: 'pull', node_title: '拉取销量', ordinal: 0, structure: 'params_only', base_sql: PULL_BASE, target_sql: PULL_TARGET,
    literals: [{ base: "'2026-09-21'", target: "'2026-09-28'" }, { base: "'2026-09-28'", target: "'2026-10-05'" }],
    base_rows: 12, target_rows: 12, base_truncated: false, target_truncated: false },
  { node_id: 'refund', node_title: '退货明细', ordinal: 0, structure: 'changed',
    base_sql: "SELECT COUNT(*) AS n\nFROM refunds r\nWHERE r.created_at >= '2026-09-21'",
    target_sql: "SELECT COUNT(*) AS n\nFROM refunds r\nWHERE r.paid_at >= '2026-09-28' AND r.status = 'done'",
    literals: [], base_rows: 500, target_rows: 488, base_truncated: true, target_truncated: false,
    differences: ['时间条件的列不同：基准按 refunds.created_at，这次按 refunds.paid_at', "这次多了筛选条件 refunds.status = 'done'"] },
  { node_id: 'stores', node_title: '门店清单', ordinal: 0, structure: 'same', base_sql: 'SELECT COUNT(*) AS n FROM stores',
    target_sql: 'SELECT COUNT(*) AS n FROM stores', literals: [], base_rows: 1, target_rows: 1, base_truncated: false, target_truncated: false },
  { node_id: 'pull', node_title: '拉取销量', ordinal: 1, structure: 'same', base_sql: 'SELECT COUNT(*) AS n FROM visits',
    target_sql: 'SELECT COUNT(*) AS n FROM visits', literals: [], base_rows: 1, target_rows: 1, base_truncated: false, target_truncated: false },
  { node_id: 'newbie', node_title: '新客查询', ordinal: 0, structure: 'added', base_sql: null,
    target_sql: "SELECT COUNT(*) AS n FROM users WHERE first_order_on >= '2026-09-28'", literals: [], base_rows: null, target_rows: 1,
    base_truncated: null, target_truncated: false },
  { node_id: 'member', node_title: '会员查询', ordinal: 0, structure: 'removed', base_sql: 'SELECT COUNT(*) AS n FROM members',
    target_sql: null, literals: [], base_rows: 1, target_rows: null, base_truncated: false, target_truncated: null,
    problem: '基准运行的查询快照无法读取' },
]
const TEMPLATE = { base_version: 3, target_version: 4, same_graph: false, nodes: [
  { node_id: 'caliber', title: '周报口径', change: 'config', keys: ['metrics'] },
  { node_id: 'refund', title: '退货明细', change: 'config', keys: ['sql', 'timeout'], labels: ['SQL', '超时（秒）'] },
  { node_id: 'newbie', title: '新客查询', change: 'added', keys: [] },
  { node_id: 'member', title: '会员查询', change: 'removed', keys: [] },
] }
const NOTE = '基准运行中「退货明细」的查询结果已截断（只保存了前 500 行），按已保存的行比较'
/** 以更早的一次（B2）为基准时多一条说明：换基准、过时答复的检查靠它认出页面上是哪一份 */
const NOTE_B2 = '以更早的一次运行为基准'

function comparison(target, base) {
  return { target: refOf(target), base: refOf(base), summary: SUMMARY, template: TEMPLATE, metrics: METRICS, queries: QUERIES,
    notes: base === B2 ? [NOTE, NOTE_B2] : [NOTE] }
}

const LABEL = { unchanged: '未变', data_only: '只数据变', caliber_and_data: '口径与数据都变', sql_changed: 'SQL 结构变',
  params_only: '只参数变', added: '新增', removed: '移除', incomparable: '无法比较' }
const ORDER = ['data_only', 'caliber_and_data', 'sql_changed', 'params_only', 'added', 'removed', 'incomparable', 'unchanged']
const REASON = '默认基准：同一定时任务的上一次运行'

// ---------------------------------------------------------------- 假接口

function events() {
  const t0 = Date.parse(day('10-05')) / 1000
  return [
    { seq: 1, type: 'run.started', node_id: null, ts: t0, data: { nodes: 2 } },
    { seq: 2, type: 'run.finished', node_id: null, ts: t0 + 4, data: { output: {}, usage: {}, timing: { wall_ms: 4000, active_ms: 4000, wait_ms: 0 } } },
  ]
}
const GRAPH = { nodes: [
  { id: 'pull', type: 'tool', position: { x: 0, y: 0 }, data: { label: '拉取销量', config: {} } },
  { id: 'caliber', type: 'metrics', position: { x: 200, y: 0 }, data: { label: '周报口径', config: {} } },
], edges: [{ source: 'pull', target: 'caliber' }] }

/**
 * 默认的假接口。opts.base(runId) / opts.compare(target, base, n) 可以换掉对应接口的答复，返回 {status, json, wait}
 * （wait 是一个 Promise：等它落定再答，用来压住某一次请求）
 */
function defaults(opts) {
  return async (method, url, n) => {
    const p = url.pathname
    if (method !== 'GET') return null
    if (p === '/api/runs') {
      const wf = url.searchParams.get('workflow_id')
      const list = Object.values(RUNS).filter((r) => !wf || r.workflow_id === wf)
        .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))
      return { json: list }
    }
    let m = p.match(/^\/api\/runs\/([^/]+)\/compare\/base$/)
    if (m) {
      if (opts.base) return opts.base(m[1])
      return m[1] === T ? { json: { base_run_id: B, reason: 'same_schedule', base: refOf(B) } }
        : { status: 404, json: { detail: '没有找到可作为基准的运行' } }
    }
    m = p.match(/^\/api\/runs\/([^/]+)\/compare$/)
    if (m) {
      const base = url.searchParams.get('base')
      if (opts.compare) return opts.compare(m[1], base, n)
      return { json: comparison(m[1], base) }
    }
    m = p.match(/^\/api\/runs\/([^/]+)(\/[a-z]+)?$/)
    if (m && RUNS[m[1]]) {
      if (!m[2]) return { json: RUNS[m[1]] }
      if (m[2] === '/events') return { json: events() }
      if (m[2] === '/graph') return { json: { graph: GRAPH, workflow_id: WF, version: RUNS[m[1]].version } }
      if (m[2] === '/artifacts') return { json: [] }
    }
    if (p === '/api/approvals' && url.searchParams.get('run_id')) return { json: [] }
    return null
  }
}

const browser = await chromium.launch({ executablePath: CHROME })

/**
 * 开一页：/api 的请求先问 defaults（再由 opts 换掉其中几个），答不了的 GET 放行到服务端，非 GET 一律拦掉。
 * 记下每次请求（「GET /api/runs/…?base=…」）和页面报错；原生对话框都算失败
 */
async function open(path, { width = 1280, height = 900, theme = 'dark', waitUntil = 'networkidle', ...opts } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height }, colorScheme: theme, timezoneId: 'Asia/Shanghai' })
  opened.add(ctx)
  ctx.setDefaultTimeout(6000)
  ctx.setDefaultNavigationTimeout(30000)
  await ctx.addInitScript((t) => {
    try {
      localStorage.setItem('agentlab.theme', t)
      localStorage.removeItem('agentlab.health')
    } catch { /* noop */ }
  }, theme)
  const page = await ctx.newPage()
  const log = []
  const blocked = []
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('dialog', (d) => { errors.push(`原生对话框：${d.message()}`); void d.dismiss() })
  const answer = defaults(opts)
  let n = 0
  await page.route((u) => new URL(u).pathname.startsWith('/api/'), async (route) => {
    const req = route.request()
    const url = new URL(req.url())
    const key = `${req.method()} ${url.pathname}${url.search}`
    log.push(key)
    const out = await answer(req.method(), url, ++n)
    if (out) {
      if (out.wait) await out.wait
      return route.fulfill({ status: out.status ?? 200, json: out.json }).catch(() => {})
    }
    if (req.method() === 'GET') return route.continue()
    blocked.push(key)
    return route.abort()
  })
  await page.goto(`${WEB}${path}`, { waitUntil })
  // 设置接口回来时会按服务端存的主题再切一次：之后照 lib/theme.ts 的 applyTheme 切到要查的这一套——切的那一帧关掉
  // 过渡，不然带 transition 的按钮、摘要格还在慢慢变色，量出来、截下来的是半路的颜色
  await page.evaluate((t) => {
    const root = document.documentElement
    root.classList.add('theme-switching')
    root.setAttribute('data-theme', t)
    void getComputedStyle(root).backgroundColor
    requestAnimationFrame(() => root.classList.remove('theme-switching'))
  }, theme)
  return { ctx, page, log, blocked, errors }
}

const comparePath = (target, base) => `/runs/compare?target=${target}${base ? `&base=${base}` : ''}`
/** 等对比页落定：state 是 ok / error / no-target / finding… */
const settled = (page, state = 'ok') => page.locator(`[data-compare-page][data-compare-state="${state}"]`).waitFor({ timeout: 8000 })
const text = (page, sel) => page.locator(sel).first().innerText()
const texts = (page, sel) => page.locator(sel).allInnerTexts()
const has = (page, sel) => page.locator(sel).count().then((c) => c > 0)
const compareHits = (log, target, base) => log.filter((k) => k === `GET /api/runs/${target}/compare?base=${base}`).length
const until = async (fn, ms = 6000, step = 100) => {
  const end = Date.now() + ms
  let v = await fn()
  while (!v && Date.now() < end) { await new Promise((r) => setTimeout(r, step)); v = await fn() }
  return v
}
const shot = async (page, name) => {
  if (!SHOTS) return
  mkdirSync(SHOTS, { recursive: true })
  await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: false })
}

// ---------------------------------------------------------------- 检查

await section('从运行详情进入', async () => {
  const { page, log, blocked, errors } = await open(`/runs/${T}`)
  await page.locator(`[data-run-detail="${T}"]`).waitFor({ timeout: 10000 })
  // 走过的地址（含 pushState / replaceState）：菜单要先问到基准再跳，不能先跳到只带本次的地址、再让对比页去找
  const visited = []
  page.on('framenavigated', (f) => { if (f === page.mainFrame()) visited.push(f.url()) })
  await page.getByRole('button', { name: '更多操作' }).click()
  const item = page.locator('[data-menu=compare]')
  check('「更多」菜单里有「与上一次对比」，可点', await item.count() === 1 && (await item.innerText()).includes('与上一次对比')
    && !(await item.isDisabled()))
  await item.click()
  await page.waitForURL((u) => u.pathname === '/runs/compare', { timeout: 8000 })
  await settled(page)
  const url = new URL(page.url())
  check('跳到 /runs/compare，带着本次和基准', url.searchParams.get('target') === T && url.searchParams.get('base') === B, url.search)
  const iBase = log.indexOf(`GET /api/runs/${T}/compare/base`)
  const iCmp = log.indexOf(`GET /api/runs/${T}/compare?base=${B}`)
  check('先请求 compare/base，再请求 compare', iBase >= 0 && iCmp > iBase, `${iBase} → ${iCmp}`)
  check('compare/base 只请求一次（页面不再自己找一遍）', log.filter((k) => k.startsWith(`GET /api/runs/${T}/compare/base`)).length === 1)
  const compareUrls = visited.map((u) => new URL(u)).filter((u) => u.pathname === '/runs/compare')
  check('菜单问到基准之后才跳，一步到带基准的地址', compareUrls.length >= 1 && compareUrls.every((u) => u.searchParams.get('base') === B),
    compareUrls.map((u) => u.search).join(' → '))
  check('写明基准是怎么选的', (await text(page, '[data-compare-reason]')).trim() === REASON, await text(page, '[data-compare-reason]').catch(() => ''))
  check('没有发出写请求', blocked.length === 0, blocked.join('；'))
  check('页面没有报错', errors.length === 0, errors.join('；'))
})

await section('未封存的运行：入口不可点并说明原因', async () => {
  const saved = RUNS[T]
  RUNS[T] = { ...saved, manifest_hash: null, manifest_seq: null }
  try {
    const { page, log } = await open(`/runs/${T}`)
    await page.locator(`[data-run-detail="${T}"]`).waitFor({ timeout: 10000 })
    await page.getByRole('button', { name: '更多操作' }).click()
    const item = page.locator('[data-menu=compare]')
    check('未封存：「与上一次对比」不可点，写明要先封存', await item.isDisabled() && (await item.innerText()).includes('封存'))
    check('未封存：没有请求 compare/base', !log.some((k) => k.includes('/compare')))
  } finally {
    RUNS[T] = saved
  }
})

await section('两次运行与摘要', async () => {
  const { page, errors } = await open(comparePath(T, B))
  await settled(page)
  const baseCard = page.locator('[data-compare-run="base"]')
  const targetCard = page.locator('[data-compare-run="target"]')
  check('基准、本次各一张卡，写着名字、版本和类别', (await baseCard.innerText()).includes(NAME)
    && (await baseCard.innerText()).includes('正式运行 v3') && (await targetCard.innerText()).includes('正式运行 v4'),
    `${(await baseCard.innerText()).replace(/\s+/g, ' ')} | ${(await targetCard.innerText()).replace(/\s+/g, ' ')}`)
  check('两张卡都能点进各自的运行详情', await baseCard.locator('a[data-compare-run-link]').getAttribute('href') === `/runs/${B}`
    && await targetCard.locator('a[data-compare-run-link]').getAttribute('href') === `/runs/${T}`)
  check('定时任务发起的运行标出来', (await targetCard.innerText()).includes('定时任务发起'))
  check('直接打开带基准的地址：不写「默认基准」', await page.locator('[data-compare-reason]').count() === 0)
  const tiles = await page.locator('[data-compare-summary] [data-compare-class]').evaluateAll((els) =>
    els.map((e) => [e.getAttribute('data-compare-class'), e.querySelector('[data-compare-count]')?.textContent?.trim(), e.textContent]))
  check('摘要按约定的八类排好', JSON.stringify(tiles.map((t) => t[0])) === JSON.stringify(ORDER), tiles.map((t) => t[0]).join(','))
  check('摘要的个数和服务端给的一致', tiles.every(([k, n]) => Number(n) === SUMMARY[k]), tiles.map(([k, n]) => `${k}=${n}`).join(' '))
  check('摘要的标签写对', tiles.every(([k, , t]) => t.includes(LABEL[k])))
  await shot(page, 'wide-dark')
  check('页面没有报错', errors.length === 0, errors.join('；'))
})

await section('指标表', async () => {
  const { page } = await open(comparePath(T, B))
  await settled(page)
  const rows = page.locator('tr[data-compare-metric]')
  check('每个指标一行', await rows.count() === METRICS.length, String(await rows.count()))
  const chips = await rows.evaluateAll((els) => els.map((e) => [e.getAttribute('data-compare-metric'),
    e.getAttribute('data-class'), e.querySelector('[data-compare-chip]')?.textContent?.trim()]))
  check('分类标签和分类对得上', chips.every(([id, k, label]) => METRICS.find((m) => `caliber:${m.metric_id}` === id)?.classification === k
    && label === LABEL[k]), chips.map((c) => c.join('/')).join(' '))
  const gmv = page.locator('tr[data-compare-metric="caliber:gmv"]')
  check('基准 → 本次、变化量用服务端渲染的写法', (await gmv.locator('[data-compare-base]').innerText()) === '45,678.5元'
    && (await gmv.locator('[data-compare-target]').innerText()) === '48,210.0元'
    && (await gmv.locator('[data-compare-delta]').innerText()) === '+2,531.5元')
  check('变化百分比照服务端的百分数写、带正负号（5.54 → +5.5%）', (await gmv.locator('[data-compare-pct]').innerText()) === '+5.5%',
    await gmv.locator('[data-compare-pct]').innerText().catch(() => ''))
  const refund = page.locator('tr[data-compare-metric="caliber:refund_rate"]')
  check('下降的百分比写负号', (await refund.locator('[data-compare-pct]').innerText()) === '-15.7%')
  const aov = page.locator('tr[data-compare-metric="caliber:aov"]')
  check('能拆分的：口径效应、数据效应并列', (await aov.locator('[data-compare-caliber]').innerText()).includes('+5.8元')
    && (await aov.locator('[data-compare-data]').innerText()).includes('-1.3元'),
    (await aov.locator('[data-compare-decomp]').innerText().catch(() => '')).replace(/\s+/g, ' '))
  check('能拆分的：画了两段条形', await aov.locator('[data-compare-decomp] [data-compare-bar] > i').count() === 2)
  check('表头说明两者之和等于总变化', (await text(page, '[data-compare-legend]')).includes('两者之和等于总变化'))
  check('不能拆的：写出服务端给的原因', (await refund.locator('[data-compare-decomp-note]').innerText())
    === '所依据的查询「退货明细」SQL 结构有改动，口径不同，不做拆分')
  check('无法比较的也写原因', (await page.locator('tr[data-compare-metric="caliber:margin"] [data-compare-decomp-note]').innerText())
    .includes('没有记录输入'))
  check('新增的：基准写「—」，本次照写', (await page.locator('tr[data-compare-metric="caliber:new_users"] [data-compare-base]').innerText()) === '—'
    && (await page.locator('tr[data-compare-metric="caliber:new_users"] [data-compare-target]').innerText()) === '320人')
  check('未变的：变化写「无变化」', (await page.locator('tr[data-compare-metric="caliber:stores"] [data-compare-delta]').innerText()) === '无变化')
  check('只数据变、不拆分的：不编造拆分', await gmv.locator('[data-compare-decomp], [data-compare-decomp-note]').count() === 0)
})

await section('展开：输入对比与维度归因', async () => {
  const { page, errors } = await open(comparePath(T, B))
  await settled(page)
  const gmvBtn = page.locator('tr[data-compare-metric="caliber:gmv"] button[data-compare-expand]')
  check('默认收起', (await gmvBtn.getAttribute('aria-expanded')) === 'false' && await page.locator('[data-compare-detail]').count() === 0)
  await gmvBtn.click()
  const detail = page.locator('tr[data-compare-detail="caliber:gmv"]')
  await detail.waitFor()
  check('展开后标为展开、指向明细', (await gmvBtn.getAttribute('aria-expanded')) === 'true'
    && (await gmvBtn.getAttribute('aria-controls')) === await detail.getAttribute('id'))
  check('列出变化说明', (await detail.locator('[data-compare-reasons]').innerText()).includes('查询结果有变化'))
  const input = detail.locator('[data-compare-inputs] tr[data-compare-input="vars.kpi.gmv"]')
  check('输入对比：路径、两边的值，变了的标出来', (await input.innerText()).includes('45,678.5') && (await input.innerText()).includes('48,210')
    && (await input.getAttribute('data-changed')) !== null, (await input.innerText().catch(() => '')).replace(/\s+/g, ' '))
  const attrRows = detail.locator('[data-compare-attr] tr[data-compare-attr-row]')
  check('维度归因只列前 10 项', await attrRows.count() === 10, String(await attrRows.count()))
  check('服务端说另有几项没列：照写', (await detail.locator('[data-compare-attr-note]').innerText()) === MORE_KEYS
    && await detail.locator('[data-compare-attr-more]').count() === 0)
  const first = attrRows.first()
  check('归因一行：键、两边的值、变化、占比', (await first.innerText()).includes('青禾区') && (await first.innerText()).includes('+1,012.6')
    && (await first.locator('[data-compare-share]').innerText()) === '40.0%', (await first.innerText()).replace(/\s+/g, ' '))
  check('写明按哪一列归因', (await detail.locator('[data-compare-attr]').innerText()).includes('amount'))
  check('有占比时不写缺占比的原因', !(await detail.locator('[data-compare-attr-note]').innerText()).includes('占比'))
  check('写出合计变化', (await detail.locator('[data-compare-attr-total]').innerText()).includes('+2,531.5'))

  // 键盘：Tab 到客单价的展开按钮，回车展开
  const aovBtn = page.locator('tr[data-compare-metric="caliber:aov"] button[data-compare-expand]')
  await aovBtn.focus()
  await page.keyboard.press('Enter')
  const aov = page.locator('tr[data-compare-detail="caliber:aov"]')
  await aov.waitFor()
  check('键盘回车也能展开', (await aovBtn.getAttribute('aria-expanded')) === 'true')
  check('拆分写成一句：口径效应、数据效应，两者之和等于总变化',
    (await aov.locator('[data-compare-decomp-sum]').innerText()).trim() === '口径效应 +5.8元，数据效应 -1.3元，两者之和等于总变化 +4.5元',
    await aov.locator('[data-compare-decomp-sum]').innerText().catch(() => ''))
  check('没有变的输入不标', (await aov.locator('tr[data-compare-input="vars.kpi.order_cnt"]').getAttribute('data-changed')) === null)
  const masked = aov.locator('tr[data-compare-input="vars.kpi.cost"]')
  check('遮罩列的输入：两边都写「已遮罩」，不标变了', (await masked.locator('td').allInnerTexts()).every((t) => t.trim() === '已遮罩')
    && (await masked.getAttribute('data-changed')) === null, (await masked.innerText().catch(() => '')).replace(/\s+/g, ' '))
  const shares = await aov.locator('[data-compare-attr] [data-compare-share]').allInnerTexts()
  check('没有占比：占比一栏写「—」', shares.length === 2 && shares.every((s) => s.trim() === '—'), shares.join(','))
  check('没有占比：写出原因', (await aov.locator('[data-compare-attr-note]').innerText()).includes('各区域订单数之和与指标的输入对不上'))
  check('多个维度列：键按列写全', (await aov.locator('[data-compare-attr] tr[data-compare-attr-row]').first().innerText()).includes('门店'))
  await shot(page, 'expanded-dark')

  // 再按一次收起
  await aovBtn.press('Enter')
  check('再按一次收起', await page.locator('tr[data-compare-detail="caliber:aov"]').count() === 0)
  // 没有归因、没有输入的指标：照实说
  await page.locator('tr[data-compare-metric="caliber:orders"] button[data-compare-expand]').click()
  const orders = page.locator('tr[data-compare-detail="caliber:orders"]')
  check('没有维度归因、没有输入时照实说', (await orders.locator('[data-compare-attr-none]').count()) === 1
    && (await orders.locator('[data-compare-inputs-none]').count()) === 1)
  // 归不了因（查询结果截断）：服务端给空的 rows 和一句说明，只写说明，不画空表
  await page.locator('tr[data-compare-metric="caliber:visitors"] button[data-compare-expand]').click()
  const visitors = page.locator('tr[data-compare-detail="caliber:visitors"]')
  check('归不了因：写服务端的说明，不画空表', (await visitors.locator('[data-compare-attr-note]').innerText()).includes('被截断')
    && await visitors.locator('[data-compare-attr] table').count() === 0 && await visitors.locator('[data-compare-attr-total]').count() === 0)
  check('页面没有报错', errors.length === 0, errors.join('；'))
})

await section('归因超过 10 项时只列前 10 项', async () => {
  const { page } = await open(comparePath(T, B), { compare: (t, b) => {
    const data = comparison(t, b)
    return { json: { ...data, metrics: data.metrics.map((m) => (m.metric_id === 'gmv'
      ? { ...m, attribution: { ...m.attribution, rows: ALL_ROWS, note: null } } : m)) } }
  } })
  await settled(page)
  await page.locator('tr[data-compare-metric="caliber:gmv"] button[data-compare-expand]').click()
  const detail = page.locator('tr[data-compare-detail="caliber:gmv"]')
  check('只列 10 项', await detail.locator('[data-compare-attr] tr[data-compare-attr-row]').count() === 10)
  check('写明另有几项没列', (await detail.locator('[data-compare-attr-more]').innerText()).includes('另有 2 项'))
})

await section('摘要筛选', async () => {
  const { page } = await open(comparePath(T, B))
  await settled(page)
  const tile = page.locator('[data-compare-summary] [data-compare-class="sql_changed"]')
  await tile.click()
  check('点一类：只剩这一类的指标，按钮标为按下', await page.locator('tr[data-compare-metric]').count() === 1
    && (await tile.getAttribute('aria-pressed')) === 'true'
    && await page.locator('tr[data-compare-metric="caliber:refund_rate"]').count() === 1)
  check('写明正在筛选', (await text(page, '[data-compare-filter]')).includes('SQL 结构变'))
  await tile.press('Enter')
  check('再按一次（键盘）回到全部', await page.locator('tr[data-compare-metric]').count() === METRICS.length
    && (await tile.getAttribute('aria-pressed')) === 'false')
  await page.locator('[data-compare-summary] [data-compare-class="unchanged"]').click()
  await page.locator('[data-compare-filter-clear]').click()
  check('「显示全部」清掉筛选', await page.locator('tr[data-compare-metric]').count() === METRICS.length)
})

await section('查询', async () => {
  const { page } = await open(comparePath(T, B))
  await settled(page)
  const qs = page.locator('[data-compare-query]')
  check('每条查询一项', await qs.count() === QUERIES.length, String(await qs.count()))
  const refund = page.locator('[data-compare-query="refund:0"]')
  check('结构变：标「结构变」', (await refund.getAttribute('data-structure')) === 'changed'
    && (await refund.locator('[data-compare-structure]').innerText()).trim() === '结构变')
  const baseSql = refund.locator('[data-compare-sql="base"]')
  const targetSql = refund.locator('[data-compare-sql="target"]')
  const [bb, tb] = [await baseSql.boundingBox(), await targetSql.boundingBox()]
  check('结构变：两条 SQL 并排（基准在左、本次在右）', !!bb && !!tb && Math.abs(bb.y - tb.y) < 2 && bb.x + bb.width <= tb.x + 1,
    JSON.stringify({ bb, tb }))
  check('两边都是原文', (await baseSql.innerText()).includes('r.created_at') && (await targetSql.innerText()).includes("r.status = 'done'"))
  const del = (await baseSql.locator('mark[data-diff="del"]').allInnerTexts()).join(' ')
  const add = (await targetSql.locator('mark[data-diff="add"]').allInnerTexts()).join(' ')
  check('高亮基准独有的部分', del.includes('created_at') && del.includes("'2026-09-21'") && !del.includes('SELECT'), del)
  check('高亮本次独有的部分', add.includes('paid_at') && add.includes("r.status = 'done'") && !add.includes('FROM'), add)
  check('相同的部分不高亮', !(await baseSql.locator('mark').allInnerTexts()).join(' ').includes('refunds'))
  check('截断照实标', (await refund.locator('[data-compare-rows]').innerText()).includes('已截断'))
  check('结构变：列出服务端说的差在哪', JSON.stringify(await refund.locator('[data-compare-differences] li').allInnerTexts())
    === JSON.stringify(QUERIES[1].differences))
  check('快照读不出来：照服务端的说明写在那一条上', (await page.locator('[data-compare-query="member:0"] [data-compare-query-problem]').innerText())
    === '基准运行的查询快照无法读取')
  const pull = page.locator('[data-compare-query="pull:0"]')
  const lits = await pull.locator('[data-compare-literals] li').allInnerTexts()
  check('只参数变：列出变化的参数', lits.length === 2 && lits[0].includes("'2026-09-21'") && lits[0].includes("'2026-09-28'")
    && lits[1].includes("'2026-10-05'"), lits.join(' | '))
  check('只参数变：默认不并排摆两条 SQL', await pull.locator('[data-compare-sql]').count() === 0)
  const same = page.locator('[data-compare-query="stores:0"]')
  check('相同的：默认不展开 SQL', (await same.locator('[data-compare-structure]').innerText()).trim() === '相同'
    && await same.locator('[data-compare-sql]').count() === 0)
  await same.locator('button[data-compare-sql-toggle]').click()
  check('相同的：点开能看 SQL', (await same.locator('[data-compare-sql]').first().innerText()).includes('FROM stores'))
  check('同一节点的第二条查询写「第 2 条」', (await page.locator('[data-compare-query="pull:1"] [data-compare-nth]').innerText()).includes('第 2 条')
    && (await pull.locator('[data-compare-nth]').innerText()).includes('第 1 条'))
  check('只有一条查询的节点不写第几条', await refund.locator('[data-compare-nth]').count() === 0)
  const added = page.locator('[data-compare-query="newbie:0"]')
  check('新增、移除的查询各只有一边的 SQL', await added.locator('[data-compare-sql="target"]').count() === 1
    && await added.locator('[data-compare-sql="base"]').count() === 0
    && await page.locator('[data-compare-query="member:0"] [data-compare-sql="base"]').count() === 1)
  await refund.scrollIntoViewIfNeeded()
  await shot(page, 'queries-dark')
})

await section('模板与说明', async () => {
  const { page } = await open(comparePath(T, B))
  await settled(page)
  check('模板：版本 v3 → v4', (await text(page, '[data-compare-versions]')).includes('v3') && (await text(page, '[data-compare-versions]')).includes('v4'))
  const node = page.locator('[data-compare-node="refund"]')
  check('变化的节点：名字、变化类型、配置项（用服务端给的叫法，键名在悬停提示里）', (await node.getAttribute('data-change')) === 'config'
    && (await node.innerText()).includes('退货明细') && (await node.innerText()).includes('配置变更')
    && JSON.stringify(await node.locator('[data-compare-key]').allInnerTexts()) === JSON.stringify(['SQL', '超时（秒）'])
    && JSON.stringify(await node.locator('[data-compare-key]').evaluateAll((els) => els.map((e) => e.getAttribute('title'))))
      === JSON.stringify(['sql', 'timeout']))
  check('没给叫法的配置项照写键名', JSON.stringify(await page.locator('[data-compare-node="caliber"] [data-compare-key]').allInnerTexts())
    === JSON.stringify(['metrics']))
  check('新增、移除的节点写明', (await text(page, '[data-compare-node="newbie"]')).includes('新增节点')
    && (await text(page, '[data-compare-node="member"]')).includes('移除节点'))
  check('说明照实列出', JSON.stringify(await texts(page, '[data-compare-notes] li')) === JSON.stringify([NOTE]))
})

await section('换基准、互换', async () => {
  const { page, log } = await open(comparePath(T, B))
  await settled(page)
  const picker = page.locator('select[data-compare-base-picker]')
  const options = await picker.locator('option').evaluateAll((els) => els.map((o) => [o.value, o.disabled, o.textContent]))
  check('下拉列出同一工作流的其他运行（不含本次）', options.some(([v]) => v === B) && options.some(([v]) => v === B2)
    && !options.some(([v]) => v === T), options.map((o) => o[0].slice(0, 12)).join(','))
  const open_ = options.find(([v]) => v === U)
  check('没封存的那次不能选，写明原因', !!open_ && open_[1] === true && open_[2].includes('未封存'), JSON.stringify(open_))
  check('当前基准是选中的', await picker.inputValue() === B)
  check('列运行用的是运行列表接口（按工作流）', log.some((k) => k.startsWith('GET /api/runs?') && k.includes(`workflow_id=${WF}`)))
  await picker.selectOption(B2)
  await until(async () => new URL(page.url()).searchParams.get('base') === B2)
  await until(async () => (await texts(page, '[data-compare-notes] li')).includes(NOTE_B2))
  check('换基准：地址跟着变', new URL(page.url()).searchParams.get('base') === B2)
  check('换基准：重新请求对比', compareHits(log, T, B2) === 1)
  check('换基准：页面换成新的一份', (await texts(page, '[data-compare-notes] li')).includes(NOTE_B2))
  check('换基准：基准卡跟着换', (await text(page, '[data-compare-run="base"]')).includes('9/21'))
  const lists = () => log.filter((k) => k.startsWith('GET /api/runs?') && k.includes(`workflow_id=${WF}`)).length
  const listsBefore = lists()
  await page.locator('button[data-compare-swap]').click()
  await until(async () => new URL(page.url()).searchParams.get('target') === B2)
  await until(async () => compareHits(log, B2, T) === 1)
  const u = new URL(page.url())
  check('互换：本次和基准对调', u.searchParams.get('target') === B2 && u.searchParams.get('base') === T, u.search)
  check('互换：重新请求对比', compareHits(log, B2, T) === 1)
  await settled(page)
  check('互换：两张卡跟着对调', (await page.locator('[data-compare-run="target"] a[data-compare-run-link]').getAttribute('href')) === `/runs/${B2}`)
  check('互换：同一工作流的运行列表不重取，下拉照常可用', lists() === listsBefore
    && await page.locator('select[data-compare-base-picker]').isEnabled() && await page.locator('select[data-compare-base-picker]').inputValue() === T,
    `${listsBefore} → ${lists()}`)
})

await section('过时的答复不盖掉新的', async () => {
  let release
  const gate = new Promise((r) => { release = r })
  const { page, log } = await open(comparePath(T, B), { waitUntil: 'domcontentloaded', compare: async (target, base) => {
    if (base === B) return { json: comparison(target, base), wait: gate }
    return { json: comparison(target, base) }
  } })
  await page.locator('[data-compare-page]').waitFor({ timeout: 10000 })
  const loading = await until(() => has(page, '[data-compare-loading][aria-busy="true"]'))
  check('加载中：标为忙', loading)
  await until(async () => (await page.locator('select[data-compare-base-picker] option').count()) > 1)
  await page.locator('select[data-compare-base-picker]').selectOption(B2)
  await settled(page)
  release()
  await page.waitForTimeout(600)
  check('慢的旧答复回来后，页面仍是新基准的那一份', (await texts(page, '[data-compare-notes] li')).includes(NOTE_B2)
    && new URL(page.url()).searchParams.get('base') === B2)
  check('两次都发出去了', compareHits(log, T, B) === 1 && compareHits(log, T, B2) === 1)
})

/** 「其他错误」那一节：第一次对比回 500，之后照常 */
let flakyCalls = 0
await section('出错与找不到基准', async () => {
  const sealed = await open(comparePath(T, B), { compare: () => ({ status: 409, json: { detail: '基准运行的封存核对不一致，无法对比' } }) })
  await settled(sealed.page, 'error')
  const e409 = sealed.page.locator('[data-compare-error="409"]')
  check('409：写明未封存或封存核对不一致，带上服务端的原因', (await e409.locator('[data-compare-error-title]').innerText()).includes('封存')
    && (await e409.innerText()).includes('基准运行的封存核对不一致，无法对比'), (await e409.innerText().catch(() => '')).replace(/\s+/g, ' '))
  check('409：给出去两次运行详情核对的入口', await e409.locator(`a[href="/runs/${B}"]`).count() === 1
    && await e409.locator(`a[href="/runs/${T}"]`).count() === 1)
  check('409：两张运行卡仍在，下拉仍能换基准', await sealed.page.locator('[data-compare-run="base"]').count() === 1
    && await sealed.page.locator('select[data-compare-base-picker]').isEnabled())
  check('409：不显示摘要、指标', await sealed.page.locator('[data-compare-summary], tr[data-compare-metric]').count() === 0)

  const other = await open(comparePath(T, B), { compare: () => ({ status: 422, json: { detail: '两次运行不属于同一个工作流' } }) })
  await settled(other.page, 'error')
  const e422 = other.page.locator('[data-compare-error="422"]')
  check('422：写明无法对比，带上原因和下一步', (await e422.innerText()).includes('两次运行不属于同一个工作流')
    && (await e422.innerText()).includes('同一工作流'), (await e422.innerText().catch(() => '')).replace(/\s+/g, ' '))

  const gone = await open(comparePath(T, B), { compare: () => ({ status: 404, json: { detail: '运行记录不存在' } }) })
  await settled(gone.page, 'error')
  check('404：写明运行记录不存在', (await gone.page.locator('[data-compare-error="404"]').innerText()).includes('运行记录不存在'))

  const flaky = await open(comparePath(T, B), { compare: () => (flakyCalls++ === 0
    ? { status: 500, json: { detail: '对比服务暂时不可用' } } : { json: comparison(T, B) }) })
  await settled(flaky.page, 'error')
  await flaky.page.locator('[data-compare-error] button', { hasText: '重试' }).click()
  await settled(flaky.page)
  check('其他错误：给「重试」，重试后正常显示', await flaky.page.locator('tr[data-compare-metric]').count() === METRICS.length)

  // 只带本次：页面自己找基准，找不到时写原因，下拉照样能选
  const lone = await open(comparePath(T), { base: () => ({ status: 404, json: { detail: '同一工作流没有其他已封存的正式运行' } }) })
  await settled(lone.page, 'error')
  const noBase = lone.page.locator('[data-compare-error="no-base"]')
  check('找不到基准：写明原因', (await noBase.locator('[data-compare-error-title]').innerText()).includes('未找到可作为基准的运行')
    && (await noBase.innerText()).includes('同一工作流没有其他已封存的正式运行'))
  check('找不到基准：仍能手动选', await lone.page.locator('select[data-compare-base-picker]').isEnabled())
  await lone.page.locator('select[data-compare-base-picker]').selectOption(B2)
  await settled(lone.page)
  check('手动选了之后照常对比', new URL(lone.page.url()).searchParams.get('base') === B2 && compareHits(lone.log, T, B2) === 1)

  const missing = await open(comparePath(T), { base: () => ({ status: 404, json: { detail: '运行记录不存在', code: 'run_not_found' } }) })
  await settled(missing.page, 'error')
  check('找基准时本次就不存在：写运行记录不存在', await missing.page.locator('[data-compare-error="404"]').count() === 1
    && (await missing.page.locator('[data-compare-error-title]').innerText()) === '运行记录不存在')

  // 只带本次、找得到：自己补上基准，写明怎么选的
  const auto = await open(comparePath(T))
  await settled(auto.page)
  check('只带本次：自己找到基准、补进地址', new URL(auto.page.url()).searchParams.get('base') === B
    && (await text(auto.page, '[data-compare-reason]')).trim() === REASON)

  // 运行详情里找不到基准：提示原因，给「手动选择基准」
  const menu = await open(`/runs/${T}`, { base: () => ({ status: 404, json: { detail: '同一工作流没有其他已封存的正式运行' } }) })
  await menu.page.locator(`[data-run-detail="${T}"]`).waitFor({ timeout: 10000 })
  await menu.page.getByRole('button', { name: '更多操作' }).click()
  await menu.page.locator('[data-menu=compare]').click()
  const toast = menu.page.locator('[data-toast]', { hasText: '同一工作流没有其他已封存的正式运行' })
  check('运行详情里找不到基准：提示原因、留在原页', await until(() => toast.count().then((c) => c === 1))
    && new URL(menu.page.url()).pathname === `/runs/${T}`)
  await toast.getByRole('button', { name: '手动选择基准' }).click()
  await menu.page.waitForURL((u) => u.pathname === '/runs/compare', { timeout: 8000 })
  check('「手动选择基准」去到对比页，只带本次', new URL(menu.page.url()).searchParams.get('target') === T
    && !new URL(menu.page.url()).searchParams.get('base'))

  const none = await open('/runs/compare')
  await settled(none.page, 'no-target')
  check('什么都没带：说明从哪里进来', (await text(none.page, '[data-compare-page]')).includes('与上一次对比'))
})

await section('空的对比', async () => {
  const { page } = await open(comparePath(T, B), { compare: (t, b) => ({ json: { ...comparison(t, b), metrics: [], queries: [], notes: [],
    summary: Object.fromEntries(ORDER.map((k) => [k, 0])), template: { base_version: 4, target_version: 4, same_graph: true, nodes: [] } } }) })
  await settled(page)
  check('没有指标：照实说', await page.locator('[data-compare-empty="metrics"]').count() === 1)
  check('没有查询：照实说', await page.locator('[data-compare-empty="queries"]').count() === 1)
  check('执行图相同：照实说', await page.locator('[data-compare-same-graph]').count() === 1)
  check('没有说明时不留空的「说明」一节', await page.locator('[data-compare-notes]').count() === 0)
  check('摘要全是 0，不能点', (await page.locator('[data-compare-summary] [data-compare-class]:not([disabled])').count()) === 0)
})

/** 对比页自己有没有横向滚动（表格、SQL 在自己的框里横向滚动不算），以及它的右边有没有出视口 */
const overflow = (page) => page.evaluate(() => {
  const root = document.querySelector('[data-compare-page]')
  if (!root) return { root: 99, right: 99 }
  const r = root.getBoundingClientRect()
  const wide = [...root.querySelectorAll('*')].filter((el) => {
    const b = el.getBoundingClientRect()
    return b.width > 0 && b.right > r.right + 1 && !el.closest('[data-compare-scroll]')
  }).map((el) => el.tagName + (el.getAttribute('data-compare-run') ?? ''))
  return { root: root.scrollWidth - root.clientWidth, right: Math.round(r.right - innerWidth), wide: wide.slice(0, 3) }
})

await section('窄屏', async () => {
  for (const theme of ['dark', 'light']) {
    const { page, errors } = await open(comparePath(T, B), { width: 390, height: 844, theme })
    await settled(page)
    let o = await overflow(page)
    check(`390px（${theme}）：页面不横向溢出`, o.root <= 1 && o.right <= 0 && !o.wide.length, JSON.stringify(o))
    await page.locator('tr[data-compare-metric="caliber:gmv"] button[data-compare-expand]').click()
    await page.locator('tr[data-compare-metric="caliber:aov"] button[data-compare-expand]').click()
    o = await overflow(page)
    check(`390px（${theme}）：展开归因后也不横向溢出`, o.root <= 1 && o.right <= 0 && !o.wide.length, JSON.stringify(o))
    const scroller = await page.evaluate(() => {
      const box = document.querySelector('[data-compare-metrics] [data-compare-scroll]')
      return box ? { sw: box.scrollWidth, cw: box.clientWidth, focusable: box.tabIndex >= 0 } : null
    })
    check(`390px（${theme}）：指标表在自己的框里横向滚动，键盘可聚焦`, !!scroller && scroller.sw > scroller.cw && scroller.focusable,
      JSON.stringify(scroller))
    const refund = page.locator('[data-compare-query="refund:0"]')
    const [bb, tb] = [await refund.locator('[data-compare-sql="base"]').boundingBox(), await refund.locator('[data-compare-sql="target"]').boundingBox()]
    check(`390px（${theme}）：两条 SQL 上下排、都在屏幕里`, !!bb && !!tb && tb.y >= bb.y + bb.height - 1 && bb.x >= 0 && bb.x + bb.width <= 390,
      JSON.stringify({ bb, tb }))
    o = await overflow(page)
    check(`390px（${theme}）：查询区不横向溢出`, o.root <= 1 && !o.wide.length, JSON.stringify(o))
    await shot(page, `narrow-${theme}`)
    check(`390px（${theme}）：页面没有报错`, errors.length === 0, errors.join('；'))
  }
})

/** 两个颜色的对比度（WCAG）：颜色是 getComputedStyle 给的 rgb / rgba / color(srgb …)，半透明的叠在底色上 */
function contrast(fg, bg, base) {
  const parse = (c) => {
    const m = c.match(/color\(srgb ([\d.]+) ([\d.]+) ([\d.]+)(?: \/ ([\d.]+))?\)/)
    if (m) return [m[1] * 255, m[2] * 255, m[3] * 255, m[4] == null ? 1 : Number(m[4])]
    const n = c.match(/rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\)/)
    return n ? [Number(n[1]), Number(n[2]), Number(n[3]), n[4] == null ? 1 : Number(n[4])] : null
  }
  const over = (top, under) => top.slice(0, 3).map((v, i) => v * top[3] + under[i] * (1 - top[3]))
  const lum = ([r, g, b]) => {
    const f = (v) => { const s = v / 255; return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4 }
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
  }
  const b0 = parse(base); const b1 = parse(bg); const f0 = parse(fg)
  if (!b0 || !b1 || !f0) return 0
  const under = over(b1, b0.slice(0, 3))
  const a = lum(over(f0, under)); const b = lum(under)
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)
}

await section('深浅色可读', async () => {
  for (const theme of ['dark', 'light']) {
    const { page } = await open(comparePath(T, B), { theme })
    await settled(page)
    const colors = await page.evaluate(() => {
      /** 往上找第一个不透明的底色 */
      const baseOf = (el) => {
        for (let p = el.parentElement; p; p = p.parentElement) {
          const c = getComputedStyle(p).backgroundColor
          if (c && !/rgba\(0, 0, 0, 0\)|transparent/.test(c)) return c
        }
        return getComputedStyle(document.body).backgroundColor
      }
      const pick = (sel) => {
        const el = document.querySelector(sel)
        if (!el) return null
        const s = getComputedStyle(el)
        return { fg: s.color, bg: s.backgroundColor, base: baseOf(el) }
      }
      return {
        chips: Object.fromEntries([...document.querySelectorAll('tr[data-compare-metric] [data-compare-chip]')]
          .map((el) => [el.closest('tr').getAttribute('data-class'), (() => {
            const s = getComputedStyle(el)
            return { fg: s.color, bg: s.backgroundColor, base: baseOf(el) }
          })()])),
        del: pick('[data-compare-sql="base"] mark[data-diff="del"]'),
        add: pick('[data-compare-sql="target"] mark[data-diff="add"]'),
        note: pick('[data-compare-decomp-note]'),
      }
    })
    const low = Object.entries(colors.chips).map(([k, c]) => [k, contrast(c.fg, c.bg, c.base)]).filter(([, r]) => r < 4.5)
    check(`${theme}：分类标签的文字对比度都 ≥ 4.5`, Object.keys(colors.chips).length === 8 && low.length === 0,
      low.map(([k, r]) => `${k} ${r.toFixed(2)}`).join('，'))
    for (const k of ['del', 'add', 'note']) {
      const c = colors[k]
      const r = c ? contrast(c.fg, c.bg, c.base) : 0
      check(`${theme}：${{ del: '基准独有的高亮', add: '本次独有的高亮', note: '不能拆分的原因' }[k]}对比度 ≥ 4.5`, r >= 4.5,
        c ? `${r.toFixed(2)}（${c.fg} on ${c.bg} / ${c.base}）` : '没找到')
    }
    await shot(page, `wide-${theme}`)
  }
})

await browser.close()
console.log(failed ? `\n✗ 运行对比：${failed} 项未通过` : '\n✓ 运行对比：全部通过')
process.exit(failed ? 1 : 0)
