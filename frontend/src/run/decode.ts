import type { RunEvent, TeamMember, TeamRound, TeamRun, ToolChange } from '../types'
import type { NodeState, RunPhase } from './trace'
import { formatDuration, formatNumber } from '../lib/format'
import {
  CATALOG_PATCH_TEXT, JUDGE_TEXT, MERGE_TEXT, TYPE_LABEL, claimTally, evidenceTally, issuanceLabel, nodeTypeLabel,
} from '../lib/terms'
import { catalogPatchOf, patchValueText, patchWhere } from './catalogPatch'
import { claimCountsOf, isJudged, statsTally } from '../lib/evidence'

// 泳道数据画布也要用（supervisor 节点要展开成协作矩阵），所以类型放在
// types.ts 里；这里再导出一遍，老引用不用改
export type { TeamMember, TeamRound, TeamRun }

/**
 * 协作团队怎么收的尾。只看 finished 分不出「调度者说完成了」和「轮数用完、硬停」——
 * 后者以前把成员的原话当结论交了出去，画布和右栏却都是一个安静的「完成」。
 */
export interface TeamVerdict {
  /** 轮数用完后补的那一次只判定、不派活的决定（agent.route.* 带 closing） */
  closing?: boolean
  /** 那次判定的结论 */
  done?: boolean
  reason?: string
  /** 用完轮数仍未完成的结局：failed 判失败（默认），degraded 降档交付 */
  outcome?: 'failed' | 'degraded'
  /** 一共派了几轮 */
  rounds?: number
  /** 一次都没被派到的成员 */
  never?: string[]
}

/** 成员这一步为什么失败（agent.step.end 的 error）。types.ts 冻结，先在这里扩 */
export interface TeamMemberEx extends TeamMember { error?: string }
export interface TeamRunEx extends TeamRun {
  verdict?: TeamVerdict
  /**
   * 这一次执行停在审批上（human.requested）。接下来那条 node.started 是恢复后的重放，
   * 同一次执行接着走；别的 node.started 都是新的一次，泳道和结局从头记
   */
  paused?: true
}

/** 画布的协作矩阵读这个：runtime.team 是 reduceTeam 给的，带着结局 */
export const teamVerdictOf = (team: TeamRun | undefined | null): TeamVerdict | undefined =>
  (team as TeamRunEx | undefined | null)?.verdict

/** 下一步该去哪儿改。界面据此给一个直达的入口，而不只是一句话 */
export type FixKind = 'canvas' | 'settings' | 'tools'

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
  /** 「开始运行」那一行：图里一共有几个节点 */
  total?: number
  /** 查询走的数据源 */
  source?: string
  /** 相邻的同一件事合并成一行后的统计（见 compactSteps） */
  repeat?: { count: number; ms: number[] }
  /** 后端给的机读代号（log.code 等）：tool_markup_leak、team_exhausted、repair… */
  code?: string
  /** 怎么办：出了状况的行直接说下一步，不让人自己去猜 */
  next?: string
  /** 下一步该去的地方 */
  fix?: FixKind
  /** 工具的时限（秒，tool.start.timeout_s）。进行中的行超过它就要说破 */
  limitS?: number
  /** 出具那一行的档位（formal / degraded / withheld） */
  tier?: string
  /** 合并查询那一行（merge.end）：合并了哪几个输入、几条警告。SQL 在 detail，预览在 result */
  merge?: MergeStep
}

/** 合并查询的一个输入：合并 SQL 里的别名、上游节点、行数、数据源 */
export interface MergeStepInput {
  alias: string
  nodeId?: string
  label?: string
  rows?: number
  source?: string
}

export interface MergeStep {
  inputs: MergeStepInput[]
  /** 执行时的警告条数。警告的原文由同一节点紧接着的 log（merge_key_type…）各占一行，不在这里重复 */
  warnings: number
  rows?: number
  truncated: boolean
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
 * 照样流；右栏只收"开始运行"那一行，节点下面的工具、查询还在转，取消还被画成
 * 红色失败。一个假的"运行中"比没有动效更糟，所以规则只写这一份，两边都调它。
 */
export interface Ending {
  phase: RunPhase
  /** 还在跑的节点、步骤、协作成员 */
  running: NodeState
  /** 停在审批上的 */
  waiting: NodeState
  /** "开始运行 / 继续运行"那种生命周期行：这一段执行本身的结局 */
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
 * 分支、循环的出口在图上叫什么。edge.taken 只带出口的 key：右栏写「走「team」这条路」，
 * 而画布出口上写的是 case 的说明，两处对不上号。给不出（没加载图、图改过）时退回 key
 */
export type ExitLabelOf = (nodeId: string, key: string) => string | undefined

export interface DecodeOptions {
  exitLabelOf?: ExitLabelOf
}

/**
 * 从图里攒出口说明。出口怎么命名（兜底叫「其他」、default 撞名合并）只有画布那边的
 * sourceHandles 说了算，由调用方传进来：这里 import 画布模块的话，解码器就不再是
 * 能在 node 里直接跑的纯函数了
 */
export function exitLabels(
  nodes: { id: string; type: string; config?: Record<string, any> }[],
  handles: (type: any, config: Record<string, any>) => { id: string; label: string }[],
): ExitLabelOf {
  const map = new Map<string, string>()
  for (const n of nodes) {
    if (n.type !== 'branch' && n.type !== 'loop') continue
    for (const h of handles(n.type, n.config ?? {})) if (h.label) map.set(`${n.id}\u0000${h.id}`, h.label)
  }
  return (id, key) => map.get(`${id}\u0000${key}`)
}

/** 没有图时的叫法，和画布出口上写的一样 */
const LOOP_EXIT: Record<string, string> = { body: '循环体', done: '结束' }

/**
 * 循环走 done 时这一轮说什么。后端的 iteration 是「这次决定之前已经跑过几轮」：走循环体时
 * +1 是正要开始的那一轮，走 done 时它本身就是一共跑了几轮——按 +1 写，5 项的 foreach
 * 走完就成了「第 6 轮」，一项都没有的成了「第 1 轮」
 */
function loopDoneNote(ran: number, total: number | undefined): string {
  if (total != null) {
    if (total === 0) return '（没有要处理的项）'
    return ran >= total ? `（共 ${total} 项）` : `（执行了 ${ran} 轮，共 ${total} 项）`
  }
  return ran === 0 ? '（未执行）' : `（执行了 ${ran} 轮）`
}

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

/** agent 开了 cite_fields 时循环结束后那次结构化抽取 */
const EXTRACT_TITLE = '按出处抽取字段'
/**
 * report.checked 带的裁判摘要 → 「结论 4 句（有依据 1 · 证据相矛盾 1 · 未裁判 2）」，和出具横幅同一种说法。
 * 「不是结论句」不算，旧数据的 unsupported 照样计入；没有摘要（没开裁判、老后端）返回空串
 */
function judgeTally(judge: unknown): string {
  const c = judge && typeof judge === 'object' ? (judge as { counts?: Record<string, unknown> }).counts : undefined
  if (!c || typeof c !== 'object') return ''
  return claimTally(claimCountsOf(c))
}

/** 报告撰写节点请裁判模型判断结论句（四期，claims: judge）：units 是这一批几句 */
const judgeTitle = (units: unknown) =>
  `请裁判模型判断${typeof units === 'number' && units > 0 ? ` ${formatNumber(units)} 句` : ''}结论`
const JUDGE_FAILED_TITLE = '结论句裁判未能执行'

/** 审批行末尾：批的时候点了「始终允许」，这个工具之后不再问人 */
const ALWAYS_NOTE = '，并设为「始终允许 · 门控把关」'

/**
 * 门控模型（工具信任档「始终允许 · 门控把关」）对一次调用的结论。team：协作团队里拦下的，
 * 成员停不下来等人，这次调用不执行。没有工具名（老数据、字段缺了）就说「一次工具调用」
 */
function gateTitle(tool: string, verdict: 'allow' | 'escalate' | 'team', reason: string): string {
  const what = tool ? ` ${tool}` : '一次工具调用'
  const head = verdict === 'allow' ? `门控通过${what}`
    : `门控拦截${what}，${verdict === 'team' ? '协作团队中不执行' : '转交人工审批'}`
  return reason ? `${head}：${reason}` : head
}

/**
 * 几种「看着跑完了、其实没做成」的状况（NI-3/4/5）。后端这几条日志是写给排查的，原样
 * 贴出来是一串标记和术语，而且不说该去哪儿改——这里说成人话，并给出下一步。
 * 认不得的 code 返回 null，照原话显示
 */
function explainLog(code: string | undefined, message: string): Pick<Step, 'title' | 'sub' | 'next' | 'fix'> | null {
  switch (code) {
    case 'tool_markup_leak': {
      // 「数据查询把工具调用写成了文字（<｜｜DSML｜｜…），没有真正调用工具，已提醒它重试一次」
      const who = message.match(/^(.+?)(?:把工具调用写成了文字|以文本形式输出了工具调用)/)?.[1]?.trim() || '模型'
      // 收尾轮那种是真调过工具、只是步数用完了还想接着查：不是没绑工具，节点也照常完成，
      // 说成「没有真正执行」会让人去查一个并不存在的故障
      const settle = message.includes('收尾')
      return {
        title: settle ? `${who === '模型' ? '' : `${who}：`}已达步数上限，收尾时仍试图调用工具`
          : `${who}以文本形式输出了工具调用，未实际执行`,
        sub: settle ? '收尾时的工具调用未执行，结论仅基于此前的查询结果'
          : message.includes('重试') ? '已要求模型重试一次' : undefined,
        next: settle ? '请在画布中调大该步骤的「最大步数」，或将问题描述得更具体'
          : `常见原因：${who === '模型' ? '该节点' : `成员「${who}」`}未绑定工具，或模型不支持工具调用。请在画布中为其绑定所需工具`,
        fix: 'canvas',
      }
    }
    case 'team_exhausted': {
      const x = exhaustedOf(message)
      return {
        title: `协作团队${x?.rounds != null ? `已用完 ${x.rounds} 轮` : '已用完轮数'}仍未完成 · 按降档交付`,
        sub: [x?.reason, x?.never?.length ? `未分派：${x.never.join('、')}` : ''].filter(Boolean).join(' · ') || undefined,
        next: '交付的是成员最后一次回复，不可作为结论使用。请在画布中检查成员是否绑定了所需工具，再调大「最多轮数」',
        fix: 'canvas',
      }
    }
    case 'report_repair': {
      // 「报告里有 3 处没通过核对（「12」「m:nope」），已要求写作者重写（第 1 次）」。没通过的
      // 不只是裸数字，还有解析不了的引用（m:nope），标题不能只说「没写引用」
      const n = message.match(/报告里?有\s*(\d+)\s*处/)?.[1]
      const round = message.match(/第\s*(\d+)\s*次/)?.[1]
      return {
        title: `报告${n ? `有 ${n} 处` : ''}未通过核对，已要求模型按清单重写${round ? `（第 ${round} 次）` : ''}`,
        sub: message.match(/(?:没|未)通过核对（(.+?)）/)?.[1],
      }
    }
    case 'agent_field_mismatch': {
      // 「有 1 个字段和查询快照对不上，已按快照取值：order_cnt 模型报 1240，快照是 1234」（llm.py _field_warnings）
      const n = message.match(/有\s*(\d+)\s*个字段/)?.[1]
      return {
        title: `有${n ? ` ${n} 个` : ''}字段与查询快照不一致，已按快照取值`,
        sub: message.match(/已按快照取值[：:]\s*(.+)$/)?.[1]?.trim() || undefined,
        next: '下游使用查询快照中的值，而非模型给出的值。模型给出的数值可能存在抄录错误、四舍五入或自行计算的情况',
      }
    }
    case 'agent_field_unverified': {
      // 两种原话：「有 N 个字段核对不了出处，记为空值（没有兜底成 0）：a（原因）；b（原因）」；
      // 抽取没成：「<原因>。N 个字段都记为空值（没有兜底成 0）」，原因是「结构化抽取没跑成：<调用的错>」
      // 或者「抽取结果不是一个对象」（llm.py 的 failed）
      const failed = message.match(/^(?:结构化抽取(?:没跑成|失败)[：:])?([\s\S]+?)。\s*(\d+)\s*个字段都?记为空值/)
      if (failed) {
        return {
          title: `按出处抽取字段失败，${failed[2]} 个字段记为空值（未以 0 代替）`,
          sub: failed[1].trim() || undefined,
          next: '这些字段以空值传给下游，口径卡按缺少输入处理。问题解决后请重新运行',
        }
      }
      const n = message.match(/有\s*(\d+)\s*个字段/)?.[1]
      return {
        title: `有${n ? ` ${n} 个` : ''}字段无法核对出处，记为空值（未以 0 代替）`,
        sub: message.match(/记为空值（[^）]*）[：:]\s*(.+)$/)?.[1]?.trim() || undefined,
        next: '模型给出的出处在查询结果中不存在。请让模型先查询到这些字段再提交，查询不到的字段记为空值',
      }
    }
    case 'metric_incomplete': {
      // 「指标「入园人次」基于被截断的查询结果计算（只取回了前 1000 行），结果不完整」（metrics.py _incomplete_reason）
      const name = message.match(/^指标「(.+?)」/)?.[1]
      return {
        title: name ? `指标「${name}」结果不完整` : '指标结果不完整',
        sub: message.match(/^指标「.+?」(.+?)，结果不完整$/)?.[1],
        next: '口径卡对截断的查询结果计数、求和，只算到了取回的部分，出具按缺口降档。'
          + '请在 SQL 中直接聚合（如 COUNT、SUM）或缩小查询范围后重新运行',
        fix: 'canvas',
      }
    }
    case 'judge_limit': {
      // 「结论句裁判已到上限（这份报告的裁判金额上限 $0.05）：3 句没判，记为未裁判；已判的保留」（judge.py run_request）
      const n = message.match(/[：:]\s*(\d+)\s*句(?:没判|未裁判)/)?.[1]
      return {
        title: `结论句裁判已达上限${n ? `，${n} 句未裁判` : ''}（已裁判的结果保留）`,
        sub: message.match(/已[到达]上限（(.+?)）/)?.[1],
        next: '存在未裁判的结论句时不能完整出具。请在报告撰写节点的「结论句裁判」中调高上限（或设为不限），或在「设置 → 偏好设置 → 证据裁判」中修改默认值',
        fix: 'canvas',
      }
    }
    case 'merge_key_type': {
      // 「合并键类型不一致：s.门店 是文本（例如 '01'），v.门店 是数值（例如 1）。SQLite 比较时会做隐式转换…」
      // （engine/merge_query._warnings）。标题说是哪两列，副标题说各是什么类型
      const m = message.match(/^合并键类型不一致[：:]\s*(.+?)。/)
      const keys = [...(m?.[1] ?? '').matchAll(/([\w一-龥]+\.[^\s，,]+)\s*是/g)].map((x) => x[1])
      return {
        title: keys.length === 2 ? `合并键类型不一致：${keys[0]} 与 ${keys[1]}` : '合并键类型不一致',
        sub: m?.[1],
        next: '两边类型不同时，SQLite 按隐式转换比较，可能错配或完全匹配不上。请在源查询中统一类型，或在合并 SQL 中用 CAST 明确转换',
        fix: 'canvas',
      }
    }
    case 'merge_rows_grew': {
      // 「合并结果有 8 行，多于行数最多的输入「s」（4 行）：合并键可能不唯一，同一行被重复匹配。…」
      const head = message.match(/^合并结果有\s*([\d,]+)\s*行，多于行数最多的输入「(.+?)」（([\d,]+)\s*行）/)
      const dup = message.match(/重复匹配。(.+?)。请检查/)?.[1]
      return {
        title: '合并结果行数多于输入，合并键可能不唯一',
        sub: [head ? `合并结果 ${head[1]} 行，行数最多的输入「${head[2]}」${head[3]} 行` : '', dup ?? '']
          .filter(Boolean).join('；') || undefined,
        next: '请检查合并条件是否覆盖了全部合并键（例如同时按日期和门店），或先在源库里聚合到相同粒度',
        fix: 'canvas',
      }
    }
    case 'merge_truncated': {
      const rows = message.match(/只保留了前\s*([\d,]+)\s*行/)?.[1]
      return {
        title: `合并结果超过上限，${rows ? `只保留了前 ${rows} 行` : '已截断'}`,
        next: '下游拿到的不是完整结果。请在合并 SQL 中聚合或加条件缩小范围',
        fix: 'canvas',
      }
    }
    case 'judge_failed': {
      // 「结论句裁判没跑完：有 2 句结论没裁判：裁判调用失败（…）」
      return {
        title: '结论句裁判未完成，未判定的结论句记为未裁判',
        sub: message.replace(/^结论句裁判(?:没跑完|未完成)[：:]\s*/, '') || undefined,
        next: '未裁判的结论句记为缺口，不能完整出具。请先检查裁判模型的接入（设置 → 偏好设置 → 证据裁判）是否可用，再重新运行',
      }
    }
    case 'judge_unpriced': {
      // 「模型「x」不在价格目录里，按令牌估不出金额：金额上限（每份报告、每次点击、每日）对它不起作用，…」
      const model = message.match(/模型「(.+?)」/)?.[1]
      return {
        title: `裁判模型${model ? `「${model}」` : ''}无法估算金额，金额上限对其无效`,
        sub: message.split(/[：:]/).slice(1).join('：').trim() || undefined,
        next: '费用仅受句数和时长上限约束。如需按金额控制，请换用价格目录中已有的裁判模型',
      }
    }
    case 'judge_same_model': {
      // 「裁判模型和写作模型都是「x」：等于自己审自己，…。到设置里选一个不同的「证据裁判模型」…」
      const model = message.match(/「(.+?)」/)?.[1]
      return {
        title: model ? `裁判模型与写作模型同为「${model}」，审查缺乏独立性` : '裁判模型与写作模型相同，审查缺乏独立性',
        next: '请在「设置 → 偏好设置 → 证据裁判」中选择其他模型，或在报告撰写节点的「结论句裁判」中指定裁判模型',
        fix: 'canvas',
      }
    }
    case 'report_rewrite': {
      // 「裁判认为 2 句结论证据不支持（「…」「…」），已交回写作者只改这几句」
      const n = message.match(/裁判认为\s*(\d+)\s*句/)?.[1]
      return {
        title: `裁判认为${n ? ` ${n} 句` : ''}结论缺乏证据支持，已退回写作模型仅修改这几句`,
        sub: message.match(/证据(?:不支持|支持不足)（(.+)）/)?.[1],
      }
    }
    case 'report_rewrite_rejected': {
      // 「改写稿冒出 1 处原稿没有的问题（「…」），没有采用，保留原稿和原来的判定」
      const n = message.match(/(?:冒出|出现了?|新增了?)\s*(\d+)\s*处/)?.[1]
      return {
        title: `改写稿新增了${n ? ` ${n} 处` : ''}原稿没有的问题，未予采用`,
        sub: message.match(/问题（(.+)）/)?.[1],
        next: '已保留原稿及原有判定',
      }
    }
    case 'repair_invented': {
      // 「第 1 次修复作废：修复时出现了原文没有的值：total_count=0」
      const values = message.match(/原文中?(?:没有|不存在)的值[：:]\s*(.+)$/)?.[1]?.trim()
      const what = values ? `（${values}）` : ''
      return {
        title: clip(`修复已作废：出现了原文中不存在的值${what}`),
        sub: `结果中包含原文不存在的值${what}，已作废`,
        next: '修复只能调整格式，不能补充数据。请先检查上游节点为何未获取到数据（常见原因是未绑定查询工具）',
        fix: 'canvas',
      }
    }
    default:
      return null
  }
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
 * 「协作团队用完 N 轮仍未完成：<理由>。一次都没被派到的成员：A、B。…」里的几样东西。
 * 判失败的报错和降档的那条日志是同一个开头；产出里的 never_dispatched 进事件时被
 * 缩成了「[1 项]」，成员名只能从这句话里认。canvas/NodeCard 也用它（更早的后端写「还未完成」，一并认）。
 * 这几个锚点是后端 multi.py 有意保留的，改后端那句话时要同步这里
 */
export function exhaustedOf(text: string): Pick<TeamVerdict, 'rounds' | 'reason' | 'never'> | null {
  const m = text.match(/用完\s*(\d+)\s*轮(?:仍|还)?未完成[：:]\s*([\s\S]*?)(?:。一次都没被派到的成员|。先看成员|。按降档交付|$)/)
  if (!m) return null
  const never = text.match(/一次都没被派到的成员[：:]\s*([^。]+)/)?.[1]
    .split('、').map((s) => s.trim()).filter(Boolean) ?? []
  const reason = m[2].trim()
  return { rounds: Number(m[1]), ...(reason ? { reason } : {}), never }
}

/** 这一次执行收了尾：审批早已答过（或者驳回后失败），不再等那条重放 */
function settledExec(team: TeamRun): TeamRun {
  if (!(team as TeamRunEx).paused) return team
  const { paused: _, ...rest } = team as TeamRunEx
  return rest
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
export function reduceTeam(prev: TeamRun | undefined, event: RunEvent): TeamRunEx | null {
  const d = (event.data ?? {}) as Record<string, any>
  const nodeId = event.node_id
  if (!nodeId) return null
  const verdictOf = (team: TeamRun) => (team as TeamRunEx).verdict
  const withVerdict = (team: TeamRun, patch: TeamVerdict): TeamRunEx =>
    ({ ...team, verdict: { ...verdictOf(team), ...patch } })

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
    case 'node.started': {
      // 新的一次执行（循环体又一轮、接着跑、服务重启后再跑）：上一次的泳道和结局不能挂着。
      // 调大轮数接着跑成功了，结局还写「用完 4 轮仍未完成」；循环里第二轮的 round 又从 0 数，
      // 会并进第一轮同号的那一列。规则和 trace.ts 一样，只有等过审批之后的那次重放算同一次——
      // 接着跑时后端也标 resumed:true，所以不能只看 resumed
      if (!prev) return null
      if ((prev as TeamRunEx).paused) {
        const { paused: _, ...same } = prev as TeamRunEx
        return same
      }
      return { members: [], rounds: [], savedMs: 0, finished: false }
    }

    case 'human.requested':
      return prev && !(prev as TeamRunEx).paused ? { ...prev, paused: true } : null

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
      // 失败的成员（异常，或者把工具调用写成了文字）：格子画成失败，而不是一个写着
      // 失败原因的「完成」格子
      const ended: Partial<TeamMemberEx> = d.failed
        ? { ms, status: 'failed', result: preview || undefined, error: String(d.error ?? '') || undefined }
        : { ms, status: 'done', result: preview || undefined }
      const idx = cur.members.findIndex((x) => x.agent === name && x.status === 'running')
      const members: TeamMember[] = idx >= 0
        ? cur.members.map((x, i) => (i === idx ? { ...x, ...ended } : x))
        : [...cur.members, { agent: name, instruction: '', ms, status: 'done', ...ended }]
      // 这一轮实际花的是最慢那个，各人之和减去它就是省下的
      return withRound(prev, round, {
        members,
        wallMs: Math.max(...members.map((x) => x.ms)),
        sumMs: members.reduce((acc, x) => acc + x.ms, 0),
      })
    }

    case 'log': {
      // 降档交付的那一条：轮数用完、调度者始终没说完成，按降档把成员原话交出去
      if (d.code === 'team_exhausted') {
        const team = prev ?? { members: [], rounds: [], savedMs: 0, finished: false }
        return withVerdict({ ...team, finished: true }, { outcome: 'degraded', ...exhaustedOf(String(d.message ?? '')) })
      }
      // 带 round 的 info 日志是调度决策，不是排查日志
      if (String(d.level ?? 'info') !== 'info' || d.round == null) return null
      const team = prev ?? { members: [], rounds: [], savedMs: 0, finished: false }
      const structured = Array.isArray(d.agents)
      const text = String(d.message ?? '')
      const legacy = text.match(/^调度\s*→\s*([^（(]+)[（(](.*)[）)]\s*$/)
      const done = structured ? !!d.done : legacy?.[1]?.trim() === 'FINISH'
      const reason = structured ? String(d.reason ?? '') : (legacy?.[2]?.trim() ?? '')
      if (done) return { ...team, finished: true }
      // 最后那次判定的 round 是「派过的轮数」，不是新的一轮：记进一列的话矩阵会多出一列空的
      if (d.closing) return team
      return reason ? withRound(team, num(d.round) ?? 0, { reason }) : team
    }

    case 'agent.route.start': {
      // 普通的一轮等 route.end 带着派给谁再记；只有收尾判定要先记一笔「在判定」，
      // 不然那几秒泳道上什么都不说，看着像卡住了
      if (!d.closing) return null
      return withVerdict(prev ?? { members: [], rounds: [], savedMs: 0, finished: false }, { closing: true })
    }

    case 'agent.route.end': {
      // 调度者的结构化决策。后端同一轮还会再发一条带 round 的 log，两条说的是
      // 同一件事，按哪条来结果都一样；只认 log 的话，哪天 log 不发了理由就丢了
      const team = prev ?? { members: [], rounds: [], savedMs: 0, finished: false }
      const reason = String(d.reason ?? '')
      if (d.closing) {
        // 轮数用完后补的那一次判定：只下结论、不派活，不是第 N+1 轮
        const judged = withVerdict(team, { closing: true, done: !!d.done, ...(reason ? { reason } : {}) })
        return d.done ? { ...judged, finished: true } : judged
      }
      if (d.done) return { ...team, finished: true }
      return reason ? withRound(team, num(d.round) ?? 0, { reason }) : team
    }

    case 'node.finished': {
      // 降档交付：产出里带 exhausted。老后端没有 team_exhausted 那条日志时也认得出
      const p = d.preview
      if (!prev) return null
      const settled = settledExec(prev)
      if (!p || typeof p !== 'object' || !p.exhausted) return settled === prev ? null : settled
      const known = verdictOf(prev)
      // 产出里没有轮数（后端只给 exhausted / exhausted_reason / never_dispatched）：
      // 先认日志原话里的「用完 N 轮」，再退到这一次实际派过几轮
      const rounds = num(p.rounds) ?? known?.rounds ?? (prev.rounds.length || undefined)
      return withVerdict({ ...settled, finished: true }, {
        outcome: 'degraded',
        ...(rounds != null ? { rounds } : {}),
        ...(!known?.reason && p.exhausted_reason ? { reason: String(p.exhausted_reason) } : {}),
      })
    }

    case 'node.failed': {
      // 判失败（on_exhausted 默认）：报错首句就是「协作团队用完 N 轮仍未完成：…」
      if (!prev) return null
      const settled = settledExec(prev)
      const exhausted = exhaustedOf(String(d.error ?? ''))
      if (!exhausted) return settled === prev ? null : settled
      return withVerdict({ ...settled, finished: true }, { outcome: 'failed', ...exhausted })
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
export function decodeRun(events: RunEvent[], final?: RunFinal, opts?: DecodeOptions): Step[] {
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
  const teams = new Map<string, TeamRunEx>()
  /** node_id → 校验节点最近一次修复。被作废的说明（repair_invented）折进这一行 */
  const repairs = new Map<string, Step>()
  /**
   * node_id → 门控拦下、还没见到下文的调用。下文是 human.requested（agent、工具节点：交给人批），
   * 或者 code 为 tool_needs_approval 的 log（协作团队：成员停不下来，这次不执行）
   */
  const escalated = new Map<string, { step: Step; tool: string; reason: string }[]>()
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
   * 老后端的 agent 不发 llm.end，一次模型调用做没做完只能看它后面来了什么：同一节点接着
   * 调了工具、开始了下一次调用、节点收了尾，前一次就是做完了。不收的话它们一直挂着，
   * 运行被取消或失败时终态清扫把一整列「思考并作答」都写成已取消——而它们后面明明跟着
   * 真执行过的工具。结束时刻取那条事件的时刻；清扫只剩最后那一次真在进行中的
   */
  const thought = new WeakSet<Step>()
  const closeLlm = (nodeId: string | undefined, status: StepStatus, at?: number) => {
    const step = pendingLlm.get(nodeId ?? '_')
    if (!step) return
    step.status = status
    if (step.ms == null && at != null && step.startedAt != null) {
      step.ms = Math.max(0, Math.round(at - step.startedAt))
      step.meta = dur(step.ms)
    }
    pendingLlm.delete(nodeId ?? '_')
  }

  /**
   * 把决定折进那条审批行，而不是另起一行——"问了什么 → 怎么定的"是一件事。
   *
   * 谁定的照事件里的 actor 写。以前一律写"你驳回了"，而多人使用时批的可能是
   * 别人；没署名（actor 为 null、老数据没有这个字段）就只说结果，不猜是谁。
   */
  const verdict = (approved: unknown, actor: unknown): string => {
    const who = typeof actor === 'string' && actor.trim() ? actor.trim() : ''
    const what = approved === false ? '驳回' : '批准'
    return who ? `${who} 已${what}` : `已${what}`
  }
  const closeInterrupt = (key: string, approved: unknown, note: string, actor?: unknown, always = false): boolean => {
    const step = openInterrupts.get(key)
    if (!step) return false
    step.status = 'done'
    step.level = undefined
    step.title = `${step.title} → ${verdict(approved, actor)}${always ? ALWAYS_NOTE : ''}`
    if (note) step.detail = [step.detail, `备注：${note}`].filter(Boolean).join('\n')
    openInterrupts.delete(key)
    return true
  }

  /** 上一条就是恢复事件——用来认出紧随其后的那条重复的"继续运行" */
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
        // 说两遍"继续运行"会让人以为恢复了两次。
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
          title: resumed ? '继续运行' : num(d.nodes) != null ? `开始运行（${d.nodes} 个节点）` : '开始运行',
          ...(num(d.nodes) != null && !resumed ? { total: d.nodes } : {}),
          // 「从「X」接着跑，保留了前面 3 个节点」——从哪接、谁点的，续跑才对得上账
          ...(resumed && (d.message || who)
            ? { sub: [String(d.message ?? ''), who ? `由 ${who} 发起` : ''].filter(Boolean).join(' · ') }
            : {}),
        })
        break
      }

      case 'node.started': {
        trackTeam(event)
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
            // 上一次的降档说明、跳过原因说的是上一次：循环第 2 轮正常收尾了，行上还写着
            // 「用完 2 轮仍未完成，按降档交付」
            existing.sub = undefined
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
          || nodeId || '此步骤'
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
        closeLlm(nodeId, 'done', at)
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
        trackTeam(event)
        // 协作团队降档交付：节点确实跑完了，但交出去的是成员最后的原话，不是一个安静的勾。
        // 轮数跟泳道的结局同一个来源（先 trackTeam）：产出里不带，要从日志原话、派过的轮数里认
        if (step && d.preview && typeof d.preview === 'object' && d.preview.exhausted) {
          step.level = 'warn'
          const rounds = teamVerdictOf(teams.get(nodeId ?? ''))?.rounds
          step.sub = `${rounds != null ? `已用完 ${rounds} 轮` : '已用完轮数'}仍未完成，按降档交付`
        }
        break
      }

      case 'node.failed': {
        // 节点失败时还在进行中的那次调用就是出错的那次，跟节点一起算失败，不留给清扫写成已取消
        closeLlm(nodeId, 'failed', at)
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
                 title: String(d.error ?? '此步骤失败'), nodeId, raw }, nodeId)
        }
        trackTeam(event)
        break
      }

      case 'llm.start': {
        // 一出现就成行：模型想的那几十秒里得有一行在走，而不是等 end 才冒出来。
        // 团队成员和调度者的调用没有 start（它们的 end 只用来记账），不会走到这
        closeLlm(nodeId, 'done', at)
        // agent 开了 cite_fields：循环结束后多一次结构化抽取，按出处把字段交出来。它不是在「作答」。
        // 报告撰写节点请裁判模型判断结论句（purpose judge）同理，另起名字
        const extract = d.purpose === 'cite_fields'
        const judging = d.purpose === 'judge'
        const step: Step = {
          id: `llm-${seq}`, seq, kind: 'llm',
          title: extract ? EXTRACT_TITLE : judging ? judgeTitle(d.units) : '思考并作答',
          status: 'running', nodeId, startedAt: at,
          ...(extract ? { code: 'cite_fields' } : judging ? { code: 'judge' } : {}),
          ...(d.model ? { detail: `模型：${d.model}` } : {}),
        }
        pendingLlm.set(nodeId ?? '_', step)
        push(step, nodeId)
        break
      }

      case 'llm.end': {
        if (d.purpose === 'repair') {
          // 校验节点让模型修格式：一次真实的模型调用（前面没有 llm.start）。以前它不成行，
          // 修复编出来的值被作废时，时间线上连「修过」这件事都看不到
          const ms = num(d.duration_ms)
          const step: Step = {
            id: `rp-${seq}`, seq, kind: 'llm', code: 'repair', nodeId, status: 'done',
            title: '让模型修复格式', ms, meta: dur(ms),
            ...(d.model ? { detail: `模型：${d.model}` } : {}),
          }
          repairs.set(nodeId ?? '_', step)
          push(step, nodeId)
          break
        }
        const step = pendingLlm.get(nodeId ?? '_')
        if (!step) break
        step.status = 'done'
        // 不给 token 数和美元——那是账单视角，不是"它干了什么"。
        // 用量在运行面板底栏、运行详情的用量区单独看。
        step.ms = num(d.duration_ms)
        step.meta = dur(step.ms)
        if (d.model && !step.detail) step.detail = `模型：${d.model}`
        if (d.purpose === 'cite_fields' && d.error) {
          // 抽取没跑成：agent 的结论还在，字段记空值、接着发警告——节点不失败，这一行是提醒。
          // 状态记成做完（琥珀色）而不是 failed：failed 会把这一行和「执行」那一栏的标头都画成红的
          step.level = 'warn'
          step.title = `${EXTRACT_TITLE}失败`
          step.detail = [String(d.error), step.detail].filter(Boolean).join('\n')
        }
        if (d.purpose === 'judge' && d.error) {
          // 裁判没跑成（超时、调用失败）：这一批记为未裁判、报告照常交——同样是提醒，不是失败
          step.level = 'warn'
          step.title = JUDGE_FAILED_TITLE
          step.detail = [String(d.error), step.detail].filter(Boolean).join('\n')
        }
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
        // 一次调用只在收尾时落一条完整思考：进行中的那次已经带着一条，说明它早做完了
        const open = pendingLlm.get(nodeId ?? '_')
        if (open && thought.has(open)) closeLlm(nodeId, 'done', at)
        const siblings = nodeId ? nodeSteps.get(nodeId)?.children : undefined
        const host = pendingLlm.get(nodeId ?? '_')
          ?? [...(siblings ?? [])].reverse().find((s) => s.kind === 'llm' && !s.sub)
        if (host) {
          thought.add(host)
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
        closeLlm(nodeId, 'done', at)
        const step = toolStep(seq, String(d.tool ?? ''), d.args ?? {})
        step.nodeId = nodeId
        step.startedAt = at
        // 引擎到点会放弃等待：进行中的秒表越过它时，界面要说「已超出上限」，而不是一直走
        const limit = num(d.timeout_s)
        if (limit != null && limit > 0) step.limitS = limit
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
          if (d.timed_out) {
            // 到点被放弃的：数据库那边可能还在跑，这里只是不等了。上限写出来，才知道
            // 该缩小查询范围还是去调时限
            const limit = step.limitS ?? num(Number(preview.match(/超过\s*(\d+(?:\.\d+)?)\s*(?:s|秒)/)?.[1]))
            const why = limit != null ? `超过 ${formatNumber(limit)} 秒上限，已停止等待` : '超过时限，已停止等待'
            step.sub = d.agent ? `${why} · ${d.agent} 调用` : why
          }
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

      case 'tool.gated': {
        // 放行是安静的一行（接着就是正常的工具调用）；拦下的要人看见，并说清交给了谁。
        // 要调什么是那次模型调用答出来的，问门控时它已经做完了（同 tool.start）
        closeLlm(nodeId, 'done', at)
        const tool = typeof d.tool === 'string' ? d.tool : ''
        const reason = typeof d.reason === 'string' ? d.reason.trim() : ''
        const allow = d.verdict === 'allow'
        const full = gateTitle(tool, allow ? 'allow' : 'escalate', reason)
        const step: Step = {
          id: `gt-${seq}`, seq, kind: 'note', nodeId, status: 'done', code: 'tool_gated',
          level: allow ? 'info' : 'warn',
          title: clip(full, 120),
          ...(full.length > 120 ? { detail: reason } : {}),
          ...(d.agent ? { sub: `${d.agent} 调用` } : {}),
          meta: [typeof d.model === 'string' && d.model ? `门控 ${d.model}` : '', dur(num(d.duration_ms)) ?? '']
            .filter(Boolean).join(' · ') || undefined,
        }
        push(step, nodeId)
        if (!allow) {
          const key = nodeId ?? '_'
          escalated.set(key, [...(escalated.get(key) ?? []), { step, tool, reason }])
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
            : num(d.exit_code) != null ? `退出码 ${d.exit_code}` : '沙箱未返回退出码')
          if (!step.detail) step.detail = step.result
          pendingTools.delete(`sandbox-${nodeId ?? seq}`)
        }
        break
      }

      case 'edge.taken': {
        const key = String(d.branch ?? '')
        const ran = num(d.iteration)
        const loop = d.mode === 'foreach' || d.mode === 'while' || ran != null
        const name = (nodeId && opts?.exitLabelOf?.(nodeId, key))
          || (loop ? LOOP_EXIT[key] : key === 'default' ? '其他' : undefined) || key
        push({
          id: `br-${seq}`, seq, kind: 'branch', nodeId, status: 'done',
          title: `转入「${name}」分支`
            + (ran == null ? '' : key === 'done' ? loopDoneNote(ran, num(d.total)) : `（第 ${ran + 1} 轮）`),
          detail: d.reason ? String(d.reason) : undefined,
        }, nodeId)
        break
      }

      case 'human.requested':
      case 'run.interrupted': {
        // 同一次中断发三条事件：human.requested、run.interrupted，恢复后
        // 节点重放又来一条 human.requested（LangGraph 的重放语义，不是 bug）。
        // 三条都指向同一次"等你确认"，界面上只该有一条。
        trackTeam(event)
        // 门控拦下的那次调用交到了人手里：「交给人工审批」说的是实话，不会再等协作团队那条 log
        escalated.delete(nodeId ?? '_')
        const payload = d.payload ?? d
        const key = String(payload.node_id ?? nodeId ?? '_')
        if (openInterrupts.has(key)) {       // 同一次中断的后续事件
          if (type === 'run.interrupted') closeLifecycles(out, 'done')
          break
        }
        // agent 要调的工具等审批：要调什么是那次模型调用答出来的，它已经做完了
        closeLlm(nodeId, 'done', at)
        const step: Step = {
          id: `hm-${seq}`, seq, kind: 'human', nodeId, status: 'waiting', level: 'warn',
          startedAt: at,
          title: String(payload.title || d.title || '待审批'),
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
        // 这一段执行到此停下等人："开始运行"不能再转圈。恢复后会另起一行"继续运行"
        if (type === 'run.interrupted') closeLifecycles(out, 'done')
        break
      }

      case 'human.resolved': {
        // 人工节点把答复包在 response 里；agent / 工具节点的工具审批是平铺的 {tool, approved, note, always}。
        // 以前只读 response，工具审批被驳回也写成「放行了」
        const r = d.response && typeof d.response === 'object' ? d.response : d
        const approved = r.approved
        const note = String(r.note ?? '')
        // 审批卡上点的是「始终允许」：批了这次，这个工具之后改由门控把关
        const always = r.always === true || d.always === true
        const actor = d.actor !== undefined ? d.actor : resumedActor
        if (closeInterrupt(String(nodeId ?? '_'), approved, note, actor, always)) break
        // 没有对应的待决审批（历史事件不全、或者审批发生在别处）——
        // 还是要把决定说出来，只是没地方折进去
        push({
          id: `hr-${seq}`, seq, kind: 'human', nodeId, status: 'done',
          title: `${verdict(approved, actor)}${always ? ALWAYS_NOTE : ''}`,
          detail: note || undefined,
        }, nodeId)
        break
      }

      case 'issuance': {
        const tier = String(d.tier ?? '')
        const label = issuanceLabel(tier)
        const gaps: string[] = []
        if (d.missing_required?.length) gaps.push(`缺少必需指标：${d.missing_required.join('、')}`)
        if (d.missing_expected?.length) gaps.push(`缺少期望指标：${d.missing_expected.join('、')}`)
        if (num(d.unmatched)) gaps.push(`${d.unmatched} 个数字无法追溯到指标集`)
        // 校验本身没跑全（叙述渲染为空、指标集为空）也会降档。不写出来的话，
        // 时间线上就是一个光秃秃的「降档出具」，看起来和"全部通过"只差一个字
        const notRun: string[] = Array.isArray(d.gaps) ? d.gaps.map(String) : []
        if (notRun.length) gaps.push(`校验不完整：${notRun.join('；')}`)
        const checked = [
          num(d.metrics_checked) != null ? `核对 ${d.metrics_checked} 个指标` : '',
          num(d.matched_numbers) != null ? `可追溯 ${d.matched_numbers} 个数字` : '',
        ].filter(Boolean).join(' · ')
        out.push({
          id: `is-${seq}`, seq, kind: 'issuance', status: 'done', ...(tier ? { tier } : {}),
          level: tier === 'formal' ? 'info' : tier === 'withheld' ? 'error' : 'warn',
          title: clip(gaps.length ? `${label}：${gaps.join('；')}` : label, 120),
          // 核对了几个指标、回指了几个数字放进展开区：横幅上有同样的数，窄栏的行尾放不下
          ...(gaps.length || checked ? { detail: [...gaps, checked].filter(Boolean).join('\n') } : {}),
        })
        break
      }

      case 'report.checked': {
        // 报告撰写节点核对完自己写的报告：几个数字有出处、哪些地方没有证据。说法和出具
        // 横幅那一行同一种（evidenceTally）。载荷缺字段（别的版本的后端）就只说核对过
        // 前半句只数数字（无证据 = 总数 − 有出处，算得平），不是数字的解析不了的引用另起一句
        const counts = statsTally(d.stats && typeof d.stats === 'object' ? d.stats : null)
        const none = counts ? counts.none + counts.other : 0
        const violations: any[] = Array.isArray(d.violations) ? d.violations : []
        const repairs = num(d.repairs) ?? 0
        const tally = [counts ? evidenceTally(counts.cited, counts.total, counts.other) : '', judgeTally(d.judge)]
          .filter(Boolean).join(' · ')
        const listed = violations.slice(0, 8)
          .map((v) => `· ${String(v?.message ?? v?.text ?? v?.code ?? '')}`).filter((x) => x.length > 2)
        const more = violations.length > listed.length ? [`…另有 ${violations.length - listed.length} 处`] : []
        // on_violation=fail 且重写后仍不过：节点接着就失败，这一步不能画成完成。老后端没有 failed 字段，照旧
        const failed = d.failed === true
        push({
          id: `rc-${seq}`, seq, kind: 'issuance', nodeId, status: failed ? 'failed' : 'done', code: 'report_checked',
          level: failed ? 'error' : none || violations.length ? 'warn' : 'info',
          title: tally ? `核对报告：${tally}` : '核对报告',
          ...(listed.length ? { detail: [...listed, ...more].join('\n') } : {}),
          ...(repairs ? { meta: `重写 ${repairs} 次` } : {}),
          ...(typeof d.doc_artifact === 'string' && d.doc_artifact ? { artifact: d.doc_artifact } : {}),
        }, nodeId)
        break
      }

      case 'evidence.judged': {
        // 探索运行跑完、封存之后，有人点开结论句请模型判断：判定追加在封存之后，不改封存的报告
        const verdicts = d.verdicts && typeof d.verdicts === 'object' ? Object.values(d.verdicts as Record<string, any>) : []
        const judged = verdicts.filter((v) => isJudged(v)).length
        const hit: string[] = Array.isArray(d.limits_hit) ? d.limits_hit : []
        push({
          id: `ej-${seq}`, seq, kind: 'note', nodeId, status: 'done', code: 'evidence_judged',
          level: hit.length || (Array.isArray(d.gaps) && d.gaps.length) ? 'warn' : 'info',
          title: `封存后按需裁判了 ${formatNumber(judged)} 句结论${hit.length ? '，部分因达到上限未裁判' : ''}`,
          sub: '封存后追加 · 模型判断，非确定',
          // 裁判模型和写作模型相同、模型不在价格表里：和证据面板「模型的解释」同样的两句提醒
          ...(d.model ? { detail: [`模型：${d.model}`, ...(d.same_model === true ? [JUDGE_TEXT.sameModel] : []),
            ...(d.priced === false ? [JUDGE_TEXT.noPrice] : []),
            ...(Array.isArray(d.gaps) ? d.gaps.map(String) : [])].join('\n') } : {}),
        }, nodeId)
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
          // 轮数用完后补的那一次只判定、不派活。它的 round 是「派过几轮」，写成「第 N+1 轮」
          // 读起来像还有新的一轮
          title: d.closing ? '轮数用完 · 调度者正在做最后判定…' : `第 ${round + 1} 轮：调度者正在规划下一步…`,
        }
        pendingRoutes.set(`${nodeId}|${round}`, step)
        push(step, nodeId)
        trackTeam(event)
        break
      }

      case 'agent.route.end': {
        const round = num(d.round) ?? 0
        const key = `${nodeId}|${round}`
        routed.add(key)
        const agents: string[] = Array.isArray(d.agents) ? d.agents.map(String) : []
        const ms = num(d.duration_ms)
        const reason = String(d.reason ?? '')
        // 最后那次判定说「没完成」时不能写「结束协作」：那读起来像团队正常收尾了
        const title = d.closing
          ? d.done ? '轮数用完 · 调度者判定：已完成'
            : clip(`轮数用完 · 调度者判定：未完成${reason ? `（${reason}）` : ''}`)
          : d.done || !agents.length ? `第 ${round + 1} 轮：结束协作`
          : agents.length > 1 ? `第 ${round + 1} 轮：交给 ${agents.join('、')}（并行）`
          : `第 ${round + 1} 轮：交给 ${agents[0]}`
        const verdictLevel = d.closing && !d.done ? { level: 'warn' as const } : {}
        const step = pendingRoutes.get(key)
        if (step) {
          Object.assign(step, { status: 'done', title, ms, meta: dur(ms), ...verdictLevel,
                                ...(reason ? { detail: reason } : {}) })
          pendingRoutes.delete(key)
        } else {
          push({ id: `re-${seq}`, seq, kind: 'branch', nodeId, status: 'done', title, ms,
                 meta: dur(ms), ...verdictLevel, ...(reason ? { detail: reason } : {}) }, nodeId)
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
        // 成员这一步失败了（异常，或者把工具调用写成了文字）：原因写在副标题上，不用展开才看得到。
        // 标记那种的原话是一整句排查说明，窄栏里截断后只剩半句，这里换成和报错行同一个说法
        const why = String(d.error ?? '')
        const failure: Partial<Step> = d.failed
          ? { status: 'failed', level: 'error',
              sub: /没有真正调用工具|未实际调用工具|工具调用的原始标记/.test(why) ? '模型未实际调用工具，此步骤未查询到任何数据'
                : clip(why || '此步骤失败', 80) }
          : { status: 'done' }
        if (step) {
          Object.assign(step, failure)
          step.ms = ms
          step.meta = dur(ms)
          step.result = preview || undefined
          pendingAgents.delete(key)
        } else {
          push({
            id: `ae-${seq}`, seq, kind: 'note', nodeId, status: 'done',
            title: `${d.agent} 回复`, ms, meta: dur(ms),
            detail: preview || undefined, ...failure,
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
        if (d.code === 'tool_needs_approval') {
          // 门控刚拦下的调用落在协作团队里：成员停不下来等人，这次调用不执行。折进门控那一行，
          // 改口说「协作团队里不执行」，而不是先说交给人工、再冒出一行不执行。成员并行时几条
          // 可能交错，按日志带的 tool 认（老后端没有这个字段，按原话里的工具名认）；认不出就是最近那一条
          const list = escalated.get(nodeId ?? '_')
          if (list?.length) {
            const named = typeof d.tool === 'string' && d.tool ? d.tool : ''
            let i = list.length - 1
            for (let j = list.length - 1; j >= 0; j -= 1) {
              if (list[j].tool && (named ? list[j].tool === named : message.includes(list[j].tool))) { i = j; break }
            }
            const [gate] = list.splice(i, 1)
            const full = gateTitle(gate.tool, 'team', gate.reason)
            gate.step.title = clip(full, 120)
            gate.step.detail = [full.length > 120 ? gate.reason : '', message].filter(Boolean).join('\n') || undefined
            break
          }
        }
        // 其余 info 是给排查用的，不进主流程——但 warn/error 用户必须看到
        if (level === 'info' || !message) break
        const code = typeof d.code === 'string' && d.code ? d.code : undefined
        const said = explainLog(code, message)
        if (code === 'repair_invented') {
          // 修复编出了原文没有的值：折进刚才那一行「让模型修复格式」，一件事一行
          const repair = repairs.get(nodeId ?? '_')
          if (repair && said) {
            Object.assign(repair, { level: 'warn', sub: said.sub, next: said.next, fix: said.fix,
                                    detail: [repair.detail, message].filter(Boolean).join('\n') })
            repairs.delete(nodeId ?? '_')
            break
          }
        }
        if (code === 'team_exhausted') trackTeam(event)
        push({
          id: `lg-${seq}`, seq, kind: 'note', nodeId, status: 'done',
          level: level === 'error' ? 'error' : 'warn',
          ...(code ? { code } : {}),
          ...(said
            ? { ...said, detail: message }
            : { title: clip(message, 120), ...(message.length > 120 ? { detail: message } : {}) }),
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

      case 'run.cancelled': {
        // 用户主动停下的，不是出错：收成中性的"已取消"，不画红色的失败。谁停的、放弃等
        // 审批时一并关了几条，写在副标题上——多人用时「怎么没了」得有个交代
        settle(endingOf(event, awaiting)!)
        // 说法和画布胶囊的那一句一致：「张工 放弃了这次运行，1 条待审批一并关闭」
        const who = typeof d.actor === 'string' && d.actor.trim() ? d.actor.trim() : ''
        const said = String(d.message ?? '').trim()
        const sub = said ? `${who ? `${who} ` : ''}${said}` : who ? `${who} 取消了本次运行` : ''
        out.push({ id: `rc-${seq}`, seq, kind: 'lifecycle', status: 'cancelled', title: '已取消',
                   ...(sub ? { sub } : {}) })
        break
      }

      case 'run.finished': {
        // 开头那条"开始运行"要收尾，否则跑完了还挂着一个转圈的图标。
        // 注意是全部而不是第一条：审批恢复过的运行有"开始运行"+"继续运行"
        // 两条，只收第一条会让"继续运行"永远转圈——明明已经完成了。
        settle(endingOf(event, awaiting)!)
        const timing = d.timing ?? {}
        const active = num(timing.active_ms) ?? num(d.duration_ms)
        const wait = num(timing.wait_ms)
        out.push({
          id: `rd-${seq}`, seq, kind: 'lifecycle', status: 'done',
          title: '完成', ms: active,
          // 等人的时间单独说：审批停了三分钟的运行，"执行 1.2 s"和"用了 3 分钟"
          // 都对，混成一个数就哪个都不对了
          meta: [dur(active), wait ? `等待审批 ${formatDuration(wait)}` : ''].filter(Boolean).join(' · ')
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
            title: content ? `已写入记忆：${content.slice(0, 40)}${content.length > 40 ? '…' : ''}`
                           : '已写入一条记忆',
            detail: content || undefined,
          }, nodeId)
          break
        }
        if (action === 'clear') {
          push({
            id: `mc-${seq}`, seq, kind: 'note', nodeId, status: 'done', level: 'warn',
            title: `已清空记忆域「${String(d.scope ?? '')}」中的 ${count} 条记忆`,
          }, nodeId)
          break
        }
        push({
          id: `mr-${seq}`, seq, kind: 'schema', nodeId,
          status: count ? 'done' : 'failed',
          level: count ? undefined : 'warn',
          title: count ? `召回 ${count} 条相关记忆` : '未召回相关记忆',
        }, nodeId)
        break
      }

      case 'merge.end': {
        // 合并查询：几次查询的结果在库外按键合并。和查询同一类（kind query）：detail 是合并 SQL，result 是预览，
        // 工件是合并结果的查询快照。输入列在 merge 里，展开区单列一块；警告的原文由紧跟着的 log 各占一行
        const inputs: MergeStepInput[] = (Array.isArray(d.inputs) ? d.inputs : [])
          .filter((i: any) => i && typeof i === 'object' && typeof i.alias === 'string')
          .map((i: any) => ({
            alias: String(i.alias),
            ...(typeof i.node_id === 'string' ? { nodeId: i.node_id } : {}),
            ...(typeof i.label === 'string' && i.label ? { label: i.label } : {}),
            ...(num(i.rows) != null ? { rows: num(i.rows) } : {}),
            ...(typeof i.source === 'string' && i.source ? { source: i.source } : {}),
          }))
        const warnings = Array.isArray(d.warnings) ? d.warnings.filter((w: any) => w && w.message).length : 0
        const rows = num(d.rows)
        const ms = num(d.duration_ms)
        const preview = Array.isArray(d.preview_rows) ? d.preview_rows : []
        push({
          id: `mg-${seq}`, seq, kind: 'query', nodeId, status: 'done', code: 'merge',
          ...(warnings ? { level: 'warn' as const, sub: MERGE_TEXT.warnings(warnings) } : {}),
          title: MERGE_TEXT.stepTitle(inputs.map((i) => i.alias)),
          detail: typeof d.sql === 'string' && d.sql ? d.sql : undefined,
          result: Array.isArray(d.columns)
            ? JSON.stringify({ columns: d.columns, rows: preview, truncated: rows != null && rows > preview.length })
            : undefined,
          meta: [rows != null ? MERGE_TEXT.rows(rows) : '', dur(ms) ?? ''].filter(Boolean).join(' · ') || undefined,
          ms,
          ...(typeof d.query_artifact === 'string' && d.query_artifact ? { artifact: d.query_artifact } : {}),
          merge: { inputs, warnings, ...(rows != null ? { rows } : {}), truncated: !!d.truncated },
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
            ? `在「${collection}」中检索到 ${count} 段`
            : `在「${collection}」中未检索到内容`,
          detail: String(d.query ?? '') || undefined,
          meta: [
            top != null ? `最高分 ${top}` : '',
            // 退回关键词是"少了一半能力"，不标出来用户只会觉得最近搜得不准
            d.degraded ? '已回退为关键词检索' : '',
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
          title: '未识别的事件',
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
  // 出了状况的行，副标题和下一步说的是为什么、怎么办：说法不同就是两件事，并成一行只剩第一件
  const same = (a: Step, b: Step) => a.kind === b.kind && a.title === b.title
    && a.level === b.level && a.status === b.status && a.nodeId === b.nodeId
    && (!a.level || (a.sub === b.sub && a.next === b.next))
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

/**
 * 助手生成工作流的阶段文案。全站唯一一份：画布顶部的锁定条、输入框下的进度行、
 * 助手栏的轮次状态、助手时间线的阶段行都从这里取，不要再各写一份
 */
export const COPILOT_PHASE_TEXT: Record<string, string> = {
  connecting: '正在连接模型',
  planning: '正在理解需求、规划步骤',
  building: '正在搭建工作流',
  wiring: '正在连接数据流',
  finalizing: '正在排版和校验',
  // 以「正在」开头：收尾时去掉「正在」就是完成态（按自查结果修正）
  repairing: '正在按自查结果修正',
}

/**
 * 出具档位的说明（悬停提示、横幅副标题）。全站唯一一份：运行记录的档位芯片和
 * 出具横幅都从这里取。档位名本身在 lib/terms 的 ISSUANCE_LABEL
 */
export const ISSUANCE_HINT: Record<'formal' | 'degraded' | 'withheld', string> = {
  formal: '指标齐全，叙述中的数字均可追溯到口径卡',
  degraded: '存在缺口，请对照声明使用结论',
  withheld: '必需指标缺失或数字无法追溯，本期结论不予采用',
}

/** 自查最多修几轮。和后端 copilot._SELF_CHECK_ROUNDS 一致，check 事件只带当前第几轮 */
export const SELF_CHECK_ROUNDS = 2

export interface CopilotIssue {
  message: string
  /** 问题落在哪个节点上（能认出来时）。有它才能给「定位」 */
  nodeId?: string
  /** 那个节点叫什么：操作流里加过、改过它就认得出，再退到画布上的名字。认不出就不给 */
  label?: string
  /** 落在节点的哪一项配置上（condition、tools、agents[1].tools）：「定位」据此聚焦到检查器里那个输入框 */
  field?: string
  /** 机读代号：datasource_out_of_scope、tools_dropped…… */
  code?: string
}

/**
 * 自查问题两种形状：新后端给对象 {level, node_id, edge_id, message, field, code?}；
 * 老会话里存的是拼好的一行字「「lp」循环条件写错了」，节点 id 包在开头的「」里，
 * 认出来才能在卡片上配「定位」；认不出就只是一行字。
 */
function parseIssue(v: unknown, labels?: (id: string) => string | undefined): CopilotIssue {
  const named = (x: CopilotIssue): CopilotIssue => {
    const label = x.nodeId ? labels?.(x.nodeId) : undefined
    return label && label !== x.nodeId ? { ...x, label } : x
  }
  if (v && typeof v === 'object') {
    const o = v as Record<string, unknown>
    return named({
      message: String(o.message ?? ''),
      ...(o.node_id ? { nodeId: String(o.node_id) } : {}),
      ...(typeof o.field === 'string' && o.field ? { field: o.field } : {}),
      ...(typeof o.code === 'string' && o.code ? { code: o.code } : {}),
    })
  }
  const text = String(v ?? '')
  const m = text.match(/^「([^」]+)」\s*(.*)$/)
  return named(m ? { nodeId: m[1], message: m[2] || text } : { message: text })
}

/** 一条自查问题写成一行：带节点名（认得出时），认不出才退到 id */
export const issueLine = (x: CopilotIssue): string =>
  x.nodeId ? `「${x.label ?? x.nodeId}」${x.message}` : x.message

/**
 * 节点 id → 名字。先认这一轮操作流里加过、改过的（问数据页没有画布，只有这一份），
 * 再退到调用方给的画布查询
 */
function labelsOf(ops: CopilotOp[], fallback?: (id: string) => string | undefined): (id: string) => string | undefined {
  const map = new Map<string, string>()
  for (const op of ops) {
    if (op.op === 'add_node' && op.node?.id) {
      const label = op.node.data?.label || op.node.label
      if (label) map.set(String(op.node.id), String(label))
    } else if (op.op === 'update_node' && op.id && op.label) {
      map.set(String(op.id), String(op.label))
    }
  }
  return (id) => map.get(id) ?? fallback?.(id)
}

/** context 操作里一个数据源的那一项（服务端 copilot_context.DatasourceContext.op） */
export interface CopilotContextSource {
  source: string
  /** 模型看得到全部字段的表（全名）。只给了表名的不在这里 */
  tables: string[]
  /** model：按需求挑的；all：库不大，全部表都带字段；fallback：没能挑出来，只给了表名 */
  selectedBy: 'model' | 'all' | 'fallback'
  /** 这个库一共几张表 */
  total?: number
  /** 没能挑出来的原因 */
  reason?: string
}

/** 挑表失败时的说法：模型这一轮只看到表名，字段要它自己去查 */
export const CONTEXT_FALLBACK_TEXT = '未能按需求挑选，已提供全部表名'

function contextSources(op: CopilotOp): CopilotContextSource[] {
  const list: unknown[] = Array.isArray(op.sources) ? op.sources : []
  return list
    .filter((x): x is Record<string, any> => !!x && typeof x === 'object' && typeof (x as any).source === 'string')
    .map((x) => {
      const total = num(x.total)
      return {
        source: x.source,
        tables: Array.isArray(x.tables) ? x.tables.map(String) : [],
        selectedBy: x.selected_by === 'model' || x.selected_by === 'fallback' ? x.selected_by : 'all',
        ...(total != null ? { total } : {}),
        ...(typeof x.reason === 'string' && x.reason ? { reason: x.reason } : {}),
      }
    })
}

/** 一个库在展开区里的那一行：怎么来的 + 表名 */
function contextLine(g: CopilotContextSource): string {
  const names = g.tables.join('、')
  const of = g.total != null ? `共 ${g.total} 张表` : ''
  switch (g.selectedBy) {
    case 'all':
      return `「${g.source}」全部 ${g.tables.length} 张表：${names}`
    case 'model':
      return g.tables.length
        ? `「${g.source}」按需求${g.total != null ? `从 ${g.total} 张表中` : ''}挑出 ${g.tables.length} 张：${names}`
        : `「${g.source}」${of ? `${of}，` : ''}未挑中与需求相关的表，已提供全部表名`
    default:
      return `「${g.source}」${of ? `${of}，` : ''}${CONTEXT_FALLBACK_TEXT}`
        + (g.tables.length ? `；现有工作流用到的 ${g.tables.length} 张表附带全部字段：${names}` : '')
  }
}

/**
 * 助手这一轮参考了哪些表（context 操作）→ 过程里的一行。
 *
 * 大库放不下全部字段，服务端先按需求挑表，挑中的表给模型全部字段。这件事要看得见：生成的 SQL 用错了表，
 * 先得知道模型当时看到的是哪几张。标题只说几张（「参考了 8 张表」），展开按数据源分组列表名；挑表失败时
 * 标题直说只给了表名，原因放在展开区。认不出的形状返回 null：不出这一行，也不报错。
 */
export function copilotContext(op: CopilotOp): { title: string; sub?: string; detail: string; ms?: number } | null {
  const groups = contextSources(op)
  if (!groups.length) return null
  const count = groups.reduce((n, g) => n + g.tables.length, 0)
  const failed = groups.filter((g) => g.selectedBy === 'fallback')
  // 有表带着字段（小库，或者现有工作流用到的表）时标题仍说几张，没挑成的事放副标题
  const sub = count && failed.length
    ? failed.length === groups.length ? CONTEXT_FALLBACK_TEXT
      : `「${failed.map((g) => g.source).join('」「')}」${CONTEXT_FALLBACK_TEXT}`
    : undefined
  const reasons = [...new Set(failed.map((g) => g.reason).filter(Boolean))]
  // 挑过表才有 elapsed_ms：挑表是生成之前多出来的一次模型调用，没挑成（比如超时）也要让人看到等了多久
  const ms = num(op.elapsed_ms)
  return {
    title: count ? `参考了 ${count} 张表` : CONTEXT_FALLBACK_TEXT,
    ...(sub ? { sub } : {}),
    detail: [...groups.map(contextLine), ...reasons.map((r) => `原因：${r}`)].join('\n'),
    ...(ms != null && ms >= 10 ? { ms } : {}),
  }
}

/** 用了限定范围之外的数据源：自查交回模型改也改不掉时，人要知道是范围的事，不是图写错了 */
const OUT_OF_SCOPE_NEXT = '本轮限定了查询的数据源，但工作流使用了范围之外的数据库。请取消限定后重新提问，或将所需数据库加入查询范围'

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
 *
 * unchanged：收到了 final，但画布一处都没变（store 比对出来的，操作流里看不出）。
 * 那一行不能再说「流程搭好了」。
 */
export function decodeCopilot(ops: CopilotOp[], opts?: {
  context?: 'canvas' | 'chat'
  unchanged?: boolean
  /** 画布上的节点名。自查问题、「去掉了」这类只带 id 的行据此写成名字 */
  labelOf?: (id: string) => string | undefined
}): Step[] {
  const canvas = opts?.context === 'canvas'
  const labels = labelsOf(ops, opts?.labelOf)
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
          ? `${COPILOT_PHASE_TEXT.repairing}（第 ${repairRound}/${SELF_CHECK_ROUNDS} 轮）`
          : COPILOT_PHASE_TEXT[String(op.phase)] ?? '正在处理'
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
      case 'context': {
        // 这一轮参考了哪些表：标题说几张，展开看按数据源分组的表名
        const ctx = copilotContext(op)
        if (!ctx) break
        add({ id: `cx-${i}`, seq: i, kind: 'schema', status: 'done', code: 'copilot_context',
              title: ctx.title, detail: ctx.detail,
              ...(ctx.sub ? { sub: ctx.sub } : {}),
              ...(ctx.ms != null ? { ms: ctx.ms, meta: formatDuration(ctx.ms) } : {}) })
        break
      }
      case 'catalog_patch': {
        // 目录修改提案：过程里记一行（哪张表、几项，展开看改前改后），保存、忽略在轮次里的卡片上做（catalogPatchesOf）
        const patch = catalogPatchOf(op, String(i - 1))
        if (!patch) break
        add({ id: `cpt-${i}`, seq: i, kind: 'schema', status: 'done', code: 'catalog_patch',
              title: CATALOG_PATCH_TEXT.step(patch.tableLabel ?? patch.table, patch.changes.length),
              sub: CATALOG_PATCH_TEXT.stepSub,
              detail: patch.changes.map((c) => `${patchWhere(c)}：${patchValueText(c.path, c.before)} → ${patchValueText(c.path, c.after)}`)
                .join('\n') })
        break
      }
      case 'plan':
        add({ id: `cp-${i}`, seq: i, kind: 'note', status: 'done',
              title: clip(String(op.summary ?? '已完成规划'), 120) })
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
        // 只改配置的 update_node 常常不带 label：名字从操作流里认，再向画布要，都没有才写 id
        add({ id: `cu-${i}`, seq: i, kind: 'note', status: 'done', nodeId: String(op.id ?? ''),
              title: clip(`调整了「${op.label || labels(String(op.id)) || op.id}」`) })
        break
      case 'remove_node':
        add({ id: `cr-${i}`, seq: i, kind: 'note', status: 'done',
              title: clip(`移除了「${labels(String(op.id)) ?? op.id}」`) })
        break
      // 连线不单独成行：用户关心有哪些步骤，不关心箭头
      case 'add_edge':
      case 'remove_edge':
        break
      case 'done':
        // 心跳那条"正在理解需求…"要收尾，否则生成完了它还在转圈
        settleAll('done')
        add({ id: `cd-${i}`, seq: i, kind: 'lifecycle', status: 'done',
              title: `工作流已搭建完成，新增 ${nodeCount} 个步骤`,
              detail: String(op.explanation ?? '') || undefined })
        break
      case 'check': {
        // 服务端自查：用运行时同一套规则过一遍，有问题交回模型改。
        // 这件事得看得见——否则多出来的那十几秒像是卡住了，改了什么也无从知道
        const issues = Array.isArray(op.issues) ? op.issues.map((x: unknown) => parseIssue(x, labels)) : []
        const lines = issues.map(issueLine)
        const outOfScope = issues.some((x) => x.code === 'datasource_out_of_scope')
        if (op.status === 'repairing') {
          repairRound = num(op.round) ?? repairRound + 1
          add({ id: `ck-${i}`, seq: i, kind: 'note', status: 'done', level: 'warn',
                title: `自查发现 ${issues.length} 处问题，已交回模型修正（第 ${repairRound}/${SELF_CHECK_ROUNDS} 轮）`,
                detail: lines.join('\n') || undefined })
        } else if (op.status === 'passed') {
          settleAll('done')
          // 规则上通过了，但有节点的工具被悄悄去掉了（模型改提示词时漏写 tools）：这不是
          // 一句安静的「自查通过」能交代的，图照样能跑，只是跑起来什么都查不到
          const dropped = droppedOf(op)
          add({ id: `ck-${i}`, seq: i, kind: 'note', status: 'done',
                title: `${op.repaired ? '自查通过：问题已修正' : '自查通过'}${dropped.length
                  ? `，但有 ${dropped.length} 处工具被移除` : ''}`,
                ...(dropped.length ? { level: 'warn' as const, code: 'tools_dropped',
                                       detail: dropped.map((x) => x.message).join('\n') } : {}) })
        } else {
          // 修正那一轮本身是跑完了的；没修好由下面这行红字说，不把阶段行也画红
          settleAll('done')
          add({ id: `ck-${i}`, seq: i, kind: 'error', level: 'error', status: 'failed',
                title: op.status === 'failed'
                  ? canvas
                    ? `自查后仍有 ${issues.length} 处问题未能自动修正`
                    : `自查后仍有 ${issues.length} 处问题，未自动运行`
                  : String(op.message ?? '自查未能完成'),
                detail: [...lines, ...droppedOf(op).map((x) => x.message)].join('\n') || undefined,
                ...(outOfScope ? { next: OUT_OF_SCOPE_NEXT, code: 'datasource_out_of_scope' } : {}),
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
          built.title = opts?.unchanged ? `已检查，无需改动（工作流共 ${total} 个步骤）`
            : nodeCount && nodeCount < total
              ? `工作流已搭建完成，新增 ${nodeCount} 个步骤，共 ${total} 个步骤`
              : `工作流已搭建完成，共 ${total} 个步骤`
        }
        settleAll('done')
        // 模型写了不存在的节点类型：那一步被跳过了。不说出来的话"共 N 步"是
        // 一句不完整的真话，用户要到运行结果不对才发现少了一步
        for (const t of skippedTypes(op)) {
          add({ id: `cm-${i}-${t}`, seq: i, kind: 'note', status: 'done', level: 'warn',
                title: clip(`已跳过一个步骤：节点类型「${t}」不存在`, 80) })
        }
        // 工具绑定变化单独成一行：改图回执以前只说「修改 1」，模型改提示词时漏写 tools、
        // 把查库的工具整个抹掉，要到运行结果不对才发现
        const changes = toolChangesOf(op)
        if (changes.length) {
          const lost = changes.filter((c) => c.removed.length)
          const one = changes.length === 1 ? changes[0] : null
          add({
            id: `cg-${i}`, seq: i, kind: 'tool', status: 'done', code: 'tool_changes',
            ...(lost.length ? { level: 'warn' as const } : {}),
            title: clip(one
              ? one.removed.length && !one.after.length ? `${changeWho(one)}的工具已全部移除`
                : one.removed.length ? `${changeWho(one)}移除了 ${one.removed.length} 个工具`
                : `${changeWho(one)}的工具绑定已变更`
              : `${changes.length} 处工具绑定已变更${lost.length ? `，其中 ${lost.length} 处移除了工具` : ''}`, 80),
            detail: changes.map((c) => `${changeWho(c)}：${c.before.join('、') || '（空）'} → ${c.after.join('、') || '（空）'}`)
              .join('\n'),
          })
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

/** final.tool_changes：后端比对新旧图得出的工具绑定变化（NI-1） */
function toolChangesOf(op: CopilotOp): ToolChange[] {
  const list: any[] = Array.isArray(op.tool_changes) ? op.tool_changes : []
  const names = (v: unknown): string[] => (Array.isArray(v) ? v.map(String) : [])
  return list.filter((c) => c && typeof c === 'object').map((c) => ({
    node_id: String(c.node_id ?? ''),
    field: String(c.field ?? 'tools'),
    label: String(c.label ?? c.node_id ?? ''),
    member: c.member == null ? null : String(c.member),
    before: names(c.before), after: names(c.after), added: names(c.added), removed: names(c.removed),
  }))
}

/** 「数据查询」/「销售分析团队」的成员「取数员」 */
const changeWho = (c: ToolChange): string =>
  c.member ? `「${c.label}」的成员「${c.member}」` : `「${c.label}」`

/** 自查带回的「工具被去掉了」警告（code=tools_dropped）。check 里有就用它，否则看 final.issues */
function droppedOf(op: CopilotOp): CopilotIssue[] {
  const list: any[] = [...(Array.isArray(op.warnings) ? op.warnings : []),
                       ...(op.op === 'final' && Array.isArray(op.issues) ? op.issues : [])]
  return list.filter((x) => x && typeof x === 'object' && x.code === 'tools_dropped').map((x) => parseIssue(x))
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
  /** 工具绑定变化（final.tool_changes）。removed 非空的那几处要提醒 */
  toolChanges: ToolChange[]
  /** 自查的「工具被去掉了」警告（tools_dropped）：这一轮的要求里没提到去掉 */
  dropped: CopilotIssue[]
}

export function copilotOutcome(
  ops: CopilotOp[], running = false, labelOf?: (id: string) => string | undefined,
): CopilotOutcome {
  const res: CopilotOutcome = {
    kind: running ? 'running' : 'empty', skipped: [], added: 0, updated: 0, removed: 0,
    toolChanges: [], dropped: [],
  }
  const labels = labelsOf(ops, labelOf)
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
        const issues = Array.isArray(op.issues) ? op.issues.map((x: unknown) => parseIssue(x, labels)) : []
        if (op.status === 'repairing') {
          res.repairing = num(op.round) ?? (res.repairing ?? 0) + 1
        } else {
          res.check = {
            status: op.status === 'passed' ? 'passed' : op.status === 'failed' ? 'failed' : 'error',
            repaired: num(op.repaired) ?? 0,
            issues,
          }
          res.dropped = droppedOf(op)
        }
        break
      }
      case 'final':
        res.total = op.graph?.nodes?.length
        res.skipped = skippedTypes(op)
        res.toolChanges = toolChangesOf(op)
        // 同一批警告 final.issues 里也有一份：自查已经带回来了就不再算一遍
        if (!res.dropped.length) res.dropped = droppedOf(op)
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
