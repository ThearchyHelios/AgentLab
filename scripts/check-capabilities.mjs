// 工具页的「能力」「端点」页签和设置页的「身份」「通知」「审计」的检查（应用平台 A6、A7）。
//
// 守的是：
//   - 能力列表标出读写效果，同步得出和治理后不一致时标出来；治理带 version 保存（乐观锁），409 时载入最新的、不覆盖；
//   - 端点的认证头只显示掩码，提交掩码值表示保持原值；允许访问内网只有管理员能开；测试连接按环境发；
//   - 关登录（默认）时所有人等同管理员：治理、退役、身份设置都能改；
//   - 设置页的身份、通知、审计三个页签在，通知渠道的地址只显示主机名。
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

async function open(path, { conflict = false } = {}) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  opened.add(ctx)
  const page = await ctx.newPage()
  page.setDefaultTimeout(8000)
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  const json = (route, body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
  const state = { caps: JSON.parse(JSON.stringify(CAPS)), endpoints: JSON.parse(JSON.stringify(ENDPOINTS)), governs: [], endpointWrites: [],
    tests: [], syncs: 0, settings: [] }

  await page.route(/\/api\//, (route) => {
    const req = route.request()
    if (req.method() === 'GET') return route.continue()
    writes.push(`${req.method()} ${new URL(req.url()).pathname}`)
    return json(route, { detail: '检查脚本不写库' }, 409)
  })
  await page.route(/\/api\/auth\/me$/, (route) => json(route, { mode: 'off', username: null, display_name: null, role: 'admin', groups: [], verified: false }))
  await page.route(/\/api\/capabilities(\?.*)?$/, (route) => json(route, { items: state.caps }))
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
      return json(route, { ok: true, status: 200, message: '' })
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
  await page.route(/\/api\/notify\/channels$/, (route) => json(route, { items: [
    { id: 'ch1', name: '财务群', scope: 'global', owner_id: null, format: 'feishu', url_host: 'open.feishu.cn', signed: true,
      events: ['task.opened'], enabled: true }] }))
  await page.route(/\/api\/notify\/deliveries(\?.*)?$/, (route) => json(route, { items: [] }))

  await page.goto(`${WEB}${path}`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(300)
  return { page, state, errors }
}

const writes = []
const browser = await chromium.launch({ executablePath: CHROME })

// ================================================================
await section('能力列表：效果、筛选、同步', async () => {
  const { page, state, errors } = await open('/tools/capabilities')
  check('工具页有「能力」页签', await page.locator('[role=tab]', { hasText: '能力' }).count() === 1)
  check('列出全部能力', await page.locator('[data-capability]').count() === CAPS.length)
  const pay = page.locator('[data-capability="erp_pay"] [data-capability-effect]')
  check('治理后的效果和同步得出的不一致时标出来', (await pay.innerText()).includes('写入') && (await pay.innerText()).includes('*'))
  check('没治理过的按同步得出的效果显示', (await page.locator('[data-capability="erp_query"] [data-capability-effect]').innerText()).includes('只读'))
  await page.locator('[data-capability="mcp:crm/find"]').click()
  check('能力名带斜杠也能打开（地址转义）', await page.locator('[data-capability-editor="mcp:crm/find"]').count() === 1)
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

await section('端点：两个环境、认证头只显示掩码、测试连接', async () => {
  const { page, state } = await open('/tools/endpoints')
  const ep = page.locator('[data-endpoint="erp"]')
  check('列出端点', await ep.count() === 1)
  check('认证头只显示掩码', (await ep.locator('[data-endpoint-headers]').innerText()).includes('****1234'))
  check('没有测试环境照实写', (await ep.locator('[data-endpoint-env="test"]').innerText()).includes('未配置'))
  await ep.locator('[data-endpoint-test="prod"]').click()
  check('测试连接按环境发', await waitFor(page, async () => state.tests.length === 1) && state.tests[0].env === 'prod', JSON.stringify(state.tests))
  await ep.getByRole('button', { name: '编辑' }).click()
  const ed = page.locator('[data-endpoint-editor]')
  await ed.waitFor()
  check('关登录时（所有人等同管理员）允许访问内网可以开', await ed.locator('[data-endpoint-intranet="prod"]').isEnabled())
  await page.locator('[data-endpoint-save]').click()
  check('不改认证头：原样提交掩码值（服务端认作保持原值）', await waitFor(page, async () => state.endpointWrites.length === 1)
    && state.endpointWrites[0].body?.environments?.prod?.headers?.Authorization === 'Bearer ****1234'
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

check('检查没有写库（兜底拦下的写请求）', writes.length === 0, writes.slice(0, 4).join('、'))
console.log(failed ? `\n${failed} 项未通过` : '\n全部通过')
await browser.close()
process.exit(failed ? 1 : 0)
