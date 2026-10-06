// 定时任务（/schedules、/schedules/new、/schedules/:id，记录页里运行名旁的「定时」标记）的回归检查。
//
// 守的是这些「看着没坏、其实不能用」的地方：预设生成的 cron 不对（选了每周一 09:00，交上去的是每天）、时区按本机写
// 而不是按定时任务的时区、预览不防抖、非法 cron 只弹一句不知道指哪一项、启用开关不带版本号（别人改过也照样覆盖）、
// 立即运行之后没有下文、触发历史只有一页、保存过的 Webhook 地址又被原样显示出来、改了别的字段却把地址清掉、
// 记录页看不出哪些运行来自定时任务、390px 窄屏被表单撑出横向滚动。运行成功结束后的自动对比：触发历史里「与上一次
// 对比」（和运行详情「更多」菜单同一个叫法）链到对比页，旁边写有几个指标有变化，悬停只写各类指标的个数，基准不是
// 紧挨着的上一次时链接上看得见的字写「与上一次成功的运行对比」、悬停写明是哪一次；两种都带「默认基准：同一定时任务
// 上一次成功的运行」的来历，和运行详情「更多」里的同名入口打开同一对运行（服务端用同一个函数挑基准）。运行详情页头的
// 「由定时任务「X」发起」是键盘到得了的真链接（记录页的「定时」标记在整行按钮里，只能鼠标点），带日历图标，窄窗口里
// （768、820、900 宽，探索运行多一个「提取为草稿」）不被挤没，定时任务删掉之后写成纯文本。
// 还有几处时序和口径：晚到的刷新把刚切换的开关盖回去、先开触发历史再开设置时带着旧版本撞假的 409、保存途中又改了
// 别的字段时替换过的地址明文留在框里、服务端会拒的组合（未发布 + 正式运行、必填没填、已发布版本里没有的字段）
// 照样放行、测试消息的结果把服务端的整句又套一层、错过已跳过的那条悬停另说一个原因（和本条记的原因对不上）。
//
// 浏览器的本机时区故意设成 America/Los_Angeles，和定时任务的时区（Asia/Shanghai、America/New_York）都不同：
// 两者相同时「按定时任务时区写」和「按本机写」在页面上看不出区别，时间相关的断言一律比对按定时任务时区写出的原文。
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

await section('纯函数：服务端会拒的组合、保存回来怎么接、测试结果', async () => {
  const m = await import(await load('/src/pages/schedules/model.ts'))
  const fields = [{ name: 'date', required: true }, { name: 'region', required: true, default: '华东' }, { name: 'note' },
    { name: 'blank_default', required: true, default: '' }]
  const ip = (inputs, fs = fields) => JSON.stringify(m.inputProblems(inputs, fs))
  const cases = [
    ['都填了', { date: '{{ schedule.yesterday }}', blank_default: 'x' }, { unknown: [], missing: [] }],
    ['必填没填（默认值为空串也算没有默认值）', {}, { unknown: [], missing: ['date', 'blank_default'] }],
    ['只填了空白算没填', { date: '  ', blank_default: 'x' }, { unknown: [], missing: ['date'] }],
    ['多出来的字段', { date: 'a', blank_default: 'x', draft_only: 'y' }, { unknown: ['draft_only'], missing: [] }],
    ['多出来但留空的不交、不算', { date: 'a', blank_default: 'x', draft_only: '' }, { unknown: [], missing: [] }],
  ]
  const bad = cases.filter(([, inputs, want]) => ip(inputs) !== JSON.stringify(want))
  check(`inputProblems：${cases.length} 种情况`, !bad.length, bad.map(([n, inputs]) => `${n}：${ip(inputs)}`).join('；'))
  check('入口没声明字段：什么键都收，不报', ip({ anything: 'x' }, []) === JSON.stringify({ unknown: [], missing: [] }))

  const fresh = { ...m.formOf(null, 'Asia/Shanghai'), name: '库存日报', workflowId: 'wf-stock', runClass: 'formal' }
  const p1 = m.formProblems(fresh, null, { published: null, fields: [{ name: 'warehouse', required: true }] })
  check('新建：正式运行 + 没发布、必填没填，两样都拦', !!p1.runClass && !!p1.inputs, JSON.stringify(p1))
  check('……工作流还没取回来（发布状态不知道）时不判', !m.formProblems(fresh, null, {}).runClass)
  check('……探索运行不要求发布', !m.formProblems({ ...fresh, runClass: 'exploratory' }, null, { published: null }).runClass)
  const saved = {
    id: 's1', name: '门店周报', workflow_id: 'wf-weekly', workflow_name: '门店周报', cron: '0 9 * * 1', timezone: 'Asia/Shanghai',
    run_class: 'formal', input_template: {}, enabled: true, overlap: 'skip', misfire_grace_s: 3600, webhook: null,
    next_run_at: null, last_fire: null, description_text: '', created_by: null, created_at: '', updated_at: '', version: 3,
  }
  const edit = m.formOf(saved)
  const ctx = { published: null, fields: [{ name: 'date', required: true }] }
  check('修改：没动工作流、运行类别、输入时服务端不重新核对，前端也不拦（工作流后来撤了发布也能改名、停用）',
    Object.keys(m.formProblems({ ...edit, name: '新名字' }, saved, { ...ctx, recheck: false })).length === 0)
  check('……动了这三样就照新建一样拦', !!m.formProblems(edit, saved, { ...ctx, recheck: true }).runClass)

  const sv = m.serverFieldValue
  check('服务端指的那一项：改了它值就变，改别的不变', sv('name', { ...edit, name: 'X' }) !== sv('name', { ...edit, name: 'XY' })
    && sv('name', edit) === sv('name', { ...edit, misfireGraceS: 60 })
    && sv('run_class', edit) !== sv('run_class', { ...edit, runClass: 'exploratory' })
    && sv('misfire_grace_s', edit) !== sv('misfire_grace_s', { ...edit, misfireGraceS: 60 })
    && sv('not_a_field', edit) === null)

  const wbase = { ...edit, webhook: { ...edit.webhook, on: true, format: 'feishu' } }
  const sent = { ...wbase, webhook: { ...wbase.webhook, replaceUrl: true, url: 'https://open.feishu.cn/hook/secret-token-zz99' } }
  const back = { ...wbase, webhook: { ...wbase.webhook, replaceUrl: false, url: '' } }
  check('保存途中没再动：换成刚存下的', JSON.stringify(m.rebaseForm(sent, sent, back)) === JSON.stringify(back))
  const during = m.rebaseForm({ ...sent, name: '途中改名' }, sent, back)
  check('保存途中改了名字：名字留着，地址换成刚存下的（不再留明文）', during.name === '途中改名' && during.webhook.url === ''
    && during.webhook.replaceUrl === false, JSON.stringify(during.webhook))
  const evs = m.rebaseForm({ ...sent, webhook: { ...sent.webhook, events: ['failed'] } }, sent, back)
  check('……途中改了 Webhook 的事件：事件留着，地址照样换掉', JSON.stringify(evs.webhook.events) === '["failed"]' && evs.webhook.url === '')

  const t = m.webhookTestText
  const tests = [
    [{ ok: true, http_status: 200, message: '请到接收端确认已收到' }, '测试消息已送达（HTTP 200）：请到接收端确认已收到'],
    [{ ok: true, http_status: 200, message: '测试消息已送达（HTTP 200）' }, '测试消息已送达（HTTP 200）'],
    [{ ok: true, http_status: 204, message: '' }, '测试消息已送达（HTTP 204）'],
    [{ ok: false, http_status: 404, message: 'open.feishu.cn 返回 HTTP 404：not found' }, '测试消息发送失败：open.feishu.cn 返回 HTTP 404：not found'],
    [{ ok: false, http_status: 404, message: '推送失败：open.feishu.cn 返回 HTTP 404：not found' }, '测试消息发送失败：open.feishu.cn 返回 HTTP 404：not found'],
    [{ ok: false, http_status: null, message: '无法连接 open.feishu.cn' }, '测试消息发送失败：无法连接 open.feishu.cn'],
    [{ ok: false, http_status: 200, message: 'open.feishu.cn 返回错误码 19002：bad sign' }, '测试消息发送失败（HTTP 200）：open.feishu.cn 返回错误码 19002：bad sign'],
  ]
  const wrong = tests.filter(([r, want]) => t(r) !== want)
  check(`测试结果：服务端给整句、给补充说明都只说一遍（${tests.length} 种）`, !wrong.length, wrong.map(([r]) => t(r)).join('；'))
})

/** 对比页写的默认基准来历（定时任务发起的）：只挑比本次先发起、成功结束并封存的运行，服务端 api/compare.py 的 pick_base */
const REASON_SCHEDULE = '默认基准：同一定时任务上一次成功的运行'

await section('纯函数：触发历史对比的文案', async () => {
  const { SCHEDULE_TEXT: T, RUN_COMPARE_TEXT: C } = await import(await load('/src/lib/terms.ts'))
  check('「与上一次对比」和运行详情「更多」菜单同一个叫法', T.compareLink === C.menu && T.compareLink === '与上一次对比', T.compareLink)
  check('基准不是紧挨着的上一次时的叫法：与上一次成功的运行对比', T.compareLinkLastOk === '与上一次成功的运行对比', T.compareLinkLastOk)
  check('默认基准的来历照服务端的挑法写「上一次成功的运行」', C.reason.same_schedule === REASON_SCHEDULE
    && C.reason.same_workflow === '默认基准：同一工作流、同一运行类别上一次成功的运行', JSON.stringify(C.reason))
  const zero = { unchanged: 0, data_only: 0, caliber_and_data: 0, sql_changed: 0, params_only: 0, added: 0, removed: 0, incomparable: 0 }
  const cases = [
    ['有变化的几类合起来', { ...zero, data_only: 3, caliber_and_data: 1, params_only: 2, unchanged: 4 }, '6 个指标有变化'],
    ['新增、移除也算变化', { ...zero, added: 1, removed: 2 }, '3 个指标有变化'],
    ['没有变化、有无法比较的', { ...zero, incomparable: 2, unchanged: 3 }, '2 个指标无法比较'],
    ['都未变', { ...zero, unchanged: 5 }, '指标都未变'],
    ['两边都没有指标：不写', zero, null],
  ]
  const bad = cases.filter(([, sum, want]) => T.compareCount(sum) !== want)
  check(`看得见的计数：${cases.length} 种情况`, !bad.length, bad.map(([n, sum]) => `${n}：${T.compareCount(sum)}`).join('；'))
  check('悬停：基准不是紧挨着的上一次时写明是哪一次', T.compareTitle({ ...zero, data_only: 1 }, '#run-f1')
    === '与上一次成功的运行（#run-f1）相比：只数据变 1 个、口径与数据都变 0 个、SQL 结构变 0 个'
    && T.compareTitle(zero, '#run-f1') === '与上一次成功的运行（#run-f1）相比：两次运行都没有指标')
})

// ---------------------------------------------------------------- 夹具：虚构的门店周报、库存日报

const NOW = Date.now()
const iso = (ms) => new Date(ms).toISOString()
const H = 3600e3
/** 下一个北京时间周一 09:00（UTC 01:00）之后再往后 k 周（k 为负数是往前） */
function nextMonday9(k = 0) {
  const d = new Date(NOW)
  d.setUTCHours(1, 0, 0, 0)
  while (d.getUTCDay() !== 1 || d.getTime() <= NOW) d.setUTCDate(d.getUTCDate() + 1)
  return d.getTime() + k * 7 * 24 * H
}

// 按时区写时刻：和页面同一套写法，这里独立实现一遍，用来比对页面上的原文
const WEEKDAY = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']
const wallFormats = new Map()
/** 某个时刻在某个时区里的年月日、时分、星期（0 是周日） */
function wall(ms, tz) {
  if (!wallFormats.has(tz)) {
    wallFormats.set(tz, new Intl.DateTimeFormat('en-US', { timeZone: tz, year: 'numeric', month: 'numeric', day: 'numeric',
      hour: 'numeric', minute: 'numeric', weekday: 'short', hourCycle: 'h23' }))
  }
  const p = Object.fromEntries(wallFormats.get(tz).formatToParts(new Date(ms)).map((x) => [x.type, x.value]))
  return { y: +p.year, mo: +p.month, d: +p.day, h: +p.hour % 24, mi: +p.minute,
    wd: ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'].indexOf(p.weekday) }
}
const pad2 = (n) => String(n).padStart(2, '0')
/** full：「2026-10-12 周一 09:00」；short：同一年省掉年份「10/12 周一 09:00」，跨年写「2027/1/4 周一 09:00」 */
function zoned(ms, tz, mode = 'short') {
  const w = wall(ms, tz)
  const hm = `${pad2(w.h)}:${pad2(w.mi)}`
  if (mode === 'full') return `${w.y}-${pad2(w.mo)}-${pad2(w.d)} ${WEEKDAY[w.wd]} ${hm}`
  const md = `${w.mo}/${w.d}`
  return `${w.y === wall(Date.now(), tz).y ? md : `${w.y}/${md}`} ${WEEKDAY[w.wd]} ${hm}`
}
/**
 * 一个时区里从现在往后符合「分 时 * * 星期」的前 n 个时刻（按 15 分钟一格往后找，够用），照真服务端那样按所选时区算。
 * 认不出的写法返回 null
 */
function occurrences(cron, tz, n) {
  const [mi, ho, dom, mon, dow] = String(cron).trim().split(/\s+/)
  const minute = Number(mi)
  const hour = Number(ho)
  if (!/^\d+$/.test(mi ?? '') || !/^\d+$/.test(ho ?? '') || dom !== '*' || mon !== '*') return null
  const days = dow === '*' ? null : new Set(String(dow).split(',').map((d) => Number(d) % 7))
  const out = []
  for (let t = Math.ceil((NOW + 1) / 900e3) * 900e3, i = 0; out.length < n && i < 8 * 7 * 96; t += 900e3, i++) {
    const w = wall(t, tz)
    if (w.h === hour && w.mi === minute && (!days || days.has(w.wd))) out.push(t)
  }
  return out
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

/**
 * 触发历史：25 条，新的在前，按 20 条一页给。计划时间都是北京时间周一 09:00（往前数第 i+1 周）。
 * 几种类型、结局、通知结果各放一条。错过、没有补发的，服务端一律记成 kind=misfire、status=skipped_misfire，
 * 原因却有两种，各放一条、原因照服务端的原话写：f03 是停机一周多、在宽限内恢复时积压的那一次（最近一次就是补发的
 * f02），f05 是超出补发宽限的。f06、f07 没有记原因（约定里 reason 可以是 null），看兜底的说明
 */
/** 服务端自动对比给的计数（只有个数）：f00 和同一任务的上一次（run-f02）比过 */
const COMPARE_F00 = { base_run_id: 'run-f02', previous: true, summary: { unchanged: 4, data_only: 3, caliber_and_data: 1,
  sql_changed: 0, params_only: 2, added: 0, removed: 0, incomparable: 0 } }
const COMPARE_F00_TITLE = '与上一次相比：只数据变 3 个、口径与数据都变 1 个、SQL 结构变 0 个、只参数变 2 个、未变 4 个'
const NO_METRICS = { base_run_id: 'run-f09', previous: true, summary: { unchanged: 0, data_only: 0, caliber_and_data: 0,
  sql_changed: 0, params_only: 0, added: 0, removed: 0, incomparable: 0 } }
/** f10 的上一次（f11）失败了：基准是再往前成功的 f12，不是紧挨着的上一次 */
const COMPARE_F10 = { base_run_id: 'run-f12', previous: false, summary: { unchanged: 1, data_only: 1, caliber_and_data: 0,
  sql_changed: 0, params_only: 0, added: 0, removed: 0, incomparable: 0 } }

function makeFires() {
  const out = []
  const local = (at) => {
    const w = wall(Date.parse(at), 'Asia/Shanghai')
    return `${w.y}-${pad2(w.mo)}-${pad2(w.d)} ${pad2(w.h)}:${pad2(w.mi)}`
  }
  const missed = { kind: 'misfire', status: 'skipped_misfire', run_id: null, run_status: null }
  for (let i = 0; i < 25; i++) {
    const at = iso(nextMonday9(-(i + 1)))
    let f = fire(`f${String(i).padStart(2, '0')}`, { fire_at: at, created_at: at })
    if (i === 0) f = { ...f, kind: 'manual', compare: COMPARE_F00, notified: [{ event: 'finished', status: 'ok', at }, { event: 'failed', status: 'failed', at }] }
    if (i === 1) f = { ...f, status: 'skipped_overlap', run_id: null, run_status: null, reason: '上一次运行（#9f8e7d）尚未结束' }
    if (i === 2) f = { ...f, kind: 'misfire', run_status: 'failed', compare: null }
    if (i === 3) f = { ...f, ...missed, reason: `服务未运行，错过了 ${local(at)} 的触发；积压的触发不补，只发起最近一次` }
    if (i === 4) f = { ...f, status: 'failed_to_start', run_id: null, run_status: null, reason: '工作流「门店周报」还没有发布：正式运行需要已发布的版本' }
    if (i === 5) f = { ...f, ...missed, reason: `服务未运行，错过了 ${local(at)} 的触发，超出补发宽限（1 小时），没有发起` }
    if (i === 6) f = { ...f, ...missed, reason: null }
    if (i === 7) f = { ...f, status: 'failed_to_start', run_id: null, run_status: null, reason: null }
    if (i === 8) f = { ...f, compare: NO_METRICS }
    if (i === 10) f = { ...f, compare: COMPARE_F10 }
    if (i === 11) f = { ...f, run_status: 'failed', compare: null }
    out.push(f)
  }
  return out
}

const RUNS = [
  { id: 'run-sched-1', workflow_id: 'wf-weekly', workflow_name: '门店周报', status: 'succeeded', input: { date: '2026-10-04' },
    output: { 周报: '上周销售额 1,234 万元' }, error: null, usage: { duration_ms: 4200 }, run_class: 'formal', version: 3,
    started_by: '定时任务：门店周报（旧名）',
    schedule_id: 's-weekly', created_at: iso(NOW - 2 * H), started_at: iso(NOW - 2 * H), finished_at: iso(NOW - 2 * H + 4200) },
  { id: 'run-manual-1', workflow_id: 'wf-weekly', workflow_name: '门店周报', status: 'succeeded', input: { date: '2026-10-03' },
    output: { 周报: '手动补一份' }, error: null, usage: { duration_ms: 3900 }, run_class: 'exploratory', version: null,
    schedule_id: null, created_at: iso(NOW - 3 * H), started_at: iso(NOW - 3 * H), finished_at: iso(NOW - 3 * H + 3900) },
]

/** 有人署名点了「立即运行」：署名照写，页头另接到定时任务的链接 */
const RUN_MANUAL_FIRE = { ...RUNS[0], id: 'run-sched-2', started_by: '检查员甲', created_at: iso(NOW - H), started_at: iso(NOW - H),
  finished_at: iso(NOW - H + 4200) }
/** 探索运行成功了，页头多一个「提取为草稿」，这一行更挤：到点发起的、署名点了立即运行的各一次 */
const RUN_EXPLORE = { ...RUNS[0], id: 'run-sched-3', run_class: 'exploratory', version: null, started_by: '定时任务：门店周报 · 每周' }
const RUN_EXPLORE_MANUAL = { ...RUN_EXPLORE, id: 'run-sched-4', started_by: '检查员甲' }
/** 运行详情要的图和事件：一个节点、开始到结束两条，够页头和时间线画出来 */
const RUN_GRAPH = { nodes: [{ id: 'out', type: 'output', position: { x: 0, y: 0 }, data: { label: '成果', config: {} } }], edges: [] }
function runEvents(run) {
  const t0 = Date.parse(run.started_at) / 1000
  return [
    { seq: 1, type: 'run.started', node_id: null, ts: t0, data: { nodes: 1 } },
    { seq: 2, type: 'run.finished', node_id: null, ts: t0 + 4.2,
      data: { output: run.output, usage: {}, timing: { wall_ms: 4200, active_ms: 4200, wait_ms: 0 } } },
  ]
}

function freshState(patch = {}) {
  return {
    schedules: [structuredClone(S1), structuredClone(S2)],
    fires: makeFires(),
    notify: { public_base_url: null },
    runs: null,
    ...patch,
  }
}

/**
 * 预览：照真服务端那样按所选时区排出接下来几次（认不出的写法按每天一次凑数），非法的回 422 和出错的那一项
 * （照约定的形状）。state.previewNoZone 时时间不带时区后缀（UTC），看页面是不是和别处一个口径
 */
function previewReply(body, state) {
  const [mi] = String(body?.cron ?? '').split(/\s+/)
  if (Number(mi) > 59) return { status: 422, json: { detail: '第 1 段（分钟）应在 0–59 之间，现为 61', field: 'cron' } }
  if (body?.timezone === 'Mars/Base') return { status: 422, json: { detail: '不认识的时区：Mars/Base', field: 'timezone' } }
  const tpl = body?.input_template ?? {}
  if (Object.values(tpl).some((v) => String(v).includes('schedule.nope'))) {
    return { status: 422, json: { detail: '输入「date」里有不认识的变量：schedule.nope', field: 'input_template' } }
  }
  const tz = body?.timezone ?? 'UTC'
  const n = body?.n ?? 5
  const times = occurrences(body?.cron, tz, n) ?? Array.from({ length: n }, (_, k) => NOW + (k + 1) * 24 * H)
  const next = times.map((t) => (state?.previewNoZone ? iso(t).replace(/Z$/, '') : iso(t)))
  const day = wall(times[0] - 24 * H, tz)
  const yesterday = `${day.y}-${pad2(day.mo)}-${pad2(day.d)}`
  const inputs = Object.fromEntries(Object.entries(tpl).map(([k, v]) => [k, String(v).replace(/\{\{\s*schedule\.yesterday\s*\}\}/g, yesterday)]))
  const weekly = body?.cron === '0 9 * * 1'
  return { json: { description_text: `${weekly ? '每周一 09:00' : '每天 09:00'}（${tz}）`, next, inputs } }
}

/** 测试消息的答复照后端的写法：message 是状态码之外的补充说明 */
const WEBHOOK_TEST_OK = { ok: true, http_status: 200, message: '请到接收端确认已收到' }

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
  if (method === 'POST' && path === '/schedules/preview') return previewReply(body, state)
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
      if (body?.version !== s.version) {
        return { status: 409, json: { detail: `这个定时任务已被修改（当前是第 ${s.version} 版，提交的是第 ${body?.version} 版），请刷新后再保存` } }
      }
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
    if (method === 'POST' && rest === '/webhook/test') return { json: state.webhookTest ?? WEBHOOK_TEST_OK }
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
    const m = path.match(/^\/runs\/([^/]+)\/(events|graph|artifacts)$/)
    const of = m && state.runs.find((r) => r.id === m[1])
    if (of && m[2] === 'events') return { json: runEvents(of) }
    if (of && m[2] === 'graph') return { json: { graph: RUN_GRAPH, workflow_id: of.workflow_id, version: of.version } }
    if (of) return { json: [] }
  }
  if (state.runs && method === 'GET' && path === '/approvals' && url.searchParams.get('run_id')) return { json: [] }
  return null
}

// ---------------------------------------------------------------- 页面

const browser = await chromium.launch({ executablePath: CHROME })

/** 浏览器的本机时区：和夹具里定时任务的时区都不同，见开头的说明 */
const LOCAL_TZ = 'America/Los_Angeles'

/** 开一页：接口按 reply 答，记下每个请求；所有原生对话框、页面报错都记下，最后核对 */
async function open(path, { state = freshState(), width = 1280, height = 900, theme = 'dark' } = {}) {
  state.theme = theme
  const ctx = await browser.newContext({ viewport: { width, height }, colorScheme: theme, timezoneId: LOCAL_TZ })
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
/** 几段页面文字压成一行，失败时的说明不被换行截断 */
const oneLine = (texts) => texts.map((t) => t.replace(/\s+/g, ' ').trim()).join(' ｜ ')
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
/**
 * 只按 Tab 键把焦点移到 sel 上：从页面开头往后按，最多按 max 次。按到了返回按了几次，按不到返回 0。键盘可达就是
 * 这个意思——鼠标能点、程序能 focus() 的不算
 */
const tabTo = async (page, sel, max = 160) => {
  await page.evaluate(() => document.activeElement?.blur?.())
  for (let i = 1; i <= max; i++) {
    await page.keyboard.press('Tab')
    if (await page.evaluate((s) => !!document.activeElement?.matches?.(s), sel)) return i
  }
  return 0
}
/** 元素整个在视口的横向范围里 */
const inViewX = (page, sel) => page.evaluate((s) => {
  const el = document.querySelector(s)
  if (!el) return false
  const b = el.getBoundingClientRect()
  return b.left >= 0 && b.right <= innerWidth + 0.5
}, sel)
/**
 * 元素的字和它实际落在上面的底色的对比度（WCAG 算法），{ratio, fg, bg}，找不到元素为 null。底色顺着祖先往上找，
 * 半透明的一层层叠上去，直到不透明的那层（都透明就按白底）。只比「字色和某个容器的底色不相等」不够：页头本身是
 * 透明的，那样比永远成立
 */
const readable = (page, sel) => page.evaluate((s) => {
  const el = document.querySelector(s)
  if (!el) return null
  const parse = (c) => {
    const m = c.match(/color\(srgb ([\d.]+) ([\d.]+) ([\d.]+)(?: \/ ([\d.]+))?\)/)
    if (m) return [m[1] * 255, m[2] * 255, m[3] * 255, m[4] == null ? 1 : Number(m[4])]
    const n = c.match(/rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\)/)
    return n ? [Number(n[1]), Number(n[2]), Number(n[3]), n[4] == null ? 1 : Number(n[4])] : null
  }
  const over = (top, under) => top.slice(0, 3).map((v, i) => v * top[3] + under[i] * (1 - top[3]))
  const layers = []
  for (let n = el; n; n = n.parentElement) {
    const c = parse(getComputedStyle(n).backgroundColor)
    if (c && c[3] > 0) layers.push(c)
    if (c && c[3] >= 1) break
  }
  let bg = [255, 255, 255]
  for (const layer of layers.reverse()) bg = over(layer, bg)
  const color = parse(getComputedStyle(el).color)
  if (!color) return null
  const fg = over(color, bg)
  const lum = (rgb) => {
    const [r, g, b] = rgb.map((v) => { const x = v / 255; return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4 })
    return 0.2126 * r + 0.7152 * g + 0.0722 * b
  }
  const [hi, lo] = [lum(fg), lum(bg)].sort((a, b) => b - a)
  return { ratio: Math.round(((hi + 0.05) / (lo + 0.05)) * 100) / 100, fg: getComputedStyle(el).color,
    bg: `rgb(${bg.map(Math.round).join(', ')})` }
}, sel)

// ---------------------------------------------------------------- 列表

await section('列表：渲染', async () => {
  const { page, sent, errors } = await open('/schedules')
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
  const wantAbs = zoned(Date.parse(S1.next_run_at), 'Asia/Shanghai')
  check(`下次运行：相对时间 + 按定时任务时区（Asia/Shanghai，不是本机的 ${LOCAL_TZ}）的绝对时间`,
    nextText.includes('3 小时后') && nextText.includes(wantAbs), `${nextText} ｜ 应含 ${wantAbs}`)
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
  const lastAt = zoned(Date.parse(S2.last_fire.fire_at), 'America/New_York')
  const lastText = await r2.locator('[data-schedule-last] time').innerText()
  check('上次触发的时间按这一行自己的时区（America/New_York）写', lastText === lastAt, `${lastText} ｜ 应为 ${lastAt}`)
  check('启用开关是 switch，状态对得上', await r1.locator('[data-schedule-toggle]').getAttribute('role') === 'switch'
    && await r1.locator('[data-schedule-toggle]').getAttribute('aria-checked') === 'true'
    && await r2.locator('[data-schedule-toggle]').getAttribute('aria-checked') === 'false')
  check('每行有立即运行、编辑、删除', await r1.locator('[data-schedule-run]').count() === 1
    && await r1.locator('[data-schedule-edit]').getAttribute('href') === '/schedules/s-weekly'
    && await r1.locator('[data-schedule-delete]').count() === 1)
  check('顶部有「新建定时任务」', (await page.locator('[data-schedule-create]').innerText()).includes('新建定时任务'))
  const listWrites = writes(sent, /./)
  check('只读：列表渲染不发写请求', listWrites.length === 0, listWrites.map((w) => w.key).join('、'))
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
  // 别处改过：409 时说清是别处改的、已重新载入。服务端那句是写给表单保存的（「请刷新后再保存」），开关不照搬，放进详情
  state.schedules = state.schedules.map((s) => (s.id === 's-stock' ? { ...s, version: 7 } : s))
  const t2 = page.locator('[data-schedule-row="s-stock"] [data-schedule-toggle]')
  await t2.click()
  const conflictToast = page.locator('[data-toast]', { hasText: '别处被修改' })
  const toastText = await until(async () => (await conflictToast.count()) ? conflictToast.first().innerText() : null)
  check('版本不符（409）：说清是别处改的、已重新载入，不叫人去刷新、也不提保存',
    !!toastText && toastText.includes('库存日报') && toastText.includes('已重新载入') && !toastText.includes('刷新') && !toastText.includes('保存'),
    toastText ?? (await page.locator('[data-toast]').allInnerTexts()).join(' ｜ '))
  check('……服务端的原话放在详情里', ((await conflictToast.first().locator('details').textContent().catch(() => '')) ?? '').includes('当前是第 7 版'))
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

/** 某个 GET 的答复按请求到达那一刻的快照、晚 ms 才回：模拟一次在写操作之前读到旧值、之后才回来的刷新 */
const slowGet = (state, path, pick, ms = 1500) => async (method, p) => {
  if (method !== 'GET' || p !== path) return null
  const snap = structuredClone(pick(state))
  await sleep(ms)
  return { json: snap }
}

await section('列表：晚到的刷新不盖掉刚切换的开关', async () => {
  const state = freshState()
  const { page, sent, errors } = await open('/schedules', { state })
  const row = page.locator('[data-schedule-row="s-weekly"]')
  const toggle = row.locator('[data-schedule-toggle]')
  await toggle.waitFor()
  state.override = slowGet(state, '/schedules', (st) => ({ items: st.schedules }))
  const lists = () => sent.filter((s) => s.key === 'GET /schedules').length
  const before = lists()
  // 立即运行之后静默刷新一次：这次读到的是开关之前的快照，晚 1.5 秒才回
  await row.locator('[data-schedule-run]').click()
  await until(() => lists() > before)
  await until(async () => !(await row.locator('[data-schedule-run]').isDisabled()))
  await toggle.click()
  check('开关先变成关', await until(async () => (await toggle.getAttribute('aria-checked')) === 'false'))
  await sleep(1900)
  check('晚到的列表答复不把开关盖回去', await toggle.getAttribute('aria-checked') === 'false' && await row.getAttribute('data-enabled') === 'false',
    `aria-checked=${await toggle.getAttribute('aria-checked')}`)
  state.override = null
  await toggle.click()
  await until(() => writes(sent, /^PATCH \/schedules\/s-weekly$/).length === 2)
  const p = writes(sent, /^PATCH \/schedules\/s-weekly$/).map((x) => x.body)
  check('再点一次带开关之后的版本（4），不撞 409', p[1]?.version === 4 && p[1]?.enabled === true, JSON.stringify(p))
  await sleep(300)
  check('……没有冲突提示', !(await page.locator('[data-toast]').allInnerTexts()).some((t) => t.includes('别处被修改')))
  check('没有页面报错', !errors.length, errors.join(' ｜ '))
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
  check(`时区默认取本机（${LOCAL_TZ}）`, await page.locator('[data-timezone]').inputValue() === LOCAL_TZ,
    await page.locator('[data-timezone]').inputValue())
  await fillWeekly(page)
  await page.locator('[data-timezone]').selectOption('Asia/Shanghai')
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
  await previewSays(page, '每周一 09:00（Asia/Shanghai）')
  const times = await page.locator('[data-preview-time]').allInnerTexts()
  const want = occurrences('0 9 * * 1', 'Asia/Shanghai', 5).map((t) => zoned(t, 'Asia/Shanghai', 'full'))
  check(`预览接下来 5 次触发：按所选时区（不是本机的 ${LOCAL_TZ}）写，逐条对得上`,
    times.length === 5 && times.every((t, i) => t.includes(want[i])) && want.every((w) => w.endsWith('周一 09:00')),
    `${oneLine(times)} ｜ 应为 ${want.join(' ｜ ')}`)
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
  await previewSays(page, '（America/New_York）')
  const ny = occurrences('0 9 * * 1', 'America/New_York', 5).map((t) => zoned(t, 'America/New_York', 'full'))
  const nyTimes = await page.locator('[data-preview-time]').allInnerTexts()
  check('换时区之后预览按新时区写：5 个时刻逐条对得上', nyTimes.length === 5 && nyTimes.every((t, i) => t.includes(ny[i])),
    `${oneLine(nyTimes)} ｜ 应为 ${ny.join(' ｜ ')}`)
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

await section('新建：换工作流、未发布与输入字段', async () => {
  const { page, errors, state } = await open('/schedules/new')
  await page.locator('[data-schedule-form]').waitFor()
  const picker = page.locator('[data-workflow-picker] input')
  const pick = async (id) => {
    await picker.click()
    await page.locator(`[data-workflow-option="${id}"]`).click()
  }
  const name = page.locator('[data-field="name"]')
  const save = page.locator('[data-schedule-save]')
  const saveTitle = async () => (await save.getAttribute('title')) ?? ''
  await pick('wf-weekly')
  check('选了门店周报：名称自动填成门店周报', await name.inputValue() === '门店周报')
  await pick('wf-stock')
  check('改选库存日报：自动填的名称跟着换', await name.inputValue() === '库存日报', await name.inputValue())
  check('库存日报没有发布：运行类别替人选探索运行', await page.locator('[data-run-class="exploratory"]').getAttribute('aria-checked') === 'true')

  // 服务端对「正式运行 + 没发布」在保存时就回 422：前端当场拦下，不说「到点才失败」
  await page.locator('[data-run-class="formal"]').click()
  const problem = page.locator('[data-run-class-problem]')
  const pt = await problem.innerText().catch(() => '')
  check('没发布又选正式运行：运行类别下面写明要先发布或改用探索运行', pt.includes('发布') && pt.includes('探索运行') && !pt.includes('发起失败'), pt)
  check('……不能保存，按钮上说还要处理运行类别', await save.isDisabled() && (await saveTitle()).includes('运行类别'), await saveTitle())
  await shot(page, 'new-not-published')
  await page.locator('[data-run-class="exploratory"]').click()
  check('改回探索运行：问题收起', await problem.count() === 0)
  const wh = page.locator('[data-input-field="warehouse"]')
  const wt = await wh.innerText()
  check('必填且没有默认值：说要填写后才能保存，不说到点才失败', wt.includes('请填写') && !wt.includes('到点'), wt.replace(/\s+/g, ' '))
  check('……不能保存，按钮上说还要处理输入', await save.isDisabled() && (await saveTitle()).includes('输入'), await saveTitle())
  await wh.locator('input').fill('北仓')
  check('填了之后可以保存', await until(async () => !(await save.isDisabled())), await saveTitle())
  await name.fill('我的日报')
  await pick('wf-weekly')
  check('人改过的名称换工作流时不覆盖', await name.inputValue() === '我的日报', await name.inputValue())

  // 探索运行用画布上的字段（有 draft_only），正式运行用已发布的 v3（没有）：交上去会被拒，写清是哪一版里没有
  await page.locator('[data-input-field="draft_only"] input').fill('只在画布上')
  await page.locator('[data-input-field="date"] input').fill('2026-10-01')
  await page.locator('[data-run-class="formal"]').click()
  const stale = page.locator('[data-input-stale="draft_only"]')
  await stale.waitFor()
  const st = await stale.innerText()
  check('切到正式运行：画布独有的字段写明已发布的 v3 中没有，不说工作流里没有', st.includes('已发布的 v3 中没有这个字段') && !st.includes('工作流中已没有'),
    st.replace(/\s+/g, ' '))
  check('……交上去会被拒：不能保存，按钮上说还要处理输入', await save.isDisabled() && (await saveTitle()).includes('输入'), await saveTitle())
  if (SHOTS) {
    // 等运行类别的颜色过渡播完、把这一行挪到保存栏上面再截
    await sleep(300)
    await stale.evaluate((el) => el.scrollIntoView({ block: 'center' }))
  }
  await shot(page, 'new-stale-field')
  await stale.getByRole('button', { name: '移除' }).click()
  check('移除之后可以保存', await until(async () => !(await save.isDisabled())), await saveTitle())

  // 保存被拒、指明运行类别：说明写在运行类别下面，不只在底部
  state.override = (method, path) => (method === 'POST' && path === '/schedules'
    ? { status: 422, json: { detail: '「门店周报」还没有发布版本，正式运行只能从发布版本发起。请先在编排页发布，或改为探索运行', field: 'run_class' } }
    : null)
  await save.click()
  const rcErr = await until(async () => (await page.locator('[data-run-class-error]').innerText().catch(() => '')) || null)
  check('保存被拒、指明运行类别：说明写在运行类别下面', !!rcErr && rcErr.includes('还没有发布版本'), rcErr ?? '')
  await shot(page, 'new-run-class-error')
  check('没有页面报错', !errors.length, errors.join(' ｜ '))
})

await section('新建：预览时间不带时区后缀', async () => {
  const { page, errors } = await open('/schedules/new', { state: freshState({ previewNoZone: true }) })
  await page.locator('[data-schedule-form]').waitFor()
  await fillWeekly(page)
  await page.locator('[data-timezone]').selectOption('Asia/Shanghai')
  await previewSays(page, '每周一 09:00（Asia/Shanghai）')
  const times = await page.locator('[data-preview-time]').allInnerTexts()
  const want = occurrences('0 9 * * 1', 'Asia/Shanghai', 5).map((t) => zoned(t, 'Asia/Shanghai', 'full'))
  check('不带时区后缀的时间按 UTC 读（和列表、触发历史一个口径），不按本机时间读', times.length === 5 && times.every((t, i) => t.includes(want[i])),
    `${oneLine(times)} ｜ 应为 ${want.join(' ｜ ')}`)
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
  // 测试结果：服务端的 message 是补充说明时接在结局后面；是整句（老写法）时不再套一层。状态码只写一次
  const testResult = async (reply, want) => {
    state.webhookTest = reply
    const before = writes(sent, /^POST \/schedules\/s-weekly\/webhook\/test$/).length
    await page.locator('[data-webhook-test]').click()
    await until(() => writes(sent, /^POST \/schedules\/s-weekly\/webhook\/test$/).length > before)
    const got = await until(async () => {
      const t = (await page.locator('[data-webhook-test-result]').innerText().catch(() => '')).trim()
      return t === want ? t : null
    }, 3000)
    return got ?? (await page.locator('[data-webhook-test-result]').innerText().catch(() => '')).trim()
  }
  const times = (str, word) => str.split(word).length - 1
  const okText = await testResult(null, '测试消息已送达（HTTP 200）：请到接收端确认已收到')
  check('发送测试消息并显示结果：结局、状态码、补充说明各一遍', okText === '测试消息已送达（HTTP 200）：请到接收端确认已收到'
    && await page.locator('[data-webhook-test-result="ok"]').count() === 1, okText)
  const whole = await testResult({ ok: true, http_status: 200, message: '测试消息已送达（HTTP 200）' }, '测试消息已送达（HTTP 200）')
  check('……服务端给的是整句：不再套一层', whole === '测试消息已送达（HTTP 200）' && times(whole, 'HTTP 200') === 1, whole)
  const fail = await testResult({ ok: false, http_status: 404, message: 'open.feishu.cn 返回 HTTP 404：not found' },
    '测试消息发送失败：open.feishu.cn 返回 HTTP 404：not found')
  check('……发送失败：状态码只写一遍', fail === '测试消息发送失败：open.feishu.cn 返回 HTTP 404：not found' && times(fail, 'HTTP 404') === 1
    && await page.locator('[data-webhook-test-result="failed"]').count() === 1, fail)
  const failOld = await testResult({ ok: false, http_status: 404, message: '推送失败：open.feishu.cn 返回 HTTP 404：not found' },
    '测试消息发送失败：open.feishu.cn 返回 HTTP 404：not found')
  check('……「发送失败」「推送失败」不叠在一起', !failOld.includes('推送失败') && times(failOld, 'HTTP 404') === 1, failOld)
  await page.locator('[data-webhook-test-result]').scrollIntoViewIfNeeded()
  await shot(page, 'detail-webhook-test-failed')
  state.webhookTest = null
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
  // 运行链接地址：读写设置的 notify 组。对比页链接也按它拼，说明里一并说到
  const unset = await text(page, '[data-link-base]')
  check('写出运行链接地址还没设置，没有它时运行和对比页链接都不附', unset.includes('未设置') && unset.includes('运行和对比页链接'),
    oneLine([unset]))
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
  check('版本不符（409）：明确说已在别处修改，并给重新载入', !!conflict && conflict.includes('别处被修改') && conflict.includes('当前是第 9 版')
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

await section('详情：保存被拒后的字段说明', async () => {
  const { page, errors, state } = await open('/schedules/s-weekly')
  await page.locator('[data-schedule-form]').waitFor()
  const reject = (field, detail, when = () => true) => {
    state.override = (method, path, body) => (method === 'PATCH' && path === '/schedules/s-weekly' && when(body)
      ? { status: 422, json: { detail, field } } : null)
  }
  const name = page.locator('[data-field="name"]')
  const save = page.locator('[data-schedule-save]')
  reject('name', '名称不能是单个字母', (b) => b?.name === 'X')
  await name.fill('X')
  await save.click()
  const said = await until(async () => (await page.locator('#schedule-name-error').innerText().catch(() => '')) || null)
  check('保存被拒、指明名称：名称下面写服务端的说明', !!said && said.includes('名称不能是单个字母'), said ?? '')
  check('……名称框标成出错、说明接到 aria-describedby', await name.getAttribute('aria-invalid') === 'true'
    && await name.getAttribute('aria-describedby') === 'schedule-name-error',
    `${await name.getAttribute('aria-invalid')} ｜ ${await name.getAttribute('aria-describedby')}`)
  check('……底部写保存失败', await page.locator('[data-schedule-save-error]').count() === 1)
  await shot(page, 'detail-name-rejected')
  await name.fill('XY')
  check('改了名称：名称下的说明和底部的保存失败都收起', await until(async () => await page.locator('#schedule-name-error').count() === 0
    && await page.locator('[data-schedule-save-error]').count() === 0))
  check('……名称框不再标成出错', await name.getAttribute('aria-invalid') === null)
  await name.fill('X')
  check('改回被拒的那个值：说明又出来（服务端说的就是这个值）', await until(async () => await page.locator('#schedule-name-error').count() === 1))

  // 运行类别、补发宽限被拒：说明写在各自下面
  await name.fill('门店周报 · 每周')
  reject('run_class', '运行类别需要选择正式运行或探索运行')
  await page.locator('[data-run-class="exploratory"]').click()
  await save.click()
  const rc = await until(async () => (await page.locator('[data-run-class-error]').innerText().catch(() => '')) || null)
  check('运行类别被拒：说明写在运行类别下面', !!rc && rc.includes('运行类别需要选择'), rc ?? '')
  await page.locator('[data-run-class="formal"]').click()
  check('……改了运行类别，说明收起', await until(async () => await page.locator('[data-run-class-error]').count() === 0))
  reject('misfire_grace_s', '补发宽限需要是 0 到 604800 之间的整数秒数（最长 7 天）')
  await page.locator('[data-misfire]').selectOption('86400')
  await save.click()
  const ms = await until(async () => (await page.locator('#schedule-misfire-error').innerText().catch(() => '')) || null)
  check('补发宽限被拒：说明写在补发宽限下面，选择框标成出错', !!ms && ms.includes('补发宽限')
    && await page.locator('[data-misfire]').getAttribute('aria-invalid') === 'true', ms ?? '')
  check('没有页面报错', !errors.length, errors.join(' ｜ '))
})

await section('详情：保存途中又改了别的', async () => {
  const { page, sent, errors, state } = await open('/schedules/s-weekly')
  await page.locator('[data-schedule-form]').waitFor()
  // PATCH 晚 1.2 秒才回，期间改名字
  state.override = async (method, path) => {
    if (method === 'PATCH' && path === '/schedules/s-weekly') await sleep(1200)
    return null
  }
  const SECRET_URL = 'https://open.feishu.cn/open-apis/bot/v2/hook/secret-token-zz99'
  await page.locator('[data-webhook-replace]').click()
  await page.locator('[data-webhook-url]').fill(SECRET_URL)
  await page.locator('[data-schedule-save]').click()
  await until(() => writes(sent, /^PATCH /).length === 1)
  await page.locator('[data-field="name"]').fill('途中改名')
  await until(async () => !(await page.locator('[data-schedule-save]').innerText()).includes('正在保存'), 5000)
  await sleep(200)
  const values = await page.locator('[data-schedules-page] input').evaluateAll((els) => els.map((e) => e.value))
  check('保存后地址只显示 url_hint，输入框里不留含令牌的完整地址', (await text(page, '[data-webhook-url-hint]').catch(() => '')).includes('…zz99')
    && !values.some((v) => v.includes('secret-token')), values.filter((v) => v.includes('http')).join(' ｜ '))
  check('途中改的名字还在、仍算未保存', await page.locator('[data-field="name"]').inputValue() === '途中改名'
    && !(await page.locator('[data-schedule-save]').isDisabled()))
  check('……Webhook 这一块不算未保存：可以发测试消息', !(await page.locator('[data-webhook-test]').isDisabled()))
  state.override = null
  await page.locator('[data-schedule-save]').click()
  await until(() => writes(sent, /^PATCH /).length === 2)
  await saveDone(page)
  const p2 = writes(sent, /^PATCH /)[1]?.body
  check('下一次保存只交名字，不再带地址', JSON.stringify(p2) === JSON.stringify({ name: '途中改名', version: 4 }), JSON.stringify(p2))
  check('没有页面报错', !errors.length, errors.join(' ｜ '))
})

await section('详情：晚到的刷新与先开触发历史', async () => {
  const state = freshState()
  const { page, sent, errors } = await open('/schedules/s-weekly', { state })
  await page.locator('[data-schedule-form]').waitFor()
  const toggle = page.locator('header [data-schedule-toggle]')
  state.override = slowGet(state, '/schedules/s-weekly', (st) => st.schedules.find((s) => s.id === 's-weekly'))
  const gets = () => sent.filter((s) => s.key === 'GET /schedules/s-weekly').length
  const before = gets()
  // 立即运行之后刷新一次：读到开关之前的快照，晚 1.5 秒才回
  await page.locator('header [data-schedule-run]').click()
  await until(() => gets() > before)
  await until(async () => !(await page.locator('header [data-schedule-run]').isDisabled()))
  await toggle.click()
  check('页头开关先变成关', await until(async () => (await toggle.getAttribute('aria-checked')) === 'false'))
  await sleep(1900)
  check('晚到的刷新不把页头开关盖回去', await toggle.getAttribute('aria-checked') === 'false', `aria-checked=${await toggle.getAttribute('aria-checked')}`)
  state.override = null
  await toggle.click()
  await until(() => writes(sent, /^PATCH /).length === 2)
  const p = writes(sent, /^PATCH /).map((x) => x.body)
  check('再点页头开关带开关之后的版本（4）', JSON.stringify(p[1]) === JSON.stringify({ enabled: true, version: 4 }), JSON.stringify(p))
  check('没有页面报错', !errors.length, errors.join(' ｜ '))

  // 先开触发历史，期间别处改过、页头刷新过；再切到设置：表单是刷新后的内容，保存就该带这一版
  const st2 = freshState()
  const h = await open('/schedules/s-weekly?tab=history', { state: st2 })
  await h.page.locator('[data-fire-id]').first().waitFor()
  st2.schedules = st2.schedules.map((s) => (s.id === 's-weekly' ? { ...s, version: 9, name: '别处改过的名字' } : s))
  await h.page.locator('header [data-schedule-run]').click()
  await until(async () => (await h.page.locator('[data-schedules-page] header').innerText()).includes('别处改过的名字'))
  await h.page.locator('[role=tab]', { hasText: '设置' }).click()
  await h.page.locator('[data-schedule-form]').waitFor()
  check('切到设置：表单是刷新后的内容', await h.page.locator('[data-field="name"]').inputValue() === '别处改过的名字')
  await h.page.locator('[data-misfire]').selectOption('86400')
  await h.page.locator('[data-schedule-save]').click()
  await until(() => writes(h.sent, /^PATCH /).length === 1)
  await saveDone(h.page)
  const hp = writes(h.sent, /^PATCH /)[0]?.body
  check('保存带的是表单所基于的那一版（9），不撞假的 409', JSON.stringify(hp) === JSON.stringify({ misfire_grace_s: 86400, version: 9 }), JSON.stringify(hp))
  check('……没有冲突提示', await h.page.locator('[data-schedule-conflict]').count() === 0)
  check('没有页面报错', !h.errors.length, h.errors.join(' ｜ '))
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
  // 一行里给人看的字：正文连同各处悬停。错过、没有补发的原因只能是本条记的那一种，页面不能另替它认定一种
  const said = async (id) => [await row(id).innerText(),
    ...await row(id).locator('[title]').evaluateAll((els) => els.map((e) => e.getAttribute('title')))].join(' ｜ ').replace(/\s+/g, ' ')
  for (const [id, label, own, other] of [['f03', '宽限内积压的', '积压的触发不补', '超出补发宽限'], ['f05', '超出宽限的', '超出补发宽限', '积压']]) {
    const k = row(id).locator('[data-fire-kind-chip]')
    const kt = (await k.getAttribute('title')) ?? ''
    const t = await row(id).innerText()
    check(`错过已跳过（${label}）：写本条的原因`, t.includes('错过已跳过') && t.includes(own), t.replace(/\s+/g, ' '))
    check(`错过已跳过（${label}）：类型写「定时」，悬停说没有补发、指向本条的原因，不说补发了一次`, (await k.innerText()).trim() === '定时'
      && !kt.includes('补发的一次') && kt.includes('没有补发') && kt.includes('原因见本条记录'), `${await k.innerText()} ｜ ${kt}`)
    const all = await said(id)
    check(`错过已跳过（${label}）：整行连同悬停都不说另一种原因（${other}）`, !all.includes(other), all)
  }
  const s6 = await said('f06')
  check('错过已跳过、没有记原因：不写原因行，悬停不替它认定原因、不指向不存在的原因', s6.includes('错过已跳过')
    && !s6.includes('原因：') && !s6.includes('超出补发宽限') && !s6.includes('积压') && !s6.includes('原因见'), s6)
  const st6 = (await row('f06').locator('[data-fire-status-chip]').getAttribute('title')) ?? ''
  check('……结局的悬停写这种结局的一般说明', st6.includes('错过') && st6.includes('没有发起'), st6)
  const s7 = await said('f07')
  const st7 = (await row('f07').locator('[data-fire-status-chip]').getAttribute('title')) ?? ''
  check('发起失败、没有记原因：悬停说清没有原因，不指向不存在的原因', s7.includes('发起失败') && !s7.includes('原因：')
    && !s7.includes('原因见') && st7.includes('未能发起运行') && st7.includes('未提供原因'), s7)
  check('发起失败写原因', (await row('f04').innerText()).includes('发起失败') && (await row('f04').innerText()).includes('还没有发布'))
  const when = row('f05').locator('time')
  check('计划时间悬停看时区', ((await when.getAttribute('title')) ?? '').includes('Asia/Shanghai'), (await when.getAttribute('title')) ?? '')
  const shownAt = await page.locator('[data-fire-id] time').allInnerTexts()
  const wantAt = makeFires().slice(0, 20).map((f) => zoned(Date.parse(f.fire_at), 'Asia/Shanghai'))
  check(`计划时间按定时任务的时区（Asia/Shanghai，不是本机的 ${LOCAL_TZ}）写：20 行逐条对得上、都是周一 09:00`,
    shownAt.length === 20 && shownAt.every((t, i) => t === wantAt[i]) && wantAt.every((w) => w.endsWith('周一 09:00')),
    shownAt.map((t, i) => (t === wantAt[i] ? null : `${t} ≠ ${wantAt[i]}`)).filter(Boolean).slice(0, 3).join('；'))
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

await section('详情：触发历史里的「与上一次对比」', async () => {
  const { page, sent, errors } = await open('/schedules/s-weekly?tab=history')
  await page.locator('[data-fire-id]').first().waitFor()
  const row = (id) => page.locator(`[data-fire-id="${id}"]`)
  const link = row('f00').locator('[data-fire-compare]')
  const label = await link.locator('[data-fire-compare-label]').innerText().catch(() => '没有链接')
  check('有对比的那次：给「与上一次对比」（和运行详情「更多」菜单同一个叫法）', (await link.count()) === 1
    && label.trim() === '与上一次对比', label)
  check('……链到对比页，本次是这次的运行、基准是服务端比过的那一次',
    await link.getAttribute('href') === '/runs/compare?target=run-f00&base=run-f02', (await link.getAttribute('href')) ?? '')
  const count = (await link.locator('[data-fire-compare-count]').innerText().catch(() => '')).trim()
  check('……旁边看得见有几个指标有变化（触屏、键盘看不到悬停）', count === '6 个指标有变化', count)
  const title = (await link.getAttribute('title')) ?? ''
  check('……悬停写各类指标的个数：三类总写，其余有才写，没有数值', title === COMPARE_F00_TITLE, title)
  const f08 = row('f08').locator('[data-fire-compare]')
  check('两边都没有指标：悬停不写一串 0 个，旁边也不写计数', ((await f08.getAttribute('title')) ?? '')
    === '与上一次相比：两次运行都没有指标' && await f08.locator('[data-fire-compare-count]').count() === 0)
  const f10 = row('f10').locator('[data-fire-compare]')
  const t10 = (await f10.getAttribute('title').catch(() => '')) ?? ''
  check('上一次失败了、基准是再往前成功的那次：悬停写明是哪一次', t10.startsWith('与上一次成功的运行（#run-f1）相比：只数据变 1 个'), t10)
  // 触屏没有悬停、键盘聚焦也不显示 title：比的不是上一次这件事得写在看得见的字里
  const l10 = f10.locator('[data-fire-compare-label]')
  check('……链接上看得见的字写「与上一次成功的运行对比」，不只靠悬停', (await l10.innerText().catch(() => '')).trim() === '与上一次成功的运行对比'
    && await l10.isVisible(), (await l10.innerText().catch(() => '没有')).trim())
  check('……紧挨着的上一次那一行照旧写「与上一次对比」', (await row('f00').locator('[data-fire-compare-label]').innerText()).trim() === '与上一次对比')
  const linked = await page.locator('[data-fire-compare]').evaluateAll((els) => els.map((e) => e.closest('[data-fire-id]')?.getAttribute('data-fire-id')))
  check('没有对比的（compare 为 null、老服务端不给、没有运行）不给链接', JSON.stringify(linked) === '["f00","f08","f10"]', JSON.stringify(linked))
  check('运行链接照旧在', await row('f00').locator('a[href="/runs/run-f00"]').count() === 1)
  const presses = await tabTo(page, '[data-fire-id="f00"] [data-fire-compare]')
  check('键盘：Tab 到得了「与上一次对比」', presses > 0, `按了 ${presses} 次`)
  await shot(page, 'history-compare-focus-dark')
  await page.keyboard.press('Enter')
  const url = await until(() => {
    const u = new URL(page.url())
    return u.pathname === '/runs/compare' ? u : null
  })
  check('……回车打开对比页，带着本次和基准', !!url && url.searchParams.get('target') === 'run-f00'
    && url.searchParams.get('base') === 'run-f02', page.url())
  // 来历跟着这一次跳转走（路由的 state），对比页据此写「默认基准：同一定时任务上一次成功的运行」
  const via = await page.evaluate(() => history.state?.usr ?? null)
  check('……紧挨着的上一次：带上「同一定时任务」的来历', via?.reason === 'same_schedule' && via?.base === 'run-f02', JSON.stringify(via))

  // 基准不是紧挨着的上一次：来历照样带上。对比页写的是「上一次成功的运行」，比的正是这一次，和从运行详情进去一字不差
  const second = await open('/schedules/s-weekly?tab=history')
  await second.page.locator('[data-fire-id="f10"] [data-fire-compare]').focus()
  const focusedLabel = await second.page.evaluate(() => document.activeElement?.querySelector('[data-fire-compare-label]')?.textContent?.trim() ?? '')
  check('……键盘聚焦在那一行的链接上时，看得见的字就是「与上一次成功的运行对比」', focusedLabel === '与上一次成功的运行对比', focusedLabel)
  await second.page.keyboard.press('Enter')
  await until(() => new URL(second.page.url()).pathname === '/runs/compare')
  const via10 = await second.page.evaluate(() => history.state?.usr ?? null)
  const reason10 = await until(async () => (await second.page.locator('[data-compare-reason]').innerText().catch(() => '')).trim())
  check('……基准不是紧挨着的上一次：带上来历，对比页写「同一定时任务上一次成功的运行」', new URL(second.page.url()).searchParams.get('base') === 'run-f12'
    && via10?.reason === 'same_schedule' && via10?.base === 'run-f12' && reason10 === REASON_SCHEDULE, `${JSON.stringify(via10)} ${reason10}`)
  const real = [...writes(sent, /./), ...writes(second.sent, /./)].filter((x) => x.key !== 'POST /schedules/preview')
  check('只读：没有写请求', real.length === 0, real.map((x) => x.key).join('、'))
  check('没有页面报错', ![...errors, ...second.errors].length, [...errors, ...second.errors].join(' ｜ '))
})

await section('详情：触发历史和运行详情的「与上一次对比」拿同一个基准', async () => {
  // f10 的上一次（f11）失败了，服务端两处都挑再往前成功的 f12：自动对比和运行详情的默认基准用的是同一个函数
  // （api/compare.py 的 pick_base），基准事后变了的旧对比触发历史不再给，这些由 backend/tests/test_scheduler_compare.py
  // 守。这里守前端：两个入口都原样用服务端给的基准，打开同一对运行、写同一句来历
  const runOf = (id, at) => ({ ...RUNS[0], id, started_by: '定时任务：门店周报 · 每周', created_at: iso(at), started_at: iso(at),
    finished_at: iso(at + 4200), manifest_hash: 'ab'.repeat(32), manifest_seq: 2 })
  const f10 = runOf('run-f10', nextMonday9(-11))
  const f12 = runOf('run-f12', nextMonday9(-13))
  const ref = (r) => ({ id: r.id, workflow_name: r.workflow_name, version: r.version, run_class: r.run_class,
    started_at: r.started_at, finished_at: r.finished_at, schedule_id: r.schedule_id })
  const compareApi = (method, path, body, url) => {
    if (method !== 'GET') return null
    if (path === '/runs/run-f10/compare/base') return { json: { base_run_id: 'run-f12', reason: 'same_schedule', base: ref(f12) } }
    if (path === '/runs/run-f10/compare' && url.searchParams.get('base') === 'run-f12') {
      return { json: { target: ref(f10), base: ref(f12), summary: COMPARE_F10.summary, metrics: [], queries: [], notes: [],
        template: { base_version: 3, target_version: 3, same_graph: true, nodes: [] } } }
    }
    return null
  }
  const stateOf = () => ({ ...freshState({ runs: [...RUNS, f10, f12] }), override: compareApi })
  const landed = (page) => until(() => {
    const u = new URL(page.url())
    return u.pathname === '/runs/compare' ? u : null
  })
  const reasonOf = (page) => until(async () => (await page.locator('[data-compare-reason]').innerText().catch(() => '')).trim())

  const menu = await open('/runs/run-f10', { state: stateOf() })
  await menu.page.locator('[data-run-detail="run-f10"]').waitFor({ timeout: 10000 })
  await menu.page.getByRole('button', { name: '更多操作' }).click()
  await menu.page.locator('[data-menu=compare]').click()
  const a = await landed(menu.page)
  const hist = await open('/schedules/s-weekly?tab=history', { state: stateOf() })
  const link = hist.page.locator('[data-fire-id="f10"] [data-fire-compare]')
  await link.waitFor()
  const label = (await link.locator('[data-fire-compare-label]').innerText()).trim()
  await link.click()
  const b = await landed(hist.page)
  check('运行详情「更多」和触发历史两个入口：打开同一对运行（本次 run-f10，基准是跳过失败那次的 run-f12）',
    !!a && !!b && a.searchParams.get('target') === 'run-f10' && b.searchParams.get('target') === 'run-f10'
    && a.searchParams.get('base') === 'run-f12' && b.searchParams.get('base') === 'run-f12', `${a?.search} | ${b?.search}`)
  const [ra, rb] = [await reasonOf(menu.page), await reasonOf(hist.page)]
  check('……两处写的默认基准一字不差：同一定时任务上一次成功的运行', ra === REASON_SCHEDULE && rb === REASON_SCHEDULE, `${ra} | ${rb}`)
  check('……触发历史上看得见的字说明比的是上一次成功的运行', label === '与上一次成功的运行对比', label)
  const tiles = (page) => until(async () => (await page.locator('[data-compare-summary] [data-compare-class]').evaluateAll((els) =>
    els.map((e) => `${e.getAttribute('data-compare-class')}=${e.querySelector('[data-compare-count]')?.textContent?.trim()}`))).join(' '))
  const [ta, tb] = [await tiles(menu.page), await tiles(hist.page)]
  check('……两边的变化摘要一致', !!ta && ta === tb, `${ta} | ${tb}`)
  const real = [...writes(menu.sent, /./), ...writes(hist.sent, /./)]
  check('只读：没有写请求', real.length === 0, real.map((x) => x.key).join('、'))
  check('没有页面报错', ![...menu.errors, ...hist.errors].length, [...menu.errors, ...hist.errors].join(' ｜ '))
})

// ---------------------------------------------------------------- 运行详情

/**
 * 运行详情页头元信息那一行：定时任务的链接（或删除后的纯文本）可见的宽度够不够写下图标加「由定时任务」五个字（fits），
 * 「N 条事件」是不是还在一行里（eventsFlat，以前被挤成一字一行），页头有没有横向溢出，链接在不在视口里，署名在不在
 */
const headRow = (page) => page.evaluate(() => {
  const head = document.querySelector('[data-run-detail] header')
  const link = head?.querySelector('[data-run-schedule-link], [data-run-schedule-gone]')
  if (!head || !link) return null
  const font = parseFloat(getComputedStyle(link).fontSize)
  const icon = link.querySelector('svg')?.getBoundingClientRect().width ?? 0
  const l = link.getBoundingClientRect()
  const events = [...head.querySelectorAll('span')].find((el) => /^[\d,]+ 条事件$/.test(el.textContent.trim()))
  const eh = events?.getBoundingClientRect().height ?? 0
  return { width: Math.round(l.width), need: Math.round(icon + 4 + font * 5), fits: l.width >= icon + 4 + font * 5 - 0.5,
    eventsFlat: !!events && eh > 0 && eh < font * 2.2, scroll: head.scrollWidth - head.clientWidth,
    inView: l.left >= 0 && l.right <= innerWidth + 0.5 && l.width > 0, starter: head.innerText.includes('检查员甲 发起') }
})

await section('运行详情：页头的定时任务链接', async () => {
  const writesSeenExtra = []
  const errorsExtra = []
  const state = freshState({ runs: [...RUNS, RUN_MANUAL_FIRE] })
  const { page, sent, errors } = await open('/runs/run-sched-1', { state })
  await page.locator('[data-run-detail="run-sched-1"]').waitFor({ timeout: 10000 })
  const head = page.locator('[data-run-detail] header').first()
  const link = head.locator('[data-run-schedule-link]')
  // 署名里记的是旧名字：取到定时任务列表之后写现在的名字
  const said = await until(async () => {
    const t = (await link.innerText().catch(() => '')).trim()
    return t === '由定时任务「门店周报 · 每周」发起' ? t : null
  })
  check('页头写「由定时任务「X」发起」，X 是定时任务现在的名字', !!said, (await link.innerText().catch(() => '没有链接')).trim())
  check('……是到那个定时任务的真链接', (await link.evaluate((el) => el.tagName)) === 'A'
    && await link.getAttribute('href') === '/schedules/s-weekly', (await link.getAttribute('href')) ?? '')
  const headText = await head.innerText()
  check('……不再另写「定时任务：X 发起」', !headText.includes('定时任务：') && !headText.includes('旧名'), oneLine([headText]).slice(0, 160))
  check('……悬停写全名和点开去哪', ((await link.getAttribute('title')) ?? '').includes('门店周报 · 每周'))
  check('……前面带和「定时」标记一样的日历图标，平时看得出能点', await link.locator('svg').count() === 1)
  const presses = await tabTo(page, '[data-run-detail] header [data-run-schedule-link]')
  check('键盘：Tab 到得了页头的定时任务链接', presses > 0, `按了 ${presses} 次`)
  const ring = await page.evaluate(() => getComputedStyle(document.activeElement).boxShadow)
  check('……聚焦时有焦点环', !!ring && ring !== 'none', ring)
  const darkTone = await readable(page, '[data-run-detail] header [data-run-schedule-link]')
  check('深色：页头的定时任务链接对比度 ≥ 4.5', !!darkTone && darkTone.ratio >= 4.5, JSON.stringify(darkTone))
  await shot(page, 'run-detail-schedule-link-dark')
  await page.keyboard.press('Enter')
  check('……回车去那个定时任务', await until(() => new URL(page.url()).pathname === '/schedules/s-weekly'), page.url())
  await page.locator('[data-schedule-form], [data-schedules-page] header').first().waitFor()

  const manual = await open('/runs/run-sched-2', { state: freshState({ runs: [...RUNS, RUN_MANUAL_FIRE] }) })
  await manual.page.locator('[data-run-detail="run-sched-2"]').waitFor({ timeout: 10000 })
  const mHead = manual.page.locator('[data-run-detail] header').first()
  const mLink = mHead.locator('[data-run-schedule-link]')
  await until(async () => (await mLink.innerText().catch(() => '')).includes('门店周报 · 每周'))
  const mText = oneLine([await mHead.innerText()])
  check('有人署名点了立即运行：署名照写，后面接「来自定时任务「X」」的链接', mText.includes('检查员甲 发起')
    && (await mLink.innerText()).trim() === '来自定时任务「门店周报 · 每周」' && await mLink.getAttribute('href') === '/schedules/s-weekly',
  mText.slice(0, 160))

  await shot(manual.page, 'run-detail-manual-fire-dark')
  const light = await open('/runs/run-sched-1', { state: freshState({ runs: [...RUNS, RUN_MANUAL_FIRE] }), theme: 'light' })
  await light.page.locator('[data-run-detail="run-sched-1"] header [data-run-schedule-link]').waitFor()
  const lightTheme = await light.page.evaluate(() => document.documentElement.dataset.theme)
  const lightTone = await readable(light.page, '[data-run-detail] header [data-run-schedule-link]')
  check('浅色：页头的定时任务链接对比度 ≥ 4.5', lightTheme === 'light' && !!lightTone && lightTone.ratio >= 4.5,
    `${lightTheme} ${JSON.stringify(lightTone)}`)
  await shot(light.page, 'run-detail-schedule-link-light')
  const plain = await open('/runs/run-manual-1', { state: freshState({ runs: [...RUNS, RUN_MANUAL_FIRE] }) })
  await plain.page.locator('[data-run-detail="run-manual-1"]').waitFor({ timeout: 10000 })
  check('手动发起的运行：页头没有定时任务链接', await plain.page.locator('[data-run-detail] header [data-run-schedule-link]').count() === 0)

  // 名字很长、详情区不宽时链接截断，不把页头撑宽（全名在悬停里）
  const longName = freshState({ runs: [...RUNS, RUN_MANUAL_FIRE] })
  longName.schedules = longName.schedules.map((x) => (x.id === 's-weekly' ? { ...x, name: '华东区全部门店按品类与渠道拆分的周度销售与退款复盘' } : x))
  const narrow = await open('/runs/run-sched-1', { state: longName, width: 900 })
  await narrow.page.locator('[data-run-detail="run-sched-1"]').waitFor({ timeout: 10000 })
  const nLink = narrow.page.locator('[data-run-detail] header [data-run-schedule-link]')
  await until(async () => (await nLink.innerText().catch(() => '')).includes('复盘'))
  const fit = await narrow.page.evaluate(() => {
    const head = document.querySelector('[data-run-detail] header')
    const link = head?.querySelector('[data-run-schedule-link]')
    const label = link?.querySelector('span')
    if (!head || !link || !label) return null
    const h = head.getBoundingClientRect()
    const l = link.getBoundingClientRect()
    return { headRight: Math.round(h.right), linkRight: Math.round(l.right), scroll: head.scrollWidth - head.clientWidth,
      clipped: label.scrollWidth > label.clientWidth }
  })
  check('长名字：链接截断，不超出页头、页头不横向溢出', !!fit && fit.linkRight <= fit.headRight && fit.scroll <= 1 && fit.clipped,
    JSON.stringify(fit))
  // 署名的立即运行（署名加链接，这一行最挤）配长名字：链接照样截断，署名不被挤掉
  const crowdedState = freshState({ runs: [...RUNS, RUN_MANUAL_FIRE] })
  crowdedState.schedules = longName.schedules
  const crowded = await open('/runs/run-sched-2', { state: crowdedState, width: 900 })
  await crowded.page.locator('[data-run-detail="run-sched-2"]').waitFor({ timeout: 10000 })
  const cLink = crowded.page.locator('[data-run-detail] header [data-run-schedule-link]')
  await until(async () => (await cLink.innerText().catch(() => '')).includes('复盘'))
  const crowd = await headRow(crowded.page)
  check('……署名的立即运行配长名字：署名照写，链接截断、看得到，页头不横向溢出', !!crowd && crowd.starter
    && crowd.fits && crowd.scroll <= 1 && crowd.inView, JSON.stringify(crowd))

  // 分屏常见的窄窗口：探索运行的页头多一个「提取为草稿」，这一行最挤。以前这一行不换行、只有链接能缩，820 宽时链接
  // 只剩「由…」，768 宽时 0 宽，Tab 停在一个看不见的链接上，「2 条事件」被压成竖排
  const tight = []
  for (const [width, run, extract] of [[820, RUN_EXPLORE, true], [820, RUN_EXPLORE_MANUAL, true],
    [768, RUN_EXPLORE, true], [768, RUN_EXPLORE_MANUAL, true], [768, RUN_MANUAL_FIRE, false]]) {
    const one = await open(`/runs/${run.id}`, { state: freshState({ runs: [...RUNS, RUN_MANUAL_FIRE, RUN_EXPLORE, RUN_EXPLORE_MANUAL] }), width })
    await one.page.locator(`[data-run-detail="${run.id}"] header [data-run-schedule-link]`).waitFor({ timeout: 10000 })
    await until(async () => (await one.page.locator('[data-run-detail] header [data-run-schedule-link]').innerText()).includes('门店周报 · 每周'))
    const got = await headRow(one.page)
    const hasExtract = await one.page.locator('[data-run-detail] header [data-action="extract"]').count() === 1
    const manual = run.started_by === '检查员甲'
    const ok = !!got && got.fits && got.eventsFlat && got.scroll <= 1 && got.inView && hasExtract === extract
      && (!manual || got.starter)
    tight.push(ok)
    check(`${width} 宽、${run.run_class === 'exploratory' ? '探索' : '正式'}运行（${manual ? '署名点了立即运行' : '到点发起'}）：链接至少写得下「由定时任务」，「条事件」不被压成竖排`,
      ok, JSON.stringify({ ...got, hasExtract }))
    const presses = await tabTo(one.page, '[data-run-detail] header [data-run-schedule-link]')
    if (width === 768 && manual && extract) {
      check('……最挤的那种 Tab 到链接时看得见（有宽度、在视口里）', presses > 0 && !!got && got.width > 0 && got.inView, `按了 ${presses} 次`)
      await shot(one.page, 'run-detail-schedule-link-768')
    }
    writesSeenExtra.push(...writes(one.sent, /./))
    errorsExtra.push(...one.errors)
  }

  // 定时任务已经删了：取回的列表里没有它，不再链过去（只会落到「不存在」页），写成纯文本，和「工作流已删除」一样
  const goneState = freshState({ runs: [...RUNS, RUN_MANUAL_FIRE] })
  goneState.schedules = goneState.schedules.filter((x) => x.id !== 's-weekly')
  const gone = await open('/runs/run-sched-1', { state: goneState })
  await gone.page.locator('[data-run-detail="run-sched-1"]').waitFor({ timeout: 10000 })
  const goneEl = gone.page.locator('[data-run-detail] header [data-run-schedule-gone]')
  await goneEl.waitFor({ timeout: 6000 }).catch(() => {})
  check('定时任务已删除：写「由定时任务「X」发起（已删除）」，不是链接', (await goneEl.count()) === 1
    && (await goneEl.innerText()).trim() === '由定时任务「门店周报（旧名）」发起（已删除）'
    && await gone.page.locator('[data-run-detail] header [data-run-schedule-link]').count() === 0
    && (await goneEl.evaluate((el) => el.tagName)) !== 'A', (await goneEl.innerText().catch(() => '没有')).trim())
  check('……悬停说已删除、运行记录仍然保留', ((await goneEl.getAttribute('title')) ?? '').includes('已删除'))
  const goneManual = await open('/runs/run-sched-2', { state: goneState })
  await goneManual.page.locator('[data-run-detail="run-sched-2"]').waitFor({ timeout: 10000 })
  const gmEl = goneManual.page.locator('[data-run-detail] header [data-run-schedule-gone]')
  await gmEl.waitFor({ timeout: 6000 }).catch(() => {})
  check('……署名点了立即运行的：署名照写，后面是「来自已删除的定时任务」', oneLine([await goneManual.page.locator('[data-run-detail] header').first().innerText()]).includes('检查员甲 发起')
    && (await gmEl.innerText().catch(() => '')).trim() === '来自已删除的定时任务', (await gmEl.innerText().catch(() => '没有')).trim())
  // 列表取不到（老服务端、暂时连不上）不算删除：照样是链接，用署名里记的名字
  const downState = freshState({ runs: [...RUNS, RUN_MANUAL_FIRE] })
  downState.override = (method, path) => (method === 'GET' && path === '/schedules' ? { status: 503, json: { detail: '暂时不可用' } } : null)
  const down = await open('/runs/run-sched-1', { state: downState })
  const downLink = down.page.locator('[data-run-detail="run-sched-1"] header [data-run-schedule-link]')
  await downLink.waitFor({ timeout: 10000 })
  await sleep(300)
  check('……定时任务列表取不到时不算删除，照样是链接', await down.page.locator('[data-run-detail] header [data-run-schedule-gone]').count() === 0
    && (await downLink.innerText()).trim() === '由定时任务「门店周报（旧名）」发起')
  writesSeenExtra.push(...writes(gone.sent, /./), ...writes(goneManual.sent, /./), ...writes(down.sent, /./))
  errorsExtra.push(...gone.errors, ...goneManual.errors, ...down.errors)
  // 回车打开的定时任务页会请求预览（POST，但只读），不算写
  const writesSeen = [...writes(sent, /./), ...writes(manual.sent, /./), ...writes(light.sent, /./), ...writes(plain.sent, /./),
    ...writes(narrow.sent, /./), ...writes(crowded.sent, /./), ...writesSeenExtra]
    .filter((x) => x.key !== 'POST /schedules/preview')
  check('只读：没有写请求', writesSeen.length === 0, writesSeen.map((x) => x.key).join('、'))
  const all = [...errors, ...manual.errors, ...light.errors, ...plain.errors, ...narrow.errors, ...crowded.errors, ...errorsExtra]
  check('没有页面报错', !all.length, all.join(' ｜ '))
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
  // 整行是打开运行的按钮，按钮里放不了可聚焦的链接：至少让读屏在这一行的名字里听到来源
  const rowName = page.getByRole('button', { name: /由定时任务「门店周报 · 每周」发起/ })
  check('读屏：整行按钮的名字里带上是哪个定时任务发起的', await until(async () => (await rowName.count()) === 1)
    && (await rowName.getAttribute('data-run-id')) === 'run-sched-1')
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
    // 换成没发布的库存日报、选正式运行：运行类别下面的说明、必填说明在窄屏上也排得开
    await form.page.locator('[data-workflow-picker] input').click()
    await form.page.locator('[data-workflow-option="wf-stock"]').click()
    await form.page.locator('[data-run-class="formal"]').click()
    await form.page.locator('[data-run-class-problem]').waitFor()
    check(`${theme}：运行类别的问题说明不横向溢出、在屏幕里`, await overflow(form.page) <= 1
      && await inViewX(form.page, '[data-run-class-problem]'), `${await overflow(form.page)}px`)
    const tone = await form.page.evaluate(() => {
      const el = document.querySelector('[data-run-class-problem]')
      return { fg: getComputedStyle(el).color, bg: getComputedStyle(document.querySelector('[data-schedule-form] section')).backgroundColor }
    })
    check(`${theme}：问题说明的字和底色分得开`, tone.fg !== tone.bg, JSON.stringify(tone))
    await form.page.locator('[data-run-class-problem]').scrollIntoViewIfNeeded()
    await shot(form.page, `new-390-problem-${theme}`)

    const hist = await open('/schedules/s-weekly?tab=history', { ...narrow, theme })
    await hist.page.locator('[data-fire-id]').first().waitFor()
    check(`${theme}：触发历史不横向溢出`, await overflow(hist.page) <= 1, `${await overflow(hist.page)}px`)
    check(`${theme}：「与上一次对比」和旁边的计数在屏幕里`, await inViewX(hist.page, '[data-fire-id="f00"] [data-fire-compare]')
      && await inViewX(hist.page, '[data-fire-id="f00"] [data-fire-compare-count]'))
    check(`${theme}：更长的「与上一次成功的运行对比」连同计数也在屏幕里`, await inViewX(hist.page, '[data-fire-id="f10"] [data-fire-compare]')
      && await inViewX(hist.page, '[data-fire-id="f10"] [data-fire-compare-count]'))
    const cmp = await readable(hist.page, '[data-fire-id="f00"] [data-fire-compare]')
    check(`${theme}：「与上一次对比」对比度 ≥ 4.5`, !!cmp && cmp.ratio >= 4.5, JSON.stringify(cmp))
    const cnt = await readable(hist.page, '[data-fire-id="f00"] [data-fire-compare-count]')
    check(`${theme}：旁边的计数对比度 ≥ 4.5`, !!cnt && cnt.ratio >= 4.5, JSON.stringify(cnt))
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
