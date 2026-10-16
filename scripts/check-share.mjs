// 分享链接的检查（docs/design/next-round/04-对外集成与触达.md C2）：/s 分享页和应用编辑里的分享管理。
//
// 守的是：
//   - /s 不进外壳：没有导航，不认人、不取目录、不连心跳——整页只发一个请求 GET /api/shares/page；
//   - 分享 key 从链接的 # 后面读，放进请求头 X-AgentLab-Share，地址里不带；
//   - 用同一个视图渲染器画：指标照常显示；待办、发起按钮这些服务端换成的说明块照画，没有待办卡、没有发起按钮；
//   - 印章常显：「数据的来历」里逐个列出视图的印章（页面上没放印章组件也列）；
//   - 页脚写「只读 · 由 X 于 … 分享 · … 到期」；
//   - 点数字不开在线证据，说明出处在下载的报告里；下载报告带请求头、只取这条分享解析出来的运行；
//   - 撤销、到期、链接不完整：照服务端的话写明（「链接已撤销」……），不给空白页；
//   - 这条分享没允许嵌入、页面又在别的页面的 iframe 里：不显示内容，给「在新窗口打开」；允许嵌入的照常显示；
//   - 应用编辑 → 岗位与授权：管理员没开开关时不给新建、写明去哪开；开了之后建分享（页面、数据、有效期、嵌入），链接只显示
//     这一次；列表写状态、打开次数；撤销先确认。
//
// 不写库：/api/shares/*、/api/apps/* 全部伪造。其余写请求一律 409 并记账，检查结束时必须是空的。
//
// 跑之前前端得起着（./scripts/dev.sh），默认连 5273：
//   AGENTLAB_WEB=http://localhost:<前端端口> node scripts/check-share.mjs
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const CHROME = process.env.CHROME_PATH ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const ONLY = (process.env.SHARE_ONLY ?? '').split(',').filter(Boolean)

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
const waitFor = async (page, fn, ms = 5000) => {
  for (let t = 0; t < ms; t += 100) {
    if (await fn().catch(() => false)) return true
    await page.waitForTimeout(100)
  }
  return false
}
const clone = (v) => JSON.parse(JSON.stringify(v))
const writes = []
// 只拦接口：按路径的开头认（/src/api/client.ts 这类前端模块的地址里也有「/api/」，不能按正则拦）
const API = (url) => url.pathname.startsWith('/api/')

// ---------------------------------------------------------------- 夹具（通用示例名）

const KEY = 'Sh4reKeyExampleOnly_0123456789abcdefGHIJ'
const STAMP = {
  as_of: '2026-10-15T01:00:00Z', as_of_text: '截至 2026-10-15 09:00；统计期 week 2026-W41', pin: '周报 v3', graph_hash: 'ab12cd34',
  calibers: [{ name: '周报口径', version: 'v3' }], tier: 'formal', tier_text: '完整出具', from: '定时任务「周一出数」第 6 次', sealed: true,
}
const VIEW = {
  format: 'agentlab.view/1',
  app: { id: 'app-share', name: '门店周报', version: 3, level: 'published', needs_upgrade: false },
  page: { id: 'home', title: '本周概览' },
  pages: [{ id: 'home', title: '本周概览' }],
  sources: {
    wk: { state: 'ok', kind: 'latest_sealed', run_id: 'run-share-1', stamp: STAMP },
    ask: { state: 'empty', kind: 'on_demand', run_id: null, stamp: null, message: '分享链接只显示定时出数的视图：这一块的数要打开应用才看得到' },
  },
  tree: [
    { id: 'gmv', type: 'metric', props: { label: '销售额' }, data: { state: 'ok', text: '18,218.54元', name: '销售额', unit: '元',
      evidence: { run_id: 'run-share-1', kind: 'metric', node: 'card', metric: 'gmv', eid: 'e1' } } },
    { id: 'asked', type: 'metric', props: { label: '按条件查看' }, data: { state: 'empty',
      message: '分享链接只显示定时出数的视图：这一块的数要打开应用才看得到' } },
    { id: 'todo', type: 'notice', props: { text: '分享链接不列待办：待办只给派给的人，请打开应用处理', tone: 'info' },
      share_placeholder: 'task_list' },
    { id: 'go', type: 'notice', props: { text: '分享链接是只读的，不能在这里发起', tone: 'info' }, share_placeholder: 'entry_button' },
  ],
  forms: {}, problems: [],
}
const PAYLOAD = {
  format: 'agentlab.share/1',
  share: { mode: 'latest', created_by: '编辑甲', created_at: '2026-10-14T02:00:00Z', expires_at: '2026-11-13T02:00:00Z', embed_allowed: false },
  view: VIEW,
  downloads: [{ view: 'wk', run_id: 'run-share-1', reports: ['write'], as_of_text: STAMP.as_of_text }],
}

async function openShare({ hash = `#${KEY}`, payload = PAYLOAD, status = 200, error = null } = {}) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 }, acceptDownloads: true })
  opened.add(ctx)
  const page = await ctx.newPage()
  page.setDefaultTimeout(8000)
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  const state = { api: [], pages: [], reports: [] }
  page.on('request', (req) => { if (new URL(req.url()).pathname.startsWith('/api/')) state.api.push(new URL(req.url()).pathname) })
  const json = (route, body, code = 200) => route.fulfill({ status: code, contentType: 'application/json', body: JSON.stringify(body) })
  await page.route(API, (route) => {
    const req = route.request()
    if (req.method() === 'GET') return json(route, { detail: '分享页不该请求这个接口' }, 418)
    writes.push(`${req.method()} ${new URL(req.url()).pathname}`)
    return json(route, { detail: '检查脚本不写库' }, 409)
  })
  await page.route(/\/api\/shares\/page(\?.*)?$/, (route) => {
    const req = route.request()
    state.pages.push({ url: req.url(), header: req.headers()['x-agentlab-share'] ?? null })
    return error ? json(route, error, status) : json(route, payload)
  })
  await page.route(/\/api\/shares\/report\.html(\?.*)?$/, (route) => {
    const req = route.request()
    state.reports.push({ url: req.url(), header: req.headers()['x-agentlab-share'] ?? null })
    return route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: '<!doctype html><title>报告</title>',
      headers: { 'Content-Disposition': "attachment; filename*=UTF-8''%E5%91%A8%E6%8A%A5.html" } })
  })
  await page.goto(`${WEB}/s${hash}`, { waitUntil: 'networkidle' })
  return { ctx, page, state, errors }
}

const browser = await chromium.launch({ executablePath: CHROME })

// ---------------------------------------------------------------- 分享页

await section('分享页：不进外壳，key 只放在请求头里', async () => {
  const { page, state, errors } = await openShare()
  check('页面画出来了', await waitFor(page, async () => await page.locator('[data-share-page="ok"]').count() === 1))
  check('没有外壳的导航', await page.locator('nav').count() === 0 && await page.locator('[data-nav-item]').count() === 0)
  check('只请求了分享页一个接口（不认人、不取目录、不连心跳）',
    state.api.length >= 1 && state.api.every((p) => p === '/api/shares/page'), state.api.join('、'))
  const [first] = state.pages
  check('key 在请求头 X-AgentLab-Share 里', first?.header === KEY)
  check('请求地址里不带 key', !!first && !first.url.includes(KEY))
  check('页面上没有报错', errors.length === 0, errors.join('；'))
})

await section('分享页：视图照画，流程组件是说明，印章常显，页脚', async () => {
  const { page } = await openShare()
  await waitFor(page, async () => await page.locator('[data-share-page="ok"]').count() === 1)
  check('定时出数的指标照常显示', (await page.locator('[data-node="gmv"]').innerText()).includes('18,218.54'))
  check('按需视图写明只显示定时出数的', (await page.locator('[data-node="asked"]').innerText()).includes('只显示定时出数的视图'))
  check('没有待办卡、没有发起按钮', await page.locator('[data-component="task_list"], [data-entry-button], [data-task-card]').count() === 0)
  check('换成的说明块照画', (await page.locator('[data-node="todo"]').innerText()).includes('不列待办')
    && (await page.locator('[data-node="go"]').innerText()).includes('只读'))
  check('只读的标记', (await page.locator('[data-share-readonly]').innerText()).includes('只读'))
  check('印章常显：页面上没放印章组件也列出来', await page.locator('[data-share-stamps] [data-stamp]').count() === 1
    && (await page.locator('[data-share-stamps]').innerText()).includes('完整出具'))
  const footer = await page.locator('[data-share-footer-text]').innerText()
  check('页脚写谁分享、什么时候、什么时候到期', /^只读 · 由 编辑甲 于 .+ 分享 · .+ 到期$/.test(footer.trim()), footer)
  check('没有刷新、提交这些操作', await page.locator('[data-view-refresh], [data-form-submit]').count() === 0)
})

await section('分享页：点数字说明出处在报告里，下载报告带请求头', async () => {
  const { page, state } = await openShare()
  await waitFor(page, async () => await page.locator('[data-share-page="ok"]').count() === 1)
  await page.locator('[data-node="gmv"] [data-evidence="metric"]').click()
  check('不开在线证据，说明出处在下载的报告里',
    await waitFor(page, async () => await page.locator('[data-share-evidence-note]').count() === 1)
    && (await page.locator('[data-share-evidence-note]').innerText()).includes('下载报告'))
  check('没有去取证据接口', !state.api.some((p) => p.includes('/evidence')))
  await page.keyboard.press('Escape')
  const [download] = await Promise.all([
    page.waitForEvent('download', { timeout: 5000 }).catch(() => null),
    page.locator('[data-share-footer] [data-share-download="run-share-1"]').click(),
  ])
  check('下载了报告文件，用服务端给的文件名', !!download && download.suggestedFilename() === '周报.html', download?.suggestedFilename())
  const [req] = state.reports
  check('下载报告带请求头、点名这次分享的运行', req?.header === KEY && req.url.includes('run=run-share-1')
    && req.url.includes('report=write') && !req.url.includes(KEY))
})

await section('分享页：撤销、到期、链接不完整都写明', async () => {
  for (const [code, title, detail] of [
    ['share_revoked', '链接已撤销', '链接已撤销（2026-10-15 10:00）：请向分享的人要新的链接'],
    ['share_expired', '链接已到期', '链接已到期（2026-10-15 10:00）：请向分享的人要新的链接'],
  ]) {
    const { page } = await openShare({ status: 410, error: { detail, code } })
    const box = page.locator(`[data-share-error="${code}"]`)
    check(`${title}：标题和服务端的原话`, await waitFor(page, async () => await box.count() === 1)
      && (await box.innerText()).includes(title) && (await box.innerText()).includes('请向分享的人要新的链接'))
  }
  const { page, state } = await openShare({ hash: '' })
  check('链接不完整（没有 # 后面那段）：写明，不发请求',
    await waitFor(page, async () => await page.locator('[data-share-error="share_key_missing"]').count() === 1)
    && state.pages.length === 0)
})

await section('分享页：没允许嵌入的不在别的页面里显示，允许的照常', async () => {
  for (const allowed of [false, true]) {
    const payload = clone(PAYLOAD)
    payload.share.embed_allowed = allowed
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } })
    opened.add(ctx)
    const page = await ctx.newPage()
    await page.route(/\/api\/shares\/page(\?.*)?$/, (route) => route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify(payload) }))
    await page.route((url) => API(url) && !url.pathname.startsWith('/api/shares/'), (route) => route.fulfill({ status: 418, body: '' }))
    await page.setContent(`<!doctype html><iframe id="f" src="${WEB}/s#${KEY}" style="width:1000px;height:700px"></iframe>`)
    const frame = await (async () => {
      for (let i = 0; i < 50; i++) {
        const f = page.frames().find((x) => x.url().includes('/s'))
        if (f) return f
        await page.waitForTimeout(100)
      }
      return null
    })()
    const want = allowed ? 'ok' : 'framed'
    const ok = !!frame && await waitFor(page, async () => await frame.locator(`[data-share-page="${want}"]`).count() === 1)
    check(allowed ? '允许嵌入：iframe 里照常显示' : '没允许嵌入：iframe 里不显示内容，给在新窗口打开', ok)
    if (!allowed && frame) check('嵌着时没有画视图', await frame.locator('[data-view-renderer]').count() === 0)
  }
})

// ---------------------------------------------------------------- 应用编辑里的分享管理

const SPEC = {
  format: 'agentlab.app/1', name: '门店周报', description: '', icon: 'app',
  pins: [{ alias: 'weekly', workflow_id: 'wf-share', version: 3, graph_hash: 'hash3', level: 'published', use: 'view' }],
  slots: {}, entries: [], forms: {}, task_ui: {},
  views: { wk: { source: { kind: 'latest_sealed', pin: 'weekly', from: 'formal' } } },
  pages: [{ id: 'home', title: '本周概览', body: [] }], hooks: [], notify: [],
}
const shareRow = (over = {}) => ({
  id: 'sh1', prefix: 'AbC123', app_id: 'app-share', page_id: 'home', page_title: '本周概览', mode: 'latest', app_version: 3,
  embed_allowed: false, created_by: 'root', created_by_name: '管理员', created_at: '2026-10-14T02:00:00Z',
  expires_at: '2026-11-13T02:00:00Z', revoked_at: null, revoked_by: null, open_count: 4, last_opened_at: '2026-10-15T02:00:00Z',
  state: 'active', ...over,
})

async function openEditor({ enabled = true, items = [shareRow()] } = {}) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  opened.add(ctx)
  const page = await ctx.newPage()
  page.setDefaultTimeout(8000)
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  const json = (route, body, code = 200) => route.fulfill({ status: code, contentType: 'application/json', body: JSON.stringify(body) })
  const state = { items: clone(items), creates: [], revokes: [] }
  const app = { id: 'app-share', name: SPEC.name, description: '', icon: 'app', status: 'active', published_version: 3,
    level: 'published', needs_upgrade: false, pending_tasks: 0, my_role: 'manage', updated_at: '2026-10-10T02:00:00Z',
    draft: clone(SPEC), draft_bindings: { slots: {} }, created_by: 'root', scope: 'shared',
    published: { version: 3, pages: [{ id: 'home', title: '本周概览' }], entries: [] } }
  await page.route(/\/api\//, (route) => {
    const req = route.request()
    if (req.method() === 'GET') return route.continue()
    writes.push(`${req.method()} ${new URL(req.url()).pathname}`)
    return json(route, { detail: '检查脚本不写库' }, 409)
  })
  await page.route(/\/api\/apps\/catalog$/, (route) => json(route, { version: 1, components: [] }))
  await page.route(/\/api\/apps\/app-share(\/.*)?(\?.*)?$/, (route) => {
    const req = route.request()
    const parts = new URL(req.url()).pathname.split('/')
    const sub = parts[4]
    if (!sub) return json(route, app)
    if (sub === 'grants') return json(route, { items: [] })
    if (sub === 'assistant-conversation') return json(route, { id: 'conv-share', kind: 'app', title: '', turns: [] })
    if (sub === 'shares') {
      if (req.method() === 'GET') return json(route, { enabled, embed_origins: false, max_days: 90, items: state.items })
      if (req.method() === 'POST') {
        const body = req.postDataJSON()
        state.creates.push(body)
        const row = shareRow({ id: 'sh2', prefix: 'NeW456', mode: body.mode, embed_allowed: body.embed_allowed, open_count: 0,
          expires_at: body.expires_at })
        state.items = [row, ...state.items]
        return json(route, { ...row, key: 'NeW456-secret-share-key', path: '/s#NeW456-secret-share-key', url: null }, 201)
      }
      if (req.method() === 'DELETE') {
        state.revokes.push(parts[5])
        state.items = state.items.map((r) => (r.id === parts[5] ? { ...r, state: 'revoked', revoked_at: '2026-10-16T02:00:00Z' } : r))
        return json(route, state.items.find((r) => r.id === parts[5]))
      }
    }
    return route.fallback()
  })
  await page.goto(`${WEB}/apps/app-share/edit/access`, { waitUntil: 'networkidle' })
  return { page, state, errors }
}

await section('应用编辑：管理员没开开关时不给新建，写明去哪开', async () => {
  const { page } = await openEditor({ enabled: false, items: [] })
  const box = page.locator('[data-app-shares="off"]')
  check('写明管理员没开、去哪开', await waitFor(page, async () => await box.count() === 1)
    && (await box.innerText()).includes('允许建分享链接'))
  check('没有新建的入口', await page.locator('[data-app-share-create]').count() === 0)
})

await section('应用编辑：建分享链接，链接只显示一次；列表与撤销', async () => {
  const { page, state, errors } = await openEditor()
  const row = page.locator('[data-app-share="AbC123"]')
  check('已有的分享列出状态、打开次数、建的人', await waitFor(page, async () => await row.count() === 1)
    && (await row.innerText()).includes('有效') && (await row.innerText()).includes('打开过 4 次')
    && (await row.innerText()).includes('由 管理员 建'))
  await page.locator('[data-app-share-create]').click()
  check('表单：选页面、数据、有效期、嵌入', await page.locator('[data-app-share-form]').count() === 1
    && await page.locator('[data-app-share-days] option').count() === 4)
  await page.locator('[data-app-share-mode="fixed"]').check()
  await page.locator('[data-app-share-days]').selectOption('7')
  await page.locator('[data-app-share-submit]').click()
  check('建好后链接只显示这一次', await waitFor(page, async () => await page.locator('[data-app-share-once]').count() === 1)
    && (await page.locator('[data-app-share-link]').innerText()).endsWith('/s#NeW456-secret-share-key')
    && (await page.locator('[data-app-share-once]').innerText()).includes('只显示这一次'))
  const [body] = state.creates
  const days = body ? (new Date(body.expires_at).getTime() - Date.now()) / 86_400_000 : 0
  check('交给服务端的：页面、固定这一期、7 天后到期、不允许嵌入', body?.page_id === 'home' && body.mode === 'fixed'
    && days > 6.9 && days < 7.1 && body.embed_allowed === false, JSON.stringify(body))
  await page.locator('[data-app-share-dismiss]').click()
  check('关掉之后链接就没有了', await page.locator('[data-app-share-link]').count() === 0
    && await page.locator('[data-app-share="NeW456"]').count() === 1)
  await page.locator('[data-app-share-revoke="AbC123"]').click()
  const dialog = page.getByRole('alertdialog').or(page.getByRole('dialog'))
  check('撤销先确认，写明后果', await waitFor(page, async () => (await dialog.innerText()).includes('链接已撤销')))
  await dialog.getByRole('button', { name: '撤销' }).click()
  check('撤销了', await waitFor(page, async () => state.revokes.includes('sh1'))
    && await waitFor(page, async () => await page.locator('[data-app-share="AbC123"][data-app-share-state="revoked"]').count() === 1))
  check('页面上没有报错', errors.length === 0, errors.join('；'))
})

check('检查没有写库（兜底拦下的写请求）', writes.length === 0, writes.slice(0, 4).join('、'))
console.log(failed ? `\n${failed} 项未通过` : '\n全部通过')
await browser.close()
process.exit(failed ? 1 : 0)
