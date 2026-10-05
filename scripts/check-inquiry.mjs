// 审批质询的回归检查：报告上的入口（run/EvidenceDoc.tsx 的句末「质询」、划选原文浮出的「质询」）、问答串和提问框
// （run/Inquiry.tsx）、审批卡上的挂法（run/ApprovalReport.tsx、run/RunPanel.tsx）、运行详情「证据」页签的挂法
// （pages/runs/EvidencePane.tsx）。
//
// 守的是这些「看着没坏、其实用不了」的地方：悬停、键盘聚焦一句都不出「质询」（或者出了 Tab 却到不了、整份报告多出一串
// Tab 位）、没有可点内容的句子键盘走不到、划选原文不浮出入口（或者带上的原文混进了徽标和「质询」本身）、提交的请求体
// 和约定对不上（多带、少带 quote、approval_id，unit 写错）、回答期间批准 / 驳回还能点、回答回来不挂到那句话下面、
// 进入审批卡时问答串没有恢复、问答串写不出问的是哪一句、回答里的数字点了不去取质询工件（或者每点一次取一次、标不出
// 那一格）、引用报告已有出处的数字点了没反应、查询的 SQL / 行数 / 截断 / 遮罩不显示、触顶和没答成不说、recorded 为假
// 不醒目、409 / 422 不照服务端的中文说明显示（或者问题文字丢了）、Esc 关不掉提问框（或者连带收起了别的、焦点掉到页面上）、
// 已处理的审批和封存了的正式运行还给入口、封存了的探索运行反而没有；窄屏（390px）被撑出横向滚动、浅色主题下看不清。
//
// 跑在预览页上：/preview.html?syn=inquiry-*（用例见 frontend/src/dev/fixtures/inquiry.tsx）。夹具在
// frontend/src/dev/fixtures/inquiry.json，报告文档和回答文档是后端 build_catalog + compose_doc 真实组装的（虚构的数据）。
// 质询的三个接口、工件、证据图、片段接口都在这里伪造；别的写请求一律拦掉并记下来，别的 GET 答 404，不碰真实服务。
//
//   node scripts/check-inquiry.mjs
// 对别的实例跑时带上地址：AGENTLAB_WEB=http://localhost:<前端端口> node scripts/check-inquiry.mjs
// INQUIRY_SHOTS=<目录> 时截几张图，供人眼复核。
import { mkdirSync, readFileSync } from 'node:fs'
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const CHROME = process.env.CHROME_PATH ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const SHOTS = process.env.INQUIRY_SHOTS ?? ''
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
 * state.gate 是个没兑现的 Promise 时先挂着，测「回答期间」）。记下列表、提问、取快照、取工件的请求和拦下的写请求
 */
async function open(syn, { width = 1100, height = 900, theme = 'dark' } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height }, colorScheme: theme, timezoneId: 'Asia/Shanghai' })
  ctx.setDefaultTimeout(6000)
  const page = await ctx.newPage()
  const hits = { lists: [], asks: [], snapshots: [], artifacts: [], segments: [], writes: [], other: [] }
  const state = {
    lists: { [RUNS.approve]: fx.items, [RUNS.formal]: fx.items, [RUNS.explore]: fx.explore_items },
    reply: (body) => ({ status: 200, json: answerFor(body) }),
    gate: null,
  }
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
    if (method !== 'GET') {
      hits.writes.push(`${method} ${path}`)
      return route.abort()
    }
    if (inq) {
      const run = decodeURIComponent(inq[1])
      hits.lists.push({ run, report: url.searchParams.get('report'), unit: url.searchParams.get('unit') })
      return route.fulfill({ json: { items: state.lists[run] ?? [] } })
    }
    const snap = path.match(/^\/api\/runs\/([^/]+)\/inquiries\/artifacts\/([^/]+)$/)
    if (snap) {
      const id = decodeURIComponent(snap[2])
      hits.snapshots.push(id)
      const body = fx.snapshots[id]
      if (!body) return route.fulfill({ status: 404, json: { detail: '不是这次运行的质询引用过的工件' } })
      // 约定没定死外面包不包一层：一份包在 {id, content} 里，其余是快照本身，两种界面都得认
      return route.fulfill({ json: id === REFUND ? { id, content: body } : body })
    }
    const art = path.match(/^\/api\/artifacts\/([^/]+)$/)
    if (art) {
      const id = decodeURIComponent(art[1])
      hits.artifacts.push(id)
      return fx.docs[id] ? route.fulfill({ json: { id, content: fx.docs[id] } })
        : route.fulfill({ status: 404, json: { detail: '工件不存在，可能已被删除' } })
    }
    const seg = path.match(/^\/api\/runs\/([^/]+)\/evidence\/segments\/([^/]+)$/)
    if (seg) {
      hits.segments.push(decodeURIComponent(seg[2]))
      return route.fulfill({ json: {} })
    }
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
  await page.goto(`${WEB}/preview.html?syn=${syn}&theme=${theme}`, { waitUntil: 'networkidle' })
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
/** 像人一样点某一句的「质询」：先把鼠标移到这一句的字上（「质询」这才出现），再点它 */
async function clickAsk(page, unit) {
  // 句子会折行：包住它的框的左上角可能落在上一句上，鼠标要放到这一句第一行的字上
  const at = await page.locator(`[data-ev-unit="${unit}"]`).evaluate((el) => {
    el.scrollIntoView({ block: 'center' })
    const r = el.getClientRects()[0]
    return { x: r.left + Math.min(8, r.width / 2), y: r.top + r.height / 2 }
  })
  await page.mouse.move(at.x, at.y)
  await askBtn(page, unit).click()
}
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
  check('平时都看不见、不占地方', !(await seen(askBtn(page, 'u2'))) && (await askBtn(page, 'u2').boundingBox())?.width < 1)
  await page.locator('[data-ev-unit="u2"] [data-seg="s9"]').hover()
  await page.waitForTimeout(100)
  check('悬停一句：这一句的「质询」出现，别的句子的不出现', await seen(askBtn(page, 'u2')) && !(await seen(askBtn(page, 'u1'))))
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
  // 报告框压矮、滚到某一句的第一行贴着框的底边（取压矮之后落在框外、滚得过去的第一句），在那一行开头划选几个字：
  // 浮出的按钮放在选区下方会被框裁掉，得换到上方
  const edge = await page.evaluate(() => {
    const box = document.querySelector('[data-approval-evidence-doc]')
    if (!box) return null
    box.style.maxHeight = '150px'
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
    const { ctx, page, hits, errors } = await open('inquiry-run-formal')
    await page.locator('[data-evidence-report="write"] [data-ev-unit], [data-evidence-report="write"] [data-seg="s4"]').first().waitFor()
    await page.waitForTimeout(300)
    check('正式运行封存后：报告上没有「质询」，也没有问答串和提问入口',
      await page.locator('[data-inquiry-ask], [data-inquiry], [data-inquiry-ask-report]').count() === 0)
    await page.evaluate(() => {
      const el = [...document.querySelectorAll('[data-evidence-report="write"] [data-block]')].find((b) => b.textContent.includes('枫岭区'))
      const range = document.createRange()
      range.selectNodeContents(el)
      getSelection().removeAllRanges()
      getSelection().addRange(range)
    })
    await page.waitForTimeout(200)
    check('正式运行封存后：划选也不浮出「质询」', await page.locator('[data-inquiry-selection]').count() === 0)
    check('正式运行封存后：不取问答列表', hits.lists.length === 0, JSON.stringify(hits.lists))
    check('正式运行：页面没有报错', errors.length === 0, errors.join('；'))
    await ctx.close()
  }
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

/** 某个元素的字色对它所在那一块底色的对比度：底色往上找第一个不透明的，最底下是 body */
const ratioOf = (page, sel) => page.evaluate((s) => {
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
    const ratios = {
      unrecorded: await ratioOf(page, '[data-inquiry-unrecorded]'),
      limit: await ratioOf(page, '[data-inquiry-limit]'),
      failed: await ratioOf(page, '[data-inquiry-failed]'),
      ask: await ratioOf(page, '[data-inquiry-ask="u1"] > span'),
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
})

await browser.close()
console.log(failed ? `\n✗ 审批质询：${failed} 项未通过` : '\n✓ 审批质询：全部通过')
process.exit(failed ? 1 : 0)
