#!/usr/bin/env node
// 评测页（docs/design/next-round/01-效果度量与回归评测.md 第 18 项；评测-D8）的界面检查。
//
// 守的是这些「看着没坏、其实不能用」的地方：
//   - 导航「评测」排在设置前面、⌥0 进得去；开了登录的使用者看不到它，手输地址也被送回应用首页；
//   - 题集列表照服务端给的写最近一次评测、同一变体的趋势、定时（没有定时给「设定时」，带着题集去新建定时）；新建、导入题集；
//   - 加题四种方式：写 SQL 定期望（选数据源、写 SQL、预览、点选格、试算、保存）、遮罩列拒收的原话落在 SQL 下面、选口径（不能复用
//     的照列、写原因、选不了）、从问数据运行认定（选运行、选查询、选取哪一列）、期望反问（不带期望答案）；交上去的请求体逐项核对，
//     数值期望不许人填（请求体里不出现 value 这类键）；
//   - 口径升版待处置：顶部提示、处置三选一（叫法由服务端给，没有缺省）；启用开关、导出；
//   - 发起评测：估算和发起是同一份请求体，评一次 / A/B 对照 / 目录贡献对照（预设）各自的请求体；估算的项数、花费、今天已花照服务端写；
//   - 评测详情：进度、每题正在做什么；停止、只重试没做完的、重评、设为基线——摆不摆按钮照服务端的 actions；总览卡片和各项分布；
//     逐题表（结论、期望和答案里的数、SQL 差在哪、点开去运行详情）、?case= 定位那道题；和上一次比（指纹差异、转移、归因、换基准）；
//     A/B 配对表（都对、只 A 对、只 B 对、p 值、看不出差别）；
//   - 「口径命中与重复度」：参考线、这一窗口落在哪一档、趋势、口径账本的明细（直接调 /api/ledger/stats）、反复问的问题能加入题集
//     （带着问法去加题）、能提取模板（从最近一次的运行提取，去画布）；
//   - 目录快照：拍当前目录、影子起草先估算再开始、能停；
//   - 记录页：评测运行写来源「评测」，点了回到评测里那道题；运行详情页头有键盘到得了的链接；
//   - 设置：评测一组的值和今天已花，存的时候只交 eval 一组。
//
// 页面是真页面（/evals 走真的应用外壳），评测、口径账本统计、设置里评测那一组、记录页要的几样都在浏览器层答掉（虚构的门店、
// 题集、数字）；写请求照约定改这里的假状态并记下请求体，别的 GET 放行到服务端（外壳要它），别的写请求一律 503、最后核对一条
// 都没漏出去。
//
//   AGENTLAB_WEB=http://localhost:<前端端口> AGENTLAB_API=http://localhost:<后端端口>/api node scripts/check-evals.mjs
// CHECK_ONLY=加题,发起 只跑段名里含这些字的段。
import { mkdirSync } from 'node:fs'
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
/** EVALS_SHOTS=<目录> 时几处关键画面各截一张，供人眼复核 */
const SHOTS = process.env.EVALS_SHOTS ?? ''
if (SHOTS) mkdirSync(SHOTS, { recursive: true })
const shot = async (page, name) => { if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: false }).catch(() => {}) }
const CHROME = process.env.CHROME_PATH ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'

let failed = 0
const check = (name, cond, detail = '') => {
  const d = String(detail ?? '').replace(/\s+/g, ' ').slice(0, 220)
  console.log(`  ${cond ? '✓' : '✗'} ${name}${d ? ` — ${d}` : ''}`)
  if (!cond) failed++
}
const ONLY = (process.env.CHECK_ONLY ?? '').split(',').map((s) => s.trim()).filter(Boolean)
const opened = new Set()
async function section(name, fn) {
  if (ONLY.length && !ONLY.some((k) => name.includes(k))) return
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
const until = async (fn, ms = 6000, step = 100) => {
  const end = Date.now() + ms
  const once = () => Promise.resolve().then(fn).catch(() => null)
  let v = await once()
  while (!v && Date.now() < end) {
    await new Promise((r) => setTimeout(r, step))
    v = await once()
  }
  return v
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const iso = (t) => new Date(t).toISOString()
const NOW = Date.now()
const H = 3600e3
const clone = (v) => structuredClone(v)
/** 漏到服务端的写请求（假接口没认的）：检查结束时必须是空的 */
const leaked = []

// ---------------------------------------------------------------- 夹具（通用示例名、编的数）

const DS = { id: 'ds-shop', name: '门店库', kind: 'sqlite', enabled: true, database: 'shop.db', options: {}, schema_cache: {},
  created_at: iso(NOW - 90 * 24 * H), updated_at: iso(NOW - 24 * H) }
const BUDGET = { max_parallel: 2, per_case_timeout_s: 900, per_run_max_usd: 2, daily_max_usd: 5 }
const RATE = (passed, n) => ({ passed, n, rate: n ? Math.round((passed / n) * 10000) / 10000 : null, ci: n ? [0.3, 0.95] : null })
const SUITE = {
  id: 'suite-a', name: '门店问数据回归', description: '虚构门店库上的几道问法', timezone: 'Asia/Shanghai', default_variant: {},
  budget: {}, effective_budget: BUDGET, archived: false, version: 3, created_by: '检查员甲', created_at: iso(NOW - 10 * 24 * H),
  updated_at: iso(NOW - H), counts: { cases: 3, enabled: 3 },
  latest: { id: 'ev-1', status: 'succeeded', running: false, arm: null, pair_id: null, trigger: 'manual', created_at: iso(NOW - 2 * H),
    finished_at: iso(NOW - H), counts: { total: 3, done: 3 }, accuracy: RATE(2, 3), traced_accuracy: RATE(1, 3) },
  trend: [{ id: 'ev-old', created_at: iso(NOW - 48 * H), arm: null, ...RATE(1, 3) }, { id: 'ev-1', created_at: iso(NOW - 2 * H), arm: null, ...RATE(2, 3) }],
  schedules: [{ id: 'sch-1', name: '每周回归', enabled: true, description_text: '每周一 09:00（Asia/Shanghai）', next_run_at: iso(NOW + 24 * H) }],
}
const SUITE_B = { ...SUITE, id: 'suite-b', name: '库存问法', description: '', counts: { cases: 0, enabled: 0 }, latest: null, trend: [],
  schedules: [], version: 1 }
const CASE_SQL = {
  id: 'case-1', suite_id: 'suite-a', seq: 1, kind: 'ask', question: '9 月第一周按支付日的销售额是多少？', datasource_ids: ['ds-shop'],
  workflow_id: null, workflow_version: null, input_template: {}, history: null,
  expectations: [{ id: 'e1', kind: 'scalar', column: 'gmv', row: 0, required: true }],
  gold: { kind: 'sql', datasource_id: 'ds-shop', sql: 'SELECT SUM(amount) AS gmv FROM orders' }, tags: ['口径陷阱'], enabled: true,
  gold_source: { id: 'ds-shop', name: '门店库' }, datasources: [{ id: 'ds-shop', name: '门店库' }], archived: false, superseded_by: null,
  version: 2, case_hash: 'h1', trial: { status: 'ok', code: null, reason: null, as_of: iso(NOW - 3 * H), run_ids: ['run-gold-1'], at: iso(NOW - 3 * H), by: '检查员甲' },
  upgrade: null, created_by: '检查员甲', created_at: iso(NOW - 9 * 24 * H), updated_at: iso(NOW - 3 * H),
}
const CASE_CAL = {
  ...CASE_SQL, id: 'case-2', seq: 2, question: '上周的周销售额？', expectations: [{ id: 'e1', kind: 'scalar', metric: 'gmv', required: true }],
  gold: { kind: 'caliber', workflow_id: 'wf-week', version: 2, node_id: 'card', inputs: { week: '{{ schedule.last_week_start }}' } },
  gold_source: null, tags: [], version: 4,
  upgrade: { pinned: 2, latest: 3, retired: false, pending: true, message: '口径已升版到 v3，待处置', policy: null, versions: [2] },
}
const CASE_ASK = { ...CASE_SQL, id: 'case-3', seq: 3, question: '最近怎么样？', expectations: [{ id: 'e1', kind: 'ask', required: true }],
  gold: null, gold_source: null, tags: ['问法含糊'], version: 1 }
const CALIBERS = [
  { workflow_id: 'wf-week', workflow_name: '周销售', version: 3, level: 'governed', node_id: 'card', card: '周销售口径', caliber: '周销售',
    metric_id: 'gmv', name: '销售额', unit: '元', reusable: true, reason: null },
  { workflow_id: 'wf-mix', workflow_name: '混合看板', version: 1, level: 'published', node_id: 'card', card: '混合口径', caliber: '混合',
    metric_id: 'ratio', name: '占比', unit: '', reusable: false, reason: '口径卡读的不是查库的格' },
]
const VERSION_WEEK = { workflow_id: 'wf-week', version: 3, graph: { nodes: [{ id: 'in', type: 'input', position: { x: 0, y: 0 },
  data: { label: '入口', config: { fields: [{ name: 'week', required: true, description: '周一的日期' }] } } }], edges: [] } }
const PREVIEW_OK = { status: 'ok', reason: null, code: null, columns: ['地区', 'gmv'], rows: [['华东', 330], ['华南', 410]], row_count: 2,
  truncated: false, shown: 2, run_id: 'run-preview-1', rendered: { sql: 'SELECT region AS 地区, SUM(amount) AS gmv FROM orders GROUP BY region' } }
const MASKED_REASON = '期望 SQL 的结果里「手机号」取自数据源设了遮罩的列：期望答案不能用遮罩列（评测页会显示期望值）。请去掉这几列'
const PREVIEW_MASKED = { status: 'refused', reason: MASKED_REASON, code: 'eval_gold_masked', columns: [], rows: [], row_count: null,
  truncated: false, run_id: 'run-preview-2', rendered: {} }
const TRIAL_OK = { status: 'ok', as_of: iso(NOW), reason: null, code: null, run_ids: ['run-trial-1'], rendered: {},
  sets: [{ run_id: 'run-trial-1', snapshot: 'snap-1', version: null, values: { e1: { kind: 'scalar', value: 410, column: 'gmv', row: 1, dims: { 地区: '华南' } } } }] }
const CHAT_RUN = { id: 'run-chat-1', workflow_id: null, workflow_name: '问数据', status: 'succeeded', input: { question: '上周华东的订单数？' },
  output: {}, error: null, usage: {}, run_class: 'exploratory', version: null, origin_kind: 'chat', origin: {}, schedule_id: null,
  created_at: iso(NOW - 5 * H), started_at: iso(NOW - 5 * H), finished_at: iso(NOW - 5 * H + 3000) }
const QUERIES = [
  { artifact: 'q-1', node_id: 'ag', source: '门店库', source_id: 'ds-shop', sql: 'SELECT COUNT(*) AS orders FROM orders', columns: ['orders'],
    row_count: 1, truncated: false, masked: false },
  { artifact: 'q-2', node_id: 'ag', source: '门店库', source_id: 'ds-shop', sql: 'SELECT phone FROM customers', columns: ['phone'],
    row_count: 5, truncated: false, masked: true },
]
const VARIANT = { assistant: { provider: null, model: null }, writer: { provider: null, model: null }, judge: false,
  catalog: { mode: 'live', snapshot: null }, repeats: 1, label: null }
const SUMMARY = {
  scorer_version: 1, items: 3, done: 3, cases: 3, verdicts: { pass: 2, fail: 1, ungradable: 0, skipped: 0 }, not_run: 0,
  accuracy: RATE(2, 3), traced_accuracy: RATE(1, 3), outcomes: { traced: 1, untraced: 1, wrong: 1 }, text: {},
  paths: { ran: 2, asked: 1 }, tiers: { formal: 0, degraded: 0, withheld: 0, explore: 2 },
  review: { verdicts: { ok: 2 }, severity: {}, retries: 0, slow: 0 }, judge: { on: false, claims: null },
  looks: { model: 2, system: 1, per_case: 1, limit_hit: 0 }, calls: { assistant: 5, per_case: 1.67 },
  timings: { total_ms: { p50: 41000, p90: 60000, n: 3 } }, usage: { assistant: { cost_usd: 0.03, calls: 5, input_tokens: 9000, output_tokens: 700 },
    writer: { cost_usd: 0.012, calls: 2 }, judge: { cost_usd: 0, calls: 0 }, gold: { cost_usd: 0, calls: 0 } },
  cost_usd: 0.042, unpriced_calls: 0, per_case_cost_usd: 0.014,
  citations: { numbers: 4, numbers_cited: 3, uncited_numbers: 1, unresolved: 0, unknown_entities: 0, rate: 0.75 },
  e1: { ...RATE(1, 2), aspects: { time: 1 } }, pass_rates: {},
  baseline: { id: 'ev-old', pinned: false, created_at: iso(NOW - 48 * H), fingerprint_diff: [{ key: 'writer_model', label: '写作模型', before: 'model-a', after: 'model-b' }],
    counts: { regressed: 1, improved: 1, same: 1, incomparable: 0 }, causes: { data: 0, method: 1, params: 1, path: 0, unknown: 0 } },
}
const EVAL_DONE = {
  id: 'ev-1', suite_id: 'suite-a', status: 'succeeded', trigger: 'manual', schedule_id: null, variant: VARIANT,
  resolved_models: { assistant: 'model-a', writer: null, judge: false }, variant_text: '助手 model-a · 写作 各节点自己的 · 当前目录 · 结论裁判关 · 每题 1 次',
  catalog_snapshot: 'snap-live', as_of: iso(NOW - 2 * H), repeats: 1, fingerprint: {}, pair_id: null, arm: null, baseline_id: 'ev-old',
  pinned: false, budget: BUDGET, counts: { total: 3, done: 3, passed: 2, failed: 1 }, usage: {}, summary: SUMMARY, stop_reason: null,
  error: null, created_by: '检查员甲', created_at: iso(NOW - 2 * H), started_at: iso(NOW - 2 * H), finished_at: iso(NOW - H),
  cleaned_at: null, running: false, current: [],
  actions: { stop: false, retry: false, rescore: true, pin: true, unpin: false, open: 0, cleaned: false },
}
const RESULTS = [
  { id: 'r-1', eval_run_id: 'ev-1', seq: 1, case_id: 'case-1', case_seq: 1, case_hash: 'h1', repeat: 0, arm: null, status: 'ok',
    run_id: 'run-answer-1', gold_run_ids: ['run-gold-1'], gold: { status: 'ok' }, path: 'ran', verdict: 'pass',
    scores: [{ id: 'e1', kind: 'scalar', required: true, outcome: 'traced', expected: 330, got: 330 }], traced: true, review: null, tier: null,
    judge: null, looks: { model: 1, system: 1 }, calls: 2, timings: { total_ms: 41000 }, usage: {}, sql_match: { status: 'proven', aspects: [] },
    details_artifact: 'd-1', current: null, error: null, started_at: iso(NOW - 2 * H), finished_at: iso(NOW - H), question: CASE_SQL.question,
    cost: { cost_usd: 0.014, unpriced_calls: 0 } },
  { id: 'r-2', eval_run_id: 'ev-1', seq: 2, case_id: 'case-2', case_seq: 2, case_hash: 'h2', repeat: 0, arm: null, status: 'ok',
    run_id: 'run-answer-2', gold_run_ids: ['run-gold-2'], gold: { status: 'ok' }, path: 'ran', verdict: 'fail',
    scores: [{ id: 'e1', kind: 'scalar', required: true, outcome: 'wrong', expected: 330, got: 310, reason: '答案里的数和期望对不上' }], traced: false,
    review: null, tier: null, judge: null, looks: { model: 1, system: 0 }, calls: 2, timings: { total_ms: 60000 }, usage: {},
    sql_match: { status: 'related', aspects: ['time'] }, details_artifact: 'd-2', current: null, error: '答案里的数和期望对不上',
    started_at: iso(NOW - 2 * H), finished_at: iso(NOW - H), question: CASE_CAL.question, cost: { cost_usd: 0.02, unpriced_calls: 0 } },
  { id: 'r-3', eval_run_id: 'ev-1', seq: 3, case_id: 'case-3', case_seq: 3, case_hash: 'h3', repeat: 0, arm: null, status: 'ok',
    run_id: null, gold_run_ids: [], gold: null, path: 'asked', verdict: 'pass', scores: [{ id: 'e1', kind: 'ask', required: true, outcome: 'traced' }],
    traced: true, review: null, tier: null, judge: null, looks: { model: 0, system: 0 }, calls: 1, timings: { total_ms: 9000 }, usage: {},
    sql_match: null, details_artifact: 'd-3', current: null, error: null, started_at: iso(NOW - 2 * H), finished_at: iso(NOW - H),
    question: CASE_ASK.question, cost: { cost_usd: 0.008, unpriced_calls: 0 } },
]
const EVAL_OLD = { ...EVAL_DONE, id: 'ev-old', created_at: iso(NOW - 48 * H), summary: { ...SUMMARY, accuracy: RATE(1, 3), baseline: undefined } }
const COMPARE = {
  base: EVAL_OLD, cleaned: false,
  fingerprint_diff: [{ key: 'writer_model', label: '写作模型', before: 'model-a', after: 'model-b' }, { key: 'schemas', label: '表结构' }],
  transitions: { counts: { regressed: 1, improved: 1, same: 1, incomparable: 0 }, causes: { data: 0, method: 1, params: 1, path: 0, unknown: 0 },
    items: [{ case_id: 'case-2', case_seq: 2, before: 'pass', after: 'fail', transition: 'regressed', cause: 'method' },
      { case_id: 'case-1', case_seq: 1, before: 'fail', after: 'pass', transition: 'improved', cause: 'params' },
      { case_id: 'case-3', case_seq: 3, before: 'pass', after: 'pass', transition: 'same' }] },
}
const PAIR_A = { ...EVAL_DONE, id: 'ev-pa', pair_id: 'ev-pa', arm: '当前目录', trigger: 'pair' }
const PAIR_B = { ...EVAL_DONE, id: 'ev-pb', pair_id: 'ev-pa', arm: '关目录', trigger: 'pair' }
const PAIR = { pair_id: 'ev-pa', evaluations: [PAIR_A, PAIR_B], cleaned: false,
  table: { arms: [{ arm: '当前目录', accuracy: RATE(3, 4) }, { arm: '关目录', accuracy: RATE(1, 4) }],
    pairs: [{ a: '当前目录', b: '关目录', both: 1, neither: 1, only_a: 2, only_b: 0, excluded: 0, p_value: 0.5, note: '这批题上看不出差别' }] } }
const ESTIMATE = (arms) => ({
  arms: arms.map((a, i) => ({ label: arms.length > 1 ? a : null, variant: VARIANT, models: { assistant: 'model-a', writer: null, judge: false },
    text: `助手 model-a · 写作 各节点自己的 · ${a} · 结论裁判关 · 每题 ${arms.length > 1 ? 3 : 1} 次` })),
  cases: 3, repeats: arms.length > 1 ? 3 : 1, estimate: { items: 3 * (arms.length > 1 ? 3 : 1) * arms.length, per_item_usd: 0.014,
    total_usd: 0.042 * (arms.length > 1 ? 3 : 1) * arms.length, basis: 'ev-1', note: '按这个题集上一次评测的每题平均花费估算' },
  budget: BUDGET, parallel: 2, spend: { usd: 0.37, calls: 12, unpriced_calls: 0, daily_max_usd: 5 },
})
const SIGNALS = {
  window: { since: iso(NOW - 30 * 24 * H), until: iso(NOW), days: 30 },
  criteria: { e1: [{ band: 'route', at_least: 0.3, text: '等价命中率 ≥ 30%：值得按已治理的口径走' }, { band: 'normalize', at_least: 0.1, text: '10%–30%：先加强规范化' },
    { band: 'reconcile', at_least: 0, text: '< 10%：先只做口径对账' }], e3: [{ band: 'worth', at_least: 0.2, text: '问题重复度 ≥ 20%：沉淀有价值' },
    { band: 'not_yet', at_least: 0, text: '< 20%：反复问的问题还不多' }], note: '参考线出自口径账本的设计文档，只作参考，不卡门' },
  current: { since: iso(NOW - 30 * 24 * H), until: iso(NOW), e1: { rate: 0.35, n: 20, queries: 31, band: 'route' }, e3: { share: 0.12, n: 50, lower_bound: 0.04, band: 'not_yet' } },
  trend: [0, 1, 2].map((i) => ({ since: iso(NOW - (3 - i) * 30 * 24 * H), until: iso(NOW - (2 - i) * 30 * 24 * H),
    e1: { rate: [0.1, 0.2, 0.35][i], n: 20, queries: 30, band: ['normalize', 'normalize', 'route'][i] }, e3: { share: 0.12, n: 50, lower_bound: 0.04, band: 'not_yet' } })),
  clusters: [{ count: 4, representative: '上周华东的销售额是多少？', first_at: iso(NOW - 9 * 24 * H), last_at: iso(NOW - 2 * H),
    members: [{ text: '上周华东的销售额是多少？', at: iso(NOW - 2 * H), run_id: 'run-chat-1', turn_id: 't1' }], run_id: 'run-chat-1' },
  { count: 3, representative: '本月退款率？', first_at: iso(NOW - 5 * 24 * H), last_at: iso(NOW - 3 * H), members: [], run_id: null }],
  questions: 50, threshold: 3, embedder: 'local-hash',
}
const LEDGER = { window: { since: SIGNALS.window.since, until: null, origins: null },
  e1: { runs: 12, queries: 31, status: { proven: 7, composed: 1, related: 9, none: 11, skipped: 3 }, with_candidates: 20, rates: { proven: 0.35 },
    proven_via: {}, unproven_reasons: { '参数配上了，但和口径不等价': 5 }, diff_aspects: { time: 4, filter: 2, aggregate: 0, join: 0, other: 1, parse: 0 },
    labels: {}, whole_equivalent: 3 },
  e3: { chat_runs: 40, runs_with_queries: 35, shapes: 22, repeated_shapes: 2, runs_in_repeated_shapes: 6, repeated_share: 0.04,
    threshold: { runs: 3, conversations: 2 }, questions: 50, clusters: 30, questions_in_repeated_clusters: 6, cluster_share: 0.12, embedder: 'local-hash' } }
const SNAPSHOTS = [
  { id: 'snap-live', origin: 'live', status: 'succeeded', artifact_id: 'art-1', sources: [{ id: 'ds-shop', name: '门店库' }], tables: null,
    draft: null, estimate: null, usage: {}, progress: {}, error: null, created_by: '检查员甲', created_at: iso(NOW - 2 * H), started_at: null,
    finished_at: iso(NOW - 2 * H), view: { mode: 'snapshot', statuses: 'all', snapshot: 'art-1' } },
  { id: 'snap-shadow', origin: 'shadow', status: 'running', artifact_id: null, sources: [{ id: 'ds-shop', name: '门店库' }], tables: { 'ds-shop': ['orders'] },
    draft: { model_id: 'model-draft', strip_llm: true }, estimate: null, usage: { calls: 1, cost_usd: 0.01 },
    progress: { tables_total: 2, tables_done: 1, failures: {} }, error: null, created_by: '检查员甲', created_at: iso(NOW - 10 * 60e3),
    started_at: iso(NOW - 10 * 60e3), finished_at: null, view: null },
]
const SHADOW_PLAN = { sources: [{ id: 'ds-shop', name: '门店库', tables: ['orders', 'refunds'], unknown: ['nope'] }],
  estimate: { tables: 2, columns: 12, calls: 1, input_tokens: 3200, output_tokens: 900, model: 'model-draft', cost_usd: 0.02, unpriced: false,
    note: '按经验估：不含思考' }, draft: {} }
const EVAL_RUN = { id: 'run-eval-ans', workflow_id: null, workflow_name: '评测：门店问数据回归·第 2 题', status: 'succeeded',
  input: { question: CASE_CAL.question }, output: { answer: '上周销售额 310 元' }, error: null, usage: {}, run_class: 'exploratory', version: null,
  origin_kind: 'eval', origin: { suite_id: 'suite-a', eval_run_id: 'ev-1', case_id: 'case-2', role: 'answer', repeat: 0 }, schedule_id: null,
  created_at: iso(NOW - 90 * 60e3), started_at: iso(NOW - 90 * 60e3), finished_at: iso(NOW - 89 * 60e3) }
const RUN_GRAPH = { nodes: [{ id: 'out', type: 'output', position: { x: 0, y: 0 }, data: { label: '成果', config: {} } }], edges: [] }
/** 已发布的工作流（工作流题要固定一个已发布版本） */
const WF_WEEK = { id: 'wf-week', name: '周销售', description: '', graph: VERSION_WEEK.graph, tags: [], version: 4, is_template: false,
  status: 'published', published_version: 3, published_level: 'governed', created_at: iso(NOW - 30 * 24 * H), updated_at: iso(NOW - 24 * H) }
const BUILDER = { mode: 'local', username: 'jiang', display_name: '检查员甲', role: 'builder', groups: [], verified: true }
const USER = { mode: 'local', username: 'li', display_name: '使用者乙', role: 'user', groups: [], verified: true }

function freshState(patch = {}) {
  return {
    suites: [clone(SUITE), clone(SUITE_B)], cases: { 'suite-a': [clone(CASE_SQL), clone(CASE_CAL), clone(CASE_ASK)], 'suite-b': [] },
    evaluations: { 'ev-1': clone(EVAL_DONE), 'ev-old': clone(EVAL_OLD), 'ev-pa': clone(PAIR_A), 'ev-pb': clone(PAIR_B) },
    writes: [], me: null, eval: { max_parallel: 2, per_case_timeout_s: 900, per_run_max_usd: 2, daily_max_usd: 5, keep_runs: 20, judge: false },
    ...patch,
  }
}

/** 一个请求怎么答：返回 {status, json}；null 表示放行（GET）或拦下（写请求） */
function reply(state, method, path, url, body) {
  const write = (key) => { state.writes.push({ key: `${method} ${key ?? path}`, body }) }
  if (state.override) {
    const out = state.override(method, path, body, url)
    if (out) return out
  }
  if (path === '/auth/me' && state.me) return { json: state.me }
  if (method === 'GET' && path === '/datasources') return { json: [DS] }
  if (method === 'GET' && path === '/evals/suites') return { json: { suites: state.suites } }
  if (method === 'POST' && path === '/evals/suites') {
    write()
    const made = { ...clone(SUITE_B), id: 'suite-new', name: body.name, version: 1 }
    state.suites.push(made)
    state.cases['suite-new'] = []
    return { status: 201, json: made }
  }
  if (method === 'POST' && path === '/evals/suites/import/preview') {
    write()
    return { json: { suite: { name: '导入的题集' }, cases: [{ seq: 1, question: '问法一', ok: true, reason: null, code: null },
      { seq: 2, question: '问法二', ok: false, reason: '数据源「另一个库」在这里没有同名的', code: 'eval_import_invalid' }],
      sources: { 门店库: 'ds-shop', 另一个库: null }, workflows: {}, importable: 1, skipped: 1 } }
  }
  if (method === 'POST' && path === '/evals/suites/import') {
    write()
    const made = { ...clone(SUITE_B), id: 'suite-imp', name: body.name ?? '导入的题集' }
    state.suites.push(made)
    state.cases['suite-imp'] = []
    return { status: 201, json: { suite: { ...made, cases: [], pending_upgrades: 0 }, imported: 1, skipped: [{ seq: 2, ok: false, reason: '没对上' }] } }
  }
  let m = path.match(/^\/evals\/suites\/([^/]+)(\/.*)?$/)
  if (m) {
    const id = m[1]
    const rest = m[2] ?? ''
    const suite = state.suites.find((s) => s.id === id)
    if (!suite) return { status: 404, json: { detail: '题集不存在，可能已被删除', code: 'eval_suite_not_found' } }
    const cases = state.cases[id] ?? []
    const detail = () => ({ ...suite, cases, pending_upgrades: cases.filter((c) => c.upgrade?.pending).length })
    if (method === 'GET' && !rest) return { json: detail() }
    if (method === 'PATCH' && !rest) {
      write()
      Object.assign(suite, body, { version: suite.version + 1 })
      return { json: detail() }
    }
    if (method === 'GET' && rest === '/evaluations') {
      return { json: { evaluations: Object.values(state.evaluations).filter((e) => e.suite_id === id) } }
    }
    if (method === 'GET' && rest === '/export') return { raw: JSON.stringify({ format: 'agentlab.eval_suite', cases: [] }), headers: {
      'Content-Disposition': `attachment; filename*=UTF-8''${encodeURIComponent('门店问数据回归.json')}` } }
    if (method === 'POST' && rest === '/cases/preview') {
      write()
      return { json: String(body.sql).includes('phone') ? PREVIEW_MASKED : PREVIEW_OK }
    }
    if (method === 'POST' && rest === '/cases/trial') { write(); return { json: { trial: TRIAL_OK } } }
    if (method === 'POST' && (rest === '/cases' || rest === '/cases/from-run')) {
      write()
      const made = { ...clone(CASE_SQL), id: `case-new-${cases.length + 1}`, seq: cases.length + 1, question: body.question ?? '', expectations: body.expectations ?? [],
        gold: body.gold ?? null, upgrade: null }
      cases.push(made)
      return { status: 201, json: { case: made, trial: TRIAL_OK } }
    }
    const c = rest.match(/^\/cases\/([^/]+)(\/.*)?$/)
    if (c) {
      const row = cases.find((x) => x.id === c[1])
      if (!row) return { status: 404, json: { detail: '题目不存在', code: 'eval_case_not_found' } }
      if (method === 'PATCH' && !c[2]) {
        write()
        if (body.version !== row.version) return { status: 409, json: { detail: '版本对不上' } }
        Object.assign(row, body, { version: row.version + 1 })
        return { json: { case: row, trial: null } }
      }
      if (method === 'POST' && c[2] === '/trial') { write(); return { json: { trial: TRIAL_OK } } }
      if (method === 'GET' && c[2] === '/upgrade') {
        return { json: { upgrade: row.upgrade, choices: { recompute: '改为固定新版本（历史在这里断开）', dual: '新旧两版各算一份期望',
          incomparable: '旧题归档，按新版本另建新题' } } }
      }
      if (method === 'POST' && c[2] === '/upgrade') {
        write()
        row.upgrade = { ...row.upgrade, pending: false, message: null, policy: body.policy }
        row.version += 1
        return { json: { case: row, new_case: null, trial: TRIAL_OK } }
      }
    }
    if (method === 'POST' && rest === '/evaluations/estimate') {
      write()
      const arms = body.preset ? (body.preset === 'catalog_reviewed' ? ['当前目录', '只用已核实和已确认的项', '关目录'] : ['当前目录', '关目录'])
        : body.arms ? body.arms.map((a, i) => a.label || 'ABC'[i]) : ['当前目录']
      return { json: ESTIMATE(arms) }
    }
    if (method === 'POST' && rest === '/evaluations') {
      write()
      const run = { ...clone(EVAL_DONE), id: 'ev-new', status: 'running', running: true, summary: {}, counts: { total: 3, done: 1 },
        current: [{ case_seq: 2, repeat: 0, arm: null, doing: '正在建图' }],
        actions: { stop: true, retry: false, rescore: false, pin: false, unpin: false, open: 2, cleaned: false } }
      state.evaluations['ev-new'] = run
      return { status: 202, json: { evaluations: [run], pair_id: null } }
    }
  }
  m = path.match(/^\/evals\/evaluations\/([^/]+)(\/.*)?$/)
  if (m) {
    const ev = state.evaluations[m[1]]
    const rest = m[2] ?? ''
    if (!ev) return { status: 404, json: { detail: '评测不存在', code: 'eval_evaluation_not_found' } }
    if (method === 'GET' && !rest) return { json: ev }
    if (method === 'GET' && rest === '/results') return { json: { evaluation_id: ev.id, cleaned: false, results: ev.id === 'ev-new' ? RESULTS.slice(0, 1) : RESULTS } }
    if (method === 'GET' && rest === '/compare') {
      state.compareBases = [...(state.compareBases ?? []), url.searchParams.get('base')]
      return { json: COMPARE }
    }
    if (method === 'POST' && rest === '/stop') {
      write()
      Object.assign(ev, { status: 'cancelled', running: false, current: [], stop_reason: null,
        actions: { stop: false, retry: true, rescore: true, pin: false, unpin: false, open: 2, cleaned: false } })
      return { json: ev }
    }
    if (method === 'POST' && rest === '/retry') {
      write()
      Object.assign(ev, { status: 'running', running: true, actions: { ...ev.actions, stop: true, retry: false } })
      return { status: 202, json: { evaluations: [ev] } }
    }
    if (method === 'POST' && rest === '/rescore') { write(); return { json: { ...ev, changed: 1 } } }
    if (method === 'POST' && rest === '/pin') {
      write()
      Object.assign(ev, { pinned: body.pinned, actions: { ...ev.actions, pin: !body.pinned, unpin: body.pinned } })
      return { json: ev }
    }
  }
  m = path.match(/^\/evals\/evaluations\/([^/]+)\/results\/([^/]+)$/)
  if (method === 'GET' && m) {
    const r = RESULTS.find((x) => x.id === m[2])
    return r ? { json: { ...r, details: { turn: { attempts: 1, reason: null, reply: null }, answer: { text: `答案原文：上周销售额 ${r.scores[0]?.got ?? ''} 元` } } } }
      : { status: 404, json: { detail: '这一题的结果不存在' } }
  }
  if (method === 'POST' && path === '/evals/suites/suite-a/cleanup') { write(); return { json: { runs: 2, results: 6, deleted_runs: 8, files: 3 } } }
  if (method === 'GET' && path === '/workflows') return { json: [WF_WEEK] }
  if (method === 'GET' && path === '/evals/pairs/ev-pa') return { json: PAIR }
  if (method === 'GET' && path === '/evals/calibers') return { json: { calibers: CALIBERS } }
  if (method === 'GET' && path === '/workflows/wf-week/versions/3') return { json: VERSION_WEEK }
  if (method === 'GET' && path === '/evals/runs/run-chat-1/queries') return { json: { queries: QUERIES } }
  if (method === 'GET' && path === '/evals/signals') {
    state.signalDays = [...(state.signalDays ?? []), url.searchParams.get('days')]
    return { json: SIGNALS }
  }
  if (method === 'GET' && path === '/ledger/stats') {
    state.ledgerSince = url.searchParams.get('since')
    return { json: LEDGER }
  }
  // 「提取模板」之后进画布：画布打开时要的几样（只读的校验、变量分析，不写库）
  if (method === 'GET' && path === '/workflows/wf-drafted') {
    return { json: { id: 'wf-drafted', name: '上周华东的销售额（草稿）', description: '', graph: { nodes: [], edges: [] }, status: 'draft',
      published_version: null, created_at: iso(NOW), updated_at: iso(NOW) } }
  }
  if (method === 'POST' && path === '/workflows/validate') return { json: { ok: true, issues: [] } }
  if (method === 'POST' && path === '/workflows/variables') return { json: { variables: [], issues: [] } }
  if (method === 'POST' && path === '/copilot/from-run') {
    write()
    return { json: { workflow_id: 'wf-drafted', name: '上周华东的销售额（草稿）', nodes: 3, dropped_nodes: 0 } }
  }
  if (method === 'GET' && path === '/evals/catalog-snapshots') return { json: { snapshots: SNAPSHOTS } }
  if (method === 'POST' && path === '/evals/catalog-snapshots') { write(); return { status: 201, json: SNAPSHOTS[0] } }
  if (method === 'POST' && path === '/evals/catalog-snapshots/shadow/estimate') { write(); return { json: SHADOW_PLAN } }
  if (method === 'POST' && path === '/evals/catalog-snapshots/shadow') { write(); return { status: 202, json: SNAPSHOTS[1] } }
  if (method === 'POST' && path === '/evals/catalog-snapshots/snap-shadow/stop') { write(); return { json: { ...SNAPSHOTS[1], status: 'cancelled' } } }
  if (method === 'GET' && path === '/settings/eval/spend') return { json: { date: '2026-10-16', usd: 0.37, calls: 12, unpriced_calls: 0, daily_max_usd: 5 } }
  if (method === 'GET' && path === '/settings' && state.settings) return { json: { ...state.settings, eval: state.eval } }
  if (method === 'PUT' && path === '/settings') {
    write()
    if (body?.values?.eval) state.eval = { ...state.eval, ...body.values.eval }
    return { json: { ...(state.settings ?? {}), eval: state.eval } }
  }
  if (method === 'GET' && path === '/runs' && url.searchParams.get('origin_kind') === 'chat') return { json: [CHAT_RUN] }
  if (state.runs && method === 'GET' && path === '/runs') return { json: state.runs }
  if (state.runs && method === 'GET' && path.startsWith('/runs/')) {
    const run = state.runs.find((r) => path === `/runs/${r.id}`)
    if (run) return { json: run }
    const rm = path.match(/^\/runs\/([^/]+)\/(events|graph|artifacts)$/)
    const of = rm && state.runs.find((r) => r.id === rm[1])
    if (of && rm[2] === 'events') {
      const t0 = Date.parse(of.started_at) / 1000
      return { json: [{ seq: 1, type: 'run.started', node_id: null, ts: t0, data: { nodes: 1 } },
        { seq: 2, type: 'run.finished', node_id: null, ts: t0 + 3, data: { output: of.output, usage: {} } }] }
    }
    if (of && rm[2] === 'graph') return { json: { graph: RUN_GRAPH, workflow_id: null, version: null } }
    if (of) return { json: [] }
  }
  if (state.runs && method === 'GET' && path === '/approvals') return { json: [] }
  return null
}

const browser = await chromium.launch({ executablePath: CHROME })
let realSettings = null

async function open(path, { state = freshState(), width = 1280, height = 900 } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height }, acceptDownloads: true })
  opened.add(ctx)
  ctx.setDefaultTimeout(6000)
  ctx.setDefaultNavigationTimeout(30000)
  await ctx.addInitScript(() => {
    try { localStorage.setItem('agentlab_actor', '检查员甲') } catch { /* noop */ }
  })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('dialog', (d) => { errors.push(`原生对话框：${d.message()}`); void d.dismiss() })
  await page.route((u) => new URL(u).pathname.startsWith('/api/'), async (route) => {
    const r = route.request()
    const url = new URL(r.url())
    const method = r.method()
    const p = decodeURIComponent(url.pathname.replace(/^\/api/, ''))
    let body = null
    try { body = r.postDataJSON() } catch { body = null }
    if (state.settings === undefined && p === '/settings' && method === 'GET') {
      realSettings ??= await route.fetch().then((x) => x.json()).catch(() => ({}))
      state.settings = realSettings
    }
    const out = reply(state, method, p, url, body)
    if (out) {
      if (out.raw !== undefined) return route.fulfill({ status: out.status ?? 200, contentType: 'application/json', body: out.raw, headers: out.headers })
      return route.fulfill({ status: out.status ?? 200, contentType: 'application/json', body: out.json === undefined ? '' : JSON.stringify(out.json) })
    }
    if (method === 'GET') return route.continue()
    leaked.push(`${method} ${p}`)
    return route.fulfill({ status: 503, body: '' })
  })
  await page.goto(`${WEB}${path}`, { waitUntil: 'networkidle' })
  return { ctx, page, errors, state }
}
const writes = (state, re) => state.writes.filter((w) => re.test(w.key))
const lastWrite = (state, re) => writes(state, re).at(-1)
const text = (page, sel) => page.locator(sel).first().innerText()
const hasHumanValue = (exps) => (exps ?? []).some((e) => ['value', 'values', 'expected', 'answer', 'rows', 'number'].some((k) => k in e))

// ---------------------------------------------------------------- 导航与权限

await section('导航：评测在设置前面、⌥0；使用者看不到', async () => {
  const builder = await open('/chat', { state: freshState({ me: BUILDER }) })
  const nav = builder.page.locator('nav[aria-label="主导航"]')
  await nav.waitFor()
  const labels = await nav.locator('li > a[aria-keyshortcuts] > span:not([aria-hidden])').allInnerTexts()
  check('搭建者：导航里有「评测」，紧挨着排在「设置」前面', labels.at(-1) === '设置' && labels.at(-2) === '评测', labels.join(','))
  const key = await nav.locator('a[href="/evals"]').getAttribute('aria-keyshortcuts')
  check('「评测」的快捷键是 ⌥0', key === 'Alt+0', key)
  await builder.page.locator('main').click({ position: { x: 600, y: 300 } }).catch(() => {})
  await builder.page.keyboard.press('Alt+Digit0')
  check('⌥0 切到评测页', await builder.page.waitForURL(/\/evals(\?|$)/, { timeout: 5000 }).then(() => true, () => false), builder.page.url())

  const user = await open('/apps', { state: freshState({ me: USER }) })
  const uNav = user.page.locator('nav[aria-label="主导航"]')
  await uNav.waitFor()
  check('使用者：导航里没有「评测」', await uNav.locator('a[href="/evals"]').count() === 0,
    (await uNav.locator('li > a[aria-keyshortcuts] > span:not([aria-hidden])').allInnerTexts()).join(','))
  await user.page.goto(`${WEB}/evals/suites/suite-a`, { waitUntil: 'networkidle' })
  check('使用者手输评测地址：送回应用首页', await user.page.waitForURL(/\/apps(\?|$)/, { timeout: 5000 }).then(() => true, () => false), user.page.url())
  await user.page.keyboard.press('Alt+Digit0')
  await sleep(300)
  check('使用者按 ⌥0 不会进评测页', !new URL(user.page.url()).pathname.startsWith('/evals'), user.page.url())
  check('没有页面报错', !builder.errors.length && !user.errors.length, [...builder.errors, ...user.errors].join(' ｜ '))
})

// ---------------------------------------------------------------- 题集列表

await section('题集列表：最近一次、趋势、定时；新建、导入', async () => {
  const { page, state, errors } = await open('/evals')
  const row = page.locator('[data-eval-suite="suite-a"]')
  await row.waitFor()
  const t = await row.innerText()
  check('写题数、最近一次的准确率（服务端给的 66.7%）', t.includes('3 道题') && t.includes('66.7%'), t)
  check('趋势画了服务端给的两次', await row.locator('[data-eval-trend="2"]').count() === 1)
  check('定时：列出「每周回归」，链到那条定时任务', await row.locator('[data-eval-suite-schedule="sch-1"]').getAttribute('href') === '/schedules/sch-1')
  const add = page.locator('[data-eval-suite="suite-b"] [data-eval-suite-add-schedule]')
  check('没有定时的题集给「设定时」，带着题集去新建定时', (await add.getAttribute('href')) === '/schedules/new?target_kind=eval_suite&target_id=suite-b')
  check('最近一次链到评测详情', await row.locator('[data-eval-suite-latest] a').getAttribute('href') === '/evals/runs/ev-1')
  await shot(page, 'list')

  await page.locator('[data-evals-create]').click()
  const dialog = page.locator('[role="dialog"]')
  await dialog.locator('input').fill('新的问法回归')
  await dialog.getByRole('button', { name: '新建题集' }).click()
  check('新建题集：交上去的只有名称，建好进题集页', await until(() => new URL(page.url()).pathname === '/evals/suites/suite-new')
    && JSON.stringify(lastWrite(state, /^POST \/evals\/suites$/)?.body) === JSON.stringify({ name: '新的问法回归' }), JSON.stringify(lastWrite(state, /^POST \/evals\/suites$/)?.body))

  await page.goto(`${WEB}/evals`, { waitUntil: 'networkidle' })
  await page.locator('[data-evals-import]').click()
  await page.locator('[data-eval-import-file]').setInputFiles({ name: '题集.json', mimeType: 'application/json',
    buffer: Buffer.from(JSON.stringify({ format: 'agentlab.eval_suite', version: 1, suite: { name: '导入的题集' }, cases: [] })) })
  const plan = page.locator('[data-eval-import-plan]')
  await plan.waitFor()
  const pt = await plan.innerText()
  check('导入预览：能导入几道、跳过几道和原因、数据源对照照服务端写', pt.includes('能导入 1 道，跳过 1 道') && pt.includes('没有同名的')
    && pt.includes('这里没有同名的数据源'), pt)
  await page.locator('[data-eval-import-confirm]').click()
  check('导入：交上去的是文件内容和名称，导完进新题集', await until(() => new URL(page.url()).pathname === '/evals/suites/suite-imp')
    && lastWrite(state, /import$/)?.body?.content?.format === 'agentlab.eval_suite' && lastWrite(state, /import$/)?.body?.name === '导入的题集',
    JSON.stringify(lastWrite(state, /import$/)?.body))
  check('没有页面报错', !errors.length, errors.join(' ｜ '))
})

// ---------------------------------------------------------------- 题集详情

await section('题集详情：题目表、口径升版处置、启用开关、导出', async () => {
  const { page, state, errors } = await open('/evals/suites/suite-a')
  await page.locator('[data-eval-cases]').waitFor()
  check('三道题都在，写期望答案的来源', (await text(page, '[data-eval-case="case-1"]')).includes('SQL · 门店库')
    && (await text(page, '[data-eval-case="case-2"]')).includes('口径 · wf-week v2'))
  const banner = page.locator('[data-eval-upgrade-banner]')
  check('顶部提示有 1 道口径题待处置', (await banner.getAttribute('data-eval-upgrade-banner')) === '1' && (await banner.innerText()).includes('待处置'))
  check('那道题上写服务端的原话「口径已升版到 v3，待处置」', (await text(page, '[data-eval-case="case-2"] [data-eval-case-upgrade]')).includes('口径已升版到 v3，待处置'))
  await shot(page, 'suite')
  await page.locator('[data-eval-case="case-2"] [data-eval-case-dispose]').click()
  const confirm = page.locator('[data-eval-upgrade-confirm]')
  await page.locator('[data-eval-upgrade-policy]').first().waitFor()
  check('处置：三种都列出（叫法由服务端给），没有缺省、不选不能确认', await page.locator('[data-eval-upgrade-policy]').count() === 3
    && await confirm.isDisabled())
  await page.locator('[data-eval-upgrade-policy="dual"]').check()
  await confirm.click()
  await until(() => lastWrite(state, /upgrade$/))
  check('处置交上去的是选的那一种和读到的版本', JSON.stringify(lastWrite(state, /upgrade$/)?.body) === JSON.stringify({ policy: 'dual', version: 4 }),
    JSON.stringify(lastWrite(state, /upgrade$/)?.body))
  check('处置完提示消失', await until(async () => await page.locator('[data-eval-upgrade-banner]').count() === 0))

  await page.locator('[data-eval-case="case-3"] [data-eval-case-toggle]').click()
  await until(() => lastWrite(state, /^PATCH \/evals\/suites\/suite-a\/cases\/case-3$/))
  check('启用开关：只交启用和读到的版本', JSON.stringify(lastWrite(state, /cases\/case-3$/)?.body) === JSON.stringify({ enabled: false, version: 1 }),
    JSON.stringify(lastWrite(state, /cases\/case-3$/)?.body))

  const [download] = await Promise.all([page.waitForEvent('download', { timeout: 5000 }).catch(() => null), page.locator('[data-eval-suite-export]').click()])
  check('导出：下载服务端给的文件名', download?.suggestedFilename() === '门店问数据回归.json', download?.suggestedFilename())
  await page.locator('[data-eval-runs]').count()
  await page.getByRole('tab', { name: '评测记录' }).click()
  check('评测记录：历次评测链到详情', await page.locator('[data-eval-run-row="ev-1"]').getAttribute('href') === '/evals/runs/ev-1')
  await page.locator('[data-eval-suite-cleanup]').click()
  await page.locator('[role="dialog"]').getByRole('button', { name: '清理更早的明细' }).click()
  check('清理更早的明细：先确认、再发一次，提示清了几次', await until(() => writes(state, /cleanup$/).length === 1)
    && await until(async () => (await page.locator('[data-toast]').allInnerTexts()).some((x) => x.includes('已清理 2 次评测的明细'))))
  check('没有页面报错', !errors.length, errors.join(' ｜ '))
})

// ---------------------------------------------------------------- 加题

await section('加题：写 SQL 定期望、预览、点选格、试算、保存；遮罩列拒收', async () => {
  const { page, state, errors } = await open('/evals/suites/suite-b')
  await page.locator('[data-eval-add-case]').click()
  const editor = page.locator('[data-eval-case-editor="sql"]')
  await editor.waitFor()
  await page.locator('[data-eval-case-question]').fill('上周各地区的销售额？')
  await page.locator('[data-eval-case-source]').selectOption('ds-shop')
  check('没点选期望时保存按钮不可用', await page.locator('[data-eval-case-save]').isDisabled())
  await page.locator('[data-eval-case-sql]').fill('SELECT region AS 地区, phone FROM customers')
  await page.locator('[data-eval-case-preview]').click()
  const err = page.locator('#eval-case-sql-error')
  await err.waitFor()
  check('遮罩列：服务端的原话落在 SQL 那一格下面，结果一格都不显示', (await err.innerText()).includes('取自数据源设了遮罩的列')
    && await page.locator('[data-eval-preview]').count() === 0, await err.innerText())
  check('预览交上去的是数据源和 SQL', JSON.stringify(lastWrite(state, /preview$/)?.body) === JSON.stringify({ datasource_id: 'ds-shop', sql: 'SELECT region AS 地区, phone FROM customers' }))
  await page.locator('[data-eval-case-sql]').fill('SELECT region AS 地区, SUM(amount) AS gmv FROM orders GROUP BY region')
  await page.locator('[data-eval-case-preview]').click()
  await page.locator('[data-eval-preview]').waitFor()
  check('预览结果画出来，写共几行', (await page.locator('[data-eval-preview-count]').innerText()).includes('共 2 行'))
  await page.locator('[data-eval-preview-cell="1:gmv"]').click()
  check('点一格定「一个数」：「gmv」第 2 行', (await text(page, '[data-eval-case-main-text]')).includes('「gmv」第 2 行'))
  await page.locator('[data-eval-preview-where-col="地区"]').check()
  check('按这一行的维度找这一行：（地区=华南）', (await text(page, '[data-eval-case-main-text]')).includes('地区=华南'))
  await page.locator('[data-eval-case-trial]').click()
  await page.locator('[data-eval-trial="ok"]').waitFor()
  await shot(page, 'case-sql')
  check('试算通过：写算出的期望（服务端算的 410，带维度）', (await text(page, '[data-eval-trial="ok"]')).includes('410') && (await text(page, '[data-eval-trial="ok"]')).includes('华南'))
  const trialBody = lastWrite(state, /cases\/trial$/)?.body
  check('试算交上去的期望只写取哪一格（不带人填的数）', trialBody?.expectations?.[0]?.kind === 'scalar' && trialBody.expectations[0].column === 'gmv'
    && trialBody.expectations[0].where?.地区 === '华南' && !hasHumanValue(trialBody.expectations), JSON.stringify(trialBody?.expectations))
  await page.locator('[data-eval-case-save]').click()
  await until(() => lastWrite(state, /^POST \/evals\/suites\/suite-b\/cases$/))
  const saved = lastWrite(state, /^POST \/evals\/suites\/suite-b\/cases$/)?.body
  check('保存：问法、期望 SQL、数据源、期望都在请求体里，期望不带人填的数', saved?.question === '上周各地区的销售额？' && saved?.gold?.kind === 'sql'
    && saved.gold.datasource_id === 'ds-shop' && saved.gold.sql.includes('GROUP BY region') && saved.kind === 'ask' && !hasHumanValue(saved.expectations),
    JSON.stringify(saved))
  check('保存后编辑框关掉、题目进了列表', await until(async () => await page.locator('[data-eval-case-editor]').count() === 0
    && await page.locator('[data-eval-case-seq="1"]').count() === 1))
  check('没有页面报错', !errors.length, errors.join(' ｜ '))
})

await section('加题：选口径、从运行认定、期望反问', async () => {
  const { page, state, errors } = await open('/evals/suites/suite-b')
  await page.locator('[data-eval-add-case]').click()
  await page.locator('[data-eval-case-mode="caliber"]').click()
  await page.locator('[data-eval-calibers]').waitFor()
  const bad = page.locator('[data-eval-caliber="wf-mix@1:card:ratio"]')
  check('不能复用的口径照列、写原因、选不了', await bad.isDisabled() && (await text(page, '[data-eval-calibers]')).includes('口径卡读的不是查库的格'))
  await page.locator('[data-eval-case-question]').fill('上周的周销售额？')
  await page.locator('[data-eval-caliber="wf-week@3:card:gmv"]').check()
  await page.locator('[data-eval-caliber-input="week"]').fill('{{ schedule.last_week_start }}')
  await shot(page, 'case-caliber')
  await page.locator('[data-eval-case-save]').click()
  await until(() => lastWrite(state, /^POST \/evals\/suites\/suite-b\/cases$/))
  const cal = lastWrite(state, /^POST \/evals\/suites\/suite-b\/cases$/)?.body
  check('口径题：固定 wf-week@3 的口径卡，期望写指标、输入照填的模板', cal?.gold?.kind === 'caliber' && cal.gold.workflow_id === 'wf-week'
    && cal.gold.version === 3 && cal.gold.node_id === 'card' && cal.gold.inputs?.week === '{{ schedule.last_week_start }}'
    && cal.expectations?.[0]?.metric === 'gmv' && !hasHumanValue(cal.expectations), JSON.stringify(cal))

  await page.locator('[data-eval-add-case]').click()
  await page.locator('[data-eval-case-mode="from_run"]').click()
  await page.locator('[data-eval-from-run-recent]').selectOption('run-chat-1')
  await page.locator('[data-eval-run-query="q-1"]').waitFor()
  check('用到遮罩列的查询写明不能认定、没有「认定这一条」', (await text(page, '[data-eval-run-query="q-2"]')).includes('不能认定')
    && await page.locator('[data-eval-run-query="q-2"] [data-eval-run-query-pick]').count() === 0)
  await page.locator('[data-eval-run-query="q-1"] [data-eval-run-query-pick]').click()
  await page.locator('[data-eval-from-run-cell="orders"]').click()
  await page.locator('[data-eval-case-save]').click()
  await until(() => lastWrite(state, /from-run$/))
  const fr = lastWrite(state, /from-run$/)?.body
  check('从运行认定：交运行、那条查询和取哪一格（认定人由服务端记）', fr?.run_id === 'run-chat-1' && fr?.query === 'q-1'
    && fr?.expectations?.[0]?.column === 'orders' && !('confirmed_by' in (fr ?? {})), JSON.stringify(fr))

  await page.locator('[data-eval-add-case]').click()
  await page.locator('[data-eval-case-mode="special"]').click()
  await page.locator('[data-eval-special-kind="ask"]').check()
  await page.locator('[data-eval-case-question]').fill('最近怎么样？')
  await page.locator('[data-eval-case-save]').click()
  await until(() => writes(state, /^POST \/evals\/suites\/suite-b\/cases$/).length === 2)
  const ask = lastWrite(state, /^POST \/evals\/suites\/suite-b\/cases$/)?.body
  check('期望反问：期望只有「反问」，不带期望答案的来源', JSON.stringify(ask?.expectations) === JSON.stringify([{ kind: 'ask' }]) && ask?.gold === null,
    JSON.stringify(ask))
  await page.locator('[data-eval-add-case]').click()
  await page.locator('[data-eval-case-kind-pick="workflow"]').check()
  await page.locator('[data-eval-case-workflow]').selectOption('wf-week')
  await page.locator('[data-eval-case-input="week"]').fill('{{ schedule.last_week_start }}')
  await page.locator('[data-eval-case-source]').selectOption('ds-shop')
  await page.locator('[data-eval-case-sql]').fill('SELECT region AS 地区, SUM(amount) AS gmv FROM orders GROUP BY region')
  await page.locator('[data-eval-case-preview]').click()
  await page.locator('[data-eval-preview-cell="0:gmv"]').click()
  await page.locator('[data-eval-case-save]').click()
  await until(() => writes(state, /^POST \/evals\/suites\/suite-b\/cases$/).length === 3)
  const wf = lastWrite(state, /^POST \/evals\/suites\/suite-b\/cases$/)?.body
  check('工作流题：固定已发布的 v3、按入口字段填输入模板，期望照样由 SQL 算', wf?.kind === 'workflow' && wf.workflow_id === 'wf-week'
    && wf.workflow_version === 3 && wf.input_template?.week === '{{ schedule.last_week_start }}' && wf.gold?.kind === 'sql',
    JSON.stringify(wf))
  check('没有页面报错', !errors.length, errors.join(' ｜ '))
})

// ---------------------------------------------------------------- 发起评测

await section('发起评测：估算和发起同一份请求体；评一次、A/B、目录贡献对照', async () => {
  const { page, state, errors } = await open('/evals/suites/suite-a')
  await page.locator('[data-eval-suite-start]').click()
  await page.locator('[data-eval-estimate-items]').waitFor()
  const est = await text(page, '[data-eval-estimate]')
  check('估算：项数、按上一次估的花费、今天已花照服务端写', est.includes('共 3 项') && est.includes('$0.042') && est.includes('评测今天已花 $0.370'), est)
  await page.locator('[data-eval-start-mode="ab"]').click()
  await until(async () => (await page.locator('[data-eval-arm]').count()) === 2)
  await page.locator('#eval-arm-1-catalog').selectOption('off')
  await until(() => writes(state, /estimate$/).some((w) => w.body?.arms?.[1]?.catalog?.mode === 'off'))
  const ab = lastWrite(state, /estimate$/)?.body
  check('A/B：两臂各自的变体（第二臂关目录），每题 3 次', ab?.arms?.length === 2 && ab.arms[1].catalog.mode === 'off' && ab.arms[0].repeats === 3,
    JSON.stringify(ab))
  await page.locator('[data-eval-start-mode="catalog"]').click()
  await page.locator('[data-eval-reviewed-arm]').check()
  await until(() => writes(state, /estimate$/).some((w) => w.body?.preset === 'catalog_reviewed'))
  const preset = lastWrite(state, /estimate$/)?.body
  check('目录贡献对照：预设 catalog_reviewed，变体里不带目录（三臂由服务端定）', preset?.preset === 'catalog_reviewed' && !('catalog' in (preset.variant ?? {})),
    JSON.stringify(preset))
  await until(async () => (await page.locator('[data-eval-estimate-arm]').count()) === 3)
  await shot(page, 'start-catalog')
  check('估算写三臂', await page.locator('[data-eval-estimate-arm]').count() === 3)
  await page.locator('[data-eval-start-mode="single"]').click()
  await sleep(500)
  await page.locator('[data-eval-start-submit]').click()
  await until(() => lastWrite(state, /^POST \/evals\/suites\/suite-a\/evaluations$/))
  const started = lastWrite(state, /^POST \/evals\/suites\/suite-a\/evaluations$/)?.body
  const lastEst = writes(state, /estimate$/).at(-1)?.body
  check('发起交上去的就是最后一次估算的那份请求体', JSON.stringify(started) === JSON.stringify(lastEst), `${JSON.stringify(started)} vs ${JSON.stringify(lastEst)}`)
  check('发起后进这次评测的详情', await until(() => new URL(page.url()).pathname === '/evals/runs/ev-new'))
  check('没有页面报错', !errors.length, errors.join(' ｜ '))
})

// ---------------------------------------------------------------- 评测详情

await section('评测详情：进度、正在做什么、停止和重试、重评、设为基线', async () => {
  const state = freshState()
  state.evaluations['ev-new'] = { ...clone(EVAL_DONE), id: 'ev-new', status: 'running', running: true, summary: {}, counts: { total: 3, done: 1 },
    current: [{ case_seq: 2, repeat: 0, arm: null, doing: '正在建图' }],
    actions: { stop: true, retry: false, rescore: false, pin: false, unpin: false, open: 2, cleaned: false } }
  const { page, errors } = await open('/evals/runs/ev-new', { state })
  await page.locator('[data-eval-progress="1/3"]').waitFor()
  await shot(page, 'run-live')
  check('进度：已评 1 / 3 项，第 2 题正在建图', (await text(page, '[data-eval-run-head]')).includes('已评 1 / 3 项')
    && (await text(page, '[data-eval-current]')).includes('第 2 题：正在建图'))
  check('在跑：只给「停止」（重试、重评、设为基线照服务端的 actions 不摆）', await page.locator('[data-eval-stop]').count() === 1
    && await page.locator('[data-eval-retry]').count() === 0 && await page.locator('[data-eval-rescore]').count() === 0
    && await page.locator('[data-eval-pin]').count() === 0)
  await page.locator('[data-eval-stop]').click()
  await page.locator('[data-eval-retry]').waitFor()
  check('停止之后：服务端说能重试，给「重试没做完的」，悬停写几项', (await page.locator('[data-eval-retry]').getAttribute('title')).includes('2 项')
    && writes(state, /\/stop$/).length === 1)
  await page.locator('[data-eval-retry]').click()
  await until(() => writes(state, /\/retry$/).length === 1)
  check('重试：发了一次', writes(state, /\/retry$/).length === 1)

  await page.goto(`${WEB}/evals/runs/ev-1`, { waitUntil: 'networkidle' })
  await page.locator('[data-eval-overview]').waitFor()
  await shot(page, 'run-overview')
  const ov = await text(page, '[data-eval-overview]')
  check('总览：准确率、可追溯准确率、花费、逐题 E1 照总表写', (await text(page, '[data-eval-card="accuracy"]')).includes('66.7%')
    && (await text(page, '[data-eval-card="traced"]')).includes('33.3%') && (await text(page, '[data-eval-card="cost"]')).includes('$0.042')
    && (await text(page, '[data-eval-card="e1"]')).includes('50%'), ov.slice(0, 200))
  check('和上一次比：退步 1、进步 1，归因、写作模型变了', (await text(page, '[data-eval-section="baseline"]')).includes('退步 1 题 · 进步 1 题')
    && (await text(page, '[data-eval-section="baseline"]')).includes('写作模型'))
  check('分布：结论、作答路径、按角色的用量', ov.includes('通过 2 · 不通过 1') && ov.includes('运行完成 2 · 反问 1')
    && (await text(page, '[data-eval-usage-role="assistant"]')).includes('$0.030'))
  await page.locator('[data-eval-rescore]').click()
  await until(() => writes(state, /\/rescore$/).length === 1)
  check('重评：发了一次，提示有几项结论变了', await until(async () => (await page.locator('[data-toast]').allInnerTexts()).some((x) => x.includes('1 项结论有变化'))))
  await page.locator('[data-eval-pin="off"]').click()
  await until(() => writes(state, /\/pin$/).length === 1)
  check('设为基线：交 pinned=true，按钮换成「取消基线」', JSON.stringify(lastWrite(state, /\/pin$/)?.body) === JSON.stringify({ pinned: true })
    && await until(async () => await page.locator('[data-eval-pin="on"]').count() === 1))
  check('没有页面报错', !errors.length, errors.join(' ｜ '))
})

await section('逐题结果、和上一次比、A/B 配对表', async () => {
  const { page, state, errors } = await open('/evals/runs/ev-1?case=case-2')
  await page.locator('[data-eval-results="3"]').waitFor()
  await shot(page, 'run-results')
  const r2 = page.locator('[data-eval-result="r-2"]')
  const t2 = await r2.innerText()
  check('?case= 打开逐题、定位并展开那道题', await page.locator('[data-eval-result-detail="r-2"]').count() === 1)
  check('逐题：结论、期望和答案里的数、SQL 差在哪', t2.includes('不通过') && t2.includes('330') && t2.includes('310') && t2.includes('时间口径'), t2)
  check('一题的花费照服务端加好的写', (await page.locator('[data-eval-result="r-1"]').innerText()).includes('$0.014'))
  check('点开去运行详情', await page.locator('[data-eval-result-detail="r-2"] [data-eval-result-run]').getAttribute('href') === '/runs/run-answer-2')
  await page.locator('[data-eval-result-details]').waitFor()
  check('展开的明细写作答的经过和答案原文（明细工件）', (await text(page, '[data-eval-result-details]')).includes('自动重来了 1 次')
    && (await page.locator('[data-eval-result-details] details').count()) === 1)

  await page.getByRole('tab', { name: '和上一次比' }).click()
  await page.locator('[data-eval-transitions]').waitFor()
  check('对比：指纹差异写哪几项变了', (await text(page, '[data-eval-fingerprint]')).includes('写作模型：model-a → model-b')
    && (await text(page, '[data-eval-fingerprint]')).includes('表结构：变了'))
  check('转移和归因', (await text(page, '[data-eval-transitions]')).includes('退步 1') && (await text(page, '[data-eval-transitions]')).includes('做法变 1')
    && await page.locator('[data-eval-transition="regressed"]').count() === 1)
  await shot(page, 'run-compare')
  await page.locator('[data-eval-compare-base]').selectOption('ev-old')
  check('换基准：按点名的那一次重新比', await until(() => (state.compareBases ?? []).includes('ev-old')), JSON.stringify(state.compareBases))

  await page.goto(`${WEB}/evals/runs/ev-pa?tab=pair`, { waitUntil: 'networkidle' })
  await page.locator('[data-eval-pair-row]').waitFor()
  await shot(page, 'run-pair')
  check('A/B：都对 1、只 A 对 2、只 B 对 0、都错 1', await text(page, '[data-eval-pair-both]') === '1' && await text(page, '[data-eval-pair-only-a]') === '2'
    && await text(page, '[data-eval-pair-only-b]') === '0' && await text(page, '[data-eval-pair-neither]') === '1')
  check('p 值和「这批题上看不出差别」照服务端写', (await text(page, '[data-eval-pair-p]')).includes('p = 0.5') && (await text(page, '[data-eval-pair-p]')).includes('看不出差别'))
  check('两臂各自链到自己的详情', await page.locator('[data-eval-pair-arm="关目录"]').getAttribute('href') === '/evals/runs/ev-pb?tab=pair')
  check('没有页面报错', !errors.length, errors.join(' ｜ '))
})

// ---------------------------------------------------------------- 口径命中与重复度

await section('口径命中与重复度：参考线、分档、趋势、明细、加入题集、提取模板', async () => {
  const { page, state, errors } = await open('/evals/signals')
  await page.locator('[data-eval-signals="ok"]').waitFor()
  await page.locator('[data-eval-signals-detail]').waitFor()
  await shot(page, 'signals')
  check('E1：35%，值得按已治理的口径走（档位由服务端给）', (await text(page, '[data-eval-card="e1"]')).includes('35%')
    && (await text(page, '[data-eval-card="e1"]')).includes('值得按已治理的口径走'))
  check('E3：12%，反复问的问题还不多，按 SQL 形状的下限 4%', (await text(page, '[data-eval-card="e3"]')).includes('12%')
    && (await text(page, '[data-eval-card="e3"]')).includes('还不多') && (await text(page, '[data-eval-card="e3"]')).includes('4%'))
  check('参考线照服务端写', (await text(page, '[data-eval-signals-criteria]')).includes('≥ 30%') && (await text(page, '[data-eval-signals-criteria]')).includes('≥ 20%'))
  check('趋势：三个窗口各一根', await page.locator('[data-eval-trend-point="e1"]').count() === 3)
  await page.locator('[data-eval-signals-detail]').waitFor()
  check('明细直接来自口径账本的统计（按同一个窗口）', (await text(page, '[data-eval-signals-detail]')).includes('等价已证明 7')
    && state.ledgerSince === SIGNALS.window.since, state.ledgerSince)
  await page.locator('[data-eval-signals-days]').selectOption('90')
  check('换窗口：按 90 天重新取', await until(() => (state.signalDays ?? []).includes('90')), JSON.stringify(state.signalDays))
  await page.locator('[data-eval-cluster="4"]').waitFor()
  check('没挂运行的问题类不能提取模板', await page.locator('[data-eval-cluster="3"] [data-eval-cluster-extract]').isDisabled())
  await page.locator('[data-eval-cluster="4"] [data-eval-cluster-add]').click()
  await page.locator('[data-eval-cluster-suite]').selectOption('suite-b')
  await page.locator('[data-eval-cluster-go]').click()
  check('加入题集：去那个题集打开加题，问法带过去', await until(() => new URL(page.url()).pathname === '/evals/suites/suite-b')
    && await until(async () => (await page.locator('[data-eval-case-question]').inputValue()) === '上周华东的销售额是多少？'))
  await page.goto(`${WEB}/evals/signals`, { waitUntil: 'networkidle' })
  await page.locator('[data-eval-cluster="4"] [data-eval-cluster-extract]').click()
  check('提取模板：从最近一次的运行提取，去画布', await until(() => new URL(page.url()).pathname === '/studio/wf-drafted')
    && JSON.stringify(lastWrite(state, /from-run$/)?.body) === JSON.stringify({ run_id: 'run-chat-1' }), JSON.stringify(lastWrite(state, /from-run$/)?.body))
  check('没有页面报错', !errors.length, errors.join(' ｜ '))
})

// ---------------------------------------------------------------- 目录快照

await section('目录快照：列表、拍当前目录、影子起草先估算再开始、停止', async () => {
  const { page, state, errors } = await open('/evals/snapshots')
  await page.locator('[data-eval-snapshot="snap-live"]').waitFor()
  await shot(page, 'snapshots')
  check('影子起草在跑：写进度、给停止', (await text(page, '[data-eval-snapshot="snap-shadow"]')).includes('已起草 1 / 2 张表')
    && await page.locator('[data-eval-snapshot="snap-shadow"] [data-eval-snapshot-stop]').count() === 1)
  await page.locator('[data-eval-snapshot-take]').click()
  await page.locator('[data-eval-snapshot-sources]').selectOption(['ds-shop'])
  await page.locator('[data-eval-snapshot-take-confirm]').click()
  await until(() => lastWrite(state, /catalog-snapshots$/))
  check('拍当前目录：交选的数据源', JSON.stringify(lastWrite(state, /catalog-snapshots$/)?.body) === JSON.stringify({ source_ids: ['ds-shop'] }))
  await page.locator('[data-eval-snapshot-shadow]').click()
  await page.locator('[data-eval-shadow] [data-eval-snapshot-sources]').selectOption(['ds-shop'])
  check('没估算不能开始', await page.locator('[data-eval-shadow-start]').isDisabled())
  await page.locator('[data-eval-shadow-estimate]').click()
  await page.locator('[data-eval-shadow-plan]').waitFor()
  await shot(page, 'shadow')
  check('估算：几张表、调几次模型、多少钱、表结构里没有的表照服务端写', (await text(page, '[data-eval-shadow-plan]')).includes('2 张表')
    && (await text(page, '[data-eval-shadow-plan]')).includes('$0.020') && (await text(page, '[data-eval-shadow-plan]')).includes('nope'))
  await page.locator('[data-eval-shadow-start]').click()
  await until(() => lastWrite(state, /shadow$/))
  const shadow = lastWrite(state, /shadow$/)?.body
  check('开始：和估算是同一份请求体', JSON.stringify(shadow) === JSON.stringify(lastWrite(state, /shadow\/estimate$/)?.body), JSON.stringify(shadow))
  await page.locator('[data-eval-snapshot="snap-shadow"] [data-eval-snapshot-stop]').click()
  check('停止：发了一次', await until(() => writes(state, /snap-shadow\/stop$/).length === 1))
  check('没有页面报错', !errors.length, errors.join(' ｜ '))
})

// ---------------------------------------------------------------- 记录页、设置

await section('记录页：评测运行写来源「评测」，能跳回那道题', async () => {
  const { page, errors } = await open('/runs', { state: freshState({ runs: [clone(EVAL_RUN)] }) })
  const tag = page.locator('[data-run-eval]').first()
  await tag.waitFor()
  check('列表：评测运行带「评测」来源和作答这一步', (await page.locator('[data-run-origin="eval"]').first().innerText()).includes('评测')
    && (await tag.innerText()).includes('作答'))
  check('标记指向评测里的那道题', (await tag.getAttribute('data-run-eval')) === '/evals/runs/ev-1?case=case-2')
  await tag.click()
  check('点标记：去评测详情、定位到那道题', await until(() => page.url().endsWith('/evals/runs/ev-1?case=case-2')), page.url())
  await page.goto(`${WEB}/runs/run-eval-ans`, { waitUntil: 'networkidle' })
  const link = page.locator('[data-run-eval-link]')
  await link.waitFor()
  check('运行详情页头：键盘到得了的链接', (await link.evaluate((el) => el.tagName)) === 'A'
    && (await link.getAttribute('href')) === '/evals/runs/ev-1?case=case-2' && (await link.innerText()).includes('来自评测'))
  check('没有页面报错', !errors.length, errors.join(' ｜ '))
})

await section('设置：评测一组和今天已花；存的时候只交 eval 一组', async () => {
  const { page, state, errors } = await open('/settings/prefs')
  const sec = page.locator('[data-eval-settings]')
  await sec.waitFor()
  await sec.scrollIntoViewIfNeeded()
  await shot(page, 'settings')
  check('今天已花照服务端写', (await text(page, '[data-eval-spend]')).includes('今天评测已花 $0.370 / 每日上限 $5.00'))
  check('四项预算、保留次数照设置写', await page.locator('[data-eval-setting="max_parallel"]').inputValue() === '2'
    && await page.locator('[data-eval-setting="keep_runs"]').inputValue() === '20')
  await page.locator('[data-eval-setting="max_parallel"]').fill('3')
  await page.locator('[data-eval-setting="judge"]').check()
  await page.locator('[data-eval-settings-save]').click()
  await until(() => lastWrite(state, /^PUT \/settings$/))
  const put = lastWrite(state, /^PUT \/settings$/)?.body
  check('保存：只交 eval 一组，六项都在', JSON.stringify(Object.keys(put?.values ?? {})) === '["eval"]' && put.values.eval.max_parallel === 3
    && put.values.eval.judge === true && put.values.eval.keep_runs === 20, JSON.stringify(put))
  check('没有页面报错', !errors.length, errors.join(' ｜ '))
})

await section('写请求都在浏览器层拦下', async () => {
  check('没有一条写请求漏到服务端', !leaked.length, leaked.join('、'))
})

await browser.close()
console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 评测页检查全部通过')
process.exit(failed ? 1 : 0)
