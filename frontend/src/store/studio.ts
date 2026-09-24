import { create } from 'zustand'
import {
  addEdge, applyEdgeChanges, applyNodeChanges,
  type Connection, type Edge, type EdgeChange, type Node, type NodeChange,
} from '@xyflow/react'
import { api, streamCopilot, streamRun } from '../api/client'
import { NODE_DEFS, sourceHandles } from '../canvas/nodeDefs'
import type { CopilotOp } from '../run/decode'
import type {
  GraphEdge, GraphSpec, NodeRuntime, NodeType, Run, RunEvent, ValidationIssue,
  VarIssue, Variable, Workflow,
} from '../types'

export type FlowNode = Node<{ nodeType: NodeType; label: string; config: Record<string, any> }>

let idSeq = 0
const nextId = (type: string) => `${type}_${Date.now().toString(36).slice(-4)}${(idSeq++ % 100).toString(36)}`

// -------------------------------------------------------------------------
// GraphSpec ↔ React Flow 互转
// -------------------------------------------------------------------------

export function toFlow(graph: GraphSpec): { nodes: FlowNode[]; edges: Edge[] } {
  return {
    nodes: (graph.nodes ?? []).map((n) => ({
      id: n.id,
      type: 'card',
      position: n.position ?? { x: 0, y: 0 },
      data: {
        nodeType: n.type,
        label: n.data?.label ?? NODE_DEFS[n.type]?.label ?? n.type,
        config: n.data?.config ?? {},
      },
    })),
    edges: (graph.edges ?? []).map((e, i) => ({
      id: e.id || `e${i}_${e.source}_${e.sourceHandle ?? ''}_${e.target}`,
      source: e.source,
      target: e.target,
      sourceHandle: e.sourceHandle ?? null,
      targetHandle: e.targetHandle ?? null,
      label: e.label || undefined,
      // 走线由 canvas/routing.ts 统一算（端口错开 + 走廊车道），
      // 不用自带的 smoothstep：它的竖直段全都落在同一个 x 上，会叠成一团
      type: 'flow',
      animated: false,
    })),
  }
}

export function toGraph(nodes: FlowNode[], edges: Edge[]): GraphSpec {
  return {
    nodes: nodes.map((n) => ({
      id: n.id,
      type: n.data.nodeType,
      position: { x: Math.round(n.position.x), y: Math.round(n.position.y) },
      data: { label: n.data.label, config: n.data.config },
    })),
    edges: edges.map<GraphEdge>((e) => ({
      id: e.id,
      source: e.source,
      target: e.target,
      sourceHandle: e.sourceHandle ?? null,
      label: typeof e.label === 'string' ? e.label : '',
    })),
  }
}

// -------------------------------------------------------------------------

/**
 * 一轮 Copilot 对话。
 *
 * 以前操作流是用完即弃的——直接改画布，只在浮条上留一句 lastOp。那意味着
 * 生成结束后，"它为什么这么建"这件事就没了：画布上是结果，过程不可回看。
 * 侧栏要显示"它大致在想什么、动了哪些节点"，就得把操作流本身留下来。
 */
export interface CopilotTurn {
  id: string
  instruction: string
  /** 原始操作流，交给 decodeCopilot 翻译。thinking 在写入时就已合并 */
  ops: CopilotOp[]
  phase: 'running' | 'done' | 'error'
  explanation: string
  error: string
}

// 只留最近几轮。画布只反映最后一次生成的结果，更早的轮次是参考而不是现状，
// 无上限地攒着只会把内存和滚动条都撑坏。
const COPILOT_TURN_LIMIT = 10

/**
 * 找出（或建出）这张图的 Copilot 会话。
 *
 * 画布的对话依附工作流：打开这张图就接着上次聊。之前连说两次"再加一个节点"，
 * 第二次并不知道第一次说了什么——base_graph 带的是图的**结果**，带不出
 * "你刚才要我干什么"，所以模型经常把上一条的意图又做一遍或者做反。
 *
 * 不进左侧列表：那边列的是"问题"，这里是"改图指令"，两种东西混在一个列表里
 * 只会让两种都更难找。
 *
 * 没保存过的草稿（workflow 为空）没有会话——它还没有一个能挂历史的身份。
 */
async function ensureCanvasConversation(
  workflowId: string, set: (partial: Partial<StudioState>) => void,
): Promise<string | null> {
  try {
    const existing = await api.conversations.list('canvas', workflowId)
    const id = existing[0]?.id
      ?? (await api.conversations.create({ kind: 'canvas', workflow_id: workflowId })).id
    set({ copilotConversationId: id })
    return id
  } catch {
    return null   // 记不下来也不该挡着人改图
  }
}

/** 把画布上这一轮记进会话，下一条指令才接得上 */
function recordCanvasTurn(
  conversationId: string | null,
  instruction: string,
  patch: { graph?: any; explanation?: string; status?: 'done' | 'error'; error?: string },
): void {
  if (!conversationId) return
  void api.conversations.startTurn(conversationId, instruction)
    .then((turn) => api.conversations.patchTurn(conversationId, turn.id, patch))
    .catch(() => undefined)
}

interface StudioState {
  workflow: Workflow | null
  nodes: FlowNode[]
  edges: Edge[]
  selectedId: string | null
  dirty: boolean
  issues: ValidationIssue[]
  /** 这张图里有哪些变量：谁产出、谁引用。结构不变就不重新请求 */
  variables: Variable[]
  /** 变量层面的提示（含"产出了没人用"这类 info，不进 issues） */
  varIssues: VarIssue[]

  run: Run | null
  events: RunEvent[]
  runtime: Record<string, NodeRuntime>
  activeEdges: string[]
  streaming: boolean
  unsubscribe: (() => void) | null

  /** 这张图的 Copilot 会话 id。一张图一条，用来让连续几条改图指令互相知情 */
  copilotConversationId: string | null

  copilot: {
    active: boolean
    lastOp: string
    explanation: string
    error: string
    model: string
    // 从提交到第一个操作之间有 5~30 秒，得让用户看见这段在发生什么
    phase: 'connecting' | 'planning' | 'building' | 'wiring' | 'finalizing' | ''
    thinking: string        // 模型的思考原文（只有支持 thinking 的模型有）
    elapsedMs: number
    lastInstruction: string // 失败后"用同一需求重试"要用
    lastUseBase: boolean
  }
  copilotNew: string[]
  copilotTurns: CopilotTurn[]
  cancelCopilot: (() => void) | null
  /** 每次 +1 = 请求画布重新取景。整张图换过坐标之后不重新取景，人会盯着一片空白 */
  fitRequest: number

  // actions
  load: (workflow: Workflow) => void
  setGraph: (graph: GraphSpec) => void
  onNodesChange: (changes: NodeChange[]) => void
  onEdgesChange: (changes: EdgeChange[]) => void
  onConnect: (conn: Connection) => void
  addNode: (type: NodeType, position: { x: number; y: number }) => void
  updateNode: (id: string, patch: { label?: string; config?: Record<string, any> }) => void
  removeNode: (id: string) => void
  duplicateNode: (id: string) => void
  select: (id: string | null) => void
  save: () => Promise<void>
  validate: () => Promise<void>
  analyzeNow: () => Promise<void>

  startRun: (input: Record<string, any>) => Promise<Run | null>
  startFormalRun: (input: Record<string, any>) => Promise<Run | null>
  runCopilot: (instruction: string, useBase: boolean, model?: string | null) => void
  stopCopilot: () => void
  retryCopilot: () => void
  clearCopilot: () => void
  attachRun: (runId: string) => Promise<void>
  /** 失败的运行从断点接着跑，把画布上改过的配置一起带过去 */
  continueRun: () => Promise<void>
  stopRun: () => Promise<void>
  clearRun: () => void
  applyEvent: (event: RunEvent) => void
}

const EMPTY_RUNTIME: NodeRuntime = { status: 'idle' }

// 300ms：比连续打字的间隔长，比"停下来看结果"的感知阈值短
const ANALYZE_DEBOUNCE_MS = 300
let analyzeTimer: ReturnType<typeof setTimeout> | null = null
/** 每次发起分析自增。回来时对不上就说明图又改过了，这个结果已经过期 */
let analyzeEpoch = 0
/** 上一次算变量表时的结构签名，没变就不重复请求 */
let lastVarSignature = ''

/**
 * "有哪些变量"只取决于图的结构，不取决于文案。
 *
 * 节点 id、类型、assign_to、入口字段名——只有这些变了，变量集合才会变。
 * 改 prompt 里的一个字不该触发一次变量分析。
 */
function varSignature(nodes: FlowNode[]): string {
  return nodes
    .map((n) => {
      const cfg = n.data.config ?? {}
      const fields = (cfg.fields ?? []).map((f: any) => f?.name ?? '').join(',')
      return `${n.id}:${n.data.nodeType}:${cfg.assign_to ?? ''}:${fields}`
    })
    .sort()
    .join('|')
}

async function runAnalysis(set: any, get: any, force = false): Promise<void> {
  const { nodes, edges } = get()
  if (!nodes.length) {
    set({ issues: [], variables: [], varIssues: [] })
    return
  }
  const epoch = ++analyzeEpoch
  const graph = toGraph(nodes, edges)
  const signature = varSignature(nodes)
  const needVars = force || signature !== lastVarSignature

  try {
    const [validation, vars] = await Promise.all([
      api.workflows.validate(graph),
      needVars ? api.workflows.variables(graph) : Promise.resolve(null),
    ])
    // 图在请求飞行期间又改了，这份结果已经不对应眼前的图——丢掉，
    // 别用旧答案覆盖新答案（校验结果闪回是最难查的那种 UI bug）
    if (epoch !== analyzeEpoch) return
    set({ issues: validation.issues ?? [] })
    if (vars) {
      lastVarSignature = signature
      set({ variables: vars.variables ?? [], varIssues: vars.issues ?? [] })
    }
  } catch {
    /* 分析失败不影响编辑 */
  }
}

export const useStudio = create<StudioState>((set, get) => ({
  workflow: null,
  nodes: [],
  edges: [],
  selectedId: null,
  dirty: false,
  issues: [],
  variables: [],
  varIssues: [],
  run: null,
  events: [],
  runtime: {},
  activeEdges: [],
  streaming: false,
  unsubscribe: null,
  copilotConversationId: null,
  copilot: { active: false, lastOp: '', explanation: '', error: '', model: '',
             phase: '', thinking: '', elapsedMs: 0, lastInstruction: '', lastUseBase: false },
  copilotNew: [],
  copilotTurns: [],
  cancelCopilot: null,
  fitRequest: 0,

  load: (workflow) => {
    const { nodes, edges } = toFlow(workflow.graph)
    get().unsubscribe?.()
    set({
      workflow, nodes, edges, selectedId: null, dirty: false, issues: [],
      run: null, events: [], runtime: {}, activeEdges: [], streaming: false, unsubscribe: null,
      // 整张图换了，视角也要跟着换。不换的话打开另一张图看到的还是上一张的
      // 那块空白——画布位置是视口的，不是图的
      fitRequest: get().fitRequest + 1,
      // 换了一张图，之前那些"我让它改成这样"就不再指向眼前这张图了
      copilotTurns: [],
      // 先清空再去解析：留着上一张图的会话 id，这中间的任何一条指令
      // 都会带着别的图的上下文发出去
      copilotConversationId: null,
    })
    void ensureCanvasConversation(workflow.id, set)
    void get().validate()
  },

  setGraph: (graph) => {
    const { nodes, edges } = toFlow(graph)
    set({ nodes, edges, dirty: true, fitRequest: get().fitRequest + 1 })
    void get().validate()
  },

  onNodesChange: (changes) => {
    set({ nodes: applyNodeChanges(changes, get().nodes) as FlowNode[] })
    // 只有拖动结束和增删才算改动，实时拖动不标脏，否则自动保存会疯狂触发
    if (changes.some((c) => (c.type === 'position' && !c.dragging) || c.type === 'remove')) {
      set({ dirty: true })
    }
  },

  onEdgesChange: (changes) => {
    set({ edges: applyEdgeChanges(changes, get().edges), dirty: true })
    if (changes.some((c) => c.type === 'remove')) void get().validate()
  },

  onConnect: (conn) => {
    set({
      edges: addEdge({ ...conn, type: 'smoothstep' }, get().edges),
      dirty: true,
    })
    void get().validate()
  },

  addNode: (type, position) => {
    const def = NODE_DEFS[type]
    const node: FlowNode = {
      id: nextId(type),
      type: 'card',
      position,
      data: { nodeType: type, label: def.label, config: structuredClone(def.defaults ?? {}) },
    }
    set({ nodes: [...get().nodes, node], selectedId: node.id, dirty: true })
    void get().validate()
  },

  updateNode: (id, patch) => {
    const target = get().nodes.find((n) => n.id === id)
    // 出口 handle 是从 config 算出来的（分支的 case key、human 的 mode），
    // 改 config 就可能让已连好的边指向一个不存在的出口。React Flow 对这种边
    // 只在 console 打一条 008 警告然后不渲染——它看不见、点不到、删不掉，
    // 却照样被 toGraph 序列化存盘。所以这里同步把边跟过去。
    const before = target ? sourceHandles(target.data.nodeType, target.data.config ?? {}) : []
    const after =
      target && patch.config !== undefined
        ? sourceHandles(target.data.nodeType, patch.config)
        : before

    let edges = get().edges
    if (target && before.length && after !== before) {
      const alive = new Set(after.map((h) => h.id))
      // 改名场景（出口数量没变）按位置重映射，这样连好的线不会白连
      const byPosition = new Map<string, string>()
      if (before.length === after.length) {
        before.forEach((h, i) => {
          if (h.id !== after[i].id) byPosition.set(h.id, after[i].id)
        })
      }
      edges = edges.flatMap((e) => {
        if (e.source !== id || !e.sourceHandle) return [e]
        if (alive.has(e.sourceHandle)) return [e]
        const moved = byPosition.get(e.sourceHandle)
        // 出口真的没了（删了一个 case、换了 human 模式）就把边一起去掉，
        // 留着只会变成一条谁也看不见的脏边
        return moved ? [{ ...e, sourceHandle: moved }] : []
      })
    }

    set({
      nodes: get().nodes.map((n) =>
        n.id === id
          ? {
              ...n,
              data: {
                ...n.data,
                ...(patch.label !== undefined ? { label: patch.label } : {}),
                ...(patch.config !== undefined ? { config: patch.config } : {}),
              },
            }
          : n,
      ),
      edges,
      dirty: true,
    })
    void get().validate()
  },

  removeNode: (id) => {
    set({
      nodes: get().nodes.filter((n) => n.id !== id),
      edges: get().edges.filter((e) => e.source !== id && e.target !== id),
      selectedId: get().selectedId === id ? null : get().selectedId,
      dirty: true,
    })
    void get().validate()
  },

  duplicateNode: (id) => {
    const source = get().nodes.find((n) => n.id === id)
    if (!source) return
    const copy: FlowNode = {
      ...source,
      id: nextId(source.data.nodeType),
      position: { x: source.position.x + 40, y: source.position.y + 40 },
      data: { ...source.data, config: structuredClone(source.data.config) },
      selected: false,
    }
    set({ nodes: [...get().nodes, copy], selectedId: copy.id, dirty: true })
  },

  select: (id) => set({ selectedId: id }),

  save: async () => {
    const { workflow, nodes, edges } = get()
    if (!workflow) return
    const graph = toGraph(nodes, edges)
    const updated = await api.workflows.update(workflow.id, { graph })
    set({ workflow: updated, dirty: false })
  },

  /**
   * 校验 + 变量分析。名字保留 validate，因为图一变就该重算的地方有八处，
   * 都在调它。
   *
   * 防抖是必须的，不是优化：在此之前它是**零防抖**的——Inspector 里每敲
   * 一个键都会 updateNode → validate()，实测敲 15 个字发 15 次全图 POST，
   * 每次都把整张图序列化上传。再挂一个变量分析就是翻倍。
   *
   * 变量表另外按结构签名跳过：改 prompt 文本不会改变"有哪些变量"，只有
   * 增删节点、改 assign_to、改入口字段才会。签名不变就不重复请求。
   */
  validate: async () => {
    const { nodes } = get()
    if (!nodes.length) {
      set({ issues: [], variables: [], varIssues: [] })
      return
    }
    if (analyzeTimer !== null) clearTimeout(analyzeTimer)
    analyzeTimer = setTimeout(() => void runAnalysis(set, get), ANALYZE_DEBOUNCE_MS)
  },

  /** 不等防抖，立刻算一次。抽屉打开、切换工作流这类明确动作用它。 */
  analyzeNow: async () => {
    if (analyzeTimer !== null) clearTimeout(analyzeTimer)
    analyzeTimer = null
    await runAnalysis(set, get, true)
  },

  // ---- 运行 ----

  startRun: async (input) => {
    const { workflow, nodes, edges } = get()
    get().unsubscribe?.()
    set({ events: [], runtime: {}, activeEdges: [], run: null })
    try {
      const run = await api.runs.start({
        workflow_id: workflow?.id,
        graph: toGraph(nodes, edges),
        input,
      })
      set({ run, streaming: true })
      await get().attachRun(run.id)
      return run
    } catch (e) {
      set({ streaming: false })
      throw e
    }
  },

  runCopilot: (instruction, useBase, model) => {
    const state = get()
    state.cancelCopilot?.()
    const turnId = `c${Date.now().toString(36)}${(idSeq++ % 1000).toString(36)}`
    set({
      copilot: {
        active: true, lastOp: '', explanation: '', error: '', model: model ?? '',
        phase: 'connecting', thinking: '', elapsedMs: 0,
        lastInstruction: instruction, lastUseBase: useBase,
      },
      copilotNew: [],
      copilotTurns: [
        ...state.copilotTurns.slice(-(COPILOT_TURN_LIMIT - 1)),
        { id: turnId, instruction, ops: [], phase: 'running', explanation: '', error: '' },
      ],
    })
    if (!useBase) set({ nodes: [], edges: [], dirty: true })

    /**
     * 记下这一条操作，供侧栏回看。
     *
     * thinking 在写入时就合并：一次生成的 delta 是几百上千条，逐条存下来
     * 光是数组本身就比图大一个量级，而 decodeCopilot 反正也要把它们并成一条。
     */
    const record = (op: CopilotOp) => {
      set((s: StudioState) => ({
        copilotTurns: s.copilotTurns.map((t) => {
          if (t.id !== turnId) return t
          const last = t.ops[t.ops.length - 1]
          if (op.op === 'thinking' && last?.op === 'thinking') {
            const merged = { ...last, delta: String(last.delta ?? '') + String(op.delta ?? '') }
            return { ...t, ops: [...t.ops.slice(0, -1), merged] }
          }
          return { ...t, ops: [...t.ops, op] }
        }),
      }))
    }
    const settle = (patch: Partial<CopilotTurn>) => {
      set((s: StudioState) => ({
        copilotTurns: s.copilotTurns.map((t) => (t.id === turnId ? { ...t, ...patch } : t)),
      }))
    }

    // 流式期间的临时摆位：新节点放在其入边源的右侧；final 会用后端排版整体替换
    const place = (nodeId: string): { x: number; y: number } => {
      const { nodes, edges } = get()
      const incoming = edges.find((e) => e.target === nodeId)
      const source = incoming && get().nodes.find((n) => n.id === incoming.source)
      if (source) {
        const siblings = edges.filter((e) => e.source === source.id).length - 1
        return { x: source.position.x + 290, y: source.position.y + siblings * 150 }
      }
      return { x: 120 + nodes.length * 290, y: 320 }
    }

    const stop = streamCopilot(
      {
        instruction,
        base_graph: useBase && state.nodes.length ? toGraph(state.nodes, state.edges) : null,
        model: model ?? undefined,
        conversation_id: state.copilotConversationId,
      },
      (op) => {
        const s = get()
        record(op)
        switch (op.op) {
          case 'model':
            // 后端首帧告知实际用的模型，浮条上直接显示，不用猜
            set({ copilot: { ...s.copilot, model: op.model ?? '' } })
            break
          case 'thinking':
            // 模型正在想什么，原样接上去。看着它想比盯着"正在起草…"强得多
            set({
              copilot: {
                ...s.copilot,
                phase: s.copilot.phase === 'connecting' ? 'planning' : s.copilot.phase,
                thinking: (s.copilot.thinking + (op.delta ?? '')).slice(-4000),
              },
            })
            break
          case 'heartbeat':
            // 模型沉默期间的补拍，用来刷新阶段和已用时长
            set({
              copilot: {
                ...s.copilot,
                phase: (op.phase as any) ?? s.copilot.phase,
                elapsedMs: op.elapsed_ms ?? s.copilot.elapsedMs,
              },
            })
            break
          case 'plan':
            set({ copilot: { ...s.copilot, phase: 'planning', lastOp: op.summary ?? '规划中' } })
            break
          case 'add_node': {
            const n = op.node
            if (!n?.id || !n?.type) break
            const node: FlowNode = {
              id: n.id, type: 'card', position: { x: 0, y: 0 },
              data: { nodeType: n.type, label: n.label ?? '', config: n.config ?? {} },
            }
            set({
              nodes: [...s.nodes.filter((x) => x.id !== n.id), node],
              copilotNew: [...s.copilotNew, n.id],
              copilot: { ...s.copilot, lastOp: `添加节点：${n.label || n.id}` },
              dirty: true,
            })
            // 位置要等边可能已到齐后算——直接再取一次最新状态摆位
            set({
              nodes: get().nodes.map((x) => (x.id === n.id ? { ...x, position: place(n.id) } : x)),
            })
            break
          }
          case 'update_node':
            set({
              nodes: s.nodes.map((x) =>
                x.id === op.id
                  ? { ...x, data: { ...x.data,
                      ...(op.label != null ? { label: op.label } : {}),
                      ...(op.config != null ? { config: op.config } : {}) } }
                  : x),
              copilotNew: s.copilotNew.includes(op.id) ? s.copilotNew : [...s.copilotNew, op.id],
              copilot: { ...s.copilot, lastOp: `修改节点：${op.id}` },
              dirty: true,
            })
            break
          case 'remove_node':
            set({
              nodes: s.nodes.filter((x) => x.id !== op.id),
              edges: s.edges.filter((e) => e.source !== op.id && e.target !== op.id),
              copilot: { ...s.copilot, lastOp: `移除节点：${op.id}` },
              dirty: true,
            })
            break
          case 'add_edge': {
            const e = op.edge
            if (!e?.source || !e?.target) break
            set({
              edges: [...s.edges, {
                id: `cp_${e.source}_${e.sourceHandle ?? ''}_${e.target}`,
                source: e.source, target: e.target,
                sourceHandle: e.sourceHandle ?? null, type: 'smoothstep',
              }],
              copilot: { ...s.copilot, lastOp: `连线：${e.source} → ${e.target}` },
              dirty: true,
            })
            break
          }
          case 'remove_edge':
            set({
              edges: s.edges.filter((e) =>
                !(e.source === op.source && e.target === op.target
                  && (op.sourceHandle == null || e.sourceHandle === op.sourceHandle))),
              dirty: true,
            })
            break
          case 'done':
            set({ copilot: { ...s.copilot, phase: 'finalizing', lastOp: '排版整理中…',
                             explanation: op.explanation ?? '' } })
            break
          case 'final': {
            // 后端排版+校验后的最终图整体落位；高亮集合保留几秒供辨认
            const { nodes, edges } = toFlow(op.graph)
            set({ nodes, edges, dirty: true,
                  // 生成完的图坐标是后端新排的，视角得跟过去，否则"生成好了"
                  // 这句话落在一片空白上
                  fitRequest: get().fitRequest + 1,
                  copilot: { ...get().copilot, active: false, lastOp: '', phase: '',
                             thinking: '', elapsedMs: 0, error: '',
                             explanation: op.explanation ?? get().copilot.explanation } })
            settle({ phase: 'done', explanation: op.explanation ?? get().copilot.explanation })
            recordCanvasTurn(get().copilotConversationId, instruction, {
              graph: op.graph, explanation: op.explanation ?? '', status: 'done',
            })
            void get().validate()
            setTimeout(() => set({ copilotNew: [] }), 6000)
            break
          }
          case 'error':
            // 保留 lastInstruction：失败后的"重试"要用它，不能让用户重填
            set({ copilot: { ...get().copilot, active: false, lastOp: '', phase: '',
                             thinking: '', explanation: '', error: op.message ?? '生成失败' } })
            settle({ phase: 'error', error: op.message ?? '生成失败' })
            recordCanvasTurn(get().copilotConversationId, instruction, {
              status: 'error', error: op.message ?? '生成失败',
            })
            break
        }
      },
      (error) => {
        const c = get().copilot
        set({
          copilot: { ...c, active: false, error: error ?? c.error },
          cancelCopilot: null,
        })
        // 流断了但没收到 final/error：这一轮不能一直挂在"进行中"，
        // 否则侧栏会永远转圈，而后台其实什么都不会再来了
        const t = get().copilotTurns.find((x) => x.id === turnId)
        if (t?.phase === 'running') {
          settle(error ? { phase: 'error', error } : { phase: 'done' })
        }
      },
    )
    set({ cancelCopilot: stop })
  },

  stopCopilot: () => {
    get().cancelCopilot?.()
    set({
      copilot: { ...get().copilot, active: false, phase: '', thinking: '' },
      cancelCopilot: null,
      copilotTurns: get().copilotTurns.map((t) =>
        t.phase === 'running' ? { ...t, phase: 'error', error: '已取消' } : t),
    })
  },

  clearCopilot: () => set({ copilotTurns: [] }),

  retryCopilot: () => {
    // 失败后用同一条需求重来。以前只能重新打开 Modal 把需求再敲一遍，
    // 而失败往往跟需求本身无关（模型抽风、协议跑偏、网络断了）
    const { lastInstruction, lastUseBase, model } = get().copilot
    if (!lastInstruction) return
    get().runCopilot(lastInstruction, lastUseBase, model || null)
  },

  startFormalRun: async (input) => {
    const { workflow } = get()
    if (!workflow) return null
    get().unsubscribe?.()
    set({ events: [], runtime: {}, activeEdges: [], run: null })
    try {
      // 正式运行不传 graph：后端只认已发布的不可变版本
      const run = await api.runs.start({
        workflow_id: workflow.id,
        run_class: 'formal',
        input,
      })
      set({ run, streaming: true })
      await get().attachRun(run.id)
      return run
    } catch (e) {
      set({ streaming: false })
      throw e
    }
  },

  attachRun: async (runId) => {
    get().unsubscribe?.()
    // 手上已有的事件不要再收一遍。审批恢复会走到这里，而 applyEvent 是无条件
    // 追加——不传 after 的话后端把整条历史重推一遍，时间线上每条都出现两次。
    const current = get()
    const after = current.run?.id === runId
      ? current.events.reduce((max, e) => Math.max(max, e.seq ?? 0), 0)
      : 0
    const stop = streamRun(
      runId,
      (event) => get().applyEvent(event),
      () => set({ streaming: false }),
      after,
    )
    set({ unsubscribe: stop, streaming: true })
  },

  continueRun: async () => {
    // 从失败的那个节点接着跑，前面跑过的不重来。
    //
    // 把画布**当前**这张图带过去，而不是运行时那份快照——失败的多半是配置错了
    // （模型 id 写错、循环条件语法错、缺必填输入），用户正是在画布上改完它才
    // 点的这里。带的是运行时那份的话，改完还会再撞同一个错。
    //
    // 后端只接受骨架一致的图：增删节点或改连线就拒绝。checkpoint 是按节点名
    // 存的，结构变了续下去会拿着错位的状态跑出一份似是而非的结果。
    const { run, nodes, edges } = get()
    if (!run) return
    const continued = await api.runs.continue(run.id, toGraph(nodes, edges))
    set({ run: continued, streaming: true })
    await get().attachRun(run.id)
  },

  stopRun: async () => {
    const run = get().run
    if (!run) return
    await api.runs.cancel(run.id).catch(() => undefined)
    set({ streaming: false })
  },

  clearRun: () => {
    get().unsubscribe?.()
    set({ run: null, events: [], runtime: {}, activeEdges: [], streaming: false, unsubscribe: null })
  },

  /**
   * 把一条事件折算成画布上的可见变化。
   * 这是"实时可视化"的全部秘密：事件流 → 节点状态 → 高亮/动效。
   */
  applyEvent: (event) => {
    const state = get()
    const events = [...state.events, event]
    const runtime = { ...state.runtime }
    const nodeId = event.node_id
    const patch = (changes: Partial<NodeRuntime>) => {
      if (!nodeId) return
      runtime[nodeId] = { ...(runtime[nodeId] ?? EMPTY_RUNTIME), ...changes }
    }

    switch (event.type) {
      case 'node.started':
        patch({ status: 'running', tokens: '', thinking: '', error: undefined, toolCalls: [] })
        break
      case 'node.finished':
        patch({ status: 'done', durationMs: event.data.duration_ms, preview: event.data.preview })
        break
      case 'node.failed':
        patch({ status: 'failed', error: event.data.error, durationMs: event.data.duration_ms })
        break
      case 'node.skipped':
        patch({ status: 'skipped' })
        break
      case 'llm.token':
        patch({ tokens: (runtime[nodeId!]?.tokens ?? '') + (event.data.delta ?? '') })
        break
      case 'llm.thinking.delta':
        patch({ thinking: (runtime[nodeId!]?.thinking ?? '') + (event.data.delta ?? '') })
        break
      case 'llm.thinking':
        // 汇总事件：直接覆盖为完整思考。回放（刷新页面）时靠这一条恢复。
        patch({ thinking: event.data.text ?? runtime[nodeId!]?.thinking })
        break
      case 'tool.start':
        patch({
          toolCalls: [...(runtime[nodeId!]?.toolCalls ?? []), { tool: event.data.tool, args: event.data.args }],
        })
        break
      case 'tool.end':
      case 'tool.error': {
        const calls = [...(runtime[nodeId!]?.toolCalls ?? [])]
        const idx = calls.map((c) => c.tool).lastIndexOf(event.data.tool)
        if (idx >= 0) {
          calls[idx] = { ...calls[idx], result: event.data.preview, ok: event.type === 'tool.end' }
        }
        patch({ toolCalls: calls })
        break
      }
      case 'human.requested':
        patch({ status: 'waiting' })
        break
      case 'run.interrupted':
        if (nodeId) patch({ status: 'waiting' })
        break
      case 'run.finished':
      case 'run.failed':
      case 'run.cancelled':
        set({ streaming: false })
        break
    }

    // 高亮当前正在流动的边：从已完成节点指向正在运行的节点
    const running = Object.entries(runtime)
      .filter(([, r]) => r.status === 'running' || r.status === 'waiting')
      .map(([id]) => id)
    const activeEdges = state.edges
      .filter((e) => running.includes(e.target) && runtime[e.source]?.status === 'done')
      .map((e) => e.id)

    set({ events, runtime, activeEdges })
  },
}))

// 开发期把 store 挂到 window 上，好让 playwright 直接读状态做断言——
// 界面上看不出"事件收全了没有"，只能问 store。生产构建里去掉。
if (import.meta.env.DEV) {
  ;(window as any).__studio = useStudio
}
