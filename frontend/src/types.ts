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
  /**
   * info 是建议，不是问题（比如 evidence.upgrade_available「可以升级为可追溯结构」）：store 在校验回来时
   * 就把它分到 advice 里，issues 里只有 error / warning——节点卡、检查器、运行按钮都只认这两档
   */
  level: 'error' | 'warning' | 'info'
  node_id?: string | null
  edge_id?: string | null
  message: string
  /** 出问题的是节点配置里的哪一项：'prompt'、'tools'、'cases[1].condition'。老后端不给 */
  field?: string | null
  /** 机读编号（governed.agent_approval_never 这类），发布前检查按它认修复。老后端不给 */
  code?: string | null
  /** 这条问题对应的修复 id：publish-check 的 fixes 里同 id 的那一项。没有修复时为空 */
  fix?: string | null
}

/** 发布等级：发布弹窗、问题面板的发布前检查共用 */
export type PublishLevel = 'published' | 'governed'

/**
 * 发布前检查给的一项修复（POST /workflows/{id}/publish-check 的 fixes）。
 * auto：答案唯一、不降低要求，预览后一键修；choice：要人拿主意，options 里挑（multiple 可多选）；
 * assist：结构性的，交给 Copilot 试着改，产出同样只是预览
 */
export interface PublishFix {
  id: string
  code: string
  kind: 'auto' | 'choice' | 'assist'
  node_id?: string | null
  label: string
  preview?: { field?: string | null; before?: unknown; after?: unknown } | null
  /** handoff：选了这一项就是「交给 Copilot」（G4 的「它在做计算」）。只认这个标记，不认值——
   *  别的选项拿节点 id 当值，节点 id 恰好叫 copilot 的报告撰写节点照样是普通候选 */
  options?: { value: unknown; label: string; hint?: string | null; handoff?: boolean }[]
  multiple?: boolean
  /** 建议值：界面上标「建议」，仍然要人选 */
  default?: unknown
}

export interface PublishCheck {
  level: PublishLevel
  ok: boolean
  issues: ValidationIssue[]
  fixes: PublishFix[]
}

/** 自动修复预览里给人看的一项改动：「节点 · 字段：原值 → 新值」 */
export interface AutofixChange {
  fix_id: string
  node_id?: string | null
  node_title?: string | null
  field?: string | null
  before?: unknown
  after?: unknown
  label?: string | null
}

/** POST /workflows/{id}/autofix：只给预览，不改库、不发布 */
export interface AutofixResult {
  /** 修复后的整张图；人确认后前端走保存接口存成草稿 */
  graph: GraphSpec | null
  changes: AutofixChange[]
  applied: string[]
  rejected: { fix_id: string; reason: string }[]
  /** 修复后仍然存在的问题（带 code 和 fix） */
  remaining: ValidationIssue[]
  /** 交给 Copilot 的那一段：它认为要人拿主意的放进 questions，不硬改 */
  assist: { ok: boolean; summary?: string; questions?: string[] } | null
  /** 修复后是否已经没有 error */
  ok: boolean
}

/**
 * 一键升级预览里的一项改动。形状同自动修复的 AutofixChange，另认这几种：
 * field 为 type 是节点类型的变化（llm → report）；field 为 node 是新插入的节点（after 是节点）；
 * field 为 edge 是连线的改接（before / after 是 {source, target}）。rule 是命中的改写规则（R1–R4；Copilot 那一段是 assist）。
 * 同一步（同一个 fix_id）的几项改动共用一句 label
 */
export interface UpgradeChange extends AutofixChange {
  rule?: string | null
}

/** 升级给的说明：不改、只建议的（R5：代码节点要不要标成 source），原样给人看 */
export interface UpgradeNote {
  text: string
  node_id?: string | null
  rule?: string | null
  level?: 'info' | 'warning'
}

/** POST /copilot/upgrade-evidence：只给预览，不自动保存 */
export interface UpgradeResult {
  /** 升级后的整张图；人确认后走现有的保存 */
  graph: GraphSpec | null
  changes: UpgradeChange[]
  notes: UpgradeNote[]
  /** 升级后的图重跑 validate 和门禁剩下的问题 */
  issues: ValidationIssue[]
  /** 没有采用的改动（Copilot 的改写冒出新问题、碰了禁止规则），写明原因 */
  rejected: { fix_id: string; reason: string }[]
  /** assist 时 Copilot 的那一段：非纯算术、保留没改的代码节点在 warnings 里 */
  assist: { ok: boolean; summary?: string; questions?: string[]; warnings?: string[] } | null
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
  /** 引文：hit 是检索快照里第几条命中，start / end 是引文在那条原文里的码点位置 */
  locator?: { metric?: string; field?: string; row?: number; column?: string; table?: string
              hit?: number; start?: number; end?: number }
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
  /** 引文的原文出处：检索快照、文档、片段 */
  source?: EvidenceQuoteSource
  /** [[t:]] / [[c:]] 写了一个哪里都找不到的名字：可能是编造的 */
  unknown?: boolean
  /** 表结构快照不全，找不到的名字只能说核对不了 */
  unverified?: boolean
}

/** 引文出自哪：检索快照工件、知识库里的文档和片段 */
export interface EvidenceQuoteSource {
  artifact?: string
  document?: string
  chunk?: string
  title?: string
  ordinal?: number
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
  /** uncited_number / unresolved_ref / unknown_entity（可能是编造的名字）/ unverified_entity（核对不了） */
  issue?: string
  /** 实体写在反引号里：text 自带两个反引号，按行内代码画 */
  code?: boolean
  /** 系统自动链接的名字，不是写作者写的标记（不算句子的依据） */
  auto?: boolean
  /** 反引号里自动发现的可疑名字（没有 ref 时名字在这里） */
  name?: string
  /** 旧运行按数值猜的候选（state 为 candidate） */
  candidates?: EvidenceCandidate[]
}

/**
 * 旧运行（没有契约）按数值猜的一个候选：查询快照的一格，或口径卡的一个指标。
 * 只做展示，不是证据
 */
export interface EvidenceCandidate {
  kind?: 'cell' | 'metric' | string
  /** Q1.r0.gmv / m:gmv */
  ref?: string
  alias?: string
  artifact?: string
  locator?: EvidenceLocator & { metric?: string }
  eid?: string
  value?: unknown
  rendered?: string
  /** 指标名 */
  name?: string
  caliber?: string
  version?: string
  node_id?: string
  tool?: string
  /** 和答案里那个数差多少 */
  diff?: number
}

/** 旧运行的猜测：guess_sources 的输出（agentlab.guess/1） */
export interface EvidenceGuess {
  schema?: string
  mode?: string
  /** 「猜测的来源，不能当证据：…」 */
  note?: string
  markdown?: string
  segments?: EvidenceSegment[]
  stats?: { numbers?: number; guessed?: number; unguessed?: number; candidates?: number }
  /** 猜的是成果里哪个字段 */
  field?: string
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
  /**
   * 结论句裁判的判定（四期）：只有送了裁判的候选句有。正式运行里节点当场判的进封存（post_seal false）；
   * 探索运行里候选句先标 unjudged + reason on_demand，点开再判
   */
  verdict?: EvidenceVerdict
}

/**
 * 模型对一句结论的判定。是模型的判断，不是证据，不能被引用。
 * status 为 unjudged 时 reason 写明为什么没判：四个上限（max_claims / max_cost_usd / daily_max_usd /
 * timeout_s，rationale 以「已到上限（…）」开头）、error、format、missing、on_demand（探索运行按需）
 */
export interface EvidenceVerdict {
  /**
   * contradicted：证据和原句冲突；insufficient：证据里没有相关信息（missing 写缺的是什么）。
   * unsupported 是拆档之前的取值：旧文档、旧事件里保留原样，界面上照旧写「证据不支持」
   */
  status: 'supported' | 'partial' | 'contradicted' | 'insufficient' | 'unsupported' | 'not_a_claim' | 'unjudged' | string
  /** 理由，最多 120 字（后端在句子边界截断，加「…」） */
  rationale?: string
  /** insufficient 时缺的是什么：「SQL」「字段清单」「取数的查询」…，由裁判给出 */
  missing?: string
  /** 按哪一版裁判规则判的（新判的都带；拆档之前判的没有） */
  rule_version?: number
  /** 判定自己带着的模型提醒（有的话盖过报告、答复、证据图里的） */
  same_model?: boolean
  priced?: boolean | null
  /** 裁判模型 id；探索运行还没判的是 null */
  judge?: string | null
  /** 封存之后按需追加的（evidence.judged 事件） */
  post_seal?: boolean
  /** 裁判看过、摘录里真有的证据编号 */
  used?: string[]
  reason?: string
  cost_usd?: number
}

/** 文档（和 report.checked、节点产出）里的裁判摘要：键的顺序同后端 judge.SUMMARY_KEYS */
export interface EvidenceJudgeSummary {
  /** inline：正式运行节点里当场判；on_demand：探索运行按需 */
  mode?: 'inline' | 'on_demand' | string
  model?: string | null
  on_unsupported?: 'degrade' | 'withhold' | string
  rewrite_once?: boolean
  limits?: { max_claims?: number | null; max_cost_usd?: number | null; timeout_s?: number | null
             daily_max_usd?: number | null } | null
  candidates?: number
  /** 预筛放掉、没送裁判的结论句 */
  screened?: string[]
  counts?: Partial<Record<'supported' | 'partial' | 'contradicted' | 'insufficient' | 'unsupported' | 'not_a_claim'
    | 'unjudged', number>>
  unjudged?: Record<string, number>
  limits_hit?: string[]
  complete?: boolean
  priced?: boolean | null
  cost_usd?: number
  calls?: number
  reused?: number
  duration_ms?: number
  gaps?: string[]
  notes?: string[]
  same_model?: boolean
  /** 撰写这份报告实际用的模型 id（裁判模型和它相同时 same_model 为真） */
  writer_model?: string | null
  /**
   * 不支持的句子交回写作者改写一次（rewrite_once）。units：交回的句子在封存文档里的编号（采用了改写稿时，
   * 改掉的句子是 null）；sentences：交回的原句；changed：改写稿里新写的句子的编号（没采用时为空）；
   * reason：没采用的原因
   */
  rewrite?: { units?: (string | null)[]; sentences?: string[]; applied?: boolean; reason?: string | null
              changed?: string[] } | null
}

/**
 * POST /runs/{id}/evidence/judge 的答复：evidence.judged 事件的载荷（NOTES-A4 §8）。前端对缺字段宽容：
 * verdicts 也认成 [{unit, …}] 的列表
 */
export interface EvidenceJudgeResult {
  /** 判的是哪份报告：{node_id, doc_artifact}（事件载荷里是节点 id 字符串） */
  report?: string | { node_id?: string; doc_artifact?: string }
  doc_artifact?: string
  verdicts?: Record<string, EvidenceVerdict> | (EvidenceVerdict & { unit?: string })[]
  model?: string | null
  cost_usd?: number
  calls?: number
  limits_hit?: string[]
  gaps?: string[]
  notes?: string[]
  skipped?: Record<string, string>
  post_seal?: boolean
  /** 触顶、没跑成、点的不是结论句时的一句话；判成了是 null */
  message?: string | null
  /** 触顶时怎么调，一个上限一句 */
  adjust?: string[]
  /** 这一次裁判模型和写作模型相同、模型在不在价格表里、写作模型 id */
  same_model?: boolean
  priced?: boolean | null
  writer_model?: string | null
  [key: string]: unknown
}

/** 证据图 reports[].judge_meta：这份报告最近一次按需裁判用的模型 */
export interface EvidenceJudgeMeta {
  model?: string | null
  same_model?: boolean
  priced?: boolean | null
  writer_model?: string | null
}

/** 这句能不能「请模型判断」：片段接口按运行的实际情况答（和按需裁判接口同一套条件） */
export interface EvidenceOnDemand {
  available?: boolean
  /**
   * formal / legacy / not_a_claim / judged / unsealed / seal_broken；outdated 是封存后追加的判定早于当前的裁判规则
   * （available 为真，照常给按钮）
   */
  reason?: string | null
  message?: string | null
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
  /** 三期：有出处的实体、引文片段数；可疑实体、核对不了的名字数。老文档没有这几个键，缺省当 0 */
  entities?: number
  quotes?: number
  unknown_entities?: number
  unverified_entities?: number
  /** 四期：claims 为 judge 时追加，候选句按判定的句数（unsupported 是拆档之前的取值，旧文档里才有） */
  supported?: number
  partial?: number
  contradicted?: number
  insufficient?: number
  unsupported?: number
  not_a_claim?: number
  unjudged?: number
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
  /** 四期：写作时结论句策略是 judge 才有 */
  judge?: EvidenceJudgeSummary
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
  /** 没有报告时为什么（none）、旧版出具怎么认的（legacy_*）：一句人话 */
  note?: string
  /**
   * legacy_contract：{note, tier, matched, unmatched, field}，按旧契约的位置标出的数字；
   * legacy_text：guess_sources 的输出（EvidenceGuess），可能按字段分成几份
   */
  legacy?: Record<string, any> | Record<string, any>[] | null
  /** legacy_text 另一种放法：猜测单独放在这里 */
  guess?: EvidenceGuess | EvidenceGuess[] | Record<string, EvidenceGuess> | null
  seal?: EvidenceSeal
  reports?: { node_id?: string; doc_artifact?: string; doc_sealed?: boolean; fields?: string[]; stats?: EvidenceStats
              /** 文档取回时哈希复验：false 对不上（或不是这次运行这个节点写的），null 文件不在了 */
              hash_ok?: boolean | null
              /** 写这份报告时的结论句策略（report.checked 里记的；老后端、升级前的运行没有） */
              claims?: string | null
              /**
               * 四期：封存之后按需追加的判定（evidence.judged 叠在一起），{unit: 判定}。后端没给时前端只认
               * 点开时片段接口、按需裁判接口答的那几句
               */
              post_seal_verdicts?: Record<string, EvidenceVerdict> | null
              /** 封存的裁判摘要（文档的 judge） */
              judge?: EvidenceJudgeSummary | null
              /** 最近一次按需裁判用的模型：和写作模型相同没有、在不在价格表里 */
              judge_meta?: EvidenceJudgeMeta | null }[]
  evidence?: { alias?: string; eid?: string; kind?: string; label?: string; node_id?: string; artifact?: string
               sealed?: boolean; cited_by?: string[]; report?: string }[]
  edges?: { from: string; to: string; rel: string }[]
}

/**
 * 查询快照里的位置：单元格 {row, column}；agent 的数组字段按行映射时是 {rows: [首, 尾], column | columns}
 * （columns 是「字段 → 列」）。行号从 0 数
 */
export interface EvidenceLocator {
  row?: number
  column?: string
  rows?: [number, number] | number[]
  columns?: Record<string, string>
}

/** 指标的一个输入：值从哪来 */
export interface EvidenceInput {
  path?: string
  value?: unknown
  node_id?: string
  /**
   * 一期：input / transform / agent / code / tool / llm …（产出节点的类型）；
   * 二期能追到查询快照那一格的两种：agent_field（agent 开了 cite_fields 的字段）、tool_cell（cell() 取数）
   */
  via?: string
  field?: string
  /** 只有 code 节点有：source 取数 / compute 计算（缺省） */
  role?: string
  /**
   * 一期 ok / missing；agent_field、tool_cell 是核对状态：verified 与快照一致、mismatch 对不上、
   * unresolved 核对不了、missing 模型照实说没查到（from 为 null）
   */
  status?: string
  /** agent 字段的出处，agent 节点内的编号（Q1.r0.gmv）。和报告目录的全局编号不是一回事，靠 artifact 对应 */
  ref?: string | null
  /** 查询快照的工件 id：跳到哪一个查询步骤按它认 */
  artifact?: string
  locator?: EvidenceLocator
  eid?: string
  /** agent_field mismatch：模型报的值（value 是快照里的） */
  model_value?: unknown
  /** tool_cell mismatch：快照里的值（value 是算的时候用的） */
  snapshot_value?: unknown
  /** unresolved 的原因（人话） */
  reason?: string
  /** 输入对到的查询在报告目录里的编号（Q3）：接口按工件补的全局编号 */
  query?: string | null
  /** 输入落在哪一格，全局编号（Q3.r5.amount） */
  cell?: string
}

/** 口径卡钉在另一个已发布工作流某个版本里的口径卡上 */
export interface EvidenceCaliberSource {
  workflow_id?: string
  workflow_version?: number | string
  node_id?: string
  /** 接口顺手给的工作流名；没给时前端按工作流目录找 */
  workflow_name?: string
}

/** 口径卡上游有新版本时声明的处置（和子工作流的升版处置同一套） */
export interface EvidenceCaliberUpgrade {
  node_id?: string
  workflow_id?: string
  pinned?: number
  latest?: number
  /** recompute / dual / incomparable */
  policy?: string
  policy_label?: string
}

/**
 * 片段证据链的一步：metric（指标）、input（指标的一个输入，也可能挂在 metric.inputs 里）、
 * run_input（报告直接引用的运行输入）、query（查询快照：被引用的行加前后各 2 行）。
 * input 步骤同时带着 EvidenceInput 的字段（via、status、model_value、ref、locator、artifact）
 */
export interface EvidenceStep extends Omit<EvidenceInput, 'status'> {
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
  /** 口径卡钉在哪个工作流的哪一版（metric；方案第 5 节的写法，接口实际给在 source 上） */
  caliber_from?: EvidenceCaliberSource | null
  /** 上游有新版本时的处置（metric）；没有升版是 null */
  caliber_upgrade?: EvidenceCaliberUpgrade | null
  // ---- query：查询快照的一个窗口 ----
  tool?: string
  /** query：数据源名；metric：口径卡钉住的来源 {workflow_id, workflow_version, node_id, workflow_name} */
  source?: string | EvidenceCaliberSource | null
  sql?: string
  columns?: string[]
  /** 只有被引用的行加前后各 2 行；遮罩列的值已经换成「已遮罩」 */
  rows?: unknown[][]
  /** rows[0] 在快照里是第几行（从 0 数） */
  row_offset?: number
  /** rows 里每一行在快照里是第几行：被引用的行隔得远时窗口不连续，按它认 */
  row_index?: number[]
  /** 快照一共几行 */
  total_rows?: number | null
  /** 查询撞了行数上限，库里还有更多 */
  truncated?: boolean | null
  /** 被引用的行太多，窗口只给了前面一截 */
  window_truncated?: boolean
  /** 被引用的行（快照里的行号）和列；cells 是精确到格的 [行, 列]，有它就只高亮这几格 */
  highlight?: { rows?: number[]; cols?: string[]; cells?: [number, string][] }
  /** 这一步里被遮罩的列 */
  masked?: string[]
  /** 这一步为什么没有行（快照不在封存范围里、哈希对不上、取不回来） */
  note?: string
  /** 数据源改名或删掉了、按查询当时记下的遮罩处理时的那句说明。行照样有 */
  mask_note?: string
  // ---- entity：表或字段（三期）----
  /** entity：table / column */
  kind?: string
  table?: string
  column?: string
  /** 字段类型（表结构快照里记的） */
  type?: string
  /** 同名字段在好几张表里时，是哪几张 */
  tables?: string[]
  /** 这个名字的来历：表结构快照、查询 SQL、查询结果列 */
  sources?: EvidenceEntitySource[]
  /** 出现在哪几次查询里（报告目录的全局编号） */
  queries?: string[]
  /** 表结构快照什么时候同步的 */
  synced_at?: string
  /** 可疑实体：最接近的已知名字 */
  closest?: (string | { alias?: string; name?: string; table?: string; kind?: string })[]
  /** 可疑实体：查过几份表结构快照、几次查询 */
  checked?: { schemas?: number; queries?: number }
  /** 表结构快照只存了一部分（库里表太多） */
  snapshot_truncated?: boolean
  /** 好几张表都有的同名字段：各表的类型 */
  types?: Record<string, string>
  /** 表：字段清单和类型（封存范围内的表结构快照，最多 60 个，遮罩的列不列入）；多出来的个数 */
  fields?: { name?: string; type?: string | null }[]
  fields_more?: number
  /** 有列按遮罩没列进字段清单 */
  fields_masked?: boolean
  /** 表：几个字段、是不是视图；表或字段的注释 */
  is_view?: boolean
  comment?: string
  nullable?: boolean
  primary_key?: boolean
  // ---- quote：逐字引文（三期）----
  /** quote：引文所在那条命中的原文（也可能放在 content 里） */
  text?: string
  /** quote：引文所在那条命中的全文；快照不在封存范围里、取不回来时是 null */
  content?: string | null
  quote?: string
  match?: { hit?: number; start?: number; end?: number }
  /** quote：按记下的位置把原文切出来，和引文逐字比对的结果 */
  match_ok?: boolean | null
  /** quote：哪个知识库、检索的是什么 */
  collection?: string
  query?: string | null
}

/** 实体的一处来历 */
export interface EvidenceEntitySource {
  /** schema（表结构快照）/ sql（查询 SQL 用到的表）/ result（查询结果列） */
  kind?: string
  artifact?: string
  alias?: string
  source?: string
  truncated?: boolean
}

/** GET /runs/{id}/evidence/audit 的一行（形状照 api/evidence.py；前端经 lib/evidence 的 auditFromApi 读） */
export interface EvidenceAuditRow {
  /** cited / none / suspicious / candidate */
  group?: string
  report?: string | null
  field?: string | null
  segment?: string | null
  unit?: string | null
  /** number / value / entity / quote / claim / violation */
  kind?: string
  text?: string
  state?: string
  issue?: string | null
  ref?: string | null
  alias?: string | null
  evidence_kind?: string | null
  evidence?: string | null
  eid?: string | null
  artifact?: string | null
  node_id?: string | null
  sealed?: boolean | null
  span?: [number, number] | null
  sentence?: string
  note?: string | null
  candidates?: EvidenceCandidate[]
}

/** GET /runs/{id}/evidence/audit：记录页的审计表 */
export interface EvidenceAudit {
  run_id?: string
  schema?: string
  mode?: string
  seal?: EvidenceSeal & { events?: number; message?: string }
  reports?: { node_id?: string; doc_artifact?: string; hash_ok?: boolean | null; doc_sealed?: boolean
              fields?: string[]; claims?: string | null; stats?: EvidenceStats }[]
  groups?: { key?: string; label?: string; count?: number; rows?: EvidenceAuditRow[] }[]
  counts?: Record<string, number>
  total?: number
  note?: string
  legacy_note?: string
}

/** GET /runs/{id}/evidence/segments/{sid}：点开一个片段 */
export interface EvidenceSegmentDetail {
  report?: { node_id?: string; doc_artifact?: string }
  segment?: Partial<EvidenceSegment> & { unit?: string }
  unit?: { id?: string; kind?: string; text?: string; span?: [number, number]; cites?: string[]
          /** 这一句的判定：封存的文档里的，或者按最新的 evidence.judged 叠上的（post_seal） */
          verdict?: EvidenceVerdict
          /** 这一句能不能请模型判断、不能的话为什么 */
          on_demand?: EvidenceOnDemand }
  block?: { id?: string; type?: string }
  chain?: EvidenceStep[]
  /** 片段为什么是现在这个状态，一句人话 */
  note?: string
  /** 落在这个片段（或这一句、没有片段的）上的违规 */
  violations?: EvidenceViolation[]
  seal?: EvidenceSeal
  /** 查询步骤里被遮罩的列（数据源 options.mask_columns）。遮罩只减少暴露，不是安全边界 */
  redacted?: { columns?: string[] }
  /** 可疑实体：最接近的已知名字（最多 3 个），给「是不是想写…」 */
  closest?: (string | { alias?: string; name?: string; table?: string; kind?: string })[]
}
