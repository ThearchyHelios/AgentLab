// 待办箱与待办卡（/apps?tab=inbox、/apps/inbox/:taskId，run/TaskCard）的回归检查。
//
// 守的是这些「看着没坏、其实不对」的地方：
//   - 会签进度：派给了谁、谁同意了、按策略还差几人（全体同意 / 至少 m 人），撤回的派单淡显在最后；
//   - 时限与升级：剩多久、超了多久（红）、升级过几次；悬停写明超时只会提醒、升级或驳回，不会自动批准；
//   - 动作确认的哈希卡：遮罩后的预览、内容哈希只写前 8 位（完整的在悬停里）、环境（生产环境醒目）；确认时把哈希原样
//     带回（payload_hash 一字不差），内容不能在这里改；缺哈希时不让确认；
//   - 核实待办：三个选项（已生效 + 外部单号 / 未生效重新发送 / 未生效放弃），没选不提交，选了已生效不填单号不提交；
//     提交的结论和单号照约定交给待办接口；
//   - 等外部系统（signal）：只有说明和关联键，没有批准、驳回；
//   - 不是派给我的（can_respond 为假）：只读，写明原因；同意过、还在等别人的写「你已处理：同意」，不写「没有派给你」；
//   - 升级之后：升级加派的那一份不算进会签人数，另写升级后的规则（升级给谁、其中一人说了算），名单里标「升级」；
//     待办箱一行写所属实例，同标题的分得开；
//   - 补充输入带表单的：本地校验，服务端 422 的错误挂回字段；表单里的选人控件按这条待办的候选池取候选（使用者读不到
//     整份名录），不在池里的服务端 422（code=principal）挂回那个字段（05-已知遗留 (b)）；
//   - 关登录时签批人一行写本机署名；会签只记下一票（resolved 为假）时提示「等待其他人处理」；
//   - 审核个人应用（kind=review，payload 同服务端 blueprints/personal.review_payload）：写提交人（显示名）、目的、说明；没有
//     「同意」——「取副本，去发布」调 POST /api/blueprints/{id}/copy 后进副本蓝图；取过的给「打开我的副本」（只认自己取的那
//     一份，别人取的不算）；「退回」可以写原因，走待办接口 decision=reject。
//
// 接口全部在浏览器层答掉（/api/tasks/**），数据是这里虚构的；写请求记在探针里。
//
//   node scripts/check-inbox.mjs
// 对别的实例跑时带上地址：AGENTLAB_WEB=http://localhost:<前端端口> node scripts/check-inbox.mjs
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

const ago = (ms) => new Date(Date.now() - ms).toISOString()
const HASH = '9c1e44ab07d2f3a1c4b5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b'

const assignment = (id, principal, status, decision = null, extra = {}) => ({
  id, principal_kind: 'user', principal, source: 'slot:finance', status, responded_by: status === 'responded' ? principal : null,
  responded_at: status === 'responded' ? ago(600_000) : null, decision, verified: false, ...extra,
})

const base = {
  approval_id: null, run_id: 'run_claim_1', node_id: 'finance', app_id: 'app_claim', app_name: '报销审批', app_version: 2,
  workflow_name: '报销流程', form: null, policy: { kind: 'any' }, status: 'pending', due_at: null, escalation_level: 0,
  payload: {}, assignments: [assignment('a0', 'zhangsan', 'pending')], can_respond: true, outcome: null, resolved_by: null,
  resolved_at: null, created_at: ago(1800_000),
}

const TASKS = {
  signoff: {
    ...base, id: 'signoff', approval_id: 'ap1', kind: 'approve', title: '财务会签', policy: { kind: 'quorum', m: 2 },
    due_at: ago(2 * 3600_000), escalation_level: 1, node_label: '财务审批',
    payload: { kind: 'human_node', message: '张三报销差旅 1,200 元' },
    assignments: [
      assignment('a1', 'lisi', 'responded', 'approve'),
      assignment('a2', 'wangwu', 'pending'),
      assignment('a3', 'zhaoliu', 'pending'),
      assignment('a4', 'qianqi', 'withdrawn'),
    ],
  },
  confirm: {
    ...base, id: 'confirm', approval_id: 'ap2', kind: 'action_confirm', title: '确认付款', node_id: 'pay',
    payload: { kind: 'action_confirm', capability: 'erp_pay', tool: 'erp_pay', environment: 'prod', payload_hash: HASH, attempt: 1,
      preview: { 收款人: '张三', 金额: 1200, 银行账号: '已遮罩' } },
  },
  nohash: {
    ...base, id: 'nohash', approval_id: 'ap3', kind: 'action_confirm', title: '确认付款（缺哈希）', node_id: 'pay',
    payload: { kind: 'action_confirm', capability: 'erp_pay', environment: 'test', preview: {} },
  },
  reconcile: {
    ...base, id: 'reconcile', kind: 'reconcile', title: '核实写入结果：erp_pay', node_id: 'pay',
    payload: { kind: 'reconcile', action_id: 'act1', capability: 'erp_pay', environment: 'prod', payload_hash: HASH, attempt: 1,
      idem_key: `${HASH.slice(0, 16)}:1`, state: 'unknown', error: '请求超时', request_at: ago(3600_000), preview: { 金额: 1200 } },
  },
  signal: {
    ...base, id: 'signal', kind: 'signal', title: '等付款回调', can_respond: false,
    assignments: [{ ...assignment('h1', 'paid', 'pending'), principal_kind: 'hook' }],
    payload: { kind: 'signal', correlation: 'order-8812', hook: 'paid' },
  },
  readonly: {
    ...base, id: 'readonly', approval_id: 'ap4', kind: 'approve', title: '总监审批', can_respond: false,
    payload: { kind: 'human_node', message: '金额超过一万，需要总监批准' },
  },
  // 同意过、还在等别人：不能再回复，写「你已处理：同意」，不写「没有派给你」
  voted: {
    ...base, id: 'voted', approval_id: 'ap6', kind: 'approve', title: '财务会签（已同意）', policy: { kind: 'all' },
    can_respond: false, my_response: { decision: 'approve', at: ago(600_000), verified: true },
    assignments: [assignment('v1', 'zhangsan', 'responded', 'approve', { verified: true }), assignment('v2', 'wangwu', 'pending')],
    payload: { kind: 'human_node', message: '钱七报销 1,280.5 元' },
  },
  // 超时升级之后：升级加派的那一份不算进会签人数（还是 0/2），另写升级给了谁、其中一人说了算
  escalated: {
    ...base, id: 'escalated', approval_id: 'ap7', kind: 'approve', title: '报销审批：钱七 · 300 元', policy: { kind: 'all' },
    due_at: ago(600_000), escalation_level: 1, instance_title: '报销审批·钱七·第二笔', node_label: '财务会签',
    assignments: [assignment('e1', 'zhangsan', 'pending'), assignment('e2', 'wangwu', 'pending'),
      assignment('e3', 'zhaoliu', 'pending', null, { source: 'escalation' })],
    progress: { policy: 'all', voters: 2, need: 2, agreed: 0, rejected: 0,
      escalation: { slot: 'director', assignees: [{ kind: 'user', name: 'zhaoliu' }], decided: null, by: null } },
    payload: { kind: 'human_node', message: '钱七报销 300 元' },
  },
  // 审核个人应用：不挂在中断上（node_id 为空），派给处理审核的人；payload 同服务端 blueprints/personal.review_payload
  review: {
    ...base, id: 'review', kind: 'review', title: '审核个人应用「我的退款周报」', node_id: null, app_id: 'app_mine',
    app_name: '我的退款周报', app_version: null, workflow_name: null, run_id: 'run_p1',
    assignments: [assignment('r1', '审批人甲', 'pending', null, { source: 'review' })],
    payload: { kind: 'review', blueprint_id: 'bp_mine', app_id: 'app_mine', name: '我的退款周报', purpose: '每周看自己门店的退款',
      template_id: 'periodic_board', submitted_by: 'zhangsan', note: '想给门店组的人都用', copies: [] },
    people: { zhangsan: '张三', 审批人甲: '审批人甲' },
  },
  // 已经取过副本：自己取的那一份给「打开我的副本」；别的处理人取的不算
  reviewed: {
    ...base, id: 'reviewed', kind: 'review', title: '审核个人应用「门店日报」', node_id: null, app_id: 'app_daily',
    app_name: '门店日报', app_version: null, workflow_name: null, run_id: 'run_p2',
    assignments: [assignment('r2', '审批人甲', 'pending', null, { source: 'review' })],
    payload: { kind: 'review', blueprint_id: 'bp_daily', app_id: 'app_daily', name: '门店日报', purpose: '每天看门店的销售',
      template_id: 'periodic_board', submitted_by: 'zhangsan', note: '', copies: [
        { id: 'bp_daily_other', owner: 'lisi', status: 'drafting', app_id: null },
        { id: 'bp_daily_mine', owner: '审批人甲', status: 'trialed', app_id: null }] },
    people: { zhangsan: '张三' },
  },
  // 补充输入里选复核人（选人控件）：使用者按这条待办的候选池选
  pick: {
    ...base, id: 'pick', approval_id: 'ap8', kind: 'input', title: '指定复核人',
    form: { format: 'agentlab.form/1', type: 'object', required: ['reviewer'], properties: {
      reviewer: { type: 'string', title: '复核人', 'x-widget': 'principal' } } },
    payload: { kind: 'human_node', message: '请指定复核人' },
  },
  input: {
    ...base, id: 'input', approval_id: 'ap5', kind: 'input', title: '补填发票信息',
    form: { format: 'agentlab.form/1', type: 'object', required: ['invoice'], properties: {
      invoice: { type: 'string', title: '发票号', maxLength: 20 }, date: { type: 'string', title: '开票日期', format: 'date' } } },
    payload: { kind: 'human_node', message: '请补填发票号' },
  },
}

const browser = await chromium.launch({ executablePath: CHROME })

async function open(state) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } })
  opened.add(ctx)
  const page = await ctx.newPage()
  page.errors = []
  page.on('pageerror', (e) => page.errors.push(e.message))
  await page.addInitScript(() => localStorage.setItem('agentlab_actor', '审批人甲'))
  await page.route((url) => url.pathname.startsWith('/api/'), (route) => {
    const r = route.request()
    const p = new URL(r.url()).pathname
    if (r.method() !== 'GET') {
      const body = r.postData() ? JSON.parse(r.postData()) : null
      state.writes.push({ method: r.method(), path: p, body })
      const m = p.match(/^\/api\/tasks\/([^/]+)\/respond$/)
      if (m) {
        if (m[1] === 'input' && state.reject422) {
          return route.fulfill({ status: 422, json: { detail: '表单没有通过校验', code: 'form_invalid', errors: [
            { path: 'invoice', code: 'max_length', message: '最多 20 个字' }] } })
        }
        if (m[1] === 'pick' && body?.values?.reviewer !== 'fin') {
          return route.fulfill({ status: 422, json: { detail: '表单没有通过校验', code: 'form_invalid', errors: [
            { path: 'reviewer', code: 'principal', message: `「${body?.values?.reviewer}」不在可选的人里，请从候选中选择` }] } })
        }
        const t = TASKS[m[1]]
        // 会签：这一票记下了，还没凑齐
        const resolved = m[1] !== 'signoff'
        return route.fulfill({ json: { task: { ...t, status: resolved ? 'resolved' : 'pending' }, resolved } })
      }
      // 审核待办「取副本，去发布」：服务端建一份归处理人的共享副本，交回副本蓝图
      if (p === '/api/blueprints/bp_mine/copy') return route.fulfill({ json: { id: 'bp_mine_copy', scope: 'shared', owner: '审批人甲' } })
      return route.fulfill({ status: 503, json: { detail: '检查脚本拦下了写请求' } })
    }
    if (p === '/api/auth/me') return route.fulfill({ json: state.me ?? { mode: 'off', username: null, display_name: null, role: 'admin', groups: [], verified: false } })
    if (p === '/api/directory/candidates') {
      const q = new URL(r.url()).searchParams
      ;(state.candidateCalls ??= []).push(Object.fromEntries(q))
      const people = [{ username: 'fin', display_name: '樊宁' }, { username: 'fay', display_name: '费伊' }]
      return route.fulfill({ json: { users: people.filter((u) => u.username.startsWith(q.get('q') ?? '')), groups: [], truncated: false } })
    }
    if (p === '/api/directory') {
      state.directoryHits = (state.directoryHits ?? 0) + 1
      return route.fulfill({ status: 403, json: { detail: '需要搭建者权限' } })
    }
    // 进了副本蓝图之后的页面不在这里查：蓝图一律答「不存在」，不打到真实服务端
    if (p.startsWith('/api/blueprints/')) return route.fulfill({ status: 404, json: { detail: '蓝图不存在或已被删除' } })
    if (p === '/api/tasks/count') return route.fulfill({ json: { mine: Object.keys(TASKS).length, apps: Object.keys(TASKS).length, other: 0 } })
    if (p === '/api/tasks') return route.fulfill({ json: { items: Object.values(TASKS) } })
    const m = p.match(/^\/api\/tasks\/([^/]+)$/)
    if (m && TASKS[m[1]]) return route.fulfill({ json: TASKS[m[1]] })
    return route.continue()
  })
  return page
}

const card = (page) => page.locator('[data-task]').first()

await section('会签进度、时限、升级', async () => {
  const state = { writes: [] }
  const page = await open(state)
  await page.goto(`${WEB}/apps/inbox/signoff`)
  await card(page).waitFor({ timeout: 15000 })
  const s = page.locator('[data-signoffs]')
  check('至少 2 人同意：进度写 1/2', (await s.innerText()).includes('1/2 已同意') && (await s.getAttribute('data-signoffs-need')) === '2')
  check('策略写明', (await s.innerText()).includes('需 2 人同意'))
  check('每个派单一行，撤回的在最后', (await s.locator('li').count()) === 4
    && (await s.locator('li').last().getAttribute('data-assignment')) === 'withdrawn')
  const due = page.locator('[data-task-due]')
  check('超时写「已超时」并标红', (await due.getAttribute('data-task-due')) === 'overdue' && /已超时/.test(await due.innerText()))
  check('悬停写明不会自动批准', /不会自动批准/.test(await due.getAttribute('title')))
  check('升级过写明', (await page.locator('[data-task-escalated]').innerText()).includes('已升级'))
  check('卡头写节点名', (await card(page).innerText()).includes('节点「财务审批」'))
  check('关登录时签批人写本机署名', (await page.locator('[data-task-signer="actor"]').innerText()).includes('审批人甲'))
  await page.locator('[data-task-approve]').click()
  await page.waitForTimeout(300)
  const sent = state.writes.find((w) => w.path === '/api/tasks/signoff/respond')
  check('批准走待办接口，decision 是 approve', sent?.body?.decision === 'approve', JSON.stringify(sent?.body))
  check('只记下一票时提示等待其他人', await page.getByText('已记下你的意见，等待其他人处理').waitFor({ timeout: 3000 }).then(() => true, () => false))
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')
})

await section('动作确认的哈希卡', async () => {
  const state = { writes: [] }
  const page = await open(state)
  await page.goto(`${WEB}/apps/inbox/confirm`)
  const box = page.locator('[data-action-confirm]')
  await box.waitFor({ timeout: 15000 })
  check('哈希只写前 8 位', (await box.locator('[data-action-hash]').innerText()) === HASH.slice(0, 8))
  check('完整哈希在悬停里', (await box.locator('[data-action-hash]').getAttribute('title')).includes(HASH))
  check('环境：生产环境', (await box.locator('[data-action-env]').innerText()) === '生产环境')
  check('遮罩后的预览照写', (await box.locator('[data-action-preview]').innerText()).includes('已遮罩'))
  check('内容不能在这里改：没有输入框改参数', (await box.locator('textarea').count()) === 0)
  await box.locator('[data-action-confirm-send]').click()
  await page.waitForTimeout(300)
  const sent = state.writes.find((w) => w.path === '/api/tasks/confirm/respond')
  check('确认时哈希原样带回', sent?.body?.payload_hash === HASH && sent?.body?.decision === 'approve', JSON.stringify(sent?.body))
})

await section('动作确认：缺哈希不让确认', async () => {
  const page = await open({ writes: [] })
  await page.goto(`${WEB}/apps/inbox/nohash`)
  const box = page.locator('[data-action-confirm]')
  await box.waitFor({ timeout: 15000 })
  check('写明缺少内容哈希', (await box.innerText()).includes('缺少内容哈希'))
  check('确认按钮停用', await box.locator('[data-action-confirm-send]').isDisabled())
})

await section('核实待办：三个选项', async () => {
  const state = { writes: [] }
  const page = await open(state)
  await page.goto(`${WEB}/apps/inbox/reconcile`)
  const box = page.locator('[data-reconcile]')
  await box.waitFor({ timeout: 15000 })
  check('三个选项', (await box.locator('[data-reconcile-option]').count()) === 3)
  check('写明不会自动重发', (await box.innerText()).includes('不会自动重发'))
  await box.locator('[data-reconcile-submit]').click()
  check('没选不提交', !state.writes.length)
  await box.locator('[data-reconcile-option="confirmed"] input').check()
  await box.locator('[data-reconcile-submit]').click()
  check('选了已生效不填单号不提交', !state.writes.length)
  await box.locator('[data-reconcile-external]').fill('ERP-20261014-0001')
  await box.locator('[data-reconcile-submit]').click()
  await page.waitForTimeout(300)
  const sent = state.writes.find((w) => w.path === '/api/tasks/reconcile/respond')
  check('已生效 + 外部单号', sent?.body?.decision === 'confirmed' && sent?.body?.external_id === 'ERP-20261014-0001', JSON.stringify(sent?.body))
})

await section('等外部系统、只读', async () => {
  const page = await open({ writes: [] })
  await page.goto(`${WEB}/apps/inbox/signal`)
  await page.locator('[data-task-signal]').waitFor({ timeout: 15000 })
  check('写明由外部系统完成，带关联键', (await page.locator('[data-task-signal]').innerText()).includes('order-8812'))
  check('没有批准、驳回', (await page.locator('[data-task-approve], [data-task-reject]').count()) === 0)
  await page.goto(`${WEB}/apps/inbox/readonly`)
  await page.locator('[data-task-readonly]').waitFor({ timeout: 15000 })
  check('不是派给我的：只读并写明', (await page.locator('[data-task-readonly]').innerText()).includes('没有派给你'))
  check('没有批准、驳回', (await page.locator('[data-task-approve], [data-task-reject]').count()) === 0)
  await page.goto(`${WEB}/apps/inbox/voted`)
  await page.locator('[data-task-readonly="responded"]').waitFor({ timeout: 15000 })
  const voted = await page.locator('[data-task-readonly]').innerText()
  check('同意过的人再打开：写「你已处理：同意」，不写「没有派给你」', voted.includes('你已处理：同意') && !voted.includes('没有派给你'), voted)
})

await section('升级之后的会签进度', async () => {
  const page = await open({ writes: [] })
  await page.goto(`${WEB}/apps/inbox/escalated`)
  const s = page.locator('[data-signoffs]')
  await s.waitFor({ timeout: 15000 })
  const text = await s.innerText()
  check('升级加派的不算进会签人数：0/2，不是 0/3', text.includes('0/2 已同意') && (await s.getAttribute('data-signoffs-need')) === '2', text)
  check('另写升级后的规则：升级给谁、其中一人说了算',
    (await s.locator('[data-signoffs-escalation="open"]').innerText()).includes('已升级给岗位「director」：其中任一人同意即通过'))
  check('升级加派的那一份在名单里标「升级」', (await s.locator('li', { hasText: 'zhaoliu' }).innerText()).includes('升级'))
  await page.goto(`${WEB}/apps?tab=inbox`)
  const row = page.locator('[data-inbox-task="escalated"]')
  await row.waitFor({ timeout: 15000 })
  check('待办箱里也是 0/2', (await row.locator('[data-inbox-signoffs]').innerText()).includes('0/2 已同意'))
  check('待办一行写所属实例（同标题的分得开）', (await row.locator('[data-inbox-meta]').innerText()).includes('报销审批·钱七·第二笔'))
})

await section('补充输入：表单校验、422 挂回字段', async () => {
  const state = { writes: [] }
  const page = await open(state)
  await page.goto(`${WEB}/apps/inbox/input`)
  const form = page.locator('[data-schema-form]')
  await form.waitFor({ timeout: 15000 })
  await page.locator('[data-task-approve]').click()
  check('必填没填本地拦下', !state.writes.length && (await form.locator('[data-field="invoice"] [data-field-error="required"]').count()) === 1)
  await form.locator('[data-field="invoice"] input').fill('FP-001')
  await form.locator('[data-field="date"] input').fill('2026-10-14')
  state.reject422 = true
  await page.locator('[data-task-approve]').click()
  await form.locator('[data-field-error="max_length"]').waitFor({ timeout: 3000 }).catch(() => {})
  check('服务端 422 挂回字段', (await form.locator('[data-field="invoice"] [data-field-error="max_length"]').count()) === 1)
  const sent = state.writes.find((w) => w.path === '/api/tasks/input/respond')
  check('提交的是表单的值，decision 是 submit', sent?.body?.decision === 'submit' && sent?.body?.values?.invoice === 'FP-001', JSON.stringify(sent?.body))
})

await section('补充输入里选人：按待办的候选池取候选，不在池里 422 挂回字段（05-已知遗留 (b)）', async () => {
  const state = { writes: [], me: { mode: 'local', username: 'zhangsan', display_name: '张三', role: 'user', groups: [], verified: true } }
  const page = await open(state)
  await page.goto(`${WEB}/apps/inbox/pick`)
  const field = page.locator('[data-schema-form] [data-field="reviewer"]')
  await field.waitFor({ timeout: 15000 })
  const input = field.locator('input.field')
  await input.fill('f')
  await page.waitForFunction(() => document.querySelectorAll('[data-field="reviewer"] datalist option').length > 1, null,
    { timeout: 5000 }).catch(() => {})
  const asked = (state.candidateCalls ?? []).at(-1) ?? {}
  check('按这条待办、这个字段问候选（purpose=form、task_id）', asked.purpose === 'form' && asked.task_id === 'pick'
    && asked.field === 'reviewer' && !asked.app_id, JSON.stringify(asked))
  check('……列出池里的人（只有名字），不去取整份名录', (await field.locator('datalist option').count()) === 2 && !state.directoryHits)
  // 候选接口一时取不到时先收下（提交时服务端判）：这里绕过前端的提前核对，直接看服务端 422 怎么挂回来
  await page.route('**/api/directory/candidates**', (route) => route.abort())
  await input.fill('wangwu')
  await input.press('Enter')
  await page.locator('[data-task-approve]').click()
  await field.locator('[data-field-error="principal"]').waitFor({ timeout: 5000 }).catch(() => {})
  check('服务端 422（code=principal）挂回选人字段', (await field.locator('[data-field-error="principal"]').count()) === 1
    && (await field.locator('[data-field-error="principal"]').innerText()).includes('不在可选的人里'))
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')
})

await section('审核个人应用：取副本去发布、打开我的副本、退回', async () => {
  const state = { writes: [] }
  const page = await open(state)
  await page.goto(`${WEB}/apps/inbox/review`)
  const box = page.locator('[data-task-review]')
  await box.waitFor({ timeout: 15000 })
  const text = await box.innerText()
  check('写提交人（显示名）、目的、提交说明', text.includes('提交人：张三') && text.includes('每周看自己门店的退款')
    && text.includes('想给门店组的人都用'), text.replace(/\s+/g, ' '))
  check('同意不在这里点：没有「同意」「驳回」按钮，也不写「没有派给你」',
    await page.locator('[data-task-approve], [data-task-reject]').count() === 0 && !(await card(page).innerText()).includes('没有派给你'))
  check('没取过副本：给「取副本，去发布」，没有「打开我的副本」', await box.locator('[data-task-review-copy]').count() === 1
    && await box.locator('[data-task-review-open]').count() === 0)
  // 待办挂在提交人那次个人应用的刷新上，只有拥有者读得到：不给「打开实例」「打开运行记录」（链出去只会 404 / 403）
  const links = await card(page).locator('a[href]').evaluateAll((as) => as.map((a) => a.getAttribute('href')))
  check('不给打不开的链接：没有指向提交人个人应用的实例页、也没有运行记录页', !links.some((h) => /\/apps\/app_mine\/i\/|^\/runs\//.test(h ?? ''))
    && !/查看实例|查看运行/.test(await card(page).innerText()), links.join(' , '))
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')
  await box.locator('[data-task-review-copy]').click()
  check('取副本：POST /api/blueprints/{原蓝图}/copy', await page.waitForURL(/\/blueprints\/bp_mine_copy$/, { timeout: 5000 }).then(() => true, () => false)
    && state.writes.some((w) => w.method === 'POST' && w.path === '/api/blueprints/bp_mine/copy'), page.url())
  check('……然后进副本蓝图（在副本上试运行、走发布单）', /\/blueprints\/bp_mine_copy$/.test(page.url()), page.url())
  check('取副本不回复待办（待办等发布单做完自动记为已发布）', !state.writes.some((w) => w.path === '/api/tasks/review/respond'))

  // 已经取过副本：只认自己取的那一份
  const p2 = await open({ writes: [] })
  await p2.goto(`${WEB}/apps/inbox/reviewed`)
  const box2 = p2.locator('[data-task-review]')
  await box2.waitFor({ timeout: 15000 })
  const openCopy = box2.locator('[data-task-review-open]')
  check('取过副本：给「打开我的副本」，链到自己取的那一份', await openCopy.count() === 1
    && (await openCopy.getAttribute('href')) === '/blueprints/bp_daily_mine', String(await openCopy.getAttribute('href').catch(() => '')))
  check('……别的处理人取的副本不算，也不再给「取副本」', await box2.locator('[data-task-review-copy]').count() === 0
    && !(await box2.innerHTML()).includes('bp_daily_other'))

  // 退回：可以写原因，走待办接口
  const s3 = { writes: [] }
  const p3 = await open(s3)
  await p3.goto(`${WEB}/apps/inbox/review`)
  const box3 = p3.locator('[data-task-review]')
  await box3.waitFor({ timeout: 15000 })
  await box3.getByRole('textbox', { name: '退回的原因（选填）' }).fill('口径和财务的对不上')
  await box3.locator('[data-task-review-reject]').click()
  await p3.waitForTimeout(300)
  const sent = s3.writes.find((w) => w.path === '/api/tasks/review/respond')
  check('退回：decision 是 reject，带上原因', sent?.body?.decision === 'reject' && sent?.body?.note === '口径和财务的对不上', JSON.stringify(sent?.body))
  check('退回后提示「已退回」', await p3.getByText('已退回').first().waitFor({ timeout: 3000 }).then(() => true, () => false))
  check('退回不取副本', !s3.writes.some((w) => w.path.endsWith('/copy')))
})

await section('待办箱：列表和徽标', async () => {
  const page = await open({ writes: [] })
  await page.goto(`${WEB}/apps?tab=inbox`)
  await page.locator('[data-inbox-task]').first().waitFor({ timeout: 15000 })
  check('每条待办一行，链到详情', (await page.locator('[data-inbox-task]').count()) === Object.keys(TASKS).length)
  check('审核个人应用的一行写种类「审核个人应用」', (await page.locator('[data-inbox-task="review"]').innerText()).includes('审核个人应用')
    && (await page.locator('[data-inbox-task="review"]').getAttribute('data-task-kind')) === 'review')
  check('「应用」导航上的徽标数应用里的待办', (await page.locator('[data-apps-badge]').innerText()).trim() === String(Object.keys(TASKS).length))
  check('页签上带数', /\d/.test(await page.getByRole('tab', { name: /待办/ }).innerText()))
})

await browser.close()
console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 全部通过')
process.exit(failed ? 1 : 0)
