import type { RunEvent } from '../../types'

/**
 * 合成的运行事件：fixtures.json 里是真实运行导出的，但有几类事件是新后端才发
 * 的（agent.route.*、带 agent 的 llm.end、node.started 的 iteration/resumed、
 * 签批人、出具 gaps），老库里没有；还有 1800 条的长运行，真实导出放进仓库太大。
 *
 * 形状照后端 emit 的字段逐一对齐（multi.py、compiler.py、io.py、runner.py），
 * 解码器检查（scripts/check-decode.mjs）和离线预览（dev/preview.tsx）共用。
 * 不依赖任何别的模块：检查脚本是把它单独转译后在 node 里 import 的。
 */

const T0 = 1_790_000_000 // 事件 ts 是秒

function builder(runId: string) {
  const out: RunEvent[] = []
  let seq = 0
  let t = T0
  const ev = (type: string, node: string | null, data: Record<string, any> = {}, dt = 0.05): RunEvent => {
    seq += 1
    t += dt
    const e = { seq, type, node_id: node, ts: Number(t.toFixed(3)), data, run_id: runId } as RunEvent
    out.push(e)
    return e
  }
  return { out, ev, at: () => t }
}

/**
 * 三人协作团队：新后端的 agent.route.start/end 与带 round 的 log 同时发；成员
 * 一完成就发自己的 step.end；成员和调度者的 llm.end 没有 llm.start 在前。
 * upto='routing' 停在第 2 轮调度者还在想；upto='members' 停在第 1 轮三人并行中。
 */
export function teamRun(upto: 'routing' | 'members' | 'done' = 'done'): RunEvent[] {
  const { out, ev } = builder('syn-team')
  ev('run.started', null, { nodes: 3, resumed: false, memory_scope: 'default', approval_default: 'dangerous' })
  ev('node.started', 'in', { node_type: 'input', label: '问题' })
  ev('node.finished', 'in', { duration_ms: 1, attempt: 1, preview: { q: '比较三家供应商的交期风险' } })
  ev('node.started', 'team', { node_type: 'supervisor', label: '供应链分析团队' })
  ev('agent.route.start', 'team', { round: 0 })
  ev('llm.end', 'team', { agent: '调度者', model: 'claude-sonnet-4', duration_ms: 2400, input_tokens: 820, output_tokens: 96, cost_usd: 0.0039 }, 2.4)
  ev('agent.route.end', 'team', { round: 0, duration_ms: 2400, agents: ['采购员', '质检员', '物流员'], parallel: 3, done: false, reason: '三方面数据互不依赖，可以同时查' })
  ev('log', 'team', { level: 'info', round: 0, parallel: 3, agents: ['采购员', '质检员', '物流员'], done: false, reason: '三方面数据互不依赖，可以同时查', message: '调度 → 采购员、质检员、物流员（三方面数据互不依赖，可以同时查） · 3 名成员同时执行' })
  for (const [name, task] of [['采购员', '查近 90 天各供应商的交期'], ['质检员', '查来料不良率'], ['物流员', '查在途延误']]) {
    ev('agent.step.start', 'team', { agent: name, instruction: task, round: 0, parallel: 3 }, 0.001)
  }
  if (upto === 'members') {
    // 物流员已经交回，另外两人还在跑
    ev('llm.end', 'team', { agent: '物流员', model: 'claude-sonnet-4', duration_ms: 3100, input_tokens: 640, output_tokens: 210, cost_usd: 0.0051 }, 3.1)
    ev('agent.step.end', 'team', { agent: '物流员', duration_ms: 3100, round: 0, parallel: 3, preview: '在途延误集中在华南线，平均 1.8 天' })
    return out
  }
  ev('llm.end', 'team', { agent: '物流员', model: 'claude-sonnet-4', duration_ms: 3100, input_tokens: 640, output_tokens: 210, cost_usd: 0.0051 }, 3.1)
  ev('agent.step.end', 'team', { agent: '物流员', duration_ms: 3100, round: 0, parallel: 3, preview: '在途延误集中在华南线，平均 1.8 天' })
  ev('llm.end', 'team', { agent: '质检员', model: 'claude-sonnet-4', duration_ms: 4200, input_tokens: 700, output_tokens: 260, cost_usd: 0.006 }, 1.1)
  ev('agent.step.end', 'team', { agent: '质检员', duration_ms: 4200, round: 0, parallel: 3, preview: 'B 供应商来料不良率 2.4%，高于均值' })
  ev('tool.start', 'team', { tool: 'db_query__erp', agent: '采购员', call_id: 'c1', args: { sql: 'SELECT supplier, AVG(lead_days) FROM po_lines WHERE created_at > now() - interval 90 day GROUP BY supplier' } }, 0.2)
  ev('tool.end', 'team', { tool: 'db_query__erp', agent: '采购员', call_id: 'c1', duration_ms: 640, preview: '{"columns":["supplier","avg_lead"],"rows":[["A",12.1],["B",15.4],["C",9.8]],"row_count":3}', artifact: '3aad64e0c3f1b2a9d8e7f6a5b4c3d2e1f0a9b8c7d6e5f4a3b2c1d0e9f8a7b6c5' }, 0.64)
  ev('llm.end', 'team', { agent: '采购员', model: 'claude-sonnet-4', duration_ms: 5600, input_tokens: 910, output_tokens: 300, cost_usd: 0.0072 }, 0.6)
  ev('agent.step.end', 'team', { agent: '采购员', duration_ms: 5600, round: 0, parallel: 3, preview: 'C 交期最短（9.8 天），B 最长（15.4 天）' })
  ev('agent.route.start', 'team', { round: 1 }, 0.05)
  if (upto === 'routing') return out
  ev('llm.end', 'team', { agent: '调度者', model: 'claude-sonnet-4', duration_ms: 1900, input_tokens: 1400, output_tokens: 80, cost_usd: 0.005 }, 1.9)
  ev('agent.route.end', 'team', { round: 1, duration_ms: 1900, agents: [], parallel: 0, done: true, reason: '三方面都有结论，可以收尾' })
  ev('log', 'team', { level: 'info', round: 1, agents: [], done: true, message: '调度 → 结束协作（三方面都有结论，可以收尾）' })
  ev('node.finished', 'team', { duration_ms: 12100, attempt: 1, preview: { text: 'C 综合最优' } })
  ev('node.started', 'out', { node_type: 'output', label: '成果' })
  ev('node.finished', 'out', { duration_ms: 1, attempt: 1, preview: { 结论: 'C 综合最优' } })
  ev('run.finished', null, {
    output: { 结论: 'C 供应商交期最短、来料稳定，建议提高份额。' },
    usage: { input_tokens: 4470, output_tokens: 946, cost_usd: 0.0322 },
    duration_ms: 12300, timing: { wall_ms: 12300, active_ms: 12300, wait_ms: 0 },
  })
  return out
}

/**
 * 逐拍控制的长运行：一个循环节点带着两个节点跑 n 拍，每拍一段 Python，超时那拍
 * 失败后被 on_error=continue 接住；每 10 拍一条「接近上限」的警告。
 * 真实库里 87daca70 就是这个形状（1860 条事件、145 拍）。
 */
export function longLoop(n = 145): RunEvent[] {
  const { out, ev } = builder('syn-long')
  ev('run.started', null, { nodes: 4, resumed: false })
  ev('node.started', 'in', { node_type: 'input', label: '批次参数' })
  ev('node.finished', 'in', { duration_ms: 1, attempt: 1, preview: { batch: 'B-0926' } })
  for (let i = 1; i <= n; i += 1) {
    ev('node.started', 'tick', { node_type: 'loop', label: '逐拍循环', iteration: i })
    ev('edge.taken', 'tick', { branch: 'body', iteration: i - 1, total: n, mode: 'while' })
    ev('node.finished', 'tick', { duration_ms: 1, attempt: 1, preview: { __decision__: 'body' } })
    ev('node.started', 'poll', { node_type: 'code', label: '读取传感器', iteration: i })
    ev('sandbox.start', 'poll', { language: 'python' })
    const ms = i === 37 ? 63 : 9 + ((i * 7) % 6)
    ev('sandbox.end', 'poll', { ok: true, exit_code: 0, duration_ms: ms, stdout: `tick ${i}: 42.${i % 10}℃` }, ms / 1000)
    ev('node.finished', 'poll', { duration_ms: ms + 2, attempt: 1, preview: { temp: 42 + (i % 10) / 10 } })
    if (i % 10 === 0) ev('log', 'poll', { level: 'warn', code: 'loop_limit', message: '循环接近上限，还剩 10 拍' })
  }
  ev('node.started', 'tick', { node_type: 'loop', label: '逐拍循环', iteration: n + 1 })
  ev('edge.taken', 'tick', { branch: 'done', iteration: n, total: n, mode: 'while' })
  ev('node.finished', 'tick', { duration_ms: 1, attempt: 1, preview: { __decision__: 'done' } })
  ev('node.started', 'out', { node_type: 'output', label: '成果' })
  ev('node.finished', 'out', { duration_ms: 1, attempt: 1, preview: { 结果: `${n} 拍全部完成` } })
  ev('run.finished', null, { output: { 结果: `${n} 拍全部完成，最高 42.9℃` }, duration_ms: 5100, usage: {},
                             timing: { wall_ms: 5100, active_ms: 5100, wait_ms: 0 } })
  return out
}

/**
 * 一条什么都有一点的运行：skip_if 跳过的节点、带思考的 agent（模型调用、按表名
 * 汇总的查询、一次看十几张表的结构）、带原始异常的工具报错、签了名的审批、
 * 只有 gaps 的降档出具，最后以某个节点失败收场。
 */
export function mixedRun(): RunEvent[] {
  const { out, ev } = builder('syn-mixed')
  ev('run.started', null, { nodes: 6, resumed: false })
  ev('node.started', 'in', { node_type: 'input', label: '统计区间' })
  ev('node.finished', 'in', { duration_ms: 2, attempt: 1, preview: { day: '2026-09-19' } })
  ev('node.skipped', 'kb', { reason: '满足跳过条件：{{ inputs.day }} 是单日，不需要背景检索', node_type: 'retrieve', label: '背景检索' })
  ev('node.started', 'agent', { node_type: 'agent', label: '出勤分析' })
  ev('llm.start', 'agent', { model: 'claude-sonnet-4', message_count: 2 })
  ev('llm.thinking', 'agent', { text: '先看一下考勤相关的表有哪些，再按班次汇总出勤率。' }, 1.2)
  ev('llm.end', 'agent', { agent: '出勤分析', model: 'claude-sonnet-4', duration_ms: 1400, input_tokens: 1200, output_tokens: 180, total_tokens: 1380, cost_usd: 0.0063, calls: 1 }, 0.2)
  ev('tool.start', 'agent', { tool: 'db_schema__warehouse', call_id: 's1', args: { table: 'hr_attendance,hr_shift,hr_employee,hr_department,hr_leave,hr_overtime,hr_holiday,hr_roster,hr_badge_log,hr_position,hr_contract,hr_site' } })
  ev('tool.end', 'agent', { tool: 'db_schema__warehouse', call_id: 's1', duration_ms: 210, preview: '12 张表的字段…' }, 0.21)
  ev('llm.start', 'agent', { model: 'claude-sonnet-4', message_count: 4 })
  ev('llm.end', 'agent', { agent: '出勤分析', model: 'claude-sonnet-4', duration_ms: 900, input_tokens: 2100, output_tokens: 120, total_tokens: 2220, cost_usd: 0.008, calls: 1 }, 0.9)
  ev('tool.start', 'agent', { tool: 'db_query__warehouse', call_id: 'q1', args: { sql: 'SELECT s.shift_name, COUNT(*) AS n, AVG(a.present) AS rate\nFROM v_device_kpi a JOIN hr_shift s ON a.shift_id = s.id\nWHERE a.day = \'2026-09-19\'\nGROUP BY s.shift_name\nORDER BY rate DESC' } })
  ev('tool.end', 'agent', { tool: 'db_query__warehouse', call_id: 'q1', duration_ms: 9500, preview: '{"columns":["shift_name","n","rate"],"rows":[["早班",52,0.9423],["晚班",26,0.7308]],"row_count":2}', artifact: '4790dbfc11aa22bb33cc44dd55ee66ff77889900aabbccddeeff001122334455' }, 9.5)
  ev('tool.start', 'agent', { tool: 'db_query__warehouse', call_id: 'q2', args: { sql: 'SELECT * FROM hr_overtime_detail WHERE day = \'2026-09-19\'' } })
  ev('tool.error', 'agent', { tool: 'db_query__warehouse', call_id: 'q2', error: '查询超时：数据库 90 秒没有返回', detail: 'sqlalchemy.exc.OperationalError: (pymysql.err.OperationalError) (3024, \'Query execution was interrupted, maximum statement execution time exceeded\')' }, 90.6)
  ev('node.finished', 'agent', { duration_ms: 103000, attempt: 1, preview: { text: '早班出勤率 94.23%，晚班 73.08%。' } })
  ev('node.started', 'review', { node_type: 'human', label: '班长复核' })
  ev('human.requested', 'review', { kind: 'human_node', node_id: 'review', mode: 'approve', title: '出勤结论可以发出吗？', message: '早班 94.23%，晚班 73.08%' })
  ev('run.interrupted', 'review', { payload: { kind: 'human_node', node_id: 'review', mode: 'approve', title: '出勤结论可以发出吗？' } })
  ev('run.resumed', null, { response: { approved: true, note: '晚班偏低要跟进' }, actor: '张工' }, 180)
  ev('run.started', null, { nodes: 6, resumed: true })
  ev('node.started', 'review', { node_type: 'human', label: '班长复核', resumed: true })
  ev('human.requested', 'review', { kind: 'human_node', node_id: 'review', mode: 'approve', title: '出勤结论可以发出吗？' })
  ev('human.resolved', 'review', { response: { approved: true, note: '晚班偏低要跟进' }, actor: '张工' })
  ev('node.finished', 'review', { duration_ms: 0, attempt: 1, preview: { approved: true } })
  ev('node.started', 'report', { node_type: 'output', label: '日报出具' })
  ev('issuance', 'report', { tier: 'degraded', missing_required: [], missing_expected: [], unmatched: 0, calibers: [{ node: 'kpi', caliber: '出勤率口径', version: 'v3' }], gaps: ['叙述模板渲染为空（路径可能有误）'], metrics_checked: 2, matched_numbers: 0 })
  ev('node.finished', 'report', { duration_ms: 3, attempt: 1, preview: {} })
  ev('node.started', 'notify', { node_type: 'tool', label: '推送通知' })
  ev('node.failed', 'notify', { error: '推送失败：通知机器人地址为空', duration_ms: 120, detail: 'httpx.ConnectError: [Errno 8] nodename nor servname provided, or not known' })
  ev('run.failed', null, { error: '推送失败：通知机器人地址为空', node_id: 'notify', label: '推送通知', detail: 'httpx.ConnectError: [Errno 8] nodename nor servname provided, or not known', timing: { wall_ms: 290000, active_ms: 110000, wait_ms: 180000 } })
  return out
}

/** mixedRun 的出具成果：只有 gaps 的降档，外加一个无法回指的数字 */
export const MIXED_OUTPUT = {
  answer: '9 月 19 日早班出勤率 **94.23%**，晚班 **73.08%**，晚班比上周低 6 个百分点。',
  _issuance: {
    tier: 'degraded',
    calibers: [{ node: 'kpi', caliber: '出勤率口径', version: 'v3' }],
    metrics_checked: 2,
    missing_required: [],
    missing_expected: [],
    unmatched_numbers: [{ token: '6', context: '晚班比上周低 6 个百分点' }],
    matched_numbers: 2,
    gaps: ['叙述模板渲染为空（路径可能有误）'],
  },
}

/**
 * 两张口径卡的出具：逐个数字的出处在 matched[].caliber 里，写成「口径名 @ 版本」
 * （后端 io.py 的拼法）。横幅按它逐张清点「回指 N 个数字」——拼法一变，这一支就悄悄不画了
 */
export const CALIBERS_OUTPUT = {
  answer: '上周订单 **128** 单，已付款 **96** 单，退款 **7** 单。',
  _issuance: {
    tier: 'formal',
    calibers: [
      { node: 'k1', caliber: '订单口径', version: 'v2' },
      { node: 'k2', caliber: '退款口径', version: 'v1' },
    ],
    metrics_checked: 3,
    missing_required: [],
    missing_expected: [],
    unmatched_numbers: [],
    matched_numbers: 3,
    matched: [
      { token: '128', metric: 'orders', caliber: '订单口径 @ v2' },
      { token: '96', metric: 'paid', caliber: '订单口径 @ v2' },
      { token: '7', metric: 'refunds', caliber: '退款口径 @ v1' },
    ],
    gaps: [],
  },
}

/** 只有一张口径卡、逐个出处都指向它：直说「都来自这张卡」 */
export const CALIBER_ONE_OUTPUT = {
  answer: '上周订单 **128** 单，已付款 **96** 单。',
  _issuance: {
    tier: 'formal',
    calibers: [{ node: 'k1', caliber: '订单口径', version: 'v2' }],
    metrics_checked: 2,
    missing_required: [],
    missing_expected: [],
    unmatched_numbers: [],
    matched_numbers: 2,
    matched: [
      { token: '128', metric: 'orders', caliber: '订单口径 @ v2' },
      { token: '96', metric: 'paid', caliber: '订单口径 @ v2' },
    ],
    gaps: [],
  },
}

/**
 * 一条一条往下走的取数流水线：n 个不同的节点，各查一张表。不折叠、不合并，
 * 行数随事件线性增长——用来验"只在贴底时跟随"
 */
export function pipelineRun(n = 30): RunEvent[] {
  const { out, ev } = builder('syn-pipe')
  ev('run.started', null, { nodes: n, resumed: false })
  for (let i = 1; i <= n; i += 1) {
    const id = `p${i}`
    ev('node.started', id, { node_type: 'tool', label: `第 ${i} 张报表` })
    ev('tool.start', id, { tool: 'db_query__warehouse', call_id: `c${i}`, args: { sql: `SELECT line, SUM(qty) FROM report_${i} GROUP BY line` } })
    ev('tool.end', id, { tool: 'db_query__warehouse', call_id: `c${i}`, duration_ms: 120 + i * 7, preview: `{"columns":["line","qty"],"rows":[["L1",${i}]],"row_count":1}` }, 0.12)
    ev('node.finished', id, { duration_ms: 130 + i * 7, attempt: 1, preview: { rows: 1 } })
  }
  return out
}

/** 取消在半路的运行：节点下面的查询还没回来 */
export function cancelledRun(): RunEvent[] {
  const { out, ev } = builder('syn-cancel')
  ev('run.started', null, { nodes: 3, resumed: false })
  ev('node.started', 'q', { node_type: 'tool', label: '全量取数' })
  ev('tool.start', 'q', { tool: 'db_query__warehouse', args: { sql: 'SELECT * FROM big_table' } })
  ev('run.cancelled', null, { timing: { wall_ms: 4200, active_ms: 4200, wait_ms: 0 } }, 4.2)
  return out
}

// ---------------------------------------------------------------------------
// 平台问题的几种结局（NI-3/4/5）：模型把工具调用写成文字、团队用完轮数、校验修复
// 想凑数、工具超时、放弃等审批的运行。文案逐字照后端（toolcalls.py、multi.py、
// human.py、runner.py），名字全是编的通用示例
// ---------------------------------------------------------------------------

const MARKUP = '<｜｜DSML｜｜invoke name="db_query__shop">'
const MEMBER_MARKUP_FAILED = '模型以文本形式输出了工具调用的原始标记，未实际调用工具，这一步没有查询到任何数据'
  + '（常见原因：该成员未绑定工具，或模型、服务不支持工具调用）'
export const TOOL_MARKUP_ERROR = '模型以文本形式输出了工具调用的原始标记，未实际调用工具，这一步没有查询到任何数据。'
  + '常见原因：节点未绑定工具，或模型、服务不支持工具调用。'
  + '请在画布中为该节点绑定所需工具；如已绑定仍出现此问题，请换用支持工具调用的模型'

/**
 * 两轮就用完的协作团队：取数员每轮都把工具调用写成文字（成员失败），汇总员一次都没派到。
 * mode：fail 默认的判失败；degrade 降档交付；judged 最后那次判定说完成了；closing 停在
 * 最后那次判定还没出结果。upto='closing' 用来看「调度者在做最后判定」那一行
 */
export function exhaustedTeam(mode: 'fail' | 'degrade' | 'judged' | 'closing' = 'fail'): RunEvent[] {
  const { out, ev } = builder(`syn-exhausted-${mode}`)
  const reason = '还没有查到任何订单数据'
  ev('run.started', null, { nodes: 3, resumed: false, replay_protocol: 2 })
  ev('node.started', 'in', { node_type: 'input', label: '问题' })
  ev('node.finished', 'in', { duration_ms: 1, attempt: 1, preview: { q: '上月各区域订单额' } })
  ev('node.started', 'team', { node_type: 'supervisor', label: '销售分析团队' })
  const member = (round: number, parallel: number, name: string, task: string, ok: boolean) => {
    ev('agent.step.start', 'team', { agent: name, instruction: task, round, parallel }, 0.001)
    if (!ok) {
      ev('llm.end', 'team', { agent: name, model: 'demo-chat', duration_ms: 1800, input_tokens: 500, output_tokens: 90, cost_usd: 0.001 }, 1.8)
      ev('log', 'team', { level: 'warn', code: 'tool_markup_leak',
        message: `${name}以文本形式输出了工具调用（${MARKUP}…），未实际调用工具，已要求模型重试一次` })
      ev('llm.end', 'team', { agent: name, model: 'demo-chat', duration_ms: 1500, input_tokens: 620, output_tokens: 80, cost_usd: 0.001 }, 1.5)
      ev('agent.step.end', 'team', { agent: name, duration_ms: 3300, round, parallel,
        preview: `（${name} 这一步执行失败：${MEMBER_MARKUP_FAILED}）`, failed: true, error: MEMBER_MARKUP_FAILED })
    } else {
      ev('llm.end', 'team', { agent: name, model: 'demo-chat', duration_ms: 2100, input_tokens: 480, output_tokens: 150, cost_usd: 0.001 }, 2.1)
      ev('agent.step.end', 'team', { agent: name, duration_ms: 2100, round, parallel, preview: '没有数据可分析，等取数员交回订单明细' })
    }
  }
  ev('agent.route.start', 'team', { round: 0 })
  ev('llm.end', 'team', { agent: '调度者', model: 'demo-chat', duration_ms: 1200, input_tokens: 400, output_tokens: 60, cost_usd: 0.0008 }, 1.2)
  ev('agent.route.end', 'team', { round: 0, duration_ms: 1200, agents: ['取数员'], parallel: 1, done: false, reason: '先把订单明细查出来' })
  ev('log', 'team', { level: 'info', round: 0, parallel: 1, agents: ['取数员'], done: false, reason: '先把订单明细查出来', message: '调度 → 取数员（先把订单明细查出来）' })
  member(0, 1, '取数员', '查上月 orders 表按区域汇总的订单额', false)
  ev('agent.route.start', 'team', { round: 1 })
  ev('llm.end', 'team', { agent: '调度者', model: 'demo-chat', duration_ms: 1100, input_tokens: 700, output_tokens: 60, cost_usd: 0.0009 }, 1.1)
  ev('agent.route.end', 'team', { round: 1, duration_ms: 1100, agents: ['取数员', '分析员'], parallel: 2, done: false, reason: '取数员再试一次，分析员先准备口径' })
  ev('log', 'team', { level: 'info', round: 1, parallel: 2, agents: ['取数员', '分析员'], done: false, reason: '取数员再试一次，分析员先准备口径', message: '调度 → 取数员、分析员（取数员再试一次，分析员先准备口径） · 2 名成员同时执行' })
  member(1, 2, '分析员', '准备各区域订单额的对比口径', true)
  member(1, 2, '取数员', '再查一次上月 orders 表', false)
  // 两轮都派过了、调度者一次都没说完成：补一次只判定、不派活的决定
  ev('agent.route.start', 'team', { round: 2, closing: true })
  if (mode === 'closing') return out
  const judged = mode === 'judged'
  ev('llm.end', 'team', { agent: '调度者', model: 'demo-chat', duration_ms: 900, input_tokens: 900, output_tokens: 50, cost_usd: 0.001 }, 0.9)
  ev('agent.route.end', 'team', { round: 2, duration_ms: 900, agents: [], parallel: 0, done: judged,
    reason: judged ? '分析员给出了口径，可以收尾' : reason, closing: true })
  if (judged) {
    ev('log', 'team', { level: 'info', round: 2, agents: [], done: true, closing: true, message: '调度 → 结束协作（分析员给出了口径，可以收尾）' })
    ev('node.finished', 'team', { duration_ms: 9800, attempt: 1, preview: { text: '口径已备好', rounds: 2 } })
    ev('run.finished', null, { output: { 结论: '口径已备好' }, usage: { input_tokens: 4600, output_tokens: 620, cost_usd: 0.008 },
      duration_ms: 10000, timing: { wall_ms: 10000, active_ms: 10000, wait_ms: 0 } })
    return out
  }
  const summary = `协作团队用完 2 轮仍未完成：${reason}。一次都没被派到的成员：汇总员`
  if (mode === 'fail') {
    const error = `${summary}。先看成员是否绑定了所需工具，再调大「最多轮数」；也可将「用完轮数时」改为「降档交付」`
    ev('node.failed', 'team', { error, duration_ms: 9800, detail: null })
    ev('run.failed', null, { error, node_id: 'team', label: '销售分析团队', timing: { wall_ms: 10000, active_ms: 10000, wait_ms: 0 } })
    return out
  }
  ev('log', 'team', { level: 'warn', code: 'team_exhausted', message: `${summary}。按降档交付：成果取自成员最后的回复，并非调度者认可的结论` })
  ev('node.finished', 'team', { duration_ms: 9800, attempt: 1, preview: {
    text: `（取数员 这一步执行失败：${MEMBER_MARKUP_FAILED}）`, rounds: 2, exhausted: true, exhausted_reason: reason, never_dispatched: '[1 项]' } })
  ev('node.started', 'out', { node_type: 'output', label: '成果' })
  ev('issuance', 'out', { tier: 'degraded', missing_required: [], missing_expected: [], unmatched: 0, calibers: [],
    gaps: ['协作团队「销售分析团队」用完 2 轮仍未完成，交付的是成员最后的回复'], metrics_checked: 0, matched_numbers: 0, matched: [] })
  ev('node.finished', 'out', { duration_ms: 2, attempt: 1, preview: {} })
  ev('run.finished', null, { output: { 结论: `（取数员 这一步执行失败：${MEMBER_MARKUP_FAILED}）` },
    usage: { input_tokens: 4600, output_tokens: 620, cost_usd: 0.008 }, duration_ms: 10000, timing: { wall_ms: 10000, active_ms: 10000, wait_ms: 0 } })
  return out
}

/** 没绑工具的 agent：把工具调用写成了文字，纠正一次还这样，判失败 */
export function markupRun(): RunEvent[] {
  const { out, ev } = builder('syn-markup')
  ev('run.started', null, { nodes: 3, resumed: false })
  ev('node.started', 'in', { node_type: 'input', label: '问题' })
  ev('node.finished', 'in', { duration_ms: 1, attempt: 1, preview: { q: '上月订单额' } })
  ev('node.started', 'query', { node_type: 'agent', label: '数据查询' })
  ev('llm.start', 'query', { model: 'demo-chat', message_count: 2 })
  ev('llm.end', 'query', { agent: '数据查询', model: 'demo-chat', duration_ms: 2200, input_tokens: 900, output_tokens: 140, total_tokens: 1040, cost_usd: 0.002, calls: 1 }, 2.2)
  ev('log', 'query', { level: 'warn', code: 'tool_markup_leak',
    message: `模型以文本形式输出了工具调用（${MARKUP}…），未实际调用工具，已要求模型重试一次` })
  ev('llm.start', 'query', { model: 'demo-chat', message_count: 4 })
  ev('llm.end', 'query', { agent: '数据查询', model: 'demo-chat', duration_ms: 1900, input_tokens: 1100, output_tokens: 120, total_tokens: 1220, cost_usd: 0.002, calls: 1 }, 1.9)
  ev('node.failed', 'query', { error: TOOL_MARKUP_ERROR, duration_ms: 4200, detail: null })
  ev('run.failed', null, { error: TOOL_MARKUP_ERROR, node_id: 'query', label: '数据查询', timing: { wall_ms: 4300, active_ms: 4300, wait_ms: 0 } })
  return out
}

/**
 * 合并查询：门店库、会员库各查一次，在库外按门店合并（engine/nodes/merge.py 的 merge.end 和随后的警告 log）。
 * 门店编号一边是文本、一边是数，又只按门店没按日期合并：两道警告都发
 */
export const MERGE_SQL = 'SELECT s.门店, s.订单数, v.到店人数 FROM s JOIN v ON s.门店 = v.门店'
export function mergeRun(): RunEvent[] {
  const { out, ev } = builder('syn-merge')
  const query = (node: string, label: string, source: string, sql: string, columns: string[], rows: unknown[][]) => {
    ev('node.started', node, { node_type: 'tool', label })
    ev('tool.start', node, { tool: `db_query__${source}`, args: { sql } })
    ev('tool.end', node, { tool: `db_query__${source}`, duration_ms: 40, artifact: `ts-${node}`, query_artifact: `qs-${node}`,
      preview: JSON.stringify({ columns, rows, row_count: rows.length, truncated: false, sql, source }) }, 0.04)
    ev('node.finished', node, { duration_ms: 45, attempt: 1 })
  }
  ev('run.started', null, { nodes: 4, resumed: false })
  query('q_sales', '门店销售', 'stores', 'SELECT order_date AS 日期, substr(store_id, 2) AS 门店, COUNT(*) AS 订单数 FROM orders GROUP BY 1, 2',
    ['日期', '门店', '订单数'], [['2026-05-01', '01', 3], ['2026-05-01', '02', 2], ['2026-05-02', '01', 2], ['2026-05-02', '02', 1]])
  query('q_visits', '到店人数', 'members', 'SELECT visit_date AS 日期, CAST(substr(store_code, 2) AS INTEGER) AS 门店, COUNT(*) AS 到店人数 FROM visits GROUP BY 1, 2',
    ['日期', '门店', '到店人数'], [['2026-05-01', 1, 6], ['2026-05-01', 2, 4], ['2026-05-02', 1, 5], ['2026-05-02', 2, 4]])
  ev('node.started', 'merge', { node_type: 'merge', label: '按门店合并' })
  ev('merge.end', 'merge', {
    inputs: [
      { alias: 's', node_id: 'q_sales', label: '门店销售', rows: 4, source: 'stores', artifact: 'qs-q_sales' },
      { alias: 'v', node_id: 'q_visits', label: '到店人数', rows: 4, source: 'members', artifact: 'qs-q_visits' },
    ],
    sql: MERGE_SQL, rows: 8, columns: ['门店', '订单数', '到店人数'],
    preview_rows: [['01', 3, 6], ['01', 3, 5], ['01', 2, 6], ['01', 2, 5], ['02', 2, 4]],
    truncated: false, duration_ms: 12, query_artifact: 'mq-merge', lineage: true,
    warnings: [{ code: 'key_type_mismatch', message: '键类型' }, { code: 'rows_grew', message: '行数' }],
  }, 0.02)
  ev('log', 'merge', { level: 'warn', code: 'merge_key_type',
    message: "合并键类型不一致：s.门店 是文本（例如 '01'），v.门店 是数值（例如 1）。SQLite 比较时会做隐式转换：文本形式的编号（如 '001'、'01'）都会等于数值 1，也可能完全匹配不上。请在源查询中统一类型，或在合并 SQL 中用 CAST 明确转换" })
  ev('log', 'merge', { level: 'warn', code: 'merge_rows_grew',
    message: '合并结果有 8 行，多于行数最多的输入「s」（4 行）：合并键可能不唯一，同一行被重复匹配。「s」中按（门店）有重复的键，例如 01 出现 2 次等 2 组；「v」中按（门店）有重复的键，例如 1 出现 2 次等 2 组。请检查合并条件是否覆盖了全部键（例如同时按日期和门店），或先在源库里聚合到相同粒度' })
  ev('node.finished', 'merge', { duration_ms: 15, attempt: 1 })
  ev('run.finished', null, { output: {}, timing: { wall_ms: 400, active_ms: 400, wait_ms: 0 } })
  return out
}

/** 结构化校验：原文里没有数，修复两次都编出了 total_count=0，两次都作废，判失败 */
export function repairRun(): RunEvent[] {
  const { out, ev } = builder('syn-repair')
  ev('run.started', null, { nodes: 3, resumed: false })
  ev('node.started', 'query', { node_type: 'agent', label: '数据查询' })
  ev('node.finished', 'query', { duration_ms: 2400, attempt: 1, preview: { text: '假设调用工具：SELECT region, SUM(amount) FROM orders GROUP BY region' } }, 2.4)
  ev('node.started', 'check', { node_type: 'validate', label: '结构校验' })
  ev('log', 'check', { level: 'warn', code: 'validate_retry', message: '第 1 次校验失败：输出不是合法 JSON' })
  ev('llm.end', 'check', { model: 'demo-chat', purpose: 'repair', duration_ms: 1300, input_tokens: 600, output_tokens: 40, total_tokens: 640, cost_usd: 0.001, calls: 1 }, 1.3)
  ev('log', 'check', { level: 'warn', code: 'repair_invented', message: '第 1 次修复作废：修复时出现了原文没有的值：total_count=0' })
  ev('llm.end', 'check', { model: 'demo-chat', purpose: 'repair', duration_ms: 1200, input_tokens: 600, output_tokens: 40, total_tokens: 640, cost_usd: 0.001, calls: 1 }, 1.2)
  ev('log', 'check', { level: 'warn', code: 'repair_invented', message: '第 2 次修复作废：修复时出现了原文没有的值：total_count=0' })
  const error = '结构化校验未通过：修复时出现了原文没有的值：total_count=0'
  ev('node.failed', 'check', { error, duration_ms: 2600, detail: null })
  ev('run.failed', null, { error, node_id: 'check', label: '结构校验', timing: { wall_ms: 5200, active_ms: 5200, wait_ms: 0 } })
  return out
}

/** 查询带着 30 秒时限：upto='live' 停在还没回来（已经超时），否则到点被放弃 */
export function timeoutRun(upto: 'live' | 'done' = 'done'): RunEvent[] {
  const { out, ev } = builder('syn-timeout')
  ev('run.started', null, { nodes: 2, resumed: false })
  ev('node.started', 'query', { node_type: 'tool', label: '取订单明细' })
  ev('tool.start', 'query', { tool: 'db_query__shop', timeout_s: 30, args: { sql: 'SELECT * FROM orders' } })
  if (upto === 'live') return out
  ev('tool.error', 'query', { tool: 'db_query__shop', timed_out: true, duration_ms: 31000,
    error: '查询超过 30 秒没有返回，已停止等待（数据库可能仍在执行，连接会在后台回收）。请添加 WHERE 条件或 LIMIT 缩小范围后重试' }, 31)
  ev('node.failed', 'query', { error: '查询超过 30 秒没有返回，已停止等待（数据库可能仍在执行，连接会在后台回收）。请添加 WHERE 条件或 LIMIT 缩小范围后重试。数据量较大时请在参数中缩小范围；如为服务暂时无响应，可稍后点「继续运行」', duration_ms: 31000 })
  return out
}

/** 停在审批上被放弃：run.cancelled 带操作人和一句说明 */
export function abandonedRun(): RunEvent[] {
  const { out, ev } = builder('syn-abandoned')
  ev('run.started', null, { nodes: 3, resumed: false })
  ev('node.started', 'review', { node_type: 'human', label: '主管审批' })
  ev('human.requested', 'review', { kind: 'human_node', node_id: 'review', mode: 'approve', title: '这份报表可以发出吗？' })
  ev('run.interrupted', 'review', { payload: { node_id: 'review', title: '这份报表可以发出吗？' } })
  ev('run.cancelled', null, { timing: { wall_ms: 60000, active_ms: 400, wait_ms: 59600 }, actor: '张工',
    message: '放弃了这次运行，1 条待审批一并关闭' }, 60)
  return out
}

/** 改图时模型漏写了 tools：自查带回 tools_dropped 警告，final 带回工具绑定变化 */
export const COPILOT_TOOLS_DROPPED = [
  { op: 'update_node', id: 'query', label: '数据查询', config: { system: '只查上月的数据' } },
  { op: 'done', explanation: '改了提示词' },
  { op: 'check', status: 'passed', repaired: 0, warnings: [
    { level: 'warning', node_id: 'query', edge_id: null, code: 'tools_dropped', field: 'tools',
      message: '「数据查询」的工具从 db_query__shop、db_schema__shop 变为无。本轮要求中没有提到移除工具，请确认是否误删：没有绑定工具时，它无法查询数据库，只能假设调用结果' },
  ] },
  { op: 'final', graph: { nodes: [{ id: 'in' }, { id: 'query' }, { id: 'out' }] }, tool_changes: [
    { node_id: 'query', label: '数据查询', member: null, field: 'tools',
      before: ['db_query__shop', 'db_schema__shop'], after: [], added: [], removed: ['db_query__shop', 'db_schema__shop'] },
  ], issues: [
    { level: 'warning', node_id: 'query', code: 'tools_dropped', field: 'tools',
      message: '「数据查询」的工具从 db_query__shop、db_schema__shop 变为无。本轮要求中没有提到移除工具，请确认是否误删：没有绑定工具时，它无法查询数据库，只能假设调用结果' },
  ] },
]

/** Copilot 操作流：思考被心跳打断、自查修一轮没修好、模型写了不存在的节点类型 */
export const COPILOT_STUCK = [
  { op: 'heartbeat', phase: 'planning', elapsed_ms: 3000 },
  { op: 'thinking', delta: '先看一下有哪些表。' },
  { op: 'heartbeat', phase: 'planning', elapsed_ms: 6000 },
  { op: 'thinking', delta: '然后按班次聚合。' },
  { op: 'heartbeat', phase: 'planning', elapsed_ms: 9000 },
  { op: 'thinking', delta: '最后出一份日报。' },
  { op: 'plan', summary: '取数 → 汇总 → 出具' },
  { op: 'add_node', node: { id: 'q', type: 'tool', label: '取数' } },
  { op: 'add_node', node: { id: 'lp', type: 'loop', label: '逐日循环' } },
  { op: 'done', explanation: '三步走' },
  { op: 'check', status: 'repairing', round: 1, issues: ['「lp」循环条件有误：表达式中不支持 | 过滤器'] },
  { op: 'heartbeat', phase: 'repairing', elapsed_ms: 12000 },
  { op: 'update_node', id: 'lp' },
  { op: 'check', status: 'failed', issues: ['「lp」循环条件有误：表达式中不支持 | 过滤器', '「q」没有选数据源'] },
  { op: 'final', graph: { nodes: [{ id: 'q' }, { id: 'lp' }] }, issues: [
    { level: 'error', node_id: 'lp', message: '循环条件有误' },
    { level: 'warning', node_id: null, code: 'unknown_node_type', type: 'excel_export', message: '模型使用了不存在的节点类型「excel_export」，已跳过这一步' },
  ] },
]

/**
 * 同一个协作团队执行了不止一次，看结局、泳道是不是按「这一次」算。
 * - rerun：用完 2 轮判失败 → 调大轮数接着跑（后端给这个节点也标 resumed:true）→ 一轮就收尾；
 * - loop：循环体里的团队，第 1 轮降档交付（产出不带轮数，后端就是这样发的），第 2 轮正常收尾；
 * - approval：成员要调的工具要审批，停下、放行后同一次执行接着走（这次重放不算新的一次）。
 */
export function rerunTeam(mode: 'rerun' | 'loop' | 'approval'): RunEvent[] {
  const { out, ev } = builder(`syn-team-${mode}`)
  const route = (round: number, agents: string[], done: boolean, reason: string) => {
    ev('agent.route.start', 'team', { round })
    ev('agent.route.end', 'team', { round, duration_ms: 800, agents, parallel: agents.length, done, reason })
  }
  const member = (round: number, name: string, preview: string) => {
    ev('agent.step.start', 'team', { agent: name, instruction: `第 ${round + 1} 轮的活`, round, parallel: 1 }, 0.001)
    ev('agent.step.end', 'team', { agent: name, duration_ms: 1200, round, parallel: 1, preview }, 1.2)
  }
  ev('run.started', null, { nodes: 3, resumed: false, replay_protocol: 2 })
  if (mode === 'rerun') {
    ev('node.started', 'team', { node_type: 'supervisor', label: '复盘小组' })
    route(0, ['检索员'], false, '先把订单查出来')
    member(0, '检索员', '没查到')
    route(1, ['检索员'], false, '再试一次')
    member(1, '检索员', '还是没查到')
    const error = '协作团队用完 2 轮仍未完成：还没有查到订单。一次都没被派到的成员：定稿员。先看成员是否绑定了所需工具，再调大「最多轮数」；也可将「用完轮数时」改为「降档交付」'
    ev('node.failed', 'team', { error, duration_ms: 4000 })
    ev('run.failed', null, { error, node_id: 'team', label: '复盘小组', timing: { wall_ms: 4200, active_ms: 4200, wait_ms: 0 } })
    // 真实的接着跑顺序：run.resumed → run.started{resumed} → node.started{resumed:true}
    ev('run.resumed', null, { from: 'team', message: '从「复盘小组」继续运行', actor: null }, 5)
    ev('run.started', null, { nodes: 3, resumed: true, replay_protocol: 2 })
    ev('node.started', 'team', { node_type: 'supervisor', label: '复盘小组', resumed: true })
    route(0, ['检索员'], false, '查订单')
    member(0, '检索员', '查到 128 单')
    route(1, [], true, '查到了，可以收尾')
    ev('node.finished', 'team', { duration_ms: 2500, attempt: 1, preview: { text: '128 单' } })
    ev('run.finished', null, { output: { 结论: '128 单' }, usage: {}, duration_ms: 7000, timing: { wall_ms: 12000, active_ms: 7000, wait_ms: 0 } })
    return out
  }
  if (mode === 'loop') {
    ev('node.started', 'lp', { node_type: 'loop', label: '逐周循环' })
    for (const iteration of [1, 2]) {
      ev('node.started', 'team', { node_type: 'supervisor', label: '复盘小组', iteration })
      route(0, ['分析员'], false, `第 ${iteration} 周`)
      member(0, '分析员', `第 ${iteration} 周的原话`)
      if (iteration === 1) {
        route(1, ['分析员'], false, '再看看')
        member(1, '分析员', '还是原话')
        ev('agent.route.start', 'team', { round: 2, closing: true })
        ev('agent.route.end', 'team', { round: 2, duration_ms: 600, agents: [], parallel: 0, done: false, reason: '数据不全', closing: true })
        ev('log', 'team', { level: 'warn', code: 'team_exhausted',
          message: '协作团队用完 2 轮仍未完成：数据不全。按降档交付：成果取自成员最后的回复，并非调度者认可的结论' })
        ev('node.finished', 'team', { duration_ms: 3000, attempt: 1,
          preview: { text: '还是原话', exhausted: true, exhausted_reason: '数据不全', never_dispatched: '[0 项]' } })
      } else {
        route(1, [], true, '这周齐了')
        ev('node.finished', 'team', { duration_ms: 1500, attempt: 1, preview: { text: '第 2 周的原话' } })
      }
      ev('edge.taken', 'lp', { branch: iteration === 1 ? 'body' : 'done' })
    }
    ev('node.finished', 'lp', { duration_ms: 5000, attempt: 1, preview: {} })
    ev('run.finished', null, { output: { 结论: '两周都看完了' }, usage: {}, duration_ms: 5200, timing: { wall_ms: 5200, active_ms: 5200, wait_ms: 0 } })
    return out
  }
  ev('node.started', 'team', { node_type: 'supervisor', label: '复盘小组' })
  route(0, ['检索员'], false, '先查订单')
  ev('agent.step.start', 'team', { agent: '检索员', instruction: '改一条订单备注', round: 0, parallel: 1 }, 0.001)
  ev('human.requested', 'team', { node_id: 'team', kind: 'tool', tool: 'db_query__shop' })
  ev('run.interrupted', null, { payload: { node_id: 'team' } })
  ev('run.resumed', null, { actor: null }, 3)
  ev('node.started', 'team', { node_type: 'supervisor', label: '复盘小组', resumed: true })
  ev('human.resolved', 'team', { approved: true, actor: null })
  ev('agent.step.end', 'team', { agent: '检索员', duration_ms: 3500, round: 0, parallel: 1, preview: '改好了' }, 0.5)
  route(1, [], true, '改好了，可以收尾')
  ev('node.finished', 'team', { duration_ms: 4200, attempt: 1, preview: { text: '改好了' } })
  ev('run.finished', null, { output: { 结论: '改好了' }, usage: {}, duration_ms: 4400, timing: { wall_ms: 7400, active_ms: 4400, wait_ms: 3000 } })
  return out
}

/** 新后端的自查问题是对象：带 field、code，message 不再以「节点 id」开头 */
export const COPILOT_OBJECT_ISSUES = [
  { op: 'add_node', node: { id: 'lp', type: 'loop', label: '逐周循环' } },
  { op: 'add_node', node: { id: 'ask', type: 'agent', label: '订单查询' } },
  { op: 'done', explanation: '两步' },
  { op: 'check', status: 'repairing', round: 1, issues: [
    { level: 'error', node_id: 'lp', edge_id: null, field: 'condition', message: '循环条件有误：表达式中不支持 | 过滤器' },
  ] },
  { op: 'check', status: 'failed', issues: [
    { level: 'error', node_id: 'ask', edge_id: null, field: 'tools', code: 'datasource_out_of_scope',
      message: '使用了限定范围之外的数据源 sales_daily：本轮只允许使用 orders' },
    { level: 'error', node_id: 'gone', edge_id: null, field: null, message: '这个节点不在这一轮的操作里' },
  ] },
  { op: 'remove_node', id: 'old' },
  { op: 'final', graph: { nodes: [{ id: 'lp' }, { id: 'ask' }] }, issues: [] },
]
