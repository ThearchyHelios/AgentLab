import { create } from 'zustand'
import { api, streamCopilot, streamRun } from '../api/client'
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
  /** Copilot 建出来的图 */
  graph: GraphSpec | null
  /** Copilot 对这张图的说明 */
  explanation: string
  run: Run | null
  /** 跑图过程中值得показ的事件（查了什么、拿到什么） */
  steps: { icon: string; text: string; detail?: string }[]
  /** 最终成果 */
  output: Record<string, any> | null
  error: string
  startedAt: number
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

export const useChat = create<ChatState>((set, get) => ({
  turns: [],
  busy: false,
  cancel: null,

  ask: (question) => {
    const id = newId()
    const patch = (fn: (t: ChatTurn) => Partial<ChatTurn>) =>
      set((s) => ({
        turns: s.turns.map((t) => (t.id === id ? { ...t, ...fn(t) } : t)),
      }))

    set((s) => ({
      busy: true,
      turns: [
        ...s.turns,
        {
          id, question, phase: 'planning', status: PHASE_TEXT.planning,
          thinking: '', graph: null, explanation: '', run: null,
          steps: [], output: null, error: '', startedAt: Date.now(),
        },
      ],
    }))

    // 建图阶段：Copilot 的操作流直接翻译成人话，不让用户看 add_node/add_edge
    const nodes: any[] = []
    const edges: any[] = []

    const cancelCopilot = streamCopilot(
      { instruction: question, base_graph: null },
      (op) => {
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
            if (op.node) nodes.push(op.node)
            patch((t) => ({
              phase: 'building',
              status: `正在搭建流程…（${nodes.length} 步）`,
              steps: [...t.steps],
            }))
            break
          case 'add_edge':
            if (op.edge) edges.push(op.edge)
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
            void launch(id, graph, patch, set, get)
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
    set({ cancel: cancelCopilot })
  },

  stop: () => {
    get().cancel?.()
    set((s) => ({
      busy: false,
      cancel: null,
      turns: s.turns.map((t) =>
        t.phase === 'done' || t.phase === 'error' ? t : { ...t, phase: 'error', error: '已取消' },
      ),
    }))
  },

  clear: () => {
    get().cancel?.()
    set({ turns: [], busy: false, cancel: null })
  },

  reattach: async (turnId) => {
    const turn = get().turns.find((t) => t.id === turnId)
    if (!turn?.run) return
    const patch = (fn: (t: ChatTurn) => Partial<ChatTurn>) =>
      set((s) => ({ turns: s.turns.map((t) => (t.id === turnId ? { ...t, ...fn(t) } : t)) }))
    patch(() => ({ phase: 'running', status: PHASE_TEXT.running }))
    set({ busy: true })
    watch(turn.run.id, turnId, patch, set)
  },
}))

/** 图建好了就直接跑——用户要的是答案，不是一张图。 */
async function launch(
  turnId: string,
  graph: GraphSpec,
  patch: (fn: (t: ChatTurn) => Partial<ChatTurn>) => void,
  set: any,
  _get: any,
) {
  try {
    const run = await api.runs.start({ graph, input: {} })
    patch(() => ({ run }))
    watch(run.id, turnId, patch, set)
  } catch (e: any) {
    patch(() => ({ phase: 'error', error: e?.message ?? '启动失败' }))
    set({ busy: false, cancel: null })
  }
}

/** 订阅运行事件，把关键动作翻译成人话。 */
function watch(
  runId: string,
  turnId: string,
  patch: (fn: (t: ChatTurn) => Partial<ChatTurn>) => void,
  set: any,
) {
  const stop = streamRun(runId, (event: RunEvent) => {
    const d: any = event.data ?? {}
    switch (event.type) {
      case 'tool.start': {
        const tool = String(d.tool ?? '')
        const sql = String(d.args?.sql ?? '')
        if (tool.startsWith('db_query')) {
          patch((t) => ({
            status: '正在查询数据…',
            steps: [...t.steps, { icon: 'db', text: '查询数据', detail: sql }],
          }))
        } else if (tool.startsWith('db_schema')) {
          patch((t) => ({
            status: '正在了解数据结构…',
            steps: [...t.steps, { icon: 'schema', text: `查看表结构 ${d.args?.table ?? ''}` }],
          }))
        } else if (tool) {
          patch((t) => ({
            status: `正在调用 ${tool}…`,
            steps: [...t.steps, { icon: 'tool', text: `调用 ${tool}` }],
          }))
        }
        break
      }
      case 'tool.end': {
        const preview = String(d.preview ?? '')
        patch((t) => {
          const steps = [...t.steps]
          const last = steps[steps.length - 1]
          if (last && !last.detail?.startsWith('→')) {
            // 查询结果里最有用的是行数，塞进上一条步骤而不是新起一行
            const m = preview.match(/"row_count":\s*(\d+)/)
            if (m) last.text = `${last.text} · ${m[1]} 行`
          }
          return { steps }
        })
        break
      }
      case 'llm.start':
        patch(() => ({ status: '正在分析…' }))
        break
      case 'run.interrupted':
        patch(() => ({ phase: 'waiting', status: PHASE_TEXT.waiting }))
        set({ busy: false })
        break
      case 'node.failed':
        patch((t) => ({
          steps: [...t.steps, { icon: 'error', text: String(d.error ?? '某一步失败了') }],
        }))
        break
      case 'run.failed':
        patch(() => ({ phase: 'error', error: String(d.error ?? '运行失败') }))
        set({ busy: false, cancel: null })
        break
      case 'run.finished':
        void finish(runId, turnId, patch, set)
        break
    }
  })
  set({ cancel: stop })
}

async function finish(
  runId: string,
  _turnId: string,
  patch: (fn: (t: ChatTurn) => Partial<ChatTurn>) => void,
  set: any,
) {
  try {
    const run = await api.runs.get(runId)
    patch(() => ({
      run, phase: 'done', status: PHASE_TEXT.done, output: run.output ?? null,
    }))
  } catch {
    patch(() => ({ phase: 'done', status: PHASE_TEXT.done }))
  }
  set({ busy: false, cancel: null })
}
