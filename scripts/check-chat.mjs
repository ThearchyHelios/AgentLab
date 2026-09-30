// 问数据页的检查：会话、轮次、运行全部在浏览器这一侧伪造，页面走的是真的
// chat store / ChatPage / AssistantStream，只有后端换成了脚本。
//
// 为什么不真跑：沙箱没有模型密钥，而且要撞上的恰恰是真跑很难稳定复现的那几种
// 时刻——两个会话同时在跑、答案超过 2000 字被事件截断、跑到一半服务重启、
// 历史取得慢或者取失败。这里每一种都由脚本精确地摆出来。
//
// 守的事：
//   1. 忙不忙按会话算：A 在跑时 B 的按钮是「发送」，停止只停 A 自己，删除保护
//      对准正在跑的那个；
//   2. run.finished 的 output 被截断时，显示和落库都用 GET 运行拿到的完整版；
//      llm.token 不进 events；
//   3. 服务重启：stream.end status=interrupted 且没有待审批，这一轮收成
//      「服务重启，本轮已中断」，给「继续运行」，不再转圈；
//   4. 加载中 / 加载失败 / 真的是空的 三种状态分开，加载失败时不能在空态上提问；
//   5. 已取消的、人驳回这类原样续上还会失败的轮次没有「继续运行」，续跑被拒时说人话；
//   6. 1440×900 下长会话的输入框和发送键完整可见，整页不多滚；
//   7. 首屏例句不拿系统表造句；可信度信息刷新后还在，且写进单独的 meta，不夹在 review 里；
//   8. 从库里恢复的半路轮次：核对撞上 500 有出路、补交付不走实时计时、查到的终态落库；
//   9. 长答案没取全要记进 meta，刷新后补全；2000 字按码点数，不按 UTF-16；
//  10. 发起运行失败：报错不糊成一整行粗体，不许诺不存在的自动重试；重跑挂起的轮次
//      顺手取消旧运行；断在建图阶段按最后一次落库算有没有动静，发起运行时 meta 跟上新运行；
//  11. 左栏：没打开过的会话也标得出状态；筛选框在有筛选词时不消失；删掉的会话进回收站，
//      能预览、恢复、彻底删除（要确认）；回收站里只能看，不发起任何运行，正在跑的不给
//      彻底删除（清空也跳过它）；
//  12. 限定数据源：点一下只查这个库，请求里带上 datasource_ids，库不在了说人话、补救是放开范围；
//      库列表没取回来时，从上一问接过来的范围照样看得见；
//  13. 画布助手的「在现有工作流上改 | 从头生成」分段控件；助手出错时同一句报错只说一遍。
//
// 所有写请求都被拦在浏览器里，不碰任何库。
// 跑之前前端得起着（./scripts/dev.sh），默认连 5273。对别的实例（比如一份沙箱拷贝）跑时
// 带上地址：AGENTLAB_WEB=http://localhost:<前端端口> node scripts/check-chat.mjs
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'
import { mkdirSync, readFileSync } from 'node:fs'

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
/** 只在「问数据的证据」那段出现：库里存着一轮带证据的答案，刷新后回运行取 _evidence */
const ASK_CONVS = {
  askZ: conv('c0askz', '问数据：各区域销售额和最大的一单', 1, 60 * 24 * 33),
}
const DETAIL = {
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
}
/**
 * 成果带逐段证据的一轮（报告撰写节点 + _evidence）。夹具是后端 compose_doc 真跑出来的，只用
 * 通用名。复核回的是一次改写：有证据时复核只能加说明，答案不能被换掉
 */
const EVIDENCE = JSON.parse(readFileSync(new URL('../frontend/src/run/__tests__/evidence-doc.json', import.meta.url), 'utf8'))
const RUNS = {
  'run-evid': { status: 'succeeded', output: EVIDENCE.output, error: null },
  'run-evq': { status: 'succeeded', output: EVQ.output, error: null },
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
  deleted: [], gens: [], sourceGets: 0,
}
const ctl = {
  slowMs: 0, failE: true, wsClosed: {}, approvals: [], thinkGo: false, writingGo: false, writingAt: 0,
  /** 列表只给前 8 个：筛选框刚好出现，删一个就不够 8 个了 */
  few: false, run500: true, reviewDelay: {}, launchFail: true, scope400: false,
  /** 回收站里多摆两个（TRASH_MORE） */
  trashMore: false,
  /** 列表里多摆「护栏」那段的三个会话（GUARD_CONVS） */
  guardConvs: false,
  /** 列表里多摆「问数据的证据」那段的会话（ASK_CONVS） */
  askConvs: false,
  /** 列表里多摆「先去改参数定义」那段的会话（FIX_CONVS） */
  fixConvs: false,
  /** 数据源列表取不回来 */
  sourcesFail: false,
  /** 数据源列表里多一个别处刚加的库 */
  extraSource: false,
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
  ...(ctl.fixConvs ? Object.values(FIX_CONVS) : [])]
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
    const turns = typeof DETAIL[id] === 'function' ? DETAIL[id]() : DETAIL[id]
    return json({ ...(known ?? conv('c0new', '新对话', 0, 0)), turns: turns ?? [] })
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

  if (path === '/copilot/generate-stream' && method === 'POST') {
    const b = body()
    log.gens.push(b)
    if (ctl.scope400 && b.datasource_ids?.length) {
      // 和后端 copilot._sources 同一个形状：detail 是一句话，code 是稳定的机读码
      // reworded：后端换了说法，只剩机读码认得出来
      return json({ detail: ctl.scope400 === 'reworded' ? '这一轮圈定的库一个也找不到了：可能已经被删掉或停用'
        : '限定的数据源都已不可用，可能已被删除或停用。请取消限定后重试，或到「数据」页确认数据源存在且已启用', code: 'datasource_scope_empty' }, 400)
    }
    if (String(b.instruction ?? '').includes('建流程就失败')) {
      // 后端的 error 操作是三段：人话、怎么办、原始异常
      const ops = [
        { op: 'heartbeat', phase: 'planning', elapsed_ms: 400 },
        { op: 'error', message: '模型返回的工作流结构有误：第 2 个节点的类型「sql」不存在',
          hint: '请重试，或把需求描述得更具体',
          detail: "2 validation errors for GraphSpec\nnodes.1.type\n  Input should be 'input', 'output', 'llm' [type=literal_error]" },
      ]
      return route.fulfill({ status: 200, contentType: 'text/event-stream',
        body: ops.map((o) => `data: ${JSON.stringify(o)}\n\n`).join('') })
    }
    const ops = [
      { op: 'heartbeat', phase: 'planning', elapsed_ms: 800 },
      { op: 'plan', summary: '查一下再回答' },
      ...GRAPH.nodes.map((n) => ({ op: 'add_node', node: n })),
      { op: 'done', explanation: '一张三步的图' },
      { op: 'final', graph: GRAPH, issues: [], explanation: '一张三步的图' },
    ]
    return route.fulfill({ status: 200, contentType: 'text/event-stream',
      body: ops.map((o) => `data: ${JSON.stringify(o)}\n\n`).join('') })
  }
  if (path === '/copilot/review' && method === 'POST') {
    const wait = ctl.reviewDelay[body().run_id]
    if (wait) await new Promise((r) => setTimeout(r, wait))
    // 老后端的复核不认 _evidence，照样回一次改写
    if (body().run_id === 'run-evid' || body().run_id === 'run-evq') {
      return json({ verdict: 'rewritten', note: '检索降级过，结论请对照原始数据', answer: '改写后的答案：销售额大约四万多',
                    original: body().run_id === 'run-evq' ? EVQ.output.answer : EVIDENCE.output['周报'], retry: false, severity: 'degraded',
                    signals: [{ kind: 'retrieval_degraded', detail: '检索退回关键词', severity: 'degraded' }] })
    }
    return json({ verdict: 'ok', note: '', answer: null, retry: false, severity: '', signals: [] })
  }
  if (path === '/runs' && method === 'POST') {
    const b = body()
    const conversation = String(b.input?.question ?? '')
    // 发起请求根本没到后端（网断了、后端正在重启）
    if (conversation.includes('启动失败') && ctl.launchFail) return route.abort('connectionrefused')
    // 发起就被拒：绑定的工具不存在（后端 runs.TOOL_MISSING，{detail, code} 的形状）
    if (conversation.includes('工具不在')) {
      return json({ detail: '绑定的工具不存在：「查数」（调用工具）绑定的 db_query__nope。请到「数据」页接入，或在节点中重新选择',
        code: 'run_tool_missing' }, 422)
    }
    const id = conversation.includes('整形解析失败') ? (conversation.includes('现在的说法')
      ? (conversation.includes('上游') ? 'run-jsonup-now' : 'run-jsonerr-now')
      : conversation.includes('节点名带超时') ? 'run-jsonslow'
      : conversation.includes('上游') ? 'run-jsonup'
      : conversation.includes('改写') ? 'run-jsonauth' : 'run-jsonerr')
      : conversation.includes('问数据带证据') ? 'run-evq'
      : conversation.includes('带证据') ? 'run-evid'
      : conversation.includes('长答案') ? (conversation.includes('取不到运行') ? 'run-noget' : 'run-long')
      : conversation.includes('审批') ? 'run-wait'
      : conversation.includes('重启') ? 'run-restart'
      : conversation.includes('正在写') ? 'run-writing'
      : conversation.includes('强杀') ? 'run-killed'
      : conversation.includes('步数用满') ? `run-steps-${++runSeq}`
      : `run-busy-${++runSeq}`
    log.runStarts.push({ id, graph: b.graph })
    return json({ id, status: 'queued', run_class: 'exploratory', output: {}, usage: {} })
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
    const id = m[1]
    log.runGets.push(id)
    if (id === 'run-long') return json({ id, status: 'succeeded', run_class: 'exploratory', output: { answer: FULL }, usage: {} })
    if (id === 'run-noget' || (id === 'run-unk' && ctl.run500)) return json({ detail: '服务端错误（500），详情请查看服务日志' }, 500)
    if (id === 'run-unk') return json({ id, status: 'cancelled', run_class: 'exploratory', output: {}, usage: {} })
    if (id.startsWith('run-404')) return json({ detail: '运行不存在' }, 404)
    if (RUNS[id]) return json({ id, run_class: 'exploratory', usage: {}, ...RUNS[id] })
    if (id.startsWith('run-c') || id === 'run-b1') return json({ id, status: 'succeeded', run_class: 'exploratory', output: {}, usage: {} })
    return json({ id, status: 'interrupted', run_class: 'exploratory', output: {}, usage: {} })
  }
  if (path.startsWith('/approvals')) {
    log.approvalGets.push(Date.now())
    const runId = url.searchParams.get('run_id')
    return json(ctl.approvals.filter((a) => !runId || a.run_id === runId))
  }
  m = path.match(/^\/runs\/([^/]+)\/events$/)
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
  ws.onClose(() => { ctl.wsClosed[runId] = true })
  const after = Number(new URL(ws.url()).searchParams.get('after') ?? 0)
  const { events, end } = script(runId, after)
  for (const e of events) {
    if (e.gate) {
      await until(() => ctl[e.gate], 15000)
      continue
    }
    if (e.seq <= after) continue
    if (runId === 'run-writing' && e.type === 'llm.token' && !ctl.writingAt) ctl.writingAt = Date.now()
    // 审批和中断同时产生：后端在停下的那一刻才建出这条待审批
    if (runId === 'run-wait' && e.type === 'run.interrupted') {
      ctl.approvals = [ctl.pendingApproval]
      ctl.interruptedAt = Date.now()
    }
    ws.send(JSON.stringify(e))
    await new Promise((r) => setTimeout(r, 15))
  }
  if (end === 'close') ws.close()
  else if (end) ws.send(JSON.stringify({ type: 'stream.end', status: end, data: { status: end } }))
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
  await page.route(/\/api\/(conversations|copilot\/(generate-stream|review)|runs|approvals|datasources)(\/|\?|$)/, fakeApi)
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
    const saved = log.patches.slice(patchesBefore).filter((p) => typeof p.body.answer === 'string' && p.body.answer)
    check('落库的也是完整答案', saved.length > 0 && saved.every((p) => p.body.answer.length === FULL.length),
      saved.map((p) => p.body.answer.length).join(','))
    // 落库是排队异步发的，交付之后再等它一下
    await until(() => log.patches.slice(patchesBefore).some((p) => p.body.meta))
    const metas = log.patches.slice(patchesBefore).map((p) => p.body.meta).filter(Boolean)
    const meta = metas.at(-1)
    check('可信度信息跟着落库（运行、类别、查库次数）', meta?.runId === 'run-long' && meta?.runClass === 'exploratory',
      JSON.stringify(metas))
    // 后端有了单独的 meta 列：review 只放复核结论，不再夹带
    check('meta 单独写，不再塞进 review', log.patches.slice(patchesBefore)
      .every((p) => !p.body.review || !('meta' in p.body.review)),
      JSON.stringify(log.patches.slice(patchesBefore).map((p) => Object.keys(p.body.review ?? {}))))
    check('完整答案取到了，meta 里没有「没取全」', !meta?.clipped, JSON.stringify(meta?.clipped))
    await page.getByText('展开全部').first().click().catch(() => {})
    check('完整结尾在页面上', await shows(page, '【完整结尾】'))
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
    await until(() => log.patches.slice(patchesBefore).some((p) => p.body.meta))
    const saved = log.patches.slice(patchesBefore)
    check('落库的是报告原文，不是改写', saved.filter((p) => typeof p.body.answer === 'string' && p.body.answer)
      .every((p) => p.body.answer === EVIDENCE.output['周报']), saved.map((p) => String(p.body.answer ?? '').slice(0, 12)).join(' | '))
    check('meta 记下这一轮有证据（刷新后回运行那里取标注）', saved.map((p) => p.body.meta).filter(Boolean).at(-1)?.evidence === true)
    check('落库的复核也不带改写', saved.filter((p) => p.body.review?.verdict).every((p) => !p.body.review.answer))
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
      await until(() => log.patches.slice(patchesBefore).some((p) => p.body.meta))
      const saved = log.patches.slice(patchesBefore)
      check('落库的是报告原文，meta 记下这一轮有证据', saved.filter((p) => typeof p.body.answer === 'string' && p.body.answer)
        .every((p) => p.body.answer === EVQ.output.answer) && saved.map((p) => p.body.meta).filter(Boolean).at(-1)?.evidence === true)
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
    const started = log.runStarts.slice(startsBefore).at(-1)
    check('发起的图里 agent 步数是 24', started?.graph?.nodes?.find((n) => n.type === 'agent')?.data?.config?.max_steps === 24,
      JSON.stringify(started?.graph?.nodes?.map((n) => n.data?.config)))
    check('上一次留档：第 1 次尝试 · 结论不可用', await shows(page, '第 1 次尝试'))
    await page.screenshot({ path: `${SHOTS}/retried-${theme}.png` })
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
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

    // 停在半路的几轮：各自的终态查到之后要落库，下次进来不再核对
    ctl.approvals = [{ id: 'ap-t', run_id: 'run-t-wait', node_id: 'ag', mode: 'approve', title: '确认一下',
      payload: {}, status: 'pending', response: {}, created_at: new Date().toISOString() }]
    const patchesBefore = log.patches.length
    await goto(page, 'c0termr')
    await until(() => ['tr1', 'tr2', 'tr3', 'tr4', 'tr6'].every((id) =>
      log.patches.slice(patchesBefore).some((p) => p.turn === id && p.body.status === 'error')), 6000)
    await page.waitForTimeout(300)
    const saved = (id) => log.patches.slice(patchesBefore).filter((p) => p.turn === id).at(-1)?.body
    check('在别处取消的：落库成已取消', saved('tr1')?.status === 'error' && saved('tr1')?.error === '已取消'
      && saved('tr1')?.meta?.outcome === 'cancelled', JSON.stringify(saved('tr1')))
    check('在别处失败的：落库成失败，原因跟着写', saved('tr2')?.status === 'error' && /鉴权/.test(saved('tr2')?.error ?? ''),
      JSON.stringify(saved('tr2'))?.slice(0, 160))
    check('服务重启挂起的：落库成中断', saved('tr3')?.status === 'error' && saved('tr3')?.meta?.outcome === 'suspended',
      JSON.stringify(saved('tr3'))?.slice(0, 160))
    check('运行被删掉的：落库成「上次未完成」', saved('tr4')?.status === 'error' && saved('tr4')?.error === '上次未完成',
      JSON.stringify(saved('tr4'))?.slice(0, 160))
    check('……后端现在的说法（鉴权失败（401））：同样认成鉴权失败落库', saved('tr6')?.status === 'error'
      && (saved('tr6')?.error ?? '').startsWith('模型鉴权失败'), JSON.stringify(saved('tr6'))?.slice(0, 160))
    check('还在等审批的不落库（它还没结束）', !saved('tr5'), JSON.stringify(saved('tr5')))
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
    check('GET 运行失败：先交截断的那份并标出来', t.clipped === 'partial', t.clipped)
    await until(() => log.patches.slice(patchesBefore).some((p) => p.body.meta?.clipped === 'partial'))
    check('「没取全」写进 meta，刷新后还认得出来', log.patches.slice(patchesBefore).some((p) => p.body.meta?.clipped === 'partial'),
      JSON.stringify(log.patches.slice(patchesBefore).map((p) => p.body.meta?.clipped)))

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

  await section('launch', '发起运行失败：报错分得清标题和原因，补救说真话', async () => {
    const { page, ctx, errors } = await open(theme)
    ctl.launchFail = true
    await goto(page, 'c0launu')
    await send(page, '一个启动失败的问题')
    const alert = page.locator('[data-turn] [role="alert"]').first()
    await alert.waitFor({ timeout: 8000 }).catch(() => {})
    const title = await alert.locator('.font-medium').first().innerText().catch(() => '')
    check('标题只是「无法连接服务」，原因不糊进粗体', title === '无法连接服务', title)
    const text = await alert.innerText().catch(() => '')
    check('不许诺页面上不存在的自动重试', !text.includes('自动重试'), text.replace(/\n/g, ' / '))
    const rerun = page.locator('[data-remedy="failed"]').getByRole('button', { name: '重新运行本轮' })
    check('流程已经搭好：给「重新运行本轮」，不让它重新搭', await rerun.count() === 1)
    check('说出怎么办：连上之后点「重新运行本轮」', text.includes('重新运行本轮'), text.replace(/\n/g, ' / '))
    await page.screenshot({ path: `${SHOTS}/launch-failed-${theme}.png` })
    ctl.launchFail = false
    const gensBefore = log.gens.length
    const startsBefore = log.runStarts.length
    await rerun.click().catch(() => {})
    await until(() => log.runStarts.length > startsBefore, 5000)
    check('重跑：原图直接发起，不再去问助手', log.runStarts.length > startsBefore && log.gens.length === gensBefore,
      `${log.runStarts.length - startsBefore} 次发起 · ${log.gens.length - gensBefore} 次建图`)
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
    check('新的一次照常发起', log.runStarts.length > startsBefore)
    await page.getByRole('button', { name: '停止这一轮' }).click().catch(() => {})
    check('没有运行时报错', errors.length === 0, errors[0] ?? '')
    await ctx.close()
  })

  await section('stale', '断在建图阶段：按最后一次落库算有没有动静；发起运行时 meta 跟上新运行', async () => {
    const { page, ctx, errors } = await open(theme)
    const patchesBefore = log.patches.length
    await goto(page, 'c0buildx')
    await shows(page, '重试之后二十分钟没动静的问题')
    await until(() => log.patches.slice(patchesBefore).some((p) => p.turn === 'tx8'), 3000)
    await page.waitForTimeout(400)
    const mine = () => log.patches.slice(patchesBefore)
    check('两天前问的、两分钟前刚重试过：不当成断了写回 error',
      !mine().some((p) => p.turn === 'tx7' && p.body.status === 'error'), JSON.stringify(mine().filter((p) => p.turn === 'tx7')))
    check('重试之后二十分钟没动静的：认定断了，写回 error',
      mine().some((p) => p.turn === 'tx8' && p.body.status === 'error'), JSON.stringify(mine().filter((p) => p.turn === 'tx8')).slice(0, 200))

    // 重试一次：搭好图时连 meta 一起写（有动静的凭据），发起运行时 meta.runId 换成新运行
    const startsBefore = log.runStarts.length
    const retryAt = log.patches.length
    await page.locator('[data-remedy="failed"]').getByRole('button', { name: '重试本轮' }).click()
    await until(() => log.runStarts.length > startsBefore, 6000)
    const newRun = log.runStarts.at(-1)?.id
    await until(() => log.patches.slice(retryAt).some((p) => p.turn === 'tx8' && p.body.run_id === newRun), 3000)
    const ps = log.patches.slice(retryAt).filter((p) => p.turn === 'tx8').map((p) => p.body)
    const graphSave = ps.find((b) => b.graph)
    check('搭好图时连 meta 一起写，带上此刻', !!graphSave?.meta && Math.abs(Date.now() - (graphSave.meta.at ?? 0)) < 30_000,
      JSON.stringify(graphSave?.meta ?? null)?.slice(0, 120))
    const started = ps.find((b) => b.run_id === newRun)
    check('发起运行：run_id 列和 meta.runId 一起换成新运行，状态是 running',
      !!newRun && started?.meta?.runId === newRun && started?.status === 'running', JSON.stringify(started ?? null)?.slice(0, 160))
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
