// 数据目录页（数据源卡片上的「数据目录」，/data/catalog/<源>）的交互回归。
//
// 数据目录的写操作都会改变助手和将来的 SQL 检查读到的东西：确认一项、驳回一项、整份提交都不是随手可以撤回的。
// 所以这里查的是「请求里带没带读到的版本」「批量确认只动推断项、来源不变」「编辑时没动的项原样交回（被驳回的照旧
// 驳回）」「别人刚改过时有没有停下来让人重新载入，而不是悄悄覆盖」「起草按批调用、能停、出错时说得清」——
// 正常路径的页面检查一个字都不会说。
//
// 写法同 check-versions：沙箱里的数据源多半没探查过结构，目录也是空的，所以数据源列表、目录清单、单表目录都用
// page.route 伪造（虚构的景区业务库和一个导入表格的源）；写请求（整份提交、单项审阅、起草）也在浏览器层答掉，
// 照服务端的规则改这里的假状态。没配的写请求一律拦成 503，不写库；最后核对真库里的数据源和目录数量没有变化。
// 跑之前前后端都得起着（./scripts/dev.sh），默认连 5273 / 8000；对别的实例跑时带上地址：
//   AGENTLAB_WEB=http://localhost:<前端端口> AGENTLAB_API=http://localhost:<后端端口>/api node scripts/check-catalog.mjs
// 只跑其中几节：CHECK_ONLY=清单,编辑 node scripts/check-catalog.mjs（按节名包含匹配）。
// 截图：CHECK_SHOTS=/某个目录 时把关键状态存下来；CHECK_THEME=light 换浅色跑一遍。
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const API = process.env.AGENTLAB_API ?? 'http://localhost:8000/api'
const SHOTS = process.env.CHECK_SHOTS ?? ''
const THEME = process.env.CHECK_THEME === 'light' ? 'light' : 'dark'
// 不用 playwright install：系统 Chrome 就够了
const CHROME = process.env.CHROME_PATH ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
/** 本机署名：写请求带的 X-Actor */
const ACTOR = '检查脚本'

let failed = 0
const check = (name, cond, detail = '') => {
  console.log(`  ${cond ? '✓' : '✗'} ${name}${detail ? ` — ${detail}` : ''}`)
  if (!cond) failed++
}

try {
  const r = await fetch(`${API}/datasources/kinds`)
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
} catch (e) {
  console.error(`✗ 连不上后端（${API}）——先起沙箱后端\n  ${e.message}`)
  process.exit(1)
}

// ---------------------------------------------------------------------------
// 真库的数量：开头记一次，最后再数一次，必须一样（检查本身不许写库）
// ---------------------------------------------------------------------------
async function realCounts() {
  const sources = await (await fetch(`${API}/datasources`)).json()
  let entries = 0
  let versions = 0
  for (const s of sources) {
    const r = await fetch(`${API}/datasources/${s.id}/catalog`)
    if (!r.ok) continue
    const d = await r.json()
    for (const t of d.tables ?? []) {
      if (t.version > 0) entries++
      versions += t.version
    }
  }
  return { sources: sources.length, entries, versions }
}
const before = await realCounts()

const browser = await chromium.launch({ executablePath: CHROME })
const opened = new Set()

/**
 * 开一页：handlers 按「METHOD 路径正则」匹配（GET 也可以配），没配的 GET 放行到后端，没配的写请求一律
 * 拦成 503。所有原生对话框都算失败
 */
async function open(path, { handlers = [], viewport = { width: 1440, height: 900 } } = {}) {
  const ctx = await browser.newContext({ viewport, colorScheme: THEME, timezoneId: 'Asia/Shanghai' })
  opened.add(ctx)
  ctx.setDefaultTimeout(6000)
  ctx.setDefaultNavigationTimeout(30000)
  await ctx.addInitScript(([t, actor]) => {
    try {
      localStorage.setItem('agentlab.theme', t)
      localStorage.removeItem('agentlab.health')
      localStorage.setItem('agentlab_actor', actor)
    } catch { /* noop */ }
  }, [THEME, ACTOR])
  const page = await ctx.newPage()
  const sent = []
  const errors = []
  const natives = []
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('dialog', (d) => { natives.push(`${d.type()}: ${d.message().slice(0, 40)}`); void d.dismiss() })
  await page.route((u) => new URL(u).pathname.startsWith('/api/'), async (route) => {
    const r = route.request()
    const url = new URL(r.url())
    const key = `${r.method()} ${decodeURIComponent(url.pathname.replace(/^\/api/, ''))}`
    for (const [pattern, handle] of handlers) {
      if (pattern.test(key)) {
        let body = null
        try { body = r.postDataJSON() } catch { body = r.postData() }
        sent.push({ key, body, actor: r.headers()['x-actor'] ?? null })
        return handle(route, { body, url, key })
      }
    }
    if (r.method() === 'GET') return route.continue()
    sent.push({ key, body: null, blocked: true })
    return route.fulfill({ status: 503, body: '' })
  })
  await page.goto(`${WEB}${path}`, { waitUntil: 'networkidle' })
  await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), THEME)
  await page.waitForTimeout(400)
  return { page, sent, errors, natives, close: () => { opened.delete(ctx); return ctx.close() } }
}
const goto = async (page, path) => {
  await page.goto(`${WEB}${path}`, { waitUntil: 'networkidle' })
  await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), THEME)
  await page.waitForTimeout(300)
}
const json = (data, status = 200) => (route) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) })
const until = async (fn, ms = 6000, step = 120) => {
  const end = Date.now() + ms
  let v = await fn()
  while (!v && Date.now() < end) { await new Promise((r) => setTimeout(r, step)); v = await fn() }
  return v
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const shot = async (page, name) => { if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png` }) }
const writes = (sent, re) => sent.filter((s) => re.test(s.key) && !s.key.startsWith('GET '))
const count = (sent, re) => sent.filter((s) => re.test(s.key)).length
const lastBody = (sent, re) => sent.filter((s) => re.test(s.key)).at(-1)?.body ?? null

const ONLY = process.env.CHECK_ONLY?.split(',').map((x) => x.trim()).filter(Boolean)
async function section(name, fn) {
  if (ONLY?.length && !ONLY.some((k) => name.includes(k))) return
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

// ---------------------------------------------------------------------------
// 夹具：虚构的景区业务库（入园记录、订单、票种、门店销售……共 152 张表，其中一张只剩目录、表结构里已经没有）、
// 一个导入表格的源（说明由系统生成）、一个还没探查结构的库、一个有表结构但还没有目录的库。名字、数字全是假的
// ---------------------------------------------------------------------------
const T0 = '2026-09-28T02:00:00+00:00'
const INIT = { fk: 'verified', human: 'confirmed' }
const item = (value, source = 'llm', status) => ({ value, source, status: status ?? INIT[source] ?? 'proposed', updated_at: T0 })
const rel = (columns, to_table, to_columns, source, extra = {}) => ({
  id: relId(columns, to_table, to_columns), columns, to_table, to_columns, cardinality: 'many_to_one', coverage: null,
  source, status: INIT[source] ?? 'proposed', updated_at: T0, ...extra,
})
/** 关系编号：服务端按两端的表和列算哈希，这里只要稳定、不含句点 */
const relId = (columns, to_table, to_columns) => `r${[...columns, to_table, ...to_columns].join('-').toLowerCase().replace(/[^a-z0-9-]/g, '')}`
const col = (name, type = 'INTEGER', extra = {}) => ({ name, type, pk: false, not_null: false, comment: null, ...extra })
const pk = (name = 'id') => col(name, 'INTEGER', { pk: true, not_null: true })

const S1 = 'check-cat-scenic'
const S2 = 'check-cat-upload'
const S3 = 'check-cat-empty'
const S4 = 'check-cat-blank'

const VISITS_COLUMNS = [pk(), col('ticket_no', 'TEXT'), col('park_id'), col('gate_id'), col('ticket_type_id'), col('member_id'),
  col('visit_time', 'TEXT'), col('visitor_count'), col('status')]
const visitsNotes = () => ({
  label: item('入园记录'),
  description: item('每张门票每次通过闸机入园记一行'),
  grain: item('一张门票的一次入园'),
  keys: item(['ticket_no']),
  kind: item('fact'),
  business_date: item({ column: 'visit_time', rule: '按入园时间的日期', timezone: 'Asia/Shanghai' }),
  valid_filter: item('status <> 9', 'human'),
  dedup: item('按 ticket_no 去重', 'llm', 'rejected'),
  columns: {
    ticket_no: { label: item('票号'), measure: item('identifier', 'name') },
    park_id: { measure: item('identifier', 'fk') },
    visitor_count: { label: item('入园人数'), unit: item('人'), measure: item('flow') },
    status: { label: item('状态'), measure: item('status', 'human'), codes: item({ 1: '已入园', 9: '已作废' }) },
  },
  relations: [
    rel(['park_id'], 'parks', ['id'], 'fk'),
    rel(['gate_id'], 'gates', ['id'], 'name'),
    rel(['ticket_type_id'], 'ticket_types', ['id'], 'fk'),
  ],
})
const WIDE_COLUMNS = [pk(), col('store_id'), col('productId'), col('member_id'), col('sold_at', 'TEXT'), col('qty'), col('amount', 'REAL'),
  ...Array.from({ length: 43 }, (_, i) => col(`promo_attr_${String(i + 1).padStart(2, '0')}`, 'TEXT'))]
const wideNotes = () => ({
  label: item('门店销售'),
  kind: item('fact'),
  columns: {
    qty: { label: item('数量'), measure: item('flow') },
    amount: { label: item('销售金额'), unit: item('元'), measure: item('flow') },
    ...Object.fromEntries(Array.from({ length: 6 }, (_, i) => [`promo_attr_${String(i + 1).padStart(2, '0')}`, { label: item(`促销属性 ${i + 1}`) }])),
  },
  relations: [rel(['store_id'], 'stores', ['id'], 'fk')],
})

/** 一张表：structure 为 null 表示表结构里已经没有（目录还留着） */
const table = (name, usage, columns, notes, extra = {}) => ({
  name, usage, columns, notes, version: notes ? (extra.version ?? 2) : 0, comment: null, is_view: false,
  updated_at: notes ? T0 : null, updated_by: notes ? '审核甲' : null, missing: false, ...extra,
})

function scenicTables() {
  const list = [
    table('visits', 42, VISITS_COLUMNS, visitsNotes()),
    table('orders', 30, [pk(), col('order_no', 'TEXT'), col('member_id'), col('total_amount', 'REAL')], {
      label: item('订单', 'human'), kind: item('fact', 'human'), relations: [rel(['member_id'], 'members', ['id'], 'fk')],
    }),
    table('order_items', 25, [pk(), col('order_id'), col('ticket_type_id'), col('qty'), col('amount', 'REAL')], {
      label: item('订单明细'), grain: item('一个订单里的一个票种'), kind: item('fact'),
      columns: { qty: { measure: item('flow') }, amount: { unit: item('元'), measure: item('flow') } },
      relations: [rel(['order_id'], 'orders', ['id'], 'fk'), rel(['ticket_type_id'], 'ticket_types', ['id'], 'name')],
    }),
    table('parks', 18, [pk(), col('park_code', 'TEXT'), col('name', 'TEXT')], {
      label: item('景区', 'human'), kind: item('dimension', 'human'), keys: item(['park_code'], 'human'),
    }),
    table('ticket_types', 12, [pk(), col('park_id'), col('name', 'TEXT'), col('base_price', 'REAL')], {
      label: item('票种'), kind: item('dimension'), relations: [rel(['park_id'], 'parks', ['id'], 'fk')],
    }),
    table('gates', 12, [pk(), col('park_id'), col('gate_code', 'TEXT')], null),
    table('store_sales', 9, WIDE_COLUMNS, wideNotes()),
    table('members', 7, [pk(), col('member_no', 'TEXT'), col('joined_on', 'TEXT')], null),
    table('channels', 5, [pk(), col('channel_code', 'TEXT')], null),
    table('stores', 4, [pk(), col('store_code', 'TEXT'), col('park_id')], null),
    table('inventory_snapshots', 3, [pk(), col('storeID'), col('snapshot_date', 'TEXT'), col('on_hand')], {
      label: item('库存快照'), kind: item('snapshot'), columns: { on_hand: { measure: item('stock') } },
    }),
  ]
  for (let i = 1; i <= 140; i++) list.push(table(`ext_log_${String(i).padStart(3, '0')}`, 0, [pk(), col('event_at', 'TEXT'), col('payload', 'TEXT')], null))
  list.push(table('legacy_coupons', 0, null, { label: item('旧版优惠券', 'human') }, { version: 3, missing: true }))
  return list
}

function uploadTables() {
  return [
    table('日客流', 6, [col('统计日期', 'DATE', { comment: '统计日期，与核对结果一致' }), col('入园人数', 'INTEGER', { comment: '当日入园人数合计，已与表内合计核对' })], {
      kind: item('fact'), columns: { 入园人数: { measure: item('flow') } },
    }, { comment: '每日入园人数汇总，由导入时的核对结果生成' }),
    table('时段客流', 2, [col('统计日期', 'DATE'), col('时段', 'TEXT'), col('入园人数', 'INTEGER')], null, { comment: '按时段汇总的入园人数' }),
  ]
}

const blank = {
  kind: 'sqlite', host: null, port: null, username: null, options: {}, readonly: true, description: '', enabled: true,
  password_masked: '', has_password: false, cached_schema: '', schema_error: '', available_schemas: [],
  last_checked_at: null, last_check_ok: null, last_latency_ms: null, last_error: null, current_snapshot: null,
}
const manual = (id, name, count) => ({
  ...blank, id, name, origin: 'manual', database: `/tmp/check/${name}.db`, table_count: count, tools: [`db_query__${name}`],
  schema_synced_at: count ? T0 : null,
})
const SOURCES = [
  manual(S1, 'zzcatscenic', 151),
  {
    ...blank, id: S2, name: 'zzcatupload', origin: 'upload', table_count: 2, tools: ['db_query__zzcatupload'], schema_synced_at: T0,
    database: `/tmp/check/uploads/${S2}.db`, import_mode: 'simple', current_recipe: null, open_staging: null,
    current_snapshot: { id: 'snap-check-cat', created_at: T0, file_name: '客流日报.xlsx', raw_state: 'kept' },
  },
  manual(S3, 'zzcatempty', 0),
  manual(S4, 'zzcatblank', 3),
]
const NAMES = SOURCES.map((s) => s.name)

const freshState = () => ({
  sources: SOURCES,
  tables: {
    [S1]: scenicTables(),
    [S2]: uploadTables(),
    [S3]: [],
    [S4]: ['parking_lots', 'parking_records', 'shuttle_trips'].map((n, i) => table(n, 3 - i, [pk(), col('name', 'TEXT')], null)),
  },
})

// ---------------------------------------------------------------------------
// 假服务端：照 backend/app/data/catalog.py 的规则答整份提交、单项审阅、起草（只做界面看得出来的部分）
// ---------------------------------------------------------------------------
const TABLE_FIELDS = ['label', 'description', 'grain', 'keys', 'kind', 'business_date', 'valid_filter', 'dedup']
const COLUMN_FIELDS = ['label', 'meaning', 'unit', 'measure', 'codes']
const clone = (x) => JSON.parse(JSON.stringify(x))

function slots(notes) {
  const out = new Map()
  for (const f of TABLE_FIELDS) if (notes?.[f]) out.set(f, notes[f])
  for (const [c, items] of Object.entries(notes?.columns ?? {})) for (const f of COLUMN_FIELDS) if (items?.[f]) out.set(`columns.${c}.${f}`, items[f])
  for (const r of notes?.relations ?? []) out.set(`relations.${r.id}`, r)
  return out
}
function assemble(map) {
  const notes = {}
  for (const [path, it] of map) {
    if (path.startsWith('columns.')) {
      const rest = path.slice('columns.'.length)
      const cut = rest.lastIndexOf('.')
      const c = rest.slice(0, cut)
      ;((notes.columns ??= {})[c] ??= {})[rest.slice(cut + 1)] = it
    } else if (path.startsWith('relations.')) (notes.relations ??= []).push(it)
    else notes[path] = it
  }
  return notes
}
function countsOf(notes) {
  const c = { proposed: 0, verified: 0, confirmed: 0, rejected: 0 }
  for (const it of slots(notes).values()) c[it.status]++
  return c
}
const valueOf = (path, it) => JSON.stringify(path.startsWith('relations.')
  ? [it.columns, it.to_table, it.to_columns, it.cardinality ?? null, it.coverage ?? null] : it.value)

/** 整份提交（apply_human_edit）：值改过的、新填的记为人工确认；值没动的按提交的状态（只认确认、驳回、初始状态） */
function applyPut(old, submitted) {
  const prev = slots(old)
  const sub = clone(submitted)
  for (const r of sub.relations ?? []) { r.id = relId(r.columns, r.to_table, r.to_columns); r.source ??= 'human'; r.status ??= 'confirmed' }
  const out = new Map()
  for (const [path, it] of slots(sub)) {
    const p = prev.get(path)
    if (!p || valueOf(path, it) !== valueOf(path, p)) {
      out.set(path, { ...it, source: 'human', status: 'confirmed', updated_at: new Date().toISOString() })
    } else {
      const ok = ['confirmed', 'rejected', INIT[p.source] ?? 'proposed'].includes(it.status)
      out.set(path, { ...p, status: ok ? it.status : p.status })
    }
  }
  return assemble(out)
}
function applyReview(notes, path, action) {
  const map = slots(clone(notes))
  const it = map.get(path)
  if (!it) return null
  if (action === 'reset' && it.source === 'human') map.delete(path)
  else it.status = action === 'confirm' ? 'confirmed' : action === 'reject' ? 'rejected' : INIT[it.source] ?? 'proposed'
  return assemble(map)
}

const isUpload = (src) => src === S2
function rowOf(t) {
  const n = t.notes ?? {}
  const live = (it) => (it && it.status !== 'rejected' ? it : null)
  return {
    table_name: t.name, qualified: t.name, is_view: t.is_view, in_schema: !t.missing,
    label: live(n.label)?.value ?? null, label_status: live(n.label)?.status ?? null, kind: live(n.kind)?.value ?? null,
    counts: countsOf(n), relations: (n.relations ?? []).filter((r) => r.status !== 'rejected').length,
    usage: t.usage, version: t.version, updated_at: t.updated_at, updated_by: t.updated_by,
  }
}
function listOf(state, src) {
  const all = state.tables[src]
  const rows = all.filter((t) => !t.missing).sort((a, b) => b.usage - a.usage).map(rowOf)
  rows.push(...all.filter((t) => t.missing).map(rowOf))
  return { tables: rows, system_notes: isUpload(src), schema_note: all.length ? null : '尚未探查结构' }
}
const detailOf = (src, t) => ({
  table_name: t.name, in_schema: !t.missing, notes: t.notes ?? {}, version: t.version, updated_at: t.updated_at, updated_by: t.updated_by,
  structure: t.missing ? null : {
    qualified: t.name, is_view: t.is_view, comment: t.comment, columns: t.columns,
    primary_key: t.columns.filter((c) => c.pk).map((c) => c.name), foreign_keys: [], unique: [],
  },
  system_notes: isUpload(src), usage: t.usage,
})
const conflictDetail = (name) => ({ detail: `表「${name}」的数据目录刚被修改过，请重新载入后再提交` })
const parts = (key) => key.split(' ')[1].split('/')  // ['', 'datasources', id, 'catalog', table?, 'review'?]
const findTable = (state, key) => {
  const [, , src, , name] = parts(key)
  return { src, t: state.tables[src]?.find((x) => x.name === name) }
}
const writeEntry = (t, notes) => {
  t.notes = notes
  t.version += 1
  t.updated_at = new Date().toISOString()
  t.updated_by = ACTOR
}

/** 起草的默认回答：每张表新增 2 项；没有目录的表顺手填上一个推断的中文名，清单重取后看得出来 */
function draftReply(state) {
  return (route, { key, body }) => {
    const src = parts(key)[2]
    const rows = (body.tables ?? []).map((name) => {
      const t = state.tables[src].find((x) => x.name === name)
      if (t && !t.notes) writeEntry(t, { label: item(`${name} 的推断名`, 'name') })
      return { table_name: name, added: 2, updated: 0, removed: 0, version: t?.version ?? 0, error: null, model_error: null }
    })
    const total = { added: rows.length * 2, updated: 0, removed: 0 }
    return json({ tables: rows, total, model_used: !!body.use_model, model: body.use_model ? 'check-model' : null, model_error: null })(route)
  }
}

function catalogHandlers(state, replies = {}) {
  const reply = (k, fallback) => (route, ctx) => {
    const q = replies[k]
    const next = Array.isArray(q) ? q.shift() : q
    return (next ?? fallback)(route, ctx)
  }
  return [
    [/^GET \/datasources$/, (route) => json(state.sources)(route)],
    // 卡片展开时取的表清单（导入表格的源默认展开）
    [/^GET \/datasources\/check-cat-[a-z]+\/schema$/, (route, { key }) =>
      json({ tables: state.tables[parts(key)[2]].filter((t) => !t.missing).map((t) => t.name), summary: '', synced_at: T0 })(route)],
    [/^GET \/datasources\/check-cat-[a-z]+\/catalog$/, (route, { key }) => json(listOf(state, parts(key)[2]))(route)],
    [/^POST \/datasources\/check-cat-[a-z]+\/catalog\/draft$/, reply('draft', draftReply(state))],
    // 影响面：引用这张表的已发布、受管模板（只读）。没配的表答空
    [/^GET \/datasources\/check-cat-[a-z]+\/catalog\/[^/]+\/impact$/, reply('impact', (route, { key }) => {
      const name = parts(key)[4]
      return json({ table: name, templates: state.impact?.[name] ?? [] })(route)
    })],
    [/^GET \/datasources\/check-cat-[a-z]+\/catalog\/[^/]+$/, reply('get', (route, { key }) => {
      const { src, t } = findTable(state, key)
      return t ? json(detailOf(src, t))(route) : json({ detail: `数据源中没有表 ${parts(key)[4]}，可能已被删除或尚未探查结构` }, 404)(route)
    })],
    [/^PUT \/datasources\/check-cat-[a-z]+\/catalog\/[^/]+$/, reply('put', (route, { key, body }) => {
      const { src, t } = findTable(state, key)
      if (body.if_version !== t.version) return json(conflictDetail(t.name), 409)(route)
      writeEntry(t, applyPut(t.notes ?? {}, body.notes))
      return json(detailOf(src, t))(route)
    })],
    [/^POST \/datasources\/check-cat-[a-z]+\/catalog\/[^/]+\/review$/, reply('review', (route, { key, body }) => {
      const { src, t } = findTable(state, key)
      if (body.if_version !== t.version) return json(conflictDetail(t.name), 409)(route)
      const notes = applyReview(t.notes ?? {}, body.path, body.action)
      if (!notes) return json({ detail: '找不到这一项，可能已被修改，请重新载入' }, 422)(route)
      writeEntry(t, notes)
      return json(detailOf(src, t))(route)
    })],
  ]
}

// ---------------------------------------------------------------------------
// 页面上的定位
// ---------------------------------------------------------------------------
const rowNames = (page) => page.locator('[data-catalog-row]').evaluateAll((els) => els.map((e) => e.getAttribute('data-catalog-row')))
const confirmBox = (page) => page.locator('[role="dialog"]').filter({ hasNot: page.locator('[data-draft-dialog]') })
  .filter({ hasNot: page.locator('[data-item-panel]') }).last()
const consequences = async (page) => (await confirmBox(page).locator('ul li').allInnerTexts()).map((t) => t.trim())
const draftBox = (page) => page.locator('[data-draft-dialog]')
const detail = (page) => page.locator('[data-catalog-detail]')
const version = async (page) => Number(await detail(page).getAttribute('data-version'))
const panel = (page) => page.locator('[data-item-panel]')
const openDetail = async (page, src, name) => {
  await goto(page, `/data/catalog/${src}/${encodeURIComponent(name)}`)
  await until(async () => (await page.locator(`[data-catalog-detail="${name}"][data-version]`).count()) > 0)
}
/** 点开一项的状态标识，等弹层定好位置 */
const openMark = async (page, path) => {
  await page.locator(`[data-item-mark="${path}"]`).first().scrollIntoViewIfNeeded()
  await page.locator(`[data-item-mark="${path}"]`).first().click()
  await panel(page).waitFor({ timeout: 3000 })
  await until(async () => (await panel(page).evaluate((el) => el.getBoundingClientRect().top)) > -1000, 2000)
}
const markStatus = (page, path) => page.locator(`[data-item-mark="${path}"]`).first().getAttribute('data-status').catch(() => null)
const rowCounts = (page, name) => page.locator(`[data-catalog-row="${name}"] [data-counts]`).getAttribute('data-counts').catch(() => '')

// 界面不露的机读码：状态、来源、表类型、度量类型、基数的英文值
const CODE_RE = /\b(?:proposed|verified|confirmed|rejected|many_to_one|one_to_many|one_to_one|flow|stock|ratio|identifier|fact|dimension|llm|fk|if_version|notes)\b/
const KNOWN = ['zzcatscenic', 'zzcatupload', 'zzcatempty', 'zzcatblank']
async function cleanCopy(page, where) {
  let t = await page.locator('[data-catalog-page]').innerText()
  for (const n of KNOWN) t = t.replaceAll(n, '')
  // 表名、列名是库里的标识，照原样显示，先去掉再查
  t = t.replace(/[A-Za-z_][A-Za-z0-9_]*_[A-Za-z0-9_]+/g, '')
  const code = t.match(CODE_RE)?.[0]
  check(`${where}：不露机读码`, !code, code ?? '')
}

// ---------------------------------------------------------------------------

try {
  const probe = await open('/data')
  const overlay = await probe.page.locator('vite-error-overlay').count()
  await probe.close()
  if (overlay) {
    console.error('✗ 页面上是 vite 的报错层——有文件正在改，等一会儿再跑')
    process.exit(1)
  }
} catch (e) {
  console.error(`✗ 打不开前端（${WEB}）——先起沙箱前端\n  ${e.message}`)
  process.exit(1)
}

await section('数据目录 · 卡片入口', async () => {
  const state = freshState()
  const { page, sent, errors, natives, close } = await open('/data/databases', { handlers: catalogHandlers(state) })
  const entry = page.locator('[data-source="zzcatscenic"] [data-catalog-open]')
  check('手工登记的库：卡片上有「数据目录」', await entry.count() === 1 && (await entry.getAttribute('aria-label')) === '数据目录')
  await entry.click()
  await page.waitForURL(`**/data/catalog/${S1}`, { timeout: 5000 }).catch(() => {})
  check('……点了进入 /data/catalog/<源>（独立页面）', new URL(page.url()).pathname === `/data/catalog/${S1}`, page.url())
  check('……页面标题写明是哪个源的数据目录', (await page.locator('[data-catalog-page] h1').innerText()).includes('「zzcatscenic」的数据目录'))
  await page.locator('[data-catalog-back]').click()
  await page.waitForURL('**/data/databases', { timeout: 5000 }).catch(() => {})
  check('「返回数据源」回到数据库标签', new URL(page.url()).pathname === '/data/databases', page.url())
  await goto(page, '/data/tables')
  check('导入表格的源：卡片上也有「数据目录」', await page.locator('[data-source="zzcatupload"] [data-catalog-open]').count() === 1)
  await page.locator('[data-source="zzcatupload"] [data-catalog-open]').click()
  await page.waitForURL(`**/data/catalog/${S2}`, { timeout: 5000 }).catch(() => {})
  await page.locator('[data-catalog-back]').click()
  await page.waitForURL('**/data/tables', { timeout: 5000 }).catch(() => {})
  check('……导入表格的源「返回数据源」回到表格标签', new URL(page.url()).pathname === '/data/tables', page.url())
  check('没有发出写请求', writes(sent, /./).length === 0, JSON.stringify(writes(sent, /./).map((s) => s.key)))
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('数据目录 · 表清单的排序与筛选', async () => {
  const state = freshState()
  const { page, errors, natives, close } = await open(`/data/catalog/${S1}`, { handlers: catalogHandlers(state) })
  await page.locator('[data-catalog-row]').first().waitFor()
  let names = await rowNames(page)
  check('列出全部 152 张表', names.length === 152, String(names.length))
  check('默认按使用次数排序：入园记录在最前', names.slice(0, 5).join(',') === 'visits,orders,order_items,parks,ticket_types', names.slice(0, 5).join(','))
  check('表结构里已经没有的表排在最后，并标出来', names.at(-1) === 'legacy_coupons'
        && (await page.locator('[data-catalog-row="legacy_coupons"] [data-row-missing]').innerText()).includes('表结构中已没有这张表'))
  const visits = await page.locator('[data-catalog-row="visits"]').innerText()
  check('每行写中文名、表名、类型、关联数、使用次数', ['入园记录', 'visits', '明细表', '3 个关联', '使用 42 次'].every((s) => visits.includes(s)),
        visits.replace(/\s+/g, ' '))
  const c = countsOf(visitsNotes())
  check('……各状态的项数（推断、已验证、已确认、已驳回）', (await rowCounts(page, 'visits')) === `${c.proposed},${c.verified},${c.confirmed},${c.rejected}`,
        await rowCounts(page, 'visits'))
  check('……读屏念得出各状态的项数', (await page.locator('[data-catalog-row="visits"] .sr-only').allInnerTexts()).join('|').includes(`推断 ${c.proposed} 项`))
  check('没有目录的表写「没有目录」、中文名写「未填写中文名」', (await page.locator('[data-catalog-row="gates"]').innerText()).includes('没有目录')
        && (await page.locator('[data-catalog-row="gates"] [data-row-label]').innerText()) === '未填写中文名')
  await cleanCopy(page, '表清单')
  await shot(page, 'catalog-list')

  // 搜索：表名、中文名都认
  const search = page.locator('[data-catalog-search]')
  await search.fill('订单')
  names = await rowNames(page)
  check('搜索中文名「订单」：订单、订单明细', names.join(',') === 'orders,order_items', names.join(','))
  check('……计数写「2 / 152 张表」', (await page.locator('[data-catalog-count]').innerText()) === '2 / 152 张表')
  await search.fill('ext_log_01')
  check('搜索表名「ext_log_01」：10 张', (await rowNames(page)).length === 10)
  await search.fill('')

  // 按状态筛选
  const counts = await page.locator('[data-catalog-filter] [data-filter]').evaluateAll((els) => els.map((e) => e.innerText.replace(/\s+/g, '')))
  check('筛选按钮带数量：全部 152、有未确认项 5、全部已确认 3、没有目录 144', counts.join('|') === '全部152|有未确认项5|全部已确认3|没有目录144', counts.join('|'))
  await page.locator('[data-filter="pending"]').click()
  names = await rowNames(page)
  check('「有未确认项」：只有还有推断项的表', names.join(',') === 'visits,order_items,ticket_types,store_sales,inventory_snapshots', names.join(','))
  await page.locator('[data-filter="done"]').click()
  names = await rowNames(page)
  check('「全部已确认」：订单、景区、只剩目录的旧表', names.join(',') === 'orders,parks,legacy_coupons', names.join(','))
  await page.locator('[data-filter="none"]').click()
  check('「没有目录」：144 张', (await rowNames(page)).length === 144)
  // 筛选组是单选组：方向键在组内移动并选中
  await page.locator('[data-filter="none"]').focus()
  await page.keyboard.press('ArrowLeft')
  check('筛选组：← 选中前一项「全部已确认」', (await page.locator('[data-catalog-filter]').getAttribute('data-catalog-filter')) === 'done')
  await page.locator('[data-filter="all"]').click()

  // 排序
  await page.locator('[data-catalog-sort]').selectOption('pending')
  names = await rowNames(page)
  const proposedOf = (n) => countsOf(state.tables[S1].find((t) => t.name === n)?.notes).proposed
  check('按未确认项排序：推断项最多的在前', proposedOf(names[0]) >= proposedOf(names[1]) && proposedOf(names[1]) >= proposedOf(names[2])
        && names.at(-1) === 'legacy_coupons', names.slice(0, 3).join(','))
  await page.locator('[data-catalog-sort]').selectOption('name')
  names = await rowNames(page)
  const inSchema = names.slice(0, -1)
  check('按表名排序：字母序，只剩目录的表仍在最后', inSchema.join(',') === [...inSchema].sort((a, b) => a.localeCompare(b, 'en')).join(',')
        && names.at(-1) === 'legacy_coupons', names.slice(0, 3).join(','))
  await page.locator('[data-catalog-sort]').selectOption('usage')

  // 键盘：↑↓ 在行之间移动，Enter 打开
  await page.locator('[data-catalog-row="visits"] [data-catalog-open-row]').focus()
  await page.keyboard.press('ArrowDown')
  check('键盘：↓ 移到下一行', await page.evaluate(() => document.activeElement?.closest('[data-catalog-row]')?.getAttribute('data-catalog-row')) === 'orders')
  await page.keyboard.press('Enter')
  await page.waitForURL(`**/data/catalog/${S1}/orders`, { timeout: 5000 }).catch(() => {})
  check('……Enter 打开这张表', new URL(page.url()).pathname === `/data/catalog/${S1}/orders`, page.url())
  check('……清单上标出正在看的表', await until(async () => (await page.locator('[data-catalog-row="orders"] [data-catalog-open-row]').getAttribute('aria-current')) === 'true'),
        String(await page.locator('[data-catalog-row="orders"] [data-catalog-open-row]').getAttribute('aria-current')))
  const focusVisible = await page.evaluate(() => {
    const el = document.querySelector('[data-catalog-row="orders"] [data-catalog-open-row]')
    el.focus()
    return getComputedStyle(el).boxShadow !== 'none' || getComputedStyle(el).outlineStyle !== 'none'
  })
  check('……行按钮有可见的焦点样式', focusVisible)
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('数据目录 · 用到但没确认（顶部摘要）', async () => {
  const state = freshState()
  // 期望值照假数据现算：使用次数大于 0、还在表结构里的表，按推断项 / 没有目录分
  const used = state.tables[S1].filter((t) => !t.missing && t.usage > 0)
  const progress = (t) => { const c = countsOf(t.notes ?? {}); return c.proposed ? 'pending' : c.verified + c.confirmed + c.rejected ? 'done' : 'none' }
  const pending = used.filter((t) => progress(t) === 'pending').length
  const none = used.filter((t) => progress(t) === 'none').length
  const { page, sent, errors, close } = await open(`/data/catalog/${S1}`, { handlers: catalogHandlers(state) })
  const bar = page.locator('[data-catalog-usage-summary]')
  await bar.waitFor({ timeout: 3000 })
  check('数字照清单算：查询过的表、有推断项的、没有目录的', await bar.getAttribute('data-catalog-usage-summary') === `${used.length},${pending},${none}`,
    `${await bar.getAttribute('data-catalog-usage-summary')} ≠ ${used.length},${pending},${none}`)
  const text = await bar.innerText()
  check('写成一句话', text.includes(`运行中查询过的 ${used.length} 张表里，有 ${pending} 张还有推断项未确认，${none} 张还没有目录`), text)
  await bar.locator('[data-usage-focus="pending"]').click()
  check('点「还有推断项未确认」：切到「有未确认项」筛选',
    (await page.locator('[data-catalog-filter]').getAttribute('data-catalog-filter')) === 'pending')
  check('……排序是按使用次数', await page.locator('[data-catalog-sort]').inputValue() === 'usage')
  const shown = await rowNames(page)
  const expectTop = used.filter((t) => progress(t) === 'pending').sort((a, b) => b.usage - a.usage).map((t) => t.name)
  check('……查询过的、有推断项的表排在最前', shown.slice(0, expectTop.length).join(',') === expectTop.join(','),
    `${shown.slice(0, expectTop.length).join(',')} ≠ ${expectTop.join(',')}`)
  await bar.locator('[data-usage-focus="none"]').click()
  check('点「还没有目录」：切到「没有目录」筛选', (await page.locator('[data-catalog-filter]').getAttribute('data-catalog-filter')) === 'none'
    && (await rowNames(page))[0] === used.filter((t) => progress(t) === 'none').sort((a, b) => b.usage - a.usage)[0].name)
  check('摘要只读、不发请求', writes(sent, /./).length === 0)
  // 都审完了：只写一句，不给按钮
  for (const t of state.tables[S1]) if (t.notes) t.notes = applyPut({}, { label: { value: t.notes.label?.value ?? t.name } })
  for (const t of state.tables[S1]) if (!t.notes && t.usage > 0) t.notes = { label: item(t.name, 'human') }
  await goto(page, `/data/catalog/${S1}`)
  await bar.waitFor({ timeout: 3000 })
  check('都确认了：写「都已确认」，没有按钮', (await bar.innerText()).includes(`运行中查询过的 ${used.length} 张表都已确认`)
    && await bar.locator('[data-usage-focus]').count() === 0, await bar.innerText())
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
  await close()
})

await section('数据目录 · 起草', async () => {
  const state = freshState()
  const MODEL_ERR = '未配置模型接入，无法由助手起草数据目录。请到「设置 → 模型接入」添加'
  const replies = { draft: [] }
  const { page, sent, errors, natives, close } = await open(`/data/catalog/${S1}`, { handlers: catalogHandlers(state, replies) })
  await page.locator('[data-catalog-row]').first().waitFor()

  // 多选：Shift 点选连选一段
  await page.locator('[data-catalog-select="gates"]').click()
  await page.locator('[data-catalog-select="members"]').click({ modifiers: ['Shift'] })
  check('Shift 点选连选一段：闸机到会员共 3 张', (await page.locator('[data-catalog-count]').innerText()) === '已选 3 张')
  await page.locator('[data-catalog-draft-selected]').click()
  await draftBox(page).waitFor()
  check('起草范围默认是「已选的 3 张表」', await draftBox(page).locator('[data-draft-scope="selected"] input').isChecked()
        && (await draftBox(page).locator('[data-draft-scope="selected"]').innerText()).includes('已选的 3 张表'))
  check('……说明只发送表结构、不发送数据', (await draftBox(page).locator('[data-draft-model]').innerText()).includes('不发送数据'))
  await draftBox(page).locator('[data-draft-model] input').check()
  // 模型整体用不了：第一批就知道，结果里写明原因
  replies.draft.push((route, ctx) => {
    const rows = ctx.body.tables.map((n) => ({ table_name: n, added: 4, updated: 1, removed: 0, version: 1, error: null, model_error: null }))
    for (const n of ctx.body.tables) writeEntry(state.tables[S1].find((t) => t.name === n), { label: item(`${n} 的推断名`, 'name') })
    return json({ tables: rows, total: { added: 12, updated: 3, removed: 0 }, model_used: false, model: null, model_error: MODEL_ERR })(route)
  })
  const listsBefore = count(sent, /^GET \/datasources\/check-cat-scenic\/catalog$/)
  await page.locator('[data-draft-start]').click()
  check('起草完成：写出「新增 12 项、更新 3 项」', await until(async () => (await draftBox(page).locator('[data-draft-summary]').innerText().catch(() => '')) === '新增 12 项、更新 3 项'),
        await draftBox(page).locator('[data-draft-summary]').innerText().catch(() => ''))
  const body = lastBody(sent, /catalog\/draft$/)
  check('……请求带所选的表（清单顺序）和「用模型起草」', JSON.stringify(body) === JSON.stringify({ tables: ['gates', 'store_sales', 'members'], use_model: true }), JSON.stringify(body))
  check('……模型用不了时写明原因', (await draftBox(page).locator('[data-draft-model-error]').innerText().catch(() => '')).includes('模型未参与起草：未配置模型接入'))
  check('……进度写「已完成 3 / 3 张表」', (await draftBox(page).locator('[data-draft-progress]').innerText()) === '已完成 3 / 3 张表')
  check('……写请求带本机署名', sent.filter((s) => /catalog\/draft$/.test(s.key)).every((s) => decodeURIComponent(s.actor ?? '') === ACTOR))
  await shot(page, 'catalog-draft-done')
  await page.locator('[data-draft-close]').click()
  check('关掉后重取清单、清空选择，起草出的中文名出现在清单上', await until(async () => count(sent, /^GET \/datasources\/check-cat-scenic\/catalog$/) > listsBefore)
        && (await page.locator('[data-catalog-count]').innerText()).endsWith('张表')
        && await until(async () => (await page.locator('[data-catalog-row="gates"] [data-row-label]').innerText()) === 'gates 的推断名'))

  // 全部 151 张（只剩目录的那张起草不了，不算进范围），不请模型时每批 40 张
  await page.locator('[data-catalog-draft]').first().click()
  await draftBox(page).waitFor()
  const scopes = await draftBox(page).locator('[data-draft-scope]').evaluateAll((els) => els.map((e) => e.getAttribute('data-draft-scope')))
  check('没选表时：范围是「使用次数最多的 20 张」「全部 151 张」，默认前者', scopes.join(',') === 'top,all'
        && await draftBox(page).locator('[data-draft-scope="top"] input').isChecked()
        && (await draftBox(page).locator('[data-draft-scope="all"]').innerText()).includes('全部 151 张表'), scopes.join(','))
  await draftBox(page).locator('[data-draft-scope="all"]').click()
  const n0 = count(sent, /catalog\/draft$/)
  await page.locator('[data-draft-start]').click()
  await until(async () => (await draftBox(page).getAttribute('data-draft-dialog')) === 'done', 8000)
  const batches = sent.filter((s) => /catalog\/draft$/.test(s.key)).slice(n0).map((s) => s.body)
  check('表多时分批调用：40、40、40、31 张，都不请模型', batches.map((b) => b.tables.length).join(',') === '40,40,40,31'
        && batches.every((b) => b.use_model === false) && !batches.flatMap((b) => b.tables).includes('legacy_coupons'),
        batches.map((b) => b.tables.length).join(','))
  check('……进度写满、合计各批的结果', (await draftBox(page).locator('[data-draft-progress]').innerText()) === '已完成 151 / 151 张表'
        && (await draftBox(page).locator('[data-draft-summary]').innerText()) === '新增 302 项、更新 0 项')
  check('……进度条读屏可读（progressbar 带当前值）', (await draftBox(page).locator('[role="progressbar"]').getAttribute('aria-valuenow')) === '151')
  await page.locator('[data-draft-close]').click()

  // 请模型时每批 12 张
  await page.locator('[data-catalog-draft]').first().click()
  await draftBox(page).locator('[data-draft-model] input').check()
  const n1 = count(sent, /catalog\/draft$/)
  await page.locator('[data-draft-start]').click()
  await until(async () => (await draftBox(page).getAttribute('data-draft-dialog')) === 'done', 8000)
  const modelBatches = sent.filter((s) => /catalog\/draft$/.test(s.key)).slice(n1).map((s) => s.body)
  check('用模型起草前 20 张：每批 12 张（12、8），都请模型', modelBatches.map((b) => `${b.tables.length}:${b.use_model}`).join(',') === '12:true,8:true',
        modelBatches.map((b) => `${b.tables.length}:${b.use_model}`).join(','))
  check('……写出用的是哪个模型', (await draftBox(page).innerText()).includes('模型：check-model'))
  await page.locator('[data-draft-close]').click()

  // 中途停止：当前这一批做完后不再发下一批
  replies.draft.push(async (route, ctx) => { await sleep(900); return draftReply(state)(route, ctx) })
  await page.locator('[data-catalog-draft]').first().click()
  await draftBox(page).locator('[data-draft-scope="all"]').click()
  const n2 = count(sent, /catalog\/draft$/)
  await page.locator('[data-draft-start]').click()
  await page.locator('[data-draft-stop]').click()
  check('点「停止」后写「当前这一批完成后停止…」', (await draftBox(page).locator('[data-draft-title]').innerText()) === '当前这一批完成后停止…')
  await until(async () => (await draftBox(page).getAttribute('data-draft-dialog')) === 'done', 5000)
  check('……这一批做完就停：只发了 1 批，写明完成了多少', count(sent, /catalog\/draft$/) - n2 === 1
        && (await draftBox(page).locator('[data-draft-title]').innerText()) === '已停止：完成 40 / 151 张表，其余未起草')
  await page.locator('[data-draft-close]').click()

  // 第二批出错：停下来，写服务端原话；个别表没起草、模型只在个别表上失败的分开列
  replies.draft.push((route, ctx) => {
    const rows = ctx.body.tables.map((n, i) => ({ table_name: n, added: 1, updated: 0, removed: 0, version: 1,
      error: i === 0 ? '这张表的数据目录正被其他人修改，请稍后再起草' : null, model_error: i === 1 ? '模型返回的内容无法解析' : null }))
    return json({ tables: rows, total: { added: 39, updated: 0, removed: 0 }, model_used: true, model: 'check-model', model_error: null })(route)
  })
  replies.draft.push(json({ detail: '服务端处理起草时出错，请稍后重试' }, 500))
  await page.locator('[data-catalog-draft]').first().click()
  await draftBox(page).locator('[data-draft-scope="all"]').click()
  await page.locator('[data-draft-start]').click()
  await until(async () => (await draftBox(page).getAttribute('data-draft-dialog')) === 'done', 5000)
  const text = await draftBox(page).innerText()
  check('第二批出错：写「起草中断」和服务端原话，进度停在 40', text.includes('起草中断') && text.includes('服务端处理起草时出错')
        && (await draftBox(page).locator('[data-draft-progress]').innerText()) === '已完成 40 / 151 张表', text.replace(/\s+/g, ' ').slice(0, 160))
  check('……没起草的表单独列出（1 张）', (await draftBox(page).locator('[data-draft-table-errors]').innerText()).includes('1 张表未能起草'))
  check('……模型只在个别表上失败的另列（1 张），说明已按其余来源起草', (await draftBox(page).locator('[data-draft-table-model-errors]').innerText()).includes('1 张表的模型起草失败'))
  await shot(page, 'catalog-draft-failed')
  check('只发过起草请求，没有别的写请求', writes(sent, /./).every((s) => /catalog\/draft$/.test(s.key)), JSON.stringify(writes(sent, /./).filter((s) => !/catalog\/draft$/.test(s.key)).map((s) => s.key)))
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('数据目录 · 表详情与单项审阅', async () => {
  const state = freshState()
  const { page, sent, errors, natives, close } = await open(`/data/catalog/${S1}`, { handlers: catalogHandlers(state) })
  await openDetail(page, S1, 'visits')
  const head = await detail(page).locator('header').innerText()
  check('表头：中文名、表类型、表名、使用次数、谁改过', ['入园记录', '明细表', 'visits', '使用 42 次', '审核甲'].every((s) => head.includes(s)), head.replace(/\s+/g, ' '))
  const grain = page.locator('[data-field="grain"]')
  check('表级项：粒度的值和「推断 · 模型起草」', (await grain.innerText()).includes('一张门票的一次入园') && (await grain.innerText()).includes('推断')
        && (await grain.innerText()).includes('模型起草'))
  check('……有效记录条件是人工填写、已确认', (await page.locator('[data-field="valid_filter"]').innerText()).includes('status <> 9')
        && await markStatus(page, 'valid_filter') === 'confirmed')
  check('……被驳回的去重规则划掉', await markStatus(page, 'dedup') === 'rejected'
        && await page.locator('[data-field="dedup"] .line-through').count() === 1)
  const bd = await page.locator('[data-field="business_date"]').innerText()
  check('……业务日期写列、规则、时区', ['visit_time', '按入园时间的日期', 'Asia/Shanghai'].every((x) => bd.includes(x)), bd.replace(/\s+/g, ' '))
  check('列表格：9 列，写列名、类型、主键', await page.locator('[data-column]').count() === 9
        && (await page.locator('[data-column="id"]').innerText()).includes('主键') && (await page.locator('[data-column="ticket_no"]').innerText()).includes('TEXT'))
  check('……码值写成「码值 含义」', (await page.locator('[data-column="status"] [data-cell="codes"]').innerText()).replace(/\s+/g, ' ').includes('1 已入园'))
  check('……度量类型写中文（可累加），外键列是「已验证」', (await page.locator('[data-column="visitor_count"] [data-cell="measure"]').innerText()).includes('可累加')
        && await markStatus(page, 'columns.park_id.measure') === 'verified')
  check('关联关系：3 条，写目标表、字段、基数、来源', await page.locator('[data-relation]').count() === 3
        && (await page.locator(`[data-relation="${relId(['gate_id'], 'gates', ['id'])}"]`).innerText()).replace(/\s+/g, ' ').includes('gates id 多对一'))
  check('四种状态用同一套标识，带图例', await page.locator('[data-status-legend] li').count() === 4)
  await cleanCopy(page, '表详情')
  await shot(page, 'catalog-detail')

  // 弹层：键盘打开、Esc 收起、焦点回到触发按钮
  await openMark(page, 'grain')
  const pt = await panel(page).innerText()
  check('点状态标识：弹层写位置、状态说明、来源', pt.includes('表的粒度的来源和状态') && pt.includes('只作提示，确认后才参与 SQL 检查') && pt.includes('来源：模型起草'), pt.replace(/\s+/g, ' '))
  check('……焦点落在「确认」上', await page.evaluate(() => document.activeElement?.getAttribute('data-review-action')) === 'confirm')
  check('……推断项已是初始状态，「恢复」不可点', await panel(page).locator('[data-review-action="reset"]').isDisabled())
  await page.keyboard.press('Escape')
  check('……Esc 收起，焦点回到状态标识', await panel(page).count() === 0
        && await page.evaluate(() => document.activeElement?.getAttribute('data-item-mark')) === 'grain')

  // 确认
  let v = await version(page)
  await openMark(page, 'grain')
  await panel(page).locator('[data-review-action="confirm"]').click()
  await until(async () => await markStatus(page, 'grain') === 'confirmed')
  let body = lastBody(sent, /\/review$/)
  check('确认：POST …/review，带路径、操作和读到的版本', JSON.stringify(body) === JSON.stringify({ path: 'grain', action: 'confirm', if_version: v }), JSON.stringify(body))
  check('……标识变成「已确认」，读屏播报', await markStatus(page, 'grain') === 'confirmed'
        && (await page.locator('[data-catalog-live]').innerText()) === '已确认表的粒度')
  const c = countsOf(state.tables[S1][0].notes)
  check('……清单上这一行的计数跟着变', (await rowCounts(page, 'visits')) === `${c.proposed},${c.verified},${c.confirmed},${c.rejected}`, await rowCounts(page, 'visits'))

  // 驳回列级项
  v = await version(page)
  await openMark(page, 'columns.ticket_no.label')
  await panel(page).locator('[data-review-action="reject"]').click()
  await until(async () => await markStatus(page, 'columns.ticket_no.label') === 'rejected')
  body = lastBody(sent, /\/review$/)
  check('驳回列级项：路径写 columns.<列名>.<字段>', JSON.stringify(body) === JSON.stringify({ path: 'columns.ticket_no.label', action: 'reject', if_version: v }), JSON.stringify(body))
  check('……值划掉', await page.locator('[data-column="ticket_no"] [data-cell="label"] .line-through').count() === 1)

  // 恢复：推断项回到初始状态
  await openMark(page, 'columns.ticket_no.label')
  check('被驳回的推断项「恢复」可点', !(await panel(page).locator('[data-review-action="reset"]').isDisabled())
        && (await panel(page).locator('[data-review-action="reset"]').innerText()).includes('恢复'))
  await panel(page).locator('[data-review-action="reset"]').click()
  await until(async () => await markStatus(page, 'columns.ticket_no.label') === 'proposed')
  check('……恢复后回到「推断」', (lastBody(sent, /\/review$/))?.action === 'reset' && await markStatus(page, 'columns.ticket_no.label') === 'proposed')

  // 人工填写的项：恢复即删除
  await openMark(page, 'valid_filter')
  check('人工填写的项：按钮写「删除」，说明恢复即删除', (await panel(page).locator('[data-review-action="reset"]').innerText()).includes('删除')
        && (await panel(page).innerText()).includes('恢复即删除'))
  await panel(page).locator('[data-review-action="reset"]').click()
  await until(async () => (await page.locator('[data-item-mark="valid_filter"]').count()) === 0)
  check('……删除后这一项显示「—」', (await page.locator('[data-field="valid_filter"]').innerText()).includes('—'))

  // 关系的审阅路径
  const gid = relId(['gate_id'], 'gates', ['id'])
  await openMark(page, `relations.${gid}`)
  await panel(page).locator('[data-review-action="confirm"]').click()
  await until(async () => await markStatus(page, `relations.${gid}`) === 'confirmed')
  check('确认关联关系：路径写 relations.<编号>', lastBody(sent, /\/review$/)?.path === `relations.${gid}`)

  // 目标表能点就跳过去
  await page.locator('[data-relation-target="parks"]').click()
  await page.waitForURL(`**/data/catalog/${S1}/parks`, { timeout: 5000 }).catch(() => {})
  check('点关联关系的目标表：打开那张表', new URL(page.url()).pathname === `/data/catalog/${S1}/parks`
        && await until(async () => (await page.locator('[data-catalog-detail="parks"][data-version]').count()) === 1))
  // 上一张 / 下一张按清单顺序
  await page.locator('[data-catalog-next]').click()
  await page.waitForURL(`**/data/catalog/${S1}/ticket_types`, { timeout: 5000 }).catch(() => {})
  check('「下一张」按清单顺序：景区之后是票种', new URL(page.url()).pathname === `/data/catalog/${S1}/ticket_types`, page.url())
  check('只发了审阅请求，没有别的写请求', writes(sent, /./).every((s) => /\/review$/.test(s.key)), JSON.stringify(writes(sent, /./).map((s) => s.key)))
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('数据目录 · 引用这张表的模板（影响面）', async () => {
  const state = freshState()
  state.impact = {
    visits: [
      { workflow_id: 'wf-daily', name: '入园日报', version: 4, level: 'governed', impact: 'direct', nodes: [
        { node_id: 'q', label: '查询入园人数', type: 'tool', impact: 'direct' },
        { node_id: 'm', label: '合并入园和订单', type: 'merge', impact: 'direct', via: [{ node_id: 'q', label: '查询入园人数', alias: 'v' }] },
      ] },
      { workflow_id: 'wf-ask', name: '客流分析', version: 2, level: 'published', impact: 'possible', nodes: [
        { node_id: 'ask', label: '分析客流', type: 'agent', impact: 'possible' },
      ] },
    ],
  }
  const { page, sent, errors, close } = await open(`/data/catalog/${S1}`, { handlers: catalogHandlers(state) })
  await openDetail(page, S1, 'visits')
  const sec = page.locator('[data-catalog-section="impact"]')
  await sec.locator('[data-catalog-impact="2"]').waitFor({ timeout: 3000 })
  const text = await sec.innerText()
  check('表详情有「引用这张表的模板」一栏，写明按已发布版本统计', text.includes('引用这张表的模板') && text.includes('按每个模板当前的已发布版本统计'))
  const daily = sec.locator('[data-impact-template="wf-daily"]')
  check('直接引用：模板名、版本、受管、「直接引用」', ['入园日报', 'v4', '受管', '直接引用'].every((x) => text.includes(x))
    && await daily.getAttribute('data-impact-level') === 'direct')
  check('合并查询写明经由哪个输入', (await daily.locator('[data-impact-node="m"]').innerText()).includes('经由输入「查询入园人数」'))
  check('Agent 写「可能涉及」，悬停说明原因', (await sec.locator('[data-impact-template="wf-ask"] [data-impact="possible"]').getAttribute('title')).includes('运行时生成'))
  check('模板名点进去是画布，定位到那个节点', (await daily.locator('a').first().getAttribute('href')) === '/studio/wf-daily?focus=q')
  // 开发模式下 StrictMode 会把挂载时的取数做两遍，只查有没有取、没有写
  check('统计影响面只读', writes(sent, /impact/).length === 0 && count(sent, /\/visits\/impact$/) >= 1,
    `${count(sent, /\/visits\/impact$/)} 次`)
  // 没有模板引用的表：直说没有
  await openDetail(page, S1, 'orders')
  check('没有模板引用：直说没有', (await page.locator('[data-catalog-section="impact"] [data-catalog-impact="0"]').innerText())
    .includes('没有已发布或受管的模板引用这张表'))
  // 表详情保存之后（版本变了）重新统计
  const before = count(sent, /\/orders\/impact$/)
  await page.locator('[data-catalog-edit]').click()
  await page.locator('[data-input="grain"]').fill('一笔订单一行（检查）')
  await page.locator('[data-catalog-save]').click()
  await until(async () => count(sent, /\/orders\/impact$/) > before, 3000)
  check('保存之后重新统计影响面', count(sent, /\/orders\/impact$/) > before, `${before} → ${count(sent, /\/orders\/impact$/)}`)
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
  await close()
})

await section('数据目录 · 批量确认', async () => {
  const state = freshState()
  const { page, sent, errors, natives, close } = await open(`/data/catalog/${S1}`, { handlers: catalogHandlers(state) })
  await openDetail(page, S1, 'order_items')
  const old = clone(state.tables[S1][2].notes)
  const n = countsOf(old).proposed
  const btn = page.locator('[data-catalog-confirm-all]')
  check(`「确认本表全部推断（${n}）」`, (await btn.innerText()).includes(`确认本表全部推断（${n}）`))
  const v = await version(page)
  await btn.click()
  await confirmBox(page).waitFor()
  const cons = await consequences(page)
  check('……先确认：标题写表和项数，后果写参与 SQL 检查、来源不变', (await confirmBox(page).innerText()).includes(`确认「订单明细」的 ${n} 项推断？`)
        && cons.includes('确认后这些项参与 SQL 检查') && cons.includes('来源保持不变，状态改为已确认'), cons.join(' | '))
  await confirmBox(page).getByRole('button', { name: `确认 ${n} 项` }).click()
  await until(async () => count(sent, /^PUT /) === 1)
  const body = lastBody(sent, /^PUT /)
  const before = slots(old)
  const after = slots(body?.notes)
  const okItems = [...before].every(([p, it]) => {
    const x = after.get(p)
    return x && x.source === it.source && x.status === (it.status === 'proposed' ? 'confirmed' : it.status) && valueOf(p, x) === valueOf(p, it)
  })
  check('……整份提交：推断项改成已确认，来源和值不变，其余项原样', body?.if_version === v && okItems && after.size === before.size, JSON.stringify(body).slice(0, 200))
  check('……提交后按钮消失、表头计数没有推断', await until(async () => (await btn.count()) === 0)
        && (await detail(page).locator('header [data-counts]').getAttribute('data-counts')).startsWith('0,'))
  await until(async () => (await rowCounts(page, 'order_items')).startsWith('0,'))
  check('……清单上这张表移到「全部已确认」', (await page.locator('[data-catalog-row="order_items"]').getAttribute('data-progress')) === 'done')

  // 确认本列推断：只动这一列
  await openDetail(page, S1, 'store_sales')
  check('一张表 50 列：全部列出', await page.locator('[data-column]').count() === 50)
  await page.locator('[data-column-filter]').fill('promo')
  check('……按列名筛选：43 列', await page.locator('[data-column]').count() === 43 && (await page.locator('[data-column-count]').innerText()) === '43 / 50 列')
  await page.locator('[data-column-filter]').fill('')
  await page.locator('[data-only-pending]').check()
  check('……只看有推断项的列：8 列', await page.locator('[data-column]').count() === 8)
  await page.locator('[data-only-pending]').uncheck()
  const wideOld = clone(state.tables[S1].find((t) => t.name === 'store_sales').notes)
  await page.locator('[data-confirm-column="amount"]').click()
  await until(async () => count(sent, /^PUT /) === 2)
  const cb = lastBody(sent, /^PUT /)
  check('「确认本列推断」：只有这一列的推断项改成已确认', cb?.notes?.columns?.amount?.unit?.status === 'confirmed'
        && cb.notes.columns.amount.measure.status === 'confirmed' && cb.notes.columns.qty.label.status === 'proposed'
        && cb.notes.label.status === 'proposed' && JSON.stringify(cb.notes.columns.amount.unit.value) === JSON.stringify(wideOld.columns.amount.unit.value))
  check('……这一行的按钮消失', await until(async () => (await page.locator('[data-confirm-column="amount"]').count()) === 0))
  check('只发了整份提交，没有别的写请求', writes(sent, /./).every((s) => /^PUT /.test(s.key)), JSON.stringify(writes(sent, /./).map((s) => s.key)))
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('数据目录 · 编辑即确认', async () => {
  const state = freshState()
  const replies = {}
  const { page, sent, errors, natives, close } = await open(`/data/catalog/${S1}`, { handlers: catalogHandlers(state, replies) })
  await openDetail(page, S1, 'visits')
  const v = await version(page)
  await page.locator('[data-catalog-edit]').click()
  const bar = page.locator('[data-catalog-edit-bar]')
  check('进入编辑：底部有保存栏，未修改时「保存」不可点', (await detail(page).getAttribute('data-editing')) === 'true'
        && (await bar.innerText()).includes('尚未修改') && await page.locator('[data-catalog-save]').isDisabled())
  check('……写明保存后改动过的项记为人工确认、清空即删除', (await detail(page).innerText()).includes('清空一项即删除'))
  check('……被驳回的项输入框留空，原值写在占位里', (await page.locator('[data-field="dedup"] input').inputValue()) === ''
        && (await page.locator('[data-field="dedup"] input').getAttribute('placeholder')) === '已驳回：按 ticket_no 去重')
  await page.locator('[data-field="label"] input').fill('入园流水')
  await page.locator('[data-field="grain"] input').fill('')
  check('改两处：写「2 处修改」，改过的项标「已修改」', (await page.locator('[data-catalog-changes]').innerText()) === '2 处修改'
        && await page.locator('[data-field="label"] [data-changed]').count() === 1)
  await page.locator('[data-field="keys"] input').fill('ticket_no、no_such_col')
  check('业务主键写了表结构里没有的列：提醒，不拦', (await page.locator('[data-keys-unknown]').innerText()).includes('no_such_col')
        && !(await page.locator('[data-catalog-save]').isDisabled()))
  await page.locator('[data-field="keys"] input').fill('ticket_no、park_id')
  const codes = page.locator('[data-column="status"] [data-input="codes"]')
  await codes.fill('1=已入园\n9=已作废\n坏行')
  check('码值格式不对：就地写第几行、底部写几处格式不正确、不能保存', (await page.locator('[data-column="status"] [role="alert"]').innerText()) === '第 3 行应写成「码值=含义」'
        && (await page.locator('[data-catalog-problems]').innerText()).includes('1 处格式不正确') && await page.locator('[data-catalog-save]').isDisabled())
  await codes.fill('1=已入园\n2=已退票\n9=已作废')
  await page.locator('[data-column="visitor_count"] [data-input="unit"]').fill('人次')
  await page.locator('[data-add-relation]').click()
  const draft = page.locator('[data-relation-draft^="new-"]')
  check('新加的关联关系没填完：写明要填哪些、不能保存', (await draft.innerText()).includes('本表字段、目标表和目标字段都要填写') && await page.locator('[data-catalog-save]').isDisabled())
  await draft.locator('[data-input="columns"]').fill('member_id')
  await draft.locator('[data-input="to_table"]').fill('members')
  await draft.locator('[data-input="to_columns"]').fill('id, member_no')
  check('……两端字段数不一致：写明', (await draft.innerText()).includes('两端的字段数不一致'))
  await draft.locator('[data-input="to_columns"]').fill('id')
  await draft.locator('[data-input="cardinality"]').selectOption('many_to_one')
  await shot(page, 'catalog-edit')
  check('保存栏写「6 处修改」', (await page.locator('[data-catalog-changes]').innerText()) === '6 处修改', await page.locator('[data-catalog-changes]').innerText())
  await page.locator('[data-catalog-save]').click()
  await until(async () => count(sent, /^PUT /) === 1)
  const body = lastBody(sent, /^PUT /)
  const n = body?.notes ?? {}
  const old = visitsNotes()
  check('PUT 带读到的版本', body?.if_version === v, String(body?.if_version))
  check('……改过的项交新值：中文名、业务主键、码值、单位', n.label?.value === '入园流水' && JSON.stringify(n.keys?.value) === '["ticket_no","park_id"]'
        && JSON.stringify(n.columns?.status?.codes?.value) === JSON.stringify({ 1: '已入园', 2: '已退票', 9: '已作废' })
        && n.columns?.visitor_count?.unit?.value === '人次', JSON.stringify({ label: n.label, keys: n.keys }))
  check('……清空的粒度不交（服务端删掉这一项）', !('grain' in n))
  check('……没动的项原样交回：来源、状态都不变（被驳回的照旧驳回）', JSON.stringify(n.dedup) === JSON.stringify(old.dedup)
        && JSON.stringify(n.description) === JSON.stringify(old.description) && JSON.stringify(n.columns?.park_id) === JSON.stringify(old.columns.park_id))
  check('……原有的关系原样交回，新的一条带两端和基数', old.relations.every((r) => n.relations?.some((x) => JSON.stringify(x) === JSON.stringify(r)))
        && n.relations?.some((r) => r.to_table === 'members' && r.columns.join() === 'member_id' && r.to_columns.join() === 'id' && r.cardinality === 'many_to_one'))
  check('保存后退出编辑，改过的项显示「已确认 · 人工填写」', await until(async () => (await detail(page).getAttribute('data-editing')) === 'false')
        && await markStatus(page, 'label') === 'confirmed' && (await page.locator('[data-field="label"]').innerText()).includes('人工填写'))
  check('……清单上的中文名跟着变', await until(async () => (await page.locator('[data-catalog-row="visits"] [data-row-label]').innerText()) === '入园流水'))
  check('……提示已保存', await until(async () => (await page.locator('body').innerText()).includes('已保存，改动过的项已记为人工确认')))

  // 服务端说格式不对（422）：显示原话，留在编辑里
  await page.locator('[data-catalog-edit]').click()
  await page.locator('[data-field="dedup"] input').fill('按票号保留最后一条')
  replies.put = [json({ detail: '数据目录的格式不正确：表的去重规则应为不超过 500 字的文字' }, 422)]
  await page.locator('[data-catalog-save]').click()
  check('服务端回 422：显示原话，留在编辑里', await until(async () => (await page.locator('[data-catalog-write-error]').innerText().catch(() => '')).includes('表的去重规则应为不超过 500 字的文字'))
        && (await detail(page).getAttribute('data-editing')) === 'true')
  check('没有被拦下的写请求', !sent.some((s) => s.blocked), JSON.stringify(sent.filter((s) => s.blocked).map((s) => s.key)))
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('数据目录 · 别人刚改过（409）', async () => {
  const state = freshState()
  const { page, sent, errors, natives, close } = await open(`/data/catalog/${S1}`, { handlers: catalogHandlers(state) })
  await openDetail(page, S1, 'ticket_types')
  // 页面载入之后，别人确认了票种的中文名
  const tt = state.tables[S1].find((t) => t.name === 'ticket_types')
  writeEntry(tt, applyReview(tt.notes, 'label', 'confirm'))
  const stale = await version(page)
  await openMark(page, 'kind')
  await panel(page).locator('[data-review-action="confirm"]').click()
  const banner = page.locator('[data-catalog-conflict]')
  await banner.waitFor({ timeout: 4000 }).catch(() => {})
  check('审阅撞上别人的修改：横幅写「刚被其他人修改过」、操作未生效', (await banner.getAttribute('data-catalog-conflict').catch(() => '')) === 'review'
        && (await banner.innerText()).includes('这张表的数据目录刚被其他人修改过') && (await banner.innerText()).includes('刚才的操作未生效'))
  check('……没有自动重试、没有改动页面上的状态', count(sent, /\/review$/) === 1 && await markStatus(page, 'kind') === 'proposed'
        && lastBody(sent, /\/review$/)?.if_version === stale)
  await shot(page, 'catalog-conflict')
  const gets = count(sent, /^GET \/datasources\/check-cat-scenic\/catalog\/ticket_types$/)
  await banner.locator('[data-catalog-reload]').click()
  check('「重新载入」：重取这张表，横幅消失，显示别人确认过的中文名', await until(async () => (await banner.count()) === 0)
        && count(sent, /^GET \/datasources\/check-cat-scenic\/catalog\/ticket_types$/) === gets + 1
        && await version(page) === tt.version && await markStatus(page, 'label') === 'confirmed')
  check('……重新载入后清单上的计数也是最新的', (await rowCounts(page, 'ticket_types')) === Object.values(countsOf(tt.notes)).join(','))

  // 编辑中撞上：修改留着，重新载入前先问
  await openDetail(page, S1, 'visits')
  await page.locator('[data-catalog-edit]').click()
  await page.locator('[data-field="label"] input').fill('入园明细')
  const vt = state.tables[S1][0]
  writeEntry(vt, applyReview(vt.notes, 'grain', 'confirm'))
  await page.locator('[data-catalog-save]').click()
  await banner.waitFor({ timeout: 4000 }).catch(() => {})
  check('保存撞上别人的修改：横幅写明修改尚未保存，留在编辑里，填的内容还在', (await banner.getAttribute('data-catalog-conflict').catch(() => '')) === 'edit'
        && (await banner.innerText()).includes('你的修改尚未保存') && (await detail(page).getAttribute('data-editing')) === 'true'
        && (await page.locator('[data-field="label"] input').inputValue()) === '入园明细')
  check('……只发了一次 PUT，没有拿新版本号悄悄重发', count(sent, /^PUT /) === 1)
  await banner.locator('[data-catalog-reload]').click()
  await confirmBox(page).waitFor()
  check('……重新载入前先问：放弃未保存的修改', (await confirmBox(page).innerText()).includes('放弃未保存的修改并重新载入？'))
  await confirmBox(page).getByRole('button', { name: '取消' }).click()
  check('……取消则什么都不动', (await detail(page).getAttribute('data-editing')) === 'true' && (await page.locator('[data-field="label"] input').inputValue()) === '入园明细')
  await banner.locator('[data-catalog-reload]').click()
  await confirmBox(page).getByRole('button', { name: '放弃修改并重新载入' }).click()
  check('……确认后退出编辑，显示最新内容', await until(async () => (await detail(page).getAttribute('data-editing')) === 'false')
        && await version(page) === vt.version && await markStatus(page, 'grain') === 'confirmed' && (await banner.count()) === 0)
  check('只发了审阅和整份提交，没有被拦下的写请求', !sent.some((s) => s.blocked))
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('数据目录 · 编辑中离开', async () => {
  const state = freshState()
  const { page, sent, errors, natives, close } = await open(`/data/catalog/${S1}`, { handlers: catalogHandlers(state) })
  await openDetail(page, S1, 'orders')
  await page.locator('[data-catalog-edit]').click()
  await page.locator('[data-field="grain"] input').fill('一个订单一行')
  await page.locator('[data-catalog-next]').click()
  await confirmBox(page).waitFor()
  check('有未保存的修改时换表：先问', (await confirmBox(page).innerText()).includes('数据目录有未保存的修改'))
  await confirmBox(page).getByRole('button', { name: '取消' }).click()
  check('……取消则留在这张表，修改还在', new URL(page.url()).pathname === `/data/catalog/${S1}/orders`
        && (await page.locator('[data-field="grain"] input').inputValue()) === '一个订单一行')
  await page.locator('[data-catalog-next]').click()
  await confirmBox(page).getByRole('button', { name: '放弃修改并离开' }).click()
  await page.waitForURL(`**/data/catalog/${S1}/order_items`, { timeout: 5000 }).catch(() => {})
  check('……确认后换到下一张，不在编辑', new URL(page.url()).pathname === `/data/catalog/${S1}/order_items`
        && await until(async () => (await page.locator('[data-catalog-detail="order_items"]').getAttribute('data-editing')) === 'false'))
  check('没有写请求', writes(sent, /./).length === 0)
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('数据目录 · 空状态与导入表格', async () => {
  const state = freshState()
  const { page, sent, errors, natives, close } = await open(`/data/catalog/${S3}`, { handlers: catalogHandlers(state) })
  const empty = page.locator('[data-catalog-no-schema]')
  await empty.waitFor()
  const et = await empty.innerText()
  check('没有表结构：写「还没有表结构」、原因，引导去点「探查结构」', et.includes('还没有表结构') && et.includes('尚未探查结构') && et.includes('点击「探查结构」'), et.replace(/\s+/g, ' '))
  check('……不给「起草」（没有表结构起草不了）', await page.locator('[data-catalog-draft]').count() === 0)
  await shot(page, 'catalog-no-schema')
  await page.locator('[data-catalog-go-introspect]').click()
  await page.waitForURL('**/data/databases', { timeout: 5000 }).catch(() => {})
  check('……按钮回到数据源卡片', new URL(page.url()).pathname === '/data/databases', page.url())

  await goto(page, `/data/catalog/${S4}`)
  const ov = page.locator('[data-catalog-overview]')
  await ov.waitFor()
  check('有表结构、还没有目录：写「还没有数据目录。先起草，再逐表确认」', (await ov.innerText()).includes('还没有数据目录') && (await ov.innerText()).includes('先起草，再逐表确认'))
  check('……给出「起草」', await ov.locator('[data-catalog-draft]').count() === 1)
  await shot(page, 'catalog-empty')

  await goto(page, `/data/catalog/${S1}`)
  const ovt = await page.locator('[data-catalog-overview]').innerText()
  check('有目录、没选表时：写审阅进度，给出使用最多、还有推断项的表', ovt.includes('共 152 张表：5 张有未确认项，3 张全部已确认，144 张没有目录')
        && (await page.locator('[data-catalog-review-next]').getAttribute('data-catalog-review-next')) === 'visits', ovt.replace(/\s+/g, ' ').slice(0, 120))

  // 导入表格的源：说明由系统生成，只读
  await goto(page, `/data/catalog/${S2}`)
  const sys = page.locator('[data-catalog-overview] [data-catalog-system-notes]')
  check('导入表格的源：概览写明说明是导入时生成的、只读', (await sys.innerText()).includes('导入时生成的说明') && (await sys.innerText()).includes('只读'))
  await openDetail(page, S2, '日客流')
  const tn = page.locator('[data-system-table-note]')
  check('……表说明只读显示，标「导入时生成」', (await tn.innerText()).includes('每日入园人数汇总，由导入时的核对结果生成') && (await tn.innerText()).includes('导入时生成'))
  const cn = page.locator('[data-column="入园人数"] [data-system-note]')
  check('……列的说明写在列名下面，标「导入时生成」', (await cn.innerText()).includes('当日入园人数合计，已与表内合计核对') && (await cn.innerText()).includes('导入时生成'))
  await page.locator('[data-catalog-edit]').click()
  const values = await page.locator('[data-catalog-detail] input, [data-catalog-detail] textarea').evaluateAll((els) => els.map((e) => e.value))
  check('……编辑时说明仍是只读文字，没有任何输入框装着它', (await tn.count()) === 1 && (await cn.count()) === 1
        && !values.some((x) => x.includes('核对结果生成') || x.includes('已与表内合计核对')))
  await shot(page, 'catalog-system-notes')
  await page.locator('[data-catalog-cancel]').click()
  await cleanCopy(page, '导入表格的表详情')
  check('没有写请求', writes(sent, /./).length === 0)
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('数据目录 · 窄屏', async () => {
  const state = freshState()
  const { page, errors, close } = await open(`/data/catalog/${S1}`, { handlers: catalogHandlers(state), viewport: { width: 390, height: 844 } })
  // 横向溢出只量数据目录页自己（列表格、关联关系表在自己的框里横向滚动，不算）。不量整页：390 宽时应用外壳里收起的
  // 导航提示本来就超出视口，与本页无关
  const overflow = () => page.evaluate(() => {
    const root = document.querySelector('[data-catalog-page]')
    return root ? root.scrollWidth - root.clientWidth : 99
  })
  await page.locator('[data-catalog-row]').first().waitFor()
  check('390 宽：只显示表清单', await page.locator('[data-catalog-list]').isVisible() && !(await page.locator('[data-catalog-overview]').isVisible()))
  check('……清单没有横向滚动', await overflow() <= 1, String(await overflow()))
  await page.locator('[data-catalog-row="visits"] [data-catalog-open-row]').click()
  await until(async () => (await page.locator('[data-catalog-detail="visits"][data-version]').count()) === 1)
  check('……打开一张表：只显示详情，有「返回表清单」', !(await page.locator('[data-catalog-list]').isVisible())
        && await page.locator('[data-catalog-detail-back]').isVisible())
  check('……详情没有横向滚动（列表格在自己的框里滚）', await overflow() <= 1, String(await overflow()))
  await openMark(page, 'grain')
  const box = await panel(page).boundingBox()
  check('……状态弹层在视口里', !!box && box.x >= 0 && box.x + box.width <= 390, JSON.stringify(box))
  await page.keyboard.press('Escape')
  await page.locator('[data-catalog-edit]').click()
  check('……编辑时保存栏可见、没有横向滚动', await page.locator('[data-catalog-save]').isVisible() && await overflow() <= 1)
  await page.locator('[data-catalog-cancel]').click()
  await page.locator('[data-catalog-detail-back]').click()
  check('……「返回表清单」回到清单', await until(async () => page.locator('[data-catalog-list]').isVisible()))
  await shot(page, 'catalog-narrow')
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await browser.close()

const after = await realCounts()
check('真库里的数据源和目录数量没有变化', JSON.stringify(after) === JSON.stringify(before), `${JSON.stringify(before)} → ${JSON.stringify(after)}`)
check('检查用的假数据源没有写进真库', !(await (await fetch(`${API}/datasources`)).json()).some((s) => NAMES.includes(s.name)))

console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 数据目录全部通过')
process.exit(failed ? 1 : 0)
