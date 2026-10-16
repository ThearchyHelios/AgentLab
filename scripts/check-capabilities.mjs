// 工具页的「能力」「端点」页签和设置页的「身份」「通知」「审计」的检查（应用平台 A6、A7）。
//
// 守的是：
//   - 能力列表标出读写效果，同步得出和治理后不一致时标出来；治理带 version 保存（乐观锁），409 时载入最新的、不覆盖；
//   - 端点的认证头只显示掩码，提交掩码值表示保持原值；名称空着、不合法、重名的那一行标红、不让保存；允许访问内网只有
//     管理员能开；测试连接按环境发，照服务端的话写（2xx 才是成功，能连上但 404 是提醒）；
//   - 关登录（默认）时所有人等同管理员：治理、退役、身份设置都能改；
//   - 设置页的身份、通知、审计三个页签在，通知渠道的地址只显示主机名；
//   - 邮件（04 细案 B2）：「邮件服务」一节管理员改、搭建者只看，口令只说设没设、留空不改，有没保存的改动时不能发测试邮件；
//     每天的待办汇总的发送时间；邮件服务没启用时不列「邮件」格式；个人邮件渠道不填地址、写「发到账号邮箱 …」，没有邮箱照实
//     写发不出去；投递记录里邮件成功写「已交给邮件服务器」，不写已送达；
//   - 能力字段的待审阅（D-i）复用数据目录的审阅队列：成批确认逐项带能力的治理版本；
//   - 自定义工具可以绑定登记的端点（只填路径）；MCP 的认证头按掩码编辑；本地子进程只有管理员能建、能改（A6）；
//   - 单点登录映射管理员：用户或组至少一项；开了登录的使用者在设置页只剩个人的主题和通知，换主题不写服务端（D-s）。
//
// 不写库：/api/capabilities、/api/endpoints、/api/auth、/api/users、/api/groups、/api/audit、/api/notify 全部伪造，
// 伪造的接口改一份内存里的数据。其余写请求一律 409 并记账，检查结束时必须是空的。
//
// 跑之前前端得起着（./scripts/dev.sh），默认连 5273：
//   AGENTLAB_WEB=http://localhost:<前端端口> node scripts/check-capabilities.mjs
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const CHROME = process.env.CHROME_PATH ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const ONLY = (process.env.CAPABILITIES_ONLY ?? '').split(',').filter(Boolean)

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
    if (await fn()) return true
    await page.waitForTimeout(100)
  }
  return false
}

// ---------------------------------------------------------------- 夹具（通用示例名）

const cap = (name, extra = {}) => ({
  name, system: 'erp', title: name, description: '', params_schema: { type: 'object', properties: {} }, returns_schema: null,
  tabular: null, effect_system: 'read', effect: null, approval: 'inherit', unattended_write: false, fields: {}, masked: [],
  credential_mode: 'service', endpoint_id: null, lookup: null, version: 1, missing: false, synced_at: '2026-10-10T02:00:00Z',
  updated_by: null, ...extra,
})
const CAPS = [
  cap('erp_query'),
  cap('erp_pay', { effect_system: 'dynamic', effect: 'write', approval: 'confirm' }),
  cap('mcp:crm/find', { system: 'crm', effect_system: 'read' }),
  cap('db_query__shop', { system: 'shop', effect_system: 'dynamic' }),
]
const ENDPOINTS = [{
  id: 'ep1', name: 'erp', description: '示例 ERP', idempotency_header: 'Idempotency-Key', timeout_s: 20, version: 3,
  environments: {
    prod: { base_url: 'https://erp.example.com/api', headers: { Authorization: 'Bearer ****1234' }, allow_intranet: false, verify_tls: true },
  },
}]

// 形状同服务端 data/capability_review.review_queue：能力名、位置（指针、字段、写好的位置说法）、值、来源、状态、优先级、原因、运行次数、版本
const REVIEW = [
  { capability: 'erp_pay', system: 'erp', title: '付款', effect: 'write', pointer: '/amount', field: 'unit', field_text: '单位',
    field_label: '返回字段 /amount 的单位', value: '元', display: '元', source: 'model', status: 'proposed', note: null, priority: 9,
    reasons: ['写入能力', '被 12 次运行用过'], runs: 12, version: 4 },
  { capability: 'erp_query', system: 'erp', title: '查询', effect: 'read', pointer: '/items/*/name', field: 'label', field_text: '中文名',
    field_label: '返回字段 /items/*/name 的中文名', value: '品名', display: '品名', source: 'naming', status: 'proposed', note: null,
    priority: 3, reasons: ['被 3 次运行用过'], runs: 3, version: 2 },
]
const OFF = { mode: 'off', username: null, display_name: null, role: 'admin', groups: [], verified: false }
const MCP_ROWS = [
  { id: 'm1', name: 'files', transport: 'stdio', command: 'npx', args: ['-y', 'fs'], env: {}, url: null, enabled: true, headers: {},
    endpoint_id: null, status: 'ok', last_error: null, tools_cache: [] },
  { id: 'm2', name: 'crm', transport: 'http', command: null, args: [], env: {}, url: 'https://crm.example.com/mcp', enabled: true,
    headers: { Authorization: 'Bearer ****9a2c' }, endpoint_id: null, status: 'ok', last_error: null, tools_cache: [] },
]

const NOTIFY_ITEMS = [
  { id: 'ch1', name: '财务群', scope: 'global', owner_id: null, format: 'feishu', url_host: 'open.feishu.cn', signed: true,
    events: ['task.opened'], enabled: true }]

async function open(path, { conflict = false, me = OFF, settings = null, notify = null, deliveries = [] } = {}) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  opened.add(ctx)
  const page = await ctx.newPage()
  page.setDefaultTimeout(8000)
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  const json = (route, body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
  const state = { caps: JSON.parse(JSON.stringify(CAPS)), endpoints: JSON.parse(JSON.stringify(ENDPOINTS)), governs: [], endpointWrites: [],
    tests: [], syncs: 0, settings: [], reviews: [], batches: [], tools: [], mcp: [], prefs: [], channelWrites: [], mailTests: 0,
    values: settings ? JSON.parse(JSON.stringify(settings)) : { ui: { theme: 'dark' } } }

  await page.route(/\/api\//, (route) => {
    const req = route.request()
    if (req.method() === 'GET') return route.continue()
    writes.push(`${req.method()} ${new URL(req.url()).pathname}`)
    return json(route, { detail: '检查脚本不写库' }, 409)
  })
  await page.route(/\/api\/auth\/me$/, (route) => json(route, me))
  await page.route(/\/api\/capabilities(\?.*)?$/, (route) => json(route, { items: state.caps }))
  await page.route(/\/api\/capabilities\/review-queue(\?.*)?$/, (route) => {
    const url = new URL(route.request().url())
    state.reviews.push(url.search)
    const name = url.searchParams.get('capability')
    const items = REVIEW.filter((i) => !name || i.capability === name)
    return json(route, { total: items.length, items })
  })
  await page.route(/\/api\/capabilities\/review\/batch$/, (route) => {
    const body = route.request().postDataJSON()
    state.batches.push(body)
    return json(route, { results: body.items.map((i) => (i.capability === 'erp_query'
      ? { capability: i.capability, pointer: i.pointer, field: i.field, ok: false, version: null,
          error: '能力「erp_query」的治理设置刚被修改过，请重新载入后再审阅', code: 'capability_conflict' }
      : { capability: i.capability, pointer: i.pointer, field: i.field, ok: true, version: i.if_version + 1, error: null, code: null })) })
  })
  await page.route(/\/api\/custom-tools(\/[^/]+)?$/, (route) => {
    const req = route.request()
    if (req.method() === 'GET') return json(route, [])
    state.tools.push({ method: req.method(), body: req.postDataJSON() })
    return json(route, { id: 't1', ...req.postDataJSON() })
  })
  await page.route(/\/api\/mcp\/servers(\/[^/]+)?$/, (route) => {
    const req = route.request()
    if (req.method() === 'GET') return json(route, MCP_ROWS)
    state.mcp.push({ method: req.method(), url: new URL(req.url()).pathname, body: req.postDataJSON() })
    return json(route, { id: 'm9', ...req.postDataJSON() })
  })
  await page.route(/\/api\/settings$/, (route) => {
    if (route.request().method() !== 'GET') {
      const body = route.request().postDataJSON()
      state.prefs.push(body)
      // 同服务端：给了的组并进去（mail 的口令不回、只说设没设），交回整份
      for (const [k, v] of Object.entries(body?.values ?? {})) {
        state.values[k] = { ...(state.values[k] ?? {}), ...v }
        if (k === 'mail') {
          const pw = v.password
          state.values.mail = { ...state.values.mail, password: null,
            password_set: typeof pw === 'string' ? pw !== '' : !!state.values.mail.password_set }
        }
      }
      return json(route, state.values)
    }
    return me.mode !== 'off' && me.role === 'user' ? json(route, { detail: '没有权限' }, 403) : json(route, state.values)
  })
  await page.route(/\/api\/settings\/mail\/test$/, (route) => {
    state.mailTests += 1
    return json(route, { ok: true, message: '已交给邮件服务器（发到 root@example.com），是否送达以邮件服务器为准，请到收件箱确认' })
  })
  await page.route(/\/api\/capabilities\/sync$/, (route) => { state.syncs += 1; return json(route, { changed: 2 }) })
  await page.route(/\/api\/capabilities\/[^/]+\/governance$/, (route) => {
    const name = decodeURIComponent(new URL(route.request().url()).pathname.split('/')[3])
    const body = route.request().postDataJSON()
    state.governs.push({ name, body })
    const cur = state.caps.find((c) => c.name === name)
    if (conflict && state.governs.length === 1) {
      cur.version += 1
      cur.updated_by = '别人'
      return json(route, { detail: '这个能力刚被其他人修改过' }, 409)
    }
    Object.assign(cur, body, { version: cur.version + 1 })
    return json(route, cur)
  })
  await page.route(/\/api\/endpoints(\/[^/?]+)?(\/test)?(\?.*)?$/, (route) => {
    const req = route.request()
    const url = new URL(req.url())
    const parts = url.pathname.split('/')
    if (parts[4] === 'test') {
      state.tests.push({ id: parts[3], env: url.searchParams.get('env') })
      // 服务端按状态码照实说（capabilities/endpoints.probe）：2xx 才 ok；能连上但 404 的 reachable 为真、ok 为假
      return json(route, state.probe ?? { ok: true, reachable: true, status: 200, message: '连接成功：基础地址返回 HTTP 200' })
    }
    if (req.method() === 'GET') return json(route, { items: state.endpoints })
    state.endpointWrites.push({ method: req.method(), body: req.postDataJSON?.() ?? null })
    if (req.method() === 'PATCH') {
      const ep = state.endpoints.find((e) => e.id === parts[3])
      Object.assign(ep, req.postDataJSON(), { version: ep.version + 1 })
      return json(route, ep)
    }
    return json(route, { ok: true })
  })
  await page.route(/\/api\/auth\/settings$/, (route) => {
    if (route.request().method() === 'PUT') { state.settings.push(route.request().postDataJSON()); return json(route, { ok: true }) }
    return json(route, { mode: 'off', env_locked: false, oidc: {}, user_can_chat: false })
  })
  await page.route(/\/api\/users$/, (route) => json(route, { items: [] }))
  await page.route(/\/api\/groups$/, (route) => json(route, { items: [{ id: 'g1', name: 'finance', label: '财务', source: 'oidc', members: ['li'] }] }))
  await page.route(/\/api\/audit(\?.*)?$/, (route) => json(route, { items: [
    { id: 'a1', at: '2026-10-10T02:00:00Z', actor: '张工', verified: false, action: 'capability.govern', subject_kind: 'capability',
      subject_id: 'erp_pay', data: { version: 2 } }] }))
  await page.route(/\/api\/notify\/channels$/, (route) => {
    const req = route.request()
    if (req.method() === 'POST') {
      const body = req.postDataJSON()
      state.channelWrites.push(body)
      return json(route, { id: 'ch-new', owner_id: null, url_host: '', signed: false, enabled: true, ...body }, 201)
    }
    return json(route, { items: notify?.items ?? NOTIFY_ITEMS, ...(notify?.mail ? { mail: notify.mail } : {}) })
  })
  await page.route(/\/api\/notify\/deliveries(\?.*)?$/, (route) => json(route, { items: deliveries }))

  await page.goto(`${WEB}${path}`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(300)
  return { page, state, errors }
}

const writes = []
const browser = await chromium.launch({ executablePath: CHROME })

// ================================================================
await section('能力列表：效果、筛选、同步', async () => {
  const { page, state, errors } = await open('/tools/capabilities')
  // 「能力」页签里还有「全部能力 / 待审阅」两个子页签（D-i），按名字整字匹配
  check('工具页有「能力」页签', await page.getByRole('tab', { name: '能力', exact: true }).count() === 1,
    (await page.locator('[role=tab]').allInnerTexts()).join(' | '))
  check('列出全部能力', await page.locator('[data-capability]').count() === CAPS.length)
  const pay = page.locator('[data-capability="erp_pay"] [data-capability-effect]')
  check('治理后的效果和同步得出的不一致时标出来', (await pay.innerText()).includes('写入') && (await pay.innerText()).includes('*'))
  check('没治理过的按同步得出的效果显示', (await page.locator('[data-capability="erp_query"] [data-capability-effect]').innerText()).includes('只读'))
  await page.locator('[data-capability="mcp:crm/find"]').click()
  // 右栏的治理设置是点开之后另取的：等它出来再判
  check('能力名带斜杠也能打开（地址转义）', await waitFor(page, async () =>
    await page.locator('[data-capability-editor="mcp:crm/find"]').count() === 1), page.url())
  await page.locator('[data-capability-sync]').click()
  check('立即同步', await waitFor(page, async () => state.syncs === 1))
  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))
})

await section('能力治理：带 version 保存；别人刚改过时载入最新的、不覆盖', async () => {
  const { page, state } = await open('/tools/capabilities/erp_pay', { conflict: true })
  const ed = page.locator('[data-capability-editor="erp_pay"]')
  await ed.waitFor()
  check('写入能力给无人值守写入的开关', await ed.locator('[data-capability-unattended]').count() === 1)
  await ed.locator('[data-capability-masked]').fill('/data/items/*/phone')
  await page.locator('[data-capability-save]').click()
  check('保存带上读到的 version', await waitFor(page, async () => state.governs.length === 1) && state.governs[0].body.version === 1,
    JSON.stringify(state.governs[0]?.body))
  check('遮罩字段一行一个，写成列表', JSON.stringify(state.governs[0]?.body.masked) === JSON.stringify(['/data/items/*/phone']))
  check('409：说明被别人改过，载入最新内容', await waitFor(page, async () => (await page.locator('body').innerText()).includes('刚被其他人修改过')))
  check('载入的是最新版本', await waitFor(page, async () => (await page.locator('[data-capability-editor="erp_pay"]').innerText()).includes('v2')))
})

// 查询能力由平台按登记的端点替它按幂等键去查：只能是同一端点上、绑定这个端点的只读能力。写记忆这类改平台自身状态的
// 内置工具不是只读，也不是端点上的读取，不能出现在候选里
await section('能力治理：查询能力只列同一端点上的只读能力', async () => {
  const { page, state } = await open('/tools/capabilities/erp_pay')
  for (const c of state.caps) if (['erp_pay', 'erp_query', 'mcp:crm/find'].includes(c.name)) c.endpoint_id = 'ep1'
  state.caps.push(cap('memory_write', { system: 'builtin', effect_system: 'dynamic' }), cap('memory_recall', { system: 'builtin' }),
    cap('crm_read', { system: 'crm', endpoint_id: 'ep2' }))
  await page.reload({ waitUntil: 'networkidle' })
  const ed = page.locator('[data-capability-editor="erp_pay"]')
  await ed.waitFor()
  const opts = await ed.locator('[data-capability-lookup] option').evaluateAll((els) => els.map((e) => e.value).filter(Boolean))
  check('只列同一端点上的只读能力：写记忆、别的端点、MCP、没绑端点的都不列', opts.join(',') === 'erp_query', opts.join(','))
})

await section('端点：两个环境、认证头只显示掩码、测试连接', async () => {
  const { page, state } = await open('/tools/endpoints')
  const ep = page.locator('[data-endpoint="erp"]')
  check('列出端点', await ep.count() === 1)
  check('认证头只显示掩码', (await ep.locator('[data-endpoint-headers]').innerText()).includes('****1234'))
  check('没有测试环境照实写', (await ep.locator('[data-endpoint-env="test"]').innerText()).includes('未配置'))
  await ep.locator('[data-endpoint-test="prod"]').click()
  check('测试连接按环境发', await waitFor(page, async () => state.tests.length === 1) && state.tests[0].env === 'prod', JSON.stringify(state.tests))
  check('2xx：成功，照服务端的话写', await waitFor(page, async () =>
    (await page.locator('[data-toast="ok"]').last().innerText().catch(() => '')).includes('连接成功：基础地址返回 HTTP 200')))
  // 能连上但基础地址回 404：不是「连接成功（HTTP 404）」，是提醒，照服务端的话写清楚
  state.probe = { ok: false, reachable: true, status: 404, message: '能连上，但对方系统上没有这个地址（HTTP 404）' }
  await ep.locator('[data-endpoint-test="prod"]').click()
  const warned = await waitFor(page, async () =>
    (await page.locator('[data-toast="warn"]').last().innerText().catch(() => '')).includes('能连上，但对方系统上没有这个地址（HTTP 404）'))
  check('能连上但 404：提醒，不说连接成功', warned
    && !(await page.locator('[data-toast="warn"]').last().innerText()).includes('连接成功'))
  await ep.getByRole('button', { name: '编辑' }).click()
  const ed = page.locator('[data-endpoint-editor]')
  await ed.waitFor()
  check('关登录时（所有人等同管理员）允许访问内网可以开', await ed.locator('[data-endpoint-intranet="prod"]').isEnabled())
  // 认证头名称空着不让存：存成 {"": ""} 之后这个环境的每个请求都发不出去（V2-04）
  const prod = ed.locator('[data-endpoint-env-editor="prod"]')
  await prod.getByRole('button', { name: '添加认证头' }).click()
  check('新加一行、名称空着：标红并说明，不让保存', await prod.locator('[data-endpoint-header-error]').innerText().catch(() => '') === '请填写请求头名称，或删掉这一行'
    && await page.locator('[data-endpoint-save]').isDisabled())
  const names = prod.getByRole('textbox', { name: '请求头名称', exact: true })
  await names.last().fill('X Token')
  check('名称带空格：说明只能用哪些字符，仍不让保存', (await prod.locator('[data-endpoint-header-error]').innerText()).includes('只能用英文字母')
    && await page.locator('[data-endpoint-save]').isDisabled())
  await names.last().fill('authorization')
  check('和已有的重名（不分大小写）：标出来', (await prod.locator('[data-endpoint-header-error]').innerText()) === '名称重复'
    && await page.locator('[data-endpoint-save]').isDisabled())
  await prod.getByRole('button', { name: '删除请求头 authorization', exact: true }).click()
  check('删掉那一行：可以保存', await prod.locator('[data-endpoint-header-error]').count() === 0 && await page.locator('[data-endpoint-save]').isEnabled())
  await page.locator('[data-endpoint-save]').click()
  check('不改认证头：原样提交掩码值（服务端认作保持原值）', await waitFor(page, async () => state.endpointWrites.length === 1)
    && state.endpointWrites[0].body?.environments?.prod?.headers?.Authorization === 'Bearer ****1234'
    && Object.keys(state.endpointWrites[0].body?.environments?.prod?.headers ?? {}).length === 1
    && state.endpointWrites[0].body?.version === 3, JSON.stringify(state.endpointWrites[0]?.body))
})

await section('设置：身份、通知、审计', async () => {
  const { page, state } = await open('/settings/identity')
  const tabs = await page.locator('[role=tab]').allInnerTexts()
  check('设置页有身份、通知、审计三个页签', ['身份', '通知', '审计'].every((t) => tabs.some((x) => x.includes(t))), tabs.join(' | '))
  check('登录方式三选一', await page.locator('[data-identity-mode-option]').count() === 3)
  await page.locator('[data-identity-mode-option="local"]').click()
  check('从关闭切到本地账号：要求同时建出第一个管理员', await page.locator('[data-identity-bootstrap]').count() === 1)
  check('没填管理员不能保存', await page.locator('[data-identity-save]').isDisabled())
  check('单点登录同步来的组只读（没有编辑按钮）', await page.locator('[data-group="finance"] button', { hasText: '编辑' }).count() === 0)
  check('还没有保存任何设置', state.settings.length === 0)

  await page.goto(`${WEB}/settings/notify`, { waitUntil: 'networkidle' })
  const ch = page.locator('[data-notify-channel="财务群"]')
  check('通知渠道只显示主机名', await ch.count() === 1 && (await ch.innerText()).includes('open.feishu.cn'))

  await page.goto(`${WEB}/settings/audit`, { waitUntil: 'networkidle' })
  const row = page.locator('[data-audit-row="capability.govern"]')
  check('审计照实标自报署名', await row.count() === 1 && (await row.innerText()).includes('自报署名'))
})

await section('设置 → 通知：可订阅的事件照服务端登记列（总方案 X6）', async () => {
  // 列表从真后端的 GET /api/notify/events 来（GET 放行）：这种范围的渠道收得到的才列；从来推不到的（以前列着、一勾就 422
  // 的「待办升级」「动作已生效」……）和登记了还没推送的（评测、召回、待办汇总……）都不列
  const { page, errors } = await open('/settings/notify')
  await page.getByRole('button', { name: '添加渠道' }).first().click()
  const dlg = page.locator('[role="dialog"]')
  const listed = () => dlg.locator('[data-notify-event]').evaluateAll((els) => els.map((e) => e.getAttribute('data-notify-event')))
  const scope = async (value) => {
    await dlg.getByLabel('范围', { exact: true }).selectOption(value)
    return waitFor(page, async () => await dlg.locator(`[data-notify-events="${value}"]`).count() === 1)
  }
  const every = ['task.opened', 'run.finished', 'run.failed', 'action.unknown']
  // 第 3 波起：访问密钥快到期推给全局渠道（服务账号的密钥）和个人渠道（个人密钥），每天的待办汇总只推个人渠道
  check('全局渠道：推给全局渠道的几项', await waitFor(page, async () => await dlg.locator('[data-notify-events="global"]').count() === 1)
    && JSON.stringify(await listed()) === JSON.stringify([...every, 'review.done', 'key.expiring']), JSON.stringify(await listed()))
  const text = await dlg.innerText()
  check('从来推不到的不列', !['待办升级', '待办超时', '待办处理完', '动作失败', '动作已生效'].some((t) => text.includes(t)), text)
  check('登记了还没推送的不列', !['评测有退步', '评测失败', '数据源有变化要处理', '口径升版，答案的数变了']
    .some((t) => text.includes(t)), text)
  check('只推个人渠道的「每天的待办汇总」全局渠道不列', !text.includes('每天的待办汇总'), text)
  check('应用的渠道：不列只发给提交人的「我提交审核的应用有结果」', await scope('app')
    && JSON.stringify(await listed()) === JSON.stringify(every), JSON.stringify(await listed()))
  check('个人渠道：连同待办汇总、密钥快到期都列', await scope('user')
    && JSON.stringify(await listed()) === JSON.stringify([...every, 'review.done', 'task.digest', 'key.expiring']),
    JSON.stringify(await listed()))
  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))
})

const MAIL = { enabled: true, host: 'smtp.example.com', port: 587, security: 'starttls', username: 'agentlab', password: null,
  password_set: true, from_addr: 'agentlab@example.com', from_name: 'AgentLab', default_for_assignees: false }

await section('设置 → 通知：邮件服务一节、每天的待办汇总', async () => {
  const { page, state, errors } = await open('/settings/notify', { settings: { mail: MAIL, notify: { public_base_url: null, digest_at: '09:00' } },
    notify: { items: NOTIFY_ITEMS, mail: { enabled: true, account_email: null } } })
  const box = page.locator('[data-mail-section="edit"]')
  check('管理员看得到、改得了邮件服务', await waitFor(page, async () => await box.count() === 1)
    && await box.locator('[data-mail-field="host"]').inputValue() === 'smtp.example.com')
  check('口令只说设没设，输入框是空的', await box.locator('[data-mail-field="password"]').inputValue() === ''
    && (await box.innerText()).includes('已设置；留空表示不修改'))
  check('没改动时能发测试邮件', !(await box.locator('[data-mail-test]').isDisabled()))
  await box.locator('[data-mail-field="host"]').fill('mail.example.org')
  check('有没保存的改动时先保存再发', await box.locator('[data-mail-test]').isDisabled() && (await box.innerText()).includes('先保存再发测试邮件'))
  await box.locator('[data-mail-save]').click()
  check('保存交的是 mail 一组，口令没填就不带', await waitFor(page, async () => state.prefs.length === 1)
    && state.prefs[0].values.mail.host === 'mail.example.org' && !('password' in state.prefs[0].values.mail),
    JSON.stringify(state.prefs[0]))
  await box.locator('[data-mail-test]').click()
  check('测试邮件照服务端的话写「已交给邮件服务器」', await waitFor(page, async () => state.mailTests === 1)
    && await waitFor(page, async () => (await page.locator('body').innerText()).includes('已交给邮件服务器（发到')))
  check('写明退信不追踪', (await box.locator('[data-mail-handoff]').innerText()).includes('退信不追踪'))
  const digest = page.locator('[data-digest-section="edit"]')
  check('待办汇总的发送时间', await digest.locator('[data-digest-at]').inputValue() === '09:00')
  await digest.locator('[data-digest-at]').fill('17:30')
  await digest.locator('[data-digest-save]').click()
  check('只交汇总时间这一项', await waitFor(page, async () => state.prefs.length === 2)
    && JSON.stringify(state.prefs[1]) === JSON.stringify({ values: { notify: { digest_at: '17:30' } } }), JSON.stringify(state.prefs[1]))
  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))

  const builder = { mode: 'local', username: 'bea', display_name: '搭建者', role: 'builder', groups: [], verified: true }
  const ro = await open('/settings/notify', { me: builder, settings: { mail: MAIL, notify: { public_base_url: null, digest_at: '09:00' } } })
  const roBox = ro.page.locator('[data-mail-section="readonly"]')
  check('搭建者只看：输入框都不能改、没有保存和测试', await waitFor(ro.page, async () => await roBox.count() === 1)
    && await roBox.locator('[data-mail-field="host"]').isDisabled() && await roBox.locator('[data-mail-save]').count() === 0
    && await roBox.locator('[data-mail-test]').count() === 0 && (await roBox.innerText()).includes('只有管理员能修改邮件服务'))
})

await section('设置 → 通知：邮件格式、个人邮件渠道发到账号邮箱、投递记录', async () => {
  const off = await open('/settings/notify', { notify: { items: NOTIFY_ITEMS, mail: { enabled: false, account_email: null } } })
  await off.page.getByRole('button', { name: '添加渠道' }).first().click()
  const offDlg = off.page.locator('[role="dialog"]')
  const options = await offDlg.locator('[data-notify-format-select] option').allInnerTexts()
  check('邮件服务没启用：不列「邮件」，写明为什么', !options.includes('邮件')
    && (await offDlg.locator('[data-notify-mail-off]').innerText()).includes('邮件服务没有启用'), options.join('、'))

  const me = { mode: 'local', username: 'uma', display_name: '小乌', role: 'user', groups: [], verified: true, user_id: 'u-uma' }
  const items = [
    { id: 'm1', name: '我的邮件', scope: 'user', owner_id: 'u-uma', format: 'email', url_host: '', signed: false,
      events: ['task.opened'], enabled: true, mail_to: ['uma@example.com'] },
    { id: 'm2', name: '旧邮件', scope: 'user', owner_id: 'u-uma', format: 'email', url_host: '', signed: false,
      events: ['task.opened'], enabled: true, mail_to: [] },
  ]
  const deliveries = [
    { id: 'd1', channel_id: 'm1', event: 'task.opened', status: 'ok', http_status: null, error: null, format: 'email', created_at: '2026-10-15T02:00:00Z' },
  ]
  const { page, state, errors } = await open('/settings/notify', { me, deliveries,
    notify: { items, mail: { enabled: true, account_email: 'uma@example.com' } } })
  const row = page.locator('[data-notify-channel="我的邮件"]')
  check('个人邮件渠道写「发到账号邮箱 …」', await waitFor(page, async () => await row.count() === 1)
    && (await row.locator('[data-notify-target]').innerText()).includes('发到账号邮箱 uma@example.com'))
  check('账号没有邮箱的照实写发不出去', (await page.locator('[data-notify-channel="旧邮件"] [data-notify-target]').innerText())
    .includes('没有邮箱地址'))
  await page.getByRole('button', { name: '添加渠道' }).first().click()
  const dlg = page.locator('[role="dialog"]')
  await dlg.locator('[data-notify-format-select]').selectOption('email')
  check('个人邮件渠道不填地址，写发到账号邮箱', await dlg.locator('[data-notify-mail-to="account"]').count() === 1
    && (await dlg.locator('[data-notify-mail-to="account"]').innerText()).includes('发到账号邮箱 uma@example.com')
    && await dlg.locator('input[placeholder="https://"]').count() === 0)
  await dlg.getByLabel('名称').fill('待办邮件')
  await dlg.locator('[data-notify-save]').click()
  check('建的时候不带地址', await waitFor(page, async () => state.channelWrites.length === 1)
    && state.channelWrites[0].format === 'email' && state.channelWrites[0].scope === 'user' && !('url' in state.channelWrites[0]),
    JSON.stringify(state.channelWrites[0]))
  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))
  const admin = await open('/settings/notify', { deliveries, notify: { items: NOTIFY_ITEMS, mail: { enabled: true, account_email: null } } })
  const d = admin.page.locator('[data-delivery-format="email"]')
  check('投递记录里邮件成功写「已交给邮件服务器」，不写已送达', await waitFor(admin.page, async () => await d.count() === 1)
    && (await d.innerText()).includes('已交给邮件服务器') && !(await d.innerText()).includes('已送达'))
})

await section('能力：待审阅复用数据目录的审阅队列，成批确认逐项带能力的治理版本', async () => {
  const { page, state, errors } = await open('/tools/capabilities?view=review')
  check('左栏换成待审阅视图', await page.locator('[data-capability-views="review"]').count() === 1)
  const rows = page.locator('[data-review-item]')
  check('列出待审阅的能力字段', await waitFor(page, async () => await rows.count() === 2), String(await rows.count()))
  check('位置照服务端的说法（返回字段 /amount 的单位）', (await rows.first().locator('[data-review-where]').innerText()).includes('返回字段 /amount 的单位'))
  check('使用次数照能力说', (await rows.first().innerText()).includes('被 12 次运行用过'))
  check('不分「影响数字的项 / 名称和含义」', await page.locator('[data-review-kinds]').count() === 0)
  await page.locator('[data-review-select-all]').check()
  await page.locator('[data-review-action="confirm"]').click()
  await page.locator('[role="dialog"]').getByRole('button', { name: /确认 2 项/ }).click()
  check('成批确认：一次请求、逐项带能力名、位置、字段和治理版本', await waitFor(page, async () => state.batches.length === 1)
    && JSON.stringify(state.batches[0].items.map((i) => [i.capability, i.pointer, i.field, i.action, i.if_version]))
      === JSON.stringify([['erp_pay', '/amount', 'unit', 'confirm', 4], ['erp_query', '/items/*/name', 'label', 'confirm', 2]]),
    JSON.stringify(state.batches[0]))
  check('冲突的那项照实标出（说的是能力，不是表）', await waitFor(page, async () =>
    (await page.locator('[data-review-summary]').innerText()).includes('所在能力刚被修改过')))
  await rows.first().locator('[data-review-open]').click()
  check('点开一项：右栏是这个能力的治理设置，标出那个位置', await waitFor(page, async () =>
    await page.locator('[data-capability-editor="erp_pay"] [data-capability-focus="/amount"]').count() === 1), page.url())
  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))
})

await section('自定义工具：绑定登记的端点，只填路径', async () => {
  const { page, state } = await open('/tools/custom')
  await page.getByRole('button', { name: '新建工具' }).first().click()
  const dlg = page.locator('[role="dialog"]')
  await dlg.locator('input[placeholder="query_order_status"]').fill('erp_order')
  await dlg.locator('[data-custom-tool-endpoint]').selectOption('ep1')
  check('绑定后只填路径，不再要完整地址', await dlg.locator('[data-custom-tool-path]').count() === 1
    && await dlg.locator('input[placeholder^="https://api.example.com"]').count() === 0)
  await dlg.locator('[data-custom-tool-path]').fill('/orders/{{ order_id }}')
  await dlg.getByRole('button', { name: '保存', exact: true }).click()
  const body = (await waitFor(page, async () => state.tools.length === 1)) ? state.tools[0].body : null
  check('保存的配置是 {endpoint, method, path, query}，没有地址和请求头', body?.config?.endpoint === 'ep1'
    && body.config.path === '/orders/{{ order_id }}' && !('url' in body.config) && !('headers' in body.config), JSON.stringify(body?.config))
})

await section('MCP：认证头按掩码编辑；本地子进程只有管理员能建、能改', async () => {
  const builder = { mode: 'local', username: 'li', display_name: '李工', role: 'builder', groups: [], verified: true }
  const { page, state } = await open('/tools/mcp', { me: builder })
  await page.locator('article', { hasText: 'files' }).getByRole('button', { name: '编辑' }).click()
  let dlg = page.locator('[role="dialog"]')
  check('搭建者打开本地子进程服务：只读，说明原因，没有保存', await dlg.locator('[data-mcp-stdio-locked]').count() === 1
    && await dlg.getByRole('button', { name: '保存', exact: true }).count() === 0)
  await dlg.getByRole('button', { name: '取消' }).click()
  await page.locator('article', { hasText: 'crm' }).getByRole('button', { name: '编辑' }).click()
  dlg = page.locator('[role="dialog"]')
  check('认证头显示掩码', await dlg.locator('[data-mcp-header-value]').first().inputValue() === 'Bearer ****9a2c')
  check('传输方式里本地子进程不可选', await dlg.locator('option[value="stdio"]').isDisabled())
  await dlg.getByRole('button', { name: '保存', exact: true }).click()
  check('不改认证头：原样提交掩码（服务端认作保持原值）', await waitFor(page, async () => state.mcp.length === 1)
    && state.mcp[0].body?.headers?.Authorization === 'Bearer ****9a2c', JSON.stringify(state.mcp[0]?.body))
})

await section('身份：单点登录映射管理员，用户或组至少一项', async () => {
  const { page } = await open('/settings/identity')
  await page.locator('[data-identity-mode-option="oidc"]').click()
  check('有「映射为管理员的用户」', await page.locator('[data-identity-admin-users]').count() === 1)
  check('用户和组都没填：说明至少一项，不能保存', (await page.locator('[data-identity-oidc]').innerText()).includes('至少要配置一个映射为管理员的用户或组')
    && await page.locator('[data-identity-save]').isDisabled())
  await page.locator('[data-identity-admin-users]').fill('admin')
  check('只填了管理员用户：可以保存', await page.locator('[data-identity-save]').isEnabled())
})

await section('设置：开了登录的使用者只剩个人的主题和通知，换主题不写服务端', async () => {
  const user = { mode: 'local', username: 'wang', display_name: '王五', role: 'user', groups: [], verified: true }
  const { page, state, errors } = await open('/settings/identity', { me: user })
  const tabs = (await page.locator('[role=tab]').allInnerTexts()).map((t) => t.trim())
  check('页签只剩偏好设置和通知', tabs.length === 2 && tabs.some((t) => t.includes('偏好')) && tabs.some((t) => t.includes('通知')), tabs.join(' | '))
  check('只给管理员的页签地址纠正到偏好设置', new URL(page.url()).pathname === '/settings/prefs', page.url())
  check('偏好只有主题，没有署名和运行默认值', await page.locator('[data-prefs-personal]').count() === 1
    && await page.locator('#pref-actor').count() === 0 && await page.locator('[data-prefs-shared]').count() === 0)
  await page.getByRole('radio', { name: /浅色/ }).click()
  await page.waitForTimeout(300)
  check('换主题只存本机，不往服务端写', state.prefs.length === 0, JSON.stringify(state.prefs))
  check('说明主题只保存在当前浏览器', (await page.locator('body').innerText()).includes('主题只保存在当前浏览器中'))
  check('没有报错提示', !(await page.locator('body').innerText()).includes('未能保存到设置'))
  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))
})

check('检查没有写库（兜底拦下的写请求）', writes.length === 0, writes.slice(0, 4).join('、'))
console.log(failed ? `\n${failed} 项未通过` : '\n全部通过')
await browser.close()
process.exit(failed ? 1 : 0)
