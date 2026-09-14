import { create } from 'zustand'
import {
  addEdge, applyEdgeChanges, applyNodeChanges,
  type Connection, type Edge, type EdgeChange, type Node, type NodeChange,
} from '@xyflow/react'
import { api, streamCopilot, streamRun } from '../api/client'
import { NODE_DEFS } from '../canvas/nodeDefs'
import type {
  GraphEdge, GraphSpec, NodeRuntime, NodeType, Run, RunEvent, ValidationIssue, Workflow,
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
      type: 'smoothstep',
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

interface StudioState {
  workflow: Workflow | null
  nodes: FlowNode[]
  edges: Edge[]
  selectedId: string | null
  dirty: boolean
  issues: ValidationIssue[]

  run: Run | null
  events: RunEvent[]
  runtime: Record<string, NodeRuntime>
  activeEdges: string[]
  streaming: boolean
  unsubscribe: (() => void) | null

  copilot: { active: boolean; lastOp: string; explanation: string; error: string }
  copilotNew: string[]
  cancelCopilot: (() => void) | null

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

  startRun: (input: Record<string, any>) => Promise<Run | null>
  startFormalRun: (input: Record<string, any>) => Promise<Run | null>
  runCopilot: (instruction: string, useBase: boolean) => void
  stopCopilot: () => void
  attachRun: (runId: string) => Promise<void>
  stopRun: () => Promise<void>
  clearRun: () => void
  applyEvent: (event: RunEvent) => void
}

const EMPTY_RUNTIME: NodeRuntime = { status: 'idle' }

export const useStudio = create<StudioState>((set, get) => ({
  workflow: null,
  nodes: [],
  edges: [],
  selectedId: null,
  dirty: false,
  issues: [],
  run: null,
  events: [],
  runtime: {},
  activeEdges: [],
  streaming: false,
  unsubscribe: null,
  copilot: { active: false, lastOp: '', explanation: '', error: '' },
  copilotNew: [],
  cancelCopilot: null,

  load: (workflow) => {
    const { nodes, edges } = toFlow(workflow.graph)
    get().unsubscribe?.()
    set({
      workflow, nodes, edges, selectedId: null, dirty: false, issues: [],
      run: null, events: [], runtime: {}, activeEdges: [], streaming: false, unsubscribe: null,
    })
    void get().validate()
  },

  setGraph: (graph) => {
    const { nodes, edges } = toFlow(graph)
    set({ nodes, edges, dirty: true })
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

  validate: async () => {
    const { nodes, edges } = get()
    if (!nodes.length) {
      set({ issues: [] })
      return
    }
    try {
      const result = await api.workflows.validate(toGraph(nodes, edges))
      set({ issues: result.issues ?? [] })
    } catch {
      /* 校验失败不影响编辑 */
    }
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

  runCopilot: (instruction, useBase) => {
    const state = get()
    state.cancelCopilot?.()
    set({
      copilot: { active: true, lastOp: '正在起草…', explanation: '', error: '' },
      copilotNew: [],
    })
    if (!useBase) set({ nodes: [], edges: [], dirty: true })

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
      { instruction, base_graph: useBase && state.nodes.length ? toGraph(state.nodes, state.edges) : null },
      (op) => {
        const s = get()
        switch (op.op) {
          case 'plan':
            set({ copilot: { ...s.copilot, lastOp: op.summary ?? '规划中' } })
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
            set({ copilot: { ...s.copilot, lastOp: '排版整理中…', explanation: op.explanation ?? '' } })
            break
          case 'final': {
            // 后端排版+校验后的最终图整体落位；高亮集合保留几秒供辨认
            const { nodes, edges } = toFlow(op.graph)
            set({ nodes, edges, dirty: true,
                  copilot: { active: false, lastOp: '',
                             explanation: op.explanation ?? get().copilot.explanation, error: '' } })
            void get().validate()
            setTimeout(() => set({ copilotNew: [] }), 6000)
            break
          }
          case 'error':
            set({ copilot: { active: false, lastOp: '', explanation: '', error: op.message ?? '生成失败' } })
            break
        }
      },
      (error) => {
        const c = get().copilot
        set({
          copilot: { ...c, active: false, error: error ?? c.error },
          cancelCopilot: null,
        })
      },
    )
    set({ cancelCopilot: stop })
  },

  stopCopilot: () => {
    get().cancelCopilot?.()
    set({
      copilot: { ...get().copilot, active: false },
      cancelCopilot: null,
    })
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
    const stop = streamRun(
      runId,
      (event) => get().applyEvent(event),
      () => set({ streaming: false }),
    )
    set({ unsubscribe: stop, streaming: true })
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
