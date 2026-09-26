import { create } from 'zustand'
import {
  addEdge, applyEdgeChanges, applyNodeChanges,
  type Connection, type Edge, type EdgeChange, type Node, type NodeChange,
} from '@xyflow/react'
import { api, streamCopilot, streamRun } from '../api/client'
import { NODE_DEFS, sourceHandles } from '../canvas/nodeDefs'
import { toast } from '../components/ui'
import { humanizeError } from '../lib/errors'
import { formatShortcut } from '../lib/keys'
import type { CopilotOp } from '../run/decode'
import { endingOf, reduceTeam, settleTeam } from '../run/decode'
import { activeEdgesOf, applyDerived } from '../run/derive'
import {
  emptyTrace, finalizeTrace, foldEvent, isActivePhase, isSettled, isTerminal, runStatusOf,
  type NodeState, type RunPhase, type Trace,
} from '../run/trace'
import type {
  ConversationTurn, GraphEdge, GraphSpec, NodeRuntime, NodeType, Run, RunEvent, ValidationIssue,
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
  /** 在现有的图上改（true），还是从头生成（false） */
  useBase?: boolean
  /** 助手只回了一句话、没有改图（op=reply）。卡片按 Markdown 渲染它 */
  reply?: string
  /**
   * 这一轮对画布做了什么。phase 只说流程走没走完，说不清画布变没变：
   * - applied   收到 final，画布改了（diff 里是改了什么）
   * - answered  助手回了一句话，画布没动
   * - unchanged 走完了，但画布一处都没变（没收到 final 的流、改了个寂寞）
   * - reverted  停止、失败或断流，已经退回这一轮之前（⇧⌘Z 能找回半成品）
   */
  outcome?: 'applied' | 'answered' | 'unchanged' | 'reverted'
  /** 这一轮改了哪些节点。回执「已应用 N 处改动」和逐项查看都用它 */
  diff?: GraphDiff
  /** final 里的问题。code === 'unknown_node_type' 的是「少了一步」：模型写了不存在的类型 */
  issues?: CopilotIssue[]
  /** 服务端自查的结论；repairing 时带第几轮 */
  check?: {
    status: 'repairing' | 'passed' | 'failed' | 'error'
    issues: string[]
    round?: number
    repaired?: number
    message?: string
  }
  /** 这一轮开始前打的撤销点。还在栈顶时「撤销这次」直接撤它 */
  checkpoint?: number
}

export interface CopilotIssue {
  level: 'error' | 'warning'
  message: string
  node_id?: string | null
  code?: string
  type?: string
}

/** 两张图差在哪。只看节点的名字和配置，挪位置不算改动 */
export interface GraphDiff {
  added: string[]
  removed: string[]
  changed: string[]
  edgesAdded: number
  edgesRemoved: number
  /** 一共几处：回执里的 N */
  total: number
}

// 只留最近几轮。画布只反映最后一次生成的结果，更早的轮次是参考而不是现状，
// 无上限地攒着只会把内存和滚动条都撑坏。
const COPILOT_TURN_LIMIT = 10

/**
 * 后端每次改图带上最近几轮问答（conversations.py 的 HISTORY_TURNS）。界面上要说清
 * 「模型此刻记得几轮」，不能显示一片空白而模型脑子里装着前 6 轮。
 */
export const COPILOT_HISTORY_TURNS = 6

const edgeSig = (e: { source: string; target: string; sourceHandle?: string | null }): string =>
  `${e.source}>${e.target}:${e.sourceHandle ?? ''}`

export function diffGraphs(
  before: { nodes: FlowNode[]; edges: Edge[] }, after: { nodes: FlowNode[]; edges: Edge[] },
): GraphDiff {
  const old = new Map(before.nodes.map((n) => [n.id, n]))
  const now = new Map(after.nodes.map((n) => [n.id, n]))
  const added = after.nodes.filter((n) => !old.has(n.id)).map((n) => n.id)
  const removed = before.nodes.filter((n) => !now.has(n.id)).map((n) => n.id)
  const changed = after.nodes.filter((n) => {
    const o = old.get(n.id)
    return !!o && o !== n && (o.data.label !== n.data.label
      || o.data.nodeType !== n.data.nodeType || configSig(o.data.config) !== configSig(n.data.config))
  }).map((n) => n.id)
  const oldEdges = new Set(before.edges.map(edgeSig))
  const newEdges = new Set(after.edges.map(edgeSig))
  const edgesAdded = [...newEdges].filter((x) => !oldEdges.has(x)).length
  const edgesRemoved = [...oldEdges].filter((x) => !newEdges.has(x)).length
  return {
    added, removed, changed, edgesAdded, edgesRemoved,
    total: added.length + removed.length + changed.length + edgesAdded + edgesRemoved,
  }
}

// -------------------------------------------------------------------------
// 撤销栈
// -------------------------------------------------------------------------

/**
 * 撤销栈里的一格：改动**之前**画布的样子，外加这是哪一步。
 *
 * 只存节点和边的引用，不深拷贝：store 里的数组和节点对象从来都是换新的、不原地改，
 * 所以旧引用本身就是一份不会再变的快照。50 步的代价只是 50 组指针。
 * 运行态（runtime、events、trace）不进来——撤销的是编辑，不是运行结果。
 */
export interface HistoryEntry {
  id: number
  label: string
  nodes: FlowNode[]
  edges: Edge[]
  at: number
  /** 合并键：同一个键在窗口期内连续提交只算一步（连续打字、一次删除里的节点和边） */
  key?: string
}

const HISTORY_LIMIT = 50
/** 同一个字段连续输入，停顿不超过这么久就算同一步 */
const TYPING_MERGE_MS = 1200
let historySeq = 0

/** 拖动开始前的样子。拖动中每一帧都有 position 变化，只在松手时记一步 */
let dragOrigin: { nodes: FlowNode[]; edges: Edge[] } | null = null

/**
 * 正在进行的那一轮助手生成。stop 按「停止」收尾（画布退回这一轮之前）；
 * abandon 只掐流不动画布——换图时用，那时候画布已经是另一张图了
 */
let activeTurn: { id: string; stop: () => void; abandon: () => void } | null = null

/** 复制出来的节点和它们之间的边。放在模块里：换一张图也能粘过去 */
let clipboard: { nodes: FlowNode[]; edges: Edge[] } | null = null

/**
 * 助手收尾时节点从流式期间的临时位置滑到排版位置。每次撤销、重做、换图、新开一轮
 * 都自增：正在滑的那一次看到对不上就停，不会把撤回去的节点又拽回来
 */
let glideSeq = 0
const GLIDE_MS = 320
/** 超过这么多节点就直接落位：逐帧重算走线的代价随节点数涨，大图上一卡一卡的反而像出错 */
const GLIDE_MAX_NODES = 60

const reducedMotion = (): boolean => {
  try {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches
  } catch {
    return false
  }
}

/** 卡片默认尺寸：量到之前按这个避让 */
const CARD_W = 238
const CARD_H = 96

const boxOf = (n: FlowNode) => ({
  x: n.position.x, y: n.position.y,
  w: n.measured?.width ?? CARD_W, h: n.measured?.height ?? CARD_H,
})

/**
 * 从 anchor（卡片左上角）开始由近到远找一块空地，不压住任何已有节点。
 *
 * 以前调色板点击添加固定落在流坐标 (220~380, 160~360) 的随机点，和视口无关，
 * 实测会叠在已有节点上；每加一个节点都得先找它在哪、再把它拖开。
 */
function freeSpot(
  nodes: FlowNode[], anchor: { x: number; y: number }, size = { w: CARD_W, h: CARD_H },
): { x: number; y: number } {
  const boxes = nodes.map(boxOf)
  const gap = 24
  const free = (x: number, y: number) => boxes.every((b) =>
    x + size.w + gap <= b.x || b.x + b.w + gap <= x || y + size.h + gap <= b.y || b.y + b.h + gap <= y)
  const stepX = size.w + 40
  const stepY = size.h + 30
  for (let ring = 0; ring < 9; ring++) {
    const cells: [number, number][] = []
    for (let i = -ring; i <= ring; i++) {
      for (let j = -ring; j <= ring; j++) {
        if (Math.max(Math.abs(i), Math.abs(j)) === ring) cells.push([i, j])
      }
    }
    // 同一圈里先试竖直方向：横向是数据流的方向，往下摆不容易挡住下游
    cells.sort((a, b) => (Math.abs(a[0]) * 2 + Math.abs(a[1])) - (Math.abs(b[0]) * 2 + Math.abs(b[1])))
    for (const [i, j] of cells) {
      const x = anchor.x + i * stepX
      const y = anchor.y + j * stepY
      if (free(x, y)) return { x: Math.round(x), y: Math.round(y) }
    }
  }
  return anchor
}

/** 整张图的包围盒中心。没有视口信息时（画布还没挂上）用它当落点 */
function centerOf(nodes: FlowNode[]): { x: number; y: number } {
  if (!nodes.length) return { x: 240, y: 200 }
  const boxes = nodes.map(boxOf)
  const x0 = Math.min(...boxes.map((b) => b.x))
  const y0 = Math.min(...boxes.map((b) => b.y))
  const x1 = Math.max(...boxes.map((b) => b.x + b.w))
  const y1 = Math.max(...boxes.map((b) => b.y + b.h))
  return { x: (x0 + x1) / 2, y: (y0 + y1) / 2 }
}

/** 节点的选中标记跟 selectedId 走。只换真的变了的那几个对象 */
function withSelection(nodes: FlowNode[], ids: Set<string>): FlowNode[] {
  let changed = false
  const next = nodes.map((n) => {
    const want = ids.has(n.id)
    if (!!n.selected === want) return n
    changed = true
    return { ...n, selected: want }
  })
  return changed ? next : nodes
}

/** 撤销回来的快照：拖动中、选中这些交互态不跟着回来 */
function restoredNodes(nodes: FlowNode[], selectedId: string | null): FlowNode[] {
  return nodes.map((n) => (n.dragging || !!n.selected !== (n.id === selectedId)
    ? { ...n, dragging: false, selected: n.id === selectedId }
    : n))
}

/** 和上次保存的比。撤销回保存时的样子，「未保存」应当消失 */
const graphSig = (nodes: FlowNode[], edges: Edge[]): string => JSON.stringify(toGraph(nodes, edges))

/**
 * 新节点的默认配置里写着 {{ input.question }}。这张图的入口字段不一定叫 question
 * （⑥ 的入口是 goal），照抄就是一个现成的「引用不到」错误。换成真实的第一个字段。
 */
function adaptDefaults(config: Record<string, any>, nodes: FlowNode[]): Record<string, any> {
  const entry = nodes.find((n) => n.data.nodeType === 'input')
  const first = entry?.data.config?.fields?.[0]?.name
  if (!first || first === 'question') return config
  const swap = (v: any): any => {
    if (typeof v === 'string') return v.replace(/input\.question\b/g, `input.${first}`)
    if (Array.isArray(v)) return v.map(swap)
    if (v && typeof v === 'object') return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, swap(x)]))
    return v
  }
  return swap(config)
}

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
  workflowId: string, set: (partial: Partial<StudioState>) => void, get: () => StudioState,
): Promise<string | null> {
  try {
    const existing = await api.conversations.list('canvas', workflowId)
    const id = existing[0]?.id
      ?? (await api.conversations.create({ kind: 'canvas', workflow_id: workflowId })).id
    // 等的这会儿又换了一张图：这条会话不属于眼前的图了
    if (get().workflow?.id !== workflowId) return null
    set({ copilotConversationId: id })
    // 把之前几轮取回来。模型每次改图都带着它们（后端 _history_section），界面上却一片
    // 空白——用户看不见模型记得什么，点了「清空」也以为重新开始了
    if (existing[0]?.turn_count) {
      void api.conversations.get(id).then((detail) => {
        if (get().copilotConversationId !== id) return
        const past = (detail.turns ?? []).filter((t) => t.status !== 'running')
        set({ copilotMemory: memoryOf(past) })
      }).catch(() => undefined)
    }
    return id
  } catch {
    return null   // 记不下来也不该挡着人改图
  }
}

const memoryOf = (past: ConversationTurn[]): StudioState['copilotMemory'] => ({
  past, total: past.length, turns: Math.min(past.length, COPILOT_HISTORY_TURNS),
})

/** 把画布上这一轮记进会话，下一条指令才接得上 */
function recordCanvasTurn(
  conversationId: string | null,
  instruction: string,
  patch: { graph?: any; explanation?: string; answer?: string; status?: 'done' | 'error'; error?: string },
  set?: (fn: (s: StudioState) => Partial<StudioState>) => void,
): void {
  if (!conversationId) return
  void api.conversations.startTurn(conversationId, instruction)
    .then((turn) => api.conversations.patchTurn(conversationId, turn.id, patch))
    .then((turn) => {
      // 模型下一轮会带上它：记忆的轮数跟着涨，界面上的「参考前 N 轮」才是真话
      set?.((s) => s.copilotConversationId === conversationId
        ? { copilotMemory: memoryOf([...s.copilotMemory.past, turn]) }
        : {})
    })
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
  /** 已落库的那些事件。llm.token / llm.thinking.delta 不进来：它们不落库，刷新后也不会回来 */
  events: RunEvent[]
  runtime: Record<string, NodeRuntime>
  /** 此刻在流动的边：只指向正在执行的节点（等待审批的不算）。内容不变时沿用原数组 */
  activeEdges: string[]
  streaming: boolean
  unsubscribe: (() => void) | null

  /** 由事件推出的运行相位。变化时同步写回 run.status（waiting → interrupted） */
  runPhase: RunPhase
  /** 运行航迹：事件 → 航迹 → 投影(t)。画布、胶囊、时间轴、回放都读它 */
  trace: Trace
  /** 实时累加的用量（llm.end）。只用来显示；终态时以后端累计为准校正 */
  usageLive: { tokensIn: number; tokensOut: number; costUsd: number }
  /** 右栏步骤行悬停的节点。卡片用 s => s.hoveredNodeId === id 订阅，别订阅整个字段 */
  hoveredNodeId: string | null
  /** 请画布取景到某个节点；seq 每次 +1，同一个节点连点两次也能再触发 */
  focusRequest: { id: string; seq: number } | null
  /** 回放游标（毫秒时间戳）。null = 实时 */
  replayAt: number | null
  /** 跟随执行：画布跟着正在跑的节点走。用户手动平移后由画布关掉 */
  follow: boolean
  /** 当前高亮的变量血缘：谁产出、谁引用 */
  lineage: { var: string; producers: string[]; consumers: string[] } | null
  /**
   * 发起（或接着跑）这次运行时画布的样子。卡片拿 configSig(当前 config) 和
   * configs[id] 比，不一样就标"结果为旧配置"；structure 对不上说明增删过节点或
   * 连线，接着跑会被后端拒绝。看历史运行时是 null（不知道当时的画布）
   */
  runSnapshot: { configs: Record<string, string>; structure: string } | null

  /** 这张图的 Copilot 会话 id。一张图一条，用来让连续几条改图指令互相知情 */
  copilotConversationId: string | null
  /**
   * 模型此刻记得的上下文。past 是这条会话里已经结束的轮次（包括打开这张图之前的），
   * turns 是下一条指令会带上的轮数（最多 COPILOT_HISTORY_TURNS）。
   * 「开始新对话」换一条会话，这里清零，模型的上下文才真的清零
   */
  copilotMemory: { past: ConversationTurn[]; total: number; turns: number }
  /** 这一轮助手最近动过的节点。画布可以据此跟随镜头、标出「下一步落在这」 */
  copilotCursor: string | null

  // ---- 编辑 ----
  /** 撤销栈：每格是改动之前的画布。新改动清空 future */
  past: HistoryEntry[]
  future: HistoryEntry[]
  /**
   * 校验和变量分析的状态。以前分析失败被吞掉，issues 保持为空，工具栏照样说「可运行」；
   * 首次分析回来之前也会先闪一下「可运行」。没校验过就不该给这个承诺
   */
  analysis: 'idle' | 'pending' | 'ok' | 'failed'
  analysisError: string | null
  /**
   * 画布视口中心的流坐标。由画布（FlowCanvas，ReactFlowProvider 在它里面）注册；
   * 调色板点击添加、粘贴都落在这附近。没注册时为 null，退回图的中心
   */
  getViewportCenter: (() => { x: number; y: number }) | null
  /** 下次保存时写进版本说明的话。恢复旧版本时是「回滚到 vN」 */
  pendingNote: string

  copilot: {
    active: boolean
    lastOp: string
    explanation: string
    error: string
    model: string
    // 从提交到第一个操作之间有 5~30 秒，得让用户看见这段在发生什么
    phase: 'connecting' | 'planning' | 'building' | 'wiring' | 'finalizing' | 'repairing' | ''
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
  /** 打开一张工作流。传 null 是卸下眼前这张：它被删了、又没有别的可换 */
  load: (workflow: Workflow | null) => void
  setGraph: (graph: GraphSpec) => void
  onNodesChange: (changes: NodeChange[]) => void
  onEdgesChange: (changes: EdgeChange[]) => void
  onConnect: (conn: Connection) => void
  /** 不给位置时落在视口中心附近的空地上。新节点自动选中 */
  addNode: (type: NodeType, position?: { x: number; y: number }) => void
  updateNode: (id: string, patch: { label?: string; config?: Record<string, any> }) => void
  removeNode: (id: string) => void
  duplicateNode: (id: string) => void
  /** 复制选中的节点（多选时全部）连同它们之间的边，挨着原位放。返回复制了几个 */
  duplicateSelection: () => number
  /** ⌘C：记下选中的节点和它们之间的边。返回几个 */
  copySelection: () => number
  /** ⌘V：换新 id 粘到视口中心附近。返回几个 */
  pasteClipboard: () => number
  selectAll: () => void
  select: (id: string | null) => void
  /** note 不传时用 pendingNote（恢复旧版本时自动填的那句） */
  save: (note?: string) => Promise<void>
  validate: () => Promise<void>
  analyzeNow: () => Promise<void>
  undo: () => void
  redo: () => void
  /** 版本历史里「恢复这一版」：作为一次可撤销的改动放上画布，保存后才生效 */
  restoreVersion: (version: number, graph: GraphSpec) => void
  /** 悬停变量：算出谁产出、谁引用，写进 lineage。null 清掉 */
  traceVariable: (path: string | null) => void

  startRun: (input: Record<string, any>) => Promise<Run | null>
  startFormalRun: (input: Record<string, any>) => Promise<Run | null>
  runCopilot: (instruction: string, useBase: boolean, model?: string | null) => void
  /** 停掉这一轮，画布退回这一轮之前（半成品进 future，⇧⌘Z 能找回来） */
  stopCopilot: () => void
  retryCopilot: () => void
  /** 旧名字：现在等于 newCopilotConversation。只清界面不换会话，模型照样带着旧上下文 */
  clearCopilot: () => void
  /** 换一条新会话：模型的上下文真的清零。旧会话保留在后端 */
  newCopilotConversation: () => Promise<void>
  /**
   * 让助手再修一次：把这一轮自查剩下的问题、少掉的步骤（没给 turnId 时用最后一轮；
   * 都没有就用画布当前的校验错误）拼成指令，在现有的图上改
   */
  repairWithCopilot: (turnId?: string) => void
  /** 撤销这一轮助手的改动。只有它还在撤销栈顶时能撤，返回撤没撤 */
  undoCopilotTurn: (turnId: string) => boolean
  attachRun: (runId: string) => Promise<void>
  /** 失败的运行从断点接着跑，把画布上改过的配置一起带过去 */
  continueRun: () => Promise<void>
  /** 失败时抛出（多半是 409：运行其实已经不在跑了），调用方负责提示；同时会对一次账 */
  stopRun: () => Promise<void>
  /** 清掉这次运行的一切痕迹，画布回到编辑态 */
  clearRun: () => void
  applyEvent: (event: RunEvent) => void
  /** GET /runs/{id} 对一次账：漏了终态事件（断线、服务被强杀）时靠它收尾 */
  reconcileRun: () => Promise<void>
  setHoveredNode: (id: string | null) => void
  focusNode: (id: string) => void
  setReplayAt: (at: number | null) => void
  setFollow: (follow: boolean) => void
  setLineage: (lineage: StudioState['lineage']) => void
}

const EMPTY_RUNTIME: NodeRuntime = { status: 'idle' }

// -------------------------------------------------------------------------
// 运行态：事件 → 航迹 → store 的其余字段
// -------------------------------------------------------------------------

const ZERO_USAGE: StudioState['usageLive'] = { tokensIn: 0, tokensOut: 0, costUsd: 0 }

export const configSig = (config: Record<string, any> | undefined): string =>
  JSON.stringify(config ?? {})

/** 骨架：节点 id 和连线。和后端 continue 判断"同一张图"的口径一致——只看结构不看配置 */
export function structureSig(nodes: FlowNode[], edges: Edge[]): string {
  const ids = nodes.map((n) => n.id).sort().join(',')
  const links = edges.map((e) => `${e.source}>${e.target}:${e.sourceHandle ?? ''}`).sort().join(',')
  return `${ids}|${links}`
}

function snapshotOf(nodes: FlowNode[], edges: Edge[]): StudioState['runSnapshot'] {
  return {
    configs: Object.fromEntries(nodes.map((n) => [n.id, configSig(n.data.config)])),
    structure: structureSig(nodes, edges),
  }
}

/** 开始一次新运行、换一条运行、清掉结果时要一起清的运行态 */
function runReset(): Partial<StudioState> {
  return {
    events: [], runtime: {}, activeEdges: [], trace: emptyTrace(), usageLive: ZERO_USAGE,
    runPhase: 'idle', replayAt: null, follow: true, runSnapshot: null,
  }
}

function seedTrace(run: Run): Trace {
  return {
    ...emptyTrace(),
    phase: run.status === 'queued' ? 'queued' : 'idle',
    runClass: run.run_class,
  }
}

/** 推导出来的几种（排队、阻断、没走到）不是 runtime 的状态，卡片从航迹读 */
function runtimeStatusOf(state: NodeState): NodeRuntime['status'] {
  switch (state) {
    case 'idle': case 'queued': case 'blocked': case 'unreached': return 'idle'
    default: return state
  }
}

const sameIds = (a: string[], b: string[]): boolean =>
  a.length === b.length && a.every((x, i) => x === b[i])

const num = (v: unknown): number | undefined =>
  typeof v === 'number' && Number.isFinite(v) ? v : undefined

/**
 * 一条事件（或一次对账）落进航迹之后，把航迹派生到 store 的其余字段上：
 * 节点 runtime 的状态、协作团队的收尾、活跃边、相位（写回 run.status）、
 * 实时用量、streaming。applyEvent 和对账走同一条路，免得两处收尾口径不一。
 *
 * runtime 由调用方先复制好（applyEvent 还要往里写正文、工具调用这些）。
 */
function advance(
  s: StudioState, runtime: Record<string, NodeRuntime>, event: RunEvent,
): Partial<StudioState> {
  const folded = foldEvent(s.trace, event)
  if (folded === s.trace) return {}
  const graph = { nodes: s.nodes, edges: s.edges }
  let trace = isSettled(folded.phase) ? finalizeTrace(folded, graph) : applyDerived(folded, graph)

  // 客户端和服务端的时钟差（见 liveAt）。补发的历史不算：它们的 ts 早就过去了
  const ts = num(event.ts)
  if (ts != null && !event.replay && event.type !== 'stream.end') {
    const skew = Date.now() - ts * 1000
    if (trace.skewMs == null || skew < trace.skewMs) trace = { ...trace, skewMs: skew }
  }

  // 节点状态只有航迹一个来源；runtime 上的正文、工具调用这些由 applyEvent 管
  for (const [id, n] of Object.entries(trace.nodes)) {
    const cur = runtime[id]
    const status = runtimeStatusOf(n.state)
    if (!cur && status === 'idle') continue
    if (cur?.status !== status || cur?.iteration !== n.iteration) {
      runtime[id] = { ...(cur ?? EMPTY_RUNTIME), status, iteration: n.iteration }
    }
  }
  // 协作团队的"进行中"随结局一起收：只收节点的话，矩阵里那几行会一直转
  const ending = endingOf(event, s.trace.book.awaiting)
  if (ending) {
    for (const [id, r] of Object.entries(runtime)) {
      if (!r.team) continue
      const team = settleTeam(r.team, ending)
      if (team !== r.team) runtime[id] = { ...r, team }
    }
  }

  const edges = isSettled(trace.phase) ? [] : activeEdgesOf(trace, graph)
  const activeEdges = sameIds(edges, s.activeEdges) ? s.activeEdges : edges

  let run = s.run
  if (run && trace.phase !== s.runPhase) {
    const status = runStatusOf(trace.phase)
    if (status && status !== run.status) run = { ...run, status }
  }
  const type = String(event.type)
  if (run && (type === 'run.finished' || type === 'run.failed' || type === 'run.cancelled')) {
    // 以前 run 一直是 POST /runs 返回的 queued 快照：失败了"接着跑"出不来，
    // 底栏的用量永远是空的。终态事件里有的都并进去，完整的再由对账补
    const d = event.data ?? {}
    const timing = d.timing ?? {}
    run = {
      ...run,
      ...(type === 'run.failed' && d.error ? { error: String(d.error) } : {}),
      ...(type === 'run.finished' && d.output ? { output: d.output } : {}),
      usage: {
        ...run.usage,
        ...(num(d.duration_ms) != null ? { duration_ms: d.duration_ms } : {}),
        ...(d.usage ?? {}),
        ...(num(timing.wall_ms) != null ? { wall_ms: timing.wall_ms } : {}),
        ...(num(timing.active_ms) != null ? { active_ms: timing.active_ms } : {}),
        ...(num(timing.wait_ms) != null ? { wait_ms: timing.wait_ms } : {}),
      },
    }
  }

  const u = s.usageLive
  const usageLive = u.tokensIn === trace.tokensIn && u.tokensOut === trace.tokensOut
    && u.costUsd === trace.costUsd
    ? u : { tokensIn: trace.tokensIn, tokensOut: trace.tokensOut, costUsd: trace.costUsd }

  // 终态、挂起就不再"在跑"。恢复后事件又来了（别处点了接着跑）而订阅还在，就重新亮起
  const streaming = isSettled(trace.phase) ? false
    : isActivePhase(trace.phase) && s.unsubscribe ? true : s.streaming

  return { runtime, trace, activeEdges, runPhase: trace.phase, run, usageLive, streaming }
}

/** 每接上一条事件流自增。对账回来时对不上，说明在途时又开了新的流（审批恢复、接着跑） */
let streamEpoch = 0

/**
 * 对账：GET /runs/{id}，必要时查一下有没有待审批，把结论当成一条合成的
 * stream.end 折进航迹。
 *
 * 收敛不能只靠事件：服务被强杀时连 server_shutdown 都不会发，WS 会一直重连；
 * 用户点停止撞上 409，也说明它其实早就不在跑了。这时候画布上的"运行中"
 * 全是假的，得问后端一次。顺带拿到完整的成果和后端累计的用量。
 */
async function reconcile(
  runId: string, set: (partial: Partial<StudioState>) => void, get: () => StudioState,
): Promise<void> {
  // 请求在途时用户可能已经批了、点了接着跑：事件又动了，或者接上了新的事件流。
  // 这时候手上的快照是旧的，折进去会把刚恢复的运行又收成挂起 / 失败（续跑、恢复
  // 不换 run id，光比 id 拦不住）。直接作废，新的那条流结束时会再对一次账
  const seq0 = get().trace.lastSeq
  const epoch0 = streamEpoch
  let fresh: Run
  try {
    fresh = await api.runs.get(runId)
  } catch {
    return
  }
  let pending: boolean | undefined
  if (fresh.status === 'interrupted') {
    try {
      // 按 run_id 查：全局列表只给最新的 100 条，更早的那条审批会被当成没有
      pending = (await api.approvals.list({ run_id: runId, status: 'pending' })).length > 0
      // 没有待审批时再读一次状态。批准和"恢复成 running"是同一个事务落库的：
      // 第一次读到 interrupted 之后有人批了，这里读到的就是 running，而不是挂起
      if (!pending) fresh = await api.runs.get(runId)
    } catch {
      /* 查不到就按事件推的算 */
      pending = undefined
    }
  }
  const s = get()
  if (s.run?.id !== runId || s.trace.lastSeq !== seq0 || streamEpoch !== epoch0) return
  // 结局已经由事件收过的话 advance 什么都不改，这里只把后端那份 run 换上
  const next = advance(s, { ...s.runtime }, {
    seq: 0, type: 'stream.end', node_id: null, ts: s.trace.book.clock / 1000,
    data: { status: fresh.status, ...(pending != null ? { pending } : {}) },
  })
  let trace = next.trace ?? s.trace
  const u = fresh.usage ?? {}
  if (isTerminal(trace.phase)) {
    // 后端累计的才是权威总数（老后端的 agent / 协作调用不发 llm.end，实时累加会少）
    const tokensIn = num(u.input_tokens) ?? trace.tokensIn
    const tokensOut = num(u.output_tokens) ?? trace.tokensOut
    const costUsd = num(u.cost_usd) ?? trace.costUsd
    if (tokensIn !== trace.tokensIn || tokensOut !== trace.tokensOut || costUsd !== trace.costUsd) {
      trace = { ...trace, tokensIn, tokensOut, costUsd }
    }
  }
  const live = s.usageLive
  set({
    ...next,
    trace,
    usageLive: live.tokensIn === trace.tokensIn && live.tokensOut === trace.tokensOut
      && live.costUsd === trace.costUsd
      ? live : { tokensIn: trace.tokensIn, tokensOut: trace.tokensOut, costUsd: trace.costUsd },
    run: { ...fresh, status: runStatusOf(trace.phase) ?? fresh.status },
  })
}

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

async function runAnalysis(
  set: (partial: Partial<StudioState>) => void, get: () => StudioState, force = false,
): Promise<void> {
  const { nodes, edges } = get()
  if (!nodes.length) {
    set({ issues: [], variables: [], varIssues: [], analysis: 'ok', analysisError: null })
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
    set({ issues: validation.issues ?? [], analysis: 'ok', analysisError: null })
    if (vars) {
      lastVarSignature = signature
      set({ variables: vars.variables ?? [], varIssues: vars.issues ?? [] })
    }
  } catch (e) {
    // 分析失败不挡编辑，但也不能装作校验过了：工具栏要说「校验不可用」，
    // 而不是拿着空的 issues 说「可运行」
    if (epoch !== analyzeEpoch) return
    const h = humanizeError(e)
    set({ analysis: 'failed', analysisError: h.reason ? `${h.title}：${h.reason}` : h.title })
    // 变量表签名没记下：恢复之后要重新算一次，而不是以为已经算过了
    lastVarSignature = ''
  }
}

type SetFn = (partial: Partial<StudioState> | ((s: StudioState) => Partial<StudioState>)) => void

/**
 * 记一步：把此刻的画布压进撤销栈，然后调用方再改。
 *
 * key + mergeMs 用来把「一件事」合成一步：连续打字（同一个节点同一组字段）、
 * 一次删除（React Flow 先发边的 remove，再发节点的 remove，同一个 tick 里）。
 */
function commit(
  set: SetFn, get: () => StudioState, label: string,
  opts: { key?: string; mergeMs?: number; nodes?: FlowNode[]; edges?: Edge[] } = {},
): HistoryEntry | null {
  const s = get()
  const now = Date.now()
  const top = s.past[s.past.length - 1]
  if (opts.key && top?.key === opts.key && now - top.at < (opts.mergeMs ?? TYPING_MERGE_MS)) {
    // 同一步的延续：只续上时间窗，快照还是这一串开始之前的那份
    set({ past: [...s.past.slice(0, -1), { ...top, at: now }], future: [] })
    return null
  }
  const entry: HistoryEntry = {
    id: ++historySeq, label, at: now, key: opts.key,
    nodes: opts.nodes ?? s.nodes, edges: opts.edges ?? s.edges,
  }
  set({ past: [...s.past, entry].slice(-HISTORY_LIMIT), future: [] })
  return entry
}

/** 画布锁着：助手正在改这张图，这时候的手动编辑会被它的最终结果悄悄覆盖 */
const locked = (get: () => StudioState) => get().copilot.active

/** 撤销 / 重做之后：选中的节点没了就收起检查器，未保存标记按内容重算 */
function afterJump(set: SetFn, get: () => StudioState, nodes: FlowNode[], edges: Edge[]) {
  glideSeq++
  const sel = get().selectedId
  const selectedId = sel && nodes.some((n) => n.id === sel) ? sel : null
  set({
    nodes: restoredNodes(nodes, selectedId), edges, selectedId,
    dirty: graphSig(nodes, edges) !== savedSig,
  })
  void get().validate()
}

/** 上次保存（或打开）时的图。撤销回这个样子时「未保存」要消失 */
let savedSig = ''

/**
 * 把一组节点从现在的位置滑到 targets，约 320ms，结束时 done()。
 *
 * 走的是 store 里的 position，不是给卡片加 CSS transform 过渡：走线由 buildRoutes 按
 * store 里的坐标算，只动卡片的话线会先跳到终点、和卡片脱节，看着像渲染出错。每帧只
 * 换真在动的那几个节点对象；量尺寸、选中这些别的变化照常进来，不会被这一帧盖掉
 */
function glide(
  set: SetFn, get: () => StudioState, targets: Map<string, { x: number; y: number }>, done: () => void,
) {
  const seq = ++glideSeq
  const from = new Map<string, { x: number; y: number }>()
  for (const n of get().nodes) {
    const to = targets.get(n.id)
    if (to && (n.position.x !== to.x || n.position.y !== to.y)) from.set(n.id, n.position)
  }
  if (!from.size) { done(); return }
  const t0 = performance.now()
  const frame = (now: number) => {
    if (seq !== glideSeq) return
    const k = Math.min(1, (now - t0) / GLIDE_MS)
    const e = 1 - (1 - k) ** 3
    set({
      nodes: get().nodes.map((n) => {
        const a = from.get(n.id)
        const b = targets.get(n.id)
        // 用户已经抓住它在拖：归他
        if (!a || !b || n.dragging) return n
        return { ...n, position: k >= 1 ? b : { x: a.x + (b.x - a.x) * e, y: a.y + (b.y - a.y) * e } }
      }),
    })
    if (k < 1) requestAnimationFrame(frame)
    else done()
  }
  requestAnimationFrame(frame)
}

/** 删掉的节点叫什么：toast 里要说出名字，「已删除 3 个节点」不如「已删除「查询销量」」具体 */
function deletedToast(get: () => StudioState, removed: FlowNode[]) {
  if (!removed.length) return
  const text = removed.length === 1
    ? `已删除「${removed[0].data.label || removed[0].id}」`
    : `已删除 ${removed.length} 个节点`
  toast(text, 'info', { key: 'studio:deleted', action: { label: '撤销', onClick: () => get().undo() } })
}

/**
 * 把一组节点（和它们之间的边）换新 id 放进画布。复制粘贴、⌘D 共用。
 * 整组平移到 anchor 附近一块不压住别人的空地，组内相对位置不变。
 */
function cloneInto(
  set: SetFn, get: () => StudioState, src: { nodes: FlowNode[]; edges: Edge[] },
  anchor: { x: number; y: number }, label: string,
): number {
  if (!src.nodes.length || locked(get)) return 0
  const { nodes, edges } = get()
  const x0 = Math.min(...src.nodes.map((n) => n.position.x))
  const y0 = Math.min(...src.nodes.map((n) => n.position.y))
  const w = Math.max(...src.nodes.map((n) => n.position.x + (n.measured?.width ?? CARD_W))) - x0
  const h = Math.max(...src.nodes.map((n) => n.position.y + (n.measured?.height ?? CARD_H))) - y0
  const spot = freeSpot(nodes, anchor, { w, h })
  const ids = new Map<string, string>()
  const fresh: FlowNode[] = src.nodes.map((n) => {
    const id = nextId(n.data.nodeType)
    ids.set(n.id, id)
    return {
      id, type: 'card', selected: true,
      position: { x: spot.x + n.position.x - x0, y: spot.y + n.position.y - y0 },
      data: { ...n.data, config: structuredClone(n.data.config ?? {}) },
    }
  })
  const links: Edge[] = src.edges
    .filter((e) => ids.has(e.source) && ids.has(e.target))
    .map((e) => {
      const source = ids.get(e.source)!
      const target = ids.get(e.target)!
      return {
        id: `e_${source}_${e.sourceHandle ?? ''}_${target}`, source, target,
        sourceHandle: e.sourceHandle ?? null, targetHandle: e.targetHandle ?? null,
        label: e.label, type: 'flow',
      }
    })
  commit(set, get, label)
  const selectedId = fresh.length === 1 ? fresh[0].id : null
  set({
    nodes: [...withSelection(nodes, new Set()), ...fresh],
    edges: [...edges.map((e) => (e.selected ? { ...e, selected: false } : e)), ...links],
    selectedId, dirty: true,
  })
  void get().validate()
  return fresh.length
}

/** 选中的那几个：React Flow 的多选，没有就是检查器里开着的那一个 */
function selectionOf(s: StudioState): FlowNode[] {
  const picked = s.nodes.filter((n) => n.selected)
  if (picked.length) return picked
  const one = s.nodes.find((n) => n.id === s.selectedId)
  return one ? [one] : []
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
  runPhase: 'idle',
  trace: emptyTrace(),
  usageLive: ZERO_USAGE,
  hoveredNodeId: null,
  focusRequest: null,
  replayAt: null,
  follow: true,
  lineage: null,
  runSnapshot: null,
  copilotConversationId: null,
  copilotMemory: { past: [], total: 0, turns: 0 },
  copilotCursor: null,
  copilot: { active: false, lastOp: '', explanation: '', error: '', model: '',
             phase: '', thinking: '', elapsedMs: 0, lastInstruction: '', lastUseBase: false },
  copilotNew: [],
  copilotTurns: [],
  cancelCopilot: null,
  fitRequest: 0,
  past: [],
  future: [],
  analysis: 'idle',
  analysisError: null,
  getViewportCenter: null,
  pendingNote: '',

  load: (workflow) => {
    const { nodes, edges } = workflow ? toFlow(workflow.graph) : { nodes: [] as FlowNode[], edges: [] as Edge[] }
    get().unsubscribe?.()
    // 上一张图的生成还在跑的话先掐掉：它的操作流会落到这张图上
    activeTurn?.abandon()
    activeTurn = null
    get().cancelCopilot?.()
    dragOrigin = null
    glideSeq++
    savedSig = graphSig(nodes, edges)
    lastVarSignature = ''
    set({
      workflow, nodes, edges, selectedId: null, dirty: false, issues: [],
      // 变量表也清掉：不清的话切图后抽屉会先显示上一张图的变量
      variables: [], varIssues: [], analysis: workflow ? 'pending' : 'idle', analysisError: null,
      ...runReset(), run: null, streaming: false, unsubscribe: null,
      hoveredNodeId: null, focusRequest: null, lineage: null,
      // 整张图换了，视角也要跟着换。不换的话打开另一张图看到的还是上一张的
      // 那块空白——画布位置是视口的，不是图的
      fitRequest: get().fitRequest + 1,
      // 换了一张图，之前那些"我让它改成这样"就不再指向眼前这张图了
      copilotTurns: [],
      copilotNew: [],
      copilotCursor: null,
      copilot: { ...get().copilot, active: false, phase: '', thinking: '', lastOp: '', error: '' },
      cancelCopilot: null,
      // 先清空再去解析：留着上一张图的会话 id，这中间的任何一条指令
      // 都会带着别的图的上下文发出去
      copilotConversationId: null,
      copilotMemory: { past: [], total: 0, turns: 0 },
      // 撤销栈属于这张图：撤回去撤出另一张图的节点，比没有撤销更糟
      past: [], future: [], pendingNote: '',
    })
    if (!workflow) return
    void ensureCanvasConversation(workflow.id, set, get)
    void get().analyzeNow()
  },

  setGraph: (graph) => {
    if (locked(get)) return
    const { nodes, edges } = toFlow(graph)
    glideSeq++
    commit(set, get, '替换整个画布')
    set({ nodes, edges, selectedId: null, dirty: true, fitRequest: get().fitRequest + 1 })
    void get().validate()
  },

  onNodesChange: (changes) => {
    if (locked(get)) {
      // 锁定期间只放行量尺寸和选中：前者是 React Flow 自己的，后者不改图
      changes = changes.filter((c) => c.type === 'dimensions' || c.type === 'select')
      if (!changes.length) return
    }
    const s = get()
    const removing = changes.filter((c) => c.type === 'remove').map((c) => (c as { id: string }).id)
    const moving = changes.filter((c) => c.type === 'position')
    if (removing.length) commit(set, get, '删除', { key: 'delete', mergeMs: 250 })
    if (moving.some((c) => (c as { dragging?: boolean }).dragging) && !dragOrigin) {
      dragOrigin = { nodes: s.nodes, edges: s.edges }
    }
    const dragEnded = moving.some((c) => (c as { dragging?: boolean }).dragging === false)
    if (!dragOrigin && moving.length && !dragEnded
        && moving.every((c) => (c as { dragging?: boolean }).dragging === undefined)) {
      // 键盘方向键挪节点：没有拖动的起止，连续挪算一步
      commit(set, get, '移动节点', { key: 'nudge' })
    }
    if (changes.some((c) => c.type === 'add' || c.type === 'replace')) commit(set, get, '改动节点')

    const nodes = applyNodeChanges(changes, get().nodes) as FlowNode[]
    const patch: Partial<StudioState> = { nodes }

    if (dragEnded && dragOrigin) {
      const origin = dragOrigin
      dragOrigin = null
      // 只是点了一下（没真挪）不记
      const moved = nodes.some((n) => {
        const o = origin.nodes.find((x) => x.id === n.id)
        return o && (Math.round(o.position.x) !== Math.round(n.position.x)
          || Math.round(o.position.y) !== Math.round(n.position.y))
      })
      if (moved) commit(set, get, '移动节点', origin)
    }

    // React Flow 自己的选中（点选、框选、Shift 多选）回写 selectedId：单选就打开检查器，
    // 多选就收起——检查器一次只能编辑一个
    if (changes.some((c) => c.type === 'select')) {
      const picked = nodes.filter((n) => n.selected)
      if (picked.length === 1 && s.selectedId !== picked[0].id) patch.selectedId = picked[0].id
      else if (picked.length > 1 && s.selectedId) patch.selectedId = null
    }
    if (removing.length) {
      if (s.selectedId && removing.includes(s.selectedId)) patch.selectedId = null
      deletedToast(get, s.nodes.filter((n) => removing.includes(n.id)))
    }
    // 只有拖动结束和增删才算改动，实时拖动不标脏，否则自动保存会疯狂触发
    if (dragEnded || removing.length || changes.some((c) => c.type === 'add' || c.type === 'replace')) {
      patch.dirty = true
    }
    set(patch)
    // 孤立节点被删时 React Flow 不会顺带删边，以前这时不会重新校验，工具栏的计数会滞后
    if (removing.length) void get().validate()
  },

  onEdgesChange: (changes) => {
    if (locked(get)) {
      changes = changes.filter((c) => c.type === 'select')
      if (!changes.length) return
    }
    const structural = changes.some((c) => c.type !== 'select')
    if (changes.some((c) => c.type === 'remove')) commit(set, get, '删除', { key: 'delete', mergeMs: 250 })
    else if (structural) commit(set, get, '改动连线')
    set({ edges: applyEdgeChanges(changes, get().edges), ...(structural ? { dirty: true } : {}) })
    if (structural) void get().validate()
  },

  onConnect: (conn) => {
    if (locked(get)) return
    commit(set, get, '连线')
    set({
      edges: addEdge({ ...conn, type: 'flow' }, get().edges),
      dirty: true,
    })
    void get().validate()
  },

  addNode: (type, position) => {
    if (locked(get)) return
    const def = NODE_DEFS[type]
    if (!def) return
    const { nodes } = get()
    let at = position
    if (!at) {
      // 视口中心是卡片的中心，不是左上角
      const c = get().getViewportCenter?.() ?? centerOf(nodes)
      at = freeSpot(nodes, { x: c.x - CARD_W / 2, y: c.y - CARD_H / 2 })
    }
    const node: FlowNode = {
      id: nextId(type),
      type: 'card',
      position: at,
      selected: true,
      data: {
        nodeType: type, label: def.label,
        config: adaptDefaults(structuredClone(def.defaults ?? {}), nodes),
      },
    }
    commit(set, get, `添加「${def.label}」`)
    set({
      nodes: [...withSelection(nodes, new Set()), node], selectedId: node.id, dirty: true,
    })
    void get().validate()
  },

  updateNode: (id, patch) => {
    if (locked(get)) return
    const target = get().nodes.find((n) => n.id === id)
    if (!target) return
    // 连续改同一个节点的同一组字段（打字）合成一步：一个字一步的撤销没法用
    const keys = [
      ...(patch.label !== undefined && patch.label !== target.data.label ? ['label'] : []),
      ...(patch.config !== undefined
        ? Object.keys({ ...target.data.config, ...patch.config })
          // 不用 configSig：它把「没有这个键」和 {} 算成一样，这里要分得清
          .filter((k) => JSON.stringify(target.data.config?.[k]) !== JSON.stringify(patch.config?.[k]))
        : []),
    ]
    // 什么都没变就什么都不做：JSON 字段里删个空格会原样再发一遍同一个对象，
    // 以前照样记一步撤销、亮「未保存」，撤回去却看不出任何变化
    if (!keys.length) return
    commit(set, get, `编辑「${target.data.label || id}」`, {
      key: `edit:${id}:${keys.sort().join(',')}`,
    })
    // 出口 handle 是从 config 算出来的（分支的 case key、human 的 mode），
    // 改 config 就可能让已连好的边指向一个不存在的出口。React Flow 对这种边
    // 只在 console 打一条 008 警告然后不渲染——它看不见、点不到、删不掉，
    // 却照样被 toGraph 序列化存盘。所以这里同步把边跟过去。
    const before = sourceHandles(target.data.nodeType, target.data.config ?? {})
    const after = patch.config !== undefined ? sourceHandles(target.data.nodeType, patch.config) : before

    let edges = get().edges
    if (before.length && after !== before) {
      const alive = new Set(after.map((h) => h.id))
      // 改名场景（出口数量没变）按位置重映射，这样连好的线不会白连
      const byPosition = new Map<string, string>()
      if (before.length === after.length) {
        before.forEach((h, i) => {
          if (h.id !== after[i].id) byPosition.set(h.id, after[i].id)
        })
      }
      // 分支按 case 的下标对应：同一个 case 改了标识，线跟着这个 case 走。按出口位置对不上
      // 的情形：key=default 的 case 和「其他」是合并成一个出口的，改名之后出口多了一个；
      // 这时 default 出口还在，线要是留在「其他」上，而那个 case 的条件又总是成立，
      // 运行就会走进一个没有线的出口
      const byCase = new Map<string, string>()
      const was = target.data.config?.cases
      const now = patch.config?.cases
      if (target.data.nodeType === 'branch' && Array.isArray(was) && Array.isArray(now) && was.length === now.length) {
        was.forEach((c: any, i: number) => {
          const a = String(c?.key ?? '').trim()
          const b = String(now[i]?.key ?? '').trim()
          if (a && b && a !== b && alive.has(b)) byCase.set(a, b)
        })
      }
      edges = edges.flatMap((e) => {
        if (e.source !== id || !e.sourceHandle) return [e]
        const follow = byCase.get(e.sourceHandle)
        if (follow) return [{ ...e, sourceHandle: follow }]
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
    if (locked(get)) return
    const gone = get().nodes.find((n) => n.id === id)
    if (!gone) return
    commit(set, get, `删除「${gone.data.label || id}」`)
    set({
      nodes: get().nodes.filter((n) => n.id !== id),
      edges: get().edges.filter((e) => e.source !== id && e.target !== id),
      selectedId: get().selectedId === id ? null : get().selectedId,
      dirty: true,
    })
    deletedToast(get, [gone])
    void get().validate()
  },

  duplicateNode: (id) => {
    const source = get().nodes.find((n) => n.id === id)
    if (!source) return
    cloneInto(set, get, { nodes: [source], edges: [] },
      { x: source.position.x + 40, y: source.position.y + 40 }, `复制「${source.data.label || id}」`)
  },

  duplicateSelection: () => {
    const picked = selectionOf(get())
    if (!picked.length) return 0
    const ids = new Set(picked.map((n) => n.id))
    const x = Math.min(...picked.map((n) => n.position.x)) + 40
    const y = Math.min(...picked.map((n) => n.position.y)) + 40
    return cloneInto(set, get, {
      nodes: picked, edges: get().edges.filter((e) => ids.has(e.source) && ids.has(e.target)),
    }, { x, y }, picked.length === 1 ? `复制「${picked[0].data.label}」` : `复制 ${picked.length} 个节点`)
  },

  copySelection: () => {
    const picked = selectionOf(get())
    if (!picked.length) return 0
    const ids = new Set(picked.map((n) => n.id))
    clipboard = {
      nodes: picked.map((n) => ({ ...n, data: { ...n.data, config: structuredClone(n.data.config ?? {}) } })),
      edges: get().edges.filter((e) => ids.has(e.source) && ids.has(e.target)),
    }
    return picked.length
  },

  pasteClipboard: () => {
    if (!clipboard) return 0
    const { nodes } = get()
    const c = get().getViewportCenter?.() ?? centerOf(nodes)
    const n = clipboard.nodes.length
    return cloneInto(set, get, clipboard, { x: c.x - CARD_W / 2, y: c.y - CARD_H / 2 },
      n === 1 ? `粘贴「${clipboard.nodes[0].data.label}」` : `粘贴 ${n} 个节点`)
  },

  selectAll: () => {
    const { nodes } = get()
    set({ nodes: withSelection(nodes, new Set(nodes.map((n) => n.id))), selectedId: null })
  },

  // 以 selectedId 为准，同时改写节点上的 selected：卡片的选中环看的是 React Flow 的
  // selected。以前两者从不同步，检查器在编辑新节点，选中环却留在旧节点上
  select: (id) => {
    const { nodes, selectedId } = get()
    const next = withSelection(nodes, new Set(id ? [id] : []))
    if (next === nodes && selectedId === id) return
    set({ selectedId: id, nodes: next })
  },

  save: async (note) => {
    const { workflow, nodes, edges, pendingNote } = get()
    if (!workflow) return
    const graph = toGraph(nodes, edges)
    const text = (note ?? pendingNote).trim()
    const updated = await api.workflows.update(workflow.id, { graph, ...(text ? { note: text } : {}) })
    savedSig = JSON.stringify(graph)
    // 请求在途时又改了的话，那部分还没存：不能把「未保存」清掉
    set({ workflow: updated, dirty: graphSig(get().nodes, get().edges) !== savedSig, pendingNote: '' })
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
    const { nodes, analysis } = get()
    if (!nodes.length) {
      set({ issues: [], variables: [], varIssues: [], analysis: 'ok', analysisError: null })
      return
    }
    // 已经有一份结论时不翻回「校验中」：连续打字会让工具栏的标签一直闪。
    // 还没结论（刚打开）或者上次失败了，才说「校验中」
    if (analysis !== 'ok' && analysis !== 'pending') set({ analysis: 'pending' })
    if (analyzeTimer !== null) clearTimeout(analyzeTimer)
    analyzeTimer = setTimeout(() => void runAnalysis(set, get), ANALYZE_DEBOUNCE_MS)
  },

  /** 不等防抖，立刻算一次。抽屉打开、切换工作流、失败后重试这类明确动作用它。 */
  analyzeNow: async () => {
    if (analyzeTimer !== null) clearTimeout(analyzeTimer)
    analyzeTimer = null
    if (get().analysis === 'failed') set({ analysis: 'pending' })
    await runAnalysis(set, get, true)
  },

  undo: () => {
    const s = get()
    if (locked(get) || !s.past.length) return
    const entry = s.past[s.past.length - 1]
    dragOrigin = null
    set({
      past: s.past.slice(0, -1),
      future: [...s.future, { ...entry, nodes: s.nodes, edges: s.edges, at: Date.now(), key: undefined }],
    })
    afterJump(set, get, entry.nodes, entry.edges)
  },

  redo: () => {
    const s = get()
    if (locked(get) || !s.future.length) return
    const entry = s.future[s.future.length - 1]
    dragOrigin = null
    set({
      future: s.future.slice(0, -1),
      past: [...s.past, { ...entry, nodes: s.nodes, edges: s.edges, at: Date.now(), key: undefined }],
    })
    afterJump(set, get, entry.nodes, entry.edges)
  },

  restoreVersion: (version, graph) => {
    if (locked(get)) return
    const { nodes, edges } = toFlow(graph)
    glideSeq++
    commit(set, get, `恢复到 v${version}`)
    set({
      nodes, edges, selectedId: null, fitRequest: get().fitRequest + 1,
      dirty: graphSig(nodes, edges) !== savedSig,
      // 恢复不直接调后端的 restore：那个接口不会把受管 / 已发布退回草稿，一张受管
      // 工作流恢复成未过闸的旧图后，工具栏仍会显示「受管」。走普通保存（PATCH）
      // 就会按规矩退回草稿，版本说明里写清是从哪一版回来的
      pendingNote: `回滚到 v${version}`,
    })
    void get().validate()
  },

  traceVariable: (path) => {
    const cur = get().lineage
    if (!path) {
      if (cur) set({ lineage: null })
      return
    }
    if (cur?.var === path) return
    const v = get().variables.find((x) => x.path === path)
    if (!v) {
      if (cur) set({ lineage: null })
      return
    }
    const producers = v.produced_by ? [v.produced_by] : []
    const consumers = [...new Set(v.refs.map((r) => r.node_id))].filter((id) => !producers.includes(id))
    set({ lineage: { var: path, producers, consumers } })
  },

  // ---- 运行 ----

  startRun: async (input) => {
    const { workflow, nodes, edges } = get()
    get().unsubscribe?.()
    // 助手的「新节点」高亮让位给运行态：紫框留着会盖住运行中、失败的边框
    set({ ...runReset(), run: null, unsubscribe: null, copilotNew: [] })
    try {
      const run = await api.runs.start({
        workflow_id: workflow?.id,
        graph: toGraph(nodes, edges),
        input,
      })
      const trace = seedTrace(run)
      set({ run, streaming: true, trace, runPhase: trace.phase, runSnapshot: snapshotOf(nodes, edges) })
      await get().attachRun(run.id)
      return run
    } catch (e) {
      set({ streaming: false })
      throw e
    }
  },

  runCopilot: (instruction, useBase, model) => {
    // 上一轮还在跑：先按停止收掉，免得两条流一起往画布上写
    if (get().copilot.active) get().stopCopilot()
    glideSeq++
    const state = get()
    const turnId = `c${Date.now().toString(36)}${(idSeq++ % 1000).toString(36)}`
    const before = { nodes: state.nodes, edges: state.edges }
    // 整轮算一步：这一轮开始之前的画布就是撤销点。以前「重新生成」一提交就清空画布，
    // 生成到一半停下、改坏了，都只能靠不保存再刷新来挽回
    const checkpoint = commit(set, get, `助手：${instruction.length > 24 ? instruction.slice(0, 24) + '…' : instruction}`)!
    /** 这一轮是不是已经收尾了（final / reply / error / 停止 / 断流，先到先算） */
    let finished = false
    /** 从头生成：收到第一个节点再清空画布。只回一句话、一开始就失败时，画布不该被清掉 */
    let cleared = useBase
    /** 已经挨着上游摆好的新节点。没等到入边就先落了位的，入边到了再挪过去 */
    const anchored = new Set<string>()
    /** 前端认不出来、没放上画布的节点类型。后端 final.issues 没说到的，收尾时补上，不能安静地少一步 */
    const dropped: string[] = []
    const withDropped = (raw: unknown): CopilotIssue[] => {
      const issues: CopilotIssue[] = Array.isArray(raw) ? [...raw] : []
      for (const type of dropped) {
        if (issues.some((i) => i.code === 'unknown_node_type' && i.type === type)) continue
        issues.push({ level: 'warning', node_id: null, code: 'unknown_node_type', type,
                      message: `模型写了一个不存在的节点类型「${type}」，这一步已跳过` })
      }
      return issues
    }

    set({
      copilot: {
        active: true, lastOp: '', explanation: '', error: '', model: model ?? '',
        phase: 'connecting', thinking: '', elapsedMs: 0,
        lastInstruction: instruction, lastUseBase: useBase,
      },
      copilotNew: [],
      copilotCursor: null,
      // 检查器让开：画布锁着，开着也改不了
      selectedId: null,
      nodes: withSelection(state.nodes, new Set()),
      copilotTurns: [
        ...state.copilotTurns.slice(-(COPILOT_TURN_LIMIT - 1)),
        { id: turnId, instruction, ops: [], phase: 'running', explanation: '', error: '',
          useBase, checkpoint: checkpoint.id },
      ],
    })

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
    const finish = (patch: Partial<StudioState['copilot']> = {}) => {
      if (activeTurn?.id === turnId) activeTurn = null
      set({
        copilot: { ...get().copilot, active: false, lastOp: '', phase: '', thinking: '',
                   elapsedMs: 0, ...patch },
        cancelCopilot: null, copilotCursor: null,
      })
    }

    /**
     * 这一轮没走完（停止、失败、断流）：画布退回这一轮之前。
     *
     * 半成品不扔：放进 future，⇧⌘Z 能找回来。以前停下来会把半张图留在画布上，
     * 「重新生成」模式下更糟——原来那张图已经被清空了。
     */
    const revert = (): 'reverted' | 'unchanged' => {
      const s = get()
      const past = s.past[s.past.length - 1]?.id === checkpoint.id ? s.past.slice(0, -1) : s.past
      if (!diffGraphs(before, s).total) {
        set({ past, copilotNew: [] })
        return 'unchanged'
      }
      set({
        past, copilotNew: [],
        future: [...s.future, {
          id: ++historySeq, label: '助手没做完的半成品', nodes: s.nodes, edges: s.edges, at: Date.now(),
        }],
      })
      afterJump(set, get, before.nodes, before.edges)
      return 'reverted'
    }

    // 流式期间的临时摆位：挨着上游放，没有上游就放在视口中心附近的空地上。
    // 以前没有上游的节点排在 x=120+n*290，常常落在视口外面，看不见它在长
    const place = (nodeId: string): { x: number; y: number } => {
      const { nodes, edges } = get()
      const others = nodes.filter((n) => n.id !== nodeId)
      const incoming = edges.find((e) => e.target === nodeId && others.some((n) => n.id === e.source))
      const source = incoming && others.find((n) => n.id === incoming.source)
      if (source) {
        anchored.add(nodeId)
        return freeSpot(others, { x: source.position.x + (source.measured?.width ?? CARD_W) + 52, y: source.position.y })
      }
      const c = get().getViewportCenter?.() ?? centerOf(others)
      return freeSpot(others, { x: c.x - CARD_W / 2, y: c.y - CARD_H / 2 })
    }

    const stop = streamCopilot(
      {
        instruction,
        base_graph: useBase && state.nodes.length ? toGraph(state.nodes, state.edges) : null,
        model: model ?? undefined,
        conversation_id: state.copilotConversationId,
      },
      (op) => {
        if (finished) return
        const s = get()
        // 卡片上的「少了一步」读的是记下来的 final：这里认不出来而后端没提的，一并记进去
        record(op.op === 'final' && dropped.length ? { ...op, issues: withDropped(op.issues) } : op)
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
          case 'check': {
            // 服务端自查：用运行时同一套规则过一遍，有问题交回模型改
            // 现在是一句话；以后后端会改成 {node_id, message}（能定位到节点），两种都接住。
            // 带节点的前面补上节点名，「再修一次」拼指令时模型才知道说的是哪个
            const issues = (op.issues ?? []).map((x: any) => {
              if (typeof x === 'string') return x
              const message = String(x?.message ?? JSON.stringify(x))
              const label = x?.node_id ? s.nodes.find((n) => n.id === x.node_id)?.data.label : ''
              return label && !message.includes(`「${label}」`) ? `「${label}」：${message}` : message
            })
            settle({ check: { status: op.status, issues, round: op.round, repaired: op.repaired,
                              message: op.message } })
            set({ copilot: { ...s.copilot,
              phase: op.status === 'repairing' ? 'repairing' : s.copilot.phase,
              lastOp: op.status === 'repairing'
                ? `第 ${op.round ?? 1} 轮修正：自查发现 ${issues.length} 处问题`
                : op.status === 'passed'
                  ? (op.repaired ? `自查发现的问题已修好（${op.repaired} 轮）` : '自查通过')
                  : op.status === 'failed' ? `自查后还有 ${issues.length} 处问题` : '自查修正没跑成' } })
            break
          }
          case 'reply': {
            // 这一句不需要改图。画布上什么都没发生，不能说「流程已更新到画布」
            finished = true
            revert()
            const text = String(op.text ?? '')
            settle({ phase: 'done', outcome: 'answered', reply: text })
            finish({ explanation: '', error: '' })
            recordCanvasTurn(get().copilotConversationId, instruction, { answer: text, status: 'done' }, set)
            break
          }
          case 'add_node': {
            const n = op.node
            // 类型不认识就不放上画布：节点卡片、属性面板都按类型查定义，查不到
            // 以前是整站白屏、连带没保存的编辑一起丢。后端也会拦，这里是第二道
            if (!n?.id || !n?.type) break
            if (!NODE_DEFS[n.type as NodeType]) {
              if (!dropped.includes(String(n.type))) dropped.push(String(n.type))
              break
            }
            if (!cleared) {
              cleared = true
              set({ nodes: [], edges: [] })
            }
            const cur = get()
            const node: FlowNode = {
              id: n.id, type: 'card', position: { x: 0, y: 0 },
              data: { nodeType: n.type, label: n.label ?? '', config: n.config ?? {} },
            }
            set({
              nodes: [...cur.nodes.filter((x) => x.id !== n.id), node],
              copilotNew: cur.copilotNew.includes(n.id) ? cur.copilotNew : [...cur.copilotNew, n.id],
              copilotCursor: n.id,
              copilot: { ...cur.copilot, phase: cur.copilot.phase === 'repairing' ? 'repairing' : 'building',
                         lastOp: `添加节点：${n.label || n.id}` },
              dirty: true,
            })
            // 位置要等边可能已到齐后算——直接再取一次最新状态摆位
            set({
              nodes: get().nodes.map((x) => (x.id === n.id ? { ...x, position: place(n.id) } : x)),
            })
            break
          }
          case 'update_node':
            if (!s.nodes.some((x) => x.id === op.id)) break
            set({
              nodes: s.nodes.map((x) =>
                x.id === op.id
                  ? { ...x, data: { ...x.data,
                      ...(op.label != null ? { label: op.label } : {}),
                      ...(op.config != null ? { config: op.config } : {}) } }
                  : x),
              copilotNew: s.copilotNew.includes(op.id) ? s.copilotNew : [...s.copilotNew, op.id],
              copilotCursor: op.id,
              copilot: { ...s.copilot, lastOp: `修改节点：${op.label || op.id}` },
              dirty: true,
            })
            break
          case 'remove_node':
            set({
              nodes: s.nodes.filter((x) => x.id !== op.id),
              edges: s.edges.filter((e) => e.source !== op.id && e.target !== op.id),
              copilotNew: s.copilotNew.filter((id) => id !== op.id),
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
                sourceHandle: e.sourceHandle ?? null, type: 'flow',
              }],
              copilot: { ...s.copilot, phase: s.copilot.phase === 'repairing' ? 'repairing' : 'wiring',
                         lastOp: `连线：${e.source} → ${e.target}` },
              dirty: true,
            })
            // 这一轮新加的节点先于它的入边落了位：入边到了，挪到上游旁边去
            if (s.copilotNew.includes(e.target) && !anchored.has(e.target)
                && !before.nodes.some((n) => n.id === e.target)) {
              set({ nodes: get().nodes.map((x) => (x.id === e.target ? { ...x, position: place(e.target) } : x)) })
            }
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
            finished = true
            const next = toFlow(op.graph)
            // 改图时后端只给新节点排版，旧节点留在用户摆好的位置（layout.mode='keep'）。
            // 老后端不带 layout、整张图重排过：旧节点照样沿用这一轮之前的坐标，不瞬移
            const keep = useBase && (op.layout?.mode === 'keep' || !op.layout)
            const old = new Map(before.nodes.map((n) => [n.id, n]))
            const live = new Map(get().nodes.map((n) => [n.id, n]))
            const nodes: FlowNode[] = next.nodes.map((n) => {
              const prev = old.get(n.id)
              // 量过的尺寸带过去：不然走线先按默认尺寸画一遍，量完再跳一下
              const measured = live.get(n.id)?.measured ?? prev?.measured
              return {
                ...n,
                ...(keep && prev ? { position: prev.position } : {}),
                ...(measured ? { measured } : {}),
              }
            })
            const diff = diffGraphs(before, { nodes, edges: next.edges })
            const issues = withDropped(op.issues)
            const explanation = op.explanation ?? get().copilot.explanation
            const outcome = diff.total ? 'applied' : 'unchanged'
            if (!diff.total) {
              // 一处都没变：这一轮不占撤销栈
              const p = get().past
              if (p[p.length - 1]?.id === checkpoint.id) set({ past: p.slice(0, -1) })
            }
            const fresh = [...diff.added, ...diff.changed]
            // 整张图重排过（新建、从头生成）或者多了节点：视角要跟过去，否则
            // "生成好了"这句话落在一片空白上。只改了配置就别动镜头
            const refit = !keep || diff.added.length > 0
            // 流式期间已经摆在临时位置上的节点，从那儿滑到排版位置，而不是瞬移。
            // reduced-motion 和大图直接落位
            const targets = new Map(nodes.map((n) => [n.id, n.position]))
            const glides = !reducedMotion() && nodes.length <= GLIDE_MAX_NODES
            const shown = glides
              ? nodes.map((n) => {
                  const cur = live.get(n.id)?.position
                  return cur && (cur.x !== n.position.x || cur.y !== n.position.y) ? { ...n, position: cur } : n
                })
              : nodes
            const moving = shown.some((n, i) => n !== nodes[i])
            set({
              nodes: shown, edges: next.edges,
              dirty: graphSig(nodes, next.edges) !== savedSig,
              copilotNew: fresh,
              ...(refit && !moving ? { fitRequest: get().fitRequest + 1 } : {}),
            })
            // 落定之后再取景：取景时节点还在半路的话，镜头对着的是它们出发的地方
            if (moving) glide(set, get, targets, () => { if (refit) set({ fitRequest: get().fitRequest + 1 }) })
            settle({ phase: 'done', outcome, diff, issues, explanation })
            finish({ explanation, error: '' })
            recordCanvasTurn(get().copilotConversationId, instruction, {
              graph: op.graph, explanation: explanation ?? '', status: 'done',
            }, set)
            void get().validate()
            setTimeout(() => {
              // 这 6 秒里又开了一轮的话，高亮归新的那一轮管
              if (get().copilotNew === fresh) set({ copilotNew: [] })
            }, 6000)
            if (outcome === 'applied') {
              const turn = get().copilotTurns.find((t) => t.id === turnId)
              const missing = issues.filter((i) => i.code === 'unknown_node_type').length
              const left = turn?.check?.status === 'failed' ? turn.check.issues.length : 0
              toast(
                left ? `已放到画布，但还有 ${left} 处问题要你处理`
                  : missing ? `已应用 ${diff.total} 处改动，但有 ${missing} 步没放上`
                  : `已应用 ${diff.total} 处改动`,
                left || missing ? 'warn' : 'ok',
                { key: `copilot:${turnId}`, duration: 8000,
                  action: { label: '撤销', onClick: () => { get().undoCopilotTurn(turnId) } } },
              )
            }
            break
          }
          case 'error': {
            finished = true
            // 保留 lastInstruction：失败后的"重试"要用它，不能让用户重填
            const message = op.message ?? '生成失败'
            const outcome = revert()
            settle({ phase: 'error', error: message, outcome })
            finish({ explanation: '', error: message })
            recordCanvasTurn(get().copilotConversationId, instruction, { status: 'error', error: message }, set)
            break
          }
        }
      },
      (error) => {
        if (finished) {
          if (activeTurn?.id === turnId) activeTurn = null
          if (get().cancelCopilot === stop) set({ cancelCopilot: null })
          return
        }
        // 流断了但没收到 final / error / reply：这一轮不能一直挂在"进行中"，
        // 否则侧栏会永远转圈，而后台其实什么都不会再来了。改了一半的画布退回去
        finished = true
        const outcome = revert()
        const message = error ?? (outcome === 'reverted' ? '这一轮没收尾就断了，画布已退回这一轮之前' : '')
        settle(message ? { phase: 'error', error: message, outcome } : { phase: 'done', outcome })
        finish({ error: message })
      },
    )
    activeTurn = {
      id: turnId,
      stop: () => {
        if (finished) return
        finished = true
        stop()
        const outcome = revert()
        settle({ phase: 'error', error: '已停止', outcome })
        finish({})
      },
      abandon: () => {
        finished = true
        stop()
      },
    }
    set({ cancelCopilot: stop })
  },

  stopCopilot: () => {
    if (activeTurn) {
      activeTurn.stop()
      return
    }
    get().cancelCopilot?.()
    set({
      copilot: { ...get().copilot, active: false, phase: '', thinking: '' },
      cancelCopilot: null, copilotNew: [], copilotCursor: null,
      copilotTurns: get().copilotTurns.map((t) =>
        t.phase === 'running' ? { ...t, phase: 'error', error: '已停止' } : t),
    })
  },

  clearCopilot: () => { void get().newCopilotConversation() },

  newCopilotConversation: async () => {
    const workflow = get().workflow
    if (get().copilot.active) get().stopCopilot()
    // 先把界面清了：就算新会话建不成，也不能让旧轮次挂在那儿冒充「这次对话」
    set({ copilotTurns: [], copilotMemory: { past: [], total: 0, turns: 0 } })
    if (!workflow) {
      set({ copilotConversationId: null })
      return
    }
    try {
      const created = await api.conversations.create({ kind: 'canvas', workflow_id: workflow.id })
      if (get().workflow?.id !== workflow.id) return
      set({ copilotConversationId: created.id })
    } catch (e) {
      // 建不成就不带会话：下一条指令没有历史，而不是悄悄带着旧的
      set({ copilotConversationId: null })
      toast.error(e, { detail: '新对话没建成，下一条指令会不带任何历史发出' })
    }
  },

  repairWithCopilot: (turnId) => {
    const s = get()
    const turn = turnId ? s.copilotTurns.find((t) => t.id === turnId) : s.copilotTurns[s.copilotTurns.length - 1]
    const lines: string[] = []
    if (turn?.check && turn.check.status !== 'passed') lines.push(...turn.check.issues)
    for (const i of turn?.issues ?? []) {
      if (i.code === 'unknown_node_type') {
        lines.push(`上一轮少了一步：用了不存在的节点类型「${i.type ?? '?'}」，请换成现有的节点类型把这一步补上`)
      } else if (i.level === 'error') {
        lines.push(i.message)
      }
    }
    if (!lines.length) {
      // 这一轮没留下问题清单：拿画布眼下的校验错误来修
      const label = (id?: string | null) => s.nodes.find((n) => n.id === id)?.data.label || id
      for (const i of s.issues) {
        if (i.level === 'error') lines.push(i.node_id ? `「${label(i.node_id)}」：${i.message}` : i.message)
      }
    }
    if (!lines.length) {
      toast.info('没有需要修的问题')
      return
    }
    const unique = [...new Set(lines)]
    const instruction = '修正这张工作流里的下列问题，只改有问题的地方，其他节点保持原样：\n'
      + unique.map((l, i) => `${i + 1}. ${l}`).join('\n')
    get().runCopilot(instruction, true, s.copilot.model || null)
  },

  undoCopilotTurn: (turnId) => {
    const s = get()
    const turn = s.copilotTurns.find((t) => t.id === turnId)
    const top = s.past[s.past.length - 1]
    if (!turn?.checkpoint || !top || top.id !== turn.checkpoint || s.copilot.active) {
      toast.warn(`这一轮之后画布又改过了，不能单独撤掉它。用 ${formatShortcut('Mod+Z')} 逐步撤回，或者在版本历史里找回`)
      return false
    }
    get().undo()
    set((st: StudioState) => ({
      copilotTurns: st.copilotTurns.map((t) => (t.id === turnId ? { ...t, outcome: 'reverted' } : t)),
    }))
    return true
  },

  retryCopilot: () => {
    // 失败后用同一条需求重来。以前只能重新打开 Modal 把需求再敲一遍，
    // 而失败往往跟需求本身无关（模型抽风、协议跑偏、网络断了）
    const { lastInstruction, lastUseBase, model } = get().copilot
    if (!lastInstruction) return
    get().runCopilot(lastInstruction, lastUseBase, model || null)
  },

  startFormalRun: async (input) => {
    const { workflow, nodes, edges } = get()
    if (!workflow) return null
    get().unsubscribe?.()
    set({ ...runReset(), run: null, unsubscribe: null, copilotNew: [] })
    try {
      // 正式运行不传 graph：后端只认已发布的不可变版本
      const run = await api.runs.start({
        workflow_id: workflow.id,
        run_class: 'formal',
        input,
      })
      const trace = seedTrace(run)
      // 正式运行跑的是已发布版本；画布有未保存改动时不让发起，所以此刻的画布就是它
      set({ run, streaming: true, trace, runPhase: trace.phase, runSnapshot: snapshotOf(nodes, edges) })
      await get().attachRun(run.id)
      return run
    } catch (e) {
      set({ streaming: false })
      throw e
    }
  },

  attachRun: async (runId) => {
    get().unsubscribe?.()
    streamEpoch += 1
    // 手上已有的事件不要再收一遍。审批恢复会走到这里，而 applyEvent 是无条件
    // 追加——不传 after 的话后端把整条历史重推一遍，时间线上每条都出现两次。
    const current = get()
    const same = current.run?.id === runId
    if (!same) {
      // 换了一条运行（从运行页跳进画布看历史）：上一条的状态一样都不能留，
      // 不然两条运行的节点状态会叠在一张图上
      set({ ...runReset(), run: null, streaming: false, unsubscribe: null })
      const run = await api.runs.get(runId).catch(() => null)
      if (get().run) return    // 等的这会儿用户又发起了别的运行
      if (run) {
        const trace = seedTrace(run)
        set({ run, trace, runPhase: trace.phase })
      }
    }
    const after = same
      ? current.events.reduce((max, e) => Math.max(max, e.seq ?? 0), 0)
      : 0
    const stop: () => void = streamRun(
      runId,
      (event) => get().applyEvent(event),
      (end) => {
        if (get().unsubscribe === stop) set({ unsubscribe: null })
        set({ streaming: false })
        // 结束标记带着当时的状态，先按它收尾（interrupted 而事件里没在等人的就是
        // 挂起），不用等下面那次请求回来。再对一次账：有没有待审批、完整的成果
        // 和用量，结束标记里都没有
        const s = get()
        if (end?.status && s.run?.id === runId) {
          set(advance(s, { ...s.runtime }, {
            seq: 0, type: 'stream.end', node_id: null, ts: s.trace.book.clock / 1000,
            data: { status: end.status },
          }))
        }
        void reconcile(runId, set, get)
      },
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
    set({ run: continued, streaming: true, runSnapshot: snapshotOf(nodes, edges) })
    await get().attachRun(run.id)
  },

  stopRun: async () => {
    const run = get().run
    if (!run) return
    try {
      await api.runs.cancel(run.id)
    } catch (e) {
      // 409 多半说明它其实已经不在跑了（停在审批上、服务重启过）。以前这里把
      // 错误吞掉再把 streaming 置假，按钮翻回"运行"，审批却还挂着——对一次账
      // 让界面说实话，错误交给调用方提示
      void reconcile(run.id, set, get)
      throw e
    }
    // 取消成功时 run.cancelled 会从事件流过来；流要是断了，兜底对一次账
    setTimeout(() => {
      if (get().run?.id === run.id && isActivePhase(get().runPhase)) void reconcile(run.id, set, get)
    }, 3000)
  },

  clearRun: () => {
    get().unsubscribe?.()
    set({ ...runReset(), run: null, streaming: false, unsubscribe: null })
  },

  reconcileRun: async () => {
    const run = get().run
    if (run) await reconcile(run.id, set, get)
  },

  setHoveredNode: (id) => {
    if (get().hoveredNodeId !== id) set({ hoveredNodeId: id })
  },

  focusNode: (id) => set({ focusRequest: { id, seq: (get().focusRequest?.seq ?? 0) + 1 } }),

  setReplayAt: (at) => set({ replayAt: at }),

  setFollow: (follow) => set({ follow }),

  setLineage: (lineage) => set({ lineage }),

  /**
   * 把一条事件折算成画布上的可见变化。
   * 这是"实时可视化"的全部秘密：事件流 → 航迹 → 节点状态 → 高亮/动效。
   *
   * 状态（在跑、等人、完成、失败、取消、挂起）只从航迹来；这里另外维护卡片要
   * 显示的正文、思考、工具调用、协作矩阵这些内容。
   */
  applyEvent: (event) => {
    const state = get()
    const type = String(event.type)
    const nodeId = event.node_id

    // 流式增量：只长正文和思考，不进 events、不碰航迹。一次运行有几千条，
    // 每条都复制整个事件数组、重算一遍航迹，量级是 O(n²)，而它们本来就不落库
    if (type === 'llm.token' || type === 'llm.thinking.delta') {
      if (!nodeId) return
      const r = state.runtime[nodeId] ?? EMPTY_RUNTIME
      const delta = String(event.data?.delta ?? '')
      set({
        runtime: {
          ...state.runtime,
          [nodeId]: type === 'llm.token'
            ? { ...r, tokens: (r.tokens ?? '') + delta }
            : { ...r, thinking: (r.thinking ?? '') + delta },
        },
      })
      return
    }
    // 重连补发、审批恢复重接时收过的那几条
    if ((event.seq ?? 0) > 0 && event.seq <= state.trace.lastSeq) return

    const runtime = { ...state.runtime }
    const patch = (changes: Partial<NodeRuntime>) => {
      if (!nodeId) return
      runtime[nodeId] = { ...(runtime[nodeId] ?? EMPTY_RUNTIME), ...changes }
    }

    switch (type) {
      case 'node.started':
        // 新一轮开始：上一轮的耗时、正文不能挂着，否则跑第 3 轮的卡片上还是第 2 轮的"2.1s"
        patch({ tokens: '', thinking: '', error: undefined, toolCalls: [], durationMs: undefined })
        break
      case 'node.finished':
        patch({ durationMs: event.data.duration_ms, preview: event.data.preview })
        break
      case 'node.failed':
        patch({ error: event.data.error, durationMs: event.data.duration_ms })
        break
      case 'llm.thinking':
        // 汇总事件：直接覆盖为完整思考。回放（刷新页面）时靠这一条恢复。
        patch({ thinking: event.data.text ?? runtime[nodeId!]?.thinking })
        break
      case 'tool.start':
        patch({
          toolCalls: [...(runtime[nodeId!]?.toolCalls ?? []),
                     { tool: event.data.tool, args: event.data.args, agent: event.data.agent }],
        })
        break
      case 'tool.end':
      case 'tool.error': {
        const calls = [...(runtime[nodeId!]?.toolCalls ?? [])]
        // 多 agent 时同一个工具名可能被两个成员同时调，只按名字找会配错人
        const idx = calls
          .map((c, i) => (c.tool === event.data.tool
            && (event.data.agent == null || c.agent === event.data.agent) ? i : -1))
          .filter((i) => i >= 0)
          .pop() ?? -1
        if (idx >= 0) {
          calls[idx] = { ...calls[idx], result: event.data.preview, ok: event.type === 'tool.end' }
        }
        patch({ toolCalls: calls })
        break
      }
      case 'edge.taken':
        patch({
          takenHandle: String(event.data.branch ?? ''),
          reason: event.data.reason ? String(event.data.reason) : undefined,
        })
        break
    }

    // 协作矩阵：只走 decode.ts 那一份翻译，免得画布和右栏对同一次运行给出不同的说法
    const team = reduceTeam(runtime[nodeId ?? '']?.team, event)
    if (team && nodeId) patch({ team })

    set({ events: [...state.events, event], runtime, ...advance(state, runtime, event) })
  },
}))

// 开发期把 store 挂到 window 上，好让 playwright 直接读状态做断言——
// 界面上看不出"事件收全了没有"，只能问 store。生产构建里去掉。
if (import.meta.env.DEV) {
  ;(window as any).__studio = useStudio
}
