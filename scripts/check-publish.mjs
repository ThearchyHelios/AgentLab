// 发布前检查与自动修复的检查：发布弹窗打开就列问题、逐条修（auto / choice / assist）、
// 先预览再「应用并重新检查」、问题面板里的「发布前检查」、老后端（两个接口 404）照旧。
// 另有一键升级为可追溯结构（可点击证据五期）：问题面板的快速修复、升级预览、应用走现有的保存、
// 从记录页横幅跳过来（?upgrade=1）。升级接口和校验给的那条建议同样在浏览器里伪造。
//
// 不写库：工作流、保存、发布、publish-check、autofix 全在浏览器里用 page.route 伪造。
// 伪造的检查接口按请求里的图现算（下面的 lint），所以修完、存完、再查，结论会真的变；
// 保存的载荷、autofix 的请求体都拦下来核对。其余写请求一律 409 并记账，检查结束时必须是空的。
//
// 跑之前前端得起着（./scripts/dev.sh），默认连 5273。对别的实例（比如一份沙箱拷贝）跑时
// 带上地址：AGENTLAB_WEB=http://localhost:<前端端口> node scripts/check-publish.mjs
//   PUBLISH_ONLY=choice,老后端 只跑段名里含这些字的段
//   PUBLISH_SHOTS=<目录>      几处界面各截一张亮、暗（含 360px）
import { mkdirSync } from 'node:fs'
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const CHROME = process.env.CHROME_PATH ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const ONLY = (process.env.PUBLISH_ONLY ?? '').split(',').filter(Boolean)
const SHOTS = process.env.PUBLISH_SHOTS ?? ''
if (SHOTS) mkdirSync(SHOTS, { recursive: true })

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

// ---------------------------------------------------------------- 夹具（通用示例名）

const node = (id, type, x, label, config = {}) => ({ id, type, position: { x, y: 120 }, data: { label, config } })
const chain = (...ids) => ids.slice(1).map((t, i) => ({ id: `e_${ids[i]}_${t}`, source: ids[i], target: t, sourceHandle: null, label: '' }))

/** 受管模板的样子：审批全放行、子工作流没钉版本、契约缺 metrics_from 和 required、有协作团队、契约不 strict */
const HARD = {
  nodes: [
    node('in', 'input', 0, '入口', { fields: [{ name: 'week', required: true }] }),
    node('fetch', 'agent', 240, '取数', { prompt: '查 orders 本周的 gmv', tools: ['db_query__shop'], approval: 'never' }),
    node('sub', 'subgraph', 480, '方法卡', { workflow_id: 'pf-lib', input: {} }),
    node('caliber', 'metrics', 720, '周报口径', { caliber: '周报口径', caliber_version: 'v1',
      metrics: [{ id: 'gmv', name: '销售额', expression: 'vars.kpi.gmv' }, { id: 'orders', name: '订单数', expression: 'vars.kpi.orders' }] }),
    node('caliber2', 'metrics', 960, '补充口径', { caliber: '补充口径', caliber_version: 'v1',
      metrics: [{ id: 'aov', name: '客单价', expression: 'vars.kpi.gmv / vars.kpi.orders' }] }),
    node('team', 'supervisor', 1200, '复核团队', { goal: '复核', max_rounds: 4, agents: [{ name: 'checker', tools: [] }] }),
    node('done', 'output', 1440, '成果', { fields: [{ name: 'report', value: '{{ vars.report }}' }],
      contract: { narrative: '{{ vars.report }}', strict: false } }),
  ],
  edges: chain('in', 'fetch', 'sub', 'caliber', 'caliber2', 'team', 'done'),
}
/** 只有一处能自动修的错（审批）加一条能自动修的提示（strict）：一键修完就能发 */
const EASY = {
  nodes: [
    node('in', 'input', 0, '入口', { fields: [{ name: 'week', required: true }] }),
    node('fetch', 'agent', 240, '取数', { prompt: '查 orders 本周的 gmv', tools: ['db_query__shop'], approval: 'never' }),
    node('caliber', 'metrics', 480, '周报口径', { caliber: '周报口径', caliber_version: 'v1',
      metrics: [{ id: 'gmv', name: '销售额', expression: 'vars.kpi.gmv' }] }),
    node('done', 'output', 720, '成果', { fields: [{ name: 'report', value: '{{ vars.report }}' }],
      contract: { metrics_from: ['caliber'], narrative: '{{ vars.report }}', required: ['gmv'], strict: false } }),
  ],
  edges: chain('in', 'fetch', 'caliber', 'done'),
}

/** 受管、唯一的出口没有契约：图级问题（没有节点），修复却落在那个出口上——生成契约骨架，required 要人选 */
const BARE = {
  nodes: [
    node('in', 'input', 0, '入口', { fields: [{ name: 'week', required: true }] }),
    node('caliber', 'metrics', 240, '周报口径', { caliber: '周报口径', caliber_version: 'v1',
      metrics: [{ id: 'gmv', name: '销售额', expression: 'vars.kpi.gmv' }, { id: 'orders', name: '订单数', expression: 'vars.kpi.orders' }] }),
    node('write', 'report', 480, '报告撰写', { instructions: '写周报' }),
    node('done', 'output', 720, '成果', { fields: [{ name: 'report', value: '{{ nodes.write.text }}' }] }),
  ],
  edges: chain('in', 'caliber', 'write', 'done'),
}
/**
 * 同一个修复挂在好几条问题上：两个 agent 都没写审批、跟随全图默认的「全部自动放行」（问题各在节点上，
 * 修复只有一条、落在全图上）；契约缺 metrics_from，validate 和门禁各报一条（同 code、同节点、同一个修复）
 */
const DUP = {
  nodes: [
    node('in', 'input', 0, '入口', { fields: [{ name: 'week', required: true }] }),
    node('ask1', 'agent', 240, '查数员', { prompt: '查 orders 本周的 gmv', tools: ['db_query__shop'] }),
    node('ask2', 'agent', 480, '复核员', { prompt: '复核 orders 本周的 gmv', tools: ['db_query__shop'] }),
    node('caliber', 'metrics', 720, '周报口径', { caliber: '周报口径', caliber_version: 'v1',
      metrics: [{ id: 'gmv', name: '销售额', expression: 'vars.kpi.gmv' }] }),
    node('done', 'output', 960, '成果', { fields: [{ name: 'report', value: '{{ vars.report }}' }],
      contract: { narrative: '{{ vars.report }}', required: ['gmv'], strict: true } }),
  ],
  edges: chain('in', 'ask1', 'ask2', 'caliber', 'done'),
}

/**
 * 两个 choice：沙箱代码喂口径卡（G4），候选里「交给 Copilot」那一项带 handoff 标记；成果字段接哪个报告
 * （G1），唯一的候选是一个 id 恰好叫 copilot 的报告撰写节点——它只是普通候选，不是「交给 Copilot」
 */
const HANDOFF = {
  nodes: [
    node('in', 'input', 0, '入口', { fields: [{ name: 'week', required: true }] }),
    node('calc', 'code', 240, '计算', { code: 'print(1)', assign_to: 'calc' }),
    node('caliber', 'metrics', 480, '周报口径', { caliber: '周报口径', caliber_version: 'v1',
      metrics: [{ id: 'ratio', name: '比例', expression: 'vars.calc.ratio' }] }),
    node('copilot', 'report', 720, '报告撰写', { instructions: '写周报', numbers: 'strict', on_violation: 'fail', claims: 'require_citation' }),
    node('done', 'output', 960, '成果', { fields: [{ name: 'report', value: '{{ vars.story }}' }],
      contract: { report_from: 'copilot', metrics_from: ['caliber'], required: ['ratio'], strict: true } }),
  ],
  edges: chain('in', 'calc', 'caliber', 'copilot', 'done'),
}

/**
 * 一键升级的旧结构（R1、R4、R5 都有）：出具契约的 narrative 指着一个模型调用节点；取数的 agent 配了
 * output_schema 没开 cite_fields；口径卡还吃一个代码节点的产出。带全图默认，升级、保存都得把它带着
 */
const LEGACY = {
  nodes: [
    node('in', 'input', 0, '入口', { fields: [{ name: 'week', required: true }] }),
    node('fetch', 'agent', 240, '取数', { prompt: '查 orders 本周的 gmv', tools: ['db_query__shop'], assign_to: 'kpi',
      output_schema: { type: 'object', properties: { gmv: { type: 'number' } } } }),
    node('calc', 'code', 240, '计算', { code: 'print(1)', assign_to: 'calc' }),
    node('caliber', 'metrics', 480, '周报口径', { caliber: '周报口径', caliber_version: 'v1',
      metrics: [{ id: 'gmv', name: '销售额', expression: 'vars.kpi.gmv' }, { id: 'ratio', name: '比例', expression: 'vars.calc.ratio' }] }),
    node('story', 'llm', 720, '写周报', { system: '你是分析师', prompt: '按口径写本周周报' }),
    node('done', 'output', 960, '成果', { fields: [{ name: 'report', value: '{{ nodes.story.text }}' }],
      contract: { metrics_from: ['caliber'], narrative: '{{ nodes.story.text }}', required: ['gmv'], strict: true } }),
  ],
  edges: [...chain('in', 'fetch', 'caliber', 'story', 'done'), ...chain('in', 'calc', 'caliber')],
  defaults: { approval: 'always', model: 'demo-model' },
}
/** 问数据页的典型图（R3）：输入 → agent → 成果，升级要在 agent 和成果之间插一个报告撰写节点 */
const ASK = {
  nodes: [
    node('in', 'input', 0, '问题', { fields: [{ name: 'question', required: true }] }),
    node('fetch', 'agent', 240, '取数', { prompt: '{{ input.question }}', tools: ['db_query__shop'] }),
    node('done', 'output', 480, '成果', { fields: [{ name: 'answer', value: '{{ nodes.fetch.text }}' }] }),
  ],
  edges: chain('in', 'fetch', 'done'),
}

/**
 * 调用工具节点里写死的 SQL 对照数据目录查出了问题（数据目录阶段 4B）：门禁的问题 code 是规则编号，field 是 args.sql。
 * 参数里 sql 不在第一行，定位要在 JSON 里找到这个键
 */
const SQLWF = {
  nodes: [
    node('in', 'input', 0, '入口', { fields: [{ name: 'week', required: true }] }),
    node('sum', 'tool', 240, '订单汇总', { tool: 'db_query__shop', args: { limit: 500, timeout: 30,
      sql: 'SELECT o.region, SUM(o.amount) AS amt\nFROM orders o JOIN order_items i ON i.order_id = o.id\nGROUP BY o.region' } }),
    node('done', 'output', 480, '成果', { fields: [{ name: 'table', value: '{{ nodes.sum.output }}' }] }),
  ],
  edges: chain('in', 'sum', 'done'),
}

const wf = (id, name, graph, extra = {}) => ({
  id, name, description: '检查脚本伪造的工作流', graph, tags: [], version: 3, is_template: false,
  status: 'draft', published_version: null, published_by: null, run_count: 0,
  created_at: '2026-09-20T02:00:00Z', updated_at: '2026-09-26T02:00:00Z', ...extra,
})
const FAKES = {
  'pf-hard': wf('pf-hard', '__publish_check_hard__', HARD, { status: 'governed', published_version: 2 }),
  'pf-easy': wf('pf-easy', '__publish_check_easy__', EASY),
  'pf-lib': wf('pf-lib', '__publish_check_lib__', { nodes: [], edges: [] }, { status: 'published', published_version: 4 }),
  'pf-bare': wf('pf-bare', '__publish_check_bare__', BARE, { status: 'governed', published_version: 2 }),
  'pf-dup': wf('pf-dup', '__publish_check_dup__', DUP, { status: 'governed', published_version: 2 }),
  'pf-handoff': wf('pf-handoff', '__publish_check_handoff__', HANDOFF, { status: 'governed', published_version: 2 }),
  // 带全图默认的图：画布只管节点和连线，保存、检查、修复都得把 defaults 原样带着
  'pf-defs': wf('pf-defs', '__publish_check_defaults__', { ...EASY, defaults: { approval: 'always', model: 'demo-model' } }),
  'pf-legacy': wf('pf-legacy', '__upgrade_check_legacy__', LEGACY, { status: 'published', published_version: 3 }),
  'pf-ask': wf('pf-ask', '__upgrade_check_ask__', ASK),
  'pf-sql': wf('pf-sql', '__publish_check_sql__', SQLWF),
}

const clone = (x) => JSON.parse(JSON.stringify(x))
const who = (n) => `「${n.data.label}」`
/** 指标候选的写法照后端 autofix._metric_options：「名称（id）」，提示写来自哪个口径卡 */
const metricOptions = (m) => m.data.config.metrics.map((x) => ({ value: x.id, label: `${x.name}（${x.id}）`, hint: `口径卡「${m.data.label}」` }))

/**
 * 伪造的门禁：按请求里的图现算，形状照 PF-SPEC（issues 带 code 和 fix，fixes 列修法）。
 * 已发布档只给提示，受管档有错。修法的 id 是 code:节点（图级的是 code:graph）。
 * followsNever：当作这张图的工作流默认设置是「全部无需审批」（画布上不带 defaults，按工作流认）
 */
function lint(graph, level, { followsNever = false } = {}) {
  const hard = level === 'governed'
  const issues = []
  const fixes = []
  const add = (n, code, message, fix, isError = true) => {
    const id = `${code}:${n.id}`
    issues.push({ level: isError && hard ? 'error' : 'warning', node_id: n.id, code, message, fix: fix ? id : null, field: null })
    if (fix) fixes.push({ id, code, node_id: n.id, ...fix })
  }
  const cards = graph.nodes.filter((n) => n.type === 'metrics')
  const outputs = graph.nodes.filter((n) => n.type === 'output')
  // 图级：唯一的出口没有契约。问题不挂节点，修复落在那个出口上（和后端 autofix._target 一样）
  if (hard && outputs.length === 1 && !outputs[0].data.config?.contract) {
    const id = `governed.no_contract:${outputs[0].id}`
    const report = graph.nodes.find((r) => r.type === 'report')
    issues.push({ level: 'error', node_id: null, code: 'governed.no_contract', fix: id, field: null,
      message: '受管级别要求至少一个「成果」节点声明出具契约，否则无法判定出具档位' })
    fixes.push({ id, code: 'governed.no_contract', node_id: outputs[0].id, kind: 'choice', multiple: true,
      label: `为${who(outputs[0])}生成出具契约：核对${report ? who(report) : '「报告撰写」'}的文档，指标来自${cards.length ? who(cards[0]) : '「口径卡」'}，再选择「必需指标」`,
      options: cards.flatMap(metricOptions) })
  }
  for (const n of graph.nodes) {
    const c = n.data.config ?? {}
    // 基于数据目录的 SQL 检查（governance._lint_sql）：错误级在受管档是硬性问题，已发布档只提示；没有确定性的修复
    if (n.type === 'tool' && /JOIN order_items/i.test(c.args?.sql ?? '')) {
      issues.push({ level: hard ? 'error' : 'warning', sql_level: 'error', node_id: n.id, code: 'fanout_sum', fix: null, field: 'args.sql',
        message: '「订单」关联「订单明细」是一对多，对「订单」的「订单金额」求和会重复计算。请先按订单汇总明细再关联' })
    }
    if (followsNever && n.type === 'agent' && !c.approval && (c.tools ?? []).length) {
      const id = 'governed.default_approval_never:graph'
      issues.push({ level: hard ? 'error' : 'warning', node_id: n.id, code: 'governed.default_approval_never', fix: id, field: 'approval',
        message: `${who(n)}（Agent）没有设置审批策略，跟随工作流默认设置中的「全部无需审批」；受管级别要求危险工具至少经过人工审批。`
          + '请将工作流默认设置中的审批策略改为「仅危险工具需要审批」，或在该节点上单独设置' })
      if (!fixes.some((f) => f.id === id)) {
        const followers = graph.nodes.filter((x) => x.type === 'agent' && !x.data.config?.approval && (x.data.config?.tools ?? []).length)
        fixes.push({ id, code: 'governed.default_approval_never', node_id: null, kind: 'auto',
          label: `将工作流默认设置中的审批策略改为「仅危险工具需要审批」（${followers.map(who).join('、')}一并生效）`,
          preview: { field: 'defaults.approval', before: 'never', after: 'dangerous' } })
      }
    }
    if (n.type === 'agent' && c.approval === 'never' && (c.tools ?? []).length) {
      add(n, 'governed.agent_approval_never', `${who(n)}（Agent）的审批策略是「全部无需审批」，受管级别要求危险工具至少经过人工审批。请改为「仅危险工具需要审批」`,
        { kind: 'auto', label: `将${who(n)}的审批策略改为「仅危险工具需要审批」`, preview: { field: 'approval', before: 'never', after: 'dangerous' } })
    }
    if (n.type === 'subgraph' && !c.workflow_version) {
      add(n, 'governed.subgraph_unpinned', `${who(n)}（子工作流）没有固定版本，口径会随上游最新版本变化。请在节点中选定一个版本`,
        { kind: 'auto', label: `将${who(n)}固定到上游当前的发布版本 v4`, preview: { field: 'workflow_version', before: null, after: 4 } })
    }
    if (n.type === 'code' && c.evidence_role !== 'source') {
      const to = graph.edges.filter((e) => e.source === n.id).map((e) => graph.nodes.find((x) => x.id === e.target))
        .filter((x) => x?.type === 'metrics').map(who).join('、')
      add(n, 'governed.caliber_compute_input', `${who(n)}（沙箱代码）的产出提供给了口径卡${to}，但它的「证据角色」是「计算」`
        + `${c.evidence_role ? '' : '（未设置时默认为「计算」）'}：沙箱中计算出的数字无法追溯出处。`
        + '若该节点负责取数，请将「证据角色」设为「取数」；若负责计算，请把计算移到口径卡的表达式中', {
        kind: 'choice', label: `${who(n)}的产出提供给了口径卡：它负责取数，还是负责计算？`,
        options: [{ value: 'source', label: '负责取数：将「证据角色」设为「取数」', hint: '口径卡读取它时会记录出处' },
          { value: 'copilot', label: '负责计算：交给助手把计算移到口径卡', hint: '助手的修改同样只是预览，需要你决定的事项会交给你确认', handoff: true }],
      })
    }
    if (n.type === 'output' && c.contract?.report_from && !(c.fields ?? []).every((f) => f.value.includes(`nodes.${c.contract.report_from}.`))) {
      const name = c.fields?.[0]?.name
      add(n, 'governed.exit_text_source', `${who(n)}（成果）的成果字段「${name}」取自其他节点的产出：受管级别出具时，给人看的文字只能来自`
        + '「报告撰写」节点，其他节点写的文字无法追溯出处。请改为取报告撰写节点的正文', {
        kind: 'choice', label: `把成果字段「${name}」改为取哪个报告撰写节点的正文`,
        options: graph.nodes.filter((r) => r.type === 'report').map((r) => ({ value: r.id, label: who(r), hint: `报告撰写 · {{ nodes.${r.id}.text }}` })),
      })
    }
    if (n.type === 'supervisor') {
      add(n, 'governed.supervisor', `${who(n)}（多 Agent 协作）：受管级别不允许使用全动态规划的节点，其分工和轮数由模型在运行时决定，`
        + '无法事先审核。请改用配置固定的 Agent 或「模型调用」节点',
        { kind: 'assist', label: `${who(n)}需要改为配置固定的 Agent 或「模型调用」节点：修复不能删除节点，助手只会给出拆分建议，替换需要由你完成` })
    }
    if (n.type === 'output' && c.contract) {
      const k = c.contract
      if (!(k.metrics_from ?? []).length) {
        // 真后端 validate 和门禁各报一条（同 code、同节点，说法不同），指向同一个修复；validate 的排在最前
        if (followsNever) {
          issues.unshift({ level: 'error', node_id: n.id, code: 'contract.metrics_from_missing', fix: `contract.metrics_from_missing:${n.id}`,
            field: 'contract.metrics_from', message: '出具契约缺少「指标来自」（提供指标的口径卡）' })
        }
        // 后端这里是多选（「从哪几个口径卡取指标」）；伪造成单选，专门看 radio 这一种控件
        add(n, 'contract.metrics_from_missing', '出具契约没有设置「指标来自」（提供指标的口径卡），叙述中的数字无法对应到指标', {
          kind: 'choice', label: '选择出具契约的「指标来自」：从哪个口径卡取指标', default: cards[0]?.id,
          options: cards.map((m) => ({ value: m.id, label: m.data.label, hint: '口径卡' })),
        })
      }
      if (hard && !(k.required ?? []).length) {
        const from = (k.metrics_from ?? []).length ? cards.filter((m) => k.metrics_from.includes(m.id)) : cards
        add(n, 'contract.required_missing', '受管级别的出具契约必须设置「必需指标」，否则「不予出具」这一档无法触发', {
          kind: 'choice', multiple: true, label: '选择受管级别出具的「必需指标」：缺少其中任何一个都将不予出具',
          options: from.flatMap(metricOptions),
        })
      }
      if (hard && !k.strict) {
        add(n, 'contract.strict_off', '受管级别建议开启出具契约的「严格模式」：未开启时，无法追溯出处的数字只会使出具降档，不会被拦截',
          { kind: 'auto', label: '开启出具契约的「严格模式」：没有出处的数字直接拦截，而不只是降档', preview: { field: 'contract.strict', before: false, after: true } }, false)
      }
    }
  }
  return { level, ok: !issues.some((i) => i.level === 'error'), issues, fixes }
}

/** 伪造的 autofix：在副本上照选中的修法改，逐项记 changes；钉不上的（pf-lib 没发过版时）进 rejected */
function autofix(body, { unpublished = false, followsNever = false } = {}) {
  const graph = clone(body.graph)
  const before = lint(graph, body.level, { followsNever })
  const changes = []
  const rejected = []
  const applied = []
  const handoff = []
  const byId = (id) => graph.nodes.find((n) => n.id === id)
  for (const id of body.apply ?? []) {
    const fix = before.fixes.find((f) => f.id === id)
    if (!fix) { rejected.push({ fix_id: id, reason: '这一处问题已不存在，请重新检查' }); continue }
    if (fix.code === 'governed.default_approval_never') {
      // 图级：改的是全图默认，不落在任何节点上
      graph.defaults = { ...(graph.defaults ?? {}), approval: 'dangerous' }
      changes.push({ fix_id: id, node_id: null, node_title: '', field: 'defaults.approval', before: 'never', after: 'dangerous', label: fix.label })
      applied.push(id)
      continue
    }
    const n = byId(fix.node_id)
    const c = n.data.config
    const change = (field, from, to) => changes.push({ fix_id: id, node_id: n.id, node_title: n.data.label, field, before: from, after: to, label: fix.label })
    if (fix.code === 'governed.agent_approval_never') { change('approval', c.approval, 'dangerous'); c.approval = 'dangerous' }
    else if (fix.code === 'governed.subgraph_unpinned') {
      if (unpublished) { rejected.push({ fix_id: id, reason: `${who(n)}嵌套的工作流还没有发布版本，无法自动固定版本。请先发布该工作流，或交给助手处理` }); continue }
      change('workflow_version', c.workflow_version ?? null, 4); c.workflow_version = 4
    } else if (fix.code === 'contract.strict_off') { change('contract.strict', !!c.contract.strict, true); c.contract.strict = true }
    else if (fix.code === 'contract.metrics_from_missing') {
      const v = body.choices?.[id]
      if (v === undefined) { rejected.push({ fix_id: id, reason: `这一处需要你选择：${fix.label}` }); continue }
      change('contract.metrics_from', c.contract.metrics_from ?? null, [v]); c.contract.metrics_from = [v]
    } else if (fix.code === 'governed.no_contract') {
      const v = body.choices?.[id]
      if (!Array.isArray(v) || !v.length) { rejected.push({ fix_id: id, reason: `这一处需要你选择：${fix.label}` }); continue }
      const contract = { report_from: 'write', metrics_from: ['caliber'], strict: true, required: v }
      change('contract', null, contract); c.contract = contract
    } else if (fix.code === 'governed.caliber_compute_input') {
      const v = body.choices?.[id]
      // 只认候选上的 handoff 标记，和后端 autofix._handoff 一样
      if (fix.options.some((o) => o.handoff && o.value === v)) { handoff.push(id); continue }
      change('evidence_role', c.evidence_role ?? null, v); c.evidence_role = v
    } else if (fix.code === 'governed.exit_text_source') {
      const v = body.choices?.[id]
      const to = `{{ nodes.${v}.text }}`
      change('fields[0].value', c.fields[0].value, to); c.fields[0].value = to
    } else if (fix.code === 'contract.required_missing') {
      const v = body.choices?.[id]
      if (!Array.isArray(v) || !v.length) { rejected.push({ fix_id: id, reason: `这一处需要你选择：${fix.label}` }); continue }
      change('contract.required', c.contract.required ?? null, v); c.contract.required = v
    } else { rejected.push({ fix_id: id, reason: '这一处属于结构性问题，需要交给助手处理' }); continue }
    applied.push(id)
  }
  let assist = null
  if (body.assist || handoff.length) {
    const team = byId('team')
    if (team) {
      changes.push({ fix_id: 'assist', node_id: 'team', node_title: team.data.label, field: 'max_rounds', before: team.data.config.max_rounds, after: 2,
        label: '助手的修改' })
      team.data.config.max_rounds = 2
    }
    assist = { ok: true, summary: '把复核团队的轮数收紧到 2 轮；换成固定编排要删节点，按规矩没动它',
      questions: ['复核团队要换成哪一个固定编排的 Agent？它现在只有一个成员 checker，保留它的职责吗？'] }
  }
  const remaining = lint(graph, body.level, { followsNever })
  return { graph, changes, applied, rejected, handoff, remaining: remaining.issues, assist, ok: remaining.ok, ops: [] }
}

/**
 * 伪造的校验建议：图里还有模型调用节点，或者 agent 的文字直接进了成果（问数据的图），就给一条 info
 * 「可以升级为可追溯结构」。升级完（节点换成了报告撰写、插了报告节点）就没有了
 */
function upgradeAdvice(graph) {
  const byId = new Map(graph.nodes.map((n) => [n.id, n]))
  const old = graph.nodes.some((n) => n.type === 'llm')
    || graph.edges.some((e) => byId.get(e.source)?.type === 'agent' && byId.get(e.target)?.type === 'output')
  return old ? [{ level: 'info', code: 'evidence.upgrade_available', node_id: null, field: null, message: '可以升级为可追溯结构' }] : []
}

/** R5、R3 的说明原文：原样显示，检查逐字比对 */
const R5_NOTE = '「计算」（沙箱代码）的产出提供给了口径卡「周报口径」：若它负责取数（查库、调用接口、读文件），请把「证据角色」设为「取数」，'
  + '口径卡读取时会记录出处；若负责计算，请把计算移到口径卡的表达式中（可交给助手把纯算术的代码改写为口径卡表达式）。它负责取数还是计算需要由你确认，升级未作修改'
const R3_NOTE = '报告会直接引用查询单元格。探索运行和已发布级别不受影响；如需按受管级别正式出具，请在成果节点的出具契约中开启「单元格引用」，'
  + '或改用口径卡：把要写的数字登记为指标，报告只引用指标。选择哪一种需要由你决定，升级未添加出具契约'

/**
 * 伪造的升级接口，形状照 E5-back 的 engine/upgrade.py：一步（fix_id = 规则:节点）的几项改动共用一句 label；
 * 换类型是 field type，新插入的节点是 field node（after 是 {id, type, label, config}），连线是 field edge
 * （改接的两头都有），成果字段整列改。bare：只给图、不给 changes（前端按前后两张图自己列）。
 * assist：Copilot 的一段总结和保留没改的代码节点
 */
function upgradeFake(body, { bare = false } = {}) {
  const graph = clone(body.graph)
  const changes = []
  const notes = []
  const at = (id) => graph.nodes.find((n) => n.id === id)
  const step = (rule, id, label, list) => changes.push(...list.map((c) => ({ fix_id: `${rule}:${id}`, rule, label, ...c })))
  const story = at('story')
  if (story?.type === 'llm') {
    const c = story.data.config
    const instructions = `${c.system}\n\n${c.prompt}`
    story.type = 'report'
    story.data.config = { instructions }
    const done = at('done')
    const before = clone(done.data.config.contract)
    done.data.config.contract = { metrics_from: ['caliber'], report_from: 'story', required: ['gmv'], strict: true }
    step('R1', 'story', '将「写周报」换成报告撰写，出具契约改为核对它的文档', [
      { node_id: 'story', node_title: '写周报', field: 'type', before: 'llm', after: 'report' },
      { node_id: 'story', node_title: '写周报', field: 'instructions', before: null, after: instructions },
      { node_id: 'story', node_title: '写周报', field: 'system', before: c.system, after: null },
      { node_id: 'story', node_title: '写周报', field: 'prompt', before: c.prompt, after: null },
      { node_id: 'done', node_title: '成果', field: 'contract', before, after: done.data.config.contract },
    ])
  }
  const fetch = at('fetch')
  const done = at('done')
  if (!at('report') && fetch && done && graph.edges.some((e) => e.source === 'fetch' && e.target === 'done')) {
    const config = { instructions: '{{ input.question }}' }
    graph.nodes.push({ id: 'report', type: 'report', position: { x: 480, y: 240 }, data: { label: '报告撰写', config } })
    done.position = { x: 720, y: 120 }
    graph.edges = graph.edges.filter((e) => !(e.source === 'fetch' && e.target === 'done'))
    graph.edges.push({ id: 'fetch:->report', source: 'fetch', target: 'report' }, { id: 'report:->done', source: 'report', target: 'done' })
    const was = clone(done.data.config.fields)
    done.data.config.fields = was.map((f) => ({ ...f, value: '{{ nodes.report.text }}' }))
    step('R3', 'fetch', '在「取数」和「成果」之间插入报告撰写，成果节点改为取报告的正文', [
      { node_id: 'report', node_title: '报告撰写', field: 'node', before: null, after: { id: 'report', type: 'report', label: '报告撰写', config } },
      { node_id: null, node_title: '', field: 'edge', before: null, after: { source: 'fetch', target: 'report' } },
      { node_id: null, node_title: '', field: 'edge', before: { source: 'fetch', target: 'done' }, after: { source: 'report', target: 'done' } },
      { node_id: 'done', node_title: '成果', field: 'fields', before: was, after: done.data.config.fields },
    ])
    notes.push({ rule: 'R3', node_id: 'report', level: 'info', text: R3_NOTE })
  }
  if (fetch?.data.config.output_schema && !fetch.data.config.cite_fields) {
    fetch.data.config.cite_fields = true
    step('R4', 'fetch', '开启「取数」的「按出处核对字段」：每个字段都核对到查询结果中的对应单元格', [
      { node_id: 'fetch', node_title: '取数', field: 'cite_fields', before: null, after: true }])
  }
  if (at('calc')) notes.push({ rule: 'R5', node_id: 'calc', level: 'info', text: R5_NOTE })
  const assist = body.assist
    ? { ok: true, summary: '「计算」调用了日期函数，不是纯算术：按规矩保留原样', questions: [],
        warnings: ['「计算」不是纯算术（用了 datetime），保留原样，没有改成口径卡表达式'] }
    : null
  return { graph, changes: bare ? [] : changes, ops: [], notes, issues: [], applied: [...new Set(changes.map((c) => c.fix_id))],
    rejected: [], assist, ok: true }
}

// ---------------------------------------------------------------- 浏览器

const browser = await chromium.launch({ executablePath: CHROME })
const writes = []

/**
 * backend：'new'（两个接口都有）| 'old'（两个接口 404）| 'nofix'（有检查、没有 autofix）
 * save：'ok' | 'fail'（保存接口 500）| 'slow'（1.5 秒后才回，看保存进行中的样子）
 */
/**
 * upgrade（一键升级那几段）：'new' 有接口 | 'old' 接口 404 | 'bare' 只回图不回 changes；
 * advice：校验给不给那条建议（老后端不给）；path：打开的地址（默认 /studio/<id>）
 */
async function open({ id = 'pf-hard', width = 1440, height = 900, backend = 'new', save = 'ok', unpublished = false, fixDelay = 0,
  upgrade = 'new', advice = true, path = null, upgradeDelay = 0 } = {}) {
  const followsNever = id === 'pf-dup'
  const ctx = await browser.newContext({ viewport: { width, height } })
  opened.add(ctx)
  await ctx.addInitScript(() => { try { localStorage.setItem('agentlab_actor', '检查脚本') } catch { /* noop */ } })
  // 升级那几段：问题面板拉高一些，预览一屏看得全（截图也看得清）
  if (id === 'pf-legacy' || id === 'pf-ask') {
    await ctx.addInitScript(() => { try { localStorage.setItem('agentlab.studio.dockHeight', '460') } catch { /* noop */ } })
  }
  const page = await ctx.newPage()
  page.setDefaultTimeout(8000)
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  const json = (route, body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
  // validate：'ok' | 'fail'（校验接口 500，后端断开时和保存一起失败）。检查中途改它，下一次校验就按新的回
  const state = { checks: [], autofixes: [], patches: [], publishes: [], upgrades: [], version: FAKES[id].version, graph: clone(FAKES[id].graph), save, backend, upgrade,
    validate: 'ok' }

  await page.route(/\/api\//, (route) => {
    const req = route.request()
    if (req.method() === 'GET') return route.continue()
    writes.push(`${req.method()} ${new URL(req.url()).pathname}`)
    return json(route, { detail: '检查脚本不写库' }, 409)
  })
  await page.route(/\/api\/workflows\/(validate|variables)$/, (route) => route.continue())
  // 一键升级的两张图：校验按图现算那条建议（真后端怎么说不影响这几段）
  if (id === 'pf-legacy' || id === 'pf-ask') {
    await page.route(/\/api\/workflows\/validate$/, (route) => state.validate === 'fail'
      ? json(route, { detail: '校验服务暂时无法连接' }, 500)
      : json(route, { ok: true, issues: advice ? upgradeAdvice(route.request().postDataJSON()?.graph ?? { nodes: [], edges: [] }) : [] }))
  }
  await page.route(/\/api\/copilot\/upgrade-evidence$/, async (route) => {
    const body = route.request().postDataJSON()
    state.upgrades.push(body)
    if (state.upgrade === 'old') return json(route, { detail: 'Not Found' }, 404)
    if (upgradeDelay) await new Promise((r) => setTimeout(r, upgradeDelay))
    return json(route, upgradeFake(body, { bare: state.upgrade === 'bare' }))
  })
  await page.route(/\/api\/workflows(\?.*)?$/, async (route) => {
    if (route.request().method() !== 'GET') return route.fallback()
    const real = await route.fetch().then((r) => r.json()).catch(() => [])
    return json(route, [...Object.values(FAKES), ...(Array.isArray(real) ? real : [])])
  })
  await page.route(/\/api\/workflows\/pf-[a-z]+(\/.*)?(\?.*)?$/, async (route) => {
    const req = route.request()
    const [, , , wid, sub, v] = new URL(req.url()).pathname.split('/')
    const method = req.method()
    const base = FAKES[wid]
    if (sub === 'versions' && !v) return json(route, [4, 3, 2].map((n) => ({ id: `v${n}`, version: n, note: '', created_at: '2026-09-26T02:00:00Z', published: n === 4 })))
    if (sub === 'versions' && v) return json(route, { id: `v${v}`, version: Number(v), workflow_id: wid, graph: base.graph, graph_hash: 'x', input_fields: [] })
    if (method === 'GET' && !sub) return json(route, wid === id ? { ...base, graph: state.graph, version: state.version } : base)
    if (method === 'PATCH' && !sub && wid === id) {
      const body = req.postDataJSON()
      state.patches.push(body)
      if (state.save === 'fail') return json(route, { detail: '数据库已锁定，请稍后重试' }, 500)
      if (state.save === 'slow') await new Promise((r) => setTimeout(r, 1500))
      state.version += 1
      state.graph = body.graph
      return json(route, { ...base, graph: body.graph, version: state.version, status: 'draft' })
    }
    if (method === 'POST' && sub === 'publish-check') {
      const body = req.postDataJSON()
      // 开发模式的 StrictMode 会把挂载时的 effect 跑两遍（第一遍的请求随即被取消）：
      // 300ms 内一模一样的两次只记一次，数的是「查了几回」
      const key = JSON.stringify(body)
      const last = state.checks.at(-1)
      if (!(last && last.key === key && Date.now() - last.at < 300)) state.checks.push({ ...body, key, at: Date.now() })
      if (state.backend === 'old') return json(route, { detail: 'Not Found' }, 404)
      return json(route, lint(body.graph ?? state.graph, body.level, { followsNever }))
    }
    if (method === 'POST' && sub === 'autofix') {
      const body = req.postDataJSON()
      state.autofixes.push(body)
      if (state.backend !== 'new') return json(route, { detail: 'Not Found' }, 404)
      if (fixDelay) await new Promise((r) => setTimeout(r, fixDelay))
      return json(route, autofix(body, { unpublished, followsNever }))
    }
    if (method === 'POST' && sub === 'publish') {
      const body = req.postDataJSON()
      state.publishes.push(body)
      const r = lint(state.graph, body.level, { followsNever })
      // 真后端的 /publish 回包：问题带 code、不带 fix（fix 只有 publish-check 给）；老后端连 code 也没有
      const issues = state.backend === 'old' ? r.issues.map(({ code: _c, fix: _f, ...rest }) => rest)
        : r.issues.map(({ fix: _f, ...rest }) => rest)
      return json(route, r.ok ? { ok: true, level: body.level, version: state.version, issues } : { ok: false, level: body.level, version: state.version, issues })
    }
    return route.fallback()
  })
  await page.route(/\/api\/conversations(\/.*)?(\?.*)?$/, (route) => {
    const req = route.request()
    if (req.method() === 'GET' && new URL(req.url()).pathname.endsWith('/conversations')) return json(route, [])
    return json(route, { id: 'pf-conv', title: '', kind: 'canvas', archived: false, turn_count: 0, last_question: '', turns: [] })
  })

  // 带参数进来（?upgrade=1）时不等网络静下来：参数没摘掉的话页面会一直重复请求，要让下面的检查说出是哪一条
  await page.goto(`${WEB}${path ?? `/studio/${id}`}`, { waitUntil: path ? 'load' : 'networkidle' })
  await page.waitForFunction((wid) => window.__studio?.getState().workflow?.id === wid, id, { timeout: 15000 })
  await page.waitForTimeout(400)
  return { ctx, page, errors, state }
}

const dialog = (page) => page.locator('[role="dialog"]')
async function openDialog(page) {
  await page.getByRole('button', { name: '发布', exact: true }).click()
  await dialog(page).waitFor()
}
const waitFor = async (page, fn, ms = 5000) => {
  for (let t = 0; t < ms; t += 100) {
    if (await fn()) return true
    await page.waitForTimeout(100)
  }
  return false
}
const submit = (page) => dialog(page).locator('[data-publish-submit]')
const cfgOf = (graph, id) => graph?.nodes?.find((n) => n.id === id)?.data?.config
const shoot = async (page, name, target) => {
  if (!SHOTS) return
  for (const theme of ['light', 'dark']) {
    await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
    await page.waitForTimeout(250)
    await (target ?? page).screenshot({ path: `${SHOTS}/${name}-${theme}.png` }).catch(() => {})
  }
  await page.evaluate(() => document.documentElement.removeAttribute('data-theme'))
}

// ================================================================
await section('打开弹窗就列出问题，每条按修法给控件', async () => {
  const { page, state, errors } = await open()
  await openDialog(page)
  await waitFor(page, async () => (await dialog(page).locator('[data-preflight-issue]').count()) > 0)
  check('一打开就调 publish-check（没点发布）', state.checks.length === 1 && state.publishes.length === 0, JSON.stringify(state.checks.map((c) => c.level)))
  check('……按工作流现在的等级（受管）查，带着画布上的图', state.checks[0]?.level === 'governed' && state.checks[0]?.graph?.nodes?.length === HARD.nodes.length)
  const rows = await dialog(page).locator('[data-preflight-issue]').count()
  check('问题先列出来，一条一行', rows === 6, `${rows} 行`)
  const summary = await dialog(page).locator('[data-preflight-summary]').innerText().catch(() => '')
  check('结论一句：5 处问题将被门禁拦截，另有 1 条提示', summary.includes('5 处问题将被门禁拦截') && summary.includes('1 条提示'), summary)
  check('auto 类有「修复」', await dialog(page).locator('[data-fix-kind="auto"] [data-fix-action="auto"]').count() === 3)
  check('choice 类是选择控件：单选用 radio（两张口径卡）、多选用 checkbox（三个指标）', await dialog(page).locator('[data-fix-choice="contract.metrics_from_missing:done"] input[type="radio"]').count() === 2
    && await dialog(page).locator('[data-fix-choice="contract.required_missing:done"] input[type="checkbox"]').count() === 3)
  check('assist 类有「交给助手」', (await dialog(page).locator('[data-fix-kind="assist"] [data-fix-action="assist"]').innerText()).includes('交给助手'))
  const all = await dialog(page).locator('[data-fix-all]').innerText().catch(() => '')
  check('顶部「一键修复可自动修复的 3 处」', all.includes('一键修复可自动修复的 3 处'), all)
  check('多选不给默认全选：一个都没勾', await dialog(page).locator('[data-fix-choice="contract.required_missing:done"] input:checked').count() === 0)
  check('单选的建议值只标「建议」，不替人选', await dialog(page).locator('[data-fix-choice="contract.metrics_from_missing:done"] input:checked').count() === 0
    && (await dialog(page).locator('[data-fix-choice="contract.metrics_from_missing:done"]').innerText()).includes('建议'))
  check('发布按钮照旧能点（不修也能发，门禁照旧拦）', await submit(page).isEnabled())
  await shoot(page, 'publish-dialog-issues', dialog(page))
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
})

await section('auto：预览逐项写「节点 · 字段：原值 → 新值」，应用并重新检查', async () => {
  const { page, state } = await open({ id: 'pf-hard', unpublished: true })
  await openDialog(page)
  await dialog(page).locator('[data-fix-all]').waitFor()
  await dialog(page).locator('[data-fix-all]').click()
  await dialog(page).locator('[data-fix-preview="ready"]').waitFor()
  const req = state.autofixes[0]
  check('一键修复请求：apply 是三处 auto 的 id，不带 assist', JSON.stringify([...(req?.apply ?? [])].sort()) === JSON.stringify(
    ['contract.strict_off:done', 'governed.agent_approval_never:fetch', 'governed.subgraph_unpinned:sub']) && !req?.assist, JSON.stringify(req?.apply))
  const lines = await dialog(page).locator('[data-fix-change]').allInnerTexts()
  const approval = lines.find((l) => l.includes('取数')) ?? ''
  check('预览：「取数」· 审批策略：全部无需审批 → 仅危险工具需要审批', /「取数」\s*·\s*审批策略：\s*全部无需审批\s*仅危险工具需要审批/.test(approval.replace(/\n.*$/s, '')), approval)
  const strict = lines.find((l) => l.includes('严格模式')) ?? ''
  check('预览：契约里的键用界面叫法（出具契约 · 严格模式：否 → 是）', /出具契约 · 严格模式：\s*否\s*是/.test(strict), strict)
  const rejected = await dialog(page).locator('[data-fix-rejected]').innerText().catch(() => '')
  check('没采用的写出原因', rejected.includes('无法自动固定版本') && rejected.includes('固定到上游当前的发布版本'), rejected)
  check('预览期间发布按钮不可用（先应用或放弃）', await submit(page).isDisabled())
  check('预览不改画布、不保存', state.patches.length === 0
    && await page.evaluate(() => window.__studio.getState().nodes.find((n) => n.id === 'fetch').data.config.approval) === 'never')
  await shoot(page, 'publish-dialog-preview', dialog(page))
  await dialog(page).locator('[data-fix-apply]').click()
  await waitFor(page, async () => state.patches.length === 1 && state.checks.length >= 2 && await dialog(page).locator('[data-fix-preview]').count() === 0)
  const saved = state.patches[0]?.graph
  check('应用 = 走保存接口存草稿：载荷里是修好的图', cfgOf(saved, 'fetch')?.approval === 'dangerous' && cfgOf(saved, 'done')?.contract?.strict === true
    && !cfgOf(saved, 'sub')?.workflow_version, JSON.stringify({ a: cfgOf(saved, 'fetch')?.approval, s: cfgOf(saved, 'done')?.contract?.strict }))
  check('……没采用的那处不在载荷里；节点和连线一个没少', saved?.nodes?.length === HARD.nodes.length && saved?.edges?.length === HARD.edges.length)
  check('……写了版本说明', /^发布前修复：/.test(state.patches[0]?.note ?? ''), state.patches[0]?.note)
  check('存完再查一遍 publish-check', state.checks.length === 2 && cfgOf(state.checks[1]?.graph, 'fetch')?.approval === 'dangerous')
  const st = await page.evaluate(() => { const s = window.__studio.getState(); return { dirty: s.dirty, v: s.workflow.version, past: s.past.length, label: s.past.at(-1)?.label } })
  check('画布落上修复、存完不再「未保存」，撤销栈里有这一步', !st.dirty && st.v === 4 && st.past >= 1 && /发布前修复/.test(st.label ?? ''), JSON.stringify(st))
  const rows = await dialog(page).locator('[data-preflight-issue]').count()
  check('重新检查后清单跟着变（剩 4 处错，strict 的提示没了）', rows === 4
    && await dialog(page).locator('[data-preflight-issue="contract.strict_off"]').count() === 0, `${rows} 行`)
  check('整个过程没有发布请求', state.publishes.length === 0)
})

await section('auto：修完没有错了，发布按钮才可用；发布只在人点之后发生', async () => {
  const { page, state } = await open({ id: 'pf-easy' })
  await openDialog(page)
  await dialog(page).locator('[data-fix-all]').waitFor()
  check('草稿工作流默认按「已发布」查', state.checks[0]?.level === 'published')
  await dialog(page).locator('[role="radio"]:has-text("受管")').click()
  await waitFor(page, async () => state.checks.length === 2)
  check('换成受管就按受管重查', state.checks[1]?.level === 'governed')
  await dialog(page).locator('[data-fix-all]').waitFor()
  await dialog(page).locator('[data-fix-all]').click()
  await dialog(page).locator('[data-fix-preview="ready"]').waitFor()
  const after = await dialog(page).locator('[data-fix-after-check]').innerText().catch(() => '')
  check('预览写明应用后可通过门禁', after.includes('应用后可通过门禁'), after)
  check('预览期间发布不可用', await submit(page).isDisabled())
  await dialog(page).locator('[data-fix-apply]').click()
  await waitFor(page, async () => state.checks.length === 3 && await dialog(page).locator('[data-fix-preview]').count() === 0)
  const summary = await dialog(page).locator('[data-preflight-summary]').innerText().catch(() => '')
  check('重新检查通过', summary.includes('发布前检查通过'), summary)
  check('发布按钮变为可用', await submit(page).isEnabled())
  check('到这一步还没有任何发布请求', state.publishes.length === 0)
  await submit(page).click()
  await waitFor(page, async () => state.publishes.length === 1)
  check('人点了「发布为受管」才发出一次发布', state.publishes.length === 1 && state.publishes[0].level === 'governed', JSON.stringify(state.publishes))
})

await section('choice：没选之前不能应用；选了之后载荷里是选的值', async () => {
  const { page, state } = await open()
  await openDialog(page)
  const req = dialog(page).locator('[data-fix-choice="contract.required_missing:done"]')
  await req.waitFor()
  check('没选时「预览」不可用，并说先选', await req.locator('[data-fix-action="choice"]').isDisabled() && (await req.innerText()).includes('请先选择再预览'))
  await req.locator('label:has-text("gmv") input').check()
  await req.locator('label:has-text("orders") input').check()
  check('选了之后可以预览', await req.locator('[data-fix-action="choice"]').isEnabled())
  await req.locator('[data-fix-action="choice"]').click()
  await dialog(page).locator('[data-fix-preview="ready"]').waitFor()
  const body = state.autofixes.at(-1)
  check('请求：apply 只有这一项，choices 是选的两个', JSON.stringify(body?.apply) === JSON.stringify(['contract.required_missing:done'])
    && JSON.stringify(body?.choices?.['contract.required_missing:done']) === JSON.stringify(['gmv', 'orders']), JSON.stringify(body))
  const change = await dialog(page).locator('[data-fix-change]').first().innerText()
  check('预览：出具契约 · 必需指标：（空）→ gmv、orders', /出具契约 · 必需指标：\s*（空）\s*gmv、orders/.test(change), change)
  await dialog(page).locator('[data-fix-apply]').click()
  await waitFor(page, async () => state.patches.length === 1)
  check('保存载荷里 required 是选的值', JSON.stringify(cfgOf(state.patches[0]?.graph, 'done')?.contract?.required) === JSON.stringify(['gmv', 'orders']),
    JSON.stringify(cfgOf(state.patches[0]?.graph, 'done')?.contract))

  // 单选：选「补充口径」
  const from = dialog(page).locator('[data-fix-choice="contract.metrics_from_missing:done"]')
  await waitFor(page, async () => (await from.count()) === 1 && await from.locator('[data-fix-action="choice"]').isVisible())
  await from.locator('label:has-text("补充口径") input').check()
  await from.locator('[data-fix-action="choice"]').click()
  await dialog(page).locator('[data-fix-preview="ready"]').waitFor()
  check('单选：choices 里是选中的那一个值', state.autofixes.at(-1)?.choices?.['contract.metrics_from_missing:done'] === 'caliber2', JSON.stringify(state.autofixes.at(-1)?.choices))
  // 改动的值是节点 id（caliber2）：预览里写节点名，和选的时候看到的是同一个名字
  const fromLine = await dialog(page).locator('[data-fix-change="contract.metrics_from_missing:done"]').first().innerText().catch(() => '')
  check('预览写节点名、不写 id：出具契约 · 指标来自：（空）→「补充口径」', /出具契约 · 指标来自：\s*（空）\s*「补充口径」/.test(fromLine)
    && !fromLine.includes('caliber2'), fromLine)
})

await section('assist：Copilot 的改动先预览、它的问题原样摆出来，不确认就不保存', async () => {
  const { page, state } = await open()
  await openDialog(page)
  await dialog(page).locator('[data-fix-action="assist"]').waitFor()
  await dialog(page).locator('[data-fix-action="assist"]').click()
  await dialog(page).locator('[data-fix-preview="ready"]').waitFor()
  const body = state.autofixes.at(-1)
  check('请求带 assist: true', body?.assist === true, JSON.stringify(body))
  const change = await dialog(page).locator('[data-fix-change]').first().innerText().catch(() => '')
  check('Copilot 的改动逐项预览', change.includes('复核团队') && /4\s*2/.test(change), change)
  const q = await dialog(page).locator('[data-fix-questions]').innerText().catch(() => '')
  check('它提的问题原样显示', q.includes('复核团队要换成哪一个固定编排的 Agent？它现在只有一个成员 checker，保留它的职责吗？'), q)
  check('它的总结也摆出来', (await dialog(page).locator('[data-fix-assist]').innerText()).includes('按规矩没动它'))
  await shoot(page, 'publish-dialog-assist', dialog(page))
  await dialog(page).getByRole('button', { name: '放弃', exact: true }).click()
  await page.waitForTimeout(300)
  check('放弃：不保存、画布不动、预览收起', state.patches.length === 0 && await dialog(page).locator('[data-fix-preview]').count() === 0
    && await page.evaluate(() => window.__studio.getState().nodes.find((n) => n.id === 'team').data.config.max_rounds) === 4)
  check('放弃后发布按钮恢复可用', await submit(page).isEnabled())
  check('没有发布请求', state.publishes.length === 0)
})

await section('保存失败、接口不存在、画布改过：都有明确状态', async () => {
  const bad = await open({ id: 'pf-easy', save: 'fail' })
  await openDialog(bad.page)
  await dialog(bad.page).locator('[data-fix-all]').click()
  await dialog(bad.page).locator('[data-fix-apply]').click()
  await dialog(bad.page).locator('[data-fix-save-failed]').waitFor()
  const msg = await dialog(bad.page).locator('[data-fix-save-failed]').innerText()
  check('保存失败：说清没存上、修复已在画布上', msg.includes('草稿保存失败') && msg.includes('数据库已锁定') && msg.includes('保存成功前无法发布'), msg)
  check('……发布按钮不可用（不能把没修的那一版发出去）', await submit(bad.page).isDisabled())
  check('……不重查', bad.state.checks.length === 1)
  bad.state.save = 'ok'
  await dialog(bad.page).getByRole('button', { name: '重试保存' }).click()
  await waitFor(bad.page, async () => bad.state.checks.length === 2)
  // 重查的请求发出去了，按钮的可用状态要等答复回来、界面更新之后才变：并行跑的时候机器挤，直接读会抢在前面
  await waitFor(bad.page, async () => submit(bad.page).isEnabled())
  check('重试保存成功后重新检查、发布可用', bad.state.patches.length === 2 && await submit(bad.page).isEnabled())

  const nofix = await open({ backend: 'nofix' })
  await openDialog(nofix.page)
  await dialog(nofix.page).locator('[data-fix-all]').click()
  await dialog(nofix.page).locator('[data-fix-preview="unsupported"]').waitFor()
  check('autofix 不存在：说当前服务版本不支持自动修复', (await dialog(nofix.page).locator('[data-fix-preview]').innerText()).includes('当前服务版本不支持自动修复'))
  check('……发布按钮照旧可用', await submit(nofix.page).isEnabled())

  const moved = await open({ id: 'pf-easy' })
  await openDialog(moved.page)
  await dialog(moved.page).locator('[data-fix-all]').click()
  await dialog(moved.page).locator('[data-fix-apply]').waitFor()
  await moved.page.evaluate(() => { const s = window.__studio.getState(); s.updateNode('fetch', { label: '取数2' }) })
  await dialog(moved.page).locator('[data-fix-apply]').click()
  await moved.page.waitForTimeout(300)
  check('预览之后画布内容又改过：不套用、说要重查', moved.state.patches.length === 0
    && (await dialog(moved.page).locator('[data-fix-preview]').innerText()).includes('此预览已失效'))
})

await section('正式运行编辑锁定时不给修复按钮', async () => {
  const { page } = await open()
  await page.evaluate(() => {
    const s = window.__studio.getState()
    window.__studio.setState({ runPhase: 'running', trace: { ...s.trace, runClass: 'formal' } })
  })
  await openDialog(page)
  await dialog(page).locator('[data-preflight-issue]').first().waitFor()
  check('问题照样列出来', await dialog(page).locator('[data-preflight-issue]').count() === 6)
  check('没有任何修复控件', await dialog(page).locator('[data-fix-action], [data-fix-all], [data-fix-choice]').count() === 0)
  check('写明为什么现在不能修', (await dialog(page).locator('[data-preflight-locked]').innerText()).includes('正式运行进行中'))
})

await section('老后端（两个接口 404）：界面和现在一样', async () => {
  const { page, state, errors } = await open({ backend: 'old' })
  await openDialog(page)
  await waitFor(page, async () => state.checks.length === 1)
  await page.waitForTimeout(300)
  check('打开时不列问题、不给修复控件', await dialog(page).locator('[data-preflight-issue], [data-fix-action], [data-fix-all]').count() === 0)
  check('只一句安静的说明：点发布时仍会执行门禁检查', (await dialog(page).locator('[data-preflight-unsupported]').innerText().catch(() => '')).includes('仍会执行门禁检查'))
  check('发布按钮可用', await submit(page).isEnabled())
  await submit(page).click()
  await waitFor(page, async () => (await dialog(page).locator('[data-preflight-issue]').count()) > 0)
  const summary = await dialog(page).locator('[data-preflight-summary]').innerText().catch(() => '')
  check('点了发布被拦：照旧列出门禁拦截的问题', summary.includes('门禁拦截了 5 处问题') && await dialog(page).locator('[data-preflight-issue]').count() === 6, summary)
  check('……仍然没有修复控件', await dialog(page).locator('[data-fix-action], [data-fix-all]').count() === 0)
  await dialog(page).locator('[data-preflight-issue]').filter({ hasText: '取数' }).locator('button').first().click()
  await page.waitForTimeout(300)
  check('点一条关掉弹窗、定位到节点（原有行为）', await dialog(page).count() === 0
    && await page.evaluate(() => window.__studio.getState().selectedId) === 'fetch')
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
})

await section('问题面板的发布前检查：选档、同一套修法、点一条定位', async () => {
  const { page, state } = await open({ id: 'pf-easy' })
  await page.locator('.react-flow__pane').click({ position: { x: 30, y: 30 } })
  await page.keyboard.press('Alt+KeyP')
  await page.locator('#dock-problems').waitFor()
  await page.locator('[data-problems-mode="publish"]').click()
  await page.locator('[data-preflight="panel"]').waitFor().catch(() => {})
  await waitFor(page, async () => state.checks.length === 1)
  check('切到「发布前检查」就按当前等级查一遍（草稿 → 已发布）', await page.locator('[data-preflight="panel"]').count() === 1
    && state.checks[0]?.level === 'published', JSON.stringify(state.checks.map((c) => c.level)))
  await page.locator('[data-preflight="panel"] [role="radio"]:has-text("受管")').click()
  await waitFor(page, async () => state.checks.length === 2)
  check('选「受管」重查', state.checks[1]?.level === 'governed')
  await page.locator('[data-preflight="panel"] [data-fix-all="2"]').waitFor()
  check('同一套修复控件', await page.locator('[data-preflight="panel"] [data-fix-action="auto"]').count() === 2)
  await shoot(page, 'problems-preflight', page.locator('section[aria-label="问题"]'))
  await page.locator('[data-preflight="panel"] [data-preflight-issue]').filter({ hasText: '取数' }).locator('button').first().click()
  await page.waitForTimeout(300)
  check('点一条定位到节点', await page.evaluate(() => window.__studio.getState().selectedId) === 'fetch')
  // 画布改过：结论可能过时
  await page.evaluate(() => { const s = window.__studio.getState(); s.updateNode('caliber', { label: '周报口径2' }) })
  await page.waitForSelector('[data-preflight="panel"] [data-preflight-stale]', { timeout: 6000 }).catch(() => {})
  check('查完画布又改过：提示结果可能过时', await page.locator('[data-preflight="panel"] [data-preflight-stale]').count() === 1)
  await page.locator('[data-preflight="panel"]').getByRole('button', { name: '重新检查' }).click()
  await waitFor(page, async () => state.checks.length === 3)
  await page.locator('[data-preflight="panel"] [data-fix-all]').click()
  await page.locator('[data-preflight="panel"] [data-fix-apply]').click()
  // 重查的请求发出去之后，回包画上去之前清单还是上一份：等预览收起（它在重查回来之后才收）
  await waitFor(page, async () => state.patches.length === 1 && state.checks.length === 4
    && await page.locator('[data-preflight="panel"] [data-fix-preview]').count() === 0)
  check('面板里应用：存草稿（带上画布上别的未保存改动）再重查', cfgOf(state.patches[0]?.graph, 'fetch')?.approval === 'dangerous'
    && state.patches[0]?.graph?.nodes?.find((n) => n.id === 'caliber')?.data?.label === '周报口径2')
  const summary = await page.locator('[data-preflight="panel"] [data-preflight-summary]').innerText().catch(() => '')
  check('……重查通过', summary.includes('发布前检查通过'), summary)
  check('面板里也不发布', state.publishes.length === 0)
  await page.locator('[data-problems-mode="lint"]').click()
  check('切回「校验」照旧是原来的问题清单', await page.locator('[data-preflight="panel"]').count() === 0)
})

await section('SQL 检查（数据目录阶段 4B）：发布前检查写中文规则名，点一条定位到调用工具参数里的 SQL', async () => {
  const { page, state, errors } = await open({ id: 'pf-sql' })
  await openDialog(page)
  const row = dialog(page).locator('[data-preflight-issue="fanout_sum"]')
  await row.waitFor()
  const text = await row.innerText()
  check('发布弹窗：SQL 检查的问题写节点名和中文规则名，不露规则编号', text.includes('「订单汇总」') && text.includes('一对多关联后重复计算：')
    && text.includes('会重复计算') && !/fanout_sum|args\.sql/.test(text), text)
  // 错误级的 SQL 检查在已发布档只提醒（设计如此），但不能和「没指定模型」这类提示一个样：级别标识和证据面板同一个
  // （SqlLevelBadge：图标 + 「错误」、失败色），并说清已发布级别只提醒、受管级别会拦
  const badge = row.locator('[data-sql-level]')
  const badgeLook = await badge.evaluate((el) => {
    const probe = document.createElement('span')
    probe.style.color = 'var(--st-failed)'
    document.body.append(probe)
    const failed = getComputedStyle(probe).color
    probe.remove()
    return { level: el.getAttribute('data-sql-level'), text: el.innerText.trim(), color: getComputedStyle(el).color, failed, icon: !!el.querySelector('svg') }
  }).catch(() => null)
  check('……带证据面板同一个级别标识：「错误」、失败色、带图标', badgeLook?.level === 'error' && badgeLook.text === '错误'
    && badgeLook.color === badgeLook.failed && badgeLook.icon, JSON.stringify(badgeLook))
  check('……写明这是 SQL 检查发现的错误级问题：已发布级别只提醒、不拦，受管级别会拦下',
    (await row.locator('[data-preflight-sql-gate="reminded"]').innerText().catch(() => '')) === 'SQL 检查发现的错误级问题：已发布级别只提醒、不拦发布；受管级别会拦下')
  check('……结论那句话里点出来，不和别的提示混成一个数', (await dialog(page).locator('[data-preflight-sql-errors="1"]').innerText().catch(() => ''))
    .includes('其中 1 处是 SQL 检查发现的错误级问题'))
  await dialog(page).locator('[role="radio"]:has-text("受管")').click()
  await waitFor(page, async () => (await row.locator('[data-preflight-sql-gate="blocked"]').count()) === 1)
  check('换成受管：同一条写「受管级别会拦下发布」，行首是错误', (await row.locator('[data-preflight-sql-gate]').innerText()).includes('受管级别会拦下发布')
    && await row.locator('svg[aria-label="错误"]').count() === 1 && await dialog(page).locator('[data-preflight-sql-errors]').count() === 0)
  await dialog(page).locator('[role="radio"]:has-text("已发布")').click()
  await waitFor(page, async () => (await row.locator('[data-preflight-sql-gate="reminded"]').count()) === 1)
  await shoot(page, 'preflight-sqlcheck', dialog(page))
  await row.locator('button').first().click()
  const where = () => page.evaluate(() => {
    const box = document.querySelector('[data-inspector-sheet] [data-field="args"] textarea')
    const line = box ? box.value.slice(0, box.value.indexOf('"sql"')).split('\n').length - 1 : -1
    const height = box ? parseFloat(getComputedStyle(box).lineHeight) || 16 : 16
    return { sel: window.__studio.getState().selectedId, field: !!box, line, top: box?.scrollTop ?? -1, visible: box ? box.scrollTop <= line * height : false }
  })
  check('点一条：弹窗关掉，选中节点并翻到「参数」，框内滚到 sql 那一行', await waitFor(page, async () => {
    const w = await where()
    return w.sel === 'sum' && w.field && w.line > 0 && w.visible
  }) && await dialog(page).count() === 0, JSON.stringify(await where()))
  // 问题面板的发布前检查：同一个落点
  await page.evaluate(() => window.__studio.getState().select(null))
  await page.locator('.react-flow__pane').click({ position: { x: 30, y: 30 } })
  await page.keyboard.press('Alt+KeyP')
  await page.locator('#dock-problems').waitFor()
  await page.locator('[data-problems-mode="publish"]').click()
  const panelRow = page.locator('[data-preflight="panel"] [data-preflight-issue="fanout_sum"]')
  await panelRow.waitFor()
  check('问题面板的发布前检查：同样写中文规则名', (await panelRow.innerText()).includes('一对多关联后重复计算：'))
  await panelRow.locator('button').first().click()
  check('……点一条同样落到「参数」里的 SQL', await waitFor(page, async () => {
    const w = await where()
    return w.sel === 'sum' && w.field && w.visible
  }), JSON.stringify(await where()))
  check('没有发布、没有保存', state.publishes.length === 0 && state.patches.length === 0)
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
})

await section('同一个修复只画一次：validate 和门禁各报一条、几条问题共用一个图级修复', async () => {
  const { page, state, errors } = await open({ id: 'pf-dup' })
  await openDialog(page)
  await waitFor(page, async () => (await dialog(page).locator('[data-preflight-issue]').count()) > 0)
  const rows = await dialog(page).locator('[data-preflight-issue]').count()
  check('同 code、同节点的两条合成一行', rows === 3
    && await dialog(page).locator('[data-preflight-issue="contract.metrics_from_missing"]').count() === 1, `${rows} 行`)
  const summary = await dialog(page).locator('[data-preflight-summary]').innerText().catch(() => '')
  check('……结论按一处算：3 处问题将被门禁拦截', summary.includes('3 处问题将被门禁拦截'), summary)
  const choice = dialog(page).locator('[data-fix-choice="contract.metrics_from_missing:done"]')
  check('契约那一处只有一组选项、一个「预览」', await choice.count() === 1 && await choice.locator('[data-fix-action]').count() === 1)
  const follow = dialog(page).locator('[data-preflight-issue="governed.default_approval_never"]')
  check('两个节点共用的图级修复只画一次，挂在第一条上', await follow.count() === 2
    && await follow.nth(0).locator('[data-fix-action="auto"]').count() === 1 && await follow.nth(1).locator('[data-fix-action]').count() === 0
    && await dialog(page).locator('[data-fix-action="auto"]').count() === 1)
  check('一键修复算一处', (await dialog(page).locator('[data-fix-all]').getAttribute('data-fix-all').catch(() => '')) === '1')
  await dialog(page).locator('[data-fix-all]').click()
  await dialog(page).locator('[data-fix-preview="ready"]').waitFor()
  const line = await dialog(page).locator('[data-fix-change]').first().innerText().catch(() => '')
  check('图级改动的预览：「整个工作流」 · 工作流默认设置 · 审批策略：全部无需审批 → 仅危险工具需要审批',
    /「整个工作流」\s*·\s*工作流默认设置 · 审批策略：\s*全部无需审批\s*仅危险工具需要审批/.test(line), line)
  await dialog(page).getByRole('button', { name: '放弃', exact: true }).click()
  // 真点了发布被拦：/publish 的回包只有 code 没有 fix。图级修复按 code 认回来，照样只画一次
  await submit(page).click()
  await waitFor(page, async () => state.publishes.length === 1
    && (await dialog(page).locator('[data-preflight-summary]').innerText().catch(() => '')).includes('门禁拦截了'))
  const gateSummary = await dialog(page).locator('[data-preflight-summary]').innerText().catch(() => '')
  check('被拦之后：照样合成一行（门禁拦截了 3 处问题）', gateSummary.includes('门禁拦截了 3 处问题')
    && await dialog(page).locator('[data-preflight-issue]').count() === 3, gateSummary)
  check('……图级修复（回包里没有 fix）照样挂在第一条上，只画一次', await follow.nth(0).locator('[data-fix-action="auto"]').count() === 1
    && await dialog(page).locator('[data-fix-action="auto"]').count() === 1)
  check('……契约那一处照样只有一组选项', await choice.count() === 1)
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
})

await section('图级问题：契约骨架按键写节点名；发布被拦之后修复控件还在', async () => {
  const { page, state, errors } = await open({ id: 'pf-bare' })
  await openDialog(page)
  const row = dialog(page).locator('[data-preflight-issue="governed.no_contract"]')
  await row.waitFor()
  check('打开时：「没有带契约的出口」旁边有生成契约骨架的选项', await row.locator('[data-fix-choice="governed.no_contract:done"]').count() === 1)
  await row.locator('label:has-text("gmv") input').check()
  await row.locator('[data-fix-action="choice"]').click()
  await dialog(page).locator('[data-fix-preview="ready"]').waitFor()
  const change = await dialog(page).locator('[data-fix-change]').first().innerText().catch(() => '')
  const after = await dialog(page).locator('[data-fix-change] [data-fix-after]').first().innerText().catch(() => '')
  const lines = after.split('\n').map((l) => l.trim()).filter(Boolean)
  check('契约骨架一键一行，键名用界面叫法、节点写名字', /「成果」\s*·\s*出具契约：\s*（空）/.test(change)
    && JSON.stringify(lines) === JSON.stringify(['指标来自：「周报口径」', '报告来自：「报告撰写」', '必需指标：gmv', '严格模式：是']), lines.join(' | '))
  check('……不露节点 id、英文键名、JSON', !/caliber|write|metrics_from|report_from|required|strict|[{}"]/.test(after), after)
  await dialog(page).getByRole('button', { name: '放弃', exact: true }).click()
  await submit(page).click()
  await waitFor(page, async () => state.publishes.length === 1
    && (await dialog(page).locator('[data-preflight-summary]').innerText().catch(() => '')).includes('门禁拦截了'))
  const gate = dialog(page).locator('[data-preflight-issue="governed.no_contract"]')
  check('发布被拦（回包只有 code 没有 fix）：图级问题旁边照样有生成契约骨架的选项', state.publishes.length === 1
    && await gate.locator('[data-fix-choice="governed.no_contract:done"]').count() === 1)
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
})

await section('保存进行中不能换等级；存失败之后换了等级也还能重试保存', async () => {
  const slow = await open({ id: 'pf-easy', save: 'slow' })
  await openDialog(slow.page)
  await dialog(slow.page).locator('[data-fix-all]').click()
  await dialog(slow.page).locator('[data-fix-apply]').click()
  await waitFor(slow.page, async () => slow.state.patches.length === 1)
  const radios = dialog(slow.page).locator('[role="radio"]')
  check('弹窗：保存进行中两个等级都不可点', await radios.nth(0).isDisabled() && await radios.nth(1).isDisabled())
  await radios.nth(1).click({ force: true, timeout: 1000 }).catch(() => {})
  await slow.page.waitForTimeout(200)
  check('……点了也不换档、不按别的档重查', slow.state.checks.every((c) => c.level === 'published')
    && await radios.nth(0).getAttribute('aria-checked') === 'true')
  await waitFor(slow.page, async () => slow.state.checks.length === 2 && await radios.nth(1).isEnabled(), 6000)
  check('……存完、重查完，等级又能选', await radios.nth(1).isEnabled())

  // 问题面板：同一条
  const pane = await open({ id: 'pf-easy', save: 'slow' })
  await pane.page.locator('.react-flow__pane').click({ position: { x: 30, y: 30 } })
  await pane.page.keyboard.press('Alt+KeyP')
  await pane.page.locator('[data-problems-mode="publish"]').click()
  const panel = pane.page.locator('[data-preflight="panel"]')
  await panel.locator('[data-fix-all]').click()
  await panel.locator('[data-fix-apply]').click()
  await waitFor(pane.page, async () => pane.state.patches.length === 1)
  const levels = panel.locator('[role="radio"]')
  check('问题面板：保存进行中两个等级都不可点', await levels.nth(0).isDisabled() && await levels.nth(1).isDisabled())
  await levels.nth(1).click({ force: true, timeout: 1000 }).catch(() => {})
  await pane.page.waitForTimeout(200)
  check('……点了也不换档', pane.state.checks.every((c) => c.level === 'published') && await levels.nth(0).getAttribute('aria-checked') === 'true')

  const bad = await open({ id: 'pf-easy', save: 'fail' })
  await openDialog(bad.page)
  await dialog(bad.page).locator('[data-fix-all]').click()
  await dialog(bad.page).locator('[data-fix-apply]').click()
  await dialog(bad.page).locator('[data-fix-save-failed]').waitFor()
  await dialog(bad.page).locator('[role="radio"]:has-text("受管")').click()
  await waitFor(bad.page, async () => bad.state.checks.length === 2)
  await bad.page.waitForTimeout(250)
  check('存失败之后换了等级：说明和「重试保存」都还在', await dialog(bad.page).locator('[data-fix-save-failed]').count() === 1
    && (await dialog(bad.page).locator('[data-fix-save-failed]').innerText().catch(() => '')).includes('草稿保存失败')
    && await dialog(bad.page).getByRole('button', { name: '重试保存' }).count() === 1)
  check('……发布照旧不可用，说的是修复尚未保存', await submit(bad.page).isDisabled()
    && await submit(bad.page).getAttribute('title') === '修复尚未保存', await submit(bad.page).getAttribute('title'))
  bad.state.save = 'ok'
  await dialog(bad.page).getByRole('button', { name: '重试保存' }).click()
  await waitFor(bad.page, async () => bad.state.checks.length === 3 && await submit(bad.page).isEnabled())
  check('……重试保存：存上、按换过的等级重查、发布可用', bad.state.patches.length === 2 && bad.state.checks[2]?.level === 'governed'
    && await submit(bad.page).isEnabled() && await dialog(bad.page).locator('[data-fix-save-failed]').count() === 0)
})

await section('保存和页面自己的保存同一套规矩：回滚说明保留、画布锁着不存', async () => {
  const { page, state } = await open({ id: 'pf-easy' })
  // 刚从版本历史恢复了旧版本：页面自己的保存会把「恢复到 v2」写进版本说明
  await page.evaluate(() => window.__studio.setState({ pendingNote: '恢复到 v2' }))
  await openDialog(page)
  await dialog(page).locator('[data-fix-all]').click()
  await dialog(page).locator('[data-fix-apply]').click()
  await waitFor(page, async () => state.patches.length === 1)
  check('版本说明保留「恢复到 v2」，后面接着写发布前修复', /^恢复到 v2；发布前修复：/.test(state.patches[0]?.note ?? ''), state.patches[0]?.note)

  const bad = await open({ id: 'pf-easy', save: 'fail' })
  await openDialog(bad.page)
  await dialog(bad.page).locator('[data-fix-all]').click()
  await dialog(bad.page).locator('[data-fix-apply]').click()
  await dialog(bad.page).locator('[data-fix-save-failed]').waitFor()
  // 存失败之后正式运行开始了：画布锁着，这时重试保存不能 PATCH
  await bad.page.evaluate(() => {
    const s = window.__studio.getState()
    window.__studio.setState({ runPhase: 'running', trace: { ...s.trace, runClass: 'formal' } })
  })
  bad.state.save = 'ok'
  await dialog(bad.page).getByRole('button', { name: '重试保存' }).click()
  await bad.page.waitForTimeout(400)
  check('画布锁着（正式运行进行中）时重试保存：不存', bad.state.patches.length === 1, `${bad.state.patches.length} 次保存`)
  const why = await dialog(bad.page).locator('[data-fix-save-failed]').innerText().catch(() => '')
  check('……说清为什么没存，「重试保存」还在', why.includes('正式运行进行中')
    && await dialog(bad.page).getByRole('button', { name: '重试保存' }).count() === 1, why)
  check('……发布照旧不可用', await submit(bad.page).isDisabled())
  await bad.page.evaluate(() => window.__studio.setState({ runPhase: 'idle' }))
  await dialog(bad.page).getByRole('button', { name: '重试保存' }).click()
  await waitFor(bad.page, async () => bad.state.patches.length === 2 && bad.state.checks.length === 2)
  check('锁解开之后重试保存：存上、重查', bad.state.patches.length === 2 && bad.state.checks.length === 2)
})

await section('全图默认不会被画布弄丢：保存、发布前检查、修复都带着 defaults', async () => {
  const { ctx, page, errors, state } = await open({ id: 'pf-defs' })
  const first = await page.evaluate(() => {
    const s = window.__studio.getState()
    const n = s.nodes.find((x) => x.data.nodeType !== 'input') ?? s.nodes[0]
    s.updateNode(n.id, { label: `${n.data.label}·改` })
    return n.id
  })
  await page.evaluate(() => window.__studio.getState().save('检查脚本：改个名字'))
  await page.waitForTimeout(300)
  const saved = state.patches.at(-1)?.graph
  check('改了一个节点再保存：全图默认原样带着（以前整个被抹掉，审批 always 悄悄放宽）',
    saved?.defaults?.approval === 'always' && saved?.defaults?.model === 'demo-model',
    JSON.stringify(saved?.defaults ?? null))
  await openDialog(page)
  const sent = state.checks.at(-1)?.graph
  check('发布前检查发出去的图带着全图默认', sent?.defaults?.approval === 'always', JSON.stringify(sent?.defaults ?? null))
  await page.keyboard.press('Escape')
  const dirty = await page.evaluate(() => {
    const s = window.__studio.getState()
    const g = { nodes: s.nodes.map((n) => ({ id: n.id, type: n.data.nodeType, position: n.position, data: { label: n.data.label, config: n.data.config } })),
      edges: s.edges.map((e) => ({ id: e.id, source: e.source, target: e.target, sourceHandle: e.sourceHandle ?? null, label: '' })),
      defaults: { approval: 'dangerous', model: 'demo-model' } }
    s.applyFixes(g, '发布前修复：全图默认')
    return window.__studio.getState().dirty
  })
  check('修复只改了全图默认：画布照样算有改动', dirty === true)
  await page.evaluate(() => window.__studio.getState().save('检查脚本：修复'))
  await page.waitForTimeout(300)
  const fixed = state.patches.at(-1)?.graph
  check('修复改的全图默认存了进去', fixed?.defaults?.approval === 'dangerous' && fixed?.defaults?.model === 'demo-model',
    JSON.stringify(fixed?.defaults ?? null))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await ctx.close()
  void first
})

await section('choice 里选「交给 Copilot」：请求带 assist、等待时说 Copilot 在修；id 叫 copilot 的报告只是普通候选', async () => {
  const { page, state, errors } = await open({ id: 'pf-handoff', fixDelay: 1200 })
  await openDialog(page)
  const g4 = dialog(page).locator('[data-fix-choice="governed.caliber_compute_input:calc"]')
  const g1 = dialog(page).locator('[data-fix-choice="governed.exit_text_source:done"]')
  await g4.waitFor()
  check('两个 choice 都画出来了', await g1.count() === 1)
  check('没选之前，按钮不是「交给助手」的样子', await g4.locator('[data-fix-handoff]').count() === 0)
  // 问句（「它负责取数，还是负责计算？」）里也有「负责计算」：带上冒号，只认候选那一项
  await g4.getByText('负责计算：', { exact: false }).click()
  const button = g4.locator('[data-fix-action="choice"]')
  check('选了「交给助手」那一项，按钮换成「交给助手」', await button.getAttribute('data-fix-handoff') === ''
    && (await button.innerText()).includes('交给助手'), await button.innerText())
  await button.click()
  const loading = dialog(page).locator('[data-fix-preview="loading"]')
  await loading.waitFor()
  check('等待时写「助手正在尝试修复」，不是「正在生成修复预览」', (await loading.innerText()).includes('助手正在尝试修复'), await loading.innerText())
  const body = state.autofixes.at(-1)
  check('请求带 assist: true，选的值原样带上', body?.assist === true && body?.choices?.['governed.caliber_compute_input:calc'] === 'copilot'
    && JSON.stringify(body?.apply) === JSON.stringify(['governed.caliber_compute_input:calc']), JSON.stringify(body))
  await dialog(page).locator('[data-fix-preview="ready"], [data-fix-preview]:not([data-fix-preview="loading"])').first().waitFor()
  check('助手的总结摆出来了', await waitFor(page, async () => (await dialog(page).locator('[data-fix-assist]').count()) > 0))
  await shoot(page, 'publish-dialog-handoff', dialog(page))

  // 另一个 choice：唯一的候选是 id 叫 copilot 的报告撰写节点——普通候选，不交给助手
  const { page: page2, state: state2, errors: errors2 } = await open({ id: 'pf-handoff', fixDelay: 1200 })
  await openDialog(page2)
  const exit = dialog(page2).locator('[data-fix-choice="governed.exit_text_source:done"]')
  await exit.waitFor()
  await exit.locator('input[type="radio"]').first().check()
  const pick = exit.locator('[data-fix-action="choice"]')
  check('选了 id 叫 copilot 的报告：按钮不变成「交给助手」', await pick.getAttribute('data-fix-handoff') === null
    && !(await pick.innerText()).includes('交给助手'), await pick.innerText())
  await pick.click()
  const wait2 = dialog(page2).locator('[data-fix-preview="loading"]')
  await wait2.waitFor()
  check('等待时写「正在生成修复预览」', (await wait2.innerText()).includes('正在生成修复预览'), await wait2.innerText())
  const body2 = state2.autofixes.at(-1)
  check('请求不带 assist，选的值是 copilot（节点 id）', !body2?.assist && body2?.choices?.['governed.exit_text_source:done'] === 'copilot',
    JSON.stringify(body2))
  check('没有页面错误', !errors.length && !errors2.length, [...errors, ...errors2].join(' | '))
})

// ================================================================ 一键升级为可追溯结构（五期）

const dock = (page) => page.locator('#dock-problems')
async function openProblems(page) {
  await page.locator('.react-flow__pane').click({ position: { x: 30, y: 30 } })
  await page.keyboard.press('Alt+KeyP')
  await dock(page).waitFor()
}
const upgradeBox = (page) => dock(page).locator('[data-upgrade]')
const preview = (page) => dock(page).locator('[data-upgrade-preview]')
const flat = (t) => String(t ?? '').replace(/\s+/g, ' ').trim()

await section('升级：建议不算问题，问题面板给快速修复；预览逐项写改动，类型变化写清楚，说明原样', async () => {
  const { page, state, errors } = await open({ id: 'pf-legacy' })
  await waitFor(page, async () => page.evaluate(() => window.__studio.getState().advice.length > 0))
  const chip = flat(await page.locator('button.chip').first().innerText().catch(() => ''))
  const st = await page.evaluate(() => { const s = window.__studio.getState(); return { issues: s.issues.length, advice: s.advice.map((a) => a.code) } })
  check('校验给的建议进 advice，不进 issues（节点卡、检查器、运行按钮只认 error / warning）', st.issues === 0
    && JSON.stringify(st.advice) === '["evidence.upgrade_available"]', JSON.stringify(st))
  check('工具栏照样说「可运行」：建议不算提示', chip.includes('可运行'), chip.slice(0, 120))
  const kept = await page.evaluate(async () => {
    // 按页面自己加载时的地址 import：改过的模块地址带 ?t=，直接写 /src/… 会拿到另一份实例
    const url = performance.getEntriesByType('resource').map((e) => e.name)
      .find((n) => { try { return new URL(n).pathname === '/src/canvas/issues.ts' } catch { return false } })
    const m = await import(url ?? '/src/canvas/issues.ts')
    const info = { level: 'info', code: 'evidence.upgrade_available', message: '可以升级为可追溯结构', node_id: null }
    const warn = { level: 'warning', code: null, message: '一条提示', node_id: null }
    return { problems: m.problemsOf([info, warn], []).map((p) => p.level), check: m.normalizeCheck({ issues: [info, warn], fixes: [] }, 'published').issues.length }
  }).catch((e) => ({ error: String(e) }))
  check('建议不进问题清单，也不算进发布前检查的「另有 N 条提示」', JSON.stringify(kept) === JSON.stringify({ problems: ['warning'], check: 1 }), JSON.stringify(kept))
  await openProblems(page)
  check('问题面板：没有问题行，但有「可以升级为可追溯结构」', await dock(page).locator('[data-problem]').count() === 0
    && (await upgradeBox(page).locator('[data-upgrade-advice]').innerText().catch(() => '')).includes('可以升级为可追溯结构'))
  const action = upgradeBox(page).locator('[data-upgrade-action]')
  check('快速修复写「升级为可追溯结构（预览改动）」', flat(await action.innerText().catch(() => '')) === '升级为可追溯结构（预览改动）',
    await action.innerText().catch(() => ''))
  check('……「未发现问题」照样说', (await dock(page).innerText()).includes('未发现问题'))
  check('还没点之前不请求升级接口', state.upgrades.length === 0)
  await action.click()
  await page.locator('#dock-problems [data-upgrade-preview="ready"]').waitFor()
  const req = state.upgrades[0]
  check('请求带着画布上的整张图（连同全图默认），不带 assist', req?.graph?.nodes?.length === LEGACY.nodes.length
    && req?.graph?.edges?.length === LEGACY.edges.length && req?.graph?.defaults?.approval === 'always' && !('assist' in (req ?? {})),
  JSON.stringify({ n: req?.graph?.nodes?.length, d: req?.graph?.defaults, a: req?.assist }))
  const typeRow = flat(await preview(page).locator('[data-upgrade-change="type"]').innerText().catch(() => ''))
  check('节点类型的变化写清楚：「写周报」· 节点类型：模型调用 → 报告撰写', /^「写周报」 · 节点类型：\s*模型调用\s*报告撰写$/.test(typeRow)
    && flat(await preview(page).locator('[data-upgrade-change="type"] [data-upgrade-before]').innerText()) === '模型调用'
    && flat(await preview(page).locator('[data-upgrade-change="type"] [data-upgrade-after]').innerText()) === '报告撰写', typeRow)
  const r1 = flat(await preview(page).locator('[data-upgrade-step="R1"] [data-upgrade-step-label]').innerText().catch(() => ''))
  check('同一步的几项改动放在一组，规则（R1）和这一步的说明只写一次', r1 === 'R1 将「写周报」换成报告撰写，出具契约改为核对它的文档'
    && await preview(page).locator('[data-upgrade-step="R1"] [data-upgrade-change]').count() === 5
    && !(await preview(page).locator('[data-upgrade-step="R1"] ul').innerText()).includes('出具契约改为核对它的文档'), r1)
  const rows = (await preview(page).locator('[data-upgrade-change]').allInnerTexts()).map(flat)
  const contract = rows.find((r) => r.includes('出具契约')) ?? ''
  check('契约改动按键一行写、节点引用写节点名（报告来自：「写周报」）', contract.includes('「成果」 · 出具契约') && contract.includes('报告来自：「写周报」')
    && contract.includes('叙述：{{ nodes.story.text }}'), contract)
  const cite = rows.find((r) => r.includes('取数')) ?? ''
  check('R4 用字段的界面叫法：「取数」· 按出处核对字段：（空）→ 是', /「取数」 · 按出处核对字段：\s*（空）\s*是/.test(cite), cite)
  const instructions = rows.find((r) => r.includes('写作要求')) ?? ''
  check('换了类型的节点，新字段按新类型的叫法念（写作要求）', instructions.includes('「写周报」 · 写作要求') && instructions.includes('按口径写本周周报'), instructions)
  const prompt = rows.find((r) => r.includes('按口径写本周周报') && !r.includes('写作要求')) ?? ''
  check('……换掉类型时去掉的，按原来的叫法念（用户提示：… → （空））', /「写周报」 · 用户提示：\s*按口径写本周周报\s*（空）/.test(prompt), prompt)
  check('一共 6 处改动，标题写着', flat(await preview(page).locator('.font-medium').first().innerText()) === '升级预览 · 6 处改动')
  check('合计：1 个节点变更了类型、修改了 2 个节点的配置', flat(await preview(page).locator('[data-upgrade-summary]').innerText().catch(() => ''))
    === '合计：1 个节点变更了类型、修改了 2 个节点的配置')
  const note = await preview(page).locator('[data-upgrade-note="R5"] [data-upgrade-note-text]').innerText().catch(() => '')
  check('说明原样显示（R5：代码节点要不要标 source，等人确认）', note === R5_NOTE && await preview(page).locator('[data-upgrade-note]').count() === 1, note)
  check('升级后没有新的错误：照实说', flat(await preview(page).locator('[data-upgrade-after-check]').innerText().catch(() => '')) === '升级后校验和门禁均无新的错误')
  check('预览不改画布、不保存', state.patches.length === 0
    && await page.evaluate(() => window.__studio.getState().nodes.find((n) => n.id === 'story').data.nodeType) === 'llm')
  check('预览开着时快速修复按钮收起（不重复开）', await upgradeBox(page).locator('[data-upgrade-action]').count() === 0)
  await shoot(page, 'upgrade-preview', page.locator('section[aria-label="问题"]'))
  await preview(page).locator('[data-upgrade-discard]').click()
  check('放弃：预览收起，快速修复回来，画布没动', await preview(page).count() === 0 && await upgradeBox(page).locator('[data-upgrade-action]').count() === 1
    && state.patches.length === 0)
  // 问题面板停在「发布前检查」时又开了一次升级预览（记录页的横幅跳过来）：切回「校验」，预览看得见
  await dock(page).locator('[data-problems-mode="publish"]').click()
  await dock(page).locator('[data-preflight="panel"]').waitFor()
  await page.evaluate(() => window.__studio.getState().previewUpgrade())
  await page.locator('#dock-problems [data-upgrade-preview="ready"]').waitFor({ timeout: 4000 }).catch(() => {})
  check('停在发布前检查时开了升级预览：切回「校验」，预览摆着', await dock(page).locator('[data-problems-mode="lint"]').getAttribute('aria-checked') === 'true'
    && await page.locator('#dock-problems [data-upgrade-preview="ready"]').count() === 1)
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
})

await section('升级：应用走现有的保存，全图默认不丢，节点类型真的换了', async () => {
  const { page, state, errors } = await open({ id: 'pf-legacy' })
  await page.evaluate(() => window.__studio.setState({ pendingNote: '恢复到 v2' }))
  await openProblems(page)
  await upgradeBox(page).locator('[data-upgrade-action]').click()
  await page.locator('[data-upgrade-preview="ready"]').waitFor()
  await preview(page).locator('[data-upgrade-apply]').click()
  await waitFor(page, async () => state.patches.length === 1 && await preview(page).count() === 0)
  const saved = state.patches[0]?.graph
  const story = saved?.nodes?.find((n) => n.id === 'story')
  check('保存的载荷：「写周报」换成报告撰写，写作要求是合成的', story?.type === 'report' && story?.data?.config?.instructions === '你是分析师\n\n按口径写本周周报',
    JSON.stringify(story))
  check('……契约改成 report_from，required / strict 原样', JSON.stringify(cfgOf(saved, 'done')?.contract)
    === JSON.stringify({ metrics_from: ['caliber'], report_from: 'story', required: ['gmv'], strict: true }))
  check('……全图默认原样带着（画布只管节点和连线，保存照样不能丢）', JSON.stringify(saved?.defaults) === JSON.stringify(LEGACY.defaults),
    JSON.stringify(saved?.defaults ?? null))
  check('……节点和连线一个没少', saved?.nodes?.length === LEGACY.nodes.length && saved?.edges?.length === LEGACY.edges.length)
  check('版本说明：「恢复到 v2」保留，后面接着写升级', state.patches[0]?.note === '恢复到 v2；升级为可追溯结构', state.patches[0]?.note)
  const st = await page.evaluate(() => {
    const s = window.__studio.getState()
    return { type: s.nodes.find((n) => n.id === 'story').data.nodeType, dirty: s.dirty, label: s.past.at(-1)?.label, v: s.workflow.version,
      card: document.querySelector('.react-flow__node[data-id="story"] [data-type]')?.getAttribute('data-type') ?? '' }
  })
  check('画布上的节点真的换成报告撰写（卡片也跟着换）', st.type === 'report' && st.card === 'report', JSON.stringify(st))
  check('存完不再「未保存」，撤销栈里这一步叫「升级为可追溯结构」', !st.dirty && st.label === '升级为可追溯结构' && st.v === 4, JSON.stringify(st))
  await waitFor(page, async () => (await upgradeBox(page).count()) === 0, 4000)
  check('重新校验之后建议没了，入口跟着收起', await upgradeBox(page).count() === 0)
  check('只存了一次，没有发布', state.patches.length === 1 && state.publishes.length === 0)
  const toasts = (await page.locator('[data-toast], [role=status]').allInnerTexts()).join(' ')
  check('提示存成了哪一版', toasts.includes('已升级为可追溯结构，保存为草稿 v4'), toasts.slice(0, 160))
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
})

await section('升级：问数据的图插入报告节点、改接连线；后端只给图时照样逐项列', async () => {
  const { page, state } = await open({ id: 'pf-ask' })
  await openProblems(page)
  await upgradeBox(page).locator('[data-upgrade-action]').waitFor()
  await upgradeBox(page).locator('[data-upgrade-action]').click()
  await page.locator('[data-upgrade-preview="ready"]').waitFor()
  const added = flat(await preview(page).locator('[data-upgrade-change="node"]').innerText().catch(() => ''))
  check('新插入的节点：新增节点：「报告撰写」（报告撰写）', added === '新增节点：「报告撰写」（报告撰写）', added)
  const edges = (await preview(page).locator('[data-upgrade-change="edge"]').allInnerTexts()).map(flat)
  check('新连线：「取数」 → 「报告撰写」（节点名取自升级后的图）', edges.includes('新连线：「取数」 → 「报告撰写」'), edges.join(' | '))
  check('改接的连线：连线：「取数」 → 「成果」 改接为 「报告撰写」 → 「成果」', edges.some((e) => /^连线：「取数」 → 「成果」\s*改接为\s*「报告撰写」 → 「成果」$/.test(e)),
    edges.join(' | '))
  const field = flat(await preview(page).locator('[data-upgrade-change="value"]').innerText().catch(() => ''))
  check('成果字段整列改：一项一行（answer：… → answer：{{ nodes.report.text }}）', /^「成果」 · 成果字段：\s*answer：\{\{ nodes\.fetch\.text \}\}\s*answer：\{\{ nodes\.report\.text \}\}$/.test(field), field)
  const note = await preview(page).locator('[data-upgrade-note="R3"] [data-upgrade-note-text]').innerText().catch(() => '')
  check('R3 的说明（契约要不要写 cells 由人定）原样显示', note === R3_NOTE, note)
  check('合计：新增 1 个节点、修改了 1 个节点的配置、连线变动 3 处', flat(await preview(page).locator('[data-upgrade-summary]').innerText().catch(() => ''))
    === '合计：新增 1 个节点、修改了 1 个节点的配置、连线变动 3 处')
  await shoot(page, 'upgrade-preview-ask', page.locator('section[aria-label="问题"]'))
  await preview(page).locator('[data-upgrade-apply]').click()
  await waitFor(page, async () => state.patches.length === 1)
  const saved = state.patches[0]?.graph
  check('应用：新节点和改接的连线都存进去（成果改取报告的正文，没替人加契约）', saved?.nodes?.some((n) => n.id === 'report' && n.type === 'report')
    && saved?.edges?.some((e) => e.source === 'report' && e.target === 'done') && !saved?.edges?.some((e) => e.source === 'fetch' && e.target === 'done')
    && cfgOf(saved, 'done')?.fields?.[0]?.value === '{{ nodes.report.text }}' && !cfgOf(saved, 'done')?.contract, JSON.stringify(saved?.edges))
  check('……画布上也有了这个节点', await page.evaluate(() => window.__studio.getState().nodes.some((n) => n.id === 'report' && n.data.nodeType === 'report')))

  const bare = await open({ id: 'pf-ask', upgrade: 'bare' })
  await openProblems(bare.page)
  await upgradeBox(bare.page).locator('[data-upgrade-action]').click()
  await bare.page.locator('[data-upgrade-preview="ready"]').waitFor()
  const kinds = await preview(bare.page).locator('[data-upgrade-change]').evaluateAll((els) => els.map((e) => e.getAttribute('data-upgrade-change')))
  check('后端只给图、不给逐项改动：按前后两张图自己列（新节点、改的配置、连线）', kinds.includes('node') && kinds.includes('value')
    && kinds.filter((k) => k === 'edge').length === 3, kinds.join(','))
  check('……照样能应用', await preview(bare.page).locator('[data-upgrade-apply]').count() === 1)
})

await section('升级：画布锁着不给入口；预览摆着时锁上，应用落不下去', async () => {
  const { page, state } = await open({ id: 'pf-legacy' })
  await openProblems(page)
  await upgradeBox(page).locator('[data-upgrade-action]').waitFor()
  await page.evaluate(() => {
    const s = window.__studio.getState()
    window.__studio.setState({ runPhase: 'running', trace: { ...s.trace, runClass: 'formal' } })
  })
  await page.waitForTimeout(200)
  check('正式运行进行中：没有快速修复按钮', await upgradeBox(page).locator('[data-upgrade-action]').count() === 0)
  check('……写明为什么现在不能升级', flat(await upgradeBox(page).locator('[data-upgrade-locked]').innerText().catch(() => '')).includes('正式运行进行中'))
  await page.evaluate(() => window.__studio.getState().previewUpgrade())
  await page.waitForTimeout(200)
  check('锁着时直接调也不请求升级接口', state.upgrades.length === 0)
  await page.evaluate(() => window.__studio.setState({ runPhase: 'idle' }))
  await upgradeBox(page).locator('[data-upgrade-action]').click()
  await page.locator('[data-upgrade-preview="ready"]').waitFor()
  await page.evaluate(() => window.__studio.setState({ copilot: { ...window.__studio.getState().copilot, active: true } }))
  await page.waitForTimeout(200)
  check('助手在改时：预览还在，但没有「应用」和「交给助手改写计算逻辑」', await preview(page).count() === 1
    && await preview(page).locator('[data-upgrade-apply], [data-upgrade-assist]').count() === 0
    && flat(await upgradeBox(page).locator('[data-upgrade-locked]').innerText().catch(() => '')).includes('助手正在修改此工作流'))
  const ok = await page.evaluate(() => window.__studio.getState().applyUpgrade())
  check('锁着时直接调应用也落不下去、不保存', ok === false && state.patches.length === 0
    && await page.evaluate(() => window.__studio.getState().nodes.find((n) => n.id === 'story').data.nodeType) === 'llm')
})

await section('升级：老后端（校验不给这条建议）不给入口；升级接口 404 照实说', async () => {
  const { page, state } = await open({ id: 'pf-legacy', advice: false })
  await openProblems(page)
  await page.waitForTimeout(600)
  check('没有这条建议：问题面板里没有升级入口', await upgradeBox(page).count() === 0 && (await dock(page).innerText()).includes('未发现问题'))
  check('……也不去请求升级接口', state.upgrades.length === 0)

  const old = await open({ id: 'pf-legacy', upgrade: 'old' })
  await openProblems(old.page)
  await upgradeBox(old.page).locator('[data-upgrade-action]').click()
  await old.page.locator('#dock-problems [data-upgrade-preview]:not([data-upgrade-preview="loading"])').waitFor()
  check('升级接口不存在（404）：照实说当前服务版本不支持', await preview(old.page).getAttribute('data-upgrade-preview') === 'unsupported'
    && flat(await preview(old.page).innerText()).includes('当前服务版本不支持一键升级'), await preview(old.page).innerText())
  check('……没有应用按钮、不保存', await preview(old.page).locator('[data-upgrade-apply]').count() === 0 && old.state.patches.length === 0)
})

await section('升级：再交给 Copilot 改语义层：请求带 assist，保留没改的写出来', async () => {
  const { page, state } = await open({ id: 'pf-legacy', upgradeDelay: 800 })
  await openProblems(page)
  await upgradeBox(page).locator('[data-upgrade-action]').click()
  await page.locator('[data-upgrade-preview="ready"]').waitFor()
  const btn = preview(page).locator('[data-upgrade-assist]')
  check('预览里有「交给助手改写计算逻辑」，悬停说清它改什么、要调模型', flat(await btn.innerText()) === '交给助手改写计算逻辑'
    && (await btn.getAttribute('title') ?? '').includes('纯算术'))
  await btn.click()
  const loading = page.locator('[data-upgrade-preview="loading"]')
  await loading.waitFor()
  check('等待时说助手在改', flat(await loading.innerText()).includes('助手正在把代码节点中的计算改写为口径卡表达式'))
  check('请求带复核用的发布级别：已发布的图按「已发布」', state.upgrades[0]?.level === 'published', JSON.stringify(state.upgrades[0]?.level))
  check('第二次请求带 assist: true、带的还是画布上的原图', state.upgrades.length === 2 && state.upgrades[1]?.assist === true
    && state.upgrades[1]?.graph?.nodes?.find((n) => n.id === 'story')?.type === 'llm', JSON.stringify(state.upgrades.map((u) => u.assist ?? null)))
  await page.locator('[data-upgrade-preview="ready"]').waitFor()
  const said = flat(await preview(page).locator('[data-upgrade-assist-said]').innerText().catch(() => ''))
  check('助手的总结和未修改的代码节点写出来', said.includes('不是纯算术') && said.includes('未修改的节点')
    && said.includes('没有改成口径卡表达式'), said)
  check('已经是助手的结果：不再给「交给助手改写计算逻辑」', await preview(page).locator('[data-upgrade-assist]').count() === 0)
  await preview(page).locator('[data-upgrade-assist-said]').scrollIntoViewIfNeeded().catch(() => {})
  await shoot(page, 'upgrade-preview-assist', page.locator('section[aria-label="问题"]'))
  // 受管的图：新写出来的报告节点要直接按受管门禁的要求配，请求带 governed
  await page.evaluate(() => { const s = window.__studio.getState(); window.__studio.setState({ workflow: { ...s.workflow, status: 'governed' } }) })
  await preview(page).locator('[data-upgrade-discard]').click()
  await upgradeBox(page).locator('[data-upgrade-action]').click()
  await waitFor(page, async () => state.upgrades.length === 3)
  check('受管的图按「受管」复核', state.upgrades[2]?.level === 'governed', JSON.stringify(state.upgrades[2]?.level))
})

await section('升级：预览之后画布又改过就不套用；存失败给重试保存', async () => {
  const { page, state } = await open({ id: 'pf-legacy' })
  await openProblems(page)
  await upgradeBox(page).locator('[data-upgrade-action]').click()
  await page.locator('[data-upgrade-preview="ready"]').waitFor()
  await page.evaluate(() => window.__studio.getState().updateNode('caliber', { label: '周报口径2' }))
  await preview(page).locator('[data-upgrade-apply]').click()
  await preview(page).locator('[data-upgrade-stale]').waitFor({ timeout: 3000 }).catch(() => {})
  check('画布改过：说预览对不上了，不套用、不保存', await preview(page).locator('[data-upgrade-stale]').count() === 1 && state.patches.length === 0
    && await page.evaluate(() => window.__studio.getState().nodes.find((n) => n.id === 'story').data.nodeType) === 'llm')
  await preview(page).locator('[data-upgrade-stale]').getByRole('button', { name: '重新预览' }).click({ timeout: 2000 }).catch(() => {})
  await waitFor(page, async () => state.upgrades.length === 2 && await page.locator('#dock-problems [data-upgrade-preview="ready"]').count() === 1)
  check('「重新预览」按现在的画布再要一份（带着刚改的名字）', state.upgrades.length === 2
    && state.upgrades[1]?.graph?.nodes?.find((n) => n.id === 'caliber')?.data?.label === '周报口径2'
    && await preview(page).locator('[data-upgrade-stale]').count() === 0)

  const bad = await open({ id: 'pf-legacy', save: 'fail' })
  await openProblems(bad.page)
  await upgradeBox(bad.page).locator('[data-upgrade-action]').click()
  await bad.page.locator('[data-upgrade-preview="ready"]').waitFor()
  await preview(bad.page).locator('[data-upgrade-apply]').click()
  await preview(bad.page).locator('[data-upgrade-save-failed]').waitFor().catch(() => {})
  const why = flat(await preview(bad.page).locator('[data-upgrade-save-failed]').innerText({ timeout: 1000 }).catch(() => ''))
  check('存失败：说清没存上、升级已经在画布上，给「重试保存」', why.includes('草稿保存失败') && why.includes('数据库已锁定')
    && why.includes('已应用到画布') && await bad.page.evaluate(() => window.__studio.getState().dirty), why)
  bad.state.save = 'ok'
  await preview(bad.page).getByRole('button', { name: '重试保存' }).click({ timeout: 2000 }).catch(() => {})
  await waitFor(bad.page, async () => bad.state.patches.length === 2 && await preview(bad.page).count() === 0)
  check('重试保存：存上，预览收起', bad.state.patches.length === 2 && bad.state.patches[1]?.graph?.nodes?.find((n) => n.id === 'story')?.type === 'report'
    && !(await bad.page.evaluate(() => window.__studio.getState().dirty)))
})

await section('升级：没存上之后——平常的保存存上了就收起；画布和升级结果对不上时不替它存；也能放弃', async () => {
  // 三种收尾共同的起点：升级落到画布上、存失败。之后保存接口恢复
  const failedApply = async () => {
    const r = await open({ id: 'pf-legacy', save: 'fail' })
    await openProblems(r.page)
    await upgradeBox(r.page).locator('[data-upgrade-action]').click()
    await r.page.locator('[data-upgrade-preview="ready"]').waitFor()
    await preview(r.page).locator('[data-upgrade-apply]').click()
    await preview(r.page).locator('[data-upgrade-save-failed]').waitFor().catch(() => {})
    r.state.save = 'ok'
    return r
  }
  const upOf = (page) => page.evaluate(() => {
    const u = window.__studio.getState().upgrade
    return u ? `${u.status}:${u.apply?.status ?? ''}` : null
  })
  const storyType = (page) => page.evaluate(() => window.__studio.getState().nodes.find((n) => n.id === 'story').data.nodeType)

  // 一、平常的保存（⌘S、工具栏的「保存」都走 studio.save）
  const a = await failedApply()
  check('起点：存失败，面板写着「草稿保存失败」', await preview(a.page).locator('[data-upgrade-save-failed]').count() === 1 && a.state.patches.length === 1,
    `${await upOf(a.page)} patches=${a.state.patches.length}`)
  const saved = await a.page.evaluate(async () => {
    window.__p5up = window.__studio.getState().upgrade
    try { await window.__studio.getState().save(); return 'ok' } catch (e) { return String(e) }
  })
  await waitFor(a.page, async () => await dock(a.page).locator('[data-upgrade-save-failed]').count() === 0, 3000)
  check('平常的保存存上了：「草稿保存失败」收起，升级的状态清掉', saved === 'ok' && a.state.patches.length === 2
    && await dock(a.page).locator('[data-upgrade-save-failed]').count() === 0 && await upOf(a.page) === null,
  `${saved} ${await upOf(a.page)} patches=${a.state.patches.length}`)
  check('……存的是画布上升级完的图，版本说明没冒充「升级为可追溯结构」', a.state.patches[1]?.graph?.nodes?.find((n) => n.id === 'story')?.type === 'report'
    && a.state.patches[1]?.note === undefined, JSON.stringify(a.state.patches[1]?.note ?? null))
  const variants = await a.page.evaluate(async () => {
    const st = window.__studio
    const base = window.__p5up
    st.setState({ upgrade: { ...base, apply: { status: 'locked', why: '助手正在修改此工作流，请等待完成或先停止助手', unsaved: true } } })
    await st.getState().save()
    const locked = st.getState().upgrade
    st.setState({ upgrade: { ...base, apply: undefined } })
    await st.getState().save()
    const open = st.getState().upgrade
    st.getState().discardUpgrade()
    return { locked: locked ? `${locked.status}:${locked.apply?.status}` : null, open: open ? `${open.status}:${open.apply?.status ?? ''}` : null }
  })
  check('锁着没存上的，平常的保存存上了同样清掉；预览还没应用的，平常的保存不替人收起', variants.locked === null && variants.open === 'ready:',
    JSON.stringify(variants))

  // 二、撤销了升级再点「重试保存」：存下去的是没升级的图，版本说明却写着升级，不能存
  const b = await failedApply()
  await b.page.evaluate(() => window.__studio.getState().undo())
  check('撤销了升级：画布回到升级前', await storyType(b.page) === 'llm')
  await preview(b.page).getByRole('button', { name: '重试保存' }).click({ timeout: 2000 }).catch(() => {})
  await b.page.waitForTimeout(500)
  check('画布内容和升级结果对不上：点「重试保存」不发保存请求', b.state.patches.length === 1, `patches=${b.state.patches.length}`)
  check('……改说此预览已失效（给「重新预览」），不再说「草稿保存失败」', await preview(b.page).locator('[data-upgrade-stale]').count() === 1
    && await preview(b.page).locator('[data-upgrade-save-failed]').count() === 0, String(await upOf(b.page)))
  // 重做回升级完的样子、再改一处：画布上有升级，但也有升级之外的改动，同样不替它存
  const direct = await b.page.evaluate(async () => {
    const st = window.__studio
    st.getState().redo()
    const redone = st.getState().nodes.find((n) => n.id === 'story').data.nodeType
    st.setState({ upgrade: { ...st.getState().upgrade, apply: { status: 'failed', error: new Error('x') } } })
    st.getState().updateNode('caliber', { label: '周报口径2' })
    return { redone, ok: await st.getState().retryUpgradeSave(), apply: st.getState().upgrade?.apply?.status ?? null }
  })
  await b.page.waitForTimeout(300)
  check('……升级之后又改过画布的，直接调重试保存也不存', direct.redone === 'report' && direct.ok === false && direct.apply === 'stale'
    && b.state.patches.length === 1, `${JSON.stringify(direct)} patches=${b.state.patches.length}`)

  // 三、没存上时也能放弃：只收起预览，画布上的升级留着、不再存
  const c = await failedApply()
  const drop = preview(c.page).locator('[data-upgrade-save-failed] [data-upgrade-discard]')
  check('没存上时有「放弃」，和「重试保存」并排', await drop.count() === 1 && flat(await drop.innerText().catch(() => '')) === '放弃'
    && await preview(c.page).locator('[data-upgrade-save-failed]').getByRole('button', { name: '重试保存' }).count() === 1)
  await c.page.evaluate(() => {
    const u = window.__studio.getState().upgrade
    window.__studio.setState({ upgrade: { ...u, apply: { status: 'locked', why: '助手正在修改此工作流，请等待完成或先停止助手', unsaved: true } } })
  })
  check('……锁着没存上时同样有「放弃」', await preview(c.page).locator('[data-upgrade-save-failed="locked"] [data-upgrade-discard]').count() === 1)
  await drop.click({ timeout: 2000 }).catch(() => {})
  await c.page.waitForTimeout(300)
  check('点「放弃」：状态清掉、预览收起；画布上的升级留着，没有再存', await upOf(c.page) === null && await preview(c.page).count() === 0
    && c.state.patches.length === 1 && await storyType(c.page) === 'report' && await c.page.evaluate(() => window.__studio.getState().dirty),
  `${await upOf(c.page)} patches=${c.state.patches.length}`)
  check('没有运行时报错', [...a.errors, ...b.errors, ...c.errors].length === 0, [...a.errors, ...b.errors, ...c.errors].join(' | '))
})

await section('升级：校验接口失败时升级块照样在（后端断开时保存和校验一起失败，「重试保存」不能跟着消失）', async () => {
  const { page, state, errors } = await open({ id: 'pf-legacy', save: 'fail' })
  await openProblems(page)
  await upgradeBox(page).locator('[data-upgrade-action]').click()
  await page.locator('[data-upgrade-preview="ready"]').waitFor()
  state.validate = 'fail'
  await page.evaluate(() => window.__studio.getState().analyzeNow())
  await waitFor(page, async () => page.evaluate(() => window.__studio.getState().analysis === 'failed'), 3000)
  check('起点：校验接口失败，面板说分析失败、给「重试」', (await dock(page).innerText()).includes('分析失败')
    && await dock(page).getByRole('button', { name: '重试', exact: true }).count() === 1)
  check('升级预览开着时校验接口失败：升级块和预览照样在', await upgradeBox(page).count() === 1
    && await page.locator('#dock-problems [data-upgrade-preview="ready"]').count() === 1
    && await preview(page).locator('[data-upgrade-apply]').count() === 1)
  const layout = await page.evaluate(() => {
    const box = document.querySelector('#dock-problems [data-upgrade]')
    const fail = document.querySelector('#dock-problems [data-analysis-failed]')
    const scroller = box?.parentElement
    return { above: !!(box && fail && box.compareDocumentPosition(fail) & Node.DOCUMENT_POSITION_FOLLOWING),
      sameScroller: !!scroller && scroller === fail?.parentElement && getComputedStyle(scroller).overflowY === 'auto' }
  })
  check('……升级块在「分析失败」上面，两块在同一个可滚动的容器里', layout.above && layout.sameScroller, JSON.stringify(layout))

  await preview(page).locator('[data-upgrade-apply]').click()
  await preview(page).locator('[data-upgrade-save-failed]').waitFor({ timeout: 3000 }).catch(() => {})
  await waitFor(page, async () => page.evaluate(() => window.__studio.getState().analysis === 'failed'), 3000)
  check('保存和校验一起失败：「草稿保存失败」「重试保存」「放弃」照样在', await page.evaluate(() => window.__studio.getState().analysis) === 'failed'
    && await preview(page).locator('[data-upgrade-save-failed]').count() === 1
    && await preview(page).locator('[data-upgrade-save-failed]').getByRole('button', { name: '重试保存' }).count() === 1
    && await preview(page).locator('[data-upgrade-save-failed] [data-upgrade-discard]').count() === 1
    && (await dock(page).innerText()).includes('分析失败'))
  // 滚到底：「没存上」那一行和下面的「分析失败 · 重试」一屏看全
  await dock(page).locator('[data-analysis-failed]').scrollIntoViewIfNeeded().catch(() => {})
  await shoot(page, 'p5fix-offline', page.locator('section[aria-label="问题"]'))
  // 后端回来了：重试保存照常存上
  state.save = 'ok'
  state.validate = 'ok'
  await preview(page).getByRole('button', { name: '重试保存' }).click({ timeout: 2000 }).catch(() => {})
  await waitFor(page, async () => state.patches.length === 2 && await preview(page).count() === 0)
  check('后端回来之后「重试保存」存上，预览收起', state.patches.length === 2 && await preview(page).count() === 0
    && state.patches[1]?.note === '升级为可追溯结构', `patches=${state.patches.length} ${JSON.stringify(state.patches[1]?.note ?? null)}`)
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
})

await section('升级：?upgrade=1 直达：打开问题面板、开始预览，地址摘掉参数', async () => {
  const { page, state, errors } = await open({ id: 'pf-legacy', path: '/studio/pf-legacy?upgrade=1' })
  await page.locator('[data-upgrade-preview="ready"]').waitFor({ timeout: 8000 }).catch(() => {})
  await page.waitForTimeout(800)
  check('一打开就是问题面板，升级预览摆着', await dock(page).count() === 1 && await page.locator('#dock-problems [data-upgrade-preview="ready"]').count() === 1)
  check('只请求了一次升级接口', state.upgrades.length === 1, String(state.upgrades.length))
  check('地址里的 upgrade 摘掉了（刷新、后退不会再要一次）', !new URL(page.url()).searchParams.has('upgrade') && new URL(page.url()).pathname === '/studio/pf-legacy',
    page.url())
  check('没有自动保存', state.patches.length === 0)
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
})

await section('360px 下不横向滚动', async () => {
  const { page } = await open({ width: 360, height: 780 })
  // 窄屏上「发布」收在「更多操作」里
  await page.getByRole('button', { name: '更多操作' }).click()
  await page.locator('[role="menu"][aria-label="更多操作"] [role^="menuitem"]', { hasText: '发布' }).click()
  await dialog(page).waitFor()
  await dialog(page).locator('[data-fix-all]').waitFor()
  await dialog(page).locator('[data-fix-all]').click()
  await dialog(page).locator('[data-fix-preview="ready"]').waitFor()
  // 量弹窗本身：编排页的工具栏在 360px 下本来就比视口宽（那是页面自己的事），弹窗是 fixed 的一层，
  // 要守的是它里面的东西——问题、选项、预览——一样都不伸出视口、不出横向滚动条
  const m = await page.evaluate(() => {
    const d = document.querySelector('[role="dialog"]')
    const r = d.getBoundingClientRect()
    const wide = [...d.querySelectorAll('*')].filter((el) => {
      const b = el.getBoundingClientRect()
      return b.width > 0 && (b.right > window.innerWidth + 0.5 || b.left < -0.5)
    }).map((el) => el.tagName + (el.className ? `.${String(el.className).split(' ')[0]}` : ''))
    const scrollers = [...d.querySelectorAll('*')].filter((el) => el.scrollWidth > el.clientWidth + 1
      && !['visible', 'hidden', 'clip'].includes(getComputedStyle(el).overflowX)).length
    return { left: r.left, right: r.right, vw: window.innerWidth, wide: wide.slice(0, 4), scrollers, own: d.scrollWidth - d.clientWidth }
  })
  check('360px：弹窗在视口里、里面没有元素伸出去、没有横向滚动条', m.left >= 0 && m.right <= m.vw && !m.wide.length && m.scrollers === 0 && m.own <= 0, JSON.stringify(m))
  await shoot(page, 'publish-dialog-360', page)
})

check('检查没有写库（兜底拦下的写请求）', writes.length === 0, writes.slice(0, 4).join('、'))
console.log(failed ? `\n${failed} 项未通过` : '\n全部通过')
await browser.close()
process.exit(failed ? 1 : 0)
