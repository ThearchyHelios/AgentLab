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

await section('合并查询', async () => {
  // merge.end 是合并查询节点做完的那一条：哪几个输入、合并 SQL、几行。和查询同一类（kind query），
  // 合并 SQL 进详情、预览进结果、工件可下钻；警告的原文由紧跟着的 log 各占一行，这一行只说有几条
  const steps = mod.decodeRun(synthetic.mergeRun(), { status: 'succeeded' })
  const all = flatten(steps)
  const m = all.find((s) => s.code === 'merge')
  check('合并查询是一行查询步骤，挂在合并节点下', !!m && m.kind === 'query' && m.nodeId === 'merge'
    && steps.some((s) => s.kind === 'node' && s.nodeId === 'merge' && s.children?.includes(m)), m?.kind)
  check('标题说合并的是哪几个输入', m?.title === '合并 s、v 的查询结果', m?.title)
  check('合并 SQL 留在详情里', m?.detail === synthetic.MERGE_SQL)
  check('预览按查询结果的形状给出，能画成表格', !!mod.parseQueryResult(m?.result ?? '')?.columns?.includes('到店人数'))
  // 合并结果 8 行、预览只存了 5 行：这是「此处仅为预览」，不是「查询已达行数上限」（A3）
  const table = mod.parseQueryResult(m?.result ?? '')
  check('预览只存了前几行：记为预览（clipped），不记为查询撞了行数上限（truncated），并带上一共几行', table?.truncated === false
    && table?.clipped === true && table?.total === 8 && table?.rows.length === 5, JSON.stringify({ t: table?.truncated, c: table?.clipped, n: table?.total }))
  check('输入、行数、数据源都在', m?.merge?.inputs?.length === 2 && m.merge.inputs[1].source === 'members'
    && m.merge.inputs[0].label === '门店销售' && m.merge.rows === 8)
  check('行数和耗时写在行尾', !!m?.meta?.includes('8 行'), m?.meta)
  check('工件可下钻到合并结果的快照', m?.artifact === 'mq-merge')
  check('有警告时这一行标成警告、说有几条', m?.level === 'warn' && !!m.sub?.includes('2 条警告'), m?.sub)
  const key = all.find((s) => s.code === 'merge_key_type')
  check('键类型不一致说成人话：标题点名两列（读 keys 字段）', key?.title === '合并键类型不一致：s.门店 与 v.门店', key?.title)
  check('……副标题是 detail 字段', key?.sub === "s.门店 是文本（例如 '01'），v.门店 是数值（例如 1）", key?.sub)
  check('键类型不一致给出下一步', !!key?.next?.includes('CAST') && key.fix === 'canvas')
  const grew = all.find((s) => s.code === 'merge_rows_grew')
  check('行数放大说成人话，并带上行数和重复的键（读 rows、input、input_rows、duplicates 字段）', !!grew?.title.includes('不唯一')
    && grew.sub === '合并结果 8 行，行数最多的输入「s」4 行；「s」中按（门店）有重复的键，例如 01 出现 2 次等 2 组；「v」中按（门店）有重复的键，例如 1 出现 2 次等 2 组',
    grew?.sub)
  // 老运行的日志没有结构化字段：不从中文里解析，只给标题，原话留在展开区
  const old = flatten(mod.decodeRun([
    { seq: 1, type: 'node.started', node_id: 'merge', ts: 1, data: { node_type: 'merge' } },
    { seq: 2, type: 'log', node_id: 'merge', ts: 2, data: { level: 'warn', code: 'merge_key_type', message: '合并键类型不一致：s.门店 是文本，v.门店 是数值。' } },
  ])).find((s) => s.code === 'merge_key_type')
  check('老日志没有结构化字段：只给标题，不解析原话', old?.title === '合并键类型不一致' && !old.sub && !!old.detail?.includes('s.门店'), old?.title)
  check('警告原文各占一行，不在合并那一行里重复', all.filter((s) => s.level === 'warn' && s.nodeId === 'merge').length === 3)
  check('合并节点照常完成，两次源查询仍是普通查询步骤', all.filter((s) => s.kind === 'query' && s.code !== 'merge').length === 2
    && steps.find((s) => s.nodeId === 'merge')?.status === 'done')
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
  check('召回说人话', !!r?.title?.includes('召回 3 条'), r?.title)
  const none = flatten(mod.decodeRun(ev({ action: 'recall', scope: 'default', count: 0 })))[0]
  check('没有召回要标出来，不是静悄悄地过去', none?.status === 'failed')

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
  check('审批结果有交代', all.some((s) => s.title.includes('批准') || s.title.includes('驳回')))
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
  check('每条都带着决定', asks.every((s) => /已(批准|驳回)/.test(s.title)),
    asks.map((s) => s.title).join(' | '))
  check('不再替人署名为"你"', !asks.some((s) => s.title.includes('你')))
  check('两次驳回一次批准，顺序没错',
    asks.filter((s) => s.title.includes('驳回')).length === 2
    && asks[2]?.title.includes('批准'))
  // detail 里是被审的草稿全文，打出来会刷屏——只报哪一轮带了备注
  check('备注跟着那一轮走', asks.some((s) => s.detail?.includes('不高级')),
    asks.map((s, i) => (s.detail?.includes('备注') ? `#${i + 1} 有备注` : '')).filter(Boolean).join(' '))
  check('没有落单的"已批准/已驳回"孤行',
    !all.some((s) => s.title === '已批准' || s.title === '已驳回'))
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
    tail.title.includes('新增 1 个步骤') && tail.title.includes('共 4 个步骤'), tail.title)

  const built = mod.decodeCopilot([
    { op: 'add_node', node: { id: 'a', type: 'input' } },
    { op: 'add_node', node: { id: 'b', type: 'output' } },
    { op: 'done', explanation: '' },
    { op: 'final', graph: { nodes: [{ id: 'a' }, { id: 'b' }] } },
  ])
  check('从零新建时不啰嗦，只说共几步',
    built[built.length - 1].title === '工作流已搭建完成，共 2 个步骤',
    built[built.length - 1].title)

  // 自查：服务端用运行时同一套规则过一遍，有问题交回模型改。多出来的这段得看得见，
  // 而且它排在"搭好了"后面，不能把那一行的"共几步"挤没了
  const checked = mod.decodeCopilot([
    { op: 'add_node', node: { id: 'a', type: 'input' } },
    { op: 'add_node', node: { id: 'lp', type: 'loop' } },
    { op: 'done', explanation: '' },
    { op: 'check', status: 'repairing', round: 1, issues: ['「lp」循环条件有误：表达式中不支持 | 过滤器'] },
    { op: 'heartbeat', phase: 'repairing', elapsed_ms: 3000 },
    { op: 'update_node', id: 'lp' },
    { op: 'check', status: 'passed', repaired: 1 },
    { op: 'final', graph: { nodes: [{ id: 'a' }, { id: 'lp' }] } },
  ])
  const titles = checked.map((s) => s.title)
  check('自查发现问题要说出来', titles.some((t) => t.includes('自查发现 1 处问题')), titles.join(' | '))
  check('交回去改的是哪条要看得到',
    checked.some((s) => (s.detail ?? '').includes('循环条件有误')))
  check('改好了要说', titles.includes('自查通过：问题已修正'), titles.join(' | '))
  check('自查插在后面也不挤掉"共几步"', titles.includes('工作流已搭建完成，共 2 个步骤'), titles.join(' | '))
  check('自查结束后没有还在转的行', !checked.some((s) => s.status === 'running'),
    checked.filter((s) => s.status === 'running').map((s) => s.title).join(','))

  const stuck = mod.decodeCopilot([
    { op: 'done', explanation: '' },
    { op: 'check', status: 'failed', issues: ['「lp」循环条件有误', '「t」「数据整形」节点还没有填写表达式'] },
    { op: 'final', graph: { nodes: [{ id: 'lp' }, { id: 't' }] } },
  ])
  const failedCheck = stuck.find((s) => s.kind === 'error')
  check('改不好要明说、说清没有自动运行',
    !!failedCheck?.title.includes('仍有 2 处问题') && failedCheck.title.includes('未自动运行'),
    failedCheck?.title)
  // 画布上从不自动运行：「未自动运行」在那儿读起来像出了别的故障
  const onCanvas = mod.decodeCopilot([
    { op: 'check', status: 'failed', issues: ['「lp」循环条件有误'] },
  ], { context: 'canvas' }).find((s) => s.kind === 'error')
  check('画布语境说「未能自动修正」', !!onCanvas?.title.includes('未能自动修正')
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
  check('调度期间有一行进行中的「调度者正在规划下一步」', !!thinking?.title.includes('调度者正在规划'), thinking?.title)
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
  check('跳过的原因写出来', !!skipped?.sub?.includes('满足跳过条件'), skipped?.sub)

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
  check('签批人照事件写', !!human?.title.endsWith('→ 张工 已批准'), human?.title)
  check('备注跟着那一轮', !!human?.detail?.includes('备注：晚班偏低要跟进'))
  const resume = steps.find((s) => s.title === '继续运行')
  check('继续运行是谁发起的写在副标题', !!resume?.sub?.includes('张工'), resume?.sub)

  const issuance = steps.find((s) => s.kind === 'issuance')
  // 只有 gaps 时以前只写一个光秃秃的「降档出具」
  check('降档的原因写进出具那一行', !!issuance?.title.includes('校验不完整') && issuance.title.includes('叙述模板渲染为空'),
    issuance?.title)
  check('出具档位用统一叫法', !!issuance?.title.startsWith('降档出具'))

  const notify = steps.find((s) => s.nodeId === 'notify')
  check('节点失败的原始异常收进技术细节', notify?.status === 'failed' && !!notify.raw?.includes('ConnectError'))
  check('run.failed 和节点失败是同一句话时不说两遍',
    all.filter((s) => s.title === '推送失败：通知机器人地址为空' || s.detail === '推送失败：通知机器人地址为空').length === 1)

  // run.failed 单独到（节点没来得及报）：能定位到节点
  const alone = mod.decodeRun([
    { seq: 1, type: 'run.started', node_id: null, ts: 1, data: { nodes: 2 } },
    { seq: 2, type: 'run.failed', node_id: null, ts: 2, data: { error: '整体超时', node_id: 'notify', label: '推送通知' } },
  ]).find((s) => s.kind === 'error')
  check('run.failed 带上出错的节点', alone?.nodeId === 'notify' && !!alone.sub?.includes('推送通知'), alone?.sub)

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
  check('stream.end 本身不成行', !ended.some((s) => s.title.includes('stream') || s.title === '未识别的事件'))
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
  check('继续运行的重放不算新一轮', replay?.execs?.length === 1 && replay.status === 'done' && !replay.level,
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

await section('Copilot：助手按需查看（look 事件）', async () => {
  // look §2.6：一次查看一行。标题说看了什么，副标题说在哪、怎么来的；挑表失败直说交回了表目录
  const steps = mod.decodeCopilot(synthetic.COPILOT_CONTEXT, { context: 'canvas' })
  const row = steps.find((s) => s.code === 'copilot_look')
  check('标题说查看了几张表（点名的、挑出的、补上的一起数）', row?.title === '查看了 5 张表', row?.title)
  check('副标题写库名和怎么来的', row?.sub === 'scenic、shop · 按需求挑选、点名查看', row?.sub)
  check('展开写需求、每个库一行、没找到的表', !!row?.detail?.includes('需求：按渠道统计上月的入园人次')
    && row.detail.includes('「scenic」：按需求挑出 visits、channels；为连接补上 channel_visits')
    && row.detail.includes('「shop」：点名的 orders、customers') && row.detail.includes('没找到：「shop」的 refund_detail'), row?.detail)
  check('查看用时挂在行尾', row?.ms === 3420 && !!row?.meta, `${row?.ms} ${row?.meta}`)
  check('是看表结构那一类、标着规划阶段', row?.kind === 'schema' && row?.stage === 'plan' && row?.status === 'done')
  check('「正在查看」的心跳只更新那一条阶段行，不另起一行',
    steps.filter((s) => s.kind === 'lifecycle' && s.id.startsWith('hb-')).length === 1,
    steps.map((s) => `${s.kind}:${s.title}`).join(' | '))
  check('心跳阶段 looking 有说法', mod.COPILOT_PHASE_TEXT.looking === '正在查看表结构', mod.COPILOT_PHASE_TEXT.looking)
  check('收尾后没有还在转的行', !steps.some((s) => s.status === 'running'))
  check('结局不受影响', mod.copilotOutcome(synthetic.COPILOT_CONTEXT).kind === 'built')
  check('没有数据目录的库：下一步提示起草目录，入口直达那个源的目录页',
    row?.next === '「shop」还没有数据目录，起草后挑表和 SQL 检查会更准' && row?.fix === 'catalog'
    && row?.fixTo === '/data/catalog/src-shop', JSON.stringify({ next: row?.next, fixTo: row?.fixTo }))

  const fb = mod.decodeCopilot(synthetic.COPILOT_CONTEXT_FALLBACK, { context: 'chat' }).filter((s) => s.code === 'copilot_look')
  check('多次查看各一行，按先后排', fb.length === 2, fb.map((s) => s.title).join(' | '))
  check('挑表失败：标题直说没挑出来、已交回表目录', fb[0]?.title === '没挑出来，已把表目录交给助手' && fb[0]?.level === 'warn',
    fb[0]?.title)
  check('挑表失败：原因放在展开区', !!fb[0]?.detail?.includes('原因：挑选数据表超过 20 秒未完成'), fb[0]?.detail)
  check('紧跟着的点名查看说点名', fb[1]?.title === '查看了 2 张表' && fb[1]?.sub === 'scenic · 点名查看', `${fb[1]?.title} / ${fb[1]?.sub}`)

  const sys = mod.decodeCopilot(synthetic.COPILOT_LOOK_SYSTEM, { context: 'canvas' }).find((s) => s.code === 'copilot_look')
  check('系统代为查看：按要修的 SQL 查看了几张表', sys?.title === '按要修的 SQL 查看了 3 张表', sys?.title)

  const app = mod.decodeCopilot(synthetic.COPILOT_APP_ASK, { context: 'app' })
  check('应用润色：系统按固定版本查看工作流版本', app.some((s) => s.title === '按固定版本查看了 1 个工作流版本'),
    app.map((s) => s.title).join(' | '))
  check('应用润色：模型查看能力，没找到的写出来', app.some((s) => s.title === '查看了 1 个能力' && s.detail?.includes('没找到：erp_refund')))
  check('应用操作成行', app.some((s) => s.code === 'app_op' && s.title === '添加了组件「销售额」'))
  const ask = app.find((s) => s.code === 'copilot_ask')
  check('ask 结束这一轮：问题在展开区、下一步说回答', ask?.detail === '报告放在指标下面还是单独一页？' && !!ask?.next, JSON.stringify(ask))

  check('查看记录原样存进 meta.looks', mod.looksOf(synthetic.COPILOT_CONTEXT_FALLBACK).length === 2
    && mod.looksOf(synthetic.COPILOT_CONTEXT_FALLBACK)[0].objects[0].kind === 'table')
  check('旧写法 sources[] 当作表类型读', mod.lookEventOf({ op: 'look', sources: [{ source: 'a', named: ['x'], missing: [] }] })
    ?.objects[0]?.kind === 'table')
  // 大库（next-round 03 A2）：点名或挑中探查范围外的表，当场读了结构、加进了范围；记录里的 added_to_scope 写进展开区
  const added = mod.lookEventOf({ op: 'look', id: 'L1', need: '投诉工单处理时长', status: 'ok', objects: [
    { kind: 'table', source: 'erp', named: [], picked: ['complaint_ticket', 'ord_hdr_east'], missing: [], total: 3000, explored: 400,
      added_to_scope: ['complaint_ticket'], prefiltered: 140 }] })
  check('look 记录读 added_to_scope、prefiltered', JSON.stringify(added?.objects[0]?.added_to_scope) === '["complaint_ticket"]'
    && added?.objects[0]?.prefiltered === 140, JSON.stringify(added?.objects[0] ?? {}))
  const addedRow = mod.copilotLook({ op: 'look', id: 'L1', need: '投诉工单处理时长', status: 'ok', objects: added.objects })
  check('……展开区写「按需加入探查范围」', !!addedRow?.detail?.includes('按需加入探查范围：complaint_ticket'), addedRow?.detail)
  check('……老服务端没有这一项：不出这一句', !mod.copilotLook({ op: 'look', id: 'L2', need: 'x', status: 'ok', objects: [
    { kind: 'table', source: 'erp', named: ['a'], missing: [] }] })?.detail?.includes('按需加入'))
  check('形状不对不出行、不报错', mod.decodeCopilot([{ op: 'look' }, { op: 'look', objects: 'x' }]).length === 0)
  check('认不出的操作照旧忽略', mod.decodeCopilot([{ op: 'something_new', x: 1 }]).length === 0)
  // 三条旧说法删掉了（look §2.6），界面上哪里都不该再出现
  const all = [...steps, ...fb, ...mod.decodeCopilot(synthetic.COPILOT_CONTEXT_LEGACY)].map((s) => `${s.title}${s.sub ?? ''}${s.detail ?? ''}`).join('\n')
  check('旧文案不再出现', !/参考了 \d+ 张表的字段|只提供了表名|未能按需求挑选/.test(all))
})

await section('Copilot：老服务端的 context 操作照 look 的说法显示', async () => {
  const steps = mod.decodeCopilot(synthetic.COPILOT_CONTEXT_LEGACY, { context: 'canvas' })
  const row = steps.find((s) => s.code === 'copilot_context')
  check('一行说查看了几张表', row?.title === '查看了 5 张表', row?.title)
  check('小库整库给了的写「全部查看」', !!row?.sub?.includes('候选不多，全部查看'), row?.sub)
  const fb = mod.copilotContext({ op: 'context', sources: [
    { source: 'a', source_id: 'id-a', tables: [], selected_by: 'fallback', total: 60, catalog_tables: 0, reason: '超时' }] })
  check('挑表失败：同 look 的说法', fb?.title === '没挑出来，已把表目录交给助手' && !!fb?.detail?.includes('原因：超时'), JSON.stringify(fb))
  const partial = mod.copilotContext({ op: 'context', sources: [
    { source: 'wide', tables: ['t001', 't002'], selected_by: 'model', total: 205, explored: 200 }] })
  check('探查截断了的库写「共 N 张，只探查了 200 张」', !!partial?.detail?.includes('共 205 张，只探查了 200 张'), partial?.detail)
  check('形状不对不出行、不报错', mod.decodeCopilot([
    { op: 'context' }, { op: 'context', sources: 'x' }, { op: 'context', sources: [null, 3, { tables: ['t'] }] },
  ]).length === 0)
})

await section('运行来源、待办、动作（run.origin、task.*、action.*）', async () => {
  const ev = (seq, type, data, node_id = null) => ({ seq, type, data, node_id, ts: 1000 + seq })
  const steps = mod.decodeRun([
    ev(1, 'run.origin', { kind: 'app_entry', app_id: 'a1', app_name: '报销审批', app_version: 3, entry: 'apply', actor: '张三', actor_verified: true }),
    ev(2, 'run.started', { nodes: 3 }),
    ev(3, 'node.started', { label: '财务审批', node_type: 'human' }, 'h'),
    ev(4, 'human.requested', { payload: { title: '财务审批', node_id: 'h' } }, 'h'),
    // 事件数据的形状同服务端 process/tasks.py、process/actions.py 记下的
    ev(5, 'task.opened', { title: '财务审批', kind: 'approve', policy: { kind: 'all' }, due_at: '2026-10-15T10:00:00+00:00',
      assignees: [{ kind: 'group', name: 'finance' }, { kind: 'user', name: '李四' }] }, 'h'),
    ev(6, 'task.responded', { actor: '李四', verified: false, decision: 'approve', index: 1, of: 2 }, 'h'),
    ev(7, 'task.escalated', { slot: 'director', level: 1, to: [{ kind: 'user', name: '王五' }] }, 'h'),
    ev(8, 'task.resolved', { decision: 'reject', actor: '王五', signoffs: [{ by: '李四', decision: 'approve' }, { by: '王五', decision: 'reject', note: '金额不对' }] }, 'h'),
    ev(9, 'action.prepared', { capability: 'erp_pay', environment: 'prod', payload_hash: 'abcdef1234567890', preview: { amount: '***' }, attempt: 1 }, 't'),
    ev(10, 'action.unknown', { capability: 'erp_pay', environment: 'prod', lookup: false, message: '写入外部系统的结果未知，等待人工核实' }, 't'),
    ev(11, 'action.reconciled', { capability: 'erp_pay', how: 'manual', outcome: 'confirmed', actor: '张三', external_id: 'ERP-9' }, 't'),
    ev(12, 'action.simulated', { capability: 'erp_pay', message: '演练' }, 't'),
  ])
  const all = flatten(steps)
  const by = (code) => all.find((s) => s.code === code)
  check('来源：一行写来源，副标题写应用、入口、发起人', by('run_origin')?.title === '来源：应用入口'
    && by('run_origin')?.sub === '应用「报销审批」 v3 · 入口「apply」 · 已登录用户「张三」', JSON.stringify(by('run_origin')))
  check('待办打开：派给谁、会签方式', by('task_opened')?.sub === '派给 组「finance」、李四 · 全体同意才通过', by('task_opened')?.sub)
  check('待办回复：第几人 / 共几人', by('task_responded')?.title === '李四 同意（第 1 / 2 人）', by('task_responded')?.title)
  check('待办升级：写升级给哪个岗位、加派给谁', by('task_escalated')?.level === 'warn'
    && by('task_escalated')?.title === '超过时限，已升级给「director」' && by('task_escalated')?.sub === '派给 王五', JSON.stringify(by('task_escalated')))
  check('待办处理完：会签明细在展开区', by('task_resolved')?.title === '待办已处理：驳回'
    && !!by('task_resolved')?.detail?.includes('王五：驳回（金额不对）'), JSON.stringify(by('task_resolved')))
  check('待办挂在中断所在的节点下', all.find((s) => s.nodeId === 'h' && s.kind === 'node')?.children?.some((c) => c.code === 'task_opened'))
  check('动作准备：副标题写环境和哈希前 8 位，展开是遮罩后的预览',
    by('action_prepared')?.sub === '生产环境 · 内容哈希 abcdef12' && !!by('action_prepared')?.detail?.includes('***'), JSON.stringify(by('action_prepared')))
  check('结果未知：等待态、写明不会自动重发', by('action_unknown')?.status === 'waiting' && !!by('action_unknown')?.next?.includes('不会自动重发'),
    JSON.stringify(by('action_unknown')))
  check('核实：谁核实的、已生效、外部单号', by('action_reconciled')?.title === '「erp_pay」的结果已核实（张三）'
    && !!by('action_reconciled')?.detail?.includes('外部单号：ERP-9'), JSON.stringify(by('action_reconciled')))
  check('演练：照实说没有发送', by('action_simulated')?.title === '演练：未向「erp_pay」发送')
  check('老运行没有来源：不出这一行', !flatten(mod.decodeRun([ev(1, 'run.started', { nodes: 1 })])).some((s) => s.code === 'run_origin'))
  check('新事件不再落成「未识别的事件」', !all.some((s) => s.title === '未识别的事件'))
})

await section('Copilot：建议更新数据目录（catalog_patch 操作）', async () => {
  // 用户在助手里说了一条数据事实，服务端核对后转出目录修改提案。过程里记一行，保存、忽略在卡片上做
  const cp = await import(await load('/src/run/catalogPatch.ts'))
  const steps = mod.decodeCopilot(synthetic.COPILOT_CATALOG_PATCH, { context: 'canvas' })
  const row = steps.find((s) => s.code === 'catalog_patch')
  check('一行说清建议改哪张表、几项', row?.title === '建议更新「入园记录」的数据目录（3 项）', row?.title)
  check('副标题说明确认后才写入', row?.sub === '确认后才写入数据目录', row?.sub)
  check('展开逐项写改前 → 改后，用界面上的叫法', !!row?.detail?.includes('列 status 的码值：0=作废、1=有效 → 0=作废、1=有效、9=作废')
    && row.detail.includes('表的有效记录条件：status = 1 → status = 1 AND status <> 9')
    && row.detail.includes('列 visitor_count 的度量类型：可累加 → 可累加'), row?.detail)
  check('是看表结构那一类、标着规划阶段', row?.kind === 'schema' && row?.stage === 'plan' && row?.status === 'done')
  check('结局仍是「只回答」', mod.copilotOutcome(synthetic.COPILOT_CATALOG_PATCH).kind === 'reply')
  // 提案落在已驳回的项上（服务端照样交回驳回的原值，before_status 为 rejected）：展开的明细和同一轮下面的卡片同一个说法
  // 「已驳回：可累加」，不写成像是当前值的「可累加 → 存量」
  const onRejected = mod.decodeCopilot([{ op: 'catalog_patch', source: 'scenic', source_id: 'src-scenic', table: 'visits', table_label: '入园记录',
    version: 3, changes: [
      { path: 'columns.visitor_count.measure', before: 'flow', before_status: 'rejected', after: 'stock', value: 'stock', reason: '', state: 'change' },
      { path: 'kind', before: null, before_status: null, after: 'fact', value: 'fact', reason: '', state: 'change' },
    ] }], { context: 'canvas' }).find((s) => s.code === 'catalog_patch')
  check('改前是已驳回的值：展开写「已驳回：可累加 → 存量」，和卡片一致',
    !!onRejected?.detail?.split('\n').includes('列 visitor_count 的度量类型：已驳回：可累加 → 存量')
    && cp.patchBeforeText({ path: 'columns.visitor_count.measure', before: 'flow', before_status: 'rejected' }) === '已驳回：可累加', onRejected?.detail)
  check('……原来没有的照旧写「未填写」', !!onRejected?.detail?.split('\n').includes('表的表类型：未填写 → 明细表'), onRejected?.detail)

  const [patch] = cp.catalogPatchesOf(synthetic.COPILOT_CATALOG_PATCH)
  check('卡片拿到版本、数据源 id、表和中文名', patch?.version === 3 && patch.sourceId === 'src-scenic' && patch.table === 'visits'
    && patch.tableLabel === '入园记录' && patch.key === '1', JSON.stringify(patch && { ...patch, changes: patch.changes.length }))
  check('三项都认、状态照抄', patch?.changes.map((c) => c.state).join(',') === 'change,change,confirm')
  check('交回保存的是原样取值', JSON.stringify(cp.patchSubmit(patch.changes)[0]) === '{"path":"columns.status.codes","value":{"9":"作废"},"reason":"用户说明 status=9 表示作废"}')
  check('新增关联关系的说法', cp.patchWhere({ path: 'relations.r1', before: null, after: { to_table: 'visits' } }) === '新增指向 visits 的关联关系')
  check('关联关系写成一行', cp.patchValueText('relations.r1', { columns: ['visit_id'], to_table: 'visits', to_columns: ['id'],
    cardinality: 'many_to_one' }) === 'visit_id → visits.id（多对一）')
  check('表类型、度量类型写界面上的叫法', cp.patchValueText('kind', 'fact') === '明细表' && cp.patchValueText('columns.a.measure', 'stock') === '存量')
  check('没有值写「未填写」', cp.patchValueText('grain', null) === '未填写')
  check('码值含义为空（剖析的候选）写「含义待填写」', cp.patchValueText('columns.status.codes', { 1: '有效', 9: '' }) === '1=有效、9=含义待填写')
  check('列名里有句点照样认', JSON.stringify(cp.patchTarget('columns.a.b.unit')) === '{"kind":"column","column":"a.b","field":"unit"}')
  check('形状不对不出卡片、不出行', cp.catalogPatchesOf([
    { op: 'catalog_patch' }, { op: 'catalog_patch', source: 's', source_id: 'i', table: 't', changes: [{ path: 'nope', after: 1 }] },
  ]).length === 0 && mod.decodeCopilot([{ op: 'catalog_patch', source: 's', table: 't' }]).length === 0)
})

await section('正式运行：发布之后数据目录有变化（catalog.drift 事件）', async () => {
  const steps = mod.decodeRun([
    { seq: 1, type: 'run.started', node_id: null, ts: 1, data: { nodes: 1 } },
    { seq: 2, type: 'catalog.drift', node_id: null, ts: 1.01, data: { version: 3, count: 2, tables: [
      { source: 'scenic', source_id: 'src-scenic', table: 'visits', label: '入园记录', published: 3, current: 5 },
      { source: 'scenic', source_id: 'src-scenic', table: 'parks', label: null, published: 0, current: 1 },
    ] } },
  ])
  const row = steps.find((s) => s.code === 'catalog_drift')
  check('一行提醒：「入园记录」等 2 张表有变化', row?.title === '自发布以来，数据目录中「入园记录」等 2 张表有变化', row?.title)
  check('是警告、不是失败', row?.level === 'warn' && row?.status === 'done' && row?.kind === 'note')
  check('副标题写明运行未被拦截', !!row?.sub?.includes('运行未被拦截'), row?.sub)
  check('展开逐表写版本，没有目录写「尚无目录」', !!row?.detail?.includes('「scenic」入园记录（visits）：发布时第 3 版，现为第 5 版')
    && row.detail.includes('「scenic」parks：发布时尚无目录，现为第 1 版'), row?.detail)
  const one = mod.catalogDrift({ tables: [{ source: 's', table: 't', label: null, published: 1, current: 2 }] })
  check('只有一张表：标题不写「等」', one?.title === '自发布以来，数据目录中「t」有变化', one?.title)
  check('老事件的表没有 impact：按直接引用', one?.direct.length === 1 && one?.possible.length === 0 && one?.tables[0].impact === 'direct')
  // Agent 可能查询的表（SQL 运行时才写）：和直接引用分开说
  const mixed = mod.catalogDrift({ tables: [
    { source: 'scenic', source_id: 'src-scenic', table: 'visits', label: '入园记录', published: 3, current: 5, impact: 'direct' },
    { source: 'scenic', source_id: 'src-scenic', table: 'orders', label: '订单', published: 2, current: 4, impact: 'possible' },
    { source: 'scenic', source_id: 'src-scenic', table: 'gates', label: null, published: 0, current: 1, impact: 'possible' },
  ] })
  check('两组都有：标题说直接引用的，再补一句 Agent 可能查询的表有变化',
    mixed?.title === '自发布以来，数据目录中「入园记录」有变化；另有 2 张 Agent 可能查询的表有变化', mixed?.title)
  check('……展开：直接引用在前，可能涉及的在小标题下面', mixed?.detail === [
    '「scenic」入园记录（visits）：发布时第 3 版，现为第 5 版', 'Agent 可能查询的表：',
    '「scenic」订单（orders）：发布时第 2 版，现为第 4 版', '「scenic」gates：发布时尚无目录，现为第 1 版'].join('\n'), mixed?.detail)
  const agentOnly = mod.catalogDrift({ tables: [
    { source: 'scenic', source_id: 'src-scenic', table: 'orders', label: '订单', published: 2, current: 4, impact: 'possible' },
  ] })
  check('只有可能涉及的：标题直说「Agent 可能查询的表有变化」', agentOnly?.title === '自发布以来，Agent 可能查询的表有变化：「订单」', agentOnly?.title)
  const run = mod.decodeRun([{ seq: 1, type: 'catalog.drift', node_id: null, ts: 1, data: { version: 2, count: 1, tables: [
    { source: 'scenic', source_id: 'src-scenic', table: 'orders', label: '订单', published: 2, current: 4, impact: 'possible' }] } }])
    .find((s) => s.code === 'catalog_drift')
  check('……时间线里那一行同样是警告', run?.level === 'warn' && run?.title === agentOnly?.title, run?.title)
  check('形状不对不出行', mod.decodeRun([{ seq: 1, type: 'catalog.drift', node_id: null, ts: 1, data: { tables: 'x' } }])
    .every((s) => s.code !== 'catalog_drift'))
})

await section('正式运行：发布之后表结构有变化（schema.drift 事件）', async () => {
  const steps = mod.decodeRun([
    { seq: 1, type: 'run.started', node_id: null, ts: 1, data: { nodes: 1 } },
    { seq: 2, type: 'schema.drift', node_id: null, ts: 1.01, data: { version: 2, count: 2, tables: [
      { source: 'shop', source_id: 'src-shop', table: 'orders', label: '订单', published: 1, current: 2, removed: false,
        changes: [{ id: 'c1', kind: 'column_removed', column: 'status', severity: 'action', summary: 'orders.status：删除了列' },
          { id: 'c2', kind: 'column_type_changed', column: 'amount', severity: 'warning', summary: 'orders.amount：列的类型变了' }],
        more: 3 },
      { source: 'shop', source_id: 'src-shop', table: 'refunds', label: null, published: 4, current: null, removed: true,
        changes: [], more: 0 },
    ] } },
  ])
  const row = steps.find((s) => s.code === 'schema_drift')
  check('一行提醒：「订单」等 2 张表的结构有变化', row?.title === '自发布以来，「订单」等 2 张表的结构有变化', row?.title)
  check('是警告、不是失败', row?.level === 'warn' && row?.status === 'done' && row?.kind === 'note')
  check('副标题写明运行未被拦截', !!row?.sub?.includes('运行未被拦截'), row?.sub)
  check('展开逐表写结构版本，表删了写「表已删除」', !!row?.detail?.includes('「shop」订单（orders）：结构第 1 版 → 第 2 版')
    && row.detail.includes('「shop」refunds：表已删除'), row?.detail)
  check('……下面列出还没处理的变化和另有几条', !!row?.detail?.includes('· orders.status：删除了列')
    && row.detail.includes('另有 3 条结构变化'), row?.detail)
  const one = mod.schemaDrift({ tables: [{ source: 's', table: 't', published: 1, current: 2 }] })
  check('只有一张表：标题不写「等」', one?.title === '自发布以来，「t」的结构有变化', one?.title)
  check('和目录漂移分开记：catalog.drift 的解析不认它', mod.catalogDrift({ tables: [] }) === null)
  check('形状不对不出行', mod.decodeRun([{ seq: 1, type: 'schema.drift', node_id: null, ts: 1, data: { tables: 'x' } }])
    .every((s) => s.code !== 'schema_drift'))
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

  const err = mod.decodeCopilot([{ op: 'error', message: '助手本轮未完成：等待超时：服务方未在限定时间内响应', hint: '稍后重试', detail: 'httpx.ReadTimeout' }])
  const row = err.find((s) => s.kind === 'error')
  check('报错：人话当标题、怎么办当副标题、原文进技术细节',
    row?.title.startsWith('助手本轮未完成') && row.sub === '稍后重试' && row.raw === 'httpx.ReadTimeout',
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
    pending?.status === 'running' && pending.title === '轮数用完 · 调度者正在做最后判定…', `${pending?.status} ${pending?.title}`)

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
  check('失败的成员带着原因', !!r0?.error?.includes('未实际调用工具'), r0?.error)
  const r1 = direct?.rounds[1]?.members.find((m) => m.agent === '分析员')
  check('同一轮做完的成员照常是 done', r1?.status === 'done', r1?.status)
  const memberRow = flatten(mod.decodeRun(synthetic.exhaustedTeam('fail')))
    .find((s) => s.title.startsWith('取数员：') && s.status === 'failed')
  check('失败的成员那一行也画成失败', !!memberRow && memberRow.level === 'error', memberRow?.status)
  check('失败的成员那一行说清为什么', !!memberRow?.sub?.includes('未实际调用工具'), memberRow?.sub)
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
  check('降档那条提醒说人话', note?.title === '协作团队已用完 2 轮仍未完成 · 按降档交付', note?.title)
  check('降档那条提醒给出下一步', !!note?.next && note.fix === 'canvas', `${note?.next} / ${note?.fix}`)
  // 认不出轮数（老后端、措辞变了）时照实说「已用完轮数」，不拿「?」凑一个数。
  // 下面这条 message 故意是一种认不出轮数的说法（不是后端现在的原文），不要改成新原文
  const vague = flatten(mod.decodeRun([
    { seq: 1, type: 'node.started', node_id: 'team', ts: 1, data: { node_type: 'supervisor', label: '团队' } },
    { seq: 2, type: 'log', node_id: 'team', ts: 2, data: { level: 'warn', code: 'team_exhausted', message: '协作团队没做完，按降档交付' } },
    { seq: 3, type: 'node.finished', node_id: 'team', ts: 3, data: { duration_ms: 5, preview: { text: 'x', exhausted: true } } },
  ]))
  const vagueNote = vague.find((s) => s.code === 'team_exhausted')
  const vagueNode = vague.find((s) => s.kind === 'node' && s.nodeId === 'team')
  check('认不出轮数时不写「?」', !vagueNote?.title.includes('?') && !vagueNode?.sub?.includes('?')
    && !!vagueNote?.title.includes('已用完轮数'), `${vagueNote?.title} / ${vagueNode?.sub}`)
})

await section('模型把工具调用写成了文字（NI-4）', async () => {
  const all = flatten(mod.decodeRun(synthetic.markupRun()))
  const warn = all.find((s) => s.code === 'tool_markup_leak')
  check('提醒行说人话，不贴原始标记', warn?.title === '模型以文本形式输出了工具调用，未实际执行'
    && !warn.title.includes('DSML'), warn?.title)
  check('说清已经提醒它重试', !!warn?.sub?.includes('重试一次'), warn?.sub)
  check('给出下一步：去画布绑定工具', !!warn?.next?.includes('绑定') && warn.fix === 'canvas', `${warn?.next} / ${warn?.fix}`)
  check('原始标记留在展开区给排查', !!warn?.detail?.includes('DSML'))
  const team = flatten(mod.decodeRun(synthetic.exhaustedTeam('fail'))).find((s) => s.code === 'tool_markup_leak')
  check('成员写成文字时说是哪个成员', team?.title === '取数员以文本形式输出了工具调用，未实际执行', team?.title)
  const node = mod.decodeRun(synthetic.markupRun()).find((s) => s.nodeId === 'query')
  check('失败的节点照常是失败，原因留在详情', node?.status === 'failed' && !!node.detail?.includes('原始标记'))
  // 收尾轮那种：真调过工具，只是步数用完了还想接着查。节点照常完成，不能说成「未实际执行」
  const settle = flatten(mod.decodeRun([
    { seq: 1, type: 'node.started', node_id: 'q', ts: 1, data: { node_type: 'agent', label: '数据查询' } },
    { seq: 2, type: 'log', node_id: 'q', ts: 2, data: { level: 'warn', code: 'tool_markup_leak',
      message: '模型以文本形式输出了工具调用（<tool_call>{"name": "db_query__shop"…），未实际调用工具：已达步数上限，收尾时仍试图调用工具，未能给出结论' } },
    { seq: 3, type: 'node.finished', node_id: 'q', ts: 3, data: { duration_ms: 9, preview: { text: '华东 1,204 单' } } },
  ])).find((s) => s.code === 'tool_markup_leak')
  check('收尾轮还想查：说已达步数上限，不说未实际执行', !!settle?.title.includes('已达步数上限') && !settle.title.includes('未实际执行'),
    settle?.title)
  check('收尾轮还想查：下一步是调大最大步数', !!settle?.next?.includes('最大步数'), settle?.next)
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
  check('没有修复行可折时单独成一行说人话', !!orphan?.title.includes('修复已作废') && orphan.level === 'warn', orphan?.title)
})

await section('工具时限（timeout_s / timed_out）', async () => {
  const live = flatten(mod.decodeRun(synthetic.timeoutRun('live'))).find((s) => s.kind === 'query')
  check('进行中的查询带着时限', live?.limitS === 30 && live.status === 'running', `${live?.limitS} ${live?.status}`)
  const done = flatten(mod.decodeRun(synthetic.timeoutRun('done'))).find((s) => s.kind === 'query')
  check('超时的查询是失败', done?.status === 'failed' && done.level === 'error', done?.status)
  check('超时的查询说清超了多少上限、已停止等待', done?.sub === '超过 30 秒上限，已停止等待', done?.sub)
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

  // 用完轮数判失败 → 调大轮数继续运行（后端也标 resumed:true）→ 成功
  const rerun = feed(synthetic.rerunTeam('rerun'))
  check('继续运行成功后：结局不再是上一次的「用完 2 轮仍未完成」', !rerun.team?.verdict?.outcome
    && !rerun.team?.verdict?.never, JSON.stringify(rerun.team?.verdict))
  check('继续运行成功后：团队收尾了', rerun.team?.finished === true)
  check('继续运行成功后：泳道只剩这一次的 1 轮', rerun.team?.rounds.length === 1
    && rerun.team.rounds[0].members.length === 1 && rerun.team.rounds[0].members[0].result === '查到 128 单',
    JSON.stringify(rerun.team?.rounds.map((r) => r.members.map((m) => m.result))))
  const atFailure = rerun.seen.find(([e]) => e.type === 'node.failed')?.[1]
  check('判失败的那一刻结局照常是 failed', atFailure?.verdict?.outcome === 'failed', JSON.stringify(atFailure?.verdict))
  const rerunNode = mod.decodeRun(synthetic.rerunTeam('rerun')).find((s) => s.nodeId === 'team')
  check('右栏泳道：继续运行成功后不画「用完 N 轮」的结局', !rerunNode?.team?.verdict?.outcome,
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
  check('降档的节点行写「用完 2 轮」，不写「用完 ? 轮」', midNode?.sub === '已用完 2 轮仍未完成，按降档交付'
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
  check('审批批准后的重放不算新的一次：第 1 轮的成员还在', approval.team?.rounds.length === 1
    && approval.team.rounds[0].members[0]?.result === '改好了' && approval.team.finished === true,
    JSON.stringify(approval.team?.rounds))
  check('审批收尾后不再挂着「等重放」的记号', !approval.team?.paused, JSON.stringify(approval.team))
})

await section('Copilot 自查问题：对象形状、节点名、超出限定范围（3C REQ-6）', async () => {
  const steps = mod.decodeCopilot(synthetic.COPILOT_OBJECT_ISSUES, { context: 'chat' })
  const repairing = steps.find((s) => s.title.startsWith('自查发现'))
  check('对象形状：交回去改的那一行写节点名，不写「lp」', !!repairing?.detail?.startsWith('「逐周循环」循环条件有误')
    && !repairing.detail.includes('「lp」'), repairing?.detail)
  const failed = steps.find((s) => s.kind === 'error')
  check('对象形状：没修好的问题也写节点名', !!failed?.detail?.includes('「订单查询」使用了限定范围之外的数据源'), failed?.detail)
  check('认不出名字的节点退到 id', !!failed?.detail?.includes('「gone」'), failed?.detail)
  check('超出限定范围：单独给一句下一步', failed?.code === 'datasource_out_of_scope' && !!failed.next?.includes('限定'),
    `${failed?.code} / ${failed?.next}`)
  const canvasNames = mod.decodeCopilot(synthetic.COPILOT_OBJECT_ISSUES, { context: 'canvas', labelOf: (id) => ({ old: '旧的汇总', gone: '质量门' })[id] })
  check('画布给了节点名：操作流里没出现过的也写名字', !!canvasNames.find((s) => s.kind === 'error')?.detail?.includes('「质量门」'),
    canvasNames.find((s) => s.kind === 'error')?.detail)
  check('「移除了」那一行也写名字', canvasNames.some((s) => s.title === '移除了「旧的汇总」'),
    canvasNames.filter((s) => s.title.startsWith('移除了')).map((s) => s.title).join(','))
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
  check('issueLine 用名字', mod.issueLine(scoped) === '「订单查询」使用了限定范围之外的数据源 sales_daily：本轮只允许使用 orders',
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
  check('撞上限退出：执行了几轮、一共几项都说', /执行了 10 轮/.test(capped) && /共 20 项/.test(capped), capped)
  const whileDone = loop({ branch: 'done', iteration: 3, total: null, mode: 'while' })
  check('while 走完：执行了 3 轮', /执行了 3 轮/.test(whileDone) && !/第/.test(whileDone), whileDone)
  const whileNone = loop({ branch: 'done', iteration: 0, total: null, mode: 'while' })
  check('while 一轮都没执行', /（未执行）/.test(whileNone), whileNone)
  // 没有图的时候，循环的两个出口和兜底出口也不写 body / done / default
  check('循环体出口不写 body', /转入「循环体」分支/.test(loop({ branch: 'body', iteration: 0, total: 2, mode: 'foreach' })))
  check('结束出口不写 done', /转入「结束」分支/.test(done5), done5)
  const fallback = branchRow([E(1, 'node.started', 'br'), E(2, 'edge.taken', 'br', { branch: 'default', reason: '都不满足', mode: 'expression' })])[0]
  check('兜底出口写「其他」', fallback?.title === '转入「其他」分支', fallback?.title)

  // 画布给了出口说明：写说明，不写 key
  const labelOf = mod.exitLabels(
    [{ id: 'br', type: 'branch', config: { cases: [{ key: 'team', label: '交给团队' }, { key: 'solo', label: '' }] } },
     { id: 'each', type: 'loop', config: {} }, { id: 'x', type: 'agent', config: {} }],
    (type, config) => (type === 'branch'
      ? [...config.cases.map((c) => ({ id: c.key, label: c.label || c.key })), { id: 'default', label: '其他' }]
      : type === 'loop' ? [{ id: 'body', label: '每一项' }, { id: 'done', label: '收尾' }] : [{ id: 'out', label: '' }]),
  )
  const labelled = branchRow([E(1, 'node.started', 'br'), E(2, 'edge.taken', 'br', { branch: 'team', mode: 'llm' })], { exitLabelOf: labelOf })[0]
  check('分支行写 case 的说明，不写 key', labelled?.title === '转入「交给团队」分支', labelled?.title)
  const bare = branchRow([E(1, 'node.started', 'br'), E(2, 'edge.taken', 'br', { branch: 'solo', mode: 'expression' })], { exitLabelOf: labelOf })[0]
  check('case 没写说明：退回 key', bare?.title === '转入「solo」分支', bare?.title)
  const gone = branchRow([E(1, 'node.started', 'br'), E(2, 'edge.taken', 'br', { branch: 'removed', mode: 'expression' })], { exitLabelOf: labelOf })[0]
  check('图上已经没有这个出口：退回 key', gone?.title === '转入「removed」分支', gone?.title)
  const loopLabelled = branchRow([E(1, 'node.started', 'each'), E(2, 'edge.taken', 'each', { branch: 'done', iteration: 2, total: 2, mode: 'foreach' })], { exitLabelOf: labelOf })[0]
  check('循环出口也按图上的叫法', loopLabelled?.title === '转入「收尾」分支（共 2 项）', loopLabelled?.title)
  check('没有出口的节点不进表', labelOf('x', 'out') === undefined)
})

await section('报告核对 report.checked、口径卡的台账（可点击证据第一期）', async () => {
  // 夹具由后端 evidence.py 的 compose_doc 真跑生成（只用通用名），report_checked 是报告节点发的那条事件的载荷
  const evidence = JSON.parse(readFileSync(`${root}frontend/src/run/__tests__/evidence-doc.json`, 'utf8'))
  const rc = evidence.report_checked
  const E = (seq, type, node_id, data = {}) => ({ seq, type, node_id, ts: 1790000000 + seq, data })
  // 修复重写那条日志的原话：和 backend/app/engine/nodes/report.py 发的同一个格式（_named 用「、」
  // 接着点名的字）。下面先核对后端源码里还是这个格式，免得这里的夹具自说自话
  const REPAIR_MESSAGE = '报告有 3 处未通过核对（「12」、「m:nope」、「3」），已要求模型重写（第 1 次）'
  const reportPy = readFileSync(`${root}backend/app/engine/nodes/report.py`, 'utf8')
  check('后端发修复日志的格式还是夹具里这一种', reportPy.includes('code="report_repair"')
    && reportPy.includes('处未通过核对（{_named(blocking)}），') && reportPy.includes('已要求模型重写（第 {repairs} 次）')
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
    // 后端 report.py 原样的说法：「报告有 N 处未通过核对（点名的字），已要求模型重写（第 N 次）」
    E(5, 'log', 'write', { level: 'warn', code: 'report_repair', message: REPAIR_MESSAGE }),
    E(6, 'report.checked', 'write', rc),
    E(7, 'node.finished', 'write', { duration_ms: 2300, preview: { text: evidence.doc.markdown.slice(0, 40), doc_artifact: rc.doc_artifact } }),
    E(8, 'run.finished', null, { output: {} }),
  ]
  const steps = mod.decodeRun(events)
  const all = flatten(steps)
  check('新事件都有翻译，没有「未识别的事件」', !all.some((s) => s.title === '未识别的事件'),
    all.filter((s) => s.title === '未识别的事件').map((s) => s.detail?.split('\n')[0]).join('、'))
  const node = steps.find((s) => s.nodeId === 'write' && s.kind === 'node')
  check('没起名的报告节点叫「报告撰写」', node?.title === '报告撰写', node?.title)
  const row = all.find((s) => s.code === 'report_checked')
  check('report.checked 解码成一条步骤，挂在报告节点下面', !!row && !!node?.children?.includes(row), row?.title)
  check('标题说几个数字有出处、几个没有（算得平：12 − 7 = 5），非数字引用另起一句（和横幅同一种说法）',
    row?.title === '核对报告：7/12 数字有出处 · 无证据 5 · 另有 1 处引用无法解析', row?.title)
  check('有无证据的地方就是提醒，不是安静的一行', row?.level === 'warn', row?.level)
  check('能下钻到报告文档', row?.artifact === rc.doc_artifact, row?.artifact)
  // 「这种引用在后续版本支持」是 evidence-doc.json 里一期老文档存下的原因（后端 LEGACY_LATER_REASON），夹具有意保留，
  // 这里借它确认老文档的违规原话照样进展开区
  check('违规清单放进展开区', !!row?.detail?.includes('数字「12」没有出处') && !!row.detail.includes('这种引用在后续版本支持'),
    row?.detail?.slice(0, 80))
  check('重写过几次写在行尾', row?.meta === '重写 1 次', row?.meta)
  const repair = all.find((s) => s.code === 'report_repair')
  check('修复重写说人话：几处未通过、第几次，原话留在展开区', repair?.title === '报告有 3 处未通过核对，已要求模型按清单重写（第 1 次）'
    && repair.detail === REPAIR_MESSAGE, `${repair?.title} / ${repair?.detail}`)
  check('点名的字放进副标题（「12」「m:nope」这类）', repair?.sub === '「12」、「m:nope」、「3」', repair?.sub)
  const oldSaying = flatten(mod.decodeRun([E(1, 'node.started', 'w', { node_type: 'report' }),
    E(2, 'log', 'w', { level: 'warn', code: 'report_repair', message: '报告要重写' })])).find((s) => s.code === 'report_repair')
  check('别的说法（老后端）：标题照样说人话，不瞎填数', oldSaying?.title === '报告未通过核对，已要求模型按清单重写'
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

  // 写作目录超出篇幅（2026-10 一次运行：上百次查询只有前二十来次进了写作者的视野，界面上一个字都没说）：
  // 现在按来源均衡地展开，没展开的列编号和摘要，并发一条警告。界面读结构化字段（queries、omitted、nodes）
  check('后端发目录有省略的警告，带着结构化字段', reportPy.includes('code="report_catalog_trimmed"')
    && reportPy.includes('queries=writer.queries, omitted=len(writer.omitted)') && reportPy.includes('"node_id", "title", "total", "omitted"'),
    '改了 report.py 的字段就同步改 decode.ts 的 explainLog 和这里的夹具')
  const TRIMMED = '证据目录超出篇幅（约 51200 字，按写作模型的上下文窗口确定）：164 次查询中 90 次列出了数据，74 次只列编号和摘要'
    + '（卡券核销省略 52 次、活动内容省略 22 次）。写作模型看不到这些查询的明细，报告不得据此断言没有数据'
  const trimmed = flatten(mod.decodeRun([E(1, 'node.started', 'w', { node_type: 'report', label: '写报告' }),
    E(2, 'log', 'w', { level: 'warn', code: 'report_catalog_trimmed', message: TRIMMED, budget: 51200, window: null, queries: 164,
      omitted: 74, nodes: [{ node_id: 'a', title: '会员专项', total: 26, omitted: 0 }, { node_id: 'b', title: '卡券核销', total: 79, omitted: 52 },
        { node_id: 'c', title: '活动内容', total: 42, omitted: 22 }] })])).find((s) => s.code === 'report_catalog_trimmed')
  check('report_catalog_trimmed：标题说几次查询只列了编号和摘要', trimmed?.title === '证据目录超出篇幅，164 次查询中 74 次只列了编号和摘要', trimmed?.title)
  check('……副标题按来源列省略了几次（没省略的来源不列）', trimmed?.sub === '卡券核销 省略 52/79 次、活动内容 省略 22/42 次', trimmed?.sub)
  // 放不下但查询全都列出、只是有的只列前几行：不写「0 次只列了编号和摘要」
  const rowsOnly = flatten(mod.decodeRun([E(1, 'node.started', 'w', { node_type: 'report', label: '写报告' }),
    E(2, 'log', 'w', { level: 'warn', code: 'report_catalog_trimmed', message: '证据目录超出篇幅', budget: 51200, window: null,
      queries: 140, omitted: 0, compact: 38, compact_rows: 3, nodes: [{ node_id: 'a', title: '会员专项', total: 60, omitted: 0, compact: 0 },
        { node_id: 'b', title: '卡券核销', total: 80, omitted: 0, compact: 38 }] })])).find((s) => s.code === 'report_catalog_trimmed')
  check('……查询全都列出、有的只列前几行：标题照实说，不写「0 次只列了编号和摘要」',
    rowsOnly?.title === '证据目录超出篇幅，140 次查询都列出了数据，38 次只列了前几行' && !rowsOnly?.title?.includes('编号和摘要'), rowsOnly?.title)
  check('……副标题按来源列只列了前几行的次数', rowsOnly?.sub === '卡券核销 38/80 次只列前几行', rowsOnly?.sub)
  check('……下一步说其余的行仍在台账里', !!rowsOnly?.next?.includes('只看到前几行') && !!rowsOnly?.next?.includes('不得据此断言没有数据'), rowsOnly?.next)
  const notesOnly = flatten(mod.decodeRun([E(1, 'node.started', 'w', { node_type: 'report', label: '写报告' }),
    E(2, 'log', 'w', { level: 'warn', code: 'report_catalog_trimmed', message: '证据目录超出篇幅', queries: 12, omitted: 0, compact: 0,
      nodes: [{ node_id: 'a', title: '会员专项', total: 12, omitted: 0, compact: 0 }] })])).find((s) => s.code === 'report_catalog_trimmed')
  check('……只省略了表和字段的说明：照实说', notesOnly?.title === '证据目录超出篇幅，12 次查询都列出了数据，表和字段的说明有省略', notesOnly?.title)
  check('……下一步说被省略的仍在台账里、不得据此断言没有数据，以及怎么看全', !!trimmed?.next?.includes('不得据此断言没有数据')
    && !!trimmed?.next?.includes('上下文窗口更大') && trimmed?.level === 'warn' && trimmed?.detail === TRIMMED, trimmed?.next)
  const trimmedBare = flatten(mod.decodeRun([E(1, 'log', 'w', { level: 'warn', code: 'report_catalog_trimmed', message: '目录有省略' })]))
    .find((s) => s.code === 'report_catalog_trimmed')
  check('……缺字段时标题照样说人话，不出 undefined', trimmedBare?.title === '证据目录超出篇幅，部分查询只列了编号和摘要'
    && trimmedBare?.sub === undefined, `${trimmedBare?.title} / ${trimmedBare?.sub}`)

  // 目录有省略时报告写了缺失断言（「没有查到」「数据缺失」），交回重写一次仍在：只标注、不判失败，时间线单独一行说清
  check('后端发缺失断言的警告，带着 count、texts', reportPy.includes('code="report_absence_claim"')
    && reportPy.includes('count=len(absent), texts=texts'), '改了 report.py 的字段就同步改 decode.ts 的 explainLog 和这里的夹具')
  const ABSENT = '报告在证据目录有省略时写了 2 处缺失断言（「数据缺失」、「没有查到」），已要求模型重写 1 次仍未改掉：'
    + '被省略的查询仍在证据台账里，这类结论依据不足，应写作本报告未覆盖。报告照常产出，这几处已标注'
  const absentRun = flatten(mod.decodeRun([E(1, 'node.started', 'w', { node_type: 'report', label: '写报告' }),
    E(2, 'log', 'w', { level: 'warn', code: 'report_absence_claim', message: ABSENT, count: 2, texts: ['数据缺失', '没有查到'] }),
    E(3, 'report.checked', 'w', { doc_artifact: 'abc', repairs: 1, ok: false, on_violation: 'fail', failed: false,
      stats: { numbers: 0, numbers_cited: 0, uncited_numbers: 0, unresolved: 0 },
      violations: [{ code: 'absence_claim', text: '数据缺失', message: '证据目录有省略时写了缺失断言：写作模型没有看到全部证据，「没有查到」「不存在」「缺失」这类结论依据不足' }] }),
  ]))
  const ab = absentRun.find((s) => s.code === 'report_absence_claim')
  check('report_absence_claim：标题说几处缺失断言、已标注、为什么（目录有省略）', ab?.title === '报告写了 2 处缺失断言，已标注（证据目录有省略）', ab?.title)
  check('……副标题点名那几处说法', ab?.sub === '「数据缺失」、「没有查到」', ab?.sub)
  check('……下一步说被省略的仍在台账里、应写「本报告未覆盖」；是提醒（warn），原话留在展开区',
    !!ab?.next?.includes('本报告未覆盖') && !!ab?.next?.includes('证据台账') && ab?.level === 'warn' && ab?.detail === ABSENT, ab?.next)
  const rcAbsent = absentRun.find((s) => s.code === 'report_checked')
  check('……核对那一行照常完成（不判失败），违规清单里写着这条的说明', rcAbsent?.status === 'done'
    && !!rcAbsent?.detail?.includes('缺失断言') && !!rcAbsent?.detail?.includes('依据不足'), `${rcAbsent?.status} ${rcAbsent?.detail}`)
})

await section('报告引用了取自遮罩列的格子（report_masked_cell）：标题说几处、已标注，副标题点名引用，不写那几格的值', async () => {
  // 契约：data = {level: warn, code: report_masked_cell, message, count, refs, columns}，重写后仍在、「违规处置」
  // 为标注时才发。自查只查 [[v:]] 引用，不按值认正文里写着的值（复核第 3 轮去掉了按值认，日志也不再带 written）
  const E = (seq, type, node_id, data = {}) => ({ seq, type, node_id, ts: 1790000000 + seq, data })
  const reportPy = readFileSync(`${root}backend/app/engine/nodes/report.py`, 'utf8')
  check('后端发引用了遮罩列的警告，带着 count、refs、columns，不再带按值认的 written', reportPy.includes('code="report_masked_cell"')
    && reportPy.includes('count=len(hits), refs=refs') && reportPy.includes('columns=list(dict.fromkeys(')
    && !reportPy.includes('written=sum('),
    '改了 report.py 的字段就同步改 decode.ts 的 explainLog 和这里的夹具')
  const MASKED = '报告引用了 2 处取自遮罩列的格子（「v:Q2.r0.tel」、「v:Q2.r1.tel」），已要求模型重写 1 次仍未改掉：'
    + '数据源设置了遮罩的列不应写进报告。报告照常产出，这几处已标注，已记为缺口。请在报告撰写节点的指令中去掉对这些列的引用'
  const steps = flatten(mod.decodeRun([E(1, 'node.started', 'w', { node_type: 'report', label: '写报告' }),
    E(2, 'log', 'w', { level: 'warn', code: 'report_masked_cell', message: MASKED, count: 2,
      refs: ['v:Q2.r0.tel', 'v:Q2.r1.tel'], columns: ['tel'] }),
    E(3, 'report.checked', 'w', { doc_artifact: 'abc', repairs: 1, ok: false, on_violation: 'flag', failed: false,
      stats: { numbers: 1, numbers_cited: 1, uncited_numbers: 0, unresolved: 0 },
      violations: [{ code: 'masked_cell', ref: 'v:Q2.r0.tel', column: 'tel',
        message: '引用了取自遮罩列的格子 [[v:Q2.r0.tel]]：列「tel」按 SQL 认出取自设置了遮罩的列「phone」，值不应写进报告' }] }),
  ]))
  const mc = steps.find((s) => s.code === 'report_masked_cell')
  check('标题说几处引用了遮罩列、已标注', mc?.title === '报告引用了 2 处取自遮罩列的格子，已标注', mc?.title)
  check('……副标题点名那几处引用（不写值）', mc?.sub === '[[v:Q2.r0.tel]]、[[v:Q2.r1.tel]]', mc?.sub)
  check('……下一步说遮罩列不应写进报告、已记为缺口、去改指令，不预告出具降档（探索运行、没有出具契约的成果不出具）；'
    + '是提醒（warn），原话留在展开区',
    !!mc?.next?.includes('不应写进报告') && !!mc?.next?.includes('记为缺口') && !!mc?.next?.includes('指令')
    && !mc?.next?.includes('降档') && !mc?.next?.includes('随报告出具') && mc?.level === 'warn' && mc?.detail === MASKED,
    mc?.next)
  check('……后端的缺口、日志原话同样不预告出具（不写「随报告出具」「降档」）', !reportPy.includes('这些值会随报告出具')
    && !reportPy.includes('出具已按缺口降档'), '改 report.py 里 report_masked_cell 的说法')
  const rc = steps.find((s) => s.code === 'report_checked')
  check('……核对那一行照常完成（标注，不判失败），违规清单里写着这一条的说明', rc?.status === 'done'
    && !!rc?.detail?.includes('取自设置了遮罩的列'), `${rc?.status} ${rc?.detail}`)
  const bare = flatten(mod.decodeRun([E(1, 'log', 'w', { level: 'warn', code: 'report_masked_cell', message: '报告引用了遮罩列' })]))
    .find((s) => s.code === 'report_masked_cell')
  check('……缺字段时标题照样说人话，不出 undefined', bare?.title === '报告引用了取自遮罩列的格子，已标注'
    && bare?.sub === undefined, `${bare?.title} / ${bare?.sub}`)
  // 第 2 轮的日志带过 written（按值认出的几处）：现在的后端不再发，万一读到也只按引用说，不改标题
  const legacy = flatten(mod.decodeRun([E(1, 'log', 'w', { level: 'warn', code: 'report_masked_cell',
    message: '报告引用了遮罩列', count: 1, written: 1, refs: ['v:Q2.r0.tel'] })])).find((s) => s.code === 'report_masked_cell')
  check('……带着 written 的日志照引用说', legacy?.title === '报告引用了 1 处取自遮罩列的格子，已标注', legacy?.title)
})

await section('整句面板挂的依据：按证据图的标签写，不读文档目录里带值的标签（citeLabel）', async () => {
  const ev = await import(await load('/src/lib/evidence.ts'))
  const doc = { catalog: {
    'm:phone_raw': { kind: 'metric', name: '首位客户手机号', label: '首位客户手机号 = 19900000301' },
    Q1: { kind: 'query', label: 'db_query__shop · 3 行' },
  } }
  const graph = { evidence: [{ alias: 'm:phone_raw', report: 'write', label: '首位客户手机号 = 已遮罩' },
    { alias: 'Q1', report: 'write', label: 'db_query__shop · 3 行' }] }
  check('证据图有这条依据：用证据图的标签（服务端遮过）', ev.citeLabel('m:phone_raw', doc, graph, 'write', true)
    === '首位客户手机号 = 已遮罩')
  check('……证据图还没回来：指标只写名字，不写目录里的值', ev.citeLabel('m:phone_raw', doc, undefined, 'write', true)
    === '首位客户手机号')
  check('……查询这类不带值的依据照目录写', ev.citeLabel('Q1', doc, undefined, 'write', true) === 'db_query__shop · 3 行')
  check('……证据图里是另一份报告的同名依据：不拿来用', ev.citeLabel('m:phone_raw', doc,
    { evidence: [{ alias: 'm:phone_raw', report: 'other', label: '首位客户手机号 = 1' }] }, 'write', true) === '首位客户手机号')
  check('……没有运行（预览）：照目录写', ev.citeLabel('m:phone_raw', doc, undefined, undefined, false)
    === '首位客户手机号 = 19900000301')
})

await section('工具门控 tool.gated（工具信任三档）', async () => {
  // 契约：data = {tool, verdict: allow|escalate, reason, model, duration_ms, call_id?, agent?}，node_id 是所在节点
  const E = (seq, type, node_id, data = {}) => ({ seq, type, node_id, ts: 1790000000 + seq, data })
  const gated = (steps) => flatten(steps).filter((s) => s.code === 'tool_gated')
  const nodeOf = (steps, id) => steps.find((s) => s.nodeId === id && s.kind === 'node')

  // 通过：info 级的一行，挂在 agent 节点下，接着是正常的工具调用
  const allowRun = mod.decodeRun([
    E(1, 'node.started', 'agent', { node_type: 'agent', label: '查档案' }),
    E(2, 'tool.gated', 'agent', { tool: 'crm_lookup', verdict: 'allow', reason: '只读查询，参数正常', model: 'tiny-1', duration_ms: 820, call_id: 'c1' }),
    E(3, 'tool.start', 'agent', { tool: 'crm_lookup', args: { id: 'C-1' }, call_id: 'c1' }),
    E(4, 'tool.end', 'agent', { tool: 'crm_lookup', call_id: 'c1', preview: '{"name":"示例客户"}', duration_ms: 40 }),
  ])
  const allow = gated(allowRun)[0]
  check('通过：说「门控通过 <工具>：<理由>」', allow?.title === '门控通过 crm_lookup：只读查询，参数正常', allow?.title)
  check('……info 级，不是提醒', allow?.level === 'info', allow?.level)
  check('……挂在所在节点下面，排在工具调用前', nodeOf(allowRun, 'agent')?.children?.[0] === allow
    && nodeOf(allowRun, 'agent')?.children?.[1]?.kind === 'tool', nodeOf(allowRun, 'agent')?.children?.map((c) => c.kind).join(','))
  check('……行尾写门控用的模型和耗时', allow?.meta === '门控 tiny-1 · 820 ms', allow?.meta)
  // 问门控时那次模型调用已经答完了：不能在门控那几秒里还转着「思考并作答」
  const thinking = flatten(mod.decodeRun([E(1, 'node.started', 'agent', { node_type: 'agent' }), E(2, 'llm.start', 'agent', { model: 'm' }),
    E(3, 'tool.gated', 'agent', { tool: 'crm_lookup', verdict: 'allow', reason: '只读' })])).find((s) => s.kind === 'llm')
  check('……门控一来，前面那次模型调用收成完成', thinking?.status === 'done', thinking?.status)

  // 拦截（agent 节点）：warn 级，接着是 human.requested，交给人批
  const escEvents = [
    E(1, 'node.started', 'agent', { node_type: 'agent', label: '改档案' }),
    E(2, 'tool.gated', 'agent', { tool: 'mcp:demo/search', verdict: 'escalate', reason: '参数里带了删除条件', model: 'tiny-1', duration_ms: 910 }),
    E(3, 'human.requested', 'agent', { mode: 'approve', tool: 'mcp:demo/search', args: {}, title: 'Agent 请求调用工具 mcp:demo/search', trust_key: 'mcp:demo/search' }),
    E(4, 'run.interrupted', 'agent', { payload: { kind: 'tool_approval', node_id: 'agent', tool: 'mcp:demo/search', trust_key: 'mcp:demo/search' } }),
  ]
  const escRun = mod.decodeRun(escEvents)
  const esc = gated(escRun)[0]
  check('拦截：说「门控拦截 <工具>，转交人工审批：<理由>」', esc?.title === '门控拦截 mcp:demo/search，转交人工审批：参数里带了删除条件', esc?.title)
  check('……warn 级', esc?.level === 'warn', esc?.level)
  check('……挂在所在节点下面，后面跟着等审批的那一行', nodeOf(escRun, 'agent')?.children?.includes(esc)
    && flatten(escRun).some((s) => s.kind === 'human' && s.status === 'waiting'))

  // 协作团队里拦下：成员停不下来，紧接着一条 tool_needs_approval 的 log，调用不执行。
  // 两个成员并行，两条门控都拦下之后 log 才到：按工具名各认各的（按先后取最近的一条会配错）
  const teamRun = mod.decodeRun([
    E(1, 'node.started', 'team', { node_type: 'supervisor', label: '调研团队' }),
    E(2, 'tool.start', 'team', { tool: 'crm_lookup', args: {}, agent: '研究员', call_id: 't1' }),
    E(3, 'tool.start', 'team', { tool: 'weather', args: {}, agent: '分析员', call_id: 't2' }),
    E(4, 'tool.gated', 'team', { tool: 'crm_lookup', verdict: 'escalate', reason: '要批量导出', model: 'tiny-1', duration_ms: 700, agent: '研究员', call_id: 't1' }),
    E(5, 'tool.gated', 'team', { tool: 'weather', verdict: 'escalate', reason: '门控模型未给出判断：超时', model: 'tiny-1', duration_ms: 20000, agent: '分析员', call_id: 't2' }),
    E(6, 'log', 'team', { level: 'warn', code: 'tool_needs_approval', message: '研究员 请求调用 crm_lookup，该调用需要人工审批，协作节点内不执行' }),
    E(7, 'log', 'team', { level: 'warn', code: 'tool_needs_approval', message: '分析员 请求调用 weather，该调用需要人工审批，协作节点内不执行' }),
    E(8, 'tool.end', 'team', { tool: 'crm_lookup', agent: '研究员', call_id: 't1', preview: '没有执行：crm_lookup 这次调用需要人工审批', duration_ms: 710 }),
    E(9, 'tool.end', 'team', { tool: 'weather', agent: '分析员', call_id: 't2', preview: '没有执行：weather 这次调用需要人工审批', duration_ms: 20010 }),
  ])
  const teamRows = gated(teamRun)
  const crm = teamRows.find((s) => s.title.includes('crm_lookup'))
  const wx = teamRows.find((s) => s.title.includes('weather'))
  check('团队里拦截：改说「门控拦截 <工具>，协作团队中不执行：<理由>」', crm?.title === '门控拦截 crm_lookup，协作团队中不执行：要批量导出', crm?.title)
  check('……并行的两条按工具名各认各的：标题和折进来的原话都对得上', wx?.title === '门控拦截 weather，协作团队中不执行：门控模型未给出判断：超时'
    && !!crm?.detail?.includes('研究员 请求调用 crm_lookup') && !!wx.detail?.includes('分析员 请求调用 weather'), `${wx?.title} / ${crm?.detail} / ${wx?.detail}`)
  check('……副标题写是哪个成员调的', crm?.sub === '研究员 调用' && wx?.sub === '分析员 调用', `${crm?.sub} / ${wx?.sub}`)
  check('……那条 log 折进门控那一行，不另起一行', !flatten(teamRun).some((s) => s.code === 'tool_needs_approval')
    && !!crm?.detail?.includes('协作节点内不执行'), flatten(teamRun).filter((s) => s.kind === 'note').map((s) => s.title).join(' | '))
  check('……挂在团队节点下面', !!nodeOf(teamRun, 'team')?.children?.includes(crm))
  // 没有门控的团队审批（ask 档）照旧：log 自己成一行
  const plain = flatten(mod.decodeRun([E(1, 'node.started', 'team', { node_type: 'supervisor' }),
    E(2, 'log', 'team', { level: 'warn', code: 'tool_needs_approval', message: '研究员 请求调用 crm_lookup，该调用需要人工审批，协作节点内不执行' })]))
  check('……没过门控的 tool_needs_approval 照旧自己成一行', plain.some((s) => s.code === 'tool_needs_approval'))
  // 拦截后交给了人（agent 节点），之后同一节点再来的 log 不改写它
  const handed = mod.decodeRun([E(1, 'node.started', 'a', { node_type: 'agent' }),
    E(2, 'tool.gated', 'a', { tool: 'crm_lookup', verdict: 'escalate', reason: '可疑' }),
    E(3, 'human.requested', 'a', { mode: 'approve', tool: 'crm_lookup', title: '要调用 crm_lookup' }),
    E(4, 'log', 'a', { level: 'warn', code: 'tool_needs_approval', message: 'x 想调用 crm_lookup' })])
  check('……已经交给人工的，不会被后来的 log 改口', gated(handed)[0]?.title === '门控拦截 crm_lookup，转交人工审批：可疑', gated(handed)[0]?.title)

  // 审批卡上点了「始终允许」：后端的 human.resolved 带 always（工具审批是平铺的 {tool, approved, note, always}）
  const resolved = (data) => flatten(mod.decodeRun([...escEvents, E(5, 'run.resumed', null, { response: data, actor: '张工' }),
    E(6, 'human.resolved', 'agent', data)]))
    .find((s) => s.kind === 'human')
  const always = resolved({ tool: 'mcp:demo/search', approved: true, note: '', actor: '张工', always: true })
  check('始终允许：审批那一行写「已批准，并设为「始终允许 · 门控把关」」', always?.title === 'Agent 请求调用工具 mcp:demo/search → 张工 已批准，并设为「始终允许 · 门控把关」',
    always?.title)
  const once = resolved({ tool: 'mcp:demo/search', approved: true, note: '', actor: '张工' })
  check('……只是批准的不带这句', once?.title === 'Agent 请求调用工具 mcp:demo/search → 张工 已批准', once?.title)
  const denied = resolved({ tool: 'mcp:demo/search', approved: false, note: '不许导出', actor: '张工' })
  check('……工具审批被驳回写「已驳回」（以前只读 response，平铺的 approved 读不到，一律写批准）',
    denied?.title === 'Agent 请求调用工具 mcp:demo/search → 张工 已驳回' && !!denied.detail?.includes('备注：不许导出'), `${denied?.title} / ${denied?.detail}`)

  // 缺字段：不崩，不出 undefined，不当成没翻译的记录
  let bare = []
  let thrown = null
  try {
    bare = flatten(mod.decodeRun([
      E(1, 'tool.gated', null, {}),
      E(2, 'node.started', 'x', { node_type: 'agent' }),
      E(3, 'tool.gated', 'x', { verdict: 'allow' }),
      E(4, 'tool.gated', 'x', { tool: 'crm_lookup', verdict: 'escalate', reason: null, duration_ms: 'slow' }),
      E(5, 'log', 'x', { level: 'warn', code: 'tool_needs_approval' }),
    ]))
  } catch (e) { thrown = e }
  const rows = bare.filter((s) => s.code === 'tool_gated')
  check('缺字段：不抛异常，每条都成一行', !thrown && rows.length === 3, thrown ? String(thrown.message) : `${rows.length} 行`)
  check('……没有工具名说「一次工具调用」，没有理由就不带冒号', rows[0]?.title === '门控拦截一次工具调用，转交人工审批'
    && rows[1]?.title === '门控通过一次工具调用', rows.map((r) => r.title).join(' | '))
  check('……理由、耗时不是字符串 / 数字时不拼出 undefined / null', !rows.some((r) => /undefined|null|NaN/.test(`${r.title}${r.meta ?? ''}${r.detail ?? ''}`)),
    rows.map((r) => `${r.title} ${r.meta ?? ''}`).join(' | '))
  check('……没有消息的 tool_needs_approval 也折进去', rows[2]?.title === '门控拦截 crm_lookup，协作团队中不执行', rows[2]?.title)
  check('……不是「未识别的事件」', !bare.some((s) => s.title === '未识别的事件'))
})

await section('agent 字段按出处核对（可点击证据第二期）：抽取调用单独成行、两个新警告码说人话', async () => {
  const E = (seq, type, node_id, data = {}) => ({ seq, type, node_id, ts: 1790000000 + seq, data })
  // 两条警告的原话：和 backend/app/engine/nodes/llm.py 的 _field_warnings 同一个格式。先核对后端源码还是这么说的
  const MISMATCH = '有 1 个字段与查询快照不一致，已按快照取值：order_cnt 模型给出 1240，快照为 1234'
  const UNVERIFIED = '有 2 个字段无法核对出处，记为空值（未以 0 代替）：new_users（Q1 没有列「new_users」）；refund_cnt（Q2 的查询快照无法读取）'
  const FAILED = '结构化抽取失败：模型调用超时。3 个字段都记为空值（未以 0 代替）'
  // 抽取调用成了、但交回来的不是对象：同一句式，前面没有「结构化抽取失败：」
  const NOT_OBJECT = '抽取结果格式有误（不是对象）。2 个字段都记为空值（未以 0 代替）'
  const llmPy = readFileSync(`${root}backend/app/engine/nodes/llm.py`, 'utf8')
  check('后端发字段核对警告的格式还是夹具里这一种', llmPy.includes('"agent_field_mismatch"')
    && llmPy.includes('个字段与查询快照不一致，已按快照取值：{detail}') && llmPy.includes('模型给出 {_shown(e.get(\'model_value\'))}，快照为 ')
    && llmPy.includes('个字段无法核对出处，记为空值（未以 0 代替）：{detail}') && llmPy.includes('个字段都记为空值（未以 0 代替）')
    && llmPy.includes('purpose="cite_fields"') && llmPy.includes('"抽取结果格式有误（不是对象）"') && llmPy.includes('结构化抽取失败：{'),
    '改了 llm.py 的说法就同步改 decode.ts 的 explainLog 和这里的夹具')
  const events = [
    E(1, 'run.started', null, { total: 2 }),
    E(2, 'node.started', 'fetch', { node_type: 'agent', label: '查数' }),
    E(3, 'llm.start', 'fetch', { model: 'm1' }),
    E(4, 'tool.start', 'fetch', { tool: 'db_query__shop', call_id: 'c1', args: { sql: 'SELECT SUM(amount) AS gmv FROM orders' } }),
    // 新运行查库成功时 tool.end 多一个 query_artifact：照常解码成那一次查询，不另起行
    E(5, 'tool.end', 'fetch', { tool: 'db_query__shop', call_id: 'c1', duration_ms: 40, artifact: 'tool-snap', query_artifact: 'query-snap',
      preview: '{"columns":["gmv"],"rows":[[45678.5]]}' }),
    E(6, 'llm.end', 'fetch', { model: 'm1', duration_ms: 900 }),
    E(7, 'llm.start', 'fetch', { model: 'm1', structured: true, purpose: 'cite_fields', message_count: 5 }),
    E(8, 'llm.end', 'fetch', { agent: '查数', model: 'm1', purpose: 'cite_fields', duration_ms: 1200, input_tokens: 900, output_tokens: 80 }),
    E(9, 'log', 'fetch', { level: 'warn', code: 'agent_field_mismatch', message: MISMATCH, fields: ['order_cnt'] }),
    E(10, 'log', 'fetch', { level: 'warn', code: 'agent_field_unverified', message: UNVERIFIED, fields: ['new_users', 'refund_cnt'] }),
    E(11, 'node.finished', 'fetch', { duration_ms: 2400 }),
  ]
  const all = flatten(mod.decodeRun(events))
  check('新事件都有翻译', !all.some((s) => s.title === '未识别的事件'))
  const llms = all.filter((s) => s.kind === 'llm')
  check('循环里那次照旧叫「思考并作答」，抽取那次单独起名「按出处抽取字段」', llms.map((s) => s.title).join('|') === '思考并作答|按出处抽取字段',
    llms.map((s) => s.title).join('|'))
  check('抽取那次跑完了、带耗时', llms[1]?.status === 'done' && llms[1]?.meta === '1.2 s', `${llms[1]?.status} ${llms[1]?.meta}`)
  const q = all.filter((s) => s.kind === 'query')
  check('tool.end 多了 query_artifact：照常是一次查询，工件仍是 tool 快照', q.length === 1 && q[0].status === 'done' && q[0].artifact === 'tool-snap',
    JSON.stringify(q.map((s) => [s.status, s.artifact])))
  const mm = all.find((s) => s.code === 'agent_field_mismatch')
  check('agent_field_mismatch 说人话：几个字段不一致、已按快照取值', mm?.title === '有 1 个字段与查询快照不一致，已按快照取值', mm?.title)
  check('……点名的字段和两边的值放副标题，原话留在展开区', mm?.sub === 'order_cnt 模型给出 1240，快照为 1234' && mm?.detail === MISMATCH,
    `${mm?.sub} / ${mm?.detail}`)
  check('……是提醒（warn），并说下游用的是哪个值', mm?.level === 'warn' && !!mm?.next?.includes('快照'), `${mm?.level} ${mm?.next}`)
  const un = all.find((s) => s.code === 'agent_field_unverified')
  check('agent_field_unverified 说人话：几个字段无法核对、记为空、未以 0 代替', un?.title === '有 2 个字段无法核对出处，记为空值（未以 0 代替）', un?.title)
  check('……原因放副标题', un?.sub === 'new_users（Q1 没有列「new_users」）；refund_cnt（Q2 的查询快照无法读取）' && un?.level === 'warn', un?.sub)
  check('……两条警告都挂在 agent 节点下面', all.find((s) => s.nodeId === 'fetch' && s.kind === 'node')?.children?.filter((c) => c.code?.startsWith('agent_field')).length === 2)

  const failed = flatten(mod.decodeRun([
    E(1, 'node.started', 'fetch', { node_type: 'agent' }),
    E(2, 'llm.start', 'fetch', { model: 'm1', structured: true, purpose: 'cite_fields', message_count: 5 }),
    E(3, 'llm.end', 'fetch', { agent: '查数', model: 'm1', purpose: 'cite_fields', duration_ms: 30000, error: 'TimeoutError: 模型调用超时' }),
    E(4, 'log', 'fetch', { level: 'warn', code: 'agent_field_unverified', message: FAILED, fields: ['a', 'b', 'c'] }),
    E(5, 'node.finished', 'fetch', { duration_ms: 31000 }),
  ]))
  const ex = failed.find((s) => s.kind === 'llm')
  check('抽取调用失败：那一行说「失败」，原因放展开区，是提醒不是整个节点失败', ex?.title === '按出处抽取字段失败' && ex?.level === 'warn'
    && !!ex?.detail?.includes('模型调用超时'), `${ex?.title} ${ex?.level} ${ex?.detail}`)
  // status 是 failed 的话，这一行和「执行」那一栏的标头都画成红的失败，可节点、运行都成功了
  check('……这一行是做完了的提醒（done + warn），不是 failed', ex?.status === 'done' && ex?.level === 'warn', `${ex?.status} ${ex?.level}`)
  check('……节点本身照样成功', failed.find((s) => s.kind === 'node')?.status === 'done', failed.find((s) => s.kind === 'node')?.status)
  const fu = failed.find((s) => s.code === 'agent_field_unverified')
  check('……接着的警告说「抽取失败，N 个字段记为空值」', fu?.title === '按出处抽取字段失败，3 个字段记为空值（未以 0 代替）'
    && fu?.sub === '模型调用超时', `${fu?.title} / ${fu?.sub}`)
  const notObject = flatten(mod.decodeRun([
    E(1, 'node.started', 'fetch', { node_type: 'agent' }),
    E(2, 'log', 'fetch', { level: 'warn', code: 'agent_field_unverified', message: NOT_OBJECT, fields: ['a', 'b'] }),
    E(3, 'node.finished', 'fetch', { duration_ms: 10 }),
  ])).find((s) => s.code === 'agent_field_unverified')
  check('「抽取结果格式有误（不是对象）」：同样说「失败，N 个字段记为空值」，原因放副标题，不说「出处不存在」',
    notObject?.title === '按出处抽取字段失败，2 个字段记为空值（未以 0 代替）' && notObject?.sub === '抽取结果格式有误（不是对象）'
    && !notObject?.next?.includes('出处在查询结果中不存在'), `${notObject?.title} / ${notObject?.sub} / ${notObject?.next}`)
  // 两次抽取都被截断（2026-10 一次运行：推理模型关不掉思考，4096 的额度在推理阶段就用完了）：单独的 code，
  // 说清是额度问题、给出能做的下一步，不说「稍后重试」「去设置测试该模型」（llm.py TRUNCATED_EXTRACT、TRUNCATED_NEXT）
  const TRUNCATED = '结构化抽取没有完成：模型的输出额度在推理阶段就用完了（最后一次的额度为 32768 token），结构化结论没能写出来。'
    + '5 个字段都记为空值（未以 0 代替）。可以为该 Agent 换用非推理模型（抽取沿用 Agent 的模型），或者调大该 Agent 的「最大输出 token」后重新运行'
  check('后端发截断警告的说法还是夹具里这一种', llmPy.includes('"agent_field_truncated"')
    && llmPy.includes('结构化抽取没有完成：模型的输出额度在推理阶段就用完了（最后一次的额度为 {budget} token），"')
    && llmPy.includes('"结构化结论没能写出来"') && llmPy.includes('可以为该 Agent 换用非推理模型（抽取沿用 Agent 的模型），或者调大该 Agent 的"')
    && llmPy.includes('已把额度加大到 {bigger} token 重试一次'), '改了 llm.py 的说法就同步改 decode.ts 的 explainLog 和这里的夹具')
  const FIELDS5 = ['members', 'stores', 'coupons', 'redeemed', 'rate']
  const cut = flatten(mod.decodeRun([
    E(1, 'node.started', 'fetch', { node_type: 'agent', label: '会员专项' }),
    E(2, 'llm.start', 'fetch', { model: 'm1', structured: true, purpose: 'cite_fields', message_count: 5, max_tokens: 16384 }),
    E(3, 'llm.end', 'fetch', { agent: '会员专项', model: 'm1', purpose: 'cite_fields', duration_ms: 40000, output_tokens: 16384,
      error: '输出被截断：16384 token 的输出额度用完了，结构化结论没有写完，已把额度加大到 32768 token 重试一次' }),
    E(4, 'llm.start', 'fetch', { model: 'm1', structured: true, purpose: 'cite_fields', message_count: 5, max_tokens: 32768 }),
    E(5, 'llm.end', 'fetch', { agent: '会员专项', model: 'm1', purpose: 'cite_fields', duration_ms: 80000, output_tokens: 32768,
      error: '输出被截断：32768 token 的输出额度用完了，结构化结论没有写完' }),
    E(6, 'log', 'fetch', { level: 'warn', code: 'agent_field_truncated', message: TRUNCATED, fields: FIELDS5, max_tokens: 32768 }),
    E(7, 'node.finished', 'fetch', { duration_ms: 125000 }),
  ]))
  const tr = cut.find((s) => s.code === 'agent_field_truncated')
  check('agent_field_truncated：标题说额度在推理阶段用完、几个字段记为空值', tr?.title === '模型的输出额度在推理阶段用完，5 个字段记为空值（未以 0 代替）', tr?.title)
  check('……副标题说抽取时输出被截断、重试到的额度（读 max_tokens 字段）',
    tr?.sub === '按出处抽取字段时输出被截断（已加大额度重试，最后一次为 32,768 token），结构化结论没能写出来', tr?.sub)
  check('……下一步：换非推理模型或调大最大输出 token；不说稍后重试，不叫人去测试模型',
    !!tr?.next?.includes('非推理模型') && !!tr?.next?.includes('「最大输出 token」') && !tr?.next?.includes('稍后重试')
    && !tr?.next?.includes('测试') && !!tr?.next?.includes('不得据此断言没有数据'), tr?.next)
  check('……入口打开该 Agent 的设置、落到最大输出 token；是提醒（warn），原话留在展开区',
    tr?.fix === 'canvas' && tr?.fixField === 'max_tokens' && tr?.level === 'warn' && tr?.detail === TRUNCATED,
    `${tr?.fix} ${tr?.fixField} ${tr?.level}`)
  const tries = cut.filter((s) => s.kind === 'llm')
  check('……两次抽取各成一行，第一次的展开区写明已加大额度重试', tries.length === 2
    && !!tries[0].detail?.includes('已把额度加大到 32768 token 重试一次') && !!tries[1].detail?.includes('结构化结论没有写完'),
    tries.map((s) => s.detail).join(' | '))
  check('……节点本身照样成功', cut.find((s) => s.kind === 'node')?.status === 'done')
  const bareCut = flatten(mod.decodeRun([E(1, 'node.started', 'x', { node_type: 'agent' }),
    E(2, 'log', 'x', { level: 'warn', code: 'agent_field_truncated', message: '结构化抽取没有完成。3 个字段都记为空值（未以 0 代替）' })]))
    .find((s) => s.code === 'agent_field_truncated')
  check('……缺 fields、max_tokens 字段时字段数从原话里认，不出 undefined',
    bareCut?.title === '模型的输出额度在推理阶段用完，3 个字段记为空值（未以 0 代替）'
    && bareCut?.sub === '按出处抽取字段时输出被截断，结构化结论没能写出来', `${bareCut?.title} / ${bareCut?.sub}`)
  const node = mod.decodeRun([E(1, 'node.started', 'fetch', { node_type: 'agent' }), E(2, 'node.finished', 'fetch', { duration_ms: 1 })])
  check('对照：普通 agent 节点不多出任何行', flatten(node).filter((s) => s.kind !== 'node').length === 0)
  const odd = flatten(mod.decodeRun([E(1, 'node.started', 'x', { node_type: 'agent' }),
    E(2, 'log', 'x', { level: 'warn', code: 'agent_field_mismatch', message: '字段不一致' }),
    E(3, 'log', 'x', { level: 'warn', code: 'agent_field_unverified' })]))
  check('别的说法（缺字段）：标题照样说人话，不出 undefined、不瞎填数', odd.find((s) => s.code === 'agent_field_mismatch')?.title === '有字段与查询快照不一致，已按快照取值'
    && !odd.some((s) => /undefined|NaN/.test(`${s.title}${s.sub ?? ''}`)), odd.map((s) => s.title).join(' | '))
})

await section('口径卡指标不完整（数据目录阶段 0）：截断的查询结果整组拿来算的指标，警告说人话并指到 SQL', async () => {
  const E = (seq, type, node_id, data = {}) => ({ seq, type, node_id, ts: 1790000000 + seq, data })
  // 日志带着结构化字段（metric_name、reason），界面读字段，不从原话里解析。先核对后端还发这两个字段
  const REASON = '基于被截断的查询结果计算（只取回了前 1000 行），结果不完整'
  const INCOMPLETE = `指标「入园人次」${REASON}`
  const metricsPy = readFileSync(`${root}backend/app/engine/nodes/metrics.py`, 'utf8')
  check('后端发指标不完整警告时带着 metric_name、reason 字段', metricsPy.includes('code="metric_incomplete"')
    && metricsPy.includes('metric_name=m["name"]') && metricsPy.includes('reason=m["incomplete_reason"]'),
  '改了 metrics.py 的字段就同步改 decode.ts 的 explainLog 和这里的夹具')
  const all = flatten(mod.decodeRun([
    E(1, 'node.started', 'card', { node_type: 'metrics' }),
    E(2, 'log', 'card', { level: 'warn', code: 'metric_incomplete', metric: 'visits', metric_name: '入园人次', reason: REASON,
      rows: [1000], message: INCOMPLETE }),
    E(3, 'node.finished', 'card', { duration_ms: 3 }),
  ]))
  const inc = all.find((s) => s.code === 'metric_incomplete')
  check('metric_incomplete 说人话：标题点名哪个指标不完整', inc?.title === '指标「入园人次」结果不完整', inc?.title)
  check('……原因放副标题，原话留在展开区', inc?.sub === '基于被截断的查询结果计算（只取回了前 1000 行）' && inc?.detail === INCOMPLETE,
    `${inc?.sub} / ${inc?.detail}`)
  check('……是提醒（warn），下一步指到 SQL，节点本身照样成功', inc?.level === 'warn' && !!inc?.next?.includes('SQL')
    && all.find((s) => s.kind === 'node')?.status === 'done', `${inc?.level} ${inc?.next}`)
  const bare = flatten(mod.decodeRun([
    E(1, 'node.started', 'card', { node_type: 'metrics' }),
    E(2, 'log', 'card', { level: 'warn', code: 'metric_incomplete', metric: 'visits', message: INCOMPLETE }),
  ])).find((s) => s.code === 'metric_incomplete')
  check('老日志没有结构化字段：只给不点名的标题，不解析原话', bare?.title === '指标结果不完整' && !bare.sub && bare.detail === INCOMPLETE,
    bare?.title)
})

await section('结论句裁判（可点击证据第四期）：裁判调用单独成行、几条警告说人话、封存后按需裁判有记录', async () => {
  const E = (seq, type, node_id, data = {}) => ({ seq, type, node_id, ts: 1790000000 + seq, data })
  // 原话和后端同一个格式：engine/judge.py 的 run_request、engine/nodes/report.py 的改写一次。先核对后端源码还是这么说的
  const judgePy = readFileSync(`${root}backend/app/engine/judge.py`, 'utf8')
  const reportPy = readFileSync(`${root}backend/app/engine/nodes/report.py`, 'utf8')
  check('后端发裁判事件的格式还是夹具里这一种', judgePy.includes('purpose="judge"') && judgePy.includes('code="judge_limit"')
    && judgePy.includes('结论句裁判已达上限（{labels}）：{n} 句未裁判，已裁判的结果保留')
    && judgePy.includes('"结论句裁判未完成：" + "；".join(failed)') && judgePy.includes('code="judge_unpriced"')
    && judgePy.includes('不在价格目录中，无法按 token 数估算金额：') && reportPy.includes('code="judge_same_model"')
    && reportPy.includes('裁判模型与写作模型相同（「{model_id}」），难以发现写作模型自身的错误') && reportPy.includes('code="report_rewrite"') && reportPy.includes('句结论证据不支持（')
    && reportPy.includes('已退回写作模型仅修改这几句') && reportPy.includes('改写稿新增了 {len(fresh)} 处原稿没有的问题（')
    && reportPy.includes('未予采用，') && reportPy.includes('已保留原稿及原有判定'),
    '改了 judge.py / report.py 的说法就同步改 decode.ts 的 explainLog 和这里的夹具')
  const LIMIT = '结论句裁判已达上限（这份报告的裁判金额上限 $0.05）：3 句未裁判，已裁判的结果保留'
  const FAILED = '结论句裁判未完成：有 2 句结论未裁判：裁判调用失败（ConnectError: 连不上）'
  const UNPRICED = '模型「jm-x」不在价格目录中，无法按 token 数估算金额：金额上限（每份报告、每次点击、每日）对其无效，费用只受句数和时长上限约束'
  const SAME = '裁判模型与写作模型相同（「wm-1」），难以发现写作模型自身的错误。请在「设置 → 偏好设置 → 证据裁判」中选择另一个模型，或在节点的「结论句裁判」中指定裁判模型'
  const REWRITE = '裁判认为 2 句结论证据不支持（「增长主要来自新客」「退款多数是部分退款」），已退回写作模型仅修改这几句'
  const REJECTED = '改写稿新增了 1 处原稿没有的问题（「12」），未予采用，已保留原稿及原有判定'
  const events = [
    E(1, 'run.started', null, { total: 1 }),
    E(2, 'node.started', 'write', { node_type: 'report', label: '写周报' }),
    E(3, 'llm.start', 'write', { model: 'wm-1' }),
    E(4, 'llm.end', 'write', { model: 'wm-1', duration_ms: 900 }),
    E(5, 'llm.start', 'write', { model: 'jm-x', structured: true, purpose: 'judge', message_count: 2, units: 4 }),
    E(6, 'llm.end', 'write', { model: 'jm-x', purpose: 'judge', duration_ms: 2100, input_tokens: 900, output_tokens: 300, units: 4 }),
    E(7, 'log', 'write', { level: 'warn', code: 'judge_unpriced', message: UNPRICED }),
    E(8, 'log', 'write', { level: 'warn', code: 'judge_same_model', message: SAME }),
    E(9, 'log', 'write', { level: 'warn', code: 'judge_limit', limits: ['max_cost_usd'], unjudged: 3, message: LIMIT }),
    E(10, 'log', 'write', { level: 'warn', code: 'judge_failed', message: FAILED }),
    E(11, 'log', 'write', { level: 'warn', code: 'report_rewrite', message: REWRITE }),
    E(12, 'log', 'write', { level: 'warn', code: 'report_rewrite_rejected', message: REJECTED }),
    E(13, 'report.checked', 'write', { doc_artifact: 'doc-1', stats: { numbers: 3, numbers_cited: 3, uncited_numbers: 0, unresolved: 0 },
      violations: [], repairs: 0, claims: 'judge',
      judge: { mode: 'inline', counts: { supported: 1, partial: 1, unsupported: 1, not_a_claim: 1, unjudged: 1 } } }),
    E(14, 'node.finished', 'write', { duration_ms: 4000 }),
    E(15, 'run.finished', null, { status: 'succeeded' }),
    // 探索运行跑完之后有人点开结论句请模型判断：判定追加在封存之后
    E(16, 'evidence.judged', 'write', { report: 'write', doc_artifact: 'doc-1', units: ['u2'], model: 'jm-x',
      verdicts: { u2: { status: 'unsupported', rationale: '看不出', judge: 'jm-x', post_seal: true } }, limits_hit: [], gaps: [],
      post_seal: true }),
  ]
  const all = flatten(mod.decodeRun(events))
  check('新事件都有翻译', !all.some((s) => s.title === '未识别的事件'), all.filter((s) => s.title === '未识别的事件').map((s) => s.raw?.type).join(','))
  const llms = all.filter((s) => s.kind === 'llm')
  check('写作照旧叫「思考并作答」，裁判那次单独起名、说判几句', llms.map((s) => s.title).join('|') === '思考并作答|请裁判模型判断 4 句结论',
    llms.map((s) => s.title).join('|'))
  check('……裁判那次跑完了、带耗时和模型', llms[1]?.status === 'done' && llms[1]?.meta === '2.1 s' && llms[1]?.code === 'judge'
    && llms[1]?.detail === '模型：jm-x', `${llms[1]?.status} ${llms[1]?.meta} ${llms[1]?.code} ${llms[1]?.detail}`)
  const by = (code) => all.find((s) => s.code === code)
  check('judge_limit：几句未裁判、哪个上限、怎么调', by('judge_limit')?.title === '结论句裁判已达上限，3 句未裁判（已裁判的结果保留）'
    && by('judge_limit')?.sub === '这份报告的裁判金额上限 $0.05' && !!by('judge_limit')?.next?.includes('不限'),
    `${by('judge_limit')?.title} / ${by('judge_limit')?.sub} / ${by('judge_limit')?.next}`)
  check('judge_failed：未完成、原因放副标题', by('judge_failed')?.title === '结论句裁判未完成，未判定的结论句记为未裁判'
    && by('judge_failed')?.sub === '有 2 句结论未裁判：裁判调用失败（ConnectError: 连不上）', `${by('judge_failed')?.title} / ${by('judge_failed')?.sub}`)
  check('judge_unpriced：点名模型、金额上限无效', by('judge_unpriced')?.title === '裁判模型「jm-x」无法估算金额，金额上限对其无效',
    by('judge_unpriced')?.title)
  check('judge_same_model：审查缺乏独立性、怎么改', by('judge_same_model')?.title === '裁判模型与写作模型同为「wm-1」，审查缺乏独立性'
    && !!by('judge_same_model')?.next?.includes('证据裁判'), `${by('judge_same_model')?.title} / ${by('judge_same_model')?.next}`)
  check('report_rewrite：几句缺乏证据支持、退回改写，句子放副标题', by('report_rewrite')?.title === '裁判认为 2 句结论缺乏证据支持，已退回写作模型仅修改这几句'
    && by('report_rewrite')?.sub === '「增长主要来自新客」「退款多数是部分退款」', `${by('report_rewrite')?.title} / ${by('report_rewrite')?.sub}`)
  check('report_rewrite_rejected：新增几处问题、未予采用', by('report_rewrite_rejected')?.title === '改写稿新增了 1 处原稿没有的问题，未予采用'
    && by('report_rewrite_rejected')?.sub === '「12」' && by('report_rewrite_rejected')?.next === '已保留原稿及原有判定',
    `${by('report_rewrite_rejected')?.title} / ${by('report_rewrite_rejected')?.sub}`)
  check('几条警告都是提醒（warn），原话留在展开区', ['judge_limit', 'judge_failed', 'judge_unpriced', 'judge_same_model', 'report_rewrite',
    'report_rewrite_rejected'].every((c) => by(c)?.level === 'warn' && by(c)?.detail))
  const rc = all.find((s) => s.code === 'report_checked')
  check('报告核对那一行也数结论句，和出具横幅同一种说法（不是结论句的不算）',
    rc?.title === '核对报告：3 个数字都有出处 · 结论 4 句（有依据 1 · 部分有依据 1 · 证据不支持 1 · 未裁判 1）', rc?.title)
  const ej = by('evidence_judged')
  check('封存后按需裁判：单独一行，写明封存后追加、模型判断非确定', ej?.title === '封存后按需裁判了 1 句结论'
    && ej?.sub === '封存后追加 · 模型判断，非确定' && ej?.level === 'info', `${ej?.title} / ${ej?.sub} / ${ej?.level}`)

  const failed = flatten(mod.decodeRun([
    E(1, 'node.started', 'write', { node_type: 'report' }),
    E(2, 'llm.start', 'write', { model: 'jm-x', structured: true, purpose: 'judge', message_count: 2, units: 2 }),
    E(3, 'llm.end', 'write', { model: 'jm-x', purpose: 'judge', duration_ms: 30000, units: 2, error: '已达上限（裁判时长上限 30 秒），这句未裁判' }),
    E(4, 'node.finished', 'write', { duration_ms: 31000 }),
    E(5, 'evidence.judged', 'write', { verdicts: { u4: { status: 'unjudged', reason: 'max_cost_usd' } }, limits_hit: ['max_cost_usd'] }),
  ]))
  const jx = failed.find((s) => s.kind === 'llm')
  check('裁判调用未能执行：那一行说「未能执行」，是提醒不是失败，原因放展开区', jx?.title === '结论句裁判未能执行' && jx?.status === 'done'
    && jx?.level === 'warn' && !!jx?.detail?.includes('裁判时长上限'), `${jx?.title} ${jx?.status} ${jx?.level}`)
  check('……报告节点照样成功', failed.find((s) => s.kind === 'node')?.status === 'done')
  const late = failed.find((s) => s.code === 'evidence_judged')
  check('按需裁判触顶：写明部分因达到上限未裁判（提醒）', late?.title === '封存后按需裁判了 0 句结论，部分因达到上限未裁判' && late?.level === 'warn',
    `${late?.title} ${late?.level}`)
  const odd = flatten(mod.decodeRun([E(1, 'node.started', 'w', { node_type: 'report' }),
    E(2, 'log', 'w', { level: 'warn', code: 'judge_limit', message: '到上限了' }),
    E(3, 'log', 'w', { level: 'warn', code: 'report_rewrite' }),
    E(4, 'report.checked', 'w', { stats: { numbers: 1, numbers_cited: 1 }, judge: { counts: 'x' } })]))
  check('别的说法（缺字段）：标题照样说人话，不出 undefined、不瞎填数', odd.find((s) => s.code === 'judge_limit')?.title === '结论句裁判已达上限（已裁判的结果保留）'
    && !odd.some((s) => /undefined|NaN/.test(`${s.title}${s.sub ?? ''}`)), odd.map((s) => s.title).join(' | '))
  check('……裁判摘要认不出时报告核对那一行只数数字', odd.find((s) => s.code === 'report_checked')?.title === '核对报告：1 个数字都有出处',
    odd.find((s) => s.code === 'report_checked')?.title)
})

await section('口径账本：封存后比对已发布口径（ledger.matched）', async () => {
  // next-round 02 做法 2：探索运行封存之后，服务端在后台比对已发布口径，追加一条 ledger.matched（只有各种状态几条）
  const E = (seq, type, node_id, data = {}) => ({ seq, type, node_id, ts: 1790000000 + seq, data })
  const run = (data) => flatten(mod.decodeRun([
    E(1, 'run.started', null, {}), E(2, 'node.started', 'q', { node_type: 'tool' }), E(3, 'node.finished', 'q', {}),
    E(4, 'run.finished', null, { status: 'succeeded' }),
    E(5, 'ledger.matched', null, { artifact: 'cm-1', manifest_hash: 'h', algo: '1', post_seal: true, ...data }),
  ]))
  const all = run({ counts: { proven: 1, composed: 0, related: 2, none: 0, skipped: 0 }, trigger: 'sealed' })
  check('认得它：不是「未识别的事件」', !all.some((s) => s.title === '未识别的事件'))
  const lm = all.find((s) => s.code === 'ledger_matched')
  check('一行：几条等价已证明、几条有相关口径，写明封存后比对、不在封存范围内',
    lm?.title === '比对已发布口径：1 条查询等价已证明，2 条有相关口径' && lm?.sub === '封存后比对，不在封存范围内'
    && lm?.level === 'info' && lm?.kind === 'note', `${lm?.title} / ${lm?.sub} / ${lm?.level}`)
  const late = run({ counts: { proven: 0, related: 0, none: 1, skipped: 1 }, trigger: 'backfill' }).find((s) => s.code === 'ledger_matched')
  check('事后补比的写明按现在的口径回看；没比完的是提醒', late?.title === '比对已发布口径：1 条没有相关的已发布口径，1 条没有比完'
    && late?.sub === '封存后比对，不在封存范围内 · 按现在的口径回看' && late?.level === 'warn', `${late?.title} / ${late?.sub} / ${late?.level}`)
  const certified = run({ counts: { proven: 1 }, trigger: 'certified' }).find((s) => s.code === 'ledger_matched')
  check('固化出的口径发布后补比的写明按之后发布的口径回看', certified?.sub === '封存后比对，不在封存范围内 · 按之后发布的口径回看',
    certified?.sub)
  const odd = run({ counts: 'x' }).find((s) => s.code === 'ledger_matched')
  check('缺字段：只说比对过，不出 undefined、不瞎填数', odd?.title === '比对已发布口径' && !/undefined|NaN/.test(`${odd?.title}${odd?.sub}`),
    odd?.title)
  const phase = [E(4, 'run.finished', null, {}), E(5, 'ledger.matched', null, {})]
    .reduce((s, ev) => mod.nextPhase(s, ev), { phase: 'running', awaiting: false })
  check('不改相位：运行照旧是结束了', phase.phase === 'succeeded', phase.phase)
})

await section('口径召回：封存后的核对结论与依据有变（ledger.recalled、ledger.basis_changed）', async () => {
  // next-round 02 做法 7：口径发布新版本后召回运行在同一份数据上重算两版，数没变的在原答案上记「已按 vN 核对，不影响」，
  // 召回运行自己记结论；目录事实被驳回或修改、表结构变了的在答案上记「依据有变」。都在封存之后，事件里不带数
  const E = (seq, type, node_id, data = {}) => ({ seq, type, node_id, ts: 1790000000 + seq, data })
  const base = [E(1, 'run.started', null, {}), E(2, 'node.started', 'q', { node_type: 'tool' }), E(3, 'node.finished', 'q', {}),
    E(4, 'run.finished', null, { status: 'succeeded' })]
  const run = (...more) => flatten(mod.decodeRun([...base, ...more]))
  const answer = run(E(5, 'ledger.recalled', null, { role: 'answer', changed: false, recall_run: 'rc-1', workflow_id: 'wf-week',
    from_version: 1, to_version: 2, post_seal: true }))
  check('认得它们：不是「未识别的事件」', !answer.some((s) => s.title === '未识别的事件'))
  const ok = answer.find((s) => s.code === 'ledger_recalled')
  check('原答案上：已按 v2 核对，不影响；写明封存后追加', ok?.title === '已按 v2 核对，不影响'
    && ok?.sub === '封存后追加，不在封存范围内' && ok?.level === 'info' && ok?.kind === 'note', `${ok?.title} / ${ok?.sub}`)
  const changed = run(E(5, 'ledger.recalled', null, { role: 'recall', outcome: 'changed', changed: true, from_version: 1,
    to_version: 2, answers: 2, tasks: ['t1', 't2'], metrics: [{ k: 0, name: '销售额', changed: true }] }))
    .find((s) => s.code === 'ledger_recalled')
  check('召回运行上：数变了是提醒，写开了几条待办，不带数', changed?.title === '召回核对（v1 → v2）：数变了'
    && changed?.level === 'warn' && changed?.meta === '开了 2 条待办' && !/\d+\.\d/.test(`${changed?.title}${changed?.detail ?? ''}`),
    `${changed?.title} / ${changed?.meta}`)
  const same = run(E(5, 'ledger.recalled', null, { role: 'recall', outcome: 'unchanged', changed: false, from_version: 1, to_version: 2 }))
    .find((s) => s.code === 'ledger_recalled')
  check('……数没变的是一般说明', same?.title === '召回核对（v1 → v2）：数没变' && same?.level === 'info', same?.title)
  const failedRun = run(E(5, 'ledger.recalled', null, { role: 'recall', outcome: 'failed', changed: null, error: '数据源已停用' }))
    .find((s) => s.code === 'ledger_recalled')
  check('……重算失败照实写原因', failedRun?.title === '召回重算失败' && failedRun?.detail === '数据源已停用' && failedRun?.level === 'warn',
    `${failedRun?.title} / ${failedRun?.detail}`)
  const probe = run(E(5, 'ledger.recalled', null, { role: 'recall', probe: true, outcome: 'failed', error: 'no such column' }))
    .find((s) => s.code === 'ledger_recalled')
  check('表结构变了之后的口径试算：没有算出来', probe?.title === '表结构变了之后试算口径：没有算出来', probe?.title)
  const basis = run(E(5, 'ledger.basis_changed', null, { kind: 'catalog', source: 'shop', table: 'orders',
    items: [{ path: 'columns.amount.measure', label: '列 amount 的度量类型', change: 'changed' }], new_errors: 1 }))
    .find((s) => s.code === 'ledger_basis_changed')
  check('目录事实被改：依据有变，列出哪几项、新出现几条错误级问题', basis?.title === '依据有变：数据目录里影响数字的项被驳回或修改'
    && basis?.meta === 'shop · orders' && basis?.detail === '列 amount 的度量类型\n按新目录检查新出现 1 条错误级问题'
    && basis?.level === 'warn', `${basis?.title} / ${basis?.detail}`)
  const structure = run(E(5, 'ledger.basis_changed', null, { kind: 'structure', table: 'orders', column: 'amount', items: [] }))
    .find((s) => s.code === 'ledger_basis_changed')
  check('表结构变了：依据的表结构已变', structure?.title === '依据的表结构已变' && structure?.level === 'warn', structure?.title)
  const odd = run(E(5, 'ledger.recalled', null, {}), E(6, 'ledger.basis_changed', null, { kind: 'x', items: 'y' }))
  check('缺字段：不出 undefined、不瞎填数，认不出的结论和种类不猜', !odd.some((s) => /undefined|NaN/.test(`${s.title}${s.sub ?? ''}${s.detail ?? ''}${s.meta ?? ''}`))
    && odd.some((s) => s.title === '口径召回核对') && odd.some((s) => s.title === '答案的依据有变'),
    odd.map((s) => s.title).join(' | '))
  const phase = [E(4, 'run.finished', null, {}), E(5, 'ledger.recalled', null, {}), E(6, 'ledger.basis_changed', null, {})]
    .reduce((s, ev) => mod.nextPhase(s, ev), { phase: 'running', awaiting: false })
  check('不改相位：运行照旧是结束了', phase.phase === 'succeeded', phase.phase)
})

await section('口径账本：问数据的助手选用已发布口径（use_caliber 操作）', async () => {
  // next-round 02 做法 4：取数节点和口径卡由服务端按那一版生成（ledger/compose.py），不另转 add_node。过程里一行说清选用了
  // 哪一版、生成了几个节点；拒收的照实写原因（同样交回了模型去改）
  const ok = { op: 'use_caliber', id: 'c1', workflow_id: 'wf-week', version: 3, metrics: ['gmv'], inputs: { start: '2026-09-01' },
    result: { status: 'ok', workflow_id: 'wf-week', workflow_name: '周销售', version: 3, level: 'governed', level_text: '受管',
              card: '周销售卡', card_id: 'c1', fetches: ['c1_fetch'], metrics: [{ id: 'gmv', name: '销售额' }], nodes: 2 } }
  const steps = mod.decodeCopilot([
    { op: 'plan', summary: '选用周销售口径' },
    { op: 'add_node', node: { id: 'start', type: 'input', label: '入口', config: {} } },
    ok,
    { op: 'add_node', node: { id: 'write', type: 'report', label: '写答案', config: {} } },
    { op: 'done', explanation: '按口径回答' },
  ], { context: 'chat' })
  const row = steps.find((s) => s.code === 'use_caliber')
  check('一行：选用了哪一版、级别', row?.title === '选用口径「周销售」 v3（受管）' && row?.kind === 'node' && row?.status === 'done', row?.title)
  check('展开：生成了几个取数节点、哪张口径卡、用到哪几个指标',
    row?.detail === '按这一版生成取数节点 1 个和口径卡「周销售卡」，用到的指标：销售额', row?.detail)
  check('节点数算进「新增几个步骤」（入口、报告各 1，选用的 2）',
    steps.find((s) => s.id.startsWith('cd-'))?.title === '工作流已搭建完成，新增 4 个步骤', steps.find((s) => s.id.startsWith('cd-'))?.title)
  const refused = mod.decodeCopilot([{ op: 'use_caliber', id: 'c2', result: { status: 'refused', reason: '请填写「end」' } }])
    .find((s) => s.code === 'use_caliber_refused')
  check('拒收的：提醒一行，写服务端给的原因', refused?.title === '没有选用口径' && refused?.detail === '请填写「end」'
    && refused?.level === 'warn', `${refused?.title} / ${refused?.detail}`)
  check('没带结果（老服务端、形状不对）：不出行、不报错', mod.decodeCopilot([{ op: 'use_caliber', id: 'c1' }, { op: 'use_caliber', result: 'x' }]).length === 0)
})

await section('术语', async () => {
  // 没起名的节点退到类型名，类型名跟全站同一张表
  const h = mod.decodeRun([{ seq: 1, type: 'node.started', node_id: 'h', ts: 1, data: { node_type: 'human', label: 'h' } }])
  check('人工节点叫「人工审批」', h[0]?.title === '人工审批', h[0]?.title)
})

await section('按后端原文匹配的地方：后端现在的原文、整改前的原文都认得（文案整改第二轮）', async () => {
  // 时间线靠正则从后端日志、报错的原文里取数字、点名和副标题。开发库里的历史运行存的是整改前的文字，
  // 新运行是整改后的文字：两种各喂一遍，翻译结果要一样。
  // 「新」样本和 backend/app 现在的写法一致（下面先核对后端源码里还是这几个片段，免得样本自说自话）；
  // 「旧」样本是整改前（提交 68fb353）的原话，只用来证明旧文字仍然认得，不要跟着后端改
  const src = (f) => readFileSync(`${root}backend/app/${f}`, 'utf8')
  const SOURCE = [
    ['engine/toolcalls.py', '以文本形式输出了工具调用（{snippet[:40]}…），未实际调用工具'],
    ['engine/toolcalls.py', '秒没有返回，已停止等待'],
    ['engine/nodes/multi.py', '，已要求模型重试一次'],
    ['engine/nodes/multi.py', '：已达步数上限，收尾时仍试图调用工具'],
    ['engine/nodes/multi.py', '协作团队用完 {max_rounds} 轮仍未完成：'],
    ['engine/nodes/multi.py', '。一次都没被派到的成员：'],
    ['engine/nodes/multi.py', '。按降档交付：成果取自成员最后的回复，并非调度者认可的结论'],
    ['engine/nodes/multi.py', '。先看成员是否绑定了所需工具'],
    ['engine/nodes/report.py', '处未通过核对（{_named(blocking)}），'],
    ['engine/nodes/report.py', '已要求模型重写（第 {repairs} 次）'],
    ['engine/nodes/report.py', '裁判模型与写作模型相同（「{model_id}」）'],
    ['engine/nodes/report.py', '已退回写作模型仅修改这几句'],
    ['engine/nodes/report.py', '改写稿新增了 {len(fresh)} 处原稿没有的问题（{_named(fresh)}），未予采用'],
    ['engine/nodes/llm.py', '个字段与查询快照不一致，已按快照取值：{detail}'],
    ['engine/nodes/llm.py', '个字段无法核对出处，记为空值（未以 0 代替）：{detail}'],
    ['engine/nodes/llm.py', '个字段都记为空值（未以 0 代替）'],
    ['engine/nodes/llm.py', '结构化抽取失败：{'],
    ['engine/nodes/llm.py', '抽取结果格式有误（不是对象）'],
    ['engine/judge.py', '结论句裁判已达上限（{labels}）：{n} 句未裁判，已裁判的结果保留'],
    ['engine/judge.py', '"结论句裁判未完成："'],
    ['engine/judge.py', '有 {n} 句结论未裁判：裁判调用失败（'],
    ['engine/judge.py', '不在价格目录中，无法按 token 数估算金额：'],
    ['engine/nodes/human.py', '次修复作废：{errors[0]}'],
    ['engine/nodes/human.py', '修复时出现了原文没有的值：'],
    ['tools/datasource.py', 'f"SQL 被拒绝：{e}"'],
    ['tools/datasource.py', 'f"查询失败：'],
    ['engine/nodes/multi.py', 'f"进入子工作流「{workflow.name}」'],
    ['engine/nodes/multi.py', 'f"子工作流「{workflow.name}」完成"'],
  ]
  const gone = SOURCE.filter(([f, frag]) => !src(f).includes(frag))
  check('「新」样本的片段在后端源码里都还在（后端改了说法就同步这里的样本和 decode.ts 的正则）', gone.length === 0,
    gone.map(([f, frag]) => `${f}：${frag}`).join(' ｜ '))

  const E = (seq, type, node_id, data = {}) => ({ seq, type, node_id, ts: 1790000000 + seq, data })
  const logRow = (code, message) => flatten(mod.decodeRun([
    E(1, 'node.started', 'n', { node_type: 'agent', label: '取数' }),
    E(2, 'log', 'n', { level: 'warn', code, message }),
  ])).find((s) => s.code === code)
  // [code, 说明, 后端现在的原文, 整改前的原文, 期望标题, 期望副标题（undefined 表示不比）]
  const CASES = [
    ['tool_markup_leak', '成员把工具调用写成文字、已要求重试',
      '数据查询以文本形式输出了工具调用（<｜DSML｜function_calls>…），未实际调用工具，已要求模型重试一次',
      '数据查询把工具调用写成了文字（<｜DSML｜function_calls>…），没有真正调用工具，已提醒它重试一次',
      '数据查询以文本形式输出了工具调用，未实际执行', '已要求模型重试一次'],
    ['tool_markup_leak', '步数用完后收尾时仍想调用工具',
      '取数员以文本形式输出了工具调用（<tool_call>…），未实际调用工具：已达步数上限，收尾时仍试图调用工具',
      '取数员把工具调用写成了文字（<tool_call>…），没有真正调用工具：步数用完后的收尾轮仍想调用工具',
      '取数员：已达步数上限，收尾时仍试图调用工具', '收尾时的工具调用未执行，结论仅基于此前的查询结果'],
    ['team_exhausted', '协作团队用完轮数、降档交付',
      '协作团队用完 2 轮仍未完成：还缺汇总。一次都没被派到的成员：写手、审校。按降档交付：成果取自成员最后的回复，并非调度者认可的结论',
      '协作团队用完 2 轮仍未完成：还缺汇总。一次都没被派到的成员：写手、审校。按降档交付：成果是成员最后的原话，不是调度者认可的结论',
      '协作团队已用完 2 轮仍未完成 · 按降档交付', '还缺汇总 · 未分派：写手、审校'],
    ['report_repair', '报告没通过核对、要求重写',
      '报告有 3 处未通过核对（「12」、「m:nope」、「3」），已要求模型重写（第 1 次）',
      '报告里有 3 处没通过核对（「12」、「m:nope」、「3」），已要求写作者重写（第 1 次）',
      '报告有 3 处未通过核对，已要求模型按清单重写（第 1 次）', '「12」、「m:nope」、「3」'],
    ['agent_field_mismatch', '字段和查询快照对不上',
      '有 1 个字段与查询快照不一致，已按快照取值：order_cnt 模型给出 1240，快照为 1234',
      '有 1 个字段和查询快照对不上，已按快照取值：order_cnt 模型报 1240，快照是 1234',
      '有 1 个字段与查询快照不一致，已按快照取值', undefined],
    ['agent_field_unverified', '字段核对不了出处',
      '有 2 个字段无法核对出处，记为空值（未以 0 代替）：gmv（引用的查询不存在）；orders（没有写出处）',
      '有 2 个字段核对不了出处，记为空值（没有兜底成 0）：gmv（引用的查询不存在）；orders（没有写出处）',
      '有 2 个字段无法核对出处，记为空值（未以 0 代替）', 'gmv（引用的查询不存在）；orders（没有写出处）'],
    ['agent_field_unverified', '结构化抽取整个失败',
      '结构化抽取失败：模型超时。3 个字段都记为空值（未以 0 代替）',
      '结构化抽取没跑成：模型超时。3 个字段都记为空值（没有兜底成 0）',
      '按出处抽取字段失败，3 个字段记为空值（未以 0 代替）', '模型超时'],
    ['agent_field_unverified', '抽取结果不是对象',
      '抽取结果格式有误（不是对象）。2 个字段都记为空值（未以 0 代替）',
      '抽取结果不是一个对象。2 个字段都记为空值（没有兜底成 0）',
      '按出处抽取字段失败，2 个字段记为空值（未以 0 代替）', undefined],
    ['judge_limit', '结论句裁判到了上限',
      '结论句裁判已达上限（这份报告的裁判金额上限 $0.05）：3 句未裁判，已裁判的结果保留',
      '结论句裁判已到上限（这份报告的裁判金额上限 $0.05）：3 句没判，记为未裁判；已判的保留',
      '结论句裁判已达上限，3 句未裁判（已裁判的结果保留）', '这份报告的裁判金额上限 $0.05'],
    ['judge_failed', '结论句裁判没有完成',
      '结论句裁判未完成：有 2 句结论未裁判：裁判调用失败（超时）',
      '结论句裁判没跑完：有 2 句结论没裁判：裁判调用失败（超时）',
      '结论句裁判未完成，未判定的结论句记为未裁判', undefined],
    ['judge_unpriced', '裁判模型不在价格目录里',
      '模型「jm-x」不在价格目录中，无法按 token 数估算金额：金额上限（每份报告、每次点击、每日）对其无效',
      '模型「jm-x」不在价格目录里，按令牌估不出金额：金额上限（每份报告、每次点击、每日）对它不起作用',
      '裁判模型「jm-x」无法估算金额，金额上限对其无效', undefined],
    ['judge_same_model', '裁判模型和写作模型相同',
      '裁判模型与写作模型相同（「wm-1」），难以发现写作模型自身的错误。请在「设置 → 偏好设置 → 证据裁判」中选择另一个模型',
      '裁判模型和写作模型都是「wm-1」：等于自己审自己，模型写错的地方它多半也看不出来。',
      '裁判模型与写作模型同为「wm-1」，审查缺乏独立性', undefined],
    ['report_rewrite', '证据不支持的句子退回改写',
      '裁判认为 2 句结论证据不支持（「华东最高」、「环比翻倍」），已退回写作模型仅修改这几句',
      '裁判认为 2 句结论证据不支持（「华东最高」、「环比翻倍」），已交回写作者只改这几句',
      '裁判认为 2 句结论缺乏证据支持，已退回写作模型仅修改这几句', '「华东最高」、「环比翻倍」'],
    ['report_rewrite_rejected', '改写稿冒出新问题、不采用',
      '改写稿新增了 1 处原稿没有的问题（「99」），未予采用，已保留原稿及原有判定',
      '改写稿冒出 1 处原稿没有的问题（「99」），没有采用，保留原稿和原来的判定',
      '改写稿新增了 1 处原稿没有的问题，未予采用', '「99」'],
    ['repair_invented', '校验修复编出原文没有的值',
      '第 1 次修复作废：修复时出现了原文没有的值：total_count=0',
      '第 1 次修复作废：修复时出现了原文没有的值：total_count=0',
      '修复已作废：出现了原文中不存在的值（total_count=0）', undefined],
  ]
  for (const [code, what, now, before, title, sub] of CASES) {
    for (const [which, message] of [['后端现在的原文', now], ['整改前的原文', before]]) {
      const row = logRow(code, message)
      check(`${code}：${what}（${which}）`, row?.title === title && (sub === undefined || row?.sub === sub),
        `${row?.title} / ${row?.sub}`)
    }
  }

  // 团队用完轮数的整句：时间线和画布节点卡（canvas/NodeCard 复用这个函数）共用。更早的后端写「还未完成」
  const failNow = '协作团队用完 3 轮仍未完成：调度者未给出理由。一次都没被派到的成员：汇总员。先看成员是否绑定了所需工具，再调大「最多轮数」；也可将「用完轮数时」改为「降档交付」'
  const failBefore = '协作团队用完 3 轮仍未完成：调度者没有给出理由。一次都没被派到的成员：汇总员。先看成员有没有绑定要用的工具，再调大「最多轮数」；也可以把「用完轮数时」改成降档交付'
  const failOlder = '协作团队用完 3 轮还未完成：还缺结论'
  const x1 = mod.exhaustedOf(failNow)
  const x2 = mod.exhaustedOf(failBefore)
  const x3 = mod.exhaustedOf(failOlder)
  check('exhaustedOf：后端现在的报错取得出轮数、理由、没派到的成员', x1?.rounds === 3 && x1.reason === '调度者未给出理由'
    && x1.never?.join('、') === '汇总员', JSON.stringify(x1))
  check('exhaustedOf：整改前的报错一样取得出', x2?.rounds === 3 && x2.reason === '调度者没有给出理由'
    && x2.never?.join('、') === '汇总员', JSON.stringify(x2))
  check('exhaustedOf：更早的「还未完成」也认得，不相干的话不误认', x3?.rounds === 3 && x3.reason === '还缺结论'
    && mod.exhaustedOf('协作团队未交出结论：所有被分派的成员都未能交回结果') === null, JSON.stringify(x3))

  // 查询工具的结果：以「SQL 被拒绝」「查询失败」开头的是失败；到点放弃的从原话里取秒数（整改前写「30s」，现在写「30 秒」）
  const toolRun = (preview, extra = {}) => flatten(mod.decodeRun([
    E(1, 'node.started', 'q', { node_type: 'agent', label: '取数' }),
    E(2, 'tool.start', 'q', { tool: 'db_query__shop', call_id: 'c1', args: { sql: 'SELECT 1' } }),
    E(3, 'tool.end', 'q', { tool: 'db_query__shop', call_id: 'c1', preview, ...extra }),
  ])).find((s) => s.id === 'tool-2')
  for (const [which, preview] of [['SQL 被拒绝', 'SQL 被拒绝：只允许只读查询'], ['查询失败', '查询失败：relation "x" does not exist']]) {
    const t = toolRun(preview)
    check(`数据库工具以「${which}」开头的结果记为失败`, t?.status === 'failed' && t.level === 'error', `${t?.status} ${t?.level}`)
  }
  for (const [which, preview] of [
    ['后端现在的原文', '查询超过 30 秒没有返回，已停止等待（数据库可能仍在执行，连接会在后台回收）'],
    ['整改前的原文', '查询超过 30s 没有返回，已放弃等待（数据库那边可能还在跑，连接会在后台收回）'],
  ]) {
    const t = toolRun(preview, { timed_out: true })
    check(`到点放弃的查询说清上限（${which}）`, t?.sub === '超过 30 秒上限，已停止等待', t?.sub)
  }

  // 协作成员这一步失败、原因是把工具调用写成了文字：副标题换成和报错行同一个说法
  for (const [which, error] of [
    ['后端现在的原文', '模型以文本形式输出了工具调用的原始标记，未实际调用工具，这一步没有查询到任何数据（常见原因：该成员未绑定工具，或模型、服务不支持工具调用）'],
    ['整改前的原文', '模型输出了工具调用的原始标记，但没有真正调用工具，这一步什么都没查到（常见原因：这个成员没有绑定工具，或者模型、服务不支持工具调用）'],
  ]) {
    const row = flatten(mod.decodeRun([
      E(1, 'node.started', 't', { node_type: 'supervisor', label: '团队' }),
      E(2, 'agent.step.start', 't', { agent: '取数员', round: 0, instruction: '查订单' }),
      E(3, 'agent.step.end', 't', { agent: '取数员', round: 0, failed: true, error }),
    ])).find((s) => s.status === 'failed' && s.nodeId === 't' && s.kind !== 'node')
    check(`协作成员没真调工具，那一行的副标题（${which}）`, row?.sub === '模型未实际调用工具，此步骤未查询到任何数据', row?.sub)
  }
  check('「新」样本的成员失败原因在后端源码里还在', src('engine/nodes/multi.py').includes('模型以文本形式输出了工具调用的原始标记，未实际调用工具'))

  // 子工作流进出的两句 info 日志：唯一的内部痕迹，照原话收成一行
  const sub = flatten(mod.decodeRun([
    E(1, 'node.started', 's', { node_type: 'subgraph', label: '子流程' }),
    E(2, 'log', 's', { level: 'info', message: '进入子工作流「月报」（4 个节点）' }),
    E(3, 'log', 's', { level: 'info', message: '子工作流「月报」完成' }),
  ])).filter((s) => s.kind === 'note').map((s) => s.title)
  check('子工作流进出的日志各收成一行', sub.includes('进入子工作流「月报」（4 个节点）') && sub.includes('子工作流「月报」完成'),
    sub.join(' | '))
})

await section('SQL 检查（数据目录阶段 4B）：指标所依据的查询未通过检查说成一行；助手自查和交付的问题写中文规则名', async () => {
  const E = (seq, type, node_id, data = {}) => ({ seq, type, node_id, ts: 1790000000 + seq, data })
  // 日志按查询归并（同一条查询的问题一行），带着结构化字段：metric_names、problems、checks、source_node、source_field。
  // 界面读字段，不从原话里解析。先核对后端还发这些字段
  const metricsPy = readFileSync(`${root}backend/app/engine/nodes/metrics.py`, 'utf8')
  check('后端发指标未通过 SQL 检查的日志时带着结构化字段', metricsPy.includes('code="metric_sql_check"')
    && ['metrics=ids', 'metric_names=', 'problems=problem.problems', 'checks=', 'source_node', 'source_field'].every((k) => metricsPy.includes(k)),
  '改了 metrics.py 的字段就同步改 decode.ts 的 explainLog 和这里的夹具')
  const CODES = /fanout_sum|stock_summed|join_unconfirmed|ratio_aggregated|missing_valid_filter|unknown_code|wrong_date_column/
  const decodeOne = (fields) => {
    const all = flatten(mod.decodeRun([
      E(1, 'node.started', 'card', { node_type: 'metrics' }),
      E(2, 'log', 'card', { level: 'warn', code: 'metric_sql_check', metric: 'aov', ...fields }),
      E(3, 'node.finished', 'card', { duration_ms: 3 }),
    ]))
    return { step: all.find((s) => s.code === 'metric_sql_check'), node: all.find((s) => s.kind === 'node') }
  }
  // 问题本身带括号（列的中文名里写了单位）：读字段就不存在截断的问题
  const P1 = '「订单」关联「订单明细」是一对多，对「订单」的「订单金额（元）」求和会重复计算'
  const ONE = `指标「客单价」所依据的查询未通过 SQL 检查（${P1}），结果不可靠`
  const one = decodeOne({ metric_names: ['客单价'], problems: [P1], message: ONE })
  check('metric_sql_check 一行：标题点名哪个指标', one.step?.title === '指标「客单价」所依据的查询未通过 SQL 检查', one.step?.title)
  check('……原因放副标题（读 problems 字段），原话留在展开区', one.step?.sub === P1 && one.step?.detail === ONE,
    `${one.step?.sub} / ${one.step?.detail}`)
  check('……是提醒（warn），下一步指到 SQL 和数据目录，节点本身照样成功', one.step?.level === 'warn'
    && !!one.step?.next?.includes('SQL') && !!one.step?.next?.includes('数据目录') && one.step?.fix === 'canvas'
    && one.node?.status === 'done', `${one.step?.level} ${one.step?.next}`)
  const P2 = '「库存」按日期以外的列求和会把存量跨期加总'
  const two = decodeOne({ metric_names: ['客单价', '订单金额'], problems: [P1, P2], message: 'x' })
  check('一条查询两处问题、影响两个指标：标题点名两个指标并写处数，副标题列出两处', two.step?.title === '指标「客单价」「订单金额」所依据的查询有 2 处未通过 SQL 检查'
    && two.step?.sub === `${P1}；${P2}`, `${two.step?.title} / ${two.step?.sub}`)
  const bare = decodeOne({ message: '指标「客单价」所依据的查询未通过 SQL 检查（某个问题），结果不可靠' })
  check('老日志没有结构化字段：只给不点名的标题，不解析原话', bare.step?.title === '指标所依据的查询未通过 SQL 检查' && !bare.step?.sub
    && !bare.step?.fixNode, `${bare.step?.title} / ${bare.step?.sub}`)

  // 助手自查（check）和交付（final.issues）里的 SQL 检查问题：code 是规则编号，field 是 args.sql，另带表、列和 SQL 片段
  const sqlIssue = (level, code, message, extra = {}) => ({ level, node_id: 'q', edge_id: null, field: 'args.sql', code, message,
    table: 'orders', ...extra })
  const ERR = sqlIssue('error', 'fanout_sum', '「订单」关联「订单明细」是一对多，对「订单」的「订单金额」求和会重复计算。请先按订单汇总明细再关联',
    { column: 'total_amount', sql_excerpt: 'SUM(o.total_amount)', relation_id: 'r0a1b2c3d4e5' })
  const WARN = sqlIssue('warning', 'missing_valid_filter', '「订单」定义了有效记录条件「status <> 9」，查询中没有按它筛选')
  const INFO = sqlIssue('info', 'join_unconfirmed', '「订单」与「门店」按推断的关系关联（o.store_id = s.id），这条关系尚未确认', { table: 'stores' })
  const ops = [
    { op: 'add_node', node: { id: 'q', type: 'tool', data: { label: '订单汇总' } } },
    { op: 'done', explanation: '' },
    { op: 'check', status: 'repairing', round: 1, issues: [ERR] },
    { op: 'check', status: 'failed', issues: [ERR] },
    { op: 'final', graph: { nodes: [{ id: 'q' }] }, issues: [ERR, INFO, WARN,
      { level: 'warning', node_id: 'q', edge_id: null, field: null, code: 'tools_dropped', message: '工具被移除' }] },
  ]
  const steps = mod.decodeCopilot(ops, { context: 'canvas' })
  const repairing = steps.find((s) => s.title.startsWith('自查发现'))
  check('自查交回去改的那一行：节点名 + 中文规则名 + 涉及的表和列，不露规则编号',
    repairing?.detail === '「订单汇总」一对多关联后重复计算：「订单」关联「订单明细」是一对多，对「订单」的「订单金额」求和会重复计算。'
      + '请先按订单汇总明细再关联（涉及 orders.total_amount）', repairing?.detail)
  const failedRow = steps.find((s) => s.kind === 'error')
  check('没修好的那一行同样写规则名', !!failedRow?.detail?.includes('一对多关联后重复计算') && !CODES.test(failedRow.detail), failedRow?.detail)
  const sqlRow = steps.find((s) => s.code === 'sql_check')
  check('交付时的 SQL 检查单独一行：数提醒、提示，没修好的错误已经列过不重复', sqlRow?.title === 'SQL 检查：1 处提醒、1 处提示', sqlRow?.title)
  check('……提醒在前、提示在后，每条写规则名和表', sqlRow?.detail === [
    '「订单汇总」未筛选有效记录：「订单」定义了有效记录条件「status <> 9」，查询中没有按它筛选（涉及 orders）',
    '「订单汇总」关联条件未经确认：「订单」与「门店」按推断的关系关联（o.store_id = s.id），这条关系尚未确认（涉及 stores）',
  ].join('\n'), sqlRow?.detail)
  check('……有提醒时是 warn，下一步指到「参数」里的 SQL，单个节点时给打开设置', sqlRow?.level === 'warn' && !!sqlRow.next?.includes('参数')
    && sqlRow.nodeId === 'q' && sqlRow.fix === 'canvas', `${sqlRow?.level} ${sqlRow?.next} ${sqlRow?.nodeId}`)
  check('整个助手流里不露规则编号', !steps.some((s) => CODES.test(`${s.title}\n${s.detail ?? ''}\n${s.sub ?? ''}`)))
  const chatRow = mod.decodeCopilot(ops, { context: 'chat' }).find((s) => s.code === 'sql_check')
  check('问数据页没有画布：下一步说去画布里打开这个工作流，不给「打开设置」', !!chatRow?.next?.includes('在画布中打开') && !chatRow.fix,
    `${chatRow?.next} ${chatRow?.fix}`)
  const out = mod.copilotOutcome(ops)
  check('结局里带着不挡运行的 SQL 检查（没修好的错误在 check 里，不重复）', out.sqlChecks.length === 2
    && out.sqlChecks.map((x) => x.code).join(',') === 'join_unconfirmed,missing_valid_filter'
    && out.check?.issues.length === 1 && out.check.issues[0].code === 'fanout_sum', JSON.stringify(out.sqlChecks.map((x) => x.code)))
  const asCheck = mod.issueSqlCheck(out.check.issues[0])
  check('问题按 SQL 检查读得出级别、表、列、SQL 片段', asCheck?.level === 'error' && asCheck.table === 'orders' && asCheck.column === 'total_amount'
    && asCheck.sql_excerpt === 'SUM(o.total_amount)' && out.check.issues[0].field === 'args.sql', JSON.stringify(asCheck))
  // 和证据面板、运行时间线同一个读法（lib/sqlcheck 的 sqlCheckOf）：关系编号不丢
  check('……关系编号也留着（以前另写的一份丢了 relation_id）', asCheck?.relation_id === 'r0a1b2c3d4e5', JSON.stringify(asCheck))
  check('不是 SQL 检查的问题不按它读', mod.issueSqlCheck({ message: 'x', code: 'tools_dropped' }) === null)
  const infoOnly = mod.decodeCopilot([{ op: 'final', graph: { nodes: [{ id: 'q' }] }, issues: [INFO] }], { context: 'canvas' })
    .find((s) => s.code === 'sql_check')
  check('只有提示：不用提醒色，下一步说依据尚未确认', !!infoOnly && !infoOnly.level && !!infoOnly.next?.includes('尚未确认'),
    `${infoOnly?.level} ${infoOnly?.next}`)
})

await section('并行查询配对（A1）、查询的 SQL 检查（tool.end.checks）、指标的问题指到来源查询的 SQL（B2）', async () => {
  const all = flatten(mod.decodeRun(synthetic.parallelQueryRun(), { status: 'succeeded' }))
  const qa = all.find((s) => s.kind === 'query' && s.nodeId === 'q_a')
  const qb = all.find((s) => s.kind === 'query' && s.nodeId === 'q_b')
  check('两个并行的查询节点用同一个工具：各自的结果配回各自的节点', !!qa?.result?.includes('119160') && !!qb?.result?.includes('"refunds"')
    && !qb.result.includes('119160'), `${qa?.result?.slice(0, 40)} | ${qb?.result?.slice(0, 40)}`)
  check('……两行都收尾了，没有一直显示进行中', qa?.status === 'done' && qb?.status === 'done', `${qa?.status} ${qb?.status}`)
  check('……检查结果挂在出问题的那条查询上', qa?.checks?.length === 1 && !qb?.checks, JSON.stringify(qb?.checks))
  const [c] = qa?.checks ?? []
  check('查询那一行带着 SQL 检查结果（级别、规则、表和列、关系编号），副标题数一下', c?.code === 'fanout_sum' && c.level === 'error'
    && c.table === 'orders' && c.column === 'total_amount' && c.relation_id === 'r1a2b3c4d5e6'
    && qa.sub === 'SQL 检查：1 处错误' && qa.level === 'warn', `${qa?.sub} ${qa?.level}`)
  const rows = all.filter((s) => s.code === 'metric_sql_check')
  check('同一条查询影响两个指标：时间线一行', rows.length === 1, String(rows.length))
  const m = rows[0]
  check('……标题点名两个指标，副标题是问题', m?.title === '指标「订单金额」「客单价」所依据的查询未通过 SQL 检查'
    && m.sub === synthetic.FANOUT_PROBLEM, `${m?.title} / ${m?.sub}`)
  check('……「打开设置」指到来源查询节点的 SQL，不是口径卡', m?.fix === 'canvas' && m.fixNode === 'q_a' && m.fixField === 'args.sql'
    && m.fixLabel === '订单金额查询' && m.nodeId === 'card', `${m?.fixNode} ${m?.fixField} ${m?.fixLabel}`)
  check('……检查结果也挂在这一行，展开能看', m?.checks?.length === 1 && m.checks[0].code === 'fanout_sum')
  check('……下一步说改来源查询的 SQL', !!m?.next?.includes('来源查询的 SQL'), m?.next)
})

await section('双盲复算与反方审稿的日志（期 3）：按 code 和结构化字段说人话，info 也上时间线，老日志缺字段不崩', async () => {
  // 期 3 前后端共用的约定：后端发 log 时带 code 和结构化字段，时间线按 code 解读、不从原话里认。原话照旧留在展开区。
  // 一致、裁决、记入数据目录这几条后端发的是 info：不上时间线的话，双盲复算节点就是一个跑了几十秒、什么都没说的黑盒
  const E = (seq, type, node_id, data = {}) => ({ seq, type, node_id, ts: 1790000000 + seq, data })
  const CARD = 'c0ffee'.repeat(10) + 'beef'
  const DONE = 'facade'.repeat(10) + 'f00d'
  const events = [
    E(1, 'run.started', null, { total: 5 }),
    E(2, 'node.started', 'dc', { node_type: 'dual_check', label: '复核销售额' }),
    E(3, 'log', 'dc', { level: 'warn', code: 'dual_check_same_model', model: 'mx-1', message: '盲算模型与原流程相同（mx-1）' }),
    E(4, 'log', 'dc', { level: 'info', code: 'dual_check_agreed', metrics: 2, message: '2 个指标与盲算一致' }),
    E(5, 'log', 'dc', { level: 'warn', code: 'dual_check_disagreed', metrics: [{ id: 'gmv', name: '销售额' }, { id: 'aov', name: '' }],
      card_artifact: CARD, message: '2 个指标与盲算不一致，已暂停等待审核' }),
    // metric 是指标 ID（F1a 发的是 job["metric_id"]），中文名只在原话里
    E(6, 'log', 'dc', { level: 'warn', code: 'dual_check_limit', limit: 'max_queries', metric: 'aov',
      message: '指标「客单价」的盲算已达每项最多查询次数（6 次），按无法复算处理' }),
    E(7, 'log', 'dc', { level: 'info', code: 'dual_check_resolved', choices: { gmv: 'a', aov: 'neither' }, gaps: 1, message: '审核人已裁决' }),
    // written 的 item 是数据目录表级项的键（F1a 的 dual_check_facts.FACT_ITEMS），界面上要写成中文叫法
    E(8, 'log', 'dc', { level: 'info', code: 'dual_check_catalog', message: '已写入数据目录：「orders」的业务日期',
      written: [{ source: 'shop', table: 'orders', item: 'business_date' }, { source: 'shop', table: 'refunds', item: 'valid_filter' }],
      skipped: [{ reason: '已有已确认的有效行过滤，未覆盖' }] }),
    E(9, 'node.finished', 'dc', { duration_ms: 3000 }),
    // 没有原话的老日志（只有 code 和结构化字段）照样解读
    E(10, 'node.started', 'dc2', { node_type: 'dual_check', label: '复核订单数' }),
    E(11, 'log', 'dc2', { level: 'info', code: 'dual_check_skipped', reason: 'disabled' }),
    E(12, 'node.finished', 'dc2', { duration_ms: 5 }),
    E(13, 'node.started', 'dc3', { node_type: 'dual_check', label: '复核退款' }),
    E(14, 'log', 'dc3', { level: 'info', code: 'dual_check_skipped', reason: 'no_inputs', message: '没有可复算的指标' }),
    E(15, 'node.finished', 'dc3', { duration_ms: 5 }),
    E(16, 'node.started', 'ch', { node_type: 'challenge', label: '反方审稿' }),
    E(17, 'log', 'ch', { level: 'warn', code: 'challenge_same_model', model: 'wm-2', message: '审稿模型与撰写模型相同' }),
    E(18, 'log', 'ch', { level: 'warn', code: 'challenge_limit', limit: 'timeout_s', claims_skipped: 2, message: '已达时长上限' }),
    E(19, 'log', 'ch', { level: 'warn', code: 'challenge_refuted', message: '2 句结论被推翻',
      items: [{ unit: 'u3', text: '增长主要来自新客' }, { unit: 'u5', text: '退款率下降' }] }),
    E(20, 'log', 'ch', { level: 'info', code: 'challenge_done', counts: { stands: 3, weakened: 1, refuted: 2, inconclusive: 0 },
      artifact: DONE, message: '反方审稿完成' }),
    E(21, 'node.finished', 'ch', { duration_ms: 9000 }),
    E(22, 'node.started', 'ch2', { node_type: 'challenge', label: '反方审稿二' }),
    E(23, 'log', 'ch2', { level: 'info', code: 'challenge_skipped', reason: 'disabled', message: '反方审稿已关闭' }),
    E(24, 'node.finished', 'ch2', { duration_ms: 5 }),
    E(25, 'run.finished', null, { status: 'succeeded' }),
  ]
  const all = flatten(mod.decodeRun(events))
  const by = (code, node) => all.find((s) => s.code === code && (!node || s.nodeId === node))
  const CODES = ['dual_check_skipped', 'dual_check_same_model', 'dual_check_agreed', 'dual_check_disagreed', 'dual_check_resolved',
    'dual_check_catalog', 'dual_check_limit', 'challenge_skipped', 'challenge_same_model', 'challenge_done', 'challenge_refuted', 'challenge_limit']
  check('约定里的 12 个日志代码各有一行（info 的也在）', CODES.every((c) => by(c)), CODES.filter((c) => !by(c)).join('、'))
  check('……都挂在自己的节点下，不是平铺', CODES.every((c) => all.some((s) => s.kind === 'node' && s.children?.some((x) => x.code === c))))
  check('……标题里没有内部代码', !all.some((s) => /dual_check_|challenge_/.test(`${s.title}${s.sub ?? ''}${s.next ?? ''}`)))
  check('……有原话的，原话留在展开区', by('dual_check_agreed')?.detail === '2 个指标与盲算一致'
    && by('challenge_done')?.detail === '反方审稿完成', `${by('dual_check_agreed')?.detail} / ${by('challenge_done')?.detail}`)

  const same = by('dual_check_same_model')
  check('dual_check_same_model：点名模型、缺乏独立性、提醒色，入口落到节点的盲算模型', same?.title === '盲算模型与原流程同为「mx-1」，复算缺乏独立性'
    && same.level === 'warn' && !!same.next?.includes('盲算模型') && same.fix === 'canvas' && same.fixField === 'model',
  `${same?.title} / ${same?.level} / ${same?.fixField}`)
  const agreed = by('dual_check_agreed')
  check('dual_check_agreed：几个指标相符，不用提醒色', agreed?.title === '双盲复算一致：2 个指标与盲算结果相符' && agreed.level === 'info',
    `${agreed?.title} / ${agreed?.level}`)
  const dis = by('dual_check_disagreed')
  check('dual_check_disagreed：几个指标不一致、等待裁决，副标题点名（没名字的写 id）', dis?.title === '双盲复算有 2 个指标与盲算结果不一致，等待审核人裁决'
    && dis.sub === '「销售额」「aov」' && dis.level === 'warn', `${dis?.title} / ${dis?.sub}`)
  check('……下一步说去审批卡上逐个选，完整对照的工件可下钻', !!dis?.next?.includes('审批卡') && dis.artifact === CARD, `${dis?.next} / ${dis?.artifact}`)
  const lim = by('dual_check_limit')
  check('dual_check_limit：哪个指标（给的是 ID，不当名字加引号）、哪个上限、去哪儿调', lim?.title === '指标 aov 的盲算已达查询次数上限，按无法复算处理'
    && lim.level === 'warn' && !!lim.next?.includes('核查'), `${lim?.title} / ${lim?.next}`)
  const res = by('dual_check_resolved')
  check('dual_check_resolved：按选择计数（为 0 的不写），缺口放副标题', res?.title === '审核人已裁决：采用原流程 1 个，两边都不对 1 个'
    && res.sub === '1 项记为缺口' && res.level === 'warn', `${res?.title} / ${res?.sub} / ${res?.level}`)
  const cat = by('dual_check_catalog')
  check('dual_check_catalog：记入几项、写的是哪张表的哪一项（中文叫法）、没写入的照实说', cat?.title === '已将 2 项人工确认的事实记入数据目录'
    && !!cat.sub?.includes('「orders」（shop）的业务日期') && cat.sub.includes('「refunds」（shop）的有效记录条件') && cat.sub.includes('1 项未写入')
    && cat.sub.includes('已有已确认的有效行过滤，未覆盖') && !!cat.next?.includes('数据目录') && cat.level === 'info', `${cat?.title} / ${cat?.sub}`)
  check('……副标题里没有目录项的键名（business_date、valid_filter 之类）', !/business_date|valid_filter|\bdedup\b|[a-z]+_[a-z_]+/.test(cat?.sub ?? ''), cat?.sub)
  const off = by('dual_check_skipped', 'dc2')
  check('dual_check_skipped（disabled，没有原话）：照样上时间线，说怎么开启', off?.title === '双盲复算未启用，已跳过'
    && !!off.next?.includes('启用') && off.level === 'info' && off.detail === undefined, `${off?.title} / ${off?.next} / ${off?.detail}`)
  const none = by('dual_check_skipped', 'dc3')
  check('dual_check_skipped（no_inputs）：没有可复算的指标是提醒，入口落到「复算的指标」', none?.title === '没有输入来自查询的指标，已跳过复算'
    && none.level === 'warn' && none.fix === 'canvas' && none.fixField === 'metrics', `${none?.title} / ${none?.level} / ${none?.fixField}`)
  const csame = by('challenge_same_model')
  check('challenge_same_model：点名模型、缺乏独立性', csame?.title === '反方审稿模型与报告撰写模型同为「wm-2」，审稿缺乏独立性'
    && csame.level === 'warn' && !!csame.next, csame?.title)
  const climit = by('challenge_limit')
  check('challenge_limit：哪个上限、几句没审、去哪儿调', climit?.title === '反方审稿已达时长上限，2 句结论未审' && climit.level === 'warn'
    && !!climit.next?.includes('反方审稿'), `${climit?.title} / ${climit?.next}`)
  const ref = by('challenge_refuted')
  check('challenge_refuted：推翻了几句，句子放副标题', ref?.title === '反方审稿推翻了 2 句结论' && ref.sub === '「增长主要来自新客」「退款率下降」'
    && ref.level === 'warn' && !!ref.next, `${ref?.title} / ${ref?.sub}`)
  const done = by('challenge_done')
  check('challenge_done：审了几句、四种结论各几句（为 0 的不写），有推翻时是提醒、工件可下钻', done?.title === '反方审稿完成：审阅了 6 句结论'
    && done.sub === '经受住 3 · 被削弱 1 · 被推翻 2' && done.level === 'warn' && done.artifact === DONE, `${done?.title} / ${done?.sub} / ${done?.level}`)
  const coff = by('challenge_skipped')
  check('challenge_skipped（disabled）：未启用、怎么开启', coff?.title === '反方审稿未启用，已跳过' && coff.level === 'info' && !!coff.next,
    `${coff?.title} / ${coff?.level}`)
  check('……节点照常完成', all.filter((s) => s.kind === 'node').every((s) => s.status === 'done'))

  // 换一种取值：每日金额上限、选择全是原流程、缺口是一份清单、推翻 0 句、跳过原因是一句中文
  const alt = flatten(mod.decodeRun([
    E(1, 'node.started', 'dc', { node_type: 'dual_check' }),
    E(2, 'log', 'dc', { level: 'warn', code: 'dual_check_limit', limit: 'daily_max_usd', message: '今日核查费用已达上限' }),
    E(3, 'log', 'dc', { level: 'info', code: 'dual_check_resolved', choices: { gmv: 'a', aov: 'a' }, gaps: [], message: '审核人已裁决' }),
    E(4, 'log', 'dc', { level: 'info', code: 'dual_check_resolved', choices: { gmv: 'b' }, gaps: ['指标「销售额」采用了盲算的结果'], message: '审核人已裁决' }),
    E(5, 'log', 'dc', { level: 'info', code: 'dual_check_catalog', written: [], skipped: [{ reason: '差异不止一个方面' }], message: '未写入' }),
    // 认不出的目录项键不往界面上放，只写表；老版本给的是一句中文的照写
    E(6, 'log', 'dc', { level: 'info', code: 'dual_check_catalog', message: '已写入',
      written: [{ source: 'shop', table: 'stores', item: 'grain_x' }, { table: 'orders', item: '业务日期：支付时间 paid_at' }], skipped: [] }),
    E(7, 'node.finished', 'dc', {}),
    E(8, 'node.started', 'ch', { node_type: 'challenge' }),
    E(9, 'log', 'ch', { level: 'info', code: 'challenge_done', counts: { stands: 4, weakened: 0, refuted: 0, inconclusive: 1 }, message: '完成' }),
    E(10, 'log', 'ch', { level: 'info', code: 'challenge_skipped', reason: '报告节点没有产出文档', message: '跳过' }),
    E(11, 'log', 'ch', { level: 'info', code: 'challenge_skipped', reason: 'no_claims', message: '跳过' }),
    // no_report：上游的报告节点找得到（找不到时节点直接报错），但它的报告文档读不出来（没写出、保存失败、所在分支没执行）
    E(12, 'log', 'ch', { level: 'info', code: 'challenge_skipped', reason: 'no_report', message: '报告「周报」的报告文档无法读取，本节点已跳过' }),
    E(13, 'node.finished', 'ch', {}),
  ]))
  const daily = alt.find((s) => s.code === 'dual_check_limit')
  check('每日金额上限：不点名指标，下一步说今日费用已达上限', daily?.title === '双盲复算已达每日金额上限' && !!daily.next?.includes('今日核查费用'),
    `${daily?.title} / ${daily?.next}`)
  const [allA, oneB] = alt.filter((s) => s.code === 'dual_check_resolved')
  check('全部采用原流程：没有缺口，不用提醒色', allA?.title === '审核人已裁决：采用原流程 2 个' && allA.sub === '没有缺口' && allA.level === 'info',
    `${allA?.title} / ${allA?.sub} / ${allA?.level}`)
  check('缺口给的是一份清单：照样数出几项', oneB?.title === '审核人已裁决：采用盲算 1 个' && oneB.sub === '1 项记为缺口' && oneB.level === 'warn',
    `${oneB?.title} / ${oneB?.sub}`)
  const [cat0, catOdd] = alt.filter((s) => s.code === 'dual_check_catalog')
  check('一项都没写入数据目录：标题照实说，原因放副标题', cat0?.title === '本次裁决未写入数据目录' && !!cat0.sub?.includes('差异不止一个方面'),
    `${cat0?.title} / ${cat0?.sub}`)
  check('认不出的目录项键不显示（只写表），一句中文的照写', catOdd?.sub === '「stores」（shop）；「orders」的业务日期：支付时间 paid_at',
    catOdd?.sub)
  const calm = alt.find((s) => s.code === 'challenge_done')
  check('没有推翻：不用提醒色，0 句的不写', calm?.title === '反方审稿完成：审阅了 5 句结论' && calm.sub === '经受住 4 · 无法判断 1'
    && calm.level === 'info', `${calm?.title} / ${calm?.sub} / ${calm?.level}`)
  const [said, noClaims, noDoc] = alt.filter((s) => s.code === 'challenge_skipped')
  check('跳过原因是一句中文：照写在副标题；认得的原因说人话', said?.title === '反方审稿已跳过' && said.sub === '报告节点没有产出文档'
    && noClaims?.title === '报告中没有需要审的关键结论，已跳过反方审稿', `${said?.title} / ${said?.sub} / ${noClaims?.title}`)
  check('no_report：说的是报告文档读不出来（不是没找到报告节点），下一步指向报告节点有没有执行成功、在不在执行到的分支上',
    noDoc?.title === '要审的报告没有可读取的报告文档，已跳过反方审稿' && !!noDoc.next?.includes('报告撰写节点是否执行成功')
    && !!noDoc.next?.includes('分支') && !noDoc.next?.includes('连接'), `${noDoc?.title} / ${noDoc?.next}`)

  // 老日志缺字段、字段类型不对：不崩，标题照样说人话，不出 undefined、NaN、[object Object]，不瞎填数
  const odd = [
    ['dual_check_skipped', {}], ['dual_check_same_model', { model: 7 }], ['dual_check_agreed', { metrics: 'x' }],
    ['dual_check_disagreed', { metrics: 'x', card_artifact: 3 }], ['dual_check_resolved', { choices: null, gaps: 'x' }],
    ['dual_check_catalog', { written: 'x', skipped: { reason: 1 } }], ['dual_check_limit', { limit: 9 }],
    ['challenge_skipped', { reason: ['x'] }], ['challenge_same_model', {}], ['challenge_done', { counts: [1, 2] }],
    ['challenge_refuted', { items: {} }], ['challenge_limit', { claims_skipped: 'x' }],
  ]
  let broken = null
  let steps = []
  try {
    steps = flatten(mod.decodeRun([E(1, 'node.started', 'x', { node_type: 'dual_check' }),
      ...odd.map(([code, data], i) => E(i + 2, 'log', 'x', { level: 'warn', code, ...data })), E(99, 'node.finished', 'x', {})]))
  } catch (e) {
    broken = String(e?.message ?? e)
  }
  check('缺字段的老日志：不抛异常', broken === null, broken ?? '')
  const rows = odd.map(([code]) => steps.find((s) => s.code === code))
  check('……每个代码照样一行，标题不空', rows.every((s) => s && s.title), odd.filter((_, i) => !rows[i]?.title).map(([c]) => c).join('、'))
  check('……不出 undefined、NaN、[object Object]，也不瞎填数', !rows.some((s) => /undefined|NaN|object Object|\d/.test(`${s?.title}${s?.sub ?? ''}`)),
    rows.filter((s) => /undefined|NaN|object Object|\d/.test(`${s?.title}${s?.sub ?? ''}`)).map((s) => `${s?.title}|${s?.sub}`).join(' ; '))
  check('……缺字段的工件 id 不当成可下钻的工件', !rows.some((s) => s?.artifact != null))
})

await section('核查类日志与出具缺口（期 4）：无法复算、未能反查、只查了部分数据源说人话，核查报上来的缺口另起两组', async () => {
  // 期 3 时这三个代码界面认不得，只能照原话显示；出具缺口里双盲复算、反方审稿报上来的几条读成
  // 「校验不完整：反方审稿「反方审稿」：反方审稿推翻了 1 句结论…」，前缀和正文重复，归在「校验不完整」下也不准。
  // 核查报上来的另起，再分两组：查出了问题的（两路结果不一致、结论被推翻）是「核查未通过」，没核成的（指标无法复算）
  // 是「核查未完成」——没核过的数说成「未通过」，读的人会以为这个数被查出了错
  const E = (seq, type, node_id, data = {}) => ({ seq, type, node_id, ts: 1790000000 + seq, data })
  const events = [
    E(1, 'run.started', null, { total: 3 }),
    E(2, 'node.started', 'dc', { node_type: 'dual_check', label: '复核销售额' }),
    // metrics 是 [{id, name, reason}]：没起名字的写 id，原因是一句中文
    E(3, 'log', 'dc', { level: 'warn', code: 'dual_check_unchecked', message: '双盲复算未能核对 2 个指标，按无法复算计入缺口',
      metrics: [{ id: 'gmv', name: '销售额', reason: '输入来自合并查询，无法单独复算' }, { id: 'aov', name: '', reason: '盲算已达查询次数上限' }] }),
    E(4, 'node.finished', 'dc', { duration_ms: 20 }),
    E(5, 'node.started', 'ch', { node_type: 'challenge', label: '审月报' }),
    E(6, 'log', 'ch', { level: 'warn', code: 'challenge_unreviewed', claims_skipped: 2, reason: '数据源「北仓库」已不存在或已停用',
      message: '反方审稿有 2 句结论未能反查，已记为无法判断：数据源「北仓库」已不存在或已停用' }),
    E(7, 'log', 'ch', { level: 'warn', code: 'challenge_sources_lost', claims: 1, sources: ['北仓库', '南仓库'],
      message: '反方审稿有 1 句结论只查了部分数据源' }),
    E(8, 'node.finished', 'ch', { duration_ms: 30 }),
    E(9, 'node.started', 'done', { node_type: 'output', label: '成果' }),
    E(10, 'issuance', 'done', { tier: 'degraded', missing_required: [], missing_expected: [], unmatched: 0, metrics_checked: 2, matched_numbers: 3,
      gaps: ['双盲复算「复核销售额」：指标「销售额」两路结果不一致，审核人选择了盲算结果', '反方审稿：推翻了 1 句结论：「东区增长主要来自新客」',
        '叙述模板渲染为空（路径可能有误）', '数据整形「双盲复算备份」：两路结果不一致', '双盲复算「复核销售额」：指标「订单数」无法复算：输入来自合并查询'] }),
    E(11, 'node.finished', 'done', { duration_ms: 5 }),
    E(12, 'run.finished', null, { status: 'succeeded' }),
  ]
  const steps = mod.decodeRun(events)
  const all = flatten(steps)
  const by = (code) => all.find((s) => s.code === code)
  const NEW = ['dual_check_unchecked', 'challenge_unreviewed', 'challenge_sources_lost']
  check('三个日志代码各有一行、挂在自己的节点下', NEW.every((c) => all.some((s) => s.kind === 'node' && s.children?.some((x) => x.code === c))),
    NEW.filter((c) => !by(c)).join('、'))
  check('……标题里没有内部代码，原话留在展开区', NEW.every((c) => !/dual_check_|challenge_/.test(`${by(c)?.title}${by(c)?.sub ?? ''}${by(c)?.next ?? ''}`))
    && by('challenge_unreviewed')?.detail === events[5].data.message)
  const un = by('dual_check_unchecked')
  // 只说记为缺口，不说「计入出具」：探索运行不出具，节点接在侧枝上时缺口也进不了出具，降不降档由出具那一行说
  check('dual_check_unchecked：几个指标无法复算、记为缺口（不提出具），副标题点名带原因（没名字的写 id），提醒色',
    un?.title === '双盲复算有 2 个指标无法复算，已记为缺口' && un.sub === '「销售额」：输入来自合并查询，无法单独复算；「aov」：盲算已达查询次数上限'
    && un.level === 'warn' && !!un.next?.includes('核查') && !`${un.title}${un.sub}`.includes('出具'), `${un?.title} / ${un?.sub}`)
  const ur = by('challenge_unreviewed')
  // 用「未能」（同服务端原话「未能反查」），不说「没有真正查询数据」（触顶的句子查过）；这一条不含触顶的句子
  // （由 challenge_limit 说），下一步不提调高上限
  check('challenge_unreviewed：几句未能反查、记为无法判断，原因放副标题，下一步不提上限', ur?.title === '反方审稿有 2 句结论未能反查，已记为无法判断'
    && ur.sub === '数据源「北仓库」已不存在或已停用' && ur.level === 'warn' && !!ur.next?.includes('核查模型') && !!ur.next?.includes('数据源')
    && !ur.next.includes('上限') && !/没能|真正/.test(`${ur.title}${ur.next}`), `${ur?.title} / ${ur?.sub} / ${ur?.next}`)
  const lost = by('challenge_sources_lost')
  check('challenge_sources_lost：几句只查了部分数据源，点名没查成的源、判断不含它们', lost?.title === '反方审稿有 1 句结论只查了部分数据源'
    && lost.sub === '未能查询：「北仓库」、「南仓库」（已不存在或已停用），判断不含其中的数据' && lost.level === 'warn' && !!lost.next?.includes('数据源'),
    `${lost?.title} / ${lost?.sub}`)

  const issuance = steps.find((s) => s.kind === 'issuance')
  const title = issuance?.title ?? ''
  const detail = issuance?.detail ?? ''
  const lines = detail.split('\n')
  check('出具那一行：核查查出了问题的两条归在「核查未通过」下', lines.includes(
    '核查未通过：双盲复算「复核销售额」：指标「销售额」两路结果不一致，审核人选择了盲算结果；反方审稿：推翻了 1 句结论：「东区增长主要来自新客」'), detail)
  check('……无法复算的那条归在「核查未完成」下，不在「核查未通过」下', lines.includes(
    '核查未完成：双盲复算「复核销售额」：指标「订单数」无法复算：输入来自合并查询')
    && !lines.some((l) => l.startsWith('核查未通过：') && l.includes('无法复算')), detail)
  check('……校验没做完的那条仍在「校验不完整」下，核查的不在那里', lines.some((l) => l.startsWith('校验不完整：')
    && l.includes('叙述模板渲染为空') && !l.includes('两路结果不一致，审核人') && !l.includes('推翻了') && !l.includes('无法复算')), detail)
  check('……只是名字里带「双盲复算」的别的节点不算核查（按节点类型的前缀认）', lines.some((l) => l.startsWith('校验不完整：')
    && l.includes('数据整形「双盲复算备份」：两路结果不一致')), detail)
  const at = (head) => lines.findIndex((l) => l.startsWith(head))
  check('……先「核查未通过」、再「核查未完成」、最后「校验不完整」，标题以档位开头', title.startsWith('降档出具：核查未通过：')
    && at('核查未通过：') < at('核查未完成：') && at('核查未完成：') < at('校验不完整：'), title)
  check('……前缀和正文不重复节点类型', !/反方审稿：反方审稿|双盲复算「[^」]*」：[^；]*双盲复算/.test(detail), detail)

  // splitIssuanceGaps：成果卡片、证据页按同一个函数分组
  const split = mod.splitIssuanceGaps(['双盲复算：指标「订单数」无法复算：输入来自合并查询', '反方审稿「审周报」：推翻了 2 句结论：「甲」「乙」',
    '缺少必需指标：gmv', '反方审稿报告没写完', '双盲复算「复核」：指标「销售额」两路结果不一致，审核人没有作出选择',
    '双盲复算「无法复算的复核」：指标「客单价」两路结果不一致，审核人判断两边都不对'])
  check('splitIssuanceGaps：「类型：」「类型「名」：」开头的才算核查缺口，同名开头的普通文字不算；无法复算的单列一组', JSON.stringify(split) === JSON.stringify({
    failed: ['反方审稿「审周报」：推翻了 2 句结论：「甲」「乙」', '双盲复算「复核」：指标「销售额」两路结果不一致，审核人没有作出选择',
      '双盲复算「无法复算的复核」：指标「客单价」两路结果不一致，审核人判断两边都不对'],
    unfinished: ['双盲复算：指标「订单数」无法复算：输入来自合并查询'],
    other: ['缺少必需指标：gmv', '反方审稿报告没写完'] }), JSON.stringify(split))

  // 和服务端的说法对得上：双盲复算、反方审稿报缺口的原话（nodes/dual_check.py 的 GAP_*、nodes/challenge.py 的
  // _refuted_gap）加上出具契约的前缀（nodes/io.py 的 _upstream_issuance_gaps：节点名就是类型叫法时只写「类型：」），
  // 按服务端的写法拼出来再分组。服务端改了说法（比如「无法复算」换了个词），这里先变红
  const dcPy = readFileSync(`${root}backend/app/engine/nodes/dual_check.py`, 'utf8')
  const chPy = readFileSync(`${root}backend/app/engine/nodes/challenge.py`, 'utf8')
  const ioPy = readFileSync(`${root}backend/app/engine/nodes/io.py`, 'utf8')
  const gapOf = (name) => dcPy.match(new RegExp(`^${name} = "([^"]+)"$`, 'm'))?.[1]
  const fill = (tpl) => tpl?.replace('{name}', '订单数').replace('{reason}', '输入来自合并查询')
  const refuted = chPy.match(/return f"(推翻了 \{len\(claims\)\} 句结论：\{_named\(claims\)\})"/)?.[1]
  check('读出了服务端报缺口的写法和前缀的写法', ['GAP_BLIND', 'GAP_NEITHER', 'GAP_UNANSWERED', 'GAP_UNCHECKED'].every((n) => !!gapOf(n))
    && !!refuted && ioPy.includes('prefix = f"{kind}：" if node.title == kind else f"{kind}「{node.title}」："'),
    ['GAP_BLIND', 'GAP_NEITHER', 'GAP_UNANSWERED', 'GAP_UNCHECKED'].map((n) => `${n}=${gapOf(n)}`).join(' / '))
  const prefixed = (kind, title, body) => (title === kind ? `${kind}：` : `${kind}「${title}」：`) + body
  const served = mod.splitIssuanceGaps([
    ...['GAP_BLIND', 'GAP_NEITHER', 'GAP_UNANSWERED'].map((n) => prefixed('双盲复算', '复核', fill(gapOf(n)))),
    prefixed('双盲复算', '双盲复算', fill(gapOf('GAP_UNCHECKED'))),
    prefixed('反方审稿', '反方审稿', refuted?.replace('{len(claims)}', '1').replace('{_named(claims)}', '「东区增长主要来自新客」')),
  ])
  check('……两路不一致（选了盲算、两边都不对、没作出选择）和被推翻的归「核查未通过」，无法复算的归「核查未完成」',
    served.failed.length === 4 && served.unfinished.length === 1 && served.unfinished[0].startsWith('双盲复算：指标「订单数」无法复算：')
    && served.other.length === 0, JSON.stringify(served))
  // 出口契约自己报的「正文写在代码块里、结论句未能核对」（nodes/io.py 的 GAP_FENCED_BODY）不是核查节点报的：不带核查
  // 节点的前缀，横幅归「校验未全部完成」、时间线归「校验不完整」。服务端改了说法、加了前缀，或者这边改了分组，这里先变红
  const fencedGap = ioPy.match(/^GAP_FENCED_BODY = "([^"]+)"$/m)?.[1]?.replace('{title}', '写周报')
  const fencedRow = flatten(mod.decodeRun([
    E(1, 'node.started', 'done', { node_type: 'output', label: '成果' }),
    E(2, 'issuance', 'done', { tier: 'degraded', missing_required: [], missing_expected: [], unmatched: 0, gaps: [fencedGap] }),
    E(3, 'node.finished', 'done', { duration_ms: 5 }),
  ])).find((s) => s.kind === 'issuance')
  check('出口契约报的「正文写在代码块里」：读出了服务端的写法，归「校验不完整」，不进核查的两组', !!fencedGap
    && JSON.stringify(mod.splitIssuanceGaps([fencedGap])) === JSON.stringify({ failed: [], unfinished: [], other: [fencedGap] })
    && fencedRow?.title === `降档出具：校验不完整：${fencedGap}`, `${fencedGap} / ${fencedRow?.title}`)

  // dual_check_catalog 的 written 带了写进去的值（期 4）：时间线写「…记为「值」」，值的写法和审批卡上的「会记为「值」」
  // 同一个函数（catalogPatch 的 patchValueText），两处一字不差
  const cp = await import(await load('/src/run/catalogPatch.ts'))
  const { DUAL_CHECK_TEXT: DCT } = await import(await load('/src/lib/terms.ts'))
  const valued = flatten(mod.decodeRun([
    E(1, 'node.started', 'dc', { node_type: 'dual_check' }),
    E(2, 'log', 'dc', { level: 'info', code: 'dual_check_catalog', message: '已写入',
      written: [{ source: 'shop', table: 'orders', item: 'business_date', value: { column: 'paid_at' } },
        { source: 'shop', table: 'refunds', item: 'valid_filter', value: "status <> 'test'" }], skipped: [] }),
    E(3, 'node.finished', 'dc', {}),
  ])).find((s) => s.code === 'dual_check_catalog')
  const date = cp.patchValueText('business_date', { column: 'paid_at' })
  const cond = cp.patchValueText('valid_filter', "status <> 'test'")
  check('dual_check_catalog 带值：业务日期写列名、有效记录条件写原句，「哪张表的哪一项记为「值」」',
    valued?.title === '已将 2 项人工确认的事实记入数据目录'
    && valued.sub === `「orders」（shop）的业务日期记为「${date}」；「refunds」（shop）的有效记录条件记为「${cond}」`
    && date === 'paid_at' && cond === "status <> 'test'", `${valued?.title} / ${valued?.sub}`)
  const card = DCT.approval.catalogPreview.write('采用盲算的结果', DCT.log.catalogItem('orders', 'shop', '业务日期'), date)
  check('……值和审批卡上「会记为「值」」的写法一字不差', card.endsWith(`会记为「${date}」`)
    && (valued?.sub ?? '').startsWith(`${DCT.log.catalogItem('orders', 'shop', '业务日期')}记为「${date}」`), `${card} / ${valued?.sub}`)

  // 老日志缺字段、字段类型不对：不崩，不出 undefined、NaN、[object Object]，不瞎填数
  const odd = [['dual_check_unchecked', { metrics: 'x' }], ['challenge_unreviewed', { claims_skipped: 'x', reason: 3 }],
    ['challenge_sources_lost', { claims: null, sources: 'x' }]]
  let broken = null
  let rows = []
  try {
    const got = flatten(mod.decodeRun([E(1, 'node.started', 'x', { node_type: 'challenge' }),
      ...odd.map(([code, data], i) => E(i + 2, 'log', 'x', { level: 'warn', code, ...data })), E(9, 'node.finished', 'x', {})]))
    rows = odd.map(([code]) => got.find((s) => s.code === code))
  } catch (e) {
    broken = String(e?.message ?? e)
  }
  check('缺字段的老日志：不抛异常，每个代码照样一行', broken === null && rows.every((r) => r?.title), broken ?? '')
  check('……不出 undefined、NaN、[object Object]，也不瞎填数', !rows.some((r) => /undefined|NaN|object Object|\d/.test(`${r?.title}${r?.sub ?? ''}`)),
    rows.map((r) => `${r?.title}|${r?.sub ?? ''}`).join(' ; '))
})

await section('核查模型的输出到了上限被截断（verify_truncated）：说清额度、这次核查没有结果，下一步指到换模型', async () => {
  // 推理模型的思考也算在输出额度里：核查 Agent 的一次调用 finish_reason=length、没有工具调用也没有 JSON 时，以前双盲复算
  // 记成「盲算没有查到这个数」、反方审稿记成「核查模型没有按结构交结果」。现在截断的回复一律不采信，核查 Agent 另发
  // verify_truncated（max_tokens、purpose），时间线按结构化字段说人话，原话留在展开区。先核对后端源码还是这么发的
  const verifyPy = readFileSync(`${root}backend/app/engine/verify_agent.py`, 'utf8')
  check('后端发截断日志的格式还是夹具里这一种', verifyPy.includes('code="verify_truncated"') && verifyPy.includes('max_tokens=limit')
    && verifyPy.includes('purpose=purpose') && !verifyPy.includes('delivered=')
    && verifyPy.includes('TRUNCATED_ERROR = "{cut}，未能给出结果。{advice}"'),
  '改了 verify_agent.py 的字段或说法就同步改 decode.ts 的 explainLog 和这里的夹具')
  const E = (seq, type, node_id, data = {}) => ({ seq, type, node_id, ts: 1790000000 + seq, data })
  const ADVICE = '通常是推理占满了输出额度。请在节点上指定不带推理的模型，或在「设置 → 偏好设置 → 核查」中更换核查模型'
  const FAILED = `核查模型的输出达到上限（16384 token）被截断，未能给出结果。${ADVICE}`
  const SMALL = `核查模型的输出达到上限（4096 token）被截断，未能给出结果。${ADVICE}`
  const all = flatten(mod.decodeRun([
    E(1, 'run.started', null, { total: 2 }),
    E(2, 'node.started', 'dc', { node_type: 'dual_check', label: '复核销售额' }),
    E(3, 'log', 'dc', { level: 'warn', code: 'verify_truncated', purpose: 'verify', max_tokens: 16384, message: FAILED }),
    E(4, 'node.finished', 'dc', { duration_ms: 20 }),
    E(5, 'node.started', 'ch', { node_type: 'challenge', label: '审月报' }),
    // 退回小额度之后在收尾时被截断
    E(6, 'log', 'ch', { level: 'warn', code: 'verify_truncated', purpose: 'verify_result', max_tokens: 4096, message: SMALL }),
    // 缺字段、字段类型不对（别的服务端版本）：只给不带数的标题，不瞎填
    E(7, 'log', 'ch', { level: 'warn', code: 'verify_truncated', max_tokens: '16384', message: FAILED }),
    E(8, 'node.finished', 'ch', { duration_ms: 30 }),
    E(9, 'run.finished', null, { status: 'succeeded' }),
  ]))
  const rows = all.filter((s) => s.code === 'verify_truncated')
  const nodes = all.filter((s) => s.kind === 'node')
  check('每条一行，挂在自己的节点下', rows.length === 3 && nodes.find((s) => s.nodeId === 'dc')?.children?.filter((x) => x.code === 'verify_truncated').length === 1
    && nodes.find((s) => s.nodeId === 'ch')?.children?.filter((x) => x.code === 'verify_truncated').length === 2, `${rows.length} 行`)
  const [failed, small, bare] = rows
  check('标题说输出到了上限被截断、带额度，副标题说这次核查没有结果', failed?.title === '核查模型的输出达到上限（16,384 token）被截断'
    && failed.sub === '这次核查未能给出结果', `${failed?.title} / ${failed?.sub}`)
  check('额度照实写（退回小额度之后是 4,096）', small?.title === '核查模型的输出达到上限（4,096 token）被截断' && small.sub === '这次核查未能给出结果',
    `${small?.title} / ${small?.sub}`)
  check('……下一步指到换不带推理的模型（节点上指定的优先，设置里的也说），入口落到节点的模型，提醒色',
    rows.every((s) => !!s.next?.includes('在节点上指定不带推理的模型') && s.next.includes('设置 → 偏好设置 → 核查')
      && s.fix === 'canvas' && s.fixField === 'model' && s.level === 'warn'),
  `${failed?.next} / ${failed?.fix} / ${failed?.fixField} / ${failed?.level}`)
  check('……没有「已按截断前查到的结果收尾」这一说', !rows.some((s) => /收尾|可能不完整/.test(`${s.title}${s.sub ?? ''}`)))
  check('……原话留在展开区', failed?.detail === FAILED && small?.detail === SMALL)
  check('缺字段、类型不对：标题不带数，不出 undefined、NaN', bare?.title === '核查模型的输出达到上限被截断'
    && !/undefined|NaN/.test(`${bare?.title}${bare?.sub ?? ''}${bare?.next ?? ''}`), `${bare?.title} / ${bare?.sub}`)
  check('标题、副标题里没有内部代码', !rows.some((s) => /verify_truncated|max_tokens|purpose/.test(`${s.title}${s.sub ?? ''}${s.next ?? ''}`)))
  check('节点照常完成', nodes.every((s) => s.status === 'done'))
})

await section('结论句裁判的输出到了上限被截断（judge_truncated）：调用那一行说截断，日志另起一行，下一步指到换模型', async () => {
  // 以前截断的那一批只发 llm.end（标题沿用「结论句裁判未能执行」）和 judge_failed（下一步是「检查接入后重新运行」）：
  // 截断时接入是好的，同一个模型重新运行多半还是截断。现在调用那一行照 truncated、max_tokens 说截断，另发 judge_truncated
  // （max_tokens、unjudged），judge_failed 只说别的失败。先核对后端源码还是这么发的
  const judgePy = readFileSync(`${root}backend/app/engine/judge.py`, 'utf8')
  check('后端发裁判截断的格式还是夹具里这一种', judgePy.includes('code="judge_truncated", max_tokens=cut_limit')
    && judgePy.includes('unjudged=outcome["unjudged"]["truncated"]') && judgePy.includes('truncated=True, max_tokens=cut_limit')
    && judgePy.includes('r not in LIMITS and r != "truncated"'),
  '改了 judge.py 的字段或说法就同步改 decode.ts 的 explainLog、llm.end 和这里的夹具')
  const E = (seq, type, node_id, data = {}) => ({ seq, type, node_id, ts: 1790000000 + seq, data })
  const CUT = '裁判模型的输出达到上限（16384 token）被截断'
  const TRUNCATED = `结论句裁判未完成：有 4 句结论未裁判：${CUT}，通常是推理占满了输出额度，可换用不带推理的裁判模型`
  const all = flatten(mod.decodeRun([
    E(1, 'run.started', null, { total: 1 }),
    E(2, 'node.started', 'write', { node_type: 'report', label: '写周报' }),
    E(3, 'llm.start', 'write', { model: 'jm-r', structured: true, purpose: 'judge', message_count: 2, units: 4 }),
    E(4, 'llm.end', 'write', { model: 'jm-r', purpose: 'judge', duration_ms: 41000, units: 4, input_tokens: 3000,
      output_tokens: 16384, error: CUT, truncated: true, max_tokens: 16384 }),
    E(5, 'log', 'write', { level: 'warn', code: 'judge_truncated', max_tokens: 16384, unjudged: 4, message: TRUNCATED }),
    E(6, 'node.finished', 'write', { duration_ms: 43000 }),
    E(7, 'run.finished', null, { status: 'succeeded' }),
  ]))
  const call = all.find((s) => s.kind === 'llm' && s.code === 'judge')
  check('调用那一行：说输出到了上限被截断、带额度，不说「未能执行」；提醒色、照常完成，原话在展开区',
    call?.title === '裁判模型的输出达到上限（16,384 token）被截断' && call.level === 'warn' && call.status === 'done'
    && !!call.detail?.includes(CUT), `${call?.title} / ${call?.level} / ${call?.status}`)
  const row = all.find((s) => s.code === 'judge_truncated')
  check('judge_truncated：标题说截断、带额度和几句未裁判', row?.title === '裁判模型的输出达到上限（16,384 token）被截断，4 句结论未裁判',
    row?.title)
  check('……下一步指到换不带推理的裁判模型（节点的「结论句裁判」、设置都说），不说检查接入', !!row?.next?.includes('不带推理的裁判模型')
    && row.next.includes('结论句裁判') && row.next.includes('设置 → 偏好设置 → 证据裁判') && !row.next.includes('接入')
    && row.fix === 'canvas' && row.fixField === 'judge' && row.level === 'warn', `${row?.next} / ${row?.fix} / ${row?.fixField}`)
  check('……原话留在展开区，标题里没有内部代码', row?.detail === TRUNCATED && !/judge_truncated|max_tokens|unjudged/.test(`${row?.title}${row?.next}`))
  check('没有混出一条 judge_failed', !all.some((s) => s.code === 'judge_failed'))
  // 没截断的失败照旧：「未能执行」那一行和 judge_failed 的说法不变
  const other = flatten(mod.decodeRun([
    E(1, 'node.started', 'write', { node_type: 'report' }),
    E(2, 'llm.start', 'write', { model: 'jm-x', structured: true, purpose: 'judge', message_count: 2, units: 2 }),
    E(3, 'llm.end', 'write', { model: 'jm-x', purpose: 'judge', duration_ms: 300, units: 2, error: 'ConnectError: 连不上' }),
    E(4, 'node.finished', 'write', { duration_ms: 400 }),
  ]))
  check('没截断的调用失败照旧叫「结论句裁判未能执行」', other.find((s) => s.kind === 'llm')?.title === '结论句裁判未能执行',
    other.find((s) => s.kind === 'llm')?.title)
  const bare = flatten(mod.decodeRun([E(1, 'node.started', 'w', { node_type: 'report' }),
    E(2, 'log', 'w', { level: 'warn', code: 'judge_truncated', max_tokens: 'x', message: '裁判被截断了' })]))
    .find((s) => s.code === 'judge_truncated')
  check('缺字段、类型不对：标题不带数，不出 undefined、NaN', bare?.title === '裁判模型的输出达到上限被截断'
    && !/undefined|NaN/.test(`${bare?.title}${bare?.next ?? ''}`), bare?.title)
})

await section('审批质询 approval.inquiry（期 4）：一行「审批人质询」，标题带问题前 40 字，副标题说回答是否完成、查了几次', async () => {
  // 前后端共用的约定：运行停在审批上时服务端追加 approval.inquiry，node_id 为空；data 里有问题、划选的原文、回答正文、
  // 回答文档、成没成、触顶原因、查询（queries 和 inquiry_artifacts 两份）。以前这类事件落进「未识别的事件」
  const E = (seq, type, node_id, data = {}) => ({ seq, type, node_id, ts: 1790000000 + seq, data })
  const DOC = 'd0c0'.repeat(16)
  const art = (i) => String(i).repeat(64).slice(0, 64)
  const q = (i) => ({ alias: `Q${i}`, artifact: art(i), sql: `SELECT ${i}`, row_count: 1, truncated: false, source: 'shop' })
  const LONG = '本月华东区销售额环比增长 12%，其中新客贡献了多少？请按门店拆开说明，并说明是否扣除了退款与优惠券抵扣的金额'
  const base = { report: 'write', unit: 'u3', approval_id: 'ap-1', answer_doc: DOC, model: 'verify-mini', usage: { calls: 3 } }
  const events = [
    E(1, 'run.started', null, { nodes: 3 }),
    E(2, 'node.started', 'h', { node_type: 'human', label: '主管审批' }),
    E(3, 'human.requested', 'h', { kind: 'human_node', node_id: 'h', mode: 'approve', title: '月报可以发出吗？' }),
    E(4, 'run.interrupted', 'h', { payload: { kind: 'human_node', node_id: 'h' } }),
    E(5, 'approval.inquiry', null, { ...base, question: '销售额是否扣除了退款？', quote: null, answer_text: '已扣除退款，退款合计 21,500 元。',
      ok: true, limit: null, error: null, inquiry_artifacts: [art(1), art(2)], queries: [q(1), q(2)] }),
    E(6, 'approval.inquiry', null, { ...base, question: LONG, quote: '华东区增长最快', answer_text: '按已查到的结果：新客贡献约一半。',
      ok: true, limit: 'max_queries', error: null, inquiry_artifacts: [1, 2, 3, 4, 5, 6].map(art), queries: [1, 2, 3, 4, 5, 6].map(q) }),
    E(7, 'approval.inquiry', null, { ...base, unit: null, question: '新客数是多少？', quote: null, answer_text: '', answer_doc: null,
      ok: false, limit: null, error: '核查模型未配置，无法作答', inquiry_artifacts: [], queries: [] }),
  ]
  const steps = mod.decodeRun(events)
  const all = flatten(steps)
  const rows = steps.filter((s) => s.code === 'approval_inquiry')
  check('三条质询各一行，挂在顶层（node_id 为空），不再是「未识别的事件」', rows.length === 3
    && !all.some((s) => s.title === '未识别的事件'), steps.map((s) => s.title).join(' | '))
  check('……审批那一行仍在等待，相位仍是 waiting', all.some((s) => s.kind === 'human' && s.nodeId === 'h' && s.status === 'waiting')
    && mod.isAwaitingHuman(steps) && mod.decodePhase(events) === 'waiting', mod.decodePhase(events))
  const [a, b, c] = rows
  check('标题：「审批人质询：」加问题', a?.title === '审批人质询：销售额是否扣除了退款？', a?.title)
  check('长问题只取前 40 字，后面加省略号', b?.title === `审批人质询：${[...LONG].slice(0, 40).join('')}…` && [...LONG].length > 40, b?.title)
  check('答完了：副标题写回答完成、查询 2 次，不用提醒色', a?.sub === '回答完成 · 查询 2 次' && a.level === 'info', `${a?.sub} / ${a?.level}`)
  check('触顶收尾：回答不完整、写明是哪个上限，查询 6 次，提醒色', b?.sub === '回答不完整（已达查询次数上限） · 查询 6 次'
    && b.level === 'warn', `${b?.sub} / ${b?.level}`)
  check('没答成：未能回答、未执行查询，提醒色', c?.sub === '未能回答 · 未执行查询' && c.level === 'warn', `${c?.sub} / ${c?.level}`)
  check('展开区：完整的问题、划选的原文、回答（没答成的写原因）、核查模型', !!b?.detail?.includes(LONG) && b.detail.includes('原文：「华东区增长最快」')
    && b.detail.includes('按已查到的结果') && !!c?.detail?.includes('核查模型未配置，无法作答') && !!a?.detail?.includes('verify-mini'),
  `${b?.detail} // ${c?.detail}`)
  check('回答文档可下钻，没有文档的不挂工件', a?.artifact === DOC && c?.artifact === undefined, `${a?.artifact} / ${c?.artifact}`)
  check('图标同人工审批（kind human），状态完成，不在转圈', rows.every((s) => s.kind === 'human' && s.status === 'done'))
  check('标题、副标题里没有内部代码', !rows.some((s) => /approval\.inquiry|approval_inquiry|max_queries|post_seal/.test(`${s.title}${s.sub ?? ''}`)))

  // 其余三个上限、封存之后追加（只有探索运行可以）
  const more = mod.decodeRun([
    E(1, 'run.started', null, { nodes: 2 }), E(2, 'run.finished', null, { output: {} }),
    E(3, 'approval.inquiry', null, { ...base, approval_id: null, post_seal: true, question: '退款率怎么算的？', answer_text: '退款金额除以订单金额。',
      ok: true, limit: null, error: null, queries: [q(1)], inquiry_artifacts: [art(1)] }),
    E(4, 'approval.inquiry', null, { ...base, question: '甲', answer_text: '乙', ok: true, limit: 'timeout_s', queries: [q(1)] }),
    E(5, 'approval.inquiry', null, { ...base, question: '丙', answer_text: '', ok: false, limit: 'daily_max_usd', queries: [] }),
    E(6, 'approval.inquiry', null, { ...base, question: '丁', answer_text: '戊', ok: true, limit: 'max_cost_usd', queries: [q(1), q(2)] }),
  ]).filter((s) => s.code === 'approval_inquiry')
  check('封存之后问的：副标题写明封存后追加', more[0]?.sub === '回答完成 · 查询 1 次 · 封存后追加', more[0]?.sub)
  check('时长上限、每日金额上限、单次金额上限各有说法', more[1]?.sub === '回答不完整（已达时长上限） · 查询 1 次'
    && more[2]?.sub === '未能回答（已达核查每日金额上限） · 未执行查询' && more[3]?.sub === '回答不完整（已达单次金额上限） · 查询 2 次',
  more.slice(1).map((s) => s.sub).join(' / '))
  check('运行跑完之后来的质询不把相位带回等待', mod.decodePhase([E(1, 'run.started', null, {}), E(2, 'run.finished', null, {}),
    E(3, 'approval.inquiry', null, { question: 'x', ok: true })]) === 'succeeded')

  // 缺字段、类型不对：不崩，不出 undefined、NaN、[object Object]，不瞎填数
  const odd = [{}, { question: 7, queries: 'x', ok: 'yes' }, { question: '  ', answer_text: 3, inquiry_artifacts: {} },
    { question: '只给了工件', inquiry_artifacts: [art(1), art(2), art(3)] }, { question: '有回答没写成没成', answer_text: '有' }]
  let broken = null
  let oddRows = []
  try {
    oddRows = mod.decodeRun(odd.map((d, i) => E(i + 1, 'approval.inquiry', null, d))).filter((s) => s.code === 'approval_inquiry')
  } catch (e) {
    broken = String(e?.message ?? e)
  }
  check('缺字段的质询事件：不抛异常，每条照样一行', broken === null && oddRows.length === odd.length, broken ?? `${oddRows.length} 行`)
  check('……问题不是字符串、是空白：标题只写「审批人质询」', oddRows[0]?.title === '审批人质询' && oddRows[1]?.title === '审批人质询'
    && oddRows[2]?.title === '审批人质询', oddRows.slice(0, 3).map((s) => s.title).join(' / '))
  check('……前三条不出 undefined、NaN、[object Object]，也不瞎填数', !oddRows.slice(0, 3).some((s) => /undefined|NaN|object Object|\d/.test(`${s.title}${s.sub ?? ''}`)),
    oddRows.slice(0, 3).map((s) => `${s.title}|${s.sub}`).join(' ; '))
  check('……只给了质询工件：按工件数说查了几次', oddRows[3]?.sub === '未能回答 · 查询 3 次', oddRows[3]?.sub)
  check('……有回答、没写成没成：只说已回答，不说完成', oddRows[4]?.sub === '已回答' && oddRows[4].level === 'info', `${oddRows[4]?.sub} / ${oddRows[4]?.level}`)
})

await section('单次运行时限将至（cause: run_deadline）：核查没做完的那几行说时限，下一步不叫人调高上限、不叫人查模型和数据源', async () => {
  // 一段执行（两次人工审批之间）有单次运行时限。双盲复算、反方审稿为后续节点预留时间：剩下的时间不够就不再开始，开始了的
  // 按剩余时间缩短时长（服务端 engine/deadline.py）。以前这几项照常记成「已达时长上限」「未能反查」「无法复算」，下一步叫人
  // 调高每句、每项的上限（调高了只会更快撞线），或者去查核查模型和数据源（它们本来就没问题）。现在服务端另起一条、带
  // cause: run_deadline 和 run_seconds，界面照它说。先核对后端还是这么发的
  const dlPy = readFileSync(`${root}backend/app/engine/deadline.py`, 'utf8')
  const chPy = readFileSync(`${root}backend/app/engine/nodes/challenge.py`, 'utf8')
  const dcPy = readFileSync(`${root}backend/app/engine/nodes/dual_check.py`, 'utf8')
  check('后端发的还是这几种形状', dlPy.includes('RUN_DEADLINE = "run_deadline"')
    && chPy.includes('code="challenge_unreviewed", cause=deadline.RUN_DEADLINE')
    && chPy.includes('{"cause": deadline.RUN_DEADLINE, "run_seconds": self.run_seconds, "claims": touched}')
    && dcPy.includes('code="dual_check_unchecked", cause=late[0]["cause"], run_seconds=seconds'),
  '改了 deadline.py、challenge.py、dual_check.py 里这几条日志的字段，就同步改 decode.ts 的 explainLog 和这里的夹具')
  const E = (seq, type, node_id, data = {}) => ({ seq, type, node_id, ts: 1790000000 + seq, data })
  const LATE = '本次运行即将达到 600 秒的时长上限，为保证运行按时完成，剩下 3 句未审'
  const BLIND = '本次运行即将达到 600 秒的时长上限，为保证运行按时完成，未进行盲算'
  const events = [
    E(1, 'run.started', null, { total: 3 }),
    E(2, 'node.started', 'dc', { node_type: 'dual_check', label: '复核五个指标' }),
    // 同一节点里别的原因照旧一条，时限将至的另起一条
    E(3, 'log', 'dc', { level: 'warn', code: 'dual_check_unchecked', message: '双盲复算未能核对 1 个指标：「订单数」（盲算没有查到这个数），按无法复算计入缺口',
      metrics: [{ id: 'orders', name: '订单数', reason: '盲算没有查到这个数' }] }),
    E(4, 'log', 'dc', { level: 'warn', code: 'dual_check_unchecked', cause: 'run_deadline', run_seconds: 600,
      message: `本次运行即将达到 600 秒的时长上限，为保证运行按时完成，双盲复算有 3 个指标未能复算：「付款客户数」「最大单笔」「low」，按无法复算计入缺口`,
      metrics: [{ id: 'buyers', name: '付款客户数', reason: BLIND }, { id: 'top', name: '最大单笔', reason: BLIND }, { id: 'low', name: '', reason: BLIND }] }),
    E(5, 'node.finished', 'dc', { duration_ms: 20 }),
    E(6, 'node.started', 'ch', { node_type: 'challenge', label: '审周报' }),
    // 第 1 句触了自己的每句时长上限，第 2 句的时长缩短过、到时没给出判断：两条都是 limit: timeout_s
    E(7, 'log', 'ch', { level: 'warn', code: 'challenge_limit', limit: 'timeout_s', claims_skipped: 0,
      message: '有 1 句反查已达上限（每句时长上限 90 秒），已按查到的结果收尾' }),
    E(8, 'log', 'ch', { level: 'warn', code: 'challenge_limit', limit: 'timeout_s', claims_skipped: 1, cause: 'run_deadline', run_seconds: 600, claims: 1,
      message: '有 1 句反查已达上限（本次运行即将达到 600 秒的时长上限，反查时长已按剩余时间收紧），已按查到的结果收尾，其中 1 句未能给出结论' }),
    E(9, 'log', 'ch', { level: 'warn', code: 'challenge_unreviewed', claims_skipped: 1, reason: '反查没有执行成功任何查询',
      message: '反方审稿有 1 句结论未能反查，已记为无法判断：反查没有执行成功任何查询' }),
    E(10, 'log', 'ch', { level: 'warn', code: 'challenge_unreviewed', cause: 'run_deadline', run_seconds: 600, claims_skipped: 3, reason: LATE,
      message: `反方审稿有 3 句结论未能反查，已记为无法判断：${LATE}` }),
    E(11, 'node.finished', 'ch', { duration_ms: 30 }),
    E(12, 'run.finished', null, { status: 'succeeded' }),
  ]
  const all = flatten(mod.decodeRun(events))
  const rows = (code) => all.filter((s) => s.code === code)
  const [dcOther, dcLate] = rows('dual_check_unchecked')
  const [ownLimit, lateLimit] = rows('challenge_limit')
  const [chOther, chLate] = rows('challenge_unreviewed')
  const late = [dcLate, lateLimit, chLate]
  check('每条一行，挂在自己的节点下', all.find((s) => s.nodeId === 'dc' && s.kind === 'node')?.children?.filter((x) => x.code === 'dual_check_unchecked').length === 2
    && all.find((s) => s.nodeId === 'ch' && s.kind === 'node')?.children?.filter((x) => /^challenge_/.test(x.code ?? '')).length === 4,
  all.map((s) => s.code).filter(Boolean).join('、'))
  check('dual_check_unchecked：标题说运行时限将至，副标题只点名（没名字的写 id）、带上时限', dcLate?.title === '运行时限将至，双盲复算有 3 个指标未能复算，已记为缺口'
    && dcLate.sub === '「付款客户数」、「最大单笔」、「low」；单次运行时限为 600 秒，已为后续节点预留时间' && dcLate.level === 'warn', `${dcLate?.title} / ${dcLate?.sub}`)
  check('challenge_limit：缩短过的那条不说「已达时长上限」，说几句缩短了、几句没给出判断', lateLimit?.title === '运行时限将至，反方审稿缩短了 1 句结论的反查时长，其中 1 句未能给出判断'
    && lateLimit.sub === '单次运行时限为 600 秒，已为后续节点预留时间' && lateLimit.level === 'warn', `${lateLimit?.title} / ${lateLimit?.sub}`)
  check('challenge_unreviewed：标题说几句未反查、记为无法判断，副标题说时限', chLate?.title === '运行时限将至，反方审稿有 3 句结论未反查，已记为无法判断'
    && chLate.sub === '单次运行时限为 600 秒，已为后续节点预留时间' && chLate.level === 'warn', `${chLate?.title} / ${chLate?.sub}`)
  check('……三行的下一步都说怎么腾出时间：减少句数或指标、在核查前加人工审批、请管理员调大单次运行时限', late.every((s) => !!s?.next?.includes('人工审批')
    && s.next.includes('单次运行时限') && s.next.includes('AGENTLAB_MAX_RUN_SECONDS'))
    && dcLate?.next?.includes('「复算的指标」') && !!chLate?.next?.includes('句数') && lateLimit?.next === chLate?.next, late.map((s) => s?.next).join(' / '))
  check('……不叫人调高核查的上限，也不叫人去查核查模型和数据源', late.every((s) => !/调高|核查模型|数据源是否/.test(`${s?.title}${s?.sub ?? ''}${s?.next ?? ''}`)),
    late.map((s) => s?.next).join(' / '))
  check('……原话留在展开区，标题、副标题、下一步里没有内部代码', late.every((s, i) => s?.detail === events[[3, 7, 9][i]].data.message
    && !/run_deadline|cause|run_seconds|timeout_s/.test(`${s.title}${s.sub ?? ''}${s.next ?? ''}`)))
  // 别的原因照旧：同一节点里的两条分得开，不是两行一样的「已达时长上限」
  check('别的原因那几行照旧：触了自己的上限仍叫人调高上限，未能反查、无法复算仍按原因说', ownLimit?.title === '反方审稿已达时长上限'
    && !!ownLimit.next?.includes('调高') && ownLimit.title !== lateLimit?.title
    && chOther?.title === '反方审稿有 1 句结论未能反查，已记为无法判断' && chOther.sub === '反查没有执行成功任何查询' && !!chOther.next?.includes('核查模型')
    && dcOther?.title === '双盲复算有 1 个指标无法复算，已记为缺口' && dcOther.sub === '「订单数」：盲算没有查到这个数' && !!dcOther.next?.includes('合并查询'),
  [ownLimit, chOther, dcOther].map((s) => `${s?.title}|${s?.sub ?? ''}`).join(' / '))

  // 都缩短了、都给出了判断；缺字段、类型不对（别的服务端版本）：不崩，不出 undefined、NaN、[object Object]，不瞎填数
  const odd = flatten(mod.decodeRun([
    E(1, 'node.started', 'ch', { node_type: 'challenge' }),
    E(2, 'log', 'ch', { level: 'warn', code: 'challenge_limit', limit: 'timeout_s', claims_skipped: 0, cause: 'run_deadline', run_seconds: 1800, claims: 2, message: 'x' }),
    E(3, 'log', 'ch', { level: 'warn', code: 'challenge_limit', limit: 'timeout_s', cause: 'run_deadline', run_seconds: 'x', claims: 'y', message: 'x' }),
    E(4, 'log', 'ch', { level: 'warn', code: 'challenge_unreviewed', cause: 'run_deadline', claims_skipped: 'x', reason: LATE, message: 'x' }),
    E(5, 'log', 'ch', { level: 'warn', code: 'dual_check_unchecked', cause: 'run_deadline', run_seconds: -1, metrics: 'x', message: 'x' }),
    E(6, 'node.finished', 'ch', {}),
  ])).filter((s) => s.code && s.kind !== 'node')
  const [settled, bareLimit, bareUnreviewed, bareUnchecked] = odd
  check('都给出了判断：说按查到的结果给出判断，时限照写（大于 1,000 用千分位）', settled?.title === '运行时限将至，反方审稿缩短了 2 句结论的反查时长，已按查到的结果给出判断'
    && settled.sub === '单次运行时限为 1,800 秒，已为后续节点预留时间', `${settled?.title} / ${settled?.sub}`)
  check('缺字段：标题不带数，没给时限就不写那句副标题（未能反查的退回原因）', bareLimit?.title === '运行时限将至，反方审稿缩短了反查时长，已按查到的结果给出判断'
    && bareLimit.sub === undefined && bareUnreviewed?.title === '运行时限将至，反方审稿有结论未反查，已记为无法判断' && bareUnreviewed.sub === LATE
    && bareUnchecked?.title === '运行时限将至，双盲复算有指标未能复算，已记为缺口' && bareUnchecked.sub === undefined,
  odd.map((s) => `${s.title}|${s.sub ?? ''}`).join(' ; '))
  check('……不出 undefined、NaN、[object Object]', !odd.some((s) => /undefined|NaN|object Object/.test(`${s.title}${s.sub ?? ''}${s.next ?? ''}`)))
})

await section('读入附件 file.extracted：读入多少字、全文多少字、截断和抽不出都看得见，工件可点开', async () => {
  // 服务端 files/text.py：模板函数 file_text 每读一个附件记一条，node_id 是读它的节点。以前这类事件落进「未识别的事件」
  const E = (seq, type, node_id, data = {}) => ({ seq, type, node_id, ts: 1790000000 + seq, data })
  const ART = 'f1e0'.repeat(16)
  const base = { file_id: 'f1', sha256: 'ab'.repeat(32), artifact: ART }
  const steps = mod.decodeRun([
    E(1, 'run.started', null, { nodes: 2 }),
    E(2, 'node.started', 'llm', { node_type: 'llm', label: '读说明' }),
    E(3, 'file.extracted', 'llm', { ...base, name: '报损说明.txt', chars: 1200, total_chars: 1200, truncated: false, ok: true }),
    E(4, 'file.extracted', 'llm', { ...base, name: '合同.pdf', chars: 20000, total_chars: 53120, truncated: true, ok: true }),
    E(5, 'file.extracted', 'llm', { ...base, name: '扫描件.png', chars: 0, total_chars: 0, truncated: false, ok: false,
      reason: '图片里的文字目前读不出来' }),
    E(6, 'node.finished', 'llm', {}),
  ])
  const all = flatten(steps)
  const rows = all.filter((s) => s.code === 'file_extracted')
  check('每读一个附件一行，挂在读它的节点下，不再是「未识别的事件」', rows.length === 3 && !all.some((s) => s.title === '未识别的事件')
    && all.find((s) => s.kind === 'node' && s.nodeId === 'llm')?.children?.filter((c) => c.code === 'file_extracted').length === 3,
  all.map((s) => s.title).join(' | '))
  const [whole, cut, failedRead] = rows
  check('读全了：标题写文件名和读入字数，副标题写全文已全部读入，不用提醒色', whole?.title === '读入附件「报损说明.txt」1,200 字'
    && whole.sub === '全文 1,200 字，已全部读入' && whole.level === undefined, `${whole?.title} / ${whole?.sub} / ${whole?.level}`)
  check('截断了：副标题写全文多少字、只读入了前多少字，提醒色', cut?.title === '读入附件「合同.pdf」20,000 字'
    && cut.sub === '全文 53,120 字，只读入了前 20,000 字，其余没有读入' && cut.level === 'warn', `${cut?.title} / ${cut?.sub}`)
  check('抽不出：标题说抽不出文字，副标题写原因，提醒色', failedRead?.title === '附件「扫描件.png」抽不出文字'
    && failedRead.sub === '图片里的文字目前读不出来' && failedRead.level === 'warn', `${failedRead?.title} / ${failedRead?.sub}`)
  check('读入的那一段可以点开（工件）', rows.every((s) => s.artifact === ART))
  check('标题、副标题里没有内部代码', !rows.some((s) => /file\.extracted|file_extracted|total_chars|truncated/.test(`${s.title}${s.sub ?? ''}`)))
  // 缺字段、类型不对：不崩，不出 undefined、NaN、[object Object]
  let broken = null
  let odd = []
  try {
    odd = flatten(mod.decodeRun([{}, { name: 7, chars: 'x', ok: 'yes' }, { ok: false }].map((d, i) => E(i + 1, 'file.extracted', null, d))))
      .filter((s) => s.code === 'file_extracted')
  } catch (e) {
    broken = String(e?.message ?? e)
  }
  check('缺字段的事件：不抛异常，每条照样一行', broken === null && odd.length === 3, broken ?? `${odd.length} 行`)
  check('……不出 undefined、NaN、[object Object]，没有文件名就不写名字，不知道字数就不写数', !odd.some((s) => /undefined|NaN|object Object|「」|\d/.test(`${s.title}${s.sub ?? ''}`))
    && odd[0]?.title === '读入附件' && odd[2]?.title === '附件抽不出文字', odd.map((s) => `${s.title}|${s.sub ?? ''}`).join(' ; '))
})

await section('人的驳回 run.cancelled reason=rejected（05-已知遗留 (a)）：终态是「被驳回」，副标题写谁、哪一步、备注；手动停止照旧「已取消」', async () => {
  const E = (seq, type, node_id, data = {}) => ({ seq, type, node_id, ts: seq, data })
  const head = [E(1, 'run.started', null, { nodes: 2 }), E(2, 'node.started', 'pay', { label: '付款' }),
    E(3, 'human.requested', 'pay', { title: '确认发送：付款' }), E(4, 'human.resolved', 'pay', { approved: false, note: '金额不对', actor: '张工' }),
    E(5, 'node.failed', 'pay', { error: '「付款」未确认（被驳回），没有发送：金额不对' })]
  const ended = (data) => flatten(mod.decodeRun([...head, E(6, 'run.cancelled', null, data)]))
    .find((s) => s.kind === 'lifecycle' && s.id === 'rc-6')
  const rejected = ended({ reason: 'rejected', node_id: 'pay', label: '付款', kind: 'action_confirm', decision: 'reject',
    actor: '张工', note: '金额不对' })
  check('标题写「被驳回」，不写「已取消」「失败」', rejected?.title === '被驳回' && rejected?.status === 'cancelled',
    `${rejected?.title} / ${rejected?.status}`)
  check('副标题写谁在哪一步驳回、备注', rejected?.sub === '张工 驳回了「付款」：金额不对', rejected?.sub)
  check('指向驳回的那一步（点了画布取景过去）', rejected?.nodeId === 'pay', rejected?.nodeId)
  const anonymous = ended({ reason: 'rejected', node_id: 'pay', label: '付款', decision: 'reject', actor: null, note: '' })
  check('没署名、没备注：「「付款」被驳回」，不出 null、undefined', anonymous?.sub === '「付款」被驳回', anonymous?.sub)
  const expired = ended({ reason: 'rejected', node_id: 'pay', label: '付款', decision: 'expired_reject', actor: null,
    note: '超过时限，按设置驳回' })
  check('超时驳回：写按设置驳回，备注不重复', expired?.sub === '「付款」超过时限，按设置驳回', expired?.sub)
  const phase = mod.decodePhase([...head, E(6, 'run.cancelled', null, { reason: 'rejected' })])
  check('相位收成 cancelled（不是 failed、不给继续运行）', phase === 'cancelled', phase)
  const stopped = ended({ reason: 'stopped', actor: '李工' })
  check('手动停止照旧「已取消」，写谁停的', stopped?.title === '已取消' && stopped?.sub === '李工 取消了本次运行',
    `${stopped?.title} / ${stopped?.sub}`)
  const legacy = ended({})
  check('老事件（没有 reason）照旧「已取消」', legacy?.title === '已取消', legacy?.title)
})

console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 解码器全部通过')
process.exit(failed ? 1 : 0)
