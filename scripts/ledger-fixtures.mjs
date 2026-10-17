// 口径账本的检查夹具（check-ledger、check-evidence、check-chat 共用）：GET /api/runs/{id}/ledger 的答复形状照服务端
// app/ledger/view.py 的 run_view（labels.view_label、issue.Plan.action、reconcile.actions_of / reconciled_view），
// 挂在可点击证据的夹具（frontend/src/run/__tests__/evidence-doc.json，compose_doc 真跑出来的周报，只用通用名）上：
//   s6「45,678.5元」来自受管口径「周报口径」v2，等价已证明；s10「1,234单」来自已发布口径「订单口径」v1，助手选用；
//   s8「8.7%」相关口径「订单口径」v1（差在哪：少了一个筛选条件），可以对账；s12「37.0元」这次的推理；
//   s23「2.35%」没有比对（比对超时）。
// 口径名、表名、数都是虚构的通用名。

/** 口径的几份（calibers） */
export const WEEKLY = { caliber: 'wf-weekly@2', workflow_id: 'wf-weekly', version: 2, level: 'governed', level_text: '受管',
  workflow_name: '周报口径', graph_hash: 'aa11' }
export const ORDERS = { caliber: 'wf-orders@1', workflow_id: 'wf-orders', version: 1, level: 'published', level_text: '已发布',
  workflow_name: '订单口径', graph_hash: 'bb22' }

const caliberLabel = (c, how, extra = {}) => ({
  kind: 'caliber', how, caliber: c.caliber, workflow_id: c.workflow_id, version: c.version, level: c.level,
  level_text: c.level_text, workflow_name: c.workflow_name, ...extra,
})

export const DIFF = { items: [{ aspect: 'filter', a: null, b: "status = '[已遮罩]'", text: '口径只保留状态为已支付的行，这次没有这个条件' }],
  fallback: false, text_diff: [] }

/** 一份比对过的账本。whole：整题判断（缺省不等价）；actions、issued、reconciled 可以换 */
export function ledgerOf(runId, { whole, actions, issued = [], reconciled = [], retro = false, labels } = {}) {
  return {
    run_id: runId, state: 'matched', note: '封存后比对，不在封存范围内', post_seal: true, artifact: 'cm-1', algo: 'v1',
    trigger: retro ? 'on_demand' : 'sealed', retro, matched_at: '2026-10-15T01:00:00+00:00',
    ...(retro ? { retro_note: '按现在的口径回看' } : {}),
    calibers: [WEEKLY, ORDERS],
    queries: [
      { artifact: 'q-gmv', node_id: 'ag', source: 'shop', tables: ['orders'], status: 'proven',
        candidates: [{ ...WEEKLY, node_id: 'fetch', status: 'proven', how: 'independent' }] },
      { artifact: 'q-orders', node_id: 'ag', source: 'shop', tables: ['orders'], status: 'related',
        candidates: [{ ...ORDERS, node_id: 'fetch', status: 'related', comparable: true, via: 'heuristic', diff: DIFF,
                       binding: [{ field: 'start', value: '2026-09-01' }] }] },
    ],
    reports: { write: { labels: labels ?? {
      s6: caliberLabel(WEEKLY, 'independent', { binding: [{ field: 'week', value: '2026-W37' }], card: 'caliber', metric: 'gmv',
                                                queries: ['q-gmv'] }),
      s10: caliberLabel(ORDERS, 'composed', { binding: [{ field: 'store', masked: true, value: '已遮罩' }], card: 'caliber',
                                              metric: 'orders', queries: ['q-gmv'] }),
      s8: { kind: 'related', related: [{ caliber: ORDERS.caliber, workflow_id: ORDERS.workflow_id, version: 1, level: 'published',
                                         level_text: '已发布', workflow_name: '订单口径', node_id: 'fetch' }], queries: ['q-orders'] },
      s12: { kind: 'reasoning', query: 'q-gmv' },
      s23: { kind: 'unknown', reason: '比对超过时限，没有做完', query: 'q-gmv' },
    } } },
    primary: 'write',
    whole: whole ?? { equivalent: false, reason: '有的数是这次的推理', counts: { caliber: 2, related: 1, reasoning: 1, unknown: 1 } },
    actions: actions ?? [RECONCILE],
    issued, reconciled,
  }
}

/**
 * 答案身上的召回标记（服务端 ledger/recall.answer_marks）：周报口径升到 v3 重算过、数没变（同一份口径核对过两次，只写最近
 * 那一版）；目录里影响数字的两项被驳回、按新目录检查新出现 1 条错误级问题，另有一次出现了新码值（同归「数据目录」那一类，
 * 项数相加）；看的这个人手上有一条说的是这个答案的 recall 待办（订单口径升到 v2，数变了）
 */
export const RECALL = {
  checked: [
    { workflow_id: 'wf-weekly', workflow_name: '周报口径', from_version: 1, to_version: 2, recall_run: 'run-rc0', changed: false,
      at: '2026-10-14T01:00:00+00:00' },
    { workflow_id: 'wf-weekly', workflow_name: '周报口径', from_version: 2, to_version: 3, recall_run: 'run-rc1', changed: false,
      at: '2026-10-15T01:00:00+00:00' },
  ],
  basis: [
    { kind: 'catalog', items: 2, new_errors: 1, at: '2026-10-15T02:00:00+00:00' },
    { kind: 'values', items: 1, new_errors: 0, at: '2026-10-15T03:00:00+00:00' },
  ],
  tasks: [{ id: 'tk-recall-1', reason: 'caliber', to_version: 2 }],
}

/** 看差多少（s8 那条查询、订单口径）：参数是推断的，要先核对 */
export const RECONCILE = {
  kind: 'reconcile', query: 'q-orders', caliber: ORDERS.caliber, workflow_id: ORDERS.workflow_id, version: 1, node_id: 'fetch',
  workflow_name: '订单口径', level_text: '已发布', needs_check: true, check_reason: '参数按这次的 SQL 推断，请核对',
  binding: [{ field: 'start', value: '2026-09-01' }], fallback: false,
  fields: [{ name: 'start', required: true, has_default: false }, { name: 'store', required: false, has_default: true }],
}

/** 按口径正式出具（整题等价于周报口径）：搭建者能一键，不自动的原因写在 auto_reason */
export const ISSUE = {
  kind: 'issue', workflow_id: WEEKLY.workflow_id, version: 2, caliber: WEEKLY.caliber, workflow_name: '周报口径', how: 'independent',
  level: 'governed', level_text: '受管', available: true, auto: false, auto_reason: '设置里关了自动按口径出具',
}

/** 整题等价时的标签（三个数都来自周报口径） */
export const WHOLE_LABELS = {
  s6: caliberLabel(WEEKLY, 'independent', { binding: [{ field: 'week', value: '2026-W37' }], queries: ['q-gmv'] }),
  s10: caliberLabel(WEEKLY, 'independent', { binding: [{ field: 'week', value: '2026-W37' }], queries: ['q-gmv'] }),
  s12: caliberLabel(WEEKLY, 'independent', { binding: [{ field: 'week', value: '2026-W37' }], queries: ['q-gmv'] }),
}
export const WHOLE = { equivalent: true, caliber: WEEKLY.caliber, workflow_id: WEEKLY.workflow_id, version: 2, level: 'governed',
  level_text: '受管', workflow_name: '周报口径', how: 'independent', binding: [{ field: 'week', value: '2026-W37' }],
  missing: [], missing_required: [], counts: { caliber: 3, related: 0, reasoning: 0, unknown: 0 } }

/** 对过的一次账（单行：一组对照）。status 可以换 */
export const reconciledOf = (status = 'succeeded') => ({
  run_id: 'run-rec-1', status, query: 'q-orders', caliber: ORDERS.caliber, workflow_id: ORDERS.workflow_id, version: 1,
  inputs_from: 'checked', rows: null, card: 'reconcile',
  groups: [{ label: '订单数', ours: 'ours_0', caliber: 'cal_0', diff: 'diff_0', rel: 'rel_0' }], created_at: '2026-10-15T02:00:00Z',
})

/** 对账卡上四个指标的展示值（/evidence/metrics/reconcile/{id} 的 rendered） */
export const RECONCILE_VALUES = { ours_0: '1,234单', cal_0: '1,180单', diff_0: '54单', rel_0: '4.58%' }

/** 对账运行的指标出处（形状同片段接口：chain、seal），给 EvidenceDrawer 画 */
export const metricDetail = (runId, metric) => ({
  run_id: runId, node: 'reconcile', metric, rendered: RECONCILE_VALUES[metric] ?? '—', eid: `ev:metric:${metric}`,
  chain: [{ step: 'metric', alias: 'm', metric, name: metric, value: 1, rendered: RECONCILE_VALUES[metric], node_id: 'reconcile',
            caliber: '口径对账', version: 'v1', expression: 'cell(nodes.ours_q1, 0, \'orders\')', sealed: true, inputs: [] }],
  seal: { sealed: true, ok: true, covered: true }, redacted: { columns: [] },
})

/**
 * 对账卡上「按口径」那个数的出处（服务端 api/evidence._upstream_steps 的形状）：它转抄口径卡的指标
 * （nodes.card.metrics[0].value），出处跨卡接上口径卡「订单数」那一步，再到口径的取数查询（q-cal-fetch）
 */
export const copiedMetricDetail = (runId, metric) => ({
  run_id: runId, node: 'reconcile', metric, rendered: RECONCILE_VALUES[metric] ?? '—', eid: `ev:metric:${metric}`,
  chain: [
    { step: 'metric', alias: 'm', metric, name: '订单数 · 口径', value: 1180, rendered: RECONCILE_VALUES[metric], node_id: 'reconcile',
      caliber: '口径对账', version: 'v1', expression: 'nodes.card.metrics[0].value', substituted: '1180', recompute_ok: true,
      sealed: true, hash_ok: true, untraced: [],
      inputs: [{ path: 'nodes.card.metrics[0].value', value: 1180, node_id: 'card', via: 'metric', field: 'metrics[0].value',
                 status: 'ok', upstream: { metric: 'orders', artifact: 'ms-card', depth: 1, traced: true } }] },
    { step: 'upstream', of: 'nodes.card.metrics[0].value', node_id: 'card', label: '口径卡', artifact: 'ms-card', metric: 'orders',
      traced: true, depth: 1, hash_ok: true, sealed: true, name: '订单数', value: 1180, unit: '单', rendered: '1,180单',
      caliber: '订单口径', version: 'v1', expression: "cell(nodes.fetch, 0, 'orders')", substituted: '1180', recompute_ok: true,
      status: 'ok', untraced: [],
      inputs: [{ step: 'input', path: "cell(nodes.fetch, 0, 'orders')", value: 1180, node_id: 'fetch', via: 'tool_cell',
                 status: 'verified', artifact: 'q-cal-fetch', locator: { row: 0, column: 'orders' }, eid: 'ev:cell:q-cal-fetch' }] },
    { step: 'query', alias: null, artifact: 'q-cal-fetch', node_id: 'fetch', tool: 'db_query__shop', source: 'shop',
      sql: "SELECT COUNT(*) AS orders FROM orders WHERE status = 'paid'", columns: ['orders'], rows: [[1180]], row_offset: 0,
      row_index: [0], total_rows: 1, highlight: { rows: [0], cols: ['orders'], cells: [[0, 'orders']] }, sealed: true, hash_ok: true },
  ],
  seal: { sealed: true, ok: true, covered: true }, redacted: { columns: [] },
})

/** 认证队列：三类来源各一项 */
export const QUEUE = {
  items: [
    { id: 'shape:aa11', kind: 'shape', source: 'shop', shape: 'SELECT SUM(amount) FROM orders WHERE paid_at >= ? AND status = ?',
      runs: 4, conversations: 3, question: '上周销售额是多少',
      turns: [{ run_id: 'run-s1', conversation_id: 'c-1', turn_id: 't-1', question: '上周销售额是多少' },
              { run_id: 'run-s2', conversation_id: 'c-2', turn_id: 't-2', question: '这周销售额是多少' }],
      params: [{ index: 0, distinct: 3 }], constants: 1, related: [{ caliber: ORDERS.caliber, workflow_id: ORDERS.workflow_id, version: 1, level: 'published' }],
      representative: { run_id: 'run-s1', query: 'q-1' }, peers: ['run-s2', 'run-s3'], frozen_into: [] },
    { id: 'cluster:bb22', kind: 'cluster', question: '各门店的退款率', count: 3,
      turns: [{ run_id: 'run-c1', turn_id: 't-c1', question: '各门店的退款率' }, { run_id: 'run-c2', turn_id: 't-c2', question: '门店退款率怎么样' }],
      frozen_into: [] },
    { id: 'reused:cc33', kind: 'reused', question: '本月新客数', uses: { exports: 2, app: false, extracted: false, canvas: true },
      turns: [{ run_id: 'run-r1', conversation_id: 'c-3', turn_id: 't-r1', question: '本月新客数' }],
      representative: { run_id: 'run-r1' }, frozen_into: [{ workflow_id: 'wf-new-old', name: '本月新客数（固化的口径草稿）', status: 'draft' }] },
  ],
  considered: { runs: 120, matched: 100, questions: 90 },
  threshold: { runs: 3, conversations: 2, exports: 2 },
}
