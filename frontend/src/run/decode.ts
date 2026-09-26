import type { RunEvent, TeamMember, TeamRound, TeamRun } from '../types'
import type { NodeState, RunPhase } from './trace'
import { formatDuration, formatNumber } from '../lib/format'
import { TYPE_LABEL, issuanceLabel, nodeTypeLabel } from '../lib/terms'

// 泳道数据画布也要用（supervisor 节点要展开成协作矩阵），所以类型放在
// types.ts 里；这里再导出一遍，老引用不用改
export type { TeamMember, TeamRound, TeamRun }

/**
 * 事件 → 人能看懂的步骤。
 *
 * 这是全站唯一的翻译层：画布时间线、助手栏、运行页都用它。分两套翻译的话，
 * 同一次运行在不同地方会读出不同的故事，而用户没法判断哪个是真的。
 *
 * 三条原则：
 *
 * 1. **不回写事件**。事件表是审计凭证——run 终态时对全部已落库事件算
 *    manifest_hash，事后增删改会对不上。翻译只发生在前端内存里。
 * 2. **成对事件合并成一行**。tool.start + tool.end 是同一件事的两个时刻，
 *    拆成两行等于把"查询数据"和"查完了"当成两件事讲。
 * 3. **节点是天然的分组单位**。一个图节点内部可能有好几轮模型调用和工具
 *    调用，它们属于同一个"步骤"——这也正是用户说的"简要思考节点"：
 *    节点本身就是那个节点，里面的动作是它的过程。
 */

export type StepKind =
  | 'node'      // 一个图节点的执行（可能带子步骤）
  | 'think'     // 模型的思考
  | 'llm'       // 模型调用
  | 'query'     // 数据库查询
  | 'schema'    // 看表结构
  | 'tool'      // 其他工具
  | 'code'      // 沙箱代码
  | 'branch'    // 分支走向
  | 'human'     // 人工介入
  | 'issuance'  // 出具判定
  | 'note'      // 日志/提示
  | 'error'
  | 'lifecycle' // 开始/结束

/**
 * cancelled：运行被停下时还没做完的步骤——用户主动停止不是出错，不能画成红色失败。
 * suspended：服务重启时还在跑的步骤，断点还在，可以接着跑。
 * skipped：skip_if 成立、这一步没有执行。和"根本没轮到"是两回事，得留痕。
 */
export type StepStatus = 'running' | 'done' | 'failed' | 'waiting' | 'cancelled' | 'suspended' | 'skipped'

/** 节点的某一次执行。循环体、轮询类节点一次运行里会执行几十上百次 */
export interface Exec {
  /** 第几次执行，从 1 数 */
  n: number
  seq: number
  status: StepStatus
  ms?: number
  /** 所在循环当时是第几轮（后端 node.started.data.iteration，从 1 数） */
  iteration?: number
}

export interface Step {
  id: string
  seq: number
  kind: StepKind
  /** 一句人话，不带技术黑话 */
  title: string
  /** 一行淡色副标题：这次调用前模型在想什么、为什么跳过、从哪接着跑 */
  sub?: string
  /** 展开后看的东西：SQL 原文、工具参数、结果预览、思考全文 */
  detail?: string
  /** 技术细节：原始异常、驱动报错。默认折叠——给排查和复制用，不给人读 */
  raw?: string
  /** 行尾的次要信息：耗时、行数 */
  meta?: string
  /** 耗时（毫秒）。meta 是给人读的字，耗时细条和统计要的是数 */
  ms?: number
  /** 开始时刻（毫秒时间戳）。进行中的行据此走实时计时 */
  startedAt?: number
  status?: StepStatus
  level?: 'info' | 'warn' | 'error'
  nodeId?: string
  /** 工件 id，可下钻到完整证据 */
  artifact?: string
  /** 工具/查询的返回。和 detail 分开：查询的 detail 是 SQL 原文，
   *  两者要同时展示（问什么 + 查到什么），合成一个字段就只能二选一 */
  result?: string
  children?: Step[]
  /** 协作团队的泳道数据。只有 supervisor 节点的顶层 Step 会有 */
  team?: TeamRun
  /** 建图阶段（Copilot 操作流）出来的步骤。问数据页把它和执行步骤排成一列，界面据此分段 */
  stage?: 'plan'
  /** 子步骤属于父节点的第几次执行（从 1 数）。父节点只执行过一次时不用看它 */
  iter?: number
  /** 节点每一次执行的记录。执行过不止一次时，界面按它把子步骤分轮折叠 */
  execs?: Exec[]
  /** 「开始执行」那一行：图里一共有几个节点 */
  total?: number
  /** 查询走的数据源 */
  source?: string
  /** 相邻的同一件事合并成一行后的统计（见 compactSteps） */
  repeat?: { count: number; ms: number[] }
}

// 这两个是流式增量，后端根本不落库（_EPHEMERAL）。单次运行的 delta 量级会
// 把列表淹掉，而且刷新页面后它们不会回来——UI 不能建立在它们之上。
const EPHEMERAL = new Set(['llm.token', 'llm.thinking.delta'])

// 只对开发者有意义、对使用者是噪音的。不是丢弃事件本身（原始事件另有视图），
// 只是不进这条给人看的流。stream.end 是连接层的结束标记，结局另外按它收尾
const SILENT = new Set(['usage'])

const num = (v: unknown): number | undefined =>
  typeof v === 'number' && Number.isFinite(v) ? v : undefined

/** 事件 ts 是秒（带小数）；老数据没有 ts */
const tsMs = (ev: RunEvent): number | undefined => {
  const t = num(ev.ts)
  return t != null && t > 0 ? t * 1000 : undefined
}

/**
 * 耗时，没有可显示内容时给 undefined 而不是空串。
 *
 * 低于 10ms 不显示。一屏全是"0 ms"看着像每步都被精确计时了，实际上只是这些
 * 步骤（输入、成果这类纯赋值节点）根本没花时间——把没有信息量的数字摆出来，
 * 反而把真正慢的那一步淹没了。
 *
 * 给 undefined 而不是 ''：Step.meta 是可选字段，赋成 '' 之后"没测到耗时"和
 * "测到了但不值一提"就分不开了。
 */
const dur = (ms?: number): string | undefined =>
  ms != null && ms >= 10 ? formatDuration(ms) : undefined

/**
 * 这次运行是不是正停在人工介入上。
 *
 * 从步骤推，不要去查审批列表——那是 4 秒轮询一次的，而 run.interrupted
 * 事件是即时到的。用列表的话，中断后会有几秒钟界面说"这次运行已结束"，
 * 而实际上它正等着你点通过。
 */
export function isAwaitingHuman(steps: Step[]): boolean {
  return steps.some(function walk(s): boolean {
    return s.status === 'waiting' || (s.children?.some(walk) ?? false)
  })
}

/** run 到终态时，所有还挂着"进行中"的生命周期行都要收尾。 */
function closeLifecycles(out: Step[], status: StepStatus) {
  out.forEach((s) => {
    if (s.kind === 'lifecycle' && s.status === 'running') s.status = status
  })
}

// -------------------------------------------------------------------------
// 终态收尾：画布（store / trace）和这里共用这一套规则
// -------------------------------------------------------------------------

/**
 * 一次运行停下来时，还挂着"进行中"的东西各自收成什么。
 *
 * 画布和右栏以前各收各的：画布只把 streaming 置假，节点照样转圈、入边的光点
 * 照样流；右栏只收"开始执行"那一行，节点下面的工具、查询还在转，取消还被画成
 * 红色失败。一个假的"运行中"比没有动效更糟，所以规则只写这一份，两边都调它。
 */
export interface Ending {
  phase: RunPhase
  /** 还在跑的节点、步骤、协作成员 */
  running: NodeState
  /** 停在审批上的 */
  waiting: NodeState
  /** "开始执行 / 继续执行"那种生命周期行：这一段执行本身的结局 */
  drive: NodeState
}

/**
 * 这条事件是不是一个结局；不是就返回 null。
 *
 * awaiting：此刻是否停在审批上。服务重启（log code=server_shutdown，或对账时
 * 状态是 interrupted）时，停在审批上的仍然是"等人"，其余的是"挂起、可接着跑"。
 * type 为 stream.end 的是客户端合成的对账事件，data 为 {status, pending?}；
 * pending 是查过审批列表的结论，有它就以它为准。
 *
 * status 也认前端自己的叫法（RunPhase）：画布把 runPhase 写回 run.status，挂起
 * 写的是 suspended（就是"interrupted 且没有待审批"）；waiting 等同有待审批的
 * interrupted。消费方照 decodePhase(events, {status: run.status}) 传进来时，
 * 不认 suspended 的话强杀的运行会一直转圈。
 */
export function endingOf(ev: RunEvent, awaiting: boolean): Ending | null {
  const d: Record<string, any> = ev.data ?? {}
  const status = ev.type === 'stream.end' ? String(d.status ?? '') : ''
  if (status === 'suspended') {
    return { phase: 'suspended', running: 'suspended', waiting: 'suspended', drive: 'suspended' }
  }
  if (ev.type === 'run.finished' || status === 'succeeded') {
    return { phase: 'succeeded', running: 'done', waiting: 'done', drive: 'done' }
  }
  if (ev.type === 'run.failed' || status === 'failed') {
    // 失败的是那一个节点；别的还在跑的是被连带停下的，不是它们自己出错
    return { phase: 'failed', running: 'cancelled', waiting: 'cancelled', drive: 'failed' }
  }
  if (ev.type === 'run.cancelled' || status === 'cancelled') {
    return { phase: 'cancelled', running: 'cancelled', waiting: 'cancelled', drive: 'cancelled' }
  }
  if ((ev.type === 'log' && d.code === 'server_shutdown') || status === 'interrupted'
      || status === 'waiting') {
    const held = status === 'waiting' || (typeof d.pending === 'boolean' ? d.pending : awaiting)
    return held
      ? { phase: 'waiting', running: 'suspended', waiting: 'waiting', drive: 'suspended' }
      : { phase: 'suspended', running: 'suspended', waiting: 'suspended', drive: 'suspended' }
  }
  return null
}

export interface PhaseState {
  phase: RunPhase
  /** 有没有停在审批上 */
  awaiting: boolean
}

/**
 * 相位的唯一推导：一条事件之后运行处在哪个相位。
 *
 * 画布的 runPhase（trace.ts）和右栏、运行页的 decodePhase 都走它。以前等待审批
 * 时标题写"正在执行…"、工具栏还挂着一个点了必然 409 的"停止"，根子就是四处
 * 各算各的。
 */
export function nextPhase(s: PhaseState, ev: RunEvent): PhaseState {
  const type = String(ev.type)
  if (EPHEMERAL.has(type)) return s
  if (type === 'run.started' || type === 'run.resumed') return { phase: 'running', awaiting: false }
  if (type === 'run.interrupted') return { phase: 'waiting', awaiting: true }
  const ending = endingOf(ev, s.awaiting)
  if (ending) return { phase: ending.phase, awaiting: ending.phase === 'waiting' }
  // 从半路接上的事件流（没有 run.started）：有节点动了就是在跑
  if ((s.phase === 'idle' || s.phase === 'queued') && type.startsWith('node.')) {
    return { phase: 'running', awaiting: s.awaiting }
  }
  return s
}

/** 客户端知道、事件里没有的结局：GET /runs/{id} 查到的状态，以及有没有待审批 */
export interface RunFinal {
  status?: string
  pending?: boolean
}

const finalEvent = (final: RunFinal): RunEvent => ({
  seq: 0, type: 'stream.end', node_id: null, ts: 0,
  data: { status: final.status, ...(final.pending != null ? { pending: final.pending } : {}) },
})

/**
 * 这次运行此刻的相位。运行面板标题、运行页卡头读它，不再从 streaming 猜：
 * 等待审批时不能写"正在执行…"。
 *
 * final 给的是事件之外的事实：服务被强杀时连 server_shutdown 都不会发，只有
 * 查运行状态（interrupted 且没有待审批）才知道它其实挂起了。
 */
export function decodePhase(events: RunEvent[], final?: RunFinal): RunPhase {
  let s: PhaseState = { phase: 'idle', awaiting: false }
  for (const ev of events) s = nextPhase(s, ev)
  if (final?.status) s = nextPhase(s, finalEvent(final))
  return s.phase
}

const STEP_OF: Partial<Record<NodeState, StepStatus>> = {
  done: 'done', failed: 'failed', waiting: 'waiting', cancelled: 'cancelled',
  suspended: 'suspended', running: 'running', skipped: 'skipped',
}
const stepStatusOf = (state: NodeState): StepStatus => STEP_OF[state] ?? 'done'

/**
 * 把还在进行中的步骤按结局收掉，递归到子步骤——节点下面的查询、工具、协作成员
 * 那几行以前一直转圈，因为只收了顶层的生命周期行。
 */
function settleSteps(steps: Step[], ending: Ending): void {
  for (const s of steps) {
    if (s.status === 'running') {
      s.status = stepStatusOf(s.kind === 'lifecycle' ? ending.drive : ending.running)
    } else if (s.status === 'waiting' && ending.waiting !== 'waiting') {
      s.status = stepStatusOf(ending.waiting)
      // 不再是待办：琥珀色的"等你确认"留着，会让人以为还能去点
      if (s.level === 'warn') s.level = undefined
    }
    // 分轮折叠的轮次头读的是 execs：最后那一轮不收的话，节点收了、轮次还在转
    s.execs?.forEach((x) => {
      if (x.status === 'running') x.status = stepStatusOf(ending.running)
      else if (x.status === 'waiting' && ending.waiting !== 'waiting') x.status = stepStatusOf(ending.waiting)
    })
    if (s.children) settleSteps(s.children, ending)
    if (s.team) s.team = settleTeam(s.team, ending)
  }
}

/**
 * 协作团队的收尾：还在跑的成员按结局收掉。画布（store 里的 runtime.team）和
 * 这里调同一个函数，免得右栏说"已取消"、画布上那一行还写着"进行中"。
 * 没有变化时返回同一个对象。
 */
export function settleTeam(team: TeamRun, ending: Ending): TeamRun {
  // 结局里不会有 skipped（那是节点自己的决定，不是运行的结局）
  const as = (st: NodeState) => stepStatusOf(st) as TeamMember['status']
  const target = (m: TeamMember): TeamMember['status'] | null =>
    m.status === 'running' ? as(ending.running)
      : m.status === 'waiting' && ending.waiting !== 'waiting' ? as(ending.waiting)
      : null
  const over = ending.phase !== 'waiting' && ending.phase !== 'suspended'
  if (!team.rounds.some((r) => r.members.some((m) => target(m))) && (team.finished || !over)) {
    return team
  }
  return {
    ...team,
    finished: team.finished || over,
    rounds: team.rounds.map((r) => (r.members.some((m) => target(m))
      ? { ...r, members: r.members.map((m) => { const st = target(m); return st ? { ...m, status: st } : m }) }
      : r)),
  }
}

/** 从 db_query 的结果预览里抠出行数——用户关心的是"查到多少"，不是 JSON。 */
function rowCountOf(preview: string): number | undefined {
  const m = preview.match(/"row_count":\s*(\d+)/)
  return m ? Number(m[1]) : undefined
}

export interface ResultTable {
  columns: string[]
  rows: unknown[][]
  /** 查询本身撞了行数上限——数据库里还有更多，是 guard 没让它全取回来 */
  truncated: boolean
  /** 这份预览被按字符数切断了——取回来的行数比这里显示的多，只是没存下来 */
  clipped?: boolean
}

/**
 * 把查询结果预览转成小表格能用的形状；解析不了就算了，原样展示。
 *
 * 必须容忍**被截断的 JSON**。后端的预览是按字符数硬切的（成果字段 2000、
 * 工具结果 4000），切点落在 JSON 中间是常态而不是例外——严格 JSON.parse
 * 对这类值一律失败，于是最典型的一次取数运行，最终成果会以满屏
 * `\"attribute01\", \"attribute02\"` 的形式呈现。那不是"降级展示"，那是
 * 什么都没展示。所以解析失败时回退到"截到最后一条完整记录"再解析。
 */
export function parseQueryResult(preview: string): ResultTable | null {
  const text = preview.trim()
  if (!text) return null

  // 值常常被 JSON 编码过一层（字符串里套字符串），截断后连外层引号都收不了口
  const unwrapped = text.startsWith('"') ? unescapeJsonString(text) : text
  if (!unwrapped.includes('"columns"')) return null

  const direct = tryTable(unwrapped)
  if (direct) return direct

  const repaired = cutAtLastCompleteRow(unwrapped)
  if (repaired) {
    const table = tryTable(repaired)
    if (table) return { ...table, clipped: true }
  }
  return null
}

function tryTable(text: string): ResultTable | null {
  try {
    const data = JSON.parse(text)
    if (Array.isArray(data?.columns) && Array.isArray(data?.rows)) {
      return { columns: data.columns, rows: data.rows, truncated: !!data.truncated }
    }
  } catch {
    /* 交给调用方决定要不要修 */
  }
  return null
}

/** 去掉外层 JSON 字符串的引号和转义。截断的串 JSON.parse 不了，只能手工拆。 */
function unescapeJsonString(text: string): string {
  const body = text.slice(1)
  return body.replace(/\\(u[0-9a-fA-F]{4}|.)/g, (_, c: string) => {
    if (c[0] === 'u') return String.fromCharCode(parseInt(c.slice(1), 16))
    return { n: '\n', t: '\t', r: '\r', b: '\b', f: '\f' }[c] ?? c
  })
}

/**
 * 把截断的 `{"columns":[...],"rows":[[...],[...],[...`
 * 砍到最后一条完整记录后补上收尾括号。
 */
function cutAtLastCompleteRow(text: string): string | null {
  const start = text.indexOf('"rows"')
  if (start < 0) return null
  const open = text.indexOf('[', start)
  if (open < 0) return null

  let depth = 0
  let inString = false
  let escaped = false
  let lastRowEnd = -1

  for (let i = open; i < text.length; i += 1) {
    const ch = text[i]
    if (escaped) { escaped = false; continue }
    if (ch === '\\') { escaped = true; continue }
    if (ch === '"') { inString = !inString; continue }
    if (inString) continue
    if (ch === '[' || ch === '{') depth += 1
    else if (ch === ']' || ch === '}') {
      depth -= 1
      // depth 回到 1 表示一条记录刚闭合（0 是 rows 数组本身）
      if (depth === 1) lastRowEnd = i + 1
      else if (depth === 0) return text.slice(0, i + 1) + '}'   // rows 其实是完整的
    }
  }
  if (lastRowEnd < 0) return null
  return text.slice(0, lastRowEnd) + ']}'
}

/**
 * 一次运行的一句话摘要，给列表用。
 *
 * 运行列表原本每行只有"临时图 · 1.2s · 340 tok"——三条运行长得一模一样，
 * 要分清哪条是哪条只能挨个点开。人记得住的是内容（问了什么、出了什么），
 * 不是耗时。
 *
 * 优先成果：跑完了的话，"查到多少"比"问了什么"更能认出这一条。
 */
export function summarizeRun(
  input?: Record<string, any> | null,
  output?: Record<string, any> | null,
): string {
  const pick = (obj?: Record<string, any> | null) => {
    if (!obj) return ''
    for (const [k, v] of Object.entries(obj)) {
      if (k.startsWith('_')) continue
      if (v == null || v === '') continue
      if (typeof v === 'string') {
        const table = parseQueryResult(v)
        if (table) {
          return `${table.rows.length}${table.clipped ? '+' : ''} 行 × ${table.columns.length} 列`
        }
        return v.replace(/\s+/g, ' ').slice(0, 70)
      }
      if (typeof v === 'object') {
        const table = parseQueryResult(JSON.stringify(v))
        if (table) return `${table.rows.length} 行 × ${table.columns.length} 列`
        const text = JSON.stringify(v)
        if (text !== '{}' && text !== '[]') return text.slice(0, 70)
        continue
      }
      return String(v).slice(0, 70)
    }
    return ''
  }
  return pick(output) || pick(input)
}

/**
 * agent 步骤的配对键。
 *
 * 光用 agent 名不够：supervisor 会在多轮里反复派给同一个 agent，第二轮的
 * end 会错配到第一轮的 start 上。round 才是区分轮次的那一维。
 */
function agentKey(nodeId: string | undefined, d: any): string {
  return [nodeId ?? '_', String(d?.agent ?? ''), String(d?.round ?? '')].join('|')
}

/** 标题的上限。窄栏只有 360px，再长就只能靠折行撑高整行 */
const TITLE_MAX = 60

const clip = (text: string, max = TITLE_MAX): string =>
  text.length > max ? `${text.slice(0, max - 1)}…` : text

/** 标识符去掉引号、库名前缀：`"ANALYTICS"."v_kpi"` → v_kpi */
const bareIdent = (s: string): string =>
  s.replace(/[`"[\]]/g, '').split('.').pop() ?? s

export interface SqlGist {
  tables: string[]
  groupBy: string[]
  aggregate: boolean
  limit?: number
}

/**
 * 从 SQL 里认出查的是哪几张表、按什么汇总。
 *
 * 一次取数运行常有五六条查询，以前标题全是「在 bi 上查询数据」，只能靠「50 行
 * · 9.5 s」这种 meta 去分——用户得逐条展开 SQL 才知道哪条查了什么。这里只做
 * 够起标题的那点识别（FROM / JOIN / GROUP BY / 聚合 / 行数上限），不是 SQL
 * 解析器；认不出表名就返回 null，由调用方退回数据源名。
 */
export function describeSql(sql: string): SqlGist | null {
  const text = sql
    .replace(/--[^\n]*/g, ' ')
    .replace(/\/\*[\s\S]*?\*\//g, ' ')
    .replace(/'(?:[^']|'')*'/g, "''")
  if (!/\b(select|with)\b/i.test(text)) return null
  // WITH 里定义的临时名不是表
  const ctes = new Set([...text.matchAll(/(?:\bwith|,)\s*([\w$]+)\s+as\s*\(/gi)]
    .map((m) => m[1].toLowerCase()))
  const tables: string[] = []
  for (const m of text.matchAll(/\b(?:from|join)\s+([`"[]?[\w$.]+[`"\]]?(?:\.[`"[]?[\w$]+[`"\]]?)*)/gi)) {
    const name = bareIdent(m[1])
    if (!name || ctes.has(name.toLowerCase()) || tables.includes(name)) continue
    // 「FROM (SELECT …)」「FROM DUAL」这类不是业务表
    if (/^(select|dual|lateral|unnest)$/i.test(name)) continue
    tables.push(name)
  }
  if (!tables.length) return null
  const groupBy = groupClause(text).split(',')
    .map((c) => c.trim())
    // 位置序号（GROUP BY 1, 2）认不出是哪一列，宁可不说
    .filter((c) => c && !/^\d+$/.test(c))
    .map((c) => bareIdent(c.match(/^\w+\(\s*([\w$."`[\]]+)\s*\)$/)?.[1] ?? c))
    .filter((c) => /^[\w$一-龥]+$/.test(c))
  const aggregate = /\b(count|sum|avg|min|max)\s*\(/i.test(text)
  const lim = text.match(/\blimit\s+(\d+)|\bfetch\s+first\s+(\d+)|\btop\s+(\d+)/i)
  const limit = lim ? Number(lim[1] ?? lim[2] ?? lim[3]) : undefined
  return { tables, groupBy, aggregate, ...(limit != null ? { limit } : {}) }
}

/**
 * GROUP BY 后面那一段。按括号配平往后扫：「GROUP BY DATE(ts)」里的右括号是函数的，
 * 子查询收尾那个多出来的右括号才是边界
 */
function groupClause(text: string): string {
  const m = /\bgroup\s+by\s+/i.exec(text)
  if (!m) return ''
  const rest = text.slice(m.index + m[0].length)
  let depth = 0
  for (let i = 0; i < rest.length; i += 1) {
    const ch = rest[i]
    if (ch === '(') depth += 1
    else if (ch === ')') {
      if (depth === 0) return rest.slice(0, i)
      depth -= 1
    } else if (ch === ';') return rest.slice(0, i)
    else if (depth === 0 && /^\s(order\s+by|having|limit|fetch|union|window)\b/i.test(rest.slice(i))) {
      return rest.slice(0, i)
    }
  }
  return rest
}

function queryTitle(sql: string, source: string): string {
  const g = describeSql(sql)
  if (!g) return `在 ${source} 上查询数据`
  const what = g.tables.length > 2
    ? `${g.tables[0]} 等 ${g.tables.length} 张表`
    : g.tables.join(' + ')
  const how = g.groupBy.length
    ? `按 ${g.groupBy.slice(0, 2).join('、')}${g.groupBy.length > 2 ? ' 等' : ''} 汇总`
    : g.aggregate ? '汇总'
    : g.limit != null ? `取前 ${formatNumber(g.limit)} 行`
    : ''
  return clip(`查询 ${what}${how ? ` · ${how}` : ''}`)
}

function toolStep(seq: number, tool: string, args: Record<string, any>): Step {
  const base = { id: `tool-${seq}`, seq, status: 'running' as StepStatus }
  // 数据库工具单独认：它是这个产品最主要的取数方式，"调用 db_query__warehouse"
  // 这种说法等于没翻译
  if (tool.startsWith('db_query')) {
    const source = tool.replace(/^db_query__/, '')
    const sql = String(args?.sql ?? '')
    // 数据源名放进展开区的表头：五条查询都挂着同一个库名，标题上是噪音
    return { ...base, kind: 'query', title: queryTitle(sql, source), detail: sql, source }
  }
  if (tool.startsWith('db_schema')) {
    // 一次看十几张表时 table 是逗号拼起来的一整串：没有断行机会，标题会越过
    // 卡片右边框一直伸到视口外。标题只摘前几张，完整清单逐行放进详情
    const tables = String(args?.table ?? '').split(',').map((t) => t.trim()).filter(Boolean)
    if (!tables.length) return { ...base, kind: 'schema', title: '列出有哪些表' }
    if (tables.length <= 3) {
      return { ...base, kind: 'schema', title: clip(`查看 ${tables.join('、')} 的字段`) }
    }
    const head = tables.slice(0, 3).join('、')
    return { ...base, kind: 'schema', title: clip(`查看 ${tables.length} 张表的字段：${head}…`),
             detail: tables.join('\n') }
  }
  const argText = Object.keys(args ?? {}).length
    ? JSON.stringify(args, null, 2) : ''
  return { ...base, kind: 'tool', title: clip(`调用 ${tool}`), detail: argText || undefined }
}

/** 低于这个毫秒数的节点不参与并行分组：瞬时节点谁跟谁都"重叠"，是噪音不是信息 */
const CONCURRENT_MIN_MS = 30
/** 开始时刻相差在这个数以内算"同一批出发"。实测 fan-out 的几路相差不到 1ms */
const SAME_WAVE_MS = 250

/**
 * 认出哪几个节点是**同时**跑的，把它们收进一个分组里。
 *
 * 图上一个节点连出多条边就是 fan-out，LangGraph 会在同一个 superstep 里并发
 * 执行它们——这是真并发（实测 3 个各 sleep 2 秒的节点，全部 +0.00s 开始、
 * +2.37s 结束，墙钟 2.6 秒）。但在时间线上它们只是穿插出现的几行，
 * "这三路是同时跑的、因此省下了 3.6 秒"这件事一个字都没说。
 *
 * 判定用时间跨度重叠，而不是看事件顺序——顺序只说明"先后发出"，
 * 说明不了"同时在跑"。
 *
 * 判据是"同一批出发"：fan-out 的几路由 LangGraph 在同一个 superstep 里启动，
 * 开始时刻相差以毫秒计。**不能只看跨度重叠**——三路同时开始但先后结束时，
 * 长的那条在时间上确实"包含"短的，按包含关系去排会把先跑完的那路漏掉
 * （实测 leg0 [0.05,2.21] / leg1 [0.05,3.01] / leg2 [0.05,1.51]，
 * 漏的就是 leg2）。
 *
 * 子工作流那种真嵌套则是父节点明显更早开始、更晚结束，出发时刻对不上。
 */
function groupConcurrent(
  out: Step[], spans: Map<string, { start: number; end: number; ms: number }>,
): void {
  const idx = new Map<Step, number>()
  out.forEach((s, i) => idx.set(s, i))

  const candidates = out.filter((s) => {
    const sp = s.nodeId ? spans.get(s.nodeId) : undefined
    return !!sp && sp.ms >= CONCURRENT_MIN_MS
  })
  if (candidates.length < 2) return

  const together = (a: Step, b: Step): boolean => {
    const x = spans.get(a.nodeId!)!
    const y = spans.get(b.nodeId!)!
    if (x.start >= y.end || y.start >= x.end) return false      // 压根没重叠
    return Math.abs(x.start - y.start) * 1000 <= SAME_WAVE_MS   // 同一批出发
  }

  const used = new Set<Step>()
  const groups: Step[][] = []
  for (const a of candidates) {
    if (used.has(a)) continue
    const g = [a]
    for (const b of candidates) {
      if (b === a || used.has(b)) continue
      // 和组里每一个都重叠才算同一批，否则 A|B 重叠、B|C 重叠但 A|C 不重叠
      // 也会被串成一组，而那三个并不是同时在跑
      if (g.every((m) => together(m, b))) g.push(b)
    }
    if (g.length > 1) { g.forEach((m) => used.add(m)); groups.push(g) }
  }

  for (const g of groups) {
    g.sort((a, b) => (idx.get(a) ?? 0) - (idx.get(b) ?? 0))
    const sp = g.map((m) => spans.get(m.nodeId!)!)
    const wallMs = Math.round(
      (Math.max(...sp.map((x) => x.end)) - Math.min(...sp.map((x) => x.start))) * 1000)
    const sumMs = sp.reduce((acc, x) => acc + x.ms, 0)
    const at = Math.min(...g.map((m) => out.indexOf(m)))
    const first = g[0]
    for (const m of g) out.splice(out.indexOf(m), 1)
    out.splice(at, 0, {
      id: `par-${first.id}`, seq: first.seq, kind: 'branch', status: 'done',
      title: `${g.length} 路并行`,
      // 省下多少是这一组存在的理由，放 meta 里一眼看得到
      meta: sumMs > wallMs
        ? `合计 ${dur(sumMs)}，实际 ${dur(wallMs)}`
        : dur(wallMs),
      children: g,
    })
  }
}

/**
 * 把一个事件并进协作团队的泳道数据，返回**新的** TeamRun。
 *
 * 时间线和画布都要这份数据：时间线用它画泳道，画布用它把 supervisor 节点
 * 展开成协作矩阵。两边各写一遍的话，同一个运行在右栏和画布上会给出不同的
 * 并行度——而"几个人同时在跑、省了多少"正是这个节点唯一值得看的东西。
 * 所以只留这一份翻译，两处都调它。
 *
 * 不可变：每次返回新对象。画布那份挂在 zustand 的节点状态上，原地改的话
 * 引用不变，React 收不到更新。
 *
 * 返回 null 表示这个事件和协作团队无关（绝大多数事件都是）。
 */
export function reduceTeam(prev: TeamRun | undefined, event: RunEvent): TeamRun | null {
  const d = (event.data ?? {}) as Record<string, any>
  const nodeId = event.node_id
  if (!nodeId) return null

  const roundOf = (team: TeamRun, round: number): TeamRound =>
    team.rounds.find((x) => x.round === round) ?? {
      round, parallel: 1, wallMs: 0, sumMs: 0, members: [],
    }

  const withRound = (team: TeamRun, round: number, patch: Partial<TeamRound>): TeamRun => {
    const next = { ...roundOf(team, round), ...patch }
    const rounds = [...team.rounds.filter((x) => x.round !== round), next]
      .sort((a, b) => a.round - b.round)
    // 只算整轮都交回了的：一轮刚开始时各人耗时都是 0，算出来的"省下 0ms"是
    // 在报一个还没发生的收益
    const savedMs = rounds.reduce((acc, x) => acc
      + (x.members.length && x.members.every((m) => m.status === 'done')
        ? Math.max(0, x.sumMs - x.wallMs) : 0), 0)
    return { ...team, rounds, savedMs }
  }

  switch (event.type) {
    case 'agent.step.start': {
      const team = prev ?? { members: [], rounds: [], savedMs: 0, finished: false }
      const name = String(d.agent ?? '')
      const round = num(d.round) ?? 0
      const cur = roundOf(team, round)
      const members = [...cur.members, {
        agent: name, instruction: String(d.instruction ?? ''), ms: 0,
        status: 'running' as TeamMember['status'],
      }]
      return withRound(
        { ...team, members: team.members.includes(name) ? team.members : [...team.members, name] },
        round,
        { members, parallel: Math.max(cur.parallel, num(d.parallel) ?? 1) },
      )
    }

    case 'agent.step.end': {
      if (!prev) return null
      const name = String(d.agent ?? '')
      const round = num(d.round) ?? 0
      const cur = roundOf(prev, round)
      const ms = num(d.duration_ms) ?? 0
      const preview = String(d.preview ?? '').slice(0, 2000)
      const idx = cur.members.findIndex((x) => x.agent === name && x.status === 'running')
      const members = idx >= 0
        ? cur.members.map((x, i) => (i === idx ? { ...x, ms, status: 'done' as const, result: preview || undefined } : x))
        : [...cur.members, { agent: name, instruction: '', ms, status: 'done' as const, result: preview || undefined }]
      // 这一轮实际花的是最慢那个，各人之和减去它就是省下的
      return withRound(prev, round, {
        members,
        wallMs: Math.max(...members.map((x) => x.ms)),
        sumMs: members.reduce((acc, x) => acc + x.ms, 0),
      })
    }

    case 'log': {
      // 带 round 的 info 日志是调度决策，不是排查日志
      if (String(d.level ?? 'info') !== 'info' || d.round == null) return null
      const team = prev ?? { members: [], rounds: [], savedMs: 0, finished: false }
      const structured = Array.isArray(d.agents)
      const text = String(d.message ?? '')
      const legacy = text.match(/^调度\s*→\s*([^（(]+)[（(](.*)[）)]\s*$/)
      const done = structured ? !!d.done : legacy?.[1]?.trim() === 'FINISH'
      const reason = structured ? String(d.reason ?? '') : (legacy?.[2]?.trim() ?? '')
      if (done) return { ...team, finished: true }
      return reason ? withRound(team, num(d.round) ?? 0, { reason }) : team
    }

    case 'agent.route.end': {
      // 调度者的结构化决策。后端同一轮还会再发一条带 round 的 log，两条说的是
      // 同一件事，按哪条来结果都一样；只认 log 的话，哪天 log 不发了理由就丢了
      const team = prev ?? { members: [], rounds: [], savedMs: 0, finished: false }
      if (d.done) return { ...team, finished: true }
      const reason = String(d.reason ?? '')
      return reason ? withRound(team, num(d.round) ?? 0, { reason }) : team
    }

    default:
      return null
  }
}

/**
 * 把一次运行的事件解码成步骤树。
 *
 * 顶层是节点，节点内部的模型/工具调用是子步骤。没有 node_id 的事件
 * （run.*、issuance 这类）留在顶层，按时间顺序穿插。
 *
 * 同一个节点在一次运行里执行多次（循环体、轮询、审批驳回后重来）时仍然只占
 * 一行：每次执行记进 execs，子步骤带上 iter，界面据此按轮折叠。以前各轮的子
 * 步骤全平铺进同一个父节点，一次 145 拍的运行就是 145 行一模一样的「运行
 * Python 代码」，内层循环的「第 3 轮」在不同外层拍之间反复出现、对不上号。
 *
 * final 是事件之外知道的结局（见 decodePhase）：服务被强杀的运行事件停在半路，
 * 只有查到它是 interrupted、又没有待审批，才能把那几行转圈收成"挂起"。
 */
export function decodeRun(events: RunEvent[], final?: RunFinal): Step[] {
  const out: Step[] = []
  /** node_id → 该节点的顶层 Step，用于把子步骤挂进去 */
  const nodeSteps = new Map<string, Step>()
  /** call_id（或工具名兜底）→ 待配对的工具 Step */
  const pendingTools = new Map<string, Step>()
  /** node_id → 待配对的模型调用 Step */
  const pendingLlm = new Map<string, Step>()
  /** supervisor 里待配对的单个 agent 步骤 */
  const pendingAgents = new Map<string, Step>()
  /** node_id|round → 调度者这一轮的决策行（route.start 开、route.end 收） */
  const pendingRoutes = new Map<string, Step>()
  /** 已经有结构化调度事件的轮次。同一轮那条带 round 的 log 说的是同一件事，不再成行 */
  const routed = new Set<string>()
  /** node_id → 协作团队的泳道数据。边解码边攒，最后挂到该节点的顶层 Step 上 */
  const teams = new Map<string, TeamRun>()
  /** node_id → 起止时刻。用来事后认出"哪几个节点是同时跑的" */
  const spans = new Map<string, { start: number; end: number; ms: number }>()

  /** 协作团队的状态只有一个来源：reduceTeam。画布那边调的是同一个函数 */
  const trackTeam = (event: RunEvent): void => {
    const next = reduceTeam(teams.get(event.node_id ?? ''), event)
    if (next && event.node_id) teams.set(event.node_id, next)
  }
  /**
   * node_id → 这个节点当前那条尚未闭合的审批步骤。
   *
   * 审批是"开→闭"配对的，不能只靠内容去重。同一次中断会发三条事件
   * （human.requested、run.interrupted，恢复后节点重放又来一条
   * human.requested），三条内容完全一样；而一个循环里连续三轮审批
   * （驳回 → 改写 → 再审）内容也完全一样。按内容去重的话，要么把重放
   * 算成新的一轮，要么把真实的第二轮当成重放吞掉——两种都错。
   *
   * 开着就是同一次，闭了才是新一次。
   */
  const openInterrupts = new Map<string, Step>()

  const push = (step: Step, nodeId?: string) => {
    const parent = nodeId ? nodeSteps.get(nodeId) : undefined
    if (parent) {
      // 记下属于父节点的第几次执行：分轮折叠靠它
      if (parent.execs && parent.execs.length > 1) step.iter = parent.execs.length
      else step.iter = 1
      ;(parent.children ??= []).push(step)
    } else out.push(step)
  }

  /** 父节点这一次执行 */
  const execOf = (nodeId?: string): Exec | undefined => {
    const execs = nodeId ? nodeSteps.get(nodeId)?.execs : undefined
    return execs?.[execs.length - 1]
  }

  /**
   * 把决定折进那条审批行，而不是另起一行——"问了什么 → 怎么定的"是一件事。
   *
   * 谁定的照事件里的 actor 写。以前一律写"你驳回了"，而多人使用时批的可能是
   * 别人；没署名（actor 为 null、老数据没有这个字段）就只说结果，不猜是谁。
   */
  const verdict = (approved: unknown, actor: unknown): string => {
    const who = typeof actor === 'string' && actor.trim() ? actor.trim() : ''
    const what = approved === false ? '驳回了' : '放行了'
    return who ? `${who} ${what}` : `已${what.slice(0, 2)}`
  }
  const closeInterrupt = (key: string, approved: unknown, note: string, actor?: unknown): boolean => {
    const step = openInterrupts.get(key)
    if (!step) return false
    step.status = 'done'
    step.level = undefined
    step.title = `${step.title} → ${verdict(approved, actor)}`
    if (note) step.detail = [step.detail, `备注：${note}`].filter(Boolean).join('\n')
    openInterrupts.delete(key)
    return true
  }

  /** 上一条就是恢复事件——用来认出紧随其后的那条重复的"继续执行" */
  let justResumed = false
  /** 恢复事件带来的签批人：run.resumed 有，紧接着的 human.resolved 老后端不一定有 */
  let resumedActor: unknown

  /** 相位跟着事件走，结局要知道此刻是不是停在审批上 */
  let phase: PhaseState = { phase: 'idle', awaiting: false }
  const settle = (ending: Ending) => {
    settleSteps(out, ending)
    for (const [id, team] of teams) teams.set(id, settleTeam(team, ending))
    // 协作成员那几行挂在 pendingAgents 上等配对；结局之后不会再有 end 了
    if (ending.phase !== 'waiting') {
      pendingAgents.clear()
      pendingTools.clear()
      pendingLlm.clear()
      pendingRoutes.clear()
      if (ending.waiting !== 'waiting') openInterrupts.clear()
    }
  }

  for (const event of events) {
    const type = String(event.type)
    if (EPHEMERAL.has(type) || SILENT.has(type)) continue
    const awaiting = phase.awaiting
    phase = nextPhase(phase, event)
    const d: any = event.data ?? {}
    const seq = event.seq ?? 0
    const nodeId = event.node_id ?? undefined
    const at = tsMs(event)
    // 每条事件都会把"刚恢复过"清掉；只有恢复分支会重新点亮它，
    // 所以这个标记只在紧挨着的下一条上为真
    const wasJustResumed = justResumed
    justResumed = false

    switch (type) {
      case 'run.started':
      case 'run.resumed': {
        const resumed = type === 'run.resumed' || !!d.resumed
        if (type === 'run.resumed') resumedActor = d.actor
        // 恢复一次会紧挨着发两条：run.resumed 和 run.started(resumed=true)。
        // 说两遍"继续执行"会让人以为恢复了两次。
        //
        // 判据是"这两条是不是紧挨着的"，不是"界面上还有没有待办"——后者
        // 曾经能用，但只要多一处会产生 waiting 状态的地方（比如把被中断的
        // 节点也标成等待），它就失效了。相邻性是这两条事件的固有性质，
        // 不会因为别处改了展示而变。
        if (resumed && wasJustResumed) break
        if (resumed) {
          justResumed = true
          // 人已经答过了，那条不该再是橙色的"等你确认"。但配对关系要留着：
          // 紧接着 LangGraph 会重放该节点，又发一条一模一样的
          // human.requested，配对还开着才能认出那是重放而不是新一轮。
          // 真正的闭合（把决定折进标题）交给带 node_id 的 human.resolved。
          openInterrupts.forEach((s) => {
            if (s.status === 'waiting') { s.status = 'done'; s.level = undefined }
          })
        }
        const who = typeof d.actor === 'string' && d.actor.trim() ? d.actor.trim() : ''
        out.push({
          id: `s-${seq}`, seq, kind: 'lifecycle', status: 'running', startedAt: at,
          title: resumed ? '继续执行' : `开始执行（${d.nodes ?? '?'} 个节点）`,
          ...(num(d.nodes) != null && !resumed ? { total: d.nodes } : {}),
          // 「从「X」接着跑，保留了前面 3 个节点」——从哪接、谁点的，续跑才对得上账
          ...(resumed && (d.message || who)
            ? { sub: [String(d.message ?? ''), who ? `由 ${who} 发起` : ''].filter(Boolean).join(' · ') }
            : {}),
        })
        break
      }

      case 'node.started': {
        // data.label 是节点标题随事件传递的唯一来源——靠画布 nodes 反查的话，
        // 助手栏和运行页没加载画布，标题会退化成裸 node_id。但没起过名字的
        // 节点它等于节点 id（后端兜底），"in"/"h"/"out" 对用户毫无意义——退到
        // 节点类型的中文名，至少能看出这步在干嘛
        const label = String(d.label ?? '')
        const typeName = TYPE_LABEL[String(d.node_type ?? '')]
        const title = (label && label !== nodeId ? label : typeName) || nodeId || '执行步骤'
        const iteration = num(d.iteration)
        if (nodeId && !spans.has(nodeId)) {
          spans.set(nodeId, { start: event.ts ?? 0, end: event.ts ?? 0, ms: 0 })
        }
        const existing = nodeId ? nodeSteps.get(nodeId) : undefined
        if (existing) {
          const execs = existing.execs ??= [{ n: 1, seq: existing.seq, status: existing.status ?? 'done' }]
          // 同一个节点又开始了，两种可能：
          // - 重放：审批恢复、接着跑之后 LangGraph 会把停下的那个节点再执行一遍。
          //   那不是"又执行了一个步骤"，是同一步继续——新后端标 resumed，老数据
          //   认"它上次没做完"（还停在等人 / 在跑 / 挂起）
          // - 新的一轮：上次已经做完了（循环体、驳回后重来）
          const replay = !!d.resumed || existing.status === 'waiting'
            || existing.status === 'running' || existing.status === 'suspended'
          if (replay) {
            const cur = execs[execs.length - 1]
            cur.status = 'running'
          } else {
            execs.push({ n: execs.length + 1, seq, status: 'running',
                         ...(iteration != null ? { iteration } : {}) })
          }
          existing.status = 'running'
          // 上一轮的失败、耗时不能挂在正在跑的这一轮上
          existing.level = undefined
          existing.meta = undefined
          existing.startedAt = at
          break
        }
        const step: Step = {
          id: `n-${nodeId ?? seq}`, seq, kind: 'node', nodeId, title, status: 'running',
          startedAt: at,
          execs: [{ n: 1, seq, status: 'running', ...(iteration != null ? { iteration } : {}) }],
        }
        if (nodeId) nodeSteps.set(nodeId, step)
        out.push(step)
        break
      }

      case 'node.skipped': {
        // skip_if 成立：这一步没有执行。以前整类事件被静默丢掉，用户分不清"被
        // 条件跳过"和"根本没轮到"，受限编排做过的这个决定在时间线上没有痕迹
        const label = String(d.label ?? '')
        const name = (label && label !== nodeId ? label : TYPE_LABEL[String(d.node_type ?? '')])
          || nodeId || '这一步'
        const reason = String(d.reason ?? '')
        const existing = nodeId ? nodeSteps.get(nodeId) : undefined
        if (existing) {
          const cur = execOf(nodeId)
          if (existing.status !== 'running') {
            existing.execs?.push({ n: existing.execs.length + 1, seq, status: 'skipped' })
          } else if (cur) cur.status = 'skipped'
          existing.status = 'skipped'
          existing.sub = reason || existing.sub
          break
        }
        const step: Step = {
          id: `sk-${nodeId ?? seq}`, seq, kind: 'node', nodeId, status: 'skipped',
          title: `跳过「${name}」`,
          ...(reason ? { sub: reason } : {}),
          execs: [{ n: 1, seq, status: 'skipped' }],
        }
        if (nodeId) nodeSteps.set(nodeId, step)
        out.push(step)
        break
      }

      case 'node.finished': {
        // 人工节点跑完就说明决定已经生效了。正常路径上 human.resolved 早就
        // 闭合了配对，这里只是兜底：万一那条事件没发出来（旧运行、别的
        // 审批来源），配对不能一直挂着——挂着的话这个节点下一轮真实审批
        // 会被当成重放吞掉
        if (nodeId && openInterrupts.has(nodeId)) {
          const p = d.preview ?? {}
          closeInterrupt(nodeId,
            typeof p === 'object' ? p.approved : undefined,
            typeof p === 'object' ? String(p.note ?? '') : '',
            resumedActor)
        }
        const span = nodeId ? spans.get(nodeId) : undefined
        if (span) {
          span.end = event.ts ?? span.end
          span.ms = num(d.duration_ms) ?? Math.round((span.end - span.start) * 1000)
        }
        const step = nodeId ? nodeSteps.get(nodeId) : undefined
        if (step) {
          const ms = num(d.duration_ms)
          const cur = execOf(nodeId)
          if (cur) { cur.status = 'done'; if (ms != null) cur.ms = ms }
          step.status = 'done'
          step.artifact = d.artifact || step.artifact
          const execs = step.execs ?? []
          if (execs.length > 1) {
            // 执行过多次：行尾说"几次、一共多久"，每一次的耗时在分轮折叠里看
            const total = execs.reduce((acc, x) => acc + (x.ms ?? 0), 0)
            step.ms = total
            step.meta = [`×${execs.length}`, dur(total) && `共 ${dur(total)}`].filter(Boolean).join(' · ')
          } else {
            step.ms = ms
            const attempt = num(d.attempt)
            step.meta = [dur(ms), attempt && attempt > 1 ? `重试 ${attempt - 1} 次` : '']
              .filter(Boolean).join(' · ') || undefined
          }
          // 空对象/空串不是"详情"，显示出来只是噪音
          const raw = d.preview
          const empty = raw == null || raw === '' ||
            (typeof raw === 'object' && Object.keys(raw).length === 0)
          if (!empty) {
            const preview = typeof raw === 'string' ? raw : JSON.stringify(raw, null, 2)
            step.detail = preview.slice(0, 4000)
          }
        }
        break
      }

      case 'node.failed': {
        const step = nodeId ? nodeSteps.get(nodeId) : undefined
        const ms = num(d.duration_ms)
        // detail 是原始异常：给排查用，不给人读，折起来
        const raw = typeof d.detail === 'string' && d.detail ? d.detail : undefined
        if (step) {
          const cur = execOf(nodeId)
          if (cur) { cur.status = 'failed'; if (ms != null) cur.ms = ms }
          step.status = 'failed'
          step.level = 'error'
          step.detail = String(d.error ?? '')
          step.raw = raw
          step.ms = ms
          step.meta = dur(ms)
        } else {
          push({ id: `e-${seq}`, seq, kind: 'error', level: 'error', status: 'failed',
                 title: String(d.error ?? '这一步失败了'), nodeId, raw }, nodeId)
        }
        break
      }

      case 'llm.start': {
        // 一出现就成行：模型想的那几十秒里得有一行在走，而不是等 end 才冒出来。
        // 团队成员和调度者的调用没有 start（它们的 end 只用来记账），不会走到这
        const step: Step = {
          id: `llm-${seq}`, seq, kind: 'llm', title: '思考并作答',
          status: 'running', nodeId, startedAt: at,
          ...(d.model ? { detail: `模型：${d.model}` } : {}),
        }
        pendingLlm.set(nodeId ?? '_', step)
        push(step, nodeId)
        break
      }

      case 'llm.end': {
        const step = pendingLlm.get(nodeId ?? '_')
        if (!step) break
        step.status = 'done'
        // 不给 token 数和美元——那是账单视角，不是"它干了什么"。
        // 用量在运行面板底栏、运行详情的用量区单独看。
        step.ms = num(d.duration_ms)
        step.meta = dur(step.ms)
        if (d.model && !step.detail) step.detail = `模型：${d.model}`
        pendingLlm.delete(nodeId ?? '_')
        break
      }

      case 'llm.thinking': {
        // 只有 Anthropic 系模型会产出（thinking_text 认的是 thinking 块）。
        // 换个 provider 就没有——所以它是增强信息，主干靠上面那些步骤撑着。
        const text = String(d.text ?? d.delta ?? '')
        if (!text.trim()) break
        const full = text + (d.truncated ? '\n\n（已截断）' : '')
        // 思考是这次调用的"意图"，不单独成行：挂到它所属的那次模型调用上当副
        // 标题。以前一次运行 23 行里 9 行是斜体的思考摘录，把真正的动作挤散了
        const siblings = nodeId ? nodeSteps.get(nodeId)?.children : undefined
        const host = pendingLlm.get(nodeId ?? '_')
          ?? [...(siblings ?? [])].reverse().find((s) => s.kind === 'llm' && !s.sub)
        if (host) {
          host.sub = thinkingHeadline(text, false)
          host.detail = [full, host.detail].filter(Boolean).join('\n\n')
          break
        }
        push({
          id: `th-${seq}`, seq, kind: 'think', nodeId, status: 'done',
          title: thinkingHeadline(text, false), detail: full,
        }, nodeId)
        break
      }

      case 'tool.start': {
        const step = toolStep(seq, String(d.tool ?? ''), d.args ?? {})
        step.nodeId = nodeId
        step.startedAt = at
        // 协作成员调的工具说清是谁调的
        if (d.agent) step.sub = `${d.agent} 调用`
        // call_id 才是可靠的配对键：同一节点并发调同名工具时，按名字配会错位。
        // 独立 Tool 节点不带 call_id，回退到工具名。
        pendingTools.set(String(d.call_id || d.tool || seq), step)
        push(step, nodeId)
        break
      }

      case 'tool.end':
      case 'tool.error': {
        const key = String(d.call_id || d.tool || '')
        const step = pendingTools.get(key)
        const preview = String(d.preview ?? d.error ?? '')
        if (step) {
          const failed = type === 'tool.error' || preview.startsWith('SQL 被拒绝')
            || preview.startsWith('查询失败')
          step.status = failed ? 'failed' : 'done'
          if (failed) step.level = 'error'
          // tool.error 只有 {tool,error}，没有 duration_ms/preview——不能假设统一形状
          const rows = rowCountOf(preview)
          step.ms = num(d.duration_ms)
          const parts = [
            rows != null ? `${formatNumber(rows)} 行` : '',
            dur(step.ms) ?? '',
          ].filter(Boolean)
          step.meta = parts.join(' · ') || undefined
          step.artifact = d.artifact
          if (typeof d.detail === 'string' && d.detail) step.raw = d.detail
          if (step.kind === 'query' || step.kind === 'schema' || failed) {
            // 查询保留 SQL 作为 detail，结果另挂；失败时结果就是错误原因
            step.detail = failed ? `${step.detail ?? ''}\n\n${preview}`.trim() : step.detail
            step.result = preview
          } else {
            step.detail = preview.slice(0, 4000) || undefined
          }
          pendingTools.delete(key)
        }
        break
      }

      case 'sandbox.start':
        pendingTools.set(`sandbox-${nodeId ?? seq}`, (() => {
          const step: Step = {
            id: `sb-${seq}`, seq, kind: 'code', nodeId, status: 'running', startedAt: at,
            title: `运行${d.language === 'python' ? ' Python ' : d.language ? ` ${d.language} ` : ''}代码`,
            // 渲染后的代码。编辑器里那份带着 {{ }}，送进沙箱的是替换完的——
            // 值里有引号或换行就会把程序写坏，而报错说的是渲染后的行号。
            // 只有插过值的才值得摆出来，没插值的两份一模一样，显示等于噪音
            detail: d.interpolated && d.code
              ? `# 实际执行的代码（模板已展开）\n${String(d.code)}`
                + (d.code_truncated ? '\n\n（已截断）' : '')
              : undefined,
          }
          push(step, nodeId)
          return step
        })())
        break

      case 'sandbox.end': {
        const step = pendingTools.get(`sandbox-${nodeId ?? seq}`)
        if (step) {
          step.status = d.ok ? 'done' : 'failed'
          if (!d.ok) step.level = 'error'
          step.ms = num(d.duration_ms)
          step.meta = dur(step.ms)
          const body = [d.stdout, d.stderr].filter(Boolean).join('\n').slice(0, 4000)
          // 和查询步骤同一个分工：detail 是"跑的是什么"（渲染后的代码），
          // result 是"跑出了什么"。塞进同一个字段的话，报错时最需要的两样
          // 东西——实际代码和 traceback——只能看见一样
          step.result = body || (d.ok ? '（无输出）'
            : num(d.exit_code) != null ? `退出码 ${d.exit_code}` : '沙箱没有返回退出码')
          if (!step.detail) step.detail = step.result
          pendingTools.delete(`sandbox-${nodeId ?? seq}`)
        }
        break
      }

      case 'edge.taken':
        push({
          id: `br-${seq}`, seq, kind: 'branch', nodeId, status: 'done',
          title: `走「${d.branch}」这条路`
            + (d.iteration != null ? `（第 ${Number(d.iteration) + 1} 轮）` : ''),
          detail: d.reason ? String(d.reason) : undefined,
        }, nodeId)
        break

      case 'human.requested':
      case 'run.interrupted': {
        // 同一次中断发三条事件：human.requested、run.interrupted，恢复后
        // 节点重放又来一条 human.requested（LangGraph 的重放语义，不是 bug）。
        // 三条都指向同一次"等你确认"，界面上只该有一条。
        const payload = d.payload ?? d
        const key = String(payload.node_id ?? nodeId ?? '_')
        if (openInterrupts.has(key)) {       // 同一次中断的后续事件
          if (type === 'run.interrupted') closeLifecycles(out, 'done')
          break
        }
        const step: Step = {
          id: `hm-${seq}`, seq, kind: 'human', nodeId, status: 'waiting', level: 'warn',
          startedAt: at,
          title: String(payload.title || d.title || '等你确认'),
          detail: [payload.message, payload.tool ? `工具：${payload.tool}` : '']
            .filter(Boolean).join('\n') || undefined,
        }
        openInterrupts.set(key, step)
        push(step, nodeId)
        // 承载它的那个节点也不是"在跑"——它停下来等人了。转着蓝圈说的是
        // "在忙，你等着"，而实际情况正相反：它在等你
        const host = nodeId ? nodeSteps.get(nodeId) : undefined
        if (host && host.status === 'running') {
          host.status = 'waiting'
          const cur = execOf(nodeId)
          if (cur) cur.status = 'waiting'
        }
        // 这一段执行到此停下等人："开始执行"不能再转圈。恢复后会另起一行"继续执行"
        if (type === 'run.interrupted') closeLifecycles(out, 'done')
        break
      }

      case 'human.resolved': {
        const r = d.response ?? {}
        const approved = typeof r === 'object' ? r.approved : undefined
        const note = typeof r === 'object' ? String(r.note ?? '') : ''
        const actor = d.actor !== undefined ? d.actor : resumedActor
        if (closeInterrupt(String(nodeId ?? '_'), approved, note, actor)) break
        // 没有对应的待决审批（历史事件不全、或者审批发生在别处）——
        // 还是要把决定说出来，只是没地方折进去
        push({
          id: `hr-${seq}`, seq, kind: 'human', nodeId, status: 'done',
          title: verdict(approved, actor),
          detail: note || undefined,
        }, nodeId)
        break
      }

      case 'issuance': {
        const tier = String(d.tier ?? '')
        const label = issuanceLabel(tier)
        const gaps: string[] = []
        if (d.missing_required?.length) gaps.push(`缺必需指标 ${d.missing_required.join('、')}`)
        if (d.missing_expected?.length) gaps.push(`缺期望指标 ${d.missing_expected.join('、')}`)
        if (num(d.unmatched)) gaps.push(`${d.unmatched} 个数字无法回指指标集`)
        // 校验本身没跑全（叙述渲染为空、指标集为空）也会降档。不写出来的话，
        // 时间线上就是一个光秃秃的「降档出具」，看起来和"全部通过"只差一个字
        const notRun: string[] = Array.isArray(d.gaps) ? d.gaps.map(String) : []
        if (notRun.length) gaps.push(`校验不完整：${notRun.join('；')}`)
        const checked = [
          num(d.metrics_checked) != null ? `核对 ${d.metrics_checked} 个指标` : '',
          num(d.matched_numbers) != null ? `回指 ${d.matched_numbers} 个数字` : '',
        ].filter(Boolean).join(' · ')
        out.push({
          id: `is-${seq}`, seq, kind: 'issuance', status: 'done',
          level: tier === 'formal' ? 'info' : tier === 'withheld' ? 'error' : 'warn',
          title: clip(gaps.length ? `${label}：${gaps.join('；')}` : label, 120),
          // 核对了几个指标、回指了几个数字放进展开区：横幅上有同样的数，窄栏的行尾放不下
          ...(gaps.length || checked ? { detail: [...gaps, checked].filter(Boolean).join('\n') } : {}),
        })
        break
      }

      case 'caliber.upgrade':
        out.push({
          id: `cu-${seq}`, seq, kind: 'note', level: 'warn', status: 'done',
          title: `口径卡 v${d.pinned} → v${d.latest} 有新版，按「${d.policy_label ?? d.policy}」处置`,
        })
        break

      case 'agent.route.start': {
        // 调度者决策前一条。它常常占掉团队节点大半的时间——几十秒里时间线上
        // 得有一行在走，不然看着像卡住了
        const round = num(d.round) ?? 0
        const step: Step = {
          id: `rs-${seq}`, seq, kind: 'branch', nodeId, status: 'running', startedAt: at,
          title: `第 ${round + 1} 轮：调度者在想下一步…`,
        }
        pendingRoutes.set(`${nodeId}|${round}`, step)
        push(step, nodeId)
        break
      }

      case 'agent.route.end': {
        const round = num(d.round) ?? 0
        const key = `${nodeId}|${round}`
        routed.add(key)
        const agents: string[] = Array.isArray(d.agents) ? d.agents.map(String) : []
        const title = d.done || !agents.length ? `第 ${round + 1} 轮：结束协作`
          : agents.length > 1 ? `第 ${round + 1} 轮：交给 ${agents.join('、')}（并行）`
          : `第 ${round + 1} 轮：交给 ${agents[0]}`
        const ms = num(d.duration_ms)
        const reason = String(d.reason ?? '')
        const step = pendingRoutes.get(key)
        if (step) {
          Object.assign(step, { status: 'done', title, ms, meta: dur(ms),
                                ...(reason ? { detail: reason } : {}) })
          pendingRoutes.delete(key)
        } else {
          push({ id: `re-${seq}`, seq, kind: 'branch', nodeId, status: 'done', title, ms,
                 meta: dur(ms), ...(reason ? { detail: reason } : {}) }, nodeId)
        }
        trackTeam(event)
        break
      }

      case 'agent.step.start': {
        // 和 tool.start/tool.end 一样是一件事的两个时刻。拆成两行的话，
        // "派给 researcher 什么任务"和"它回了什么"会变成两条互不相干的记录，
        // 而且 start 那条永远停在转圈——多 agent 节点跑完了它还在转。
        const step: Step = {
          id: `as-${seq}`, seq, kind: 'note', nodeId, status: 'running', startedAt: at,
          title: clip(`${d.agent}：${String(d.instruction ?? '')}`, 80),
        }
        pendingAgents.set(agentKey(nodeId, d), step)
        push(step, nodeId)
        trackTeam(event)
        break
      }

      case 'agent.step.end': {
        const key = agentKey(nodeId, d)
        const step = pendingAgents.get(key)
        const preview = String(d.preview ?? '').slice(0, 2000)
        const ms = num(d.duration_ms)
        if (step) {
          step.status = 'done'
          step.ms = ms
          step.meta = dur(ms)
          step.result = preview || undefined
          pendingAgents.delete(key)
        } else {
          push({
            id: `ae-${seq}`, seq, kind: 'note', nodeId, status: 'done',
            title: `${d.agent} 回复`, ms, meta: dur(ms),
            detail: preview || undefined,
          }, nodeId)
        }
        trackTeam(event)
        break
      }

      case 'log': {
        const level = String(d.level ?? 'info')
        const ending = endingOf(event, awaiting)
        if (ending) settle(ending)
        // supervisor 的调度决策后端标成了 info，但它不是排查用的日志——
        // "为什么派给 researcher"、"为什么只跑一轮就 FINISH"，不显示的话
        // 多 agent 节点在界面上就是一个跑了 31 秒的黑盒。带 round 字段的
        // 就是它，和普通 info 日志区分得开
        if (level === 'info' && d.round != null) {
          trackTeam(event)
          // 新后端先发了结构化的 agent.route.end，这条 log 说的是同一件事
          if (routed.has(`${nodeId}|${num(d.round) ?? 0}`)) break
          const text = String(d.message ?? '')
          const round = (num(d.round) ?? 0) + 1
          // 优先读结构化字段。以前只有一句中文消息串，这里得拿正则去拆
          // 「调度 → X（理由）」——后端文案一改就散架，而文案是会改的。
          // 老运行的事件没有这些字段，正则那条路留着兜底
          const structured = Array.isArray(d.agents)
          const m = text.match(/^调度\s*→\s*([^（(]+)[（(](.*)[）)]\s*$/)
          const legacyTarget = m?.[1].trim() ?? ''
          const agents: string[] = structured
            ? (d.agents as unknown[]).map(String)
            : (legacyTarget && legacyTarget !== 'FINISH' ? [legacyTarget] : [])
          const done = structured ? !!d.done : legacyTarget === 'FINISH'
          const reason = structured ? String(d.reason ?? '') : (m?.[2].trim() ?? '')

          // FINISH 是协议里的收尾标记，不是某个 agent。"交给 FINISH"
          // 会让人以为还有个叫 FINISH 的成员
          const title = done ? `第 ${round} 轮：结束协作`
            : agents.length > 1 ? `第 ${round} 轮：交给 ${agents.join('、')}（并行）`
            : agents.length === 1 ? `第 ${round} 轮：交给 ${agents[0]}`
            : clip(text)
          push({
            id: `sv-${seq}`, seq, kind: 'branch', nodeId, status: 'done',
            title, detail: reason || undefined,
          }, nodeId)
          break
        }
        // 子工作流的进出是 info，但子图节点在时间线上本来就是个黑盒，这两句
        // 是它唯一的内部痕迹
        const message = String(d.message ?? '')
        if (level === 'info' && /^(进入子工作流|子工作流「)/.test(message)) {
          push({ id: `lg-${seq}`, seq, kind: 'note', nodeId, status: 'done', title: clip(message) },
               nodeId)
          break
        }
        // 其余 info 是给排查用的，不进主流程——但 warn/error 用户必须看到
        if (level === 'info' || !message) break
        push({
          id: `lg-${seq}`, seq, kind: 'note', nodeId, status: 'done',
          level: level === 'error' ? 'error' : 'warn',
          title: clip(message, 120),
          ...(message.length > 120 ? { detail: message } : {}),
        }, nodeId)
        break
      }

      case 'run.failed': {
        const msg = String(d.error ?? '运行失败')
        // 节点失败会先报一次，run.failed 往往是同一句话——说两遍不会让人
        // 更明白，只会让人以为出了两个错
        const dup = out.some(function same(s): boolean {
          return (s.status === 'failed' && (s.detail === msg || s.title === msg))
            || (s.children?.some(same) ?? false)
        })
        settle(endingOf(event, awaiting)!)
        if (!dup) {
          // 能定位到节点的话带上，点这一行画布就能取景过去
          const where = String(d.label ?? '') || (d.node_id ? nodeSteps.get(d.node_id)?.title : '')
          out.push({
            id: `rf-${seq}`, seq, kind: 'error', level: 'error', status: 'failed', title: msg,
            ...(d.node_id ? { nodeId: String(d.node_id) } : {}),
            ...(where ? { sub: `出错的节点：${where}` } : {}),
            ...(typeof d.detail === 'string' && d.detail ? { raw: d.detail } : {}),
          })
        }
        break
      }

      case 'run.cancelled':
        // 用户主动停下的，不是出错：收成中性的"已取消"，不画红色的失败
        settle(endingOf(event, awaiting)!)
        out.push({ id: `rc-${seq}`, seq, kind: 'lifecycle', status: 'cancelled', title: '已取消' })
        break

      case 'run.finished': {
        // 开头那条"开始执行"要收尾，否则跑完了还挂着一个转圈的图标。
        // 注意是全部而不是第一条：审批恢复过的运行有"开始执行"+"继续执行"
        // 两条，只收第一条会让"继续执行"永远转圈——明明已经完成了。
        settle(endingOf(event, awaiting)!)
        const timing = d.timing ?? {}
        const active = num(timing.active_ms) ?? num(d.duration_ms)
        const wait = num(timing.wait_ms)
        out.push({
          id: `rd-${seq}`, seq, kind: 'lifecycle', status: 'done',
          title: '完成', ms: active,
          // 等人的时间单独说：审批停了三分钟的运行，"执行 1.2 s"和"用了 3 分钟"
          // 都对，混成一个数就哪个都不对了
          meta: [dur(active), wait ? `等人 ${formatDuration(wait)}` : ''].filter(Boolean).join(' · ')
            || undefined,
        })
        break
      }

      case 'stream.end': {
        // 连接层的结束标记（WS 回放完、服务关停），不成行；带着的状态照样收尾
        const ending = endingOf(event, awaiting)
        if (ending) settle(ending)
        break
      }

      case 'memory.end': {
        // 写入要把**记了什么**原样显示出来。只说"写入 1 条"等于没说——
        // 用户没法判断系统记的是不是他想让它记的，而这东西会影响以后每次对话
        const action = String(d.action ?? '')
        const count = num(d.count) ?? 0
        const content = String(d.content ?? '')
        if (action === 'write') {
          push({
            id: `mw-${seq}`, seq, kind: 'note', nodeId, status: 'done',
            title: content ? `记住了：${content.slice(0, 40)}${content.length > 40 ? '…' : ''}`
                           : '写入了一条记忆',
            detail: content || undefined,
          }, nodeId)
          break
        }
        if (action === 'clear') {
          push({
            id: `mc-${seq}`, seq, kind: 'note', nodeId, status: 'done', level: 'warn',
            title: `清空了记忆域「${String(d.scope ?? '')}」的 ${count} 条`,
          }, nodeId)
          break
        }
        push({
          id: `mr-${seq}`, seq, kind: 'schema', nodeId,
          status: count ? 'done' : 'failed',
          level: count ? undefined : 'warn',
          title: count ? `想起 ${count} 条相关记忆` : '没有想起相关的记忆',
        }, nodeId)
        break
      }

      case 'retrieve.end': {
        // 知识检索。以前只发一条 info 日志，于是"这句结论依据的是哪份文档的
        // 哪一段"在轨迹里查不到——而 SQL 取数那条路早就能下钻到工件
        const count = num(d.count) ?? 0
        const collection = String(d.collection ?? '')
        const top = num(d.top_score)
        push({
          id: `rt-${seq}`, seq, kind: 'schema', nodeId,
          status: count ? 'done' : 'failed',
          level: d.degraded ? 'warn' : count ? undefined : 'warn',
          title: count
            ? `在「${collection}」里检索到 ${count} 段`
            : `在「${collection}」里没检索到内容`,
          detail: String(d.query ?? '') || undefined,
          meta: [
            top != null ? `最高分 ${top}` : '',
            // 退回关键词是"少了一半能力"，不标出来用户只会觉得最近搜得不准
            d.degraded ? '已退回关键词' : '',
          ].filter(Boolean).join(' · ') || undefined,
          artifact: d.artifact,
        }, nodeId)
        break
      }

      default:
        // 新增事件类型时不要静默吞掉——宁可显示一条，也比让人觉得"什么都没
        // 发生"好。但内部类型名（agent.route.end 这种）对使用者是一串代码，
        // 标题、副标题都不放，收进展开区给排查的人认。加了新事件记得回来补映射
        push({
          id: `x-${seq}`, seq, kind: 'note', nodeId, status: 'done',
          title: '一条还没翻译的记录',
          detail: `事件类型：${type}\n${JSON.stringify(d, null, 2).slice(0, 1000)}`,
        }, nodeId)
    }
  }

  // 事件之外知道的结局（对账查到的状态）。已经收过的再收一次也不会变
  if (final?.status) {
    const ending = endingOf(finalEvent(final), phase.awaiting)
    if (ending) settle(ending)
  }

  // 还没收到 end 的工具/模型调用保持 running——它们正在进行，不是丢了
  groupConcurrent(out, spans)

  // 泳道挂到 supervisor 节点的顶层 Step 上。节点内部那些"第N轮交给谁""X：指令"
  // 仍然留着——泳道给的是一眼看清的形状，那些步骤给的是内容，两者不重复
  for (const [nodeId, team] of teams) {
    if (!team.rounds.length) continue
    const step = nodeSteps.get(nodeId)
    if (step) step.team = team
  }

  return out
}

// -------------------------------------------------------------------------
// 给界面用的统计：进度、用量、相邻重复行的合并
// -------------------------------------------------------------------------

export interface Progress {
  /** 已经完成（或跳过）的不同节点数 */
  done: number
  /** 图里一共几个节点；老数据没有就是 undefined */
  total?: number
  /** 此刻正在跑的那一步（最深的一层） */
  current?: Step
}

/**
 * 跑到第几个节点了。按不同节点数算，不按执行次数：循环会让同一节点执行十几次，
 * 按次数算会出现「9/8」这种比总数还多的读数。
 */
export function progressOf(steps: Step[]): Progress {
  let total: number | undefined
  const nodes = new Map<string, Step>()
  let current: Step | undefined
  const walk = (list: Step[], depth: number) => {
    for (const s of list) {
      if (s.kind === 'lifecycle' && s.total != null && total == null) total = s.total
      if (s.kind === 'node' && s.nodeId && depth <= 1) nodes.set(s.nodeId, s)
      if (s.status === 'running' && s.kind !== 'lifecycle') current = s
      if (s.children) walk(s.children, s.kind === 'branch' && !s.nodeId ? depth : depth + 1)
    }
  }
  walk(steps, 0)
  let done = 0
  for (const s of nodes.values()) {
    if (s.status === 'done' || s.status === 'skipped' || (s.execs?.some((x) => x.status === 'done'))) done += 1
  }
  return { done, ...(total != null ? { total } : {}), ...(current ? { current } : {}) }
}

export interface UsageSummary {
  tokensIn: number
  tokensOut: number
  costUsd: number
  /** true = 取自 run.finished 的后端累计（权威）；false = 实时累加的，可能偏少 */
  final: boolean
  activeMs?: number
  waitMs?: number
  wallMs?: number
}

/**
 * 一次运行的用量和时长。运行中按 llm.end 累加（只用来显示，会偏少），到终态用
 * run.finished 的后端累计校正——和画布 store 的 usageLive 同一个口径。
 */
export function usageOf(events: RunEvent[]): UsageSummary {
  let tokensIn = 0
  let tokensOut = 0
  let costUsd = 0
  let final = false
  let timing: Record<string, any> | undefined
  for (const e of events) {
    const d: any = e.data ?? {}
    if (e.type === 'llm.end') {
      tokensIn += num(d.input_tokens) ?? 0
      tokensOut += num(d.output_tokens) ?? 0
      costUsd += num(d.cost_usd) ?? 0
    } else if (e.type === 'run.finished' && d.usage) {
      tokensIn = num(d.usage.input_tokens) ?? tokensIn
      tokensOut = num(d.usage.output_tokens) ?? tokensOut
      costUsd = num(d.usage.cost_usd) ?? costUsd
      final = true
    }
    if ((e.type === 'run.finished' || e.type === 'run.failed' || e.type === 'run.cancelled')) {
      timing = { ...(d.timing ?? {}), ...(num(d.duration_ms) != null && !d.timing ? { active_ms: d.duration_ms } : {}) }
    }
  }
  return {
    tokensIn, tokensOut, costUsd, final,
    ...(num(timing?.active_ms) != null ? { activeMs: timing!.active_ms } : {}),
    ...(num(timing?.wait_ms) != null ? { waitMs: timing!.wait_ms } : {}),
    ...(num(timing?.wall_ms) != null ? { wallMs: timing!.wall_ms } : {}),
  }
}

const median = (xs: number[]): number => {
  const s = [...xs].sort((a, b) => a - b)
  const mid = Math.floor(s.length / 2)
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2
}

/**
 * 相邻的同一件事并成一行：「运行 Python 代码 ×145 · 中位 12 ms · 最慢 63 ms」。
 *
 * 轮询、逐拍控制这类流程会连着出现上百行一模一样的步骤，一行一行排开只是
 * 让人滚动几万像素，最慢那一拍、出警告的那一拍反而被淹没。并的时候保留
 * level：「循环达到上限 ×136」照样是琥珀色。有子步骤、有泳道、没做完的不并——
 * 它们各有各的内容。返回新数组，原数组不动。
 */
export function compactSteps(steps: Step[]): Step[] {
  const out: Step[] = []
  const same = (a: Step, b: Step) => a.kind === b.kind && a.title === b.title
    && a.level === b.level && a.status === b.status && a.nodeId === b.nodeId
  const mergeable = (s: Step) => !s.children?.length && !s.team && !s.execs?.length
    && s.status !== 'running' && s.status !== 'waiting'
  for (const s of steps) {
    const last = out[out.length - 1]
    if (last && mergeable(s) && mergeable(last) && same(last, s)) {
      const rep = last.repeat ?? { count: 1, ms: last.ms != null ? [last.ms] : [] }
      rep.count += 1
      if (s.ms != null) rep.ms.push(s.ms)
      out[out.length - 1] = { ...last, repeat: rep, meta: repeatMeta(rep) }
      continue
    }
    out.push(s)
  }
  return out
}

function repeatMeta(rep: { count: number; ms: number[] }): string {
  const parts = [`×${rep.count}`]
  if (rep.ms.length >= 2) {
    const m = dur(median(rep.ms))
    const max = dur(Math.max(...rep.ms))
    if (m) parts.push(`中位 ${m}`)
    if (max && max !== m) parts.push(`最慢 ${max}`)
  }
  return parts.join(' · ')
}

/** 分轮折叠用：按子步骤的 iter 分组，保持出现顺序 */
export function childrenByExec(step: Step): { exec: Exec; steps: Step[] }[] {
  const execs = step.execs ?? []
  const groups = execs.map((exec) => ({ exec, steps: [] as Step[] }))
  for (const c of step.children ?? []) {
    const g = groups[(c.iter ?? 1) - 1] ?? groups[groups.length - 1]
    g?.steps.push(c)
  }
  return groups
}

/** 一串耗时的统计，给分轮折叠的摘要行和折线用 */
export function spread(ms: number[]): { median?: number; max?: number; maxAt?: number } {
  const xs = ms.filter((x) => Number.isFinite(x))
  if (!xs.length) return {}
  const max = Math.max(...xs)
  return { median: median(xs), max, maxAt: ms.indexOf(max) }
}

// -------------------------------------------------------------------------
// Copilot 建图阶段：操作流 → 同一套 Step
// -------------------------------------------------------------------------

export interface CopilotOp {
  op: string
  [k: string]: any
}

/**
 * 一段思考在列表里显示哪一句。
 *
 * 还在想的时候取**最后一句**：思考是流式的，第一句往往是"用户想要一个……"
 * 这种复述，之后几十秒里它其实一直在推进（"先看看现有的节点"、"这里需要
 * 一个分支"）。固定显示开头，等于把一段活的叙述冻在起点上，看着就像卡住了。
 *
 * 想完了取**开头**：这时它是一条历史记录，开头那句最接近"这段在想什么"。
 */
function thinkingHeadline(text: string, live: boolean): string {
  const flat = text.replace(/\s+/g, ' ').trim()
  if (!flat) return '正在思考…'
  if (!live) return flat.slice(0, 60) + (flat.length > 60 ? '…' : '')
  // 按中英文句末切；最后一段往往还没说完，取它前面那句更完整
  const parts = flat.split(/(?<=[。！？；.!?;])\s*/).filter(Boolean)
  const tail = parts.length > 1 && parts[parts.length - 1].length < 6
    ? parts[parts.length - 2]
    : parts[parts.length - 1]
  const line = tail ?? flat
  return line.length > 60 ? '…' + line.slice(-60) : line
}

const PHASE_LABEL: Record<string, string> = {
  connecting: '正在连接模型',
  planning: '正在理解需求、规划步骤',
  building: '正在搭建流程',
  wiring: '正在连接数据流',
  finalizing: '正在排版和校验',
  repairing: '正在按自查结果修正',
}

/** 自查最多修几轮。和后端 copilot._SELF_CHECK_ROUNDS 一致，check 事件只带当前第几轮 */
export const SELF_CHECK_ROUNDS = 2

export interface CopilotIssue {
  message: string
  /** 问题落在哪个节点上（能认出来时）。有它才能给「定位」 */
  nodeId?: string
}

/**
 * 自查问题是后端拼好的一行字：「「lp」循环条件写错了」。节点 id 包在开头的
 * 「」里，认出来才能在卡片上配「定位」；认不出就只是一行字。
 */
function parseIssue(v: unknown): CopilotIssue {
  if (v && typeof v === 'object') {
    const o = v as Record<string, unknown>
    return { message: String(o.message ?? ''), ...(o.node_id ? { nodeId: String(o.node_id) } : {}) }
  }
  const text = String(v ?? '')
  const m = text.match(/^「([^」]+)」\s*(.*)$/)
  return m ? { nodeId: m[1], message: m[2] || text } : { message: text }
}

/**
 * Copilot 的操作流解码。
 *
 * 和运行事件是两套完全独立的协议（不共享 seq、node_id、不落库），只在这里
 * 统一成同一种 Step，让用户看到的是一条连续的过程，而不是"建图"和"执行"
 * 两段风格迥异的日志。每一行都标 stage='plan'，问数据页把建图和执行排成
 * 一列时，界面据此分出「规划」「执行」两段。
 *
 * context：画布上从不自动运行，「没有自动运行」是问数据页的说法，放到画布上
 * 读起来像出了别的故障。
 */
export function decodeCopilot(ops: CopilotOp[], opts?: { context?: 'canvas' | 'chat' }): Step[] {
  const canvas = opts?.context === 'canvas'
  const out: Step[] = []
  let i = 0
  let nodeCount = 0
  let repairRound = 0

  /**
   * 思考结束了：不再显示"最新一句"，换成开头那句，收掉转圈。收全部而不是最后
   * 一条——心跳穿插进来之后思考会被切成几段，只收最后一段的话，前面那几段
   * 在「流程搭好了」之后还在转
   */
  const settleThinking = () => {
    for (const s of out) {
      if (s.kind === 'think' && s.status === 'running') {
        s.status = 'done'
        s.title = thinkingHeadline(s.detail ?? '', false)
      }
    }
  }
  /**
   * 这一段结束了：还挂着的阶段行、思考行一律收掉。阶段行的「正在…」改成过去
   * 时——收了尾的行还写着「正在理解需求」，读起来就是还没好
   */
  const settleAll = (status: StepStatus) => {
    settleThinking()
    for (const s of out) {
      if (s.kind === 'lifecycle' && s.status === 'running') s.title = s.title.replace(/^正在/, '')
    }
    closeLifecycles(out, status)
  }
  const add = (step: Omit<Step, 'stage'>) => { out.push({ ...step, stage: 'plan' }) }

  for (const op of ops) {
    i += 1
    // 除了继续思考和心跳，任何一条操作都说明它已经想完、开始动手了
    if (op.op !== 'thinking' && op.op !== 'heartbeat') settleThinking()
    switch (op.op) {
      case 'thinking': {
        const text = String(op.delta ?? '')
        if (!text.trim()) break
        // 思考是连续流，一片 delta 一行会碎成几十条。同一段连续思考并成一条，
        // 详情是全文——这才是"一次思考是一个节点"。心跳插在中间不算打断：
        // 往回找最近那条还在想的，中间隔着的只能是阶段行
        let host: Step | undefined
        for (let k = out.length - 1; k >= 0; k -= 1) {
          const s = out[k]
          if (s.kind === 'think' && s.status === 'running') { host = s; break }
          if (s.kind !== 'lifecycle') break
        }
        if (host) {
          host.detail = (host.detail ?? '') + text
          host.title = thinkingHeadline(host.detail, true)
        } else {
          add({ id: `ct-${i}`, seq: i, kind: 'think', status: 'running',
                title: thinkingHeadline(text, true), detail: text })
        }
        break
      }
      case 'heartbeat': {
        // 心跳只更新"还活着"的那一条阶段行，不该每 3 秒堆一行，也不该插在
        // 思考中间把一段思考切成两截
        const label = op.phase === 'repairing' && repairRound
          ? `${PHASE_LABEL.repairing}（第 ${repairRound}/${SELF_CHECK_ROUNDS} 轮）`
          : PHASE_LABEL[String(op.phase)] ?? '正在处理'
        const elapsed = num(op.elapsed_ms)
        const phaseRow = [...out].reverse().find((s) => s.kind === 'lifecycle' && s.status === 'running')
        if (phaseRow) {
          phaseRow.title = label
          if (elapsed) { phaseRow.ms = elapsed; phaseRow.meta = formatDuration(elapsed) }
          break
        }
        const last = out[out.length - 1]
        if (last?.kind === 'think' && last.status === 'running') {
          if (elapsed) { last.ms = elapsed; last.meta = formatDuration(elapsed) }
          break
        }
        add({ id: `hb-${i}`, seq: i, kind: 'lifecycle', status: 'running', title: label,
              ...(elapsed ? { ms: elapsed, meta: formatDuration(elapsed) } : {}) })
        break
      }
      case 'plan':
        add({ id: `cp-${i}`, seq: i, kind: 'note', status: 'done',
              title: clip(String(op.summary ?? '想好了怎么做'), 120) })
        break
      case 'add_node':
        nodeCount += 1
        add({
          id: `cn-${i}`, seq: i, kind: 'node', status: 'done',
          title: clip(String(op.node?.data?.label || op.node?.label || op.node?.id || '新步骤')),
          // 类型说中文：行尾挂着 input / memory 这种英文，读起来像日志
          ...(op.node?.type ? { meta: nodeTypeLabel(String(op.node.type)) } : {}),
          ...(op.node?.id ? { nodeId: String(op.node.id) } : {}),
        })
        break
      case 'update_node':
        add({ id: `cu-${i}`, seq: i, kind: 'note', status: 'done', nodeId: String(op.id ?? ''),
              title: clip(`调整了「${op.label || op.id}」`) })
        break
      case 'remove_node':
        add({ id: `cr-${i}`, seq: i, kind: 'note', status: 'done',
              title: clip(`去掉了「${op.id}」`) })
        break
      // 连线不单独成行：用户关心有哪些步骤，不关心箭头
      case 'add_edge':
      case 'remove_edge':
        break
      case 'done':
        // 心跳那条"正在理解需求…"要收尾，否则生成完了它还在转圈
        settleAll('done')
        add({ id: `cd-${i}`, seq: i, kind: 'lifecycle', status: 'done',
              title: `流程搭好了，加了 ${nodeCount} 步`,
              detail: String(op.explanation ?? '') || undefined })
        break
      case 'check': {
        // 服务端自查：用运行时同一套规则过一遍，有问题交回模型改。
        // 这件事得看得见——否则多出来的那十几秒像是卡住了，改了什么也无从知道
        const issues = Array.isArray(op.issues) ? op.issues.map((x: unknown) => parseIssue(x)) : []
        const lines = issues.map((x: CopilotIssue) => (x.nodeId ? `「${x.nodeId}」${x.message}` : x.message))
        if (op.status === 'repairing') {
          repairRound = num(op.round) ?? repairRound + 1
          add({ id: `ck-${i}`, seq: i, kind: 'note', status: 'done', level: 'warn',
                title: `自查发现 ${issues.length} 处问题，交回去改（第 ${repairRound}/${SELF_CHECK_ROUNDS} 轮）`,
                detail: lines.join('\n') || undefined })
        } else if (op.status === 'passed') {
          settleAll('done')
          add({ id: `ck-${i}`, seq: i, kind: 'note', status: 'done',
                title: op.repaired ? '自查通过：问题已经改好' : '自查通过' })
        } else {
          // 修正那一轮本身是跑完了的；没修好由下面这行红字说，不把阶段行也画红
          settleAll('done')
          add({ id: `ck-${i}`, seq: i, kind: 'error', level: 'error', status: 'failed',
                title: op.status === 'failed'
                  ? canvas
                    ? `自查后还有 ${issues.length} 处问题没能自动修好`
                    : `自查后还有 ${issues.length} 处问题，没有自动运行`
                  : String(op.message ?? '自查没能完成'),
                detail: lines.join('\n') || undefined,
                ...(typeof op.detail === 'string' && op.detail ? { raw: op.detail } : {}) })
        }
        break
      }
      case 'final': {
        // 后端排版校验后的最终图才知道整张图有几步。nodeCount 只是这一轮
        // 新增的数量——在"改图"场景下说"共 2 步"是错的，图上明明有四个节点。
        // 自查的记录会排在"搭好了"后面，所以往回找那一行，不能只看最后一条
        const total = op.graph?.nodes?.length
        const last = out[out.length - 1]
        // 流没等到 done 就断了时没有那一行，沿用原来的做法：改写最后一条生命周期
        const built = [...out].reverse().find((s) => s.id.startsWith('cd-'))
          ?? (last?.kind === 'lifecycle' ? last : undefined)
        if (total && built) {
          built.title = nodeCount && nodeCount < total
            ? `流程搭好了，加了 ${nodeCount} 步，整张图共 ${total} 步`
            : `流程搭好了，共 ${total} 步`
        }
        settleAll('done')
        // 模型写了不存在的节点类型：那一步被跳过了。不说出来的话"共 N 步"是
        // 一句不完整的真话，用户要到运行结果不对才发现少了一步
        for (const t of skippedTypes(op)) {
          add({ id: `cm-${i}-${t}`, seq: i, kind: 'note', status: 'done', level: 'warn',
                title: clip(`少了一步：模型用了不存在的节点类型「${t}」，已跳过`, 80) })
        }
        break
      }
      case 'reply':
        // 直接回答、没有改图。回答本身作为成果显示，这里只把还在转的收掉
        settleAll('done')
        break
      case 'error':
        settleAll('failed')
        add({ id: `ce-${i}`, seq: i, kind: 'error', level: 'error', status: 'failed',
              title: String(op.message ?? '生成失败'),
              ...(op.hint ? { sub: String(op.hint) } : {}),
              ...(typeof op.detail === 'string' && op.detail ? { raw: op.detail } : {}) })
        break
      default:
        break
    }
  }
  return out
}

function skippedTypes(op: CopilotOp): string[] {
  const issues: any[] = Array.isArray(op.issues) ? op.issues : []
  return [...new Set(issues
    .filter((x) => x && typeof x === 'object' && x.code === 'unknown_node_type')
    .map((x) => String(x.type ?? '').trim() || '未知'))]
}

/**
 * 一轮 Copilot 的结局，给卡片标题和补救动作用。
 *
 * 以前卡片只看 phase：done 一律写「流程已更新到画布」——模型只回了一句话、
 * 自查没修好、少放了一步，标题都一样。这几种的下一步完全不同（读回答 /
 * 去修 / 让它补上），标题得先分开。
 */
export interface CopilotOutcome {
  /** reply：只回了话；built：图放上去了；error：出错；running：还在生成；empty：流结束了但什么都没做 */
  kind: 'running' | 'reply' | 'built' | 'error' | 'empty'
  reply?: string
  /** 自查：passed 通过（repaired 为修过几轮）、failed 没修好、error 自查本身没跑成 */
  check?: { status: 'passed' | 'failed' | 'error'; repaired: number; issues: CopilotIssue[] }
  /** 正在修第几轮 */
  repairing?: number
  /** 被跳过的节点类型（模型编出来的） */
  skipped: string[]
  /** 整张图几个节点 */
  total?: number
  added: number
  updated: number
  removed: number
  error?: { message: string; hint?: string; raw?: string }
}

export function copilotOutcome(ops: CopilotOp[], running = false): CopilotOutcome {
  const res: CopilotOutcome = { kind: running ? 'running' : 'empty', skipped: [], added: 0, updated: 0, removed: 0 }
  for (const op of ops) {
    switch (op.op) {
      case 'add_node': res.added += 1; break
      case 'update_node': res.updated += 1; break
      case 'remove_node': res.removed += 1; break
      case 'reply':
        res.reply = String(op.text ?? '')
        if (!running) res.kind = 'reply'
        break
      case 'check': {
        const issues = Array.isArray(op.issues) ? op.issues.map((x: unknown) => parseIssue(x)) : []
        if (op.status === 'repairing') {
          res.repairing = num(op.round) ?? (res.repairing ?? 0) + 1
        } else {
          res.check = {
            status: op.status === 'passed' ? 'passed' : op.status === 'failed' ? 'failed' : 'error',
            repaired: num(op.repaired) ?? 0,
            issues,
          }
        }
        break
      }
      case 'final':
        res.total = op.graph?.nodes?.length
        res.skipped = skippedTypes(op)
        if (!running) res.kind = 'built'
        break
      case 'error':
        res.error = {
          message: String(op.message ?? '生成失败'),
          ...(op.hint ? { hint: String(op.hint) } : {}),
          ...(typeof op.detail === 'string' && op.detail ? { raw: op.detail } : {}),
        }
        if (!running) res.kind = 'error'
        break
    }
  }
  return res
}
