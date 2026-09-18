import { create } from 'zustand'
import { api, streamCopilot, streamRun } from '../api/client'
import type { CopilotOp } from '../run/decode'
import { decodeRun } from '../run/decode'
import { useConversations } from './conversations'
import type { GraphSpec, Run, RunEvent } from '../types'

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
            lastSeq: 0, cancel: null,
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

    const cancelCopilot = streamCopilot(
      { instruction: question, base_graph: null, intent: 'answer', conversation_id: conversationId },
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
            void save({ graph, explanation: op.explanation ?? '' })
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
            void launch(conversationId, id, graph, question, patch, set, save)
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
  },

  stop: (conversationId) => {
    // 只停最后一轮。把所有非终态 turn 一律标成"已取消"会误杀正等人工介入的
    // 历史轮次——那些是等着用户回来处理的，不是卡住的。
    const turns = get().byConversation[conversationId] ?? []
    const last = turns[turns.length - 1]
    last?.cancel?.()
    get().cancel?.()
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

  reattach: async (conversationId, turnId) => {
    const turn = get().byConversation[conversationId]?.find((t) => t.id === turnId)
    if (!turn?.run) return
    const patch = patcher(set, conversationId, turnId)
    const save = async (body: Record<string, any>) => {
      if (!turn.serverId) return
      try {
        await api.conversations.patchTurn(conversationId, turn.serverId, body)
      } catch { /* 同上 */ }
    }
    patch(() => ({ phase: 'running', status: PHASE_TEXT.running }))
    set({ busy: true })
    watch(turn.run.id, patch, set, save, turn.lastSeq)
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
async function launch(
  conversationId: string, turnId: string, graph: GraphSpec, question: string,
  patch: Patch, set: any, save: (body: Record<string, any>) => Promise<void>,
) {
  try {
    const run = await api.runs.start({
      graph, input: fillInput(graph, question, historyOf(conversationId, turnId)),
    })
    patch(() => ({ run }))
    void save({ run_id: run.id })
    watch(run.id, patch, set, save)
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
        patch(() => ({
          phase: 'done', status: PHASE_TEXT.done, output: d.output ?? null,
        }))
        set({ busy: false, cancel: null })
        void save({ status: 'done', answer: answerText(d.output ?? null) })
        void api.runs.get(runId)
          .then((run) => patch(() => ({ run })))
          .catch(() => undefined)
        break
    }
  }, undefined, after)
  // 句柄同时挂在 turn 和 store 上：turn 上的用于精确关闭这一轮，
  // store 上的是"当前活跃流"的快捷方式
  patch(() => ({ cancel: stop }))
  set({ cancel: stop })
}

export { decodeRun }
