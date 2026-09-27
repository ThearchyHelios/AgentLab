// 节点卡运行态的检查：每种状态 × 三档缩放 × 亮暗两套主题，灌事件、量尺寸、数动画、截图。
//
// 不真跑模型：沙箱没有密钥，而且真跑撞不上"三个人同时在跑、循环跑到第 3 轮、
// 另一支停在审批上"这种同一瞬间。dev 构建把 store 挂在 window.__studio 上，这里
// 直接往 applyEvent 里灌事件——走的是页面上那一份航迹 / NodeCard，只有事件来源
// 是脚本。工作流、审批、校验这些接口全部用 page.route 伪造，不写任何库。
//
// 守的是这几件事：
// - 运行期间卡片高度不变（流式输出、协作矩阵、遥测都不许把卡片撑高，缩放换档也不许）
// - 等待、完成和各种终态没有常驻动画；系统关了动效时画布上一个动画都没有
// - 每种状态的剪影（StatusBadge 的 data-shape）互不相同：去掉颜色也认得出
// - 遥测槽、协作矩阵、循环进度、出具印章、去审批这些真的长出来了
// - 回放（replayAt）时卡片、矩阵、印章都回到那一刻，不拿终值冒充；排队、阻断
//   这些推导状态和循环容器「两轮之间」也按那一刻算
// - 协作矩阵的表头不和底部打架：并行的一轮交回一部分、全部交回等调度时都不说「串行」
// - 去审批浮层键盘可达；出口密的分支节点标签不压别人的线；精简档读数够大
// - 收场要说人话：协作团队用完轮数（判失败 / 降档交付）写清「用完 N 轮未完成」、点出
//   一次都没被派到的成员，收尾判定不另算一轮；模型把工具调用写成文字的失败写原因，
//   不是半句原话；工具超出后端声明的时限直说「已超出 Ns 上限」；校验靠修复才过要提一句
//
// 跑之前前后端都得起着（./scripts/dev.sh），默认连 5273 / 8000。对别的实例（比如一份
// 沙箱拷贝）跑时带上地址：
//   AGENTLAB_WEB=http://localhost:<前端端口> AGENTLAB_API=http://localhost:<后端端口>/api node scripts/check-run-states.mjs
// 截图落在 RUN_STATES_SHOTS（默认 /tmp/agentlab-run-states）。
import { mkdirSync } from 'node:fs'
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const API = process.env.AGENTLAB_API ?? 'http://localhost:8000/api'
const CHROME = process.env.CHROME_PATH
  ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const SHOTS = process.env.RUN_STATES_SHOTS ?? '/tmp/agentlab-run-states'
// 截图的像素密度：要看清卡片里 11px 的字时设成 2
const DSF = Number(process.env.RUN_STATES_DSF ?? 1) || 1
mkdirSync(SHOTS, { recursive: true })
// 只跑其中几段（逗号分隔：themes, interact, team, endings, replay, history, reduced, exits），调样式时省时间
const ONLY = (process.env.RUN_STATES_ONLY ?? '').split(',').filter(Boolean)
const want = (part) => !ONLY.length || ONLY.includes(part)
/**
 * 一段一段地跑：某一段里等待超时、元素找不到，只记成这一段失败，关掉它开的浏览器，
 * 接着跑下一段——不让一处卡住把后面几百项一起吞掉。段标题在各段自己里面打
 */
const opened = new Set()
async function section(part, fn) {
  if (!want(part)) return
  try {
    await fn()
  } catch (e) {
    check(`「${part}」这一段中途出错`, false, String(e?.message ?? e).split('\n')[0])
  } finally {
    for (const b of opened) await b.close().catch(() => {})
    opened.clear()
  }
}

let failed = 0
const check = (name, cond, detail = '') => {
  console.log(`  ${cond ? '✓' : '✗'} ${name}${detail ? ` — ${detail}` : ''}`)
  if (!cond) failed++
}

// ---------------------------------------------------------------- 图

const agent = (name, description) => ({ name, description, system: name, tools: [] })

const GRAPH = {
  nodes: [
    { id: 'in', type: 'input', data: { label: '工单参数', config: { fields: [{ name: 'line' }, { name: 'week' }] } } },
    { id: 'retr', type: 'retrieve', data: { label: '检索停机记录', config: { collection: 'mes_docs', limit: 20 } } },
    { id: 'sql', type: 'tool', data: { label: 'MES 取数', config: { tool: 'db_query' } } },
    { id: 'skip', type: 'agent', data: { label: '背景检索', config: { prompt: '补充行业背景', skip_if: 'vars.cached' } } },
    { id: 'team', type: 'supervisor', data: { label: '根因分析团队', config: {
      goal: '定位停机根因', max_rounds: 4, max_parallel: 3,
      agents: [agent('数据员', '拉明细'), agent('工艺员', '对工艺参数'), agent('审校', '核对数字')],
    } } },
    { id: 'join', type: 'transform', data: { label: '证据汇总', config: { expression: 'merge(retr, skip)' } } },
    { id: 'loop', type: 'loop', data: { label: '逐线复核', config: { mode: 'foreach', items: 'vars.lines', max_iterations: 10 } } },
    { id: 'body', type: 'llm', data: { label: '单线结论', config: { prompt: '{{ item }} 的停机归因' } } },
    { id: 'gate', type: 'branch', data: { label: '质量门', config: { mode: 'expression', cases: [
      { key: 'pass', condition: 'vars.ok', label: '通过' },
      { key: 'fail', condition: 'not vars.ok', label: '不通过' },
    ] } } },
    { id: 'human', type: 'human', data: { label: '班长审批', config: { mode: 'approve', title: '签发前人工确认' } } },
    { id: 'out', type: 'output', data: { label: '周报出具', config: { contract: true, fields: [{ name: '停机周报' }] } } },
    { id: 'fix', type: 'output', data: { label: '补数工单', config: { fields: [{ name: '缺口清单' }] } } },
  ],
  edges: [
    { source: 'in', target: 'retr' }, { source: 'in', target: 'sql' }, { source: 'in', target: 'skip' },
    { source: 'retr', target: 'team' }, { source: 'retr', target: 'join' }, { source: 'skip', target: 'join' },
    { source: 'sql', target: 'loop' },
    { source: 'loop', target: 'body', sourceHandle: 'body' }, { source: 'body', target: 'loop' },
    { source: 'loop', target: 'gate', sourceHandle: 'done' },
    { source: 'team', target: 'gate' }, { source: 'join', target: 'gate' },
    { source: 'gate', target: 'human', sourceHandle: 'pass' }, { source: 'gate', target: 'fix', sourceHandle: 'fail' },
    { source: 'human', target: 'out', sourceHandle: 'approved' }, { source: 'human', target: 'fix', sourceHandle: 'rejected' },
  ],
}
const NODE_IDS = GRAPH.nodes.map((n) => n.id)
const WF_ID = 'wcard-run-states'
const RUN_ID = 'wcard-run-0001'

// ---------------------------------------------------------------- 事件
//
// 事件的 ts 要贴着"现在"：store 用实时事件里最小的 (本机时间 − ts) 估服务端时钟
// 偏差，ts 若整体落在一分钟前，计时器会被当成时钟偏差吃掉、显示 00:00。所以每个
// 场景最后一条事件的 ts 取灌入那一刻，前面的按间隔往回推。

function script(steps) {
  // steps: [dtSeconds, type, node_id, data]，dt 是距上一条的间隔
  const total = steps.reduce((acc, s) => acc + s[0], 0)
  return (now) => {
    let t = now / 1000 - total
    return steps.map(([dt, type, node_id, data = {}], i) => {
      t += dt
      return { seq: i + 1, type, node_id, data, ts: t }
    })
  }
}

const loopRound = (k, bodyMs, withTokens = false) => [
  [0.1, 'node.started', 'loop', {}],
  [0.05, 'edge.taken', 'loop', { branch: 'body', iteration: k - 1, total: 5, mode: 'foreach' }],
  [0.05, 'node.finished', 'loop', { duration_ms: 3, preview: { __decision__: 'body' } }],
  [0.1, 'node.started', 'body', { iteration: k }],
  ...(withTokens ? [] : [
    [bodyMs / 1000, 'llm.end', 'body', { model: 'claude-sonnet', input_tokens: 820, output_tokens: 240, cost_usd: 0.002 }],
    [0.02, 'node.finished', 'body', { duration_ms: bodyMs, preview: { text: '换模具等待' } }],
  ]),
]

const HEAD = [
  [0, 'run.started', null, { nodes: NODE_IDS.length }],
  [0.2, 'node.started', 'in', {}],
  [0.1, 'node.finished', 'in', { duration_ms: 4, preview: { line: 'L3' } }],
  [0.05, 'node.skipped', 'skip', { reason: 'skip_if 成立：vars.cached 为真' }],
  [0.05, 'node.started', 'retr', {}],
  [0.02, 'node.started', 'sql', {}],
  [0.05, 'tool.start', 'sql', { tool: 'db_query', args: { sql: 'select …' } }],
  [1.8, 'tool.end', 'sql', { tool: 'db_query', preview: '{"row_count": 128}' }],
  [0.05, 'node.finished', 'sql', { duration_ms: 1920, preview: { rows: new Array(5).fill(0) } }],
  [2.4, 'node.finished', 'retr', { duration_ms: 4300, preview: { hits: new Array(20).fill(0) } }],
]

const TEAM_ROUND0 = [
  [0.1, 'node.started', 'team', {}],
  [0.05, 'agent.route.start', 'team', { round: 0 }],
  [3.2, 'agent.route.end', 'team', { round: 0, duration_ms: 3200, agents: ['数据员', '工艺员', '审校'], parallel: 3, done: false, reason: '三个方向互不依赖' }],
  [0.02, 'log', 'team', { level: 'info', round: 0, agents: ['数据员', '工艺员', '审校'], done: false, parallel: 3, reason: '三个方向互不依赖', message: '调度 → 数据员、工艺员、审校' }],
  [0.02, 'agent.step.start', 'team', { agent: '数据员', instruction: '拉 L3 本周停机明细', round: 0, parallel: 3 }],
  [0.02, 'agent.step.start', 'team', { agent: '工艺员', instruction: '对照换模工艺参数', round: 0, parallel: 3 }],
  [0.02, 'agent.step.start', 'team', { agent: '审校', instruction: '核对口径', round: 0, parallel: 3 }],
  [0.3, 'tool.start', 'team', { tool: 'db_query', agent: '数据员' }],
]

/** A · 进行中：并行、循环第 3 项、流式输出、排队、跳过、未运行 */
const LIVE = script([
  ...HEAD,
  ...TEAM_ROUND0,
  [2.2, 'agent.step.end', 'team', { agent: '审校', duration_ms: 2500, round: 0, parallel: 3, preview: '口径一致' }],
  ...loopRound(1, 1400), ...loopRound(2, 1600), ...loopRound(3, 0, true),
  [0.4, 'llm.token', 'body', { delta: 'L3 线本周停机 14 次，其中换模等待占 9 次，平均 18 分钟；' }],
  [0.2, 'llm.token', 'body', { delta: '与上周相比换模时长上升 22%，主要集中在夜班……' }],
])

/** B · 等待审批：上游都完成，人工审批停着 */
const TEAM_DONE = [
  [4.1, 'agent.step.end', 'team', { agent: '审校', duration_ms: 4100, round: 0, parallel: 3, preview: '口径一致' }],
  [3.0, 'agent.step.end', 'team', { agent: '工艺员', duration_ms: 7100, round: 0, parallel: 3, preview: '换模参数偏高' }],
  [1.9, 'agent.step.end', 'team', { agent: '数据员', duration_ms: 9000, round: 0, parallel: 3, preview: '128 行' }],
  [0.05, 'tool.end', 'team', { tool: 'db_query', agent: '数据员', preview: 'ok' }],
  [0.05, 'agent.route.start', 'team', { round: 1 }],
  [2.1, 'agent.route.end', 'team', { round: 1, duration_ms: 2100, agents: [], parallel: 0, done: true, reason: '证据足够' }],
  [0.02, 'log', 'team', { level: 'info', round: 1, agents: [], done: true, reason: '证据足够', message: '调度 → 结束协作' }],
  [0.1, 'llm.end', 'team', { agent: '调度者', model: 'claude-sonnet', input_tokens: 5200, output_tokens: 900, cost_usd: 0.02 }],
  [0.05, 'node.finished', 'team', { duration_ms: 17500, preview: { text: '根因：换模等待' } }],
]
const LOOP_DONE = [
  ...loopRound(1, 1400), ...loopRound(2, 1600), ...loopRound(3, 1500), ...loopRound(4, 1300), ...loopRound(5, 1700),
  [0.1, 'node.started', 'loop', {}],
  [0.05, 'edge.taken', 'loop', { branch: 'done', iteration: 5, total: 5, mode: 'foreach' }],
  [0.05, 'node.finished', 'loop', { duration_ms: 2, preview: { __decision__: 'done' } }],
]
const TO_GATE = [
  [0.1, 'node.started', 'join', {}],
  [0.1, 'node.finished', 'join', { duration_ms: 12, preview: { merged: true } }],
  [0.1, 'node.started', 'gate', {}],
  [0.05, 'edge.taken', 'gate', { branch: 'pass', reason: '命中条件：vars.ok' }],
  [0.05, 'node.finished', 'gate', { duration_ms: 3, preview: { __decision__: 'pass' } }],
  [0.1, 'node.started', 'human', {}],
  [0.05, 'human.requested', 'human', { node_id: 'human', title: '签发前人工确认', mode: 'approve' }],
  [0.05, 'run.interrupted', 'human', { payload: { node_id: 'human', title: '签发前人工确认' } }],
]
const WAITING = script([...HEAD, ...TEAM_ROUND0, ...TEAM_DONE, ...LOOP_DONE, ...TO_GATE, [134, 'log', null, { level: 'info', message: '心跳' }]])

/** C · 失败：团队里一个成员报错，节点失败；并行的循环被收成取消，下游阻断 */
const FAILED = script([
  ...HEAD, ...TEAM_ROUND0,
  [2.2, 'agent.step.end', 'team', { agent: '审校', duration_ms: 2500, round: 0, parallel: 3, preview: '口径一致' }],
  ...loopRound(1, 1400), ...loopRound(2, 0, true),
  [0.6, 'log', 'team', { level: 'warn', code: 'node_retry', message: '重试' }],
  [4.0, 'agent.step.end', 'team', { agent: '工艺员', duration_ms: 7200, round: 0, parallel: 3, error: 'timeout', preview: '' }],
  [0.1, 'node.failed', 'team', { duration_ms: 12400, error: '工艺员 30 秒没有回应，模型调用超时。可以调大超时后从这里接着跑' }],
  [0.1, 'run.failed', null, { error: '「根因分析团队」失败', node_id: 'team', timing: { wall_ms: 16000, active_ms: 16000, wait_ms: 0 } }],
])

/** D · 成功：全程完成，没走的一侧未到达，成果节点落出具印章 */
const SUCCEEDED = script([
  ...HEAD, ...TEAM_ROUND0, ...TEAM_DONE, ...LOOP_DONE, ...TO_GATE,
  [3, 'human.resolved', 'human', { approved: true, actor: '李工' }],
  [0.05, 'run.resumed', null, { actor: '李工' }],
  [0.05, 'node.started', 'human', { resumed: true }],
  [0.05, 'edge.taken', 'human', { branch: 'approved' }],
  [0.05, 'node.finished', 'human', { duration_ms: 4, preview: { __decision__: 'approved' } }],
  [0.1, 'node.started', 'out', {}],
  [0.1, 'issuance', 'out', { tier: 'formal', missing_required: [], missing_expected: [], unmatched: 0, metrics_checked: 3, matched_numbers: 7 }],
  [0.05, 'node.finished', 'out', { duration_ms: 310, preview: { 停机周报: '…' } }],
  [0.05, 'run.finished', null, { output: {}, usage: { input_tokens: 21000, output_tokens: 3200, cost_usd: 0.05 }, duration_ms: 40000, timing: { wall_ms: 180000, active_ms: 40000, wait_ms: 140000 } }],
])

/** E · 取消 / 服务重启挂起 */
const CANCELLED = script([...HEAD, ...TEAM_ROUND0, ...loopRound(1, 1400), ...loopRound(2, 0, true),
  [1.5, 'run.cancelled', null, { timing: { wall_ms: 12000, active_ms: 12000, wait_ms: 0 } }]])
const SUSPENDED = script([...HEAD, ...TEAM_ROUND0, ...loopRound(1, 1400), ...loopRound(2, 0, true),
  [1.5, 'log', null, { level: 'warn', code: 'server_shutdown', message: '服务重启' }]])

// ---------------------------------------------------------------- 页面

async function layout() {
  const res = await fetch(`${API}/copilot/layout`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ graph: GRAPH }),
  })
  if (!res.ok) throw new Error(`排版接口 ${res.status}`)
  return res.json()
}

const LAID = await layout()
const WORKFLOW = {
  id: WF_ID, name: '__run_states__', description: '', graph: LAID, tags: [], version: 1,
  is_template: false, status: 'draft',
}
const APPROVAL = {
  id: 'wcard-approval', run_id: RUN_ID, node_id: 'human', mode: 'approve', title: '签发前人工确认',
  payload: { message: '本周 L3 停机周报：换模等待是主因，建议夜班加一名换模工。' }, status: 'pending', response: {},
  created_at: new Date().toISOString(), workflow_name: '__run_states__', node_label: '班长审批', run_status: 'interrupted',
}

let issues = []
// 开发服务器热更新会把页面整个重载，或者只把 store 模块换掉（状态清空、页面
// 不导航），之后的断言全部落空。数一下，失败时好分清是卡片的问题还是别处恰好
// 在改代码
let reloads = 0
const lost = async (page) => {
  const ok = await page.evaluate((id) => window.__studio?.getState().run?.id === id, RUN_ID).catch(() => false)
  if (!ok) reloads++
  return !ok
}

async function open({ theme = 'dark', reduced = false, workflow = WORKFLOW } = {}) {
  const browser = await chromium.launch({ executablePath: CHROME })
  opened.add(browser)
  const ctx = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: DSF,
    colorScheme: theme,
    reducedMotion: reduced ? 'reduce' : 'no-preference',
  })
  await ctx.addInitScript((t) => { try { localStorage.setItem('agentlab.theme', t) } catch { /* 无痕窗口 */ } }, theme)
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message))
  page.on('console', (m) => { if (m.type() === 'error' && !/404|Failed to load resource/.test(m.text())) errors.push(m.text()) })

  // 主题：设置接口里存的偏好会在启动时覆盖本地的，这里把它改成要测的那一套
  await page.route('**/api/settings', async (route) => {
    if (route.request().method() !== 'GET') return route.continue()
    const res = await route.fetch()
    const body = await res.json().catch(() => ({}))
    await route.fulfill({ response: res, json: { ...body, ui: { ...(body.ui ?? {}), theme } } })
  })
  await page.route(`**/api/workflows/${workflow.id}`, (route) => route.fulfill({ json: workflow }))
  await page.route('**/api/workflows/validate', (route) => route.fulfill({ json: { ok: !issues.length, issues } }))
  await page.route('**/api/workflows/variables', (route) => route.fulfill({ json: { variables: [], issues: [] } }))
  await page.route('**/api/conversations**', (route) => route.fulfill({
    json: route.request().method() === 'GET' ? [] : { id: 'wcard-conv', kind: 'canvas', title: '', turns: [] },
  }))
  await page.route('**/api/approvals**', (route) => route.fulfill({ json: [APPROVAL] }))

  await page.goto(`${WEB}/studio/${workflow.id}`, { waitUntil: 'networkidle' })
  page.on('framenavigated', (f) => { if (f === page.mainFrame()) reloads++ })
  await page.waitForFunction((n) => document.querySelectorAll('.react-flow__node').length === n, workflow.graph.nodes.length)
  await page.waitForTimeout(700)
  await fit(page)
  return { browser, page, errors }
}

async function fit(page) {
  await page.locator('.react-flow__controls-fitview').click()
  await page.waitForTimeout(350)
}

async function zoomOf(page) {
  // 页面刚被热更新重载时画布还没挂上：等它回来，别在这里崩掉、把重载提示也吞了
  await page.waitForSelector('.react-flow__viewport', { timeout: 15000 })
  return page.evaluate(() => {
    const m = getComputedStyle(document.querySelector('.react-flow__viewport')).transform
    return m && m !== 'none' ? new DOMMatrix(m).a : 1
  })
}

/**
 * 缩放到某一档。档位由画布（FlowCanvas）按缩放写在 .react-flow 上；还没写的话
 * 这里按同一套阈值补上，好让卡片这一侧的检查不依赖画布那边的进度。
 */
async function setLod(page, lod) {
  await fit(page)
  const target = { full: 0.62, compact: 0.5, signal: 0.3 }[lod]
  for (let i = 0; i < 12; i++) {
    const z = await zoomOf(page)
    if (lod === 'full' ? z >= 0.6 : lod === 'compact' ? z < 0.58 && z >= 0.37 : z < 0.33) break
    await page.locator(z > target ? '.react-flow__controls-zoomout' : '.react-flow__controls-zoomin').click()
    await page.waitForTimeout(220)
  }
  await park(page)
  const z = await zoomOf(page)
  const own = await page.evaluate(({ lod, z }) => {
    const root = document.querySelector('.react-flow')
    if (root.dataset.lod && root.dataset.lodBy !== 'check') return root.dataset.lod
    root.dataset.lod = lod
    root.dataset.lodBy = 'check'
    root.style.setProperty('--zoom', String(z))
    return null
  }, { lod, z })
  await page.waitForTimeout(150)
  return { zoom: z, lod: own ?? lod, byCanvas: own != null }
}

/**
 * 鼠标挪到画布顶上的空白处。点完左下角的缩放按钮，指针正停在时间轴上，
 * 时间轴会把悬停的那一行当成「右栏悬停」，给某张卡挂上聚焦环
 */
async function park(page) {
  const pane = await page.locator('.react-flow').boundingBox()
  await page.mouse.move(pane.x + pane.width / 2, pane.y + 24)
}

/**
 * 精简档的主读数。它只在 compact 档露出来，别的档是 visibility:hidden——innerText 这时取到
 * 空串。LOD 切换、换数据后的重画都可能晚一拍：等它露出来、有字（给了 ready 就等 ready 成立），
 * 最多 3 秒，到点就按当时读到的算（真回归照样判错，只是不再因为早读了一拍取到空文本）
 */
async function lodRead(page, id, ready = (t) => t !== '') {
  const sel = `.react-flow__node[data-id="${id}"] .nc-lod-read`
  const read = () => page.evaluate((q) => {
    const el = document.querySelector(q)
    return el && getComputedStyle(el).visibility === 'visible' ? el.innerText.trim() : ''
  }, sel)
  const end = Date.now() + 3000
  for (;;) {
    const text = await read().catch(() => '')
    if (ready(text) || Date.now() > end) return text
    await page.waitForTimeout(80)
  }
}

async function feed(page, events, extra = {}) {
  await page.evaluate(({ events, runId, extra }) => {
    const st = window.__studio
    st.getState().clearRun()
    st.setState({
      run: { id: runId, workflow_id: 'wcard-run-states', workflow_name: '__run_states__', status: 'running',
             input: {}, output: extra.output ?? {}, error: null, usage: {}, run_class: extra.runClass ?? 'exploratory' },
      runSnapshot: null,
    })
    for (const e of events) st.getState().applyEvent(extra.replay ? { ...e, replay: true } : e)
  }, { events, runId: RUN_ID, extra })
}

/** 每张卡：状态、剪影、高度（offsetHeight 不受缩放影响） */
const cards = (page) => page.evaluate(() => Object.fromEntries(
  [...document.querySelectorAll('.react-flow__node')].map((el) => {
    const card = el.querySelector('.nc')
    const badge = card?.querySelector('.nc-head [data-shape]')
    return [el.dataset.id, {
      state: card?.dataset.state,
      shape: badge?.getAttribute('data-shape') ?? null,
      h: card?.offsetHeight ?? 0,
      text: card?.querySelector('.nc-tele')?.innerText ?? '',
    }]
  })))

/**
 * 画布里还在跑的动画，按所在卡片的状态归类。getAnimations 会连已经播完、
 * 停在最后一帧的也给出来，只算 playState 为 running 的
 */
const runningAnimations = (page, { transitions = true } = {}) => page.evaluate((transitions) => {
  const out = {}
  const names = new Set()
  for (const a of document.getAnimations()) {
    if (a.playState !== 'running') continue
    if (!transitions && a instanceof CSSTransition) continue
    const el = a.effect?.target
    if (!el || !el.closest?.('.react-flow')) continue
    const card = el.closest('.nc')
    const key = card ? card.dataset.state : el.closest('.react-flow__edge') ? 'edge' : 'other'
    out[key] = (out[key] ?? 0) + 1
    if (card) names.add(a.animationName ?? a.transitionProperty)
  }
  return { ...out, names: [...names] }
}, transitions)
const countOf = (anim) => Object.entries(anim).reduce((acc, [k, v]) => (k === 'names' ? acc : acc + v), 0)

async function shot(page, name) {
  const path = `${SHOTS}/${name}.png`
  await page.locator('.react-flow').screenshot({ path })
  return path
}

/** 只截几张卡（外扩一圈）：看卡片里的字用，整张画布截图里它们太小 */
async function cardShot(page, ids, name) {
  const boxes = (await Promise.all(ids.map((id) => page.locator(`.react-flow__node[data-id="${id}"]`).boundingBox())))
    .filter(Boolean)
  if (!boxes.length) return null
  const x = Math.min(...boxes.map((b) => b.x)) - 16
  const y = Math.min(...boxes.map((b) => b.y)) - 16
  const w = Math.max(...boxes.map((b) => b.x + b.width)) + 16 - x
  const h = Math.max(...boxes.map((b) => b.y + b.height)) + 16 - y
  const path = `${SHOTS}/${name}.png`
  await page.screenshot({ path, clip: { x: Math.max(0, x), y: Math.max(0, y), width: w, height: h } })
  return path
}

const SCENARIOS = [
  { key: 'live', events: LIVE, expect: { team: 'running', loop: 'running', body: 'running', join: 'queued', skip: 'skipped', gate: 'idle', in: 'done' } },
  { key: 'waiting', events: WAITING, expect: { human: 'waiting', body: 'done', loop: 'done', team: 'done', out: 'idle' } },
  { key: 'failed', events: FAILED, expect: { team: 'failed', gate: 'blocked', out: 'blocked', body: 'cancelled' } },
  { key: 'succeeded', events: SUCCEEDED, expect: { out: 'done', fix: 'unreached', human: 'done' }, extra: { runClass: 'formal' } },
  { key: 'cancelled', events: CANCELLED, expect: { team: 'cancelled', body: 'cancelled' } },
  { key: 'suspended', events: SUSPENDED, expect: { team: 'suspended', body: 'suspended' } },
]

// ---------------------------------------------------------------- 亮暗两套

const shapesByState = {}
const shots = []

for (const theme of want('themes') ? ['dark', 'light'] : []) await section('themes', async () => {
  console.log(`\n=== ${theme === 'dark' ? '暗色' : '亮色'}主题 ===`)
  const { browser, page, errors } = await open({ theme })

  await setLod(page, 'full')
  const idle = await cards(page)
  const baseH = Object.fromEntries(Object.entries(idle).map(([id, c]) => [id, c.h]))
  check('编辑态每张卡都画出来了', Object.values(idle).every((c) => c.h > 60), JSON.stringify(baseH))
  check('编辑态没有状态剪影（正常态安静）', Object.values(idle).every((c) => !c.shape))

  // 校验标记：error 和 warning 都上卡
  issues = [
    { level: 'error', node_id: 'body', message: '引用了不存在的变量 item.name' },
    { level: 'warning', node_id: 'join', message: '产出者排在后面，这里取到空值' },
  ]
  await page.evaluate((iss) => window.__studio.setState({ issues: iss }), issues)
  await page.waitForTimeout(150)
  const marks = await page.evaluate(() => ({
    error: document.querySelector('.react-flow__node[data-id="body"] .nc-issue .is-error')?.textContent,
    warning: document.querySelector('.react-flow__node[data-id="join"] .nc-issue .is-warning')?.textContent,
    slot: document.querySelector('.react-flow__node[data-id="join"] .nc-tele')?.innerText,
  }))
  check('error 角标上卡', !!marks.error, marks.error)
  check('warning 角标同样上卡', !!marks.warning, marks.warning)
  check('编辑态遥测槽写出问题原文', (marks.slot ?? '').includes('取到空值'), marks.slot)
  shots.push(await shot(page, `${theme}-edit-issues`))
  issues = []
  await page.evaluate(() => window.__studio.setState({ issues: [] }))

  for (const sc of SCENARIOS) {
    await setLod(page, 'full')
    await feed(page, sc.events(Date.now()), sc.extra)
    await page.waitForTimeout(1300)  // 等一次性的抖动、扫光播完
    const now = await cards(page)
    for (const [id, want] of Object.entries(sc.expect)) {
      check(`${sc.key}: ${id} 是 ${want}`, now[id]?.state === want, now[id]?.state)
    }
    for (const c of Object.values(now)) {
      if (c.state && c.shape) (shapesByState[c.state] ??= new Set()).add(c.shape)
    }
    const grew = Object.entries(now).filter(([id, c]) => c.h !== baseH[id])
    check(`${sc.key}: 卡片高度和编辑态一致`, grew.length === 0,
      grew.map(([id, c]) => `${id} ${baseH[id]}→${c.h}`).join(', '))

    const anim = await runningAnimations(page)
    delete anim.names
    const idleStates = ['waiting', 'done', 'failed', 'skipped', 'cancelled', 'suspended', 'blocked', 'unreached', 'queued', 'idle']
    const lingering = idleStates.filter((s) => anim[s]).map((s) => `${s}×${anim[s]}`)
    check(`${sc.key}: 非运行中的卡片没有常驻动画`, lingering.length === 0, lingering.join(', ') || JSON.stringify(anim))

    if (sc.key === 'live') {
      const tele = now.body.text
      check('流式输出替换摘要位', (await page.locator('.react-flow__node[data-id="body"] .nc-stream').innerText()).includes('夜班'))
      check('运行中走着计时', /\d\d:\d\d/.test(tele), tele)
      const t1 = await page.locator('.react-flow__node[data-id="body"] .nc-tele-main').innerText()
      await page.waitForTimeout(600)
      const t2 = await page.locator('.react-flow__node[data-id="body"] .nc-tele-main').innerText()
      check('计时器在走', t1 !== t2, `${t1} → ${t2}`)
      check('循环写出 3/5 与分段进度', now.loop.text.includes('3/5')
        && await page.locator('.react-flow__node[data-id="loop"] .nc-tele .nc-seg i').count() === 5, now.loop.text)
      check('循环体写出执行次数 ×3', (await page.locator('.react-flow__node[data-id="body"] .nc-head').innerText()).includes('×3'))
      const matrix = await page.locator('.react-flow__node[data-id="team"] .team-matrix').innerText()
      check('矩阵有调度者一行', matrix.includes('调度者'), matrix.split('\n').slice(0, 3).join(' / '))
      check('先交回的成员先变绿', await page.locator('.react-flow__node[data-id="team"] .team-row[data-member-status="done"]').count() === 1)
      // 三人并行交回一个：仍是并行的一轮，人数按派出去的算，另说还有几个在跑
      check('本轮并行中的说法', matrix.includes('本轮 3 人并行 · 2 人在跑') && !matrix.includes('串行'), matrix.split('\n')[0])
      check('底部测量前写「—」', /—\s*$/.test(matrix.trim()), matrix.split('\n').slice(-1)[0])
      check('跳过写出理由', now.skip.text.includes('skip_if'), now.skip.text)
      check('排队写出在等什么', now.join.text.includes('排队'), now.join.text)
      check('运行中的卡片有光弧', await page.locator('.nc.node-running .fx-halo').count() > 0)
      const halo = await page.evaluate(() => {
        const el = document.querySelector('.nc.node-running .fx-halo')
        return el ? getComputedStyle(el).backgroundImage : ''
      })
      const accent = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--accent').trim())
      const rgb = await page.evaluate((c) => { const d = document.createElement('div'); d.style.color = c; document.body.append(d); const v = getComputedStyle(d).color; d.remove(); return v }, accent)
      check('光弧用运行态色（不是类型色）', halo.includes(rgb.replace('rgb(', '').replace(')', '').split(', ').join(', ')), rgb)
    }
    if (sc.key === 'waiting') {
      check('等待写出已等时长', /已等\s*\d\d:\d\d/.test(now.human.text), now.human.text)
      const btn = page.locator('.react-flow__node[data-id="human"] .nc-approve')
      check('等待节点上有「去审批」', await btn.count() === 1)
      // 药丸画出来 16px 高，按钮本身撑满遥测槽：药丸上沿、下沿再往外 2px 仍点得到。
      // 先取景到这个节点再量（换档之后它可能在窗口外）；也要先量再点：点的时候
      // Playwright 会把按钮滚进视野，画布容器跟着挪，之后按钮可能出窗口
      await page.evaluate(() => window.__studio.getState().focusNode('human'))
      await page.waitForTimeout(700)
      await park(page)
      const reach = await btn.evaluate((el) => {
        const k = el.getBoundingClientRect().height / el.offsetHeight
        const r = el.getBoundingClientRect()
        const x = r.left + r.width / 3
        const hit = (y) => document.elementFromPoint(x, y)
        const miss = [r.top + 0.5 * k, r.bottom - 0.5 * k].map(hit).filter((h) => h !== el)
        return { h: el.offsetHeight, ok: el.offsetHeight >= 20 && !miss.length, miss: miss.map((h) => `${h?.tagName}.${h?.className}`).join(' ') }
      })
      check('去审批的点击区比画出来的药丸大', reach.ok, `按钮高 ${reach.h}px（药丸 16px）${reach.miss ? ` · 点到了 ${reach.miss}` : ''}`)
      await btn.click()
      await page.waitForTimeout(500)
      const pop = page.locator('.nc-pop')
      check('去审批就地打开审批卡', await pop.count() === 1 && (await pop.innerText()).includes('通过'),
        (await pop.innerText().catch(() => '')).split('\n')[0])
      check('点审批卡不会选中节点', await page.evaluate(() => window.__studio.getState().selectedId) == null)
      check('画布上的审批卡不再重复工作流名', !(await pop.innerText()).includes('__run_states__'))
      const aria = await btn.evaluate((el) => ({ pop: el.getAttribute('aria-haspopup'), ctl: el.getAttribute('aria-controls') }))
      check('去审批声明弹出的是对话框', aria.pop === 'dialog' && !!aria.ctl
        && await page.evaluate((id) => !!document.getElementById(id)?.classList.contains('nc-pop'), aria.ctl), JSON.stringify(aria))
      // 键盘：打开就进到浮层里，Tab 在里面转圈，走不到下一个节点上
      const inside = () => page.evaluate(() => !!document.activeElement?.closest('.nc-pop'))
      check('打开后焦点在审批卡里', await inside(), await page.evaluate(() => document.activeElement?.outerHTML.slice(0, 60)))
      let stayed = true
      for (let i = 0; i < 8; i++) { await page.keyboard.press('Tab'); stayed &&= await inside() }
      await page.keyboard.press('Shift+Tab'); stayed &&= await inside()
      check('Tab 在审批卡里转圈', stayed)
      const hasButtons = await page.evaluate(() => ['通过', '驳回'].every((t) =>
        [...document.querySelectorAll('.nc-pop button')].some((b) => b.textContent.includes(t))))
      check('键盘够得着通过 / 驳回', hasButtons)
      shots.push(await shot(page, `${theme}-waiting-approve`))
      // 焦点在浮层的输入框里按 Esc：以前被浮层根的 stopPropagation 截走，收不起来
      await page.locator('.nc-pop input').first().focus()
      await page.keyboard.press('Escape')
      await page.waitForTimeout(200)
      check('焦点在浮层里时 Esc 收起审批卡', await pop.count() === 0)
      check('收起后焦点回到「去审批」', await page.evaluate(() => document.activeElement?.classList.contains('nc-approve')))
      // 焦点还在按钮上时的 Esc 走 document 那一路
      await btn.click()
      await page.waitForTimeout(300)
      await btn.focus()
      await page.keyboard.press('Escape')
      await page.waitForTimeout(200)
      check('焦点在按钮上时 Esc 也收起', await pop.count() === 0)
      // 悬停联动（右栏悬停这一步）不能把等待的琥珀加粗盖掉
      await page.evaluate(() => window.__studio.getState().setHoveredNode('human'))
      await page.waitForTimeout(150)
      const weight = await page.evaluate(() => {
        const d = document.createElement('div'); d.style.color = 'var(--st-waiting)'; document.body.append(d)
        const amber = getComputedStyle(d).color; d.remove()
        return { amber, shadow: getComputedStyle(document.querySelector('.react-flow__node[data-id="human"] .nc')).boxShadow }
      })
      check('悬停不盖掉等待的琥珀加粗', weight.shadow.includes(weight.amber) && weight.shadow.split('rgb').length > 4,
        weight.shadow.slice(0, 90))
      await page.evaluate(() => window.__studio.getState().setHoveredNode(null))
    }
    if (sc.key === 'failed') {
      check('失败写出错误摘要', now.team.text.includes('没有回应'), now.team.text)
      check('阻断写出理由', now.gate.text.includes('上游失败'), now.gate.text)
      check('重试次数上卡', (await page.locator('.react-flow__node[data-id="team"] .nc-head').innerText()).includes('↻1'))
    }
    if (sc.key === 'cancelled') {
      check('取消的循环写出停在第几项', now.loop.text.includes('2/5')
        && await page.locator('.react-flow__node[data-id="loop"] .nc-seg i.is-cur').count() === 0, now.loop.text)
    }
    if (sc.key === 'succeeded') {
      const stamp = page.locator('.react-flow__node[data-id="out"] .nc-stamp')
      check('成果节点落出具印章', await stamp.count() === 1 && (await stamp.innerText()).includes('完整出具'),
        await stamp.innerText().catch(() => ''))
      check('没走的一侧未到达', now.fix.text.includes('本次未执行'), now.fix.text)
      check('命中的出口亮起、落空的压暗',
        await page.locator('.react-flow__node[data-id="gate"] .nc-exit.is-hit').count() === 1
        && await page.locator('.react-flow__node[data-id="gate"] .nc-exit.is-miss').count() >= 1)
    }

    for (const lod of ['full', 'compact', 'signal']) {
      const got = await setLod(page, lod)
      if (await lost(page)) console.log(`  ! ${sc.key}/${lod}: 运行状态丢了（热更新？），这一档的结果不可信`)
      const h = await cards(page)
      const moved = Object.entries(h).filter(([id, c]) => c.h !== baseH[id])
      check(`${sc.key}/${lod}: 换档不改卡片尺寸`, moved.length === 0,
        moved.map(([id, c]) => `${id} ${baseH[id]}→${c.h}`).join(', ') || `zoom ${got.zoom.toFixed(2)}${got.byCanvas ? '' : '（档位由脚本补写）'}`)
      if (lod === 'signal' && (sc.key === 'failed' || sc.key === 'waiting')) {
        const pin = await page.evaluate(() => {
          const el = document.querySelector('.nc-pin')
          if (!el) return null
          const r = el.getBoundingClientRect()
          return { vis: getComputedStyle(el).visibility, font: parseFloat(getComputedStyle(el).fontSize), h: r.height }
        })
        check(`${sc.key}/signal: 异常节点挂出反向缩放的牌子`, !!pin && pin.vis === 'visible' && pin.h >= 12 && pin.h <= 26,
          pin ? `屏幕高 ${pin.h.toFixed(1)}px` : '没有牌子')
      }
      if (lod === 'compact') {
        const vis = await page.evaluate(() => getComputedStyle(document.querySelector('.nc-lod')).visibility)
        check(`${sc.key}/compact: 精简卡替身可见`, vis === 'visible')
        if (sc.key === 'live') {
          const brief = await lodRead(page, 'team')
          check('live/compact: 协作矩阵收成一行摘要（读数在前）', /^\d\/\d 在跑 · 第 1 轮/.test(brief), brief)
          // 精简档的读数是这一档的重点：屏幕上不小于 9px，档内最低的 0.33 倍也一样
          // （回滞让精简档一直留到 0.33）。--zoom 临时改成 0.33 量一遍再改回去
          const fonts = await page.evaluate((z) => {
            const root = document.querySelector('.react-flow')
            const read = document.querySelector('.react-flow__node[data-id="body"] .nc-lod-read')
            const title = document.querySelector('.react-flow__node[data-id="body"] .nc-lod-title')
            const lod = read.closest('.nc-lod')
            const size = (el) => parseFloat(getComputedStyle(el).fontSize)
            const now = { read: size(read) * z, title: size(title) * z }
            const was = root.style.getPropertyValue('--zoom')
            root.style.setProperty('--zoom', '0.33')
            const low = { read: size(read) * 0.33, title: size(title) * 0.33, fits: lod.scrollHeight <= lod.clientHeight + 1 }
            root.style.setProperty('--zoom', was)
            return { now, low }
          }, got.zoom)
          check('live/compact: 读数屏幕字号不小于 9px（当前缩放与 0.33 倍）',
            fonts.now.read >= 9 && fonts.low.read >= 9 && fonts.low.title >= 11 && fonts.low.fits,
            `当前 ${fonts.now.read.toFixed(1)} / 标题 ${fonts.now.title.toFixed(1)}px · 0.33 倍 ${fonts.low.read.toFixed(1)} / ${fonts.low.title.toFixed(1)}px${fonts.low.fits ? '' : ' · 撑出了卡片'}`)
          const bodyRead = await lodRead(page, 'body')
          check('live/compact: 读数不再重复状态字', /^\d\d:\d\d/.test(bodyRead), bodyRead)
        }
      }
      if (sc.key === 'succeeded' && lod !== 'full') {
        const stampH = await page.evaluate(() => document.querySelector('.react-flow__node[data-id="out"] .nc-stamp')?.getBoundingClientRect().height ?? 0)
        check(`succeeded/${lod}: 印章反向缩放，远景也读得出档位`, stampH >= 14 && stampH <= 28, `${stampH.toFixed(1)}px`)
      }
      if (lod === 'signal') {
        const glyph = await page.evaluate(() => {
          const el = document.querySelector('.react-flow__node[data-id="in"] .nc-sig svg')
          return el ? el.getBoundingClientRect().height : 0
        })
        check(`${sc.key}/signal: 居中剪影在屏幕上够大`, glyph >= 13 && glyph <= 24, `${glyph.toFixed(1)}px`)
      }
      shots.push(await shot(page, `${theme}-${sc.key}-${lod}`))
    }
  }

  check('没有运行时报错', errors.length === 0, errors.slice(0, 3).join(' | '))
  await browser.close()
})

// ---------------------------------------------------------------- 剪影

await section('themes', async () => {
  console.log('\n=== 剪影 ===')
  const pairs = Object.entries(shapesByState).map(([s, set]) => [s, [...set]])
  const owner = {}
  let clash = ''
  for (const [state, shapes] of pairs) {
    if (shapes.length !== 1) clash ||= `${state} 有 ${shapes.join('/')}`
    for (const sh of shapes) {
      if (owner[sh] && owner[sh] !== state) clash ||= `${owner[sh]} 和 ${state} 都是 ${sh}`
      owner[sh] = state
    }
  }
  const covered = Object.keys(shapesByState).sort()
  const all = ['blocked', 'cancelled', 'done', 'failed', 'idle', 'queued', 'running', 'skipped', 'suspended', 'unreached', 'waiting']
  check('每种状态都在卡片上出现过', all.every((s) => covered.includes(s)), covered.join(' '))
  check('每种状态的剪影互不相同', !clash, clash || pairs.map(([s, sh]) => `${s}=${sh}`).join(' '))
})

// ---------------------------------------------------------------- 历史灌入

await section('interact', async () => {
  console.log('\n=== 选中、聚焦、Copilot 新节点、一次性时刻 ===')
  const { browser, page, errors } = await open({ theme: 'dark' })
  await setLod(page, 'full')
  const cls = (id) => page.evaluate((id) => document.querySelector(`.react-flow__node[data-id="${id}"] .nc`).className, id)

  await page.evaluate(() => window.__studio.getState().select('gate'))
  await page.waitForTimeout(150)
  check('选中态以 selectedId 为准', (await cls('gate')).includes('nc-selected'))
  const ring = await page.evaluate(() => getComputedStyle(document.querySelector('.nc.nc-selected')).boxShadow)
  check('选中用外圈（隔一圈底色），不是只换边框色', /,/.test(ring) && ring !== 'none', ring.slice(0, 60))
  await page.evaluate(() => window.__studio.getState().select(null))

  await page.evaluate(() => window.__studio.getState().setHoveredNode('body'))
  await page.waitForTimeout(150)
  check('右栏悬停：对应节点亮聚焦环', (await cls('body')).includes('nc-hover'))
  check('右栏悬停：其余节点退下去', (await cls('gate')).includes('nc-dim'))
  await page.evaluate(() => window.__studio.getState().setHoveredNode(null))

  await page.evaluate(() => window.__studio.getState().focusNode('join'))
  await page.waitForTimeout(80)
  check('取景请求：落点节点荡一圈', await page.locator('.react-flow__node[data-id="join"] .fx-focus').count() === 1)

  // Copilot 紫框：编辑态有，运行态一来就让位
  await page.evaluate(() => window.__studio.setState({ copilotNew: ['body', 'gate'] }))
  await page.waitForTimeout(100)
  const purple = await page.evaluate(() => getComputedStyle(document.querySelector('.react-flow__node[data-id="gate"] .nc')).borderTopColor)
  // 刚放上来的节点被选中：选中圈和紫色描边叠在一起，不是紫色把选中圈盖掉
  await page.evaluate(() => window.__studio.getState().select('gate'))
  await page.waitForTimeout(150)
  const picked = await page.evaluate(() => {
    const d = document.createElement('div'); d.style.color = 'var(--accent)'; document.body.append(d)
    const accent = getComputedStyle(d).color; d.style.color = 'var(--copilot)'
    const violet = getComputedStyle(d).color; d.remove()
    const shadow = getComputedStyle(document.querySelector('.react-flow__node[data-id="gate"] .nc')).boxShadow
    return { accent, violet, shadow }
  })
  check('Copilot 新节点选中后有选中圈', picked.shadow.includes(picked.accent), picked.shadow.slice(0, 90))
  await page.evaluate(() => window.__studio.getState().select(null))
  await feed(page, LIVE(Date.now()))
  await page.evaluate(() => window.__studio.setState({ copilotNew: ['body', 'gate'] }))
  await page.waitForTimeout(200)
  const colors = await page.evaluate(() => {
    const b = getComputedStyle(document.querySelector('.react-flow__node[data-id="body"] .nc')).borderTopColor
    const d = document.createElement('div'); d.style.color = 'var(--st-running)'; document.body.append(d)
    const run = getComputedStyle(d).color; d.remove()
    return { b, run }
  })
  check('编辑态的 Copilot 新节点是紫框', purple !== colors.run && purple !== 'rgb(35, 42, 58)', purple)
  check('运行态压过 Copilot 紫框', colors.b === colors.run, `${colors.b} / ${colors.run}`)
  await page.evaluate(() => window.__studio.setState({ copilotNew: [] }))

  // 一次性时刻：根元素写上 data-moment，卡片侧播一次、播完就停
  const moment = async (kind, events, states) => {
    await feed(page, events(Date.now()), { replay: true })
    await page.waitForTimeout(300)
    await page.evaluate((k) => { document.querySelector('.react-flow').dataset.moment = k }, kind)
    await page.waitForTimeout(90)
    const during = await runningAnimations(page, { transitions: false })
    const hit = states.reduce((acc, st) => acc + (during[st] ?? 0), 0)
    await page.waitForTimeout(2200)
    const after = await runningAnimations(page, { transitions: false })
    const left = states.reduce((acc, st) => acc + (after[st] ?? 0), 0)
    await page.evaluate(() => { delete document.querySelector('.react-flow').dataset.moment })
    check(`时刻 ${kind}：卡片上播了（${states.join('/')}）`, hit > 0, during.names.join(', '))
    check(`时刻 ${kind}：播完就停`, left === 0, JSON.stringify(after))
  }
  // 探索运行的印章带「不归档」；正式运行进行中画布只读，端口不可连
  const gapOutput = { _issuance: { tier: 'degraded', matched_numbers: 4, metrics_checked: 0, gaps: ['指标集为空'], missing_required: [], unmatched_numbers: [] } }
  const degraded = SUCCEEDED(Date.now()).map((e) => (e.type === 'issuance' ? { ...e, data: { ...e.data, tier: 'degraded' } } : e))
  await feed(page, degraded, { runClass: 'exploratory' })
  // run.finished 带的 output 会把运行上的覆盖掉：灌完再放完整的出具明细
  await page.evaluate((output) => {
    const st = window.__studio
    st.setState({ run: { ...st.getState().run, output } })
  }, gapOutput)
  await page.waitForTimeout(200)
  const stamp = page.locator('.react-flow__node[data-id="out"] .nc-stamp')
  const stampText = await stamp.innerText().catch(() => '')
  check('探索运行的印章写明不归档', stampText.includes('不归档') && stampText.includes('降档出具'), stampText.replace(/\n/g, ' '))
  const stampInfo = await stamp.evaluate((el) => ({
    tag: el.tagName, role: el.getAttribute('role'), tab: el.tabIndex, cursor: getComputedStyle(el).cursor, title: el.title,
  })).catch(() => null)
  // 它是一枚章，不是按钮：不进 Tab 序列，悬停前没有手形
  check('印章不是个按钮', !!stampInfo && stampInfo.tag !== 'BUTTON' && stampInfo.role === 'img'
    && stampInfo.tab < 0 && stampInfo.cursor !== 'pointer', JSON.stringify(stampInfo && { ...stampInfo, title: undefined }))
  check('印章悬停写出「校验没跑全」', (stampInfo?.title ?? '').includes('校验没跑全：指标集为空'), (stampInfo?.title ?? '').split('\n').join(' / '))
  // 右栏看得见时点它是去看完整判定的捷径：广播 agentlab:goto-issuance，右栏在对话层时自己切过去
  await page.evaluate(() => {
    window.__issuance = []
    window.addEventListener('agentlab:goto-issuance', (e) => window.__issuance.push(e.detail))
  })
  await stamp.hover()
  const cursor = await stamp.evaluate((el) => getComputedStyle(el).cursor)
  await stamp.click()
  const went = await page.evaluate(() => window.__issuance)
  check('右栏看得见：悬停成了链接，点它请右栏摆出出具横幅', cursor === 'pointer' && went.length === 1 && went[0].runId === RUN_ID,
    `${cursor} · ${JSON.stringify(went)}`)
  // 右栏停在对话层时横幅不在页面上：还是能点，右栏接手——切到运行层、滚到横幅、焦点落上去
  await page.locator('[data-assistant-panel] button[title="回到和助手的对话"]').click({ timeout: 2000 }).catch(() => {})
  await page.waitForTimeout(250)
  const bannerGone = await page.locator('[data-issuance-banner]').count() === 0
  await stamp.hover()
  const chatCursor = await stamp.evaluate((el) => getComputedStyle(el).cursor)
  await stamp.click()
  await page.waitForFunction(() => document.activeElement?.closest('[data-issuance-banner]'), null, { timeout: 3000 }).catch(() => {})
  const landed = await page.evaluate(() => {
    const b = document.querySelector('[data-issuance-banner]')
    return { banner: !!b, focus: !!document.activeElement?.closest('[data-issuance-banner]'), flash: b?.dataset.flash ?? null }
  })
  check('右栏在对话层：印章照样是链接，点了切到运行层、落到出具横幅上', bannerGone && chatCursor === 'pointer'
    && landed.banner && landed.focus && landed.flash === 'focus',
    `${bannerGone ? '' : '（准备：横幅还在页面上）'}${chatCursor} · ${JSON.stringify(landed)}`)
  // 回指明细带口径（issuance.matched[].caliber）：同一个指标换过口径，看得出这次按的是哪一版
  await page.evaluate(() => {
    const st = window.__studio
    const out = st.getState().run.output
    st.setState({ run: { ...st.getState().run, output: { ...out, _issuance: { ...out._issuance,
      matched: [{ token: '12.4%', metric: '毛利率', caliber: '月度口径 @ v2' }, { token: '1,204', metric: '订单数' }] } } } })
  })
  await page.waitForTimeout(150)
  const matchedTitle = await stamp.getAttribute('title')
  check('印章悬停列出回指明细和口径', (matchedTitle ?? '').includes('12.4% → 毛利率（月度口径 @ v2）')
    && (matchedTitle ?? '').includes('1,204 → 订单数'), (matchedTitle ?? '').split('\n').join(' / '))
  check('运行结束后端口恢复可连', await page.evaluate(() =>
    document.querySelector('.react-flow__node[data-id="gate"] .react-flow__handle')?.classList.contains('connectable')))
  // 探索运行进行中也不能连线：端口跟着 React Flow 的 connectable 走
  await feed(page, LIVE(Date.now()), { runClass: 'exploratory' })
  await page.waitForTimeout(200)
  const explore = await page.evaluate(() => {
    const h = document.querySelector('.react-flow__node[data-id="gate"] .react-flow__handle')
    return h ? { connectable: h.classList.contains('connectable'), pe: getComputedStyle(h).pointerEvents, op: getComputedStyle(h).opacity } : null
  })
  check('探索运行进行中：端口不给「还能连」的暗示', !!explore && !explore.connectable && explore.pe === 'none' && Number(explore.op) < 0.5,
    JSON.stringify(explore))
  await feed(page, LIVE(Date.now()), { runClass: 'formal' })
  await page.waitForTimeout(200)
  const lock = await page.evaluate(() => {
    const root = document.querySelector('.react-flow')
    const h = document.querySelector('.react-flow__node[data-id="gate"] .react-flow__handle')
    return { cls: root.dataset.runClass, phase: root.dataset.runPhase, pe: h ? getComputedStyle(h).pointerEvents : null }
  })
  check('正式运行进行中：卡片端口不可连', lock.pe === 'none', JSON.stringify(lock))

  await moment('success', SUCCEEDED, ['done'])
  await moment('failed', FAILED, ['blocked'])
  await moment('start', LIVE, ['idle', 'done', 'queued', 'skipped'])

  check('没有运行时报错', errors.length === 0, errors.slice(0, 3).join(' | '))
  await browser.close()
})

// ---------------------------------------------------------------- 协作矩阵的措辞

await section('team', async () => {
  console.log('\n=== 协作矩阵：并行的一轮交回一部分、全部交回等调度 ===')
  const { browser, page, errors } = await open({ theme: 'dark' })
  await setLod(page, 'full')
  const ROUND = [...HEAD, ...TEAM_ROUND0,
    [2.2, 'agent.step.end', 'team', { agent: '审校', duration_ms: 2500, round: 0, parallel: 3, preview: 'ok' }],
    [1.0, 'agent.step.end', 'team', { agent: '工艺员', duration_ms: 3500, round: 0, parallel: 3, preview: 'ok' }],
  ]
  const matrixOf = () => page.evaluate(() => {
    const m = document.querySelector('.react-flow__node[data-id="team"] .team-matrix')
    const flat = (sel) => (m?.querySelector(sel)?.textContent ?? '').replace(/\s+/g, ' ').trim()
    return { head: flat('.team-head'), foot: flat('.team-foot') }
  })
  // 两个交回、一个还在跑：以前表头按"此刻在跑几个"说成「串行推进」
  await feed(page, script(ROUND)(Date.now()))
  await page.waitForTimeout(250)
  const two = await matrixOf()
  check('三人并行交回两个：表头仍是并行', two.head.includes('本轮 3 人并行') && two.head.includes('1 人在跑') && !two.head.includes('串行'),
    two.head)
  // 三个都交回、下一次调度还没开始（老后端没有 route 事件时这段能有十几秒）：
  // 底部已经写「并行省下」，表头不能说「串行」
  await feed(page, script([...ROUND,
    [1.0, 'agent.step.end', 'team', { agent: '数据员', duration_ms: 4500, round: 0, parallel: 3, preview: 'ok' }],
  ])(Date.now()))
  await page.waitForTimeout(250)
  const all = await matrixOf()
  check('三人都交回、等下一次调度：表头不说串行', !all.head.includes('串行') && all.head.includes('已交回'), all.head)
  check('三人都交回：底部报省下的时间，和表头不打架', all.foot.includes('并行省下') && !all.head.includes('串行'), `${all.head} | ${all.foot}`)
  await setLod(page, 'compact')
  const brief = await lodRead(page, 'team')
  check('精简档摘要不写「0/3 在跑」', !brief.includes('0/') && brief.includes('已交回'), brief)
  shots.push(await shot(page, 'dark-team-all-back'))
  check('没有运行时报错', errors.length === 0, errors.slice(0, 3).join(' | '))
  await browser.close()
})

// ---------------------------------------------------------------- 收场：用完轮数、写成文字的工具调用、超时

await section('endings', async () => {
  // 一张小图：协作团队两轮上限、三个成员（「定稿员」一次都派不到）；一个 agent 节点把
  // 工具调用写成了文字；一个工具节点查询超时；一个校验节点靠修复才过
  const graph = await (await fetch(`${API}/copilot/layout`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ graph: {
      nodes: [
        { id: 'in', type: 'input', data: { label: '入口', config: { fields: [{ name: 'week' }] } } },
        { id: 'team', type: 'supervisor', data: { label: '复盘小组', config: {
          goal: '汇总本周订单异常', max_rounds: 2, max_parallel: 2,
          agents: [agent('检索员', '拉订单明细'), agent('分析员', '找异常原因'), agent('定稿员', '写结论')],
        } } },
        { id: 'ask', type: 'agent', data: { label: '订单查询', config: { prompt: '查 orders 本周异常', tools: ['db_query__shop'] } } },
        { id: 'q', type: 'tool', data: { label: '取订单', config: { tool: 'db_query__shop' } } },
        { id: 'chk', type: 'validate', data: { label: '结构校验', config: { repair_with_llm: true, max_retries: 2 } } },
        { id: 'out', type: 'output', data: { label: '周报', config: { fields: [{ name: 'report' }] } } },
      ],
      edges: [
        { source: 'in', target: 'team' }, { source: 'in', target: 'ask' }, { source: 'in', target: 'q' },
        { source: 'q', target: 'chk' }, { source: 'team', target: 'out' }, { source: 'ask', target: 'out' },
        { source: 'chk', target: 'out' },
      ],
    } }),
  })).json()
  const workflow = { ...WORKFLOW, id: 'wcard-endings', name: '__endings__', graph }

  const MARKUP = '模型输出了工具调用的原始标记，但没有真正调用工具。常见原因：节点没有绑定工具，或者模型、服务不支持工具调用。'
  const EXHAUSTED = '协作团队用完 2 轮仍未完成：还没有定稿。一次都没被派到的成员：定稿员。先看成员有没有绑定要用的工具，再调大「最多轮数」；也可以把「用完轮数时」改成降档交付'
  const START = [
    [0, 'run.started', null, { nodes: 6 }],
    [0.1, 'node.started', 'in', {}],
    [0.05, 'node.finished', 'in', { duration_ms: 3, preview: { week: '2026-W39' } }],
  ]
  // 两轮都派了、调度者始终没说完成；第一轮的检索员把工具调用写成了文字，这一步记失败
  const ROUNDS = [
    [0.05, 'node.started', 'team', {}],
    [0.02, 'agent.route.start', 'team', { round: 0 }],
    [1.2, 'agent.route.end', 'team', { round: 0, duration_ms: 1200, agents: ['检索员'], parallel: 1, done: false, reason: '先拉订单明细' }],
    [0.02, 'agent.step.start', 'team', { agent: '检索员', instruction: '拉本周订单', round: 0, parallel: 1 }],
    [2.0, 'agent.step.end', 'team', { agent: '检索员', duration_ms: 2000, round: 0, parallel: 1, preview: '<tool_call>{"name": "db_query__shop"}</tool_call>',
      failed: true, error: '模型输出了工具调用的原始标记，但没有真正调用工具' }],
    [0.02, 'agent.route.start', 'team', { round: 1 }],
    [1.0, 'agent.route.end', 'team', { round: 1, duration_ms: 1000, agents: ['分析员'], parallel: 1, done: false, reason: '换人分析' }],
    [0.02, 'agent.step.start', 'team', { agent: '分析员', instruction: '找异常原因', round: 1, parallel: 1 }],
    [1.5, 'agent.step.end', 'team', { agent: '分析员', duration_ms: 1500, round: 1, parallel: 1, preview: '缺数据，无法判断' }],
  ]
  // 轮数用完之后的收尾判定：只判定、不派活，round 是上限（从 0 数就是「第 3 轮」）
  const closing = (done, reason) => [
    [0.02, 'agent.route.start', 'team', { round: 2, closing: true }],
    [0.9, 'agent.route.end', 'team', { round: 2, closing: true, duration_ms: 900, agents: [], parallel: 0, done, reason }],
    [0.05, 'llm.end', 'team', { agent: '调度者', model: 'm', input_tokens: 900, output_tokens: 120, cost_usd: 0.001 }],
  ]
  const FAIL = script([...START,
    [0.02, 'node.started', 'ask', {}],
    ...ROUNDS, ...closing(false, '还没有定稿'),
    [0.02, 'log', 'ask', { level: 'warn', code: 'tool_markup_leak', message: '模型输出了工具调用的原始标记，但没有真正调用工具' }],
    [0.02, 'node.failed', 'ask', { duration_ms: 6800, error: MARKUP }],
    [0.05, 'node.failed', 'team', { duration_ms: 6800, error: EXHAUSTED }],
    [0.05, 'run.failed', null, { error: '「复盘小组」失败', node_id: 'team', timing: { wall_ms: 7000, active_ms: 7000, wait_ms: 0 } }],
  ])
  const DEGRADE = script([...START,
    [0.02, 'node.started', 'ask', {}],
    // 第一次查询超出声明的 2s 时限被放弃，第二次查到了；收尾时模型还想要工具
    [0.05, 'tool.start', 'ask', { tool: 'db_query__shop', call_id: 'c1', timeout_s: 2 }],
    [3.0, 'tool.error', 'ask', { tool: 'db_query__shop', call_id: 'c1', duration_ms: 3000, timed_out: true, error: '查询超过 2s 没有返回，已放弃等待' }],
    [0.05, 'tool.start', 'ask', { tool: 'db_query__shop', call_id: 'c2', timeout_s: 2 }],
    [0.4, 'tool.end', 'ask', { tool: 'db_query__shop', call_id: 'c2', duration_ms: 400, preview: '{"row_count": 12}' }],
    [0.1, 'log', 'ask', { level: 'warn', code: 'tool_markup_leak', message: '收尾轮仍想调用工具：步数用完了' }],
    [0.05, 'llm.end', 'ask', { agent: '订单查询', model: 'm', input_tokens: 1200, output_tokens: 300, cost_usd: 0.002 }],
    [0.02, 'node.finished', 'ask', { duration_ms: 3700, preview: { text: '本周异常 12 单' } }],
    [0.02, 'node.started', 'q', {}],
    [0.02, 'tool.start', 'q', { tool: 'db_query__shop', call_id: 'c3', timeout_s: 30 }],
    [0.3, 'tool.end', 'q', { tool: 'db_query__shop', call_id: 'c3', duration_ms: 300, preview: '{"row_count": 12}' }],
    [0.02, 'node.finished', 'q', { duration_ms: 320, preview: { row_count: 12 } }],
    [0.02, 'node.started', 'chk', {}],
    [0.4, 'llm.end', 'chk', { model: 'm', purpose: 'repair', duration_ms: 400, input_tokens: 300, output_tokens: 40, cost_usd: 0.0005, calls: 1 }],
    [0.02, 'log', 'chk', { level: 'warn', code: 'repair_invented', message: '修复时出现了原文没有的值：total_count=0' }],
    [0.4, 'llm.end', 'chk', { model: 'm', purpose: 'repair', duration_ms: 400, input_tokens: 300, output_tokens: 40, cost_usd: 0.0005, calls: 1 }],
    [0.02, 'node.finished', 'chk', { duration_ms: 850, preview: { total_count: 12 } }],
    ...ROUNDS, ...closing(false, '还没有定稿'),
    [0.02, 'log', 'team', { level: 'warn', code: 'team_exhausted', message: '协作团队用完 2 轮仍未完成：还没有定稿。一次都没被派到的成员：定稿员。按降档交付' }],
    [0.02, 'node.finished', 'team', { duration_ms: 6800, preview: { exhausted: true, exhausted_reason: '还没有定稿', never_dispatched: '[1 项]', text: '缺数据，无法判断' } }],
    [0.05, 'node.started', 'out', {}],
    [0.05, 'node.finished', 'out', { duration_ms: 4, preview: { report: '…' } }],
    [0.05, 'run.finished', null, { output: {}, usage: {}, duration_ms: 9000, timing: { wall_ms: 9000, active_ms: 9000, wait_ms: 0 } }],
  ])
  // 进行中：收尾判定还没出结论；工具节点的查询已经超出声明的 2s
  const LIVE_JUDGING = script([...START,
    ...ROUNDS,
    [0.02, 'agent.route.start', 'team', { round: 2, closing: true }],
    [0.02, 'node.started', 'q', {}],
    [0.02, 'tool.start', 'q', { tool: 'db_query__shop', call_id: 'c9', timeout_s: 2 }],
    [3.2, 'log', null, { level: 'info', message: '心跳' }],
  ])
  // 收尾判定说完成了：正常交付，表头说清是轮数用完之后判的，不点名谁没被派到
  const CLOSED_OK = script([...START,
    ...ROUNDS, ...closing(true, '材料够了，可以收尾'),
    [0.05, 'node.finished', 'team', { duration_ms: 6800, preview: { text: '结论：…' } }],
    [0.05, 'run.finished', null, { output: {}, usage: {}, duration_ms: 7000, timing: { wall_ms: 7000, active_ms: 7000, wait_ms: 0 } }],
  ])
  const askLlm = (out) => [0.05, 'llm.end', 'ask', { agent: '订单查询', model: 'm', input_tokens: 600, output_tokens: out, cost_usd: 0.001 }]
  // 同一个节点执行两次：第一次的收场不能挂到第二次上。团队用完轮数判失败 → 调大轮数
  // 接着跑（后端给失败的节点标 resumed，但这是重新执行；前两轮在断点里，不重发）→ 派到
  // 定稿员、调度者判完成。agent 第一次收尾时还想调工具，第二次（循环体每一轮也是这样）干干净净
  const RERUN = script([...START,
    [0.02, 'node.started', 'ask', {}],
    [0.3, 'log', 'ask', { level: 'warn', code: 'tool_markup_leak',
      message: '模型把工具调用写成了文字（<tool_call>{"name": "db_query__shop"…），没有真正调用工具：步数用完后的收尾轮仍想调用工具，没能给出结论' }],
    askLlm(300),
    [0.02, 'node.finished', 'ask', { duration_ms: 400, preview: { text: '本周异常 12 单' } }],
    ...ROUNDS, ...closing(false, '还没有定稿'),
    [0.05, 'node.failed', 'team', { duration_ms: 6800, error: EXHAUSTED }],
    [0.05, 'run.failed', null, { error: '「复盘小组」失败', node_id: 'team', timing: { wall_ms: 7000, active_ms: 7000, wait_ms: 0 } }],
    [2.0, 'run.resumed', null, { from: ['team'], message: '从「复盘小组」接着跑，前面 2 个节点的结果保留' }],
    [0.02, 'run.started', null, { nodes: 6, resumed: true }],
    [0.05, 'node.started', 'team', { resumed: true }],
    [0.02, 'agent.route.start', 'team', { round: 2 }],
    [0.8, 'agent.route.end', 'team', { round: 2, duration_ms: 800, agents: ['定稿员'], parallel: 1, done: false, reason: '派定稿员写结论' }],
    [0.02, 'agent.step.start', 'team', { agent: '定稿员', instruction: '写结论', round: 2, parallel: 1 }],
    [1.2, 'agent.step.end', 'team', { agent: '定稿员', duration_ms: 1200, round: 2, parallel: 1, preview: '结论：…' }],
    [0.02, 'agent.route.start', 'team', { round: 3 }],
    [0.6, 'agent.route.end', 'team', { round: 3, duration_ms: 600, agents: [], parallel: 0, done: true, reason: '已经定稿' }],
    [0.05, 'node.finished', 'team', { duration_ms: 2700, preview: { text: '结论：…' } }],
    [0.05, 'node.started', 'ask', {}],
    askLlm(240),
    [0.3, 'node.finished', 'ask', { duration_ms: 320, preview: { text: '本周异常 9 单' } }],
    [0.05, 'node.started', 'out', {}],
    [0.05, 'node.finished', 'out', { duration_ms: 4, preview: { report: '…' } }],
    [0.05, 'run.finished', null, { output: {}, usage: {}, duration_ms: 12000, timing: { wall_ms: 12000, active_ms: 10000, wait_ms: 0 } }],
  ])
  // 写成文字、被提醒之后重答了：成果是重答的那一次，不是「收尾时仍想调用工具」那种不完整。
  // 团队里检索员提醒后重答了；分析员提醒后还这样，这一步记失败，不能算进「纠正过」
  const NUDGE = script([...START,
    [0.02, 'node.started', 'ask', {}],
    [0.3, 'log', 'ask', { level: 'warn', code: 'tool_markup_leak',
      message: '模型把工具调用写成了文字（<tool_call>{"name": "db_query__shop"…），没有真正调用工具，已提醒它重试一次' }],
    [0.02, 'tool.start', 'ask', { tool: 'db_query__shop', call_id: 'n1', timeout_s: 30 }],
    [0.3, 'tool.end', 'ask', { tool: 'db_query__shop', call_id: 'n1', duration_ms: 300, preview: '{"row_count": 12}' }],
    askLlm(280),
    [0.02, 'node.finished', 'ask', { duration_ms: 700, preview: { text: '本周异常 12 单' } }],
    [0.02, 'node.started', 'team', {}],
    [0.02, 'agent.route.start', 'team', { round: 0 }],
    [0.8, 'agent.route.end', 'team', { round: 0, duration_ms: 800, agents: ['检索员', '分析员'], parallel: 2, done: false, reason: '先拉明细、再找原因' }],
    [0.02, 'agent.step.start', 'team', { agent: '检索员', instruction: '拉本周订单', round: 0, parallel: 2 }],
    [0.01, 'agent.step.start', 'team', { agent: '分析员', instruction: '找异常原因', round: 0, parallel: 2 }],
    [0.4, 'log', 'team', { level: 'warn', code: 'tool_markup_leak',
      message: '检索员把工具调用写成了文字（<tool_call>{"name": "db_query__shop"…），没有真正调用工具，已提醒它重试一次' }],
    [0.2, 'log', 'team', { level: 'warn', code: 'tool_markup_leak',
      message: '分析员把工具调用写成了文字（<tool_call>{"name": "db_query__shop"…），没有真正调用工具，已提醒它重试一次' }],
    [0.6, 'agent.step.end', 'team', { agent: '检索员', duration_ms: 1200, round: 0, parallel: 2, preview: '拉到 12 单' }],
    [0.3, 'agent.step.end', 'team', { agent: '分析员', duration_ms: 1500, round: 0, parallel: 2, preview: '',
      failed: true, error: '模型输出了工具调用的原始标记，但没有真正调用工具，这一步什么都没查到' }],
    [0.02, 'agent.route.start', 'team', { round: 1 }],
    [0.6, 'agent.route.end', 'team', { round: 1, duration_ms: 600, agents: [], parallel: 0, done: true, reason: '明细够了' }],
    [0.05, 'node.finished', 'team', { duration_ms: 2500, preview: { text: '结论：…' } }],
    [0.05, 'run.finished', null, { output: {}, usage: {}, duration_ms: 3500, timing: { wall_ms: 3500, active_ms: 3500, wait_ms: 0 } }],
  ])
  /** 改团队节点的配置（运行之后调过「最多轮数」、或者压根没配）。值给 null 就是删掉这一项 */
  const setTeamConfig = (page, patch) => page.evaluate((patch) => {
    const st = window.__studio
    const node = st.getState().nodes.find((n) => n.id === 'team')
    const config = { ...node.data.config, ...patch }
    for (const [k, v] of Object.entries(patch)) if (v === null) delete config[k]
    st.setState({ nodes: st.getState().nodes.map((n) => (n.id === 'team' ? { ...n, data: { ...n.data, config } } : n)) })
  }, patch)

  const at = (page, id) => page.locator(`.react-flow__node[data-id="${id}"]`)
  const flat = (t) => (t ?? '').replace(/\s+/g, ' ').trim()
  const matrixOf = (page) => page.evaluate(() => {
    const m = document.querySelector('.react-flow__node[data-id="team"] .team-matrix')
    const right = m?.querySelector('.team-head-right')
    const row = (name) => [...(m?.querySelectorAll('.team-row') ?? [])].find((r) => r.textContent.includes(name))
    const member = (name) => {
      const r = row(name)
      return r ? { status: r.dataset.memberStatus, text: r.innerText.replace(/\s+/g, ' ').trim(), title: r.title } : null
    }
    return {
      head: (m?.querySelector('.team-head')?.textContent ?? '').replace(/\s+/g, ' ').trim(),
      headTitle: m?.querySelector('.team-head')?.getAttribute('title') ?? '',
      right: right?.textContent ?? '', tone: right?.classList.contains('is-err') ? 'err' : right?.classList.contains('is-warn') ? 'warn' : '',
      dispatch: (m?.querySelector('.team-row-dispatch')?.textContent ?? '').replace(/\s+/g, ' ').trim(),
      a: member('检索员'), b: member('分析员'), c: member('定稿员'),
    }
  })
  const note = (page, id) => page.evaluate((id) => {
    const el = document.querySelector(`.react-flow__node[data-id="${id}"] .nc-tele-note`)
    return el ? { text: el.textContent.trim(), title: el.title, warn: el.classList.contains('is-warn') } : null
  }, id)

  for (const theme of ['dark', 'light']) {
    console.log(`\n=== ${theme === 'dark' ? '暗色' : '亮色'}：收场说人话（用完轮数、工具调用写成文字、超时、修复） ===`)
    const { browser, page, errors } = await open({ theme, workflow })
    await setLod(page, 'full')
    const base = Object.fromEntries(Object.entries(await cards(page)).map(([id, c]) => [id, c.h]))

    // ---- 判失败：用完轮数（默认）
    await feed(page, FAIL(Date.now()))
    await page.waitForTimeout(900)
    let c = await cards(page)
    let m = await matrixOf(page)
    const teamNote = await note(page, 'team')
    check(`${theme}: 团队用完轮数判失败，卡片写「用完 2 轮仍未完成」`, c.team.state === 'failed'
      && teamNote?.text === '协作团队用完 2 轮仍未完成', JSON.stringify(teamNote?.text))
    check(`${theme}: 悬停写清理由和怎么办`, (teamNote?.title ?? '').includes('还没有定稿') && (teamNote?.title ?? '').includes('怎么办'),
      flat(teamNote?.title))
    check(`${theme}: 收尾判定不另算一轮（表头仍是第 2 轮 / 上限 2）`, m.head.startsWith('第 2 轮 / 上限 2'), m.head)
    check(`${theme}: 表头说「用完 2 轮 · 未完成」，失败上红`, m.right === '用完 2 轮 · 未完成' && m.tone === 'err', `${m.right} · ${m.tone}`)
    check(`${theme}: 调度者那一行写「收尾判定」`, m.dispatch.includes('收尾判定'), m.dispatch)
    check(`${theme}: 一次都没被派到的成员点出来`, m.c?.status === 'never' && m.c.text.includes('未派到')
      && m.headTitle.includes('一次都没被派到：定稿员'), JSON.stringify(m.c))
    check(`${theme}: 成员把工具调用写成文字：格子是失败，悬停写原因`, m.a?.status === 'failed'
      && (m.a.title ?? '').includes('原因：模型输出了工具调用的原始标记'), JSON.stringify(m.a))
    const askNote = await note(page, 'ask')
    check(`${theme}: agent 把工具调用写成文字而失败：卡片写原因，不是半句原话`, c.ask.state === 'failed'
      && askNote?.text === '模型没有真正调用工具' && (askNote?.title ?? '').includes('怎么办'), JSON.stringify(askNote))
    const grew = Object.entries(c).filter(([id, x]) => x.h !== base[id])
    check(`${theme}: 失败收场不撑高卡片`, grew.length === 0, grew.map(([id, x]) => `${id} ${base[id]}→${x.h}`).join(', '))
    shots.push(await shot(page, `${theme}-endings-failed`), await cardShot(page, ['team', 'ask'], `${theme}-endings-failed-cards`))

    // ---- 降档交付、收尾时还想调工具、查询超时、修复才过
    await feed(page, DEGRADE(Date.now()))
    await page.waitForTimeout(900)
    c = await cards(page)
    m = await matrixOf(page)
    const degr = await note(page, 'team')
    const chip = await at(page, 'team').locator('.nc-head .nc-chip-warn', { hasText: '降档' }).count()
    check(`${theme}: 降档交付：卡片写「用完 2 轮未完成 · 降档交付」，琥珀色、带「降档」标`, c.team.state === 'done'
      && degr?.text === '用完 2 轮未完成 · 降档交付' && degr.warn && chip === 1, JSON.stringify({ degr, chip }))
    check(`${theme}: 降档交付：表头琥珀、点出没被派到的成员`, m.right === '用完 2 轮 · 未完成' && m.tone === 'warn'
      && m.c?.status === 'never', `${m.right} · ${m.tone} · ${m.c?.status}`)
    const askDone = await note(page, 'ask')
    check(`${theme}: 收尾时还想调工具：跑完了但琥珀色提一句`, c.ask.state === 'done' && askDone?.text === '收尾时仍想调用工具' && askDone.warn,
      JSON.stringify(askDone))
    const tools = await at(page, 'ask').locator('.nc-tools').evaluate((el) => ({ bad: el.classList.contains('is-bad'), title: el.title }))
      .catch(() => null)
    check(`${theme}: 超时的那次调用算失败，悬停写「超时（上限 2s）」`, !!tools?.bad && tools.title.includes('超时（上限 2s）'), JSON.stringify(tools))
    const chkNote = await note(page, 'chk')
    check(`${theme}: 校验靠修复才过：写「修复 2 次后通过」，有一次凑数被作废就上琥珀`, chkNote?.text === '修复 2 次后通过'
      && chkNote.warn && chkNote.title.includes('total_count=0'), JSON.stringify(chkNote))
    shots.push(await shot(page, `${theme}-endings-degraded`), await cardShot(page, ['team', 'ask', 'q'], `${theme}-endings-degraded-cards`))
    // 校验节点在右边，常被右栏挡住：取景过去再截
    await page.evaluate(() => window.__studio.getState().focusNode('chk'))
    await page.waitForTimeout(600)
    shots.push(await cardShot(page, ['chk'], `${theme}-endings-degraded-validate`))

    // 回放到收尾判定还没出结论的那一刻：不提前降档、不提前点名
    const judging = await page.evaluate(() => {
      const segs = window.__studio.getState().trace.nodes.team.segments.filter((s) => s.kind === 'dispatch')
      return segs[segs.length - 1].start + 300
    })
    await page.evaluate((t) => window.__studio.getState().setReplayAt(t), judging)
    await page.waitForTimeout(300)
    m = await matrixOf(page)
    const early = await at(page, 'team').locator('.nc-head .nc-chip-warn', { hasText: '降档' }).count()
    check(`${theme}: 回放到收尾判定中：表头写「调度者在判定」，不提前降档、不点名`, m.right.includes('调度者在判定') && early === 0
      && m.c?.status !== 'never', `${m.right} · 降档标 ${early} · ${m.c?.status}`)
    await page.evaluate(() => window.__studio.getState().setReplayAt(null))

    // ---- 进行中：收尾判定中；查询超出声明的时限
    await feed(page, LIVE_JUDGING(Date.now()))
    await page.waitForTimeout(600)
    m = await matrixOf(page)
    check(`${theme}: 进行中的收尾判定：表头写「轮数用完 · 调度者在判定」，不是新的一轮`,
      m.right === '轮数用完 · 调度者在判定' && m.head.startsWith('第 2 轮 / 上限 2') && m.dispatch.includes('判定'), `${m.head} | ${m.dispatch}`)
    const over = await at(page, 'q').locator('.nc-over').innerText().catch(() => '')
    const overTitle = await at(page, 'q').locator('.nc-tools').getAttribute('title').catch(() => '') ?? ''
    // 这句话整个落在卡片里，不被右边沿切掉
    const overFits = await page.evaluate(() => {
      const el = document.querySelector('.react-flow__node[data-id="q"] .nc-over')
      const card = el?.closest('.nc')
      if (!el || !card) return false
      return el.getBoundingClientRect().right <= card.getBoundingClientRect().right - 2
    })
    check(`${theme}: 查询超出声明的时限：直说「已超出 2s 上限」，工具名在悬停里`, over === '已超出 2s 上限' && overTitle.includes('db_query__shop ⋯ 进行中（上限 2s）') && overFits, `${over} · ${flat(overTitle)}${overFits ? '' : ' · 超出了卡片'}`)
    shots.push(await shot(page, `${theme}-endings-live`), await cardShot(page, ['team', 'q'], `${theme}-endings-live-cards`))
    await setLod(page, 'compact')
    const brief = await lodRead(page, 'team', (t) => t === '轮数用完 · 收尾判定中')
    check(`${theme}: 精简档：收尾判定中写清是判定，不写「第 3 轮」`, brief === '轮数用完 · 收尾判定中', brief)
    // 收尾判定认事件里的 closing，不拿配置里的「最多轮数」比：没配（后端按缺省值跑）、
    // 运行之后调大了（接着跑之前、回放历史运行），照样认得出是在判定
    await setTeamConfig(page, { max_rounds: null })
    await page.waitForTimeout(150)
    const briefBare = await lodRead(page, 'team', (t) => t === '轮数用完 · 收尾判定中')
    await setLod(page, 'full')
    m = await matrixOf(page)
    check(`${theme}: 没配最多轮数：照样认出收尾判定（精简档、表头、调度者那一行）`, briefBare === '轮数用完 · 收尾判定中'
      && m.right === '轮数用完 · 调度者在判定' && m.dispatch.includes('判定'), `${briefBare} | ${m.right} | ${m.dispatch}`)
    await setTeamConfig(page, { max_rounds: 4 })
    await page.waitForTimeout(150)
    m = await matrixOf(page)
    check(`${theme}: 运行之后调大了最多轮数：照样认出收尾判定`, m.right === '轮数用完 · 调度者在判定'
      && m.dispatch.includes('判定'), `${m.right} | ${m.dispatch}`)
    await setTeamConfig(page, { max_rounds: 2 })

    // ---- 收尾判定说完成了
    await feed(page, CLOSED_OK(Date.now()))
    await page.waitForTimeout(600)
    m = await matrixOf(page)
    check(`${theme}: 收尾判定已完成：表头说清、安静、不点名`, m.right === '轮数用完 · 判定已完成' && m.tone === ''
      && m.head.startsWith('第 2 轮 / 上限 2') && m.c?.status !== 'never', `${m.head} · ${m.tone} · ${m.c?.status}`)
    check(`${theme}: 悬停写完整判定`, m.headTitle.includes('轮数用完 · 调度者判定：已完成') && m.headTitle.includes('材料够了'), flat(m.headTitle))

    // ---- 同一个节点执行第二次：上一次的收场不挂到这一次上。照真实流程先把最多轮数调大
    // 再接着跑：回放到第一次时「用完 2 轮」得按那一次的原话，不按现在的配置写成 4
    await setTeamConfig(page, { max_rounds: 4 })
    // 接着跑、团队重新执行而调度者还没开口：reduceTeam 刚把上一次清空（rounds 为空）。矩阵要画成
    // 「还没有数据」，和第一次执行刚开始时一模一样，不写「等调度者」「已收尾」（3C REQ-17）
    const rr = RERUN(Date.now())
    const teamStarts = rr.map((e, i) => (e.type === 'node.started' && e.node_id === 'team' ? i : -1)).filter((i) => i >= 0)
    await feed(page, rr.slice(0, teamStarts[1] + 1))
    await page.waitForTimeout(250)
    const resetM = await matrixOf(page)
    await feed(page, rr.slice(0, teamStarts[0] + 1))
    await page.waitForTimeout(250)
    const freshM = await matrixOf(page)
    check(`${theme}: 接着跑、团队重新执行还没派活：矩阵和第一次刚开始时一样（还没有数据）`,
      teamStarts.length >= 2 && resetM.head === freshM.head && resetM.right === freshM.right
        && !/等调度者|已收尾|共 0 轮/.test(resetM.head), `${resetM.head} ‖ ${freshM.head}`)
    await feed(page, RERUN(Date.now()))
    await page.waitForTimeout(700)
    const quietEnding = async (label) => {
      const cs = await cards(page)
      const mx = await matrixOf(page)
      const never = await at(page, 'team').locator('[data-member-status="never"]').count()
      const chips = await at(page, 'team').locator('.nc-head .nc-chip-warn', { hasText: '降档' }).count()
      const tn = await note(page, 'team')
      const an = await note(page, 'ask')
      check(`${theme}: ${label}：团队表头不挂上一次的「判定未完成」、安静、不点名`, cs.team.state === 'done' && mx.tone === ''
        && !mx.right.includes('未完成') && !mx.headTitle.includes('未完成') && never === 0 && chips === 0 && !tn?.warn,
        `${mx.right} · 语气「${mx.tone}」· 未派到 ${never} · 降档标 ${chips} · ${JSON.stringify(tn)}`)
      check(`${theme}: ${label}：agent 这一次干干净净，写产出量，不写上一次的「收尾时仍想调用工具」`, cs.ask.state === 'done'
        && !!an && !an.warn && /tok/.test(an.text), JSON.stringify(an))
    }
    await quietEnding('调大轮数接着跑成功、agent 再执行一次')
    shots.push(await cardShot(page, ['team', 'ask'], `${theme}-endings-rerun-cards`))
    const execs = await page.evaluate(() => {
      const n = window.__studio.getState().trace.nodes
      const runs = (id) => n[id].segments.filter((s) => s.kind === 'run')
      return { teamRuns: runs('team').length, askRuns: runs('ask').length,
               first: runs('team')[0].end + 300, second: runs('ask').at(-1).end + 10 }
    })
    check(`${theme}: 夹具确实是两次执行（团队、agent 各两段）`, execs.teamRuns === 2 && execs.askRuns === 2, JSON.stringify(execs))
    await page.evaluate((t) => window.__studio.getState().setReplayAt(t), execs.second)
    await page.waitForTimeout(300)
    await quietEnding('回放到第二次执行里')
    // 回放到第一次失败的那一刻：说的是那一次的收场，不因为后来成功了就不说
    await page.evaluate((t) => window.__studio.getState().setReplayAt(t), execs.first)
    await page.waitForTimeout(300)
    m = await matrixOf(page)
    const firstTeam = await note(page, 'team')
    const firstAsk = await note(page, 'ask')
    check(`${theme}: 回放到第一次失败那一刻：团队写那一次的「用完 2 轮仍未完成」，表头红、点名定稿员`,
      firstTeam?.text === '协作团队用完 2 轮仍未完成' && m.right === '用完 2 轮 · 未完成' && m.tone === 'err' && m.c?.status === 'never',
      `${JSON.stringify(firstTeam?.text)} · ${m.right} · ${m.tone} · ${m.c?.status}`)
    check(`${theme}: 回放到第一次执行：agent 写那一次的「收尾时仍想调用工具」`, firstAsk?.text === '收尾时仍想调用工具' && firstAsk.warn,
      JSON.stringify(firstAsk))
    await page.evaluate(() => window.__studio.getState().setReplayAt(null))
    await setTeamConfig(page, { max_rounds: 2 })

    // ---- 写成文字、被提醒之后重答了：不是「收尾时仍想调用工具」
    await feed(page, NUDGE(Date.now()))
    await page.waitForTimeout(700)
    const askNudge = await note(page, 'ask')
    check(`${theme}: agent 写成文字被提醒后重答了：写「纠正过一次工具调用」，不说成果可能不完整`,
      askNudge?.text === '纠正过一次工具调用' && askNudge.warn && askNudge.title.includes('提醒之后重答了')
      && !askNudge.title.includes('可能不完整'), JSON.stringify(askNudge))
    const teamNudge = await note(page, 'team')
    check(`${theme}: 协作成员提醒后重答了：团队卡同样写「纠正过一次」，提醒后仍失败的那位不算进去`,
      teamNudge?.text === '纠正过一次工具调用' && teamNudge.warn && teamNudge.title.includes('检索员')
      && !teamNudge.title.includes('分析员'), JSON.stringify(teamNudge))
    shots.push(await cardShot(page, ['team', 'ask'], `${theme}-endings-nudge-cards`))
    check('没有运行时报错', errors.length === 0, errors.slice(0, 3).join(' | '))
    await browser.close()
  }
})

await section('replay', async () => {
  console.log('\n=== 回放：卡片回到游标那一刻 ===')
  const { browser, page, errors } = await open({ theme: 'dark' })
  await setLod(page, 'full')
  await feed(page, SUCCEEDED(Date.now()), { replay: true })
  await page.waitForTimeout(300)
  // 游标放在三个成员都派出去、最快的那个还没交回的时候
  const at = await page.evaluate(() => {
    const segs = window.__studio.getState().trace.nodes.team.segments.filter((s) => s.kind === 'member')
    return Math.max(...segs.map((s) => s.start)) + 400
  })
  await page.evaluate((at) => window.__studio.getState().setReplayAt(at), at)
  await page.waitForTimeout(300)
  const r = await cards(page)
  check('回放：团队回到运行中', r.team.state === 'running', r.team.state)
  check('回放：人工审批还没到', r.human.state === 'idle', r.human.state)
  const matrix = await page.locator('.react-flow__node[data-id="team"] .team-matrix').innerText()
  check('回放：矩阵里三个人都还在跑', await page.locator('.react-flow__node[data-id="team"] .team-row[data-member-status="running"]').count() === 3,
    matrix.split('\n').slice(0, 2).join(' / '))
  check('回放：还没省下时间就不报', !matrix.includes('省下'), matrix.split('\n').slice(-1)[0])
  await page.waitForTimeout(700)
  const later = (await cards(page)).team.text
  check('回放：计时是定值、不走', /\d\d:\d\d/.test(r.team.text) && r.team.text === later, `${r.team.text} → ${later}`)
  check('回放：印章还没落', await page.locator('.react-flow__node[data-id="out"] .nc-stamp').count() === 0)
  check('回放：还没跑到的分支出口不亮', await page.locator('.react-flow__node[data-id="gate"] .nc-exit.is-hit').count() === 0)
  const memberClock = await page.locator('.react-flow__node[data-id="team"] .team-row[data-member-status="running"] .team-ms').first().innerText()
  check('回放：在跑的成员写那一刻的用时', /^\d\d:\d\d$/.test(memberClock), memberClock)
  check('回放：高度仍不变', Object.values(r).every((c) => c.h > 60))
  shots.push(await shot(page, 'dark-replay-mid'))

  // 游标放进循环体第 3 轮：循环容器在两轮之间，实时时它不转圈、不挂光弧，回放也一样。
  // 汇合节点这时上游都交付了、自己还没开始：和画布、小地图一样是「排队」，不是「未运行」
  const at3 = await page.evaluate(() => {
    const runs = window.__studio.getState().trace.nodes.body.segments.filter((s) => s.kind === 'run')
    return runs[2].start + 200
  })
  await page.evaluate((at) => window.__studio.getState().setReplayAt(at), at3)
  await page.waitForTimeout(300)
  const r3 = await cards(page)
  const halo = (id) => page.locator(`.react-flow__node[data-id="${id}"] .fx-halo`).count()
  check('回放：循环在两轮之间仍是运行中', r3.loop.state === 'running', r3.loop.state)
  check('回放：两轮之间的循环容器不挂光弧（循环体挂）', await halo('loop') === 0 && await halo('body') === 1,
    `loop ${await halo('loop')} / body ${await halo('body')}`)
  check('回放：推导状态和画布一致（汇合节点在排队）', r3.join.state === 'queued', r3.join.state)
  shots.push(await shot(page, 'dark-replay-loop'))
  await page.evaluate(() => window.__studio.getState().setReplayAt(null))
  await page.waitForTimeout(300)
  const back = await cards(page)
  check('回到实时：终态回来了', back.team.state === 'done' && back.out.state === 'done', `${back.team.state} / ${back.out.state}`)
  check('回到实时：印章回来了', await page.locator('.react-flow__node[data-id="out"] .nc-stamp').count() === 1)
  check('回到实时：命中的出口亮回来', await page.locator('.react-flow__node[data-id="gate"] .nc-exit.is-hit').count() === 1)
  check('没有运行时报错', errors.length === 0, errors.slice(0, 3).join(' | '))
  await browser.close()
})

await section('history', async () => {
  console.log('\n=== 打开历史运行：一次性动效不播 ===')
  const { browser, page } = await open({ theme: 'dark' })
  await feed(page, FAILED(Date.now()), { replay: true })
  await page.waitForTimeout(60)
  // 只看卡片上的一次性动效（抖、扫、琥珀一圈、落章）；状态切换带的 200ms 透明度
  // 过渡不算时刻
  const anim = await runningAnimations(page, { transitions: false })
  const moments = anim.names.filter((n) => /fx-shake|fx-done|nc-ring-out|nc-stamp|nc-light|nc-block/.test(n))
  check('补发的历史不抖、不扫、不落章', moments.length === 0, anim.names.join(', ') || '无')
  check('补发的历史不挂 .nc-enter', await page.locator('.nc.nc-enter').count() === 0)
  await browser.close()
})

// ---------------------------------------------------------------- 出口密的分支

await section('exits', async () => {
  console.log('\n=== 六出口分支：标签不压别的出口的线 ===')
  const cases = ['A', 'B', 'C', 'D', 'E'].map((k, i) => ({ key: `c${i}`, condition: `vars.x == ${i}`, label: `产线${k}` }))
  const graph = await (await fetch(`${API}/copilot/layout`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ graph: {
      nodes: [
        { id: 'in', type: 'input', data: { label: '入口', config: { fields: [{ name: 'x' }] } } },
        { id: 'br', type: 'branch', data: { label: '六路分流', config: { mode: 'expression', cases } } },
        ...cases.map((c, i) => ({ id: `o${i}`, type: 'output', data: { label: `出口${i}`, config: { fields: [{ name: 'r' }] } } })),
        { id: 'od', type: 'output', data: { label: '兜底', config: { fields: [{ name: 'r' }] } } },
      ],
      edges: [
        { source: 'in', target: 'br' },
        ...cases.map((c, i) => ({ source: 'br', target: `o${i}`, sourceHandle: c.key })),
        { source: 'br', target: 'od', sourceHandle: 'default' },
      ],
    } }),
  })).json()
  const workflow = { ...WORKFLOW, id: 'wcard-six-exits', name: '__six_exits__', graph }
  for (const theme of ['dark', 'light']) {
    const { browser, page, errors } = await open({ theme, workflow })
    await setLod(page, 'full')
    const info = await page.evaluate(() => {
      const n = document.querySelector('.react-flow__node[data-id="br"]')
      const labels = [...n.querySelectorAll('.nc-exit')].map((el) => { const r = el.getBoundingClientRect(); return { t: el.textContent, top: r.top, bottom: r.bottom } })
      const handles = [...n.querySelectorAll('.react-flow__handle.source')].map((el) => { const r = el.getBoundingClientRect(); return r.top + r.height / 2 })
      return { labels, handles, dense: n.querySelector('.nc').classList.contains('nc-exits-dense') }
    })
    const covers = []
    info.labels.forEach((l, i) => info.handles.forEach((y, j) => {
      if (j !== i && y > l.top && y < l.bottom) covers.push(`${l.t} 压住第 ${j + 1} 条`)
    }))
    const own = info.labels.every((l, i) => info.handles[i] > l.top && info.handles[i] < l.bottom)
    check(`${theme}: 六个出口都画了标签`, info.labels.length === 6 && info.handles.length === 6, `${info.labels.length} / ${info.handles.length}`)
    check(`${theme}: 出口密时标签骑在自己那条线上`, info.dense && own)
    check(`${theme}: 没有标签压住别的出口的线`, covers.length === 0, covers.join('，'))
    const box = await page.locator('.react-flow__node[data-id="br"]').boundingBox()
    const path = `${SHOTS}/${theme}-six-exits.png`
    await page.screenshot({ path, clip: { x: box.x - 20, y: box.y - 30, width: box.width + 200, height: box.height + 60 } })
    shots.push(path)
    check('没有运行时报错', errors.length === 0, errors.slice(0, 3).join(' | '))
    await browser.close()
  }
})

// ---------------------------------------------------------------- 关掉动效

await section('reduced', async () => {
  console.log('\n=== 系统关了动效 ===')
  const { browser, page } = await open({ theme: 'dark', reduced: true })
  await setLod(page, 'full')
  await feed(page, LIVE(Date.now()))
  // 跟随执行会把视口挪到在跑的节点上；截图要看全图，关掉跟随再取一次景
  await page.evaluate(() => window.__studio.getState().setFollow(false))
  await setLod(page, 'full')
  await page.waitForTimeout(600)
  const anim = await runningAnimations(page)
  check('画布上一个在播的动画都没有', countOf(anim) === 0, JSON.stringify(anim))
  const drawn = await page.evaluate(() => ({
    halo: document.querySelector('.fx-halo') ? getComputedStyle(document.querySelector('.fx-halo')).display : 'none',
    tele: document.querySelector('.react-flow__node[data-id="body"] .nc-tele')?.innerText,
    slot: getComputedStyle(document.querySelector('.react-flow__node[data-id="body"] .nc-slot')).backgroundColor,
  }))
  check('光弧不再绘制', drawn.halo === 'none', drawn.halo)
  check('静态下仍看得出在跑：状态字 + 计时', /运行中\s*\d\d:\d\d/.test(drawn.tele ?? ''), JSON.stringify(drawn.tele))
  check('静态下仍看得出在跑：左侧实心状态槽', drawn.slot !== 'rgba(0, 0, 0, 0)', drawn.slot)
  shots.push(await shot(page, 'dark-live-reduced'))
  await browser.close()
})

console.log(`\n截图 ${shots.length} 张：${SHOTS}`)
if (reloads) console.log(`注意：测试中页面被重载或 store 被热更新重建了 ${reloads} 次，失败项请重跑确认`)
console.log(failed ? `\n${failed} 项未通过` : '\n全部通过')
process.exit(failed ? 1 : 0)
