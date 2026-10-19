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
//   - 驳回之后去哪（中断内容的 reject_route）：接了驳回出口的提示、驳回后的提示、处理过的结果写「转到「x」」，子工作流里的
//     写「子工作流 › x」、不露内部 id；补充输入接了出口的按钮写「驳回」，没接出口的工具审批写「驳回并终止」；
//   - 关登录时签批人一行写本机署名；会签只记下一票（resolved 为假）时提示「等待其他人处理」；
//   - 审核个人应用（kind=review，payload 同服务端 blueprints/personal.review_payload）：写提交人（显示名）、目的、说明；没有
//     「同意」——「取副本，去发布」调 POST /api/blueprints/{id}/copy 后进副本蓝图；取过的给「打开我的副本」（只认自己取的那
//     一份，别人取的不算）；「退回」可以写原因，走待办接口 decision=reject。
//   - 口径召回（kind=recall，payload 同服务端 ledger/recall.task_payload，next-round 02 做法 7）：旧口径、新口径、两版之差
//     一行一个指标，每个数点得开出处（召回卡的指标链）；每个答案当时的值也点得开（正式运行的口径卡、问数回答的查询快照）；
//     问数回答链回那段对话；「按新版重新出具」decision=reissue、「知道了」decision=ack；服务端说不能重新出具时按钮停用、
//     写原因；使用者看到的遮罩值写「已遮罩」、点不开；目录有变那一种只有「知道了」，列出变了的目录项和新出现的问题。
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
  // 口径召回：口径升到 v2，这一组答案的数变了（payload 同服务端 ledger/recall.task_payload）
  recall: {
    ...base, id: 'recall', kind: 'recall', title: '口径「周销售」升到 v2，答案的数变了', node_id: null, app_id: null,
    app_name: null, app_version: null, workflow_name: '口径召回：周销售 v1 → v2', run_id: 'rc_1',
    assignments: [assignment('rc1', '审批人甲', 'pending', null, { source: 'recall' })],
    payload: { kind: 'recall', reason: 'caliber', recall_run: 'rc_1', workflow_id: 'wf_week', workflow_name: '周销售',
      from_version: 1, to_version: 2,
      binding: [{ field: 'end', value: '2026-09-08', masked: false }, { field: 'start', value: '2026-09-01', masked: false }],
      metrics: [{ k: 0, name: '销售额', unit: '元', changed: true, card: 'card', metric: 'gmv',
        old: { value: 170.5, text: '170.50元', masked: false, metric: 'old_0' },
        new: { value: 208.5, text: '208.50元', masked: false, metric: 'new_0' },
        diff: { value: 38, text: '38.00元', masked: false, metric: 'diff_0' } }],
      answers: [
        { kind: 'answer', kind_text: '问数回答', run_id: 'run_ans_1', version: 1, conversation_id: 'conv_1', turn_id: 'turn_1',
          at: ago(86400_000), original: [{ k: 0, value: 170.5, text: '170.50元', masked: false, query: 'snap_1', column: '销售额' }] },
        { kind: 'schedule', kind_text: '定时任务', run_id: 'run_fm_1', version: 1, schedule_id: 'sch_1', at: ago(3 * 86400_000),
          original: [{ k: 0, value: 165, text: '165.00元', masked: false, node: 'card', metric: 'gmv' }] },
      ],
      actions: [{ decision: 'reissue', available: true }, { decision: 'ack', available: true }] },
  },
  // 使用者看的：遮罩值写「已遮罩」、点不开；没有被他能用的应用固定，服务端说不能重新出具
  recallUser: {
    ...base, id: 'recallUser', kind: 'recall', title: '口径「客户分层」升到 v3，答案的数变了', node_id: null, app_id: null,
    app_name: null, app_version: null, workflow_name: '口径召回：客户分层 v2 → v3', run_id: 'rc_2',
    assignments: [assignment('rc2', 'zhangsan', 'pending', null, { source: 'recall' })],
    payload: { kind: 'recall', reason: 'caliber', recall_run: 'rc_2', workflow_id: 'wf_tier', workflow_name: '客户分层',
      from_version: 2, to_version: 3, binding: [{ field: 'phone', value: null, masked: true }],
      metrics: [{ k: 0, name: '消费额', unit: '元', changed: true, card: 'card', metric: 'spend',
        old: { value: null, text: '已遮罩', masked: true, metric: 'old_0' },
        new: { value: 90, text: '90元', masked: false, metric: 'new_0' },
        diff: { value: null, text: '已遮罩', masked: true, metric: 'diff_0' } }],
      answers: [{ kind: 'answer', kind_text: '问数回答', run_id: 'run_ans_2', version: 2, conversation_id: 'conv_2',
        original: [{ k: 0, value: null, text: null, masked: false, missing: '原答案里没有单独的这个数' }] }],
      actions: [{ decision: 'reissue', available: false,
        reason: '这份口径没有被你能使用的应用固定，不能由你按口径出具，请搭建者出具' }, { decision: 'ack', available: true }] },
  },
  // 已经按新版重新出具过的（处理完的）：出具的那次正式运行搭建者点得开，使用者只写一句（进不了记录页）
  recallIssued: {
    ...base, id: 'recallIssued', kind: 'recall', title: '口径「客户分层」升到 v3，答案的数变了', node_id: null, app_id: null,
    app_name: null, app_version: null, workflow_name: '口径召回：客户分层 v2 → v3', run_id: 'rc_3', status: 'resolved',
    resolved_by: 'zhangsan', resolved_at: ago(60_000), can_respond: false,
    outcome: { decision: 'reissue', note: '', issued_run: 'run_issued_9' },
    assignments: [assignment('rc4', 'zhangsan', 'responded', 'reissue', { source: 'recall' })],
    payload: { kind: 'recall', reason: 'caliber', recall_run: 'rc_3', workflow_id: 'wf_tier', workflow_name: '客户分层',
      from_version: 2, to_version: 3, binding: [], metrics: [], answers: [],
      outcome: { decision: 'reissue', note: '', issued_run: 'run_issued_9' },
      actions: [{ decision: 'reissue', available: false, issued_run: 'run_issued_9' }, { decision: 'ack', available: false }] },
  },
  // 答案依据的目录有变、按新目录检查出了新的错误级问题：只有「知道了」
  recallCatalog: {
    ...base, id: 'recallCatalog', kind: 'recall', title: '答案依据的数据目录有变：按新目录检查出了新的问题', node_id: null,
    app_id: null, app_name: null, app_version: null, workflow_name: '', run_id: 'run_ans_3',
    assignments: [assignment('rc3', '审批人甲', 'pending', null, { source: 'recall' })],
    payload: { kind: 'recall', reason: 'catalog', recall_run: 'run_ans_3',
      basis: { kind: 'catalog', source: 'shop', table: 'orders', new_errors: 1,
        items: [{ path: 'columns.amount.measure', label: '列 amount 的度量类型', change: 'changed' }],
        checks: [{ code: 'stock_summed', message: '对存量列 amount 求和，分组里没有业务日期' }] },
      actions: [{ decision: 'ack', available: true }] },
  },
  // 补充输入里选复核人（选人控件）：使用者按这条待办的候选池选
  pick: {
    ...base, id: 'pick', approval_id: 'ap8', kind: 'input', title: '指定复核人',
    form: { format: 'agentlab.form/1', type: 'object', required: ['reviewer'], properties: {
      reviewer: { type: 'string', title: '复核人', 'x-widget': 'principal' } } },
    payload: { kind: 'human_node', message: '请指定复核人' },
  },
  // 驳回之后去哪（中断内容的 reject_route，服务端 engine/replay.reject_route）：接了驳回出口的写转到哪一步，没接的写运行终止
  routed: {
    ...base, id: 'routed', approval_id: 'ap9', kind: 'approve', title: '主管审批（接了驳回出口）',
    payload: { kind: 'human_node', message: '王五报销 800 元', reject_route: { to: [{ node_id: 'tell', label: '通知申请人' }] } },
  },
  routedInput: {
    ...base, id: 'routedInput', approval_id: 'ap10', kind: 'input', title: '补填用途（接了驳回出口）',
    payload: { kind: 'human_node', message: '请补填用途', reject_route: { to: [{ node_id: 'sub/back', label: '复核子流程 › 退回' }] } },
  },
  stopTool: {
    ...base, id: 'stopTool', approval_id: 'ap11', kind: 'tool', title: '调用工具：erp_query',
    payload: { kind: 'tool_approval', tool: 'erp_query', args: { q: 1 }, reject_route: { ends: 'run' } },
  },
  rejectedDone: {
    ...base, id: 'rejectedDone', approval_id: 'ap12', kind: 'approve', title: '主管审批（已驳回）', status: 'resolved',
    can_respond: false, outcome: { decision: 'reject' }, resolved_by: 'zhangsan', resolved_at: ago(300_000),
    payload: { kind: 'human_node', message: '赵六报销 50 元', reject_route: { to: [{ node_id: 'tell', label: '通知申请人' }] } },
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
  if (state.notify) {
    // 系统通知换成记录器：开了后台提醒、已授权；记下每一条通知（点它就是调它的 onclick），页面一直在后台
    await page.addInitScript(() => {
      localStorage.setItem('agentlab.notify', '1')
      window.__notes = []
      class FakeNotification {
        static permission = 'granted'
        static async requestPermission() { return 'granted' }
        constructor(title, opts) { this.title = title; this.tag = opts?.tag; window.__notes.push(this) }
        close() {}
      }
      window.Notification = FakeNotification
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })
    })
  }
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
    // state.live：派给我的待处理待办由这一段自己换（外壳的待办通知看数和最新一条的时间变没变）
    if (p === '/api/tasks/count' && state.live) {
      return route.fulfill({ json: { mine: state.live.length, apps: 0, other: state.live.length,
        latest: state.live.map((t) => t.created_at).sort().at(-1) ?? null } })
    }
    if (p === '/api/tasks' && state.live) return route.fulfill({ json: { items: state.live } })
    if (p === '/api/approvals' && state.live) return route.fulfill({ json: [] })
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

await section('口径召回：三个值和差值、原答案当时的值、两个动作', async () => {
  const state = { writes: [] }
  const page = await open(state)
  const asked = []
  page.on('request', (r) => { if (r.url().includes('/api/')) asked.push(new URL(r.url()).pathname) })
  await page.goto(`${WEB}/apps/inbox/recall`)
  const box = page.locator('[data-task-recall="caliber"]')
  await box.waitFor({ timeout: 15000 })
  check('写明口径从 v1 升到 v2、同一份数据上两版各算一次', (await box.innerText()).includes('口径「周销售」从 v1 升到了 v2')
    && (await box.innerText()).includes('同一份数据'), (await box.innerText()).slice(0, 80))
  check('这一组的参数', (await box.locator('[data-recall-binding-field="start"]').innerText()) === 'start=2026-09-01')
  const row = box.locator('[data-recall-metric="gmv"]')
  check('旧口径、新口径、两版之差一行，标「变了」', await row.getAttribute('data-changed') === 'true'
    && (await row.locator('[data-recall-cell="old"]').innerText()) === '170.50元'
    && (await row.locator('[data-recall-cell="new"]').innerText()) === '208.50元'
    && (await row.locator('[data-recall-cell="diff"]').innerText()) === '38.00元', (await row.innerText()).replace(/\s+/g, ' '))
  const head = await box.locator('[data-recall-metrics] thead').innerText()
  check('表头写旧口径 v1、新口径 v2、两版之差', head.includes('旧口径 v1') && head.includes('新口径 v2') && head.includes('两版之差'), head)
  check('两个答案各一行，写原答案当时的值', await box.locator('[data-recall-answer]').count() === 2
    && (await box.locator('[data-recall-answer="run_ans_1"] [data-recall-original="0"]').innerText()) === '170.50元'
    && (await box.locator('[data-recall-answer="run_fm_1"] [data-recall-original="0"]').innerText()) === '165.00元')
  check('问数回答链回那段对话', (await box.locator('[data-recall-answer="run_ans_1"] [data-recall-answer-link="chat"]')
    .getAttribute('href')) === '/chat/conv_1')
  check('正式运行（搭建者）链到记录页', (await box.locator('[data-recall-answer="run_fm_1"] [data-recall-answer-link="run"]')
    .getAttribute('href')) === '/runs/run_fm_1')
  const wait = (part) => page.waitForRequest((r) => r.url().includes(part), { timeout: 5000 }).then(() => true, () => false)
  let hit = wait('/api/runs/rc_1/evidence/metrics/recall/diff_0')
  await row.locator('[data-recall-cell="diff"]').click()
  check('点差值：取召回卡上这个指标的出处链', await hit, asked.slice(-3).join(' , '))
  await page.keyboard.press('Escape')
  hit = wait('/api/runs/run_fm_1/evidence/metrics/card/gmv')
  await box.locator('[data-recall-answer="run_fm_1"] [data-recall-original="0"]').click()
  check('点定时任务那次当时的值：取那次正式运行口径卡的指标链', await hit, asked.slice(-3).join(' , '))
  await page.keyboard.press('Escape')
  hit = wait('snap_1')
  await box.locator('[data-recall-answer="run_ans_1"] [data-recall-original="0"]').click()
  check('点问数回答当时的值：打开那条查询快照', await hit, asked.slice(-3).join(' , '))
  await page.keyboard.press('Escape')
  await page.locator('[data-recall-reissue]').click()
  await page.waitForTimeout(300)
  const sent = state.writes.find((w) => w.path === '/api/tasks/recall/respond')
  check('「按新版重新出具」走待办接口，decision 是 reissue', sent?.body?.decision === 'reissue', JSON.stringify(sent?.body))
  check('……提示已按新版发起正式出具', await page.getByText('已按新版发起正式出具').first().waitFor({ timeout: 3000 }).then(() => true, () => false))
  check('不给审批那一套按钮', await page.locator('[data-task-approve], [data-task-reject]').count() === 0)
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')

  const s2 = { writes: [] }
  const p2 = await open(s2)
  await p2.goto(`${WEB}/apps/inbox/recall`)
  await p2.locator('[data-recall-ack]').waitFor({ timeout: 15000 })
  await p2.locator('[data-recall-ack]').click()
  await p2.waitForTimeout(300)
  check('「知道了」decision 是 ack', s2.writes.find((w) => w.path === '/api/tasks/recall/respond')?.body?.decision === 'ack',
    JSON.stringify(s2.writes))
})

await section('口径召回：使用者看到的遮罩值、不能重新出具的原因；目录有变那一种', async () => {
  const state = { writes: [], me: { mode: 'local', username: 'zhangsan', display_name: '张三', role: 'user', groups: [], verified: true } }
  const page = await open(state)
  await page.goto(`${WEB}/apps/inbox/recallUser`)
  const box = page.locator('[data-task-recall="caliber"]')
  await box.waitFor({ timeout: 15000 })
  const row = box.locator('[data-recall-metric="spend"]')
  check('遮罩值写「已遮罩」、点不开', (await row.locator('[data-recall-cell="old"]').innerText()) === '已遮罩'
    && await row.locator('[data-recall-cell="old"]').isDisabled() && (await row.locator('[data-recall-cell="new"]').innerText()) === '90元')
  check('参数里的遮罩值同样遮住', (await box.locator('[data-recall-binding-field="phone"]').innerText()) === 'phone=已遮罩')
  check('原答案里没有单独这个数：写「—」不给链接', (await box.locator('[data-recall-answer="run_ans_2"]').innerText()).includes('—')
    && await box.locator('[data-recall-answer="run_ans_2"] [data-recall-original]').count() === 0)
  check('不能重新出具：按钮停用，写服务端给的原因', await page.locator('[data-recall-reissue]').isDisabled()
    && (await page.locator('[data-recall-reissue-reason]').innerText()).includes('请搭建者出具'))
  check('使用者不给记录页的链接', await box.locator('[data-recall-answer-link="run"]').count() === 0)

  const p3 = await open({ writes: [], me: state.me })
  await p3.goto(`${WEB}/apps/inbox/recallIssued`)
  const done = p3.locator('[data-recall-outcome="reissue"]')
  await done.waitFor({ timeout: 15000 })
  check('使用者重新出具过：只写已出具、正式运行由搭建者在记录页查看，不给点了回不来的记录页链接',
    (await done.innerText()).includes('正式运行的记录由搭建者在记录页查看')
    && await p3.locator('[data-recall-issued]').count() === 0, (await done.innerText()).replace(/\s+/g, ' '))
  const p4 = await open({ writes: [] })
  await p4.goto(`${WEB}/apps/inbox/recallIssued`)
  await p4.locator('[data-recall-outcome="reissue"]').waitFor({ timeout: 15000 })
  check('……搭建者照样链到那次正式运行', (await p4.locator('[data-recall-outcome] [data-recall-issued]').getAttribute('href'))
    === '/runs/run_issued_9' && await p4.locator('[data-recall-issued-note]').count() === 0)

  const p2 = await open({ writes: [] })
  await p2.goto(`${WEB}/apps/inbox/recallCatalog`)
  const basis = p2.locator('[data-recall-basis="catalog"]')
  await basis.waitFor({ timeout: 15000 })
  const text = await basis.innerText()
  check('目录有变：写变了的目录项和新出现的问题', text.includes('列 amount 的度量类型（改了）') && text.includes('新出现的错误级问题 1 条')
    && text.includes('分组里没有业务日期'), text.replace(/\s+/g, ' '))
  check('……只有「知道了」', await p2.locator('[data-recall-ack]').count() === 1 && await p2.locator('[data-recall-reissue]').count() === 0)
  check('没有未捕获的运行时错误', [page, p2, p3, p4].every((x) => x.errors.length === 0),
    [page, p2, p3, p4].flatMap((x) => x.errors)[0] ?? '')
})

await section('口径召回：后台的系统通知点开去待办页（不去记录页）', async () => {
  // recall 待办不属于应用（app_id 为空），挂在召回运行上：记录页里没有这张卡，使用者也进不了记录页。点通知一律去待办页
  const users = {
    builder: null,
    user: { mode: 'local', username: 'zhangsan', display_name: '张三', role: 'user', groups: [], verified: true },
  }
  for (const [who, me] of Object.entries(users)) {
    const state = { writes: [], notify: true, live: [], me: me ?? undefined }
    const page = await open(state)
    await page.goto(`${WEB}/apps`)
    await page.locator('nav[aria-label="主导航"]').waitFor({ timeout: 15000 })
    await page.waitForTimeout(1200)
    state.live = [{ ...TASKS.recall, created_at: new Date().toISOString() }]
    const noted = await page.waitForFunction(() => window.__notes.some((n) => n.tag === 'task:recall'), null, { timeout: 15000 })
      .then(() => true, () => false)
    check(`${who === 'user' ? '使用者' : '搭建者'}：来了 recall 待办，发系统通知`, noted,
      (await page.evaluate(() => window.__notes.map((n) => n.tag))).join(','))
    if (!noted) continue
    await page.evaluate(() => window.__notes.find((n) => n.tag === 'task:recall').onclick())
    const there = await page.waitForURL((u) => u.pathname === '/apps/inbox/recall', { timeout: 8000 }).then(() => true, () => false)
    check('……点通知去这条待办的待办页', there, new URL(page.url()).pathname)
    check('……待办页画出召回卡', await page.locator('[data-task-recall="caliber"]').waitFor({ timeout: 8000 })
      .then(() => true, () => false))
    check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')
  }
})

await section('驳回之后去哪：提示、按钮、驳回后的提示、处理过的结果', async () => {
  const state = { writes: [] }
  const page = await open(state)
  await page.goto(`${WEB}/apps/inbox/routed`)
  await card(page).waitFor({ timeout: 15000 })
  let text = await card(page).innerText()
  check('接了驳回出口的审批：提示写驳回后转到哪一步', text.includes('驳回：转到「通知申请人」'), text.slice(-80))
  check('……不再写「转入「驳回」分支」', !text.includes('转入「驳回」分支'))
  await page.locator('[data-task-reject]').click()
  check('驳回之后提示转到哪一步', await page.getByText('已驳回，转到「通知申请人」').waitFor({ timeout: 3000 }).then(() => true, () => false))
  check('驳回照约定交给待办接口', state.writes.some((w) => w.path === '/api/tasks/routed/respond' && w.body?.decision === 'reject'))

  await page.goto(`${WEB}/apps/inbox/routedInput`)
  await card(page).waitFor({ timeout: 15000 })
  text = await card(page).innerText()
  check('补充输入接了驳回出口：按钮写「驳回」，不写「驳回并终止」', (await page.locator('[data-task-reject]').innerText()).trim() === '驳回')
  check('……提示写子工作流里的那一步', text.includes('驳回：转到「复核子流程 › 退回」'), text.slice(-80))
  check('……不露内部 id', !text.includes('sub/back'))

  await page.goto(`${WEB}/apps/inbox/stopTool`)
  await card(page).waitFor({ timeout: 15000 })
  text = await card(page).innerText()
  check('没接驳回出口的工具审批：按钮写「驳回并终止」', (await page.locator('[data-task-reject]').innerText()).trim() === '驳回并终止')
  check('……提示写本次运行终止', text.includes('驳回：本次运行终止'), text.slice(-80))

  await page.goto(`${WEB}/apps/inbox/rejectedDone`)
  await card(page).waitFor({ timeout: 15000 })
  const resolved = await page.locator('[data-task-resolved]').first().innerText()
  check('处理过的：结果写驳回之后去了哪', resolved.includes('结果：驳回，转到「通知申请人」'), resolved)
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')
})

await section('待办箱：列表和徽标', async () => {
  const page = await open({ writes: [] })
  await page.goto(`${WEB}/apps?tab=inbox`)
  await page.locator('[data-inbox-task]').first().waitFor({ timeout: 15000 })
  check('每条待办一行，链到详情', (await page.locator('[data-inbox-task]').count()) === Object.keys(TASKS).length)
  check('审核个人应用的一行写种类「审核个人应用」', (await page.locator('[data-inbox-task="review"]').innerText()).includes('审核个人应用')
    && (await page.locator('[data-inbox-task="review"]').getAttribute('data-task-kind')) === 'review')
  check('口径召回的一行写种类「口径召回」', (await page.locator('[data-inbox-task="recall"]').innerText()).includes('口径召回')
    && (await page.locator('[data-inbox-task="recall"]').getAttribute('data-task-kind')) === 'recall')
  check('「应用」导航上的徽标数应用里的待办', (await page.locator('[data-apps-badge]').innerText()).trim() === String(Object.keys(TASKS).length))
  check('页签上带数', /\d/.test(await page.getByRole('tab', { name: /待办/ }).innerText()))
})

await browser.close()
console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 全部通过')
process.exit(failed ? 1 : 0)
