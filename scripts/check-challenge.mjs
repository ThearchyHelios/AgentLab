// 反方审稿的回归检查：审批卡上的反方审稿（run/ChallengeSummary.tsx）和检查器里反方审稿节点的配置（canvas/nodeDefs.ts 的
// challenge、canvas/verifyFields.tsx）。
//
// 守的是这些「看着没坏、其实用不了」的地方：概况的句数和条目对不上、有被推翻的结论却不提醒降档（或者原因只藏在悬停里）、
// 没有被推翻的结论也写降档、只标注时也写降档、探索运行里说出具、运行类别取不到时降档提示悄悄消失、没经过反方审稿的报告
// 也冒出一块空框（或者白发一次请求）、点开一句不取工件（或者每点一句取一次）、理由里的数字看不出引用了哪次查询、
// 裸数字没被标出来、理由文档是另一个工件时不去取、查询的 SQL / 行数 / 截断不显示、工件取不到时卡死在加载中、
// 审稿记录没存上时每句还能点开、条目只列前十句时看不到其余的（或者补的时候吞掉原文相同的另一句、键盘焦点掉到页面上）；
// 检查器里的字段写不进节点配置（或者写进去的不是约定的形状：跟随设置要把这一项拿掉，不能写成空字符串）、下拉里列出了
// 下游的报告、留空时认的报告和服务端不一样（「初稿 → 终稿 → 反方审稿」审终稿）、选中的报告被删了还看着像「未选择」、
// 选中的不是报告撰写节点却说成不在上游、反方模型和撰写报告的模型相同不提醒、画布校验已经报了的又在字段下面说一遍、
// 节点卡片的摘要还写着功能开发中；窄屏（390px）被撑出横向滚动、浅色主题下标签看不清。
//
// 跑在预览页上：/preview.html?syn=challenge-*（用例见 frontend/src/dev/fixtures/challenge.tsx）。夹具在
// frontend/src/dev/fixtures/challenge.json，理由文档是后端 run_verify_agent + compose_doc 真跑出来的（虚构的库和数字）。
// 运行类别（GET /api/runs/{id}）、工件（GET /api/artifacts/{id}）、画布校验（两个纯计算的 POST）在这里伪造，别的非 GET
// 一律拦掉并记下来，别的 GET 放行。
//
//   node scripts/check-challenge.mjs
// 对别的实例跑时带上地址：AGENTLAB_WEB=http://localhost:<前端端口> node scripts/check-challenge.mjs
// CHALLENGE_SHOTS=<目录> 时亮暗两套各截几张，供人眼复核。
import { mkdirSync, readFileSync } from 'node:fs'
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const CHROME = process.env.CHROME_PATH ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const SHOTS = process.env.CHALLENGE_SHOTS ?? ''
const root = new URL('..', import.meta.url).pathname
const fx = JSON.parse(readFileSync(`${root}frontend/src/dev/fixtures/challenge.json`, 'utf8'))
const ART = fx.challenge.artifact
const DOC = fx.doc_u8
const MANY = fx.many.artifact
const BROKEN = 'e'.repeat(64)
// challenge-approval-dup 的工件：同 many，只把补出来的 u30（被推翻）的原文改成和条目 u20 一字不差
const DUP = 'd'.repeat(64)
const DUP_CONTENT = {
  ...fx.artifacts[MANY],
  claims: fx.artifacts[MANY].claims.map((c) => (c.unit === 'u30' ? { ...c, text: fx.many.items.find((it) => it.unit === 'u20').text } : c)),
}
const RUNS = fx.runs
const RUN_CLASS = {
  [RUNS.formal]: 'formal', [RUNS.flag]: 'formal', [RUNS.explore]: 'exploratory', [RUNS.none]: 'formal',
  [RUNS.many]: 'formal', [RUNS.broken]: 'formal', [RUNS.clear]: 'formal', [RUNS.clearFlag]: 'formal',
  [RUNS.exploreFlag]: 'exploratory', [RUNS.runfail]: 'formal', [RUNS.dup]: 'formal', [RUNS.norecord]: 'formal',
}
const LABEL = { stands: '经受住', weakened: '被削弱', refuted: '被推翻', inconclusive: '无法判断' }
const FLAG_NOTE = '按节点设置，被推翻的结论只做标注，不影响出具档位'
const DEGRADE_NOTE = '被推翻的结论计入出具缺口，本次正式运行将降档出具'
const FAILED = '无法获取反方审稿的详细内容'
// F3a 画布校验（schema._check_challenge）在同样情形下的说法：检查器按 field 挂在字段下面
const SERVER_MANY = '「反方审稿」的上游有多个「报告撰写」节点（「写月报」、「写周报」），请在「要审的报告」中选择要审的那一份'
const SERVER_GHOST = '「反方审稿」的「要审的报告」选择了「ghost」，但工作流中不存在这个节点'
const SERVER_SAME = '反方模型与报告「写月报」的写作模型相同（「writer-large」），等于自己审自己，难以发现写作模型自身的错误。'
  + '请在「反方模型」中选择另一个模型'

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

/**
 * 开一个预览用例。工件按夹具答（BROKEN 先答 404，state.broken 置 false 后答正常内容），运行类别按 RUN_CLASS 答
 * （runfail 先答 500，state.runFail 置 false 后照常答），画布校验答 state.issues；
 * 记下取了哪些工件、问了哪些运行、拦下了哪些写请求
 */
async function open(syn, { width = 1100, height = 900, theme = 'dark' } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height }, colorScheme: theme, timezoneId: 'Asia/Shanghai' })
  ctx.setDefaultTimeout(6000)
  const page = await ctx.newPage()
  const hits = { artifacts: [], runs: [], writes: [] }
  const state = { broken: true, runFail: true, issues: [] }
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('dialog', (d) => { errors.push(`原生对话框：${d.message()}`); void d.dismiss() })
  await page.route((u) => new URL(u).pathname.startsWith('/api/'), async (route) => {
    const req = route.request()
    const path = new URL(req.url()).pathname
    if (req.method() === 'POST' && path === '/api/workflows/validate') {
      return route.fulfill({ json: { ok: !state.issues.some((i) => i.level === 'error'), issues: state.issues } })
    }
    if (req.method() === 'POST' && path === '/api/workflows/variables') return route.fulfill({ json: { variables: [], issues: [] } })
    // 画布载入一张图时会找（没有就建）它的助手会话：答一份假的，不碰真实会话
    if (path === '/api/conversations') {
      return route.fulfill({ json: req.method() === 'GET' ? [] : { id: 'conv-challenge', kind: 'canvas', title: '', turns: [] } })
    }
    if (req.method() !== 'GET') {
      hits.writes.push(`${req.method()} ${path}`)
      return route.abort()
    }
    const art = path.match(/^\/api\/artifacts\/([^/]+)$/)
    if (art) {
      const id = decodeURIComponent(art[1])
      hits.artifacts.push(id)
      if (id === BROKEN && !state.broken) return route.fulfill({ json: { id, content: fx.artifacts[ART] } })
      if (id === DUP) return route.fulfill({ json: { id, content: DUP_CONTENT } })
      const content = fx.artifacts[id]
      return content ? route.fulfill({ json: { id, content } })
        : route.fulfill({ status: 404, json: { detail: '工件不存在，可能已被删除' } })
    }
    const run = path.match(/^\/api\/runs\/([^/]+)$/)
    if (run) {
      const id = decodeURIComponent(run[1])
      hits.runs.push(id)
      if (id === RUNS.runfail && state.runFail) return route.fulfill({ status: 500, json: { detail: '服务暂时不可用' } })
      return RUN_CLASS[id] ? route.fulfill({ json: { id, run_class: RUN_CLASS[id], status: 'interrupted' } })
        : route.fulfill({ status: 404, json: { detail: '运行不存在' } })
    }
    return route.continue()
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
/** 等运行类别答完、界面画上：否定的断言（不写降档）得在这之后做 */
async function runClassSettled(page, hits, runId) {
  for (let i = 0; i < 50 && !hits.runs.includes(runId); i++) await page.waitForTimeout(100)
  await page.waitForTimeout(400)
}
const item = (page, unit) => page.locator(`[data-challenge-item="${unit}"]`)
const toggle = (page, unit) => item(page, unit).locator('[data-challenge-toggle]')
const detail = (page, unit) => page.locator(`[data-challenge-detail="${unit}"]`)
const config = (page) => page.evaluate(() => window.__studio.getState().nodes.find((n) => n.id === 'critic')?.data.config ?? null)

await section('审批卡：概况与降档提示', async () => {
  const { ctx, page, hits, errors } = await open('challenge-approval')
  const block = page.locator('[data-challenge="write"]')
  await block.waitFor()
  check('审批卡里有这份报告的反方审稿', (await text(block.locator('div').first())).includes('报告「写月报」的反方审稿'))
  const counts = {}
  for (const v of Object.keys(LABEL)) counts[v] = await text(block.locator(`[data-challenge-count="${v}"]`))
  check('一行概况：四种判断各几句', counts.stands === '经受住 1 句' && counts.weakened === '被削弱 1 句'
    && counts.refuted === '被推翻 1 句' && counts.inconclusive === '无法判断 1 句', JSON.stringify(counts))
  await page.waitForSelector('[data-challenge-degrade]')
  check('正式运行、没写处理方式（按降档）、有被推翻的：醒目提示「出具会降档」',
    (await text(page.locator('[data-challenge-degrade]'))) === '出具会降档')
  // 原因不能只放在悬停提示和读屏文字里：触屏、键盘用户看不到（只标注那一档反而有常显的说明）
  const note = page.locator('[data-challenge-degrade-note]')
  check('降档的原因常显在概况下面，不靠悬停', await note.count() === 1 && await note.isVisible() && (await text(note)) === DEGRADE_NOTE
    && (await page.locator('[data-challenge-degrade]').getAttribute('title')) === null, await note.count() ? await text(note) : '没有')
  check('不是只标注：没有「只做标注」那句', await page.locator('[data-challenge-flag]').count() === 0)
  check('运行类别取到了：没有「暂时无法确认运行类别」', await page.locator('[data-challenge-runclass]').count() === 0)
  const rows = await block.locator('[data-challenge-item]').evaluateAll((els) => els.map((el) => ({
    unit: el.getAttribute('data-challenge-item'),
    tag: el.querySelector('[data-challenge-tag]')?.textContent?.trim(),
    text: el.querySelector('[data-challenge-text]')?.textContent?.trim(),
  })))
  const expected = fx.challenge.items.map((it) => ({ unit: it.unit, tag: LABEL[it.verdict], text: it.text }))
  check('逐句列出结论原文和判断标签，顺序照审批的条目', JSON.stringify(rows) === JSON.stringify(expected), JSON.stringify(rows))
  check('每句是一个收起的按钮', await block.locator('[data-challenge-toggle][aria-expanded="false"]').count() === 4)
  check('没点开之前也说清判断出自反方模型', (await text(block.locator('[data-challenge-judged]'))) === '判断由反方模型给出，并非系统核对')
  check('没点开之前不取工件', hits.artifacts.length === 0, hits.artifacts.join('、'))
  check('运行类别按运行 id 取了一次', hits.runs.filter((r) => r === RUNS.formal).length === 1, hits.runs.join('、'))
  await shot(page, 'approval-dark')
  check('页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('点开一句：取工件，显示理由与查询', async () => {
  const { ctx, page, hits, errors } = await open('challenge-approval')
  await toggle(page, 'u3').click()
  await detail(page, 'u3').locator('[data-challenge-doc]').waitFor()
  check('点开后标为展开、指向详情', (await toggle(page, 'u3').getAttribute('aria-expanded')) === 'true'
    && await page.locator(`#${CSS_ESC(await toggle(page, 'u3').getAttribute('aria-controls'))}[data-challenge-detail="u3"]`).count() === 1)
  check('按 challenge_artifact 取了一次工件', JSON.stringify(hits.artifacts) === JSON.stringify([ART]), hits.artifacts.join('、'))
  const u3 = detail(page, 'u3')
  const rationale = await text(u3.locator('[data-challenge-doc]'))
  check('显示理由正文（数字是查询结果里的值）', rationale.includes('9 月新客销售额 151,000') && rationale.includes('1,135,000'), rationale)
  const cites = await u3.locator('[data-challenge-cite]').evaluateAll((els) => els.map((el) => [el.getAttribute('data-challenge-cite'),
    el.querySelector('sup')?.textContent, el.getAttribute('aria-label'), el.getAttribute('data-ev-line')]))
  check('有出处的数字照证据的样子画实线，后面挂查询编号', cites.length === 4
    && cites.every(([alias, sup, , line]) => alias === sup && line === 'solid')
    && JSON.stringify(cites.map((c) => c[0])) === JSON.stringify(['Q1', 'Q2', 'Q2', 'Q1']), JSON.stringify(cites.map((c) => c.slice(0, 2))))
  check('读屏听得到引用的是哪次查询、哪一格', (cites[0]?.[2] ?? '').includes('出处：查询 Q1 · 第 1 行 · amount'), cites[0]?.[2])
  const queries = await u3.locator('[data-challenge-query]').evaluateAll((els) => els.map((el) => ({
    alias: el.getAttribute('data-challenge-query'), sql: el.querySelector('[data-challenge-sql]')?.textContent ?? '',
    rows: el.querySelector('[data-challenge-rows]')?.textContent ?? '', cut: !!el.querySelector('[data-challenge-truncated]'),
  })))
  check('列出反方执行的查询：SQL、行数', queries.length === 2 && queries[0].alias === 'Q1' && queries[1].alias === 'Q2'
    && queries.every((q) => q.sql.includes('GROUP BY customer_type') && q.rows === '2 行' && !q.cut), JSON.stringify(queries.map((q) => [q.alias, q.rows])))
  check('查询一节写明共几次', (await text(u3.locator('[data-challenge-queries] > div').first())) === '反方执行的查询（2 次）')
  check('写明判断出自反方模型、不是系统核对（取到工件后带上模型名，整块只说一次）',
    (await text(page.locator('[data-challenge="write"] [data-challenge-judged]'))) === '判断由反方模型「critic-large」给出，并非系统核对'
    && await page.locator('[data-challenge-judged]').count() === 1)

  // 点理由里的数字：焦点落到那次查询上，描一下边
  await u3.locator('[data-challenge-cite="Q2"]').first().click()
  await page.waitForTimeout(100)
  const jumped = await page.evaluate(() => {
    const head = document.activeElement
    const card = head?.closest('[data-challenge-query]')
    return { head: head?.hasAttribute('data-challenge-query-head'), alias: card?.getAttribute('data-challenge-query'), flash: card?.getAttribute('data-flash') }
  })
  check('点有出处的数字：跳到它引用的那次查询并描边', jumped.head && jumped.alias === 'Q2' && jumped.flash === 'focus', JSON.stringify(jumped))

  // 裸数字被标出来
  await toggle(page, 'u5').click()
  const u5 = detail(page, 'u5')
  await u5.locator('[data-challenge-doc]').waitFor()
  const bare = await u5.locator('.ev-mark[data-ev-state="none"]').allInnerTexts()
  check('理由里的裸数字画点状线，句末挂「无证据」', JSON.stringify(bare.map((t) => t.replace(/\s*（无证据）$/, ''))) === JSON.stringify(['三', '70000', '一半'])
    && (await u5.locator('[data-ev-tag="none"]').innerText()).includes('无证据'), JSON.stringify(bare))
  check('有出处的那个照样挂编号', await u5.locator('[data-challenge-cite="Q1"]').count() === 1)
  check('同一张卡点第二句不再取工件', hits.artifacts.filter((a) => a === ART).length === 1, hits.artifacts.join('、'))

  // 理由文档是另一个工件
  await toggle(page, 'u8').click()
  const u8 = detail(page, 'u8')
  await u8.locator('[data-challenge-doc]').waitFor()
  const u8Text = await text(u8.locator('[data-challenge-doc]'))
  check('理由文档是工件 id 时去取它，按文档显示', hits.artifacts.includes(DOC) && u8Text.includes('9 月退款金额 21,500')
    && await u8.locator('[data-challenge-cite]').count() === 2, `${hits.artifacts.length} 次取工件 · ${u8Text}`)

  // 没有理由文档：按渲染后的正文显示；查询截断照实说
  await toggle(page, 'u11').click()
  const u11 = detail(page, 'u11')
  await u11.locator('[data-challenge-rationale-text]').waitFor()
  check('没有理由文档时显示渲染后的正文', (await text(u11.locator('[data-challenge-rationale-text]'))) === '数据源中没有区分线上与门店的字段，无法判断这句结论。')
  check('查询结果截断时写明', (await text(u11.locator('[data-challenge-rows]'))) === '50 行'
    && (await text(u11.locator('[data-challenge-truncated]'))) === '结果已截断，数据库中还有更多数据')
  await shot(page, 'approval-open-dark')

  // 收起；键盘开合
  await toggle(page, 'u3').click()
  check('再点一下收起', await detail(page, 'u3').count() === 0 && (await toggle(page, 'u3').getAttribute('aria-expanded')) === 'false')
  await toggle(page, 'u3').focus()
  await page.keyboard.press('Enter')
  await detail(page, 'u3').locator('[data-challenge-doc]').waitFor()
  check('键盘：回车展开', (await toggle(page, 'u3').getAttribute('aria-expanded')) === 'true')
  await page.keyboard.press('Space')
  await page.waitForTimeout(100)
  check('键盘：空格收起', await detail(page, 'u3').count() === 0)
  check('没有发出任何写请求', hits.writes.length === 0, hits.writes.join('、'))
  check('页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

// 下面几段各开各的页面、各自成段：一段中途出错（比如等一个不该出现的元素超时）不遮住其余几段
await section('只标注：不写降档', async () => {
  const { ctx, page, hits, errors } = await open('challenge-approval-flag')
  await page.locator('[data-challenge="write"]').waitFor()
  await runClassSettled(page, hits, RUNS.flag)
  const all = await text(page.locator('[data-challenge="write"]'))
  check('只标注：不写「出具会降档」', await page.locator('[data-challenge-degrade], [data-challenge-degrade-note]').count() === 0
    && !all.includes('降档'), all)
  check('只标注：写明被推翻的结论只做标注', (await text(page.locator('[data-challenge-flag]'))) === FLAG_NOTE)
  check('只标注：页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('探索运行：不说出具', async () => {
  const { ctx, page, hits, errors } = await open('challenge-approval-explore')
  await page.locator('[data-challenge="write"]').waitFor()
  await runClassSettled(page, hits, RUNS.explore)
  const all = await text(page.locator('[data-challenge="write"]'))
  check('探索运行：反方审稿里不说任何和出具有关的话', !all.includes('出具')
    && await page.locator('[data-challenge-degrade], [data-challenge-degrade-note], [data-challenge-flag]').count() === 0, all)
  check('探索运行：概况和条目照常显示', (await text(page.locator('[data-challenge-count="refuted"]'))) === '被推翻 1 句'
    && await page.locator('[data-challenge-item]').count() === 4)
  check('探索运行：页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('探索运行配只标注：同样不说出具', async () => {
  const { ctx, page, hits, errors } = await open('challenge-approval-explore-flag')
  await page.locator('[data-challenge="write"]').waitFor()
  await runClassSettled(page, hits, RUNS.exploreFlag)
  const all = await text(page.locator('[data-challenge="write"]'))
  check('探索运行、只标注：不写「只做标注、不影响出具档位」', await page.locator('[data-challenge-flag]').count() === 0 && !all.includes('出具'), all)
  check('探索运行、只标注：也不写降档', await page.locator('[data-challenge-degrade], [data-challenge-degrade-note]').count() === 0)
  check('探索运行、只标注：页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('正式运行、没有被推翻的结论：不写降档', async () => {
  for (const [syn, run, what] of [['challenge-approval-clear', RUNS.clear, '按降档'], ['challenge-approval-clear-flag', RUNS.clearFlag, '只标注']]) {
    const { ctx, page, hits, errors } = await open(syn)
    await page.locator('[data-challenge="write"]').waitFor()
    await runClassSettled(page, hits, run)
    const all = await text(page.locator('[data-challenge="write"]'))
    check(`正式运行、${what}、被推翻 0 句：概况照实写 0 句`, (await text(page.locator('[data-challenge-count="refuted"]'))) === '被推翻 0 句'
      && await page.locator('[data-challenge-item]').count() === 3, all)
    check(`正式运行、${what}、被推翻 0 句：既不写降档，也不写只做标注`,
      await page.locator('[data-challenge-degrade], [data-challenge-degrade-note], [data-challenge-flag]').count() === 0
      && !all.includes('降档') && !all.includes('只做标注'), all)
    check(`正式运行、${what}、被推翻 0 句：页面没有报错`, errors.length === 0, errors.join('；'))
    await ctx.close()
  }
})

await section('运行类别取不到：说明并可重试', async () => {
  const { ctx, page, hits, state, errors } = await open('challenge-approval-runfail')
  await page.locator('[data-challenge="write"]').waitFor()
  await runClassSettled(page, hits, RUNS.runfail)
  const failedLine = page.locator('[data-challenge-runclass="failed"]')
  check('取不到：不写降档，也不装作没事——写明暂时无法确认运行类别', await page.locator('[data-challenge-degrade]').count() === 0
    && await failedLine.count() === 1 && (await text(failedLine)).startsWith('暂时无法确认运行类别'),
    await failedLine.count() ? await text(failedLine) : '没有说明')
  // 窗口重新拿到焦点时再取一次（store 对出错的槽会重发）
  await page.evaluate(() => window.dispatchEvent(new Event('focus')))
  for (let i = 0; i < 30 && hits.runs.filter((r) => r === RUNS.runfail).length < 2; i++) await page.waitForTimeout(100)
  check('窗口重新拿到焦点：再取一次运行类别', hits.runs.filter((r) => r === RUNS.runfail).length === 2, hits.runs.join('、'))
  state.runFail = false
  await failedLine.locator('button', { hasText: '重试' }).focus()
  await page.keyboard.press('Enter')
  await page.waitForSelector('[data-challenge-degrade]')
  check('按重试：取到正式运行后写「出具会降档」，说明随之消失', await failedLine.count() === 0
    && hits.runs.filter((r) => r === RUNS.runfail).length === 3, hits.runs.join('、'))
  await page.waitForTimeout(100)
  check('按重试：那一行拿掉后焦点接到概况上，不掉到页面上', await page.evaluate(() =>
    document.activeElement?.hasAttribute('data-challenge-counts') ?? false), await page.evaluate(() => document.activeElement?.tagName))
  check('取不到：页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('没有反方审稿：什么都不画', async () => {
  const { ctx, page, hits, errors } = await open('challenge-approval-none')
  await page.locator('[data-approval-report="write"]').waitFor()
  await page.waitForTimeout(500)
  check('报告没有经过反方审稿：什么都不画', await page.locator('[data-challenge]').count() === 0)
  check('报告没有经过反方审稿：不问运行类别、不取工件', !hits.runs.includes(RUNS.none) && hits.artifacts.length === 0,
    `${hits.runs.join('、')} / ${hits.artifacts.join('、')}`)
  check('报告核对概况照常显示', (await text(page.locator('[data-approval-report="write"]'))).includes('报告「写月报」的核对'))
  check('没有反方审稿：页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('工件取不到：说明并可重试', async () => {
  const { ctx, page, state, hits, errors } = await open('challenge-approval-broken')
  await toggle(page, 'u3').click()
  await page.waitForSelector('[data-challenge-detail="u3"] [data-challenge-detail-state="error"]')
  check('取不到：写明无法获取，给重试', (await text(detail(page, 'u3'))).includes(FAILED)
    && await detail(page, 'u3').locator('button', { hasText: '重试' }).count() === 1)
  check('取不到：不停在「正在加载」', await page.locator('[data-challenge-detail-state="loading"]').count() === 0)
  state.broken = false
  // 用键盘按重试：按钮在加载时就拿掉了，取到之后焦点得接到理由上
  await detail(page, 'u3').locator('button', { hasText: '重试' }).focus()
  await page.keyboard.press('Enter')
  await detail(page, 'u3').locator('[data-challenge-doc]').waitFor()
  check('重试：再取一次，取到后显示理由', hits.artifacts.filter((a) => a === BROKEN).length === 2
    && (await text(detail(page, 'u3').locator('[data-challenge-doc]'))).includes('151,000'))
  await page.waitForTimeout(100)
  check('键盘按重试：取到后焦点落到这一句的理由上，不掉到页面上', await page.evaluate(() =>
    document.activeElement?.matches('[data-challenge-detail="u3"] [data-challenge-rationale]') ?? false),
    await page.evaluate(() => document.activeElement?.tagName))
  check('取不到：页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('超过十句：显示全部', async () => {
  const { ctx, page, hits, errors } = await open('challenge-approval-many')
  await page.locator('[data-challenge="write"]').waitFor()
  check('条目只列前十句', await page.locator('[data-challenge-item]').count() === 10)
  check('计数是全部十二句', (await text(page.locator('[data-challenge-count="stands"]'))) === '经受住 7 句'
    && (await text(page.locator('[data-challenge-count="refuted"]'))) === '被推翻 2 句')
  check('写明另有几句未列出', (await text(page.locator('[data-challenge-more]'))).startsWith('另有 2 句未列出'))
  check('没要之前不取工件', hits.artifacts.length === 0)
  // 用键盘按：按钮所在那一行补齐后会拿掉，焦点得落到补出来的第一句上，不能掉到页面上、下一个 Tab 跳过新补的几句
  await page.locator('[data-challenge-show-all]').focus()
  await page.keyboard.press('Enter')
  await page.waitForFunction(() => document.querySelectorAll('[data-challenge-item]').length === 12)
  check('显示全部：从工件里补齐其余两句，排在后面', JSON.stringify(await page.locator('[data-challenge-item]').evaluateAll((els) =>
    els.slice(10).map((el) => el.getAttribute('data-challenge-item')))) === JSON.stringify(['u30', 'u31']) && hits.artifacts.includes(MANY))
  await page.waitForTimeout(100)
  const focused = await page.evaluate(() => ({
    toggle: document.activeElement?.hasAttribute('data-challenge-toggle') ?? false,
    unit: document.activeElement?.closest('[data-challenge-item]')?.getAttribute('data-challenge-item') ?? document.activeElement?.tagName,
  }))
  check('键盘按「显示全部」：焦点落到补出来的第一句上', focused.toggle && focused.unit === 'u30', JSON.stringify(focused))
  check('显示全部之后不再写「另有」', await page.locator('[data-challenge-more]').count() === 0)
  check('补出来的一句带着自己的判断标签', (await text(item(page, 'u30').locator('[data-challenge-tag]'))) === '被推翻')
  await toggle(page, 'u30').click()
  await detail(page, 'u30').locator('[data-challenge-rationale-text]').waitFor()
  const u30 = await text(detail(page, 'u30'))
  check('补出来的那几句也能点开', u30.includes('白汀区的退款金额高于云屿区这一说法与查询结果不符'), u30)
  check('超过十句：页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('显示全部：原文相同、unit 不同的一句不被吞掉', async () => {
  // 同一句结论在摘要和正文各出现一次、各自被选中审稿：补的时候按 unit 认，不按原文
  const { ctx, page, hits, errors } = await open('challenge-approval-dup')
  await page.locator('[data-challenge="write"]').waitFor()
  await page.locator('[data-challenge-show-all]').click()
  await item(page, 'u31').waitFor()
  const units = await page.locator('[data-challenge-item]').evaluateAll((els) => els.map((el) => el.getAttribute('data-challenge-item')))
  check('补齐十二句，原文和 u20 相同的 u30 也在', units.length === 12 && units.includes('u30') && hits.artifacts.includes(DUP), JSON.stringify(units))
  check('补出来的 u30 带着自己的判断（被推翻）', await item(page, 'u30').count() === 1
    && (await text(item(page, 'u30').locator('[data-challenge-tag]'))) === '被推翻')
  await toggle(page, 'u20').click()
  await detail(page, 'u20').locator('[data-challenge-rationale-text]').waitFor()
  check('点开原文相同的 u20：显示的是 u20 自己的理由', !(await text(detail(page, 'u20'))).includes('白汀区的退款金额高于云屿区这一说法与查询结果不符'),
    await text(detail(page, 'u20')))
  check('原文相同：页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('审稿记录没存上：不能展开，说明原因', async () => {
  const { ctx, page, hits, errors } = await open('challenge-approval-norecord')
  const block = page.locator('[data-challenge="write"]')
  await block.waitFor()
  await page.waitForTimeout(300)
  check('概况、判断照常列出', (await text(block.locator('[data-challenge-count="stands"]'))) === '经受住 7 句'
    && await block.locator('[data-challenge-item]').count() === 10)
  check('每句不再是能点开的按钮（点开也看不到东西）', await block.locator('[data-challenge-toggle]').count() === 0
    && await block.locator('[data-challenge-item] [data-challenge-tag]').count() === 10)
  const norecord = block.locator('[data-challenge-norecord]')
  check('整块说一句审稿记录没存上、看不了理由和查询', await norecord.count() === 1
    && (await text(norecord)) === '反方审稿记录未保存，无法查看理由和查询', await norecord.count() ? await text(norecord) : '没有')
  check('「另有 N 句未列出」照样交代，只是没有「显示全部」', (await text(block.locator('[data-challenge-more]'))) === '另有 2 句未列出'
    && await block.locator('[data-challenge-show-all]').count() === 0)
  check('没有工件 id：不发取工件的请求', hits.artifacts.length === 0, hits.artifacts.join('、'))
  check('审稿记录没存上：页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('检查器：字段写进节点配置', async () => {
  const { ctx, page, hits, errors } = await open('challenge-inspector', { width: 1280, height: 1000 })
  await page.locator('[data-field="report"]').waitFor()
  const panel = page.locator('[data-challenge-inspector]')
  check('节点说明不再写功能开发中', !(await text(panel)).includes('功能开发中'))
  const card = page.locator('.react-flow__node[data-id="critic"]')
  await card.waitFor()
  check('节点卡片摘要：没配时写审上游报告、句数跟随设置', (await text(card)).includes('审上游报告 · 句数跟随设置'), await text(card))

  // 要审的报告：只列上游的报告撰写节点
  const report = page.locator('[data-field="report"] select')
  const options = await report.locator('option').evaluateAll((els) => els.map((o) => [o.value, o.textContent]))
  check('要审的报告：只列上游的报告撰写节点，下游的不列', JSON.stringify(options) === JSON.stringify([
    ['', '未选择：审上游的报告撰写节点'], ['write', '写月报（write）'], ['write2', '写周报（write2）']]), JSON.stringify(options))
  check('上游有两个、又没选：提醒选一个', (await text(page.locator('[data-node-pick-hint="many"]'))) === '上游有 2 个报告撰写节点，请选择要审的那一个')
  await report.selectOption('write')
  check('选了报告：写进 report', (await config(page))?.report === 'write')
  check('选了之后不再提醒', await page.locator('[data-node-pick-hint]').count() === 0)

  await page.locator('[data-field="max_claims"] input').fill('3')
  check('最多审几句：写进 max_claims（数）', (await config(page))?.max_claims === 3)
  await page.waitForTimeout(150)
  check('节点卡片摘要写清审哪份报告、最多几句', (await text(card)).includes('审报告节点 write · 最多审 3 句结论'), await text(card))

  // 反方模型
  const provider = page.locator('[data-field="provider"] select')
  const providers = await provider.locator('option').evaluateAll((els) => els.map((o) => o.textContent))
  check('反方模型 · 接入：跟随设置在第一项，停用的接入标出来', JSON.stringify(providers) === JSON.stringify(['跟随设置', '主力接入', '备用接入（已停用）']),
    JSON.stringify(providers))
  await provider.selectOption('主力接入')
  const model = page.locator('[data-field="model"] input')
  await model.fill('writer-large')
  let c = await config(page)
  check('接入、模型写进 provider、model', c?.provider === '主力接入' && c?.model === 'writer-large', JSON.stringify(c))
  check('和撰写报告的模型相同：提醒', (await text(page.locator('[data-verify-same-model]'))).includes('「writer-large」'))
  await model.fill('critic-large')
  check('换一个模型：不再提醒', await page.locator('[data-verify-same-model]').count() === 0)
  check('模型框给出所选接入的模型', JSON.stringify(await page.locator('[data-field="model"] datalist option').evaluateAll((els) => els.map((o) => o.value)))
    === JSON.stringify(['writer-large', 'critic-large']))
  await provider.selectOption('')
  c = await config(page)
  check('接入改回留空：配置里拿掉 provider', !('provider' in (c ?? {})) || c.provider === undefined, JSON.stringify(c))
  check('只填了模型：接入一项写「按模型名找接入」，并说明会用哪个接入',
    (await provider.locator('option').first().textContent()) === '按模型名找接入'
    && (await text(page.locator('[data-verify-by-model="found"]'))).includes('「主力接入」'))

  // 被推翻时怎么办
  const onRefuted = page.locator('[data-field="on_refuted"] select')
  check('结论被推翻时：没写时落在「出具降档（默认）」', (await onRefuted.evaluate((s) => s.options[s.selectedIndex]?.textContent)) === '出具降档（默认）')
  await onRefuted.selectOption('flag')
  check('选只标注：写进 on_refuted', (await config(page))?.on_refuted === 'flag')
  await onRefuted.selectOption('fail')
  check('选报错：写进 on_refuted', (await config(page))?.on_refuted === 'fail')

  // 启用：三态
  const group = page.locator('[data-field="enabled"] [role="radiogroup"]')
  check('启用：一组单选，组名是「启用」，默认跟随设置', (await group.getAttribute('aria-label')) === '启用'
    && (await group.getAttribute('data-tri-state')) === 'follow' && !('enabled' in (await config(page))))
  // 指到真实存在的入口：设置页的标签是 模型接入 / 偏好设置 / 运行环境，「核查」一节在偏好设置里
  check('跟随设置时说明跟的是哪一项（入口写全：设置 → 偏好设置 → 核查）',
    (await text(page.locator('[data-tri-hint]'))) === '使用「设置 → 偏好设置 → 核查」中「反方审稿 · 启用」的值')
  check('检查器里提到设置的地方都写全入口', !(await text(panel)).includes('「设置 → 核查」'))
  await group.locator('[data-tri="off"]').click()
  check('点关闭：写进 enabled: false', (await config(page))?.enabled === false && (await group.getAttribute('data-tri-state')) === 'off')
  await page.keyboard.press('ArrowLeft')
  check('键盘：方向键在组内移动并选中（关闭 → 开启）', (await config(page))?.enabled === true
    && await page.evaluate(() => document.activeElement?.getAttribute('data-tri')) === 'on')
  await group.locator('[data-tri="follow"]').click()
  check('点跟随设置：配置里拿掉 enabled，不写 null 也不写空字符串', !('enabled' in (await config(page))) || (await config(page)).enabled === undefined,
    JSON.stringify(await config(page)))
  await group.locator('[data-tri="off"]').click()
  await page.waitForTimeout(150)
  check('关掉的节点：卡片摘要写已关闭', (await text(card)).includes('已关闭 · 审报告节点 write'), await text(card))

  // 高级选项里的两项上限
  await panel.locator('button', { hasText: '高级选项' }).click()
  await page.locator('[data-field="max_queries"] input').fill('2')
  await page.locator('[data-field="timeout_s"] input').fill('60')
  c = await config(page)
  check('每句最多查询次数、每句时长上限写进 max_queries、timeout_s', c?.max_queries === 2 && c?.timeout_s === 60, JSON.stringify(c))
  await page.locator('[data-field="max_queries"] input').fill('')
  check('清空上限：配置里拿掉这一项（跟随设置）', (await config(page))?.max_queries === undefined)

  // 选中的报告挪到下游、被删掉：照实列出来，不像「未选择」
  await page.evaluate(() => {
    const s = window.__studio.getState()
    const n = s.nodes.find((x) => x.id === 'critic')
    s.updateNode('critic', { config: { ...n.data.config, report: 'later' } })
  })
  await page.waitForTimeout(100)
  check('选中的报告在下游：下拉里照实标「不在上游」，并提醒', (await report.evaluate((s) => s.options[s.selectedIndex]?.textContent)) === '写摘要（later）（不在上游）'
    && await page.locator('[data-node-pick-hint="notUpstream"]').count() === 1)
  await page.evaluate(() => {
    const s = window.__studio.getState()
    const n = s.nodes.find((x) => x.id === 'critic')
    s.updateNode('critic', { config: { ...n.data.config, report: 'ghost' } })
  })
  await page.waitForTimeout(100)
  check('选中的报告已不在画布上：照实标出，并提醒', (await report.evaluate((s) => s.options[s.selectedIndex]?.textContent)) === 'ghost（已不在画布上）'
    && await page.locator('[data-node-pick-hint="missing"]').count() === 1)
  // 选中的是上游的输入节点（助手改图、导入 JSON 会写出这种配置）：它在上游，错在类型，不能说成「不在上游」
  await page.evaluate(() => {
    const s = window.__studio.getState()
    const n = s.nodes.find((x) => x.id === 'critic')
    s.updateNode('critic', { config: { ...n.data.config, report: 'input' } })
  })
  await page.waitForTimeout(100)
  const notReport = page.locator('[data-node-pick-hint="wrongType"]')
  check('选中的是上游的非报告节点：下拉照实标「不是报告撰写节点」，提示说清它是哪类节点',
    (await report.evaluate((s) => s.options[s.selectedIndex]?.textContent)) === '输入（input）（不是报告撰写节点）'
    && await notReport.count() === 1 && (await text(notReport)) === '所选节点是「输入」节点，不是报告撰写节点，请重新选择'
    && await page.locator('[data-node-pick-hint="notUpstream"]').count() === 0,
    `${await report.evaluate((s) => s.options[s.selectedIndex]?.textContent)} · ${await notReport.count() ? await text(notReport) : '没有提示'}`)
  await shot(page, 'inspector-dark')
  check('检查器：没有发出任何写请求', hits.writes.length === 0, hits.writes.join('、'))
  check('检查器：页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('检查器：链式接法按服务端的认法定要审的报告', async () => {
  // schema.challenge_report：留空时先看直接连着的报告撰写节点，恰好一个就审它（「初稿 → 终稿 → 反方审稿」审终稿），
  // 否则才看整个上游是否只有一个
  const { ctx, page, errors } = await open('challenge-inspector-chain', { width: 1280, height: 1000 })
  await page.locator('[data-field="report"]').waitFor()
  const report = page.locator('[data-field="report"] select')
  const options = await report.locator('option').evaluateAll((els) => els.map((o) => [o.value, o.textContent]))
  check('留空那一项写出实际会审的那份（终稿），两份上游报告都可选', JSON.stringify(options) === JSON.stringify([
    ['', '未选择：审上游的「终稿」'], ['draft', '初稿（draft）'], ['final', '终稿（final）']]), JSON.stringify(options))
  check('服务端照常审终稿、不报错：检查器也不要求选择', await page.locator('[data-node-pick-hint]').count() === 0,
    await page.locator('[data-node-pick-hint]').count() ? await text(page.locator('[data-node-pick-hint]')) : '')
  check('反方模型和终稿的模型相同：提醒', await page.locator('[data-verify-same-model]').count() === 1
    && (await text(page.locator('[data-verify-same-model]'))).includes('「writer-large」'))
  // 改成审初稿（模型不同）：同模型提醒随之消失
  await report.selectOption('draft')
  check('改审初稿：配置写进 report，同模型提醒消失', (await config(page))?.report === 'draft'
    && await page.locator('[data-verify-same-model]').count() === 0)
  await shot(page, 'inspector-chain-dark')
  check('链式接法：页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('检查器：画布校验报了就不重复说', async () => {
  // 服务端（F3a 的 _check_challenge）在同样情形下会发 report / model 字段上的问题，检查器把它们挂在字段下面；
  // 本地那句提示再画一遍就是同一件事说两遍，还一句琥珀一句红（同结论句裁判「校验也会报，报了就不重复说」）
  const { ctx, page, state, errors } = await open('challenge-inspector', { width: 1280, height: 1000 })
  await page.locator('[data-field="report"]').waitFor()
  const reportField = page.locator('[data-field="report"]')
  const settle = async (issues, wait) => {
    state.issues = issues
    await page.evaluate(() => window.__studio.getState().analyzeNow())
    if (wait) await page.getByText(wait, { exact: false }).first().waitFor()
    await page.waitForTimeout(150)
  }
  check('校验没报时：本地提醒照常（上游有两个）', await page.locator('[data-node-pick-hint="many"]').count() === 1)
  await settle([{ level: 'error', node_id: 'critic', field: 'report', code: 'challenge.report_invalid', message: SERVER_MANY }], SERVER_MANY)
  check('校验报了「上游有多个」：字段下只剩服务端那一句', await page.locator('[data-node-pick-hint]').count() === 0
    && (await text(reportField)).includes(SERVER_MANY), await text(reportField))
  await page.evaluate(() => {
    const s = window.__studio.getState()
    const n = s.nodes.find((x) => x.id === 'critic')
    s.updateNode('critic', { config: { ...n.data.config, report: 'ghost' } })
  })
  await settle([{ level: 'error', node_id: 'critic', field: 'report', code: 'challenge.report_invalid', message: SERVER_GHOST }], SERVER_GHOST)
  check('校验报了「不存在这个节点」：不再画本地的「已不在画布上」提示，下拉里照实标出的那一项保留',
    await page.locator('[data-node-pick-hint]').count() === 0
    && (await reportField.locator('select').evaluate((s) => s.options[s.selectedIndex]?.textContent)) === 'ghost（已不在画布上）',
    await text(reportField))
  await reportField.locator('select').selectOption('write')
  await page.locator('[data-field="model"] input').fill('writer-large')
  await settle([], null)
  check('校验没报同模型时：本地提醒照常', await page.locator('[data-verify-same-model]').count() === 1)
  await settle([{ level: 'warning', node_id: 'critic', field: 'model', code: 'challenge.same_model', message: SERVER_SAME }], SERVER_SAME)
  const modelField = page.locator('[data-field="model"]')
  check('校验报了「模型相同」：字段下只剩服务端那一句', await page.locator('[data-verify-same-model]').count() === 0
    && (await text(modelField)).includes(SERVER_SAME), await text(modelField))
  await shot(page, 'inspector-dedupe-dark')
  check('校验报重复：页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

/** 页面和某个容器都没有横向滚动（SQL 框自己的滚动条不算） */
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

await section('窄屏与深浅色', async () => {
  for (const theme of ['dark', 'light']) {
    const { ctx, page, errors } = await open('challenge-approval', { width: 390, height: 844, theme })
    await page.waitForSelector('[data-challenge-degrade]')
    for (const unit of ['u3', 'u5', 'u11']) {
      await toggle(page, unit).click()
      await detail(page, unit).locator('[data-challenge-queries]').waitFor()
    }
    const o = await overflow(page, '[data-challenge="write"]')
    check(`390px（${theme}）：展开三句也没有横向溢出`, o.page <= 0 && o.box <= 1, JSON.stringify(o))
    const colors = await page.evaluate(() => {
      const block = document.querySelector('[data-challenge="write"]')
      const base = getComputedStyle(document.body).backgroundColor
      const bg = getComputedStyle(block).backgroundColor
      const pick = (el) => ({ fg: getComputedStyle(el).color, weight: Number(getComputedStyle(el).fontWeight) })
      return {
        base, bg,
        degrade: pick(document.querySelector('[data-challenge-degrade]')),
        tags: [...document.querySelectorAll('[data-challenge-tag]')].slice(0, 4).map((el) => ({ v: el.getAttribute('data-challenge-tag'), ...pick(el) })),
        counts: [...document.querySelectorAll('[data-challenge-count]')].map((el) => ({ v: el.getAttribute('data-challenge-count'), ...pick(el) })),
        sup: pick(document.querySelector('[data-challenge-cite] sup')),
      }
    })
    const ratio = (c) => contrast(c.fg, colors.bg, colors.base)
    check(`${theme}：「出具会降档」对比度 ≥ 4.5、加粗`, ratio(colors.degrade) >= 4.5 && colors.degrade.weight >= 600,
      `${ratio(colors.degrade).toFixed(2)} · ${colors.degrade.weight}`)
    const tagRatios = colors.tags.map((t) => [t.v, ratio(t).toFixed(2)])
    check(`${theme}：四种判断标签对比度都 ≥ 4.5`, colors.tags.length === 4 && colors.tags.every((t) => ratio(t) >= 4.5), JSON.stringify(tagRatios))
    check(`${theme}：概况里的句数对比度都 ≥ 4.5`, colors.counts.every((t) => ratio(t) >= 4.5),
      JSON.stringify(colors.counts.map((t) => [t.v, ratio(t).toFixed(2)])))
    check(`${theme}：数字后面的查询编号对比度 ≥ 4.5`, ratio(colors.sup) >= 4.5, ratio(colors.sup).toFixed(2))
    await shot(page, `narrow-${theme}`)
    check(`390px（${theme}）：页面没有报错`, errors.length === 0, errors.join('；'))
    await ctx.close()
  }
  // 这次新加的几行说明（降档原因、运行类别取不到、审稿记录没存上）：窄屏不撑宽，深浅色都看得清
  for (const theme of ['dark', 'light']) {
    const notes = [
      ['challenge-approval', '[data-challenge-degrade-note]'],
      ['challenge-approval-runfail', '[data-challenge-runclass="failed"] span'],
      ['challenge-approval-norecord', '[data-challenge-norecord]'],
    ]
    for (const [syn, sel] of notes) {
      const { ctx, page, errors } = await open(syn, { width: 390, height: 844, theme })
      await page.locator(sel).waitFor()
      const o = await overflow(page, '[data-challenge="write"]')
      const c = await page.evaluate((s) => ({
        fg: getComputedStyle(document.querySelector(s)).color,
        bg: getComputedStyle(document.querySelector('[data-challenge="write"]')).backgroundColor,
        base: getComputedStyle(document.body).backgroundColor,
      }), sel)
      const r = contrast(c.fg, c.bg, c.base)
      check(`390px（${theme}）${syn}：说明不横向溢出、对比度 ≥ 4.5`, o.page <= 0 && o.box <= 1 && r >= 4.5, `${JSON.stringify(o)} · ${r.toFixed(2)}`)
      await shot(page, `${syn.replace('challenge-', '')}-narrow-${theme}`)
      check(`390px（${theme}）${syn}：页面没有报错`, errors.length === 0, errors.join('；'))
      await ctx.close()
    }
  }
  {
    const { ctx, page } = await open('challenge-approval-dense', { width: 1100, height: 900 })
    await page.locator('[data-challenge="write"]').waitFor()
    await toggle(page, 'u3').click()
    await detail(page, 'u3').locator('[data-challenge-queries]').waitFor()
    const o = await overflow(page, '[data-fixture="challenge-approval-dense"]')
    check('360px 右栏：展开后不撑宽右栏', o.page <= 0 && o.box <= 0, JSON.stringify(o))
    await shot(page, 'dense-dark')
    await ctx.close()
  }
  for (const theme of ['dark', 'light']) {
    const { ctx, page, errors } = await open('challenge-inspector', { width: 390, height: 844, theme })
    await page.locator('[data-field="report"]').waitFor()
    await page.locator('[data-field="model"] input').fill('writer-large')
    await page.waitForSelector('[data-verify-by-model]')
    const o = await overflow(page, '[data-challenge-inspector]')
    check(`390px（${theme}）：检查器不横向溢出`, o.page <= 0 && o.box <= 0, JSON.stringify(o))
    await shot(page, `inspector-narrow-${theme}`)
    check(`390px 检查器（${theme}）：页面没有报错`, errors.length === 0, errors.join('；'))
    await ctx.close()
  }
})

function CSS_ESC(id) {
  return String(id ?? '').replace(/([^a-zA-Z0-9_-])/g, '\\$1')
}

await browser.close()
console.log(failed ? `\n✗ 反方审稿：${failed} 项未通过` : '\n✓ 反方审稿：全部通过')
process.exit(failed ? 1 : 0)
