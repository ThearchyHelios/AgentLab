import { create } from 'zustand'
import { api, streamCopilot, streamRun } from '../api/client'
import type { CopilotOp } from '../run/decode'
import { decodeRun } from '../run/decode'
import { useConversations } from './conversations'
import type { GraphSpec, ReviewResult, Run, RunEvent, ValidationIssue } from '../types'

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
 */

export type Phase =
  | 'idle'
  | 'planning'    // Copilot 在想
  | 'building'    // 图在长出来
  | 'running'     // 图在跑
  | 'waiting'     // 停在人工介入
  | 'ready'       // 图建好了，等用户决定跑不跑
  | 'done'
  | 'error'

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
  /** 跑图的原始事件 */
  events: RunEvent[]
  /** Copilot 建出来的图 */
  graph: GraphSpec | null
  /** Copilot 对这张图的说明 */
  explanation: string
  run: Run | null
  /** 最终成果 */
  output: Record<string, any> | null
  error: string
  startedAt: number
  /** 已收到的最大事件 seq。重新接流时要带上，否则后端把历史整条重推 */
  lastSeq: number
  /** 这一轮自己的取消句柄。放 turn 上而不是 store 上：store 单值会被
   *  下一轮 ask 覆盖，上一轮的 WebSocket 就再也关不掉了 */
  cancel: (() => void) | null
  /** 这一轮没有查库，答案是根据前几轮说的。界面上必须标出来 */
  noQuery?: boolean
  /** 图建好了但按模型的意思没有自动跑，等用户点「跑一下」 */
  pendingRun?: boolean
  /** 从库里恢复的历史轮次。它没有事件流，步骤要点开才去取 */
  restored?: boolean
  /** 已经补取过事件（或正在取），别重复拉 */
  stepsLoaded?: boolean
  /** 跑完之后的复核结论。null = 没复核过，和「复核过、没问题」不是一回事 */
  review?: ReviewResult | null
  /** 被复核重写之前的答案。改写是有损的，原件得留着让用户能对照 */
  rawOutput?: Record<string, any> | null
  /** 这一轮重建过几次图。上限 1——第二次还不行多半是问题本身没说清，
   *  该让用户介入，而不是替他一遍遍烧钱 */
  attempt?: number
}

interface ChatState {
  /** 按会话分桶。切走再切回来要能看到原来的步骤，所以内存里有就不回源 */
  byConversation: Record<string, ChatTurn[]>
  busy: boolean
  cancel: (() => void) | null
  loading: boolean

  turnsOf: (conversationId: string | null) => ChatTurn[]
  load: (conversationId: string) => Promise<void>
  ask: (conversationId: string, question: string) => void
  stop: (conversationId: string) => void
  /** 审批完成后重新接上运行 */
  reattach: (conversationId: string, turnId: string) => Promise<void>
  /** 历史轮次点开"执行过程"时才去取事件 */
  loadSteps: (conversationId: string, turnId: string) => Promise<void>
  /** 模型建了图但没自动跑时，用户点「跑一下」 */
  runNow: (conversationId: string, turnId: string) => void
  /** 跑挂了：从失败的那个节点接着跑，前面跑过的不重来 */
  continueTurn: (conversationId: string, turnId: string) => Promise<void>
  /** 会话被删了，把内存里那一桶也丢掉 */
  forget: (conversationId: string) => void
}

let seq = 0
const newId = () => `t${Date.now().toString(36)}${seq++}`

const PHASE_TEXT: Record<Phase, string> = {
  idle: '',
  planning: '正在理解需求…',
  building: '正在搭建流程…',
  running: '正在执行…',
  waiting: '等待你的确认',
  ready: '流程搭好了，没有自动执行',
  done: '完成',
  error: '出错了',
}

const EMPTY: ChatTurn[] = []

/** 落库成果的上限。只是防跑飞，不是显示上限 */
const ANSWER_CAP = 20_000

/** 等复核的上限。超了就先把原答案交出去，说明后补不了就算了 */
const REVIEW_TIMEOUT_MS = 25_000

type Patch = (fn: (t: ChatTurn) => Partial<ChatTurn>) => void

/**
 * 成果 dict 压成一句话，存进 conversation_turns.answer。
 *
 * 它有两个去处：刷新页面后重建界面，以及下一轮拼进 Copilot 的上下文。
 * 后者决定了这里不能只存个 "[object Object]"——模型要靠它知道上一轮
 * 答了什么，才接得住"再按月份拆一下"。
 */
function answerText(output: Record<string, any> | null): string {
  if (!output) return ''
  const parts: string[] = []
  for (const [key, value] of Object.entries(output)) {
    if (key.startsWith('_') || value == null || value === '') continue
    parts.push(typeof value === 'string' ? value : JSON.stringify(value))
  }
  // 落库的是完整报告。之前在 4000 字处无声切断，刷新页面后那份就永久少一截，
  // 而且断点常常落在表格中间。上限只是防跑飞的护栏，真切到了必须说出来——
  // 喂给下一轮的那份另有更狠的截断（HISTORY_ANSWER_CHARS），两回事
  const text = parts.join('\n')
  return text.length > ANSWER_CAP
    ? text.slice(0, ANSWER_CAP) + `\n\n…（成果过长，已截断，完整内容见运行记录）`
    : text
}

export const useChat = create<ChatState>((set, get) => ({
  byConversation: {},
  busy: false,
  cancel: null,
  loading: false,

  turnsOf: (conversationId) =>
    (conversationId && get().byConversation[conversationId]) || EMPTY,

  forget: (conversationId) =>
    set((s) => {
      get().byConversation[conversationId]?.forEach((t) => t.cancel?.())
      const { [conversationId]: _gone, ...rest } = s.byConversation
      return { byConversation: rest }
    }),

  load: async (conversationId) => {
    // 内存里有就用内存里的：那一份带着这次会话跑出来的完整步骤，
    // 回源拿到的只有问题和答案。切走再切回来不该丢掉过程
    if (get().byConversation[conversationId]) return
    set({ loading: true })
    try {
      const detail = await api.conversations.get(conversationId)
      const turns: ChatTurn[] = detail.turns.map((t) => ({
        id: t.id,
        serverId: t.id,
        question: t.question,
        phase: t.status === 'error' ? 'error' : t.status === 'running' ? 'error' : 'done',
        status: t.status === 'error' ? PHASE_TEXT.error : PHASE_TEXT.done,
        thinking: '',
        ops: [],
        events: [],
        graph: (t.graph as GraphSpec) ?? null,
        explanation: t.explanation ?? '',
        // 只留 id：usage / run_class 这些恢复时用不上，真要看详情那一栏
        // 自己会去取。给个空壳比不给强——「看执行过程」全靠它找到那次运行
        run: t.run_id ? ({ id: t.run_id } as Run) : null,
        output: t.answer ? { answer: t.answer } : null,
        // running 说明上次是被强杀/关标签页断在半路的。标成出错而不是留一个
        // 永远转圈的行——它不会再动了，转圈只是在骗人
        error: t.status === 'running' ? '上次没有跑完' : (t.error ?? ''),
        startedAt: Date.parse(t.created_at ?? '') || Date.now(),
        lastSeq: 0,
        cancel: null,
        restored: true,
        stepsLoaded: false,
        // 复核结论是答案的一部分。刷新之后只剩一个看起来很完整的答案、
        // 而"它哪里不可靠"没了，比一开始就不复核更糟
        review: t.review && t.review.verdict !== 'ok' ? (t.review as ReviewResult) : null,
        rawOutput: t.review?.original ? { answer: t.review.original } : null,
      }))
      set((s) => ({
        byConversation: { ...s.byConversation, [conversationId]: turns },
        loading: false,
      }))
    } catch {
      set({ loading: false })
    }
  },

  loadSteps: async (conversationId, turnId) => {
    const turn = get().byConversation[conversationId]?.find((t) => t.id === turnId)
    if (!turn || turn.stepsLoaded) return
    const runId = turn.run?.id
    if (!runId) return
    const patch = patcher(set, conversationId, turnId)
    patch(() => ({ stepsLoaded: true }))
    try {
      const events = await api.runs.events(runId)
      patch(() => ({ events }))
    } catch {
      patch(() => ({ stepsLoaded: false }))   // 取失败就还能再点一次
    }
  },

  ask: (conversationId, question) => {
    const id = newId()
    const patch = patcher(set, conversationId, id)

    set((s) => ({
      busy: true,
      byConversation: {
        ...s.byConversation,
        [conversationId]: [
          ...(s.byConversation[conversationId] ?? []),
          {
            id, serverId: null, question, phase: 'planning', status: PHASE_TEXT.planning,
            thinking: '', ops: [], events: [], graph: null, explanation: '',
            run: null, output: null, error: '', startedAt: Date.now(),
            lastSeq: 0, cancel: null, attempt: 0,
          },
        ],
      },
    }))

    useConversations.getState().touch(conversationId, question)

    // 先开一轮，拿到 serverId 再开始建图。顺序不能反：Copilot 那边要读
    // 这次会话的历史，而"这一轮"必须已经是 running 状态才不会被算进自己的上下文
    const started = api.conversations.startTurn(conversationId, question)
      .then((turn) => { patch(() => ({ serverId: turn.id })); return turn.id })
      .catch(() => null)

    const save = async (body: Record<string, any>) => {
      const serverId = await started
      if (!serverId) return
      try {
        await api.conversations.patchTurn(conversationId, serverId, body)
      } catch { /* 存不下不该影响正在进行的对话 */ }
    }

    plan({
      conversationId, turnId: id, question, instruction: question,
      attempt: 0, patch, set, save,
    })
  },
  stop: (conversationId) => {
    // 只停最后一轮。把所有非终态 turn 一律标成"已取消"会误杀正等人工介入的
    // 历史轮次——那些是等着用户回来处理的，不是卡住的。
    const turns = get().byConversation[conversationId] ?? []
    const last = turns[turns.length - 1]
    last?.cancel?.()
    get().cancel?.()
    // 上面两句只是把自己这头的事件流关了。运行在后端，断开订阅不会让它停：
    // 以前点了「停止」，模型和 SQL 其实还在跑、还在计费，界面上却已经显示"已取消"
    if (last?.run?.id && !['done', 'error', 'waiting'].includes(last.phase)) {
      void api.runs.cancel(last.run.id).catch(() => undefined)   // 已经跑完会回 409，无妨
    }
    if (last?.serverId && !['done', 'error', 'waiting'].includes(last.phase)) {
      void api.conversations
        .patchTurn(conversationId, last.serverId, { status: 'error', error: '已取消' })
        .catch(() => undefined)
    }
    set((s) => ({
      busy: false,
      cancel: null,
      byConversation: {
        ...s.byConversation,
        [conversationId]: turns.map((t) =>
          t.id === last?.id && !['done', 'error', 'waiting'].includes(t.phase)
            ? { ...t, phase: 'error' as Phase, error: '已取消', cancel: null }
            : t,
        ),
      },
    }))
  },

  runNow: (conversationId, turnId) => {
    const turn = get().byConversation[conversationId]?.find((t) => t.id === turnId)
    if (!turn?.graph || get().busy) return
    const patch = patcher(set, conversationId, turnId)
    const save = async (body: Record<string, any>) => {
      if (!turn.serverId) return
      try {
        await api.conversations.patchTurn(conversationId, turn.serverId, body)
      } catch { /* 存不下不该影响这次运行 */ }
    }
    patch(() => ({ phase: 'running', status: PHASE_TEXT.running, pendingRun: false }))
    set({ busy: true })
    void launch(conversationId, turnId, turn.graph, turn.question, patch, set, save)
  },

  continueTurn: async (conversationId, turnId) => {
    // 从失败的节点接着跑。**不带图**——问数据页的用户改不了图，这里就是
    // "用原来的配置重试失败的那一步"，对超时、限流、网络抖这类瞬时失败有用。
    // 配置本身错了（模型 id 写错、缺必填输入）要到画布上改，那边的「接着跑」
    // 会把改过的图一起带上。
    //
    // 接上之后 run.finished 照常触发 settle，复核层自动覆盖这一次
    const turn = get().byConversation[conversationId]?.find((t) => t.id === turnId)
    if (!turn?.run || get().busy) return
    await api.runs.continue(turn.run.id)
    await get().reattach(conversationId, turnId)
  },

  reattach: async (conversationId, turnId) => {
    const turn = get().byConversation[conversationId]?.find((t) => t.id === turnId)
    if (!turn?.run) return
    const patch = patcher(set, conversationId, turnId)
    // 上一轮的错要清掉：接着跑之后它还挂在那里，就成了一条已经不成立的红字
    const save = async (body: Record<string, any>) => {
      if (!turn.serverId) return
      try {
        await api.conversations.patchTurn(conversationId, turn.serverId, body)
      } catch { /* 同上 */ }
    }
    patch(() => ({ phase: 'running', status: PHASE_TEXT.running, error: '' }))
    set({ busy: true })
    watch(turn.run.id, patch, set, save, turn.lastSeq, turn.question)
  },
}))

/** 定点更新某个会话里的某一轮。认的是 turn id，所以切走看别的会话也不受影响 */
function patcher(set: any, conversationId: string, turnId: string): Patch {
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

/** 图建好了就直接跑——用户要的是答案，不是一张图。 */
/** 一轮最多再试一次。第二次还不行多半是问题本身没说清，该让用户介入 */
const MAX_ATTEMPTS = 2

interface PlanArgs {
  conversationId: string
  turnId: string
  /** 用户原本问的是什么。跑图取数、复核都要用它，不能被重试的补充说明污染 */
  question: string
  /** 真正发给 Copilot 的那句话。重试时会在后面缀上失败原因 */
  instruction: string
  /** 重试时把上一次那张图交回去，让它改而不是重建。null = 从零建 */
  baseGraph?: GraphSpec | null
  attempt: number
  patch: Patch
  set: any
  save: (body: Record<string, any>) => Promise<void>
}

/**
 * 建图 → 跑图。从 ask 里拆出来，是为了能带着失败原因原地重来一次。
 *
 * 重试必须回到**同一轮**：用户问了一个问题，中间重建了一次图是过程，不是
 * 两次对话。之前这件事全靠用户自己发现不对、再打一句「重来」——他那句话
 * 本来就是在手动做这件事。
 */
function plan(args: PlanArgs) {
  const {
    conversationId, turnId, question, instruction, baseGraph, attempt, patch, set, save,
  } = args

  /** 这一轮建出来的图。retry 要把它交回去，所以得在闭包里留一份 */
  let built: GraphSpec | null = baseGraph ?? null

  /**
   * 带着原因重来一次。超过上限就把原因如实留在这一轮上，不再烧钱。
   *
   * 关键是**把失败的那张图一起交回去**。只给一句"上次没成功：工具参数错了"
   * 而不给图，模型只能照着原问题从零再建一遍——很可能建出一模一样的那张，
   * 白跑一个来回。而且它连自己上次建了什么都看不到：会话历史按
   * `status != running` 过滤，正在重试的这一轮恰好被排除在外。
   *
   * 交回去之后走的是后端的"改图"分支：图预置进累加器，模型只输出改动操作，
   * 于是调大最大步数、改正工具参数这种事就是改一个字段，不是推倒重来。
   */
  const retry = (reason: string) => {
    if (attempt + 1 >= MAX_ATTEMPTS) return false
    const base = built && (built.nodes?.length ?? 0) > 0 ? built : null
    patch(() => ({
      attempt: attempt + 1,
      phase: 'planning',
      status: base ? '上一次没跑通，正在调整流程…' : '上一次没跑通，正在重新搭建流程…',
      // 旧的操作流和事件要清掉：留着会让"执行过程"里出现两段互相矛盾的记录，
      // 而用户分不清哪段是最终那次。图不清——它正是这次要改的东西
      ops: [], events: [], run: null, output: null,
      review: null, rawOutput: null, lastSeq: 0,
    }))
    plan({
      ...args,
      attempt: attempt + 1,
      baseGraph: base,
      instruction: base
        ? `这张流程上一次运行没有成功：${reason}。\n\n`
          + `请针对这个原因改它（比如调大最大步数、改正工具参数、换个查询方式），`
          + `改完继续回答原来的问题：${question}`
        : `${question}\n\n（上一次没有成功：${reason}。请针对这个原因调整，重新给出完整的工作流。）`,
    })
    return true
  }

  const cancelCopilot = streamCopilot(
    { instruction, base_graph: baseGraph ?? null, intent: 'answer', conversation_id: conversationId },
    (op) => {
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
          patch(() => ({ phase: 'building', status: op.summary || '正在搭建流程…' }))
          break
        case 'add_node':
          patch((t) => ({
            phase: 'building',
            status: `正在搭建流程…（${t.ops.filter((o) => o.op === 'add_node').length} 步）`,
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
            phase: 'done', status: PHASE_TEXT.done,
            output: { answer: text }, noQuery: true,
          }))
          set({ busy: false, cancel: null })
          void save({ status: 'done', answer: text })
          break
        }
        case 'final': {
          const graph = op.graph as GraphSpec
          built = graph          // 要重试的话，交回去的就是它
          void save({ graph, explanation: op.explanation ?? '' })

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
            // 原因放后面留作线索
            const why = `这次没能搭出可以运行的流程，你可以换个说法再问一次。`
              + `（原因：${blockers.join('；')}）`
            patch(() => ({ graph, phase: 'error', error: why }))
            set({ busy: false, cancel: null })
            void save({ status: 'error', error: why })
            break
          }

          // 用户要的是流程本身时（「设计一个每天跑的工作流」），建完就跑
          // 等于替他多花一次钱，而他要的是那张图
          if (op.autorun === false) {
            patch(() => ({
              graph, phase: 'ready', status: PHASE_TEXT.ready,
              explanation: op.explanation ?? '', pendingRun: true,
            }))
            set({ busy: false, cancel: null })
            void save({ status: 'done' })
            break
          }
          patch(() => ({
            graph, phase: 'running', status: PHASE_TEXT.running,
            explanation: op.explanation ?? '',
          }))
          void launch(conversationId, turnId, graph, question, patch, set, save, retry)
          break
        }
        case 'error':
          patch(() => ({ phase: 'error', error: op.message ?? '生成失败' }))
          set({ busy: false, cancel: null })
          void save({ status: 'error', error: op.message ?? '生成失败' })
          break
      }
    },
    (error) => {
      if (!error) return
      patch((t) => (t.phase === 'error' ? {} : { phase: 'error', error }))
      set({ busy: false, cancel: null })
      void save({ status: 'error', error })
    },
  )
  patch(() => ({ cancel: cancelCopilot }))
  set({ cancel: cancelCopilot })
}

async function launch(
  conversationId: string, turnId: string, graph: GraphSpec, question: string,
  patch: Patch, set: any, save: (body: Record<string, any>) => Promise<void>,
  retry?: (reason: string) => boolean,
) {
  try {
    const run = await api.runs.start({
      graph, input: fillInput(graph, question, historyOf(conversationId, turnId)),
    })
    patch(() => ({ run }))
    void save({ run_id: run.id })
    watch(run.id, patch, set, save, 0, question, retry)
  } catch (e: any) {
    patch(() => ({ phase: 'error', error: e?.message ?? '启动失败' }))
    set({ busy: false, cancel: null })
    void save({ status: 'error', error: e?.message ?? '启动失败' })
  }
}

/**
 * 订阅运行事件。
 *
 * 只做两件事：把事件原样存进这一轮，以及更新那几个驱动 UI 状态机的相位。
 * "查了哪张表、跑了什么 SQL"不在这里翻译——decodeRun 会从同一批事件里读出来。
 */
function watch(
  runId: string, patch: Patch, set: any,
  save: (body: Record<string, any>) => Promise<void>, after = 0,
  question = '', retry?: (reason: string) => boolean,
) {
  const stop = streamRun(runId, (event: RunEvent) => {
    const d: any = event.data ?? {}
    patch((t) => ({
      events: [...t.events, event],
      // 记下进度：审批恢复要重新接流，带上它才不会把历史再收一遍
      lastSeq: Math.max(t.lastSeq ?? 0, event.seq ?? 0),
    }))
    switch (event.type) {
      case 'tool.start': {
        const tool = String(d.tool ?? '')
        patch(() => ({
          status: tool.startsWith('db_query') ? '正在查询数据…'
            : tool.startsWith('db_schema') ? '正在了解数据结构…'
            : tool ? `正在调用 ${tool}…` : '正在执行…',
        }))
        break
      }
      case 'llm.start':
        patch(() => ({ status: '正在分析…' }))
        break
      case 'run.interrupted':
        patch(() => ({ phase: 'waiting', status: PHASE_TEXT.waiting }))
        set({ busy: false })
        break
      case 'run.failed':
        patch(() => ({ phase: 'error', error: String(d.error ?? '运行失败') }))
        set({ busy: false, cancel: null })
        void save({ status: 'error', error: String(d.error ?? '运行失败') })
        break
      case 'run.finished':
        // 成果在事件里就有，不必再取一次 run；但 run 对象带着 usage 和
        // run_class（出具横幅要用），所以还是拉一次，失败也不影响成果
        // 成果**先落库**再显示。两件事顺序不能并：
        //  - 落库要马上，否则复核期间关掉标签页，这次跑出来的东西就没了；
        //  - 显示要等复核，否则用户已经把结论读完、信了，说明才姗姗来迟——
        //    实测复核要十秒上下，这十秒里他看到的是一个没人核对过的答案。
        // 所以这里只改状态，output 交给 settle 在核完之后一次性摆出来
        patch(() => ({ status: '正在核对结果…' }))
        void save({ status: 'done', answer: answerText(d.output ?? null) })
        void api.runs.get(runId)
          .then((run) => patch(() => ({ run })))
          .catch(() => undefined)
        // 跑完不等于答对：先接住这次编排，确认没问题再交付
        void settle(runId, question, d.output ?? null, patch, set, save, retry)
        break
    }
  }, undefined, after)
  // 句柄同时挂在 turn 和 store 上：turn 上的用于精确关闭这一轮，
  // store 上的是"当前活跃流"的快捷方式
  patch(() => ({ cancel: stop }))
  set({ cancel: stop })
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
  runId: string, question: string, output: Record<string, any> | null,
  patch: Patch, set: any, save: (body: Record<string, any>) => Promise<void>,
  retry?: (reason: string) => boolean,
) {
  let result: ReviewResult | null = null
  try {
    // 复核慢到一定程度就不等了。它是补一层说明，不该反过来把答案扣住不放——
    // 干净的运行后端 15ms 就返回，会走到这个上限的都是真叫了模型的那些
    result = await Promise.race([
      api.copilot.review({ run_id: runId, question }),
      new Promise<null>((r) => setTimeout(() => r(null), REVIEW_TIMEOUT_MS)),
    ])
  } catch { /* 复核用不了就按原样交付，下面照常走 */ }

  // 库里存完整结论（包括 verdict='ok'），store 里只留值得摆到界面上的那部分。
  // 两者分开，"这一轮没复核过"和"复核过、没发现问题"才是两件可区分的事——
  // 事后排查"它当时为什么没警告我"，靠的就是这个区别
  const review = result && result.verdict !== 'ok' ? result : null

  // 答案根本不可信，而且原因说得清楚——重建一次图再跑，别让用户自己发现
  if (review?.retry && review.severity === 'broken' && retry) {
    const reason = review.signals.map((s) => s.detail).join('；') || review.note
    if (retry(reason)) return
  }

  const rewritten = review?.answer ?? null
  patch(() => ({
    phase: 'done',
    status: PHASE_TEXT.done,
    review,
    // 到这一步才把成果摆出来：复核说明和结论同时出现，而不是结论先读完
    output,
    // 改写是有损的，原件得留着——界面上可以展开对照。它跟着 review 一起
    // 落库，所以刷新之后还在
    ...(rewritten
      ? { rawOutput: { answer: review?.original ?? answerText(output) }, output: { answer: rewritten } }
      : {}),
  }))
  set({ busy: false, cancel: null })
  void save({
    status: 'done',
    answer: rewritten || answerText(output),
    ...(result ? { review: result } : {}),
  })
}

export { decodeRun }
