// 事件解码器的回归检查。
//
// 用**真实运行**导出的事件跑（fixtures.json 取自 data/agentlab.db），不是构造的样本：
// 这里要守的恰恰是真实事件的脏细节——审批会重放、成对事件字段不齐、
// 空 preview、节点没起过名字。构造的样本永远不会长成那样。
//
// 转译借 vite dev server（它 serve 的就是应用实际运行的那份），所以跑之前
// 前端得起着：./scripts/dev.sh
import { readFileSync } from 'node:fs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const root = new URL('..', import.meta.url).pathname
const fixtures = JSON.parse(
  readFileSync(`${root}frontend/src/run/__tests__/fixtures.json`, 'utf8'))

const res = await fetch(`${WEB}/src/run/decode.ts?t=${Date.now()}`)
if (!res.ok) {
  console.error(`✗ 拿不到转译结果（${WEB}）——前端没起？先跑 ./scripts/dev.sh`)
  process.exit(1)
}
const mod = await import(
  'data:text/javascript;base64,' + Buffer.from(await res.text()).toString('base64'))

let failed = 0
const check = (name, cond, detail = '') => {
  console.log(`  ${cond ? '✓' : '✗'} ${name}${detail ? ` — ${detail}` : ''}`)
  if (!cond) failed++
}
const flatten = (steps) =>
  steps.flatMap((s) => [s, ...(s.children ? flatten(s.children) : [])])

console.log('=== 数据库查询 ===')
{
  const steps = mod.decodeRun(fixtures.db)
  const all = flatten(steps)
  const q = all.find((s) => s.kind === 'query')
  check('查询步骤说人话，不是 db_query__warehouse(...)',
    !!q && q.title.includes('查询数据') && !q.title.includes('db_query__'), q?.title)
  check('SQL 原文留在详情里可展开', !!q?.detail?.includes('SELECT'))
  check('工具调用挂在节点下，不是平铺',
    steps.some((s) => s.kind === 'node' && s.children?.some((c) => c.kind === 'query')))
}

console.log('\n=== 人工审批（含重放）===')
{
  const steps = mod.decodeRun(fixtures.human)
  const all = flatten(steps)
  // 一次审批会产生 human.requested ×2（重放）+ run.interrupted，界面上只该有一条
  check('"等你确认"只出现一次',
    all.filter((s) => s.kind === 'human' && s.title.includes('确认')).length === 1,
    `${all.filter((s) => s.kind === 'human' && s.title.includes('确认')).length} 条`)
  check('"继续执行"只出现一次',
    steps.filter((s) => s.title === '继续执行').length === 1,
    `${steps.filter((s) => s.title === '继续执行').length} 条`)
  // 节点重放不该让同一个节点在时间线上出现两遍
  const nodeIds = steps.filter((s) => s.kind === 'node').map((s) => s.nodeId)
  check('节点不因重放而重复', new Set(nodeIds).size === nodeIds.length,
    nodeIds.join(','))
  check('审批结果有交代', all.some((s) => s.title.includes('放行') || s.title.includes('驳回')))
}

console.log('\n=== 出具判定 ===')
{
  const steps = mod.decodeRun(fixtures.issue)
  check('出具档位翻译成中文', steps.some((s) => s.kind === 'issuance' && s.title.includes('出具')),
    steps.find((s) => s.kind === 'issuance')?.title)
}

console.log('\n=== 失败运行 ===')
{
  const steps = mod.decodeRun(fixtures.failed)
  const all = flatten(steps)
  const errs = all.filter((s) => s.level === 'error')
  check('错误只说一遍（节点失败与 run.failed 是同一件事）',
    new Set(errs.map((s) => s.detail ?? s.title)).size === errs.length,
    `${errs.length} 条错误`)
  check('开头那条不会一直转圈',
    !steps.some((s) => s.kind === 'lifecycle' && s.status === 'running'))
}

console.log('\n=== 通用规则 ===')
{
  const all = Object.values(fixtures).flatMap((evts) => flatten(mod.decodeRun(evts)))
  check('没有裸节点 id 当标题',
    !all.some((s) => s.kind === 'node' && /^(in|out|h|m|t\d|n\d)$/.test(s.title)),
    all.filter((s) => s.kind === 'node' && /^(in|out|h|m)$/.test(s.title)).map(s=>s.title).join(','))
  check('没有空详情', !all.some((s) => s.detail === '{}' || s.detail === ''))
  check('没有原始事件名泄漏到标题',
    !all.some((s) => /^(node|run|llm|tool)\./.test(s.title)),
    all.filter((s) => /^(node|run|llm|tool)\./.test(s.title)).map(s=>s.title).join(','))
  check('耗时是人类单位', all.filter((s) => s.meta).every((s) => /(\d+ms|\d+\.\d+s|\d+m\d+s|\d+ 行)/.test(s.meta)))
}

console.log('\n=== Copilot 操作流 ===')
{
  const ops = [
    { op: 'heartbeat', phase: 'planning', elapsed_ms: 3000 },
    { op: 'heartbeat', phase: 'planning', elapsed_ms: 6000 },
    { op: 'thinking', delta: '用户要查销量。' },
    { op: 'thinking', delta: '先看表结构。' },
    { op: 'plan', summary: '三步：取数→汇总→出具' },
    { op: 'add_node', node: { id: 'q', type: 'tool', data: { label: '取数' } } },
    { op: 'add_edge', edge: { source: 'a', target: 'q' } },
    { op: 'done', explanation: '完成' },
  ]
  const steps = mod.decodeCopilot(ops)
  check('心跳不堆行', steps.filter((s) => s.kind === 'lifecycle' && s.status === 'running').length === 1)
  check('连续思考并成一条', steps.filter((s) => s.kind === 'think').length === 1,
    `${steps.filter((s) => s.kind === 'think').length} 条`)
  check('思考全文保留在详情', steps.find((s) => s.kind === 'think')?.detail?.includes('先看表结构'))
  check('连线不单独成行', !steps.some((s) => s.title.includes('edge')))
  check('节点用标签不用 id', steps.some((s) => s.kind === 'node' && s.title === '取数'))
}

console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 解码器全部通过')
process.exit(failed ? 1 : 0)
