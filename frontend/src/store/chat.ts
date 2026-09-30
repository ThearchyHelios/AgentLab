import { create } from 'zustand'
import { ApiError, api, streamCopilot, streamRun } from '../api/client'
import type { CopilotOp, FixKind, RunFinal } from '../run/decode'
import { decodeRun } from '../run/decode'
import { errorMessage, humanizeError, isNetworkError } from '../lib/errors'
import { guardReview, hasEvidence } from '../lib/evidence'
import { explainRunError, explainStartError } from '../lib/explain'
import { parseServerTime } from '../lib/format'
import { useCatalog } from './catalog'
import { useConversations } from './conversations'
import type {
  ConversationTurn, GraphSpec, ReviewResult, Run, RunEvent, ValidationIssue,
} from '../types'

/**
 * 对话式入口的状态。
 *
 * 和 studio store 分开：画布那套状态（选中节点、脏标记、校验问题）在这里
 * 一个都用不上，混进去只会让两边互相牵制——对话页跑一次图就把人家画布上
 * 没保存的东西冲掉，那是灾难。
 *
 * 一轮对话 = 一次「建图 → 跑图 → 出结果」。图对用户是隐藏的（想看可以展开），
 * 但它仍然是真实存在的工作流，跑完能存成模板、能在画布里继续改。
 *
 * 这里**只存原始流**（Copilot 的操作、运行的事件），不在 store 里翻译。
 * 翻译归 run/decode.ts，全站一份；store 里再译一遍的结果是同一次运行在
 * 对话页和画布上讲出两个不同的故事，而用户没法判断哪个是真的。
 *
 * 轮次按会话分桶存放。之前是单个 turns 数组，于是"对话"只存在于内存里：
 * 刷新页面就全没了，而每轮又各自从零建图、互不知情。现在每一轮同时写进
 * 后端的 conversation_turns，刷新能恢复，下一轮也拿得到上一轮的上下文。
 *
 * **忙不忙按会话算，由轮次的相位推出来**，store 上不再有全局的 busy / cancel。
 * 全局只有一份的时候，会话 A 在跑、切到 B，B 的按钮就成了「停止」，点下去
 * 关掉的是 A 的事件流——A 那一轮永远转圈，后端却还在跑、还在计费。
 */

export type Phase =
  | 'idle'
  | 'planning'    // Copilot 在想
  | 'building'    // 图在长出来
  | 'running'     // 图在跑
  | 'waiting'     // 停在人工审批
  | 'ready'       // 图建好了，等用户决定跑不跑
  | 'checking'    // 从库里恢复的、上次停在半路的轮次，正在向后端核对它现在的状态
  | 'done'
  | 'error'
  | 'cancelled'   // 用户点了停止。不是出错：不画红，也不给「接着跑」
  | 'suspended'   // 服务重启打断了，断点还在，可以接着跑
  /**
   * 核对撞上后端出错、超时：不知道它现在是在跑、跑完了还是失败了。只给「重新核对」——
   * 给「重试」会在旧运行可能还活着的时候另起一次
   */
  | 'unknown'

/** 占着这个会话的相位：这时再问一句、或者删掉这个会话，都会撞上正在跑的东西 */
const BUSY_PHASES: ReadonlySet<Phase> = new Set(['planning', 'building', 'running'])

/** 这一轮出了什么事。title 是一句人话，detail 是原文，给「技术细节」 */
export interface Failure {
  title: string
  reason?: string
  hint?: string
  detail?: string
  /**
   * 翻成人话之前的那句原话。运行的失败把它交给流里的报错块按 lib/explain 再讲一遍；
   * 别的交拆好的 title / reason——把翻好的「操作超时：查询超时…」再翻一遍，标题和
   * 原因就成了两遍「操作超时」
   */
  source?: string
  /** 原样接着跑也过不去（缺输入、人驳回、没绑工具…），不给「接着跑」。见 lib/explain */
  continuable?: false
  /** 流程本身没问题，是发起那一下没到后端：原图再发起一次就行，不必让 Copilot 重新搭 */
  rerun?: true
  /** 限定的数据源都不在了（删了、停用了）。带着原范围重试只会再被拒一次 */
  scopeGone?: true
  /**
   * 该去哪儿改（lib/explain 的 fix）。流里的报错块据此放直达入口（去模型接入、去工具库），
   * 不必再把原话交过去让它重讲一遍
   */
  fix?: FixKind | 'rerun'
  /** fix 的站内地址（lib/explain 的 fixTo）：直接打开要改的那一项 */
  fixTo?: string
  /** 先去 fix 那里改好，再接着跑（lib/explain 的 fixFirst）：补救按钮里去改的那一个排在「接着跑」前面 */
  fixFirst?: true
}

/** 限定这一问只查哪些数据源。名字跟着存：库删了之后，这一轮查的是谁还得说得出来 */
export interface ScopeSource {
  id: string
  name: string
}

/**
 * 用户点「重试这一轮」之前的那一次。
 *
 * 用户主动重试不能把上一次抹掉：那一次的答案可能只是有缺口、并非全错，
 * 新的一次也未必更好，两份要能对照。
 */
export interface Attempt {
  n: number
  outcome: 'failed' | 'cancelled' | 'suspended' | 'unusable' | 'partial' | 'done'
  summary: string
  detail?: string
  runId?: string | null
  /** 那一次交出来的答案（截断到 ATTEMPT_ANSWER_CAP） */
  answer?: string
  at: number
}

/**
 * 刷新之后还得在的可信度信息，存在 conversation_turns.meta。
 *
 * 出具档位、运行类别、「这一条没查库」以前都只活在内存里，刷新就没了——而历史
 * 会话恰恰是事后复盘、转发结论时最常看的地方。老轮次把它挂在 review.meta 下，
 * 后端读出来时已经统一放进 meta，这里只认 meta。
 */
export interface TurnMeta {
  v: 1
  /** 当前这次尝试的运行。重试建图失败时是 null——run_id 列清不掉，得以这里为准 */
  runId?: string | null
  runClass?: string
  runStatus?: string
  /** output._issuance 原样。只存档位的话出具横幅会退化成「核对 0 个指标」 */
  issuance?: Record<string, any>
  noQuery?: boolean
  outcome?: 'cancelled' | 'suspended'
  failure?: Failure
  attempts?: Attempt[]
  /** 这一轮从开问到交付的墙钟毫秒 */
  ms?: number
  /** 查了几次库 */
  queries?: number
  /**
   * 答案没取全。不记下来的话，刷新之后只能靠「恰好 2000 字」去猜：多个成果键拼起来的
   * 答案比 2000 长，猜不中，「刷新后会自动补全」的承诺就落空了
   */
  clipped?: 'partial' | 'lost'
  /** 这一问限定了只查哪些数据源 */
  scope?: ScopeSource[]
  /**
   * 成果里有逐段证据（报告撰写节点的文档）。库里只存答案的文字，证据的标注和字段名在
   * 运行的成果里：有这个记号的轮次恢复时要回运行那里取一次
   */
  evidence?: boolean
  /**
   * 这份 meta 写下的时刻（本机时钟）。断在建图阶段的轮次按它和 created_at 里晚的那个算
   * 有多久没动静：后端按 updated_at 算，而重试一轮昨天的问题时 created_at 是昨天的
   */
  at?: number
}

export interface ChatTurn {
  id: string
  /** 后端 conversation_turns 里的 id。回填靠它 */
  serverId: string | null
  question: string
  phase: Phase
  /** 一句话进度，给人看"现在到哪了" */
  status: string
  /** 模型的思考（支持 thinking 的模型才有） */
  thinking: string
  /** Copilot 的原始操作流。thinking delta 在写入时已合并 */
  ops: CopilotOp[]
  /** 跑图的原始事件。不含 llm.token / llm.thinking.delta：它们不落库，界面也不读 */
  events: RunEvent[]
  /** Copilot 建出来的图 */
  graph: GraphSpec | null
  /** Copilot 对这张图的说明 */
  explanation: string
  run: Run | null
  /** 最终成果 */
  output: Record<string, any> | null
  /** 一行错误文案（兼容老数据和流式头部）。结构化的在 failure 里 */
  error: string
  failure?: Failure | null
  startedAt: number
  /** 交付、失败、取消或中断的时刻。计时器停在这里 */
  endedAt?: number
  /** 已收到的最大事件 seq。重新接流时要带上，否则后端把历史整条重推 */
  lastSeq: number
  /** 这一轮自己的取消句柄。放 turn 上而不是 store 上：store 单值会被
   *  下一轮 ask 覆盖，上一轮的 WebSocket 就再也关不掉了 */
  cancel: (() => void) | null
  /** 事件之外知道的结局（stream.end、GET 运行查到的状态），交给 decodeRun 收尾 */
  final?: RunFinal
  /** 后端这次运行的状态。决定给不给「接着跑」：只有 failed 和服务重启挂起的才续得上 */
  runStatus?: string
  /** 这一轮没有查库，答案是根据前几轮说的。界面上必须标出来 */
  noQuery?: boolean
  /** 图建好了但按模型的意思没有自动跑，等用户点「跑一下」 */
  pendingRun?: boolean
  /** 从库里恢复的历史轮次。它没有事件流，步骤要点开才去取 */
  restored?: boolean
  /** 历史轮次的事件：idle 没取过，loading 在取，shown 展开着，hidden 取过但收起了 */
  steps?: 'idle' | 'loading' | 'shown' | 'hidden'
  /** 向后端核对过运行（补回完整答案、档位、真实状态）。pending 表示在路上 */
  hydrated?: 'pending' | 'done'
  /** 跑完之后的复核结论。null = 没复核过，和「复核过、没问题」不是一回事 */
  review?: ReviewResult | null
  /** 复核原样（含 verdict='ok'），落库时和 meta 一起写回 review 字段 */
  reviewRaw?: ReviewResult | null
  /** 被复核重写之前的答案。改写是有损的，原件得留着让用户能对照 */
  rawOutput?: Record<string, any> | null
  /** 这一轮自动重建过几次图。上限 1——第二次还不行多半是问题本身没说清，
   *  该让用户介入，而不是替他一遍遍烧钱 */
  attempt?: number
  /** 用户点「重试」之前的各次尝试 */
  attempts?: Attempt[]
  /**
   * 答案不全：partial = 这次只拿到了事件里那份截断的，完整版要回源；
   * lost = 旧版本保存时就被切在 2000 字，对应的运行也删了，补不回来
   */
  clipped?: 'partial' | 'lost'
  /** 刷新前落库的查库次数和耗时（事件没取回来时头部小标签靠它） */
  meta?: TurnMeta | null
  /**
   * 模型此刻在出字：只记写了多少、思考想到哪一句，原文不存也不落库。答案要等
   * 复核完才摆出来，这里给的是「它没卡住」的证据，不是提前看答案的口子
   */
  writing?: { chars: number; thought: string } | null
  /** 这一问只查这几个数据源。重试、重新问都沿用它 */
  scope?: ScopeSource[]
}

export type LoadState = 'loading' | 'ready' | 'error'

interface ChatState {
  /** 按会话分桶。切走再切回来要能看到原来的步骤，所以内存里有就不回源 */
  byConversation: Record<string, ChatTurn[]>
  /**
   * 每个会话的历史取回到哪了。没有键 = 还没取过。
   *
   * 「加载中」「加载失败」「真的是空的」必须分开：以前三者一个样子，加载失败时
   * 页面显示「问点什么」，用户一提问就在内存里建出只有新一轮的桶，历史在这次
   * 页面生命周期里再也不会回来。
   */
  loadState: Record<string, LoadState>
  loadError: Record<string, unknown>
  /**
   * 每个会话限定查哪些数据源，空数组 = 不限。键 '' 是还没建出会话的首屏：开口那一下
   * 建出会话后由调用方带过去。按会话记：换一个会话问别的库，回来还是原来的范围
   */
  scopes: Record<string, ScopeSource[]>
  setScope: (key: string, scope: ScopeSource[]) => void

  turnsOf: (conversationId: string | null) => ChatTurn[]
  /** force：已经取回来过也再取一次（重连后、点重试时） */
  load: (conversationId: string, opts?: { force?: boolean }) => Promise<void>
  /**
   * 发出去了返回 true。会话还在加载或加载失败、或者正忙时不发。
   * scope 不给就用这个会话眼下的范围
   */
  ask: (conversationId: string, question: string, opts?: { scope?: ScopeSource[] }) => boolean
  /** 只停这个会话自己正在跑的那一轮 */
  stop: (conversationId: string) => void
  /** 审批完成后重新接上运行 */
  reattach: (conversationId: string, turnId: string) => Promise<void>
  /** 历史轮次的「执行过程」：第一次点去取事件，之后是展开 / 收起 */
  toggleSteps: (conversationId: string, turnId: string) => Promise<void>
  /** 模型建了图但没自动跑时，用户点「跑一下」 */
  runNow: (conversationId: string, turnId: string) => void
  /** 跑挂了或被服务重启打断：从断点接着跑。成功接上返回 true，被拒时抛出后端的原因 */
  continueTurn: (conversationId: string, turnId: string) => Promise<boolean>
  /**
   * 用户主动重试这一轮：上一次折叠留档，新的一次接在同一轮里。
   * maxSteps：把图里 agent 的步数上限调到这个值，原图重跑（「放宽步数重跑」）；
   * rerun：原图原样重跑（取消了、中断了的那种，不需要改）；
   * scope：换一个数据源范围重来（限定的库不在了），不给就沿用这一轮的
   */
  retryTurn: (
    conversationId: string, turnId: string,
    opts?: { maxSteps?: number; rerun?: boolean; scope?: ScopeSource[] },
  ) => void
  /** 向后端核对一轮历史：真实状态、完整答案、档位。只做一次 */
  hydrate: (conversationId: string, turnId: string) => Promise<void>
  /** 上次核对撞上后端出错（unknown）的轮次，再核对一次 */
  recheck: (conversationId: string, turnId: string) => Promise<void>
  /** 会话被删了，把内存里那一桶也丢掉 */
  forget: (conversationId: string) => void
}

let seq = 0
const newId = () => `t${Date.now().toString(36)}${seq++}`

const PHASE_TEXT: Record<Phase, string> = {
  idle: '',
  planning: '正在理解需求…',
  building: '正在生成工作流…',
  running: '正在运行…',
  waiting: '等待审批',
  ready: '工作流已生成，未自动运行',
  checking: '正在核对本轮的状态…',
  done: '已完成',
  error: '失败',
  cancelled: '已取消',
  suspended: '服务重启，本轮已中断',
  unknown: '无法确认本轮的当前状态',
}

const EMPTY: ChatTurn[] = []

/** 落库成果的上限。只是防跑飞，不是显示上限 */
const ANSWER_CAP = 20_000

/** 等复核的上限。超了就先把原答案交出去，说明后补不了就算了 */
const REVIEW_TIMEOUT_MS = 25_000

/**
 * 旧版本落库时答案被切在这个长度：run.finished 事件里的 output 按字典每个值
 * 2000 字截断，而以前落库和显示用的都是事件里那份。恰好这么长的历史答案要回源核对
 */
const LEGACY_CLIP = 2000

/**
 * 是不是恰好切在 LEGACY_CLIP。后端按码点切，JS 的 length 数的是 UTF-16：答案里
 * 有一个 emoji 就多出一个，按 length 比就认不出来了
 */
function atClip(s: string): boolean {
  if (s.length < LEGACY_CLIP || s.length > LEGACY_CLIP * 2) return false
  let n = 0
  for (const _ of s) n++
  return n === LEGACY_CLIP
}

/**
 * 还停在 running 的轮次、又没有运行：断在建图阶段了。建图跟着页面那条流走，页面一关
 * 就停；这么久还没动静就当它断了（和后端 conversations.BUILD_STALE 一致）。「动静」
 * 按最后一次落库算，见 TurnMeta.at
 */
const BUILD_STALE_MS = 15 * 60_000

/** 留档的旧答案存多少。它是用来对照的，不是用来读全文的——全文在运行记录里 */
const ATTEMPT_ANSWER_CAP = 4000

/** 事件流里的增量：不落库，decodeRun 一开头就丢掉；存进 events 只会让每条都重解码整个会话 */
const EPHEMERAL = new Set(['llm.token', 'llm.thinking.delta'])

/** 出字计数多久写回一次。数字跳得比人读得快没有意义；关掉动效的人一秒一次 */
const PEN_MS = 250
const PEN_MS_REDUCED = 1000

/** 回放里只收下、不据此改相位的事件：它们说的是过去某一刻的结局 */
const REPLAY_SKIP = new Set(['run.finished', 'run.failed', 'run.cancelled', 'run.interrupted', 'log'])

type Patch = (fn: (t: ChatTurn) => Partial<ChatTurn>) => void
type Save = (body: Record<string, any>) => Promise<void>
type SetState = (fn: (s: ChatState) => Partial<ChatState> | ChatState) => void

/** 会话此刻被哪一轮占着。按相位推，不另记一份——另记的那份迟早和相位对不上 */
export function busyTurn(turns: ChatTurn[] | undefined): ChatTurn | undefined {
  if (!turns) return undefined
  for (let i = turns.length - 1; i >= 0; i--) {
    if (BUSY_PHASES.has(turns[i].phase)) return turns[i]
  }
  return undefined
}

export const isBusy = (turns: ChatTurn[] | undefined): boolean => !!busyTurn(turns)

/**
 * 这一轮的执行过程现在摆不摆出来。本次会话里跑的一直摆着；从库里恢复的要点开
 * 才取，点开后能收起——接着跑、重新接流之后事件自己来了，也照样摆出来
 */
export function stepsVisible(t: ChatTurn): boolean {
  if (!t.restored) return true
  if (t.steps === 'hidden') return false
  return t.steps === 'shown' || t.events.length > 0
}

/**
 * 成果 dict 压成一句话，存进 conversation_turns.answer。
 *
 * 它有两个去处：刷新页面后重建界面，以及下一轮拼进 Copilot 的上下文。
 * 后者决定了这里不能只存个 "[object Object]"——模型要靠它知道上一轮
 * 答了什么，才接得住"再按月份拆一下"。
 */
function answerText(output: Record<string, any> | null | undefined): string {
  if (!output) return ''
  const parts: string[] = []
  for (const [key, value] of Object.entries(output)) {
    if (key.startsWith('_') || value == null || value === '') continue
    parts.push(typeof value === 'string' ? value : JSON.stringify(value))
  }
  // 喂给下一轮的那份另有更狠的截断（HISTORY_ANSWER_CHARS），两回事。
  // 这里的上限只是防跑飞的护栏，真切到了必须说出来
  const text = parts.join('\n')
  return text.length > ANSWER_CAP
    ? text.slice(0, ANSWER_CAP) + `\n\n…（内容过长，已截断，完整内容请查看运行记录）`
    : text
}

/** 事件里那份 output 看起来是不是被切过：老后端不带 output_truncated，只能看长度 */
function looksClipped(output: Record<string, any> | null | undefined): boolean {
  if (!output) return false
  return Object.values(output).some((v) => typeof v === 'string' && atClip(v))
}

/** 出具信息存进 meta 前瘦一下身：无法回指的数字可能有上百个，横幅只列得下一行 */
function slimIssuance(iss: any): Record<string, any> | undefined {
  if (!iss || typeof iss !== 'object' || !iss.tier) return undefined
  const unmatched = Array.isArray(iss.unmatched_numbers) ? iss.unmatched_numbers.slice(0, 20) : undefined
  return { ...iss, ...(unmatched ? { unmatched_numbers: unmatched } : {}) }
}

/** 查库次数：db_query 的每一次调用 */
function countQueries(events: RunEvent[]): number {
  return events.filter((e) => e.type === 'tool.start' && String(e.data?.tool ?? '').startsWith('db_query')).length
}

export function metaOf(t: ConversationTurn): TurnMeta | null {
  return t.meta && typeof t.meta === 'object' ? (t.meta as TurnMeta) : null
}

/** review 字段里真正的复核结论。只挂了 meta、没有 verdict 的不算复核过（老轮次的 meta 在里面，剥掉） */
function reviewOf(t: ConversationTurn): ReviewResult | null {
  const r = t.review as any
  if (!r || typeof r !== 'object' || !r.verdict) return null
  const { meta: _meta, ...rest } = r
  return rest as ReviewResult
}

function buildMeta(t: ChatTurn): TurnMeta {
  return {
    v: 1,
    runId: t.run?.id ?? null,
    runClass: t.run?.run_class ?? t.meta?.runClass,
    runStatus: t.runStatus,
    issuance: slimIssuance(t.output?._issuance ?? t.rawOutput?._issuance) ?? t.meta?.issuance,
    noQuery: t.noQuery || undefined,
    outcome: t.phase === 'cancelled' ? 'cancelled' : t.phase === 'suspended' ? 'suspended' : undefined,
    failure: t.failure ?? undefined,
    attempts: t.attempts?.length ? t.attempts : undefined,
    ms: t.endedAt ? t.endedAt - t.startedAt : t.meta?.ms,
    queries: t.events.length ? countQueries(t.events) : t.meta?.queries,
    clipped: t.clipped,
    scope: t.scope?.length ? t.scope : undefined,
    evidence: hasEvidence(t.output) || t.meta?.evidence || undefined,
    at: Date.now(),
  }
}

/**
 * 一行错误文案 → 结构化。后端的 detail 按约定已是人话，humanizeError 只做拆句和去类名。
 * 运行本身的失败不走这里，走 runFailure
 */
function failureOf(e: unknown, extra?: { hint?: string; detail?: string }): Failure {
  const h = humanizeError(e)
  // 原话留一份（后端的 detail）。连不上后端时原话是「Failed to fetch」，没有留的价值
  const source = h.kind === 'network' ? undefined
    : typeof e === 'string' ? e
    : e instanceof ApiError ? e.message
    : undefined
  return {
    title: h.title,
    reason: h.reason,
    // 通用的「稍等几秒会自动重试」在这一页不成立：没有谁会替这一轮重发，得人来点
    hint: extra?.hint || (h.kind === 'network' ? '服务端恢复连接后，点击「重试本轮」。' : h.action),
    detail: extra?.detail || (h.raw && h.raw !== h.title ? h.raw : undefined),
    ...(source ? { source } : {}),
  }
}

/**
 * 运行失败的说明：和记录页、画布右栏读同一份 lib/explain，同一次失败三处说法一致。
 * 它还说得出「原样接着跑有没有用」：缺输入、人驳回、工具没绑，接着跑只会再失败一次。
 * 原话跟着留：流里的报错块拿它按同一份 lib/explain 再讲一遍，才给得出「去模型接入」
 * 这类直达入口
 */
function runFailure(error: string | null | undefined, detail?: string | null): Failure {
  const x = explainRunError(error, detail)
  return {
    title: x.title,
    reason: x.reason,
    hint: x.action,
    detail: x.raw && x.raw !== x.title ? x.raw : undefined,
    ...(x.continuable ? {} : { continuable: false as const }),
    ...(x.fix ? { fix: x.fix } : {}),
    ...(x.fixTo ? { fixTo: x.fixTo } : {}),
    ...(x.fixFirst ? { fixFirst: true as const } : {}),
    ...(error ? { source: error } : {}),
  }
}

const lineOf = (f: Failure) => (f.reason ? `${f.title}：${f.reason}` : f.title)

/**
 * 发起运行那一下被拒（POST /runs 的 ApiError）的说明。绑定的工具在本机不存在（422 run_tool_missing）
 * 按 lib/explain 讲，带上直达入口（数据源工具去数据页，自定义 / MCP 工具去工具页），和画布上
 * 发起被拒（run/RunControl）同一套；这时还没有运行 id，流里的报错块不会再拿原话认一遍，
 * 所以入口得在这里就记进 failure。别的照旧按一句报错拆
 */
function startFailure(e: unknown): Failure {
  const x = explainStartError(e)
  if (!x) return failureOf(e)
  return {
    title: x.title,
    reason: x.reason,
    hint: x.action,
    detail: x.raw && x.raw !== x.title ? x.raw : undefined,
    continuable: false,
    ...(x.fix ? { fix: x.fix } : {}),
    ...(x.fixTo ? { fixTo: x.fixTo } : {}),
    ...(e instanceof ApiError ? { source: e.message } : {}),
  }
}

/** 运行记录被删了：没有断点可续，也补不回完整答案 */
const GONE: Failure = {
  title: '上次未完成',
  reason: '对应的运行记录已删除，无法从断点继续。',
  hint: '点击「重试本轮」重新开始。',
}

const SUSPENDED: Failure = {
  title: PHASE_TEXT.suspended,
  reason: '断点已保留，已查询的表和已执行的步骤都会保留。',
  hint: '点击「继续运行」从断点继续；如需整轮重来，请点击「重新运行本轮」。',
}

// -------------------------------------------------------------------------
// 落库：同一轮的写入排队发，免得「运行中」比「完成」晚到、把它盖回去
// -------------------------------------------------------------------------

/** 还没拿到 serverId 的新轮次：开轮请求在路上，写入要等它 */
const serverIds = new Map<string, Promise<string | null>>()
const saveChains = new Map<string, Promise<void>>()

function saver(conversationId: string, turnId: string): Save {
  return (body) => {
    const prev = saveChains.get(turnId) ?? Promise.resolve()
    const next = prev.then(async () => {
      const turn = findTurn(conversationId, turnId)
      const serverId = turn?.serverId ?? (await serverIds.get(turnId)) ?? null
      if (!serverId) return
      try {
        await api.conversations.patchTurn(conversationId, serverId, body)
      } catch { /* 存不下不该影响正在进行的对话 */ }
    })
    saveChains.set(turnId, next)
    return next
  }
}

function findTurn(conversationId: string, turnId: string): ChatTurn | undefined {
  return useChat.getState().byConversation[conversationId]?.find((t) => t.id === turnId)
}

/**
 * 在回收站里的会话只能翻看。从这里发起的运行左栏找不到、没人看着，却照样计费；
 * 跑着的时候又不能彻底删除。按钮已经按这个关掉了，这里再守一道
 */
const inTrash = (conversationId: string) =>
  useConversations.getState().trash.some((c) => c.id === conversationId)

/**
 * 连同 meta 和复核一起写。两者在后端都是整块替换。review 写 {} 而不是 null：null 等于
 * 「这次不改」，重试之后上一次的复核就留在库里，刷新后挂到新答案头上
 */
function persist(conversationId: string, turnId: string, body: Record<string, any> = {}) {
  const turn = findTurn(conversationId, turnId)
  if (!turn) return Promise.resolve()
  return saver(conversationId, turnId)({ ...body, meta: buildMeta(turn), review: turn.reviewRaw ?? {} })
}

/**
 * 一轮里「当前这一次」的异步流程：建图的 SSE、启动运行、事件流、复核。
 *
 * 停止、重试、重新接流都会换一个 epoch，旧流程之后到的东西一律作废。不这样的话，
 * 被停掉的 SSE 缓冲里还没派发完的 final 会把这一轮又启动起来；重试之后，上一次
 * 那条流晚到的事件会混进新的一次里。
 */
interface Flow {
  conversationId: string
  turnId: string
  patch: Patch
  save: Save
  persist: (body?: Record<string, any>) => Promise<void>
  alive: () => boolean
}

const epochs = new Map<string, number>()

function flowOf(conversationId: string, turnId: string): Flow {
  const epoch = (epochs.get(turnId) ?? 0) + 1
  epochs.set(turnId, epoch)
  const alive = () => epochs.get(turnId) === epoch
  const patch = patcher(useChat.setState as SetState, conversationId, turnId)
  const save = saver(conversationId, turnId)
  return {
    conversationId,
    turnId,
    alive,
    patch: (fn) => { if (alive()) patch(fn) },
    save: (body) => (alive() ? save(body) : Promise.resolve()),
    persist: (body) => (alive() ? persist(conversationId, turnId, body) : Promise.resolve()),
  }
}

/** 让这一轮手上所有的旧流程作废 */
const retire = (turnId: string) => epochs.set(turnId, (epochs.get(turnId) ?? 0) + 1)

// -------------------------------------------------------------------------
// 从库里恢复
// -------------------------------------------------------------------------

function restoreTurn(t: ConversationTurn): ChatTurn {
  const meta = metaOf(t)
  const review = reviewOf(t)
  const runId = meta && 'runId' in meta ? meta.runId ?? null : t.run_id ?? null
  // 老数据没有 meta：停止写的是 status=error + 「已取消」
  const outcome = meta?.outcome ?? (t.status === 'error' && t.error === '已取消' ? 'cancelled' : undefined)
  const answer = t.answer ?? ''
  // 老数据没有 meta.failure，error 列里是原始异常：有运行的按运行失败解释
  const failure = meta?.failure
    ?? (t.error && t.error !== '已取消' ? (runId ? runFailure(t.error) : failureOf(t.error)) : null)

  let phase: Phase
  let status: string
  if (t.status === 'running') {
    // 上次停在半路：页面被关掉、服务重启、或者还在等审批。有运行就去问后端它
    // 现在怎样了；没有运行说明断在建图阶段，没有断点可续
    phase = runId ? 'checking' : 'error'
    status = runId ? PHASE_TEXT.checking : PHASE_TEXT.error
  } else if (t.status === 'error') {
    phase = outcome ?? 'error'
    status = PHASE_TEXT[phase]
  } else if (t.graph && !runId && !answer) {
    phase = 'ready'   // 建好了没跑：刷新之后「跑一下」还得在
    status = PHASE_TEXT.ready
  } else {
    phase = 'done'
    status = PHASE_TEXT.done
  }

  const buildInterrupted: Failure = {
    title: '上次未完成',
    reason: '生成工作流时页面被关闭或服务重启，本轮没有可恢复的断点。',
    hint: '点击「重试本轮」重新开始。',
  }
  const output: Record<string, any> | null = answer
    ? { answer, ...(meta?.issuance ? { _issuance: meta.issuance } : {}) }
    : null
  const graphful = !!t.graph && ((t.graph as GraphSpec).nodes?.length ?? 0) > 0

  return {
    id: t.id,
    serverId: t.id,
    question: t.question,
    phase,
    status,
    thinking: '',
    ops: [],
    events: [],
    graph: (t.graph as GraphSpec) ?? null,
    explanation: t.explanation ?? '',
    // 先给个壳：run_class 从 meta 来；usage、完整成果由 hydrate 回源补
    run: runId ? ({ id: runId, run_class: meta?.runClass } as Run) : null,
    output,
    error: phase === 'error' ? (failure ? lineOf(failure) : t.status === 'running' ? buildInterrupted.title : '') : '',
    failure: phase === 'error' ? (t.status === 'running' ? buildInterrupted : failure) : phase === 'suspended' ? SUSPENDED : null,
    startedAt: Date.parse(t.created_at ?? '') || Date.now(),
    endedAt: meta?.ms != null ? (Date.parse(t.created_at ?? '') || 0) + meta.ms : undefined,
    lastSeq: 0,
    cancel: null,
    runStatus: meta?.runStatus,
    final: meta?.runStatus ? { status: meta.runStatus } : undefined,
    // 没有运行、没有图、却有答案：这一条是 reply，根据前几轮说的，没查库。
    // 这是这条路径上最硬的一条约定，老数据也要标出来
    noQuery: meta?.noQuery ?? (t.status === 'done' && !t.run_id && !t.graph && !!answer),
    pendingRun: phase === 'ready',
    restored: true,
    steps: 'idle',
    // 复核结论是答案的一部分。刷新之后只剩一个看起来很完整的答案、
    // 而"它哪里不可靠"没了，比一开始就不复核更糟
    review: review && review.verdict !== 'ok' ? review : null,
    reviewRaw: review,
    rawOutput: review?.original ? { answer: review.original } : null,
    attempts: meta?.attempts,
    // 上次记下没取全的：运行还在就等 hydrate 补，运行没了就补不回来了。
    // 老数据没记过，只能看是不是恰好切在 2000 字——运行也删了的，至少别装作完整
    clipped: meta?.clipped === 'partial' ? (runId ? 'partial' : 'lost')
      : meta?.clipped === 'lost' ? 'lost'
      : !runId && graphful && atClip(answer) && !review?.answer ? 'lost' : undefined,
    meta,
    scope: meta?.scope,
  }
}

/** 恢复出来的这一轮要不要马上向后端核对（而不是等它滚进视口） */
function needsEagerHydrate(t: ChatTurn): boolean {
  if (!t.run?.id) return false
  if (t.phase === 'checking' || t.phase === 'suspended') return true
  // 「接着跑」给不给取决于运行现在的状态；老数据没存过
  if (t.phase === 'error' && !t.runStatus) return true
  // 有逐段证据：标注在运行的成果里，库里那份答案只有文字
  if (t.phase === 'done' && t.meta?.evidence && !hasEvidence(t.output) && !t.review?.answer) return true
  // 答案没取全：运行还在就把完整版补回来
  return t.phase === 'done' && !t.review?.answer
    && (t.clipped === 'partial' || (typeof t.output?.answer === 'string' && atClip(t.output.answer)))
}


export const useChat = create<ChatState>((set, get) => ({
  byConversation: {},
  loadState: {},
  loadError: {},
  scopes: {},

  setScope: (key, scope) => set((s) => ({ scopes: { ...s.scopes, [key]: scope } })),

  turnsOf: (conversationId) =>
    (conversationId && get().byConversation[conversationId]) || EMPTY,

  forget: (conversationId) =>
    set((s) => {
      s.byConversation[conversationId]?.forEach((t) => { retire(t.id); t.cancel?.() })
      const { [conversationId]: _gone, ...rest } = s.byConversation
      const { [conversationId]: _state, ...states } = s.loadState
      return { byConversation: rest, loadState: states }
    }),

  load: async (conversationId, opts) => {
    // 取回来过就用内存里的：那一份带着这次会话跑出来的完整步骤，回源拿到的只有
    // 问题和答案。判断的是「成功取回来过」而不是「有这个桶」——加载失败时如果
    // 用户先问了一句，桶就有了，而历史从来没回来过
    const state = get().loadState[conversationId]
    if (state === 'loading' || (state === 'ready' && !opts?.force)) return
    set((s) => ({ loadState: { ...s.loadState, [conversationId]: 'loading' } }))
    try {
      const detail = await api.conversations.get(conversationId)
      const restored = detail.turns.map(restoreTurn)
      set((s) => {
        const mem = s.byConversation[conversationId] ?? []
        // 内存里已有的（这次会话里跑的、或刚才重连前就在的）优先：它们带着
        // 完整的事件和进行中的流。库里多出来的补在前面
        const known = new Set(mem.map((t) => t.serverId).filter(Boolean))
        const extra = restored.filter((t) => !known.has(t.serverId))
        // 数据源范围接着上一问的：回来追问的多半还是那个库。输入框旁写着范围，不是暗中沿用
        const lastScope = restored[restored.length - 1]?.scope
        return {
          byConversation: { ...s.byConversation, [conversationId]: mem.length ? [...extra, ...mem] : restored },
          loadState: { ...s.loadState, [conversationId]: 'ready' },
          loadError: { ...s.loadError, [conversationId]: undefined },
          ...(lastScope && !(conversationId in s.scopes) ? { scopes: { ...s.scopes, [conversationId]: lastScope } } : {}),
        }
      })
      // 内存里已有的那几轮没有并进来，下面对它们的 hydrate / persist 按 id 找不到，自然跳过
      const now = Date.now()
      detail.turns.forEach((row, i) => {
        const t = restored[i]
        if (needsEagerHydrate(t)) void get().hydrate(conversationId, t.id)
        // 断在建图阶段、早就没动静的：库里那行一直是 running，每次进来都当它还在跑，
        // Copilot 的上下文也把它当成没答完的一轮漏掉。认定断了就写回去。
        // 动静按最后一次落库算：重试昨天那一轮时 created_at 是昨天，而它正在别的页面里建图
        // 后端判「断了」按 updated_at（conversations.BUILD_STALE），有就用同一只钟；老后端
        // 没有这一列，退到 created_at 和本机记下的 meta.at 里晚的那个
        const updated = parseServerTime(row.updated_at ?? null)?.getTime()
        const at = updated ?? Math.max(parseServerTime(row.created_at ?? null)?.getTime() ?? 0, t.meta?.at ?? 0)
        if (row.status === 'running' && !t.run && t.phase === 'error' && at && now - at > BUILD_STALE_MS) {
          void persist(conversationId, t.id, { status: 'error', error: lineOf(t.failure ?? GONE) })
        }
      })
    } catch (e) {
      set((s) => ({
        loadState: { ...s.loadState, [conversationId]: 'error' },
        loadError: { ...s.loadError, [conversationId]: e },
      }))
    }
  },

  toggleSteps: async (conversationId, turnId) => {
    const turn = findTurn(conversationId, turnId)
    const runId = turn?.run?.id
    if (!turn || !runId || turn.steps === 'loading') return
    const patch = patcher(set, conversationId, turnId)
    if (stepsVisible(turn)) return patch(() => ({ steps: 'hidden' }))
    if (turn.events.length) return patch(() => ({ steps: 'shown' }))
    patch(() => ({ steps: 'loading' }))
    try {
      const events = (await api.runs.events(runId)).filter((e) => !EPHEMERAL.has(e.type))
      patch((t) => ({
        steps: 'shown',
        events: mergeEvents(t.events, events),
        lastSeq: Math.max(t.lastSeq, ...events.map((e) => e.seq ?? 0)),
      }))
    } catch {
      patch(() => ({ steps: 'idle' }))   // 取失败就还能再点一次
    }
  },

  ask: (conversationId, question, opts) => {
    const state = get().loadState[conversationId]
    // 历史还没回来就提问，新的一轮会把"这个会话"变成只有它一轮；加载失败时
    // 页面上什么都没有，更像是空会话——两种情况都先别让它发
    if (state === 'loading' || state === 'error') return false
    if (isBusy(get().byConversation[conversationId]) || inTrash(conversationId)) return false

    const id = newId()
    const scope = opts?.scope ?? get().scopes[conversationId] ?? []
    set((s) => ({
      byConversation: {
        ...s.byConversation,
        [conversationId]: [
          ...(s.byConversation[conversationId] ?? []),
          {
            id, serverId: null, question, phase: 'planning', status: PHASE_TEXT.planning,
            thinking: '', ops: [], events: [], graph: null, explanation: '',
            run: null, output: null, error: '', startedAt: Date.now(),
            lastSeq: 0, cancel: null, attempt: 0, scope: scope.length ? scope : undefined,
          },
        ],
      },
      // 没取过的会话能走到这里，只可能是刚建的空会话：它没有历史可丢
      loadState: state ? s.loadState : { ...s.loadState, [conversationId]: 'ready' },
    }))
    const flow = flowOf(conversationId, id)

    useConversations.getState().touch(conversationId, question)

    // 先开一轮，拿到 serverId 再开始建图。顺序不能反：Copilot 那边要读
    // 这次会话的历史，而"这一轮"必须已经是 running 状态才不会被算进自己的上下文
    const started = api.conversations.startTurn(conversationId, question)
      .then((turn) => {
        patcher(set, conversationId, id)(() => ({ serverId: turn.id }))
        return turn.id
      })
      .catch(() => null)
    serverIds.set(id, started)

    plan(flow, { question, instruction: question, attempt: 0, scope })
    return true
  },

  stop: (conversationId) => {
    // 只停这个会话里占着的那一轮。等审批的不算：它们是等着用户回来处理的，
    // 不是卡住的，误杀了就得重来
    const target = busyTurn(get().byConversation[conversationId])
    if (!target) return
    retire(target.id)
    target.cancel?.()
    // 上面只是把自己这头的事件流关了。运行在后端，断开订阅不会让它停：
    // 以前点了「停止」，模型和 SQL 其实还在跑、还在计费，界面上却已经显示"已取消"
    if (target.run?.id) {
      void api.runs.cancel(target.run.id).catch(() => undefined)   // 已经跑完会回 409，无妨
    }
    patcher(set, conversationId, target.id)(() => ({
      phase: 'cancelled', status: PHASE_TEXT.cancelled, cancel: null, error: '',
      failure: null, endedAt: Date.now(), writing: null,
      ...(target.run ? { runStatus: 'cancelled', final: { status: 'cancelled' } } : {}),
    }))
    void persist(conversationId, target.id, { status: 'error', error: '已取消' })
  },

  runNow: (conversationId, turnId) => {
    const turn = findTurn(conversationId, turnId)
    if (!turn?.graph || isBusy(get().byConversation[conversationId]) || inTrash(conversationId)) return
    const flow = flowOf(conversationId, turnId)
    flow.patch(() => ({
      phase: 'running', status: PHASE_TEXT.running, pendingRun: false,
      startedAt: Date.now(), endedAt: undefined,
    }))
    void launch(flow, turn.graph, turn.question)
  },

  continueTurn: async (conversationId, turnId) => {
    // 从断点接着跑。**不带图**——问数据页的用户改不了图，这里就是"用原来的
    // 配置重试失败的那一步"，对超时、限流、网络抖、服务重启这类瞬时失败有用。
    // 配置本身错了（模型 id 写错、缺必填输入）要到画布上改，那边的「接着跑」
    // 会把改过的图一起带上。
    //
    // 接上之后 run.finished 照常触发 settle，复核层自动覆盖这一次
    const turn = findTurn(conversationId, turnId)
    if (!turn?.run || isBusy(get().byConversation[conversationId]) || inTrash(conversationId)) return false
    try {
      await api.runs.continue(turn.run.id)
    } catch (e) {
      // 409 的 detail 是人话（「这次运行已取消，不能接着跑…」），交给调用方弹出来。
      // 顺手再核对一次：按钮是照着过时的状态给的，别让它留在那里被点第二次
      patcher(set, conversationId, turnId)(() => ({ hydrated: undefined }))
      void get().hydrate(conversationId, turnId)
      throw e
    }
    await get().reattach(conversationId, turnId)
    return true
  },

  retryTurn: (conversationId, turnId, opts) => {
    const turns = get().byConversation[conversationId]
    const turn = turns?.find((t) => t.id === turnId)
    if (!turn || isBusy(turns) || inTrash(conversationId)) return
    turn.cancel?.()
    // 服务重启挂起的旧运行没人会再去续它：不取消的话，它在记录里一直是「已挂起 · 可续跑」。
    // 停在审批上的不算——重试不会从那里发起
    if (turn.run?.id && turn.phase !== 'waiting' && (turn.phase === 'suspended' || turn.runStatus === 'interrupted')) {
      void api.runs.cancel(turn.run.id).catch(() => undefined)
    }
    const flow = flowOf(conversationId, turnId)
    const base = turn.graph && (turn.graph.nodes?.length ?? 0) > 0 ? turn.graph : null
    const record = attemptOf(turn)
    const reason = record.detail ? `${record.summary}（${record.detail.slice(0, 300)}）` : record.summary
    const direct = !!base && !!(opts?.maxSteps || opts?.rerun)
    const scope = opts?.scope ?? turn.scope

    // 上一次原样留档，这一次从头记：旧的事件、答案、复核留在这里只会让人分不清
    // 哪段是最终那次
    flow.patch((t) => ({
      phase: direct ? 'running' : 'planning',
      status: opts?.maxSteps ? `正在将步数上限放宽到 ${opts.maxSteps} 并重新运行…`
        : direct ? '正在重新运行本轮…' : '正在按原问题重新生成…',
      attempts: [...(t.attempts ?? []), record],
      ops: [], events: [], run: null, output: null, review: null, reviewRaw: null,
      rawOutput: null, failure: null, error: '', lastSeq: 0, final: undefined,
      runStatus: undefined, clipped: undefined, pendingRun: false, steps: undefined,
      hydrated: 'done', startedAt: Date.now(), endedAt: undefined, attempt: 0, cancel: null,
      noQuery: false, thinking: '', writing: null, scope: scope?.length ? scope : undefined,
    }))
    void flow.persist({ status: 'running', error: '', answer: '' })

    if (direct && base) {
      const graph = opts?.maxSteps ? withMaxSteps(base, opts.maxSteps) : base
      flow.patch(() => ({ graph }))
      void launch(flow, graph, turn.question)
      return
    }
    plan(flow, {
      question: turn.question, attempt: 0, baseGraph: base, scope,
      instruction: base
        ? `这个工作流上一次没有给出可用的结果：${reason}。\n\n`
          + `请针对这个原因改它（比如调大最大步数、改正工具参数、换个查询方式），`
          + `改完继续回答原来的问题：${turn.question}`
        : `${turn.question}\n\n（上一次没有成功：${reason}。请针对这个原因调整，重新给出完整的工作流。）`,
    })
  },

  hydrate: async (conversationId, turnId) => {
    const turn = findTurn(conversationId, turnId)
    const runId = turn?.run?.id
    if (!turn || !runId || turn.hydrated) return
    // 核对期间用户点了重试或停止：拿回来的是上一次的状态，作废
    const epoch = epochs.get(turnId) ?? 0
    const stale = () => (epochs.get(turnId) ?? 0) !== epoch
    const patch = patcher(set, conversationId, turnId)
    patch(() => ({ hydrated: 'pending' }))
    /**
     * 查到了库里那行还不知道的结局：内存里改，库里也改。只改内存的话那行永远是
     * running——每次进来都要再核对一遍，Copilot 的上下文把它当没答完的一轮漏掉，
     * 运行一删它就只能退成「上次没有跑完」，连「已取消」都说不出来了
     */
    const conclude = (fields: Partial<ChatTurn>, error: string) => {
      patch(() => fields)
      void persist(conversationId, turnId, { status: 'error', error })
    }
    let run: Run
    try {
      run = await api.runs.get(runId)
    } catch (e) {
      const cur = findTurn(conversationId, turnId)
      if (stale() || !cur) return
      if (e instanceof ApiError && e.status === 404) {
        if (cur.phase === 'checking') {
          conclude({
            hydrated: 'done', runStatus: 'gone', phase: 'error', status: PHASE_TEXT.error,
            error: GONE.title, failure: GONE,
          }, GONE.title)
          return
        }
        // 没取全的答案，运行又删了：补不回来，但得说出来，也记下来
        const lost = cur.phase === 'done' && !cur.review?.answer
          && (cur.clipped === 'partial' || atClip(String(cur.output?.answer ?? '')))
        patch((t) => ({ hydrated: 'done', runStatus: 'gone', clipped: lost ? 'lost' : t.clipped }))
        if (lost) void persist(conversationId, turnId)
      } else if (cur.phase === 'checking') {
        // 后端出错、超时：不知道它现在怎样，但不能一直停在「正在核对」转圈——那之后
        // 再也没有人来核对（重连只在断开过之后才触发）。说清楚，给「重新核对」
        const said = errorMessage(e)
        const raw = humanizeError(e).raw
        const failure: Failure = {
          title: PHASE_TEXT.unknown,
          reason: `查询本轮运行状态时出错（${said}）。运行可能仍在进行，也可能已结束。`,
          hint: '请稍后点击「重新核对」；服务端重新连接后会自动再核对一次。',
          // 原话已经在括号里的，技术细节不再重复一遍
          detail: raw && !said.includes(raw) ? raw : undefined,
        }
        patch(() => ({
          hydrated: undefined, phase: 'unknown', status: PHASE_TEXT.unknown, failure, error: lineOf(failure),
        }))
      } else {
        patch(() => ({ hydrated: undefined }))   // 已经有个结局摆着，下次再补
      }
      return
    }
    const pending = run.status === 'interrupted' ? await pendingApproval(run.id) : false
    const cur = findTurn(conversationId, turnId)
    if (!cur || stale()) return
    patch(() => ({ run, runStatus: run.status, final: { status: run.status, pending }, hydrated: 'done' }))
    const unsettled = cur.phase === 'checking' || cur.phase === 'suspended' || cur.phase === 'error'

    // 这一轮其实还活着：接回去，别让它在历史里装死
    if (run.status === 'running' || run.status === 'queued') {
      if (unsettled) {
        const flow = flowOf(conversationId, turnId)
        // 计时不从提问那天算：中断过的接着上次停下的地方走，停在半路的从运行开始算
        const spent = cur.endedAt ? Math.max(0, cur.endedAt - cur.startedAt) : null
        const startedAt = spent != null ? Date.now() - spent
          : parseServerTime(run.started_at ?? null)?.getTime() ?? cur.startedAt
        flow.patch(() => ({
          phase: 'running', status: PHASE_TEXT.running, failure: null, error: '', startedAt, endedAt: undefined,
        }))
        watch(flow, run.id, cur.lastSeq, cur.question)
      }
      return
    }
    if (run.status === 'interrupted') {
      if (pending) {
        // 还在等人：库里那行保持 running 就是对的，它确实没结束
        patch(() => ({ phase: 'waiting', status: PHASE_TEXT.waiting, failure: null, error: '' }))
        void useCatalog.getState().refreshApprovals()
      } else if (cur.phase !== 'done' && cur.phase !== 'suspended') {
        conclude({ phase: 'suspended', status: PHASE_TEXT.suspended, failure: SUSPENDED, error: '' }, PHASE_TEXT.suspended)
      }
      return
    }
    if (run.status === 'cancelled') {
      if (cur.phase !== 'done' && cur.phase !== 'cancelled') {
        conclude({ phase: 'cancelled', status: PHASE_TEXT.cancelled, failure: null, error: '' }, '已取消')
      }
      return
    }
    if (run.status === 'failed') {
      if (cur.phase === 'error' && cur.failure) return
      if (cur.phase !== 'done') {
        const failure = runFailure(run.error || '')
        conclude({ phase: 'error', status: PHASE_TEXT.error, failure, error: lineOf(failure) }, lineOf(failure))
      }
      return
    }
    if (run.status !== 'succeeded') return
    if (unsettled) {
      // 跑完了，但交付没赶上（复核期间关了页面、服务重启）：把这一轮补完，
      // 走一遍和实时一样的复核与落库。相位停在「正在核对」而不是「运行中」：运行
      // 早就结束了，按实时算的计时会从提问那天一路数到现在（「72:00:01」）
      const flow = flowOf(conversationId, turnId)
      flow.patch(() => ({ phase: 'checking', status: '正在核对结果…', failure: null, error: '' }))
      // 耗时按运行真正结束的时刻算：补交付可能发生在几天之后
      const endedAt = parseServerTime(run.finished_at ?? null)?.getTime()
      void settle(flow, run.id, cur.question, run.output ?? null, { run, endedAt })
      return
    }
    // 已经交付过的：答案没取全就换成完整的，顺手把档位补回来
    const full = answerText(run.output)
    const stored = String(cur.output?.answer ?? '')
    const repaired = !cur.review?.answer && stored.length >= 200
      && full.length > stored.length && full.startsWith(stored.slice(0, 200))
    const issuance = slimIssuance(run.output?._issuance)
    if (repaired) {
      patch(() => ({ output: run.output, clipped: undefined }))
      void persist(conversationId, turnId, { answer: full })
    } else if (hasEvidence(run.output) && !hasEvidence(cur.output) && !cur.review?.answer) {
      // 库里只存了答案的文字，逐段证据的标注（_evidence）和字段名都在运行的成果里：
      // 换回运行那份，刷新之后报告里的数字照样能点开。复核改写过的不换（有证据的不会被改写）
      patch(() => ({ output: run.output, clipped: undefined }))
    } else if (issuance && !cur.output?._issuance) {
      patch((t) => ({ output: t.output ? { ...t.output, _issuance: issuance } : t.output }))
    }
  },

  recheck: (conversationId, turnId) => {
    const turn = findTurn(conversationId, turnId)
    if (!turn || turn.phase !== 'unknown') return Promise.resolve()
    patcher(set, conversationId, turnId)(() => ({
      phase: 'checking', status: PHASE_TEXT.checking, failure: null, error: '', hydrated: undefined,
    }))
    return get().hydrate(conversationId, turnId)
  },

  reattach: async (conversationId, turnId) => {
    const turn = findTurn(conversationId, turnId)
    if (!turn?.run) return
    // 等审批时的那条流后端不关；不先关掉它，恢复后的事件会从两条流各来一遍
    turn.cancel?.()
    const flow = flowOf(conversationId, turnId)
    // 计时接着上次停下的地方走，不从提问那一刻算：中断、失败之后隔了一夜才点
    // 「接着跑」，头部不该写「已运行 14 小时」。同一次会话里等审批的那段照算，
    // 它是这一轮过程的一部分
    const spent = turn.endedAt ? Math.max(0, turn.endedAt - turn.startedAt) : 0
    const startedAt = turn.endedAt || turn.restored ? Date.now() - spent : turn.startedAt
    // 上一轮的错要清掉：接着跑之后它还挂在那里，就成了一条已经不成立的红字
    flow.patch(() => ({
      phase: 'running', status: PHASE_TEXT.running, error: '', failure: null, startedAt,
      final: undefined, runStatus: 'running', endedAt: undefined, cancel: null, writing: null,
    }))
    watch(flow, turn.run.id, turn.lastSeq, turn.question)
  },
}))

if (import.meta.env.DEV) {
  ;(window as any).__chat = useChat
}

/** 定点更新某个会话里的某一轮。认的是 turn id，所以切走看别的会话也不受影响 */
function patcher(set: SetState, conversationId: string, turnId: string): Patch {
  return (fn) =>
    set((s: ChatState) => {
      const turns = s.byConversation[conversationId]
      if (!turns) return s
      return {
        byConversation: {
          ...s.byConversation,
          [conversationId]: turns.map((t) => (t.id === turnId ? { ...t, ...fn(t) } : t)),
        },
      }
    })
}

/** 按 seq 去重合并：历史事件取回来之后又接上了流，重叠的那段只要一份 */
function mergeEvents(have: RunEvent[], incoming: RunEvent[]): RunEvent[] {
  if (!have.length) return incoming
  const seen = new Set(have.map((e) => e.seq))
  const fresh = incoming.filter((e) => !e.seq || !seen.has(e.seq))
  if (!fresh.length) return have
  const last = have[have.length - 1]?.seq ?? 0
  const merged = [...have, ...fresh]
  // 绝大多数时候新来的都排在后面，不必每批都排序
  return fresh.every((e) => (e.seq ?? 0) > last) ? merged : merged.sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0))
}

async function pendingApproval(runId: string): Promise<boolean> {
  try {
    const rows = await api.approvals.list({ run_id: runId, status: 'pending' })
    return rows.some((a) => a.run_id === runId && a.status === 'pending')
  } catch {
    // 查不到就按「在等人」算：那是 interrupted 最常见的来源，说错了也只是多一张卡
    return true
  }
}

/** 这一轮现在的样子，折成一条留档 */
function attemptOf(t: ChatTurn): Attempt {
  const n = (t.attempts?.length ?? 0) + 1
  const answer = answerText(t.output) || undefined
  const base = {
    n, at: t.endedAt ?? Date.now(), runId: t.run?.id ?? null,
    answer: answer && answer.length > ATTEMPT_ANSWER_CAP ? answer.slice(0, ATTEMPT_ANSWER_CAP) + '…' : answer,
  }
  const signals = (t.review?.signals ?? []).map((s) => s.detail).join('\n') || undefined
  if (t.phase === 'cancelled') return { ...base, outcome: 'cancelled', summary: '已手动停止' }
  if (t.phase === 'suspended') return { ...base, outcome: 'suspended', summary: '服务重启，运行中断' }
  if (t.phase === 'error' || t.failure) {
    const f = t.failure ?? failureOf(t.error || '发生错误')
    return { ...base, outcome: 'failed', summary: lineOf(f), detail: f.detail }
  }
  if (t.review?.severity === 'broken') {
    return { ...base, outcome: 'unusable', summary: `结论不可用：${t.review.note}`, detail: signals }
  }
  if (t.review?.severity === 'degraded') {
    return { ...base, outcome: 'partial', summary: `存在缺口：${t.review.note}`, detail: signals }
  }
  return { ...base, outcome: 'done', summary: '已给出答案' }
}

/**
 * 图里 agent 节点的步数上限（取最大的那个）。没配的跟随设置里的默认值 fallback；
 * 以前画布自动写进去的 12 后端按「没配」处理（engine/guards.py），这里口径一致
 */
export function agentSteps(graph: GraphSpec | null | undefined, fallback: number): number | null {
  const agents = (graph?.nodes ?? []).filter((n: any) => n.type === 'agent')
  if (!agents.length) return null
  return Math.max(...agents.map((n: any) => {
    const steps = Number(n.data?.config?.max_steps)
    return steps > 0 && steps !== 12 ? steps : fallback
  }))
}

function withMaxSteps(graph: GraphSpec, steps: number): GraphSpec {
  return {
    ...graph,
    nodes: graph.nodes.map((n: any) => (n.type === 'agent'
      ? { ...n, data: { ...n.data, config: { ...(n.data?.config ?? {}), max_steps: steps } } }
      : n)),
  }
}

/**
 * 用户的问题填进这张图的入口字段。
 *
 * 在此之前这里是硬编码的 `input: {}`——问题根本没进图。Copilot 只要给入口
 * 声明了一个必填字段（它有时候会，比如 `topic`），这次运行就在第一个节点
 * 上立刻挂掉："缺少必填输入：topic"。用户看到的是自己刚问完就报错，而且
 * 错误里提的那个字段名他从来没见过——那是生成的图里的东西。
 *
 * 之前"能跑"纯属运气：Copilot 那一次恰好没声明字段。
 *
 * history 是另一件事：图跑起来的时候，模型得知道"它们"指的是上一轮那批数据。
 * Copilot 侧的上下文只影响图长什么样，管不到这个。
 */
export function fillInput(
  graph: GraphSpec, question: string, history = '',
): Record<string, any> {
  const entry = (graph.nodes ?? []).find((n: any) => n.type === 'input')
  const fields: any[] = (entry?.data?.config?.fields ?? []) as any[]
  if (!fields.length) return { question, history }   // 没声明字段也带上，下游可能用 input.question

  const payload: Record<string, any> = { history }
  for (const f of fields) {
    if (f?.name) payload[f.name] = f.default ?? ''
  }
  // 问题往哪个字段放：名字像"问题"的优先，其次第一个必填的，再不然第一个。
  // 只有一个字段时不用挑——那必然就是它
  const named = fields.find((f) =>
    /question|query|topic|input|问题|需求|主题/i.test(String(f?.name ?? '')))
  const target = named ?? fields.find((f) => f?.required) ?? fields[0]
  if (target?.name) payload[target.name] = question
  return payload
}

/** 把这次会话之前几轮压成纯文本，塞进 input.history */
function historyOf(conversationId: string, upToTurnId: string): string {
  const turns = useChat.getState().byConversation[conversationId] ?? []
  const lines: string[] = []
  for (const t of turns) {
    if (t.id === upToTurnId) break
    const answer = answerText(t.output) || t.explanation
    if (t.question && answer) lines.push(`问：${t.question}\n答：${answer.slice(0, 600)}`)
  }
  return lines.slice(-6).join('\n\n')
}

/** 一轮最多自动再试一次。第二次还不行多半是问题本身没说清，该让用户介入 */
const MAX_ATTEMPTS = 2

interface PlanArgs {
  /** 用户原本问的是什么。跑图取数、复核都要用它，不能被重试的补充说明污染 */
  question: string
  /** 真正发给 Copilot 的那句话。重试时会在后面缀上失败原因 */
  instruction: string
  /** 重试时把上一次那张图交回去，让它改而不是重建。null = 从零建 */
  baseGraph?: GraphSpec | null
  attempt: number
  /** 这一问限定只查的数据源。空或不给 = 不限 */
  scope?: ScopeSource[]
}

/**
 * 建图 → 跑图。从 ask 里拆出来，是为了能带着失败原因原地重来一次。
 *
 * 重试必须回到**同一轮**：用户问了一个问题，中间重建了一次图是过程，不是
 * 两次对话。之前这件事全靠用户自己发现不对、再打一句「重来」——他那句话
 * 本来就是在手动做这件事。
 */
function plan(flow: Flow, args: PlanArgs) {
  const { question, instruction, baseGraph, attempt, scope } = args
  const { patch } = flow

  /** 这一轮建出来的图。retry 要把它交回去，所以得在闭包里留一份 */
  let built: GraphSpec | null = baseGraph ?? null

  /**
   * 带着原因自动重来一次。超过上限就把原因如实留在这一轮上，不再烧钱。
   *
   * 关键是**把失败的那张图一起交回去**。只给一句"上次没成功：工具参数错了"
   * 而不给图，模型只能照着原问题从零再建一遍——很可能建出一模一样的那张，
   * 白跑一个来回。而且它连自己上次建了什么都看不到：会话历史按
   * `status != running` 过滤，正在重试的这一轮恰好被排除在外。
   *
   * 交回去之后走的是后端的"改图"分支：图预置进累加器，模型只输出改动操作，
   * 于是调大最大步数、改正工具参数这种事就是改一个字段，不是推倒重来。
   *
   * 这里会清空这一次的过程和答案——它是自动补救，用户还没拿到过答案。用户主动
   * 点的重试走 retryTurn，上一次要留档。
   */
  const retry = (reason: string) => {
    if (attempt + 1 >= MAX_ATTEMPTS || !flow.alive()) return false
    const base = built && (built.nodes?.length ?? 0) > 0 ? built : null
    patch(() => ({
      attempt: attempt + 1,
      phase: 'planning',
      status: base ? '上次运行未成功，正在调整工作流…' : '上次运行未成功，正在重新生成工作流…',
      ops: [], events: [], run: null, output: null, final: undefined, runStatus: undefined,
      review: null, reviewRaw: null, rawOutput: null, lastSeq: 0, failure: null, error: '',
    }))
    plan(flow, {
      question,
      attempt: attempt + 1,
      baseGraph: base,
      scope,
      instruction: base
        ? `这个工作流上一次运行没有成功：${reason}。\n\n`
          + `请针对这个原因改它（比如调大最大步数、改正工具参数、换个查询方式），`
          + `改完继续回答原来的问题：${question}`
        : `${question}\n\n（上一次没有成功：${reason}。请针对这个原因调整，重新给出完整的工作流。）`,
    })
    return true
  }

  /** 建图阶段就停下了：没有运行，没有断点，只能重试或换个说法 */
  const fail = (failure: Failure, graph?: GraphSpec) => {
    patch(() => ({
      ...(graph ? { graph } : {}),
      phase: 'error', status: PHASE_TEXT.error, error: lineOf(failure), failure,
      cancel: null, endedAt: Date.now(),
    }))
    void flow.persist({ status: 'error', error: lineOf(failure) })
  }

  const cancelCopilot = streamCopilot(
    {
      instruction, base_graph: baseGraph ?? null, intent: 'answer', conversation_id: flow.conversationId,
      ...(scope?.length ? { datasource_ids: scope.map((d) => d.id) } : {}),
    },
    (op) => {
      // 停止之后缓冲里还没派发完的操作：一条 final 就能把这一轮又跑起来
      if (!flow.alive()) return
      // 先原样收下，翻译交给 decodeCopilot。thinking 合并在写入时做：
      // 一次生成几百上千条 delta，逐条存下来光数组就比图大一个量级
      patch((t) => {
        const last = t.ops[t.ops.length - 1]
        if (op.op === 'thinking' && last?.op === 'thinking') {
          const merged = { ...last, delta: String(last.delta ?? '') + String(op.delta ?? '') }
          return { ops: [...t.ops.slice(0, -1), merged] }
        }
        return { ops: [...t.ops, op] }
      })
      switch (op.op) {
        case 'thinking':
          patch((t) => ({
            phase: 'planning',
            thinking: (t.thinking + (op.delta ?? '')).slice(-2000),
          }))
          break
        case 'heartbeat':
          patch(() => ({ status: PHASE_TEXT[(op.phase as Phase) ?? 'planning'] ?? '正在处理…' }))
          break
        case 'plan':
          patch(() => ({ phase: 'building', status: op.summary || '正在生成工作流…' }))
          break
        case 'add_node':
          patch((t) => ({
            phase: 'building',
            status: `正在生成工作流…（${t.ops.filter((o) => o.op === 'add_node').length} 步）`,
          }))
          break
        case 'done':
          patch(() => ({ explanation: op.explanation ?? '' }))
          break
        case 'reply': {
          // 这句话不需要工作流——问的是前几轮已经查出来的东西，或者是
          // 对过程说的话（「重试」「换个说法」）。以前这些也各自重建一整张
          // 图、跑一遍 153 张表的库，纯属白花钱
          const text = String(op.text ?? '')
          patch(() => ({
            phase: 'done', status: PHASE_TEXT.done, cancel: null, endedAt: Date.now(),
            output: { answer: text }, noQuery: true,
          }))
          void flow.persist({ status: 'done', answer: text })
          break
        }
        case 'final': {
          const graph = op.graph as GraphSpec
          built = graph          // 要重试的话，交回去的就是它
          // 连 meta 一起写：它的 at 是「建图还有动静」的凭据，后端这时也会刷新 updated_at
          void flow.persist({ graph, explanation: op.explanation ?? '' })

          // 没过校验的图不能拿去跑：引擎会照实抛出它自己的措辞（"图是空的，
          // 先拖一个节点进来"），而用户根本不在画布上，这句话对他毫无意义。
          // 这条路真的把它当答案交到用户面前过
          const blockers = ((op.issues ?? []) as ValidationIssue[])
            .filter((i) => i.level === 'error')
            .map((i) => i.message)
          if (blockers.length && retry(`生成的工作流没有通过校验：${blockers.join('；')}`)) break
          if (blockers.length) {
            // 重来一次还是没过。校验消息是写给画布用户的（"先拖一个节点进来"），
            // 直接甩给一个在问数据页打字的人毫无意义——先给一句他能照做的，
            // 原文留在技术细节里作线索
            fail({
              title: '未能生成可运行的工作流',
              reason: '已自动调整一次，生成的工作流仍未通过检查。',
              hint: '请补充描述后重新提问，写明要查询的数据库、时间范围和指标。',
              detail: blockers.join('\n'),
            }, graph)
            break
          }

          // 用户要的是流程本身时（「设计一个每天跑的工作流」），建完就跑
          // 等于替他多花一次钱，而他要的是那张图
          if (op.autorun === false) {
            patch(() => ({
              graph, phase: 'ready', status: PHASE_TEXT.ready, cancel: null,
              explanation: op.explanation ?? '', pendingRun: true, endedAt: Date.now(),
            }))
            void flow.persist({ status: 'done' })
            break
          }
          patch(() => ({
            graph, phase: 'running', status: PHASE_TEXT.running,
            explanation: op.explanation ?? '',
          }))
          void launch(flow, graph, question, retry)
          break
        }
        case 'error':
          // 后端给的是三段：message 人话、hint 怎么办、detail 原始异常。
          // 原文不再截成一行甩给用户，收进可展开、可复制的技术细节
          fail({
            ...failureOf(op.message ?? '生成失败'),
            ...(op.hint ? { hint: String(op.hint) } : {}),
            ...(op.detail ? { detail: String(op.detail) } : {}),
          })
          break
      }
    },
    (error, info?: { code?: string }) => {
      if (!error || !flow.alive()) return
      const turn = findTurn(flow.conversationId, flow.turnId)
      if (!turn || !BUSY_PHASES.has(turn.phase) || turn.run) return
      // 限定的库一个都不在了：后端在开流之前就 400，带机读码 datasource_scope_empty。
      // 认出来了，补救得是「不限数据源重试」，带着原范围重试只会再被拒一次。
      // 认码不认话；不带码的老后端退回按 detail 的开头认
      if (scope?.length && (info?.code === 'datasource_scope_empty' || error.startsWith('限定的数据源'))) {
        const at = error.indexOf('：')
        fail({
          title: at > 0 ? error.slice(0, at) : error,
          reason: at > 0 ? `${error.slice(at + 1).split('。')[0]}。` : undefined,
          hint: '点击「不限数据源重试」；如仍需限定该数据源，请先前往「数据」页确认它存在且已启用。',
          detail: error,
          scopeGone: true,
        })
        return
      }
      fail(failureOf(error))
    },
  )
  patch(() => ({ cancel: cancelCopilot }))
}

async function launch(
  flow: Flow, graph: GraphSpec, question: string, retry?: (reason: string) => boolean,
) {
  const { patch } = flow
  try {
    const run = await api.runs.start({
      graph, input: fillInput(graph, question, historyOf(flow.conversationId, flow.turnId)),
    })
    // 启动请求在路上时用户点了停止或重试：运行已经起来了，得追一个取消过去
    if (!flow.alive()) {
      void api.runs.cancel(run.id).catch(() => undefined)
      return
    }
    patch(() => ({ run, runStatus: run.status }))
    // meta 里的 runId 优先于 run_id 列（重试建图失败时那一列清不掉），所以得一起改：
    // 只写列的话，重试出来的这次运行在库里一直挂着 runId=null，跑着的时候刷新或者
    // 另开一页，它就成了「断在建图阶段」，放久了还会被当成断了写回 error。
    // 「跑一下」发起时库里那行还是 done，一并改成 running
    void flow.persist({ run_id: run.id, status: 'running' })
    watch(flow, run.id, 0, question, retry)
  } catch (e) {
    // 发起那一下没到后端：流程是好的，原样再发起就行。通用的「稍等几秒会自动重试」
    // 在这里不成立——这一页没有谁会替它重发
    const failure: Failure = isNetworkError(e)
      ? { ...failureOf(e, { hint: '服务端恢复连接后，点击「重新运行本轮」，已生成的工作流无需重新构建。' }), rerun: true }
      : startFailure(e)
    patch(() => ({
      phase: 'error', status: PHASE_TEXT.error, error: lineOf(failure), failure,
      cancel: null, endedAt: Date.now(),
    }))
    void flow.persist({ status: 'error', error: lineOf(failure) })
  }
}

/** 一帧之内到的事件并成一次写入。后台标签页不跑 rAF，另挂一个定时器兜底 */
function batcher(flush: () => void): { push: () => void; now: () => void } {
  let armed = false
  const run = () => {
    if (!armed) return
    armed = false
    flush()
  }
  return {
    push: () => {
      if (armed) return
      armed = true
      requestAnimationFrame(run)
      setTimeout(run, 120)
    },
    now: run,
  }
}

/**
 * 模型出字的计数器：token 和思考增量只在这里累计，节流写回这一轮的 writing。
 *
 * 一轮运行八千条增量，逐条 patch 就是八千次重渲染；而人要的只是「写了多少、
 * 在想什么」。任何一条结构事件（下一次调用、调了工具、节点结束）都说明这一段
 * 出字已经结束，计数清零
 */
function penOf(patch: Patch): { add: (e: RunEvent) => void; clear: () => void } {
  let chars = 0
  let thought = ''
  let timer: ReturnType<typeof setTimeout> | null = null
  const flush = () => {
    timer = null
    patch(() => ({ writing: chars || thought ? { chars, thought } : null }))
  }
  return {
    add: (e) => {
      const delta = String(e.data?.delta ?? '')
      if (!delta) return
      if (e.type === 'llm.token') chars += delta.length
      else thought = (thought + delta).slice(-400)
      if (timer) return
      const reduced = typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches
      timer = setTimeout(flush, reduced ? PEN_MS_REDUCED : PEN_MS)
    },
    clear: () => {
      if (timer) clearTimeout(timer)
      timer = null
      if (!chars && !thought) return
      chars = 0
      thought = ''
      patch(() => ({ writing: null }))
    },
  }
}

/**
 * 订阅运行事件。
 *
 * 只做两件事：把事件原样存进这一轮，以及更新那几个驱动 UI 状态机的相位。
 * "查了哪张表、跑了什么 SQL"不在这里翻译——decodeRun 会从同一批事件里读出来。
 *
 * 结局有两个来源：事件本身（run.finished / failed / cancelled、服务关停的 log），
 * 和流结束时后端带来的 stream.end {status}。后者兜住"事件没来齐就断了"的情况——
 * 服务重启后重连，后端回放完就发 stream.end status=interrupted；以前前端不认它，
 * 这一轮就一直转圈。
 */
function watch(
  flow: Flow, runId: string, after = 0,
  question = '', retry?: (reason: string) => boolean,
) {
  const { patch, conversationId, turnId } = flow
  /** 这条流上已经见到结局事件了，stream.end 不用再判一次 */
  let ended = false
  let buffer: RunEvent[] = []
  const batch = batcher(() => {
    if (!buffer.length) return
    const got = buffer
    buffer = []
    patch((t) => ({
      events: mergeEvents(t.events, got),
      // 记下进度：审批恢复要重新接流，带上它才不会把历史再收一遍
      lastSeq: Math.max(t.lastSeq ?? 0, ...got.map((e) => e.seq ?? 0)),
    }))
  })
  const current = () => findTurn(conversationId, turnId)
  const pen = penOf(patch)

  const suspend = () => {
    batch.now()
    pen.clear()
    patch(() => ({
      phase: 'suspended', status: PHASE_TEXT.suspended, failure: SUSPENDED, error: '',
      final: { status: 'interrupted', pending: false }, runStatus: 'interrupted',
      endedAt: Date.now(),
    }))
    void flow.persist({ status: 'error', error: PHASE_TEXT.suspended })
  }

  const stop = streamRun(runId, (event: RunEvent) => {
    if (!flow.alive()) return
    // token 和思考增量：后端不落库，decodeRun 也一开头就丢掉。存进 events 的唯一
    // 效果是每个 token 都复制一遍整个事件数组、重解码整个会话——一轮运行八千条。
    // 只留一个计数
    if (EPHEMERAL.has(event.type)) {
      pen.add(event)
      return
    }
    if (event.type !== 'log') pen.clear()
    buffer.push(event)
    batch.push()
    const d: any = event.data ?? {}
    // 补发的历史里的结局是"当时"的结局：一条失败后又接着跑的运行，回放里先有
    // run.failed、后有 run.resumed。照着它改相位、落库，这一轮会先闪成失败再
    // 变回来。回放完后端会用 stream.end 说出现在的状态，结局以那个为准
    if (event.replay && REPLAY_SKIP.has(event.type)) return
    switch (event.type) {
      case 'tool.start': {
        const tool = String(d.tool ?? '')
        patch(() => ({
          status: tool.startsWith('db_query') ? '正在查询数据…'
            : tool.startsWith('db_schema') ? '正在读取数据结构…'
            : tool ? `正在调用 ${tool}…` : '正在运行…',
        }))
        break
      }
      case 'llm.start':
        patch(() => ({ status: '正在分析…' }))
        break
      case 'run.resumed':
        patch(() => ({ phase: 'running', status: PHASE_TEXT.running, final: undefined }))
        break
      case 'run.interrupted':
        patch(() => ({ phase: 'waiting', status: PHASE_TEXT.waiting }))
        // 审批列表平时 4 秒才轮询一次，头部已经写着「等待审批」，通过/驳回的
        // 按钮却要晚几秒才出现——这一下立刻去拿
        void useCatalog.getState().refreshApprovals()
        break
      case 'log':
        // 服务在关停：这一轮会停在断点上。停在审批上的仍然是在等人
        if (d.code === 'server_shutdown' && current()?.phase !== 'waiting') suspend()
        break
      case 'run.cancelled':
        ended = true
        batch.now()
        patch(() => ({
          phase: 'cancelled', status: PHASE_TEXT.cancelled, cancel: null, endedAt: Date.now(),
          runStatus: 'cancelled', final: { status: 'cancelled' },
        }))
        void flow.persist({ status: 'error', error: '已取消' })
        break
      case 'run.failed': {
        ended = true
        batch.now()
        const failure = runFailure(d.error ? String(d.error) : '', d.detail ? String(d.detail) : undefined)
        patch(() => ({
          phase: 'error', status: PHASE_TEXT.error, error: lineOf(failure), failure,
          cancel: null, endedAt: Date.now(), runStatus: 'failed', final: { status: 'failed' },
        }))
        void flow.persist({ status: 'error', error: lineOf(failure) })
        break
      }
      case 'run.finished':
        ended = true
        batch.now()
        patch(() => ({ status: '正在核对结果…', runStatus: 'succeeded' }))
        // 跑完不等于答对：先接住这次编排，确认没问题再交付
        void settle(flow, runId, question, d.output ?? null, {
          truncated: d.output_truncated === true, retry,
        })
        break
    }
  }, (end) => {
    batch.now()
    pen.clear()
    if (ended || !flow.alive()) return
    const status = String(end?.status ?? '')
    const turn = current()
    if (!turn) return
    if (status === 'interrupted' || status === 'suspended') {
      // 连到一条已经停下的运行：分清是停在审批上，还是服务重启挂起的
      void (async () => {
        const pending = status === 'suspended' ? false : await pendingApproval(runId)
        if (!flow.alive()) return
        if (pending) {
          patch(() => ({ phase: 'waiting', status: PHASE_TEXT.waiting, final: { status, pending } }))
          void useCatalog.getState().refreshApprovals()
        } else {
          suspend()
        }
      })()
      return
    }
    if (status === 'succeeded') {
      // 结局事件在断线时丢了：成果以库里为准
      ended = true
      void settle(flow, runId, question, null, { retry })
      return
    }
    // 失败、取消、或者运行不见了：状态和原因让 hydrate 去读
    patch(() => ({ hydrated: undefined, phase: 'checking', status: PHASE_TEXT.checking, cancel: null }))
    void useChat.getState().hydrate(conversationId, turnId)
  }, after)
  patch(() => ({ cancel: stop }))
}


/**
 * 接住一次编排的产出，确认能不能就这么交给用户。
 *
 * 「跑完了」和「答得对」是两回事。库里能查到大量 status=succeeded 的运行，
 * 过程中检索降级了、工具报错了、agent 步数用满了，而交到用户手上的答案对此
 * 只字不提——最糟的一次交出去的干脆是一句内部管道文案。
 *
 * 后端先用规则扫一遍事件流，干净的运行直接返回 ok，一次模型都不调；扫出异常
 * 才叫模型重组答案并把话说破（backend/app/engine/review.py）。
 *
 * 这个函数必须**在任何情况下都把答案交出去**：复核是补一层说明，它自己失败了
 * 不能把已经到手的结果一起带走，更不能让这一轮永远停在"正在核对"。
 */
async function settle(
  flow: Flow, runId: string, question: string,
  eventOutput: Record<string, any> | null,
  opts: { truncated?: boolean; retry?: (reason: string) => boolean; run?: Run; endedAt?: number },
) {
  const { patch } = flow

  // 成果以库里那份为准。run.finished 事件里的 output 为了控制推送体积被切过
  // （字典每个值 2000 字），以前落库、显示的都是它：几千字的报告停在半句话上，
  // 看起来却是完整的，刷新之后还是那半截
  let run: Run | null = opts.run ?? null
  if (!run) {
    try { run = await api.runs.get(runId) } catch { /* 取不到就用事件里那份，下面会标出来 */ }
  }
  const full = run?.output && Object.keys(run.output).length ? run.output : null
  const output = full ?? eventOutput
  const clipped = !full && (opts.truncated || looksClipped(eventOutput)) ? 'partial' as const : undefined
  patch((t) => ({ run: run ?? t.run, runStatus: 'succeeded', final: { status: 'succeeded' } }))

  // 成果**先落库**再显示。两件事顺序不能并：
  //  - 落库要马上，否则复核期间关掉标签页，这次跑出来的东西就没了；
  //  - 显示要等复核，否则用户已经把结论读完、信了，说明才姗姗来迟——
  //    实测复核要十秒上下，这十秒里他看到的是一个没人核对过的答案。
  void flow.save({ status: 'done', answer: answerText(output) })

  let result: ReviewResult | null = null
  try {
    // 复核慢到一定程度就不等了。它是补一层说明，不该反过来把答案扣住不放——
    // 干净的运行后端 15ms 就返回，会走到这个上限的都是真叫了模型的那些
    result = await Promise.race([
      api.copilot.review({ run_id: runId, question }),
      new Promise<null>((r) => setTimeout(() => r(null), REVIEW_TIMEOUT_MS)),
    ])
  } catch { /* 复核用不了就按原样交付，下面照常走 */ }
  // 复核期间用户点了停止或者重试：这一轮已经不归这次运行管了
  if (!flow.alive()) return

  // 成果带逐段证据时复核只能加说明：改写会把成果整个换成 {answer}，报告文档的每个片段
  // 就都对不上了。后端也拦，这里再拦一道，老后端回来的改写同样不落到界面和库里
  result = guardReview(result, output)
  // 库里存完整结论（包括 verdict='ok'），store 里只留值得摆到界面上的那部分。
  // 两者分开，"这一轮没复核过"和"复核过、没发现问题"才是两件可区分的事——
  // 事后排查"它当时为什么没警告我"，靠的就是这个区别
  const review = result && result.verdict !== 'ok' ? result : null

  // 答案根本不可信，而且原因说得清楚——重建一次图再跑，别让用户自己发现
  if (review?.retry && review.severity === 'broken' && opts.retry) {
    const reason = review.signals.map((s) => s.detail).join('；') || review.note
    if (opts.retry(reason)) return
  }

  const rewritten = review?.answer ?? null
  const issuance = output?._issuance
  patch(() => ({
    phase: 'done',
    status: PHASE_TEXT.done,
    review,
    reviewRaw: result,
    cancel: null,
    endedAt: opts.endedAt ?? Date.now(),
    // 复核改写时读的是库里那份完整成果，改写出来的答案不缺尾巴
    clipped: rewritten ? undefined : clipped,
    // 到这一步才把成果摆出来：复核说明和结论同时出现，而不是结论先读完
    output,
    // 改写是有损的，原件得留着——界面上可以展开对照。它跟着 review 一起
    // 落库，所以刷新之后还在
    ...(rewritten ? {
      rawOutput: { answer: review?.original ?? answerText(output) },
      output: { answer: rewritten, ...(issuance ? { _issuance: issuance } : {}) },
    } : {}),
  }))
  void flow.persist({ status: 'done', answer: rewritten || answerText(output) })
}

export { decodeRun }
