// 反方审稿的回归检查：审批卡上的反方审稿（run/ChallengeSummary.tsx）和检查器里反方审稿节点的配置（canvas/nodeDefs.ts 的
// challenge、canvas/verifyFields.tsx）。
//
// 守的是这些「看着没坏、其实用不了」的地方：概况的句数和条目对不上、有被推翻的结论却不提醒降档、只标注时也写降档、
// 探索运行里说出具、没经过反方审稿的报告也冒出一块空框（或者白发一次请求）、点开一句不取工件（或者每点一句取一次）、
// 理由里的数字看不出引用了哪次查询、裸数字没被标出来、理由文档是另一个工件时不去取、查询的 SQL / 行数 / 截断不显示、
// 工件取不到时卡死在加载中、条目只列前十句时看不到其余的；检查器里的字段写不进节点配置（或者写进去的不是约定的形状：
// 跟随设置要把这一项拿掉，不能写成空字符串）、下拉里列出了下游的报告、选中的报告被删了还看着像「未选择」、反方模型和
// 撰写报告的模型相同不提醒、节点卡片的摘要还写着功能开发中；窄屏（390px）被撑出横向滚动、浅色主题下标签看不清。
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
const RUNS = fx.runs
const RUN_CLASS = {
  [RUNS.formal]: 'formal', [RUNS.flag]: 'formal', [RUNS.explore]: 'exploratory', [RUNS.none]: 'formal',
  [RUNS.many]: 'formal', [RUNS.broken]: 'formal',
}
const LABEL = { stands: '经受住', weakened: '被削弱', refuted: '被推翻', inconclusive: '无法判断' }
const FLAG_NOTE = '按节点设置，被推翻的结论只做标注，不影响出具档位'
const FAILED = '无法获取反方审稿的详细内容'

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
 * 开一个预览用例。工件按夹具答（BROKEN 先答 404，state.broken 置 false 后答正常内容），运行类别按 RUN_CLASS 答；
 * 记下取了哪些工件、问了哪些运行、拦下了哪些写请求
 */
async function open(syn, { width = 1100, height = 900, theme = 'dark' } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height }, colorScheme: theme, timezoneId: 'Asia/Shanghai' })
  ctx.setDefaultTimeout(6000)
  const page = await ctx.newPage()
  const hits = { artifacts: [], runs: [], writes: [] }
  const state = { broken: true }
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('dialog', (d) => { errors.push(`原生对话框：${d.message()}`); void d.dismiss() })
  await page.route((u) => new URL(u).pathname.startsWith('/api/'), async (route) => {
    const req = route.request()
    const path = new URL(req.url()).pathname
    if (req.method() === 'POST' && path === '/api/workflows/validate') return route.fulfill({ json: { ok: true, issues: [] } })
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
      const content = fx.artifacts[id]
      return content ? route.fulfill({ json: { id, content } })
        : route.fulfill({ status: 404, json: { detail: '工件不存在，可能已被删除' } })
    }
    const run = path.match(/^\/api\/runs\/([^/]+)$/)
    if (run) {
      const id = decodeURIComponent(run[1])
      hits.runs.push(id)
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
    (await text(page.locator('[data-challenge-degrade]'))).startsWith('出具会降档'))
  check('不是只标注：没有「只做标注」那句', await page.locator('[data-challenge-flag]').count() === 0)
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

await section('只标注、探索运行、没有反方审稿', async () => {
  {
    const { ctx, page, hits, errors } = await open('challenge-approval-flag')
    await page.locator('[data-challenge="write"]').waitFor()
    await runClassSettled(page, hits, RUNS.flag)
    const all = await text(page.locator('[data-challenge="write"]'))
    check('只标注：不写「出具会降档」', await page.locator('[data-challenge-degrade]').count() === 0 && !all.includes('降档'), all)
    check('只标注：写明被推翻的结论只做标注', (await text(page.locator('[data-challenge-flag]'))) === FLAG_NOTE)
    check('只标注：页面没有报错', errors.length === 0, errors.join('；'))
    await ctx.close()
  }
  {
    const { ctx, page, hits, errors } = await open('challenge-approval-explore')
    await page.locator('[data-challenge="write"]').waitFor()
    await runClassSettled(page, hits, RUNS.explore)
    const all = await text(page.locator('[data-challenge="write"]'))
    check('探索运行：反方审稿里不说任何和出具有关的话', !all.includes('出具') && await page.locator('[data-challenge-degrade], [data-challenge-flag]').count() === 0, all)
    check('探索运行：概况和条目照常显示', (await text(page.locator('[data-challenge-count="refuted"]'))) === '被推翻 1 句'
      && await page.locator('[data-challenge-item]').count() === 4)
    check('探索运行：页面没有报错', errors.length === 0, errors.join('；'))
    await ctx.close()
  }
  {
    const { ctx, page, hits, errors } = await open('challenge-approval-none')
    await page.locator('[data-approval-report="write"]').waitFor()
    await page.waitForTimeout(500)
    check('报告没有经过反方审稿：什么都不画', await page.locator('[data-challenge]').count() === 0)
    check('报告没有经过反方审稿：不问运行类别、不取工件', !hits.runs.includes(RUNS.none) && hits.artifacts.length === 0,
      `${hits.runs.join('、')} / ${hits.artifacts.join('、')}`)
    check('报告核对概况照常显示', (await text(page.locator('[data-approval-report="write"]'))).includes('报告「写月报」的核对'))
    check('没有反方审稿：页面没有报错', errors.length === 0, errors.join('；'))
    await ctx.close()
  }
})

await section('工件取不到：说明并可重试', async () => {
  const { ctx, page, state, hits, errors } = await open('challenge-approval-broken')
  await toggle(page, 'u3').click()
  await page.waitForSelector('[data-challenge-detail="u3"] [data-challenge-detail-state="error"]')
  check('取不到：写明无法获取，给重试', (await text(detail(page, 'u3'))).includes(FAILED)
    && await detail(page, 'u3').locator('button', { hasText: '重试' }).count() === 1)
  check('取不到：不停在「正在加载」', await page.locator('[data-challenge-detail-state="loading"]').count() === 0)
  state.broken = false
  await detail(page, 'u3').locator('button', { hasText: '重试' }).click()
  await detail(page, 'u3').locator('[data-challenge-doc]').waitFor()
  check('重试：再取一次，取到后显示理由', hits.artifacts.filter((a) => a === BROKEN).length === 2
    && (await text(detail(page, 'u3').locator('[data-challenge-doc]'))).includes('151,000'))
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
  await page.locator('[data-challenge-show-all]').click()
  await page.waitForFunction(() => document.querySelectorAll('[data-challenge-item]').length === 12)
  check('显示全部：从工件里补齐其余两句，排在后面', JSON.stringify(await page.locator('[data-challenge-item]').evaluateAll((els) =>
    els.slice(10).map((el) => el.getAttribute('data-challenge-item')))) === JSON.stringify(['u30', 'u31']) && hits.artifacts.includes(MANY))
  check('显示全部之后不再写「另有」', await page.locator('[data-challenge-more]').count() === 0)
  check('补出来的一句带着自己的判断标签', (await text(item(page, 'u30').locator('[data-challenge-tag]'))) === '被推翻')
  await toggle(page, 'u30').click()
  await detail(page, 'u30').locator('[data-challenge-rationale-text]').waitFor()
  const u30 = await text(detail(page, 'u30'))
  check('补出来的那几句也能点开', u30.includes('白汀区的退款金额高于云屿区这一说法与查询结果不符'), u30)
  check('超过十句：页面没有报错', errors.length === 0, errors.join('；'))
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
  check('跟随设置时说明跟的是哪一项', (await text(page.locator('[data-tri-hint]'))) === '使用「设置 → 核查」中「反方审稿 · 启用」的值')
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
  await shot(page, 'inspector-dark')
  check('检查器：没有发出任何写请求', hits.writes.length === 0, hits.writes.join('、'))
  check('检查器：页面没有报错', errors.length === 0, errors.join('；'))
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
