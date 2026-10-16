// 设置 → 访问密钥的检查（docs/design/next-round/04-对外集成与触达.md A1、A6、A7）。名字不叫 check-tokens：那个名字查的是设计令牌。
//
// 守的是：
//   - 管理员：个人访问密钥的开关；服务账号（建、停用前确认）；全部密钥（拥有者、状态、到期、最近使用、权限）；
//   - 新建密钥：缺省只勾读的权限；服务账号不能勾回复待办；有效期必填、最长 365 天；建好后明文只显示这一次，点「我已保存」就没了；
//   - 轮换：先确认，签出的新密钥同样只显示一次；撤销：先确认；
//   - 「最近的请求」（和钩子的收件记录同一个组件）：接口写路由模板，成功、被拒分开上色；
//   - 接口说明：照服务端那张表列出方法、路径、权限、说明、错误码，要幂等键的标出来；
//   - 使用者：管理员开了个人访问密钥才有这一页，只看自己的，建的时候不选拥有者；没开时设置里没有这一页；
//   - 关登录：照实说访问密钥不生效，不给建密钥的入口，接口说明照看；
//   - 身份页：服务账号那一行标出来，没有角色、密码这些控件。
//
// 不写库：/api/auth/me、/api/access-keys/*、/api/apps、/api/users、/api/groups、/api/auth/settings 全部伪造，伪造的接口改一份
// 内存里的数据。其余写请求一律 409 并记账，检查结束时必须是空的。
//
// 跑之前前端得起着（./scripts/dev.sh），默认连 5273：
//   AGENTLAB_WEB=http://localhost:<前端端口> node scripts/check-access-keys.mjs
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const CHROME = process.env.CHROME_PATH ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const ONLY = (process.env.ACCESS_KEYS_ONLY ?? '').split(',').filter(Boolean)

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

// ---------------------------------------------------------------- 夹具（通用示例名）

const ADMIN = { mode: 'local', username: 'root', display_name: '管理员', role: 'admin', groups: [], verified: true }
const USER = { mode: 'local', username: 'pia', display_name: '小皮', role: 'user', groups: [], verified: true }
const OFF = { mode: 'off', username: null, display_name: null, role: 'admin', groups: [], verified: false }
const SCOPES = ['apps.read', 'apps.run', 'tasks.read', 'tasks.respond', 'reports.read']
const settingsOf = (over = {}) => ({
  login_on: true, personal_keys: false, admin: true, can_create_personal: false, scopes: SCOPES,
  read_scopes: ['apps.read', 'tasks.read', 'reports.read'], personal_scopes: ['tasks.respond'],
  expires_default_days: 90, expires_max_days: 365, ...over,
})
const SERVICES = [
  { id: 'sa1', username: 'svc-portal', display_name: '门户', disabled: false, created_by: 'root', created_at: '2026-10-01T02:00:00Z', keys: 1 },
]
const keyRow = (over = {}) => ({
  id: 'k1', name: '门户拉数', prefix: 'agl_AbCd1234', scopes: ['apps.read', 'reports.read'], app_ids: null,
  owner: { id: 'sa1', username: 'svc-portal', display_name: '门户', kind: 'service' }, personal: false, state: 'active',
  expires_at: '2026-12-30T02:00:00Z', revoked_at: null, revoked_by: null, last_used_at: '2026-10-15T08:00:00Z',
  last_used_ip: '10.0.0.5', rotated_from: null, created_by: 'root', created_at: '2026-10-01T02:00:00Z', ...over,
})
const KEYS = [
  keyRow(),
  keyRow({ id: 'k2', name: '旧脚本', prefix: 'agl_ZzYy9876', personal: true, state: 'revoked', revoked_at: '2026-10-10T02:00:00Z',
    owner: { id: 'u2', username: 'pia', display_name: '小皮', kind: 'person' }, last_used_at: null, scopes: ['apps.read'] }),
]
const APPS = [
  { id: 'app-w', name: '周报看板', description: '', icon: 'app', status: 'active', published_version: 1, level: 'published',
    needs_upgrade: false, pending_tasks: 0, my_role: 'manage', created_at: null, updated_at: '2026-10-01T02:00:00Z', scope: 'shared' },
  { id: 'app-p', name: '我的看板', description: '', icon: 'app', status: 'active', published_version: null, level: null,
    needs_upgrade: false, pending_tasks: 0, my_role: 'manage', created_at: null, updated_at: '2026-10-01T02:00:00Z', scope: 'personal' },
]
const REFERENCE = {
  base_url: 'https://agentlab.example.com', header: 'Authorization: Bearer agl_…', idempotency_header: 'Idempotency-Key',
  scopes: SCOPES, implicit_scope: 'me', personal_scopes: ['tasks.respond'], rate_per_minute: 120,
  common_errors: ['key_invalid', 'key_expired', 'key_revoked', 'key_route_forbidden'],
  routes: [
    { method: 'GET', path: '/api/auth/me', scope: 'me', summary: '这把密钥认出来是谁', errors: [], idempotent: false },
    { method: 'GET', path: '/api/apps/{app_id}/views/{view_id}/data', scope: 'apps.read', summary: '视图数据', errors: ['app_not_found'],
      idempotent: false },
    { method: 'POST', path: '/api/apps/{app_id}/entries/{entry_id}', scope: 'apps.run', summary: '按入口发起一个实例',
      errors: ['key_idempotency_required', 'key_files_unsupported'], idempotent: true },
  ],
}
const REQUESTS = [
  { at: '2026-10-15T08:00:00Z', method: 'GET', route: '/api/apps/{app_id}/views/{view_id}/data', status: 200, run_id: 'run-abc12345', ip: '10.0.0.5' },
  { at: '2026-10-15T07:59:00Z', method: 'GET', route: '/api/runs', status: 403, run_id: null, ip: '10.0.0.5' },
]

async function open(path, { me = ADMIN, settings = settingsOf(), keys = KEYS, services = SERVICES, users = [] } = {}) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  opened.add(ctx)
  const page = await ctx.newPage()
  page.setDefaultTimeout(8000)
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  const json = (route, body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
  const state = { settings: clone(settings), keys: clone(keys), services: clone(services), switches: [], serviceWrites: [],
    creates: [], revokes: [], rotates: [], requests: [], references: 0 }

  await page.route(/\/api\//, (route) => {
    const req = route.request()
    if (req.method() === 'GET') return route.continue()
    writes.push(`${req.method()} ${new URL(req.url()).pathname}`)
    return json(route, { detail: '检查脚本不写库' }, 409)
  })
  await page.route(/\/api\/auth\/me$/, (route) => json(route, me))
  await page.route(/\/api\/apps(\?.*)?$/, (route) => json(route, { items: APPS, can_create: { shared: true, personal: false } }))
  await page.route(/\/api\/users$/, (route) => json(route, { items: users }))
  await page.route(/\/api\/groups$/, (route) => json(route, { items: [] }))
  await page.route(/\/api\/auth\/settings$/, (route) => json(route, { mode: me.mode, env_locked: false, oidc: {}, user_can_chat: false }))
  await page.route(/\/api\/access-keys(\/.*)?(\?.*)?$/, (route) => {
    const req = route.request()
    const parts = new URL(req.url()).pathname.split('/').slice(2) // ['access-keys', ...]
    const [, a, b] = parts
    if (a === 'settings') {
      if (req.method() === 'PUT') {
        state.switches.push(req.postDataJSON())
        state.settings.personal_keys = req.postDataJSON().personal_keys
      }
      return json(route, state.settings)
    }
    if (a === 'reference') { state.references += 1; return json(route, REFERENCE) }
    if (a === 'service-accounts') {
      if (req.method() === 'GET') return json(route, { items: state.services })
      const body = req.postDataJSON()
      state.serviceWrites.push({ method: req.method(), id: b ?? null, body })
      if (req.method() === 'POST') {
        const row = { id: `sa${state.services.length + 1}`, username: body.username, display_name: body.display_name || body.username,
          disabled: false, created_by: 'root', created_at: '2026-10-16T02:00:00Z', keys: 0 }
        state.services.push(row)
        return json(route, row, 201)
      }
      const row = state.services.find((s) => s.id === b)
      Object.assign(row, body)
      return json(route, row)
    }
    if (!a) {
      if (req.method() === 'GET') return json(route, { items: state.keys })
      const body = req.postDataJSON()
      state.creates.push(body)
      const row = keyRow({ id: `k${state.keys.length + 1}`, name: body.name, prefix: 'agl_NewKey00', scopes: body.scopes,
        app_ids: body.app_ids, personal: !body.service_account_id, last_used_at: null })
      state.keys.unshift(row)
      return json(route, { ...row, key: 'agl_NewKey00-only-shown-once-secret' }, 201)
    }
    if (b === 'revoke') {
      state.revokes.push(a)
      const row = state.keys.find((k) => k.id === a)
      Object.assign(row, { state: 'revoked', revoked_at: '2026-10-16T02:00:00Z' })
      return json(route, row)
    }
    if (b === 'rotate') {
      state.rotates.push(a)
      const old = state.keys.find((k) => k.id === a)
      const row = { ...old, id: `${a}-r`, prefix: 'agl_Rotated0', rotated_from: a }
      state.keys.unshift(row)
      return json(route, { ...row, key: 'agl_Rotated0-new-secret-after-rotation', previous: old })
    }
    if (b === 'requests') { state.requests.push(a); return json(route, { items: REQUESTS, retain_days: 90 }) }
    return json(route, { detail: '没有这个接口' }, 404)
  })

  await page.goto(`${WEB}${path}`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(300)
  return { page, state, errors }
}

const confirm = async (page, label) => {
  const dlg = page.locator('[role="dialog"]').last()
  await dlg.waitFor()
  await dlg.getByRole('button', { name: label, exact: true }).click()
}

const writes = []
const browser = await chromium.launch({ executablePath: CHROME })

// ================================================================
await section('管理员：个人访问密钥的开关、服务账号、全部密钥', async () => {
  const { page, state, errors } = await open('/settings/keys')
  check('设置页有「访问密钥」页签', await page.getByRole('tab', { name: '访问密钥', exact: true }).count() === 1,
    (await page.locator('[role=tab]').allInnerTexts()).join(' | '))
  const root = page.locator('[data-access-keys="admin"]')
  check('按管理员显示', await waitFor(page, async () => await root.count() === 1))
  check('说清访问密钥按使用者的权限算、看到的一律遮罩', (await root.innerText()).includes('按使用者的权限算'))
  const sw = page.locator('[data-access-keys-personal-switch]')
  check('个人访问密钥的开关缺省关', await sw.getAttribute('data-access-keys-personal-switch') === 'off')
  // 受控的开关：等服务端回了才翻过来，所以点它、再等它变
  await sw.locator('input').click()
  check('打开开关：只改这一项', await waitFor(page, async () => state.switches.length === 1)
    && state.switches[0].personal_keys === true
    && await waitFor(page, async () => await sw.getAttribute('data-access-keys-personal-switch') === 'on'), JSON.stringify(state.switches))
  check('服务账号列出来，标着在用的密钥数', (await page.locator('[data-service-account="svc-portal"]').innerText()).includes('1 把在用的密钥'))
  await page.locator('[data-service-account-create]').click()
  const form = page.locator('[data-service-account-form]')
  check('新建服务账号：用户名预填 svc- 开头', await form.locator('[data-service-account-username]').inputValue() === 'svc-')
  await form.locator('[data-service-account-username]').fill('svc-bi')
  await page.locator('[data-service-account-save]').click()
  check('建服务账号按填的用户名发', await waitFor(page, async () => state.serviceWrites.length === 1)
    && state.serviceWrites[0].body.username === 'svc-bi', JSON.stringify(state.serviceWrites))
  check('建好之后列出来', await waitFor(page, async () => await page.locator('[data-service-account="svc-bi"]').count() === 1))
  await page.locator('[data-service-account="svc-portal"] [data-service-account-toggle="disable"]').click()
  await confirm(page, '停用')
  check('停用前先确认，确认后才发', await waitFor(page, async () => state.serviceWrites.length === 2)
    && state.serviceWrites[1].body.disabled === true && state.serviceWrites[1].id === 'sa1')
  const list = page.locator('[data-access-key-list]')
  check('全部密钥：服务账号的和别人的个人密钥都在', await list.locator('[data-access-key]').count() === 2)
  const active = list.locator('[data-access-key="门户拉数"]')
  check('一把密钥：开头一截、拥有者、到期、最近使用、权限', (await active.innerText()).includes('agl_AbCd1234')
    && (await active.innerText()).includes('svc-portal') && (await active.innerText()).includes('到期')
    && (await active.innerText()).includes('最近使用') && await active.locator('[data-access-key-scopes] .chip').count() === 2)
  const revoked = list.locator('[data-access-key="旧脚本"]')
  check('撤销了的照实标出，不再给轮换和撤销', await revoked.getAttribute('data-access-key-state') === 'revoked'
    && (await revoked.innerText()).includes('已撤销') && await revoked.locator('[data-access-key-rotate]').count() === 0)
  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))
})

await section('新建密钥：缺省只勾读的；服务账号不能回复待办；明文只显示一次', async () => {
  const { page, state } = await open('/settings/keys')
  await page.locator('[data-access-key-create]').click()
  const form = page.locator('[data-access-key-form]')
  await form.waitFor()
  check('拥有者缺省是服务账号（个人访问密钥没开）', await waitFor(page, async () =>
    await form.locator('[data-access-key-owner]').inputValue() === 'sa1'))
  const checked = async (s) => form.locator(`[data-access-key-scope="${s}"] input`).isChecked()
  check('缺省只勾读的三项', await checked('apps.read') && await checked('tasks.read') && await checked('reports.read')
    && !(await checked('apps.run')))
  check('服务账号：回复待办勾不了、说清原因', await form.locator('[data-access-key-scope="tasks.respond"] input').isDisabled()
    && (await form.locator('[data-access-key-scope="tasks.respond"]').innerText()).includes('不能被派待办'))
  check('个人应用不在限定应用里', await form.locator('[data-access-key-app="app-p"]').count() === 0
    && await form.locator('[data-access-key-app="app-w"]').count() === 1)
  check('名称空着不能建', await page.locator('[data-access-key-save]').isDisabled())
  await form.locator('[data-access-key-name]').fill('BI 拉数')
  await form.locator('[data-access-key-days]').fill('400')
  check('有效期超过 365 天不能建、说清最长几天', await page.locator('[data-access-key-save]').isDisabled()
    && (await form.innerText()).includes('最长 365 天'))
  await form.locator('[data-access-key-days]').fill('30')
  await form.locator('[data-access-key-scope="apps.run"] input').check()
  await form.locator('[data-access-key-app="app-w"] input').check()
  await page.locator('[data-access-key-save]').click()
  const body = (await waitFor(page, async () => state.creates.length === 1)) ? state.creates[0] : null
  check('建的时候发名称、权限、限定应用、有效期、服务账号', body?.name === 'BI 拉数' && body.expires_in_days === 30
    && body.service_account_id === 'sa1' && JSON.stringify(body.app_ids) === '["app-w"]'
    && body.scopes.includes('apps.run') && !body.scopes.includes('tasks.respond'), JSON.stringify(body))
  const once = page.locator('[data-access-key-once="BI 拉数"]')
  check('明文只显示这一次：值、复制、说明', await waitFor(page, async () => await once.count() === 1)
    && (await once.locator('[data-access-key-secret]').innerText()).includes('agl_NewKey00-only-shown-once-secret')
    && (await once.innerText()).includes('只显示这一次') && await once.getByRole('button', { name: '复制' }).count() === 1)
  await once.locator('[data-access-key-once-done]').click()
  check('点「我已保存」之后页面上没有明文了', await page.locator('[data-access-key-secret]').count() === 0
    && !(await page.locator('body').innerText()).includes('only-shown-once-secret'))
})

await section('轮换与撤销：先确认；轮换出的新密钥只显示一次', async () => {
  const { page, state } = await open('/settings/keys')
  await page.locator('[data-access-key-rotate="门户拉数"]').click()
  const dlg = page.locator('[role="dialog"]').last()
  await dlg.waitFor()
  check('轮换前说清旧密钥 24 小时后到期', (await dlg.innerText()).includes('24 小时后到期'))
  await confirm(page, '轮换')
  check('确认后才发轮换', await waitFor(page, async () => state.rotates.length === 1) && state.rotates[0] === 'k1')
  check('新密钥只显示这一次', await waitFor(page, async () =>
    (await page.locator('[data-access-key-secret]').innerText()).includes('agl_Rotated0-new-secret-after-rotation')))
  check('列表里标出由轮换签发', await waitFor(page, async () =>
    (await page.locator('[data-access-key-list]').innerText()).includes('由轮换签发')))
  await page.locator('[data-access-key-revoke="门户拉数"]').last().click()
  await page.locator('[role="dialog"]').last().getByRole('button', { name: '取消' }).click()
  await page.waitForTimeout(200)
  check('撤销取消了就不发', state.revokes.length === 0)
  await page.locator('[data-access-key-revoke="门户拉数"]').last().click()
  await confirm(page, '撤销')
  check('确认后才撤销', await waitFor(page, async () => state.revokes.length === 1))
})

await section('最近的请求：路由模板、成功和被拒分开', async () => {
  const { page, state } = await open('/settings/keys')
  await page.locator('[data-access-key-requests-toggle="门户拉数"]').click()
  const box = page.locator('[data-access-key-requests="k1"]')
  check('按这把密钥取', await waitFor(page, async () => state.requests.includes('k1')))
  check('两条请求，写的是路由模板', await waitFor(page, async () => await box.locator('[data-recent-request]').count() === 2)
    && (await box.innerText()).includes('/api/apps/{app_id}/views/{view_id}/data'))
  check('成功的、被拒的分开标', await box.locator('[data-recent-request="ok"]').count() === 1
    && (await box.locator('[data-recent-request="bad"]').innerText()).includes('被拒 403'))
  check('写明保留几天', (await box.locator('[data-recent-requests-summary]').innerText()).includes('保留 90 天'))
})

await section('接口说明：照服务端那张表列出', async () => {
  const { page, state } = await open('/settings/keys')
  await page.locator('[data-access-keys-view="reference"]').click()
  const ref = page.locator('[data-access-key-reference]')
  // 开发模式下 effect 执行两次，取一次、两次都算
  check('取了接口说明', await waitFor(page, async () => state.references >= 1 && await ref.count() === 1))
  check('每个接口一行：方法、路径、权限、说明、错误码', await ref.locator('[data-access-key-route]').count() === 3
    && (await ref.locator('[data-access-key-route="POST /api/apps/{app_id}/entries/{entry_id}"]').innerText()).includes('key_idempotency_required'))
  check('要幂等键的标出来', (await ref.locator('[data-access-key-route="POST /api/apps/{app_id}/entries/{entry_id}"]').innerText()).includes('要幂等键'))
  check('me 写不用申请', (await ref.locator('[data-access-key-route="GET /api/auth/me"]').innerText()).includes('不用申请'))
  check('认证方式、对外访问地址、限流、通用错误码都写了', (await ref.innerText()).includes('Authorization: Bearer agl_…')
    && (await ref.innerText()).includes('https://agentlab.example.com') && (await ref.innerText()).includes('每分钟最多 120 次')
    && (await ref.innerText()).includes('key_route_forbidden'))
})

await section('使用者：开了个人访问密钥才有这一页，只看自己的', async () => {
  const { page, state } = await open('/settings/keys', {
    me: { ...USER, personal_keys: true },
    settings: settingsOf({ admin: false, personal_keys: true, can_create_personal: true }),
    keys: [keyRow({ id: 'k9', name: '我的脚本', personal: true, owner: { id: 'u2', username: 'pia', display_name: '小皮', kind: 'person' } })],
  })
  const tabs = (await page.locator('[role=tab]').allInnerTexts()).map((t) => t.trim())
  check('页签：偏好、通知、访问密钥', tabs.some((t) => t.includes('访问密钥')) && tabs.length === 3, tabs.join(' | '))
  check('按本人显示：没有开关、没有服务账号', await page.locator('[data-access-keys="self"]').count() === 1
    && await page.locator('[data-access-keys-personal-switch]').count() === 0 && await page.locator('[data-service-accounts]').count() === 0)
  check('标题是「我的访问密钥」', (await page.locator('[data-access-key-list]').innerText()).includes('我的访问密钥'))
  await page.locator('[data-access-key-create]').click()
  const form = page.locator('[data-access-key-form]')
  await form.waitFor()
  check('建自己的：不选拥有者，回复待办可以勾', await form.locator('[data-access-key-owner]').count() === 0
    && await form.locator('[data-access-key-scope="tasks.respond"] input').isEnabled())
  await form.locator('[data-access-key-name]').fill('日报')
  await page.locator('[data-access-key-save]').click()
  check('发的是个人访问密钥（不带服务账号）', await waitFor(page, async () => state.creates.length === 1)
    && !('service_account_id' in state.creates[0]), JSON.stringify(state.creates[0]))
})

await section('使用者：没开个人访问密钥时设置里没有这一页', async () => {
  const { page } = await open('/settings/keys', { me: USER, settings: settingsOf({ admin: false }) })
  const tabs = (await page.locator('[role=tab]').allInnerTexts()).map((t) => t.trim())
  check('没有「访问密钥」页签', !tabs.some((t) => t.includes('访问密钥')), tabs.join(' | '))
  check('地址纠正到偏好设置', new URL(page.url()).pathname === '/settings/prefs', page.url())
})

await section('关登录：照实说访问密钥不生效，不给建的入口', async () => {
  const { page } = await open('/settings/keys', { me: OFF, settings: settingsOf({ login_on: false }) })
  check('说清未开启登录时不生效', await waitFor(page, async () => await page.locator('[data-access-keys-login-off]').count() === 1)
    && (await page.locator('[data-access-keys-login-off]').innerText()).includes('未开启登录'))
  check('没有建密钥、建服务账号的入口', await page.locator('[data-access-key-create]').count() === 0
    && await page.locator('[data-service-account-create]').count() === 0)
  await page.locator('[data-access-keys-view="reference"]').click()
  check('接口说明照看', await waitFor(page, async () => await page.locator('[data-access-key-route]').count() === 3))
})

await section('身份页：服务账号那一行标出来，没有角色和密码', async () => {
  const users = [
    { id: 'u1', username: 'root', display_name: '管理员', email: null, role: 'admin', disabled: false, source: 'local',
      last_login_at: null, created_at: '2026-10-01T02:00:00Z', kind: 'person' },
    { id: 'sa1', username: 'svc-portal', display_name: '门户', email: null, role: 'user', disabled: false, source: 'local',
      last_login_at: null, created_at: '2026-10-01T02:00:00Z', kind: 'service' },
  ]
  const { page } = await open('/settings/identity', { users })
  const svc = page.locator('[data-user="svc-portal"]')
  check('服务账号标出来、说清在哪管', await waitFor(page, async () => await svc.locator('[data-user-service]').count() === 1)
    && (await svc.innerText()).includes('设置 → 访问密钥'))
  check('服务账号没有角色、重设密码、停用这些控件', await svc.locator('select').count() === 0 && await svc.locator('button').count() === 0)
  check('人照旧有角色', await page.locator('[data-user="root"] select').count() === 1)
})

check('检查没有写库（兜底拦下的写请求）', writes.length === 0, writes.slice(0, 4).join('、'))
console.log(failed ? `\n${failed} 项未通过` : '\n全部通过')
await browser.close()
process.exit(failed ? 1 : 0)
