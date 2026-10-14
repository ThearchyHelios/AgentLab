// 数据目录闭环（/data/catalog/<源>）的交互回归：表助手、审阅队列、导出导入、防错事实的提议在表详情里的说明。
//
// 这几样都直接改数据目录：表助手的「保存 N 项修改」、审阅队列的成批确认 / 驳回、导入。查的是「请求里带没带读到的版本、
// 该带的对话和待保存的修改」「同一项以最后一次提案为准」「别人刚改过（409）时有没有按最新内容重算、停下来让人再看一眼」
// 「成批审阅逐条的结果、冲突的那几条标没标出来」「导入先预览、冲突逐条列出，确认后才写」——正常路径的页面检查一个字都
// 不会说。
//
// 写法同 check-catalog：沙箱里的数据源多半没探查过结构，所以数据源列表、目录清单、单表目录、审阅队列、表助手的事件流、
// 导出导入都用 page.route 伪造（虚构的景区业务库和一个开了数据剖析的库）；写请求在浏览器层答掉，照服务端的规则改这里的
// 假状态。没配的写请求一律拦成 503，不写库；最后核对真库里的数据源和目录数量没有变化。表助手不调真模型：事件流是这里
// 拼的。
// 跑之前前后端都得起着（./scripts/dev.sh），默认连 5273 / 8000；对别的实例跑时带上地址：
//   AGENTLAB_WEB=http://localhost:<前端端口> AGENTLAB_API=http://localhost:<后端端口>/api node scripts/check-catalog-loop.mjs
// 只跑其中几节：CHECK_ONLY=表助手,审阅 node scripts/check-catalog-loop.mjs（按节名包含匹配）。
// 截图：CHECK_SHOTS=/某个目录 时把关键状态存下来；CHECK_THEME=light 换浅色跑一遍。
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const API = process.env.AGENTLAB_API ?? 'http://localhost:8000/api'
const SHOTS = process.env.CHECK_SHOTS ?? ''
const THEME = process.env.CHECK_THEME === 'light' ? 'light' : 'dark'
// 不用 playwright install：系统 Chrome 就够了
const CHROME = process.env.CHROME_PATH ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
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

// 真库的数量：开头记一次，最后再数一次，必须一样（检查本身不许写库）
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

/** 开一页：handlers 按「METHOD 路径正则」匹配，没配的 GET 放行到后端，没配的写请求一律拦成 503。原生对话框都算失败 */
async function open(path, { handlers = [], viewport = { width: 1440, height: 900 } } = {}) {
  const ctx = await browser.newContext({ viewport, colorScheme: THEME, timezoneId: 'Asia/Shanghai', acceptDownloads: true })
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
        sent.push({ key, body, query: Object.fromEntries(url.searchParams), actor: r.headers()['x-actor'] ?? null })
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
  // 关之前先撤掉拦截、忽略还在路上的那几条：满负荷并行时，取真实返回的拦截可能在页面关掉之后才回来，
  // 没人接住的报错会把整个检查进程带崩（「Request context disposed」）
  return { page, sent, errors, natives, close: async () => {
    opened.delete(ctx)
    await page.unrouteAll({ behavior: 'ignoreErrors' }).catch(() => {})
    return ctx.close()
  } }
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
const writes = (sent) => sent.filter((s) => !s.key.startsWith('GET '))
const count = (sent, re) => sent.filter((s) => re.test(s.key)).length
const lastSent = (sent, re) => sent.filter((s) => re.test(s.key)).at(-1) ?? null
const textOf = (loc) => loc.first().innerText({ timeout: 800 }).then((t) => t.replace(/\s+/g, ' ').trim()).catch(() => '')

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
// 夹具：虚构的景区业务库（没开数据剖析）和一个开了数据剖析的库。名字、数字全是假的
// ---------------------------------------------------------------------------
const T0 = '2026-10-10T02:00:00+00:00'
const INIT = { fk: 'verified', human: 'confirmed' }
const item = (value, source = 'llm', status, extra = {}) => ({ value, source, status: status ?? INIT[source] ?? 'proposed', updated_at: T0, ...extra })
const col = (name, type = 'INTEGER', extra = {}) => ({ name, type, pk: false, not_null: false, comment: null, ...extra })
const pk = (name = 'id') => col(name, 'INTEGER', { pk: true, not_null: true })

const S1 = 'check-loop-scenic'
const S2 = 'check-loop-profiled'
const NAME1 = 'zzloopscenic'
const NAME2 = 'zzloopprofiled'

/** 防错事实的提议：模型起草的有效记录条件、去重规则，附剖析核对的排除比例（或没核对的原因） */
const FILTER_NOTE = '剖析核对：排除 3.2% 的行（共 12,345 行）'
const DEDUP_NOTE = '按票号去重，同一张票重复刷闸只算一次；没有核对排除比例（数据源没有开数据剖析）'

const VISITS_COLUMNS = [pk(), col('ticket_no', 'TEXT'), col('park_id'), col('gate_id'), col('visit_time', 'TEXT'), col('visitor_count'), col('status')]
const visitsNotes = () => ({
  label: item('入园记录', 'human'),
  grain: item('一张门票的一次入园'),
  keys: item(['ticket_no']),
  valid_filter: item('status NOT IN (9)', 'llm', 'proposed', { note: FILTER_NOTE }),
  dedup: item('按 ticket_no 去重', 'llm', 'proposed', { note: DEDUP_NOTE }),
  columns: {
    ticket_no: { label: item('票号'), measure: item('identifier', 'name') },
    visitor_count: { label: item('入园人数'), unit: item('人'), measure: item('flow') },
    status: { label: item('状态'), measure: item('status', 'human'), codes: item({ 1: '已入园' }) },
  },
})
const PROMO_COLUMNS = [pk(), ...Array.from({ length: 110 }, (_, i) => col(`attr_${String(i + 1).padStart(3, '0')}`, 'TEXT'))]

const table = (name, usage, columns, notes, extra = {}) => ({
  name, usage, columns, notes, version: notes ? (extra.version ?? 2) : 0, comment: null, is_view: false,
  updated_at: notes ? T0 : null, updated_by: notes ? '审核甲' : null, missing: false, ...extra,
})

function scenicTables() {
  return [
    table('visits', 42, VISITS_COLUMNS, visitsNotes()),
    table('orders', 30, [pk(), col('order_no', 'TEXT'), col('member_id'), col('total_amount', 'REAL')], {
      label: item('订单'), kind: item('fact'),
    }),
    table('parks', 18, [pk(), col('park_code', 'TEXT'), col('name', 'TEXT')], { label: item('景区', 'human') }),
    table('promo_attrs', 6, PROMO_COLUMNS, { label: item('促销属性') }),
    table('members', 3, [pk(), col('member_no', 'TEXT'), col('joined_on', 'TEXT')], null),
  ]
}
function profiledTables() {
  return [
    // 上一次起草留了 2 列空（gate_code、gate_kind）：开场引导先问这几列。opened_on 也还没有中文名和含义，但它是那次
    // 没写出来的列、不是模型留空的——排在表结构的前面，按「目录里还没有中文名和含义的前几列」举例就会举错
    table('gates', 12, [pk(), col('opened_on', 'TEXT'), col('gate_code', 'TEXT'), col('gate_kind', 'TEXT'), col('park_id')], {
      label: item('闸机'), columns: { id: { label: item('编号') }, park_id: { label: item('景区编号') } },
    }, { lastDraft: { status: 'ok', job_id: 'check-job-old', finished_at: T0, error: null, model_error: null, blank_columns: 2 } }),
    table('stores', 4, [pk(), col('store_code', 'TEXT')], null),
  ]
}
/** 那次起草的任务（GET draft-jobs/{id}）：只有闸机表这一行要紧——起草时记下的留空列名 */
const OLD_JOB = {
  id: 'check-job-old', source_id: 'check-loop-profiled', status: 'succeeded', use_model: true, model: 'check-model', guidance: null, note: null,
  created_at: T0, started_at: T0, finished_at: T0, created_by: '检查脚本', total: 1, done: 1,
  counts: { pending: 0, running: 0, ok: 1, partial: 0, failed: 0, skipped: 0, cancelled: 0 }, added: 3, updated: 0, removed: 0,
  usage: { calls: 1, input_tokens: 1000, output_tokens: 200 }, error: null, blank_columns: 2,
  tasks: [{ table_name: 'gates', status: 'ok', columns: 5, parts_total: 1, parts_done: 1, parts_written: 1, chunks_total: 1, chunks_done: 1,
    added: 3, updated: 0, removed: 0, version: 2, model_items: 3, blank_columns: 2, blank_sample: ['gate_code', 'gate_kind'],
    error: null, model_error: null, started_at: T0, finished_at: T0 }],
}

const blank = {
  kind: 'sqlite', host: null, port: null, username: null, readonly: true, description: '', enabled: true,
  password_masked: '', has_password: false, cached_schema: '', schema_error: '', available_schemas: [],
  last_checked_at: null, last_check_ok: null, last_latency_ms: null, last_error: null, current_snapshot: null,
}
const manual = (id, name, count, options = {}) => ({
  ...blank, id, name, origin: 'manual', database: `/tmp/check/${name}.db`, table_count: count, tools: [`db_query__${name}`],
  schema_synced_at: T0, options,
})
const PROFILE_ON = { catalog_profile: { enabled: true, max_queries: 60, query_timeout_s: 10, sample_size: 2000, max_scan_rows: 100000, max_total_s: 120 } }
const SOURCES = [manual(S1, NAME1, 5), manual(S2, NAME2, 2, PROFILE_ON)]
const NAMES = SOURCES.map((s) => s.name)

const freshState = () => ({ sources: SOURCES, tables: { [S1]: scenicTables(), [S2]: profiledTables() }, guidance: { [S1]: '金额单位是元' } })

// ---------------------------------------------------------------------------
// 假服务端：目录清单、单表目录、单项审阅、保存提案、预览（只做界面看得出来的部分）
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
      ;((notes.columns ??= {})[rest.slice(0, cut)] ??= {})[rest.slice(cut + 1)] = it
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
function rowOf(t) {
  const n = t.notes ?? {}
  const live = (it) => (it && it.status !== 'rejected' ? it : null)
  return {
    table_name: t.name, qualified: t.name, is_view: t.is_view, in_schema: !t.missing,
    label: live(n.label)?.value ?? null, label_status: live(n.label)?.status ?? null, kind: live(n.kind)?.value ?? null,
    counts: countsOf(n), relations: (n.relations ?? []).filter((r) => r.status !== 'rejected').length,
    usage: t.usage, version: t.version, updated_at: t.updated_at, updated_by: t.updated_by, last_draft: t.lastDraft ?? null,
  }
}
const listOf = (state, src) => ({
  tables: [...state.tables[src]].sort((a, b) => b.usage - a.usage).map(rowOf), system_notes: false, schema_note: null,
  schema_truncated: false, schema_total: state.tables[src].length, guidance: state.guidance[src] ?? null, active_job: null,
})
const detailOf = (t) => ({
  table_name: t.name, in_schema: !t.missing, notes: t.notes ?? {}, version: t.version, updated_at: t.updated_at, updated_by: t.updated_by,
  structure: { qualified: t.name, is_view: false, comment: null, columns: t.columns, primary_key: t.columns.filter((c) => c.pk).map((c) => c.name),
    foreign_keys: [], unique: [] },
  system_notes: false, usage: t.usage,
})
const parts = (key) => key.split(' ')[1].split('/')  // ['', 'datasources', id, 'catalog', table?, …]
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
const conflictDetail = (name) => ({ detail: `表「${name}」的数据目录刚被修改过，请重新载入后再提交` })
/** 单项审阅（catalog.review_item）：确认、驳回留着确认时的依据（reason），撤销审阅回到推断时去掉；人工填写的撤销即删除 */
function applyReview(notes, path, action) {
  const map = slots(clone(notes ?? {}))
  const it = map.get(path)
  if (!it) return null
  if (action === 'reset' && it.source === 'human') map.delete(path)
  else {
    it.status = action === 'confirm' ? 'confirmed' : action === 'reject' ? 'rejected' : INIT[it.source] ?? 'proposed'
    if (action === 'reset') delete it.reason
  }
  return assemble(map)
}
/**
 * 保存提案（catalog.apply_patch 的简化）：码值是补充（驳回的码值不算），其余整项替换。值改了的项记为人工填写、已确认；
 * 值不变的只改成已确认（来源、备注不变）。写了依据的项依据另存在 reason（确认时的依据），和备注分开，每次整句换掉：
 * 这次没写的去掉旧的
 */
function applyPatch(notes, changes) {
  const map = slots(clone(notes ?? {}))
  for (const c of changes) {
    const prev = map.get(c.path)
    const live = prev && prev.status !== 'rejected' ? prev : null
    const value = c.path.endsWith('.codes') && live ? { ...live.value, ...c.value } : c.value
    const reason = c.reason ? { reason: c.reason } : {}
    const at = new Date().toISOString()
    if (live && JSON.stringify(live.value) === JSON.stringify(value)) {
      if (live.status === 'confirmed') continue
      const { reason: _old, ...kept } = live
      map.set(c.path, { ...kept, status: 'confirmed', updated_at: at, ...reason })
      continue
    }
    map.set(c.path, { value, source: 'human', status: 'confirmed', updated_at: at, ...reason })
  }
  return assemble(map)
}
/** 预览：对着当前目录算改前、改后（只读）。被驳回的项改前照样写出原值、状态写已驳回，改后和状态按没有这一项算 */
function previewOf(t, changes) {
  const map = slots(t.notes ?? {})
  return {
    table_name: t.name, version: t.version, problems: [],
    changes: changes.map((c) => {
      const prev = map.get(c.path)
      const live = prev && prev.status !== 'rejected' ? prev : null
      const after = c.path.endsWith('.codes') && live ? { ...live.value, ...c.value } : c.value
      return { path: c.path, before: prev?.value ?? null, before_status: prev?.status ?? null, after, value: c.value, reason: c.reason ?? '',
        state: live && JSON.stringify(live.value) === JSON.stringify(after) ? (live.status === 'confirmed' ? 'same' : 'confirm') : 'change' }
    }),
  }
}

/**
 * 表助手的事件流：每个事件一行 data: <JSON>。服务端等模型时发 SSE 注释行（「: keep-alive」，不是事件）：开头、每个事件
 * 之间都夹一行，读流时得跳过
 */
const KEEP_ALIVE = ': keep-alive\n\n'
const sse = (events) => (route) => route.fulfill({
  status: 200, headers: { 'content-type': 'text/event-stream; charset=utf-8', 'cache-control': 'no-cache' },
  body: KEEP_ALIVE + events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join(KEEP_ALIVE),
})
const done = (queries = 0) => ({ type: 'done', usage: { input_tokens: 2400, output_tokens: 600 }, queries })
const MODEL_DOWN = { detail: '未配置模型接入，无法由模型起草数据目录。请到「设置 → 模型接入」添加', code: 'catalog_draft_model_unavailable' }
const BUSY = { detail: `数据源「${NAME1}」正在起草数据目录，请等这次起草结束（或先停止它）再开始新的起草`, code: 'catalog_draft_busy' }

function loopHandlers(state, replies = {}) {
  const reply = (k, fallback) => (route, ctx) => {
    const q = replies[k]
    const next = Array.isArray(q) ? q.shift() : q
    return (next ?? fallback)(route, ctx)
  }
  return [
    [/^GET \/datasources$/, (route) => json(state.sources)(route)],
    [/^GET \/datasources\/check-loop-[a-z]+\/catalog$/, (route, { key }) => json(listOf(state, parts(key)[2]))(route)],
    [/^GET \/datasources\/check-loop-[a-z]+\/catalog\/draft-jobs\/(active|latest)$/, json({ job: null })],
    // 某一次起草任务：表助手开场从中取起草时记下的留空列名。只认 state.jobs 里有的，别的 404（同服务端：任务不在了）
    [/^GET \/datasources\/check-loop-[a-z]+\/catalog\/draft-jobs\/[^/]+$/, (route, { key }) => {
      const id = parts(key)[5]
      const job = (state.jobs ?? [OLD_JOB]).find((j) => j.id === id)
      return job ? json({ job })(route) : json({ detail: '找不到这次起草任务' }, 404)(route)
    }],
    // 审阅队列、进度、成批审阅、导出导入：放在单表的 GET 前面
    [/^GET \/datasources\/check-loop-[a-z]+\/catalog\/review-queue$/, reply('queue', (route, { key, url }) => {
      const src = parts(key)[2]
      const kind = url.searchParams.get('kind') ?? 'all'
      const only = url.searchParams.get('table') ?? ''
      const limit = Number(url.searchParams.get('limit') ?? 50)
      const offset = Number(url.searchParams.get('offset') ?? 0)
      const all = (state.queue?.[src] ?? []).filter((i) => i.status === 'proposed' && (kind === 'all' || i.kind === kind) && (!only || i.table_name === only))
      return json({ total: all.length, items: all.slice(offset, offset + limit).map(({ kind: _k, ...i }) => ({ ...i, version: tableVersion(state, src, i.table_name) })) })(route)
    })],
    [/^GET \/datasources\/check-loop-[a-z]+\/catalog\/review-progress$/, reply('progress', (route) => json(state.progress ?? PROGRESS)(route))],
    [/^POST \/datasources\/check-loop-[a-z]+\/catalog\/review\/batch$/, reply('batch', (route, { key, body }) => {
      const src = parts(key)[2]
      const results = []
      const byTable = new Map()
      for (const it of body.items) (byTable.get(it.table_name) ?? byTable.set(it.table_name, []).get(it.table_name)).push(it)
      for (const [name, list] of byTable) {
        const t = state.tables[src].find((x) => x.name === name)
        if (state.conflictTables?.has(name) || list.some((i) => i.if_version !== t.version)) {
          for (const i of list) results.push({ table_name: name, path: i.path, ok: false, version: null, error: conflictDetail(name).detail, code: 'catalog_conflict' })
          continue
        }
        let notes = t.notes
        for (const i of list) notes = applyReview(notes, i.path, i.action) ?? notes
        writeEntry(t, notes)
        for (const i of list) {
          const q = state.queue[src].find((x) => x.table_name === name && x.path === i.path)
          if (q) q.status = i.action === 'confirm' ? 'confirmed' : i.action === 'reject' ? 'rejected' : 'proposed'
          results.push({ table_name: name, path: i.path, ok: true, version: t.version, error: null, code: null })
        }
      }
      return json({ results })(route)
    })],
    [/^GET \/datasources\/check-loop-[a-z]+\/catalog\/export$/, reply('export', (route, { key }) => {
      const src = parts(key)[2]
      const source = state.sources.find((s) => s.id === src)
      const file = { format: 'agentlab-catalog', version: 1, exported_at: T0, source: { name: source.name, kind: source.kind },
        guidance: state.guidance[src] ?? null, tables: Object.fromEntries(state.tables[src].filter((t) => t.notes).map((t) => [t.name, { notes: t.notes, version: t.version }])) }
      const name = `${source.name}-数据目录.json`
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(file, null, 2),
        headers: { 'content-disposition': `attachment; filename="catalog.json"; filename*=UTF-8''${encodeURIComponent(name)}` } })
    })],
    [/^POST \/datasources\/check-loop-[a-z]+\/catalog\/import\/preview$/, reply('importPreview', json(IMPORT_PREVIEW))],
    [/^POST \/datasources\/check-loop-[a-z]+\/catalog\/import$/, reply('import', (route, { key }) => {
      const src = parts(key)[2]
      const out = clone(IMPORT_PREVIEW)
      for (const t of out.tables) {
        const target = state.tables[src].find((x) => x.name === (t.matched_as ?? t.table_name))
        if (target) { writeEntry(target, { ...(target.notes ?? {}), description: item('导入的说明', 'human') }); t.version = target.version }
      }
      return json(out)(route)
    })],
    [/^GET \/datasources\/check-loop-[a-z]+\/catalog\/[^/]+\/impact$/, (route, { key }) => json({ table: parts(key)[4], templates: [] })(route)],
    [/^GET \/datasources\/check-loop-[a-z]+\/catalog\/[^/]+\/usage$/, reply('usage', (route, { key, url }) => {
      const runs = state.usage?.[parts(key)[4]] ?? []
      return json({ total: runs.length, runs: runs.slice(0, Number(url.searchParams.get('limit') ?? 20)) })(route)
    })],
    // 表助手：每一轮按 replies.assistant 依次答（没配就是 503，检查不能让请求落到真服务端）。请求不合规的照服务端的
    // 校验回 422：对话最多 40 条、每条最多 4000 字，待保存的修改最多 200 项
    [/^POST \/datasources\/check-loop-[a-z]+\/catalog\/[^/]+\/assistant$/, (route, ctx) => {
      const b = ctx.body ?? {}
      const bad = (b.messages?.length ?? 0) > 40 || (b.messages ?? []).some((m) => [...(m.content ?? '')].length > 4000) || (b.pending?.length ?? 0) > 200
      if (bad) return json({ detail: [{ type: 'too_long', loc: ['body', (b.pending?.length ?? 0) > 200 ? 'pending' : 'messages'], msg: 'too long' }] }, 422)(route)
      return reply('assistant', json({ detail: '检查脚本没有为这一轮准备答复' }, 503))(route, ctx)
    }],
    [/^POST \/datasources\/check-loop-[a-z]+\/catalog\/[^/]+\/patch\/preview$/, reply('preview', (route, { key, body }) => {
      const { t } = findTable(state, key)
      return json(previewOf(t, body.changes))(route)
    })],
    [/^POST \/datasources\/check-loop-[a-z]+\/catalog\/[^/]+\/patch$/, reply('patch', (route, { key, body }) => {
      const { t } = findTable(state, key)
      if (body.if_version !== t.version) return json(conflictDetail(t.name), 409)(route)
      if (body.changes.length > 200) return json({ detail: [{ type: 'too_long', loc: ['body', 'changes'], msg: 'too long' }] }, 422)(route)
      writeEntry(t, applyPatch(t.notes, body.changes))
      return json(detailOf(t))(route)
    })],
    [/^POST \/datasources\/check-loop-[a-z]+\/catalog\/[^/]+\/review$/, (route, { key, body }) => {
      const { t } = findTable(state, key)
      if (body.if_version !== t.version) return json(conflictDetail(t.name), 409)(route)
      const notes = applyReview(t.notes, body.path, body.action)
      if (!notes) return json({ detail: '找不到这一项，可能已被修改，请重新载入' }, 422)(route)
      writeEntry(t, notes)
      const q = state.queue?.[parts(key)[2]]?.find((x) => x.table_name === t.name && x.path === body.path)
      if (q) q.status = body.action === 'confirm' ? 'confirmed' : body.action === 'reject' ? 'rejected' : 'proposed'
      return json(detailOf(t))(route)
    }],
    [/^GET \/datasources\/check-loop-[a-z]+\/catalog\/[^/]+$/, (route, { key }) => {
      const { t } = findTable(state, key)
      return t ? json(detailOf(t))(route) : json({ detail: `数据源中没有表 ${parts(key)[4]}` }, 404)(route)
    }],
  ]
}
const tableVersion = (state, src, name) => state.tables[src].find((t) => t.name === name)?.version ?? 0

// ---------------------------------------------------------------------------
// 审阅队列的夹具：服务端排好的顺序（常用表里影响数字的推断在前）。kind 只是假服务端按筛选挑项用，不回给页面
// ---------------------------------------------------------------------------
// 形状照服务端（data/catalog_review.py）：field_label 只是字段的叫法（「中文名」「度量类型」），列级的项另给 column；
// reasons 的说法同服务端（「常用表（使用 42 次）」「影响数字的项（有效记录条件）」）
const qItem = (table_name, path, field, field_label, value, display, kind, priority, reasons, table_usage, extra = {}) => ({
  table_name, path, field, field_label, column: path.startsWith('columns.') ? path.slice(8, path.lastIndexOf('.')) : null,
  value, display, source: 'llm', status: 'proposed', note: null, priority, reasons, table_usage, kind, ...extra,
})
const MEMBER_REL = { columns: ['member_id'], to_table: 'members', to_columns: ['id'], cardinality: 'many_to_one', coverage: 0.98 }
function queueOf() {
  const list = [
    qItem('visits', 'valid_filter', 'valid_filter', '有效记录条件', 'status NOT IN (9)', 'status NOT IN (9)', 'facts', 9.6,
      ['常用表（使用 42 次）', '影响数字的项（有效记录条件）', '模型起草，未确认'], 42, { note: FILTER_NOTE }),
    qItem('visits', 'dedup', 'dedup', '去重规则', '按 ticket_no 去重', '按 ticket_no 去重', 'facts', 9.5,
      ['常用表（使用 42 次）', '影响数字的项（去重规则）', '模型起草，未确认'], 42, { note: DEDUP_NOTE }),
    qItem('orders', 'kind', 'kind', '表类型', 'fact', '明细表', 'names', 6.2, ['常用表（使用 30 次）', '帮助理解取值的项（表类型）', '模型起草，未确认'], 30),
    qItem('orders', 'relations.r_member', 'relation', '关联关系', MEMBER_REL, 'member_id → members.id（多对一，覆盖率 98%）', 'facts', 5.5,
      ['常用表（使用 30 次）', '影响数字的项（关联关系）', '命名推断，未确认'], 30, { source: 'name' }),
    qItem('visits', 'columns.ticket_no.measure', 'measure', '度量类型', 'identifier', '标识', 'facts', 5.1,
      ['常用表（使用 42 次）', '影响计数和汇总的项（度量类型）', '命名推断，未确认'], 42, { source: 'name' }),
    qItem('visits', 'grain', 'grain', '粒度', '一张门票的一次入园', '一张门票的一次入园', 'names', 4.4,
      ['常用表（使用 42 次）', '帮助理解取值的项（粒度）', '模型起草，未确认'], 42),
  ]
  // 余下的：促销属性表的列中文名（使用次数少，排在后面）。总数 57，一页 50
  for (let i = 1; i <= 51; i++) {
    const c = `attr_${String(i).padStart(3, '0')}`
    list.push(qItem('promo_attrs', `columns.${c}.label`, 'label', '中文名', `属性 ${i}`, `属性 ${i}`, 'names', 1 - i / 100,
      ['使用 6 次', '名称和说明（中文名）', '模型起草，未确认'], 6))
  }
  return list
}
// 形状照服务端：top 只算被运行用过的表（5 张，不到 20），另带 items_proposed、items_verified
const PROGRESS = {
  top_n: 20,
  top: { tables: 5, items_total: 286, items_confirmed: 120, items_proposed: 150, items_verified: 10 },
  all: { items_total: 1460, items_confirmed: 262, items_proposed: 1180, items_rejected: 12, items_verified: 6 },
  facts: { proposed: 56, confirmed: 12 },
}
const USAGE_RUNS = [
  { run_id: 'run-check-0003', workflow_id: 'wf-check-1', workflow_name: '每日入园统计', run_class: 'formal', status: 'succeeded', started_at: '2026-10-12T01:00:00+00:00' },
  { run_id: 'run-check-0002', workflow_id: 'wf-check-2', workflow_name: '闸机异常排查', run_class: 'exploratory', status: 'failed', started_at: '2026-10-11T03:00:00+00:00' },
  { run_id: 'run-check-0001', workflow_id: null, workflow_name: null, run_class: 'exploratory', status: 'succeeded', started_at: '2026-10-10T05:00:00+00:00' },
]

// ---------------------------------------------------------------------------
// 导入的夹具：文件里的表 visits、ORDERS（大小写不同，对上 orders）、legacy_tickets（这里没有）
// ---------------------------------------------------------------------------
const IMPORT_FILE = {
  format: 'agentlab-catalog', version: 1, exported_at: '2026-10-09T08:00:00+00:00', source: { name: 'zzloopother', kind: 'sqlite' },
  guidance: '景区票务库：金额单位是元',
  tables: {
    visits: { version: 7, notes: { label: item('入园记录', 'human'), description: item('每次刷闸记一行', 'human') } },
    ORDERS: { version: 3, notes: { kind: item('fact', 'human') } },
    legacy_tickets: { version: 1, notes: { label: item('旧票', 'human') } },
  },
}
// 形状照服务端（data/catalog_transfer.py）：matched_as 总有值（对不上的表只在 unmatched_tables 里），每张表另带 version、
// error（预览里也带），冲突另带 file_status、target_status；界面只在导入的结果里写版本
const IMPORT_PREVIEW = {
  tables: [
    { table_name: 'visits', matched_as: 'visits', add: 1, update: 0, keep: 4, version: 2, error: null,
      skipped: [{ path: 'columns.gate_code.label', why: '表结构里没有列 gate_code' }],
      conflicts: [{ path: 'label', label: '表的中文名', file_value: '入园流水', target_value: '入园记录', why: '这里已确认，以这里为准',
        file_status: 'confirmed', target_status: 'confirmed' }] },
    { table_name: 'ORDERS', matched_as: 'orders', add: 0, update: 1, keep: 1, version: 2, error: null, conflicts: [], skipped: [] },
  ],
  unmatched_tables: ['legacy_tickets'],
  totals: { tables: 2, add: 1, update: 1, keep: 5, conflicts: 1, skipped: 1 },
  guidance: { file: '景区票务库：金额单位是元', target: '金额单位是元', action: 'keep' },
}

// ---------------------------------------------------------------------------
// 页面上的定位
// ---------------------------------------------------------------------------
const detail = (page) => page.locator('[data-catalog-detail]')
const version = async (page) => Number(await detail(page).getAttribute('data-version'))
const openDetail = async (page, src, name, q = '') => {
  await goto(page, `/data/catalog/${src}/${encodeURIComponent(name)}${q}`)
  await until(async () => (await page.locator(`[data-catalog-detail="${name}"][data-version]`).count()) > 0)
}
const panel = (page) => page.locator('[data-table-assistant]')
const input = (page) => panel(page).locator('[data-assistant-input]')
const streaming = (page) => panel(page).getAttribute('data-streaming').catch(() => null)
/** 发一句话，等这一轮答完（或出错、停止） */
async function say(page, text, { wait = true } = {}) {
  await input(page).fill(text)
  await input(page).press('Enter')
  if (wait) {
    await until(async () => (await streaming(page)) === 'true', 1500)
    await until(async () => (await streaming(page)) === 'false')
  }
}
const lastTurn = (page) => panel(page).locator('[data-assistant-msg="assistant"]').last()
const pendingCount = async (page) => Number(await panel(page).locator('[data-assistant-pending]').getAttribute('data-assistant-pending').catch(() => '-1'))
const changeState = (page, path, turn = null) => (turn ?? panel(page)).locator(`[data-assistant-change="${path}"]`).last().getAttribute('data-change-state').catch(() => null)
const confirmBox = (page) => page.locator('[role="dialog"]').filter({ hasNot: page.locator('[data-import-dialog]') }).last()

const CODE_RE = /\b(?:proposed|verified|confirmed|rejected|many_to_one|identifier|fact|dimension|llm|fk|if_version|notes|facts|names|catalog_draft_busy|catalog_conflict)\b/
async function cleanCopy(page, where) {
  let t = await page.locator('[data-catalog-page]').innerText()
  for (const n of [...NAMES, 'zzloopother']) t = t.replaceAll(n, '')
  // 表名、列名、SQL 是库里的东西，照原样显示，先去掉再查
  t = t.replace(/[A-Za-z_][A-Za-z0-9_]*_[A-Za-z0-9_]+/g, '').replace(/SELECT[^\n]*/g, '')
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

await section('纯函数 · 对话怎么截、待保存的修改怎么累积、审阅结果怎么并', async () => {
  const { page, errors, close } = await open('/data')
  const r = await page.evaluate(async () => {
    const a = await import('/src/pages/catalog/assistant.ts')
    const rv = await import('/src/pages/catalog/review.ts')
    const dj = await import('/src/pages/catalog/draftJob.ts')
    const out = {}
    // 对话：最多 40 条、每条最多 4000 字，总从用户的话开始；没有正文的回答写成占位
    const turns = []
    for (let i = 0; i < 25; i++) {
      turns.push({ id: `u${i}`, role: 'user', text: i === 0 ? 'x'.repeat(5000) : `问 ${i}` })
      turns.push({ id: `a${i}`, role: 'assistant', state: 'done', parts: i === 3 ? [] : [{ kind: 'text', text: `答 ${i}` }] })
    }
    const h = a.historyOf(turns, '最后一问')
    out.history = { n: h.length, first: h[0].role, last: h.at(-1).content, max: Math.max(...h.map((m) => [...m.content].length)) }
    const short = a.historyOf(turns.slice(0, 8), '再问')
    out.short = { n: short.length, first: [...short[0].content].length, placeholder: short[7].content }
    // 待保存的修改：同一路径换成后一条、位置不动；已经是这个值且已确认的不收
    const c = (path, value, state = 'change') => ({ path, before: null, before_status: null, after: value, value, reason: '', state })
    let p = a.mergeProposal([], [c('label', '甲'), c('grain', '一行'), c('kind', 'fact', 'same')], 'p1', 3)
    p = a.mergeProposal(p, [c('label', '乙'), c('dedup', '按票号')], 'p2', 5)
    out.pending = { paths: p.map((x) => x.path).join(','), label: p[0].value, origin: p[0].origin, version: a.pendingVersion(p) }
    out.states = [a.changeStateOf(p, new Set(), 'p1', 'label'), a.changeStateOf(p, new Set(), 'p2', 'label'),
      a.changeStateOf(a.dropPending(p, 'grain'), new Set(), 'p1', 'grain'), a.changeStateOf(p, new Set([a.savedKey('p1', 'grain')]), 'p1', 'grain')]
    // 409 后按预览重算：预览里没有的、已经是这个值的去掉，版本换成预览的
    const pv = a.applyPreview(p, { table_name: 't', version: 9, problems: ['x'], changes: [
      { ...c('label', '乙'), before: '丙', before_status: 'proposed' }, { ...c('grain', '一行'), state: 'same' } ] })
    out.preview = { paths: pv.map((x) => x.path).join(','), before: pv[0].before, version: pv[0].version }
    // 上次起草的留空列：停了、没有新写入时用再之前那次的
    out.blank = [
      dj.lastBlankInfo({ status: 'ok', blank_columns: 4, blank_columns_from: 9 }),
      dj.lastBlankInfo({ status: 'cancelled', blank_columns: 0, blank_columns_from: 6 }),
      dj.lastBlankInfo({ status: 'cancelled', blank_columns: 0, blank_columns_from: 6, parts_written: 2 }),
      dj.lastBlankInfo({ status: 'running', blank_columns: 3, blank_columns_from: 6 }),
      dj.lastBlankInfo({ status: 'cancelled', blank_columns: 2, blank_columns_from: 6 }),
    ]
    out.chunks = [dj.chunksOf({ parts_total: 3, chunks_total: 3, chunks_done: 1 }), dj.chunksOf({ parts_total: 3, chunks_total: 5, chunks_done: 2 }),
      dj.chunksOf({ parts_total: 1, chunks_total: 2, chunks_done: 0 }), dj.chunksOf({ parts_total: null, chunks_total: null })]
    // 审阅：地址参数来回、成批的结果、撤销审阅只发审阅成功过的、表详情改过之后跟着换状态
    const q = rv.writeReviewParams(new URLSearchParams('q=x&view=review'), { kind: 'facts', table: 'visits', item: 'columns.a.b.label' })
    out.params = { qs: q.toString(), back: rv.reviewParamsOf(q), cleared: rv.writeReviewParams(q, { view: 'list', item: '' }).toString() }
    const items = [{ table_name: 't1', path: 'grain', status: 'proposed', version: 2 }, { table_name: 't1', path: 'dedup', status: 'proposed', version: 2 },
      { table_name: 't2', path: 'kind', status: 'proposed', version: 7 }]
    const sel = new Set(items.map(rv.itemKey))
    const res = rv.applyResults([
      { table_name: 't1', path: 'grain', ok: true, version: 3, error: null, code: null },
      { table_name: 't1', path: 'dedup', ok: true, version: 3, error: null, code: null },
      { table_name: 't2', path: 'kind', ok: false, version: null, error: '刚被修改过', code: 'catalog_conflict' },
    ], 'confirm', new Map(), new Map())
    out.results = { t1: res.versions.get('t1'), t2: res.versions.get('t2') ?? null, kinds: items.map((i) => res.outcomes.get(rv.itemKey(i))?.kind).join(',') }
    out.targets = { reset: rv.batchTargets(items, sel, 'reset', res.outcomes).map((i) => i.path).join(','),
      confirm: rv.batchTargets(items, sel, 'confirm', res.outcomes).map((i) => i.path).join(','),
      reject: rv.batchTargets(items, sel, 'reject', res.outcomes).length }
    const synced = rv.syncFromDetail(items, 't1', { grain: { value: 'x', source: 'llm', status: 'proposed' },
      dedup: { value: 'y', source: 'llm', status: 'rejected' } }, 4, res.outcomes, res.versions)
    out.sync = { grain: synced.outcomes.get(rv.itemKey(items[0])), dedup: synced.outcomes.get(rv.itemKey(items[1]))?.status, v: synced.versions.get('t1') }
    out.pct = [rv.percentOf(120, 286), rv.percentOf(0, 0)]
    // 列表头「N 项待确认」：这一页上审阅成功、不再是推断的项从总数里减掉；撤销审阅回到推断的、冲突的照算
    out.pendingTotal = [rv.pendingTotal(57, items, new Map()), rv.pendingTotal(57, items, res.outcomes),
      rv.pendingTotal(57, items, synced.outcomes), rv.pendingTotal(1, items, res.outcomes)]
    // 提案的改前：被驳回的原值也写出来（同导入预览「目标里这一项已经驳回」）
    const cp = await import('/src/run/catalogPatch.ts')
    out.before = [
      cp.patchBeforeText({ path: 'columns.amount.unit', before: '元', before_status: 'rejected' }),
      cp.patchBeforeText({ path: 'columns.amount.unit', before: '元', before_status: 'proposed' }),
      cp.patchBeforeText({ path: 'columns.amount.unit', before: null, before_status: null }),
      cp.patchBeforeText({ path: 'kind', before: 'fact', before_status: 'rejected' }),
      cp.patchBeforeText({ path: 'columns.status.codes', before: { 1: '有效' }, before_status: 'rejected' }),
    ]
    // 审阅队列一项的位置：按 path 说到列（服务端的 field_label 只是字段的叫法）
    out.where = [
      rv.reviewWhere({ path: 'columns.status.codes', field_label: '码值', column: 'status', value: { 1: '有效' } }),
      rv.reviewWhere({ path: 'valid_filter', field_label: '有效记录条件', column: null, value: 'status <> 9' }),
      rv.reviewWhere({ path: 'relations.r1', field_label: '关联关系', column: null, value: { columns: ['park_id'], to_table: 'parks', to_columns: ['id'] } }),
      rv.reviewWhere({ path: 'columns.a.b.label', field_label: '中文名', column: 'a.b', value: '甲' }),
      rv.reviewWhere({ path: 'columns.x.weird', field_label: '怪字段', column: 'x', value: 1 }),
      rv.reviewWhere({ path: 'weird', field_label: '怪字段', value: 1 }),
    ]
    // 审阅进度：三行同一个口径——已确认 ÷（已确认 + 待确认）；不知道待确认几项时不算百分比
    out.progress = [rv.confirmProgress(120, 150), rv.confirmProgress(30, 0), rv.confirmProgress(0, 0), rv.confirmProgress(5, undefined)]
    // 409 后只重算交去保存的那几项：保存期间又到的提案原样留着
    const A = { ...c('label', '甲'), origin: 'p1', version: 2 }
    const B = { ...c('grain', '一行'), origin: 'p2', version: 3 }
    const pv2 = { table_name: 't', version: 9, problems: [], changes: [{ ...c('label', '甲'), before: '丙', before_status: 'proposed' }] }
    out.only = { kept: a.applyPreview([A, B], pv2, [A]).map((x) => `${x.path}@${x.version}`).join(','),
      all: a.applyPreview([A, B], pv2).map((x) => x.path).join(',') }
    // 交给助手的待保存的修改最多 200 项：只交最近的
    const many = Array.from({ length: 202 }, (_, i) => ({ ...c(`columns.c${i}.label`, `列 ${i}`), origin: 'p', version: 1 }))
    const capped = a.pendingForAssistant(many)
    out.capped = { n: capped.length, first: capped[0].path, last: capped.at(-1).path, few: a.pendingForAssistant(many.slice(0, 3)).length }
    // 开场的留空列：只举起草时记下的；拿不到时改说目录里还没有中文名和含义的列，按钮上的数字就是点名的列数
    const T = (await import('/src/lib/terms.ts')).CATALOG_ASSISTANT_TEXT
    const lead = (n, sample, unnamed) => {
      const b = a.blankLeadOf(n, sample, unnamed)
      return { from: b.from, text: T.blankLead(b), button: b.ask ? T.suggestBlank(b.from, b.ask.total) : null,
        ask: b.ask ? T.suggestBlankText(b.from, b.ask.columns, b.ask.total) : null }
    }
    out.blankLead = [
      lead(2, ['gate_code', 'gate_kind'], ['opened_on', 'gate_code', 'gate_kind']),
      lead(2, ['gate_code', 'gate_kind'], ['opened_on', 'gate_kind']),
      lead(2, ['gate_code', 'gate_kind'], ['opened_on']),
      lead(12, ['c1', 'c2', 'c3', 'c4', 'c5'], ['c0', 'c2', 'c5', 'c9']),
      lead(2, null, ['opened_on', 'gate_code', 'gate_kind']),
      lead(2, [], []),
      lead(2, null, Array.from({ length: 25 }, (_, i) => `k${i}`)),
    ]
    return out
  })
  // 51 条取后 40 条，打头的那条是助手的回答，去掉：39 条
  check('对话最多交 40 条，从用户的话开始，最后一条是这一问', r.history.n === 39 && r.history.first === 'user' && r.history.last === '最后一问', JSON.stringify(r.history))
  check('……每条最多 4000 字', r.history.max <= 4000 && r.short.first === 4000, JSON.stringify(r.short))
  check('……没有正文的回答写成占位，保持一问一答', r.short.n === 9 && r.short.placeholder === '（没有回答）', JSON.stringify(r.short))
  check('待保存的修改：同一路径换成后一条、位置不动，已是这个值且已确认的不收', r.pending.paths === 'label,grain,dedup' && r.pending.label === '乙'
        && r.pending.origin === 'p2', JSON.stringify(r.pending))
  check('……保存带最早那条提案对照的版本', r.pending.version === 3)
  check('……提案卡片上一项的处境：被替换、待保存、去掉、已保存', r.states.join(',') === 'replaced,pending,dropped,saved', r.states.join(','))
  check('409 后按预览重算：预览里没有的、已是这个值的去掉，换上最新的改前和版本', r.preview.paths === 'label' && r.preview.before === '丙' && r.preview.version === 9,
    JSON.stringify(r.preview))
  check('留空列：结束的起草用自己的；停了且没有新写入用再之前那次的；写进过段、还在起草的不用旧数',
    JSON.stringify(r.blank) === JSON.stringify([{ n: 4, earlier: false }, { n: 6, earlier: true }, { n: 0, earlier: false }, { n: 0, earlier: false }, { n: 2, earlier: false }]),
    JSON.stringify(r.blank))
  check('块进度：块比段多才有', JSON.stringify(r.chunks) === JSON.stringify([null, { done: 2, total: 5 }, { done: 0, total: 2 }, null]), JSON.stringify(r.chunks))
  check('审阅视图的地址参数：只动自己的键，来回一致', r.params.qs === 'q=x&view=review&kind=facts&rtable=visits&item=columns.a.b.label'
        && r.params.back.kind === 'facts' && r.params.back.table === 'visits' && r.params.back.item === 'columns.a.b.label' && r.params.cleared === 'q=x&kind=facts&rtable=visits',
    JSON.stringify(r.params))
  check('成批审阅的结果：成功的记这张表写入后的版本，冲突的标出来', r.results.t1 === 3 && r.results.t2 === null && r.results.kinds === 'ok,ok,conflict', JSON.stringify(r.results))
  check('……撤销审阅只发审阅成功过的，冲突的照旧可以再确认', r.targets.reset === 'grain,dedup' && r.targets.confirm === 'kind' && r.targets.reject === 3,
    JSON.stringify(r.targets))
  check('表详情改过之后：回到推断的记成撤销审阅，驳回的记成已驳回，版本换成最新的', r.sync.grain?.action === 'reset' && r.sync.dedup === 'rejected' && r.sync.v === 4,
    JSON.stringify(r.sync))
  check('进度的百分比四舍五入，分母为 0 时不写', r.pct[0] === 42 && r.pct[1] === null, JSON.stringify(r.pct))
  check('列表头的待确认项数：这一页上审阅成功的从总数里减掉，撤销审阅、冲突的照算，不会减成负数',
    JSON.stringify(r.pendingTotal) === JSON.stringify([57, 55, 56, 0]), JSON.stringify(r.pendingTotal))
  check('提案的改前：被驳回的写「已驳回：原值」（值按界面上的说法），别的照原样，没有值写「未填写」',
    JSON.stringify(r.before) === JSON.stringify(['已驳回：元', '元', '未填写', '已驳回：明细表', '已驳回：1=有效']), JSON.stringify(r.before))
  check('队列一项的位置写到列：列 status 的码值、表的有效记录条件、指向 parks 的关联关系；列名里有句点、认不出的路径也说得通',
    JSON.stringify(r.where) === JSON.stringify(['列 status 的码值', '表的有效记录条件', '指向 parks 的关联关系', '列 a.b 的中文名', '列 x 的怪字段', '怪字段']),
    JSON.stringify(r.where))
  check('审阅进度三行同一个口径：已确认 ÷（已确认 + 待确认），审完是 100%，没有可确认的、不知道待确认几项时不算',
    JSON.stringify(r.progress.map((x) => x.pct)) === JSON.stringify([44, 100, null, null]) && r.progress[3].proposed === null,
    JSON.stringify(r.progress))
  check('409 后只重算交去保存的那几项：保存期间又到的提案（粒度）原样留着、版本不变；不给时照旧全部重算',
    r.only.kept === 'label@9,grain@3' && r.only.all === 'label', JSON.stringify(r.only))
  check('交给助手的待保存的修改最多 200 项，只交最近的；不到 200 项的全交', r.capped.n === 200 && r.capped.first === 'columns.c2.label'
        && r.capped.last === 'columns.c201.label' && r.capped.few === 3, JSON.stringify(r.capped))
  const bl = r.blankLead
  check('留空列（记全了）：举的就是起草时记下的列，没写出来的 opened_on 不算；按钮上的 2 列就是点名的 2 列',
    bl[0].from === 'draft' && bl[0].text === '上次起草有 2 列模型拿不准、留空了：gate_code、gate_kind。可以先从这几列开始。'
        && bl[0].button === '补上留空的 2 列' && bl[0].ask === '请帮我补上这几列的中文名和含义：gate_code、gate_kind。拿不准的地方先问我。', JSON.stringify(bl[0]))
  check('……补上了一列：写还有 1 列没补上，按钮是「补上留空的 1 列」、只点名它', bl[1].text === '上次起草有 2 列模型拿不准、留空了，还有 1 列没补上：gate_kind。可以先从这几列开始。'
        && bl[1].button === '补上留空的 1 列' && bl[1].ask === '请帮我补上这几列的中文名和含义：gate_kind。拿不准的地方先问我。', JSON.stringify(bl[1]))
  check('……都补上了：写明都已补上，不给按钮', bl[2].text === '上次起草有 2 列模型拿不准、留空了，现在都已补上中文名或含义。' && bl[2].button === null, JSON.stringify(bl[2]))
  check('……没记全（12 列只记了 5 个）：举还没补上的那几个，按钮不写数字',
    bl[3].text === '上次起草有 12 列模型拿不准、留空了，例如 c2、c5。可以先从这几列开始。' && bl[3].button === '补上留空的列'
        && bl[3].ask === '请帮我补上上次起草时模型留空的列的中文名和含义，例如 c2、c5。拿不准的地方先问我。', JSON.stringify(bl[3]))
  check('拿不到起草时记下的列名：改说目录里还没有中文名和含义的列，按钮上的数字就是这些列的总数',
    bl[4].from === 'catalog' && bl[4].text === '上次起草有 2 列模型拿不准、留空了。目录里还有 3 列没有中文名和含义，例如 opened_on、gate_code、gate_kind，可以先从这些列开始。'
        && bl[4].button === '补上没有中文名和含义的 3 列' && bl[4].ask === '请帮我补上这几列的中文名和含义：opened_on、gate_code、gate_kind。拿不准的地方先问我。',
    JSON.stringify(bl[4]))
  check('……目录里的列都有了：不给按钮', bl[5].text === '上次起草有 2 列模型拿不准、留空了，现在目录里的列都已有中文名或含义。' && bl[5].button === null, JSON.stringify(bl[5]))
  check('……超过 20 列：点名前 20 列、写「等 25 列」，和按钮上的 25 列一致', bl[6].button === '补上没有中文名和含义的 25 列'
        && bl[6].ask.includes('k19 等 25 列') && !bl[6].ask.includes('k20'), JSON.stringify(bl[6]))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

// 第一轮：用户说出码值，助手整理成两项提案
// 形状照服务端：另带 source（数据源名）、table_label（表的中文名），界面不用
const PROPOSAL_1 = {
  type: 'proposal', source: NAME1, source_id: S1, table: 'visits', table_label: '入园记录', version: 2, changes: [
    { path: 'columns.status.codes', label: '列 status 的码值', before: { 1: '已入园' }, before_status: 'proposed', after: { 1: '已入园', 9: '已作废' },
      value: { 9: '已作废' }, reason: '用户说「status 为 9 表示作废」', state: 'change' },
    { path: 'columns.status.meaning', label: '列 status 的含义', before: null, before_status: null, after: '入园状态：1 已入园，9 已作废',
      value: '入园状态：1 已入园，9 已作废', reason: '用户说「status 为 9 表示作废」', state: 'change' },
  ],
}
// 第二轮：码值再补一个（同一项，以这一次为准），有效记录条件跟着改
const PROPOSAL_2 = {
  type: 'proposal', source: NAME1, source_id: S1, table: 'visits', table_label: '入园记录', version: 2, changes: [
    { path: 'columns.status.codes', label: '列 status 的码值', before: { 1: '已入园' }, before_status: 'proposed', after: { 1: '已入园', 8: '已退票', 9: '已作废' },
      value: { 8: '已退票', 9: '已作废' }, reason: '用户说「8 是退票」', state: 'change' },
    { path: 'valid_filter', label: '有效记录条件', before: 'status NOT IN (9)', before_status: 'proposed', after: 'status NOT IN (8, 9)',
      value: 'status NOT IN (8, 9)', reason: '用户说「统计时退票和作废的都要排除」', state: 'change' },
  ],
}
const SAY_1 = 'status 为 9 表示作废，统计时要排除'
const SAY_2 = '8 是退票，统计时退票和作废的都要排除'
const TEXT_1 = ['好的，', '我把码值和含义整理成了提案：']

await section('表助手 · 入口、开场、提案与待保存的修改', async () => {
  const state = freshState()
  const replies = {
    assistant: [
      sse([{ type: 'text', delta: TEXT_1[0] }, { type: 'text', delta: TEXT_1[1] }, PROPOSAL_1, done()]),
      sse([{ type: 'text', delta: '明白。退票也排除：' }, PROPOSAL_2, { type: 'text', delta: '请核对后保存。' }, done()]),
    ],
  }
  const { page, sent, errors, natives, close } = await open(`/data/catalog/${S1}/visits`, { handlers: loopHandlers(state, replies) })
  await until(async () => (await page.locator('[data-catalog-detail="visits"][data-version]').count()) > 0)
  const entry = page.locator('[data-catalog-assistant-open="header"]')
  check('表详情顶部有「和助手一起完善」', await entry.count() === 1 && (await textOf(entry)).includes('和助手一起完善'), await textOf(entry))
  await entry.click()
  check('点开：表助手面板出现，焦点在输入框', await until(async () => panel(page).isVisible())
        && await until(async () => page.evaluate(() => document.activeElement?.hasAttribute('data-assistant-input'))))
  check('……入口按钮标为展开', (await entry.getAttribute('aria-expanded')) === 'true')
  check('没开剖析的源：写明只能根据表结构和目录推断，不能查数据',
    (await textOf(panel(page).locator('[data-assistant-no-profile]'))).includes('这个数据源没开数据剖析，助手只能根据表结构和目录推断，不能查数据'))
  check('开场：说明整理成修改提案、确认后才写入、不写公式', await until(async () => (await textOf(panel(page).locator('[data-assistant-greeting]'))).includes('修改提案'))
        && (await textOf(panel(page).locator('[data-assistant-greeting]'))).includes('不写计算公式'))
  const log = panel(page).locator('[data-assistant-log]')
  check('对话区是读屏会播报的 log（aria-live=polite）', (await log.getAttribute('role')) === 'log' && (await log.getAttribute('aria-live')) === 'polite')
  check('输入框、发送按钮有名字', (await input(page).getAttribute('aria-label')) === '给表助手的消息'
        && (await panel(page).locator('[data-assistant-send]').getAttribute('aria-label')) === '发送')
  check('还没有待保存的修改：不给保存', await panel(page).locator('[data-assistant-save]').count() === 0)
  // 输入法组字时的回车是选词、Esc 是取消候选词：不发送、不关面板
  await input(page).fill('status 为')
  const fire = (init) => input(page).evaluate((el, i) => el.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, cancelable: true, ...i })), init)
  await fire({ key: 'Enter', isComposing: true })
  await fire({ key: 'Enter', keyCode: 229 })
  await fire({ key: 'Escape', isComposing: true })
  await sleep(300)
  check('输入法组字时的回车不发送、Esc 不关面板，草稿还在', count(sent, /\/assistant$/) === 0 && await panel(page).isVisible()
        && (await input(page).inputValue()) === 'status 为')
  await input(page).fill('')

  // 第一轮：流式显示正文，提案逐项写改前、改后、依据，进待保存的修改
  await say(page, SAY_1)
  const req1 = lastSent(sent, /\/assistant$/)
  check('第一轮的请求：对话只有这一句，还没有待保存的修改', JSON.stringify(req1?.body) === JSON.stringify({ messages: [{ role: 'user', content: SAY_1 }], pending: [] }),
    JSON.stringify(req1?.body))
  check('……用户的话出现在对话里', (await textOf(panel(page).locator('[data-assistant-msg="user"] [data-assistant-user-text]').last())) === SAY_1,
    await textOf(panel(page).locator('[data-assistant-msg="user"]').last()))
  check('回答的正文是几段增量接起来的', (await textOf(lastTurn(page).locator('[data-assistant-text]'))) === TEXT_1.join(''), await textOf(lastTurn(page).locator('[data-assistant-text]')))
  const card1 = panel(page).locator('[data-assistant-msg="assistant"]').first().locator('[data-assistant-proposal]')
  check('提案卡片：2 项，每项写改前、改后、依据', await card1.count() === 1 && await card1.locator('[data-assistant-change]').count() === 2
        && (await textOf(card1.locator('[data-assistant-change="columns.status.codes"]'))).includes('改前')
        && (await textOf(card1.locator('[data-assistant-change="columns.status.codes"]'))).includes('依据')
        && (await textOf(card1.locator('[data-assistant-change="columns.status.codes"]'))).includes('用户说「status 为 9 表示作废」'),
    await textOf(card1))
  check('……码值写成「1=已入园、9=已作废」，位置用服务端给的说法', (await textOf(card1.locator('[data-assistant-change="columns.status.codes"] [data-change-after]'))).includes('1=已入园、9=已作废')
        && (await textOf(card1.locator('[data-assistant-change="columns.status.codes"]'))).startsWith('列 status 的码值'))
  check('待保存的修改：2 项，给「保存 2 项修改」', await pendingCount(page) === 2 && (await textOf(panel(page).locator('[data-assistant-save]'))) === '保存 2 项修改')
  // 逐项去掉、加回
  await card1.locator('[data-assistant-change="columns.status.meaning"] [data-assistant-change-toggle]').click()
  check('提案里去掉一项：标「已去掉」，待保存的少一项', await until(async () => (await pendingCount(page)) === 1)
        && (await changeState(page, 'columns.status.meaning', card1)) === 'dropped')
  const toggle = card1.locator('[data-assistant-change="columns.status.meaning"] [data-assistant-change-toggle]')
  check('……去掉以后按钮是「加回」，读屏名写明加回哪一项', (await textOf(toggle)) === '加回' && (await toggle.getAttribute('aria-label')) === '加回：列 status 的含义')
  await toggle.click()
  check('……加回：又是 2 项', await until(async () => (await pendingCount(page)) === 2) && (await changeState(page, 'columns.status.meaning', card1)) === 'pending')
  await panel(page).locator('[data-assistant-pending-item="columns.status.meaning"] [data-assistant-pending-remove]').click()
  check('在「待保存的修改」里去掉：剩 1 项，卡片上跟着标「已去掉」', await until(async () => (await pendingCount(page)) === 1)
        && (await changeState(page, 'columns.status.meaning', card1)) === 'dropped')

  // 第二轮：带上前面的对话和还没保存的修改；同一项以这一次为准
  await say(page, SAY_2)
  const req2 = lastSent(sent, /\/assistant$/)?.body
  check('第二轮的请求：带上前面的对话（助手那句是它的正文）和待保存的修改', JSON.stringify(req2?.messages) === JSON.stringify([
    { role: 'user', content: SAY_1 }, { role: 'assistant', content: TEXT_1.join('') }, { role: 'user', content: SAY_2 },
  ]) && JSON.stringify(req2?.pending) === JSON.stringify([{ path: 'columns.status.codes', value: { 9: '已作废' }, reason: '用户说「status 为 9 表示作废」' }]),
  JSON.stringify(req2))
  check('……正文、提案、正文按到来的先后排', JSON.stringify(await lastTurn(page).locator('[data-assistant-part]').evaluateAll((els) => els.map((e) => e.getAttribute('data-assistant-part'))))
    === JSON.stringify(['text', 'proposal', 'text']))
  check('同一项以最后一次提案为准：待保存 2 项（码值换成这一轮的、加上有效记录条件）', await pendingCount(page) === 2
        && (await textOf(panel(page).locator('[data-assistant-pending-item="columns.status.codes"]'))).includes('8=已退票'),
    await textOf(panel(page).locator('[data-assistant-pending-item="columns.status.codes"]')))
  check('……第一轮卡片上的码值标「已改用另一条提案的值」', (await changeState(page, 'columns.status.codes', card1)) === 'replaced'
        && (await textOf(card1.locator('[data-assistant-change="columns.status.codes"]'))).includes('已改用另一条提案的值'))
  await shot(page, 'catalog-assistant')

  // 保存：带提案对照的版本；别人刚改过（409）就按最新内容重算，停下来让人再看一眼
  const t = state.tables[S1].find((x) => x.name === 'visits')
  writeEntry(t, { ...t.notes, description: item('别人刚补的说明', 'human') })
  const v0 = await version(page)
  await panel(page).locator('[data-assistant-save]').click()
  const save1 = lastSent(sent, /\/visits\/patch$/)?.body
  check('保存：交回 2 项的路径、原样取值和依据，带提案对照的版本 2', JSON.stringify(save1) === JSON.stringify({
    changes: [{ path: 'columns.status.codes', value: { 8: '已退票', 9: '已作废' }, reason: '用户说「8 是退票」' },
      { path: 'valid_filter', value: 'status NOT IN (8, 9)', reason: '用户说「统计时退票和作废的都要排除」' }], if_version: 2,
  }), JSON.stringify(save1))
  const conflict = panel(page).locator('[data-assistant-conflict]')
  check('别人刚改过（409）：写明这张表刚被修改过，已按最新内容重算', await until(async () => (await conflict.count()) === 1)
        && (await textOf(conflict)).includes('这张表刚被修改过') && (await textOf(conflict)).includes('已按最新内容重算'), await textOf(conflict))
  check('……用预览接口重算（只读），没有直接再存一次', count(sent, /\/visits\/patch\/preview$/) === 1 && count(sent, /\/visits\/patch$/) === 1)
  check('……待保存的修改照旧 2 项，没有丢', await pendingCount(page) === 2)
  await panel(page).locator('[data-assistant-save]').click()
  check('再点保存：带最新的版本 3，存上了', await until(async () => count(sent, /\/visits\/patch$/) === 2) && lastSent(sent, /\/visits\/patch$/)?.body?.if_version === 3)
  check('……存上以后：待保存的清空，写明已保存 2 项（第 4 版）', await until(async () => (await panel(page).locator('[data-assistant-saved]').count()) === 1)
        && (await textOf(panel(page).locator('[data-assistant-saved]'))).includes('已保存 2 项修改（第 4 版）') && await panel(page).locator('[data-assistant-pending]').count() === 0,
    await textOf(panel(page).locator('[data-assistant-saved]')))
  check('……表详情换上最新的目录（第 4 版），有效记录条件是新的值', await until(async () => (await version(page)) === 4) && v0 === 2
        && (await textOf(page.locator('[data-field="valid_filter"]'))).includes('status NOT IN (8, 9)'))
  // 保存的修改把依据另存成这一项确认时的依据（服务端的 reason，和备注分开）：表级的项写在值下面，列的一格悬停看得到
  check('……确认时的依据看得到：有效记录条件的值下面写「确认时的依据：用户说……」',
    (await textOf(page.locator('[data-field="valid_filter"] [data-field-reason]'))) === '确认时的依据：用户说「统计时退票和作废的都要排除」',
    await textOf(page.locator('[data-field="valid_filter"]')))
  check('……依据不拼进说明：改了值的有效记录条件不再有说明（剖析核对的排除比例说的是旧条件）',
    await page.locator('[data-field="valid_filter"] [data-field-note]').count() === 0, await textOf(page.locator('[data-field="valid_filter"]')))
  const savedReason = page.locator('[data-column="status"] [data-cell="codes"] [data-cell-reason]')
  check('……码值那一格悬停写确认时的依据', ((await savedReason.getAttribute('title').catch(() => '')) ?? '') === '确认时的依据：用户说「8 是退票」',
    String(await savedReason.getAttribute('title').catch(() => null)))
  check('……那一格没有另挂说明', await page.locator('[data-column="status"] [data-cell="codes"] [data-cell-note]').count() === 0)
  check('……卡片上存了的项标「已保存」，不能再加回', (await changeState(page, 'valid_filter')) === 'saved'
        && await panel(page).locator('[data-assistant-change="valid_filter"] [data-assistant-change-toggle]').count() === 0)
  await cleanCopy(page, '表助手')
  check('只发过表助手、保存、预览的写请求', writes(sent).every((s) => /\/(assistant|patch|patch\/preview)$/.test(s.key)), JSON.stringify(writes(sent).map((s) => s.key)))
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('表助手 · 停止、出错、模型用不了与离开前确认', async () => {
  const state = freshState()
  let release = null
  const held = new Promise((r) => { release = r })
  const replies = {
    assistant: [
      // 起草模型用不了：开流之前就回 409
      json(MODEL_DOWN, 409),
      // 一直不答完：等着点「停止回答」
      async (route) => { await held; await route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' }).catch(() => {}) },
      // 流里出错
      sse([{ type: 'text', delta: '我先看一下这张表' }, { type: 'error', message: '调用模型出错：服务暂时不可用', code: 'assistant_model_error' }]),
      // 有一项提案：之后离开、关闭时要先问
      sse([{ type: 'text', delta: '整理好了：' }, PROPOSAL_1, done()]),
    ],
  }
  const { page, sent, errors, natives, close } = await open(`/data/catalog/${S1}/visits`, { handlers: loopHandlers(state, replies) })
  await until(async () => (await page.locator('[data-catalog-detail="visits"][data-version]').count()) > 0)
  await page.locator('[data-catalog-assistant-open="header"]').click()
  await panel(page).waitFor()

  await say(page, '你好')
  const err = lastTurn(page).locator('[data-assistant-error]')
  check('起草模型用不了（409）：照原样写服务端那句话', (await textOf(err)) === MODEL_DOWN.detail, await textOf(err))
  check('……输入框还能用', !(await input(page).isDisabled()))

  await say(page, '帮我看看这张表', { wait: false })
  check('回答中：面板标为正在回答，发送换成「停止回答」', await until(async () => (await streaming(page)) === 'true')
        && await panel(page).locator('[data-assistant-stop]').isVisible() && (await panel(page).locator('[data-assistant-stop]').getAttribute('aria-label')) === '停止回答')
  check('……对话区标 aria-busy，答完再播报', (await panel(page).locator('[data-assistant-log]').getAttribute('aria-busy')) === 'true')
  await panel(page).locator('[data-assistant-stop]').click()
  check('点「停止回答」：这一轮标「已停止回答」，可以接着说', await until(async () => (await streaming(page)) === 'false')
        && (await textOf(lastTurn(page).locator('[data-assistant-turn-status]'))).includes('已停止回答') && await panel(page).locator('[data-assistant-send]').isVisible())
  release()

  await say(page, '再试一次')
  check('流里出错：已经写出的正文留着，写明这一轮没有完成和原因', (await textOf(lastTurn(page).locator('[data-assistant-text]'))) === '我先看一下这张表'
        && (await textOf(lastTurn(page).locator('[data-assistant-error]'))).includes('调用模型出错：服务暂时不可用'))
  const req = lastSent(sent, /\/assistant$/)?.body
  check('……停止、出错的那几轮也交给服务端（没有正文的写成占位，保持一问一答）',
    req?.messages?.length === 5 && req.messages.map((m) => m.role).join(',') === 'user,assistant,user,assistant,user', JSON.stringify(req?.messages))

  await say(page, '码值 9 是作废')
  check('有待保存的修改', await pendingCount(page) === 2)
  await page.locator('[data-catalog-next]').click()
  check('有待保存的修改时换表：先问一句（表助手有未保存的修改）', await until(async () => (await confirmBox(page).innerText().catch(() => '')).includes('表助手有未保存的修改')),
    await confirmBox(page).innerText().catch(() => ''))
  await confirmBox(page).getByRole('button', { name: '留下' }).or(confirmBox(page).getByRole('button', { name: '取消' })).first().click()
  check('……选留下：还在这张表，待保存的修改还在', await until(async () => (await confirmBox(page).count()) === 0 || !(await confirmBox(page).isVisible()))
        && new URL(page.url()).pathname.endsWith('/visits') && await pendingCount(page) === 2)
  await panel(page).locator('[data-assistant-close]').click()
  check('关闭表助手：有待保存的修改时先问', await until(async () => (await confirmBox(page).innerText().catch(() => '')).includes('关闭表助手')))
  await confirmBox(page).getByRole('button', { name: '放弃并关闭' }).click()
  check('……确认后面板收起，焦点回到入口', await until(async () => (await panel(page).count()) === 0)
        && await until(async () => page.evaluate(() => document.activeElement?.getAttribute('data-catalog-assistant-open') === 'header')))
  check('只发过表助手的请求，没有保存', writes(sent).every((s) => /\/assistant$/.test(s.key)), JSON.stringify(writes(sent).map((s) => s.key)))
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('表助手 · 回答进行中保存、撞上 409 时新到的提案不丢', async () => {
  const state = freshState()
  const t = state.tables[S1].find((x) => x.name === 'visits')
  let releaseStream = null
  const streamGate = new Promise((r) => { releaseStream = r })
  let releasePreview = null
  const previewGate = new Promise((r) => { releasePreview = r })
  // 第二轮回答里的提案：别人改过之后才提的，对照第 3 版
  const GRAIN = {
    type: 'proposal', source: NAME1, source_id: S1, table: 'visits', table_label: '入园记录', version: 3, changes: [
      { path: 'grain', label: '表的粒度', before: '一张门票的一次入园', before_status: 'proposed', after: '一次刷闸', value: '一次刷闸',
        reason: '用户说「一次刷闸记一行」', state: 'change' },
    ],
  }
  const replies = {
    assistant: [
      sse([{ type: 'text', delta: '整理好了：' }, PROPOSAL_1, done()]),
      // 第二轮：等点了保存、撞上 409、正在按最新内容重算时才答出来
      async (route) => { await streamGate; await sse([{ type: 'text', delta: '粒度也改一下：' }, GRAIN, done()])(route).catch(() => {}) },
    ],
    // 重算（预览）：等第二轮的提案到了再答
    preview: [async (route, { key, body }) => { await previewGate; await json(previewOf(findTable(state, key).t, body.changes))(route).catch(() => {}) }],
  }
  const { page, sent, errors, natives, close } = await open(`/data/catalog/${S1}/visits`, { handlers: loopHandlers(state, replies) })
  await until(async () => (await page.locator('[data-catalog-detail="visits"][data-version]').count()) > 0)
  await page.locator('[data-catalog-assistant-open="header"]').click()
  await panel(page).waitFor()
  await say(page, SAY_1)
  check('第一轮：待保存 2 项', await pendingCount(page) === 2)
  // 别人刚改过这张表（第 3 版）；接着问第二轮，回答还没出来时点保存
  writeEntry(t, { ...t.notes, description: item('别人刚补的说明', 'human') })
  await say(page, '一次刷闸记一行', { wait: false })
  await until(async () => (await streaming(page)) === 'true')
  const save = panel(page).locator('[data-assistant-save]')
  await save.click()
  check('回答进行中点保存：带提案对照的第 2 版，撞上 409，按最新内容重算（预览）', await until(async () => count(sent, /\/visits\/patch\/preview$/) === 1)
        && lastSent(sent, /\/visits\/patch$/)?.body?.if_version === 2 && lastSent(sent, /\/visits\/patch\/preview$/)?.body?.changes?.length === 2)
  // 重算还没回来时，第二轮的提案到了
  releaseStream()
  check('重算期间第二轮的提案到了：待保存 3 项', await until(async () => (await pendingCount(page)) === 3))
  check('……保存按钮照旧是「正在保存…」、不能点（提案到了不把正在进行的保存翻回可以点）', await save.isDisabled()
        && (await textOf(save)).includes('正在保存'), await textOf(save))
  releasePreview()
  const conflict = panel(page).locator('[data-assistant-conflict]')
  check('重算回来：写明这张表刚被修改过、已按最新内容重算', await until(async () => (await conflict.count()) === 1))
  check('……重算期间新到的提案（粒度）还在待保存的修改里，没被当成不合法悄悄去掉', await pendingCount(page) === 3
        && await panel(page).locator('[data-assistant-pending-item="grain"]').count() === 1 && (await changeState(page, 'grain')) === 'pending',
    `${await pendingCount(page)} · ${await changeState(page, 'grain')}`)
  check('……没有说有哪几项已无法保存（预览里的都还合法）', await conflict.locator('[data-assistant-problems]').count() === 0)
  await save.click()
  const body = await until(async () => (count(sent, /\/visits\/patch$/) === 2 ? lastSent(sent, /\/visits\/patch$/)?.body : null))
  check('再点保存：带最新的第 3 版，3 项都交上去（含新到的粒度）', body?.if_version === 3 && JSON.stringify(body.changes.map((c) => c.path))
    === JSON.stringify(['columns.status.codes', 'columns.status.meaning', 'grain']), JSON.stringify(body))
  check('……存上了：待保存的清空，写明已保存 3 项（第 4 版）', await until(async () => (await panel(page).locator('[data-assistant-saved]').count()) === 1)
        && (await textOf(panel(page).locator('[data-assistant-saved]'))).includes('已保存 3 项修改（第 4 版）'))
  check('只发过表助手、保存、预览的写请求', writes(sent).every((s) => /\/(assistant|patch|patch\/preview)$/.test(s.key)), JSON.stringify(writes(sent).map((s) => s.key)))
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('表助手 · 开了剖析的源：只读查询与留空列的引导', async () => {
  const state = freshState()
  // 形状照服务端：query 带 truncated（后面还有行时为真，row_count 只是取回的行数）
  const QUERY = { type: 'query', sql: 'SELECT gate_kind, COUNT(*) AS n FROM gates GROUP BY gate_kind', columns: ['gate_kind', 'n', 'gate_code'],
    rows: [['E', 6, '已遮罩'], ['X', 2, '已遮罩']], row_count: 2, truncated: false, error: null }
  const LONG = { type: 'query', sql: 'SELECT gate_code, park_id FROM gates', columns: ['gate_code', 'park_id'],
    rows: Array.from({ length: 10 }, (_, i) => ['已遮罩', i + 1]), row_count: 50, truncated: true, error: null }
  const replies = {
    assistant: [sse([QUERY, { type: 'query', sql: 'SELECT COUNT(*) FROM gates WHERE park_id IS NULL', columns: [], rows: [], row_count: 0, truncated: false, error: '查询超时（10 秒）' },
      LONG, { type: 'text', delta: 'gate_kind 只有 E、X 两种取值，分别是什么意思？' }, done(3)])],
  }
  const { page, sent, errors, close } = await open(`/data/catalog/${S2}/gates`, { handlers: loopHandlers(state, replies) })
  await until(async () => (await page.locator('[data-catalog-detail="gates"][data-version]').count()) > 0)
  const tip = page.locator('[data-catalog-last-draft]')
  check('上次起草有留空的列：顶部提示里有「和助手一起完善」', await until(async () => (await tip.locator('[data-catalog-assistant-open="notice"]').count()) === 1))
  await tip.locator('[data-catalog-assistant-open="notice"]').click()
  await panel(page).waitFor()
  check('开了剖析的源：不写「不能查数据」，写明可以发少量只读查询', await panel(page).locator('[data-assistant-no-profile]').count() === 0
        && (await textOf(panel(page).locator('[data-assistant-profile-on]'))).includes('只读查询'))
  // 留空的列名取那次起草记下的（任务里这张表的 blank_sample），不取「目录里还没有中文名和含义的列」——那里面还有没写出来的 opened_on
  const leadBox = panel(page).locator('[data-assistant-blank]')
  // 开发模式的 StrictMode 会把副作用多跑一遍，取任务可能是两次
  check('开场先取那次起草的任务（从中取起草时记下的留空列名）', await until(async () => count(sent, /draft-jobs\/check-job-old$/) >= 1)
        && await until(async () => (await leadBox.getAttribute('data-assistant-blank').catch(() => null)) === 'draft'))
  const lead = await textOf(leadBox)
  check('开场引导先问留空的列：写明上次起草有 2 列留空，就是 gate_code、gate_kind，不把没写出来的 opened_on 当成留空的列',
    lead === '上次起草有 2 列模型拿不准、留空了：gate_code、gate_kind。可以先从这几列开始。' && !lead.includes('opened_on'), lead)
  const suggest = panel(page).locator('[data-assistant-suggest="blank"]')
  check('……给一个按钮「补上留空的 2 列」', (await textOf(suggest)) === '补上留空的 2 列', await textOf(suggest))
  await suggest.click()
  await until(async () => (await streaming(page)) === 'false' && count(sent, /\/assistant$/) === 1)
  const first = lastSent(sent, /\/assistant$/)?.body?.messages?.[0]?.content ?? ''
  check('……点了就把这 2 列交给助手：点名的正是按钮上说的 2 列，没有别的列', first === '请帮我补上这几列的中文名和含义：gate_code、gate_kind。拿不准的地方先问我。', first)
  const q = lastTurn(page).locator('[data-assistant-query]')
  check('跑过的只读查询：三条，默认收起，摘要写行数（服务端等模型时发的注释行跳过了）', await until(async () => (await q.count()) === 3)
        && !(await q.first().evaluate((el) => el.open)) && (await textOf(q.first().locator('summary'))).includes('查询了数据（2 行）'))
  check('……查询没成功的写明原因', (await textOf(q.nth(1).locator('summary'))).includes('查询没有成功') && (await textOf(q.nth(1))).includes('查询超时（10 秒）'))
  check('……后面还有行的（truncated）：写「取回前 50 行，后面还有」，不说成结果一共 50 行', (await textOf(q.nth(2).locator('summary'))).includes('查询了数据（取回前 50 行，后面还有）'),
    await textOf(q.nth(2).locator('summary')))
  await q.first().locator('summary').click()
  check('……展开：SQL 和前几行，遮罩列的值写「已遮罩」', await until(async () => q.first().locator('[data-assistant-query-sql]').isVisible())
        && (await textOf(q.first().locator('[data-assistant-query-sql]'))).startsWith('SELECT gate_kind')
        && await q.first().locator('[data-assistant-query-rows] tbody tr').count() === 2
        && (await textOf(q.first().locator('[data-assistant-query-rows]'))).includes('已遮罩'))
  await q.nth(2).locator('summary').click()
  check('……后面还有行的展开：写取回前 50 行（后面还有），显示前 10 行', await until(async () => q.nth(2).locator('[data-assistant-query-rows]').isVisible())
        && (await textOf(q.nth(2).locator('[data-assistant-query-rows] caption'))) === '取回前 50 行（后面还有），显示前 10 行',
    await textOf(q.nth(2).locator('[data-assistant-query-rows] caption')))
  check('……写明这一轮查询了 3 次', (await textOf(lastTurn(page).locator('[data-assistant-turn-status]'))).includes('查询 3 次'),
    await textOf(lastTurn(page).locator('[data-assistant-turn-status]')))
  await shot(page, 'catalog-assistant-query')
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('表助手 · 拿不到起草时记下的留空列名', async () => {
  const state = freshState()
  const gates = state.tables[S2].find((t) => t.name === 'gates')
  const leadOf = async (page) => {
    await page.locator('[data-catalog-assistant-open="header"]').click()
    await panel(page).waitFor()
    const box = panel(page).locator('[data-assistant-blank]')
    await until(async () => (await box.getAttribute('data-assistant-blank').catch(() => null)) !== null)
    return { from: await box.getAttribute('data-assistant-blank').catch(() => null), text: await textOf(box) }
  }
  // 那次起草的任务已经不在了（404）：改说目录里还没有中文名和含义的列，按钮上的数字就是点名的列数
  gates.lastDraft = { ...gates.lastDraft, job_id: 'check-job-gone' }
  const replies = { assistant: [sse([{ type: 'text', delta: '好的。' }, done()])] }
  const { page, sent, errors, close } = await open(`/data/catalog/${S2}/gates`, { handlers: loopHandlers(state, replies) })
  await until(async () => (await page.locator('[data-catalog-detail="gates"][data-version]').count()) > 0)
  const gone = await leadOf(page)
  check('任务不在了：不再说这几列是留空的列，改说目录里还有 3 列没有中文名和含义', gone.from === 'catalog'
        && gone.text === '上次起草有 2 列模型拿不准、留空了。目录里还有 3 列没有中文名和含义，例如 opened_on、gate_code、gate_kind，可以先从这些列开始。', gone.text)
  const suggest = panel(page).locator('[data-assistant-suggest="blank"]')
  check('……按钮写「补上没有中文名和含义的 3 列」（不写成「补上留空的 2 列」）', (await textOf(suggest)) === '补上没有中文名和含义的 3 列', await textOf(suggest))
  await suggest.click()
  await until(async () => (await streaming(page)) === 'false' && count(sent, /\/assistant$/) === 1)
  const msg = lastSent(sent, /\/assistant$/)?.body?.messages?.[0]?.content ?? ''
  check('……点名的就是这 3 列', msg === '请帮我补上这几列的中文名和含义：opened_on、gate_code、gate_kind。拿不准的地方先问我。', msg)
  await close()

  // 最近一次停止了、没有新写入：留空列数是再之前那次的，不知道是哪一次——不去取停止的那次任务，改说目录里的
  gates.lastDraft = { status: 'cancelled', job_id: 'check-job-stopped', finished_at: T0, error: null, model_error: null,
    blank_columns: 0, blank_columns_from: 2, parts_written: 0 }
  const again = await open(`/data/catalog/${S2}/gates`, { handlers: loopHandlers(state, {}) })
  await until(async () => (await again.page.locator('[data-catalog-detail="gates"][data-version]').count()) > 0)
  const earlier = await leadOf(again.page)
  check('最近一次停止了：用再之前那次的留空列数，不去取停止的那次任务，改说目录里还没有中文名和含义的列',
    earlier.from === 'catalog' && earlier.text.startsWith('上次起草有 2 列模型拿不准、留空了。目录里还有 3 列没有中文名和含义')
        && count(again.sent, /draft-jobs\/check-job-stopped$/) === 0, `${earlier.text} · ${count(again.sent, /draft-jobs\//)}`)
  check('没有运行时报错', errors.length === 0 && again.errors.length === 0, errors[0] ?? again.errors[0] ?? '')
  await again.close()
})

await section('表助手 · 一次最多保存 200 项', async () => {
  const state = freshState()
  const changes = []
  for (let i = 1; i <= 101; i++) {
    const c = `attr_${String(i).padStart(3, '0')}`
    changes.push({ path: `columns.${c}.label`, before: null, before_status: null, after: `属性 ${i}`, value: `属性 ${i}`, reason: '按列名推断', state: 'change' })
    changes.push({ path: `columns.${c}.meaning`, before: null, before_status: null, after: `促销属性 ${i}`, value: `促销属性 ${i}`, reason: '按列名推断', state: 'change' })
  }
  const replies = {
    assistant: [
      sse([{ type: 'proposal', source_id: S1, table: 'promo_attrs', version: 2, changes }, done()]),
      sse([{ type: 'text', delta: '好的，先说这几列。' }, done()]),
    ],
  }
  const { page, sent, errors, close } = await open(`/data/catalog/${S1}/promo_attrs`, { handlers: loopHandlers(state, replies) })
  await until(async () => (await page.locator('[data-catalog-detail="promo_attrs"][data-version]').count()) > 0)
  await page.locator('[data-catalog-assistant-open="header"]').click()
  await panel(page).waitFor()
  await say(page, '把这些列都补上中文名和含义')
  check('202 项待保存：保存按钮不能点，写明一次最多 200 项', await pendingCount(page) === 202 && await panel(page).locator('[data-assistant-save]').isDisabled()
        && (await textOf(panel(page).locator('[data-assistant-too-many]'))).includes('一次最多保存 200 项'))
  // 待保存的超过 200 项时接着问：服务端一轮最多收 200 项待保存的修改（多了整个请求 422），只交最近的 200 项
  await say(page, '先说 attr_101 是什么')
  const asked = lastSent(sent, /\/assistant$/)?.body?.pending ?? []
  check('……接着问：只交最近的 200 项待保存的修改，这一轮照常答出来', asked.length === 200 && asked[0]?.path === 'columns.attr_002.label'
        && asked.at(-1)?.path === 'columns.attr_101.meaning' && (await lastTurn(page).getAttribute('data-turn-state')) === 'done',
    `${asked.length} · ${asked[0]?.path} · ${asked.at(-1)?.path} · ${await lastTurn(page).getAttribute('data-turn-state')}`)
  check('……界面上的待保存照旧 202 项（只是没全交给助手）', await pendingCount(page) === 202)
  for (const p of ['columns.attr_101.meaning', 'columns.attr_101.label']) {
    await panel(page).locator(`[data-assistant-pending-item="${p}"] [data-assistant-pending-remove]`).click()
  }
  check('去掉 2 项：200 项，可以保存', await until(async () => (await pendingCount(page)) === 200) && !(await panel(page).locator('[data-assistant-save]').isDisabled())
        && await panel(page).locator('[data-assistant-too-many]').count() === 0)
  await panel(page).locator('[data-assistant-save]').click()
  check('……保存：一次交 200 项', await until(async () => count(sent, /\/promo_attrs\/patch$/) === 1) && lastSent(sent, /\/promo_attrs\/patch$/)?.body?.changes?.length === 200)
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('表助手 · 窄屏', async () => {
  const state = freshState()
  const replies = { assistant: [sse([{ type: 'text', delta: TEXT_1.join('') }, PROPOSAL_1, done()])] }
  const { page, errors, close } = await open(`/data/catalog/${S1}/visits`, { handlers: loopHandlers(state, replies), viewport: { width: 390, height: 844 } })
  const overflow = () => page.evaluate(() => {
    const root = document.querySelector('[data-catalog-page]')
    return root ? root.scrollWidth - root.clientWidth : 99
  })
  await until(async () => (await page.locator('[data-catalog-detail="visits"][data-version]').count()) > 0)
  await page.locator('[data-catalog-assistant-open="header"]').click()
  await panel(page).waitFor()
  const box = await panel(page).boundingBox()
  check('390 宽：表助手铺满详情，在视口里，页面不横向滚动', !!box && box.x >= 0 && box.x + box.width <= 391 && await overflow() <= 1,
    `${JSON.stringify(box)} ${await overflow()}`)
  await say(page, SAY_1)
  const inner = await panel(page).evaluate((el) => {
    const log = el.querySelector('[data-assistant-log]')
    return Math.max(el.scrollWidth - el.clientWidth, log ? log.scrollWidth - log.clientWidth : 0)
  })
  check('……回答、提案、待保存的修改都折行，面板不横向滚动', inner <= 1 && await overflow() <= 1 && await pendingCount(page) === 2, String(inner))
  check('……保存按钮看得到', await panel(page).locator('[data-assistant-save]').isVisible())
  await shot(page, 'catalog-assistant-narrow')
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('审阅队列 · 视图、顺序与原因、筛选、进度', async () => {
  const state = { ...freshState(), queue: { [S1]: queueOf() }, usage: { visits: USAGE_RUNS } }
  const { page, sent, errors, natives, close } = await open(`/data/catalog/${S1}`, { handlers: loopHandlers(state) })
  await page.locator('[data-catalog-row]').first().waitFor()
  const tabs = page.locator('[data-catalog-view]')
  check('清单上方有两个视图：表清单、审阅队列，默认表清单', await tabs.count() === 1
        && (await page.locator('[data-catalog-view-tab="list"]').getAttribute('aria-selected')) === 'true'
        && (await textOf(page.locator('[data-catalog-view-tab="review"]'))).includes('审阅队列'))
  await page.locator('[data-catalog-view-tab="review"]').click()
  const queue = page.locator('[data-review-queue]')
  check('切到审阅队列：地址带 view=review，取第一页（50 项、全部）', await until(async () => (await queue.locator('[data-review-item]').count()) === 50)
        && new URL(page.url()).searchParams.get('view') === 'review'
        && JSON.stringify(lastSent(sent, /review-queue$/)?.query) === JSON.stringify({ limit: '50', offset: '0', kind: 'all' }),
    `${page.url()} ${JSON.stringify(lastSent(sent, /review-queue$/)?.query)}`)
  const keys = await queue.locator('[data-review-item]').evaluateAll((els) => els.map((e) => e.getAttribute('data-review-item')))
  check('……照服务端排好的顺序列（常用表里影响数字的在前）', keys.slice(0, 3).join(' | ') === 'visits::valid_filter | visits::dedup | orders::kind', keys.slice(0, 3).join(' | '))
  const first = queue.locator('[data-review-item="visits::valid_filter"]')
  const firstText = await textOf(first)
  check('第一项：写位置、所在表的中文名、值、来源和状态、所在表的使用次数', firstText.includes('表的有效记录条件') && firstText.includes('入园记录')
        && firstText.includes('status NOT IN (9)') && firstText.includes('推断') && firstText.includes('模型起草') && firstText.includes('使用 42 次'), firstText)
  check('……写为什么排在这里（服务端的原话）', (await textOf(first.locator('[data-review-reasons]'))).includes('影响数字的项（有效记录条件）'))
  check('……有效记录条件写剖析核对的排除比例', (await textOf(first.locator('[data-review-note]'))).includes(FILTER_NOTE))
  check('……一共 57 项，还能再载入 7 项', (await textOf(queue.locator('[data-review-total]'))).includes('57 项待确认') && (await textOf(queue.locator('[data-review-more]'))).includes('再载入 7 项'))
  // 位置：服务端的 field_label 只是字段的叫法（「度量类型」「中文名」），列级的项要写出是哪一列，不然成批确认时分不清
  const whereOf = (k) => textOf(queue.locator(`[data-review-item="${k}"] [data-review-where]`))
  const box = (k) => queue.locator(`[data-review-item="${k}"] [data-review-select]`)
  check('列级的项写明是哪一列：「列 ticket_no 的度量类型」「列 attr_001 的中文名」', (await whereOf('visits::columns.ticket_no.measure')) === '列 ticket_no 的度量类型'
        && (await whereOf('promo_attrs::columns.attr_001.label')) === '列 attr_001 的中文名',
    `${await whereOf('visits::columns.ticket_no.measure')} · ${await whereOf('promo_attrs::columns.attr_001.label')}`)
  check('……表级的项写「表的……」，关联关系写指向哪张表', (await whereOf('visits::valid_filter')) === '表的有效记录条件'
        && (await whereOf('orders::relations.r_member')) === '指向 members 的关联关系', `${await whereOf('visits::valid_filter')} · ${await whereOf('orders::relations.r_member')}`)
  check('……复选框的读屏名也写到列：「选择：visits 列 ticket_no 的度量类型」',
    (await box('visits::columns.ticket_no.measure').getAttribute('aria-label')) === '选择：visits 列 ticket_no 的度量类型'
        && (await box('promo_attrs::columns.attr_002.label').getAttribute('aria-label')) === '选择：promo_attrs 列 attr_002 的中文名',
    await box('visits::columns.ticket_no.measure').getAttribute('aria-label'))
  const promoWhere = await queue.locator('[data-review-item^="promo_attrs::"] [data-review-where]').evaluateAll((els) => els.map((e) => e.textContent))
  check('……促销属性表的几十项中文名各写各的列，没有两行一样', promoWhere.length === 44 && new Set(promoWhere).size === promoWhere.length, `${promoWhere.length} · ${new Set(promoWhere).size}`)
  // 进度：三行同一个口径（已确认 ÷（已确认 + 待确认）），常用表写实际几张
  const prog = queue.locator('[data-review-progress]')
  const line = (k) => textOf(prog.locator(`[data-review-progress-${k}]`))
  const pct = (k) => prog.locator(`[data-review-progress-${k}]`).getAttribute('data-review-progress-pct')
  check('进度：常用表写实际几张（被运行用过的 5 张，不写死 20 张），已确认 44%、待确认 150 项', (await line('top')).includes('常用 5 张表')
        && !(await line('top')).includes('20 张') && (await line('top')).includes('已确认 44% · 待确认 150 项') && (await pct('top')) === '44', await line('top'))
  check('……全部表、影响数字的项同一个口径：已确认 18%、待确认 1,180 项；已确认 18%、待确认 56 项', (await line('all')).includes('已确认 18% · 待确认 1,180 项')
        && (await line('facts')).includes('已确认 18% · 待确认 56 项') && (await pct('all')) === '18' && (await pct('facts')) === '18', `${await line('all')} · ${await line('facts')}`)
  check('……悬停写明口径：已驳回的、有确证的不计入', ((await prog.locator('[data-review-progress-all] span').last().getAttribute('title')) ?? '').includes('已驳回的、有确证的（外键约束）不用再确认'))
  // 审完了（待确认 0 项）三行都是 100%：已驳回的、有确证的项不会让「全部表」永远到不了 100%；没有运行用过的表时写明
  state.progress = {
    top_n: 20, top: { tables: 0, items_total: 0, items_confirmed: 0, items_proposed: 0, items_verified: 0 },
    all: { items_total: 40, items_confirmed: 30, items_proposed: 0, items_rejected: 6, items_verified: 4 }, facts: { proposed: 0, confirmed: 9 },
  }
  await queue.locator('[data-review-refresh]').click()
  check('审完了：全部表、影响数字的项都是已确认 100%（6 项驳回、4 项有确证不计入）', await until(async () => (await pct('all')) === '100')
        && (await line('all')).includes('已确认 100% · 待确认 0 项') && (await pct('facts')) === '100', `${await line('all')} · ${await line('facts')}`)
  check('……没有运行用过的表：常用表那一行写「还没有运行用过的表」，不写「常用 20 张表」', (await line('top')).includes('还没有运行用过的表')
        && !(await line('top')).includes('20 张') && !(await line('top')).includes('还没有目录项'), await line('top'))
  state.progress = null
  await queue.locator('[data-review-refresh]').click()
  await until(async () => (await pct('top')) === '44')
  // 筛选
  await queue.locator('[data-review-kind="facts"]').click()
  check('按项筛选「影响数字的项」：请求带 kind=facts，地址也记着', await until(async () => lastSent(sent, /review-queue$/)?.query?.kind === 'facts')
        && new URL(page.url()).searchParams.get('kind') === 'facts' && await until(async () => (await queue.locator('[data-review-item]').count()) === 4))
  await queue.locator('[data-review-table]').selectOption('visits')
  check('按表筛选：请求带 table=visits，地址记成 rtable', await until(async () => lastSent(sent, /review-queue$/)?.query?.table === 'visits')
        && new URL(page.url()).searchParams.get('rtable') === 'visits')
  await goto(page, `/data/catalog/${S1}?view=review&kind=facts&rtable=visits`)
  check('刷新页面：视图和筛选都还在', await until(async () => (await queue.locator('[data-review-item]').count()) === 3)
        && (await queue.locator('[data-review-kind="facts"]').getAttribute('aria-checked')) === 'true'
        && (await queue.locator('[data-review-table]').inputValue()) === 'visits')
  await queue.locator('[data-review-kind="all"]').click()
  await queue.locator('[data-review-table]').selectOption('')
  check('筛选改回全部：又是第一页 50 项', await until(async () => (await queue.locator('[data-review-item]').count()) === 50),
    `${page.url()} · ${await queue.locator('[data-review-item]').count()} · ${await textOf(queue.locator('[data-review-total]'))}`)
  await queue.locator('[data-review-more]').click()
  check('「再载入 7 项」：取第二页（offset=50），接在后面', await until(async () => (await queue.locator('[data-review-item]').count()) === 57)
        && lastSent(sent, /review-queue$/)?.query?.offset === '50')
  // 没有待确认项
  await page.locator('[data-catalog-view-tab="list"]').click()
  check('切回表清单：地址去掉 view，清单还在', await until(async () => page.locator('[data-catalog-list]').isVisible()) && !new URL(page.url()).searchParams.get('view'))
  const openQueue = page.locator('[data-catalog-overview] [data-catalog-open-review]')
  const queueText = await textOf(openQueue)
  await openQueue.click()
  check('表清单的概览里也有「打开审阅队列」', queueText === '打开审阅队列' && await until(async () => new URL(page.url()).searchParams.get('view') === 'review'
        && (await page.locator('[data-catalog-view-tab="review"]').getAttribute('aria-selected')) === 'true'), `${queueText} · ${page.url()}`)
  await page.locator('[data-catalog-view-tab="review"]').press('ArrowLeft')
  check('视图标签可以用方向键切换', await until(async () => !new URL(page.url()).searchParams.get('view') && page.locator('[data-catalog-list]').isVisible())
        && await page.evaluate(() => document.activeElement?.getAttribute('data-catalog-view-tab') === 'list'))
  await shot(page, 'catalog-review')
  await cleanCopy(page, '审阅队列')
  check('没有写请求', writes(sent).length === 0, JSON.stringify(writes(sent).map((s) => s.key)))
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('审阅队列 · 成批确认、冲突、撤销审阅、打开所在表', async () => {
  const state = { ...freshState(), queue: { [S1]: queueOf() }, usage: { visits: USAGE_RUNS }, conflictTables: new Set(['orders']) }
  const { page, sent, errors, natives, close } = await open(`/data/catalog/${S1}?view=review`, { handlers: loopHandlers(state) })
  const queue = page.locator('[data-review-queue]')
  await until(async () => (await queue.locator('[data-review-item]').count()) === 50)
  // 列表头的「N 项待确认」和视图标签上的计数（清单里各表的推断项）：成批审阅之后一起变
  const headTotal = () => textOf(queue.locator('[data-review-total]'))
  const tabCount = async () => Number((await textOf(page.locator('[data-catalog-view-tab="review"]'))).replace(/\D/g, ''))
  const tab0 = await tabCount()
  check('还没审阅：列表头写「57 项待确认」', (await headTotal()) === '57 项待确认' && tab0 > 0, `${await headTotal()} · 标签 ${tab0}`)
  const sel = (k) => queue.locator(`[data-review-item="${k}"] [data-review-select]`)
  await sel('visits::valid_filter').check()
  await sel('visits::dedup').check()
  await sel('orders::kind').check()
  const bar = queue.locator('[data-review-batch]')
  check('选了 3 项：写「已选 3 项」，给确认、驳回', (await textOf(bar)).includes('已选 3 项') && (await textOf(bar.locator('[data-review-action="confirm"]'))) === '确认（3）'
        && (await textOf(bar.locator('[data-review-action="reject"]'))) === '驳回（3）')
  check('……还没审阅过的项不能撤销审阅', await bar.locator('[data-review-action="reset"]').isDisabled())
  await bar.locator('[data-review-action="confirm"]').click()
  check('成批确认：先问一句，写明后果', await until(async () => (await confirmBox(page).innerText().catch(() => '')).includes('确认 3 项推断'))
        && (await confirmBox(page).innerText()).includes('参与 SQL 检查'))
  await confirmBox(page).getByRole('button', { name: '确认 3 项' }).click()
  const batch = await until(async () => lastSent(sent, /review\/batch$/)?.body)
  check('请求逐条带表名、路径、动作和所在表读到的版本', JSON.stringify(batch) === JSON.stringify({ items: [
    { table_name: 'visits', path: 'valid_filter', action: 'confirm', if_version: 2 },
    { table_name: 'visits', path: 'dedup', action: 'confirm', if_version: 2 },
    { table_name: 'orders', path: 'kind', action: 'confirm', if_version: 2 },
  ] }), JSON.stringify(batch))
  const result = (k) => queue.locator(`[data-review-item="${k}"]`).getAttribute('data-review-result').catch(() => null)
  check('逐条的结果原地标出：入园记录的 2 项已确认，订单那项冲突', await until(async () => (await result('orders::kind')) === 'conflict')
        && (await result('visits::valid_filter')) === 'ok' && (await result('visits::dedup')) === 'ok')
  check('……冲突的那一项写明未生效、这张表刚被修改过', (await textOf(queue.locator('[data-review-item="orders::kind"] [data-review-outcome]'))).includes('这张表刚被修改过'))
  check('……确认了的状态换成已确认', (await textOf(queue.locator('[data-review-item="visits::dedup"] [data-review-outcome]'))).includes('已确认'))
  const summary = await textOf(queue.locator('[data-review-summary]'))
  check('……汇总：已确认 2 项；1 项未生效（所在表刚被修改过），请刷新队列后再审阅', summary.includes('已确认 2 项') && summary.includes('1 项未生效（所在表刚被修改过）'), summary)
  check('……清单里那张表的计数跟着重取', await until(async () => count(sent, /^GET \/datasources\/check-loop-scenic\/catalog$/) >= 2))
  check('……审阅进度跟着重取', count(sent, /review-progress$/) >= 2)
  check('……列表头跟着减：确认了的 2 项不再待确认、冲突的那项照算（57 → 55），和视图标签上的计数一起少 2',
    await until(async () => (await headTotal()) === '55 项待确认') && await until(async () => (await tabCount()) === tab0 - 2),
    `${await headTotal()} · 标签 ${tab0} → ${await tabCount()}`)
  // 撤销审阅：只发这一页上审阅成功的，带这张表写入后的版本
  await sel('visits::valid_filter').check()
  await sel('visits::dedup').check()
  await bar.locator('[data-review-action="reset"]').click()
  const undo = await until(async () => (count(sent, /review\/batch$/) === 2 ? lastSent(sent, /review\/batch$/)?.body : null))
  check('撤销审阅：发这 2 项的撤销，带写入后的版本 3', JSON.stringify(undo) === JSON.stringify({ items: [
    { table_name: 'visits', path: 'valid_filter', action: 'reset', if_version: 3 },
    { table_name: 'visits', path: 'dedup', action: 'reset', if_version: 3 },
  ] }), JSON.stringify(undo))
  check('……回到推断，标「已撤销审阅」', await until(async () => (await textOf(queue.locator('[data-review-item="visits::dedup"] [data-review-outcome]'))).includes('已撤销审阅')))
  check('……列表头加回去：又是「57 项待确认」，视图标签也回到原来的数', await until(async () => (await headTotal()) === '57 项待确认')
        && await until(async () => (await tabCount()) === tab0), `${await headTotal()} · 标签 ${await tabCount()}`)

  // 打开所在表：右栏是这张表的详情，滚到那一项、标出来，并列出用过这张表的运行
  await queue.locator('[data-review-item="visits::dedup"] [data-review-open]').click()
  check('点开一项：地址带 item，右栏是入园记录的详情，左栏还是队列', await until(async () => (await page.locator('[data-catalog-detail="visits"][data-version]').count()) === 1)
        && new URL(page.url()).searchParams.get('item') === 'dedup' && new URL(page.url()).searchParams.get('view') === 'review' && await queue.isVisible())
  check('……那一项标出来', await until(async () => (await page.locator('[data-review-focus="dedup"]').count()) === 1))
  const usage = page.locator('[data-table-usage]')
  check('……用过这张表的运行：3 次，新的在前，点得进运行记录', await until(async () => (await usage.locator('[data-usage-run]').count()) === 3)
        && (await usage.locator('[data-usage-run]').first().getAttribute('data-usage-run')) === 'run-check-0003'
        && (await usage.locator('[data-usage-run] a').first().getAttribute('href')) === '/runs/run-check-0003'
        && (await textOf(usage.locator('[data-usage-run]').first())).includes('每日入园统计'))
  check('……请求带 limit=20', lastSent(sent, /\/visits\/usage$/)?.query?.limit === '20')
  // 两个数口径不同：表头「使用 42 次」按查询算，这里按运行算（3 次运行），各写各的单位，悬停说明为什么不一样
  const usageCount = usage.locator('[data-table-usage-count]')
  check('……写「共 3 次运行」（按运行算），悬停说明和表头的使用次数（按查询算）不是一个数', (await textOf(usageCount)) === '共 3 次运行'
        && ((await usageCount.getAttribute('title')) ?? '').includes('表头的使用次数按查询算'), await textOf(usageCount))
  // 在详情里单项确认：队列里那一项跟着标已确认
  await page.locator('[data-item-mark="dedup"]').first().click()
  await page.locator('[data-item-panel] [data-review-action="confirm"]').click()
  check('在详情里确认那一项：队列里跟着标「已确认」', await until(async () => (await textOf(queue.locator('[data-review-item="visits::dedup"] [data-review-outcome]'))).includes('已确认')))
  check('……列表头跟着少一项：「56 项待确认」', await until(async () => (await headTotal()) === '56 项待确认'), await headTotal())
  // 刷新：审阅过的移除
  await queue.locator('[data-review-refresh]').click()
  check('刷新队列：已确认的那一项移除', await until(async () => (await queue.locator('[data-review-item="visits::dedup"]').count()) === 0))
  check('……服务端的总数和刷新前列表头写的一样：「56 项待确认」', (await headTotal()) === '56 项待确认', await headTotal())
  await cleanCopy(page, '成批审阅')
  check('只发过成批审阅、单项审阅的写请求', writes(sent).every((s) => /\/review(\/batch)?$/.test(s.key)), JSON.stringify(writes(sent).map((s) => s.key)))
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('审阅队列 · 窄屏', async () => {
  const state = { ...freshState(), queue: { [S1]: queueOf() } }
  const { page, errors, close } = await open(`/data/catalog/${S1}?view=review`, { handlers: loopHandlers(state), viewport: { width: 390, height: 844 } })
  const overflow = () => page.evaluate(() => {
    const root = document.querySelector('[data-catalog-page]')
    return root ? root.scrollWidth - root.clientWidth : 99
  })
  const queue = page.locator('[data-review-queue]')
  check('390 宽：只显示队列，没有横向滚动', await until(async () => (await queue.locator('[data-review-item]').count()) === 50) && await overflow() <= 1, String(await overflow()))
  await queue.locator('[data-review-item="visits::valid_filter"] [data-review-open]').click()
  check('……点开一项：只显示详情，有「返回」', await until(async () => (await page.locator('[data-catalog-detail="visits"][data-version]').count()) === 1)
        && !(await queue.isVisible()) && await page.locator('[data-catalog-detail-back]').isVisible())
  await page.locator('[data-catalog-detail-back]').click()
  check('……返回：回到队列', await until(async () => queue.isVisible()))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('导出导入 · 导出文件、导入先预览再写入', async () => {
  const state = freshState()
  const replies = {}
  const { page, sent, errors, natives, close } = await open(`/data/catalog/${S1}`, { handlers: loopHandlers(state, replies) })
  await page.locator('[data-catalog-row]').first().waitFor()
  const exp = page.locator('[data-catalog-export]')
  check('页头有「导出目录」「导入目录」', await exp.count() === 1 && (await exp.getAttribute('aria-label')) === '导出目录'
        && (await page.locator('[data-catalog-import]').getAttribute('aria-label')) === '导入目录')
  const [download] = await Promise.all([page.waitForEvent('download', { timeout: 8000 }), exp.click()])
  check('导出：下载的文件名同服务端（「<数据源名>-数据目录.json」）', download.suggestedFilename() === `${NAME1}-数据目录.json`, download.suggestedFilename())
  const file = JSON.parse(await (await import('node:fs/promises')).readFile(await download.path(), 'utf8'))
  check('……文件原样存下（导出格式、全部表）', file.format === 'agentlab-catalog' && Object.keys(file.tables).length === 4)

  // 导入：选文件 → 预览 → 确认导入
  await page.locator('[data-catalog-import]').click()
  const dlg = page.locator('[data-import-dialog]')
  check('「导入目录」：打开对话框，先选文件，写明导入的规矩', await until(async () => (await dlg.getAttribute('data-step').catch(() => '')) === 'choose')
        && (await textOf(dlg)).includes('以这里为准') && (await textOf(dlg)).includes('只填补'))
  check('……没选文件时不能预览', await page.locator('[data-import-preview]').isDisabled())
  await dlg.locator('[data-import-file]').setInputFiles({ name: 'broken.json', mimeType: 'application/json', buffer: Buffer.from('{not json') })
  check('文件不是 JSON：就地写明，不发请求', await until(async () => (await textOf(dlg.locator('[data-import-error]'))).includes('文件不是有效的 JSON'))
        && count(sent, /import\/preview$/) === 0)
  await dlg.locator('[data-import-file]').setInputFiles({ name: 'other-数据目录.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(IMPORT_FILE)) })
  check('……换成导出的文件：写明来自哪个数据源，起草要求默认一起导入', await until(async () => (await textOf(dlg.locator('[data-import-file-meta]'))).includes('zzloopother'))
        && await dlg.locator('[data-import-guidance] input').isChecked() && (await dlg.locator('[data-import-error]').count()) === 0)
  await page.locator('[data-import-preview]').click()
  check('预览：交回文件内容和要不要导入起草要求（只读接口）', await until(async () => (await dlg.getAttribute('data-step')) === 'preview')
        && JSON.stringify(lastSent(sent, /import\/preview$/)?.body) === JSON.stringify({ content: IMPORT_FILE, include_guidance: true }))
  check('……合计：2 张表，新增 1、更新 1、保留 5、冲突 1、跳过 1', (await textOf(dlg.locator('[data-import-totals]'))).includes('2 张表：新增 1 项、更新 1 项、保留 5 项、冲突 1 项、跳过 1 项'),
    await textOf(dlg.locator('[data-import-totals]')))
  check('……逐表的计数；名字不一样的写明对上了哪张', (await textOf(dlg.locator('[data-import-table="ORDERS"]'))).includes('对上「orders」')
        && await dlg.locator('[data-import-table="visits"] [data-import-count="keep"]').innerText() === '4')
  const conflict = await textOf(dlg.locator('[data-import-conflict]'))
  check('……冲突逐条列出：位置、文件中的值、这里的值、原因', conflict.includes('表的中文名') && conflict.includes('入园流水') && conflict.includes('入园记录')
        && conflict.includes('这里已确认，以这里为准'), conflict)
  check('……跳过的写界面上的说法和原因，对不上的表列出来', (await textOf(dlg.locator('[data-import-skipped]'))).includes('列 gate_code 的中文名：表结构里没有列 gate_code')
        && (await textOf(dlg.locator('[data-import-unmatched]'))).includes('legacy_tickets'))
  check('……起草要求：这里已有，保留不覆盖', (await textOf(dlg.locator('[data-import-guidance-action]'))).includes('保留这个数据源现有的'))
  check('……预览不写库', count(sent, /catalog\/import$/) === 0)
  check('……预览里不写版本（服务端的预览也带 version，只有导入的结果才写第几版）', !(await textOf(dlg.locator('[data-import-table="visits"]'))).includes('第 2 版'),
    await textOf(dlg.locator('[data-import-table="visits"]')))
  await shot(page, 'catalog-import-preview')
  const lists = count(sent, /^GET \/datasources\/check-loop-scenic\/catalog$/)
  await page.locator('[data-import-confirm]').click()
  check('确认导入：交同一份内容，结果写明写入了几张表、各是第几版', await until(async () => (await dlg.getAttribute('data-step')) === 'done')
        && JSON.stringify(lastSent(sent, /catalog\/import$/)?.body) === JSON.stringify({ content: IMPORT_FILE, include_guidance: true })
        && (await textOf(dlg.locator('[data-import-table="visits"]'))).includes('第 3 版'), await textOf(dlg))
  check('……目录清单重取', await until(async () => count(sent, /^GET \/datasources\/check-loop-scenic\/catalog$/) > lists))
  await page.locator('[data-import-close]').click()
  check('……「完成」关上对话框', await until(async () => (await dlg.count()) === 0))

  // 起草任务进行中（409）、文件格式不对（422）：照原样写服务端那句话，留在这一步
  replies.importPreview = [json({ detail: '文件不是导出的数据目录：缺少 format（应为 agentlab-catalog）' }, 422)]
  replies.import = [json(BUSY, 409)]
  await page.locator('[data-catalog-import]').click()
  await dlg.locator('[data-import-file]').setInputFiles({ name: 'x.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify({ tables: {} })) })
  await page.locator('[data-import-preview]').click()
  check('文件格式不对（422）：写服务端那句话，留在选文件这一步', await until(async () => (await textOf(dlg.locator('[data-import-error]'))).includes('文件不是导出的数据目录'))
        && (await dlg.getAttribute('data-step')) === 'choose')
  await dlg.locator('[data-import-file]').setInputFiles({ name: 'other.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(IMPORT_FILE)) })
  await dlg.locator('[data-import-guidance] input').uncheck()
  await page.locator('[data-import-preview]').click()
  await until(async () => (await dlg.getAttribute('data-step')) === 'preview')
  check('不导入起草要求：请求里写 false', lastSent(sent, /import\/preview$/)?.body?.include_guidance === false)
  await page.locator('[data-import-confirm]').click()
  check('有起草任务在进行（409）：写服务端那句话，留在预览', await until(async () => (await textOf(dlg.locator('[data-import-error]'))).includes('正在起草数据目录'))
        && (await dlg.getAttribute('data-step')) === 'preview')
  await cleanCopy(page, '导入预览')
  check('只发过导入的写请求（预览、导入）', writes(sent).every((s) => /catalog\/import(\/preview)?$/.test(s.key)), JSON.stringify(writes(sent).map((s) => s.key)))
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('导出导入 · 导入结果只数真正写进去的表', async () => {
  // 文件里的两张表都对得上：visits 有一项新增（写进去，第 2 版 → 第 3 版）；ORDERS 文件里的项这里都有了（没有变化，还是第 2 版）。
  // 第二次导入两张表都没有变化。合计按服务端的写法：tables 是对上的表数，不是写进去的表数
  const report = (visitsAdd, after) => ({
    ...clone(IMPORT_PREVIEW),
    tables: [
      { ...clone(IMPORT_PREVIEW.tables[0]), add: visitsAdd, version: after && visitsAdd ? 3 : 2 },
      { ...clone(IMPORT_PREVIEW.tables[1]), update: 0, keep: 2, version: 2 },
    ],
    totals: { tables: 2, add: visitsAdd, update: 0, keep: 6, conflicts: 1, skipped: 1 },
  })
  const replies = {
    importPreview: [json(report(1, false)), json(report(0, false))],
    import: [json(report(1, true)), json(report(0, true))],
  }
  const { page, sent, errors, natives, close } = await open(`/data/catalog/${S1}`, { handlers: loopHandlers(freshState(), replies) })
  await page.locator('[data-catalog-row]').first().waitFor()
  const dlg = page.locator('[data-import-dialog]')
  const importOnce = async () => {
    await page.locator('[data-catalog-import]').click()
    await until(async () => (await dlg.getAttribute('data-step').catch(() => '')) === 'choose')
    await dlg.locator('[data-import-file]').setInputFiles({ name: 'other.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(IMPORT_FILE)) })
    await until(async () => (await textOf(dlg.locator('[data-import-file-meta]'))).includes('zzloopother'))
    await page.locator('[data-import-preview]').click()
    await until(async () => (await dlg.getAttribute('data-step')) === 'preview')
    await page.locator('[data-import-confirm]').click()
    return until(async () => (await dlg.getAttribute('data-step')) === 'done')
  }
  check('导入完成', await importOnce())
  const doneText = await textOf(dlg.locator('[data-import-done]'))
  check('合计只数真正写进去的表：「写入 1 张表」，没有变化的 ORDERS 不算', doneText.includes('写入 1 张表：新增 1 项、更新 0 项')
        && !doneText.includes('写入 2 张表') && doneText.includes('冲突 1 项以这里为准'), doneText)
  await shot(page, 'catalog-import-written')
  check('……逐表：写进去的写第几版，没有变化的写「没有变化」', (await textOf(dlg.locator('[data-import-table="visits"]'))).includes('第 3 版')
        && (await textOf(dlg.locator('[data-import-table="ORDERS"]'))).includes('没有变化')
        && !(await textOf(dlg.locator('[data-import-table="ORDERS"]'))).includes('第 2 版'),
    `${await textOf(dlg.locator('[data-import-table="visits"]'))} · ${await textOf(dlg.locator('[data-import-table="ORDERS"]'))}`)
  await page.locator('[data-import-close]').click()
  await until(async () => (await dlg.count()) === 0)
  check('再导入一次，两张表都没有变化', await importOnce())
  const nothing = await textOf(dlg.locator('[data-import-done]'))
  await shot(page, 'catalog-import-nothing')
  check('……一张都没写进去：说清「没有需要写入的内容」，不写「写入 2 张表」', nothing.includes('没有需要写入的内容') && !nothing.includes('写入 2 张表')
        && !nothing.includes('新增 0 项'), nothing)
  check('……两张表都写「没有变化」', (await textOf(dlg.locator('[data-import-table="visits"]'))).includes('没有变化')
        && (await textOf(dlg.locator('[data-import-table="ORDERS"]'))).includes('没有变化'))
  await cleanCopy(page, '导入结果')
  check('只发过导入的写请求（预览、导入）', writes(sent).every((s) => /catalog\/import(\/preview)?$/.test(s.key)), JSON.stringify(writes(sent).map((s) => s.key)))
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('表详情 · 确认时的依据和人工确认的项的说明', async () => {
  // 保存修改提案时记下的依据（项上的 reason，界面上叫「确认时的依据」）和备注（note：起草的依据、剖析的测量、双盲复算的
  // 出处）分开写：表级的项写在值下面各一行，列的一格、关联关系各一个悬停的小图标，读屏念「确认时的依据：……」「说明：……」；
  // 状态弹层里也写依据。只在已确认的项上就近写，驳回的只在弹层里写。推断的项照旧只有有效记录条件、去重规则在值下面写说明
  const state = freshState()
  const visits = state.tables[S1].find((t) => t.name === 'visits')
  const LABEL_REASON = '使用者说：这张表一次刷闸记一行'
  const FILTER_REASON = '使用者说：只算有效的'
  const KEYS_REASON = '使用者说：票号不会重复'
  const UNIT_REASON = '使用者说：入园人数按人计'
  const DATE_NOTE = '双盲复算（运行 run-check-0003，节点「复核入园人数」）：审核人在指标「入园人数」上采用了盲算的写法'
  const CODES_NOTE = '数据剖析：整表 12,345 行，1 个取值'
  const CODES_REASON = '使用者说：1 是已入园'
  const REL_NOTE = '数据剖析：抽样 2,000 行，覆盖率 98%'
  const REL_REASON = '使用者说：每条入园记录对应一个景区'
  visits.notes.label = item('入园记录', 'human', undefined, { reason: LABEL_REASON })
  // 值不变、保存即确认的模型起草项：起草时的说明照旧，依据另写一行
  visits.notes.valid_filter = item('status NOT IN (9)', 'llm', 'confirmed', { note: FILTER_NOTE, reason: FILTER_REASON })
  visits.notes.grain = item('一张门票的一次入园', 'llm', 'proposed', { note: '依据：表名和票号列' })
  visits.notes.keys = item(['ticket_no'], 'llm', 'rejected', { reason: KEYS_REASON })
  visits.notes.columns.visitor_count.unit = item('人', 'human', undefined, { reason: UNIT_REASON })
  visits.notes.columns.visitor_count.label = item('入园人数', 'llm', 'proposed', { note: '依据：列名' })
  // 双盲复算写进来的人工确认项：只有备注（出处），没有确认时的依据
  visits.notes.business_date = item({ column: 'visit_time' }, 'human', undefined, { note: DATE_NOTE })
  visits.notes.columns.status.codes = item({ 1: '已入园' }, 'profile', 'confirmed', { note: CODES_NOTE, reason: CODES_REASON })
  visits.notes.relations = [{ id: 'r_park', columns: ['park_id'], to_table: 'parks', to_columns: ['id'], cardinality: 'many_to_one', coverage: 0.98,
    source: 'human', status: 'confirmed', updated_at: T0, note: REL_NOTE, reason: REL_REASON }]
  const { page, errors, close } = await open(`/data/catalog/${S1}/visits`, { handlers: loopHandlers(state) })
  await until(async () => (await page.locator('[data-catalog-detail="visits"][data-version]').count()) > 0)
  await shot(page, 'catalog-confirmed-notes')
  const field = (f, part) => page.locator(`[data-field="${f}"] [data-field-${part}]`)
  check('表级的项：值下面写确认时的依据（中文名）', (await textOf(field('label', 'reason'))) === `确认时的依据：${LABEL_REASON}`,
    await textOf(page.locator('[data-field="label"]')))
  check('……没有备注的不另写说明', await field('label', 'note').count() === 0)
  check('依据和说明分开写：有效记录条件下面一行依据、一行起草时的说明', (await textOf(field('valid_filter', 'reason'))) === `确认时的依据：${FILTER_REASON}`
        && (await textOf(field('valid_filter', 'note'))) === `说明：${FILTER_NOTE}`, await textOf(page.locator('[data-field="valid_filter"]')))
  check('人工确认的项只有备注（双盲复算的出处）：写说明，不写依据（业务日期）', (await textOf(field('business_date', 'note'))) === `说明：${DATE_NOTE}`
        && await field('business_date', 'reason').count() === 0, await textOf(page.locator('[data-field="business_date"]')))
  check('……推断的表级项照旧不在值下面写（粒度）', await field('grain', 'note').count() === 0 && await field('grain', 'reason').count() === 0)
  check('……驳回的项不在值下面写依据（业务主键）', await field('keys', 'reason').count() === 0, await textOf(page.locator('[data-field="keys"]')))
  const cell = (c, f, part) => page.locator(`[data-column="${c}"] [data-cell="${f}"] [data-cell-${part}]`)
  const titleOf = async (loc) => (await loc.getAttribute('title').catch(() => null)) ?? ''
  check('列的一格：悬停写确认时的依据（单位）', (await titleOf(cell('visitor_count', 'unit', 'reason'))) === `确认时的依据：${UNIT_REASON}`,
    await titleOf(cell('visitor_count', 'unit', 'reason')))
  check('……读屏念「确认时的依据：……」', (await cell('visitor_count', 'unit', 'reason').innerText().catch(() => '')).includes(`确认时的依据：${UNIT_REASON}`))
  check('……没有备注的那一格不挂说明的图标', await cell('visitor_count', 'unit', 'note').count() === 0)
  check('依据和说明各一个图标（码值：剖析的说明、确认时的依据）', (await titleOf(cell('status', 'codes', 'reason'))) === `确认时的依据：${CODES_REASON}`
        && (await titleOf(cell('status', 'codes', 'note'))) === `说明：${CODES_NOTE}`)
  check('……推断的列级项不加（中文名）', await cell('visitor_count', 'label', 'note').count() === 0 && await cell('visitor_count', 'label', 'reason').count() === 0)
  if (SHOTS) {
    await page.locator('[data-column="visitor_count"]').scrollIntoViewIfNeeded()
    await shot(page, 'catalog-confirmed-notes-columns')
    await page.locator('[data-relation="r_park"]').scrollIntoViewIfNeeded()
    await shot(page, 'catalog-confirmed-notes-relations')
  }
  check('关联关系：依据、测量的说明各悬停一个', (await titleOf(page.locator('[data-relation="r_park"] [data-relation-reason]'))) === `确认时的依据：${REL_REASON}`
        && (await titleOf(page.locator('[data-relation="r_park"] [data-relation-note]'))) === `说明：${REL_NOTE}`)
  // 状态弹层：依据单独一块，写明是确认时的依据；驳回的项也在这里看得到当初确认时的依据
  const popover = async (path) => {
    await page.locator(`[data-item-mark="${path}"]`).first().click()
    const box = page.locator(`[data-item-panel="${path}"]`)
    await box.waitFor({ timeout: 2000 }).catch(() => {})
    const text = await textOf(box.locator('[data-item-reason]'))
    await page.keyboard.press('Escape')
    await box.waitFor({ state: 'detached', timeout: 2000 }).catch(() => {})
    return text
  }
  check('状态弹层里写确认时的依据（中文名）', (await popover('label')) === `确认时的依据：${LABEL_REASON}`)
  check('……驳回的项只在弹层里写（业务主键）', (await popover('keys')) === `确认时的依据：${KEYS_REASON}`)
  check('……没有依据的项弹层里不写（粒度）', (await popover('grain')) === '')
  await cleanCopy(page, '确认时的依据')
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('表助手 · 改前是已驳回的值', async () => {
  // 单位「元」被驳回过：助手提「人」，改前写「已驳回：元」（不是「未填写」），和导入预览「目标里这一项已经驳回」一致
  const state = freshState()
  const visits = state.tables[S1].find((t) => t.name === 'visits')
  visits.notes.columns.visitor_count.unit = item('元', 'llm', 'rejected')
  const PROPOSAL_R = {
    type: 'proposal', source: NAME1, source_id: S1, table: 'visits', table_label: '入园记录', version: 2, changes: [
      { path: 'columns.visitor_count.unit', label: '列 visitor_count 的单位', before: '元', before_status: 'rejected', after: '人', value: '人',
        reason: '使用者说：入园人数按人计', state: 'change' },
      { path: 'kind', label: '表的表类型', before: null, before_status: null, after: 'fact', value: 'fact', reason: '使用者说：一行一次入园', state: 'change' },
    ],
  }
  const replies = { assistant: [sse([{ type: 'text', delta: '好的，单位按人计。' }, PROPOSAL_R, done()])] }
  const { page, sent, errors, close } = await open(`/data/catalog/${S1}/visits`, { handlers: loopHandlers(state, replies) })
  await until(async () => (await page.locator('[data-catalog-detail="visits"][data-version]').count()) > 0)
  await page.locator('[data-catalog-assistant-open="header"]').click()
  await until(async () => panel(page).isVisible())
  await say(page, '入园人数按人计')
  const unit = panel(page).locator('[data-assistant-change="columns.visitor_count.unit"]')
  await shot(page, 'catalog-assistant-rejected')
  check('提案卡片的改前：「已驳回：元」，不写「未填写」', (await textOf(unit.locator('[data-change-before]'))) === '已驳回：元', await textOf(unit))
  check('……原来没有的照旧写「未填写」', (await textOf(panel(page).locator('[data-assistant-change="kind"] [data-change-before]'))) === '未填写')
  const tray = panel(page).locator('[data-assistant-pending-item="columns.visitor_count.unit"]')
  check('待保存的修改里同样写「改前 已驳回：元」', (await textOf(tray)).includes('改前 已驳回：元'), await textOf(tray))
  await panel(page).locator('[data-assistant-save]').click()
  check('保存：交回原样取值和依据', await until(async () => count(sent, /\/visits\/patch$/) === 1)
        && JSON.stringify(lastSent(sent, /\/visits\/patch$/)?.body?.changes?.[0]) === JSON.stringify({ path: 'columns.visitor_count.unit', value: '人', reason: '使用者说：入园人数按人计' }))
  check('……存上以后那一格是人工确认的「人」，悬停写确认时的依据', await until(async () => (await version(page)) === 3)
        && (await textOf(page.locator('[data-column="visitor_count"] [data-cell="unit"]'))).includes('人')
        && ((await page.locator('[data-column="visitor_count"] [data-cell="unit"] [data-cell-reason]').getAttribute('title').catch(() => '')) ?? '') === '确认时的依据：使用者说：入园人数按人计')
  await cleanCopy(page, '改前是已驳回的值')
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('防错事实的提议 · 表详情里的说明', async () => {
  const state = freshState()
  const { page, errors, close } = await open(`/data/catalog/${S1}/visits`, { handlers: loopHandlers(state) })
  await until(async () => (await page.locator('[data-catalog-detail="visits"][data-version]').count()) > 0)
  check('有效记录条件的推断项：值下面写剖析核对的排除比例', (await textOf(page.locator('[data-field="valid_filter"] [data-field-note]'))).includes(FILTER_NOTE),
    await textOf(page.locator('[data-field="valid_filter"]')))
  check('去重规则的推断项：写起草的依据和没有核对的原因', (await textOf(page.locator('[data-field="dedup"] [data-field-note]'))).includes('没有核对排除比例'))
  check('别的项不在值下面写说明', await page.locator('[data-field="grain"] [data-field-note]').count() === 0)
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await browser.close()

const after = await realCounts()
check('真库里的数据源和目录数量没有变化', JSON.stringify(after) === JSON.stringify(before), `${JSON.stringify(before)} → ${JSON.stringify(after)}`)
check('检查用的假数据源没有写进真库', !(await (await fetch(`${API}/datasources`)).json()).some((s) => NAMES.includes(s.name)))

console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 数据目录闭环全部通过')
process.exit(failed ? 1 : 0)
