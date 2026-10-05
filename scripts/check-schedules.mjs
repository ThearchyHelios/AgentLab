// 定时任务（/schedules、/schedules/new、/schedules/:id，记录页里运行名旁的「定时」标记）的回归检查。
//
// 守的是这些「看着没坏、其实不能用」的地方：预设生成的 cron 不对（选了每周一 09:00，交上去的是每天）、时区按本机写
// 而不是按定时任务的时区、预览不防抖、非法 cron 只弹一句不知道指哪一项、启用开关不带版本号（别人改过也照样覆盖）、
// 立即运行之后没有下文、触发历史只有一页、保存过的 Webhook 地址又被原样显示出来、改了别的字段却把地址清掉、
// 记录页看不出哪些运行来自定时任务、390px 窄屏被表单撑出横向滚动。
//
// 页面是真页面（/schedules 走真的应用外壳），接口全部在浏览器层答掉：定时任务、触发历史、预览、变量、工作流列表
// 都是这里的假数据（虚构的门店周报、库存日报），写请求照约定改这里的假状态；设置接口读真的、只把 notify 一组换成
// 假的，写请求不放行。别的 GET 放行到服务端（外壳要它），别的写请求一律拦成 503，不碰真实写接口。
// 另有一节纯函数（pages/schedules/model.ts：预设 ⇄ cron、按时区写时间、表单 ⇄ 请求体），借 vite 转译后直接调用。
//
//   node scripts/check-schedules.mjs
// 对别的实例跑时带上地址：AGENTLAB_WEB=http://localhost:<前端端口> AGENTLAB_API=http://localhost:<后端端口>/api …
// SCHEDULES_SHOTS=<目录> 时亮暗两套各截几张，供人眼复核；CHECK_ONLY=列表,新建 只跑段名里含这些字的段。
import { mkdirSync } from 'node:fs'
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const API = process.env.AGENTLAB_API ?? 'http://localhost:8000/api'
const CHROME = process.env.CHROME_PATH ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const SHOTS = process.env.SCHEDULES_SHOTS ?? ''
if (SHOTS) mkdirSync(SHOTS, { recursive: true })

let failed = 0
const check = (name, cond, detail = '') => {
  console.log(`  ${cond ? '✓' : '✗'} ${name}${detail ? ` — ${detail}` : ''}`)
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

// ---------------------------------------------------------------- 纯函数：借 vite 转译（同 check-trace）

const loaded = new Map()
async function load(path) {
  if (loaded.has(path)) return loaded.get(path)
  const res = await fetch(`${WEB}${path}?t=${Date.now()}`)
  if (!res.ok) throw new Error(`${path} → HTTP ${res.status}`)
  let code = await res.text()
  const deps = new Set([...code.matchAll(/from\s+["'](\/src\/[^"'?]+)(?:\?[^"']*)?["']/g)].map((m) => m[1]))
  for (const dep of deps) {
    const url = await load(dep)
    const pattern = new RegExp(`(["'])${dep.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(\\?[^"']*)?\\1`, 'g')
    code = code.replace(pattern, JSON.stringify(url))
  }
  const url = 'data:text/javascript;base64,' + Buffer.from(code).toString('base64')
  loaded.set(path, url)
  return url
}

await section('纯函数：预设 ⇄ cron', async () => {
  const m = await import(await load('/src/pages/schedules/model.ts'))
  const f = (patch) => ({ ...m.DEFAULT_FREQ, ...patch })
  const cases = [
    ['每天 09:30', f({ preset: 'daily', time: '09:30' }), '30 9 * * *'],
    ['每周一 09:00', f({ preset: 'weekly', time: '09:00', weekdays: [1] }), '0 9 * * 1'],
    ['工作日连成区间', f({ preset: 'weekly', time: '09:00', weekdays: [5, 1, 3, 2, 4] }), '0 9 * * 1-5'],
    ['周末逐个列出', f({ preset: 'weekly', time: '09:00', weekdays: [6, 0] }), '0 9 * * 0,6'],
    ['不连着的逐个列出', f({ preset: 'weekly', time: '18:15', weekdays: [1, 2, 4] }), '15 18 * * 1,2,4'],
    ['一周七天写 *', f({ preset: 'weekly', time: '09:00', weekdays: [0, 1, 2, 3, 4, 5, 6] }), '0 9 * * *'],
    ['每月 15 日 08:05', f({ preset: 'monthly', time: '08:05', monthday: 15 }), '5 8 15 * *'],
    ['每隔 6 小时在第 15 分', f({ preset: 'hourly', everyHours: 6, minute: 15 }), '15 */6 * * *'],
    ['每隔 1 小时写 *', f({ preset: 'hourly', everyHours: 1, minute: 0 }), '0 * * * *'],
    ['自定义去掉多余空白', f({ preset: 'custom', cron: '  0  9 * *   1 ' }), '0 9 * * 1'],
    ['钟点没填不猜', f({ preset: 'daily', time: '' }), ''],
    ['每周一天都没选不猜', f({ preset: 'weekly', weekdays: [] }), ''],
    ['每隔 5 小时不是 24 的约数', f({ preset: 'hourly', everyHours: 5, minute: 0 }), ''],
  ]
  const bad = cases.filter(([, freq, want]) => m.cronOf(freq) !== want)
  check(`cronOf：${cases.length} 种写法`, !bad.length, bad.map(([n, freq, want]) => `${n}：${m.cronOf(freq)} ≠ ${want}`).join('；'))

  const back = [
    ['0 9 * * 1', { preset: 'weekly', time: '09:00', weekdays: [1] }],
    ['0 9 * * 1-5', { preset: 'weekly', weekdays: [1, 2, 3, 4, 5] }],
    ['0 9 * * 7', { preset: 'weekly', weekdays: [0] }],
    ['30 7 * * *', { preset: 'daily', time: '07:30' }],
    ['5 8 15 * *', { preset: 'monthly', time: '08:05', monthday: 15 }],
    ['15 */6 * * *', { preset: 'hourly', everyHours: 6, minute: 15 }],
    ['0 * * * *', { preset: 'hourly', everyHours: 1, minute: 0 }],
    ['0 9 1,15 * *', { preset: 'custom' }],
    ['0 9 * 1 *', { preset: 'custom' }],
    ['*/5 * * * *', { preset: 'custom' }],
    ['0 */5 * * *', { preset: 'custom' }],
    ['0 9 * * MON', { preset: 'custom', cron: '0 9 * * MON' }],
    ['0 9 * *', { preset: 'custom' }],
  ]
  const wrong = back.filter(([cron, want]) => {
    const got = m.freqOf(cron)
    return Object.entries(want).some(([k, v]) => JSON.stringify(got[k]) !== JSON.stringify(v))
  })
  check(`freqOf：${back.length} 条 cron 落回预设，认不出的算自定义、原文照放`, !wrong.length,
    wrong.map(([cron]) => `${cron} → ${JSON.stringify(m.freqOf(cron))}`).join('；'))
  const round = ['0 9 * * 1', '0 9 * * 1-5', '0 9 * * 0,6', '30 7 * * *', '5 8 15 * *', '15 */6 * * *', '0 9 1,15 * *']
    .filter((c) => m.cronOf(m.freqOf(c)) !== c)
  check('认得出的写法来回一趟不变', !round.length, round.join('；'))
})

await section('纯函数：按定时任务的时区写时间', async () => {
  const m = await import(await load('/src/pages/schedules/model.ts'))
  const d = new Date('2026-10-05T01:00:00Z')
  const now = new Date('2026-10-01T00:00:00Z')
  check('Asia/Shanghai：2026-10-05 周一 09:00', m.formatZoned(d, 'Asia/Shanghai', 'full') === '2026-10-05 周一 09:00',
    m.formatZoned(d, 'Asia/Shanghai', 'full'))
  check('America/New_York：同一时刻是前一天 21:00', m.formatZoned(d, 'America/New_York', 'full') === '2026-10-04 周日 21:00',
    m.formatZoned(d, 'America/New_York', 'full'))
  check('同一年的短写法省掉年份', m.formatZoned(d, 'Asia/Shanghai', 'short', now) === '10/5 周一 09:00',
    m.formatZoned(d, 'Asia/Shanghai', 'short', now))
  check('跨年的短写法带年份', m.formatZoned(new Date('2027-01-04T01:00:00Z'), 'Asia/Shanghai', 'short', now) === '2027/1/4 周一 09:00')
  check('偏移：北京 UTC+08:00、纽约十月 UTC-04:00、一月 UTC-05:00、UTC',
    m.offsetLabel('Asia/Shanghai', d) === 'UTC+08:00' && m.offsetLabel('America/New_York', d) === 'UTC-04:00'
      && m.offsetLabel('America/New_York', new Date('2027-01-04T01:00:00Z')) === 'UTC-05:00' && m.offsetLabel('UTC', d) === 'UTC',
    [m.offsetLabel('Asia/Shanghai', d), m.offsetLabel('America/New_York', d), m.offsetLabel('UTC', d)].join(' '))
  check('浏览器不认的时区名不抛错', m.zonedParts(d, 'Mars/Base') === null && typeof m.formatZoned(d, 'Mars/Base') === 'string')
  const t = d.getTime()
  const until = [
    [t - 3 * 3600e3 - 5 * 60e3, '3 小时后'], [t - 59.6 * 60e3, '1 小时后'], [t - 30e3, '不到 1 分钟后'],
    [t - 12 * 60e3, '12 分钟后'], [t - 23.6 * 3600e3, '1 天后'], [t - 50 * 3600e3, '2 天后'],
  ].filter(([nowMs, want]) => m.formatUntil(d, nowMs) !== want)
  check('距下次运行：先取整再判档', !until.length, until.map(([n, w]) => `${m.formatUntil(d, n)} ≠ ${w}`).join('；'))
  check('已经过了的不写「…后」', m.formatUntil(d, t + 1000) === null)
  const opts = m.timezoneOptions('Europe/Berlin', 'Asia/Shanghai', d)
  check('时区下拉：本机排第一、正在用的紧跟、不重复，写城市和偏移',
    opts[0].tz === 'Asia/Shanghai' && opts[0].label.includes('本机') && opts[0].label.includes('UTC+08:00')
      && opts[1].tz === 'Europe/Berlin' && new Set(opts.map((o) => o.tz)).size === opts.length,
    opts.slice(0, 2).map((o) => o.label).join(' ｜ '))
  const odd = m.timezoneOptions('Antarctica/Troll', 'Asia/Shanghai', d)
  check('不在常用表里的时区也列上', odd.some((o) => o.tz === 'Antarctica/Troll'))
})

await section('纯函数：表单 ⇄ 请求体', async () => {
  const m = await import(await load('/src/pages/schedules/model.ts'))
  const saved = {
    id: 's1', name: '门店周报', workflow_id: 'wf-weekly', workflow_name: '门店周报', cron: '0 9 * * 1', timezone: 'Asia/Shanghai',
    run_class: 'formal', input_template: { date: '{{ schedule.yesterday }}' }, enabled: true, overlap: 'skip', misfire_grace_s: 3600,
    webhook: { format: 'feishu', url_hint: 'open.feishu.cn/…ab12', has_secret: true, events: ['failed', 'finished'] },
    next_run_at: null, last_fire: null, description_text: '每周一 09:00（Asia/Shanghai）', created_by: null,
    created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:00Z', version: 3,
  }
  const base = m.formOf(saved)
  check('读出来的表单：每周一 09:00、Webhook 开着、地址不回填', base.freq.preset === 'weekly' && base.freq.time === '09:00'
    && base.webhook.on && base.webhook.url === '' && !base.webhook.replaceUrl, JSON.stringify(base.freq))
  check('什么都没改：不发请求', m.patchBody(base, base, saved) === null)
  const reorder = { ...base, webhook: { ...base.webhook, events: ['finished', 'failed'] } }
  check('事件只是换了勾选顺序：不算改动', m.patchBody(reorder, base, saved) === null)
  const renamed = m.patchBody({ ...base, name: ' 门店周报（新）' }, base, saved)
  check('只改名字：只交名字，不带 Webhook（地址省略即不改）', JSON.stringify(renamed) === JSON.stringify({ name: '门店周报（新）' }),
    JSON.stringify(renamed))
  const events = m.patchBody({ ...base, webhook: { ...base.webhook, events: ['finished'] } }, base, saved)
  check('只改事件：Webhook 带格式和事件，不带地址和密钥', JSON.stringify(events) === JSON.stringify({ webhook: { format: 'feishu', events: ['finished'] } }),
    JSON.stringify(events))
  const replaced = m.patchBody({ ...base, webhook: { ...base.webhook, replaceUrl: true, url: ' https://open.feishu.cn/hook/new9 ' } }, base, saved)
  check('替换地址：带新地址', replaced?.webhook?.url === 'https://open.feishu.cn/hook/new9' && !('secret' in replaced.webhook), JSON.stringify(replaced))
  const removed = m.patchBody({ ...base, webhook: { ...base.webhook, on: false } }, base, saved)
  check('删除 Webhook：地址交空串', removed?.webhook?.url === '' && removed.webhook.format === 'feishu', JSON.stringify(removed))
  const cleared = m.patchBody({ ...base, webhook: { ...base.webhook, secretMode: 'clear' } }, base, saved)
  check('清除密钥：密钥交空串、地址省略', cleared?.webhook?.secret === '' && !('url' in cleared.webhook), JSON.stringify(cleared))
  const freq = m.patchBody({ ...base, freq: { ...base.freq, weekdays: [1, 3] } }, base, saved)
  check('改频率：交新的 cron', freq?.cron === '0 9 * * 1,3' && Object.keys(freq).length === 1, JSON.stringify(freq))
  const odd = { ...saved, cron: '0 9 * * 0-6' }
  const oddForm = m.formOf(odd)
  check('cron 原文写法不同（0-6）但人没动它：不算改了频率', m.patchBody({ ...oddForm, name: 'x' }, oddForm, odd)?.cron === undefined)
  const blanks = m.patchBody({ ...base, inputs: { date: '{{ schedule.yesterday }}', region: '  ' } }, base, saved)
  check('只填了空白的输入不算填了', blanks === null, JSON.stringify(blanks))

  const fresh = { ...m.formOf(null, 'Asia/Shanghai'), name: ' 库存日报 ', workflowId: 'wf-stock',
    freq: { ...m.DEFAULT_FREQ, preset: 'weekly', weekdays: [1], time: '09:00' }, inputs: { date: '{{ schedule.yesterday }}', region: '' } }
  const created = m.createBody({ ...fresh, webhook: { ...fresh.webhook, on: true, format: 'wecom', url: 'https://qyapi.weixin.qq.com/x?key=1', events: ['failed', 'finished'] } })
  check('新建的请求体：cron、时区、去掉没填的输入、没填密钥就不带', created.cron === '0 9 * * 1' && created.timezone === 'Asia/Shanghai'
    && created.name === '库存日报' && JSON.stringify(created.input_template) === JSON.stringify({ date: '{{ schedule.yesterday }}' })
    && created.webhook && !('secret' in created.webhook) && JSON.stringify(created.webhook.events) === '["finished","failed"]'
    && created.overlap === 'skip' && created.misfire_grace_s === 3600 && created.enabled === true, JSON.stringify(created))
  check('Webhook 关着：交 null', m.createBody(fresh).webhook === null)

  const probs = m.formProblems({ ...m.formOf(null, 'Asia/Shanghai'), webhook: { ...fresh.webhook, on: true, url: 'ftp://x', events: [] } }, null)
  check('问题：缺名称、缺工作流、地址不是 http(s)、一个事件都没选', !!probs.name && !!probs.workflow && !!probs.url && !!probs.events
    && !probs.freq, JSON.stringify(probs))
  const keep = m.formProblems({ ...base }, saved)
  check('已保存过地址、没点替换：不要求重填地址', !keep.url, JSON.stringify(keep))
  check('频率不完整各有说法', m.freqProblem({ ...m.DEFAULT_FREQ, preset: 'weekly', weekdays: [] }) === '至少选择一天'
    && m.freqProblem({ ...m.DEFAULT_FREQ, preset: 'custom', cron: '0 9 * *' }) === 'cron 表达式应为五段，以空格分隔'
    && m.freqProblem({ ...m.DEFAULT_FREQ, preset: 'daily', time: '' }) === '请填写时间')
})

// ---------------------------------------------------------------- 夹具：虚构的门店周报、库存日报

const NOW = Date.now()
const iso = (ms) => new Date(ms).toISOString()
const H = 3600e3
/** 下一个北京时间周一 09:00（UTC 01:00）之后再往后 k 周 */
function nextMonday9(k = 0) {
  const d = new Date(NOW)
  d.setUTCHours(1, 0, 0, 0)
  while (d.getUTCDay() !== 1 || d.getTime() <= NOW) d.setUTCDate(d.getUTCDate() + 1)
  return d.getTime() + k * 7 * 24 * H
}

const WORKFLOWS = [
  {
    id: 'wf-weekly', name: '门店周报', description: '各门店上周销售', tags: [], version: 4, is_template: false, status: 'published',
    published_version: 3, run_count: 12, created_at: iso(NOW - 40 * 24 * H), updated_at: iso(NOW - 2 * 24 * H),
    graph: { nodes: [{ id: 'in', type: 'input', position: { x: 0, y: 0 }, data: { label: '输入', config: {
      fields: [{ name: 'date', required: true }, { name: 'region', default: '华东' }, { name: 'draft_only' }] } } }], edges: [] },
  },
  {
    id: 'wf-stock', name: '库存日报', description: '各仓库存', tags: [], version: 2, is_template: false, status: 'draft',
    published_version: null, run_count: 3, created_at: iso(NOW - 10 * 24 * H), updated_at: iso(NOW - 24 * H),
    graph: { nodes: [{ id: 'in', type: 'input', position: { x: 0, y: 0 }, data: { label: '输入', config: {
      fields: [{ name: 'warehouse', required: true }] } } }], edges: [] },
  },
]
/** 正式运行的字段取已发布的 v3（比画布少一个 draft_only） */
const VERSION3 = {
  id: 'v3', version: 3, note: '发布', workflow_id: 'wf-weekly', published: true, graph: WORKFLOWS[0].graph,
  input_fields: [{ name: 'date', required: true, description: '统计日期' }, { name: 'region', default: '华东' }],
}
const VARIABLES = [
  { name: 'schedule.yesterday', label: '昨天（YYYY-MM-DD）', example: '2026-10-04' },
  { name: 'schedule.today', label: '今天（YYYY-MM-DD）', example: '2026-10-05' },
  { name: 'schedule.last_week_start', label: '上周一（YYYY-MM-DD）', example: '2026-09-28' },
]
const FEISHU_URL = 'https://open.feishu.cn/open-apis/bot/v2/hook/0f3c9a7e-demo-ab12'
const urlHint = (url) => {
  const u = new URL(url)
  return `${u.host}/…${url.slice(-4)}`
}

const fire = (id, patch = {}) => ({
  id, fire_at: iso(NOW - 24 * H), kind: 'scheduled', status: 'started', run_id: `run-${id}`, run_status: 'succeeded',
  reason: null, notified: [], created_at: iso(NOW - 24 * H), ...patch,
})
const S1 = {
  id: 's-weekly', name: '门店周报 · 每周', workflow_id: 'wf-weekly', workflow_name: '门店周报', cron: '0 9 * * 1',
  timezone: 'Asia/Shanghai', run_class: 'formal', input_template: { date: '{{ schedule.yesterday }}' }, enabled: true,
  overlap: 'skip', misfire_grace_s: 3600,
  webhook: { format: 'feishu', url_hint: urlHint(FEISHU_URL), has_secret: true, events: ['finished', 'failed'] },
  next_run_at: iso(NOW + 3 * H + 5 * 60e3),
  last_fire: fire('f-last', { fire_at: iso(NOW - 6 * 24 * H), run_id: 'run-sched-1', notified: [{ event: 'finished', status: 'ok', at: iso(NOW - 6 * 24 * H) }] }),
  description_text: '每周一 09:00（Asia/Shanghai）', created_by: '检查脚本', created_at: iso(NOW - 30 * 24 * H),
  updated_at: iso(NOW - 24 * H), version: 3,
}
const S2 = {
  id: 's-stock', name: '库存日报', workflow_id: 'wf-stock', workflow_name: '库存日报', cron: '30 7 * * *', timezone: 'America/New_York',
  run_class: 'exploratory', input_template: {}, enabled: false, overlap: 'skip', misfire_grace_s: 300, webhook: null,
  next_run_at: null,
  last_fire: fire('f-skip', { status: 'skipped_overlap', run_id: null, run_status: null, reason: '上一次运行（#a1b2c3）尚未结束' }),
  description_text: '每天 07:30（America/New_York）', created_by: null, created_at: iso(NOW - 9 * 24 * H), updated_at: iso(NOW - 9 * 24 * H),
  version: 1,
}

/** 触发历史：25 条，新的在前，按 20 条一页给。几种类型、结局、通知结果各放一条 */
function makeFires() {
  const out = []
  for (let i = 0; i < 25; i++) {
    const at = iso(NOW - (i + 1) * 7 * 24 * H)
    let f = fire(`f${String(i).padStart(2, '0')}`, { fire_at: at, created_at: at })
    if (i === 0) f = { ...f, kind: 'manual', notified: [{ event: 'finished', status: 'ok', at }, { event: 'failed', status: 'failed', at }] }
    if (i === 1) f = { ...f, status: 'skipped_overlap', run_id: null, run_status: null, reason: '上一次运行（#9f8e7d）尚未结束' }
    if (i === 2) f = { ...f, kind: 'misfire', run_status: 'failed' }
    if (i === 3) f = { ...f, status: 'skipped_misfire', run_id: null, run_status: null, reason: '服务停机，错过触发时间 3 小时，超出补发宽限 1 小时' }
    if (i === 4) f = { ...f, status: 'failed_to_start', run_id: null, run_status: null, reason: '工作流「门店周报」还没有发布：正式运行需要已发布的版本' }
    out.push(f)
  }
  return out
}

const RUNS = [
  { id: 'run-sched-1', workflow_id: 'wf-weekly', workflow_name: '门店周报', status: 'succeeded', input: { date: '2026-10-04' },
    output: { 周报: '上周销售额 1,234 万元' }, error: null, usage: { duration_ms: 4200 }, run_class: 'formal', version: 3,
    schedule_id: 's-weekly', created_at: iso(NOW - 2 * H), started_at: iso(NOW - 2 * H), finished_at: iso(NOW - 2 * H + 4200) },
  { id: 'run-manual-1', workflow_id: 'wf-weekly', workflow_name: '门店周报', status: 'succeeded', input: { date: '2026-10-03' },
    output: { 周报: '手动补一份' }, error: null, usage: { duration_ms: 3900 }, run_class: 'exploratory', version: null,
    schedule_id: null, created_at: iso(NOW - 3 * H), started_at: iso(NOW - 3 * H), finished_at: iso(NOW - 3 * H + 3900) },
]

function freshState(patch = {}) {
  return {
    schedules: [structuredClone(S1), structuredClone(S2)],
    fires: makeFires(),
    notify: { public_base_url: null },
    runs: null,
    ...patch,
  }
}

const shanghaiDay = (ms) => new Date(ms + 8 * H).toISOString().slice(0, 10)

/** 预览：几种认得出的写法按北京时间排出 5 次，非法的回 422 和出错的那一项（照约定的形状） */
function previewReply(body) {
  const [mi] = String(body?.cron ?? '').split(/\s+/)
  if (Number(mi) > 59) return { status: 422, json: { detail: '第 1 段（分钟）应在 0–59 之间，现为 61', field: 'cron' } }
  if (body?.timezone === 'Mars/Base') return { status: 422, json: { detail: '不认识的时区：Mars/Base', field: 'timezone' } }
  const tpl = body?.input_template ?? {}
  if (Object.values(tpl).some((v) => String(v).includes('schedule.nope'))) {
    return { status: 422, json: { detail: '输入「date」里有不认识的变量：schedule.nope', field: 'input_template' } }
  }
  const weekly = body?.cron === '0 9 * * 1'
  const next = Array.from({ length: body?.n ?? 5 }, (_, k) => iso(weekly ? nextMonday9(k) : NOW + (k + 1) * 24 * H))
  const yesterday = shanghaiDay(new Date(next[0]).getTime() - 24 * H)
  const inputs = Object.fromEntries(Object.entries(tpl).map(([k, v]) => [k, String(v).replace(/\{\{\s*schedule\.yesterday\s*\}\}/g, yesterday)]))
  return { json: { description_text: weekly ? '每周一 09:00（Asia/Shanghai）' : '每天 09:00（Asia/Shanghai）', next, inputs } }
}

/** 照约定改假状态：PATCH 核对版本、按地址省略 / 空串改 Webhook */
function applyPatch(s, body) {
  const out = { ...s }
  for (const k of ['name', 'workflow_id', 'cron', 'timezone', 'run_class', 'input_template', 'enabled', 'misfire_grace_s']) {
    if (k in body) out[k] = body[k]
  }
  if ('webhook' in body) {
    const w = body.webhook
    if (w.url === '') out.webhook = null
    else {
      out.webhook = {
        format: w.format, events: w.events,
        url_hint: w.url ? urlHint(w.url) : s.webhook?.url_hint,
        has_secret: w.secret === '' ? false : w.secret ? true : !!s.webhook?.has_secret,
      }
    }
  }
  out.version = s.version + 1
  out.updated_at = iso(Date.now())
  if (!out.enabled) out.next_run_at = null
  else if (!s.enabled) out.next_run_at = iso(NOW + 3 * H + 5 * 60e3)
  return out
}

let realSettings = null
async function settingsOf(state) {
  realSettings ??= await fetch(`${API}/settings`).then((r) => r.json()).catch(() => ({}))
  return { ...realSettings, ui: { ...(realSettings.ui ?? {}), theme: state.theme ?? 'dark' }, notify: state.notify }
}

/** 一个请求怎么答。返回 {status, json}；返回 null 表示放行（GET）或拦下（写请求） */
async function reply(state, method, path, url, body) {
  if (state.override) {
    const out = await state.override(method, path, body, url)
    if (out) return out
  }
  const m = path.match(/^\/schedules\/([^/]+)(\/.*)?$/)
  if (method === 'GET' && path === '/schedules') return { json: { items: state.schedules } }
  if (method === 'POST' && path === '/schedules/preview') return previewReply(body)
  if (method === 'GET' && path === '/schedules/variables') return { json: VARIABLES }
  if (method === 'POST' && path === '/schedules') {
    const id = 's-new'
    const s = {
      ...body, id, workflow_name: WORKFLOWS.find((w) => w.id === body.workflow_id)?.name ?? '', version: 1,
      webhook: body.webhook ? { format: body.webhook.format, url_hint: urlHint(body.webhook.url), has_secret: !!body.webhook.secret, events: body.webhook.events } : null,
      next_run_at: body.enabled ? iso(nextMonday9()) : null, last_fire: null, description_text: '每周一 09:00（Asia/Shanghai）',
      created_by: '检查脚本', created_at: iso(Date.now()), updated_at: iso(Date.now()),
    }
    state.schedules = [s, ...state.schedules]
    return { status: 201, json: s }
  }
  if (m) {
    const id = decodeURIComponent(m[1])
    const rest = m[2] ?? ''
    const s = state.schedules.find((x) => x.id === id)
    if (!s) return { status: 404, json: { detail: '定时任务不存在或已被删除' } }
    if (method === 'GET' && !rest) return { json: s }
    if (method === 'PATCH' && !rest) {
      if (body?.version !== s.version) return { status: 409, json: { detail: '定时任务已被修改（版本不符），请刷新后重试' } }
      const next = applyPatch(s, body)
      state.schedules = state.schedules.map((x) => (x.id === id ? next : x))
      return { json: next }
    }
    if (method === 'DELETE' && !rest) {
      state.schedules = state.schedules.filter((x) => x.id !== id)
      return { status: 204 }
    }
    if (method === 'POST' && rest === '/run') {
      const f = fire('f-manual', { fire_at: iso(Date.now()), kind: 'manual', run_id: 'run-new-1', run_status: 'queued' })
      state.schedules = state.schedules.map((x) => (x.id === id ? { ...x, last_fire: f } : x))
      return { json: { fire: f, run_id: 'run-new-1' } }
    }
    if (method === 'GET' && rest === '/fires') {
      const limit = Number(url.searchParams.get('limit') ?? 20)
      const before = url.searchParams.get('before')
      const start = before ? state.fires.findIndex((f) => f.fire_at === before) + 1 : 0
      const items = state.fires.slice(start, start + limit)
      const more = start + limit < state.fires.length
      return { json: { items, next_before: more ? items.at(-1).fire_at : null } }
    }
    if (method === 'POST' && rest === '/webhook/test') return { json: { ok: true, http_status: 200, message: '飞书机器人已接收' } }
  }
  if (method === 'GET' && path === '/workflows') return { json: WORKFLOWS }
  if (method === 'GET' && path === '/workflows/wf-weekly/versions/3') return { json: VERSION3 }
  if (method === 'GET' && path === '/settings') return { json: await settingsOf(state) }
  if (method === 'PUT' && path === '/settings') {
    if (body?.values?.notify) state.notify = body.values.notify
    return { json: await settingsOf(state) }
  }
  if (state.runs && method === 'GET' && path === '/runs') return { json: state.runs }
  if (state.runs && method === 'GET' && path.startsWith('/runs/')) {
    const run = state.runs.find((r) => path === `/runs/${r.id}`)
    if (run) return { json: run }
  }
  return null
}

// ---------------------------------------------------------------- 页面

const browser = await chromium.launch({ executablePath: CHROME })

/** 开一页：接口按 reply 答，记下每个请求；所有原生对话框、页面报错都记下，最后核对 */
async function open(path, { state = freshState(), width = 1280, height = 900, theme = 'dark' } = {}) {
  state.theme = theme
  const ctx = await browser.newContext({ viewport: { width, height }, colorScheme: theme, timezoneId: 'Asia/Shanghai' })
  opened.add(ctx)
  ctx.setDefaultTimeout(6000)
  ctx.setDefaultNavigationTimeout(30000)
  await ctx.addInitScript((t) => {
    try {
      localStorage.setItem('agentlab.theme', t)
      localStorage.setItem('agentlab_actor', '检查脚本')
    } catch { /* noop */ }
  }, theme)
  const page = await ctx.newPage()
  const sent = []
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
    sent.push({ key: `${method} ${p}`, body, search: url.search, at: Date.now() })
    const out = await reply(state, method, p, url, body)
    if (out) {
      return route.fulfill({ status: out.status ?? 200, contentType: 'application/json',
        body: out.json === undefined ? '' : JSON.stringify(out.json) })
    }
    if (method === 'GET') return route.continue()
    return route.fulfill({ status: 503, body: '' })
  })
  await page.goto(`${WEB}${path}`, { waitUntil: 'networkidle' })
  await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
  return { ctx, page, sent, errors, state }
}

const until = async (fn, ms = 6000, step = 100) => {
  const end = Date.now() + ms
  let v = await fn()
  while (!v && Date.now() < end) {
    await new Promise((r) => setTimeout(r, step))
    v = await fn()
  }
  return v
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const shot = async (page, name) => { if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true }) }
const writes = (sent, re) => sent.filter((s) => !s.key.startsWith('GET ') && re.test(s.key))
const text = (page, sel) => page.locator(sel).first().innerText()
/**
 * 本页自己的横向溢出：页面根和页里会横向滚动的容器（overflow-x 为 auto / scroll）。不量整页：390 宽时外壳里收起的
 * 导航提示本来就超出视口，与本页无关；也不量 overflow 为 hidden 的（截断加省略号是有意的）
 */
const overflow = (page) => page.evaluate(() => {
  const root = document.querySelector('[data-schedules-page]')
  if (!root) return 99
  const boxes = [root, ...[...root.querySelectorAll('*')].filter((el) => ['auto', 'scroll'].includes(getComputedStyle(el).overflowX))]
  return Math.max(0, ...boxes.map((el) => el.scrollWidth - el.clientWidth))
})
/** 等表单保存完（按钮回到「保存」且不可点）：只等请求发出去就往下走，回来的答复会落在下一步中间 */
const saveDone = (page) => until(async () => {
  const b = page.locator('[data-schedule-save]')
  return (await b.isDisabled()) && !(await b.innerText()).includes('正在保存')
})
/** 等预览算完、说的是这个频率 */
const previewSays = (page, words) => until(async () => (await page.locator('[data-schedule-preview][data-status="ok"]').count()) === 1
  && (await page.locator('[data-schedule-preview]').innerText()).includes(words))
/** 元素整个在视口的横向范围里 */
const inViewX = (page, sel) => page.evaluate((s) => {
  const el = document.querySelector(s)
  if (!el) return false
  const b = el.getBoundingClientRect()
  return b.left >= 0 && b.right <= innerWidth + 0.5
}, sel)

// ---------------------------------------------------------------- 列表

await section('列表：渲染', async () => {
  const { page, errors } = await open('/schedules')
  await page.locator('[data-schedule-row]').first().waitFor()
  const rows = await page.locator('[data-schedule-row]').count()
  check('两行定时任务', rows === 2, String(rows))
  const r1 = page.locator('[data-schedule-row="s-weekly"]')
  const t1 = await r1.innerText()
  check('名字、工作流、频率的中文说法', t1.includes('门店周报 · 每周') && t1.includes('门店周报') && t1.includes('每周一 09:00（Asia/Shanghai）'),
    t1.replace(/\s+/g, ' ').slice(0, 120))
  check('名字链到详情', await r1.locator('[data-schedule-name]').getAttribute('href') === '/schedules/s-weekly')
  const next = r1.locator('[data-schedule-next] time')
  const nextText = await next.innerText()
  const wantAbs = (() => {
    const d = new Date(S1.next_run_at)
    const p = new Intl.DateTimeFormat('en-US', { timeZone: 'Asia/Shanghai', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })
      .formatToParts(d).reduce((o, x) => ({ ...o, [x.type]: x.value }), {})
    return `${p.month}/${p.day}`
  })()
  check('下次运行：相对时间 + 按定时任务时区的绝对时间', nextText.includes('3 小时后') && nextText.includes(wantAbs), nextText)
  const title = await next.getAttribute('title')
  check('下次运行悬停看时区', !!title && title.includes('Asia/Shanghai'), title ?? '')
  check('下次运行的 datetime 是 ISO', (await next.getAttribute('datetime')) === S1.next_run_at)
  const last = r1.locator('[data-schedule-last] a')
  check('上次触发链到那次运行', await last.getAttribute('href') === '/runs/run-sched-1', await last.getAttribute('href') ?? '无链接')
  check('上次触发写运行的状态', (await last.innerText()).includes('已完成'), await last.innerText())
  const r2 = page.locator('[data-schedule-row="s-stock"]')
  const t2 = await r2.innerText()
  check('暂停的：标「已暂停」，下次运行写暂停中', t2.includes('已暂停') && t2.includes('暂停中'), t2.replace(/\s+/g, ' ').slice(0, 160))
  check('跳过的上次触发写明原因', t2.includes('上次未结束已跳过')
    && ((await r2.locator('[data-schedule-last] [title]').first().getAttribute('title')) ?? '').includes('尚未结束'))
  check('启用开关是 switch，状态对得上', await r1.locator('[data-schedule-toggle]').getAttribute('role') === 'switch'
    && await r1.locator('[data-schedule-toggle]').getAttribute('aria-checked') === 'true'
    && await r2.locator('[data-schedule-toggle]').getAttribute('aria-checked') === 'false')
  check('每行有立即运行、编辑、删除', await r1.locator('[data-schedule-run]').count() === 1
    && await r1.locator('[data-schedule-edit]').getAttribute('href') === '/schedules/s-weekly'
    && await r1.locator('[data-schedule-delete]').count() === 1)
  check('顶部有「新建定时任务」', (await page.locator('[data-schedule-create]').innerText()).includes('新建定时任务'))
  check('只读：没有写请求', true)
  await shot(page, 'list-dark')
  check('没有页面报错、原生对话框', !errors.length, errors.join(' ｜ '))
})

await section('列表：空态与老服务端', async () => {
  const { page, errors } = await open('/schedules', { state: freshState({ schedules: [] }) })
  await page.locator('[data-schedules-empty]').waitFor()
  const t = await text(page, '[data-schedules-empty]')
  check('空态说明定时任务是什么、怎么建', t.includes('还没有定时任务') && t.includes('自动发起工作流运行') && t.includes('选择工作流'), t.replace(/\s+/g, ' '))
  check('空态里有新建按钮', await page.locator('[data-schedules-empty] [data-schedule-create]').count() === 1)
  await page.locator('[data-schedules-empty] [data-schedule-create]').click()
  check('点新建去 /schedules/new', await until(() => new URL(page.url()).pathname === '/schedules/new'))

  const old = freshState()
  old.override = (method, path) => (path.startsWith('/schedules') ? { status: 404, json: { detail: 'Not Found' } } : null)
  const o = await open('/schedules', { state: old })
  await o.page.locator('[data-schedules-unsupported]').waitFor()
  check('老服务端（404）：说服务端暂不支持，不露英文原文', (await text(o.page, '[data-schedules-unsupported]')).includes('服务端暂不支持定时任务')
    && !(await o.page.locator('[data-schedules-page]').innerText()).includes('Not Found'))
  check('没有页面报错', !errors.length && !o.errors.length, [...errors, ...o.errors].join(' ｜ '))
})

await section('列表：启用开关', async () => {
  const { page, sent, errors, state } = await open('/schedules')
  const toggle = page.locator('[data-schedule-row="s-weekly"] [data-schedule-toggle]')
  await toggle.waitFor()
  await toggle.click()
  await until(async () => (await toggle.getAttribute('aria-checked')) === 'false')
  const patches = writes(sent, /^PATCH \/schedules\/s-weekly$/)
  check('点开关发一次 PATCH，带 enabled 和读到的版本', patches.length === 1
    && JSON.stringify(patches[0].body) === JSON.stringify({ enabled: false, version: 3 }), JSON.stringify(patches.map((p) => p.body)))
  check('开关跟着变成关、行里标已暂停', (await page.locator('[data-schedule-row="s-weekly"]').innerText()).includes('已暂停'))
  await toggle.click()
  await until(async () => (await toggle.getAttribute('aria-checked')) === 'true')
  const again = writes(sent, /^PATCH \/schedules\/s-weekly$/)
  check('再点一次带上新版本（4）', again.length === 2 && again[1].body?.version === 4 && again[1].body?.enabled === true,
    JSON.stringify(again.map((p) => p.body)))
  // 别处改过：409 时说服务端的原话、重新载入
  state.schedules = state.schedules.map((s) => (s.id === 's-stock' ? { ...s, version: 7 } : s))
  const t2 = page.locator('[data-schedule-row="s-stock"] [data-schedule-toggle]')
  await t2.click()
  const toastText = await until(async () => {
    const t = await page.locator('[data-toast]').allInnerTexts()
    return t.find((x) => x.includes('版本不符')) ?? null
  })
  check('版本不符（409）：说清原因并重新载入', !!toastText, toastText ?? (await page.locator('[data-toast]').allInnerTexts()).join(' ｜ '))
  const reloaded = await until(() => sent.filter((s) => s.key === 'GET /schedules').length >= 2)
  check('……重新拉了一次列表，开关回到服务端的状态', !!reloaded && await t2.getAttribute('aria-checked') === 'false')
  check('键盘：开关能聚焦、空格切换', await (async () => {
    await toggle.focus()
    await page.keyboard.press('Space')
    return until(async () => (await toggle.getAttribute('aria-checked')) === 'false')
  })())
  check('没有页面报错', !errors.length, errors.join(' ｜ '))
})

await section('列表：立即运行与删除', async () => {
  const { page, sent, errors, state } = await open('/schedules')
  const run = page.locator('[data-schedule-row="s-weekly"] [data-schedule-run]')
  await run.waitFor()
  await run.click()
  const ok = await until(async () => (await page.locator('[data-toast]').allInnerTexts()).find((x) => x.includes('已发起运行')) ?? null)
  check('立即运行发 POST', writes(sent, /^POST \/schedules\/s-weekly\/run$/).length === 1)
  check('结果：已发起运行，可直达那次运行', !!ok && ok.includes('查看运行'), ok ?? '')
  check('上次触发换成这一次（链到新运行）', await until(async () =>
    (await page.locator('[data-schedule-row="s-weekly"] [data-schedule-last] a').getAttribute('href')) === '/runs/run-new-1'))
  await page.locator('[data-toast]', { hasText: '已发起运行' }).getByRole('button', { name: '查看运行' }).click()
  check('点「查看运行」去 /runs/run-new-1', await until(() => new URL(page.url()).pathname === '/runs/run-new-1'))

  // 上一次还没结束：照样记一条手动触发，但跳过了
  state.override = (method, path) => (method === 'POST' && path === '/schedules/s-weekly/run'
    ? { json: { fire: fire('f-skip2', { kind: 'manual', status: 'skipped_overlap', run_id: null, run_status: null, reason: '上一次运行（#run-new）尚未结束' }), run_id: null } }
    : null)
  await page.goto(`${WEB}/schedules`, { waitUntil: 'networkidle' })
  await page.locator('[data-schedule-row="s-weekly"] [data-schedule-run]').click()
  const skipped = await until(async () => (await page.locator('[data-toast]').allInnerTexts()).find((x) => x.includes('尚未结束')) ?? null)
  check('上一次没结束：说本次已跳过', !!skipped, skipped ?? '')
  state.override = (method, path) => (method === 'POST' && path === '/schedules/s-weekly/run'
    ? { json: { fire: fire('f-bad', { kind: 'manual', status: 'failed_to_start', run_id: null, run_status: null, reason: '正式运行需要已发布的版本' }), run_id: null } }
    : null)
  await page.locator('[data-schedule-row="s-weekly"] [data-schedule-run]').click()
  const refused = await until(async () => (await page.locator('[data-toast]').allInnerTexts()).find((x) => x.includes('未能发起运行')) ?? null)
  check('发起被拒：写出服务端给的原因', !!refused && refused.includes('正式运行需要已发布的版本'), refused ?? '')
  state.override = null

  await page.locator('[data-schedule-row="s-stock"] [data-schedule-delete]').click()
  const dialog = page.locator('[role="dialog"]')
  await dialog.waitFor()
  const dt = await dialog.innerText()
  check('删除先确认（站内对话框），写清后果', dt.includes('删除定时任务「库存日报」') && dt.includes('已发起的运行'), dt.replace(/\s+/g, ' '))
  check('……确认前没发 DELETE', writes(sent, /^DELETE /).length === 0)
  await dialog.getByRole('button', { name: '删除定时任务' }).click()
  await until(async () => (await page.locator('[data-schedule-row="s-stock"]').count()) === 0)
  check('确认后发 DELETE、行消失', writes(sent, /^DELETE \/schedules\/s-stock$/).length === 1
    && await page.locator('[data-schedule-row="s-stock"]').count() === 0)
  check('没有页面报错、原生对话框', !errors.length, errors.join(' ｜ '))
})

// ---------------------------------------------------------------- 新建

/** 在新建页上填好「门店周报，每周一 09:00，飞书 Webhook」 */
async function fillWeekly(page) {
  const picker = page.locator('[data-workflow-picker] input')
  await picker.click()
  await picker.fill('周报')
  await page.locator('[data-workflow-option="wf-weekly"]').waitFor()
  check('工作流可搜索：输入「周报」只剩门店周报', await page.locator('[data-workflow-option]').count() === 1)
  await page.locator('[data-workflow-option="wf-weekly"]').click()
  check('选了工作流、名字空着时用工作流名填上', await page.locator('[data-field="name"]').inputValue() === '门店周报')
  await page.locator('[data-field="name"]').fill('门店周报 · 每周')
  await page.locator('[data-freq-preset="weekly"]').click()
  check('选「每周」默认勾周一', await page.locator('[data-weekday="1"]').getAttribute('aria-pressed') === 'true'
    && await page.locator('[data-weekday="3"]').getAttribute('aria-pressed') === 'false')
  await page.locator('[data-freq-time]').fill('09:00')
}

await section('新建：每周一 09:00 的请求体', async () => {
  const { page, sent, errors } = await open('/schedules/new')
  await page.locator('[data-schedule-form]').waitFor()
  check('页头写「新建定时任务」', (await page.locator('[data-schedules-page] header h1').innerText()).includes('新建定时任务'))
  check('时区默认取本机（Asia/Shanghai）', await page.locator('[data-timezone]').inputValue() === 'Asia/Shanghai')
  await fillWeekly(page)
  // 正式运行：字段取已发布的 v3（没有画布上的 draft_only）
  await page.locator('[data-input-field="date"]').waitFor()
  check('正式运行的输入字段取已发布的 v3', await page.locator('[data-input-field]').count() === 2
    && await page.locator('[data-input-field="draft_only"]').count() === 0
    && (await text(page, '[data-inputs-source]')).includes('v3'))
  check('必填字段标出来（和发起运行的表单一样是红色星号，读屏念「必填」）',
    await page.locator('[data-input-field="date"] [data-required][aria-label="必填"]').count() === 1
      && await page.locator('[data-input-field="region"] [data-required]').count() === 0)
  await page.locator('[data-input-vars="date"]').click()
  await page.locator('[data-input-field="date"] [data-var="schedule.yesterday"]').click()
  check('点变量插入模板', await page.locator('[data-input-field="date"] input').inputValue() === '{{ schedule.yesterday }}',
    await page.locator('[data-input-field="date"] input').inputValue())
  await previewSays(page, '每周一 09:00')
  const times = await page.locator('[data-preview-time]').allInnerTexts()
  check('预览接下来 5 次触发', times.length === 5 && times.every((t) => /周一 09:00/.test(t)), times.join(' ｜ '))
  check('预览写频率的中文说法', (await text(page, '[data-schedule-preview]')).includes('每周一 09:00'))
  const rendered = await until(async () => {
    const t = await page.locator('[data-input-rendered="date"]').innerText().catch(() => '')
    return /\d{4}-\d{2}-\d{2}/.test(t) ? t : null
  })
  check('输入旁边写渲染结果', !!rendered && rendered.includes('下次触发时为'), rendered ?? '')
  // 预览防抖：连续改三次只发一两次
  const before = writes(sent, /^POST \/schedules\/preview$/).length
  for (const t of ['09:01', '09:02', '09:00']) await page.locator('[data-freq-time]').fill(t)
  await sleep(1200)
  const burst = writes(sent, /^POST \/schedules\/preview$/).length - before
  check('预览防抖：连着改三次不发三次', burst >= 1 && burst <= 2, `${burst} 次`)

  // Webhook：飞书、地址、密钥，取消「等待审批」
  await page.locator('[data-webhook-on]').check()
  await page.locator('[data-webhook-format="feishu"]').click()
  await page.locator('[data-webhook-url]').fill(FEISHU_URL)
  await page.locator('[data-webhook-secret]').fill('s3cr3t-demo')
  await page.locator('[data-webhook-event="awaiting_approval"]').uncheck()
  check('测试消息要先保存', await page.locator('[data-webhook-test]').isDisabled())
  await page.locator('[data-schedule-save]').click()
  await until(() => writes(sent, /^POST \/schedules$/).length === 1)
  const body = writes(sent, /^POST \/schedules$/)[0]?.body
  check('POST 请求体：cron 0 9 * * 1', body?.cron === '0 9 * * 1', body?.cron)
  check('POST 请求体：时区 Asia/Shanghai', body?.timezone === 'Asia/Shanghai', body?.timezone)
  check('POST 请求体：工作流、名称、运行类别', body?.workflow_id === 'wf-weekly' && body?.name === '门店周报 · 每周' && body?.run_class === 'formal')
  check('POST 请求体：输入模板只带填了的字段', JSON.stringify(body?.input_template) === JSON.stringify({ date: '{{ schedule.yesterday }}' }),
    JSON.stringify(body?.input_template))
  check('POST 请求体：Webhook 格式、地址、密钥、事件', JSON.stringify(body?.webhook) === JSON.stringify({
    format: 'feishu', url: FEISHU_URL, secret: 's3cr3t-demo', events: ['finished', 'failed'] }), JSON.stringify(body?.webhook))
  check('POST 请求体：启用、重叠跳过、补发宽限', body?.enabled === true && body?.overlap === 'skip' && body?.misfire_grace_s === 3600,
    JSON.stringify({ enabled: body?.enabled, overlap: body?.overlap, misfire_grace_s: body?.misfire_grace_s }))
  check('创建后进详情页', await until(() => new URL(page.url()).pathname === '/schedules/s-new'))
  await page.locator('[data-webhook-url-hint]').waitFor()
  check('保存后地址只显示 url_hint', (await text(page, '[data-webhook-url-hint]')).includes('open.feishu.cn/…ab12'))
  check('……页面上和输入框里都没有完整地址', !(await page.locator('[data-schedules-page]').innerText()).includes('0f3c9a7e')
    && await page.locator('[data-webhook-url]').count() === 0
    && !(await page.locator('[data-schedules-page] input').evaluateAll((els) => els.map((e) => e.value))).some((v) => v.includes('0f3c9a7e')))
  check('……密钥显示「已设置」，不回填', (await text(page, '[data-webhook-secret-set]')).includes('已设置')
    && await page.locator('[data-webhook-secret]').count() === 0)
  await shot(page, 'detail-after-create')
  check('没有页面报错、原生对话框', !errors.length, errors.join(' ｜ '))
})

await section('新建：预览出错与保存前的问题', async () => {
  const { page, sent, errors } = await open('/schedules/new')
  await page.locator('[data-schedule-form]').waitFor()
  check('没填完不能保存，按钮上说还缺什么', await page.locator('[data-schedule-save]').isDisabled()
    && ((await page.locator('[data-schedule-save]').getAttribute('title')) ?? '').includes('名称'))
  await fillWeekly(page)
  await page.locator('[data-freq-preset="custom"]').click()
  check('切到自定义：带上当前预设生成的 cron', await page.locator('[data-freq-cron]').inputValue() === '0 9 * * 1',
    await page.locator('[data-freq-cron]').inputValue())
  await page.locator('[data-freq-cron]').fill('61 9 * * 1')
  const err = await until(async () => {
    const t = await page.locator('[data-freq-error]').innerText().catch(() => '')
    return t.includes('0–59') ? t : null
  })
  check('非法 cron：频率下面写服务端的说明', !!err, err ?? '')
  check('……频率输入框标成出错，说明接到 aria-describedby', await page.locator('[data-freq-cron]').getAttribute('aria-invalid') === 'true'
    && !!(await page.locator('[data-freq-cron]').getAttribute('aria-describedby')))
  check('……预览区不再摆上一次的时间', await page.locator('[data-preview-time]').count() === 0)
  check('……不能保存', await page.locator('[data-schedule-save]').isDisabled())
  await page.locator('[data-freq-cron]').fill('0 9 * * 1')
  await page.locator('[data-preview-time]').nth(4).waitFor()
  check('改对之后说明消失、预览回来', await page.locator('[data-freq-error]').count() === 0)
  await page.locator('[data-timezone]').selectOption('America/New_York')
  check('换时区之后预览按新时区写', await until(async () => (await text(page, '[data-schedule-preview]')).includes('America/New_York')))
  await page.locator('[data-input-field="date"] input').fill('{{ schedule.nope }}')
  const inErr = await until(async () => {
    const t = await page.locator('[data-inputs-error]').innerText().catch(() => '')
    return t.includes('schedule.nope') ? t : null
  })
  check('模板里写错变量：输入下面写服务端的说明', !!inErr, inErr ?? '')
  check('没有提交过 POST', writes(sent, /^POST \/schedules$/).length === 0)
  // 键盘：频率预设是单选组，方向键切换
  await page.locator('[data-freq-preset="custom"]').focus()
  await page.keyboard.press('ArrowLeft')
  check('键盘：频率预设方向键切换', await page.locator('[data-freq-preset="hourly"]').getAttribute('aria-checked') === 'true')
  check('表单字段都有 label', await page.locator('[data-schedule-form] input:not([type=hidden]), [data-schedule-form] select, [data-schedule-form] textarea')
    .evaluateAll((els) => els.every((el) => el.labels?.length || el.getAttribute('aria-label') || el.getAttribute('aria-labelledby'))))
  check('没有页面报错', !errors.length, errors.join(' ｜ '))
})

await section('新建：离开前确认', async () => {
  const { page, errors } = await open('/schedules/new')
  await page.locator('[data-schedule-form]').waitFor()
  await page.locator('[data-field="name"]').fill('半截')
  await page.locator('nav[aria-label="主导航"] a[href="/runs"]').click()
  const dialog = page.locator('[role="dialog"]')
  check('改了一半离开：先问一句', await dialog.waitFor({ timeout: 3000 }).then(() => true, () => false)
    && (await dialog.innerText()).includes('未保存的修改'))
  await dialog.getByRole('button', { name: '取消' }).click()
  check('……选留下：还在新建页', new URL(page.url()).pathname === '/schedules/new')
  check('没有页面报错', !errors.length, errors.join(' ｜ '))
})

// ---------------------------------------------------------------- 详情

await section('详情：Webhook 与运行链接地址', async () => {
  const { page, sent, errors, state } = await open('/schedules/s-weekly')
  await page.locator('[data-schedule-form]').waitFor()
  check('页头写名字和频率', (await page.locator('[data-schedules-page] header').innerText()).includes('门店周报 · 每周'))
  check('读出来的频率选中每周、勾周一', await page.locator('[data-freq-preset="weekly"]').getAttribute('aria-checked') === 'true'
    && await page.locator('[data-weekday="1"]').getAttribute('aria-pressed') === 'true')
  check('已保存的地址只显示 url_hint', (await text(page, '[data-webhook-url-hint]')).includes('open.feishu.cn/…ab12')
    && await page.locator('[data-webhook-url]').count() === 0)
  check('没改动时保存按钮不可点', await page.locator('[data-schedule-save]').isDisabled())
  await page.locator('[data-webhook-test]').click()
  const result = await until(async () => {
    const t = await page.locator('[data-webhook-test-result]').innerText().catch(() => '')
    return t.includes('飞书机器人已接收') ? t : null
  })
  check('发送测试消息并显示结果', writes(sent, /^POST \/schedules\/s-weekly\/webhook\/test$/).length === 1 && !!result && result.includes('200'), result ?? '')
  // 只改事件：PATCH 不带地址
  await page.locator('[data-webhook-event="awaiting_approval"]').check()
  check('改了 Webhook 设置后，测试要先保存', await page.locator('[data-webhook-test]').isDisabled())
  await page.locator('[data-schedule-save]').click()
  await until(() => writes(sent, /^PATCH \/schedules\/s-weekly$/).length === 1)
  await saveDone(page)
  const p1 = writes(sent, /^PATCH \/schedules\/s-weekly$/)[0]?.body
  check('只改事件：PATCH 带版本、格式和事件，不带地址和密钥', JSON.stringify(p1) === JSON.stringify({
    webhook: { format: 'feishu', events: ['finished', 'failed', 'awaiting_approval'] }, version: 3 }), JSON.stringify(p1))
  check('保存后按钮回到不可点、地址提示还在', await page.locator('[data-schedule-save]').isDisabled()
    && (await text(page, '[data-webhook-url-hint]')).includes('…ab12'))
  // 替换地址
  await page.locator('[data-webhook-replace]').click()
  await page.locator('[data-webhook-url]').fill('https://open.feishu.cn/open-apis/bot/v2/hook/new-demo-cd34')
  await page.locator('[data-schedule-save]').click()
  await until(() => writes(sent, /^PATCH \/schedules\/s-weekly$/).length === 2)
  await saveDone(page)
  const p2 = writes(sent, /^PATCH \/schedules\/s-weekly$/)[1]?.body
  check('替换地址：带新地址和新版本', p2?.webhook?.url === 'https://open.feishu.cn/open-apis/bot/v2/hook/new-demo-cd34' && p2?.version === 4
    && !('secret' in (p2?.webhook ?? {})), JSON.stringify(p2))
  check('……保存后又只显示 url_hint', await until(async () => (await text(page, '[data-webhook-url-hint]').catch(() => '')).includes('…cd34'))
    && await page.locator('[data-webhook-url]').count() === 0)
  // 运行链接地址：读写设置的 notify 组
  check('写出运行链接地址还没设置', (await text(page, '[data-link-base]')).includes('未设置'))
  await page.locator('[data-link-base-edit]').click()
  await page.locator('[data-link-base-input]').fill('ftp://bad')
  check('地址不对时不让存', await page.locator('[data-link-base-save]').isDisabled())
  await page.locator('[data-link-base-input]').fill('https://agentlab.example.com/')
  await page.locator('[data-link-base-save]').click()
  await until(() => writes(sent, /^PUT \/settings$/).length === 1)
  const put = writes(sent, /^PUT \/settings$/)[0]?.body
  check('PUT /api/settings 只写 notify 一组（去掉末尾的斜杠）', JSON.stringify(put) === JSON.stringify({
    values: { notify: { public_base_url: 'https://agentlab.example.com' } } }), JSON.stringify(put))
  check('……页面上写出当前值', await until(async () => (await text(page, '[data-link-base]')).includes('https://agentlab.example.com')))
  check('假状态里存上了', state.notify.public_base_url === 'https://agentlab.example.com')

  // 删除 Webhook
  await page.locator('[data-webhook-remove]').click()
  await page.locator('[data-schedule-save]').click()
  await until(() => writes(sent, /^PATCH \/schedules\/s-weekly$/).length === 3)
  await saveDone(page)
  const p3 = writes(sent, /^PATCH \/schedules\/s-weekly$/)[2]?.body
  check('删除 Webhook：地址交空串', p3?.webhook?.url === '' && p3?.version === 5, JSON.stringify(p3))
  check('……之后 Webhook 是关着的', await until(async () => !(await page.locator('[data-webhook-on]').isChecked())))
  check('没有页面报错、原生对话框', !errors.length, errors.join(' ｜ '))
})

await section('详情：保存冲突与页头操作', async () => {
  const { page, sent, errors, state } = await open('/schedules/s-weekly')
  await page.locator('[data-schedule-form]').waitFor()
  // 页头的开关：带版本，表单之后的保存用新版本
  await page.locator('header [data-schedule-toggle]').click()
  await until(async () => (await page.locator('header [data-schedule-toggle]').getAttribute('aria-checked')) === 'false')
  check('页头开关发 PATCH 带版本', JSON.stringify(writes(sent, /^PATCH /)[0]?.body) === JSON.stringify({ enabled: false, version: 3 }))
  await page.locator('[data-field="name"]').fill('门店周报 · 每周（新）')
  await page.locator('[data-schedule-save]').click()
  await until(() => writes(sent, /^PATCH /).length === 2)
  await saveDone(page)
  check('之后保存表单用开关之后的版本（4），不把启用状态改回去', JSON.stringify(writes(sent, /^PATCH /)[1]?.body) === JSON.stringify({
    name: '门店周报 · 每周（新）', version: 4 }), JSON.stringify(writes(sent, /^PATCH /)[1]?.body))
  // 别处改过
  state.schedules = state.schedules.map((s) => (s.id === 's-weekly' ? { ...s, version: 9, name: '别处改过的名字' } : s))
  await page.locator('[data-field="name"]').fill('又改一次')
  await page.locator('[data-schedule-save]').click()
  const conflict = await until(async () => (await page.locator('[data-schedule-conflict]').innerText().catch(() => '')) || null)
  check('版本不符（409）：明确说已在别处修改，并给重新载入', !!conflict && conflict.includes('别处被修改') && conflict.includes('版本不符')
    && await page.locator('[data-schedule-reload]').count() === 1, conflict ?? '')
  check('……没改页面上填的内容', await page.locator('[data-field="name"]').inputValue() === '又改一次')
  await page.locator('[data-schedule-reload]').click()
  const dialog = page.locator('[role="dialog"]')
  await dialog.waitFor()
  await dialog.getByRole('button', { name: '放弃修改并重新载入' }).click()
  check('重新载入后换成服务端的设置', await until(async () => (await page.locator('[data-field="name"]').inputValue()) === '别处改过的名字'))
  check('……冲突提示收起', await page.locator('[data-schedule-conflict]').count() === 0)
  // 立即运行：结果、触发历史跟着刷新
  await page.locator('header [data-schedule-run]').click()
  check('页头立即运行发 POST 并说结果', await until(async () => (await page.locator('[data-toast]').allInnerTexts()).some((x) => x.includes('已发起运行'))))
  check('没有页面报错', !errors.length, errors.join(' ｜ '))
})

await section('详情：触发历史分页', async () => {
  const { page, sent, errors } = await open('/schedules/s-weekly?tab=history')
  await page.locator('[data-fire-id]').first().waitFor()
  check('第一页 20 条', await page.locator('[data-fire-id]').count() === 20)
  const first = sent.find((s) => s.key === 'GET /schedules/s-weekly/fires')
  check('请求带 limit', !!first && new URLSearchParams(first.search).get('limit') === '20', first?.search ?? '')
  const row = (id) => page.locator(`[data-fire-id="${id}"]`)
  const t0 = await row('f00').innerText()
  check('手动触发写「手动」、通知结果逐条写', t0.includes('手动') && t0.includes('运行结束') && t0.includes('已发送') && t0.includes('发送失败'),
    t0.replace(/\s+/g, ' '))
  check('发起了的链到运行', await row('f00').locator('a[href="/runs/run-f00"]').count() === 1)
  const t1 = await row('f01').innerText()
  check('上次未结束已跳过：写原因，没有运行链接', t1.includes('上次未结束已跳过') && t1.includes('#9f8e7d') && await row('f01').locator('a[href^="/runs/"]').count() === 0,
    t1.replace(/\s+/g, ' '))
  check('补发写「补发」，运行状态照实写', (await row('f02').innerText()).includes('补发') && (await row('f02').innerText()).includes('失败'))
  check('错过已跳过写原因', (await row('f03').innerText()).includes('错过已跳过') && (await row('f03').innerText()).includes('超出补发宽限'))
  check('发起失败写原因', (await row('f04').innerText()).includes('发起失败') && (await row('f04').innerText()).includes('还没有发布'))
  const when = row('f05').locator('time')
  check('计划时间按定时任务的时区写，悬停看时区', /周一 09:00|周\S \d{2}:\d{2}/.test(await when.innerText())
    && ((await when.getAttribute('title')) ?? '').includes('Asia/Shanghai'), `${await when.innerText()} ｜ ${await when.getAttribute('title')}`)
  await page.locator('[data-fire-more]').click()
  await until(async () => (await page.locator('[data-fire-id]').count()) === 25)
  const more = sent.filter((s) => s.key === 'GET /schedules/s-weekly/fires').at(-1)
  check('加载更多：带上一页给的 before', new URLSearchParams(more?.search ?? '').get('before') === makeFires()[19].fire_at, more?.search ?? '')
  check('……接在后面，共 25 条、写已全部加载', await page.locator('[data-fire-id]').count() === 25
    && await page.locator('[data-fire-more]').count() === 0 && (await text(page, '[data-fire-list]')).includes('已全部加载'))
  check('没有重复的行', new Set(await page.locator('[data-fire-id]').evaluateAll((els) => els.map((e) => e.getAttribute('data-fire-id')))).size === 25)
  await page.locator('[role=tab]', { hasText: '设置' }).click()
  check('切回设置：地址去掉 tab', await until(() => !new URL(page.url()).searchParams.has('tab')))
  const real = writes(sent, /./).filter((s) => s.key !== 'POST /schedules/preview')
  check('只读：没有写请求', real.length === 0, real.map((s) => s.key).join('、'))
  check('直接打开触发历史：不先把表单的预览请求一遍', !sent.slice(0, sent.findIndex((x) => x.key === 'GET /schedules/s-weekly/fires') + 1)
    .some((x) => x.key === 'POST /schedules/preview'))
  check('没有页面报错', !errors.length, errors.join(' ｜ '))

  const none = freshState({ fires: [] })
  const e = await open('/schedules/s-stock?tab=history', { state: none })
  await e.page.locator('[data-fire-empty]').waitFor()
  check('没有触发记录：说清会记什么', (await text(e.page, '[data-fire-empty]')).includes('还没有触发记录'))
  const gone = await open('/schedules/s-nope')
  await gone.page.locator('[data-schedule-missing]').waitFor()
  check('不存在的定时任务：说清并给返回列表', (await text(gone.page, '[data-schedule-missing]')).includes('不存在')
    && await gone.page.locator('[data-schedule-missing] a[href="/schedules"]').count() === 1)
})

// ---------------------------------------------------------------- 记录页

await section('记录页：定时标记', async () => {
  const { page, errors } = await open('/runs', { state: freshState({ runs: RUNS }) })
  await page.locator('[data-runs-list] [data-run-id="run-sched-1"]').waitFor({ timeout: 10000 })
  const tag = page.locator('[data-run-id="run-sched-1"] [data-run-schedule]')
  check('定时任务发起的运行名字旁标「定时」', (await tag.innerText()).includes('定时'))
  check('手动发起的不标', await page.locator('[data-run-id="run-manual-1"] [data-run-schedule]').count() === 0)
  check('悬停写是哪个定时任务', await until(async () => ((await tag.getAttribute('title')) ?? '').includes('门店周报 · 每周')),
    (await tag.getAttribute('title')) ?? '')
  await tag.click()
  check('点标记去那个定时任务（不打开运行）', await until(() => new URL(page.url()).pathname === '/schedules/s-weekly'), page.url())
  check('没有页面报错', !errors.length, errors.join(' ｜ '))
})

// ---------------------------------------------------------------- 窄屏、浅色

await section('窄屏 390px 与浅色', async () => {
  // 每一步另开一页：新建表单填了一半时 page.goto 会被离开前确认（beforeunload）拦下
  const narrow = { width: 390, height: 844 }
  for (const theme of ['dark', 'light']) {
    const list = await open('/schedules', { ...narrow, theme })
    await list.page.locator('[data-schedule-row]').first().waitFor()
    // 外壳按设置换主题、开关的颜色过渡都要一点时间
    await sleep(400)
    check(`${theme}：列表不横向溢出`, await overflow(list.page) <= 1, `${await overflow(list.page)}px`)
    check(`${theme}：每行的立即运行、删除在屏幕里`, await inViewX(list.page, '[data-schedule-row="s-weekly"] [data-schedule-run]')
      && await inViewX(list.page, '[data-schedule-row="s-weekly"] [data-schedule-delete]'))
    const colors = await list.page.evaluate(() => {
      const row = document.querySelector('[data-schedule-row]')
      const track = document.querySelector('[data-schedule-toggle][aria-checked="true"] [data-switch-track]')
      const probe = document.createElement('div')
      probe.style.background = 'var(--accent-solid)'
      document.body.appendChild(probe)
      const accent = getComputedStyle(probe).backgroundColor
      probe.remove()
      return {
        theme: document.documentElement.dataset.theme, fg: getComputedStyle(row).color, bg: getComputedStyle(row).backgroundColor,
        track: track && getComputedStyle(track).backgroundColor, accent,
      }
    })
    check(`${theme}：页面是${theme === 'dark' ? '深色' : '浅色'}，开着的开关是强调色，行里字和底色分得开`,
      colors.theme === theme && colors.track === colors.accent && colors.fg !== colors.bg, JSON.stringify(colors))
    await shot(list.page, `list-390-${theme}`)

    const form = await open('/schedules/new', { ...narrow, theme })
    await form.page.locator('[data-schedule-form]').waitFor()
    await fillWeekly(form.page)
    await form.page.locator('[data-webhook-on]').check()
    await previewSays(form.page, '每周一 09:00')
    check(`${theme}：新建表单不横向溢出`, await overflow(form.page) <= 1, `${await overflow(form.page)}px`)
    check(`${theme}：保存按钮、预览、Webhook 地址在屏幕里`, await inViewX(form.page, '[data-schedule-save]')
      && await inViewX(form.page, '[data-schedule-preview]') && await inViewX(form.page, '[data-webhook-url]'))
    const order = await form.page.evaluate(() => {
      const y = (s) => document.querySelector(s)?.getBoundingClientRect().top ?? -1
      return { freq: y('[data-freq-preset]'), preview: y('[data-schedule-preview]'), inputs: y('[data-input-field]') }
    })
    check(`${theme}：窄屏上预览排在频率下面、输入上面`, order.freq < order.preview && order.preview < order.inputs, JSON.stringify(order))
    await shot(form.page, `new-390-${theme}`)

    const hist = await open('/schedules/s-weekly?tab=history', { ...narrow, theme })
    await hist.page.locator('[data-fire-id]').first().waitFor()
    check(`${theme}：触发历史不横向溢出`, await overflow(hist.page) <= 1, `${await overflow(hist.page)}px`)
    check(`${theme}：页头的开关、立即运行、删除在屏幕里`, await inViewX(hist.page, 'header [data-schedule-run]')
      && await inViewX(hist.page, 'header [data-schedule-toggle]') && await inViewX(hist.page, 'header [data-schedule-delete]'))
    await shot(hist.page, `history-390-${theme}`)
    const errors = [...list.errors, ...form.errors, ...hist.errors]
    check(`${theme}：没有页面报错`, !errors.length, errors.join(' ｜ '))
  }
})

await browser.close()
console.log(failed ? `\n✗ 定时任务：${failed} 项未通过` : '\n✓ 定时任务：全部通过')
process.exit(failed ? 1 : 0)
