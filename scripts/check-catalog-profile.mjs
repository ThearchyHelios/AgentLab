// 数据剖析（数据目录页的「数据剖析」、表清单的「剖析」一列、表详情的「数据剖析」一节、数据源的剖析设置）的交互回归。
//
// 剖析会对业务库发查询，是服务端逐表进行的后台任务（/catalog/profile-jobs）。这里查的是正常路径的截图看不出来的那些：
// 「开始前的范围和个数是不是按清单算的」「预算是不是按表写的、估了这次最多几条查询」「明说只读、一条一条发、可以停」
// 「同源有起草任务时是不是写排队」「进行中每张表到哪了（各类检查几项、发了几条查询、正在做哪一项）」「停得下来、停下来
// 写清做完的保留了什么」「展开一张表看得到检查清单和发过的查询（字面量换成了 ?）」「结束后能按有发现 / 有跳过 / 失败筛、
// 只重试没做完的表」「止损、服务重启的原因照实写（进程被杀时没来得及收尾的那张表点名，不说做完的已写进目录）」
// 「各种 409 各有下一步」「表清单的剖析一列和需要剖析的筛选」「表详情的一节和剖析这张表」「设置的两项新字段、被拒时报错
// 落到那一项」「起草排在剖析后面时怎么说」「展开进行中的表时只在一项检查开始、结束时重取，不跟着查询数每次轮询都取」。
//
// 写法同 check-catalog：数据源列表、目录清单、单表目录、剖析任务、起草任务都用 page.route 伪造（虚构的网店业务库，表名
// orders、customers、visits……都是通用的假名字），任务怎么往前走由检查自己推（settle、finish、stopJob），页面每 1.5 秒
// 取一次，看到的就是推到的那一步。没配的写请求一律拦成 503，不写库、更不会发到业务库；最后核对真库里的数据源和目录
// 数量没有变化。
// 跑之前前后端都得起着（./scripts/dev.sh），默认连 5273 / 8000；对别的实例跑时带上地址：
//   AGENTLAB_WEB=http://localhost:<前端端口> AGENTLAB_API=http://localhost:<后端端口>/api node scripts/check-catalog-profile.mjs
// 只跑其中几节：CHECK_ONLY=设置,409 node scripts/check-catalog-profile.mjs（按节名包含匹配）。
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
/** 夹具里另一个人建的剖析任务 */
const OTHER = '检查脚本乙'

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
  // 关之前先撤掉拦截、忽略还在路上的那几条：满负荷并行时，取真实返回的拦截可能在页面关掉之后才回来
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
// 夹具：虚构的网店业务库（订单、客户、访问记录、商品、退款、一张空的日志表、一个视图、一张没有可剖析项的配置表、一张只剩
// 目录的旧表）、一个没开剖析的库（剖析设置里还留着旧的两个键）、一个导入表格的源。名字、数字全是假的
// ---------------------------------------------------------------------------
const isoNow = () => new Date().toISOString()
const ago = (days, hours = 0) => new Date(Date.now() - days * 86_400_000 - hours * 3_600_000).toISOString()
const T0 = ago(3)
const clone = (x) => JSON.parse(JSON.stringify(x))
const item = (value, source = 'llm', status = 'proposed') => ({ value, source, status, updated_at: T0 })
const col = (name, type = 'INTEGER', extra = {}) => ({ name, type, pk: false, not_null: false, comment: null, ...extra })
const pk = (name = 'id') => col(name, 'INTEGER', { pk: true, not_null: true })

const S1 = 'check-prof-shop'
const S2 = 'check-prof-off'
const S3 = 'check-prof-upload'
const NAME1 = 'zzprofshop'
const NAME2 = 'zzprofoff'
const NAME3 = 'zzprofupload'

/**
 * 一张表。plan：按当前目录和表结构这张表要做的检查（关系写「列 → 表.列」，码值、日期写列名），profile_checks 就是它的项数；
 * newRelations：剖析之后目录里又出现了待核实的关系（profile_due_reason 为 new_relations）
 */
const table = (name, usage, columns, plan, extra = {}) => ({
  name, usage, columns, plan: { relation: [], codes: [], date: [], ...plan }, notes: extra.notes ?? null, version: extra.notes ? 2 : 0,
  is_view: false, missing: false, newRelations: false, updated_at: extra.notes ? T0 : null, updated_by: extra.notes ? '审核甲' : null, ...extra,
})
const checksOf = (t) => (t.missing || t.is_view ? 0 : t.plan.relation.length + t.plan.codes.length + t.plan.date.length)

const ORDERS_PLAN = {
  relation: ['customer_id → customers.id', 'product_id → products.id', 'store_id → stores.id', 'coupon_id → coupons.id'],
  codes: ['status', 'channel', 'pay_type', 'region', 'level', 'source'],
  date: ['created_at'],
}
function shopTables() {
  return [
    table('orders', 40, [pk(), col('order_no', 'TEXT'), col('customer_id'), col('product_id'), col('store_id'), col('coupon_id'), col('status'),
      col('channel', 'TEXT'), col('pay_type'), col('region', 'TEXT'), col('level'), col('source', 'TEXT'), col('created_at', 'TEXT'), col('amount', 'REAL')],
    ORDERS_PLAN, { notes: {
      label: item('订单'),
      columns: {
        status: { codes: item({ 1: '已下单', 2: '', 9: '' }, 'profile') },
        channel: { codes: item({ web: '', app: '' }, 'profile') },
      },
    } }),
    table('customers', 30, [pk(), col('customer_no', 'TEXT'), col('level'), col('joined_at', 'TEXT')],
      { relation: [], codes: ['level'], date: ['joined_at'] }),
    table('visits', 22, [pk(), col('customer_id'), col('page', 'TEXT'), col('device'), col('visited_at', 'TEXT')],
      { relation: ['customer_id → customers.id'], codes: ['device', 'page'], date: ['visited_at'] }),
    table('products', 15, [pk(), col('category_id'), col('state'), col('listed_at', 'TEXT')],
      { relation: ['category_id → categories.id'], codes: ['state'], date: ['listed_at'] }, { newRelations: true }),
    table('refunds', 10, [pk(), col('order_id'), col('reason')], { relation: ['order_id → orders.id'], codes: ['reason'] }),
    table('tmp_log', 0, [pk(), col('logged_at', 'TEXT')], { date: ['logged_at'] }),
    table('order_summary', 5, [col('day', 'TEXT'), col('total', 'REAL')], {}, { is_view: true }),
    table('settings_kv', 1, [col('k', 'TEXT'), col('v', 'TEXT')], {}),
    table('legacy_coupons', 0, null, {}, { missing: true, notes: { label: item('旧版优惠券', 'human', 'confirmed') } }),
  ]
}

const blank = {
  kind: 'sqlite', host: null, port: null, username: null, readonly: true, description: '', enabled: true,
  password_masked: '', has_password: false, cached_schema: '', schema_error: '', available_schemas: [],
  last_checked_at: null, last_check_ok: null, last_latency_ms: null, last_error: null, current_snapshot: null,
}
const manual = (id, name, count, options) => ({
  ...blank, id, name, origin: 'manual', database: `/tmp/check/${name}.db`, table_count: count, tools: [`db_query__${name}`],
  schema_synced_at: T0, options,
})
/** 开着剖析的库：每张表最多 40 条、60 秒，单条 10 秒（缺省） */
const PROFILE_ON = { enabled: true }
const sources = () => [
  manual(S1, NAME1, 8, { catalog_profile: PROFILE_ON }),
  // 没开剖析的库：剖析设置里还留着旧的两个键（整次的查询次数上限、总时长上限），界面不认、保存时不带
  manual(S2, NAME2, 2, { catalog_profile: { enabled: false, max_queries: 60, max_total_s: 120 } }),
  {
    ...blank, id: S3, name: NAME3, origin: 'upload', table_count: 1, tools: [`db_query__${NAME3}`], schema_synced_at: T0,
    database: `/tmp/check/uploads/${S3}.db`, import_mode: 'simple', current_recipe: null, open_staging: null, options: { mask_columns: ['联系电话'] },
    current_snapshot: { id: 'snap-check-prof', created_at: T0, file_name: '客流日报.xlsx', raw_state: 'kept' },
  },
]
const NAMES = [NAME1, NAME2, NAME3]

const DEFAULTS = { enabled: false, table_max_queries: 40, table_max_s: 60, query_timeout_s: 10, sample_size: 2000, max_scan_rows: 100000 }
const settingsOf = (state, src) => {
  const raw = state.sources.find((s) => s.id === src)?.options?.catalog_profile ?? {}
  const out = { ...DEFAULTS, enabled: raw.enabled === true }
  for (const k of Object.keys(DEFAULTS)) if (k !== 'enabled' && typeof raw[k] === 'number') out[k] = raw[k]
  return out
}

// ---------------------------------------------------------------------------
// 假服务端的剖析任务：照接口约定 C 的形状，逐表行由检查推进的状态算出来
// ---------------------------------------------------------------------------
const JOB_ACTIVE = new Set(['queued', 'running'])
const TASK_END = new Set(['ok', 'partial', 'failed', 'skipped', 'cancelled'])
const RETRYABLE = new Set(['failed', 'partial', 'cancelled'])
const UNFINISHED = new Set(['budget', 'deadline', 'failed', 'timeout', 'error', 'rejected', 'slow', 'cancelled'])
const KINDS = ['relation', 'codes', 'date']

function newProfileJob(state, src, { tables, scope, retry_of = null, created_by = ACTOR, at = null }) {
  const id = `check-pjob-${++state.pjobSeq}`
  const tasks = tables.map((name, i) => {
    const t = state.tables[src].find((x) => x.name === name)
    const why = !t || t.missing ? `表结构里没有 ${name}，请先重新探查结构` : t.is_view ? '视图不剖析' : !checksOf(t) ? '没有可剖析的项' : null
    const checks = why ? [] : KINDS.flatMap((kind) => t.plan[kind].map((target) => ({
      kind, target, path: kind === 'relation' ? `relations.${target}` : kind === 'codes' ? `columns.${target}.codes` : 'business_date',
      state: 'pending', reason: null, detail: null, queries: 0,
    })))
    return {
      seq: i + 1, table_name: name, status: why ? 'skipped' : 'pending', checks, current: null, queries: 0, elapsed_ms: 0, row_estimate: null,
      findings: [], skipped: [], date_ranges: [], query_log: [], added: 0, updated: 0, removed: 0, version: t?.version ?? 0, error: why,
      started_at: null, finished_at: why ? (at ?? isoNow()) : null,
    }
  })
  const job = {
    id, source_id: src, status: 'queued', scope, settings: settingsOf(state, src), created_at: at ?? isoNow(), started_at: null, finished_at: null,
    created_by, retry_of, waiting_for: activeDraft(state, src) ? 'draft' : null, stop_reason: null, notice: null, error: null, tasks,
  }
  state.pjobs.push(job)
  return job
}

const zero = () => ({ relations_checked: 0, relations_verified: 0, codes: 0, codes_pending: 0, business_dates: 0 })
function summaryOf(findings) {
  const s = zero()
  for (const f of findings) {
    if (f.kind === 'relation') { s.relations_checked++; if (f.status === 'verified') s.relations_verified++ }
    if (f.kind === 'codes') { s.codes++; s.codes_pending += f.pending ?? 0 }
    if (f.kind === 'business_date') s.business_dates++
  }
  return s
}
function taskRow(t) {
  const byKind = Object.fromEntries(KINDS.map((k) => {
    const list = t.checks.filter((c) => c.kind === k)
    return [k, [list.filter((c) => c.state === 'done' || c.state === 'skipped').length, list.length]]
  }))
  const { checks, findings, skipped, date_ranges, query_log, ...rest } = t
  return {
    ...rest, checks_total: checks.length, checks_done: checks.filter((c) => c.state === 'done' || c.state === 'skipped').length,
    checks_by_kind: byKind, summary: summaryOf(findings), skipped_count: skipped.length,
    unfinished: skipped.filter((s) => UNFINISHED.has(s.reason)).length,
  }
}
const taskDetail = (job, t) => clone({ ...taskRow(t), job_id: job.id, job_status: job.status, created_by: job.created_by, checks: t.checks,
  findings: t.findings, skipped: t.skipped, date_ranges: t.date_ranges, query_log: t.query_log })
function jobOut(job) {
  const tasks = job.tasks.map(taskRow)
  const counts = { pending: 0, running: 0, ok: 0, partial: 0, failed: 0, skipped: 0, cancelled: 0 }
  for (const t of tasks) counts[t.status]++
  const sum = (k) => tasks.reduce((n, t) => n + t[k], 0)
  const summary = zero()
  for (const t of tasks) for (const k of Object.keys(summary)) summary[k] += t.summary[k]
  const log = job.tasks.flatMap((t) => t.query_log)
  return clone({
    ...Object.fromEntries(Object.entries(job).filter(([k]) => k !== 'tasks' && k !== 'stopAsked')),
    total: tasks.length, done: tasks.filter((t) => TASK_END.has(t.status)).length, counts,
    queries: sum('queries'), timeouts: log.filter((q) => q.outcome === 'timeout').length, errors: log.filter((q) => q.outcome === 'error').length,
    added: sum('added'), updated: sum('updated'), removed: sum('removed'), summary, tasks,
  })
}

/**
 * 推进一张表。o.status 默认 ok（每项都有结论）；running 时 o.done 写各类做完了几项，下一项是「进行中」。
 * o.skip：[[类, 第几项, 原因, 整句]]，跳过的项也记进 skipped。o.rest：结束时剩下没做的项一律记这个原因（停止、止损）
 */
function settle(state, job, name, o = {}) {
  const task = job.tasks.find((t) => t.table_name === name)
  if (job.status === 'queued') Object.assign(job, { status: 'running', started_at: isoNow(), waiting_for: null })
  const status = o.status ?? 'ok'
  task.started_at ??= isoNow()
  for (const c of task.checks) if (c.state === 'running') c.state = 'pending'
  for (const k of KINDS) {
    const list = task.checks.filter((c) => c.kind === k)
    const n = status === 'ok' ? list.length : o.done?.[k] ?? list.filter((c) => c.state !== 'pending').length
    list.forEach((c, i) => {
      if (i < n && c.state === 'pending') Object.assign(c, { state: 'done', queries: k === 'relation' ? 3 : 1, detail: c.detail ?? `${c.target}：有结论` })
    })
  }
  for (const [k, i, reason, detail] of o.skip ?? []) {
    const c = task.checks.filter((x) => x.kind === k)[i]
    Object.assign(c, { state: 'skipped', reason, detail })
    task.skipped.push({ kind: k, target: c.target, path: c.path, reason, detail })
  }
  if (o.rest) {
    for (const c of task.checks) {
      if (c.state !== 'pending') continue
      Object.assign(c, { state: 'skipped', reason: o.rest[0], detail: o.rest[1] })
      task.skipped.push({ kind: c.kind, target: c.target, path: c.path, reason: o.rest[0], detail: o.rest[1] })
    }
  }
  if (status === 'running') { const next = task.checks.find((c) => c.state === 'pending'); if (next) next.state = 'running' }
  const { done: _d, skip: _s, rest: _r, status: _st, ...fields } = o
  Object.assign(task, fields, { status })
  if (status !== 'running') task.current = null
  if (TASK_END.has(status)) {
    task.finished_at = isoNow()
    const t = state.tables[job.source_id].find((x) => x.name === name)
    if (t && (status === 'ok' || status === 'partial')) t.newRelations = false
    if (t && task.added + task.updated > 0) {
      t.version += 1
      t.updated_at = isoNow()
      t.updated_by = ACTOR
      t.notes ??= {}
      task.version = t.version
    }
  }
}
/** 收尾：有失败、部分完成、停止的表就是 partial */
function finish(job, status) {
  const bad = job.tasks.some((t) => RETRYABLE.has(t.status))
  Object.assign(job, { status: status ?? (bad ? 'partial' : 'succeeded'), finished_at: isoNow(), notice: null })
  job.started_at ??= job.finished_at
  delete job.stopAsked
}
/**
 * 停下（使用者停止、止损、服务重启）。正在做的那张表照服务端分两种：
 * - 来得及收尾（saved，缺省：停止、止损、关停服务）：写入已做完的项，其余项记原因，状态按逐表算（有项没查是部分完成），
 *   error 是 rest（服务端的「已做完的 N 项照常写进了数据目录，剩下 M 项没查」）；
 * - 没来得及收尾（saved: false：进程被杀，下次启动时才标中断）：结论一项都没写进目录，表记已停止，查完的项也改记跳过，
 *   说法照服务端（catalog_profile_jobs._close_tasks）：查完的项「{原因}，查完了但结论没有写进数据目录」，表的原因
 *   「{原因}：已查完的 N 项没有写进数据目录，剩下 M 项没查」；一项都还没查完时表的原因就是 error。
 * 没开始的表记已停止，原因同一句
 */
function stopJob(job, { status = 'cancelled', reason = 'cancelled', current = null, rest = null, error = null, saved = true } = {}) {
  for (const t of job.tasks) {
    if (t.status === 'running') {
      const left = t.checks.filter((c) => c.state === 'pending' || c.state === 'running')
      for (const c of left) {
        Object.assign(c, { state: 'skipped', reason, detail: current ?? '已停止，这一项没有查' })
        t.skipped.push({ kind: c.kind, target: c.target, path: c.path, reason, detail: c.detail })
      }
      if (saved) {
        Object.assign(t, { status: left.length ? 'partial' : 'ok', current: null, finished_at: isoNow(), error: rest ?? error })
        continue
      }
      const done = t.checks.filter((x) => x.state === 'done')
      for (const c of done) {
        Object.assign(c, { state: 'skipped', reason, detail: `${error}，查完了但结论没有写进数据目录` })
        t.skipped.push({ kind: c.kind, target: c.target, path: c.path, reason, detail: c.detail })
      }
      const own = done.length ? `${error}：已查完的 ${done.length} 项没有写进数据目录${left.length ? `，剩下 ${left.length} 项没查` : ''}` : error
      Object.assign(t, { status: 'cancelled', current: null, finished_at: isoNow(), error: own })
    } else if (t.status === 'pending') {
      Object.assign(t, { status: 'cancelled', finished_at: isoNow(), error: error ?? '已停止，这张表没有剖析' })
    }
  }
  if (error) job.error = error
  finish(job, status)
}

const activeProfile = (state, src) => state.pjobs.find((j) => j.source_id === src && JOB_ACTIVE.has(j.status)) ?? null
const pjobOf = (state, key) => {
  const [, , src, , , id] = key.split(' ')[1].split('/')
  return state.pjobs.find((j) => j.id === id && j.source_id === src) ?? null
}
/** 一张表最近一次剖析（清单行上的 last_profile、单表目录的 last_profile） */
function lastTaskOf(state, src, name) {
  for (const job of [...state.pjobs].reverse()) {
    if (job.source_id !== src) continue
    const t = job.tasks.find((x) => x.table_name === name)
    if (t) return { job, t }
  }
  return null
}
function dueOf(state, src, t) {
  if (t.missing || t.is_view || !checksOf(t)) return null
  const last = lastTaskOf(state, src, t.name)
  if (!last) return 'never'
  if (last.t.status === 'pending' || last.t.status === 'running') return null
  if (RETRYABLE.has(last.t.status)) return 'unfinished'
  return t.newRelations ? 'new_relations' : null
}
/** 范围 needed / all：服务端的口径（有待核实关系的在前，这里按使用次数，同清单顺序） */
const scopeTables = (state, src, scope) => state.tables[src].filter((t) => !t.missing).sort((a, b) => b.usage - a.usage)
  .filter((t) => (scope === 'all' ? checksOf(t) > 0 && !t.is_view : dueOf(state, src, t) != null)).map((t) => t.name)

// ---------------------------------------------------------------------------
// 假服务端的起草任务：只做排队要用的部分（建、取、停），起草本身不推
// ---------------------------------------------------------------------------
function newDraftJob(state, src, tables) {
  const id = `check-djob-${++state.djobSeq}`
  const job = {
    id, source_id: src, status: 'queued', use_model: false, model: null, guidance: null, note: null, created_at: isoNow(), started_at: null,
    finished_at: null, created_by: ACTOR, usage: { calls: 0, input_tokens: 0, output_tokens: 0 }, error: null, notice: null,
    waiting_for: activeProfile(state, src) ? 'profile' : null,
    tasks: tables.map((name) => ({ table_name: name, status: 'pending', columns: 3, parts_total: null, parts_done: 0, added: 0, updated: 0, removed: 0,
      version: 0, model_items: null, error: null, model_error: null, started_at: null, finished_at: null })),
  }
  state.djobs.push(job)
  return job
}
function draftOut(job) {
  const counts = { pending: 0, running: 0, ok: 0, partial: 0, failed: 0, skipped: 0, cancelled: 0 }
  for (const t of job.tasks) counts[t.status]++
  return clone({ ...job, total: job.tasks.length, done: job.tasks.filter((t) => TASK_END.has(t.status)).length, counts, added: 0, updated: 0, removed: 0 })
}
const activeDraft = (state, src) => state.djobs.find((j) => j.source_id === src && JOB_ACTIVE.has(j.status)) ?? null

// ---------------------------------------------------------------------------
// 假服务端：清单、单表、剖析任务、起草任务、保存数据源
// ---------------------------------------------------------------------------
const freshState = () => {
  const state = { sources: sources(), tables: { [S1]: shopTables(), [S2]: [
    table('orders', 3, [pk(), col('status')], { codes: ['status'] }),
    table('order_summary', 1, [col('day', 'TEXT')], {}, { is_view: true }),
  ], [S3]: [table('日客流', 2, [col('统计日期', 'DATE'), col('入园人数')], { date: ['统计日期'] })] },
  pjobs: [], pjobSeq: 0, djobs: [], djobSeq: 0 }
  // 三天前别人做的一次剖析（部分完成）：客户、商品做完了，访问记录没做完，退款写入一直冲突
  const old = newProfileJob(state, S1, { tables: ['customers', 'visits', 'products', 'refunds'], scope: 'needed', created_by: OTHER, at: T0 })
  settle(state, old, 'customers', {
    queries: 4, elapsed_ms: 2300, row_estimate: { rows: 12000, method: 'stats', at_least: null },
    findings: [{ kind: 'codes', path: 'columns.level.codes', column: 'level', values: [{ value: '1', rows: 9000 }, { value: '2', rows: 3000 }], rows: 12000,
      status: 'proposed', pending: 2, summary: '2 个取值' }],
    date_ranges: [{ column: 'joined_at', min: '2021-03-01 08:00:00', max: '2026-10-01 21:30:00' }],
    skip: [], query_log: [
      { at: T0, kind: 'row_estimate', target: 'customers', sql: 'SELECT COUNT(*) FROM (SELECT 1 FROM customers LIMIT ?) t', ms: 12, rows: 1, outcome: 'ok', detail: null },
      { at: T0, kind: 'codes', target: 'level', sql: 'SELECT level, COUNT(*) FROM customers GROUP BY level ORDER BY COUNT(*) DESC LIMIT ?', ms: 40, rows: 2, outcome: 'ok', detail: null },
      { at: T0, kind: 'date', target: 'joined_at', sql: 'SELECT MIN(joined_at), MAX(joined_at) FROM customers', ms: 25, rows: 1, outcome: 'ok', detail: null },
    ],
  })
  // 码值候选之外的一项按规则不查（遮罩），不算没做完
  old.tasks[0].skipped.push({ kind: 'codes', target: 'phone', path: 'columns.phone.codes', reason: 'masked', detail: 'customers.phone 在数据源设置中被遮罩，不取值' })
  settle(state, old, 'visits', { status: 'partial', done: { relation: 0, codes: 1, date: 1 }, queries: 40, elapsed_ms: 31000,
    skip: [['relation', 0, 'budget', '这张表的查询次数用完（40 条），这一项没有查，保留原来的结论'], ['codes', 1, 'budget', '这张表的查询次数用完（40 条），这一项没有查，保留原来的结论']] })
  settle(state, old, 'products', { queries: 6, elapsed_ms: 3100 })
  settle(state, old, 'refunds', { status: 'failed', queries: 3, elapsed_ms: 900, error: '写入一直冲突：剖析期间这张表的数据目录被反复修改，结论没有写进去' })
  for (const t of old.tasks) { t.started_at = T0; t.finished_at = T0 }
  Object.assign(old, { started_at: T0 })
  finish(old)
  old.finished_at = T0
  // 那次剖析之后，目录里又出现了商品的待核实关系
  state.tables[S1].find((t) => t.name === 'products').newRelations = true
  return state
}

const parts = (key) => key.split(' ')[1].split('/')  // ['', 'datasources', id, 'catalog', ...]
function rowOf(state, src, t) {
  const last = lastTaskOf(state, src, t.name)
  const lp = last ? (({ seq, status, started_at, finished_at, summary, skipped_count, unfinished, error, row_estimate }) =>
    ({ job_id: last.job.id, seq, status, started_at, finished_at, summary, skipped_count, unfinished, error, row_estimate }))(taskRow(last.t)) : null
  const counts = { proposed: 0, verified: 0, confirmed: 0, rejected: 0 }
  return {
    table_name: t.name, qualified: t.name, is_view: t.is_view, in_schema: !t.missing, label: t.notes?.label?.value ?? null,
    label_status: t.notes?.label?.status ?? null, kind: null, counts: t.notes ? { ...counts, proposed: 1 } : counts, relations: 0, usage: t.usage,
    version: t.version, updated_at: t.updated_at, updated_by: t.updated_by, last_draft: null,
    last_profile: lp, profile_due: dueOf(state, src, t) != null, profile_due_reason: dueOf(state, src, t), profile_checks: checksOf(t),
  }
}
function listOf(state, src) {
  const all = state.tables[src]
  const rows = all.filter((t) => !t.missing).sort((a, b) => b.usage - a.usage).map((t) => rowOf(state, src, t))
  rows.push(...all.filter((t) => t.missing).map((t) => rowOf(state, src, t)))
  const pa = activeProfile(state, src)
  const da = activeDraft(state, src)
  return {
    tables: rows, system_notes: src === S3, schema_note: null, schema_truncated: false, schema_total: rows.length, guidance: null,
    active_job: da ? (({ id, status, total, done, waiting_for }) => ({ id, status, total, done, waiting_for }))(draftOut(da)) : null,
    active_profile_job: pa ? (({ id, status, total, done, waiting_for }) => ({ id, status, total, done, waiting_for }))(jobOut(pa)) : null,
  }
}
function detailOf(state, src, t) {
  const last = lastTaskOf(state, src, t.name)
  return {
    table_name: t.name, in_schema: !t.missing, notes: t.notes ?? {}, version: t.version, updated_at: t.updated_at, updated_by: t.updated_by,
    structure: t.missing ? null : { qualified: t.name, is_view: t.is_view, comment: null, columns: t.columns,
      primary_key: t.columns.filter((c) => c.pk).map((c) => c.name), foreign_keys: [], unique: [] },
    system_notes: src === S3, usage: t.usage, last_profile: last ? taskDetail(last.job, last.t) : null,
  }
}
const busyOf = (state, job) => {
  const out = jobOut(job)
  return { detail: `数据源「${state.sources.find((s) => s.id === job.source_id)?.name}」正在进行数据剖析（已完成 ${out.done} / ${out.total} 张表），`
    + '请等这次剖析结束（或先停止它）再开始新的剖析', code: 'profile_busy' }
}
const NO_PJOB = json({ detail: '剖析任务不存在，可能属于别的数据源，或者已随数据源删除' }, 404)

function handlers(state, replies = {}) {
  const reply = (k, fallback) => (route, ctx) => {
    const q = replies[k]
    const next = Array.isArray(q) ? q.shift() : q
    return (next ?? fallback)(route, ctx)
  }
  const P = 'check-prof-[a-z]+'
  return [
    [/^GET \/datasources$/, (route) => json(state.sources)(route)],
    // 数据源的变化、取值观察、后台任务条（next-round 第 4 波）：这里没有，给空的（不放行到服务端去查一个假的源）
    [/^GET \/datasources\/check-prof-[a-z]+\/changes$/, json({ items: [], total: 0, counts: { pending: 0, open: 0, by_severity: { action: 0, warning: 0, info: 0 } } })],
    [/^GET \/datasources\/check-prof-[a-z]+\/observations$/, json({ table: '', series: [] })],
    // 后台任务条的队列：照服务端（同源队先后），在做的在前、排着的按建的先后（05 (g)：界面不自己排）
    [new RegExp(`^GET /datasources/${P}/background-jobs$`), (route, { key }) => {
      const src = parts(key)[2]
      const active = [...state.djobs.map((j) => ['draft', draftOut(j)]), ...state.pjobs.map((j) => ['profile', jobOut(j)])]
        .filter(([, j]) => j.source_id === src && JOB_ACTIVE.has(j.status))
        .sort(([, a], [, b]) => (b.status === 'running') - (a.status === 'running') || a.created_at.localeCompare(b.created_at))
      return json({ items: active.map(([kind, j], i) => ({ kind, id: j.id, status: j.status, total: j.total, done: j.done,
        waiting_for: j.status === 'queued' ? j.waiting_for ?? null : null, position: i })) })(route)
    }],
    [/^GET \/datasources\/upkeep$/, json({ items: [] })],
    [new RegExp(`^GET /datasources/${P}/schema$`), (route, { key }) =>
      json({ tables: state.tables[parts(key)[2]].filter((t) => !t.missing).map((t) => t.name), summary: '', synced_at: T0 })(route)],
    [new RegExp(`^GET /datasources/${P}/catalog$`), (route, { key }) => json(listOf(state, parts(key)[2]))(route)],
    // ---- 剖析任务
    [new RegExp(`^POST /datasources/${P}/catalog/profile-jobs$`), reply('create', (route, { key, body }) => {
      const src = parts(key)[2]
      const s = state.sources.find((x) => x.id === src)
      if (!settingsOf(state, src).enabled) {
        return json({ detail: `数据源「${s.name}」未开启数据剖析。剖析会对业务库发查询，请先在数据源设置中开启数据剖析`, code: 'profile_disabled' }, 409)(route)
      }
      const active = activeProfile(state, src)
      if (active) return json(busyOf(state, active), 409)(route)
      if ((body?.tables?.length ?? 0) > 500) return json({ detail: '一次最多剖析 500 张表' }, 422)(route)
      const tables = body?.tables?.length ? [...new Set(body.tables)] : scopeTables(state, src, body?.scope ?? 'needed')
      if (!tables.length) {
        return json({ detail: '按「需要剖析的表」挑出 0 张表：表都剖析过、上次都做完了，也没有新的待核实关联关系。可以改选「全部表」，或指定要剖析的表',
          code: 'profile_nothing' }, 409)(route)
      }
      const job = newProfileJob(state, src, { tables, scope: body?.tables?.length ? 'selected' : body?.scope ?? 'needed' })
      return json({ job: jobOut(job) }, 202)(route)
    })],
    [new RegExp(`^GET /datasources/${P}/catalog/profile-jobs/active$`), (route, { key }) => {
      const job = activeProfile(state, parts(key)[2])
      return json({ job: job && jobOut(job) })(route)
    }],
    [new RegExp(`^GET /datasources/${P}/catalog/profile-jobs/latest$`), (route, { key }) => {
      const job = state.pjobs.filter((j) => j.source_id === parts(key)[2]).at(-1) ?? null
      return json({ job: job && jobOut(job) })(route)
    }],
    [new RegExp(`^GET /datasources/${P}/catalog/profile-jobs/[^/]+/tasks/\\d+$`), (route, { key }) => {
      const job = pjobOf(state, key)
      const t = job?.tasks.find((x) => x.seq === Number(parts(key).at(-1)))
      return t ? json({ task: taskDetail(job, t) })(route) : json({ detail: '任务里没有这张表' }, 404)(route)
    }],
    // 停止：排队的直接停；进行中的记下来，等检查用 stopJob 推到底（这期间页面应当写「正在停止」）
    [new RegExp(`^POST /datasources/${P}/catalog/profile-jobs/[^/]+/cancel$`), reply('cancel', (route, { key }) => {
      const job = pjobOf(state, key)
      if (!job) return NO_PJOB(route)
      if (!JOB_ACTIVE.has(job.status)) return json({ detail: '这次剖析已经结束了，不需要停止', code: 'catalog_profile_finished' }, 409)(route)
      if (job.status === 'queued') stopJob(job)
      else job.stopAsked = true
      return json({ job: jobOut(job) })(route)
    })],
    [new RegExp(`^POST /datasources/${P}/catalog/profile-jobs/[^/]+/retry$`), reply('retry', (route, { key }) => {
      const old = pjobOf(state, key)
      if (!old) return NO_PJOB(route)
      const active = activeProfile(state, old.source_id)
      if (active) return json(busyOf(state, active), 409)(route)
      const tables = old.tasks.filter((t) => RETRYABLE.has(t.status)).map((t) => t.table_name)
      if (!tables.length) return json({ detail: '这次剖析里没有失败、部分完成或停止了的表，不需要重试' }, 422)(route)
      const job = newProfileJob(state, old.source_id, { tables, scope: 'selected', retry_of: old.id })
      return json({ job: jobOut(job) }, 202)(route)
    })],
    [new RegExp(`^GET /datasources/${P}/catalog/profile-jobs/[^/]+$`), (route, { key }) => {
      const job = pjobOf(state, key)
      return job ? json({ job: jobOut(job) })(route) : NO_PJOB(route)
    }],
    // 旧的同步剖析接口：前端不该再用它，调了就答 410，下面的检查会数它
    [new RegExp(`^POST /datasources/${P}/catalog/profile$`), json({ detail: '检查脚本：前端不该再调用同步剖析接口' }, 410)],
    // ---- 起草任务（排队那一节用）
    [new RegExp(`^POST /datasources/${P}/catalog/draft-jobs$`), (route, { key, body }) => {
      const src = parts(key)[2]
      if (activeDraft(state, src)) return json({ detail: '正在起草', code: 'catalog_draft_busy' }, 409)(route)
      const job = newDraftJob(state, src, body?.tables?.length ? body.tables : state.tables[src].filter((t) => !t.missing).map((t) => t.name).slice(0, 20))
      return json({ job: draftOut(job) }, 202)(route)
    }],
    [new RegExp(`^GET /datasources/${P}/catalog/draft-jobs/active$`), (route, { key }) => {
      const job = activeDraft(state, parts(key)[2])
      return json({ job: job && draftOut(job) })(route)
    }],
    [new RegExp(`^GET /datasources/${P}/catalog/draft-jobs/latest$`), (route, { key }) => {
      const job = state.djobs.filter((j) => j.source_id === parts(key)[2]).at(-1) ?? null
      return json({ job: job && draftOut(job) })(route)
    }],
    [new RegExp(`^GET /datasources/${P}/catalog/draft-jobs/[^/]+$`), (route, { key }) => {
      const job = state.djobs.find((j) => j.id === parts(key).at(-1))
      return job ? json({ job: draftOut(job) })(route) : json({ detail: '起草任务不存在' }, 404)(route)
    }],
    [/^GET \/settings$/, async (route) => {
      const real = await route.fetch().catch(() => null)
      if (!real) return route.abort().catch(() => {})
      const body = await real.json().catch(() => ({}))
      return route.fulfill({ response: real, json: { ...body, catalog_draft: { provider: null, model: null } } }).catch(() => {})
    }],
    [/^GET \/copilot\/model$/, json({ configured: true, provider: null, model: null, effective_provider: 'check-gateway', effective_model: 'check-model', error: null })],
    // ---- 单表、保存数据源
    [new RegExp(`^GET /datasources/${P}/catalog/[^/]+/impact$`), (route, { key }) => json({ table: parts(key)[4], templates: [] })(route)],
    [new RegExp(`^PATCH /datasources/${P}$`), reply('patch', (route, { key, body }) => {
      const id = parts(key)[2]
      state.sources = state.sources.map((s) => (s.id === id ? { ...s, ...body, password: undefined } : s))
      return json(state.sources.find((s) => s.id === id))(route)
    })],
    [new RegExp(`^GET /datasources/${P}/catalog/[^/]+$`), (route, { key }) => {
      const src = parts(key)[2]
      const t = state.tables[src]?.find((x) => x.name === parts(key)[4])
      return t ? json(detailOf(state, src, t))(route) : json({ detail: `数据源中没有表 ${parts(key)[4]}` }, 404)(route)
    }],
    [new RegExp(`^PUT /datasources/${P}/catalog/[^/]+$`), (route, { key, body }) => {
      const src = parts(key)[2]
      const t = state.tables[src].find((x) => x.name === parts(key)[4])
      if (body.if_version !== t.version) return json({ detail: `表「${t.name}」的数据目录刚被修改过，请重新载入后再提交` }, 409)(route)
      Object.assign(t, { notes: body.notes, version: t.version + 1, updated_at: isoNow(), updated_by: ACTOR })
      return json(detailOf(state, src, t))(route)
    }],
  ]
}

// ---------------------------------------------------------------------------
// 页面上的定位
// ---------------------------------------------------------------------------
const box = (page) => page.locator('[data-profile-dialog]')
/** 剖析对话框在哪一步：setup / blocked / running / done；没开着为 null */
const phase = (page) => box(page).getAttribute('data-profile-dialog', { timeout: 500 }).catch(() => null)
/** 某一处的文字（空白压成一个空格）；没有这一处为空串 */
const textOf = async (loc) => (await loc.first().innerText({ timeout: 800 }).catch(() => '')).replace(/\s+/g, ' ').trim()
const inBox = (page, sel) => textOf(box(page).locator(sel))
const taskLine = (page, name) => box(page).locator(`[data-profile-task="${name}"]`)
const head = (page) => page.locator('[data-catalog-profile]')
/** 后台任务条上的一项（进度只在这一处，05 (g)） */
const stripJob = (page, kind) => page.locator(`[data-catalog-job-strip] [data-catalog-job="${kind}"]`)
const stripText = (page, kind) => textOf(stripJob(page, kind).locator('[data-catalog-job-text]'))
const stripKinds = (page) => page.locator('[data-catalog-job-strip] [data-catalog-job]').evaluateAll((els) => els.map((e) => e.getAttribute('data-catalog-job')))
/** 表清单一行的「剖析」一格（去掉给读屏的「数据剖析：」）；没有这一格为空串 */
const rowProfile = async (page, name) => (await textOf(page.locator(`[data-catalog-row="${name}"] [data-row-profile]`))).replace(/^数据剖析：\s*/, '')
const toasts = (page) => page.locator('[data-toast]').allInnerTexts().then((t) => t.join(' | ')).catch(() => '')
async function openDialog(page) {
  await head(page).click()
  await box(page).waitFor()
}
// 界面不该露的机读码：状态、跳过原因、检查类别、查询结果的英文值
const CODE_RE = /\b(?:queued|running|succeeded|partial|failed|cancelled|interrupted|pending|skipped|budget|deadline|timeout|rejected|masked|too_large|slow|nothing|row_estimate|relation|codes|verified|proposed|unfinished|new_relations|new_checks|never)\b/
async function cleanCopy(page, where) {
  let t = await box(page).innerText().catch(() => '')
  // 表名、列名、SQL 是库里的标识，照原样显示，先去掉再查
  t = t.replace(/SELECT[\s\S]*?(?=\n|$)/g, '').replace(/[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)?/g, (w) => (CODE_RE.test(w) && !/_/.test(w) ? w : ''))
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

await section('剖析 · 设置：每张表的两项预算、被拒时报错落到那一项', async () => {
  const state = freshState()
  const replies = { patch: [] }
  const { page, sent, errors, natives, close } = await open('/data/databases', { handlers: handlers(state, replies) })
  // 没开剖析的库（设置里还留着旧的两个键）：编辑框里「数据剖析」默认关闭，预算收着
  await page.locator(`[data-source="${NAME2}"]`).getByRole('button', { name: '编辑', exact: true }).click()
  const sec = page.locator('[data-profile-settings]')
  await sec.waitFor()
  check('编辑框里有「数据剖析」，关着、预算收着', (await sec.getAttribute('data-profile-settings')) === 'off'
        && await sec.locator('[data-profile-field]').count() === 0)
  await sec.locator('[data-profile-enabled]').check()
  const labels = await sec.locator('label.label').allInnerTexts()
  check('开启后五项预算展开：每张表最多查询数、每张表时长上限在前，没有整次的查询次数上限、总时长上限',
    labels.join('、') === '每张表最多查询数、每张表时长上限、单条查询时限、核对键值数、整表统计行数上限', labels.join('、'))
  const units = await sec.locator('[data-profile-field] span').allInnerTexts()
  check('……每项带单位', units.join(',') === '条,秒,秒,个,行', units.join(','))
  const hints = await sec.locator('[id$="-hint"]').allInnerTexts()
  check('……写明范围和默认值，说清用完只停这张表', hints.some((h) => h.startsWith('范围 1–200 条，默认 40') && h.includes('用完只停这张表'))
        && hints.some((h) => h.startsWith('范围 10–600 秒，默认 60') && h.includes('只停这张表')), hints.join(' | '))
  check('……旧的两个键不回显成输入框的值', await page.locator('#ds-profile-table_max_queries').inputValue() === ''
        && await page.locator('#ds-profile-table_max_s').inputValue() === '')
  const risk = () => sec.locator('[data-profile-risk]').innerText().catch(() => '')
  check('……风险说明按表写：一条一条地发、可随时停止，每张表最多 40 条、60 秒，单条 10 秒',
    (await risk()).includes('一条一条地发，可随时停止：每张表最多 40 条、60 秒，单条不超过 10 秒'), await risk())
  await page.locator('#ds-profile-table_max_queries').fill('30')
  check('……风险说明跟着填的数走', (await risk()).includes('每张表最多 30 条'), await risk())
  await page.locator('#ds-profile-table_max_queries').fill('500')
  const err = await page.locator('#ds-profile-table_max_queries-error').innerText().catch(() => '')
  check('填错（500 条，超过 200）：就地写明范围，保存按钮禁用', err === '请填写 1 到 200 之间的整数' && await page.locator('[data-source-save]').isDisabled(), err)
  check('……输入框标出错', (await page.locator('#ds-profile-table_max_queries').getAttribute('aria-invalid')) === 'true')
  await page.locator('#ds-profile-table_max_queries').fill('30')
  await page.locator('#ds-profile-table_max_s').fill('90')
  // 服务端按字段拒收（422）：带机读码和出错的那一项，界面按它落到那一格，不从原话里认
  const REJECT = '数据剖析的「每张表时长上限（秒）」需要填写 10 到 600 之间的数；当前为「90」'
  replies.patch.push(json({ detail: REJECT, code: 'profile_settings_invalid', field: 'table_max_s' }, 422))
  await page.locator('[data-source-save]').click()
  check('保存被拒（422，field 是每张表时长上限）：服务端原话写在那一项下面', await until(async () =>
    (await page.locator('#ds-profile-table_max_s-error').innerText().catch(() => '')) === REJECT),
  await page.locator('#ds-profile-table_max_s-error').innerText().catch(() => ''))
  check('……光标落在那一项上，框标出错', await until(async () => page.evaluate(() => document.activeElement?.id === 'ds-profile-table_max_s'))
        && (await page.locator('#ds-profile-table_max_s').getAttribute('aria-invalid')) === 'true')
  replies.patch.push(json({ detail: '数据剖析的「每张表最多查询数」需要填写 1 到 200 之间的整数；当前为「30」', code: 'profile_settings_invalid',
    field: 'table_max_queries' }, 422))
  await page.locator('#ds-profile-table_max_s').fill('95')
  await page.locator('[data-source-save]').click()
  check('……field 是每张表最多查询数：落在那一格', await until(async () =>
    (await page.locator('#ds-profile-table_max_queries').getAttribute('aria-invalid')) === 'true'
    && page.evaluate(() => document.activeElement?.id === 'ds-profile-table_max_queries')))
  if (SHOTS) await sleep(350)
  await shot(page, 'profile-settings-422')
  await page.locator('#ds-profile-table_max_queries').fill('30')
  await page.locator('#ds-profile-table_max_s').fill('90')
  await page.locator('[data-source-save]').click()
  await until(async () => (await page.locator('[data-profile-settings]').count()) === 0)
  const saved = writes(sent, /^PATCH /).at(-1)?.body
  check('保存：只写开关和填了的新项（数字），旧的两个键不带', JSON.stringify(saved?.options?.catalog_profile)
        === JSON.stringify({ enabled: true, table_max_queries: 30, table_max_s: 90 }), JSON.stringify(saved?.options))
  check('……卡片上标「数据剖析已开启」，悬停按表写预算', await until(async () => (await page.locator(`[data-source="${NAME2}"] [data-profile-on]`).count()) === 1)
        && (await page.locator(`[data-source="${NAME2}"] [data-profile-on]`).getAttribute('title')) === '数据剖析已开启：每张表最多 30 条只读查询、90 秒，单条不超过 10 秒',
  await page.locator(`[data-source="${NAME2}"] [data-profile-on]`).getAttribute('title').catch(() => ''))
  await page.locator(`[data-source="${NAME2}"]`).getByRole('button', { name: '编辑', exact: true }).click()
  await page.locator('[data-profile-settings="on"]').waitFor()
  check('再打开编辑框：开关和填的数回显', await page.locator('#ds-profile-table_max_queries').inputValue() === '30'
        && await page.locator('#ds-profile-table_max_s').inputValue() === '90')
  await page.getByRole('button', { name: '取消', exact: true }).click()

  // 导入表格的源没有编辑框：卡片上单独设置，options 的其余键原样带上
  await goto(page, '/data/tables')
  await page.locator(`[data-source="${NAME3}"] [data-edit-profile]`).click()
  const dialog = page.locator('[data-profile-settings-dialog]')
  await dialog.waitFor()
  check('导入表格的源：卡片上「设置数据剖析」打开只改这一项的弹窗，写明对之后新建的剖析生效',
    (await textOf(page.locator('[role="dialog"]').last())).includes('对之后新建的剖析生效'))
  await dialog.locator('[data-profile-enabled]').check()
  await page.locator(`#profile-${S3}-table_max_s`).fill('120')
  await page.locator('[data-profile-save]').click()
  await until(async () => (await dialog.count()) === 0)
  const body = writes(sent, new RegExp(`^PATCH /datasources/${S3}$`)).at(-1)?.body
  check('……保存只换剖析设置，遮罩列原样带上', JSON.stringify(body)
        === JSON.stringify({ options: { mask_columns: ['联系电话'], catalog_profile: { enabled: true, table_max_s: 120 } } }), JSON.stringify(body))
  check('写请求只有保存数据源', writes(sent, /./).every((s) => /^PATCH \/datasources\/check-prof-/.test(s.key)),
    JSON.stringify(writes(sent, /./).map((s) => s.key)))
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('剖析 · 开始前：范围、个数与预算', async () => {
  const state = freshState()
  const { page, sent, errors, natives, close } = await open(`/data/catalog/${S1}`, { handlers: handlers(state) })
  await page.locator('[data-catalog-row]').first().waitFor()
  check('页头写「数据剖析」', (await head(page).getAttribute('aria-label')) === '数据剖析' && (await head(page).getAttribute('data-catalog-profile')) === '')
  await openDialog(page)
  const scopes = await box(page).locator('[data-profile-scope]').evaluateAll((els) => els.map((e) => `${e.getAttribute('data-profile-scope')}:${e.getAttribute('data-count')}`))
  // 需要剖析：从没剖析过的订单、空日志表；上次没做完的访问记录、退款；剖析后有新关系的商品。全部：再加上客户，不含视图、
  // 没有可剖析项的配置表、只剩目录的旧表
  check('没选表、没筛选：范围是「需要剖析的 5 张表」（默认）和「全部 6 张表」', scopes.join(',') === 'needed:5,all:6'
        && await box(page).locator('[data-profile-scope="needed"] input').isChecked(), scopes.join(','))
  check('……写明需要剖析的口径（含上次是空表的不算）、全部不含视图', (await inBox(page, '[data-profile-scope="needed"]')).includes('从没剖析过、上次没做完，或剖析之后又出现了待核实关联关系、要取码值或日期范围的列的表。上次是空表的不算')
        && (await inBox(page, '[data-profile-scope="all"]')).includes('不含视图'))
  const budget = await inBox(page, '[data-profile-budget]')
  check('预算按表写：每张表最多 40 条、60 秒，单条 10 秒，核对键值数、整表统计门槛，按哪个源的设置',
    ['每张表最多 40 条查询、60 秒，用完只停这张表', '单条查询不超过 10 秒', '取 2,000 个不同键值核对', '行数超过 100,000 的表不做整表统计', `按「${NAME1}」的数据剖析设置`]
      .every((x) => budget.includes(x)), budget)
  check('……按这次的表数估上限：「这次 5 张表，最多约 200 条查询、5 分钟」', (await inBox(page, '[data-profile-budget-total]')) === '这次 5 张表，最多约 200 条查询、5 分钟',
    await inBox(page, '[data-profile-budget-total]'))
  await box(page).locator('[data-profile-scope="all"]').click()
  check('……换成全部 6 张：估算跟着变', (await inBox(page, '[data-profile-budget-total]')) === '这次 6 张表，最多约 240 条查询、6 分钟')
  check('醒目一句：只读查询、一条一条地发、可随时停止', (await inBox(page, '[data-profile-readonly]')).includes('只读查询，一条一条地发，可随时停止'))
  check('……不再说「无法中途停止」', !(await textOf(box(page))).includes('无法中途停止'))
  check('……没有起草任务：按钮写「开始剖析」', (await textOf(page.locator('[data-profile-start]'))).includes('开始剖析')
        && (await page.locator('[data-profile-start]').getAttribute('data-profile-start')) === 'start')
  check('上一次剖析没做完：设置里写一行，可以点开', (await inBox(page, '[data-profile-last]')).includes('剖析完成，部分表没做完')
        && await box(page).locator('[data-profile-last-open]').count() === 1, await inBox(page, '[data-profile-last]'))
  check('还没开始：没有写请求', writes(sent, /./).length === 0)
  if (SHOTS) await sleep(350)
  await shot(page, 'profile-setup')
  await cleanCopy(page, '开始前')
  await page.keyboard.press('Escape')
  await until(async () => (await box(page).count()) === 0)

  // 已选三张（其中一张是视图）：剖析不了的不交给服务端，写明另有几张不剖析
  for (const n of ['orders', 'customers', 'order_summary']) await page.locator(`[data-catalog-select="${n}"]`).click()
  await page.locator('[data-catalog-profile-selected]').click()
  await box(page).waitFor()
  check('剖析所选：范围默认是「已选的 2 张表」，写明另有 1 张不剖析', await box(page).locator('[data-profile-scope="selected"] input').isChecked()
        && (await inBox(page, '[data-profile-scope="selected"]')).includes('已选的 2 张表')
        && (await inBox(page, '[data-profile-scope="selected"]')).includes('另有 1 张不剖析'), await inBox(page, '[data-profile-scope="selected"]'))
  await page.keyboard.press('Escape')
  await page.locator('[data-catalog-clear]').click()
  // 搜索「order」：当前筛选里的订单、退款（id 列里有 order）……按清单的筛选结果算
  await page.locator('[data-catalog-search]').fill('order')
  await openDialog(page)
  check('有筛选：多一项「当前筛选结果中的 N 张表」', await box(page).locator('[data-profile-scope="filtered"]').count() === 1
        && (await inBox(page, '[data-profile-scope="filtered"]')).startsWith('当前筛选结果中的 1 张表'), await inBox(page, '[data-profile-scope="filtered"]'))
  check('没有调用旧的同步剖析接口', count(sent, /catalog\/profile$/) === 0)
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('剖析 · 进行中：逐表进度、后台继续与停止', async () => {
  const state = freshState()
  const { page, sent, errors, close } = await open(`/data/catalog/${S1}`, { handlers: handlers(state) })
  await page.locator('[data-catalog-row]').first().waitFor()
  await openDialog(page)
  await page.locator('[data-profile-start]').click()
  check('开始：进入进行中，建任务的请求带「需要剖析」范围和本机署名', await until(async () => (await phase(page)) === 'running')
        && JSON.stringify(lastBody(sent, /profile-jobs$/)) === JSON.stringify({ scope: 'needed' })
        && decodeURIComponent(writes(sent, /profile-jobs$/).at(-1)?.actor ?? '') === ACTOR, JSON.stringify(lastBody(sent, /profile-jobs$/)))
  const job = state.pjobs.at(-1)
  check('……刚建好还在排队：标题写「排队中」，逐表 5 行都写「排队」', (await inBox(page, '[data-profile-title]')).startsWith('排队中')
        && (await box(page).locator('[data-profile-task]').count()) === 5 && (await inBox(page, '[data-profile-task="orders"] [data-profile-task-status]')) === '排队')
  // 订单表做到一半：关系 3/4、码值 5/6、日期 1/1，发了 23 条查询，正在核对第 4 条关系
  settle(state, job, 'orders', { status: 'running', done: { relation: 3, codes: 5, date: 1 }, queries: 23, elapsed_ms: 18000,
    current: '正在核对 orders.coupon_id → coupons.id' })
  check('进行中：逐表一行写各类检查的进度「关系 3/4 · 码值 5/6 · 日期 1/1」', await until(async () =>
    (await inBox(page, '[data-profile-task="orders"] [data-profile-task-checks]')) === '关系 3/4 · 码值 5/6 · 日期 1/1'),
  await inBox(page, '[data-profile-task="orders"]'))
  check('……查询数、用时、正在做哪一项', (await inBox(page, '[data-profile-task="orders"] [data-profile-task-queries]')) === '23 条查询'
        && (await inBox(page, '[data-profile-task="orders"] [data-profile-task-elapsed]')) === '18 秒'
        && (await inBox(page, '[data-profile-task="orders"] [data-profile-task-note]')) === '正在核对 orders.coupon_id → coupons.id'
        && (await inBox(page, '[data-profile-task="orders"] [data-profile-task-status]')) === '剖析中')
  check('……头部：几张 / 共几张、已发几条查询、用时', (await inBox(page, '[data-profile-progress]')) === '0 / 5 张表'
        && (await inBox(page, '[data-profile-queries]')).includes('已发 23 条查询') && (await inBox(page, '[data-profile-elapsed]')).startsWith('· 用时'),
  await textOf(box(page).locator('[role="status"]')))
  check('……标题写「正在剖析…」', (await inBox(page, '[data-profile-title]')) === '正在剖析…')
  // 展开进行中的那一行：服务端只在一项检查开始、结束时写检查清单和发过的查询，其间每发一条查询只加查询数。所以只多了
  // 查询数时不重取这张表的完整结果，一项结束了、换了一项在做才重取（开发模式下 StrictMode 挂载时会多取一次，按展开后的
  // 次数为准）
  const detailGets = () => count(sent, new RegExp(`^GET .*profile-jobs/${job.id}/tasks/1$`))
  const jobPolls = () => count(sent, new RegExp(`^GET .*profile-jobs/${job.id}$`))
  const ordersTask = job.tasks.find((t) => t.table_name === 'orders')
  await taskLine(page, 'orders').locator('[data-profile-task-toggle]').click()
  const live = box(page).locator('[data-profile-task-detail="orders"]')
  check('进行中展开一行：取这张表的完整结果，正在做的那一项写「进行中」', await until(async () => (await live.locator('[data-profile-checks]').count()) === 1)
        && detailGets() >= 1 && (await live.locator('[data-profile-check][data-state="running"]').count()) === 1, `${detailGets()} 次`)
  await sleep(300)
  const gets0 = detailGets()
  for (const n of [25, 27]) {
    const p0 = jobPolls()
    Object.assign(ordersTask, { queries: n, elapsed_ms: ordersTask.elapsed_ms + 1500 })
    await until(async () => jobPolls() >= p0 + 2, 5000)
  }
  check('……这一项没结束、只多发了几条查询：逐表一行跟着走，不重取这张表的完整结果', (await inBox(page, '[data-profile-task="orders"] [data-profile-task-queries]')) === '27 条查询'
        && detailGets() === gets0, `展开后 ${gets0} 次 → ${detailGets()} 次`)
  settle(state, job, 'orders', { status: 'running', done: { relation: 4, codes: 5, date: 1 }, queries: 29, current: '正在取 orders.source 的码值候选' })
  check('……这一项结束、换了下一项：重取一次，检查清单跟上', await until(async () => detailGets() === gets0 + 1
        && (await live.locator('[data-profile-check="relation"][data-state="done"]').count()) === 4), `展开后 ${gets0} 次 → ${detailGets()} 次`)
  await taskLine(page, 'orders').locator('[data-profile-task-toggle]').click()
  await until(async () => (await live.count()) === 0)
  settle(state, job, 'orders', {
    queries: 31, elapsed_ms: 26000, added: 3, updated: 2, row_estimate: { rows: 250000, method: 'stats', at_least: null },
    findings: [
      { kind: 'relation', path: 'relations.customer_id → customers.id', target: 'customer_id → customers.id', columns: ['customer_id'], to_table: 'customers',
        to_columns: ['id'], status: 'verified', confirmed: false, previous_status: 'proposed', coverage: 0.991, cardinality: 'many_to_one', cardinality_checked: true,
        sample: 2000, matched: 1982, summary: '' },
      { kind: 'codes', path: 'columns.status.codes', column: 'status', values: [{ value: '1', rows: 200000 }, { value: '2', rows: 40000 }, { value: '9', rows: 10000 }],
        rows: 250000, status: 'proposed', pending: 2, summary: '' },
      { kind: 'codes', path: 'columns.channel.codes', column: 'channel', values: [{ value: 'web', rows: 150000 }, { value: 'app', rows: 100000 }],
        rows: 250000, status: 'proposed', pending: 2, summary: '' },
    ],
    query_log: [
      { at: isoNow(), kind: 'row_estimate', target: 'orders', sql: 'SELECT reltuples FROM pg_class WHERE relname = ?', ms: 8, rows: 1, outcome: 'ok', detail: null },
      { at: isoNow(), kind: 'relation', target: 'customer_id → customers.id', sql: 'SELECT DISTINCT customer_id FROM orders WHERE customer_id IS NOT NULL LIMIT ?',
        ms: 120, rows: 2000, outcome: 'ok', detail: null },
      { at: isoNow(), kind: 'relation', target: 'customer_id → customers.id', sql: 'SELECT id FROM customers WHERE id IN (?, ?, ?)', ms: 95, rows: 1982, outcome: 'ok', detail: null },
      { at: isoNow(), kind: 'relation', target: 'coupon_id → coupons.id', sql: 'SELECT DISTINCT coupon_id FROM orders WHERE coupon_id IS NOT NULL LIMIT ?',
        ms: 10000, rows: null, outcome: 'timeout', detail: '超过单条查询时限（10 秒），数据库已中断这条查询' },
    ],
  })
  const lists0 = count(sent, new RegExp(`^GET /datasources/${S1}/catalog$`))
  check('一张表剖析完：「1 / 5 张表」，进度条读屏可读', await until(async () => (await inBox(page, '[data-profile-progress]')) === '1 / 5 张表')
        && (await box(page).locator('[role="progressbar"]').getAttribute('aria-valuenow')) === '1'
        && (await box(page).locator('[role="progressbar"]').getAttribute('aria-valuemax')) === '5')
  check('……这张表写结论：核实了几条关系、取到几列码值候选（几个含义待填写）', (await inBox(page, '[data-profile-task="orders"] [data-profile-task-note]'))
        === '核实 1 条关系，取到 2 列码值候选（4 个含义待填写）', await inBox(page, '[data-profile-task="orders"] [data-profile-task-note]'))
  check('……写行数估算', (await inBox(page, '[data-profile-task="orders"]')).includes('约 250,000 行'))
  check('……头部写出超时的查询', (await inBox(page, '[data-profile-queries-bad]')).includes('超时 1 条'), await inBox(page, '[data-profile-queries-bad]'))
  check('写完的表马上并入目录：清单重取', await until(async () => count(sent, new RegExp(`^GET /datasources/${S1}/catalog$`)) > lists0))
  if (SHOTS) await sleep(350)
  await shot(page, 'profile-running')
  await cleanCopy(page, '进行中')

  // 关掉对话框：任务不停，页头只换图标、读屏写「进行中」，进度在下方的后台任务条上；表清单的「剖析」一格写排队、剖析中
  settle(state, job, 'visits', { status: 'running', done: { relation: 0, codes: 1, date: 0 }, queries: 3, elapsed_ms: 1500, current: '正在取 visits.page 的码值候选' })
  await until(async () => (await inBox(page, '[data-profile-task="visits"] [data-profile-task-status]')) === '剖析中')
  await page.locator('[data-profile-hide]').click()
  check('「在后台继续」：对话框关上，没有发停止', await until(async () => (await box(page).count()) === 0) && count(sent, /\/cancel$/) === 0)
  check('……页头只做入口：读屏写「数据剖析（进行中）」，不写数字', await until(async () => (await head(page).getAttribute('data-catalog-profile')) === 'running')
        && (await head(page).getAttribute('aria-label')) === '数据剖析（进行中）' && !/\d/.test(await textOf(head(page))), await head(page).getAttribute('aria-label'))
  check('……进度在后台任务条上：「剖析 1/5」', await until(async () => (await stripText(page, 'profile')) === '剖析 1/5'), await stripText(page, 'profile'))
  check('……表清单：剖析完的写「刚刚剖析」，正在剖析的写「剖析中」，后面的写「排队剖析」', await until(async () =>
    (await rowProfile(page, 'orders')) === '刚刚剖析' && (await rowProfile(page, 'visits')) === '剖析中' && (await rowProfile(page, 'products')) === '排队剖析'),
  [await rowProfile(page, 'orders'), await rowProfile(page, 'visits'), await rowProfile(page, 'products')].join(' / '))
  // 最近查询多数超时：服务端给提醒，关着对话框时写在后台任务条上剖析那一行（全文，窄屏、触屏也看得见），页头不再挂提醒
  const NOTICE = '最近 6 条查询有 4 条超时，数据库响应变慢了；剖析会继续，可以停止，换在业务不忙的时候再剖析'
  job.notice = NOTICE
  check('多数超时的提醒：写在后台任务条上剖析那一行，写全文', await until(async () =>
    (await textOf(stripJob(page, 'profile').locator('[data-catalog-job-notice]'))) === NOTICE))
  check('……页头只做入口，不带提醒标记', await page.locator('[data-catalog-profile-notice]').count() === 0)
  await shot(page, 'profile-header')
  await head(page).click()
  check('点页头：回到对话框，直接是进行中的任务，提醒写在进度条下面', await until(async () => (await phase(page)) === 'running')
        && (await inBox(page, '[data-profile-notice]')) === NOTICE && await box(page).locator('[data-profile-scope]').count() === 0)

  // 停止：调停止接口。服务端不再等正在执行的那条查询（数据库按单条时限截止它），收尾写完就答，这期间写「正在停止」
  await page.locator('[data-profile-stop]').click()
  check('点「停止」：调停止接口，写「正在停止：正在执行的那条查询不再等待，由数据库按单条时限截止…」，停止按钮不能再点', await until(async () =>
    (await inBox(page, '[data-profile-title]')).startsWith('正在停止：正在执行的那条查询不再等待，由数据库按单条时限截止'))
    && !(await inBox(page, '[data-profile-title]')).includes('等正在执行的那条查询结束')
    && count(sent, new RegExp(`^POST .*profile-jobs/${job.id}/cancel$`)) === 1 && await page.locator('[data-profile-stop]').isDisabled(),
  await inBox(page, '[data-profile-title]'))
  // 服务端：正在剖析的访问记录来得及收尾，做完的 1 项写进目录、剩下 3 项没查（部分完成），后面的表已停止
  stopJob(job, { rest: '剖析已停止：已做完的 1 项照常写进了数据目录，剩下 3 项没查' })
  check('……停下来：标题写「已停止」，写明做完的已写进目录、没做的保留原来的结论', await until(async () => (await phase(page)) === 'done')
        && (await inBox(page, '[data-profile-title]')) === '已停止' && (await inBox(page, '[data-profile-cancelled]')).includes('没做的项保留原来的结论')
        && !(await inBox(page, '[data-profile-cancelled]')).includes('没来得及'),
  `${await inBox(page, '[data-profile-title]')} · ${await inBox(page, '[data-profile-cancelled]')}`)
  check('……正在剖析的那张写部分完成和服务端的原因；后面的表都记已停止', (await inBox(page, '[data-profile-task="visits"] [data-profile-task-status]')) === '部分完成'
        && (await inBox(page, '[data-profile-task="visits"] [data-profile-task-note]')).includes('已做完的 1 项照常写进了数据目录')
        && (await inBox(page, '[data-profile-task="tmp_log"] [data-profile-task-status]')) === '已停止'
        && await taskLine(page, 'tmp_log').locator('[data-profile-task-checks]').count() === 0, await inBox(page, '[data-profile-task="visits"]'))
  check('……汇总照实写：5 张表，完成 1，部分完成 1，已停止 3', (await inBox(page, '[data-profile-counts]')) === '5 张表：完成 1，部分完成 1，已停止 3',
    await inBox(page, '[data-profile-counts]'))
  check('……可以重试没做完的 4 张', (await textOf(page.locator('[data-profile-retry]'))).includes('重试没做完的表（4 张）'))
  check('……页头回到「数据剖析」，任务条收起', await until(async () => (await head(page).getAttribute('data-catalog-profile')) === ''
        && (await head(page).getAttribute('aria-label')) === '数据剖析' && await stripJob(page, 'profile').count() === 0))
  const polls = () => count(sent, new RegExp(`^GET .*profile-jobs/${job.id}$`))
  const n = polls()
  await sleep(3200)
  check('结束以后不再取进度', polls() === n, `${n} → ${polls()}`)
  check('写请求只有建任务和停止', writes(sent, /./).every((s) => /profile-jobs(\/[^/]+\/cancel)?$/.test(s.key)), JSON.stringify(writes(sent, /./).map((s) => s.key)))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('剖析 · 结束后：汇总、筛选、展开与重试', async () => {
  const state = freshState()
  const replies = { retry: [] }
  const { page, sent, errors, natives, close } = await open(`/data/catalog/${S1}`, { handlers: handlers(state, replies) })
  await page.locator('[data-catalog-row]').first().waitFor()
  await openDialog(page)
  await box(page).locator('[data-profile-scope="all"]').click()
  await page.locator('[data-profile-start]').click()
  await until(async () => (await phase(page)) === 'running')
  check('全部表：请求带「全部」范围', JSON.stringify(lastBody(sent, /profile-jobs$/)) === JSON.stringify({ scope: 'all' }))
  const job = state.pjobs.at(-1)
  settle(state, job, 'orders', { queries: 31, elapsed_ms: 26000, added: 2, updated: 1, findings: [
    { kind: 'relation', path: 'relations.customer_id → customers.id', target: 'customer_id → customers.id', columns: ['customer_id'], to_table: 'customers',
      to_columns: ['id'], status: 'verified', confirmed: false, previous_status: 'proposed', coverage: 0.991, cardinality: 'many_to_one', cardinality_checked: true,
      sample: 2000, matched: 1982, summary: '' },
    { kind: 'relation', path: 'relations.coupon_id → coupons.id', target: 'coupon_id → coupons.id', columns: ['coupon_id'], to_table: 'coupons', to_columns: ['id'],
      status: 'proposed', confirmed: false, previous_status: 'proposed', coverage: 0.4, cardinality: 'many_to_one', cardinality_checked: false, sample: 500, matched: 200, summary: '' },
    { kind: 'codes', path: 'columns.status.codes', column: 'status', values: [{ value: '1', rows: 9 }, { value: '2', rows: 3 }, { value: '9', rows: 1 }], rows: 13,
      status: 'proposed', pending: 2, summary: '' },
    { kind: 'codes', path: 'columns.channel.codes', column: 'channel', values: [{ value: 'web', rows: 8 }, { value: 'app', rows: 5 }], rows: 13,
      status: 'proposed', pending: 2, summary: '' },
    { kind: 'business_date', path: 'business_date', column: 'created_at', min: '2024-01-01 00:00:00', max: '2026-10-14 23:59:00', status: 'proposed', summary: '' },
  ] })
  settle(state, job, 'customers', { queries: 4, elapsed_ms: 2000 })
  // 访问记录：这张表的 40 条查询用完了，2 项没查成（旧结论保留）
  settle(state, job, 'visits', { status: 'partial', done: { relation: 0, codes: 1, date: 1 }, queries: 40, elapsed_ms: 33000,
    skip: [['relation', 0, 'budget', '这张表的查询次数用完（40 条），这一项没有查，保留原来的结论'], ['codes', 1, 'timeout', '超过单条查询时限（10 秒），这一项没有查成']],
    query_log: [
      { at: isoNow(), kind: 'row_estimate', target: 'visits', sql: 'SELECT COUNT(*) FROM (SELECT 1 FROM visits LIMIT ?) t', ms: 30, rows: 1, outcome: 'ok', detail: null },
      { at: isoNow(), kind: 'codes', target: 'page', sql: 'SELECT page, COUNT(*) FROM visits GROUP BY page ORDER BY COUNT(*) DESC LIMIT ?', ms: 10000, rows: null,
        outcome: 'timeout', detail: '超过单条查询时限（10 秒），数据库已中断这条查询' },
      { at: isoNow(), kind: 'date', target: 'visited_at', sql: "SELECT MIN(visited_at), MAX(visited_at) FROM visits WHERE visited_at > ?", ms: 60, rows: 1, outcome: 'ok', detail: null },
    ] })
  settle(state, job, 'products', { queries: 6, elapsed_ms: 3000 })
  settle(state, job, 'refunds', { status: 'failed', queries: 3, elapsed_ms: 800, error: '写入一直冲突：剖析期间这张表的数据目录被反复修改，结论没有写进去' })
  settle(state, job, 'tmp_log', { status: 'skipped', queries: 1, elapsed_ms: 100, error: '空表，没有可剖析的数据' })
  finish(job)
  check('结束：标题写「剖析完成，部分表没做完」', await until(async () => (await phase(page)) === 'done')
        && (await inBox(page, '[data-profile-title]')) === '剖析完成，部分表没做完', await inBox(page, '[data-profile-title]'))
  check('……进度写做完了几张「完成 3 / 6 张表」（跳过、失败、部分完成的不算），进度条同', (await inBox(page, '[data-profile-progress]')) === '完成 3 / 6 张表'
        && (await box(page).locator('[role="progressbar"]').getAttribute('aria-valuenow')) === '3', await inBox(page, '[data-profile-progress]'))
  check('……汇总：6 张表：完成 3，部分完成 1，跳过 1，失败 1', (await inBox(page, '[data-profile-counts]')) === '6 张表：完成 3，部分完成 1，跳过 1，失败 1',
    await inBox(page, '[data-profile-counts]'))
  check('……发现的合计：核实了几条、几条仍为推断、几列码值候选（几个含义待填写）、几张表的业务日期', (await inBox(page, '[data-profile-findings-summary]'))
        === '核实 1 条关系，1 条关系仍为推断，取到 2 列码值候选（4 个含义待填写），提议 1 张表的业务日期', await inBox(page, '[data-profile-findings-summary]'))
  check('……目录的变化', (await inBox(page, '[data-profile-changes]')) === '目录新增 2 项、更新 1 项')
  check('……查询次数用完的表写「40 条查询（已用完）」，没查成的项数', (await inBox(page, '[data-profile-task="visits"] [data-profile-task-queries]')) === '40 条查询（已用完）'
        && (await inBox(page, '[data-profile-task="visits"] [data-profile-task-note]')).includes('2 项没查成'), await inBox(page, '[data-profile-task="visits"]'))
  check('……空表、写入冲突照服务端的原话', (await inBox(page, '[data-profile-task="tmp_log"] [data-profile-task-note]')) === '空表，没有可剖析的数据'
        && (await inBox(page, '[data-profile-task="refunds"] [data-profile-task-note]')).startsWith('写入一直冲突'))
  // 筛选：有发现 / 有跳过 / 失败
  const chips = await box(page).locator('[data-profile-filter-key]').evaluateAll((els) => els.map((e) => e.innerText.replace(/\s+/g, '')))
  check('筛选带数量：全部 6、有发现 1、有跳过 2、失败 1', chips.join('|') === '全部6|有发现1|有跳过2|失败1', chips.join('|'))
  const shown = () => box(page).locator('[data-profile-task]').evaluateAll((els) => els.map((e) => e.getAttribute('data-profile-task')).join(','))
  await box(page).locator('[data-profile-filter-key="failed"]').click()
  check('「失败」：只有写入冲突的退款', (await shown()) === 'refunds', await shown())
  await box(page).locator('[data-profile-filter-key="skipped"]').click()
  check('「有跳过」：访问记录（有项没查成）、空日志表', (await shown()) === 'visits,tmp_log', await shown())
  await box(page).locator('[data-profile-filter-key="findings"]').click()
  check('「有发现」：订单', (await shown()) === 'orders', await shown())
  await box(page).locator('[data-profile-filter-key="all"]').click()

  // 展开一张表：检查清单（每项状态、原因、几条查询）、发过的查询（字面量换成了 ?）
  await taskLine(page, 'visits').locator('[data-profile-task-toggle]').click()
  const visits = box(page).locator('[data-profile-task-detail="visits"]')
  check('点一行展开：按 seq 取这张表的完整结果', await until(async () => (await visits.locator('[data-profile-checks]').count()) === 1)
        && count(sent, new RegExp(`^GET .*profile-jobs/${job.id}/tasks/3$`)) >= 1
        && (await taskLine(page, 'visits').locator('[data-profile-task-toggle]').getAttribute('aria-expanded')) === 'true')
  const checksList = await visits.locator('[data-profile-check]').evaluateAll((els) => els.map((e) => `${e.getAttribute('data-profile-check')}:${e.getAttribute('data-state')}`))
  check('……检查清单 4 项：关系没查成、码值一项完成一项没查成、日期完成', checksList.join(',') === 'relation:skipped,codes:done,codes:skipped,date:done', checksList.join(','))
  check('……没查成的写原因（中文）和服务端的整句', (await textOf(visits.locator('[data-profile-check="relation"]'))).includes('本表查询次数用完')
        && (await textOf(visits.locator('[data-profile-check="relation"]'))).includes('保留原来的结论')
        && (await textOf(visits.locator('[data-profile-check="codes"][data-state="skipped"]'))).includes('查询超时'))
  check('……检查清单的标题写几项结束了', (await textOf(visits.locator('[data-profile-checks] > div').first())) === '检查清单（4 / 4 项）')
  const log = visits.locator('[data-profile-queries-log]')
  check('……发过的查询折着：「看发过的查询（3 条）」', (await textOf(log.locator('summary'))) === '看发过的查询（3 条）')
  await log.locator('summary').click()
  const sqls = await log.locator('[data-profile-query-sql]').allInnerTexts()
  check('……展开：每条的 SQL 照服务端给的写（字面量都是 ?），写明为什么', sqls.length === 3 && sqls.every((q) => q.includes('?'))
        && (await textOf(log)).includes('SQL 里的取值都换成了「?」'), sqls.join(' | '))
  check('……结果写中文：成功、超时（带原因），写用时和行数', (await textOf(log.locator('[data-profile-query][data-outcome="timeout"]'))).includes('超时')
        && (await textOf(log.locator('[data-profile-query][data-outcome="timeout"]'))).includes('数据库已中断这条查询')
        && (await textOf(log.locator('[data-profile-query]').first())).includes('30 毫秒'))
  if (SHOTS) await sleep(350)
  await shot(page, 'profile-expanded')
  // 有发现的表：关系的结论、码值的「填写含义」
  await taskLine(page, 'orders').locator('[data-profile-task-toggle]').click()
  const orders = box(page).locator('[data-profile-task-detail="orders"]')
  await until(async () => (await orders.locator('[data-profile-finding]').count()) > 0)
  check('展开有发现的表：关系的结论「升为已验证」、覆盖率不足的「保持推断」', (await textOf(orders.locator('[data-profile-outcome="raised"]'))) === '升为已验证'
        && (await textOf(orders.locator('[data-profile-outcome="kept"]'))).startsWith('保持推断：覆盖率不足'))
  check('……码值候选给「填写含义」，提议的业务日期', await orders.locator('[data-profile-fill="status"]').count() === 1
        && (await textOf(orders.locator('[data-profile-finding="business_date"]'))).includes('提议按 created_at 作为业务日期'))
  await cleanCopy(page, '结束后的结果')
  // 填写含义：对话框收起，打开那张表、弹出那一列的码值；取消后回到结果，那张表照旧展开，焦点落在下一列的「填写含义」上
  await orders.locator('[data-profile-fill="status"]').click()
  const codes = page.locator('[data-codes-dialog="status"]')
  check('点「填写含义」：对话框收起，弹出这一列的码值', await until(async () => (await codes.count()) === 1) && (await box(page).count()) === 0)
  check('……每个码值一个输入框，空着的写「含义待填写」，光标在第一个空着的上面', await codes.locator('input[data-code]').count() === 3
        && (await codes.locator('input[data-code="2"]').getAttribute('placeholder')) === '含义待填写'
        && await until(async () => page.evaluate(() => document.activeElement?.getAttribute('data-code') === '2')))
  check('……没填时保存禁用', await page.locator('[data-codes-save]').isDisabled())
  await page.keyboard.press('Escape')
  check('……取消：回到结果，那张表照旧展开，焦点在下一列（channel）的「填写含义」上', await until(async () => (await phase(page)) === 'done')
        && await until(async () => (await box(page).locator('[data-profile-task-detail="orders"] [data-profile-fill]').count()) === 2)
        && await until(async () => page.evaluate(() => document.activeElement?.getAttribute('data-profile-fill') === 'channel'), 2000),
  await page.evaluate(() => document.activeElement?.outerHTML?.slice(0, 120) ?? ''))

  // 接着填下一列：填完回车保存（整份提交，只换这一列码值的含义，带读到的版本），回到结果，这一列写「含义已填写」
  await page.keyboard.press('Enter')
  const channel = page.locator('[data-codes-dialog="channel"]')
  check('……回车接着填下一列', await until(async () => (await channel.count()) === 1))
  const v0 = state.tables[S1].find((t) => t.name === 'orders').version
  await channel.locator('input[data-code="web"]').fill('网站')
  await channel.locator('input[data-code="app"]').fill('应用')
  await page.keyboard.press('Enter')
  const put = () => lastBody(sent, /^PUT .*catalog\/orders$/)
  check('回车保存：整份提交，只换这一列码值的含义，带读到的版本', await until(async () => (await channel.count()) === 0 && !!put())
        && JSON.stringify(put()?.notes?.columns?.channel?.codes?.value) === JSON.stringify({ web: '网站', app: '应用' }) && put()?.if_version === v0,
  JSON.stringify(put()?.notes?.columns?.channel))
  const channelLine = box(page).locator('[data-profile-task-detail="orders"] [data-profile-finding="codes"][data-column="channel"]')
  check('……回到结果：这一列写「含义已填写」，按钮改成「修改含义」，前一列照旧待填写', await until(async () => (await phase(page)) === 'done')
        && await until(async () => (await textOf(channelLine.locator('[data-codes-filled]'))).includes('含义已填写'))
        && (await textOf(channelLine.locator('[data-profile-fill]'))) === '修改含义' && await channelLine.locator('[data-codes-pending]').count() === 0
        && (await textOf(box(page).locator('[data-profile-task-detail="orders"] [data-column="status"] [data-codes-pending]'))) === '2 个含义待填写',
  await textOf(channelLine))

  // 重试没做完的表：别处又建了一个（正忙）时写清是谁的；之后只重试访问记录、退款
  replies.retry.push(json({ detail: `数据源「${NAME1}」正在进行数据剖析（已完成 0 / 3 张表），请等这次剖析结束（或先停止它）再开始新的剖析`, code: 'profile_busy' }, 409))
  await page.locator('[data-profile-retry]').click()
  check('重试撞上正忙（409）：转到「开始不了」，写明正忙和下一步', await until(async () => (await phase(page)) === 'blocked')
        && (await inBox(page, '[data-profile-blocked="busy"]')).includes('这个数据源已有剖析任务'), await inBox(page, '[data-profile-blocked]'))
  await page.locator('[data-profile-back]').click()
  await page.locator('[data-profile-last-open]').click()
  await until(async () => (await phase(page)) === 'done')
  check('……「返回」再点开上一次：还是这次的结果', (await inBox(page, '[data-profile-counts]')).startsWith('6 张表'))
  await page.locator('[data-profile-retry]').click()
  check('重试：只含部分完成和失败的 2 张，进入新任务的进行中', await until(async () => (await phase(page)) === 'running')
        && JSON.stringify(state.pjobs.at(-1).tasks.map((t) => t.table_name)) === JSON.stringify(['visits', 'refunds'])
        && state.pjobs.at(-1).retry_of === job.id && (await box(page).locator('[data-profile-task]').count()) === 2)
  const again = state.pjobs.at(-1)
  // 关着对话框时结束：右下角提示，带「查看报告」
  await page.locator('[data-profile-hide]').click()
  settle(state, again, 'visits', { queries: 12, elapsed_ms: 9000 })
  settle(state, again, 'refunds', { queries: 3, elapsed_ms: 900 })
  finish(again)
  const toastBox = page.locator('[data-toast]').filter({ hasText: '数据剖析完成' })
  check('关着对话框时结束：提示「数据剖析完成」，带「查看报告」', await until(async () => (await toastBox.count()) > 0), await toasts(page))
  await toastBox.getByRole('button', { name: '查看报告' }).click()
  check('……点「查看报告」打开这一次的结果', await until(async () => (await phase(page)) === 'done') && (await inBox(page, '[data-profile-title]')) === '剖析完成')
  check('……都做完了：不再给重试', await page.locator('[data-profile-retry]').count() === 0)
  await page.locator('[data-profile-close]').click()
  await openDialog(page)
  check('结果看过了，再点页头就是重新选范围', (await phase(page)) === 'setup')
  check('写请求只有建任务、重试和保存码值的含义', writes(sent, /./).every((s) => /profile-jobs(\/[^/]+\/retry)?$|^PUT .*catalog\/orders$/.test(s.key)),
    JSON.stringify(writes(sent, /./).map((s) => s.key)))
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('剖析 · 止损与服务重启', async () => {
  const state = freshState()
  const { page, errors, close } = await open(`/data/catalog/${S1}`, { handlers: handlers(state) })
  await page.locator('[data-catalog-row]').first().waitFor()
  await openDialog(page)
  await page.locator('[data-profile-start]').click()
  await until(async () => (await phase(page)) === 'running')
  const job = state.pjobs.at(-1)
  settle(state, job, 'orders', { queries: 31, elapsed_ms: 26000 })
  settle(state, job, 'visits', { status: 'running', done: { relation: 1 }, queries: 9, elapsed_ms: 70000 })
  // 最近 10 条查询里 7 条超时：任务停下，后面的表记已停止、原因同一句
  const SLOW = '数据库响应慢：最近 10 条查询 7 条超时，已停止，避免给业务库加压'
  job.stop_reason = 'slow'
  stopJob(job, { status: 'partial', reason: 'slow', current: '数据库响应慢，这一项没有查', error: SLOW, rest: SLOW })
  check('止损（多数超时）：写「数据库响应慢，剖析已停止」和服务端的整句、下一步，给「修改设置」', await until(async () => (await phase(page)) === 'done')
        && (await inBox(page, '[data-profile-stopped="slow"]')).includes('数据库响应慢，剖析已停止')
        && (await inBox(page, '[data-profile-stopped="slow"]')).includes(SLOW)
        && (await inBox(page, '[data-profile-stopped="slow"]')).includes('业务库不忙的时候重试没做完的表')
        && await box(page).locator('[data-profile-stopped] [data-profile-settings-open]').count() === 1, await inBox(page, '[data-profile-stopped]'))
  check('……同一句原因不逐表重复', await box(page).locator('[data-profile-task-note]').filter({ hasText: SLOW }).count() === 0)
  check('……后面的表记已停止，可以重试', (await inBox(page, '[data-profile-task="products"] [data-profile-task-status]')) === '已停止'
        && (await textOf(page.locator('[data-profile-retry]'))).includes('（4 张）'))
  await shot(page, 'profile-slow')
  await page.locator('[data-profile-close]').click()

  // 连续失败：写出第一行报错，下一步是先测试连接
  await openDialog(page)
  await page.locator('[data-profile-start]').click()
  await until(async () => (await phase(page)) === 'running')
  const job2 = state.pjobs.at(-1)
  const FAIL = '连续 3 条查询失败，已停止：could not connect to server: Connection refused'
  job2.stop_reason = 'failed'
  settle(state, job2, job2.tasks[0].table_name, { status: 'running', queries: 3 })
  stopJob(job2, { status: 'partial', reason: 'failed', error: FAIL, rest: FAIL })
  check('止损（连续失败）：写「连续多条查询失败」、服务端的第一行报错、先测试连接', await until(async () => (await phase(page)) === 'done')
        && (await inBox(page, '[data-profile-stopped="failed"]')).includes('连续多条查询失败，剖析已停止')
        && (await inBox(page, '[data-profile-stop-error]')) === FAIL && (await inBox(page, '[data-profile-stopped="failed"]')).includes('测试连接'))
  await page.locator('[data-profile-close]').click()

  // 服务重启：任务标中断，不自动续跑。关停服务时正在剖析的那张表来得及收尾（做完的项写进目录），后面的表已停止
  const RESTART = '服务重启，剖析中断'
  await openDialog(page)
  await page.locator('[data-profile-start]').click()
  await until(async () => (await phase(page)) === 'running')
  const job3 = state.pjobs.at(-1)
  const [first3, second3] = job3.tasks.map((t) => t.table_name)
  settle(state, job3, first3, { queries: 5 })
  settle(state, job3, second3, { status: 'running', done: { relation: 1 }, queries: 4 })
  const left3 = job3.tasks[1].checks.filter((c) => c.state !== 'done').length
  stopJob(job3, { status: 'interrupted', error: RESTART, rest: `${RESTART}：已做完的 1 项照常写进了数据目录，剩下 ${left3} 项没查` })
  check('服务重启（关停时收了尾）：标题写「服务重启，剖析中断」，写明做完的项已写进目录、不会自动续上、可以重试', await until(async () => (await phase(page)) === 'done')
        && (await inBox(page, '[data-profile-title]')) === RESTART && (await inBox(page, '[data-profile-interrupted]')).includes('不会自动续上')
        && (await inBox(page, '[data-profile-interrupted]')).includes('做完的项已写进数据目录')
        && !(await inBox(page, '[data-profile-interrupted]')).includes('没来得及'), await inBox(page, '[data-profile-interrupted]'))
  check('……正在剖析的那张：部分完成，写服务端的原因（做完的写进了目录）', (await inBox(page, `[data-profile-task="${second3}"] [data-profile-task-status]`)) === '部分完成'
        && (await inBox(page, `[data-profile-task="${second3}"] [data-profile-task-note]`)).includes('已做完的 1 项照常写进了数据目录'),
  await inBox(page, `[data-profile-task="${second3}"]`))
  check('……和任务相同的原因不逐表重复（只有收了尾的那张写它自己的一句）', await box(page).locator('[data-profile-task-note]').filter({ hasText: RESTART }).count() === 1)
  await page.locator('[data-profile-close]').click()

  // 进程被杀：下次启动时才标中断，正在剖析的那张表没来得及收尾，这次的结论一项都没写进目录（服务端把查完的项改记跳过）。
  // 不能说「做完的项已写进数据目录」：点名这张表，写明它这次的结论没写进去
  await openDialog(page)
  await page.locator('[data-profile-start]').click()
  await until(async () => (await phase(page)) === 'running')
  const job4 = state.pjobs.at(-1)
  const [first4, lost4] = job4.tasks.map((t) => t.table_name)
  settle(state, job4, first4, { queries: 5 })
  settle(state, job4, lost4, { status: 'running', done: { relation: 1 }, queries: 4 })
  stopJob(job4, { status: 'interrupted', error: RESTART, current: `${RESTART}，未检查`, saved: false })
  const note = () => inBox(page, '[data-profile-interrupted]')
  check('服务重启（进程被杀、没来得及收尾）：点名剖析到一半的那张表，写明它这次的结论没写进目录、保留原来的结论', await until(async () => (await phase(page)) === 'done')
        && (await note()).includes(`「${lost4}」剖析到一半服务就停了`) && (await note()).includes('这次的结论没有写进目录') && (await note()).includes('保留原来的结论')
        && (await note()).includes('其余做完的项已写进数据目录') && (await note()).includes('不会自动续上'), await note())
  check('……不再一律说「做完的项已写进数据目录」', !(await note()).startsWith('服务重启时这次剖析还没有完成：做完的项已写进数据目录'), await note())
  check('……那张表记已停止，这一行照服务端写明查完的项没写进目录；前面剖析完的表照常', (await inBox(page, `[data-profile-task="${lost4}"] [data-profile-task-status]`)) === '已停止'
        && (await inBox(page, `[data-profile-task="${lost4}"] [data-profile-task-note]`)) === `${RESTART}：已查完的 1 项没有写进数据目录，剩下 1 项没查`
        && (await inBox(page, `[data-profile-task="${first4}"] [data-profile-task-status]`)) === '完成', await inBox(page, `[data-profile-task="${lost4}"]`))
  await taskLine(page, lost4).locator('[data-profile-task-toggle]').click()
  const lostBox = box(page).locator(`[data-profile-task-detail="${lost4}"]`)
  check('……展开：检查清单没有标「完成」的项，查完的那项写明结论没写进目录', await until(async () => (await lostBox.locator('[data-profile-checks]').count()) === 1)
        && await lostBox.locator('[data-profile-check][data-state="done"]').count() === 0
        && (await textOf(lostBox.locator('[data-profile-check="relation"]'))).includes('查完了但结论没有写进数据目录'), await textOf(lostBox.locator('[data-profile-checks]')))
  check('……没开始的表不写这一句', await box(page).locator('[data-profile-task-note]').filter({ hasText: '写进' }).count() === 1)
  await shot(page, 'profile-interrupted')
  await page.locator('[data-profile-close]').click()

  // 进程被杀时那张表一项都还没查完（还在估行数）：服务端给它的原因就是任务的那一句，这一行改写「没来得及收尾」；
  // 也没有别的表收了尾，不说「其余做完的项已写进」
  await openDialog(page)
  await page.locator('[data-profile-start]').click()
  await until(async () => (await phase(page)) === 'running')
  const job5 = state.pjobs.at(-1)
  const lost5 = job5.tasks[0].table_name
  settle(state, job5, lost5, { status: 'running', queries: 1, current: `正在估算 ${lost5} 的行数` })
  stopJob(job5, { status: 'interrupted', error: RESTART, current: `${RESTART}，未检查`, saved: false })
  check('……一项都还没查完就被杀：这一行写「没来得及收尾，这次的结论没有写进目录」，说明里不提「其余做完的项」', await until(async () => (await phase(page)) === 'done')
        && (await inBox(page, `[data-profile-task="${lost5}"] [data-profile-task-note]`)) === '剖析到一半中断，没来得及收尾，这次的结论没有写进目录，保留原来的结论'
        && (await note()).includes(`「${lost5}」剖析到一半服务就停了`) && !(await note()).includes('其余做完的项'),
  `${await inBox(page, `[data-profile-task="${lost5}"]`)} · ${await note()}`)
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('剖析 · 开始不了（409）', async () => {
  const state = freshState()
  const replies = { create: [], patch: [] }
  // 这个源在数据源列表里没开剖析（服务端也会说没开）
  state.sources = state.sources.map((s) => (s.id === S1 ? { ...s, options: {} } : s))
  const { page, sent, errors, natives, close } = await open(`/data/catalog/${S1}`, { handlers: handlers(state, replies) })
  await page.locator('[data-catalog-row]').first().waitFor()
  await openDialog(page)
  check('没开启：写明需要先开启，「开始剖析」禁用，没有预算', (await inBox(page, '[data-profile-disabled]')).includes('未开启数据剖析')
        && await page.locator('[data-profile-start]').isDisabled() && await box(page).locator('[data-profile-budget]').count() === 0)
  await box(page).locator('[data-profile-disabled] [data-profile-enable]').click()
  const settings = page.locator('[data-profile-settings-dialog]')
  await settings.waitFor()
  await settings.locator('[data-profile-enabled]').check()
  await page.locator(`#profile-${S1}-table_max_queries`).fill('25')
  await page.locator('[data-profile-save]').click()
  check('就地开启：保存后回到剖析，预算按刚存的设置', await until(async () => (await settings.count()) === 0)
        && await until(async () => (await inBox(page, '[data-profile-budget]')).includes('每张表最多 25 条查询'))
        && !(await page.locator('[data-profile-start]').isDisabled()))
  check('……保存的是这个源的剖析设置', JSON.stringify(writes(sent, /^PATCH /).at(-1)?.body) === JSON.stringify({ options: { catalog_profile: { enabled: true, table_max_queries: 25 } } }),
    JSON.stringify(writes(sent, /^PATCH /).at(-1)?.body))

  // 服务端的 409 都带机读码，界面按它分情况，不从原话里认
  const cases = [
    ['disabled', '数据源「zzprofshop」未开启数据剖析。剖析会对业务库发查询，请先在数据源设置中开启数据剖析', '未开启数据剖析', 'data-profile-enable', 'profile_disabled'],
    ['inactive', '数据源「zzprofshop」已停用，无法进行数据剖析。请先在数据源设置中启用', '数据源已停用', 'data-profile-go-source', 'datasource_inactive'],
    ['noSchema', '尚未探查结构，无法进行数据剖析。请先在数据源卡片上点「探查结构」', '还没有表结构', 'data-profile-go-source', 'schema_missing'],
    ['tampered', '数据文件与登记的版本不一致，可能被修改过，已拒绝查询', '数据文件核对未通过', 'data-profile-go-source', 'snapshot_tampered'],
    ['nothing', '按「需要剖析的表」挑出 0 张表：表都剖析过、上次都做完了，也没有新的待核实关联关系。可以改选「全部表」，或指定要剖析的表', '没有可剖析的表',
      'data-profile-choose-all', 'profile_nothing'],
  ]
  for (const [kind, detail, title, action, code] of cases) {
    replies.create.push(json({ detail, code }, 409))
    await page.locator('[data-profile-start]').click()
    const blocked = box(page).locator(`[data-profile-blocked="${kind}"]`)
    const ok = await until(async () => (await blocked.count()) === 1)
    const text = ok ? await textOf(blocked) : ''
    check(`409（${title}）：写明是哪种情况、服务端原话和下一步，给对应的按钮`, ok && text.includes(title) && text.includes(detail)
          && await blocked.locator(`[${action}]`).count() === 1, text.slice(0, 160))
    if (kind === 'nothing') {
      // 改选全部表：这回建得起来
      await blocked.locator('[data-profile-choose-all]').click()
      check('……「改选全部表」：按全部表再建一次，进入进行中', await until(async () => (await phase(page)) === 'running')
            && JSON.stringify(lastBody(sent, /profile-jobs$/)) === JSON.stringify({ scope: 'all' }))
      stopJob(state.pjobs.at(-1))
      await until(async () => (await phase(page)) === 'done')
      await page.locator('[data-profile-again]').click()
      await until(async () => (await phase(page)) === 'setup')
      continue
    }
    if (kind === 'disabled') {
      // 服务端说没开（列表里的设置过时了）：同样就地开启
      await blocked.locator('[data-profile-enable]').click()
      await settings.waitFor()
      await page.locator(`#profile-${S1}-table_max_queries`).fill('30')
      await page.locator('[data-profile-save]').click()
      check('……就地开启后回到选范围', await until(async () => (await phase(page)) === 'setup'))
      continue
    }
    await page.locator('[data-profile-back]').click()
    await until(async () => (await phase(page)) === 'setup')
  }
  // 没带机读码的 409（老服务端）：不从原话里猜，按「无法开始剖析」说，给重试
  replies.create.push(json({ detail: '数据源正在进行数据剖析，请等待完成后再试' }, 409))
  await page.locator('[data-profile-start]').click()
  check('409 没带机读码：不从原话里猜，按「无法开始剖析」说，给重试', await until(async () => (await box(page).locator('[data-profile-blocked="other"]').count()) === 1)
        && await box(page).locator('[data-profile-retry-start]').count() === 1)
  await box(page).locator('[data-profile-retry-start]').click()
  check('……「重试」同一个范围再发一次，这次建起来', await until(async () => (await phase(page)) === 'running'))
  stopJob(state.pjobs.at(-1))
  await until(async () => (await phase(page)) === 'done')
  const latestN = count(sent, /profile-jobs\/latest$/)
  await page.locator('[data-profile-again]').click()
  await until(async () => (await phase(page)) === 'setup')
  // 正忙：别处（另一个标签页）在这一页打开以后建了一个，写清是谁的、到哪了，可以去看它的进度。先等设置阶段把上一次剖析
  // 取回来，免得它晚到、取回的已经是别处那个进行中的任务（那样会直接转去显示它，走不到 409）
  await until(async () => count(sent, /profile-jobs\/latest$/) > latestN)
  await sleep(300)
  const other = newProfileJob(state, S1, { tables: ['orders', 'customers', 'visits'], scope: 'selected', created_by: OTHER })
  settle(state, other, 'orders', { queries: 20 })
  await page.locator('[data-profile-start]').click()
  const busy = box(page).locator('[data-profile-blocked="busy"]')
  check('409（正忙）：写服务端原话、谁发起的、已完成几张，下一步可以查看它的进度', await until(async () => (await busy.count()) === 1)
        && (await textOf(busy)).includes('已完成 1 / 3 张表') && (await inBox(page, '[data-profile-busy-at]')) === `由「${OTHER}」发起，已完成 1 / 3 张表`
        && await busy.locator('[data-profile-view-active]').count() === 1, await textOf(busy))
  if (SHOTS) await sleep(350)
  await shot(page, 'profile-blocked')
  await busy.locator('[data-profile-view-active]').click()
  check('……「查看它的进度」：跟着进行中的那一个，写明是已有的任务', await until(async () => (await phase(page)) === 'running')
        && (await inBox(page, '[data-profile-busy]')).includes('已有剖析任务在进行') && (await inBox(page, '[data-profile-progress]')) === '1 / 3 张表'
        && (await inBox(page, '[data-profile-created-by]')) === `由「${OTHER}」发起`)
  stopJob(other)
  await until(async () => (await phase(page)) === 'done')
  await page.locator('[data-profile-again]').click()
  await until(async () => (await phase(page)) === 'setup')
  // 不是 409 的失败：留在设置里写明，能再点开始
  replies.create.push(json({ detail: '服务端处理剖析时出错，请稍后重试' }, 500))
  await page.locator('[data-profile-start]').click()
  check('其余失败：留在设置里写「未能开始剖析」和原因', await until(async () => (await inBox(page, '[data-profile-start-error]')).startsWith('未能开始剖析'))
        && (await phase(page)) === 'setup' && !(await page.locator('[data-profile-start]').isDisabled()))
  // 去数据源：停用、没有表结构、快照被改动时
  replies.create.push(json({ detail: cases[1][1], code: cases[1][4] }, 409))
  await page.locator('[data-profile-start]').click()
  await box(page).locator('[data-profile-go-source]').click()
  await page.waitForURL('**/data/databases', { timeout: 5000 }).catch(() => {})
  check('「前往数据源」回到数据源卡片', new URL(page.url()).pathname === '/data/databases', page.url())
  check('写请求只有建任务、停止和保存剖析设置', writes(sent, /./).every((s) => /profile-jobs(\/[^/]+\/cancel)?$|^PATCH \/datasources\/check-prof-shop$/.test(s.key)),
    JSON.stringify(writes(sent, /./).map((s) => s.key)))
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('剖析 · 和起草排队', async () => {
  const state = freshState()
  // 起草任务在进行：剖析排在它后面
  const draft = newDraftJob(state, S1, ['orders', 'customers', 'visits'])
  Object.assign(draft, { status: 'running', started_at: isoNow() })
  Object.assign(draft.tasks[0], { status: 'ok', finished_at: isoNow() })
  const { page, sent, errors, close } = await open(`/data/catalog/${S1}`, { handlers: handlers(state) })
  await page.locator('[data-catalog-row]').first().waitFor()
  check('页头的起草入口读屏写「起草（进行中）」，进度「起草 1/3」在后台任务条上', await until(async () =>
    (await page.locator('[data-catalog-draft-head="running"]').getAttribute('aria-label')) === '起草（进行中）' && (await stripText(page, 'draft')) === '起草 1/3'),
  await stripText(page, 'draft'))
  await openDialog(page)
  check('同源有起草任务：按钮写「排队剖析」，下面写起草任务结束后自动开始', (await textOf(page.locator('[data-profile-start]'))).includes('排队剖析')
        && (await page.locator('[data-profile-start]').getAttribute('data-profile-start')) === 'queue'
        && (await inBox(page, '[data-profile-queue]')).includes('起草任务结束后自动开始'))
  await page.locator('[data-profile-start]').click()
  await until(async () => (await phase(page)) === 'running')
  const job = state.pjobs.at(-1)
  check('……建出来是排队，写「排在起草任务后面，起草结束后自动开始」、开始时按起草后的目录再定', job.waiting_for === 'draft' && (await inBox(page, '[data-profile-title]')).startsWith('排队中')
        && (await inBox(page, '[data-profile-waiting="draft"]')) === '排在起草任务后面，起草结束后自动开始。要剖析哪些表，开始时按起草后的目录再定')
  await page.locator('[data-profile-hide]').click()
  check('……页头读屏写「数据剖析（排队中）」，任务条按服务端的先后：起草在前，剖析「排队中，起草结束后开始」', await until(async () =>
    (await head(page).getAttribute('data-catalog-profile')) === 'queued' && (await head(page).getAttribute('aria-label')) === '数据剖析（排队中）'
    && JSON.stringify(await stripKinds(page)) === '["draft","profile"]' && (await stripText(page, 'profile')) === '剖析 排队中，起草结束后开始'),
  `${JSON.stringify(await stripKinds(page))} ${await stripText(page, 'profile')}`)
  await shot(page, 'profile-queued')
  // 起草结束、剖析开始
  for (const t of draft.tasks) Object.assign(t, { status: 'ok', finished_at: isoNow() })
  Object.assign(draft, { status: 'succeeded', finished_at: isoNow() })
  settle(state, job, job.tasks[0].table_name, { status: 'running', queries: 2 })
  await goto(page, `/data/catalog/${S1}`)
  check('起草结束后剖析开始：任务条写「剖析 0/5」，页头读屏写「进行中」', await until(async () => (await stripText(page, 'profile')) === `剖析 0/${job.tasks.length}`
        && (await head(page).getAttribute('aria-label')) === '数据剖析（进行中）'), await stripText(page, 'profile'))

  // 反过来：剖析在进行时起草，排在剖析后面
  await page.locator('[data-catalog-draft]').first().click()
  const draftBox = page.locator('[data-draft-dialog]')
  await draftBox.waitFor()
  check('起草设置：写明有剖析任务在进行，起草会排在它后面', (await textOf(draftBox.locator('[data-draft-queue]'))).includes('起草会排在它后面，剖析结束后自动开始'))
  await page.locator('[data-draft-start]').click()
  check('……建出来的起草任务排在剖析后面：写「排在剖析任务后面，剖析结束后自动开始」', await until(async () =>
    (await textOf(draftBox.locator('[data-draft-waiting="profile"]'))) === '排在剖析任务后面，剖析结束后自动开始'))
  await page.locator('[data-draft-hide]').click()
  check('……任务条按服务端的先后：剖析在前，起草「排队中，剖析结束后开始」', await until(async () => JSON.stringify(await stripKinds(page)) === '["profile","draft"]'
        && (await stripText(page, 'draft')) === '起草 排队中，剖析结束后开始'), `${JSON.stringify(await stripKinds(page))} ${await stripText(page, 'draft')}`)
  check('……页头的起草入口读屏写「起草（排队中）」，不写数字', (await page.locator('[data-catalog-draft-head="queued"]').getAttribute('aria-label')) === '起草（排队中）'
        && !/\d/.test(await textOf(page.locator('[data-catalog-draft-head="queued"]'))))
  stopJob(job)
  check('写请求只有建剖析任务、建起草任务', writes(sent, /./).every((s) => /(profile|draft)-jobs$/.test(s.key)), JSON.stringify(writes(sent, /./).map((s) => s.key)))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('剖析 · 表清单的「剖析」一列与筛选', async () => {
  const state = freshState()
  const { page, sent, errors, close } = await open(`/data/catalog/${S1}`, { handlers: handlers(state) })
  await page.locator('[data-catalog-row]').first().waitFor()
  const cell = (n) => rowProfile(page, n)
  const tone = (n) => page.locator(`[data-catalog-row="${n}"] [data-row-profile]`).getAttribute('data-row-profile').catch(() => null)
  check('从没剖析过：「未剖析」，用提醒色', (await cell('orders')) === '未剖析' && (await tone('orders')) === 'due', await cell('orders'))
  check('剖析过、做完了：「3 天前剖析」', (await cell('customers')) === '3 天前剖析' && (await tone('customers')) === 'quiet', await cell('customers'))
  check('上次没做完：「剖析部分完成」，用提醒色', (await cell('visits')) === '剖析部分完成' && (await tone('visits')) === 'due')
  check('剖析之后出现了新的待核实关系：「有新关系待剖析」', (await cell('products')) === '有新关系待剖析' && (await tone('products')) === 'due')
  check('上次失败：「剖析失败」，用错误色', (await cell('refunds')) === '剖析失败' && (await tone('refunds')) === 'bad')
  check('没有可剖析的项：「无可剖析项」；视图、只剩目录的表不写', (await cell('settings_kv')) === '无可剖析项'
        && await page.locator('[data-catalog-row="order_summary"] [data-row-profile]').count() === 0
        && await page.locator('[data-catalog-row="legacy_coupons"] [data-row-profile]').count() === 0)
  const title = await page.locator('[data-catalog-row="visits"] [data-row-profile]').getAttribute('title')
  check('……悬停写上次剖析的时间、结果和为什么需要剖析', title.startsWith('上次剖析（') && title.includes('部分完成') && title.includes('上次剖析没做完'), title)
  const due = page.locator('[data-catalog-profile-due]')
  check('「需要剖析」开关带数量 5，审阅进度的四项筛选照旧', (await textOf(due)).replace(/\s+/g, '') === '需要剖析5'
        && (await page.locator('[data-catalog-filter] [data-filter]').count()) === 4, await textOf(due))
  await due.click()
  const names = await page.locator('[data-catalog-row]').evaluateAll((els) => els.map((e) => e.getAttribute('data-catalog-row')))
  check('打开：只列需要剖析的 5 张', names.join(',') === 'orders,visits,products,refunds,tmp_log', names.join(','))
  check('……写进地址栏（profile=due），按钮标为按下', await until(async () => new URL(page.url()).searchParams.get('profile') === 'due')
        && (await due.getAttribute('aria-pressed')) === 'true')
  await page.reload({ waitUntil: 'networkidle' })
  await page.locator('[data-catalog-row]').first().waitFor()
  check('……刷新之后还在', (await page.locator('[data-catalog-row]').count()) === 5 && (await page.locator('[data-catalog-profile-due]').getAttribute('aria-pressed')) === 'true')
  // 筛出来一键剖析：当前筛选结果就是这 5 张
  await openDialog(page)
  check('……打开剖析：多一项「当前筛选结果中的 5 张表」', await box(page).locator('[data-profile-scope="filtered"]').count() === 1
        && (await inBox(page, '[data-profile-scope="filtered"]')).startsWith('当前筛选结果中的 5 张表'), await inBox(page, '[data-profile-scope="filtered"]'))
  await page.keyboard.press('Escape')
  await page.locator('[data-catalog-profile-due]').click()
  check('关掉：回到全部 9 张，地址里不留', await until(async () => (await page.locator('[data-catalog-row]').count()) === 9 && new URL(page.url()).search === ''))
  await shot(page, 'profile-list')
  // 没开剖析的源：每行不写「剖析」，没有开关
  await goto(page, `/data/catalog/${S2}`)
  await page.locator('[data-catalog-row]').first().waitFor()
  check('没开剖析的源（也没剖析过）：不写「剖析」一格，没有「需要剖析」开关', await page.locator('[data-row-profile]').count() === 0
        && await page.locator('[data-catalog-profile-due]').count() === 0)
  check('只读、不发写请求', writes(sent, /./).length === 0)
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('剖析 · 表详情的「数据剖析」一节', async () => {
  const state = freshState()
  const replies = { create: [] }
  const { page, sent, errors, natives, close } = await open(`/data/catalog/${S1}/customers`, { handlers: handlers(state, replies) })
  const sec = page.locator('[data-catalog-section="profile"]')
  await sec.waitFor()
  const old = state.pjobs[0]
  check('上次剖析：结果、时间、发起人、发了几条查询，给「查看这次剖析」', await until(async () => (await textOf(sec.locator('[data-catalog-profile-last]'))).includes('完成'))
        && (await textOf(sec.locator('[data-catalog-profile-last]'))).includes('上次剖析：') && (await textOf(sec.locator('[data-catalog-profile-last]'))).includes(`${OTHER} 发起`)
        && (await textOf(sec.locator('[data-catalog-profile-last]'))).includes('4 条查询') && await sec.locator(`[data-catalog-profile-job="${old.id}"]`).count() === 1,
  await textOf(sec.locator('[data-catalog-profile-last]')))
  check('……行数估算和来源', (await textOf(sec.locator('[data-profile-rows]'))) === '行数：约 12,000 行（来自数据库统计信息）', await textOf(sec.locator('[data-profile-rows]')))
  check('……发现摘要、码值候选', (await textOf(sec.locator('[data-catalog-profile-findings]'))) === '取到 1 列码值候选（2 个含义待填写）'
        && await sec.locator('[data-profile-finding="codes"][data-column="level"]').count() === 1)
  check('……日期范围', (await textOf(sec.locator('[data-profile-date-range="joined_at"]'))).includes('取值从 2021-03-01 08:00:00 到 2026-10-01 21:30:00'))
  check('……跳过的项（按规则不查的遮罩列）', (await textOf(sec.locator('[data-profile-skip="masked"]'))).includes('列已遮罩'))
  check('……发过的查询（折着）', (await textOf(sec.locator('[data-profile-queries-log] summary'))) === '看发过的查询（3 条）')
  await sec.locator(`[data-catalog-profile-job="${old.id}"]`).click()
  check('「查看这次剖析」：取来那个任务，打开它的结果', await until(async () => (await phase(page)) === 'done')
        && (await inBox(page, '[data-profile-counts]')).startsWith('4 张表') && count(sent, new RegExp(`^GET .*profile-jobs/${old.id}$`)) >= 1)
  await page.locator('[data-profile-close]').click()
  await shot(page, 'profile-detail')

  // 剖析这张表：建只含这张表的任务；这张表在任务里时写「正在剖析这张表」和正在做哪一项
  await sec.locator('[data-catalog-profile-table]').click()
  check('「剖析这张表」：建只含这张表的任务，提示已开始、可以查看进度', await until(async () => JSON.stringify(lastBody(sent, /profile-jobs$/)) === JSON.stringify({ tables: ['customers'] }))
        && await until(async () => (await page.locator('[data-toast]').filter({ hasText: '已开始剖析 customers' }).count()) > 0), await toasts(page))
  const job = state.pjobs.at(-1)
  settle(state, job, 'customers', { status: 'running', done: { codes: 1 }, queries: 2, current: '正在取 customers.joined_at 的最小值和最大值' })
  check('……这一节写「正在剖析这张表」和正在做哪一项，按钮不能再点', await until(async () => (await textOf(sec.locator('[data-catalog-profile-live="running"]'))).includes('正在剖析这张表'))
        && (await textOf(sec.locator('[data-catalog-profile-live]'))).includes('正在取 customers.joined_at 的最小值和最大值')
        && await sec.locator('[data-catalog-profile-table]').isDisabled())
  // 正有剖析任务时剖析别的表（409 正忙）：写明进行中的那个到哪了、这张表在不在里面
  await page.locator('[data-catalog-row="products"] [data-catalog-open-row]').click()
  await until(async () => (await page.locator('[data-catalog-detail="products"][data-version]').count()) === 1)
  await sec.locator('[data-catalog-profile-table]').click()
  const blocked = sec.locator('[data-catalog-profile-blocked="busy"]')
  check('正忙（409）：写服务端原话、进行中的那个到哪了、这张表不在其中', await until(async () => (await blocked.count()) === 1)
        && (await textOf(blocked)).includes('正在进行数据剖析') && (await textOf(sec.locator('[data-catalog-profile-busy-where]'))).includes('这张表不在其中，等它结束后再剖析这张表'),
  await textOf(blocked))
  await blocked.locator('[data-catalog-profile-progress]').click()
  check('……「查看进度」打开进行中的那一个', await until(async () => (await phase(page)) === 'running') && (await inBox(page, '[data-profile-progress]')) === '0 / 1 张表')
  await page.locator('[data-profile-hide]').click()
  settle(state, job, 'customers', { queries: 4, elapsed_ms: 2000, row_estimate: { rows: 12500, method: 'stats', at_least: null } })
  finish(job)
  await page.locator('[data-catalog-row="customers"] [data-catalog-open-row]').click()
  check('剖析完：这一节换成这次的结果（行数更新），发起人是本机', await until(async () => (await textOf(sec.locator('[data-profile-rows]'))).includes('约 12,500 行'))
        && (await textOf(sec.locator('[data-catalog-profile-last]'))).includes(`${ACTOR} 发起`))
  // 再剖析一次，剖析到一半进程被杀（下次启动时才标中断）：这张表没来得及收尾，这一节照服务端写明查完的项没写进目录，
  // 跳过的项里那一项写明结论没写进去
  await sec.locator('[data-catalog-profile-table]').click()
  await until(async () => state.pjobs.at(-1) !== job)
  const killed = state.pjobs.at(-1)
  settle(state, killed, 'customers', { status: 'running', done: { codes: 1 }, queries: 2 })
  await until(async () => (await sec.locator('[data-catalog-profile-live="running"]').count()) === 1)
  stopJob(killed, { status: 'interrupted', error: '服务重启，剖析中断', current: '服务重启，剖析中断，未检查', saved: false })
  check('剖析到一半进程被杀：这一节写已停止和服务端的原因（查完的项没写进目录），跳过的项写明结论没写进去', await until(async () =>
    (await textOf(sec.locator('[data-profile-table-error]'))) === '服务重启，剖析中断：已查完的 1 项没有写进数据目录，剩下 1 项没查')
        && (await textOf(sec.locator('[data-catalog-profile-last]'))).includes('已停止')
        && (await textOf(sec)).includes('查完了但结论没有写进数据目录') && !(await textOf(sec)).includes('照常写进'), await textOf(sec))
  // 没剖析过、视图、没有可剖析项
  await page.locator('[data-catalog-row="orders"] [data-catalog-open-row]').click()
  check('从没剖析过的表：写「还没有剖析过」，给「剖析这张表」', await until(async () => (await textOf(sec.locator('[data-catalog-profile-never]'))) === '这张表还没有剖析过。')
        && !(await sec.locator('[data-catalog-profile-table]').isDisabled()))
  await page.locator('[data-catalog-row="order_summary"] [data-catalog-open-row]').click()
  check('视图：写「视图不剖析」，不给按钮', await until(async () => (await textOf(sec.locator('[data-catalog-profile-never]'))) === '视图不剖析。')
        && await sec.locator('[data-catalog-profile-table]').count() === 0)
  await page.locator('[data-catalog-row="settings_kv"] [data-catalog-open-row]').click()
  check('没有可剖析的项：写明，不给按钮', await until(async () => (await textOf(sec.locator('[data-catalog-profile-never]'))).includes('没有可剖析的项'))
        && await sec.locator('[data-catalog-profile-table]').count() === 0)
  // 没开剖析的源：引到剖析设置
  await goto(page, `/data/catalog/${S2}/orders`)
  await sec.waitFor()
  check('没开剖析的源：写明未开启，「剖析这张表」不能点，给「开启数据剖析」', (await textOf(sec.locator('[data-catalog-profile-disabled]'))).includes('未开启数据剖析')
        && await sec.locator('[data-catalog-profile-table]').isDisabled())
  await sec.locator('[data-catalog-profile-enable]').click()
  check('……点开剖析设置', await until(async () => (await page.locator('[data-profile-settings-dialog]').count()) === 1))
  await page.keyboard.press('Escape')
  check('写请求只有建任务', writes(sent, /./).every((s) => /profile-jobs$/.test(s.key)), JSON.stringify(writes(sent, /./).map((s) => s.key)))
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('剖析 · 窄屏', async () => {
  const state = freshState()
  const { page, errors, close } = await open(`/data/catalog/${S1}`, { handlers: handlers(state), viewport: { width: 390, height: 844 } })
  await page.locator('[data-catalog-row]').first().waitFor()
  const overflow = () => page.evaluate(() => {
    const root = document.querySelector('[data-catalog-page]')
    return root ? root.scrollWidth - root.clientWidth : 99
  })
  check('390 宽：表清单带「剖析」一格，不横向滚动', await page.locator('[data-row-profile]').count() > 0 && await overflow() <= 1, String(await overflow()))
  await openDialog(page)
  await page.locator('[data-profile-start]').click()
  await until(async () => (await phase(page)) === 'running')
  const job = state.pjobs.at(-1)
  settle(state, job, 'orders', { status: 'running', done: { relation: 3, codes: 5, date: 1 }, queries: 23, elapsed_ms: 18000,
    current: '正在核对 orders.coupon_id → coupons.id，这一条要先抽样再到被指向表里核对' })
  await until(async () => (await inBox(page, '[data-profile-task="orders"] [data-profile-task-checks]')) !== '')
  await taskLine(page, 'orders').locator('[data-profile-task-toggle]').click()
  await until(async () => (await box(page).locator('[data-profile-checks]').count()) === 1)
  const d = await page.locator('[role="dialog"]').last().boundingBox()
  const inner = await box(page).evaluate((el) => el.scrollWidth - el.clientWidth)
  check('……进行中、展开一张表：对话框在视口里，正文不横向滚动', !!d && d.x >= 0 && d.x + d.width <= 390 && inner <= 1, `${JSON.stringify(d)} ${inner}`)
  await shot(page, 'profile-narrow')
  // 关掉对话框：后台任务条在窄屏上看得见，「最近查询多数超时」写全文（没有悬停也读得到），不撑宽页面
  job.notice = '最近 6 条查询有 4 条超时，数据库响应变慢了；剖析会继续，可以停止，换在业务不忙的时候再剖析'
  await page.locator('[data-profile-hide]').click()
  await until(async () => (await stripJob(page, 'profile').locator('[data-catalog-job-notice]').count()) === 1)
  const strip = await page.locator('[data-catalog-job-strip]').boundingBox()
  check('……窄屏：后台任务条看得见、提醒写全文，不横向滚动', !!strip && strip.width <= 390 && await stripJob(page, 'profile').isVisible()
        && (await textOf(stripJob(page, 'profile').locator('[data-catalog-job-notice]'))).includes('换在业务不忙的时候再剖析') && await overflow() <= 1,
  `${JSON.stringify(strip)} ${await overflow()}`)
  stopJob(job)
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await browser.close()

const after = await realCounts()
check('真库里的数据源和目录数量没有变化', JSON.stringify(after) === JSON.stringify(before), `${JSON.stringify(before)} → ${JSON.stringify(after)}`)
check('检查用的假数据源没有写进真库', !(await (await fetch(`${API}/datasources`)).json()).some((s) => NAMES.includes(s.name)))

console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 数据剖析全部通过')
process.exit(failed ? 1 : 0)
