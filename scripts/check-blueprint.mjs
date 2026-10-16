// 普通用户建应用的创建链路（docs/design/normal-user-apps.md 第四部分 §4.1–§4.4、N11）：/apps/create、/blueprints/:id、
// /blueprints/:id/trial、发布摘要。
//
// 守的是这些「看着没坏、其实不对」的地方：
//   - 新建应用三个起点：说说你要什么（例句填进输入框；开始后建对话起点的蓝图、这句话作为第一轮发给蓝图助手）、从场景模板开始
//     （模板卡写适用场景、会产出什么、要准备什么）、从已有的开始（最近的问数据回答；已发布的工作流只给搭建者）。身份决定范围：
//     搭建者建共享蓝图；使用者在管理员开了个人应用时建个人蓝图，个人应用用不了的模板不给选；开关没开照实说没开放；
//     应用首页的「新建应用」链到 /apps/create；
//   - 卡片：卡头标状态（缺按服务端检查、待确认、已确认），对到数据的项写表、中文名、粒度、业务日期、为什么；「修改」发 bp_set
//     （纯值），确认只改状态，「确认全部」一次带上整张卡片待确认的几项，加一项发 bp_add（纯值）；缺项时「用真数试一次」置灰并写清缺什么；
//   - 写回卡片：目标能力没登记查询时摆「结果不明时谁去核实」（服务端 reconciler_needed），缺时只挡发布，选人发 bp_set；
//   - 自由形的「谁来做」（05-已知遗留 (d)）：按服务端的 derived_people 一行一行摆（岗位、核实人能选人，发起人、表达式的
//     只读），不能加减步骤、不改会签；没绑的照服务端的 bp.people_unbound 标「缺」；选人发 bp_set 到那一行的 path；
//   - 反问：助手的 bp_ask 画成可点的选项，「这一轮在等你回答」常显，输入框提示回答；点选项就是把它作为下一轮发出去（回答记在
//     问题上、这一轮落进蓝图对话都是服务端做的，前端不另记）；
//   - 关节：只列生成关节，按卡片填的汇总一句；「让助手填」走 fill-joints-stream，逐组写进度，组之间并行；
//   - 试运行：横幅「试运行：数据是真的，但这不是正式结果」、这次的输入、「换一期再试」；流程类右侧列待办，按钮写「以试运行
//     身份…」；出错时写清为什么，给「交给助手修」「回到卡片改」；
//   - 发布摘要：每一步一句白话；问题分三栏（你来定、助手可以修、要找管理员），有 error 不能确认；确认按钮写清结果
//     （「发布并每周一 09:00 自动出数」）；执行时逐步打勾；停下时写停在哪一步、为什么，「接着做」；受管级只给管理员；
//   - 个人蓝图试过之后「存成我的应用」；
//   - 窄屏（390px）不出横向滚动。
//
// 页面是真页面（走真的应用外壳），蓝图、模板、发布单、蓝图助手的流、对话、待办在浏览器层答掉，数据是这里虚构的（「门店周报」）；
// 其余 GET 放行到服务端（外壳要它），别的写请求一律拦下、记在探针里。
//
//   node scripts/check-blueprint.mjs
// 对别的实例跑时带上地址：AGENTLAB_WEB=http://localhost:<前端端口> node scripts/check-blueprint.mjs
// CHECK_ONLY=新建,反问 只跑段名里含这些字的段。
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
const clone = (x) => JSON.parse(JSON.stringify(x))
const sse = (ops) => ops.map((o) => `data: ${JSON.stringify(o)}\n\n`).join('')
const until = async (cond, ms = 5000) => {
  const end = Date.now() + ms
  while (!cond() && Date.now() < end) await new Promise((r) => setTimeout(r, 50))
  return cond()
}

// ---------------------------------------------------------------- 假数据

const ADMIN_OFF = { mode: 'off', username: null, display_name: null, role: 'admin', groups: [], verified: false }
const BUILDER = { mode: 'local', username: 'lisi', display_name: '李四', role: 'builder', groups: [], verified: true }
const USER_ON = { mode: 'local', username: 'zhangsan', display_name: '张三', role: 'user', groups: [], verified: true,
  user_can_chat: true, user_personal_apps: true }
const USER_OFF = { ...USER_ON, user_personal_apps: false }

const TEMPLATES = [
  { id: 'periodic_board', version: 1, label: '周期看板', description: '每天、每周或每月看一组固定的数', applies_when: '', tags: [],
    personal: '全部可用',
    cards: [{ card: 'purpose', note: '必填' }, { card: 'data', note: '必填：要看的数' }, { card: 'cadence', note: '必填：多久出一次数' },
      { card: 'access', note: '谁能用' }] },
  { id: 'approval', version: 1, label: '审批流程', description: '有人发起、有人审批，可以按金额分级', applies_when: '', tags: [],
    personal: null,
    cards: [{ card: 'purpose', note: '必填' }, { card: 'fields', note: '必填：发起时填什么' }, { card: 'people', note: '必填：谁审批' },
      { card: 'writeback', note: '可选：通过后写回哪个系统' }, { card: 'access', note: '' }] },
  { id: 'freeform', version: 1, label: '自由形', description: '从已有的运行或工作流导入', applies_when: '', tags: [], personal: '只读部分',
    cards: [{ card: 'purpose', note: '' }, { card: 'cadence', note: '' },
      { card: 'people', note: '可选：只定每一步由谁做；会签、时限、超时在画布上改' }, { card: 'access', note: '' }] },
]

const item = (value, source = 'assistant', status, why = null) =>
  ({ value, source, status: status ?? (value == null ? 'missing' : source === 'user' ? 'confirmed' : 'proposed'), why })

const SPEC = {
  format: 'agentlab.blueprint/1',
  purpose: item('每周一看各门店上周的销售额和退款率', 'user'),
  template: { id: 'periodic_board', version: 1 },
  data: [
    { id: 'gmv', label: item('销售额', 'user'), unit: item('元'),
      table: item({ source: 'shop', table: 'orders', label: '订单' }, 'catalog', 'proposed',
        '数据目录里「订单」一行一单，业务日期是支付日，金额列是实收'),
      measure: item('按实收金额求和'), date: item({ column: 'paid_at', rule: '按支付日' }, 'catalog', 'confirmed'),
      filter: item(null), grain: item('一行一单', 'catalog', 'confirmed'), reuse: null },
    { id: 'refund', label: item('退款率', 'user'), unit: item(null), table: item(null), measure: item(null), date: item(null),
      filter: item(null), grain: item(null), reuse: null },
  ],
  cadence: item({ preset: 'weekly', weekday: 1, time: '09:00', tz: 'Asia/Shanghai', period: 'last_iso_week' }, 'template'),
  people: [], fields: [], writeback: null,
  access: { use: [{ kind: 'group', name: '门店' }], observe: [], manage: [] },
  asks: [],
}
const UNMAPPED = { code: 'bp.data_unmapped', level: 'error', path: '/data/1/table', message: '「退款率」还没对到表或已发布的口径卡',
  plain: { title: '「退款率」还没对到表', why: '不知道从哪张表取数，试运行出不了数', who: 'you', action: '在卡片上写明对到哪张表，或者让助手去对' } }

const BP = {
  id: 'bp_week', name: '门店周报', owner: null, scope: 'shared', status: 'drafting', spec: SPEC,
  template_id: 'periodic_board', template_version: 1, source: { kind: 'conversation' }, conversation_id: 'conv_bp',
  workflow_id: null, app_id: null, last_trial_run_id: null, graph_hash: null,
  joints: {
    'start.fields': { status: 'filled', by: 'template' },
    sql_gmv: { status: 'filled', by: 'assistant', label: '销售额的取数 SQL', kind: 'sql' },
    sql_refund: { status: 'pending', by: 'template', label: '退款率的取数 SQL', kind: 'sql', note: '这一项助手还没填上：可以请助手填，或者换个说法再试' },
  },
  copied_from: null, issues: [UNMAPPED], created_at: ago(600_000), updated_at: ago(60_000),
}
/** 补齐之后（可以试运行；试过之后服务端按发布查，待确认的项挡发布不挡试运行） */
const READY = (extra = {}) => ({ ...clone(BP), issues: [], joints: { ...BP.joints, sql_refund: { status: 'filled', by: 'assistant' } }, ...extra })

const ASK = { id: 'q1', question: '销售额按下单日还是支付日算？', options: ['按下单日', '按支付日'], path: '/data/0/date', answer: null }

const STAMP = { as_of: ago(3600_000), as_of_text: '截至 2026-10-13 09:00；统计期 上一 ISO 周 2026-W41', pin: '门店周报（试运行）',
  graph_hash: 'ab12cd34', calibers: [], tier: 'formal', tier_text: '完整出具', from: '试运行', sealed: true, trial: true,
  notes: ['数据是真的，但这不是正式结果：发布之后才会按设定出数'] }
// 形状同服务端试运行预览（blueprints/trial.preview_page）：印章 trial=true；趋势只有这一次试运行一个点，version 为 null，
// note 照实说没有趋势（apps/resolve._trial_series）；视图模型带 trial（对着哪次试运行）
const TRIAL_SERIES_NOTE = '试运行只有一次，没有趋势：发布之后每出一期数，这里多一个点'
const PREVIEW = {
  format: 'agentlab.view/1',
  app: { id: '', name: '门店周报', version: 0, level: null, needs_upgrade: false },
  page: { id: 'home', title: '本周概览' }, pages: [{ id: 'home', title: '本周概览' }],
  sources: { wk: { kind: 'trial', state: 'ok', run_id: 'run_trial', stamp: STAMP } },
  tree: [
    { id: 'stamp', type: 'stamp', props: {}, data: { view: 'wk', state: 'ok', stamp: STAMP } },
    { id: 'gmv', type: 'metric', props: { label: '销售额' }, data: { state: 'ok', text: '45.68 万元', name: '销售额' } },
    { id: 'gmv_trend', type: 'series', props: { label: '销售额的趋势' }, data: { state: 'ok', mode: 'single', unit: '万元',
      trial: true, breaks: [], note: TRIAL_SERIES_NOTE,
      series: [{ version: null, label: '试运行', points: [{ run_id: 'run_trial', as_of: '2026-10-12', value: 45.68, text: '45.68 万元',
        evidence: null }] }] } },
  ],
  forms: {}, problems: [],
  trial: { run_id: 'run_trial', status: 'succeeded', status_text: '已完成' },
}
const PREVIEW_FAILED = {
  ...PREVIEW, sources: { wk: { kind: 'trial', state: 'error', run_id: 'run_trial', stamp: null,
    message: '「取数」这一步查询失败：表 refunds 里没有列 refund_amt' } },
  tree: [{ id: 'gmv', type: 'metric', props: { label: '销售额' }, data: { view: 'wk', state: 'error' } }],
}
// 看板类的应用骨架（同服务端 periodic_board.app_skeleton）：没有入口；「按条件查看」那一页的条件表单是图的输入（统计期），
// 按引用写在 forms 里（{"$ref": "#/forms/start"}）——「换一期再试」填的就是它
const BOARD_SPEC = {
  format: 'agentlab.app/1', name: '门店周报', pins: [{ alias: 'main', workflow_id: null, version: null, graph_hash: 'ab12cd34', use: 'view' }],
  slots: {}, entries: [], task_ui: {}, hooks: [], notify: [], pages: [],
  forms: { start: { format: 'agentlab.form/1', type: 'object', required: [], 'x-order': ['period'],
    properties: { period: { type: 'string', title: '统计期' } } } },
  views: { wk: { source: { kind: 'latest_sealed', pin: 'main' } },
    custom: { source: { kind: 'on_demand', pin: 'main', params: { $ref: '#/forms/start' }, ttl_s: 3600 } } },
}
const TRIAL_TASK = {
  id: 'task_trial', approval_id: null, run_id: 'run_trial', node_id: 'review', app_id: null, app_name: null, workflow_name: null,
  kind: 'approve', title: '财务审批', form: null, policy: { kind: 'any' }, status: 'pending', due_at: null, escalation_level: 0,
  payload: { kind: 'human_node', message: '张三报销 1,200 元' }, assignments: [], can_respond: true, outcome: null,
  resolved_by: null, resolved_at: null, created_at: ago(60_000),
}

const STEP_KINDS = ['save_version', 'preflight', 'publish_workflow', 'create_schedule', 'create_app', 'publish_app', 'enable_schedule',
  'grant', 'first_run']
const steps = (done, failedAt, message) => STEP_KINDS.map((kind, i) => ({
  kind, status: i < done ? 'done' : kind === failedAt ? 'failed' : 'pending',
  result: kind === 'create_app' && i < done ? { app_id: 'app_week' } : null,
  ...(kind === failedAt ? { error_code: 'plan.grant', message } : {}),
}))
const SUMMARY = [
  { step: 'save_version', text: '保存这一版工作流（和最近一次试运行是同一张图）' },
  { step: 'publish_workflow', text: '发布「门店周报」：此后每周一 09:00 按这一版自动出数' },
  { step: 'grant', text: '门店组的人可以使用' },
]
// 形状同服务端 blueprints/publish_plan.plan_out：ok（没有 error 级问题、也没停下）由服务端算，界面照它决定能不能确认；
// 每条问题顶层带 who（归谁处理）和 source（出自哪一项预检）
const PLAN_BLOCKED = {
  id: 'plan_1', blueprint_id: 'bp_week', level: 'published', graph_hash: 'ab12cd34', status: 'planned', steps: steps(0),
  summary: SUMMARY, options: { first_run: true, dispositions: {}, spec_hash: 'f00d' }, ok: false, actor: 'lisi',
  created_at: '2026-10-14T09:00:00+00:00', updated_at: '2026-10-14T09:00:00+00:00',
  problems: [
    { code: 'bp.unconfirmed', level: 'error', path: '/data/0/table', message: '「看哪些数·第 1 项·对到的表」还没确认', who: 'you',
      source: 'blueprint',
      plain: { title: '「销售额」对到的表还没确认', why: '这是助手按数据目录对的', who: 'you', action: '在卡片上点「确认」' } },
    { code: 'app.upgrade_policy', level: 'error', path: 'pins[0]', alias: 'weekly', message: '口径升版必须处置', who: 'you',
      source: 'app', plain: { title: '口径变了，以前的数怎么处理还没选', why: '', who: 'you', action: '选一种处置' } },
    { code: 'app.binding_target_missing', level: 'error', path: 'pages.home.c1', message: '绑定的视图不存在', who: 'assistant',
      source: 'app', plain: { title: '页面上有一块绑的东西不存在', why: '', who: 'assistant', action: '交给助手按卡片改' } },
    { code: 'app.view_unbound', level: 'warning', path: 'views.wk', message: '视图没有绑定定时任务', who: 'assistant',
      source: 'app', plain: { title: '有一块看板没有对应的数', why: '', who: 'assistant', action: '交给助手按卡片改' } },
    { code: 'governed.metric_untraced', level: 'warning', path: null, message: '指标没有静态追到原始数据', who: 'admin',
      source: 'workflow', fix_kind: null,
      plain: { title: '有的指标追不到原始数据', why: '受管级要求', who: 'admin', action: '请管理员处理，或改选已发布级' } },
  ],
}
const PLAN_OK = { ...PLAN_BLOCKED, id: 'plan_2', problems: [], ok: true }

// ---------------------------------------------------------------- 浏览器

const browser = await chromium.launch({ executablePath: CHROME })

/**
 * 开一页。state：me（身份）、bp（蓝图）、plan（发布单剧本）、preview、streams（按指令挑的蓝图助手剧本）。
 * 写请求记进 state.writes；蓝图助手、填关节的请求体记进 state.streams / state.fills
 */
async function open({ width = 1280, height = 900, state }) {
  const ctx = await browser.newContext({ viewport: { width, height } })
  opened.add(ctx)
  const page = await ctx.newPage()
  page.errors = []
  page.on('pageerror', (e) => page.errors.push(e.message))
  state.writes ??= []
  state.chats ??= []
  state.fills ??= []
  await page.route((url) => url.pathname.startsWith('/api/'), async (route) => {
    const r = route.request()
    const url = new URL(r.url())
    const p = url.pathname
    const method = r.method()
    const body = () => { try { return JSON.parse(r.postData() || '{}') } catch { return {} } }
    const json = (data, status = 200) => route.fulfill({ status, json: data })
    if (p === '/api/auth/me') return json(state.me ?? ADMIN_OFF)
    if (p === '/api/templates') return json({ items: TEMPLATES })
    if (p === '/api/workflows' && method === 'GET') {
      return json([{ id: 'wf_sales', name: '销售日报', description: '', graph: { nodes: [], edges: [] }, tags: [], version: 4,
        is_template: false, status: 'published', published_version: 3 }])
    }
    if (p === '/api/conversations' && method === 'GET') {
      return json([
        { id: 'c_chat', title: '上周销售', kind: 'chat', archived: false, turn_count: 2, last_question: '上周各门店销售额是多少',
          last_status: 'done', last_run_id: 'run_chat_1', last_active_at: ago(3600_000) },
        { id: 'c_fail', title: '失败的一问', kind: 'chat', archived: false, turn_count: 1, last_question: '跑挂了', last_status: 'error',
          last_run_id: 'run_bad', last_active_at: ago(7200_000) },
      ])
    }
    // 蓝图对话的历史按蓝图取（权限跟着蓝图走，没开问数据的使用者也读得到）
    if (/^\/api\/blueprints\/[^/]+\/conversation$/.test(p) && method === 'GET') {
      return json({ id: 'conv_bp', title: '蓝图', kind: 'blueprint', archived: false, turn_count: 0, last_question: '', turns: state.past ?? [] })
    }
    if (p === '/api/conversations/conv_bp' && method === 'GET') {
      return json({ id: 'conv_bp', title: '蓝图', kind: 'blueprint', archived: false, turn_count: 0, last_question: '', turns: state.past ?? [] })
    }
    if (/^\/api\/conversations\/conv_bp\/turns/.test(p)) {
      state.writes.push({ method, path: p, body: r.postData() })
      return json({ id: 't_rec', seq: 1, question: body().question ?? '', answer: '', explanation: '', status: 'done', error: '' })
    }
    if (p === '/api/copilot/blueprint-stream' && method === 'POST') {
      const b = body()
      state.chats.push(b)
      const script = (state.streams ?? []).find((s) => b.instruction.includes(s.when)) ?? { ops: [{ op: 'done', explanation: '好' }] }
      if (script.blueprint) state.bp = script.blueprint
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: sse(script.ops) })
    }
    if (p === '/api/copilot/fill-joints-stream' && method === 'POST') {
      state.fills.push(body())
      const filled = READY()
      state.bp = filled
      // 服务端 api/copilot_blueprint.fill_joints_stream 的形状：每组 group started → … → filled / failed，最后 final
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: sse([
        { op: 'model', model: 'fake-model' },
        { op: 'group', group: 'g1', joints: ['sql_refund'], status: 'started' },
        { op: 'group', group: 'g2', joints: ['metric_refund'], status: 'started' },
        { op: 'look', group: 'g1', id: 'L1', by: 'system', need: '这一组关节要用的表', status: 'ok', elapsed_ms: 10, objects: [] },
        { op: 'plan', group: 'g2', summary: '按口径卡算退款率' },
        { op: 'fill_joint', group: 'g1', joint: 'sql_refund', value: 'SELECT 1' },
        { op: 'group', group: 'g1', status: 'filled' },
        { op: 'group', group: 'g2', status: 'failed', message: '指标的算法用了口径卡里没有的取数' },
        { op: 'final', blueprint: filled, groups: { g1: { status: 'filled' }, g2: { status: 'failed' } },
          problems: [{ code: 'tpl.joint_unfilled', level: 'error', message: '「退款率的算法」还没填',
            plain: { title: '「退款率的算法」助手没填上', why: '', who: 'assistant', action: '换个说法再让助手填' } }] },
      ]) })
    }
    if (p === '/api/blueprints' && method === 'POST') {
      const b = body()
      state.creates = [...(state.creates ?? []), b]
      const created = { ...clone(BP), id: 'bp_new', name: '新应用', scope: b.scope, spec: { ...clone(SPEC), data: [], template: null },
        issues: [], joints: {}, source: b.source }
      state.bp = created
      return json(created, 201)
    }
    if (p === '/api/blueprints' && method === 'GET') {
      return json({ items: [{ ...clone(BP), status: 'drafting' }, { ...clone(BP), id: 'bp_done', name: '已发布的', status: 'published' }] })
    }
    let m = p.match(/^\/api\/blueprints\/([^/]+)$/)
    if (m && method === 'GET') return json(state.bp ?? BP)
    if (m && method === 'PATCH') {
      const b = body()
      state.patches = [...(state.patches ?? []), b]
      if (state.patchReject) return json({ detail: state.patchReject, code: 'bp.refused' }, 422)
      return json(state.bp ?? BP)
    }
    m = p.match(/^\/api\/blueprints\/([^/]+)\/(instantiate|trial|preview|publish-plan|save-personal)$/)
    if (m) {
      const what = m[2]
      if (what === 'trial') {
        state.trials = [...(state.trials ?? []), body()]
        state.bp = { ...(state.bp ?? BP), status: 'trialed', last_trial_run_id: 'run_trial' }
        return json({ run_id: 'run_trial', blueprint: state.bp, inputs: { period: '2026-W41' } })
      }
      if (what === 'preview') return json(state.preview ?? PREVIEW)
      if (what === 'instantiate') {
        return json({ blueprint: state.bp ?? BP, app_spec: state.appSpec ?? { format: 'agentlab.app/1', name: '门店周报', pins: [], slots: {},
          entries: [], forms: {}, task_ui: {}, views: {}, pages: [], hooks: [], notify: [] }, problems: [] })
      }
      if (what === 'publish-plan') {
        state.planBodies = [...(state.planBodies ?? []), body()]
        return json(state.plans?.shift() ?? PLAN_OK)
      }
      state.writes.push({ method, path: p })
      return json({ app_id: 'app_mine', blueprint: { ...(state.bp ?? BP), status: 'personal', app_id: 'app_mine' } })
    }
    m = p.match(/^\/api\/publish-plans\/([^/]+)(?:\/(run|resume))?$/)
    if (m) {
      if (m[2]) state.planActs = [...(state.planActs ?? []), m[2]]
      const next = (m[2] ? state.planRun?.[m[2]] : state.planPoll?.shift()) ?? state.planLast
      if (next) state.planLast = next
      return json(next ?? PLAN_OK)
    }
    if (p === '/api/tasks' && method === 'GET') return json({ items: url.searchParams.get('run_id') === 'run_trial' ? state.tasks ?? [] : [] })
    if (p === '/api/tasks/count') return json({ mine: 0, apps: 0, other: 0 })
    if (p.startsWith('/api/tasks/') && p.endsWith('/respond')) {
      state.responds = [...(state.responds ?? []), body()]
      state.tasks = []
      return json({ task: { ...TRIAL_TASK, status: 'resolved', can_respond: false }, resolved: true })
    }
    if (p === '/api/apps' && method === 'GET') return json({ items: [] })
    if (method !== 'GET') {
      state.writes.push({ method, path: p, body: r.postData() })
      return json({ detail: '检查脚本拦下了写请求' }, 503)
    }
    return route.continue()
  })
  return { ctx, page }
}

const noHScroll = (page) => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)

// ---------------------------------------------------------------- 新建应用

await section('新建应用：三个起点（搭建者）', async () => {
  const state = {}
  const { page } = await open({ state })
  await page.goto(`${WEB}/apps?tab=apps`)
  const create = page.locator('header [data-app-create]')
  await create.waitFor({ timeout: 15000 })
  check('应用首页的「新建应用」链到 /apps/create，范围是共享', await create.getAttribute('href') === '/apps/create'
    && await create.getAttribute('data-app-create') === 'shared', `${await create.getAttribute('href')}`)
  await page.goto(`${WEB}/apps/create`)
  await page.locator('[data-new-app="shared"]').waitFor({ timeout: 15000 })
  check('还没做完的蓝图列在最上面「接着做」，做完的不列', await page.locator('[data-new-app-blueprint="bp_week"]').waitFor({ timeout: 5000 })
    .then(() => true, () => false) && await page.locator('[data-new-app-blueprint="bp_done"]').count() === 0
    && await page.locator('[data-new-app-blueprint="bp_week"]').getAttribute('href') === '/blueprints/bp_week')
  check('三个起点都在', await page.locator('[data-new-app-start="talk"]').count() === 1
    && await page.locator('[data-new-app-start="template"]').count() === 1 && await page.locator('[data-new-app-start="existing"]').count() === 1)
  const board = page.locator('[data-template="periodic_board"]')
  await board.waitFor({ timeout: 5000 })
  check('模板卡写适用场景、会产出什么、要准备什么', (await board.locator('[data-template-when]').innerText()).includes('固定的数')
    && (await board.locator('[data-template-produces]').innerText()).includes('看板')
    && (await board.locator('[data-template-prepare]').innerText()).includes('看哪些数'))
  check('「自由形」不作为从零开始的场景', await page.locator('[data-template="freeform"]').count() === 0)
  check('最近的问数据回答只列答完、有运行的', await page.locator('[data-new-app-answer="run_chat_1"]').count() === 1
    && await page.locator('[data-new-app-answer="run_bad"]').count() === 0)
  check('已发布的工作流列出来（搭建者）', await page.locator('[data-new-app-workflow="wf_sales"]').count() === 1)
  await page.locator('[data-new-app-example]').first().click()
  const text = await page.getByRole('textbox', { name: '你想做一个什么样的应用' }).inputValue()
  check('点例句只是填进输入框', text.includes('每周一') && !(state.creates ?? []).length, text)
  state.streams = [{ when: '每周一', ops: [{ op: 'heartbeat', phase: 'planning' }, { op: 'done', explanation: '整理好了' }] }]
  await page.locator('[data-new-app-talk-go]').click()
  await page.waitForURL(/\/blueprints\/bp_new$/, { timeout: 5000 }).catch(() => {})
  check('开始：建对话起点的共享蓝图', state.creates?.[0]?.source?.kind === 'conversation' && state.creates[0].scope === 'shared',
    JSON.stringify(state.creates?.[0]))
  check('……进蓝图页', /\/blueprints\/bp_new$/.test(page.url()), page.url())
  check('……这句话作为第一轮发给蓝图助手', await until(() => state.chats.length === 1)
    && state.chats[0].instruction === text && state.chats[0].blueprint_id === 'bp_new', JSON.stringify(state.chats[0]))

  // 从模板、从回答、从已发布的工作流：各建一种起点
  for (const [what, click, expect] of [
    ['模板', '[data-template-use="periodic_board"]', (b) => b.source.kind === 'template' && b.template === 'periodic_board'],
    ['问数据回答', '[data-new-app-answer="run_chat_1"] button', (b) => b.source.kind === 'run' && b.source.id === 'run_chat_1'],
    ['已发布的工作流', '[data-new-app-workflow="wf_sales"] button',
      (b) => b.source.kind === 'workflow_version' && b.source.id === 'wf_sales' && b.source.version === 3],
  ]) {
    const s = {}
    const o = await open({ state: s })
    await o.page.goto(`${WEB}/apps/create`)
    await o.page.locator(click).first().waitFor({ timeout: 15000 })
    await o.page.locator(click).first().click()
    check(`从${what}开始：起点写对`, await until(() => (s.creates ?? []).length === 1) && expect(s.creates[0]), JSON.stringify(s.creates?.[0]))
    check(`……进蓝图页、不发第一轮`, await o.page.waitForURL(/\/blueprints\/bp_new/, { timeout: 5000 }).then(() => true, () => false)
      && s.chats.length === 0)
  }
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')
})

await section('新建应用：使用者（个人应用的开关）', async () => {
  const state = { me: USER_ON }
  const { page } = await open({ state })
  await page.goto(`${WEB}/apps?tab=apps`)
  const create = page.locator('header [data-app-create]')
  check('开关开着：使用者也有「新建应用」，范围是个人', await create.waitFor({ timeout: 15000 }).then(() => true, () => false)
    && await create.getAttribute('data-app-create') === 'personal')
  await page.goto(`${WEB}/apps/create`)
  await page.locator('[data-new-app="personal"]').waitFor({ timeout: 15000 })
  check('写明建的是个人应用', (await page.locator('[data-new-app-personal]').innerText()).includes('只有你自己看得见'))
  await page.locator('[data-template="approval"]').waitFor({ timeout: 5000 })
  check('个人应用用不了的模板不给选，照实说为什么', await page.locator('[data-template-use="approval"]').isDisabled()
    && (await page.locator('[data-template="approval"] [data-template-personal]').innerText()).includes('不能用这个模板'))
  check('已发布的工作流只给搭建者：使用者没有', await page.locator('[data-new-app-workflows]').count() === 0)
  await page.locator('[data-template-use="periodic_board"]').click()
  check('建个人蓝图', await until(() => (state.creates ?? []).length === 1) && state.creates[0].scope === 'personal',
    JSON.stringify(state.creates?.[0]))

  const off = { me: USER_OFF }
  const o = await open({ state: off })
  await o.page.goto(`${WEB}/apps?tab=apps`)
  await o.page.waitForTimeout(1500)
  check('开关没开：使用者的应用首页没有「新建应用」', await o.page.locator('[data-app-create], [data-app-create-empty]').count() === 0)
  await o.page.goto(`${WEB}/apps/create`)
  await o.page.locator('[data-new-app="none"]').waitFor({ timeout: 15000 })
  check('……直接打开 /apps/create：照实说没开放，不给起点', (await o.page.locator('[data-new-app="none"]').innerText()).includes('还没有开放个人应用')
    && await o.page.locator('[data-new-app-start]').count() === 0)
  check('没有未捕获的运行时错误', page.errors.length === 0 && o.page.errors.length === 0, page.errors[0] ?? o.page.errors[0] ?? '')
})

// ---------------------------------------------------------------- 卡片

await section('蓝图页：卡片的状态、对到的数据、改一项、缺项挡试运行', async () => {
  const state = { bp: clone(BP) }
  const { page } = await open({ state })
  await page.goto(`${WEB}/blueprints/bp_week`)
  await page.locator('[data-blueprint-cards]').waitFor({ timeout: 15000 })
  check('按模板摆卡片（目的、场景、看哪些数、多久更新、谁能看）',
    await page.locator('[data-blueprint-cards]').getAttribute('data-blueprint-cards') === 'purpose,template,data,cadence,access',
    await page.locator('[data-blueprint-cards]').getAttribute('data-blueprint-cards'))
  check('卡头标状态：目的已确认、看哪些数缺（服务端说缺）、多久更新待确认',
    await page.locator('[data-card="purpose"]').getAttribute('data-card-status') === 'confirmed'
    && await page.locator('[data-card="data"]').getAttribute('data-card-status') === 'missing'
    && await page.locator('[data-card="cadence"]').getAttribute('data-card-status') === 'proposed')
  check('选填的空项（筛选条件）不标「缺」', await page.locator('[data-bp-item="/data/0/filter"]').getAttribute('data-item-status') === 'empty')
  check('服务端说缺的那一项标「缺」', await page.locator('[data-bp-item="/data/1/table"]').getAttribute('data-item-status') === 'missing')
  const match = page.locator('[data-bp-match="shop.orders"]')
  const matchText = await match.innerText()
  check('对到数据的项写表、中文名、粒度、业务日期、为什么', ['shop.orders', '订单', '一行一单', 'paid_at', '按支付日', '一行一单，业务日期是支付日']
    .every((k) => matchText.includes(k)), matchText.replace(/\n/g, ' '))
  check('多久更新写成白话', (await page.locator('[data-bp-item="/cadence"] [data-bp-value]').innerText()).includes('每周一 09:00，统计上周'))
  check('卡片上的问题照白话写', (await page.locator('[data-card="data"] [data-card-issues]').innerText()).includes('「退款率」还没对到表'))
  const trial = page.locator('[data-blueprint-trial]')
  check('缺项时「用真数试一次」置灰，写清缺什么', await trial.isDisabled()
    && (await page.locator('[data-blueprint-missing]').innerText()).includes('「退款率」还没对到表'))
  check('没试过：没有「发布」', await page.locator('[data-blueprint-publish]').count() === 0)

  // 改一项：bp_set，值是纯值
  await page.locator('[data-bp-item="/data/0/unit"] [data-bp-edit]').click()
  await page.locator('[data-bp-item="/data/0/unit"] [data-bp-editor] input').fill('万元')
  await page.locator('[data-bp-item="/data/0/unit"] [data-bp-save]').click()
  check('改一项：发 bp_set，值是纯值（来源由服务端记成你）', await until(() => (state.patches ?? []).length === 1)
    && JSON.stringify(state.patches[0]) === JSON.stringify({ ops: [{ op: 'bp_set', path: '/data/0/unit', value: '万元' }] }),
    JSON.stringify(state.patches?.[0]))
  // 确认：只改状态
  await page.locator('[data-bp-item="/data/0/table"] [data-bp-confirm]').click()
  check('确认：只改状态（/status = confirmed）', await until(() => (state.patches ?? []).length === 2)
    && JSON.stringify(state.patches[1].ops) === JSON.stringify([{ op: 'bp_set', path: '/data/0/table/status', value: 'confirmed' }]),
    JSON.stringify(state.patches?.[1]))
  // 加一项：bp_add，卡片字段写纯值
  await page.locator('[data-bp-add="/data"]').click()
  const dialog = page.getByRole('dialog')
  await dialog.locator('input').fill('订单数')
  await dialog.getByRole('button', { name: '加一项' }).click()
  check('加一项：bp_add，卡片字段写纯值、id 不和已有的重', await until(() => (state.patches ?? []).length === 3)
    && JSON.stringify(state.patches[2].ops) === JSON.stringify([{ op: 'bp_add', path: '/data', value: { id: 'd3', label: '订单数' } }]),
    JSON.stringify(state.patches?.[2]))
  // 确认全部：这张卡片上待确认、有值的几项一次标成已确认（单位、对到的表、算法）；只有一项的卡片（多久更新）不摆
  const confirmAll = page.locator('[data-card="data"] [data-bp-confirm-all]')
  check('卡头有「确认全部」，写明几项', (await confirmAll.innerText()).includes('确认全部（3）')
    && await page.locator('[data-card="cadence"] [data-bp-confirm-all]').count() === 0, await confirmAll.innerText())
  check('改值的按钮写「修改」', (await page.locator('[data-bp-item="/data/0/unit"] [data-bp-edit]').innerText()).trim() === '修改')
  await confirmAll.click()
  check('确认全部：一次修改带上这几项的状态，只改状态', await until(() => (state.patches ?? []).length === 4)
    && JSON.stringify(state.patches[3].ops) === JSON.stringify(['/data/0/unit', '/data/0/table', '/data/0/measure']
      .map((p) => ({ op: 'bp_set', path: `${p}/status`, value: 'confirmed' }))), JSON.stringify(state.patches?.[3]))
  // 被拒：照实说服务端的原因
  state.patchReject = '「单位」最多 20 个字'
  await page.locator('[data-bp-item="/data/0/unit"] [data-bp-edit]').click()
  await page.locator('[data-bp-item="/data/0/unit"] [data-bp-editor] input').fill('很长很长的单位')
  await page.locator('[data-bp-item="/data/0/unit"] [data-bp-save]').click()
  check('被拒：编辑框留着，提示服务端的原因', await page.getByText('「单位」最多 20 个字').first().waitFor({ timeout: 3000 }).then(() => true, () => false)
    && await page.locator('[data-bp-item="/data/0/unit"] [data-bp-editor]').count() === 1)
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')
})

// ---------------------------------------------------------------- 写回：结果不明时谁去核实

/** 审批流程的蓝图：通过后写回一项没登记查询的写入能力（服务端说要定核实人：reconciler_needed） */
const CLAIM_SPEC = {
  ...clone(SPEC), template: { id: 'approval', version: 1 }, data: [], cadence: item(null),
  people: [{ id: 's1', label: item('主管审批', 'user'), who: item([{ kind: 'user', name: 'zhang' }], 'user'),
    policy: item({ kind: 'any' }, 'user'), due: item(null), on_timeout: item(null), threshold: item(null) }],
  fields: [{ name: 'amount', spec: item({ type: 'number', title: '金额' }, 'user'), sensitive: false }],
  writeback: { capability: item('erp_claim', 'user'), mapping: item({ amount: 'form.amount' }, 'user'),
    confirm: item(true, 'user'), reconciler: item(null) },
}
const RECONCILER_UNBOUND = { code: 'bp.reconciler_unbound', level: 'error', path: '/writeback/reconciler',
  message: '「erp_claim」没登记查询：写回的结果不明时要有人去目标系统里核实，还没定是谁',
  plain: { title: '写回的结果不明时没人核实', why: '平台查不了，也不会自动重发', who: 'you',
    action: '在「写回哪里」卡片的「结果不明时谁去核实」里选人或组。' } }
const CLAIM = (extra = {}) => ({ ...clone(BP), template_id: 'approval', spec: clone(CLAIM_SPEC), joints: {},
  issues: [RECONCILER_UNBOUND], reconciler_needed: true, ...extra })

await section('写回：结果不明时谁去核实（能力没登记查询才摆，只能由人选，只挡发布）', async () => {
  const state = { bp: CLAIM() }
  const { page } = await open({ state })
  await page.goto(`${WEB}/blueprints/bp_week`)
  await page.locator('[data-card="writeback"]').waitFor({ timeout: 15000 })
  const row = page.locator('[data-bp-item="/writeback/reconciler"]')
  check('能力没登记查询：摆出「结果不明时谁去核实」，标「缺」，写明为什么要人核实',
    await row.count() === 1 && await row.getAttribute('data-item-status') === 'missing'
    && await page.locator('[data-bp-reconciler-hint="needed"]').count() === 1
    && await page.locator('[data-card="writeback"]').getAttribute('data-card-status') === 'missing')
  check('卡片上照白话写缺什么、怎么改', (await page.locator('[data-card="writeback"] [data-card-issues]').innerText())
    .includes('写回的结果不明时没人核实'))
  check('只挡发布：「用真数试一次」照样能点', !(await page.locator('[data-blueprint-trial]').isDisabled()))
  await row.locator('[data-bp-edit]').click()
  const editor = row.locator('[data-bp-editor]')
  await editor.locator('select').first().selectOption('group')
  await editor.locator('[data-principal-picker] input').fill('财务组')
  await editor.locator('[data-principal-picker] input').press('Enter')
  await row.locator('[data-bp-save]').click()
  check('选人：bp_set /writeback/reconciler，值是一组人或组', await until(() => (state.patches ?? []).length === 1)
    && JSON.stringify(state.patches[0].ops) === JSON.stringify([
      { op: 'bp_set', path: '/writeback/reconciler', value: [{ kind: 'group', name: '财务组' }] }]),
    JSON.stringify(state.patches?.[0]))
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')

  // 能力登记了查询：平台自己查，不摆这一项；以前选过的照样摆出来，写明用不上
  const looked = { bp: CLAIM({ issues: [], reconciler_needed: false }) }
  const second = await open({ state: looked })
  await second.page.goto(`${WEB}/blueprints/bp_week`)
  await second.page.locator('[data-card="writeback"]').waitFor({ timeout: 15000 })
  check('登记了查询、没选过人：不摆这一项', await second.page.locator('[data-bp-item="/writeback/reconciler"]').count() === 0)
  const chosen = CLAIM({ issues: [], reconciler_needed: false })
  chosen.spec.writeback.reconciler = item([{ kind: 'group', name: '财务组' }], 'user')
  const third = await open({ state: { bp: chosen } })
  await third.page.goto(`${WEB}/blueprints/bp_week`)
  await third.page.locator('[data-card="writeback"]').waitFor({ timeout: 15000 })
  check('登记了查询、以前选过人：照样摆出来，写明用不上', await third.page.locator('[data-bp-item="/writeback/reconciler"]').count() === 1
    && await third.page.locator('[data-bp-reconciler-hint="unused"]').count() === 1)
})

// ---------------------------------------------------------------- 自由形：谁来做

/** 从已发布的「财务审批（岗位）+ 写回（没登记查询）」工作流导入的自由形蓝图：服务端按图列出的「谁来做」（freeform.people_rows） */
const FREE_ROWS = [
  { kind: 'slot', slot: 'finance', label: '「财务审批」', detail: '「财务审批」处理', path: '/people/0/who', who: null,
    uses: [{ node_id: 'review', title: '财务审批', role: 'assignee' }] },
  { kind: 'reconciler', slot: 'reconciler', label: '写入结果不明时谁去核实', detail: '「写回报销单」写入结果不明时去核实',
    path: '/writeback/reconciler', who: null, uses: [{ node_id: 'pay', title: '写回报销单', role: 'reconcile' }] },
  { kind: 'initiator', slot: null, label: '「本人确认」', detail: '由发起人自己处理', path: null, who: null,
    uses: [{ node_id: 'me', title: '本人确认', role: 'assignee' }] },
]
const unboundAt = (path, message) => ({ code: 'bp.people_unbound', level: 'error', path, message, slot: 'x',
  plain: { title: '有一步还没定谁来处理', why: '流程走到这一步就没人接手', who: 'you', action: '在「谁来做」里选人、组或岗位。' } })
const FREE = () => ({ ...clone(BP), template_id: 'freeform',
  spec: { ...clone(SPEC), template: { id: 'freeform', version: 1 }, data: [], people: [] },
  joints: {}, derived_people: clone(FREE_ROWS),
  issues: [unboundAt('/people/0/who', '「财务审批」还没定谁来处理（「财务审批」处理）'),
    unboundAt('/writeback/reconciler', '写入结果不明时谁去核实还没定')] })

await section('自由形：「谁来做」按导入的图列出，只定由谁做（05-已知遗留 (d)）', async () => {
  const state = { bp: FREE() }
  const { page } = await open({ state })
  await page.goto(`${WEB}/blueprints/bp_week`)
  const card = page.locator('[data-card="people"]')
  await card.waitFor({ timeout: 15000 })
  check('按服务端给的行摆：岗位、核实人、只读的发起人', (await card.locator('[data-bp-derived]').evaluateAll(
    (els) => els.map((e) => e.getAttribute('data-bp-derived')))).join(',') === 'slot,reconciler,initiator')
  check('行名、用途照服务端写（节点在图上的名字）', (await card.locator('[data-bp-derived="slot"]').innerText()).includes('「财务审批」处理'))
  check('没绑的标「缺」，卡头也是缺', await card.locator('[data-bp-item="/people/0/who"]').getAttribute('data-item-status') === 'missing'
    && await card.locator('[data-bp-item="/writeback/reconciler"]').getAttribute('data-item-status') === 'missing'
    && await card.getAttribute('data-card-status') === 'missing')
  check('卡片上照白话写缺什么、怎么改（核实人那一条也归在这张卡片上）',
    (await card.locator('[data-card-issues]').getAttribute('data-card-issues')) === '2')
  check('发起人的那一步只读：没有「修改」', await card.locator('[data-bp-derived="initiator"] [data-bp-edit]').count() === 0)
  check('不能加减步骤：没有「加一步」、没有删除', await card.locator('[data-bp-add="/people"]').count() === 0
    && await card.locator('[data-bp-remove]').count() === 0)
  const row = card.locator('[data-bp-item="/people/0/who"]')
  await row.locator('[data-bp-edit]').click()
  const editor = row.locator('[data-bp-editor]')
  await editor.locator('[data-principal-picker] input').fill('zhang')
  await editor.locator('[data-principal-picker] input').press('Enter')
  await row.locator('[data-bp-save]').click()
  check('选人：bp_set 到那一行的 path，值是一组人或组', await until(() => (state.patches ?? []).length === 1)
    && JSON.stringify(state.patches[0].ops) === JSON.stringify([
      { op: 'bp_set', path: '/people/0/who', value: [{ kind: 'user', name: 'zhang' }] }]),
    JSON.stringify(state.patches?.[0]))
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')
})

// ---------------------------------------------------------------- 反问

await section('反问：选项可点、「这一轮在等你回答」常显、回答记在问题上', async () => {
  const asked = { ...clone(BP), spec: { ...clone(SPEC), asks: [ASK] } }
  const answered = { ...clone(BP), spec: { ...clone(SPEC), asks: [{ ...ASK, answer: '按支付日' }] } }
  const state = {
    bp: clone(BP),
    streams: [
      { when: '退款率', blueprint: asked, ops: [
        { op: 'heartbeat', phase: 'planning' },
        { op: 'look', id: 'L1', by: 'model', status: 'ok', objects: [{ kind: 'table', source: 'shop', named: [], picked: ['orders'],
          joined: [], missing: [], total: 3 }] },
        { op: 'bp_set', path: '/data/0/measure', value: '按实收金额求和' },
        { op: 'bp_ask', id: 'q1', question: ASK.question, options: ASK.options, path: ASK.path },
        { op: 'final', blueprint: asked },
      ] },
      { when: '按支付日', blueprint: answered, ops: [{ op: 'bp_set', path: '/data/0/date', value: { column: 'paid_at' } },
        { op: 'done', explanation: '按支付日算' }, { op: 'final', blueprint: answered }] },
    ],
  }
  const { page } = await open({ state })
  await page.goto(`${WEB}/blueprints/bp_week`)
  await page.locator('[data-blueprint-chat]').waitFor({ timeout: 15000 })
  const box = page.getByRole('textbox', { name: '对助手说' })
  await box.fill('还要看退款率')
  await box.press('Enter')
  const ask = page.locator('[data-blueprint-ask="q1"]')
  check('助手的反问画成可点的选项', await ask.waitFor({ timeout: 5000 }).then(() => true, () => false)
    && (await ask.locator('[data-blueprint-ask-option]').allInnerTexts()).join('|') === '按下单日|按支付日')
  check('「这一轮在等你回答」常显', (await ask.locator('[data-blueprint-waiting]').innerText()).includes('这一轮在等你回答'))
  check('输入框提示回答助手的问题', (await box.getAttribute('placeholder')).includes('回答助手的问题'))
  const steps = await page.locator('[data-blueprint-chat]').innerText()
  check('过程里说改了哪张卡片的哪一项、问了什么', steps.includes('改了「看哪些数」第 1 项的「怎么算」') && steps.includes('助手问你一个问题'),
    steps.replace(/\n/g, ' ').slice(0, 300))
  await ask.locator('[data-blueprint-ask-option="按支付日"]').click()
  check('点选项：作为下一轮发给蓝图助手（回答记在问题上由服务端做）', await until(() => state.chats.length === 2)
    && JSON.stringify(state.chats[1]) === JSON.stringify({ blueprint_id: 'bp_week', instruction: '按支付日' }), JSON.stringify(state.chats[1]))
  check('前端不另记这一轮、不另记回答（服务端已经记了，免得一轮两条）', !state.writes.some((w) => w.path.includes('/conversations/'))
    && !(state.patches ?? []).length, JSON.stringify(state.writes.map((w) => w.path)))
  check('回答之后不再显示在等', await page.locator('[data-blueprint-ask]').waitFor({ state: 'detached', timeout: 5000 }).then(() => true, () => false))
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')
})

// ---------------------------------------------------------------- 关节

await section('关节：逐组写进度，组之间并行', async () => {
  const state = { bp: clone(BP) }
  const { page } = await open({ state })
  await page.goto(`${WEB}/blueprints/bp_week`)
  const joints = page.locator('[data-bp-joints]')
  await joints.waitFor({ timeout: 15000 })
  check('列出生成关节的状态（服务端给的名字）', (await joints.locator('[data-bp-joint="sql_gmv"]').innerText()).includes('销售额的取数 SQL')
    && await joints.locator('[data-bp-joint="sql_refund"]').getAttribute('data-joint-status') === 'pending')
  check('按卡片直接填好的关节不逐个列，汇总一句', await joints.locator('[data-bp-joint="start.fields"]').count() === 0
    && await joints.locator('[data-bp-joints-fixed]').getAttribute('data-bp-joints-fixed') === '1')
  await joints.locator('[data-bp-fill]').click()
  check('不点名哪几组：服务端填还有没填上的关节的那几组', await until(() => state.fills.length === 1)
    && JSON.stringify(state.fills[0]) === JSON.stringify({ blueprint_id: 'bp_week' }), JSON.stringify(state.fills[0]))
  const groups = joints.locator('[data-bp-fill-groups]')
  await groups.waitFor({ timeout: 5000 })
  check('两组各一行', await groups.locator('[data-bp-fill-group]').count() === 2)
  check('第一组填好、第二组没填上并写明原因', await joints.locator('[data-bp-fill-group="g1"]').getAttribute('data-group-status') === 'done'
    && await joints.locator('[data-bp-fill-group="g2"]').getAttribute('data-group-status') === 'failed'
    && (await joints.locator('[data-bp-fill-group="g2"]').innerText()).includes('口径卡里没有的取数'))
  check('final 的蓝图换上：关节都已填', await joints.locator('[data-bp-joint="sql_refund"]').getAttribute('data-joint-status') === 'filled')
  check('有一组没填上：按钮写「让助手重新填」', (await joints.innerText()).includes('让助手重新填') || await joints.locator('[data-bp-fill]').count() === 0)
  check('final 带来的问题照白话列出', (await joints.locator('[data-bp-instantiate-problems]').innerText()).includes('「退款率的算法」助手没填上'))
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')
})

// ---------------------------------------------------------------- 试运行

await section('试运行：横幅、输入、换一期再试、待办、出错', async () => {
  const state = { bp: READY(), tasks: [TRIAL_TASK] }
  const { page } = await open({ state })
  await page.goto(`${WEB}/blueprints/bp_week`)
  const trial = page.locator('[data-blueprint-trial]')
  await trial.waitFor({ timeout: 15000 })
  check('补齐之后可以试运行', !(await trial.isDisabled()))
  await trial.click()
  await page.waitForURL(/\/blueprints\/bp_week\/trial\?run=run_trial/, { timeout: 5000 })
  check('试运行：不带输入时服务端按「多久更新」算最近一期', JSON.stringify(state.trials?.[0]) === '{}', JSON.stringify(state.trials))
  const banner = page.locator('[data-trial-banner]')
  await banner.waitFor({ timeout: 5000 })
  check('横幅：数据是真的，但这不是正式结果', (await banner.locator('[data-trial-banner-title]').innerText()) === '试运行：数据是真的，但这不是正式结果')
  check('横幅写这次的输入', (await banner.locator('[data-trial-inputs]').innerText()).includes('2026-W41'))
  check('应用的样子用 ViewRenderer 渲染（预览）', await page.locator('[data-view-renderer="preview"]').count() === 1
    && (await page.locator('[data-stamp]').first().innerText()).includes('试运行'))
  const trend = page.locator('[data-component="series"]')
  check('趋势只有试运行一个点：照写服务端的说明（没有趋势），不当成出错', (await trend.locator('[data-series-note]').allInnerTexts())
    .includes(TRIAL_SERIES_NOTE), (await trend.innerText().catch(() => '')).replace(/\s+/g, ' '))
  const tasks = page.locator('[data-trial-tasks]')
  check('有待办：右侧列试运行里的待办', await tasks.waitFor({ timeout: 5000 }).then(() => true, () => false))
  check('按钮写「以试运行身份…」', (await tasks.locator('[data-task-approve]').innerText()).includes('以试运行身份同意')
    && (await tasks.locator('[data-task-reject]').innerText()).includes('以试运行身份驳回'))
  check('写入走测试环境或演练', (await tasks.locator('[data-trial-write-note]').innerText()).includes('测试环境'))
  await tasks.locator('[data-task-approve]').click()
  check('以试运行身份同意：回复待办', await until(() => (state.responds ?? []).length === 1) && state.responds[0].decision === 'approve')
  await banner.locator('[data-trial-retry]').click()
  check('预览没给表单：不自己从骨架里挑，只能原样再试', await banner.locator('[data-trial-retry-form] [data-field]').count() === 0)
  await banner.locator('[data-trial-retry-go]').click()
  check('换一期再试：再发起一次试运行', await until(() => (state.trials ?? []).length === 2))

  // 看板类：「换一期再试」的表单由服务端随预览给（ViewModel.trial.form，blueprints/trial.retry_form：看板类是按条件查看的
  // 条件表单），界面照它画、不自己从骨架里挑；填的期带进试运行
  const board = { bp: READY(), appSpec: BOARD_SPEC, preview: { ...PREVIEW, trial: { ...PREVIEW.trial, form: BOARD_SPEC.forms.start } } }
  const b = await open({ state: board })
  await b.page.goto(`${WEB}/blueprints/bp_week`)
  await b.page.locator('[data-bp-instantiate]').click()
  await b.page.waitForTimeout(300)
  await b.page.locator('[data-blueprint-trial]').click()
  await b.page.waitForURL(/\/trial\?run=run_trial/, { timeout: 5000 })
  const bBanner = b.page.locator('[data-trial-banner]')
  await bBanner.waitFor({ timeout: 5000 })
  check('横幅写服务端交回的这次输入（字段按表单写中文名）', (await bBanner.locator('[data-trial-inputs]').innerText()).includes('统计期：2026-W41'),
    await bBanner.locator('[data-trial-inputs]').innerText())
  await bBanner.locator('[data-trial-retry]').click()
  const retryForm = bBanner.locator('[data-trial-retry-form] [data-field="period"] input')
  check('看板类：「换一期再试」照服务端随预览给的条件表单画，带上这次的期', await retryForm.count() === 1
    && await retryForm.inputValue() === '2026-W41', String(await retryForm.inputValue().catch(() => '（没有表单）')))
  await retryForm.fill('2026-W40')
  await bBanner.locator('[data-trial-retry-go]').click()
  check('……换的期带进试运行', await until(() => (board.trials ?? []).length === 2) && board.trials[1]?.inputs?.period === '2026-W40',
    JSON.stringify(board.trials))

  // 出错：白话说为什么，给「交给助手修」「回到卡片改」
  const bad = { bp: READY({ status: 'trialed', last_trial_run_id: 'run_trial' }), preview: PREVIEW_FAILED,
    streams: [{ when: '试运行出错了', ops: [{ op: 'done', explanation: '改了' }] }] }
  const o = await open({ state: bad })
  await o.page.goto(`${WEB}/blueprints/bp_week/trial?run=run_trial`)
  const err = o.page.locator('[data-trial-error]')
  check('出错时写清停在哪一步、为什么', await err.waitFor({ timeout: 15000 }).then(() => true, () => false)
    && (await err.innerText()).includes('没有列 refund_amt'))
  await err.locator('[data-trial-fix]').click()
  check('交给助手修：把原因作为一轮发给蓝图助手，回到卡片页', await until(() => bad.chats.length === 1)
    && bad.chats[0].instruction.includes('没有列 refund_amt') && /\/blueprints\/bp_week$/.test(o.page.url()), o.page.url())
  check('没有未捕获的运行时错误', page.errors.length === 0 && o.page.errors.length === 0, page.errors[0] ?? o.page.errors[0] ?? '')
})

// ---------------------------------------------------------------- 发布摘要

await section('发布摘要：三栏、处置、交给助手修', async () => {
  const state = { bp: READY({ status: 'trialed', last_trial_run_id: 'run_trial' }), plans: [clone(PLAN_BLOCKED), clone(PLAN_BLOCKED)] }
  const { page } = await open({ state })
  await page.goto(`${WEB}/blueprints/bp_week`)
  await page.locator('[data-blueprint-publish]').click()
  const dlg = page.locator('[data-plan="planned"]')
  await dlg.waitFor({ timeout: 15000 })
  check('每一步一句白话', (await dlg.locator('[data-plan-step="publish_workflow"]').innerText()).includes('此后每周一 09:00 按这一版自动出数'))
  check('挡路的问题分三栏：你来定、助手可以修、要找管理员', await dlg.locator('[data-plan-who="you"]').getAttribute('data-plan-who-count') === '2'
    && await dlg.locator('[data-plan-who="assistant"]').getAttribute('data-plan-who-count') === '1'
    && await dlg.locator('[data-plan-who="admin"]').getAttribute('data-plan-who-count') === '0')
  check('只是提醒的（warning）另列，不进三栏', await dlg.locator('[data-plan-reminders]').getAttribute('data-plan-reminders') === '2'
    && await dlg.locator('[data-plan-reminder="app.view_unbound"]').count() === 1
    && await dlg.locator('[data-plan-reminder="governed.metric_untraced"]').count() === 1)
  check('有 error 不能确认，写明为什么', await page.locator('[data-plan-confirm]').isDisabled() && await dlg.locator('[data-plan-blocked]').count() === 1)
  check('关登录（管理员）：可以选受管级', await dlg.locator('[data-plan-level]').count() === 1)
  await dlg.locator('[data-plan-disposition="weekly"] button').first().click()
  check('口径升版：选处置就重出发布单，带上处置', await until(() => (state.planBodies ?? []).length === 2)
    && state.planBodies[1].dispositions?.weekly === 'recompute', JSON.stringify(state.planBodies?.[1]))
  await page.locator('[data-plan-fix]').click()
  check('交给助手修：关掉摘要，把问题作为一轮发给助手', await until(() => state.chats.length === 1)
    && state.chats[0].instruction.includes('页面上有一块绑的东西不存在')
    && !state.chats[0].instruction.includes('有一块看板没有对应的数'), JSON.stringify(state.chats[0]))

  // 搭建者（开了登录）：没有受管级可选
  const b = { me: BUILDER, bp: READY({ status: 'trialed', last_trial_run_id: 'run_trial' }), plans: [clone(PLAN_OK)] }
  const o = await open({ state: b })
  await o.page.goto(`${WEB}/blueprints/bp_week`)
  await o.page.locator('[data-blueprint-publish]').click()
  await o.page.locator('[data-plan="planned"]').waitFor({ timeout: 15000 })
  check('搭建者：受管级只给管理员，不给选', await o.page.locator('[data-plan-level]').count() === 0)
  check('没有未捕获的运行时错误', page.errors.length === 0 && o.page.errors.length === 0, page.errors[0] ?? o.page.errors[0] ?? '')
})

await section('发布摘要：确认、逐步打勾、停下、接着做', async () => {
  const running = { ...clone(PLAN_OK), status: 'running', steps: steps(2) }
  const stopped = { ...clone(PLAN_OK), status: 'stopped', ok: false, steps: steps(7, 'grant', '名录里没有组「门店」：先在设置里建这个组，或者在卡片上改授权') }
  const done = { ...clone(PLAN_OK), status: 'done', steps: steps(9) }
  const state = {
    bp: READY({ status: 'trialed', last_trial_run_id: 'run_trial' }), plans: [clone(PLAN_OK)],
    planRun: { run: running, resume: done }, planPoll: [running, stopped],
  }
  const { page } = await open({ state })
  await page.goto(`${WEB}/blueprints/bp_week`)
  await page.locator('[data-blueprint-publish]').click()
  const confirm = page.locator('[data-plan-confirm]')
  await confirm.waitFor({ timeout: 15000 })
  check('确认按钮写清结果', (await confirm.innerText()).includes('发布并每周一 09:00 自动出数'), await confirm.innerText())
  check('没有问题照实说', await page.locator('[data-plan-no-problems]').count() === 1)
  check('看板类默认勾「发布后立即运行一次」', await page.locator('[data-plan-first-run]').isChecked())
  await confirm.click()
  check('执行：调 run', await until(() => (state.planActs ?? []).includes('run')))
  check('逐步打勾：已完成的打勾', await page.locator('[data-plan-step="save_version"][data-step-status="done"]').waitFor({ timeout: 5000 }).then(() => true, () => false))
  const stop = page.locator('[data-plan-stopped="grant"]')
  check('停下时写停在哪一步、为什么', await stop.waitFor({ timeout: 8000 }).then(() => true, () => false)
    && (await stop.innerText()).includes('停在「授权」') && (await stop.innerText()).includes('名录里没有组「门店」'))
  await page.locator('[data-plan-resume]').click()
  check('接着做：调 resume', await until(() => (state.planActs ?? []).includes('resume')))
  const openApp = page.locator('[data-plan-open-app]')
  check('做完：给「打开应用」', await openApp.waitFor({ timeout: 5000 }).then(() => true, () => false)
    && await openApp.getAttribute('href') === '/apps/app_week')
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')
})

await section('个人蓝图：试过之后存成我的应用', async () => {
  const state = { me: USER_ON, bp: READY({ scope: 'personal', status: 'trialed', last_trial_run_id: 'run_trial', owner: 'zhangsan' }) }
  const { page } = await open({ state })
  await page.goto(`${WEB}/blueprints/bp_week`)
  const save = page.locator('[data-blueprint-save-personal]')
  await save.waitFor({ timeout: 15000 })
  check('个人蓝图：没有「发布」，有「存成我的应用」', await page.locator('[data-blueprint-publish]').count() === 0
    && (await save.innerText()).includes('存成我的应用'))
  check('个人蓝图不摆「谁能看」', await page.locator('[data-card="access"]').count() === 0)
  await save.click()
  check('存成我的应用：调 save-personal，进应用页', await page.waitForURL(/\/apps\/app_mine$/, { timeout: 5000 }).then(() => true, () => false)
    && state.writes.some((w) => w.path === '/api/blueprints/bp_week/save-personal'), page.url())
})

await section('窄屏：390px 不出横向滚动', async () => {
  const state = { bp: READY({ status: 'trialed', last_trial_run_id: 'run_trial' }) }
  const { page } = await open({ width: 390, height: 844, state })
  for (const path of ['/apps/create', '/blueprints/bp_week', '/blueprints/bp_week/trial?run=run_trial']) {
    await page.goto(`${WEB}${path}`)
    await page.waitForTimeout(1000)
    check(`${path} 没有横向滚动`, await noHScroll(page))
  }
})

await browser.close()
console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 全部通过')
process.exit(failed ? 1 : 0)
