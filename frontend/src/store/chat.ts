import { create } from 'zustand'
import { ANALYSIS_TEXT, analysisMethodLabel, analysisToolOp } from '../lib/termsAnalysis'
import { ApiError, api, streamRun } from '../api/client'
import { streamChatTurn, type ChatTurnEvent, type ChatTurnStart } from '../api/chatTurns'
import type { CopilotOp, FixKind, RunFinal } from '../run/decode'
import { copilotContext, copilotLook, decodeRun } from '../run/decode'
import { CHAT_BLUEPRINT_TEXT, CHAT_RETRY_TEXT, LEDGER_TEXT, LOOK_TEXT, REJECTED_TEXT } from '../lib/terms'
import type { LookEvent } from '../typesApp'
import { errorMessage, humanizeError } from '../lib/errors'
import { hasEvidence } from '../lib/evidence'
import { explainRunError, explainStartError } from '../lib/explain'
import { useCatalog } from './catalog'
import { useConversations } from './conversations'
import type {
  ChatTurnJob, ConversationTurn, GraphSpec, ReviewResult, Run, RunEvent,
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
 * **一轮在服务端跑，这里只发起、只播放**（docs/design/orchestration-round/00-总方案.md §1）。以前建图、发起运行、复核、
 * 按复核重来、落库都是这一页在推：网络一抖，建图那条流就断了、建了一半的图丢掉；页面一关，这一轮停在 running，没人复核、
 * 不会重来，15 分钟后才被判成断了。现在一轮是服务端的一个后台任务（backend/app/chat/job.py）：这里 POST /api/chat/turns 发起，
 * 再订阅这一轮的事件日志（api/chatTurns 的 streamChatTurn：先回放、再接实时，断了按 seq 自动补齐），收到什么照原来的渲染
 * 逻辑画——画布边收边长、过程步骤、答案、复核说明、用量。要不要运行、要不要重来、什么时候交付都是服务端定的，这一页不再判；
 * conversation_turns 那一行也由服务端写，这里不再写这一轮的结局。刷新、关掉再打开，进行中的轮次从这一次尝试的第一条
 * 接着播（ConversationTurn.job.attempt_seq）。运行自己的事件仍经运行的实时流来（watch），只用来画过程。
 *
 * 这里**只存原始流**（Copilot 的操作、运行的事件），不在 store 里翻译。
 * 翻译归 run/decode.ts，全站一份；store 里再译一遍的结果是同一次运行在
 * 对话页和画布上讲出两个不同的故事，而用户没法判断哪个是真的。
 *
 * 轮次按会话分桶存放，刷新从后端的 conversation_turns 恢复。
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

/** 被驳回的那一句：事件流里来的按 run.cancelled 的谁、哪一步、备注拼（REJECTED_TEXT.sub）；核对状态时只有报错原文 */
export interface Rejected {
  sub: string
}

/** 限定这一问只查哪些数据源。名字跟着存：库删了之后，这一轮查的是谁还得说得出来 */
export interface ScopeSource {
  id: string
  name: string
}

/**
 * 用户点「重试这一轮」之前的那一次（服务端归档，backend/app/chat/record.attempt_record）。
 *
 * 用户主动重试不能把上一次抹掉：那一次的答案可能只是有缺口、并非全错，
 * 新的一次也未必更好，两份要能对照。
 */
export interface Attempt {
  n: number
  /** rejected：那一次的运行被人驳回结束（不是手动停止，也不是故障）。老的留档里被驳回的那次记的是 cancelled，不改写 */
  outcome: 'failed' | 'cancelled' | 'rejected' | 'suspended' | 'unusable' | 'partial' | 'done'
  summary: string
  detail?: string
  runId?: string | null
  /** 那一次交出来的答案（截断到 4000 字） */
  answer?: string
  at: number
  /** 失败的那一次的原因（服务端只记原因，说法在这里按 lib/explain 讲，见 causeFailure） */
  cause?: TurnCause
}

/**
 * 这一轮为什么失败（服务端 backend/app/chat/record.py 的 cause）：服务端只记发生了什么，说法在界面层按 lib/explain 讲
 * （causeFailure），和记录页、画布右栏同一套。老轮次记的是翻好的 failure，照旧认
 */
export type TurnCause =
  | { kind: 'refused'; message?: string; code?: string | null }
  | { kind: 'copilot'; message?: string; hint?: string | null; detail?: string | null }
  | { kind: 'blocked'; issues?: string[]; self_checked?: boolean }
  | { kind: 'start'; message?: string; code?: string | null; status?: number | null }
  | { kind: 'run'; error?: string; detail?: string | null }
  | { kind: 'gone' }
  | { kind: 'build_interrupted' }
  /** 服务重启后接着收尾时，发起这一轮的账号已停用、或者已经没有问数据的权限（服务端 chat/job.current_principal） */
  | { kind: 'identity'; message?: string }
  | { kind: 'crash'; message?: string }

/**
 * 刷新之后还得在的可信度信息，存在 conversation_turns.meta（服务端写，backend/app/chat/record.py）。
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
  /**
   * 运行被人驳回结束（outcome 是 cancelled）：服务端记 run.cancelled 带的谁、哪一步、备注（REJECTED_TEXT.sub 拼成一句）；
   * 老轮次记的是拼好的那一句（sub）。刷新之后照样写「被驳回」，不退回成「你已停止本轮」
   */
  rejected?: Partial<Rejected> & { actor?: string; label?: string; note?: string; decision?: string }
  /** 老轮次（页面写的）：翻好的失败说明 */
  failure?: Failure
  /** 服务端写的：失败的原因 */
  cause?: TurnCause
  attempts?: Attempt[]
  /** 这一轮从开问到交付的墙钟毫秒 */
  ms?: number
  /** 查了几次库 */
  queries?: number
  /**
   * 答案没取全（老轮次：以前页面落库时用的是事件里截断过的那份）。服务端落库读的是完整成果，新轮次不会有
   */
  clipped?: 'partial' | 'lost'
  /** 这一问限定了只查哪些数据源 */
  scope?: ScopeSource[]
  /** 这一轮以助手反问收尾（answer 列里是助手问的那句话，不是答案）：刷新之后照样标「助手反问」，不当成没查库的回答 */
  asked?: true | { question?: string; options?: string[] }
  /** 这一轮助手查看过的表（look 事件原样，look §2.5.8）：下一轮服务端据此写「上一轮查看过」，追问时直接点名查看 */
  looks?: LookEvent[]
  /**
   * 成果里有逐段证据（报告撰写节点的文档）。库里只存答案的文字，证据的标注和字段名在
   * 运行的成果里：有这个记号的轮次恢复时要回运行那里取一次
   */
  evidence?: boolean
  /** 这份 meta 写下的时刻 */
  at?: number
  /** 这一轮的花费：助手建图、运行、复核各一份（next-round 细案 01 做法 3），见 TurnUsage */
  usage?: TurnUsage
}

/**
 * 一份用量：服务端 providers/usage.py 的汇总（助手建图的 usage 操作、复核结果的 usage），或者运行的 usage。cost_usd 只是
 * 估得出的那部分；unpriced_calls 次估不出（模型不在价格目录里，或者用量没报全），这时金额前写「≥」
 */
export interface UsageTotal {
  cost_usd?: number | null
  unpriced_calls?: number
  calls?: number
  /** 没做完的调用（被取消、超时）：算在 calls 里，token 只计了已报的（服务端 providers/usage 的 unfinished_calls） */
  unfinished_calls?: number
  input_tokens?: number
  output_tokens?: number
}

/**
 * 这一轮的花费 = 助手建图 + 运行 + 复核（以前只有运行那份，问数据的花费是低报的）。自动重来、用户点的重试都还是这一轮：
 * 前几次建图、复核累加在 copilot / review 里，被换掉的运行累加在 earlier 里；run 是当前这次运行（落库时从 run.usage 抄一份，
 * 恢复的历史轮次不用回源就有数）
 */
export interface TurnUsage {
  copilot?: UsageTotal
  review?: UsageTotal
  earlier?: UsageTotal
  run?: UsageTotal
}

export interface ChatTurn {
  id: string
  /** 后端 conversation_turns 里的 id（也是服务端这一轮的任务 id）。发起请求还在路上时为 null */
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
  /**
   * 这一轮的运行被人驳回结束（服务端 cancelled + end_reason=rejected，05-已知遗留 (a)）。phase 照旧是 cancelled（不是故障、
   * 不给继续运行），说法另写「被驳回」和谁、在哪一步、备注
   */
  rejected?: Rejected | null
  startedAt: number
  /** 交付、失败、取消或中断的时刻。计时器停在这里 */
  endedAt?: number
  /** 已收到的最大运行事件 seq。重新接运行的流时要带上，否则后端把历史整条重推 */
  lastSeq: number
  /** 已收到的最大这一轮事件 seq（服务端的事件日志，和运行事件的 seq 是两套） */
  turnSeq?: number
  /** 关掉这一轮在这一页上开着的流（这一轮的事件日志、运行的实时流）。放 turn 上而不是 store 上：store 单值会被
   *  下一轮 ask 覆盖，上一轮的流就再也关不掉了。只是不看了，服务端那一轮照跑；要停用 stop */
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
  /** 复核原样（含 verdict='ok'） */
  reviewRaw?: ReviewResult | null
  /** 被复核重写之前的答案。改写是有损的，原件得留着让用户能对照 */
  rawOutput?: Record<string, any> | null
  /** 这一轮自动重建过几次图（服务端的 turn.retry）。上限 1——第二次还不行多半是问题本身没说清 */
  attempt?: number
  /** 用户点「重试」之前的各次尝试 */
  attempts?: Attempt[]
  /**
   * 答案不全（老轮次）：partial = 当时只拿到了事件里那份截断的，完整版要回源；
   * lost = 旧版本保存时就被切在 2000 字，对应的运行也删了，补不回来
   */
  clipped?: 'partial' | 'lost'
  /** 落库的查库次数和耗时（事件没取回来时头部小标签靠它） */
  meta?: TurnMeta | null
  /**
   * 模型此刻在出字：只记写了多少、思考想到哪一句，原文不存也不落库。答案要等
   * 复核完才摆出来，这里给的是「它没卡住」的证据，不是提前看答案的口子
   */
  writing?: { chars: number; thought: string } | null
  /** 这一问只查这几个数据源。重试、重新问都沿用它 */
  scope?: ScopeSource[]
  /** 这一轮到目前为止的助手建图、复核、被换掉的运行的用量（当前这次运行的在 run.usage），见 TurnUsage */
  usage?: TurnUsage
  /**
   * 助手问回来的那句话（N11）：这一轮到此结束，问题作为这一轮的内容显示、进历史，输入框提示回答它；用户的回答就是下一轮
   */
  asked?: string
  /** 这一轮在服务端的任务（从库里恢复时带来的）：进行中的从它的 attempt_seq 接着播 */
  job?: ChatTurnJob | null
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
  /** 只停这个会话自己正在跑的那一轮（服务端那一轮连同它的运行一起停） */
  stop: (conversationId: string) => void
  /** 审批完成后重新接上运行的实时流 */
  reattach: (conversationId: string, turnId: string) => Promise<void>
  /** 历史轮次的「执行过程」：第一次点去取事件，之后是展开 / 收起 */
  toggleSteps: (conversationId: string, turnId: string) => Promise<void>
  /** 模型建了图但没自动跑时，用户点「跑一下」 */
  runNow: (conversationId: string, turnId: string) => void
  /** 跑挂了或被服务重启打断：从断点接着跑。成功接上返回 true，被拒时抛出后端的原因 */
  continueTurn: (conversationId: string, turnId: string) => Promise<boolean>
  /**
   * 用户主动重试这一轮：上一次由服务端折叠留档，新的一次接在同一轮里。
   * maxSteps：把图里 agent 的步数上限调到这个值，原图重跑（「放宽步数重跑」）；
   * rerun：原图原样重跑（取消了、中断了的那种，不需要改）；
   * scope：换一个数据源范围重来（限定的库不在了），不给就沿用这一轮的
   */
  retryTurn: (
    conversationId: string, turnId: string,
    opts?: { maxSteps?: number; rerun?: boolean; scope?: ScopeSource[] },
  ) => void
  /** 向后端核对一轮历史：真实状态、完整答案、档位；服务端还在跑的接着播。只做一次 */
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

/** 复核那一步的进度 */
const REVIEWING = '正在核对结果…'

const EMPTY: ChatTurn[] = []

/** 服务端这一轮的任务还在跑（排队、进行中） */
const LIVE_JOB = new Set(['queued', 'running'])

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

/** 事件流里的增量：不落库，decodeRun 一开头就丢掉；存进 events 只会让每条都重解码整个会话 */
const EPHEMERAL = new Set(['llm.token', 'llm.thinking.delta'])

/** 出字计数多久写回一次。数字跳得比人读得快没有意义；关掉动效的人一秒一次 */
const PEN_MS = 250
const PEN_MS_REDUCED = 1000

/** 回放里只收下、不据此改相位的事件：它们说的是过去某一刻的结局 */
const REPLAY_SKIP = new Set(['run.finished', 'run.failed', 'run.cancelled', 'run.interrupted', 'log'])

type Patch = (fn: (t: ChatTurn) => Partial<ChatTurn>) => void
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
 * 成果 dict 压成一句话（同服务端 chat/record.answer_text）。老轮次补全截断的答案时拿它比对
 */
function answerText(output: Record<string, any> | null | undefined): string {
  if (!output) return ''
  const parts: string[] = []
  for (const [key, value] of Object.entries(output)) {
    if (key.startsWith('_') || value == null || value === '') continue
    parts.push(typeof value === 'string' ? value : JSON.stringify(value))
  }
  return parts.join('\n')
}

/** 出具信息瘦一下身：无法回指的数字可能有上百个，横幅只列得下一行 */
function slimIssuance(iss: any): Record<string, any> | undefined {
  if (!iss || typeof iss !== 'object' || !iss.tier) return undefined
  const unmatched = Array.isArray(iss.unmatched_numbers) ? iss.unmatched_numbers.slice(0, 20) : undefined
  return { ...iss, ...(unmatched ? { unmatched_numbers: unmatched } : {}) }
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

const USAGE_KEYS = ['cost_usd', 'unpriced_calls', 'calls', 'unfinished_calls', 'input_tokens', 'output_tokens'] as const

/** 两份用量相加（缺的当 0）。b 不是对象时原样交回 a */
export function addUsage(a: UsageTotal | undefined, b: UsageTotal | null | undefined): UsageTotal | undefined {
  if (!b || typeof b !== 'object') return a
  const out: UsageTotal = { ...(a ?? {}) }
  for (const k of USAGE_KEYS) {
    const v = Number(b[k] ?? 0)
    if (Number.isFinite(v) && v) out[k] = Number(out[k] ?? 0) + v
  }
  if (out.cost_usd != null) out.cost_usd = Math.round(out.cost_usd * 1e6) / 1e6
  return out
}

/** 运行的 usage（形状同 UsageTotal，可能带别的键）→ 只留要记的几项 */
function usageTotal(u: Record<string, any> | null | undefined): UsageTotal | undefined {
  return u && typeof u === 'object' ? addUsage({}, u) : undefined
}

/** 这一轮现在的用量：内存里累加的那几份，加上当前这次运行的（运行还没回来时沿用落库的那份） */
export function usageOfTurn(t: ChatTurn): TurnUsage | undefined {
  const base = t.usage ?? t.meta?.usage
  const run = usageTotal(t.run?.usage) ?? base?.run
  const out: TurnUsage = { ...(base ?? {}), ...(run ? { run } : {}) }
  return Object.keys(out).length ? out : undefined
}

/** 当前这次运行换下来了（自动重来）：它的用量并进 earlier，这一轮的花费不丢这一笔（同服务端 record.retire_run） */
function retireRun(t: ChatTurn): TurnUsage | undefined {
  const u = usageOfTurn(t)
  if (!u?.run) return u
  const { run, ...rest } = u
  return { ...rest, earlier: addUsage(rest.earlier, run) }
}

/**
 * 这一轮的花费：助手 + 运行 + 复核。一份都没有（老轮次、还没开始）是 null。unpriced 是估不出金额的调用次数：
 * 有的话金额只是下限，界面写「≥」（lib/terms 的 runCostText）
 */
export function turnCost(u: TurnUsage | undefined): { usd: number; unpriced: number; calls: number } | null {
  if (!u) return null
  const parts = [u.copilot, u.review, u.earlier, u.run].filter(Boolean) as UsageTotal[]
  if (!parts.length) return null
  const sum = parts.reduce<UsageTotal>((acc, p) => addUsage(acc, p) ?? acc, {})
  return { usd: Number(sum.cost_usd ?? 0), unpriced: Number(sum.unpriced_calls ?? 0), calls: Number(sum.calls ?? 0) }
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
 * 发起运行那一下被拒（服务端记的 start 原因：状态码、说法、机读码）的说明。绑定的工具在本机不存在（422 run_tool_missing）
 * 按 lib/explain 讲，带上直达入口（数据源工具去数据页，自定义 / MCP 工具去工具页），和画布上发起被拒（run/RunControl）
 * 同一套；这时还没有运行 id，流里的报错块不会再拿原话认一遍，所以入口得在这里就记进 failure。别的照旧按一句报错拆
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

/** 建图时服务重启（或者老轮次：页面被关掉）：没有可恢复的断点 */
const BUILD_INTERRUPTED: Failure = {
  title: '上次未完成',
  reason: '生成工作流时页面被关闭或服务重启，本轮没有可恢复的断点。',
  hint: '点击「重试本轮」重新开始。',
}

/** 限定的库一个都不在了：补救是「不限数据源重试」，带着原范围重试只会再被拒一次 */
function scopeGoneFailure(message: string): Failure {
  const at = message.indexOf('：')
  return {
    title: at > 0 ? message.slice(0, at) : message,
    reason: at > 0 ? `${message.slice(at + 1).split('。')[0]}。` : undefined,
    hint: '点击「不限数据源重试」；如仍需限定该数据源，请先前往「数据」页确认它存在且已启用。',
    detail: message,
    scopeGone: true,
  }
}

/**
 * 服务端记的失败原因 → 界面上的说明（说法都在这里和 lib/explain，服务端不写人话标题）。scope 是这一问限定的数据源：
 * 开流前被拒时认得出是不是「限定的库都不在了」（认码不认话；不带码的老说法退回按开头认）
 */
function causeFailure(cause: TurnCause, scope?: ScopeSource[]): Failure {
  switch (cause.kind) {
    case 'refused': {
      const message = String(cause.message ?? '')
      if (cause.code === 'datasource_scope_empty' || (scope?.length && message.startsWith('限定的数据源'))) {
        return scopeGoneFailure(message)
      }
      return failureOf(message || '助手没有开始')
    }
    case 'copilot':
      // 后端给的是三段：message 人话、hint 怎么办、detail 原始异常。原文收进可展开、可复制的技术细节
      return {
        ...failureOf(cause.message || '生成失败'),
        ...(cause.hint ? { hint: String(cause.hint) } : {}),
        ...(cause.detail ? { detail: String(cause.detail) } : {}),
      }
    case 'blocked':
      // 自查修正过（或重来一次）还是没过。校验消息是写给画布用户的（"先拖一个节点进来"），直接甩给一个在问数据页
      // 打字的人毫无意义——先给一句他能照做的，原文留在技术细节里作线索
      return {
        title: '未能生成可运行的工作流',
        reason: cause.self_checked === false ? '已自动调整一次，生成的工作流仍未通过检查。' : '助手已按检查结果修正过，生成的工作流仍未通过检查。',
        hint: '请补充描述后重新提问，写明要查询的数据库、时间范围和指标。',
        detail: (cause.issues ?? []).join('\n') || undefined,
      }
    case 'start':
      return startFailure(new ApiError(Number(cause.status ?? 0), String(cause.message ?? ''), { code: cause.code ?? undefined }))
    case 'run':
      return runFailure(cause.error ?? '', cause.detail ?? undefined)
    case 'gone':
      return GONE
    case 'build_interrupted':
      return BUILD_INTERRUPTED
    case 'identity':
      return {
        title: '上次未完成',
        reason: cause.message || '发起这一轮的账号已无法接着收尾，本轮没有接着做。',
        hint: '如需继续，请由现在有问数据权限的人重新提问。',
      }
    case 'crash':
      return failureOf(cause.message || '发生错误')
  }
  return failureOf('发生错误')
}

/** 归档的那一次：失败的照 lib/explain 再讲一遍（服务端只记原话），别的照服务端写的那一句 */
function withSummary(a: Attempt): Attempt {
  if (a.outcome !== 'failed' || !a.cause) return a
  const f = causeFailure(a.cause)
  return { ...a, summary: lineOf(f), ...(f.detail && !a.detail ? { detail: f.detail } : {}) }
}

const attemptsOf = (list: unknown): Attempt[] | undefined =>
  Array.isArray(list) && list.length ? (list as Attempt[]).map(withSummary) : undefined

/** 被驳回的那一句：服务端记的是谁、哪一步、备注，按 REJECTED_TEXT.sub 拼；老轮次记的是拼好的那一句 */
function rejectedOf(r: TurnMeta['rejected']): Rejected | null {
  if (!r || typeof r !== 'object') return null
  return { sub: typeof r.sub === 'string' && r.sub ? r.sub : REJECTED_TEXT.sub(r) }
}

// -------------------------------------------------------------------------
// 从库里恢复
// -------------------------------------------------------------------------

/** 服务端还在跑的这一轮，它在哪一步 → 这一页的相位和进度 */
function livePhase(job: ChatTurnJob): { phase: Phase; status: string } {
  if (job.stage === 'waiting') return { phase: 'waiting', status: PHASE_TEXT.waiting }
  if (job.stage === 'run') return { phase: 'running', status: PHASE_TEXT.running }
  if (job.stage === 'review') return { phase: 'running', status: REVIEWING }
  return { phase: 'planning', status: PHASE_TEXT.planning }
}

function restoreTurn(t: ConversationTurn): ChatTurn {
  const meta = metaOf(t)
  const review = reviewOf(t)
  const runId = meta && 'runId' in meta ? meta.runId ?? null : t.run_id ?? null
  // 老数据没有 meta：停止写的是 status=error + 「已取消」
  const outcome = meta?.outcome ?? (t.status === 'error' && t.error === '已取消' ? 'cancelled' : undefined)
  const answer = t.answer ?? ''
  // 老轮次记的是翻好的 failure；服务端写的记原因（cause）；再老的数据两样都没有，error 列里是原始异常：有运行的按运行失败解释
  const failure = meta?.failure
    ?? (meta?.cause ? causeFailure(meta.cause, meta.scope) : null)
    ?? (t.error && t.error !== '已取消' ? (runId ? runFailure(t.error) : failureOf(t.error)) : null)
  const live = t.status === 'running' && t.job && LIVE_JOB.has(t.job.status) ? livePhase(t.job) : null

  let phase: Phase
  let status: string
  if (live) {
    // 服务端还在跑：hydrate 从这一次尝试的第一条事件接着播
    ;({ phase, status } = live)
  } else if (t.status === 'running') {
    // 上次停在半路（服务重启后没接着收尾、或者这一波之前页面推的老轮次）：有运行就请服务端接着收尾；没有运行说明断在
    // 建图阶段，没有断点可续
    phase = runId ? 'checking' : 'error'
    status = runId ? PHASE_TEXT.checking : PHASE_TEXT.error
  } else if (t.status === 'error') {
    phase = outcome ?? 'error'
    status = PHASE_TEXT[phase]
  } else if (t.graph && !runId && (!answer || meta?.asked)) {
    phase = 'ready'   // 建好了没跑：刷新之后「跑一下」还得在
    status = PHASE_TEXT.ready
  } else {
    phase = 'done'
    status = PHASE_TEXT.done
  }

  const output: Record<string, any> | null = answer
    ? { answer, ...(meta?.issuance ? { _issuance: meta.issuance } : {}) }
    : null
  const graphful = !!t.graph && ((t.graph as GraphSpec).nodes?.length ?? 0) > 0
  const rejected = phase === 'cancelled' ? rejectedOf(meta?.rejected) : null

  return {
    id: t.id,
    serverId: t.id,
    question: t.question,
    phase,
    status: phase === 'cancelled' && rejected ? REJECTED_TEXT.title : status,
    thinking: '',
    ops: [],
    events: [],
    graph: (t.graph as GraphSpec) ?? null,
    explanation: t.explanation ?? '',
    // 先给个壳：run_class 从 meta 来；usage、完整成果由 hydrate 回源补
    run: runId ? ({ id: runId, run_class: meta?.runClass } as Run) : null,
    output,
    error: phase === 'error' ? (failure ? lineOf(failure) : t.status === 'running' ? BUILD_INTERRUPTED.title : '') : '',
    failure: phase === 'error' ? (t.status === 'running' ? BUILD_INTERRUPTED : failure) : phase === 'suspended' ? SUSPENDED : null,
    rejected,
    startedAt: Date.parse(t.created_at ?? '') || Date.now(),
    endedAt: meta?.ms != null ? (Date.parse(t.created_at ?? '') || 0) + meta.ms : undefined,
    lastSeq: 0,
    cancel: null,
    runStatus: meta?.runStatus,
    final: meta?.runStatus ? { status: meta.runStatus } : undefined,
    // 没有运行、没有图、却有答案：这一条是 reply，根据前几轮说的，没查库。
    // 这是这条路径上最硬的一条约定，老数据也要标出来
    noQuery: meta?.noQuery ?? (!meta?.asked && t.status === 'done' && !t.run_id && !t.graph && !!answer),
    pendingRun: phase === 'ready',
    restored: true,
    steps: 'idle',
    // 复核结论是答案的一部分。刷新之后只剩一个看起来很完整的答案、
    // 而"它哪里不可靠"没了，比一开始就不复核更糟
    review: review && review.verdict !== 'ok' ? review : null,
    reviewRaw: review,
    rawOutput: review?.original ? { answer: review.original } : null,
    attempts: attemptsOf(meta?.attempts),
    // 上次记下没取全的：运行还在就等 hydrate 补，运行没了就补不回来了。
    // 老数据没记过，只能看是不是恰好切在 2000 字——运行也删了的，至少别装作完整
    clipped: meta?.clipped === 'partial' ? (runId ? 'partial' : 'lost')
      : meta?.clipped === 'lost' ? 'lost'
      : !runId && graphful && atClip(answer) && !review?.answer ? 'lost' : undefined,
    meta,
    scope: meta?.scope,
    asked: meta?.asked && answer ? answer : undefined,
    usage: meta?.usage,
    job: t.job ?? null,
  }
}

/** 恢复出来的这一轮要不要马上向后端核对（而不是等它滚进视口） */
function needsEagerHydrate(t: ChatTurn): boolean {
  // 服务端还在跑：马上接着播
  if (t.job && LIVE_JOB.has(t.job.status) && BUSY_PHASES.has(t.phase)) return true
  if (t.phase === 'waiting' && t.job && LIVE_JOB.has(t.job.status)) return true
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

function findTurn(conversationId: string, turnId: string): ChatTurn | undefined {
  return useChat.getState().byConversation[conversationId]?.find((t) => t.id === turnId)
}

/**
 * 在回收站里的会话只能翻看。从这里发起的运行左栏找不到、没人看着，却照样计费；
 * 跑着的时候又不能彻底删除。按钮已经按这个关掉了，这里再守一道
 */
const inTrash = (conversationId: string) =>
  useConversations.getState().trash.some((c) => c.id === conversationId)

// -------------------------------------------------------------------------
// 这一页上的「当前这一次」：播放这一轮的事件日志、接运行的实时流
// -------------------------------------------------------------------------

/**
 * 一轮里「当前这一次」在这一页上开着的流。
 *
 * 停止、重试、重新接流都会换一个 epoch，旧的流之后到的东西一律作废：重试之后，上一次那条流晚到的事件不能混进新的一次里
 */
interface Flow {
  conversationId: string
  turnId: string
  patch: Patch
  alive: () => boolean
}

const epochs = new Map<string, number>()

function flowOf(conversationId: string, turnId: string): Flow {
  const epoch = (epochs.get(turnId) ?? 0) + 1
  epochs.set(turnId, epoch)
  const alive = () => epochs.get(turnId) === epoch
  const patch = patcher(useChat.setState as SetState, conversationId, turnId)
  return { conversationId, turnId, alive, patch: (fn) => { if (alive()) patch(fn) } }
}

/** 让这一轮手上所有的旧流程作废 */
const retire = (turnId: string) => epochs.set(turnId, (epochs.get(turnId) ?? 0) + 1)

/** 正在播放的轮次：能从外面让它重新接上运行的实时流（审批过了） */
interface Player {
  flow: Flow
  openRun: (runId: string, from?: number) => void
  closeRun: () => void
}
const players = new Map<string, Player>()

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
      s.byConversation[conversationId]?.forEach((t) => { retire(t.id); t.cancel?.(); players.delete(t.id) })
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
      // 内存里已有的那几轮没有并进来，下面对它们的 hydrate 按 id 找不到，自然跳过
      for (const t of restored) {
        if (needsEagerHydrate(t)) void get().hydrate(conversationId, t.id)
      }
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
    useConversations.getState().touch(conversationId, question)
    void launchTurn(flowOf(conversationId, id), {
      conversation_id: conversationId, question,
      ...(scope.length ? { datasource_ids: scope.map((d) => d.id) } : {}),
    })
    return true
  },

  stop: (conversationId) => {
    // 只停这个会话里占着的那一轮。等审批的不算：它们是等着用户回来处理的，
    // 不是卡住的，误杀了就得重来
    const target = busyTurn(get().byConversation[conversationId])
    if (!target) return
    retire(target.id)
    target.cancel?.()
    players.delete(target.id)
    // 上面只是把自己这头的流关了。这一轮在服务端跑，断开订阅不会让它停：停止要告诉服务端，它连同运行一起停。
    // 发起请求还在路上的（没有 serverId），launchTurn 拿到 id 后看到这一轮已作废，会补一个停止过去
    if (target.serverId) void api.chatTurns.cancel(target.serverId).catch(() => undefined)   // 已经结束会回 409，无妨
    patcher(set, conversationId, target.id)(() => ({
      phase: 'cancelled', status: PHASE_TEXT.cancelled, cancel: null, error: '',
      failure: null, endedAt: Date.now(), writing: null,
      ...(target.run ? { runStatus: 'cancelled', final: { status: 'cancelled' } } : {}),
    }))
  },

  runNow: (conversationId, turnId) => {
    const turn = findTurn(conversationId, turnId)
    if (!turn?.graph || !turn.serverId || isBusy(get().byConversation[conversationId]) || inTrash(conversationId)) return
    const flow = flowOf(conversationId, turnId)
    flow.patch(() => ({
      phase: 'running', status: PHASE_TEXT.running, pendingRun: false,
      startedAt: Date.now(), endedAt: undefined,
    }))
    void launchTurn(flow, { conversation_id: conversationId, retry_of: turn.serverId, mode: 'run' })
  },

  continueTurn: async (conversationId, turnId) => {
    // 从断点接着跑。**不带图**——问数据页的用户改不了图，这里就是"用原来的
    // 配置重试失败的那一步"，对超时、限流、网络抖、服务重启这类瞬时失败有用。
    // 配置本身错了（模型 id 写错、缺必填输入）要到画布上改，那边的「接着跑」
    // 会把改过的图一起带上。接上之后服务端照常等它跑完、复核、收尾
    const turn = findTurn(conversationId, turnId)
    if (!turn?.run || !turn.serverId || isBusy(get().byConversation[conversationId]) || inTrash(conversationId)) return false
    try {
      const res = await api.chatTurns.start({ conversation_id: conversationId, retry_of: turn.serverId, mode: 'continue' })
      const flow = flowOf(conversationId, turnId)
      flow.patch(() => ({ phase: 'running', status: PHASE_TEXT.running, error: '', failure: null, job: res.turn?.job ?? null }))
      play(flow, turn.serverId, Math.max(0, (res.turn?.job?.attempt_seq ?? 1) - 1))
      return true
    } catch (e) {
      // 409 的 detail 是人话（「这次运行已取消，不能接着跑…」），交给调用方弹出来。
      // 顺手再核对一次：按钮是照着过时的状态给的，别让它留在那里被点第二次
      patcher(set, conversationId, turnId)(() => ({ hydrated: undefined }))
      void get().hydrate(conversationId, turnId)
      throw e
    }
  },

  retryTurn: (conversationId, turnId, opts) => {
    const turns = get().byConversation[conversationId]
    const turn = turns?.find((t) => t.id === turnId)
    if (!turn || isBusy(turns) || inTrash(conversationId)) return
    turn.cancel?.()
    const flow = flowOf(conversationId, turnId)
    const base = turn.graph && (turn.graph.nodes?.length ?? 0) > 0
    const mode = base && opts?.maxSteps ? 'max_steps' as const : base && opts?.rerun ? 'rerun' as const : 'retry' as const
    const scope = opts?.scope ?? turn.scope
    // 先把相位摆出来；上一次怎么归档、这一次从哪开始，等服务端的 turn.attempt 来了照它重置
    flow.patch(() => ({
      phase: mode === 'retry' ? 'planning' : 'running', status: retryStatus(mode, opts?.maxSteps),
      failure: null, error: '', pendingRun: false, hydrated: 'done', endedAt: undefined, cancel: null,
      startedAt: Date.now(), writing: null, scope: scope?.length ? scope : undefined,
    }))
    if (!turn.serverId) {
      // 发起那一下就没到服务端（这一轮在库里不存在）：原样再问一次
      void launchTurn(flow, {
        conversation_id: conversationId, question: turn.question,
        ...(scope?.length ? { datasource_ids: scope.map((d) => d.id) } : {}),
      })
      return
    }
    void launchTurn(flow, {
      conversation_id: conversationId, retry_of: turn.serverId, mode,
      ...(mode === 'max_steps' ? { max_steps: opts!.maxSteps } : {}),
      ...(opts?.scope ? { datasource_ids: opts.scope.map((d) => d.id) } : {}),
    })
  },

  hydrate: async (conversationId, turnId) => {
    const turn = findTurn(conversationId, turnId)
    if (!turn || turn.hydrated) return
    const patch = patcher(set, conversationId, turnId)
    // 服务端还在跑这一轮（刷新、关掉再打开）：从这一次尝试的第一条事件接着播
    if (turn.serverId && turn.job && LIVE_JOB.has(turn.job.status)) {
      patch(() => ({ hydrated: 'done' }))
      play(flowOf(conversationId, turnId), turn.serverId, Math.max(0, turn.job.attempt_seq - 1))
      return
    }
    // 库里那一行停在半路、服务端又没在跑它（服务重启后没接着收尾、这一波之前页面推的老轮次）：请服务端接着等那次运行、
    // 照常复核收尾，不在这一页上自己收尾
    if (turn.phase === 'checking' && turn.serverId && turn.run?.id) {
      patch(() => ({ hydrated: 'pending' }))
      await resumeTurn(conversationId, turnId)
      return
    }
    // 重新核对一轮还没有运行的（实时流断在建图中、读不到了）：问服务端它现在怎样，在跑就接着播，收了尾就照那一行摆
    if (turn.phase === 'checking' && turn.serverId) {
      patch(() => ({ hydrated: 'pending' }))
      try {
        const res = await api.chatTurns.get(turn.serverId)
        if (res.job && LIVE_JOB.has(res.job.status)) {
          patch(() => ({ hydrated: 'done', job: res.job }))
          play(flowOf(conversationId, turnId), turn.serverId, Math.max(0, res.job.attempt_seq - 1))
        } else {
          patch((t) => ({ ...endPatch(t, { turn: res.turn }), hydrated: 'done' }))
        }
      } catch (e) {
        const failure: Failure = {
          title: PHASE_TEXT.unknown,
          reason: `查询本轮运行状态时出错（${errorMessage(e)}）。运行可能仍在进行，也可能已结束。`,
          hint: '请稍后点击「重新核对」；服务端重新连接后会自动再核对一次。',
        }
        patch(() => ({ hydrated: undefined, phase: 'unknown', status: PHASE_TEXT.unknown, failure, error: lineOf(failure) }))
      }
      return
    }
    const runId = turn.run?.id
    if (!runId) return
    // 核对期间用户点了重试或停止：拿回来的是上一次的状态，作废
    const epoch = epochs.get(turnId) ?? 0
    const stale = () => (epochs.get(turnId) ?? 0) !== epoch
    patch(() => ({ hydrated: 'pending' }))
    let run: Run
    try {
      run = await api.runs.get(runId)
    } catch (e) {
      const cur = findTurn(conversationId, turnId)
      if (stale() || !cur) return
      if (e instanceof ApiError && e.status === 404) {
        // 没取全的答案，运行又删了：补不回来，但得说出来
        const lost = cur.phase === 'done' && !cur.review?.answer
          && (cur.clipped === 'partial' || atClip(String(cur.output?.answer ?? '')))
        patch((t) => ({ hydrated: 'done', runStatus: 'gone', clipped: lost ? 'lost' : t.clipped }))
      } else {
        patch(() => ({ hydrated: undefined }))   // 已经有个结局摆着，下次再补
      }
      return
    }
    const pending = run.status === 'interrupted' ? await pendingApproval(run.id) : false
    const cur = findTurn(conversationId, turnId)
    if (!cur || stale()) return
    patch(() => ({ run, runStatus: run.status, final: { status: run.status, pending }, hydrated: 'done' }))
    const settled = cur.phase === 'suspended' || cur.phase === 'error'
    // 已经收过尾的这一轮其实又活了（别处接着跑了、通过了审批、跑完了）：请服务端接着等它、照常复核收尾
    const revived = run.status === 'running' || run.status === 'queued' || (run.status === 'interrupted' && pending)
      || run.status === 'succeeded'
    if (settled && revived && cur.serverId) {
      await resumeTurn(conversationId, turnId)
      return
    }
    if (run.status !== 'succeeded' || cur.phase !== 'done') return
    // 已经交付过的：答案没取全就换成完整的（老轮次），顺手把档位补回来
    const full = answerText(run.output)
    const stored = String(cur.output?.answer ?? '')
    const repaired = !cur.review?.answer && stored.length >= 200
      && full.length > stored.length && full.startsWith(stored.slice(0, 200))
    const issuance = slimIssuance(run.output?._issuance)
    if (repaired) {
      patch(() => ({ output: run.output, clipped: undefined }))
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
    // 这一页正在播这一轮：审批过了，服务端接着等那次运行，这里把运行的实时流重新接上
    const player = players.get(turnId)
    if (player?.flow.alive()) {
      player.openRun(turn.run.id, turn.lastSeq)
      return
    }
    // 没在播（老轮次、刷新前停在审批上的）：请服务端接着等这次运行、照常收尾
    if (turn.serverId) await resumeTurn(conversationId, turnId)
  },
}))

if (import.meta.env.DEV) {
  ;(window as any).__chat = useChat
}

/** 用户点重试时先摆出来的那一句（服务端的 turn.attempt 来了照它再说一遍） */
function retryStatus(mode: 'retry' | 'rerun' | 'max_steps' | string, maxSteps?: number): string {
  return mode === 'max_steps' ? `正在将步数上限放宽到 ${maxSteps} 并重新运行…`
    : mode === 'rerun' ? '正在重新运行本轮…'
    : mode === 'retry' ? '正在按原问题重新生成…'
    : mode === 'run' || mode === 'continue' || mode === 'resume' ? PHASE_TEXT.running
    : PHASE_TEXT.planning
}

/**
 * 发起一轮（新问、重试本轮、重新运行、跑一下），拿到 id 就开始播。发起那一下就被拒（409 这段对话正忙、网络断了……）：
 * 说清楚，给「重试本轮」——服务端没有这一轮的话就原样再问一次
 */
async function launchTurn(flow: Flow, body: ChatTurnStart) {
  try {
    const res = await api.chatTurns.start(body)
    // 发起请求在路上时用户点了停止或重试：服务端那一轮已经起来了，得追一个停止过去
    if (!flow.alive()) {
      void api.chatTurns.cancel(res.turn_id).catch(() => undefined)
      return
    }
    flow.patch(() => ({ serverId: res.turn_id, job: res.turn?.job ?? null }))
    play(flow, res.turn_id, Math.max(0, (res.turn?.job?.attempt_seq ?? 1) - 1))
  } catch (e) {
    if (!flow.alive()) return
    const failure = failureOf(e)
    flow.patch(() => ({
      phase: 'error', status: PHASE_TEXT.error, error: lineOf(failure), failure, cancel: null, endedAt: Date.now(),
    }))
  }
}

/**
 * 请服务端接着等这一轮的那次运行、照常复核收尾（mode=resume），再接着播。核对撞上后端出错、超时：不知道它现在怎样，
 * 但不能一直停在「正在核对」转圈——说清楚，给「重新核对」
 */
async function resumeTurn(conversationId: string, turnId: string) {
  const turn = findTurn(conversationId, turnId)
  if (!turn?.serverId) return
  const flow = flowOf(conversationId, turnId)
  try {
    const res = await api.chatTurns.start({ conversation_id: conversationId, retry_of: turn.serverId, mode: 'resume' })
    if (!flow.alive()) return
    flow.patch(() => ({ hydrated: 'done', job: res.turn?.job ?? null }))
    play(flow, turn.serverId, Math.max(0, (res.turn?.job?.attempt_seq ?? 1) - 1))
  } catch (e) {
    if (!flow.alive()) return
    // 别处（另一个标签页）刚请服务端接着收尾了这一轮：不再起一次，接着看它（服务端在跑就从这一次尝试的第一条播，收了尾就照那一行摆）
    if (e instanceof ApiError && e.status === 409 && e.code === 'chat_turn_busy') {
      try {
        const res = await api.chatTurns.get(turn.serverId)
        if (!flow.alive()) return
        if (res.job && LIVE_JOB.has(res.job.status)) {
          flow.patch(() => ({ hydrated: 'done', job: res.job }))
          play(flow, turn.serverId, Math.max(0, res.job.attempt_seq - 1))
          return
        }
        if (res.turn && res.turn.status !== 'running') {
          flow.patch((t) => ({ ...endPatch(t, { turn: res.turn }), hydrated: 'done' }))
          return
        }
      } catch { /* 问不到就照下面说没核对上 */ }
      if (!flow.alive()) return
    }
    const said = errorMessage(e)
    const raw = humanizeError(e).raw
    const failure: Failure = {
      title: PHASE_TEXT.unknown,
      reason: `查询本轮运行状态时出错（${said}）。运行可能仍在进行，也可能已结束。`,
      hint: '请稍后点击「重新核对」；服务端重新连接后会自动再核对一次。',
      // 原话已经在括号里的，技术细节不再重复一遍
      detail: raw && !said.includes(raw) ? raw : undefined,
    }
    flow.patch(() => ({
      hydrated: undefined, phase: 'unknown', status: PHASE_TEXT.unknown, failure, error: lineOf(failure),
    }))
  }
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

// -------------------------------------------------------------------------
// 播放：服务端这一轮的事件日志 → 这一轮的样子
// -------------------------------------------------------------------------

/** 这一次尝试开始了（turn.attempt）：上一次的过程、答案、复核都清掉，归档照服务端给的 */
function attemptReset(t: ChatTurn, d: Record<string, any>, ts: number): Partial<ChatTurn> {
  const mode = String(d.mode ?? 'ask')
  const attempts = attemptsOf(d.attempts) ?? (mode === 'run' || mode === 'continue' || mode === 'resume' ? t.attempts : undefined)
  const scope = Array.isArray(d.scope) && d.scope.length ? d.scope as ScopeSource[] : t.scope
  const usage = d.usage && typeof d.usage === 'object' ? d.usage as TurnUsage : t.usage
  const startedAt = typeof d.started_ms === 'number' ? d.started_ms : ts ? ts * 1000 : Date.now()
  if (mode === 'continue' || mode === 'resume') {
    // 接着等同一次运行：过程照旧留着，运行的流接着上次收到的地方。运行早就跑完了的（补交付）停在「正在核对」、计时停在
    // 运行结束的那一刻：按实时算会从提问那天一路数到现在（「72:00:01」）
    const ended = typeof d.ended_ms === 'number' ? d.ended_ms : undefined
    return {
      phase: ended ? 'checking' : mode === 'resume' && t.phase === 'waiting' ? 'waiting' : 'running',
      status: ended ? REVIEWING : PHASE_TEXT.running,
      error: '', failure: null, final: undefined, endedAt: ended, writing: null, startedAt, attempts, scope, usage,
      hydrated: 'done',
    }
  }
  if (mode === 'run') {
    // 只建了图、用户点「运行」：建图的过程照旧留着，只换上这一次的运行
    return {
      phase: 'running', status: PHASE_TEXT.running, attempts, scope, usage,
      events: [], run: null, output: null, review: null, reviewRaw: null, rawOutput: null, failure: null, error: '',
      lastSeq: 0, final: undefined, rejected: null, runStatus: undefined, pendingRun: false, hydrated: 'done',
      startedAt, endedAt: undefined, writing: null, noQuery: false, restored: false, steps: undefined,
    }
  }
  return {
    phase: mode === 'ask' || mode === 'retry' ? 'planning' : 'running',
    status: mode === 'ask' ? PHASE_TEXT.planning : retryStatus(mode, Number(d.max_steps) || undefined),
    attempts, scope, usage,
    ops: [], events: [], run: null, output: null, review: null, reviewRaw: null,
    rawOutput: null, failure: null, error: '', lastSeq: 0, final: undefined, rejected: null,
    runStatus: undefined, clipped: undefined, pendingRun: false, steps: undefined,
    hydrated: 'done', startedAt, endedAt: undefined, attempt: 0,
    noQuery: false, thinking: '', writing: null, asked: undefined,
    // 刷新后接着播的那一轮就是正在跑的一轮：过程照实时的样子一直摆着（不是从库里恢复、要点开才取的历史轮次）
    restored: false,
  }
}

/** 助手的一条操作：原样收下（思考增量并进上一条），照原来的样子更新进度。要不要运行、要不要重来都等服务端说 */
function opPatch(t: ChatTurn, op: CopilotOp): Partial<ChatTurn> {
  const last = t.ops[t.ops.length - 1]
  const ops = op.op === 'thinking' && last?.op === 'thinking'
    ? [...t.ops.slice(0, -1), { ...last, delta: String(last.delta ?? '') + String(op.delta ?? '') }]
    // 心跳只说「还在等」：接着的一拍顶替上一拍（重连时服务端会补最近的一拍，chat/log.beat），不叠成好几条
    : op.op === 'heartbeat' && last?.op === 'heartbeat' ? [...t.ops.slice(0, -1), op]
      : [...t.ops, op]
  switch (op.op) {
    case 'thinking':
      return { ops, phase: 'planning', thinking: (t.thinking + (op.delta ?? '')).slice(-2000) }
    case 'heartbeat':
      // 查看进行中（looking）不是这一页的阶段：浮条直说「正在查看表结构…」
      return { ops, status: op.phase === 'looking' ? LOOK_TEXT.running
        : PHASE_TEXT[(op.phase as Phase) ?? 'planning'] ?? '正在处理…' }
    case 'look':
    case 'context': {
      // 查看结束：浮条换成查看了什么
      const look = op.op === 'look' ? copilotLook(op) : copilotContext(op)
      return look ? { ops, status: look.title } : { ops }
    }
    case 'plan':
      return { ops, phase: 'building', status: op.summary || '正在生成工作流…' }
    case 'add_node':
      return { ops, phase: 'building', status: `正在生成工作流…（${ops.filter((o) => o.op === 'add_node').length} 步）` }
    case 'use_caliber':
      // 助手选用了一份已发布口径（取数节点和口径卡由服务端生成，过程里那一行由 decodeCopilot 写）
      return { ops, phase: 'building', status: LEDGER_TEXT.use.status }
    case 'done':
      return { ops, explanation: op.explanation ?? '' }
    case 'ask': {
      // 助手要先问清楚才能动手（N11）：记下问题。这一轮怎么收尾（反问、摆着等运行）等服务端的 end
      const asked = String(op.question ?? '').trim()
      return asked ? { ops, status: CHAT_BLUEPRINT_TEXT.asked, asked } : { ops }
    }
    case 'final':
      // 图建好了：先摆出来（画布边收边长到这里收尾），跑不跑由服务端定（stage run / end ready / end error）
      return { ops, ...(op.graph ? { graph: op.graph as GraphSpec } : {}), explanation: op.explanation ?? t.explanation }
    case 'usage': {
      // 这一轮助手建图花了多少（服务端 answer_ops 最后一条）：累加进这一轮的用量
      const base = t.usage ?? t.meta?.usage
      return { ops, usage: { ...base, copilot: addUsage(base?.copilot, op as UsageTotal) } }
    }
  }
  return { ops }
}

/** 自动重来一次（turn.retry）：这一次的过程和答案清掉（它是自动补救，用户还没拿到过答案），换下来的运行的用量并进这一轮 */
function retryPatch(t: ChatTurn, d: Record<string, any>): Partial<ChatTurn> {
  const status = d.kind === 'empty' ? CHAT_RETRY_TEXT.empty
    : d.base ? '上次运行未成功，正在调整工作流…' : '上次运行未成功，正在重新生成工作流…'
  return {
    attempt: Number(d.attempt ?? (t.attempt ?? 0) + 1), phase: 'planning', status,
    ops: [], events: [], run: null, output: null, final: undefined, runStatus: undefined,
    review: null, reviewRaw: null, rawOutput: null, lastSeq: 0, failure: null, error: '',
    // 服务端带着换下来的那次运行的用量（页面上的运行壳子里没有用量）；老服务端没带就按手上的算
    usage: d.usage && typeof d.usage === 'object' ? d.usage as TurnUsage : retireRun(t),
  }
}

/**
 * 这一轮到此为止（end）：照服务端落库的那一行摆结局（和刷新之后从库里恢复的是同一份），过程留着。跑完了的成果用 end 带来的
 * 完整那份（报告的逐段证据在里面）；复核改写了的照那一行
 */
function endPatch(t: ChatTurn, d: Record<string, any>): Partial<ChatTurn> {
  const row = d.turn as ConversationTurn | null
  if (!row) return { cancel: null, writing: null, endedAt: Date.now() }
  const r = restoreTurn(row)
  const run = d.run && typeof d.run === 'object' ? d.run as Partial<Run> & { id: string } : null
  const output = d.output && typeof d.output === 'object' && r.phase === 'done' && !r.review?.answer && !r.noQuery && !r.asked
    ? d.output as Record<string, any> : r.output
  return {
    phase: r.phase === 'checking' ? 'unknown' : r.phase,
    status: r.status, error: r.error, failure: r.failure, rejected: r.rejected,
    review: r.review, reviewRaw: r.reviewRaw, rawOutput: r.rawOutput, output,
    meta: r.meta, usage: r.meta?.usage ?? t.usage, noQuery: r.noQuery, asked: r.asked, pendingRun: r.pendingRun,
    graph: r.graph ?? t.graph, explanation: r.explanation || t.explanation, attempts: r.attempts,
    run: run ? ({ ...(t.run ?? {}), ...run } as Run) : r.run ? (t.run?.id === r.run.id ? t.run : r.run) : null,
    runStatus: run?.status ?? r.runStatus,
    final: run?.status ? { status: run.status } : r.final ?? t.final,
    // 计时停在服务端记的那一刻（补交付的停在运行结束时）
    clipped: undefined, writing: null, endedAt: r.meta?.ms != null ? t.startedAt + r.meta.ms : Date.now(),
    job: row.job ?? null,
  }
}

/**
 * 播放这一轮在服务端的事件日志（发起 + 播放）。after 是从哪一条之后开始（这一次尝试的第一条之前）；断线时 streamChatTurn
 * 按收到的最后一个 seq 自动重连、补齐。收到发起了运行（run）就接上运行自己的实时流画过程；运行停在审批上、审批过了接着跑
 * 时（stage waiting → run）重新接上
 */
function play(flow: Flow, serverId: string, after: number) {
  const { patch } = flow
  players.get(flow.turnId)?.closeRun()
  let watching: { runId: string; stop: () => void } | null = null
  const openRun = (runId: string, from = 0) => {
    if (!flow.alive()) return
    if (watching?.runId === runId) return
    watching?.stop()
    watching = { runId, stop: watch(flow, runId, from) }
  }
  const closeRun = () => {
    watching?.stop()
    watching = null
  }
  /** 运行停在审批上：运行的实时流会自己结束（stream.end interrupted），审批过了要重新接 */
  const parked = () => {
    const w = watching
    watching = null
    w?.stop()
  }

  const apply = (e: ChatTurnEvent) => {
    const d = e.data ?? {}
    switch (e.type) {
      case 'turn.attempt':
        closeRun()
        patch((t) => ({ ...attemptReset(t, d, e.ts), turnSeq: e.seq }))
        if ((d.mode === 'continue' || d.mode === 'resume')) {
          const runId = findTurn(flow.conversationId, flow.turnId)?.run?.id
          if (runId) openRun(runId, findTurn(flow.conversationId, flow.turnId)?.lastSeq ?? 0)
        }
        return
      case 'op':
        // 心跳不带 seq（不落库，chat/log.beat）：不动收到的最后一个 seq
        patch((t) => ({ ...opPatch(t, d as CopilotOp), ...(typeof e.seq === 'number' ? { turnSeq: e.seq } : {}) }))
        return
      case 'stage': {
        const stage = String(d.stage ?? '')
        patch((t) => {
          // 补交付（运行早就跑完了）停在「正在核对」，不按实时运行画
          if (t.phase === 'checking' && t.endedAt) return { turnSeq: e.seq, ...(stage === 'review' ? { runStatus: 'succeeded' } : {}) }
          switch (stage) {
            case 'build':
              return { turnSeq: e.seq, ...(t.phase === 'planning' || t.phase === 'building' ? {} : { phase: 'planning' as Phase }) }
            case 'run':
              return { turnSeq: e.seq, phase: 'running' as Phase, final: undefined,
                       ...(t.phase !== 'running' ? { status: PHASE_TEXT.running } : {}) }
            case 'waiting':
              return { turnSeq: e.seq, phase: 'waiting' as Phase, status: PHASE_TEXT.waiting,
                       final: { status: 'interrupted', pending: true }, runStatus: 'interrupted' }
            case 'review':
              return { turnSeq: e.seq, phase: 'running' as Phase, status: REVIEWING, runStatus: 'succeeded' }
          }
          return { turnSeq: e.seq }
        })
        if (stage === 'waiting') {
          parked()
          // 审批列表平时 4 秒才轮询一次，头部已经写着「等待审批」，通过/驳回的按钮却要晚几秒才出现——这一下立刻去拿
          void useCatalog.getState().refreshApprovals()
        }
        if (stage === 'run') {
          const cur = findTurn(flow.conversationId, flow.turnId)
          if (cur?.run?.id) openRun(cur.run.id, cur.lastSeq)
        }
        return
      }
      case 'run': {
        const runId = String(d.run_id ?? '')
        if (!runId) return
        patch((t) => ({
          turnSeq: e.seq, phase: 'running', status: PHASE_TEXT.running, runStatus: String(d.status ?? 'queued'),
          run: t.run?.id === runId ? t.run : ({ id: runId, status: d.status, run_class: d.run_class, output: {}, usage: {} } as unknown as Run),
          ...(t.run?.id === runId ? {} : { events: [], lastSeq: 0 }),
        }))
        openRun(runId, 0)
        return
      }
      case 'review':
        // 复核这一次的用量也是这一轮的花费；结论跟着 end 一起摆出来（复核说明和结论同时出现，而不是结论先读完）
        patch((t) => {
          const base = t.usage ?? t.meta?.usage
          return { turnSeq: e.seq, usage: { ...base, review: addUsage(base?.review, d.result?.usage) } }
        })
        return
      case 'review.late':
        // 等不及的那次复核晚到了：答案已经照原样交了，它的用量照样记进这一轮
        patch((t) => {
          const base = t.usage ?? t.meta?.usage
          return { turnSeq: e.seq, usage: { ...base, review: addUsage(base?.review, d.usage) },
                   meta: t.meta ? { ...t.meta, usage: { ...base, review: addUsage(base?.review, d.usage) } } : t.meta }
        })
        return
      case 'turn.retry':
        closeRun()
        patch((t) => ({ ...retryPatch(t, d), turnSeq: e.seq }))
        return
      case 'end':
        patch((t) => ({ ...endPatch(t, d), turnSeq: e.seq }))
        if (d.outcome === 'cancelled' || d.outcome === 'suspended' || d.outcome === 'error') {
          // 运行已经停了：它的实时流会把最后几条补完再结束；被取消的不再看
          if (d.outcome === 'cancelled') closeRun()
        }
        return
    }
  }

  const stopTurn = streamChatTurn(serverId, after, (e) => {
    if (!flow.alive()) return
    apply(e)
  }, (end) => {
    // 服务端说这一轮的事件都发完了（或者这一轮读不了了）。运行的实时流还开着的让它自己收尾。
    // 正常情况下 end 早就到了；没到（服务端那一轮出了岔子、这一轮被删了、没权限了）就别让它一直转圈：照库里那一行摆，
    // 读不到就照实说没核对上
    const cur = findTurn(flow.conversationId, flow.turnId)
    if (!flow.alive() || !cur || !(BUSY_PHASES.has(cur.phase) || cur.phase === 'checking' || cur.phase === 'waiting')) return
    const unknown = (said: string) => {
      const failure: Failure = {
        title: PHASE_TEXT.unknown,
        reason: `查询本轮运行状态时出错（${said}）。运行可能仍在进行，也可能已结束。`,
        hint: '请稍后点击「重新核对」；服务端重新连接后会自动再核对一次。',
      }
      patch(() => ({ phase: 'unknown', status: PHASE_TEXT.unknown, failure, error: lineOf(failure), cancel: null,
                     writing: null, hydrated: undefined }))
    }
    if (end.error) return unknown(end.error)
    void api.chatTurns.get(serverId).then((res) => {
      if (!flow.alive()) return
      if (res.turn && res.turn.status !== 'running') patch((t) => endPatch(t, { turn: res.turn }))
      else if (!res.job || !LIVE_JOB.has(res.job.status)) unknown(PHASE_TEXT.unknown)
    }, (e) => { if (flow.alive()) unknown(errorMessage(e)) })
  })
  const close = () => {
    stopTurn()
    closeRun()
  }
  players.set(flow.turnId, { flow, openRun, closeRun })
  patch(() => ({ cancel: close }))
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
 * 订阅运行事件，只为画过程：把事件原样存进这一轮，顺带更新「此刻在做什么」那一句和出字计数。
 * "查了哪张表、跑了什么 SQL"不在这里翻译——decodeRun 会从同一批事件里读出来。
 *
 * 这一轮的相位和结局不看这里：跑完了要复核、要不要重来、什么时候交付都是服务端定的，经这一轮的事件日志来（play）。
 * 只有停在审批上那一下例外：run.interrupted 一到就去拿待审批（头部写着「等待审批」，通过 / 驳回的按钮不该晚几秒才出现）
 */
function watch(flow: Flow, runId: string, after = 0): () => void {
  const { patch } = flow
  let buffer: RunEvent[] = []
  const batch = batcher(() => {
    if (!buffer.length) return
    const got = buffer
    buffer = []
    patch((t) => (t.run?.id !== runId ? {} : {
      events: mergeEvents(t.events, got),
      // 记下进度：审批恢复要重新接流，带上它才不会把历史再收一遍
      lastSeq: Math.max(t.lastSeq ?? 0, ...got.map((e) => e.seq ?? 0)),
    }))
  })
  const pen = penOf(patch)

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
    // 补发的历史里的结局是"当时"的结局，不据此改什么
    if (event.replay && REPLAY_SKIP.has(event.type)) return
    switch (event.type) {
      case 'tool.start': {
        const tool = String(d.tool ?? '')
        // 分析工具写算子的叫法（契约的叫法，lib/termsAnalysis），不写 analysis_decompose
        const op = analysisToolOp(tool)
        patch((t) => (t.phase !== 'running' ? {} : {
          status: tool.startsWith('db_query') ? '正在查询数据…'
            : tool.startsWith('db_schema') ? '正在读取数据结构…'
            : op ? ANALYSIS_TEXT.step.running(analysisMethodLabel(op, d.args?.mode))
            : tool ? `正在调用 ${tool}…` : '正在运行…',
        }))
        break
      }
      case 'llm.start':
        patch((t) => (t.phase !== 'running' ? {} : { status: '正在分析…' }))
        break
      case 'run.resumed':
        patch(() => ({ final: undefined }))
        break
      case 'run.interrupted':
        patch((t) => (t.phase !== 'running' ? {} : { phase: 'waiting', status: PHASE_TEXT.waiting }))
        void useCatalog.getState().refreshApprovals()
        break
      case 'run.finished':
        patch((t) => (t.phase !== 'running' ? {} : { status: REVIEWING }))
        break
    }
  }, () => {
    batch.now()
    pen.clear()
  }, after)
  return () => {
    batch.now()
    pen.clear()
    stop()
  }
}

export { decodeRun }
