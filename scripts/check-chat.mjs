// 问数据页的检查：会话、轮次、运行全部在浏览器这一侧伪造，页面走的是真的
// chat store / ChatPage / AssistantStream，只有后端换成了脚本。
//
// 为什么不真跑：沙箱没有模型密钥，而且要撞上的恰恰是真跑很难稳定复现的那几种
// 时刻——两个会话同时在跑、答案超过 2000 字被事件截断、跑到一半服务重启、
// 历史取得慢或者取失败。这里每一种都由脚本精确地摆出来。
//
// 守的事：
//   1. 忙不忙按会话算：A 在跑时 B 的按钮是「发送」，停止只停 A 自己，删除保护
//      对准正在跑的那个；
//   2. run.finished 的 output 被截断时，显示和落库都用 GET 运行拿到的完整版；
//      llm.token 不进 events；
//   3. 服务重启：stream.end status=interrupted 且没有待审批，这一轮收成
//      「服务重启，这一轮中断了」，给接着跑，不再转圈；
//   4. 加载中 / 加载失败 / 真的是空的 三种状态分开，加载失败时不能在空态上提问；
//   5. 已取消的轮次没有「接着跑」，续跑被拒时说人话；
//   6. 1440×900 下长会话的输入框和发送键完整可见，整页不多滚；
//   7. 首屏例句不拿系统表造句；可信度信息刷新后还在。
//
// 所有写请求都被拦在浏览器里，不碰任何库。
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'
import { mkdirSync } from 'node:fs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const CHROME = process.env.CHROME_PATH
  ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const SHOTS = process.env.SHOTS ?? '/tmp/agentlab-check-chat'
mkdirSync(SHOTS, { recursive: true })

// 只跑其中几段：ONLY=busy,restart THEMES=dark node scripts/check-chat.mjs
const ONLY = (process.env.ONLY ?? '').split(',').filter(Boolean)
const THEMES = (process.env.THEMES ?? 'light,dark').split(',').filter(Boolean)
const section = (name, title) => {
  if (ONLY.length && !ONLY.includes(name)) return false
  console.log(`\n=== ${title} ===`)
  return true
}

let failed = 0
const check = (name, cond, detail = '') => {
  console.log(`  ${cond ? '✓' : '✗'} ${name}${detail ? ` — ${detail}` : ''}`)
  if (!cond) failed++
}

// ---------------------------------------------------------------- 伪造的数据

const iso = (minsAgo) => new Date(Date.now() - minsAgo * 60_000).toISOString()
const conv = (id, title, turns, minsAgo) => ({
  id, title, kind: 'chat', workflow_id: null, archived: false,
  created_at: iso(minsAgo + 5), last_active_at: iso(minsAgo), turn_count: turns, last_question: title,
})
const LONG = (n) => Array.from({ length: n }, (_, i) =>
  `${i + 1}. 这一段是很长的结论正文，用来把会话撑到超过一屏，检查输入框会不会被挤出视口。`).join('\n')
const turn = (id, question, extra = {}) => ({
  id, seq: 0, question, answer: '', explanation: '', graph: null, run_id: null,
  status: 'done', error: '', review: null, created_at: iso(30), ...extra,
})
const GRAPH = {
  nodes: [
    { id: 'in', type: 'input', position: { x: 0, y: 0 }, data: { label: '问题', config: { fields: [{ name: 'question' }] } } },
    { id: 'ag', type: 'agent', position: { x: 0, y: 100 }, data: { label: '查数', config: { max_steps: 12 } } },
    { id: 'out', type: 'output', position: { x: 0, y: 200 }, data: { label: '成果', config: {} } },
  ],
  edges: [{ source: 'in', target: 'ag' }, { source: 'ag', target: 'out' }],
}

const CONVS = {
  busyA: conv('c0busya', '会话 A：跑得很久的那个', 0, 1),
  idleB: conv('c0idleb', '会话 B：已经答完的那个', 1, 3),
  longC: conv('c0longc', '会话 C：长会话，检查布局', 6, 60),
  slowD: conv('c0slowd', '会话 D：历史取得慢', 2, 90),
  failE: conv('c0faile', '会话 E：历史取失败', 3, 120),
  histF: conv('c0histf', '会话 F：取消过、失败过、不可用的历史', 4, 60 * 30),
  emptyG: conv('c0emptg', '新对话', 0, 60 * 24 * 3),
  truncH: conv('c0trunc', '会话 H：长答案', 0, 60 * 24 * 10),
  restI: conv('c0resti', '会话 I：跑到一半服务重启', 0, 60 * 24 * 12),
  buildJ: conv('c0buildj', '会话 J：建流程就失败', 0, 60 * 24 * 13),
  waitK: conv('c0waitk', '会话 K：停在审批上', 0, 60 * 24 * 14),
  writeL: conv('c0writel', '会话 L：模型正在出字', 0, 60 * 24 * 15),
  metaM: conv('c0metam', '会话 M：刷新之后的可信度', 1, 60 * 24 * 16),
  killN: conv('c0killn', '会话 N：服务被强杀', 0, 60 * 24 * 17),
  suspO: conv('c0suspo', '会话 O：三天前被重启打断的', 1, 60 * 24 * 3),
}
const DETAIL = {
  c0busya: [],
  c0idleb: [turn('tb1', 'B 的问题', { answer: 'B 的答案', graph: GRAPH, run_id: 'run-b1' })],
  c0longc: Array.from({ length: 6 }, (_, i) =>
    turn(`tc${i}`, `第 ${i + 1} 个问题`, { answer: LONG(14), graph: GRAPH, run_id: `run-c${i}` })),
  c0slowd: [turn('td1', 'D 的第一问', { answer: 'D 的答案一' }), turn('td2', 'D 的第二问', { answer: 'D 的答案二' })],
  c0faile: [turn('te1', 'E 的第一问', { answer: 'E 的答案' })],
  c0histf: [
    // 用户点过停止的老数据：status=error + 「已取消」，运行在后端是 cancelled
    turn('tf1', '被我停下的那一轮', { status: 'error', error: '已取消', graph: GRAPH, run_id: 'run-cancelled' }),
    // 运行失败了：可以接着跑，但这次后端会拒绝（比如别处已经把它续跑完了）
    turn('tf2', '跑挂了的那一轮', { status: 'error', error: 'KeyError: 查询超时，数据库没有在 30 秒内返回', graph: GRAPH, run_id: 'run-failed' }),
    // 没有运行、没有图，却有答案：reply，没查库。老数据没有 meta，靠推断
    turn('tf3', '上一轮的数字是多少', { answer: '上一轮查到的是 42。' }),
    // 复核判为不可用，原因是步数用满
    turn('tf4', '步数用满的那一轮', {
      answer: '只查了一半的结论', graph: GRAPH, run_id: 'run-broken',
      review: {
        verdict: 'annotated', note: 'agent 用满了 12 步还没给出结论。', answer: null, retry: true, severity: 'broken',
        signals: [{ kind: 'step_limit', detail: 'agent 用满了 12 步', severity: 'broken' }],
        meta: { v: 1, runId: 'run-broken', runClass: 'exploratory', runStatus: 'succeeded', queries: 3, ms: 41000 },
      },
    }),
  ],
  c0emptg: [],
  c0trunc: [],
  c0resti: [],
  c0buildj: [],
  c0waitk: [],
  c0writel: [],
  c0killn: [],
  // 三天前服务重启打断、跑了 42 秒的那一轮
  c0suspo: [turn('to1', '三天前没跑完的问题', {
    status: 'error', error: '服务重启，这一轮中断了', graph: GRAPH, run_id: 'run-susp', created_at: iso(60 * 24 * 3),
    review: { meta: { v: 1, runId: 'run-susp', runStatus: 'interrupted', outcome: 'suspended', ms: 42000 } },
  })],
  // 出具档位、运行类别、耗时、查库次数只存在 review.meta 里：刷新后得从这里读回来，
  // 而不是再去问一遍运行
  c0metam: [turn('tm1', '上个月各产线的出勤率', {
    answer: '一线 96.2%，二线 91.0%。', graph: GRAPH, run_id: 'run-meta',
    review: { meta: {
      v: 1, runId: 'run-meta', runClass: 'exploratory', runStatus: 'succeeded', ms: 12300, queries: 2,
      issuance: { tier: 'degraded', gaps: ['叙述模板渲染为空'], matched_numbers: 2, metrics_checked: 3, unmatched_numbers: [] },
    } },
  })],
}
const RUNS = {
  'run-cancelled': { status: 'cancelled', output: {}, error: null },
  'run-failed': { status: 'failed', output: {}, error: '查询超时' },
  'run-broken': { status: 'succeeded', output: { answer: '只查了一半的结论' }, error: null },
}
const FULL = '这是完整答案的开头。' + '正文'.repeat(1920) + '【完整结尾】'   // 3,850 字左右
const SOURCES = [
  { id: 'ds-mig', name: 'shop', kind: 'mysql', description: '' },
  { id: 'ds-bi', name: 'bi', kind: 'oracle', description: '示例 BI 库：采购、物料、生产、设备' },
]
const TABLES = {
  'ds-mig': ['__drizzle_migrations', '_migrations', 'admin_audit_log', 'app_log', 'test_sales', 'user', 'orders'],
  'ds-bi': ['ANALYTICS.test_sales', 'ANALYTICS.job_etl_audit', 'ANALYTICS.v_demo_table'],
}

// ---------------------------------------------------------------- 伪造的后端

const log = { patches: [], cancels: [], continues: [], runStarts: [], archived: [], posts: [], approvalGets: [], runGets: [] }
const ctl = { slowMs: 0, failE: true, wsClosed: {}, approvals: [], thinkGo: false, writingGo: false, writingAt: 0 }
let runSeq = 0

async function fakeApi(route) {
  const req = route.request()
  const url = new URL(req.url())
  const path = url.pathname.replace(/^\/api/, '')
  const method = req.method()
  const json = (body, status = 200) => route.fulfill({ status, json: body })
  const body = () => { try { return req.postDataJSON() } catch { return {} } }

  if (path === '/conversations' && method === 'GET') return json(Object.values(CONVS))
  if (path === '/conversations' && method === 'POST') return json({ ...conv('c0new', '新对话', 0, 0), turns: [] }, 201)
  let m = path.match(/^\/conversations\/([^/]+)$/)
  if (m && method === 'GET') {
    const id = m[1]
    if (id === 'c0slowd') await new Promise((r) => setTimeout(r, ctl.slowMs))
    if (id === 'c0faile' && ctl.failE) return json({ detail: '数据库暂时不可用' }, 500)
    const known = Object.values(CONVS).find((c) => c.id === id)
    if (!known && id !== 'c0new') return json({ detail: '这个对话不存在，可能已经被删了' }, 404)
    return json({ ...known, turns: DETAIL[id] ?? [] })
  }
  if (m && method === 'PATCH') { log.archived.push([m[1], body()]); return json({ ...CONVS[m[1]], ...body() }) }
  m = path.match(/^\/conversations\/([^/]+)\/turns$/)
  if (m && method === 'POST') {
    log.posts.push(m[1])
    return json(turn(`srv-${m[1]}-${log.posts.length}`, body().question, { status: 'running' }), 201)
  }
  m = path.match(/^\/conversations\/([^/]+)\/turns\/([^/]+)$/)
  if (m && method === 'PATCH') { log.patches.push({ conv: m[1], turn: m[2], body: body() }); return json(turn(m[2], '', body())) }

  if (path === '/copilot/generate-stream' && method === 'POST') {
    if (String(body().instruction ?? '').includes('建流程就失败')) {
      // 后端的 error 操作是三段：人话、怎么办、原始异常
      const ops = [
        { op: 'heartbeat', phase: 'planning', elapsed_ms: 400 },
        { op: 'error', message: '模型交回的工作流结构不对：第 2 个节点的类型「sql」不存在',
          hint: '再试一次，或者把需求说得更具体些',
          detail: "2 validation errors for GraphSpec\nnodes.1.type\n  Input should be 'input', 'output', 'llm' [type=literal_error]" },
      ]
      return route.fulfill({ status: 200, contentType: 'text/event-stream',
        body: ops.map((o) => `data: ${JSON.stringify(o)}\n\n`).join('') })
    }
    const ops = [
      { op: 'heartbeat', phase: 'planning', elapsed_ms: 800 },
      { op: 'plan', summary: '查一下再回答' },
      ...GRAPH.nodes.map((n) => ({ op: 'add_node', node: n })),
      { op: 'done', explanation: '一张三步的图' },
      { op: 'final', graph: GRAPH, issues: [], explanation: '一张三步的图' },
    ]
    return route.fulfill({ status: 200, contentType: 'text/event-stream',
      body: ops.map((o) => `data: ${JSON.stringify(o)}\n\n`).join('') })
  }
  if (path === '/copilot/review' && method === 'POST') {
    return json({ verdict: 'ok', note: '', answer: null, retry: false, severity: '', signals: [] })
  }
  if (path === '/runs' && method === 'POST') {
    const b = body()
    const conversation = String(b.input?.question ?? '')
    const id = conversation.includes('长答案') ? 'run-long'
      : conversation.includes('审批') ? 'run-wait'
      : conversation.includes('重启') ? 'run-restart'
      : conversation.includes('正在写') ? 'run-writing'
      : conversation.includes('强杀') ? 'run-killed'
      : conversation.includes('步数用满') ? `run-steps-${++runSeq}`
      : `run-busy-${++runSeq}`
    log.runStarts.push({ id, graph: b.graph })
    return json({ id, status: 'queued', run_class: 'exploratory', output: {}, usage: {} })
  }
  m = path.match(/^\/runs\/([^/]+)\/(cancel|continue)$/)
  if (m && method === 'POST') {
    if (m[2] === 'cancel') { log.cancels.push(m[1]); return json({ ok: true }) }
    log.continues.push(m[1])
    if (m[1] === 'run-restart' || m[1] === 'run-susp') return json({ id: m[1], status: 'running' })
    return json({ detail: '这次运行已经完成，不能再接着跑。要重来，请重新发起一次运行。' }, 409)
  }
  m = path.match(/^\/runs\/([^/]+)$/)
  if (m && method === 'GET') {
    const id = m[1]
    log.runGets.push(id)
    if (id === 'run-long') return json({ id, status: 'succeeded', run_class: 'exploratory', output: { answer: FULL }, usage: {} })
    if (RUNS[id]) return json({ id, run_class: 'exploratory', usage: {}, ...RUNS[id] })
    if (id.startsWith('run-c') || id === 'run-b1') return json({ id, status: 'succeeded', run_class: 'exploratory', output: {}, usage: {} })
    return json({ id, status: 'interrupted', run_class: 'exploratory', output: {}, usage: {} })
  }
  if (path.startsWith('/approvals')) {
    log.approvalGets.push(Date.now())
    const runId = url.searchParams.get('run_id')
    return json(ctl.approvals.filter((a) => !runId || a.run_id === runId))
  }
  m = path.match(/^\/runs\/([^/]+)\/events$/)
  if (m && method === 'GET') {
    return json([ev(1, 'run.started', null, { nodes: 3 }), ev(2, 'node.started', 'ag', {}),
      ev(3, 'node.finished', 'ag', { duration_ms: 700 }), ev(4, 'run.finished', null, { output: {} })])
  }
  if (path === '/datasources' && method === 'GET') return json(SOURCES)
  m = path.match(/^\/datasources\/([^/]+)\/schema$/)
  if (m) return json({ tables: TABLES[m[1]] ?? [] })

  if (method === 'GET') return route.continue()
  return route.abort()   // 其余写操作一律不放
}

/** 每条运行的事件剧本。done=true 时剧本放完就发 stream.end */
const ev = (seq, type, node_id, data = {}) => ({ seq, type, node_id, data, ts: Date.now() / 1000 })
function script(runId, after = 0) {
  if (runId === 'run-long') {
    return {
      events: [
        ev(1, 'run.started', null, { nodes: 3 }),
        ev(2, 'node.started', 'ag', {}),
        ...Array.from({ length: 60 }, (_, i) => ev(3 + i, 'llm.token', 'ag', { delta: '字' })),
        ev(70, 'node.finished', 'ag', { duration_ms: 1200 }),
        ev(71, 'run.finished', null, { output: { answer: FULL.slice(0, 2000) }, output_truncated: true }),
      ],
      end: 'succeeded',
    }
  }
  if (runId === 'run-restart' && after >= 4) {
    // 接着跑：从断点往下，这次跑完
    return {
      events: [
        ev(5, 'run.resumed', null, { from: 'ag', message: '从「查数」接着跑…' }),
        ev(6, 'node.started', 'ag', { resumed: true }),
        ev(7, 'tool.start', 'ag', { tool: 'db_query', call_id: 'q2', args: { sql: 'select 1' } }),
        ev(8, 'tool.end', 'ag', { tool: 'db_query', call_id: 'q2', rows: 3 }),
        ev(9, 'node.finished', 'ag', { duration_ms: 800 }),
        ev(10, 'run.finished', null, { output: { answer: '接着跑完了：三条记录。' } }),
      ],
      end: 'succeeded',
    }
  }
  if (runId === 'run-killed') {
    // 进程被强杀：连结束标记都来不及发，连接直接断了。前端自己重连，
    // 重启后的后端回放完补一条 stream.end status=interrupted
    if (after >= 2) return { events: [], end: 'interrupted' }
    return { events: [ev(1, 'run.started', null, { nodes: 3 }), ev(2, 'node.started', 'ag', {})], end: 'close' }
  }
  if (runId === 'run-restart') {
    return {
      events: [
        ev(1, 'run.started', null, { nodes: 3 }),
        ev(2, 'node.started', 'ag', {}),
        ev(3, 'tool.start', 'ag', { tool: 'db_query', call_id: 'q1', args: { sql: 'select 1' } }),
        ev(4, 'log', null, { level: 'warn', code: 'server_shutdown', message: '服务正在关停' }),
      ],
      end: 'interrupted',
    }
  }
  if (runId === 'run-wait') {
    // 停在审批上：事件流不关，后端也不发 stream.end
    return {
      events: [
        ev(1, 'run.started', null, { nodes: 3 }),
        ev(2, 'node.started', 'ag', {}),
        ev(3, 'human.requested', 'ag', { interrupt_id: 'ap-1', payload: { title: '确认一下再查' } }),
        ev(4, 'run.interrupted', null, { node_id: 'ag' }),
      ],
      end: null,
    }
  }
  if (runId === 'run-writing') {
    // 先想、再写，写完不收尾：一次还在出字的模型调用。gate 处等脚本放行
    // 第一段停在半句上：半句不该出现在头部，要等它写完
    const tokens = [...Array.from({ length: 12 }, () => '字'.repeat(100)), '字'.repeat(34)]   // 1,234 字
    return {
      events: [
        ev(1, 'run.started', null, { nodes: 3 }),
        ev(2, 'node.started', 'ag', {}),
        ev(3, 'llm.start', 'ag', { model: 'm' }),
        ev(4, 'llm.thinking.delta', 'ag', { delta: '先看看 bi 里有哪些表。' }),
        ev(5, 'llm.thinking.delta', 'ag', { delta: '然后按月汇总出勤' }),
        { gate: 'thinkGo' },
        ev(6, 'llm.thinking.delta', 'ag', { delta: '率，再和上个月比。' }),
        { gate: 'writingGo' },
        ...tokens.map((delta, i) => ev(10 + i, 'llm.token', 'ag', { delta })),
      ],
      end: null,
    }
  }
  if (runId.startsWith('run-steps')) {
    return {
      events: [
        ev(1, 'run.started', null, { nodes: 3 }),
        ev(2, 'node.started', 'ag', {}),
        ev(3, 'node.finished', 'ag', { duration_ms: 900 }),
        ev(4, 'run.finished', null, { output: { answer: '放宽步数之后查全了' } }),
      ],
      end: 'succeeded',
    }
  }
  // 忙的那条：开了头就停住，流不关，像一次还在跑的运行
  return { events: [ev(1, 'run.started', null, { nodes: 3 }), ev(2, 'node.started', 'ag', {})], end: null }
}

async function fakeStream(ws) {
  const runId = new URL(ws.url()).pathname.split('/')[3]
  ctl.wsClosed[runId] = false
  ws.onClose(() => { ctl.wsClosed[runId] = true })
  const after = Number(new URL(ws.url()).searchParams.get('after') ?? 0)
  const { events, end } = script(runId, after)
  for (const e of events) {
    if (e.gate) {
      await until(() => ctl[e.gate], 15000)
      continue
    }
    if (e.seq <= after) continue
    if (runId === 'run-writing' && e.type === 'llm.token' && !ctl.writingAt) ctl.writingAt = Date.now()
    // 审批和中断同时产生：后端在停下的那一刻才建出这条待审批
    if (runId === 'run-wait' && e.type === 'run.interrupted') {
      ctl.approvals = [ctl.pendingApproval]
      ctl.interruptedAt = Date.now()
    }
    ws.send(JSON.stringify(e))
    await new Promise((r) => setTimeout(r, 15))
  }
  if (end === 'close') ws.close()
  else if (end) ws.send(JSON.stringify({ type: 'stream.end', status: end, data: { status: end } }))
}

// ---------------------------------------------------------------- 浏览器

const browser = await chromium.launch({ executablePath: CHROME })

async function open(theme = 'light', { width = 1440, height = 900, reducedMotion = 'no-preference' } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height }, colorScheme: theme, reducedMotion })
  await ctx.addInitScript((t) => localStorage.setItem('agentlab.theme', t), theme)
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  await page.route('**/api/settings', async (route) => {
    if (route.request().method() !== 'GET') return route.abort()
    // 主题以设置为准（沙箱里存的是 light），这里按要看的那套改掉
    return route.fulfill({ json: { ui: { theme }, run: {}, limits: { max_agent_steps: 25 }, copilot: {}, embedding: {} } })
  })
  await page.route(/\/api\/(conversations|copilot\/(generate-stream|review)|runs|approvals|datasources)(\/|\?|$)/, fakeApi)
  await page.routeWebSocket(/\/api\/runs\/[^/]+\/stream/, fakeStream)
  return { page, ctx, errors }
}

const goto = async (page, id) => {
  await page.goto(`${WEB}/chat/${id}`)
  await page.waitForFunction(() => !!window.__chat, null, { timeout: 15000 })
}
const shows = (page, text, timeout = 6000) =>
  page.getByText(text).first().waitFor({ timeout }).then(() => true, () => false)
const chatState = (page) => page.evaluate(() => window.__chat.getState())
/** 等脚本这一侧的某个条件成立（伪造后端记下的请求、事件流的开关） */
const until = async (cond, ms = 5000) => {
  const end = Date.now() + ms
  while (!cond() && Date.now() < end) await new Promise((r) => setTimeout(r, 50))
  return cond()
}
const send = async (page, q) => {
  const box = page.getByRole('textbox', { name: '向数据提问' })
  await box.fill(q)
  await box.press('Enter')
}

for (const theme of THEMES) {
  console.log(`\n######## ${theme} ########`)

  if (section('hero', '首屏：输入框是主角，例句不拿系统表造句')) {
    const { page, ctx, errors } = await open(theme)
    await goto(page, 'c0emptg')
    check('空会话显示首屏', await shows(page, '问你的数据'))
    const hints = await page.locator('button[title="填进输入框，改一改再发"]').allInnerTexts()
    check('有例句', hints.length > 0, hints.join(' | '))
    check('例句里没有系统表',
      hints.every((h) => !/__drizzle|_migrations|test_|etl_audit|_log\b/.test(h)), hints.join(' | '))
    check('描述里的业务词造了句', hints.some((h) => /bi：/.test(h)), hints.join(' | '))
    const ph = await page.getByRole('textbox', { name: '向数据提问' }).getAttribute('placeholder')
    check('占位符简短', !!ph && ph.length <= 12, ph ?? '')
    const box = await page.getByRole('textbox', { name: '向数据提问' }).boundingBox()
    check('输入框在首屏中部，不贴底', !!box && box.y > 200 && box.y + box.height < 700, JSON.stringify(box))
    check('数据源入口指向 /data', await page.locator('a[href="/data"]').count() > 0)
    const postsBefore = log.posts.length
    await page.locator('button[title="填进输入框，改一改再发"]').first().click()
    await page.waitForTimeout(200)
    check('点例句只是填进输入框', (await page.getByRole('textbox', { name: '向数据提问' }).inputValue()).length > 0
      && log.posts.length === postsBefore)
    await page.screenshot({ path: `${SHOTS}/hero-${theme}.png` })
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  }

  if (section('states', '三种状态：加载中 / 加载失败 / 空')) {
    const { page, ctx, errors } = await open(theme)
    ctl.slowMs = 2500
    await page.goto(`${WEB}/chat/c0slowd`)
    await page.waitForFunction(() => !!window.__chat, null, { timeout: 15000 })
    await page.waitForTimeout(600)
    check('取历史时画骨架', await page.locator('[aria-busy="true"]').count() > 0)
    check('取历史时不显示首屏', !(await page.getByText('问你的数据').count()))
    const blocked = await page.getByRole('button', { name: '发送' }).isDisabled()
    check('取历史时发不出去', blocked)
    await page.screenshot({ path: `${SHOTS}/loading-${theme}.png` })
    check('历史回来了', await shows(page, 'D 的答案二', 5000))
    ctl.slowMs = 0

    ctl.failE = true
    const before = log.posts.length
    await goto(page, 'c0faile')
    check('取失败时给出错误和重试', await shows(page, '数据库暂时不可用'))
    check('取失败时不显示首屏', !(await page.getByText('问你的数据').count()))
    await send(page, '失败时问一句')
    await page.waitForTimeout(400)
    check('取失败时不能在空态上提问', log.posts.length === before, `${log.posts.length - before} 次开轮`)
    await page.screenshot({ path: `${SHOTS}/load-failed-${theme}.png` })
    ctl.failE = false
    await page.getByRole('button', { name: '重试' }).first().click()
    check('重试后历史回来', await shows(page, 'E 的答案'))
    check('草稿还在', (await page.getByRole('textbox', { name: '向数据提问' }).inputValue()) === '失败时问一句')
    ctl.failE = true

    // 取失败（500、断网）留在原地给重试；只有「这个对话不存在」（404）才把人带走
    await page.goto(`${WEB}/chat/c0gone`)
    check('指向不存在的对话：说清楚为什么换了地方', await shows(page, '那个对话不在了'))
    check('落到一个真实的对话', await page.waitForURL(/\/chat\/c0[a-z]+$/, { timeout: 5000 }).then(() => !page.url().endsWith('c0gone'), () => false), page.url())
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  }

  if (section('layout', '布局：长会话里输入框和发送键完整可见（1440×900，另看 1024×700）')) {
    for (const [width, height] of [[1440, 900], [1024, 700]]) {
      const { page, ctx } = await open(theme, { width, height })
      await goto(page, 'c0longc')
      await shows(page, '第 6 个问题')
      await page.waitForTimeout(500)
      const m = await page.evaluate(() => {
        const ta = document.querySelector('textarea')?.getBoundingClientRect()
        const btn = document.querySelector('button[aria-label="发送"]')?.getBoundingClientRect()
        return { ta: ta && [ta.top, ta.bottom], btn: btn && [btn.top, btn.bottom], h: innerHeight,
                 scroll: document.documentElement.scrollHeight, wide: document.documentElement.scrollWidth, w: innerWidth }
      })
      const at = `${width}×${height}`
      check(`${at} 输入框底边在视口内`, !!m.ta && m.ta[1] <= m.h, JSON.stringify(m))
      check(`${at} 发送键完整可见`, !!m.btn && m.btn[0] >= 0 && m.btn[1] <= m.h, JSON.stringify(m.btn))
      check(`${at} 整页不多滚`, m.scroll <= m.h && m.wide <= m.w, `${m.wide}×${m.scroll} / ${m.w}×${m.h}`)
      await page.screenshot({ path: `${SHOTS}/long-${width}-${theme}.png` })
      await ctx.close()
    }
  }

  if (section('busy', '忙不忙按会话算')) {
    const { page, ctx, errors } = await open(theme)
    const cancelsBefore = log.cancels.length
    await goto(page, 'c0busya')
    await send(page, '一个会跑很久的问题')
    check('A 在跑：按钮是停止', await page.getByRole('button', { name: '停止这一轮' })
      .waitFor({ timeout: 8000 }).then(() => true, () => false))
    // 停止键在建图时就出现了；要等运行真的起来、事件流接上，才谈得上"别的会话别碰它"
    await page.waitForFunction(() => !!window.__chat.getState().byConversation.c0busya?.at(-1)?.run?.id,
      null, { timeout: 8000 }).catch(() => {})
    const runA = (await chatState(page)).byConversation.c0busya.at(-1).run?.id
    await until(() => ctl.wsClosed[runA] === false)
    await page.getByRole('button', { name: /^会话 B/ }).click()
    await shows(page, 'B 的答案')
    check('切到 B：按钮是发送，不是停止',
      await page.getByRole('button', { name: '发送' }).count() === 1
      && await page.getByRole('button', { name: '停止这一轮' }).count() === 0)
    check('B 里说清 A 还在跑', await shows(page, '正在运行，这里可以照常提问'))
    const rowA = page.locator('aside[aria-label="对话列表"] div.group', { hasText: '会话 A' })
    check('列表里 A 标着运行中', (await rowA.innerText()).includes('运行中'))
    await rowA.hover()
    check('A 正在跑，不能删', await rowA.getByRole('button', { name: /删除/ }).isDisabled())
    const rowB = page.locator('aside[aria-label="对话列表"] div.group', { hasText: '会话 B' })
    await rowB.hover()
    check('B 空闲，可以删', !(await rowB.getByRole('button', { name: /删除/ }).isDisabled()))
    await page.screenshot({ path: `${SHOTS}/busy-elsewhere-${theme}.png` })
    check('在 B 里什么都没停：A 的事件流还开着', ctl.wsClosed[runA] === false, runA)
    check('A 的运行没被取消', log.cancels.length === cancelsBefore)

    await page.getByRole('link', { name: '去看看' }).click()
    await page.getByRole('button', { name: '停止这一轮' }).click()
    await until(() => log.cancels.length > cancelsBefore && ctl.wsClosed[runA] === true)
    check('停止只取消 A 自己的运行', log.cancels.slice(cancelsBefore).join() === runA, log.cancels.slice(cancelsBefore).join())
    check('A 的事件流关了', ctl.wsClosed[runA] === true)
    const a = (await chatState(page)).byConversation.c0busya.at(-1)
    check('A 这一轮收成已取消', a.phase === 'cancelled', a.phase)
    check('已取消的轮次没有「接着跑」', !(await page.getByRole('button', { name: '接着跑' }).count()))
    check('已取消的轮次给「重跑这一轮」', await page.getByRole('button', { name: '重跑这一轮' }).count() === 1)
    const b = (await chatState(page)).byConversation.c0idleb
    check('B 的轮次没被动过', b.length === 1 && b[0].phase === 'done')
    await page.waitForTimeout(300)   // 「去看看」刚切过来，卡片的入场动效还没播完
    await page.screenshot({ path: `${SHOTS}/cancelled-${theme}.png` })
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  }

  if (section('truncated', '长答案：事件里截断了，用 GET 运行的完整版')) {
    const { page, ctx, errors } = await open(theme)
    const patchesBefore = log.patches.length
    await goto(page, 'c0trunc')
    await send(page, '给我一份长答案')
    check('答完了', await page.waitForFunction(
      () => window.__chat.getState().byConversation.c0trunc?.at(-1)?.phase === 'done', null, { timeout: 10000 })
      .then(() => true, () => false))
    const t = (await chatState(page)).byConversation.c0trunc.at(-1)
    check('显示的是完整答案', String(t.output?.answer ?? '').length === FULL.length, `${String(t.output?.answer ?? '').length} / ${FULL.length}`)
    check('llm.token 没进 events', !t.events.some((e) => e.type === 'llm.token'), `${t.events.length} 条`)
    const saved = log.patches.slice(patchesBefore).filter((p) => typeof p.body.answer === 'string' && p.body.answer)
    check('落库的也是完整答案', saved.length > 0 && saved.every((p) => p.body.answer.length === FULL.length),
      saved.map((p) => p.body.answer.length).join(','))
    // 落库是排队异步发的，交付之后再等它一下
    await until(() => log.patches.slice(patchesBefore).some((p) => p.body.review?.meta))
    const metas = log.patches.slice(patchesBefore).map((p) => p.body.review?.meta).filter(Boolean)
    const meta = metas.at(-1)
    check('可信度信息跟着落库（运行、类别、查库次数）', meta?.runId === 'run-long' && meta?.runClass === 'exploratory',
      JSON.stringify(metas))
    await page.getByText('展开全部').first().click().catch(() => {})
    check('完整结尾在页面上', await shows(page, '【完整结尾】'))
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  }

  if (section('restart', '服务重启：interrupted 且没有待审批，收尾而不是转圈')) {
    const { page, ctx, errors } = await open(theme)
    await goto(page, 'c0resti')
    await send(page, '跑到一半服务重启')
    check('说清是服务重启打断的', await shows(page, '服务重启，这一轮中断了', 8000))
    await page.waitForTimeout(300)
    const t = (await chatState(page)).byConversation.c0resti.at(-1)
    check('这一轮收成挂起', t.phase === 'suspended', t.phase)
    const spinning = await page.locator('main .animate-spin, [role="main"] .animate-spin').count()
      + await page.locator('.animate-spin').count()
    check('不再转圈', spinning === 0, `${spinning} 个转圈`)
    check('给「接着跑」', await page.getByRole('button', { name: '接着跑' }).count() === 1)
    check('给「重跑这一轮」', await page.getByRole('button', { name: '重跑这一轮' }).count() === 1)
    check('输入框可以接着问', await page.getByRole('button', { name: '停止这一轮' }).count() === 0)
    await page.screenshot({ path: `${SHOTS}/restart-${theme}.png` })

    await page.getByRole('button', { name: '接着跑' }).click()
    check('接着跑：从断点续上并跑完', await page.waitForFunction(
      () => window.__chat.getState().byConversation.c0resti?.at(-1)?.phase === 'done', null, { timeout: 8000 })
      .then(() => true, () => false), (await chatState(page)).byConversation.c0resti.at(-1).phase)
    check('续跑发给的是这一轮自己的运行', log.continues.at(-1) === 'run-restart', log.continues.join(','))
    check('续上之后答案出来了', await shows(page, '接着跑完了：三条记录。'))
    check('续上之后补救按钮收起', await page.getByRole('button', { name: '接着跑' }).count() === 0)
    await page.waitForTimeout(300)
    await page.screenshot({ path: `${SHOTS}/restart-continued-${theme}.png` })

    // 强杀：连结束标记都没有，连接直接断。重连上重启后的后端，照样收成中断
    await goto(page, 'c0killn')
    await send(page, '一个跑着跑着服务被强杀的问题')
    check('连接直接断了：重连之后收成中断', await page.waitForFunction(
      () => window.__chat.getState().byConversation.c0killn?.at(-1)?.phase === 'suspended', null, { timeout: 8000 })
      .then(() => true, () => false), (await chatState(page)).byConversation.c0killn?.at(-1)?.phase)
    check('强杀之后同样给「接着跑」', await page.getByRole('button', { name: '接着跑' }).count() === 1)
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  }

  if (section('resume', '隔了几天再接着跑：计时接着上次的走')) {
    const { page, ctx, errors } = await open(theme)
    await goto(page, 'c0suspo')
    check('从库里恢复的中断轮次认成中断', await shows(page, '服务重启，这一轮中断了'))
    const card = page.locator('[data-remedy="suspended"]')
    await card.getByRole('button', { name: '接着跑' }).click()
    const clock = page.locator('[data-turn="to1"] [title="已运行"]')
    await clock.waitFor({ timeout: 6000 }).catch(() => {})
    const text = await clock.innerText().catch(() => '')
    check('计时从 42 秒接着走，不是从三天前算起', /^00:4\d/.test(text), text)
    await page.screenshot({ path: `${SHOTS}/resumed-${theme}.png` })
    await page.getByRole('button', { name: '停止这一轮' }).click()
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  }

  if (section('history', '历史：已取消、续跑被拒、没查库、放宽步数')) {
    const { page, ctx, errors } = await open(theme)
    await goto(page, 'c0histf')
    await shows(page, '步数用满的那一轮')
    await page.waitForTimeout(800)
    const turns = (await chatState(page)).byConversation.c0histf
    check('老数据里的「已取消」认成已取消', turns[0].phase === 'cancelled', turns[0].phase)
    const cancelledCard = page.locator('[data-remedy="cancelled"]')
    check('已取消的那一轮没有「接着跑」', await cancelledCard.count() === 1
      && !(await cancelledCard.getByRole('button', { name: '接着跑' }).count()))
    check('失败那一轮按运行状态给「接着跑」', turns[1].runStatus === 'failed'
      && await page.locator('[data-remedy="failed"]').getByRole('button', { name: '接着跑' }).count() === 1)
    const failedCard = await page.locator('[data-turn="tf2"]').innerText()
    check('失败原因说人话、不露 Python 类名', failedCard.includes('查询超时') && !failedCard.includes('KeyError'),
      failedCard.split('\n').slice(0, 4).join(' / '))
    // 人话只翻一遍：翻好的「操作超时：查询超时…」再翻一遍，标题和原因会各写一次「操作超时」
    check('报错标题和原因不重复', !failedCard.includes('操作超时：'), failedCard.split('\n').slice(2, 6).join(' / '))
    check('没查库的老答案标出来了', await shows(page, '这一条没有查库'))
    const stepBtn = page.getByRole('button', { name: /放宽步数重跑（12 → 24 步）/ })
    check('步数用满给出「放宽步数重跑」并写明目标值', await stepBtn.count() === 1)

    await page.locator('[data-turn="tf1"]').scrollIntoViewIfNeeded()
    await page.screenshot({ path: `${SHOTS}/history-${theme}.png` })
    await page.locator('[data-remedy="failed"]').getByRole('button', { name: '接着跑' }).click()
    check('续跑被拒时说人话', await shows(page, '不能再接着跑'))
    await page.waitForTimeout(400)
    await page.screenshot({ path: `${SHOTS}/continue-refused-${theme}.png` })

    const startsBefore = log.runStarts.length
    await stepBtn.click()
    check('放宽步数：原图直接重跑，步数调到 24', await page.waitForFunction(
      (n) => window.__chat.getState().byConversation.c0histf.at(-1).phase === 'done'
        && window.__chat.getState().byConversation.c0histf.at(-1).attempts?.length === 1, startsBefore, { timeout: 8000 })
      .then(() => true, () => false))
    const started = log.runStarts.slice(startsBefore).at(-1)
    check('发起的图里 agent 步数是 24', started?.graph?.nodes?.find((n) => n.type === 'agent')?.data?.config?.max_steps === 24,
      JSON.stringify(started?.graph?.nodes?.map((n) => n.data?.config)))
    check('上一次留档：第 1 次尝试 · 结论不可用', await shows(page, '第 1 次尝试'))
    await page.screenshot({ path: `${SHOTS}/retried-${theme}.png` })
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  }

  if (section('build', '建流程就失败：说人话、原文收进技术细节、给补救')) {
    const { page, ctx, errors } = await open(theme)
    await goto(page, 'c0buildj')
    await send(page, '一个建流程就失败的问题')
    check('说出发生了什么', await shows(page, '模型交回的工作流结构不对'))
    check('说出怎么办', await shows(page, '再试一次，或者把需求说得更具体些'))
    const card = await page.locator('[data-turn]').last().innerText()
    check('原始异常不直接摆出来', !card.includes('validation errors'), card.split('\n').slice(0, 5).join(' / '))
    check('原文在「技术细节」里', await page.locator('[data-turn] details', { hasText: '技术细节' }).count() > 0)
    const remedy = page.locator('[data-remedy="failed"]')
    check('没有运行就没有「接着跑」', !(await remedy.getByRole('button', { name: '接着跑' }).count()))
    check('给「重试这一轮」', await remedy.getByRole('button', { name: '重试这一轮' }).count() === 1)
    await remedy.getByRole('button', { name: '换个说法' }).click()
    const box = page.getByRole('textbox', { name: '向数据提问' })
    await page.waitForTimeout(150)
    const filled = await box.inputValue()
    const focused = await box.evaluate((el) => el === document.activeElement)
    check('「换个说法」把原问题填回输入框并聚焦', filled === '一个建流程就失败的问题' && focused,
      `${filled} · ${focused ? '已聚焦' : '没聚焦'}`)
    await page.screenshot({ path: `${SHOTS}/build-failed-${theme}.png` })
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  }

  if (section('approval', '停在审批上：审批卡立刻出现，不等 4 秒轮询')) {
    const { page, ctx, errors } = await open(theme)
    ctl.approvals = []
    ctl.pendingApproval = {
      id: 'ap-1', run_id: 'run-wait', node_id: 'ag', mode: 'approve', title: '确认一下再查',
      payload: { title: '确认一下再查' }, status: 'pending', response: {}, created_at: new Date().toISOString(),
      workflow_name: '问数据', node_label: '查数', run_status: 'interrupted', run_class: 'exploratory',
    }
    ctl.interruptedAt = 0
    await goto(page, 'c0waitk')
    await send(page, '一个要审批的问题')
    const seen = await page.locator('[data-approval-slot] button', { hasText: /通过|放行|批准/ }).first()
      .waitFor({ timeout: 6000 }).then(() => true, () => false)
    const lag = ctl.interruptedAt ? Date.now() - ctl.interruptedAt : -1
    // 全局轮询是 4 秒一次；中断时立刻去拿的话，一秒内就该出来
    check('审批卡在收到中断后马上出现（不等 4 秒轮询）', seen && lag >= 0 && lag < 1500, `${lag} ms`)
    await page.waitForFunction(() => window.__chat.getState().byConversation.c0waitk?.at(-1)?.phase === 'waiting',
      null, { timeout: 3000 }).catch(() => {})
    const t = (await chatState(page)).byConversation.c0waitk.at(-1)
    check('这一轮是等待审批，不算占着会话', t.phase === 'waiting'
      && await page.getByRole('button', { name: '停止这一轮' }).count() === 0, t.phase)
    await page.screenshot({ path: `${SHOTS}/waiting-${theme}.png` })
    ctl.approvals = []
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  }

  if (section('steps', '历史轮次的执行过程：点开、收起')) {
    const { page, ctx, errors } = await open(theme)
    await goto(page, 'c0idleb')
    await shows(page, 'B 的答案')
    const toggle = page.getByRole('button', { name: /执行过程/ }).first()
    check('默认只显示问题和答案', (await toggle.innerText()).includes('看执行过程'))
    await toggle.click()
    check('点开后能收起', await page.getByRole('button', { name: '收起执行过程' })
      .waitFor({ timeout: 4000 }).then(() => true, () => false))
    await page.getByRole('button', { name: '收起执行过程' }).click()
    check('收起之后按钮回到「看执行过程」', await page.getByRole('button', { name: '看执行过程' }).count() === 1)
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  }

  if (section('writing', '模型出字：只给计数和思考的最后一句，不进 events')) {
    for (const reducedMotion of ['no-preference', 'reduce']) {
      const { page, ctx, errors } = await open(theme, { reducedMotion })
      ctl.thinkGo = ctl.writingGo = false
      ctl.writingAt = 0
      await goto(page, 'c0writel')
      await send(page, '一个正在写的问题')
      const head = page.locator('[data-turn]').last()
      if (reducedMotion === 'no-preference') {
        const said = (text) => head.getByText(text, { exact: true }).first()
          .waitFor({ timeout: 8000 }).then(() => true, () => false)
        check('思考时头部写出最后一句完整的话', await said('正在思考：先看看 bi 里有哪些表'))
        await page.waitForTimeout(400)
        check('还在写的半句不上头部', !(await head.getByText('然后按月汇总出勤').count()))
        await page.screenshot({ path: `${SHOTS}/thinking-${theme}.png` })
        ctl.thinkGo = true
        check('半句写完了才换上它', await said('正在思考：然后按月汇总出勤率，再和上个月比'))
      }
      ctl.thinkGo = true
      ctl.writingGo = true
      await until(() => ctl.writingAt > 0, 8000)
      if (reducedMotion === 'reduce') {
        // 关掉动效时一秒写回一次：半秒时还没出现，一秒多一点就有了
        await page.waitForTimeout(Math.max(0, ctl.writingAt + 500 - Date.now()))
        check('关掉动效时计数不逐帧跳（半秒时还没写回）', !(await head.getByText('已生成').count()))
      }
      const shown = await head.getByText('正在撰写 · 已生成 1,234 字').first()
        .waitFor({ timeout: 3000 }).then(() => Date.now() - ctl.writingAt, () => -1)
      check(reducedMotion === 'reduce' ? '关掉动效时一秒左右写回计数' : '出字时头部写「正在撰写 · 已生成 1,234 字」',
        shown >= 0 && (reducedMotion === 'reduce' ? shown >= 800 : shown < 900), `${shown} ms`)
      const t = (await chatState(page)).byConversation.c0writel.at(-1)
      check('token 和思考增量都没进 events', !t.events.some((e) => e.type.startsWith('llm.token') || e.type === 'llm.thinking.delta'),
        t.events.map((e) => e.type).join(','))
      check('答案原文没有提前露出来', !(await head.innerText()).includes('字字字'))
      await page.screenshot({ path: `${SHOTS}/writing-${reducedMotion === 'reduce' ? 'reduced-' : ''}${theme}.png` })
      await page.getByRole('button', { name: '停止这一轮' }).click()
      await page.waitForTimeout(200)
      check('停下之后不再写「正在撰写」', !(await head.getByText('正在撰写').count()))
      check('没有运行时报错', errors.length === 0, errors[0] ?? '')
      await ctx.close()
    }
  }

  if (section('meta', '可信度信息刷新之后还在')) {
    const { page, ctx, errors } = await open(theme)
    await goto(page, 'c0metam')
    await shows(page, '一线 96.2%')
    await page.waitForTimeout(600)
    const card = page.locator('[data-turn="tm1"]')
    check('出具档位还在', await card.getByText('降档出具').count() > 0)
    check('探索运行的标注还在', await card.getByText('探索运行 · 不进正式归档').count() > 0)
    check('降档的原因还在', await card.getByText('叙述模板渲染为空').count() > 0)
    check('耗时还在', (await card.innerText()).includes('12.3 s'))
    check('这些都是从落库的 meta 读回来的，没有再去问运行', !log.runGets.includes('run-meta'), log.runGets.join(','))
    await page.screenshot({ path: `${SHOTS}/meta-${theme}.png` })
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  }

  if (section('list', '会话列表：分组、相对时间、完整标题、筛选')) {
    const { page, ctx, errors } = await open(theme)
    await goto(page, 'c0idleb')
    const aside = page.locator('aside[aria-label="对话列表"]')
    await aside.getByText('会话 B').first().waitFor({ timeout: 6000 })
    const heads = await aside.locator('section').evaluateAll((els) => els.map((e) => e.getAttribute('aria-label')))
    check('按今天 / 昨天 / 近 7 天 / 更早分组', heads.join(',') === '今天,昨天,近 7 天,更早', heads.join(','))
    const rowA = aside.locator('div.group', { hasText: '会话 A' })
    check('写相对时间', /分钟前|刚刚/.test(await rowA.innerText()), (await rowA.innerText()).replace(/\n/g, ' / '))
    const tip = await aside.locator('div.group', { hasText: '会话 F' }).locator('button').first().getAttribute('title')
    check('悬停能看到完整标题', !!tip && tip.startsWith('会话 F：取消过、失败过、不可用的历史'), tip ?? '')
    const box = aside.getByRole('searchbox', { name: '按标题或问过的话找对话' })
    await box.fill('长会话')
    const hits = await aside.locator('div.group').allInnerTexts()
    check('筛选只留下匹配的对话', hits.length === 1 && hits[0].includes('会话 C'), hits.map((h) => h.split('\n')[0]).join(' | '))
    await page.screenshot({ path: `${SHOTS}/list-filter-${theme}.png` })
    await box.press('Escape')
    check('Esc 清掉筛选', (await aside.locator('div.group').count()) === Object.keys(CONVS).length)
    await box.fill('没有这样的对话')
    check('没有匹配时说一声', await aside.getByText('没有标题或问题里带「没有这样的对话」的对话').count() === 1)
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  }

  if (section('delete', '删除：确认，且可以撤销')) {
    const { page, ctx, errors } = await open(theme)
    await goto(page, 'c0idleb')
    await shows(page, 'B 的答案')
    const row = page.locator('aside[aria-label="对话列表"] div.group', { hasText: '会话 B' })
    await row.hover()
    await row.getByRole('button', { name: /删除/ }).click()
    check('删除前先确认', await page.getByRole('dialog').waitFor({ timeout: 3000 }).then(() => true, () => false))
    await page.getByRole('dialog').getByRole('button', { name: '删除' }).click()
    await until(() => log.archived.some(([id, b]) => id === 'c0idleb' && b.archived === true))
    check('删除是归档，不是真删', log.archived.some(([id, b]) => id === 'c0idleb' && b.archived === true))
    check('给了撤销', await page.getByRole('button', { name: '撤销' }).first()
      .waitFor({ timeout: 4000 }).then(() => true, () => false))
    await page.getByRole('button', { name: '撤销' }).first().click()
    await until(() => log.archived.some(([id, b]) => id === 'c0idleb' && b.archived === false))
    await page.waitForTimeout(200)
    check('撤销放回去了', log.archived.some(([id, b]) => id === 'c0idleb' && b.archived === false)
      && await page.locator('aside[aria-label="对话列表"]').getByText('会话 B').count() > 0)
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  }
}

await browser.close()
console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 问数据全部通过')
process.exit(failed ? 1 : 0)
