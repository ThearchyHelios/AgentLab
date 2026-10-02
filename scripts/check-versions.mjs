// 上传表格的版本页（数据源卡片上的「版本」，P3-SPEC 7.8、10.4）的交互回归。
//
// 版本页上的每个写操作都不可轻易回退：启用旧版本会让之后的运行换一份数据，作废接受不能撤回，清除原件
// 之后这一期再也不能按新配方重导。所以这里查的是「确认框把后果说全了没有」「请求里带没带界面看到的当前版本」
// 「服务端拒绝时有没有把原话摆出来并刷新列表」——这些正常路径的页面检查一个字都不会说。
//
// 写法同 check-manage：版本页的接口（快照列表、导入记录、清单）在后端还没有、也不该依赖沙箱里的真数据，
// 所以连 GET 一起用 page.route 伪造；数据源列表也伪造，只放这里造的几个源。没配的写请求一律拦成 503，
// 不写库。跑之前前后端都得起着（./scripts/dev.sh），默认连 5273 / 8000；对别的实例跑时带上地址：
//   AGENTLAB_WEB=http://localhost:<前端端口> AGENTLAB_API=http://localhost:<后端端口>/api node scripts/check-versions.mjs
// 只跑其中几节：CHECK_ONLY=启用,清单 node scripts/check-versions.mjs（按节名包含匹配）。
// 截图：CHECK_SHOTS=/某个目录 时把关键状态存下来；CHECK_THEME=light 换浅色跑一遍。
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const API = process.env.AGENTLAB_API ?? 'http://localhost:8000/api'
const SHOTS = process.env.CHECK_SHOTS ?? ''
const THEME = process.env.CHECK_THEME === 'light' ? 'light' : 'dark'
// 不用 playwright install：系统 Chrome 就够了
const CHROME = process.env.CHROME_PATH ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
/** 本机署名：确认框里写「署名（未认证）：检查脚本」，请求体的 signed_by 也是它 */
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

const browser = await chromium.launch({ executablePath: CHROME })
const opened = new Set()

/**
 * 开一页：handlers 按「METHOD 路径正则」匹配（GET 也可以配），没配的 GET 放行到后端，没配的写请求一律
 * 拦成 503。所有原生对话框都算失败
 */
async function open(path, { handlers = [], viewport = { width: 1280, height: 860 } } = {}) {
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
        sent.push({ key, body })
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
const coded = (status, code, detail) => json({ detail, code }, status)
const ago = (ms) => new Date(Date.now() - ms).toISOString()
const until = async (fn, ms = 8000, step = 150) => {
  const end = Date.now() + ms
  let v = await fn()
  while (!v && Date.now() < end) { await new Promise((r) => setTimeout(r, step)); v = await fn() }
  return v
}
const shot = async (page, name) => { if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png` }) }
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
// 夹具：全是假名、假数。A 按期累积三期（7、8、9 月，8 月带一条接受，7 月原件已清除）；B 每期替换、回滚过、
// 唯一的一期带接受（作废时回滚到一个简单导入的版本）；C 按期累积只有一期；D 简单上传（历史里有一个按期累积的
// 配方版本）；E 每期替换、带接受的一期作废时回滚到配方第 2 版（遮罩列在目标版本里丢失）；PG 手工登记的库
// ---------------------------------------------------------------------------
const H = 3600_000
const sha = (c) => c.repeat(64)
const JUL = { start: '2026-07-01', end: '2026-07-31' }
const AUG = { start: '2026-08-01', end: '2026-08-31' }
const SEP = { start: '2026-09-01', end: '2026-09-30' }
const rowsOf = (days) => ({ 日客流: days, 时段客流: days * 17, 时段客流_表内合计: days * 3 })
const addRows = (parts) => parts.reduce((acc, p) => {
  for (const [t, n] of Object.entries(p.rows)) acc[t] = (acc[t] ?? 0) + n
  return acc
}, {})

const A = 'check-ver-acc'
const B = 'check-ver-rep'
const C = 'check-ver-one'
const D = 'check-ver-simple'
const E = 'check-ver-roll'
const S = {
  a3: sha('a'), a2: sha('b'), a1: sha('c'), rev: sha('d'), lost: sha('e'), ret1: sha('f'), ret2: sha('9'),
  b2: sha('3'), b1: sha('4'), c1: sha('5'), d1: sha('6'), d0: sha('g'), e2: sha('0'), e1: sha('h'),
}
const IMP = {
  jul: 'imp-jul', aug0: 'imp-aug0', aug: 'imp-aug', sep: 'imp-sep', b1: 'imp-b1', b2: 'imp-b2', c1: 'imp-c1', e1: 'imp-e1', e2: 'imp-e2',
}

const part = (import_id, seq, per, file, days, extra = {}) => ({
  import_id, seq, period_start: per?.start ?? null, period_end: per?.end ?? null, file_name: file, raw_state: 'kept',
  status: 'active', rows: rowsOf(days), overrides: 0, waivers: 0, revoked: false, ...extra,
})
const snap = (id, over = {}) => ({
  id, current: false, mode: 'accumulate', created_at: ago(2 * H), activated_at: ago(2 * H), recipe: { id: 'rcp-3', seq: 3 },
  parts: [], tables: {}, db_sha256_prefix: id.slice(0, 8), db_size: 245_760, available: true, pinned_runs: 0,
  activatable: true, reason: null, reason_code: null, mask_lost: [], periods_diff: { added: [], removed: [] }, ...over,
})
const withTables = (s) => ({ ...s, tables: addRows(s.parts) })
const record = (id, seq, per, file, over = {}) => ({
  id, seq, build_id: sha('1'), file_name: file, file_size: 23_456, raw_sha256: sha('2'), raw_state: 'kept', status: 'active',
  purged: null, current: true, in_current: true, created_at: ago(5 * H), activated_at: ago(5 * H), recipe_id: 'rcp-3',
  recipe_seq: 3, period_start: per?.start ?? null, period_end: per?.end ?? null, signed_by: ACTOR, overrides: 0, waivers: 0,
  acceptances: [], revoked: null, manifest_artifact: `art-${id}`, rows: rowsOf(30), revoke_plan: null,
  raw_shared_with: [], raw_open_stagings: 0, ...over,
})

const P = {
  jul: part(IMP.jul, 1, JUL, '客流_2026-07.xlsx', 31, { raw_state: 'purged' }),
  aug: part(IMP.aug, 3, AUG, '客流_2026-08.xlsx', 31, { overrides: 1 }),
  sep: part(IMP.sep, 4, SEP, '客流_2026-09.xlsx', 30),
}
const ACCEPT_AUG = { check_id: 'R1', kind: 'override', reason: '分区乙当日设备故障，数字由人工补录', signed_by: '审核甲', at: ago(5 * H) }
const ACCEPT_AUG0 = { check_id: 'K1', kind: 'waiver', reason: '夜间合计无法核对，原表缺数', signed_by: '审核乙', at: ago(40 * H) }
const ref = (per, file) => ({ start: per.start, end: per.end, file_name: file })

/** A 的版本列表：故意让服务端的顺序和界面的顺序不同（不能启用的 S.rev 启用时间最新，排在列表第二） */
function snapsA() {
  return [
    withTables(snap(S.a3, { current: true, parts: [P.jul, P.aug, P.sep], activatable: false, reason: '已是当前版本',
      reason_code: 'current', pinned_runs: 1 })),
    withTables(snap(S.rev, { parts: [P.jul, { ...P.aug, revoked: true }], activatable: false, activated_at: ago(3 * H),
      reason: '包含已作废接受的导入，无法启用', reason_code: 'contains_revoked' })),
    withTables(snap(S.a2, { parts: [P.jul, P.aug], recipe: { id: 'rcp-2', seq: 2 }, activated_at: ago(26 * H),
      created_at: ago(26 * H), pinned_runs: 2, periods_diff: { added: [], removed: [ref(SEP, P.sep.file_name)] } })),
    withTables(snap(S.lost, { parts: [P.jul], available: false, activatable: false, activated_at: ago(30 * H),
      reason: '数据文件已丢失，无法启用', reason_code: 'file_lost' })),
    withTables(snap(S.a1, { mode: 'replace', parts: [P.jul], recipe: { id: 'rcp-1', seq: 1 }, activated_at: ago(50 * H),
      mask_lost: ['分区丁'], periods_diff: { added: [], removed: [ref(AUG, P.aug.file_name), ref(SEP, P.sep.file_name)] } })),
    withTables(snap(S.ret1, { parts: [P.jul], available: false, activatable: false, activated_at: ago(80 * H),
      reason: '已回收，无法启用', reason_code: 'retired' })),
    withTables(snap(S.ret2, { parts: [P.jul], available: false, activatable: false, activated_at: ago(90 * H),
      reason: '已回收，无法启用', reason_code: 'retired' })),
  ]
}
function importsA() {
  return [
    record(IMP.sep, 4, SEP, P.sep.file_name, {
      rows: rowsOf(30), raw_shared_with: [{ source_name: 'zzveracc', count: 1 }, { source_name: 'zzverother', count: 2 }],
      raw_open_stagings: 1,
    }),
    record(IMP.aug, 3, AUG, P.aug.file_name, {
      rows: rowsOf(31), overrides: 1, acceptances: [ACCEPT_AUG],
      revoke_plan: { action: 'remove_period', target_snapshot_id: null, target: null,
        result_parts: [ref(JUL, P.jul.file_name), ref(SEP, P.sep.file_name)], gaps: [AUG], mask_lost: [], reason: null },
    }),
    record(IMP.aug0, 2, AUG, '客流_2026-08_初版.xlsx', {
      status: 'superseded', current: false, in_current: false, recipe_id: 'rcp-2', recipe_seq: 2, waivers: 1,
      // 第二项是老数据：引用它的源已删除，老后端给的名字是 null（现在的后端已写成「已删除的数据源」）。界面兜底，不显示 null
      acceptances: [ACCEPT_AUG0], rows: rowsOf(31), raw_shared_with: [{ source_name: 'zzverother', count: 1 }, { source_name: null, count: 1 }],
    }),
    record(IMP.jul, 1, JUL, P.jul.file_name, {
      raw_state: 'purged', purged: { at: ago(20 * H), reason: '按保密要求清除', signed_by: '审核甲' }, recipe_id: 'rcp-1',
      recipe_seq: 1, rows: rowsOf(31),
    }),
  ]
}

const B_PART = part(IMP.b2, 2, SEP, '客流_替换_2026-09.xlsx', 30, { waivers: 1 })
function snapsB() {
  return [
    withTables(snap(S.b2, { current: true, mode: 'replace', recipe: { id: 'rcp-b', seq: 1 }, parts: [B_PART],
      created_at: ago(48 * H), activated_at: ago(1 * H), activatable: false, reason: '已是当前版本', reason_code: 'current' })),
    withTables(snap(S.b1, { mode: null, recipe: null, parts: [part(IMP.b1, 1, null, '早期客流.xlsx', 30)], activated_at: ago(60 * H),
      periods_diff: { added: [{ start: null, end: null, file_name: '早期客流.xlsx' }], removed: [ref(SEP, B_PART.file_name)] } })),
  ]
}
function importsB() {
  return [
    record(IMP.b2, 2, SEP, B_PART.file_name, {
      recipe_id: 'rcp-b', recipe_seq: 1, waivers: 1, acceptances: [{ ...ACCEPT_AUG0, signed_by: '审核丙' }],
      revoke_plan: { action: 'rollback', target_snapshot_id: S.b1,
        target: { parts: [{ start: null, end: null, file_name: '早期客流.xlsx' }], recipe_seq: null, mode: null, simple: true },
        result_parts: [], gaps: [], mask_lost: [], reason: null },
    }),
    record(IMP.b1, 1, null, '早期客流.xlsx', { status: 'superseded', current: false, in_current: false, recipe_id: null, recipe_seq: null }),
  ]
}
const snapsC = () => [withTables(snap(S.c1, { current: true, parts: [part(IMP.c1, 1, SEP, '客流_2026-09.xlsx', 30)],
  activatable: false, reason: '已是当前版本', reason_code: 'current' }))]
const importsC = () => [record(IMP.c1, 1, SEP, '客流_2026-09.xlsx')]

/** D：当前是简单导入，历史里有一个按期累积的配方版本（启用它时导入模式从无到「按期累积」，确认框必须写出来） */
const snapsD = () => [
  withTables(snap(S.d1, { current: true, mode: null, recipe: null, parts: [part('imp-d1', 3, null, '分区明细.csv', 30)],
    activatable: false, reason: '已是当前版本', reason_code: 'current' })),
  withTables(snap(S.d0, { parts: [part('imp-d0a', 1, JUL, '明细_2026-07.xlsx', 31), part('imp-d0b', 2, AUG, '明细_2026-08.xlsx', 31)],
    recipe: { id: 'rcp-d', seq: 2 }, activated_at: ago(20 * H),
    periods_diff: { added: [ref(JUL, '明细_2026-07.xlsx'), ref(AUG, '明细_2026-08.xlsx')], removed: [{ start: null, end: null, file_name: '分区明细.csv' }] } })),
]

/** E：每期替换，当前那一期带接受；作废时回滚到配方第 2 版、每期替换的 8 月，源的遮罩列在那个版本里没有同名列 */
const E_PART = part(IMP.e2, 2, SEP, '客流_滚动_2026-09.xlsx', 30, { waivers: 1 })
const E_OLD = part(IMP.e1, 1, AUG, '客流_滚动_2026-08.xlsx', 31)
const snapsE = () => [
  withTables(snap(S.e2, { current: true, mode: 'replace', parts: [E_PART], activatable: false, reason: '已是当前版本', reason_code: 'current' })),
  withTables(snap(S.e1, { mode: 'replace', recipe: { id: 'rcp-e2', seq: 2 }, parts: [E_OLD], activated_at: ago(30 * H), mask_lost: ['分区丁'],
    periods_diff: { added: [ref(AUG, E_OLD.file_name)], removed: [ref(SEP, E_PART.file_name)] } })),
]
const importsE = () => [
  record(IMP.e2, 2, SEP, E_PART.file_name, {
    waivers: 1, acceptances: [{ ...ACCEPT_AUG0, signed_by: '审核丁' }],
    revoke_plan: { action: 'rollback', target_snapshot_id: S.e1,
      target: { parts: [ref(AUG, E_OLD.file_name)], recipe_seq: 2, mode: 'replace', simple: false },
      result_parts: [], gaps: [], mask_lost: ['分区丁'], reason: null },
  }),
  record(IMP.e1, 1, AUG, E_OLD.file_name, { status: 'superseded', current: false, in_current: false, recipe_id: 'rcp-e2', recipe_seq: 2 }),
]

const blank = {
  kind: 'sqlite', host: null, port: null, username: null, options: {}, readonly: true, description: '', enabled: true,
  password_masked: '', has_password: false, cached_schema: '', schema_error: '', available_schemas: [],
  last_checked_at: null, last_check_ok: null, last_latency_ms: null, last_error: null,
}
const upload = (id, name, over = {}) => ({
  ...blank, id, name, origin: 'upload', table_count: 3, tools: [`db_query__${name}`], schema_synced_at: ago(2 * H),
  database: `/tmp/x/uploads/tables/${id}/builds/${sha('8')}.db`, import_mode: 'recipe', open_staging: null,
  current_recipe: { id: 'rcp-3', seq: 3, origin: 'rules', activated_at: ago(2 * H), signed_by: ACTOR }, ...over,
})
const SRC = {
  a: upload(A, 'zzveracc', { current_snapshot: { id: S.a3, created_at: ago(2 * H), activated_at: ago(2 * H), file_name: P.sep.file_name,
    raw_state: 'kept', mode: 'accumulate', periods: 3, period_start: JUL.start, period_end: SEP.end } }),
  b: upload(B, 'zzverrep', { table_count: 2, current_snapshot: { id: S.b2, created_at: ago(48 * H), activated_at: ago(1 * H),
    file_name: B_PART.file_name, raw_state: 'kept', mode: 'replace', periods: 1, period_start: SEP.start, period_end: SEP.end } }),
  c: upload(C, 'zzverone', { current_snapshot: { id: S.c1, created_at: ago(5 * H), activated_at: ago(5 * H), file_name: '客流_2026-09.xlsx',
    raw_state: 'kept', mode: 'accumulate', periods: 1, period_start: SEP.start, period_end: SEP.end } }),
  d: upload(D, 'zzversimple', { table_count: 1, import_mode: 'simple', current_recipe: null,
    current_snapshot: { id: S.d1, created_at: ago(3 * H), file_name: '分区明细.csv', raw_state: 'kept' } }),
  e: upload(E, 'zzverroll', { current_snapshot: { id: S.e2, created_at: ago(5 * H), activated_at: ago(5 * H), file_name: E_PART.file_name,
    raw_state: 'kept', mode: 'replace', periods: 1, period_start: SEP.start, period_end: SEP.end } }),
  // 服务端写了按期累积、却没给期数（老后端或字段漏了）：卡片不能替它说成「1 期」
  nop: upload('check-ver-nop', 'zzvernop', { current_snapshot: { id: sha('n'), created_at: ago(5 * H), activated_at: ago(5 * H),
    file_name: '客流_2026-09.xlsx', raw_state: 'kept', mode: 'accumulate' } }),
  pg: { ...blank, id: 'check-ver-pg', name: 'zzverpg', kind: 'postgres', host: '10.0.0.8', port: 5432, database: 'mes',
    username: 'reader', table_count: 0, tools: ['db_query__zzverpg'], schema_synced_at: null, origin: 'manual', current_snapshot: null },
}
const NAMES = ['zzveracc', 'zzverrep', 'zzverone', 'zzversimple', 'zzverroll', 'zzvernop', 'zzverother']

const MANIFEST_AUG = {
  artifact_id: 'art-imp-aug', verified: true, kind: 'import_manifest',
  content: {
    format: 'agentlab-import-manifest/1', source_id: A, import_id: IMP.aug, seq: 3, build_id: sha('1'), build_reused: false,
    snapshot_id: S.a3, db_sha256: sha('7'),
    file: { name: P.aug.file_name, size: 23_456, raw_sha256: `c0ffee12${'0'.repeat(56)}` },
    recipe: { id: 'rcp-3', sha256: `beefcafe${'0'.repeat(56)}`, origin: 'rules', canonical: { format: 'agentlab-recipe/2', sheets: [] } },
    period: { start: AUG.start, end: AUG.end, source: 'cells', cells: ['客流汇总!B2'] },
    checks: [
      { id: 'C1', kind: 'context_agree', title: '统计期多处一致', status: 'passed', category: 'structure', checked: 1, failed: 0,
        unverifiable: 0, sql: null, params: [], details: [], cells: [] },
      // 服务端每个核对最多逐条列 20 格，其余写成「另有 N 格不一致」「N 格无法核对：…」（recipe_checks._finish）：
      // 清单视图截断的话，正好丢掉末尾这两行
      { id: 'R1', kind: 'relation_sum_eq', title: '全日客流 = 分区甲 + 分区乙', status: 'mismatch', category: 'data_quality',
        checked: 31, failed: 25, unverifiable: 2,
        details: [...Array.from({ length: 20 }, (_, i) => `8 月 ${i + 1} 日：全日客流与分区之和不等`), '另有 5 格不一致', '2 格无法核对：明细含空值'],
        cells: ['客流汇总!E5'], sql: 'SELECT 日期 FROM 日客流 WHERE 全日客流 <> 分区甲 + 分区乙', params: [] },
      { id: 'R2', kind: 'relation_sum_eq', title: '全日客流与分区合计的口径对照', status: 'info', category: 'info', checked: 31, failed: 31,
        unverifiable: 0, details: ['31 天中 0 天相等'], cells: [], sql: null, params: [] },
    ],
    acceptances: { overrides: [{ check_id: 'R1', reason: ACCEPT_AUG.reason, signed_by: ACCEPT_AUG.signed_by, at: ACCEPT_AUG.at }], waivers: [] },
    confirmations: [{ id: 'placeholder:·', label: '「·」存为空值（31 格）', at: ago(5 * H) }],
    edits: [{ seq: 1, kind: 'fix', key: 'remove_label:日间:7-8', title: '在分段「日间」的期望标签中去掉「7-8」', at: ago(6 * H),
      signed_by: ACTOR, superseded: false }],
    receipt: {
      ledger: [{ sheet: '客流汇总', nonempty_scan: 771, nonempty_read: 771, unclaimed: 0, roles: {} }],
      tables: [{ name: '日客流', rows: 31 }, { name: '时段客流', rows: 527 }, { name: '时段客流_表内合计', rows: 93 }],
      placeholders: { '·': 31 },
      outside_text: [{ sheet: '客流汇总', cell: '客流汇总!B3', text: '客流汇总表', kind: 'text' }],
      rows_excluded: [{ sheet: '客流汇总', reason: 'total_not_kept', rows: [[28, 30]], cells: 93, anchor: '18-22 时合计', block: '交叉表' }],
    },
    notes: { templates: {}, rendered: { 日客流: { comment: '按日的客流；个别日期全日客流与分区之和不等，已写明理由接受。', columns: { 全日客流: '单位：人次' } } } },
    ai: { usage: [], consents: [] },
    signed_by: { name: ACTOR, verified: false }, staging_id: 'stg-aug', kind: 'reupload', created_at: ago(5 * H),
  },
}
const MANIFEST_JUL = { ...MANIFEST_AUG, artifact_id: 'art-imp-jul', verified: false,
  content: { ...MANIFEST_AUG.content, import_id: IMP.jul, file: { name: P.jul.file_name, size: 20_000, raw_sha256: sha('2') } } }

/** 版本页那几个接口的伪造。state 是这一页的可变数据（写操作的回答会改它），replies 是写请求的回答 */
function versionHandlers(state, replies = {}) {
  const reply = (k, fallback) => (route, ctx) => {
    const q = replies[k]
    const next = Array.isArray(q) ? q.shift() : q
    return (next ?? fallback)(route, ctx)
  }
  const none = (route) => json({ detail: '检查脚本没有准备这次的回答' }, 500)(route)
  return [
    [/^GET \/datasources$/, (route) => json(state.sources)(route)],
    [/^GET \/datasources\/check-ver-[a-z]+\/schema$/, json({ tables: ['日客流', '时段客流', '时段客流_表内合计'], summary: '', synced_at: ago(0) })],
    [/^GET \/datasources\/check-ver-[a-z]+\/snapshots$/, (route, { key }) => json(state.snaps[key.split('/')[2]] ?? [])(route)],
    [/^GET \/datasources\/check-ver-[a-z]+\/imports$/, (route, { key }) => json(state.imports[key.split('/')[2]] ?? [])(route)],
    [/^GET \/datasources\/check-ver-acc\/imports\/imp-aug\/manifest$/, json(MANIFEST_AUG)],
    [/^GET \/datasources\/check-ver-acc\/imports\/imp-jul\/manifest$/, json(MANIFEST_JUL)],
    [/^POST \/datasources\/[^/]+\/snapshots\/[^/]+\/activate$/, reply('activate', none)],
    [/^POST \/datasources\/[^/]+\/imports\/[^/]+\/remove$/, reply('remove', none)],
    [/^POST \/datasources\/[^/]+\/imports\/[^/]+\/revoke-acceptance$/, reply('revoke', none)],
    [/^POST \/datasources\/[^/]+\/imports\/[^/]+\/purge-raw$/, reply('purge', none)],
  ]
}
const freshState = () => ({
  sources: [SRC.a, SRC.b, SRC.c, SRC.d, SRC.e],
  snaps: { [A]: snapsA(), [B]: snapsB(), [C]: snapsC(), [D]: snapsD(), [E]: snapsE() },
  imports: { [A]: importsA(), [B]: importsB(), [C]: importsC(), [D]: [], [E]: importsE() },
})

// ---------------------------------------------------------------------------
// 页面上的定位
// ---------------------------------------------------------------------------
const versionsModal = (page) => page.locator('[role="dialog"]').filter({ has: page.locator('[data-versions]') })
/** 确认框（confirmDialog / promptDialog）：不含版本页内容的那个对话框 */
const confirmBox = (page) => page.locator('[role="dialog"]').filter({ hasNot: page.locator('[data-versions]') })
const consequences = async (page) => (await confirmBox(page).locator('ul li').allInnerTexts()).map((t) => t.trim())
const openVersions = async (page, name) => {
  await page.locator(`[data-source="${name}"] [data-versions-open]`).click()
  await page.locator('[data-versions]').waitFor({ timeout: 5000 })
  await until(async () => (await page.locator('[data-versions] [data-current-overview], [data-versions] [data-history-empty]').count()) > 0
    || (await page.locator('[data-versions] [role="tablist"]').count()) > 0, 5000)
}
const switchTab = (page, label) => versionsModal(page).getByRole('tab', { name: label }).click()

const FORBIDDEN = ['快照', '并集', '构建', '放行', '钉']
// 界面不露的机读码：蛇形的键名（contains_revoked、file_lost……）和几个单词形态的枚举值
const CODE_RE = /\b(?:[a-z]+_[a-z0-9_]+|accumulate|replace|override|waiver|kept|purged|absent|superseded|retired|rollback|activatable|current)\b/
/** 版本页整页（含标题）的文字里，没有禁用的说法、没有机读码。数据源名字是用户起的标识，先去掉再查 */
async function cleanCopy(page, where) {
  let t = await versionsModal(page).innerText()
  for (const n of NAMES) t = t.replaceAll(n, '')
  const bad = FORBIDDEN.filter((w) => t.includes(w))
  const code = t.match(CODE_RE)?.[0]
  check(`${where}：整页不出现「快照」「并集」「构建」「放行」「钉」`, bad.length === 0, bad.join('、'))
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

await section('版本 · 卡片入口与卡片上的版本信息', async () => {
  const state = freshState()
  state.sources = [...state.sources, SRC.nop, SRC.pg]
  const { page, sent, natives, errors, close } = await open('/data/tables', { handlers: versionHandlers(state) })
  for (const name of ['zzveracc', 'zzverrep', 'zzverone', 'zzversimple']) {
    const btn = page.locator(`[data-source="${name}"] [data-versions-open]`)
    check(`上传的表格「${name}」卡片上有「版本」`, await btn.count() === 1 && (await btn.innerText()).trim() === '版本')
  }
  const modeA = await page.locator('[data-source="zzveracc"] [data-current-mode]').innerText().catch(() => '')
  check('按期累积的源：卡片写「按期累积 · 3 期」', modeA.includes('按期累积 · 3 期'), modeA)
  const modeN = await page.locator('[data-source="zzvernop"] [data-current-mode]').innerText().catch(() => '')
  check('按期累积、服务端没给期数：卡片只写「按期累积」，不替它写成「1 期」', modeN.includes('按期累积') && !/\d\s*期/.test(modeN), modeN)
  const verB = page.locator('[data-source="zzverrep"] [data-current-version]')
  const textB = await verB.innerText().catch(() => '')
  check('回滚过的源（启用时间晚于导入时间）：卡片写「1 小时前启用」，不写当初的导入时间', textB.includes('1 小时前启用') && !textB.includes('导入'), textB)
  check('……悬停写启用时间和导入时间', ((await verB.getAttribute('title')) ?? '').includes('启用') && ((await verB.getAttribute('title')) ?? '').includes('导入于'))
  check('……每期替换的源写「每期替换」', textB.includes('每期替换'), textB)
  const textA = await page.locator('[data-source="zzveracc"] [data-current-version]').innerText().catch(() => '')
  check('没有回滚过的源照旧写「2 小时前导入」', textA.includes('2 小时前导入') && !textA.includes('启用'), textA)
  const textD = await page.locator('[data-source="zzversimple"] [data-current-version]').innerText().catch(() => '')
  check('简单上传（服务端没写导入模式）：不替它写「每期替换」「按期累积」', !textD.includes('每期替换') && !textD.includes('按期累积')
        && await page.locator('[data-source="zzversimple"] [data-current-mode]').count() === 0, textD)
  check('打开页面时不请求版本列表（点「版本」才取）', count(sent, /snapshots$/) === 0)
  await goto(page, '/data/databases')
  check('手工登记的库没有「版本」', await page.locator('[data-source="zzverpg"]').count() === 1
        && await page.locator('[data-source="zzverpg"] [data-versions-open]').count() === 0)
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('版本 · 当前版本与历史版本', async () => {
  const state = freshState()
  const { page, sent, natives, errors, close } = await open('/data/tables', { handlers: versionHandlers(state) })
  await openVersions(page, 'zzveracc')
  const root = page.locator('[data-versions]')
  // 开发模式下 React 的 StrictMode 会把挂载时的副作用执行两次，所以只要求发过，不数次数
  check('点「版本」发 GET …/snapshots 和 …/imports', count(sent, /^GET \/datasources\/check-ver-acc\/snapshots$/) >= 1
        && count(sent, /^GET \/datasources\/check-ver-acc\/imports$/) >= 1)
  check('对话框标题是「「zzveracc」的版本」', (await versionsModal(page).locator('h2').first().innerText()).includes('「zzveracc」的版本'))
  check('默认在「当前版本」页签', (await root.getAttribute('data-tab')) === 'current'
        && (await versionsModal(page).getByRole('tab', { name: '当前版本' }).getAttribute('aria-selected')) === 'true')
  const rule = await root.locator('[data-retention-rule]').innerText().catch(() => '')
  check('顶部常驻保留规则', rule.includes('除当前版本和被运行引用的版本外，系统保留最近 3 个版本') && rule.includes('回收后无法再启用'), rule)
  const overview = await root.locator('[data-current-overview]').innerText().catch(() => '')
  check('概览写导入模式、期数、配方第几版、各表行数', overview.includes('按期累积') && overview.includes('3 期')
        && overview.includes('配方第 3 版') && overview.includes('日客流') && overview.includes('92 行'), overview.replace(/\s+/g, ' '))
  const parts = root.locator('[data-snapshot-part]')
  check('当前版本的各期逐行列出（3 期，按统计期）', await parts.count() === 3
        && (await parts.evaluateAll((els) => els.map((e) => e.getAttribute('data-period')))).join('|')
          === '2026-07-01~2026-07-31|2026-08-01~2026-08-31|2026-09-01~2026-09-30')
  const aug = root.locator(`[data-snapshot-part="${IMP.aug}"]`)
  const augText = await aug.innerText()
  check('……每期写统计期、文件名和各表行数', augText.includes('2026-08-01 至 2026-08-31') && augText.includes('客流_2026-08.xlsx')
        && augText.includes('时段客流') && augText.includes('527 行'), augText.replace(/\s+/g, ' '))
  check('……原件状态：7 月写「原件已清除」', (await root.locator(`[data-snapshot-part="${IMP.jul}"]`).innerText()).includes('原件已清除'))
  check('「撤回这一期（作废接受）」只在带接受的 8 月出现', await root.locator('[data-revoke]').count() === 1
        && await aug.locator('[data-revoke]').count() === 1 && (await aug.locator('[data-revoke]').innerText()).includes('撤回这一期（作废接受）'))
  check('「清除原件」：原件已清除的 7 月没有，8 月、9 月有', await root.locator(`[data-snapshot-part="${IMP.jul}"] [data-purge-raw]`).count() === 0
        && await root.locator('[data-purge-raw]').count() === 2)
  check('「移除这一期」（按期累积）每期都有、都可用', await root.locator('[data-remove-period]').count() === 3
        && (await root.locator('[data-remove-period]').evaluateAll((els) => els.every((e) => !e.disabled))))
  check('每期都有「查看导入清单」', await root.locator('[data-snapshot-part] [data-manifest-open]').count() === 3)
  const acc = aug.locator('[data-part-acceptances]')
  check('各期首尾相接时不写空缺', await root.locator('[data-current-gaps]').count() === 0)
  check('8 月写「接受 1 条」，默认收起', (await acc.locator('summary').innerText()).includes('接受 1 条')
        && !(await acc.evaluate((el) => el.open)))
  await acc.locator('summary').click()
  const accText = await acc.innerText()
  check('……展开后有理由、署名（未认证）和时间', accText.includes(ACCEPT_AUG.reason) && accText.includes('署名（未认证）：审核甲')
        && /\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(accText), accText.replace(/\s+/g, ' '))
  await cleanCopy(page, '当前版本页签')
  await shot(page, 'versions-current')

  await switchTab(page, '历史版本')
  const items = root.locator('[data-history-list] > [data-snapshot]')
  const order = await items.evaluateAll((els) => els.map((e) => `${e.getAttribute('data-snapshot').slice(0, 1)}:${e.getAttribute('data-activatable')}`))
  check('历史版本：可以启用的在前（各自按启用时间倒序），当前版本不在这里', order.join(',') === 'b:true,c:true,d:false,e:false', order.join(','))
  const rev = root.locator(`[data-snapshot="${S.rev}"]`)
  const lost = root.locator(`[data-snapshot="${S.lost}"]`)
  check('不能启用的显示原因，没有「启用」', (await rev.innerText()).includes('包含已作废接受的导入，无法启用')
        && (await lost.innerText()).includes('数据文件已丢失，无法启用')
        && await rev.locator('[data-activate]').count() === 0 && await lost.locator('[data-activate]').count() === 0)
  const a2 = await root.locator(`[data-snapshot="${S.a2}"]`).innerText()
  check('每个版本写期数、各期的统计期、配方第几版、导入模式、文件大小、被几次运行引用',
        a2.includes('2 期') && a2.includes('2026-07-01 至 2026-07-31、2026-08-01 至 2026-08-31') && a2.includes('配方第 2 版')
        && a2.includes('按期累积') && a2.includes('文件大小 240.0 KB') && a2.includes('被 2 次运行引用'), a2.replace(/\s+/g, ' '))
  check('……遮罩列丢失的版本标出来', (await root.locator(`[data-snapshot="${S.a1}"] [data-mask-lost]`).innerText().catch(() => '')).includes('分区丁'))
  check('……可以启用的有「启用这个版本」', (await root.locator(`[data-snapshot="${S.a2}"] [data-activate]`).innerText()).includes('启用这个版本'))
  const group = root.locator('[data-retired-group]')
  check('已回收的折叠成一行「已回收 2 个」，展开前不列出', (await group.innerText()).includes('已回收 2 个')
        && await root.locator('[data-snapshot][data-reason-code="retired"]').count() === 0)
  await group.getByRole('button').click()
  check('……展开后列出 2 个，写「已回收，无法启用」，没有「启用」', await root.locator('[data-snapshot][data-reason-code="retired"]').count() === 2
        && (await group.innerText()).includes('已回收，无法启用') && await group.locator('[data-activate]').count() === 0)
  await cleanCopy(page, '历史版本页签')
  await shot(page, 'versions-history')

  // 中间一期已经移除过（当前版本只剩 7 月、9 月）：之后再打开版本页，概览里仍然写出空缺，不只在移除时的确认框里写一次
  await page.keyboard.press('Escape')
  await page.waitForTimeout(200)
  state.snaps[A] = state.snaps[A].map((s) => (s.current ? withTables({ ...s, parts: [P.jul, P.sep] }) : s))
  await openVersions(page, 'zzveracc')
  const gapNote = await root.locator('[data-current-overview] [data-current-gaps]').innerText().catch(() => '')
  check('当前版本各期之间有空缺：概览写「各期之间有空缺（2026-08-01 至 2026-08-31），比较不同期之前需要先查询日期的覆盖范围」',
        gapNote.includes('各期之间有空缺（2026-08-01 至 2026-08-31），比较不同期之前需要先查询日期的覆盖范围')
        && (await root.locator('[data-current-overview]').innerText()).includes('2 期'), gapNote)
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('版本 · 启用旧版本', async () => {
  const state = freshState()
  const activated = {
    ...SRC.a, current_snapshot: { ...SRC.a.current_snapshot, id: S.a2, created_at: ago(26 * H), activated_at: ago(0), periods: 2, period_end: AUG.end },
  }
  const replies = {
    activate: [
      coded(409, 'contains_revoked', '这个版本包含已作废接受的导入，无法启用'),
      coded(409, 'snapshot_unavailable', '这个版本已回收，无法启用'),
      // 服务端发现遮罩列丢失：之后的列表里这个版本带上 mask_lost，界面应当按它重新打开确认框
      (route) => {
        state.snaps[A] = state.snaps[A].map((s) => (s.id === S.a2 ? { ...s, mask_lost: ['分区丁'] } : s))
        return coded(409, 'mask_lost', '以下遮罩列在这个版本中没有同名列：分区丁。请在确认框中确认，或先调整遮罩设置')(route)
      },
      json({ source: activated, snapshot_id: S.a2, previous_snapshot_id: S.a3, recipe_id: 'rcp-2' }),
      coded(409, 'base_changed', '数据源的当前版本已被更新，请刷新版本列表后重试'),
    ],
  }
  const { page, sent, natives, errors, close } = await open('/data/tables', { handlers: versionHandlers(state, replies) })
  await openVersions(page, 'zzveracc')
  await switchTab(page, '历史版本')
  const root = page.locator('[data-versions]')
  const snapsGets = () => count(sent, /^GET \/datasources\/check-ver-acc\/snapshots$/)
  const activateBtn = (id) => root.locator(`[data-snapshot="${id}"] [data-activate]`)
  const confirmBtn = () => confirmBox(page).getByRole('button', { name: '启用这个版本' })

  await activateBtn(S.a2).click()
  await confirmBox(page).waitFor()
  const cons = await consequences(page)
  check('「启用」先弹确认框（标题「启用这个版本？」）', (await confirmBox(page).locator('h2').innerText()).includes('启用这个版本？'))
  check('……后果：「当前版本将不再包含：2026-09-01 至 2026-09-30」（按两个版本的各期算）',
        cons.some((c) => c.includes('当前版本将不再包含') && c.includes('2026-09-01 至 2026-09-30')), cons.join(' | '))
  check('……后果：新发起的运行用这个版本、已经在进行的运行不受影响',
        cons.includes('启用后，新发起的运行使用这个版本') && cons.includes('已经在进行的运行不受影响'), cons.join(' | '))
  check('……后果：「配方回到这个版本使用的第 2 版」「之后上传新一期时，以启用后的版本为基础」「未完成的导入需要重新试运行」',
        cons.includes('配方回到这个版本使用的第 2 版') && cons.includes('之后上传新一期时，以启用后的版本为基础')
        && cons.includes('未完成的导入需要重新试运行'), cons.join(' | '))
  check('……导入模式相同时不写「导入模式回到…」，没有遮罩列丢失时不写遮罩那条', !cons.some((c) => c.includes('导入模式回到') || c.includes('遮罩')))
  check('……正文写「署名（未认证）：检查脚本」，不是危险样式', (await confirmBox(page).innerText()).includes(`署名（未认证）：${ACTOR}`)
        && await confirmBox(page).locator('button.btn-danger').count() === 0)
  // 理由可选（7.2）：确认框带一个「理由（可选）」输入框，空着也能确认；超过 500 字不让确认
  const reasonBox = () => confirmBox(page).locator('input')
  check('……确认框里有「理由（可选）」输入框，空着时「启用这个版本」可以点',
        (await confirmBox(page).locator('label').innerText().catch(() => '')).includes('理由（可选）')
        && await reasonBox().count() === 1 && (await reasonBox().inputValue()) === '' && !(await confirmBtn().isDisabled()))
  await reasonBox().fill('长'.repeat(501))
  await page.waitForTimeout(50)
  check('……理由超过 500 字时说原因并禁用确认', (await confirmBox(page).innerText()).includes('理由不能超过 500 字') && await confirmBtn().isDisabled())
  await confirmBox(page).getByRole('button', { name: '取消' }).click()
  await page.waitForTimeout(200)
  check('取消则不发请求', count(sent, /activate$/) === 0)

  // 409 contains_revoked
  let gets = snapsGets()
  await activateBtn(S.a2).click()
  await confirmBtn().click()
  await until(async () => (await root.innerText()).includes('这个版本包含已作废接受的导入，无法启用'), 5000)
  const body1 = lastBody(sent, /activate$/)
  check('确认后发 POST …/activate：confirm、expected_current_snapshot_id（列表里的当前版本）、signed_by',
        body1?.confirm === true && body1?.expected_current_snapshot_id === S.a3 && body1?.signed_by === ACTOR && !('ack_mask_lost' in (body1 ?? {})),
        JSON.stringify(body1))
  check('……理由没填：请求体不带 reason', body1 != null && !('reason' in body1), JSON.stringify(body1))
  check('……发到这个版本的地址', sent.filter((s) => /activate$/.test(s.key)).at(-1)?.key.includes(`/snapshots/${S.a2}/activate`))
  check('409 contains_revoked：显示服务端原话，并重新取版本列表', (await root.locator('[data-versions-banner="err"]').innerText()).includes('这个版本包含已作废接受的导入，无法启用')
        && await until(() => snapsGets() > gets, 3000))

  // 409 snapshot_unavailable（理由只填了空白：等于没填）
  gets = snapsGets()
  await activateBtn(S.a2).click()
  await confirmBox(page).waitFor()
  await reasonBox().fill('   ')
  await confirmBtn().click()
  await until(async () => (await root.innerText()).includes('这个版本已回收，无法启用'), 5000)
  const bodyBlank = lastBody(sent, /activate$/)
  check('……理由只有空白：请求体同样不带 reason', count(sent, /activate$/) === 2 && bodyBlank != null && !('reason' in bodyBlank), JSON.stringify(bodyBlank))
  check('409 snapshot_unavailable：显示服务端原话，并重新取版本列表', (await root.innerText()).includes('这个版本已回收，无法启用')
        && !(await root.innerText()).includes('这个版本包含已作废接受的导入') && await until(() => snapsGets() > gets, 3000))

  // 409 mask_lost → 按刷新后的列表重新打开确认框（这次写了理由）
  const REASON = '9 月的文件传错了，先回到 8 月的版本'
  await activateBtn(S.a2).click()
  await confirmBox(page).waitFor()
  await reasonBox().fill(`  ${REASON} `)
  await confirmBtn().click()
  await until(async () => (await confirmBox(page).count()) === 1 && (await consequences(page)).some((c) => c.includes('分区丁')), 5000)
  const bodyReason = sent.filter((x) => /activate$/.test(x.key))[2]?.body ?? null
  check('……填了理由：请求体带 reason（去掉首尾空白）', bodyReason?.reason === REASON, JSON.stringify(bodyReason))
  const cons2 = await consequences(page)
  check('409 mask_lost：刷新后重新打开确认框，把丢失的遮罩列列进后果', cons2.includes('以下遮罩列在这个版本中没有同名列，启用后不再遮罩：分区丁'), cons2.join(' | '))
  check('……重开的确认框带着刚才写的理由，不用再写一遍', (await reasonBox().inputValue().catch(() => '')) === REASON)
  gets = snapsGets()
  await confirmBtn().click()
  await until(async () => count(sent, /activate$/) === 4, 5000)
  const body2 = lastBody(sent, /activate$/)
  check('……再次确认发出 ack_mask_lost（与列表里的 mask_lost 相同）', JSON.stringify(body2?.ack_mask_lost) === JSON.stringify(['分区丁'])
        && body2?.expected_current_snapshot_id === S.a3, JSON.stringify(body2))
  check('……再次确认的请求同样带 reason', body2?.reason === REASON, JSON.stringify(body2))
  check('启用成功后重新取版本列表', await until(() => snapsGets() > gets, 3000))
  const cardMode = await page.locator('[data-source="zzveracc"] [data-current-mode]').innerText().catch(() => '')
  const cardVer = await page.locator('[data-source="zzveracc"] [data-current-version]').innerText().catch(() => '')
  check('……卡片按返回的数据源更新：「按期累积 · 2 期」「刚刚启用」', cardMode.includes('2 期') && cardVer.includes('刚刚启用'), cardVer)

  // 换模式、遮罩列丢失的版本；409 base_changed
  await activateBtn(S.a1).click()
  await confirmBox(page).waitFor()
  const cons3 = await consequences(page)
  check('导入模式不同的版本：后果有「导入模式回到每期替换」「配方回到这个版本使用的第 1 版」，不再包含的写两期',
        cons3.includes('导入模式回到每期替换') && cons3.includes('配方回到这个版本使用的第 1 版')
        && cons3.some((c) => c.includes('当前版本将不再包含') && c.includes('2026-08-01 至 2026-08-31') && c.includes('2026-09-01 至 2026-09-30')),
        cons3.join(' | '))
  check('……遮罩列丢失时有遮罩那一条', cons3.includes('以下遮罩列在这个版本中没有同名列，启用后不再遮罩：分区丁'))
  gets = snapsGets()
  await confirmBtn().click()
  await until(async () => (await root.innerText()).includes('数据源的当前版本已被更新，请刷新版本列表后重试'), 5000)
  const body3 = lastBody(sent, /activate$/)
  check('……请求带 ack_mask_lost', JSON.stringify(body3?.ack_mask_lost) === JSON.stringify(['分区丁']), JSON.stringify(body3))
  check('409 base_changed：显示服务端原话和「列表已刷新，请重新确认」，并重新取版本列表',
        (await root.innerText()).includes('数据源的当前版本已被更新，请刷新版本列表后重试')
        && (await root.locator('[data-versions-refreshed]').innerText().catch(() => '')).includes('列表已刷新，请重新确认')
        && await until(() => snapsGets() > gets, 3000))
  await cleanCopy(page, '启用之后')

  // 当前是简单导入，启用一个按期累积的配方版本：之后上传新一期会按累积处理，确认框必须写导入模式
  await page.keyboard.press('Escape')
  await page.waitForTimeout(200)
  const activates = count(sent, /activate$/)
  await openVersions(page, 'zzversimple')
  await switchTab(page, '历史版本')
  await root.locator(`[data-snapshot="${S.d0}"] [data-activate]`).click()
  await confirmBox(page).waitFor()
  const consD = await consequences(page)
  check('当前是简单导入、目标按期累积：后果有「导入模式回到按期累积」「配方回到这个版本使用的第 2 版」，不写「回到简单导入」',
        consD.includes('导入模式回到按期累积') && consD.includes('配方回到这个版本使用的第 2 版')
        && !consD.some((c) => c.startsWith('这个数据源回到简单导入')), consD.join(' | '))
  check('……当前版本将增加的两期也写出来', consD.some((c) => c.includes('当前版本将增加') && c.includes('2026-07-01 至 2026-07-31')
        && c.includes('2026-08-01 至 2026-08-31')), consD.join(' | '))
  await confirmBox(page).getByRole('button', { name: '取消' }).click()
  await page.waitForTimeout(200)
  await page.keyboard.press('Escape')
  await page.waitForTimeout(200)

  // 当前按配方导入，启用简单导入的版本：写「回到简单导入」，不写导入模式
  await openVersions(page, 'zzverrep')
  await switchTab(page, '历史版本')
  await root.locator(`[data-snapshot="${S.b1}"] [data-activate]`).click()
  await confirmBox(page).waitFor()
  const consB = await consequences(page)
  check('当前按配方、目标是简单导入：后果有「这个数据源回到简单导入」，不写配方第几版和导入模式',
        consB.some((c) => c.startsWith('这个数据源回到简单导入')) && !consB.some((c) => c.includes('导入模式回到') || c.startsWith('配方回到')),
        consB.join(' | '))
  await confirmBox(page).getByRole('button', { name: '取消' }).click()
  await page.waitForTimeout(200)
  check('……两次都取消，不发请求', count(sent, /activate$/) === activates)
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('版本 · 移除这一期', async () => {
  const state = freshState()
  const removed = { ...SRC.a, current_snapshot: { ...SRC.a.current_snapshot, id: S.a2, activated_at: ago(0), created_at: ago(26 * H), periods: 2 } }
  const replies = { remove: [json({ source: removed, snapshot_id: S.a2, removed_import_id: IMP.sep, reused: true })] }
  const { page, sent, natives, errors, close } = await open('/data/tables', { handlers: versionHandlers(state, replies) })
  await openVersions(page, 'zzveracc')
  const root = page.locator('[data-versions]')
  const confirmBtn = () => confirmBox(page).getByRole('button', { name: '移除这一期' })

  await root.locator(`[data-remove-period="${IMP.aug}"]`).click()
  await confirmBox(page).waitFor()
  const cons = await consequences(page)
  check('移除中间一期：标题「从当前版本中移除 2026-08-01 至 2026-08-31？」', (await confirmBox(page).locator('h2').innerText()).includes('从当前版本中移除 2026-08-01 至 2026-08-31'))
  check('……后果：「移除后当前版本包含：」剩下的两期', cons.includes('移除后当前版本包含：2026-07-01 至 2026-07-31、2026-09-01 至 2026-09-30'), cons.join(' | '))
  check('……后果：各期之间出现空缺（2026-08-01 至 2026-08-31）', cons.some((c) => c.startsWith('移除后各期之间出现空缺（2026-08-01 至 2026-08-31）')), cons.join(' | '))
  check('……后果：配方和导入模式不变、可以在「历史版本」中重新启用移除前的版本、已经在进行的运行不受影响',
        cons.includes('配方和导入模式不变') && cons.some((c) => c.startsWith('可以在「历史版本」中重新启用移除前的版本'))
        && cons.includes('已经在进行的运行不受影响'), cons.join(' | '))
  check('……理由为空时确认禁用', await confirmBtn().isDisabled())
  await confirmBox(page).getByRole('button', { name: '取消' }).click()
  await page.waitForTimeout(200)
  check('……取消则不发请求', count(sent, /\/remove$/) === 0)

  await root.locator(`[data-remove-period="${IMP.sep}"]`).click()
  await confirmBox(page).waitFor()
  const cons2 = await consequences(page)
  check('移除最后一期：不写空缺', !cons2.some((c) => c.includes('空缺')) && cons2.includes('移除后当前版本包含：2026-07-01 至 2026-07-31、2026-08-01 至 2026-08-31'), cons2.join(' | '))
  check('……正文写署名（未认证）', (await confirmBox(page).innerText()).includes(`署名（未认证）：${ACTOR}`))
  await confirmBox(page).locator('input').fill('九月的文件传错了，先移除')
  check('……写了理由才能确认', await confirmBtn().isEnabled())
  const gets = count(sent, /^GET \/datasources\/check-ver-acc\/snapshots$/)
  await confirmBtn().click()
  await until(() => count(sent, /\/remove$/) === 1, 5000)
  const body = lastBody(sent, /\/remove$/)
  check('发 POST …/imports/{这一期}/remove：confirm、expected_current_snapshot_id、理由、署名',
        sent.filter((s) => /\/remove$/.test(s.key)).at(-1)?.key.includes(`/imports/${IMP.sep}/remove`)
        && body?.confirm === true && body?.expected_current_snapshot_id === S.a3 && body?.reason === '九月的文件传错了，先移除'
        && body?.signed_by === ACTOR, JSON.stringify(body))
  check('……成功后重新取版本列表', await until(() => count(sent, /^GET \/datasources\/check-ver-acc\/snapshots$/) > gets, 3000))
  check('……卡片按返回的数据源更新为 2 期', (await page.locator('[data-source="zzveracc"] [data-current-mode]').innerText()).includes('2 期'))
  await page.keyboard.press('Escape')
  await page.waitForTimeout(200)

  await openVersions(page, 'zzverone')
  const only = page.locator('[data-versions] [data-remove-period]')
  check('当前版本只有一期：「移除这一期」禁用，旁边写「这是当前版本里唯一的一期，不能移除」', await only.count() === 1 && await only.isDisabled()
        && (await page.locator('[data-versions] [data-remove-last]').innerText()).includes('这是当前版本里唯一的一期，不能移除'))
  await page.keyboard.press('Escape')
  await page.waitForTimeout(200)
  await openVersions(page, 'zzverrep')
  check('每期替换的源没有「移除这一期」', await page.locator('[data-versions] [data-remove-period]').count() === 0)
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('版本 · 撤回这一期（作废接受）', async () => {
  const state = freshState()
  const replies = {
    revoke: [
      json({ source: SRC.a, snapshot_id: S.a2, action: 'remove_period' }),
      json({ source: { ...SRC.b, import_mode: 'simple', current_recipe: null, current_snapshot: { id: S.b1, created_at: ago(60 * H), activated_at: ago(0), file_name: '早期客流.xlsx', raw_state: 'kept' } },
        snapshot_id: S.b1, action: 'rollback' }),
      coded(409, 'revoke_target_changed', '可以回滚的版本已有变化，请刷新后重新确认'),
    ],
  }
  const { page, sent, natives, errors, close } = await open('/data/tables', { handlers: versionHandlers(state, replies) })
  await openVersions(page, 'zzveracc')
  const confirmBtn = () => confirmBox(page).getByRole('button', { name: '撤回这一期（作废接受）' })
  await page.locator(`[data-versions] [data-revoke="${IMP.aug}"]`).click()
  await confirmBox(page).waitFor()
  const cons = await consequences(page)
  check('按期累积：确认框是危险样式（确认按钮标红）', await confirmBox(page).locator('button.btn-danger').count() === 1)
  check('……后果第一条「作废后无法恢复」', cons[0]?.startsWith('作废后无法恢复'), cons[0] ?? '')
  check('……写明这一期将从当前版本中移除、剩下的各期、配方和导入模式不变',
        cons.includes('这一期将从当前版本中移除，当前版本包含：2026-07-01 至 2026-07-31、2026-09-01 至 2026-09-30；配方和导入模式不变'), cons.join(' | '))
  check('……按预案写出空缺', cons.some((c) => c.startsWith('移除后各期之间出现空缺（2026-08-01 至 2026-08-31）')))
  check('……导入记录和接受理由保留、已经在进行的运行不受影响', cons.includes('导入记录和接受理由保留，标记为已作废') && cons.includes('已经在进行的运行不受影响'))
  check('……理由为空时确认禁用', await confirmBtn().isDisabled())
  await confirmBox(page).locator('input').fill('补录的数字有误，接受作废')
  const gets = count(sent, /^GET \/datasources\/check-ver-acc\/snapshots$/)
  await confirmBtn().click()
  await until(() => count(sent, /revoke-acceptance$/) === 1, 5000)
  const body = lastBody(sent, /revoke-acceptance$/)
  check('发 POST …/revoke-acceptance：expected_current_snapshot_id、理由、署名；累积模式不带回滚目标',
        sent.filter((s) => /revoke-acceptance$/.test(s.key)).at(-1)?.key.includes(`/imports/${IMP.aug}/revoke-acceptance`)
        && body?.confirm === true && body?.expected_current_snapshot_id === S.a3 && body?.reason === '补录的数字有误，接受作废'
        && body?.signed_by === ACTOR && !('expected_target_snapshot_id' in (body ?? {})), JSON.stringify(body))
  check('……成功后重新取版本列表', await until(() => count(sent, /^GET \/datasources\/check-ver-acc\/snapshots$/) > gets, 3000))
  await page.keyboard.press('Escape')
  await page.waitForTimeout(200)

  await openVersions(page, 'zzverrep')
  await page.locator(`[data-versions] [data-revoke="${IMP.b2}"]`).click()
  await confirmBox(page).waitFor()
  const consB = await consequences(page)
  check('每期替换：按预案写出回滚目标（各期、简单导入），危险样式', consB.includes('当前版本将回到：早期客流.xlsx（统计期未记录），简单导入')
        && await confirmBox(page).locator('button.btn-danger').count() === 1, consB.join(' | '))
  check('……目标是简单导入时有「这个数据源回到简单导入」', consB.some((c) => c.startsWith('这个数据源回到简单导入')), consB.join(' | '))
  check('……「作废后无法恢复」', consB.some((c) => c.startsWith('作废后无法恢复')))
  await confirmBox(page).locator('input').fill('文件口径有误')
  await confirmBtn().click()
  await until(() => count(sent, /revoke-acceptance$/) === 2, 5000)
  const bodyB = lastBody(sent, /revoke-acceptance$/)
  check('……请求另带 expected_target_snapshot_id（预案里的回滚目标）', bodyB?.expected_target_snapshot_id === S.b1
        && bodyB?.expected_current_snapshot_id === S.b2, JSON.stringify(bodyB))
  check('……卡片按返回的数据源更新（回到简单导入，不再写导入模式）',
        await until(async () => (await page.locator('[data-source="zzverrep"] [data-current-mode]').count()) === 0, 3000))
  await page.keyboard.press('Escape')
  await page.waitForTimeout(200)

  // 每期替换、回滚目标是配方版本：后果写出各期、配方第几版、导入模式；目标版本里遮罩列丢失时列出来并带 ack_mask_lost；
  // 服务端在锁内重算的回滚目标变了（revoke_target_changed）时显示原话、注明列表已刷新，并重新取列表
  await openVersions(page, 'zzverroll')
  const rootE = page.locator('[data-versions]')
  await rootE.locator(`[data-revoke="${IMP.e2}"]`).click()
  await confirmBox(page).waitFor()
  const consE = await consequences(page)
  check('每期替换、回滚到配方版本：后果写「当前版本将回到：2026-08-01 至 2026-08-31，配方第 2 版，每期替换」',
        consE.includes('当前版本将回到：2026-08-01 至 2026-08-31，配方第 2 版，每期替换'), consE.join(' | '))
  check('……目标不是简单导入时不写「这个数据源回到简单导入」', !consE.some((c) => c.startsWith('这个数据源回到简单导入')), consE.join(' | '))
  check('……回滚目标里遮罩列丢失时有遮罩那一条', consE.includes('以下遮罩列在这个版本中没有同名列，启用后不再遮罩：分区丁'), consE.join(' | '))
  await confirmBox(page).locator('input').fill('补录口径有误，接受作废')
  const getsE = count(sent, /^GET \/datasources\/check-ver-roll\/snapshots$/)
  const listGetsE = count(sent, /^GET \/datasources$/)
  await confirmBtn().click()
  await until(async () => (await rootE.innerText()).includes('可以回滚的版本已有变化，请刷新后重新确认'), 5000)
  const bodyE = lastBody(sent, /revoke-acceptance$/)
  check('……请求带 ack_mask_lost（与预案的 mask_lost 相同）和 expected_target_snapshot_id',
        JSON.stringify(bodyE?.ack_mask_lost) === JSON.stringify(['分区丁']) && bodyE?.expected_target_snapshot_id === S.e1
        && bodyE?.expected_current_snapshot_id === S.e2, JSON.stringify(bodyE))
  check('409 revoke_target_changed：显示服务端原话和「列表已刷新，请重新确认」',
        (await rootE.locator('[data-versions-banner="err"]').innerText().catch(() => '')).includes('可以回滚的版本已有变化，请刷新后重新确认')
        && (await rootE.locator('[data-versions-refreshed]').innerText().catch(() => '')).includes('列表已刷新，请重新确认'))
  check('……并重新取版本列表和数据源列表', await until(() => count(sent, /^GET \/datasources\/check-ver-roll\/snapshots$/) > getsE, 3000)
        && await until(() => count(sent, /^GET \/datasources$/) > listGetsE, 3000))
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('版本 · 清除原件', async () => {
  const state = freshState()
  const replies = {
    purge: [(route) => {
      state.imports[A] = state.imports[A].map((r) => (r.id === IMP.sep ? { ...r, raw_state: 'purged' } : r))
      return json({
        import: { ...state.imports[A][0], raw_state: 'purged' }, file_deleted: true,
        also_purged: [{ id: 'imp-other', source_id: 'check-ver-other', source_name: 'zzverother', seq: 2, file_name: '客流_2026-09_副本.xlsx' }],
        discarded_stagings: ['stg-open-1'],
      })(route)
    }],
  }
  const { page, sent, natives, errors, close } = await open('/data/tables', { handlers: versionHandlers(state, replies) })
  await openVersions(page, 'zzveracc')
  const root = page.locator('[data-versions]')
  const confirmBtn = () => confirmBox(page).getByRole('button', { name: '清除原件' })
  await root.locator(`[data-purge-raw="${IMP.sep}"]`).click()
  await confirmBox(page).waitFor()
  const cons = await consequences(page)
  check('清除原件：危险样式', await confirmBox(page).locator('button.btn-danger').count() === 1)
  check('……正文写哪一次导入、哪一期和署名（未认证）', (await confirmBox(page).innerText()).includes(`第 4 次导入，2026-09-01 至 2026-09-30。署名（未认证）：${ACTOR}`))
  check('……后果：证据面板显示「原件已清除」、不能再按修改后的配方重新导入',
        cons.includes('证据面板将显示「原件已清除」') && cons.includes('原件清除后，这一期无法再按修改后的配方重新导入'), cons.join(' | '))
  check('……提交前就列出同一份内容的其他导入记录', cons.includes('同一份内容的其他导入记录一并清除：「zzveracc」1 条、「zzverother」2 条'), cons.join(' | '))
  check('……提交前就列出一并放弃的未完成导入', cons.includes('引用它的 1 个未完成导入一并放弃'), cons.join(' | '))
  check('……理由为空时确认禁用', await confirmBtn().isDisabled())
  await confirmBox(page).locator('input').fill('原件含不必要的明细，按要求清除')
  const listGets = count(sent, /^GET \/datasources$/)
  const gets = count(sent, /^GET \/datasources\/check-ver-acc\/snapshots$/)
  await confirmBtn().click()
  await until(async () => (await root.innerText()).includes('另外清除了'), 5000)
  const body = lastBody(sent, /purge-raw$/)
  check('发 POST …/purge-raw：confirm、理由、署名', body?.confirm === true && body?.reason === '原件含不必要的明细，按要求清除'
        && body?.signed_by === ACTOR, JSON.stringify(body))
  const banner = await root.locator('[data-versions-banner="info"]').innerText().catch(() => '')
  check('……之后显示一并清除的导入记录和放弃的未完成导入', banner.includes('已清除。另外清除了 1 条导入记录的原件，放弃了 1 个未完成导入')
        && banner.includes('「zzverother」第 2 次导入（客流_2026-09_副本.xlsx）'), banner.replace(/\s+/g, ' '))
  check('……重新取版本列表和数据源列表（卡片上的原件状态）', await until(() => count(sent, /^GET \/datasources\/check-ver-acc\/snapshots$/) > gets, 3000)
        && await until(() => count(sent, /^GET \/datasources$/) > listGets, 3000))
  check('……清除后这一期不再有「清除原件」', await until(async () => (await root.locator(`[data-purge-raw="${IMP.sep}"]`).count()) === 0, 3000))
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('版本 · 导入清单', async () => {
  const state = freshState()
  const { page, sent, natives, errors, close } = await open('/data/tables', { handlers: versionHandlers(state) })
  await openVersions(page, 'zzveracc')
  const root = page.locator('[data-versions]')
  await root.locator(`[data-snapshot-part="${IMP.aug}"] [data-manifest-open]`).click()
  await root.locator('[data-manifest]').waitFor({ timeout: 5000 })
  check('「查看导入清单」发 GET …/imports/{id}/manifest', count(sent, new RegExp(`^GET /datasources/check-ver-acc/imports/${IMP.aug}/manifest$`)) >= 1)
  check('在版本页里打开（内嵌视图，不另开对话框）', await page.locator('[role="dialog"]').count() === 1 && (await root.getAttribute('data-tab')) === 'manifest')
  const block = (id) => root.locator(`[data-manifest-block="${id}"]`).innerText().catch(() => '')
  const file = await block('file')
  check('文件块：名字、大小、内容哈希前 8 位、原件状态', file.includes('客流_2026-08.xlsx') && file.includes('22.9 KB') && file.includes('内容哈希 c0ffee12')
        && file.includes('原件已保存'), file.replace(/\s+/g, ' '))
  const recipe = await block('recipe')
  check('配方块：第几版、来源、哈希前 8 位、「查看配方 JSON」', recipe.includes('配方第 3 版') && recipe.includes('规则起草')
        && recipe.includes('内容哈希 beefcafe') && recipe.includes('查看配方 JSON'), recipe.replace(/\s+/g, ' '))
  const period = await block('period')
  check('统计期块：起止和来源格子', period.includes('2026-08-01 至 2026-08-31') && period.includes('B2'), period.replace(/\s+/g, ' '))
  const checks = await block('checks')
  check('核对块：状态、标题、细节，可以展开 SQL', checks.includes('不一致') && checks.includes('全日客流 = 分区甲 + 分区乙')
        && checks.includes('8 月 3 日：全日客流与分区之和不等') && checks.includes('查看 SQL'), checks.replace(/\s+/g, ' '))
  const r1 = root.locator('[data-manifest-check="R1"]')
  check('……核对行写计数：「核对 31 处，不一致 25 处，无法核对 2 处」', (await r1.locator('[data-check-counts]').innerText().catch(() => '')).trim()
        === '核对 31 处，不一致 25 处，无法核对 2 处')
  check('……细节不截断：20 格之后的「另有 5 格不一致」「2 格无法核对：明细含空值」都在', await r1.locator('[data-check-details] > li').count() === 22
        && checks.includes('8 月 20 日：全日客流与分区之和不等') && checks.includes('另有 5 格不一致') && checks.includes('2 格无法核对：明细含空值'))
  check('……通过的核对只写核对了几处，「说明」类不写计数', (await root.locator('[data-manifest-check="C1"] [data-check-counts]').innerText().catch(() => '')).trim() === '核对 1 处'
        && await root.locator('[data-manifest-check="R2"] [data-check-counts]').count() === 0)
  const accept = await block('acceptances')
  check('接受块：核对、理由、署名（未认证）、时间', accept.includes('R1') && accept.includes(ACCEPT_AUG.reason)
        && accept.includes('署名（未认证）：审核甲') && /\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(accept), accept.replace(/\s+/g, ' '))
  check('确认清单块：勾过的项（只写文字，不露 id）', (await block('confirmations')).includes('「·」存为空值（31 格）')
        && !(await block('confirmations')).includes('placeholder'))
  check('修改记录块：修复的标题和署名', (await block('edits')).includes('在分段「日间」的期望标签中去掉「7-8」') && (await block('edits')).includes('修复'))
  check('说明块：表说明、列说明', (await block('notes')).includes('按日的客流') && (await block('notes')).includes('全日客流：单位：人次'))
  const excluded = await root.locator('[data-summary-excluded]').innerText().catch(() => '')
  check('排除的行：原因、锚点、行号', excluded.includes('排除的行') && excluded.includes('只核对、不另存的合计行') && excluded.includes('18-22 时合计')
        && excluded.includes('第 28–30 行'), excluded.replace(/\s+/g, ' '))
  check('回执摘要：区域外文字全文，注明不导入数据表、裁判可见', (await block('receipt')).includes('客流汇总表')
        && (await block('receipt')).includes('区域外文字不导入数据表；核对报告时，裁判可以看到其中可见单元格的文字'))
  check('AI 用量块：没有调用模型', (await block('ai')).includes('这次导入没有调用模型'))
  check('清单校验通过时不出哈希警告', await root.locator('[data-manifest-unverified]').count() === 0)
  const raw = root.locator('[data-manifest-raw]')
  check('「查看原始清单」默认收起', !(await raw.evaluate((el) => el.open)) && !(await root.locator('[data-manifest]').innerText()).includes('"format"'))
  await cleanCopy(page, '导入清单（原始清单收起时）')
  await shot(page, 'versions-manifest')
  await raw.locator('summary').click()
  check('……展开后显示等宽的 JSON', (await raw.locator('pre').innerText()).includes('"format": "agentlab-import-manifest/1"'))
  await root.locator('[data-manifest-back]').click()
  check('「返回版本列表」回到当前版本页签', (await root.getAttribute('data-tab')) === 'current' && await root.locator('[data-snapshot-part]').count() === 3)
  await root.locator(`[data-snapshot-part="${IMP.jul}"] [data-manifest-open]`).click()
  await root.locator('[data-manifest]').waitFor({ timeout: 5000 })
  check('内容哈希复验没通过的清单：标出「不能作为证据」', (await root.locator('[data-manifest-unverified]').innerText().catch(() => '')).includes('内容哈希校验失败，可能已被修改，不能作为证据'))
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('版本 · 全部导入记录', async () => {
  const state = freshState()
  const replies = {
    purge: [(route) => {
      state.imports[A] = state.imports[A].map((r) => (r.id === IMP.aug0 ? { ...r, raw_state: 'purged' } : r))
      return json({ import: { ...state.imports[A][2], raw_state: 'purged' }, file_deleted: true, also_purged: [], discarded_stagings: [] })(route)
    }],
  }
  const { page, sent, natives, errors, close } = await open('/data/tables', { handlers: versionHandlers(state, replies) })
  await openVersions(page, 'zzveracc')
  await switchTab(page, '全部导入记录')
  const root = page.locator('[data-versions]')
  const recs = root.locator('[data-import-record]')
  check('列出全部导入记录（含已被替换的），按导入次序倒序', (await recs.evaluateAll((els) => els.map((e) => e.getAttribute('data-import-record')))).join(',')
        === [IMP.sep, IMP.aug, IMP.aug0, IMP.jul].join(','))
  const old = await root.locator(`[data-import-record="${IMP.aug0}"]`).innerText()
  check('已被替换的那条：写「已被替换」，接受逐条显示理由和「署名（未认证）」', old.includes('已被替换') && old.includes(ACCEPT_AUG0.reason)
        && old.includes('署名（未认证）：审核乙'), old.replace(/\s+/g, ' '))
  check('在当前版本里的写「在当前版本中」', (await root.locator(`[data-import-record="${IMP.aug}"]`).innerText()).includes('在当前版本中'))
  check('每条都有「查看导入清单」', await root.locator('[data-import-records] [data-manifest-open]').count() === 4)
  check('这里没有移除、作废按钮（只对当前版本有意义，放在「当前版本」页签）', await root.locator('[data-import-records] [data-remove-period], [data-import-records] [data-revoke]').count() === 0
        && (await root.innerText()).match(/移除这一期|撤回这一期/) === null)
  check('在当前版本里的记录没有「清除原件」（操作只放在「当前版本」页签）', await root.locator(`[data-import-record="${IMP.sep}"] [data-purge-raw], [data-import-record="${IMP.aug}"] [data-purge-raw]`).count() === 0)
  check('已被替换、原件还在的记录有「清除原件」（全页签只有这一个）', await root.locator('[data-import-records] [data-purge-raw]').count() === 1
        && await root.locator(`[data-import-record="${IMP.aug0}"] [data-purge-raw]`).count() === 1)
  await cleanCopy(page, '全部导入记录页签')
  await shot(page, 'versions-imports')

  await root.locator(`[data-import-record="${IMP.aug0}"] [data-purge-raw]`).click()
  await confirmBox(page).waitFor()
  const cons = await consequences(page)
  const purgeBtn = () => confirmBox(page).getByRole('button', { name: '清除原件' })
  check('……点了弹同一个清除确认框：危险样式，正文写第 2 次导入和统计期', await confirmBox(page).locator('button.btn-danger').count() === 1
        && (await confirmBox(page).innerText()).includes('第 2 次导入，2026-08-01 至 2026-08-31'))
  check('……后果照列，提交前列出同一份内容的其他导入记录（源已删除、名字为 null 的写「已删除的数据源」）', cons.includes('证据面板将显示「原件已清除」')
        && cons.includes('同一份内容的其他导入记录一并清除：「zzverother」1 条、「已删除的数据源」1 条')
        && !cons.some((c) => c.includes('null') || c.includes('未完成导入')), cons.join(' | '))
  check('……理由为空时确认禁用', await purgeBtn().isDisabled())
  await confirmBox(page).locator('input').fill('旧文件已无保留必要')
  await purgeBtn().click()
  await until(() => count(sent, /purge-raw$/) === 1, 5000)
  const body = lastBody(sent, /purge-raw$/)
  check('……发 POST …/imports/{这条记录}/purge-raw：confirm、理由、署名', sent.filter((s) => /purge-raw$/.test(s.key)).at(-1)?.key.includes(`/imports/${IMP.aug0}/purge-raw`)
        && body?.confirm === true && body?.reason === '旧文件已无保留必要' && body?.signed_by === ACTOR, JSON.stringify(body))
  check('……清除后列表刷新，这条写「原件已清除」，不再有「清除原件」', await until(async () => (await root.locator(`[data-import-record="${IMP.aug0}"] [data-purge-raw]`).count()) === 0
        && (await root.locator(`[data-import-record="${IMP.aug0}"]`).innerText()).includes('原件已清除'), 3000))
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('版本 · 窄屏', async () => {
  const state = freshState()
  const { page, errors, close } = await open('/data/tables', { handlers: versionHandlers(state), viewport: { width: 390, height: 800 } })
  // 横向溢出：对话框本身、版本页内容两处取最大（哪一处被撑宽都算）。不量整页：390 宽时应用外壳里收起的导航本来就
  // 超出视口（打开版本页之前就有，与版本页无关），量进来只会把那边的问题算到这里
  const overflowOf = (sel) => page.evaluate((s) => Math.max(0, ...[...document.querySelectorAll(s)].map((el) => el.scrollWidth - el.clientWidth)), sel)
  await openVersions(page, 'zzveracc')
  const root = page.locator('[data-versions]')
  const where = []
  where.push(['当前版本页签', await overflowOf('[role="dialog"], [data-versions]')])
  await switchTab(page, '历史版本')
  await root.locator('[data-retired-group]').getByRole('button').click()
  where.push(['历史版本页签（已回收的展开）', await overflowOf('[role="dialog"], [data-versions]')])
  await switchTab(page, '全部导入记录')
  where.push(['全部导入记录页签', await overflowOf('[role="dialog"], [data-versions]')])
  await root.locator(`[data-import-record="${IMP.aug}"] [data-manifest-open]`).click()
  await root.locator('[data-manifest]').waitFor({ timeout: 5000 })
  await root.locator('[data-manifest-raw] summary').click()
  await root.locator('[data-check-sql] summary').first().click()
  where.push(['导入清单（原始清单、SQL 展开）', await overflowOf('[role="dialog"], [data-versions]')])
  await root.locator('[data-manifest-back]').click()
  await switchTab(page, '当前版本')
  await root.locator(`[data-revoke="${IMP.aug}"]`).click()
  await confirmBox(page).waitFor()
  where.push(['作废确认框', await overflowOf('[role="dialog"]')])
  for (const [w, px] of where) check(`390 宽：${w}没有横向滚动`, px <= 1, `${px}px`)
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await browser.close()
console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 版本页全部通过')
process.exit(failed ? 1 : 0)
