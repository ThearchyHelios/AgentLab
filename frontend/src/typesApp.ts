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
  /**
   * 管理员开没开「使用者可以建个人应用」（设置 apps.user_personal_apps，docs/design/normal-user-apps.md N7）。使用者读不到
   * 设置，开关随认人、登录接口一起给（服务端 api/auth.principal_full）；老服务端不给时按关
   */
  user_personal_apps?: boolean
  /**
   * 管理员开没开个人访问密钥（auth.personal_keys，docs/design/next-round/04-对外集成与触达.md A1）：关着时不是管理员的人在设置
   * 里看不到「访问密钥」（建不了，已有的个人密钥也不生效）。老服务端不给时按关
   */
  personal_keys?: boolean
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
  /** person 人；service 服务账号（只持有访问密钥，在「设置 → 访问密钥」里管）。老服务端没有这一项，按人算 */
  kind?: 'person' | 'service'
}

export interface GroupRow {
  id: string
  name: string
  label: string
  source: 'local' | 'oidc'
  members: string[]
}

/** 选人用的名录（GET /api/directory，搭建者及以上只读）：只有用户名、显示名、是否停用和组名 */
export interface Directory {
  /** service：服务账号（能拿应用授权，不能被派待办：岗位绑定、改派里不能选它） */
  users: { username: string; display_name: string; disabled: boolean; service?: boolean }[]
  groups: { name: string; label: string }[]
}

/**
 * 使用者选人的候选从哪个池取（GET /api/directory/candidates，05-已知遗留 (b)）：form 是表单里的选人控件（入口表单带 app_id、
 * entry，待办表单带 task_id，都带 field），review 是个人应用提交审核
 */
export type CandidateSource =
  | { purpose: 'form'; app_id?: string; entry?: string; task_id?: string; field: string }
  | { purpose: 'review' }

/**
 * 候选池给的那一份：只有用户名和显示名（不给是否停用、角色、邮箱，停用的不列）。needs_query：应用授权给了所有人，
 * 不输入开头不列；truncated：符合的超过了上限（最多 50 位）
 */
export interface Candidates {
  users: { username: string; display_name: string }[]
  groups: { name: string; label: string }[]
  truncated: boolean
  needs_query?: boolean
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
  /** 文件（N9）：字段 type 为 object，值是上传后拿回的引用 FileRef（typesBlueprint.ts） */
  | 'file'

/** 文件控件的限制（x-file，服务端 apps/forms._file_field_problems）：可选的类型（扩展名或 MIME）、大小上限（MB） */
export interface FileLimits {
  accept?: string[]
  max_mb?: number
}

export interface FormField {
  /** object 只有文件控件能用（x-widget 为 file，服务端 apps/spec.FILE_FIELD_TYPE），值是 FileRef */
  type: 'string' | 'number' | 'integer' | 'boolean' | 'array' | 'object'
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
  /** 文件控件的限制，只配 x-widget: file */
  'x-file'?: FileLimits
  /**
   * 选人控件的候选池，只配 x-widget: principal：app（能用这个应用的人，缺省）、group:<组>、slot:<岗位>
   * （服务端 auth/directory.pool_problem，05-已知遗留 (b)）
   */
  'x-principal'?: { pool: string }
}

export interface FormSchema {
  format?: 'agentlab.form/1'
  type: 'object'
  title?: string
  required?: string[]
  properties: Record<string, FormField>
  'x-order'?: string[]
}

/**
 * 前后端同一种错误格式（服务端 apps/spec.FORM_ERROR_CODES）。file：文件控件的值不是上传后拿回的引用、类型或大小不合
 * x-file 的限制，或提交时核对引用没过（不是这个人传的、已经用过）；principal：开了登录时选人控件的值不在候选池里
 * （服务端 auth/directory.principal_errors，05-已知遗留 (b)）
 */
export interface FormError {
  path: string
  code: 'required' | 'type' | 'enum' | 'minimum' | 'maximum' | 'min_length' | 'max_length' | 'max_items' | 'format' | 'file' | 'principal'
  message: string
}

// ---------------------------------------------------------------------------
// 应用描述 agentlab.app/1（§3.2、§3.4、§3.5）
// ---------------------------------------------------------------------------

export type PinUse = 'view' | 'entry' | 'process'
export type Disposition = 'recompute' | 'dual' | 'incomparable'

export interface AppPin {
  alias: string
  /** 共享应用钉工作流版本；个人应用（docs/design/normal-user-apps.md N7）写 blueprint，workflow_id 为空 */
  workflow_id: string | null
  /** 还没发布（蓝图试运行时派生的草稿）、或个人应用时为空 */
  version: number | null
  graph_hash: string
  level?: string | null
  use: PinUse
  blueprint?: string | null
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
  /** exploratory 只在个人应用里合法：最近一次成功封存的探索运行（N7） */
  kind: 'latest_sealed' | 'on_demand' | 'instance' | 'exploratory'
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
  /** 作者隐藏了它（N6）：解析时跳过，编辑状态下画成半透明 */
  hidden?: boolean
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
  /**
   * 属性的 JSON Schema（服务端 apps/catalog.py）。给人看的文字标了 `x-text: true`（页面编辑「改文字」只列这几项，N6）；
   * 写给解析器的字符串（列名、换算）不标。注解不参与校验
   */
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
  /**
   * 当前用户的授权（关登录时为 manage）。为 null 的只有一种：开了登录的管理员看别人的个人应用（服务端 api/apps._personal_stub，
   * 列表、详情、归档的回包同一份）——只有名称、拥有者、状态、创建和更新时间，能归档；说明、页面、运行、蓝图一概不给（N7）
   */
  my_role: GrantRole | null
  created_at: string | null
  updated_at: string
  /** 个人应用（N7）：只有拥有者看得见、跑探索运行；老服务端不给时按 shared */
  scope?: 'shared' | 'personal'
  owner?: string | null
  blueprint_id?: string | null
}

/**
 * GET /api/apps（scope=shared|personal 只列那一种）：items 是这个人看得到的应用；can_create 是他能建哪种应用（共享要搭建者及
 * 以上，个人看管理员的开关），和认人接口的 user_personal_apps 出自同一条规则（服务端 blueprints/personal.allowed）
 */
export interface AppList {
  items: AppSummary[]
  can_create: { shared: boolean; personal: boolean }
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
  /** 个人应用给拥有者的那一块（服务端 blueprints/personal.personal_state）：只在拥有者取个人应用的详情时给 */
  personal?: PersonalState
  /**
   * 草稿（连同岗位绑定）和当前发布版是否一致（服务端 api/apps._draft_changed，按发布时的规整方式比，normal-user-apps N6）。
   * 只给 manage，而且只在有发布版、发布版规整得了时给；没给的是不知道：界面只在这一次改过之后说「有未发布的改动」，
   * 不在浏览器里逐项比
   */
  draft_changed?: boolean
}

/**
 * 个人应用的状态（N7、§4.6）：能不能刷新、提交审核，今天还能刷新几次（和按需视图共用每日上限，按人计）、蓝图改过没有、
 * 审核到哪了。blueprint_status 同 typesBlueprint.BlueprintStatus；蓝图被删了为 null
 */
export interface PersonalState {
  /**
   * 这个人现在能不能刷新、提交审核（服务端 blueprints/personal.allowed：开了登录的搭建者及以上随时能，使用者和关登录时看
   * 管理员的开关）。为假时个人应用照样看得到，按钮置灰
   */
  allowed: boolean
  remaining_today: number
  daily_limit: number
  blueprint_status: import('./typesBlueprint').BlueprintStatus | null
  /** 蓝图在存成个人应用之后又改过（图的哈希对不上）：刷新按存下的那一版跑，要用改过的得回蓝图重新存 */
  graph_changed: boolean
  /** 最近一条审核待办；没提交过审核为 null */
  review: PersonalReview | null
}

/** 个人应用最近一条审核待办：status 是待办的状态，decision 是结论（published 搭建者发布了副本，reject 退回） */
export interface PersonalReview {
  task_id: string
  status: TaskStatus
  decision: 'published' | 'reject' | null
  /** 退回的原因（没写为空串） */
  note: string
  /** 发布出来的正式应用（decision=published 时）；别的时候为 null */
  app_id: string | null
  /** 派给了谁（没撤回的那几份） */
  to: { kind: TaskAssignment['principal_kind']; name: string }[]
  resolved_at: string | null
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
  /** 蓝图的试运行（N4）：来源写「试运行」、说明里写这不是正式结果。不是试运行时不给 */
  trial?: boolean
  /** 照实标出的错位：定时任务已改跑新版本、应用还钉在旧版本，等 */
  notes?: string[]
}

export interface ViewSourceState {
  /** 来源的种类：按需视图（on_demand）才有「刷新」；试运行解析时为 trial，个人应用为 exploratory */
  kind?: 'latest_sealed' | 'on_demand' | 'instance' | 'exploratory' | 'trial'
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
   * 作者隐藏了它（N6）：只在按草稿解析（编辑状态，draft=1）时下发并带上这个标记，界面画成半透明；发布版、个人应用的
   * 使用面不下发隐藏的组件
   */
  hidden?: boolean
  /**
   * 分享链接里（resolve_page 的 share 旁路）被换成说明块的组件原来是什么（entry_button、task_list、report……）：type 已经是
   * notice，props.text 写着为什么不显示。别处没有这一项
   */
  share_placeholder?: string
  /**
   * 服务端解析好的数据（backend/app/apps/resolve.py 的 _data），形状随组件而定；绑定了视图的都带 state 和可选的 message：
   * - stamp：{view, state, stamp}；metric：{state, text, name, unit, evidence}
   * - series：{state, mode: single|dual|incomparable|recompute, unit, series: [{version, label, points: [{run_id, as_of,
   *   value, text, evidence, backfill?}]}], breaks: [{after_run?, from, to, text}], note?, trial?}——version 是固定的工作流
   *   版本号；试运行预览里只有这一次试运行一个点，version 为 null、trial 为真、note 照实写没有趋势；钉住项还没有版本号
   *   （试运行派生的草稿）时 state=empty、message 说明
   * - table、chart：{state, columns, rows（遮罩的格子已换成「已遮罩」）, column_types, masked, total_rows, truncated, evidence}
   * - report：{state, node, doc_artifact, evidence}；field：{state, text, value?, evidence?}；status：{state, value, text}
   * - form：{form}；entry_button：{entry, label, disabled, reason?}；instance_list：{who, entry, items, trial?}
   * - instance_header：InstanceRow + {state, stamp}；instance_timeline：{state, status, status_code, status_text, steps}
   * - task_list：{scope, items}；task_form、signoffs：{state, task, task_ui, handler, assignments, progress, my_response,
   *   people, signoffs_on_page?, trial?}——trial：试运行里的待办派给了试运行的人自己
   * - action_status：{state, node, records: [{id, state, environment, attempt, payload_hash, error?, note?, receipt?}]}
   * - records_list：RecordsPage（typesBlueprint.ts）：服务端取好的第一页，界面翻页再取
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
  /**
   * 蓝图的试运行预览（GET /api/blueprints/{id}/preview，N4）：对着的是哪次试运行、跑到哪了。应用页上不给。
   * 试运行没有应用版本，app.version 为 0。form 是「换一期再试」填的表单（服务端 blueprints/trial.retry_form，按这次试运行
   * 对应的草稿：流程类、填报登记是入口的示例表单，看板类是按条件查看的条件表单）；没有为 null，只能原样再试一次
   */
  trial?: { run_id: string; status: string; status_text: string; form?: FormSchema | null }
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
  /** 给人看的状态（服务端 apps/resolve.instance_state）：status_text 说办到哪了、结论是什么（「已退回」「等待核实写入
   *  结果」「「付款」未确认（被驳回）」），status_code 是状态标记画哪种（运行状态那几种，加 held） */
  status_code?: string
  status_text?: string
}

// ---------------------------------------------------------------------------
// 待办（§3.7）
// ---------------------------------------------------------------------------

/**
 * 待办的种类。review 是个人应用提交审核（docs/design/normal-user-apps.md N7）：不挂在运行的中断上，同意不在待办上点——
 * 处理人取一份共享副本、走发布单（发布单做完这条自动记为已发布），不发布就退回（decision=reject）；payload 见 ReviewTaskPayload
 */
export type TaskKind =
  | 'approve' | 'input' | 'edit' | 'tool' | 'code' | 'dual_check' | 'action_confirm' | 'reconcile' | 'signal' | 'review'
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

/**
 * 会签进度（服务端 process/tasks.progress，界面照着画）：voters 是会签人数（判定之后按判定那一刻的人数），升级加派的
 * 那几份不算进去——它们一人说了算，单列在 escalation 里
 */
export interface TaskProgress {
  policy: TaskPolicy['kind']
  voters: number
  need: number
  agreed: number
  rejected: number
  escalation?: {
    slot: string | null
    assignees: { kind: string; name: string }[]
    /** 升级加派的人里有人回复了：他一人的决定 */
    decided: string | null
    by: string | null
  }
}

/** 这个人在这条待办上的回复（处理过的人再打开时写「你已处理」） */
export interface TaskMyResponse {
  decision: string | null
  at: string | null
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
  /**
   * 给人看的上下文：人工节点的 message / context / draft / reports，工具审批的参数，动作确认的预览等（原样来自中断内容）；
   * 审核个人应用（kind=review）是 ReviewTaskPayload
   */
  payload: Record<string, any>
  assignments: TaskAssignment[]
  /** 当前用户能不能回复这一条 */
  can_respond: boolean
  /** 会签进度；只有批准 / 驳回这一类有 */
  progress?: TaskProgress | null
  /** 当前用户回复过的话，他的回复 */
  my_response?: TaskMyResponse | null
  /** 所属实例的标题：同一个节点开出的待办标题一样，列表里靠它分开 */
  instance_title?: string | null
  outcome: Record<string, any> | null
  resolved_by: string | null
  resolved_at: string | null
  created_at: string
  /** 待办上出现的人的显示名（用户名 → 显示名：派给谁、谁回复、谁处理）。界面写显示名、悬停给用户名；老服务端不给 */
  people?: Record<string, string>
}

/**
 * 审核个人应用的待办给处理人看的（服务端 blueprints/personal.review_payload）：提交的是哪个蓝图、哪个个人应用、谁提交的、
 * 留了什么话，取过的共享副本（审核副本归取它的那位搭建者，owner 是他）。不含个人应用页面上的数：要看数，取副本后在
 * 副本上用真数试一次。键总是都在；蓝图被删了时 name、submitted_by 为 null、copies 为空
 */
export interface ReviewTaskPayload {
  kind: 'review'
  blueprint_id: string | null
  /** 提交审核的那个个人应用 */
  app_id: string | null
  name: string | null
  purpose: string
  template_id: string | null
  submitted_by: string | null
  note: string
  copies: { id: string; owner: string | null; status: import('./typesBlueprint').BlueprintStatus; app_id: string | null }[]
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
  /** email：邮件（第五种格式，服务端 notify/mail.py，04 细案 B2） */
  format: 'generic' | 'feishu' | 'wecom' | 'dingtalk' | 'email'
  /** 只回主机名；邮件渠道是空串 */
  url_host: string
  signed: boolean
  events: string[]
  enabled: boolean
  /**
   * 邮件渠道发给谁：个人渠道是拥有者的账号邮箱（没有邮箱是空的，发不出去），全局、应用的是填的那几个收件地址。
   * Webhook 渠道没有这一项
   */
  mail_to?: string[]
}

/**
 * GET /api/notify/channels 另给的 mail：邮件服务（设置的 mail 一组）启没启用、本人的账号邮箱（关登录时为 null）。
 * 设置页据此列不列「邮件」这一格式、个人邮件渠道写「发到账号邮箱 …」。老服务端不给时按没启用
 */
export interface NotifyMailState {
  enabled: boolean
  account_email: string | null
}

/**
 * 分享链接（服务端 api/shares.py，04 细案 C2）：管理应用的人在应用编辑里建、撤销。key 只在建的时候交回一次
 * （ViewShareCreated），之后只给开头一截（prefix）认是哪一条
 */
export interface ViewShareRow {
  id: string
  prefix: string
  app_id: string
  page_id: string
  page_title: string
  /** latest：始终是最近一次封存；fixed：固定在建分享那一刻每个视图取的运行 */
  mode: 'latest' | 'fixed'
  app_version: number | null
  embed_allowed: boolean
  created_by: string | null
  created_by_name: string | null
  created_at: string | null
  expires_at: string | null
  revoked_at: string | null
  revoked_by: string | null
  open_count: number
  last_opened_at: string | null
  state: 'active' | 'revoked' | 'expired'
}

export interface ViewShareCreated extends ViewShareRow {
  key: string
  /** /s#<key> */
  path: string
  /** 拼上对外访问地址的完整链接；没设对外访问地址时为 null，界面按当前页面的地址拼 */
  url: string | null
}

export interface ViewShareList {
  /** 管理员开没开「允许建分享链接」：关着时不给新建，已有的照列、能撤销 */
  enabled: boolean
  /** 部署配没配允许嵌入分享页的站点（AGENTLAB_EMBED_ORIGINS） */
  embed_origins: boolean
  max_days: number
  items: ViewShareRow[]
}

/** 分享页（GET /api/shares/page，agentlab.share/1）：页脚要的、按分享解析的视图模型、能下载报告的那几次运行 */
export interface SharePayload {
  format: 'agentlab.share/1'
  share: { mode: 'latest' | 'fixed'; created_by: string | null; created_at: string; expires_at: string; embed_allowed: boolean }
  view: ViewModel
  downloads: { view: string; run_id: string; reports: string[]; as_of_text?: string | null }[]
}

/**
 * 登记的通知事件（GET /api/notify/events，服务端 notify/service.EVENTS，总方案 X6）。scopes 是推给哪几条路：global
 * 全局渠道自己订阅，user 相关的人自己的个人渠道，app 应用版本里的订阅；空的是登记了、还没有推送的，界面上不列
 */
export interface NotifyEventRow {
  event: string
  label: string
  scopes: ('global' | 'user' | 'app')[]
}

// ---------------------------------------------------------------------------
// 运行来源（§2.10 X3）与助手查看（§2.9 L2）
// ---------------------------------------------------------------------------

/** 运行来源（服务端 engine/origin.ORIGIN_KINDS，顺序相同）。trial：蓝图的试运行；personal_app：个人应用的刷新（都是探索运行） */
export type OriginKind =
  | 'chat' | 'canvas' | 'launcher' | 'schedule' | 'app_entry' | 'app_view' | 'app_backfill' | 'hook' | 'api'
  | 'trial' | 'personal_app'
  | 'eval' | 'ledger_issue' | 'reconcile' | 'recall'

/**
 * GET /api/runs/unlisted：同样的筛选下，记录页因为「系统派生的运行默认不列」而没列出的次数。系统派生的是评测、口径对账、
 * 口径召回（服务端 engine/origin.SYSTEM_ORIGINS）：不进使用统计，按来源点名才列
 */
export interface UnlistedRuns {
  total: number
  by_origin: Partial<Record<OriginKind, number>>
}

/** 查看的对象。caliber 是已发布口径卡上的指标（蓝图助手复用口径时看，服务端 look/calibers.py） */
export interface LookObject {
  kind: 'table' | 'capability' | 'workflow_version' | 'caliber'
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

// ---------------------------------------------------------------------------
// 大库下的查看（docs/design/next-round/03-数据接入与目录维护.md A2，第 2 波 W2-a）

export interface LookObject {
  /** 点名或挑中探查范围外的表，当场读了结构、加进了探查范围（全名） */
  added_to_scope?: string[]
  /** 候选太多、先按需求检索：交给挑选模型的表有几张 */
  prefiltered?: number
}

// ---------------------------------------------------------------------------
// 访问密钥与服务账号（docs/design/next-round/04-对外集成与触达.md A1–A7，服务端 api/access_keys.py、auth/keys.py）
// ---------------------------------------------------------------------------

/** 能申请的权限（auth/keys.SCOPES）；另有不用申请的 me */
export type AccessKeyScope = 'apps.read' | 'apps.run' | 'tasks.read' | 'tasks.respond' | 'reports.read'

/** 一把密钥现在能不能用：能用 / 已撤销 / 已过期 / 拥有者停用了 */
export type AccessKeyState = 'active' | 'revoked' | 'expired' | 'owner_disabled'

export interface AccessKeyRow {
  id: string
  name: string
  /** 明文开头一截（agl_ 加随机串前 8 位），只用来认是哪一把 */
  prefix: string
  scopes: AccessKeyScope[]
  /** 限定的应用；null 是不限应用 */
  app_ids: string[] | null
  owner: { id: string; username: string | null; display_name: string | null; kind: 'person' | 'service' | null }
  /** 个人访问密钥（拥有者是人）；false 是服务账号的 */
  personal: boolean
  state: AccessKeyState
  expires_at: string
  revoked_at: string | null
  revoked_by: string | null
  last_used_at: string | null
  last_used_ip: string | null
  /** 轮换出来的新密钥记着从哪一把换来 */
  rotated_from: string | null
  created_by: string | null
  created_at: string
}

/** 建、轮换的回包：明文 key 只在这一次出现 */
export interface AccessKeyIssued extends AccessKeyRow {
  key: string
  /** 轮换时：旧的那一把（24 小时后到期） */
  previous?: AccessKeyRow
}

export interface ServiceAccountRow {
  id: string
  username: string
  display_name: string
  disabled: boolean
  created_by: string | null
  created_at: string
  /** 没撤销的密钥有几把 */
  keys: number
}

export interface AccessKeySettings {
  /** 开没开登录：关登录时访问密钥不生效 */
  login_on: boolean
  /** 个人访问密钥的开关（管理员开，缺省关） */
  personal_keys: boolean
  admin: boolean
  can_create_personal: boolean
  scopes: AccessKeyScope[]
  /** 新建时缺省勾上的（只有读的） */
  read_scopes: AccessKeyScope[]
  /** 只给个人密钥的（回复待办） */
  personal_scopes: AccessKeyScope[]
  expires_default_days: number
  expires_max_days: number
}

/** 一把密钥的一次请求（access_key_requests）：路由模板，不是带参数的路径 */
export interface AccessKeyRequestRow {
  at: string
  method: string
  route: string
  status: number
  run_id: string | null
  ip: string | null
}

/** 接口说明（GET /api/access-keys/reference）：从门禁那张表生成 */
export interface AccessKeyReference {
  base_url: string | null
  header: string
  idempotency_header: string
  scopes: AccessKeyScope[]
  implicit_scope: string
  personal_scopes: AccessKeyScope[]
  rate_per_minute: number
  common_errors: string[]
  routes: { method: string; path: string; scope: string; summary: string; errors: string[]; idempotent: boolean }[]
}

/** 钩子最近收到的请求（GET /api/apps/{id}/hooks/{name}/receipts） */
export interface HookReceiptRow {
  idem_key: string
  status: 'processing' | 'accepted' | 'rejected'
  received_at: string | null
  run_id: string | null
  task_id: string | null
  detail: string | null
}

export interface HookReceipts {
  items: HookReceiptRow[]
  /** 最近一小时验签失败了几次（不落库，只在服务端进程里数） */
  signature_failures: number
  window_s: number
}

/**
 * 视图数据 agentlab.viewdata/1（GET /api/apps/{id}/views/{view}/data，A5）：给外部系统拉封存的数。前端暂时不用，类型照服务端
 * apps/resolve.view_data 写
 */
export interface ViewData {
  format: 'agentlab.viewdata/1'
  app: { id: string; version: number }
  view: { id: string; kind: 'latest_sealed' | 'on_demand' }
  state: 'ok' | 'empty' | 'running' | 'error'
  /** 按需视图：规整后的参数 */
  params?: Record<string, unknown>
  run_id: string | null
  manifest_hash: string | null
  stamp: Record<string, any> | null
  metrics: {
    node: string
    node_label: string
    caliber: string | null
    caliber_version: string | null
    id: string
    name: string
    unit: string
    /** 原值；遮罩的为 null */
    value: unknown
    text: string
    state: 'ok' | 'masked' | 'missing'
    evidence: { run_id: string; eid: string | null; node: string; metric: string }
  }[]
  message?: string
}
