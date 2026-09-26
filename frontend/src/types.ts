export type NodeType =
  | 'input' | 'output' | 'llm' | 'agent' | 'supervisor' | 'tool' | 'code'
  | 'branch' | 'loop' | 'subgraph' | 'memory' | 'retrieve' | 'transform'
  | 'human' | 'validate' | 'metrics'

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
  status?: 'draft' | 'published' | 'governed'
  published_version?: number | null
  run_count?: number
  created_at?: string
  updated_at?: string
}

/** 工作流的一个版本。列表接口只有前四项，取单个版本时才有 graph 等 */
export interface WorkflowVersion {
  id: string
  version: number
  note: string
  created_at?: string
  workflow_id?: string
  graph?: GraphSpec
  graph_hash?: string | null
  /** 这一版入口节点声明的字段。正式运行的表单照它填，不能照画布 */
  input_fields?: { name: string; required?: boolean; [key: string]: any }[]
  /** 是不是当前的已发布版本 */
  published?: boolean
}

/**
 * 后端写进 runs.status 的值。interrupted 既可能是停在审批上，也可能是服务重启
 * 打断后挂起——显示时用 lib/status 的 resolveStatus 配合审批列表区分。
 * suspended 目前后端不写，前端推导的 RunPhase 会用到，这里一并收下。
 */
export type RunStatus =
  | 'queued' | 'running' | 'interrupted' | 'succeeded' | 'failed' | 'cancelled' | 'suspended'

/**
 * 运行的用量与时长。
 *
 * 三种时长口径不同，不能混用：duration_ms / active_ms 是各段执行时长之和（审批
 * 恢复、续跑的每一段都算），wall_ms 是从第一次开始到结束的墙钟，wait_ms 是等人
 * 审批的总时长。老数据只有 duration_ms，而且可能只是最后一段。
 */
export interface RunUsage {
  duration_ms?: number
  wall_ms?: number
  active_ms?: number
  wait_ms?: number
  input_tokens?: number
  output_tokens?: number
  cost_usd?: number
  [key: string]: any
}

export interface Run {
  id: string
  workflow_id: string | null
  workflow_name: string
  status: RunStatus
  input: Record<string, any>
  output: Record<string, any>
  error: string | null
  usage: RunUsage
  run_class?: 'formal' | 'exploratory'
  version?: number | null
  version_hash?: string | null
  manifest_hash?: string | null
  /** 封存到第几条事件为止。有它才说明这条运行的清单封存过 */
  manifest_seq?: number | null
  /** 失败时能定位到的节点 */
  error_node_id?: string | null
  started_by?: string | null
  created_at?: string
  started_at?: string
  finished_at?: string | null
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
  // 以下是审批卡的上下文，老后端不给
  workflow_id?: string | null
  workflow_name?: string | null
  node_label?: string | null
  run_status?: RunStatus | null
  run_class?: 'formal' | 'exploratory' | null
  /** 谁处理的。null 表示处理时没有署名，显示「未署名」 */
  resolved_by?: string | null
  resolved_at?: string | null
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
  /** 运行时是否真的会因它停下来等审批。MCP 与自定义工具目前不会，界面别说它「需确认」 */
  runtime_approval?: boolean
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
  /**
   * 从哪来。kind=run 时带运行、节点和工作流名；manual 是手动添加的；playground 是
   * 在工具库里直接调 remember 写进来的。老后端不给
   */
  source?: MemorySource | null
  created_at?: string
  /** 后端不再给：每次计数的回忆都会写这一行，updated_at 跟着回忆走，不是编辑时间 */
  updated_at?: never
  last_used_at?: string | null
}

export interface MemorySource {
  kind?: 'run' | 'manual' | 'playground' | string
  run_id?: string | null
  node_id?: string | null
  workflow_name?: string | null
  node_label?: string | null
  /** 来源运行是否还在。删掉之后只剩文字，不再给链接 */
  run_exists?: boolean
  [key: string]: any
}

export interface KbDocument {
  id: string
  collection: string
  title: string
  source: string
  mime: string
  chunk_count: number
  /** ready | processing | failed。大文件的切块和算向量在后台跑 */
  status?: string
  error?: string
  meta?: { progress?: { done: number; total: number } }
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
  /** cancelled / suspended 是终态清扫时收的：运行被取消或服务重启时还没跑完的节点 */
  status: 'idle' | 'running' | 'done' | 'failed' | 'waiting' | 'skipped' | 'cancelled' | 'suspended'
  durationMs?: number
  preview?: any
  error?: string
  tokens?: string
  thinking?: string
  toolCalls?: { tool: string; args?: any; result?: string; ok?: boolean; agent?: string }[]
  /** supervisor 的协作矩阵：谁被派了、在第几轮、几个人同时在跑。运行中长出来 */
  team?: TeamRun
  /** loop 走到第几轮（从 1 数）。卡片上那个"第 2/5 轮" */
  iteration?: number
  /** 分支/循环实际走的出口。命中的那条亮起来、其余压暗——不然一个六出口的
   *  分支节点跑完，谁也说不清它到底选了哪条 */
  takenHandle?: string
  /** 走这条出口的理由：表达式分支命中了哪条条件，或调度者为什么派给这个人 */
  reason?: string
}

/**
 * 协作团队一轮里的一个人。
 *
 * ms 是**各自量出来**的耗时，不是拿整轮墙钟摊的——界面要显示"并行省了多少"，
 * 那个数按 N×墙钟 推算会把快的那个也记成最慢那条，省下的时间就被夸大了。
 */
export interface TeamMember {
  agent: string
  instruction: string
  ms: number
  status: 'running' | 'done' | 'failed' | 'waiting' | 'cancelled' | 'suspended'
  result?: string
}

export interface TeamRound {
  round: number
  /** 这一轮同时派了几个人。1 就是串行的一步 */
  parallel: number
  /** 这一轮实际花的时间：并发时是最慢那个 */
  wallMs: number
  /** 各人耗时之和：并发时它大于 wallMs，差值就是省下的 */
  sumMs: number
  reason?: string
  members: TeamMember[]
}

export interface TeamRun {
  /** 花名册，按第一次出现的顺序——泳道的行顺序 */
  members: string[]
  rounds: TeamRound[]
  /** 并行一共省下多少毫秒。全程串行则为 0 */
  savedMs: number
  /** 协作是不是已经收尾了 */
  finished: boolean
}

/** 一处 {{ }} 引用 */
export interface VarRef {
  node_id: string
  node_label: string
  field: string
  expr: string
}

/** 一个可以被 {{ }} 引用的东西。由后端静态分析整张图得出 */
export interface Variable {
  path: string
  kind: 'input' | 'var' | 'node' | 'builtin' | 'loop'
  label: string
  produced_by?: string | null
  produced_by_label?: string | null
  order: number
  refs: VarRef[]
  description: string
}

export interface VarIssue {
  level: 'error' | 'warning' | 'info'
  message: string
  node_id?: string | null
  path: string
}

/**
 * 一次对话。
 *
 * kind 分两种：chat 是问数据页（一轮 = 建图 → 跑图 → 出答案，进左侧列表），
 * canvas 是画布右栏的 Copilot（依附某张图，一轮 = 一次改图，不进列表）。
 */
export interface Conversation {
  id: string
  title: string
  kind: 'chat' | 'canvas'
  workflow_id?: string | null
  archived: boolean
  created_at?: string
  last_active_at?: string
  turn_count: number
  /** 列表里的副标题，让人一眼认出是哪次聊天 */
  last_question: string
}

export interface ConversationDetail extends Conversation {
  turns: ConversationTurn[]
}

export interface ConversationTurn {
  id: string
  seq: number
  question: string
  answer: string
  explanation: string
  graph?: GraphSpec | null
  /** 可能为空：建图阶段就失败时压根没有 run，但这一轮仍然发生过 */
  run_id?: string | null
  status: 'running' | 'done' | 'error'
  error: string
  /** 复核结论。null = 这一轮没复核过，和「复核过、没发现问题」不是一回事 */
  review?: ReviewResult | null
  created_at?: string
}

/**
 * 一次运行跑完之后的复核结论（后端 engine/review.py）。
 *
 * 「跑完了」和「答得对」是两回事：检索降级、工具报错、agent 步数用满这些事
 * 原先一件都不会进到答案里，用户拿到一个看起来很完整的结论，却无从知道它是在
 * 什么条件下得出的。复核层就是把这段补上。
 */
export interface ReviewSignal {
  kind: string
  detail: string
  /** broken = 答案不可信；degraded = 能用但有缺口 */
  severity: 'broken' | 'degraded'
}

export interface ReviewResult {
  /** ok=没问题 / annotated=加了说明 / rewritten=重写过 / reverted=重写里有新数字被退回 */
  verdict: 'ok' | 'annotated' | 'rewritten' | 'reverted'
  /** 给用户看的异常说明。排在成果上方 */
  note: string
  /** 重写后的答案。null 表示沿用原答案 */
  answer: string | null
  /** 改写之前的那份。改写是有损的，得能对照原件 */
  original?: string | null
  /** 换个配置重跑一遍大概率就好了 */
  retry: boolean
  severity: 'broken' | 'degraded' | ''
  signals: ReviewSignal[]
}
