// 审批质询的回归检查：报告上的入口（run/EvidenceDoc.tsx 的句末「质询」、划选原文浮出的「质询」）、问答串和提问框
// （run/Inquiry.tsx）、审批卡上的挂法（run/ApprovalReport.tsx、run/RunPanel.tsx）、运行详情「证据」页签的挂法
// （pages/runs/EvidencePane.tsx），以及运行详情「工件」页签里质询回答的名字（pages/runs/model.ts）和航迹跳转
// （pages/runs/ArtifactsPane.tsx）。
//
// 守的是这些「看着没坏、其实用不了」的地方：悬停、键盘聚焦一句都不出「质询」（或者出了 Tab 却到不了、整份报告多出一串
// Tab 位）、没有可点内容的句子键盘走不到、划选原文不浮出入口（或者带上的原文混进了徽标和「质询」本身）、提交的请求体
// 和约定对不上（多带、少带 quote、approval_id，unit 写错）、回答期间批准 / 驳回还能点、回答回来不挂到那句话下面、
// 进入审批卡时问答串没有恢复、问答串写不出问的是哪一句、回答里的数字点了不去取质询工件（或者每点一次取一次、标不出
// 那一格）、引用报告已有出处的数字点了没反应、查询的 SQL / 行数 / 截断 / 遮罩不显示、触顶和没答成不说、recorded 为假
// 不醒目、409 / 422 不照服务端的中文说明显示（或者问题文字丢了）、Esc 关不掉提问框（或者连带收起了别的、焦点掉到页面上）、
// 已处理的审批和封存了的正式运行还给入口、封存了的探索运行反而没有；同一次运行的几处问答串要对的审批不同、审批列表
// 取不到时后一处一直不列别的审批里问的（头上写着次数，下面一条没有），或者把没能确认的说成「报告其他版本」、不给重试；
// 工件页签里质询的回答只写英文的种类名、不归在「核查记录」、截断截在空格上的问题摘要不补省略号、悬停里没有整句说明、
// 封存后追加的工件给一个跳到运行结束之后的航迹时刻；窄屏（390px）被撑出横向滚动、浅色主题下看不清。
//
// 跑在预览页上：/preview.html?syn=inquiry-*（用例见 frontend/src/dev/fixtures/inquiry.tsx）。夹具在
// frontend/src/dev/fixtures/inquiry.json，报告文档和回答文档是后端 build_catalog + compose_doc 真实组装的（虚构的数据）。
// 质询的三个接口、工件、证据图、片段接口、审批列表、运行的工件列表都在这里伪造；别的写请求一律拦掉并记下来，别的 GET
// 答 404，不碰真实服务。
//
//   node scripts/check-inquiry.mjs
// 对别的实例跑时带上地址：AGENTLAB_WEB=http://localhost:<前端端口> node scripts/check-inquiry.mjs
// INQUIRY_SHOTS=<目录> 时截几张图，供人眼复核。INQUIRY_ONLY=<字> 只跑名字里带这几个字的段（改代码时用）。
import { mkdirSync, readFileSync } from 'node:fs'
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const CHROME = process.env.CHROME_PATH ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const SHOTS = process.env.INQUIRY_SHOTS ?? ''
const ONLY = process.env.INQUIRY_ONLY ?? ''
const root = new URL('..', import.meta.url).pathname
const fx = JSON.parse(readFileSync(`${root}frontend/src/dev/fixtures/inquiry.json`, 'utf8'))
const RUNS = fx.runs
const RUN_CLASS = { [RUNS.approve]: 'exploratory', [RUNS.formal]: 'formal', [RUNS.explore]: 'exploratory', [RUNS.sealed]: 'formal' }
const REPORT = { [RUNS.approve]: fx.reports.approve, [RUNS.formal]: fx.reports.formal, [RUNS.explore]: fx.reports.explore,
  [RUNS.sealed]: fx.reports.sealed }
const [REFUND, NEWCOMERS, MEMBERS, ONLINE] = Object.keys(fx.snapshots)
const BUSY_DETAIL = '正在回答质询，回答完再批准或驳回'
const ERRORS = {
  inquiry_busy: { status: 409, json: { detail: '这次运行另有一条质询正在回答，请稍后再问', code: 'inquiry_busy' } },
  inquiry_not_open: { status: 409, json: { detail: '运行已不在等待这张审批，无法质询', code: 'inquiry_not_open' } },
  inquiry_disabled: { status: 409, json: { detail: '审批质询已在设置中关闭，可在「设置 → 偏好设置 → 核查」中开启', code: 'inquiry_disabled' } },
  invalid: { status: 422, json: { detail: '问题不能为空，最长 1000 字', field: 'question' } },
}

let failed = 0
const check = (name, cond, detail = '') => {
  console.log(`  ${cond ? '✓' : '✗'} ${name}${detail ? ` — ${detail}` : ''}`)
  if (!cond) failed++
}
async function section(name, fn) {
  if (ONLY && !name.includes(ONLY)) return
  console.log(`\n=== ${name} ===`)
  try {
    await fn()
  } catch (e) {
    check(`${name} 中途出错`, false, String(e?.message ?? e).split('\n')[0])
  }
}

const browser = await chromium.launch({ executablePath: CHROME })

/** 一次新问的答复：照夹具里的 answer，问题、句子按这次请求的改 */
const answerFor = (body, extra = {}) => ({
  inquiry: { ...fx.answer, question: body.question, unit: body.unit ?? null, quote: body.quote ?? null,
             approval_id: body.approval_id ?? null, post_seal: !body.approval_id, at: new Date().toISOString() },
  recorded: true, note: null, ...extra,
})

/**
 * 开一个预览用例。列表按运行答夹具里的问答（state.lists 可改），提问按 state.reply 答（{status, json} 或按请求体算的函数；
 * state.gate 是个没兑现的 Promise 时先挂着，测「回答期间」；state.listGate 同样挂着列表）。审批列表按 state.approvals
 * （运行 → 审批数组）答，没有的答 404，state.approvalsGate 同样先挂着；设置按 state.settings 答；state.docs、state.snaps
 * 盖过夹具里的文档、快照；运行的工件列表答夹具里的 run_artifacts（探索运行）。setup 在打开页面之前改 state（进入时就要
 * 取的列表、设置得先备好）；ws 接管运行的事件流。记下列表、提问、取快照、取工件、取审批列表的请求和拦下的写请求
 */
async function open(syn, { width = 1100, height = 900, theme = 'dark', setup, ws, waitUntil = 'networkidle' } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height }, colorScheme: theme, timezoneId: 'Asia/Shanghai' })
  ctx.setDefaultTimeout(6000)
  const page = await ctx.newPage()
  const hits = { lists: [], asks: [], snapshots: [], artifacts: [], segments: [], approvals: [], writes: [], responds: [], tasks: [], other: [] }
  const state = {
    lists: { [RUNS.approve]: fx.items, [RUNS.formal]: fx.items, [RUNS.explore]: fx.explore_items },
    reply: (body) => ({ status: 200, json: answerFor(body) }),
    gate: null,
    listGate: null,
    approvalsGate: null,
    approvals: {},
    settings: {},
    docs: {},
    snaps: {},
  }
  setup?.(state)
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('dialog', (d) => { errors.push(`原生对话框：${d.message()}`); void d.dismiss() })
  await page.route((u) => new URL(u).pathname.startsWith('/api/'), async (route) => {
    const req = route.request()
    const url = new URL(req.url())
    const path = url.pathname
    const method = req.method()
    const inq = path.match(/^\/api\/runs\/([^/]+)\/inquiries$/)
    if (inq && method === 'POST') {
      const body = JSON.parse(req.postData() ?? '{}')
      hits.asks.push({ run: decodeURIComponent(inq[1]), body })
      if (state.gate) await state.gate
      const out = typeof state.reply === 'function' ? state.reply(body) : state.reply
      return route.fulfill({ status: out.status, json: out.json })
    }
    // 审批卡的内容在待办卡里（应用平台 X4）：批准、驳回走 /api/tasks/{id}/respond。预览给合成审批带上了待办
    // （task-<审批 id>，frontend/src/dev/taskOf.ts），这里只记下请求体、回一条凑齐了的待办，不算「写请求」
    const respond = path.match(/^\/api\/tasks\/([^/]+)\/respond$/)
    if (respond && method === 'POST') {
      hits.responds.push({ task: decodeURIComponent(respond[1]), body: JSON.parse(req.postData() ?? '{}') })
      return route.fulfill({ json: { task: { id: decodeURIComponent(respond[1]), status: 'resolved' }, resolved: true } })
    }
    if (method !== 'GET') {
      hits.writes.push(`${method} ${path}`)
      return route.abort()
    }
    // 审批带着待办，审批卡不该再按运行去取待办列表
    if (path === '/api/tasks') {
      hits.tasks.push(url.search)
      return route.fulfill({ json: { items: [] } })
    }
    if (path === '/api/tasks/count') return route.fulfill({ json: { mine: 0, apps: 0, other: 0 } })
    if (inq) {
      const run = decodeURIComponent(inq[1])
      hits.lists.push({ run, report: url.searchParams.get('report'), unit: url.searchParams.get('unit') })
      if (state.listGate) await state.listGate
      return route.fulfill({ json: { items: state.lists[run] ?? [] } })
    }
    if (path === '/api/approvals') {
      const run = url.searchParams.get('run_id')
      hits.approvals.push({ run, status: url.searchParams.get('status') })
      if (state.approvalsGate) await state.approvalsGate
      const list = state.approvals[run]
      return list ? route.fulfill({ json: list }) : route.fulfill({ status: 404, json: { detail: '检查脚本没有伪造这个接口' } })
    }
    if (path === '/api/settings') return route.fulfill({ json: state.settings })
    const snap = path.match(/^\/api\/runs\/([^/]+)\/inquiries\/artifacts\/([^/]+)$/)
    if (snap) {
      const id = decodeURIComponent(snap[2])
      hits.snapshots.push(id)
      if (state.snaps[id]) return route.fulfill({ json: state.snaps[id] })
      const body = fx.snapshots[id]
      if (!body) return route.fulfill({ status: 404, json: { detail: '不是这次运行的质询引用过的工件' } })
      // 约定没定死外面包不包一层：一份包在 {id, content} 里，其余是快照本身，两种界面都得认
      return route.fulfill({ json: id === REFUND ? { id, content: body } : body })
    }
    const art = path.match(/^\/api\/artifacts\/([^/]+)$/)
    if (art) {
      const id = decodeURIComponent(art[1])
      hits.artifacts.push(id)
      const doc = state.docs[id] ?? fx.docs[id]
      return doc ? route.fulfill({ json: { id, content: doc } })
        : route.fulfill({ status: 404, json: { detail: '工件不存在，可能已被删除' } })
    }
    const seg = path.match(/^\/api\/runs\/([^/]+)\/evidence\/segments\/([^/]+)$/)
    if (seg) {
      hits.segments.push(decodeURIComponent(seg[2]))
      return route.fulfill({ json: {} })
    }
    const arts = path.match(/^\/api\/runs\/([^/]+)\/artifacts$/)
    if (arts) return route.fulfill({ json: decodeURIComponent(arts[1]) === RUNS.explore ? fx.run_artifacts : [] })
    const graph = path.match(/^\/api\/runs\/([^/]+)\/evidence$/)
    if (graph) {
      const run = decodeURIComponent(graph[1])
      const r = REPORT[run]
      return route.fulfill({ json: {
        run_id: run, mode: 'cited', seal: { sealed: true, ok: true },
        reports: r ? [{ node_id: 'write', doc_artifact: r.doc_artifact, doc_sealed: true, hash_ok: true, fields: ['report'], stats: r.stats }] : [],
      } })
    }
    const run = path.match(/^\/api\/runs\/([^/]+)$/)
    if (run) {
      const id = decodeURIComponent(run[1])
      return route.fulfill({ json: { id, run_class: RUN_CLASS[id] ?? 'exploratory', status: 'interrupted' } })
    }
    hits.other.push(path)
    return route.fulfill({ status: 404, json: { detail: '检查脚本没有伪造这个接口' } })
  })
  // 事件流：Playwright 靠注入页面的脚本接管 WebSocket，得在打开页面之前挂上
  if (ws) await page.routeWebSocket(/\/api\/runs\/[^/]+\/stream/, ws)
  await page.goto(`${WEB}/preview.html?syn=${syn}&theme=${theme}`, { waitUntil })
  return { ctx, page, hits, state, errors }
}

const text = async (loc) => (await loc.innerText()).replace(/\s+/g, ' ').trim()
const shot = async (page, name) => {
  if (!SHOTS) return
  mkdirSync(SHOTS, { recursive: true })
  await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true })
}
const group = (page, unit) => page.locator(`[data-inquiry-group="${unit}"]`)
const entry = (page, seq) => page.locator(`[data-inquiry-entry="${seq}"]`)
const askBtn = (page, unit) => page.locator(`[data-inquiry-ask="${unit}"]`)
const composer = (page) => page.locator('[data-inquiry-composer]')
/** 一个元素此刻看不看得见：宽度、不透明度（「质询」平时宽 0、透明） */
const seen = (loc) => loc.evaluate((el) => {
  const r = el.getBoundingClientRect()
  return r.width > 4 && Number(getComputedStyle(el).opacity) > 0.9
})
const active = (page) => page.evaluate(() => {
  const el = document.activeElement
  return el ? { ask: el.getAttribute('data-inquiry-ask'), seg: el.getAttribute('data-seg'), tag: el.tagName,
    input: el.hasAttribute('data-inquiry-input'), entry: el.closest('[data-inquiry-entry]')?.getAttribute('data-inquiry-entry') ?? null,
    pending: el.hasAttribute('data-inquiry-pending'), report: el.hasAttribute('data-inquiry-ask-report') } : null
})
const approve = (page) => page.locator('[data-approval]').getByRole('button', { name: '批准', exact: true })
const reject = (page) => page.locator('[data-approval]').getByRole('button', { name: '驳回', exact: true })
/** 展开审批卡里的逐段证据，等报告文档画出来 */
async function expand(page) {
  await page.locator('[data-approval-evidence]').click()
  await page.locator('[data-approval-evidence-doc] [data-evidence-doc] [data-block]').first().waitFor()
}
/** 鼠标放到某一句第一行的字上（句子会折行：包住它的框的左上角可能落在上一句上） */
async function hoverUnit(page, unit, scope = page) {
  const at = await scope.locator(`[data-ev-unit="${unit}"]`).evaluate((el) => {
    el.scrollIntoView({ block: 'center' })
    const r = el.getClientRects()[0]
    return { x: r.left + Math.min(8, r.width / 2), y: r.top + r.height / 2 }
  })
  await page.mouse.move(at.x, at.y)
}
/** 像人一样点某一句的「质询」：先把鼠标移到这一句的字上（「质询」这才显出来），再点它。scope：同一页有两份报告时限定在哪一份 */
async function clickAsk(page, unit, scope = page) {
  await hoverUnit(page, unit, scope)
  await scope.locator(`[data-inquiry-ask="${unit}"]`).click()
}
/** 一次审批（审批列表接口的一行）：审的是哪一稿报告 */
const approvalRow = (id, run, doc) => ({ id, run_id: run, node_id: 'review', mode: 'approve', status: 'approved', response: {},
  payload: { kind: 'human_node', reports: [{ node_id: 'write', doc_artifact: doc }] }, created_at: '2026-10-05T06:00:00' })
const clone = (x) => JSON.parse(JSON.stringify(x))
/** 问答串的分组：每组是哪一句、抬头写的什么、有哪几条 */
const groupsOf = (page, scope = '[data-inquiry="write"]') => page.locator(`${scope} [data-inquiry-group]`).evaluateAll((els) => els.map((el) => ({
  unit: el.getAttribute('data-inquiry-group'), about: el.querySelector('[data-inquiry-about]')?.textContent?.trim() ?? '',
  entries: [...el.querySelectorAll('[data-inquiry-entry]')].map((x) => x.getAttribute('data-inquiry-entry')),
})))
/** 报告正文里每个片段、每句话、每个「质询」的位置（第一个框），悬停前后对比用 */
const layout = (page, root = '[data-evidence-doc]') => page.evaluate((r) => {
  const out = []
  for (const el of document.querySelector(r)?.querySelectorAll('[data-seg], [data-ev-unit], [data-inquiry-ask], td, th') ?? []) {
    const box = el.getClientRects()[0] ?? el.getBoundingClientRect()
    const key = el.getAttribute('data-seg') ?? (el.hasAttribute('data-inquiry-ask') ? `ask:${el.getAttribute('data-inquiry-ask')}`
      : el.getAttribute('data-ev-unit') ?? `${el.tagName}${out.length}`)
    out.push([key, { x: box.left, y: box.top, w: box.width, h: box.height }])
  }
  return out
}, root).then((list) => new Map(list))
/** 两次位置里挪了的（超过半个像素） */
const moved = (a, b) => [...a].filter(([k, v]) => {
  const w = b.get(k)
  return !w || Math.abs(w.x - v.x) > 0.5 || Math.abs(w.y - v.y) > 0.5 || Math.abs(w.w - v.w) > 0.5
}).map(([k, v]) => `${k} ${Math.round(v.x)},${Math.round(v.y)}→${Math.round(b.get(k)?.x)},${Math.round(b.get(k)?.y)}`)
/** 焦点所在的元素在不在视口里、在不在会裁切它的祖先（限高滚动的报告框）的可见范围里 */
const focusShown = (page) => page.evaluate(() => {
  const el = document.activeElement
  if (!el || el === document.body) return { ok: false, why: 'body' }
  const r = el.getBoundingClientRect()
  if (r.bottom <= 0 || r.top >= innerHeight || r.width === 0) return { ok: false, why: `视口外 ${Math.round(r.top)}` }
  for (let p = el.parentElement; p; p = p.parentElement) {
    const s = getComputedStyle(p)
    if (s.overflowY === 'visible' && s.overflowX === 'visible') continue
    const b = p.getBoundingClientRect()
    if (r.bottom <= b.top || r.top >= b.bottom) return { ok: false, why: `被 ${p.tagName} 裁掉` }
  }
  return { ok: true, ask: el.getAttribute('data-inquiry-ask') }
})
/** 等着某个条件成立（最多 ms 毫秒） */
async function until(fn, ms = 3000) {
  const end = Date.now() + ms
  while (Date.now() < end) {
    if (await fn()) return true
    await new Promise((r) => setTimeout(r, 50))
  }
  return !!(await fn())
}
/** 在某一句里划选一段原文（同用鼠标拖选：浏览器发 selectionchange） */
const select = (page, unit, words) => page.evaluate(([u, w]) => {
  // 没有「质询」的报告上句子不包一层：在整份报告里找这段字
  const el = document.querySelector(`[data-ev-unit="${u}"]`) ?? document.querySelector('[data-evidence-doc] [role="group"]')
  if (!el) return false
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT)
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const i = n.data.indexOf(w)
    if (i < 0) continue
    const range = document.createRange()
    range.setStart(n, i)
    range.setEnd(n, i + w.length)
    const sel = getSelection()
    sel.removeAllRanges()
    sel.addRange(range)
    return true
  }
  return false
}, [unit, words])

await section('审批卡：进入时恢复问答串，按句子挂好', async () => {
  const { ctx, page, hits, errors } = await open('inquiry-approval')
  const box = page.locator('[data-inquiry="write"]')
  await box.locator('[data-inquiry-entry="36"]').waitFor()
  check('问答串挂在报告核对概况里（同一块），标题写审批质询和次数', await page.locator('[data-approval-report="write"] [data-inquiry="write"]').count() === 1
    && (await text(box.locator('[data-inquiry-count]'))) === '· 3 次', await text(box.locator('div').first()))
  check('进入时按列表接口取一次，带上报告节点', hits.lists.length === 1 && hits.lists[0].run === RUNS.approve && hits.lists[0].report === 'write',
    JSON.stringify(hits.lists))
  const groups = await box.locator('[data-inquiry-group]').evaluateAll((els) => els.map((el) => ({
    unit: el.getAttribute('data-inquiry-group'), about: el.querySelector('[data-inquiry-about-text]')?.textContent?.trim() ?? '',
    entries: [...el.querySelectorAll('[data-inquiry-entry]')].map((x) => x.getAttribute('data-inquiry-entry')),
  })))
  check('按句子归拢、按时间排：退款那句、新客那句、枫岭区那句', JSON.stringify(groups.map((g) => [g.unit, g.entries]))
    === JSON.stringify([['u2', ['31']], ['u3', ['34']], ['u4', ['36']]]), JSON.stringify(groups.map((g) => [g.unit, g.entries])))
  check('每组写出问的是哪一句（原文取自报告文档，逐段证据收着也取）', groups[0]?.about === '「退款金额 21,500元，退款率 1.89%。」'
    && groups[1]?.about === '「增长主要来自新客。」' && groups[2]?.about.startsWith('「分片区看，枫岭区销售额最高'),
  groups.map((g) => g.about).join(' / '))
  check('划选原文问的：问题下面写出原文', (await text(entry(page, 36).locator('[data-inquiry-quote]'))) === '原文「枫岭区销售额最高」')
  const e1 = entry(page, 31)
  // 时间按本地时区写（检查固定在 Asia/Shanghai）；不是今天的前面带日期
  const meta = await text(e1.locator('[data-inquiry-meta]'))
  check('问题、提问人和时间', (await text(e1.locator('[data-inquiry-question]'))) === '退款率的分母是含税销售额还是不含税销售额？'
    && meta.startsWith('张工 · ') && meta.endsWith('14:12'), meta)
  check('没署名的写「未署名」', (await text(entry(page, 34).locator('[data-inquiry-meta]'))).startsWith('未署名'))
  await e1.locator('[data-inquiry-answer-doc]').waitFor()
  check('回答按文档画（数字带出处），取了回答文档', (await text(e1.locator('[data-inquiry-answer-doc]'))).includes('两者之比为 0.0189')
    && hits.artifacts.includes(fx.items[0].answer.doc_artifact))
  const cites = await e1.locator('[data-inquiry-cite]').evaluateAll((els) => els.map((el) => [el.getAttribute('data-inquiry-cite'),
    el.querySelector('sup')?.textContent ?? null, el.getAttribute('data-ev-line')]))
  check('质询自己查询里的数挂查询编号、报告已有出处的不挂', JSON.stringify(cites) === JSON.stringify([['Q1', 'Q1', 'solid'],
    ['Q1', 'Q1', 'solid'], ['Q1', 'Q1', 'solid'], ['report', null, 'solid']]), JSON.stringify(cites))
  check('读屏听得到出处', ((await e1.locator('[data-inquiry-cite="Q1"]').first().getAttribute('aria-label')) ?? '')
    .includes('出处：查询 Q1 · 第 1 行 · refund'))
  const e2 = entry(page, 34)
  await e2.locator('[data-inquiry-answer-doc]').waitFor()
  check('裸数字画点状线、挂「无证据」', JSON.stringify(await e2.locator('.ev-mark[data-ev-state="none"]').allInnerTexts()
    .then((t) => t.map((x) => x.replace(/\s*（无证据）$/, '')))) === JSON.stringify(['13%'])
    && (await e2.locator('[data-ev-tag="none"]').count()) === 1)
  check('触顶收尾：写明哪个上限、回答可能不完整', (await text(e2.locator('[data-inquiry-limit]'))) === '已达查询次数上限：回答依据已查到的结果，可能不完整')
  const e3 = entry(page, 36)
  check('没答成：写明原因（服务端的说明），不再另说「没有给出回答」', (await text(e3.locator('[data-inquiry-failed]'))).startsWith('未能回答（已达核查每日金额上限）：今日核查费用已达上限')
    && await e3.locator('[data-inquiry-answer-empty]').count() === 0, await text(e3.locator('[data-inquiry-failed]')))
  check('没答成、没调用过模型：不写「回答由核查模型给出」，也不写花费 $0', await e3.locator('[data-inquiry-judged]').count() === 0
    && !(await text(e3)).includes('$0'), await text(e3))
  check('没查询的写明没有执行查询', (await text(e3.locator('[data-inquiry-queries="0"]'))) === '核查模型没有执行查询')
  check('说清回答是核查模型给的、不是系统核对', (await text(e1.locator('[data-inquiry-judged]'))).startsWith('回答由核查模型「verify-mini」给出，并非系统核对'))
  check('写进了运行记录的不说「没有写入」', await box.locator('[data-inquiry-unrecorded]').count() === 0)
  check('给「就整份报告提问」、没有提问框', await box.locator('[data-inquiry-ask-report]').count() === 1 && await composer(page).count() === 0)
  check('逐段证据收着时不取片段、不取快照', hits.snapshots.length === 0 && hits.segments.length === 0)
  await shot(page, 'approval-dark')
  check('没有发出任何写请求', hits.writes.length === 0, hits.writes.join('、'))
  check('页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('回答里的数字：质询自己的查询取质询工件、标出那一格；查询列表', async () => {
  const { ctx, page, hits, errors } = await open('inquiry-approval')
  const e1 = entry(page, 31)
  await e1.locator('[data-inquiry-answer-doc]').waitFor()
  check('查询列表默认收着，写明共几次', (await text(e1.locator('[data-inquiry-queries-toggle]'))) === '核查模型执行的查询（1 次）'
    && (await e1.locator('[data-inquiry-queries-toggle]').getAttribute('aria-expanded')) === 'false')
  await e1.locator('[data-inquiry-cite="Q1"]').first().click()
  const q1 = e1.locator('[data-inquiry-query="Q1"]')
  await q1.locator('[data-inquiry-snapshot="ok"]').waitFor()
  check('点数字：按质询工件接口取那次查询的快照（包了一层 {id, content} 的也认）', JSON.stringify(hits.snapshots) === JSON.stringify([REFUND]),
    hits.snapshots.join('、'))
  const cell = await q1.locator('[data-highlight="cell"]').allInnerTexts()
  check('标出被引用的那一格（第 1 行 refund）', JSON.stringify(cell) === JSON.stringify(['21500']), JSON.stringify(cell))
  check('焦点落到那次查询上', (await page.evaluate(() => document.activeElement?.closest('[data-inquiry-query]')?.getAttribute('data-inquiry-query'))) === 'Q1')
  check('查询卡：SQL、行数', (await text(q1.locator('[data-inquiry-sql]'))).startsWith('SELECT SUM(r.amount) AS refund')
    && (await text(q1.locator('[data-inquiry-rows]'))) === '1 行')
  await e1.locator('[data-inquiry-cite="Q1"]').nth(2).click()
  await page.waitForTimeout(150)
  check('点同一次查询的另一个数：换高亮的格，不再取快照', JSON.stringify(await q1.locator('[data-highlight="cell"]').allInnerTexts()) === JSON.stringify(['0.0189'])
    && hits.snapshots.length === 1, `${await q1.locator('[data-highlight="cell"]').allInnerTexts()} · ${hits.snapshots.length}`)

  const e2 = entry(page, 34)
  await e2.locator('[data-inquiry-queries-toggle]').click()
  const q2 = e2.locator('[data-inquiry-query="Q2"]')
  check('截断的照实写：行数和「结果已截断」', (await text(q2.locator('[data-inquiry-rows]'))) === '50 行'
    && (await text(q2.locator('[data-inquiry-truncated]'))) === '结果已截断，数据库中还有更多数据')
  await q2.locator('[data-inquiry-result-toggle]').click()
  await q2.locator('[data-inquiry-snapshot="ok"]').waitFor()
  const masked = await q2.locator('td[data-masked]').count()
  check('展开结果：遮罩列写「已遮罩」，并说明', masked === 50 && (await text(q2.locator('[data-inquiry-masked]'))) === 'phone 在数据源中设置了遮罩，不显示原值'
    && !(await text(q2)).includes('000-1000'), `${masked} 格`)
  check('取的是这次查询的快照', hits.snapshots.at(-1) === MEMBERS)
  await q2.locator('[data-inquiry-result-toggle]').click()
  check('收起结果', await q2.locator('[data-inquiry-snapshot]').count() === 0)
  // 第二个数引用的是第二次查询：展开它，焦点跟过去
  await e2.locator('[data-inquiry-cite="Q2"]').click()
  await q2.locator('[data-inquiry-snapshot="ok"]').waitFor()
  check('点第二次查询的数：标出第 1 行 amount', JSON.stringify(await q2.locator('[data-highlight="cell"]').allInnerTexts()) === JSON.stringify(['9000']))
  await shot(page, 'cite-dark')
  check('没有发出任何写请求', hits.writes.length === 0, hits.writes.join('、'))
  check('页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('回答里引用报告已有出处的数字：在报告里打开证据面板', async () => {
  const { ctx, page, hits, errors } = await open('inquiry-approval')
  const e1 = entry(page, 31)
  await e1.locator('[data-inquiry-answer-doc]').waitFor()
  check('逐段证据收着', await page.locator('[data-approval-evidence-doc]').count() === 0)
  await e1.locator('[data-inquiry-cite="report"]').click()
  await page.locator('[data-approval] [data-evidence-panel]').waitFor()
  const opened = await page.evaluate(() => [...document.querySelectorAll('[data-seg][aria-expanded="true"]')].map((el) => el.getAttribute('data-seg')))
  check('展开逐段证据，在引用同一出处的那一处（销售额）打开证据面板', JSON.stringify(opened) === JSON.stringify(['s4']) && hits.segments.includes('s4'),
    JSON.stringify(opened))
  check('焦点落到报告里那一处', (await active(page))?.seg === 's4', JSON.stringify(await active(page)))
  check('不去质询工件接口取', hits.snapshots.length === 0)
  check('页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('入口：悬停、键盘聚焦一句出现「质询」，Tab 到得了、Esc 关得掉', async () => {
  const { ctx, page, hits, errors } = await open('inquiry-approval')
  await expand(page)
  const units = await page.locator('[data-inquiry-ask]').evaluateAll((els) => els.map((el) => el.getAttribute('data-inquiry-ask')))
  check('每句结论后各一个「质询」，标题不挂', JSON.stringify(units) === JSON.stringify(['u1', 'u2', 'u3', 'u4', 'u5', 'u6', 'u7']), JSON.stringify(units))
  const idle = await askBtn(page, 'u2').evaluate((el) => ({ w: el.getBoundingClientRect().width, o: Number(getComputedStyle(el).opacity) }))
  check('平时是句末淡淡的一个小图标（不抢眼，宽度固定）', !(await seen(askBtn(page, 'u2'))) && idle.o < 0.75 && idle.w > 8 && idle.w < 24,
    JSON.stringify(idle))
  const before = await layout(page)
  // 悬停第一句（9 月销售额……），它的「质询」显出来；同一段里后面那一句（退款金额 21,500元……）不能被挤开
  await page.locator('[data-ev-unit="u1"] [data-seg="s4"]').hover()
  await page.waitForTimeout(100)
  const mid = await layout(page)
  check('悬停前后正文一个字都不挪（「质询」显出来只换颜色，不撑开）', moved(before, mid).length === 0, moved(before, mid).slice(0, 3).join('；'))
  // 悬停第一句时看到的后一句的数字，把鼠标移过去：第一句的「质询」收起，数字还在原处，鼠标底下就是它
  const s9 = mid.get('s9')
  await page.mouse.move(s9.x + 4, s9.y + s9.h / 2)
  await page.waitForTimeout(100)
  check('移到悬停时看到的后一句的数字上：鼠标底下就是它', await page.evaluate(([x, y]) => document.elementFromPoint(x, y)?.closest('[data-seg]')?.getAttribute('data-seg'),
    [s9.x + 4, s9.y + s9.h / 2]) === 's9')
  check('悬停一句：这一句的「质询」显出来，别的句子的不显', await seen(askBtn(page, 'u2')) && !(await seen(askBtn(page, 'u1'))))
  check('读屏听得到问的是哪一句', (await askBtn(page, 'u2').getAttribute('aria-label')) === '质询这句：退款金额 21,500元，退款率 1.89%。')
  await page.mouse.move(5, 5)
  await page.waitForTimeout(100)
  // 键盘：焦点落到一句的数字上，「质询」出现，Tab 到它
  await page.locator('[data-seg="s4"]').focus()
  check('焦点在这一句上：「质询」出现，进 Tab 顺序', await seen(askBtn(page, 'u1'))
    && (await askBtn(page, 'u1').getAttribute('tabindex')) === '0' && (await askBtn(page, 'u2').getAttribute('tabindex')) === '-1')
  await page.keyboard.press('Tab')
  check('Tab 到这一句的「质询」', (await active(page))?.ask === 'u1', JSON.stringify(await active(page)))
  await page.keyboard.press('Shift+Tab')
  check('Shift + Tab 回到这一句', (await active(page))?.seg === 's4')
  await page.keyboard.press('ArrowRight')
  await page.keyboard.press('ArrowRight')
  check('方向键走到下一句：Tab 位跟着换到那一句的「质询」', (await active(page))?.seg === 's9'
    && (await askBtn(page, 'u2').getAttribute('tabindex')) === '0' && (await askBtn(page, 'u1').getAttribute('tabindex')) === '-1')
  const tabbable = await page.locator('[data-evidence-doc] [tabindex="0"]').count()
  check('整份报告仍然只占一个 Tab 位，外加这一句的「质询」', tabbable === 2, `${tabbable} 个`)
  await page.keyboard.press('ArrowDown')
  check('没有可点内容的句子（增长主要来自新客）：方向键直接走到它的「质询」，出现', (await active(page))?.ask === 'u3' && await seen(askBtn(page, 'u3')),
    JSON.stringify(await active(page)))
  await page.keyboard.press('Enter')
  await composer(page).waitFor()
  check('回车打开提问框：写明就这句提问、显示这一句', (await composer(page).getAttribute('data-inquiry-composer')) === 'unit'
    && (await text(composer(page).locator('[data-inquiry-target]'))) === '增长主要来自新客。')
  check('提问框挂在这一句的问答下面', await group(page, 'u3').locator('[data-inquiry-composer]').count() === 1)
  check('焦点在问题输入框里', (await active(page))?.input === true)
  await page.keyboard.press('Escape')
  await page.waitForTimeout(100)
  check('Esc 关掉提问框，焦点回到这一句的「质询」', await composer(page).count() === 0 && (await active(page))?.ask === 'u3', JSON.stringify(await active(page)))
  check('Esc 不连带收起别的（逐段证据还开着）', await page.locator('[data-approval-evidence-doc]').count() === 1)
  await clickAsk(page, 'u1')
  await composer(page).waitFor()
  await page.locator('[data-inquiry-input]').fill('销售额有没有扣除优惠券？')
  await clickAsk(page, 'u2')
  await page.waitForTimeout(100)
  check('提问框开着又点另一句：换到那一句，问题留着', (await text(composer(page).locator('[data-inquiry-target]'))) === '退款金额 21,500元，退款率 1.89%。'
    && (await page.locator('[data-inquiry-input]').inputValue()) === '销售额有没有扣除优惠券？' && await group(page, 'u2').locator('[data-inquiry-composer]').count() === 1)
  await composer(page).locator('button', { hasText: '取消' }).click()
  check('取消关掉提问框', await composer(page).count() === 0)
  await shot(page, 'entry-dark')
  check('没有发出任何写请求', hits.writes.length === 0 && hits.asks.length === 0, hits.writes.join('、'))
  check('页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('提问：请求体按约定；回答期间批准 / 驳回不可用；回答挂到那句话下面', async () => {
  const { ctx, page, hits, state, errors } = await open('inquiry-approval')
  await expand(page)
  await page.locator('[data-seg="s4"]').focus()
  await page.keyboard.press('Tab')
  await page.keyboard.press('Enter')
  await composer(page).waitFor()
  check('提交按钮：没写问题时不可用', await page.locator('[data-inquiry-submit]').isDisabled())
  await page.locator('[data-inquiry-input]').fill('这里的销售额包含线上订单吗？')
  let release
  state.gate = new Promise((r) => { release = r })
  await page.locator('[data-inquiry-submit]').click()
  await page.locator('[data-inquiry-pending]').waitFor()
  check('请求体：report、unit、question、approval_id，没划选就不带 quote', JSON.stringify(hits.asks.map((a) => a.body))
    === JSON.stringify([{ report: 'write', unit: 'u1', question: '这里的销售额包含线上订单吗？', approval_id: fx.approval_id }])
    && hits.asks[0].run === RUNS.approve, JSON.stringify(hits.asks))
  check('回答期间：说正在查询与回答，通常需要几十秒', (await text(page.locator('[data-inquiry-pending] [role="status"]'))).startsWith('正在查询与回答，通常需要几十秒')
    && await group(page, 'u1').locator('[data-inquiry-pending]').count() === 1)
  check('回答期间：提问框收起，焦点接到正在回答的那一条上', await composer(page).count() === 0 && (await active(page))?.pending === true,
    JSON.stringify(await active(page)))
  check('回答期间：批准、驳回不可用，并说明原因', await approve(page).isDisabled() && await reject(page).isDisabled()
    && (await text(page.locator('[data-approval-inquiry-busy]'))) === BUSY_DETAIL)
  await clickAsk(page, 'u2')
  await composer(page).waitFor()
  await page.locator('[data-inquiry-input]').fill('第二个问题')
  check('回答期间再开提问框：提交不可用，写明要等这一条答完', await page.locator('[data-inquiry-submit]').isDisabled()
    && (await text(composer(page))).includes('这次运行另有一条质询正在回答，回答完再提问'))
  await composer(page).locator('button', { hasText: '取消' }).click()
  await shot(page, 'pending-dark')
  release()
  await group(page, 'u1').locator('[data-inquiry-entry="40"]').waitFor()
  check('回答回来：挂在问的那一句下面，按文档画', (await text(entry(page, 40).locator('[data-inquiry-answer-doc]'))).includes('线上订单销售额 118,000')
    && await entry(page, 40).locator('[data-inquiry-cite="Q1"]').count() === 2
    && await page.locator('[data-inquiry-pending]').count() === 0, await text(group(page, 'u1')))
  check('回答回来：批准、驳回恢复可用，原因说明拿掉', !(await approve(page).isDisabled()) && !(await reject(page).isDisabled())
    && await page.locator('[data-approval-inquiry-busy]').count() === 0)
  check('回答回来：次数加一', (await text(page.locator('[data-inquiry-count]'))) === '· 4 次')
  check('写进了运行记录：不说没有写入', await entry(page, 40).locator('[data-inquiry-unrecorded]').count() === 0)
  await page.waitForTimeout(100)
  check('回答回来时焦点已经在别处（刚才点过的那句「质询」）：不抢', (await active(page))?.ask === 'u2', JSON.stringify(await active(page)))
  check('除了提问没有别的写请求', hits.writes.length === 0, hits.writes.join('、'))
  check('页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('划选原文：浮出「质询」，带上原文', async () => {
  const { ctx, page, hits, errors } = await open('inquiry-approval')
  await expand(page)
  check('还没划选：没有浮出的「质询」', await page.locator('[data-inquiry-selection]').count() === 0)
  check('划选一段原文', await select(page, 'u4', '枫岭区销售额最高'))
  await page.locator('[data-inquiry-selection]').waitFor()
  const at = await page.evaluate(() => {
    const sel = getSelection().getRangeAt(0).getBoundingClientRect()
    const b = document.querySelector('[data-inquiry-selection]').getBoundingClientRect()
    return { gap: b.top - sel.bottom, near: Math.abs(b.left + b.width / 2 - sel.right) }
  })
  check('浮出的「质询」贴在选区末尾下方', at.gap >= 0 && at.gap < 20 && at.near < 60, JSON.stringify(at))
  await page.locator('[data-inquiry-selection]').click()
  await composer(page).waitFor()
  check('点它：提问框写明就选中的原文提问，显示原文', (await composer(page).getAttribute('data-inquiry-composer')) === 'quote'
    && (await text(composer(page).locator('[data-inquiry-target]'))) === '枫岭区销售额最高')
  check('浮出的按钮随之拿掉、选区清掉', await page.locator('[data-inquiry-selection]').count() === 0
    && await page.evaluate(() => getSelection().isCollapsed || !document.querySelector('[data-approval-evidence-doc]').contains(getSelection().anchorNode)))
  check('挂在原文起头那一句（枫岭区那句）的问答下面', await group(page, 'u4').locator('[data-inquiry-composer]').count() === 1)
  await page.locator('[data-inquiry-input]').fill('枫岭区的销售额是否包含门店自提？')
  await page.keyboard.press('Control+Enter')
  await entry(page, 40).waitFor()
  check('Ctrl + 回车提交；请求体带上原文和它起头的那一句', JSON.stringify(hits.asks.map((a) => a.body)) === JSON.stringify([{
    report: 'write', unit: 'u4', quote: '枫岭区销售额最高', question: '枫岭区的销售额是否包含门店自提？', approval_id: fx.approval_id }]),
  JSON.stringify(hits.asks.map((a) => a.body)))
  // 跨两句划选：原文里不能混进句末的「质询」、「无证据」小标签
  check('跨句划选', await page.evaluate(() => {
    const a = document.querySelector('[data-ev-unit="u4"]')
    const b = document.querySelector('[data-ev-unit="u5"]')
    const range = document.createRange()
    range.setStart(a, 0)
    range.setEnd(b, b.childNodes.length)
    getSelection().removeAllRanges()
    getSelection().addRange(range)
    return true
  }))
  await page.locator('[data-inquiry-selection]').waitFor()
  await page.locator('[data-inquiry-selection]').click()
  await composer(page).waitFor()
  const quote = await text(composer(page).locator('[data-inquiry-target]'))
  check('跨句的原文：只有报告的字，没有混进「质询」和「无证据」', quote.startsWith('分片区看，枫岭区销售额最高') && quote.endsWith('环比下降 3.2%。')
    && !quote.includes('质询') && !quote.includes('无证据'), quote)
  await page.keyboard.press('Escape')
  // 证据面板里的字不算报告原文
  await page.locator('[data-seg="s17"]').click()
  await page.locator('[data-approval] [data-evidence-panel]').waitFor()
  await page.evaluate(() => {
    const p = document.querySelector('[data-evidence-panel] [data-ev-body]')
    const range = document.createRange()
    range.selectNodeContents(p)
    getSelection().removeAllRanges()
    getSelection().addRange(range)
  })
  await page.waitForTimeout(200)
  check('划选证据面板里的字：不浮出「质询」', await page.locator('[data-inquiry-selection]').count() === 0)
  await shot(page, 'selection-dark')
  check('页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('划选原文：浮出的「质询」算在审批卡里，贴着报告框底边时也看得见', async () => {
  const { ctx, page, hits, errors } = await open('inquiry-approval')
  await expand(page)
  // 画布上的审批浮层（canvas/NodeCard）在 document 上按捕获阶段听 pointerdown，按在浮层外面就收起。浮出的「质询」
  // 要是挂在页面末尾，点它等于点外面：浮层连同报告一起收掉，问不成
  await page.evaluate(() => {
    window.__outside = []
    document.addEventListener('pointerdown', (e) => {
      const card = document.querySelector('[data-approval]')
      if (!card?.contains(e.target)) window.__outside.push(String(e.target?.outerHTML ?? e.target).slice(0, 80))
    }, true)
  })
  await select(page, 'u4', '枫岭区销售额最高')
  await page.locator('[data-inquiry-selection]').waitFor()
  await page.locator('[data-inquiry-selection]').click()
  await composer(page).waitFor()
  const outside = await page.evaluate(() => window.__outside)
  check('点浮出的「质询」：按下的位置在审批卡里，「点外面就收起」的浮层不会因此收起', outside.length === 0, outside.join(' | '))
  check('……照样打开提问框，带上原文', (await text(composer(page).locator('[data-inquiry-target]'))) === '枫岭区销售额最高')
  await page.keyboard.press('Escape')
  // Esc 之后焦点在下一帧才回到原文起头那一句的「质询」，它一出现那一段可能多折一行、下面的句子往下挪：等它落定再量，
  // 不然量好的「贴着底边」被挤到框外
  check('Esc 关掉提问框，焦点回到原文起头那一句的「质询」', await until(async () => (await active(page))?.ask === 'u4'),
    JSON.stringify(await active(page)))
  // 报告框压矮、滚到某一句的第一行贴着框的底边（取压矮之后落在框外、滚得过去的第一句），在那一行开头划选几个字：
  // 浮出的按钮放在选区下方会被框裁掉，得换到上方
  const edge = await page.evaluate(() => {
    const box = document.querySelector('[data-approval-evidence-doc]')
    if (!box) return null
    box.style.maxHeight = '150px'
    // 刚才提问框滚进过视野，页面滚到哪要看时机：先把报告框整个放进视口（选区滚出视口时按钮本来就不出）
    box.scrollIntoView({ block: 'center' })
    for (const unit of box.querySelectorAll('[data-ev-unit]')) {
      const b = box.getBoundingClientRect()
      const line = unit.getClientRects()[0]
      const need = line.bottom - (b.bottom - 6)
      if (need <= 0 || need > box.scrollHeight - box.clientHeight - box.scrollTop) continue
      box.scrollTop += need
      const walker = document.createTreeWalker(unit, NodeFilter.SHOW_TEXT)
      for (let n = walker.nextNode(); n; n = walker.nextNode()) {
        const t = n.data.trim()
        if (t.length < 4 || n.parentElement?.closest('[data-inquiry-ask], .sr-only')) continue
        const at = n.data.indexOf(t)
        const range = document.createRange()
        range.setStart(n, at)
        range.setEnd(n, at + 4)
        getSelection().removeAllRanges()
        getSelection().addRange(range)
        const r = range.getBoundingClientRect()
        const after = box.getBoundingClientRect()
        return { unit: unit.getAttribute('data-ev-unit'), words: t.slice(0, 4), gap: Math.round(after.bottom - r.bottom) }
      }
    }
    return null
  })
  check('报告框压矮后可以滚动：一句的第一行贴着框的底边，在那一行开头划选', !!edge && edge.gap >= 0 && edge.gap < 20, JSON.stringify(edge))
  await page.locator('[data-inquiry-selection]').waitFor()
  await page.waitForTimeout(150)
  const vis = await page.evaluate(() => {
    const btn = document.querySelector('[data-inquiry-selection]')
    const r = btn.getBoundingClientRect()
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
    const box = document.querySelector('[data-approval-evidence-doc]').getBoundingClientRect()
    return { shown: !!hit && btn.contains(hit), w: Math.round(r.width), inBox: r.top >= box.top - 1 && r.bottom <= box.bottom + 1 }
  })
  check('浮出的「质询」看得见：不被报告框裁掉', vis.shown && vis.w > 20, JSON.stringify(vis))
  await page.locator('[data-inquiry-selection]').click()
  await composer(page).waitFor()
  check('点它照样打开提问框，带上划选的字', (await text(composer(page).locator('[data-inquiry-target]'))) === edge?.words,
    `${await text(composer(page).locator('[data-inquiry-target]'))} / ${edge?.words}`)
  await shot(page, 'selection-edge-dark')
  check('没有发出任何写请求', hits.writes.length === 0 && hits.asks.length === 0, hits.writes.join('、'))
  check('页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('划选原文：选区上面的排版变了（不滚动、不改选区），浮出的「质询」跟着选区走', async () => {
  const { ctx, page, errors } = await open('inquiry-approval')
  await expand(page)
  check('划选一段原文', await select(page, 'u4', '枫岭区销售额最高'))
  await page.locator('[data-inquiry-selection]').waitFor()
  await page.waitForTimeout(100)
  const place = () => page.evaluate(() => {
    const sel = getSelection().getRangeAt(0).getBoundingClientRect()
    const b = document.querySelector('[data-inquiry-selection]')?.getBoundingClientRect()
    return { sel: Math.round(sel.bottom), gap: b ? Math.round(b.top - sel.bottom) : null }
  })
  const before = await place()
  // 同悬停一句时它的「质询」出现、那一段多折一行：上面的内容变高，选区往下挪，却没有滚动、没有选区变化的事件
  await page.evaluate(() => { document.querySelector('[data-approval-evidence-doc] [data-block]').style.paddingTop = '30px' })
  await page.waitForTimeout(200)
  const after = await place()
  check('选区往下挪了，浮出的「质询」跟着挪，仍贴在选区下方', after.sel - before.sel >= 25 && before.gap != null && after.gap != null
    && Math.abs(after.gap - before.gap) <= 2, `${JSON.stringify(before)} → ${JSON.stringify(after)}`)
  check('页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('就整份报告提问；recorded 为假时醒目说明', async () => {
  const { ctx, page, hits, state, errors } = await open('inquiry-approval')
  state.reply = (body) => ({ status: 200, json: answerFor(body, {
    inquiry: { ...answerFor(body).inquiry, seq: null }, recorded: false, note: '运行已不在等待审批，问答未写入运行记录' }) })
  await page.locator('[data-inquiry-ask-report]').click()
  await composer(page).waitFor()
  check('提问框写明就整份报告提问，不显示原文', (await composer(page).getAttribute('data-inquiry-composer')) === 'report'
    && await composer(page).locator('[data-inquiry-target]').count() === 0 && await group(page, 'report').locator('[data-inquiry-composer]').count() === 1)
  await page.locator('[data-inquiry-input]').fill('这份报告的数据截止到哪一天？')
  await page.locator('[data-inquiry-submit]').click()
  await group(page, 'report').locator('[data-inquiry-recorded="false"]').waitFor()
  check('请求体：unit 为 null，不带 quote', JSON.stringify(hits.asks[0]?.body) === JSON.stringify({
    report: 'write', unit: null, question: '这份报告的数据截止到哪一天？', approval_id: fx.approval_id }), JSON.stringify(hits.asks[0]?.body))
  await page.waitForTimeout(100)
  check('提交后焦点接到正在回答的那一条，回答回来再接到回答上，不掉到页面上', (await active(page))?.entry === '', JSON.stringify(await active(page)))
  const note = group(page, 'report').locator('[data-inquiry-unrecorded]')
  check('没写进运行记录：醒目说明，带上服务端的说明', (await text(note)).startsWith('这次问答没有写入运行记录：运行已不在等待审批，问答未写入运行记录')
    && Number(await note.evaluate((el) => getComputedStyle(el).fontWeight)) >= 600, await text(note))
  check('……并说清后果：只在当前页面、不随运行封存', (await text(note)).includes('不会随运行封存'))
  await shot(page, 'unrecorded-dark')
  check('页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('409 / 422：照服务端的中文说明显示，问题留着', async () => {
  const { ctx, page, hits, state, errors } = await open('inquiry-approval')
  for (const [code, reply] of Object.entries(ERRORS)) {
    state.reply = reply
    await page.locator('[data-inquiry-ask-report]').click()
    await composer(page).waitFor()
    await page.locator('[data-inquiry-input]').fill(`问题 ${code}`)
    await page.locator('[data-inquiry-submit]').click()
    const err = composer(page).locator('[data-inquiry-error]')
    await err.waitFor()
    const want = code === 'invalid' ? '422' : code
    check(`${reply.status} ${code === 'invalid' ? '输入不合法' : code}：提问框带着原来的问题重新打开，照服务端的说明显示`,
      (await err.getAttribute('data-inquiry-error')) === want && (await text(err)) === reply.json.detail
      && (await page.locator('[data-inquiry-input]').inputValue()) === `问题 ${code}` && (await err.getAttribute('role')) === 'alert',
    `${await err.getAttribute('data-inquiry-error')} · ${await text(err)}`)
    check(`${code}：批准、驳回照常可用，没有留下正在回答`, !(await approve(page).isDisabled()) && await page.locator('[data-inquiry-pending]').count() === 0)
    if (code === 'invalid') {
      check('422 指明是问题这一项：输入框标为无效', (await page.locator('[data-inquiry-input]').getAttribute('aria-invalid')) === 'true')
    }
    await page.locator('[data-inquiry-input]').fill('改一下')
    check(`${code}：改了问题，报错拿掉`, await composer(page).locator('[data-inquiry-error]').count() === 0)
    await page.keyboard.press('Escape')
  }
  check('四次都发出了请求', hits.asks.length === 4, `${hits.asks.length} 次`)
  check('页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('字数上限、空问题', async () => {
  const { ctx, page, hits, errors } = await open('inquiry-approval')
  await page.locator('[data-inquiry-ask-report]').click()
  await composer(page).waitFor()
  await page.locator('[data-inquiry-input]').fill('  ')
  await page.keyboard.press('Control+Enter')
  check('只有空白：提交不可用，Ctrl + 回车也不发', await page.locator('[data-inquiry-submit]').isDisabled() && hits.asks.length === 0)
  await page.locator('[data-inquiry-input]').fill('问'.repeat(1001))
  check('超过 1000 字：字数标红、写明上限、提交不可用', (await text(composer(page).locator('[data-inquiry-chars]'))) === '1,001/1,000'
    && (await text(composer(page))).includes('问题最多 1,000 字') && await page.locator('[data-inquiry-submit]').isDisabled()
    && (await page.locator('[data-inquiry-input]').getAttribute('aria-invalid')) === 'true')
  await page.locator('[data-inquiry-input]').fill('问'.repeat(1000))
  check('正好 1000 字：可以提交', !(await page.locator('[data-inquiry-submit]').isDisabled()))
  check('页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('正式运行的审批：审批阶段照样能问；已处理的审批只读', async () => {
  {
    const { ctx, page, hits, errors } = await open('inquiry-approval-formal')
    await page.locator('[data-inquiry="write"] [data-inquiry-entry="31"]').waitFor()
    await page.locator('[data-inquiry-ask-report]').click()
    await page.locator('[data-inquiry-input]').fill('正式运行里问一句')
    await page.locator('[data-inquiry-submit]').click()
    await entry(page, 40).waitFor()
    check('正式运行停在审批上：可以问，带的是这张审批', hits.asks[0]?.run === RUNS.formal && hits.asks[0]?.body.approval_id === fx.formal_approval_id,
      JSON.stringify(hits.asks[0]))
    check('正式运行：页面没有报错', errors.length === 0, errors.join('；'))
    await ctx.close()
  }
  {
    const { ctx, page, hits, errors } = await open('inquiry-approval-done')
    await page.locator('[data-inquiry="write"] [data-inquiry-entry="31"]').waitFor()
    await expand(page)
    check('已处理的审批：问答串照常列出', await page.locator('[data-inquiry-entry]').count() === 3)
    check('已处理的审批：不给「就整份报告提问」、报告上没有「质询」', await page.locator('[data-inquiry-ask-report], [data-inquiry-ask]').count() === 0)
    await select(page, 'u4', '枫岭区销售额最高')
    await page.waitForTimeout(200)
    check('已处理的审批：划选也不浮出「质询」', await page.locator('[data-inquiry-selection]').count() === 0)
    check('已处理的审批：没有发出提问', hits.asks.length === 0)
    check('已处理的审批：页面没有报错', errors.length === 0, errors.join('；'))
    await ctx.close()
  }
})

await section('运行详情「证据」页签：封存了的探索运行有入口，正式运行没有', async () => {
  {
    const { ctx, page, hits, errors } = await open('inquiry-run-explore')
    await page.locator('[data-evidence-report="write"] [data-ev-unit="u1"]').waitFor()
    await page.locator('[data-inquiry="write"] [data-inquiry-entry="58"]').waitFor()
    check('报告上有「质询」，每句结论一个', await page.locator('[data-evidence-report="write"] [data-inquiry-ask]').count() === 7)
    check('问答串挂在报告下面：封存之后问的写明封存后追加', (await text(entry(page, 58).locator('[data-inquiry-meta]'))).includes('封存后追加'))
    check('问答串按运行和报告取', hits.lists.length === 1 && hits.lists[0].run === RUNS.explore && hits.lists[0].report === 'write')
    await page.locator('[data-evidence-report="write"] [data-seg="s4"]').focus()
    await page.keyboard.press('Tab')
    await page.keyboard.press('Enter')
    await composer(page).waitFor()
    check('提问框写明运行已封存、问答追加在封存之后', (await text(composer(page))).includes('运行已封存：问答追加在封存之后，不改动报告'))
    await page.locator('[data-inquiry-input]').fill('封存之后再问一句')
    await page.locator('[data-inquiry-submit]').click()
    await entry(page, 40).waitFor()
    check('请求体不带 approval_id', JSON.stringify(hits.asks[0]?.body) === JSON.stringify({ report: 'write', unit: 'u1', question: '封存之后再问一句' }),
      JSON.stringify(hits.asks[0]?.body))
    await shot(page, 'run-explore-dark')
    check('探索运行：页面没有报错', errors.length === 0, errors.join('；'))
    await ctx.close()
  }
  {
    // 审批时问过的随运行封存：审批处理掉以后，在运行详情的报告下面只读列出（查过的 SQL、行数、快照都在）
    const { ctx, page, hits, errors } = await open('inquiry-run-formal', { setup: (s) => {
      s.lists[RUNS.sealed] = fx.items.map((x) => ({ ...x, approval_id: fx.formal_approval_id }))
      s.approvals[RUNS.sealed] = [approvalRow(fx.formal_approval_id, RUNS.sealed, fx.reports.sealed.doc_artifact)]
    } })
    await page.locator('[data-evidence-report="write"] [data-ev-unit], [data-evidence-report="write"] [data-seg="s4"]').first().waitFor()
    await page.locator('[data-inquiry="write"] [data-inquiry-entry="31"]').waitFor()
    await page.waitForTimeout(200)
    check('正式运行封存后：报告上没有「质询」，也没有提问入口、提问框',
      await page.locator('[data-inquiry-ask], [data-inquiry-ask-report], [data-inquiry-composer], [data-inquiry-intro]').count() === 0)
    const groups = await groupsOf(page)
    check('正式运行封存后：审批时问过的照列，按句子挂在报告下面（审的就是封存的这一稿）',
      JSON.stringify(groups.map((g) => [g.unit, g.entries])) === JSON.stringify([['u2', ['31']], ['u3', ['34']], ['u4', ['36']]]),
      JSON.stringify(groups.map((g) => [g.unit, g.entries])))
    const e2 = entry(page, 34)
    await e2.locator('[data-inquiry-queries-toggle]').click()
    const q2 = e2.locator('[data-inquiry-query="Q2"]')
    check('正式运行封存后：查过的 SQL、行数照样看得到', (await text(q2.locator('[data-inquiry-sql]'))).startsWith('SELECT member_id')
      && (await text(q2.locator('[data-inquiry-rows]'))) === '50 行')
    await page.evaluate(() => {
      const el = [...document.querySelectorAll('[data-evidence-report="write"] [data-block]')].find((b) => b.textContent.includes('枫岭区'))
      const range = document.createRange()
      range.selectNodeContents(el)
      getSelection().removeAllRanges()
      getSelection().addRange(range)
    })
    await page.waitForTimeout(200)
    check('正式运行封存后：划选也不浮出「质询」', await page.locator('[data-inquiry-selection]').count() === 0)
    check('正式运行封存后：按运行和报告取问答列表，没有发出提问', hits.lists.length === 1 && hits.lists[0].run === RUNS.sealed && hits.asks.length === 0,
      JSON.stringify(hits.lists))
    check('正式运行：页面没有报错', errors.length === 0, errors.join('；'))
    await ctx.close()
  }
  {
    // 没有任何问答的正式运行：什么都不画
    const { ctx, page, errors } = await open('inquiry-run-formal')
    await page.locator('[data-evidence-report="write"] [data-seg="s4"]').first().waitFor()
    await page.waitForTimeout(300)
    check('正式运行封存后、没有问答：不画问答串', await page.locator('[data-inquiry], [data-inquiry-ask]').count() === 0)
    check('没有问答的正式运行：页面没有报错', errors.length === 0, errors.join('；'))
    await ctx.close()
  }
})

await section('运行详情「工件」页签：质询的回答有名字和说明，不出现英文的工件种类名；封存后追加的不给航迹跳转', async () => {
  const { ctx, page, errors } = await open('inquiry-run-artifacts')
  await page.locator('[data-run-artifacts] [data-artifact-id]').first().waitFor()
  // 每一行：种类（行首的标签）、说明（同一种类有几件时的「#n」另在截断范围外，不在说明里）、悬停、分组
  const rows = await page.locator('[data-artifact-id]').evaluateAll((els) => els.map((el) => {
    const open = el.querySelector('[data-action="artifact-open"]')
    return { id: el.getAttribute('data-artifact-id'), kind: el.querySelector('[data-artifact-kind-label]')?.textContent?.trim() ?? '',
      about: el.querySelector('[data-artifact-about]')?.textContent?.trim() ?? '', title: open?.getAttribute('title') ?? '',
      group: el.getAttribute('data-artifact-class') }
  }))
  const answers = fx.run_artifacts.filter((a) => a.kind === 'inquiry_answer').map((a) => rows.find((r) => r.id === a.id))
  check('质询的回答：种类写「质询回答」', answers.length === 4 && answers.every((r) => r?.kind === '质询回答'), JSON.stringify(answers))
  check('有问题摘要的：说明写「质询「问题」的回答」', answers[0]?.about === '质询「退款率怎么算的？」的回答', answers[0]?.about)
  check('问题摘要满 40 字（服务端只记前 40 字）：末尾补省略号',
    answers[1]?.about === '质询「枫岭区的销售额是否包含线上订单？如果包含，线上订单按下单门店还是按收货地址归到片…」的回答', answers[1]?.about)
  check('没有问题摘要的：也有一句说明', answers[2]?.about === '审批质询的回答：逐段的片段和每个数字的出处', answers[2]?.about)
  // 第 40 个字是空格：服务端截下来的摘要以空格结尾（截之前整个问题已经去过首尾空白），去掉空白再数只有 39 字
  check('截断截在空格上的摘要：照样补省略号，不当成完整的问题',
    answers[3]?.about === '质询「本周枫岭片区 GMV 环比下降了多少？下降的主要原因是不是 SKU 结构的变化…」的回答', answers[3]?.about)
  // 航迹跳转：运行期间产出的报告文档能跳到航迹里那一刻；封存后追加的质询回答和它查询的快照晚于运行结束，航迹里没有
  // 那一刻（以前给「T+」几十分钟、几天的跳转，点了把航迹拖到运行早就结束之后），只写时间
  const moments = Object.fromEntries(await page.locator('[data-artifact-id]').evaluateAll((els) => els.map((el) =>
    [el.getAttribute('data-artifact-id'), el.querySelector('[data-action="artifact-moment"]')?.textContent?.trim() ?? null])))
  const doc = fx.run_artifacts.find((a) => a.kind === 'report_doc')
  const late = fx.run_artifacts.filter((a) => a.kind !== 'report_doc')
  check('运行期间产出的报告文档：给航迹跳转', /^T\+/.test(moments[doc.id] ?? ''), moments[doc.id])
  check('封存后追加的质询回答、它查询的快照：不给航迹跳转，只写时间', late.every((a) => moments[a.id] === null)
    && (await page.locator(`[data-artifact-id="${late[0].id}"] > span`).last().innerText()).trim() !== '',
    JSON.stringify(late.map((a) => moments[a.id])))
  await page.locator(`[data-artifact-id="${doc.id}"] [data-action="artifact-moment"]`).click()
  const jumped = await page.locator('[data-fixture="inquiry-run-artifacts"]').getAttribute('data-fixture-moment')
  check('……点报告文档的跳转：到它产出的那一刻', Number(jumped) === Date.parse(`${doc.created_at}Z`), jumped)
  check('悬停写「打开质询回答」，接着是整句说明（行上截断了也看得全）', answers.every((r) => r?.title.startsWith(`打开质询回答：${r.about}`)),
    answers.map((r) => r?.title).join(' / '))
  check('质询回答归在「核查记录」，不算证据快照', answers.every((r) => r?.group === 'verify'), answers.map((r) => r?.group).join('、'))
  const pane = await text(page.locator('[data-run-artifacts]'))
  check('工件页签里不出现英文的工件种类名', !/inquiry_answer|report_doc|query_snapshot/.test(pane) && rows.every((r) => !/[a-z_]{4,}/.test(r.kind)),
    rows.map((r) => r.kind).join('、'))
  check('不挂在节点上的写「不属于某个节点」', (await text(page.locator('[data-artifact-group=""] h3'))).startsWith('不属于某个节点'))
  // 打开一件：弹窗标题也是中文的种类名
  await page.locator(`[data-artifact-id="${answers[0]?.id}"] [data-action="artifact-open"]`).click()
  const dialog = page.getByRole('dialog')
  await dialog.waitFor()
  const head = await text(dialog.locator('h2').first())
  check('打开的弹窗标题写「质询回答」，不写英文', head.includes('质询回答') && !head.includes('inquiry_answer'), head)
  check('页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('有表格的报告：悬停句子、表格的格子都不挪动排版；表格的格子不挂「质询」', async () => {
  // 报告文档换成带表格的那份（run/__tests__/evidence-doc.json）：表里带出处的格子也是结论句
  const ev = JSON.parse(readFileSync(`${root}frontend/src/run/__tests__/evidence-doc.json`, 'utf8'))
  const { ctx, page, errors } = await open('inquiry-approval', { setup: (s) => {
    s.docs[fx.reports.approve.doc_artifact] = { ...ev.doc, node_id: 'write' }
  } })
  await expand(page)
  const table = page.locator('[data-approval-evidence-doc] table').first()
  await table.waitFor()
  check('表格的格子不挂「质询」', await table.locator('[data-inquiry-ask]').count() === 0
    && await table.locator('[data-ev-unit]').count() === 0)
  check('表格外的结论句照常挂', await page.locator('[data-approval-evidence-doc] [data-inquiry-ask]').count() >= 5)
  const before = await layout(page)
  const cell = table.locator('td [data-seg]').first()
  await cell.hover()
  await page.waitForTimeout(100)
  const mid = await layout(page)
  check('悬停表格里有出处的格子：列宽、正文都不变', moved(before, mid).length === 0, moved(before, mid).slice(0, 3).join('；'))
  const units = await page.locator('[data-approval-evidence-doc] [data-ev-unit]').evaluateAll((els) => els.map((el) => el.getAttribute('data-ev-unit')))
  const shifted = []
  for (const u of units) {
    await hoverUnit(page, u)
    await page.waitForTimeout(60)
    const now = await layout(page)
    // 悬停时页面为了把那一句放到中间滚动过：比相对位置，不比绝对位置
    const base = before.get(u)
    const dy = now.get(u).y - base.y
    const shift = [...before].filter(([k, v]) => { const w = now.get(k); return !w || Math.abs(w.x - v.x) > 0.5 || Math.abs(w.y - dy - v.y) > 0.5 || Math.abs(w.w - v.w) > 0.5 })
    if (shift.length) shifted.push(`${u}：${shift.slice(0, 2).map(([k]) => k).join('、')}`)
  }
  check('逐句悬停：正文、表格一处都不挪', shifted.length === 0, shifted.slice(0, 3).join('；'))
  check('页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('Esc、「取消」关掉提问框：焦点回到那一句的「质询」，并滚回视野', async () => {
  for (const [width, height] of [[390, 844], [1100, 700]]) {
    const { ctx, page, errors } = await open('inquiry-approval', { width, height })
    await expand(page)
    await page.locator('[data-seg="s4"]').focus()
    await page.keyboard.press('Tab')
    await page.keyboard.press('Enter')
    await composer(page).waitFor()
    await page.waitForTimeout(200)
    await page.keyboard.press('Escape')
    check(`${width}px：Esc 后焦点回到 u1 的「质询」`, await until(async () => (await active(page))?.ask === 'u1'), JSON.stringify(await active(page)))
    await page.waitForTimeout(150)
    const shown = await focusShown(page)
    check(`${width}px：……它在视野里（页面、报告框都滚到它），键盘用户看得见焦点`, shown.ok, JSON.stringify(shown))
    await clickAsk(page, 'u4')
    await composer(page).waitFor()
    await page.waitForTimeout(200)
    await composer(page).locator('button', { hasText: '取消' }).click()
    check(`${width}px：点「取消」后焦点回到 u4 的「质询」`, await until(async () => (await active(page))?.ask === 'u4'), JSON.stringify(await active(page)))
    await page.waitForTimeout(150)
    check(`${width}px：……同样在视野里`, (await focusShown(page)).ok, JSON.stringify(await focusShown(page)))
    check(`${width}px：页面没有报错`, errors.length === 0, errors.join('；'))
    await ctx.close()
  }
})

await section('同一张审批卡同时挂在两处（画布的审批浮层 + 右栏）：提问框只在点的那一处，焦点不跳走', async () => {
  const { ctx, page, hits, state, errors } = await open('inquiry-approval-twice', { width: 1400, height: 900 })
  const pop = page.locator('[data-fixture-pop]')
  const panel = page.locator('[data-fixture-panel]')
  await pop.locator('[data-inquiry-entry="31"]').waitFor()
  await panel.locator('[data-inquiry-entry="31"]').waitFor()
  const where = () => page.evaluate(() => {
    const a = document.activeElement
    return { pop: !!a?.closest('[data-fixture-pop]'), panel: !!a?.closest('[data-fixture-panel]'), input: !!a?.hasAttribute('data-inquiry-input') }
  })
  await pop.locator('[data-inquiry-ask-report]').click()
  await pop.locator('[data-inquiry-composer]').waitFor()
  await page.waitForTimeout(200)
  check('在浮层里点「就整份报告提问」：只有浮层里出提问框', await page.locator('[data-inquiry-composer]').count() === 1
    && await pop.locator('[data-inquiry-composer]').count() === 1)
  check('焦点在浮层的输入框里，没有跳到右栏', JSON.stringify(await where()) === JSON.stringify({ pop: true, panel: false, input: true }),
    JSON.stringify(await where()))
  check('右栏同一组写明提问框在另一处打开', (await text(panel.locator('[data-inquiry-group="report"] [data-inquiry-draft-elsewhere]'))).startsWith('提问框已在另一处打开'))
  await page.keyboard.type('浮层里写的问题')
  check('敲的字进浮层的输入框，右栏没有第二个输入框', (await pop.locator('[data-inquiry-input]').inputValue()) === '浮层里写的问题'
    && await panel.locator('[data-inquiry-input]').count() === 0)
  // 句子上的「质询」：两处都展开逐段证据，在浮层里点
  await pop.locator('[data-approval-evidence]').click()
  await pop.locator('[data-approval-evidence-doc] [data-block]').first().waitFor()
  await panel.locator('[data-approval-evidence]').click()
  await panel.locator('[data-approval-evidence-doc] [data-block]').first().waitFor()
  await clickAsk(page, 'u3', pop)
  await page.waitForTimeout(200)
  check('在浮层里点一句的「质询」：提问框换到那一句，仍只在浮层里，问题留着', await page.locator('[data-inquiry-composer]').count() === 1
    && (await pop.locator('[data-inquiry-composer]').getAttribute('data-inquiry-composer')) === 'unit'
    && (await pop.locator('[data-inquiry-input]').inputValue()) === '浮层里写的问题')
  check('……焦点仍在浮层里', JSON.stringify(await where()) === JSON.stringify({ pop: true, panel: false, input: true }), JSON.stringify(await where()))
  await page.keyboard.press('Escape')
  check('Esc：焦点回到浮层里那一句的「质询」', await until(async () => (await where()).pop && (await active(page))?.ask === 'u3'),
    JSON.stringify(await where()))
  // 浮层关掉：右栏接过提问框，但不抢焦点
  await clickAsk(page, 'u3', pop)
  await pop.locator('[data-inquiry-input]').fill('关浮层之前写的问题')
  await page.locator('[data-fixture-toggle]').click()
  await panel.locator('[data-inquiry-composer]').waitFor()
  await page.waitForTimeout(200)
  check('浮层关了：右栏接过提问框，问题还在', await page.locator('[data-inquiry-composer]').count() === 1
    && (await panel.locator('[data-inquiry-input]').inputValue()) === '关浮层之前写的问题')
  check('……接过来时不抢焦点（焦点还在刚点的按钮上）', await page.evaluate(() => document.activeElement?.hasAttribute('data-fixture-toggle')))
  // 再打开浮层：提问框留在右栏，浮层里可以「在这里继续」
  await page.locator('[data-fixture-toggle]').click()
  await pop.locator('[data-inquiry-draft-elsewhere]').waitFor()
  check('再打开浮层：提问框仍在右栏，浮层写明在另一处打开', await panel.locator('[data-inquiry-composer]').count() === 1
    && await pop.locator('[data-inquiry-composer]').count() === 0)
  await pop.locator('[data-inquiry-draft-elsewhere] button').click()
  await pop.locator('[data-inquiry-composer]').waitFor()
  await page.waitForTimeout(150)
  check('点「在这里继续」：提问框挪到浮层，焦点跟过去，问题还在', await panel.locator('[data-inquiry-composer]').count() === 0
    && (await pop.locator('[data-inquiry-input]').inputValue()) === '关浮层之前写的问题'
    && JSON.stringify(await where()) === JSON.stringify({ pop: true, panel: false, input: true }))
  // 在浮层里提交：回答期间、回答回来时焦点都只在浮层里交接
  let release
  state.gate = new Promise((r) => { release = r })
  await pop.locator('[data-inquiry-submit]').click()
  await pop.locator('[data-inquiry-pending]').waitFor()
  await page.waitForTimeout(150)
  check('回答期间：两处都显示正在回答，焦点接到浮层里那一条', await page.locator('[data-inquiry-pending]').count() === 2
    && await page.evaluate(() => !!document.activeElement?.closest('[data-fixture-pop] [data-inquiry-pending]')))
  check('回答期间：两处的批准都不可用', await page.locator('[data-approval]').getByRole('button', { name: '批准', exact: true }).evaluateAll((els) => els.length === 2 && els.every((el) => el.disabled)))
  release()
  await pop.locator('[data-inquiry-entry="40"]').waitFor()
  await panel.locator('[data-inquiry-entry="40"]').waitFor()
  await page.waitForTimeout(200)
  check('回答回来：两处都挂上，焦点接到浮层里的回答', await page.evaluate(() => !!document.activeElement?.closest('[data-fixture-pop] [data-inquiry-entry="40"]')))
  const heard = await page.evaluate(() => [...document.querySelectorAll('[data-inquiry-live]')].map((el) => [!!el.closest('[data-fixture-pop]'), el.textContent]))
  check('「已收到回答」只在问的那一处播报', heard.filter(([, t]) => t).length === 1 && heard.find(([, t]) => t)?.[0] === true, JSON.stringify(heard))
  check('只发出一次提问', hits.asks.length === 1 && hits.asks[0].body.approval_id === fx.approval_id)
  check('页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('同一份报告先后两张审批：换了审批，上一张的提问框不跟过来，提问带新的那张', async () => {
  for (const syn of ['inquiry-approval-steps', 'inquiry-approval-steps-same']) {
    const { ctx, page, hits, errors } = await open(syn, { setup: (s) => {
      s.approvals[RUNS.approve] = [approvalRow(fx.approval_id, RUNS.approve, fx.reports.approve.doc_artifact),
        approvalRow('ap-inq-3', RUNS.approve, fx.reports.approve.doc_artifact)]
    } })
    await entry(page, 31).waitFor()
    await page.locator('[data-inquiry-ask-report]').click()
    await page.locator('[data-inquiry-input]').fill('上一张审批上没发出去的问题')
    await page.locator('[data-fixture-next]').click()
    await page.locator('[data-approval="ap-inq-3"]').waitFor()
    await entry(page, 31).waitFor()
    await page.waitForTimeout(200)
    check(`${syn}：换到下一张审批，上一张的提问框不跟过来`, await composer(page).count() === 0)
    check(`${syn}：也没抢焦点`, !(await active(page))?.input)
    check(`${syn}：上一级审批问过的（同一份报告）照样挂在句子下面`, JSON.stringify((await groupsOf(page)).map((g) => [g.unit, g.entries]))
      === JSON.stringify([['u2', ['31']], ['u3', ['34']], ['u4', ['36']]]), JSON.stringify((await groupsOf(page)).map((g) => [g.unit, g.entries])))
    await page.locator('[data-inquiry-ask-report]').click()
    await composer(page).waitFor()
    check(`${syn}：在新的审批上点提问：从头写`, (await page.locator('[data-inquiry-input]').inputValue()) === '')
    await page.locator('[data-inquiry-input]').fill('总监的问题')
    await page.locator('[data-inquiry-submit]').click()
    await entry(page, 40).waitFor()
    check(`${syn}：请求体带的是新的那张审批`, hits.asks.length === 1 && hits.asks[0].body.approval_id === 'ap-inq-3',
      JSON.stringify(hits.asks.map((a) => a.body)))
    check(`${syn}：回答挂在句子下面（问的就是这一稿）`, await group(page, 'report').locator('[data-inquiry-entry="40"]').count() === 1)
    check(`${syn}：页面没有报错`, errors.length === 0, errors.join('；'))
    await ctx.close()
  }
})

await section('之前几轮审批里问的：报告改写过的不按句子编号挂，单独一组', async () => {
  const prev = { ...clone(fx.items[0]), seq: 12, approval_id: 'ap-inq-0', question: '上一轮：退款率 3.5% 是怎么算的？', at: '2026-10-05T05:40:00Z' }
  const level = { ...clone(fx.items[1]), seq: 20, approval_id: 'ap-inq-9', question: '主管那一级问的：新客怎么界定？', at: '2026-10-05T05:50:00Z' }
  const lost = { ...clone(fx.items[1]), seq: 22, approval_id: 'ap-gone', question: '审批列表里找不到这张审批', at: '2026-10-05T05:55:00Z' }
  const { ctx, page, hits, errors } = await open('inquiry-approval', { setup: (s) => {
    s.lists[RUNS.approve] = [prev, level, lost, ...fx.items]
    s.approvals[RUNS.approve] = [approvalRow(fx.approval_id, RUNS.approve, fx.reports.approve.doc_artifact),
      approvalRow('ap-inq-9', RUNS.approve, fx.reports.approve.doc_artifact), approvalRow('ap-inq-0', RUNS.approve, '0'.repeat(64))]
  } })
  await entry(page, 12).waitFor()
  await entry(page, 36).waitFor()
  const groups = await groupsOf(page)
  // 组按各自最早的那一条排：主管那一级问新客的那条最早
  check('同一稿报告上问的照常按句子挂（另一级审批审的是同一份文档的也挂上）',
    JSON.stringify(groups.slice(0, 3).map((g) => [g.unit, g.entries])) === JSON.stringify([['u3', ['20', '34']], ['u2', ['31']], ['u4', ['36']]]),
    JSON.stringify(groups.map((g) => [g.unit, g.entries])))
  const other = groups.at(-1)
  check('报告改写前问的、对不上是哪一稿的：单独一组，排在最后', other?.unit === 'other' && JSON.stringify(other.entries) === JSON.stringify(['12', '22']),
    JSON.stringify(other))
  check('那一组写明是报告其他版本上的质询，不拿当前报告同编号的句子当抬头', other?.about.startsWith('报告其他版本上的质询')
    && !other.about.includes('退款金额'), other?.about)
  check('次数照算全部', (await text(page.locator('[data-inquiry-count]'))) === '· 6 次')
  check('按运行取一次审批列表（全部状态）', hits.approvals.length === 1 && hits.approvals[0].run === RUNS.approve && hits.approvals[0].status === 'all',
    JSON.stringify(hits.approvals))
  // 那一组里的回答引用了报告已有的出处：不在当前这一稿里找
  const e = entry(page, 12)
  await e.locator('[data-inquiry-answer-doc]').waitFor()
  check('报告其他版本上的回答：引用报告出处的数，读屏说是提问时那一版里的', ((await e.locator('[data-inquiry-cite="report"]').getAttribute('aria-label')) ?? '')
    .includes('提问时那一版报告中的出处'))
  await e.locator('[data-inquiry-cite="report"]').click()
  await page.waitForTimeout(200)
  check('……点了不去当前报告里打开证据，照文档记下的出处说一句', await page.locator('[data-approval-evidence-doc]').count() === 0
    && (await text(e.locator('[data-inquiry-elsewhere]'))).includes('提问时那一版报告中的出处'))
  check('页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
  {
    // 审批列表取不到：别的审批里问的一律单独一组（宁可不挂，不挂错）。不知道是哪一稿，不能说成「其他版本」（审批人会以为
    // 报告换过稿）：写明没能确认、给重试。这里用浅色主题量那句的颜色（深色在下一段量）
    const { ctx, page, errors } = await open('inquiry-approval', { theme: 'light', setup: (s) => { s.lists[RUNS.approve] = [prev, level, ...fx.items] } })
    await entry(page, 12).waitFor()
    const g = await groupsOf(page)
    check('审批列表取不到：别的审批里问的都单独一组', JSON.stringify(g.at(-1)) !== undefined && g.at(-1).unit === 'unsure'
      && JSON.stringify(g.at(-1).entries) === JSON.stringify(['12', '20']), JSON.stringify(g.map((x) => [x.unit, x.entries])))
    check('审批列表取不到：那一组写「无法确认提问时的报告版本」，不说「其他版本」，给重试',
      g.at(-1)?.about.startsWith('无法确认提问时的报告版本') && !g.at(-1)?.about.includes('其他版本')
      && await group(page, 'unsure').locator('[data-inquiry-docs-retry]').count() === 1, g.at(-1)?.about)
    const unsureRatio = await ratioOf(page, '[data-inquiry-group="unsure"] [data-inquiry-about-text]')
    check('审批列表取不到（浅色）：那一句对比度 ≥ 4.5', unsureRatio >= 4.5, unsureRatio.toFixed(2))
    check('审批列表取不到：页面没有报错', errors.length === 0, errors.join('；'))
    await ctx.close()
  }
  {
    // 运行详情：显示的是封存的那一稿。封存之后问的、审批时审的就是这一稿的挂句子下面，更早那一稿上问的单独一组
    const early = { ...clone(fx.items[0]), seq: 14, approval_id: 'ap-early', question: '改写前：退款率是不是算错了？', at: '2026-10-05T05:10:00Z' }
    const last = { ...clone(fx.items[1]), seq: 30, approval_id: 'ap-last', question: '最后一轮审批：新客口径？', at: '2026-10-05T06:10:00Z' }
    const { ctx, page, errors } = await open('inquiry-run-explore', { setup: (s) => {
      s.lists[RUNS.explore] = [early, last, ...fx.explore_items]
      s.approvals[RUNS.explore] = [approvalRow('ap-early', RUNS.explore, '1'.repeat(64)),
        approvalRow('ap-last', RUNS.explore, fx.reports.explore.doc_artifact)]
    } })
    await page.locator('[data-inquiry="write"] [data-inquiry-entry="14"]').waitFor()
    const g = await groupsOf(page)
    check('运行详情：封存之后问的、最后一轮审批时问的挂句子下面，改写前那一稿上问的单独一组',
      JSON.stringify(g.map((x) => [x.unit, x.entries])) === JSON.stringify([['u3', ['30']], ['u2', ['58']], ['other', ['14']]]),
      JSON.stringify(g.map((x) => [x.unit, x.entries])))
    check('运行详情：页面没有报错', errors.length === 0, errors.join('；'))
    await ctx.close()
  }
})

/** 一处问答串：抬头写的次数、实际列出的条数 */
const tally = async (page, scope) => ({
  head: (await page.locator(`${scope} [data-inquiry-count]`).getAttribute('data-inquiry-count').catch(() => null)) ?? '',
  shown: await page.locator(`${scope} [data-inquiry-entry]`).count(),
})

await section('两处要对的审批不同、审批列表取不到：两处的问答都列出来，次数和条数一致，写明没能确认、重试取到后各归其位', async () => {
  // 一张卡审两份报告：写月报上有 ap-x 里问的，写周报上有 ap-y 里问的，两份报告的问答串共用这次运行的审批列表。先发的那一处
  // 请求还在路上时，后一处要对的审批也得记上：不然请求回来（这里是 404）之后后一处一直等着，头上写「· 1 次」，下面一条没有
  const x = { ...clone(fx.items[0]), seq: 41, approval_id: 'ap-x', question: '写月报上一轮问的：退款率怎么算？', at: '2026-10-05T05:30:00Z' }
  const y = { ...clone(fx.items[1]), seq: 42, report: 'write2', approval_id: 'ap-y', question: '写周报上一轮问的：新客怎么界定？',
    at: '2026-10-05T05:35:00Z' }
  let release
  // 审批列表压着，页面等不到网络空闲：打开到 load 就往下走
  const { ctx, page, hits, errors } = await open('inquiry-approval-two', { waitUntil: 'load', setup: (s) => {
    s.lists[RUNS.approve] = [x, y]
    s.approvalsGate = new Promise((r) => { release = r })
  } })
  // 两份报告的问答串都取回来了（头上写着次数），审批列表的请求还压着
  await page.locator('[data-inquiry="write"] [data-inquiry-count]').waitFor()
  await page.locator('[data-inquiry="write2"] [data-inquiry-count]').waitFor()
  await until(() => hits.approvals.length >= 1)
  await page.waitForTimeout(200)
  release()
  const both = await until(async () => (await tally(page, '[data-inquiry="write"]')).shown === 1
    && (await tally(page, '[data-inquiry="write2"]')).shown === 1)
  const [gw, gw2] = [await groupsOf(page, '[data-inquiry="write"]'), await groupsOf(page, '[data-inquiry="write2"]')]
  check('写月报：审批列表没取到，那张审批里问的单独一组（没能确认）', JSON.stringify(gw.map((g) => [g.unit, g.entries])) === JSON.stringify([['unsure', ['41']]]),
    JSON.stringify(gw.map((g) => [g.unit, g.entries])))
  check('写周报：请求是另一处先发的，回来之后照样列出、单独一组', both
    && JSON.stringify(gw2.map((g) => [g.unit, g.entries])) === JSON.stringify([['unsure', ['42']]]), JSON.stringify(gw2.map((g) => [g.unit, g.entries])))
  const [tw, tw2] = [await tally(page, '[data-inquiry="write"]'), await tally(page, '[data-inquiry="write2"]')]
  check('两份报告头上的次数都和列出的条数一致', tw.head === '1' && tw.shown === 1 && tw2.head === '1' && tw2.shown === 1,
    JSON.stringify({ tw, tw2 }))
  check('按运行只取一次审批列表（一份列表里各张审批都有）', hits.approvals.length === 1, JSON.stringify(hits.approvals))
  check('页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
  {
    // 运行详情直接打开「证据」页签、运行停在审批上：时间线里的待审批卡只为更早那一轮（ap-inq-0）取审批列表，证据页签不认
    // 任何审批，ap-inq-0 和这张卡的 ap-inq-1 都要对。卡那一处先发了请求，证据页签那一处的条目在请求回来之后也得列出来
    const early = { ...clone(fx.items[0]), seq: 12, approval_id: 'ap-inq-0', question: '上一轮：退款率 3.5% 是怎么算的？', at: '2026-10-05T05:40:00Z' }
    const { ctx, page, hits, state, errors } = await open('inquiry-run-waiting', { waitUntil: 'load', setup: (s) => {
      s.lists[RUNS.approve] = [early, clone(fx.items[1])]
      s.approvalsGate = new Promise((r) => { release = r })
    } })
    const card = '[data-fixture-card] [data-inquiry="write"]'
    const pane = '[data-fixture-evidence] [data-inquiry="write"]'
    await page.locator(`${card} [data-inquiry-count]`).waitFor()
    await page.locator(`${pane} [data-inquiry-count]`).waitFor()
    await until(() => hits.approvals.length >= 1)
    await page.waitForTimeout(200)
    release()
    const listed = await until(async () => (await tally(page, card)).shown === 2 && (await tally(page, pane)).shown === 2)
    const [gc, gp] = [await groupsOf(page, card), await groupsOf(page, pane)]
    check('待审批卡：这张审批里问的挂句子下面，没能确认的那一轮单独一组',
      JSON.stringify(gc.map((g) => [g.unit, g.entries])) === JSON.stringify([['u3', ['34']], ['unsure', ['12']]]),
      JSON.stringify(gc.map((g) => [g.unit, g.entries])))
    check('证据页签：两条都列出来，没能确认的单独一组（不再一直等着）', listed
      && JSON.stringify(gp.map((g) => [g.unit, g.entries])) === JSON.stringify([['unsure', ['12', '34']]]), JSON.stringify(gp.map((g) => [g.unit, g.entries])))
    const [tc, tp] = [await tally(page, card), await tally(page, pane)]
    check('两处头上的次数都和列出的条数一致', tc.head === '2' && tc.shown === 2 && tp.head === '2' && tp.shown === 2, JSON.stringify({ tc, tp }))
    check('运行详情：按运行只取一次审批列表', hits.approvals.length === 1, JSON.stringify(hits.approvals))
    // 没读到审批记录不能说成「其他版本」：卡上同一条挂在句子下面，下面证据页签却说是别的版本，审批人会以为报告换过稿
    const heads = (await page.locator('[data-inquiry-group="unsure"] [data-inquiry-about-text]').allInnerTexts()).map((t) => t.trim())
    check('没能确认的那一组（两处）：写「无法确认提问时的报告版本」，不说「其他版本」',
      heads.length === 2 && heads.every((t) => t === '无法确认提问时的报告版本'), heads.join(' | '))
    check('……两处都给「重试」', await page.locator(`${card} [data-inquiry-group="unsure"] [data-inquiry-docs-retry]`).count() === 1
      && await page.locator(`${pane} [data-inquiry-group="unsure"] [data-inquiry-docs-retry]`).count() === 1)
    const unsureRatio = await ratioOf(page, `${pane} [data-inquiry-group="unsure"] [data-inquiry-about-text]`)
    check('……那一句（深色）对比度 ≥ 4.5', unsureRatio >= 4.5, unsureRatio.toFixed(2))
    // 重试，这回取得到：这张审批审的就是眼前这一稿，更早那一轮审的是另一稿
    state.approvals[RUNS.approve] = [approvalRow(fx.approval_id, RUNS.approve, fx.reports.approve.doc_artifact),
      approvalRow('ap-inq-0', RUNS.approve, '0'.repeat(64))]
    await page.locator(`${pane} [data-inquiry-docs-retry]`).click()
    const want = JSON.stringify([['u3', ['34']], ['other', ['12']]])
    const placed = await until(async () => JSON.stringify((await groupsOf(page, pane)).map((g) => [g.unit, g.entries])) === want
      && JSON.stringify((await groupsOf(page, card)).map((g) => [g.unit, g.entries])) === want)
    const [gc2, gp2] = [await groupsOf(page, card), await groupsOf(page, pane)]
    check('重试取到之后（证据页签）：这张审批里问的回到句子下面，更早那一轮算报告其他版本', placed
      && JSON.stringify(gp2.map((g) => [g.unit, g.entries])) === want, JSON.stringify(gp2.map((g) => [g.unit, g.entries])))
    check('重试取到之后（待审批卡）：更早那一轮也改写成报告其他版本', JSON.stringify(gc2.map((g) => [g.unit, g.entries])) === want
      && gc2.at(-1)?.about.startsWith('报告其他版本上的质询'), JSON.stringify(gc2.map((g) => [g.unit, g.about.slice(0, 12)])))
    check('……重试只再取一次审批列表，两处共用', hits.approvals.length === 2, JSON.stringify(hits.approvals))
    const [tc2, tp2] = [await tally(page, card), await tally(page, pane)]
    check('……两处的次数仍和条数一致', tc2.head === '2' && tc2.shown === 2 && tp2.head === '2' && tp2.shown === 2, JSON.stringify({ tc2, tp2 }))
    check('运行详情：页面没有报错', errors.length === 0, errors.join('；'))
    await ctx.close()
  }
})

await section('回答期间另开了提问框，正在回答的那一条没问成：新写的不被覆盖', async () => {
  const { ctx, page, state, errors } = await open('inquiry-approval')
  await expand(page)
  let release
  state.gate = new Promise((r) => { release = r })
  state.reply = { status: 500, json: { detail: '核查模型调用超时，请稍后重试' } }
  await page.locator('[data-seg="s4"]').focus()
  await page.keyboard.press('Tab')
  await page.keyboard.press('Enter')
  await page.locator('[data-inquiry-input]').fill('第一个问题：销售额含不含线上？')
  await page.locator('[data-inquiry-submit]').click()
  await page.locator('[data-inquiry-pending]').waitFor()
  await page.waitForTimeout(3300)
  const status = await page.locator('[data-inquiry-pending] [role="status"]').evaluateAll((els) => els.map((el) => el.textContent.trim()))
  check('等了几秒：播报区只有不变的那句，「已等待 N 秒」在播报区外、读屏不读', JSON.stringify(status) === JSON.stringify(['正在查询与回答，通常需要几十秒'])
    && (await page.locator('[data-inquiry-waited]').getAttribute('aria-hidden')) === 'true'
    && (await text(page.locator('[data-inquiry-waited]'))).startsWith('· 已等待'), JSON.stringify(status))
  await page.locator('[data-inquiry-ask-report]').click()
  await page.locator('[data-inquiry-input]').fill('第二个问题：数据截止到哪天？')
  release()
  await page.locator('[data-inquiry-failed-ask]').waitFor()
  await page.waitForTimeout(150)
  check('提问框原样留着：还是整份报告、还是新写的问题', await composer(page).count() === 1
    && (await composer(page).getAttribute('data-inquiry-composer')) === 'report'
    && (await page.locator('[data-inquiry-input]').inputValue()) === '第二个问题：数据截止到哪天？')
  const failedAsk = group(page, 'u1').locator('[data-inquiry-failed-ask]')
  check('没问成的那一条挂在它那一句下面：问题和服务端的说明都在', (await text(failedAsk.locator('[data-inquiry-question]'))) === '第一个问题：销售额含不含线上？'
    && (await text(failedAsk.locator('[data-inquiry-error]'))) === '没有问成：核查模型调用超时，请稍后重试', await text(failedAsk))
  check('……原因用 role=alert 报出', (await failedAsk.locator('[data-inquiry-error]').getAttribute('role')) === 'alert')
  check('提问框开着时不给「放回提问框」（免得盖掉新写的）', await failedAsk.locator('[data-inquiry-reask]').count() === 0)
  await composer(page).locator('button', { hasText: '取消' }).click()
  await failedAsk.locator('[data-inquiry-reask]').click()
  await composer(page).waitFor()
  check('放回提问框：就那一句、原来的问题，没问成的那一条拿掉', (await composer(page).getAttribute('data-inquiry-composer')) === 'unit'
    && (await page.locator('[data-inquiry-input]').inputValue()) === '第一个问题：销售额含不含线上？'
    && await group(page, 'u1').locator('[data-inquiry-composer]').count() === 1 && await page.locator('[data-inquiry-failed-ask]').count() === 0)
  check('批准、驳回照常可用', !(await approve(page).isDisabled()))
  check('页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('回答回来时读屏播报，连着两次都报', async () => {
  const { ctx, page, state, errors } = await open('inquiry-approval')
  await entry(page, 31).waitFor()
  await page.evaluate(() => {
    const live = document.querySelector('[data-inquiry="write"] [data-inquiry-live]')
    window.__heard = []
    new MutationObserver(() => { const t = live.textContent; if (t) window.__heard.push(t) })
      .observe(live, { childList: true, characterData: true, subtree: true })
  })
  let seq = 100
  state.reply = (body) => ({ status: 200, json: answerFor(body, { inquiry: { ...answerFor(body).inquiry, seq: seq++ } }) })
  for (const n of [100, 101]) {
    await page.locator('[data-inquiry-ask-report]').click()
    await page.locator('[data-inquiry-input]').fill('数据截止到哪一天？')
    await page.locator('[data-inquiry-submit]').click()
    await entry(page, n).waitFor()
    await page.waitForTimeout(200)
  }
  const heard = await page.evaluate(() => window.__heard)
  check('同一个问题问两遍，两次回答各报一遍「已收到回答」', heard.length === 2 && heard.every((t) => t === '已收到回答：数据截止到哪一天？'),
    JSON.stringify(heard))
  check('页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('花费估不出不写 $0；查询列表收着也写截断；核对不了、猜测来源的字照报告的样子画', async () => {
  const items = clone(fx.items)
  items[0].usage = { input_tokens: 5210, output_tokens: 640, cost_usd: 0, calls: 3, unpriced_calls: 3 }
  items[1].usage = { input_tokens: 5210, output_tokens: 640, cost_usd: 0.002, calls: 3, unpriced_calls: 1 }
  const docId = items[0].answer.doc_artifact
  const doc = clone(fx.docs[docId])
  // 回答里再加一句：一个核对不了的表名、一个按数值猜测来源的数
  doc.blocks.push({ id: 'b9', type: 'paragraph', units: [{ id: 'u9', kind: 'claim', segments: [
    { id: 's90', kind: 'structural', text: '\n\n', state: 'neutral' },
    { id: 's91', kind: 'text', text: '退款明细在 ', state: 'neutral' },
    { id: 's92', kind: 'entity', text: 'refund_items', state: 'none', issue: 'unverified_entity' },
    { id: 's93', kind: 'text', text: ' 表中，约占 ', state: 'neutral' },
    { id: 's94', kind: 'number', text: '96%', state: 'candidate' },
    { id: 's95', kind: 'text', text: '。', state: 'neutral' },
  ] }] })
  const { ctx, page, errors } = await open('inquiry-approval', { setup: (s) => { s.lists[RUNS.approve] = items; s.docs[docId] = doc } })
  const e1 = entry(page, 31)
  await e1.locator('[data-inquiry-answer-doc]').waitFor()
  const c1 = e1.locator('[data-inquiry-cost]')
  check('模型都不在价格目录中：写「金额无法估算」，不写 $0', (await text(c1)) === '· 金额无法估算'
    && ((await c1.getAttribute('title')) ?? '').startsWith('3 次调用的模型不在价格目录中'), await text(e1.locator('[data-inquiry-judged]')))
  check('部分调用估不出金额：写「≥」', (await text(entry(page, 34).locator('[data-inquiry-cost]'))) === '· 花费 ≥ $0.002',
    await text(entry(page, 34).locator('[data-inquiry-judged]')))
  const e2 = entry(page, 34)
  check('查询列表收着时也写明有几次结果被截断', (await e2.locator('[data-inquiry-queries-toggle]').getAttribute('aria-expanded')) === 'false'
    && (await text(e2.locator('[data-inquiry-queries-truncated]'))) === '· 其中 1 次结果已截断'
    && await e1.locator('[data-inquiry-queries-truncated]').count() === 0)
  const marks = await e1.locator('.ev-mark').evaluateAll((els) => els.map((el) => [el.getAttribute('data-ev-state'), el.getAttribute('data-ev-line'),
    getComputedStyle(el).textDecorationStyle, el.textContent]))
  check('核对不了的名称、猜测的来源：点状线，读屏听得到是什么状态', JSON.stringify(marks.map((m) => m.slice(0, 3))) === JSON.stringify([
    ['unverified', 'dotted', 'dotted'], ['candidate', 'dotted', 'dotted']]) && marks[0][3].includes('（') && marks[1][3].includes('（'),
  JSON.stringify(marks))
  check('页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('没有逐段证据的报告：只说可以就整份报告提问；列表取回来之前不说「还没有问答」', async () => {
  let release
  const { ctx, page, errors } = await open('inquiry-approval-nodoc', { waitUntil: 'load', setup: (s) => {
    s.lists[RUNS.approve] = []
    s.listGate = new Promise((r) => { release = r })
  } })
  await page.locator('[data-inquiry="write"]').waitFor()
  await page.waitForTimeout(300)
  check('列表还在取：不说「还没有问答、可以这样问」', await page.locator('[data-inquiry-intro]').count() === 0)
  release()
  await page.locator('[data-inquiry-intro]').waitFor()
  check('没有逐段证据：没有「展开逐段证据」，提示只说就整份报告提问', await page.locator('[data-approval-evidence]').count() === 0
    && (await text(page.locator('[data-inquiry-intro]'))) === '这份报告没有逐段证据，可以就整份报告提问')
  check('页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('设置里关了审批质询：不给入口，写明已关闭，之前的问答照列', async () => {
  const { ctx, page, hits, errors } = await open('inquiry-approval', { setup: (s) => { s.settings = { verify: { inquiry: { enabled: false } } } } })
  await entry(page, 31).waitFor()
  await page.waitForTimeout(200)
  check('关了：没有「就整份报告提问」，写明已在设置中关闭', await page.locator('[data-inquiry-ask-report]').count() === 0
    && (await text(page.locator('[data-inquiry-disabled]'))) === '审批质询已在设置中关闭')
  check('关了：之前的问答照列', await page.locator('[data-inquiry-entry]').count() === 3)
  await expand(page)
  check('关了：报告上没有「质询」', await page.locator('[data-inquiry-ask]').count() === 0)
  await select(page, 'u4', '枫岭区销售额最高')
  await page.waitForTimeout(200)
  check('关了：划选也不浮出「质询」', await page.locator('[data-inquiry-selection]').count() === 0)
  check('关了：没有发出提问', hits.asks.length === 0)
  check('页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('遮罩：接口给了原值、只在快照里记着遮罩列时，表格、悬停说明、导出的 CSV 都不出原值', async () => {
  const raw = clone(fx.snapshots[MEMBERS])
  raw.rows = raw.rows.map((r, i) => [r[0], `139-0000-${1000 + i}`, r[2], r[3]])
  delete raw.masked
  raw.mask_columns = ['phone']
  const { ctx, page, errors } = await open('inquiry-approval', { setup: (s) => { s.snaps[MEMBERS] = { id: MEMBERS, content: raw } } })
  const e2 = entry(page, 34)
  await e2.locator('[data-inquiry-answer-doc]').waitFor()
  await e2.locator('[data-inquiry-cite="Q2"]').click()
  const q2 = e2.locator('[data-inquiry-query="Q2"]')
  await q2.locator('[data-inquiry-snapshot="ok"]').waitFor()
  const leak = await q2.evaluate((el) => [el.innerText, ...[...el.querySelectorAll('[title]')].map((x) => x.getAttribute('title'))]
    .filter((t) => t && t.includes('139-0000')).length)
  check('表格里、悬停说明里都没有原值', leak === 0 && await q2.locator('td[data-masked]').count() === 50, `${leak} 处`)
  const [download] = await Promise.all([page.waitForEvent('download'), q2.getByRole('button', { name: 'CSV' }).click()])
  const csv = readFileSync(await download.path(), 'utf8')
  check('导出的 CSV 也没有原值，写「已遮罩」', !csv.includes('139-0000') && csv.includes('已遮罩'), csv.split('\n').slice(0, 2).join(' | '))
  check('页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('写进运行记录的问答：画布正显示这次运行、没在接流时补接一次，时间线收到那一条', async () => {
  const urls = []
  const { ctx, page, state, errors } = await open('inquiry-approval', { ws: (ws) => {
    urls.push(ws.url())
    ws.send(JSON.stringify({ seq: 40, type: 'approval.inquiry', node_id: null, ts: 1759646000, data: {
      question: '这份报告的数据截止到哪一天？', quote: null, report: 'write', unit: null, approval_id: fx.approval_id,
      answer_text: '截止到 9 月 30 日。', answer_doc: null, ok: true, limit: null, error: null, inquiry_artifacts: [], queries: [] } }))
    ws.send(JSON.stringify({ seq: 0, type: 'stream.end', node_id: null, ts: 0, data: { status: 'interrupted' } }))
  } })
  await entry(page, 31).waitFor()
  // 画布正显示这次运行：停在审批上，流已经收尾（streaming 为假），手上最大的序号是 39
  await page.evaluate(([run]) => window.__studio.setState({ run: { id: run, status: 'interrupted', run_class: 'exploratory' },
    streaming: false, unsubscribe: null, events: [{ seq: 39, type: 'human.requested', node_id: 'review', ts: 1759645000, data: {} }] }), [RUNS.approve])
  await page.locator('[data-inquiry-ask-report]').click()
  await page.locator('[data-inquiry-input]').fill('这份报告的数据截止到哪一天？')
  await page.locator('[data-inquiry-submit]').click()
  await entry(page, 40).waitFor()
  check('补接事件流：从手上最大的序号之后接', await until(async () => urls.length === 1) && urls[0].includes('after=39'), urls.join('、'))
  check('那一条质询事件进了画布的事件（时间线据此出「审批人质询」）', await until(() => page.evaluate(() =>
    window.__studio.getState().events.some((e) => e.type === 'approval.inquiry' && e.seq === 40))))
  // 没写进运行记录的不补接
  state.reply = (body) => ({ status: 200, json: answerFor(body, { inquiry: { ...answerFor(body).inquiry, seq: null }, recorded: false,
    note: '运行已不在等待审批' }) })
  await page.locator('[data-inquiry-ask-report]').click()
  await page.locator('[data-inquiry-input]').fill('再问一句')
  await page.locator('[data-inquiry-submit]').click()
  await page.locator('[data-inquiry-recorded="false"]').waitFor()
  await page.waitForTimeout(300)
  check('没写进运行记录的不补接', urls.length === 1, urls.join('、'))
  check('页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

/** 页面和某个容器都没有横向滚动（表格、SQL 框自己的滚动条不算） */
async function overflow(page, sel) {
  return page.evaluate((s) => {
    const doc = document.documentElement
    const box = document.querySelector(s)
    return { page: doc.scrollWidth - doc.clientWidth, box: box ? box.scrollWidth - box.clientWidth : -1 }
  }, sel)
}

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

/**
 * 某个元素的字色对它所在那一块底色的对比度：底色往上找第一个不透明的，最底下是 body。写成函数声明：前面几段（审批列表
 * 取不到的那一组）也要用
 */
function ratioOf(page, sel) {
  return page.evaluate((s) => {
    const el = document.querySelector(s)
    if (!el) return null
    let bgEl = el
    let bg = 'rgba(0, 0, 0, 0)'
    while (bgEl && /rgba\(0, 0, 0, 0\)|transparent/.test(bg)) {
      bg = getComputedStyle(bgEl).backgroundColor
      bgEl = bgEl.parentElement
    }
    return { fg: getComputedStyle(el).color, bg, base: getComputedStyle(document.body).backgroundColor }
  }, sel).then((c) => (c ? contrast(c.fg, c.bg, c.base) : 0))
}

await section('窄屏与深浅色', async () => {
  for (const theme of ['dark', 'light']) {
    const { ctx, page, state, errors } = await open('inquiry-approval', { width: 390, height: 844, theme })
    state.reply = (body) => ({ status: 200, json: answerFor(body, { inquiry: { ...answerFor(body).inquiry, seq: null }, recorded: false,
      note: '运行已不在等待审批' }) })
    await page.locator('[data-inquiry-entry="34"] [data-inquiry-answer-doc]').waitFor()
    await expand(page)
    await page.locator('[data-inquiry-entry="34"] [data-inquiry-cite="Q2"]').click()
    await page.locator('[data-inquiry-entry="34"] [data-inquiry-query="Q2"] [data-inquiry-snapshot="ok"]').waitFor()
    await page.locator('[data-inquiry-ask-report]').click()
    await page.locator('[data-inquiry-input]').fill('窄屏里问一句')
    await page.locator('[data-inquiry-submit]').click()
    await page.locator('[data-inquiry-unrecorded]').waitFor()
    await page.locator('[data-seg="s4"]').focus()
    await page.keyboard.press('Tab')
    await page.keyboard.press('Enter')
    await composer(page).waitFor()
    const o = await overflow(page, '[data-approval]')
    check(`390px（${theme}）：展开逐段证据、查询结果、提问框也没有横向溢出`, o.page <= 0 && o.box <= 1, JSON.stringify(o))
    // 「质询」量的是悬停这一句时显出来的样子（平时是淡淡的提示，悬停、焦点在这一句上时才是要读清的那个按钮）
    await hoverUnit(page, 'u1')
    await page.waitForTimeout(100)
    const ratios = {
      unrecorded: await ratioOf(page, '[data-inquiry-unrecorded]'),
      limit: await ratioOf(page, '[data-inquiry-limit]'),
      failed: await ratioOf(page, '[data-inquiry-failed]'),
      ask: await ratioOf(page, '[data-inquiry-ask="u1"]'),
      judged: await ratioOf(page, '[data-inquiry-judged]'),
      meta: await ratioOf(page, '[data-inquiry-meta]'),
    }
    check(`${theme}：没写入、触顶、没答成的说明，「质询」，模型说明对比度都 ≥ 4.5`, Object.values(ratios).every((r) => r >= 4.5),
      JSON.stringify(Object.fromEntries(Object.entries(ratios).map(([k, v]) => [k, Number(v.toFixed(2))]))))
    await shot(page, `narrow-${theme}`)
    // 报错那一行
    state.reply = ERRORS.inquiry_busy
    await page.locator('[data-inquiry-input]').fill('再问一句')
    await page.locator('[data-inquiry-submit]').click()
    await page.locator('[data-inquiry-error]').waitFor()
    const r = await ratioOf(page, '[data-inquiry-error]')
    const o2 = await overflow(page, '[data-approval]')
    check(`390px（${theme}）：报错说明不横向溢出、对比度 ≥ 4.5`, o2.page <= 0 && o2.box <= 1 && r >= 4.5, `${JSON.stringify(o2)} · ${r.toFixed(2)}`)
    check(`390px（${theme}）：页面没有报错`, errors.length === 0, errors.join('；'))
    await ctx.close()
  }
  for (const theme of ['dark', 'light']) {
    // 回答期间批准 / 驳回下面那句原因：审批卡铺着一层提醒色的底，提醒色的字在浅色主题下不够深
    const { ctx, page, state, errors } = await open('inquiry-approval', { theme })
    state.gate = new Promise(() => {})
    await page.locator('[data-inquiry-ask-report]').click()
    await page.locator('[data-inquiry-input]').fill('回答期间的说明看得清吗')
    await page.locator('[data-inquiry-submit]').click()
    await page.locator('[data-approval-inquiry-busy]').waitFor()
    const r = await ratioOf(page, '[data-approval-inquiry-busy]')
    check(`${theme}：回答期间批准 / 驳回不可用的原因，对比度 ≥ 4.5`, r >= 4.5, r.toFixed(2))
    check(`${theme}：页面没有报错`, errors.length === 0, errors.join('；'))
    await ctx.close()
  }
  {
    const { ctx, page, errors } = await open('inquiry-approval-dense', { width: 1100, height: 900 })
    await page.locator('[data-inquiry-entry="34"] [data-inquiry-answer-doc]').waitFor()
    await expand(page)
    await page.locator('[data-inquiry-entry="31"] [data-inquiry-cite="Q1"]').first().click()
    await page.locator('[data-inquiry-entry="31"] [data-inquiry-snapshot="ok"]').waitFor()
    await clickAsk(page, 'u2')
    await composer(page).waitFor()
    const o = await overflow(page, '[data-fixture="inquiry-approval-dense"]')
    check('360px 右栏：查询结果、提问框都不撑宽右栏', o.page <= 0 && o.box <= 0, JSON.stringify(o))
    await shot(page, 'dense-dark')
    check('360px 右栏：页面没有报错', errors.length === 0, errors.join('；'))
    await ctx.close()
  }
  {
    const { ctx, page, errors } = await open('inquiry-run-explore', { width: 390, height: 844, theme: 'light' })
    await page.locator('[data-inquiry="write"] [data-inquiry-entry="58"]').waitFor()
    const o = await overflow(page, '[data-evidence-main]')
    check('390px 运行详情（light）：报告和问答串不横向溢出', o.page <= 0 && o.box <= 0, JSON.stringify(o))
    await shot(page, 'run-explore-narrow-light')
    check('390px 运行详情：页面没有报错', errors.length === 0, errors.join('；'))
    await ctx.close()
  }
  for (const theme of ['dark', 'light']) {
    // 运行详情「工件」页签：质询回答的说明可能很长（问题的前 40 字），窄屏上截断、不撑宽；种类和说明深浅色都看得清
    const { ctx, page, errors } = await open('inquiry-run-artifacts', { width: 390, height: 844, theme })
    const long = fx.run_artifacts.find((a) => a.kind === 'inquiry_answer' && [...(a.meta.question ?? '')].length >= 40)
    const row = `[data-artifact-id="${long.id}"] [data-action="artifact-open"]`
    await page.locator(row).waitFor()
    const o = await overflow(page, '[data-run-artifacts]')
    const ratios = { kind: await ratioOf(page, `${row} [data-artifact-kind-label]`), about: await ratioOf(page, `${row} [data-artifact-about]`) }
    check(`390px 工件页签（${theme}）：质询回答的长说明截断、不横向溢出，种类和说明对比度 ≥ 4.5`,
      o.page <= 0 && o.box <= 0 && ratios.kind >= 4.5 && ratios.about >= 4.5,
      JSON.stringify({ o, kind: Number(ratios.kind.toFixed(2)), about: Number(ratios.about.toFixed(2)) }))
    await shot(page, `run-artifacts-narrow-${theme}`)
    check(`390px 工件页签（${theme}）：页面没有报错`, errors.length === 0, errors.join('；'))
    await ctx.close()
  }
})

await section('审批卡的批准走待办回复：/api/tasks/{id}/respond，不按运行取待办列表', async () => {
  const { ctx, page, hits, errors } = await open('inquiry-approval', {
    setup: (s) => { s.approvals[RUNS.approve] = [approvalRow(fx.approval_id, RUNS.approve, fx.reports.approve.doc_artifact)] },
  })
  await approve(page).waitFor()
  check('审批带着待办：卡片画出来，没有按运行去取待办列表', hits.tasks.length === 0, hits.tasks.join('、'))
  await approve(page).click()
  await page.waitForTimeout(300)
  const sent = hits.responds
  check('批准：只发一次待办回复，按审批对应的待办', sent.length === 1 && sent[0].task === `task-${fx.approval_id}`, JSON.stringify(sent))
  check('……决定是批准', sent[0]?.body?.decision === 'approve', JSON.stringify(sent[0]?.body))
  check('……不再走老的审批回复接口', !hits.writes.some((w) => /\/api\/approvals\/[^/]+\/decide$|\/resume$/.test(w)), hits.writes.join('、'))
  check('页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await browser.close()
console.log(failed ? `\n✗ 审批质询：${failed} 项未通过` : '\n✓ 审批质询：全部通过')
process.exit(failed ? 1 : 0)
