import { create } from 'zustand'
import { api, streamCopilot, streamRun } from '../api/client'
import type { CopilotOp } from '../run/decode'
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
 */

export type Phase =
  | 'idle'
  | 'planning'    // Copilot 在想
  | 'building'    // 图在长出来
  | 'running'     // 图在跑
  | 'waiting'     // 停在人工介入
  | 'done'
  | 'error'

export interface ChatTurn {
  id: string
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
}

interface ChatState {
  turns: ChatTurn[]
  busy: boolean
  cancel: (() => void) | null

  ask: (question: string) => void
  stop: () => void
  clear: () => void
  /** 审批完成后重新接上运行 */
  reattach: (turnId: string) => Promise<void>
}

let seq = 0
const newId = () => `t${Date.now().toString(36)}${seq++}`

const PHASE_TEXT: Record<Phase, string> = {
  idle: '',
  planning: '正在理解需求…',
  building: '正在搭建流程…',
  running: '正在执行…',
  waiting: '等待你的确认',
  done: '完成',
  error: '出错了',
}

type Patch = (fn: (t: ChatTurn) => Partial<ChatTurn>) => void

export const useChat = create<ChatState>((set, get) => ({
  turns: [],
  busy: false,
  cancel: null,

  ask: (question) => {
    const id = newId()
    const patch: Patch = (fn) =>
      set((s) => ({
        turns: s.turns.map((t) => (t.id === id ? { ...t, ...fn(t) } : t)),
      }))

    set((s) => ({
      busy: true,
      turns: [
        ...s.turns,
        {
          id, question, phase: 'planning', status: PHASE_TEXT.planning,
          thinking: '', ops: [], events: [], graph: null, explanation: '',
          run: null, output: null, error: '', startedAt: Date.now(),
          lastSeq: 0, cancel: null,
        },
      ],
    }))

    const cancelCopilot = streamCopilot(
      { instruction: question, base_graph: null },
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
          case 'final': {
            // 后端排版校验后的最终图。拿到它就可以直接跑了
            const graph = op.graph as GraphSpec
            patch(() => ({
              graph, phase: 'running', status: PHASE_TEXT.running,
              explanation: op.explanation ?? '',
            }))
            void launch(id, graph, patch, set)
            break
          }
          case 'error':
            patch(() => ({ phase: 'error', error: op.message ?? '生成失败' }))
            set({ busy: false, cancel: null })
            break
        }
      },
      (error) => {
        if (!error) return
        patch((t) => (t.phase === 'error' ? {} : { phase: 'error', error }))
        set({ busy: false, cancel: null })
      },
    )
    patch(() => ({ cancel: cancelCopilot }))
    set({ cancel: cancelCopilot })
  },

  stop: () => {
    // 只停最后一轮。把所有非终态 turn 一律标成"已取消"会误杀正等人工介入的
    // 历史轮次——那些是等着用户回来处理的，不是卡住的。
    const turns = get().turns
    const last = turns[turns.length - 1]
    last?.cancel?.()
    get().cancel?.()
    set((s) => ({
      busy: false,
      cancel: null,
      turns: s.turns.map((t) =>
        t.id === last?.id && !['done', 'error', 'waiting'].includes(t.phase)
          ? { ...t, phase: 'error', error: '已取消', cancel: null }
          : t,
      ),
    }))
  },

  clear: () => {
    get().cancel?.()
    get().turns.forEach((t) => t.cancel?.())   // 每轮各有各的订阅，逐个关
    set({ turns: [], busy: false, cancel: null })
  },

  reattach: async (turnId) => {
    const turn = get().turns.find((t) => t.id === turnId)
    if (!turn?.run) return
    const patch: Patch = (fn) =>
      set((s) => ({ turns: s.turns.map((t) => (t.id === turnId ? { ...t, ...fn(t) } : t)) }))
    patch(() => ({ phase: 'running', status: PHASE_TEXT.running }))
    set({ busy: true })
    watch(turn.run.id, turnId, patch, set, turn.lastSeq)
  },
}))

/** 图建好了就直接跑——用户要的是答案，不是一张图。 */
async function launch(turnId: string, graph: GraphSpec, patch: Patch, set: any) {
  try {
    const run = await api.runs.start({ graph, input: {} })
    patch(() => ({ run }))
    watch(run.id, turnId, patch, set)
  } catch (e: any) {
    patch(() => ({ phase: 'error', error: e?.message ?? '启动失败' }))
    set({ busy: false, cancel: null })
  }
}

/**
 * 订阅运行事件。
 *
 * 只做两件事：把事件原样存进这一轮，以及更新那几个驱动 UI 状态机的相位。
 * "查了哪张表、跑了什么 SQL"不在这里翻译——decodeRun 会从同一批事件里读出来。
 */
function watch(runId: string, turnId: string, patch: Patch, set: any, after = 0) {
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
        break
      case 'run.finished':
        // 成果在事件里就有，不必再取一次 run；但 run 对象带着 usage 和
        // run_class（出具横幅要用），所以还是拉一次，失败也不影响成果
        patch(() => ({
          phase: 'done', status: PHASE_TEXT.done, output: d.output ?? null,
        }))
        set({ busy: false, cancel: null })
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
