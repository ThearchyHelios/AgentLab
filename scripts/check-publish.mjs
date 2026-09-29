// 发布前检查与自动修复的检查：发布弹窗打开就列问题、逐条修（auto / choice / assist）、
// 先预览再「应用并重新检查」、问题面板里的「发布前检查」、老后端（两个接口 404）照旧。
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
}

const clone = (x) => JSON.parse(JSON.stringify(x))
const who = (n) => `「${n.data.label}」`

/**
 * 伪造的门禁：按请求里的图现算，形状照 PF-SPEC（issues 带 code 和 fix，fixes 列修法）。
 * 已发布档只给提示，受管档有错。修法的 id 是 code:节点（图级的是 code:graph）。
 * followsNever：当作这张图的全图默认是「全部自动放行」（画布上不带 defaults，按工作流认）
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
    issues.push({ level: 'error', node_id: null, code: 'governed.no_contract', fix: id, field: null,
      message: '受管模板至少要有一个「成果 / 出具」节点声明出具契约' })
    fixes.push({ id, code: 'governed.no_contract', node_id: outputs[0].id, kind: 'choice', multiple: true,
      label: `给${who(outputs[0])}生成出具契约，再选出必需的指标`,
      options: cards.flatMap((m) => m.data.config.metrics.map((x) => ({ value: x.id, label: `${x.id} · ${x.name}` }))) })
  }
  for (const n of graph.nodes) {
    const c = n.data.config ?? {}
    if (followsNever && n.type === 'agent' && !c.approval && (c.tools ?? []).length) {
      const id = 'governed.default_approval_never:graph'
      issues.push({ level: hard ? 'error' : 'warning', node_id: n.id, code: 'governed.default_approval_never', fix: id, field: 'approval',
        message: `${who(n)}（Agent）没写自己的审批策略，跟随全图默认的「全部自动放行」` })
      if (!fixes.some((f) => f.id === id)) {
        fixes.push({ id, code: 'governed.default_approval_never', node_id: null, kind: 'auto', label: '把全图默认的审批策略改成「仅危险工具需要审批」',
          preview: { field: 'defaults.approval', before: 'never', after: 'dangerous' } })
      }
    }
    if (n.type === 'agent' && c.approval === 'never' && (c.tools ?? []).length) {
      add(n, 'governed.agent_approval_never', `${who(n)}（Agent）的审批策略是「全部自动放行」，受管模板要求危险工具至少人工审批`,
        { kind: 'auto', label: '把审批策略改成「仅危险工具需要审批」', preview: { field: 'approval', before: 'never', after: 'dangerous' } })
    }
    if (n.type === 'subgraph' && !c.workflow_version) {
      add(n, 'governed.subgraph_unpinned', `${who(n)}（子工作流）没有钉住版本，口径会随上游最新版漂移`,
        { kind: 'auto', label: '钉到它最新的已发布版本 v4', preview: { field: 'workflow_version', before: null, after: 4 } })
    }
    if (n.type === 'code' && c.evidence_role !== 'source') {
      add(n, 'governed.caliber_compute_input', `${who(n)}（沙箱代码）的产出喂给了口径卡，而它的角色是计算`, {
        kind: 'choice', label: `${who(n)}的产出喂给了口径卡：它是在取数，还是在做计算？`,
        options: [{ value: 'source', label: '它在取数：标成 source（取数）' },
          { value: 'copilot', label: '它在做计算：交给 Copilot 把计算挪进口径卡', handoff: true }],
      })
    }
    if (n.type === 'output' && c.contract?.report_from && !(c.fields ?? []).every((f) => f.value.includes(`nodes.${c.contract.report_from}.`))) {
      add(n, 'governed.exit_text_source', `${who(n)}的成果字段取的不是报告撰写节点的正文`, {
        kind: 'choice', label: '把成果字段改成取哪个报告撰写节点的正文',
        options: graph.nodes.filter((r) => r.type === 'report').map((r) => ({ value: r.id, label: who(r), hint: '报告撰写' })),
      })
    }
    if (n.type === 'supervisor') {
      add(n, 'governed.supervisor', `${who(n)}（多 Agent 协作）：受管模板不允许全动态规划的节点`,
        { kind: 'assist', label: '换成固定编排是结构性的改动，交给 Copilot' })
    }
    if (n.type === 'output' && c.contract) {
      const k = c.contract
      if (!(k.metrics_from ?? []).length) {
        // 真后端 validate 和门禁各报一条（同 code、同节点，说法不同），指向同一个修复；validate 的排在最前
        if (followsNever) {
          issues.unshift({ level: 'error', node_id: n.id, code: 'contract.metrics_from_missing', fix: `contract.metrics_from_missing:${n.id}`,
            field: 'contract.metrics_from', message: '出具契约缺 metrics_from（指标来自哪个「口径卡」节点）' })
        }
        add(n, 'contract.metrics_from_missing', '出具契约没有声明 metrics_from（指标来自哪个「口径卡」节点）', {
          kind: 'choice', label: '指标来自哪张口径卡', default: cards[0]?.id,
          options: cards.map((m) => ({ value: m.id, label: m.data.label, hint: `${m.data.config.metrics.length} 个指标` })),
        })
      }
      if (hard && !(k.required ?? []).length) {
        const from = (k.metrics_from ?? []).length ? cards.filter((m) => k.metrics_from.includes(m.id)) : cards
        add(n, 'contract.required_missing', '受管模板的出具契约必须声明 required（必需指标）', {
          kind: 'choice', multiple: true, label: '哪些指标缺了就不该出具',
          options: from.flatMap((m) => m.data.config.metrics.map((x) => ({ value: x.id, label: `${x.id} · ${x.name}` }))),
        })
      }
      if (hard && !k.strict) {
        add(n, 'contract.strict_off', '受管模板建议把出具契约设为 strict：非 strict 下未回指的数字只降档不拦截',
          { kind: 'auto', label: '把出具契约设为 strict', preview: { field: 'contract.strict', before: false, after: true } }, false)
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
    if (!fix) { rejected.push({ fix_id: id, reason: '这条问题已经不在了' }); continue }
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
      if (unpublished) { rejected.push({ fix_id: id, reason: '嵌的工作流还没有发布过版本，钉不上：先去发布它，或者交给人选' }); continue }
      change('workflow_version', c.workflow_version ?? null, 4); c.workflow_version = 4
    } else if (fix.code === 'contract.strict_off') { change('contract.strict', !!c.contract.strict, true); c.contract.strict = true }
    else if (fix.code === 'contract.metrics_from_missing') {
      const v = body.choices?.[id]
      if (v === undefined) { rejected.push({ fix_id: id, reason: '要人选：没给选择' }); continue }
      change('contract.metrics_from', c.contract.metrics_from ?? null, [v]); c.contract.metrics_from = [v]
    } else if (fix.code === 'governed.no_contract') {
      const v = body.choices?.[id]
      if (!Array.isArray(v) || !v.length) { rejected.push({ fix_id: id, reason: '要人选：没给选择' }); continue }
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
      if (!Array.isArray(v) || !v.length) { rejected.push({ fix_id: id, reason: '要人选：没给选择' }); continue }
      change('contract.required', c.contract.required ?? null, v); c.contract.required = v
    } else { rejected.push({ fix_id: id, reason: '这一条不能直接修' }); continue }
    applied.push(id)
  }
  let assist = null
  if (body.assist || handoff.length) {
    const team = byId('team')
    if (team) {
      changes.push({ fix_id: 'assist', node_id: 'team', node_title: team.data.label, field: 'max_rounds', before: team.data.config.max_rounds, after: 2,
        label: 'Copilot：先把协作轮数收紧' })
      team.data.config.max_rounds = 2
    }
    assist = { ok: true, summary: '把复核团队的轮数收紧到 2 轮；换成固定编排要删节点，按规矩没动它',
      questions: ['复核团队要换成哪一个固定编排的 Agent？它现在只有一个成员 checker，保留它的职责吗？'] }
  }
  const remaining = lint(graph, body.level, { followsNever })
  return { graph, changes, applied, rejected, handoff, remaining: remaining.issues, assist, ok: remaining.ok, ops: [] }
}

// ---------------------------------------------------------------- 浏览器

const browser = await chromium.launch({ executablePath: CHROME })
const writes = []

/**
 * backend：'new'（两个接口都有）| 'old'（两个接口 404）| 'nofix'（有检查、没有 autofix）
 * save：'ok' | 'fail'（保存接口 500）| 'slow'（1.5 秒后才回，看保存进行中的样子）
 */
async function open({ id = 'pf-hard', width = 1440, height = 900, backend = 'new', save = 'ok', unpublished = false, fixDelay = 0 } = {}) {
  const followsNever = id === 'pf-dup'
  const ctx = await browser.newContext({ viewport: { width, height } })
  opened.add(ctx)
  await ctx.addInitScript(() => { try { localStorage.setItem('agentlab_actor', '检查脚本') } catch { /* noop */ } })
  const page = await ctx.newPage()
  page.setDefaultTimeout(8000)
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  const json = (route, body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
  const state = { checks: [], autofixes: [], patches: [], publishes: [], version: FAKES[id].version, graph: clone(FAKES[id].graph), save, backend }

  await page.route(/\/api\//, (route) => {
    const req = route.request()
    if (req.method() === 'GET') return route.continue()
    writes.push(`${req.method()} ${new URL(req.url()).pathname}`)
    return json(route, { detail: '检查脚本不写库' }, 409)
  })
  await page.route(/\/api\/workflows\/(validate|variables)$/, (route) => route.continue())
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
      if (state.save === 'fail') return json(route, { detail: '数据库锁住了，稍后再试' }, 500)
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

  await page.goto(`${WEB}/studio/${id}`, { waitUntil: 'networkidle' })
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
  check('结论一句：会被拦下 5 处，另有 1 条提示', summary.includes('会被门禁拦下 5 处') && summary.includes('1 条提示'), summary)
  check('auto 类有「修复」', await dialog(page).locator('[data-fix-kind="auto"] [data-fix-action="auto"]').count() === 3)
  check('choice 类是选择控件：单选用 radio（两张口径卡）、多选用 checkbox（三个指标）', await dialog(page).locator('[data-fix-choice="contract.metrics_from_missing:done"] input[type="radio"]').count() === 2
    && await dialog(page).locator('[data-fix-choice="contract.required_missing:done"] input[type="checkbox"]').count() === 3)
  check('assist 类有「交给 Copilot」', (await dialog(page).locator('[data-fix-kind="assist"] [data-fix-action="assist"]').innerText()).includes('交给 Copilot'))
  const all = await dialog(page).locator('[data-fix-all]').innerText().catch(() => '')
  check('顶部「一键修复可自动修的 3 处」', all.includes('一键修复可自动修的 3 处'), all)
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
  check('预览：「取数」· 审批策略：全部自动放行 → 仅危险工具需要审批', /「取数」\s*·\s*审批策略：\s*全部自动放行\s*仅危险工具需要审批/.test(approval.replace(/\n.*$/s, '')), approval)
  const strict = lines.find((l) => l.includes('严格模式')) ?? ''
  check('预览：契约里的键用界面叫法（出具契约 · 严格模式：否 → 是）', /出具契约 · 严格模式：\s*否\s*是/.test(strict), strict)
  const rejected = await dialog(page).locator('[data-fix-rejected]').innerText().catch(() => '')
  check('没采用的写出原因', rejected.includes('钉不上') && rejected.includes('钉到它最新的已发布版本'), rejected)
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
  check('预览写明应用后门禁不再拦', after.includes('不再拦'), after)
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
  check('没选时「预览」不可用，并说先选', await req.locator('[data-fix-action="choice"]').isDisabled() && (await req.innerText()).includes('先选好再预览'))
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
  check('保存失败：说清没存上、修复已在画布上', msg.includes('草稿没保存上') && msg.includes('数据库锁住了') && msg.includes('保存成功之前不能发布'), msg)
  check('……发布按钮不可用（不能把没修的那一版发出去）', await submit(bad.page).isDisabled())
  check('……不重查', bad.state.checks.length === 1)
  bad.state.save = 'ok'
  await dialog(bad.page).getByRole('button', { name: '重试保存' }).click()
  await waitFor(bad.page, async () => bad.state.checks.length === 2)
  check('重试保存成功后重新检查、发布可用', bad.state.patches.length === 2 && await submit(bad.page).isEnabled())

  const nofix = await open({ backend: 'nofix' })
  await openDialog(nofix.page)
  await dialog(nofix.page).locator('[data-fix-all]').click()
  await dialog(nofix.page).locator('[data-fix-preview="unsupported"]').waitFor()
  check('autofix 不存在：说这个后端还不支持自动修复', (await dialog(nofix.page).locator('[data-fix-preview]').innerText()).includes('还不支持自动修复'))
  check('……发布按钮照旧可用', await submit(nofix.page).isEnabled())

  const moved = await open({ id: 'pf-easy' })
  await openDialog(moved.page)
  await dialog(moved.page).locator('[data-fix-all]').click()
  await dialog(moved.page).locator('[data-fix-apply]').waitFor()
  await moved.page.evaluate(() => { const s = window.__studio.getState(); s.updateNode('fetch', { label: '取数2' }) })
  await dialog(moved.page).locator('[data-fix-apply]').click()
  await moved.page.waitForTimeout(300)
  check('预览之后画布内容又改过：不套用、说要重查', moved.state.patches.length === 0
    && (await dialog(moved.page).locator('[data-fix-preview]').innerText()).includes('对不上了'))
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
  check('只一句安静的说明：点发布时门禁照旧检查', (await dialog(page).locator('[data-preflight-unsupported]').innerText().catch(() => '')).includes('门禁照旧检查'))
  check('发布按钮可用', await submit(page).isEnabled())
  await submit(page).click()
  await waitFor(page, async () => (await dialog(page).locator('[data-preflight-issue]').count()) > 0)
  const summary = await dialog(page).locator('[data-preflight-summary]').innerText().catch(() => '')
  check('点了发布被拦：照旧列出门禁拦下的问题', summary.includes('门禁拦下了 5 处') && await dialog(page).locator('[data-preflight-issue]').count() === 6, summary)
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

await section('同一个修复只画一次：validate 和门禁各报一条、几条问题共用一个图级修复', async () => {
  const { page, state, errors } = await open({ id: 'pf-dup' })
  await openDialog(page)
  await waitFor(page, async () => (await dialog(page).locator('[data-preflight-issue]').count()) > 0)
  const rows = await dialog(page).locator('[data-preflight-issue]').count()
  check('同 code、同节点的两条合成一行', rows === 3
    && await dialog(page).locator('[data-preflight-issue="contract.metrics_from_missing"]').count() === 1, `${rows} 行`)
  const summary = await dialog(page).locator('[data-preflight-summary]').innerText().catch(() => '')
  check('……结论按一处算：会被门禁拦下 3 处', summary.includes('会被门禁拦下 3 处'), summary)
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
  check('图级改动的预览：「整张工作流」 · 全图默认 · 审批策略：全部自动放行 → 仅危险工具需要审批',
    /「整张工作流」\s*·\s*全图默认 · 审批策略：\s*全部自动放行\s*仅危险工具需要审批/.test(line), line)
  await dialog(page).getByRole('button', { name: '放弃', exact: true }).click()
  // 真点了发布被拦：/publish 的回包只有 code 没有 fix。图级修复按 code 认回来，照样只画一次
  await submit(page).click()
  await waitFor(page, async () => state.publishes.length === 1
    && (await dialog(page).locator('[data-preflight-summary]').innerText().catch(() => '')).includes('门禁拦下了'))
  const gateSummary = await dialog(page).locator('[data-preflight-summary]').innerText().catch(() => '')
  check('被拦之后：照样合成一行（门禁拦下了 3 处）', gateSummary.includes('门禁拦下了 3 处')
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
    && (await dialog(page).locator('[data-preflight-summary]').innerText().catch(() => '')).includes('门禁拦下了'))
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
    && (await dialog(bad.page).locator('[data-fix-save-failed]').innerText().catch(() => '')).includes('草稿没保存上')
    && await dialog(bad.page).getByRole('button', { name: '重试保存' }).count() === 1)
  check('……发布照旧不可用，说的是修复还没保存上', await submit(bad.page).isDisabled()
    && await submit(bad.page).getAttribute('title') === '修复还没保存上', await submit(bad.page).getAttribute('title'))
  bad.state.save = 'ok'
  await dialog(bad.page).getByRole('button', { name: '重试保存' }).click()
  await waitFor(bad.page, async () => bad.state.checks.length === 3 && await submit(bad.page).isEnabled())
  check('……重试保存：存上、按换过的等级重查、发布可用', bad.state.patches.length === 2 && bad.state.checks[2]?.level === 'governed'
    && await submit(bad.page).isEnabled() && await dialog(bad.page).locator('[data-fix-save-failed]').count() === 0)
})

await section('保存和页面自己的保存同一套规矩：回滚说明保留、画布锁着不存', async () => {
  const { page, state } = await open({ id: 'pf-easy' })
  // 刚从版本历史恢复了旧版本：页面自己的保存会把「回滚到 v2」写进版本说明
  await page.evaluate(() => window.__studio.setState({ pendingNote: '回滚到 v2' }))
  await openDialog(page)
  await dialog(page).locator('[data-fix-all]').click()
  await dialog(page).locator('[data-fix-apply]').click()
  await waitFor(page, async () => state.patches.length === 1)
  check('版本说明保留「回滚到 v2」，后面接着写发布前修复', /^回滚到 v2；发布前修复：/.test(state.patches[0]?.note ?? ''), state.patches[0]?.note)

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
  check('没选之前，按钮不是「交给 Copilot」的样子', await g4.locator('[data-fix-handoff]').count() === 0)
  await g4.getByText('它在做计算', { exact: false }).click()
  const button = g4.locator('[data-fix-action="choice"]')
  check('选了「交给 Copilot」那一项，按钮换成「交给 Copilot」', await button.getAttribute('data-fix-handoff') === ''
    && (await button.innerText()).includes('交给 Copilot'), await button.innerText())
  await button.click()
  const loading = dialog(page).locator('[data-fix-preview="loading"]')
  await loading.waitFor()
  check('等待时写「Copilot 正在试着修」，不是「正在生成修复预览」', (await loading.innerText()).includes('Copilot 正在试着修'), await loading.innerText())
  const body = state.autofixes.at(-1)
  check('请求带 assist: true，选的值原样带上', body?.assist === true && body?.choices?.['governed.caliber_compute_input:calc'] === 'copilot'
    && JSON.stringify(body?.apply) === JSON.stringify(['governed.caliber_compute_input:calc']), JSON.stringify(body))
  await dialog(page).locator('[data-fix-preview="ready"], [data-fix-preview]:not([data-fix-preview="loading"])').first().waitFor()
  check('Copilot 的总结摆出来了', await waitFor(page, async () => (await dialog(page).locator('[data-fix-assist]').count()) > 0))
  await shoot(page, 'publish-dialog-handoff', dialog(page))

  // 另一个 choice：唯一的候选是 id 叫 copilot 的报告撰写节点——普通候选，不交给 Copilot
  const { page: page2, state: state2, errors: errors2 } = await open({ id: 'pf-handoff', fixDelay: 1200 })
  await openDialog(page2)
  const exit = dialog(page2).locator('[data-fix-choice="governed.exit_text_source:done"]')
  await exit.waitFor()
  await exit.locator('input[type="radio"]').first().check()
  const pick = exit.locator('[data-fix-action="choice"]')
  check('选了 id 叫 copilot 的报告：按钮不变成「交给 Copilot」', await pick.getAttribute('data-fix-handoff') === null
    && !(await pick.innerText()).includes('交给 Copilot'), await pick.innerText())
  await pick.click()
  const wait2 = dialog(page2).locator('[data-fix-preview="loading"]')
  await wait2.waitFor()
  check('等待时写「正在生成修复预览」', (await wait2.innerText()).includes('正在生成修复预览'), await wait2.innerText())
  const body2 = state2.autofixes.at(-1)
  check('请求不带 assist，选的值是 copilot（节点 id）', !body2?.assist && body2?.choices?.['governed.exit_text_source:done'] === 'copilot',
    JSON.stringify(body2))
  check('没有页面错误', !errors.length && !errors2.length, [...errors, ...errors2].join(' | '))
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
