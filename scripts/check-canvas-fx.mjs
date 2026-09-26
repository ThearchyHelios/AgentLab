// 画布表层的检查：把脚本化的事件灌进 store，看画布、运行胶囊、航迹坞真的跟上了。
//
// 为什么不是"真跑一张图"：多 agent 那张图要连模型、要等十几秒，而且撞不上
// "三个人同时还在跑"那一瞬间——检查会变成偶发失败。dev 构建把 store 挂在
// window.__studio 上，这里直接往里灌事件：走的是页面上那一份 applyEvent /
// NodeCard / FlowEdge / RunHud，只有事件来源是脚本。
//
// 不写库：工作流、会话、已发布版本全用 page.route 在浏览器里伪造，后端只被
// 读（目录、校验、排版这些纯计算）。以前这里会在库里建一张 __fx_check__，
// 别的检查打开 /studio 落到第一张图时会撞上它。
//
// 守的是几件"事件对了但界面没跟上"的事：
// - 取消 / 挂起之后光弧、光点、协作行还在转；
// - 等人时入边还在流光点，工具栏还给一个必然 409 的「停止」；
// - 失败后「接着跑」出不来；胶囊的计时不走；
// - 跑完之后看不出执行路径；拖时间轴卡片不跟着变；
// - 打开大图缩到 0.2；正式运行期间还能拖动改图；
// - 一次性时刻被补发的历史事件触发，或者播完不撤；
// - 定位 / 去审批取景半路被拉远，落地卡在 compact 档（错误正文被藏）；
// - 「只看执行路径」带进下一次运行；正式运行横幅盖住「恢复」；
// - 同一刻的等人时长几处写几个数；只是点一下画布就暂停跟随；
// - Esc 在右栏里按也清掉整次运行；泳道、节点格、坞高只能用鼠标。
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
    { id: 'start', type: 'input', data: { label: '问题', config: { fields: [{ name: 'q', required: true }] } } },
    { id: 'team', type: 'supervisor', data: { label: '研究协作团队', config: {
      goal: '多角度研究', max_rounds: 4, max_parallel: 3,
      agents: [agent('检索员', '找资料'), agent('分析员', '交叉验证'),
               agent('撰写员', '写成结论'), agent('审校', '核对数字')],
    } } },
    { id: 'gate', type: 'branch', data: { label: '质量门', config: { mode: 'expression', cases: [
      { key: 'ok', condition: "vars.verdict == 'ok'", label: '通过' },
      { key: 'fail', condition: "vars.verdict == 'fail'", label: '打回' },
    ] } } },
    { id: 'review', type: 'human', data: { label: '主管审批', config: { mode: 'approve', title: '看一眼再发' } } },
    { id: 'retry', type: 'loop', data: { label: '返工重试', config: { mode: 'while', condition: 'x', max_iterations: 5 } } },
    { id: 'fix', type: 'llm', data: { label: '返工', config: { prompt: '按意见改' } } },
    { id: 'done', type: 'output', data: { label: '出具', config: { fields: [{ name: 'r' }] } } },
  ],
  edges: [
    { id: 'e-start-team', source: 'start', target: 'team' },
    { id: 'e-team-gate', source: 'team', target: 'gate' },
    { id: 'e-gate-review', source: 'gate', target: 'review', sourceHandle: 'ok' },
    { id: 'e-gate-retry', source: 'gate', target: 'retry', sourceHandle: 'fail' },
    { id: 'e-retry-fix', source: 'retry', target: 'fix', sourceHandle: 'body' },
    { id: 'e-fix-retry', source: 'fix', target: 'retry' },
    { id: 'e-retry-done', source: 'retry', target: 'done', sourceHandle: 'done' },
    { id: 'e-review-done', source: 'review', target: 'done', sourceHandle: 'approved' },
  ],
}

/** 一条长链：取景策略要以入口为左锚、不低于可读缩放 */
const WIDE = {
  nodes: Array.from({ length: 12 }, (_, i) => ({
    id: `n${i}`, type: i === 0 ? 'input' : i === 11 ? 'output' : 'llm',
    data: { label: i === 0 ? '入口' : `第 ${i} 步`, config: i === 0 ? { fields: [{ name: 'q' }] } : { prompt: 'x' } },
  })),
  edges: Array.from({ length: 11 }, (_, i) => ({ id: `w${i}`, source: `n${i}`, target: `n${i + 1}` })),
}

// ---------------------------------------------------------------- 事件

const BASE = Date.now() / 1000 - 45
// t 是相对 BASE 的秒数：段有真实宽度，时间轴才有东西可拖
const ev = (seq, type, node_id, data = {}, t = seq) => ({ seq, type, node_id, data, ts: BASE + t })

const PARALLEL = [
  ev(1, 'run.started', null, { nodes: 7 }, 0),
  ev(2, 'node.started', 'start', {}, 0.2),
  ev(3, 'node.finished', 'start', { duration_ms: 12, preview: { q: 'x' } }, 0.3),
  ev(4, 'node.started', 'team', { node_type: 'supervisor' }, 0.5),
  ev(5, 'log', 'team', { level: 'info', round: 0, agents: ['检索员', '分析员', '撰写员'], done: false,
    parallel: 3, reason: '三个方向互不依赖', message: '调度 → 检索员、分析员、撰写员 · 3 人同时进行' }, 3),
  ev(6, 'agent.step.start', 'team', { agent: '检索员', instruction: '把资料找全', round: 0, parallel: 3 }, 3.1),
  ev(7, 'agent.step.start', 'team', { agent: '分析员', instruction: '交叉验证', round: 0, parallel: 3 }, 3.2),
  ev(8, 'agent.step.start', 'team', { agent: '撰写员', instruction: '写成结论', round: 0, parallel: 3 }, 3.3),
]
// 并行那一段刚开始：最后一条事件离"现在"很近，实时计时从这里往前走
const PARALLEL_NOW = PARALLEL.map((e) => ({ ...e, ts: e.ts + 40 }))

const TEAM_DONE = [
  ev(9, 'agent.step.end', 'team', { agent: '检索员', duration_ms: 6600, round: 0, parallel: 3, preview: '8 篇' }, 9.7),
  ev(10, 'agent.step.end', 'team', { agent: '分析员', duration_ms: 9800, round: 0, parallel: 3, preview: '2 处矛盾' }, 13),
  ev(11, 'agent.step.end', 'team', { agent: '撰写员', duration_ms: 4200, round: 0, parallel: 3, preview: '草案' }, 13.1),
  ev(12, 'node.finished', 'team', { duration_ms: 13000, preview: { text: '结论' } }, 13.5),
]

const TO_REVIEW = [
  ev(13, 'node.started', 'gate', {}, 13.6),
  ev(14, 'edge.taken', 'gate', { branch: 'ok', reason: "命中条件：vars.verdict == 'ok'", mode: 'expression' }, 13.7),
  ev(15, 'node.finished', 'gate', { duration_ms: 3, preview: { __decision__: 'ok' } }, 13.8),
  ev(16, 'node.started', 'review', {}, 14),
  ev(17, 'human.requested', 'review', { node_id: 'review', mode: 'approve', title: '看一眼再发' }, 14.1),
]

const TO_LOOP = [
  ev(13, 'node.started', 'gate', {}, 14),
  ev(14, 'edge.taken', 'gate', { branch: 'fail', reason: "命中条件：vars.verdict == 'fail'", mode: 'expression' }, 14.1),
  ev(15, 'node.finished', 'gate', { duration_ms: 3, preview: { __decision__: 'fail' } }, 14.2),
  ev(16, 'node.started', 'retry', {}, 14.5),
  ev(17, 'edge.taken', 'retry', { branch: 'body', iteration: 0, mode: 'while' }, 14.6),
  ev(18, 'node.finished', 'retry', { duration_ms: 1, preview: { __decision__: 'body', iteration: 0 } }, 14.7),
  ev(19, 'node.started', 'fix', {}, 15),
  ev(20, 'node.finished', 'fix', { duration_ms: 8000, preview: { text: '改好了' } }, 23),
  ev(21, 'node.started', 'retry', {}, 23.2),
  ev(22, 'edge.taken', 'retry', { branch: 'done', iteration: 1, mode: 'while' }, 23.3),
  ev(23, 'node.finished', 'retry', { duration_ms: 1, preview: { __decision__: 'done', iteration: 1 } }, 23.4),
  ev(24, 'node.started', 'done', {}, 24),
  ev(25, 'node.finished', 'done', { duration_ms: 400, preview: { r: 'ok' } }, 24.4),
  ev(26, 'run.finished', null, { output: { r: 'ok' }, usage: { input_tokens: 1200, output_tokens: 840, cost_usd: 0.0123 },
    duration_ms: 24000, timing: { wall_ms: 24500, active_ms: 24500, wait_ms: 0 } }, 24.5),
]

// ---------------------------------------------------------------- 伪造的后端

const FX_ID = 'fx-canvas'
const WIDE_ID = 'fx-wide'

async function layout(graph) {
  const res = await fetch(`${API}/copilot/layout`, {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ graph }),
  })
  if (!res.ok) throw new Error(`排版接口 ${res.status}`)
  return res.json()
}

const [LAID, LAID_WIDE] = await Promise.all([layout(GRAPH), layout(WIDE)])
const workflow = (id, name, graph, extra = {}) => ({
  id, name, description: '', graph, tags: [], version: 3, is_template: false, status: 'draft',
  published_version: 2, run_count: 0, created_at: '2026-09-26T00:00:00Z', updated_at: '2026-09-26T00:00:00Z', ...extra,
})
const FAKES = {
  [FX_ID]: workflow(FX_ID, '画布表层检查', LAID),
  [WIDE_ID]: workflow(WIDE_ID, '取景检查', LAID_WIDE, { published_version: null }),
}
// 已发布的 v2 入口字段和画布上的不一样：正式运行的表单必须照 v2 填
const VERSION = { id: 'v2', version: 2, note: '', workflow_id: FX_ID, graph: LAID, graph_hash: 'x',
  input_fields: [{ name: 'topic', required: true, description: '已发布版本的字段' }], published: true }

async function fakeBackend(page) {
  const json = (route, body, status = 200) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
  await page.route(/\/api\/workflows(\?.*)?$/, async (route) => {
    if (route.request().method() !== 'GET') return route.continue()
    const res = await route.fetch()
    const real = await res.json().catch(() => [])
    return json(route, [...Object.values(FAKES), ...(Array.isArray(real) ? real : [])])
  })
  await page.route(/\/api\/workflows\/fx-[a-z]+(\/.*)?(\?.*)?$/, (route) => {
    const url = new URL(route.request().url())
    const [, , , id, sub, v] = url.pathname.split('/')
    if (sub === 'versions' && v) return json(route, VERSION)
    if (route.request().method() === 'GET' && !sub) return json(route, FAKES[id])
    return json(route, { detail: '检查脚本不写库' }, 409)
  })
  // 画布会话：只读列表返回空，建会话返回一个假的，都不落库
  await page.route(/\/api\/conversations(\/.*)?(\?.*)?$/, (route) => {
    const method = route.request().method()
    if (method === 'GET') return json(route, [])
    return json(route, { id: 'fx-conv', kind: 'canvas', title: '', turns: [] })
  })
  await page.route(/\/api\/runs(\/.*)?(\?.*)?$/, (route) =>
    route.request().method() === 'GET' ? route.continue() : json(route, { detail: '检查脚本不发起运行' }, 409))
}

async function openStudio({ id = FX_ID, theme = 'dark', ...extra } = {}) {
  const browser = await chromium.launch({ executablePath: CHROME })
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 950 }, colorScheme: theme, ...extra })
  await ctx.addInitScript((t) => { try { localStorage.setItem('agentlab.theme', t) } catch { /* 隐私窗口 */ } }, theme)
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message))
  page.on('console', (m) => { if (m.type() === 'error' && !m.text().includes('404') && !m.text().includes('409')) errors.push(m.text()) })
  await fakeBackend(page)
  await page.goto(`${WEB}/studio/${id}`, { waitUntil: 'networkidle' })
  await page.waitForFunction((wid) => window.__studio?.getState().workflow?.id === wid, id, { timeout: 15000 })
  await page.waitForTimeout(700)
  return { browser, page, errors }
}

const feed = (page, events) => page.evaluate((list) => {
  const s = window.__studio.getState()
  for (const e of list) s.applyEvent(e)
}, events)

/** 灌事件前先放一个运行对象：真实流程里它来自 POST /runs（queued 的快照） */
const seedRun = (page, extra = {}) => page.evaluate((x) => {
  window.__studio.setState({ run: {
    id: 'fxrun000001', workflow_id: 'fx-canvas', workflow_name: '画布表层检查', status: 'queued', input: {},
    output: {}, error: null, usage: {}, run_class: 'exploratory', version: null, ...x,
  } })
}, extra)

const count = (page, sel) => page.locator(sel).count()
const capsule = (page) => page.locator('.sf-capsule')
/** 工具栏上的「运行」发起按钮（胶囊的名字也以「运行中」开头，按全名找） */
const launcher = (page) => page.locator('[data-run-control]').getByRole('button', { name: /^运行$/ })
const capText = async (page) => (await capsule(page).innerText().catch(() => '')).replace(/\s+/g, ' ')
const edgeClass = (page, id) => page.evaluate((eid) =>
  document.querySelector(`.react-flow__edge[data-id="${eid}"]`)?.getAttribute('class') ?? '', id)
/** 画布上一处空白（不压在卡片、控件、浮条上）：从那儿拖才是平移画布 */
const emptySpot = (page) => page.evaluate(() => {
  const r = document.querySelector('.react-flow__pane').getBoundingClientRect()
  for (let y = r.top + 90; y < r.bottom - 120; y += 23) {
    for (let x = r.left + 30; x < r.right - 180; x += 37) {
      if (document.elementFromPoint(x, y)?.classList.contains('react-flow__pane')) return { x, y }
    }
  }
  return { x: r.left + 30, y: r.top + 90 }
})
const nodeState = (page, id) => page.evaluate((nid) => {
  const el = document.querySelector(`.react-flow__node[data-id="${nid}"] [data-state]`)
  return el?.getAttribute('data-state') ?? null
}, id)

// ---------------------------------------------------------------- 运行中

console.log('=== 运行中：胶囊、光点、计时 ===')
{
  const { browser, page, errors } = await openStudio()
  await seedRun(page)
  await feed(page, PARALLEL_NOW)
  await page.waitForTimeout(400)

  check('运行中的卡片有光弧', await count(page, '.node-running .fx-halo, [data-state="running"] .fx-halo') > 0)
  check('协作矩阵长出来了', await count(page, '.team-matrix') > 0)
  check('三个成员同时在跑', await count(page, '.team-row-running') >= 3, `${await count(page, '.team-row-running')} 行`)
  check('流入执行中节点的边：一个光点', await count(page, '.react-flow__edge.sf-e-flow .edge-packet') === 1,
    `${await count(page, '.edge-packet')} 个光点`)
  check('走过的边是实线（sf-e-walked）', (await edgeClass(page, 'e-start-team')).includes('sf-e-flow')
    || (await edgeClass(page, 'e-start-team')).includes('sf-e-walked'), await edgeClass(page, 'e-start-team'))

  const cap = await capText(page)
  check('胶囊替换了工具栏的「停止」：写着运行中', cap.includes('运行中'), cap)
  check('运行中的主动作是停止', await capsule(page).getByRole('button', { name: /停止/ }).count() === 1)
  check('运行中不露发起按钮', await launcher(page).count() === 0)

  const t1 = await page.locator('[data-clock]').innerText()
  await page.waitForTimeout(1300)
  const t2 = await page.locator('[data-clock]').innerText()
  check('胶囊计时在走', t1 !== t2 && /^\d\d:\d\d\.\d$/.test(t2), `${t1} → ${t2}`)

  // 展开遥测面板：墙钟、节点格、并行、时限
  await capsule(page).locator('.sf-cap-main').click()
  const hud = page.locator('.sf-hud')
  const hudText = (await hud.innerText()).replace(/\s+/g, ' ')
  check('面板说出了并行路数', /3 路/.test(hudText), hudText.match(/并行 [^ ]+ [^ ]+/)?.[0])
  check('节点格按拓扑序每节点一格', await count(page, '.sf-cell') === 7, `${await count(page, '.sf-cell')} 格`)
  check('面板有墙钟 T+', /T\+\d\d:\d\d\.\d/.test(hudText))
  check('时限余量来自 /api/system', /余 \d+:\d\d/.test(hudText), hudText.match(/时限[^用]*/)?.[0])
  check('还没收到用量时写「—」，不写 0 tok', /用量 — —/.test(hudText) && !/(^|\s)0 tok/.test(hudText),
    hudText.match(/用量[^时]*/)?.[0])
  const cells = await page.evaluate(() => ({
    items: document.querySelectorAll('.sf-cells > [role="listitem"] > button.sf-cell').length,
    tabbable: document.querySelectorAll('.sf-cells button.sf-cell[tabindex="0"]').length,
  }))
  check('节点格是列表项里的按钮，整条只占一个 Tab 位', cells.items === 7 && cells.tabbable === 1, JSON.stringify(cells))
  await page.locator('.sf-cells button.sf-cell[tabindex="0"]').focus()
  await page.keyboard.press('ArrowRight')
  const cellAt = await page.evaluate(() => [...document.querySelectorAll('.sf-cells button.sf-cell')].indexOf(document.activeElement))
  check('节点格：左右键在格子间走', cellAt === 1, String(cellAt))
  await page.keyboard.press('Escape')
  await page.waitForTimeout(150)
  check('Esc 收起面板', await count(page, '.sf-hud') === 0)

  check('坞头开关有名字（窄屏只剩图标时读屏也念得出）',
    await page.getByRole('checkbox', { name: '跟随执行' }).count() === 1
    && await page.getByRole('checkbox', { name: '压缩空闲' }).count() === 1)
  check('运行中不给「只看执行路径」：路径要等结束才定下来',
    await page.getByRole('checkbox', { name: '只看执行路径' }).count() === 0)

  // 只是点一下画布空白处（取消选择）：d3 也报开始和结束，但视口没动，不该暂停跟随
  await page.locator('.react-flow__pane').click({ position: { x: 20, y: 300 } })
  await page.waitForTimeout(200)
  check('点一下画布不算手动平移：跟随照常', !(await page.locator('.sf-follow').innerText()).includes('暂停'),
    await page.locator('.sf-follow').innerText())

  // 泳道、坞高：键盘也能用
  const lanes = await page.evaluate(() => ({
    n: document.querySelectorAll('.tl-lane[role="button"]').length,
    tab: document.querySelectorAll('.tl-lane[tabindex="0"]').length,
  }))
  check('泳道是按钮，整片只占一个 Tab 位', lanes.n >= 7 && lanes.tab === 1, JSON.stringify(lanes))
  await page.locator('.tl-lane[tabindex="0"]').focus()
  await page.keyboard.press('ArrowDown')
  const nextLane = await page.evaluate(() => document.activeElement?.getAttribute('data-node-id'))
  await page.evaluate(() => window.__studio.setState({ focusRequest: null }))
  await page.keyboard.press('Enter')
  const picked = await page.evaluate(() => window.__studio.getState().focusRequest?.id)
  check('泳道：下键换下一条，回车在画布上取景', nextLane === 'team' && picked === 'team', `${nextLane} → ${picked}`)
  const grip = page.locator('.tl-grip')
  const h0 = Number(await grip.getAttribute('aria-valuenow'))
  await grip.focus()
  await page.keyboard.press('ArrowUp')
  const h1 = Number(await grip.getAttribute('aria-valuenow'))
  check('坞高可以用键盘调（上键加高 16px）', h1 === h0 + 16, `${h0} → ${h1}`)

  check('航迹坞出现了', await count(page, '.tl') === 1)
  check('坞里按拓扑序排了泳道', await count(page, '.tl-lane') >= 7, `${await count(page, '.tl-lane')} 条`)

  const lod = await page.evaluate(() => {
    const rf = document.querySelector('.react-flow')
    return { lod: rf?.dataset.lod, zoom: rf?.style.getPropertyValue('--zoom'), phase: rf?.dataset.runPhase,
      rank: document.querySelector('.react-flow__node[data-id="gate"]')?.style.getPropertyValue('--rank') }
  })
  check('根元素写了 data-lod / --zoom', !!lod.lod && !!lod.zoom, JSON.stringify(lod))
  check('根元素写了 data-run-phase', lod.phase === 'running', lod.phase)
  check('节点上写了 --rank', lod.rank === '2', `gate 的 --rank=${lod.rank}`)
  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))
  await browser.close()
}

// ---------------------------------------------------------------- 等人

console.log('=== 等人：入边闸门，不流光点；去审批代替停止 ===')
{
  const { browser, page } = await openStudio()
  await seedRun(page)
  await feed(page, [...PARALLEL, ...TEAM_DONE, ...TO_REVIEW])
  await page.waitForTimeout(400)
  // human.requested 到了、run.interrupted 还没来（并行分支要等同一超步跑完）
  const cls = await edgeClass(page, 'e-gate-review')
  check('入边改成闸门（sf-e-held）', cls.includes('sf-e-held'), cls)
  check('入边上没有光点', await count(page, '.react-flow__edge[data-id="e-gate-review"] .edge-packet') === 0)
  check('闸门画出来了', await count(page, '.react-flow__edge[data-id="e-gate-review"] .sf-gate') === 1)
  check('run.interrupted 之前相位还是运行中，工具栏不给「去审批」',
    (await capText(page)).includes('运行中') && await capsule(page).getByRole('button', { name: /去审批/ }).count() === 0,
    await capText(page))

  await feed(page, [ev(18, 'run.interrupted', 'review', { payload: { node_id: 'review' } }, 14.3)])
  await page.waitForTimeout(300)
  const cap = await capText(page)
  check('胶囊写着等待审批', cap.includes('等待审批'), cap)
  check('主动作是去审批', await capsule(page).getByRole('button', { name: /去审批/ }).count() === 1)
  check('等审批时不给必然 409 的停止', await capsule(page).getByRole('button', { name: /停止/ }).count() === 0)
  check('等人时全图 0 个光点', await count(page, '.edge-packet') === 0, `${await count(page, '.edge-packet')} 个`)
  check('小地图上等人的节点放大了一圈', await count(page, '.sf-mm-ring.sf-mm-waiting') === 1)

  // 去审批：属性面板盖在右栏上时先让开，画布取景到等人的节点
  await page.evaluate(() => window.__studio.setState({ focusRequest: null, selectedId: 'gate' }))
  await capsule(page).getByRole('button', { name: /去审批/ }).click()
  await page.waitForTimeout(200)
  const went = await page.evaluate(() => ({ sel: window.__studio.getState().selectedId, focus: window.__studio.getState().focusRequest?.id }))
  check('去审批：属性面板让开、取景到等人的节点', went.sel === null && went.focus === 'review', JSON.stringify(went))

  // F：在需要处理的队列里逐个定位
  await page.evaluate(() => window.__studio.setState({ focusRequest: null }))
  await page.locator('.react-flow__pane').click({ position: { x: 20, y: 300 } })
  await page.keyboard.press('f')
  await page.waitForTimeout(200)
  const focus = await page.evaluate(() => window.__studio.getState().focusRequest?.id)
  check('F 定位到等人的节点', focus === 'review', String(focus))
  await browser.close()
}

console.log('=== 等人：同一刻的等待时长各处同一个数 ===')
{
  // 打开一条正在等审批的运行（?run= 接上时补发的历史）。事件挪到"刚才"：等了七八秒，
  // 段够宽、写得下字；补发的历史不参与时钟偏差，等待时长按真实时间走
  const { browser, page } = await openStudio()
  await seedRun(page)
  const shift = Date.now() / 1000 - 22 - BASE
  const history = [...PARALLEL, ...TEAM_DONE, ...TO_REVIEW,
    ev(18, 'run.interrupted', 'review', { payload: { node_id: 'review' } }, 14.3)]
  await feed(page, history.map((e) => ({ ...e, ts: e.ts + shift, replay: true })))
  await page.waitForTimeout(400)
  await capsule(page).locator('.sf-cap-main').click()
  await page.waitForTimeout(300)
  // 一次读完：所有计时共用一拍，同一次提交里读到的是同一刻
  const waits = await page.evaluate(() => {
    const txt = (el) => el?.textContent ?? ''
    const find = (sel, head) => [...document.querySelectorAll(sel)].map(txt).find((t) => t.startsWith(head)) ?? ''
    const raw = {
      header: txt(document.querySelector('.tl-alert.is-warn')),
      headline: txt(document.querySelector('.sf-hud-line')),
      queue: txt(document.querySelector('.sf-queue-item.is-waiting .sf-dim')),
      metric: find('.sf-metric-s', '等人'),
      runBar: find('.tl-run .tl-bar em', '等人'),
      lane: find('.tl-lane[data-node-id="review"] .tl-bar em', '等待审批'),
      sum: txt(document.querySelector('.tl-run .tl-sum')),
    }
    // 十分之一秒为单位；合计一分钟以上写 m:ss，只比到秒
    const val = (k, s) => {
      let m = /(\d+):(\d\d)\.(\d)/.exec(s)
      if (m) return Number(m[1]) * 600 + Number(m[2]) * 10 + Number(m[3])
      m = /等 (\d+)\.(\d) s/.exec(s)
      if (m) return Number(m[1]) * 10 + Number(m[2])
      m = /等 (\d+):(\d\d)/.exec(s)
      return m ? { secs: Number(m[1]) * 60 + Number(m[2]) } : null
    }
    return { raw, vals: Object.fromEntries(Object.entries(raw).map(([k, s]) => [k, val(k, s)])) }
  })
  const base = waits.vals.header
  const same = typeof base === 'number' && Object.values(waits.vals).every((v) =>
    v === base || (v && typeof v === 'object' && v.secs === Math.floor(base / 10)))
  check('坞头、面板标题、需要处理、等人合计、运行行、泳道、右边合计：同一个数', same, JSON.stringify(waits.raw))
  check('等了几秒而不是 0', typeof base === 'number' && base >= 30, String(base))
  await browser.close()
}

// ---------------------------------------------------------------- 失败 / 取消 / 挂起

const settleCase = async (label, tail, expect) => {
  console.log(`=== ${label} ===`)
  const { browser, page } = await openStudio()
  await seedRun(page)
  await feed(page, [...PARALLEL, ...tail])
  await page.waitForTimeout(500)
  check('光弧全部收掉', await count(page, '.fx-halo') === 0, `${await count(page, '.fx-halo')} 个`)
  check('光点全部收掉', await count(page, '.edge-packet') === 0, `${await count(page, '.edge-packet')} 个`)
  check('协作行不再"进行中"', await count(page, '.team-row-running') === 0, `${await count(page, '.team-row-running')} 行`)
  const cap = await capText(page)
  check(`胶囊写着${expect.text}`, cap.includes(expect.text), cap)
  for (const name of expect.actions) {
    check(`有「${name}」`, await capsule(page).getByRole('button', { name: new RegExp(name) }).count() >= 1)
  }
  check('不再给停止', await capsule(page).getByRole('button', { name: /停止/ }).count() === 0)
  check('发起按钮回来了', await launcher(page).count() === 1)
  await expect.more?.(page)
  await browser.close()
}

await settleCase('失败：接着跑、定位，下游阻断', [
  ev(9, 'node.failed', 'team', { error: '401 模型 id 不存在，改成可用的模型再接着跑', duration_ms: 6000 }, 9.5),
  ev(10, 'run.failed', null, { error: '「研究协作团队」失败', node_id: 'team', timing: { wall_ms: 9600, active_ms: 9600, wait_ms: 0 } }, 9.6),
], {
  text: '失败', actions: ['接着跑', '定位'],
  more: async (page) => {
    check('下游的边画成阻断（sf-e-cut）', await count(page, '.react-flow__edge.sf-e-cut') > 0,
      `${await count(page, '.react-flow__edge.sf-e-cut')} 条`)
    check('小地图上失败的节点放大了一圈', await count(page, '.sf-mm-ring.sf-mm-failed') === 1)
    const phase = await page.evaluate(() => document.querySelector('.react-flow')?.dataset.runPhase)
    check('根元素的相位是 failed', phase === 'failed', phase)
    await capsule(page).getByRole('button', { name: /定位/ }).click()
    const focus = await page.evaluate(() => window.__studio.getState().focusRequest?.id)
    check('定位请求画布取景到失败节点', focus === 'team', String(focus))

    // 取景落地要是完整卡片：d3 默认插值半路会拉远到 0.4–0.5，回滞又让它停在 compact，
    // 失败卡片的错误正文就被藏了。先拖开，再定位；再缩到 compact，再定位
    const lodNow = () => page.evaluate(() => {
      const rf = document.querySelector('.react-flow')
      return { lod: rf?.dataset.lod, zoom: Number(rf?.style.getPropertyValue('--zoom')) }
    })
    await page.waitForTimeout(600)
    const pane = await page.locator('.react-flow__pane').boundingBox()
    await page.mouse.move(pane.x + 400, pane.y + 300)
    await page.mouse.down()
    await page.mouse.move(pane.x + 60, pane.y + 150, { steps: 6 })
    await page.mouse.up()
    await page.waitForTimeout(300)
    await page.evaluate(() => {
      const rf = document.querySelector('.react-flow')
      window.__minZoom = Number(rf.style.getPropertyValue('--zoom'))
      new MutationObserver(() => {
        window.__minZoom = Math.min(window.__minZoom, Number(rf.style.getPropertyValue('--zoom')))
      }).observe(rf, { attributes: true, attributeFilter: ['style'] })
    })
    const start = await lodNow()
    await capsule(page).getByRole('button', { name: /定位/ }).click()
    await page.waitForTimeout(700)
    const landed = await lodNow()
    const dip = await page.evaluate(() => window.__minZoom)
    check('定位途中不拉远', dip >= Math.min(start.zoom, 0.6) - 1e-3, `起点 ${start.zoom} · 途中最低 ${dip}`)
    check('定位落地：可读缩放、完整卡片', landed.lod === 'full' && landed.zoom >= 0.6 - 1e-3, JSON.stringify(landed))
    for (let i = 0; i < 2; i++) await page.locator('.react-flow__controls-zoomout').click()
    await page.waitForTimeout(400)
    const compact = await lodNow()
    await capsule(page).getByRole('button', { name: /定位/ }).click()
    await page.waitForTimeout(700)
    const back = await lodNow()
    check('从 compact 定位回来也回到完整卡片', compact.lod !== 'full' && back.lod === 'full' && back.zoom >= 0.6 - 1e-3,
      `${JSON.stringify(compact)} → ${JSON.stringify(back)}`)
  },
})

await settleCase('取消：清除、回放', [
  ev(9, 'run.cancelled', null, { timing: { wall_ms: 9000, active_ms: 9000, wait_ms: 0 } }, 9),
], { text: '已取消', actions: ['清除', '回放'] })

await settleCase('服务重启挂起：接着跑', [
  ev(9, 'log', null, { level: 'warn', code: 'server_shutdown', message: '服务关停时这次运行还在跑，已挂起' }, 9),
], {
  text: '已中断', actions: ['接着跑'],
  more: async (page) => {
    await capsule(page).locator('.sf-cap-main').click()
    await page.waitForTimeout(200)
    const counts = (await page.locator('.sf-hud-counts').innerText()).replace(/\s+/g, ' ')
    check('面板的状态计数里有「已中断」的节点，点得到', await count(page, '.sf-count[title^="已中断"]') === 1, counts)
  },
})

// ---------------------------------------------------------------- 跑完

console.log('=== 跑完：执行路径、回边、回放、清除 ===')
{
  const { browser, page, errors } = await openStudio()
  await seedRun(page)
  // 分两批灌：同一秒里只放一个一次性时刻，开场点亮和成功回扫挤在一起时后一个会被让掉
  await feed(page, PARALLEL)
  await page.waitForTimeout(1100)
  await feed(page, [...TEAM_DONE, ...TO_LOOP])
  await page.waitForTimeout(150)
  const moment = await page.evaluate(() => ({
    kind: document.querySelector('.react-flow')?.dataset.moment, sweeps: document.querySelectorAll('.sf-sweep').length,
  }))
  check('实时跑完：根元素写了成功时刻，走过的边回扫一遍', moment.kind === 'success' && moment.sweeps > 0, JSON.stringify(moment))
  await page.waitForTimeout(2800)
  check('时刻只播一次：过后撤掉', await page.evaluate(() => !document.querySelector('.react-flow')?.dataset.moment)
    && await count(page, '.sf-sweep') === 0)

  const cap = await capText(page)
  check('胶囊写着已完成', cap.includes('已完成'), cap)
  check('结束后给回放和清除',
    await capsule(page).getByRole('button', { name: /回放/ }).count() === 1
    && await capsule(page).getByRole('button', { name: /清除/ }).count() === 1)
  check('走过的边留在画布上', await count(page, '.react-flow__edge.sf-e-walked') >= 5,
    `${await count(page, '.react-flow__edge.sf-e-walked')} 条`)
  check('没走的那一侧虚线淡出', (await edgeClass(page, 'e-gate-review')).includes('sf-e-unwalked'),
    await edgeClass(page, 'e-gate-review'))
  check('回边可辨（虚线 + ↺）', (await edgeClass(page, 'e-fix-retry')).includes('sf-e-back')
    && await count(page, '.sf-loop-badge') === 1)
  check('每条边都有箭头', await count(page, '.sf-arrow') === GRAPH.edges.length,
    `${await count(page, '.sf-arrow')} / ${GRAPH.edges.length}`)
  check('没有常驻的光点', await count(page, '.edge-packet') === 0)
  await capsule(page).locator('.sf-cap-main').click()
  const hud = (await page.locator('.sf-hud').innerText()).replace(/\s+/g, ' ')
  check('用量按后端累计校正（2,040 tokens）', /2\.0k tok/.test(hud) && hud.includes('$0.012'), hud.match(/用量[^时]*/)?.[0])
  check('结束后节点数写「经过」', /6\/7 经过/.test(hud), hud.match(/节点 [^ ]+ [^ ]+/)?.[0])
  await page.keyboard.press('Escape')

  // 航迹坞和画布、右栏联动：悬停泳道 = 悬停节点，点泳道 = 画布取景
  const lane = page.locator('.tl-lane[data-node-id="gate"]').first()
  await lane.hover()
  await page.waitForTimeout(100)
  check('悬停泳道：hoveredNodeId 跟着变', await page.evaluate(() => window.__studio.getState().hoveredNodeId) === 'gate')
  await lane.click()
  await page.waitForTimeout(100)
  check('点泳道：画布取景到那个节点', await page.evaluate(() => window.__studio.getState().focusRequest?.id) === 'gate')
  await page.mouse.move(700, 200)
  await page.evaluate(() => window.__studio.getState().setHoveredNode('fix'))
  await page.waitForTimeout(100)
  check('右栏悬停某一步：对应泳道高亮', await count(page, '.tl-lane.is-hover[data-node-id="fix"]') === 1)
  await page.evaluate(() => window.__studio.getState().setHoveredNode(null))

  // 拖时间轴：游标拖到很早的时候，卡片回到那一刻
  const before = await nodeState(page, 'done')
  const axis = page.locator('.tl-axis .tl-track')
  const box = await axis.boundingBox()
  await page.mouse.move(box.x + box.width * 0.2, box.y + box.height / 2)
  await page.mouse.down()
  await page.mouse.move(box.x + box.width * 0.12, box.y + box.height / 2, { steps: 4 })
  await page.mouse.up()
  await page.waitForTimeout(300)
  const replayAt = await page.evaluate(() => window.__studio.getState().replayAt)
  const after = await nodeState(page, 'done')
  const teamAt = await nodeState(page, 'team')
  check('拖游标进入回放', replayAt != null)
  check('卡片状态随游标变（出具：完成 → 那一刻还没开始）', before === 'done' && after !== 'done', `${before} → ${after}`)
  check('那一刻协作团队还在跑', teamAt === 'running', String(teamAt))
  check('回放时胶囊说明在回放', (await capText(page)).includes('回放'), await capText(page))
  const replayEdge = await edgeClass(page, 'e-retry-done')
  check('回放时边也回到那一刻（出具的入边还没走）', !replayEdge.includes('sf-e-walked'), replayEdge)

  // 游标之后压暗的那层不能盖住游标头：泳道滚上去之后也不能
  const head = await page.evaluate(() => {
    const layers = [...document.querySelectorAll('.tl-future, .tl-cursor, .tl-fold')]
    layers.forEach((el) => { el.style.pointerEvents = 'auto' })
    const probe = () => {
      const h = document.querySelector('.tl-cursor-head').getBoundingClientRect()
      return [h.left + 3, h.left + h.width / 2, h.right - 3]
        .every((x) => document.elementFromPoint(x, h.top + h.height / 2)?.closest('.tl-cursor-head'))
    }
    const top = probe()
    const sc = document.querySelector('.tl-scroll')
    sc.scrollTop = 60
    const scrolled = probe()
    const scrollable = sc.scrollTop > 0
    sc.scrollTop = 0
    layers.forEach((el) => { el.style.pointerEvents = '' })
    return { top, scrolled, scrollable }
  })
  check('回放：游标头整块露在最上面（压暗、竖线都不盖它）', head.top && head.scrolled, JSON.stringify(head))

  // 坞里的中文不小于 11px（只有纯数字的刻度用 10.5px）
  const sizes = await page.evaluate(() => {
    const px = (el) => parseFloat(getComputedStyle(el).fontSize)
    const fold = document.createElement('span')
    fold.className = 'tl-fold-tag'
    document.querySelector('.tl-axis .tl-track').appendChild(fold)
    const out = {
      bar: Math.min(...[...document.querySelectorAll('.tl-bar em')].map(px)),
      head: px(document.querySelector('.tl-cursor-head')),
      fold: px(fold),
    }
    fold.remove()
    return out
  })
  check('段上的字、折叠标签、游标头都不小于 11px', sizes.bar >= 11 && sizes.head >= 11 && sizes.fold >= 11,
    JSON.stringify(sizes))

  await page.locator('.tl-seg').getByRole('button', { name: /实时/ }).click()
  await page.waitForTimeout(200)
  check('切回实时', await page.evaluate(() => window.__studio.getState().replayAt) === null)
  check('回到实时后卡片落回终态', await nodeState(page, 'done') === 'done')

  // 只看执行路径：没走的节点和边压暗。开着它去清除，看下一次运行会不会沿用
  await page.locator('.tl-toggle', { hasText: '只看执行路径' }).click()
  await page.waitForTimeout(200)
  check('只看执行路径：没走的节点压暗', await count(page, '.react-flow__node.sf-offpath') === 1,
    `${await count(page, '.react-flow__node.sf-offpath')} 个`)

  // Esc 只认画布这一片：焦点在右栏的步骤上时按 Esc 不该清掉整次运行
  const inPanel = await page.evaluate(() => {
    const row = [...document.querySelectorAll('[data-node-id]')].find((el) => !el.closest('.react-flow, .tl'))
    const btn = row?.matches('button') ? row : row?.querySelector('button')
    btn?.focus()
    return !!btn && document.activeElement === btn
  })
  await page.keyboard.press('Escape')
  await page.waitForTimeout(200)
  check('焦点在右栏时 Esc 不清结果', inPanel && await page.evaluate(() => window.__studio.getState().runPhase) === 'succeeded',
    inPanel ? '' : '右栏没找到带 data-node-id 的步骤行')

  // Esc 清除结果，回到编辑态
  await page.locator('.react-flow__pane').click({ position: { x: 20, y: 300 } })
  await page.keyboard.press('Escape')
  await page.waitForTimeout(300)
  check('Esc 清除结果回到编辑态', await page.evaluate(() => window.__studio.getState().runPhase) === 'idle')
  check('清除后航迹坞收起', await count(page, '.tl') === 0)
  check('清除后没有运行痕迹的边', await count(page, '.react-flow__edge.sf-e-walked') === 0)

  // 清除之前「只看执行路径」是开着的：下一次运行不能把还没走到的下游压暗
  await seedRun(page, { id: 'fxrun000002' })
  await feed(page, PARALLEL_NOW)
  await page.waitForTimeout(400)
  const carried = await page.evaluate(() => ({
    nodes: document.querySelectorAll('.react-flow__node.sf-offpath').length,
    edges: document.querySelectorAll('.react-flow__edge.sf-e-offpath').length,
    attr: document.querySelector('.react-flow')?.dataset.pathOnly ?? null,
  }))
  check('「只看执行路径」不带进下一次运行', carried.nodes === 0 && carried.edges === 0 && carried.attr === null,
    JSON.stringify(carried))
  await feed(page, [ev(9, 'run.cancelled', null, { timing: { wall_ms: 44000, active_ms: 44000, wait_ms: 0 } }, 44)])
  await page.waitForTimeout(300)
  const toggle = page.getByRole('checkbox', { name: '只看执行路径' })
  check('那次运行结束后开关是关着的', await toggle.count() === 1 && !(await toggle.isChecked()))
  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))
  await browser.close()
}

console.log('=== 补发的历史（attachRun 回放）：直接落终态，不播时刻 ===')
{
  const { browser, page } = await openStudio()
  await seedRun(page)
  const replayed = [...PARALLEL, ...TEAM_DONE, ...TO_LOOP].map((e) => ({ ...e, replay: true }))
  await feed(page, replayed.slice(0, PARALLEL.length))
  await page.waitForTimeout(1100)
  await feed(page, replayed.slice(PARALLEL.length))
  await page.waitForTimeout(150)
  const seen = await page.evaluate(() => ({
    moment: document.querySelector('.react-flow')?.dataset.moment ?? null, sweeps: document.querySelectorAll('.sf-sweep').length,
    phase: document.querySelector('.react-flow')?.dataset.runPhase,
  }))
  check('历史事件不触发一次性时刻', seen.moment === null && seen.sweeps === 0, JSON.stringify(seen))
  check('历史事件照样落到终态', seen.phase === 'succeeded', seen.phase)
  await browser.close()
}

// ---------------------------------------------------------------- 正式运行

console.log('=== 正式运行：入口看 published_version；运行期间画布只读 ===')
{
  const { browser, page } = await openStudio()
  // 保存过一次（status 退回草稿）且画布有未保存改动：正式运行入口照样在
  await page.evaluate(() => window.__studio.setState({ dirty: true }))
  const formal = page.getByRole('button', { name: /正式运行 v2/ })
  check('草稿状态下正式运行入口还在（看 published_version）', await formal.count() === 1)
  check('画布有未保存改动时也能点', await formal.isEnabled())
  await formal.click()
  await page.waitForSelector('.sf-pop', { timeout: 5000 })
  await page.waitForTimeout(400)
  const pop = (await page.locator('.sf-pop').innerText()).replace(/\s+/g, ' ')
  check('提示不含画布改动', pop.includes('不含画布改动'), pop.slice(0, 80))
  check('表单字段取已发布版本的（topic），不取画布的（q）',
    await page.locator('.sf-pop textarea#run-field-topic').count() === 1
    && await page.locator('.sf-pop textarea#run-field-q').count() === 0)
  await page.keyboard.press('Escape')

  await page.evaluate(() => window.__studio.setState({ dirty: false }))
  await seedRun(page, { run_class: 'formal', version: 2 })
  await feed(page, PARALLEL_NOW)
  await page.waitForTimeout(400)
  check('只读横幅出现', (await page.locator('.sf-banner').innerText().catch(() => '')).includes('画布只读'))
  const ro = await page.evaluate(() => document.querySelector('.react-flow')?.dataset)
  check('根元素标了正式运行和只读', ro?.runClass === 'formal' && ro?.readonly === '1', JSON.stringify(ro))

  const pos = () => page.evaluate(() => window.__studio.getState().nodes.find((n) => n.id === 'gate').position)
  const p0 = await pos()
  const card = await page.locator('.react-flow__node[data-id="gate"]').boundingBox()
  await page.mouse.move(card.x + 40, card.y + 14)
  await page.mouse.down()
  await page.mouse.move(card.x + 160, card.y + 120, { steps: 6 })
  await page.mouse.up()
  await page.waitForTimeout(200)
  const p1 = await pos()
  check('正式运行期间拖拽被禁', p0.x === p1.x && p0.y === p1.y, `${JSON.stringify(p0)} → ${JSON.stringify(p1)}`)
  await browser.close()
}

for (const width of [1280, 1024]) {
  console.log(`=== 正式运行 @${width}：只读横幅不盖住跟随那一枚 ===`)
  const { browser, page } = await openStudio({ viewport: { width, height: 860 } })
  await seedRun(page, { run_class: 'formal', version: 2 })
  await feed(page, PARALLEL_NOW)
  await page.waitForTimeout(400)
  // 手动拖一下画布，让跟随那一枚变成「已暂停跟随 · 恢复」：恢复得点得到
  const spot = await emptySpot(page)
  await page.mouse.move(spot.x, spot.y)
  await page.mouse.down()
  await page.mouse.move(spot.x + 60, spot.y + 30, { steps: 4 })
  await page.mouse.up()
  await page.waitForTimeout(250)
  const geo = await page.evaluate(() => {
    const r = (sel) => document.querySelector(sel)?.getBoundingClientRect()
    const a = r('.sf-follow')
    const b = r('.sf-banner')
    const btn = document.querySelector('.sf-follow button')
    const br = btn?.getBoundingClientRect()
    const hit = br ? document.elementFromPoint(br.left + br.width / 2, br.top + br.height / 2) : null
    const overlap = !!a && !!b && a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom
    return { overlap, resume: !!btn && hit === btn, follow: a && [Math.round(a.left), Math.round(a.right)],
      banner: b && [Math.round(b.left), Math.round(b.right), Math.round(b.top)] }
  })
  check('横幅和跟随那一枚不重叠', !geo.overlap && !!geo.banner && !!geo.follow, JSON.stringify(geo))
  check('「恢复」点得到', geo.resume)
  await browser.close()
}

// ---------------------------------------------------------------- 取景

console.log('=== 打开宽图：以入口为左锚，缩放不低于可读下限 ===')
{
  const { browser, page, errors } = await openStudio({ id: WIDE_ID })
  await page.waitForTimeout(600)
  const view = await page.evaluate(() => {
    const vp = document.querySelector('.react-flow__viewport')
    const m = /matrix\(([^)]+)\)|scale\(([^)]+)\)/.exec(getComputedStyle(vp).transform)
    const zoom = m ? Number((m[1] ?? m[2]).split(',')[0]) : NaN
    const pane = document.querySelector('.react-flow').getBoundingClientRect()
    const entry = document.querySelector('.react-flow__node[data-id="n0"]').getBoundingClientRect()
    return { zoom, entryLeft: entry.left - pane.left, entryRight: entry.right - pane.left, paneW: pane.width }
  })
  check('缩放不低于 0.6', view.zoom >= 0.6 - 1e-6, `zoom=${view.zoom.toFixed(3)}`)
  check('入口在视口左侧、完整可见', view.entryLeft >= 0 && view.entryRight < view.paneW / 2,
    `left=${view.entryLeft.toFixed(0)} right=${view.entryRight.toFixed(0)} / ${view.paneW.toFixed(0)}`)
  check('没有常驻的航迹坞（没有运行）', await count(page, '.tl') === 0)
  const zoomText = await page.locator('.sf-zoom-btn').innerText()
  check('控件里有缩放读数', zoomText.trim() === `${Math.round(view.zoom * 100)}%`, zoomText)
  check('控件是中文的（适配全图）', await page.getByRole('button', { name: '适配全图' }).count() === 1)

  // 跟随执行：视口外的节点开跑时镜头平移过去；人手动拖过画布之后让出 10 秒
  const inView = (id) => page.evaluate((nid) => {
    const pane = document.querySelector('.react-flow').getBoundingClientRect()
    const r = document.querySelector(`.react-flow__node[data-id="${nid}"]`).getBoundingClientRect()
    return r.left >= pane.left && r.right <= pane.right && r.top >= pane.top && r.bottom <= pane.bottom
  }, id)
  const vp = () => page.evaluate(() => document.querySelector('.react-flow__viewport').style.transform)
  const t0 = Date.now() / 1000
  const wev = (seq, type, node_id, dt) => ({ seq, type, node_id, data: {}, ts: t0 + dt })
  await seedRun(page, { workflow_id: WIDE_ID })
  await feed(page, [wev(1, 'run.started', null, 0), wev(2, 'node.started', 'n0', 0.1), wev(3, 'node.finished', 'n0', 0.2)])
  await page.waitForTimeout(300)
  check('n9 起初在视口外', !(await inView('n9')))
  await feed(page, [wev(4, 'node.started', 'n9', 0.3)])
  await page.waitForTimeout(700)
  check('跟随执行：视口外的节点开跑，镜头跟过去', await inView('n9'))
  const pane = await page.locator('.react-flow__pane').boundingBox()
  await page.mouse.move(pane.x + 300, pane.y + 60)
  await page.mouse.down()
  await page.mouse.move(pane.x + 500, pane.y + 90, { steps: 5 })
  await page.mouse.up()
  await page.waitForTimeout(200)
  const paused = await vp()
  check('手动拖过画布：显示「已暂停跟随」', (await page.locator('.sf-follow').innerText()).includes('已暂停跟随'))
  await feed(page, [wev(5, 'node.finished', 'n9', 0.4), wev(6, 'node.started', 'n2', 0.5)])
  await page.waitForTimeout(700)
  check('暂停期间不再抢镜头', await vp() === paused)
  // 点名取景：已暂停也照做（是人要看它）
  await page.evaluate(() => window.__studio.getState().focusNode('n11'))
  await page.waitForTimeout(700)
  check('focusNode：画布取景到点名的节点', await inView('n11'))
  await page.evaluate(() => window.__studio.getState().clearRun())
  await page.waitForTimeout(200)

  // 缩小到远景：语义缩放换档
  for (let i = 0; i < 6; i++) await page.locator('.react-flow__controls-zoomout').click()
  await page.waitForTimeout(400)
  const lod = await page.evaluate(() => document.querySelector('.react-flow')?.dataset.lod)
  check('缩小后 LOD 换到 compact / signal', lod === 'compact' || lod === 'signal', String(lod))
  await page.locator('.sf-zoom-btn').click()
  await page.waitForTimeout(500)
  check('点缩放读数回到 100%', (await page.locator('.sf-zoom-btn').innerText()).trim() === '100%'
    && await page.evaluate(() => document.querySelector('.react-flow')?.dataset.lod) === 'full')
  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))
  await browser.close()
}

// ---------------------------------------------------------------- 关掉动效

console.log('=== 系统关了动效 ===')
{
  const { browser, page } = await openStudio({ reducedMotion: 'reduce' })
  await seedRun(page)
  await feed(page, PARALLEL_NOW)
  await page.waitForTimeout(400)
  const styles = await page.evaluate(() => {
    const el = document.querySelector('.fx-halo')
    const packet = document.querySelector('.edge-packet')
    return {
      matches: matchMedia('(prefers-reduced-motion: reduce)').matches,
      halo: el ? getComputedStyle(el).display : '不在 DOM 里',
      packet: packet ? getComputedStyle(packet).display : '不在 DOM 里',
    }
  })
  check('浏览器确实处在减少动效模式', styles.matches)
  check('光弧不再绘制', styles.halo === 'none' || styles.halo === '不在 DOM 里', styles.halo)
  check('光点不再绘制', styles.packet === 'none', styles.packet)
  // 状态本身还得看得出来：走过 / 正在流的边是实线，胶囊照样计时
  check('静态下边的运行态还在', (await edgeClass(page, 'e-start-team')).includes('sf-e-flow'))
  check('静态下胶囊照样写着运行中', (await capText(page)).includes('运行中'))

  // 「适配全图」也不做动画：点完下一帧就到位
  const vp = () => page.evaluate(() => document.querySelector('.react-flow__viewport').style.transform)
  const pane = await page.locator('.react-flow__pane').boundingBox()
  await page.mouse.move(pane.x + 300, pane.y + 300)
  await page.mouse.down()
  await page.mouse.move(pane.x + 80, pane.y + 200, { steps: 4 })
  await page.mouse.up()
  await page.waitForTimeout(200)
  const moved = await vp()
  await page.getByRole('button', { name: '适配全图' }).click()
  await page.waitForTimeout(40)
  const soon = await vp()
  await page.waitForTimeout(400)
  const later = await vp()
  check('减少动效时「适配全图」直接到位', soon !== moved && soon === later, `${moved} → ${soon} → ${later}`)
  await browser.close()
}

console.log(failed ? `\n${failed} 项未通过` : '\n全部通过')
process.exit(failed ? 1 : 0)
