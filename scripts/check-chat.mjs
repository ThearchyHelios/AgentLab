// 问数据页的检查：会话、轮次、运行全部在浏览器这一侧伪造，页面走的是真的
// chat store / ChatPage / AssistantStream，只有后端换成了脚本。
//
// 一轮问数据在服务端跑（docs/design/orchestration-round/00-总方案.md §1）：页面只发起（POST /api/chat/turns）、只播放
// （GET /api/chat/turns/{id}/stream）。这里伪造的服务端（startTurn / driveTurn，照 backend/app/chat/job.py 的做法）用原来那几份
// 剧本把一轮跑完：生成（copilotScript）→ 发起运行（launchScript）→ 等运行的剧本放完（fakeStream）→ 复核（reviewScript）→
// 照服务端的写法拼那一行（end）。log.gens / runStarts / reviews 照旧记下服务端这一侧做了什么，log.patches 用来守「页面不再写
// 这一轮的结局」。
//
// 为什么不真跑：沙箱没有模型密钥，而且要撞上的恰恰是真跑很难稳定复现的那几种
// 时刻——两个会话同时在跑、答案超过 2000 字被事件截断、跑到一半服务重启、
// 历史取得慢或者取失败。这里每一种都由脚本精确地摆出来。
//
// 守的事：
//   1. 忙不忙按会话算：A 在跑时 B 的按钮是「发送」，停止只停 A 自己，删除保护
//      对准正在跑的那个；
//   2. run.finished 的 output 被截断时，显示用服务端按库里完整成果写的那一份；页面不 PATCH 这一轮；
//      llm.token 不进 events；
//   3. 服务重启：stream.end status=interrupted 且没有待审批，这一轮收成
//      「服务重启，本轮已中断」，给「继续运行」，不再转圈；
//   4. 加载中 / 加载失败 / 真的是空的 三种状态分开，加载失败时不能在空态上提问；
//   5. 已取消的、人驳回这类原样续上还会失败的轮次没有「继续运行」，续跑被拒时说人话；
//   6. 1440×900 下长会话的输入框和发送键完整可见，整页不多滚；
//   7. 首屏例句不拿系统表造句；可信度信息刷新后还在，且写进单独的 meta，不夹在 review 里；
//   8. 从库里恢复的半路轮次（老轮次、服务重启后停着的）：交给服务端接着收尾（mode=resume），核对撞上 500 有出路、
//      补交付不走实时计时，结局照服务端写的那一行摆；
//   9. 老轮次的长答案没取全：刷新后补全；2000 字按码点数，不按 UTF-16；新轮次按库里的完整成果交付；
//  10. 发起那一下没到服务端：报错不糊成一整行粗体，不许诺不存在的自动重试；重跑挂起的轮次
//      顺手取消旧运行；断在建图阶段的老轮次页面不替它写库，重试本轮交给服务端；
//  11. 左栏：没打开过的会话也标得出状态；筛选框在有筛选词时不消失；删掉的会话进回收站，
//      能预览、恢复、彻底删除（要确认）；回收站里只能看，不发起任何运行，正在跑的不给
//      彻底删除（清空也跳过它）；
//  12. 限定数据源：点一下只查这个库，请求里带上 datasource_ids，库不在了说人话、补救是放开范围；
//      库列表没取回来时，从上一问接过来的范围照样看得见；
//  13. 画布助手的「在现有工作流上改 | 从头生成」分段控件；助手出错时同一句报错只说一遍；
//  14. 助手反问（normal-user-apps N11）：只问不改的一轮记成「助手反问」，问题作为这一轮的内容、落库进历史，不再一直停在
//      「正在理解需求」；输入框提示回答助手的问题；刷新后照样标「助手反问」，不当成没查库的回答；
//  15. 回答上的「做成应用」带着这次运行进蓝图（建运行起点的蓝图），不再提取工作流、跳画布的发布弹窗；
//  16. 服务端这一轮已经自查修正过（收到 check 的结论）还没过的：不再整轮重新生成一遍，直接说没过、给补救；助手只写了思考、
//      一个操作都没给（error.next=retry）的照服务端的决定自动重来一次，还不行再照实报。
//  17. 口径账本（next-round 02）：交付了的轮次底部写「正在比对口径」，按退避再取到比对出结果、比完不再取；一行摘要
//      （几个数来自哪份口径）、按口径正式出具和对账按钮照服务端给的动作摆；出具、对账的写请求在浏览器层拦下并核对请求体，
//      出具过的正式运行挂在轮次下面（档位、打开运行）。
//  18. 过程里被驳回的那一步（node.failed 带 reason=rejected）：停止的剪影、中性色、行尾写「被驳回」，不画成完成的绿勾；
//  19. 答案身上的召回标记（账本接口的 recall）写在轮次底部：「已按口径「X」vN 核对，不影响」、依据有变（目录 / 表结构，几项、
//      按新目录新出现几条错误级问题），看的人手上有说的是这个答案的召回待办时链到待办；没比对的答案照样写依据有变；做完的
//      对账卡上写「差在哪」、问说明有没有帮助（说明有用 / 看不懂）。
//  20. 一轮在服务端跑：实时流断了按收到的最后一个 seq 重连，补齐、不重复；刷新、关掉再打开，服务端那一轮照跑，页面从
//      这一次尝试的第一条接着播；停止发给服务端的这一轮（建图中也停得下）；发起、重试、继续运行的请求体照实。
//
// 所有写请求都被拦在浏览器里，不碰任何库。
// 跑之前前端得起着（./scripts/dev.sh），默认连 5273。对别的实例（比如一份沙箱拷贝）跑时
// 带上地址：AGENTLAB_WEB=http://localhost:<前端端口> node scripts/check-chat.mjs
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'
import { mkdirSync, readFileSync } from 'node:fs'
import { ISSUE, RECALL, RECONCILE, WHOLE, WHOLE_LABELS, ledgerOf, metricDetail, reconciledOf } from './ledger-fixtures.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const CHROME = process.env.CHROME_PATH
  ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const SHOTS = process.env.SHOTS ?? '/tmp/agentlab-check-chat'
mkdirSync(SHOTS, { recursive: true })

// 只跑其中几段：ONLY=busy,restart THEMES=dark node scripts/check-chat.mjs
const ONLY = (process.env.ONLY ?? '').split(',').filter(Boolean)
const THEMES = (process.env.THEMES ?? 'light,dark').split(',').filter(Boolean)
/**
 * 一节一节地跑：某一节里等待超时、元素找不到，只记成这一节失败，收掉它开的页面，
 * 接着跑下一节。以前一处超时就让整个脚本崩掉，后面几百项一项都不跑
 */
const opened = new Set()
async function section(name, title, fn) {
  if (ONLY.length && !ONLY.includes(name)) return
  console.log(`\n=== ${title} ===`)
  // 伪造的服务端：上一段里还在跑的几轮就此放下，不再往下写（各段互不牵连）
  srv.gen += 1
  try {
    await fn()
  } catch (e) {
    check(`${title} 中途出错`, false, String(e?.message ?? e).split('\n')[0])
  } finally {
    for (const c of opened) await c.close().catch(() => {})
    opened.clear()
  }
}

let failed = 0
const check = (name, cond, detail = '') => {
  console.log(`  ${cond ? '✓' : '✗'} ${name}${detail ? ` — ${detail}` : ''}`)
  if (!cond) failed++
}

// ---------------------------------------------------------------- 伪造的数据

const iso = (minsAgo) => new Date(Date.now() - minsAgo * 60_000).toISOString()
const YESTERDAY_NOON = () => Math.round((Date.now() - new Date(new Date().setHours(12, 0, 0, 0) - 86_400_000).getTime()) / 60_000)
// 「今天」同理：零点刚过的那一分钟里，「1 分钟前」已经是昨天
const TODAY_RECENT = () => Math.min(1, Math.floor((Date.now() - new Date().setHours(0, 0, 0, 0)) / 60_000))
/**
 * 会话的时间在每次被读到（列表接口序列化、展开拷贝）时现算。以前模块加载时就算好了：
 * 整套跑十几分钟，23:4x 开跑、跑到会话列表那段已经过了零点，「今天」的全成了「昨天」，
 * 分组检查误报。minsAgo 可以是函数：「昨天中午」「今天刚才」要按读的那一刻算
 */
const conv = (id, title, turns, minsAgo, extra = {}) => {
  const ago = typeof minsAgo === 'function' ? minsAgo : () => minsAgo
  return {
    id, title, kind: 'chat', workflow_id: null, archived: false,
    get created_at() { return iso(ago() + 5) },
    get last_active_at() { return iso(ago()) },
    turn_count: turns, last_question: title,
    last_status: turns ? 'done' : null, last_run_id: null, ...extra,
  }
}
const LONG = (n) => Array.from({ length: n }, (_, i) =>
  `${i + 1}. 这一段是很长的结论正文，用来把会话撑到超过一屏，检查输入框会不会被挤出视口。`).join('\n')
const FULL = '这是完整答案的开头。' + '正文'.repeat(1920) + '【完整结尾】'   // 3,850 字左右
// 后端按码点截在 2000：带 BMP 之外的字符时，JS 的 length 会比 2000 多
const ASTRAL_2000 = '旧'.repeat(1995) + '📊'.repeat(5)
const turn = (id, question, extra = {}) => ({
  id, seq: 0, question, answer: '', explanation: '', graph: null, run_id: null,
  status: 'done', error: '', review: null, created_at: iso(30), ...extra,
})
// 坏参数定义的后端原文（tools/custom.py broken_tool_message）。BROKEN_TOOL 是老说法：库里老运行的 error 存的是它，
// 前端要继续认得；BROKEN_TOOL_NOW 是后端现在的说法。「先去改参数定义」那段两份各走一遍
const BROKEN_TOOL = '自定义工具「lookup_order」的参数定义格式不对：参数 store 要写成 {"type": "string"} 这样的对象，'
  + '不能直接写 "string"。到「工具」页把它的参数定义改好再运行'
const BROKEN_TOOL_NOW = '自定义工具「lookup_order」的参数定义格式有误：参数 store 应写成 {"type": "string"} 这样的对象，'
  + '不能直接写 "string"。到「工具」页修改参数定义后再运行'
const GRAPH = {
  nodes: [
    { id: 'in', type: 'input', position: { x: 0, y: 0 }, data: { label: '问题', config: { fields: [{ name: 'question' }] } } },
    { id: 'ag', type: 'agent', position: { x: 0, y: 100 }, data: { label: '查数', config: { max_steps: 12 } } },
    { id: 'out', type: 'output', position: { x: 0, y: 200 }, data: { label: '成果', config: {} } },
  ],
  edges: [{ source: 'in', target: 'ag' }, { source: 'ag', target: 'out' }],
}

/**
 * 问数据的答案（可点击证据第二期）：input → agent → report → output，报告直接引用 agent 查过的
 * 单元格、整表。文档是 compose_doc 真跑出来的，片段接口的答复带查询步骤（窗口、高亮、遮罩）
 */
const EVQ = JSON.parse(readFileSync(new URL('../frontend/src/run/__tests__/evidence-query.json', import.meta.url), 'utf8'))
const EVQ_DOC = { ...EVQ.doc, run_id: 'run-evq' }
/** 整形节点按 JSON 解析失败的两种原话（backend/app/engine/nodes/io.py 的 _json_error） */
const JSON_TEMPLATE_ERROR = '模板渲染出来的不是合法 JSON（第 1 行第 934 列附近）。检查模板里的引号、逗号，字符串值要用 | json 过滤器输出'
const JSON_UPSTREAM_ERROR = '上游「查数」输出的不是合法 JSON（第 934 列附近），常见原因是字符串里有没转义的英文引号；'
  + '让模型交结构化数据请用 output_schema + cite_fields，别用整形节点解析它写的文字'
/**
 * 上面两句是老说法（库里老运行的 error 存的是它们），前端要继续认得；下面两句是后端现在的原文
 *（io.py 的 TEMPLATE_HINT、_json_error 里上游那一句），两组都走一遍
 */
const JSON_TEMPLATE_ERROR_NOW = '模板渲染出来的不是合法 JSON（第 1 行第 934 列附近）。请检查模板中的引号和逗号，字符串值需使用 | json 过滤器输出'
const JSON_UPSTREAM_ERROR_NOW = '上游「查数」输出的不是合法 JSON（第 934 列附近），常见原因是字符串中有未转义的英文引号；'
  + '如需模型输出结构化数据，请配置「结构化输出 Schema」并开启「按出处核对字段」，不要用「数据整形」节点解析模型写的文字'
/** 同一句上游说法，节点名里带着别的规则的关键词（超时）：不能被当成超时、又把「继续运行」给回来 */
const JSON_SLOW_ERROR = JSON_UPSTREAM_ERROR.replace('上游「查数」', '上游「查询超时订单」')
const EVQ_SEG = (text, nth = 0) => Object.values(EVQ.segments).filter((d) => d.segment.text === text)[nth]?.segment.id

const CONVS = {
  busyA: conv('c0busya', '会话 A：跑得很久的那个', 0, TODAY_RECENT),
  idleB: conv('c0idleb', '会话 B：已经答完的那个', 1, 3),
  longC: conv('c0longc', '会话 C：长会话，检查布局', 6, 60),
  slowD: conv('c0slowd', '会话 D：历史取得慢', 2, 90),
  failE: conv('c0faile', '会话 E：历史取失败', 3, 120),
  // 「昨天」按日历算：写死 30 小时前的话，凌晨 6 点前跑会落进前天，分组检查就挂了
  histF: conv('c0histf', '会话 F：取消过、失败过、不可用的历史', 4, YESTERDAY_NOON),
  emptyG: conv('c0emptg', '新对话', 0, 60 * 24 * 3),
  truncH: conv('c0trunc', '会话 H：长答案', 0, 60 * 24 * 10),
  // 下面四个在「列表」那段里没打开过：状态只能从列表接口的 last_status 来
  restI: conv('c0resti', '会话 I：跑到一半服务重启', 0, 60 * 24 * 12, { last_status: 'suspended' }),
  buildJ: conv('c0buildj', '会话 J：建流程就失败', 0, 60 * 24 * 13, { last_status: 'error' }),
  waitK: conv('c0waitk', '会话 K：停在审批上', 0, 60 * 24 * 14, { last_status: 'waiting', last_run_id: 'run-wait' }),
  writeL: conv('c0writel', '会话 L：模型正在出字', 0, 60 * 24 * 15, { last_status: 'running' }),
  metaM: conv('c0metam', '会话 M：刷新之后的可信度', 1, 60 * 24 * 16),
  killN: conv('c0killn', '会话 N：服务被强杀', 0, 60 * 24 * 17),
  suspO: conv('c0suspo', '会话 O：三天前被重启打断的', 1, 60 * 24 * 3, { last_status: 'suspended' }),
  unkP: conv('c0unkp', '会话 P：核对时后端出错', 1, 60 * 24 * 18),
  lateQ: conv('c0lateq', '会话 Q：跑完了、没来得及交付', 1, 60 * 24 * 3),
  termR: conv('c0termr', '会话 R：停在半路的几轮', 6, 60 * 24 * 19),
  clipS: conv('c0clips', '会话 S：长答案没取全', 0, 60 * 24 * 20),
  clipT: conv('c0clipt', '会话 T：刷新之后补全长答案', 3, 60 * 24 * 21),
  launchU: conv('c0launu', '会话 U：发起运行失败', 0, 60 * 24 * 22),
  scopeV: conv('c0scopv', '会话 V：限定数据源', 1, 60 * 24 * 23),
  scopeW: conv('c0scopw', '会话 W：上一问限定过数据源', 1, 60 * 24 * 24),
  buildX: conv('c0buildx', '会话 X：两天前的问题，别处刚又跑了一遍', 2, 60 * 24 * 25),
  fixY: conv('c0fixy', '会话 Y：绑的自定义工具参数定义写坏了', 1, 60 * 24 * 26),
}
/** 回收站里的：列表接口只有带 include_archived 才给 */
const TRASHED = {
  trash1: conv('c0trash1', '删掉的对话甲', 3, 60 * 24 * 2, { archived: true }),
  trash2: conv('c0trash2', '删掉的对话乙', 1, 60 * 24 * 40, { archived: true }),
}
/** 只在「回收站里只能看」那段出现，免得别的段数回收站里有几个时数错 */
const TRASH_MORE = {
  // 各种能补救的轮次都有一个：没跑的、停在审批上的、跑挂了的、结论不可用的
  trash3: conv('c0trash3', '删掉的对话丙：各种没跑完的', 4, 60 * 24 * 4, { archived: true, last_status: 'done' }),
  // 删掉之后又跑起来了（比如在「记录」里通过了审批）：它底下有一个真在跑的运行
  trash4: conv('c0trash4', '删掉的对话丁：还在跑', 1, 60 * 24 * 5,
    { archived: true, last_status: 'running', last_run_id: 'run-trash-live' }),
}
/** 只在「护栏」那段出现：放宽步数只在步数用满、而且还有得放宽时给（后端 engine/guards.py） */
const GRAPH8 = { ...GRAPH, nodes: GRAPH.nodes.map((n) => (n.type === 'agent'
  ? { ...n, data: { ...n.data, config: { max_steps: 8 } } } : n)) }
const guardTurn = (id, graph, reason, detail) => turn(id, '查一下各门店的销量', {
  answer: '只查了一部分的结论', graph, run_id: `run-${id}`,
  review: {
    verdict: 'annotated', note: detail, answer: null, retry: true, severity: 'broken',
    signals: [{ kind: 'step_limit', detail, severity: 'broken', ...(reason ? { reason } : {}) }],
  },
  meta: { v: 1, runId: `run-${id}`, runClass: 'exploratory', runStatus: 'succeeded' },
})
const GUARD_CONVS = {
  stall: conv('c0gstall', '护栏：连续几步没进展收的尾', 1, 60 * 24 * 30),
  steps: conv('c0gsteps', '护栏：写死 8 步、用满了', 1, 60 * 24 * 31),
  dflt: conv('c0gdflt', '护栏：跟随默认步数', 1, 60 * 24 * 32),
}
/** 只在「先去改参数定义」那段出现：同一种失败，后端现在的说法（BROKEN_TOOL_NOW） */
const FIX_CONVS = {
  fixZ: conv('c0fixz', '会话 Z：参数定义有误（后端现在的说法）', 1, 60 * 24 * 27),
}
/**
 * 只在「被驳回」那段出现（05-已知遗留 (a)）：一轮的运行被审批人驳回结束（服务端 cancelled + end_reason=rejected，
 * 落库时 meta.outcome 是 cancelled、meta.rejected 记着那一句）。rejR 会打开、重新运行一次；rejS 不打开，状态只能从
 * 列表接口的 last_status 来
 */
const REJECT_CONVS = {
  rejR: conv('c0rejr', '会话：退款那一问', 1, 60 * 24 * 35, { last_status: 'rejected', last_run_id: 'run-rej2' }),
  rejS: conv('c0rejs', '会话：没打开过的那一问', 1, 60 * 24 * 36, { last_status: 'rejected' }),
}
/** 只在「助手反问」那段出现：库里存着一轮以反问收尾的（answer 列里是助手问的那句话，meta.asked 标着） */
/** 只在「刷新后接着看」那段出现：一开始是空的，问的那一轮在伪造的服务端里跑，刷新、关掉再打开时从那里取回来 */
const REPLAY_CONVS = {
  replay: conv('c0replay', '会话：刷新后接着看', 0, 60 * 24 * 41),
}
const ASKBACK_CONVS = {
  askBack: conv('c0askbk', '助手反问过的会话', 1, 60 * 24 * 34),
}
/** 只在「口径账本」那段出现：库里存着一轮带证据的答案（evidence-doc 的周报），交付了，等着比对口径 */
const LEDGER_CONVS = {
  led: conv('c0ledgr', '问数据：上周的周报', 1, 60 * 24 * 32),
}
/** 只在「问数据的证据」那段出现：库里存着一轮带证据的答案，刷新后回运行取 _evidence */
const ASK_CONVS = {
  askZ: conv('c0askz', '问数据：各区域销售额和最大的一单', 1, 60 * 24 * 33),
}
const DETAIL = {
  c0replay: [],
  c0askbk: [turn('tab1', '上周销售额是多少', { answer: '销售额按下单日还是支付日算？', meta: { v: 1, asked: true } })],
  c0ledgr: [turn('tl1', '上周的周报', {
    answer: '（周报原文）', graph: GRAPH, run_id: 'run-led',
    meta: { v: 1, runId: 'run-led', runClass: 'exploratory', runStatus: 'succeeded', evidence: true },
  })],
  c0askz: [turn('tz9', '上周各区域的销售额，最大的一单是多少', {
    answer: EVQ.output.answer, graph: GRAPH, run_id: 'run-evq',
    meta: { v: 1, runId: 'run-evq', runClass: 'exploratory', runStatus: 'succeeded', evidence: true },
  })],
  c0gstall: [guardTurn('tg1', GRAPH8, 'stall', 'Agent 连续 3 步没有获得新信息，已根据已查到的内容收尾。可能是提示词中的目标无法查到，或工具参数持续有误，请检查 Agent 最后几次的工具调用。收尾时仍未给出结论。')],
  c0gsteps: [guardTurn('tg2', GRAPH8, 'steps', 'Agent 已用完 8 步上限。如需完整结果，请调大节点的「最大步数」或设置中的默认步数；如果大部分步数用于逐张表查询结构，可在提示词中指明要查的表。收尾时仍未给出结论。')],
  c0gdflt: [guardTurn('tg3', GRAPH, null, 'Agent 已用完 12 步上限，仍未给出结论。请调大节点的「最大步数」；如果大部分步数用于逐张表查询结构，可在提示词中指明要查的表。')],
  c0busya: [],
  c0idleb: [turn('tb1', 'B 的问题', { answer: 'B 的答案', graph: GRAPH, run_id: 'run-b1' })],
  c0longc: Array.from({ length: 6 }, (_, i) =>
    turn(`tc${i}`, `第 ${i + 1} 个问题`, { answer: LONG(14), graph: GRAPH, run_id: `run-c${i}` })),
  c0slowd: [turn('td1', 'D 的第一问', { answer: 'D 的答案一' }), turn('td2', 'D 的第二问', { answer: 'D 的答案二' })],
  c0faile: [turn('te1', 'E 的第一问', { answer: 'E 的答案' })],
  c0histf: [
    // 用户点过停止的老数据：status=error + 「已取消」，运行在后端是 cancelled
    turn('tf1', '被我停下的那一轮', { status: 'error', error: '已取消', graph: GRAPH, run_id: 'run-cancelled' }),
    // 运行失败了：可以接着跑，但这次后端会拒绝（比如别处已经把它续跑完了）
    turn('tf2', '跑挂了的那一轮', { status: 'error', error: 'KeyError: 查询超时，数据库没有在 30 秒内返回', graph: GRAPH, run_id: 'run-failed' }),
    // 人工驳回：原样接着跑只会再被驳回一次（lib/explain 的 continuable=false），不给「接着跑」
    turn('tf5', '被人驳回的那一轮', { status: 'error', error: '人工驳回：数字对不上', graph: GRAPH, run_id: 'run-rejected' }),
    // 当时就拆好、落进 meta 的运行失败（带着原话 source，没记 fix 的老写法）：流里照这份画，
    // 不拿原话再讲一遍；直达入口按原话认出来（401 → 去模型接入）
    turn('tf6', '密钥失效的那一轮', {
      status: 'error', error: '当时记下的标题：这把密钥被拒了', graph: GRAPH, run_id: 'run-auth',
      meta: { v: 1, runId: 'run-auth', runStatus: 'failed', failure: {
        title: '当时记下的标题：这把密钥被拒了', reason: '当时记下的原因：对方回了 401。',
        hint: '当时记下的怎么办：换一把密钥再来。', continuable: false,
        source: 'AuthenticationError: Error code: 401 - invalid x-api-key' } },
    }),
    // 没有运行、没有图，却有答案：reply，没查库。老数据没有 meta，靠推断
    turn('tf3', '上一轮的数字是多少', { answer: '上一轮查到的是 42。' }),
    // 复核判为不可用，原因是步数用满
    turn('tf4', '步数用满的那一轮', {
      answer: '只查了一半的结论', graph: GRAPH, run_id: 'run-broken',
      review: {
        verdict: 'annotated', note: 'Agent 已用完 12 步上限，仍未给出结论。', answer: null, retry: true, severity: 'broken',
        signals: [{ kind: 'step_limit', detail: 'Agent 已用完 12 步上限，仍未给出结论。请调大节点的「最大步数」；如果大部分步数用于逐张表查询结构，可在提示词中指明要查的表。', severity: 'broken' }],
      },
      meta: { v: 1, runId: 'run-broken', runClass: 'exploratory', runStatus: 'succeeded', queries: 3, ms: 41000 },
    }),
  ],
  c0emptg: [],
  c0trunc: [],
  c0resti: [],
  c0buildj: [],
  c0waitk: [],
  c0writel: [],
  c0killn: [],
  // 三天前服务重启打断、跑了 42 秒的那一轮。error 是老前端落库时的原话（现在落的是「服务重启，本轮已中断」），
  // 留着旧说法：认中断靠 meta.outcome，界面上照样按现在的说法显示
  c0suspo: [turn('to1', '三天前没跑完的问题', {
    status: 'error', error: '服务重启，这一轮中断了', graph: GRAPH, run_id: 'run-susp', created_at: iso(60 * 24 * 3),
    meta: { v: 1, runId: 'run-susp', runStatus: 'interrupted', outcome: 'suspended', ms: 42000 },
  })],
  // 出具档位、运行类别、耗时、查库次数只存在 meta 里：刷新后得从这里读回来，
  // 而不是再去问一遍运行
  c0metam: [turn('tm1', '上个月各产线的出勤率', {
    answer: '一线 96.2%，二线 91.0%。', graph: GRAPH, run_id: 'run-meta',
    meta: {
      v: 1, runId: 'run-meta', runClass: 'exploratory', runStatus: 'succeeded', ms: 12300, queries: 2,
      issuance: { tier: 'degraded', gaps: ['叙述模板渲染为空'], matched_numbers: 2, metrics_checked: 3, unmatched_numbers: [] },
    },
  })],
  // 上次停在半路（轮次还是 running），核对时后端回 500
  c0unkp: [turn('tp1', '核对时撞上后端出错的那一轮', {
    status: 'running', graph: GRAPH, run_id: 'run-unk', created_at: iso(60 * 24 * 2),
  })],
  // 运行三天前就跑完了，交付没赶上：补交付期间不能按实时运行从三天前起算计时
  c0lateq: [turn('tq1', '三天前跑完、没交付的问题', {
    status: 'running', graph: GRAPH, run_id: 'run-late', created_at: iso(60 * 24 * 3),
  })],
  // 轮次都还是 running，运行早已各有结局：查到之后要落库，不能每次进来都重新核对
  c0termr: [
    turn('tr1', '在别处被取消的那一轮', { status: 'running', graph: GRAPH, run_id: 'run-t-cancel' }),
    turn('tr2', '在别处失败的那一轮', { status: 'running', graph: GRAPH, run_id: 'run-t-failed' }),
    turn('tr3', '服务重启挂起的那一轮', { status: 'running', graph: GRAPH, run_id: 'run-t-susp' }),
    turn('tr4', '运行记录被删掉的那一轮', { status: 'running', graph: GRAPH, run_id: 'run-404-t' }),
    turn('tr5', '还停在审批上的那一轮', { status: 'running', graph: GRAPH, run_id: 'run-t-wait' }),
    turn('tr6', '在别处失败的那一轮（后端现在的说法）', { status: 'running', graph: GRAPH, run_id: 'run-t-failed-now' }),
  ],
  c0clips: [],
  c0clipt: [
    // 上次交付时 GET 运行失败，只拿到事件里截断的那份：meta 记着 partial，刷新后要补全。
    // 多个键拼起来比 2000 长，光看长度是认不出来的
    turn('tt1', '多个成果键的长答案', {
      answer: `${FULL.slice(0, 2000)}\n附注：另一个成果键`, graph: GRAPH, run_id: 'run-multi',
      meta: { v: 1, runId: 'run-multi', runStatus: 'succeeded', clipped: 'partial' },
    }),
    // 恰好 2000 个码点（UTF-16 是 2005）、运行也没了：补不回来，但要标出来
    turn('tt2', '旧版本被截断、运行没了的长答案', { answer: ASTRAL_2000, graph: GRAPH }),
    turn('tt3', '旧版本被截断、运行记录删掉了的长答案', { answer: ASTRAL_2000, graph: GRAPH, run_id: 'run-404-clip' }),
  ],
  c0launu: [],
  c0scopv: [turn('tv1', '上一问', { answer: '上一问的答案', graph: GRAPH, run_id: 'run-b1' })],
  c0scopw: [turn('tw0', '上一问只查了一个库', {
    answer: '上一问的答案 W', graph: GRAPH, run_id: 'run-b1',
    meta: { v: 1, runId: 'run-b1', runStatus: 'succeeded', scope: [{ id: 'ds-plant', name: 'factory' }] },
  })],
  // 两天前问的、刚被重试过：run_id 列还指着上一次的运行（清不掉），meta.runId 是 null。
  // 有没有动静按 meta.at 算——取的那一刻才定，脚本跑得再久也是「2 分钟前」「20 分钟前」
  c0buildx: () => [
    turn('tx7', '两分钟前在别的页面里重试的问题', {
      status: 'running', graph: GRAPH, run_id: 'run-failed', created_at: iso(60 * 24 * 2),
      meta: { v: 1, runId: null, at: Date.now() - 2 * 60_000 },
    }),
    turn('tx8', '重试之后二十分钟没动静的问题', {
      status: 'running', graph: GRAPH, run_id: 'run-failed', created_at: iso(60 * 24 * 2),
      meta: { v: 1, runId: null, at: Date.now() - 20 * 60_000 },
    }),
  ],
  c0trash1: [turn('tx1', '回收站里的问题', { answer: '回收站里的答案', graph: GRAPH, run_id: 'run-b1' })],
  c0trash2: [],
  c0trash3: [
    turn('ty0', '搭好了没跑的问题', { graph: GRAPH }),
    turn('ty1', '停在审批上的问题', { status: 'running', graph: GRAPH, run_id: 'run-trash-wait' }),
    turn('ty2', '跑挂了的问题', {
      status: 'error', error: '查询超时', graph: GRAPH, run_id: 'run-failed',
      meta: { v: 1, runId: 'run-failed', runStatus: 'failed' },
    }),
    turn('ty3', '步数用满、结论不可用的问题', {
      answer: '只查了一半的结论', graph: GRAPH, run_id: 'run-broken',
      review: {
        verdict: 'annotated', note: 'Agent 已用完 12 步上限，仍未给出结论。', answer: null, retry: true, severity: 'broken',
        signals: [{ kind: 'step_limit', detail: 'Agent 已用完 12 步上限，仍未给出结论。请调大节点的「最大步数」；如果大部分步数用于逐张表查询结构，可在提示词中指明要查的表。', severity: 'broken' }],
      },
      meta: { v: 1, runId: 'run-broken', runStatus: 'succeeded' },
    }),
  ],
  c0trash4: [turn('tz1', '删掉之后还在跑的问题', { status: 'running', graph: GRAPH, run_id: 'run-trash-live', created_at: iso(10) })],
  // 要改的是工具库里的参数定义，不在这一轮的流程里：先去改，再接着跑（没记 fix 的老写法，按原话认）
  c0fixy: [turn('ty1', '查一下订单状态', { status: 'error', error: BROKEN_TOOL, graph: GRAPH, run_id: 'run-badtool' })],
  c0fixz: [turn('tfz1', '查一下订单状态', { status: 'error', error: BROKEN_TOOL_NOW, graph: GRAPH, run_id: 'run-badtool-now' })],
  c0rejr: [turn('trj1', '这个月的退款有多少', { status: 'error', error: '已取消', graph: GRAPH, run_id: 'run-rej2',
    meta: { v: 1, runId: 'run-rej2', runStatus: 'cancelled', outcome: 'cancelled', rejected: { sub: '张工 驳回了「查数」：数字对不上' } } })],
  c0rejs: [],
}
/**
 * 成果带逐段证据的一轮（报告撰写节点 + _evidence）。夹具是后端 compose_doc 真跑出来的，只用
 * 通用名。复核回的是一次改写：有证据时复核只能加说明，答案不能被换掉
 */
const EVIDENCE = JSON.parse(readFileSync(new URL('../frontend/src/run/__tests__/evidence-doc.json', import.meta.url), 'utf8'))
const RUNS = {
  'run-evid': { status: 'succeeded', output: EVIDENCE.output, error: null },
  'run-evq': { status: 'succeeded', output: EVQ.output, error: null },
  'run-led': { status: 'succeeded', output: EVIDENCE.output, error: null },
  'run-cancelled': { status: 'cancelled', output: {}, error: null },
  'run-failed': { status: 'failed', output: {}, error: '查询超时' },
  'run-rejected': { status: 'failed', output: {}, error: '人工驳回：数字对不上' },
  'run-auth': { status: 'failed', output: {}, error: 'AuthenticationError: Error code: 401 - invalid x-api-key' },
  'run-broken': { status: 'succeeded', output: { answer: '只查了一半的结论' }, error: null },
  'run-late': { status: 'succeeded', output: { answer: '补交付的答案：三条记录。' }, error: null,
                started_at: iso(60 * 24 * 3 - 1), finished_at: iso(60 * 24 * 3 - 1.5) },
  'run-t-cancel': { status: 'cancelled', output: {}, error: null },
  // 鉴权失败的后端原文（core/errors.py）：run-t-failed 是老说法（库里老运行的 error），run-t-failed-now 是现在的说法
  'run-t-failed': { status: 'failed', output: {}, error: '鉴权没通过（401）：对方拒绝了这把密钥' },
  'run-t-failed-now': { status: 'failed', output: {}, error: '鉴权失败（401）：服务方拒绝了当前 API Key' },
  'run-multi': { status: 'succeeded', output: { answer: FULL, note: '附注：另一个成果键' }, error: null },
  'run-trash-live': { status: 'running', output: {}, error: null },
  'run-badtool': { status: 'failed', output: {}, error: BROKEN_TOOL },
  'run-badtool-now': { status: 'failed', output: {}, error: BROKEN_TOOL_NOW },
  'run-rej2': { status: 'cancelled', end_reason: 'rejected', output: {}, error: '人工驳回：数字对不上' },
}
const SOURCES = [
  { id: 'ds-mig', name: 'shop', kind: 'mysql', description: '' },
  { id: 'ds-plant', name: 'factory', kind: 'oracle', description: '示例工厂库：采购、物料、生产、设备' },
]
/** 别处（另一个标签页、别人）刚加上的库：只有重新取列表才看得到 */
const EXTRA_SOURCE = { id: 'ds-new', name: 'warehouse', kind: 'postgres', description: '' }
const TABLES = {
  'ds-mig': ['__prisma_migrations', 'schema_migrations', 'admin_audit_log', 'app_log', 'test_sales', 'user', 'orders'],
  'ds-plant': ['ANALYTICS.test_sales', 'ANALYTICS.job_etl_audit', 'ANALYTICS.v_demo_table'],
}

// ---------------------------------------------------------------- 伪造的后端

const log = {
  patches: [], cancels: [], continues: [], runStarts: [], archived: [], posts: [], approvalGets: [], runGets: [],
  deleted: [], gens: [], sourceGets: 0, catalogPatches: [], reviews: [],
  /** 问数据的一轮：发起（POST /api/chat/turns 的请求体）、停止、实时流的每一次连接（从哪个 seq 之后） */
  turnStarts: [], turnCancels: [], streams: [],
}
/** 新服务端复核给的重来那句话：前端得原样交回去，不自己拼 */
const DECIDE_RETRY = '（服务端拼好的重来说法）照服务端的决定重新回答：有几家门店'
const ctl = {
  slowMs: 0, failE: true, wsClosed: {}, approvals: [], thinkGo: false, writingGo: false, writingAt: 0,
  /** 列表只给前 8 个：筛选框刚好出现，删一个就不够 8 个了 */
  few: false, run500: true, reviewDelay: {}, launchFail: true, scope400: false,
  /** 目录修改提案第一次保存回 409（别人刚改过这张表） */
  patchConflict: false,
  /** 回收站里多摆两个（TRASH_MORE） */
  trashMore: false,
  /** 列表里多摆「护栏」那段的三个会话（GUARD_CONVS） */
  guardConvs: false,
  /** 列表里多摆「问数据的证据」那段的会话（ASK_CONVS） */
  askConvs: false,
  /** 列表里多摆「助手反问」那段的会话（ASKBACK_CONVS） */
  askBack: false,
  /** 列表里多摆「先去改参数定义」那段的会话（FIX_CONVS） */
  fixConvs: false,
  /** 数据源列表取不回来 */
  sourcesFail: false,
  /** 数据源列表里多一个别处刚加的库 */
  extraSource: false,
  /** 列表里多摆「被驳回」那段的两个会话（REJECT_CONVS） */
  rejectConvs: false,
  /** 列表里多摆「口径账本」那段的会话（LEDGER_CONVS） */
  ledgerConvs: false,
  /** 伪造的服务端：运行的剧本放完了（`${runId}@${after}`）、继续运行过的运行、等复核最多多久（毫秒） */
  wsDone: {}, continued: {}, reviewWaitMs: 25_000,
  /** POST /api/chat/turns 根本没到后端（网断了） */
  startFail: false,
  /** 接下来几次实时流连接直接断掉（测断线重连补齐） */
  dropStreams: 0,
  /** 对话详情里带上服务端写的那几行（测刷新后接着看） */
  liveDetail: false,
  /** 助手每条操作之间隔多久（毫秒） */
  opGapMs: 10,
  /** 列表里多摆「刷新后接着看」那段的会话（REPLAY_CONVS）；那段里建图、运行各停在一道闸上 */
  replayConvs: false, replayBuildGo: false, replayRunGo: false,
  /** 「停止」那段：建图停在一道闸上 */
  stopBuildGo: false,
}
/** 会话的归档 / 删除状态。各段各自 resetDb，免得上一段删掉的会话影响下一段 */
const db = { archived: new Set(), purged: new Set() }
const resetDb = () => {
  db.archived = new Set([...Object.values(TRASHED), ...Object.values(TRASH_MORE)].map((c) => c.id))
  db.purged = new Set()
}
resetDb()
const allConvs = () => [...Object.values(CONVS), ...Object.values(TRASHED), ...(ctl.trashMore ? Object.values(TRASH_MORE) : []),
  ...(ctl.guardConvs ? Object.values(GUARD_CONVS) : []), ...(ctl.askConvs ? Object.values(ASK_CONVS) : []),
  ...(ctl.askBack ? Object.values(ASKBACK_CONVS) : []), ...(ctl.replayConvs ? Object.values(REPLAY_CONVS) : []),
  ...(ctl.fixConvs ? Object.values(FIX_CONVS) : []), ...(ctl.rejectConvs ? Object.values(REJECT_CONVS) : []),
  ...(ctl.ledgerConvs ? Object.values(LEDGER_CONVS) : [])]
  .filter((c) => !db.purged.has(c.id))
  .map((c) => ({ ...c, archived: db.archived.has(c.id) }))
let runSeq = 0

async function fakeApi(route) {
  const req = route.request()
  const url = new URL(req.url())
  const path = url.pathname.replace(/^\/api/, '')
  const method = req.method()
  const json = (body, status = 200) => route.fulfill({ status, json: body })
  const body = () => { try { return req.postDataJSON() } catch { return {} } }

  if (path === '/conversations' && method === 'GET') {
    if (url.searchParams.get('kind') === 'canvas') return json([])
    const rows = allConvs()
    const live = rows.filter((c) => !c.archived)
    const shown = ctl.few ? live.slice(0, 8) : live
    return json(url.searchParams.get('include_archived') === 'true' ? [...shown, ...rows.filter((c) => c.archived)] : shown)
  }
  if (path === '/conversations' && method === 'POST') return json({ ...conv('c0new', '新对话', 0, 0), turns: [] }, 201)
  let m = path.match(/^\/conversations\/([^/]+)$/)
  if (m && method === 'GET') {
    const id = m[1]
    if (id === 'c0slowd') await new Promise((r) => setTimeout(r, ctl.slowMs))
    if (id === 'c0faile' && ctl.failE) return json({ detail: '数据库暂时不可用' }, 500)
    const known = allConvs().find((c) => c.id === id)
    if (!known && id !== 'c0new') return json({ detail: '这个对话不存在，可能已经被删了' }, 404)
    let turns = (typeof DETAIL[id] === 'function' ? DETAIL[id]() : DETAIL[id]) ?? []
    if (ctl.liveDetail) {
      // 服务端写的那几行（刷新、关掉再打开时取回来的就是它们）：夹具里有的换成服务端现在的，新问的接在后面
      const live = [...srv.turns.values()].filter((T) => T.conversation_id === id && T.gen === srv.gen).map(rowOut)
      turns = [...turns.map((t) => live.find((x) => x.id === t.id) ?? t), ...live.filter((x) => !turns.some((t) => t.id === x.id))]
    }
    return json({ ...(known ?? conv('c0new', '新对话', 0, 0)), turns })
  }
  if (m && method === 'PATCH') {
    const b = body()
    log.archived.push([m[1], b])
    if (b.archived === true) db.archived.add(m[1])
    if (b.archived === false) db.archived.delete(m[1])
    return json({ ...allConvs().find((c) => c.id === m[1]), ...b })
  }
  if (m && method === 'DELETE') {
    log.deleted.push(m[1])
    db.purged.add(m[1])
    return route.fulfill({ status: 204, body: '' })
  }
  m = path.match(/^\/conversations\/([^/]+)\/turns$/)
  if (m && method === 'POST') {
    log.posts.push(m[1])
    return json(turn(`srv-${m[1]}-${log.posts.length}`, body().question, { status: 'running' }), 201)
  }
  m = path.match(/^\/conversations\/([^/]+)\/turns\/([^/]+)$/)
  if (m && method === 'PATCH') { log.patches.push({ conv: m[1], turn: m[2], body: body() }); return json(turn(m[2], '', body())) }

  // 问数据的一轮（伪造的服务端，见 startTurn / driveTurn）
  if (path === '/chat/turns' && method === 'POST') {
    const b = body()
    if (ctl.startFail) {
      log.turnStarts.push({ ...b, failed: true })
      return route.abort('connectionrefused')
    }
    const got = await startTurn(b)
    return json(got.json, got.status)
  }
  m = path.match(/^\/chat\/turns\/([^/]+)(\/stream|\/cancel)?$/)
  if (m) {
    const T = srv.turns.get(decodeURIComponent(m[1]))
    if (!T) return json({ detail: '该轮对话不存在，可能已被删除' }, 404)
    if (m[2] === '/stream' && method === 'GET') return streamTurn(route, T, Number(url.searchParams.get('after') ?? 0))
    if (m[2] === '/cancel' && method === 'POST') {
      log.turnCancels.push(T.id)
      return cancelTurn(T) ? json({ ok: true }) : json({ detail: '这一轮已经结束，无需停止', code: 'chat_turn_finished' }, 409)
    }
    if (!m[2] && method === 'GET') return json({ id: T.id, conversation_id: T.conversation_id, job: { ...T.job }, turn: rowOut(T) })
  }
  if (path === '/copilot/generate-stream' && method === 'POST') {
    // 画布助手照旧走这条流（问数据的一轮在伪造的服务端里走同一份剧本，见 copilotScript）
    const b = body()
    log.gens.push(b)
    const got = copilotScript(b)
    if (got.status) return json(got.json, got.status)
    return route.fulfill({ status: 200, contentType: 'text/event-stream',
      body: got.ops.map((o) => `data: ${JSON.stringify(o)}\n\n`).join('') })
  }
  if (path === '/datasources/src-shop/catalog/orders/patch' && method === 'POST') {
    log.catalogPatches.push(body())
    if (ctl.patchConflict) {
      ctl.patchConflict = false
      return json({ detail: '表「orders」的数据目录刚被修改过，请重新载入后再提交' }, 409)
    }
    return json({ table_name: 'orders', in_schema: true, notes: {}, version: 4, updated_at: null, updated_by: null,
      structure: null, system_notes: false, usage: 0 })
  }
  if (path === '/datasources/src-shop/catalog/orders/patch/preview' && method === 'POST') {
    log.catalogPatches.push({ preview: body() })
    return json({ table_name: 'orders', version: 3, problems: [], changes: [
      { path: 'columns.status.codes', before: { 1: '已支付', 2: '已退款' }, before_status: 'confirmed',
        after: { 1: '已支付', 2: '已退款', 9: '作废' }, value: { 9: '作废' }, reason: '用户说明 status=9 表示作废', state: 'change' }] })
  }
  if (path === '/datasources/src-shop/catalog/orders/impact' && method === 'GET') {
    return json({ table: 'orders', templates: [] })
  }
  if (path === '/copilot/review' && method === 'POST') return json(await reviewScript(body()))
  if (path === '/runs' && method === 'POST') {
    const got = launchScript(body())
    if (got.abort) return route.abort('connectionrefused')
    if (got.status) return json(got.json, got.status)
    return json({ id: got.id, status: 'queued', run_class: 'exploratory', output: {}, usage: {} })
  }
  m = path.match(/^\/runs\/([^/]+)\/(cancel|continue)$/)
  if (m && method === 'POST') {
    if (m[2] === 'cancel') { log.cancels.push(m[1]); return json({ ok: true }) }
    log.continues.push(m[1])
    if (m[1] === 'run-restart' || m[1] === 'run-susp') return json({ id: m[1], status: 'running' })
    return json({ detail: '这次运行已完成，无需继续运行：只有失败的运行或被服务重启打断的运行可以继续运行。如需再次运行，请重新发起。' }, 409)
  }
  m = path.match(/^\/runs\/([^/]+)$/)
  if (m && method === 'GET') {
    log.runGets.push(m[1])
    const got = runRecord(m[1])
    return json(got.json, got.status)
  }
  if (path.startsWith('/approvals')) {
    log.approvalGets.push(Date.now())
    const runId = url.searchParams.get('run_id')
    return json(ctl.approvals.filter((a) => !runId || a.run_id === runId))
  }
  m = path.match(/^\/runs\/([^/]+)\/events$/)
  if (m && method === 'GET' && m[1] === 'run-rej2') {
    // 被驳回的那一轮：「查数」里的动作确认被驳回，那一步的 node.failed 带 reason=rejected（服务端 compiler 的写法）
    return json([ev(1, 'run.started', null, { nodes: 3 }), ev(2, 'node.started', 'ag', { node_type: 'agent', label: '查数' }),
      ev(3, 'node.failed', 'ag', { duration_ms: 900, error: '人工驳回：数字对不上', reason: 'rejected', kind: 'action',
        decision: 'reject', actor: '张工', note: '数字对不上' }),
      ev(4, 'run.cancelled', null, { reason: 'rejected', node_id: 'ag', label: '查数', kind: 'action', decision: 'reject',
        actor: '张工', note: '数字对不上' })])
  }
  if (m && method === 'GET') {
    return json([ev(1, 'run.started', null, { nodes: 3 }), ev(2, 'node.started', 'ag', {}),
      ev(3, 'node.finished', 'ag', { duration_ms: 700 }), ev(4, 'run.finished', null, { output: {} })])
  }
  if (path === '/datasources' && method === 'GET') {
    log.sourceGets++
    return ctl.sourcesFail ? json({ detail: '数据源列表暂时取不到' }, 500)
      : json(ctl.extraSource ? [...SOURCES, EXTRA_SOURCE] : SOURCES)
  }
  m = path.match(/^\/datasources\/([^/]+)\/schema$/)
  if (m) return json({ tables: TABLES[m[1]] ?? [] })

  if (method === 'GET') return route.continue()
  return route.abort()   // 其余写操作一律不放
}

/**
 * 复核的剧本（服务端 api/copilot.review_of_run 的结果），按运行 id 挑；有的故意慢（reviewDelay、lateReview）。问数据的一轮由伪造的
 * 服务端（fakeTurns）调它，/copilot/review 照旧答它
 */
async function reviewScript(b) {
  const wait = ctl.reviewDelay[b.run_id]
  if (wait) await new Promise((r) => setTimeout(r, wait))
  if (ctl.lateReview && String(b.run_id).startsWith('run-decide')) {
    // 复核慢过问数据页等的上限（检查里调短成几百毫秒）：答案先交出去，这一次复核的用量晚到了照样要记进这一轮
    await new Promise((r) => setTimeout(r, ctl.lateReview))
    log.reviews.push(b)
    return { verdict: 'ok', note: '', answer: null, retry: false, severity: '', signals: [], retry_instruction: null,
                  usage: { cost_usd: 0.004, unpriced_calls: 0, calls: 1 } }
  }
  if (String(b.run_id).startsWith('run-decide')) {
    // 新服务端：第一次判不可用、给出重来的那句话；重来之后的那次没问题、不再重来。各带复核的用量
    log.reviews.push(b)
    const first = b.attempt === 0
    return { verdict: first ? 'annotated' : 'ok', note: first ? '查到一半就停了' : '', answer: null, retry: first,
                  severity: first ? 'broken' : '', signals: first ? [{ kind: 'step_limit', detail: '用满了步数', severity: 'broken' }] : [],
                  retry_instruction: first ? DECIDE_RETRY : null,
                  usage: { cost_usd: first ? 0.003 : 0, unpriced_calls: 0, calls: first ? 1 : 0 } }
  }
  // 老后端的复核不认 _evidence，照样回一次改写
  if (b.run_id === 'run-evid' || b.run_id === 'run-evq') {
    return { verdict: 'rewritten', note: '检索降级过，结论请对照原始数据', answer: '改写后的答案：销售额大约四万多',
                  original: b.run_id === 'run-evq' ? EVQ.output.answer : EVIDENCE.output['周报'], retry: false, severity: 'degraded',
                  signals: [{ kind: 'retrieval_degraded', detail: '检索退回关键词', severity: 'degraded' }] }
  }
  return { verdict: 'ok', note: '', answer: null, retry: false, severity: '', signals: [] }
}

/** 发起一次运行（服务端 engine/launch.launch_run）：按问题挑这次运行的 id，记下发起时的图和来源。被拒的给 {status, json} */
function launchScript(b) {
  const conversation = String(b.input?.question ?? '')
  // 发起请求根本没到后端（网断了、后端正在重启）
  if (conversation.includes('启动失败') && ctl.launchFail) return { abort: true }
  // 发起就被拒：绑定的工具不存在（后端 runs.TOOL_MISSING，{detail, code} 的形状）
  if (conversation.includes('工具不在')) {
    return { status: 422, json: { detail: '绑定的工具不存在：「查数」（调用工具）绑定的 db_query__nope。请到「数据」页接入，或在节点中重新选择',
      code: 'run_tool_missing' } }
  }
  const id = conversation.includes('整形解析失败') ? (conversation.includes('现在的说法')
    ? (conversation.includes('上游') ? 'run-jsonup-now' : 'run-jsonerr-now')
    : conversation.includes('节点名带超时') ? 'run-jsonslow'
    : conversation.includes('上游') ? 'run-jsonup'
    : conversation.includes('改写') ? 'run-jsonauth' : 'run-jsonerr')
    : conversation.includes('问数据带证据') ? 'run-evq'
    : conversation.includes('带证据') ? 'run-evid'
    : conversation.includes('长答案') ? (conversation.includes('取不到运行') ? 'run-noget' : 'run-long')
    : conversation.includes('刷新后接着看') ? 'run-replay'
    : conversation.includes('审批') ? 'run-wait'
    : conversation.includes('重启') ? 'run-restart'
    : conversation.includes('正在写') ? 'run-writing'
    : conversation.includes('强杀') ? 'run-killed'
    : conversation.includes('步数用满') ? `run-steps-${++runSeq}`
    : conversation.includes('照服务端的决定') ? `run-decide-${++runSeq}`
    : `run-busy-${++runSeq}`
  log.runStarts.push({ id, graph: b.graph, origin_ref: b.origin_ref })
  return { id }
}

/** 库里的运行（GET /api/runs/{id}）：{status, json} */
function runRecord(id) {
  if (id === 'run-long') return { status: 200, json: { id, status: 'succeeded', run_class: 'exploratory', output: { answer: FULL }, usage: {} } }
  if (id === 'run-noget' || (id === 'run-unk' && ctl.run500)) return { status: 500, json: { detail: '服务端错误（500），详情请查看服务日志' } }
  if (id === 'run-unk') return { status: 200, json: { id, status: 'cancelled', run_class: 'exploratory', output: {}, usage: {} } }
  if (id.startsWith('run-404')) return { status: 404, json: { detail: '运行不存在' } }
  if (RUNS[id]) return { status: 200, json: { id, run_class: 'exploratory', usage: {}, ...RUNS[id] } }
  if (id.startsWith('run-c') || id === 'run-b1') return { status: 200, json: { id, status: 'succeeded', run_class: 'exploratory', output: {}, usage: {} } }
  if (id.startsWith('run-decide')) {
    return { status: 200, json: { id, status: 'succeeded', run_class: 'exploratory', output: { answer: '三家门店。' },
                  usage: { cost_usd: 0.02, calls: 2, input_tokens: 6000, output_tokens: 400 } } }
  }
  return { status: 200, json: { id, status: 'interrupted', run_class: 'exploratory', output: {}, usage: {} } }
}

/**
 * 助手生成过程的剧本（服务端 api/copilot.answer_ops 交出的那一串操作），按问法挑。开流之前就被拒的给 {status, json}。
 * 画布助手经 /copilot/generate-stream 收它；问数据的一轮由伪造的服务端（fakeTurns）照它跑
 */
function copilotScript(b) {
  if (ctl.scope400 && b.datasource_ids?.length) {
    // 和后端 copilot._sources 同一个形状：detail 是一句话，code 是稳定的机读码
    // reworded：后端换了说法，只剩机读码认得出来
    return { status: 400, json: { detail: ctl.scope400 === 'reworded' ? '这一轮圈定的库一个也找不到了：可能已经被删掉或停用'
      : '限定的数据源都已不可用，可能已被删除或停用。请取消限定后重试，或到「数据」页确认数据源存在且已启用', code: 'datasource_scope_empty' } }
  }
  if (String(b.instruction ?? '').includes('建流程就失败')) {
    // 后端的 error 操作是三段：人话、怎么办、原始异常
    const ops = [
      { op: 'heartbeat', phase: 'planning', elapsed_ms: 400 },
      { op: 'error', message: '模型返回的工作流结构有误：第 2 个节点的类型「sql」不存在',
        hint: '请重试，或把需求描述得更具体',
        detail: "2 validation errors for GraphSpec\nnodes.1.type\n  Input should be 'input', 'output', 'llm' [type=literal_error]" },
    ]
    return { ops }
  }
  if (String(b.instruction ?? '').includes('自查没修好')) {
    // 服务端自查交回模型改过、还是没过（check failed），final 带着同一批问题：前端不该再整轮重来
    const ops = [
      { op: 'heartbeat', phase: 'planning', elapsed_ms: 300 },
      ...GRAPH.nodes.map((n) => ({ op: 'add_node', node: n })),
      { op: 'check', status: 'failed', issues: ['「查数」节点还没有选择工具'] },
      { op: 'final', graph: GRAPH, explanation: '', issues: [{ level: 'error', node_id: 'ag', message: '「查数」节点还没有选择工具' }] },
    ]
    return { ops }
  }
  if (String(b.instruction ?? '').includes('只写了思考')) {
    // 生成过程一个操作都没给（deepseek 偶发只出思考）：服务端的 error 带 next=retry（backend/app/chat/turn.py 的 next_step）。
    // 重来那一次（指令里缀了「上一次没有成功」）照常直接回答；「两次都」那一问每次都只写思考
    const again = String(b.instruction).includes('上一次没有成功') && !String(b.instruction).includes('两次都')
    const spent = { by_model: { m: { input_tokens: 500, output_tokens: 40, calls: 1, cost_usd: 0.001, unpriced_calls: 0 } },
      input_tokens: 500, output_tokens: 40, calls: 1, cost_usd: 0.001, unpriced_calls: 0 }
    const ops = again ? [
      { op: 'heartbeat', phase: 'planning', elapsed_ms: 200 },
      { op: 'reply', text: '已经查过了：一共 3 家门店。' },
      { op: 'usage', ...spent },
    ] : [
      { op: 'heartbeat', phase: 'planning', elapsed_ms: 200 },
      { op: 'thinking', delta: '先想一想要查哪张表……' },
      { op: 'error', message: '助手本轮未完成：模型没有给出任何修改，也没有说明原因', hint: '画布未修改。可以重试，或把需求描述得更具体',
        detail: '', reason: '模型没有给出任何修改，也没有说明原因', code: 'copilot_empty', next: 'retry' },
      { op: 'usage', ...spent },
    ]
    return { ops }
  }
  if (String(b.instruction ?? '').includes('照服务端的决定') || String(b.instruction ?? '').includes(DECIDE_RETRY)) {
    // 新服务端：final 带 next（backend/app/chat/turn.py），最后一条是这一轮建图的用量（answer_ops 的 usage）。
    // 「只建不跑」那一问：autorun 没写 false，next 却是 ready——前端得照 next 走
    const ready = String(b.instruction ?? '').includes('只建不跑')
    const ops = [
      { op: 'heartbeat', phase: 'planning', elapsed_ms: 300 },
      ...GRAPH.nodes.map((n) => ({ op: 'add_node', node: n })),
      { op: 'done', explanation: '一张三步的图' },
      { op: 'check', status: 'passed', repaired: 0, warnings: [] },
      { op: 'final', graph: GRAPH, issues: [], explanation: '一张三步的图', autorun: true, next: ready ? 'ready' : 'run' },
      { op: 'usage', by_model: { m: { input_tokens: 4000, output_tokens: 300, calls: 1, cost_usd: 0.012, unpriced_calls: 0 } },
        input_tokens: 4000, output_tokens: 300, calls: 1, cost_usd: 0.012, unpriced_calls: 0 },
    ]
    return { ops }
  }
  if (/刷新后接着看|停止测试/.test(String(b.instruction ?? ''))) {
    // 建到一半停在闸上：刷新、关掉再打开、停止都要在建图中途发生
    const gate = String(b.instruction).includes('停止测试') ? 'stopBuildGo' : 'replayBuildGo'
    const ops = [
      { op: 'heartbeat', phase: 'planning', elapsed_ms: 300 },
      { op: 'plan', summary: '查一下再回答' },
      ...GRAPH.nodes.slice(0, 2).map((n) => ({ op: 'add_node', node: n })),
      { gate },
      { op: 'add_node', node: GRAPH.nodes[2] },
      { op: 'done', explanation: '一张三步的图' },
      { op: 'final', graph: GRAPH, issues: [], explanation: '一张三步的图', autorun: true, next: 'run' },
    ]
    return { ops }
  }
  if (String(b.instruction ?? '').includes('先问清楚')) {
    // 助手要先问清楚才能动手（服务端 copilot_gen 的 ask）：问题转出去，这一轮到此为止——没有 final，也没有 reply
    const ops = [
      { op: 'heartbeat', phase: 'planning', elapsed_ms: 300 },
      { op: 'ask', question: '销售额按下单日还是支付日算？' },
    ]
    return { ops }
  }
  if (String(b.instruction ?? '').includes('表示作废')) {
    // 用户说了一条数据事实：服务端核对后转出目录修改提案，只回答、不建图
    const ops = [
      { op: 'heartbeat', phase: 'planning', elapsed_ms: 300 },
      { op: 'catalog_patch', source: 'shop', source_id: 'src-shop', table: 'orders', table_label: '订单', version: 2, changes: [
        { path: 'columns.status.codes', before: { 1: '已支付' }, before_status: 'confirmed', after: { 1: '已支付', 9: '作废' },
          value: { 9: '作废' }, reason: '用户说明 status=9 表示作废', state: 'change' }] },
      { op: 'reply', text: '已整理成数据目录的修改建议，确认后保存即可。' },
    ]
    return { ops }
  }
  const ops = [
    { op: 'heartbeat', phase: 'planning', elapsed_ms: 800 },
    { op: 'heartbeat', phase: 'looking', elapsed_ms: 1000 },
    // 助手按需查看了表（look 事件）：过程里多一行「查看了 N 张表」，查看记录存进这一轮的 meta.looks。
    // 这个库还没有数据目录（catalog_tables: 0）：那一行下面提示先去起草目录
    { op: 'look', id: 'L1', by: 'model', need: '查订单和明细', status: 'ok', elapsed_ms: 1200, objects: [
      { kind: 'table', source: 'shop', source_id: 'src-shop', named: [], picked: ['orders', 'order_items'], joined: [],
        missing: [], total: 48, catalog_tables: 0 }] },
    { op: 'plan', summary: '查一下再回答' },
    ...GRAPH.nodes.map((n) => ({ op: 'add_node', node: n })),
    { op: 'done', explanation: '一张三步的图' },
    { op: 'final', graph: GRAPH, issues: [], explanation: '一张三步的图' },
  ]
  return { ops }
}

/** 每条运行的事件剧本。done=true 时剧本放完就发 stream.end */
const ev = (seq, type, node_id, data = {}) => ({ seq, type, node_id, data, ts: Date.now() / 1000 })
function script(runId, after = 0) {
  if (runId === 'run-noget') {
    // 事件里的成果被切过，交付时 GET 运行又失败了：只能先交这一份，并且要记下来
    return {
      events: [
        ev(1, 'run.started', null, { nodes: 3 }),
        ev(2, 'node.finished', 'ag', { duration_ms: 900 }),
        ev(3, 'run.finished', null, { output: { answer: FULL.slice(0, 2000), note: '附注' }, output_truncated: true }),
      ],
      end: 'succeeded',
    }
  }
  if (runId === 'run-jsonerr' || runId === 'run-jsonup' || runId === 'run-jsonauth' || runId === 'run-jsonslow'
    || runId === 'run-jsonerr-now' || runId === 'run-jsonup-now') {
    // 用户真实踩到的：整形节点按 JSON 解析上游的文字失败。旧说法（让人查模板）和新说法（点名上游）各一种；
    // 对照：说法被改写过的（鉴权失败）原话还得留着
    const error = runId === 'run-jsonerr' ? JSON_TEMPLATE_ERROR : runId === 'run-jsonup' ? JSON_UPSTREAM_ERROR
      : runId === 'run-jsonerr-now' ? JSON_TEMPLATE_ERROR_NOW : runId === 'run-jsonup-now' ? JSON_UPSTREAM_ERROR_NOW
      : runId === 'run-jsonslow' ? JSON_SLOW_ERROR
      : 'AuthenticationError: Error code: 401 - invalid x-api-key'
    const detail = "JSONDecodeError: Expecting ',' delimiter: line 1 column 934 (char 933)\n出错位置前后的原文：…\"gmv\": 45678.5, \"note\": \"含\"⟨此处⟩促销\"…"
    return {
      events: [
        ev(1, 'run.started', null, { nodes: 3 }),
        ev(2, 'node.started', 'ag', { node_type: 'agent', label: runId === 'run-jsonslow' ? '查询超时订单' : '查数' }),
        ev(3, 'node.finished', 'ag', { duration_ms: 900 }),
        ev(4, 'node.started', 'tf', { node_type: 'transform', label: '解析取数结果为变量' }),
        ev(5, 'node.failed', 'tf', { error, detail, duration_ms: 3 }),
        ev(6, 'run.failed', null, { error, detail }),
      ],
      end: 'failed',
    }
  }
  if (runId === 'run-evq') {
    // 问数据：agent 查库（新运行的 tool.end 多一个 query_artifact）→ 按出处抽取字段 → 报告核对 → 成果
    return {
      events: [
        ev(1, 'run.started', null, { nodes: 4 }),
        ev(2, 'node.started', 'ag', { node_type: 'agent', label: '查数' }),
        ev(3, 'llm.start', 'ag', { model: 'm' }),
        ev(4, 'tool.start', 'ag', { tool: 'db_query__shop', call_id: 'q1', args: { sql: 'SELECT region, SUM(amount) AS amount FROM orders GROUP BY region' } }),
        ev(5, 'tool.end', 'ag', { tool: 'db_query__shop', call_id: 'q1', duration_ms: 30, artifact: 'tool-snap', query_artifact: 'query-snap', rows: 4 }),
        ev(6, 'llm.end', 'ag', { model: 'm', duration_ms: 800 }),
        ev(7, 'llm.start', 'ag', { model: 'm', structured: true, purpose: 'cite_fields', message_count: 6 }),
        ev(8, 'llm.end', 'ag', { agent: '查数', model: 'm', purpose: 'cite_fields', duration_ms: 900 }),
        ev(9, 'log', 'ag', { level: 'warn', code: 'agent_field_mismatch', fields: ['order_cnt'],
          message: '有 1 个字段与查询快照不一致，已按快照取值：order_cnt 模型给出 1240，快照为 1234' }),
        ev(10, 'node.finished', 'ag', { duration_ms: 2100 }),
        ev(11, 'node.started', 'write', { node_type: 'report', label: '写答案' }),
        ev(12, 'report.checked', 'write', EVQ.report_checked),
        ev(13, 'node.finished', 'write', { duration_ms: 900 }),
        ev(14, 'run.finished', null, { output: EVQ.output }),
      ],
      end: 'succeeded',
    }
  }
  if (runId === 'run-evid') {
    return {
      events: [
        ev(1, 'run.started', null, { nodes: 3 }),
        ev(2, 'node.started', 'write', { node_type: 'report', label: '写周报' }),
        ev(3, 'report.checked', 'write', EVIDENCE.report_checked),
        ev(4, 'node.finished', 'write', { duration_ms: 900 }),
        ev(5, 'run.finished', null, { output: EVIDENCE.output }),
      ],
      end: 'succeeded',
    }
  }
  if (runId === 'run-long') {
    return {
      events: [
        ev(1, 'run.started', null, { nodes: 3 }),
        ev(2, 'node.started', 'ag', {}),
        ...Array.from({ length: 60 }, (_, i) => ev(3 + i, 'llm.token', 'ag', { delta: '字' })),
        ev(70, 'node.finished', 'ag', { duration_ms: 1200 }),
        ev(71, 'run.finished', null, { output: { answer: FULL.slice(0, 2000) }, output_truncated: true }),
      ],
      end: 'succeeded',
    }
  }
  if (runId === 'run-replay') {
    // 跑到一半停在闸上：关掉再打开时运行还在跑
    return {
      events: [
        ev(1, 'run.started', null, { nodes: 3 }),
        ev(2, 'node.started', 'ag', { node_type: 'agent', label: '查数' }),
        { gate: 'replayRunGo' },
        ev(3, 'node.finished', 'ag', { duration_ms: 900 }),
        ev(4, 'run.finished', null, { output: { answer: '刷新之后接着跑完了：两家门店。' } }),
      ],
      end: 'succeeded',
    }
  }
  if (runId === 'run-restart' && after >= 4) {
    // 接着跑：从断点往下，这次跑完
    return {
      events: [
        ev(5, 'run.resumed', null, { from: 'ag', message: '从「查数」继续运行' }),
        ev(6, 'node.started', 'ag', { resumed: true }),
        ev(7, 'tool.start', 'ag', { tool: 'db_query', call_id: 'q2', args: { sql: 'select 1' } }),
        ev(8, 'tool.end', 'ag', { tool: 'db_query', call_id: 'q2', rows: 3 }),
        ev(9, 'node.finished', 'ag', { duration_ms: 800 }),
        ev(10, 'run.finished', null, { output: { answer: '接着跑完了：三条记录。' } }),
      ],
      end: 'succeeded',
    }
  }
  if (runId === 'run-killed') {
    // 进程被强杀：连结束标记都来不及发，连接直接断了。前端自己重连，
    // 重启后的后端回放完补一条 stream.end status=interrupted
    if (after >= 2) return { events: [], end: 'interrupted' }
    return { events: [ev(1, 'run.started', null, { nodes: 3 }), ev(2, 'node.started', 'ag', {})], end: 'close' }
  }
  if (runId === 'run-restart') {
    return {
      events: [
        ev(1, 'run.started', null, { nodes: 3 }),
        ev(2, 'node.started', 'ag', {}),
        ev(3, 'tool.start', 'ag', { tool: 'db_query', call_id: 'q1', args: { sql: 'select 1' } }),
        ev(4, 'log', null, { level: 'warn', code: 'server_shutdown', message: '服务正在关停' }),
      ],
      end: 'interrupted',
    }
  }
  if (runId === 'run-wait') {
    // 停在审批上：事件流不关，后端也不发 stream.end
    return {
      events: [
        ev(1, 'run.started', null, { nodes: 3 }),
        ev(2, 'node.started', 'ag', {}),
        ev(3, 'human.requested', 'ag', { interrupt_id: 'ap-1', payload: { title: '确认一下再查' } }),
        ev(4, 'run.interrupted', null, { node_id: 'ag' }),
      ],
      end: null,
    }
  }
  if (runId === 'run-writing') {
    // 先想、再写，写完不收尾：一次还在出字的模型调用。gate 处等脚本放行
    // 第一段停在半句上：半句不该出现在头部，要等它写完
    const tokens = [...Array.from({ length: 12 }, () => '字'.repeat(100)), '字'.repeat(34)]   // 1,234 字
    return {
      events: [
        ev(1, 'run.started', null, { nodes: 3 }),
        ev(2, 'node.started', 'ag', {}),
        ev(3, 'llm.start', 'ag', { model: 'm' }),
        ev(4, 'llm.thinking.delta', 'ag', { delta: '先看看 factory 里有哪些表。' }),
        ev(5, 'llm.thinking.delta', 'ag', { delta: '然后按月汇总出勤' }),
        { gate: 'thinkGo' },
        ev(6, 'llm.thinking.delta', 'ag', { delta: '率，再和上个月比。' }),
        { gate: 'writingGo' },
        ...tokens.map((delta, i) => ev(10 + i, 'llm.token', 'ag', { delta })),
      ],
      end: null,
    }
  }
  if (runId.startsWith('run-decide')) {
    return {
      events: [
        ev(1, 'run.started', null, { nodes: 3 }),
        ev(2, 'node.started', 'ag', {}),
        ev(3, 'node.finished', 'ag', { duration_ms: 900 }),
        ev(4, 'run.finished', null, { output: { answer: '三家门店。' } }),
      ],
      end: 'succeeded',
    }
  }
  if (runId.startsWith('run-steps')) {
    return {
      events: [
        ev(1, 'run.started', null, { nodes: 3 }),
        ev(2, 'node.started', 'ag', {}),
        ev(3, 'node.finished', 'ag', { duration_ms: 900 }),
        ev(4, 'run.finished', null, { output: { answer: '放宽步数之后查全了' } }),
      ],
      end: 'succeeded',
    }
  }
  // 忙的那条：开了头就停住，流不关，像一次还在跑的运行
  return { events: [ev(1, 'run.started', null, { nodes: 3 }), ev(2, 'node.started', 'ag', {})], end: null }
}

async function fakeStream(ws) {
  const runId = new URL(ws.url()).pathname.split('/')[3]
  ctl.wsClosed[runId] = false
  let closed = false
  ws.onClose(() => { closed = true; ctl.wsClosed[runId] = true })
  const after = Number(new URL(ws.url()).searchParams.get('after') ?? 0)
  const { events, end } = script(runId, after)
  for (const e of events) {
    if (e.gate) {
      await until(() => ctl[e.gate], 15000)
      continue
    }
    if (e.seq <= after) continue
    if (closed) return
    if (runId === 'run-writing' && e.type === 'llm.token' && !ctl.writingAt) ctl.writingAt = Date.now()
    // 审批和中断同时产生：后端在停下的那一刻才建出这条待审批
    if (runId === 'run-wait' && e.type === 'run.interrupted') {
      ctl.approvals = [ctl.pendingApproval]
      ctl.interruptedAt = Date.now()
    }
    ws.send(JSON.stringify(e))
    await new Promise((r) => setTimeout(r, 15))
  }
  if (closed) return
  if (end === 'close') ws.close()
  else if (end) ws.send(JSON.stringify({ type: 'stream.end', status: end, data: { status: end } }))
  // 剧本放完了：伪造的服务端据此认这次运行到了终态（真服务端按库里的状态，事件先落库）
  if (end) ctl.wsDone[`${runId}@${after}`] = true
}

// ---------------------------------------------------------------- 伪造的服务端一轮

/**
 * 问数据的一轮在服务端跑（backend/app/chat/job.py；接口 backend/app/api/chat_turns.py）。页面只发起（POST /api/chat/turns）、
 * 只看（GET /api/chat/turns/{id}/stream 先回放、再推实时，断了按 seq 重连），这里照服务端的做法把一轮跑完：
 *   生成（copilotScript 的剧本，原样记进 log.gens）→ final.next → 发起运行（launchScript，记进 log.runStarts）→ 等运行的剧本放完
 *   （fakeStream）→ 复核（reviewScript，最多等 ctl.reviewWaitMs，晚到的照记用量）→ 按复核重来一次 → 照服务端的写法拼那一行（end）。
 * 实时流是长轮询：没有新事件就等一会儿再答，答完连接就关；页面按收到的最后一个 seq 马上接着连（和服务端断线时一样）。
 */
const srv = { turns: new Map(), seq: 0, launched: new Set(), gen: 0 }
const ACTIVE_JOB = new Set(['queued', 'running'])
const turnActive = (T) => ACTIVE_JOB.has(T.job.status) || !!T.late
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const BUSY_TEXT = '这段对话正有一轮在进行：等它结束或先停止，再提问'

function emit(T, type, data = {}) {
  const e = { seq: T.events.length + 1, type, data, ts: Date.now() / 1000 }
  T.events.push(e)
  T.job.last_seq = e.seq
  return e.seq
}

/** 那一行（GET /api/conversations/{id} 里的一轮、end 里带的 turn） */
const rowOut = (T) => ({ ...T.row, meta: T.row.meta ? { ...T.row.meta } : T.row.meta, job: { ...T.job } })

const addTotal = (a, b) => {
  const out = { ...(a ?? {}) }
  for (const k of ['cost_usd', 'unpriced_calls', 'calls', 'unfinished_calls', 'input_tokens', 'output_tokens']) {
    const v = Number(b?.[k] ?? 0)
    if (Number.isFinite(v) && v) out[k] = Math.round((Number(out[k] ?? 0) + v) * 1e6) / 1e6
  }
  return out
}
const retireRun = (usage) => {
  const { run, ...rest } = usage ?? {}
  return run && Object.keys(run).length ? { ...rest, earlier: addTotal(rest.earlier, run) } : rest
}
const answerText = (output) => Object.entries(output ?? {})
  .filter(([k, v]) => !k.startsWith('_') && v != null && v !== '').map(([, v]) => (typeof v === 'string' ? v : JSON.stringify(v))).join('\n')

/** 用户点重试：上一次的结局归档（同服务端 chat/record.attempt_record） */
function attemptRecord(row, n) {
  const meta = row.meta ?? {}
  const review = row.review ?? {}
  const base = { n, at: Date.now(), runId: meta.runId !== undefined ? meta.runId : row.run_id ?? null,
                 ...(row.answer ? { answer: row.answer.length > 4000 ? `${row.answer.slice(0, 4000)}…` : row.answer } : {}) }
  const signals = (review.signals ?? []).map((x) => x.detail).join('\n') || undefined
  if (meta.outcome === 'cancelled' && meta.rejected) {
    const sub = meta.rejected.sub ?? ''
    return { ...base, outcome: 'rejected', summary: `被驳回${sub ? `：${sub}` : ''}` }
  }
  if (meta.outcome === 'cancelled' || (row.status === 'error' && row.error === '已取消')) return { ...base, outcome: 'cancelled', summary: '已手动停止' }
  if (meta.outcome === 'suspended') return { ...base, outcome: 'suspended', summary: '服务重启，运行中断' }
  if (row.status === 'error') {
    const f = meta.failure
    return { ...base, outcome: 'failed', summary: f ? (f.reason ? `${f.title}：${f.reason}` : f.title) : row.error || '发生错误',
             ...(meta.cause ? { cause: meta.cause } : {}) }
  }
  if (review.severity === 'broken') return { ...base, outcome: 'unusable', summary: `结论不可用：${review.note ?? ''}`, detail: signals }
  if (review.severity === 'degraded') return { ...base, outcome: 'partial', summary: `存在缺口：${review.note ?? ''}`, detail: signals }
  return { ...base, outcome: 'done', summary: '已给出答案' }
}
const attemptReason = (a) => (a.detail ? `${a.summary}（${String(a.detail).slice(0, 300)}）` : a.summary)

/** 种一轮：库里已经有的（夹具里的那一行），或者新问的 */
function seedTurn(conversationId, id, row) {
  const T = {
    id, conversation_id: conversationId, row: { ...row, meta: row.meta ? { ...row.meta } : null },
    job: { status: 'succeeded', stage: null, mode: 'ask', run_id: null, attempt: 0, last_seq: 0, attempt_seq: 0, error: null },
    events: [], cancelled: false, late: null, runId: null, usage: {}, looks: [],
  }
  srv.turns.set(id, T)
  return T
}
function knownRow(conversationId, turnId) {
  const rows = typeof DETAIL[conversationId] === 'function' ? DETAIL[conversationId]() : DETAIL[conversationId] ?? []
  return rows.find((t) => t.id === turnId) ?? null
}
const scopeOf = (ids) => (ids ?? []).map((id) => {
  const hit = [...SOURCES, EXTRA_SOURCE].find((x) => x.id === id || x.name === id)
  return hit ? { id: hit.id, name: hit.name } : { id, name: id }
})

/** 这一轮还在跑时每一步之间看一眼：停了、这一段检查结束了，就不再往下写 */
const gone = (T, epoch) => T.cancelled || T.epoch !== epoch || T.gen !== srv.gen

async function startTurn(b) {
  const mode = b.mode ?? (b.retry_of ? 'retry' : 'ask')
  let T
  if (mode === 'ask') {
    log.posts.push(b.conversation_id)
    const id = `srv-${b.conversation_id}-${++srv.seq}`
    const scope = scopeOf(b.datasource_ids)
    T = seedTurn(b.conversation_id, id, turn(id, b.question, { status: 'running', meta: { v: 1, ...(scope.length ? { scope } : {}) } }))
  } else {
    T = srv.turns.get(b.retry_of)
    // 夹具里的那一行：每一段检查从库里原样的那一行起（上一段、上一套主题对它做过什么都不算）
    const row = !T || T.gen !== srv.gen ? knownRow(b.conversation_id, b.retry_of) : null
    if (row) T = seedTurn(b.conversation_id, b.retry_of, row)
    if (!T) return { status: 404, json: { detail: '该轮对话不存在，可能已被删除' } }
  }
  const others = [...srv.turns.values()].filter((x) => x !== T && x.conversation_id === b.conversation_id
    && ACTIVE_JOB.has(x.job.status) && x.job.stage !== 'waiting' && x.gen === srv.gen)
  if (mode !== 'resume' && others.length) return { status: 409, json: { detail: BUSY_TEXT, code: 'chat_turn_busy' } }
  if (mode !== 'ask' && ACTIVE_JOB.has(T.job.status)) return { status: 409, json: { detail: BUSY_TEXT, code: 'chat_turn_busy' } }
  const meta = { ...(T.row.meta ?? {}) }
  const runId = meta.runId !== undefined ? meta.runId : T.row.run_id ?? null
  const now = Date.now()
  const request = { mode, started_ms: now }
  let attempts = [...(meta.attempts ?? [])]
  const scope = b.datasource_ids ? scopeOf(b.datasource_ids) : meta.scope ?? []
  if (mode === 'continue') {
    log.continues.push(runId)
    if (runId !== 'run-restart' && runId !== 'run-susp') {
      return { status: 409, json: { detail: '这次运行已完成，无需继续运行：只有失败的运行或被服务重启打断的运行可以继续运行。如需再次运行，请重新发起。' } }
    }
    ctl.continued[runId] = true
    delete ctl.wsDone[`${runId}@4`]
  }
  if (mode === 'resume' && ctl.run500 && runId === 'run-unk') {
    // 服务端核对那一下出错（查库超时之类）：页面说没核对上，给「重新核对」
    return { status: 500, json: { detail: '服务端错误（500），详情请查看服务日志' } }
  }
  if (mode === 'continue' || mode === 'resume') {
    request.follow = runId
    if (T.row.status !== 'running' && typeof meta.ms === 'number') request.started_ms = now - meta.ms
    const rec = runRecord(String(runId ?? ''))
    if (T.row.status === 'running' && rec.json?.started_at) request.started_ms = Date.parse(rec.json.started_at)
    if (mode === 'resume' && ['succeeded', 'failed', 'cancelled'].includes(rec.json?.status) && rec.json?.finished_at) {
      request.ended_ms = Date.parse(rec.json.finished_at)
    }
    T.row = { ...T.row, status: 'running', error: '', meta: { ...meta, scope: scope.length ? scope : undefined } }
  } else {
    if (['rerun', 'max_steps', 'run'].includes(mode) && !(T.row.graph?.nodes?.length)) {
      return { status: 409, json: { detail: '这一轮没有可运行的工作流，请重试本轮', code: 'chat_turn_invalid' } }
    }
    if (runId && mode !== 'run') {
      // 服务重启挂起的旧运行没人会再去续它：顺手取消（停在审批上的不算）
      const rec = runRecord(runId)
      const pending = ctl.approvals.some((a) => a.run_id === runId && a.status === 'pending')
      if (rec.json?.status === 'interrupted' && !pending) log.cancels.push(runId)
    }
    if (mode !== 'run') {
      const archived = attemptRecord(T.row, attempts.length + 1)
      attempts.push(archived)
      if (mode === 'retry') {
        const reason = attemptReason(archived)
        request.base = !!T.row.graph?.nodes?.length
        request.instruction = request.base
          ? `这个工作流上一次没有给出可用的结果：${reason}。\n\n请针对这个原因改它（比如调大最大步数、改正工具参数、换个查询方式），改完继续回答原来的问题：${T.row.question}`
          : `${T.row.question}\n\n（上一次没有成功：${reason}。请针对这个原因调整，重新给出完整的工作流。）`
      }
      if (mode === 'max_steps') request.max_steps = b.max_steps
    }
    const usage = mode === 'run' ? meta.usage : retireRun(meta.usage)
    T.row = { ...T.row, status: 'running', error: '', answer: mode === 'run' ? T.row.answer : '', review: {}, run_id: null,
              meta: mode === 'run' ? { ...meta, runId: null } : { v: 1, ...(attempts.length ? { attempts } : {}),
                ...(scope.length ? { scope } : {}), ...(usage && Object.keys(usage).length ? { usage } : {}), runId: null } }
  }
  T.epoch = (T.epoch ?? 0) + 1
  T.gen = srv.gen
  T.cancelled = false
  T.late = null
  T.request = request
  T.scope = scope
  T.job = { status: 'running', stage: null, mode, run_id: request.follow ?? null, attempt: 0, last_seq: T.events.length,
            attempt_seq: 0, error: null }
  T.job.attempt_seq = emit(T, 'turn.attempt', {
    mode, attempts, scope, usage: T.row.meta?.usage ?? {}, started_ms: request.started_ms,
    ...(request.ended_ms ? { ended_ms: request.ended_ms } : {}), ...(request.max_steps ? { max_steps: request.max_steps } : {}),
  })
  log.turnStarts.push({ ...b, _turn: T.id })
  void driveTurn(T, T.epoch).catch((e) => console.log(`  · 伪造的服务端出错：${e?.stack ?? e}`))
  return { status: 202, json: { turn_id: T.id, turn: rowOut(T) } }
}

function stage(T, name) {
  T.job.stage = name
  emit(T, 'stage', { stage: name })
}

function saveRow(T, fields, meta) {
  const merged = { ...(T.row.meta ?? {}), v: 1 }
  for (const [k, v] of Object.entries(meta ?? {})) {
    if (v === undefined || v === null) delete merged[k]
    else merged[k] = v
  }
  merged.at = Date.now()
  T.row = { ...T.row, ...fields, meta: merged }
}

/** 这一轮到此为止：写那一行、发 end（同服务端 ChatTurnExecution._conclude） */
function settle(T, outcome, fields, meta, output = null, run = null) {
  const ms = Math.max(0, (T.request.ended_ms ?? Date.now()) - T.request.started_ms)
  saveRow(T, fields, { looks: T.looks.length ? T.looks : undefined, ms, ...meta })
  emit(T, 'end', { outcome, turn: rowOut(T), output, run })
  T.job = { ...T.job, status: 'succeeded', stage: null }
}

async function driveTurn(T, epoch) {
  const req = T.request
  const mode = req.mode
  const autoRetry = mode === 'ask' || mode === 'retry'
  let attempts = 0
  let instruction = req.instruction ?? T.row.question
  let base = req.base ? T.row.graph : null
  let direct = ['rerun', 'max_steps', 'run'].includes(mode) ? T.row.graph : null
  if (mode === 'max_steps') {
    direct = { ...direct, nodes: direct.nodes.map((n) => (n.type === 'agent'
      ? { ...n, data: { ...n.data, config: { ...(n.data?.config ?? {}), max_steps: req.max_steps } } } : n)) }
    T.row = { ...T.row, graph: direct }
  }
  let follow = req.follow ?? null
  T.usage = { ...(T.row.meta?.usage ?? {}) }
  T.looks = []
  while (true) {
    if (!direct && !follow) {
      stage(T, 'build')
      const gen = { instruction, base_graph: base, intent: 'answer', conversation_id: T.conversation_id,
                    ...(T.scope?.length ? { datasource_ids: T.scope.map((x) => x.id) } : {}) }
      log.gens.push(gen)
      const got = copilotScript(gen)
      if (got.status) {
        const message = got.json.detail
        return settle(T, 'error', { status: 'error', error: message, review: {} },
          { cause: { kind: 'refused', message, code: got.json.code ?? null }, runId: null, usage: T.usage })
      }
      const ops = []
      for (const op of got.ops) {
        if (op.gate) { await until(() => ctl[op.gate] || gone(T, epoch), 15000); continue }
        if (gone(T, epoch)) return
        emit(T, 'op', op)
        if (op.op !== 'thinking' && op.op !== 'heartbeat') ops.push(op)
        if (op.op === 'look') T.looks.push(op)
        if (op.op === 'usage') T.usage.copilot = addTotal(T.usage.copilot, op)
        if (op.op === 'final' && !op.asked) T.row = { ...T.row, graph: op.graph, explanation: op.explanation ?? '' }
        await sleep(ctl.opGapMs ?? 10)
      }
      if (gone(T, epoch)) return
      const terminal = ops.filter((o) => ['final', 'reply', 'error'].includes(o.op)).at(-1)
      if (!terminal) {
        const ask = ops.filter((o) => o.op === 'ask').at(-1)
        const q = String(ask?.question ?? '')
        return settle(T, 'asked', { status: 'done', answer: q, review: {} },
          { asked: { question: q, options: ask?.options ?? [] }, runId: null, usage: T.usage }, q ? { answer: q } : null)
      }
      if (terminal.op === 'error') {
        if (terminal.next === 'retry' && attempts + 1 < 2) {
          attempts += 1
          const reason = terminal.reason ?? String(terminal.message ?? '').replace(/^助手本轮未完成：/, '')
          instruction = base ? `这个工作流上一次运行没有成功：${reason}。\n\n请针对这个原因改它（比如调大最大步数、改正工具参数、换个查询方式），改完继续回答原来的问题：${T.row.question}`
            : `${T.row.question}\n\n（上一次没有成功：${reason}。请针对这个原因调整，重新给出完整的工作流。）`
          emit(T, 'turn.retry', { kind: 'empty', reason, base: !!base, attempt: attempts, usage: T.usage })
          continue
        }
        const message = String(terminal.message ?? '助手本轮未完成')
        return settle(T, 'error', { status: 'error', error: message, review: {} },
          { cause: { kind: 'copilot', message, hint: terminal.hint, detail: terminal.detail }, runId: null, usage: T.usage })
      }
      if (terminal.op === 'reply') {
        const text = String(terminal.text ?? '')
        return settle(T, 'reply', { status: 'done', answer: text, review: {} }, { noQuery: true, runId: null, usage: T.usage },
          { answer: text })
      }
      const blockers = (terminal.issues ?? []).filter((i) => i.level === 'error').map((i) => i.message)
      const selfChecked = ops.some((o) => o.op === 'check')
      const asked = ops.filter((o) => o.op === 'ask').at(-1)
      const next = terminal.next ?? (blockers.length ? (selfChecked ? 'fail' : 'retry') : terminal.autorun === false ? 'ready' : 'run')
      if (next === 'fail' || next === 'retry') {
        return settle(T, 'error', { status: 'error', error: `生成的工作流没有通过校验：${blockers.join('；')}`, graph: terminal.graph,
                                    explanation: terminal.explanation ?? '', review: {} },
          { cause: { kind: 'blocked', issues: blockers, self_checked: selfChecked }, runId: null, usage: T.usage })
      }
      if (next === 'ready') {
        const q = String(asked?.question ?? '').trim()
        return settle(T, 'ready', { status: 'done', graph: terminal.graph, explanation: terminal.explanation ?? '', review: {},
                                    ...(q ? { answer: q } : {}) },
          { runId: null, usage: T.usage, ...(q ? { asked: { question: q, options: [] } } : {}) }, q ? { answer: q } : null)
      }
      direct = terminal.graph
    }

    stage(T, 'run')
    let runId = follow
    if (!runId) {
      const launched = launchScript({ graph: direct, input: { question: T.row.question, history: '' }, origin: 'chat',
                                      origin_ref: { conversation_id: T.conversation_id, turn_id: T.id } })
      if (launched.abort || launched.status) {
        const message = launched.json?.detail ?? '运行没有发起'
        return settle(T, 'error', { status: 'error', error: message, review: {} },
          { cause: { kind: 'start', message, code: launched.json?.code ?? null, status: launched.status ?? 0 }, runId: null, usage: T.usage })
      }
      runId = launched.id
      srv.launched.add(runId)
      // 同一个运行 id 各段、各套主题会再用：这一次从头算（剧本还没放、没有接着跑过）
      delete ctl.wsDone[`${runId}@0`]
      delete ctl.continued[runId]
      delete T.usage.run
      T.runId = runId
      T.job.run_id = runId
      saveRow(T, { run_id: runId, status: 'running' }, { runId, runClass: 'exploratory', runStatus: 'queued' })
      emit(T, 'run', { run_id: runId, status: 'queued', run_class: 'exploratory' })
    } else {
      T.runId = runId
    }
    const graphRun = direct
    direct = follow = null
    const result = await followRun(T, epoch, runId)
    if (gone(T, epoch)) return
    const rec = runRecord(runId)
    const run = { id: runId, status: result.status, run_class: 'exploratory', usage: rec.json?.usage ?? {},
                  error: result.error ?? null, end_reason: result.endReason ?? null }
    T.usage.run = addTotal({}, rec.json?.usage)
    const meta = { runId, runClass: 'exploratory', runStatus: result.status, usage: T.usage }
    if (result.status === 'gone') {
      return settle(T, 'error', { status: 'error', error: '运行记录不见了', review: {} }, { ...meta, cause: { kind: 'gone' }, runStatus: 'gone' })
    }
    if (result.status === 'rejected') {
      return settle(T, 'cancelled', { status: 'error', error: '已取消', review: {} },
        { ...meta, runStatus: 'cancelled', outcome: 'cancelled', rejected: result.rejected }, null, { ...run, status: 'cancelled' })
    }
    if (result.status === 'cancelled') {
      return settle(T, 'cancelled', { status: 'error', error: '已取消', review: {} }, { ...meta, outcome: 'cancelled' }, null, run)
    }
    if (result.status === 'suspended') {
      return settle(T, 'suspended', { status: 'error', error: '服务重启，本轮已中断', review: {} },
        { ...meta, outcome: 'suspended', runStatus: 'interrupted' }, null, { ...run, status: 'interrupted' })
    }
    if (result.status === 'failed') {
      return settle(T, 'error', { status: 'error', error: result.error ?? '运行失败', review: {} },
        { ...meta, cause: { kind: 'run', error: result.error ?? '', ...(result.detail ? { detail: result.detail } : {}) } }, null, run)
    }

    stage(T, 'review')
    // 成果带逐段证据时复核只能加说明（服务端 review_of_run 按 locked 不改写）：剧本里老后端那种改写在这里收成「加说明」
    const locked = !!(result.output ?? {})._evidence
    const reviewing = reviewScript({ run_id: runId, question: T.row.question, attempt: attempts }).then((r) => (locked && r?.answer
      ? { ...r, answer: null, original: null, verdict: r.verdict === 'rewritten' ? 'annotated' : r.verdict } : r))
    const review = await Promise.race([reviewing, sleep(ctl.reviewWaitMs ?? 25_000).then(() => null)])
    if (gone(T, epoch)) return
    if (review) {
      emit(T, 'review', { result: review })
      T.usage.review = addTotal(T.usage.review, review.usage)
      if (autoRetry && review.retry_instruction) {
        attempts += 1
        T.usage = retireRun(T.usage)
        T.runId = null
        saveRow(T, { run_id: null }, { runId: null, runStatus: null, usage: T.usage })
        const reason = (review.signals ?? []).map((x) => x.detail).join('；') || review.note
        emit(T, 'turn.retry', { kind: 'review', reason, base: !!graphRun?.nodes?.length, attempt: attempts, usage: T.usage })
        instruction = review.retry_instruction
        base = graphRun?.nodes?.length ? graphRun : null
        continue
      }
    } else {
      // 等不及：先交原答案；那一次复核晚到了照样把用量记进这一轮
      T.late = reviewing.then((r) => {
        if (T.epoch !== epoch) return
        if (r?.usage) {
          const usage = { ...(T.row.meta?.usage ?? {}), review: addTotal(T.row.meta?.usage?.review, r.usage) }
          saveRow(T, {}, { usage })
          emit(T, 'review.late', { usage: r.usage })
        }
      }).finally(() => { if (T.epoch === epoch) T.late = null })
    }
    const output = result.output ?? {}
    const rewritten = review && review.verdict !== 'ok' && !output._evidence ? review.answer : null
    const issuance = output._issuance
    return settle(T, 'done', { status: 'done', answer: rewritten || answerText(output), review: review ?? {} },
      { ...meta, runStatus: 'succeeded', queries: result.queries,
        ...(issuance ? { issuance } : {}), ...(output._evidence ? { evidence: true } : {}) }, output, { ...run, status: 'succeeded' })
  }
}

/**
 * 等这次运行到终态（同服务端 _follow：停在审批上照旧等人，没有待审批的断点收成中断）。这一段检查里发起的运行按它的剧本：
 * 剧本有结局的等实时流放完再收尾，没结局的（在跑、等审批、出字中）一直等到被停止；库里早就有的运行（老轮次接着收尾）按
 * GET 运行的状态
 */
async function followRun(T, epoch, runId) {
  const after = ctl.continued[runId] ? 4 : 0
  const sc = script(runId, after)
  const rec = runRecord(runId)
  if (rec.status === 404) return { status: 'gone' }
  const pending = () => ctl.approvals.some((a) => a.run_id === runId && a.status === 'pending')
  const terminal = (status) => {
    const fin = sc.events.filter((e) => e.type === 'run.finished').at(-1)?.data?.output
    const failed = sc.events.filter((e) => e.type === 'run.failed').at(-1)?.data
    const queries = sc.events.filter((e) => e.type === 'tool.start' && String(e.data?.tool ?? '').startsWith('db_query')).length
    const db = rec.status === 200 && rec.json.status === 'succeeded' && Object.keys(rec.json.output ?? {}).length ? rec.json.output : null
    if (status === 'succeeded') {
      // 成果以库里那份为准（事件里那份为了推送体积被切过）
      return { status, queries, output: db ?? (runId === 'run-noget' ? { answer: FULL, note: '附注' } : fin ?? {}) }
    }
    if (status === 'failed') return { status, error: failed?.error ?? rec.json?.error ?? '', detail: failed?.detail }
    if (status === 'cancelled' && rec.json?.end_reason === 'rejected') {
      return { status: 'rejected', endReason: 'rejected', rejected: { sub: rec.json?.error || '被驳回' } }
    }
    return { status }
  }
  /** 停在审批上：等人（stage waiting），审批过了接着等 */
  const waitApproval = async () => {
    if (T.job.stage !== 'waiting') stage(T, 'waiting')
    while (!gone(T, epoch) && pending()) await sleep(80)
    if (!gone(T, epoch)) stage(T, 'run')
  }
  const deadline = Date.now() + 120_000
  if (srv.launched.has(runId) || ctl.continued[runId]) {
    const ended = sc.end === 'close' ? 'interrupted' : sc.end
    if (ended) {
      await until(() => ctl.wsDone[`${runId}@${after}`] || gone(T, epoch), 3000)
      if (ended !== 'interrupted') return terminal(ended)
      if (!pending()) return { status: 'suspended' }
    }
    // 剧本里不结束的：一直在跑，停在审批上的等人
    while (!gone(T, epoch) && Date.now() < deadline) {
      if (pending()) await waitApproval()
      await sleep(80)
    }
    return { status: 'running' }
  }
  // 库里早就有的运行
  while (!gone(T, epoch) && Date.now() < deadline) {
    const status = runRecord(runId).json?.status
    if (['succeeded', 'failed', 'cancelled'].includes(status)) return terminal(status)
    if (status === 'interrupted') {
      if (!pending()) return { status: 'suspended' }
      await waitApproval()
    }
    await sleep(80)
  }
  return { status: 'running' }
}

/** 停止（服务端 chat/job.cancel）：取消生成、取消运行，那一行收成已取消 */
function cancelTurn(T) {
  if (!ACTIVE_JOB.has(T.job.status)) return false
  T.cancelled = true
  if (T.runId) log.cancels.push(T.runId)
  saveRow(T, { status: 'error', error: '已取消', review: {} }, { outcome: 'cancelled', runStatus: T.runId ? 'cancelled' : undefined })
  emit(T, 'end', { outcome: 'cancelled', turn: rowOut(T), output: null,
                   run: T.runId ? { id: T.runId, status: 'cancelled', run_class: 'exploratory', usage: {} } : null })
  T.job = { ...T.job, status: 'cancelled', stage: null }
  return true
}

/** 实时流（长轮询）：没有新事件就等一会儿；这一轮不在进行中、也都给完了，补一条 stream.end */
async function streamTurn(route, T, after) {
  log.streams.push({ turn: T.id, after })
  if (ctl.dropStreams > 0) {
    // 断线：这一次连接什么都没给就断了（网络抖一下）。页面按收到的最后一个 seq 退避重连
    ctl.dropStreams -= 1
    return route.abort('connectionreset')
  }
  await until(() => T.events.length > after || !turnActive(T) || T.gen !== srv.gen, 6000)
  const fresh = T.events.filter((e) => e.seq > after)
  let body = fresh.map((e) => `data: ${JSON.stringify(e)}\n\n`).join('')
  if (!turnActive(T) && after + fresh.length >= T.events.length) {
    body += `data: ${JSON.stringify({ type: 'stream.end', status: T.job.status, data: { status: T.job.status } })}\n\n`
  }
  return route.fulfill({ status: 200, contentType: 'text/event-stream', body: body || ': ping\n\n' })
}

// ---------------------------------------------------------------- 浏览器

const browser = await chromium.launch({ executablePath: CHROME })

async function open(theme = 'light', { width = 1440, height = 900, reducedMotion = 'no-preference', settings = null } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height }, colorScheme: theme, reducedMotion })
  opened.add(ctx)
  await ctx.addInitScript((t) => localStorage.setItem('agentlab.theme', t), theme)
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  watchLoad(page)
  await page.route('**/api/settings', async (route) => {
    if (route.request().method() !== 'GET') return route.abort()
    // 主题以设置为准（沙箱里存的是 light），这里按要看的那套改掉
    return route.fulfill({ json: { ui: { theme }, run: {}, limits: { max_agent_steps: 25 }, copilot: {}, embedding: {},
      ...(settings ?? {}) } })
  })
  await page.route(/\/api\/(conversations|chat\/turns|copilot\/(generate-stream|review)|runs|approvals|datasources)(\/|\?|$)/, fakeApi)
  await page.routeWebSocket(/\/api\/runs\/[^/]+\/stream/, fakeStream)
  return { page, ctx, errors }
}

/** 记下页面加载时出的岔子（页面报错、模块没取到），等不到 store 时拿来说清卡在哪 */
const loadTrouble = new WeakMap()
function watchLoad(page) {
  const list = []
  loadTrouble.set(page, list)
  const isModule = (url) => /^\/(src|node_modules|@vite|@id|@fs)\//.test(new URL(url).pathname)
  page.on('pageerror', (e) => list.push(`页面报错「${e.message.split('\n')[0]}」`))
  page.on('requestfailed', (r) => { if (isModule(r.url())) list.push(`模块没取到 ${new URL(r.url()).pathname}`) })
  page.on('response', (r) => { if (r.status() >= 400 && isModule(r.url())) list.push(`模块 ${r.status()} ${new URL(r.url()).pathname}`) })
}
/**
 * 等页面把 store 挂到 window 上。main → App → store 全是静态 import，load 之前就求值完了
 * （实测单跑、四个检查并行跑，load 那一刻都已挂出），所以等不到不是「慢」，是模块图断了
 * 或页面卡死。只有「模块没取到」（开发服务器那一下没答上）重载一次，并打一行出来；
 * 页面报错、vite 报错层、主线程卡死是真问题，说清卡在哪，不只留一句 Timeout
 */
async function ready(page, fn, what, { arg = null, timeout = 15000, retry = true } = {}) {
  if (await page.waitForFunction(fn, arg, { timeout }).then(() => true, () => false)) return
  const trouble = loadTrouble.get(page) ?? []
  if (retry && trouble.length && trouble.every((t) => t.startsWith('模块'))) {
    console.log(`  · ${what} 没挂出来（${trouble.slice(0, 2).join('；')}），重载一次`)
    trouble.length = 0
    await page.reload()
    return ready(page, fn, what, { arg, timeout, retry: false })
  }
  const probe = page.evaluate(() => {
    const overlay = document.querySelector('vite-error-overlay')
    if (overlay) return `vite 报错层「${(overlay.shadowRoot?.querySelector('.message')?.textContent ?? '').trim().slice(0, 120)}」`
    return `页面停在 ${location.pathname}，#root 下 ${document.getElementById('root')?.childElementCount ?? 0} 个元素`
  }).catch((e) => `读不到页面：${String(e.message).split('\n')[0]}`)
  const hung = new Promise((r) => setTimeout(() => r('页面主线程 3 秒没有响应'), 3000))
  const where = await Promise.race([probe, hung])
  throw new Error(`${what} ${timeout / 1000} 秒没挂出来：${[where, ...trouble.slice(0, 3)].join('；')}`)
}
const goto = async (page, id) => {
  await page.goto(`${WEB}/chat/${id}`)
  await ready(page, () => !!window.__chat, '问数据页的 store（window.__chat）')
}
const shows = (page, text, timeout = 6000) =>
  page.getByText(text).first().waitFor({ timeout }).then(() => true, () => false)
const chatState = (page) => page.evaluate(() => window.__chat.getState())
/** 等脚本这一侧的某个条件成立（伪造后端记下的请求、事件流的开关） */
const until = async (cond, ms = 5000) => {
  const end = Date.now() + ms
  while (!cond() && Date.now() < end) await new Promise((r) => setTimeout(r, 50))
  return cond()
}
const send = async (page, q) => {
  const box = page.getByRole('textbox', { name: '向数据提问' })
  await box.fill(q)
  await box.press('Enter')
}

for (const theme of THEMES) {
  console.log(`\n######## ${theme} ########`)

  await section('hero', '首屏：输入框是主角，例句不拿系统表造句', async () => {
    const { page, ctx, errors } = await open(theme)
    await goto(page, 'c0emptg')
    check('空会话显示首屏', await shows(page, '问你的数据'))
    const hints = await page.locator('button[title="填入输入框，修改后发送"]').allInnerTexts()
    check('有例句', hints.length > 0, hints.join(' | '))
    check('例句里没有系统表',
      hints.every((h) => !/__prisma|_migrations|test_|etl_audit|_log\b/.test(h)), hints.join(' | '))
    check('描述里的业务词造了句', hints.some((h) => /factory：/.test(h)), hints.join(' | '))
    const ph = await page.getByRole('textbox', { name: '向数据提问' }).getAttribute('placeholder')
    check('占位符简短', !!ph && ph.length <= 12, ph ?? '')
    const box = await page.getByRole('textbox', { name: '向数据提问' }).boundingBox()
    check('输入框在首屏中部，不贴底', !!box && box.y > 200 && box.y + box.height < 700, JSON.stringify(box))
    check('数据源入口指向 /data', await page.locator('a[href="/data"]').count() > 0)
    const postsBefore = log.posts.length
    await page.locator('button[title="填入输入框，修改后发送"]').first().click()
    await page.waitForTimeout(200)
    check('点例句只是填进输入框', (await page.getByRole('textbox', { name: '向数据提问' }).inputValue()).length > 0
      && log.posts.length === postsBefore)
    await page.screenshot({ path: `${SHOTS}/hero-${theme}.png` })
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()

    // 知识库、工具没取回来（system-2 终验）：能力条不能把「没取回来」说成「0 个」
    const off = await open(theme)
    await off.page.route(/\/api\/(tools|kb\/collections)(\?|$)/, (route) => route.fulfill({ status: 502, body: 'bad gateway' }))
    await goto(off.page, 'c0emptg')
    await shows(off.page, '问你的数据')
    // 导航里也有这两个链接（「知识」「工具」），只看首屏能力条上的
    const strip = off.page.locator('a[href="/knowledge"], a[href="/tools"]').filter({ hasText: /知识库|工具\s*(\d|—|加载失败)/ })
    // 请求还在路上时写「—」，等它落定
    await off.page.waitForFunction(() => [...document.querySelectorAll('a[href="/knowledge"], a[href="/tools"]')]
      .every((a) => !a.textContent.includes('—')), null, { timeout: 8000 }).catch(() => {})
    const said = (await strip.allInnerTexts()).join(' | ')
    check('知识库、工具没取回来：不写「0 个」', !/知识库 0 个|工具 0 个/.test(said), said)
    check('知识库、工具没取回来：说加载失败', /知识库加载失败/.test(said) && /工具加载失败/.test(said), said)
    await off.page.screenshot({ path: `${SHOTS}/hero-catalog-failed-${theme}.png` })
    await off.ctx.close()
  })

  await section('states', '三种状态：加载中 / 加载失败 / 空', async () => {
    const { page, ctx, errors } = await open(theme)
    ctl.slowMs = 2500
    await page.goto(`${WEB}/chat/c0slowd`)
    await ready(page, () => !!window.__chat, '问数据页的 store（window.__chat）')
    await page.waitForTimeout(600)
    check('取历史时画骨架', await page.locator('[aria-busy="true"]').count() > 0)
    check('取历史时不显示首屏', !(await page.getByText('问你的数据').count()))
    const blocked = await page.getByRole('button', { name: '发送' }).isDisabled()
    check('取历史时发不出去', blocked)
    await page.screenshot({ path: `${SHOTS}/loading-${theme}.png` })
    check('历史回来了', await shows(page, 'D 的答案二', 5000))
    ctl.slowMs = 0

    ctl.failE = true
    const before = log.posts.length
    await goto(page, 'c0faile')
    check('取失败时给出错误和重试', await shows(page, '数据库暂时不可用'))
    check('取失败时不显示首屏', !(await page.getByText('问你的数据').count()))
    await send(page, '失败时问一句')
    await page.waitForTimeout(400)
    check('取失败时不能在空态上提问', log.posts.length === before, `${log.posts.length - before} 次开轮`)
    await page.screenshot({ path: `${SHOTS}/load-failed-${theme}.png` })
    ctl.failE = false
    await page.getByRole('button', { name: '重试' }).first().click()
    check('重试后历史回来', await shows(page, 'E 的答案'))
    check('草稿还在', (await page.getByRole('textbox', { name: '向数据提问' }).inputValue()) === '失败时问一句')
    ctl.failE = true

    // 取失败（500、断网）留在原地给重试；只有「这个对话不存在」（404）才把人带走
    await page.goto(`${WEB}/chat/c0gone`)
    check('指向不存在的对话：说清楚为什么换了地方', await shows(page, '该对话不存在'))
    check('落到一个真实的对话', await page.waitForURL(/\/chat\/c0[a-z]+$/, { timeout: 5000 }).then(() => !page.url().endsWith('c0gone'), () => false), page.url())
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  })

  await section('layout', '布局：长会话里输入框和发送键完整可见（1440×900，另看 1024×700）', async () => {
    for (const [width, height] of [[1440, 900], [1024, 700]]) {
      const { page, ctx } = await open(theme, { width, height })
      await goto(page, 'c0longc')
      await shows(page, '第 6 个问题')
      await page.waitForTimeout(500)
      const m = await page.evaluate(() => {
        const ta = document.querySelector('textarea')?.getBoundingClientRect()
        const btn = document.querySelector('button[aria-label="发送"]')?.getBoundingClientRect()
        return { ta: ta && [ta.top, ta.bottom], btn: btn && [btn.top, btn.bottom], h: innerHeight,
                 scroll: document.documentElement.scrollHeight, wide: document.documentElement.scrollWidth, w: innerWidth }
      })
      const at = `${width}×${height}`
      check(`${at} 输入框底边在视口内`, !!m.ta && m.ta[1] <= m.h, JSON.stringify(m))
      check(`${at} 发送键完整可见`, !!m.btn && m.btn[0] >= 0 && m.btn[1] <= m.h, JSON.stringify(m.btn))
      check(`${at} 整页不多滚`, m.scroll <= m.h && m.wide <= m.w, `${m.wide}×${m.scroll} / ${m.w}×${m.h}`)
      await page.screenshot({ path: `${SHOTS}/long-${width}-${theme}.png` })
      await ctx.close()
    }

    // 窄屏（390×844）：240px 的会话列表以前照常占宽，正文一行只剩一两个字、整页横向溢出
    const { page, ctx } = await open(theme, { width: 390, height: 844 })
    await goto(page, 'c0longc')
    await shows(page, '第 6 个问题')
    await page.waitForTimeout(400)
    const narrow = await page.evaluate(() => {
      const root = document.querySelector('button[aria-label="展开对话列表"]')?.closest('.relative')
      const body = document.querySelector('[data-stream-scroll]')?.getBoundingClientRect()
      const ta = document.querySelector('textarea')?.getBoundingClientRect()
      return { list: !!document.querySelector('aside[aria-label="对话列表"]'), toggle: !!root,
               over: root ? root.scrollWidth - root.clientWidth : null, body: body && Math.round(body.width),
               ta: ta && Math.round(ta.right), w: innerWidth }
    })
    check('390 宽：会话列表默认收起', !narrow.list && narrow.toggle, JSON.stringify(narrow))
    check('390 宽：问数据这一栏不横向溢出', narrow.over === 0, JSON.stringify(narrow))
    check('390 宽：正文宽度够读（≥ 260px）', (narrow.body ?? 0) >= 260, JSON.stringify(narrow))
    check('390 宽：输入框右边在视口内', !!narrow.ta && narrow.ta <= narrow.w, JSON.stringify(narrow))
    await page.screenshot({ path: `${SHOTS}/narrow-390-${theme}.png` })
    await page.getByRole('button', { name: '展开对话列表' }).click()
    const drawer = page.locator('[data-conversation-drawer] aside[aria-label="对话列表"]')
    check('390 宽：展开是浮在正文上面的抽屉', await drawer.waitFor({ timeout: 3000 }).then(() => true, () => false))
    const bodyAfter = await page.evaluate(() => Math.round(document.querySelector('[data-stream-scroll]')?.getBoundingClientRect().width ?? 0))
    check('390 宽：抽屉打开时正文不被挤窄', bodyAfter === narrow.body, `${narrow.body} → ${bodyAfter}`)
    await page.screenshot({ path: `${SHOTS}/narrow-390-drawer-${theme}.png` })
    await drawer.locator('div.group', { hasText: '会话 B' }).locator('button').first().click()
    await page.waitForURL(/\/chat\/c0idleb$/, { timeout: 5000 }).catch(() => {})
    await page.waitForTimeout(300)
    check('390 宽：挑了一个对话，抽屉收回去', page.url().endsWith('/chat/c0idleb')
      && await page.locator('aside[aria-label="对话列表"]').count() === 0, page.url())
    await ctx.close()
  })

  await section('busy', '忙不忙按会话算', async () => {
    const { page, ctx, errors } = await open(theme)
    const cancelsBefore = log.cancels.length
    await goto(page, 'c0busya')
    await send(page, '一个会跑很久的问题')
    check('A 在跑：按钮是停止', await page.getByRole('button', { name: '停止这一轮' })
      .waitFor({ timeout: 8000 }).then(() => true, () => false))
    // 停止键在建图时就出现了；要等运行真的起来、事件流接上，才谈得上"别的会话别碰它"
    await page.waitForFunction(() => !!window.__chat.getState().byConversation.c0busya?.at(-1)?.run?.id,
      null, { timeout: 8000 }).catch(() => {})
    const runA = (await chatState(page)).byConversation.c0busya.at(-1).run?.id
    await until(() => ctl.wsClosed[runA] === false)
    await page.getByRole('button', { name: /^会话 B/ }).click()
    await shows(page, 'B 的答案')
    check('切到 B：按钮是发送，不是停止',
      await page.getByRole('button', { name: '发送' }).count() === 1
      && await page.getByRole('button', { name: '停止这一轮' }).count() === 0)
    check('B 里说清 A 还在跑', await shows(page, '正在运行，这里可以照常提问'))
    const rowA = page.locator('aside[aria-label="对话列表"] div.group', { hasText: '会话 A' })
    check('列表里 A 标着运行中', (await rowA.innerText()).includes('运行中'))
    await rowA.hover()
    check('A 正在跑，不能删', await rowA.getByRole('button', { name: /删除/ }).isDisabled())
    const rowB = page.locator('aside[aria-label="对话列表"] div.group', { hasText: '会话 B' })
    await rowB.hover()
    check('B 空闲，可以删', !(await rowB.getByRole('button', { name: /删除/ }).isDisabled()))
    await page.screenshot({ path: `${SHOTS}/busy-elsewhere-${theme}.png` })
    check('在 B 里什么都没停：A 的事件流还开着', ctl.wsClosed[runA] === false, runA)
    check('A 的运行没被取消', log.cancels.length === cancelsBefore)

    await page.locator('[role=status]', { hasText: '正在运行，这里可以照常提问' }).getByRole('link', { name: '查看', exact: true }).click()
    await page.getByRole('button', { name: '停止这一轮' }).click()
    await until(() => log.cancels.length > cancelsBefore && ctl.wsClosed[runA] === true)
    check('停止只取消 A 自己的运行', log.cancels.slice(cancelsBefore).join() === runA, log.cancels.slice(cancelsBefore).join())
    check('A 的事件流关了', ctl.wsClosed[runA] === true)
    const a = (await chatState(page)).byConversation.c0busya.at(-1)
    check('A 这一轮收成已取消', a.phase === 'cancelled', a.phase)
    check('已取消的轮次没有「继续运行」', !(await page.getByRole('button', { name: '继续运行' }).count()))
    check('已取消的轮次给「重新运行本轮」', await page.getByRole('button', { name: '重新运行本轮' }).count() === 1)
    const b = (await chatState(page)).byConversation.c0idleb
    check('B 的轮次没被动过', b.length === 1 && b[0].phase === 'done')
    await page.waitForTimeout(300)   // 「查看」刚切过来，卡片的入场动效还没播完
    await page.screenshot({ path: `${SHOTS}/cancelled-${theme}.png` })
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  })

  await section('truncated', '长答案：事件里截断了，用 GET 运行的完整版', async () => {
    const { page, ctx, errors } = await open(theme)
    const patchesBefore = log.patches.length
    await goto(page, 'c0trunc')
    await send(page, '给我一份长答案')
    check('答完了', await page.waitForFunction(
      () => window.__chat.getState().byConversation.c0trunc?.at(-1)?.phase === 'done', null, { timeout: 10000 })
      .then(() => true, () => false))
    const t = (await chatState(page)).byConversation.c0trunc.at(-1)
    check('显示的是完整答案', String(t.output?.answer ?? '').length === FULL.length, `${String(t.output?.answer ?? '').length} / ${FULL.length}`)
    check('llm.token 没进 events', !t.events.some((e) => e.type === 'llm.token'), `${t.events.length} 条`)
    // 一轮在服务端跑（docs/design/orchestration-round §1）：那一行由服务端写，页面不再 PATCH 这一轮的结局
    check('页面不写这一轮的结局（不 PATCH 轮次）', log.patches.slice(patchesBefore).length === 0,
      JSON.stringify(log.patches.slice(patchesBefore).map((p) => Object.keys(p.body))))
    const meta = t.meta
    check('服务端写的那一行带着可信度信息（运行、类别），页面照它摆', meta?.runId === 'run-long' && meta?.runClass === 'exploratory',
      JSON.stringify(meta))
    // 后端有了单独的 meta 列：review 只放复核结论，不再夹带
    check('meta 单独存，不夹在 review 里', !t.reviewRaw || !('meta' in t.reviewRaw), JSON.stringify(Object.keys(t.reviewRaw ?? {})))
    check('完整答案取到了，meta 里没有「没取全」', !meta?.clipped, JSON.stringify(meta?.clipped))
    await page.getByText('展开全部').first().click().catch(() => {})
    check('完整结尾在页面上', await shows(page, '【完整结尾】'))
    // 建图时助手查看了哪些表：收在「规划」里，点开是一行，再点开看表名
    check('建图的 look 事件原样记进这一轮', t.ops.some((o) => o.op === 'look'))
    check('查看记录随那一行存进 meta.looks（look §2.5.8）', Array.isArray(meta?.looks) && meta.looks.length === 1
      && meta.looks[0].objects?.[0]?.source === 'shop', JSON.stringify(meta?.looks))
    const turnCard = page.locator('[data-turn]').last()
    await turnCard.locator('button', { hasText: '规划' }).first().click()
    const ctxRow = turnCard.locator('[data-step-code="copilot_look"]')
    check('点开「规划」看到查看了几张表', await ctxRow.waitFor({ timeout: 3000 }).then(() => true, () => false)
      && (await ctxRow.innerText()).includes('查看了 2 张表'))
    await ctxRow.locator('button').first().click()
    check('再点开看到每个库一行的表名', await ctxRow.getByText('「shop」：按需求挑出 orders、order_items')
      .waitFor({ timeout: 3000 }).then(() => true, () => false))
    // 真实运行里数据源一张目录都没有，挑表和 SQL 检查都用不上目录：这一行下面提示起草，并给去目录页的入口
    const noCatalog = ctxRow.locator('[data-step-next]')
    check('没有数据目录的大库：提示先起草目录', await noCatalog.count() === 1
      && (await noCatalog.innerText()).includes('这个数据源还没有数据目录，起草后挑表和 SQL 检查会更准'),
      await noCatalog.count() ? await noCatalog.innerText() : '没有这一句')
    check('……入口直达这个数据源的目录页', await ctxRow.locator('a[data-fix="catalog"]').getAttribute('href').catch(() => null)
      === '/data/catalog/src-shop')
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  })

  await section('evidence', '成果带逐段证据：复核只加说明，不改写答案（可点击证据第一期）', async () => {
    const { page, ctx, errors } = await open(theme)
    // 文档记着的运行要和成果的运行对得上（run-evid），不然前端按「别的运行写的」退回普通文本
    await page.route(/\/api\/artifacts\//, (route) => route.fulfill({ json: { id: EVIDENCE.doc_artifact,
      content: { ...EVIDENCE.doc, run_id: 'run-evid' } } }))
    await page.route(/\/api\/runs\/run-evid\/evidence(\/.*)?(\?.*)?$/, (route) => route.fulfill({ json: EVIDENCE.graph }))
    const patchesBefore = log.patches.length
    await goto(page, 'c0trunc')
    await send(page, '写一份带证据的周报')
    check('答完了', await page.waitForFunction(
      () => window.__chat.getState().byConversation.c0trunc?.at(-1)?.phase === 'done', null, { timeout: 10000 })
      .then(() => true, () => false))
    const t = (await chatState(page)).byConversation.c0trunc.at(-1)
    check('复核的改写没有落到答案上：成果还是报告原文和它的证据标注',
      t.output?.['周报'] === EVIDENCE.output['周报'] && !!t.output?._evidence && !t.output?.answer, JSON.stringify(Object.keys(t.output ?? {})))
    check('复核的说明照样摆出来，改写的文字丢掉', t.review?.note?.includes('检索降级过') && t.review.answer === null
      && t.review.verdict === 'annotated' && !t.rawOutput, JSON.stringify(t.review))
    await page.waitForSelector('[data-evidence-doc]', { timeout: 5000 }).catch(() => {})
    check('页面上是逐段可点的报告，不是改写后的文字', await page.locator('[data-evidence-doc] [data-seg]').count() > 0
      && !(await shows(page, '改写后的答案')))
    check('没有「复核改写过此答案」的对照', await page.getByText('复核改写过此答案').count() === 0)
    check('页面不写这一轮的结局（不 PATCH 轮次）', log.patches.slice(patchesBefore).length === 0)
    check('meta 记下这一轮有证据（刷新后回运行那里取标注）', t.meta?.evidence === true, JSON.stringify(t.meta))
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  })

  await section('askdata', '问数据的答案带逐段证据：实时、刷新后补回、复核守卫、窄屏抽屉（可点击证据第二期）', async () => {
    const routeEvidence = async (page) => {
      // 文档记着的运行要和成果的运行对得上（run-evq），片段按运行认
      await page.route(/\/api\/artifacts\//, (route) => route.fulfill({ json: { id: EVQ.doc_artifact, content: EVQ_DOC } }))
      await page.route(/\/api\/runs\/run-evq\/evidence(\/.*)?(\?.*)?$/, (route) => {
        const m = new URL(route.request().url()).pathname.match(/\/segments\/([^/]+)$/)
        if (!m) return route.fulfill({ json: EVQ.graph })
        const body = EVQ.segments[decodeURIComponent(m[1])]
        return body ? route.fulfill({ json: body }) : route.fulfill({ status: 404, json: { detail: '没有这个片段', code: 'evidence_segment_not_found' } })
      })
    }
    {
      const { page, ctx, errors } = await open(theme)
      await routeEvidence(page)
      const patchesBefore = log.patches.length
      await goto(page, 'c0trunc')
      await send(page, '问数据带证据：各区域销售额，最大的一单是多少')
      check('答完了', await page.waitForFunction(
        () => window.__chat.getState().byConversation.c0trunc?.at(-1)?.phase === 'done', null, { timeout: 10000 })
        .then(() => true, () => false))
      const t = (await chatState(page)).byConversation.c0trunc.at(-1)
      check('复核的改写没落到答案上：成果还是报告原文和 _evidence', t.output?.answer === EVQ.output.answer && !!t.output?._evidence
        && t.review?.answer === null, JSON.stringify({ keys: Object.keys(t.output ?? {}), review: t.review?.verdict }))
      await page.waitForSelector('[data-evidence-doc] [data-seg]', { timeout: 6000 }).catch(() => {})
      const turn = page.locator('[data-turn]').last()
      check('答案是逐段可点的报告', await turn.locator('[data-evidence-doc] [data-seg]').count() > 10)
      check('没有出具契约：文档自己给计数条', (await turn.locator('[data-evidence-tally]').innerText().catch(() => '')).includes('数字有出处'))
      const text = await turn.innerText()
      check('执行过程里有「按出处抽取字段」和字段对不上的提醒', text.includes('按出处抽取字段') && text.includes('有 1 个字段与查询快照不一致'),
        text.replace(/\s+/g, ' ').slice(0, 200))
      check('整表的每格都是可点的片段', await turn.locator('[data-evidence-doc] table td [data-seg]').count() === 9)
      await turn.locator(`[data-seg="${EVQ_SEG('1,288')}"]`).click()
      await page.waitForSelector('[data-evidence-panel] [data-ev-query]', { timeout: 5000 }).catch(() => {})
      const panel = page.locator('[data-evidence-panel]')
      check('宽屏：点开数字从侧边弹出，看得到查询步骤', await panel.getAttribute('data-evidence-panel') === 'side'
        && await panel.locator('[data-ev-query]').count() === 1)
      check('……被引用的格高亮，SQL 在，遮罩说明在', (await panel.locator('td[data-highlight="cell"]').innerText().catch(() => '')) === '1288'
        && (await panel.locator('[data-ev-sql]').innerText().catch(() => '')).includes('FROM orders')
        && (await panel.locator('[data-ev-mask-note]').innerText().catch(() => '')).includes('不是安全边界'))
      await page.waitForTimeout(300)
      await page.screenshot({ path: `${SHOTS}/askdata-evidence-${theme}.png` })
      await page.keyboard.press('Escape')
      check('页面不写这一轮的结局；服务端写的 meta 记下这一轮有证据', log.patches.slice(patchesBefore).length === 0
        && t.meta?.evidence === true, JSON.stringify(t.meta))
      check('没有运行时报错（实时）', errors.length === 0, errors[0] ?? '')
      await ctx.close()
    }
    resetDb()
    ctl.askConvs = true
    try {
      const { page, ctx, errors } = await open(theme)
      await routeEvidence(page)
      const gets = log.runGets.length
      await goto(page, 'c0askz')
      await page.waitForSelector('[data-evidence-doc] [data-seg]', { timeout: 8000 }).catch(() => {})
      check('刷新后补回：库里只有文字，回运行取回 _evidence，答案照样逐段可点',
        log.runGets.slice(gets).includes('run-evq') && await page.locator('[data-evidence-doc] [data-seg]').count() > 10)
      await page.locator(`[data-evidence-doc] [data-seg="${EVQ_SEG('8.7%')}"]`).click()
      await page.waitForSelector('[data-evidence-panel] [data-ev-sources]', { timeout: 5000 }).catch(() => {})
      const panel = page.locator('[data-evidence-panel]')
      check('……点开指标：口径卡来源、输入来源、两次查询都在', (await panel.locator('[data-ev-caliber-from]').innerText().catch(() => '')).includes('销售周报')
        && await panel.locator('[data-ev-source]').count() === 2 && await panel.locator('[data-ev-query]').count() === 2)
      check('没有运行时报错（刷新后）', errors.length === 0, errors[0] ?? '')
      await ctx.close()

      const narrow = await open(theme, { width: 800, height: 900 })
      await routeEvidence(narrow.page)
      await goto(narrow.page, 'c0askz')
      await narrow.page.waitForSelector('[data-evidence-doc] [data-seg]', { timeout: 8000 }).catch(() => {})
      await narrow.page.locator(`[data-evidence-doc] [data-seg="${EVQ_SEG('1,288')}"]`).click()
      await narrow.page.waitForSelector('[data-evidence-panel] [data-ev-query]', { timeout: 5000 }).catch(() => {})
      const drawer = narrow.page.locator('[data-evidence-panel]')
      const sc = await narrow.page.evaluate(() => ({ sw: document.documentElement.scrollWidth, vw: innerWidth }))
      check('窄屏：面板从底部抽出，里面是查询步骤', await drawer.getAttribute('data-evidence-panel') === 'drawer'
        && await drawer.locator('[data-ev-query]').count() === 1)
      check('……整页没有横向滚动', sc.sw <= sc.vw, JSON.stringify(sc))
      await narrow.page.waitForTimeout(300)
      await narrow.page.screenshot({ path: `${SHOTS}/askdata-drawer-${theme}.png` })
      await narrow.page.keyboard.press('Escape')
      await narrow.page.waitForTimeout(200)
      check('……Esc 收起抽屉', await narrow.page.locator('[data-evidence-panel]').count() === 0)
      check('没有运行时报错（窄屏）', narrow.errors.length === 0, narrow.errors[0] ?? '')
      await narrow.ctx.close()
    } finally {
      ctl.askConvs = false
    }
  })

  await section('nodeerror', '节点报错说一遍：原因、怎么办不在展开区里再贴一遍原话（整形节点解析 JSON 失败）', async () => {
    for (const [ask, said] of [['整形解析失败：旧说法', JSON_TEMPLATE_ERROR], ['整形解析失败：上游说法', JSON_UPSTREAM_ERROR],
      ['整形解析失败：节点名带超时', JSON_SLOW_ERROR], ['整形解析失败：现在的说法', JSON_TEMPLATE_ERROR_NOW],
      ['整形解析失败：上游、现在的说法', JSON_UPSTREAM_ERROR_NOW]]) {
      const { page, ctx, errors } = await open(theme)
      await goto(page, 'c0trunc')
      await send(page, ask)
      await page.waitForFunction(() => window.__chat.getState().byConversation.c0trunc?.at(-1)?.phase === 'error', null, { timeout: 10000 })
        .catch(() => {})
      const row = page.locator('[data-turn]').last().locator('[data-node-id="tf"][data-step-status="failed"]').first()
      await row.locator('button[aria-expanded]').first().click()
      await page.waitForTimeout(250)
      const shown = await row.innerText()
      const head = said.split(/[。；，]/)[0]
      const times = shown.split(head).length - 1
      check(`「${ask}」：节点那一行把报错说一遍，展开后不再整段贴一遍原话`, times === 1, `出现 ${times} 次：${shown.replace(/\s+/g, ' ').slice(0, 240)}`)
      const tail = said.split(/[；]|。/).at(-1).slice(0, 12)
      check(`「${ask}」：怎么办那半句照样在`, shown.includes(tail), tail)
      const resume = await page.getByRole('button', { name: /^继续运行/ }).count()
      const canvas = await page.getByRole('button', { name: '在画布里打开' }).count() + await page.locator('[data-fix="canvas"]').count()
      check(`「${ask}」：原样继续运行只会再失败一次，不给「继续运行」，给去画布改的入口`, resume === 0 && canvas > 0,
        `继续运行 ${resume} 个 · 画布入口 ${canvas} 个`)
      await row.locator('summary', { hasText: '技术细节' }).click().catch(() => {})
      check(`「${ask}」：原始异常和出错位置前后的原文收在技术细节里`, (await row.innerText()).includes('JSONDecodeError')
        && (await row.innerText()).includes('出错位置前后的原文'))
      if (said === JSON_UPSTREAM_ERROR) await page.screenshot({ path: `${SHOTS}/nodeerror-json-${theme}.png` })
      check('没有运行时报错', errors.length === 0, errors[0] ?? '')
      await ctx.close()
    }
    const { page, ctx } = await open(theme)
    await goto(page, 'c0trunc')
    // 节点名是用户起的，里面带着超时、额度、连不上这些别的规则的关键词：照样认作 JSON 解析失败
    // 老说法、后端现在的说法各喂一遍
    const named = await page.evaluate(async (saids) => {
      const { explainRunError } = await import('/src/lib/explain.ts')
      return saids.flatMap((said) => ['查询超时订单', '额度查询', '连不上的库', '服务器 500 统计'].map((t) => {
        const x = explainRunError(said.replace('上游「查数」', `上游「${t}」`))
        return { t, title: x.title, continuable: x.continuable, fix: x.fix }
      }))
    }, [JSON_UPSTREAM_ERROR, JSON_UPSTREAM_ERROR_NOW])
    check('节点名里带超时 / 额度 / 连不上 / 500：照样认作「上游输出的不是合法 JSON」，不给继续运行',
      named.length === 8 && named.every((x) => x.title.startsWith(`上游「${x.t}」输出的不是合法 JSON`) && x.continuable === false && x.fix === 'canvas'),
      JSON.stringify(named.filter((x) => x.continuable !== false)))
    await send(page, '整形解析失败：改写过的说法')
    await page.waitForFunction(() => window.__chat.getState().byConversation.c0trunc?.at(-1)?.phase === 'error', null, { timeout: 10000 })
      .catch(() => {})
    const row = page.locator('[data-turn]').last().locator('[data-node-id="tf"][data-step-status="failed"]').first()
    await row.locator('button[aria-expanded]').first().click()
    await page.waitForTimeout(250)
    const t = await row.innerText()
    check('对照：说法改写过（鉴权失败）时，原话还在展开区里', t.includes('模型鉴权失败') && t.includes('invalid x-api-key'),
      t.replace(/\s+/g, ' ').slice(0, 200))
    await ctx.close()
  })

  await section('restart', '服务重启：interrupted 且没有待审批，收尾而不是转圈', async () => {
    const { page, ctx, errors } = await open(theme)
    await goto(page, 'c0resti')
    await send(page, '跑到一半服务重启')
    check('说清是服务重启打断的', await shows(page, '服务重启，本轮已中断', 8000))
    await page.waitForTimeout(300)
    const t = (await chatState(page)).byConversation.c0resti.at(-1)
    check('这一轮收成挂起', t.phase === 'suspended', t.phase)
    // 只数这一轮自己和它在左栏的那一行：左栏里夹具的会话 L 本来就标着运行中，它转是对的
    const spinning = await page.locator('[data-turn] .animate-spin').count()
      + await page.locator('aside[aria-label="对话列表"] div.group', { hasText: '会话 I' }).locator('.animate-spin').count()
    check('不再转圈', spinning === 0, `${spinning} 个转圈`)
    check('给「继续运行」', await page.getByRole('button', { name: '继续运行' }).count() === 1)
    check('给「重新运行本轮」', await page.getByRole('button', { name: '重新运行本轮' }).count() === 1)
    check('输入框可以接着问', await page.getByRole('button', { name: '停止这一轮' }).count() === 0)
    await page.screenshot({ path: `${SHOTS}/restart-${theme}.png` })

    await page.getByRole('button', { name: '继续运行' }).click()
    check('继续运行：从断点续上并跑完', await page.waitForFunction(
      () => window.__chat.getState().byConversation.c0resti?.at(-1)?.phase === 'done', null, { timeout: 8000 })
      .then(() => true, () => false), (await chatState(page)).byConversation.c0resti.at(-1).phase)
    check('续跑发给的是这一轮自己的运行', log.continues.at(-1) === 'run-restart', log.continues.join(','))
    const cont = log.turnStarts.filter((b) => b.mode === 'continue').at(-1)
    check('继续运行的请求：带着这一轮（retry_of、mode=continue），不带图', cont?.conversation_id === 'c0resti'
      && cont?.retry_of === t.serverId && !cont?.graph, JSON.stringify(cont))
    check('续上之后答案出来了', await shows(page, '接着跑完了：三条记录。'))
    check('续上之后补救按钮收起', await page.getByRole('button', { name: '继续运行' }).count() === 0)
    await page.waitForTimeout(300)
    await page.screenshot({ path: `${SHOTS}/restart-continued-${theme}.png` })

    // 强杀：连结束标记都没有，连接直接断。重连上重启后的后端，照样收成中断
    await goto(page, 'c0killn')
    await send(page, '一个跑着跑着服务被强杀的问题')
    check('连接直接断了：重连之后收成中断', await page.waitForFunction(
      () => window.__chat.getState().byConversation.c0killn?.at(-1)?.phase === 'suspended', null, { timeout: 8000 })
      .then(() => true, () => false), (await chatState(page)).byConversation.c0killn?.at(-1)?.phase)
    check('强杀之后同样给「继续运行」', await page.getByRole('button', { name: '继续运行' }).count() === 1)
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  })

  await section('resume', '隔了几天再接着跑：计时接着上次的走', async () => {
    const { page, ctx, errors } = await open(theme)
    await goto(page, 'c0suspo')
    check('从库里恢复的中断轮次认成中断', await shows(page, '服务重启，本轮已中断'))
    const card = page.locator('[data-remedy="suspended"]')
    await card.getByRole('button', { name: '继续运行' }).click()
    const clock = page.locator('[data-turn="to1"] [title="已运行"]')
    await clock.waitFor({ timeout: 6000 }).catch(() => {})
    const text = await clock.innerText().catch(() => '')
    check('计时从 42 秒接着走，不是从三天前算起', /^00:4\d/.test(text), text)
    await page.screenshot({ path: `${SHOTS}/resumed-${theme}.png` })
    await page.getByRole('button', { name: '停止这一轮' }).click()
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  })

  await section('history', '历史：已取消、续跑被拒、没查库、放宽步数', async () => {
    const { page, ctx, errors } = await open(theme)
    await goto(page, 'c0histf')
    await shows(page, '步数用满的那一轮')
    await page.waitForTimeout(800)
    const turns = (await chatState(page)).byConversation.c0histf
    check('老数据里的「已取消」认成已取消', turns[0].phase === 'cancelled', turns[0].phase)
    const cancelledCard = page.locator('[data-remedy="cancelled"]')
    check('已取消的那一轮没有「继续运行」', await cancelledCard.count() === 1
      && !(await cancelledCard.getByRole('button', { name: '继续运行' }).count()))
    check('失败那一轮按运行状态给「继续运行」', turns[1].runStatus === 'failed'
      && await page.locator('[data-remedy="failed"]').getByRole('button', { name: '继续运行' }).count() === 1)
    const failedCard = await page.locator('[data-turn="tf2"]').innerText()
    // 说法和记录页同一份（lib/explain）：它把超时归成「等待超时」，原话收进技术细节
    check('失败原因说人话、不露 Python 类名', /超时/.test(failedCard) && !failedCard.includes('KeyError'),
      failedCard.split('\n').slice(0, 6).join(' / '))
    // 人话只翻一遍：翻好的「操作超时：查询超时…」再翻一遍，标题和原因会各写一次「操作超时」
    check('报错标题和原因不重复', !failedCard.includes('操作超时：'), failedCard.split('\n').slice(2, 6).join(' / '))
    const rejected = page.locator('[data-turn="tf5"] + * [data-remedy="failed"]')
    check('人驳回的失败：不给「继续运行」（原样续上只会再被驳回）', await rejected.count() === 1
      && !(await rejected.getByRole('button', { name: '继续运行' }).count()), (await rejected.innerText().catch(() => '')).replace(/\n/g, ' / '))
    check('没查库的老答案标出来了', await shows(page, '本条未查询数据库'))
    // 流里的报错照 store 拆好的那份画（3C REQ-12）：以前交的是原话，流拿它再讲一遍，当时记下的
    // 标题、原因、怎么办全被换掉
    const authErr = page.locator('[data-turn="tf6"] [data-turn-error]')
    const authText = (await authErr.innerText().catch(() => '')).replace(/\n/g, ' / ')
    check('落库的失败照原样画：标题、原因、怎么办都是当时记下的那份，只出现一次',
      authText.startsWith('当时记下的标题：这把密钥被拒了') && authText.includes('当时记下的原因')
        && authText.includes('当时记下的怎么办') && authText.split('当时记下的标题').length === 2, authText.slice(0, 120))
    check('老失败没记 fix：按原话认出该去模型接入，给直达入口',
      await authErr.locator('a[data-fix="settings"][href="/settings/providers"]').count() === 1)
    const stepBtn = page.getByRole('button', { name: /放宽步数后重新运行（12 → 24 步）/ })
    check('步数用满给出「放宽步数后重新运行」并写明目标值', await stepBtn.count() === 1)

    await page.locator('[data-turn="tf1"]').scrollIntoViewIfNeeded()
    await page.screenshot({ path: `${SHOTS}/history-${theme}.png` })
    await page.locator('[data-remedy="failed"]').getByRole('button', { name: '继续运行' }).click()
    check('续跑被拒时说人话', await shows(page, '无需继续运行'))
    await page.waitForTimeout(400)
    await page.screenshot({ path: `${SHOTS}/continue-refused-${theme}.png` })

    const startsBefore = log.runStarts.length
    await stepBtn.click()
    check('放宽步数：原图直接重跑，步数调到 24', await page.waitForFunction(
      (n) => window.__chat.getState().byConversation.c0histf.at(-1).phase === 'done'
        && window.__chat.getState().byConversation.c0histf.at(-1).attempts?.length === 1, startsBefore, { timeout: 8000 })
      .then(() => true, () => false))
    const asked = log.turnStarts.filter((b) => b.retry_of === 'tf4').at(-1)
    check('放宽步数的请求：带着这一轮、方式和目标步数（mode=max_steps、max_steps=24），图由服务端改',
      asked?.conversation_id === 'c0histf' && asked?.mode === 'max_steps' && asked?.max_steps === 24 && !asked?.graph, JSON.stringify(asked))
    const started = log.runStarts.slice(startsBefore).at(-1)
    check('发起的图里 agent 步数是 24', started?.graph?.nodes?.find((n) => n.type === 'agent')?.data?.config?.max_steps === 24,
      JSON.stringify(started?.graph?.nodes?.map((n) => n.data?.config)))
    // 口径账本的来源链（next-round 02 做法 9）：运行记下属于哪段对话、哪一轮（服务端核对后封存进运行来源）
    const server = await page.evaluate(() => window.__chat.getState().byConversation.c0histf.at(-1).serverId)
    check('发起时带上这一轮属于哪段对话、哪一轮', started?.origin_ref?.conversation_id === 'c0histf'
      && !!server && started?.origin_ref?.turn_id === server, JSON.stringify(started?.origin_ref))
    check('上一次留档：第 1 次尝试 · 结论不可用', await shows(page, '第 1 次尝试'))
    await page.screenshot({ path: `${SHOTS}/retried-${theme}.png` })
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  })

  await section('rejected', '被驳回：补救区和列表写「被驳回」，重新运行后「之前的尝试」不写「已手动停止」', async () => {
    ctl.rejectConvs = true
    try {
      const { page, ctx, errors } = await open(theme)
      await goto(page, 'c0rejr')
      await shows(page, '这个月的退款有多少')
      await page.waitForTimeout(500)
      const turn0 = (await chatState(page)).byConversation.c0rejr?.[0]
      check('刷新之后照样认成被驳回（相位 cancelled，带着驳回的那一句）', turn0?.phase === 'cancelled'
        && turn0?.rejected?.sub === '张工 驳回了「查数」：数字对不上', JSON.stringify({ phase: turn0?.phase, rejected: turn0?.rejected }))
      const card = page.locator('[data-remedy="cancelled"]')
      const cardText = (await card.innerText().catch(() => '')).replace(/\n/g, ' / ')
      check('补救区说是审批人驳回、谁在哪一步驳回，不说「你已停止本轮」', cardText.includes('被审批人驳回')
        && cardText.includes('张工 驳回了「查数」：数字对不上') && !cardText.includes('你已停止本轮'), cardText)
      check('被驳回的没有「继续运行」', !(await card.getByRole('button', { name: '继续运行' }).count()))
      // 过程里被驳回的那一步（node.failed 带 reason=rejected）：停止的剪影、中性色、行尾写「被驳回」，
      // 不是绿色的「完成」勾，也不是失败的红三角（总方案 X9、细案 5.3「各处都写被驳回」）
      await page.locator('[data-turn]').first().getByRole('button', { name: '查看执行过程' }).click()
      const stepRow = page.locator('[data-turn]').first().locator('[data-node-id="ag"][data-step-status]').first()
      await stepRow.waitFor({ timeout: 4000 }).catch(() => {})
      const look = await stepRow.evaluate((el) => {
        const badge = el.querySelector('svg[data-status]')
        const line = el.querySelector('button')
        return { status: el.dataset.stepStatus, badge: badge?.getAttribute('data-status') ?? null,
          shape: badge?.getAttribute('data-shape') ?? null, badgeColor: badge ? getComputedStyle(badge).color : null,
          done: getComputedStyle(document.documentElement).getPropertyValue('--st-done').trim(),
          lineColor: line ? getComputedStyle(line).color : null, text: el.innerText.replace(/\n/g, ' / ') }
      }).catch((e) => ({ error: String(e) }))
      check('过程里被驳回的那一步：状态 rejected，剪影是停止、不是完成的勾', look.status === 'rejected'
        && look.badge === 'rejected' && look.shape === 'stop', JSON.stringify(look))
      const doneRgb = await page.evaluate(() => {
        const probe = document.createElement('span')
        probe.style.color = 'var(--st-done)'
        document.body.appendChild(probe)
        const c = getComputedStyle(probe).color
        probe.remove()
        return c
      })
      check('……图标不是绿色（完成的颜色）', !!look.badgeColor && look.badgeColor !== doneRgb, `${look.badgeColor} vs ${doneRgb}`)
      check('……副标题写谁驳回、备注，行尾写「被驳回」，不写「失败」', (look.text ?? '').includes('张工 驳回：数字对不上')
        && (look.text ?? '').includes('被驳回') && !(look.text ?? '').includes('失败'), look.text)
      if (theme === 'dark') await page.screenshot({ path: `${SHOTS}/rejected-step-${theme}.png` })
      const aside = page.locator('aside[aria-label="对话列表"]')
      const rowText = async (t) => (await aside.locator('div.group', { hasText: t }).innerText().catch(() => '')).replace(/\n/g, ' / ')
      // 标题里不带「被驳回」：标出来的是状态，不是标题里的字
      check('列表里打开过的：标「被驳回」', (await rowText('会话：退款那一问')).includes('被驳回'), await rowText('会话：退款那一问'))
      check('列表里没打开过的：按 last_status 标「被驳回」，不标「失败」', (await rowText('没打开过的那一问')).includes('被驳回')
        && !(await rowText('没打开过的那一问')).includes('失败'), await rowText('没打开过的那一问'))

      const startsBefore = log.runStarts.length
      await card.getByRole('button', { name: '重新运行本轮' }).click()
      check('重新运行：留下一次尝试', await page.waitForFunction(
        () => window.__chat.getState().byConversation.c0rejr.at(-1).attempts?.length === 1, null, { timeout: 8000 })
        .then(() => true, () => false))
      check('按原图重新发起了运行', log.runStarts.length > startsBefore, `${log.runStarts.length - startsBefore} 次`)
      const [attempt] = (await chatState(page)).byConversation.c0rejr.at(-1).attempts ?? []
      check('那一次记成被驳回、写谁在哪一步驳回', attempt?.outcome === 'rejected'
        && attempt?.summary === '被驳回：张工 驳回了「查数」：数字对不上', JSON.stringify(attempt))
      const history = page.locator('[aria-label="之前的尝试"]')
      const historyText = (await history.innerText().catch(() => '')).replace(/\n/g, ' / ')
      check('「之前的尝试」写「被驳回」，不写「已手动停止」', historyText.includes('第 1 次尝试')
        && historyText.includes('被驳回：张工 驳回了「查数」') && !historyText.includes('已手动停止'), historyText)
      check('没有运行时报错', errors.length === 0, errors[0] ?? '')
      await ctx.close()
    } finally {
      ctl.rejectConvs = false
    }
  })

  await section('ledger', '口径账本：轮次底部「正在比对口径」→ 摘要、按口径正式出具、对账（写请求在浏览器层拦下）', async () => {
    ctl.ledgerConvs = true
    try {
      const { page, ctx, errors } = await open(theme)
      // 文档记着的运行要和成果的运行对得上（run-led），片段、证据图按这次运行答
      await page.route(/\/api\/artifacts\//, (route) => route.fulfill({ json: { id: EVIDENCE.doc_artifact,
        content: { ...EVIDENCE.doc, run_id: 'run-led' } } }))
      await page.route(/\/api\/runs\/run-led\/evidence(\/.*)?(\?.*)?$/, (route) => {
        const m = new URL(route.request().url()).pathname.match(/\/segments\/([^/]+)$/)
        return m ? route.fulfill({ json: EVIDENCE.segments[decodeURIComponent(m[1])] ?? {} }) : route.fulfill({ json: EVIDENCE.graph })
      })
      // 账本：头两次答 pending（正在比对），之后比对过了：整题等价于受管口径「周报口径」v2，另有一条查询可以对账
      const led = { gets: [], writes: [], issued: [] }
      await page.route(/\/api\/runs\/run-led\/ledger$/, (route) => {
        led.gets.push(Date.now())
        if (led.gets.length <= 2) return route.fulfill({ json: { run_id: 'run-led', state: 'pending', note: '封存后比对，不在封存范围内' } })
        return route.fulfill({ json: ledgerOf('run-led', { labels: WHOLE_LABELS, whole: WHOLE, actions: [ISSUE, RECONCILE],
                                                            issued: led.issued }) })
      })
      await page.route(/\/api\/ledger\//, (route) => {
        const req = route.request()
        const path = new URL(req.url()).pathname
        let body = null
        try { body = req.postDataJSON() } catch { body = null }
        led.writes.push({ key: `${req.method()} ${path}`, body })
        if (req.method() === 'POST' && path === '/api/ledger/answers/run-led/issue') {
          led.issued = [{ run_id: 'run-iss-1', status: 'succeeded', end_reason: null, workflow_id: 'wf-weekly', version: 2,
                          tier: 'formal', tier_text: '完整出具', auto: false, created_at: iso(0) }]
          return route.fulfill({ json: { run_id: 'run-iss-1', created: true, issued: led.issued[0] } })
        }
        // 对账：这一段只核对按钮发出的请求体，结果卡片在 check-ledger 里看
        if (req.method() === 'POST' && path === '/api/ledger/answers/run-led/reconcile') {
          return route.fulfill({ status: 409, json: { detail: '检查脚本：不真的发起对账', code: 'ledger_reconcile_refused' } })
        }
        return route.fulfill({ status: 409, json: { detail: '检查脚本不写库' } })
      })
      await goto(page, 'c0ledgr')
      const turnBox = page.locator('[data-turn]').first()
      const waiting = await page.locator('[data-ledger-turn="waiting"]').first().waitFor({ timeout: 8000 }).then(() => true, () => false)
      check('交付了的轮次：比对还没出结果时写「正在比对口径」', waiting
        && (await page.locator('[data-ledger-turn="waiting"]').first().innerText()).includes('正在比对口径'))
      const summary = page.locator('[data-ledger-summary]').first()
      const shown = await summary.waitFor({ timeout: 10000 }).then(() => true, () => false)
      check('按退避再取，比对出结果后写摘要（不靠实时流）', shown && led.gets.length === 3, `${led.gets.length} 次`)
      const gaps = led.gets.slice(1).map((t, i) => t - led.gets[i])
      check('……隔得越来越久（退避），不是每一拍都取', gaps.length === 2 && gaps[0] >= 700 && gaps[1] >= gaps[0] - 100,
        gaps.join('ms, ') + 'ms')
      const text = (await summary.innerText()).replace(/\s+/g, ' ')
      check('摘要：几个数来自哪份口径', text.includes('3 个数来自口径「周报口径」v2（受管）'), text)
      check('整题等价：写等价于哪份口径、怎么认的', (await page.locator('[data-ledger-whole]').first().innerText())
        .includes('整题等价于口径「周报口径」v2（受管） · 等价已证明'))
      await page.waitForTimeout(3500)
      check('比对过了、没有要等的：不再取', led.gets.length === 3, `${led.gets.length} 次`)
      const issueBtn = page.locator('[data-ledger-issue-button]').first()
      check('按服务端的动作摆「按口径正式出具」，下面写为什么不自动', await issueBtn.count() === 1
        && (await page.locator('[data-ledger-auto-reason]').first().innerText()).includes('设置里关了自动按口径出具'))
      check('有相关口径：摆「对账」按钮', (await page.locator('[data-ledger-turn-reconcile]').first().innerText()).includes('对账：「订单口径」v1（已发布）'))
      check('数字上挂着来自口径的小标记', await turnBox.locator('[data-ev-caliber-of="s6"]').count() === 1)

      await issueBtn.click()
      const posted = await until(() => led.writes.some((w) => w.key === 'POST /api/ledger/answers/run-led/issue'), 5000)
      const issueBody = led.writes.find((w) => w.key === 'POST /api/ledger/answers/run-led/issue')?.body
      check('出具的请求只说按哪份口径（参数由服务端从比对里取）', posted
        && JSON.stringify(issueBody) === JSON.stringify({ workflow_id: 'wf-weekly', version: 2 }), JSON.stringify(issueBody))
      const issued = page.locator('[data-ledger-issued="run-iss-1"]').first()
      const issuedShown = await issued.waitFor({ timeout: 6000 }).then(() => true, () => false)
      const issuedText = issuedShown ? (await issued.innerText()).replace(/\s+/g, ' ') : ''
      check('出具的正式运行挂在轮次下面：写档位', issuedText.includes('按口径正式出具') && issuedText.includes('「周报口径」v2（受管）')
        && issuedText.includes('完整出具'), issuedText)
      check('……点开去运行', await issued.locator('a[href="/runs/run-iss-1"]').count() === 1)
      check('出具过了：不再摆出具按钮', await page.locator('[data-ledger-issue-button]').count() === 0)

      await page.locator('[data-ledger-turn-reconcile]').first().click()
      const dialog = page.locator('[data-ledger-reconcile-dialog]')
      check('点「对账」打开对账：差在哪、核对参数', await dialog.waitFor({ timeout: 4000 }).then(() => true, () => false)
        && (await dialog.innerText()).includes('口径只保留状态为已支付的行') && (await dialog.innerText()).includes('参数按这次的 SQL 推断，请核对'))
      await page.locator('[data-ledger-param-input="start"]').fill('2026-09-02')
      await page.locator('[data-ledger-reconcile-launch]').click()
      await until(() => led.writes.some((w) => w.key === 'POST /api/ledger/answers/run-led/reconcile'), 5000)
      const recBody = led.writes.find((w) => w.key === 'POST /api/ledger/answers/run-led/reconcile')?.body
      check('对账的请求：哪条查询、哪份口径、核对过的参数（confirm）', recBody?.query === 'q-orders' && recBody?.workflow_id === 'wf-orders'
        && recBody?.version === 1 && recBody?.confirm === true && recBody?.inputs?.start === '2026-09-02' && !('store' in (recBody?.inputs ?? {})),
        JSON.stringify(recBody))
      check('服务端拒了：照它的话写在对账框里', (await page.locator('[data-ledger-reconcile-problem]').innerText().catch(() => ''))
        .includes('检查脚本：不真的发起对账'))
      check('写请求只有出具、对账这两次（都在浏览器层拦下）', led.writes.length === 2, led.writes.map((w) => w.key).join(' | '))
      await page.screenshot({ path: `${SHOTS}/ledger-${theme}.png` })
      check('没有运行时报错', errors.length === 0, errors[0] ?? '')
      await ctx.close()
    } finally {
      ctl.ledgerConvs = false
    }
  })

  await section('recall', '口径账本：答案身上的召回标记写在轮次底部（已按 vN 核对，不影响；依据有变；有待办时链到待办）', async () => {
    // 召回在原答案的运行上追加 ledger.recalled、ledger.basis_changed（封存后），账本接口的 recall 把它们交出来（02 做法 7）。
    // 以前只在「过程」里点开才看得到，使用者又进不了记录页。没比对的答案（state 不是 matched）照样可能被标依据有变
    ctl.ledgerConvs = true
    try {
      // 比对过的那份另带一次做完的对账（另有两个人说过说明有用）：轮次里卡片上方没有「差在哪」，卡里写一份，反馈问的就是它
      const reconciled = [{ ...reconciledOf('succeeded'), feedback: { mine: null, useful: 2, unclear: 0 } }]
      for (const [name, LED] of [['matched', { ...ledgerOf('run-led', { labels: WHOLE_LABELS, whole: WHOLE, actions: [], reconciled }),
        recall: RECALL }],
        ['ineligible', { run_id: 'run-led', state: 'ineligible', reason: '这种来源的运行不比对', note: '封存后比对，不在封存范围内',
          recall: { checked: [], basis: [{ kind: 'structure', items: 1, new_errors: 0, at: iso(5) }], tasks: [] } }]]) {
        const { page, ctx, errors } = await open(theme)
        await page.route(/\/api\/artifacts\//, (route) => route.fulfill({ json: { id: EVIDENCE.doc_artifact,
          content: { ...EVIDENCE.doc, run_id: 'run-led' } } }))
        await page.route(/\/api\/runs\/run-led\/evidence(\/.*)?(\?.*)?$/, (route) => {
          const m = new URL(route.request().url()).pathname.match(/\/segments\/([^/]+)$/)
          return m ? route.fulfill({ json: EVIDENCE.segments[decodeURIComponent(m[1])] ?? {} }) : route.fulfill({ json: EVIDENCE.graph })
        })
        await page.route(/\/api\/runs\/run-led\/ledger$/, (route) => route.fulfill({ json: LED }))
        await page.route(/\/api\/runs\/run-rec-1\/evidence\/metrics\/reconcile\/[^/]+$/, (route) =>
          route.fulfill({ json: metricDetail('run-rec-1', new URL(route.request().url()).pathname.split('/').at(-1)) }))
        await goto(page, 'c0ledgr')
        const marks = page.locator('[data-ledger-turn] [data-ledger-recall]').first()
        const shown = await marks.waitFor({ timeout: 8000 }).then(() => true, () => false)
        const text = shown ? (await marks.innerText()).replace(/\s+/g, ' ') : ''
        if (name === 'matched') {
          check('轮次底部写「已按口径「周报口径」v3 核对，不影响」（核对过两次只写最近那一版）', shown
            && text.includes('已按口径「周报口径」v3 核对，不影响') && !text.includes('v2 核对'), text)
          check('……依据有变：项数相加，写按新目录新出现几条错误级问题', text.includes('依据的数据目录有变（3 项）')
            && text.includes('按新目录检查新出现 1 条错误级问题'), text)
          const link = marks.locator('[data-ledger-recall-task="tk-recall-1"] a')
          check('……有说的是这个答案的召回待办：写数变了、链到待办', text.includes('口径升到 v2，这个答案用到的数变了')
            && await link.getAttribute('href').catch(() => null) === '/apps/inbox/tk-recall-1', text)
          check('……和摘要在同一张账本卡里', await page.locator('[data-ledger-turn="matched"] [data-ledger-recall]').count() === 1)
          const card = page.locator('[data-ledger-turn] [data-ledger-reconciled="run-rec-1"]')
          const where = (await card.locator('[data-ledger-reconciled-where]').innerText().catch(() => '')).replace(/\s+/g, ' ')
          check('轮次里做完的对账卡：写「差在哪」，下面问说明有没有帮助、写各几个人', where.includes('口径只保留状态为已支付的行')
            && (await card.locator('[data-ledger-feedback]').innerText().catch(() => '')).includes('2 人觉得有用')
            && await card.locator('[data-ledger-feedback-give]').count() === 2, where)
          await page.screenshot({ path: `${SHOTS}/ledger-recall-${theme}.png` })
        } else {
          check('不比对的答案：轮次底部只写依据有变（表结构）', shown && text.includes('依据的表结构有变（1 项）')
            && await page.locator('[data-ledger-turn="recall"]').count() === 1 && await page.locator('[data-ledger-summary]').count() === 0, text)
        }
        check(`没有运行时报错（${name}）`, errors.length === 0, errors[0] ?? '')
        await ctx.close()
      }
    } finally {
      ctl.ledgerConvs = false
    }
  })

  await section('fixfirst', '要改的东西在工具库里：先去改参数定义，「接着跑」退成次要', async () => {
    ctl.fixConvs = true
    try {
      for (const [cid, tid, variant] of [['c0fixy', 'ty1', ''], ['c0fixz', 'tfz1', '（后端现在的说法）']]) {
        const { page, ctx, errors } = await open(theme)
        await goto(page, cid)
        const card = page.locator('[data-remedy="failed"]')
        await card.waitFor({ timeout: 6000 }).catch(() => {})
        const fix = card.locator('[data-remedy-fix="tools"]')
        const go = card.getByRole('button', { name: '继续运行' })
        check(`主按钮是「去改参数定义」，直达那个工具的编辑框${variant}`,
          (await fix.innerText().catch(() => '')).includes('去改参数定义')
            && /\bbtn-primary\b/.test(await fix.getAttribute('class').catch(() => '') ?? '')
            && await fix.getAttribute('href').catch(() => null) === '/tools/custom?edit=lookup_order',
          await fix.getAttribute('href').catch(() => '没有这个按钮') ?? '')
        check(`「继续运行」还在，但不是实心的，排在去改的后面${variant}`,
          await go.count() === 1 && !/\bbtn-primary\b/.test(await go.getAttribute('class').catch(() => '') ?? '')
            && await page.evaluate(() => {
              const c = document.querySelector('[data-remedy="failed"]')
              const f = c?.querySelector('[data-remedy-fix]')
              const g = [...(c?.querySelectorAll('button') ?? [])].find((b) => b.textContent?.includes('继续运行'))
              return !!f && !!g && !!(f.compareDocumentPosition(g) & Node.DOCUMENT_POSITION_FOLLOWING)
            }))
        check(`没有别的实心按钮抢主位${variant}`, await card.locator('.btn-primary').count() === 1)
        const streamFix = page.locator(`[data-turn="${tid}"] [data-turn-error] a[data-fix="tools"]`)
        check(`流里的报错也直达那个工具${variant}`, await streamFix.getAttribute('href').catch(() => null) === '/tools/custom?edit=lookup_order'
          && (await streamFix.innerText().catch(() => '')).includes('去改参数定义'))
        await page.screenshot({ path: `${SHOTS}/fixfirst-${cid}-${theme}.png` })
        check(`没有运行时报错${variant}`, errors.length === 0, errors[0] ?? '')
        await ctx.close()
      }
    } finally {
      ctl.fixConvs = false
    }
  })

  await section('build', '建流程就失败：说人话、原文收进技术细节、给补救', async () => {
    const { page, ctx, errors } = await open(theme)
    await goto(page, 'c0buildj')
    await send(page, '一个建流程就失败的问题')
    check('说出发生了什么', await shows(page, '模型返回的工作流结构有误'))
    check('说出怎么办', await shows(page, '请重试，或把需求描述得更具体'))
    const card = await page.locator('[data-turn]').last().innerText()
    check('原始异常不直接摆出来', !card.includes('validation errors'), card.split('\n').slice(0, 5).join(' / '))
    check('原文在「技术细节」里', await page.locator('[data-turn] details', { hasText: '技术细节' }).count() > 0)
    const remedy = page.locator('[data-remedy="failed"]')
    check('没有运行就没有「继续运行」', !(await remedy.getByRole('button', { name: '继续运行' }).count()))
    check('给「重试本轮」', await remedy.getByRole('button', { name: '重试本轮' }).count() === 1)
    await remedy.getByRole('button', { name: '换个说法' }).click()
    const box = page.getByRole('textbox', { name: '向数据提问' })
    await page.waitForTimeout(150)
    const filled = await box.inputValue()
    const focused = await box.evaluate((el) => el === document.activeElement)
    check('「换个说法」把原问题填回输入框并聚焦', filled === '一个建流程就失败的问题' && focused,
      `${filled} · ${focused ? '已聚焦' : '没聚焦'}`)
    await page.screenshot({ path: `${SHOTS}/build-failed-${theme}.png` })
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  })

  await section('catalogpatch', '说了一条数据事实：「建议更新数据目录」卡片，保存撞上 409 能重新载入', async () => {
    const { page, ctx, errors } = await open(theme)
    await goto(page, 'c0emptg')
    ctl.patchConflict = true
    const before = log.catalogPatches.length
    await send(page, 'status=9 表示作废，统计时要排除')
    const card = page.locator('[data-catalog-patch="orders"]')
    check('回答下面有「建议更新数据目录」卡片', await card.waitFor({ timeout: 6000 }).then(() => true, () => false)
      && (await card.innerText()).includes('建议更新数据目录'))
    check('逐项写改前、改后、理由', (await card.locator('[data-patch-before]').innerText()).includes('1=已支付')
      && (await card.locator('[data-patch-after]').innerText()).includes('9=作废')
      && (await card.locator('[data-patch-reason]').innerText()).includes('status=9'))
    const t = (await chatState(page)).byConversation.c0emptg.at(-1)
    check('提案原样记进这一轮的操作流，回答照常交付', t.ops.some((o) => o.op === 'catalog_patch') && t.phase === 'done' && t.noQuery === true)
    await card.locator('[data-patch-save]').click()
    await card.locator('[data-patch-conflict]').waitFor({ timeout: 3000 })
    check('409：提示这张表刚被修改过', (await card.innerText()).includes('这张表刚被修改过'))
    await card.locator('[data-patch-reload]').click()
    await card.locator('[data-patch-save]').waitFor({ timeout: 3000 })
    check('重新载入后改前按最新的目录写', (await card.locator('[data-patch-before]').innerText()).includes('2=已退款'))
    await card.locator('[data-patch-save]').click()
    await card.locator('[data-patch-done]').waitFor({ timeout: 3000 })
    const sent = log.catalogPatches.slice(before)
    check('先带提案的版本，重新载入后带新版本', sent[0]?.if_version === 2 && !!sent[1]?.preview && sent[2]?.if_version === 3,
      JSON.stringify(sent))
    check('保存成功写明第几版、给到数据目录的入口', (await card.locator('[data-patch-done]').innerText()).includes('第 4 版')
      && (await card.locator('[data-patch-open]').getAttribute('href')) === '/data/catalog/src-shop/orders')
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  })

  await section('askback', '助手反问：这一轮记成「助手反问」，问题进历史，输入框提示回答', async () => {
    const { page, ctx, errors } = await open(theme)
    await goto(page, 'c0emptg')
    const patches = log.patches.length
    await send(page, '先问清楚再查上周销售额')
    const t = await page.waitForFunction(() => window.__chat.getState().byConversation.c0emptg?.at(-1)?.phase !== 'planning', null,
      { timeout: 6000 }).then(() => true, () => false)
    const turn = (await chatState(page)).byConversation.c0emptg.at(-1)
    check('只问不改的一轮收尾，不再一直停在「正在理解需求」', t && turn.phase === 'done', turn.phase)
    check('这一轮记成「助手反问」，问题作为这一轮的内容', turn.asked === '销售额按下单日还是支付日算？'
      && turn.output?.answer === turn.asked, JSON.stringify({ asked: turn.asked, output: turn.output }))
    const card = page.locator('[data-turn]').last()
    check('轮次头写「助手反问」，问题摆出来', (await card.innerText()).includes('助手反问')
      && (await card.innerText()).includes('销售额按下单日还是支付日算？'), (await card.innerText()).split('\n').slice(0, 6).join(' / '))
    check('不标「本条未查询数据库」（这不是回答）', !(await card.innerText()).includes('本条未查询数据库'))
    const box = page.getByRole('textbox', { name: '向数据提问' })
    check('输入框提示回答助手的问题', (await box.getAttribute('placeholder')).includes('回答助手的问题'))
    // 那一行由服务端写（答案列是助手问的那句话，meta 标着反问）：页面不 PATCH，照 end 带来的那一行摆
    check('页面不写这一轮的结局；服务端写的那一行 meta 标着反问', log.patches.slice(patches).length === 0
      && !!turn.meta?.asked, JSON.stringify(turn.meta?.asked))
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()

    // 刷新之后：库里的这一轮照样是「助手反问」，不当成没查库的回答
    ctl.askBack = true
    const again = await open(theme)
    await goto(again.page, 'c0askbk')
    const restored = again.page.locator('[data-turn]').last()
    await restored.waitFor({ timeout: 6000 })
    check('刷新后照样标「助手反问」', (await restored.innerText()).includes('助手反问'), (await restored.innerText()).split('\n').slice(0, 4).join(' / '))
    check('……不标「本条未查询数据库」', !(await restored.innerText()).includes('本条未查询数据库'))
    check('……输入框提示回答助手的问题', (await again.page.getByRole('textbox', { name: '向数据提问' }).getAttribute('placeholder'))
      .includes('回答助手的问题'))
    ctl.askBack = false
    await again.ctx.close()
  })

  await section('selfcheck', '服务端已经自查修正过还没过：不再整轮重新生成', async () => {
    const { page, ctx, errors } = await open(theme)
    await goto(page, 'c0emptg')
    const gens = log.gens.length
    await send(page, '自查没修好的问题')
    const done = await page.waitForFunction(() => window.__chat.getState().byConversation.c0emptg?.at(-1)?.phase === 'error', null,
      { timeout: 6000 }).then(() => true, () => false)
    await page.waitForTimeout(500)
    const turn = (await chatState(page)).byConversation.c0emptg.at(-1)
    check('只生成了一次（同一轮的自查修正已经做过，不再整轮重来）', log.gens.length - gens === 1, `${log.gens.length - gens} 次`)
    check('这一轮照实说没过、说明已经修正过', done && turn.failure?.reason?.includes('已按检查结果修正过'), JSON.stringify(turn.failure))
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  })

  await section('emptyretry', '助手只写了思考、一个操作都没给：照服务端的 next=retry 自动重来一次，还不行再照实报', async () => {
    const { page, ctx, errors } = await open(theme)
    await goto(page, 'c0emptg')
    let gens = log.gens.length
    await send(page, '只写了思考：有几家门店')
    const done = await page.waitForFunction(() => window.__chat.getState().byConversation.c0emptg?.at(-1)?.phase === 'done',
      null, { timeout: 6000 }).then(() => true, () => false)
    await page.waitForTimeout(300)
    const turn = (await chatState(page)).byConversation.c0emptg.at(-1)
    check('第一次没给操作：自动重来一次，第二次照常回答', done && log.gens.length - gens === 2
      && String(turn.output?.answer ?? '').includes('3 家'), `${log.gens.length - gens} 次生成 · ${JSON.stringify(turn.output)}`)
    check('重来时交回去的话带着原因（同服务端 retry_instruction 的从零重建措辞）',
      log.gens.at(-1)?.instruction === '只写了思考：有几家门店\n\n（上一次没有成功：模型没有给出任何修改，也没有说明原因。请针对这个原因调整，重新给出完整的工作流。）',
      log.gens.at(-1)?.instruction)
    check('两次建图的用量都记进这一轮', Math.abs((turn.usage?.copilot?.cost_usd ?? 0) - 0.002) < 1e-9, JSON.stringify(turn.usage))
    gens = log.gens.length
    await send(page, '两次都只写了思考：有几家门店')
    const failed = await page.waitForFunction(() => window.__chat.getState().byConversation.c0emptg?.at(-1)?.phase === 'error',
      null, { timeout: 6000 }).then(() => true, () => false)
    await page.waitForTimeout(500)
    const last = (await chatState(page)).byConversation.c0emptg.at(-1)
    check('重来还不行：照实报「助手本轮未完成」，不再试第三次', failed && log.gens.length - gens === 2
      && JSON.stringify(last.failure ?? {}).includes('助手本轮未完成'), `${log.gens.length - gens} 次生成 · ${JSON.stringify(last.failure)}`)
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  })

  await section('decide', '新服务端：照 final.next 和复核的 retry_instruction 走，这一轮的花费 = 助手 + 运行 + 复核', async () => {
    const { page, ctx, errors } = await open(theme)
    await goto(page, 'c0emptg')
    const gens = log.gens.length
    const runs = log.runStarts.length
    const reviews = log.reviews.length
    await send(page, '照服务端的决定：有几家门店')
    const done = await page.waitForFunction(() => {
      const t = window.__chat.getState().byConversation.c0emptg?.at(-1)
      return t?.phase === 'done' && t?.run?.id?.startsWith('run-decide')
    }, null, { timeout: 8000 }).then(() => true, () => false)
    await page.waitForTimeout(500)
    const turn = (await chatState(page)).byConversation.c0emptg.at(-1)
    check('复核判不可用：带着原图重来一次', done && log.gens.length - gens === 2 && log.runStarts.length - runs === 2,
      `${log.gens.length - gens} 次生成、${log.runStarts.length - runs} 次运行`)
    check('重来时交回去的就是服务端给的那句话', log.gens.at(-1)?.instruction === DECIDE_RETRY, log.gens.at(-1)?.instruction)
    check('第二次复核带着这一轮试过的次数', log.reviews.slice(reviews).map((r) => r.attempt).join(',') === '0,1',
      JSON.stringify(log.reviews.slice(reviews)))
    // 服务端写的那一行（end 带来的）
    const last = turn.meta?.usage
    check('meta.usage 记下助手、复核、换下来的运行和这一次运行', !!last && Math.abs(last.copilot?.cost_usd - 0.024) < 1e-9
      && Math.abs(last.review?.cost_usd - 0.003) < 1e-9 && Math.abs(last.earlier?.cost_usd - 0.02) < 1e-9
      && Math.abs(last.run?.cost_usd - 0.02) < 1e-9, JSON.stringify(last))
    const tag = page.locator(`[data-turn="${turn.id}"] [data-turn-cost]`)
    const tagText = await tag.innerText().catch(() => '')
    const title = await tag.getAttribute('title').catch(() => '')
    check('头部写这一轮的花费，悬停分得清助手、运行、复核', tagText.includes('$0.067') && (title ?? '').includes('助手建图')
      && (title ?? '').includes('复核'), `${tagText} | ${title}`)

    // 复核慢过等待上限：答案先交出去，复核这一次的用量晚到了照记（这一轮的花费照实是助手 + 运行 + 复核，复核不落下）
    ctl.lateReview = 900
    // 服务端最多等复核这么久（检查里调短）
    ctl.reviewWaitMs = 200
    const patchesBefore = log.patches.length
    await send(page, '照服务端的决定：复核晚到的那一轮')
    const delivered = await page.waitForFunction(() => {
      const t = window.__chat.getState().byConversation.c0emptg?.at(-1)
      return t?.phase === 'done' && t?.question?.includes('复核晚到')
    }, null, { timeout: 8000 }).then(() => true, () => false)
    const early = (await chatState(page)).byConversation.c0emptg.at(-1)
    check('复核没等到：答案先交出去，这时还没有复核的用量', delivered && !early.usage?.review, JSON.stringify(early.usage))
    await page.waitForTimeout(1200)
    const lateTurn = (await chatState(page)).byConversation.c0emptg.at(-1)
    const lateMeta = lateTurn.meta?.usage
    check('页面不写这一轮的结局', log.patches.slice(patchesBefore).length === 0)
    check('复核晚到：它的用量照记进这一轮（服务端并进那一行的 meta.usage）', Math.abs((lateTurn.usage?.review?.cost_usd ?? 0) - 0.004) < 1e-9
      && Math.abs((lateMeta?.review?.cost_usd ?? 0) - 0.004) < 1e-9 && Math.abs((lateMeta?.copilot?.cost_usd ?? 0) - 0.012) < 1e-9,
      JSON.stringify(lateMeta))
    ctl.lateReview = 0
    ctl.reviewWaitMs = 25_000

    // 只建不跑：autorun 没写 false，next 是 ready，前端照 next 走、不发起运行
    const before = log.runStarts.length
    await send(page, '照服务端的决定，只建不跑')
    const ready = await page.waitForFunction(() => window.__chat.getState().byConversation.c0emptg?.at(-1)?.phase === 'ready',
      null, { timeout: 6000 }).then(() => true, () => false)
    await page.waitForTimeout(300)
    check('next=ready：图摆着等运行，不发起', ready && log.runStarts.length === before, `${log.runStarts.length - before} 次运行`)
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  })

  await section('makeapp', '做成应用：带着这次运行进蓝图，不再跳画布发布', async () => {
    const { page, ctx, errors } = await open(theme)
    const made = []
    const extracted = []
    await page.route('**/api/copilot/from-run**', (route) => { extracted.push(route.request().url()); return route.abort() })
    await page.route(/\/api\/(blueprints|templates)(\/|\?|$)/, (route) => {
      const r = route.request()
      const path = new URL(r.url()).pathname
      if (path === '/api/templates') return route.fulfill({ json: { items: [] } })
      if (path === '/api/blueprints' && r.method() === 'POST') {
        made.push(r.postDataJSON())
        return route.fulfill({ status: 201, json: {
          id: 'bp_from_run', name: 'B 的问题', owner: null, scope: 'shared', status: 'drafting', template_id: 'freeform',
          template_version: 1, source: { kind: 'run', id: 'run-b1' }, conversation_id: null, workflow_id: null, app_id: null,
          last_trial_run_id: null, graph_hash: 'x', joints: {}, copied_from: null, issues: [], created_at: iso(1), updated_at: iso(1),
          spec: { format: 'agentlab.blueprint/1', purpose: { value: 'B 的问题', source: 'assistant', status: 'proposed' },
            template: { id: 'freeform', version: 1 }, data: [], cadence: { value: null, source: 'assistant', status: 'missing' },
            people: [], fields: [], writeback: null, access: { use: [], observe: [], manage: [] }, asks: [] },
        } })
      }
      return route.fulfill({ status: 404, json: { detail: '检查脚本没有这个蓝图' } })
    })
    await goto(page, 'c0idleb')
    const button = page.locator('[data-make-app]').last()
    check('回答完成、有运行的轮次有「做成应用」', await button.waitFor({ timeout: 6000 }).then(() => true, () => false))
    check('关登录（等同管理员）：建共享蓝图', await button.getAttribute('data-make-app') === 'shared')
    await button.click()
    check('建运行起点的蓝图', await until(() => made.length === 1) && made[0].source?.kind === 'run' && made[0].source?.id === 'run-b1'
      && made[0].scope === 'shared', JSON.stringify(made[0]))
    check('进蓝图页，不去画布', await page.waitForURL(/\/blueprints\/bp_from_run$/, { timeout: 5000 }).then(() => true, () => false)
      && !page.url().includes('/studio/'), page.url())
    check('不再提取工作流（from-run）', extracted.length === 0, extracted.join(' | '))
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  })

  await section('approval', '停在审批上：审批卡立刻出现，不等 4 秒轮询', async () => {
    const { page, ctx, errors } = await open(theme)
    ctl.approvals = []
    ctl.pendingApproval = {
      id: 'ap-1', run_id: 'run-wait', node_id: 'ag', mode: 'approve', title: '确认一下再查',
      payload: { title: '确认一下再查' }, status: 'pending', response: {}, created_at: new Date().toISOString(),
      workflow_name: '问数据', node_label: '查数', run_status: 'interrupted', run_class: 'exploratory',
    }
    ctl.interruptedAt = 0
    await goto(page, 'c0waitk')
    await send(page, '一个要审批的问题')
    const seen = await page.locator('[data-approval-slot] button', { hasText: /通过|放行|批准/ }).first()
      .waitFor({ timeout: 6000 }).then(() => true, () => false)
    const lag = ctl.interruptedAt ? Date.now() - ctl.interruptedAt : -1
    // 全局轮询是 4 秒一次；中断时立刻去拿的话，一秒内就该出来
    check('审批卡在收到中断后马上出现（不等 4 秒轮询）', seen && lag >= 0 && lag < 1500, `${lag} ms`)
    await page.waitForFunction(() => window.__chat.getState().byConversation.c0waitk?.at(-1)?.phase === 'waiting',
      null, { timeout: 3000 }).catch(() => {})
    const t = (await chatState(page)).byConversation.c0waitk.at(-1)
    check('这一轮是等待审批，不算占着会话', t.phase === 'waiting'
      && await page.getByRole('button', { name: '停止这一轮' }).count() === 0, t.phase)
    await page.screenshot({ path: `${SHOTS}/waiting-${theme}.png` })
    ctl.approvals = []
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  })

  await section('reconnect', '实时流断了：按收到的最后一个 seq 重连，补齐、不重复（一轮在服务端照跑）', async () => {
    const { page, ctx, errors } = await open(theme)
    await goto(page, 'c0emptg')
    ctl.opGapMs = 120
    const streamsBefore = log.streams.length
    const patchesBefore = log.patches.length
    await send(page, '断线重连：查一下订单和明细')
    await page.waitForFunction(() => (window.__chat.getState().byConversation.c0emptg?.at(-1)?.ops.length ?? 0) >= 2,
      null, { timeout: 6000 }).catch(() => {})
    // 接下来两次连接都直接断掉（网络抖了两下）：这一轮在服务端照跑，页面退避着重连
    ctl.dropStreams = 2
    const ran = await page.waitForFunction(() => {
      const t = window.__chat.getState().byConversation.c0emptg?.at(-1)
      return !!t?.run?.id && t.phase === 'running'
    }, null, { timeout: 12000 }).then(() => true, () => false)
    ctl.opGapMs = 10
    await page.waitForTimeout(300)
    const t = (await chatState(page)).byConversation.c0emptg.at(-1)
    const T = srv.turns.get(t.serverId)
    const afters = log.streams.slice(streamsBefore).filter((x) => x.turn === t.serverId).map((x) => x.after)
    const resumedAt = afters.findIndex((a, i) => i >= 2 && a > 0 && afters[i - 1] === a && afters[i - 2] === a)
    check('断了之后接着连：每次都带上收到的最后一个 seq（断掉的那两次和接上的那一次是同一个）', ran && resumedAt > 0,
      afters.join(','))
    check('从不往回要：重连的 seq 只增不减', afters.every((a, i) => i === 0 || a >= afters[i - 1]), afters.join(','))
    check('补齐：服务端这一轮记下的事件，页面一条不少地收到了', !!T && t.turnSeq === T.events.length,
      `${t.turnSeq} / ${T?.events.length}`)
    const ops = T?.events.filter((e) => e.type === 'op').length ?? -1
    check('不重复：助手的操作一条只收一份', t.ops.length === ops && t.ops.filter((o) => o.op === 'add_node').length === 3,
      `${t.ops.length} / ${ops}`)
    check('画布边收边长到收尾：图有 3 个节点，运行接上了', t.graph?.nodes?.length === 3 && !!t.run?.id)
    check('页面不写这一轮的结局（不 PATCH）', log.patches.slice(patchesBefore).length === 0)
    await page.getByRole('button', { name: '停止这一轮' }).click().catch(() => {})
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  })

  await section('replay', '刷新、关掉再打开：服务端那一轮照跑，页面从这一次尝试的第一条接着播', async () => {
    resetDb()
    ctl.replayConvs = true
    ctl.liveDetail = true
    ctl.replayBuildGo = false
    ctl.replayRunGo = false
    try {
      const patchesBefore = log.patches.length
      let { page, ctx, errors } = await open(theme)
      await goto(page, 'c0replay')
      const startsBefore = log.turnStarts.length
      await send(page, '刷新后接着看：各门店的订单数')
      await page.waitForFunction(() => (window.__chat.getState().byConversation.c0replay?.at(-1)?.ops
        .filter((o) => o.op === 'add_node').length ?? 0) === 2, null, { timeout: 6000 }).catch(() => {})
      const serverId = (await chatState(page)).byConversation.c0replay.at(-1).serverId

      // 刷新：库里那一行是 running、带着服务端的任务（建图中）
      await page.reload()
      await ready(page, () => !!window.__chat, '问数据页的 store（window.__chat）')
      const live = await page.waitForFunction((id) => {
        const t = window.__chat.getState().byConversation.c0replay?.find((x) => x.serverId === id)
        return !!t && (t.phase === 'planning' || t.phase === 'building') && t.ops.filter((o) => o.op === 'add_node').length === 2
      }, serverId, { timeout: 6000 }).then(() => true, () => false)
      const t1 = (await chatState(page)).byConversation.c0replay?.find((x) => x.serverId === serverId)
      check('刷新之后：这一轮还在服务端建图，照那个样子摆（不当成断了）', live, `${t1?.phase} · ${t1?.ops?.length} 条操作`)
      check('……从这一次尝试的第一条回放，已经建出的两步一步不少、一步不重',
        t1?.ops.filter((o) => o.op === 'add_node').length === 2 && t1?.ops.filter((o) => o.op === 'plan').length === 1)
      check('……建图的过程照实时的样子摆着（不是要点开才取的历史轮次）', !t1?.restored
        && await page.locator('[data-turn]').last().locator('button', { hasText: '规划' }).count() > 0)
      check('……刷新没有再发起一次（只发起过一次）', log.turnStarts.length - startsBefore === 1,
        `${log.turnStarts.length - startsBefore} 次`)
      check('……回放从这一次尝试开始（after 是 attempt_seq 之前）', log.streams.some((x) => x.turn === serverId && x.after === 0))

      ctl.replayBuildGo = true
      const running = await page.waitForFunction((id) => {
        const t = window.__chat.getState().byConversation.c0replay?.find((x) => x.serverId === id)
        return t?.phase === 'running' && t.run?.id === 'run-replay' && t.graph?.nodes?.length === 3
      }, serverId, { timeout: 6000 }).then(() => true, () => false)
      check('接着播：建完、服务端发起了运行，画布长全', running)
      await page.screenshot({ path: `${SHOTS}/replay-running-${theme}.png` })
      check('没有运行时报错（刷新后）', errors.length === 0, errors[0] ?? '')
      await ctx.close()

      // 关掉再打开：运行还在跑（停在闸上）
      ;({ page, ctx, errors } = await open(theme))
      await goto(page, 'c0replay')
      const reopened = await page.waitForFunction((id) => {
        const t = window.__chat.getState().byConversation.c0replay?.find((x) => x.serverId === id)
        return t?.phase === 'running' && t.run?.id === 'run-replay' && t.ops.filter((o) => o.op === 'add_node').length === 3
      }, serverId, { timeout: 6000 }).then(() => true, () => false)
      check('关掉再打开：照样接着看（运行中，建图的过程还在）', reopened,
        JSON.stringify((await chatState(page)).byConversation.c0replay?.map((x) => x.phase)))
      check('……运行的过程也接回来了', await page.waitForFunction((id) =>
        (window.__chat.getState().byConversation.c0replay?.find((x) => x.serverId === id)?.events.length ?? 0) >= 2,
      serverId, { timeout: 4000 }).then(() => true, () => false))
      ctl.replayRunGo = true
      const done = await page.waitForFunction((id) =>
        window.__chat.getState().byConversation.c0replay?.find((x) => x.serverId === id)?.phase === 'done',
      serverId, { timeout: 8000 }).then(() => true, () => false)
      check('跑完、复核完：答案出来了', done && await shows(page, '刷新之后接着跑完了：两家门店。'))
      check('页面一个结局都没写（不 PATCH）', log.patches.slice(patchesBefore).length === 0)
      await page.screenshot({ path: `${SHOTS}/replay-done-${theme}.png` })
      check('没有运行时报错（重新打开后）', errors.length === 0, errors[0] ?? '')
      await ctx.close()
    } finally {
      ctl.replayConvs = false
      ctl.liveDetail = false
      ctl.replayBuildGo = true
      ctl.replayRunGo = true
    }
  })

  await section('stopreq', '停止：建图中也停得下，停止请求发给服务端的这一轮', async () => {
    const { page, ctx, errors } = await open(theme)
    await goto(page, 'c0emptg')
    ctl.stopBuildGo = false
    const patchesBefore = log.patches.length
    const startsBefore = log.turnStarts.length
    const runsBefore = log.runStarts.length
    await send(page, '停止测试：各门店的订单数')
    await page.waitForFunction(() => (window.__chat.getState().byConversation.c0emptg?.at(-1)?.ops.length ?? 0) >= 2,
      null, { timeout: 6000 }).catch(() => {})
    const asked = log.turnStarts.slice(startsBefore).at(-1)
    check('发起的请求：这段对话、这个问题，不限数据源时不带 datasource_ids', asked?.conversation_id === 'c0emptg'
      && asked?.question === '停止测试：各门店的订单数' && !asked?.retry_of && !('datasource_ids' in (asked ?? {})), JSON.stringify(asked))
    const t0 = (await chatState(page)).byConversation.c0emptg.at(-1)
    await page.getByRole('button', { name: '停止这一轮' }).click()
    await until(() => log.turnCancels.includes(t0.serverId), 3000)
    check('停止请求发给服务端的这一轮（POST /api/chat/turns/{id}/cancel）', log.turnCancels.at(-1) === t0.serverId,
      `${log.turnCancels.at(-1)} / ${t0.serverId}`)
    await page.waitForTimeout(300)
    const t = (await chatState(page)).byConversation.c0emptg.at(-1)
    check('这一轮收成已取消，不画红', t.phase === 'cancelled' && !t.failure, t.phase)
    ctl.stopBuildGo = true
    await page.waitForTimeout(400)
    const T = srv.turns.get(t0.serverId)
    check('服务端那一轮也停了：不再往下建、不发起运行', T?.job.status === 'cancelled' && log.runStarts.length === runsBefore
      && T.events.filter((e) => e.type === 'op' && e.data.op === 'add_node').length === 2, `${T?.job.status} · ${log.runStarts.length - runsBefore} 次运行`)
    check('建图中停止：补救区说「你在搭建工作流时停止了本轮」', await shows(page, '你在搭建工作流时停止了本轮'))
    check('页面不写这一轮的结局（不 PATCH）', log.patches.slice(patchesBefore).length === 0)
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  })

  await section('steps', '历史轮次的执行过程：点开、收起', async () => {
    const { page, ctx, errors } = await open(theme)
    await goto(page, 'c0idleb')
    await shows(page, 'B 的答案')
    const toggle = page.getByRole('button', { name: /执行过程/ }).first()
    check('默认只显示问题和答案', (await toggle.innerText()).includes('查看执行过程'))
    await toggle.click()
    check('点开后能收起', await page.getByRole('button', { name: '收起执行过程' })
      .waitFor({ timeout: 4000 }).then(() => true, () => false))
    await page.getByRole('button', { name: '收起执行过程' }).click()
    check('收起之后按钮回到「查看执行过程」', await page.getByRole('button', { name: '查看执行过程' }).count() === 1)
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  })

  await section('writing', '模型出字：只给计数和思考的最后一句，不进 events', async () => {
    for (const reducedMotion of ['no-preference', 'reduce']) {
      const { page, ctx, errors } = await open(theme, { reducedMotion })
      ctl.thinkGo = ctl.writingGo = false
      ctl.writingAt = 0
      await goto(page, 'c0writel')
      await send(page, '一个正在写的问题')
      const head = page.locator('[data-turn]').last()
      if (reducedMotion === 'no-preference') {
        const said = (text) => head.getByText(text, { exact: true }).first()
          .waitFor({ timeout: 8000 }).then(() => true, () => false)
        check('思考时头部写出最后一句完整的话', await said('正在思考：先看看 factory 里有哪些表'))
        await page.waitForTimeout(400)
        check('还在写的半句不上头部', !(await head.getByText('然后按月汇总出勤').count()))
        await page.screenshot({ path: `${SHOTS}/thinking-${theme}.png` })
        ctl.thinkGo = true
        check('半句写完了才换上它', await said('正在思考：然后按月汇总出勤率，再和上个月比'))
      }
      ctl.thinkGo = true
      ctl.writingGo = true
      await until(() => ctl.writingAt > 0, 8000)
      if (reducedMotion === 'reduce') {
        // 关掉动效时一秒写回一次：半秒时还没出现，一秒多一点就有了
        await page.waitForTimeout(Math.max(0, ctl.writingAt + 500 - Date.now()))
        check('关掉动效时计数不逐帧跳（半秒时还没写回）', !(await head.getByText('已生成').count()))
      }
      const shown = await head.getByText('正在撰写 · 已生成 1,234 字').first()
        .waitFor({ timeout: 3000 }).then(() => Date.now() - ctl.writingAt, () => -1)
      check(reducedMotion === 'reduce' ? '关掉动效时一秒左右写回计数' : '出字时头部写「正在撰写 · 已生成 1,234 字」',
        shown >= 0 && (reducedMotion === 'reduce' ? shown >= 800 : shown < 900), `${shown} ms`)
      const t = (await chatState(page)).byConversation.c0writel.at(-1)
      check('token 和思考增量都没进 events', !t.events.some((e) => e.type.startsWith('llm.token') || e.type === 'llm.thinking.delta'),
        t.events.map((e) => e.type).join(','))
      check('答案原文没有提前露出来', !(await head.innerText()).includes('字字字'))
      await page.screenshot({ path: `${SHOTS}/writing-${reducedMotion === 'reduce' ? 'reduced-' : ''}${theme}.png` })
      await page.getByRole('button', { name: '停止这一轮' }).click()
      await page.waitForTimeout(200)
      check('停下之后不再写「正在撰写」', !(await head.getByText('正在撰写').count()))
      check('没有运行时报错', errors.length === 0, errors[0] ?? '')
      await ctx.close()
    }
  })

  await section('meta', '可信度信息刷新之后还在', async () => {
    const { page, ctx, errors } = await open(theme)
    await goto(page, 'c0metam')
    await shows(page, '一线 96.2%')
    await page.waitForTimeout(600)
    const card = page.locator('[data-turn="tm1"]')
    check('出具档位还在', await card.getByText('降档出具').count() > 0)
    check('探索运行的标注还在', await card.getByText('探索运行 · 不进正式归档').count() > 0)
    check('降档的原因还在', await card.getByText('叙述模板渲染为空').count() > 0)
    check('耗时还在', (await card.innerText()).includes('12.3 s'))
    check('这些都是从落库的 meta 读回来的，没有再去问运行', !log.runGets.includes('run-meta'), log.runGets.join(','))
    await page.screenshot({ path: `${SHOTS}/meta-${theme}.png` })
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  })

  await section('list', '会话列表：分组、相对时间、完整标题、筛选、没打开过的也标状态', async () => {
    resetDb()
    const { page, ctx, errors } = await open(theme)
    await goto(page, 'c0idleb')
    const aside = page.locator('aside[aria-label="对话列表"]')
    await aside.getByText('会话 B').first().waitFor({ timeout: 6000 })
    const heads = await aside.locator('section').evaluateAll((els) => els.map((e) => e.getAttribute('aria-label')))
    check('按今天 / 昨天 / 近 7 天 / 更早分组', heads.join(',') === '今天,昨天,近 7 天,更早', heads.join(','))
    const rowA = aside.locator('div.group', { hasText: '会话 A' })
    check('写相对时间', /分钟前|刚刚/.test(await rowA.innerText()), (await rowA.innerText()).replace(/\n/g, ' / '))
    const tip = await aside.locator('div.group', { hasText: '会话 F' }).locator('button').first().getAttribute('title')
    check('悬停能看到完整标题', !!tip && tip.startsWith('会话 F：取消过、失败过、不可用的历史'), tip ?? '')
    // 这几条这次都没打开过，状态只能来自列表接口的 last_status
    const rowText = async (t) => (await aside.locator('div.group', { hasText: t }).innerText()).replace(/\n/g, ' / ')
    check('没打开过的：停在审批上的标「待审批」', (await rowText('会话 K')).includes('待审批'), await rowText('会话 K'))
    check('没打开过的：失败的标「失败」', (await rowText('会话 J')).includes('失败'), await rowText('会话 J'))
    check('没打开过的：服务重启挂起的标「已中断」', (await rowText('会话 I')).includes('已中断'), await rowText('会话 I'))
    check('没打开过的：在跑的标「运行中」', (await rowText('会话 L')).includes('运行中'), await rowText('会话 L'))
    check('正常完成的不标', !/待审批|失败|已中断|运行中/.test(await rowText('会话 M')), await rowText('会话 M'))
    const rowL = aside.locator('div.group', { hasText: '会话 L' })
    await rowL.hover()
    check('列表说它在跑的，也不能删', await rowL.getByRole('button', { name: /删除/ }).isDisabled())
    const box = aside.getByRole('searchbox', { name: '按标题或提问内容搜索对话' })
    await box.fill('长会话')
    const hits = await aside.locator('div.group').allInnerTexts()
    check('筛选只留下匹配的对话', hits.length === 1 && hits[0].includes('会话 C'), hits.map((h) => h.split('\n')[0]).join(' | '))
    await page.screenshot({ path: `${SHOTS}/list-filter-${theme}.png` })
    await box.press('Escape')
    check('Esc 清掉筛选', (await aside.locator('div.group').count()) === Object.keys(CONVS).length)
    await box.fill('没有这样的对话')
    check('没有匹配时说一声', await aside.getByText('没有标题或问题中包含「没有这样的对话」的对话').count() === 1)
    await box.fill('')
    await page.screenshot({ path: `${SHOTS}/list-status-${theme}.png` })
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  })

  await section('filter', '筛选框：有筛选词时不跟着列表变短一起消失', async () => {
    resetDb()
    ctl.few = true
    const { page, ctx, errors } = await open(theme)
    await goto(page, 'c0busya')
    const aside = page.locator('aside[aria-label="对话列表"]')
    const box = aside.getByRole('searchbox', { name: '按标题或提问内容搜索对话' })
    check('8 个对话时有筛选框', await box.waitFor({ timeout: 6000 }).then(() => true, () => false))
    await box.fill('会话 B')
    const row = aside.locator('div.group', { hasText: '会话 B' })
    await row.hover()
    await row.getByRole('button', { name: /删除/ }).click()
    // 老版本删除前还要确认一次
    if (await page.getByRole('dialog').waitFor({ timeout: 800 }).then(() => true, () => false)) {
      await page.getByRole('dialog').getByRole('button', { name: '删除' }).click()
    }
    await until(() => db.archived.has('c0idleb'))
    await page.waitForTimeout(300)
    check('删到 7 个：筛选词还在，筛选框也还在', await box.isVisible().catch(() => false)
      && (await box.inputValue().catch(() => '')) === '会话 B')
    await page.screenshot({ path: `${SHOTS}/filter-shrunk-${theme}.png` })
    // 老版本框已经没了，清不掉：不在这里卡 30 秒，下面几条照样判红
    await box.fill('', { timeout: 2000 }).catch(() => {})
    await page.waitForTimeout(200)
    check('清掉筛选词：剩下 7 个都回来了', (await aside.locator('div.group').count()) === 7,
      String(await aside.locator('div.group').count()))
    check('没有筛选词、不够 8 个时，筛选框收起', !(await box.isVisible().catch(() => false)))
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    ctl.few = false
    resetDb()
    await ctx.close()
  })

  await section('delete', '删除：先进回收站，可以撤销', async () => {
    resetDb()
    const { page, ctx, errors } = await open(theme)
    await goto(page, 'c0idleb')
    await shows(page, 'B 的答案')
    const aside = page.locator('aside')
    const trashBtn = aside.getByRole('button', { name: /^打开回收站/ })
    check('回收站入口写着里面有几个', /2/.test(await trashBtn.innerText().catch(() => '')),
      await trashBtn.innerText().catch(() => '没有入口'))
    const row = page.locator('aside[aria-label="对话列表"] div.group', { hasText: '会话 B' })
    await row.hover()
    await row.getByRole('button', { name: /删除/ }).click()
    // 回收站能找回来：删除不再先弹一次确认，撤销和回收站就是后悔药
    const asked = await page.getByRole('dialog').waitFor({ timeout: 800 }).then(() => true, () => false)
    check('删除不再先弹确认', !asked)
    if (asked) await page.getByRole('dialog').getByRole('button', { name: '删除' }).click()
    await until(() => log.archived.some(([id, b]) => id === 'c0idleb' && b.archived === true))
    check('删除是归档，不是真删', log.archived.some(([id, b]) => id === 'c0idleb' && b.archived === true)
      && !log.deleted.includes('c0idleb'))
    const undo = page.getByRole('button', { name: '撤销' }).first()
    check('给了撤销', await undo.waitFor({ timeout: 4000 }).then(() => true, () => false))
    check('提示里说去了回收站', await page.getByText(/已移到回收站/).count() > 0)
    check('回收站的数跟着变成 3', /3/.test(await trashBtn.innerText().catch(() => '')), await trashBtn.innerText().catch(() => ''))
    await undo.click().catch(() => {})
    await until(() => log.archived.some(([id, b]) => id === 'c0idleb' && b.archived === false))
    await page.waitForTimeout(200)
    check('撤销放回去了', log.archived.some(([id, b]) => id === 'c0idleb' && b.archived === false)
      && await page.locator('aside[aria-label="对话列表"]').getByText('会话 B').count() > 0)
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  })

  await section('trash', '回收站：预览、恢复、彻底删除（要确认）', async () => {
    resetDb()
    const deletedBefore = log.deleted.length
    const { page, ctx, errors } = await open(theme)
    await goto(page, 'c0idleb')
    await shows(page, 'B 的答案')
    await page.locator('aside').getByRole('button', { name: /^打开回收站/ }).click()
    const bin = page.locator('aside[aria-label="回收站"]')
    check('打开回收站', await bin.waitFor({ timeout: 3000 }).then(() => true, () => false))
    const rows = bin.locator('div.group')
    check('列出回收站里的对话', (await rows.count()) === 2, (await rows.allInnerTexts()).map((t) => t.split('\n')[0]).join(' | '))
    check('顶行和页头一样高（48px）', await bin.evaluate((el) => Math.round(el.firstElementChild.getBoundingClientRect().height)) === 48)
    await page.screenshot({ path: `${SHOTS}/trash-${theme}.png` })

    // 点开看一眼：内容照常显示，但不能接着问
    await rows.filter({ hasText: '删掉的对话甲' }).locator('button').first().click()
    await page.waitForURL(/\/chat\/c0trash1$/, { timeout: 5000 }).catch(() => {})
    check('能打开回收站里的对话看内容', await shows(page, '回收站里的答案'))
    check('说清它在回收站里', await shows(page, '该对话在回收站中'))
    await page.getByRole('textbox', { name: '向数据提问' }).fill('回收站里还能问吗')
    check('在回收站里不能接着问', await page.getByRole('button', { name: '发送' }).isDisabled())
    await page.waitForTimeout(800)   // 轮次有入场动画，等它落定再截
    await page.screenshot({ path: `${SHOTS}/trash-preview-${theme}.png` })
    await page.locator('[data-trash-banner]').getByRole('button', { name: '恢复' }).click()
    await until(() => log.archived.some(([id, b]) => id === 'c0trash1' && b.archived === false))
    await page.waitForTimeout(300)
    check('恢复：放回列表，提示条收起，可以接着问', !(await page.locator('[data-trash-banner]').count())
      && !(await page.getByRole('button', { name: '发送' }).isDisabled()))
    check('恢复之后回到对话列表，它在里面', await page.locator('aside[aria-label="对话列表"]')
      .getByText('删掉的对话甲').count() > 0)

    // 彻底删除：要确认，确认后才发 DELETE
    await page.locator('aside').getByRole('button', { name: /^打开回收站/ }).click()
    const left = bin.locator('div.group', { hasText: '删掉的对话乙' })
    await left.hover()
    await left.getByRole('button', { name: /彻底删除/ }).click()
    const dialog = page.getByRole('dialog')
    check('彻底删除前先确认', await dialog.waitFor({ timeout: 3000 }).then(() => true, () => false))
    check('确认框说清无法恢复', (await dialog.innerText().catch(() => '')).includes('无法恢复'))
    await page.waitForTimeout(300)
    await page.screenshot({ path: `${SHOTS}/trash-purge-${theme}.png` })
    await dialog.getByRole('button', { name: '彻底删除' }).click()
    await until(() => log.deleted.length > deletedBefore)
    check('确认后才真删', log.deleted.slice(deletedBefore).join() === 'c0trash2', log.deleted.slice(deletedBefore).join())
    check('回收站空了就说一声', await shows(page, '回收站为空'))
    await bin.getByRole('button', { name: /返回/ }).click()
    check('返回对话列表', await page.locator('aside[aria-label="对话列表"]').waitFor({ timeout: 3000 }).then(() => true, () => false))
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    resetDb()
    await ctx.close()
  })

  await section('readonly', '回收站里只能看：不发起运行，正在跑的不给彻底删除', async () => {
    resetDb()
    ctl.trashMore = true
    ctl.approvals = [{ id: 'ap-bin', run_id: 'run-trash-wait', node_id: 'ag', mode: 'approve', title: '确认一下再查',
      payload: {}, status: 'pending', response: {}, created_at: new Date().toISOString() }]
    const { page, ctx, errors } = await open(theme)
    const counts = () => ({ starts: log.runStarts.length, continues: log.continues.length, gens: log.gens.length, posts: log.posts.length })
    const before = counts()
    await goto(page, 'c0trash3')
    await page.locator('[data-trash-banner]').waitFor({ timeout: 6000 }).catch(() => {})
    // 停在审批上的那一轮要核对完、成了「等待审批」，四种补救才都摆出来
    await page.waitForFunction(() => window.__chat.getState().byConversation.c0trash3?.find((t) => t.id === 'ty1')?.phase === 'waiting',
      null, { timeout: 6000 }).catch(() => {})
    await page.waitForTimeout(400)
    const STARTERS = /继续运行|重试本轮|重新运行本轮|重新问一次|不限数据源重试|放宽步数|^\s*运行\s*$/
    // 补救动作排在轮次卡片外面（AssistantStream 的动作槽），整页找
    const starters = page.locator('button', { hasText: STARTERS })
    const labels = await starters.evaluateAll((bs) => bs.map((b) => `${b.textContent.trim()}${b.disabled ? '' : '（可点）'}`))
    const has = (re) => labels.some((l) => re.test(l))
    check('会发起运行的按钮照样摆着（恢复之后能做什么看得到）',
      has(/^运行$/) && has(/继续运行/) && has(/重新问一次|重试本轮/) && has(/放宽步数/), labels.join(' | '))
    check('……但一个都点不了', labels.length > 0 && labels.every((l) => !l.endsWith('（可点）')), labels.join(' | '))
    const why = await starters.evaluateAll((bs) => [...new Set(bs.map((b) => b.title))])
    check('按钮上说清为什么点不了', why.length === 1 && /回收站/.test(why[0]), why.join(' | '))
    check('换个说法照常能用（它不发起运行）',
      await page.locator('[data-remedy]').getByRole('button', { name: '换个说法' }).first().isEnabled().catch(() => false))
    check('查看执行过程照常能用', await page.getByRole('button', { name: '查看执行过程' }).first().isEnabled().catch(() => false))
    check('停在审批上的：不摆能点「批准」的审批卡，说清恢复之后再处理',
      !(await page.getByRole('button', { name: '批准' }).count())
        && (await page.locator('[data-pending-approval]').innerText().catch(() => '')).includes('恢复对话后'))
    // 按钮之外再绕一道：绕过禁用直接点、直接调 store，也一次运行都发不出去
    await starters.evaluateAll((bs) => bs.forEach((b) => b.click()))
    await page.evaluate(() => {
      const s = window.__chat.getState()
      s.retryTurn('c0trash3', 'ty3')
      s.retryTurn('c0trash3', 'ty2', { rerun: true })
      s.runNow('c0trash3', 'ty0')
      void s.continueTurn('c0trash3', 'ty2')
      s.ask('c0trash3', '回收站里偷偷问一句')
    })
    await page.waitForTimeout(900)
    const after = counts()
    check('一次运行都没发起：不建图、不开轮、不 POST /runs、不继续运行',
      JSON.stringify(after) === JSON.stringify(before), `${JSON.stringify(before)} → ${JSON.stringify(after)}`)
    await page.locator('[data-turn="ty2"]').scrollIntoViewIfNeeded().catch(() => {})
    await page.waitForTimeout(300)
    await page.screenshot({ path: `${SHOTS}/trash-readonly-${theme}.png` })

    // 删掉之后又跑起来的：和列表里一样不给彻底删除
    await page.locator('aside').getByRole('button', { name: /^打开回收站/ }).click()
    const bin = page.locator('aside[aria-label="回收站"]')
    const liveRow = bin.locator('div.group', { hasText: '删掉的对话丁' })
    await liveRow.waitFor({ timeout: 3000 }).catch(() => {})
    check('回收站里标出它在运行', (await liveRow.innerText().catch(() => '')).includes('运行中'),
      (await liveRow.innerText().catch(() => '')).replace(/\n/g, ' / '))
    await liveRow.hover()
    const rowPurge = liveRow.getByRole('button', { name: /彻底删除/ })
    check('正在跑的：行上的彻底删除点不了，说清为什么', await rowPurge.isDisabled().catch(() => false)
      && (await rowPurge.getAttribute('title')) === '该对话正在运行，请先停止再删除', await rowPurge.getAttribute('title').catch(() => ''))
    await liveRow.locator('button').first().click()
    const live = await page.waitForFunction(() => window.__chat.getState().byConversation.c0trash4?.[0]?.phase === 'running',
      null, { timeout: 6000 }).then(() => true, () => false)
    check('打开它：核对发现运行还活着，接了回去', live, (await chatState(page)).byConversation.c0trash4?.[0]?.phase)
    const bannerPurge = page.locator('[data-trash-banner]').getByRole('button', { name: '彻底删除' })
    check('提示条上的彻底删除也点不了', await bannerPurge.isDisabled().catch(() => false))
    const stop = page.getByRole('button', { name: '停止这一轮' })
    check('在回收站里也停得下来', await stop.count() === 1)
    await page.waitForTimeout(300)
    await page.screenshot({ path: `${SHOTS}/trash-running-${theme}.png` })

    // 清空：只删没在跑的；在跑的留下，确认框里先说
    const deletedBefore = log.deleted.length
    await bin.getByRole('button', { name: '清空回收站' }).click()
    const dialog = page.getByRole('dialog')
    await dialog.waitFor({ timeout: 3000 }).catch(() => {})
    const said = await dialog.innerText().catch(() => '')
    check('清空前说清在跑的这次不删', said.includes('删掉的对话丁') && said.includes('正在运行'), said.replace(/\n/g, ' / '))
    await dialog.getByRole('button', { name: '全部删除' }).click().catch(() => {})
    await until(() => log.deleted.length - deletedBefore >= 3, 4000)
    await page.waitForTimeout(300)
    const gone = log.deleted.slice(deletedBefore)
    check('清空：没在跑的都删了，在跑的没删', ['c0trash1', 'c0trash2', 'c0trash3'].every((id) => gone.includes(id))
      && !gone.includes('c0trash4'), gone.join(','))
    check('在跑的那条事件流没被关掉、运行没被丢下', ctl.wsClosed['run-trash-live'] === false
      && (await chatState(page)).byConversation.c0trash4?.[0]?.phase === 'running')
    check('它还在回收站里', await bin.locator('div.group', { hasText: '删掉的对话丁' }).count() === 1)

    // 停下来之后就能删了
    await stop.click().catch(() => {})
    await until(() => log.cancels.includes('run-trash-live'), 3000)
    await page.waitForTimeout(200)
    check('停下之后：彻底删除可以点了', await bannerPurge.isEnabled().catch(() => false))
    await bannerPurge.click().catch(() => {})
    await dialog.waitFor({ timeout: 3000 }).catch(() => {})
    await dialog.getByRole('button', { name: '彻底删除' }).click().catch(() => {})
    await until(() => log.deleted.slice(deletedBefore).includes('c0trash4'), 3000)
    check('确认之后删掉了', log.deleted.slice(deletedBefore).includes('c0trash4'))
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    ctl.trashMore = false
    ctl.approvals = []
    resetDb()
    await ctx.close()
  })

  await section('recheck', '恢复的半路轮次：核对撞上 500 有出路、补交付不计实时、终态落库', async () => {
    const { page, ctx, errors } = await open(theme)
    ctl.run500 = true
    await goto(page, 'c0unkp')
    // 先等这一轮取回来：取回来之前「不在核对」是空洞地成立的
    const settled = await page.waitForFunction(() => {
      const t = window.__chat.getState().byConversation.c0unkp?.[0]
      return !!t && t.hydrated !== 'pending' && t.phase !== 'checking'
    }, null, { timeout: 5000 }).then(() => true, () => false)
    check('核对撞上 500：不再一直停在「正在核对」', settled, (await chatState(page)).byConversation.c0unkp?.[0]?.phase)
    check('说清没核对上', await shows(page, '无法确认本轮的当前状态'))
    const recheck = page.locator('[data-remedy]').getByRole('button', { name: '重新核对' })
    check('给「重新核对」', await recheck.count() === 1)
    check('不给会另起运行的按钮', !(await page.getByRole('button', { name: /重试本轮|重新运行本轮|继续运行/ }).count()))
    check('这时不转圈', !(await page.locator('[data-turn="tp1"] .animate-spin').count()))
    await page.screenshot({ path: `${SHOTS}/recheck-failed-${theme}.png` })
    ctl.run500 = false
    await recheck.click().catch(() => {})
    check('重新核对：按运行的真实状态收尾', await page.waitForFunction(
      () => window.__chat.getState().byConversation.c0unkp?.[0]?.phase === 'cancelled', null, { timeout: 5000 })
      .then(() => true, () => false), (await chatState(page)).byConversation.c0unkp?.[0]?.phase)
    ctl.run500 = true

    // 三天前跑完、交付没赶上：补交付（等复核）期间不按实时运行计时
    ctl.reviewDelay['run-late'] = 1800
    await goto(page, 'c0lateq')
    await page.waitForFunction(() => window.__chat.getState().byConversation.c0lateq?.[0]?.hydrated === 'done', null, { timeout: 5000 }).catch(() => {})
    await page.waitForTimeout(300)
    const mid = (await chatState(page)).byConversation.c0lateq?.[0]
    check('补交付期间不算「运行中」', mid?.phase !== 'running' && mid?.phase !== 'done', mid?.phase)
    const clock = await page.locator('[data-turn="tq1"] [title="已运行"]').allInnerTexts()
    check('补交付期间头部不走实时计时（不会从三天前算起）', clock.length === 0, clock.join(','))
    await page.screenshot({ path: `${SHOTS}/late-delivery-${theme}.png` })
    check('补交付完成', await shows(page, '补交付的答案：三条记录。', 5000))
    delete ctl.reviewDelay['run-late']

    // 停在半路的几轮（这一波之前页面推的老轮次，服务端没有它们的任务）：页面不再自己收尾、不写库，请服务端接着等各自的
    // 那次运行、照常收尾（POST /api/chat/turns mode=resume），结局照服务端写的那一行摆
    ctl.approvals = [{ id: 'ap-t', run_id: 'run-t-wait', node_id: 'ag', mode: 'approve', title: '确认一下',
      payload: {}, status: 'pending', response: {}, created_at: new Date().toISOString() }]
    const patchesBefore = log.patches.length
    const resumesBefore = log.turnStarts.length
    await goto(page, 'c0termr')
    const phaseOf = async (id) => (await chatState(page)).byConversation.c0termr?.find((t) => t.id === id)
    for (let i = 0; i < 80; i++) {
      const turns = (await chatState(page)).byConversation.c0termr ?? []
      if (['tr1', 'tr2', 'tr3', 'tr4', 'tr5', 'tr6'].every((id) => ['cancelled', 'error', 'suspended', 'waiting']
        .includes(turns.find((t) => t.id === id)?.phase))) break
      await page.waitForTimeout(100)
    }
    const resumed = log.turnStarts.slice(resumesBefore).filter((b) => b.mode === 'resume').map((b) => b.retry_of)
    check('停在半路的几轮都交给服务端接着收尾（mode=resume，带着各自这一轮）',
      ['tr1', 'tr2', 'tr3', 'tr4', 'tr5', 'tr6'].every((id) => resumed.includes(id))
        && log.turnStarts.slice(resumesBefore).every((b) => b.conversation_id === 'c0termr'), resumed.join(','))
    check('页面不写这几轮的结局（不 PATCH 轮次）', log.patches.slice(patchesBefore).length === 0,
      JSON.stringify(log.patches.slice(patchesBefore).map((p) => p.turn)))
    const tr = Object.fromEntries(await Promise.all(['tr1', 'tr2', 'tr3', 'tr4', 'tr5', 'tr6'].map(async (id) => [id, await phaseOf(id)])))
    check('在别处取消的：收成已取消', tr.tr1?.phase === 'cancelled', tr.tr1?.phase)
    check('在别处失败的：收成失败，原因说人话', tr.tr2?.phase === 'error' && /鉴权/.test(tr.tr2?.error ?? ''), tr.tr2?.error)
    check('服务重启挂起的：收成中断', tr.tr3?.phase === 'suspended', tr.tr3?.phase)
    check('运行被删掉的：收成「上次未完成」', tr.tr4?.phase === 'error' && tr.tr4?.failure?.title === '上次未完成',
      JSON.stringify(tr.tr4?.failure))
    check('……后端现在的说法（鉴权失败（401））：同样认成鉴权失败', tr.tr6?.phase === 'error'
      && (tr.tr6?.error ?? '').startsWith('模型鉴权失败'), tr.tr6?.error)
    check('还在等审批的：照旧等人（它还没结束）', tr.tr5?.phase === 'waiting', tr.tr5?.phase)
    ctl.approvals = []
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  })

  await section('clipped', '长答案没取全：记进 meta、刷新后补全，2000 字按码点数', async () => {
    const { page, ctx, errors } = await open(theme)
    const patchesBefore = log.patches.length
    await goto(page, 'c0clips')
    await send(page, '一份取不到运行的长答案')
    await page.waitForFunction(() => window.__chat.getState().byConversation.c0clips?.at(-1)?.phase === 'done', null, { timeout: 10000 }).catch(() => {})
    const t = (await chatState(page)).byConversation.c0clips.at(-1)
    // 一轮在服务端跑：交付读的是库里的完整成果，页面 GET 运行失败也不影响，新轮次不会再有「没取全」
    check('服务端按库里的完整成果交付：答案完整、不标「没取全」', !t.clipped && String(t.output?.answer ?? '').length === FULL.length,
      `${t.clipped} · ${String(t.output?.answer ?? '').length} / ${FULL.length}`)
    check('页面不写这一轮的结局（不 PATCH 轮次）', log.patches.slice(patchesBefore).length === 0)

    const runGetsBefore = log.runGets.length
    await goto(page, 'c0clipt')
    await shows(page, '多个成果键的长答案')
    await until(() => log.runGets.slice(runGetsBefore).includes('run-multi'), 4000)
    check('meta 说没取全：一进来就去补，不等滚进视口', log.runGets.slice(runGetsBefore).includes('run-multi'),
      log.runGets.slice(runGetsBefore).join(','))
    await page.waitForFunction(() => String(window.__chat.getState().byConversation.c0clipt?.[0]?.output?.answer ?? '').includes('【完整结尾】'),
      null, { timeout: 4000 }).catch(() => {})
    const tt1 = (await chatState(page)).byConversation.c0clipt[0]
    check('多个成果键的长答案补全了', String(tt1.output?.answer ?? '').includes('【完整结尾】') && !tt1.clipped,
      `${String(tt1.output?.answer ?? '').length} 字 · ${tt1.clipped}`)
    await page.waitForTimeout(300)
    const lost = await page.locator('[data-remedy]', { hasText: '截断为 2000 字' }).count()
    check('恰好 2000 个码点（UTF-16 不是 2000）也认得出被截断、补不回来', lost === 2, `${lost} 处`)
    await page.locator('[data-turn="tt2"]').scrollIntoViewIfNeeded().catch(() => {})
    await page.screenshot({ path: `${SHOTS}/clipped-${theme}.png` })
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  })

  await section('launch', '发起那一下没到服务端：报错分得清标题和原因，补救说真话', async () => {
    // 一轮在服务端跑之后，发起运行是服务端的事，「发起运行的请求没到后端」不会再发生；会断的是发起这一轮的那一下
    //（POST /api/chat/turns）。这时服务端那边还没有这一轮，补救就是原样再问一次
    const { page, ctx, errors } = await open(theme)
    ctl.startFail = true
    ctl.launchFail = false
    await goto(page, 'c0launu')
    await send(page, '一个启动失败的问题')
    const alert = page.locator('[data-turn] [role="alert"]').first()
    await alert.waitFor({ timeout: 8000 }).catch(() => {})
    const title = await alert.locator('.font-medium').first().innerText().catch(() => '')
    check('标题只是「无法连接服务」，原因不糊进粗体', title === '无法连接服务', title)
    const text = await alert.innerText().catch(() => '')
    check('不许诺页面上不存在的自动重试', !text.includes('自动重试'), text.replace(/\n/g, ' / '))
    const retry = page.locator('[data-remedy="failed"]').getByRole('button', { name: '重试本轮' })
    check('给「重试本轮」', await retry.count() === 1)
    check('说出怎么办：连上之后点「重试本轮」', text.includes('重试本轮'), text.replace(/\n/g, ' / '))
    await page.screenshot({ path: `${SHOTS}/launch-failed-${theme}.png` })
    ctl.startFail = false
    const gensBefore = log.gens.length
    const startsBefore = log.turnStarts.length
    await retry.click().catch(() => {})
    await until(() => log.gens.length > gensBefore, 5000)
    const again = log.turnStarts.slice(startsBefore).at(-1)
    check('重试：服务端还没有这一轮，原样再问一次（同一个问题，不带 retry_of）', again?.question === '一个启动失败的问题'
      && !again?.retry_of && again?.conversation_id === 'c0launu', JSON.stringify(again))
    check('这一次照常建图', log.gens.length > gensBefore, `${log.gens.length - gensBefore} 次建图`)
    await page.getByRole('button', { name: '停止这一轮' }).click().catch(() => {})
    ctl.launchFail = true
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  })

  await section('toolmissing', '发起就被拒（绑定的工具在本机不存在）：报错里给直达入口，不给接着跑', async () => {
    const { page, ctx, errors } = await open(theme)
    await goto(page, 'c0launu')
    await send(page, '一个工具不在的问题')
    const alert = page.locator('[data-turn] [data-turn-error]').first()
    await alert.waitFor({ timeout: 8000 }).catch(() => {})
    const text = await alert.innerText().catch(() => '')
    check('标题点名那个工具', text.includes('绑定的工具「db_query__nope」不存在'), text.replace(/\n/g, ' / '))
    check('说清这次运行没有发起、怎么办', text.includes('本次运行未启动') && text.includes('「数据」页接入'), text.replace(/\n/g, ' / '))
    const fix = alert.locator('a[data-fix="data"]')
    check('报错里有直达入口「前往「数据」页接入」（和画布上发起被拒同一套）', await fix.count() === 1
      && (await fix.innerText().catch(() => '')).includes('前往「数据」页接入'))
    check('不给「继续运行」（原样继续运行还是缺这个工具）', await page.getByRole('button', { name: '继续运行' }).count() === 0)
    // 这时还没有运行 id，流里的报错块不会再拿原话认一遍：入口得在 store 里就记进这一轮的 failure，
    // 刷新之后（meta 里的 failure）照样有
    const stored = await page.evaluate(() => (window.__chat.getState().byConversation.c0launu ?? []).at(-1)?.failure ?? null)
    check('入口记进这一轮的 failure（fix / fixTo，不可继续运行）', stored?.fix === 'tools' && stored?.fixTo === '/data'
      && stored?.continuable === false, JSON.stringify(stored))
    await fix.click().catch(() => {})
    await page.waitForURL((u) => u.pathname.startsWith('/data'), { timeout: 5000 }).catch(() => {})
    check('点了去数据页', new URL(page.url()).pathname.startsWith('/data'), page.url())
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  })

  await section('guard', '放宽步数只在对症时给：停滞、预算收的尾不给，跟随默认 100 步的也不给', async () => {
    resetDb()
    ctl.guardConvs = true
    try {
      const stepBtn = (page) => page.getByRole('button', { name: /放宽步数后重新运行/ })
      // 新后端：默认 100 步、硬上限 100
      const fresh = { run: { agent_max_steps: 100 }, limits: { max_agent_steps: 100 } }
      const { page, ctx, errors } = await open(theme, { settings: fresh })
      await goto(page, 'c0gstall')
      await shows(page, '只查了一部分的结论')
      check('连续几步没进展收的尾：不给「放宽步数」（加步数只会原样再撞一次）', await stepBtn(page).count() === 0)
      await goto(page, 'c0gsteps')
      await shows(page, '只查了一部分的结论')
      const eight = stepBtn(page)
      check('节点写死 8 步、步数用满：给「放宽步数」，从 8 放到 16', await eight.count() === 1
        && /8 → 16 步/.test(await eight.innerText()), await eight.innerText().catch(() => '（没有按钮）'))
      await goto(page, 'c0gdflt')
      await shows(page, '只查了一部分的结论')
      check('节点上的 12 跟随默认 100 步、已经到硬上限：不许一个放不宽的数', await stepBtn(page).count() === 0)
      check('没有运行时报错', errors.length === 0, errors[0] ?? '')
      await ctx.close()
    } finally {
      ctl.guardConvs = false
    }
  })

  await section('rerun', '重跑服务重启挂起的那一轮：旧运行顺手取消', async () => {
    const { page, ctx, errors } = await open(theme)
    await goto(page, 'c0suspo')
    await shows(page, '服务重启，本轮已中断')
    const before = log.cancels.length
    const startsBefore = log.runStarts.length
    await page.locator('[data-remedy="suspended"]').getByRole('button', { name: '重新运行本轮' }).click()
    await until(() => log.runStarts.length > startsBefore, 5000)
    await until(() => log.cancels.slice(before).includes('run-susp'), 2000)
    check('挂起的旧运行被取消，不会一直挂在记录里', log.cancels.slice(before).includes('run-susp'), log.cancels.slice(before).join(','))
    const rerun = log.turnStarts.filter((b) => b.retry_of === 'to1').at(-1)
    check('重新运行本轮的请求：带着这一轮、mode=rerun', rerun?.mode === 'rerun' && rerun?.conversation_id === 'c0suspo',
      JSON.stringify(rerun))
    check('新的一次照常发起', log.runStarts.length > startsBefore)
    await page.getByRole('button', { name: '停止这一轮' }).click().catch(() => {})
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  })

  await section('stale', '断在建图阶段的老轮次：页面不替它写库；重试本轮交给服务端', async () => {
    // 这一波之前页面推的老轮次停在 running、又没有运行：建图那条流随页面、随上次的进程一起没了。以前页面按「多久没动静」
    // 替它写回 error；现在服务端启动时整理它们（chat/job.startup），页面只照实摆「上次未完成」，不写库。
    // 服务端还在建图的一轮带着任务（ConversationTurn.job），照它在跑的样子接着播（见「刷新后接着看」那一段）
    const { page, ctx, errors } = await open(theme)
    const patchesBefore = log.patches.length
    await goto(page, 'c0buildx')
    await shows(page, '重试之后二十分钟没动静的问题')
    await page.waitForTimeout(600)
    const turns = (await chatState(page)).byConversation.c0buildx ?? []
    check('页面不替老轮次写库（不 PATCH）', log.patches.slice(patchesBefore).length === 0,
      JSON.stringify(log.patches.slice(patchesBefore).map((p) => p.turn)))
    check('断在建图阶段的老轮次：照实说上次未完成', turns.every((t) => t.phase === 'error' && t.failure?.title === '上次未完成'),
      turns.map((t) => `${t.id}:${t.phase}`).join(','))

    // 重试一次：交给服务端（带着这一轮、mode=retry），这一次的运行挂在这一轮上
    const startsBefore = log.runStarts.length
    const turnStarts = log.turnStarts.length
    await page.locator('[data-remedy="failed"]').last().getByRole('button', { name: '重试本轮' }).click()
    await until(() => log.runStarts.length > startsBefore, 6000)
    const body = log.turnStarts.slice(turnStarts).at(-1)
    check('重试本轮：请求带着这一轮和方式（retry_of、mode=retry）', body?.retry_of === 'tx8' && body?.mode === 'retry'
      && body?.conversation_id === 'c0buildx', JSON.stringify(body))
    const newRun = log.runStarts.at(-1)?.id
    await page.waitForFunction((id) => window.__chat.getState().byConversation.c0buildx?.find((t) => t.id === 'tx8')?.run?.id === id,
      newRun, { timeout: 4000 }).catch(() => {})
    const tx8 = (await chatState(page)).byConversation.c0buildx.find((t) => t.id === 'tx8')
    check('服务端发起的这次运行挂在这一轮上，照常在跑', !!newRun && tx8?.run?.id === newRun && tx8?.phase === 'running',
      `${tx8?.run?.id} · ${tx8?.phase}`)
    check('页面照旧不写库', log.patches.slice(patchesBefore).length === 0)
    await page.getByRole('button', { name: '停止这一轮' }).click().catch(() => {})
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  })

  await section('scope', '限定数据源：点一下只查这个库', async () => {
    const { page, ctx, errors } = await open(theme)
    await goto(page, 'c0emptg')
    await shows(page, '问你的数据')
    const chip = page.locator('[data-scope-chip="ds-plant"]')
    check('首屏的数据源标签可以点', await chip.waitFor({ timeout: 5000 }).then(() => true, () => false))
    check('默认不限', (await chip.getAttribute('aria-pressed')) === 'false')
    await chip.click()
    check('点一下就限定成它', (await chip.getAttribute('aria-pressed')) === 'true')
    await page.waitForTimeout(300)   // 颜色有过渡，等它换完再截
    await page.screenshot({ path: `${SHOTS}/scope-hero-${theme}.png` })
    const gensBefore = log.gens.length
    await send(page, '只查这个库的问题')
    await until(() => log.gens.length > gensBefore, 5000)
    check('请求里带上 datasource_ids', JSON.stringify(log.gens.at(-1)?.datasource_ids) === '["ds-plant"]',
      JSON.stringify(log.gens.at(-1)?.datasource_ids))
    check('这一轮上写着只查了哪个库', await shows(page, '只查 factory'))
    await page.getByRole('button', { name: '停止这一轮' }).click().catch(() => {})
    await page.waitForTimeout(200)

    // 在对话里：输入框左边的范围选择
    await goto(page, 'c0scopv')
    await shows(page, '上一问的答案')
    const picker = page.getByRole('button', { name: /数据源范围/ })
    check('输入框旁有数据源范围', await picker.count() === 1)
    check('默认写「全部数据源」', (await picker.innerText().catch(() => '')).includes('全部数据源'), await picker.innerText().catch(() => ''))
    await picker.click()
    await page.getByRole('checkbox', { name: 'shop' }).check()
    check('选了之后按钮写「只查 shop」', (await picker.innerText().catch(() => '')).includes('只查 shop'), await picker.innerText().catch(() => ''))
    await page.screenshot({ path: `${SHOTS}/scope-picker-${theme}.png` })
    await page.keyboard.press('Escape')
    const before2 = log.gens.length
    await send(page, '在对话里限定的问题')
    await until(() => log.gens.length > before2, 5000)
    check('对话里限定的也带上', JSON.stringify(log.gens.at(-1)?.datasource_ids) === '["ds-mig"]',
      JSON.stringify(log.gens.at(-1)?.datasource_ids))
    await page.getByRole('button', { name: '停止这一轮' }).click().catch(() => {})
    await page.waitForTimeout(200)

    // 认的是机读码，不是那句话的开头：后端换了说法，补救照样是放开范围（3C REQ-20）
    ctl.scope400 = 'reworded'
    await send(page, '限定的库换了说法')
    await shows(page, '这一轮圈定的库一个也找不到了')
    check('换了说法的 400：凭机读码认出来，补救是「不限数据源重试」',
      await page.locator('[data-remedy="failed"]').last().getByRole('button', { name: '不限数据源重试' }).count() === 1)
    await page.waitForTimeout(200)

    // 限定的库被删了、停用了：后端 400，把原因原样说出来
    ctl.scope400 = true
    await send(page, '限定的库已经不在了')
    check('库不在了：说清原因', await shows(page, '限定的数据源都已不可用'))
    await page.screenshot({ path: `${SHOTS}/scope-400-${theme}.png` })
    // 带着原范围的「重试本轮」只会再撞一次 400：补救得是放开范围
    const loose = page.locator('[data-remedy="failed"]').last().getByRole('button', { name: '不限数据源重试' })
    check('库不在了：补救是「不限数据源重试」', await loose.count() === 1)
    const before3 = log.gens.length
    await loose.click().catch(() => {})
    await until(() => log.gens.length > before3, 5000)
    check('不限数据源重试：请求里不再带 datasource_ids', log.gens.length > before3 && !log.gens.at(-1)?.datasource_ids?.length,
      JSON.stringify(log.gens.at(-1)?.datasource_ids))
    check('输入框旁的范围也一起放开', (await picker.innerText().catch(() => '')).includes('全部数据源'),
      await picker.innerText().catch(() => ''))
    await page.getByRole('button', { name: '停止这一轮' }).click().catch(() => {})
    ctl.scope400 = false

    // 库列表没取回来：从上一问接过来的范围照样会发出去，所以照样得摆在眼前
    ctl.sourcesFail = true
    await goto(page, 'c0scopw')
    await shows(page, '上一问的答案 W')
    const picker2 = page.getByRole('button', { name: /数据源范围/ })
    await picker2.waitFor({ timeout: 3000 }).catch(() => {})
    check('库列表没取回来：范围照样摆在输入框旁', (await picker2.innerText().catch(() => '')).includes('只查 factory'),
      await picker2.innerText().catch(() => '没有范围按钮'))
    await picker2.click().catch(() => {})
    const stray = page.locator('[data-scope-stray]')
    check('范围里的库照样列出来，注明状态获取失败', (await stray.innerText().catch(() => '')).includes('状态获取失败')
      && await stray.getByRole('checkbox').isChecked().catch(() => false), await stray.innerText().catch(() => ''))
    await page.waitForTimeout(250)
    await page.screenshot({ path: `${SHOTS}/scope-unconfirmed-${theme}.png` })
    await stray.getByRole('checkbox').uncheck().catch(() => {})
    check('取消勾选之后它还留在原处，按钮写回「全部数据源」', await stray.count() === 1
      && (await picker2.innerText().catch(() => '')).includes('全部数据源'), await picker2.innerText().catch(() => ''))
    await stray.getByRole('checkbox').check().catch(() => {})
    await page.keyboard.press('Escape')
    const before4 = log.gens.length
    await send(page, '库列表没取回来时的问题')
    await until(() => log.gens.length > before4, 5000)
    check('发出去的范围和看得见的一致', JSON.stringify(log.gens.at(-1)?.datasource_ids) === '["ds-plant"]',
      JSON.stringify(log.gens.at(-1)?.datasource_ids))
    await page.getByRole('button', { name: '停止这一轮' }).click().catch(() => {})
    ctl.sourcesFail = false
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  })

  await section('opencanvas', '在画布里打开：画布有没保存的改动时，先问再建，整条路只问一遍', async () => {
    const { page, ctx, errors } = await open(theme)
    const creates = []
    await page.route(/\/api\/workflows(\?.*)?$/, (route) => {
      if (route.request().method() !== 'POST') return route.fallback()
      creates.push(route.request().postDataJSON())
      return route.fulfill({ status: 201, json: { id: 'wf-chat-open', name: '问数据：B 的问题', description: '', graph: GRAPH,
        tags: [], version: 1, is_template: false, status: 'draft', published_version: null, published_by: null, run_count: 0,
        created_at: iso(0), updated_at: iso(0) } })
    })
    await page.route(/\/api\/workflows\/wf-chat-open(\/.*)?(\?.*)?$/, (route) => {
      if (route.request().method() !== 'GET') return route.abort()
      const url = new URL(route.request().url())
      if (url.pathname.endsWith('/versions')) return route.fulfill({ json: [] })
      return route.fulfill({ json: { id: 'wf-chat-open', name: '问数据：B 的问题', description: '', graph: GRAPH, tags: [], version: 1,
        is_template: false, status: 'draft', published_version: null, published_by: null, run_count: 0,
        created_at: iso(0), updated_at: iso(0) } })
    })
    await goto(page, 'c0idleb')
    await shows(page, 'B 的答案')
    // 画布（store 里的那张）有没保存的改动：离开编排页它也还在
    await page.evaluate(() => window.__studio.setState({ dirty: true,
      workflow: { ...(window.__studio.getState().workflow ?? {}), id: 'wf-dirty', name: '没存的那张' } }))
    await page.evaluate(() => {
      window.__asks = 0
      new MutationObserver(() => {
        const d = [...document.querySelectorAll('[role=dialog]')].some((e) => e.textContent?.includes('未保存的改动'))
        if (d && !window.__askOpen) window.__asks += 1
        window.__askOpen = d
      }).observe(document.body, { childList: true, subtree: true })
    })
    const openBtn = page.getByRole('button', { name: '在画布里打开' }).first()
    const ask = page.getByRole('dialog').filter({ hasText: '未保存的改动' })
    await openBtn.click()
    const asked = await ask.waitFor({ timeout: 3000 }).then(() => true, () => false)
    check('先问要不要放弃画布上的改动，还没建工作流', asked && creates.length === 0, `${asked ? '问了' : '没问'} · 建了 ${creates.length} 个`)
    await ask.getByRole('button', { name: '取消' }).click().catch(() => {})
    await page.waitForTimeout(300)
    check('说了取消：不建，留在问数据页', creates.length === 0 && new URL(page.url()).pathname.startsWith('/chat'))
    await openBtn.click()
    await ask.waitFor({ timeout: 3000 }).catch(() => {})
    await ask.getByRole('button', { name: '放弃并切换' }).click().catch(() => {})
    await page.waitForURL((u) => u.pathname === '/studio/wf-chat-open', { timeout: 6000 }).catch(() => {})
    await page.waitForTimeout(400)
    const asks = await page.evaluate(() => window.__asks)
    check('说了放弃：建一个、跳过去，整条路只问这一遍', creates.length === 1 && asks === 2
      && new URL(page.url()).pathname === '/studio/wf-chat-open', `建了 ${creates.length} 个 · 一共问了 ${asks} 遍（取消那次算一遍）`)
    check('建的工作流带上来源：哪次运行、哪段对话、哪一轮（服务端核对）', JSON.stringify(creates[0]?.origin)
      === JSON.stringify({ run_id: 'run-b1', conversation_id: 'c0idleb', turn_id: 'tb1' }), JSON.stringify(creates[0]?.origin))
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  })

  await section('fresh', '数据源列表：旧了就在进页面时重取，别处加的库不用整页刷新', async () => {
    const { page, ctx, errors } = await open(theme)
    await goto(page, 'c0emptg')
    await page.locator('[data-scope-chip="ds-plant"]').waitFor({ timeout: 5000 }).catch(() => {})
    check('先看到的是启动时取回来的两个库', await page.locator('[data-scope-chip]').count() === 2)
    // 别处加了一个库；这一页的列表已经是半分钟以前的了
    ctl.extraSource = true
    await page.evaluate(async () => {
      const url = performance.getEntriesByType('resource').map((e) => e.name).find((n) => n.includes('/src/store/catalog.ts'))
      const { useCatalog } = await import(url ?? '/src/store/catalog.ts')
      useCatalog.setState((s) => ({ loadedAt: { ...s.loadedAt, datasources: Date.now() - 120_000 } }))
    })
    const getsBefore = log.sourceGets
    // 站内跳走再回来：页面重新挂载，不整页刷新
    await page.locator('nav[aria-label="主导航"] a[href="/runs"]').click()
    await page.waitForURL((u) => u.pathname.startsWith('/runs'), { timeout: 5000 }).catch(() => {})
    await page.locator('nav[aria-label="主导航"] a[href^="/chat"]').first().click()
    await page.waitForURL((u) => u.pathname.startsWith('/chat'), { timeout: 5000 }).catch(() => {})
    check('回到问数据页：重新取了一次库列表', await until(() => log.sourceGets > getsBefore, 5000),
      `${log.sourceGets - getsBefore} 次`)
    check('别处刚加的库出现在首屏的标签里', await page.locator('[data-scope-chip="ds-new"]').waitFor({ timeout: 5000 })
      .then(() => true, () => false))
    ctl.extraSource = false
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  })

  await section('composer', '画布助手：「在现有工作流上改 | 从头生成」分段控件', async () => {
    const { page, ctx, errors } = await open(theme)
    const WF = {
      id: 'wf-chat-check', name: '__chat_check_composer__', description: '检查脚本伪造的工作流', graph: GRAPH,
      tags: [], version: 1, is_template: false, status: 'draft', published_version: null, published_by: null,
      run_count: 0, created_at: iso(60), updated_at: iso(60),
    }
    await page.route(/\/api\/workflows(\?.*)?$/, (route) => (route.request().method() === 'GET'
      ? route.fulfill({ json: [WF] }) : route.fallback()))
    await page.route(/\/api\/workflows\/wf-chat-check(\/.*)?(\?.*)?$/, (route) => {
      const url = new URL(route.request().url())
      if (route.request().method() !== 'GET') return route.abort()
      if (url.pathname.endsWith('/versions')) return route.fulfill({ json: [] })
      return route.fulfill({ json: WF })
    })
    await page.goto(`${WEB}/studio/wf-chat-check`)
    await ready(page, () => window.__studio?.getState().workflow?.id === 'wf-chat-check', '画布上伪造的工作流（window.__studio）')
      .catch(() => {})
    const group = page.getByRole('radiogroup', { name: '生成方式' })
    check('有「生成方式」分段控件', await group.waitFor({ timeout: 6000 }).then(() => true, () => false))
    const base = group.getByRole('radio', { name: '在现有工作流上改' })
    const fresh = group.getByRole('radio', { name: '从头生成' })
    check('默认「在现有工作流上改」', (await base.getAttribute('aria-checked')) === 'true'
      && (await fresh.getAttribute('aria-checked')) === 'false')
    const size = await fresh.evaluate((el) => parseFloat(getComputedStyle(el).fontSize)).catch(() => 0)
    check('字不小于 11px', size >= 11, `${size}px`)
    // 从头生成只是不带画布上的图：后端照样垫上之前几轮对话和上一轮的图（studio-m2 终验）。
    // 提示不能说成「整个重新生成」、像是什么都不记得
    const freshTip = await fresh.getAttribute('title') ?? ''
    check('「从头生成」的提示说清之前的对话仍会参考', /忽略画布上的现有工作流/.test(freshTip) && /之前.*对话/.test(freshTip)
      && /参考/.test(freshTip), freshTip)
    await base.focus()
    await page.keyboard.press('ArrowRight')
    check('方向键切到「从头生成」', (await fresh.getAttribute('aria-checked')) === 'true'
      && await fresh.evaluate((el) => el === document.activeElement))
    await page.waitForTimeout(300)   // 底色有过渡，等它换完再截
    await page.screenshot({ path: `${SHOTS}/composer-fresh-${theme}.png` })
    const gensBefore = log.gens.length
    const box = page.getByRole('textbox', { name: '描述要生成或修改的工作流' })
    await box.fill('重新画一张三步的图')
    await box.press('Enter')
    await until(() => log.gens.length > gensBefore, 5000)
    check('从头生成：不带现有的图', log.gens.at(-1)?.base_graph === null, JSON.stringify(log.gens.at(-1)?.base_graph)?.slice(0, 60))
    await page.waitForFunction(() => !window.__studio.getState().copilot.active, null, { timeout: 8000 }).catch(() => {})
    await base.click()
    await box.fill('在最后加一步人工审批')
    await box.press('Enter')
    await until(() => log.gens.length > gensBefore + 1, 5000)
    check('在现有工作流上改：带上现有的工作流', !!log.gens.at(-1)?.base_graph?.nodes?.length)

    // 助手出错：出错的那一轮自己有报错块（怎么办、原文都在），输入框上面不再重复一条
    await page.waitForFunction(() => !window.__studio.getState().copilot.active, null, { timeout: 8000 }).catch(() => {})
    await box.fill('这一句会让建流程就失败')
    await box.press('Enter')
    const card = page.locator('[data-turn-error]', { hasText: '模型返回的工作流结构有误' })
    const shown = await card.first().waitFor({ timeout: 6000 }).then(() => true, () => false)
    const said = await card.first().innerText().catch(() => '')
    check('助手出错：那一轮写出发生了什么和怎么办', shown && said.includes('请重试，或把需求描述得更具体'),
      said.replace(/\n/g, ' / '))
    check('同一句报错只说一遍（输入框上面不再摆一条）', await card.count() === 1
      && await page.locator('[data-copilot-error]').count() === 0,
      `${await card.count()} 块 · 错误条 ${await page.locator('[data-copilot-error]').count()} 条`)
    await page.waitForTimeout(300)
    await page.screenshot({ path: `${SHOTS}/composer-error-${theme}.png` })
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  })
}

await browser.close()
console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 问数据全部通过')
process.exit(failed ? 1 : 0)
