#!/usr/bin/env node
// 数据源的变化、表详情的「结构与取值变化」、定时维护、目录页后台任务条的界面检查（docs/design/next-round 03 B2–B4、05 (g)）。
//
// 守的是截图看不出来、点了才知道对不对的那些：
//   - 数据目录页的「变化」视图按要处理 / 提醒 / 说明分组、组内照服务端的先后；能按开着的 / 处理过的、结构 / 取值筛，筛选和打开
//     的那一条记在地址里；视图标签上写几条要处理；
//   - 一条变化写依据（谁测到的、取值变化的观察口径、结构变化比的两版）、前后、影响面（目录项现在的状态、写死的 SQL 会报列
//     不存在、有效记录条件）；动作照服务端给的 actions 画，做不了的写原因；各种动作的请求体（清理先确认、加入码值带含义、
//     误报必须写理由——写之前按钮不能点）；处理完照实写目录版本、建了的任务（点得进进度）；
//   - 表详情的「结构与取值变化」一节：这张表的变化、撤销过的「已列全」、最近几次观察和各自的口径、口径换过的提醒；
//   - 数据源卡片的角标（读屏写全，点了到「变化」视图）；定时维护：缺省关、开启时提示低峰时段，建（POST /schedules，
//     target_kind=source_maintenance）和改（PATCH 带版本）、步骤（PUT /maintenance），最近一次建了哪些任务、哪几步没执行；
//   - 后台任务条：按服务端给的先后列出探查、起草、剖析（两种排法的文字都对），页头只做入口（读屏写状态、不写数字），进度和
//     提醒只在这一条上，「查看」打开对应的对话框；窄屏不撑宽页面。
//
// 写法同 check-catalog-profile：数据源列表、目录、变化、观察、后台任务、定时都用 page.route 伪造（虚构的网店库，表名 orders、
// customers 都是通用的假名字），没配的 GET 放行到服务端（外壳要它），没配的写请求一律拦成 503 并记账；写请求在浏览器层拦下，
// 断言请求体。最后核对真库里的数据源数量没有变化。
//
// 跑之前前后端都得起着，默认连 5273 / 8000；对别的实例：AGENTLAB_WEB=… AGENTLAB_API=…/api node scripts/check-catalog-changes.mjs
// 只跑其中几节：CHECK_ONLY=分组,定时 node scripts/check-catalog-changes.mjs（按节名包含匹配）。CHECK_SHOTS=<目录> 存截图。
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const API = process.env.AGENTLAB_API ?? 'http://localhost:8000/api'
const SHOTS = process.env.CHECK_SHOTS ?? ''
const CHROME = process.env.CHROME_PATH ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const ACTOR = '检查脚本'

let failed = 0
const check = (name, cond, detail = '') => {
  const d = String(detail ?? '').replace(/\s+/g, ' ').slice(0, 200)
  console.log(`  ${cond ? '✓' : '✗'} ${name}${d ? ` — ${d}` : ''}`)
  if (!cond) failed++
}

try {
  const r = await fetch(`${API}/datasources/kinds`)
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
} catch (e) {
  console.error(`✗ 连不上服务端（${API}）——先起沙箱后端\n  ${e.message}`)
  process.exit(1)
}
const realSources = async () => (await (await fetch(`${API}/datasources`)).json()).length
const before = await realSources()

const browser = await chromium.launch({ executablePath: CHROME })
const opened = new Set()
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

/**
 * 开一页：handlers 按「METHOD 路径正则」匹配（路径去掉 /api，不含查询串），没配的 GET 放行到服务端，没配的写请求一律拦成 503。
 * sent 记下每个配上的请求（含查询串和请求体）。原生对话框都算失败
 */
async function open(path, { handlers = [], viewport = { width: 1440, height: 900 } } = {}) {
  const ctx = await browser.newContext({ viewport, timezoneId: 'Asia/Shanghai' })
  opened.add(ctx)
  ctx.setDefaultTimeout(6000)
  ctx.setDefaultNavigationTimeout(30000)
  await ctx.addInitScript((actor) => {
    try { localStorage.setItem('agentlab_actor', actor); localStorage.removeItem('agentlab.health') } catch { /* noop */ }
  }, ACTOR)
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
        sent.push({ key, body, query: Object.fromEntries(url.searchParams) })
        return handle(route, { body, url, key })
      }
    }
    if (r.method() === 'GET') return route.continue()
    sent.push({ key, body: null, blocked: true })
    return route.fulfill({ status: 503, body: '' })
  })
  await page.goto(`${WEB}${path}`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(300)
  return { page, sent, errors, natives }
}
const goto = async (page, path) => { await page.goto(`${WEB}${path}`, { waitUntil: 'networkidle' }); await page.waitForTimeout(250) }
const json = (data, status = 200) => (route) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) })
const until = async (fn, ms = 6000, step = 120) => {
  const end = Date.now() + ms
  let v = await fn().catch(() => false)
  while (!v && Date.now() < end) { await new Promise((r) => setTimeout(r, step)); v = await fn().catch(() => false) }
  return v
}
const textOf = async (loc) => (await loc.first().innerText({ timeout: 800 }).catch(() => '')).replace(/\s+/g, ' ').trim()
const attrs = (loc, name) => loc.evaluateAll((els, n) => els.map((e) => e.getAttribute(n)), name)
const shot = async (page, name) => { if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png` }) }
const writes = (sent, re) => sent.filter((s) => re.test(s.key) && !s.key.startsWith('GET ') && !s.blocked)
const blocked = (sent) => sent.filter((s) => s.blocked)
const clone = (x) => JSON.parse(JSON.stringify(x))
const isoNow = () => new Date().toISOString()
const ago = (days) => new Date(Date.now() - days * 86_400_000).toISOString()
const overflow = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)

// ---------------------------------------------------------------------------
// 夹具：虚构的网店库（剖析没开）和一个导入表格的源。名字、数字全是假的
// ---------------------------------------------------------------------------
const S1 = 'check-chg-shop'
const S2 = 'check-chg-upload'
const NAME1 = 'zzchgshop'
const NAME2 = 'zzchgupload'
const NOT_PROFILED = `数据源「${NAME1}」未开启数据剖析。剖析会对业务库发查询，请先在数据源设置中开启数据剖析`
const blank = {
  kind: 'sqlite', host: null, port: null, username: null, readonly: true, description: '', enabled: true,
  password_masked: '', has_password: false, cached_schema: '', schema_error: '', available_schemas: [],
  last_checked_at: null, last_check_ok: null, last_latency_ms: null, last_error: null, current_snapshot: null,
}
const sources = () => [
  { ...blank, id: S1, name: NAME1, origin: 'manual', database: `/tmp/check/${NAME1}.db`, table_count: 2, tools: [`db_query__${NAME1}`],
    schema_synced_at: ago(1), options: { catalog_profile: { enabled: false } } },
  { ...blank, id: S2, name: NAME2, origin: 'upload', database: `/tmp/check/uploads/${S2}.db`, table_count: 1, tools: [`db_query__${NAME2}`],
    schema_synced_at: ago(1), import_mode: 'simple', current_recipe: null, open_staging: null, options: {},
    current_snapshot: { id: 'snap-check-chg', created_at: ago(2), file_name: '客流日报.xlsx', raw_state: 'kept' } },
]
const item = (value, source = 'human', status = 'confirmed') => ({ value, source, status, updated_at: ago(3) })
const col = (name, type = 'INTEGER') => ({ name, type, pk: name === 'id', not_null: name === 'id', comment: null })
const TABLES = {
  orders: { usage: 30, columns: [col('id'), col('channel', 'VARCHAR(8)'), col('amount', 'NUMERIC(12, 2)'), col('created_at', 'TEXT')],
    notes: { label: item('订单'), columns: { channel: { codes: item({ web: '线上', shop: '门店' }) } } }, version: 5 },
  customers: { usage: 12, columns: [col('id'), col('level')], notes: null, version: 0 },
}
const counts0 = { proposed: 0, verified: 0, confirmed: 0, rejected: 0 }
const rowOf = (name) => {
  const t = TABLES[name]
  return { table_name: name, qualified: name, is_view: false, in_schema: true, label: t.notes?.label?.value ?? null, label_status: null, kind: null,
    counts: t.notes ? { ...counts0, confirmed: 2 } : counts0, relations: 0, usage: t.usage, version: t.version, updated_at: null, updated_by: null,
    last_draft: null, last_profile: null, profile_due: false, profile_due_reason: null, profile_checks: 0 }
}
const detailOf = (name) => {
  const t = TABLES[name]
  return { table_name: name, in_schema: true, absent_reason: null, notes: t.notes ?? {}, version: t.version, updated_at: null, updated_by: null,
    structure: { qualified: name, is_view: false, comment: null, columns: t.columns, primary_key: ['id'], foreign_keys: [], unique: [] },
    system_notes: false, usage: t.usage, last_profile: null }
}

/** 一条变化（agentlab-change/1 加上给界面的几项），没写的取缺省 */
const change = (o) => ({
  schema: 'agentlab-change/1', source_id: S1, source_name: NAME1, column: null, detected_at: ago(1), last_seen_at: ago(1), seen_count: 1,
  detected_by: { job_kind: 'introspect', job_id: 'check-ijob-1', task_seq: 1, trigger: 'schedule', actor: '定时任务「zzchgshop 定时维护」' },
  basis: null, basis_text: null, before: null, after: null, affects: { catalog: [], templates: [], rules_touched: false },
  struct_version_before: null, struct_version_after: null, status: 'open', resolution: null, job: null, ...o,
})
const act = (action, reason = null) => ({ action, ok: !reason, reason })
const COMMON = [act('acknowledge'), act('dismiss')]
function freshChanges() {
  return [
    change({ seq: 11, id: 'chg-removed', table: 'orders', column: 'status', kind: 'column_removed', category: 'struct', severity: 'action',
      summary: 'orders.status：删除了列', before: { type: 'VARCHAR(10)', nullable: true }, struct_version_before: 2, struct_version_after: 3,
      affects: {
        catalog: [
          { table: 'orders', path: 'columns.status.codes', label: '列 status 的码值', status: 'confirmed', source: 'human', needs_action: true, now: { status: 'confirmed', source: 'human' } },
          { table: 'orders', path: 'valid_filter', label: '表的有效记录条件', status: 'confirmed', source: 'human', needs_action: true, now: { status: 'confirmed', source: 'human' } },
        ],
        templates: [{ workflow_id: 'wf-check-weekly', name: '门店周报', version: 3, level: 'published', impact: 'direct',
          nodes: [{ node_id: 'q_orders', label: '取订单', type: 'tool', impact: 'direct', breaks: true, unknown_columns: ['status'] }] }],
        rules_touched: true,
      },
      actions: [act('clean_catalog'), act('redraft'), act('reprofile', NOT_PROFILED), ...COMMON] }),
    change({ seq: 14, id: 'chg-codes', table: 'orders', column: 'channel', kind: 'codes_new', category: 'value', severity: 'action',
      summary: 'orders.channel：出现了新码值（app）', basis: { method: 'first_rows', key: 'first_rows:100000', query_refs: [3] },
      basis_text: '按存储顺序取前 100000 行', before: { known: ['shop', 'web'], complete: true },
      after: { codes: ['app'], shares: { app: 0.4 }, complete_revoked: true, catalog_version: 5 },
      detected_by: { job_kind: 'profile', job_id: 'check-pjob-9', task_seq: 1, trigger: 'schedule', actor: null },
      actions: [act('add_codes'), act('reprofile', NOT_PROFILED), ...COMMON] }),
    change({ seq: 12, id: 'chg-type', table: 'orders', column: 'amount', kind: 'column_type_changed', category: 'struct', severity: 'warning',
      summary: 'orders.amount：列的类型变了（NUMERIC(10, 2) → NUMERIC(12, 2)）', before: 'NUMERIC(10, 2)', after: 'NUMERIC(12, 2)',
      struct_version_before: 2, struct_version_after: 3, actions: [act('redraft'), act('reprofile', NOT_PROFILED), ...COMMON] }),
    change({ seq: 15, id: 'chg-rows', table: 'customers', kind: 'rows_jump', category: 'value', severity: 'info', summary: 'customers：行数大幅上涨',
      basis: { method: 'count', key: 'count', query_refs: [] }, basis_text: '逐行数出', before: { rows: 100 }, after: { rows: 400, estimated: false },
      detected_by: { job_kind: 'profile', job_id: 'check-pjob-9', task_seq: 2, trigger: 'manual', actor: '审核甲' },
      actions: [act('reprofile', NOT_PROFILED), ...COMMON] }),
    change({ seq: 8, id: 'chg-stalled', table: 'orders', column: 'created_at', kind: 'date_stalled', category: 'value', severity: 'warning',
      summary: 'orders.created_at：最新业务日期没有前进', basis_text: '整表', status: 'dismissed',
      after: { max: '2026-09-30', age_days: 9, threshold_days: 3 }, before: { max: '2026-09-30', median_days: 1 },
      resolution: { by: '审核甲', at: ago(2), action: 'dismiss', note: '国庆假期停业' }, actions: [] }),
  ]
}
const PENDING = new Set(['open', 'acknowledged'])
const countsOf = (changes) => {
  const by = { action: 0, warning: 0, info: 0 }
  let open = 0
  for (const c of changes) if (PENDING.has(c.status)) { by[c.severity]++; if (c.status === 'open') open++ }
  return { pending: by.action + by.warning + by.info, open, by_severity: by }
}
const RANK = { action: 3, warning: 2, info: 1 }

/** 观察：行数（两次同口径）、码值（口径换过：块采样 → 按存储顺序前 N 行）、按日行数（中间断了两天） */
const OBSERVATIONS = { table: 'orders', series: [
  { kind: 'rows', column: '', basis_changed: false, points: [
    { at: ago(0), basis_key: 'count', basis_text: '逐行数出', data: { rows: 1200, at_least: false, method: 'count' } },
    { at: ago(1), basis_key: 'count', basis_text: '逐行数出', data: { rows: 1180, at_least: false, method: 'count' } },
  ] },
  { kind: 'codes_observe', column: 'channel', basis_changed: true, points: [
    { at: ago(0), basis_key: 'first_rows:100000', basis_text: '按存储顺序取前 100000 行',
      data: { values: [['web', 600, 0.5], ['app', 480, 0.4], ['shop', 120, 0.1]], rows: 1200, truncated: false } },
    { at: ago(3), basis_key: 'sample:1', basis_text: '块采样约 1%', data: { values: [['web', 30, 0.75], ['shop', 10, 0.25]], rows: 40, truncated: false } },
  ] },
  { kind: 'freshness', column: 'created_at', basis_changed: false, points: [
    { at: ago(0), basis_key: 'full', basis_text: '整表', data: { max: '2026-10-15' } },
  ] },
  { kind: 'daily_volume', column: 'created_at', basis_changed: false, points: [
    { at: ago(0), basis_key: 'window:created_at:35', basis_text: '业务日期 created_at 最近 35 天',
      data: { since: '2026-10-11', until: '2026-10-15', days: [['2026-10-11', 40], ['2026-10-12', 0], ['2026-10-13', 0], ['2026-10-14', 42], ['2026-10-15', 41]],
        total: 123, days_with_data: 3 } },
  ] },
] }

/** 后台任务：探查在做、起草等探查、剖析等起草（服务端的先后），或者反过来 */
const JOBS_A = [
  { kind: 'introspect', id: 'check-ijob-2', status: 'running', total: 20, done: 3, waiting_for: null, position: 0 },
  { kind: 'draft', id: 'check-djob-1', status: 'queued', total: 5, done: 0, waiting_for: 'introspect', position: 1 },
  { kind: 'profile', id: 'check-pjob-1', status: 'queued', total: 8, done: 0, waiting_for: 'draft', position: 2 },
]
const JOBS_B = [
  { kind: 'profile', id: 'check-pjob-1', status: 'running', total: 8, done: 2, waiting_for: null, position: 0 },
  { kind: 'draft', id: 'check-djob-1', status: 'queued', total: 5, done: 0, waiting_for: 'profile', position: 1 },
]
const PROFILE_NOTICE = '最近 6 条查询里有 5 条超时：业务库可能正忙，或者表太大。可以先停止，到剖析设置里调大单条查询时限'
const draftJob = (status, done = 0) => ({ id: 'check-djob-1', source_id: S1, status, use_model: false, model: null, guidance: null, note: null,
  created_at: ago(0), started_at: status === 'running' ? ago(0) : null, finished_at: null, created_by: ACTOR,
  usage: { calls: 0, input_tokens: 0, output_tokens: 0 }, error: null, notice: null, waiting_for: status === 'queued' ? 'profile' : null,
  total: 5, done, counts: { pending: 5 - done, running: 0, ok: done, partial: 0, failed: 0, skipped: 0, cancelled: 0 }, added: 0, updated: 0, removed: 0,
  tasks: ['orders', 'customers', 'a1', 'a2', 'a3'].map((n, i) => ({ table_name: n, status: i < done ? 'ok' : 'pending', columns: 3, parts_total: null,
    parts_done: 0, added: 0, updated: 0, removed: 0, version: 0, model_items: null, error: null, model_error: null, started_at: null, finished_at: null })) })
const profileJob = (status, done = 0, notice = null) => ({ id: 'check-pjob-1', source_id: S1, status, scope: 'needed',
  settings: { enabled: true, table_max_queries: 40, table_max_s: 60, query_timeout_s: 10, sample_size: 2000, max_scan_rows: 100000 },
  created_at: ago(0), started_at: status === 'running' ? ago(0) : null, finished_at: null, created_by: ACTOR, retry_of: null,
  waiting_for: status === 'queued' ? 'draft' : null, stop_reason: null, notice, error: null, total: 8, done,
  counts: { pending: 8 - done, running: 0, ok: done, partial: 0, failed: 0, skipped: 0, cancelled: 0 }, queries: 0, timeouts: 0, errors: 0,
  added: 0, updated: 0, removed: 0, summary: { relations_checked: 0, relations_verified: 0, codes: 0, codes_pending: 0, business_dates: 0 },
  tasks: Array.from({ length: 8 }, (_, i) => ({ seq: i + 1, table_name: `t${i}`, status: i < done ? 'ok' : 'pending', checks_total: 1, checks_done: i < done ? 1 : 0,
    checks_by_kind: { relation: [0, 0], codes: [0, 1], date: [0, 0] }, current: null, queries: 0, elapsed_ms: 0, row_estimate: null, summary: { relations_checked: 0,
      relations_verified: 0, codes: 0, codes_pending: 0, business_dates: 0 }, skipped_count: 0, unfinished: 0, added: 0, updated: 0, removed: 0, version: 0,
    error: null, started_at: null, finished_at: null })) })

function freshState() {
  return { sources: sources(), changes: freshChanges(), jobs: [], activeDraft: null, activeProfile: null, draftJobs: {}, profileJobs: {},
    maintenance: { steps: ['introspect', 'profile_needed', 'observe'], profile_enabled: false, maintained: true, scheduler_on: true, schedules: [] },
    schedSeq: 0 }
}
const P = 'check-chg-[a-z]+'
const parts = (key) => key.split(' ')[1].split('/')
const view = (c) => clone(c)

function handlers(state) {
  const findChange = (key) => state.changes.find((c) => c.id === parts(key)[4])
  return [
    [/^GET \/datasources$/, (route) => json(state.sources)(route)],
    [/^GET \/datasources\/upkeep$/, (route) => json({ items: state.sources.map((s) => ({ source_id: s.id,
      changes: s.id === S1 ? countsOf(state.changes) : countsOf([]),
      maintenance: s.id === S1 ? state.maintenance.schedules.map((x) => ({ id: x.id, enabled: x.enabled, cron: x.cron, timezone: x.timezone,
        description_text: x.description_text, next_run_at: x.next_run_at })) : [] })) })(route)],
    [new RegExp(`^GET /datasources/${P}/catalog$`), (route) => json({
      tables: Object.keys(TABLES).map(rowOf), system_notes: false, schema_note: null, schema_truncated: false, schema_total: 2, guidance: null, scope: null,
      active_job: state.activeDraft ? (({ id, status, total, done, waiting_for }) => ({ id, status, total, done, waiting_for }))(state.activeDraft) : null,
      active_profile_job: state.activeProfile ? (({ id, status, total, done, waiting_for }) => ({ id, status, total, done, waiting_for }))(state.activeProfile) : null,
    })(route)],
    [new RegExp(`^GET /datasources/${P}/catalog/draft-jobs/[^/]+$`), (route, { key }) => {
      const job = state.draftJobs[parts(key).at(-1)]
      return job ? json({ job })(route) : json({ detail: '起草任务不存在' }, 404)(route)
    }],
    [new RegExp(`^GET /datasources/${P}/catalog/profile-jobs/[^/]+$`), (route, { key }) => {
      const job = state.profileJobs[parts(key).at(-1)]
      return job ? json({ job })(route) : json({ detail: '剖析任务不存在' }, 404)(route)
    }],
    [new RegExp(`^GET /datasources/${P}/catalog/[^/]+/impact$`), (route, { key }) => json({ table: parts(key)[4], templates: [] })(route)],
    [new RegExp(`^GET /datasources/${P}/catalog/[^/]+$`), (route, { key }) => {
      const name = parts(key)[4]
      return TABLES[name] ? json(detailOf(name))(route) : json({ detail: `数据源中没有表 ${name}` }, 404)(route)
    }],
    [new RegExp(`^GET /datasources/${P}/background-jobs$`), (route) => json({ items: state.jobs })(route)],
    [new RegExp(`^GET /datasources/${P}/observations$`), (route, { url }) =>
      json(url.searchParams.get('table') === 'orders' ? OBSERVATIONS : { table: url.searchParams.get('table'), series: [] })(route)],
    [new RegExp(`^GET /datasources/${P}/changes$`), (route, { url }) => {
      const status = url.searchParams.get('status') ?? 'pending'
      const cat = url.searchParams.get('category')
      const table = url.searchParams.get('table')
      const limit = Number(url.searchParams.get('limit') ?? 100)
      let rows = state.changes.filter((c) => (status === 'handled' ? !PENDING.has(c.status) : PENDING.has(c.status)))
      if (cat) rows = rows.filter((c) => c.category === cat)
      if (table) rows = rows.filter((c) => c.table === table)
      rows = status === 'handled' ? rows.sort((a, b) => b.seq - a.seq) : rows.sort((a, b) => RANK[b.severity] - RANK[a.severity] || b.seq - a.seq)
      return json({ items: rows.slice(0, limit).map((c) => view(c)), total: rows.length, counts: countsOf(state.changes) })(route)
    }],
    [new RegExp(`^GET /datasources/${P}/changes/[^/]+$`), (route, { key }) => {
      const c = findChange(key)
      return c ? json({ change: view(c) })(route) : json({ detail: '这条变化不存在，可能已随数据源删除' }, 404)(route)
    }],
    [new RegExp(`^POST /datasources/${P}/changes/[^/]+/resolve$`), (route, { key, body }) => {
      const c = findChange(key)
      if (!c) return json({ detail: '这条变化不存在' }, 404)(route)
      if (!PENDING.has(c.status)) return json({ detail: '这条变化已经处理过了' }, 409)(route)
      if (body?.action === 'dismiss' && !String(body.note ?? '').trim()) return json({ detail: '标为误报要写明理由' }, 422)(route)
      const res = { by: ACTOR, at: isoNow(), action: body.action, note: body.note ?? null }
      const out = {}
      if (body.action === 'clean_catalog' || body.action === 'add_codes') {
        TABLES[c.table].version += 1
        res.catalog_version = TABLES[c.table].version
        out.catalog_version = res.catalog_version
        c.affects = { ...c.affects, catalog: c.affects.catalog.map((r) => ({ ...r, now: null })) }
      }
      if (body.action === 'redraft' || body.action === 'reprofile') {
        const kind = body.action === 'redraft' ? 'draft' : 'profile'
        res.job_id = kind === 'draft' ? 'check-djob-1' : 'check-pjob-1'
        c.job = { kind, id: res.job_id }
        out.job = c.job
        if (kind === 'draft') { state.draftJobs['check-djob-1'] = draftJob('running', 1); state.activeDraft = state.draftJobs['check-djob-1'] }
      }
      c.status = body.action === 'acknowledge' ? 'acknowledged' : body.action === 'dismiss' ? 'dismissed' : 'resolved'
      c.resolution = res
      c.actions = c.status === 'acknowledged' ? c.actions.filter((a) => a.action !== 'acknowledge') : []
      return json({ ...out, change: view(c) })(route)
    }],
    // ---- 定时维护
    [new RegExp(`^GET /datasources/${P}/maintenance$`), (route, { key }) => json(parts(key)[2] === S2
      ? { ...state.maintenance, maintained: false, schedules: [] } : state.maintenance)(route)],
    [new RegExp(`^PUT /datasources/${P}/maintenance$`), (route, { body }) => {
      state.maintenance.steps = ['introspect', 'profile_needed', 'observe'].filter((s) => body.steps.includes(s))
      return json(state.maintenance)(route)
    }],
    [/^POST \/schedules\/preview$/, (route, { body }) => {
      if (!/^\d+ \d+ \* \* \*$/.test(body.cron) && body.cron !== '0 3 * * *') return json({ detail: '检查脚本只认每天的频率' }, 422)(route)
      const [m, h] = body.cron.split(' ')
      return json({ description_text: `每天 ${h.padStart(2, '0')}:${m.padStart(2, '0')}（${body.timezone}）`, next: ['2026-10-17T19:00:00Z', '2026-10-18T19:00:00Z'], inputs: {} })(route)
    }],
    [/^POST \/schedules$/, (route, { body }) => {
      const [m, h] = body.cron.split(' ')
      const row = { id: `check-sched-${++state.schedSeq}`, name: body.name, target_kind: body.target_kind, target_id: body.target_id, target_label: '数据源维护',
        target_name: NAME1, target_exists: true, workflow_id: '', workflow_name: NAME1, cron: body.cron, timezone: body.timezone, run_class: 'exploratory',
        input_template: {}, enabled: body.enabled, overlap: 'skip', misfire_grace_s: body.misfire_grace_s, webhook: null,
        next_run_at: '2026-10-17T19:00:00Z', last_fire: null, description_text: `每天 ${h.padStart(2, '0')}:${m.padStart(2, '0')}（${body.timezone}）`,
        created_by: ACTOR, created_at: isoNow(), updated_at: isoNow(), version: 1 }
      state.maintenance.schedules.push(row)
      return json(row, 201)(route)
    }],
    [/^PATCH \/schedules\/[^/]+$/, (route, { key, body }) => {
      const row = state.maintenance.schedules.find((s) => s.id === parts(key)[2])
      if (!row) return json({ detail: '定时任务不存在' }, 404)(route)
      if (body.version !== row.version) return json({ detail: '这个定时任务刚被别人改过，请刷新后再改' }, 409)(route)
      Object.assign(row, Object.fromEntries(Object.entries(body).filter(([k]) => k !== 'version')), { version: row.version + 1 })
      if (body.cron) { const [m, h] = body.cron.split(' '); row.description_text = `每天 ${h.padStart(2, '0')}:${m.padStart(2, '0')}（${row.timezone}）` }
      if (body.enabled === false) row.next_run_at = null
      return json(row)(route)
    }],
  ]
}

// ---------------------------------------------------------------------------
// 页面上的定位
// ---------------------------------------------------------------------------
const queue = (page) => page.locator('[data-changes-queue]')
const detail = (page) => page.locator('[data-change-detail]')
const groupsOf = (page) => attrs(page.locator('[data-change-group]'), 'data-change-group')
const itemsOf = (page) => attrs(page.locator('[data-change-item]'), 'data-change-item')
const lastQuery = (sent, re) => sent.filter((s) => re.test(s.key)).at(-1)?.query ?? null
const openChange = async (page, id) => {
  await page.locator(`[data-change-item="${id}"] [data-change-open]`).click()
  await until(async () => (await detail(page).getAttribute('data-change-detail')) === id)
}

try {
  const probe = await open('/data')
  const overlay = await probe.page.locator('vite-error-overlay').count()
  for (const c of opened) await c.close().catch(() => {})
  opened.clear()
  if (overlay) {
    console.error('✗ 页面上是 vite 的报错层——有文件正在改，等一会儿再跑')
    process.exit(1)
  }
} catch (e) {
  console.error(`✗ 打不开前端（${WEB}）——先起沙箱前端\n  ${e.message}`)
  process.exit(1)
}

// ---------------------------------------------------------------------------

await section('变化视图：分组、筛选、地址、标签上的计数', async () => {
  const state = freshState()
  const { page, sent, errors, natives } = await open(`/data/catalog/${S1}?view=changes`, { handlers: handlers(state) })
  await queue(page).waitFor()
  await until(async () => (await page.locator('[data-change-item]').count()) === 4)
  check('三个视图：表清单、审阅队列、变化', JSON.stringify(await attrs(page.locator('[data-catalog-view-tab]'), 'data-catalog-view-tab'))
        === '["list","review","changes"]')
  check('「变化」标签选中，带着 2 条要处理（读屏写全）', (await page.locator('[data-catalog-view-tab="changes"]').getAttribute('aria-selected')) === 'true'
        && (await page.locator('[data-catalog-changes-count]').getAttribute('data-catalog-changes-count')) === '2'
        && (await textOf(page.locator('[data-catalog-view-tab="changes"]'))).includes('2 条要处理'),
  await textOf(page.locator('[data-catalog-view-tab="changes"]')))
  check('按严重程度分组：要处理、提醒、说明', JSON.stringify(await groupsOf(page)) === '["action","warning","info"]', JSON.stringify(await groupsOf(page)))
  check('组内照服务端给的先后', JSON.stringify(await itemsOf(page)) === '["chg-codes","chg-removed","chg-type","chg-rows"]', JSON.stringify(await itemsOf(page)))
  check('组名写几条', (await textOf(page.locator('[data-change-group="action"] h3'))) === '要处理 2 条', await textOf(page.locator('[data-change-group="action"] h3')))
  const row = page.locator('[data-change-item="chg-removed"]')
  check('一条：服务端的一句说法、类别、测到的时间', (await textOf(row)).includes('orders.status：删除了列') && (await textOf(row)).includes('结构变化')
        && (await textOf(row)).includes('测到于'), await textOf(row))
  check('……读屏先念严重程度', (await row.locator('.sr-only').first().innerText()).startsWith('要处理'))
  check('开着的不带 status=handled，不按类别筛', lastQuery(sent, /\/changes$/)?.status === 'pending' && !lastQuery(sent, /\/changes$/)?.category)

  await page.locator('[data-changes-cat="struct"]').click()
  check('筛「结构变化」：请求带 category=struct，只剩两条结构变化', await until(async () => JSON.stringify(await itemsOf(page)) === '["chg-removed","chg-type"]')
        && lastQuery(sent, /\/changes$/)?.category === 'struct', JSON.stringify(await itemsOf(page)))
  check('……筛选记进地址', new URL(page.url()).searchParams.get('ccat') === 'struct')
  await page.locator('[data-changes-cat="value"]').click()
  check('筛「取值变化」：只剩两条取值变化', await until(async () => JSON.stringify(await itemsOf(page)) === '["chg-codes","chg-rows"]'))
  await page.locator('[data-changes-cat="all"]').click()
  await page.locator('[data-changes-status="handled"]').click()
  check('「处理过的」：请求带 status=handled，列出标为误报的那一条', await until(async () => JSON.stringify(await itemsOf(page)) === '["chg-stalled"]')
        && lastQuery(sent, /\/changes$/)?.status === 'handled' && new URL(page.url()).searchParams.get('cstatus') === 'handled')
  check('……写着它的状态', (await textOf(page.locator('[data-change-item="chg-stalled"] [data-change-state]'))) === '标为误报')
  await page.locator('[data-change-item="chg-stalled"] [data-change-open]').click()
  await until(async () => (await detail(page).getAttribute('data-change-detail')) === 'chg-stalled')
  check('点开处理过的：写处理结果（谁、动作、理由），不给动作', (await textOf(page.locator('[data-change-resolution]'))).includes('已标为误报 · 审核甲')
        && (await textOf(page.locator('[data-change-resolved-note]'))) === '理由：国庆假期停业' && await page.locator('[data-change-action]').count() === 0)
  await page.reload({ waitUntil: 'networkidle' })
  check('刷新：筛选和打开的那一条都还在', await until(async () => (await detail(page).getAttribute('data-change-detail')) === 'chg-stalled')
        && (await page.locator('[data-changes-status="handled"]').getAttribute('aria-checked')) === 'true')
  await page.locator('[data-catalog-view-tab="list"]').click()
  check('切回表清单：变化的参数都去掉', await until(async () => !new URL(page.url()).searchParams.has('cstatus') && !new URL(page.url()).searchParams.has('change')
        && !new URL(page.url()).searchParams.has('view')))
  check('没有拦下的写请求', blocked(sent).length === 0, blocked(sent).map((s) => s.key).join('、'))
  check('没有原生对话框、没有运行时报错', natives.length === 0 && errors.length === 0, [...natives, ...errors].join(' | '))
})

await section('变化：依据、前后、影响面、动作照服务端给的画', async () => {
  const state = freshState()
  const { page, errors } = await open(`/data/catalog/${S1}?view=changes`, { handlers: handlers(state) })
  await until(async () => (await page.locator('[data-change-item]').count()) === 4)
  await openChange(page, 'chg-removed')
  const d = detail(page)
  check('依据：定时维护的探查结构测到，比对的是结构第 2 版和第 3 版', (await textOf(d.locator('[data-change-detected]'))).startsWith('定时维护的探查结构')
        && (await textOf(d.locator('[data-change-basis="struct"]'))) === '比对的是这张表的结构第 2 版和第 3 版', await textOf(d.locator('[data-change-section="basis"]')))
  check('前后：删掉的列之前是什么类型', (await textOf(d.locator('[data-change-facts]'))).includes('VARCHAR(10)'))
  check('影响面：说到这一列的两项目录（测到时和现在的状态），人工确认过的标出来', await d.locator('[data-change-ref]').count() === 2
        && (await textOf(d.locator('[data-change-ref="orders::columns.status.codes"]'))).includes('现在：已确认')
        && await d.locator('[data-change-ref-needs]').count() === 2)
  check('……写死这张表的已发布模板：「这条 SQL 现在会报列不存在（status）」', (await textOf(d.locator('[data-change-node="q_orders"]'))).includes('这条 SQL 现在会报列不存在（status）')
        && (await d.locator('[data-change-node="q_orders"]').getAttribute('data-change-breaks')) === '1')
  check('……模板链到编排页那个节点', (await d.locator('[data-change-template] a').first().getAttribute('href')) === '/studio/wf-check-weekly?focus=q_orders')
  check('……有效记录条件用到了这一列', (await textOf(d.locator('[data-change-rules]'))).includes('有效记录条件用到了这一列'))
  check('动作照服务端给的：清理目录项、重新起草、重新剖析、知道了、误报', JSON.stringify(await attrs(d.locator('[data-change-action]'), 'data-change-action'))
        === '["clean_catalog","redraft","reprofile","acknowledge","dismiss"]')
  check('……做不了的不能点，写原因（剖析没开）', await d.locator('[data-change-action="reprofile"]').isDisabled()
        && (await textOf(d.locator('[data-change-why="reprofile"]'))).includes('未开启数据剖析'), await textOf(d.locator('[data-change-why="reprofile"]')))
  await openChange(page, 'chg-codes')
  check('取值变化的依据写观察口径，只和同口径比', (await textOf(d.locator('[data-change-basis="value"]'))).startsWith('观察口径：按存储顺序取前 100000 行')
        && (await textOf(d.locator('[data-change-basis="value"]'))).includes('只和口径相同的历史观察比'))
  check('前后：新码值和占比、撤销了「已列全」（目录第 5 版）', (await textOf(d.locator('[data-change-facts]'))).includes('app（占 40%）')
        && (await textOf(d.locator('[data-change-revoked]'))).includes('撤销了码值项的「已列全」（目录第 5 版）'), await textOf(d.locator('[data-change-facts]')))
  check('……影响面空的照实说', (await textOf(d.locator('[data-change-impact-none]'))).length > 0)
  await openChange(page, 'chg-type')
  check('类型变了：前后两种写法', (await textOf(d.locator('[data-change-facts]'))).includes('NUMERIC(10, 2) → NUMERIC(12, 2)'))
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
})

await section('变化：各种处理动作的请求体、误报必须写理由、处理完照实写结果', async () => {
  const state = freshState()
  const { page, sent, errors, natives } = await open(`/data/catalog/${S1}?view=changes`, { handlers: handlers(state) })
  await until(async () => (await page.locator('[data-change-item]').count()) === 4)
  const d = detail(page)
  // 误报：理由没写之前按钮不能点
  await openChange(page, 'chg-type')
  await d.locator('[data-change-action="dismiss"]').click()
  check('误报：先写理由，没写之前「标为误报」不能点', await d.locator('[data-change-note]').isVisible() && await d.locator('[data-change-note-submit]').isDisabled())
  await d.locator('[data-change-note]').fill('   ')
  check('……只填空白也不能点', await d.locator('[data-change-note-submit]').isDisabled())
  await d.locator('[data-change-note]').fill('测试库里改的类型')
  await d.locator('[data-change-note-submit]').click()
  await until(async () => (await d.getAttribute('data-change-status')) === 'dismissed')
  check('……请求体是 {action: dismiss, note}', JSON.stringify(writes(sent, /chg-type\/resolve$/).at(-1)?.body) === '{"action":"dismiss","note":"测试库里改的类型"}',
    JSON.stringify(writes(sent, /chg-type\/resolve$/).at(-1)?.body))
  check('……处理完写结果和理由，左栏这一条移走', (await textOf(d.locator('[data-change-resolved-note]'))) === '理由：测试库里改的类型'
        && await until(async () => !(await itemsOf(page)).includes('chg-type')))

  // 清理目录项：先确认，确认后请求体只有动作，结果写目录第几版、影响面里的项现在不在目录里了
  await openChange(page, 'chg-removed')
  await d.locator('[data-change-action="clean_catalog"]').click()
  const dlg = page.locator('[role="alertdialog"], [role="dialog"]').last()
  await until(async () => (await textOf(dlg)).includes('清理 2 项目录'))
  check('清理目录项：先确认，写清后果', (await textOf(dlg)).includes('目录升一版') && writes(sent, /chg-removed\/resolve$/).length === 0, await textOf(dlg))
  await dlg.getByRole('button', { name: '清理', exact: true }).click()
  await until(async () => (await d.getAttribute('data-change-status')) === 'resolved')
  check('……请求体是 {action: clean_catalog}', JSON.stringify(writes(sent, /chg-removed\/resolve$/).at(-1)?.body) === '{"action":"clean_catalog"}')
  check('……结果写目录第 6 版、谁处理的', (await d.locator('[data-change-resolved-version]').getAttribute('data-change-resolved-version')) === '6'
        && (await textOf(d.locator('[data-change-resolution]'))).includes(`已清理目录项 · ${ACTOR}`))
  check('……影响面里的项现在不在目录里了', JSON.stringify(await attrs(d.locator('[data-change-ref]'), 'data-change-ref-now')) === '["gone","gone"]')
  check('……动了目录：重取了清单', sent.filter((s) => /^GET .*\/catalog$/.test(s.key)).length >= 2)
  check('……标签上的计数跟着少了一条', await until(async () => (await page.locator('[data-catalog-changes-count]').getAttribute('data-catalog-changes-count')) === '1'))

  // 加入码值：表单里列出新码值，含义可以先填，请求体带 codes
  await openChange(page, 'chg-codes')
  await d.locator('[data-change-action="add_codes"]').click()
  check('加入码值：列出新出现的码值，含义可以先空着', await d.locator('[data-change-code-input="app"]').isVisible())
  await d.locator('[data-change-code-input="app"]').fill('应用内')
  await d.locator('[data-change-codes-submit]').click()
  await until(async () => (await d.getAttribute('data-change-status')) === 'resolved')
  check('……请求体带码值和含义', JSON.stringify(writes(sent, /chg-codes\/resolve$/).at(-1)?.body) === '{"action":"add_codes","codes":{"app":"应用内"}}',
    JSON.stringify(writes(sent, /chg-codes\/resolve$/).at(-1)?.body))

  // 知道了：只改状态，还留在开着的里、不能再点知道了
  await openChange(page, 'chg-rows')
  await d.locator('[data-change-action="acknowledge"]').click()
  await until(async () => (await d.getAttribute('data-change-status')) === 'acknowledged')
  check('知道了：请求体只有动作，留在开着的里，不再给「知道了」', JSON.stringify(writes(sent, /chg-rows\/resolve$/).at(-1)?.body) === '{"action":"acknowledge"}'
        && await d.locator('[data-change-action="acknowledge"]').count() === 0 && (await itemsOf(page)).includes('chg-rows')
        && (await textOf(page.locator('[data-change-item="chg-rows"] [data-change-state]'))) === '知道了，还没处理')

  // 服务端拒了（误报没写理由的 422 这种）：原话写在处理一节里
  state.changes.find((c) => c.id === 'chg-rows').status = 'open'
  check('没有拦下的写请求、没有原生对话框、没有运行时报错', blocked(sent).length === 0 && natives.length === 0 && errors.length === 0,
    [...blocked(sent).map((s) => s.key), ...natives, ...errors].join(' | '))
})

await section('变化：重新起草建了任务，点得进进度', async () => {
  const state = freshState()
  state.changes = state.changes.filter((c) => c.id !== 'chg-codes')
  const { page, sent, errors } = await open(`/data/catalog/${S1}?view=changes&change=chg-type`, { handlers: handlers(state) })
  const d = detail(page)
  await until(async () => (await d.getAttribute('data-change-detail')) === 'chg-type')
  await d.locator('[data-change-action="redraft"]').click()
  await until(async () => (await d.getAttribute('data-change-status')) === 'resolved')
  check('重新起草：请求体 {action: redraft}', JSON.stringify(writes(sent, /chg-type\/resolve$/).at(-1)?.body) === '{"action":"redraft"}')
  check('……结果写起草任务的编号', (await textOf(d.locator('[data-change-job]'))).includes('起草任务 #check-'))
  await d.locator('[data-change-job-open]').click()
  check('……「查看进度」打开起草的进度', await until(async () => (await page.locator('[data-draft-dialog]').getAttribute('data-draft-dialog')) === 'running'),
    await page.locator('[data-draft-dialog]').getAttribute('data-draft-dialog').catch(() => ''))
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
})

await section('表详情：结构与取值变化一节', async () => {
  const state = freshState()
  const { page, errors } = await open(`/data/catalog/${S1}/orders`, { handlers: handlers(state) })
  const sec = page.locator('[data-catalog-section="changes"]')
  await sec.waitFor()
  await until(async () => (await sec.locator('[data-table-change]').count()) > 0)
  check('这张表的变化：开着的在前，处理过的也列上', JSON.stringify(await attrs(sec.locator('[data-table-change]'), 'data-table-change'))
        === '["chg-codes","chg-removed","chg-type","chg-stalled"]', JSON.stringify(await attrs(sec.locator('[data-table-change]'), 'data-table-change')))
  check('……撤销过「已列全」照实说（哪一列、目录第几版）', (await textOf(sec.locator('[data-table-revoked]'))).includes('channel')
        && (await textOf(sec.locator('[data-table-revoked]'))).includes('撤销了「已列全」（目录第 5 版）'), await textOf(sec.locator('[data-table-revoked]')))
  check('最近几次观察：行数、码值分布、最新业务日期、按日行数', JSON.stringify(await attrs(sec.locator('[data-table-observe]'), 'data-table-observe'))
        === '["rows:","codes_observe:channel","freshness:created_at","daily_volume:created_at"]')
  const codes = sec.locator('[data-table-observe="codes_observe:channel"]')
  check('……各写口径（照实写块采样、前 N 行、窗口）', (await textOf(codes.locator('[data-observe-basis]'))) === '口径：按存储顺序取前 100000 行'
        && (await textOf(sec.locator('[data-table-observe="daily_volume:created_at"] [data-observe-basis]'))) === '口径：业务日期 created_at 最近 35 天')
  check('……码值分布照服务端的占比写', (await textOf(codes.locator('[data-observe-value]'))).includes('web 50% · app 40% · shop 10%'), await textOf(codes.locator('[data-observe-value]')))
  check('……口径换过的说一句不互相比较', (await codes.getAttribute('data-observe-basis-changed')) === '1'
        && (await textOf(codes.locator('[data-observe-basis-note]'))).includes('口径不同的不互相比较'))
  check('……行数写数、往前几次收着', (await textOf(sec.locator('[data-table-observe="rows:"] [data-observe-value]'))) === '1,200 行'
        && await sec.locator('[data-table-observe="rows:"] details').count() === 1)
  const bars = sec.locator('[data-observe-bars]')
  check('……按日行数的小柱图：断了的两天也在（5 天），读屏名称写数', (await bars.getAttribute('data-observe-bars')) === '5'
        && (await bars.getAttribute('aria-label')).includes('有数据的 3 天，共 123 行'), await bars.getAttribute('aria-label').catch(() => ''))
  await sec.locator('[data-table-change="chg-removed"] [data-table-change-open]').click()
  check('「去处理」：到变化视图打开那一条', await until(async () => (await detail(page).getAttribute('data-change-detail')) === 'chg-removed')
        && new URL(page.url()).searchParams.get('view') === 'changes' && new URL(page.url()).searchParams.get('change') === 'chg-removed')
  await detail(page).locator('[data-change-open-table="orders"]').click()
  check('……从变化回到这张表：表清单视图，地址里不再带变化', await until(async () => new URL(page.url()).pathname.endsWith('/orders')
        && !new URL(page.url()).searchParams.has('view') && await page.locator('[data-catalog-section="changes"]').count() === 1),
  `${page.url()} · ${await page.locator('[data-catalog-section="changes"]').count()}`)
  await goto(page, `/data/catalog/${S1}/customers`)
  await until(async () => (await page.locator('[data-catalog-section="changes"] [data-table-observe-none]').count()) === 1)
  check('没有观察的表照实说', (await textOf(page.locator('[data-table-observe-none]'))).includes('还没有取值观察'))
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
})

await section('数据源卡片：要处理的角标', async () => {
  const state = freshState()
  const { page, errors } = await open('/data/databases', { handlers: handlers(state) })
  const card = page.locator(`[data-source="${NAME1}"]`)
  await card.waitFor()
  const badge = card.locator('[data-source-changes]')
  check('角标写几个变化要处理', await until(async () => (await badge.getAttribute('data-source-changes')) === '2') && (await textOf(badge)) === '2 个变化要处理')
  check('……读屏写全：哪个源、几条、点了去哪', (await badge.getAttribute('aria-label')) === `数据源「${NAME1}」有 2 个变化要处理，打开数据目录的「变化」`)
  await badge.click()
  check('……点开到数据目录的「变化」视图', await until(async () => new URL(page.url()).pathname === `/data/catalog/${S1}`
        && new URL(page.url()).searchParams.get('view') === 'changes') && await until(async () => (await page.locator('[data-change-item]').count()) === 4))
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
})

await section('定时维护：缺省关、建一条维护定时、改频率和开关、步骤', async () => {
  const state = freshState()
  const { page, sent, errors, natives } = await open('/data/databases', { handlers: handlers(state) })
  const card = page.locator(`[data-source="${NAME1}"]`)
  await card.waitFor()
  check('卡片上没有维护定时时不画「定时维护」标签', await card.locator('[data-maintenance-chip]').count() === 0)
  check('导入表格的源不给定时维护', await page.goto(`${WEB}/data/tables`, { waitUntil: 'networkidle' }).then(async () =>
    (await page.locator(`[data-source="${NAME2}"]`).waitFor().then(() => true)) && await page.locator(`[data-source="${NAME2}"] [data-maintenance-open]`).count() === 0))
  await goto(page, '/data/databases')
  await card.locator('[data-maintenance-open]').click()
  const dlg = page.locator('[data-maintenance-dialog]')
  await dlg.waitFor()
  check('缺省关闭，不提示低峰时段', (await dlg.getAttribute('data-maintenance-dialog')) === 'off' && await dlg.locator('[data-maintenance-offpeak]').count() === 0)
  check('三步都选着：探查结构、剖析需要的表、取值观察', JSON.stringify(await dlg.locator('[data-maintenance-step] input').evaluateAll((els) => els.map((e) => e.checked)))
        === '[true,true,true]' && JSON.stringify(await attrs(dlg.locator('[data-maintenance-step]'), 'data-maintenance-step')) === '["introspect","profile_needed","observe"]')
  check('剖析没开：照实说到点时剖析、观察两步不执行', (await textOf(dlg.locator('[data-maintenance-profile-off]'))).includes('两步不执行'))
  check('缺省时段是凌晨 03:00', (await page.locator('#maint-time').inputValue()) === '03:00')
  await dlg.locator('[data-maintenance-enabled] [role="switch"]').click()
  check('开启：提示选低峰时段', await until(async () => (await dlg.locator('[data-maintenance-offpeak]').count()) === 1)
        && (await textOf(dlg.locator('[data-maintenance-offpeak]'))).includes('低峰时段'))
  check('……频率的说法和接下来几次由服务端算', await until(async () => (await textOf(page.locator('[data-maintenance-preview]'))).includes('每天 03:00')))
  await page.locator('[data-maintenance-save]').click()
  await until(async () => writes(sent, /^POST \/schedules$/).length === 1)
  const created = writes(sent, /^POST \/schedules$/)[0].body
  check('保存：建一条 target_kind=source_maintenance、target_id=数据源的定时任务', created.target_kind === 'source_maintenance' && created.target_id === S1
        && created.cron === '0 3 * * *' && created.timezone === 'Asia/Shanghai' && created.enabled === true && created.overlap === 'skip'
        && created.misfire_grace_s === 3600 && !('workflow_id' in created) && !('webhook' in created) && created.name.includes(NAME1), JSON.stringify(created))
  check('……步骤没改不发 PUT /maintenance', writes(sent, /\/maintenance$/).length === 0)
  check('……保存后照服务端写下一次什么时候、还没有触发过', await until(async () => (await dlg.locator('[data-maintenance-next]').count()) === 1)
        && (await textOf(dlg.locator('[data-maintenance-last]'))).includes('还没有触发过'))
  // 最近一次触发：建了探查任务、剖析两步没执行和原因
  state.maintenance.schedules[0].last_fire = { id: 'fire-1', fire_at: ago(0.1), kind: 'scheduled', status: 'started', run_id: null, run_status: null,
    reason: '剖析需要的表没有执行：未开启；取值观察没有执行：未开启', notified: [], compare: null, created_at: ago(0.1),
    inputs: { steps: ['introspect', 'profile_needed', 'observe'], jobs: [{ step: 'introspect', kind: 'introspect', id: 'check-ijob-7', tables: 12 }],
      skipped: [{ step: 'profile_needed', reason: NOT_PROFILED }, { step: 'observe', reason: NOT_PROFILED }] } }
  await page.getByRole('button', { name: '取消', exact: true }).click()
  check('卡片上带「定时维护 · 每天 03:00」标签', await until(async () => (await textOf(card.locator('[data-maintenance-chip]'))).includes('定时维护 · 每天 03:00')))
  await card.locator('[data-maintenance-open]').click()
  await dlg.waitFor()
  check('再打开：开着，最近一次建了哪个任务（几张表）', (await dlg.getAttribute('data-maintenance-dialog')) === 'on'
        && (await textOf(dlg.locator('[data-maintenance-job="check-ijob-7"]'))).includes('探查结构：探查任务 #check-'), await textOf(dlg.locator('[data-maintenance-last]')))
  check('……哪几步没执行、原因照服务端', (await textOf(dlg.locator('[data-maintenance-skipped="profile_needed"]'))).startsWith('剖析需要的表没有执行：')
        && (await textOf(dlg.locator('[data-maintenance-skipped="observe"]'))).includes('未开启数据剖析'))
  check('……链到定时任务页的触发记录', (await dlg.locator('[data-maintenance-history]').getAttribute('href')) === '/schedules/check-sched-1')
  check('……探查任务没有另外的进度页，不给点了没用的「查看进度」', await dlg.locator('[data-maintenance-job="check-ijob-7"] [data-maintenance-job-open]').count() === 0)
  // 改频率：PATCH 只带改了的和版本
  await page.locator('#maint-time').fill('02:30')
  await until(async () => (await textOf(page.locator('[data-maintenance-preview]'))).includes('每天 02:30'))
  await page.locator('[data-maintenance-save]').click()
  await until(async () => writes(sent, /^PATCH \/schedules\//).length === 1)
  check('改频率：PATCH 只带 cron 和读到的版本', JSON.stringify(writes(sent, /^PATCH \/schedules\//)[0].body) === '{"version":1,"cron":"30 2 * * *"}',
    JSON.stringify(writes(sent, /^PATCH \/schedules\//)[0].body))
  // 改步骤、关掉：PUT 步骤，PATCH 开关（带新版本）
  await until(async () => (await dlg.getAttribute('data-maintenance-dialog')) === 'on')
  await dlg.locator('[data-maintenance-step="observe"] input').uncheck()
  await dlg.locator('[data-maintenance-enabled] [role="switch"]').click()
  await page.locator('[data-maintenance-save]').click()
  await until(async () => writes(sent, /^PATCH \/schedules\//).length === 2)
  check('改步骤：PUT /maintenance 只带选着的两步', JSON.stringify(writes(sent, /\/maintenance$/).at(-1)?.body) === '{"steps":["introspect","profile_needed"]}',
    JSON.stringify(writes(sent, /\/maintenance$/).at(-1)?.body))
  check('关掉：PATCH 带 enabled=false 和新的版本', JSON.stringify(writes(sent, /^PATCH \/schedules\//)[1].body) === '{"version":2,"enabled":false}',
    JSON.stringify(writes(sent, /^PATCH \/schedules\//)[1].body))
  await until(async () => (await dlg.getAttribute('data-maintenance-dialog')) === 'off')
  // 一步都不选：不能保存
  for (const s of ['introspect', 'profile_needed']) await dlg.locator(`[data-maintenance-step="${s}"] input`).uncheck()
  check('一步都不选：写明至少选一步，不能保存', await page.locator('[data-maintenance-save]').isDisabled()
        && (await textOf(dlg.locator('#maint-steps-hint'))) === '至少选一步')
  // 有没存的改动：关的时候先问，放弃修改才关
  await page.keyboard.press('Escape')
  await page.getByRole('button', { name: '放弃修改', exact: true }).click()
  await until(async () => (await page.locator('[data-maintenance-dialog]').count()) === 0)
  check('卡片上的标签照实写已暂停', await until(async () => (await textOf(card.locator('[data-maintenance-chip]'))) === '定时维护 · 已暂停'))
  // 最近一次建了剖析任务：「查看进度」到数据目录页打开那个剖析任务（地址里的 pjob 用过就去掉）
  state.profileJobs['check-pjob-1'] = profileJob('running', 1)
  state.maintenance.schedules[0].last_fire = { ...state.maintenance.schedules[0].last_fire, inputs: { steps: ['introspect', 'profile_needed'],
    jobs: [{ step: 'introspect', kind: 'introspect', id: 'check-ijob-8', tables: 12 }, { step: 'profile_needed', kind: 'profile', id: 'check-pjob-1', tables: 8 }],
    skipped: [] } }
  await card.locator('[data-maintenance-open]').click()
  await dlg.waitFor()
  await dlg.locator('[data-maintenance-job="check-pjob-1"] [data-maintenance-job-open]').click()
  check('剖析任务的「查看进度」：到数据目录页打开那个剖析任务', await until(async () => new URL(page.url()).pathname === `/data/catalog/${S1}`
        && (await page.locator('[data-profile-dialog]').count()) === 1) && await until(async () => !new URL(page.url()).searchParams.has('pjob')),
  page.url())
  check('没有拦下的写请求、没有原生对话框、没有运行时报错', blocked(sent).length === 0 && natives.length === 0 && errors.length === 0,
    [...blocked(sent).map((s) => s.key), ...natives, ...errors].join(' | '))
})

// 指名一张卡片的地址全站只有一种写法：/data/databases#source-<id>，带 &maintenance 时打开它的「定时维护」（复核 W4R-04：定时
// 任务页上维护定时的「去数据源页设置」以前链的是 ?source=<id>，数据源页不认，落在列表顶上）
await section('链到一张卡片：#source-<id> 滚过去，带 &maintenance 打开定时维护', async () => {
  const state = freshState()
  // 前面垫几张别的库，指名的那张落在第一屏下面：不滚过去就看不见
  const fillers = 'abcdefghij'.split('').map((x) => ({ ...state.sources[0], id: `check-chg-fill${x}`, name: `zzchgfill${x}`,
    database: `/tmp/check/zzchgfill${x}.db`, tools: [`db_query__zzchgfill${x}`] }))
  state.sources = [...fillers, ...state.sources]
  const inView = (page, id) => page.locator(`[id="source-${id}"]`).evaluate((el) => {
    const r = el.getBoundingClientRect()
    return r.top >= 0 && r.bottom <= window.innerHeight
  }).catch(() => false)
  // 从别的页链过来：先离开本页再进，免得只换 # 的同页跳转
  const land = async (page, path) => { await page.goto('about:blank'); await goto(page, path) }
  const { page, sent, errors, natives } = await open('/data/databases', { handlers: handlers(state), viewport: { width: 1280, height: 720 } })
  await page.locator(`[data-source="${NAME1}"]`).waitFor()
  check('（对照）不带地址落在列表顶上：那张卡片在第一屏下面', !(await inView(page, S1)))
  const dlg = page.locator('[data-maintenance-dialog]')
  await land(page, `/data/databases#source-${S1}&maintenance`)
  check('带 &maintenance：打开那张卡片的「定时维护」', await until(async () => (await dlg.count()) === 1)
        && (await textOf(page.getByRole('dialog'))).includes(`「${NAME1}」的定时维护`), await textOf(page.getByRole('dialog')))
  check('……卡片滚到眼前', await until(() => inView(page, S1)))
  check('……用过的 &maintenance 从地址里去掉，只留 #source-<id>', await until(async () => new URL(page.url()).hash === `#source-${S1}`), page.url())
  await page.keyboard.press('Escape')
  await until(async () => (await dlg.count()) === 0)
  await page.reload({ waitUntil: 'networkidle' })
  await page.locator(`[data-source="${NAME1}"]`).waitFor()
  await page.waitForTimeout(500)
  check('刷新：只滚过去，不再打开定时维护', await until(() => inView(page, S1)) && await dlg.count() === 0)
  await land(page, '/data/databases#source-check-chg-gone&maintenance')
  check('指名的数据源不在了：照实说，不打开对话框', await until(async () => (await page.locator('[data-toast]')
    .filter({ hasText: '没找到这个数据源，可能已经删除' }).count()) === 1) && await dlg.count() === 0)
  await land(page, `/data/tables#source-${S2}&maintenance`)
  await page.locator(`[data-source="${NAME2}"]`).waitFor()
  await page.waitForTimeout(500)
  check('导入表格的源不做定时维护：只滚过去，不打开对话框', await dlg.count() === 0
        && await until(async () => new URL(page.url()).hash === `#source-${S2}`), page.url())
  // 对话框打开时会算一次频率的说法（POST /schedules/preview，纯计算），不算写
  const saved = writes(sent, /./).filter((s) => s.key !== 'POST /schedules/preview')
  check('只是打开、没有保存：没有写请求、没有原生对话框、没有运行时报错', saved.length === 0 && blocked(sent).length === 0 && natives.length === 0
        && errors.length === 0, [...saved.map((s) => s.key), ...blocked(sent).map((s) => s.key), ...natives, ...errors].join(' | '))
})

await section('后台任务条：按服务端的先后，页头只做入口', async () => {
  const state = freshState()
  state.jobs = clone(JOBS_A)
  state.activeDraft = draftJob('queued')
  state.activeProfile = profileJob('queued')
  state.draftJobs['check-djob-1'] = state.activeDraft
  state.profileJobs['check-pjob-1'] = state.activeProfile
  const { page, errors } = await open(`/data/catalog/${S1}`, { handlers: handlers(state) })
  const strip = page.locator('[data-catalog-job-strip]')
  await until(async () => (await strip.locator('[data-catalog-job]').count()) === 3)
  check('一条任务条，按服务端的先后：探查、起草、剖析', JSON.stringify(await attrs(strip.locator('[data-catalog-job]'), 'data-catalog-job'))
        === '["introspect","draft","profile"]', JSON.stringify(await attrs(strip.locator('[data-catalog-job]'), 'data-catalog-job')))
  const texts = await strip.locator('[data-catalog-job-text]').allInnerTexts()
  check('……每项一行：「探查 3/20」「起草 排队中，探查结束后开始」「剖析 排队中，起草结束后开始」',
    JSON.stringify(texts) === JSON.stringify(['探查 3/20', '起草 排队中，探查结束后开始', '剖析 排队中，起草结束后开始']), JSON.stringify(texts))
  check('……进度条读屏可读', (await strip.locator('[data-catalog-job="introspect"] [role="progressbar"]').getAttribute('aria-valuenow')) === '3')
  const draftHead = page.locator('[data-catalog-draft-head]')
  const profileHead = page.locator('[data-catalog-profile]')
  check('页头只做入口：读屏写状态、不写数字', (await draftHead.getAttribute('aria-label')) === '起草（排队中）'
        && (await profileHead.getAttribute('aria-label')) === '数据剖析（排队中）'
        && !/\d/.test(`${await draftHead.innerText()}${await profileHead.innerText()}`), `${await draftHead.getAttribute('aria-label')} · ${await profileHead.getAttribute('aria-label')}`)
  check('……进度只在任务条上一处（页头没有进度数）', await page.locator('[data-catalog-page] header [data-progress]').count() === 0)

  // 反过来排：剖析在做（最近查询多数超时），起草等剖析
  state.jobs = clone(JOBS_B)
  state.activeProfile = profileJob('running', 2, PROFILE_NOTICE)
  state.profileJobs['check-pjob-1'] = state.activeProfile
  state.activeDraft = draftJob('queued')
  state.draftJobs['check-djob-1'] = state.activeDraft
  await goto(page, `/data/catalog/${S1}`)
  await until(async () => (await strip.locator('[data-catalog-job]').count()) === 2)
  const texts2 = await strip.locator('[data-catalog-job-text]').allInnerTexts()
  check('反过来排：「剖析 2/8」「起草 排队中，剖析结束后开始」', JSON.stringify(texts2) === JSON.stringify(['剖析 2/8', '起草 排队中，剖析结束后开始']), JSON.stringify(texts2))
  check('……剖析的「最近查询多数超时」在任务条上写全文（窄屏、触屏也看得见）', await until(async () =>
    (await textOf(strip.locator('[data-catalog-job="profile"] [data-catalog-job-notice]'))) === PROFILE_NOTICE))
  check('……页头的剖析入口进行中：读屏写「进行中」，不带提醒标记', (await profileHead.getAttribute('aria-label')) === '数据剖析（进行中）'
        && await page.locator('[data-catalog-profile-notice]').count() === 0)
  await strip.locator('[data-catalog-job="profile"] [data-catalog-job-open]').click()
  check('「查看」打开剖析的进度', await until(async () => (await page.locator('[data-profile-dialog]').count()) === 1))
  await page.keyboard.press('Escape')
  await until(async () => (await page.locator('[data-profile-dialog]').count()) === 0)
  await strip.locator('[data-catalog-job="draft"] [data-catalog-job-open]').click()
  check('「查看」打开起草的进度', await until(async () => (await page.locator('[data-draft-dialog]').count()) === 1))
  await page.keyboard.press('Escape')
  // 探查的「查看」：到数据源卡片上
  state.jobs = clone(JOBS_A)
  await goto(page, `/data/catalog/${S1}`)
  await until(async () => (await strip.locator('[data-catalog-job="introspect"]').count()) === 1)
  await strip.locator('[data-catalog-job="introspect"] [data-catalog-job-open]').click()
  check('探查的「查看」：到数据源页那张卡片', await until(async () => new URL(page.url()).pathname === '/data/databases'
        && new URL(page.url()).hash === `#source-${S1}`) && await until(async () => (await page.locator(`#source-${S1}`).count()) === 1))
  // 都结束了：任务条收起
  state.jobs = []
  state.activeDraft = null
  state.activeProfile = null
  await goto(page, `/data/catalog/${S1}`)
  await page.locator('[data-catalog-row]').first().waitFor()
  check('没有排队、进行中的任务：没有任务条，页头是普通入口', await page.locator('[data-catalog-job-strip]').count() === 0
        && (await draftHead.getAttribute('aria-label')) === '起草' && (await profileHead.getAttribute('aria-label')) === '数据剖析')
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
})

await section('窄屏：变化视图、任务条、表详情一节不撑宽页面', async () => {
  const state = freshState()
  state.jobs = clone(JOBS_B)
  state.activeProfile = profileJob('running', 2, PROFILE_NOTICE)
  state.profileJobs['check-pjob-1'] = state.activeProfile
  state.activeDraft = draftJob('queued')
  state.draftJobs['check-djob-1'] = state.activeDraft
  const { page, errors } = await open(`/data/catalog/${S1}?view=changes`, { handlers: handlers(state), viewport: { width: 390, height: 844 } })
  await until(async () => (await page.locator('[data-change-item]').count()) === 4)
  await until(async () => (await page.locator('[data-catalog-job-notice]').count()) === 1)
  check('变化列表、任务条（带着提醒）不横向滚动', await overflow(page) <= 1, String(await overflow(page)))
  await shot(page, 'changes-narrow-list')
  await openChange(page, 'chg-removed')
  check('一次只显示一栏：打开一条时列表收起，详情不横向滚动', !(await queue(page).isVisible()) && await overflow(page) <= 1)
  await detail(page).locator('[data-change-back]').click()
  check('「返回变化列表」回到列表', await until(async () => queue(page).isVisible()))
  await goto(page, `/data/catalog/${S1}/orders`)
  await page.locator('[data-catalog-section="changes"] [data-table-observe]').first().waitFor()
  await page.locator('[data-catalog-section="changes"]').scrollIntoViewIfNeeded()
  check('表详情的「结构与取值变化」不横向滚动', await overflow(page) <= 1, String(await overflow(page)))
  await shot(page, 'changes-narrow-table')
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
})

await browser.close()
const after = await realSources()
check('真库里的数据源数量没有变化', after === before, `${before} → ${after}`)
console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 数据源的变化与定时维护全部通过')
process.exit(failed ? 1 : 0)
