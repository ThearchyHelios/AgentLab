// 编排页编辑体验的检查：撤销重做、问题面板定位、快捷键、三栏收起、调色板落点、
// 分支 default 拦截、?run=&focus= 参数、助手改图（应用 / 只回话 / 失败 / 停止）。
//
// 不写库：要编辑的工作流、它的版本、画布会话、助手的流式输出都用 page.route 在
// 浏览器里伪造；后端只被读（目录、校验、变量分析这些纯计算）。保存走伪造的 PATCH，
// 其余写请求一律拦成 409 并记下来——检查结束时这张单子必须是空的。
//
// 跑之前前端得起着（./scripts/dev.sh），默认连 5273。对别的实例（比如一份沙箱拷贝）跑时
// 带上地址：AGENTLAB_WEB=http://localhost:<前端端口> node scripts/check-studio.mjs
import { readFileSync } from 'node:fs'
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const CHROME = process.env.CHROME_PATH
  ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const MOD = process.platform === 'darwin' ? 'Meta' : 'Control'

// STUDIO_ONLY=助手改图,离开 只跑段名里含这些字的段（逗号分隔），改一处时不用整本跑
const ONLY = (process.env.STUDIO_ONLY ?? '').split(',').filter(Boolean)
// STUDIO_SHOTS=<目录>：几处改过的界面各截一张亮、暗
const SHOTS = process.env.STUDIO_SHOTS ?? ''
/**
 * 一节一节地跑：某一节里等待超时、元素找不到，只记成这一节失败，收掉它开的页面，
 * 接着跑下一节——不让一处卡住把后面几十项一起吞掉
 */
const opened = new Set()
async function section(name, fn) {
  if (ONLY.length && !ONLY.some((k) => name.includes(k))) return
  console.log(`=== ${name} ===`)
  try {
    await fn()
  } catch (e) {
    check(`${name} 中途出错`, false, String(e?.message ?? e).split('\n')[0])
  } finally {
    for (const c of opened) await c.close().catch(() => {})
    opened.clear()
  }
}

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

// 可疑实体、引文、表名字段名的报告（可点击证据三期）：夹具是后端真跑出来的，只用通用名
const fxe = JSON.parse(readFileSync(new URL('../frontend/src/run/__tests__/evidence-entity.json', import.meta.url), 'utf8'))
const EV_GRAPH = {
  nodes: [
    node('start', 'input', 0, 200, '输入', { fields: [{ name: 'week', required: true }] }),
    node('fetch', 'agent', 300, 120, '取数', { prompt: '查 {{ input.week }} 的订单', tools: ['db_query__shop'], assign_to: 'rows' }),
    node('manual', 'retrieve', 300, 320, '查手册', { query: '退款口径', collection: 'ops', assign_to: 'manual' }),
    node('write', 'report', 640, 200, '写周报', { instructions: '写本周周报', numbers: 'strict', claims: 'require_citation' }),
    node('done', 'output', 980, 200, '成果', { fields: [{ name: 'answer', value: '{{ nodes.write.text }}' }] }),
  ],
  edges: [edge('start', 'fetch'), edge('start', 'manual'), edge('fetch', 'write'), edge('manual', 'write'), edge('write', 'done')],
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
  'st-ev': wf('st-ev', { name: '__studio_check_ev__', graph: EV_GRAPH, version: 2 }),
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

/** 记下页面加载时出的岔子（页面报错、模块没取到），等不到 store 时拿来说清卡在哪 */
const loadTrouble = new WeakMap()
function watchLoad(page) {
  const list = []
  loadTrouble.set(page, list)
  const isModule = (url) => /^\/(src|node_modules|@vite|@id|@fs)\//.test(new URL(url).pathname)
  page.on('pageerror', (e) => list.push(`页面报错「${e.message.split('\n')[0]}」`))
  page.on('requestfailed', (r) => { if (isModule(r.url())) list.push(`模块没取到 ${new URL(r.url()).pathname}`) })
  page.on('response', (r) => { if (r.status() >= 400 && isModule(r.url())) list.push(`模块 ${r.status()} ${new URL(r.url()).pathname}`) })
}
/**
 * 等页面把 store 挂到 window 上、装上伪造的工作流。store 是静态 import，load 之前就求值完了，
 * 工作流由这里伪造的接口立刻答，所以等不到不是「慢」，是模块图断了或页面卡死。
 * 只有「模块没取到」（开发服务器那一下没答上）重载一次，并打一行出来；
 * 页面报错、vite 报错层、主线程卡死是真问题，说清卡在哪，不只留一句 Timeout
 */
async function ready(page, fn, what, { arg = null, timeout = 15000, retry = true } = {}) {
  if (await page.waitForFunction(fn, arg, { timeout }).then(() => true, () => false)) return
  const trouble = loadTrouble.get(page) ?? []
  if (retry && trouble.length && trouble.every((t) => t.startsWith('模块'))) {
    console.log(`  · ${what} 没挂出来（${trouble.slice(0, 2).join('；')}），重载一次`)
    trouble.length = 0
    await page.reload()
    return ready(page, fn, what, { arg, timeout, retry: false })
  }
  const probe = page.evaluate(() => {
    const overlay = document.querySelector('vite-error-overlay')
    if (overlay) return `vite 报错层「${(overlay.shadowRoot?.querySelector('.message')?.textContent ?? '').trim().slice(0, 120)}」`
    return `页面停在 ${location.pathname}，#root 下 ${document.getElementById('root')?.childElementCount ?? 0} 个元素`
  }).catch((e) => `读不到页面：${String(e.message).split('\n')[0]}`)
  const hung = new Promise((r) => setTimeout(() => r('页面主线程 3 秒没有响应'), 3000))
  const where = await Promise.race([probe, hung])
  throw new Error(`${what} ${timeout / 1000} 秒没挂出来：${[where, ...trouble.slice(0, 3)].join('；')}`)
}

/**
 * only：目录里只放这几张伪造的（不混真实的），用来造「删完一张不剩」
 * platform：伪装成别的平台（'Windows'），看快捷键提示是不是跟着平台走
 */
async function open({ width = 1440, height = 900, path = '/studio/st-main', prefs = {}, stream, only, platform, init, before } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height } })
  opened.add(ctx)
  // init：[函数, 参数]，在页面脚本之前跑（比如装数渲染次数的钩子）
  if (init) await ctx.addInitScript(init[0], init[1])
  // 按页面自己加载时的地址 import 模块：改过的模块地址带 ?t=，直接写 /src/… 会拿到另一份
  // 实例——另一个 store，而且它一加载就把 window.__studio 换成一个空的
  await ctx.addInitScript(() => {
    performance.setResourceTimingBufferSize(10_000)
    window.__appImport = (path) => import(performance.getEntriesByType('resource').map((e) => e.name)
      .find((n) => { try { return new URL(n).pathname === path } catch { return false } }) ?? path)
  })
  // 每个上下文只在第一次加载时清偏好：刷新之后要看得到上一次存下的收起状态
  await ctx.addInitScript((p) => {
    try {
      if (sessionStorage.getItem('st-init')) return
      sessionStorage.setItem('st-init', '1')
      localStorage.removeItem('agentlab.studio.palette')
      localStorage.removeItem('agentlab.studio.assistant')
      localStorage.removeItem('agentlab.studio.assistant.narrow')
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
  watchLoad(page)
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
    if (method === 'GET' && !sub) return state.deleted.has(id) ? json(route, { detail: '工作流不存在，可能已被删除' }, 404) : json(route, FAKES[id])
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
  // 发布前检查和自动修复是只读的 POST：这里按老后端（没有这两个接口）答，发布弹窗照原样。
  // 这两个接口本身的行为在 check-publish 里查
  await page.route(/\/api\/workflows\/st-[a-z]+\/(publish-check|autofix)$/, (route) => json(route, { detail: 'Not Found' }, 404))
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

  // before：页面第一次加载之前再装几条路由（画布一打开就发的请求，比如报告节点卡去取最近一次运行）
  if (before) await before(page)
  await page.goto(`${WEB}${path}`, { waitUntil: 'networkidle' })
  await ready(page, () => window.__studio?.getState().workflow != null, '画布的 store（window.__studio）')
  await page.waitForTimeout(500)
  return { ctx, page, errors, state }
}

const S = (page, fn, arg) => page.evaluate(fn, arg)

/**
 * 数某个组件重渲染了几次。在 React 之前装一个假的 devtools 钩子：每次提交时找出名字对得上的
 * fiber，它的第一个 hook 对象换了就是重新渲染过（跳过渲染时 memoizedState 原样不动）。
 * 第一次见到的不算——那是挂载
 */
const countRenders = (names) => {
  const seen = new WeakMap()
  window.__renders = Object.fromEntries(names.map((n) => [n, 0]))
  const walk = (f) => {
    for (; f; f = f.sibling) {
      const name = typeof f.type === 'function' ? f.type.name : null
      if (name && name in window.__renders) {
        const prev = seen.has(f) ? seen.get(f) : f.alternate && seen.has(f.alternate) ? seen.get(f.alternate) : undefined
        if (prev !== undefined && prev !== f.memoizedState) window.__renders[name] += 1
        seen.set(f, f.memoizedState)
      }
      walk(f.child)
    }
  }
  window.__REACT_DEVTOOLS_GLOBAL_HOOK__ = {
    supportsFiber: true, isDisabled: false, renderers: new Map(),
    inject: () => 1, checkDCE: () => {}, onCommitFiberUnmount: () => {}, onPostCommitFiberRoot: () => {},
    onScheduleFiberRoot: () => {},
    onCommitFiberRoot: (_id, root) => { try { walk(root.current) } catch { /* 数不到就算了 */ } },
  }
}
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

/**
 * 页面里的 navigate(to, opts)：从 React 树里找到 RouterProvider 手里的那个 data router，
 * 和页面组件调 navigate 走的是同一条路（离开守卫、state 都一样）。给「先 canLeave 再跳」
 * 这类只在别人页面里才有入口的写法用
 */
const routerGo = (page, to, opts) => page.evaluate(([to, opts]) => {
  const find = () => {
    const root = document.getElementById('root')
    const stack = [root[Object.keys(root).find((k) => k.startsWith('__reactContainer$'))]]
    while (stack.length) {
      const f = stack.pop()
      if (!f) continue
      if (typeof f.memoizedProps?.router?.navigate === 'function') return f.memoizedProps.router
      stack.push(f.sibling, f.child)
    }
    return null
  }
  window.__checkRouter ??= find()
  return window.__checkRouter.navigate(to, opts)
}, [to, opts])

/** 数「放弃未保存的改动」一共弹过几次（弹过又关掉的也算）：一条路该只问一遍 */
const watchAsks = (page) => page.evaluate(() => {
  window.__asks = 0
  if (window.__asksOn) return
  window.__asksOn = true
  const seen = new WeakSet()
  new MutationObserver(() => {
    for (const d of document.querySelectorAll('[role="dialog"]')) {
      if (seen.has(d) || !d.textContent.includes('未保存的改动')) continue
      seen.add(d)
      window.__asks += 1
    }
  }).observe(document.body, { childList: true, subtree: true, characterData: true })
})
const asks = (page) => page.evaluate(() => window.__asks)

// ================================================================ 1
await section('工具栏：版本标签、校验结论、问题面板定位', async () => {
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

  await page.locator('[data-problem]').filter({ hasText: '还没有选择工具' }).locator('button').first().click()
  await page.waitForTimeout(400)
  const s = await S(page, () => ({ sel: window.__studio.getState().selectedId, focus: window.__studio.getState().focusRequest?.id }))
  check('点一条问题：选中那个节点', s.sel === 'call', s.sel)
  check('点一条问题：请画布取景到它', s.focus === 'call', s.focus)
  const fieldMsg = await page.locator('[data-field="tool"]').innerText().catch(() => '')
  check('字段级错误落在「工具」输入框下面', fieldMsg.includes('还没有选择工具'), fieldMsg.slice(0, 40))
  const ring = await S(page, () => window.__studio.getState().nodes.filter((n) => n.selected).map((n) => n.id).join(','))
  check('React Flow 的选中和检查器一致', ring === 'call', ring)

  // 图级和无效连线：注入和后端同形的 issues
  await S(page, () => window.__studio.setState((s) => ({ issues: [...s.issues,
    { level: 'error', node_id: null, edge_id: null, message: '找不到起始节点：每个节点都有上游连线，工作流中存在环路且没有起点' },
    { level: 'error', node_id: null, edge_id: 'ghost', message: '连线的终点节点「nope」不存在' }] })))
  await page.waitForTimeout(200)
  check('图级问题有自己的分组', (await page.locator('[role="list"][aria-label="问题"]').innerText()).includes('整个工作流'))
  check('无效连线给出「删除这条无效连线」', await count(page, 'button:has-text("删除这条无效连线")') === 1)

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
  check('下游变量的 warning 落在「用户提示」下面', /之后才执行|取到的将是空值/.test(promptMsg), promptMsg.slice(0, 50))
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
  check('下游的 vars.summary 置灰、写明取不到', /vars\.summary[\s\S]*无法引用/.test(list))
  await page.keyboard.press('Escape')

  // 表达式字段敲 {{ 就地提示
  await S(page, () => window.__studio.getState().select('gate'))
  await page.waitForTimeout(300)
  const cond = page.locator('[data-field="cases"] input').nth(2)
  await cond.click()
  await page.keyboard.press('End')
  await page.keyboard.type(' {{')
  await page.waitForTimeout(150)
  check('表达式里敲 {{ 就地提示不需要', (await page.locator('[data-field="cases"]').innerText()).includes('此处为表达式'))
  check('条件字段标着「表达式」', (await page.locator('[data-field="cases"]').innerText()).includes('表达式'))
  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))
  await ctx.close()
})

// ================================================================ 2
await section('分支 case：default 拦截、重复标识、出口去重', async () => {
  const { ctx, page } = await open()
  await S(page, () => window.__studio.getState().select('gate'))
  await page.waitForTimeout(400)
  const handles = () => count(page, '.react-flow__node[data-id="gate"] .react-flow__handle.source')
  check('key=default 和兜底出口合并成一个（2 个出口）', await handles() === 2, `${await handles()} 个`)
  const text = await page.locator('[data-field="cases"]').innerText()
  check('行内说破 default 是保留标识', text.includes('默认出口的保留标识'))
  check('case 表单每个框都有标签', /标识（连线出口）/.test(text) && /说明/.test(text) && /条件/.test(text))
  await page.locator('[data-field="cases"] button:has-text("改为 team")').click()
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
  check('说明为什么没改', (await page.locator('[data-field="cases"]').innerText()).includes('未能修改为「fast」'))
  check('重复标识不产生同 id 的两个出口', await count(page, '.react-flow__node[data-id="gate"] .react-flow__handle[data-handleid="fast"]') === 1)

  // 禁用 default
  await keyInput.fill('default')
  await page.waitForTimeout(150)
  check('新写 default 当场拦下', (await page.locator('[data-field="cases"]').innerText()).includes('保留标识'))
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
})

// ================================================================ 3
await section('撤销 / 重做、删除回执、复制粘贴、调色板落点', async () => {
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
})

// ================================================================ 4
await section('快捷键与三栏', async () => {
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
})

// ================================================================ 3b
/**
 * 工具栏里看得见的一排控件：谁压着谁、有没有伸出视口。嵌在别的控件里的（芯片里的图标）不单算
 */
const toolbarLayout = (page) => page.evaluate(() => {
  const bar = document.querySelector('.\\@container')
  const vw = document.documentElement.clientWidth
  const all = [...bar.querySelectorAll('button, .chip, [data-toolbar-item]')]
    .filter((el) => el.offsetParent && el.getBoundingClientRect().width > 0)
  const items = all.filter((el) => !all.some((o) => o !== el && o.contains(el)))
  const box = items.map((el) => { const r = el.getBoundingClientRect(); return { n: (el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 10), l: r.left, r: r.right } })
  const overlaps = []
  for (let i = 0; i < box.length; i++) {
    for (let j = i + 1; j < box.length; j++) {
      if (Math.min(box[i].r, box[j].r) - Math.max(box[i].l, box[j].l) > 1) overlaps.push(`${box[i].n}×${box[j].n}`)
    }
  }
  return { sw: bar.scrollWidth, cw: bar.clientWidth, vw, right: Math.round(Math.max(...box.map((b) => b.r))), overlaps, names: box.map((b) => b.n) }
})

await section('窄屏：次要按钮收进「更多」、助手栏默认收起、展开是浮层', async () => {
  const { ctx, page, errors } = await open({ width: 768, height: 860 })
  await waitAnalysis(page)
  // 最挤的时候：有未保存改动、校验同时有错和提示
  await S(page, () => window.__studio.setState({ dirty: true }))
  await page.waitForTimeout(300)
  const bar = await toolbarLayout(page)
  check('768 宽、两枚芯片都在时：工具栏上没有互相压着的控件', bar.overlaps.length === 0, bar.overlaps.join('、'))
  check('768 宽：最右的控件也在视口里、工具栏不溢出', bar.right <= bar.vw && bar.sw <= bar.cw, JSON.stringify({ right: bar.right, vw: bar.vw, sw: bar.sw, cw: bar.cw }))
  check('768 宽：运行和收起助手栏都露在外面', bar.names.includes('运行') && bar.names.some((n) => n.includes('助手栏')), bar.names.join(' '))

  const more = page.getByRole('button', { name: '更多操作' })
  check('窄屏把次要按钮收进「更多操作」', await more.count() === 1 && await more.isVisible())
  await more.click()
  await page.waitForTimeout(200)
  const menu = await page.locator('[role="menu"][aria-label="更多操作"] [role^="menuitem"]').allInnerTexts()
  check('「更多操作」里有助手、变量、版本历史、自动排版', ['助手', '变量', '版本历史', '自动排版'].every((t) => menu.some((m) => m.includes(t))), menu.join(' | '))
  await page.locator('[role="menu"][aria-label="更多操作"] [role^="menuitem"]', { hasText: '变量' }).click()
  await page.waitForTimeout(250)
  check('从「更多操作」打开变量抽屉，菜单随即收起', await count(page, '#dock-variables') === 1 && await count(page, '[role="menu"][aria-label="更多操作"]') === 0)
  await more.click()
  await page.keyboard.press('Escape')
  await page.waitForTimeout(150)
  check('Esc 收起「更多操作」', await count(page, '[role="menu"][aria-label="更多操作"]') === 0)

  // 画布那一栏是助手栏前面的 <main>（外壳自己还有一个 <main>，不能按标签取第一个）
  const widths = () => page.evaluate(() => {
    const aside = document.querySelector('main + aside')
    return {
      main: Math.round(aside.previousElementSibling.getBoundingClientRect().width),
      aside: Math.round(aside.getBoundingClientRect().width),
      over: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    }
  })
  const closed = await widths()
  check('窄于 1280：助手栏默认收起，画布拿到整块宽度', closed.aside === 0 && closed.main >= 640, JSON.stringify(closed))
  await page.getByRole('button', { name: '展开助手栏' }).click()
  await page.waitForTimeout(250)
  const opened = await widths()
  check('窄屏展开助手栏：浮在画布上，不把画布挤窄', opened.aside >= 300 && Math.abs(opened.main - closed.main) <= 1 && opened.over <= 0, JSON.stringify(opened))
  const prefs = await S(page, () => ({ wide: localStorage.getItem('agentlab.studio.assistant'), narrow: localStorage.getItem('agentlab.studio.assistant.narrow') }))
  check('窄屏的开合单独记，不改宽屏的偏好', prefs.narrow === 'open' && prefs.wide === null, JSON.stringify(prefs))
  await S(page, () => window.__studio.getState().select('answer'))
  await page.waitForTimeout(250)
  check('窄屏选中节点：属性面板在浮层里', await count(page, '[data-inspector-sheet] [data-field="prompt"]') === 1)
  await page.getByRole('button', { name: '收起助手栏' }).click()
  await page.waitForTimeout(200)
  check('窄屏收起浮层', (await widths()).aside === 0)

  // 有一次跑完的运行：胶囊 + 发起按钮一起挤在工具栏上
  await S(page, () => {
    const st = window.__studio
    const t = Date.now() / 1000 - 5
    st.setState({ run: { id: 'st-run-narrow', workflow_id: 'st-main', workflow_name: '__studio_check__', status: 'queued', input: {},
      output: {}, error: null, usage: {}, run_class: 'exploratory', version: null } })
    for (const [i, e] of [['run.started', null, {}], ['node.started', 'start', {}], ['node.finished', 'start', { duration_ms: 3 }],
      ['run.finished', null, { output: {}, usage: {}, timing: { wall_ms: 800, active_ms: 800, wait_ms: 0 } }]].entries()) {
      st.getState().applyEvent({ seq: i + 1, type: e[0], node_id: e[1], data: e[2], ts: t + i * 0.2 })
    }
  })
  await page.waitForTimeout(400)
  const ran = await toolbarLayout(page)
  check('768 宽、有运行结果时：胶囊和发起按钮也不压着别的、不伸出视口', ran.overlaps.length === 0 && ran.right <= ran.vw,
    `${ran.overlaps.join('、')} right=${ran.right}`)
  // 最挤的一种：失败的运行（胶囊里多一个「接着跑」）+ 画布不是已发布那一版 + 未保存 + 有错有提示
  await S(page, () => {
    const st = window.__studio
    const t = Date.now() / 1000 - 5
    st.getState().clearRun()
    st.setState({ dirty: true, run: { id: 'st-run-narrow2', workflow_id: 'st-main', workflow_name: '__studio_check__', status: 'queued',
      input: {}, output: {}, error: null, usage: {}, run_class: 'exploratory', version: null } })
    for (const [i, e] of [['run.started', null, {}], ['node.started', 'start', {}],
      ['node.failed', 'start', { error: '检查脚本造的失败', duration_ms: 3 }], ['run.failed', null, { error: '检查脚本造的失败', node_id: 'start' }]].entries()) {
      st.getState().applyEvent({ seq: i + 1, type: e[0], node_id: e[1], data: e[2], ts: t + i * 0.2 })
    }
  })
  await page.waitForTimeout(400)
  const worst = await toolbarLayout(page)
  check('768 宽、失败的运行 + 未保存 + 有错有提示：仍不压、不伸出视口，继续运行露在外面',
    worst.overlaps.length === 0 && worst.right <= worst.vw && worst.names.includes('继续运行'),
    `${worst.overlaps.join('、')} right=${worst.right} ${worst.names.join(' ')}`)
  const ver = await page.locator('span[title*="画布是草稿"]').innerText().catch(() => '')
  check('最窄的工具栏里版本标签只留「草稿 vN」（已发布的版本号在正式运行按钮上）', /草稿 v3/.test(ver) && !/已发布/.test(ver), ver)
  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))
  await ctx.close()

  // 窄屏里点开过的，刷新后还开着；宽屏照旧默认展开
  const again = await open({ width: 1024, height: 760, prefs: { 'agentlab.studio.assistant.narrow': 'open' } })
  check('窄屏记住的「展开」刷新后还在', await again.page.evaluate(() => Math.round(document.querySelector('main + aside').getBoundingClientRect().width)) >= 300)
  await again.ctx.close()
  const fresh = await open({ width: 1024, height: 760 })
  check('1024 宽没记过偏好：助手栏默认收起', await fresh.page.evaluate(() => Math.round(document.querySelector('main + aside').getBoundingClientRect().width)) === 0)
  await fresh.ctx.close()
  const wide = await open({ width: 1440 })
  check('1440 宽：助手栏默认展开、占位不浮', await wide.page.evaluate(() => {
    const a = document.querySelector('main + aside')
    return Math.round(a.getBoundingClientRect().width) >= 300 && getComputedStyle(a).position !== 'absolute'
  }))
  await wide.ctx.close()
})

// ================================================================ 3c
await section('文案：运行被拦时点名节点、人工审批兜底摘要、Skill 空提示、正式运行署名', async () => {
  const { ctx, page, errors } = await open()
  await waitAnalysis(page)
  const runTitle = await page.locator('[data-run-control] button[aria-label="运行"]').getAttribute('title')
  check('运行被拦：提示前面点出是哪个节点', /「查询销量」/.test(runTitle ?? '') && /还没有选择工具/.test(runTitle ?? ''), runTitle)
  check('运行被拦：指向问题面板', /问题面板/.test(runTitle ?? ''), runTitle)

  await S(page, () => {
    const s = window.__studio.getState()
    s.addNode('human', { x: 0, y: 700 })
    const n = window.__studio.getState().nodes.find((x) => x.data.nodeType === 'human')
    s.updateNode(n.id, { config: { ...n.data.config, title: '' } })
  })
  await page.waitForTimeout(300)
  const humanSummary = await page.evaluate(() => {
    const n = window.__studio.getState().nodes.find((x) => x.data.nodeType === 'human')
    return document.querySelector(`.react-flow__node[data-id="${CSS.escape(n.id)}"] .nc-summary`)?.textContent ?? ''
  })
  check('没填标题的人工审批：摘要不写成像状态的「等待人工」', humanSummary === '人工审批 · 未填写标题', humanSummary)
  await S(page, () => { window.__studio.getState().undo(); window.__studio.getState().undo() })

  // Skill 目录是空的：空提示给一条去创建的链接
  await S(page, async () => { const m = await window.__appImport('/src/store/catalog.ts'); m.useCatalog.setState({ skills: [] }) })
  await S(page, () => window.__studio.getState().select('answer'))
  await page.waitForTimeout(400)
  const skills = page.locator('[data-field="skills"]')
  const addSkill = skills.getByRole('button', { name: /添加 Skill/ })
  check('Skill 的多选按钮写「添加 Skill」', await addSkill.count() === 1)
  await addSkill.click()
  await page.waitForTimeout(200)
  const link = skills.locator('a[href="/knowledge/skills"]')
  const linkText = await link.innerText().catch(() => '')
  check('没有 Skill：空提示是一条去「知识 → 方法论 Skill」的链接', await link.count() === 1 && linkText.includes('知识') && linkText.includes('方法论 Skill'), linkText)
  await S(page, () => window.__studio.getState().select(null))

  // 正式运行的弹层：写明以谁的名义发起；没署名用琥珀色说出来，给去设置的路
  const formal = page.locator('[data-run-control]').getByRole('button', { name: /^正式运行 v2$/ })
  await formal.click()
  await page.waitForTimeout(300)
  const signed = await page.locator('[role="dialog"][aria-label="正式运行 v2"]').innerText().catch(() => '')
  check('正式运行弹层写出署名', signed.includes('将以「检查脚本」的名义发起'), signed.replace(/\s+/g, ' ').slice(0, 80))
  await page.keyboard.press('Escape')
  await S(page, async () => { const m = await window.__appImport('/src/lib/actor.ts'); m.setLocalActor(null) })
  await page.waitForTimeout(150)
  await formal.click()
  await page.waitForTimeout(300)
  const pop = page.locator('[role="dialog"][aria-label="正式运行 v2"]')
  const unsigned = await pop.innerText().catch(() => '')
  const warn = await pop.locator('[data-signer]').evaluate((el) => {
    const probe = document.createElement('span')
    probe.style.color = 'var(--st-waiting)'
    document.body.append(probe)
    const want = getComputedStyle(probe).color
    probe.remove()
    return { color: getComputedStyle(el).color, want }
  }).catch(() => null)
  check('没署名：写「未署名」并给去设置的链接', unsigned.includes('未署名') && await pop.locator('a[href="/settings/prefs"]').count() === 1,
    unsigned.replace(/\s+/g, ' ').slice(0, 80))
  check('没署名那一行是琥珀色（等待色）', !!warn && warn.color === warn.want && await pop.locator('[data-signer="unsigned"]').count() === 1, JSON.stringify(warn))
  await page.keyboard.press('Escape')
  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))
  await ctx.close()
})

// ================================================================ 4b
await section('分析失败不静默', async () => {
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
  check('校验接口报错（后端没断）不承诺会自动重试', dockText.includes('点击「重试」') && !dockText.includes('自动重新校验'))
  const kept = await S(page, () => window.__studio.getState().issues)
  await S(page, () => window.__studio.setState({ issues: [] }))
  await page.waitForTimeout(100)
  check('没有旧清单可列时不说「以下为」', (await page.locator('#dock-problems').innerText()).includes('暂时无法获取问题清单')
    && !(await page.locator('#dock-problems').innerText()).includes('以下'))
  await page.evaluate((v) => window.__studio.setState({ issues: v }), kept)
  down = false
  await chip.click()
  await page.waitForFunction(() => window.__studio.getState().analysis === 'ok', null, { timeout: 5000 }).catch(() => {})
  check('点重试恢复', await chip.count() === 0 && await S(page, () => window.__studio.getState().analysis) === 'ok')
  await ctx.close()
})

// ================================================================ 5
await section('版本历史：预览、恢复成一次可撤销的改动', async () => {
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
  check('预览列出恢复后将移除的节点', (await page.locator('[role="dialog"][aria-label="版本历史"]').innerText()).includes('将移除'))
  const before = await st(page)
  await page.locator('button:has-text("恢复 v1 到画布")').click()
  await page.waitForTimeout(300)
  const after = await st(page)
  const note = await S(page, () => window.__studio.getState().pendingNote)
  check('恢复：画布换成 v1', after.nodes.length === V1.nodes.length, `${after.nodes.length} 个节点`)
  check('恢复不调后端 restore（受管不会被悄悄保留）', !writes.some((w) => w.includes('/restore')))
  check('保存时写「恢复到 v1」', note === '恢复到 v1', note)
  await blur(page)
  await page.keyboard.press(`${MOD}+z`)
  await page.waitForTimeout(200)
  check('恢复是一次可撤销的改动', (await st(page)).nodes.length === before.nodes.length)
  await page.keyboard.press(`${MOD}+Shift+z`)
  await page.keyboard.press(`${MOD}+s`)
  await page.waitForTimeout(400)
  check('保存带上版本说明', state.patches[0]?.note === '恢复到 v1', JSON.stringify(state.patches[0] ?? {}).slice(0, 60))
  await ctx.close()
})

// ================================================================ 6
await section('助手改图：一步撤销、回执、只回话、失败退回、停止、再修一次、记忆', async () => {
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
    // 服务端按需求挑了表（copilot_context）：过程里多一行「参考了 N 张表」
    { op: 'context', elapsed_ms: 1500, sources: [
      { source: 'shop', tables: ['orders', 'order_items', 'customers'], selected_by: 'model', total: 48 }] },
    { op: 'plan', summary: '加一步润色' },
    { op: 'update_node', id: 'answer', config: finalGraph.nodes[3].data.config },
    { op: 'add_node', node: { id: 'polish', type: 'llm', label: '润色', config: { prompt: '{{ nodes.answer.text }}' } } },
    { op: 'add_edge', edge: { source: 'answer', target: 'polish' } },
    { op: 'done', explanation: '加了润色' },
    { op: 'check', status: 'passed', repaired: 0 },
    { op: 'final', graph: finalGraph, explanation: '加了润色', layout: { mode: 'keep', placed: ['polish'] },
      issues: [{ level: 'warning', node_id: null, code: 'unknown_node_type', type: 'magic',
        message: '模型使用了不存在的节点类型「magic」，已跳过这一步' }] },
  ]
  const past0 = (await st(page)).past
  const began = await S(page, () => window.__studio.getState().runCopilot('给快速回答后面加一步润色', true))
  await page.waitForFunction(() => window.__studio.getState().copilotTurns.at(-1)?.phase !== 'running')
  const t1 = await S(page, () => window.__studio.getState().copilotTurns.at(-1))
  check('开始了就告诉输入框（runCopilot 返回 true）', began === true, String(began))
  check('收到 final：这一轮是「已应用」', t1.outcome === 'applied', t1.outcome)
  const ctxRow = page.locator('[data-step-code="copilot_context"]').last()
  check('助手栏的过程里一行说参考了几张表', await ctxRow.waitFor({ timeout: 3000 }).then(() => true, () => false)
    && (await ctxRow.innerText()).includes('参考了 3 张表'))
  await ctxRow.locator('button').first().click()
  check('点开看到按数据源分组的表名', await ctxRow.getByText('「shop」按需求从 48 张表中挑出 3 张：orders、order_items、customers')
    .waitFor({ timeout: 3000 }).then(() => true, () => false))
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
    { op: 'error', message: '助手本轮未完成：等待超时：服务方未在限定时间内响应' },
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
  const bar = page.locator('div:has(> [role="status"]:has-text("助手正在修改工作流"))')
  check('生成中画布上方有进度条和停止', await bar.locator('button:has-text("停止")').count() === 1)
  const spoken = await page.locator('[role="status"]:has-text("助手正在修改工作流")').innerText()
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
    { op: 'check', status: 'failed', issues: ['「查询销量」：「调用工具」节点还没有选择工具'] },
    { op: 'final', graph: GRAPH, explanation: '', layout: { mode: 'keep', placed: [] }, issues: [] },
  ]
  await S(page, () => window.__studio.getState().runCopilot('检查一下', true))
  await page.waitForFunction(() => window.__studio.getState().copilotTurns.at(-1)?.phase !== 'running')
  state.stream = [{ op: 'model', model: 'fake-model' }, { op: 'reply', text: '好' }]
  const turnId = await S(page, () => window.__studio.getState().copilotTurns.at(-1).id)
  await S(page, (id) => window.__studio.getState().repairWithCopilot(id), turnId)
  await page.waitForTimeout(400)
  const lastBody = state.bodies.at(-1)
  check('再修一次：指令里带着剩下的问题', lastBody?.instruction?.includes('还没有选择工具') && !!lastBody?.base_graph,
    (lastBody?.instruction ?? '').slice(0, 40))

  // 6) 开始新对话：换会话，记忆清零
  await S(page, () => window.__studio.getState().newCopilotConversation())
  await page.waitForTimeout(200)
  const m2 = await S(page, () => ({ id: window.__studio.getState().copilotConversationId, turns: window.__studio.getState().copilotMemory.turns }))
  check('开始新对话：换一条会话，模型上下文清零', m2.id === 'conv-new' && m2.turns === 0, JSON.stringify(m2))
  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))
  await ctx.close()
})

// ================================================================ 7
await section('入口：?run=&focus=、选择器、发布弹窗', async () => {
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
  check('模板行常驻「以此模板新建」', await count(page, 'li:has-text("__studio_check_tpl__") button:has-text("以此模板新建")') === 1)
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
  // 只看发布弹窗里的：助手输入框的「在现有工作流上改 | 从头生成」也是一组 radio
  const radio = await gov.page.locator('[role="dialog"] [role="radio"][aria-checked="true"]').innerText()
  check('受管工作流的发布默认选「受管」', radio.includes('受管'), radio.slice(0, 20))
  check('选项用统一术语（已发布 — 可发起正式运行）', await gov.page.getByText('已发布 — 可发起正式运行').count() === 1)
  check('写明以谁的名义发布', await gov.page.getByText('检查脚本').count() >= 1)
  await gov.page.locator('[role="dialog"] [role="radio"]:has-text("已发布")').click()
  check('从受管降级要说出来', await gov.page.getByText(/将降级/).count() === 1)
  await gov.ctx.close()
})

// ================================================================ 6b
await section('助手改节点是合并，不是整体替换（NI-1）；出错留下「怎么办」', async () => {
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
    { op: 'error', message: '助手本轮未完成：等待超时：服务方未在限定时间内响应', hint: '服务方可能繁忙或网络不稳定，请稍后重试；持续超时请检查地址和网络', detail: 'ReadTimeout: 120s' },
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
  check('出错时保留处理建议和技术细节（Composer 的错误条要用）',
    cp.error.includes('等待超时') && cp.errorHint === '服务方可能繁忙或网络不稳定，请稍后重试；持续超时请检查地址和网络' && cp.errorDetail === 'ReadTimeout: 120s',
    JSON.stringify({ hint: cp.errorHint, detail: cp.errorDetail }))
  const t0 = await S(page, () => window.__studio.getState().copilotTurns.at(-1))
  check('这一轮记下开始时刻（助手流头部的实时计时用）', typeof t0.startedAt === 'number'
    && Math.abs(Date.now() - t0.startedAt) < 60_000, String(t0.startedAt))
  // 「新对话」之后面板回到空的输入框：上一段对话的报错不能留在那儿冒充这一段的
  await S(page, () => window.__studio.getState().newCopilotConversation())
  await page.waitForTimeout(250)
  const fresh = await S(page, () => {
    const c = window.__studio.getState().copilot
    return { error: c.error, hint: c.errorHint, detail: c.errorDetail }
  })
  check('新对话清掉上一段的报错、怎么办和技术细节', !fresh.error && !fresh.hint && !fresh.detail, JSON.stringify(fresh))
  check('新对话的空面板上没有旧的错误条', await count(page, '[data-copilot-error]') === 0)

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
    warn('fetch', 'tools', '「数据查询」的工具从 db_query__shop、db_schema__shop 变为无。本轮要求中没有提到移除工具，请确认是否误删：没有绑定工具时，它无法查询数据库，只能假设调用结果'),
    warn('team', 'agents[0].tools', '「复核团队」的成员「fetcher」的工具从 db_query__shop 变为无。本轮要求中没有提到移除工具，请确认是否误删：没有绑定工具时，它无法查询数据库，只能假设调用结果'),
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
  const toastBox = page.locator('[role="status"] > div').filter({ hasText: '以下工具绑定被移除' })
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

  // 一轮里既有让删的、也有没让删的：只有后端点了名（tools_dropped，同节点同字段）的那条
  // 算「没让删」。以前整轮只要有一条提醒，所有变少的都列在「没让删」底下，按要求删的
  // 那条也被说成改漏了，真正要紧的那句反而被冲淡
  const mixed = {
    ...TEAM_BOUND,
    nodes: TEAM_BOUND.nodes.map((n) => {
      if (n.id === 'fetch') return { ...n, data: { ...n.data, config: { ...n.data.config, tools: [] } } }
      if (n.id === 'team') {
        return { ...n, data: { ...n.data, config: { ...n.data.config,
          agents: n.data.config.agents.map((a) => (a.name === 'fetcher' ? { ...a, tools: [] } : a)) } } }
      }
      return n
    }),
  }
  const onlyMember = [warnings[1]]
  state.stream = [
    { op: 'model', model: 'fake-model' },
    { op: 'update_node', id: 'fetch', config: { tools: [] } },
    { op: 'update_node', id: 'team', config: { agents: [{ name: 'fetcher', tools: [] }] } },
    { op: 'check', status: 'passed', repaired: 0, warnings: onlyMember },
    { op: 'final', graph: mixed, explanation: '', layout: { mode: 'keep', placed: [] }, issues: onlyMember, tool_changes: changes },
  ]
  await S(page, () => window.__studio.getState().runCopilot('把「数据查询」的查库工具去掉', true))
  await page.waitForFunction(() => window.__studio.getState().copilotTurns.at(-1)?.phase !== 'running')
  const mixedToast = page.locator('[role="status"] > div').filter({ hasText: '已按要求移除' })
  const mixedText = await mixedToast.first().innerText({ timeout: 2000 }).catch(() => '')
  const lines = mixedText.split('\n').map((l) => l.trim()).filter(Boolean)
  const unaskedAt = lines.findIndex((l) => l.includes('本轮并未要求删除'))
  const askedAt = lines.findIndex((l) => l.includes('已按要求移除以下工具绑定'))
  const lineOf = (needle) => lines.findIndex((l) => l.includes(needle))
  check('回执分两组：没让删的列在「确认一下」底下，按要求删的另列', unaskedAt >= 0 && askedAt > unaskedAt
    && lineOf('的成员「fetcher」') > unaskedAt && lineOf('的成员「fetcher」') < askedAt
    && lineOf('「数据查询」：') > askedAt, lines.join(' / ').slice(0, 160))

  // 回执怎么写是个纯函数：分组、色调、常驻都在这儿定
  const receipt = await page.evaluate(async ({ changes, warnings }) => {
    const m = await window.__appImport('/src/canvas/copilotMerge.ts')
    const base = { total: 2, left: 0, missing: 0, toolChanges: changes }
    const noField = [{ ...warnings[1], field: undefined }]
    return {
      mixed: m.copilotReceipt({ ...base, toolWarnings: [warnings[1]] }),
      asked: m.copilotReceipt({ ...base, toolWarnings: [] }),
      unasked: m.copilotReceipt({ ...base, toolWarnings: warnings }),
      // 老后端的提醒不带 field：按节点认
      legacy: m.copilotReceipt({ ...base, toolWarnings: noField }),
    }
  }, { changes, warnings }).catch((e) => {
    const none = { text: '', kind: '', sticky: null, error: e.message.split('\n')[0] }
    return { mixed: none, asked: none, unasked: none, legacy: none }
  })
  check('有没让删的：warn 色、常驻到人看过', receipt.mixed.kind === 'warn' && receipt.mixed.sticky === true
    && receipt.unasked.sticky === true && !receipt.unasked.text.includes('已按要求移除'), JSON.stringify(receipt.unasked).slice(0, 120))
  check('全是按要求删的：不常驻、不标 warn，也不说「改漏了」', receipt.asked.sticky === false && receipt.asked.kind === 'ok'
    && receipt.asked.text.includes('已按要求移除') && !receipt.asked.text.includes('本轮并未要求删除'), receipt.asked.text.replace(/\n/g, ' / '))
  check('提醒不带 field 时按节点认', receipt.legacy.sticky && receipt.legacy.text.indexOf('fetcher')
    < receipt.legacy.text.indexOf('已按要求移除'), receipt.legacy.text.replace(/\n/g, ' / '))

  // 自查的问题：后端现在给对象 {level, node_id, edge_id, message, field, code}。拼好的一行字
  // 给「再修一次」用，原样的对象留着给「定位」落到检查器里那一栏（循环的条件、成员的工具）
  state.stream = [
    { op: 'model', model: 'fake-model' },
    { op: 'check', status: 'repairing', round: 1, issues: [
      { level: 'error', node_id: 'team', edge_id: null, field: 'agents[1].tools', message: '成员「writer」的提示词要求用「python_exec」，但没有给这个成员绑定它' },
      '「查询销量」：「调用工具」节点还没有选择工具',
    ] },
    { op: 'check', status: 'failed', issues: [
      { level: 'error', node_id: 'fetch', edge_id: null, field: 'prompt', code: 'datasource_out_of_scope', message: '使用了限定范围之外的数据源 archive_db：本轮只允许使用 shop' },
    ] },
    { op: 'final', graph: TEAM_BOUND, explanation: '', layout: { mode: 'keep', placed: [] }, issues: [] },
  ]
  await S(page, () => window.__studio.getState().runCopilot('按门店拆一下', true))
  await page.waitForFunction(() => window.__studio.getState().copilotTurns.at(-1)?.phase !== 'running')
  const ck = await S(page, () => window.__studio.getState().copilotTurns.at(-1).check)
  check('自查问题留着拼好的一行字（带节点名）', ck?.issues?.[0]?.startsWith('「数据查询」：使用了限定范围之外的数据源'), JSON.stringify(ck?.issues))
  check('也留着原样的 {node_id, field, code}', ck?.items?.length === 1 && ck.items[0].node_id === 'fetch'
    && ck.items[0].field === 'prompt' && ck.items[0].code === 'datasource_out_of_scope' && ck.items[0].level === 'error',
    JSON.stringify(ck?.items))

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
})

// ================================================================ 6c
await section('正式运行期间画布只读：调色板、粘贴、复制、排版、检查器、撤销都拦下', async () => {
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

  // 被拦下的添加不能安静地什么都不做：鼠标点、键盘回车、搜索框回车都要说为什么。
  // 以前整块 pointer-events:none，aria-disabled 挂在外层 div 上读屏听不到，卡片照样能
  // Tab 到、回车没反应也不说原因
  const lockToast = page.locator('[role="status"] > div').filter({ hasText: '正式运行进行中' })
  const clearToasts = async () => {
    for (const b of await page.getByRole('button', { name: '关闭提示' }).all()) await b.click({ timeout: 500 }).catch(() => {})
    await page.waitForTimeout(100)
  }
  const cards = page.locator('[aria-label="节点库"] button[data-node-type]')
  const cardInfo = await cards.evaluateAll((els) => els.map((el) => ({
    dis: el.getAttribute('aria-disabled'), desc: document.getElementById(el.getAttribute('aria-describedby') ?? '')?.textContent ?? '',
  })))
  check('每张节点卡都标 aria-disabled，并指向说明原因的那句话', cardInfo.length > 5
    && cardInfo.every((c) => c.dis === 'true' && c.desc.includes('只读')), JSON.stringify(cardInfo[0]))
  await clearToasts()
  // aria-disabled 在 playwright 眼里是「不可点」，真人照样点得到：force 跳过它的可操作性检查
  await cards.filter({ hasText: '模型调用' }).first().click({ force: true, timeout: 2000 }).catch(() => {})
  await page.waitForTimeout(200)
  check('鼠标点被拦下，并说为什么', (await st(page)).nodes.length === s0.nodes.length && await lockToast.count() === 1)
  await clearToasts()
  await cards.first().focus({ timeout: 2000 }).catch(() => {})
  await page.keyboard.press('Enter')
  await page.waitForTimeout(200)
  check('键盘回车被拦下，并说为什么', (await st(page)).nodes.length === s0.nodes.length && await lockToast.count() === 1)
  await clearToasts()
  await page.locator('#studio-palette-search').fill('模型')
  await page.locator('#studio-palette-search').press('Enter')
  await page.waitForTimeout(200)
  check('搜索框回车被拦下，并说为什么', (await st(page)).nodes.length === s0.nodes.length && await lockToast.count() === 1)
  await page.locator('#studio-palette-search').fill('')
  await clearToasts()
  await S(page, () => window.__studio.getState().addNode('llm'))
  check('store 这一层也拦着（直接调 addNode 不生效）', (await st(page)).nodes.length === s0.nodes.length)
  // 收起的图标轨：悬停 / 聚焦的说明换成锁的原因，不再劝人「点击添加到视图中央」
  await page.getByRole('button', { name: '收起节点库' }).click({ timeout: 2000 }).catch(() => {})
  await page.waitForTimeout(200)
  const railBtn = page.locator('[aria-label="节点库（已收起）"] button[data-node-type]').first()
  await railBtn.focus({ timeout: 2000 }).catch(() => {})
  await page.waitForTimeout(150)
  const railTip = await page.locator('[role="tooltip"]').innerText().catch(() => '')
  check('图标轨聚焦时说明锁的原因', railTip.includes('只读') && !railTip.includes('添加到视图中央')
    && await railBtn.getAttribute('aria-disabled') === 'true', railTip.replace(/\s+/g, ' ').slice(0, 60))
  await page.keyboard.press('Enter')
  await page.waitForTimeout(200)
  check('图标轨回车被拦下，并说为什么', (await st(page)).nodes.length === s0.nodes.length && await lockToast.count() === 1)
  await page.getByRole('button', { name: '展开节点库' }).click({ timeout: 2000 }).catch(() => {})
  await clearToasts()

  await page.evaluate(() => document.activeElement instanceof HTMLElement && document.activeElement.blur())
  await page.keyboard.press(`${MOD}+v`)
  await page.keyboard.press(`${MOD}+d`)
  await page.keyboard.press('Shift+KeyL')
  await page.keyboard.press(`${MOD}+z`)
  await page.waitForTimeout(400)
  const s1 = await st(page)
  check('⌘V 粘贴、⌘D 复制、⇧L 排版、⌘Z 撤销都不改图', s1.nodes.length === s0.nodes.length && s1.past === s0.past
    && layoutCalls.length === 0, `${s1.nodes.length}/${s0.nodes.length} 节点 · 排版请求 ${layoutCalls.length}`)
  // 数的是提示条，不是调色板底下那句常驻说明（它也写着这几个字）
  check('按了改图快捷键会说为什么不行', await lockToast.count() === 1)
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
    && await page.getByText('只读', { exact: true }).count() === 1)
  await S(page, () => window.__studio.getState().updateNode('fetch', { label: '改不动' }))
  check('store 这一层也拦着（直接调 updateNode 不生效）',
    (await S(page, () => window.__studio.getState().nodes.find((n) => n.id === 'fetch').data.label)) === '数据查询')
  const prompt0 = await S(page, () => window.__studio.getState().copilotTurns.length)
  // 返回 false：输入框据此留着用户刚写的那句，不能拦下了还把它清掉
  const started = await S(page, () => window.__studio.getState().runCopilot('加一步', true))
  check('正式运行期间不让助手改图（并告诉输入框没开始）', started === false
    && (await S(page, () => window.__studio.getState().copilotTurns.length)) === prompt0
    && !(await S(page, () => window.__studio.getState().copilot.active)), String(started))
  // 360px 的助手栏里，锁的原因要读得全：以前截成「正式运行进行中，画…」，全文只在禁用的发送键的
  // title 上，好多浏览器对禁用元素不显示 title（3C REQ-26）
  await S(page, () => window.__studio.getState().select(null))
  // 运行一开始右栏切到运行层（那一层没有输入框），回到对话层看输入框
  await page.locator('[data-assistant-panel] button[title="回到和助手的对话"]').click({ timeout: 2000 }).catch(() => {})
  await page.waitForTimeout(250)
  const lockHint = await page.evaluate(() => {
    const el = document.querySelector('[data-assistant-panel] [data-prompt-blocked]')
    return el ? { text: el.textContent ?? '', cut: el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1,
                  size: parseFloat(getComputedStyle(el).fontSize), title: el.getAttribute('title') } : null
  })
  check('输入框下写全锁的原因（换行不截断，字不小于 11px）', !!lockHint && lockHint.text.includes('请在运行结束后编辑') && !lockHint.cut
    && lockHint.size >= 11, JSON.stringify(lockHint))
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await page.waitForTimeout(200)
      await page.locator('[data-assistant-panel]').screenshot({ path: `${SHOTS}/studio-formal-lock-hint-${theme}.png` }).catch(() => {})
    }
  }

  // 跑完就能改了
  await formal('succeeded')
  await page.waitForTimeout(150)
  await S(page, () => window.__studio.getState().addNode('llm'))
  check('正式运行结束后恢复编辑', (await st(page)).nodes.length === s0.nodes.length + 1)
  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))

  // Shift+1 的名字要和它做的事一致：它回到打开时的取景（从入口看起），不是「看全所有节点」
  const fit = await page.evaluate(async () => (await import('/src/canvas/shortcuts.ts')).studioShortcut('fit').label)
  check('Shift+1 叫「重置视图」', fit.startsWith('重置视图') && !/看全|适应画布/.test(fit), fit)
  await ctx.close()
})

// ================================================================ 6d
await section('提示词点名的工具没绑定：问题面板定位到工具那一栏，一键绑定（NI-2）；团队用完轮数（NI-5）', async () => {
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
})

// ================================================================ 6e
await section('定位落到输入框：自查问题的 {node_id, field} 直接聚焦检查器里那一栏', async () => {
  const { ctx, page, errors } = await open()
  // 分支第 2 个 case 的条件：检查器打开、滚到那一项、光标落进条件输入框
  const r = await page.evaluate(async () => {
    const m = await window.__appImport('/src/canvas/InspectorSheet.tsx')
    m.revealField('gate', 'cases[1].condition', { focus: true })
    await new Promise((res) => setTimeout(res, 600))
    const a = document.activeElement
    return { sel: window.__studio.getState().selectedId, focus: window.__studio.getState().focusRequest?.id,
      inField: !!a?.closest('[data-field="cases"] [data-item="1"] [data-sub="condition"]'),
      tag: a?.tagName, value: (a && 'value' in a) ? a.value : null }
  }).catch((e) => ({ error: e.message.split('\n')[0] }))
  check('选中节点、画布取景过去', r.sel === 'gate' && r.focus === 'gate', JSON.stringify(r))
  check('光标落在第 2 个分支的条件输入框里', r.inField, `${r.tag} · ${r.value}`)
  // 提示词：大编辑框本身就是能填的那个
  await S(page, () => window.__studio.getState().select(null))
  await page.waitForTimeout(200)
  const t = await page.evaluate(async () => {
    const m = await window.__appImport('/src/canvas/InspectorSheet.tsx')
    m.revealField('answer', 'prompt', { focus: true })
    await new Promise((res) => setTimeout(res, 600))
    return { sel: window.__studio.getState().selectedId, inField: !!document.activeElement?.closest('[data-field="prompt"]') }
  }).catch((e) => ({ error: e.message.split('\n')[0] }))
  check('提示词那一栏：光标落进提示词编辑框', t.sel === 'answer' && t.inField, JSON.stringify(t))
  // 字段认不出来（老后端没给 field）：只选中、取景，不乱聚焦
  const u = await page.evaluate(async () => {
    const m = await window.__appImport('/src/canvas/InspectorSheet.tsx')
    document.body.focus()
    m.revealField('lookup', null, { focus: true })
    await new Promise((res) => setTimeout(res, 400))
    return { sel: window.__studio.getState().selectedId, active: document.activeElement?.tagName }
  }).catch((e) => ({ error: e.message.split('\n')[0] }))
  check('没有 field：只选中节点，不抢焦点', u.sel === 'lookup' && u.active === 'BODY', JSON.stringify(u))
  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))
  await ctx.close()

  // 工具那一栏（agent 的 tools、成员的 agents[i].tools）：前面是一排已绑工具的芯片，每个带
  // 「移除」。从「工具被去掉了」点定位过来是要加回去的——光标得落在「添加」上；落在 × 上，
  // 顺手一个回车又解绑一个
  const team = await open({ path: '/studio/st-team' })
  const toolsOf = (node, member) => S(team.page, ([n, m]) => {
    const c = window.__studio.getState().nodes.find((x) => x.id === n)?.data.config
    return JSON.stringify(m == null ? c?.tools : c?.agents?.[m]?.tools)
  }, [node, member])
  for (const [node, field, member, within] of [
    ['fetch', 'tools', null, '[data-field="tools"]'],
    ['team', 'agents[0].tools', 0, '[data-field="agents"] [data-item="0"] [data-sub="tools"]'],
  ]) {
    await S(team.page, () => window.__studio.getState().select(null))
    await team.page.waitForTimeout(200)
    const before = await toolsOf(node, member)
    const f = await team.page.evaluate(async ([node, field, within]) => {
      const m = await window.__appImport('/src/canvas/InspectorSheet.tsx')
      m.revealField(node, field, { focus: true })
      await new Promise((res) => setTimeout(res, 600))
      const a = document.activeElement
      return { chips: document.querySelectorAll(`[data-inspector-sheet] ${within} button[aria-label^="移除"]`).length,
        inField: !!a?.closest(within), label: a?.getAttribute('aria-label') ?? '', text: a?.textContent?.trim() ?? '',
        add: !!a?.hasAttribute('data-reveal-focus') }
    }, [node, field, within]).catch((e) => ({ error: e.message.split('\n')[0] }))
    check(`${field}：有已绑工具的芯片（这条检查才有意义）`, f.chips > 0, JSON.stringify(f))
    check(`${field}：光标落在「添加工具」上，不落在「移除」上`, f.inField && f.text === '添加工具' && !f.label.startsWith('移除'), JSON.stringify(f))
    await team.page.keyboard.press('Enter')
    await team.page.waitForTimeout(200)
    const after = await toolsOf(node, member)
    check(`${field}：顺手按回车，绑定的工具一个不少`, before === after && before !== undefined, `${before} → ${after}`)
  }
  check('工具那一栏：没有运行时报错', team.errors.length === 0, team.errors.slice(0, 2).join(' | '))
  await team.ctx.close()
})

// ================================================================ 6e2
await section('检查器：目录刷新、数据源取回来，不带着每个字段重渲染', async () => {
  const { ctx, page, errors } = await open({ path: '/studio/st-team', init: [countRenders, ['FieldInput', 'ToolsInput']] })
  const hooked = await page.evaluate(() => typeof window.__renders?.FieldInput === 'number')
  const select = async (id) => {
    await S(page, () => window.__studio.getState().select(null))
    await page.waitForTimeout(200)
    await S(page, (x) => window.__studio.getState().select(x), id)
    await page.waitForTimeout(900)
  }
  // 第一次打开时运行默认值、数据源还在路上；看第二次（缓存都有了）挂上之后还重渲染几次
  await select('fetch')
  await S(page, () => { window.__renders.FieldInput = 0; window.__renders.ToolsInput = 0 })
  await select('fetch')
  const mount = await page.evaluate(() => ({ ...window.__renders }))
  const fields = await count(page, '[data-inspector-sheet] [data-field]')
  check('数渲染的钩子装上了', hooked && fields > 4, `${fields} 个字段`)
  check('打开检查器：挂上之后字段不再各自重渲染一遍（以前每个字段都等数据源回来再画一次）',
    mount.FieldInput === 0, JSON.stringify(mount))
  // 连接心跳、待审批轮询都会写 catalog：以前每个字段订阅整个 catalog，每跳一次全体重画
  await S(page, () => { window.__renders.FieldInput = 0; window.__renders.ToolsInput = 0 })
  await page.evaluate(async () => {
    const { useCatalog } = await window.__appImport('/src/store/catalog.ts')
    for (let i = 0; i < 3; i++) {
      useCatalog.setState({ latencyMs: 40 + i, lastOkAt: Date.now() })
      await new Promise((r) => setTimeout(r, 60))
    }
  })
  await page.waitForTimeout(200)
  const beat = await page.evaluate(() => ({ ...window.__renders }))
  check('连接心跳写 catalog：字段不重渲染', beat.FieldInput === 0, JSON.stringify(beat))
  // 数据源目录换了一份：只有工具那一栏跟着变
  await S(page, () => { window.__renders.FieldInput = 0; window.__renders.ToolsInput = 0 })
  await page.evaluate(async () => {
    const { useCatalog } = await window.__appImport('/src/store/catalog.ts')
    useCatalog.setState((s) => ({ datasources: [...s.datasources] }))
  })
  await page.waitForTimeout(200)
  const src = await page.evaluate(() => ({ ...window.__renders }))
  check('数据源目录刷新：只有工具那一栏重渲染', src.FieldInput === 0 && src.ToolsInput >= 1, JSON.stringify(src))
  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))
  await ctx.close()
})

// ================================================================ 6f
await section('自动排版：请求在路上时画布锁了或改了，不套用也不报「已重新排版」', async () => {
  const { ctx, page, errors } = await open()
  // 排版接口晚 700ms 回：这段时间里正式运行开始了，或者人又动了画布
  await page.route(/\/api\/copilot\/layout$/, async (route) => {
    await new Promise((res) => setTimeout(res, 700))
    return route.continue()
  })
  const okToast = page.locator('[role="status"] > div').filter({ hasText: '已重新排版' })
  const pos = () => S(page, () => JSON.stringify(window.__studio.getState().nodes.map((n) => [n.id, n.position.x, n.position.y])))
  const layoutBtn = page.getByRole('button', { name: '自动排版' })
  // 等排版接口真的回来再看结果：固定等 1100ms 在四道并行、后端忙的时候不够（接口本身就压了 700ms）
  const laidOut = () => page.waitForResponse((r) => new URL(r.url()).pathname.endsWith('/api/copilot/layout'),
    { timeout: 15000 }).catch(() => null)
  const settle = async (done) => { await done; await page.waitForTimeout(250) }

  const p0 = await pos()
  const past0 = (await st(page)).past
  let done = laidOut()
  await layoutBtn.click()
  await page.waitForTimeout(150)
  await page.evaluate(() => window.__studio.setState((s) => ({
    run: { id: 'st-formal-0002', workflow_id: 'st-main', status: 'running', run_class: 'formal', version: 2,
      input: {}, output: {}, error: '', usage: {}, created_at: '2026-09-26T02:00:00Z' },
    runPhase: 'running', trace: { ...s.trace, phase: 'running', runClass: 'formal' },
  })))
  await settle(done)
  check('正式运行半路开始：排版不套用', await pos() === p0 && (await st(page)).past === past0)
  check('也不报「已重新排版」（撤销按钮会撤掉别的改动）', await okToast.count() === 0)
  check('说清为什么没排', await page.locator('[role="status"] > div').filter({ hasText: '正式运行进行中' }).count() === 1)

  await page.evaluate(() => window.__studio.setState((s) => ({
    run: { ...s.run, status: 'succeeded' }, runPhase: 'succeeded', trace: { ...s.trace, phase: 'succeeded' },
  })))
  await page.waitForTimeout(150)
  done = laidOut()
  await layoutBtn.click()
  await page.waitForTimeout(150)
  await S(page, () => window.__studio.getState().updateNode('lookup', { label: '背景检索（排版时改的）' }))
  await settle(done)
  const kept = await S(page, () => window.__studio.getState().nodes.find((n) => n.id === 'lookup')?.data.label)
  check('排版期间又改了画布：不拿旧图的排版盖掉刚才的改动', kept === '背景检索（排版时改的）', kept)
  check('说清这次排版没套用', await okToast.count() === 0
    && await page.locator('[role="status"] > div').filter({ hasText: '排版期间画布已被修改' }).count() === 1)

  done = laidOut()
  await layoutBtn.click()
  await settle(done)
  check('正常情况照样排版、给撤销', await okToast.count() === 1 && await okToast.locator('button:has-text("撤销")').count() === 1)
  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))
  await ctx.close()
})

// ================================================================ 6g
await section('有未保存改动时换工作流：换页之前问，只问一次', async () => {
  const { ctx, page, errors } = await open({ path: '/studio/st-gov' })
  const ask = page.getByRole('dialog').filter({ hasText: '未保存的改动' })
  const here = () => new URL(page.url()).pathname
  const wfId = () => S(page, () => window.__studio.getState().workflow?.id)
  const pick = async (name) => {
    await page.locator('button[title^="切换、新建"]').click()
    await page.waitForTimeout(250)
    await page.locator('li[role="option"]', { has: page.locator('span.truncate', { hasText: new RegExp(`^${name}$`) }) }).click()
  }
  // 从选择器换到 st-main（干净的，不问），好让浏览器后退能退回 st-gov
  await pick('__studio_check__')
  await page.waitForFunction(() => window.__studio.getState().workflow?.id === 'st-main', null, { timeout: 5000 }).catch(() => {})
  check('画布干净时换图不问', await ask.count() === 0 && here() === '/studio/st-main')
  check('干净时不登记离开守卫', await page.evaluate(() => window.__leave.count()) === 0)
  await S(page, () => window.__studio.getState().updateNode('lookup', { label: '背景检索（没存）' }))
  await page.waitForTimeout(150)
  check('有未保存改动：登记离开守卫（关页、刷新也会问）', await page.evaluate(() => window.__leave.count()) >= 1)

  // 浏览器后退：地址还没跳就问。以前是地址先跳到 st-gov、再问，取消时再 replace 回来
  await page.evaluate(() => history.back())
  await ask.waitFor({ timeout: 3000 }).catch(() => {})
  check('后退先问', await ask.count() === 1)
  check('问的时候地址和画布都还是原来那张', here() === '/studio/st-main' && await wfId() === 'st-main', here())
  await ask.getByRole('button', { name: '取消' }).click()
  await page.waitForTimeout(300)
  const stay = await S(page, () => ({ id: window.__studio.getState().workflow?.id, dirty: window.__studio.getState().dirty,
    label: window.__studio.getState().nodes.find((n) => n.id === 'lookup')?.data.label }))
  check('选取消：留在原地，改动还在', here() === '/studio/st-main' && stay.id === 'st-main' && stay.dirty
    && stay.label === '背景检索（没存）', JSON.stringify(stay))

  await page.evaluate(() => history.back())
  await ask.waitFor({ timeout: 3000 }).catch(() => {})
  await ask.getByRole('button', { name: '放弃并切换' }).click({ timeout: 2000 }).catch(() => {})
  await page.waitForFunction(() => window.__studio.getState().workflow?.id === 'st-gov', null, { timeout: 5000 }).catch(() => {})
  await page.waitForTimeout(600)
  check('选放弃：换过去，只问了这一次', here() === '/studio/st-gov' && await wfId() === 'st-gov' && await ask.count() === 0,
    `${here()} · ${await wfId()} · 还开着 ${await ask.count()} 个`)

  // 去别的页不拦：画布还在 store 里，回来还是它
  await S(page, () => window.__studio.getState().updateNode('lookup', { label: '背景检索（又没存）' }))
  await page.waitForTimeout(150)
  await page.locator('nav[aria-label="主导航"] a[href="/runs"]').click()
  await page.waitForTimeout(500)
  check('去别的页不问', await ask.count() === 0 && here() === '/runs', here())
  // 从别的页打开另一张工作流（⌘K）：同样先问，取消就留在记录页，地址不先跳
  await page.keyboard.press(`${MOD}+k`)
  await page.waitForTimeout(300)
  await page.keyboard.type('__studio_check_team__')
  await page.waitForTimeout(300)
  await page.keyboard.press('Enter')
  await ask.waitFor({ timeout: 3000 }).catch(() => {})
  check('从别的页打开另一张：跳之前先问', await ask.count() === 1 && here() === '/runs', here())
  await ask.getByRole('button', { name: '取消' }).click({ timeout: 2000 }).catch(() => {})
  await page.waitForTimeout(300)
  check('取消：留在记录页，画布上的改动还在', here() === '/runs'
    && await S(page, () => window.__studio.getState().dirty && window.__studio.getState().workflow?.id === 'st-gov'))

  // 选择器自己问过（放弃并切换）：换过去时不再问第二遍
  await page.evaluate(() => history.back())
  await page.waitForFunction(() => location.pathname === '/studio/st-gov', null, { timeout: 5000 }).catch(() => {})
  await page.waitForTimeout(400)
  check('回到原来那张不问', await ask.count() === 0 && here() === '/studio/st-gov', here())
  await pick('__studio_check__')
  await ask.waitFor({ timeout: 3000 }).catch(() => {})
  await ask.getByRole('button', { name: '放弃并切换' }).click({ timeout: 2000 }).catch(() => {})
  await page.waitForFunction(() => window.__studio.getState().workflow?.id === 'st-main', null, { timeout: 5000 }).catch(() => {})
  await page.waitForTimeout(600)
  check('选择器问过一次，换过去不再问', here() === '/studio/st-main' && await ask.count() === 0, here())
  check('换过去的那张是干净的，守卫撤了', !(await S(page, () => window.__studio.getState().dirty))
    && await page.evaluate(() => window.__leave.count()) === 0)

  const scribble = () => S(page, () => {
    const s = window.__studio.getState()
    const n = s.nodes[0]
    s.updateNode(n.id, { label: `${n.data.label}（没存）` })
  })
  const toRuns = async () => {
    await page.locator('nav[aria-label="主导航"] a[href="/runs"]').click()
    await page.waitForFunction(() => location.pathname === '/runs', null, { timeout: 3000 }).catch(() => {})
    await page.waitForTimeout(400)
  }
  const landOn = async (id) => {
    await page.waitForFunction((id) => window.__studio.getState().workflow?.id === id, id, { timeout: 5000 }).catch(() => {})
    await page.waitForTimeout(600)
    return { at: here(), id: await wfId(), asks: await asks(page), open: await ask.count(),
      dirty: await S(page, () => window.__studio.getState().dirty) }
  }

  // ⌘K 从别的页打开另一张、选「放弃并切换」：编排页是在记录页之后新挂上的，得认出守卫已经
  // 问过（takeDiscarded），不能落地后再补问一遍
  await watchAsks(page)
  await scribble()
  await page.waitForTimeout(150)
  await toRuns()
  await page.keyboard.press(`${MOD}+k`)
  await page.waitForTimeout(300)
  await page.keyboard.type('__studio_check_team__')
  await page.waitForTimeout(300)
  await page.keyboard.press('Enter')
  await ask.waitFor({ timeout: 3000 }).catch(() => {})
  check('⌘K 从记录页打开：问的时候还在记录页', await ask.count() === 1 && here() === '/runs', here())
  await ask.getByRole('button', { name: '放弃并切换' }).click({ timeout: 2000 }).catch(() => {})
  const k = await landOn('st-team')
  check('选放弃：落到那一张，整条路只问了一次', k.at === '/studio/st-team' && k.id === 'st-team' && k.asks === 1
    && k.open === 0 && !k.dirty, JSON.stringify(k))

  // 先建再跳（问数据页「在画布里打开」、记录页「提取为草稿」照着写的那条路）：
  //   if (!(await canLeave('/studio/'))) return; …建好…; navigate(`/studio/${新 id}`, leavePass())
  // 问的时候还没有新 id。落地后编排页再问一遍的话，第二遍点取消，刚建的那张就成了孤儿。
  // 从别的页来（编排页新挂上）、就在编排页上（已经挂着）各走一遍
  const recipe = async (choice) => {
    await page.evaluate(async () => {
      const { canLeave } = await window.__appImport('/src/lib/leave.ts')
      window.__left = canLeave('/studio/')
    })
    await ask.waitFor({ timeout: 3000 }).catch(() => {})
    await ask.getByRole('button', { name: choice }).click({ timeout: 2000 }).catch(() => {})
    return page.evaluate(() => Promise.race([window.__left, new Promise((res) => setTimeout(() => res('pending'), 2000))]))
  }
  const pass = () => page.evaluate(async () => {
    const { leavePass } = await window.__appImport('/src/lib/leave.ts')
    return leavePass()
  })
  for (const [where, to] of [['记录页', 'st-lint'], ['编排页', 'st-main']]) {
    await watchAsks(page)
    await scribble()
    await page.waitForTimeout(150)
    if (where === '记录页') await toRuns()
    const left = await recipe('放弃并切换')
    check(`先建再跳（在${where}）：canLeave('/studio/') 问过、说放弃`, left === true, String(left))
    await routerGo(page, `/studio/${to}`, await pass())
    const r = await landOn(to)
    check(`先建再跳（在${where}）：带 leavePass() 落到新的那张，只问了一次`, r.at === `/studio/${to}` && r.id === to
      && r.asks === 1 && r.open === 0 && !r.dirty, JSON.stringify(r))
  }
  // 上一问说了放弃、却没跳成（建的时候出错了），这一问说取消：canLeave 给 false，调用方不建
  // 也不跳，改动还在。而且上一问的「放弃」不能还作数——之后哪条路没经过守卫就进了编排页，
  // 兜底那一问照样要问，不能拿着那句「放弃」悄悄把改动扔了
  await watchAsks(page)
  await scribble()
  await page.waitForTimeout(150)
  await recipe('放弃并切换')
  const stayed = await recipe('取消')
  const s2 = await landOn('st-main')
  check('先建再跳说取消：不放行，留在原地，改动还在', stayed === false && s2.at === '/studio/st-main' && s2.dirty
    && s2.asks === 2 && s2.open === 0, `${stayed} · ${JSON.stringify(s2)}`)
  await routerGo(page, '/studio/st-gov', await pass())
  await ask.waitFor({ timeout: 3000 }).catch(() => {})
  check('之后绕过守卫换图：兜底照样问，不拿上一问的「放弃」作数', await ask.count() === 1 && await asks(page) === 3,
    `${here()} · 问了 ${await asks(page)} 次`)
  await ask.getByRole('button', { name: '取消' }).click({ timeout: 2000 }).catch(() => {})
  const s3 = await landOn('st-main')
  check('兜底那一问选取消：地址退回原来那张，改动还在', s3.at === '/studio/st-main' && s3.dirty, JSON.stringify(s3))
  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))
  await ctx.close()
})

// ================================================================ 7b
await section('工作流目录没取回来：不劝人新建（EmptyState source）', async () => {
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
    // 目录一直不回来时，启动屏 5 秒后给「直接进入」：进去之后才轮到编排页说话
    if (mode === 'loading') await page.getByRole('button', { name: '直接进入' }).click({ timeout: 12000 }).catch(() => {})
    const hit = await page.waitForSelector(`[data-empty-unknown="${mode}"]`, { timeout: 8000 }).then(() => true).catch(() => false)
    check(mode === 'loading' ? '目录还在读：说「正在读取」' : '目录没取回来：说加载失败、给重新读取', hit
      && (mode === 'loading' ? /正在读取/ : /加载失败/).test(await page.locator('[data-empty-unknown]').innerText()))
    check(mode === 'loading' ? '读的时候不劝人新建' : '读失败时不劝人新建',
      await page.getByRole('button', { name: /新建工作流/ }).count() === 0)
    await ctx.close()
  }
})

// ================================================================ 8
await section('删掉眼前这张', async () => {
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
  check('卸空时不误报「不存在」', await solo.page.getByText('该工作流不存在').count() === 0)
  await solo.ctx.close()
})

// ================================================================ E2W
await section('发起就被拒（工具不存在）给修复入口；整形节点解析上游失败，入口指到上游（E2W-4/5）', async () => {
  const { ctx, page, errors } = await open()
  // lib/explain 的纯函数：按页面自己加载的那一份取
  const x = await page.evaluate(async () => {
    const m = await window.__appImport('/src/lib/explain.ts')
    const e = (msg, code) => Object.assign(new Error(msg), code ? { code } : {})
    return {
      // 工具不存在的三条喂的是旧原文（「在本机不存在」「绑的」「去数据页接入」）：历史运行里存的是这种说法，
      // 证明它仍认得；后端现在的原文见下面画布上发起被拒那段（DETAIL）
      data: m.explainRunError('绑定的工具在本机不存在：「查数」（调用工具）绑的 db_query__nope；「团队」的成员「研究员」绑的 db_schema__nope。去数据页接入，或在节点里重新选'),
      custom: m.explainRunError('绑定的工具在本机不存在：「查数」（Agent）绑的 lookup_order。去工具页接入，或在节点里重新选'),
      mcp: m.explainRunError('绑定的工具在本机不存在：「查数」（Agent）绑的 mcp:shop/query。去工具页接入，或在节点里重新选'),
      // 话故意不是「绑定的工具不存在」：看的是只认机读码
      coded: m.explainStartError(e('无法发起：部分工具不存在', 'run_tool_missing')),
      other: m.explainStartError(e('工作流不存在，可能已被删除', 'workflow_gone')),
      up: m.explainRunError('上游「快速回答」输出的不是合法 JSON（第 12 列附近），常见原因是字符串中有未转义的英文引号；如需模型输出结构化数据，请为其配置「结构化输出 Schema」；数字需要进入口径卡时，请改用 Agent 并开启「按出处核对字段」'),
      pointed: m.explainRunError('模板渲染出来的不是合法 JSON（第 1 行第 20 列附近）。模板里 {{ vars.result }} 是模型写的文字，放入 JSON 时请写成 {{ vars.result | json }}（外面不要再加引号）'),
      empty: m.explainRunError('模板渲染出来的不是合法 JSON（第 1 行第 20 列附近）。模板里 {{ vars.x }} 的取值为空，此处缺少一个值：请检查路径是否正确、前面的节点是否有产出'),
      old: m.explainRunError('模板渲染出来的不是合法 JSON（第 1 行第 20 列附近）。请检查模板中的引号和逗号，字符串值需使用 | json 过滤器输出'),
    }
  })
  check('工具不存在：数据源工具（db_query__ / db_schema__）指向数据页，不给继续运行',
    x.data.fixTo === '/data' && x.data.fixLabel === '前往「数据」页接入' && x.data.continuable === false, JSON.stringify(x.data).slice(0, 160))
  check('……自定义工具指向工具页的自定义工具，一个工具时标题点名', x.custom.fixTo === '/tools/custom' && x.custom.fixLabel === '前往「工具」页接入'
    && x.custom.continuable === false && x.custom.title === '绑定的工具「lookup_order」不存在', `${x.custom.fixTo} ${x.custom.title}`)
  check('……MCP 工具指向工具页的 MCP 接入', x.mcp.fixTo === '/tools/mcp', x.mcp.fixTo)
  check('发起报错认机读码 run_tool_missing（话改了也认），别的码不认', x.coded?.continuable === false && x.coded?.fixTo === '/tools'
    && x.other === null, JSON.stringify({ coded: x.coded?.fixTo, other: x.other }))
  check('上游写坏了 JSON：入口指到上游「快速回答」（fixNode），不指整形节点', x.up.fixNode === '快速回答' && x.up.fix === 'canvas' && !x.up.continuable, x.up.fixNode)
  check('模板里 {{ 开头（后端给了准确改法）：不再追加「改用结构化输出 Schema」', !x.pointed.action.includes('结构化输出 Schema')
    && x.pointed.action.includes('| json') && !x.empty.action.includes('结构化输出 Schema'), `${x.pointed.action} ｜ ${x.empty.action}`)
  check('……笼统的说法照旧追加那一句', x.old.action.includes('「结构化输出 Schema」和「按出处核对字段」'), x.old.action)

  // 画布上发起：POST /runs 回 422 run_tool_missing，报错里给「前往「数据」页接入」，点了就去
  const DETAIL = '绑定的工具不存在：「查询销量」（调用工具）绑定的 db_query__nope。请到「数据」页接入，或在节点中重新选择'
  await page.route(/\/api\/runs$/, (route) => (route.request().method() === 'POST'
    ? route.fulfill({ status: 422, contentType: 'application/json', body: JSON.stringify({ detail: DETAIL, code: 'run_tool_missing' }) })
    : route.fallback()))
  // 这张图有一处「还没有选择工具」会把运行按钮置灰：等校验落定后清掉，只看发起被拒这一步
  await waitAnalysis(page)
  await S(page, () => window.__studio.setState({ issues: [] }))
  await page.waitForTimeout(150)
  await page.locator('[data-run-control] button[aria-label="运行"]').click()
  await page.locator('[role="dialog"][aria-label="探索运行"]').locator('textarea, input').first().fill('x')
  await page.locator('[role="dialog"][aria-label="探索运行"] button.btn-primary').click()
  const fixBtn = page.getByRole('button', { name: '前往「数据」页接入' })
  await fixBtn.waitFor({ timeout: 4000 }).catch(() => {})
  check('发起被拒：报错里有「前往「数据」页接入」', await fixBtn.count() === 1)
  const toastText = await page.locator('text=绑定的工具「db_query__nope」不存在').first().innerText().catch(() => '')
  check('……标题点名那个工具，原因里说本次运行未启动', toastText.includes('db_query__nope') && toastText.includes('本次运行未启动'), toastText.slice(0, 120))
  check('……没有「继续运行」', await page.getByRole('button', { name: '继续运行' }).count() === 0)
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await page.waitForTimeout(200)
      await page.screenshot({ path: `${SHOTS}/studio-run-tool-missing-${theme}.png`, clip: { x: 300, y: 40, width: 900, height: 240 } })
    }
  }
  await fixBtn.click().catch(() => {})
  await page.waitForTimeout(400)
  check('……点了去数据页', new URL(page.url()).pathname.startsWith('/data'), page.url())
  await ctx.close()

  // 整形节点解析上游的文字失败：轮次顶上的入口打开上游「快速回答」的设置，不是「汇总」
  const run = await open()
  const UP = '上游「快速回答」输出的不是合法 JSON（第 12 列附近），常见原因是字符串中有未转义的英文引号；如需模型输出结构化数据，请为其配置「结构化输出 Schema」；数字需要进入口径卡时，请改用 Agent 并开启「按出处核对字段」'
  await S(run.page, (msg) => {
    const st = window.__studio
    const t = Date.now() / 1000 - 5
    st.setState({ run: { id: 'st-run-json', workflow_id: 'st-main', workflow_name: '__studio_check__', status: 'queued',
      input: {}, output: {}, error: null, usage: {}, run_class: 'exploratory', version: null } })
    // 真实的 node.started 带着节点名和类型：流里的步骤按名字认上游
    const list = [['run.started', null, {}], ['node.started', 'answer', { label: '快速回答', node_type: 'llm' }],
      ['node.finished', 'answer', { duration_ms: 3 }], ['node.started', 'sum', { label: '汇总', node_type: 'transform' }],
      ['node.failed', 'sum', { error: msg, duration_ms: 2 }], ['run.failed', null, { error: msg, node_id: 'sum' }]]
    list.forEach((e, i) => st.getState().applyEvent({ seq: i + 1, type: e[0], node_id: e[1], data: e[2], ts: t + i * 0.2 }))
  }, UP)
  const turnFix = run.page.locator('[data-turn-error] [data-fix="canvas"]')
  await turnFix.waitFor({ timeout: 4000 }).catch(() => {})
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await run.page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await run.page.waitForTimeout(200)
      await run.page.locator('[data-turn-error]').first().screenshot({ path: `${SHOTS}/studio-upstream-json-fix-${theme}.png` }).catch(() => {})
    }
  }
  const label = await turnFix.innerText().catch(() => '')
  check('轮次报错的入口：打开上游「快速回答」的设置', label.includes('打开「快速回答」的设置'), label)
  await turnFix.click().catch(() => {})
  await run.page.waitForTimeout(300)
  check('……点了选中上游节点（不是整形节点）', await S(run.page, () => window.__studio.getState().selectedId) === 'answer')
  check('没有运行时报错', errors.length === 0 && run.errors.length === 0, [...errors, ...run.errors].slice(0, 2).join(' | '))
  await run.ctx.close()
})

await section('证据路径：点开报告片段时画布高亮、节点名能对准；报告卡上的章', async () => {
  // 运行号用夹具里文档记着的那个：成果字段取文档时核对「是不是这次运行写的」，对不上就按普通文本画
  const EV_RUN = fxe.run_id
  const json = (route, body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
  /** 伪造这张图的「最近一次运行」：latest 为 null 时一次都没跑过；传函数时每次请求现取（中途换成新跑的那次） */
  const routes = (latest) => async (page) => {
    await page.route((u) => { const x = new URL(u); return x.pathname === '/api/runs' && x.searchParams.get('workflow_id') === 'st-ev' },
      (r) => {
        const id = typeof latest === 'function' ? latest() : latest
        return json(r, id ? [{ id, workflow_id: 'st-ev', status: 'succeeded', run_class: 'exploratory' }] : [])
      })
    await page.route(new RegExp(`/api/runs/${fxe.run_id}/evidence$`), (r) => json(r, fxe.graph))
    await page.route(new RegExp(`/api/runs/${fxe.run_id}/evidence/audit(\\?.*)?$`), (r) => json(r, fxe.audit))
    await page.route(new RegExp(`/api/runs/${fxe.dup.run_id}/evidence/audit(\\?.*)?$`), (r) => json(r, fxe.dup.audit))
    await page.route(new RegExp(`/api/artifacts/${fxe.doc_artifact}$`), (r) => json(r, { id: fxe.doc_artifact, content: fxe.doc }))
    await page.route(new RegExp(`/api/artifacts/${fxe.dup.doc_artifact}$`), (r) => json(r, { id: fxe.dup.doc_artifact, content: fxe.dup.doc }))
    await page.route(new RegExp(`/api/runs/${fxe.run_id}/evidence/segments/`), (r) => {
      const sid = decodeURIComponent(new URL(r.request().url()).pathname.split('/').pop())
      return fxe.segments[sid] ? json(r, fxe.segments[sid]) : json(r, { detail: '没有这个片段' }, 404)
    })
  }
  const stamp = (page) => page.locator('.react-flow__node[data-id="write"] [data-report-stamp]')
  const st = fxe.report_checked.stats
  const cited = st.numbers_cited + st.values + st.entities + st.quotes
  const none = (st.numbers - st.numbers_cited) + Math.max(0, st.uncited_numbers + st.unresolved - (st.numbers - st.numbers_cited))
    + st.unknown_entities + st.uncited_claims
  // 另一次运行（结论句策略 off）：章上的数照 stampCounts 的口径算
  const sd = fxe.dup.report_checked.stats
  const dupText = `有出处 ${sd.numbers_cited + sd.values + (sd.entities ?? 0) + (sd.quotes ?? 0)} · 无证据 ${(sd.numbers - sd.numbers_cited)
    + Math.max(0, sd.uncited_numbers + sd.unresolved - (sd.numbers - sd.numbers_cited)) + (sd.unknown_entities ?? 0)}`
  /** 画布上把运行摘下来（RunHud「清除」）：等它重新去取这张图的最近一次运行（取回来、画完再读章） */
  const clearAndRefetch = async (pg) => {
    const refetch = pg.waitForRequest((r) => { const x = new URL(r.url()); return x.pathname === '/api/runs' && x.searchParams.get('workflow_id') === 'st-ev' },
      { timeout: 3000 }).catch(() => null)
    await S(pg, () => window.__studio.getState().clearRun())
    const req = await refetch
    await pg.waitForLoadState('networkidle').catch(() => {})
    await pg.waitForTimeout(400)
    return !!req
  }

  // 一次都没跑过：不画章
  let latestRun = null
  const { page, errors } = await open({ path: '/studio/st-ev', before: routes(() => latestRun) })
  await page.waitForTimeout(400)
  check('这张图没有运行：报告节点卡上不画章', await stamp(page).count() === 0)

  // 挂上一次运行：report.checked 落下就盖章，数字取它的统计（结论句策略为 require_citation，没挂依据的结论句计入）
  await S(page, ({ id, rc, output }) => {
    const s = window.__studio
    const t = Date.now() / 1000 - 5
    s.setState({ run: { id, workflow_id: 'st-ev', workflow_name: '__studio_check_ev__', status: 'queued', input: { week: '2026-W37' },
      output: {}, error: null, usage: {}, run_class: 'exploratory', version: null } })
    const list = [['run.started', null, {}], ['node.started', 'write', { label: '写周报', node_type: 'report' }],
      ['report.checked', 'write', rc], ['node.finished', 'write', { duration_ms: 1200 }],
      ['run.finished', null, { output, usage: {}, timing: { wall_ms: 1500, active_ms: 1500, wait_ms: 0 } }]]
    list.forEach((e, i) => s.getState().applyEvent({ seq: i + 1, type: e[0], node_id: e[1], data: e[2], ts: t + i * 0.2 }))
  }, { id: EV_RUN, rc: fxe.report_checked, output: fxe.output })
  await stamp(page).waitFor({ timeout: 4000 }).catch(() => {})
  const text = await stamp(page).innerText().catch(() => '')
  check(`报告卡上盖章「有出处 ${cited} · 无证据 ${none}」`, text === `有出处 ${cited} · 无证据 ${none}`, text)
  const title = await stamp(page).getAttribute('title').catch(() => '')
  check('章的悬停说明写出可疑名字、没挂依据的结论句计入缺口', (title ?? '').includes('疑似不存在的名称 2')
    && (title ?? '').includes('计入缺口'), (title ?? '').replace(/\n/g, ' / '))
  check('有缺口的章用提醒色，不是绿的', (await stamp(page).getAttribute('class')).includes('is-degraded'))
  check('别的节点卡上没有章', await page.locator('[data-report-stamp]').count() === 1)

  // 右栏的报告点开片段：证据路径画到画布上（LineageLayer 的 style[data-lineage]）
  const doc = page.locator('[data-assistant-panel] [data-evidence-doc]')
  await doc.waitFor({ timeout: 6000 }).catch(() => {})
  check('右栏的成果是逐段可点的报告', await doc.locator('[data-seg]').count() > 5)
  await doc.locator('[data-seg]', { hasText: /^orders$/ }).first().click()
  await page.waitForSelector('[data-assistant-panel] [data-evidence-panel="inline"]', { timeout: 4000 }).catch(() => {})
  await page.waitForTimeout(300)
  const lineage = await page.evaluate(() => {
    const el = document.querySelector('style[data-lineage]')
    return el ? { var: el.getAttribute('data-lineage'), css: el.textContent } : null
  })
  check('点开片段：画布上画出证据路径（LineageLayer 的 data-lineage 是这段证据）', lineage?.var === '证据 orders', lineage?.var ?? '没有')
  check('……产出证据的查询节点实线描边、报告节点虚线描边', !!lineage && /\[data-id="fetch"\][^{]*> \.nc \{\s*box-shadow/.test(lineage.css)
    && /\[data-id="write"\][^{]*> \.nc \{\s*outline: 1\.5px dashed/.test(lineage.css), (lineage?.css ?? '').slice(0, 160))
  const state = await S(page, () => window.__studio.getState().lineage)
  check('……用的是 setLineage 的原形状 {var, producers, consumers}', JSON.stringify(state) === JSON.stringify({ var: '证据 orders', producers: ['fetch'], consumers: ['write'] }),
    JSON.stringify(state))
  await doc.locator('[data-seg]', { hasText: '退款金额以财务确认日为准' }).first().click()
  await page.waitForTimeout(300)
  const quote = await S(page, () => window.__studio.getState().lineage)
  check('换一段（引文）：路径换成检索节点 → 报告', quote?.producers?.join(',') === 'manual' && quote?.consumers?.join(',') === 'write', JSON.stringify(quote))
  // 面板里的节点名：点一下选中并对准画布上的节点
  const chip = page.locator('[data-assistant-panel] [data-evidence-panel] button[data-ev-node="manual"]').first()
  check('面板里的节点名是按钮，写画布上的节点名', (await chip.innerText().catch(() => '')).includes('查手册'))
  await chip.click().catch(() => {})
  await page.waitForTimeout(250)
  const sel = await S(page, () => ({ selected: window.__studio.getState().selectedId, focus: window.__studio.getState().focusRequest?.id }))
  check('点节点名：选中并对准画布上的那个节点', sel.selected === 'manual' && sel.focus === 'manual', JSON.stringify(sel))
  await S(page, () => window.__studio.getState().select(null))
  await page.waitForTimeout(250)
  if (SHOTS) {
    await doc.locator('[data-seg]', { hasText: /^orders$/ }).first().click()
    await page.waitForTimeout(300)
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await page.waitForTimeout(250)
      await page.screenshot({ path: `${SHOTS}/studio-evidence-path-${theme}.png` })
    }
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'))
  }
  // 收起面板：只收回自己画的那条路径
  const back = page.locator('[data-assistant-panel] [data-evidence-panel] button', { hasText: '回到正文' })
  await back.click().catch(() => {})
  await page.waitForTimeout(250)
  check('收起面板：画布上的证据路径一起收掉', await page.locator('style[data-lineage]').count() === 0
    && await S(page, () => window.__studio.getState().lineage) === null)
  // 「清除」把刚才那次运行摘下来：打开画布时这张图还没跑过（取到的是空的），现在最近一次就是刚才那次——
  // 章要跟着换，不能拿打开画布时的「没跑过」当最近一次
  latestRun = EV_RUN
  const refetched = await clearAndRefetch(page)
  check('「清除」摘下运行：重新取这张图的最近一次运行', refetched)
  check('……章取刚才那次（打开画布时还没跑过，不能照旧不画）', (await stamp(page).innerText().catch(() => '没有章')) === `有出处 ${cited} · 无证据 ${none}`,
    await stamp(page).innerText().catch(() => '没有章'))
  check('没有运行时报错（证据路径）', errors.length === 0, errors.slice(0, 2).join(' | '))
  await page.context().close()

  // 没挂运行、这张图最近一次运行有报告核对：章从那次运行的证据图取
  let latestNow = fxe.run_id
  const latest = await open({ path: '/studio/st-ev', before: routes(() => latestNow) })
  await stamp(latest.page).waitFor({ timeout: 4000 }).catch(() => {})
  check('没挂运行：章取这张图最近一次运行的核对统计（和挂着运行时一样，结论句策略也算上）',
    (await stamp(latest.page).innerText().catch(() => '')) === `有出处 ${cited} · 无证据 ${none}`,
    await stamp(latest.page).innerText().catch(() => '没有章'))
  if (SHOTS) {
    // 取景到报告节点（可读的缩放），截卡片连同下沿的章，四周留一圈
    await S(latest.page, () => window.__studio.getState().focusNode('write'))
    await latest.page.waitForTimeout(600)
    for (const theme of ['dark', 'light']) {
      await latest.page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await latest.page.waitForTimeout(250)
      const box = await latest.page.locator('.react-flow__node[data-id="write"]').boundingBox()
      if (box) {
        await latest.page.screenshot({ path: `${SHOTS}/studio-report-stamp-${theme}.png`,
          clip: { x: box.x - 40, y: box.y - 40, width: box.width + 80, height: box.height + 80 } }).catch(() => {})
      }
    }
  }
  // 在画布上又跑了一次（另一份报告），跑完「清除」：最近一次换成了这次，章不能还是打开画布时取的那份
  await S(latest.page, ({ id, rc, output }) => {
    const s = window.__studio
    const t = Date.now() / 1000 - 3
    s.setState({ run: { id, workflow_id: 'st-ev', workflow_name: '__studio_check_ev__', status: 'queued', input: {},
      output: {}, error: null, usage: {}, run_class: 'exploratory', version: null } })
    const list = [['run.started', null, {}], ['node.started', 'write', { label: '写周报', node_type: 'report' }],
      ['report.checked', 'write', rc], ['node.finished', 'write', { duration_ms: 900 }],
      ['run.finished', null, { output, usage: {}, timing: { wall_ms: 1000, active_ms: 1000, wait_ms: 0 } }]]
    list.forEach((e, i) => s.getState().applyEvent({ seq: i + 1, type: e[0], node_id: e[1], data: e[2], ts: t + i * 0.2 }))
  }, { id: fxe.dup.run_id, rc: fxe.dup.report_checked, output: fxe.dup.output })
  await latest.page.waitForTimeout(300)
  check(`挂上新跑的一次：章跟着它（${dupText}）`, (await stamp(latest.page).innerText().catch(() => '')) === dupText,
    await stamp(latest.page).innerText().catch(() => '没有章'))
  latestNow = fxe.dup.run_id
  const again = await clearAndRefetch(latest.page)
  check('「清除」之后重新取最近一次运行（打开画布时取的那份作废）', again)
  check(`……章换成新跑的那次（${dupText}），不是打开画布时的「有出处 ${cited} · 无证据 ${none}」`,
    (await stamp(latest.page).innerText().catch(() => '没有章')) === dupText, await stamp(latest.page).innerText().catch(() => '没有章'))
  check('没有运行时报错（最近一次运行的章）', latest.errors.length === 0, latest.errors.slice(0, 2).join(' | '))
  await latest.ctx.close()

  // 取「最近一次运行」的请求还没回来，就在画布上跑完、清除了：先发的那次（那时还没跑过）晚回来，不能把后来
  // 取到的那份盖掉。页面加载等网络静下来才算完，慢请求只能在加载后从 store 发（和报告卡发的是同一个 loadLatest）
  let raceRun = null
  let raceHits = 0
  const race = await open({ path: '/studio/st-ev', before: async (pg) => {
    await routes(() => raceRun)(pg)
    await pg.route((u) => { const x = new URL(u); return x.pathname === '/api/runs' && x.searchParams.get('workflow_id') === 'st-ev' },
      async (r) => {
        if (raceHits++ !== 1) return r.fallback()
        const body = raceRun ? [{ id: raceRun, workflow_id: 'st-ev', status: 'succeeded', run_class: 'exploratory' }] : []
        await new Promise((res) => setTimeout(res, 1800))
        return json(r, body).catch(() => {})
      })
  } })
  await race.page.evaluate(async () => {
    const { useEvidence } = await window.__appImport('/src/store/evidence.ts')
    useEvidence.getState().dropLatest('st-ev')
    void useEvidence.getState().loadLatest('st-ev')
  })
  await race.page.waitForTimeout(150)
  await S(race.page, ({ id, rc, output }) => {
    const s = window.__studio
    const t = Date.now() / 1000 - 2
    s.setState({ run: { id, workflow_id: 'st-ev', workflow_name: '__studio_check_ev__', status: 'queued', input: {},
      output: {}, error: null, usage: {}, run_class: 'exploratory', version: null } })
    const list = [['run.started', null, {}], ['report.checked', 'write', rc],
      ['run.finished', null, { output, usage: {}, timing: { wall_ms: 1000, active_ms: 1000, wait_ms: 0 } }]]
    list.forEach((e, i) => s.getState().applyEvent({ seq: i + 1, type: e[0], node_id: e[1], data: e[2], ts: t + i * 0.2 }))
  }, { id: fxe.dup.run_id, rc: fxe.dup.report_checked, output: fxe.dup.output })
  raceRun = fxe.dup.run_id
  await race.page.waitForTimeout(150)
  await S(race.page, () => window.__studio.getState().clearRun())
  await race.page.waitForTimeout(2600)
  check(`先发的请求晚回来（那时还没跑过）：章照旧是清除后取到的那次（${dupText}），没被盖成不画`,
    raceHits >= 3 && (await stamp(race.page).innerText().catch(() => '没有章')) === dupText,
    `${raceHits} 次请求，${await stamp(race.page).innerText().catch(() => '没有章')}`)
  check('没有运行时报错（请求先后颠倒）', race.errors.length === 0, race.errors.slice(0, 2).join(' | '))
  await race.ctx.close()
})

check('整个检查没有弹出原生 confirm / prompt', dialogs === 0, `${dialogs} 次`)
check('检查没有写库（兜底拦下的写请求）', writes.length === 0, writes.slice(0, 4).join('、'))

console.log(failed ? `\n${failed} 项未通过` : '\n全部通过')
await browser.close()
process.exit(failed ? 1 : 0)
