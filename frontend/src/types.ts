export type NodeType =
  | 'input' | 'output' | 'llm' | 'agent' | 'supervisor' | 'tool' | 'code'
  | 'branch' | 'loop' | 'subgraph' | 'memory' | 'retrieve' | 'transform'
  | 'human' | 'validate'

export interface GraphNode {
  id: string
  type: NodeType
  position: { x: number; y: number }
  data: { label?: string; description?: string; config: Record<string, any> }
}

export interface GraphEdge {
  id?: string
  source: string
  target: string
  sourceHandle?: string | null
  targetHandle?: string | null
  label?: string
}

export interface GraphSpec {
  nodes: GraphNode[]
  edges: GraphEdge[]
  viewport?: Record<string, any>
  defaults?: Record<string, any>
}

export interface Workflow {
  id: string
  name: string
  description: string
  graph: GraphSpec
  tags: string[]
  version: number
  is_template: boolean
  run_count?: number
  created_at?: string
  updated_at?: string
}

export type RunStatus =
  | 'queued' | 'running' | 'interrupted' | 'succeeded' | 'failed' | 'cancelled'

export interface Run {
  id: string
  workflow_id: string | null
  workflow_name: string
  status: RunStatus
  input: Record<string, any>
  output: Record<string, any>
  error: string | null
  usage: Record<string, any>
  created_at?: string
  started_at?: string
  finished_at?: string
}

export interface RunEvent {
  seq: number
  type: string
  node_id: string | null
  ts: number
  data: Record<string, any>
  replay?: boolean
}

export interface Approval {
  id: string
  run_id: string
  node_id: string
  mode: 'approve' | 'input' | 'edit'
  title: string
  payload: Record<string, any>
  status: string
  response: Record<string, any>
  created_at?: string
}

export interface Provider {
  id: string
  name: string
  kind: string
  base_url: string | null
  default_model: string | null
  models: { id: string; label?: string; context?: number; pricing?: any }[]
  enabled: boolean
  extra: Record<string, any>
  api_key_masked: string
  has_key: boolean
}

export interface ToolInfo {
  id: string
  name: string
  description: string
  category: string
  source: 'builtin' | 'custom' | 'mcp'
  dangerous: boolean
  schema: Record<string, any>
}

export interface Skill {
  id: string
  name: string
  description: string
  instructions: string
  examples: { input?: string; output?: string }[]
  suggested_tools: string[]
  tags: string[]
  enabled: boolean
}

export interface MemoryItem {
  id: string
  scope: string
  kind: string
  content: string
  importance: number
  use_count: number
  meta: Record<string, any>
  created_at?: string
}

export interface KbDocument {
  id: string
  collection: string
  title: string
  source: string
  mime: string
  chunk_count: number
  created_at?: string
}

export interface ValidationIssue {
  level: 'error' | 'warning'
  node_id?: string | null
  edge_id?: string | null
  message: string
}

/** 单个节点在一次运行中的实时状态，驱动画布上的高亮。 */
export interface NodeRuntime {
  status: 'idle' | 'running' | 'done' | 'failed' | 'waiting' | 'skipped'
  durationMs?: number
  preview?: any
  error?: string
  tokens?: string
  thinking?: string
  toolCalls?: { tool: string; args?: any; result?: string; ok?: boolean }[]
}
