// 应用编辑（搭建面，/apps/:appId/edit）的检查：固定版本、派生、页面树和属性面板、检查（按 path 定位）、
// 发布时为升版的固定版本选处置、助手流（应用操作实时落到草稿上、look 和 ask 照 decode 显示）。
//
// 守的是 docs/design/app-platform-and-look.md 的几条：
//   - 只能固定已发布且未退役的版本（A1）；新建应用从版本确定性派生，不靠模型（A8、P3）；
//   - 属性面板由组件目录的 props schema 驱动；绑定种类只能从目录登记的里挑；可见性表达式只编辑、不在浏览器求值（A2、P7）；
//   - 固定版本升版必须选处置，不给默认值，写清后果（口径升版是必须处置的事件）；
//   - 助手只润色：应用操作到一条落一条，final 的整份 spec 为准；ask 结束这一轮；
//   - 助手对话按应用一段（D-e）：先取或建、请求带 conversation_id、每轮落一条（问题、说明、查看记录），之前的轮次显示出来；
//   - 钩子按名字（D-g）：声明 ∪ 已有的行，标出发布版里有没有、生成过密钥没有；密钥只显示一次；
//   - 入口、表单、待办外观按 JSON 编辑（带格式校验），检查的 path 落在这几块上时定位到 JSON 的那一行；
//   - 按新口径补算（D-f）：固定版本升了版、处置为按新口径重算、视图里有趋势时才给按钮，确认框写明最多发起几次运行。
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
    // 属性的 title、description、x-enum-labels 照服务端目录（apps/catalog.py）的写法
    { name: 'metric', label: '指标块', category: 'display', version: 1, children: false, bindings: ['metric', 'cell'],
      props: { type: 'object', properties: { label: { type: 'string', maxLength: 200, title: '名称' },
        unit: { type: 'string', maxLength: 200, title: '单位', description: '绑定单元格时写在数下面；绑定指标时数里已经带着口径卡的单位' } } } },
    { name: 'notice', label: '提示', category: 'display', version: 1, children: false, bindings: [],
      props: { type: 'object', properties: { text: { type: 'string', maxLength: 4000, title: '提示文字' },
        tone: { enum: ['info', 'warn', 'error'], 'x-enum-labels': ['说明', '提醒', '错误'], title: '样式' } } } },
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

const LINT = [
  { level: 'error', code: 'app.expr_arith', message: '可见性表达式不能有算术', path: '/pages/0/body/1' },
  { level: 'error', code: 'app.binding_target_missing', message: '绑定的指标在固定的版本里不存在', path: 'pages[0].body[0].children[0]' },
  { level: 'warning', code: 'app.view_source_empty', message: '视图暂时没有可读的运行', path: 'views.wk' },
]

async function open({ path = '/apps/app-ae/edit/pages', publish = 'disposition', stream = [], spec = SPEC, lint = LINT,
  versions = [], versionSpec = null, hooks = [], conversation = { id: 'conv-ae', kind: 'app', title: '', turns: [] },
  backfill = undefined, plan = null } = {}) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  opened.add(ctx)
  await ctx.addInitScript(() => { try { localStorage.setItem('agentlab_actor', '检查脚本') } catch { /* noop */ } })
  const page = await ctx.newPage()
  page.setDefaultTimeout(8000)
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  const json = (route, body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
  const app = { id: 'app-ae', name: SPEC.name, description: '', icon: 'app', status: 'active', published_version: 2, level: 'published',
    needs_upgrade: false, pending_tasks: 0, my_role: 'manage', updated_at: '2026-10-10T02:00:00Z', draft: clone(spec),
    draft_bindings: { slots: {} }, created_by: '检查脚本', ...(backfill ? { backfill } : {}) }
  const state = { app, patches: [], lints: 0, publishes: [], derives: [], creates: [], streams: [], pages: [],
    hooks: clone(hooks), rotates: [], hookPatches: [], hookDeletes: [], backfills: [], turns: [], turnPatches: [], conversations: 0 }

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
      return json(route, { level: null, issues: lint })
    }
    if (sub === 'hooks') {
      const [, , , , , name, action] = url.pathname.split('/')
      if (!name) return json(route, { items: state.hooks })
      const hook = decodeURIComponent(name)
      if (action === 'rotate') {
        state.rotates.push(hook)
        return json(route, { id: `h-${hook}`, name: hook, kind: 'signal', entry_id: null, enabled: true, has_secret: true,
          in_draft: true, in_published: false, url_path: `/api/hooks/h-${hook}`, created_at: '2026-10-14T02:00:00Z',
          secret: `secret-of-${hook}-0123456789` })
      }
      if (req.method() === 'PATCH') { state.hookPatches.push({ hook, body: req.postDataJSON() }); return json(route, {}) }
      if (req.method() === 'DELETE') { state.hookDeletes.push(hook); return route.fulfill({ status: 204, body: '' }) }
    }
    if (sub === 'assistant-conversation') {
      state.conversations += 1
      return json(route, conversation)
    }
    if (sub === 'views' && url.pathname.endsWith('/backfill')) {
      // GET：补算计划（不发起运行）；POST：发起，回发起了几次、跳过几次
      if (req.method() === 'GET') return plan ? json(route, plan) : json(route, { detail: '没有这个接口' }, 404)
      state.backfills.push(url.pathname.split('/')[5])
      return json(route, { view: 'wk', launched: 7, skipped: 1, run_ids: [] })
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
    if (sub === 'versions') {
      return json(route, rest ? { ...versions.find((v) => v.version === Number(rest)), spec: versionSpec ?? spec, bindings: { slots: {} } }
        : { items: versions })
    }
    if (sub === 'grants') return json(route, { items: [] })
    if (sub === 'pages') {
      state.pages.push(url.search)
      return json(route, { format: 'agentlab.view/1', app: { id: 'app-ae', name: SPEC.name, version: 2, level: 'published', needs_upgrade: false },
        page: { id: rest, title: '本周概览' }, pages: [{ id: 'home', title: '本周概览' }], sources: {}, tree: [], forms: {}, problems: [] })
    }
    return route.fallback()
  })
  await page.route(/\/api\/notify\/channels$/, (route) => json(route, { items: [] }))
  // 助手对话的轮次：开一轮、落说明和查看记录
  await page.route(/\/api\/conversations\/conv-ae\/turns(\/[^/]+)?$/, (route) => {
    const req = route.request()
    if (req.method() === 'POST') {
      state.turns.push(req.postDataJSON())
      return json(route, { id: `turn-${state.turns.length}`, seq: state.turns.length, question: req.postDataJSON().question, answer: '',
        explanation: '', status: 'running', error: '' })
    }
    state.turnPatches.push(req.postDataJSON())
    return json(route, { id: 'turn-1', seq: 1, question: '', answer: '', explanation: '', status: 'done', error: '' })
  })
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
  const { page, state, errors } = await open({ path: '/apps/new/edit' })
  // 应用首页的「新建应用」到这里：新建向导的起点，现在只有「从已有的工作流开始」一种
  const start = page.locator('[data-app-editor="new"] [data-app-new-start="workflow"]')
  check('写明从已有的工作流开始', await start.count() === 1 && (await start.locator('h2').innerText()) === '从已有的工作流开始')
  check('有派生说明', (await page.locator('[data-app-editor="new"]').innerText()).includes('确定性地派生'))
  check('没选之前不派生、不能点', state.derives.length === 0 && await page.locator('[data-app-derive]').isDisabled())
  await page.locator('[data-app-editor="new"] select').first().selectOption('ae-wf')
  const opts = await page.locator('[data-app-editor="new"] select').nth(1).locator('option').allInnerTexts()
  check('版本只列已发布且未退役的（v5 退役、v4 没发布不列）', opts.some((t) => t.startsWith('v7')) && opts.some((t) => t.startsWith('v6'))
    && !opts.some((t) => t.startsWith('v5')) && !opts.some((t) => t.startsWith('v4')), opts.join(' | '))
  // 选好版本、起个名字：按这一版派生、建应用、进编辑器，和「做成应用」走同一条路
  await page.locator('[data-app-editor="new"] select').nth(1).selectOption('6')
  await page.locator('[data-app-editor="new"] input').fill('门店周报看板')
  await page.locator('[data-app-derive]').click()
  check('按选的版本派生，带上起的名字', await waitFor(page, async () => state.derives.length === 1)
    && state.derives[0].workflow_id === 'ae-wf' && state.derives[0].version === 6 && state.derives[0].name === '门店周报看板',
    JSON.stringify(state.derives))
  check('派生的 spec 原样建成应用', await waitFor(page, async () => state.creates.length === 1)
    && state.creates[0].spec?.pins?.[0]?.version === 6, JSON.stringify(state.creates[0]?.spec?.pins))
  await page.waitForURL('**/apps/app-new/edit/pages')
  check('建好跳进编辑器的页面一节', new URL(page.url()).pathname === '/apps/app-new/edit/pages')
  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))
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
  check('属性名写目录里的中文名，不露键名', await insp.locator('[data-app-prop="label"] label').innerText() === '名称'
    && await insp.locator('[data-app-prop="unit"] label').innerText() === '单位',
    `${await insp.locator('[data-app-prop="label"] label').innerText()} ｜ ${await insp.locator('[data-app-prop="unit"] label').innerText()}`)
  const unitHint = insp.locator('[data-app-prop="unit"] p')
  check('取值不直观的属性下面写目录给的说明，接到读屏', (await unitHint.innerText()).includes('绑定指标时数里已经带着')
    && await insp.locator('[data-app-prop="unit"] input').getAttribute('aria-describedby') === await unitHint.getAttribute('id'))
  const kinds = await insp.locator('[data-app-bind-kind] option').allInnerTexts()
  check('绑定种类只给目录登记的（指标、单元格）', kinds.includes('指标') && kinds.includes('单元格') && !kinds.includes('报告'), kinds.join(' | '))
  await insp.locator('[data-app-prop="unit"] input').fill('元')
  await page.locator('[data-app-component="warn"]').click()
  const vis = page.locator('[data-app-inspector="warn"] [data-app-visible]')
  check('可见性表达式照原样显示，可编辑', await vis.inputValue() === "run.tier != 'formal'")
  const tones = await page.locator('[data-app-inspector="warn"] [data-app-prop="tone"] option').allInnerTexts()
  check('枚举的选项写目录给的中文（x-enum-labels），不写 info / warn / error', tones.join(',') === '—,说明,提醒,错误', tones.join(' | '))
  await page.locator('[data-app-palette]').click()
  await page.locator('[data-app-palette-item="report"]').click()
  check('从目录添加组件，新组件选中', await page.locator('[data-app-inspector^="report"]').count() === 1)
  await page.locator('[data-app-save]').click()
  await waitFor(page, async () => state.patches.length === 1)
  const body = state.patches[0]?.draft?.pages?.[0]?.body ?? []
  check('保存的草稿里有改过的属性和新组件', JSON.stringify(body).includes('"unit":"元"') && JSON.stringify(body).includes('"type":"report"'),
    JSON.stringify(body).slice(0, 200))
})

await section('视图来源：每种来源写一句解释；新增时起名，删除先确认、写明用到它的组件、可以撤销', async () => {
  const { page, state } = await open()
  const panel = page.locator('[data-app-views]')
  await panel.locator('summary').click()
  const wk = panel.locator('[data-app-view="wk"]')
  check('最近封存写一句解释（不只放在悬停里）', (await wk.locator('[data-app-view-kind-hint="latest_sealed"]').innerText()).includes('不发起运行'))
  await wk.locator('select').first().selectOption('on_demand')
  check('换成按需：解释跟着换', await waitFor(page, async () => (await wk.locator('[data-app-view-kind-hint]').getAttribute('data-app-view-kind-hint')) === 'on_demand'
    && (await wk.locator('[data-app-view-kind-hint]').innerText()).includes('缓存时效')))
  await wk.locator('select').first().selectOption('latest_sealed')

  // 新增：起名；重名不让加（会盖掉已有的视图），不起名叫 vN
  const name = panel.locator('[data-app-view-name]')
  await name.fill('wk')
  check('重名：不让加，写明已有同名视图', await panel.locator('[data-app-view-add]').isDisabled()
    && (await panel.locator('[data-app-view-name-taken]').innerText()).includes('已有名为「wk」的视图'))
  await name.fill(' detail ')
  await panel.locator('[data-app-view-add]').click()
  check('起了名：按名字新增（去掉首尾空白），输入框清空', await panel.locator('[data-app-view="detail"]').count() === 1 && await name.inputValue() === '')
  await panel.locator('[data-app-view-add]').click()
  check('不起名：照旧叫 vN', await panel.locator('[data-app-view="v3"]').count() === 1,
    (await panel.locator('[data-app-view]').evaluateAll((els) => els.map((e) => e.getAttribute('data-app-view')))).join(','))

  // 删除：先确认，写明用到它的组件；取消不删
  await wk.getByRole('button', { name: '删除视图「wk」' }).click()
  const dlg = page.locator('[role="dialog"], [role="alertdialog"]').filter({ hasText: '删除视图「wk」？' }).first()
  await dlg.waitFor({ timeout: 4000 })
  const said = await dlg.innerText()
  check('确认框写明用到它的组件（绑定的、可见条件指它的）和后果', said.includes('指标块「销售额」') && said.includes('提示「warn」')
    && said.includes('失去数据来源'), said.replace(/\s+/g, ' '))
  await dlg.getByRole('button', { name: '取消' }).click()
  check('取消：视图还在', await panel.locator('[data-app-view="wk"]').count() === 1)
  await wk.getByRole('button', { name: '删除视图「wk」' }).click()
  await dlg.waitFor({ timeout: 4000 })
  await dlg.getByRole('button', { name: '删除视图' }).click()
  check('确认后删掉', await waitFor(page, async () => await panel.locator('[data-app-view="wk"]').count() === 0))
  const undo = page.locator('[data-toast]').filter({ hasText: '已删除视图「wk」' }).getByRole('button', { name: '撤销' })
  check('删完给一次撤销', await waitFor(page, async () => await undo.count() === 1))
  await undo.click()
  check('撤销：视图回来了，来源照旧', await waitFor(page, async () => await panel.locator('[data-app-view="wk"]').count() === 1)
    && await panel.locator('[data-app-view="wk"] select').first().inputValue() === 'latest_sealed')
  // 没人用的视图：确认框照实说没有组件用到
  await panel.locator('[data-app-view="detail"]').getByRole('button', { name: '删除视图「detail」' }).click()
  const idle = page.locator('[role="dialog"], [role="alertdialog"]').filter({ hasText: '删除视图「detail」？' }).first()
  await idle.waitFor({ timeout: 4000 })
  check('没人用的视图：写明没有组件用到', (await idle.innerText()).includes('没有组件用到这个视图'))
  await idle.getByRole('button', { name: '删除视图' }).click()
  await page.locator('[data-app-save]').click()
  await waitFor(page, async () => state.patches.length === 1)
  const views = Object.keys(state.patches[0]?.draft?.views ?? {}).sort()
  check('保存的草稿：wk 撤销回来了、detail 删了、v3 新增了', views.join(',') === 'v3,wk', views.join(','))
})

await section('固定版本：「固定版本」「处置」各写一句解释', async () => {
  const { page } = await open({ path: '/apps/app-ae/edit/pins' })
  const terms = page.locator('[data-app-terms]')
  check('固定版本：写明应用只按这一版发起、工作流发新版不自动换', (await terms.locator('[data-app-term="pin"]').innerText()).includes('不会自动换'))
  const disposition = await terms.locator('[data-app-term="disposition"]').innerText()
  check('处置：写明是升版前后怎么放在一起看、三种选法', ['升版前后', '按新口径重算', '两版并列', '前后不可比'].every((w) => disposition.includes(w)),
    disposition)
})

await section('检查：和发布同一套规则，点一条定位到组件', async () => {
  const { page, state } = await open({ path: '/apps/app-ae/edit/check' })
  check('打开就检查一次', await waitFor(page, async () => state.lints === 1))
  const issues = page.locator('[data-app-issue]')
  check('列出错误和提醒', await waitFor(page, async () => await issues.count() === 3))
  await page.locator('[data-app-issue="app.binding_target_missing"] [data-app-issue-locate]').click()
  await page.waitForURL('**/edit/pages')
  // 页面一节要等组件目录到了才画页面树和属性面板：等它出来再判，别读到骨架屏
  const shows = (sel) => page.locator(sel).waitFor({ timeout: 4000 }).then(async () => await page.locator(sel).count() === 1, () => false)
  check('点「定位」：跳到页面一节、选中那个组件（点号加下标的 path）', await shows('[data-app-inspector="gmv"]'))
  await page.goto(`${WEB}/apps/app-ae/edit/check`, { waitUntil: 'networkidle' })
  await page.locator('[data-app-issue="app.expr_arith"] [data-app-issue-locate]').click()
  await page.waitForURL('**/edit/pages')
  check('JSON 指针的 path 也认得', await shows('[data-app-inspector="warn"]'))
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

await section('助手对话：取或建这个应用的对话、请求带 conversation_id、每轮落一条，之前的轮次显示出来', async () => {
  const look = { op: 'look', id: 'L1', by: 'system', need: '固定的工作流版本', status: 'ok', elapsed_ms: 60,
    objects: [{ kind: 'workflow_version', named: ['周销售 v7'], missing: [] }] }
  const conversation = { id: 'conv-ae', kind: 'app', title: '', turns: [
    { id: 'old-1', seq: 1, question: '先前的问题：指标放最上面', answer: '', explanation: '把指标挪到了首页顶部', status: 'done', error: '',
      meta: { looks: [look] } }] }
  const stream = [look, { op: 'final', spec: clone(SPEC), issues: [], explanation: '调整了首页的顺序' }]
  const { page, state, errors } = await open({ stream, conversation })
  check('进编辑器取或建一次对话', await waitFor(page, async () => state.conversations === 1))
  const panel = page.locator('[data-app-assistant]')
  check('之前的轮次显示出来（问题）', await waitFor(page, async () => (await panel.innerText()).includes('先前的问题：指标放最上面')))
  check('……带着当时查看了什么', (await panel.innerText()).includes('按固定版本查看了 1 个工作流版本'))
  await page.locator('[data-app-assistant] textarea').fill('报告放到第二页')
  await page.keyboard.press('Enter')
  check('请求带上这段对话', await waitFor(page, async () => state.streams.length === 1) && state.streams[0].conversation_id === 'conv-ae',
    JSON.stringify(state.streams[0]?.conversation_id))
  check('这一轮落库：开一轮带问题', await waitFor(page, async () => state.turns.length === 1) && state.turns[0].question === '报告放到第二页',
    JSON.stringify(state.turns))
  check('……再落说明和查看记录', await waitFor(page, async () => state.turnPatches.length === 1)
    && state.turnPatches[0].explanation === '调整了首页的顺序' && state.turnPatches[0].meta?.looks?.length === 1
    && state.turnPatches[0].status === 'done', JSON.stringify(state.turnPatches[0]))
  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))
})

await section('钩子：按名字列出声明和密钥状态；生成的密钥只显示一次', async () => {
  const spec = { ...clone(SPEC), hooks: [{ name: 'paid', kind: 'signal', entry: null }] }
  // 形状同服务端 process/hooks.hook_out：声明在草稿 / 发布版里没有、密钥生成过没有、收请求的地址
  const hooks = [
    { id: null, name: 'paid', kind: 'signal', entry_id: null, enabled: false, has_secret: false, in_draft: true, in_published: false,
      url_path: null, created_at: null },
    { id: 'h-old', name: 'old', kind: 'start', entry_id: 'apply', enabled: true, has_secret: true, in_draft: false, in_published: false,
      url_path: '/api/hooks/h-old', created_at: '2026-10-01T02:00:00Z' },
  ]
  const { page, state } = await open({ path: '/apps/app-ae/edit/hooks', spec, hooks })
  const paid = page.locator('[data-app-hook="paid"]')
  check('声明的钩子标出还没生成密钥、发布版里还没有', await waitFor(page, async () => await paid.locator('[data-app-hook-state="none"]').count() === 1)
    && (await paid.innerText()).includes('发布版里还没有'))
  check('草稿里没声明、但生成过密钥的单独列出', await page.locator('[data-app-hook="old"][data-app-hook-undeclared]').count() === 1)
  await paid.locator('[data-app-hook-rotate="paid"]').click()
  check('生成密钥按名字发', await waitFor(page, async () => state.rotates.length === 1) && state.rotates[0] === 'paid', JSON.stringify(state.rotates))
  const shown = page.locator('[data-app-hook-secret="paid"]')
  check('密钥只显示这一次：值、复制、说明', await waitFor(page, async () => await shown.count() === 1)
    && (await shown.locator('[data-app-hook-secret-value]').innerText()).includes('secret-of-paid-0123456789')
    && (await shown.innerText()).includes('只显示这一次') && await shown.getByRole('button', { name: '复制' }).count() >= 1)
  check('……附入站地址', (await shown.locator('[data-app-hook-url]').innerText()).endsWith('/api/hooks/h-paid'))
  await page.locator('[data-app-hook-delete="old"]').click()
  await page.locator('[role="dialog"]').getByRole('button', { name: '删除密钥' }).click()
  check('删除密钥按名字发', await waitFor(page, async () => state.hookDeletes.length === 1) && state.hookDeletes[0] === 'old')
  await page.locator('[data-app-nav="pages"]').click()
  await page.locator('[data-app-nav="hooks"]').click()
  check('离开这一节再回来：密钥不再显示', await page.locator('[data-app-hook-secret]').count() === 0)
})

await section('入口与表单：JSON 编辑、格式校验、检查的 path 定位到那一行', async () => {
  const spec = { ...clone(SPEC),
    entries: [{ id: 'apply', label: '发起', pin: 'weekly', form: { $ref: '#/forms/apply' }, map: { amount: 'form.amount' } }],
    forms: { apply: { format: 'agentlab.form/1', type: 'object', required: ['amount'],
      properties: { amount: { type: 'number', title: '金额', minimum: 0 }, note: { type: 'string', title: '说明' } } } } }
  const lint = [{ level: 'error', code: 'app.entry_form_mismatch', message: '入口表单和固定版本的入口字段对不上',
    path: 'forms.apply.properties.note' }]
  const { page, state } = await open({ path: '/apps/app-ae/edit/forms', spec, lint })
  const entries = page.locator('[data-app-json="entries"]')
  await entries.locator('textarea').fill('[{"id": "apply",')
  check('JSON 格式不对：照实说，不能应用', await entries.locator('[data-app-json-error]').count() === 1
    && await entries.locator('[data-app-json-apply]').isDisabled())
  await entries.locator('textarea').fill(JSON.stringify([{ id: 'apply', label: '发起', pin: 'nope', form: { $ref: '#/forms/apply' }, map: {} }]))
  check('形状检查：固定版本的别名不存在', (await entries.locator('[data-app-json-issues]').innerText()).includes('固定版本「nope」不存在'))
  await entries.locator('[data-app-json-reset]').click()
  const forms = page.locator('[data-app-json="forms"]')
  const next = clone(spec.forms)
  next.apply.properties.amount.maximum = 10000
  await forms.locator('textarea').fill(JSON.stringify(next, null, 2))
  await forms.locator('[data-app-json-apply]').click()
  await page.locator('[data-app-save]').click()
  check('应用到草稿后保存：表单的数据约束跟着存', await waitFor(page, async () => state.patches.length === 1)
    && state.patches[0].draft?.forms?.apply?.properties?.amount?.maximum === 10000, JSON.stringify(state.patches[0]?.draft?.forms))
  await page.locator('[data-app-nav="check"]').click()
  await page.locator('[data-app-issue="app.entry_form_mismatch"] [data-app-issue-locate]').click()
  await page.waitForURL('**/edit/forms')
  const located = await waitFor(page, async () => Number(await page.locator('[data-app-json="forms"]').getAttribute('data-app-json-located')) > 0)
  check('检查的 path 落在表单上：跳到入口与表单，在 JSON 里定位到那个字段', located,
    await page.locator('[data-app-json="forms"]').getAttribute('data-app-json-located'))
})

await section('按新口径补算：服务端给了能补算的视图，确认框写明会发起几次、跳过几次', async () => {
  const spec = { ...clone(SPEC), pins: [{ ...SPEC.pins[0], version: 6 }], pages: [{ id: 'home', title: '本周概览', body: [
    { id: 'trend', type: 'metric', props: {}, bind: { view: 'wk', series: 'kpi.gmv', last: 8 } }] }] }
  // 形状同服务端 apps/backfill.Plan.out：应用详情的 backfill 列出能补算的视图，GET …/backfill 是最新的计划
  const planned = { view: 'wk', alias: 'weekly', source: 'formal', from_version: 5, to_version: 6, periods: 8, will_launch: 6, will_skip: 2 }
  const { page, state } = await open({ spec, backfill: [planned], plan: planned })
  const btn = page.locator('[data-app-backfill="wk"]')
  check('视图来源下面给「按新口径补算历史」', await waitFor(page, async () => await btn.count() === 1))
  check('次数照服务端算好的', await btn.getAttribute('data-app-backfill-max') === '6')
  await btn.click()
  const dlg = page.locator('[role="dialog"]')
  await dlg.waitFor({ timeout: 4000 })
  check('确认框写明会发起几次、跳过几次、从哪一版到哪一版', (await dlg.innerText()).includes('将发起 6 次正式运行')
    && (await dlg.innerText()).includes('2 次的输入已经补算过') && (await dlg.innerText()).includes('v5') && (await dlg.innerText()).includes('v6'))
  await dlg.getByRole('button', { name: '发起 6 次运行' }).click()
  check('按视图发起补算', await waitFor(page, async () => state.backfills.length === 1) && state.backfills[0] === 'wk', JSON.stringify(state.backfills))
  check('提示发起了几次、跳过几次', await waitFor(page, async () => (await page.locator('body').innerText()).includes('已发起 7 次补算，跳过 1 次')))
  await page.goto(`${WEB}/apps/app-ae`, { waitUntil: 'networkidle' })
  check('应用页（管理）也给补算入口', await waitFor(page, async () => await page.locator('[data-app-backfill-panel] [data-app-backfill="wk"]').count() === 1))
})

await section('按新口径补算：老服务端没给计划时按版本列表推，写明最多几次', async () => {
  const spec = { ...clone(SPEC), pins: [{ ...SPEC.pins[0], version: 6 }], pages: [{ id: 'home', title: '本周概览', body: [
    { id: 'trend', type: 'metric', props: {}, bind: { view: 'wk', series: 'kpi.gmv', last: 8 } }] }] }
  const versions = [
    { version: 2, spec_hash: 'h2', level: 'published', pins: [{ ...SPEC.pins[0], version: 6 }], dispositions: { weekly: 'recompute' },
      note: '', published_by: '检查脚本', published_at: '2026-10-12T02:00:00Z' },
    { version: 1, spec_hash: 'h1', level: 'published', pins: [{ ...SPEC.pins[0], version: 5 }], dispositions: {},
      note: '', published_by: '检查脚本', published_at: '2026-10-01T02:00:00Z' },
  ]
  const { page, state } = await open({ spec, versions })
  const btn = page.locator('[data-app-backfill="wk"]')
  check('视图来源下面给「按新口径补算历史」', await waitFor(page, async () => await btn.count() === 1))
  check('上限按趋势取的最近几次', await btn.getAttribute('data-app-backfill-max') === '8')
  await btn.click()
  const dlg = page.locator('[role="dialog"]')
  await dlg.waitFor({ timeout: 4000 })
  check('确认框写明最多发起几次运行', (await dlg.innerText()).includes('最多发起 8 次正式运行'))
  await dlg.getByRole('button', { name: '发起最多 8 次运行' }).click()
  check('按视图发起补算', await waitFor(page, async () => state.backfills.length === 1))
})

await section('按新口径补算：处置不是按新口径重算时不给按钮', async () => {
  const versions = [
    { version: 2, spec_hash: 'h2', level: 'published', pins: [{ ...SPEC.pins[0], version: 6 }], dispositions: { weekly: 'dual' },
      note: '', published_by: null, published_at: '2026-10-12T02:00:00Z' },
    { version: 1, spec_hash: 'h1', level: 'published', pins: [{ ...SPEC.pins[0], version: 5 }], dispositions: {},
      note: '', published_by: null, published_at: '2026-10-01T02:00:00Z' },
  ]
  const { page } = await open({ versions })
  await page.waitForTimeout(500)
  check('两版并列：没有补算按钮', await page.locator('[data-app-backfill]').count() === 0)
})

// 授权三行长得一样，不写标题分不出哪一行是哪一档；搭建者读不到 /users、/groups（只给管理员），选人走只读的名录
// （/api/directory），填错的名字当场标出来，不静默存进授权
await section('岗位与授权：三档各写标题和说明；搭建者从名录选人，名录里没有的不让加、已选的标出来', async () => {
  const { page } = await open({ path: '/apps/app-ae/edit/access' })
  const asked = []
  await page.route(/\/api\/auth\/me$/, (route) => route.fulfill({ json: {
    mode: 'local', username: 'lisi', display_name: '李四', role: 'builder', groups: [], verified: true } }))
  await page.route(/\/api\/directory$/, (route) => {
    asked.push(1)
    return route.fulfill({ json: { users: [{ username: 'zhangsan', display_name: '张三', disabled: false },
      { username: 'wangwu', display_name: '王五', disabled: true }], groups: [{ name: 'fin', label: '财务' }] } })
  })
  await page.route(/\/api\/(users|groups)$/, (route) => route.fulfill({ status: 403, json: { detail: '没有权限' } }))
  await page.route(/\/api\/apps\/app-ae\/grants$/, (route) => (route.request().method() === 'GET'
    ? route.fulfill({ json: { items: [{ principal_kind: 'user', principal: 'ghost', role: 'use' }] } }) : route.fallback()))
  await page.reload({ waitUntil: 'networkidle' })
  const rows = page.locator('[data-app-grant-role]')
  await rows.first().waitFor({ timeout: 8000 })
  const titles = await page.locator('[data-app-grant-title]').allInnerTexts()
  check('三行写明使用 / 查看全部实例 / 管理', titles.join(',') === '使用,查看全部实例,管理', titles.join(','))
  check('每行带一句管什么的说明', (await rows.nth(0).innerText()).includes('派给自己的待办')
    && (await rows.nth(1).innerText()).includes('全部实例') && (await rows.nth(2).innerText()).includes('编辑、发布'))
  const use = rows.first()
  check('已选的名录里没有：标签上标出来', await waitFor(page, async () => await use.locator('[data-principal-problem="missing"]').count() === 1))
  const input = use.getByRole('textbox')
  const chips = () => use.locator('li[data-principal]').count()
  await input.fill('nobody')
  await input.press('Enter')
  check('名录里没有的人不让加，说清是谁', (await use.locator('[data-principal-refused="hard"]').innerText().catch(() => '')).includes('nobody')
    && await chips() === 1)
  await input.fill('wangwu')
  await input.press('Enter')
  check('停用的人不让加', (await use.locator('[data-principal-refused]').innerText().catch(() => '')).includes('已停用') && await chips() === 1)
  const options = await use.locator('datalist option').evaluateAll((els) => els.map((e) => e.value))
  check('候选来自名录，停用的不列', options.join(',') === 'zhangsan', options.join(','))
  await input.fill('zhangsan')
  await input.press('Enter')
  check('名录里有的照常加上', await chips() === 2 && await use.locator('[data-principal-refused]').count() === 0)
  check('名录只取一次，几个选择器共用', asked.length === 1, String(asked.length))
})

check('检查没有写库（兜底拦下的写请求）', writes.length === 0, writes.slice(0, 4).join('、'))
console.log(failed ? `\n${failed} 项未通过` : '\n全部通过')
await browser.close()
process.exit(failed ? 1 : 0)
