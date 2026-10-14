// 应用编辑（搭建面，/apps/:appId/edit）的检查：固定版本、派生、页面树和属性面板、检查（按 path 定位）、
// 发布时为升版的固定版本选处置、助手流（应用操作实时落到草稿上、look 和 ask 照 decode 显示）。
//
// 守的是 docs/design/app-platform-and-look.md 的几条：
//   - 只能固定已发布且未退役的版本（A1）；新建应用从版本确定性派生，不靠模型（A8、P3）；
//   - 属性面板由组件目录的 props schema 驱动；绑定种类只能从目录登记的里挑；可见性表达式只编辑、不在浏览器求值（A2、P7）；
//   - 固定版本升版必须选处置，不给默认值，写清后果（口径升版是必须处置的事件）；
//   - 助手只润色：应用操作到一条落一条，final 的整份 spec 为准；ask 结束这一轮。
//
// 不写库：/api/apps、/api/workflows、/api/copilot/app-stream 全在浏览器里用 page.route 伪造，伪造的应用接口按收到的
// 请求改一份内存里的应用（保存、检查、发布都读它）。其余写请求一律 409 并记账，检查结束时必须是空的。
//
// 跑之前前端得起着（./scripts/dev.sh），默认连 5273。对别的实例跑时带上地址：
//   AGENTLAB_WEB=http://localhost:<前端端口> node scripts/check-app-editor.mjs
//   APP_EDITOR_ONLY=发布,助手   只跑段名里含这些字的段
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const CHROME = process.env.CHROME_PATH ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const ONLY = (process.env.APP_EDITOR_ONLY ?? '').split(',').filter(Boolean)

let failed = 0
const check = (name, cond, detail = '') => {
  const d = String(detail ?? '').replace(/\s+/g, ' ').slice(0, 160)
  console.log(`  ${cond ? '✓' : '✗'} ${name}${d ? ` — ${d}` : ''}`)
  if (!cond) failed++
}
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
const clone = (v) => JSON.parse(JSON.stringify(v))
const waitFor = async (page, fn, ms = 5000) => {
  for (let t = 0; t < ms; t += 100) {
    if (await fn()) return true
    await page.waitForTimeout(100)
  }
  return false
}

// ---------------------------------------------------------------- 夹具（通用示例名）

const CATALOG = {
  version: 1,
  categories: { layout: '布局', display: '展示', input: '输入', process: '流程' },
  components: [
    { name: 'columns', label: '分栏', category: 'layout', version: 1, children: true, bindings: [],
      props: { type: 'object', properties: { count: { type: 'integer', minimum: 1, maximum: 4 } } } },
    { name: 'metric', label: '指标块', category: 'display', version: 1, children: false, bindings: ['metric', 'cell'],
      props: { type: 'object', properties: { label: { type: 'string', maxLength: 200 }, unit: { type: 'string', maxLength: 200 } } } },
    { name: 'notice', label: '提示', category: 'display', version: 1, children: false, bindings: [],
      props: { type: 'object', properties: { text: { type: 'string', maxLength: 4000 }, tone: { enum: ['info', 'warn', 'error'] } } } },
    { name: 'report', label: '报告', category: 'display', version: 1, children: false, bindings: ['report'],
      props: { type: 'object', properties: { title: { type: 'string', maxLength: 200 } } } },
    { name: 'entry_button', label: '发起按钮', category: 'process', version: 1, children: false, bindings: [],
      props: { type: 'object', properties: { entry: { type: 'string' }, label: { type: 'string', maxLength: 200 } } } },
  ],
}

const WF = { id: 'ae-wf', name: '__app_editor_wf__', status: 'published', published_version: 7, version: 7, graph: {
  nodes: [{ id: 'kpi', type: 'metrics', position: { x: 0, y: 0 }, data: { label: '口径', config: {} } },
          { id: 'rep', type: 'report', position: { x: 200, y: 0 }, data: { label: '周报', config: {} } }],
  edges: [] }, tags: [], is_template: false, description: '' }
const VERSIONS = [7, 6, 5, 4].map((n) => ({ id: `v${n}`, version: n, note: '', created_at: '2026-10-01T02:00:00Z',
  published: n === 7, level: n === 4 ? null : 'published', retired_at: n === 5 ? '2026-10-02T02:00:00Z' : null }))

const SPEC = {
  format: 'agentlab.app/1', name: '周报看板', description: '', icon: 'app',
  pins: [{ alias: 'weekly', workflow_id: 'ae-wf', version: 6, graph_hash: 'hash6', level: 'published', use: 'view' }],
  slots: {}, entries: [], forms: {}, task_ui: {},
  views: { wk: { source: { kind: 'latest_sealed', pin: 'weekly', from: 'formal' } } },
  pages: [{ id: 'home', title: '本周概览', body: [
    { id: 'cols', type: 'columns', props: {}, children: [
      { id: 'gmv', type: 'metric', props: { label: '销售额' }, bind: { view: 'wk', metric: 'kpi.gmv' } }] },
    { id: 'warn', type: 'notice', props: { text: '本期为降级出具' }, visible: "run.tier != 'formal'", scope: 'wk' },
  ] }],
  hooks: [], notify: [],
}

async function open({ path = '/apps/app-ae/edit/pages', publish = 'disposition', stream = [] } = {}) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  opened.add(ctx)
  await ctx.addInitScript(() => { try { localStorage.setItem('agentlab_actor', '检查脚本') } catch { /* noop */ } })
  const page = await ctx.newPage()
  page.setDefaultTimeout(8000)
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  const json = (route, body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
  const app = { id: 'app-ae', name: SPEC.name, description: '', icon: 'app', status: 'active', published_version: 2, level: 'published',
    needs_upgrade: false, pending_tasks: 0, my_role: 'manage', updated_at: '2026-10-10T02:00:00Z', draft: clone(SPEC),
    draft_bindings: { slots: {} }, created_by: '检查脚本' }
  const state = { app, patches: [], lints: 0, publishes: [], derives: [], creates: [], streams: [], pages: [] }

  await page.route(/\/api\//, (route) => {
    const req = route.request()
    if (req.method() === 'GET') return route.continue()
    writes.push(`${req.method()} ${new URL(req.url()).pathname}`)
    return json(route, { detail: '检查脚本不写库' }, 409)
  })
  await page.route(/\/api\/workflows(\?.*)?$/, (route) => (route.request().method() === 'GET' ? json(route, [WF]) : route.fallback()))
  await page.route(/\/api\/workflows\/ae-wf\/versions(\/\d+)?$/, (route) => {
    const v = Number(new URL(route.request().url()).pathname.split('/')[5])
    return json(route, v ? { ...VERSIONS.find((x) => x.version === v), workflow_id: 'ae-wf', graph: WF.graph, graph_hash: `hash${v}` } : VERSIONS)
  })
  await page.route(/\/api\/apps\/catalog$/, (route) => json(route, CATALOG))
  await page.route(/\/api\/apps\/derive$/, (route) => {
    const body = route.request().postDataJSON()
    state.derives.push(body)
    return json(route, { spec: { ...clone(SPEC), name: body.name || WF.name,
      pins: [{ alias: 'p1', workflow_id: body.workflow_id, version: body.version, graph_hash: `hash${body.version}`, level: 'published', use: 'view' }] } })
  })
  await page.route(/\/api\/apps$/, (route) => {
    if (route.request().method() !== 'POST') return json(route, { items: [] })
    const body = route.request().postDataJSON()
    state.creates.push(body)
    return json(route, { ...app, id: 'app-new', name: body.name, draft: body.spec })
  })
  await page.route(/\/api\/apps\/app-[a-z]+(\/.*)?(\?.*)?$/, (route) => {
    const req = route.request()
    const url = new URL(req.url())
    const [, , , , sub, rest] = url.pathname.split('/')
    if (!sub && req.method() === 'GET') return json(route, state.app)
    if (!sub && req.method() === 'PATCH') {
      const body = req.postDataJSON()
      state.patches.push(body)
      state.app = { ...state.app, ...(body.draft ? { draft: body.draft } : {}), ...(body.draft_bindings ? { draft_bindings: body.draft_bindings } : {}) }
      return json(route, state.app)
    }
    if (sub === 'lint') {
      state.lints += 1
      return json(route, { level: null, issues: [
        { level: 'error', code: 'app.expr_arith', message: '可见性表达式不能有算术', path: '/pages/0/body/1' },
        { level: 'error', code: 'app.binding_target_missing', message: '绑定的指标在固定的版本里不存在', path: 'pages[0].body[0].children[0]' },
        { level: 'warning', code: 'app.view_source_empty', message: '视图暂时没有可读的运行', path: 'views.wk' },
      ] })
    }
    if (sub === 'publish') {
      const body = req.postDataJSON()
      state.publishes.push(body)
      if (publish === 'disposition' && !body.dispositions) {
        return json(route, { ok: false, issues: [{ level: 'error', code: 'app.upgrade_policy', message: '固定版本升了版，没有选处置' }],
          needs_disposition: [{ alias: 'weekly', from: 5, to: 6 }] })
      }
      return json(route, { ok: true, version: 3, issues: [] })
    }
    if (sub === 'versions') return json(route, { items: [] })
    if (sub === 'grants') return json(route, { items: [] })
    if (sub === 'pages') {
      state.pages.push(url.search)
      return json(route, { format: 'agentlab.view/1', app: { id: 'app-ae', name: SPEC.name, version: 2, level: 'published', needs_upgrade: false },
        page: { id: rest, title: '本周概览' }, pages: [{ id: 'home', title: '本周概览' }], sources: {}, tree: [], forms: {}, problems: [] })
    }
    return route.fallback()
  })
  await page.route(/\/api\/notify\/channels$/, (route) => json(route, { items: [] }))
  await page.route(/\/api\/copilot\/app-stream$/, (route) => {
    state.streams.push(route.request().postDataJSON())
    return route.fulfill({ status: 200, contentType: 'text/event-stream', body: stream.map((o) => `data: ${JSON.stringify(o)}\n\n`).join('') })
  })

  await page.goto(`${WEB}${path}`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(300)
  return { page, state, errors }
}

const writes = []
const browser = await chromium.launch({ executablePath: CHROME })

// ================================================================
await section('新建：从发布弹窗带着工作流和版本进来，直接派生、新建、进编辑器', async () => {
  const { page, state, errors } = await open({ path: '/apps/new/edit?workflow=ae-wf&version=7' })
  check('派生不靠模型：调 /apps/derive，带工作流和版本', await waitFor(page, async () => state.derives.length === 1)
    && state.derives[0].workflow_id === 'ae-wf' && state.derives[0].version === 7, JSON.stringify(state.derives))
  check('派生的 spec 原样建成应用', await waitFor(page, async () => state.creates.length === 1)
    && state.creates[0].spec?.pins?.[0]?.version === 7, JSON.stringify(state.creates[0]?.spec?.pins))
  await page.waitForURL('**/apps/app-new/edit/pages')
  check('建好跳进编辑器的页面一节', new URL(page.url()).pathname === '/apps/app-new/edit/pages')
  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))
})

await section('新建：不带参数时先选工作流和版本，只列已发布且未退役的', async () => {
  const { page } = await open({ path: '/apps/new/edit' })
  check('有派生说明', (await page.locator('[data-app-editor="new"]').innerText()).includes('确定性地派生'))
  await page.locator('[data-app-editor="new"] select').first().selectOption('ae-wf')
  const opts = await page.locator('[data-app-editor="new"] select').nth(1).locator('option').allInnerTexts()
  check('版本只列已发布且未退役的（v5 退役、v4 没发布不列）', opts.some((t) => t.startsWith('v7')) && opts.some((t) => t.startsWith('v6'))
    && !opts.some((t) => t.startsWith('v5')) && !opts.some((t) => t.startsWith('v4')), opts.join(' | '))
})

await section('固定版本：别名、用途、改版本取新的哈希', async () => {
  const { page, state } = await open({ path: '/apps/app-ae/edit/pins' })
  const pin = page.locator('[data-pin="weekly"]')
  check('列出固定的版本', await pin.count() === 1 && (await pin.innerText()).includes('hash6'.slice(0, 8)))
  await pin.locator('select').first().selectOption('7')
  await page.waitForTimeout(300)
  await page.locator('[data-app-save]').click()
  await waitFor(page, async () => state.patches.length === 1)
  const saved = state.patches[0]?.draft?.pins?.[0]
  check('改版本：版本和哈希一起换（哈希取自版本详情）', saved?.version === 7 && saved?.graph_hash === 'hash7', JSON.stringify(saved))
  check('界面写「固定版本」，不写钉住', !(await page.locator('main').innerText()).includes('钉'))
})

await section('页面树和属性面板：目录驱动、绑定、可见性', async () => {
  const { page, state } = await open()
  check('页面树列出组件', await page.locator('[data-app-component]').count() === 3)
  await page.locator('[data-app-component="gmv"]').click()
  const insp = page.locator('[data-app-inspector="gmv"]')
  check('属性面板按目录的 props 画：标签、单位', await insp.locator('[data-app-prop="label"]').count() === 1
    && await insp.locator('[data-app-prop="unit"]').count() === 1)
  const kinds = await insp.locator('[data-app-bind-kind] option').allInnerTexts()
  check('绑定种类只给目录登记的（指标、单元格）', kinds.includes('指标') && kinds.includes('单元格') && !kinds.includes('报告'), kinds.join(' | '))
  await insp.locator('[data-app-prop="unit"] input').fill('元')
  await page.locator('[data-app-component="warn"]').click()
  const vis = page.locator('[data-app-inspector="warn"] [data-app-visible]')
  check('可见性表达式照原样显示，可编辑', await vis.inputValue() === "run.tier != 'formal'")
  await page.locator('[data-app-palette]').click()
  await page.locator('[data-app-palette-item="report"]').click()
  check('从目录添加组件，新组件选中', await page.locator('[data-app-inspector^="report"]').count() === 1)
  await page.locator('[data-app-save]').click()
  await waitFor(page, async () => state.patches.length === 1)
  const body = state.patches[0]?.draft?.pages?.[0]?.body ?? []
  check('保存的草稿里有改过的属性和新组件', JSON.stringify(body).includes('"unit":"元"') && JSON.stringify(body).includes('"type":"report"'),
    JSON.stringify(body).slice(0, 200))
})

await section('检查：和发布同一套规则，点一条定位到组件', async () => {
  const { page, state } = await open({ path: '/apps/app-ae/edit/check' })
  check('打开就检查一次', await waitFor(page, async () => state.lints === 1))
  const issues = page.locator('[data-app-issue]')
  check('列出错误和提醒', await waitFor(page, async () => await issues.count() === 3))
  await page.locator('[data-app-issue="app.binding_target_missing"] [data-app-issue-locate]').click()
  await page.waitForURL('**/edit/pages')
  check('点「定位」：跳到页面一节、选中那个组件（点号加下标的 path）', await page.locator('[data-app-inspector="gmv"]').count() === 1)
  await page.goto(`${WEB}/apps/app-ae/edit/check`, { waitUntil: 'networkidle' })
  await page.locator('[data-app-issue="app.expr_arith"] [data-app-issue-locate]').click()
  await page.waitForURL('**/edit/pages')
  check('JSON 指针的 path 也认得', await page.locator('[data-app-inspector="warn"]').count() === 1)
})

await section('发布：固定版本升了版，必须为每一项选处置', async () => {
  const { page, state } = await open({ path: '/apps/app-ae/edit/publish' })
  await page.locator('[data-app-publish-submit]').click()
  const dlg = page.locator('[role="dialog"]').filter({ hasText: '请为每一项选处置' })
  check('服务端要处置时弹出选择', await dlg.waitFor({ timeout: 4000 }).then(() => true, () => false))
  check('写清每种处置的后果', (await dlg.innerText()).includes('补算') && (await dlg.innerText()).includes('两条线')
    && (await dlg.innerText()).includes('断开'))
  check('不给默认值：没选之前不能发布', await dlg.locator('[data-app-disposition-submit]').isDisabled())
  await dlg.locator('[data-app-disposition="weekly"] [data-app-disposition-option="incomparable"]').check()
  await dlg.locator('[data-app-disposition-submit]').click()
  check('带着处置重发', await waitFor(page, async () => state.publishes.length === 2)
    && state.publishes[1].dispositions?.weekly === 'incomparable', JSON.stringify(state.publishes))
  check('发布成功提示应用版本', await waitFor(page, async () => (await page.locator('body').innerText()).includes('已发布应用版本 v3')))
})

await section('预览：按草稿解析、不发起运行', async () => {
  const { page, state } = await open({ path: '/apps/app-ae/edit/preview' })
  check('按草稿取页面（draft=1）', await waitFor(page, async () => state.pages.some((q) => q.includes('draft=1'))), state.pages.join(','))
})

await section('助手：应用操作实时落到草稿上，look 和 ask 照 decode 显示', async () => {
  const stream = [
    { op: 'look', id: 'L1', by: 'system', need: '固定的工作流版本', status: 'ok', elapsed_ms: 80,
      objects: [{ kind: 'workflow_version', named: ['周销售 v7'], missing: [] }] },
    { op: 'app_add', parent: 'home', index: 0, component: { id: 'hello', type: 'notice', props: { text: '欢迎' } } },
    { op: 'ask', question: '报告放在指标下面还是单独一页？' },
  ]
  const { page, state, errors } = await open({ stream })
  await page.locator('[data-app-assistant] textarea').fill('首页先看指标再看报告')
  await page.keyboard.press('Enter')
  check('请求带上编辑器里此刻的草稿和岗位绑定', await waitFor(page, async () => state.streams.length === 1)
    && state.streams[0].spec?.pages?.[0]?.id === 'home' && state.streams[0].app_id === 'app-ae' && !!state.streams[0].bindings)
  check('应用操作落到页面树上', await waitFor(page, async () => await page.locator('[data-app-component="hello"]').count() === 1))
  const steps = page.locator('[data-app-assistant]')
  check('系统查看固定版本那一行照 decode 的说法', await waitFor(page, async () => (await steps.innerText()).includes('按固定版本查看了 1 个工作流版本')))
  check('ask 结束这一轮，问题照实显示', (await steps.innerText()).includes('助手需要你确认'))
  check('助手改的草稿算未保存的修改', await page.locator('[data-app-save]').isEnabled())
  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))
})

check('检查没有写库（兜底拦下的写请求）', writes.length === 0, writes.slice(0, 4).join('、'))
console.log(failed ? `\n${failed} 项未通过` : '\n全部通过')
await browser.close()
process.exit(failed ? 1 : 0)
