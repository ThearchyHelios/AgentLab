export type NodeType =
  | 'input' | 'output' | 'llm' | 'agent' | 'supervisor' | 'tool' | 'code'
  | 'branch' | 'loop' | 'subgraph' | 'memory' | 'retrieve' | 'transform'
  | 'human' | 'validate' | 'metrics' | 'report'

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
  /** 最近一次发布的署名。发布时没有署名为 null */
  published_by?: string | null
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
  /** 发起时实际生效的记忆域和知识库（没指定时取设置里的默认值）。老后端不给 */
  memory_scope?: string | null
  collection?: string | null
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
  /**
   * 工具审批的 payload 是 {kind: 'tool_approval', tool, args, title}。探索运行里 MCP /
   * 自定义工具的审批另带 trust_key（同 ToolInfo.trust_key）：有它，审批卡才给「始终允许」
   */
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

/**
 * 后端记着的最近一次测连接（模型接入、数据源、MCP 的列表接口都平铺这四项）。
 * 没测过、或者连接配置改过之后全是 null。用 lib/health 的 healthFromServer 转成
 * HealthRecord 给 HealthPill
 */
export interface ServerHealth {
  last_checked_at?: string | null
  last_check_ok?: boolean | null
  last_latency_ms?: number | null
  last_error?: string | null
}

export interface Provider extends ServerHealth {
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

/**
 * MCP / 自定义工具在探索运行里的信任档（节点审批策略为「仅危险工具」时才看它）：
 * ask 每次等人批（默认）；gated 每次先问门控模型，它判可疑的仍交给人；always 直接执行。
 * 正式运行不看它，一律按 ask
 */
export type ToolTrust = 'ask' | 'gated' | 'always'

export interface ToolInfo {
  id: string
  name: string
  description: string
  category: string
  source: 'builtin' | 'custom' | 'mcp'
  dangerous: boolean
  /**
   * 运行时可能停下来等审批。MCP / 自定义工具：ask、gated 为 true，always 为 false；
   * 更老的后端一律给 false（那时审批关卡认不出它们）
   */
  runtime_approval?: boolean
  /** 只有 MCP / 自定义工具有，老后端不给：没有它就不显示信任档控件 */
  trust?: ToolTrust
  /** 改信任档时用的键（PUT /api/tools/trust），等于 id */
  trust_key?: string
  schema: Record<string, any>
  /**
   * 只有自定义工具有：库里存着的参数定义哪里写坏了（一句中文）。绑了它的节点运行时
   * 一定失败，列表上要醒目地标出来。null / 缺省表示没问题
   */
  problem?: string | null
}

/**
 * 数据源（GET /api/datasources 的一行）。tools 是它给模型的两个工具名
 * db_query__<name> / db_schema__<name>——/api/tools 里没有它们，挑工具要从这里取
 */
export interface DataSource extends ServerHealth {
  id: string
  /** 标识，进了工具名，建好不能改 */
  name: string
  kind: string
  host: string | null
  port: number | null
  database: string | null
  username: string | null
  options: Record<string, any>
  readonly: boolean
  description: string
  enabled: boolean
  password_masked?: string
  has_password?: boolean
  table_count?: number
  schema_synced_at?: string | null
  /** 上次探查为什么没拿到表；空串 = 没出错（或还没探查） */
  schema_error?: string
  available_schemas?: string[]
  tools?: string[]
  /** 缓存按哪个 schema 探的：'' 默认 schema，null 没有缓存 */
  cached_schema?: string | null
}

/** 自定义工具（GET /api/custom-tools 的一行；POST / PATCH 的返回同形） */
export interface CustomTool {
  id: string
  name: string
  description: string
  kind: 'http' | 'python' | (string & {})
  parameters: Record<string, any>
  config: Record<string, any>
  enabled: boolean
  /** 同 ToolInfo.problem：参数定义写坏了的那句话，没问题是 null */
  problem?: string | null
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
  /** 出问题的是节点配置里的哪一项：'prompt'、'tools'、'cases[1].condition'。老后端不给 */
  field?: string | null
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
  /**
   * 最后一轮停在哪。左栏对这次没打开过的会话也能标出运行中、待审批、失败。
   * 没有轮次时为 null；老后端不给
   */
  last_status?: 'running' | 'waiting' | 'error' | 'cancelled' | 'suspended' | 'done' | null
  /** 最后一轮的运行。建图阶段就断了的没有 */
  last_run_id?: string | null
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
  /**
   * 可信度元数据（出具档位、运行类别、耗时、查库次数……），前端整块写、整块读，
   * 后端只用它算 last_status。老轮次挂在 review.meta 下，后端读出来时统一放到这里
   */
  meta?: Record<string, any> | null
  created_at?: string
  /**
   * 这一轮最后一次落库的时刻（UTC，后端 TurnOut）。后端判「建图断了」按它算
   * （conversations.BUILD_STALE），前端判断同一件事时用同一只钟。老后端没有
   */
  updated_at?: string
}

/**
 * Copilot 改图前后都在、而绑定的工具变了的节点或协作成员。
 * 画布上看不出来（节点还在），跑起来才发现查不了库，所以改图回执要明说
 */
export interface ToolChange {
  node_id: string
  label: string
  /** 协作成员的名字；节点本身的工具为 null */
  member: string | null
  /** 配置里的哪一项：'tools' 或 'agents[2].tools' */
  field: string
  before: string[]
  after: string[]
  added: string[]
  removed: string[]
}

/**
 * Copilot 自查（SSE op='check'）里的一条问题。新后端给对象，老会话里存的是
 * 「「node_id」message」一行字符串，两种都要认，见 CopilotCheckEntry
 */
export interface CopilotCheckIssue {
  level: 'error' | 'warning'
  node_id: string | null
  edge_id: string | null
  /** 不再以「节点 id」开头；要带节点名自己用 node_id 查 */
  message: string
  /** 出问题的配置项，比如 'tools'、'agents[1].tools'，检查器据此定位 */
  field?: string | null
  /** datasource_out_of_scope、tools_dropped 这类机器码 */
  code?: string
}

export type CopilotCheckEntry = string | CopilotCheckIssue

/** SSE 的自查操作。repairing 时带第几轮；warnings 是不挡运行的提醒（tools_dropped） */
export interface CopilotCheckOp {
  op: 'check'
  status: 'repairing' | 'passed' | 'failed' | 'error'
  issues?: CopilotCheckEntry[]
  warnings?: CopilotCheckIssue[]
  round?: number
  repaired?: number
  message?: string
  detail?: string
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
  /**
   * agent 为什么提前收尾（后端 engine/guards.py）：steps / stall / budget_tokens / budget_usd / context。
   * 老运行没有。只有 steps（或者没有这个字段的老运行）放宽步数才对症
   */
  reason?: string
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

// -------------------------------------------------------------------------
// 可点击证据（报告撰写节点产出的文档、证据接口）
//
// 形状照后端 engine/evidence.py 和 api/evidence.py。前端对缺字段一律宽容：接口和文档
// 是分期长出来的，老运行、别的版本的后端都可能少几个键，少了就降级显示，不白屏。
// 偏移（span、start/end）按 Unicode 码点算，不是 JS 的 UTF-16 下标。
// -------------------------------------------------------------------------

/** 一处引用解析后的结果 */
export interface EvidenceCitation {
  /** 不带前缀的引用，如 gmv、week */
  ref?: string
  /** 目录里的别名，如 m:gmv、i:week、Q1 */
  alias?: string
  locator?: { metric?: string; field?: string; row?: number; column?: string; table?: string }
  eid?: string
  /** metric / input / cell / table / column / quote / query */
  kind?: string
  role?: string
  status?: 'resolved' | 'unresolved' | string
  /** 解析不了的原因（人话） */
  reason?: string
  conv?: string
  value?: unknown
  rendered?: string
}

/** 可点的最小片段 */
export interface EvidenceSegment {
  id: string
  /** text / number / value / entity / quote / structural */
  kind: string
  text: string
  span?: [number, number]
  /** deterministic / none / neutral，后续期还有 probabilistic、candidate */
  state?: string
  /** 带前缀的原始引用，如 m:gmv|万 */
  ref?: string
  cite?: EvidenceCitation
  strong?: boolean
  /** uncited_number / unresolved_ref */
  issue?: string
}

/** 句子、列表项或表格单元格 */
export interface EvidenceUnit {
  id: string
  /** claim / connective / heading / code。确定性规则分的，不是裁判结论 */
  kind?: string
  span?: [number, number]
  cites?: string[]
  see?: EvidenceCitation[]
  segments: EvidenceSegment[]
  /** 表格单元格的位置，表头 row = -1 */
  loc?: { row: number; col: number }
  /** 列表项的缩进层级 */
  depth?: number
}

export interface EvidenceBlock {
  id: string
  /** heading / paragraph / list / table / quote / code / hr */
  type: string
  level?: number
  ordered?: boolean
  start?: number
  lang?: string
  units: EvidenceUnit[]
}

export interface EvidenceViolation {
  /** uncited_number / unresolved_ref / render_mismatch / eid_mismatch / … */
  code: string
  message?: string
  span?: [number, number]
  text?: string
  /** 指向的片段。结构片段（列表序号、代码围栏标签）里的数字没有可画线的文字，只能在清单里找 */
  segment?: string
  unit?: string
  ref?: string
  context?: string
}

export interface EvidenceStats {
  units?: number
  segments?: number
  claims?: number
  connective?: number
  numbers?: number
  numbers_cited?: number
  values?: number
  uncited_numbers?: number
  unresolved?: number
  see?: number
  uncited_claims?: number
  violations?: number
}

/** report_doc 工件的内容 */
export interface EvidenceDocData {
  schema?: string
  run_id?: string | null
  node_id?: string
  /** 渲染后的全文（真实数字，不含标记） */
  markdown?: string
  /** 建文档时的完整目录：alias → 条目 */
  catalog?: Record<string, Record<string, any>>
  blocks: EvidenceBlock[]
  stats?: EvidenceStats
  violations?: EvidenceViolation[]
}

/**
 * 成果上的标注：这几个字段逐字等于某个报告撰写节点的文档，可以逐段点开看证据。
 * 契约 report_from 指着的那份在最外层；另有报告也被原样放进成果的，列在 others 里
 */
export interface EvidenceFieldRef {
  report_node?: string
  doc_artifact?: string
  fields?: string[]
  others?: EvidenceFieldRef[]
}

/** 封存状态 */
export interface EvidenceSeal {
  sealed?: boolean
  ok?: boolean | null
  /** 这件证据在不在封存范围内 */
  covered?: boolean
  manifest_seq?: number
  legacy?: boolean
}

/** GET /runs/{id}/evidence：整次运行的证据图 */
export interface EvidenceGraph {
  run_id?: string
  schema?: string
  /** cited / legacy_contract / legacy_text / none */
  mode?: string
  seal?: EvidenceSeal
  reports?: { node_id?: string; doc_artifact?: string; doc_sealed?: boolean; fields?: string[]; stats?: EvidenceStats
              /** 文档取回时哈希复验：false 对不上（或不是这次运行这个节点写的），null 文件不在了 */
              hash_ok?: boolean | null }[]
  evidence?: { alias?: string; eid?: string; kind?: string; label?: string; node_id?: string; artifact?: string
               sealed?: boolean; cited_by?: string[] }[]
  edges?: { from: string; to: string; rel: string }[]
}

/** 指标的一个输入：值从哪来 */
export interface EvidenceInput {
  path?: string
  value?: unknown
  node_id?: string
  via?: string
  field?: string
  role?: string
  /** ok / missing */
  status?: string
}

/**
 * 片段证据链的一步。本期有三种：metric（指标）、input（指标的一个输入，也可能挂在
 * metric.inputs 里）、run_input（报告直接引用的运行输入）
 */
export interface EvidenceStep {
  step: string
  metric?: string
  decimals?: number | null
  format?: string
  conv?: string | null
  eid?: string
  /** eid 能由工件和定位重算出来 */
  eid_ok?: boolean
  /** 工件取回时哈希复验通过、指标在卡里 */
  hash_ok?: boolean
  /** 卡里的值按同样的格式渲染出来就是报告上的字 */
  render_ok?: boolean
  /** 这件证据在封存范围内 */
  sealed?: boolean
  alias?: string
  name?: string
  value?: unknown
  unit?: string
  rendered?: string
  caliber?: string
  version?: string
  node_id?: string
  artifact?: string
  expression?: string
  substituted?: string
  recompute_ok?: boolean | null
  status?: string
  inputs?: EvidenceInput[]
  path?: string
  field?: string
  via?: string
}

/** GET /runs/{id}/evidence/segments/{sid}：点开一个片段 */
export interface EvidenceSegmentDetail {
  report?: { node_id?: string; doc_artifact?: string }
  segment?: Partial<EvidenceSegment> & { unit?: string }
  unit?: { id?: string; kind?: string; text?: string; span?: [number, number]; cites?: string[] }
  block?: { id?: string; type?: string }
  chain?: EvidenceStep[]
  /** 片段为什么是现在这个状态，一句人话 */
  note?: string
  /** 落在这个片段（或这一句、没有片段的）上的违规 */
  violations?: EvidenceViolation[]
  seal?: EvidenceSeal
}
