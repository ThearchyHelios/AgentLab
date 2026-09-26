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
  ev('log', 'team', { level: 'info', round: 0, parallel: 3, agents: ['采购员', '质检员', '物流员'], done: false, reason: '三方面数据互不依赖，可以同时查', message: '调度 → 采购员、质检员、物流员（三方面数据互不依赖，可以同时查） · 3 人同时进行' })
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
  ev('node.skipped', 'kb', { reason: 'skip_if 成立：{{ inputs.day }} 是单日，不需要背景检索', node_type: 'retrieve', label: '背景检索' })
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
  ev('issuance', 'report', { tier: 'degraded', missing_required: [], missing_expected: [], unmatched: 0, calibers: [{ node: 'kpi', caliber: '出勤率口径', version: 'v3' }], gaps: ['叙述模板渲染为空（路径可能写错了）'], metrics_checked: 2, matched_numbers: 0 })
  ev('node.finished', 'report', { duration_ms: 3, attempt: 1, preview: {} })
  ev('node.started', 'notify', { node_type: 'tool', label: '推送企业微信' })
  ev('node.failed', 'notify', { error: '推送失败：企业微信机器人地址没配', duration_ms: 120, detail: 'httpx.ConnectError: [Errno 8] nodename nor servname provided, or not known' })
  ev('run.failed', null, { error: '推送失败：企业微信机器人地址没配', node_id: 'notify', label: '推送企业微信', detail: 'httpx.ConnectError: [Errno 8] nodename nor servname provided, or not known', timing: { wall_ms: 290000, active_ms: 110000, wait_ms: 180000 } })
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
    gaps: ['叙述模板渲染为空（路径可能写错了）'],
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
  { op: 'check', status: 'repairing', round: 1, issues: ['「lp」循环条件写错了：表达式里没有 | 过滤器'] },
  { op: 'heartbeat', phase: 'repairing', elapsed_ms: 12000 },
  { op: 'update_node', id: 'lp' },
  { op: 'check', status: 'failed', issues: ['「lp」循环条件写错了：表达式里没有 | 过滤器', '「q」没有选数据源'] },
  { op: 'final', graph: { nodes: [{ id: 'q' }, { id: 'lp' }] }, issues: [
    { level: 'error', node_id: 'lp', message: '循环条件写错了' },
    { level: 'warning', node_id: null, code: 'unknown_node_type', type: 'excel_export', message: '模型写了一个不存在的节点类型「excel_export」，这一步已跳过' },
  ] },
]
