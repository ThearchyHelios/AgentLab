// 画布运行态动效的检查：把脚本化的事件灌进 store，看画布上真的长出了东西。
//
// 为什么不是"真跑一张图"：多 agent 那张图要连模型、要等十几秒，而且撞不上
// "三个人同时还在跑"那一瞬间——检查会变成偶发失败。dev 构建把 store 挂在
// window.__studio 上，这里直接往里灌事件：走的是页面上那一份 applyEvent /
// NodeCard / FlowEdge，只有事件来源是脚本。
//
// 守的是几件"事件对了但界面没跟上"的事：协作矩阵没长出来、并行度没显示、
// 分支跑完看不出走了哪条、动效在系统关了动效时还在播。
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const API = process.env.AGENTLAB_API ?? 'http://localhost:8000/api'
const CHROME = process.env.CHROME_PATH
  ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'

let failed = 0
const check = (name, cond, detail = '') => {
  console.log(`  ${cond ? '✓' : '✗'} ${name}${detail ? ` — ${detail}` : ''}`)
  if (!cond) failed++
}

const agent = (name, description) => ({ name, description, system: name, tools: ['web_search'] })

const GRAPH = {
  nodes: [
    { id: 'start', type: 'input', data: { label: '问题', config: { fields: [{ name: 'q' }] } } },
    { id: 'team', type: 'supervisor', data: { label: '研究协作团队', config: {
      goal: '多角度研究', max_rounds: 4, max_parallel: 3,
      agents: [agent('检索员', '找资料'), agent('分析员', '交叉验证'),
               agent('撰写员', '写成结论'), agent('审校', '核对数字')],
    } } },
    { id: 'gate', type: 'branch', data: { label: '质量门', config: { mode: 'expression', cases: [
      { key: 'ok', condition: "vars.verdict == 'ok'", label: '通过' },
      { key: 'fail', condition: "vars.verdict == 'fail'", label: '打回' },
    ] } } },
    { id: 'retry', type: 'loop', data: { label: '返工重试', config: { mode: 'while', condition: 'x', max_iterations: 5 } } },
    { id: 'done', type: 'output', data: { label: '出具', config: { fields: [{ name: 'r' }] } } },
  ],
  edges: [
    { source: 'start', target: 'team' },
    { source: 'team', target: 'gate' },
    { source: 'gate', target: 'done', sourceHandle: 'ok' },
    { source: 'gate', target: 'retry', sourceHandle: 'fail' },
    { source: 'retry', target: 'done', sourceHandle: 'done' },
  ],
}

const ev = (seq, type, node_id, data = {}) => ({ seq, type, node_id, data, ts: Date.now() / 1000 })

/** 三个人同时开工，然后各自交回 */
const PARALLEL = [
  ev(1, 'run.started', null, { nodes: 5 }),
  ev(2, 'node.started', 'start', {}),
  ev(3, 'node.finished', 'start', { duration_ms: 0, preview: { q: 'x' } }),
  ev(4, 'node.started', 'team', { node_type: 'supervisor' }),
  ev(5, 'log', 'team', { level: 'info', round: 0, agents: ['检索员', '分析员', '撰写员'], done: false,
    parallel: 3, reason: '三个方向互不依赖', message: '调度 → 检索员、分析员、撰写员 · 3 人同时进行' }),
  ev(6, 'agent.step.start', 'team', { agent: '检索员', instruction: '把资料找全', round: 0, parallel: 3 }),
  ev(7, 'agent.step.start', 'team', { agent: '分析员', instruction: '交叉验证', round: 0, parallel: 3 }),
  ev(8, 'agent.step.start', 'team', { agent: '撰写员', instruction: '写成结论', round: 0, parallel: 3 }),
]

const SETTLED = [
  ev(9, 'agent.step.end', 'team', { agent: '检索员', duration_ms: 6600, round: 0, parallel: 3, preview: '8 篇' }),
  ev(10, 'agent.step.end', 'team', { agent: '分析员', duration_ms: 9800, round: 0, parallel: 3, preview: '2 处矛盾' }),
  ev(11, 'agent.step.end', 'team', { agent: '撰写员', duration_ms: 4200, round: 0, parallel: 3, preview: '草案' }),
  ev(12, 'log', 'team', { level: 'info', round: 1, agents: ['审校'], done: false, parallel: 1,
    reason: '数字要有人核对', message: '调度 → 审校' }),
  ev(13, 'agent.step.start', 'team', { agent: '审校', instruction: '核对数字', round: 1, parallel: 1 }),
]

const FINISHED = [
  ev(21, 'node.finished', 'retry', { duration_ms: 0, preview: { __decision__: 'body', iteration: 0 } }),
  ev(22, 'run.finished', null, { output: {}, usage: {}, duration_ms: 30000 }),
]

const BRANCH = [
  ev(14, 'node.finished', 'team', { duration_ms: 26400, preview: { text: '结论' } }),
  ev(15, 'log', 'team', { level: 'info', round: 2, agents: [], done: true, reason: '可出具', message: '调度 → 结束协作' }),
  ev(16, 'node.started', 'gate', {}),
  ev(17, 'edge.taken', 'gate', { branch: 'fail', reason: "命中条件：vars.verdict == 'fail'", mode: 'expression' }),
  ev(18, 'node.finished', 'gate', { duration_ms: 0, preview: { __decision__: 'fail' } }),
  ev(19, 'node.started', 'retry', {}),
  ev(20, 'edge.taken', 'retry', { branch: 'body', iteration: 0, mode: 'while' }),
]

async function openStudio(extra = {}) {
  const browser = await chromium.launch({ executablePath: CHROME })
  const ctx = await browser.newContext({ viewport: { width: 1400, height: 950 }, ...extra })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message))
  page.on('console', (m) => { if (m.type() === 'error' && !m.text().includes('404')) errors.push(m.text()) })

  // 先让后端把坐标排好再落库。图里没有坐标的话所有节点都堆在原点，
  // 这张"最近更新的工作流"会被别的检查脚本（check-ui 打开 /studio 落到第一张）
  // 撞上，那边点节点就会点到叠在一起的东西上——检查之间不该互相下绊子
  const laid = await (await fetch(`${API}/copilot/layout`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ graph: GRAPH }),
  })).json()

  const list = await (await fetch(`${API}/workflows`)).json()
  const found = list.find((w) => w.name === '__fx_check__')
  const id = found
    ? found.id
    : (await (await fetch(`${API}/workflows`, {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ name: '__fx_check__', graph: laid }),
      })).json()).id
  if (found) {
    await fetch(`${API}/workflows/${id}`, {
      method: 'PATCH', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ graph: laid }),
    })
  }
  await page.goto(`${WEB}/studio/${id}`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(900)
  await page.locator('.react-flow__controls-fitview').click()
  await page.waitForTimeout(500)
  return { browser, page, errors, id }
}

const feed = (page, events) => page.evaluate((list) => {
  for (const e of list) window.__studio.getState().applyEvent(e)
}, events)

// ---------------------------------------------------------------- 展开

console.log('=== 多 agent 节点展开协作矩阵 ===')
{
  const { browser, page, errors } = await openStudio()
  await feed(page, PARALLEL)
  await page.waitForTimeout(500)

  const matrix = page.locator('.team-matrix').first()
  check('矩阵长出来了', await matrix.count() > 0)

  const rows = page.locator('.team-row')
  check('花名册全员在列（含没被派到的）', await rows.count() === 4, `${await rows.count()} 行`)
  check('三个人同时在跑', await page.locator('.team-row-running').count() === 3,
    `${await page.locator('.team-row-running').count()} 行`)
  check('没被派到的那个是待命', await page.locator('.team-row-idle').count() === 1)

  const body = await matrix.innerText()
  check('说出了并行度', body.includes('3 人同时进行'), body.split('\n')[0])
  check('说了是第几轮', /第 1 轮/.test(body))
  check('在跑的人带着当前任务', body.includes('把资料找全'))

  // 正在跑的节点：光环和扫描线都得在
  check('运行中的卡片有光弧', await page.locator('.node-running .fx-halo').count() > 0)
  check('运行中的卡片有扫描线', await page.locator('.node-running .fx-scan').count() > 0)
  check('活跃边上有流动的光点', await page.locator('.edge-packet').count() > 0,
    `${await page.locator('.edge-packet').count()} 个`)

  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))
  await browser.close()
}

// ---------------------------------------------------------------- 收束

console.log('=== 一轮交回之后 ===')
{
  const { browser, page } = await openStudio()
  await feed(page, [...PARALLEL, ...SETTLED])
  await page.waitForTimeout(500)

  const matrix = page.locator('.team-matrix').first()
  const body = await matrix.innerText()
  check('交回的人不再是"进行中"', await page.locator('.team-row-running').count() === 1,
    `${await page.locator('.team-row-running').count()} 个还在跑`)
  check('说了跑了多少轮', /^\d+ 轮$/m.test(body), body.match(/\d+ 轮/)?.[0])
  check('算出了并行省下的时间', /并行省下/.test(body), body.match(/并行省下[^\n]*/)?.[0])

  // 条形长度按最慢那个归一化：9.8s 的那条必须比 4.2s 的长
  const widths = await page.locator('.team-bar-fill').evaluateAll(
    (els) => els.map((el) => parseFloat(el.style.width)))
  check('条形长度和耗时成正比', widths.length >= 2 && Math.max(...widths) === 100
    && widths.every((w) => w > 0 && w <= 100), widths.join(', '))

  // 前几轮干过的人不该被写成"待命"
  check('上一轮干过的人保留成绩', body.includes('6.6s') && body.includes('9.8s'))
  await browser.close()
}

// ---------------------------------------------------------------- 分支与循环

console.log('=== 分支命中的出口 / 循环第几轮 ===')
{
  const { browser, page } = await openStudio()
  await feed(page, [...PARALLEL, ...SETTLED, ...BRANCH])
  await page.waitForTimeout(500)

  const gate = page.locator('.react-flow__node[data-id="gate"]')
  check('命中的出口被点亮', await gate.locator('.handle-taken').count() === 1)
  check('落空的出口被压暗', await gate.locator('.handle-idle').count() === 2,
    `${await gate.locator('.handle-idle').count()} 个`)
  // 还在跑的时候那条边是"活跃"（虚线 + 光点）；跑完才落成"走过的边"
  check('跑动中那条边是活跃的', await page.locator('.react-flow__edge.edge-active').count() > 0,
    `${await page.locator('.react-flow__edge.edge-active').count()} 条`)
  await feed(page, FINISHED)
  await page.waitForTimeout(400)
  check('跑完之后执行路径留在画布上',
    await page.locator('.react-flow__edge.edge-taken').count() > 0,
    `${await page.locator('.react-flow__edge.edge-taken').count()} 条`)

  const retry = page.locator('.react-flow__node[data-id="retry"]')
  check('循环显示第几轮 / 上限', (await retry.innerText()).includes('第 1/5 轮'),
    await retry.innerText().then((t) => t.split('\n')[0]))
  await browser.close()
}

// ---------------------------------------------------------------- 关掉动效

console.log('=== 系统关了动效 ===')
{
  const { browser, page } = await openStudio({ reducedMotion: 'reduce' })
  await feed(page, PARALLEL)
  await page.waitForTimeout(400)

  const styles = await page.evaluate(() => {
    const el = document.querySelector('.fx-halo')
    return {
      matches: matchMedia('(prefers-reduced-motion: reduce)').matches,
      halo: el ? getComputedStyle(el).display : '不在 DOM 里',
      packet: document.querySelector('.edge-packet')
        ? getComputedStyle(document.querySelector('.edge-packet')).display : '不在 DOM 里',
    }
  })
  check('浏览器确实处在减少动效模式', styles.matches)
  check('光弧不再绘制', styles.halo === 'none', styles.halo)
  check('光点不再绘制', styles.packet === 'none', styles.packet)
  // 状态本身还得看得出来，不能因为关了动效就变成"不知道在不在跑"
  const body = await page.locator('.team-matrix').first().innerText()
  check('静态下仍看得出谁在跑', body.includes('进行中'))
  await browser.close()
}

console.log(failed ? `\n${failed} 项未通过` : '\n全部通过')
process.exit(failed ? 1 : 0)
