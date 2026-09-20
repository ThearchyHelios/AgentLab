// 事件解码器的回归检查。
//
// 用**真实运行**导出的事件跑（fixtures.json 取自 data/agentlab.db），不是构造的样本：
// 这里要守的恰恰是真实事件的脏细节——审批会重放、成对事件字段不齐、
// 空 preview、节点没起过名字。构造的样本永远不会长成那样。
// 库名表名已脱敏（改的只是标识符字面量，事件的结构和脏细节原样保留）。
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
  check('查询结果和 SQL 分开存，不互相覆盖', !!q?.result && q.result !== q.detail)
  check('工具调用挂在节点下，不是平铺',
    steps.some((s) => s.kind === 'node' && s.children?.some((c) => c.kind === 'query')))
}

console.log('\n=== 知识检索 ===')
{
  // 以前检索只发一条 info 日志，轨迹里查不到"这句结论依据的是哪一段"。
  // 现在它是一条正经事件，而且带工件 id 可以下钻。
  const ev = (data) => [{ seq: 1, type: 'retrieve.end', node_id: 'kb', data }]

  const hit = mod.decodeRun(ev({
    collection: '手册', query: '管理员怎么定义', count: 3,
    top_score: 0.82, artifact: 'abc123', degraded: false,
  }))
  const s1 = flatten(hit).find((x) => x.kind === 'schema')
  check('检索步骤说人话', !!s1 && s1.title.includes('手册') && s1.title.includes('3 段'), s1?.title)
  check('问的是什么留在详情里', s1?.detail === '管理员怎么定义')
  check('带得出工件 id，能下钻', s1?.artifact === 'abc123')
  check('最高分露出来', !!s1?.meta?.includes('0.82'))

  const none = flatten(mod.decodeRun(ev({ collection: '手册', count: 0 })))
    .find((x) => x.kind === 'schema')
  check('没检索到要标成失败，不是静悄悄地过去', none?.status === 'failed')

  const bad = flatten(mod.decodeRun(ev({ collection: '手册', count: 2, degraded: true })))
    .find((x) => x.kind === 'schema')
  check('退回关键词这件事标出来了', bad?.level === 'warn' && !!bad?.meta?.includes('关键词'))

  // 兜底那条也要还在：新事件没补映射时宁可显示原始的
  const unknown = mod.decodeRun([{ seq: 1, type: 'brand.new', node_id: 'x', data: { a: 1 } }])
  check('未知事件不被静默吞掉', flatten(unknown).some((x) => x.title === 'brand.new'))
}

console.log('\n=== 长期记忆 ===')
{
  // 写记忆以前只发 info 日志，而 info 在这一层是被丢弃的——系统往长期记忆里
  // 存东西，界面上一点痕迹都没有。Copilot 会主动记之后，这条不可接受。
  const ev = (data) => [{ seq: 1, type: 'memory.end', node_id: 'mem', data }]

  const w = flatten(mod.decodeRun(ev({
    action: 'write', scope: 'default', count: 1,
    content: '用户姓名：张三；工作单位：示例科技',
  })))[0]
  check('写记忆看得见', !!w)
  check('把记了什么说出来，不是只报条数',
    !!w?.title?.includes('张三'), w?.title)
  check('全文留在详情里', !!w?.detail?.includes('示例科技'))

  const r = flatten(mod.decodeRun(ev({ action: 'recall', scope: 'default', count: 3 })))[0]
  check('召回说人话', !!r?.title?.includes('想起 3 条'), r?.title)
  const none = flatten(mod.decodeRun(ev({ action: 'recall', scope: 'default', count: 0 })))[0]
  check('没想起来要标出来，不是静悄悄地过去', none?.status === 'failed')

  const c = flatten(mod.decodeRun(ev({ action: 'clear', scope: 'default', count: 7 })))[0]
  check('清空是破坏性的，标成警告', c?.level === 'warn' && c.title.includes('7'))
}

console.log('\n=== 被截断的结果集 ===')
{
  // 后端按字符数硬切预览，切点落在 JSON 中间是常态。严格 JSON.parse 一律失败，
  // 于是最典型的一次取数运行，成果会变成满屏 \"attribute01\"。这几条守的就是它。
  const fin = fixtures.db.find((e) => e.type === 'run.finished')
  const raw = Object.values(fin.data.output).find((v) => typeof v === 'string')
  check('样本确实是坏 JSON（不然这组断言等于没测）', (() => {
    try { JSON.parse(raw); return false } catch { return true }
  })(), `${raw.length} 字`)

  const t = mod.parseQueryResult(raw)
  check('截断的结果集仍能解析出表格', !!t)
  check('列名完整', Array.isArray(t?.columns) && t.columns.length > 10, `${t?.columns?.length} 列`)
  check('至少救回一条完整记录', (t?.rows?.length ?? 0) >= 1, `${t?.rows?.length} 行`)
  check('每行长度和列数对得上', t?.rows?.every((r) => r.length === t.columns.length))
  check('标出这是被切断的预览，而不是查询上限', t?.clipped === true && !t?.truncated)

  // 完整的结果集不该被误判成截断
  const whole = JSON.stringify({ columns: ['a', 'b'], rows: [[1, 2], [3, 4]] })
  const w = mod.parseQueryResult(whole)
  check('完整结果集不标截断', w?.rows.length === 2 && !w.clipped)
  check('普通文本不会被硬认成表格', mod.parseQueryResult('查到 3 条记录') === null)
  check('空串不报错', mod.parseQueryResult('') === null)
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
  // 恢复过的运行有"开始执行"+"继续执行"两条生命周期。只收第一条的话，
  // "继续执行"会永远转圈——明明整条已经跑完了
  check('恢复过的运行不会留下转圈的行',
    !all.some((s) => s.status === 'running'),
    all.filter((s) => s.status === 'running').map((s) => s.title).join(','))
  check('处理完的审批不再是待办色',
    !all.some((s) => s.status === 'done' && s.level === 'warn'))
}

console.log('\n=== 停在审批时的状态 ===')
{
  // 只喂到中断为止，模拟"运行正卡在人工介入上"这一刻
  const upto = fixtures.human.slice(0, fixtures.human.findIndex((e) => e.type === 'run.interrupted') + 1)
  const all = flatten(mod.decodeRun(upto))
  const host = all.find((s) => s.kind === 'node' && s.nodeId === 'h')

  // 转圈说的是"它在忙，你等着"，而实际情况正相反：它在等你。
  // 两种状态的行动含义完全相反，不能共用一个图标
  check('被中断的节点是"等你"而不是"在跑"', host?.status === 'waiting', host?.status)
  check('那一刻没有任何东西在转圈',
    !all.some((s) => s.kind === 'node' && s.status === 'running'),
    all.filter((s) => s.status === 'running').map((s) => s.title).join(','))

  // 恢复之后它得回到"在跑"，否则整条流永远显示成在等人
  const after = mod.decodeRun(fixtures.human)
  const doneHost = flatten(after).find((s) => s.kind === 'node' && s.nodeId === 'h')
  check('恢复后回到正常状态', doneHost?.status === 'done', doneHost?.status)

  // "在不在等人"必须从事件推。查审批列表的话要等 4 秒轮询，中断后那几秒
  // 界面会说"这次运行已结束"，而它其实正等着你点通过
  check('从事件就能判断正在等人', mod.isAwaitingHuman(mod.decodeRun(upto)) === true)
  check('跑完了就不再说在等人', mod.isAwaitingHuman(after) === false)
  check('压根没有人工节点的运行也不误报',
    mod.isAwaitingHuman(mod.decodeRun(fixtures.db)) === false)
}

console.log('\n=== 循环里的多轮审批 ===')
{
  // 真实运行：驳回 → 改写 → 再驳回（带备注）→ 改写 → 放行。三轮，每轮都有
  // 重放。按内容去重必然出错——三轮的 title 一模一样，和重放无法区分。
  // 这条只有拿真实运行才测得出来：构造样本不会长成这样。
  const steps = mod.decodeRun(fixtures.loop_approve)
  const all = flatten(steps)
  const asks = all.filter((s) => s.kind === 'human')
  check('三轮审批就是三条，不多不少', asks.length === 3, `${asks.length} 条`)
  check('每条都带着你的决定', asks.every((s) => /你(放行|驳回)了/.test(s.title)),
    asks.map((s) => s.title).join(' | '))
  check('两次驳回一次放行，顺序没错',
    asks.filter((s) => s.title.includes('驳回')).length === 2
    && asks[2]?.title.includes('放行'))
  // detail 里是被审的草稿全文，打出来会刷屏——只报哪一轮带了备注
  check('备注跟着那一轮走', asks.some((s) => s.detail?.includes('不高级')),
    asks.map((s, i) => (s.detail?.includes('你的备注') ? `#${i + 1} 有备注` : '')).filter(Boolean).join(' '))
  check('没有落单的"你放行了/你驳回了"孤行',
    !all.some((s) => s.title === '你放行了' || s.title === '你驳回了'))
  check('重放不制造重复节点', (() => {
    const ids = steps.filter((s) => s.kind === 'node').map((s) => s.nodeId)
    return new Set(ids).size === ids.length
  })())
  check('跑完了没有还在转的行', !all.some((s) => s.status === 'running'))
}

console.log('\n=== 多 agent 协作 ===')
{
  // 真实运行 #6038f5：supervisor 派给 researcher 一次就 FINISH 了。
  // 界面上那个多智能体节点跑完 31.7s 之后，researcher 那行还在转圈。
  const steps = mod.decodeRun(fixtures.supervisor)
  const all = flatten(steps)

  const agentRows = all.filter((s) => s.title.startsWith('researcher'))
  check('一个 agent 步骤就是一行，不是"派任务"+"回复"两行',
    agentRows.length === 1, `${agentRows.length} 行：${agentRows.map((s) => s.title.slice(0, 20)).join(' | ')}`)
  check('回复挂在同一行里可展开', !!agentRows[0]?.result)
  check('跑完了就不转圈了', agentRows[0]?.status === 'done', agentRows[0]?.status)

  // supervisor 的调度理由被后端标成了 info 日志，整类丢掉的话，
  // 多 agent 节点在界面上就是一个跑了 31 秒的黑盒
  const routes = all.filter((s) => s.kind === 'branch')
  check('调度决策要说出来', routes.length === 2, `${routes.length} 条`)
  check('说清楚第几轮交给谁',
    routes[0]?.title.includes('第 1 轮') && routes[0]?.title.includes('researcher'),
    routes[0]?.title)
  // FINISH 是协议里的收尾标记，不是某个 agent
  check('为什么结束也要有交代，且不说"交给 FINISH"',
    routes[1]?.title.includes('结束协作') && !routes[1]?.title.includes('FINISH')
    && !!routes[1]?.detail,
    `${routes[1]?.title} / ${routes[1]?.detail?.slice(0, 24)}`)
  check('普通 info 日志仍然不进主流程',
    !all.some((s) => s.title.includes('校验通过')))
  check('warn 日志照常显示', all.some((s) => s.title.includes('校验失败')))
  check('整条流跑完没有转圈的行', !all.some((s) => s.status === 'running'),
    all.filter((s) => s.status === 'running').map((s) => s.title.slice(0, 24)).join(','))
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

console.log('\n=== 并行分支 ===')
{
  // 图上一个节点连出多条边就是 fan-out，LangGraph 在同一个 superstep 里并发
  // 执行——这是真并发。但在时间线上它们原来只是穿插出现的几行，
  // "这三路是同时跑的、因此省下了 3.6 秒"一个字都没说。
  // 这份样本是真跑出来的：三路分别 sleep 1.8 / 2.6 / 1.1 秒。
  const steps = mod.decodeRun(fixtures.fanout)
  const group = steps.find((s) => /路并行/.test(s.title))
  check('认出了并行的那一批', !!group, steps.map((s) => s.title).join(' | '))
  check('三路都在组里', group?.children?.length === 3, `${group?.children?.length} 路`)
  check('省下多少说出来了', /合计 6\.\ds，实际 3\.\ds/.test(group?.meta ?? ''), group?.meta)
  // 顺序执行的图不能被误判成并行——误报比漏报更糟，它会让人以为省了时间
  for (const name of ['db', 'human', 'loop_approve']) {
    const seq = mod.decodeRun(fixtures[name])
    check(`顺序执行的 ${name} 没有被误判`, !seq.some((s) => /路并行/.test(s.title)))
  }
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
  // 一屏"0ms"看着像每步都被精确计时，实际只是这些步骤没花时间，
  // 反而把真正慢的那一步淹了
  // 用词边界，不然 "130ms" 里的 "0ms" 会误判
  check('不显示 0ms 这种没信息量的耗时', !all.some((s) => /\b0ms\b/.test(s.meta ?? '')),
    all.filter((s) => /\b0ms\b/.test(s.meta ?? '')).map((s) => s.title).join(','))
  check('没有空 meta 占位', !all.some((s) => s.meta === ''))
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
  const mid = mod.decodeCopilot(ops.slice(0, 2))
  check('心跳不堆行', mid.filter((s) => s.kind === 'lifecycle' && s.status === 'running').length === 1)

  const steps = mod.decodeCopilot(ops)
  check('连续思考并成一条', steps.filter((s) => s.kind === 'think').length === 1,
    `${steps.filter((s) => s.kind === 'think').length} 条`)
  check('思考全文保留在详情', steps.find((s) => s.kind === 'think')?.detail?.includes('先看表结构'))

  // 编排期那 50 秒里，界面上只有一行不动的"正在理解需求"。思考是流式的，
  // 固定显示开头那句等于把一段活的叙述冻在起点上，看着就像卡住了
  const thinkingNow = mod.decodeCopilot([
    { op: 'thinking', delta: '用户想要一个能查销量的流程。' },
    { op: 'thinking', delta: '先看看画布上现有的节点有哪些。' },
    { op: 'thinking', delta: '这里需要一个分支' },
  ])
  const live = thinkingNow[thinkingNow.length - 1]
  check('还在想的时候显示最新一句', live.title.includes('这里需要一个分支'), live.title)
  check('还在想的时候是进行中状态', live.status === 'running', live.status)
  check('全文一句不丢', live.detail?.includes('用户想要') && live.detail?.includes('现有的节点'))

  // 想完了它就是条历史记录，开头那句最接近"这段在想什么"
  const settled = mod.decodeCopilot([
    ...thinkingNow.length ? [
      { op: 'thinking', delta: '用户想要一个能查销量的流程。' },
      { op: 'thinking', delta: '先看看画布上现有的节点有哪些。' },
    ] : [],
    { op: 'plan', summary: '三步走' },
  ])
  const done = settled.find((s) => s.kind === 'think')
  check('想完了换成开头那句', done?.title.startsWith('用户想要'), done?.title)
  check('想完了不再转圈', done?.status === 'done', done?.status)
  check('连线不单独成行', !steps.some((s) => s.title.includes('edge')))
  check('节点用标签不用 id', steps.some((s) => s.kind === 'node' && s.title === '取数'))
  // 生成完了那条"正在理解需求…"还在转圈的话，看上去像卡住了
  check('生成结束后没有还在转的行', !steps.some((s) => s.status === 'running'),
    steps.filter((s) => s.status === 'running').map((s) => s.title).join(','))

  // 改图场景：这一轮加了 1 个节点，但整张图有 4 个。说"共 1 步"是错的
  const edited = mod.decodeCopilot([
    ...ops,
    { op: 'final', graph: { nodes: [{ id: 'a' }, { id: 'q' }, { id: 'b' }, { id: 'c' }] } },
  ])
  const tail = edited[edited.length - 1]
  check('改图时说清"加了几步"和"整张图几步"',
    tail.title.includes('加了 1 步') && tail.title.includes('共 4 步'), tail.title)

  const built = mod.decodeCopilot([
    { op: 'add_node', node: { id: 'a', type: 'input' } },
    { op: 'add_node', node: { id: 'b', type: 'output' } },
    { op: 'done', explanation: '' },
    { op: 'final', graph: { nodes: [{ id: 'a' }, { id: 'b' }] } },
  ])
  check('从零新建时不啰嗦，只说共几步',
    built[built.length - 1].title === '流程搭好了，共 2 步',
    built[built.length - 1].title)
}

console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 解码器全部通过')
process.exit(failed ? 1 : 0)
