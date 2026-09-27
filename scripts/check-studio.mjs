// 编排页编辑体验的检查：撤销重做、问题面板定位、快捷键、三栏收起、调色板落点、
// 分支 default 拦截、?run=&focus= 参数、助手改图（应用 / 只回话 / 失败 / 停止）。
//
// 不写库：要编辑的工作流、它的版本、画布会话、助手的流式输出都用 page.route 在
// 浏览器里伪造；后端只被读（目录、校验、变量分析这些纯计算）。保存走伪造的 PATCH，
// 其余写请求一律拦成 409 并记下来——检查结束时这张单子必须是空的。
//
// 跑法（只对沙箱）：
//   AGENTLAB_WEB=http://localhost:5373 AGENTLAB_API=http://localhost:8100/api node scripts/check-studio.mjs
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const API = process.env.AGENTLAB_API ?? 'http://localhost:8000/api'
const CHROME = process.env.CHROME_PATH
  ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const MOD = process.platform === 'darwin' ? 'Meta' : 'Control'

let failed = 0
const check = (name, cond, detail = '') => {
  const d = String(detail ?? '').replace(/\s+/g, ' ').slice(0, 120)
  console.log(`  ${cond ? '✓' : '✗'} ${name}${d ? ` — ${d}` : ''}`)
  if (!cond) failed++
}

// ---------------------------------------------------------------- 夹具

const node = (id, type, x, y, label, config = {}) => ({ id, type, position: { x, y }, data: { label, config } })
const edge = (source, target, sourceHandle) => ({
  id: `e_${source}_${sourceHandle ?? ''}_${target}`, source, target, sourceHandle: sourceHandle ?? null,
})

const GRAPH = {
  nodes: [
    node('start', 'input', 0, 200, '目标', { fields: [{ name: 'goal', required: true }] }),
    node('lookup', 'retrieve', 300, 200, '背景检索', { query: '{{ input.goal }}', assign_to: 'knowledge', limit: 3 }),
    node('gate', 'branch', 600, 200, '模式分支', {
      mode: 'expression',
      cases: [{ key: 'fast', condition: "input.goal == 'x'", label: '快速' },
              { key: 'default', condition: 'true', label: '协作' }],
    }),
    // vars.summary 由下游的「汇总」产出：这里取到的是空值（warning），补全里也该置灰
    node('answer', 'llm', 900, 80, '快速回答', { prompt: '{{ input.goal }} {{ vars.knowledge }} {{ vars.summary }}', assign_to: 'result' }),
    node('call', 'tool', 900, 340, '查询销量', { args: {} }),
    node('sum', 'transform', 1200, 200, '汇总', { mode: 'template', template: '{{ vars.result }}', assign_to: 'summary' }),
    node('done', 'output', 1500, 200, '成果', { fields: [{ name: '结果', value: '{{ vars.summary }}' }] }),
  ],
  edges: [
    edge('start', 'lookup'), edge('lookup', 'gate'), edge('gate', 'answer', 'fast'),
    edge('gate', 'call', 'default'), edge('answer', 'sum'), edge('call', 'sum'), edge('sum', 'done'),
  ],
}
const V1 = { nodes: GRAPH.nodes.slice(0, 3).concat([GRAPH.nodes[6]]), edges: [edge('start', 'lookup'), edge('lookup', 'gate'), edge('gate', 'done', 'fast')] }

// 查库的 agent 和带成员的协作团队：助手改节点时工具不能被悄悄改没（NI-1），提示词点名的
// 工具没绑定要能定位到工具那一栏（NI-2）。全是通用示例名
const QUERY_TOOLS = ['db_query__shop', 'db_schema__shop']
const TEAM_GRAPH = {
  nodes: [
    node('start', 'input', 0, 200, '输入', { fields: [{ name: 'question', required: true }] }),
    node('fetch', 'agent', 300, 200, '数据查询', {
      system: '你是取数助手。', prompt: '先用 db_schema__shop 看表结构，再用 db_query__shop 查 orders 表里每个门店的订单数',
      tools: ['db_query__shop'], assign_to: 'rows', max_steps: 8,
    }),
    node('team', 'supervisor', 600, 200, '复核团队', {
      goal: '核对订单数', max_rounds: 4,
      agents: [
        { name: 'fetcher', description: '负责查库', system: '只用 SQL 取数', tools: ['db_query__shop'] },
        { name: 'writer', description: '写结论', system: '用 python_exec 算出占比再写结论', tools: [] },
      ],
    }),
    node('done', 'output', 900, 200, '成果', { fields: [{ name: 'answer', value: '{{ vars.rows }}' }] }),
  ],
  edges: [edge('start', 'fetch'), edge('fetch', 'team'), edge('team', 'done')],
}
/** 工具都绑好了的那一版：合并语义要守住的就是它 */
const TEAM_BOUND = {
  ...TEAM_GRAPH,
  nodes: TEAM_GRAPH.nodes.map((n) => (n.id === 'fetch'
    ? { ...n, data: { ...n.data, config: { ...n.data.config, tools: [...QUERY_TOOLS] } } } : n)),
}

const wf = (id, extra) => ({
  id, name: extra.name, description: extra.description ?? '检查脚本伪造的工作流', graph: extra.graph ?? GRAPH,
  tags: extra.tags ?? [], version: extra.version ?? 3, is_template: !!extra.is_template,
  status: extra.status ?? 'draft', published_version: extra.published_version ?? null,
  published_by: extra.published_by ?? null, run_count: 0,
  created_at: '2026-09-20T02:00:00Z', updated_at: '2026-09-26T02:00:00Z',
})
const FAKES = {
  'st-main': wf('st-main', { name: '__studio_check__', version: 3, published_version: 2 }),
  'st-gov': wf('st-gov', { name: '__studio_check_gov__', status: 'governed', version: 3, published_version: 3, published_by: '张工' }),
  'st-tpl': wf('st-tpl', { name: '__studio_check_tpl__', is_template: true, tags: ['extracted'] }),
  'st-team': wf('st-team', { name: '__studio_check_team__', graph: TEAM_BOUND, version: 2, published_version: 2, status: 'published' }),
  'st-lint': wf('st-lint', { name: '__studio_check_lint__', graph: TEAM_GRAPH, version: 1 }),
}
const VERSIONS = [
  { id: 'v3', version: 3, note: '', created_at: '2026-09-26T02:00:00Z' },
  { id: 'v2', version: 2, note: '收紧检索条数', created_at: '2026-09-25T02:00:00Z' },
  { id: 'v1', version: 1, note: '起步', created_at: '2026-09-24T02:00:00Z' },
]
const FAKE_RUN = 'st-run-0001'

const sse = (ops) => ops.map((o) => `data: ${JSON.stringify(o)}\n\n`).join('')

// ---------------------------------------------------------------- 浏览器

const browser = await chromium.launch({ executablePath: CHROME })
const writes = []
let dialogs = 0

/**
 * only：目录里只放这几张伪造的（不混真实的），用来造「删完一张不剩」
 * platform：伪装成别的平台（'Windows'），看快捷键提示是不是跟着平台走
 */
async function open({ width = 1440, height = 900, path = '/studio/st-main', prefs = {}, stream, only, platform } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height } })
  // 每个上下文只在第一次加载时清偏好：刷新之后要看得到上一次存下的收起状态
  await ctx.addInitScript((p) => {
    try {
      if (sessionStorage.getItem('st-init')) return
      sessionStorage.setItem('st-init', '1')
      localStorage.removeItem('agentlab.studio.palette')
      localStorage.removeItem('agentlab.studio.assistant')
      localStorage.setItem('agentlab_actor', '检查脚本')
      for (const [k, v] of Object.entries(p)) localStorage.setItem(k, v)
    } catch { /* noop */ }
  }, prefs)
  if (platform) {
    await ctx.addInitScript((p) => {
      Object.defineProperty(navigator, 'platform', { get: () => (p === 'Windows' ? 'Win32' : p) })
      Object.defineProperty(navigator, 'userAgentData', { get: () => ({ platform: p }) })
    }, platform)
  }
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('dialog', (d) => { dialogs++; void d.dismiss() })
  const json = (route, body, status = 200) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
  const state = { stream: stream ?? null, bodies: [], patches: [], deleted: new Set() }

  // 兜底：别的写请求一律拦下记账（先注册的最后匹配）
  await page.route(/\/api\//, (route) => {
    const req = route.request()
    if (req.method() === 'GET') return route.continue()
    writes.push(`${req.method()} ${new URL(req.url()).pathname}`)
    return json(route, { detail: '检查脚本不写库' }, 409)
  })
  await page.route(/\/api\/workflows\/(validate|variables)$/, (route) => route.continue())
  await page.route(/\/api\/copilot\/layout$/, (route) => route.continue())
  await page.route(/\/api\/workflows(\?.*)?$/, async (route) => {
    if (route.request().method() !== 'GET') return route.fallback()
    const res = await route.fetch()
    const real = only ? [] : await res.json().catch(() => [])
    const fakes = Object.values(FAKES).filter((w) => (!only || only.includes(w.id)) && !state.deleted.has(w.id))
    return json(route, [...fakes, ...(Array.isArray(real) ? real : [])])
  })
  await page.route(/\/api\/workflows\/st-[a-z]+(\/.*)?(\?.*)?$/, (route) => {
    const url = new URL(route.request().url())
    const [, , , id, sub, v] = url.pathname.split('/')
    const method = route.request().method()
    if (sub === 'versions' && !v) return json(route, VERSIONS)
    if (sub === 'versions' && v) {
      const n = Number(v)
      return json(route, { ...VERSIONS.find((x) => x.version === n), workflow_id: id,
        graph: n === 1 ? V1 : GRAPH, graph_hash: 'x', input_fields: [{ name: 'goal' }], published: n === 2 })
    }
    if (method === 'GET' && !sub) return state.deleted.has(id) ? json(route, { detail: '不在了' }, 404) : json(route, FAKES[id])
    if (method === 'DELETE' && !sub) {
      state.deleted.add(id)
      return route.fulfill({ status: 204, body: '' })
    }
    if (method === 'PATCH' && !sub) {
      const body = route.request().postDataJSON()
      state.patches.push(body)
      return json(route, { ...FAKES[id], ...body, version: FAKES[id].version + 1, status: 'draft' })
    }
    return route.fallback()
  })
  await page.route(/\/api\/conversations(\/.*)?(\?.*)?$/, (route) => {
    const req = route.request()
    const url = new URL(req.url())
    if (req.method() === 'GET' && url.pathname.endsWith('/conversations')) {
      return json(route, [{ id: 'conv-old', title: '', kind: 'canvas', workflow_id: 'st-main', archived: false,
        turn_count: 2, last_question: '改造一下' }])
    }
    if (req.method() === 'GET') {
      return json(route, { id: 'conv-old', title: '', kind: 'canvas', archived: false, turn_count: 2, last_question: '',
        turns: [1, 2].map((i) => ({ id: `t${i}`, seq: i, question: `之前的第 ${i} 轮`, answer: '', explanation: '好',
          status: 'done', error: '' })) })
    }
    if (req.method() === 'POST' && url.pathname.endsWith('/conversations')) {
      return json(route, { id: 'conv-new', title: '', kind: 'canvas', archived: false, turn_count: 0, last_question: '', turns: [] })
    }
    // 记一轮：startTurn / patchTurn
    return json(route, { id: 't-new', seq: 3, question: 'x', answer: '', explanation: '', status: 'done', error: '' })
  })
  await page.route(/\/api\/copilot\/generate-stream$/, async (route) => {
    state.bodies.push(route.request().postDataJSON())
    const s = state.stream
    if (s === 'hang') return new Promise(() => {})
    return route.fulfill({ status: 200, contentType: 'text/event-stream', body: sse(s ?? []) })
  })
  await page.route(new RegExp(`/api/runs/${FAKE_RUN}(\\?.*)?$`), (route) => json(route, {
    id: FAKE_RUN, workflow_id: 'st-main', status: 'succeeded', run_class: 'exploratory', version: 3,
    input: {}, output: {}, error: '', usage: {}, created_at: '2026-09-26T02:00:00Z',
  }))

  await page.goto(`${WEB}${path}`, { waitUntil: 'networkidle' })
  await page.waitForFunction(() => window.__studio?.getState().workflow != null, null, { timeout: 8000 })
  await page.waitForTimeout(500)
  return { ctx, page, errors, state }
}

const S = (page, fn, arg) => page.evaluate(fn, arg)
const st = (page) => page.evaluate(() => {
  const s = window.__studio.getState()
  return { nodes: s.nodes.map((n) => n.id), edges: s.edges.length, selectedId: s.selectedId,
    past: s.past.length, future: s.future.length, dirty: s.dirty, analysis: s.analysis }
})
const waitAnalysis = (page) => page.waitForFunction(() => {
  const s = window.__studio.getState()
  return s.analysis === 'ok' && s.issues.length > 0
}, null, { timeout: 8000 })
/** 点画布空白处：让焦点离开输入框（快捷键不抢输入框里的键） */
const blur = (page) => page.locator('.react-flow__pane').click({ position: { x: 30, y: 30 } })
const count = (page, sel) => page.locator(sel).count()

// ================================================================ 1
console.log('=== 工具栏：版本标签、校验结论、问题面板定位 ===')
{
  const { ctx, page, errors } = await open()
  await waitAnalysis(page)
  const label = await page.locator('span[title*="画布是草稿"]').innerText().catch(() => '')
  check('状态标签写出草稿和已发布两版', /草稿 v3/.test(label) && /已发布 v2/.test(label) && /领先 1 版/.test(label), label.replace(/\s+/g, ' '))

  const chip = page.locator('button[aria-expanded]').filter({ hasText: /错|提示|可运行/ }).first()
  const chipText = (await chip.innerText()).replace(/\s+/g, ' ')
  check('校验 chip 同时写出错误和提示', /1 错/.test(chipText) && /提示/.test(chipText), chipText)

  await chip.click()
  await page.waitForTimeout(250)
  check('点 chip 打开问题面板', await count(page, '[role="tablist"][aria-label="画布停靠栏"]') === 1)
  const rows = await count(page, '[data-problem]')
  const issues = await S(page, () => window.__studio.getState().issues.length)
  check('每条问题一行', rows === issues, `${rows} 行 / ${issues} 条`)

  await page.locator('[data-problem]').filter({ hasText: '还没选工具' }).locator('button').first().click()
  await page.waitForTimeout(400)
  const s = await S(page, () => ({ sel: window.__studio.getState().selectedId, focus: window.__studio.getState().focusRequest?.id }))
  check('点一条问题：选中那个节点', s.sel === 'call', s.sel)
  check('点一条问题：请画布取景到它', s.focus === 'call', s.focus)
  const fieldMsg = await page.locator('[data-field="tool"]').innerText().catch(() => '')
  check('字段级错误落在「工具」输入框下面', fieldMsg.includes('还没选工具'), fieldMsg.slice(0, 40))
  const ring = await S(page, () => window.__studio.getState().nodes.filter((n) => n.selected).map((n) => n.id).join(','))
  check('React Flow 的选中和检查器一致', ring === 'call', ring)

  // 图级和悬空边：注入和后端同形的 issues
  await S(page, () => window.__studio.setState((s) => ({ issues: [...s.issues,
    { level: 'error', node_id: null, edge_id: null, message: '找不到入口：每个节点都有入边，图里存在环且没有起点' },
    { level: 'error', node_id: null, edge_id: 'ghost', message: "边指向了不存在的目标节点 'nope'" }] })))
  await page.waitForTimeout(200)
  check('图级问题有自己的分组', (await page.locator('[role="list"][aria-label="问题"]').innerText()).includes('整张工作流'))
  check('悬空边给出「删除这条悬空边」', await count(page, 'button:has-text("删除这条悬空边")') === 1)

  // F8 在节点上的问题之间跳（图级、边上的没有节点可定位，列在面板顶上）
  await blur(page)
  await page.keyboard.press('F8')
  await page.waitForTimeout(200)
  const first = await S(page, () => window.__studio.getState().selectedId)
  await page.keyboard.press('F8')
  await page.waitForTimeout(200)
  const second = await S(page, () => window.__studio.getState().selectedId)
  await page.keyboard.press('Shift+F8')
  await page.waitForTimeout(200)
  const back = await S(page, () => window.__studio.getState().selectedId)
  check('F8 逐个跳到有问题的节点，⇧F8 往回', !!first && !!second && back === first, `${first} → ${second} → ${back}`)

  // 字段级 warning + 模板高亮
  await S(page, () => window.__studio.getState().analyzeNow())
  await S(page, () => window.__studio.getState().select('answer'))
  await page.waitForTimeout(400)
  const promptMsg = await page.locator('[data-field="prompt"]').innerText().catch(() => '')
  check('下游变量的 warning 落在「用户提示」下面', /之后才跑|取到的会是空值/.test(promptMsg), promptMsg.slice(0, 50))
  check('高亮层把下游变量标成琥珀', await count(page, '[data-field="prompt"] [data-path="vars.summary"][data-tone="late"]') === 1)
  check('能取到的变量是普通胶囊', await count(page, '[data-field="prompt"] [data-path="vars.knowledge"][data-tone="ok"]') === 1)
  check('字段旁标着「模板」', (await page.locator('[data-field="prompt"]').innerText()).includes('模板'))

  // 补全只给上游
  const ta = page.locator('[data-field="prompt"] textarea')
  await ta.click()
  await page.keyboard.press('End')
  await page.keyboard.type(' {{ va')
  await page.waitForTimeout(200)
  const list = await page.locator('[data-field="prompt"] [role="listbox"]').innerText().catch(() => '')
  check('补全给出上游的 vars.knowledge', list.includes('vars.knowledge'), list.replace(/\s+/g, ' ').slice(0, 80))
  check('下游的 vars.summary 置灰、写明取不到', /vars\.summary[\s\S]*这里还取不到/.test(list))
  await page.keyboard.press('Escape')

  // 表达式字段敲 {{ 就地提示
  await S(page, () => window.__studio.getState().select('gate'))
  await page.waitForTimeout(300)
  const cond = page.locator('[data-field="cases"] input').nth(2)
  await cond.click()
  await page.keyboard.press('End')
  await page.keyboard.type(' {{')
  await page.waitForTimeout(150)
  check('表达式里敲 {{ 就地提示不需要', (await page.locator('[data-field="cases"]').innerText()).includes('这里是表达式'))
  check('条件字段标着「表达式」', (await page.locator('[data-field="cases"]').innerText()).includes('表达式'))
  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))
  await ctx.close()
}

// ================================================================ 2
console.log('=== 分支 case：default 拦截、重复标识、出口去重 ===')
{
  const { ctx, page } = await open()
  await S(page, () => window.__studio.getState().select('gate'))
  await page.waitForTimeout(400)
  const handles = () => count(page, '.react-flow__node[data-id="gate"] .react-flow__handle.source')
  check('key=default 和兜底出口合并成一个（2 个出口）', await handles() === 2, `${await handles()} 个`)
  const text = await page.locator('[data-field="cases"]').innerText()
  check('行内说破 default 是保留名', text.includes('兜底出口的保留名'))
  check('case 表单每个框都有标签', /标识（连线出口）/.test(text) && /说明/.test(text) && /条件/.test(text))
  await page.locator('[data-field="cases"] button:has-text("改成 team")').click()
  await page.waitForTimeout(250)
  const key = await S(page, () => window.__studio.getState().nodes.find((n) => n.id === 'gate').data.config.cases[1].key)
  check('一键改名为 team', key === 'team', key)
  check('改名后出口变成 3 个（fast / team / 其他）', await handles() === 3, `${await handles()} 个`)
  const keyInput = page.locator('[data-field="cases"] input').nth(3)
  const caseKey = () => S(page, () => window.__studio.getState().nodes.find((n) => n.id === 'gate').data.config.cases[1].key)
  const gateEdge = () => S(page, () => window.__studio.getState().edges.find((e) => e.source === 'gate' && e.target === 'call')?.sourceHandle ?? null)

  // 改名：先清空再重打，中间态不落地，连在这个出口上的线跟过去
  await keyInput.fill('')
  await keyInput.type('crew')
  check('写到一半（空标识）不落进画布，线不断', await caseKey() === 'team' && await gateEdge() === 'team', `${await caseKey()} / ${await gateEdge()}`)
  await keyInput.press('Enter')
  await page.waitForTimeout(200)
  check('回车后改名生效，线跟到新出口', await caseKey() === 'crew' && await gateEdge() === 'crew', `${await caseKey()} / ${await gateEdge()}`)

  // 禁用重复
  await keyInput.fill('fast')
  await page.waitForTimeout(200)
  check('重复标识当场报错', (await page.locator('[data-field="cases"]').innerText()).includes('重复'))
  await keyInput.press('Enter')
  await page.waitForTimeout(200)
  check('重复标识落不进画布，退回原来的', await caseKey() === 'crew' && await gateEdge() === 'crew', `${await caseKey()}`)
  check('说明为什么没改', (await page.locator('[data-field="cases"]').innerText()).includes('没有改成「fast」'))
  check('重复标识不产生同 id 的两个出口', await count(page, '.react-flow__node[data-id="gate"] .react-flow__handle[data-handleid="fast"]') === 1)

  // 禁用 default
  await keyInput.fill('default')
  await page.waitForTimeout(150)
  check('新写 default 当场拦下', (await page.locator('[data-field="cases"]').innerText()).includes('保留名'))
  // 焦点挪到别的输入框上（点画布空白会连检查器一起收起）
  await page.locator('input[id^="node-gate-label"]').click()
  await page.waitForTimeout(200)
  check('default 落不进画布（离开输入框也不行）', await caseKey() === 'crew', await caseKey())

  // 新加的分支自带不重名的标识，不会一出来就是红的
  await page.locator('[data-field="cases"] button:has-text("添加分支")').click()
  await page.waitForTimeout(150)
  const keys = await S(page, () => window.__studio.getState().nodes.find((n) => n.id === 'gate').data.config.cases.map((c) => c.key))
  check('添加分支自带不重名的标识', keys.length === 3 && !!keys[2] && new Set(keys).size === 3, keys.join(','))
  await ctx.close()
}

// ================================================================ 3
console.log('=== 撤销 / 重做、删除回执、复制粘贴、调色板落点 ===')
{
  const { ctx, page, errors } = await open()
  const s0 = await st(page)

  // 连续打字只算一步
  await S(page, () => window.__studio.getState().select('answer'))
  await page.waitForTimeout(250)
  const name = page.locator('input[id^="node-answer-label"]')
  await name.click()
  await page.keyboard.press('End')
  await page.keyboard.type('（改）')
  await page.waitForTimeout(100)
  const s1 = await st(page)
  check('连续打字合成一步撤销', s1.past === s0.past + 1, `${s0.past} → ${s1.past}`)
  await blur(page)
  await page.keyboard.press(`${MOD}+z`)
  await page.waitForTimeout(150)
  const label = await S(page, () => window.__studio.getState().nodes.find((n) => n.id === 'answer').data.label)
  check('⌘Z 撤回改名', label === '快速回答', label)
  check('撤回到保存时的样子，「未保存」消失', !(await st(page)).dirty)
  await page.keyboard.press(`${MOD}+Shift+z`)
  await page.waitForTimeout(150)
  check('⇧⌘Z 重做', (await S(page, () => window.__studio.getState().nodes.find((n) => n.id === 'answer').data.label)) === '快速回答（改）')
  await page.keyboard.press(`${MOD}+z`)

  // 删除：Backspace → 检查器不留空白层，toast 能撤销
  await page.locator('.react-flow__node[data-id="sum"]').click()
  await page.waitForTimeout(200)
  await page.keyboard.press('Backspace')
  await page.waitForTimeout(300)
  const s2 = await st(page)
  check('Backspace 删掉选中的节点', !s2.nodes.includes('sum'))
  check('删掉之后检查器收起', s2.selectedId === null && await count(page, 'button[title="返回助手（Esc）"]') === 0)
  const toastUndo = page.locator('button:has-text("撤销")').last()
  check('删除回执带「撤销」', await page.getByText('已删除「汇总」').count() === 1)
  await toastUndo.click()
  await page.waitForTimeout(250)
  const s3 = await st(page)
  check('回执里撤销：节点和它的边都回来了', s3.nodes.includes('sum') && s3.edges === s0.edges, `${s3.edges}/${s0.edges} 条边`)
  check('一次删除（边 + 节点）只占一步', s3.future === 1, `future=${s3.future}`)

  // 什么都没变的编辑：JSON 字段里删个空格会把同一个对象原样再发一遍。不记步、不亮「未保存」，
  // 也不该顺手清掉重做栈
  await S(page, () => {
    const s = window.__studio.getState()
    const n = s.nodes.find((x) => x.id === 'call')
    s.updateNode('call', { label: n.data.label, config: structuredClone(n.data.config) })
  })
  const same = await st(page)
  check('原样再发一遍的编辑不占撤销、不标未保存', same.past === s3.past && same.future === s3.future && same.dirty === s3.dirty,
    `past ${s3.past}→${same.past} future ${s3.future}→${same.future} dirty ${same.dirty}`)

  // 复制粘贴、⌘D
  await page.locator('.react-flow__node[data-id="answer"]').click()
  await page.waitForTimeout(150)
  await blur(page)
  await S(page, () => window.__studio.getState().select('answer'))
  await page.locator('.react-flow__pane').focus().catch(() => {})
  await page.evaluate(() => document.activeElement instanceof HTMLElement && document.activeElement.blur())
  await page.keyboard.press(`${MOD}+c`)
  await page.keyboard.press(`${MOD}+v`)
  await page.waitForTimeout(200)
  const s4 = await st(page)
  const pasted = s4.nodes.filter((id) => !s3.nodes.includes(id))
  check('⌘C ⌘V 粘出一个新节点（新 id）', pasted.length === 1 && pasted[0].startsWith('llm_'), pasted.join(','))
  await page.keyboard.press(`${MOD}+d`)
  await page.waitForTimeout(200)
  check('⌘D 原地复制一份', (await st(page)).nodes.length === s4.nodes.length + 1)

  // 调色板点击添加：视口中心附近、不压住别人、自动选中
  const before = await st(page)
  await page.locator('button[aria-label="添加模型调用"], button:has-text("模型调用")').first().click()
  await page.waitForTimeout(300)
  const added = await S(page, () => {
    const s = window.__studio.getState()
    const n = s.nodes[s.nodes.length - 1]
    const c = s.getViewportCenter?.()
    const box = (x) => ({ x: x.position.x, y: x.position.y, w: x.measured?.width ?? 238, h: x.measured?.height ?? 96 })
    const me = box(n)
    const hit = s.nodes.filter((o) => o.id !== n.id).some((o) => {
      const b = box(o)
      return me.x < b.x + b.w && b.x < me.x + me.w && me.y < b.y + b.h && b.y < me.y + me.h
    })
    return { id: n.id, sel: s.selectedId, ring: !!n.selected, dx: c ? Math.abs(n.position.x + 119 - c.x) : -1,
      dy: c ? Math.abs(n.position.y + 48 - c.y) : -1, hit, hasCenter: !!c,
      prompt: n.data.config.prompt }
  })
  check('调色板点击：多了一个节点', (await st(page)).nodes.length === before.nodes.length + 1)
  check('新节点自动选中（检查器 + 选中环）', added.sel === added.id && added.ring, JSON.stringify(added).slice(0, 80))
  check('落点在视口中心附近', added.hasCenter && added.dx < 700 && added.dy < 500, `dx=${Math.round(added.dx)} dy=${Math.round(added.dy)}`)
  check('不压住已有节点', !added.hit)
  check('默认提示词用上了这张图真实的入口字段', added.prompt === '{{ input.goal }}', added.prompt)
  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))
  await ctx.close()
}

// ================================================================ 4
console.log('=== 快捷键与三栏 ===')
{
  const { ctx, page, state } = await open()
  const width = (sel) => page.locator(sel).evaluate((el) => Math.round(el.getBoundingClientRect().width))
  await blur(page)
  await page.keyboard.press('Alt+KeyV')
  await page.waitForTimeout(250)
  check('⌥V 打开变量抽屉（Mac 上 e.key 是 √ 也认）', await count(page, '#dock-variables') === 1)
  await page.hover('tr[data-var="vars.knowledge"]')
  await page.waitForTimeout(150)
  const lineage = await S(page, () => window.__studio.getState().lineage)
  check('悬停变量：画出血缘（谁产出、谁引用）', lineage?.var === 'vars.knowledge' && lineage.producers[0] === 'lookup'
    && lineage.consumers.includes('answer'), JSON.stringify(lineage))
  const drawn = await page.evaluate(() => {
    const nc = (id) => document.querySelector(`.react-flow__node[data-id="${id}"] > .nc`)
    const tag = (id) => getComputedStyle(nc(id), '::after').content
    const dim = (id) => getComputedStyle(nc(id)).getPropertyValue('--nc-dim').trim()
    const edgeOp = (s, t) => {
      const e = window.__studio.getState().edges.find((x) => x.source === s && x.target === t)
      return getComputedStyle(document.querySelector(`.react-flow__edge[data-id="${CSS.escape(e.id)}"]`)).opacity
    }
    return { producer: tag('lookup'), consumer: tag('answer'), other: dim('call'), mid: dim('gate'),
      hop: edgeOp('gate', 'answer'), off: edgeOp('gate', 'call') }
  })
  check('画布上标出产出和引用的节点', drawn.producer.includes('产出 vars.knowledge') && drawn.consumer.includes('引用'),
    `${drawn.producer} / ${drawn.consumer}`)
  check('中间隔着的节点和走线一起亮，其余退下去', drawn.mid === '1' && drawn.other === '0.45'
    && drawn.hop === '1' && Number(drawn.off) < 1, JSON.stringify(drawn))
  await blur(page)
  await page.keyboard.press('Alt+KeyV')
  await page.waitForTimeout(200)
  check('再按 ⌥V 收起', await count(page, '#dock-variables') === 0)
  check('收起后血缘高亮一起收掉', (await S(page, () => window.__studio.getState().lineage)) === null)

  await page.keyboard.press('Alt+KeyP')
  await page.waitForTimeout(200)
  check('⌥P 打开问题面板', await count(page, '#dock-problems') === 1)

  await page.keyboard.press('Alt+KeyN')
  await page.waitForTimeout(200)
  check('⌥N 把节点库收成 48px 图标轨', await width('aside:has([aria-label^="节点库"])') === 48)
  check('收起状态存进 localStorage', await S(page, () => localStorage.getItem('agentlab.studio.palette')) === 'closed')
  // 图标轨的悬停说明：入场只有 fade-up 那 4px，不能先落低半个身位、动画一完再跳上去
  const railBtn = page.locator('button[aria-label="添加模型调用"]')
  await railBtn.hover()
  await page.waitForSelector('[role="tooltip"]')
  const tops = []
  for (let i = 0; i < 10; i++) {
    tops.push(await page.evaluate(() => {
      const t = document.querySelector('[role="tooltip"]')
      return { outer: t.getBoundingClientRect().top, inner: t.firstElementChild.getBoundingClientRect().top }
    }))
    await page.waitForTimeout(20)
  }
  const iconMid = await railBtn.evaluate((el) => { const r = el.getBoundingClientRect(); return r.top + r.height / 2 })
  const tipMid = await page.evaluate(() => { const r = document.querySelector('[role="tooltip"]').firstElementChild.getBoundingClientRect(); return r.top + r.height / 2 })
  const inner = tops.map((t) => t.inner)
  const spread = Math.max(...inner) - Math.min(...inner)
  check('图标轨说明浮层不跳：入场只挪 4px，落定时和图标对齐',
    new Set(tops.map((t) => Math.round(t.outer))).size === 1 && spread <= 4.5 && Math.abs(tipMid - iconMid) <= 1,
    `spread=${spread.toFixed(1)} 偏差=${(tipMid - iconMid).toFixed(1)}`)
  await page.mouse.move(700, 450)
  await page.keyboard.press('Alt+KeyA')
  await page.waitForTimeout(200)
  const asideW = await page.locator('aside[aria-hidden="true"]').count()
  check('⌥A 收起助手栏（不卸载，输入草稿保住）', asideW === 1 && await count(page, 'textarea') > 0)
  const overflow = () => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
  check('助手栏收起时页面不横向溢出', await overflow() <= 0, `${await overflow()}px`)
  await page.keyboard.press('Alt+KeyA')
  await page.waitForTimeout(150)
  check('助手栏展开时页面不横向溢出', await overflow() <= 0, `${await overflow()}px`)

  // 选着节点也要收得起来：以前「选中就展开」的 effect 盯着栏的开合，一收它就又展开
  const asideWidth = () => page.locator('main + aside').evaluate((el) => Math.round(el.getBoundingClientRect().width))
  await page.locator('.react-flow__node[data-id="lookup"]').click()
  await page.waitForTimeout(200)
  const picked = await S(page, () => window.__studio.getState().selectedId)
  await page.keyboard.press('Alt+KeyA')
  await page.waitForTimeout(250)
  const shut = { w: await asideWidth(), sel: await S(page, () => window.__studio.getState().selectedId) }
  check('选着节点按 ⌥A 也收得起助手栏（属性面板跟着收）', picked === 'lookup' && shut.w === 0 && shut.sel === null, JSON.stringify(shut))
  await page.locator('.react-flow__node[data-id="answer"]').click()
  await page.waitForTimeout(250)
  check('收着时选一个节点：栏展开，属性面板有地方待', await asideWidth() > 0 && await count(page, '[data-field="prompt"]') === 1)
  await page.getByRole('button', { name: '收起助手栏' }).click()
  await page.waitForTimeout(250)
  check('选着节点点「收起助手栏」也收得起来', await asideWidth() === 0)
  await page.getByRole('button', { name: '展开助手栏' }).click()
  await page.waitForTimeout(200)

  await page.keyboard.press('/')
  await page.waitForTimeout(200)
  check('「/」展开节点库并聚焦搜索', await S(page, () => document.activeElement?.id) === 'studio-palette-search')
  await blur(page)

  await page.keyboard.press('Alt+KeyH')
  await page.waitForTimeout(500)
  check('⌥H 打开版本历史', await count(page, '[role="dialog"][aria-label="版本历史"]') === 1)
  await page.keyboard.press('Escape')
  await page.waitForTimeout(150)

  await S(page, () => window.__studio.getState().select('lookup'))
  await blur(page)
  await S(page, () => window.__studio.getState().select('lookup'))
  const seq0 = await S(page, () => window.__studio.getState().focusRequest?.seq ?? 0)
  await page.keyboard.press('f')
  await page.waitForTimeout(100)
  const fr = await S(page, () => window.__studio.getState().focusRequest)
  check('F 把镜头对准选中的节点', fr?.id === 'lookup' && fr.seq > seq0, JSON.stringify(fr))

  // ⌘S 保存（走伪造的 PATCH）
  await S(page, () => window.__studio.getState().updateNode('lookup', { label: '背景检索2' }))
  await blur(page)
  await page.keyboard.press(`${MOD}+s`)
  await page.waitForTimeout(500)
  check('⌘S 保存', state.patches.length === 1 && !(await st(page)).dirty, `${state.patches.length} 次 PATCH`)

  // 刷新后收起状态还在（上面的「/」把它展开了，先收回去）
  await blur(page)
  await page.keyboard.press('Alt+KeyN')
  await page.waitForTimeout(150)
  await page.reload({ waitUntil: 'networkidle' })
  await page.waitForTimeout(500)
  check('刷新后节点库仍是收起的', await width('aside:has([aria-label^="节点库"])') === 48)
  await ctx.close()

  const narrow = await open({ width: 1024, height: 760 })
  check('1024 宽默认收起节点库', await narrow.page.locator('aside:has([aria-label^="节点库"])')
    .evaluate((el) => Math.round(el.getBoundingClientRect().width)) === 48)
  const tb = await narrow.page.evaluate(() => {
    const t = document.querySelector('.\\@container')
    return { sw: t.scrollWidth, cw: t.clientWidth, h: Math.round(t.getBoundingClientRect().height) }
  })
  check('1024 宽工具栏不溢出、不超过 48px', tb.sw <= tb.cw && tb.h <= 48, JSON.stringify(tb))
  await narrow.ctx.close()
  const wide = await open({ width: 1440 })
  check('1440 宽默认展开节点库', await wide.page.locator('aside:has([aria-label^="节点库"])')
    .evaluate((el) => Math.round(el.getBoundingClientRect().width)) === 208)
  await wide.ctx.close()
}

// ================================================================ 4b
console.log('=== 分析失败不静默 ===')
{
  const { ctx, page } = await open()
  await waitAnalysis(page)
  let down = true
  await page.route(/\/api\/workflows\/(validate|variables)$/, (route) => (down
    ? route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: '后端忙不过来' }) })
    : route.continue()))
  await S(page, () => window.__studio.getState().analyzeNow())
  await page.waitForTimeout(300)
  const chip = page.locator('button:has-text("分析失败 · 重试")')
  check('分析失败时工具栏写「分析失败 · 重试」，不再说可运行', await chip.count() === 1
    && await page.getByText('可运行').count() === 0)
  await blur(page)
  await page.keyboard.press('Alt+KeyP')
  await page.waitForTimeout(200)
  check('问题面板说清分析失败、给重试', (await page.locator('#dock-problems').innerText()).includes('分析失败'))
  const dockText = await page.locator('#dock-problems').innerText()
  const staleRows = await count(page, '#dock-problems [data-problem]')
  check('分析失败时照样列出上一次的问题，并说明可能过时', staleRows > 0 && dockText.includes('上一次校验'), `${staleRows} 行`)
  check('校验接口报错（后端没断）不承诺会自动重试', dockText.includes('点「重试」') && !dockText.includes('自动重新校验'))
  const kept = await S(page, () => window.__studio.getState().issues)
  await S(page, () => window.__studio.setState({ issues: [] }))
  await page.waitForTimeout(100)
  check('没有旧清单可列时不说「下面列的」', (await page.locator('#dock-problems').innerText()).includes('问题清单暂时拿不到')
    && !(await page.locator('#dock-problems').innerText()).includes('下面'))
  await page.evaluate((v) => window.__studio.setState({ issues: v }), kept)
  down = false
  await chip.click()
  await page.waitForFunction(() => window.__studio.getState().analysis === 'ok', null, { timeout: 5000 }).catch(() => {})
  check('点重试恢复', await chip.count() === 0 && await S(page, () => window.__studio.getState().analysis) === 'ok')
  await ctx.close()
}

// ================================================================ 5
console.log('=== 版本历史：预览、恢复成一次可撤销的改动 ===')
{
  const { ctx, page, state } = await open()
  await blur(page)
  await page.keyboard.press('Alt+KeyH')
  await page.waitForTimeout(500)
  const rows = await count(page, 'ol[aria-label="版本"] > li')
  check('列出全部版本', rows === 3, `${rows} 版`)
  check('标出已发布的那一版', (await page.locator('ol[aria-label="版本"] > li').nth(1).innerText()).includes('已发布'))
  await page.locator('ol[aria-label="版本"] > li').nth(2).locator('button').first().click()
  await page.waitForTimeout(500)
  check('选一版给出缩略图预览', await count(page, 'svg[aria-label^="缩略图"]') === 1)
  check('预览列出恢复后会拿掉的节点', (await page.locator('[role="dialog"][aria-label="版本历史"]').innerText()).includes('拿掉'))
  const before = await st(page)
  await page.locator('button:has-text("恢复 v1 到画布")').click()
  await page.waitForTimeout(300)
  const after = await st(page)
  const note = await S(page, () => window.__studio.getState().pendingNote)
  check('恢复：画布换成 v1', after.nodes.length === V1.nodes.length, `${after.nodes.length} 个节点`)
  check('恢复不调后端 restore（受管不会被悄悄保留）', !writes.some((w) => w.includes('/restore')))
  check('保存时写「回滚到 v1」', note === '回滚到 v1', note)
  await blur(page)
  await page.keyboard.press(`${MOD}+z`)
  await page.waitForTimeout(200)
  check('恢复是一次可撤销的改动', (await st(page)).nodes.length === before.nodes.length)
  await page.keyboard.press(`${MOD}+Shift+z`)
  await page.keyboard.press(`${MOD}+s`)
  await page.waitForTimeout(400)
  check('保存带上版本说明', state.patches[0]?.note === '回滚到 v1', JSON.stringify(state.patches[0] ?? {}).slice(0, 60))
  await ctx.close()
}

// ================================================================ 6
console.log('=== 助手改图：一步撤销、回执、只回话、失败退回、停止、再修一次、记忆 ===')
{
  const { ctx, page, state, errors } = await open()
  const mem = await S(page, () => window.__studio.getState().copilotMemory)
  check('打开图就取回这条会话之前的轮次', mem.turns === 2 && mem.past.length === 2, JSON.stringify({ turns: mem.turns }))

  // 1) 应用：改一个节点、加一个节点；final 里旧节点坐标不动
  const pos0 = await S(page, () => Object.fromEntries(window.__studio.getState().nodes.map((n) => [n.id, n.position])))
  const finalGraph = {
    nodes: [
      ...GRAPH.nodes.map((n) => (n.id === 'answer'
        ? { ...n, data: { ...n.data, config: { ...n.data.config, prompt: '{{ input.goal }} 简短回答' } } }
        : n)),
      node('polish', 'llm', 1180, 40, '润色', { prompt: '{{ nodes.answer.text }}' }),
    ],
    edges: [...GRAPH.edges, edge('answer', 'polish')],
  }
  state.stream = [
    { op: 'model', model: 'fake-model' },
    { op: 'plan', summary: '加一步润色' },
    { op: 'update_node', id: 'answer', config: finalGraph.nodes[3].data.config },
    { op: 'add_node', node: { id: 'polish', type: 'llm', label: '润色', config: { prompt: '{{ nodes.answer.text }}' } } },
    { op: 'add_edge', edge: { source: 'answer', target: 'polish' } },
    { op: 'done', explanation: '加了润色' },
    { op: 'check', status: 'passed', repaired: 0 },
    { op: 'final', graph: finalGraph, explanation: '加了润色', layout: { mode: 'keep', placed: ['polish'] },
      issues: [{ level: 'warning', node_id: null, code: 'unknown_node_type', type: 'magic',
        message: '模型写了一个不存在的节点类型「magic」，这一步已跳过' }] },
  ]
  const past0 = (await st(page)).past
  const began = await S(page, () => window.__studio.getState().runCopilot('给快速回答后面加一步润色', true))
  await page.waitForFunction(() => window.__studio.getState().copilotTurns.at(-1)?.phase !== 'running')
  const t1 = await S(page, () => window.__studio.getState().copilotTurns.at(-1))
  check('开始了就告诉输入框（runCopilot 返回 true）', began === true, String(began))
  check('收到 final：这一轮是「已应用」', t1.outcome === 'applied', t1.outcome)
  check('diff 记下新增和改动', t1.diff?.added.includes('polish') && t1.diff?.changed.includes('answer'), JSON.stringify(t1.diff))
  check('final.issues 进了这一轮（少了一步）', t1.issues?.some((i) => i.code === 'unknown_node_type'))
  const pos1 = await S(page, () => Object.fromEntries(window.__studio.getState().nodes.map((n) => [n.id, n.position])))
  check('旧节点留在原位，不整图重排', Object.keys(pos0).every((id) => pos1[id].x === pos0[id].x && pos1[id].y === pos0[id].y))
  check('整轮只占一步撤销', (await st(page)).past === past0 + 1)
  check('回执：已应用 N 处改动 · 撤销', await page.getByText(/已应用 \d+ 处改动/).count() === 1)
  check('新节点高亮', (await S(page, () => window.__studio.getState().copilotNew)).includes('polish'))
  await page.waitForTimeout(450)
  const landed = await S(page, () => window.__studio.getState().nodes.find((n) => n.id === 'polish')?.position)
  check('新节点从临时位置滑到排版位置，落定在后端给的坐标', landed?.x === 1180 && landed?.y === 40, JSON.stringify(landed))
  await blur(page)
  await page.keyboard.press(`${MOD}+z`)
  await page.waitForTimeout(200)
  const undone = await S(page, () => {
    const s = window.__studio.getState()
    return { polish: s.nodes.some((n) => n.id === 'polish'), prompt: s.nodes.find((n) => n.id === 'answer').data.config.prompt }
  })
  check('一次 ⌘Z 撤掉整轮改图', !undone.polish && undone.prompt.includes('vars.knowledge'), JSON.stringify(undone))
  await page.keyboard.press(`${MOD}+Shift+z`)
  await page.waitForTimeout(150)
  check('⇧⌘Z 再应用回来', await S(page, () => window.__studio.getState().nodes.some((n) => n.id === 'polish')))

  // 1b) 前端认不出的类型：后端 final.issues 没说到的，这一轮也要记下「少了一步」
  state.stream = [
    { op: 'model', model: 'fake-model' },
    { op: 'add_node', node: { id: 'm1', type: 'wizard', label: '魔法', config: {} } },
    { op: 'update_node', id: 'answer', label: '快速回答' },
    { op: 'final', graph: finalGraph, explanation: '', layout: { mode: 'keep', placed: [] }, issues: [] },
  ]
  await S(page, () => window.__studio.getState().runCopilot('加一步魔法', true))
  await page.waitForFunction(() => window.__studio.getState().copilotTurns.at(-1)?.phase !== 'running')
  const tw = await S(page, () => window.__studio.getState().copilotTurns.at(-1))
  check('认不出的节点类型不上画布，但记成「少了一步」', tw.issues?.some((i) => i.code === 'unknown_node_type' && i.type === 'wizard')
    && !(await st(page)).nodes.includes('m1'), JSON.stringify(tw.issues))

  // 2) 只回了一句话
  state.stream = [{ op: 'model', model: 'fake-model' }, { op: 'reply', text: '分支按 **input.goal** 分' }]
  const b2 = await st(page)
  await S(page, () => window.__studio.getState().runCopilot('分支是按什么分的？', true))
  await page.waitForFunction(() => window.__studio.getState().copilotTurns.at(-1)?.phase !== 'running')
  const t2 = await S(page, () => window.__studio.getState().copilotTurns.at(-1))
  check('reply：这一轮记下回答', t2.reply?.includes('input.goal') && t2.outcome === 'answered', t2.outcome)
  check('reply：画布没动、不占撤销栈', (await st(page)).nodes.length === b2.nodes.length && (await st(page)).past === b2.past)

  // 3) 从头生成，搭到一半失败：画布退回原样，半成品能 ⇧⌘Z 找回
  state.stream = [
    { op: 'model', model: 'fake-model' },
    { op: 'add_node', node: { id: 'x1', type: 'llm', label: '半截', config: {} } },
    { op: 'error', message: '助手这一轮没跑完：模型超时' },
  ]
  const b3 = await st(page)
  await S(page, () => window.__studio.getState().runCopilot('重新设计', false))
  await page.waitForFunction(() => window.__studio.getState().copilotTurns.at(-1)?.phase !== 'running')
  const t3 = await S(page, () => window.__studio.getState().copilotTurns.at(-1))
  const a3 = await st(page)
  check('失败：画布退回这一轮之前（从头生成也不丢原图）', a3.nodes.join() === b3.nodes.join(), `${a3.nodes.length}/${b3.nodes.length}`)
  check('失败：这一轮标成已退回', t3.outcome === 'reverted' && t3.phase === 'error', `${t3.outcome}/${t3.phase}`)
  check('失败：半成品进了重做栈', a3.future === 1)
  check('失败：新节点高亮清掉', (await S(page, () => window.__studio.getState().copilotNew.length)) === 0)

  // 4) 生成中锁画布与工具栏，停止
  state.stream = 'hang'
  await S(page, () => window.__studio.getState().runCopilot('慢慢想', true))
  await page.waitForTimeout(300)
  const bar = page.locator('div:has(> [role="status"]:has-text("助手正在改这张工作流"))')
  check('生成中画布上方有进度条和停止', await bar.locator('button:has-text("停止")').count() === 1)
  const spoken = await page.locator('[role="status"]:has-text("助手正在改这张工作流")').innerText()
  check('播报区只圈阶段文字，不带 100ms 一跳的计时', !/\d\d:\d\d/.test(spoken)
    && /\d\d:\d\d/.test(await bar.innerText()), spoken)
  const n4 = (await st(page)).nodes.length
  await S(page, () => window.__studio.getState().addNode('llm'))
  check('生成中编辑被锁住', (await st(page)).nodes.length === n4)
  check('生成中工具栏锁住（保存、撤销、运行）', await page.locator('fieldset[disabled]').count() >= 1)
  await bar.locator('button:has-text("停止")').click()
  await page.waitForTimeout(200)
  const t4 = await S(page, () => ({ active: window.__studio.getState().copilot.active, turn: window.__studio.getState().copilotTurns.at(-1) }))
  check('停止：解锁，这一轮收尾', !t4.active && t4.turn.phase === 'error' && t4.turn.error === '已停止', JSON.stringify(t4.turn?.error))

  // 5) 让助手再修一次：把剩下的问题拼成指令，在现有的图上改
  state.stream = [
    { op: 'model', model: 'fake-model' },
    { op: 'check', status: 'failed', issues: ['「查询销量」：「调用工具」节点还没选工具'] },
    { op: 'final', graph: GRAPH, explanation: '', layout: { mode: 'keep', placed: [] }, issues: [] },
  ]
  await S(page, () => window.__studio.getState().runCopilot('检查一下', true))
  await page.waitForFunction(() => window.__studio.getState().copilotTurns.at(-1)?.phase !== 'running')
  state.stream = [{ op: 'model', model: 'fake-model' }, { op: 'reply', text: '好' }]
  const turnId = await S(page, () => window.__studio.getState().copilotTurns.at(-1).id)
  await S(page, (id) => window.__studio.getState().repairWithCopilot(id), turnId)
  await page.waitForTimeout(400)
  const lastBody = state.bodies.at(-1)
  check('再修一次：指令里带着剩下的问题', lastBody?.instruction?.includes('还没选工具') && !!lastBody?.base_graph,
    (lastBody?.instruction ?? '').slice(0, 40))

  // 6) 开始新对话：换会话，记忆清零
  await S(page, () => window.__studio.getState().newCopilotConversation())
  await page.waitForTimeout(200)
  const m2 = await S(page, () => ({ id: window.__studio.getState().copilotConversationId, turns: window.__studio.getState().copilotMemory.turns }))
  check('开始新对话：换一条会话，模型上下文清零', m2.id === 'conv-new' && m2.turns === 0, JSON.stringify(m2))
  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))
  await ctx.close()
}

// ================================================================ 7
console.log('=== 入口：?run=&focus=、选择器、发布弹窗 ===')
{
  const { ctx, page } = await open({ path: `/studio/st-main?run=${FAKE_RUN}&focus=answer` })
  await page.waitForFunction(() => window.__studio.getState().run != null, null, { timeout: 5000 }).catch(() => {})
  await page.waitForTimeout(400)
  const s = await S(page, () => ({ run: window.__studio.getState().run?.id, focus: window.__studio.getState().focusRequest?.id }))
  check('?run= 接上那次运行', s.run === FAKE_RUN, s.run)
  check('?focus= 请画布取景到那个节点', s.focus === 'answer', s.focus)

  await page.locator('button[title^="切换、新建"]').click()
  await page.waitForTimeout(300)
  await page.keyboard.type('__studio_check')
  await page.waitForTimeout(200)
  const rows = await count(page, '[role="listbox"][aria-label="工作流"] > li')
  check('选择器能搜索', rows === Object.keys(FAKES).length, `${rows} 条`)
  check('当前工作流有底色和勾', await count(page, 'li[aria-selected="true"] .bg-accent-solid') === 1)
  check('模板行常驻「用它新建」', await count(page, 'li:has-text("__studio_check_tpl__") button:has-text("用它新建")') === 1)
  check('标签本地化（extracted → 从运行提取）', await count(page, 'li:has-text("__studio_check_tpl__") :text("从运行提取")') === 1)
  const mainRow = page.locator('li[role="option"]', { has: page.locator('span.truncate', { hasText: /^__studio_check__$/ }) })
  const chips = await mainRow.locator('.chip').allInnerTexts()
  check('发布后又改过的草稿：选择器同时写「草稿」和「已发布 v2」', chips.includes('草稿') && chips.some((t) => t.includes('已发布 v2')), chips.join(' | '))
  await page.keyboard.press('Escape')
  await ctx.close()

  // 发布只换状态和发布人，图没动：撤销栈、镜头都留着
  const pub = await open()
  let published = false
  await pub.page.route(/\/api\/workflows\/st-main\/publish$/, (route) => {
    published = true
    return route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ ok: true, level: 'published', version: 3, issues: [] }) })
  })
  await pub.page.route(/\/api\/workflows\/st-main$/, (route) => (route.request().method() === 'GET' && published
    ? route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ ...FAKES['st-main'], status: 'published', published_version: 3, published_by: '检查脚本' }) })
    : route.fallback()))
  await S(pub.page, () => { const s = window.__studio.getState(); s.updateNode('lookup', { label: '背景检索2' }); s.undo() })
  const pb = await S(pub.page, () => { const s = window.__studio.getState(); return { future: s.future.length, fit: s.fitRequest, dirty: s.dirty } })
  await pub.page.getByRole('button', { name: '发布', exact: true }).click()
  await pub.page.waitForSelector('[role="dialog"] button.btn-primary', { timeout: 5000 }).catch(() => {})
  await pub.page.waitForTimeout(300)
  await pub.page.locator('[role="dialog"] button.btn-primary').click()
  await pub.page.waitForFunction(() => window.__studio.getState().workflow?.status === 'published', null, { timeout: 5000 }).catch(() => {})
  await pub.page.waitForTimeout(200)
  const pa = await S(pub.page, () => { const s = window.__studio.getState(); return { future: s.future.length, fit: s.fitRequest, status: s.workflow?.status, pv: s.workflow?.published_version } })
  check('发布之后状态跟着变（已发布 v3）', pa.status === 'published' && pa.pv === 3, JSON.stringify(pa))
  check('发布不清撤销栈、不重新取景', !pb.dirty && pb.future === 1 && pa.future === 1 && pa.fit === pb.fit, JSON.stringify({ pb, pa }))
  await pub.ctx.close()

  // 快捷键提示跟着平台走：Windows 上不该写 ⌘ ⌫
  const win = await open({ platform: 'Windows' })
  await S(win.page, () => window.__studio.getState().select('answer'))
  await win.page.waitForTimeout(300)
  const delTitle = await win.page.getByRole('button', { name: '删除节点', exact: true }).getAttribute('title')
  const undoTitle = await win.page.getByRole('button', { name: '撤销', exact: true }).first().getAttribute('title')
  check('非 Mac 上快捷键提示写 Backspace / Ctrl+Z，不写 ⌫ ⌘', /Backspace/.test(delTitle) && /Ctrl\+Z/.test(undoTitle)
    && !/[⌘⌫]/.test(delTitle + undoTitle), `${delTitle} · ${undoTitle}`)
  await win.ctx.close()

  const gov = await open({ path: '/studio/st-gov' })
  // 按名字找：别的按钮（版本标签、提示）也可能带着「发布」两个字
  await gov.page.getByRole('button', { name: '发布', exact: true }).click()
  await gov.page.waitForSelector('[role="dialog"] [role="radio"]', { timeout: 5000 }).catch(() => {})
  await gov.page.waitForTimeout(300)
  // 只看发布弹窗里的：助手输入框的「在现有图上改 | 从头生成」也是一组 radio
  const radio = await gov.page.locator('[role="dialog"] [role="radio"][aria-checked="true"]').innerText()
  check('受管工作流的发布默认选「受管」', radio.includes('受管'), radio.slice(0, 20))
  check('选项用统一术语（已发布 — 可发起正式运行）', await gov.page.getByText('已发布 — 可发起正式运行').count() === 1)
  check('写明以谁的名义发布', await gov.page.getByText('检查脚本').count() >= 1)
  await gov.page.locator('[role="dialog"] [role="radio"]:has-text("已发布")').click()
  check('从受管降级要说出来', await gov.page.getByText(/会降级/).count() === 1)
  await gov.ctx.close()
}

// ================================================================ 6b
console.log('=== 助手改节点是合并，不是整体替换（NI-1）；出错留下「怎么办」 ===')
{
  const { ctx, page, state, errors } = await open({ path: '/studio/st-team' })
  // 流在最后一步出错：画布退回这一轮之前，半成品进重做栈。读重做栈顶就是按操作流逐条
  // 落上去的样子——final 会拿后端合并好的整张图覆盖，只看 final 看不出前端合得对不对
  state.stream = [
    { op: 'model', model: 'fake-model' },
    { op: 'update_node', id: 'fetch', label: '数据查询',
      config: { system: '你是取数助手，按月份汇总。', prompt: '按月份统计 orders 的订单数' } },
    { op: 'update_node', id: 'team', config: { goal: '核对按月的订单数', agents: [
      { name: 'fetcher', system: '按月份取数' },
      { name: 'writer', remove: true },
      { name: 'checker', description: '复核口径', tools: ['db_schema__shop'], system: null },
    ] } },
    { op: 'update_node', id: 'fetch', config: { max_steps: null } },
    { op: 'error', message: '助手这一轮没跑完：等待超时', hint: '换一个响应快的模型再试', detail: 'ReadTimeout: 120s' },
  ]
  await S(page, () => window.__studio.getState().runCopilot('按月份拆一下', true))
  await page.waitForFunction(() => window.__studio.getState().copilotTurns.at(-1)?.phase !== 'running')
  const half = await S(page, () => {
    const s = window.__studio.getState()
    const nodes = s.future.at(-1)?.nodes ?? []
    const cfg = (id) => nodes.find((n) => n.id === id)?.data.config ?? null
    return { fetch: cfg('fetch'), team: cfg('team') }
  })
  const f = half.fetch ?? {}
  const members = half.team?.agents ?? []
  const fetcher = members.find((m) => m.name === 'fetcher') ?? {}
  const checker = members.find((m) => m.name === 'checker') ?? {}
  check('只改提示词：agent 的工具一个不少', JSON.stringify(f.tools) === JSON.stringify(QUERY_TOOLS), JSON.stringify(f.tools))
  check('写了的键改上去、没写的键保留', f.prompt === '按月份统计 orders 的订单数' && f.assign_to === 'rows',
    `${f.prompt} / ${f.assign_to}`)
  check('写 null 的键删掉', !('max_steps' in f), JSON.stringify(Object.keys(f)))
  check('成员按 name 合并：没写 tools 的保留原来的 tools 和别的字段',
    JSON.stringify(fetcher.tools) === '["db_query__shop"]' && fetcher.system === '按月份取数' && fetcher.description === '负责查库',
    JSON.stringify(fetcher))
  check('{name, remove:true} 删掉成员，新名字接在后面', members.map((m) => m.name).join(',') === 'fetcher,checker',
    members.map((m) => m.name).join(','))
  check('新成员里写 null 的字段不留', !('system' in checker) && JSON.stringify(checker.tools) === '["db_schema__shop"]',
    JSON.stringify(checker))
  check('团队没写的顶层键保留', half.team?.max_rounds === 4 && half.team?.goal === '核对按月的订单数')
  const cp = await S(page, () => window.__studio.getState().copilot)
  check('出错时保留「怎么办」和技术细节（Composer 的错误条要用）',
    cp.error.includes('等待超时') && cp.errorHint === '换一个响应快的模型再试' && cp.errorDetail === 'ReadTimeout: 120s',
    JSON.stringify({ hint: cp.errorHint, detail: cp.errorDetail }))
  const t0 = await S(page, () => window.__studio.getState().copilotTurns.at(-1))
  check('这一轮记下开始时刻（助手流头部的实时计时用）', typeof t0.startedAt === 'number'
    && Math.abs(Date.now() - t0.startedAt) < 60_000, String(t0.startedAt))

  // 改图回执：后端 final 带回的工具绑定变化。工具集合变小、指令又没让删的，warn 色、可一键撤销
  const dropped = {
    ...TEAM_BOUND,
    nodes: TEAM_BOUND.nodes.map((n) => {
      if (n.id === 'fetch') return { ...n, data: { ...n.data, config: { ...n.data.config, prompt: '简短一点', tools: [] } } }
      if (n.id === 'team') {
        return { ...n, data: { ...n.data, config: { ...n.data.config,
          agents: n.data.config.agents.map((a) => (a.name === 'fetcher' ? { ...a, tools: [] } : a)) } } }
      }
      return n
    }),
  }
  const warn = (id, field, message) => ({ level: 'warning', node_id: id, edge_id: null, code: 'tools_dropped', field, message })
  const warnings = [
    warn('fetch', 'tools', '「数据查询」的工具从 db_query__shop、db_schema__shop 变成了空。这一轮的要求里没有提到去掉工具，确认一下是不是改漏了'),
    warn('team', 'agents[0].tools', '「复核团队」的成员「fetcher」的工具从 db_query__shop 变成了空。这一轮的要求里没有提到去掉工具，确认一下是不是改漏了'),
  ]
  const changes = [
    { node_id: 'fetch', label: '数据查询', member: null, field: 'tools', before: QUERY_TOOLS, after: [], added: [], removed: QUERY_TOOLS },
    { node_id: 'team', label: '复核团队', member: 'fetcher', field: 'agents[0].tools', before: ['db_query__shop'], after: [], added: [], removed: ['db_query__shop'] },
  ]
  state.stream = [
    { op: 'model', model: 'fake-model' },
    { op: 'update_node', id: 'fetch', config: { prompt: '简短一点', tools: [] } },
    { op: 'update_node', id: 'team', config: { agents: [{ name: 'fetcher', tools: [] }] } },
    { op: 'done', explanation: '改短了' },
    { op: 'check', status: 'passed', repaired: 0, warnings },
    { op: 'final', graph: dropped, explanation: '改短了', layout: { mode: 'keep', placed: [] },
      issues: [...warnings, { level: 'warning', node_id: 'done', edge_id: null, message: '成果字段引用的变量可能为空' }],
      tool_changes: changes },
  ]
  await S(page, () => window.__studio.getState().runCopilot('提示词写短一点', true))
  await page.waitForFunction(() => window.__studio.getState().copilotTurns.at(-1)?.phase !== 'running')
  const t1 = await S(page, () => window.__studio.getState().copilotTurns.at(-1))
  check('回执记下后端给的工具绑定变化', t1.toolChanges?.length === 2 && t1.toolChanges[0].node_id === 'fetch',
    JSON.stringify(t1.toolChanges?.map((c) => c.node_id)))
  check('工具被删的提醒单独记，不混进普通校验提示', t1.toolWarnings?.length === 2
    && t1.toolWarnings.every((w) => w.code === 'tools_dropped'), JSON.stringify(t1.toolWarnings?.map((w) => w.node_id)))
  check('自查那一步也带着这两条提醒', t1.check?.warnings?.length === 2, JSON.stringify(t1.check?.warnings))
  check('记下的 final 操作带着 tool_changes（右栏回执从它解码）',
    t1.ops.find((o) => o.op === 'final')?.tool_changes?.length === 2)
  const toastBox = page.locator('[role="status"] > div').filter({ hasText: '工具绑定变少了' })
  const toastText = (await toastBox.first().innerText().catch(() => '')).replace(/\s+/g, ' ')
  check('工具集合变小：回执是 warn 色，逐条列出前后', await toastBox.count() === 1
    && toastText.includes('「数据查询」') && toastText.includes('db_query__shop、db_schema__shop → 空')
    && toastText.includes('fetcher'), toastText.slice(0, 120))
  check('工具被删的回执用 warn 色边框', await toastBox.first().evaluate((el) =>
    el.getAttribute('style')?.includes('var(--warn)'), null, { timeout: 2000 }).catch(() => false))
  await toastBox.first().locator('button:has-text("撤销")').click({ timeout: 2000 }).catch(() => {})
  await page.waitForTimeout(250)
  const back = await S(page, () => window.__studio.getState().nodes.find((n) => n.id === 'fetch').data.config.tools)
  check('回执里一键撤销：工具回来了', JSON.stringify(back) === JSON.stringify(QUERY_TOOLS), JSON.stringify(back))

  // 老后端的 final 不带 tool_changes：前端自己比对前后两张图
  state.stream = [
    { op: 'model', model: 'fake-model' },
    { op: 'update_node', id: 'fetch', config: { tools: ['db_query__shop', 'python_exec'] } },
    { op: 'final', graph: { ...TEAM_BOUND, nodes: TEAM_BOUND.nodes.map((n) => (n.id === 'fetch'
      ? { ...n, data: { ...n.data, config: { ...n.data.config, tools: ['db_query__shop', 'python_exec'] } } } : n)) },
      explanation: '', layout: { mode: 'keep', placed: [] }, issues: [] },
  ]
  await S(page, () => window.__studio.getState().runCopilot('查表结构换成算一下', true))
  await page.waitForFunction(() => window.__studio.getState().copilotTurns.at(-1)?.phase !== 'running')
  const t2 = await S(page, () => window.__studio.getState().copilotTurns.at(-1))
  const c2 = t2.toolChanges?.[0]
  check('老后端不给 tool_changes：前端自己比对出来', t2.toolChanges?.length === 1 && c2.node_id === 'fetch'
    && c2.added.join() === 'python_exec' && c2.removed.join() === 'db_schema__shop' && c2.member === null,
    JSON.stringify(t2.toolChanges))
  check('换了一个工具（数量没少）不算被删，没有提醒', !(t2.toolWarnings ?? []).length)
  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))
  await ctx.close()

  // 合并规则本身：和后端 merge_node_config 同一组用例（backend/tests/test_copilot_merge.py）
  const unit = await open({ path: '/studio/st-team' })
  const r = await unit.page.evaluate(async () => {
    const m = await import('/src/canvas/copilotMerge.ts')
    const old = { prompt: 'p', tools: ['db_query__shop', 'db_schema__shop'], assign_to: 'rows',
      agents: [{ name: '取数员', system: '只用 SQL 取数', tools: ['db_query__shop'] }, { system: '无名', tools: ['x'] }] }
    const snapshot = JSON.stringify(old)
    const a = m.mergeNodeConfig('agent', old, { assign_to: null, temperature: 0.2 })
    const b = m.mergeNodeConfig('agent', old, { tools: ['db_query__shop'] })
    const c = m.mergeNodeConfig('supervisor', old, { agents: [
      { name: 'agent1', tools: null }, { name: '核对员', tools: ['db_schema__shop'], note: null }, { name: '取数员', remove: true }] })
    // agents 不是列表、或者不是 supervisor：整体替换这一个键（和后端一样只对 supervisor 的成员列表按 name 合并）
    const d = m.mergeNodeConfig('agent', old, { agents: [{ name: 'x' }] })
    return { a, b, c, d, untouched: JSON.stringify(old) === snapshot }
  })
  check('合并：null 删键，没写的留着', !('assign_to' in r.a) && r.a.temperature === 0.2 && r.a.tools.length === 2, JSON.stringify(r.a))
  check('合并：显式写 tools 就换成新的', r.b.tools.join() === 'db_query__shop')
  // 没名字的那个按 agent1 认出来、合并进去：条目里写的 name 也跟着落上（后端同样如此）
  check('合并：没名字的成员按 agentN 认；删成员、新名字接在后面', r.c.agents.map((x) => x.name).join() === 'agent1,核对员'
    && !('tools' in r.c.agents[0]) && r.c.agents[0].system === '无名' && !('note' in r.c.agents[1]), JSON.stringify(r.c.agents))
  check('合并：不是 supervisor 时 agents 整体替换', JSON.stringify(r.d.agents) === '[{"name":"x"}]')
  check('合并：旧 config 不被原地改（比对工具变化还要用它）', r.untouched)
  await unit.ctx.close()
}

// ================================================================ 6c
console.log('=== 正式运行期间画布只读：调色板、粘贴、复制、排版、检查器、撤销都拦下 ===')
{
  const { ctx, page, errors, state } = await open({ path: '/studio/st-team' })
  const layoutCalls = []
  page.on('request', (req) => { if (req.url().includes('/copilot/layout')) layoutCalls.push(req.url()) })
  const formal = (phase) => page.evaluate((p) => window.__studio.setState((s) => ({
    run: { id: 'st-formal-0001', workflow_id: 'st-team', status: p === 'succeeded' ? 'succeeded' : 'running',
      run_class: 'formal', version: 2, input: {}, output: {}, error: '', usage: {}, created_at: '2026-09-26T02:00:00Z' },
    runPhase: p, trace: { ...s.trace, phase: p, runClass: 'formal' },
  })), phase)
  // 先选中一个节点、复制下来，再开始正式运行
  await S(page, () => window.__studio.getState().select('fetch'))
  await blur(page)
  await S(page, () => window.__studio.getState().select('fetch'))
  await page.evaluate(() => document.activeElement instanceof HTMLElement && document.activeElement.blur())
  await page.keyboard.press(`${MOD}+c`)
  // 助手先改一轮（改成果节点的名字），正式运行开始后再点这一轮的「撤销这次生成」
  const renamed = { ...TEAM_BOUND, nodes: TEAM_BOUND.nodes.map((n) => (n.id === 'done'
    ? { ...n, data: { ...n.data, label: '成果（按月）' } } : n)) }
  state.stream = [
    { op: 'model', model: 'fake-model' },
    { op: 'update_node', id: 'done', label: '成果（按月）' },
    { op: 'final', graph: renamed, explanation: '', layout: { mode: 'keep', placed: [] }, issues: [] },
  ]
  await S(page, () => window.__studio.getState().runCopilot('成果改个名字', true))
  await page.waitForFunction(() => window.__studio.getState().copilotTurns.at(-1)?.phase !== 'running')
  const turnId = await S(page, () => window.__studio.getState().copilotTurns.at(-1).id)
  await formal('running')
  await page.waitForTimeout(200)
  const s0 = await st(page)

  await page.locator('button:has-text("模型调用"), button[aria-label="添加模型调用"]').first().click({ force: true, timeout: 2000 }).catch(() => {})
  await S(page, () => window.__studio.getState().addNode('llm'))
  check('调色板点击添加被拦下', (await st(page)).nodes.length === s0.nodes.length)
  check('调色板标成不可用并说明为什么', await count(page, '[aria-label^="节点库"] [aria-disabled="true"]') === 1
    && (await page.locator('[aria-label^="节点库"]').innerText()).includes('只读'))

  await page.evaluate(() => document.activeElement instanceof HTMLElement && document.activeElement.blur())
  await page.keyboard.press(`${MOD}+v`)
  await page.keyboard.press(`${MOD}+d`)
  await page.keyboard.press('Shift+KeyL')
  await page.keyboard.press(`${MOD}+z`)
  await page.waitForTimeout(400)
  const s1 = await st(page)
  check('⌘V 粘贴、⌘D 复制、⇧L 排版、⌘Z 撤销都不改图', s1.nodes.length === s0.nodes.length && s1.past === s0.past
    && layoutCalls.length === 0, `${s1.nodes.length}/${s0.nodes.length} 节点 · 排版请求 ${layoutCalls.length}`)
  check('按了改图快捷键会说为什么不行', await page.getByText(/正式运行进行中/).count() >= 1)
  check('工具栏的撤销、自动排版置灰', await page.getByRole('button', { name: '自动排版' }).isDisabled()
    && await page.getByRole('button', { name: '撤销', exact: true }).first().isDisabled())
  // 撤销被锁拦下、画布没动：这一轮不能被标成「已撤回」，回执也不能说撤掉了
  const undone = await S(page, (id) => window.__studio.getState().undoCopilotTurn(id), turnId)
  const kept = await S(page, (id) => {
    const s = window.__studio.getState()
    return { outcome: s.copilotTurns.find((t) => t.id === id)?.outcome,
      label: s.nodes.find((n) => n.id === 'done')?.data.label }
  }, turnId)
  check('「撤销这次生成」也拦下，这一轮不被标成已撤回', undone === false && kept.outcome === 'applied'
    && kept.label === '成果（按月）', JSON.stringify({ undone, ...kept }))

  await S(page, () => window.__studio.getState().select('fetch'))
  await page.waitForTimeout(300)
  check('检查器整块只读，并说明原因', await count(page, 'fieldset[disabled]:has([data-field="prompt"])') === 1
    && await page.getByText('只能看，不能改').count() === 1)
  await S(page, () => window.__studio.getState().updateNode('fetch', { label: '改不动' }))
  check('store 这一层也拦着（直接调 updateNode 不生效）',
    (await S(page, () => window.__studio.getState().nodes.find((n) => n.id === 'fetch').data.label)) === '数据查询')
  const prompt0 = await S(page, () => window.__studio.getState().copilotTurns.length)
  // 返回 false：输入框据此留着用户刚写的那句，不能拦下了还把它清掉
  const started = await S(page, () => window.__studio.getState().runCopilot('加一步', true))
  check('正式运行期间不让助手改图（并告诉输入框没开始）', started === false
    && (await S(page, () => window.__studio.getState().copilotTurns.length)) === prompt0
    && !(await S(page, () => window.__studio.getState().copilot.active)), String(started))

  // 跑完就能改了
  await formal('succeeded')
  await page.waitForTimeout(150)
  await S(page, () => window.__studio.getState().addNode('llm'))
  check('正式运行结束后恢复编辑', (await st(page)).nodes.length === s0.nodes.length + 1)
  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))

  // Shift+1 的名字要和它做的事一致：它回到打开时的取景（从入口看起），不是「看全所有节点」
  const fit = await page.evaluate(async () => (await import('/src/canvas/shortcuts.ts')).studioShortcut('fit').label)
  check('Shift+1 叫「重新取景」', fit.startsWith('重新取景') && !fit.includes('看全'), fit)
  await ctx.close()
}

// ================================================================ 6d
console.log('=== 提示词点名的工具没绑定：问题面板定位到工具那一栏，一键绑定（NI-2）；团队用完轮数（NI-5） ===')
{
  const { ctx, page, errors } = await open({ path: '/studio/st-lint' })
  await page.waitForFunction(() => window.__studio.getState().analysis === 'ok', null, { timeout: 8000 })
  const lint = await S(page, () => window.__studio.getState().issues
    .filter((i) => /要求用「/.test(i.message)).map((i) => ({ node: i.node_id, field: i.field, msg: i.message.slice(0, 30) })))
  check('后端校验报出两处「提示词要求用 X 但没绑定」', lint.length === 2, JSON.stringify(lint))

  await page.locator('button[aria-expanded]').filter({ hasText: /错|提示|可运行/ }).first().click()
  await page.waitForTimeout(250)
  const agentRow = page.locator('[data-problem]').filter({ hasText: '节点没有绑定它' })
  check('问题面板里这一条指向「可用工具」', (await agentRow.innerText().catch(() => '')).includes('可用工具'),
    (await agentRow.innerText().catch(() => '')).replace(/\s+/g, ' ').slice(-20))
  const memberRow = page.locator('[data-problem]').filter({ hasText: '没有给这个成员绑定它' })
  check('成员那一条指向「成员「writer」的工具」', (await memberRow.innerText().catch(() => '')).includes('成员「writer」的工具'),
    (await memberRow.innerText().catch(() => '')).replace(/\s+/g, ' ').slice(-20))

  await agentRow.locator('button').first().click({ timeout: 2000 }).catch(() => {})
  await page.waitForTimeout(500)
  const tools = page.locator('[data-field="tools"]')
  check('点一下：检查器翻到工具那一栏，问题落在那儿', await S(page, () => window.__studio.getState().selectedId) === 'fetch'
    && (await tools.innerText()).includes('db_schema__shop'))
  await tools.locator('button:has-text("绑定 db_schema__shop")').click({ timeout: 2000 }).catch(() => {})
  await page.waitForTimeout(200)
  const bound = await S(page, () => window.__studio.getState().nodes.find((n) => n.id === 'fetch').data.config.tools)
  check('一键绑定：工具加进这个节点', JSON.stringify(bound) === JSON.stringify(['db_query__shop', 'db_schema__shop']), JSON.stringify(bound))

  await memberRow.locator('button').first().click({ timeout: 2000 }).catch(() => {})
  await page.waitForTimeout(500)
  const member = page.locator('[data-field="agents"] [data-item="1"]')
  check('成员那一条落在第 2 个成员的工具下面', (await member.locator('[data-sub="tools"]').innerText({ timeout: 2000 })
    .catch(() => '')).includes('没有给这个成员绑定它'))
  const inView = await member.locator('[data-sub="tools"]').evaluate((el) => {
    const r = el.getBoundingClientRect()
    return r.top >= 0 && r.bottom <= window.innerHeight
  }, null, { timeout: 2000 }).catch(() => false)
  check('镜头滚到那个成员的工具', inView)
  await member.locator('button:has-text("绑定 python_exec")').click({ timeout: 2000 }).catch(() => {})
  await page.waitForTimeout(200)
  const mt = await S(page, () => window.__studio.getState().nodes.find((n) => n.id === 'team').data.config.agents[1].tools)
  check('一键绑定到那个成员', JSON.stringify(mt) === '["python_exec"]', JSON.stringify(mt))
  await page.waitForFunction(() => !window.__studio.getState().issues.some((i) => /要求用「/.test(i.message)), null, { timeout: 6000 }).catch(() => {})
  check('绑好之后这两条问题消失', !(await S(page, () => window.__studio.getState().issues.some((i) => /要求用「/.test(i.message)))))

  // 数据源的查询工具要能在选择器里挑到：不然「把它加进工具里」这句话做不到
  await member.locator('button:has-text("添加")').click({ timeout: 2000 }).catch(() => {})
  await page.waitForTimeout(300)
  const pick = await member.innerText({ timeout: 2000 }).catch(() => '')
  check('工具选择器列出数据源的 db_query__ / db_schema__ 工具', /db_query__/.test(pick) && /db_schema__/.test(pick))
  await member.locator('button:has-text("收起")').click({ timeout: 2000 }).catch(() => {})

  // NI-5：协作团队用完轮数时怎么收场
  await S(page, () => window.__studio.getState().select('team'))
  await page.waitForTimeout(300)
  const ex = page.locator('[data-field="on_exhausted"]')
  const exText = await ex.innerText().catch(() => '')
  check('团队有「用完轮数时」，默认判为失败', exText.includes('用完轮数时')
    && String(await ex.locator('select').evaluate((el) => el.options[el.selectedIndex].text, null, { timeout: 2000 })
      .catch(() => '')).includes('判为失败'), exText.slice(0, 40))
  check('选项说清两者差别', /降档/.test(exText) && /失败/.test(exText) && exText.length > 40)
  // 报错和右栏都叫人「调大『最多轮数』」：字段名得是这四个字，照着找得到
  check('轮数字段叫「最多轮数」，和报错里的说法一致',
    (await page.locator('[data-field="max_rounds"]').innerText().catch(() => '')).includes('最多轮数'))
  await ex.locator('select').selectOption({ label: '降档交付' }, { timeout: 2000 }).catch(() => {})
  check('选降档交付写进配置', (await S(page, () => window.__studio.getState().nodes.find((n) => n.id === 'team').data.config.on_exhausted)) === 'degrade')
  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))
  await ctx.close()
}

// ================================================================ 7b
console.log('=== 工作流目录没取回来：不劝人新建（EmptyState source） ===')
{
  // 目录还在读、或者读失败时，「还没有工作流 · 新建」是一句假话：库里可能有几十张
  for (const mode of ['loading', 'error']) {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } })
    const page = await ctx.newPage()
    await page.route(/\/api\/workflows(\?.*)?$/, (route) => {
      if (route.request().method() !== 'GET') return route.fallback()
      return mode === 'loading' ? new Promise(() => {})
        : route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: '检查脚本伪造的 500' }) })
    })
    await page.goto(`${WEB}/studio`, { waitUntil: 'domcontentloaded' })
    // 目录一直不回来时，启动屏 5 秒后给「先进去看看」：进去之后才轮到编排页说话
    if (mode === 'loading') await page.getByRole('button', { name: '先进去看看' }).click({ timeout: 12000 }).catch(() => {})
    const hit = await page.waitForSelector(`[data-empty-unknown="${mode}"]`, { timeout: 8000 }).then(() => true).catch(() => false)
    check(mode === 'loading' ? '目录还在读：说「正在读取」' : '目录没取回来：说没取回来、给重新读取', hit
      && (mode === 'loading' ? /正在读取/ : /没取回来/).test(await page.locator('[data-empty-unknown]').innerText()))
    check(mode === 'loading' ? '读的时候不劝人新建' : '读失败时不劝人新建',
      await page.getByRole('button', { name: /新建工作流/ }).count() === 0)
    await ctx.close()
  }
}

// ================================================================ 8
console.log('=== 删掉眼前这张 ===')
{
  const removeCurrent = async (page) => {
    await page.locator('button[title^="切换、新建"]').click()
    await page.waitForTimeout(300)
    await page.getByRole('button', { name: '删除「__studio_check__」', exact: true }).click()
    await page.waitForTimeout(200)
    await page.getByRole('button', { name: '删除工作流' }).click()
  }
  const { ctx, page, state } = await open()
  await removeCurrent(page)
  await page.waitForFunction(() => window.__studio.getState().workflow?.id !== 'st-main', null, { timeout: 5000 }).catch(() => {})
  await page.waitForTimeout(300)
  const s = await S(page, () => { const x = window.__studio.getState(); return { id: x.workflow?.id, nodes: x.nodes.length, dirty: x.dirty, past: x.past.length } })
  const at = new URL(page.url()).pathname
  check('删掉正开着的那张：换到剩下的一张，画布和地址都不再是它', state.deleted.has('st-main') && !!s.id && s.id !== 'st-main'
    && at === `/studio/${s.id}` && s.nodes > 0, `${s.id} · ${at}`)
  check('换过去的那张是干净的（没有未保存、撤销栈）', !s.dirty && s.past === 0)
  await ctx.close()

  const solo = await open({ only: ['st-main'] })
  await removeCurrent(solo.page)
  await solo.page.waitForFunction(() => window.__studio.getState().workflow == null, null, { timeout: 5000 }).catch(() => {})
  await solo.page.waitForTimeout(300)
  const e = await S(solo.page, () => ({ wf: window.__studio.getState().workflow?.id ?? null, nodes: window.__studio.getState().nodes.length }))
  check('一张不剩：画布卸空，回到「还没有工作流」', e.wf === null && e.nodes === 0
    && await solo.page.getByText('还没有工作流').count() === 1 && new URL(solo.page.url()).pathname === '/studio', JSON.stringify(e))
  check('卸空时不误报「不在了」', await solo.page.getByText('那张工作流不在了').count() === 0)
  await solo.ctx.close()
}

check('整个检查没有弹出原生 confirm / prompt', dialogs === 0, `${dialogs} 次`)
check('检查没有写库（兜底拦下的写请求）', writes.length === 0, writes.slice(0, 4).join('、'))

console.log(failed ? `\n${failed} 项未通过` : '\n全部通过')
await browser.close()
process.exit(failed ? 1 : 0)
