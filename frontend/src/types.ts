export type NodeType =
  | 'input' | 'output' | 'llm' | 'agent' | 'supervisor' | 'tool' | 'code' | 'merge'
  | 'branch' | 'loop' | 'subgraph' | 'memory' | 'retrieve' | 'transform'
  | 'human' | 'validate' | 'metrics' | 'report' | 'dual_check' | 'challenge'

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
  /** 当前发布版本的级别。画布上改过之后 status 回到 draft，「这是不是受管工作流」看这一项（老服务端不给） */
  published_level?: 'published' | 'governed' | null
  /** 画布这一版的图和当前发布版内容相同（服务端按图哈希比）：撤回改动后保存出来的新版本不算「领先」（老服务端不给） */
  same_as_published?: boolean
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
  /** 发布时记下的数据目录版本，没记过为 null */
  catalog_versions?: CatalogVersions | null
  /** 发布之后目录有变化的表 */
  catalog_changes?: CatalogDriftTable[]
  /** 发布时记下的表结构版本 {源名: {表名: 结构版本}}（只记写死的 SQL 直接引用的表）；记录之前发布的为 null */
  schema_versions?: Record<string, Record<string, number>> | null
  /** 发布之后结构版本变了的表 */
  schema_changes?: SchemaDriftTable[]
}

/**
 * 发布时记下的数据目录版本（服务端 catalog_impact.recorded_versions），两组都是 {源名: {表名: 版本}}：
 * - direct：SQL 里写着的表，0 表示当时还没有目录
 * - possible：Agent 绑定了查询工具的数据源中所有有目录的表（SQL 运行时才生成）；源下为空表示当时一张有目录的
 *   表都没有。只记了直接引用的老版本这一组为空
 */
export interface CatalogVersions {
  direct: Record<string, Record<string, number>>
  possible: Record<string, Record<string, number>>
}

/** 发布之后数据目录有变化的一张表（服务端 catalog_impact.catalog_drift；运行里的 catalog.drift 事件同形） */
export interface CatalogDriftTable {
  source: string
  source_id: string
  table: string
  /** 表现在的中文名 */
  label: string | null
  /** 发布时的版本，0 表示当时还没有目录 */
  published: number | null
  current: number
  /** direct：SQL 里写着这张表；possible：Agent 可能查询的表。老事件没有这一项，按 direct */
  impact: 'direct' | 'possible'
}

/**
 * 发布之后表结构变了的一张表（服务端 catalog_impact.schema_drift；运行里的 schema.drift 事件同形）。和目录有变化
 * （CatalogDriftTable）分开记：这里的版本是表结构版本，不是目录版本
 */
export interface SchemaDriftTable {
  source: string
  source_id: string
  table: string
  /** 表现在的中文名 */
  label: string | null
  /** 发布时的结构版本 */
  published: number
  /** 现在的结构版本；表已删除时为 null */
  current: number | null
  removed: boolean
  /** 发布之后测到、还没处理的结构变化（最多 5 条，more 是另外还有几条） */
  changes: { id: string; kind: string; column: string | null; severity: 'info' | 'warning' | 'action'; summary: string }[]
  more: number
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
  /** 金额估不出来的调用次数（模型不在价格目录中），cost_usd 只是估得出的那部分。没有这种调用时不给 */
  unpriced_calls?: number
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
  /**
   * 这条审批对应的待办（一次中断对应一条 Approval 和一条 Task，应用平台 D-r）。有它审批卡直接按 id 取待办，
   * 不必再按运行取一遍待办列表去对；老服务端不给
   */
  task_id?: string | null
}

/**
 * 人工审批审的报告的核对概况（human 节点请求里的 payload.reports，服务端 nodes/human._reviewed_reports）：
 * 上游直接连着的、或者标题和说明里引用了正文的报告撰写节点，各一项。stats 同 report.checked，judge 是裁判摘要
 * 的几项（mode：inline 节点里当场判的——交给审批的报告探索运行也当场判 / on_demand 按需判，老服务端的探索运行）。
 * 老后端、审的不是报告时没有
 */
export interface ApprovalReport {
  node_id: string
  label?: string
  stats?: EvidenceStats
  /** 结论句策略：off / require_citation / judge */
  claims?: string
  doc_artifact?: string
  judge?: { mode?: string; counts?: Record<string, unknown>; complete?: boolean; unjudged?: unknown; limits_hit?: unknown }
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
  /**
   * 在工具库里执行只演练、不发送：写外部系统的能力（被治理为写入，或绑定端点、视参数而定），和执行接口同一个判断。
   * 详情区的提示条照它说「这里只演练」，不说「请求会实际发出」（老服务端不给）
   */
  write_rehearsal?: boolean
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
  /**
   * upload：上传的表格（连接信息由系统维护，不能手改、不能重新探查，更新数据靠同名重新上传）；
   * manual：手工登记的连接。卡片按它分到「表格」「数据库」两个标签，不再按库文件路径猜
   */
  origin?: 'upload' | 'manual' | (string & {})
  /** 上传表格当前启用的版本。手工登记的源恒为 null */
  current_snapshot?: CurrentSnapshot | null
  /** simple 简单导入；recipe 按配方导入；手工登记的源为 null。老后端没有这个字段 */
  import_mode?: 'simple' | 'recipe' | null
  /** 按配方导入的源当前启用的配方 */
  current_recipe?: { id: string; seq: number; origin: string; activated_at: string | null; signed_by: string | null } | null
  /** 这个源最近一个未结束的导入（按配方导入的暂存区） */
  open_staging?: { id: string; kind: ImportStagingKind; status: ImportStagingStatus; created_at: string } | null
}

/** 上传表格当前启用的版本（DataSource.current_snapshot） */
export interface CurrentSnapshot {
  id: string
  /**
   * 这一版导入（发布）的时间。迁移补建的初始版本（raw_state 为 absent、没有文件名）例外：
   * 这里是迁移那一刻（升级后服务启动的时间），不是导入时间
   */
  created_at: string | null
  /** 上传时的文件名。迁移前上传的老版本没有记录，是空串 */
  file_name: string
  /** 原件：kept 保存在服务端；purged 已清除；absent 没有保存（迁移前上传的老版本） */
  raw_state: 'kept' | 'purged' | 'absent' | (string & {})
  /** 期 3：导入模式。简单导入、期 3 之前的版本没有或为 null（按每期替换理解） */
  mode?: 'replace' | 'accumulate' | (string & {}) | null
  /** 期 3：当前版本含几期（按期累积时可以多于 1） */
  periods?: number
  /** 期 3：各期统计期的最早起点、最晚终点；没有统计期为 null */
  period_start?: string | null
  period_end?: string | null
  /** 期 3：最近一次成为当前版本的时刻（回滚之后卡片写「…启用」，不再写当初的导入时间） */
  activated_at?: string | null
}

/**
 * 上传表格时解析器要用户先拍板的事（POST /api/datasources/upload 回 422，body 是
 * {detail, decision}）。mixed：数字列混入非数字；shape：交叉表、多块结构
 */
export type UploadDecision =
  | { kind: 'mixed'; details: { columns: UploadMixedColumn[] } }
  | {
    kind: 'shape'
    details: {
      reasons: UploadShapeReason[]
      /** 预告：选了按原样导入之后还会问的混合列（没有就是 undefined） */
      mixed?: UploadMixedColumn[]
      /** 预告是不是看完了整张表；为假时之后的混合列可能比这里多 */
      mixed_complete?: boolean
    }
  }

/** 一列以数字为主、混有非数字的值。values 是非数字的取值和个数（最多五种） */
export interface UploadMixedColumn {
  sheet: string
  table: string
  /** SQL 列名 */
  column: string
  /** 原表头 */
  header: string
  numeric: number
  nonnumeric: number
  values: { value: string; count: number }[]
}

/** 这张表不是一行一条记录的一条理由。cells 是 A1 坐标或区域，message 是给人看的一句话（已含坐标） */
export interface UploadShapeReason {
  sheet: string
  /** date_header / date_row / section_title / table_totals / formula_above */
  kind: string
  cells: string[]
  message: string
}

/** 导入回执里的一张表 */
export interface UploadedTable {
  name: string
  /** 原工作表名（CSV 是文件名） */
  sheet: string
  rows: number
  /** name 是 SQL 列名，header 是原表头（表头格为空时是空串） */
  columns: { name: string; type: string; header?: string | null }[]
  /** 导入区域（A1，表头行到最后一行数据） */
  region?: string | null
  /** 按原样导入、未经规整 */
  unshaped?: boolean
  blank_rows_skipped?: number
  /** 去掉的左右两侧整列为空的列（列字母） */
  columns_trimmed?: string[]
}

/** 导入时做过的类型转换：kind 是 thousands_separator / nonnumeric_to_null / kept_as_text */
export interface UploadConversion {
  table: string
  column: string
  kind: string
  count: number
  examples: unknown[]
}

/** POST /api/datasources/upload 成功时的回执 */
export interface UploadResult {
  source: DataSource
  /** 同名就地更新（新版本替换了旧版本），不是新建 */
  replaced: boolean
  import_id?: string
  snapshot_id?: string
  /** 同一份文件、同样的选项以前导入过，沿用了那次的库 */
  build_reused?: boolean
  /** 期 3：服务端的同版本数据文件曾被改动，已用本次上传的文件恢复（P3-SPEC 第 8 节遗留项 1） */
  build_restored?: boolean
  /** reason：hidden 隐藏工作表不导入；empty 没有内容。state：visible / hidden / veryHidden */
  skipped_sheets?: { sheet: string; state: string; reason: string }[]
  conversions?: UploadConversion[]
  warnings?: string[]
  tables: UploadedTable[]
}

// ---------------------------------------------------------------------------
// 按配方导入（期 2：/api/datasources/imports/*、/{id}/reupload|redraft|recipe）
// 字段与后端 recipe_types.py 的 dataclass 同名；后端多出来的字段界面不认也不报错
// ---------------------------------------------------------------------------

/** first 首次导入；reupload 上传新一期；redraft 修改配方（不换文件）；switch 从简单导入切换 */
export type ImportStagingKind = 'first' | 'reupload' | 'redraft' | 'switch' | (string & {})
export type ImportStagingStatus = 'drafting' | 'trialed' | 'rejected' | 'committed' | 'discarded' | 'expired' | (string & {})

/**
 * 配方 JSON（agentlab-recipe/2）。结构由服务端的静态校验把关，界面只按已知字段读写，所以这里是宽松类型：
 * 表单改哪个字段就按路径写哪个字段，整份经 PUT recipe 交给服务端
 */
export type Recipe = Record<string, any>

/** 配方静态校验的问题：path 是 JSON Pointer（如 /tables/0/units/客流），message 是整句人话 */
export interface RecipeProblem {
  path: string
  code: string
  message: string
  /** 期 3：这条问题对应的修复提议 id（Staging.fixes 里的 FixProposal.id）；没有提议为 [] 或缺省 */
  fix_ids?: string[]
}

/** structure 结构问题；data_quality 数据质量；confirm 需确认；input 需要录入；recipe 配方不合法 */
export type ImportProblemCategory = 'structure' | 'data_quality' | 'confirm' | 'input' | 'recipe' | (string & {})

/** 干跑、试运行的问题（契约 Problem）。cells 是「工作表!A1」或「工作表!A1:B2」，最多 20 个 */
export interface ImportProblem {
  code: string
  category: ImportProblemCategory
  message: string
  cells?: string[]
  /** 修复按钮的种类（FixKind）；界面不靠它放按钮，只按 fix_ids 放（P3-SPEC 3.1） */
  fix?: string | null
  /** 期 3：构造修复补丁的参数（只含原文和坐标），界面不直接用：补丁由服务端算 */
  fix_args?: Record<string, any> | null
  /** 期 3：这条问题对应的修复提议 id（Staging.fixes 里的 FixProposal.id）；没有提议为 [] 或缺省 */
  fix_ids?: string[]
}

export type CheckStatus = 'passed' | 'mismatch' | 'unverifiable' | 'info' | (string & {})

/** 一条核对的结果（契约 CheckResult）。acceptable：可以写理由接受 */
export interface CheckResult {
  id: string
  kind: string
  title: string
  status: CheckStatus
  category: 'structure' | 'data_quality' | 'info' | (string & {})
  checked?: number
  failed?: number
  unverifiable?: number
  sql?: string | null
  params?: unknown[]
  details?: string[]
  cells?: string[]
  acceptable?: boolean
  reasons?: Record<string, number>
}

/**
 * 确认项的来源（契约 ConfirmItem.source），确认清单按它分组（P3-SPEC 9.5）：recipe 配方；edit 修改（fix:*、select:*、
 * redraft_adopted）；accumulate 累积；outside / sheet / context 本期；diff 差异；switch 切换
 */
export type ConfirmSource = 'recipe' | 'diff' | 'outside' | 'sheet' | 'context' | 'switch' | 'edit' | 'accumulate' | (string & {})

/** 启用前要逐条勾选的一项（契约 ConfirmItem；id 的取值见 P2-SPEC 7.5、P3-SPEC 9.5） */
export interface ConfirmItem {
  id: string
  label: string
  detail?: string
  required?: boolean
  /** 缺省按 recipe */
  source?: ConfirmSource
}

/** 上传新一期与上一期相比的一条差异（契约 DiffItem） */
export interface DiffItem {
  kind: string
  label: string
  detail?: string
  requires_confirm?: boolean
  confirm_id?: string | null
}

/** 建议卡片：一条建议加一句理由，指向网格上的格子 */
export interface Card {
  id: string
  title: string
  reason: string
  cells?: string[]
  /** 对应的待确认问题（Question.id） */
  question?: string | null
}

export interface QuestionOption {
  value: string
  label: string
  /** 选它时必须写理由（如「不登记」） */
  needs_reason?: boolean
}

/** 待确认问题：只有封闭的选项；选项对配方的修改由服务端应用，界面只发选了哪一项 */
export interface Question {
  id: string
  text: string
  options: QuestionOption[]
  default?: string | null
}

/** 已选的回答（StagingOut.answers 的值） */
export interface QuestionAnswer {
  value: string
  reason?: string | null
}

/** 规则或 AI 起草的结果（契约 Draft，不含给模型看的那份原因） */
export interface Draft {
  recipe: Recipe | null
  complete: boolean
  origin: 'rules' | 'ai' | (string & {})
  cards?: Card[]
  questions?: Question[]
  failures?: string[]
}

/** 一次模型调用的用量 */
export interface AiUsage {
  model: string
  input_tokens: number
  output_tokens: number
  cost_usd: number
  ok: boolean
  at: string
  attempt?: number
}

/** 一张可见工作表的原始网格预览 */
export interface GridPreview {
  sheet: string
  bounds: string | null
  total_rows?: number
  total_cols?: number
  /** 预览只取了前面的部分 */
  truncated?: boolean
  rows?: number[]
  cols?: number[]
  /** [行, 列, 显示文字, 种类]；种类：text / number / date / bool / error / formula / formula_uncached */
  cells: [number, number, string, string][]
  /** A1 → 公式原文 */
  formulas?: Record<string, string>
  merges?: string[]
  hidden_rows?: number[]
  hidden_cols?: number[]
}

/** 格子的去向（契约 Role），按区域给出。期 3 新增角色 ignored（按配方忽略） */
export interface RegionMark {
  sheet: string
  role: string
  ref: string
  /** 期 3：所在块的 id（区域外文字、统计期为 null）：网格按块描边、框选的重放比对按它过滤 */
  block?: string | null
}

/** 格子账：每张工作表的非空格和各去向的个数 */
export interface LedgerSheet {
  sheet: string
  nonempty_scan: number
  nonempty_read: number
  roles?: Record<string, number>
  unclaimed?: number
}

export interface ReceiptTable {
  name: string
  sheet: string
  kind: 'data' | 'reported_total' | (string & {})
  columns: { name: string; type: string; header?: string | null; unit?: string | null; role?: string }[]
  grain: string[]
  rows: number
  sources?: string[]
}

export interface PeriodOut {
  start: string
  end: string
  source: 'cells' | 'human' | (string & {})
  cells?: string[]
  signed_by?: string | null
  texts?: Record<string, string>
  annotated?: Record<string, string>
}

export interface OutsideText {
  sheet: string
  /** 带工作表名的坐标「工作表!A1」（与问题的 cells 同一写法） */
  cell: string
  text: string
  kind: 'text' | 'text_digits' | (string & {})
  period_source?: boolean
  /**
   * 期 4：所在行或列是否被隐藏（执行器写入）。true 在隐藏行列里；false 可见；null 或没有这个键：导入时没有记录
   * （期 4 之前的清单）。裁判摘录只送 false 的项，界面照常显示全部区域外文字
   */
  hidden?: boolean | null
}

/** 试运行回执（Extraction 去掉问题和核对之后给界面看的部分） */
export interface TrialReceipt {
  ledger?: LedgerSheet[]
  tables?: ReceiptTable[]
  period?: PeriodOut | null
  placeholders?: Record<string, number>
  outside_text?: OutsideText[]
  canonicalized_total?: number
  blank_rows_skipped?: number
  formula_cells_accepted?: number
  full_calc_on_load?: boolean
  db_sha256?: string
  /** 期 3：排除的行（H2）。期 3 之前的回执没有这个字段：缺省表示「未记录」，不等于「没有排除」。
   * 服务端：试运行回执（TrialOut.receipt）只抄 recipe_imports._RECEIPT_VIEW 白名单里的键，WP-5 要把
   * rows_excluded 加进去；导入清单的 receipt 也要有。期 3 的试运行一律带这个键（没有排除时是 []） */
  rows_excluded?: ExcludedRows[]
  [key: string]: any
}

/** passed 通过；needs_input 需要录入统计期；needs_decision 有可接受的核对待写理由；rejected 拒收 */
export type TrialStatus = 'passed' | 'needs_input' | 'needs_decision' | 'rejected' | (string & {})

/** 最近一次试运行（StagingOut.trial） */
export interface TrialOut {
  trial_id: string
  status: TrialStatus
  receipt: TrialReceipt
  problems: ImportProblem[]
  checks: CheckResult[]
  confirm_items: ConfirmItem[]
  /** 可以写理由接受、本次未通过的核对 id */
  acceptable: string[]
  /** 表 → 说明预览 */
  notes?: Record<string, { comment: string; columns?: Record<string, string> }>
  diff?: DiffItem[] | null
  same_as_import?: { id: string; seq: number } | null
  base_snapshot_id?: string | null
  /** 期 3：累积计划。新配方是按期累积、或现行是按期累积时给出 */
  accumulate?: AccumulatePlan | null
  /** 期 3：工作配方与现行配方不同时的新旧对照 */
  recipe_compare?: RecipeComparison | null
  /** 期 3：并集的结构核对 U1–U3（物化了才有） */
  union_checks?: CheckResult[] | null
  /** 期 3：同一份构建以前被接受过的理由，只读显示，不预填 */
  prior_acceptances?: PriorAcceptance[]
}

/** AI 起草的结果摘要 */
export interface AiDraftSummary {
  draft: Draft | null
  usage?: AiUsage[]
  attempts?: number
  total_tokens?: number
  cost_usd?: number
  error?: string | null
}

/** 暂存区（一次未提交的按配方导入）：GET /imports/{id} 等接口的返回 */
export interface Staging {
  id: string
  kind: ImportStagingKind
  status: ImportStagingStatus
  source: { id: string; name: string; exists: boolean; import_mode: 'simple' | 'recipe' | null }
  file: { name: string; size: number; sha256_prefix: string }
  sheets?: {
    name: string; state: string; bounds: string | null; nonempty: number; merged?: number
    formulas?: number; formulas_uncached?: number; hidden_rows?: number; hidden_cols?: number
  }[]
  skipped_sheets?: { sheet: string; state: string }[]
  full_calc_on_load?: boolean
  grids: GridPreview[]
  draft: Draft | null
  ai_draft: AiDraftSummary | null
  /** offered：这次导入提供 AI 起草入口；available：模型接入可用 */
  ai: { offered: boolean; available: boolean; reason?: string; model?: string; provider?: string }
  ai_consents?: { signed_by: string | null; at: string; model: string; compressed_sha256: string; chars: number }[]
  recipe: Recipe | null
  recipe_origin?: string | null
  recipe_problems: RecipeProblem[]
  answers: Record<string, QuestionAnswer>
  cards: Card[]
  questions: Question[]
  draft_problems: ImportProblem[]
  /** 干跑只检查了每张工作表的前若干行 */
  draft_partial: boolean
  /** 分段标题原文 → 常量可选的词 */
  candidates: Record<string, string[]>
  /** 单位词表 */
  units: string[]
  marks: RegionMark[]
  trial: TrialOut | null
  context_inputs?: Record<string, { start: string; end: string; signed_by?: string | null }>
  /** 最近一次试运行时数据源的当前版本（与 trial.base_snapshot_id 同值；提交时对不上回 409 base_changed）。
   *  数据源还没有当前版本（首次导入）或没试运行过时为 null；试运行作废后保留原值。
   *  所以不能拿它是不是 null 判断「试运行过没有」，那要看 trial 与 status */
  base_snapshot_id?: string | null
  /** 提交后生成的导入记录 id；未提交为 null */
  committed_import_id?: string | null
  /** 期 3：修复提议（按当前问题现算）。问题对象上的 fix_ids 指向这里的 id */
  fixes?: FixProposal[]
  /** 期 3：当前工作配方的哈希（撤销是否可用、预览是否过期都按它判断） */
  recipe_sha256?: string | null
  /** 期 3：已应用的修改（修复、框选），按 seq 升序 */
  edits?: StagingEdit[]
  /** 期 3：改配方后不再成立、被移除的回答 */
  answers_dropped?: AnswerDropped[]
  created_at?: string
  updated_at?: string
  expires_at?: string
}

/** GET draft-ai/preview：将要发给模型的压缩表示原文 */
export interface AiPreview {
  text: string
  chars: number
  /** 同意令牌：绑定全文、接入名和模型 id，POST draft-ai 原样带回（三者任一变了服务端回 409 ai_preview_stale） */
  sha256: string
  /** 全文本身的 sha256（留痕用） */
  text_sha256?: string
  model: string
  provider: string
}

/** POST imports/{id}/commit 的返回 */
export interface CommitOut {
  source: DataSource
  import_id: string
  snapshot_id: string
  build_id: string
  recipe_id: string
  build_reused: boolean
  /** 与某次导入完全相同，未新建版本 */
  unchanged: boolean
  /** 期 3：服务端的同版本数据文件曾被改动，已用本次上传的文件恢复 */
  build_restored?: boolean
  /** 期 3：启用后当前版本含几期 */
  parts?: number
  /** 期 3：与此前某个版本内容相同，直接启用了那个版本 */
  snapshot_reused?: boolean
}

/** GET /{source_id}/recipe：当前启用的配方（补全了默认值） */
export interface CurrentRecipe {
  recipe_id: string
  seq: number
  origin: string
  recipe: Recipe
  recipe_sha256: string
  confirmations: { id: string; label: string; at: string }[]
  signed_by: string | null
  activated_at: string | null
}

// ---------------------------------------------------------------------------
// 期 3：修复按钮、框选、按期累积、配方对照、版本页（P3-SPEC 第 9 节）。字段与后端 recipe_types.py 的 dataclass
// 和接口的 JSON 同名（Selection 的 as 除外：后端 dataclass 叫 as_，JSON 里是 as）
// ---------------------------------------------------------------------------

/** 修复按钮的九种（契约 FIX_KINDS） */
export type FixKind =
  | 'remove_label' | 'add_label' | 'edit_members' | 'rename_title' | 'declare_total' | 'ignore_cells'
  | 'declare_placeholder' | 'declare_hidden' | 'rename_sheet' | (string & {})

/** 修复提议的一个封闭选项。效果由服务端算：界面只发 fix_id、value 和理由 */
export interface FixOption {
  value: string
  label: string
  detail?: string
  /** 选它必须写理由（1–200 字）：理由填好后才预览，理由改了要重新预览 */
  needs_reason?: boolean
  breaking?: boolean
}

/** 提议对应哪一条问题：problem / recipe_problem 按下标，sheet_renamed 按本期的工作表名 */
export interface FixAnchor {
  kind: 'problem' | 'recipe_problem' | 'sheet_renamed' | (string & {})
  index?: number | null
  sheet?: string | null
}

/** 修复提议（Staging.fixes 的一项，契约 FixProposal） */
export interface FixProposal {
  /** fx- 加 12 位十六进制：同样的问题得到同样的 id */
  id: string
  kind: FixKind
  problem_code: string | null
  title: string
  cells: string[]
  target: Record<string, any>
  options: FixOption[]
  anchor: FixAnchor
}

/** 框选「选它是什么」的封闭取值（契约 SELECTION_AS） */
export type SelectionAs =
  | 'list' | 'crosstab' | 'segment' | 'derived' | 'section_title' | 'ignore_rows' | 'ignore_columns' | 'ignore_outside'

/** 框选的参数（按 as 取用：list 用 header_rows、table、bottom；segment 用 role、table；derived 用 keep；
 * section_title 用 segment；ignore_* 用 reason） */
export interface SelectionOptions {
  /** 1–3，默认 1 */
  header_rows?: number
  table?: string
  /** box 以框的下边为准（默认）；auto 下边界按规则推断 */
  bottom?: 'box' | 'auto'
  role?: 'measures' | 'dimension'
  keep?: boolean
  segment?: string
  reason?: string
  [key: string]: unknown
}

/** 一次框选（edits/preview、edits/apply 请求里的 selection） */
export interface SelectionRequest {
  sheet: string
  /** A1 区域，不带工作表名，列字母大写、不带 $（「C5」或「C5:F8」）：别的写法服务端回 422 edit_invalid */
  ref: string
  as: SelectionAs
  options?: SelectionOptions
}

/** edits/preview、edits/apply 的请求：fix 与 selection 二选一。seq 是界面的预览序号，原样回传 */
export type EditRequest =
  | { fix: { id: string; option: string; reason?: string }; selection?: never; seq?: number }
  | { selection: SelectionRequest; fix?: never; seq?: number }

/** 框选换算出的锚点（契约 Anchor），界面显示「按文字定位」 */
export interface EditAnchor {
  kind: 'header' | 'row_label' | 'section_title' | 'total_word' | 'axis' | 'after_title' | 'outside_text' | (string & {})
  text: string
  cell?: string | null
}

/** 框选的期望区域与重放结果的比对（契约 ReplayCompare） */
export interface ReplayCompare {
  /** 列表：{header, data, total}；交叉表：{axis, labels, values} */
  expected: Record<string, string | null>
  actual: Record<string, string | null>
  match: boolean
  diffs: string[]
  /** 部分干跑时只比较前多少行；完整比较为 null */
  window_rows?: number | null
}

/** POST edits/preview 的返回（P3-SPEC 9.4 EditPreview） */
export interface EditPreview {
  ok: boolean
  kind: 'fix' | 'selection' | (string & {})
  /** 目标键（remove_label:日间:7-8）；确认项 fix:<key> / select:<key> 用它 */
  key: string
  title: string
  summary: string[]
  /** 按完整形式写的 JSON Patch：不给用户看，最多收在技术细节里 */
  ops: { op: string; path: string; value?: unknown }[]
  anchors: EditAnchor[]
  notes: string[]
  /** ok=false 时的原因（category=recipe），坐标可点 */
  problems: ImportProblem[]
  recipe_sha256_before: string | null
  /** apply 时原样作为 expected_sha256 发回 */
  recipe_sha256_after: string | null
  recipe_problems: RecipeProblem[]
  dry_run: { problems: ImportProblem[]; partial: boolean; marks: RegionMark[] } | null
  replay: ReplayCompare | null
  /** 对现行配方的破坏性变化：表名 → 人话列表；空对象表示没有。
   * 注意：这不是后端 EditResult.breaking（那是服务端内部的布尔）。WP-5 组装预览时必须总是用
   * table_changes 算出的字典覆盖它；收到布尔说明服务端漏了覆盖，Object.entries(false) 是 []，
   * 破坏性变化会从预览里消失 */
  breaking: Record<string, string[]>
  accumulate_change: 'same' | 'compatible' | 'retire' | 'semantic' | (string & {}) | null
  /** 当前工作配方对比修改后的配方（复用 RecipeCompare 显示） */
  compare: RecipeComparison | null
  /** 框选：换算出的期望区域 */
  expected?: Record<string, string | null> | null
  /** 框选：新增或替换的块 id */
  block?: string | null
  /** 请求里的预览序号，原样回传：不是最新一次的回包丢弃 */
  seq?: number | null
}

/**
 * 暂存区里已应用的一次修改（Staging.edits 的一项）。服务端 recipe_imports._edit_out 从修改记录里去掉撤销用的起点
 * （before、base_sha256_after）和补丁（ops，只进导入清单），其余原样给出
 */
export interface StagingEdit {
  seq: number
  kind: 'fix' | 'selection' | (string & {})
  key: string
  title: string
  /** 预览时的人话摘要（EditPreview.summary），应用时原样记下 */
  summary?: string[]
  at: string
  signed_by: string | null
  recipe_sha256_before: string | null
  recipe_sha256_after: string | null
  /** 之后整份替换过配方（PUT）：不能撤销，也不再出确认项 */
  superseded: boolean
  /** 只有最后一条、未被覆盖的为 true */
  undoable: boolean
  /** kind=fix：人选的是哪条提议、哪一项、写的理由（与 edits/apply 请求里的 fix 同形）。WP-5 评审修复之前记下的修改没有 */
  fix?: { id: string; option: string; reason: string | null }
  /** kind=selection：这次框选（Selection.to_json，键是 as，options 总是对象）。同上，旧记录没有 */
  selection?: SelectionRequest & { options: SelectionOptions }
}

/** 改配方后被移除的回答：changed 需要重新回答；gone 问题已不适用。text 是问题文字（界面不露 id） */
export interface AnswerDropped {
  id: string
  text: string
  reason: 'changed' | 'gone' | (string & {})
}

/** 同一份构建以前被接受过的理由（TrialOut.prior_acceptances），只读显示 */
export interface PriorAcceptance {
  check_id: string
  reason: string
  signed_by: string | null
  at: string
  /** 第几次导入 */
  import_seq: number
}

/** 排除的行的原因（契约 ExcludedRows.reason） */
export type ExcludedReason =
  | 'hidden_excluded' | 'blank_skipped' | 'ignored_rows' | 'ignored_outside' | 'after_stop' | 'total_not_kept'
  | 'subtotal'
  | (string & {})

/** 回执里的「排除的行」（契约 ExcludedRows）。按表头忽略的列不在这里（记在回执的 ignored_columns） */
export interface ExcludedRows {
  sheet: string
  reason: ExcludedReason
  /** [[起, 止], …]，1 起的行号闭区间 */
  rows: [number, number][]
  cells: number
  anchor?: string | null
  block?: string | null
}

/** ReceiptBlocks.tsx 的「排除的行」：向导回执和版本页的清单视图共用。onFocus 收「工作表!A1」 */
export interface RowsExcludedProps {
  rows: ExcludedRows[] | null | undefined
  onFocus?: (cell: string) => void
}

/** ReceiptBlocks.tsx 的回执摘要：receipt 是试运行回执或导入清单里的 receipt（形状相同） */
export interface ReceiptSummaryProps {
  receipt: TrialReceipt | null | undefined
  onFocus?: (cell: string) => void
}

/** 累积计划的 action（契约 ACCUMULATE_ACTIONS） */
export type AccumulateAction = 'replace' | 'first' | 'append' | 'replace_period' | 'restart' | 'rejected' | (string & {})

/** 累积计划里的一期。本期 import_id、seq 为 null，new 为 true */
export interface AccumulatePart {
  import_id: string | null
  seq: number | null
  start: string
  end: string
  file_name: string
  new: boolean
  /** 表 → 该期行数 */
  rows: Record<string, number>
  /** 该期配方不满足按期累积的原因（契约 accumulate_blockers） */
  blockers: RecipeProblem[]
}

/** 计划里提到的一期（被替换、被移出、部分重叠）：至少有起止，其余字段同 AccumulatePart、可能缺省 */
export interface AccumulatePeriodRef {
  import_id?: string | null
  seq?: number | null
  start: string | null
  end: string | null
  file_name?: string
  rows?: Record<string, number>
}

/** 表或列的新增、退役：column 为 null 表示整张表 */
export interface TableColumnRef {
  table: string
  column: string | null
  periods?: string[]
}

/** 各期维度取值的差异（P3-SPEC 2.5） */
export interface LabelSetDiff {
  table: string
  column: string
  segment: string
  periods: { start: string; end: string; missing: string[]; extra: string[] }[]
}

/** 累积计划（TrialOut.accumulate，P3-SPEC 9.4 AccumulatePlan） */
export interface AccumulatePlan {
  mode: 'replace' | 'accumulate' | (string & {})
  action: AccumulateAction
  /** 本期统计期。source：cells 取自表内，human 人工录入；每期替换且没有统计期时 start、end 为 null */
  period: { start: string | null; end: string | null; source?: 'cells' | 'human' | (string & {}) }
  parts: AccumulatePart[]
  replaces: AccumulatePeriodRef | null
  /** restart、模式切换时移出当前版本的各期：界面标「将不在当前版本中」 */
  dropped: AccumulatePeriodRef[]
  overlaps: AccumulatePeriodRef[]
  gaps: { start: string; end: string }[]
  backfill: boolean
  change: 'same' | 'compatible' | 'retire' | 'semantic' | (string & {})
  added: TableColumnRef[]
  retired_new: TableColumnRef[]
  retired_existing: TableColumnRef[]
  semantic: string[]
  label_sets: LabelSetDiff[]
  mode_switch: 'replace->accumulate' | 'accumulate->replace' | (string & {}) | null
  reason: string | null
  /** 物化了的并集：rows 是「启用后当前版本」的行数；没有物化为 null（此时等于本期） */
  union: { union_id: string; db_sha256: string; rows: Record<string, number> } | null
}

/** 新旧配方对照里一列的摘要 */
export interface CompareColumnBrief {
  type: string
  unit?: string | null
  source?: string | null
}

export type CompareStatus = 'added' | 'removed' | 'changed' | 'same' | (string & {})

/** 新旧配方对照（P3-SPEC 9.4 RecipeComparison，WP-3 compare_recipes 产出） */
export interface RecipeComparison {
  tables: {
    name: string
    status: CompareStatus
    columns: {
      name: string
      status: CompareStatus
      old: CompareColumnBrief | null
      new: CompareColumnBrief | null
      /** table_changes 的 kind（type / unit / source / store / const_value …） */
      changes: string[]
    }[]
    grain: { old: string[] | null; new: string[] | null; changed: boolean }
    kind: { old: string | null; new: string | null }
    /** 这张表除单位以外的破坏性变化（table_changes 的原话），是完整清单；单位变化只在 columns[].changes 与 units_changed 里 */
    breaking: string[]
  }[]
  segments: {
    id: string
    status: CompareStatus
    title: { old: string | null; new: string | null }
    labels: { added: string[]; removed: string[] }
    ignore: { added: string[]; removed: string[] }
  }[]
  relations: { id: string; status: CompareStatus; old: string | null; new: string | null }[]
  sheets: { id: string; name: { old: string | null; new: string | null } }[]
  mode: { old: string | null; new: string | null }
  breaking: boolean
  /** 单位变化的列（界面排在最前） */
  units_changed: { table: string; column: string; old?: string | null; new?: string | null }[]
  accumulate: {
    change: string
    added: TableColumnRef[]
    retired_new: TableColumnRef[]
    retired_existing: TableColumnRef[]
  } | null
}

/** POST imports/{id}/redraft-rules 的返回：不改工作配方，「采用」时再 PUT recipe（origin: rules_redraft） */
export interface RedraftRulesOut {
  draft: Draft
  aligned_recipe: Recipe | null
  /** 名字对齐的人话说明 */
  alignment: string[]
  compare: RecipeComparison | null
}

// ---- 版本页（P3-SPEC 第 7 节）

/** 一期的统计期引用：简单导入或没有统计期的期，start、end 为 null，带 file_name */
export interface PeriodRef {
  start: string | null
  end: string | null
  file_name?: string
}

/** 版本列表里一个版本的一期 */
export interface SnapshotPart {
  import_id: string
  seq: number
  period_start: string | null
  period_end: string | null
  file_name: string
  raw_state: 'kept' | 'purged' | 'absent' | (string & {})
  status: string
  /** 表 → 该期行数 */
  rows: Record<string, number>
  /** 接受的条数（数据质量不成立 / 合计无法核对） */
  overrides: number
  waivers: number
  revoked: boolean
}

/** GET /{source_id}/snapshots 的一行（7.1）。当前版本排第一 */
/** 快照不能启用的原因代码（后端 recipe_types.SnapshotReasonCode；VERSIONS_TEXT.notActivatable 的键与它相同） */
export type SnapshotReasonCode = 'current' | 'retired' | 'file_lost' | 'contains_revoked'

export interface SnapshotOut {
  id: string
  current: boolean
  mode: 'replace' | 'accumulate' | (string & {}) | null
  created_at: string | null
  activated_at: string | null
  /** 简单导入为 null */
  recipe: { id: string; seq: number } | null
  parts: SnapshotPart[]
  /** 表 → 启用这个版本后的行数 */
  tables: Record<string, number>
  db_sha256_prefix: string
  /** 数据文件的字节数 */
  db_size: number | null
  /** 记录没回收、文件在 */
  available: boolean
  /** 引用它的运行数（任何状态） */
  pinned_runs: number
  activatable: boolean
  /** 不能启用的原因（人话）；能启用为 null。服务端取后端契约 SNAPSHOT_NOT_ACTIVATABLE[reason_code]，
   * 与 VERSIONS_TEXT.notActivatable 逐字相同 */
  reason: string | null
  /** 不能启用的原因的代码（契约补充：7.1 只给了人话，available=false 同时覆盖「已回收」和「数据文件已丢失」，
   * 界面分不出来）。能启用为 null。界面按 VERSIONS_TEXT.notActivatable[reason_code] 显示，缺省或不认识时
   * 退回显示 reason，不去匹配 reason 的原文。几种同时成立时服务端取第一个：current > retired > file_lost >
   * contains_revoked */
  reason_code?: SnapshotReasonCode | null
  /** 当前的遮罩列里，这个版本没有同名列的 */
  mask_lost: string[]
  /** 与当前版本相比，启用后增减的期 */
  periods_diff: { added: PeriodRef[]; removed: PeriodRef[] }
}

/** POST …/snapshots/{snapshot_id}/activate 的请求（7.2） */
export interface ActivateSnapshotBody {
  confirm: true
  /** 界面看到的当前版本 id：对不上回 409 base_changed */
  expected_current_snapshot_id: string | null
  /** mask_lost 非空时必填，与它相同 */
  ack_mask_lost?: string[]
  reason?: string
  signed_by?: string | null
}

export interface ActivateSnapshotOut {
  source: DataSource
  snapshot_id: string
  previous_snapshot_id: string | null
  recipe_id: string | null
}

/** 导入清单、快照清单（7.3）。verified：取回时内容哈希复验通过 */
export interface ManifestOut {
  artifact_id: string | null
  verified?: boolean
  /** import_manifest；简单导入是 build_report（构建回执）；snapshot_manifest；单期快照是 snapshot_view（合成的视图） */
  kind: 'import_manifest' | 'build_report' | 'snapshot_manifest' | 'snapshot_view' | (string & {})
  content: Record<string, any>
}

/** POST …/imports/{import_id}/remove 的请求（7.4，累积模式） */
export interface RemovePeriodBody {
  confirm: true
  expected_current_snapshot_id: string | null
  /** 1–500 字，必填 */
  reason: string
  signed_by?: string | null
}

export interface RemovePeriodOut {
  source: DataSource
  snapshot_id: string
  removed_import_id: string
  /** 复用（或复活）了同内容的已有版本 */
  reused: boolean
}

/** POST …/imports/{import_id}/revoke-acceptance 的请求（7.5） */
export interface RevokeAcceptanceBody {
  confirm: true
  expected_current_snapshot_id: string | null
  reason: string
  signed_by?: string | null
  /** 替换模式：取自 revoke_plan.target_snapshot_id */
  expected_target_snapshot_id?: string | null
  /** 回滚目标有遮罩列丢失时带上，与 revoke_plan.mask_lost 相同 */
  ack_mask_lost?: string[]
}

export interface RevokeAcceptanceOut {
  source: DataSource
  snapshot_id: string
  action: 'rollback' | 'remove_period' | (string & {})
}

/** 一条接受（导入记录的 acceptances） */
export interface ImportAcceptance {
  check_id: string
  kind: 'override' | 'waiver' | (string & {})
  reason: string
  signed_by: string | null
  at: string | null
}

/** 作废接受的预案（只对在当前版本里、带接受的导入给）：确认框照它写 */
export interface RevokePlan {
  /** 不能作废时为 null，reason 写原因 */
  action: 'rollback' | 'remove_period' | (string & {}) | null
  target_snapshot_id: string | null
  /** 替换模式的回滚目标 */
  target: { parts: PeriodRef[]; recipe_seq: number | null; mode: string | null; simple: boolean } | null
  /** 累积模式：移除后的各期 */
  result_parts: PeriodRef[]
  gaps: { start: string; end: string }[]
  mask_lost: string[]
  reason: string | null
}

/** GET /{source_id}/imports 的一行（期 1 接口，期 3 补字段，7.6） */
export interface ImportRecord {
  id: string
  seq: number
  build_id: string
  file_name: string
  file_size: number | null
  raw_sha256: string | null
  raw_state: 'kept' | 'purged' | 'absent' | (string & {})
  status: string
  /** 清除记录 {at, reason, signed_by…}；没清除为 null */
  purged: Record<string, any> | null
  /** 在当前版本里（与 in_current 同值） */
  current: boolean
  created_at: string | null
  activated_at: string | null
  recipe_id: string | null
  period_start: string | null
  period_end: string | null
  signed_by: string | null
  /** 署名是不是认证过的身份：按这一期导入清单里记下的（05-已知遗留 (c)），旧清单记的是 false。老后端不给 */
  signed_by_verified?: boolean
  /** 接受的条数 */
  overrides: number
  waivers: number
  /** 期 3 */
  acceptances?: ImportAcceptance[]
  revoked?: { at: string; reason: string; signed_by: string | null; signed_by_verified: boolean } | null
  manifest_artifact?: string | null
  recipe_seq?: number | null
  in_current?: boolean
  /** 表 → 该期行数 */
  rows?: Record<string, number>
  revoke_plan?: RevokePlan | null
  /** 同一份原件内容在其他导入记录里的引用，按源汇总（本源也算）。source_name：现在的后端对已删除的源写
   *  「已删除的数据源」，老后端或老数据可能是 null——声明成可空，让界面的兜底由类型强制（同 also_purged） */
  raw_shared_with?: { source_name: string | null; count: number }[]
  /** 引用这份原件的未结束导入个数 */
  raw_open_stagings?: number
}

/** POST …/imports/{import_id}/purge-raw 的请求（理由必填） */
export interface PurgeRawBody {
  confirm: true
  reason: string
  signed_by?: string | null
}

export interface PurgeRawOut {
  import: ImportRecord
  file_deleted: boolean
  /** 同一份内容一并清除的其他导入记录 */
  also_purged: { id: string; source_id: string; source_name: string | null; seq: number; file_name: string }[]
  /** 一并放弃的未完成导入 */
  discarded_stagings: string[]
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
  /**
   * 发布门禁里的 SQL 检查：这条检查本来的级别。level 说的是拦不拦（已发布档一律 warning），它说的是错误还是提醒。
   * 别的问题、老服务端不给
   */
  sql_level?: 'error' | 'warning' | null
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
  /** 这条修复落在哪个字段：点名的模型接入一处一条（同一节点上的几处按它区分） */
  field?: string | null
  label: string
  preview?: { field?: string | null; before?: unknown; after?: unknown } | null
  /** handoff：选了这一项就是「交给 Copilot」（G4 的「它在做计算」）。只认这个标记，不认值——
   *  别的选项拿节点 id 当值，节点 id 恰好叫 copilot 的报告撰写节点照样是普通候选。
   *  demo：点名的模型接入的候选，修完以后执行时落到演示类接入（不调用真实模型），选项名旁加醒目的标签 */
  options?: { value: unknown; label: string; hint?: string | null; handoff?: boolean; demo?: boolean }[]
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
  /** rejected：这一步被人驳回（node.failed 带 reason=rejected，run/trace 的 NodeState 同名） */
  status: 'idle' | 'running' | 'done' | 'failed' | 'waiting' | 'skipped' | 'cancelled' | 'suspended' | 'rejected'
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
  /**
   * 前端建的只有 chat、canvas；服务端另建两种：app 是应用编辑器的助手对话，blueprint 是蓝图对话（docs/design/normal-user-apps.md
   * §2.2，权限跟着蓝图走，经 GET /api/blueprints/{id}/conversation 读）
   */
  kind: 'chat' | 'canvas' | 'app' | 'blueprint'
  workflow_id?: string | null
  archived: boolean
  created_at?: string
  last_active_at?: string
  turn_count: number
  /** 列表里的副标题，让人一眼认出是哪次聊天 */
  last_question: string
  /**
   * 最后一轮停在哪。左栏对这次没打开过的会话也能标出运行中、待审批、失败、被驳回（rejected：运行被审批人驳回结束，
   * 服务端 cancelled + end_reason=rejected）。没有轮次时为 null；老后端不给
   */
  last_status?: 'running' | 'waiting' | 'error' | 'cancelled' | 'rejected' | 'suspended' | 'done' | null
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
  /**
   * 要不要带着原图重来一次、重来时交给助手的话（服务端 backend/app/chat/turn.py 按这一轮试过几次判）：要就是那句话，不要
   * 是 null。老服务端没有这个字段，问数据页照原来的规则自己判
   */
  retry_instruction?: string | null
  /** 复核这一次的用量（服务端 providers/usage.py 的汇总，没调模型时全 0）：这一轮的花费 = 助手 + 运行 + 复核 */
  usage?: {
    cost_usd?: number | null; unpriced_calls?: number; calls?: number; input_tokens?: number; output_tokens?: number
    by_model?: Record<string, { input_tokens: number; output_tokens: number; calls: number; cost_usd: number
      unpriced_calls: number }>
  }
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
   * 结论句裁判的判定（四期）：只有送了裁判的候选句有。正式运行（以及交给人工审批的探索运行）里节点当场判的
   * 进封存（post_seal false）；其余探索运行里候选句先标 unjudged + reason on_demand，点开再判
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
  /** inline：节点里当场判（正式运行，以及交给人工审批的探索运行）；on_demand：其余探索运行按需 */
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
              judge_meta?: EvidenceJudgeMeta | null
              /**
               * 期 4：被反方审过的句子 → 判断（_challenge_verdicts，和片段接口的 unit.challenge 同一个读法，每句的判断
               * 一字不差）。正文据此给被审过的句子挂句末标记、点开整句面板。一句都没审、文档读不出来、老服务端都没有这个键
               */
              challenge_verdicts?: EvidenceChallengeVerdicts }[]
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
  /**
   * tool_cell：按位置取的格（vars.q.rows[0][0]）穿过了「只是把调用工具节点的结果原样按 JSON 解析」的整形节点，这是那个
   * 节点的 id；through_label 是它在图里的名字（证据接口补的）。cell() 取的格没有这两个键
   */
  through?: string | null
  through_label?: string
  /** unresolved 的原因（人话） */
  reason?: string
  /** 输入对到的查询在报告目录里的编号（Q3）：接口按工件补的全局编号 */
  query?: string | null
  /** 输入落在哪一格，全局编号（Q3.r5.amount） */
  cell?: string
  /** agent 的数组字段：引用的行段到了截断查询结果的末行，后面还有没取回的行（query 步骤上是快照本身截断了） */
  truncated?: boolean | null
  /**
   * 期 4：取自数据源设置了遮罩的列（_withheld）：服务端已把值换成「已遮罩」、拿掉 model_value / snapshot_value，这里写明
   * 是哪几列。没遮的没有这个键
   */
  masked?: string[]
  /**
   * 期 4：遮没遮核对不了（查询快照、取值链上的产出读不出来）：值同样换成「已遮罩」，这是原因（人话，服务端写好的一句）。
   * 不进 masked：面板底下「这些列设置了遮罩」不把它们算进去
   */
  mask_unverified?: string
  /**
   * via 为 metric、转抄另一张口径卡的指标（nodes.<卡>.metrics[i].value）时，运行时跨卡往下追的出处：那个指标的 id、那张卡的
   * 指标集工件、转抄了几层、那个指标的输入是不是都追到了原始数据（服务端 engine/lineage.metric_copy）。老运行、别的输入没有
   */
  upstream?: EvidenceUpstreamRef | null
  /**
   * 子工作流（docs/design/orchestration-round/00-总方案.md §2）：读子工作流出口的字段、接上子图里那一格的出处时，经过的子工作流
   * 节点和出口字段（subgraph、field）；node_id 落在子工作流里（「子工作流节点 id/…」）时证据接口另给图上的名字
   * （node_label、subgraph_label：「子工作流 › 节点」），画布、记录页认不出这个 id 时照它写。via 为 subgraph_input：停在
   * 子工作流的入口（子图的运行输入不是父图的原始数据）
   */
  subgraph?: string
  node_label?: string
  subgraph_label?: string
}

/** 转抄的输入跨卡往下追的出处（EvidenceInput.upstream） */
export interface EvidenceUpstreamRef {
  metric?: string
  artifact?: string | null
  depth?: number
  traced?: boolean
}

// ---------------------------------------------------------------------------
// 期 4：证据接口里的核查状态（api/evidence.py）。是核查，不是证据：没核查过的指标、句子不带这些键。形状照服务端的
// 真实输出逐字核对过，前端认的时候仍按「认不出就不画」写（run/EvidencePanel.tsx 的 dualCheckOf / challengeOf）
// ---------------------------------------------------------------------------

/** 双盲复算跳过一个指标的原因（_not_checked）：关闭了；这个指标没有来自查询的输入；不在复算范围内（或值不是数） */
export type EvidenceDualCheckSkip = 'disabled' | 'no_inputs' | 'not_targeted'

/**
 * 指标步骤上的双盲复算状态（_dual_check_of / _dual_status）。没有双盲复算复核过这份指标集时指标步骤没有 dual_check。
 * 产出读不出来、退回台账条目时只有 status、node_id 和 choice
 */
export interface EvidenceDualCheck {
  /** agreed 与盲算一致；disagreed 不一致；unchecked 未能完成（盲算触顶、出错，或输入没法单独复算）；skipped 没复算这个指标 */
  status: 'agreed' | 'disagreed' | 'unchecked' | 'skipped'
  /** 双盲复算节点 */
  node_id: string
  /** 只有 disagreed：审核人的选择（a 采用原流程、b 采用盲算、neither 两边都不对）。没选的没有 */
  choice?: DualCheckChoice
  /** 只有 agreed / disagreed：盲算值，服务端按口径卡的格式渲染好的 */
  blind_rendered?: string
  /** unchecked：给人看的原因；skipped：EvidenceDualCheckSkip 里的代码（界面认不出的代码不显示） */
  reason?: string
}

/**
 * 一份报告里被反方审过的句子 → 判断（证据图 reports[].challenge_verdicts，_challenge_verdicts）。只有被审过的句子；判断和
 * 点开这一句时片段接口答的 unit.challenge.verdict 一字不差。正文据此挂句末标记，证据面板在片段接口答回来之前先按它写判断
 */
export type EvidenceChallengeVerdicts = Record<string, ChallengeVerdict | (string & {})>

/** 句子上的反方审稿判断（_challenge_of）：片段接口答的 unit.challenge。没被反方审过的句子没有这个键 */
export interface EvidenceUnitChallenge {
  /** 这一句的判断（反方模型给的，不是系统核对） */
  verdict: ChallengeVerdict | (string & {})
  /** 反方审稿节点（几个都审了这一句时取最后跑完的那个） */
  node_id: string
  /** 审稿记录（challenge_artifact）。服务端保存失败时为 null：判断照样有，理由和查询看不了 */
  artifact: string | null
  /** 实际生效的处置：degrade 被推翻的计入出具缺口；flag 只标注；fail 节点报错、运行停了 */
  on_refuted: 'degrade' | 'flag' | 'fail'
  /** 报告撰写节点的名字（画布上的标题，取不到时是节点 id） */
  report_label: string
  /** 这次审稿四种判断各几句：整份报告的（节点产出里的那一份），不是这一句的 */
  counts: ChallengeCounts
  /** 没能反查的句数（都记为无法判断，不会多于 counts.inconclusive）。节点产出里有才带 */
  unreviewed?: number
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
/** 合并查询的一个输入（证据面板的 merge.inputs） */
export interface EvidenceMergeInput {
  /** 合并 SQL 里的表名 */
  alias: string
  node_id?: string | null
  /** 节点在画布上的名字 */
  label?: string | null
  /** 这个输入在报告目录里的编号（Q1）；不在这份报告的目录里时为空 */
  query?: string | null
  rows?: number | null
  /** 数据源名；输入本身是合并结果时是「合并查询」 */
  source?: string | null
  artifact?: string | null
  sealed?: boolean
  /** 这个输入查询对照数据目录查出的问题（形状同查询步骤的 checks）。没查出问题、快照读不出来时没有这个键 */
  checks?: SqlCheckItem[]
}

/**
 * 出具声明（output._issuance、issuance 事件）里的 sql_checks：没通过 SQL 检查的查询，一条一个。报告页、出具横幅据此
 * 在显眼处说「因为 SQL 检查没通过而降档」。gap 是 gaps 里对应的那一句（单列之后不在其余缺口里重复）
 */
export interface IssuanceSqlCheck {
  /** 「查询「取数」（Q1）」；给不出时没有 */
  query?: string
  node_id?: string
  /** 每条问题那半句 */
  problems?: string[]
  /** 受影响的引用：「指标「订单金额」」「Q1 第 1 行「gmv」」 */
  refs?: string[]
  gap?: string
}

/** 查询用到的一张表、查询当时它的数据目录版本 */
export interface EvidenceCatalogVersion {
  /** 表结构里的表名 */
  table: string
  /** 目录里的中文名 */
  label?: string
  version?: number | null
}

/** 被引用的一格追到了哪个输入的哪一格；追不到时 input 为空，note 说明只有表级来历 */
export interface EvidenceMergeTrace {
  /** 合并结果里的 [行, 列]，行号从 0 数 */
  cell: [number, string]
  input: string | null
  query: string | null
  row: number | null
  column: string | null
  note?: string
}

export interface EvidenceMerge {
  sql?: string | null
  inputs: EvidenceMergeInput[]
  warnings: { code?: string | null; message: string }[]
  traced: EvidenceMergeTrace[]
}

/** 指标的一个输入追不到原始数据：出处追到哪个节点为止。认不出是哪个节点时 node_id、label 为 null */
export interface EvidenceUntraced {
  path: string
  node_id: string | null
  /** 封存的图里那个节点的名字 */
  label: string | null
  /** 节点类型（transform、code、llm……） */
  type: string | null
}

export interface EvidenceStep extends Omit<EvidenceInput, 'status'> {
  step: string
  /**
   * upstream：转抄另一张口径卡的指标时跨卡接上的那一步（api/evidence._upstream_steps）。of 是接在哪个输入后面（输入的路径），
   * label 是那张卡在图里的名字，traced 是那个指标的输入都追到了原始数据，depth 是转抄了几层
   */
  of?: string
  label?: string | null
  traced?: boolean
  depth?: number
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
  /** metric：表达式把截断的查询结果当成整组用了（计数、求和……），值只算到了取回的那部分 */
  incomplete?: boolean
  /** metric：不完整的原因（人话，「基于被截断的查询结果计算（只取回了前 1000 行），结果不完整」） */
  incomplete_reason?: string
  /** metric：所依据的查询没通过 SQL 检查（有错误级的问题），值照算，出具已按缺口降档 */
  sql_check_failed?: boolean
  /** metric：没通过的原因（人话，「所依据的查询未通过 SQL 检查（…），结果不可靠」） */
  sql_check_reason?: string
  /** metric（期 4）：这个指标的双盲复算状态。没有双盲复算复核过这份指标集时没有这个键 */
  dual_check?: EvidenceDualCheck
  /**
   * metric：输入里追不到原始数据（查询快照里的格、运行输入）的，空数组是都追到了；卡上没有输入、老服务端没有这个键。
   * 和「有出处」（口径卡确定性复算一致）是两件事，面板分开说（服务端 engine/lineage.py）
   */
  untraced?: EvidenceUntraced[]
  /** query：这次查询对照数据目录查出的问题。没查出问题时没有这个键 */
  checks?: SqlCheckItem[]
  /** query：查询当时冻结的表结构快照（工件 id）。合并查询、老快照没有 */
  schema_artifact?: string
  /** query：这条查询用到的表在表结构快照里冻结的数据目录版本（SQL 检查对照的那一版）。没有目录时没有这个键 */
  catalog?: EvidenceCatalogVersion[]
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
  /**
   * 期 4：可以请求推断来源（GET …/segments/{sid}/provenance）。只有带文档标记的报告、单元格片段的查询步骤、
   * 查询快照来自上传的表格时才有，且恒为 true；其余一个键都不加。前端只在 seg.cite?.kind === 'cell' 且它为 true 时请求
   */
  provenance?: boolean
  /** 合并查询的结果（快照的 source 是「合并查询」）：合并了哪几个输入、合并 SQL、执行时的警告、被引用的格追到哪 */
  merge?: EvidenceMerge
  /** 这一步是哪次合并查询的输入（合并结果在目录里的编号）：排在那个合并步骤后面，高亮的是追到的格 */
  merged_into?: string
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
          on_demand?: EvidenceOnDemand
          /** 期 4：反方审稿对这一句的判断。没审到这一句的没有这个键 */
          challenge?: EvidenceUnitChallenge }
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
  /**
   * 口径账本（next-round 02 做法 3）：这个数的口径标签和比对的来历。封存后比对，没比对过的运行、不是数的片段没有这个键
   * （老运行的返回一字不差）。形状见 typesLedger.ts
   */
  ledger?: import('./typesLedger').SegmentLedger
  /** 指标接口（/evidence/metrics/{node}/{metric}）给的展示值：遮罩的写「已遮罩」 */
  rendered?: string
}

// -------------------------------------------------------------------------
// 期 4：推断的来源（GET /runs/{id}/evidence/segments/{sid}/provenance?report=…）
// 契约照 backend/app/data/provenance_types.py（asdict 之后的 JSON，P4-SPEC 2.8.1）。这是期 4 新加的接口，
// 答复里每个键都在（值可以是 null），所以这里不写成可选；老服务端没有这个接口时回 404 且没有 code，三节都不画。
// -------------------------------------------------------------------------

/** inferred 给出格子；table_only 只给表级来历（version 非空）；none 不画，或只画一句原因 / 红色提示 */
export type ProvenanceStatus = 'inferred' | 'table_only' | 'none'

/** 不下钻的原因（provenance_types.ReasonCode）。界面不按它拼字，原样显示服务端给的 reason.text */
export type ProvenanceReasonCode =
  | 'legacy_doc' | 'not_cell' | 'not_sealed' | 'not_upload' | 'simple_upload' | 'manifest_unreadable'
  | 'chain_mismatch' | 'expression' | 'alias' | 'multi_table' | 'unparsed' | 'no_pk' | 'pk_missing' | 'masked'
  | 'null_value' | 'null_pk' | 'snapshot_gone' | 'db_tampered' | 'recheck_missing' | 'recheck_multiple'
  | 'recheck_mismatch' | 'no_lineage' | 'merge_no_lineage'

/** 标红的提示：出现时 reason.code 与它相同，界面只画提示 */
export type ProvenanceAlertCode = 'db_tampered' | 'chain_mismatch' | 'manifest_unreadable'

/** 附带的格：日期表头格、行标签、分段标题、列表头、合计标签 */
export type ProvenanceFromRole = 'axis_header' | 'row_label' | 'section_title' | 'col_header' | 'total_label'

/** 本期结论（照搬清单里 CheckResult.status） */
export type ProvenancePartStatus = 'passed' | 'mismatch' | 'unverifiable' | 'info'
/** 这一行的关系核对结果（只给关系核对） */
export type ProvenanceRowStatus = 'passed' | 'mismatch' | 'unverifiable'
/** 这一格的合计核对结果（只给 K、G、T） */
export type ProvenanceCellStatus = 'ok' | 'unverifiable' | 'unknown' | 'not_formula'

export interface ProvenanceReportRef {
  node_id: string
  doc_artifact: string
}

/** 片段本身是单元格引用就给（与 status 无关），不是时为 null */
export interface ProvenanceCellRef {
  /** 目录里的查询编号（Q3） */
  alias: string
  row: number
  column: string
  /** 查询快照的工件 id */
  artifact: string | null
}

export interface ProvenanceReason {
  code: ProvenanceReasonCode
  /** 细分，给测试和日志用，界面不显示 */
  detail: string
  /** 界面原样显示 */
  text: string
}

export interface ProvenanceAlert {
  code: ProvenanceAlertCode
  text: string
}

export interface ProvenanceTableRef {
  name: string
  kind: 'data' | 'reported_total'
}

export interface ProvenancePeriod {
  start: string
  end: string
  source: 'cells' | 'human'
  /** 解析出统计期的格子，带工作表名（「客流汇总!B2」） */
  cells: string[]
  /** 人工录入时的署名（未认证） */
  signed_by: string | null
}

/** 一条接受理由（导入清单里的，内容寻址，是当时的事实） */
export interface ProvenanceAcceptance {
  check_id: string
  title: string | null
  kind: 'override' | 'waiver'
  reason: string
  signed_by: string | null
  at: string | null
}

/** 导入记录上的当前状态（清除原件、作废接受） */
export interface ProvenanceStateNote {
  at: string | null
  signed_by: string | null
  reason: string | null
}

/** 数据版本里的一期（按统计期升序）。raw_state、purged、revoked 是当前状态，界面标「当前状态」 */
export interface ProvenancePart {
  import_id: string
  seq: number
  /** 导入清单的工件 id：「查看导入清单」按它经 /api/artifacts/{id} 取 */
  manifest: string
  /** 没有统计期时 null（「统计期未记录」） */
  period: ProvenancePeriod | null
  file_name: string | null
  raw_sha256: string | null
  /** 区域（「客流汇总!B4:AG30」，多张表用「、」连）；取不到为 null */
  region: string | null
  /** 排除的行数之和；期 3 之前的回执没有记录时为 null */
  excluded_rows: number | null
  recipe_sha256: string | null
  /** 配方第几版（当前状态）；取不到为 null */
  recipe_seq: number | null
  /** 导入时间（清单的 created_at） */
  committed_at: string | null
  /** 署名（未认证） */
  signed_by: string | null
  acceptances: ProvenanceAcceptance[]
  raw_state: 'kept' | 'purged' | 'absent' | null
  purged: ProvenanceStateNote | null
  revoked: ProvenanceStateNote | null
  /** 这一格所在的一期：inferred 时恰好一项为 true */
  has_row: boolean
}

/** 数据版本（表级来历），来自封存链上的清单，不加「推断」标注 */
export interface ProvenanceVersion {
  source: string
  snapshot_id: string
  mode: 'replace' | 'accumulate'
  union: boolean
  tables: ProvenanceTableRef[]
  /** false：数据源设有遮罩，不出「查看导入清单」 */
  manifest_view: boolean
  parts: ProvenancePart[]
}

export interface ProvenanceFromCell {
  role: ProvenanceFromRole
  /** 这一格给出的是哪一列的值；宽表指标列的行标签（指标名取自哪一格）为 null */
  column: string | null
  sheet: string
  /** 不带工作表名的坐标（「G4」） */
  cell: string
  /** 格子原文；拿不到为 null */
  text: string | null
  /** 只给分段标题：配方中的定位文字 */
  locate_title: string | null
}

export interface ProvenanceYear {
  source: 'period' | 'human'
  cells: string[]
  signed_by: string | null
  /** 这一块的日期表头有的是日期格、有的是文本，无法确定这一格属于哪一种 */
  mixed: boolean
}

/**
 * 这一行标签列（交叉表的维度、合计表的合计项）的原文与数据库里的规范写法不同时给出。被引用的是值列也给：
 * 合计表那一格是「18-22 时合计」→「18-22时合计」，全角标签那一期的值格是「８－９」→「8-9」
 */
export interface ProvenanceCanonical {
  raw: string
  canonical: string
}

export interface ProvenanceRecheck {
  /** 占位符一律是 ?，按主键的顺序与 params 一一对应 */
  sql: string
  params: unknown[]
  ok: boolean
}

/** 推断出的格子（只在 inferred 时有） */
export interface ProvenanceCellSource {
  table: string
  column: string
  column_role: 'axis' | 'dim' | 'derive' | 'const' | 'measure' | 'value' | 'text' | (string & {})
  kind: 'data' | 'reported_total'
  pk: Record<string, unknown>
  /** 快照库的 rowid（累积时是合并后的行号） */
  rowid: number
  part_seq: number
  part_rowid: number
  sheet: string
  /** 不带工作表名的坐标（「G5」） */
  cell: string
  header: string | null
  unit: string | null
  from: ProvenanceFromCell[]
  year: ProvenanceYear | null
  canonical: ProvenanceCanonical | null
  raw_purged: boolean
  /** 列表这一块按合并单元格的左上格填充 */
  merged_fill: boolean
  recheck: ProvenanceRecheck
}

/** 相关核对的一条（只在 inferred 时有） */
export interface ProvenanceCheck {
  id: string
  kind: string
  title: string
  part_status: ProvenancePartStatus
  row_status: ProvenanceRowStatus | null
  cell_status: ProvenanceCellStatus | null
  acceptance: ProvenanceAcceptance | null
  /** K 无法核对时的原因摘要（可能带数字，系统呈现） */
  detail: string | null
}

/** GET /runs/{id}/evidence/segments/{sid}/provenance：推断的来源 */
export interface EvidenceProvenance {
  schema: 'agentlab.provenance/1' | (string & {})
  report: ProvenanceReportRef
  segment: string
  cell: ProvenanceCellRef | null
  status: ProvenanceStatus
  /** status 不是 inferred 时必有 */
  reason: ProvenanceReason | null
  /** 标红；有它时不再画 reason.text */
  alert: ProvenanceAlert | null
  /** 运行已封存且封存核对通过；false 时数据版本一节加一句未封存 */
  sealed: boolean
  /** status 为 none 时为 null */
  version: ProvenanceVersion | null
  cell_source: ProvenanceCellSource | null
  checks: ProvenanceCheck[]
  /** 被引用的格在合并查询的结果里时，经过的每一次合并；别的查询为空 */
  merge: ProvenanceMergeHop[]
}

/** 经过的一次合并查询：合并结果里被引用的格追到了哪个输入的哪一格；追不到的那一跳后四项为 null */
export interface ProvenanceMergeHop {
  /** 合并结果在目录里的编号（Q3） */
  alias: string
  node_id: string | null
  /** 追到的输入别名（合并 SQL 里的表名） */
  input: string | null
  /** 那个输入在目录里的编号（Q1） */
  query: string | null
  row: number | null
  column: string | null
}

// ===========================================================================
// 业务数据目录（/api/datasources/{id}/catalog，后端 app/data/catalog.py）。
// 每张表一份业务说明：中文名、粒度、业务主键、列的含义和度量类型、表与表的关联关系……每一项都注明来源和状态。
// 目录只记数据事实，不放计算公式（公式在口径卡里）。
// ===========================================================================

/** 项的来源：数据库注释、外键约束、命名推断、数据剖析、模型起草、人工填写 */
export type CatalogSource = 'comment' | 'fk' | 'name' | 'profile' | 'llm' | 'human'
/** 项的状态：推断、已验证（外键约束、数据剖析）、已确认（人工）、已驳回（人工） */
export type CatalogStatus = 'proposed' | 'verified' | 'confirmed' | 'rejected'
/** 表类型：明细（事实）、维度、快照、日志、配置 */
export type CatalogTableKind = 'fact' | 'dimension' | 'snapshot' | 'log' | 'config'
/** 列的度量类型：可累加（流量）、存量、比率、标识、状态、属性 */
export type CatalogMeasure = 'flow' | 'stock' | 'ratio' | 'identifier' | 'status' | 'attribute'
/** 关系的基数（从本表看过去） */
export type CatalogCardinality = 'many_to_one' | 'one_to_one' | 'one_to_many'

/** 目录里的一项：值连同来源、状态 */
export interface CatalogItem<T = unknown> {
  value: T
  source: CatalogSource
  status: CatalogStatus
  /** 备注：机器写的说明（模型起草的依据、数据剖析的测量、双盲复算的出处……） */
  note?: string
  /**
   * 确认时的依据：保存修改提案（表助手、助手流里「建议更新数据目录」）时使用者确认的依据，和备注分开存。值改了换成那一次的，
   * 撤销审阅回到推断时去掉；只由服务端写，整份提交时带上也不作数
   */
  reason?: string
  updated_at?: string
}

/** 业务日期：按哪一列算，规则和时区写成文字 */
export interface CatalogBusinessDate {
  column: string
  rule?: string
  timezone?: string
}

/** 一列的目录 */
export interface CatalogColumnNotes {
  label?: CatalogItem<string>
  meaning?: CatalogItem<string>
  unit?: CatalogItem<string>
  measure?: CatalogItem<CatalogMeasure>
  /** 码值 → 含义 */
  codes?: CatalogItem<Record<string, string>>
}

/** 一条关联关系。编号由两端的表和列算出来，改了指向就是另一条 */
export interface CatalogRelation {
  id: string
  columns: string[]
  to_table: string
  to_columns: string[]
  cardinality: CatalogCardinality | null
  /** 0 到 1：本表这几列的值在被指向表里找得到的比例（数据剖析给出） */
  coverage: number | null
  source: CatalogSource
  status: CatalogStatus
  note?: string
  /** 确认时的依据（同 CatalogItem.reason） */
  reason?: string
  updated_at?: string
  /**
   * 基数是数据剖析用数据核实的（子表一侧数过是否唯一），只由剖析写。外键、命名推断的基数没有这个键。
   * 一对多关联后重复计算的检查只在它为真、或者关系人工确认过时报「错误」，否则最多是「提醒」
   */
  cardinality_checked?: boolean
}

/** 一张表的目录（notes）。字段固定，服务端不认识的一律拒收 */
export interface CatalogNotes {
  label?: CatalogItem<string>
  description?: CatalogItem<string>
  grain?: CatalogItem<string>
  keys?: CatalogItem<string[]>
  kind?: CatalogItem<CatalogTableKind>
  business_date?: CatalogItem<CatalogBusinessDate>
  /** SQL 条件片段 */
  valid_filter?: CatalogItem<string>
  dedup?: CatalogItem<string>
  columns?: Record<string, CatalogColumnNotes>
  relations?: CatalogRelation[]
}

/** 各状态的项数（表级项、列级项、关系都算），四种状态都有键 */
export type CatalogCounts = Record<CatalogStatus, number>

/** 表清单的一行。表结构里的每张表各一行（没有目录的也列）；目录还在、表已不在的 in_schema 为 false，排在最后 */
export interface CatalogTableRow {
  table_name: string
  qualified: string
  is_view: boolean
  in_schema: boolean
  /** 中文名（被驳回的不算） */
  label: string | null
  label_status: CatalogStatus | null
  kind: CatalogTableKind | null
  counts: CatalogCounts
  /** 没被驳回的关联关系条数 */
  relations: number
  /** 被运行查询过的次数（同一次运行里结果相同的重复查询只算一次） */
  usage: number
  /** 这张表还没有目录时为 0 */
  version: number
  updated_at: string | null
  updated_by: string | null
  /** 这张表最近一次起草任务里的那一项（任务还在进行就是进行中的状态）；没起草过为 null。老服务端没有这个键 */
  last_draft?: CatalogLastDraft | null
  /** 这张表最近一次剖析（任务还在进行就是进行中的状态）；没剖析过为 null。老服务端没有这个键 */
  last_profile?: CatalogLastProfile | null
  /** 按「需要剖析」的口径这张表要不要剖析（表结构里、不是视图、有可剖析的项，且满足 profile_due_reason 之一） */
  profile_due?: boolean
  /**
   * 为什么需要剖析：never 从没剖析过；unfinished 最近一次剖析没做完（部分完成、失败、已停止）；new_relations 目录里有待核实的
   * 关系不在最近一次剖析的检查项里；new_checks 最近一次剖析之后多出了码值、日期检查（某一列被记成了状态类、日期列变了）。
   * 不需要时为 null
   */
  profile_due_reason?: CatalogProfileDueReason | null
  /** 按当前目录和表结构这张表要做几项检查；0 表示没有可剖析的项（视图为 0） */
  profile_checks?: number
}

/** GET /datasources/{id}/catalog */
export interface CatalogList {
  /** 使用次数多的在前 */
  tables: CatalogTableRow[]
  /** 导入表格的源：表和列的说明由系统按核对结果生成，只读 */
  system_notes: boolean
  /** 没有表结构时的原因（「尚未探查结构」「结构探查失败：…」）；有表结构时为 null */
  schema_note: string | null
  /** 探查结构截断了（每个数据源最多取 200 张表）：没探查到的表不在清单里，助手也看不到。老后端没有这一项 */
  schema_truncated?: boolean
  /** 数据库里一共几张表（含视图）；没截断时等于探查到的张数 */
  schema_total?: number
  /** 这个数据源的起草要求（使用者补充给模型的背景和要求）；没填为 null。老服务端没有这一项 */
  guidance?: string | null
  /** 排队或进行中的起草任务（页面据此显示进度）；没有为 null。老服务端没有这一项 */
  active_job?: CatalogActiveDraftJob | null
  /** 排队或进行中的剖析任务（页面据此显示进度）；没有为 null。老服务端没有这一项 */
  active_profile_job?: CatalogActiveProfileJob | null
}

/** 表结构里的一列 */
export interface CatalogStructureColumn {
  name: string
  type: string
  pk: boolean
  not_null: boolean
  /** 数据库注释；导入表格的源是系统按核对结果生成的说明 */
  comment: string | null
}

export interface CatalogForeignKey {
  columns: string[]
  to_table: string
  to_columns: string[]
}

export interface CatalogStructure {
  qualified: string | null
  is_view: boolean
  /** 表注释；导入表格的源是系统按核对结果生成的说明 */
  comment: string | null
  columns: CatalogStructureColumn[]
  primary_key: string[]
  /** 升级前探查的缓存里没有：null 表示不知道，空列表表示读过、没有 */
  foreign_keys: CatalogForeignKey[] | null
  unique: string[][] | null
}

/** GET /datasources/{id}/catalog/{table}；PUT、审阅也返回这个形状 */
export interface CatalogDetail {
  table_name: string
  in_schema: boolean
  notes: CatalogNotes
  version: number
  updated_at: string | null
  updated_by: string | null
  /** 表结构里已经没有这张表时为 null */
  structure: CatalogStructure | null
  system_notes: boolean
  usage: number
  /** 这张表最近一次剖析的完整结果（含检查清单、发现、发过的查询；任务还在进行就是进行中的状态）；没剖析过为 null */
  last_profile?: CatalogProfileTaskDetail | null
}

/** 单项审阅：确认、驳回、撤销审阅（回到来源的初始状态；人工填写的项直接删掉） */
export type CatalogReviewAction = 'confirm' | 'reject' | 'reset'

// ---------------------------------------------------------------------------
// 起草任务（/api/datasources/{id}/catalog/draft-jobs）：起草在服务端后台进行，逐表起草、每张表一结束就并入目录。
// 关掉对话框不停任务；服务重启时还没结束的任务记 interrupted，不自动续跑，由人点「重试」把没写全的表重新起草一遍
// （重试不跳过已写进目录的段，整张表重新分段起草；已写进目录的内容不会丢）。
// ---------------------------------------------------------------------------

/**
 * 起草任务的状态：排队、进行中、完成、部分完成（有失败、部分完成或停止的表）、失败（任务整体出错）、已停止（用户停止）、
 * 服务重启中断
 */
export type CatalogDraftJobStatus = 'queued' | 'running' | 'succeeded' | 'partial' | 'failed' | 'cancelled' | 'interrupted'

/**
 * 任务里一张表的状态。ok：写入成功（或没有东西要写），用模型时各段都写出来了；partial：用模型，有的段没写出来；
 * failed：整张表没起草（error），或用模型时一段都没写出来（model_error），按注释、外键、命名起草的部分照常写入；
 * skipped：表结构里没有这张表；cancelled：停止或服务重启时还没开始、没写完
 */
export type CatalogDraftTaskStatus = 'pending' | 'running' | 'ok' | 'partial' | 'failed' | 'skipped' | 'cancelled'

/** 起草任务里的一张表（按起草顺序） */
export interface CatalogDraftTask {
  table_name: string
  status: CatalogDraftTaskStatus
  /** 列数 */
  columns: number
  /** 模型分几段起草（超过 40 列的宽表拆成表级一段加列分段）；不用模型时为 null */
  parts_total: number | null
  parts_done: number
  /**
   * 已经写进目录的段数：每写完一段就并入目录，停止、服务重启时已写进的段保留。老服务端没有这个键
   */
  parts_written?: number
  /**
   * 这张表实际分成的块数和已结束的块数：额度用完、超时后段被缩小、拆半，块会比段多。不用模型时为 null；
   * 老服务端没有这两个键
   */
  chunks_total?: number | null
  chunks_done?: number | null
  added: number
  updated: number
  removed: number
  version: number
  /**
   * 模型给了这张表几项可用内容（并入目录之前）。没用模型、模型这部分失败时为 null。
   * 大于 0 而新增、更新、删除都是 0，才是「模型给了内容、只是和现有目录一致」
   */
  model_items: number | null
  /**
   * 模型拿不准、留空的列数：模型列出了（或这一段请它写了），中文名和含义却都没给（给了却都没通过校验的也算）。
   * 表的状态不因此变（照旧可以是已完成）。老服务端没有这个键
   */
  blank_columns?: number
  /** 留空的列里的前几个（最多 5 个，列名照表结构原样）。老服务端没有这个键 */
  blank_sample?: string[]
  /**
   * 这张表起草时记下的几句话（服务端整句中文，不算失败），比如模型提议的有效记录条件没有收下的原因。最多 10 句；
   * 老服务端没有这个键
   */
  log?: string[]
  /** 整张表没起草的原因：表结构里没有、写入一直冲突、服务重启中断 */
  error: string | null
  /** 模型那部分没写出来的原因和办法（第几段、哪些列、为什么） */
  model_error: string | null
  started_at: string | null
  finished_at: string | null
}

/** 起草任务的模型用量 */
export interface CatalogDraftUsage {
  /** 调用次数（没写出来的、超时的也算） */
  calls: number
  /** 超时的调用不计 token（照实记 0：服务方可能已经生成了内容） */
  input_tokens: number
  output_tokens: number
  /** 超时的调用次数。老服务端没有这个键 */
  timeouts?: number
  /** 没写出来的调用次数：额度用完、只给空壳、超时、出错。老服务端没有这个键 */
  failed_calls?: number
}

/** 一次起草任务 */
export interface CatalogDraftJob {
  id: string
  source_id: string
  status: CatalogDraftJobStatus
  use_model: boolean
  /** 实际用的模型 id；不用模型、还没开始时为 null */
  model: string | null
  /** 这次用到的起草要求（数据源级那份的快照） */
  guidance: string | null
  /** 这次的补充说明 */
  note: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  /** 谁建的（X-Actor） */
  created_by: string | null
  /** 重试建的任务记着原任务的 id，别的为 null。接口约定里没有这一项，服务端多给的；界面不用，老服务端没有这个键 */
  retry_of?: string | null
  /** 表数 */
  total: number
  /** 已结束的表数 */
  done: number
  counts: Record<CatalogDraftTaskStatus, number>
  added: number
  updated: number
  removed: number
  /** 各表模型拿不准、留空的列数合计。老服务端没有这个键 */
  blank_columns?: number
  /** 模型调用的用量；拿不到用量时照实是 0 */
  usage: CatalogDraftUsage
  /**
   * 当前每段最多几列：一次起草里额度用完或超时累计 2 次后，之后新开始的段减半（40 → 20 → 10，最低 10）。
   * 不用模型时可能为 null；老服务端没有这个键
   */
  part_columns?: number | null
  /**
   * 调用接连没写出来时的提醒（一句中文：最近几次里几次没写出来、原因，建议换起草模型、缩小范围或停止）；
   * 好转后服务端清掉，为 null。老服务端没有这个键
   */
  notice?: string | null
  /** 任务整体出错的原因 */
  error: string | null
  /**
   * 排在同一个数据源的剖析任务后面（起草和剖析同源串行，按建立顺序）：剖析结束后自动开始。不在等时为 null；
   * 老服务端没有这个键
   */
  waiting_for?: 'profile' | 'introspect' | null
  tasks: CatalogDraftTask[]
}

/** 目录清单顶上的进行中任务（GET /datasources/{id}/catalog 的 active_job） */
export interface CatalogActiveDraftJob {
  id: string
  status: CatalogDraftJobStatus
  total: number
  done: number
  /** 排在剖析任务后面（同 CatalogDraftJob.waiting_for） */
  waiting_for?: 'profile' | 'introspect' | null
}

/** 清单一行上这张表最近一次起草的结果 */
export interface CatalogLastDraft {
  status: CatalogDraftTaskStatus
  job_id: string
  finished_at: string | null
  error: string | null
  model_error: string | null
  /** 那次起草模型拿不准、留空的列数（同 CatalogDraftTask.blank_columns）。老服务端没有这个键 */
  blank_columns?: number
  /**
   * 最近一次这张表结束于完成、部分完成的起草记下的留空列数（没有这样的起草为 null）。最近一次是停止、服务重启中断、
   * 又没有新写入时，界面用它接着提示留空的列。老服务端没有这个键
   */
  blank_columns_from?: number | null
  /** 那次起草已写进目录的段数（同 CatalogDraftTask.parts_written）。接口约定里没有这一项，服务端给了才用 */
  parts_written?: number
}

/**
 * 目录修改提案的一项（服务端 catalog.PatchChange）。助手转出的提案和预览接口都是这个形状。
 * path 是审阅接口的写法；新增关联关系已规范成 relations.<编号>
 */
export interface CatalogPatchChange {
  path: string
  /** 当前目录里的值；没有（或被驳回）为 null */
  before: unknown
  before_status: CatalogStatus | null
  /** 保存后的值（码值是补充后的完整对照） */
  after: unknown
  /** 交回保存的原样取值（码值只有补充的那几个） */
  value: unknown
  reason: string
  /** change 值有变化；confirm 值相同、还不是已确认（保存即确认）；same 已经是这个值且已确认 */
  state: 'change' | 'confirm' | 'same'
  /** 服务端对这一项的说明（目前只有：新增的关联关系没写基数、又推算不出来）。没有时不给 */
  note?: string
}

/** 保存、预览提案时交回的一项：路径、原样取值、理由 */
export interface CatalogPatchSubmit {
  path: string
  value: unknown
  reason?: string
}

/** POST /datasources/{id}/catalog/{table}/patch/preview：对着当前目录重算改前、改后（只读） */
export interface CatalogPatchPreview {
  table_name: string
  version: number
  changes: CatalogPatchChange[]
  /** 不合法的项的说明（目录刚被改过、列已删除……） */
  problems: string[]
}

/** 影响面里模板的一个节点（服务端 catalog_impact.template_refs） */
export interface CatalogImpactNode {
  node_id: string
  label: string
  type: string
  /** direct：SQL 里写着这张表；possible：Agent 运行时自己写 SQL，可能用到 */
  impact: 'direct' | 'possible'
  /** 合并查询：经由哪些输入（别名 → 节点） */
  via?: { node_id: string; label: string; alias: string }[]
  /** 协作节点：绑定了查询工具的成员 */
  member?: string
}

/** 引用这张表的一个已发布或受管模板（按它当前的已发布版本算） */
export interface CatalogImpactTemplate {
  workflow_id: string
  name: string
  /** 当前的已发布版本 */
  version: number
  level: 'published' | 'governed'
  impact: 'direct' | 'possible'
  nodes: CatalogImpactNode[]
}

// ---------------------------------------------------------------------------
// 审阅队列（/api/datasources/{id}/catalog/review-queue、review/batch、review-progress、{table}/usage，后端
// data/catalog_review.py）：跨表列出推断项，按表的使用次数、这一项对数字的影响和来源排好，可以成批确认、驳回。
// ---------------------------------------------------------------------------

/** 队列的筛选：全部、影响数字的项（有效记录条件、去重、业务日期、关联关系、业务主键、度量类型）、名称和含义 */
export type CatalogReviewKind = 'all' | 'facts' | 'names'

/** 队列里的一项（只列推断状态的项） */
export interface CatalogReviewItem {
  table_name: string
  /** 审阅接口的路径写法 */
  path: string
  /** 字段名（valid_filter、label……；关联关系是 relation） */
  field: string
  /**
   * 字段的界面叫法（服务端写好的「中文名」「码值」「有效记录条件」「关联关系」），不含是哪一列：界面上的位置按 path
   * 说（「列 status 的码值」，review.ts 的 reviewWhere）
   */
  field_label: string
  /** 列级的项是哪一列；表级的项、关联关系为 null。接口约定里没有写明，服务端给了才用 */
  column?: string | null
  value: unknown
  /** 值写成一行字（服务端写好的） */
  display: string
  source: CatalogSource
  status: CatalogStatus
  note: string | null
  /** 越大越靠前：表的使用次数权重 × 风险权重 × 来源权重 */
  priority: number
  /** 为什么排在这里（中文，服务端写好的） */
  reasons: string[]
  /** 所在表被运行用过的次数 */
  table_usage: number
  /** 所在表的目录版本：成批审阅时逐表带上 */
  version: number
}

/** GET /catalog/review-queue */
export interface CatalogReviewQueue {
  total: number
  items: CatalogReviewItem[]
}

/** POST /catalog/review/batch 的一项 */
export interface CatalogReviewBatchItem {
  table_name: string
  path: string
  action: CatalogReviewAction
  if_version: number
}

/** 成批审阅逐条的结果：一张表冲突不影响别的表；成功的写明这张表写入后的版本 */
export interface CatalogReviewBatchResult {
  table_name: string
  path: string
  ok: boolean
  version: number | null
  error: string | null
  code: string | null
}

/** 用过这张表的一次运行（新的在前） */
export interface CatalogUsageRun {
  run_id: string
  workflow_id: string | null
  workflow_name: string | null
  run_class: 'formal' | 'exploratory' | null
  status: string
  started_at: string | null
}

/** GET /catalog/{table}/usage */
export interface CatalogTableUsage {
  total: number
  runs: CatalogUsageRun[]
}

/**
 * GET /catalog/review-progress：常用的前 top_n 张表、全部表、影响数字的项各审阅到哪儿。
 * 「常用表」只算被运行用过的：tables 可能比 top_n 少，没有运行时是 0。三行的进度都按「已确认 ÷（已确认 + 待确认）」算
 * （review.ts 的 confirmProgress）：已驳回的、有确证的（外键约束）不用再确认，不计入
 */
export interface CatalogReviewProgress {
  top_n: number
  /** items_proposed、items_verified：接口约定里没有写明，服务端给了才用（没有待确认数时只写已确认几项） */
  top: { tables: number; items_total: number; items_confirmed: number; items_proposed?: number; items_verified?: number }
  all: { items_total: number; items_confirmed: number; items_proposed: number; items_rejected: number; items_verified?: number }
  facts: { proposed: number; confirmed: number }
}

// ---------------------------------------------------------------------------
// 导出导入（/api/datasources/{id}/catalog/export、import/preview、import，后端 data/catalog_transfer.py）：
// 本机整理好的目录带到另一个实例。先预览（只读），确认后逐表写入。
// ---------------------------------------------------------------------------

/** 导出的文件：含全部状态的项 */
export interface CatalogExportFile {
  format: 'agentlab-catalog'
  version: 1
  exported_at: string
  source: { name: string; kind: string }
  guidance: string | null
  tables: Record<string, { notes: CatalogNotes; version: number }>
}

/** 一处冲突：这里已确认、已驳回的项和文件中的不一样，以这里为准 */
export interface CatalogImportConflict {
  path: string
  label: string
  file_value: unknown
  target_value: unknown
  why: string
}

/** 预览、导入报告里的一张表 */
export interface CatalogImportTable {
  /** 文件中的表名 */
  table_name: string
  /** 对上的这个数据源里的表名（名字不一样时才有意思）；对不上为 null */
  matched_as: string | null
  add: number
  update: number
  keep: number
  conflicts: CatalogImportConflict[]
  skipped: { path: string; why: string }[]
  /** 导入后这张表的目录版本（只有导入的报告有） */
  version?: number | null
  /** 这张表没导入的原因（逐表写入，一张表失败不影响别的）。接口约定里没有写明，服务端给了才显示 */
  error?: string | null
}

export interface CatalogImportTotals {
  tables: number
  add: number
  update: number
  keep: number
  conflicts: number
  skipped: number
}

/** POST /catalog/import/preview、/catalog/import */
export interface CatalogImportReport {
  tables: CatalogImportTable[]
  /** 文件中有、这个数据源里对不上的表 */
  unmatched_tables: string[]
  totals: CatalogImportTotals
  /** 起草要求：写入文件中的（这里还没有）、保留这里的、不导入 */
  guidance: { file: string | null; target: string | null; action: 'keep' | 'replace' | 'none' }
}

// ---------------------------------------------------------------------------
// 表助手（POST /api/datasources/{id}/catalog/{table}/assistant，后端 data/catalog_assistant.py）：对话完善一张表的
// 目录。回答是事件流，每个事件一行 data: <JSON>。对话不存服务端，每一轮都把前面的对话和还没保存的修改带上。
// ---------------------------------------------------------------------------

/** 提案里的一项：和助手流里的 catalog_patch 一样，多一个给人看的位置 */
export interface CatalogAssistantChange extends CatalogPatchChange {
  label?: string
}

/**
 * 事件流里的一个事件。服务端等模型时另发 SSE 注释行（「: keep-alive」，不是事件），读流时跳过。
 * query 的 row_count 是取回的行数；truncated（接口约定里没有写明，服务端给了才用）为真时后面还有行，取回的只是前一截
 */
export type CatalogAssistantEvent =
  | { type: 'text'; delta: string }
  | { type: 'query'; sql: string; columns: string[]; rows: unknown[][]; row_count: number; truncated?: boolean; error: string | null }
  | { type: 'proposal'; source_id: string; table: string; version: number; changes: CatalogAssistantChange[] }
  | { type: 'done'; usage: { input_tokens: number; output_tokens: number }; queries: number }
  | { type: 'error'; message: string; code?: string | null }

/** 交给表助手的一轮请求 */
export interface CatalogAssistantRequest {
  messages: { role: 'user' | 'assistant'; content: string }[]
  /** 界面上还没保存的修改：最多 200 项（服务端同一个上限，多了整个请求 422），多出来的只交最近的 200 项 */
  pending: CatalogPatchSubmit[]
}

/** GET /datasources/{id}/catalog/{table}/impact */
export interface CatalogImpact {
  table: string
  /** 直接引用的在前 */
  templates: CatalogImpactTemplate[]
}

// ===========================================================================
// 数据剖析（后端 app/data/catalog_profile.py、catalog_profile_jobs.py）：对业务库发少量只读查询，核实推断的关联关系、
// 取码值候选、提议业务日期。默认关闭，按数据源在 options.catalog_profile 里开启。
// 剖析是服务端的后台任务（/api/datasources/{id}/catalog/profile-jobs）：逐表剖析，每张表有自己的查询次数和时长预算，
// 每张表一结束就写进目录；查询一条一条发，可以随时停止。和同一个数据源的起草任务按建立顺序排队，一个结束了另一个才开始。
// 服务重启时还没结束的任务记 interrupted，不自动续跑（由人点「重试没做完的表」）。
// ===========================================================================

/** 剖析设置里的数值项 */
export type CatalogProfileNumberKey = 'table_max_queries' | 'table_max_s' | 'query_timeout_s' | 'sample_size' | 'max_scan_rows'

/** 一个数据源的剖析设置（服务端 ProfileSettings.to_dict）；任务里的 settings 是建任务时的快照 */
export interface CatalogProfileSettings {
  enabled: boolean
  /** 每张表最多发几条查询，用完只停这张表 */
  table_max_queries: number
  /** 每张表最多剖析几秒，到时只停这张表 */
  table_max_s: number
  /** 单条查询时限（不超过数据源自身的查询时限，也不超过这张表剩下的时长） */
  query_timeout_s: number
  sample_size: number
  /** 0 表示一律不做整表统计 */
  max_scan_rows: number
}

/** 为什么需要剖析（GET /catalog 每行的 profile_due_reason） */
export type CatalogProfileDueReason = 'never' | 'unfinished' | 'new_relations' | 'new_checks'

/**
 * 剖析的范围：selected 指定的表；needed 需要剖析的表；all 全部可剖析的表（不含视图）；observe 观察到期的表（只做取值观察）；
 * maintenance 定时维护（需要剖析的表 + 观察到期的表）。后两种由定时维护建，界面不发起
 */
export type CatalogProfileScope = 'selected' | 'needed' | 'all' | 'observe' | 'maintenance'

/**
 * 剖析任务的状态：排队、进行中、完成、部分完成（有失败、部分完成、已停止的表）、失败（任务整体出错）、已停止（使用者停止）、
 * 服务重启中断。和起草任务同一套
 */
export type CatalogProfileJobStatus = CatalogDraftJobStatus

/**
 * 任务里一张表的状态。ok：每项检查都有结论；partial：有项因为运行原因没查成（unfinished）；failed：写入一直冲突或整张表出错；
 * skipped：视图、表结构里没有、空表、没有可剖析的项；cancelled：停止、止损、服务重启时还没开始、没做完
 */
export type CatalogProfileTaskStatus = CatalogDraftTaskStatus

/** 跳过一项的原因（CatalogProfileSkip.reason、检查项的 reason） */
export type CatalogProfileSkipReason = 'budget' | 'deadline' | 'failed' | 'timeout' | 'error' | 'rejected' | 'slow' | 'cancelled'
  | 'masked' | 'too_large' | 'view' | 'unsupported' | 'no_data' | 'missing' | 'high_cardinality' | 'nothing'

/** 一类检查：关联关系、码值、日期列；取值观察的四类（行数、码值观察、最新业务日期、按日行数） */
export type CatalogProfileCheckKind = 'relation' | 'codes' | 'date' | 'rows' | 'codes_observe' | 'freshness' | 'daily_volume'

/** 发现的合计（任务里是逐表合计） */
export interface CatalogProfileSummary {
  /** 有结论的关系条数 */
  relations_checked: number
  /** 其中状态为已验证的 */
  relations_verified: number
  /** 取到码值候选的列数 */
  codes: number
  /** 这些码值里含义还空着的个数之和 */
  codes_pending: number
  /** 提议了业务日期的表数（逐表时 0 或 1） */
  business_dates: number
}

/** 一张表的行数：stats 数据库的统计信息（估算）；count 数到上限为止；unknown 没能得到 */
export interface CatalogProfileSize {
  rows: number | null
  method: 'stats' | 'count' | 'unknown' | (string & {})
  /** 数到上限也没数完：至少这么多行 */
  at_least: number | null
}

/** 关系的核对结论：覆盖率、基数，升为已验证（verified）或保持推断（proposed）；人工确认过的只补覆盖率和基数 */
export interface CatalogProfileRelationFinding {
  kind: 'relation'
  path: string
  target: string
  columns: string[]
  to_table: string
  to_columns: string[]
  status: CatalogStatus
  confirmed: boolean
  /** 剖析之前目录里这条关系的状态：报告按它说「升为已验证」还是「仍为已验证」。老服务端不给 */
  previous_status?: CatalogStatus | null
  coverage: number
  cardinality: CatalogCardinality | null
  /** 基数是不是用数据核实的（子表一侧数过是否唯一）。老服务端不给 */
  cardinality_checked?: boolean
  /** 抽了几个不同的键值、在被指向表里对上几个 */
  sample: number
  matched: number
  summary: string
}

/** 码值候选：观察到的取值和行数，含义留给人填 */
export interface CatalogProfileCodesFinding {
  kind: 'codes'
  path: string
  column: string
  values: { value: string; rows: number }[]
  rows: number
  status: CatalogStatus
  /** 写进目录的码值里还有几个含义空着（别的来源写过的含义会沿用）。老服务端不给 */
  pending?: number
  summary: string
}

/** 业务日期提议：表里只有一个日期类列 */
export interface CatalogProfileDateFinding {
  kind: 'business_date'
  path: string
  column: string
  min: string
  max: string
  status: CatalogStatus
  summary: string
}

export type CatalogProfileFinding = CatalogProfileRelationFinding | CatalogProfileCodesFinding | CatalogProfileDateFinding

/** 没做的一项。detail 是可以直接显示的整句 */
export interface CatalogProfileSkip {
  /** relation / codes / date / row_estimate / table */
  kind: string
  target: string
  path: string | null
  /**
   * 运行原因没查成（计入 unfinished、使表成为部分完成）：budget 这张表的查询次数用完、deadline 这张表的时长用完、failed 连续失败
   * 止损、timeout、error、rejected 未通过只读守卫、slow 超时比例止损、cancelled 停止或服务重启。
   * 按规则不查（不算没做完）：masked、too_large、view、unsupported、no_data、missing、high_cardinality、nothing 没有可剖析的项
   */
  reason: CatalogProfileSkipReason | (string & {})
  detail: string
}

/** 任务里的一张表（按 seq） */
export interface CatalogProfileTaskRow {
  seq: number
  table_name: string
  status: CatalogProfileTaskStatus
  checks_total: number
  /** 结束了的项（含跳过的） */
  checks_done: number
  /** 每类检查 [结束了的, 一共]；这类没有检查时是 [0, 0]。取值观察的四类合成 observe 一类（老服务端不给） */
  checks_by_kind: Partial<Record<'relation' | 'codes' | 'date' | 'observe', [number, number]>>
  /** 进行中时正在做哪一项的整句（如「正在核对 orders.customer_id → customers.id」）；其余时候 null */
  current: string | null
  queries: number
  elapsed_ms: number
  row_estimate: CatalogProfileSize | null
  summary: CatalogProfileSummary
  /** 跳过的项数 */
  skipped_count: number
  /** 其中因为运行原因没查成的项数 */
  unfinished: number
  added: number
  updated: number
  removed: number
  version: number
  /** 整张表的原因：表结构里没有、视图、空表、没有可剖析的项、写入一直冲突、停止 / 中断 / 止损 */
  error: string | null
  started_at: string | null
  finished_at: string | null
}

/** 检查清单里的一项 */
export interface CatalogProfileCheck {
  kind: CatalogProfileCheckKind
  /** 哪一项：关系写「customer_id → customers.id」，码值、日期写列名 */
  target: string
  /** 审阅路径；没有为 null */
  path: string | null
  state: 'pending' | 'running' | 'done' | 'skipped'
  /** 跳过的原因（同 CatalogProfileSkip.reason）；没跳过为 null */
  reason: CatalogProfileSkipReason | (string & {}) | null
  /** 可以直接显示的整句（结论或跳过的原因）；没有为 null */
  detail: string | null
  queries: number
}

/** 发过的一条查询。sql 的字面量都换成了 ?（IN 列表里抽到的键值不留在 AgentLab 的库里） */
export interface CatalogProfileQuery {
  at: string
  kind: 'row_estimate' | CatalogProfileCheckKind
  target: string
  sql: string
  ms: number
  /** 返回的行数；没返回（超时、出错、守卫拒）时为 null */
  rows: number | null
  outcome: 'ok' | 'timeout' | 'error' | 'rejected'
  /** 超时、出错、守卫拒时的原因 */
  detail: string | null
}

/** GET /profile-jobs/{id}/tasks/{seq}：一张表的完整结果；GET /catalog/{table} 的 last_profile 也是这个形状 */
export interface CatalogProfileTaskDetail extends CatalogProfileTaskRow {
  job_id: string
  job_status: CatalogProfileJobStatus
  created_by: string | null
  checks: CatalogProfileCheck[]
  findings: CatalogProfileFinding[]
  skipped: CatalogProfileSkip[]
  /** 日期类列的取值范围 */
  /** invalid：零日期、占位日期这类不计入最小值和最大值的个数（没有时不给） */
  date_ranges: { column: string; min: string; max: string; invalid?: number }[]
  query_log: CatalogProfileQuery[]
}

/** 一次剖析任务 */
export interface CatalogProfileJob {
  id: string
  source_id: string
  status: CatalogProfileJobStatus
  scope: CatalogProfileScope
  /** 建任务时的剖析设置快照 */
  settings: CatalogProfileSettings
  created_at: string
  started_at: string | null
  finished_at: string | null
  /** 谁建的（X-Actor） */
  created_by: string | null
  /** 重试建的任务记着原任务的 id */
  retry_of: string | null
  /** manual 人点的；schedule 定时维护建的（fire_at 是那一次触发的计划时刻）。老服务端不给 */
  trigger?: 'manual' | 'schedule'
  fire_at?: string | null
  /** 排在同一个数据源的起草任务后面：起草结束后自动开始。不在等时为 null */
  waiting_for: 'draft' | 'introspect' | null
  total: number
  /** 已结束的表数 */
  done: number
  counts: Record<CatalogProfileTaskStatus, number>
  /** 整个任务发出的查询数、其中超时的、失败的 */
  queries: number
  timeouts: number
  errors: number
  /** 任务级止损停下的原因：failed 连续 3 条查询失败；slow 最近 10 条里 7 条超时。没停为 null */
  stop_reason: 'failed' | 'slow' | null
  /** 最近查询多数超时时的提醒（一句中文）；好转后服务端清掉 */
  notice: string | null
  /** 任务整体出错或止损的整句原因 */
  error: string | null
  added: number
  updated: number
  removed: number
  summary: CatalogProfileSummary
  tasks: CatalogProfileTaskRow[]
}

/** 清单一行上这张表最近一次剖析 */
export interface CatalogLastProfile {
  job_id: string
  seq: number
  status: CatalogProfileTaskStatus
  started_at: string | null
  finished_at: string | null
  summary: CatalogProfileSummary
  skipped_count: number
  unfinished: number
  error: string | null
  row_estimate: CatalogProfileSize | null
}

/** 目录清单顶上的进行中剖析任务（GET /catalog 的 active_profile_job） */
export interface CatalogActiveProfileJob {
  id: string
  status: CatalogProfileJobStatus
  total: number
  done: number
  waiting_for: 'draft' | 'introspect' | null
}

// ===========================================================================
// 基于数据目录的 SQL 检查（后端 app/data/sqlcheck.py）：七条规则，编号稳定
// ===========================================================================

export type SqlCheckCode = 'fanout_sum' | 'stock_summed' | 'join_unconfirmed' | 'ratio_aggregated' | 'missing_valid_filter'
  | 'unknown_code' | 'wrong_date_column'

/** error：依据已核实、结果很可能有误；warning：依据有确证、结果可能有误；info：依据只是推断 */
export type SqlCheckLevel = 'error' | 'warning' | 'info'

/** 一条检查结果（证据接口查询步骤里的 checks、助手自查和发布前检查的问题）。交回模型改写用的那句不上界面，这里不收 */
export interface SqlCheckItem {
  code: SqlCheckCode | (string & {})
  level: SqlCheckLevel | (string & {})
  /** 给人看的说明 */
  message: string
  /** schema_cache 里的表名 */
  table: string
  column?: string
  relation_id?: string
  sql_excerpt?: string
}

// ===========================================================================
// 数字推演（后端 app/api/whatif.py、app/engine/recompute.py）：在一次运行的口径卡上改输入、排除查询快照里的行，
// 看指标和报告里引用它的地方会变成多少。推演不是出具：不写运行记录、不改报告
// ===========================================================================

/** 推演的改动：输入路径 → 新值；查询快照的工件 id → 要排除的行号（快照里的行号，从 0 数） */
export interface WhatIfOverrides {
  inputs: Record<string, number>
  exclude_rows: Record<string, number[]>
}

/** POST /runs/{id}/evidence/whatif 的请求体 */
export interface WhatIfRequest {
  /** 报告撰写节点 id：只列这份报告里引用的地方 */
  report?: string
  /** 口径卡节点 id */
  card: string
  /** 只推演这一个指标 */
  metric?: string
  overrides: WhatIfOverrides
}

/** 一个输入在推演里的样子 */
export interface WhatIfInput {
  path: string
  via?: string | null
  node_id?: string | null
  /** 落在哪份查询快照上（cell() 取的格、agent 字段、整份查询结果）：排除这份快照里的行会影响它 */
  artifact?: string
  locator?: EvidenceLocator
  kind: 'number' | 'text' | 'bool' | 'missing' | 'container' | (string & {})
  /** 数值（或原本缺失）的标量、不是遮罩列、不决定取哪一格、还原得出：可以改。整个指标推演不了时一律为 false */
  editable: boolean
  /** 原值、推演用的值。容器写大概（「[12 项]」）；遮罩列的一律是 null */
  old?: unknown
  new?: unknown
  changed?: boolean
  /** 起了作用的被排除行（快照里的行号，从 0 数） */
  excluded_rows?: number[]
  /**
   * 这个输入实际用到的快照行（含两端，从 0 数）：单格一行、数组字段一段、整份查询结果全部。排除别的行影响不到它，
   * 界面只让这几行可勾。遮罩的、还原不了的（完整值找不回来、快照读不出来）没有，决定取哪一格的数组字段、整份结果
   * 也没有（少了几行，取到哪一格说不准），整个指标推演不了时一律没有
   */
  row_span?: [number, number]
  masked?: boolean
  /** 落在的查询快照读不出来（文件不在、与哈希不一致）：遮没遮核对不了，值不给 */
  withheld?: boolean
}

/**
 * unchanged：没碰到改动，原值原样交回；ok：重算了；excluded：排除行后缺了输入，算不出来；error：新值让计算出错（除以零……）；
 * unsupported：推演不了（有输入还原不了、口径卡没记输入，或者排除行后说不准受不受影响）；masked：用到了遮罩列，
 * 不推演。整个指标推演不了的不改动也是 unsupported / masked（unsupported 里另有一条 input 为空的）
 */
export type WhatIfStatus = 'unchanged' | 'ok' | 'excluded' | 'error' | 'unsupported' | 'masked'

export interface WhatIfMetric {
  id: string
  name: string
  unit: string
  decimals?: number | null
  format?: string
  old: unknown
  new: unknown
  delta: number | null
  /** 按口径卡的格式渲染（和报告里的字同一套规则） */
  rendered_old: string
  /** unsupported、masked 时为 null：不知道会变成什么 */
  rendered_new: string | null
  /** 带正负号；比率、百分数写「个百分点」。没变化、不是数时为 null */
  rendered_delta: string | null
  changed: boolean | null
  status: WhatIfStatus | (string & {})
  reason: string | null
  /** 推演用的代入式（重算了才有；没碰到改动的是卡上原来的代入式） */
  substituted: string | null
  inputs: WhatIfInput[]
}

/** 报告里引用这个指标的一处，推演之后会写成什么 */
export interface WhatIfSegment {
  report: string
  unit: string
  sid: string
  metric: string
  rendered_old: string
  rendered_new: string | null
  changed: boolean | null
}

export interface WhatIfUnsupported {
  metric: string
  /**
   * null：整个指标都推演不了（口径卡没记输入、用到了遮罩列、有输入还原不了或所在的快照读不出来），reason 和指标上的
   * 一样。不论改什么都推演不了，不改动也有这一条；界面据此只写原因，不画输入框、勾选框
   */
  input: string | null
  reason: string
}

/** 引用了这张口径卡的下游口径卡：只标可能受影响，不重算 */
export interface WhatIfAffectedCard {
  node_id: string
  artifact: string
  caliber: string
  version: string
  metrics: string[]
  /** 直接引用（false 是经别的卡间接引用） */
  direct: boolean
}

/**
 * GET /runs/{id}/evidence/whatif/rows：一页查询快照的行（推演时勾行用），形状和证据面板的查询步骤一致，
 * 可以直接交给 queryWindow。遮罩列写「已遮罩」；快照读不出来时没有行，note 说明原因
 */
export interface WhatIfRowsPage {
  run_id: string
  artifact: string
  node_id?: string | null
  tool?: string | null
  source?: string | null
  columns: string[]
  rows: unknown[][]
  row_offset: number
  row_index: number[]
  total_rows: number | null
  truncated: boolean | null
  masked: string[]
  hash_ok: boolean | null
  sealed: boolean
  note?: string
}

export interface WhatIfResult {
  run_id: string
  card: { node_id: string; artifact: string; caliber: string; version: string }
  report: string | null
  /** 运行已封存且封存核对一致：推演依据的数据不会再变 */
  sealed: boolean
  seal: { sealed: boolean; ok: boolean | null }
  metrics: WhatIfMetric[]
  segments: WhatIfSegment[]
  unsupported: WhatIfUnsupported[]
  affected_cards: WhatIfAffectedCard[]
}

// ---------------------------------------------------------------------------
// 定时任务（/api/schedules，页面在 pages/schedules）。前后端按同一份接口约定，字段名和形状不各自发挥；时间一律是
// ISO 字符串（UTC），按定时任务自己的时区显示
// ---------------------------------------------------------------------------

/**
 * 运行列表里的定时来源：发起它的定时任务，手动发起的是 null，老服务端不给。写在这里而不是上面 Run 的定义里：
 * 同名接口会合并，定时任务的类型集中在文件末尾这一段，不和别的工作包改同一处
 */
export interface Run {
  schedule_id?: string | null
}

export type ScheduleRunClass = 'formal' | 'exploratory'
/** Webhook 消息的格式：通用 JSON、飞书 / 企业微信 / 钉钉的群机器人 */
export type ScheduleWebhookFormat = 'generic' | 'feishu' | 'wecom' | 'dingtalk'
/** 订阅的事件：运行结束、运行失败、停在人工审批上（只在真有待审批时发） */
export type ScheduleEvent = 'finished' | 'failed' | 'awaiting_approval'
/** 触发的来源：到点、手动「立即运行」、错过之后在宽限内补发 */
export type ScheduleFireKind = 'scheduled' | 'manual' | 'misfire'
/** 触发的结局：发起了运行、上一次还没结束而跳过、错过时间超出宽限而跳过、发起时被拒 */
export type ScheduleFireStatus = 'started' | 'skipped_overlap' | 'skipped_misfire' | 'failed_to_start'
/**
 * 定时的目标种类（next-round X4：定时只有一套）：工作流、评测题集、数据源维护。非工作流目标没有工作流、输入模板、运行类别
 * 和 Webhook（backend/app/api/schedules.py）
 */
export type ScheduleTargetKind = 'workflow' | 'eval_suite' | 'source_maintenance'

/** 读出来的 Webhook 设置。完整地址服务端加密保存、不再返回，只露主机和末 4 位 */
export interface ScheduleWebhook {
  format: ScheduleWebhookFormat
  /** 如 open.feishu.cn/…ab12 */
  url_hint: string
  has_secret: boolean
  events: ScheduleEvent[]
}

/** 一次触发发出的一条通知 */
export interface ScheduleFireNotice {
  event: ScheduleEvent
  status: 'ok' | 'failed'
  at: string
}

/**
 * 运行成功结束后和同一定时任务上一次成功的运行的自动对比：基准是哪一次、各类指标的个数（同对比接口的 summary）。
 * 只有个数，具体的数在对比页上
 */
export interface ScheduleFireCompare {
  base_run_id: string
  summary: RunCompareSummary
  /**
   * 基准是不是紧挨着的上一次。上一次失败了、取消了时，基准是再往前成功的那一次，为 false：界面写明比的是哪一次。
   * 老服务端不给，按 true 看
   */
  previous?: boolean
}

/** 一次触发（触发历史的一行） */
/** 数据源定时维护的步骤（app/data/source_maintenance.STEPS） */
export type ScheduleMaintenanceStep = 'introspect' | 'profile_needed' | 'observe'

/**
 * 数据源维护的一次触发建了什么（app/data/source_maintenance 记在触发上的 inputs）：要做的步骤、建了的探查 / 剖析任务（step 是
 * 这个任务做的那几步，剖析、观察合成一个任务时写成 profile_needed+observe）、没执行的步骤和原因。和数据源页「定时维护」的
 * 「最近一次」读的是同一份
 */
export interface ScheduleMaintenanceInputs {
  steps?: ScheduleMaintenanceStep[]
  jobs?: { step: string; kind: 'introspect' | 'profile'; id: string; tables?: number; scope?: string }[]
  skipped?: { step: ScheduleMaintenanceStep; reason: string }[]
}

export interface ScheduleFire {
  id: string
  /** 计划触发的时间；手动运行是按下的那一刻 */
  fire_at: string
  kind: ScheduleFireKind
  status: ScheduleFireStatus
  run_id: string | null
  /** 发起的那次运行现在的状态（同 Run.status） */
  run_status: RunStatus | null
  /** 跳过、发起失败的原因（中文） */
  reason: string | null
  /** 目标是评测题集时这一次触发建的那次评测（没有 run_id）；老服务端不给 */
  eval_run_id?: string | null
  /** 目标是数据源维护时这一次建了哪些任务、哪几步没执行和原因（触发记录的 inputs）；别的目标为 null，老服务端不给 */
  inputs?: ScheduleMaintenanceInputs | null
  notified: ScheduleFireNotice[]
  /**
   * 和同一定时任务上一次成功的运行的对比。运行没有成功结束、没有可比的、还没算、比不了、任何一边又在执行（不在终态）
   * 或重新封存过时为 null；老服务端不给
   */
  compare?: ScheduleFireCompare | null
  created_at: string
}

export interface Schedule {
  id: string
  name: string
  /** 目标：种类、id、种类的叫法、目标现在的名字（不在了是保存时记下的）、还在不在。老服务端不给（都是工作流） */
  target_kind?: ScheduleTargetKind
  target_id?: string
  target_label?: string
  target_name?: string
  target_exists?: boolean
  /** 非工作流目标存空串 */
  workflow_id: string
  workflow_name: string
  /** 五段 cron：分 时 日 月 星期 */
  cron: string
  /** IANA 时区名，如 Asia/Shanghai */
  timezone: string
  run_class: ScheduleRunClass
  /** 字段名 → 模板字符串，变量（{{ schedule.yesterday }} 这类）按触发时间计算 */
  input_template: Record<string, string>
  enabled: boolean
  /** 上一次还没结束时怎么办：目前只有跳过 */
  overlap: 'skip'
  /** 错过触发时间后多少秒内恢复还补发一次 */
  misfire_grace_s: number
  webhook: ScheduleWebhook | null
  /** 暂停中、或者往后不会再触发时为 null */
  next_run_at: string | null
  last_fire: ScheduleFire | null
  /** 频率的中文说法，如「每周一 09:00（Asia/Shanghai）」 */
  description_text: string
  created_by: string | null
  created_at: string
  updated_at: string
  /** 乐观锁：修改时带上读到的版本，别人在这期间改过时回 409 */
  version: number
}

/** 新建、修改时交上去的 Webhook。地址、密钥的明文只在写的时候给 */
export interface ScheduleWebhookIn {
  format: ScheduleWebhookFormat
  /** 修改时：省略表示不改，空串表示删除整个 Webhook */
  url?: string
  /** 修改时：省略表示不改，空串表示清除 */
  secret?: string
  events: ScheduleEvent[]
}

/** POST /api/schedules 的请求体 */
export interface ScheduleCreate {
  name: string
  workflow_id: string
  cron: string
  timezone: string
  run_class: ScheduleRunClass
  input_template: Record<string, string>
  enabled: boolean
  overlap: 'skip'
  misfire_grace_s: number
  webhook: ScheduleWebhookIn | null
}

/**
 * POST /api/schedules 建非工作流目标（评测题集、数据源维护，总方案 X4）的请求体：写 target_kind、target_id，不写工作流、
 * 输入模板和 Webhook（服务端回 422，field 是那一项）；运行类别不用。改的时候照样用 SchedulePatch（只带 name、cron、timezone、
 * enabled、misfire_grace_s 这几项）
 */
export interface ScheduleTargetCreate {
  name: string
  target_kind: Exclude<ScheduleTargetKind, 'workflow'>
  target_id: string
  cron: string
  timezone: string
  enabled: boolean
  overlap: 'skip'
  misfire_grace_s: number
}

/** PATCH /api/schedules/{id} 的请求体：只带改了的字段，外加读到的版本 */
export type SchedulePatch = Partial<Omit<ScheduleCreate, 'webhook'>> & {
  webhook?: ScheduleWebhookIn
  version: number
}

export interface SchedulePreviewRequest {
  cron: string
  timezone: string
  input_template?: Record<string, string>
  workflow_id?: string
  n?: number
}

/** 预览：频率的中文说法、接下来几次触发时间、按下一次触发时间渲染出的输入。非法时 422 {detail, field} */
export interface SchedulePreview {
  description_text: string
  next: string[]
  inputs: Record<string, string>
}

/** 输入模板里能用的变量 */
export interface ScheduleVariable {
  /** 如 schedule.yesterday */
  name: string
  /** 如「昨天（YYYY-MM-DD）」 */
  label: string
  example: string
  /** 补充说明（ISO 周的跨年规则），没有的是空串；老服务端没有这个键 */
  note?: string
}

/** POST /api/schedules/{id}/run：这次手动触发和它发起的运行（没发起时为 null） */
export interface ScheduleRunResult {
  fire: ScheduleFire
  run_id: string | null
}

/** GET /api/schedules/{id}/fires 的一页，新的在前；next_before 是下一页的游标，没有更多时为 null */
export interface ScheduleFirePage {
  items: ScheduleFire[]
  next_before: string | null
}

/** POST /api/schedules/{id}/webhook/test：用已保存的设置发一条测试消息 */
export interface ScheduleWebhookTest {
  ok: boolean
  http_status: number | null
  message: string
}

// 运行对比（GET /api/runs/{id}/compare/base、/compare?base=）：两次运行的指标变化按数据 / 口径 / 模板与 SQL 拆开说。
// 形状照前后端共用的接口约定，字段名一个不改；数值都是服务端算好的，界面只排版，渲染值原样显示
// ---------------------------------------------------------------------------

/** 对比里的一次运行。版本是空的：没从固定版本发起（探索运行）；schedule_id 是空的：手动发起 */
export interface RunCompareRef {
  id: string
  workflow_name: string
  version: number | null
  run_class: 'formal' | 'exploratory' | null
  started_at: string | null
  finished_at: string | null
  schedule_id: string | null
}

/** 默认基准从哪来：同一定时任务的上一次，没有就同一工作流、同一运行类别的上一次 */
export type RunCompareBaseReason = 'same_schedule' | 'same_workflow'

/** GET /runs/{id}/compare/base。找不到时 404，detail 是一句原因 */
export interface RunCompareBase {
  base_run_id: string
  reason: RunCompareBaseReason | (string & {})
  base: RunCompareRef
  /**
   * 基准是不是紧挨着的上一次（服务端 pick_base 算的，同触发历史里自动对比的 previous）：上一次失败、取消、还没结束时
   * 为 false，运行详情「更多」里的入口据此写「与上一次成功的运行对比」。老服务端不给，按 true 算
   */
  previous?: boolean
}

/**
 * 指标的分类。unchanged：未变；data_only：只数据变；caliber_and_data：口径与数据都变（能严格拆时给口径效应、数据效应）；
 * sql_changed：所依据的查询 SQL 结构变了（口径不同，不拆）；params_only：SQL 只差字面量（定时运行的日期）；
 * added / removed：只有本次 / 只有基准有；incomparable：缺比较所需的信息
 */
export type RunCompareClass =
  | 'unchanged' | 'data_only' | 'caliber_and_data' | 'sql_changed' | 'params_only' | 'added' | 'removed' | 'incomparable'

/** 各类指标的个数 */
export type RunCompareSummary = Record<RunCompareClass, number>

export interface RunCompareTemplateNode {
  node_id: string
  title: string
  change: 'added' | 'removed' | 'config' | (string & {})
  /** 改了的配置项（节点配置里的键） */
  keys: string[]
  /** 约定之外、服务端顺带给的：各配置项在界面上的叫法，和 keys 一一对应。没给（或对不上）时照写键名 */
  labels?: string[]
}

export interface RunCompareTemplate {
  base_version: number | null
  target_version: number | null
  /** 两次运行的执行图完全相同 */
  same_graph: boolean
  nodes: RunCompareTemplateNode[]
}

/**
 * 严格拆分：口径效应 = 新口径(本次的数据) − 旧口径(本次的数据)，数据效应 = 旧口径(本次的数据) − 旧口径(基准的数据)，
 * 两者之和正好是总变化。*_rendered 是服务端按口径卡格式写好的（带正负号）
 */
export interface RunCompareDecomposition {
  caliber_effect: number
  data_effect: number
  caliber_rendered: string
  data_rendered: string
}

/** 一个输入在两次运行里的值（容器写大概，缺的是 null） */
export interface RunCompareInput {
  path: string
  base: unknown
  target: unknown
  /** 约定之外、服务端顺带给的：取自设置了遮罩的列，两边的值都不给 */
  masked?: boolean
}

export interface RunCompareAttributionRow {
  /** 各维度列上的取值，和 columns 一一对应 */
  key: unknown[]
  base: number | null
  target: number | null
  delta: number | null
  /** 这一项的变化占总变化的比例（小数，0.4 是 40%）。度量列之和对不上指标的输入时为 null，原因在 note */
  share: number | null
}

/** 维度归因：按键对齐两边查询结果的行，变化最大的排在前面 */
export interface RunCompareAttribution {
  columns: string[]
  measure: string
  rows: RunCompareAttributionRow[]
  total_delta: number | null
  note: string | null
}

export interface RunCompareMetric {
  card_id: string
  card_title: string
  metric_id: string
  name: string
  unit: string
  decimals: number | null
  base: number | null
  target: number | null
  base_rendered: string | null
  target_rendered: string | null
  delta: number | null
  /** 带正负号；比率、百分数写「个百分点」。没变化、不是数时可能为 null */
  delta_rendered: string | null
  /** 相对基准变化了百分之几（已乘 100，保留两位：12.5 是 +12.5%，和占比的小数写法不同）；基准为 0、缺值时为 null */
  delta_pct: number | null
  classification: RunCompareClass | (string & {})
  reasons: string[]
  decomposition: RunCompareDecomposition | null
  /** 没法严格拆分的原因；能拆、或者用不着拆（只数据变、未变）时为 null */
  decomposition_note: string | null
  inputs: RunCompareInput[]
  attribution: RunCompareAttribution | null
}

/** same：完全相同；params_only：规范化之后相同，只差字面量；changed：结构不同；added / removed：只有一边有 */
export type RunCompareStructure = 'same' | 'params_only' | 'changed' | 'added' | 'removed'

export interface RunCompareQuery {
  node_id: string
  node_title: string
  /** 同一节点里的第几条查询（服务端的序号，只用来排序和区分，界面另按 1 起数） */
  ordinal: number
  structure: RunCompareStructure | (string & {})
  base_sql: string | null
  target_sql: string | null
  /** 不同的字面量（SQL 写法，字符串带引号），按出现的顺序 */
  literals: { base: unknown; target: unknown }[]
  base_rows: number | null
  target_rows: number | null
  base_truncated: boolean | null
  target_truncated: boolean | null
  /** 约定之外、服务端顺带给的：结构变了时差在哪，一处一句中文（按对结果的影响从大到小） */
  differences?: string[]
  /** 约定之外、服务端顺带给的：快照读不出来、与哈希不一致时的一句说明 */
  problem?: string
}

/** GET /runs/{id}/compare?base=。任一未封存或封存核对不一致 409；不是同一工作流、是同一次运行 422 */
export interface RunComparison {
  target: RunCompareRef
  base: RunCompareRef
  summary: RunCompareSummary
  template: RunCompareTemplate
  metrics: RunCompareMetric[]
  queries: RunCompareQuery[]
  notes: string[]
}

/** 运行接口回的运行带着发起它的定时任务（RunOut.schedule_id）；Run 上没写这一项，对比页的运行卡要用 */
export type RunWithSchedule = Run & { schedule_id?: string | null }

// ---------------------------------------------------------------------------
// 双盲复算：审批 payload、完整对照工件、节点配置，以及设置里「核查」一组（期 3 前后端共用的约定）
// ---------------------------------------------------------------------------

/** 审核人对一个指标的裁决：a 采用原流程的结果，b 采用盲算的结果，neither 两边都不对 */
export type DualCheckChoice = 'a' | 'b' | 'neither'

/** 双盲复算挂起时 payload.summary 的一项：审批卡先用它画，完整对照另从 card_artifact 取 */
export interface DualCheckSummaryItem {
  metric_id: string
  name: string
  unit?: string | null
  original_rendered: string | null
  blind_rendered: string | null
  /** (盲算值 − 原流程值) ÷ |原流程值| × 100，保留两位小数（12.5 表示相差 12.5%）；原流程值为 0 时为 null */
  delta_pct: number | null
  /** 具体差异，一条一句中文，最多 3 条 */
  diff: string[]
  /** 只有兜底说明：两条 SQL 的写法不同，未能归纳成具体差异 */
  fallback: boolean
  /**
   * 约定之外、服务端另带的：采用原流程（a）、采用盲算（b）各会往数据目录里写什么。老服务端没有这一项，卡上按差异的
   * 形状有条件地说
   */
  catalog?: { a: DualCheckCatalogSide; b: DualCheckCatalogSide }
}

/**
 * 采用一边的结果会往数据目录里写什么：服务端按发起审批时的数据目录预告（dual_check_facts.preview，和裁决后真写的
 * 是同一套判断）。write 为真时 table 的 item（business_date / valid_filter / dedup）会记为 value（有效记录条件是和
 * 目录里已有的条件合起来之后的整句）；为假时 reason 说明为什么不写
 */
export type DualCheckCatalogSide =
  | { write: true; source?: string | null; table: string; item: string; value: unknown }
  | { write: false; reason?: string | null }

/** payload.kind 为 dual_check 的审批 payload（审批的 mode 是 input） */
export interface DualCheckPayload {
  kind: 'dual_check'
  node_id: string
  title: string
  /** 完整对照放在这个工件里（GET /api/artifacts/{id}），内容见 DualCheckCard */
  card_artifact: string
  summary: DualCheckSummaryItem[]
  /** 约定之外、服务端另带的：节点实际生效的「未采用原流程时」。没有时卡上从运行的图快照里读 */
  on_reject?: 'degrade' | 'fail'
}

/** 审核人的回复：POST /approvals/{id}/decide 的 value */
export interface DualCheckReply {
  choices: Record<string, DualCheckChoice>
  note: string
}

/** 指标的一个输入：取自哪条查询的哪一格（alias 是查询快照的编号） */
export interface DualCheckInput {
  path: string
  value: unknown
  sql?: string | null
  source?: string | null
  alias?: string | null
}

/** 盲算那一边的输入多一段盲算模型自己的说明 */
export interface DualCheckBlindInput extends DualCheckInput {
  rationale?: string | null
}

export interface DualCheckSide<I extends DualCheckInput = DualCheckInput> {
  value: number | null
  rendered: string | null
  inputs: I[]
}

/** 两条 SQL 差在哪（服务端 sql_semantics.explain_sql_diff）。aspect 为 other 的是兜底说明，parse 是解析不了 */
export interface DualCheckDiff {
  equivalent: boolean
  items: { aspect: string; text: string }[]
  /** 用了兜底说明（一条具体说法都没有，或者具体说法只覆盖了一部分） */
  fallback: boolean
  /**
   * 规范化之后两条 SQL 不同的行：「- 」开头是原流程的，「+ 」开头是盲算的；「  」开头的是两边相同的上级行，只为标出
   * 不同的行在哪个子句里（老工件里没有）
   */
  text_diff: string[]
}

export interface DualCheckMetric {
  metric_id: string
  name: string
  unit?: string | null
  decimals?: number | null
  original: DualCheckSide
  blind: DualCheckSide<DualCheckBlindInput>
  diff: DualCheckDiff
  /** 相差比例，口径同 DualCheckSummaryItem.delta_pct（有的话） */
  delta_pct?: number | null
}

/** card_artifact 工件的内容 */
export interface DualCheckCard {
  metrics: DualCheckMetric[]
  /** 盲算用的模型 */
  model?: string | null
  /** 盲算模型和原流程的模型相同 */
  same_model?: boolean
}

/** 双盲复算节点的配置。留空的项：card 取上游的口径卡，metrics 取输入来自查询的全部指标，其余跟随设置 */
export interface DualCheckConfig {
  card?: string
  metrics?: string[]
  definitions?: Record<string, string>
  provider?: string
  model?: string
  tolerance?: number
  on_reject?: 'degrade' | 'fail'
  /** null 或不写：跟随设置 */
  enabled?: boolean | null
}

/** 设置里的 verify 一组（GET /settings 给的是补齐默认之后的完整一组；上限为 null 表示不限） */
export interface VerifySettings {
  provider: string | null
  model: string | null
  dual_check: { enabled: boolean; tolerance: number; max_queries: number | null; timeout_s: number | null }
  challenge: { enabled: boolean; max_claims: number | null; max_queries: number | null; timeout_s: number | null }
  inquiry: { enabled: boolean; max_queries: number | null; timeout_s: number | null }
  daily_max_usd: number | null
}

/** GET /settings/verify/spend：今天（本地日期）核查花了多少。unpriced_calls 是估不出金额、没算进 usd 的调用次数 */
export interface VerifySpend {
  date?: string
  usd?: number
  calls?: number
  unpriced_calls?: number
  daily_max_usd?: number | null
}

// ---------------------------------------------------------------------------
// 反方审稿（challenge 节点）：审批卡上的概况、challenge_artifact 工件的内容。形状照前后端共用的接口约定，字段名一个不改；
// 判断由反方模型给出，界面只排版，理由正文、行数这些都是服务端给的
// ---------------------------------------------------------------------------

/** 一句结论经受住反方的复核没有：经受住 / 被削弱 / 被推翻 / 无法判断 */
export type ChallengeVerdict = 'stands' | 'weakened' | 'refuted' | 'inconclusive'

/** 各种判断的句数 */
export type ChallengeCounts = Record<ChallengeVerdict, number>

/** 审批卡、节点产出里列的一句：unit 是报告文档里这一句的 id，text 是结论原文 */
export interface ChallengeItem {
  unit: string
  text: string
  verdict: ChallengeVerdict | (string & {})
}

/**
 * 审批卡上的反方审稿概况（human 节点 payload.reports[i].challenge）。items 最多 10 条，完整内容在 artifact 指的工件里。
 * on_refuted 是实际生效的处理方式：degrade 被推翻的结论计入出具缺口；flag 只标注。报错（fail）时节点已经失败，到不了审批。
 * 老服务端不给时按 degrade
 */
export interface ApprovalChallenge {
  counts: ChallengeCounts
  items: ChallengeItem[]
  /** challenge_artifact 工件 id。服务端保存审稿记录失败时为 null：判断照样有，理由和查询看不了 */
  artifact: string | null
  on_refuted?: 'degrade' | 'flag' | (string & {})
  /** 期 4（nodes/challenge.challenge_summary）：没能反查的句数，都记为无法判断。升级前的产出、老服务端没有 */
  unreviewed?: number
  /**
   * 期 4：被推翻的会不会让出具降档（按降档处置、又接在带出具契约的成果节点上游才为真）。升级前的产出、老服务端没有，
   * 审批卡按「没写只标注就降档」的旧规则说
   */
  degrades?: boolean
}

/**
 * 审批审的报告经过反方审稿时多一个 challenge。和上面的 ApprovalReport 是同一个接口（声明合并）：反方审稿的类型集中在
 * 这一段，这一项也就近写在这里
 */
export interface ApprovalReport {
  challenge?: ApprovalChallenge
}

/** 反方为复核一句结论执行的一次只读查询：编号是它自己的（Q1、Q2…），和报告里的查询编号无关 */
export interface ChallengeQuery {
  alias: string
  /** 查询快照工件 */
  artifact: string
  sql: string
  row_count: number | null
  truncated: boolean
  /** 数据源名 */
  source: string | null
}

/** 工件里的一句：判断、理由、查询 */
export interface ChallengeClaim extends ChallengeItem {
  /** 理由渲染后的正文（数字已换成查询结果里的值） */
  rationale: string
  /**
   * 理由组装成的文档（和报告文档同一种形状，数字带出处、裸数字被标出）。约定没说是工件 id 还是文档本身，两种都认；
   * 没有时只显示 rationale
   */
  rationale_doc?: string | EvidenceDocData | null
  queries: ChallengeQuery[]
  usage?: { input_tokens?: number; output_tokens?: number; total_tokens?: number; cost_usd?: number | null; calls?: number }
}

/** challenge_artifact 工件的内容：审的哪份报告、每句的复核，以及反方用的模型 */
export interface ChallengeArtifact {
  /** 被审的报告撰写节点 */
  report: string
  doc_artifact: string
  claims: ChallengeClaim[]
  model: string | null
}

// ---------------------------------------------------------------------------
// 审批质询：审批人就待审的报告提问，核查模型只读查库作答。形状照前后端共用的接口约定（F5a / F5b 同一份），字段名一个
// 不改；回答正文、行数、触顶原因都是服务端给的，界面只排版
// ---------------------------------------------------------------------------

/** 回答停在哪个上限上：每次最多查询次数、时长、这一次的金额、核查的每日金额 */
export type InquiryLimit = 'max_queries' | 'timeout_s' | 'max_cost_usd' | 'daily_max_usd'

/** 质询的回答 */
export interface InquiryAnswer {
  /** 渲染后的正文（数字已按出处换好） */
  text: string
  /** compose_doc 组装的文档工件：有它，正文里的数字可以点开看出处；没有（组装失败）只显示 text */
  doc_artifact: string | null
  ok: boolean
  limit: InquiryLimit | (string & {}) | null
  /** 没答成时的中文说明 */
  error: string | null
}

/** 核查模型为回答执行的一次只读查询。编号是这次质询自己的，快照按质询工件接口取（遮罩列已遮掉） */
export interface InquiryQuery {
  alias: string
  artifact: string
  sql: string
  row_count: number | null
  truncated: boolean
  /** 数据源名 */
  source: string | null
}

export interface InquiryUsage {
  input_tokens?: number
  output_tokens?: number
  cost_usd?: number | null
  calls?: number
  unpriced_calls?: number
}

/** 一次质询：问的是哪份报告的哪一句（或划选的哪段原文）、问了什么、怎么答的、查了什么 */
export interface Inquiry {
  /** 运行记录里那条 approval.inquiry 事件的序号；没写进运行记录时为 null */
  seq: number | null
  /** 什么时候问的（ISO 时间或秒级时间戳） */
  at: string | number | null
  /** 提问人的署名；没署名时为 null */
  actor: string | null
  /** 报告撰写节点 id */
  report: string
  /** 句子单元 id（如 u12）；就整份报告提问时为 null */
  unit: string | null
  /** 划选的原文 */
  quote: string | null
  question: string
  answer: InquiryAnswer
  queries: InquiryQuery[]
  /** 作答的核查模型 */
  model: string | null
  usage: InquiryUsage
  /** 运行封存之后才问的（只有探索运行可以） */
  post_seal: boolean
  /** 提问时停在的那张审批；封存之后问的为 null */
  approval_id: string | null
}

/** POST /runs/{id}/inquiries 的请求体 */
export interface InquiryRequest {
  report: string
  unit: string | null
  quote?: string | null
  question: string
  /** 运行停在审批上时必填：指明是哪一张 */
  approval_id?: string
}

/** POST /runs/{id}/inquiries 的答复 */
export interface InquiryResponse {
  inquiry: Inquiry
  /** 是否写进了运行记录 */
  recorded: boolean
  /** 没写进运行记录时的中文说明，否则为 null */
  note: string | null
}

/** GET /runs/{id}/inquiries 的答复 */
export interface InquiryList {
  items: Inquiry[]
}

/**
 * GET /runs/{id}/inquiries/artifacts/{artifact}：质询引用过的查询快照，遮罩列照证据面板的规则遮掉（值写「已遮罩」）。
 * 约定只说了是快照：可能是快照本身，也可能包在 {id, content} 里（同工件接口），界面两种都认
 */
export interface InquirySnapshot {
  columns: string[]
  rows: unknown[][]
  sql: string | null
  truncated: boolean
  /** 结果一共几行（快照的 row_count / total_rows），不给时按 rows 的长度 */
  total: number | null
  /** 遮掉的列 */
  masked: string[]
  source: string | null
}

// ---------------------------------------------------------------------------
// 应用平台（docs/design/app-platform-and-look.md）：运行来源、版本的发布 / 退役与使用情况、从运行提取的来源。
// 接口约定的主体在 typesApp.ts（冻结）；这里只往已有的类型上补字段，同名接口会合并
// ---------------------------------------------------------------------------

/** 运行来源（X3）。老运行四项都为空，界面写「来源未记录」 */
export interface Run {
  origin_kind?: import('./typesApp').OriginKind | null
  /** run.origin 事件的数据：kind、app_id、entry / view、hook、actor、actor_verified…… */
  origin?: Record<string, any> | null
  app_id?: string | null
  app_version?: number | null
}

/** 每个版本各自记发布、退役（A1、F5）；VersionDetailOut 另带入口表单和使用情况 */
export interface WorkflowVersion {
  level?: 'published' | 'governed' | null
  published_at?: string | null
  published_by?: string | null
  retired_at?: string | null
  retired_by?: string | null
  retire_note?: string | null
  /** 入口字段按 agentlab.form/1 转好的表单（服务端 input_schema(fields)），发起表单照它画 */
  input_schema?: import('./typesApp').FormSchema | null
  /** 被哪些应用固定、生效记录数、正式运行数（同 GET /api/ledger/workflow-versions/{wf}/{v}） */
  usage?: { apps: { app_id: string; name: string; app_version: number }[]; effects: number; formal_runs: number } | null
}

/** 从运行提取时的来源：run_id、对话、轮次（/copilot/from-run 写） */
export interface Workflow {
  origin?: Record<string, any> | null
}

/**
 * GET /api/datasources/names（应用平台 D-c）：开了登录、允许使用者问数据时，问数据页按它列可选的数据源。
 * 只有名字和说明，没有连接信息、表结构
 */
export interface DataSourceName {
  id: string
  name: string
  description?: string
  kind?: string
  enabled?: boolean
}

// ---------------------------------------------------------------------------
// 驳回 ≠ 失败（docs/design/next-round/05-已知遗留.md (a)，总方案 X9）

/** 运行为什么结束在 cancelled（服务端 Run.end_reason）：rejected 人工驳回，stopped 手动停止。其余终态、老运行为空 */
export type RunEndReason = 'rejected' | 'stopped'

export interface Run {
  end_reason?: RunEndReason | null
}

// ---------------------------------------------------------------------------
// 大库：对象索引、探查范围、后台探查（docs/design/next-round/03-数据接入与目录维护.md A1、A2，第 2 波 W2-a）

/** 探查范围的摘要（服务端 schema_cache.scope）：范围内几张、读到结构的、读不到的、范围外的、上限、各原因几张 */
export interface SchemaScope {
  in_scope: number
  reflected: number
  failed: number
  out_of_scope: number
  cap: number
  index_total: number
  pinned: number
  explicit: number
  rule: number
  on_demand: number
  /** dictionary 字典查询 / inspector 退回 Inspector（没有注释） */
  index_method?: string
  /** 对象索引超过了 5 万个的上限，只收了按名字排序的前面那些 */
  index_capped?: boolean
  index_at?: string
}

export type IntrospectJobStatus = 'queued' | 'running' | 'succeeded' | 'partial' | 'failed' | 'cancelled' | 'interrupted'

/** 同源队里排在前面的事（waiting_for）：起草、剖析、导入目录、同步起草 */
export type LaneKind = 'draft' | 'profile' | 'import' | 'sync_draft' | 'introspect'

/** 数据源卡片上最近一个探查任务（DataSourceOut.introspect_job） */
export interface IntrospectJobBrief {
  id: string
  status: IntrospectJobStatus
  total: number
  done: number
  failed: number
  cancelled: number
  waiting_for: LaneKind | null
  finished_at: string | null
  error: string | null
}

/** GET /datasources/{id}/introspect-jobs/{job_id} 的 job */
export interface IntrospectJob {
  id: string
  source_id: string
  status: IntrospectJobStatus
  /** manual 人点的 | schedule 定时维护 */
  trigger: string
  schema: string
  created_by: string | null
  created_at: string | null
  started_at: string | null
  finished_at: string | null
  total: number
  done: number
  counts: Record<'pending' | 'running' | 'ok' | 'failed' | 'cancelled', number>
  /** 正在读的那张表 */
  current: string | null
  /** 读不到结构的表（最多 50 张）和原因 */
  failures: { name: string; error: string | null }[]
  error: string | null
  /** 建任务那一刻的范围：规则原样、各原因几张、对象索引的情况 */
  scope: Partial<SchemaScope> & { rules?: IntrospectScopeRules; hits?: number; added?: number; removed?: number }
  waiting_for: LaneKind | null
}

/** 探查范围的规则（数据源 options.introspect_scope） */
export interface IntrospectScopeRules {
  mode: 'all' | 'rules'
  include: string[]
  exclude: string[]
  /** 显式列出：一律进范围 */
  tables: string[]
  /** 显式移出：有数据目录、被已发布模板用到的表也排得掉 */
  removed: string[]
  views: boolean
  max_tables: number
  updated_by?: string | null
  updated_at?: string | null
}

/** 按规则对着对象索引数的命中 */
export interface IntrospectScopePreview {
  in_scope: number
  /** 计上限的那部分（规则、一律在范围内、显式列出；按需加入的另算） */
  hits: number
  cap: number
  out_of_scope: number
  index_total: number
  pinned: number
  explicit: number
  rule: number
  on_demand: number
  over: boolean
}

/** 按需加入、目录页手工加入范围的留痕 */
export interface IntrospectScopeTrail {
  name: string
  scope_reason: string | null
  /** look 助手查看 | db_schema 运行时查表结构 | catalog 目录页 */
  via: 'look' | 'db_schema' | 'catalog' | null
  by: string | null
  run_id: string | null
  conversation_id: string | null
  ref: string | null
  need: string | null
  at: string | null
}

/** GET / PUT /datasources/{id}/introspect-scope、POST …/preview */
export interface IntrospectScope {
  scope: IntrospectScopeRules
  defaults: { max_tables: number; max_limit: number; index_limit: number; rules_max: number }
  /** 没有对象索引时为 null：先刷新对象索引才数得出命中 */
  preview: IntrospectScopePreview | null
  index: { ready: boolean; schema: string; method: string | null; capped: boolean; at: string | null; total: number | null }
  /** 一律在范围内的表（举前 20 个例子） */
  pinned_sample: string[]
  on_demand: IntrospectScopeTrail[]
}

export interface DataSource {
  /** 探查范围的摘要；升级前探查的源、上传表格为 null */
  schema_scope?: SchemaScope | null
  /** 最近一个探查任务；没有（或升级前同步探查的）为 null */
  introspect_job?: IntrospectJobBrief | null
}

/**
 * 表结构里没有这张表时为什么（目录清单、表详情的 absent_reason）：removed 表已不在（真删除）、missing 数据源里从来没有、
 * out_of_scope 在探查范围外、reflect_failed 在范围里却读不到结构、unknown 说不清（升级前只探查了一部分的老缓存）
 */
export type CatalogAbsentReason = 'removed' | 'missing' | 'out_of_scope' | 'reflect_failed' | 'unknown'

export interface CatalogTableRow {
  absent_reason?: CatalogAbsentReason | null
}

export interface CatalogDetail {
  absent_reason?: CatalogAbsentReason | null
}

export interface CatalogList {
  /** 探查范围的摘要；升级前探查的源、上传表格为 null */
  scope?: SchemaScope | null
}

/** GET /datasources/{id}/catalog/search 的一项：对象索引里按名字、注释找到的表（含探查范围外的） */
export interface CatalogSearchItem {
  name: string
  qualified: string
  kind: 'table' | 'view'
  comment: string | null
  est_rows: number | null
  in_scope: boolean
  scope_reason: string | null
  absent_reason: CatalogAbsentReason | null
}

export interface CatalogSearch {
  items: CatalogSearchItem[]
  total: number
  /** 有对象索引（没有的只在表结构里找） */
  indexed: boolean
}

/** POST /datasources/{id}/catalog/scope/add */
export interface CatalogScopeAdd {
  added: string[]
  already: string[]
  failed: { name: string; error: string }[]
  refused: { name: string; reason: string }[]
  scope: SchemaScope | null
}
