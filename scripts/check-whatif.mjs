// 数字推演（证据面板里口径卡上的数，run/WhatIf.tsx）的回归检查。
//
// 守的是这些「看着没坏、其实用不了」的地方：横幅没了或者不醒目（推演被当成出具）、边打字边发请求、请求体里的
// 输入路径和行号不对（行号对人从 1 数，对接口从 0 数）、勾选框在不推演时也冒出来、恢复原值之后还留着一行没勾回去、
// 不支持的输入一声不吭、窄屏（390px）被推演区撑出横向滚动、浅色主题下横幅看不清。还有复核第 1 轮查出来的：整份
// 结果作输入时无行可勾、窗口里勾了不起作用的行也给勾、勾完行结果不在视野里、加载中和失败后还摆着上一次的答复、
// 封存核对没过不说、整个指标推演不了时又写「无可推演」又写「无变化」。复核第 2 轮：用到了遮罩列、有输入还原不了的
// 指标一打开仍写「无变化」，还给可改的输入框和可勾的行（改了、勾了才说推演不了）。
//
// 跑在预览页上：/preview.html?syn=whatif（宽屏右侧弹出、窄屏底部抽出）和 ?syn=whatif-rail（360px 画布右栏）。
// 文档是 frontend/src/run/__tests__/evidence-query.json 里后端 compose_doc 真跑出来的（只用通用名）；片段接口
// 按它的 segments 作答（s18 改成整份结果作输入），推演接口按后端 engine/recompute.py 的规则在这里算（只覆盖用到的
// 几个指标），勾行接口按夹具里 Q3 的快照作答，别的非 GET 一律拦掉。
//
//   node scripts/check-whatif.mjs
// 对别的实例跑时带上地址：AGENTLAB_WEB=http://localhost:<前端端口> node scripts/check-whatif.mjs
// WHATIF_SHOTS=<目录> 时亮暗两套各截几张，供人眼复核。
import { mkdirSync, readFileSync } from 'node:fs'
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const CHROME = process.env.CHROME_PATH ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const SHOTS = process.env.WHATIF_SHOTS ?? ''
const root = new URL('..', import.meta.url).pathname
const fx = JSON.parse(readFileSync(`${root}frontend/src/run/__tests__/evidence-query.json`, 'utf8'))
const RUN = fx.run_id
const BANNER = '推演，非出具：不会写进运行记录，也不会改动报告'

let failed = 0
const check = (name, cond, detail = '') => {
  console.log(`  ${cond ? '✓' : '✗'} ${name}${detail ? ` — ${detail}` : ''}`)
  if (!cond) failed++
}
async function section(name, fn) {
  console.log(`\n=== ${name} ===`)
  try {
    await fn()
  } catch (e) {
    check(`${name} 中途出错`, false, String(e?.message ?? e).split('\n')[0])
  }
}

// ---------------------------------------------------------------- 夹具

/**
 * 片段接口的答复：夹具里的原样，几处改成推演要用到的样子（推演接口的假答复和这里对得上）：
 * - s22：老运行的容器输入（没存完整值）乘一个本来能改的数，整个指标推演不了
 * - s10：Q1 的 gmv 列设了遮罩，整个指标推演不了
 * - s8：订单数按文本条件取（文本输入不能改），订单数本身能改
 * - s6：销售额是一个加工过的列表求和（对不上任何快照），没有能改的输入、也没有能勾的行
 * - s18：整份查询结果作输入（len(vars.orders.rows)）：证据链里没有它的查询步骤，和后端真跑出来的一样
 */
function segmentReply(sid) {
  const body = structuredClone(fx.segments[sid])
  if (sid === 's22') {
    body.chain[0] = { ...body.chain[0], expression: 'sum(vars.adjust.items) * vars.adjust.k',
      substituted: 'sum(vars.adjust.items) * 1' }
    body.chain[1] = { step: 'input', path: 'vars.adjust.items', value: '[4 项]', node_id: 'adjust', via: 'code',
      field: 'items', role: 'compute', status: 'ok' }
    body.chain.push({ step: 'input', path: 'vars.adjust.k', value: 1, node_id: 'adjust', via: 'code', field: 'k',
      role: 'compute', status: 'ok' })
  }
  if (sid === 's10') body.chain = body.chain.map((s) => (s.step === 'query' ? { ...s, masked: ['gmv'] } : s))
  if (sid === 's8') {
    body.chain[0] = { ...body.chain[0], expression: "vars.kpi.order_cnt if vars.kpi.region == '华东' else 0",
      substituted: "1234 if '华东' == '华东' else 0" }
    body.chain.splice(2, 0, { step: 'input', path: 'vars.kpi.region', value: '华东', node_id: 'parse', via: 'transform',
      field: 'region', status: 'ok' })
  }
  if (sid === 's6') {
    body.chain = [
      { ...body.chain[0], expression: 'sum(vars.kpi.daily)', substituted: 'sum(vars.kpi.daily)' },
      { step: 'input', path: 'vars.kpi.daily', value: '[7 项]', node_id: 'daily', via: 'transform', field: 'daily', status: 'ok' },
    ]
  }
  if (sid === 's18') {
    body.chain = [
      { ...body.chain[0], name: '订单笔数', expression: 'len(vars.orders.rows)', substituted: 'len(vars.orders.rows)',
        value: 12, rendered: '12单', unit: '单', decimals: null, status: 'ok', recompute_ok: true },
      { step: 'input', path: 'vars.orders.rows', value: '[12 项]', node_id: 'parse', via: 'transform', field: 'rows', status: 'ok' },
    ]
  }
  return body
}

const chainOf = (sid) => fx.segments[sid].chain
const inputsOf = (sid) => chainOf(sid).filter((s) => s.step === 'input')
const Q1 = chainOf('s12')[1].artifact
const Q4 = chainOf('s12')[2].artifact
const Q3 = inputsOf('s70')[0].artifact
const SNAP = fx.snapshots.Q3.content
const thousands1 = (v) => v.toLocaleString('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 1 })
const NO_LINEAGE = '这个指标生成于系统升级前，口径卡没有记录输入，不能推演'
const MASKED_METRIC = '这个指标用到了设置了遮罩的列，不能推演'
const LOST = '无法推演：vars.adjust.items（当时没有保存这个输入的完整值）'
/** 整个指标推演不了时后端给的样子：不改动也是 unsupported / masked，新值、变化一概为空 */
const blocked = (status, reason) => ({ new: null, delta: null, rendered_new: null, rendered_delta: null, changed: null,
  status, reason, substituted: null })
const AFFECTED = [{ node_id: 'yearly', artifact: 'b1'.repeat(32), caliber: '年报口径', version: 'v1', metrics: ['y'], direct: false }]

/** 推演接口在这里按后端的规则算：给了新值的用新值；cell / agent 字段那一格所在的行被排除了，指标排除后无法计算 */
const METRICS = {
  wow: {
    sid: 's12', name: '环比增幅', unit: '%', calc: ([g, p]) => Math.round((g - p) / p * 1000) / 10,
    render: (v) => `${v.toFixed(1)}%`, delta: (d) => `${d > 0 ? '+' : '-'}${Math.abs(d).toFixed(1)} 个百分点`,
  },
  spread: {
    sid: 's70', name: '首末单差额', unit: '元', calc: ([t, l]) => Math.round((t - l) * 10) / 10,
    render: (v) => `${thousands1(v)}元`, delta: (d) => `${d > 0 ? '+' : '-'}${thousands1(Math.abs(d))}元`,
  },
}

function whatifReply(body) {
  const overrides = body?.overrides ?? {}
  const inputs = overrides.inputs ?? {}
  const exclude = overrides.exclude_rows ?? {}
  const base = { run_id: RUN, card: { node_id: 'caliber', artifact: chainOf('s12')[0].artifact, caliber: '周报口径', version: 'v3' },
    report: body?.report ?? null, sealed: true, seal: { sealed: true, ok: true }, affected_cards: [] }
  const changed = Object.keys(inputs).length > 0 || Object.keys(exclude).length > 0
  if (body?.metric === 'new_users') {
    // 整份查询结果作输入：行数就是指标，勾掉几行少几单
    const gone = exclude[Q3] ?? []
    const value = SNAP.rows.length - gone.length
    return { ...base, affected_cards: gone.length ? AFFECTED : [],
      metrics: [{ id: 'new_users', name: '订单笔数', unit: '单', old: 12, new: value, delta: value - 12,
        rendered_old: '12单', rendered_new: `${value}单`, rendered_delta: gone.length ? `-${gone.length}单` : null,
        changed: gone.length > 0, status: gone.length ? 'ok' : 'unchanged', reason: null,
        substituted: 'len(vars.orders.rows)',
        inputs: [{ path: 'vars.orders.rows', via: 'transform', node_id: 'parse', artifact: Q3, row_span: [0, 11],
          kind: 'container', editable: false, old: '[12 项]', new: `[${value} 项]`, changed: gone.length > 0,
          ...(gone.length ? { excluded_rows: gone } : {}) }] }],
      segments: [{ report: 'write', unit: fx.segments.s18.unit.id, sid: 's18', metric: 'new_users', rendered_old: '12单',
        rendered_new: `${value}单`, changed: gone.length > 0 }],
      unsupported: [] }
  }
  if (body?.metric === 'refund_rate') {
    // 升级前的口径卡：没记输入，整个指标推演不了
    return { ...base, metrics: [{ id: 'refund_rate', name: '退款率', unit: '', old: 0.0235, rendered_old: '2.35%',
      ...blocked('unsupported', NO_LINEAGE), inputs: [] }],
    segments: [{ report: 'write', unit: fx.segments.s16.unit.id, sid: 's16', metric: 'refund_rate', rendered_old: '2.35%',
      rendered_new: null, changed: null }],
    unsupported: [{ metric: 'refund_rate', input: null, reason: NO_LINEAGE }] }
  }
  if (body?.metric === 'adj') {
    // 老运行：容器没存完整值。乘的那个数本来能改，指标整个推演不了，也就不让改
    return { ...base, metrics: [{ id: 'adj', name: '调整后销售额', unit: '元', old: 44000, rendered_old: '44,000.0元',
      ...blocked('unsupported', LOST),
      inputs: [{ path: 'vars.adjust.items', via: 'code', node_id: 'adjust', kind: 'container', editable: false,
        old: '[4 项]', new: null, changed: false },
      { path: 'vars.adjust.k', via: 'code', node_id: 'adjust', kind: 'number', editable: false, old: 1, new: 1, changed: false }] }],
    segments: [{ report: 'write', unit: fx.segments.s22.unit.id, sid: 's22', metric: 'adj', rendered_old: '44,000.0元',
      rendered_new: null, changed: null }],
    unsupported: [{ metric: 'adj', input: null, reason: LOST },
      { metric: 'adj', input: 'vars.adjust.items', reason: '无法推演：当时没有保存这个输入的完整值' }] }
  }
  if (body?.metric === 'aov') {
    // gmv 列设了遮罩：那一格不给值；订单数本来能改、它那一行本来能勾，指标整个推演不了，一样不让动
    const cell = (path, column) => ({ path, via: 'agent_field', node_id: 'fetch', artifact: Q1, locator: { row: 0, column } })
    return { ...base, metrics: [{ id: 'aov', name: '客单价', unit: '元', old: 37.0, rendered_old: '37.0元',
      ...blocked('masked', MASKED_METRIC),
      inputs: [{ ...cell('vars.kpi.gmv', 'gmv'), kind: 'number', editable: false, old: null, new: null, changed: false, masked: true },
        { ...cell('vars.kpi.order_cnt', 'order_cnt'), kind: 'number', editable: false, old: 1234, new: 1234, changed: false }] }],
    segments: [{ report: 'write', unit: fx.segments.s10.unit.id, sid: 's10', metric: 'aov', rendered_old: '37.0元',
      rendered_new: null, changed: null }],
    unsupported: [{ metric: 'aov', input: null, reason: MASKED_METRIC },
      { metric: 'aov', input: 'vars.kpi.gmv', reason: '这一列在数据源中设置了遮罩，不能用于推演' }] }
  }
  if (body?.metric === 'orders') {
    // 推演得了的指标里有一个文本输入：照实显示、列出原因，订单数照常能改
    return { ...base, metrics: [{ id: 'orders', name: '订单数', unit: '单', old: 1234, new: 1234, delta: 0,
      rendered_old: '1,234单', rendered_new: '1,234单', rendered_delta: null, changed: false, status: 'unchanged', reason: null,
      substituted: "1234 if '华东' == '华东' else 0",
      inputs: [{ path: 'vars.kpi.order_cnt', via: 'agent_field', node_id: 'fetch', artifact: Q1,
        locator: { row: 0, column: 'order_cnt' }, row_span: [0, 0], kind: 'number', editable: true, old: 1234, new: 1234, changed: false },
      { path: 'vars.kpi.region', via: 'transform', node_id: 'parse', kind: 'text', editable: false, old: '华东', new: '华东', changed: false }] }],
    segments: [{ report: 'write', unit: fx.segments.s8.unit.id, sid: 's8', metric: 'orders', rendered_old: '1,234单',
      rendered_new: '1,234单', changed: false }],
    unsupported: [{ metric: 'orders', input: 'vars.kpi.region', reason: '不是数值，不能修改' }] }
  }
  if (body?.metric === 'gmv') {
    // 加工过的列表（对不上任何快照）：不能改、也没有行可勾，推演得了却没什么可动
    return { ...base, metrics: [{ id: 'gmv', name: '销售额', unit: '元', old: 45678.5, new: 45678.5, delta: 0,
      rendered_old: '45,678.5元', rendered_new: '45,678.5元', rendered_delta: null, changed: false, status: 'unchanged',
      reason: null, substituted: 'sum(vars.kpi.daily)',
      inputs: [{ path: 'vars.kpi.daily', via: 'transform', node_id: 'daily', kind: 'container', editable: false,
        old: '[7 项]', new: '[7 项]', changed: false }] }],
    segments: [{ report: 'write', unit: fx.segments.s6.unit.id, sid: 's6', metric: 'gmv', rendered_old: '45,678.5元',
      rendered_new: '45,678.5元', changed: false }],
    unsupported: [{ metric: 'gmv', input: 'vars.kpi.daily', reason: '列表或对象不能直接修改，也没有对应的查询结果可以排除行' }] }
  }
  if (body?.metric === 'fx') {
    // 空态：原式里没有引用任何输入（写死的常数）
    return { ...base, metrics: [{ id: 'fx', name: '汇率', unit: '', old: 7.12, new: 7.12, delta: 0, rendered_old: '7.12',
      rendered_new: '7.12', rendered_delta: null, changed: false, status: 'unchanged', reason: null, substituted: '7.12', inputs: [] }],
    segments: [], unsupported: [] }
  }
  const def = METRICS[body?.metric]
  const step = chainOf(def.sid)[0]
  const views = inputsOf(def.sid).map((inp) => {
    const row = inp.locator?.row
    // row_span：这个输入实际用到的快照行，单格就是它那一行（后端 recompute._row_span）
    const view = { path: inp.path, via: inp.via, node_id: inp.node_id, artifact: inp.artifact, locator: inp.locator,
      row_span: [row, row], kind: 'number', editable: true, old: inp.value }
    if (inp.path in inputs) return { ...view, new: inputs[inp.path], changed: inputs[inp.path] !== inp.value, touched: true }
    if ((exclude[inp.artifact] ?? []).includes(row)) {
      return { ...view, new: null, changed: true, excluded_rows: [row], touched: true, gone: true }
    }
    return { ...view, new: inp.value, changed: false }
  })
  const touched = views.some((v) => v.touched)
  const gone = views.find((v) => v.gone)
  let status = 'unchanged'; let value = step.value; let reason = null
  if (touched && gone) {
    status = 'excluded'; value = null; reason = `排除后无法计算：${gone.path} 所在的第 ${gone.locator.row + 1} 行已排除`
  } else if (touched) {
    status = 'ok'; value = def.calc(views.map((v) => v.new))
  }
  const delta = value == null ? null : Math.round((value - step.value) * 10) / 10
  const rendered = value == null ? '—' : def.render(value)
  const metric = { id: step.metric, name: def.name, unit: def.unit, old: step.value, new: value, delta,
    rendered_old: step.rendered, rendered_new: rendered, rendered_delta: delta ? def.delta(delta) : null,
    changed: value !== step.value, status, reason, substituted: status === 'ok' ? `（推演代入式 ${views.map((v) => v.new).join('、')}）` : step.substituted,
    inputs: views.map(({ touched: _t, gone: _g, ...v }) => v) }
  return { ...base, metrics: [metric], unsupported: [], affected_cards: changed ? AFFECTED : [],
    segments: [{ report: 'write', unit: fx.segments[def.sid].unit.id, sid: def.sid, metric: step.metric,
      rendered_old: step.rendered, rendered_new: rendered, changed: rendered !== step.rendered }] }
}

/** 勾行接口：按夹具里 Q3 的快照给一页（phone 列在夹具里已经写成「已遮罩」） */
function rowsReply(url) {
  const artifact = url.searchParams.get('artifact')
  const offset = Number(url.searchParams.get('offset') ?? 0)
  const limit = Number(url.searchParams.get('limit') ?? 50)
  if (artifact !== Q3) return { status: 422, json: { detail: '这份查询快照不属于这次运行，或不在封存范围内，不能用于推演', code: 'whatif_bad_input' } }
  const picked = SNAP.rows.slice(offset, offset + limit)
  return { json: { run_id: RUN, artifact, node_id: 'fetch', tool: 'db_query__shop', source: 'shop', columns: SNAP.columns,
    rows: picked, row_offset: offset, row_index: picked.map((_, i) => offset + i), total_rows: SNAP.rows.length,
    truncated: false, masked: ['phone'], hash_ok: true, sealed: true } }
}

// ---------------------------------------------------------------- 页面

const browser = await chromium.launch({ executablePath: CHROME })

/**
 * 开一页：片段接口按夹具答，推演接口按 whatifReply 答（reply 给了就由它改：延迟、出错、未封存），记下每次推演的
 * 请求体和时刻。别的非 GET 一律拦掉，别的 GET 放行
 */
async function open(syn, { width = 1280, height = 900, theme = 'dark', reply = null } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height }, colorScheme: theme, timezoneId: 'Asia/Shanghai' })
  ctx.setDefaultTimeout(6000)
  const page = await ctx.newPage()
  const sent = []
  const pages = []
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('dialog', (d) => { errors.push(`原生对话框：${d.message()}`); void d.dismiss() })
  await page.route((u) => new URL(u).pathname.startsWith('/api/'), async (route) => {
    const req = route.request()
    const url = new URL(req.url())
    if (req.method() === 'POST' && url.pathname === `/api/runs/${RUN}/evidence/whatif`) {
      const body = req.postDataJSON()
      sent.push({ body, at: Date.now() })
      const out = reply ? await reply(body, sent.length) : { json: whatifReply(body) }
      return route.fulfill({ status: out.status ?? 200, json: out.json })
    }
    if (req.method() !== 'GET') return route.abort()
    if (url.pathname === `/api/runs/${RUN}/evidence/whatif/rows`) {
      pages.push(Object.fromEntries(url.searchParams))
      const out = rowsReply(url)
      return route.fulfill({ status: out.status ?? 200, json: out.json })
    }
    const seg = url.pathname.match(new RegExp(`/api/runs/${RUN}/evidence/segments/([^/]+)$`))
    if (seg) return route.fulfill({ json: segmentReply(decodeURIComponent(seg[1])) })
    return route.continue()
  })
  await page.goto(`${WEB}/preview.html?syn=${syn}&theme=${theme}`, { waitUntil: 'networkidle' })
  await page.waitForSelector('[data-evidence-doc]')
  return { ctx, page, sent, pages, errors }
}

/** 点开一个片段，等指标步骤和「推演」开关出来 */
async function openMetric(page, sid) {
  await page.click(`[data-seg="${sid}"]`)
  await page.waitForSelector('[data-ev-metric] [data-whatif-toggle]')
}

/** 展开推演，等第一份答复画出来 */
async function openWhatIf(page) {
  await page.click('[data-whatif-toggle]')
  await page.waitForSelector('[data-whatif-result="unchanged"], [data-whatif-result="ok"]')
}

const settle = (page, ms = 700) => page.waitForTimeout(ms)
const text = (page, sel) => page.locator(sel).first().innerText()
/** 这个元素整个在面板正文（窄屏抽屉、宽屏侧栏都是 [data-ev-body] 在滚）和窗口的可见范围里 */
const inView = (page, sel) => page.evaluate((s) => {
  const el = document.querySelector(s)
  if (!el) return false
  const body = document.querySelector('[data-ev-body]')?.getBoundingClientRect() ?? { top: 0, bottom: innerHeight }
  const r = el.getBoundingClientRect()
  return r.top >= Math.max(0, body.top) - 1 && r.bottom <= Math.min(innerHeight, body.bottom) + 1
}, sel)
const shot = async (page, name) => {
  if (!SHOTS) return
  mkdirSync(SHOTS, { recursive: true })
  await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: false })
}

await section('打开推演', async () => {
  const { ctx, page, sent, errors } = await open('whatif')
  await openMetric(page, 's12')
  const toggle = page.locator('[data-whatif-toggle]')
  check('指标步骤上有「推演」，默认收起', (await toggle.getAttribute('aria-expanded')) === 'false')
  check('不推演时查询步骤里没有勾选框', await page.locator('[data-whatif-rows], [data-whatif-row] input').count() === 0)
  await openWhatIf(page)
  check('展开后开关标为展开、指向推演区', (await toggle.getAttribute('aria-expanded')) === 'true'
    && (await page.locator(`#${await toggle.getAttribute('aria-controls')}[data-whatif]`).count()) === 1)
  check('顶上醒目写着「推演，非出具」', (await text(page, '[data-whatif-banner]')).trim() === BANNER)
  check('第一次请求不带改动', JSON.stringify(sent[0]?.body) === JSON.stringify(
    { report: 'write', card: 'caliber', metric: 'wow', overrides: { inputs: {}, exclude_rows: {} } }), JSON.stringify(sent[0]?.body))
  check('原值 → 原值，标「无变化」', (await text(page, '[data-whatif-new]')) === '8.7%'
    && (await text(page, '[data-whatif-delta]')) === '无变化')
  const fields = await page.locator('[data-whatif-field]').count()
  check('两个数值输入都能改，框里是原值', fields === 2
    && await page.locator('[data-whatif-input="vars.kpi.gmv"] [data-whatif-field]').inputValue() === '45678.5')
  check('取自查询的输入给「去勾选要排除的行」', await page.locator('[data-whatif-pick]').count() === 2)
  check('输入落在的两个查询步骤里出现勾选框，都是这个指标用到的行、可勾', await page.locator('[data-whatif-rows]').count() === 2
    && await page.locator('[data-whatif-row] input[type="checkbox"]').count() === 2
    && await page.locator('[data-whatif-row] input[type="checkbox"]:disabled').count() === 0)
  check('报告里引用它的一处不受影响', (await text(page, '[data-whatif-segments-none]')).includes('1 处不受影响'))
  check('「恢复原值」在没有改动时不可点', await page.locator('[data-whatif-reset]').isDisabled())
  await shot(page, 'open-dark')
  check('页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('改一个输入', async () => {
  const { ctx, page, sent } = await open('whatif')
  await openMetric(page, 's12')
  await openWhatIf(page)
  const field = page.locator('[data-whatif-input="vars.kpi.gmv"] [data-whatif-field]')
  await field.fill('')
  const before = sent.length
  await field.pressSequentially('50000', { delay: 40 })
  await page.waitForTimeout(150)
  check('边打字不发请求', sent.length === before)
  await page.waitForSelector('[data-whatif-result="ok"]')
  check('停顿之后只发一次', sent.length === before + 1, `${sent.length - before} 次`)
  check('请求体带着改动的输入', JSON.stringify(sent.at(-1).body.overrides) === JSON.stringify({ inputs: { 'vars.kpi.gmv': 50000 }, exclude_rows: {} }),
    JSON.stringify(sent.at(-1).body.overrides))
  check('显示新值和变化量', (await text(page, '[data-whatif-new]')) === '19.0%'
    && (await text(page, '[data-whatif-delta]')) === '变化 +10.3 个百分点', await text(page, '[data-whatif-result]'))
  check('报告里会变的那一处：8.7% → 19.0%，可定位', (await text(page, '[data-whatif-segment="s12"]')).includes('19.0%')
    && await page.locator('[data-whatif-locate="s12"]').count() === 1)
  check('横幅一直在', (await text(page, '[data-whatif-banner]')).trim() === BANNER)

  // 回车立刻提交，不等停顿
  await field.fill('60000')
  const t0 = Date.now()
  await field.press('Enter')
  await page.waitForFunction(() => document.querySelector('[data-whatif-new]')?.textContent === '42.8%')
  check('回车立刻提交', sent.at(-1).body.overrides.inputs['vars.kpi.gmv'] === 60000 && sent.at(-1).at - t0 < 300,
    `${sent.at(-1).at - t0}ms`)
  // 写的不是数：提示，保留上一次的结果
  await field.fill('六万')
  await settle(page)
  check('不是数：提示「请输入数值」、不发请求', (await page.locator('[data-whatif-input="vars.kpi.gmv"]').innerText()).includes('请输入数值')
    && sent.at(-1).body.overrides.inputs['vars.kpi.gmv'] === 60000)
  // Esc 退回已提交的值，面板不关
  await field.press('Escape')
  check('Esc 退回已提交的值，面板还开着', await field.inputValue() === '60000' && await page.locator('[data-evidence-panel]').count() === 1)

  // 定位：焦点回到正文里那一处，推演不收起
  await page.click('[data-whatif-locate="s12"]')
  await page.waitForTimeout(200)
  check('定位到正文里那一处，推演照旧展开', await page.evaluate(() => document.activeElement?.getAttribute('data-seg')) === 's12'
    && await page.locator('[data-whatif]').count() === 1 && (await text(page, '[data-whatif-new]')) === '42.8%')
  await ctx.close()
})

await section('勾掉一行', async () => {
  const { ctx, page, sent } = await open('whatif')
  await openMetric(page, 's12')
  await openWhatIf(page)
  const box = page.locator(`[data-ev-query="${Q4}"] [data-whatif-row="0"] input[type="checkbox"]`)
  check('勾选框的读屏名从 1 数', (await box.getAttribute('aria-label')) === '排除第 1 行')
  await box.check()
  await page.waitForSelector('[data-whatif-result="excluded"]')
  check('请求体带 exclude_rows（快照工件 id → 从 0 数的行号）',
    JSON.stringify(sent.at(-1).body.overrides) === JSON.stringify({ inputs: {}, exclude_rows: { [Q4]: [0] } }), JSON.stringify(sent.at(-1).body.overrides))
  check('这一行划掉', await page.locator(`[data-ev-query="${Q4}"] [data-whatif-row="0"][data-excluded]`).count() === 1)
  check('排除后无法计算：写明原因、新值是「—」', (await text(page, '[data-whatif-reason]')).startsWith('排除后无法计算')
    && (await text(page, '[data-whatif-new]')) === '—')
  check('输入下写明排除了哪一行', (await text(page, `[data-whatif-input="cell(nodes.pull, 0, 'gmv')"] [data-whatif-excluded]`)) === '已排除第 1 行')
  check('结果窗口下写明已排除几行', (await text(page, `[data-ev-query="${Q4}"] [data-whatif-excluded-count]`)) === '已排除 1 行')
  await shot(page, 'excluded-dark')

  // 恢复原值：输入框、勾选框、结果都回去
  await page.locator('[data-whatif-input="vars.kpi.gmv"] [data-whatif-field]').fill('50000')
  await page.locator('[data-whatif-input="vars.kpi.gmv"] [data-whatif-field]').press('Enter')
  await settle(page, 300)
  await page.click('[data-whatif-reset]')
  await page.waitForSelector('[data-whatif-result="unchanged"]')
  check('恢复原值：结果回到原值', (await text(page, '[data-whatif-new]')) === '8.7%')
  check('恢复原值：输入框回到原值、勾选框都放开',
    await page.locator('[data-whatif-input="vars.kpi.gmv"] [data-whatif-field]').inputValue() === '45678.5'
    && !(await box.isChecked()) && await page.locator('[data-whatif-row][data-excluded]').count() === 0)
  check('恢复原值后按钮不可点', await page.locator('[data-whatif-reset]').isDisabled())

  // 收起：勾选框跟着收起，结果窗口换回原来的表格
  await page.click('[data-whatif-toggle]')
  check('收起后推演区和勾选框都没了，查询结果照常显示', await page.locator('[data-whatif], [data-whatif-rows]').count() === 0
    && await page.locator(`[data-ev-query="${Q4}"] table`).count() === 1)
  await ctx.close()
})

await section('多行快照与遮罩', async () => {
  const { ctx, page, sent } = await open('whatif')
  await openMetric(page, 's70')
  await openWhatIf(page)
  const rows = page.locator(`[data-ev-query="${Q3}"] [data-whatif-row]`)
  check('窗口里每行一个勾选框', await rows.count() === chainOf('s70').find((s) => s.step === 'query').rows.length)
  check('遮罩列照旧写「已遮罩」', await page.locator(`[data-ev-query="${Q3}"] [data-whatif-rows] td[data-masked]`).count() > 0
    && (await page.locator(`[data-ev-query="${Q3}"] [data-whatif-rows] td[data-masked]`).first().innerText()).trim() === '已遮罩')
  await page.locator(`[data-ev-query="${Q3}"] [data-whatif-row="5"] input`).check()
  await page.waitForSelector('[data-whatif-result="excluded"]')
  check('勾掉第 6 行：请求里是行号 5', JSON.stringify(sent.at(-1).body.overrides.exclude_rows) === JSON.stringify({ [Q3]: [5] }))
  await ctx.close()
})

await section('只让这个指标用到的行可勾', async () => {
  const { ctx, page, sent } = await open('whatif')
  await openMetric(page, 's70')
  await openWhatIf(page)
  const row = (n) => page.locator(`[data-ev-query="${Q3}"] [data-whatif-row="${n}"]`)
  check('用到的第 6、12 行可勾', !(await row(5).locator('input').isDisabled()) && !(await row(11).locator('input').isDisabled()))
  const unused = await page.locator(`[data-ev-query="${Q3}"] [data-whatif-row][data-whatif-row-unused]`).count()
  check('前后几行上下文不能勾，悬停写明没用到', unused === 6 && await row(4).locator('input').isDisabled()
    && (await row(4).locator('td').first().getAttribute('title')) === '这个指标没有用到这一行，排除它不影响结果', `${unused} 行`)
  const before = sent.length
  await row(4).locator('input').click({ force: true })
  await settle(page, 300)
  check('点了不起作用的行也不发请求', sent.length === before && !(await row(4).locator('input').isChecked()))
  check('表格下面说明只有用到的行能勾', (await text(page, `[data-ev-query="${Q3}"] [data-whatif-rows]`)).includes('没有用到的行不能勾选'))
  await ctx.close()
})

await section('整份结果作输入：推演区里勾行', async () => {
  const { ctx, page, sent, pages } = await open('whatif')
  await openMetric(page, 's18')
  await openWhatIf(page)
  check('证据链里没有它的查询步骤', await page.locator('[data-ev-query]').count() === 0)
  const toggle = page.locator('[data-whatif-input="vars.orders.rows"] [data-whatif-pick-here]')
  check('输入下面给「勾选要排除的行（共 12 行）」', (await toggle.innerText()).includes('勾选要排除的行（共 12 行）'))
  await toggle.click()
  await page.waitForSelector('[data-whatif-snapshot] [data-whatif-row]')
  check('按页取这份快照，从第 0 行起', pages[0]?.artifact === Q3 && pages[0]?.offset === '0', JSON.stringify(pages[0]))
  const rows = page.locator('[data-whatif-snapshot] [data-whatif-row]')
  check('12 行都在、都能勾', await rows.count() === 12
    && await page.locator('[data-whatif-snapshot] [data-whatif-row] input:disabled').count() === 0)
  check('遮罩列写「已遮罩」', await page.locator('[data-whatif-snapshot] td[data-masked]').count() === 12)
  check('写明是第几行到第几行', (await text(page, '[data-whatif-snapshot-range]')) === '第 1–12 行，共 12 行')
  await page.locator('[data-whatif-snapshot] [data-whatif-row="7"] input').check()
  await page.waitForSelector('[data-whatif-result="ok"]')
  check('请求体带 exclude_rows：第 8 行是行号 7',
    JSON.stringify(sent.at(-1).body.overrides) === JSON.stringify({ inputs: {}, exclude_rows: { [Q3]: [7] } }), JSON.stringify(sent.at(-1).body.overrides))
  check('新值随之变化：12单 → 11单', (await text(page, '[data-whatif-result] [data-whatif-new]')) === '11单'
    && (await text(page, '[data-whatif-result] [data-whatif-delta]')) === '变化 -1单')
  check('勾行的表格底下带着推演结果', (await text(page, '[data-whatif-snapshot] [data-whatif-summary]')).includes('11单'))
  check('输入下写明排除了哪一行', (await text(page, '[data-whatif-input="vars.orders.rows"] [data-whatif-excluded]')) === '已排除第 8 行')
  check('报告里引用它的那一处跟着变', (await text(page, '[data-whatif-segment="s18"]')).includes('11单'))
  await shot(page, 'table-input-dark')
  await ctx.close()
})

await section('勾行后结果就在手边', async () => {
  const { ctx, page } = await open('whatif')
  await openMetric(page, 's12')
  await openWhatIf(page)
  await page.locator(`[data-whatif-input="cell(nodes.pull, 0, 'gmv')"] [data-whatif-pick]`).click()
  await page.locator(`[data-ev-query="${Q4}"] [data-whatif-row="0"] input`).check()
  await page.waitForSelector(`[data-ev-query="${Q4}"] [data-whatif-summary="excluded"]`)
  const summary = await text(page, `[data-ev-query="${Q4}"] [data-whatif-summary]`)
  check('勾了行，表格底下就写着新值和原因', summary.includes('—') && summary.includes('排除后无法计算'), summary)
  await page.waitForTimeout(200)
  check('答复回来后，表格底下这份结果整个在视野里', await inView(page, `[data-ev-query="${Q4}"] [data-whatif-summary]`),
    JSON.stringify(await page.evaluate((s) => {
      const r = document.querySelector(s)?.getBoundingClientRect()
      const b = document.querySelector('[data-ev-body]')?.getBoundingClientRect()
      return { top: r?.top, bottom: r?.bottom, bodyTop: b?.top, bodyBottom: b?.bottom, h: innerHeight }
    }, `[data-ev-query="${Q4}"] [data-whatif-summary]`)))
  await page.click(`[data-ev-query="${Q4}"] [data-whatif-back]`)
  await page.waitForTimeout(250)
  check('「回到推演结果」：焦点回到推演结果、滚进视野', await page.evaluate(() => document.activeElement?.hasAttribute('data-whatif-result'))
    && await inView(page, '[data-whatif-result]'))
  await ctx.close()
})

await section('不支持的输入', async () => {
  const { ctx, page } = await open('whatif')
  await openMetric(page, 's8')
  await openWhatIf(page)
  check('不支持推演的输入列出原因', (await text(page, '[data-whatif-unsupported-input="vars.kpi.region"]'))
    .includes('不是数值，不能修改'))
  check('文本输入照实显示、不给输入框；数值输入照常能改', await page.locator('[data-whatif-field]').count() === 1
    && (await text(page, '[data-whatif-input="vars.kpi.region"] [data-whatif-value]')).includes('华东')
    && await page.locator('[data-whatif-input="vars.kpi.order_cnt"] [data-whatif-field]').count() === 1)
  check('指标照常推演：原值 → 原值，标「无变化」', (await text(page, '[data-whatif-new]')) === '1,234单'
    && (await text(page, '[data-whatif-delta]')) === '无变化')
  check('能改的那一格所在的查询步骤里出现勾选框', await page.locator(`[data-ev-query="${Q1}"] [data-whatif-row="0"] input:not(:disabled)`).count() === 1)
  await ctx.close()

  const bare = await open('whatif')
  await openMetric(bare.page, 's6')
  await openWhatIf(bare.page)
  check('加工过的列表：列出原因、不给输入框', (await text(bare.page, '[data-whatif-unsupported-input="vars.kpi.daily"]'))
    .includes('列表或对象不能直接修改') && await bare.page.locator('[data-whatif-field]').count() === 0
    && (await text(bare.page, '[data-whatif-input="vars.kpi.daily"] [data-whatif-value]')).includes('[7 项]'))
  check('没有能改的输入、也没有能勾的行：说一句为什么', (await text(bare.page, '[data-whatif-nothing]')).includes('没有可以修改的输入'))
  await bare.ctx.close()
})

/** 整个指标推演不了：展开推演，等结果区写出原因。交回结果区的字（压成一行，好写进检查项的说明） */
async function openWhole(page) {
  await page.click('[data-whatif-toggle]')
  await page.waitForSelector('[data-whatif-result="whole"]')
  return (await text(page, '[data-whatif-result]')).replace(/\s+/g, ' ').trim()
}

await section('整个指标推演不了：输入还原不了', async () => {
  const { ctx, page, sent, errors } = await open('whatif')
  await openMetric(page, 's22')
  const result = await openWhole(page)
  check('一打开就写明哪个输入还原不了、为什么', result.includes(LOST), result)
  check('不写「无变化」，也不画「→」', !result.includes('无变化') && !result.includes('→'), result)
  check('本来能改的数也不给输入框，不列输入、不支持的输入、报告中会变的地方',
    await page.locator('[data-whatif-field], [data-whatif-inputs], [data-whatif-unsupported], [data-whatif-segments]').count() === 0)
  check('「恢复原值」不可点，只发过一次不带改动的请求', await page.locator('[data-whatif-reset]').isDisabled() && sent.length === 1
    && JSON.stringify(sent[0].body.overrides) === JSON.stringify({ inputs: {}, exclude_rows: {} }))
  check('横幅照旧在', (await text(page, '[data-whatif-banner]')).trim() === BANNER)
  await shot(page, 'whole-lost-dark')
  check('页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('整个指标推演不了：用到了遮罩列', async () => {
  const { ctx, page } = await open('whatif')
  await openMetric(page, 's10')
  const result = await openWhole(page)
  check('一打开就写明用到了遮罩列', result.includes(MASKED_METRIC), result)
  check('不写「无变化」，也不画「→」', !result.includes('无变化') && !result.includes('→'), result)
  check('本来能改的订单数也不给输入框', await page.locator('[data-whatif-field], [data-whatif-inputs]').count() === 0)
  check('查询步骤里不出勾选框，结果窗口照常显示（遮罩列写「已遮罩」）', await page.locator('[data-whatif-rows], [data-whatif-row]').count() === 0
    && await page.locator(`[data-ev-query="${Q1}"] table`).count() === 1
    && (await text(page, `[data-ev-query="${Q1}"] table`)).includes('已遮罩'))
  check('不说报告里引用它的地方「不受影响」', await page.locator('[data-whatif-segments], [data-whatif-segments-none]').count() === 0)
  await ctx.close()
})

await section('整个指标推演不了：口径卡没记输入', async () => {
  const { ctx, page } = await open('whatif')
  await openMetric(page, 's16')
  const result = await openWhole(page)
  check('只说原因：口径卡没有记录输入', result.includes('口径卡没有记录输入，不能推演'), result)
  check('不写「无变化」，也不说「没有引用任何输入」', !result.includes('无变化') && !result.includes('→')
    && await page.locator('[data-whatif-empty], [data-whatif-inputs], [data-whatif-unsupported]').count() === 0)
  check('不画勾选框', await page.locator('[data-whatif-rows]').count() === 0)
  await ctx.close()
})

await section('空态', async () => {
  const { ctx, page } = await open('whatif')
  await openMetric(page, 's24')
  await openWhatIf(page)
  check('原式没有引用输入：说清无可推演，不画空列表', (await text(page, '[data-whatif-empty]')).includes('无可推演')
    && await page.locator('[data-whatif-input]').count() === 0)
  check('没有报告引用时不画「报告中会变的地方」', await page.locator('[data-whatif-segments]').count() === 0)
  await ctx.close()
})

await section('加载、失败、未封存', async () => {
  let release
  const gate = new Promise((r) => { release = r })
  const slow = await open('whatif', { reply: async (body, n) => {
    if (n === 1) await gate
    return { json: whatifReply(body) }
  } })
  await openMetric(slow.page, 's12')
  await slow.page.click('[data-whatif-toggle]')
  await slow.page.waitForSelector('[data-whatif-loading]')
  check('答复回来之前显示「推演中」，横幅已经在', (await text(slow.page, '[data-whatif-loading]')).includes('推演中')
    && (await text(slow.page, '[data-whatif-banner]')).trim() === BANNER)
  release()
  await slow.page.waitForSelector('[data-whatif-result="unchanged"]')
  check('答复回来后加载提示消失', await slow.page.locator('[data-whatif-loading]').count() === 0)
  await slow.ctx.close()

  const flaky = await open('whatif', { reply: async (body, n) => (n === 1
    ? { status: 500, json: { detail: '推演服务暂时不可用' } } : { json: whatifReply(body) }) })
  await openMetric(flaky.page, 's12')
  await flaky.page.click('[data-whatif-toggle]')
  await flaky.page.waitForSelector('[data-whatif] [role="alert"]')
  check('失败时给出原因和「重试」', (await text(flaky.page, '[data-whatif] [role="alert"]')).includes('重试'))
  await flaky.page.locator('[data-whatif] [role="alert"]').getByRole('button', { name: '重试' }).click()
  await flaky.page.waitForSelector('[data-whatif-result="unchanged"]')
  check('重试之后正常显示', await flaky.page.locator('[data-whatif] [role="alert"]').count() === 0)
  await flaky.ctx.close()

  const open_ = await open('whatif', { reply: async (body) => ({ json: { ...whatifReply(body), sealed: false, seal: { sealed: false, ok: null } } }) })
  await openMetric(open_.page, 's12')
  await openWhatIf(open_.page)
  check('运行未封存时提示数据可能还会变', (await text(open_.page, '[data-whatif-unsealed]')) === '运行未封存，数据可能还会变')
  await open_.ctx.close()

  const broken = await open('whatif', { reply: async (body) => ({ json: { ...whatifReply(body), sealed: false, seal: { sealed: true, ok: false } } }) })
  await openMetric(broken.page, 's12')
  await openWhatIf(broken.page)
  check('封存核对未通过时照实说', (await text(broken.page, '[data-whatif-unsealed="broken"]')) === '封存核对未通过，推演所依据的数据可能已被改动')
  await broken.ctx.close()
})

await section('加载中、失败后不留上一次的答复', async () => {
  let release
  const gate = new Promise((r) => { release = r })
  const { ctx, page } = await open('whatif', { reply: async (body, n) => {
    if (n === 2) await gate
    if (n === 3) return { status: 500, json: { detail: '推演服务暂时不可用' } }
    return { json: whatifReply(body) }
  } })
  await openMetric(page, 's12')
  await openWhatIf(page)
  await page.locator(`[data-ev-query="${Q4}"] [data-whatif-row="0"] input`).check()
  await page.waitForSelector('[data-whatif-segments="loading"]')
  check('答复回来之前：报告中会变的地方变淡、标为忙，结果也是', (await page.locator('[data-whatif-segments]').getAttribute('aria-busy')) === 'true'
    && await page.locator('[data-whatif-result="loading"]').count() === 1)
  release()
  await page.waitForSelector('[data-whatif-result="excluded"]')
  check('答复回来后按这一次画：排除标记、下游口径卡', await page.locator('[data-whatif-excluded]').count() === 1
    && await page.locator('[data-whatif-affected=""]').count() === 1)
  const field = page.locator('[data-whatif-input="vars.kpi.gmv"] [data-whatif-field]')
  await field.fill('50000')
  await field.press('Enter')
  await page.waitForSelector('[data-whatif] [role="alert"]')
  check('请求失败：上一次的结果、排除标记、报告中会变的地方、下游口径卡都不再显示',
    await page.locator('[data-whatif-result], [data-whatif-excluded], [data-whatif-segments], [data-whatif-affected]').count() === 0)
  check('输入框还在，能接着改', await page.locator('[data-whatif-field]').count() === 2)
  await ctx.close()
})

/** 页面和面板正文都没有横向滚动（表格自己的横向滚动条不算：那是表格容器里的） */
async function overflow(page) {
  return page.evaluate(() => {
    const doc = document.documentElement
    const body = document.querySelector('[data-ev-body]')
    const box = document.querySelector('[data-whatif]')
    return { page: doc.scrollWidth - doc.clientWidth, body: body ? body.scrollWidth - body.clientWidth : 0,
      whatif: box ? box.scrollWidth - box.clientWidth : 0 }
  })
}

await section('窄屏', async () => {
  for (const theme of ['dark', 'light']) {
    const { ctx, page, errors } = await open('whatif', { width: 390, height: 844, theme })
    await openMetric(page, 's12')
    check(`390px（${theme}）：面板从底部抽出`, await page.locator('[data-evidence-panel="drawer"]').count() === 1)
    await openWhatIf(page)
    await page.locator('[data-whatif-input="vars.kpi.gmv"] [data-whatif-field]').fill('50000')
    await page.locator('[data-whatif-input="vars.kpi.gmv"] [data-whatif-field]').press('Enter')
    await page.waitForSelector('[data-whatif-result="ok"]')
    await page.locator(`[data-ev-query="${Q4}"] [data-whatif-row="0"] input`).check()
    await page.waitForSelector('[data-whatif-result="excluded"]')
    const o = await overflow(page)
    check(`390px（${theme}）：推演展开、勾了行也没有横向溢出`, o.page <= 0 && o.body <= 1 && o.whatif <= 1, JSON.stringify(o))
    await shot(page, `narrow-${theme}`)
    // 推演区里另给的勾行表格（四列，带遮罩列）在窄屏里同样不撑宽
    // 先关上抽屉再点另一处：抽屉盖着正文下半截
    await page.locator('[data-evidence-panel] button[aria-label="关闭证据"]').click()
    await openMetric(page, 's18')
    await openWhatIf(page)
    await page.click('[data-whatif-pick-here]')
    await page.waitForSelector('[data-whatif-snapshot] [data-whatif-row]')
    const t = await overflow(page)
    check(`390px（${theme}）：推演区里的勾行表格不横向溢出`, t.page <= 0 && t.body <= 1 && t.whatif <= 1, JSON.stringify(t))
    // 整个指标推演不了：结果区只有原值和原因，原因里的输入路径长也要折行
    await page.locator('[data-evidence-panel] button[aria-label="关闭证据"]').click()
    await openMetric(page, 's22')
    await openWhole(page)
    const w = await overflow(page)
    check(`390px（${theme}）：整个指标推演不了时不横向溢出`, w.page <= 0 && w.body <= 1 && w.whatif <= 1, JSON.stringify(w))
    await shot(page, `narrow-whole-${theme}`)
    check(`390px（${theme}）：页面没有报错`, errors.length === 0, errors.join('；'))
    await ctx.close()
  }
  const rail = await open('whatif-rail', { width: 1100, height: 900 })
  await openMetric(rail.page, 's70')
  check('360px 画布右栏：面板栏内展开', await rail.page.locator('[data-evidence-panel="inline"]').count() === 1)
  await openWhatIf(rail.page)
  const o = await rail.page.evaluate(() => {
    const box = document.querySelector('[data-fixture="whatif-rail"]')
    const panel = document.querySelector('[data-whatif]')
    return { rail: box.scrollWidth - box.clientWidth, whatif: panel.scrollWidth - panel.clientWidth }
  })
  check('360px 画布右栏：推演区不撑宽右栏', o.rail <= 0 && o.whatif <= 1, JSON.stringify(o))
  await shot(rail.page, 'rail-dark')
  await rail.ctx.close()
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
    const { ctx, page } = await open('whatif', { theme })
    await openMetric(page, 's12')
    await openWhatIf(page)
    const c = await page.evaluate(() => {
      const banner = document.querySelector('[data-whatif-banner]')
      const panel = document.querySelector('[data-evidence-panel]')
      const s = getComputedStyle(banner)
      return { fg: s.color, bg: s.backgroundColor, base: getComputedStyle(panel).backgroundColor,
        weight: Number(s.fontWeight) }
    })
    const ratio = contrast(c.fg, c.bg, c.base)
    check(`${theme}：横幅文字对比度 ≥ 4.5`, ratio >= 4.5, `${ratio.toFixed(2)}（${c.fg} on ${c.bg}）`)
    check(`${theme}：横幅加粗`, c.weight >= 500, String(c.weight))
    await shot(page, `open-${theme}`)
    await ctx.close()

    // 整个指标推演不了时，结果区里只剩一句原因：它就是要人读的那句
    const whole = await open('whatif', { theme })
    await openMetric(whole.page, 's10')
    await openWhole(whole.page)
    const r = await whole.page.evaluate(() => {
      const reason = document.querySelector('[data-whatif-reason]')
      const panel = document.querySelector('[data-evidence-panel]')
      return { fg: getComputedStyle(reason).color, base: getComputedStyle(panel).backgroundColor }
    })
    const reasonRatio = contrast(r.fg, r.base, r.base)
    check(`${theme}：整个指标推演不了的原因对比度 ≥ 4.5`, reasonRatio >= 4.5, `${reasonRatio.toFixed(2)}（${r.fg} on ${r.base}）`)
    await shot(whole.page, `whole-masked-${theme}`)
    await whole.ctx.close()
  }
})

await browser.close()
console.log(failed ? `\n✗ 数字推演：${failed} 项未通过` : '\n✓ 数字推演：全部通过')
process.exit(failed ? 1 : 0)
