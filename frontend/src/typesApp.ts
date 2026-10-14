/**
 * 应用平台的前后端接口约定（docs/design/app-platform-and-look.md 第三部分）。
 *
 * 契约骨架之后冻结：要改，向编排者提，由编排者统一改，前后端同时对齐。字段名和后端返回一字不差。
 */

// ---------------------------------------------------------------------------
// 身份（§2.7、§3.10）
// ---------------------------------------------------------------------------

export type AuthMode = 'off' | 'local' | 'oidc'
export type Role = 'admin' | 'builder' | 'user'

/** GET /api/auth/me。关登录时 mode='off'、role='admin'、verified=false、username 为自报署名（可能为空） */
export interface Principal {
  mode: AuthMode
  username: string | null
  display_name: string | null
  role: Role
  groups: string[]
  verified: boolean
  /** 开登录后 user 角色能不能问数据（Q2，管理员开关） */
  user_can_chat?: boolean
}

export interface UserRow {
  id: string
  username: string
  display_name: string
  email: string | null
  role: Role
  disabled: boolean
  source: 'local' | 'oidc'
  last_login_at: string | null
  created_at: string
}

export interface GroupRow {
  id: string
  name: string
  label: string
  source: 'local' | 'oidc'
  members: string[]
}

export interface AuditRow {
  id: string
  at: string
  actor: string | null
  verified: boolean
  action: string
  subject_kind: string
  subject_id: string | null
  data: Record<string, any>
}

// ---------------------------------------------------------------------------
// 表单 agentlab.form/1（§3.3）
// ---------------------------------------------------------------------------

export type FormWidget =
  | 'text' | 'textarea' | 'number' | 'integer' | 'date' | 'datetime' | 'select' | 'radio' | 'multiselect'
  | 'checkbox' | 'principal' | 'note' | 'json'

export interface FormField {
  type: 'string' | 'number' | 'integer' | 'boolean' | 'array'
  title?: string
  description?: string
  default?: unknown
  readOnly?: boolean
  enum?: (string | number)[]
  'x-enum-labels'?: string[]
  minimum?: number
  maximum?: number
  minLength?: number
  maxLength?: number
  maxItems?: number
  format?: 'date' | 'date-time' | 'email' | 'uri'
  items?: { type: 'string'; enum?: string[] }
  'x-widget'?: FormWidget
}

export interface FormSchema {
  format?: 'agentlab.form/1'
  type: 'object'
  title?: string
  required?: string[]
  properties: Record<string, FormField>
  'x-order'?: string[]
}

/** 前后端同一种错误格式 */
export interface FormError {
  path: string
  code: 'required' | 'type' | 'enum' | 'minimum' | 'maximum' | 'min_length' | 'max_length' | 'max_items' | 'format'
  message: string
}

// ---------------------------------------------------------------------------
// 应用描述 agentlab.app/1（§3.2、§3.4、§3.5）
// ---------------------------------------------------------------------------

export type PinUse = 'view' | 'entry' | 'process'
export type Disposition = 'recompute' | 'dual' | 'incomparable'

export interface AppPin {
  alias: string
  workflow_id: string
  version: number
  graph_hash: string
  level?: string | null
  use: PinUse
}

export interface AppEntry {
  id: string
  label: string
  pin: string
  form: FormSchema | { $ref: string }
  map: Record<string, string>
  instance_title?: { pattern: string; fields: string[] } | null
}

export interface AppViewSource {
  kind: 'latest_sealed' | 'on_demand' | 'instance'
  pin?: string | null
  from?: string | null
  params?: FormSchema | { $ref: string } | null
  ttl_s?: number | null
  auto?: boolean
}

/** 绑定：出现哪个键就是哪种（metric / series / cell / query / report / input / run / output / task / action） */
export type AppBinding = { view?: string } & Record<string, any>

export interface AppComponent {
  id: string
  type: string
  props?: Record<string, any>
  bind?: AppBinding | null
  visible?: string | null
  scope?: string | null
  children?: AppComponent[]
}

export interface AppPage {
  id: string
  title: string
  body: AppComponent[]
}

export interface AppSpec {
  format: 'agentlab.app/1'
  name: string
  description?: string
  icon?: string
  pins: AppPin[]
  slots: Record<string, { label: string }>
  entries: AppEntry[]
  forms: Record<string, FormSchema>
  task_ui: Record<string, Record<string, any>>
  views: Record<string, { source: AppViewSource }>
  pages: AppPage[]
  hooks: { name: string; kind: 'start' | 'signal'; entry?: string | null }[]
  notify: { channel: string; events: string[] }[]
}

/** 岗位绑定到人或组（不进 spec，存在应用版本的 bindings 里） */
export interface AppBindings {
  slots: Record<string, { kind: 'user' | 'group'; name: string }[]>
  hooks?: Record<string, any>
  channels?: Record<string, any>
}

export interface CatalogComponent {
  name: string
  label: string
  category: 'layout' | 'display' | 'input' | 'process'
  version: number
  children: boolean
  bindings: string[]
  props: Record<string, any>
}

export interface ComponentCatalog {
  version: number
  categories: Record<string, string>
  components: CatalogComponent[]
}

// ---------------------------------------------------------------------------
// 应用对象（§3.1、§3.10）
// ---------------------------------------------------------------------------

export type AppLevel = 'published' | 'governed'
export type GrantRole = 'use' | 'observe' | 'manage'

export interface AppSummary {
  id: string
  name: string
  description: string
  icon: string
  status: 'active' | 'archived'
  published_version: number | null
  level: AppLevel | null
  /** 钉住的版本被退役：入口拒发新实例 */
  needs_upgrade: boolean
  /** 当前用户在这个应用里的待办数 */
  pending_tasks: number
  /** 当前用户的授权（关登录时为 manage） */
  my_role: GrantRole | null
  updated_at: string
}

export interface AppDetail extends AppSummary {
  /** 草稿和岗位绑定只给 manage（D-m）；use / observe 拿到的详情里没有 */
  draft?: AppSpec
  draft_bindings?: AppBindings
  created_by?: string | null
  /** 当前发布版的页面和入口目录：没有 manage 的人靠它找首页、入口名 */
  published?: { version: number; pages: { id: string; title: string }[]; entries: { id: string; label: string }[] } | null
  /** 能按新口径补算的视图（只在 manage 取详情时算，D-f） */
  backfill?: BackfillPlan[]
}

/** 按新口径补算会发起几次（GET /api/apps/{id}/views/{view}/backfill，不发起运行） */
export interface BackfillPlan {
  view: string
  alias: string
  /** schedule:<id> 或 formal */
  source: string
  from_version: number
  to_version: number
  /** 趋势画几期 */
  periods: number
  will_launch: number
  will_skip: number
}

/** POST 同一路径的结果：发起了几次、跳过了几次（同一输入已经补算过）；发起到一半被拒的原因照实交回 */
export interface BackfillResult extends BackfillPlan {
  launched: number
  skipped: number
  run_ids: string[]
  refused?: string | null
}

export interface AppVersionRow {
  version: number
  spec_hash: string
  level: AppLevel
  pins: AppPin[]
  dispositions: Record<string, Disposition>
  note: string
  published_by: string | null
  published_at: string
}

export interface AppGrant {
  principal_kind: 'user' | 'group' | 'everyone'
  principal: string
  role: GrantRole
}

/** 发布检查的一条问题（app.* / action.* / human.* / process.*） */
export interface LintIssue {
  message: string
  level: 'error' | 'warning'
  code: string
  path?: string | null
  node_id?: string | null
}

export interface PublishResult {
  ok: boolean
  version?: number
  level?: AppLevel | null
  spec_hash?: string
  issues: LintIssue[]
  /** 钉住项相对上一版升了版、需要选处置的：[{alias, from, to}] */
  needs_disposition?: { alias: string; from: number; to: number }[]
}

// ---------------------------------------------------------------------------
// 页面解析 agentlab.view/1（§3.6）
// ---------------------------------------------------------------------------

export interface Stamp {
  as_of: string | null
  as_of_text: string
  pin: string
  graph_hash: string
  calibers: { name: string; version: string }[]
  tier: 'formal' | 'degraded' | 'withheld' | null
  tier_text: string
  from: string
  sealed: boolean
  /** 照实标出的错位：定时任务已改跑新版本、应用还钉在旧版本，等 */
  notes?: string[]
}

export interface ViewSourceState {
  /** 来源的种类：按需视图（on_demand）才有「刷新」 */
  kind?: 'latest_sealed' | 'on_demand' | 'instance'
  state: 'ok' | 'empty' | 'running' | 'stale' | 'error'
  run_id: string | null
  stamp: Stamp | null
  message?: string
}

/** 一个数、一格、一个指标的出处：点开走证据接口 */
export interface ViewEvidence {
  run_id: string
  kind: 'metric' | 'cell' | 'report' | 'input' | 'query'
  eid?: string
  node?: string
  metric?: string
  seg?: string
  artifact?: string
}

export interface ViewNode {
  id: string
  type: string
  props: Record<string, any>
  /**
   * 服务端解析好的数据（backend/app/apps/resolve.py 的 _data），形状随组件而定；绑定了视图的都带 state 和可选的 message：
   * - stamp：{view, state, stamp}；metric：{state, text, name, unit, evidence}
   * - series：{state, mode: single|dual|incomparable|recompute, unit, series: [{version, label, points: [{run_id, as_of,
   *   value, text, evidence}]}], breaks: [{after_run?, from, to, text}]}
   * - table、chart：{state, columns, rows（遮罩的格子已换成「已遮罩」）, column_types, masked, total_rows, truncated, evidence}
   * - report：{state, node, doc_artifact, evidence}；field：{state, text, value?, evidence?}；status：{state, value, text}
   * - form：{form}；entry_button：{entry, label, disabled, reason?}；instance_list：{who, entry, items}
   * - instance_header：InstanceRow + {state, status_text, stamp}；instance_timeline：{state, status, status_text, steps}
   * - task_list：{scope, items}；task_form、signoffs：{state, task, task_ui, assignments?}
   * - action_status：{state, node, records: [{id, state, environment, attempt, payload_hash, error, receipt?}]}
   */
  data?: Record<string, any> | null
  children?: ViewNode[]
}

export interface ViewModel {
  format: 'agentlab.view/1'
  app: { id: string; name: string; version: number; level: AppLevel; needs_upgrade: boolean }
  page: { id: string; title: string }
  pages: { id: string; title: string }[]
  sources: Record<string, ViewSourceState>
  tree: ViewNode[]
  forms: Record<string, FormSchema>
  problems: { component: string; code: string; message: string }[]
}

export interface InstanceRow {
  run_id: string
  title: string
  entry_id: string | null
  status: string
  started_by: string | null
  created_at: string
  finished_at: string | null
  pending_tasks: number
  tier?: string | null
}

// ---------------------------------------------------------------------------
// 待办（§3.7）
// ---------------------------------------------------------------------------

export type TaskKind =
  | 'approve' | 'input' | 'edit' | 'tool' | 'code' | 'dual_check' | 'action_confirm' | 'reconcile' | 'signal'
export type TaskStatus = 'pending' | 'resolving' | 'resolved' | 'cancelled' | 'expired'
export type TaskPolicy = { kind: 'any' } | { kind: 'all' } | { kind: 'quorum'; m: number }

export interface TaskAssignment {
  id: string
  principal_kind: 'user' | 'group' | 'anyone' | 'hook'
  principal: string
  source: string
  status: 'pending' | 'responded' | 'withdrawn'
  responded_by: string | null
  responded_at: string | null
  /** 回复的决定（approve / reject / 提交的值），会签进度用 */
  decision?: string | null
  verified: boolean
}

export interface Task {
  id: string
  approval_id: string | null
  run_id: string
  node_id: string | null
  /** 节点在图上的名字 */
  node_label?: string | null
  run_class?: 'formal' | 'exploratory' | null
  workflow_id?: string | null
  app_id: string | null
  app_name?: string | null
  app_version: number | null
  workflow_name?: string | null
  kind: TaskKind
  title: string
  form: FormSchema | null
  policy: TaskPolicy
  status: TaskStatus
  due_at: string | null
  escalation_level: number
  /** 给人看的上下文：人工节点的 message / context / draft / reports，工具审批的参数，动作确认的预览等（原样来自中断内容） */
  payload: Record<string, any>
  assignments: TaskAssignment[]
  /** 当前用户能不能回复这一条 */
  can_respond: boolean
  outcome: Record<string, any> | null
  resolved_by: string | null
  resolved_at: string | null
  created_at: string
}

export interface TaskRespond {
  decision: 'approve' | 'reject' | 'submit' | 'confirmed' | 'not_applied_resend' | 'not_applied_abandon'
  values?: Record<string, any>
  note?: string
  /** 动作确认：确认卡上显示的哈希，原样带回 */
  payload_hash?: string
  /** 核实「已生效」时填的外部单号 */
  external_id?: string
}

// ---------------------------------------------------------------------------
// 动作（§3.8）
// ---------------------------------------------------------------------------

export type ActionState = 'pending' | 'sent' | 'confirmed' | 'failed' | 'unknown' | 'simulated'

export interface ActionRecordRow {
  id: string
  run_id: string
  node_id: string
  idem_key?: string
  endpoint_id?: string | null
  request_at?: string | null
  response_status?: number | null
  lookup_attempts?: number
  confirm_task_id?: string | null
  workflow_id?: string | null
  workflow_version?: number | null
  capability_version?: number | null
  app_id: string | null
  capability: string
  environment: 'prod' | 'test' | 'simulated'
  state: ActionState
  attempt: number
  payload_hash: string
  preview: Record<string, any> | null
  receipt: Record<string, any> | null
  error: string | null
  resolution: Record<string, any> | null
  created_at: string
  updated_at: string
}

// ---------------------------------------------------------------------------
// 能力与端点（§3.9）
// ---------------------------------------------------------------------------

export type CapabilityEffect = 'read' | 'write' | 'dynamic'

export interface CapabilityRow {
  name: string
  system: string
  title: string
  description: string
  params_schema: Record<string, any>
  returns_schema: Record<string, any> | null
  tabular: { rows: string; columns: Record<string, string> } | null
  effect_system: CapabilityEffect
  effect: CapabilityEffect | null
  approval: 'inherit' | 'confirm' | 'none'
  unattended_write: boolean
  fields: Record<string, Record<string, any>>
  masked: string[]
  credential_mode: 'service' | 'service_on_behalf'
  endpoint_id: string | null
  lookup: string | null
  version: number
  missing: boolean
  synced_at: string | null
  updated_by: string | null
}

export interface EndpointEnv {
  base_url: string
  /** 值只回掩码；提交掩码值表示保持原值 */
  headers: Record<string, string>
  allow_intranet: boolean
  verify_tls: boolean
}

export interface EndpointRow {
  id: string
  name: string
  description: string
  environments: { test?: EndpointEnv; prod?: EndpointEnv }
  idempotency_header: string
  timeout_s: number
  version: number
}

// ---------------------------------------------------------------------------
// 通知（§2.4）
// ---------------------------------------------------------------------------

export interface NotifyChannelRow {
  id: string
  name: string
  scope: 'global' | 'app' | 'user'
  owner_id: string | null
  format: 'generic' | 'feishu' | 'wecom' | 'dingtalk'
  /** 只回主机名 */
  url_host: string
  signed: boolean
  events: string[]
  enabled: boolean
}

// ---------------------------------------------------------------------------
// 运行来源（§2.10 X3）与助手查看（§2.9 L2）
// ---------------------------------------------------------------------------

export type OriginKind =
  | 'chat' | 'canvas' | 'launcher' | 'schedule' | 'app_entry' | 'app_view' | 'app_backfill' | 'hook' | 'api'

export interface LookObject {
  kind: 'table' | 'capability' | 'workflow_version'
  source?: string
  /** 点名了不在这一轮范围里的数据源时为 null，missing 里是点名的表 */
  source_id?: string | null
  named: string[]
  picked?: string[]
  joined?: string[]
  missing: string[]
  total?: number
  explored?: number
  catalog_tables?: number
  schema_synced_at?: string | null
  catalog_versions?: Record<string, number>
  versions?: Record<string, number>
}

export interface LookEvent {
  op: 'look'
  id: string
  by: 'model' | 'system'
  need: string
  status: 'ok' | 'index' | 'failed'
  reason?: string
  elapsed_ms: number
  objects: LookObject[]
}

// ---------------------------------------------------------------------------
// 入站钩子（§2.4）与助手流（§2.9 L1、§3.11）
// ---------------------------------------------------------------------------

/** 钩子管理里的一行（D-g）：草稿或发布版里声明的 ∪ 已经生成过密钥的，按名字一行 */
export interface AppHookRow {
  /** app_hooks 那一行的 id；还没生成密钥时为 null */
  id: string | null
  name: string
  kind: 'start' | 'signal' | null
  entry_id: string | null
  enabled: boolean
  has_secret: boolean
  /** 草稿的 spec.hooks 里有这个名字 */
  in_draft: boolean
  /** 当前发布版里有（入站请求按发布版认种类和入口，没有就拒收） */
  in_published: boolean
  /** 收请求的地址 /api/hooks/{id}；还没生成密钥时为 null */
  url_path: string | null
  created_at: string | null
  /** 只在生成、轮换的回包里出现一次 */
  secret?: string
}

/** 助手问澄清问题：这一轮就此结束（已经改了的，后面跟 final，autorun 为 false） */
export interface AskEvent {
  op: 'ask'
  question: string
}

/** POST /api/copilot/app-stream 的请求体；流里的应用操作就是 app_add / app_update / app_remove / app_move / app_page / app_set */
export interface AppStreamRequest {
  instruction: string
  app_id?: string
  spec?: AppSpec
  bindings?: AppBindings
  conversation_id?: string
  provider?: string
  model?: string
}

export interface AppStreamFinal {
  op: 'final'
  spec: AppSpec
  issues: LintIssue[]
  explanation: string
}

// ---------------------------------------------------------------------------
// 能力字段语义的审阅（D-i，GET /api/capabilities/review-queue、POST /api/capabilities/review/batch）
// ---------------------------------------------------------------------------

export interface CapabilityReviewRow {
  capability: string
  system: string
  title: string
  effect: CapabilityEffect
  /** 返回里的位置（JSON 指针） */
  pointer: string
  /** label / meaning / unit / measure / codes */
  field: string
  field_text: string
  /** 服务端写好的位置说法（「返回字段 /amount 的单位」） */
  field_label: string
  value: unknown
  display: string
  source: string
  status: string
  note: string | null
  priority: number
  reasons: string[]
  /** 用到这个能力的运行次数 */
  runs: number
  /** 能力的治理版本：成批审阅时带上（乐观锁） */
  version: number
}

export interface CapabilityReviewOutcome {
  capability: string
  pointer: string
  field: string
  ok: boolean
  version: number | null
  error: string | null
  code: string | null
}
