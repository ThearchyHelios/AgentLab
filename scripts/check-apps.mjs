// 应用使用面（/apps、/apps/:appId、/apps/:appId/p/:pageId、/apps/:appId/new/:entryId、/apps/:appId/i/:runId）的回归检查。
//
// 守的是这些「看着没坏、其实不对」的地方：
//   - 应用首页三个页签（待办 / 应用 / 我发起的）记在地址上；关登录时挂一行说明（所有人都能用全部功能，签批人是自报署名）；
//     需升级的应用照实标出来；
//   - 应用页按视图模型渲染：印章四项（截至 / 口径 / 出具档位 / 来源）和服务端标出的错位照写；指标块的数是按钮，点开走
//     指标链接口（GET /runs/{id}/evidence/metrics/{node}/{metric}），面板里有指标步骤；没有出处的数照实标「无证据」；
//     趋势是自绘 SVG，升版不可比的地方画断开线并写明；「查看数据」给出表格视图；表格点表头排序、遮罩列写「已遮罩」；
//     不认识的组件写「此组件需要更新前端」，不报错、不空白；服务端解析出错的组件（problems）列在页面顶上；
//     有视图在生成时自动重取；
//   - 入口页：必填没填本地拦下、错误挂到字段下面；服务端 422 的 {errors:[{path, code, message}]} 按 path 挂回字段，
//     对不上字段的列在表单顶上；⌘/Ctrl+Enter 发起；成功后进实例页；需升级时不让发起；
//   - 实例页：状态（服务端给的说法：结果未知写「等待核实写入结果」）、印章、进度（结果未知的那一步不写「完成」）、
//     本实例的待办卡（就地处理）；不是处理人的待办表单只画这一步的状态、不去取待办；动作状态（哈希前 8 位、环境；
//     结果未知的经过记一笔，不当失败原因）；
//   - 窄屏（390px）不出横向滚动；
//   - 页面上直接改（normal-user-apps N6）：有 manage 的人点「编辑」进编辑状态（地址记 edit=1），按草稿解析；点组件是选中、
//     不打开出处；小工具条改文字只给目录标了 x-text 的属性；隐藏、上移、删除都自动存草稿（PATCH 带整份草稿）后按草稿
//     重新解析；草稿里隐藏的组件画成半透明，服务端没画出来的列在页底能取消隐藏；「加一张数据卡片」按类别分组、候选 id
//     撞了换新的；顶部写「有未发布的改动」，「发布」走应用发布；应用出自蓝图时给「改取数」，链到蓝图页。
//
// 页面是真页面（走真的应用外壳），/api/apps/**、/api/tasks/**、指标链接口在浏览器层答掉，数据是这里虚构的
// （「门店周报」「报销审批」）；其余 GET 放行到服务端（外壳要它），写请求一律拦下、记在探针里，不碰真实写接口。
//
//   node scripts/check-apps.mjs
// 对别的实例跑时带上地址：AGENTLAB_WEB=http://localhost:<前端端口> node scripts/check-apps.mjs
// CHECK_ONLY=首页,入口 只跑段名里含这些字的段。
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
const RUN = 'run_week_14'

// ---------------------------------------------------------------- 假数据

const APPS = [
  { id: 'app_weekly', name: '门店周报', description: '每周一看各门店的销售概况', icon: 'app', status: 'active',
    published_version: 3, level: 'governed', needs_upgrade: false, pending_tasks: 0, my_role: 'manage', updated_at: ago(3600_000) },
  { id: 'app_claim', name: '报销审批', description: '员工发起报销，财务会签后付款', icon: 'app', status: 'active',
    published_version: 2, level: 'published', needs_upgrade: true, pending_tasks: 1, my_role: 'use', updated_at: ago(7200_000) },
]

const STAMP = {
  as_of: ago(86400_000), as_of_text: '截至 2026-10-13 10:05；统计期 week 2026-W41', pin: '周销售 v7', graph_hash: '3fa2c1d0',
  calibers: [{ name: '周销售口径', version: 'v3' }], tier: 'formal', tier_text: '完整出具',
  from: '定时任务「周一 10 点」第 14 次', sealed: true, notes: ['定时任务已改为运行 v8，本应用固定在 v7'],
}

const metricEv = (metric) => ({ run_id: RUN, kind: 'metric', node: 'kpi', metric, eid: `ev:metric:${metric}` })
const point = (i, value) => ({
  run_id: `run_w${i}`, as_of: new Date(Date.UTC(2026, 8, 1 + i * 7)).toISOString(), value, text: `${value} 万元`,
  evidence: { run_id: `run_w${i}`, kind: 'metric', node: 'kpi', metric: 'gmv', eid: `ev:${i}` },
})

const HOME = {
  format: 'agentlab.view/1',
  app: { id: 'app_weekly', name: '门店周报', version: 3, level: 'governed', needs_upgrade: false },
  page: { id: 'home', title: '本周概览' },
  pages: [{ id: 'home', title: '本周概览' }, { id: 'detail', title: '门店明细' }],
  sources: { wk: { state: 'ok', run_id: RUN, stamp: STAMP } },
  tree: [
    { id: 'stamp', type: 'stamp', props: {}, data: { view: 'wk', state: 'ok', stamp: STAMP } },
    { id: 'kpis', type: 'columns', props: { count: 2 }, children: [
      // 绑定指标：服务端交来的字已经按口径卡带着单位（render_metric），data.unit 说明是哪个单位；派生时属性里也写了单位
      { id: 'gmv', type: 'metric', props: { label: '销售额', unit: '万元' },
        data: { state: 'ok', text: '45.68 万元', name: '销售额', unit: '万元', evidence: metricEv('gmv') } },
      { id: 'orders', type: 'metric', props: { label: '订单数' }, data: { state: 'ok', text: '1,284 单', name: '订单数' } },
      // 绑定单元格：字不带单位（render_cell），单位是搭建者在属性里填的
      { id: 'shops', type: 'metric', props: { label: '门店数', unit: '家' },
        data: { state: 'ok', text: '128', evidence: { run_id: RUN, kind: 'cell', node: 'q1', artifact: 'art_q1', eid: 'ev:cell:shops' } } },
      // 绑定指标、口径卡没写单位：服务端照样带 unit（空串），字里没有单位，写搭建者在属性里填的
      { id: 'aov', type: 'metric', props: { label: '客单价', unit: '元' },
        data: { state: 'ok', text: '356.2', name: '客单价', unit: '', evidence: metricEv('aov') } },
    ] },
    { id: 'trend', type: 'series', props: { label: '销售额趋势' }, data: {
      state: 'ok', mode: 'incomparable', unit: '万元',
      series: [
        { version: 6, label: 'v6', points: [point(0, 40.1), point(1, 41.3), point(2, 39.8)] },
        { version: 7, label: 'v7', points: [point(3, 44.2), point(4, 45.68)] },
      ],
      breaks: [{ after_run: 'run_w2', from: 6, to: 7, text: '口径从 v6 升到 v7，前后不可比' }],
    } },
    { id: 'stores', type: 'table', props: { title: '门店排行' }, data: {
      state: 'ok', columns: ['门店', '销售额', '店长手机'], masked: ['店长手机'], total_rows: 3, truncated: false,
      rows: [['南山店', 120.5, '已遮罩'], ['福田店', 98.2, '已遮罩'], ['宝安店', 130.1, '已遮罩']],
      evidence: { run_id: RUN, kind: 'query', node: 'q1', artifact: 'art_q1' },
    } },
    { id: 'future', type: 'gauge3d', props: {} },
    { id: 'broken', type: 'metric', props: { label: '毛利率' }, data: { state: 'error' } },
  ],
  forms: {},
  problems: [{ component: 'broken', code: 'view.resolve_failed', message: '解析出错：KeyError' }],
}

const CLAIM_HOME = {
  format: 'agentlab.view/1',
  app: { id: 'app_claim', name: '报销审批', version: 2, level: 'published', needs_upgrade: false },
  page: { id: 'home', title: '报销' },
  pages: [{ id: 'home', title: '报销' }],
  sources: {},
  tree: [
    { id: 'new', type: 'entry_button', props: { entry: 'apply' }, data: { entry: 'apply', label: '发起报销', disabled: false } },
    { id: 'mine', type: 'instance_list', props: { entry: 'apply', who: 'mine' }, data: { who: 'mine', entry: 'apply', items: [] } },
  ],
  forms: {
    apply: {
      format: 'agentlab.form/1', type: 'object', required: ['amount', 'reason'], 'x-order': ['amount', 'reason', 'kind'],
      properties: {
        amount: { type: 'number', title: '金额（元）', minimum: 0 },
        reason: { type: 'string', title: '事由', 'x-widget': 'textarea', maxLength: 500 },
        kind: { type: 'string', title: '类别', enum: ['travel', 'meal'], 'x-enum-labels': ['差旅', '餐费'] },
      },
    },
  },
  problems: [],
}

const TASK = {
  id: 'task_fin', approval_id: 'ap_fin', run_id: 'run_claim_1', node_id: 'finance', app_id: 'app_claim', app_name: '报销审批',
  app_version: 2, workflow_name: '报销流程', instance_title: '张三 的报销', kind: 'approve', title: '财务审批', form: null,
  policy: { kind: 'all' },
  status: 'pending', due_at: new Date(Date.now() + 3600_000).toISOString(), escalation_level: 0,
  payload: { kind: 'human_node', message: '张三报销差旅 **1,200 元**，请审批' },
  assignments: [
    { id: 'as1', principal_kind: 'user', principal: 'lisi', source: 'slot:finance', status: 'responded', responded_by: 'lisi',
      responded_at: ago(600_000), decision: 'approve', verified: false },
    { id: 'as2', principal_kind: 'user', principal: 'wangwu', source: 'slot:finance', status: 'pending', responded_by: null,
      responded_at: null, decision: null, verified: false },
  ],
  can_respond: true, outcome: null, resolved_by: null, resolved_at: null, created_at: ago(1800_000),
}

/** 会签组件和待办表单绑同一条待办：页面解析给的数据一样，只有待办表单带 signoffs_on_page */
function withSignoffsData(instance) {
  const tree = instance.view.tree
  const form = tree.find((n) => n.id === 'review')
  const { signoffs_on_page: _, ...data } = form.data
  tree.find((n) => n.id === 'signs').data = data
  return instance
}

const INSTANCE = withSignoffsData({
  run_id: 'run_claim_1', title: '张三 的报销', entry_id: 'apply', status: 'interrupted', started_by: '张三',
  created_at: ago(1800_000), finished_at: null, pending_tasks: 1, tier: null,
  // 给人看的状态服务端说（apps/resolve.instance_state）：结果未知时是在等核实，不是「等待审批」
  status_code: 'waiting', status_text: '等待核实写入结果',
  view: {
    format: 'agentlab.view/1',
    app: { id: 'app_claim', name: '报销审批', version: 2, level: 'published', needs_upgrade: false },
    page: { id: 'instance', title: '实例' }, pages: [{ id: 'home', title: '报销' }, { id: 'instance', title: '实例' }],
    sources: { inst: { state: 'running', run_id: 'run_claim_1', stamp: null, message: '等待核实写入结果，流程走完后自动显示' } },
    tree: [
      { id: 'inst_header', type: 'instance_header', props: {}, data: {
        state: 'ok', run_id: 'run_claim_1', title: '张三 的报销', status: 'interrupted', status_code: 'waiting',
        status_text: '等待核实写入结果', started_by: '张三', created_at: ago(1800_000), finished_at: null, pending_tasks: 1,
        stamp: null } },
      { id: 'inst_timeline', type: 'instance_timeline', props: {}, data: {
        state: 'ok', status: 'interrupted', status_code: 'waiting', status_text: '等待核实写入结果', steps: [
          { node: 'input', title: '报销单', state: 'done', text: '完成', at: ago(1790_000) },
          { node: 'finance', title: '财务审批', state: 'waiting', text: '等待审批', at: ago(1700_000) },
          { node: 'pay', title: '付款', state: 'unknown', text: '结果未知，等待核实', at: ago(600_000) },
        ] } },
      // 发起人不是这条待办的处理人：服务端解析时判定（handler 为假），只给这一步的状态，界面不去取待办。同一页上另有绑
      // 同一条待办的会签组件（signs）：服务端说一声（signoffs_on_page），待办表单不再画会签进度。人名服务端给显示名（people）
      { id: 'review', type: 'task_form', props: {}, bind: { task: 'review' }, data: {
        state: 'ok', handler: false, task_ui: {}, signoffs_on_page: true,
        people: { lisi: '李四', wangwu: '王五', zhaoliu: '赵六' },
        task: { id: 'task_other', run_id: 'run_claim_1', node_id: 'review', kind: 'approve', title: '复核', status: 'pending',
          due_at: new Date(Date.now() + 3600_000).toISOString(), policy: { kind: 'all' }, escalation_level: 1,
          created_at: ago(1700_000), instance_title: '张三 的报销', form: null },
        assignments: [
          { id: 'r1', principal_kind: 'user', principal: 'lisi', source: 'slot:finance', status: 'responded', responded_by: 'lisi',
            responded_at: ago(900_000), decision: 'approve', verified: true },
          { id: 'r2', principal_kind: 'user', principal: 'wangwu', source: 'slot:finance', status: 'pending', responded_by: null,
            responded_at: null, decision: null, verified: false },
          { id: 'r3', principal_kind: 'user', principal: 'zhaoliu', source: 'escalation', status: 'pending', responded_by: null,
            responded_at: null, decision: null, verified: false },
        ],
        progress: { policy: 'all', voters: 2, need: 2, agreed: 1, rejected: 0,
          escalation: { slot: 'director', assignees: [{ kind: 'user', name: 'zhaoliu' }], decided: null, by: null } },
        my_response: null } },
      { id: 'signs', type: 'signoffs', props: {}, bind: { task: 'review' }, data: null },
      { id: 'inst_tasks', type: 'task_list', props: { scope: 'instance' }, data: { scope: 'instance', items: [] } },
      { id: 'pay', type: 'action_status', props: { label: '付款' }, data: { state: 'ok', node: 'pay', records: [
        { id: 'act1', state: 'unknown', environment: 'prod', attempt: 1, payload_hash: '9c1e44ab',
          note: '发送时没有拿到确定的结果（请求超时），等待核实：在外部系统里可能已经生效，不会自动重发' },
      ] } },
    ],
    forms: {}, problems: [],
  },
})

/**
 * 指标链照服务端的形状（api/evidence 的 _metric_chain）。销售额按位置取数据整形的结果（vars.q.rows[0][0]），整形节点不是
 * 原样解析，出处追到它为止：untraced 写明停在哪个节点。复算照样一致——「有出处」和「追到原始数据」是两件事
 */
const UNTRACED_INPUT = { path: 'vars.q.rows[0][0]', value: 456800, node_id: 'parse', via: 'transform', field: 'rows[0][0]', status: 'ok' }
const METRIC_CHAIN = {
  chain: [
    { step: 'metric', node_id: 'kpi', metric: 'gmv', name: '销售额', value: 456800, expression: 'vars.q.rows[0][0]',
      substituted: '456800', recompute_ok: true, inputs: [UNTRACED_INPUT],
      untraced: [{ path: 'vars.q.rows[0][0]', node_id: 'parse', label: '解析查询结果', type: 'transform' }] },
    { step: 'input', ...UNTRACED_INPUT },
  ],
  seal: { sealed: true, ok: true, covered: true },
  redacted: { columns: [] },
}
/** 同一个指标，整形节点原样解析查询结果：按位置取的格卡上记成单元格取数，穿过整形节点接上查询快照 */
const TRACED_INPUT = { path: 'vars.q.rows[0][0]', value: 456800, node_id: 'fetch', via: 'tool_cell', through: 'parse',
  through_label: '解析查询结果', artifact: 'art_q1', locator: { row: 0, column: 'gmv' }, eid: 'ev:cell:gmv', status: 'verified',
  query: 'Q1', cell: 'Q1.r0.gmv' }
const METRIC_CHAIN_TRACED = {
  ...METRIC_CHAIN,
  chain: [{ ...METRIC_CHAIN.chain[0], inputs: [TRACED_INPUT], untraced: [] }, { step: 'input', ...TRACED_INPUT }],
}

// ---------------------------------------------------------------- 页面上直接改（N6）的假数据

/** 门店周报的草稿：和 HOME 的组件一一对应；stores 被隐藏（服务端按草稿解析照样下发、带 hidden），old_note 也被隐藏
 *  但服务端没画出来（编辑层列在页底） */
const DRAFT = {
  format: 'agentlab.app/1', name: '门店周报', description: '每周一看各门店的销售概况',
  pins: [{ alias: 'wk', workflow_id: 'wf_week', version: 7, graph_hash: '3fa2c1d0', level: 'governed', use: 'view' }],
  entries: [], views: { wk: { source: { kind: 'latest_sealed', pin: 'wk', from: 'formal' } } }, forms: {}, slots: {}, task_ui: {},
  pages: [{ id: 'home', title: '本周概览', body: [
    { id: 'stamp', type: 'stamp', props: {}, bind: { view: 'wk', run: 'status' } },
    { id: 'kpis', type: 'columns', props: { count: 2 }, children: [
      { id: 'gmv', type: 'metric', props: { label: '销售额', unit: '万元', conv: '万' }, bind: { view: 'wk', metric: 'kpi.gmv' } },
      { id: 'orders', type: 'metric', props: { label: '订单数' }, bind: { view: 'wk', metric: 'kpi.orders' } },
      { id: 'shops', type: 'metric', props: { label: '门店数', unit: '家' }, bind: { view: 'wk', cell: { node: 'q1', row: 0, col: 'n' } } },
      { id: 'aov', type: 'metric', props: { label: '客单价', unit: '元' }, bind: { view: 'wk', metric: 'kpi.aov' } },
    ] },
    { id: 'trend', type: 'series', props: { label: '销售额趋势' }, bind: { view: 'wk', series: 'kpi.gmv', last: 12 } },
    { id: 'stores', type: 'table', props: { title: '门店排行' }, hidden: true, bind: { view: 'wk', query: { node: 'q1' } } },
    { id: 'old_note', type: 'notice', props: { text: '旧的说明' }, hidden: true },
    { id: 'future', type: 'gauge3d', props: {} },
    { id: 'broken', type: 'metric', props: { label: '毛利率' }, bind: { view: 'wk', metric: 'kpi.margin' } },
  ] }, { id: 'detail', title: '门店明细', body: [] }],
}
const DETAIL = { ...APPS[0], blueprint_id: 'bp_week', draft: DRAFT, draft_bindings: { slots: {} },
  published: { version: 3, pages: [{ id: 'home', title: '本周概览' }, { id: 'detail', title: '门店明细' }], entries: [] } }
/** 按草稿解析：stores 带 hidden 下发，old_note 不下发 */
const DRAFT_HOME = { ...HOME, tree: HOME.tree.map((n) => (n.id === 'stores' ? { ...n, hidden: true } : n)) }
const CATALOG = { version: 1, categories: { layout: '布局', display: '展示' }, components: [
  { name: 'columns', label: '分栏', category: 'layout', version: 1, children: true, bindings: [],
    props: { type: 'object', properties: { count: { type: 'integer', title: '列数' } } } },
  { name: 'stamp', label: '印章', category: 'display', version: 1, children: false, bindings: ['run'],
    props: { type: 'object', properties: { compact: { type: 'boolean', title: '紧凑显示' } } } },
  { name: 'metric', label: '指标块', category: 'display', version: 1, children: false, bindings: ['metric', 'cell'],
    props: { type: 'object', properties: {
      label: { type: 'string', maxLength: 200, title: '名称', 'x-text': true },
      unit: { type: 'string', maxLength: 200, title: '单位', 'x-text': true },
      conv: { type: 'string', maxLength: 200, title: '换算' } } } },
  { name: 'series', label: '趋势', category: 'display', version: 1, children: false, bindings: ['series'],
    props: { type: 'object', properties: { label: { type: 'string', maxLength: 200, title: '名称', 'x-text': true } } } },
  { name: 'table', label: '表格', category: 'display', version: 1, children: false, bindings: ['query'],
    props: { type: 'object', properties: { title: { type: 'string', maxLength: 200, title: '标题', 'x-text': true } } } },
  { name: 'notice', label: '提示', category: 'display', version: 1, children: false, bindings: [],
    props: { type: 'object', properties: { text: { type: 'string', maxLength: 4000, title: '提示文字', 'x-text': true } } } },
] }
/** 「加一张数据卡片」的候选：口径卡指标两个（其中一个 id 和页面上的 gmv 撞了）、趋势一个 */
const CANDIDATES = { items: [
  { kind: 'metric', label: '退款率', group: '口径卡「周销售」', component: { id: 'refund', type: 'metric', props: { label: '退款率' },
    bind: { view: 'wk', metric: 'kpi.refund' } } },
  { kind: 'metric', label: '销售额', group: '口径卡「周销售」', component: { id: 'gmv', type: 'metric', props: { label: '销售额' },
    bind: { view: 'wk', metric: 'kpi.gmv' } } },
  { kind: 'series', label: '订单数趋势', group: '口径卡「周销售」', component: { id: 'orders_trend', type: 'series',
    props: { label: '订单数趋势' }, bind: { view: 'wk', series: 'kpi.orders', last: 12 } } },
] }

/** 草稿里按 id 找组件 */
const findIn = (spec, id) => {
  const walk = (list) => { for (const c of list ?? []) { if (c.id === id) return c; const hit = walk(c.children); if (hit) return hit } return null }
  for (const pg of spec?.pages ?? []) { const hit = walk(pg.body); if (hit) return hit }
  return null
}

// ---------------------------------------------------------------- 浏览器

const browser = await chromium.launch({ executablePath: CHROME })

async function open({ width = 1280, height = 900, state }) {
  const ctx = await browser.newContext({ viewport: { width, height } })
  opened.add(ctx)
  const page = await ctx.newPage()
  page.errors = []
  page.on('pageerror', (e) => page.errors.push(e.message))
  await page.route((url) => url.pathname.startsWith('/api/'), (route) => {
    const r = route.request()
    const url = new URL(r.url())
    const p = url.pathname
    if (r.method() === 'GET' && p.startsWith('/api/tasks/')) (state.taskReads ??= []).push(p)
    if (r.method() !== 'GET') {
      state.writes.push({ method: r.method(), path: p, body: r.postData() })
      if (p === '/api/apps/app_claim/entries/apply') {
        const body = JSON.parse(r.postData() || '{}')
        if (state.reject422) {
          return route.fulfill({ status: 422, json: { detail: '表单没有通过校验', errors: [
            { path: 'amount', code: 'maximum', message: '不能大于 5000' },
            { path: 'approver', code: 'required', message: '缺少审批人' },
          ] } })
        }
        return route.fulfill({ status: 201, json: { run_id: 'run_claim_1', title: `${body.values?.reason ?? ''}` } })
      }
      if (p.startsWith('/api/tasks/') && p.endsWith('/respond')) {
        return route.fulfill({ json: { task: { ...TASK, status: 'resolved', can_respond: false }, resolved: true } })
      }
      // 页面上直接改：存草稿回详情（带上存进去的草稿），发布回新版本
      if (state.detail && r.method() === 'PATCH' && p === '/api/apps/app_weekly') {
        const body = JSON.parse(r.postData() || '{}')
        state.patches = [...(state.patches ?? []), body]
        state.detail = { ...state.detail, ...(body.draft ? { draft: body.draft } : {}) }
        return route.fulfill({ json: state.detail })
      }
      if (state.detail && p === '/api/apps/app_weekly/publish') {
        return route.fulfill({ json: { ok: true, version: 4, level: 'governed', spec_hash: 'h4', issues: [] } })
      }
      return route.fulfill({ status: 503, json: { detail: '检查脚本拦下了写请求' } })
    }
    if (p === '/api/auth/me') return route.fulfill({ json: state.me ?? { mode: 'off', username: null, display_name: null, role: 'admin', groups: [], verified: false } })
    if (p === '/api/apps') return route.fulfill({ json: { items: APPS } })
    // 应用详情：默认不给草稿（不进编辑状态）；页面上直接改的段落给 manage 的详情
    if (p === '/api/apps/app_weekly') return route.fulfill({ json: state.detail ?? { ...APPS[0], published: DETAIL.published } })
    if (p === '/api/apps/app_claim') return route.fulfill({ json: { ...APPS[1], published: { version: 2, pages: [{ id: 'home', title: '报销' }], entries: [] } } })
    if (p === '/api/apps/catalog') return route.fulfill({ json: CATALOG })
    if (p === '/api/apps/app_weekly/candidates') {
      state.candidateHits = (state.candidateHits ?? 0) + 1
      return route.fulfill({ json: CANDIDATES })
    }
    if (p === '/api/apps/app_weekly/pages/home' && url.searchParams.get('draft') === '1') {
      state.draftHits = (state.draftHits ?? 0) + 1
      return route.fulfill({ json: state.draftHome ?? DRAFT_HOME })
    }
    if (p === '/api/apps/app_weekly/pages/home') {
      state.pageHits = (state.pageHits ?? 0) + 1
      return route.fulfill({ json: state.home ?? HOME })
    }
    if (p === '/api/apps/app_weekly/pages/detail') return route.fulfill({ json: { ...HOME, page: { id: 'detail', title: '门店明细' }, tree: [] } })
    if (p === '/api/apps/app_claim/pages/home') return route.fulfill({ json: state.claimHome ?? CLAIM_HOME })
    if (/^\/api\/apps\/[^/]+\/instances$/.test(p)) return route.fulfill({ json: { items: p.includes('app_claim') ? [INSTANCE] : [] } })
    if (p === '/api/apps/app_claim/instances/run_claim_1') return route.fulfill({ json: INSTANCE })
    if (p === `/api/runs/${RUN}/evidence/metrics/kpi/gmv`) {
      state.chainHits = (state.chainHits ?? 0) + 1
      return route.fulfill({ json: state.traced ? METRIC_CHAIN_TRACED : METRIC_CHAIN })
    }
    if (p === '/api/tasks/count') return route.fulfill({ json: { mine: 1, apps: 1, other: 0 } })
    if (p === '/api/tasks') return route.fulfill({ json: { items: url.searchParams.get('status') === 'resolved' ? [] : [TASK] } })
    if (p === '/api/tasks/task_fin') return route.fulfill({ json: TASK })
    return route.continue()
  })
  return { ctx, page }
}

const noHScroll = (page) => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)

// ---------------------------------------------------------------- 首页

await section('首页：三个页签、关登录说明、需升级', async () => {
  const state = { writes: [] }
  const { page } = await open({ state })
  await page.goto(`${WEB}/apps`)
  const tabs = page.getByRole('tablist', { name: '应用' })
  await tabs.waitFor({ timeout: 15000 })
  check('三个页签：待办、应用、我发起的', (await tabs.getByRole('tab').allInnerTexts()).map((t) => t.replace(/\d+/g, '').trim()).join(',') === '待办,应用,我发起的')
  check('默认是待办页签', (await page.locator('[data-apps-home]').getAttribute('data-apps-home')) === 'inbox')
  check('关登录时挂一行说明（自报署名）', /未开启登录/.test(await page.locator('[data-auth-off]').innerText()))
  check('待办按应用分组', (await page.locator('[data-inbox-group="app_claim"]').count()) === 1)
  check('会签的待办写进度', /1\/2 已同意/.test(await page.locator('[data-inbox-signoffs]').innerText()))
  check('待办一行写所属实例和开出的时间（同标题的分得开）',
    /张三 的报销 · .*发起/.test(await page.locator('[data-inbox-meta]').first().innerText()),
    await page.locator('[data-inbox-meta]').first().innerText())
  await tabs.getByRole('tab', { name: /应用/ }).click()
  check('页签记在地址上', /tab=apps/.test(page.url()), page.url())
  await page.locator('[data-app]').first().waitFor({ timeout: 5000 })
  check('两个应用卡', (await page.locator('[data-app]').count()) === 2)
  check('需升级的应用照实标出', (await page.locator('[data-app="app_claim"] [data-app-needs-upgrade]').count()) === 1)
  check('能管理的应用有「编辑」，只能用的没有',
    (await page.locator('[data-app="app_weekly"] a[href$="/edit"]').count()) === 1
    && (await page.locator('[data-app="app_claim"] a[href$="/edit"]').count()) === 0)
  // 新建应用（normal-user-apps §4.1）：搭建者及以上在页头看得到（关登录时所有人等同管理员），点进去是三个起点的新建页，
  // 建的是共享蓝图。起点和蓝图页本身在 check-blueprint 里查
  const create = page.locator('header [data-app-create]')
  check('页头有「新建应用」，链到新建页（三个起点）', await create.count() === 1
    && (await create.innerText()).includes('新建应用') && await create.getAttribute('href') === '/apps/create'
    && await create.getAttribute('data-app-create') === 'shared',
    `${await create.count()} ｜ ${await create.getAttribute('href').catch(() => '')}`)
  await page.getByRole('tab', { name: '我发起的' }).click()
  await page.locator('[data-mine] [data-instance]').first().waitFor({ timeout: 5000 })
  check('我发起的：合各应用的实例，链到实例页',
    (await page.locator('[data-mine] a[href="/apps/app_claim/i/run_claim_1"]').count()) === 1)
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')

  // 开了登录的使用者、管理员没开个人应用：没有「新建应用」（开了的情形在 check-blueprint 里查）
  const user = { writes: [], me: { mode: 'local', username: 'zhangsan', display_name: '张三', role: 'user', groups: [], verified: true } }
  const u = await open({ state: user })
  await u.page.goto(`${WEB}/apps?tab=apps`)
  await u.page.locator('[data-app]').first().waitFor({ timeout: 15000 })
  check('使用者的应用首页没有「新建应用」', await u.page.locator('[data-app-create], [data-app-create-empty]').count() === 0)
  check('……没有未捕获的运行时错误', u.page.errors.length === 0, u.page.errors[0] ?? '')
})

// ---------------------------------------------------------------- 应用页

await section('应用页：印章、指标、出处、趋势、表格、未知组件', async () => {
  const state = { writes: [] }
  const { page } = await open({ state })
  await page.goto(`${WEB}/apps/app_weekly`)
  await page.locator('[data-view-renderer]').waitFor({ timeout: 15000 })
  const stamp = page.locator('[data-stamp]').first()
  const stampText = await stamp.innerText()
  check('印章四项：截至、口径、出具档位、来源', ['截至', '口径', '出具档位', '来源'].every((k) => stampText.includes(k)), stampText.replace(/\n/g, ' '))
  // 截至是服务端给的整句（自己带着「截至」）：完整、紧凑两种印章都只写一次，不写成「截至 截至 …」
  const stampTexts = await page.locator('[data-stamp]').allInnerTexts()
  check('印章里「截至」只出现一次（完整和紧凑两种）', stampTexts.length > 0 && stampTexts.every((t) => t.split('截至').length === 2),
    stampTexts.map((t) => t.replace(/\s+/g, ' ').slice(0, 40)).join(' | '))
  check('印章写口径名和哈希前 8 位', stampText.includes('周销售 v7 · 3fa2c1d0') && stampText.includes('周销售口径 v3'))
  check('服务端标出的错位照写', (await stamp.locator('[data-stamp-note]').innerText()).includes('固定在 v7'))
  check('页面有两页：页签', (await page.getByRole('tab').count()) >= 2)

  const gmv = page.locator('[data-node="gmv"] button[data-evidence="metric"]')
  check('指标块的数是按钮', (await gmv.innerText()).includes('45.68 万元'))
  const gmvText = await page.locator('[data-node="gmv"] [data-component="metric"]').innerText()
  check('绑定指标：单位只写在数里，下面不再重复一行', gmvText.split('万元').length === 2, gmvText.replace(/\s+/g, ' '))
  const shops = page.locator('[data-node="shops"] [data-component="metric"]')
  check('绑定单元格：数不带单位，单位写在下面', (await shops.innerText()).replace(/\s+/g, ' ').trim() === '门店数 128 家',
    (await shops.innerText()).replace(/\s+/g, ' '))
  const aov = (await page.locator('[data-node="aov"] [data-component="metric"]').innerText()).replace(/\s+/g, ' ').trim()
  check('绑定指标、口径卡没写单位：写搭建者在属性里填的单位，不吞掉', aov === '客单价 356.2 元', aov)
  check('没有出处的数照实标「无证据」', (await page.locator('[data-node="orders"] [data-evidence="none"]').innerText()).includes('无证据'))
  await gmv.click()
  const panel = page.locator('[data-evidence-panel]')
  check('点数字打开出处面板', await panel.waitFor({ timeout: 5000 }).then(() => true, () => false))
  check('走的是指标链接口', state.chainHits === 1, `${state.chainHits ?? 0} 次`)
  // 有出处（口径卡确定性复算一致）和追到原始数据分开说：追不到的不能只写「有出处」
  const origin = panel.locator('[data-ev-origin]')
  check('复算一致照写', (await panel.locator('[data-ev-recompute]').getAttribute('data-ev-recompute')) === 'ok')
  check('输入追不到原始数据：照实说追到哪个节点为止', await origin.getAttribute('data-ev-origin') === 'untraced'
    && (await origin.innerText()).includes('vars.q.rows[0][0] 的出处追到「解析查询结果」（数据整形）为止，没追到原始数据'),
    await origin.innerText().catch(() => ''))
  check('……指标那一行另挂「未追到原始数据」', (await panel.locator('[data-ev-untraced-chip]').innerText()) === '未追到原始数据')
  await page.keyboard.press('Escape')

  const series = page.locator('[data-component="series"]')
  check('趋势是 SVG', (await series.locator('svg[role="img"]').count()) === 1)
  check('升版不可比处画断开线', (await series.locator('[data-plot-break]').count()) === 1)
  check('升版说明写在图下', (await series.locator('[data-series-note]').innerText()).includes('前后不可比'))
  check('两条线有图例', (await series.locator('ul li').count()) === 2)
  await series.getByRole('button', { name: '查看数据' }).click()
  check('「查看数据」给出表格视图', (await series.locator('table tbody tr').count()) === 5)

  const table = page.locator('[data-component="table"]')
  const firstCell = () => table.locator('tbody tr').first().locator('td').first().innerText()
  check('表格按服务端给的顺序', (await firstCell()) === '南山店')
  await table.getByRole('button', { name: /^销售额/ }).click()
  check('点表头升序', (await firstCell()) === '福田店', await firstCell())
  await table.getByRole('button', { name: /^销售额/ }).click()
  check('再点降序', (await firstCell()) === '宝安店', await firstCell())
  check('遮罩列写「已遮罩」', (await table.locator('td[data-masked]').count()) === 3)

  check('不认识的组件写「此组件需要更新前端」', (await page.locator('[data-unknown-component="gauge3d"]').innerText()).includes('此组件需要更新前端'))
  check('服务端解析出错的组件列在顶上', (await page.locator('[data-view-problems]').innerText()).includes('解析出错'))
  check('出错的组件就地说明', (await page.locator('[data-node="broken"] [data-block-state="error"]').count()) === 1)
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')

  // 整形节点原样解析查询结果：按位置取的格穿过它接上查询快照，面板写明经过哪个节点、输入都追到了原始数据
  const traced = { writes: [], traced: true }
  const t = await open({ state: traced })
  await t.page.goto(`${WEB}/apps/app_weekly`)
  await t.page.locator('[data-node="gmv"] button[data-evidence="metric"]').click()
  const tp = t.page.locator('[data-evidence-panel]')
  await tp.locator('[data-ev-origin]').waitFor({ timeout: 5000 })
  check('穿过原样解析的整形节点：输入都追到了原始数据，不挂「未追到」', await tp.locator('[data-ev-origin]').getAttribute('data-ev-origin') === 'traced'
    && (await tp.locator('[data-ev-origin]').innerText()).includes('输入都追到了原始数据')
    && await tp.locator('[data-ev-untraced-chip]').count() === 0)
  const source = tp.locator('[data-ev-source="vars.q.rows[0][0]"]')
  check('……输入来源写明经过哪个整形节点、落在哪一格', (await source.innerText()).includes('经「解析查询结果」原样解析')
    && (await source.innerText()).includes('与快照一致'), (await source.innerText().catch(() => '')).replace(/\s+/g, ' '))
  check('……没有未捕获的运行时错误', t.page.errors.length === 0, t.page.errors[0] ?? '')
})

await section('应用页：视图生成中自动重取、需升级提示', async () => {
  const running = { ...HOME, app: { ...HOME.app, needs_upgrade: true },
    sources: { wk: { state: 'running', run_id: RUN, stamp: null, message: '正在运行，完成后自动刷新' } },
    tree: [{ id: 'stamp', type: 'stamp', props: {}, data: { view: 'wk', state: 'running', stamp: null, message: '正在运行，完成后自动刷新' } }] }
  const state = { writes: [], home: running }
  const { page } = await open({ state })
  await page.goto(`${WEB}/apps/app_weekly`)
  await page.locator('[data-view-renderer]').waitFor({ timeout: 15000 })
  check('需升级照实提示', (await page.locator('[data-app-needs-upgrade]').count()) === 1)
  check('生成中写明在等', (await page.locator('[data-block-state="running"]').count()) === 1)
  const before = state.pageHits
  state.home = HOME
  await page.waitForTimeout(6000)
  check('有视图在生成时 5 秒后自动重取', state.pageHits > before, `${before} → ${state.pageHits}`)
  check('生成完换上数据', (await page.locator('[data-node="gmv"]').count()) === 1)
})

// ---------------------------------------------------------------- 入口

await section('入口：本地校验、422 挂回字段、发起后进实例页', async () => {
  const state = { writes: [] }
  const { page } = await open({ state })
  await page.goto(`${WEB}/apps/app_claim/new/apply`)
  const form = page.locator('[data-schema-form]')
  await form.waitFor({ timeout: 15000 })
  check('标题用发起按钮上的名字', (await page.locator('h2').first().innerText()) === '发起报销')
  check('必填有星号', (await form.locator('[data-field="amount"] label span[aria-hidden]').innerText()) === '*')
  check('字段按 x-order 排', (await form.locator('[data-field]').evaluateAll((els) => els.map((e) => e.getAttribute('data-field')))).join(',') === 'amount,reason,kind')
  check('枚举画成下拉，显示中文', (await form.locator('[data-field="kind"] option').allInnerTexts()).includes('差旅'))
  await page.locator('[data-entry-submit]').click()
  check('必填没填本地拦下，不发请求', !state.writes.some((w) => w.path.endsWith('/entries/apply')))
  check('错误挂在字段下面', (await form.locator('[data-field="amount"] [data-field-error="required"]').count()) === 1)
  await form.locator('[data-field="amount"] input').fill('1.')
  check('数字框能打出小数点', (await form.locator('[data-field="amount"] input').inputValue()) === '1.')
  await form.locator('[data-field="amount"] input').fill('-5')
  await form.locator('[data-field="reason"] textarea').fill('出差深圳')
  await page.locator('[data-entry-submit]').click()
  check('金额低于下限（minimum）本地拦下，不发请求',
    (await form.locator('[data-field="amount"] [data-field-error="minimum"]').count()) === 1
    && !state.writes.some((w) => w.path.endsWith('/entries/apply')))
  await form.locator('[data-field="amount"] input').fill('9000')
  state.reject422 = true
  await page.locator('[data-entry-submit]').click()
  await form.locator('[data-field="amount"] [data-field-error="maximum"]').waitFor({ timeout: 5000 }).catch(() => {})
  check('服务端 422 按 path 挂回字段', (await form.locator('[data-field="amount"] [data-field-error="maximum"]').innerText()).includes('5000'))
  check('对不上字段的错误列在表单顶上', (await form.locator('[data-form-errors]').innerText()).includes('缺少审批人'))
  const sent = JSON.parse(state.writes.find((w) => w.path.endsWith('/entries/apply')).body)
  check('交上去的金额是数，不是字符串', sent.values.amount === 9000, JSON.stringify(sent.values))
  state.reject422 = false
  await form.locator('[data-field="amount"] input').fill('1200')
  await form.locator('[data-field="reason"] textarea').press(process.platform === 'darwin' ? 'Meta+Enter' : 'Control+Enter')
  check('⌘/Ctrl+Enter 发起，成功后进实例页', await page.waitForURL(/\/apps\/app_claim\/i\/run_claim_1/, { timeout: 5000 }).then(() => true, () => false), page.url())
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')
})

await section('入口：需升级时不让发起', async () => {
  const state = { writes: [], claimHome: { ...CLAIM_HOME, app: { ...CLAIM_HOME.app, needs_upgrade: true } } }
  const { page } = await open({ state })
  await page.goto(`${WEB}/apps/app_claim/new/apply`)
  await page.locator('[data-schema-form]').waitFor({ timeout: 15000 })
  check('写明需升级', (await page.locator('[data-app-needs-upgrade]').count()) === 1)
  check('发起按钮停用', await page.locator('[data-entry-submit]').isDisabled())
})

// ---------------------------------------------------------------- 实例

await section('实例页：状态、进度、待办卡、动作状态', async () => {
  const state = { writes: [] }
  const { page } = await open({ state })
  await page.goto(`${WEB}/apps/app_claim/i/run_claim_1`)
  await page.locator('[data-view-renderer]').waitFor({ timeout: 15000 })
  check('写出发起人', (await page.locator('[data-instance-page]').innerText()).includes('由「张三」发起'))
  check('实例状态按服务端的说法写（结果未知：等待核实写入结果，不是「等待审批」）',
    (await page.locator('[data-component="instance_header"] span[data-status]').first().innerText()).includes('等待核实写入结果')
    && !(await page.locator('[data-component="instance_header"]').innerText()).includes('等待审批'))
  check('进度按节点列出', (await page.locator('[data-timeline-step]').count()) === 3)
  check('结果未知的那一步写「结果未知」，不写「完成」',
    (await page.locator('[data-timeline-step="unknown"]').innerText()).includes('结果未知'))
  const other = page.locator('[data-component="task_form"][data-task-handler="no"]')
  check('不是处理人：只画这一步的状态（在等谁），不出红框',
    (await other.locator('[data-task-state="pending"]').count()) === 1 && (await other.locator('[role="alert"]').count()) === 0
    && (await other.innerText()).includes('等待 王五、赵六 处理'), (await other.innerText()).replace(/\n/g, ' '))
  check('……等待名单写显示名，悬停给用户名', (await other.locator('[data-task-waiting-on]').getAttribute('title')) === 'wangwu、zhaoliu')
  const signs = page.locator('[data-component="signoffs"]')
  check('同一页上有会签组件：待办表单不再画会签进度，会签进度只画一遍', (await other.locator('[data-signoff-head]').count()) === 0
    && (await signs.locator('[data-signoff-head]').count()) === 1 && (await signs.innerText()).includes('1/2 已同意')
    && (await signs.innerText()).includes('已升级给岗位「director」'), (await signs.innerText()).replace(/\n/g, ' '))
  const lisi = signs.locator('[data-assignee]').first()
  check('……会签名单写显示名，悬停给用户名', (await lisi.innerText()) === '李四' && (await lisi.getAttribute('title')) === 'lisi'
    && (await signs.innerText()).includes('赵六'), (await signs.innerText()).replace(/\n/g, ' '))
  check('……也不去取那条待办', !(state.taskReads ?? []).includes('/api/tasks/task_other'), JSON.stringify(state.taskReads))
  await page.locator('[data-component="task_list"] [data-task]').waitFor({ timeout: 5000 })
  check('本实例的待办就地处理（完整的待办卡）', (await page.locator('[data-component="task_list"] [data-task-approve]').count()) === 1)
  check('动作状态写结果未知和环境', /结果未知/.test(await page.locator('[data-action="unknown"]').innerText())
    && /生产环境/.test(await page.locator('[data-action="unknown"]').innerText()))
  check('结果未知的经过记一笔，不当「失败原因」',
    (await page.locator('[data-action="unknown"] [data-action-error]').count()) === 0
    && /不会自动重发/.test(await page.locator('[data-action="unknown"] [data-action-note]').innerText()))
  check('关登录（等同管理员）时接运行流', (await page.locator('[data-instance-follow]').getAttribute('data-instance-follow')) === 'stream')
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')
})

// ---------------------------------------------------------------- 页面上直接改（N6）

await section('应用页：编辑状态、改文字、隐藏、上下挪、删除、加一张数据卡片、发布', async () => {
  const state = { writes: [], detail: structuredClone(DETAIL) }
  const { page } = await open({ state })
  await page.goto(`${WEB}/apps/app_weekly`)
  await page.locator('[data-view-renderer]').waitFor({ timeout: 15000 })
  const start = page.locator('[data-page-edit-start]')
  check('有 manage 的人有「编辑」', await start.waitFor({ timeout: 5000 }).then(() => true, () => false)
    && (await start.innerText()).includes('编辑'))
  await start.click()
  await page.locator('[data-page-edit-layer]').waitFor({ timeout: 8000 })
  check('编辑状态记在地址上（edit=1）', /[?&]edit=1/.test(page.url()), page.url())
  check('按草稿解析（draft=1）', (state.draftHits ?? 0) >= 1, `${state.draftHits ?? 0} 次`)
  check('顶部的编辑条：写「编辑中」，给「发布」', /编辑中/.test(await page.locator('[data-page-edit-bar]').innerText())
    && await page.locator('[data-page-edit-publish]').count() === 1)
  const changeData = page.locator('[data-page-edit-change-data]')
  check('应用出自蓝图：给「改取数」，链到蓝图页', await changeData.count() === 1
    && await changeData.getAttribute('href') === '/blueprints/bp_week', await changeData.getAttribute('href').catch(() => ''))

  // 隐藏：服务端带 hidden 下发的画成半透明、标「已隐藏」；没下发的列在页底
  const stores = page.locator('[data-node="stores"]')
  check('隐藏的组件画成半透明', await stores.getAttribute('data-node-hidden') === ''
    && /opacity-45/.test(await stores.getAttribute('class') ?? ''), await stores.getAttribute('class').catch(() => ''))
  check('……旁边标「已隐藏」', await page.locator('[data-hidden-tag]').count() >= 1)
  const hiddenList = page.locator('[data-page-edit-hidden-list]')
  check('服务端没画出来的隐藏组件列在页底，能取消隐藏', await hiddenList.getAttribute('data-page-edit-hidden-list') === '1'
    && await hiddenList.locator('[data-page-edit-unhide="old_note"]').count() === 1)

  // 点组件是选中，不打开出处
  await page.locator('[data-node="gmv"] button[data-evidence="metric"]').click()
  const toolbar = page.locator('[data-page-edit-toolbar="gmv"]')
  check('点组件出小工具条（选中）', await toolbar.waitFor({ timeout: 3000 }).then(() => true, () => false))
  check('编辑状态下点数字不打开出处', await page.locator('[data-evidence-panel]').count() === 0 && !state.chainHits)
  const ops = await toolbar.locator('[data-page-edit-op]').evaluateAll((els) => els.map((e) => e.getAttribute('data-page-edit-op')))
  check('小工具条五样：改文字、隐藏、上移、下移、删除', ops.join(',') === 'text,hide,up,down,remove', ops.join(','))

  // 改文字：只给标了 x-text 的属性（名称、单位），不给换算
  await toolbar.locator('[data-page-edit-op="text"]').click()
  const dlg = page.getByRole('dialog')
  await dlg.locator('[data-page-edit-text]').waitFor({ timeout: 3000 })
  const fields = await dlg.locator('[data-page-edit-text-field]').evaluateAll((els) => els.map((e) => e.getAttribute('data-page-edit-text-field')))
  check('改文字只列给人看的文字（名称、单位），不列换算', fields.join(',') === 'label,unit', fields.join(','))
  await dlg.locator('[data-page-edit-text-field="label"] input').fill('本周销售额')
  const before = state.draftHits ?? 0
  await dlg.locator('[data-page-edit-text-save]').click()
  await page.waitForFunction(() => document.querySelector('[data-page-edit-save]')?.getAttribute('data-page-edit-save') === 'saved', null, { timeout: 5000 }).catch(() => {})
  check('改文字自动存草稿（PATCH 带整份草稿）', findIn(state.patches?.at(-1)?.draft, 'gmv')?.props?.label === '本周销售额',
    JSON.stringify(findIn(state.patches?.at(-1)?.draft, 'gmv')?.props ?? {}))
  check('……存完按草稿重新解析', (state.draftHits ?? 0) > before, `${before} → ${state.draftHits ?? 0}`)
  check('……顶部写「有未发布的改动」', (await page.locator('[data-page-edit-status]').innerText()).includes('有未发布的改动'))

  // 隐藏
  await page.locator('[data-node="gmv"]').click()
  await page.locator('[data-page-edit-toolbar="gmv"] [data-page-edit-op="hide"]').click()
  await page.waitForTimeout(400)
  check('隐藏：草稿里这个组件 hidden 为真', findIn(state.patches?.at(-1)?.draft, 'gmv')?.hidden === true)

  // 同一层里下移一格
  await page.locator('[data-node="orders"]').click()
  await page.locator('[data-page-edit-toolbar="orders"] [data-page-edit-op="down"]').click()
  await page.waitForTimeout(400)
  const order = findIn(state.patches?.at(-1)?.draft, 'kpis')?.children?.map((c) => c.id).join(',')
  check('下移：同一层里和下一个换位置', order === 'gmv,shops,orders,aov', order)
  check('第一个组件不能上移', await page.locator('[data-node="stamp"]').click().then(async () =>
    page.locator('[data-page-edit-toolbar="stamp"] [data-page-edit-op="up"]').isDisabled()))

  // 删除要确认
  await page.locator('[data-node="broken"]').click()
  await page.locator('[data-page-edit-toolbar="broken"] [data-page-edit-op="remove"]').click()
  const confirm = page.locator('[role="dialog"], [role="alertdialog"]').filter({ hasText: '删除「毛利率」？' }).first()
  check('删除先确认、写明后果', await confirm.waitFor({ timeout: 3000 }).then(() => true, () => false))
  await confirm.getByRole('button', { name: '删除组件' }).click()
  await page.waitForTimeout(400)
  check('删除：草稿里没有这个组件了', !!state.patches?.at(-1)?.draft && findIn(state.patches.at(-1).draft, 'broken') === null)

  // 取消隐藏页底列出的
  await page.locator('[data-page-edit-unhide="old_note"]').click()
  await page.waitForTimeout(400)
  check('页底的「取消隐藏」：草稿里去掉 hidden', findIn(state.patches?.at(-1)?.draft, 'old_note') && !('hidden' in findIn(state.patches.at(-1).draft, 'old_note')))

  // 加一张数据卡片：候选按类别分组，id 撞了换新的
  await page.locator('[data-page-edit-add-card]').click()
  const cand = page.getByRole('dialog')
  await cand.locator('[data-page-edit-candidates]').waitFor({ timeout: 3000 })
  const groups = await cand.locator('[data-candidate-group]').evaluateAll((els) => els.map((e) => e.getAttribute('data-candidate-group')))
  check('候选按类别分组（口径卡指标、趋势）', groups.join(',') === 'metric,series', groups.join(','))
  check('……组名写中文', (await cand.locator('[data-candidate-group="metric"] h3').innerText()) === '口径卡指标')
  await cand.locator('[data-candidate="gmv"] [data-candidate-add]').click()
  await page.waitForTimeout(400)
  const home = state.patches?.at(-1)?.draft?.pages?.[0]?.body ?? []
  check('加上的卡片落在页尾，id 和已有的撞了换成新的', home.at(-1)?.id === 'gmv_2' && home.at(-1)?.bind?.metric === 'kpi.gmv',
    JSON.stringify(home.at(-1) ?? {}))

  // 发布
  const writesBefore = state.writes.length
  await page.locator('[data-page-edit-publish]').click()
  const pub = page.locator('[role="dialog"], [role="alertdialog"]').filter({ hasText: '发布这些改动？' }).first()
  await pub.waitFor({ timeout: 3000 })
  await pub.getByRole('button', { name: '发布', exact: true }).click()
  await page.waitForTimeout(500)
  check('「发布」走应用发布', state.writes.slice(writesBefore).some((w) => w.method === 'POST' && w.path === '/api/apps/app_weekly/publish'))
  check('……发布后不再写「有未发布的改动」', !(await page.locator('[data-page-edit-status]').innerText().catch(() => '')).includes('有未发布的改动'))

  await page.locator('[data-page-edit-done]').click()
  await page.locator('[data-page-edit-start]').waitFor({ timeout: 5000 })
  check('完成编辑：回到使用者看到的样子（地址上去掉 edit）', !/[?&]edit=1/.test(page.url()), page.url())
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')
})

await section('应用页：只能用的人没有「编辑」，编辑地址也不进编辑状态', async () => {
  const state = { writes: [] }
  const { page } = await open({ state })
  await page.goto(`${WEB}/apps/app_claim?edit=1`)
  await page.locator('[data-view-renderer]').waitFor({ timeout: 15000 })
  await page.waitForTimeout(500)
  check('使用者没有「编辑」', await page.locator('[data-page-edit-start]').count() === 0)
  check('带着 edit=1 也不进编辑状态', await page.locator('[data-page-edit-layer]').count() === 0)
})

await section('窄屏：390px 不出横向滚动', async () => {
  const state = { writes: [] }
  const { page } = await open({ width: 390, height: 844, state })
  for (const path of ['/apps', '/apps/app_weekly', '/apps/app_claim/new/apply', '/apps/app_claim/i/run_claim_1']) {
    await page.goto(`${WEB}${path}`)
    await page.waitForTimeout(800)
    check(`${path} 没有横向滚动`, await noHScroll(page))
  }
})

// 推演、请模型判断只给搭建者及以上（接口同样只给）：使用者的出处面板里不挂这些按钮，不让人点了再被拒（V1-93）。
// 同一个面板、关登录时照常有「推演」，两头对照
await section('使用者的出处面板：不给推演，遮罩说明按使用者说', async () => {
  // 指标链带一列遮罩：面板底下的遮罩说明对谁说什么
  const masked = (page) => page.route(`**/api/runs/${RUN}/evidence/metrics/kpi/gmv`,
    (route) => route.fulfill({ json: { ...METRIC_CHAIN, redacted: { columns: ['phone'] } } }))
  const off = await open({ state: { writes: [] } })
  await masked(off.page)
  await off.page.goto(`${WEB}/apps/app_weekly`)
  await off.page.locator('[data-node="gmv"] button[data-evidence="metric"]').click()
  const offPanel = off.page.locator('[data-evidence-panel]')
  await offPanel.locator('[data-ev-metric]').waitFor({ timeout: 8000 })
  check('关登录（所有人等同管理员）：指标上有「推演」', await offPanel.locator('[data-whatif-toggle]').count() === 1)
  check('关登录：遮罩只减少暴露，不是安全边界', (await offPanel.locator('[data-ev-mask-note]').innerText().catch(() => ''))
    .includes('遮罩只减少暴露，不是安全边界'))

  const state = { writes: [], me: { mode: 'local', username: 'zhangsan', display_name: '张三', role: 'user', groups: [], verified: true } }
  const { page } = await open({ state })
  await masked(page)
  await page.goto(`${WEB}/apps/app_weekly`)
  await page.locator('[data-node="gmv"] button[data-evidence="metric"]').click()
  const panel = page.locator('[data-evidence-panel]')
  await panel.locator('[data-ev-metric]').waitFor({ timeout: 8000 })
  check('使用者：出处照常看，没有「推演」', await panel.locator('[data-whatif-toggle]').count() === 0)
  check('使用者：没有「请模型判断」', await panel.locator('[data-ev-judge-ask]').count() === 0)
  const note = panel.locator('[data-ev-mask-note]')
  check('使用者：遮罩对他是边界，不说「完整快照中仍为原值」', await note.getAttribute('data-ev-mask-note') === 'user'
    && (await note.innerText()).includes('原值不对使用者开放') && !(await note.innerText()).includes('仍为原值'), await note.innerText().catch(() => ''))
  check('没有发出只给搭建者的请求', !state.writes.some((w) => /whatif|evidence\/judge|inquiries/.test(w.path)),
    state.writes.map((w) => w.path).join(','))
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')
})

await browser.close()
console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 全部通过')
process.exit(failed ? 1 : 0)
