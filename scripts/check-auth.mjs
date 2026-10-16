// 登录与按角色收起的外壳（/login、导航、账户菜单、401 跳登录、防跨站请求头）的回归检查。
//
// 守的是这些「看着没坏、其实不对」的地方：
//   - 开了登录、没登录：任何页面都送到 /login?next=原处，登录成功后回到原处；登录页按服务端说的登录方式给账号密码表单
//     或单点登录按钮（单点登录时本地账号收在下面）；密码不对写人话；单点登录回调失败带回的原因照写；next 只认站内路径；
//     进门只认一次人（/auth/me 只请求一次：认人接口的 401 不整页跳，开发模式下 effect 执行两次也共用一个请求）；
//   - 账号密码登录的新部署（/auth/me 的 401 带 can_bootstrap）：登录页给「建第一个管理员」，确认密码对不上不让提交，
//     建好即登录、回到原处；已有账号的人可以展开登录表单；
//   - 会话中途失效：任何接口回 401 都整页跳登录页，带上原处；
//   - 非 GET 请求都带 X-AgentLab-Request 头（服务端开登录时据此防跨站）；
//   - 使用者（user 角色）：导航只有「应用」和「设置」，别的地址送回应用首页，⌥ 数字切不到看不见的页；没有署名框，
//     账户菜单写显示名、角色、退出登录；待办卡写「以登录身份处理」；
//   - 搭建者：导航十项都在（第 4 波加了「评测」，搭建者以上可见），账户菜单写搭建者；
//   - 关登录：/login 直接回首页；导航底部是署名入口，没有账户菜单；应用首页挂「未开启登录」的说明；
//   - 管理员允许使用者问数据时（D-c）：问数据页只取数据源的名字（/api/datasources/names），不取完整列表和表结构；
//   - 使用者的设置页只剩偏好设置（主题）和通知，换主题只存本机、不写服务端（D-s）；
//   - 管理员的「使用者可以建个人应用」开关（认人接口的 user_personal_apps，docs/design/normal-user-apps.md N7）：关着时使用者
//     没有「新建应用」、新建页写明管理员还没开放、进不了蓝图页；开着时有「新建应用」（建的是个人应用），自己的个人应用单列
//     一组「我的应用」，能进自己的蓝图页。
//
// 身份接口在浏览器层答掉（/api/auth/**），按 state 切登录方式和角色；其余 GET 放行（外壳要它），写请求拦下记在探针里。
// 编排者的完整关口另起一套开着 local 登录的服务跑真接口，见 docs/design §6.3。
//
//   node scripts/check-auth.mjs
// 对别的实例跑时带上地址：AGENTLAB_WEB=http://localhost:<前端端口> node scripts/check-auth.mjs
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const CHROME = process.env.CHROME_PATH ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'

let failed = 0
const check = (name, cond, detail = '') => {
  console.log(`  ${cond ? '✓' : '✗'} ${name}${detail ? ` — ${detail}` : ''}`)
  if (!cond) failed++
}
const ONLY = (process.env.CHECK_ONLY ?? '').split(',').map((s) => s.trim()).filter(Boolean)
const opened = new Set()
async function section(name, fn) {
  if (ONLY.length && !ONLY.some((k) => name.includes(k))) return
  console.log(`\n=== ${name} ===`)
  try {
    await fn()
  } catch (e) {
    check(`${name} 中途出错`, false, String(e?.message ?? e).split('\n')[0])
  } finally {
    for (const c of opened) await c.close().catch(() => {})
    opened.clear()
  }
}

const PEOPLE = {
  user: { username: 'zhangsan', display_name: '张三', role: 'user', groups: ['财务'] },
  builder: { username: 'lisi', display_name: '李四', role: 'builder', groups: [] },
  admin: { username: 'root', display_name: '管理员甲', role: 'admin', groups: [] },
}

const TASK = {
  id: 'task_x', approval_id: 'ap_x', run_id: 'run_x', node_id: 'finance', app_id: 'app_claim', app_name: '报销审批',
  app_version: 2, workflow_name: '报销流程', kind: 'approve', title: '财务审批', form: null, policy: { kind: 'any' },
  status: 'pending', due_at: null, escalation_level: 0, payload: { kind: 'human_node', message: '请审批' },
  assignments: [{ id: 'a1', principal_kind: 'user', principal: 'zhangsan', source: 'slot:finance', status: 'pending',
    responded_by: null, responded_at: null, decision: null, verified: false }],
  can_respond: true, outcome: null, resolved_by: null, resolved_at: null, created_at: new Date().toISOString(),
}

// 使用者自己建的个人应用和它的蓝图（形状同服务端 api/apps._summary、blueprints/store.blueprint_out）
const ago = (ms) => new Date(Date.now() - ms).toISOString()
const MY_APP = {
  id: 'app_mine', name: '我的退款周报', description: '每周看自己门店的退款', icon: 'app', status: 'active', published_version: null,
  level: null, needs_upgrade: false, pending_tasks: 0, my_role: 'manage', created_at: ago(86_400_000), updated_at: ago(600_000),
  scope: 'personal', owner: 'zhangsan', blueprint_id: 'bp_mine',
}
const SHARED_APP = {
  id: 'app_weekly', name: '门店周报', description: '每周一看各门店的销售概况', icon: 'app', status: 'active', published_version: 3,
  level: 'published', needs_upgrade: false, pending_tasks: 0, my_role: 'use', created_at: ago(7 * 86_400_000),
  updated_at: ago(3600_000), scope: 'shared', owner: null, blueprint_id: null,
}
const item = (value, status = 'confirmed') => ({ value, source: 'user', status, why: null })
const MY_BLUEPRINT = {
  id: 'bp_mine', name: '我的退款周报', owner: 'zhangsan', scope: 'personal', status: 'personal',
  spec: { format: 'agentlab.blueprint/1', purpose: item('每周看自己门店的退款'), template: null, data: [],
    cadence: item(null, 'missing'), people: [], fields: [], writeback: null, access: { use: [], observe: [], manage: [] }, asks: [] },
  template_id: null, template_version: null, source: { kind: 'conversation' }, conversation_id: null, workflow_id: null,
  app_id: 'app_mine', last_trial_run_id: null, graph_hash: null, joints: {}, copied_from: null, issues: [],
  created_at: ago(86_400_000), updated_at: ago(600_000),
}

const browser = await chromium.launch({ executablePath: CHROME })

/**
 * state.mode：off / local / oidc；state.who：登录了是谁（user / builder），没登录为空；state.expire：之后的接口一律回 401
 */
async function open(state) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 860 } })
  opened.add(ctx)
  const page = await ctx.newPage()
  page.errors = []
  page.on('pageerror', (e) => page.errors.push(e.message))
  await page.route((url) => url.pathname.startsWith('/api/'), (route) => {
    const r = route.request()
    const p = new URL(r.url()).pathname
    const headers = r.headers()
    if (r.method() !== 'GET') {
      state.writes.push({ method: r.method(), path: p, csrf: headers['x-agentlab-request'], body: r.postData() })
      if (p === '/api/auth/login') {
        const body = JSON.parse(r.postData() || '{}')
        if (body.password !== 'right') return route.fulfill({ status: 401, json: { detail: '用户名或密码不正确' } })
        state.who = body.username === 'lisi' ? 'builder' : 'user'
        return route.fulfill({ json: { mode: state.mode, ...PEOPLE[state.who], verified: true } })
      }
      if (p === '/api/auth/logout') { state.who = null; return route.fulfill({ status: 204, body: '' }) }
      if (p === '/api/auth/bootstrap') {
        if (!state.bootstrap) return route.fulfill({ status: 409, json: { detail: '已经有管理员了，请用管理员账号登录' } })
        const body = JSON.parse(r.postData() || '{}')
        state.bootstrap = false
        state.who = 'admin'
        return route.fulfill({ json: { mode: state.mode, ...PEOPLE.admin, username: body.username,
          display_name: body.display_name || body.username, verified: true } })
      }
      return route.fulfill({ json: {} })
    }
    if (p === '/api/health') return route.continue()
    if (p === '/api/auth/me') {
      state.meHits = (state.meHits ?? 0) + 1
      if (state.mode === 'off') return route.fulfill({ json: { mode: 'off', username: null, display_name: null, role: 'admin', groups: [], verified: false } })
      // 会话过期时认人接口也回 401（同服务端）：不然登录页认得出人、又把人送回原处，来回跳
      if (!state.who || state.expire) {
        return route.fulfill({ status: 401, json: { detail: '请先登录', code: 'auth_required', mode: state.mode,
          can_bootstrap: !!state.bootstrap } })
      }
      return route.fulfill({ json: { mode: state.mode, ...PEOPLE[state.who], verified: true, user_can_chat: !!state.chat,
        user_personal_apps: !!state.personal } })
    }
    state.reads?.push(p)
    if (state.mode !== 'off' && (!state.who || state.expire)) {
      return route.fulfill({ status: 401, json: { detail: '请先登录', code: 'auth_required', mode: state.mode } })
    }
    if (p === '/api/apps') {
      return route.fulfill({ json: { items: state.apps ?? [], can_create: { shared: state.who !== 'user', personal: !!state.personal } } })
    }
    // 蓝图：使用者自己的个人蓝图（开关开着时能进）；没有准备的一律当没有
    if (p === '/api/blueprints') return route.fulfill({ json: { items: state.personal ? [MY_BLUEPRINT] : [] } })
    if (p === '/api/blueprints/bp_mine') return route.fulfill({ json: MY_BLUEPRINT })
    if (p.startsWith('/api/blueprints')) return route.fulfill({ status: 404, json: { detail: '蓝图不存在或已被删除' } })
    if (p === '/api/templates') return route.fulfill({ json: { items: [] } })
    if (p === '/api/tasks/count') return route.fulfill({ json: { mine: 1, apps: 1, other: 0 } })
    if (p === '/api/tasks') return route.fulfill({ json: { items: [TASK] } })
    if (p === '/api/tasks/task_x') return route.fulfill({ json: TASK })
    // 管理员允许使用者问数据时，问数据页只拿得到数据源的名字和说明
    if (p === '/api/datasources/names') {
      return state.chat ? route.fulfill({ json: { items: [{ id: 'src1', name: 'shop', description: '门店销售' }] } })
        : route.fulfill({ status: 403, json: { detail: '没有权限' } })
    }
    if (p === '/api/conversations') return route.fulfill({ json: [] })
    if (p === '/api/settings' && state.who === 'user') return route.fulfill({ status: 403, json: { detail: '没有权限' } })
    // 使用者碰不到这些：服务端回 403
    if (state.who === 'user' && /^\/api\/(workflows|approvals|runs|tools|providers|skills|kb|datasources)/.test(p)) {
      return route.fulfill({ status: 403, json: { detail: '没有权限' } })
    }
    return route.continue()
  })
  return page
}

const nav = (page) => page.locator('nav[aria-label="主导航"]')
const navLabels = (page) => nav(page).locator('li > a[aria-keyshortcuts] > span:not([aria-hidden])').allInnerTexts()

await section('没登录：送到登录页，登录后回原处', async () => {
  const state = { mode: 'local', who: null, writes: [] }
  const page = await open(state)
  await page.goto(`${WEB}/apps?tab=inbox`)
  check('送到 /login 并带上原处', await page.waitForURL(/\/login\?next=%2Fapps%3Ftab%3Dinbox/, { timeout: 15000 }).then(() => true, () => false), page.url())
  await page.locator('[data-login-form]').waitFor({ timeout: 8000 })
  await page.waitForTimeout(500)
  check('进门只认一次人（/auth/me 只请求一次）', state.meHits === 1, `${state.meHits} 次`)
  check('账号密码登录：有表单、没有单点登录按钮', (await page.locator('[data-login-oidc]').count()) === 0)
  check('已有管理员：不给建管理员的表单', (await page.locator('[data-login-bootstrap]').count()) === 0)
  check('登录页不在外壳里（没有导航）', (await nav(page).count()) === 0)
  await page.locator('#login-username').fill('zhangsan')
  await page.locator('#login-password').fill('wrong')
  await page.getByRole('button', { name: '登录' }).click()
  check('密码不对写人话', (await page.locator('[data-login-error]').innerText()).includes('用户名或密码不正确'))
  const tried = state.writes.find((w) => w.path === '/api/auth/login')
  check('登录请求带 X-AgentLab-Request 头', tried?.csrf === '1', String(tried?.csrf))
  check('密码不对不跳走（登录接口自己的 401 不触发跳转）', /\/login/.test(page.url()))
  await page.locator('#login-password').fill('right')
  await page.getByRole('button', { name: '登录' }).click()
  check('登录后回到原处', await page.waitForURL(/\/apps\?tab=inbox/, { timeout: 15000 }).then(() => true, () => false), page.url())
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')
})

await section('新部署：登录页建第一个管理员，建好即登录、回原处', async () => {
  const state = { mode: 'local', who: null, bootstrap: true, writes: [] }
  const page = await open(state)
  await page.goto(`${WEB}/apps`)
  check('送到 /login 并带上原处', await page.waitForURL(/\/login\?next=%2Fapps/, { timeout: 15000 }).then(() => true, () => false), page.url())
  const form = page.locator('[data-login-bootstrap]')
  check('给「建第一个管理员」的表单', await form.waitFor({ timeout: 8000 }).then(() => true, () => false))
  check('不先给登录表单', (await page.locator('[data-login-form]').count()) === 0)
  check('写明这是新部署、建好直接登录', (await form.innerText()).includes('还没有能登录的管理员'))
  const submit = form.getByRole('button', { name: '建立并登录' })
  await page.locator('#boot-username').fill('root')
  await page.locator('#boot-display').fill('管理员甲')
  await page.locator('#boot-password').fill('password-123')
  await page.locator('#boot-confirm').fill('password-124')
  check('两次密码不一致：当场说、不让提交', (await form.locator('[data-login-mismatch]').count()) === 1 && await submit.isDisabled())
  // 已有别的账号的人：展开登录表单，再收回来
  await page.locator('[data-login-have-account]').click()
  check('「已有账号」展开登录表单', await page.locator('[data-login-form]').waitFor({ timeout: 3000 }).then(() => true, () => false)
    && (await page.locator('[data-login-bootstrap]').count()) === 0)
  await page.reload()
  await form.waitFor({ timeout: 8000 })
  await page.locator('#boot-username').fill('root')
  await page.locator('#boot-display').fill('管理员甲')
  await page.locator('#boot-password').fill('password-123')
  await page.locator('#boot-confirm').fill('password-123')
  await submit.click()
  check('建好即登录、回到原处', await page.waitForURL(/\/apps(\?|$)/, { timeout: 15000 }).then(() => true, () => false), page.url())
  const sent = state.writes.find((w) => w.path === '/api/auth/bootstrap')
  const body = JSON.parse(sent?.body || '{}')
  check('请求带用户名、显示名、密码，带 X-AgentLab-Request 头', body.username === 'root' && body.display_name === '管理员甲'
    && body.password === 'password-123' && !('confirm' in body) && sent?.csrf === '1', sent?.body)
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')
})

await section('使用者：导航只有应用和设置、账户菜单、没有署名框', async () => {
  const state = { mode: 'local', who: 'user', writes: [] }
  const page = await open(state)
  await page.goto(`${WEB}/apps`)
  await nav(page).waitFor({ timeout: 15000 })
  check('导航只有「应用」「设置」', (await navLabels(page)).join(',') === '应用,设置', (await navLabels(page)).join(','))
  check('没有署名入口', (await nav(page).locator('a[href="/settings/prefs"]').count()) === 0)
  check('没有「未开启登录」的说明', (await page.locator('[data-auth-off]').count()) === 0)
  await nav(page).locator('[data-account]').click()
  const menu = page.locator('[data-account-menu]')
  check('账户菜单：显示名、角色', (await menu.innerText()).includes('张三') && (await menu.locator('[data-account-role]').innerText()) === '使用者')
  await page.keyboard.press('Escape')
  await page.goto(`${WEB}/studio`)
  check('看不到的页面送回应用首页', await page.waitForURL(/\/apps(\?|$)/, { timeout: 8000 }).then(() => true, () => false), page.url())
  await page.locator('main').click({ position: { x: 600, y: 400 } }).catch(() => {})
  await page.keyboard.press('Alt+Digit2')
  await page.waitForTimeout(500)
  check('⌥2 切不到编排', !/\/studio/.test(page.url()), page.url())
  await page.goto(`${WEB}/apps/inbox/task_x`)
  await page.locator('[data-task]').waitFor({ timeout: 8000 })
  check('待办卡写登录身份，不写署名', (await page.locator('[data-task-signer]').getAttribute('data-task-signer')) === 'verified'
    && (await page.locator('[data-task-signer]').innerText()).includes('张三（zhangsan）'))
  await nav(page).locator('[data-account]').click()
  await page.locator('[data-logout]').click()
  check('退出登录回到登录页', await page.waitForURL(/\/login/, { timeout: 8000 }).then(() => true, () => false), page.url())
  check('退出请求带 X-AgentLab-Request 头', state.writes.find((w) => w.path === '/api/auth/logout')?.csrf === '1')
})

await section('会话中途失效：401 整页跳登录', async () => {
  const state = { mode: 'local', who: 'user', writes: [] }
  const page = await open(state)
  await page.goto(`${WEB}/apps?tab=apps`)
  await nav(page).waitFor({ timeout: 15000 })
  state.expire = true
  // 点一下触发一次取数；外壳的心跳可能先收到 401、整页已经跳走了，那时点不到也不要紧——要核对的是跳到了哪里
  await page.getByRole('tab', { name: '我发起的' }).click({ timeout: 3000 }).catch(() => {})
  check('任何接口回 401 都跳 /login，带上原处', await page.waitForURL(/\/login\?next=/, { timeout: 15000 }).then(() => true, () => false), page.url())
})

await section('搭建者：导航十项、账户菜单', async () => {
  const state = { mode: 'local', who: 'builder', writes: [] }
  const page = await open(state)
  await page.goto(`${WEB}/apps`)
  await nav(page).waitFor({ timeout: 15000 })
  check('导航十项都在（含搭建者以上可见的「评测」）', (await navLabels(page)).length === 10 && (await navLabels(page)).includes('评测'),
    (await navLabels(page)).join(','))
  await nav(page).locator('[data-account]').click()
  check('账户菜单写搭建者', (await page.locator('[data-account-role]').innerText()) === '搭建者')
})

await section('单点登录：按钮、本地账号收在下面、回调失败的原因', async () => {
  const state = { mode: 'oidc', who: null, writes: [] }
  const page = await open(state)
  await page.goto(`${WEB}/login?next=%2Fapps&error=${encodeURIComponent('身份提供方拒绝了授权')}`)
  const btn = page.locator('[data-login-oidc]')
  await btn.waitFor({ timeout: 15000 })
  check('单点登录按钮带上 next', (await btn.getAttribute('href')) === '/api/auth/oidc/start?next=%2Fapps', await btn.getAttribute('href'))
  check('本地账号收在下面', (await page.locator('[data-login-form]').count()) === 0)
  check('回调失败的原因照写', (await page.locator('[data-login-error]').innerText()).includes('拒绝了授权'))
  await page.goto(`${WEB}/login?next=${encodeURIComponent('//evil.example/x')}`)
  await btn.waitFor({ timeout: 8000 })
  check('站外的 next 不认', (await btn.getAttribute('href')) === '/api/auth/oidc/start?next=%2F', await btn.getAttribute('href'))
})

await section('关登录：登录页回首页、署名入口、说明行', async () => {
  const state = { mode: 'off', who: null, writes: [] }
  const page = await open(state)
  await page.goto(`${WEB}/login`)
  check('/login 回首页', await page.waitForURL(/\/chat/, { timeout: 15000 }).then(() => true, () => false), page.url())
  await nav(page).waitFor({ timeout: 8000 })
  check('导航十项', (await navLabels(page)).length === 10)
  check('底部是署名入口', (await nav(page).locator('a[href="/settings/prefs"]').count()) === 1)
  check('没有账户菜单', (await nav(page).locator('[data-account]').count()) === 0)
  await page.goto(`${WEB}/apps`)
  check('应用首页挂「未开启登录」的说明', await page.locator('[data-auth-off]').waitFor({ timeout: 8000 }).then(() => true, () => false))
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')
})

await section('使用者问数据（管理员开了）：只取数据源的名字，不取完整列表和表结构', async () => {
  const state = { mode: 'local', who: 'user', chat: true, writes: [], reads: [] }
  const page = await open(state)
  await page.goto(`${WEB}/chat`)
  await nav(page).waitFor({ timeout: 15000 })
  check('导航有「问数据」', (await navLabels(page)).includes('问数据'), (await navLabels(page)).join(','))
  await page.waitForTimeout(1200)
  check('取了数据源的名字', state.reads.includes('/api/datasources/names'), state.reads.filter((x) => x.includes('datasources')).join(','))
  check('没取完整的数据源列表和表结构', !state.reads.some((x) => x === '/api/datasources' || /^\/api\/datasources\/[^/]+\/schema/.test(x)),
    state.reads.filter((x) => x.includes('datasources')).join(','))
  check('没取只给搭建者的目录（工作流、工具、模型接入）', !state.reads.some((x) => /^\/api\/(workflows|tools|providers|skills)$/.test(x)),
    state.reads.filter((x) => /workflows|tools|providers|skills/.test(x)).join(','))
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')
})

await section('使用者的设置页：只剩偏好设置和通知，换主题只存本机', async () => {
  const state = { mode: 'local', who: 'user', writes: [], reads: [] }
  const page = await open(state)
  await page.goto(`${WEB}/settings`)
  await page.locator('[role=tab]').first().waitFor({ timeout: 15000 })
  const tabs = (await page.locator('[role=tab]').allInnerTexts()).map((t) => t.trim())
  check('页签只剩偏好设置和通知', tabs.length === 2 && tabs.some((t) => t.includes('偏好')) && tabs.some((t) => t.includes('通知')), tabs.join(' | '))
  await page.getByRole('radio', { name: /深色/ }).click()
  await page.waitForTimeout(400)
  check('换主题不往服务端写', !state.writes.some((w) => w.path === '/api/settings'), state.writes.map((w) => w.path).join(','))
  check('不提示「未能保存到设置」', !(await page.locator('body').innerText()).includes('未能保存到设置'))
  check('启动时也不去读服务端的设置', !state.reads.includes('/api/settings'))
})

await section('使用者：个人应用开关关着时没有新建入口，开着时有「新建应用」和「我的应用」', async () => {
  // 关着：首页没有「新建应用」，也没有「我的应用」一组；直接打开新建页写明管理员还没开放；进不了蓝图页
  const off = { mode: 'local', who: 'user', personal: false, apps: [SHARED_APP], writes: [] }
  const page = await open(off)
  await page.goto(`${WEB}/apps?tab=apps`)
  await page.locator('[data-app="app_weekly"]').waitFor({ timeout: 15000 })
  check('关着：页头没有「新建应用」', await page.locator('[data-app-create]').count() === 0)
  check('……没有「我的应用」一组', await page.locator('[data-app-group="personal"]').count() === 0)
  await page.goto(`${WEB}/apps/create`)
  await page.locator('[data-new-app]').waitFor({ timeout: 8000 })
  check('……打开新建页：写明管理员还没有开放个人应用，不给起点', (await page.locator('[data-new-app]').getAttribute('data-new-app')) === 'none'
    && (await page.locator('main').innerText()).includes('管理员还没有开放个人应用'))
  await page.goto(`${WEB}/blueprints/bp_mine`)
  check('……进不了蓝图页（送回应用首页）', await page.waitForURL(/\/apps(\?|$)/, { timeout: 8000 }).then(() => true, () => false), page.url())
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')

  // 开着：页头有「新建应用」（建的是个人应用），自己的个人应用单列「我的应用」；能进自己的蓝图页
  const on = { mode: 'local', who: 'user', personal: true, apps: [MY_APP, SHARED_APP], writes: [] }
  const p2 = await open(on)
  await p2.goto(`${WEB}/apps?tab=apps`)
  const mine = p2.locator('[data-app-group="personal"]')
  check('开着：自己的个人应用单列「我的应用」', await mine.waitFor({ timeout: 15000 }).then(() => true, () => false)
    && await mine.locator('[data-app="app_mine"]').count() === 1)
  check('……共享的应用另列一组', await p2.locator('[data-app-group="shared"] [data-app="app_weekly"]').count() === 1)
  const create = p2.locator('[data-app-create]')
  check('……页头有「新建应用」，建的是个人应用', await create.count() === 1 && (await create.getAttribute('data-app-create')) === 'personal'
    && (await create.getAttribute('href')) === '/apps/create', String(await create.getAttribute('data-app-create').catch(() => '')))
  await p2.goto(`${WEB}/apps/create`)
  await p2.locator('[data-new-app]').waitFor({ timeout: 8000 })
  check('……新建页按个人应用给起点', (await p2.locator('[data-new-app]').getAttribute('data-new-app')) === 'personal'
    && await p2.locator('[data-new-app-personal]').count() === 1)
  await p2.goto(`${WEB}/blueprints/bp_mine`)
  await p2.waitForTimeout(800)
  check('……能进自己的蓝图页', /\/blueprints\/bp_mine/.test(p2.url()), p2.url())
  check('没有未捕获的运行时错误', p2.errors.length === 0, p2.errors[0] ?? '')
})

await browser.close()
console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 全部通过')
process.exit(failed ? 1 : 0)
