// 数据目录页（数据源卡片上的「数据目录」，/data/catalog/<源>）的交互回归。
//
// 数据目录的写操作都会改变助手和将来的 SQL 检查读到的东西：确认一项、驳回一项、整份提交都不是随手可以撤回的。
// 所以这里查的是「请求里带没带读到的版本」「批量确认只动推断项、来源不变」「编辑时没动的项原样交回（被驳回的照旧
// 驳回）」「别人刚改过时有没有停下来让人重新载入，而不是悄悄覆盖」——正常路径的页面检查一个字都不会说。
// 起草是后台任务：查的是「起草要求和补充说明有没有随请求发出」「关掉对话框任务照常进行、页面上有进度、再打开就是它」
// 「能停」「哪张表没写全、为什么写得清」「能只重试失败的」「已有任务、模型用不了这两种 409 各有去处」「清单上标出上次
// 起草失败的表、能单独重起草」。任务怎么往前走由检查自己推，页面每 1.5 秒取一次。
// 数据剖析（阶段 4B）会对业务库发查询：剖析接口、保存数据源一律在浏览器层答掉，查的是「确认前写没写预算」「进行中是否
// 明说停不下来」「报告说没说清用了几条查询、为什么停、跳过了什么」「各种 409 有没有下一步」「设置被拒时报错落没落到那一项」。
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
/** 景区库的数据源说明：起草时连同起草要求一起交给模型，起草对话框里写明 */
const S1_DESCRIPTION = '虚构的景区票务与客流库，供检查使用'
const SOURCES = [
  { ...manual(S1, 'zzcatscenic', 151), description: S1_DESCRIPTION },
  {
    ...blank, id: S2, name: 'zzcatupload', origin: 'upload', table_count: 2, tools: ['db_query__zzcatupload'], schema_synced_at: T0,
    database: `/tmp/check/uploads/${S2}.db`, import_mode: 'simple', current_recipe: null, open_staging: null,
    current_snapshot: { id: 'snap-check-cat', created_at: T0, file_name: '客流日报.xlsx', raw_state: 'kept' },
  },
  manual(S3, 'zzcatempty', 0),
  manual(S4, 'zzcatblank', 3),
]
const NAMES = SOURCES.map((s) => s.name)

/** 景区库存着的起草要求（数据源级，PUT /catalog/guidance 改它） */
const S1_GUIDANCE = '景区票务库：「核销」指闸机检票入园；金额单位是元，人数单位是人'

const freshState = () => ({
  sources: SOURCES,
  tables: {
    [S1]: scenicTables(),
    [S2]: uploadTables(),
    [S3]: [],
    [S4]: ['parking_lots', 'parking_records', 'shuttle_trips'].map((n, i) => table(n, 3 - i, [pk(), col('name', 'TEXT')], null)),
  },
  // 起草任务（后台任务）：建的先后就是数组的先后
  jobs: [],
  jobSeq: 0,
  guidance: { [S1]: S1_GUIDANCE },
  // 设置里的起草模型（没配：跟随助手的模型）和助手当前用的模型
  draftModel: { provider: null, model: null },
  assistantModel: { configured: true, provider: null, model: null, effective_provider: 'check-gateway', effective_model: 'check-assistant-model', error: null },
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

/**
 * 整份提交（apply_human_edit）：值改过的、新填的记为人工确认；值没动的按提交的状态（只认确认、驳回、初始状态）；
 * 提交里没有的关联关系，人工添加的删掉，其余的转为驳回（删掉会被起草和现推带回来）
 */
function applyPut(old, submitted) {
  const prev = slots(old)
  const sub = clone(submitted)
  for (const r of sub.relations ?? []) { r.id = relId(r.columns, r.to_table, r.to_columns); r.source ??= 'human'; r.status ??= 'confirmed' }
  const out = new Map()
  const given = slots(sub)
  for (const [path, it] of given) {
    const p = prev.get(path)
    if (!p || valueOf(path, it) !== valueOf(path, p)) {
      out.set(path, { ...it, source: 'human', status: 'confirmed', updated_at: new Date().toISOString() })
    } else {
      const ok = ['confirmed', 'rejected', INIT[p.source] ?? 'proposed'].includes(it.status)
      out.set(path, { ...p, status: ok ? it.status : p.status })
    }
  }
  for (const [path, p] of prev) {
    if (path.startsWith('relations.') && !given.has(path) && p.source !== 'human') out.set(path, { ...p, status: 'rejected' })
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
  // 每行带这张表最近一次起草任务里的那一项（夹具可以直接给 lastDraft，任务里有这张表时以任务为准）
  for (const r of rows) r.last_draft = lastDraftOf(state, src, r.table_name)
  // 探查结构截断了的源（state.schemaTotal 里给了数据库里的总数）：清单只有探查到的那部分
  const explored = all.filter((t) => !t.missing).length
  const total = state.schemaTotal?.[src] ?? explored
  const active = state.jobs.find((j) => j.source_id === src && JOB_ACTIVE.has(j.status))
  return { tables: rows, system_notes: isUpload(src), schema_note: all.length ? null : '尚未探查结构',
    schema_truncated: total > explored, schema_total: total, guidance: state.guidance[src] ?? null,
    active_job: active ? (({ id, status, total: n, done }) => ({ id, status, total: n, done }))(jobOut(active)) : null }
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

// ---------------------------------------------------------------------------
// 起草任务（后台任务，/api/datasources/{id}/catalog/draft-jobs）。任务怎么往前走由检查自己推（settle、finishJob、
// stopJob）：页面每 1.5 秒取一次，看到的就是推到的那一步。不按时间自己走，结果不受机器快慢影响
// ---------------------------------------------------------------------------
const JOB_ACTIVE = new Set(['queued', 'running'])
const TASK_END = new Set(['ok', 'partial', 'failed', 'skipped', 'cancelled'])
const RETRYABLE = new Set(['failed', 'partial', 'cancelled'])
const isoNow = () => new Date().toISOString()
/**
 * 服务端的几种答复（机读码 + 一句中文），说法照 api/catalog.py、data/catalog_jobs.py、data/draft_model.py 的原话。
 * 起草模型用不了有长短两句：没有接入时短（界面原样显示），设置里的起草模型所在接入停用时长（带句号，也要原样显示）
 */
const busyOf = (state, src) => ({
  detail: `数据源「${state.sources.find((s) => s.id === src)?.name ?? src}」正在起草数据目录，请等这次起草结束（或先停止它）再开始新的起草`,
  code: 'catalog_draft_busy',
})
const MODEL_DOWN = { detail: '未配置模型接入，无法由模型起草数据目录。请到「设置 → 模型接入」添加', code: 'catalog_draft_model_unavailable' }
const MODEL_OFF = {
  detail: '起草模型使用的模型接入「check-gw」已停用。请在「设置 → 模型接入」中启用它，或在「设置 → 偏好设置」的「起草模型」中重新选择',
  code: 'catalog_draft_model_unavailable',
}
/** 任务整体出错时服务端写的原因（catalog_jobs.JOB_ERROR：已经带着「起草任务出错：」开头） */
const JOB_ERR = '起草任务出错：database is locked'
/** 起草时每段最多几列（服务端的常量）：超过的宽表拆成「表级一段 + 列分段」 */
const PART_COLUMNS = 40

/**
 * 建一个排队的任务：表结构里没有的表照样建进任务、记 skipped。重试建的记着原任务（retry_of，界面不用）。
 * 超过 40 列的宽表均匀分段：列分段数 = ceil(列数 / 40)，段进度是「表级一段 + 列分段」。
 * 用模型的任务记着当前每段最多几列（part_columns，额度用完、超时多了服务端会减半）、调用接连没写出来时的提醒（notice），
 * 用量里另记超时和没写出来的调用次数；逐表记模型拿不准、留空的列数和前几个列名（blank_columns、blank_sample）
 */
function newJob(state, src, { tables, use_model, note = null, retry_of = null }) {
  const id = `check-job-${++state.jobSeq}`
  const tasks = tables.map((name) => {
    const t = state.tables[src].find((x) => x.name === name && !x.missing)
    const cols = t?.columns?.length ?? 0
    return {
      table_name: name, status: t ? 'pending' : 'skipped', columns: cols,
      parts_total: t && use_model ? (cols > PART_COLUMNS ? 1 + Math.ceil(cols / PART_COLUMNS) : 1) : null, parts_done: 0,
      added: 0, updated: 0, removed: 0, version: t?.version ?? 0, model_items: null, blank_columns: 0, blank_sample: [],
      error: t ? null : `表结构里没有 ${name}，请先重新探查结构`, model_error: null,
      started_at: null, finished_at: t ? null : isoNow(),
    }
  })
  const job = {
    id, source_id: src, status: 'queued', use_model: !!use_model, model: use_model ? 'check-draft-model' : null,
    guidance: state.guidance[src] ?? null, note, created_at: isoNow(), started_at: null, finished_at: null, created_by: ACTOR,
    retry_of, part_columns: use_model ? PART_COLUMNS : null, notice: null,
    usage: { calls: 0, input_tokens: 0, output_tokens: 0, timeouts: 0, failed_calls: 0 }, error: null, tasks,
  }
  state.jobs.push(job)
  return job
}

/** 接口返回的形状：计数、合计（含留空的列数）由逐表算出来 */
function jobOut(job) {
  const counts = { pending: 0, running: 0, ok: 0, partial: 0, failed: 0, skipped: 0, cancelled: 0 }
  for (const t of job.tasks) counts[t.status]++
  const sum = (k) => job.tasks.reduce((n, t) => n + t[k], 0)
  const { stopAsked, ...rest } = job
  return clone({ ...rest, total: job.tasks.length, done: job.tasks.filter((t) => TASK_END.has(t.status)).length, counts,
    added: sum('added'), updated: sum('updated'), removed: sum('removed'), blank_columns: sum('blank_columns') })
}

/**
 * 推进一张表：outcome.status 默认 ok（写完），给 running 就是还在起草（可带 parts_done）。写进了东西的表给它
 * 一个起草出的中文名，清单重取后看得出来。用模型的每次推进记一次调用
 */
function settle(state, job, name, outcome = {}) {
  const task = job.tasks.find((t) => t.table_name === name)
  if (job.status === 'queued') Object.assign(job, { status: 'running', started_at: isoNow() })
  const status = outcome.status ?? 'ok'
  Object.assign(task, { started_at: task.started_at ?? isoNow() }, outcome, { status })
  if (TASK_END.has(status)) {
    task.finished_at = isoNow()
    if (status === 'ok' && task.parts_total != null && outcome.parts_done == null) task.parts_done = task.parts_total
    if (task.added > 0) {
      const t = state.tables[job.source_id].find((x) => x.name === name)
      if (t) {
        writeEntry(t, { ...(t.notes ?? {}), label: item(`${name} 的起草名`) })
        task.version = t.version
      }
    }
  }
  if (job.use_model) {
    job.usage.calls += 1
    job.usage.input_tokens += 1200
    job.usage.output_tokens += 2400
  }
}

/** 排队的任务开始起草（服务端轮到它了） */
const runJob = (job) => Object.assign(job, { status: 'running', started_at: isoNow() })

/** 收尾：有失败、部分完成、停止的表就是 partial */
function finishJob(job, status) {
  const bad = job.tasks.some((t) => RETRYABLE.has(t.status))
  Object.assign(job, { status: status ?? (bad ? 'partial' : 'succeeded'), finished_at: isoNow() })
  job.started_at ??= job.finished_at
}

/**
 * 停下：没开始和没写完的表记 cancelled，已经写进目录的不动。带原因的（服务重启中断）任务和表记同一句（服务端 _close 的
 * 做法）；使用者点的停止不带原因
 */
function stopJob(job, status = 'cancelled', error = null) {
  for (const t of job.tasks) {
    if (t.status === 'pending' || t.status === 'running') Object.assign(t, { status: 'cancelled', error, finished_at: isoNow() })
  }
  if (error) job.error = error
  delete job.stopAsked
  finishJob(job, status)
}

/** 任务整体出错（数据源没了、开始时起草模型已经用不了、意外出错）：没结束的表也记失败，原因同任务的（服务端 _end 的做法） */
function failJob(job, error) {
  for (const t of job.tasks) {
    if (t.status === 'pending' || t.status === 'running') Object.assign(t, { status: 'failed', error, finished_at: isoNow() })
  }
  job.error = error
  finishJob(job, 'failed')
}

function lastDraftOf(state, src, name) {
  for (const job of [...state.jobs].reverse()) {
    if (job.source_id !== src) continue
    const t = job.tasks.find((x) => x.table_name === name)
    if (t) {
      return { status: t.status, job_id: job.id, finished_at: t.finished_at, error: t.error, model_error: t.model_error,
        blank_columns: t.blank_columns }
    }
  }
  return state.tables[src]?.find((x) => x.name === name)?.lastDraft ?? null
}

const jobOf = (state, key) => {
  const [, , src, , , id] = parts(key)
  return state.jobs.find((j) => j.id === id && j.source_id === src) ?? null
}
const activeOf = (state, src) => state.jobs.find((j) => j.source_id === src && JOB_ACTIVE.has(j.status)) ?? null

function jobHandlers(state, reply) {
  const NO_JOB = json({ detail: '起草任务不存在，可能属于别的数据源，或者已随数据源删除' }, 404)
  return [
    // 起草要求（数据源级）：空串清除，超长 422。请求里必须有 guidance 这一项（服务端同样：没有就是 pydantic 的英文 422）
    [/^PUT \/datasources\/check-cat-[a-z]+\/catalog\/guidance$/, reply('guidance', (route, { key, body }) => {
      if (!body || typeof body !== 'object' || !('guidance' in body)) {
        return json({ detail: [{ type: 'missing', loc: ['guidance'], msg: 'Field required', input: body }] }, 422)(route)
      }
      const g = String(body.guidance ?? '').trim()
      if ([...g].length > 2000) return json({ detail: `起草要求最多 2000 字，当前 ${[...g].length} 字，请删减后再保存` }, 422)(route)
      state.guidance[parts(key)[2]] = g || null
      return json({ guidance: g || null })(route)
    })],
    // 建任务：同一个源已有排队、进行中的 409；不给 tables 取使用次数前 20 张
    [/^POST \/datasources\/check-cat-[a-z]+\/catalog\/draft-jobs$/, reply('createJob', (route, { key, body }) => {
      const src = parts(key)[2]
      if (activeOf(state, src)) return json(busyOf(state, src), 409)(route)
      const tables = body?.tables?.length ? body.tables
        : state.tables[src].filter((t) => !t.missing).sort((a, b) => b.usage - a.usage).slice(0, 20).map((t) => t.name)
      const job = newJob(state, src, { tables, use_model: !!body?.use_model, note: body?.note?.trim() || null })
      return json({ job: jobOut(job) }, 202)(route)
    })],
    [/^GET \/datasources\/check-cat-[a-z]+\/catalog\/draft-jobs\/active$/, (route, { key }) => {
      const job = activeOf(state, parts(key)[2])
      return json({ job: job && jobOut(job) })(route)
    }],
    [/^GET \/datasources\/check-cat-[a-z]+\/catalog\/draft-jobs\/latest$/, (route, { key }) => {
      const job = state.jobs.filter((j) => j.source_id === parts(key)[2]).at(-1) ?? null
      return json({ job: job && jobOut(job) })(route)
    }],
    // 停止：排队的直接停；进行中的记下来，等检查用 stopJob 推到底（这期间页面应当写「正在停止」）
    [/^POST \/datasources\/check-cat-[a-z]+\/catalog\/draft-jobs\/[^/]+\/cancel$/, reply('cancel', (route, { key }) => {
      const job = jobOf(state, key)
      if (!job) return NO_JOB(route)
      if (!JOB_ACTIVE.has(job.status)) return json({ detail: '这次起草已经结束了，不需要停止', code: 'catalog_draft_finished' }, 409)(route)
      if (job.status === 'queued') stopJob(job)
      else job.stopAsked = true
      return json({ job: jobOut(job) })(route)
    })],
    // 重试：新任务只含失败、部分完成、停止的表，沿用用模型和补充说明。起草模型用不了的 409 由检查用 replies.retry 给
    [/^POST \/datasources\/check-cat-[a-z]+\/catalog\/draft-jobs\/[^/]+\/retry$/, reply('retry', (route, { key }) => {
      const old = jobOf(state, key)
      if (!old) return NO_JOB(route)
      if (activeOf(state, old.source_id)) return json(busyOf(state, old.source_id), 409)(route)
      const tables = old.tasks.filter((t) => RETRYABLE.has(t.status)).map((t) => t.table_name)
      if (!tables.length) return json({ detail: '这次起草里没有失败、只写出一部分或停止了的表，不需要重试' }, 422)(route)
      const job = newJob(state, old.source_id, { tables, use_model: old.use_model, note: old.note, retry_of: old.id })
      return json({ job: jobOut(job) }, 202)(route)
    })],
    [/^GET \/datasources\/check-cat-[a-z]+\/catalog\/draft-jobs\/[^/]+$/, (route, { key }) => {
      const job = jobOf(state, key)
      return job ? json({ job: jobOut(job) })(route) : NO_JOB(route)
    }],
    // 起草对话框写明用哪个起草模型：设置里的 catalog_draft（其余设置照真服务端的）、助手当前用的模型
    [/^GET \/settings$/, async (route) => {
      const real = await route.fetch()
      const body = await real.json().catch(() => ({}))
      return route.fulfill({ response: real, json: { ...body, catalog_draft: state.draftModel } })
    }],
    [/^GET \/copilot\/model$/, (route) => json(state.assistantModel)(route)],
  ]
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
    // 起草任务、起草要求：放在单表的 GET / PUT 前面（catalog/guidance 也对得上单表 PUT 的写法）
    ...jobHandlers(state, reply),
    // 旧的同步起草接口：前端不该再用它，调了就答 410，下面的检查会数它
    [/^POST \/datasources\/check-cat-[a-z]+\/catalog\/draft$/, json({ detail: '检查脚本：前端不该再调用同步起草接口' }, 410)],
    // 影响面：引用这张表的已发布、受管模板（只读）。没配的表答空
    [/^GET \/datasources\/check-cat-[a-z]+\/catalog\/[^/]+\/impact$/, reply('impact', (route, { key }) => {
      const name = parts(key)[4]
      return json({ table: name, templates: state.impact?.[name] ?? [] })(route)
    })],
    // 剖析没配答复的一律 503：检查脚本不能让剖析真的发到业务库
    [/^POST \/datasources\/check-cat-[a-z]+\/catalog\/profile$/, reply('profile', json({ detail: '检查脚本没有为这次剖析准备答复' }, 503))],
    // 保存数据源（剖析设置）：照提交改假状态，回整条数据源
    [/^PATCH \/datasources\/check-cat-[a-z]+$/, reply('patch', (route, { key, body }) => {
      const id = parts(key)[2]
      state.sources = state.sources.map((s) => (s.id === id ? { ...s, ...body, password: undefined } : s))
      return json(state.sources.find((s) => s.id === id))(route)
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
/** 起草对话框在哪一步：setup / running / done；没开着为 null */
const draftPhase = (page) => draftBox(page).getAttribute('data-draft-dialog', { timeout: 500 }).catch(() => null)
/** 对话框里某一处的文字（空白压成一个空格）；没有这一处为空串 */
const draftText = async (page, sel) => (await draftBox(page).locator(sel).first().innerText({ timeout: 500 }).catch(() => '')).replace(/\s+/g, ' ').trim()

/** 选几张表（第一张点、最后一张 Shift 点）或者不选，打开起草对话框，按需勾用模型、写补充说明，开始；等到进入进行中 */
async function startDraft(page, { select = null, model = false, note = '' } = {}) {
  if (select) {
    await page.locator(`[data-catalog-select="${select[0]}"]`).click()
    if (select.length > 1) await page.locator(`[data-catalog-select="${select.at(-1)}"]`).click({ modifiers: ['Shift'] })
    await page.locator('[data-catalog-draft-selected]').click()
  } else {
    await page.locator('[data-catalog-draft]').first().click()
  }
  await draftBox(page).waitFor()
  if (model) await draftBox(page).locator('[data-draft-model] input').check()
  if (note) await draftBox(page).locator('[data-draft-note] textarea').fill(note)
  await page.locator('[data-draft-start]').click()
  return until(async () => (await draftPhase(page)) === 'running')
}

/** 起草用的几句话（虚构） */
const GUIDANCE2 = '景区票务库：「核销」指闸机检票入园；金额单位是元。会员相关的表统一写「会员」，不写「用户」'
const DRAFT_NOTE = '这一批是闸机和会员相关的表'
// 宽表没写出来的那一段写明列的范围（列名照表结构原样）；只有真拆过才写「已拆小重试仍没写出」
const WIDE_ERR = '这张表 50 列，分 3 段起草，第 3 段（promo_attr_19 到 promo_attr_43，共 25 列）没写出来：模型的思考和输出用完了额度（32768 个 token），已拆小重试仍没写出。可以只重试这张表'
const CALL_ERR = '调用模型出错：服务暂时不可用（已重试 2 次）'
/** 调用接连没写出来时服务端给的提醒（虚构的一句，照约定写明次数、原因和办法） */
const DRAFT_NOTICE = '最近 6 次调用有 4 次没写出来（3 次超时、1 次额度用完）。建议在「设置 → 偏好设置」的「起草模型」换一个模型、缩小起草范围，或者停止这次起草'
/** 宽表里模型拿不准、留空的列（服务端最多给 5 个列名） */
const BLANK_SAMPLE = ['promo_attr_31', 'promo_attr_32', 'promo_attr_33', 'promo_attr_34', 'promo_attr_35']
/** 补上缩写含义的起草要求（虚构） */
const GUIDANCE_ABBR = `${S1_GUIDANCE}。promo_attr 开头的列是促销活动的附加属性，按列名后面的编号区分`
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
  // 没填中文名的表：标题就是表名，「未填写中文名」降为下一行的淡色提示；填了的照旧中文名作标题、表名在下一行
  const titleOf = (name) => page.locator(`[data-catalog-row="${name}"] [data-row-title]`).evaluate((el) => ({
    text: el.innerText, weight: Number(getComputedStyle(el).fontWeight), color: getComputedStyle(el).color,
    hint: getComputedStyle(el.closest('[data-catalog-row]').querySelector('[data-row-no-label]') ?? el).color,
  }))
  const gatesTitle = await titleOf('gates')
  const visitsTitle = await titleOf('visits')
  check('没填中文名：标题写表名、用正文的颜色和字重，「未填写中文名」是淡色的次要提示', gatesTitle.text === 'gates' && gatesTitle.weight >= 500
    && gatesTitle.color === visitsTitle.color && gatesTitle.hint !== gatesTitle.color, JSON.stringify(gatesTitle))
  check('填了中文名：标题写中文名', visitsTitle.text === '入园记录')
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
  check('筛选按钮带数量：全部 152、有待确认项 5、没有待确认项 3、没有目录 144', counts.join('|') === '全部152|有待确认项5|没有待确认项3|没有目录144', counts.join('|'))
  // 「已确认」专指人工确认：只有已验证项的表不说「全部已确认」，说「没有待确认项」，悬停写明已验证的不需要逐项确认
  check('筛选不说「全部已确认」（只有已验证项的表也在这一组）', !counts.join('|').includes('全部已确认')
    && (await page.locator('[data-filter="done"]').getAttribute('title')).includes('已验证的项由外键约束或数据剖析核实'))
  await page.locator('[data-filter="pending"]').click()
  names = await rowNames(page)
  check('「有待确认项」：只有还有推断项的表', names.join(',') === 'visits,order_items,ticket_types,store_sales,inventory_snapshots', names.join(','))
  await page.locator('[data-filter="done"]').click()
  names = await rowNames(page)
  check('「没有待确认项」：订单、景区、只剩目录的旧表', names.join(',') === 'orders,parks,legacy_coupons', names.join(','))
  await page.locator('[data-filter="none"]').click()
  check('「没有目录」：144 张', (await rowNames(page)).length === 144)
  // 筛选组是单选组：方向键在组内移动并选中
  await page.locator('[data-filter="none"]').focus()
  await page.keyboard.press('ArrowLeft')
  check('筛选组：← 选中前一项「没有待确认项」', (await page.locator('[data-catalog-filter]').getAttribute('data-catalog-filter')) === 'done')
  await page.locator('[data-filter="all"]').click()

  // 排序
  await page.locator('[data-catalog-sort]').selectOption('pending')
  names = await rowNames(page)
  const proposedOf = (n) => countsOf(state.tables[S1].find((t) => t.name === n)?.notes).proposed
  check('按待确认项排序：推断项最多的在前', proposedOf(names[0]) >= proposedOf(names[1]) && proposedOf(names[1]) >= proposedOf(names[2])
        && names.at(-1) === 'legacy_coupons', names.slice(0, 3).join(','))
  await page.locator('[data-catalog-sort]').selectOption('name')
  names = await rowNames(page)
  const inSchema = names.slice(0, -1)
  check('按表名排序：字母序，只剩目录的表仍在最后', inSchema.join(',') === [...inSchema].sort((a, b) => a.localeCompare(b, 'en')).join(',')
        && names.at(-1) === 'legacy_coupons', names.slice(0, 3).join(','))
  await page.locator('[data-catalog-sort]').selectOption('usage')

  // 筛选、搜索、排序写进地址栏：刷新不丢，打开一张表、再回到清单都带着
  await page.locator('[data-filter="pending"]').click()
  await page.locator('[data-catalog-sort]').selectOption('name')
  await search.fill('order')
  await until(async () => new URL(page.url()).searchParams.get('q') === 'order', 3000)
  const listUrl = new URL(page.url())
  check('筛选、搜索、排序写进地址栏的查询参数', listUrl.searchParams.get('filter') === 'pending' && listUrl.searchParams.get('sort') === 'name'
    && listUrl.searchParams.get('q') === 'order', listUrl.search)
  await page.reload({ waitUntil: 'networkidle' })
  await page.locator('[data-catalog-row]').first().waitFor()
  names = await rowNames(page)
  check('刷新之后筛选、搜索、排序都还在', (await page.locator('[data-catalog-filter]').getAttribute('data-catalog-filter')) === 'pending'
    && await page.locator('[data-catalog-sort]').inputValue() === 'name' && await search.inputValue() === 'order'
    && names.join(',') === 'order_items', `${names.join(',')} ${page.url()}`)
  await page.locator('[data-catalog-row="order_items"] [data-catalog-open-row]').click()
  await page.waitForURL(`**/data/catalog/${S1}/order_items?**`, { timeout: 5000 }).catch(() => {})
  const tableUrl = new URL(page.url())
  check('打开一张表：地址带着清单的查询参数，清单照旧筛着', tableUrl.pathname === `/data/catalog/${S1}/order_items`
    && tableUrl.search === listUrl.search && (await rowNames(page)).join(',') === 'order_items', page.url())
  await page.goBack({ waitUntil: 'networkidle' })
  check('后退回到清单：筛选照旧', new URL(page.url()).search === listUrl.search
    && (await page.locator('[data-catalog-filter]').getAttribute('data-catalog-filter')) === 'pending', page.url())
  await page.locator('[data-filter="all"]').click()
  await page.locator('[data-catalog-sort]').selectOption('usage')
  await search.fill('')
  check('回到默认（全部、按使用次数、不搜索）：地址里不留这几个参数', await until(async () => new URL(page.url()).search === '', 3000), page.url())

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
  check('点「还有推断项未确认」：切到「有待确认项」筛选',
    (await page.locator('[data-catalog-filter]').getAttribute('data-catalog-filter')) === 'pending')
  check('……排序是按使用次数', await page.locator('[data-catalog-sort]').inputValue() === 'usage')
  const shown = await rowNames(page)
  const expectTop = used.filter((t) => progress(t) === 'pending').sort((a, b) => b.usage - a.usage).map((t) => t.name)
  check('……只列查询过的、有推断项的表，按使用次数排', shown.join(',') === expectTop.join(','), `${shown.join(',')} ≠ ${expectTop.join(',')}`)
  await bar.locator('[data-usage-focus="none"]').click()
  check('点「还没有目录」：切到「没有目录」筛选', (await page.locator('[data-catalog-filter]').getAttribute('data-catalog-filter')) === 'none'
    && (await rowNames(page))[0] === used.filter((t) => progress(t) === 'none').sort((a, b) => b.usage - a.usage)[0].name)
  // 摘要说「N 张还没有目录」，点进去就是那 N 张：只看运行中查询过的表，不是全部没有目录的表
  const expectNone = used.filter((t) => progress(t) === 'none').sort((a, b) => b.usage - a.usage).map((t) => t.name)
  const noneShown = await rowNames(page)
  check('……只列运行中查询过、还没有目录的表，张数和摘要一致', noneShown.join(',') === expectNone.join(',') && noneShown.length === none,
    `${noneShown.length} 张：${noneShown.slice(0, 6).join(',')}`)
  check('……清单上写明「只看运行中查询过的表」', (await page.locator('[data-catalog-used-only]').innerText().catch(() => '')).includes('只看运行中查询过的表'))
  const usedCounts = await page.locator('[data-catalog-filter] [data-filter]').evaluateAll((els) => els.map((e) => e.innerText.replace(/\s+/g, '')))
  check('……筛选按钮上的数字按同一个口径算', usedCounts.includes(`没有目录${none}`) && usedCounts.includes(`全部${used.length}`), usedCounts.join('|'))
  check('……摘要上点中的那一段标为按下', await bar.locator('[data-usage-focus="none"]').getAttribute('aria-pressed') === 'true')
  await shot(page, 'catalog-used-only')
  await page.locator('[data-catalog-used-only-clear]').click()
  const allNone = state.tables[S1].filter((t) => progress(t) === 'none').length
  check('取消「只看运行中查询过的表」：回到全部没有目录的表，摘要不再标按下', (await rowNames(page)).length === allNone
    && await page.locator('[data-catalog-used-only]').count() === 0
    && await bar.locator('[data-usage-focus="none"]').getAttribute('aria-pressed') === 'false', String((await rowNames(page)).length))
  check('摘要只读、不发请求', writes(sent, /./).length === 0)
  // 都审完了：只写一句，不给按钮
  for (const t of state.tables[S1]) if (t.notes) t.notes = applyPut({}, { label: { value: t.notes.label?.value ?? t.name } })
  for (const t of state.tables[S1]) if (!t.notes && t.usage > 0) t.notes = { label: item(t.name, 'human') }
  await goto(page, `/data/catalog/${S1}`)
  await bar.waitFor({ timeout: 3000 })
  check('都确认了：写「都没有待确认项」，没有按钮', (await bar.innerText()).includes(`运行中查询过的 ${used.length} 张表都没有待确认项`)
    && await bar.locator('[data-usage-focus]').count() === 0, await bar.innerText())
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
  await close()
})

await section('数据目录 · 起草：设置与开始', async () => {
  const state = freshState()
  const replies = { createJob: [] }
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
  check('没勾「用模型起草」：不显示起草要求和补充说明（只对模型有用）',
    await draftBox(page).locator('[data-draft-guidance], [data-draft-note]').count() === 0)
  await draftBox(page).locator('[data-draft-model] input').check()
  const guidance = draftBox(page).locator('[data-draft-guidance] textarea')
  const note = draftBox(page).locator('[data-draft-note] textarea')
  check('勾上以后：起草要求显示数据源现在存的那一份，可以改', await guidance.inputValue().catch(() => '') === S1_GUIDANCE
        && await guidance.isEditable().catch(() => false), await guidance.inputValue().catch(() => '—'))
  check('……本次补充说明是空的', await note.inputValue().catch(() => '—') === '')
  check('……写明用的是哪个起草模型：设置里没配，跟随助手的模型（写出当前是哪个）',
    (await draftText(page, '[data-draft-model-name]')).includes('跟随助手的模型（当前：check-assistant-model）'), await draftText(page, '[data-draft-model-name]'))
  check('……数据源说明也一并交给模型：写出来', (await draftText(page, '[data-draft-source-description]')).includes(S1_DESCRIPTION),
    await draftText(page, '[data-draft-source-description]'))
  await shot(page, 'catalog-draft-setup')

  // 超长：写明上限，不能开始
  await guidance.fill('长'.repeat(2001))
  check('起草要求超过 2,000 字：写明上限，「起草」不可用', (await draftText(page, '[data-draft-guidance]')).includes('不超过 2,000 字')
        && await page.locator('[data-draft-start]').isDisabled(), await draftText(page, '[data-draft-guidance]'))
  await guidance.fill(GUIDANCE2)
  await note.fill(DRAFT_NOTE)

  // 起草模型用不了：409 带机读码，留在设置阶段写明原因，不进入进行中、不去取任务
  replies.createJob.push(json(MODEL_DOWN, 409))
  await page.locator('[data-draft-start]').click()
  check('起草模型用不了（409）：留在设置阶段，照原样写服务端那句话', await until(async () => (await draftText(page, '[data-draft-start-error]')) === MODEL_DOWN.detail)
        && (await draftPhase(page)) === 'setup', await draftText(page, '[data-draft-start-error]'))
  const keys = writes(sent, /./).map((s) => s.key.replace(/^(\w+) .*\/catalog\//, '$1 '))
  check('……起草要求改过：先存起草要求、再建任务', keys.join(',') === 'PUT guidance,POST draft-jobs', keys.join(','))
  check('……存的是改过的那一份', JSON.stringify(lastBody(sent, /catalog\/guidance$/)) === JSON.stringify({ guidance: GUIDANCE2 }),
    JSON.stringify(lastBody(sent, /catalog\/guidance$/)))
  check('……没有去取任务进度', count(sent, /draft-jobs\/check-job/) === 0)

  // 再点一次：这回建得起来；起草要求已经存过、没再改，不再存一次
  await page.locator('[data-draft-start]').click()
  check('建起来以后进入进行中', await until(async () => (await draftPhase(page)) === 'running'))
  const body = lastBody(sent, /draft-jobs$/)
  check('建任务的请求：所选的表（清单顺序）、用模型、本次补充说明',
    JSON.stringify(body) === JSON.stringify({ tables: ['gates', 'store_sales', 'members'], use_model: true, note: DRAFT_NOTE }), JSON.stringify(body))
  check('……起草要求只存了一次', count(sent, /^PUT .*catalog\/guidance$/) === 1)
  check('……写请求都带本机署名', writes(sent, /catalog\/(draft-jobs|guidance)/).every((s) => decodeURIComponent(s.actor ?? '') === ACTOR))
  check('进行中：逐表列出 3 张，都在等待', (await draftBox(page).locator('[data-draft-task]').count()) === 3
        && (await draftText(page, '[data-draft-task="gates"] [data-draft-task-status]')) === '等待', await draftText(page, '[data-draft-tasks]'))
  check('……进度写「已完成 0 / 3 张表」', (await draftText(page, '[data-draft-progress]')) === '已完成 0 / 3 张表', await draftText(page, '[data-draft-progress]'))
  check('……清单上的选择清空（这几张已经交给起草任务）', (await page.locator('[data-catalog-count]').innerText()).endsWith('张表'))
  await shot(page, 'catalog-draft-running')
  stopJob(state.jobs.at(-1))
  check('没有调用旧的同步起草接口', count(sent, /catalog\/draft$/) === 0)
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('数据目录 · 起草：进度、结果与重试', async () => {
  const state = freshState()
  const replies = { retry: [] }
  const { page, sent, errors, close } = await open(`/data/catalog/${S1}`, { handlers: catalogHandlers(state, replies) })
  await page.locator('[data-catalog-row]').first().waitFor()
  check('开始起草（闸机到会员 3 张，用模型，带补充说明）', await startDraft(page, { select: ['gates', 'members'], model: true, note: DRAFT_NOTE }))
  const job = state.jobs.at(-1)

  // 一张写完、宽表起草到第 1 段
  settle(state, job, 'gates', { added: 3, model_items: 6 })
  settle(state, job, 'store_sales', { status: 'running', parts_done: 1 })
  check('进度：已完成 1 / 3 张表', await until(async () => (await draftBox(page).locator('[data-draft-progress]').getAttribute('data-draft-progress', { timeout: 500 }).catch(() => '')) === '1/3'),
    await draftText(page, '[data-draft-progress]'))
  const bar = draftBox(page).locator('[role="progressbar"]').first()
  check('……进度条读屏可读：名称、当前值、上限', (await bar.getAttribute('aria-valuenow')) === '1' && (await bar.getAttribute('aria-valuemax')) === '3'
        && !!(await bar.getAttribute('aria-label')))
  check('……宽表写出段进度「1 / 3 段」，状态是「起草中」', (await draftText(page, '[data-draft-task="store_sales"] [data-draft-task-parts]')) === '1 / 3 段'
        && (await draftText(page, '[data-draft-task="store_sales"] [data-draft-task-status]')) === '起草中', await draftText(page, '[data-draft-task="store_sales"]'))
  check('……写完的那张写「已完成」', (await draftText(page, '[data-draft-task="gates"] [data-draft-task-status]')) === '已完成')
  check('……合计写「新增 3 项、更新 0 项」', (await draftText(page, '[data-draft-summary]')) === '新增 3 项、更新 0 项', await draftText(page, '[data-draft-summary]'))
  check('……写明用的模型', (await draftText(page, '[data-draft-model-used]')).includes('check-draft-model'), await draftText(page, '[data-draft-model-used]'))
  check('写完的表马上并入目录：清单重取，起草出的中文名出现',
    await until(async () => (await page.locator('[data-catalog-row="gates"] [data-row-label]').innerText({ timeout: 500 }).catch(() => '')) === 'gates 的起草名'))

  // 宽表有一段没写出来；会员表一段都没写出来
  settle(state, job, 'store_sales', { status: 'partial', parts_done: 2, added: 4, model_items: 8, model_error: WIDE_ERR })
  settle(state, job, 'members', { status: 'failed', model_items: 0, model_error: CALL_ERR })
  finishJob(job)
  check('结束：标题写「起草完成，部分表未写全」', await until(async () => (await draftPhase(page)) === 'done')
        && (await draftText(page, '[data-draft-title]')) === '起草完成，部分表未写全', await draftText(page, '[data-draft-title]'))
  const problems = await draftText(page, '[data-draft-problems]')
  check('……写明哪些没写全：失败 1 张、部分完成 1 张', problems.includes('失败 1 张') && problems.includes('部分完成 1 张'), problems)
  const shown = await draftBox(page).locator('[data-draft-task]').evaluateAll((els) => els.map((e) => e.getAttribute('data-draft-task')))
  check('……逐表只列没写全的 2 张，各自写原因', shown.join(',') === 'store_sales,members'
        && (await draftText(page, '[data-draft-task="store_sales"] [data-draft-task-reason]')).includes('第 3 段（promo_attr_19 到 promo_attr_43，共 25 列）没写出来')
        && (await draftText(page, '[data-draft-task="members"] [data-draft-task-reason]')).includes('已重试 2 次'), shown.join(','))
  await draftBox(page).locator('[data-draft-show-all]').click()
  check('……点「显示全部」列出 3 张', (await draftBox(page).locator('[data-draft-task]').count()) === 3)
  check('……合计、用量照实写', (await draftText(page, '[data-draft-summary]')) === '新增 7 项、更新 0 项'
        && (await draftText(page, '[data-draft-usage]')).includes('调用模型 4 次'), `${await draftText(page, '[data-draft-summary]')} · ${await draftText(page, '[data-draft-usage]')}`)
  check('……清单上失败的表标「上次起草失败」', await until(async () => (await page.locator('[data-catalog-row="members"] [data-row-last-draft="failed"]').count()) === 1))
  await shot(page, 'catalog-draft-partial')

  // 结束以后不再取进度
  const polls = () => count(sent, new RegExp(`^GET .*draft-jobs/${job.id}$`))
  const n = polls()
  await sleep(3200)
  check('结束以后不再取进度', polls() === n, `${n} → ${polls()}`)

  // 只重试失败的表
  const retry = page.locator('[data-draft-retry]')
  check('给「只重试失败的表（2 张）」', (await retry.innerText().catch(() => '')).includes('只重试失败的表（2 张）'), await retry.innerText().catch(() => ''))
  // 重试时起草模型已经用不了（服务端和建任务时一样回 409 catalog_draft_model_unavailable）：留在结果里，照原样写服务端
  // 那句话（长句也不拆成「标题：原因」），不建新任务
  replies.retry.push(json(MODEL_OFF, 409))
  await retry.click()
  check('重试时起草模型用不了（409）：留在结果里，照原样写服务端那句话，不建新任务',
    await until(async () => (await draftText(page, '[data-draft-action-error]')) === MODEL_OFF.detail)
        && (await draftPhase(page)) === 'done' && state.jobs.at(-1) === job, await draftText(page, '[data-draft-action-error]'))
  await retry.click()
  check('重试：调重试接口，进入新任务的进行中，只含没写全的 2 张', await until(async () => (await draftPhase(page)) === 'running'
        && (await draftBox(page).locator('[data-draft-task]').count()) === 2) && count(sent, new RegExp(`^POST .*draft-jobs/${job.id}/retry$`)) === 2)
  check('……上一次的报错收起', await draftBox(page).locator('[data-draft-action-error]').count() === 0)
  const job2 = state.jobs.at(-1)
  check('……新任务沿用用模型和补充说明', job2.id !== job.id && job2.use_model && job2.note === DRAFT_NOTE && job2.retry_of === job.id)
  settle(state, job2, 'store_sales', { added: 1, model_items: 3 })
  settle(state, job2, 'members', { added: 5, model_items: 5 })
  finishJob(job2)
  check('……都写完：「起草完成」，不再给重试', await until(async () => (await draftText(page, '[data-draft-title]')) === '起草完成')
        && await page.locator('[data-draft-retry]').count() === 0, await draftText(page, '[data-draft-title]'))
  check('……没有没写全的表时逐表全部列出', (await draftBox(page).locator('[data-draft-task]').count()) === 2)
  await page.locator('[data-draft-close]').click()
  check('关掉以后清单上的标记跟着最新一次起草：会员表不再标失败', await until(async () => (await page.locator('[data-catalog-row="members"] [data-row-last-draft]').count()) === 0))

  // 一项都没写进去：只有「模型给了内容、只是和现有目录一致」才说「没有变化」
  const nothing = async (outcome) => {
    await startDraft(page, { select: ['parks'], model: true })
    const j = state.jobs.at(-1)
    settle(state, j, 'parks', outcome)
    finishJob(j)
    await until(async () => (await draftPhase(page)) === 'done')
    const out = await draftText(page, '[data-draft-summary]')
    await page.locator('[data-draft-close]').click()
    return out
  }
  check('模型一项可用内容都没给：不说「没有变化」，说「目录没有更新」', (await nothing({ status: 'failed', model_items: 0, model_error: '模型没有给出这张表的可用内容' }))
        === '目录没有更新：模型没有给出可用的内容，原因见下方')
  check('……模型给了内容、只是和现有目录一致：才说「没有变化」', (await nothing({ model_items: 3 })) === '没有变化：起草出的内容与现有目录一致')
  check('没有调用旧的同步起草接口', count(sent, /catalog\/draft$/) === 0)
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('数据目录 · 起草：停止、后台继续与再打开', async () => {
  const state = freshState()
  const { page, sent, errors, close } = await open(`/data/catalog/${S1}`, { handlers: catalogHandlers(state) })
  await page.locator('[data-catalog-row]').first().waitFor()
  // 页头「起草」：没选表，范围默认是使用次数最多的 20 张
  await page.locator('[data-catalog-draft]').first().click()
  await draftBox(page).waitFor()
  const scopes = await draftBox(page).locator('[data-draft-scope]').evaluateAll((els) => els.map((e) => e.getAttribute('data-draft-scope')))
  check('没选表时：范围是「使用次数最多的 20 张」「全部 151 张」，默认前者', scopes.join(',') === 'top,all'
        && await draftBox(page).locator('[data-draft-scope="top"] input').isChecked()
        && (await draftBox(page).locator('[data-draft-scope="all"]').innerText()).includes('全部 151 张表'), scopes.join(','))
  check('……没有上一次起草时不写「上一次」', await draftBox(page).locator('[data-draft-last]').count() === 0)
  await draftBox(page).locator('[data-draft-model] input').check()
  await page.locator('[data-draft-start]').click()
  await until(async () => (await draftPhase(page)) === 'running')
  const body = lastBody(sent, /draft-jobs$/) ?? {}
  check('请求带使用次数最多的 20 张表（不含只剩目录的那张），补充说明空着就不带', body.tables?.length === 20 && !body.tables.includes('legacy_coupons')
        && body.use_model === true && !('note' in body), JSON.stringify(body).slice(0, 160))
  const job = state.jobs.at(-1)
  check('刚建好还在排队：标题写「排队中」', (await draftText(page, '[data-draft-title]')).startsWith('排队中'), await draftText(page, '[data-draft-title]'))
  runJob(job)
  check('……轮到它了：「正在起草…」', await until(async () => (await draftText(page, '[data-draft-title]')) === '正在起草…'), await draftText(page, '[data-draft-title]'))

  // 关掉对话框：任务不停，目录页上方显示进度
  await page.locator('[data-draft-hide]').click()
  check('「在后台继续」：对话框关上，没有发停止', await until(async () => (await draftBox(page).count()) === 0) && count(sent, /\/cancel$/) === 0)
  const strip = page.locator('[data-catalog-draft-progress]')
  check('……目录页上方显示一条进度', await until(async () => (await strip.count()) === 1) && (await strip.innerText()).includes('已完成 0 / 20 张表'),
    await strip.innerText({ timeout: 500 }).catch(() => ''))
  settle(state, job, 'visits', { added: 2 })
  settle(state, job, 'orders', { added: 1 })
  check('……进度跟着任务走', await until(async () => (await strip.getAttribute('data-catalog-draft-progress', { timeout: 500 }).catch(() => '')) === '2/20'),
    await strip.getAttribute('data-catalog-draft-progress', { timeout: 500 }).catch(() => ''))
  check('……进度条读屏可读', (await strip.locator('[role="progressbar"]').getAttribute('aria-valuenow').catch(() => '')) === '2')
  await shot(page, 'catalog-draft-strip')
  // 点开回到对话框：直接是进行中的那个任务
  await strip.locator('[data-catalog-draft-open]').click()
  check('点开：回到对话框，直接显示进行中的任务', await until(async () => (await draftPhase(page)) === 'running')
        && (await draftText(page, '[data-draft-progress]')) === '已完成 2 / 20 张表', await draftText(page, '[data-draft-progress]'))
  // 停止：调停止接口；服务端马上取消进行中的模型调用，只等手上那张表写完（停止接口等它收尾才答）。这期间写「正在停止」，
  // 说法照服务端的做法：不是「等当前这次调用结束」——一次调用可能要几分钟
  await page.locator('[data-draft-stop]').click()
  check('点「停止」：调停止接口，写「正在停止：取消进行中的模型调用…」，停止按钮不能再点',
    await until(async () => (await draftText(page, '[data-draft-title]')).startsWith('正在停止：取消进行中的模型调用'))
        && count(sent, new RegExp(`^POST .*draft-jobs/${job.id}/cancel$`)) === 1 && await page.locator('[data-draft-stop]').isDisabled(),
    await draftText(page, '[data-draft-title]'))
  stopJob(job)
  check('……停下来：标题写「已停止」，没开始的 18 张记已停止', await until(async () => (await draftPhase(page)) === 'done')
        && (await draftText(page, '[data-draft-title]')) === '已停止' && (await draftText(page, '[data-draft-problems]')).includes('已停止 18 张'),
    `${await draftText(page, '[data-draft-title]')} · ${await draftText(page, '[data-draft-problems]')}`)
  check('……已经写进目录的不回滚：合计照实写', (await draftText(page, '[data-draft-summary]')) === '新增 3 项、更新 0 项', await draftText(page, '[data-draft-summary]'))
  check('……可以只重试没写的 18 张', (await page.locator('[data-draft-retry]').innerText().catch(() => '')).includes('（18 张）'))
  await page.locator('[data-draft-close]').click()

  // 再起一个（不用模型），按 Esc 关掉对话框，重新载入页面：清单带着进行中的任务，打开就是它
  await page.locator('[data-catalog-draft]').first().click()
  await draftBox(page).waitFor()
  check('上一次停止了：设置阶段写明上一次的结果，可以点开看', (await draftText(page, '[data-draft-last]')).includes('已停止')
        && await draftBox(page).locator('[data-draft-last-open]').count() === 1, await draftText(page, '[data-draft-last]'))
  await page.locator('[data-draft-start]').click()
  await until(async () => (await draftPhase(page)) === 'running')
  const job3 = state.jobs.at(-1)
  check('不用模型：请求的用模型为假，逐表不写段进度', lastBody(sent, /draft-jobs$/)?.use_model === false
        && await draftBox(page).locator('[data-draft-task-parts]').count() === 0)
  await page.keyboard.press('Escape')
  check('按 Esc 关掉对话框也不停任务', await until(async () => (await draftBox(page).count()) === 0)
        && count(sent, new RegExp(`^POST .*draft-jobs/${job3.id}/cancel$`)) === 0)
  await goto(page, `/data/catalog/${S1}`)
  check('重新载入页面：清单带着进行中的任务，上方照样显示进度', await until(async () => (await page.locator('[data-catalog-draft-progress]').count()) === 1))
  await page.locator('[data-catalog-draft]').first().click()
  check('……点「起草」直接显示进行中的任务，不是重新选范围', await until(async () => (await draftPhase(page)) === 'running')
        && await draftBox(page).locator('[data-draft-scope]').count() === 0)
  await page.locator('[data-draft-hide]').click()
  // 关着对话框时结束：提示，带「查看结果」
  for (const t of job3.tasks) if (t.status === 'pending') settle(state, job3, t.table_name, { added: 1 })
  finishJob(job3)
  const toastBox = page.locator('[data-toast]').filter({ hasText: '起草完成' })
  check('关着对话框时结束：提示「数据目录起草完成」，带「查看结果」', await until(async () => (await toastBox.count()) > 0),
    await page.locator('[data-toast]').allInnerTexts().then((t) => t.join(' | ')).catch(() => ''))
  check('……上方的进度收起', (await page.locator('[data-catalog-draft-progress]').count()) === 0)
  await toastBox.getByRole('button', { name: '查看结果' }).click()
  check('……点「查看结果」打开这一次的结果', await until(async () => (await draftPhase(page)) === 'done')
        && (await draftText(page, '[data-draft-title]')) === '起草完成', await draftText(page, '[data-draft-title]'))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('数据目录 · 起草：已有任务（409）、服务重启中断、整体出错与离开页面', async () => {
  const state = freshState()
  // 上一次起草因服务重启中断：没结束的表记已停止，写明原因
  const old = newJob(state, S1, { tables: ['visits', 'orders', 'parks'], use_model: true, note: DRAFT_NOTE })
  settle(state, old, 'visits', { added: 2 })
  stopJob(old, 'interrupted', '服务重启，起草中断')
  const { page, sent, errors, close } = await open(`/data/catalog/${S1}`, { handlers: catalogHandlers(state) })
  await page.locator('[data-catalog-row]').first().waitFor()
  await page.locator('[data-catalog-draft]').first().click()
  await draftBox(page).waitFor()
  check('上一次起草因服务重启中断：设置阶段写明，给「查看」', await until(async () => (await draftText(page, '[data-draft-last]')).includes('服务重启')),
    await draftText(page, '[data-draft-last]'))
  await draftBox(page).locator('[data-draft-last-open]').click()
  check('……点开：结果写明服务重启中断，可以接着重试', await until(async () => (await draftPhase(page)) === 'done')
        && (await draftText(page, '[data-draft-title]')) === '服务重启，起草中断' && (await draftText(page, '[data-draft-interrupted]')).includes('重试'),
    `${await draftText(page, '[data-draft-title]')} · ${await draftText(page, '[data-draft-interrupted]')}`)
  // 服务端把任务和没写完的表都记「服务重启，起草中断」：标题、提示已经写了，逐表只写「已停止」，不再各写一遍
  check('……没写的 2 张记「已停止」，原因不再逐张重复', (await draftText(page, '[data-draft-task="orders"] [data-draft-task-status]')) === '已停止'
        && (await draftText(page, '[data-draft-task="parks"] [data-draft-task-status]')) === '已停止'
        && await draftBox(page).locator('[data-draft-task-reason]').count() === 0, await draftText(page, '[data-draft-tasks]'))
  await shot(page, 'catalog-draft-interrupted')
  await page.locator('[data-draft-retry]').click()
  check('……「只重试失败的表」接着起草没写的 2 张', await until(async () => (await draftPhase(page)) === 'running')
        && JSON.stringify(state.jobs.at(-1).tasks.map((t) => t.table_name)) === JSON.stringify(['orders', 'parks']))
  const again = state.jobs.at(-1)
  settle(state, again, 'orders', { added: 1 })
  settle(state, again, 'parks', { added: 1 })
  finishJob(again)
  await until(async () => (await draftPhase(page)) === 'done')
  await page.locator('[data-draft-close]').click()

  // 任务整体出错（意外出错、开始时起草模型已经用不了……）：服务端把没结束的表也记失败、原因同任务的。原因只在标题下面
  // 写一次，照服务端的原话（它已经带着「起草任务出错：」开头，界面不再套一层）；逐表不再重复同一句；这些表都能重试
  check('开始起草（使用次数最多的 20 张，用模型）', await startDraft(page, { model: true }))
  const broken = state.jobs.at(-1)
  settle(state, broken, 'visits', { added: 2, model_items: 4 })
  settle(state, broken, 'orders', { status: 'failed', model_items: 0, model_error: CALL_ERR })
  failJob(broken, JOB_ERR)
  check('任务整体出错：标题写「起草失败」，原因照服务端的原话写一次', await until(async () => (await draftPhase(page)) === 'done')
        && (await draftText(page, '[data-draft-title]')) === '起草失败' && (await draftText(page, '[data-draft-job-error]')) === JOB_ERR,
    `${await draftText(page, '[data-draft-title]')} · ${await draftText(page, '[data-draft-job-error]')}`)
  check('……逐表只列没写的 19 张；和任务相同的原因不再逐张重复，各有原因的照样写', (await draftBox(page).locator('[data-draft-task]').count()) === 19
        && await draftBox(page).locator('[data-draft-task-reason]').count() === 1
        && (await draftText(page, '[data-draft-task="orders"] [data-draft-task-reason]')) === CALL_ERR, await draftText(page, '[data-draft-tasks]'))
  check('……这 19 张都能重试', (await page.locator('[data-draft-retry]').innerText().catch(() => '')).includes('（19 张）'))
  await shot(page, 'catalog-draft-failed')
  await page.locator('[data-draft-close]').click()

  // 别处（另一个标签页）又起了一个任务，这一页还不知道：点开始时服务端回 409，转去显示那一个。先等设置阶段把上一次起草
  // 取回来（写出「上一次起草…起草失败」），免得它晚到、取回的已经是别处那个进行中的任务，就走不到 409 了
  await page.locator('[data-catalog-draft]').first().click()
  await draftBox(page).waitFor()
  check('上一次整体出错：设置阶段写明「起草失败」', await until(async () => (await draftText(page, '[data-draft-last]')).includes('起草失败')),
    await draftText(page, '[data-draft-last]'))
  const other = newJob(state, S1, { tables: ['gates', 'members'], use_model: true })
  settle(state, other, 'gates', { added: 1 })
  const pollsOf = (id) => count(sent, new RegExp(`^GET .*draft-jobs/${id}$`))
  await page.locator('[data-draft-start]').click()
  check('已有进行中的任务（409）：转去显示那一个，并写明', await until(async () => (await draftPhase(page)) === 'running')
        && (await draftText(page, '[data-draft-busy]')).includes('已有进行中的起草任务')
        && (await draftText(page, '[data-draft-progress]')) === '已完成 1 / 2 张表', `${await draftText(page, '[data-draft-busy]')} · ${await draftText(page, '[data-draft-progress]')}`)
  check('……先问进行中的是哪一个，之后跟着它取进度', count(sent, /draft-jobs\/active$/) >= 1 && await until(async () => pollsOf(other.id) >= 1),
    `active ${count(sent, /draft-jobs\/active$/)} 次，取进度 ${pollsOf(other.id)} 次`)

  // 任务进行中离开数据目录页（页内跳转，页面卸下，不是重新载入）：不再取进度。卸下以后稍停，让卸下前已经发出的那次
  // 取进度落进记录（停得太久，没清掉的那一拍可能恰好落在这段里、数不出来），再数两个多间隔
  await page.locator('[data-draft-hide]').click()
  await until(async () => (await draftBox(page).count()) === 0)
  await page.locator('[data-catalog-back]').click()
  const left = await until(async () => new URL(page.url()).pathname === '/data/databases' && (await page.locator('[data-catalog-page]').count()) === 0)
  await sleep(100)
  const polled = pollsOf(other.id)
  await sleep(3200)
  check('任务进行中离开数据目录页：页面卸下以后不再取进度', left && pollsOf(other.id) === polled,
    `${left ? '' : `没有离开（${new URL(page.url()).pathname}）；`}${polled} → ${pollsOf(other.id)}`)
  stopJob(other)
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('数据目录 · 起草：清单上的失败标记与单表重起草', async () => {
  const state = freshState()
  const replies = { createJob: [] }
  const FAIL = '模型 420 秒内没有写完这一段'
  const tOf = (n) => state.tables[S1].find((t) => t.name === n)
  tOf('orders').lastDraft = { status: 'failed', job_id: 'check-job-old', finished_at: T0, error: null, model_error: FAIL }
  tOf('order_items').lastDraft = { status: 'partial', job_id: 'check-job-old', finished_at: T0, error: null, model_error: WIDE_ERR }
  tOf('parks').lastDraft = { status: 'ok', job_id: 'check-job-old', finished_at: T0, error: null, model_error: null }
  const { page, sent, errors, close } = await open(`/data/catalog/${S1}`, { handlers: catalogHandlers(state, replies) })
  await page.locator('[data-catalog-row]').first().waitFor()
  const mark = (n) => page.locator(`[data-catalog-row="${n}"] [data-row-last-draft]`)
  check('上次起草失败的表：清单上标「上次起草失败」并写原因', (await mark('orders').getAttribute('data-row-last-draft', { timeout: 1000 }).catch(() => '')) === 'failed'
        && (await mark('orders').innerText()).includes('上次起草失败') && (await mark('orders').innerText()).includes(FAIL),
    await mark('orders').innerText({ timeout: 500 }).catch(() => ''))
  check('……部分完成的标「上次起草部分完成」', (await mark('order_items').getAttribute('data-row-last-draft', { timeout: 1000 }).catch(() => '')) === 'partial'
        && (await mark('order_items').innerText()).includes('上次起草部分完成'))
  check('……原因全文在悬停里', (await mark('order_items').getAttribute('title', { timeout: 500 }).catch(() => '') ?? '').includes(WIDE_ERR))
  check('……写完了的、没起草过的不标', await mark('parks').count() === 0 && await mark('visits').count() === 0)
  await shot(page, 'catalog-last-draft-marks')

  // 打开这张表：写明原因，给「重新起草这张表」
  await page.locator('[data-catalog-row="orders"] [data-catalog-open-row]').click()
  const note = page.locator('[data-catalog-last-draft]')
  check('打开这张表：写明上次起草失败和原因', await until(async () => (await note.count()) === 1)
        && (await note.innerText()).includes('上次起草失败') && (await note.innerText()).includes(FAIL), await note.innerText({ timeout: 500 }).catch(() => ''))
  await shot(page, 'catalog-last-draft-detail')
  await note.locator('[data-catalog-redraft]').click()
  check('「重新起草这张表」：建只含这张表、用模型、不带补充说明的任务', await until(async () => !!lastBody(sent, /draft-jobs$/))
        && JSON.stringify(lastBody(sent, /draft-jobs$/)) === JSON.stringify({ tables: ['orders'], use_model: true }), JSON.stringify(lastBody(sent, /draft-jobs$/)))
  check('……打开对话框显示这个任务的进度', await until(async () => (await draftPhase(page)) === 'running')
        && (await draftBox(page).locator('[data-draft-task]').count()) === 1)
  const job = state.jobs.at(-1)
  settle(state, job, 'orders', { added: 2 })
  finishJob(job)
  await until(async () => (await draftPhase(page)) === 'done')
  await page.locator('[data-draft-close]').click()
  check('……写完以后：清单上的标记和详情里的说明都收起', await until(async () => (await mark('orders').count()) === 0
        && (await page.locator('[data-catalog-last-draft]').count()) === 0))

  // 模型用不了：单表重起草直接提示原因，不打开对话框
  await page.locator('[data-catalog-row="order_items"] [data-catalog-open-row]').click()
  await until(async () => (await page.locator('[data-catalog-last-draft]').count()) === 1)
  check('部分完成的表：详情里写「上次起草部分完成」', (await page.locator('[data-catalog-last-draft]').innerText()).includes('上次起草部分完成'))
  // 正在编辑这张表：起草会写进它，编辑中的保存就会撞上版本冲突，先不给点
  await page.locator('[data-catalog-edit]').click()
  check('正在编辑这张表：「重新起草这张表」不能点，写明先保存或取消', await page.locator('[data-catalog-redraft]').isDisabled()
        && (await page.locator('[data-catalog-last-draft]').innerText()).includes('保存或取消后再重新起草'))
  await page.locator('[data-catalog-cancel]').click()
  replies.createJob.push(json(MODEL_DOWN, 409))
  await page.locator('[data-catalog-redraft]').click()
  check('模型用不了：提示原因，不打开对话框', await until(async () => (await page.locator('[data-toast]').filter({ hasText: '未配置模型接入' }).count()) > 0)
        && await draftBox(page).count() === 0, await page.locator('[data-toast]').allInnerTexts().then((t) => t.join(' | ')).catch(() => ''))
  check('只发过起草任务相关的写请求', writes(sent, /./).every((s) => /catalog\/(draft-jobs|guidance)/.test(s.key)),
    JSON.stringify(writes(sent, /./).map((s) => s.key)))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('数据目录 · 起草：接连没写出来的提醒、超时与段缩小', async () => {
  const state = freshState()
  const { page, errors, natives, close } = await open(`/data/catalog/${S1}`, { handlers: catalogHandlers(state) })
  await page.locator('[data-catalog-row]').first().waitFor()
  check('开始起草（闸机到会员 3 张，用模型）', await startDraft(page, { select: ['gates', 'members'], model: true }))
  const job = state.jobs.at(-1)
  const usageLine = () => draftText(page, '[data-draft-usage-line]')
  settle(state, job, 'store_sales', { status: 'running', parts_done: 1 })
  check('还没有超时、没写出来的调用：用量只写调用次数和 token，没有提醒、超时的说明和段大小的说明',
    await until(async () => (await usageLine()).includes('调用模型 1 次')) && !(await usageLine()).includes('超时')
        && !(await usageLine()).includes('没写出来')
        && await draftBox(page).locator('[data-draft-notice], [data-draft-timeout-note], [data-draft-part-columns]').count() === 0, await usageLine())

  // 宽表的段接连超时、用完额度：服务端记下超时和没写出来的次数，之后的段缩小到每段 20 列，并给出一句提醒
  Object.assign(job.usage, { calls: 7, timeouts: 3, failed_calls: 4 })
  Object.assign(job, { part_columns: 20, notice: DRAFT_NOTICE })
  const notice = draftBox(page).locator('[data-draft-notice]')
  check('接连没写出来：进行中醒目显示服务端的提醒，是提醒的样式（不是错误）',
    await until(async () => (await draftText(page, '[data-draft-notice]')) === DRAFT_NOTICE)
        && (await notice.getAttribute('data-notice')) === 'warn', await draftText(page, '[data-draft-notice]'))
  const above = await page.evaluate(() => {
    const n = document.querySelector('[data-draft-notice]')?.getBoundingClientRect()
    const list = document.querySelector('[data-draft-tasks]')?.getBoundingClientRect()
    return !!n && !!list && n.bottom <= list.top
  })
  check('……提醒在逐表进度上面，不用往下翻', above)
  // 没写出来的次数里含超时的：写成「其中」，免得读成两个数相加
  check('……用量写「没写出来 4 次（其中超时 3 次）」', (await usageLine()).includes('没写出来 4 次（其中超时 3 次）'), await usageLine())
  check('……写明超时的调用不计用量', (await draftText(page, '[data-draft-timeout-note]')).includes('超时的调用不计用量'),
    await draftText(page, '[data-draft-timeout-note]'))
  check('……写明之后的段每段最多 20 列', (await draftText(page, '[data-draft-part-columns]')).includes('每段最多 20 列'),
    await draftText(page, '[data-draft-part-columns]'))
  await shot(page, 'catalog-draft-notice')

  // 关掉对话框：上方的进度也带着提醒；好转后服务端清掉提醒，两处都收起
  await page.locator('[data-draft-hide]').click()
  const strip = page.locator('[data-catalog-draft-progress]')
  const stripNotice = strip.locator('[data-catalog-draft-notice]')
  check('在后台继续：上方的进度也带着提醒（全文在悬停里）', await until(async () => (await stripNotice.count()) === 1)
        && (await stripNotice.getAttribute('title')) === DRAFT_NOTICE && (await stripNotice.innerText()).length > 0,
    await stripNotice.getAttribute('title', { timeout: 500 }).catch(() => ''))
  await shot(page, 'catalog-draft-strip-notice')
  job.notice = null
  check('……好转后服务端清掉提醒：上方的进度不再带着', await until(async () => (await stripNotice.count()) === 0))
  await strip.locator('[data-catalog-draft-open]').click()
  check('……点开对话框：提醒也不再显示，超时和没写出来的次数照旧', await until(async () => (await draftPhase(page)) === 'running')
        && await notice.count() === 0 && (await usageLine()).includes('其中超时 3 次'), await usageLine())

  // 又接连没写出来：提醒再出现；任务结束以后不再显示（停止、缩小范围的建议只对进行中的任务有用），用量里的次数和
  // 超时不计用量的说明照旧，段缩小的说明收起
  Object.assign(job.usage, { timeouts: 5, failed_calls: 6 })
  Object.assign(job, { part_columns: 10, notice: DRAFT_NOTICE })
  check('又接连没写出来：提醒再出现，段缩小到每段 10 列', await until(async () => (await draftText(page, '[data-draft-notice]')) === DRAFT_NOTICE)
        && (await draftText(page, '[data-draft-part-columns]')).includes('每段最多 10 列'), await draftText(page, '[data-draft-part-columns]'))
  for (const name of ['gates', 'store_sales', 'members']) settle(state, job, name, { added: 1, model_items: 1 })
  finishJob(job)
  check('结束：不再显示提醒和段缩小的说明，用量里的次数和超时不计用量的说明照旧', await until(async () => (await draftPhase(page)) === 'done')
        && await notice.count() === 0 && await draftBox(page).locator('[data-draft-part-columns]').count() === 0
        && (await usageLine()).includes('没写出来 6 次（其中超时 5 次）') && (await draftText(page, '[data-draft-timeout-note]')).includes('超时的调用不计用量'),
    await usageLine())
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('数据目录 · 起草：模型拿不准、留空的列', async () => {
  const state = freshState()
  // 不在这次起草里的一张表：上一次起草部分完成，又有留空的列
  state.tables[S1].find((t) => t.name === 'channels').lastDraft = {
    status: 'partial', job_id: 'check-job-old', finished_at: T0, error: null, model_error: CALL_ERR, blank_columns: 4,
  }
  const { page, sent, errors, natives, close } = await open(`/data/catalog/${S1}`, { handlers: catalogHandlers(state) })
  await page.locator('[data-catalog-row]').first().waitFor()
  check('开始起草（闸机到会员 3 张，用模型）', await startDraft(page, { select: ['gates', 'members'], model: true }))
  const job = state.jobs.at(-1)

  // 留空的列：模型列出了、中文名和含义都没给。表的状态不因此变。闸机表写完了；宽表还在起草，已经有几列留空
  settle(state, job, 'gates', { added: 2, model_items: 3, blank_columns: 1, blank_sample: ['gate_code'] })
  settle(state, job, 'store_sales', { status: 'running', parts_done: 2, blank_columns: 5, blank_sample: BLANK_SAMPLE })
  const blankOf = (n) => draftBox(page).locator(`[data-draft-task="${n}"] [data-draft-task-blank]`)
  const rowBlank = (n) => page.locator(`[data-catalog-row="${n}"] [data-row-blank]`)
  check('起草中：逐表跟着写留空的列；清单上写完的表才提示「上次起草」，还在起草的不提示',
    await until(async () => (await blankOf('store_sales').innerText({ timeout: 500 }).catch(() => '')).includes('5 列模型拿不准、留空了')
      && (await rowBlank('gates').count()) === 1)
        && (await rowBlank('store_sales').count()) === 0 && (await page.locator('[data-catalog-row="store_sales"] [data-row-last-draft]').count()) === 0,
    `${await draftText(page, '[data-draft-task="store_sales"]')} · ${await rowBlank('gates').count()} · ${await rowBlank('store_sales').count()}`)
  settle(state, job, 'store_sales', { added: 30, model_items: 38, blank_columns: 12, blank_sample: BLANK_SAMPLE })
  check('逐表：写完的宽表写「12 列模型拿不准、留空了」，状态照旧是「已完成」',
    await until(async () => (await blankOf('store_sales').innerText({ timeout: 500 }).catch(() => '')).includes('12 列模型拿不准、留空了'))
        && (await draftText(page, '[data-draft-task="store_sales"] [data-draft-task-status]')) === '已完成',
    await draftText(page, '[data-draft-task="store_sales"]'))
  const sample = blankOf('store_sales').locator('[data-draft-task-blank-sample]')
  check('……悬停看得到是哪几列', ((await blankOf('store_sales').locator('summary').getAttribute('title')) ?? '').includes(BLANK_SAMPLE.join('、')),
    await blankOf('store_sales').locator('summary').getAttribute('title').catch(() => ''))
  check('……列名默认收起', !(await sample.isVisible()))
  await blankOf('store_sales').locator('summary').click()
  check('……展开写出列名，列不全时写一共几列', await until(async () => sample.isVisible())
        && (await draftText(page, '[data-draft-task="store_sales"] [data-draft-task-blank-sample]')) === `留空的列：${BLANK_SAMPLE.join('、')} 等 12 列`,
    await draftText(page, '[data-draft-task="store_sales"] [data-draft-task-blank-sample]'))
  settle(state, job, 'members', { added: 3, model_items: 3 })
  finishJob(job)
  check('结束：标题照旧「起草完成」（留空的列不算没写全），不给「只重试失败的表」',
    await until(async () => (await draftPhase(page)) === 'done') && (await draftText(page, '[data-draft-title]')) === '起草完成'
        && await page.locator('[data-draft-retry]').count() === 0, await draftText(page, '[data-draft-title]'))
  const blankSum = await draftText(page, '[data-draft-blank-summary]')
  check('……汇总写合计：共 13 列模型拿不准、留空了（2 张表）', blankSum.includes('共 13 列模型拿不准、留空了（2 张表）'), blankSum)
  check('……并给出办法：在起草要求里补充缩写和术语的含义，再重新起草这几张表，已有的内容不会丢',
    blankSum.includes('在起草要求里补充缩写和术语的含义') && blankSum.includes('重新起草这几张表') && blankSum.includes('已有的内容不会丢'), blankSum)
  const shown = await draftBox(page).locator('[data-draft-task]').evaluateAll((els) => els.map((e) => e.getAttribute('data-draft-task')))
  check('……逐表默认只列有留空列的 2 张，没有留空列的那张不写，可以显示全部', shown.join(',') === 'gates,store_sales'
        && await draftBox(page).locator('[data-draft-show-all]').count() === 1, shown.join(','))
  await draftBox(page).locator('[data-draft-show-all]').click()
  check('……显示全部：没有留空列的表不写留空', (await draftBox(page).locator('[data-draft-task]').count()) === 3 && await blankOf('members').count() === 0)
  await shot(page, 'catalog-draft-blank')

  // 清单：有留空列的表显示轻提示，不是失败标记
  check('清单：有留空列的表显示轻提示「上次起草有 12 列模型拿不准、留空了」',
    await until(async () => (await rowBlank('store_sales').innerText({ timeout: 500 }).catch(() => '')).includes('上次起草有 12 列模型拿不准、留空了')),
    await rowBlank('store_sales').innerText({ timeout: 500 }).catch(() => ''))
  const colors = await page.evaluate(() => {
    const color = (q) => { const el = document.querySelector(q); return el ? getComputedStyle(el).color : null }
    return { blank: color('[data-catalog-row="store_sales"] [data-row-blank]'), faint: color('[data-catalog-row="store_sales"] [data-row-usage]'),
      partial: color('[data-catalog-row="channels"] [data-row-last-draft]') }
  })
  check('……不是失败标记：不写「上次起草失败 / 部分完成」，用次要信息的淡色，不用警告色', await page.locator('[data-catalog-row="store_sales"] [data-row-last-draft]').count() === 0
        && !!colors.blank && colors.blank === colors.faint && colors.blank !== colors.partial, JSON.stringify(colors))
  check('……没有留空列的表不提示', await rowBlank('members').count() === 0)
  check('……部分完成又有留空列的表：失败标记和留空提示都在', (await page.locator('[data-catalog-row="channels"] [data-row-last-draft="partial"]').count()) === 1
        && (await rowBlank('channels').innerText().catch(() => '')).includes('上次起草有 4 列模型拿不准、留空了'))

  // 「重新起草这 2 张表」：回到设置，范围就是这 2 张、勾着用模型，焦点在起草要求；补上缩写的含义再开始
  await draftBox(page).locator('[data-draft-blank-redraft]').click()
  check('「重新起草这 2 张表」：回到设置，范围是有留空列的这 2 张，勾着用模型，焦点在起草要求',
    await until(async () => (await draftPhase(page)) === 'setup')
        && await draftBox(page).locator('[data-draft-scope="preset"] input').isChecked()
        && (await draftText(page, '[data-draft-scope="preset"]')).includes('有留空列的 2 张表')
        && await draftBox(page).locator('[data-draft-model] input').isChecked()
        && await until(async () => page.evaluate(() => document.activeElement?.id === 'catalog-draft-guidance')),
    `${await draftText(page, '[data-draft-scope="preset"]')} · ${await page.evaluate(() => document.activeElement?.id ?? '')}`)
  check('……起草按钮写「起草 2 张表」', (await page.locator('[data-draft-start]').innerText()).includes('起草 2 张表'))
  await draftBox(page).locator('[data-draft-guidance] textarea').fill(GUIDANCE_ABBR)
  await page.locator('[data-draft-start]').click()
  check('……先存起草要求，再建只含这 2 张表、用模型的任务', await until(async () => (await draftPhase(page)) === 'running')
        && JSON.stringify(lastBody(sent, /catalog\/guidance$/)) === JSON.stringify({ guidance: GUIDANCE_ABBR })
        && JSON.stringify(lastBody(sent, /draft-jobs$/)) === JSON.stringify({ tables: ['gates', 'store_sales'], use_model: true }),
    JSON.stringify(lastBody(sent, /draft-jobs$/)))
  // 这一回宽表仍有几列拿不准（已有的内容不丢由服务端保证）。关着对话框时结束：提示里也写留空的列
  const again = state.jobs.at(-1)
  await page.locator('[data-draft-hide]').click()
  await until(async () => (await draftBox(page).count()) === 0)
  settle(state, again, 'gates', { model_items: 3 })
  settle(state, again, 'store_sales', { updated: 5, model_items: 40, blank_columns: 7, blank_sample: BLANK_SAMPLE })
  finishJob(again)
  const toastBox = page.locator('[data-toast]').filter({ hasText: '起草完成' })
  check('关着对话框时结束：提示里写「7 列模型拿不准、留空了」', await until(async () => (await toastBox.count()) > 0)
        && (await toastBox.first().innerText()).includes('7 列模型拿不准、留空了'),
    await page.locator('[data-toast]').allInnerTexts().then((t) => t.join(' | ')).catch(() => ''))
  await toastBox.getByRole('button', { name: '查看结果' }).click()
  check('……点「查看结果」：汇总写合计', await until(async () => (await draftText(page, '[data-draft-blank-summary]')).includes('共 7 列模型拿不准、留空了（1 张表）')),
    await draftText(page, '[data-draft-blank-summary]'))
  await page.locator('[data-draft-close]').click()
  // 再打开起草：设置阶段写明上一次有留空的列，可以点开
  await page.locator('[data-catalog-draft]').first().click()
  await draftBox(page).waitFor()
  check('再打开起草：设置阶段写明上一次起草有 7 列模型拿不准、留空了，可以点开', await until(async () => (await draftText(page, '[data-draft-last]')).includes('7 列模型拿不准、留空了'))
        && await draftBox(page).locator('[data-draft-last-open]').count() === 1, await draftText(page, '[data-draft-last]'))
  await page.keyboard.press('Escape')
  await until(async () => (await draftBox(page).count()) === 0)

  // 表详情：顶部同样提示（不是失败），给「重新起草这张表」和「补充起草要求」
  await page.locator('[data-catalog-row="store_sales"] [data-catalog-open-row]').click()
  const tip = page.locator('[data-catalog-last-draft]')
  check('表详情：顶部写「上次起草有 7 列模型拿不准、留空了」，是提示（不是失败）',
    await until(async () => (await tip.innerText({ timeout: 500 }).catch(() => '')).includes('上次起草有 7 列模型拿不准、留空了'))
        && (await tip.getAttribute('data-notice')) === 'info', await tip.innerText({ timeout: 500 }).catch(() => ''))
  check('……写明办法：补充起草要求再重新起草，已有的内容不会丢；给「重新起草这张表」', (await tip.innerText()).includes('已有的内容不会丢')
        && await tip.locator('[data-catalog-redraft]').count() === 1, await tip.innerText())
  await shot(page, 'catalog-blank-detail')
  const jobsBefore = count(sent, /^POST .*draft-jobs$/)
  await tip.locator('[data-catalog-draft-guidance]').click()
  check('「补充起草要求」：打开起草设置，范围只有这张表，勾着用模型，焦点在起草要求',
    await until(async () => (await draftPhase(page)) === 'setup')
        && await draftBox(page).locator('[data-draft-scope="preset"] input').isChecked()
        && (await draftText(page, '[data-draft-scope="preset"]')).includes('store_sales')
        && await draftBox(page).locator('[data-draft-model] input').isChecked()
        && (await page.locator('[data-draft-start]').innerText()).includes('起草 1 张表')
        && await until(async () => page.evaluate(() => document.activeElement?.id === 'catalog-draft-guidance')),
    `${await draftText(page, '[data-draft-scope="preset"]')} · ${await page.evaluate(() => document.activeElement?.id ?? '')}`)
  await page.keyboard.press('Escape')
  check('……没改就关掉：不建任务', await until(async () => (await draftBox(page).count()) === 0) && count(sent, /^POST .*draft-jobs$/) === jobsBefore)
  await tip.locator('[data-catalog-redraft]').click()
  check('「重新起草这张表」：建只含这张表、用模型的任务', await until(async () => count(sent, /^POST .*draft-jobs$/) === jobsBefore + 1)
        && JSON.stringify(lastBody(sent, /draft-jobs$/)) === JSON.stringify({ tables: ['store_sales'], use_model: true }),
    JSON.stringify(lastBody(sent, /draft-jobs$/)))
  const third = state.jobs.at(-1)
  await until(async () => (await draftPhase(page)) === 'running')
  settle(state, third, 'store_sales', { model_items: 40 })
  finishJob(third)
  await until(async () => (await draftPhase(page)) === 'done')
  await page.locator('[data-draft-close]').click()
  check('……这回没有留空的列：清单的轻提示和详情的提示都收起', await until(async () => (await rowBlank('store_sales').count()) === 0 && (await tip.count()) === 0))

  // 部分完成又有留空列的表：一条说明里写原因和留空的列，只有一个「重新起草这张表」
  await page.locator('[data-catalog-row="channels"] [data-catalog-open-row]').click()
  check('部分完成又有留空列的表：详情里一条说明写原因和留空的列，只有一个「重新起草这张表」',
    await until(async () => (await tip.innerText({ timeout: 500 }).catch(() => '')).includes('上次起草部分完成'))
        && (await tip.innerText()).includes(CALL_ERR) && (await tip.innerText()).includes('4 列模型拿不准、留空了')
        && (await tip.getAttribute('data-notice')) === 'warn' && await tip.locator('[data-catalog-redraft]').count() === 1,
    await tip.innerText({ timeout: 500 }).catch(() => ''))
  await cleanCopy(page, '留空列的提示')
  check('只发过起草任务相关的写请求', writes(sent, /./).every((s) => /catalog\/(draft-jobs|guidance)/.test(s.key)),
    JSON.stringify(writes(sent, /./).map((s) => s.key)))
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
  // 时间用全站统一的短格式（formatTime：今年写「9/28 10:00」），完整时间（带秒和时区）放在悬停里
  const updated = detail(page).locator('[data-detail-updated]')
  check('……修改时间写全站统一的短格式，完整时间放在悬停里', /^审核甲 于 (2026\/)?9\/28 10:00 修改$/.test(await updated.innerText())
    && !head.includes('UTC') && (await updated.getAttribute('title')) === '2026-09-28 10:00:00 (UTC+08:00)', `${await updated.innerText()} / ${await updated.getAttribute('title')}`)
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
  check('……更新时间也写短格式，完整时间放在悬停里', /更新于 (2026\/)?9\/28 10:00/.test(pt) && !pt.includes('UTC')
    && (await panel(page).locator('[data-item-updated]').getAttribute('title')) === '2026-09-28 10:00:00 (UTC+08:00)', pt.replace(/\s+/g, ' '))
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
  // 「直接引用」不是警示：用强调色，不用琥珀色（全站的「等待、要人处理」）
  const directLook = await daily.locator('[data-impact="direct"]').first().evaluate((el) => {
    const probe = document.createElement('span')
    document.body.append(probe)
    const tone = (v) => { probe.style.color = `var(${v})`; return getComputedStyle(probe).color }
    const out = { color: getComputedStyle(el).color, accent: tone('--accent'), waiting: tone('--st-waiting') }
    probe.remove()
    return out
  })
  check('「直接引用」用强调色，不用警示的琥珀色', directLook.color === directLook.accent && directLook.color !== directLook.waiting, JSON.stringify(directLook))
  check('合并查询写明经由哪个输入', (await daily.locator('[data-impact-node="m"]').innerText()).includes('经由输入「查询入园人数」'))
  check('Agent 写「可能涉及」，悬停说明原因', (await sec.locator('[data-impact-template="wf-ask"] [data-impact="possible"]').getAttribute('title')).includes('运行时生成'))
  check('模板名点进去是画布，定位到那个节点', (await daily.locator('a').first().getAttribute('href')) === '/studio/wf-daily?focus=q')
  // 开发模式下 StrictMode 会把挂载时的取数做两遍，只查有没有取、没有写
  check('统计影响面只读', writes(sent, /impact/).length === 0 && count(sent, /\/visits\/impact$/) >= 1,
    `${count(sent, /\/visits\/impact$/)} 次`)
  // 在目录页改过（这里是编辑保存）：表头说有几个已发布模板引用这张表，和助手卡片保存后同一句
  const savedHint = page.locator('[data-catalog-saved-impact]')
  check('没改过：表头不说影响面', await savedHint.count() === 0)
  await page.locator('[data-catalog-edit]').click()
  await page.locator('[data-input="grain"]').fill('一张门票的一次入园（检查）')
  await page.locator('[data-catalog-save]').click()
  check('在目录页编辑保存后：表头说「2 个已发布模板引用这张表，下次正式运行时会提示数据目录有变化」',
    await until(async () => (await savedHint.count()) === 1)
    && (await savedHint.innerText()).includes('2 个已发布模板引用这张表，下次正式运行时会提示数据目录有变化'),
    await savedHint.innerText().catch(() => ''))
  await shot(page, 'catalog-saved-impact')
  await savedHint.locator('[data-catalog-saved-impact-show]').click()
  check('……点「查看模板」：焦点落到「引用这张表的模板」一栏', await until(async () => page.evaluate(() => document.activeElement?.id === 'catalog-sec-impact'), 2000))
  await savedHint.locator('[data-catalog-saved-impact-dismiss]').click()
  check('……可以收起', await savedHint.count() === 0)
  // 单项确认也升版本：同样说
  await openMark(page, 'description')
  await page.locator('[data-item-panel] [data-review-action="confirm"]').click()
  check('单项确认之后同样说', await until(async () => (await savedHint.count()) === 1))
  // 没有模板引用的表：直说没有
  await openDetail(page, S1, 'orders')
  check('换一张表：上一张的提示不带过来', await savedHint.count() === 0)
  check('没有模板引用：直说没有', (await page.locator('[data-catalog-section="impact"] [data-catalog-impact="0"]').innerText())
    .includes('没有已发布或受管的模板引用这张表'))
  // 表详情保存之后（版本变了）重新统计
  const before = count(sent, /\/orders\/impact$/)
  await page.locator('[data-catalog-edit]').click()
  await page.locator('[data-input="grain"]').fill('一笔订单一行（检查）')
  await page.locator('[data-catalog-save]').click()
  await until(async () => count(sent, /\/orders\/impact$/) > before, 3000)
  check('保存之后重新统计影响面', count(sent, /\/orders\/impact$/) > before, `${before} → ${count(sent, /\/orders\/impact$/)}`)
  await sleep(300)
  check('……没有模板引用的表保存后不说影响面', await savedHint.count() === 0)
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
  check('……清单上这张表移到「没有待确认项」', (await page.locator('[data-catalog-row="order_items"]').getAttribute('data-progress')) === 'done')

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

  // 码值的含义还空着（数据剖析给的候选只有取值）：各种确认都先说清确认的是取值清单，确认后照旧写「含义待填写」
  state.tables[S1].find((t) => t.name === 'visits').notes.columns.status.codes = item({ 1: '已入园', 9: '', 2: '' }, 'profile', 'proposed')
  await openDetail(page, S1, 'visits')
  const puts = () => count(sent, /^PUT |\/review$/)
  const put0 = puts()
  await page.locator('[data-catalog-confirm-all]').click()
  await confirmBox(page).waitFor()
  check('「确认本表全部推断」：码值含义还空着时后果里写明确认的是取值清单', (await consequences(page)).includes('有 2 个码值的含义待填写，确认的是取值清单'),
        (await consequences(page)).join(' | '))
  await confirmBox(page).getByRole('button', { name: '取消' }).click()
  await openMark(page, 'columns.status.codes')
  await panel(page).locator('[data-review-action="confirm"]').click()
  await confirmBox(page).waitFor()
  check('单项「确认」码值：同样先问', (await confirmBox(page).innerText()).includes('确认列 status 的码值？')
        && (await consequences(page)).includes('有 2 个码值的含义待填写，确认的是取值清单'), (await confirmBox(page).innerText()).replace(/\s+/g, ' '))
  await confirmBox(page).getByRole('button', { name: '取消' }).click()
  await page.locator('[data-confirm-column="status"]').click()
  await confirmBox(page).waitFor()
  const blankCons = await consequences(page)
  check('「确认本列推断」原本一键确认，码值含义还空着时先问：有 2 个码值的含义待填写，确认的是取值清单',
        (await confirmBox(page).innerText()).includes('确认列 status 的 2 项推断？') && blankCons.includes('有 2 个码值的含义待填写，确认的是取值清单')
        && blankCons.some((c) => c.includes('仍显示「含义待填写」')), blankCons.join(' | '))
  await confirmBox(page).getByRole('button', { name: '取消' }).click()
  check('……三处取消都没有提交', puts() === put0, String(puts() - put0))
  await page.locator('[data-confirm-column="status"]').click()
  await confirmBox(page).waitFor()
  await confirmBox(page).getByRole('button', { name: '确认 2 项' }).click()
  await until(async () => puts() === put0 + 1)
  const blankCell = page.locator('[data-column="status"] [data-cell="codes"]')
  check('……确认后码值记为已确认，空着的仍写「含义待填写」，还能填写', await until(async () => (await markStatus(page, 'columns.status.codes')) === 'confirmed')
        && await blankCell.locator('[data-code-pending]').count() === 2 && await blankCell.locator('[data-codes-fill]').count() === 1
        && JSON.stringify(lastBody(sent, /^PUT .*catalog\/visits$/)?.notes?.columns?.status?.codes?.value) === JSON.stringify({ 1: '已入园', 9: '', 2: '' }),
        (await blankCell.innerText()).replace(/\s+/g, ' '))
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
  const complete = page.locator('[data-column="status"] [data-input="codes-complete"]')
  check('码值下有「已列出全部取值」：原来没标的不勾，没有码值的列勾不了', !(await complete.isChecked())
        && (await page.locator('[data-column="status"] [data-cell="codes"]').innerText()).includes('已列出全部取值')
        && await page.locator('[data-column="visitor_count"] [data-input="codes-complete"]').isDisabled())
  await complete.check()
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
  // 关联关系：人工添加的才能删除；外键约束、命名推断得出的只能驳回（删掉会被起草和现推带回来）
  const PARK = relId(['park_id'], 'parks', ['id'])
  const GATE = relId(['gate_id'], 'gates', ['id'])
  const parkDraft = page.locator(`[data-relation-draft="${PARK}"]`)
  check('关联关系：外键约束、命名推断得出的不给「删除」只给「驳回」，新加的给「删除」，写明为什么',
        await parkDraft.locator('[data-relation-reject]').count() === 1 && await parkDraft.locator('[aria-label="删除这条关联关系"]').count() === 0
        && await page.locator(`[data-relation-draft="${GATE}"] [data-relation-reject]`).count() === 1
        && await draft.locator('[aria-label="删除这条关联关系"]').count() === 1 && await draft.locator('[data-relation-reject]').count() === 0
        && (await page.locator('[data-relation-remove-hint]').innerText()).includes('只能驳回'))
  await parkDraft.locator('[data-input="cardinality"]').selectOption('one_to_one')
  await parkDraft.locator('[data-relation-reject]').click()
  check('……点「驳回」：标「保存后驳回」，两端和基数回到原样、锁住不能改，可以撤销', (await parkDraft.getAttribute('data-rejecting')) === 'true'
        && (await parkDraft.innerText()).includes('保存后驳回') && (await parkDraft.locator('[data-input="cardinality"]').inputValue()) === 'many_to_one'
        && await parkDraft.locator('[data-input="columns"]').isDisabled() && await parkDraft.locator('[data-relation-undo-reject]').count() === 1)
  await parkDraft.locator('[data-relation-undo-reject]').click()
  check('……撤销驳回：回到原样，不算修改', (await parkDraft.getAttribute('data-rejecting')) === null
        && (await page.locator('[data-catalog-changes]').innerText()) === '7 处修改', await page.locator('[data-catalog-changes]').innerText())
  await page.locator(`[data-relation-draft="${GATE}"] [data-relation-reject]`).click()
  await shot(page, 'catalog-edit')
  check('保存栏写「8 处修改」（勾「已列出全部取值」、驳回一条关系各算一处）', (await page.locator('[data-catalog-changes]').innerText()) === '8 处修改', await page.locator('[data-catalog-changes]').innerText())
  await page.locator('[data-catalog-save]').click()
  await until(async () => count(sent, /^PUT /) === 1)
  const body = lastBody(sent, /^PUT /)
  const n = body?.notes ?? {}
  const old = visitsNotes()
  check('PUT 带读到的版本', body?.if_version === v, String(body?.if_version))
  check('……改过的项交新值：中文名、业务主键、码值、单位', n.label?.value === '入园流水' && JSON.stringify(n.keys?.value) === '["ticket_no","park_id"]'
        && JSON.stringify(n.columns?.status?.codes?.value) === JSON.stringify({ 1: '已入园', 2: '已退票', 9: '已作废' })
        && n.columns?.visitor_count?.unit?.value === '人次', JSON.stringify({ label: n.label, keys: n.keys }))
  check('……码值带上「已列出全部取值」', n.columns?.status?.codes?.complete === true, JSON.stringify(n.columns?.status?.codes))
  check('……清空的粒度不交（服务端删掉这一项）', !('grain' in n))
  check('……没动的项原样交回：来源、状态都不变（被驳回的照旧驳回）', JSON.stringify(n.dedup) === JSON.stringify(old.dedup)
        && JSON.stringify(n.description) === JSON.stringify(old.description) && JSON.stringify(n.columns?.park_id) === JSON.stringify(old.columns.park_id))
  const gateOld = old.relations.find((r) => r.id === GATE)
  check('……原有的关系原样交回，新的一条带两端和基数', old.relations.filter((r) => r.id !== GATE).every((r) => n.relations?.some((x) => JSON.stringify(x) === JSON.stringify(r)))
        && n.relations?.some((r) => r.to_table === 'members' && r.columns.join() === 'member_id' && r.to_columns.join() === 'id' && r.cardinality === 'many_to_one'))
  check('……驳回的那条原样交回、状态改为已驳回（等同单项驳回，不是删掉）',
        JSON.stringify(n.relations?.find((r) => r.id === GATE)) === JSON.stringify({ ...gateOld, status: 'rejected' }))
  check('保存后退出编辑，改过的项显示「已确认 · 人工填写」', await until(async () => (await detail(page).getAttribute('data-editing')) === 'false')
        && await markStatus(page, 'label') === 'confirmed' && (await page.locator('[data-field="label"]').innerText()).includes('人工填写'))
  check('……驳回的关系在列表里划掉、标已驳回', (await page.locator(`[data-relation="${GATE}"]`).getAttribute('data-status')) === 'rejected')
  check('……码值标「已列全」', await page.locator('[data-column="status"] [data-codes-complete-mark]').count() === 1
        && (await page.locator('[data-column="status"] [data-codes-complete-mark]').innerText()).includes('已列全'))
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
  check('有目录、没选表时：写审阅进度，给出使用最多、还有推断项的表', ovt.includes('共 152 张表：5 张有待确认项，3 张没有待确认项，144 张没有目录')
        && (await page.locator('[data-catalog-review-next]').getAttribute('data-catalog-review-next')) === 'visits', ovt.replace(/\s+/g, ' ').slice(0, 120))
  // 进度条的色段和那句话同一个先后（有待确认项、没有待确认项、没有目录），下面有图例说明每种颜色
  const segs = await page.locator('[data-catalog-overview] [data-progress-seg]').evaluateAll((els) => els.map((e) => e.getAttribute('data-progress-seg')))
  const legend = await page.locator('[data-catalog-progress-legend] [data-legend]').evaluateAll((els) => els.map((e) => [e.getAttribute('data-legend'), e.innerText.trim()]))
  check('进度条的色段按「有待确认项、没有待确认项、没有目录」排，和文字的先后一致', segs.join(',') === 'pending,done,none', segs.join(','))
  check('……下面有图例，先后和文字一致', legend.map(([k]) => k).join(',') === 'pending,done,none'
    && legend.map(([, t]) => t).join('|') === '有待确认项|没有待确认项|没有目录', JSON.stringify(legend))
  const swatch = await page.evaluate(() => [...document.querySelectorAll('[data-catalog-progress-legend] [data-legend]')].map((li) => {
    const key = li.getAttribute('data-legend')
    const seg = document.querySelector(`[data-progress-seg="${key}"]`)
    return getComputedStyle(li.querySelector('span')).backgroundColor === (seg ? getComputedStyle(seg).backgroundColor : '')
  }))
  check('……图例的色块和色段同色', swatch.length === 3 && swatch.every(Boolean), JSON.stringify(swatch))

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

// ---------------------------------------------------------------------------
// 数据剖析（阶段 4B）：剖析会对业务库发查询，这里一条都不能真的发出去——剖析接口和保存数据源都在浏览器层答掉，
// 照服务端（data/catalog_profile.py）的结论改假状态
// ---------------------------------------------------------------------------
const GATE_REL = relId(['gate_id'], 'gates', ['id'])
const MEMBER_REL = relId(['member_id'], 'members', ['id'])
const PROFILE_DAY = '2026-10-03'
const PROFILE_ON = { enabled: true, max_queries: 60, query_timeout_s: 10 }
const withProfile = (state, src, value) => {
  state.sources = state.sources.map((s) => (s.id === src ? { ...s, options: { ...s.options, catalog_profile: value } } : s))
}
const BUDGET_DETAIL = '本次剖析的查询次数已用完（上限 60 条），未检查'

/**
 * 剖析的默认答复：入园记录的闸机关系核实成已验证（覆盖率 98.7%），会员关系覆盖率不够保持推断，状态列的码值候选多出一个
 * 没有含义的取值 2；订单只剖析了一半就用完了查询次数，订单明细一条没查。ms 是服务端「执行」多久
 */
function profileReply(state, { ms = 0, extraCodes = false } = {}) {
  return async (route) => {
    if (ms) await sleep(ms)
    const visits = state.tables[S1].find((t) => t.name === 'visits')
    const notes = clone(visits.notes)
    const at = `${PROFILE_DAY}T03:00:00+00:00`
    // 剖析之前这两条关系的状态（服务端 previous_status）：第一次闸机关系是命名推断，再剖析一次时已经是已验证
    const before = (id) => visits.notes.relations.find((r) => r.id === id)?.status ?? null
    const previous = { gate: before(GATE_REL), member: before(MEMBER_REL) }
    notes.relations = notes.relations.map((r) => (r.id === GATE_REL ? {
      ...r, source: 'profile', status: 'verified', coverage: 0.987, cardinality: 'many_to_one', cardinality_checked: true, updated_at: at,
      note: '数据剖析：子表抽样 300 个不同键值，父表对上 296 个，覆盖率 98.7%；被指向列是主键；子表一侧有重复值，多对一。',
    } : r))
    notes.relations.push(rel(['member_id'], 'members', ['id'], 'profile', { coverage: 0.62, updated_at: at,
      note: '数据剖析：抽样覆盖率 62%，可能不是这条关系；子表抽样 200 个不同键值，父表对上 124 个；被指向列是主键。' }))
    notes.columns.status.codes = { ...item({ 1: '已入园', 9: '已作废', 2: '' }, 'profile', 'proposed'), updated_at: at }
    notes.columns.status.codes.note = '数据剖析：统计全表，182000 行非空值共 3 个取值：1（94%）、9（5%）、2（1%）。'
    // 统计的是全表：服务端给码值候选标「已列全」
    notes.columns.status.codes.complete = true
    // extraCodes：票种列也取到码值候选（两个取值，含义都空着）——报告里有两列要填，填完一列回到报告接着填下一列
    if (extraCodes) notes.columns.ticket_type_id = { codes: item({ 11: '', 12: '' }, 'profile', 'proposed') }
    writeEntry(visits, notes)
    const tables = [
      {
        table_name: 'visits', queries: 38, row_estimate: { rows: 182000, method: 'stats', at_least: null },
        findings: [
          { kind: 'relation', path: `relations.${GATE_REL}`, target: 'gate_id → gates.id', columns: ['gate_id'], to_table: 'gates', to_columns: ['id'],
            status: 'verified', confirmed: false, previous_status: previous.gate, coverage: 0.987, cardinality: 'many_to_one', cardinality_checked: true,
            sample: 300, matched: 296,
            summary: previous.gate === 'verified' ? '覆盖率 98.7%，仍为有确证' : '覆盖率 98.7%，升为有确证' },
          { kind: 'relation', path: `relations.${MEMBER_REL}`, target: 'member_id → members.id', columns: ['member_id'], to_table: 'members',
            to_columns: ['id'], status: 'proposed', confirmed: false, previous_status: previous.member, coverage: 0.62, cardinality: 'many_to_one',
            cardinality_checked: false, sample: 200, matched: 124,
            summary: '抽样覆盖率 62%，保持推断' },
          { kind: 'codes', path: 'columns.status.codes', column: 'status', values: [{ value: '1', rows: 171080 }, { value: '9', rows: 9100 },
            { value: '2', rows: 1820 }], rows: 182000, status: 'proposed', pending: 1, summary: '3 个取值' },
          ...(extraCodes ? [{ kind: 'codes', path: 'columns.ticket_type_id.codes', column: 'ticket_type_id', values: [{ value: '11', rows: 120000 },
            { value: '12', rows: 62000 }], rows: 182000, status: 'proposed', pending: 2, summary: '2 个取值' }] : []),
        ],
        skipped: [
          { kind: 'codes', target: 'member_level', path: 'columns.member_level.codes', reason: 'masked', detail: 'visits.member_level 在数据源设置中被遮罩，不取值' },
          { kind: 'date', target: 'visit_time', path: 'business_date', reason: 'too_large',
            detail: 'visits 的行数超过整表统计的行数上限（100000 行），不取日期列的最小值和最大值' },
        ],
        date_ranges: [], added: 1, updated: 2, removed: 0, version: visits.version, error: null,
      },
      {
        table_name: 'orders', queries: 22, row_estimate: { rows: null, method: 'count', at_least: 100001 }, findings: [],
        skipped: [{ kind: 'relation', target: 'member_id → members.id', path: 'relations.x', reason: 'budget', detail: BUDGET_DETAIL }],
        date_ranges: [], added: 0, updated: 0, removed: 0, version: 1, error: null,
      },
      {
        table_name: 'order_items', queries: 0, row_estimate: null, findings: [],
        skipped: [{ kind: 'table', target: 'order_items', path: null, reason: 'budget', detail: BUDGET_DETAIL }],
        date_ranges: [], added: 0, updated: 0, removed: 0, version: 1, error: null,
      },
    ]
    return json({
      profiled_at: at, actor: ACTOR, settings: { ...PROFILE_ON, sample_size: 2000, max_scan_rows: 100000, max_total_s: 120 },
      queries_used: 60, stopped: 'budget', tables, total: { added: 1, updated: 2, removed: 0 }, note: null,
      empty_tables: ['ext_log_001', 'ext_log_002'],
    })(route)
  }
}
const profileBox = (page) => page.locator('[data-profile-dialog]')
const profilePhase = (page) => profileBox(page).getAttribute('data-profile-dialog').catch(() => null)
/** 报告里不该露的机读码：停止原因、跳过原因、状态、基数的英文值 */
const PROFILE_CODE_RE = /\b(?:budget|deadline|failed|masked|too_large|high_cardinality|no_data|unsupported|verified|proposed|many_to_one|row_estimate)\b/

await section('数据目录 · 只探查了一部分表', async () => {
  // 探查结构每个数据源最多取 200 张表：多出来的不在清单里，助手也看不到。顶部要说出来，并给出下一步
  const state = freshState()
  const explored = state.tables[S1].filter((t) => !t.missing).length
  const total = explored + 57
  state.schemaTotal = { [S1]: total }
  const { page, sent, errors, natives, close } = await open(`/data/catalog/${S1}`, { handlers: catalogHandlers(state) })
  const bar = page.locator('[data-catalog-schema-partial]')
  check('顶部有一条说明', await bar.waitFor({ timeout: 3000 }).then(() => true, () => false))
  check('……数字：一共几张、探查了几张', (await bar.getAttribute('data-catalog-schema-partial')) === `${total},${explored}`,
    await bar.getAttribute('data-catalog-schema-partial'))
  const text = await bar.innerText().catch(() => '')
  check('……写成「共有 N 张表，只探查了前 M 张」', text.includes(`这个数据源共有 ${total} 张表，只探查了前 ${explored} 张`), text)
  check('……说明为什么只取了这些、其余的助手也看不到', text.includes('最多取 200 张表') && text.includes('其余 57 张不在数据目录中')
    && text.includes('助手也看不到'), text.replace(/\s+/g, ' '))
  check('……给出下一步：换 schema 或缩小范围后重新探查', text.includes('换一个 schema') && text.includes('缩小范围')
    && text.includes('探查结构'), text.replace(/\s+/g, ' '))
  await shot(page, 'catalog-schema-partial')
  await cleanCopy(page, '只探查了一部分表')
  await bar.locator('[data-catalog-schema-partial-go]').click()
  await page.waitForURL('**/data/databases', { timeout: 5000 }).catch(() => {})
  check('……按钮回到数据源卡片', new URL(page.url()).pathname === '/data/databases', page.url())
  // 没截断的库不出这一条
  await goto(page, `/data/catalog/${S4}`)
  await page.locator('[data-catalog-overview]').waitFor()
  check('没截断的库不出这一条', await page.locator('[data-catalog-schema-partial]').count() === 0)
  check('只读、不发写请求', writes(sent, /./).length === 0)
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
  await close()
})

await section('数据目录 · 数据剖析设置', async () => {
  const state = freshState()
  const replies = { patch: [] }
  const { page, sent, errors, natives, close } = await open('/data/databases', { handlers: catalogHandlers(state, replies) })
  // 手工登记的库：编辑框里有「数据剖析」一段，默认关闭，预算收着
  await page.locator('[data-source="zzcatscenic"]').getByRole('button', { name: '编辑', exact: true }).click()
  const box = page.locator('[data-profile-settings]')
  await box.waitFor()
  check('编辑框里有「数据剖析」，默认关闭、预算收着', (await box.getAttribute('data-profile-settings')) === 'off'
        && await box.locator('[data-profile-field]').count() === 0 && (await box.innerText()).includes('默认关闭'))
  await box.locator('[data-profile-enabled]').check()
  const labels = await box.locator('label.label').allInnerTexts()
  check('开启后五项预算展开，中文标签', labels.join('、') === '查询次数上限、单条查询时限、抽样键值数、整表统计行数上限、总时长上限', labels.join('、'))
  const units = await box.locator('[data-profile-field] span').allInnerTexts()
  check('……每项带单位', units.join(',') === '条,秒,个,行,秒', units.join(','))
  const hints = await box.locator('[id$="-hint"]').allInnerTexts()
  check('……写明范围和默认值，整表统计写明 0 的含义', hints.some((h) => h.startsWith('范围 1–500 条，默认 60'))
        && hints.some((h) => h.includes('范围 0–10,000,000 行') && h.includes('填 0 表示一律不做整表统计')), hints.join(' | '))
  const risk = () => box.locator('[data-profile-risk]').innerText().catch(() => '')
  check('……风险说明：会发出只读查询，每次最多 60 条、单条 10 秒（缺省）', (await risk()).includes('只读查询：最多 60 条，单条不超过 10 秒'), await risk())
  await page.locator('#ds-profile-max_queries').fill('80')
  check('……风险说明跟着填的数走', (await risk()).includes('最多 80 条'), await risk())
  await page.locator('#ds-profile-max_queries').fill('0')
  const err = await page.locator('#ds-profile-max_queries-error').innerText().catch(() => '')
  check('填错（0 条）：就地写明范围，保存按钮禁用', err === '请填写 1 到 500 之间的整数' && await page.locator('[data-source-save]').isDisabled(), err)
  check('……输入框标出错（aria-invalid）', (await page.locator('#ds-profile-max_queries').getAttribute('aria-invalid')) === 'true')
  await page.locator('#ds-profile-max_queries').fill('80')
  await page.locator('#ds-profile-query_timeout_s').fill('20')
  // 服务端按字段拒收（422）：报错落在那一项下面，光标放进去，不只弹提示
  const REJECT = '数据剖析的「单条查询时限（秒）」需要填写 1 到 60 之间的数；当前为「20」'
  // 服务端带机读码和出错的那一项（api/coded.py：code + field），界面按它落到那一格，不从原话里认
  replies.patch.push(json({ detail: REJECT, code: 'profile_settings_invalid', field: 'query_timeout_s' }, 422))
  await page.locator('[data-source-save]').click()
  check('保存被拒（422）：服务端原话写在「单条查询时限」下面', await until(async () =>
    (await page.locator('#ds-profile-query_timeout_s-error').innerText().catch(() => '')) === REJECT),
  await page.locator('#ds-profile-query_timeout_s-error').innerText().catch(() => ''))
  check('……光标落在那一项上，框标出错', await until(async () => page.evaluate(() => document.activeElement?.id === 'ds-profile-query_timeout_s'))
        && (await page.locator('#ds-profile-query_timeout_s').getAttribute('aria-invalid')) === 'true')
  check('……没有落到「高级连接参数」的查询时限上', await page.locator('#ds-adv-query_timeout_s-error').count() === 0)
  if (SHOTS) await sleep(350)
  await shot(page, 'catalog-profile-settings-422')
  await page.locator('#ds-profile-query_timeout_s').fill('15')
  check('改了这一项，报错收起', await page.locator('#ds-profile-query_timeout_s-error').count() === 0)
  await page.locator('[data-source-save]').click()
  await until(async () => (await page.locator('[data-profile-settings]').count()) === 0)
  const saved = writes(sent, /^PATCH /).at(-1)?.body
  check('保存：options 里写剖析设置（开关和填了的项，数字）', JSON.stringify(saved?.options?.catalog_profile)
        === JSON.stringify({ enabled: true, max_queries: 80, query_timeout_s: 15 }), JSON.stringify(saved?.options))
  check('……卡片上标「数据剖析已开启」，悬停写明预算', await until(async () => (await page.locator('[data-source="zzcatscenic"] [data-profile-on]').count()) === 1)
        && (await page.locator('[data-source="zzcatscenic"] [data-profile-on]').getAttribute('title')) === '数据剖析已开启：每次最多 80 条只读查询，单条不超过 15 秒')
  // 再打开：剖析设置回显，没有摊进「高级连接参数」变成一串文字
  await page.locator('[data-source="zzcatscenic"]').getByRole('button', { name: '编辑', exact: true }).click()
  await page.locator('[data-profile-settings="on"]').waitFor()
  check('再打开编辑框：开关和填的数回显', await page.locator('#ds-profile-max_queries').inputValue() === '80'
        && await page.locator('#ds-profile-query_timeout_s').inputValue() === '15')
  const advanced = await page.locator('[data-profile-settings] ~ details, details').allInnerTexts()
  check('……剖析设置没有摊进「高级连接参数」', !advanced.join(' ').includes('catalog_profile') && !advanced.join(' ').includes('[object Object]'))
  await page.getByRole('button', { name: '取消', exact: true }).click()

  // 导入表格的源没有编辑框：卡片上单独设置，options 的其余键原样带上
  withProfile(state, S2, undefined)
  state.sources = state.sources.map((s) => (s.id === S2 ? { ...s, options: { mask_columns: ['联系电话'] } } : s))
  await goto(page, '/data/tables')
  await page.locator('[data-source="zzcatupload"] [data-edit-profile]').click()
  const dialog = page.locator('[data-profile-settings-dialog]')
  await dialog.waitFor()
  check('导入表格的源：卡片上「设置数据剖析」打开只改这一项的弹窗', (await page.locator('[role="dialog"]').last().innerText()).includes('「zzcatupload」的数据剖析设置'))
  check('……没改时保存禁用', await page.locator('[data-profile-save]').isDisabled())
  await dialog.locator('[data-profile-enabled]').check()
  await page.locator('[data-profile-save]').click()
  await until(async () => (await dialog.count()) === 0)
  const body = writes(sent, /^PATCH \/datasources\/check-cat-upload$/).at(-1)?.body
  check('……保存只换剖析设置，遮罩列原样带上', JSON.stringify(body) === JSON.stringify({ options: { mask_columns: ['联系电话'], catalog_profile: { enabled: true } } }),
        JSON.stringify(body))
  check('……卡片上标「数据剖析已开启」', await until(async () => (await page.locator('[data-source="zzcatupload"] [data-profile-on]').count()) === 1))
  check('写请求只有保存数据源', writes(sent, /./).every((s) => /^PATCH \/datasources\/check-cat-/.test(s.key)),
        JSON.stringify(writes(sent, /./).map((s) => s.key)))
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('数据目录 · 数据剖析', async () => {
  const state = freshState()
  withProfile(state, S1, PROFILE_ON)
  const replies = { profile: [] }
  const { page, sent, errors, natives, close } = await open(`/data/catalog/${S1}`, { handlers: catalogHandlers(state, replies) })
  await page.locator('[data-catalog-row]').first().waitFor()
  const head = page.locator('[data-catalog-profile]')
  check('页头有「数据剖析」', (await head.getAttribute('aria-label')) === '数据剖析')
  await head.click()
  await profileBox(page).waitFor()
  const setup = await profileBox(page).innerText()
  check('没选表、没筛选：范围只有「使用次数最多、有待核实关联关系的表」，写明至多 10 张',
        (await profileBox(page).locator('[data-profile-scope]').evaluateAll((els) => els.map((e) => e.getAttribute('data-profile-scope')))).join(',') === 'default'
        && setup.includes('使用次数最多、有待核实关联关系的表') && setup.includes('至多 10 张'))
  const budget = await profileBox(page).locator('[data-profile-budget]').innerText()
  check('确认前写明预算：查询条数、单条和总时长、抽样规模、整表统计门槛，按哪个源的设置',
        ['最多 60 条只读查询，单条不超过 10 秒', '总时长不超过 120 秒', '抽样 2,000 个键值', '行数超过 100,000 的表不做整表统计', '按「zzcatscenic」的数据剖析设置']
          .every((s) => budget.includes(s)), budget.replace(/\s+/g, ' '))
  check('……写明开始后无法中途停止', setup.includes('开始后无法中途停止'))
  check('……还没开始就没有写请求', writes(sent, /./).length === 0)
  if (SHOTS) await sleep(350)
  await shot(page, 'catalog-profile-setup')
  await page.keyboard.press('Escape')
  await until(async () => (await profileBox(page).count()) === 0)

  // 打开入园记录的详情，选两张表剖析：完成后清单和正在看的详情都要重新载入
  await openDetail(page, S1, 'visits')
  const v0 = await version(page)
  check('剖析前：闸机关系是「推断 · 命名推断」，没有覆盖率', (await markStatus(page, `relations.${GATE_REL}`)) === 'proposed'
        && (await page.locator(`[data-relation="${GATE_REL}"]`).innerText()).includes('—'))
  await page.locator('[data-catalog-select="visits"]').click()
  await page.locator('[data-catalog-select="orders"]').click()
  await page.locator('[data-catalog-profile-selected]').click()
  await profileBox(page).waitFor()
  check('剖析所选：范围默认是「已选的 2 张表」', await profileBox(page).locator('[data-profile-scope="selected"] input').isChecked()
        && (await profileBox(page).locator('[data-profile-scope="selected"]').innerText()).includes('已选的 2 张表'))
  replies.profile.push(profileReply(state, { ms: 2600 }))
  const lists0 = count(sent, /^GET \/datasources\/check-cat-scenic\/catalog$/)
  await page.locator('[data-profile-start]').click()
  check('进行中：写「正在剖析…」和已用时长、总时长上限', await until(async () => (await profilePhase(page)) === 'running')
        && (await profileBox(page).locator('[data-profile-title]').innerText()) === '正在剖析…'
        && (await profileBox(page).locator('[data-profile-elapsed]').innerText()).includes('（总时长上限 2 分 00 秒）'))
  check('……时长在走（不是一直「不到 1 秒」）', await until(async () => Number(await profileBox(page).locator('[data-profile-elapsed]').getAttribute('data-profile-elapsed')) >= 1, 2500))
  check('……不给「停止」，明说无法中途停止', await profileBox(page).getByRole('button', { name: /停止|取消/ }).count() === 0
        && (await profileBox(page).locator('[data-profile-no-cancel]').innerText()).includes('无法中途停止'))
  const req = writes(sent, /catalog\/profile$/).at(-1)
  check('……请求带所选的两张表和本机署名', JSON.stringify([...(req?.body?.tables ?? [])].sort()) === JSON.stringify(['orders', 'visits'])
        && decodeURIComponent(req?.actor ?? '') === ACTOR, JSON.stringify(req?.body))
  if (SHOTS) await sleep(350)
  await shot(page, 'catalog-profile-running')
  // 关掉窗口剖析照常进行：页头换成计时，完成后提示并可以打开报告
  await page.locator('[data-profile-background]').click()
  check('「在后台继续」：窗口关掉，页头换成「正在剖析」', await until(async () => (await profileBox(page).count()) === 0)
        && (await page.locator('[data-catalog-profile="running"]').innerText()).includes('正在剖析'))
  const toastBox = page.locator('[data-toast]').filter({ hasText: '数据剖析已停止' })
  check('完成：提示「数据剖析已停止：查询次数已用完」，带「查看报告」', await until(async () => (await toastBox.count()) > 0, 6000),
        await page.locator('[data-toast]').allInnerTexts().then((t) => t.join(' | ')).catch(() => ''))
  check('……页头回到「数据剖析」', await page.locator('[data-catalog-profile="running"]').count() === 0)
  check('……写进去的表重新载入：清单重取、正在看的详情版本更新', await until(async () => count(sent, /^GET \/datasources\/check-cat-scenic\/catalog$/) > lists0)
        && await until(async () => (await version(page)) === v0 + 1))
  await toastBox.getByRole('button', { name: '查看报告' }).click()
  await profileBox(page).waitFor()
  const report = profileBox(page).locator('[data-profile-report]')
  check('报告：停止原因说中文', (await report.locator('[data-profile-title]').innerText()) === '剖析已停止：查询次数已用完')
  check('……用了多少条查询、剖析几张表', (await report.locator('[data-profile-summary]').innerText()).startsWith('用了 60 / 60 条查询，剖析 3 张表'))
  check('……目录的变化', (await report.locator('[data-profile-changes]').innerText()) === '新增 1 项、更新 2 项')
  check('……停下之后怎么办：调高查询次数上限或缩小范围，给「修改设置」', (await report.locator('[data-profile-stopped="budget"]').innerText()).includes('调高查询次数上限')
        && await report.locator('[data-profile-stopped] [data-profile-settings-open]').count() === 1)
  const visitsCard = report.locator('[data-profile-table="visits"]')
  const gate = visitsCard.locator(`[data-profile-finding="relation"][data-path="relations.${GATE_REL}"]`)
  check('关系核实：覆盖率、基数、抽样规模，升为已验证', (await gate.locator('[data-profile-coverage]').innerText()) === '覆盖率 98.7%'
        && (await gate.locator('[data-profile-cardinality]').innerText()) === '多对一（已用数据核实）' && (await gate.innerText()).includes('抽样 300 个键值，对上 296 个')
        && (await gate.locator('[data-profile-outcome]').innerText()) === '升为已验证' && (await gate.getAttribute('data-status')) === 'verified',
        (await gate.innerText()).replace(/\s+/g, ' '))
  // 结论是状态说明，不是链接：带框的小标签，文字用次要文字色（不是强调蓝），不能点
  const outcomeLook = await gate.locator('[data-profile-outcome]').evaluate((el) => {
    const probe = document.createElement('span')
    probe.style.color = 'var(--accent)'
    document.body.append(probe)
    const accent = getComputedStyle(probe).color
    probe.style.color = 'var(--text-dim)'
    const dim = getComputedStyle(probe).color
    probe.remove()
    const cs = getComputedStyle(el)
    return { tag: el.tagName, color: cs.color, accent, dim, border: cs.borderTopStyle, cursor: cs.cursor, kind: el.getAttribute('data-profile-outcome'),
      clickable: !!el.closest('a, button') }
  })
  check('……结论用状态标签的样子：带框、次要文字色，不是蓝色正文、不像链接', outcomeLook.kind === 'raised' && outcomeLook.color === outcomeLook.dim
    && outcomeLook.color !== outcomeLook.accent && outcomeLook.border === 'solid' && outcomeLook.cursor !== 'pointer' && !outcomeLook.clickable,
    JSON.stringify(outcomeLook))
  // 结构化的读数：基数核实读 cardinality_checked，几个含义待填写读 pending（假答复的 summary 里故意不写），空表读 empty_tables
  check('……基数用数据核实过没有，读 cardinality_checked', (await gate.locator('[data-profile-cardinality]').getAttribute('data-checked')) === 'true'
        && (await visitsCard.locator(`[data-path="relations.${MEMBER_REL}"] [data-profile-cardinality]`).innerText()) === '多对一（子表一侧未核实，按表结构推断）')
  check('……挑表时跳过的空表读 empty_tables', (await report.locator('[data-profile-empty-tables="2"]').innerText().catch(() => ''))
        === '挑表时跳过 2 张空表：ext_log_001、ext_log_002')
  const member = visitsCard.locator(`[data-profile-finding="relation"][data-path="relations.${MEMBER_REL}"]`)
  check('……覆盖率不够的保持推断，写明原因', (await member.locator('[data-profile-outcome]').innerText()) === '保持推断：覆盖率不足，可能不是这条关系'
        && (await member.locator('[data-profile-coverage]').innerText()) === '覆盖率 62%')
  const codes = visitsCard.locator('[data-profile-finding="codes"]')
  check('码值候选：列、各取值的行数、几个含义待填写，给「填写含义」', (await codes.getAttribute('data-column')) === 'status'
        && (await codes.innerText()).includes('3 个取值，共 182,000 行') && (await codes.innerText()).includes('171,080 行')
        && (await codes.locator('[data-codes-pending]').innerText()) === '1 个含义待填写' && await codes.locator('[data-profile-fill="status"]').count() === 1,
        (await codes.innerText()).replace(/\s+/g, ' '))
  const skipped = visitsCard.locator('[data-profile-skipped]')
  check('跳过的项：原因说中文，附服务端的整句', (await skipped.innerText()).includes('列已遮罩') && (await skipped.innerText()).includes('visits.member_level 在数据源设置中被遮罩，不取值')
        && (await skipped.innerText()).includes('表太大'), (await skipped.innerText()).replace(/\s+/g, ' '))
  check('……行数估算写成人话：统计信息约 182,000 行、数到上限的超过 100,000 行',
        (await visitsCard.locator('header').innerText()).includes('约 182,000 行') && (await report.locator('[data-profile-table="orders"] header').innerText()).includes('超过 100,000 行'))
  check('……一条没查的表写明「查询次数用完」', (await report.locator('[data-profile-table="order_items"] [data-profile-skip="budget"]').innerText()).includes('查询次数用完'))
  const reportText = await report.innerText()
  check('报告不露机读码', !PROFILE_CODE_RE.test(reportText), reportText.match(PROFILE_CODE_RE)?.[0] ?? '')
  if (SHOTS) await sleep(350)
  await shot(page, 'catalog-profile-report')
  await page.locator('[data-profile-close]').click()
  await until(async () => (await profileBox(page).count()) === 0)

  // 表详情：关系的来源写「数据剖析」，带覆盖率和基数；码值的含义空着写「含义待填写」
  const gateRow = page.locator(`[data-relation="${GATE_REL}"]`)
  check('表详情：闸机关系「已验证 · 数据剖析」，覆盖率 98.7%、多对一', (await markStatus(page, `relations.${GATE_REL}`)) === 'verified'
        && (await gateRow.innerText()).includes('数据剖析') && (await gateRow.innerText()).includes('98.7%') && (await gateRow.innerText()).includes('多对一'),
        (await gateRow.innerText().catch(() => '')).replace(/\s+/g, ' '))
  await openMark(page, `relations.${GATE_REL}`)
  check('……点开来源：写明抽样规模和覆盖率的剖析说明', (await panel(page).innerText()).includes('子表抽样 300 个不同键值，父表对上 296 个'))
  // 说明里不带日期：时间看这一项的 updated_at（按本地时间，2026-10-03 03:00 UTC 是 11:00）
  check('……说明不带日期，时间按这一项的 updated_at 写', !/\d{4}-\d{2}-\d{2}/.test(await panel(page).locator('.whitespace-pre-wrap').innerText())
        && /更新于 (2026\/)?10\/3 11:00/.test(await panel(page).innerText()), (await panel(page).innerText()).replace(/\s+/g, ' '))
  check('……关系表上写基数已用数据核实', await page.locator(`[data-relation="${GATE_REL}"] [data-relation-cardinality][data-checked="true"]`).count() === 1
        && (await page.locator(`[data-relation="${GATE_REL}"] [data-relation-cardinality]`).innerText()).includes('已核实'))
  await page.keyboard.press('Escape')
  const statusCodes = page.locator('[data-column="status"] [data-cell="codes"]')
  check('……码值的含义空着写「含义待填写」，给「填写含义」', (await statusCodes.locator('[data-code="2"] [data-code-pending]').innerText()) === '含义待填写'
        && (await statusCodes.locator('[data-codes-fill]').innerText()).includes('填写含义'))
  // 从报告点「填写含义」：窗口关掉，打开这张表那一列的码值弹窗，光标在空着的那一个上
  await page.locator('[data-catalog-profile]').click()
  await profileBox(page).waitFor()
  check('页头再点：上一次的报告看过了就回到选范围', (await profilePhase(page)) === 'setup')
  await page.keyboard.press('Escape')
  await statusCodes.locator('[data-codes-fill]').click()
  const codesBox = page.locator('[data-codes-dialog="status"]')
  await codesBox.waitFor()
  check('码值弹窗：每个码值一个输入框，空着的写「含义待填写」，光标在它上面', await codesBox.locator('input[data-code]').count() === 3
        && (await codesBox.locator('input[data-code="2"]').getAttribute('placeholder')) === '含义待填写'
        && await until(async () => page.evaluate(() => document.activeElement?.getAttribute('data-code') === '2')))
  check('……写明剖析说明（各取值的占比），时间按这一项的 updated_at', (await codesBox.innerText()).includes('1（94%）、9（5%）、2（1%）')
        && /^（(2026\/)?10\/3 11:00）$/.test(await codesBox.locator('[data-codes-note-at]').innerText().catch(() => '')),
        await codesBox.locator('[data-codes-note]').innerText().catch(() => ''))
  check('……没填时保存禁用', await page.locator('[data-codes-save]').isDisabled())
  check('……写明当前已列出全部取值（剖析统计了全表），可以取消', await codesBox.locator('[data-codes-complete]').isChecked()
        && (await codesBox.innerText()).includes('已列出全部取值') && (await statusCodes.locator('[data-codes-complete-mark]').count()) === 1)
  await codesBox.locator('[data-codes-complete]').uncheck()
  check('……只改勾选也能保存', !(await page.locator('[data-codes-save]').isDisabled()))
  await codesBox.locator('[data-codes-complete]').check()
  await codesBox.locator('input[data-code="2"]').fill('已退票')
  if (SHOTS) await sleep(350)
  await shot(page, 'catalog-codes-fill')
  const v1 = await version(page)
  await page.keyboard.press('Enter')
  check('回车保存：整份提交，只换这一列码值的含义，带读到的版本', await until(async () => (await codesBox.count()) === 0)
        && JSON.stringify(lastBody(sent, /^PUT .*catalog\/visits$/)?.notes?.columns?.status?.codes?.value) === JSON.stringify({ 1: '已入园', 9: '已作废', 2: '已退票' })
        && lastBody(sent, /^PUT .*catalog\/visits$/)?.notes?.columns?.status?.codes?.complete === true
        && lastBody(sent, /^PUT .*catalog\/visits$/)?.if_version === v1, JSON.stringify(lastBody(sent, /^PUT .*catalog\/visits$/)?.notes?.columns?.status))
  check('……保存后不再有「含义待填写」，这一项记为人工确认', await until(async () => (await statusCodes.locator('[data-code-pending]').count()) === 0)
        && (await statusCodes.innerText()).includes('已退票') && (await markStatus(page, 'columns.status.codes')) === 'confirmed')
  // 报告里的「填写含义」直达
  await page.locator('[data-catalog-profile]').click()
  replies.profile.push(profileReply(state, { extraCodes: true }))
  await page.locator('[data-profile-start]').click()
  await until(async () => (await profilePhase(page)) === 'done')
  check('窗口开着时完成：直接看报告，不另弹提示', (await toastBox.count()) <= 1)
  // 重新剖析：闸机关系上一次已经核实成已验证，这次照样核实——写「仍为已验证」，不说「升为已验证」
  const gateAgain = profileBox(page).locator(`[data-profile-finding="relation"][data-path="relations.${GATE_REL}"] [data-profile-outcome]`)
  check('重新剖析：原本就是已验证的关系写「仍为已验证，覆盖率 98.7%」，不说「升为」', (await gateAgain.innerText()) === '仍为已验证，覆盖率 98.7%'
        && (await gateAgain.getAttribute('data-profile-outcome')) === 'same', await gateAgain.innerText())
  const memberAgain = profileBox(page).locator(`[data-profile-finding="relation"][data-path="relations.${MEMBER_REL}"] [data-profile-outcome]`)
  check('……原本就是推断、这次仍不够的写「保持推断」', (await memberAgain.innerText()) === '保持推断：覆盖率不足，可能不是这条关系'
        && (await memberAgain.getAttribute('data-profile-outcome')) === 'kept')
  // 点「填写含义」：报告收起、弹出那一列的码值；保存或取消之后回到报告，这一列改写成「含义已填写」，焦点落在下一列上
  const fillLine = (col) => profileBox(page).locator(`[data-profile-finding="codes"][data-column="${col}"]`)
  await fillLine('status').locator('[data-profile-fill]').click()
  const fillBox = page.locator('[data-codes-dialog="status"]')
  check('报告里点「填写含义」：报告收起，弹出这一列的码值', await until(async () => (await fillBox.count()) === 1)
        && (await profileBox(page).count()) === 0)
  await fillBox.locator('input[data-code="2"]').fill('已退票')
  await page.keyboard.press('Enter')
  check('填完保存：回到报告（不用重新剖析）', await until(async () => (await profilePhase(page)) === 'done') && (await fillBox.count()) === 0
        && count(sent, /catalog\/profile$/) === 2, String(await profilePhase(page)))
  const statusLine = fillLine('status')
  check('……刚填的这一列写「含义已填写」，按钮改成「修改含义」', (await statusLine.locator('[data-codes-filled]').innerText().catch(() => '')).includes('含义已填写')
        && await statusLine.locator('[data-codes-pending]').count() === 0 && (await statusLine.locator('[data-profile-fill]').innerText()) === '修改含义',
        (await statusLine.innerText().catch(() => '')).replace(/\s+/g, ' '))
  check('……还没填的下一列照旧写待填写，焦点落在它的「填写含义」上', (await fillLine('ticket_type_id').locator('[data-codes-pending]').innerText()) === '2 个含义待填写'
        && await until(async () => page.evaluate(() => document.activeElement?.getAttribute('data-profile-fill') === 'ticket_type_id'), 2000),
        await page.evaluate(() => document.activeElement?.outerHTML?.slice(0, 120) ?? ''))
  if (SHOTS) await sleep(350)
  await shot(page, 'catalog-profile-report-filled')
  await page.keyboard.press('Enter')
  const nextBox = page.locator('[data-codes-dialog="ticket_type_id"]')
  check('……回车接着填下一列', await until(async () => (await nextBox.count()) === 1))
  await page.keyboard.press('Escape')
  check('码值弹窗取消：同样回到报告，那一列仍写待填写', await until(async () => (await profilePhase(page)) === 'done') && (await nextBox.count()) === 0
        && (await fillLine('ticket_type_id').locator('[data-codes-pending]').innerText()) === '2 个含义待填写')
  await page.locator('[data-profile-close]').click()
  await until(async () => (await profileBox(page).count()) === 0)
  await openDetail(page, S1, 'orders')
  await page.locator('[data-catalog-profile]').click()
  await profileBox(page).waitFor()
  check('换了一张表、再点页头：回到选范围（报告已看过）', (await profilePhase(page)) === 'setup')
  await page.keyboard.press('Escape')

  check('写请求只有剖析和整份提交', writes(sent, /./).every((s) => /catalog\/profile$|^PUT .*catalog\/visits$/.test(s.key)),
        JSON.stringify(writes(sent, /./).map((s) => s.key)))
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('数据目录 · 数据剖析开始不了（409）', async () => {
  const state = freshState()
  const replies = { profile: [], patch: [] }
  const { page, sent, errors, natives, close } = await open(`/data/catalog/${S1}`, { handlers: catalogHandlers(state, replies) })
  await page.locator('[data-catalog-row]').first().waitFor()
  // 数据源列表里就没开：不让开始，就地开启
  await page.locator('[data-catalog-profile]').click()
  await profileBox(page).waitFor()
  check('没开启：写明需要先开启，「开始剖析」禁用，没有预算', (await profileBox(page).locator('[data-profile-disabled]').innerText()).includes('未开启数据剖析')
        && await page.locator('[data-profile-start]').isDisabled() && await profileBox(page).locator('[data-profile-budget]').count() === 0)
  await profileBox(page).locator('[data-profile-disabled] [data-profile-enable]').click()
  const settings = page.locator('[data-profile-settings-dialog]')
  await settings.waitFor()
  await settings.locator('[data-profile-enabled]').check()
  await page.locator(`#profile-${S1}-max_queries`).fill('40')
  // 保存被拒：只认机读码和 field。没带机读码的（老服务端）不从原话里猜是哪一项，只弹通用提示，弹窗留着
  const QUERIES_REJECT = '数据剖析的「查询次数上限」需要填写 1 到 500 之间的整数；当前为「40」'
  replies.patch.push(json({ detail: QUERIES_REJECT }, 422))
  await page.locator('[data-profile-save]').click()
  check('422 没带机读码：不从原话里猜是哪一项，只弹通用提示，弹窗留着',
    await until(async () => (await page.locator('[data-toast]').filter({ hasText: '查询次数上限' }).count()) > 0)
    && await settings.locator('[aria-invalid="true"]').count() === 0 && await settings.count() === 1)
  replies.patch.push(json({ detail: QUERIES_REJECT, code: 'profile_settings_invalid', field: 'max_queries' }, 422))
  await page.locator('[data-profile-save]').click()
  check('422 带机读码和 field：报错落在「查询次数上限」那一格，光标放进去',
    await until(async () => (await page.locator(`#profile-${S1}-max_queries`).getAttribute('aria-invalid')) === 'true')
    && await until(async () => page.evaluate((id) => document.activeElement?.id === id, `profile-${S1}-max_queries`)))
  replies.patch.push(json({ detail: '数据剖析设置中有无法识别的项「rows」，请删除后重试', code: 'profile_settings_invalid' }, 422))
  await page.locator('[data-profile-save]').click()
  check('422 带机读码、没有 field：报错写在剖析设置这一节上', await until(async () =>
    (await settings.locator('[data-profile-error="section"]').innerText().catch(() => '')).includes('无法识别的项')))
  await page.locator('[data-profile-save]').click()
  check('就地开启：保存后回到剖析，预算按刚存的设置', await until(async () => (await settings.count()) === 0)
        && await until(async () => (await profileBox(page).locator('[data-profile-budget]').innerText().catch(() => '')).includes('最多 40 条只读查询'))
        && !(await page.locator('[data-profile-start]').isDisabled()))
  check('……保存的是这个源的剖析设置', JSON.stringify(writes(sent, /^PATCH /).at(-1)?.body) === JSON.stringify({ options: { catalog_profile: { enabled: true, max_queries: 40 } } }),
        JSON.stringify(writes(sent, /^PATCH /).at(-1)?.body))

  // 服务端的 409 都带机读码（api/coded.py），界面按它分五种情况，不从原话里认
  const cases = [
    ['disabled', '数据源「zzcatscenic」未开启数据剖析。剖析会对业务库发查询，请先在数据源设置中开启数据剖析', '未开启数据剖析', 'data-profile-enable', 'profile_disabled'],
    ['inactive', '数据源「zzcatscenic」已停用，无法进行数据剖析。请先在数据源设置中启用', '数据源已停用', 'data-profile-go-source', 'datasource_inactive'],
    ['noSchema', '尚未探查结构，无法进行数据剖析。请先在数据源卡片上点「探查结构」', '还没有表结构', 'data-profile-go-source', 'schema_missing'],
    ['busy', '数据源「zzcatscenic」正在进行数据剖析，请等待完成后再试', '这个数据源正在剖析', 'data-profile-retry', 'profile_busy'],
    ['tampered', '数据文件与登记的版本不一致，可能被修改过，已拒绝查询', '数据文件核对未通过', 'data-profile-go-source', 'snapshot_tampered'],
  ]
  // 没带机读码的 409（老服务端）：原话里明明写着「正在进行数据剖析」，也不去猜，按「无法开始剖析」给重试
  replies.profile.push(json({ detail: cases[3][1] }, 409))
  await page.locator('[data-profile-start]').click()
  check('409 没带机读码：不从原话里猜，按「无法开始剖析」说，给重试', await until(async () =>
    (await profileBox(page).locator('[data-profile-blocked="other"]').count()) === 1)
    && await profileBox(page).locator('[data-profile-blocked="other"] [data-profile-retry]').count() === 1)
  await page.locator('[data-profile-back]').click()
  await until(async () => (await profilePhase(page)) === 'setup')
  for (const [kind, detail, title, action, code] of cases) {
    replies.profile.push(json({ detail, code }, 409))
    await page.locator('[data-profile-start]').click()
    const blocked = profileBox(page).locator(`[data-profile-blocked="${kind}"]`)
    const ok = await until(async () => (await blocked.count()) === 1)
    const text = ok ? await blocked.innerText() : ''
    check(`409（${title}）：写明是哪种情况、服务端原话和下一步，给对应的按钮`, ok && text.includes(title) && text.includes(detail)
          && await blocked.locator(`[${action}]`).count() === 1, text.replace(/\s+/g, ' ').slice(0, 140))
    if (kind === 'busy') {
      // 稍后重试：同一个范围再发一次，这次成功
      replies.profile.push(profileReply(state))
      await blocked.locator('[data-profile-retry]').click()
      check('……「重试」再发一次，成功后看到报告', await until(async () => (await profilePhase(page)) === 'done'))
    }
    if (kind === 'disabled') {
      // 服务端说没开（列表里的设置过时了）：同样就地开启，开了回到选范围
      await blocked.locator('[data-profile-enable]').click()
      await settings.waitFor()
      await page.locator(`#profile-${S1}-max_queries`).fill('45')
      await page.locator('[data-profile-save]').click()
      check('……就地开启后回到选范围', await until(async () => (await profilePhase(page)) === 'setup'))
      continue
    }
    // 底栏的按钮不在弹窗正文里
    await page.locator(kind === 'busy' ? '[data-profile-again]' : '[data-profile-back]').click()
    await until(async () => (await profilePhase(page)) === 'setup')
  }
  if (SHOTS) await sleep(350)
  await shot(page, 'catalog-profile-blocked')
  // 不是 409 的失败：照常的报错，能重试
  replies.profile.push(json({ detail: '服务端处理剖析时出错，请稍后重试' }, 500))
  await page.locator('[data-profile-start]').click()
  check('其余失败：写「剖析未能完成」和原因，能重试', await until(async () => (await profileBox(page).locator('[data-profile-failed]').count()) === 1)
        && (await profileBox(page).locator('[data-profile-failed]').innerText()).includes('剖析未能完成'))
  // 去数据源：停用、没有表结构、快照被改动时
  replies.profile.push(json({ detail: cases[1][1], code: cases[1][4] }, 409))
  await profileBox(page).locator('[data-profile-failed]').getByRole('button', { name: /重试/ }).click()
  await profileBox(page).locator('[data-profile-go-source]').click()
  await page.waitForURL('**/data/databases', { timeout: 5000 }).catch(() => {})
  check('「前往数据源」回到数据源卡片', new URL(page.url()).pathname === '/data/databases', page.url())
  check('写请求只有剖析和保存剖析设置', writes(sent, /./).every((s) => /catalog\/profile$|^PATCH \/datasources\/check-cat-scenic$/.test(s.key)),
        JSON.stringify(writes(sent, /./).map((s) => s.key)))
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
  // 页头放不下说明：只留标题，不能被挤成一个字的宽度、在标题后面露出说明的头一个字
  const headSub = await page.locator('[data-catalog-page] > header p').count()
  check('……页头只有标题，不露说明的残字', headSub === 0 && !(await page.locator('[data-catalog-page] > header').innerText()).includes('先起草'),
    await page.locator('[data-catalog-page] > header').innerText())
  await page.locator('[data-catalog-row="visits"] [data-catalog-open-row]').click()
  await until(async () => (await page.locator('[data-catalog-detail="visits"][data-version]').count()) === 1)
  check('……打开一张表：只显示详情，有「返回表清单」', !(await page.locator('[data-catalog-list]').isVisible())
        && await page.locator('[data-catalog-detail-back]').isVisible())
  check('……详情没有横向滚动', await overflow() <= 1, String(await overflow()))
  // 列表格、关联关系不再是 860px / 640px 宽、要横着滚的表格：排成卡片，状态标识始终在框里看得到
  const fits = (sel) => page.evaluate((q) => {
    const box = document.querySelector(q)
    if (!box) return null
    const r = box.getBoundingClientRect()
    const marks = [...box.querySelectorAll('[data-item-mark]')].map((m) => m.getBoundingClientRect())
    return { scroll: box.scrollWidth - box.clientWidth, width: Math.round(r.width), marks: marks.length,
      inside: marks.every((m) => m.width > 0 && m.left >= r.left - 1 && m.right <= r.right + 1),
      head: getComputedStyle(box.querySelector('thead')).display }
  }, sel)
  const cols = await fits('[data-catalog-columns]')
  check('……列表格排成卡片：不横向滚动，表头收起，每个状态标识都在框里', !!cols && cols.scroll <= 1 && cols.head === 'none' && cols.marks > 0 && cols.inside,
    JSON.stringify(cols))
  const codesBox = await page.locator('[data-column="status"] [data-cell="codes"]').boundingBox()
  check('……码值那一项在屏幕里，前面写着字段名', !!codesBox && codesBox.x >= 0 && codesBox.x + codesBox.width <= 390
    && (await page.locator('[data-column="status"] [data-cell="codes"]').innerText()).startsWith('码值'), JSON.stringify(codesBox))
  await page.locator('[data-column="visitor_count"]').scrollIntoViewIfNeeded()
  if (SHOTS) await sleep(200)
  await shot(page, 'catalog-narrow-columns')
  const rels = await fits('[data-catalog-relations]')
  check('……关联关系也排成卡片：不横向滚动，三条关系的状态都看得到', !!rels && rels.scroll <= 1 && rels.head === 'none' && rels.marks === 3 && rels.inside,
    JSON.stringify(rels))
  await page.locator('[data-catalog-section="relations"]').scrollIntoViewIfNeeded()
  if (SHOTS) await sleep(200)
  await shot(page, 'catalog-narrow-relations')
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
  // 数据剖析：页头只留图标，弹窗在视口里、不横向滚动
  await page.locator('[data-catalog-profile]').click()
  await profileBox(page).waitFor()
  const dlg = await page.locator('[role="dialog"]').last().boundingBox()
  const inner = await profileBox(page).evaluate((el) => el.scrollWidth - el.clientWidth)
  check('……数据剖析弹窗在视口里，正文不横向滚动', !!dlg && dlg.x >= 0 && dlg.x + dlg.width <= 390 && inner <= 1, `${JSON.stringify(dlg)} ${inner}`)
  await page.keyboard.press('Escape')
  // 起草：设置阶段、进行中（宽表的段进度、长原因）弹窗都在视口里、不横向滚动；关掉以后上方那条进度也不撑宽页面
  const draftFits = async () => {
    const d = await page.locator('[role="dialog"]').last().boundingBox()
    const scroll = await draftBox(page).evaluate((el) => el.scrollWidth - el.clientWidth)
    return { ok: !!d && d.x >= 0 && d.x + d.width <= 390 && scroll <= 1, d, scroll }
  }
  await page.locator('[data-catalog-draft]').first().click()
  await draftBox(page).waitFor()
  await draftBox(page).locator('[data-draft-model] input').check()
  const setupFit = await draftFits()
  check('……起草弹窗（设置阶段）在视口里，正文不横向滚动', setupFit.ok, JSON.stringify(setupFit))
  await page.locator('[data-draft-start]').click()
  await until(async () => (await draftPhase(page)) === 'running')
  const narrowJob = state.jobs.at(-1)
  settle(state, narrowJob, 'visits', { added: 2 })
  settle(state, narrowJob, 'store_sales', { status: 'partial', parts_done: 2, added: 4, model_error: WIDE_ERR, blank_columns: 12, blank_sample: BLANK_SAMPLE })
  // 接连没写出来的提醒、超时和没写出来的次数、段缩小的说明也都在
  Object.assign(narrowJob.usage, { timeouts: 3, failed_calls: 4 })
  Object.assign(narrowJob, { part_columns: 20, notice: DRAFT_NOTICE })
  await until(async () => (await draftText(page, '[data-draft-task="store_sales"] [data-draft-task-reason]')).includes('第 3 段')
    && (await draftText(page, '[data-draft-notice]')) === DRAFT_NOTICE)
  await draftBox(page).locator('[data-draft-task="store_sales"] [data-draft-task-blank] summary').click().catch(() => {})
  const runFit = await draftFits()
  check('……起草进行中：段进度、长原因、提醒、展开的留空列名都折行，弹窗不横向滚动', runFit.ok
    && await draftBox(page).locator('[data-draft-task-blank-sample]').isVisible().catch(() => false), JSON.stringify(runFit))
  await shot(page, 'catalog-narrow-draft')
  await page.locator('[data-draft-hide]').click()
  await until(async () => (await page.locator('[data-catalog-draft-progress]').count()) === 1)
  check('……上方的起草进度（带着提醒）不撑宽页面', await overflow() <= 1 && !!(await page.locator('[data-catalog-draft-progress]').boundingBox())
        && (await page.locator('[data-catalog-draft-progress]').boundingBox()).width <= 390
        && await page.locator('[data-catalog-draft-notice]').count() === 1, String(await overflow()))
  stopJob(narrowJob)
  await until(async () => (await page.locator('[data-catalog-draft-progress]').count()) === 0)
  // 宽屏照旧是表格（表头在，列表格 860px 起，框里横向滚动）
  await page.setViewportSize({ width: 1440, height: 900 })
  await openDetail(page, S1, 'visits')
  const wide = await page.locator('[data-catalog-columns]').evaluate((el) => ({ head: getComputedStyle(el.querySelector('thead')).display,
    table: getComputedStyle(el.querySelector('table')).display, width: el.querySelector('table').getBoundingClientRect().width }))
  check('宽屏照旧是表格', wide.head !== 'none' && wide.table === 'table' && wide.width >= 860, JSON.stringify(wide))
  check('……页头写着说明', (await page.locator('[data-catalog-page] > header').innerText()).includes('先起草'))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await browser.close()

const after = await realCounts()
check('真库里的数据源和目录数量没有变化', JSON.stringify(after) === JSON.stringify(before), `${JSON.stringify(before)} → ${JSON.stringify(after)}`)
check('检查用的假数据源没有写进真库', !(await (await fetch(`${API}/datasources`)).json()).some((s) => NAMES.includes(s.name)))

console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 数据目录全部通过')
process.exit(failed ? 1 : 0)
