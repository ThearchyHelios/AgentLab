// 运行航迹（run/trace.ts）和图推导（run/derive.ts）的回归检查。
//
// 和 check-decode 一样用**真实运行**导出的事件跑（fixtures.json），不用构造的样本：
// 要守的恰恰是真实事件的脏细节——审批恢复会对同一节点重发 node.started、
// 老运行的事件没有 ts、协作成员的 end 事件晚发。夹具里没有图，下面按节点 id
// 搭了和那次运行一致的最小图；分支、容错、取消这几种夹具里没有的结局才用构造的。
//
// 转译借 vite dev server（它 serve 的就是应用实际运行的那份），所以跑之前
// 前端得起着：./scripts/dev.sh
import { readFileSync } from 'node:fs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const root = new URL('..', import.meta.url).pathname
const fixtures = JSON.parse(
  readFileSync(`${root}frontend/src/run/__tests__/fixtures.json`, 'utf8'))

// trace.ts 在运行时 import 了 decode.ts 和 derive.ts。data: URL 里的模块没法
// 解析 "/src/..." 这种路径，所以把依赖也各自转成 data: URL 再替换进去
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

let trace, derive, decode
try {
  trace = await import(await load('/src/run/trace.ts'))
  derive = await import(await load('/src/run/derive.ts'))
  decode = await import(await load('/src/run/decode.ts'))
} catch (e) {
  console.error(`✗ 拿不到转译结果（${WEB}）——前端没起？先跑 ./scripts/dev.sh\n  ${e.message}`)
  process.exit(1)
}
const { emptyTrace, foldEvent, finalizeTrace, project, isSettled } = trace
const { topology, activeEdgesOf, heldEdgesOf, walkedEdges, applyDerived } = derive

let failed = 0
const check = (name, cond, detail = '') => {
  console.log(`  ${cond ? '✓' : '✗'} ${name}${detail ? ` — ${detail}` : ''}`)
  if (!cond) failed++
}

/** store 里的那条路：折一条、推导一次，终态时收尾 */
const run = (events, graph) => events.reduce((t, ev) => {
  const folded = foldEvent(t, ev)
  if (!graph) return folded
  return isSettled(folded.phase) ? finalizeTrace(folded, graph) : applyDerived(folded, graph)
}, emptyTrace())
const upto = (events, seq) => events.filter((e) => e.seq <= seq)
/** decodeRun 的步骤树摊平：收尾要递归到子步骤，断言也得看到每一行 */
const flat = (steps) => steps.flatMap(function walk(s) { return [s, ...(s.children ?? []).flatMap(walk)] })
const g = (nodes, edges) => ({
  nodes: nodes.map((n) => (typeof n === 'string' ? { id: n } : n)),
  edges: edges.map(([source, target, sourceHandle]) => ({ source, target, sourceHandle: sourceHandle ?? null })),
})
const ms = (x) => `${Math.round(x)}ms`
const LATER = Date.now() + 86_400_000

// 各夹具对应的图（按那次运行的节点 id 搭的）
const G = {
  db: g(['input', 'query_kpi', 'output'], [['input', 'query_kpi'], ['query_kpi', 'output']]),
  // 人工审批带通过 / 驳回两个出口，这次走的是通过：驳回那条下游就是"分支落空"
  human: g([{ id: 'in', type: 'input' }, { id: 'h', type: 'human' }, 'out', 'rejected_note'],
           [['in', 'h'], ['h', 'out', 'approved'], ['h', 'rejected_note', 'rejected']]),
  // 驳回 → 改写 → 再审：rewrite → review 是回边
  loop_approve: g([{ id: 'start', type: 'input' }, 'draft', { id: 'review', type: 'human' }, 'rewrite', 'done'],
                  [['start', 'draft'], ['draft', 'review'], ['review', 'done', 'approved'],
                   ['review', 'rewrite', 'rejected'], ['rewrite', 'review']]),
  fanout: g([{ id: 'start', type: 'input' }, 'leg0', 'leg1', 'leg2', 'merge', 'out'],
            [['start', 'leg0'], ['start', 'leg1'], ['start', 'leg2'],
             ['leg0', 'merge'], ['leg1', 'merge'], ['leg2', 'merge'], ['merge', 'out']]),
  // 循环条件写错了，循环节点当场失败：循环体和 done 出口之后的都被堵住
  failed: g([{ id: 'start', type: 'input' }, 'init_state', { id: 'loop_sum', type: 'loop' }, 'add_one', 'result'],
            [['start', 'init_state'], ['init_state', 'loop_sum'], ['loop_sum', 'add_one', 'body'],
             ['add_one', 'loop_sum'], ['loop_sum', 'result', 'done']]),
  think: g([{ id: 'start', type: 'input' }, { id: 'each', type: 'loop' }, 'handle', 'collect', 'done'],
           [['start', 'each'], ['each', 'handle', 'body'], ['handle', 'collect'], ['collect', 'each'],
            ['each', 'done', 'done']]),
}

console.log('=== 段 ===')
{
  const t = run(fixtures.db, G.db)
  const q = t.nodes.query_kpi
  const started = fixtures.db.find((e) => e.type === 'node.started' && e.node_id === 'query_kpi')
  const finished = fixtures.db.find((e) => e.type === 'node.finished' && e.node_id === 'query_kpi')
  check('一个节点一段，起止取事件的 ts', q?.segments.length === 1
    && q.segments[0].start === started.ts * 1000 && q.segments[0].end === finished.ts * 1000,
    q?.segments.map((s) => `${s.kind}:${s.status}`).join(','))
  check('节点跑完是 done、次数 1', q?.state === 'done' && q.count === 1, `${q?.state} ×${q?.count}`)
  check('工具调用计数、跑完没有挂着的', q?.tools === 1 && q.toolsRunning === 0)
  check('相位是成功', t.phase === 'succeeded', t.phase)
  const p = project(t, LATER)
  check('完成 3 / 共 3', p.nodesDone === 3 && p.nodesTotal === 3, `${p.nodesDone}/${p.nodesTotal}`)
  check('跑完了没有东西还开着', !Object.values(t.nodes).some((n) => n.segments.some((s) => s.end == null)))
  check('llm.token 不改航迹引用',
    foldEvent(t, { seq: 999, type: 'llm.token', node_id: 'query_kpi', ts: 1, data: { delta: 'x' } }) === t)
  check('收过的 seq 再来一遍原样返回', foldEvent(t, fixtures.db[3]) === t)
}

console.log('\n=== 并行度 ===')
{
  const t = run(fixtures.fanout, G.fanout)
  const peak = Math.max(...t.parallelSeries.map(([, v]) => v))
  check('三路同时在跑，峰值 3', peak === 3, `峰值 ${peak}`)
  const at = (seq) => fixtures.fanout.find((e) => e.seq === seq).ts * 1000
  const mid = project(t, at(9) + 1)
  check('三路都开跑后投影出 3 路并行', mid.parallelNow === 3, `${mid.parallelNow}`)
  check('那一刻三路都是 running',
    ['leg0', 'leg1', 'leg2'].every((id) => mid.nodes[id]?.state === 'running'),
    ['leg0', 'leg1', 'leg2'].map((id) => mid.nodes[id]?.state).join(','))
  const later = project(t, at(11) + 1)
  check('先跑完的那路落成 done，并行度降到 2',
    later.nodes.leg2?.state === 'done' && later.parallelNow === 2,
    `${later.nodes.leg2?.state} / ${later.parallelNow}`)
  check('跑完后此刻并行度是 0', project(t, LATER).parallelNow === 0)
  check('回放到开始之前：什么都没跑', project(t, at(1) - 1).nodes.leg0?.state === 'idle')
}

console.log('\n=== defer 汇合 ===')
{
  // 汇合节点等其余任务都跑完再跑一次。一路回来了、其余还在跑时它是"待汇合"
  const e = fixtures.fanout
  const s3 = run(upto(e, 3), G.fanout)
  check('入口跑完、三路还没开始：三路都在排队',
    ['leg0', 'leg1', 'leg2'].every((id) => s3.nodes[id]?.state === 'queued'),
    ['leg0', 'leg1', 'leg2'].map((id) => s3.nodes[id]?.state).join(','))
  const s4 = run(upto(e, 4), G.fanout)
  check('开跑的那路不再排队', s4.nodes.leg0?.state === 'running' && s4.nodes.leg1?.state === 'queued')
  check('上游一路都没回来时汇合节点不排队', (s4.nodes.merge?.state ?? 'idle') === 'idle')
  const s11 = run(upto(e, 11), G.fanout)
  check('一路回来了：汇合节点待汇合', s11.nodes.merge?.state === 'queued', s11.nodes.merge?.state)
  check('待汇合时指向它的边不算活跃（它还没在跑）',
    !activeEdgesOf(s11, G.fanout).some((id) => id.includes('merge')))
  const s16 = run(upto(e, 16), G.fanout)
  check('汇合开跑后三条入边都在流', activeEdgesOf(s16, G.fanout).filter((id) => id.endsWith('|merge|')).length === 3,
    activeEdgesOf(s16, G.fanout).join(' '))
  const done = run(e, G.fanout)
  check('汇合节点只跑了一次', done.nodes.merge?.count === 1, `×${done.nodes.merge?.count}`)
}

console.log('\n=== 墙钟 / 执行 / 等人 ===')
{
  const e = fixtures.human
  const t = run(e, G.human)
  const ts = (type) => e.find((x) => x.type === type).ts * 1000
  const wall = e[e.length - 1].ts * 1000 - e[0].ts * 1000
  const wait = ts('run.resumed') - ts('run.interrupted')
  const p = project(t, LATER)
  check('墙钟 = 第一条到最后一条', Math.abs(p.elapsedMs - wall) < 1, `${ms(p.elapsedMs)} vs ${ms(wall)}`)
  check('等人 = 中断到恢复', Math.abs(p.waitMs - wait) < 1, `${ms(p.waitMs)} vs ${ms(wait)}`)
  check('执行 = 墙钟 − 等人', Math.abs(p.activeMs - (wall - wait)) < 1,
    `${ms(p.activeMs)} vs ${ms(wall - wait)}`)
  check('不用 run.finished.duration_ms 的那个 12ms', p.elapsedMs > 500)
  const h = t.nodes.h
  check('审批恢复后的重放不算第二次执行', h?.count === 1, `×${h?.count}`)
  check('等人节点一行三段：跑 → 等 → 接着跑',
    h?.segments.map((s) => s.kind).join(',') === 'run,wait,run' && h.segments[2].resumed === true,
    h?.segments.map((s) => `${s.kind}:${s.status}`).join(','))

  const waiting = run(upto(e, 6), G.human)
  check('停在审批上：相位 waiting、记下等的是谁', waiting.phase === 'waiting' && waiting.waitingNodeId === 'h')
  check('等待节点的入边没有光点', activeEdgesOf(waiting, G.human).length === 0,
    activeEdgesOf(waiting, G.human).join(' '))
  check('入边改记成停住的边（闸门）', heldEdgesOf(waiting, G.human).join(' ') === 'in|h|',
    heldEdgesOf(waiting, G.human).join(' '))
  const later = project(waiting, ts('run.interrupted') + 5000)
  check('等的时候墙钟和等人一起涨，执行不涨',
    later.waitMs >= 4999 && later.elapsedMs - later.waitMs < 50,
    `墙钟 ${ms(later.elapsedMs)} · 等人 ${ms(later.waitMs)} · 执行 ${ms(later.activeMs)}`)
  check('回放到等待中间：h 是 waiting',
    project(t, ts('run.interrupted') + 1).nodes.h?.state === 'waiting')
}

console.log('\n=== 循环轮次 ===')
{
  const e = fixtures.think
  const t = run(e, G.think)
  const each = t.nodes.each
  check('走 done 出口不再 +1：3 项跑完是第 3 轮', each?.iteration === 3, `第 ${each?.iteration} 轮`)
  check('总项数取 edge.taken.total', each?.iterTotal === 3, `${each?.iterTotal}`)
  check('循环走完了', each?.loopDone === true && each.state === 'done')
  check('循环体一行三段，标号 1 2 3',
    t.nodes.handle?.segments.filter((s) => s.kind === 'run').map((s) => s.iteration).join(',') === '1,2,3',
    t.nodes.handle?.segments.map((s) => s.iteration).join(','))
  const mid = run(upto(e, 47), G.think)
  check('两轮之间循环节点是"容器进行中"，不是打勾',
    mid.nodes.each?.state === 'running' && mid.nodes.each.looping === true, mid.nodes.each?.state)
  check('容器进行中时指向它的边不流动', !activeEdgesOf(mid, G.think).some((id) => id.includes('|each|')))
  check('第二轮时轮次是 2', mid.nodes.each?.iteration === 2)
  check('用量从 llm.end 累加、终态以后端总数校正',
    t.tokensIn === 177 && t.tokensOut === 922, `${t.tokensIn} / ${t.tokensOut}`)
}

console.log('\n=== 循环回边 ===')
{
  const topo = topology(G.loop_approve)
  check('认出改写连回审批那条是回边', topo.back.size === 1 && topo.back.has('rewrite|review|'),
    [...topo.back].join(' '))
  check('去掉回边后拓扑序成立', topo.order.indexOf('review') < topo.order.indexOf('rewrite')
    && topo.order.indexOf('draft') < topo.order.indexOf('review'), topo.order.join(' → '))
  check('层级：审批在第 2 层', topo.rank.review === 2, JSON.stringify(topo.rank))
  const t = run(fixtures.loop_approve, G.loop_approve)
  check('审了三轮就是三次，重放不算', t.nodes.review?.count === 3, `×${t.nodes.review?.count}`)
  check('改写两次', t.nodes.rewrite?.count === 2)
  const walked = walkedEdges(t, G.loop_approve)
  check('走过的边包含回边和两条出口', walked.size === 5, [...walked].join(' '))
}

console.log('\n=== 失败后下游被阻断 ===')
{
  const t = run(fixtures.failed, G.failed)
  check('失败的是循环节点', t.failedNodeId === 'loop_sum' && t.nodes.loop_sum.state === 'failed')
  check('循环体和 done 出口之后都是 blocked',
    t.nodes.add_one?.state === 'blocked' && t.nodes.result?.state === 'blocked',
    `${t.nodes.add_one?.state} / ${t.nodes.result?.state}`)
  check('跑过的上游不受影响', t.nodes.init_state?.state === 'done')
  check('相位是失败，没有活跃的边', t.phase === 'failed' && activeEdgesOf(t, G.failed).length === 0)
}

console.log('\n=== on_error=continue ===')
{
  const ev = (seq, type, node_id, data = {}) => ({ seq, type, node_id, ts: 1000 + seq / 10, data })
  const base = [
    ev(1, 'run.started', null, { nodes: 3 }),
    ev(2, 'node.started', 'a'), ev(3, 'node.finished', 'a', { duration_ms: 10 }),
    ev(4, 'node.started', 'b'), ev(5, 'node.failed', 'b', { error: '接口超时', duration_ms: 20 }),
  ]
  const tolerant = g(['a', { id: 'b', data: { config: { on_error: 'continue' } } }, 'c'], [['a', 'b'], ['b', 'c']])
  const strict = g(['a', 'b', 'c'], [['a', 'b'], ['b', 'c']])
  const mid = run(base, tolerant)
  check('容错失败后下游照常排队', mid.nodes.c?.state === 'queued', mid.nodes.c?.state)
  const ok = run([...base, ev(6, 'node.started', 'c'), ev(7, 'node.finished', 'c'),
                  ev(8, 'run.finished', null, { usage: {} })], tolerant)
  check('跑完：失败的那个仍是 failed，下游 done，不传播阻断',
    ok.phase === 'succeeded' && ok.nodes.b.state === 'failed' && ok.nodes.c.state === 'done')
  const bad = run([...base, ev(6, 'run.failed', null, { error: '接口超时' })], strict)
  check('不容错的失败：下游 blocked', bad.nodes.c?.state === 'blocked', bad.nodes.c?.state)
  check('没有 on_error=continue 时失败节点不放行', mid.nodes.c && run(base, strict).nodes.c?.state !== 'queued')
}

console.log('\n=== 分支落空 ===')
{
  const t = run(fixtures.human, G.human)
  check('审批走了通过：驳回那条下游是 unreached', t.nodes.rejected_note?.state === 'unreached',
    t.nodes.rejected_note?.state)
  check('走过的边只有入口和通过那条',
    [...walkedEdges(t, G.human)].sort().join(' ') === 'h|out|approved in|h|',
    [...walkedEdges(t, G.human)].join(' '))
  const loop = run(fixtures.think, G.think)
  check('循环跑完所有节点都走到了', !Object.values(loop.nodes).some((n) => n.state === 'unreached'))
}

console.log('\n=== 终态清扫 ===')
{
  const ev = (seq, type, node_id, data = {}) => ({ seq, type, node_id, ts: 2000 + seq, data })
  const team = [
    ev(1, 'run.started', null, { nodes: 3 }),
    ev(2, 'node.started', 'in'), ev(3, 'node.finished', 'in'),
    ev(4, 'node.started', 'team'),
    ev(5, 'log', 'team', { level: 'info', round: 0, agents: ['甲', '乙'], done: false }),
    ev(6, 'agent.step.start', 'team', { agent: '甲', round: 0 }),
    ev(7, 'agent.step.start', 'team', { agent: '乙', round: 0 }),
  ]
  const gr = g([{ id: 'in', type: 'input' }, { id: 'team', type: 'supervisor' }, 'out'], [['in', 'team'], ['team', 'out']])
  const live = run(team, gr)
  check('两个成员在跑时并行度是 2', live.parallelSeries.at(-1)[1] === 2)
  check('调度段是推算的', live.nodes.team.segments.some((s) => s.kind === 'dispatch' && s.estimated))
  const stop = run([...team, ev(8, 'run.cancelled', null)], gr)
  check('取消：节点收成 cancelled', stop.phase === 'cancelled' && stop.nodes.team.state === 'cancelled',
    stop.nodes.team.state)
  check('取消：成员段一个不剩地收掉', !stop.nodes.team.segments.some((s) => s.end == null))
  check('取消：没有活跃的边', activeEdgesOf(stop, gr).length === 0)
  check('取消：没走到的是 unreached，不是 blocked', stop.nodes.out?.state === 'unreached', stop.nodes.out?.state)
  check('取消后并行度归零', stop.parallelSeries.at(-1)[1] === 0)

  const down = run([...team, ev(8, 'log', null, { level: 'warn', code: 'server_shutdown', message: '服务关停' })], gr)
  check('服务重启：挂起，不是失败', down.phase === 'suspended' && down.nodes.team.state === 'suspended')
  check('挂起不推"没走到"——还能接着跑', (down.nodes.out?.state ?? 'idle') === 'idle')
  const back = run([...team, ev(8, 'log', null, { level: 'warn', code: 'server_shutdown' }),
                    ev(9, 'run.resumed', null, {}), ev(10, 'run.started', null, { resumed: true }),
                    ev(11, 'node.started', 'team')], gr)
  check('挂起后接着跑：重新开跑、算第二次执行', back.phase === 'running'
    && back.nodes.team.state === 'running' && back.nodes.team.count === 2)
  check('接着跑之后结束时刻清掉', back.endedAt == null)

  const lost = run([...team, { seq: 0, type: 'stream.end', node_id: null, ts: 2008, data: { status: 'interrupted', pending: false } }], gr)
  check('对账：interrupted 且没有待审批 = 挂起', lost.phase === 'suspended' && lost.nodes.team.state === 'suspended')

  const human = fixtures.human
  const held = run([...upto(human, 6), { seq: 0, type: 'stream.end', node_id: null, ts: 0,
                                         data: { status: 'interrupted', pending: true } }], G.human)
  check('对账：有待审批就还是在等人', held.phase === 'waiting' && held.nodes.h.state === 'waiting')
  const gone = run([...upto(human, 6), ev(99, 'run.cancelled', null)], G.human)
  check('等人时取消：等待节点收成 cancelled、等人段闭合',
    gone.nodes.h.state === 'cancelled' && !gone.nodes.h.segments.some((s) => s.end == null) && gone.waitingSince == null)
  const fail = run([...team, ev(8, 'run.failed', null, { error: '超时' })], gr)
  check('失败但没有节点报错（超时）：在跑的收成 cancelled', fail.nodes.team.state === 'cancelled'
    && fail.failedNodeId == null, fail.nodes.team.state)
}

console.log('\n=== 老数据没有 ts ===')
{
  for (const name of ['loop_approve', 'supervisor']) {
    let t
    try { t = run(fixtures[name]) } catch (e) { check(`${name} 折叠不抛错`, false, e.message); continue }
    check(`${name} 标成无时间戳`, t.timed === false)
    const p = project(t, Date.now())
    check(`${name} 投影出结局`, p.phase === 'succeeded', p.phase)
    check(`${name} 时长只来自 duration_ms、是有限值`, Number.isFinite(p.elapsedMs) && p.elapsedMs > 0
      && p.waitMs === 0, `${ms(p.elapsedMs)}`)
    check(`${name} 段按顺序排、没有 NaN`, Object.values(t.nodes).every((n) => n.segments.every((s) =>
      Number.isFinite(s.start) && (s.end == null || Number.isFinite(s.end)))))
  }
  const sup = run(fixtures.supervisor)
  const researcher = sup.nodes.research_supervisor.segments.find((s) => s.kind === 'member')
  check('成员段按 duration 定宽', researcher && researcher.end - researcher.start === 6660,
    researcher && `${researcher.end - researcher.start}`)
  check('等人节点在无 ts 时也只算一次', sup.nodes.human_review?.count === 1)
}

console.log('\n=== 相位和右栏同一个来源 ===')
{
  let same = true
  let where = ''
  for (const [name, events] of Object.entries(fixtures)) {
    let t = emptyTrace()
    for (let i = 0; i < events.length; i++) {
      t = foldEvent(t, events[i])
      const expect = decode.decodePhase(events.slice(0, i + 1))
      if (t.phase !== expect) { same = false; where = `${name}#${events[i].seq}: ${t.phase} vs ${expect}` }
    }
  }
  check('每个前缀上 trace.phase === decodePhase', same, where)
  const upToWait = upto(fixtures.human, 6)
  check('停在审批上的相位是 waiting', decode.decodePhase(upToWait) === 'waiting')
  check('服务强杀后对账：interrupted 且没有待审批是 suspended',
    decode.decodePhase(upto(fixtures.db, 7), { status: 'interrupted', pending: false }) === 'suspended')
  // store 写回 run.status 的是 runStatusOf(phase)，挂起写的是 suspended。消费方照
  // decodePhase(events, {status: run.status}) 传进来，得认得这个值，不然强杀的运行一直转圈
  const cut = upto(fixtures.db, 7)
  check('run.status 写回的 suspended 也认得', decode.decodePhase(cut, { status: 'suspended' }) === 'suspended',
    decode.decodePhase(cut, { status: 'suspended' }))
  check('decodeRun 按 suspended 收尾：没有还在转的行',
    !flat(decode.decodeRun(cut, { status: 'suspended' })).some((s) => s.status === 'running'),
    flat(decode.decodeRun(cut, { status: 'suspended' })).map((s) => `${s.kind}:${s.status}`).join(','))

  // 人工审批：右栏在 human.requested 就把宿主节点标成等你，画布得同一时刻变，
  // 不能等到 run.interrupted（并行时它要等整个超步跑完才来）。恢复后的重放不算新一轮
  let same2 = true
  let where2 = ''
  for (const [name, events] of Object.entries(fixtures)) {
    for (let i = 0; i < events.length; i++) {
      if (!['human.requested', 'human.resolved', 'run.interrupted', 'run.resumed'].includes(events[i].type)) continue
      const cutAt = events.slice(0, i + 1)
      const t = run(cutAt)
      const onCanvas = Object.values(t.nodes).filter((n) => n.state === 'waiting').map((n) => n.id).sort().join()
      const inPanel = decode.decodeRun(cutAt).filter((s) => s.kind === 'node' && s.status === 'waiting')
        .map((s) => s.nodeId).sort().join()
      if (events[i].type !== 'run.resumed' && onCanvas !== inPanel) {
        same2 = false; where2 = `${name}#${events[i].seq}: 画布 [${onCanvas}] 右栏 [${inPanel}]`
      }
    }
  }
  check('审批事件之后画布和右栏认定的"在等人"是同一批节点', same2, where2)
}

console.log('\n=== 并行时 human.requested 先到 ===')
{
  const ev = (seq, type, node_id, data = {}) => ({ seq, type, node_id, ts: 6000 + seq / 10, data })
  const gr = g([{ id: 'in', type: 'input' }, { id: 'h', type: 'human' }, 'x', 'ok'],
               [['in', 'h'], ['in', 'x'], ['h', 'ok', 'approved']])
  const asked = [
    ev(1, 'run.started', null, { nodes: 4 }),
    ev(2, 'node.started', 'in'), ev(3, 'node.finished', 'in'),
    ev(4, 'node.started', 'h'), ev(5, 'node.started', 'x'),
    ev(6, 'human.requested', 'h', { kind: 'human_node', node_id: 'h', mode: 'approve', title: '确认' }),
  ]
  const t = run(asked, gr)
  check('另一路还在跑：审批节点已经是 waiting', t.nodes.h?.state === 'waiting', t.nodes.h?.state)
  check('相位还是 running（整条流水线没停）', t.phase === 'running' && decode.decodePhase(asked) === 'running', t.phase)
  check('右栏同一时刻也是等你', decode.decodeRun(asked).find((s) => s.nodeId === 'h')?.status === 'waiting')
  check('指向它的边不流动、改记成闸门',
    !activeEdgesOf(t, gr).includes('in|h|') && heldEdgesOf(t, gr).includes('in|h|') && activeEdgesOf(t, gr).includes('in|x|'),
    `流动 ${activeEdgesOf(t, gr).join(' ')} · 闸门 ${heldEdgesOf(t, gr).join(' ')}`)
  check('它不算并行里在跑的那几个', t.parallelSeries.at(-1)[1] === 1, `${t.parallelSeries.at(-1)[1]}`)
  const paused = run([...asked, ev(7, 'node.finished', 'x'),
                      ev(8, 'run.interrupted', 'h', { payload: { node_id: 'h' } })], gr)
  check('run.interrupted 来了不再开第二段等待',
    paused.nodes.h.segments.map((s) => s.kind).join(',') === 'run,wait', paused.nodes.h.segments.map((s) => s.kind).join(','))
  const resumed = run([...asked, ev(7, 'node.finished', 'x'), ev(8, 'run.interrupted', 'h', { payload: { node_id: 'h' } }),
                       ev(9, 'run.resumed', null, {}), ev(10, 'run.started', null, { resumed: true }),
                       ev(11, 'node.started', 'h'),
                       ev(12, 'human.requested', 'h', { kind: 'human_node', node_id: 'h', mode: 'approve', title: '确认' })], gr)
  check('恢复后重放的 human.requested 不把它打回等待', resumed.nodes.h.state === 'running' && resumed.nodes.h.count === 1,
    `${resumed.nodes.h.state} ×${resumed.nodes.h.count}`)
  const again = run(upto(fixtures.loop_approve, 75))
  check('驳回后真的第二轮审批：human.requested 一到就是等待', again.nodes.review?.state === 'waiting', again.nodes.review?.state)
}

console.log('\n=== 画布的节点形状（type 是渲染器 card，类型在 data.nodeType） ===')
{
  // store 传进 derive 的是 FlowNode。以前只读 type，拿到的全是 'card'，分支、循环、
  // 审批的出口路由一概不认，没选的出口后面全显示成排队
  const fn = (id, nodeType, config = {}) => ({ id, type: 'card', data: { nodeType, label: id, config } })
  const edge = (id, source, target, sourceHandle = null) => ({ id, source, target, sourceHandle })
  const ev = (seq, type, node_id, data = {}) => ({ seq, type, node_id, ts: 7000 + seq / 10, data })
  const flow = {
    nodes: [fn('in', 'input'), fn('br', 'branch'), fn('a', 'llm'), fn('b', 'llm'), fn('out', 'output')],
    // 分支命中 yes → a；default → b；a 之后也连到 b（b 另有来路）
    edges: [edge('e1', 'in', 'br'), edge('e2', 'br', 'a', 'yes'), edge('e3', 'br', 'b', 'default'),
            edge('e4', 'a', 'b'), edge('e5', 'b', 'out')],
  }
  const spec = { nodes: flow.nodes.map((n) => ({ id: n.id, type: n.data.nodeType, data: { config: n.data.config } })),
                 edges: flow.edges }
  const head = [
    ev(1, 'run.started', null, { nodes: 5 }), ev(2, 'node.started', 'in'), ev(3, 'node.finished', 'in'),
    ev(4, 'node.started', 'br'), ev(5, 'edge.taken', 'br', { branch: 'yes' }), ev(6, 'node.finished', 'br'),
  ]
  const mid = run(head, flow)
  check('分支没选的出口：目标不排队', (mid.nodes.b?.state ?? 'idle') === 'idle', mid.nodes.b?.state)
  check('选中的出口：目标排队', mid.nodes.a?.state === 'queued', mid.nodes.a?.state)
  check('FlowNode 和 GraphSpec 推出同一个结果',
    JSON.stringify(derive.deriveStates(mid, flow)) === JSON.stringify(derive.deriveStates(mid, spec)),
    `${JSON.stringify(derive.deriveStates(mid, flow))} vs ${JSON.stringify(derive.deriveStates(mid, spec))}`)
  // 画布上随手拖出来、还没连线的节点也没有入边：入口得按类型认，不能退到"没有入边的"
  const loose = { nodes: [...flow.nodes, fn('draft', 'llm')], edges: flow.edges }
  check('入口按 data.nodeType 认', topology(loose).entries.join() === 'in', topology(loose).entries.join())
  const all = run([...head, ev(7, 'node.started', 'a'), ev(8, 'node.finished', 'a'),
                   ev(9, 'node.started', 'b'), ev(10, 'node.finished', 'b'),
                   ev(11, 'node.started', 'out'), ev(12, 'node.finished', 'out'),
                   ev(13, 'run.finished', null, { usage: {} })], flow)
  const walked = walkedEdges(all, flow)
  check('b 是从 a 那路到的：分支没选的出口不算走过', !walked.has('e3') && walked.has('e2') && walked.has('e4'),
    [...walked].join(' '))

  const loopG = {
    nodes: [fn('s', 'input'), fn('lp', 'loop', { max_iterations: 5 }), fn('body', 'llm'), fn('after', 'output')],
    edges: [edge('l1', 's', 'lp'), edge('l2', 'lp', 'body', 'body'), edge('l3', 'body', 'lp'),
            edge('l4', 'lp', 'after', 'done')],
  }
  const lp = run([ev(1, 'run.started', null, { nodes: 4 }), ev(2, 'node.started', 's'), ev(3, 'node.finished', 's'),
                  ev(4, 'node.started', 'lp'), ev(5, 'edge.taken', 'lp', { branch: 'body', iteration: 0, total: 3 }),
                  ev(6, 'node.finished', 'lp'), ev(7, 'node.started', 'body')], loopG)
  check('循环还在 body 里转：done 出口的目标不排队', (lp.nodes.after?.state ?? 'idle') === 'idle', lp.nodes.after?.state)

  const hg = {
    nodes: [fn('i', 'input'), fn('h', 'human'), fn('ok', 'output'), fn('no', 'output')],
    edges: [edge('h1', 'i', 'h'), edge('h2', 'h', 'ok', 'approved'), edge('h3', 'h', 'no', 'rejected')],
  }
  const hv = run([ev(1, 'run.started', null, { nodes: 4 }), ev(2, 'node.started', 'i'), ev(3, 'node.finished', 'i'),
                  ev(4, 'node.started', 'h'),
                  ev(5, 'node.finished', 'h', { preview: { approved: true, __decision__: 'approved' } })], hg)
  check('审批通过：驳回出口的目标不排队、通过出口的排队',
    (hv.nodes.no?.state ?? 'idle') === 'idle' && hv.nodes.ok?.state === 'queued',
    `${hv.nodes.no?.state} / ${hv.nodes.ok?.state}`)
}

console.log('\n=== 失败后接着跑 ===')
{
  const ev = (seq, type, node_id, data = {}) => ({ seq, type, node_id, ts: 8000 + seq / 10, data })
  const failed = [
    ev(1, 'run.started', null, { nodes: 2 }),
    ev(2, 'node.started', 'a'), ev(3, 'node.failed', 'a', { error: '模型 id 写错了' }),
    ev(4, 'run.failed', null, { error: '模型 id 写错了' }),
  ]
  const gr = g(['a', 'b'], [['a', 'b']])
  check('失败时记下是谁', run(failed, gr).failedNodeId === 'a')
  const cont = [...failed, ev(5, 'run.resumed', null, {}), ev(6, 'run.started', null, { resumed: true })]
  check('接着跑一开始，上一回合的失败节点就清掉', run(cont, gr).failedNodeId == null, run(cont, gr).failedNodeId)
  const ok = run([...cont, ev(7, 'node.started', 'a'), ev(8, 'node.finished', 'a'),
                  ev(9, 'node.started', 'b'), ev(10, 'node.finished', 'b'), ev(11, 'run.finished', null, { usage: {} })], gr)
  check('接着跑成功了：不再挂着"失败于 a"', ok.phase === 'succeeded' && ok.failedNodeId == null, `${ok.phase} / ${ok.failedNodeId}`)
}

console.log('\n=== 右栏收尾（decodeRun） ===')
{
  const ev = (seq, type, node_id, data = {}) => ({ seq, type, node_id, ts: 9000 + seq, data })
  const live = [
    ev(1, 'run.started', null, { nodes: 4 }),
    ev(2, 'node.started', 'in', { label: '输入' }), ev(3, 'node.finished', 'in'),
    ev(4, 'node.started', 'q', { label: '查询' }),
    ev(5, 'tool.start', 'q', { tool: 'db_query__shop', call_id: 'c1', args: { sql: 'select 1' } }),
    ev(6, 'tool.start', 'q', { tool: 'web_search', call_id: 'c2', args: { q: 'x' } }),
    ev(7, 'node.started', 'team', { label: '协作' }),
    ev(8, 'agent.step.start', 'team', { agent: '甲', round: 0, instruction: '查一下' }),
  ]
  const rows = (steps) => flat(steps).map((s) => `${s.kind}:${s.status}`).join(',')
  const before = decode.decodeRun(live)
  check('进行中：节点、工具、成员、开始执行都在转',
    ['node', 'query', 'tool', 'note', 'lifecycle'].every((k) => flat(before).some((s) => s.kind === k && s.status === 'running')),
    rows(before))

  const stop = decode.decodeRun([...live, ev(9, 'run.cancelled', null)])
  check('取消：一行都不再转', !flat(stop).some((s) => s.status === 'running'), rows(stop))
  check('取消：不画成失败', !flat(stop).some((s) => s.status === 'failed'), rows(stop))
  check('取消：节点下面的查询、工具行也收成已取消',
    ['query', 'tool'].every((k) => flat(stop).find((s) => s.kind === k)?.status === 'cancelled')
    && flat(stop).find((s) => s.nodeId === 'q' && s.kind === 'node')?.status === 'cancelled')
  check('取消：开始执行那行收成已取消', stop.find((s) => s.kind === 'lifecycle')?.status === 'cancelled')
  const member = stop.find((s) => s.nodeId === 'team')?.team?.rounds[0]?.members[0]
  check('取消：协作成员一起收', member?.status === 'cancelled', member?.status)

  const down = decode.decodeRun([...live, ev(9, 'log', null, { level: 'warn', code: 'server_shutdown', message: '服务关停' })])
  check('服务重启：转着的收成挂起', !flat(down).some((s) => s.status === 'running')
    && flat(down).filter((s) => ['node', 'query', 'tool'].includes(s.kind) && s.nodeId !== 'in').every((s) => s.status === 'suspended'),
    rows(down))

  const failedRun = decode.decodeRun([...live, ev(9, 'node.failed', 'q', { error: '查询超时' }),
                                      ev(10, 'run.failed', null, { error: '查询超时' })])
  check('失败：只有失败的那个是红的，连带停下的是已取消',
    flat(failedRun).find((s) => s.nodeId === 'team' && s.kind === 'node')?.status === 'cancelled'
    && flat(failedRun).find((s) => s.nodeId === 'q' && s.kind === 'node')?.status === 'failed'
    && !flat(failedRun).some((s) => s.status === 'running'), rows(failedRun))

  const waiting = decode.decodeRun(upto(fixtures.human, 6))
  check('停在审批上：开始执行收成 done，不再转圈',
    waiting.find((s) => s.kind === 'lifecycle')?.status === 'done', rows(waiting))
  const killed = decode.decodeRun(live, { status: 'interrupted', pending: false })
  check('强杀后对账（interrupted、无待审批）：没有还在转的行',
    !flat(killed).some((s) => s.status === 'running') && flat(killed).some((s) => s.status === 'suspended'), rows(killed))

  const start = (agent, seq) => ev(seq, 'agent.step.start', 'team', { agent, round: 0, instruction: '' })
  const end = (agent, seq, ms) => ev(seq, 'agent.step.end', 'team', { agent, round: 0, duration_ms: ms })
  const fold = (evs) => evs.reduce((t, e) => decode.reduceTeam(t ?? undefined, e) ?? t, undefined)
  check('一轮刚开始：省下 0，不报还没发生的收益', fold([start('甲', 1), start('乙', 2)]).savedMs === 0)
  check('只回来一个：还是 0', fold([start('甲', 1), start('乙', 2), end('甲', 3, 100)]).savedMs === 0)
  check('整轮都交回了：省下 = 各人之和 − 最慢的',
    fold([start('甲', 1), start('乙', 2), end('甲', 3, 100), end('乙', 4, 300)]).savedMs === 100)
}

console.log('\n=== 性能 ===')
{
  // 256 段的大循环（真实运行里见过）：一次折叠 + 投影都要快，拖游标才不卡
  const ev = []
  let seq = 0
  const push = (type, node_id, data = {}) => ev.push({ seq: ++seq, type, node_id, ts: 3000 + seq / 100, data })
  push('run.started', null, { nodes: 4 })
  for (let i = 0; i < 128; i++) {
    push('node.started', 'loop'); push('edge.taken', 'loop', { branch: 'body', iteration: i, total: 128 })
    push('node.finished', 'loop', { duration_ms: 1 })
    push('node.started', 'body'); push('llm.end', 'body', { input_tokens: 1, output_tokens: 2 })
    push('node.finished', 'body', { duration_ms: 5 })
  }
  push('node.started', 'loop'); push('edge.taken', 'loop', { branch: 'done', iteration: 128, total: 128 })
  push('node.finished', 'loop'); push('run.finished', null, { usage: {} })
  const gr = g([{ id: 'loop', type: 'loop' }, 'body', 'after'], [['loop', 'body', 'body'], ['body', 'loop'], ['loop', 'after', 'done']])
  const t0 = performance.now()
  const t = run(ev, gr)
  const foldMs = performance.now() - t0
  const t1 = performance.now()
  for (let i = 0; i < 60; i++) project(t, 3000_000 + i * 50)
  const projMs = (performance.now() - t1) / 60
  check('折叠 ~770 条事件（含 256 段）', foldMs < 100, `${foldMs.toFixed(1)}ms`)
  check('一次投影 < 5ms', projMs < 5, `${projMs.toFixed(2)}ms`)
  check('循环轮次对得上', t.nodes.loop.iteration === 128 && t.nodes.body.count === 128)
}

console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 航迹全部通过')
process.exit(failed ? 1 : 0)
