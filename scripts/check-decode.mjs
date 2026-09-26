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

// decode.ts 在运行时 import 了 lib/format、lib/terms。data: URL 里的模块没法解析
// "/src/..." 这种路径，所以把依赖也各自转成 data: URL 再替换进去（同 check-trace）
const loaded = new Map()
async function load(path) {
  if (loaded.has(path)) return loaded.get(path)
  const res = await fetch(`${WEB}${path}?t=${Date.now()}`)
  if (!res.ok) throw new Error(`${path} → HTTP ${res.status}`)
  let code = await res.text()
  const deps = new Set([...code.matchAll(/from\s+["'](\/src\/[^"'?]+)(?:\?[^"']*)?["']/g)]
    .map((m) => m[1]))
  for (const dep of deps) {
    const url = await load(dep)
    const pattern = new RegExp(`(["'])${dep.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(\\?[^"']*)?\\1`, 'g')
    code = code.replace(pattern, JSON.stringify(url))
  }
  const url = 'data:text/javascript;base64,' + Buffer.from(code).toString('base64')
  loaded.set(path, url)
  return url
}

let mod, synthetic
try {
  mod = await import(await load('/src/run/decode.ts'))
  synthetic = await import(await load('/src/run/__tests__/synthetic.ts'))
} catch (e) {
  console.error(`✗ 拿不到转译结果（${WEB}）——前端没起？先跑 ./scripts/dev.sh\n  ${e.message}`)
  process.exit(1)
}

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
  // 一次取数常有五六条查询，标题全写「在 X 上查询数据」只能靠 meta 分。
  // 标题从 SQL 里取表名：这条是 SELECT * FROM ANALYTICS.v_device_kpi FETCH FIRST 50
  check('查询步骤说人话，不是 db_query__warehouse(...)',
    !!q && q.title.startsWith('查询') && !q.title.includes('db_query__'), q?.title)
  check('查询标题说出查的是哪张表', !!q?.title.includes('v_device_kpi'), q?.title)
  check('数据源名留给展开区，不挤标题', q?.source === 'warehouse' && !q.title.includes('warehouse'))
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

  // 兜底那条也要还在：新事件没补映射时宁可显示一条，但内部类型名不当标题
  const unknown = flatten(mod.decodeRun([{ seq: 1, type: 'brand.new', node_id: 'x', data: { a: 1 } }]))
  check('未知事件不被静默吞掉', unknown.some((x) => x.detail?.startsWith('事件类型：brand.new')),
    unknown.map((x) => x.title).join(','))
  check('未知事件的类型名不上标题和副标题', !unknown.some((x) => x.title.includes('brand.new') || x.sub?.includes('brand.new')))
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
  // 老数据没有签批人：只说结果，不再一律写"你"——多人使用时批的可能是别人
  check('每条都带着决定', asks.every((s) => /已(放行|驳回)/.test(s.title)),
    asks.map((s) => s.title).join(' | '))
  check('不再替人署名为"你"', !asks.some((s) => s.title.includes('你')))
  check('两次驳回一次放行，顺序没错',
    asks.filter((s) => s.title.includes('驳回')).length === 2
    && asks[2]?.title.includes('放行'))
  // detail 里是被审的草稿全文，打出来会刷屏——只报哪一轮带了备注
  check('备注跟着那一轮走', asks.some((s) => s.detail?.includes('不高级')),
    asks.map((s, i) => (s.detail?.includes('备注') ? `#${i + 1} 有备注` : '')).filter(Boolean).join(' '))
  check('没有落单的"已放行/已驳回"孤行',
    !all.some((s) => s.title === '已放行' || s.title === '已驳回'))
  // 同一个节点执行了三次（驳回两次后重来）：仍然只占一行，每次执行记在 execs 里，
  // 子步骤按轮标号——以前三轮的子步骤平铺在同一个父节点下，分不清哪条是哪一轮
  const review = steps.find((s) => s.nodeId === 'review')
  check('三轮审批记成同一节点的三次执行', review?.execs?.length === 3, `${review?.execs?.length}`)
  check('每轮的审批行标着自己是第几次', asks.map((s) => s.iter).join(',') === '1,2,3',
    asks.map((s) => s.iter).join(','))
  const rewrite = steps.find((s) => s.nodeId === 'rewrite')
  check('执行多次的节点行尾说几次、共多久', /^×2 · 共 /.test(rewrite?.meta ?? ''), rewrite?.meta)
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
  check('省下多少说出来了', /合计 6\.\d s，实际 3\.\d s/.test(group?.meta ?? ''), group?.meta)
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
  // 全站只有一套写法（lib/format）：「820 ms」「7.6 s」「1 分 14 秒」。旧的「1m14s」
  // 「2min」「12345ms」一个都不能再出现
  const metas = all.map((s) => s.meta).filter(Boolean)
  check('耗时是全站统一的写法', metas.every((m) => !/\d(ms|s|min)\b|\dm\d/.test(m)),
    metas.filter((m) => /\d(ms|s|min)\b|\dm\d/.test(m)).join(' | '))
  // 一屏"0 ms"看着像每步都被精确计时，实际只是这些步骤没花时间，
  // 反而把真正慢的那一步淹了
  check('不显示 0 ms 这种没信息量的耗时', !metas.some((m) => /(^|[^\d.])0 ms/.test(m)),
    all.filter((s) => /(^|[^\d.])0 ms/.test(s.meta ?? '')).map((s) => s.title).join(','))
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

  // 自查：服务端用运行时同一套规则过一遍，有问题交回模型改。多出来的这段得看得见，
  // 而且它排在"搭好了"后面，不能把那一行的"共几步"挤没了
  const checked = mod.decodeCopilot([
    { op: 'add_node', node: { id: 'a', type: 'input' } },
    { op: 'add_node', node: { id: 'lp', type: 'loop' } },
    { op: 'done', explanation: '' },
    { op: 'check', status: 'repairing', round: 1, issues: ['「lp」循环条件写错了：表达式里没有 | 过滤器'] },
    { op: 'heartbeat', phase: 'repairing', elapsed_ms: 3000 },
    { op: 'update_node', id: 'lp' },
    { op: 'check', status: 'passed', repaired: 1 },
    { op: 'final', graph: { nodes: [{ id: 'a' }, { id: 'lp' }] } },
  ])
  const titles = checked.map((s) => s.title)
  check('自查发现问题要说出来', titles.some((t) => t.includes('自查发现 1 处问题')), titles.join(' | '))
  check('交回去改的是哪条要看得到',
    checked.some((s) => (s.detail ?? '').includes('循环条件写错了')))
  check('改好了要说', titles.includes('自查通过：问题已经改好'), titles.join(' | '))
  check('自查插在后面也不挤掉"共几步"', titles.includes('流程搭好了，共 2 步'), titles.join(' | '))
  check('自查结束后没有还在转的行', !checked.some((s) => s.status === 'running'),
    checked.filter((s) => s.status === 'running').map((s) => s.title).join(','))

  const stuck = mod.decodeCopilot([
    { op: 'done', explanation: '' },
    { op: 'check', status: 'failed', issues: ['「lp」循环条件写错了', '「t」整形节点没有填表达式'] },
    { op: 'final', graph: { nodes: [{ id: 'lp' }, { id: 't' }] } },
  ])
  const failedCheck = stuck.find((s) => s.kind === 'error')
  check('改不好要明说、说清没有自动运行',
    !!failedCheck?.title.includes('还有 2 处问题') && failedCheck.title.includes('没有自动运行'),
    failedCheck?.title)
  // 画布上从不自动运行：「没有自动运行」在那儿读起来像出了别的故障
  const onCanvas = mod.decodeCopilot([
    { op: 'check', status: 'failed', issues: ['「lp」循环条件写错了'] },
  ], { context: 'canvas' }).find((s) => s.kind === 'error')
  check('画布语境说「没能自动修好」', !!onCanvas?.title.includes('没能自动修好')
    && !onCanvas.title.includes('自动运行'), onCanvas?.title)
}

console.log('\n=== 协作团队：调度者的决策（新后端的 agent.route.*）===')
{
  // 以前没有映射，每一轮多出两行标题是「agent.route.start」「agent.route.end」的原始类型名
  const done = mod.decodeRun(synthetic.teamRun('done'))
  const all = flatten(done)
  const routes = all.filter((s) => s.kind === 'branch' && s.nodeId === 'team')
  check('每轮调度只有一行（route 事件和同一轮的 log 不重复）', routes.length === 2,
    routes.map((s) => s.title).join(' | '))
  check('说清交给谁、是不是并行', routes[0]?.title === '第 1 轮：交给 采购员、质检员、物流员（并行）', routes[0]?.title)
  check('调度花了多久写在行尾', routes[0]?.meta === '2.4 s', routes[0]?.meta)
  check('调度理由可展开', routes[0]?.detail === '三方面数据互不依赖，可以同时查')
  check('结束协作说成人话', routes[1]?.title === '第 2 轮：结束协作', routes[1]?.title)
  check('成员和调度者的 llm.end 不生成孤立的"思考并作答"',
    !all.some((s) => s.kind === 'llm' && s.nodeId === 'team'))
  const rawTitle = /^[a-z]+\.[a-z.]+$/
  const everywhere = [
    ...Object.values(fixtures).flatMap((evts) => flatten(mod.decodeRun(evts))),
    ...flatten(done), ...flatten(mod.decodeRun(synthetic.mixedRun())),
    ...flatten(mod.decodeRun(synthetic.longLoop(12))), ...flatten(mod.decodeRun(synthetic.cancelledRun())),
  ]
  check('没有任何一行的标题是内部类型名', !everywhere.some((s) => rawTitle.test(s.title) || s.title.startsWith('agent.')),
    everywhere.filter((s) => rawTitle.test(s.title)).map((s) => s.title).join(','))

  // 调度者还在想：那几十秒里得有一行在走
  const routing = flatten(mod.decodeRun(synthetic.teamRun('routing')))
  const thinking = routing.find((s) => s.kind === 'branch' && s.status === 'running')
  check('调度期间有一行进行中的「调度者在想下一步」', !!thinking?.title.includes('调度者在想'), thinking?.title)
  check('进行中的调度行带开始时刻（界面据此走秒表）', typeof thinking?.startedAt === 'number')

  // 三人并行、只有一人交回：「省下」要等这一轮的人都交回来才说
  const live = mod.decodeRun(synthetic.teamRun('members'))
  const team = live.find((s) => s.nodeId === 'team')?.team
  check('一轮没收齐时不报「省下」', team?.savedMs === 0, `${team?.savedMs}`)
  check('还在跑的成员是 running', team?.rounds[0]?.members.filter((m) => m.status === 'running').length === 2)
  const finished = done.find((s) => s.nodeId === 'team')?.team
  check('收齐之后才算出省下的时间', (finished?.savedMs ?? 0) > 0, `${finished?.savedMs}`)

  // 用量：运行中按 llm.end 累加（含成员、调度者），终态以 run.finished 为准
  const partial = mod.usageOf(synthetic.teamRun('members'))
  check('运行中的用量把调度者和成员都算上', partial.tokensIn === 820 + 640 && !partial.final,
    `${partial.tokensIn} / final=${partial.final}`)
  const total = mod.usageOf(synthetic.teamRun('done'))
  check('终态用后端累计的总数', total.final && total.tokensIn === 4470 && total.tokensOut === 946,
    `${total.tokensIn}/${total.tokensOut}`)
  check('终态带着执行时长', total.activeMs === 12300, `${total.activeMs}`)
}

console.log('\n=== 被跳过的节点、思考、查询标题、技术细节、签批人、出具缺口 ===')
{
  const steps = mod.decodeRun(synthetic.mixedRun())
  const all = flatten(steps)
  const skipped = steps.find((s) => s.nodeId === 'kb')
  // skip_if 成立的节点以前被整类丢掉，和"根本没轮到"分不开
  check('被跳过的节点留痕', skipped?.status === 'skipped' && !!skipped.title.includes('跳过「背景检索」'),
    `${skipped?.status} ${skipped?.title}`)
  check('跳过的原因写出来', !!skipped?.sub?.includes('skip_if 成立'), skipped?.sub)

  // 思考不单独成行：挂到它所属的那次模型调用上当副标题
  check('思考不再单独成行', !all.some((s) => s.kind === 'think'))
  const llm = all.find((s) => s.kind === 'llm' && s.sub)
  check('思考成了模型调用的副标题', !!llm?.sub?.includes('先看一下考勤'), llm?.sub)
  check('思考全文还在展开区', !!llm?.detail?.includes('按班次汇总出勤率'))

  const q1 = all.find((s) => s.kind === 'query' && s.status === 'done')
  check('查询标题从 SQL 派生：表名 + 汇总维度',
    !!q1?.title.includes('v_device_kpi') && q1.title.includes('按 shift_name 汇总'), q1?.title)
  check('查询带着工件 id，可以下钻', !!q1?.artifact?.startsWith('4790dbfc'))
  const q2 = all.find((s) => s.kind === 'query' && s.status === 'failed')
  check('查询报错的原始异常收进技术细节', !!q2?.raw?.includes('OperationalError'), q2?.raw?.slice(0, 40))
  check('报错的人话还在正文', !!q2?.result?.includes('查询超时'))

  // 一次看十几张表：标题摘要，完整清单逐行放进详情（以前标题撑破卡片、页面横向滚动）
  const schema = all.find((s) => s.kind === 'schema')
  check('多表结构的标题不超过 60 字', (schema?.title.length ?? 99) <= 60, `${schema?.title.length}：${schema?.title}`)
  check('说出一共几张表', !!schema?.title.includes('12 张表'), schema?.title)
  check('完整表名逐行在详情里', schema?.detail?.split('\n').length === 12)

  const human = all.find((s) => s.kind === 'human')
  check('签批人照事件写', !!human?.title.endsWith('→ 张工 放行了'), human?.title)
  check('备注跟着那一轮', !!human?.detail?.includes('备注：晚班偏低要跟进'))
  const resume = steps.find((s) => s.title === '继续执行')
  check('续跑是谁发起的写在副标题', !!resume?.sub?.includes('张工'), resume?.sub)

  const issuance = steps.find((s) => s.kind === 'issuance')
  // 只有 gaps 时以前只写一个光秃秃的「降档出具」
  check('降档的原因写进出具那一行', !!issuance?.title.includes('校验不完整') && issuance.title.includes('叙述模板渲染为空'),
    issuance?.title)
  check('出具档位用统一叫法', !!issuance?.title.startsWith('降档出具'))

  const notify = steps.find((s) => s.nodeId === 'notify')
  check('节点失败的原始异常收进技术细节', notify?.status === 'failed' && !!notify.raw?.includes('ConnectError'))
  check('run.failed 和节点失败是同一句话时不说两遍',
    all.filter((s) => s.title === '推送失败：企业微信机器人地址没配' || s.detail === '推送失败：企业微信机器人地址没配').length === 1)

  // run.failed 单独到（节点没来得及报）：能定位到节点
  const alone = mod.decodeRun([
    { seq: 1, type: 'run.started', node_id: null, ts: 1, data: { nodes: 2 } },
    { seq: 2, type: 'run.failed', node_id: null, ts: 2, data: { error: '整体超时', node_id: 'notify', label: '推送企业微信' } },
  ]).find((s) => s.kind === 'error')
  check('run.failed 带上出错的节点', alone?.nodeId === 'notify' && !!alone.sub?.includes('推送企业微信'), alone?.sub)

  // 模型调用一出现就成行（以前要等 llm.end 才冒出来，想的那几十秒里什么都没有）
  const ev = synthetic.mixedRun()
  const upto = ev.slice(0, ev.findIndex((e) => e.type === 'llm.start') + 1)
  const pending = flatten(mod.decodeRun(upto)).find((s) => s.kind === 'llm')
  check('模型调用一开始就有一行进行中', pending?.status === 'running', pending?.status)
  check('进行中的行带开始时刻', typeof pending?.startedAt === 'number')

  const p = mod.progressOf(steps)
  check('进度按不同节点数算', p.total === 6 && p.done >= 4 && p.done <= 6, `${p.done}/${p.total}`)
}

console.log('\n=== 停下来的运行 ===')
{
  // 用户主动停止：还在跑的查询收成中性的「已取消」，不画红色的失败，也不再转圈
  const all = flatten(mod.decodeRun(synthetic.cancelledRun()))
  const q = all.find((s) => s.kind === 'query')
  check('取消时还在跑的查询收成已取消', q?.status === 'cancelled', q?.status)
  check('取消不是失败', !all.some((s) => s.status === 'failed' || s.level === 'error'))
  check('没有还在转的行', !all.some((s) => s.status === 'running'))

  // 服务重启：WS 回放完发一条 stream.end。事件停在半路也得收住，而且不成行
  const ev = synthetic.mixedRun()
  const half = ev.slice(0, ev.findIndex((e) => e.type === 'tool.end'))
  const ended = flatten(mod.decodeRun([...half,
    { seq: 999, type: 'stream.end', node_id: null, ts: 0, data: { status: 'interrupted', pending: false } }]))
  check('stream.end 之后没有还在转的行', !ended.some((s) => s.status === 'running'),
    ended.filter((s) => s.status === 'running').map((s) => s.title).join(','))
  check('stream.end 本身不成行', !ended.some((s) => s.title.includes('stream') || s.title === '一条还没翻译的记录'))
  check('挂起的步骤标成 suspended', ended.some((s) => s.status === 'suspended'))
}

console.log('\n=== 长运行：按轮折叠（runs-5）===')
{
  const ev = synthetic.longLoop(145)
  const steps = mod.decodeRun(ev)
  // 1100 多条事件、145 拍：顶层仍然只有「开始、参数、循环、读传感器、成果、完成」这几行
  check('顶层行数不随拍数增长', steps.length <= 7, `${steps.length} 行（${ev.length} 条事件）`)
  const poll = steps.find((s) => s.nodeId === 'poll')
  check('同一节点 145 次执行记在一行里', poll?.execs?.length === 145, `${poll?.execs?.length}`)
  check('行尾说执行了几次', !!poll?.meta?.startsWith('×145'), poll?.meta)
  const groups = mod.childrenByExec(poll)
  check('子步骤按轮分组，一轮一组', groups.length === 145 && groups.every((g) => g.steps.length >= 1))
  check('每一轮的循环序号跟着 node.started.iteration', groups[36].exec.iteration === 37)
  const st = mod.spread(groups.map((g) => g.exec.ms ?? 0))
  check('最慢那一拍认得出来', st.maxAt === 36 && st.max === 65, `第 ${st.maxAt + 1} 拍 ${st.max} ms`)
  check('提醒挂在它所在的那一轮', groups[9].steps.some((s) => s.level === 'warn'))

  // 相邻的同一件事并成一行，保留 level
  const rows = mod.compactSteps([
    { id: 'a', seq: 1, kind: 'code', title: '运行 Python 代码', status: 'done', ms: 10 },
    { id: 'b', seq: 2, kind: 'code', title: '运行 Python 代码', status: 'done', ms: 12 },
    { id: 'c', seq: 3, kind: 'code', title: '运行 Python 代码', status: 'done', ms: 63 },
    { id: 'd', seq: 4, kind: 'note', title: '循环达到上限', level: 'warn', status: 'done' },
    { id: 'e', seq: 5, kind: 'note', title: '循环达到上限', level: 'warn', status: 'done' },
  ])
  check('相邻重复行并成一行', rows.length === 2, rows.map((r) => r.title).join(' | '))
  check('并后的行写出次数、中位和最慢', rows[0].meta === '×3 · 中位 12 ms · 最慢 63 ms', rows[0].meta)
  check('警告并完还是警告', rows[1].level === 'warn' && rows[1].repeat?.count === 2)

  // 恢复后的重放（resumed）不算新的一轮；循环体真的又跑一次才算
  const replay = mod.decodeRun([
    { seq: 1, type: 'node.started', node_id: 'x', ts: 1, data: { label: '取数' } },
    { seq: 2, type: 'node.failed', node_id: 'x', ts: 2, data: { error: '超时' } },
    { seq: 3, type: 'run.resumed', node_id: null, ts: 3, data: {} },
    { seq: 4, type: 'node.started', node_id: 'x', ts: 4, data: { label: '取数', resumed: true } },
    { seq: 5, type: 'node.finished', node_id: 'x', ts: 5, data: { duration_ms: 20 } },
  ]).find((s) => s.nodeId === 'x')
  check('接着跑的重放不算新一轮', replay?.execs?.length === 1 && replay.status === 'done' && !replay.level,
    `${replay?.execs?.length} ${replay?.status} ${replay?.level}`)
}

console.log('\n=== 从 SQL 认表名 ===')
{
  const g = mod.describeSql
  check('JOIN 的两张表都认出来', g('SELECT a.x FROM orders a JOIN users u ON a.uid = u.id')?.tables.join(',') === 'orders,users')
  check('WITH 里的临时名不算表', g('WITH t AS (SELECT * FROM sales) SELECT * FROM t')?.tables.join(',') === 'sales')
  check('GROUP BY 1 这种位置序号不瞎猜', g('SELECT region, SUM(x) FROM s GROUP BY 1')?.groupBy.length === 0)
  check('聚合认得出', g('SELECT COUNT(*) FROM s')?.aggregate === true)
  check('函数包着的分组列取里面的列名', g('SELECT DATE(ts), COUNT(*) FROM s GROUP BY DATE(ts)')?.groupBy[0] === 'ts')
  check('不是查询就不硬认', g('这不是 SQL') === null)
}

console.log('\n=== Copilot：心跳穿插、回话、少了一步、报错 ===')
{
  const steps = mod.decodeCopilot(synthetic.COPILOT_STUCK, { context: 'canvas' })
  check('心跳穿插的思考仍然并成一条', steps.filter((s) => s.kind === 'think').length === 1,
    steps.filter((s) => s.kind === 'think').map((s) => s.title).join(' | '))
  check('收尾后没有任何还在转的行', !steps.some((s) => s.status === 'running'),
    steps.filter((s) => s.status === 'running').map((s) => s.title).join(','))
  check('阶段行收尾后不再写「正在…」', !steps.some((s) => s.kind === 'lifecycle' && s.title.startsWith('正在')),
    steps.filter((s) => s.kind === 'lifecycle').map((s) => s.title).join(' | '))
  check('修正写明第几轮', steps.some((s) => s.title.includes('第 1/2 轮')))
  check('少了一步说出来', steps.some((s) => s.level === 'warn' && s.title.includes('excel_export')))
  check('节点类型说中文', steps.find((s) => s.kind === 'node')?.meta === '调用工具',
    steps.find((s) => s.kind === 'node')?.meta)
  check('建图的每一行都标着规划阶段', steps.every((s) => s.stage === 'plan'))

  const out = mod.copilotOutcome(synthetic.COPILOT_STUCK)
  check('结局：图放上去了', out.kind === 'built' && out.total === 2)
  check('结局：自查没修好，问题带着节点 id', out.check?.status === 'failed'
    && out.check.issues[0]?.nodeId === 'lp' && out.check.issues[1]?.nodeId === 'q',
    JSON.stringify(out.check?.issues))
  check('结局：认出被跳过的类型', out.skipped.join(',') === 'excel_export')

  const reply = mod.copilotOutcome([{ op: 'reply', text: '按 mode 分' }])
  check('只回了一句话的轮次认得出', reply.kind === 'reply' && reply.reply === '按 mode 分')
  check('只回话时没有残留的转圈', !mod.decodeCopilot([
    { op: 'heartbeat', phase: 'planning', elapsed_ms: 3000 }, { op: 'reply', text: 'x' },
  ]).some((s) => s.status === 'running'))
  check('流结束了但什么都没给', mod.copilotOutcome([{ op: 'heartbeat', phase: 'planning' }]).kind === 'empty')

  const err = mod.decodeCopilot([{ op: 'error', message: '助手这一轮没跑完：模型服务没响应', hint: '稍后重试', detail: 'httpx.ReadTimeout' }])
  const row = err.find((s) => s.kind === 'error')
  check('报错：人话当标题、怎么办当副标题、原文进技术细节',
    row?.title.startsWith('助手这一轮没跑完') && row.sub === '稍后重试' && row.raw === 'httpx.ReadTimeout',
    `${row?.title} / ${row?.sub} / ${row?.raw}`)
  const eo = mod.copilotOutcome([{ op: 'error', message: 'm', hint: 'h', detail: 'd' }])
  check('结局里的报错分开带着 hint 和 detail', eo.kind === 'error' && eo.error?.hint === 'h' && eo.error?.raw === 'd')
}

console.log('\n=== 术语 ===')
{
  // 没起名的节点退到类型名，类型名跟全站同一张表
  const h = mod.decodeRun([{ seq: 1, type: 'node.started', node_id: 'h', ts: 1, data: { node_type: 'human', label: 'h' } }])
  check('人工节点叫「人工审批」', h[0]?.title === '人工审批', h[0]?.title)
}

console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 解码器全部通过')
process.exit(failed ? 1 : 0)
