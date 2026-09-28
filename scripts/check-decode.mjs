// 事件解码器的回归检查。
//
// 用**真实运行**导出的事件跑（fixtures.json 取自 data/agentlab.db），不是构造的样本：
// 这里要守的恰恰是真实事件的脏细节——审批会重放、成对事件字段不齐、
// 空 preview、节点没起过名字。构造的样本永远不会长成那样。
// 库名表名已脱敏（改的只是标识符字面量，事件的结构和脏细节原样保留）。
//
// 转译借 vite dev server（它 serve 的就是应用实际运行的那份）。
// 跑之前前端得起着（./scripts/dev.sh），默认连 5273。对别的实例（比如一份沙箱拷贝）跑时
// 带上地址：AGENTLAB_WEB=http://localhost:<前端端口> node scripts/check-decode.mjs
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

/**
 * 一节一节地跑：某一节里抛了异常，只记成这一节失败，接着跑下一节，
 * 不让一处卡住把后面的检查一起吞掉。这里是纯函数的检查：
 * 某种事件让翻译层直接抛了异常，也只算这一节
 */
async function section(name, fn) {
  console.log(`\n=== ${name} ===`)
  try {
    await fn()
  } catch (e) {
    check(`${name} 中途出错`, false, String(e?.message ?? e).split('\n')[0])
  }
}
const flatten = (steps) =>
  steps.flatMap((s) => [s, ...(s.children ? flatten(s.children) : [])])

await section('数据库查询', async () => {
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
})

await section('知识检索', async () => {
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
})

await section('长期记忆', async () => {
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
})

await section('被截断的结果集', async () => {
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
})

await section('人工审批（含重放）', async () => {
  const steps = mod.decodeRun(fixtures.human)
  const all = flatten(steps)
  // 一次审批会产生 human.requested ×2（重放）+ run.interrupted，界面上只该有一条
  check('"等你确认"只出现一次',
    all.filter((s) => s.kind === 'human' && s.title.includes('确认')).length === 1,
    `${all.filter((s) => s.kind === 'human' && s.title.includes('确认')).length} 条`)
  check('"继续运行"只出现一次',
    steps.filter((s) => s.title === '继续运行').length === 1,
    `${steps.filter((s) => s.title === '继续运行').length} 条`)
  // 节点重放不该让同一个节点在时间线上出现两遍
  const nodeIds = steps.filter((s) => s.kind === 'node').map((s) => s.nodeId)
  check('节点不因重放而重复', new Set(nodeIds).size === nodeIds.length,
    nodeIds.join(','))
  check('审批结果有交代', all.some((s) => s.title.includes('放行') || s.title.includes('驳回')))
  // 恢复过的运行有"开始运行"+"继续运行"两条生命周期。只收第一条的话，
  // "继续运行"会永远转圈——明明整条已经跑完了
  check('恢复过的运行不会留下转圈的行',
    !all.some((s) => s.status === 'running'),
    all.filter((s) => s.status === 'running').map((s) => s.title).join(','))
  check('处理完的审批不再是待办色',
    !all.some((s) => s.status === 'done' && s.level === 'warn'))
})

await section('停在审批时的状态', async () => {
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
})

await section('循环里的多轮审批', async () => {
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
})

await section('多 agent 协作', async () => {
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
})

await section('出具判定', async () => {
  const steps = mod.decodeRun(fixtures.issue)
  check('出具档位翻译成中文', steps.some((s) => s.kind === 'issuance' && s.title.includes('出具')),
    steps.find((s) => s.kind === 'issuance')?.title)
})

await section('失败运行', async () => {
  const steps = mod.decodeRun(fixtures.failed)
  const all = flatten(steps)
  const errs = all.filter((s) => s.level === 'error')
  check('错误只说一遍（节点失败与 run.failed 是同一件事）',
    new Set(errs.map((s) => s.detail ?? s.title)).size === errs.length,
    `${errs.length} 条错误`)
  check('开头那条不会一直转圈',
    !steps.some((s) => s.kind === 'lifecycle' && s.status === 'running'))
})

await section('并行分支', async () => {
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
})

await section('通用规则', async () => {
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
})

await section('Copilot 操作流', async () => {
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
    built[built.length - 1].title === '工作流搭好了，共 2 步',
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
  check('自查插在后面也不挤掉"共几步"', titles.includes('工作流搭好了，共 2 步'), titles.join(' | '))
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
})

await section('协作团队：调度者的决策（新后端的 agent.route.*）', async () => {
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
})

await section('被跳过的节点、思考、查询标题、技术细节、签批人、出具缺口', async () => {
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
  const resume = steps.find((s) => s.title === '继续运行')
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
})

await section('停下来的运行', async () => {
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
})

await section('长运行：按轮折叠（runs-5）', async () => {
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
  // 出了状况的行，副标题说的是为什么：两次修复凑出来的值不一样，并成一行就只剩第一次的
  const why = mod.compactSteps([
    { id: 'r1', seq: 1, kind: 'llm', title: '让模型修复格式', level: 'warn', status: 'done', sub: '出现了原文没有的值（total_count=0）' },
    { id: 'r2', seq: 2, kind: 'llm', title: '让模型修复格式', level: 'warn', status: 'done', sub: '出现了原文没有的值（region=华东）' },
    { id: 'r3', seq: 3, kind: 'llm', title: '让模型修复格式', level: 'warn', status: 'done', sub: '出现了原文没有的值（region=华东）' },
  ])
  check('原因不同的警告不并成一行，原因相同的照并', why.length === 2 && why[1].repeat?.count === 2,
    why.map((r) => `${r.sub} ${r.meta ?? ''}`).join(' | '))

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
})

await section('从 SQL 认表名', async () => {
  const g = mod.describeSql
  check('JOIN 的两张表都认出来', g('SELECT a.x FROM orders a JOIN users u ON a.uid = u.id')?.tables.join(',') === 'orders,users')
  check('WITH 里的临时名不算表', g('WITH t AS (SELECT * FROM sales) SELECT * FROM t')?.tables.join(',') === 'sales')
  check('GROUP BY 1 这种位置序号不瞎猜', g('SELECT region, SUM(x) FROM s GROUP BY 1')?.groupBy.length === 0)
  check('聚合认得出', g('SELECT COUNT(*) FROM s')?.aggregate === true)
  check('函数包着的分组列取里面的列名', g('SELECT DATE(ts), COUNT(*) FROM s GROUP BY DATE(ts)')?.groupBy[0] === 'ts')
  check('不是查询就不硬认', g('这不是 SQL') === null)
})

await section('Copilot：心跳穿插、回话、少了一步、报错', async () => {
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
})

await section('协作团队用完轮数：最后那次判定不是新的一轮（REQ-3A-3、NI-5）', async () => {
  const team = (evts) => mod.decodeRun(evts).find((s) => s.nodeId === 'team')
  const routes = (evts) => flatten(mod.decodeRun(evts)).filter((s) => s.kind === 'branch' && s.nodeId === 'team')

  // 以前 closing 判定被写成「第 3 轮：调度者在想下一步…」→「第 3 轮：结束协作」，done=false 时
  // 读起来像团队正常收尾了；泳道还多出一列空的第 3 轮
  const failRoutes = routes(synthetic.exhaustedTeam('fail'))
  const closing = failRoutes[failRoutes.length - 1]
  check('判定没完成：说「轮数用完 · 调度者判定：未完成」并带理由',
    closing?.title === '轮数用完 · 调度者判定：未完成（还没有查到任何订单数据）', closing?.title)
  check('判定没完成的那一行是提醒色', closing?.level === 'warn', closing?.level)
  check('不再把判定写成「第 3 轮」', !failRoutes.some((s) => s.title.includes('第 3 轮')),
    failRoutes.map((s) => s.title).join(' | '))
  const judged = routes(synthetic.exhaustedTeam('judged'))
  check('判定完成：说「轮数用完 · 调度者判定：已完成」',
    judged[judged.length - 1]?.title === '轮数用完 · 调度者判定：已完成', judged[judged.length - 1]?.title)
  const live = routes(synthetic.exhaustedTeam('closing'))
  const pending = live[live.length - 1]
  check('还在判定时有一行进行中，不说「第 3 轮」',
    pending?.status === 'running' && pending.title === '轮数用完 · 调度者在做最后判定…', `${pending?.status} ${pending?.title}`)

  for (const mode of ['fail', 'judged', 'degrade']) {
    const t = team(synthetic.exhaustedTeam(mode))?.team
    check(`${mode}：泳道只有派过活的 2 轮，判定不加列`, t?.rounds.length === 2, `${t?.rounds.length} 轮`)
  }
  // reduceTeam 是画布协作矩阵和右栏共用的那一份：直接喂事件也不能多出一列
  let direct
  for (const e of synthetic.exhaustedTeam('fail')) {
    const next = mod.reduceTeam(direct, e)
    if (next && e.node_id === 'team') direct = next
  }
  check('reduceTeam 直接喂也只有 2 轮', direct?.rounds.length === 2, `${direct?.rounds.length}`)
  check('reduceTeam 记下了判定结论', direct?.verdict?.closing === true && direct.verdict.done === false
    && direct.verdict.reason === '还没有查到任何订单数据', JSON.stringify(direct?.verdict))
  // 还在判定：泳道据此写「调度者在判定」，而不是空着或多一列
  let judging
  for (const e of synthetic.exhaustedTeam('closing')) {
    const next = mod.reduceTeam(judging, e)
    if (next && e.node_id === 'team') judging = next
  }
  check('判定进行中：记下在判定、还没有结论，也不加列', judging?.verdict?.closing === true && judging.verdict.done === undefined
    && judging.rounds.length === 2, JSON.stringify(judging?.verdict))
  const judgedTeam = team(synthetic.exhaustedTeam('judged'))?.team
  check('判定完成时团队算收尾了', judgedTeam?.finished === true && judgedTeam.verdict?.done === true)

  // 成员把工具调用写成文字：这一步是失败，不是一个写着失败原因的「完成」格子
  const r0 = direct?.rounds[0]?.members.find((m) => m.agent === '取数员')
  check('失败的成员格子是 failed', r0?.status === 'failed', r0?.status)
  check('失败的成员带着原因', !!r0?.error?.includes('没有真正调用工具'), r0?.error)
  const r1 = direct?.rounds[1]?.members.find((m) => m.agent === '分析员')
  check('同一轮做完的成员照常是 done', r1?.status === 'done', r1?.status)
  const memberRow = flatten(mod.decodeRun(synthetic.exhaustedTeam('fail')))
    .find((s) => s.title.startsWith('取数员：') && s.status === 'failed')
  check('失败的成员那一行也画成失败', !!memberRow && memberRow.level === 'error', memberRow?.status)
  check('失败的成员那一行说清为什么', !!memberRow?.sub?.includes('没有真正调用工具'), memberRow?.sub)
  check('一轮里有人失败时不算「省下」', direct?.savedMs === 0, `${direct?.savedMs}`)

  // 判失败：矩阵和右栏都要说「用完 N 轮未完成」，点出从没派到的成员
  check('判失败：团队的结局是 failed', direct?.verdict?.outcome === 'failed', JSON.stringify(direct?.verdict))
  check('判失败：认出轮数', direct?.verdict?.rounds === 2, `${direct?.verdict?.rounds}`)
  check('判失败：点出从没派到的成员', direct?.verdict?.never?.join(',') === '汇总员', direct?.verdict?.never?.join(','))

  // 降档交付：节点「完成」了，但不能是一个安静的勾
  const degraded = mod.decodeRun(synthetic.exhaustedTeam('degrade'))
  const node = degraded.find((s) => s.nodeId === 'team')
  check('降档：团队节点行是提醒色', node?.status === 'done' && node.level === 'warn', `${node?.status} ${node?.level}`)
  check('降档：团队节点行说清是降档交付', !!node?.sub?.includes('用完 2 轮') && node.sub.includes('降档'), node?.sub)
  check('降档：泳道的结局是 degraded，点出没派到的成员',
    node?.team?.verdict?.outcome === 'degraded' && node.team.verdict.never?.join(',') === '汇总员',
    JSON.stringify(node?.team?.verdict))
  const note = flatten(degraded).find((s) => s.code === 'team_exhausted')
  check('降档那条提醒说人话', note?.title === '协作团队用完 2 轮仍未完成 · 按降档交付', note?.title)
  check('降档那条提醒给出下一步', !!note?.next && note.fix === 'canvas', `${note?.next} / ${note?.fix}`)
  // 认不出轮数（老后端、措辞变了）时照实说「用完了轮数」，不拿「?」凑一个数
  const vague = flatten(mod.decodeRun([
    { seq: 1, type: 'node.started', node_id: 'team', ts: 1, data: { node_type: 'supervisor', label: '团队' } },
    { seq: 2, type: 'log', node_id: 'team', ts: 2, data: { level: 'warn', code: 'team_exhausted', message: '协作团队没做完，按降档交付' } },
    { seq: 3, type: 'node.finished', node_id: 'team', ts: 3, data: { duration_ms: 5, preview: { text: 'x', exhausted: true } } },
  ]))
  const vagueNote = vague.find((s) => s.code === 'team_exhausted')
  const vagueNode = vague.find((s) => s.kind === 'node' && s.nodeId === 'team')
  check('认不出轮数时不写「?」', !vagueNote?.title.includes('?') && !vagueNode?.sub?.includes('?')
    && !!vagueNote?.title.includes('用完了轮数'), `${vagueNote?.title} / ${vagueNode?.sub}`)
})

await section('模型把工具调用写成了文字（NI-4）', async () => {
  const all = flatten(mod.decodeRun(synthetic.markupRun()))
  const warn = all.find((s) => s.code === 'tool_markup_leak')
  check('提醒行说人话，不贴原始标记', warn?.title === '模型把工具调用写成了文字，没有真正执行'
    && !warn.title.includes('DSML'), warn?.title)
  check('说清已经提醒它重试', !!warn?.sub?.includes('重试一次'), warn?.sub)
  check('给出下一步：去画布绑定工具', !!warn?.next?.includes('绑定') && warn.fix === 'canvas', `${warn?.next} / ${warn?.fix}`)
  check('原始标记留在展开区给排查', !!warn?.detail?.includes('DSML'))
  const team = flatten(mod.decodeRun(synthetic.exhaustedTeam('fail'))).find((s) => s.code === 'tool_markup_leak')
  check('成员写成文字时说是哪个成员', team?.title === '取数员把工具调用写成了文字，没有真正执行', team?.title)
  const node = mod.decodeRun(synthetic.markupRun()).find((s) => s.nodeId === 'query')
  check('失败的节点照常是失败，原因留在详情', node?.status === 'failed' && !!node.detail?.includes('原始标记'))
  // 收尾轮那种：真调过工具，只是步数用完了还想接着查。节点照常完成，不能说成「没有真正执行」
  const settle = flatten(mod.decodeRun([
    { seq: 1, type: 'node.started', node_id: 'q', ts: 1, data: { node_type: 'agent', label: '数据查询' } },
    { seq: 2, type: 'log', node_id: 'q', ts: 2, data: { level: 'warn', code: 'tool_markup_leak',
      message: '模型把工具调用写成了文字（<tool_call>{"name": "db_query__shop"…），没有真正调用工具：步数用完后的收尾轮仍想调用工具，结论只基于之前查到的部分' } },
    { seq: 3, type: 'node.finished', node_id: 'q', ts: 3, data: { duration_ms: 9, preview: { text: '华东 1,204 单' } } },
  ])).find((s) => s.code === 'tool_markup_leak')
  check('收尾轮还想查：说步数用完，不说没有真正执行', !!settle?.title.includes('步数用完') && !settle.title.includes('没有真正执行'),
    settle?.title)
  check('收尾轮还想查：下一步是调大最多步数', !!settle?.next?.includes('最多步数'), settle?.next)
})

await section('校验修复想凑数（NI-3）', async () => {
  const all = flatten(mod.decodeRun(synthetic.repairRun()))
  const repairs = all.filter((s) => s.code === 'repair')
  check('每次修复是一行模型调用', repairs.length === 2 && repairs.every((s) => s.kind === 'llm' && s.title === '让模型修复格式'),
    repairs.map((s) => s.title).join(' | '))
  check('修复调用带着耗时', repairs[0]?.meta === '1.3 s', repairs[0]?.meta)
  check('被作废的修复折进那一行，不另起孤行', !all.some((s) => s.code === 'repair_invented'),
    all.filter((s) => s.code === 'repair_invented').map((s) => s.title).join(','))
  check('作废的修复画成提醒，说出凑出来的值', repairs.every((s) => s.level === 'warn' && s.sub?.includes('total_count=0')),
    repairs.map((s) => `${s.level} ${s.sub}`).join(' | '))
  check('作废的修复给出下一步', !!repairs[0]?.next?.includes('上游'), repairs[0]?.next)
  check('第一次校验失败那条照常显示', all.some((s) => s.code === 'validate_retry' && s.title.includes('第 1 次校验失败')))
  // 老数据：没有 purpose=repair 的 llm.end，作废说明也不能丢
  const legacy = flatten(mod.decodeRun(synthetic.repairRun().filter((e) => e.type !== 'llm.end')))
  const orphan = legacy.find((s) => s.code === 'repair_invented')
  check('没有修复行可折时单独成一行说人话', !!orphan?.title.includes('修复被作废') && orphan.level === 'warn', orphan?.title)
})

await section('工具时限（timeout_s / timed_out）', async () => {
  const live = flatten(mod.decodeRun(synthetic.timeoutRun('live'))).find((s) => s.kind === 'query')
  check('进行中的查询带着时限', live?.limitS === 30 && live.status === 'running', `${live?.limitS} ${live?.status}`)
  const done = flatten(mod.decodeRun(synthetic.timeoutRun('done'))).find((s) => s.kind === 'query')
  check('超时的查询是失败', done?.status === 'failed' && done.level === 'error', done?.status)
  check('超时的查询说清超了多少上限、已放弃等待', done?.sub === '超过 30 s 上限，已放弃等待', done?.sub)
  check('超时的原话留在结果里', !!done?.result?.includes('缩小范围'))
})

await section('放弃等审批的运行', async () => {
  const steps = mod.decodeRun(synthetic.abandonedRun())
  const row = steps.find((s) => s.kind === 'lifecycle' && s.status === 'cancelled')
  check('取消那一行说清谁放弃的、一并关了什么', !!row?.sub?.includes('张工') && row.sub.includes('1 条待审批一并关闭'), row?.sub)
  check('放弃之后审批不再是待办', !flatten(steps).some((s) => s.status === 'waiting' || s.status === 'running'))
  const plain = mod.decodeRun(synthetic.cancelledRun()).find((s) => s.kind === 'lifecycle' && s.status === 'cancelled')
  check('老数据的取消行不硬编副标题', plain && !plain.sub, plain?.sub)
})

await section('出具那一行记着档位', async () => {
  const is = mod.decodeRun(synthetic.mixedRun()).find((s) => s.kind === 'issuance')
  check('出具步骤带 tier，头部能据此提醒', is?.tier === 'degraded', is?.tier)
})

await section('Copilot 改图：工具绑定变化（NI-1）', async () => {
  const steps = mod.decodeCopilot(synthetic.COPILOT_TOOLS_DROPPED, { context: 'chat' })
  const change = steps.find((s) => s.code === 'tool_changes')
  check('工具绑定变化单独成一行', !!change, steps.map((s) => s.title).join(' | '))
  check('工具少了用提醒色，说清是哪个节点', change?.level === 'warn' && change.title.includes('数据查询'), change?.title)
  check('逐项列出前后', !!change?.detail?.includes('db_query__shop') && change.detail.includes('→'), change?.detail)
  const selfCheck = steps.find((s) => s.title.startsWith('自查通过'))
  check('自查通过但有工具被去掉：自查那一行是提醒，不是安静的通过', selfCheck?.level === 'warn'
    && selfCheck.title.includes('工具'), `${selfCheck?.level} ${selfCheck?.title}`)
  const out = mod.copilotOutcome(synthetic.COPILOT_TOOLS_DROPPED)
  check('结局里带着工具绑定变化', out.toolChanges?.length === 1 && out.toolChanges[0].removed.length === 2,
    JSON.stringify(out.toolChanges))
  check('结局里带着「工具被去掉」的警告', out.dropped?.length === 1, JSON.stringify(out.dropped))
  const plain = mod.copilotOutcome(synthetic.COPILOT_STUCK)
  check('没有变化时两样都是空的', !plain.toolChanges?.length && !plain.dropped?.length)
})

await section('协作团队执行了不止一次：结局和泳道按这一次算（3C REQ-2/3）', async () => {
  // 画布卡片和右栏泳道都读 reduceTeam 的结果：store 对每个事件都调它，没有别的清理
  const feed = (evts) => {
    let t
    const seen = []
    for (const e of evts) {
      const next = mod.reduceTeam(t, e)
      if (next && e.node_id === 'team') { t = next; seen.push([e, t]) }
    }
    return { team: t, seen }
  }

  // 用完轮数判失败 → 调大轮数接着跑（后端也标 resumed:true）→ 成功
  const rerun = feed(synthetic.rerunTeam('rerun'))
  check('接着跑成功后：结局不再是上一次的「用完 2 轮仍未完成」', !rerun.team?.verdict?.outcome
    && !rerun.team?.verdict?.never, JSON.stringify(rerun.team?.verdict))
  check('接着跑成功后：团队收尾了', rerun.team?.finished === true)
  check('接着跑成功后：泳道只剩这一次的 1 轮', rerun.team?.rounds.length === 1
    && rerun.team.rounds[0].members.length === 1 && rerun.team.rounds[0].members[0].result === '查到 128 单',
    JSON.stringify(rerun.team?.rounds.map((r) => r.members.map((m) => m.result))))
  const atFailure = rerun.seen.find(([e]) => e.type === 'node.failed')?.[1]
  check('判失败的那一刻结局照常是 failed', atFailure?.verdict?.outcome === 'failed', JSON.stringify(atFailure?.verdict))
  const rerunNode = mod.decodeRun(synthetic.rerunTeam('rerun')).find((s) => s.nodeId === 'team')
  check('右栏泳道：接着跑成功后不画「用完 N 轮」的结局', !rerunNode?.team?.verdict?.outcome,
    JSON.stringify(rerunNode?.team?.verdict))

  // 循环里的团队：第 1 轮降档、第 2 轮正常
  const loop = feed(synthetic.rerunTeam('loop'))
  check('循环第 2 轮正常收尾：结局不再是上一轮的 degraded', !loop.team?.verdict?.outcome,
    JSON.stringify(loop.team?.verdict))
  check('循环第 2 轮：round 从 0 数，不并进上一轮同号的那一列', loop.team?.rounds.length === 1
    && loop.team.rounds[0].members.length === 1 && loop.team.rounds[0].members[0].result === '第 2 周的原话',
    JSON.stringify(loop.team?.rounds.map((r) => r.members.map((m) => m.result))))
  const degradedAt = loop.seen.find(([e]) => e.type === 'node.finished')?.[1]
  // 产出里不带轮数（后端只给 exhausted / exhausted_reason / never_dispatched）：从日志原话里认
  check('降档那一刻：轮数从日志原话里认出来', degradedAt?.verdict?.outcome === 'degraded'
    && degradedAt.verdict.rounds === 2, JSON.stringify(degradedAt?.verdict))
  const loopEvents = synthetic.rerunTeam('loop')
  const firstEnd = loopEvents.findIndex((e) => e.type === 'node.finished' && e.node_id === 'team')
  const midNode = flatten(mod.decodeRun(loopEvents.slice(0, firstEnd + 1))).find((s) => s.kind === 'node' && s.nodeId === 'team')
  check('降档的节点行写「用完 2 轮」，不写「用完 ? 轮」', midNode?.sub === '用完 2 轮仍未完成，按降档交付'
    && midNode.level === 'warn', `${midNode?.sub} / ${midNode?.level}`)
  const loopNode = flatten(mod.decodeRun(loopEvents)).find((s) => s.kind === 'node' && s.nodeId === 'team')
  check('循环第 2 轮正常收尾：节点行不再挂着上一轮的降档说明', loopNode?.execs?.length === 2
    && !loopNode.sub && !loopNode.level, `${loopNode?.execs?.length} / ${loopNode?.sub} / ${loopNode?.level}`)

  // 日志也没有轮数时，退到这一次实际派过几轮
  const noLog = feed([
    { seq: 1, type: 'node.started', node_id: 'team', ts: 1, data: { node_type: 'supervisor' } },
    { seq: 2, type: 'agent.route.end', node_id: 'team', ts: 2, data: { round: 0, agents: ['分析员'], parallel: 1, done: false, reason: '看看' } },
    { seq: 3, type: 'agent.step.start', node_id: 'team', ts: 3, data: { agent: '分析员', round: 0 } },
    { seq: 4, type: 'agent.step.end', node_id: 'team', ts: 4, data: { agent: '分析员', round: 0, duration_ms: 5 } },
    { seq: 5, type: 'agent.step.start', node_id: 'team', ts: 5, data: { agent: '分析员', round: 1 } },
    { seq: 6, type: 'agent.step.end', node_id: 'team', ts: 6, data: { agent: '分析员', round: 1, duration_ms: 5 } },
    { seq: 7, type: 'node.finished', node_id: 'team', ts: 7, data: { duration_ms: 20, preview: { exhausted: true, exhausted_reason: '没做完' } } },
  ])
  check('日志也没说几轮：用派过的轮数', noLog.team?.verdict?.rounds === 2, JSON.stringify(noLog.team?.verdict))

  // 等过审批之后的重放是同一次执行：泳道不清空
  const approval = feed(synthetic.rerunTeam('approval'))
  check('审批放行后的重放不算新的一次：第 1 轮的成员还在', approval.team?.rounds.length === 1
    && approval.team.rounds[0].members[0]?.result === '改好了' && approval.team.finished === true,
    JSON.stringify(approval.team?.rounds))
  check('审批收尾后不再挂着「等重放」的记号', !approval.team?.paused, JSON.stringify(approval.team))
})

await section('Copilot 自查问题：对象形状、节点名、超出限定范围（3C REQ-6）', async () => {
  const steps = mod.decodeCopilot(synthetic.COPILOT_OBJECT_ISSUES, { context: 'chat' })
  const repairing = steps.find((s) => s.title.startsWith('自查发现'))
  check('对象形状：交回去改的那一行写节点名，不写「lp」', !!repairing?.detail?.startsWith('「逐周循环」循环条件写错了')
    && !repairing.detail.includes('「lp」'), repairing?.detail)
  const failed = steps.find((s) => s.kind === 'error')
  check('对象形状：没修好的问题也写节点名', !!failed?.detail?.includes('「订单查询」用了限定范围之外的数据源'), failed?.detail)
  check('认不出名字的节点退到 id', !!failed?.detail?.includes('「gone」'), failed?.detail)
  check('超出限定范围：单独给一句下一步', failed?.code === 'datasource_out_of_scope' && !!failed.next?.includes('限定'),
    `${failed?.code} / ${failed?.next}`)
  const canvasNames = mod.decodeCopilot(synthetic.COPILOT_OBJECT_ISSUES, { context: 'canvas', labelOf: (id) => ({ old: '旧的汇总', gone: '质量门' })[id] })
  check('画布给了节点名：操作流里没出现过的也写名字', !!canvasNames.find((s) => s.kind === 'error')?.detail?.includes('「质量门」'),
    canvasNames.find((s) => s.kind === 'error')?.detail)
  check('「去掉了」那一行也写名字', canvasNames.some((s) => s.title === '去掉了「旧的汇总」'),
    canvasNames.filter((s) => s.title.startsWith('去掉了')).map((s) => s.title).join(','))
  // 只改配置的 update_node 常常不带 label：「调整了」那一行也向画布要名字，不写「调整了「lp」」（3C REQ-27）
  const bare = mod.decodeCopilot([{ op: 'update_node', id: 'lp', config: { max_iterations: 4 } }],
    { context: 'canvas', labelOf: (id) => ({ lp: '逐周循环' })[id] })
  check('不带 label 的 update_node：写画布上的节点名', bare.some((s) => s.title === '调整了「逐周循环」'),
    bare.map((s) => s.title).join(','))
  const bareNoName = mod.decodeCopilot([{ op: 'update_node', id: 'lp', config: {} }], { context: 'canvas' })
  check('画布上也认不出：退到 id', bareNoName.some((s) => s.title === '调整了「lp」'), bareNoName.map((s) => s.title).join(','))

  const out = mod.copilotOutcome(synthetic.COPILOT_OBJECT_ISSUES)
  const scoped = out.check?.issues[0]
  check('结局里的问题留着 field 和 code', scoped?.nodeId === 'ask' && scoped.field === 'tools'
    && scoped.code === 'datasource_out_of_scope' && scoped.label === '订单查询', JSON.stringify(scoped))
  check('field 为 null 的不带 field', out.check?.issues[1] && !('field' in out.check.issues[1]), JSON.stringify(out.check?.issues[1]))
  check('issueLine 用名字', mod.issueLine(scoped) === '「订单查询」用了限定范围之外的数据源 sales_daily：这一轮只查 orders',
    mod.issueLine(scoped))
  // 老会话里存的还是字符串：照旧认出节点 id
  const legacy = mod.copilotOutcome(synthetic.COPILOT_STUCK)
  check('字符串形状照旧：节点 id 从开头的「」里认', legacy.check?.issues[0]?.nodeId === 'lp'
    && legacy.check.issues[0].label === '逐日循环' && !legacy.check.issues[0].field,
    JSON.stringify(legacy.check?.issues[0]))
})

await section('老后端没有 llm.end：做完的模型调用不算「已取消」（终验 NEW）', async () => {
  const E = (seq, type, node_id, ts, data = {}) => ({ seq, type, node_id, ts, data })
  const llms = (steps) => flatten(steps).filter((s) => s.kind === 'llm')
  // 最小复现：一次调用后面跟着真执行过的工具，第二次调用进行中被取消
  const cancelled = mod.decodeRun([
    E(1, 'run.started', null, 100, { nodes: 1 }),
    E(2, 'node.started', 'a', 100.1, { label: '取数' }),
    E(3, 'llm.start', 'a', 100.2, { model: 'm' }),
    E(4, 'llm.thinking', 'a', 101.0, { text: '先查一下订单表' }),
    E(5, 'tool.start', 'a', 101.4, { tool: 'calc', call_id: 'c1', args: {} }),
    E(6, 'tool.end', 'a', 101.5, { tool: 'calc', call_id: 'c1', output: '1' }),
    E(7, 'llm.start', 'a', 101.6, { model: 'm' }),
    E(8, 'run.cancelled', null, 102, {}),
  ])
  const rows = flatten(cancelled).filter((s) => s.kind === 'llm' || s.kind === 'tool' || s.kind === 'query')
  check('后面跟着工具调用的那次模型调用收成完成，不是已取消',
    rows.map((s) => `${s.kind}|${s.status}`).join(',') === 'llm|done,tool|done,llm|cancelled',
    rows.map((s) => `${s.kind}|${s.status}`).join(','))
  const first = llms(cancelled)[0]
  check('收尾时刻取下一条事件（tool.start）：耗时 1.2 s', first?.ms === 1200, `${first?.ms} / ${first?.meta}`)
  check('思考仍挂在它那次调用上', first?.sub === '先查一下订单表', first?.sub)

  // 连着两次调用、中间没有工具：前一次也做完了
  const twice = llms(mod.decodeRun([
    E(1, 'node.started', 'a', 1, {}),
    E(2, 'llm.start', 'a', 1.1, {}),
    E(3, 'llm.start', 'a', 2.1, {}),
    E(4, 'run.cancelled', null, 3, {}),
  ]))
  check('又开始下一次调用：前一次收成完成', twice.map((s) => s.status).join(',') === 'done,cancelled',
    twice.map((s) => s.status).join(','))
  // 同一次调用只会有一条完整思考；又来一条，说明前一次已经做完
  const thought = llms(mod.decodeRun([
    E(1, 'node.started', 'a', 1, {}),
    E(2, 'llm.start', 'a', 1.1, {}),
    E(3, 'llm.thinking', 'a', 2, { text: '第一次的想法' }),
    E(4, 'llm.thinking', 'a', 3, { text: '第二次的想法' }),
    E(5, 'run.cancelled', null, 4, {}),
  ]))
  check('又来一条思考：前一次收成完成', thought[0]?.status === 'done' && thought[0].sub === '第一次的想法',
    `${thought[0]?.status} / ${thought[0]?.sub}`)
  // 节点跑完了，它最后那次调用当然也做完了：之后别的节点上被取消，不能连带
  const finished = llms(mod.decodeRun([
    E(1, 'node.started', 'a', 1, {}),
    E(2, 'llm.start', 'a', 1.1, {}),
    E(3, 'node.finished', 'a', 2, { duration_ms: 900 }),
    E(4, 'node.started', 'b', 2.1, {}),
    E(5, 'llm.start', 'b', 2.2, {}),
    E(6, 'run.cancelled', null, 3, {}),
  ]))
  check('节点收尾时它那次调用收成完成，别的节点上进行中的才是已取消',
    finished.map((s) => `${s.nodeId}|${s.status}`).join(',') === 'a|done,b|cancelled',
    finished.map((s) => `${s.nodeId}|${s.status}`).join(','))

  // 节点失败：出错的就是它最后那次调用，跟节点一起算失败，不写「已取消」
  const failed = mod.decodeRun([
    E(1, 'run.started', null, 1, { nodes: 1 }),
    E(2, 'node.started', 'a', 1, {}),
    E(3, 'llm.start', 'a', 1.1, {}),
    E(4, 'tool.start', 'a', 2, { tool: 'calc', call_id: 'c1', args: {} }),
    E(5, 'tool.end', 'a', 2.1, { tool: 'calc', call_id: 'c1', output: '1' }),
    E(6, 'llm.start', 'a', 2.2, {}),
    E(7, 'node.failed', 'a', 3, { error: '模型接口拒绝了请求（401）' }),
    E(8, 'run.failed', null, 3, { error: '模型接口拒绝了请求（401）', node_id: 'a' }),
  ])
  check('节点失败：最后那次调用算失败，之前的算完成',
    llms(failed).map((s) => s.status).join(',') === 'done,failed', llms(failed).map((s) => s.status).join(','))

  // 新后端照常：llm.end 的耗时为准
  const modern = llms(mod.decodeRun([
    E(1, 'node.started', 'a', 1, {}),
    E(2, 'llm.start', 'a', 1.1, {}),
    E(3, 'llm.end', 'a', 1.5, { duration_ms: 420 }),
    E(4, 'tool.start', 'a', 1.6, { tool: 'calc', call_id: 'c1', args: {} }),
    E(5, 'run.cancelled', null, 2, {}),
  ]))
  check('有 llm.end 的照旧：耗时用它给的', modern[0]?.status === 'done' && modern[0].ms === 420, `${modern[0]?.status} ${modern[0]?.ms}`)
})

await section('分支、循环出口的说法（runfx-9、终验 NEW）', async () => {
  const E = (seq, type, node_id, data = {}) => ({ seq, type, node_id, ts: seq, data })
  const branchRow = (events, opts) => flatten(mod.decodeRun(events, undefined, opts)).filter((s) => s.kind === 'branch')
  const loop = (data) => branchRow([E(1, 'node.started', 'each'), E(2, 'edge.taken', 'each', data)])[0]?.title ?? ''
  // 循环节点的 iteration 是「这次决定之前跑过几轮」：走循环体时 +1 是正要开始的那一轮，走 done 时它就是一共几轮
  check('走循环体：第 3 轮', /第 3 轮/.test(loop({ branch: 'body', iteration: 2, total: 5, mode: 'foreach' })),
    loop({ branch: 'body', iteration: 2, total: 5, mode: 'foreach' }))
  const done5 = loop({ branch: 'done', iteration: 5, total: 5, mode: 'foreach' })
  check('5 项走完：写共 5 项，不写第 6 轮', /共 5 项/.test(done5) && !/第\s*6/.test(done5), done5)
  const done0 = loop({ branch: 'done', iteration: 0, total: 0, mode: 'foreach' })
  check('0 项：不写第 1 轮', !/第\s*1\s*轮/.test(done0) && /没有要处理的项/.test(done0), done0)
  const capped = loop({ branch: 'done', iteration: 10, total: 20, mode: 'foreach' })
  check('撞上限退出：跑了几轮、一共几项都说', /跑了 10 轮/.test(capped) && /共 20 项/.test(capped), capped)
  const whileDone = loop({ branch: 'done', iteration: 3, total: null, mode: 'while' })
  check('while 走完：跑了 3 轮', /跑了 3 轮/.test(whileDone) && !/第/.test(whileDone), whileDone)
  const whileNone = loop({ branch: 'done', iteration: 0, total: null, mode: 'while' })
  check('while 一轮都没跑', /一轮都没跑/.test(whileNone), whileNone)
  // 没有图的时候，循环的两个出口和兜底出口也不写 body / done / default
  check('循环体出口不写 body', /走「循环体」/.test(loop({ branch: 'body', iteration: 0, total: 2, mode: 'foreach' })))
  check('结束出口不写 done', /走「结束」/.test(done5), done5)
  const fallback = branchRow([E(1, 'node.started', 'br'), E(2, 'edge.taken', 'br', { branch: 'default', reason: '都不满足', mode: 'expression' })])[0]
  check('兜底出口写「其他」', fallback?.title === '走「其他」这条路', fallback?.title)

  // 画布给了出口说明：写说明，不写 key
  const labelOf = mod.exitLabels(
    [{ id: 'br', type: 'branch', config: { cases: [{ key: 'team', label: '交给团队' }, { key: 'solo', label: '' }] } },
     { id: 'each', type: 'loop', config: {} }, { id: 'x', type: 'agent', config: {} }],
    (type, config) => (type === 'branch'
      ? [...config.cases.map((c) => ({ id: c.key, label: c.label || c.key })), { id: 'default', label: '其他' }]
      : type === 'loop' ? [{ id: 'body', label: '每一项' }, { id: 'done', label: '收尾' }] : [{ id: 'out', label: '' }]),
  )
  const labelled = branchRow([E(1, 'node.started', 'br'), E(2, 'edge.taken', 'br', { branch: 'team', mode: 'llm' })], { exitLabelOf: labelOf })[0]
  check('分支行写 case 的说明，不写 key', labelled?.title === '走「交给团队」这条路', labelled?.title)
  const bare = branchRow([E(1, 'node.started', 'br'), E(2, 'edge.taken', 'br', { branch: 'solo', mode: 'expression' })], { exitLabelOf: labelOf })[0]
  check('case 没写说明：退回 key', bare?.title === '走「solo」这条路', bare?.title)
  const gone = branchRow([E(1, 'node.started', 'br'), E(2, 'edge.taken', 'br', { branch: 'removed', mode: 'expression' })], { exitLabelOf: labelOf })[0]
  check('图上已经没有这个出口：退回 key', gone?.title === '走「removed」这条路', gone?.title)
  const loopLabelled = branchRow([E(1, 'node.started', 'each'), E(2, 'edge.taken', 'each', { branch: 'done', iteration: 2, total: 2, mode: 'foreach' })], { exitLabelOf: labelOf })[0]
  check('循环出口也按图上的叫法', loopLabelled?.title === '走「收尾」这条路（共 2 项）', loopLabelled?.title)
  check('没有出口的节点不进表', labelOf('x', 'out') === undefined)
})

await section('报告核对 report.checked、口径卡的台账（可点击证据第一期）', async () => {
  // 夹具由后端 evidence.py 的 compose_doc 真跑生成（只用通用名），report_checked 是报告节点发的那条事件的载荷
  const evidence = JSON.parse(readFileSync(`${root}frontend/src/run/__tests__/evidence-doc.json`, 'utf8'))
  const rc = evidence.report_checked
  const E = (seq, type, node_id, data = {}) => ({ seq, type, node_id, ts: 1790000000 + seq, data })
  // 修复重写那条日志的原话：和 backend/app/engine/nodes/report.py 发的同一个格式（_named 用「、」
  // 接着点名的字）。下面先核对后端源码里还是这个格式，免得这里的夹具自说自话
  const REPAIR_MESSAGE = '报告里有 3 处没通过核对（「12」、「m:nope」、「3」），已要求写作者重写（第 1 次）'
  const reportPy = readFileSync(`${root}backend/app/engine/nodes/report.py`, 'utf8')
  check('后端发修复日志的格式还是夹具里这一种', reportPy.includes('code="report_repair"')
    && reportPy.includes('处没通过核对（{_named(blocking)}），') && reportPy.includes('已要求写作者重写（第 {repairs} 次）')
    && reportPy.includes('"、".join(f"「{n}」"'), '改了 report.py 的说法就同步改 decode.ts 的 explainLog 和这里的夹具')
  const card = evidence.doc.catalog['m:gmv'].artifact
  const events = [
    E(1, 'run.started', null, { total: 3 }),
    E(2, 'node.started', 'caliber', { node_type: 'metrics', label: '周报口径' }),
    // 口径卡的 node.finished 多了台账（evidence 数组），产出里多了 artifact 字符串：照常解码、不另起行
    E(3, 'node.finished', 'caliber', { duration_ms: 12,
      preview: { kind: 'metric_set', caliber: '周报口径', caliber_version: 'v2', artifact: card, text: '销售额 = 45,678.5元' },
      evidence: [{ kind: 'metric_set', node_id: 'caliber', exec: 1, artifact: card, caliber: '周报口径', version: 'v2', metrics: ['gmv'] }] }),
    E(4, 'node.started', 'write', { node_type: 'report' }),
    // 后端 report.py 原样的说法：「报告里有 N 处没通过核对（点名的字），已要求写作者重写（第 N 次）」
    E(5, 'log', 'write', { level: 'warn', code: 'report_repair', message: REPAIR_MESSAGE }),
    E(6, 'report.checked', 'write', rc),
    E(7, 'node.finished', 'write', { duration_ms: 2300, preview: { text: evidence.doc.markdown.slice(0, 40), doc_artifact: rc.doc_artifact } }),
    E(8, 'run.finished', null, { output: {} }),
  ]
  const steps = mod.decodeRun(events)
  const all = flatten(steps)
  check('新事件都有翻译，没有「一条还没翻译的记录」', !all.some((s) => s.title === '一条还没翻译的记录'),
    all.filter((s) => s.title === '一条还没翻译的记录').map((s) => s.detail?.split('\n')[0]).join('、'))
  const node = steps.find((s) => s.nodeId === 'write' && s.kind === 'node')
  check('没起名的报告节点叫「报告撰写」', node?.title === '报告撰写', node?.title)
  const row = all.find((s) => s.code === 'report_checked')
  check('report.checked 解码成一条步骤，挂在报告节点下面', !!row && !!node?.children?.includes(row), row?.title)
  check('标题说几个数字有出处、几个没有（算得平：12 − 7 = 5），非数字引用另起一句（和横幅同一种说法）',
    row?.title === '核对报告：7/12 数字有出处 · 无证据 5 · 另有 1 处引用解析不了', row?.title)
  check('有无证据的地方就是提醒，不是安静的一行', row?.level === 'warn', row?.level)
  check('能下钻到报告文档', row?.artifact === rc.doc_artifact, row?.artifact)
  check('违规清单放进展开区', !!row?.detail?.includes('数字「12」没有出处') && !!row.detail.includes('这种引用在后续版本支持'),
    row?.detail?.slice(0, 80))
  check('重写过几次写在行尾', row?.meta === '重写 1 次', row?.meta)
  const repair = all.find((s) => s.code === 'report_repair')
  check('修复重写说人话：几处没通过、第几次，原话留在展开区', repair?.title === '报告有 3 处没通过核对，已让模型按清单重写（第 1 次）'
    && repair.detail === REPAIR_MESSAGE, `${repair?.title} / ${repair?.detail}`)
  check('点名的字放进副标题（「12」「m:nope」这类）', repair?.sub === '「12」、「m:nope」、「3」', repair?.sub)
  const oldSaying = flatten(mod.decodeRun([E(1, 'node.started', 'w', { node_type: 'report' }),
    E(2, 'log', 'w', { level: 'warn', code: 'report_repair', message: '报告要重写' })])).find((s) => s.code === 'report_repair')
  check('别的说法（老后端）：标题照样说人话，不瞎填数', oldSaying?.title === '报告没通过核对，已让模型按清单重写'
    && oldSaying.sub === undefined, `${oldSaying?.title} / ${oldSaying?.sub}`)
  const caliber = steps.find((s) => s.nodeId === 'caliber')
  check('口径卡带台账的 node.finished 照常收成完成', caliber?.status === 'done' && !caliber.children?.length,
    `${caliber?.status} ${caliber?.children?.length ?? 0}`)

  const clean = flatten(mod.decodeRun([E(1, 'node.started', 'w', { node_type: 'report', label: '写周报' }),
    E(2, 'report.checked', 'w', { doc_artifact: 'abc', repairs: 0,
      stats: { numbers: 7, numbers_cited: 7, uncited_numbers: 0, unresolved: 0 }, violations: [] })]))
    .find((s) => s.code === 'report_checked')
  check('全都有出处：一句话说完，不是提醒', clean?.title === '核对报告：7 个数字都有出处' && clean.level !== 'warn'
    && !clean.meta, `${clean?.title} ${clean?.level} ${clean?.meta}`)
  const blocked = flatten(mod.decodeRun([E(1, 'node.started', 'w', { node_type: 'report', label: '写周报' }),
    E(2, 'report.checked', 'w', { doc_artifact: 'abc', repairs: 1, ok: false, on_violation: 'fail', failed: true,
      stats: { numbers: 3, numbers_cited: 2, uncited_numbers: 1, unresolved: 0 },
      violations: [{ code: 'uncited_number', message: '数字「45678」没有出处' }] })]))
    .find((s) => s.code === 'report_checked')
  check('fail 模式下仍不过：这一步画成失败，不是完成', blocked?.status === 'failed' && blocked.level === 'error',
    `${blocked?.status} ${blocked?.level}`)
  const flagged = flatten(mod.decodeRun([E(1, 'report.checked', 'w', { ok: false, on_violation: 'flag', failed: false,
    stats: { numbers: 3, numbers_cited: 2, uncited_numbers: 1, unresolved: 0 }, violations: [] })]))
    .find((s) => s.code === 'report_checked')
  check('flag 模式照常产出：完成、提醒级', flagged?.status === 'done' && flagged.level === 'warn',
    `${flagged?.status} ${flagged?.level}`)
  const bare = flatten(mod.decodeRun([E(1, 'report.checked', null, {})])).find((s) => s.code === 'report_checked')
  check('载荷缺字段也不崩，只说核对了报告', bare?.title === '核对报告' && !bare.artifact, bare?.title)
})

await section('术语', async () => {
  // 没起名的节点退到类型名，类型名跟全站同一张表
  const h = mod.decodeRun([{ seq: 1, type: 'node.started', node_id: 'h', ts: 1, data: { node_type: 'human', label: 'h' } }])
  check('人工节点叫「人工审批」', h[0]?.title === '人工审批', h[0]?.title)
})

console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 解码器全部通过')
process.exit(failed ? 1 : 0)
