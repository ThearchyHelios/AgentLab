/**
 * 普通用户建应用的接口约定（docs/design/normal-user-apps.md 第三部分）：蓝图、场景模板、试运行、发布单、文件、记录。
 * 和后端 app/blueprints/spec.py、app/db/models_blueprints.py 一一对应。改这里要同时改后端（骨架阶段冻结）。
 */
import type { AppComponent, Disposition, LintIssue, LookEvent, ViewModel } from './typesApp'

// ---------------------------------------------------------------- 蓝图 agentlab.blueprint/1

export type ItemSource = 'user' | 'assistant' | 'template' | 'catalog' | 'caliber'
export type ItemStatus = 'confirmed' | 'proposed' | 'missing'

/** 卡片上的一项：值、来源、状态，可选一句「为什么」 */
export interface BpItem<T = unknown> {
  value: T | null
  source: ItemSource
  status: ItemStatus
  why?: string | null
}

export interface BpDataItem {
  id: string
  label: BpItem<string>
  unit: BpItem<string>
  /** 对到 {source, table} */
  table: BpItem<{ source: string; table: string }>
  measure: BpItem<string>
  date: BpItem<{ column: string; rule?: string }>
  filter: BpItem<string>
  grain: BpItem<string>
  /** 复用已发布的口径卡 */
  reuse?: { workflow_id: string; version: number; node_id: string; metric_id: string } | null
}

export interface BpPrincipal { kind: 'user' | 'group' | 'slot' | 'initiator'; name: string }

export interface BpStep {
  id: string
  label: BpItem<string>
  who: BpItem<BpPrincipal[]>
  policy: BpItem<{ kind: 'any' | 'all' | 'quorum'; m?: number }>
  due: BpItem<string>
  on_timeout: BpItem<{ action: 'remind' | 'escalate' | 'reject'; to?: BpPrincipal[] }>
  threshold: BpItem<{ field: string; op: string; value: number | string }>
}

export interface BpField { name: string; spec: BpItem<Record<string, any>>; sensitive: boolean }

export interface BpWriteback {
  capability: BpItem<string>
  mapping: BpItem<Record<string, string>>
  confirm: BpItem<boolean>
}

export interface BpAccess {
  use: { kind: string; name: string }[]
  observe: { kind: string; name: string }[]
  manage: { kind: string; name: string }[]
}

export interface BpAsk { id: string; question: string; options: string[]; path?: string | null; answer?: string | null }

export interface BlueprintSpec {
  format: 'agentlab.blueprint/1'
  purpose: BpItem<string>
  template: { id: string; version: number } | null
  data: BpDataItem[]
  cadence: BpItem<{ preset: 'daily' | 'weekly' | 'monthly'; weekday?: number; day?: number; time: string; tz: string; period?: string }>
  people: BpStep[]
  fields: BpField[]
  writeback: BpWriteback | null
  access: BpAccess
  asks: BpAsk[]
}

export type BpOpName = 'bp_set' | 'bp_add' | 'bp_remove' | 'bp_ask' | 'bp_template'
export interface BpOp {
  op: BpOpName
  /** JSON 指针（/data/0/table） */
  path?: string | null
  value?: unknown
  question?: string | null
  options?: string[]
  template?: { id: string; version: number } | null
}

export type BlueprintScope = 'personal' | 'shared'
export type BlueprintStatus = 'drafting' | 'trialed' | 'published' | 'personal' | 'in_review'
export type BlueprintSourceKind = 'conversation' | 'template' | 'run' | 'workflow_version'

/** 关节的类型（服务端 blueprints/templates.JOINT_KINDS）：sql、metric_expr 交给助手填（生成关节），其余按卡片确定性地填 */
export type JointKind =
  | 'sql' | 'metric_expr' | 'fields' | 'assignee' | 'policy' | 'due' | 'on_timeout' | 'threshold' | 'capability' | 'contract'

/**
 * 一个关节的状态（服务端 blueprints/store.blueprint_out，只给状态，不给填的值——值在图上看）。by：确定性关节和还没填的
 * 生成关节是 template，助手填上的是 assistant。label、kind、group 是模板声明的说法、类型和分组（同一组的关节一起交给
 * 助手填，组之间并行）；实例化之前存下的关节没有这三项
 */
export interface JointState {
  status: 'filled' | 'pending' | 'refused'
  by: 'template' | 'assistant' | 'user'
  note?: string | null
  label?: string
  kind?: JointKind
  group?: string | null
}

export interface Blueprint {
  id: string
  name: string
  owner: string | null
  scope: BlueprintScope
  status: BlueprintStatus
  spec: BlueprintSpec
  template_id: string | null
  template_version: number | null
  source: { kind: BlueprintSourceKind; id?: string | null; version?: number | null }
  conversation_id: string | null
  workflow_id: string | null
  app_id: string | null
  last_trial_run_id: string | null
  graph_hash: string | null
  joints: Record<string, JointState>
  copied_from: string | null
  /** 蓝图检查（bp.* / tpl.*），带白话 */
  issues?: PlainIssue[]
  created_at: string
  updated_at: string
}

// ---------------------------------------------------------------- 白话层

export type ProblemWho = 'you' | 'assistant' | 'admin'
export interface Plain { title: string; why: string; who: ProblemWho; action: string }
/** 发布单的问题出自哪一项预检（服务端 blueprints/publish_plan._preflight） */
export type PlanProblemSource = 'blueprint' | 'plan' | 'workflow' | 'schedule' | 'app' | 'grants'
/**
 * 检查问题加上白话（后端 app/plain.with_plain）；没登记白话的 plain 为空，照旧显示 message。
 *
 * 发布单上的问题另带（blueprints/publish_plan._with_who）：who 归谁处理（摘要按它分「你来定 / 助手可以修 / 要找管理员」三栏，
 * 没登记白话的也有）；source 出自哪一项预检；工作流检查带 fix_kind（自动修复的种类：choice 要人选，auto、assist 助手能做）；
 * 应用检查的 app.upgrade_policy 带 alias（要选处置的那个钉住项）。蓝图检查带 joint（还没填的关节）。
 */
export type PlainIssue = LintIssue & {
  plain?: Plain | null
  who?: ProblemWho
  source?: PlanProblemSource
  fix_kind?: 'choice' | 'auto' | 'assist' | null
  alias?: string
  joint?: string
  field?: string | null
}

// ---------------------------------------------------------------- 场景模板

export interface TemplateCard { card: string; note: string }
export interface TemplateInfo {
  id: string
  version: number
  label: string
  description: string
  applies_when: string
  cards: TemplateCard[]
  /** 个人应用能用哪一部分；不能用为 null */
  personal: string | null
  tags: string[]
}

// ---------------------------------------------------------------- 试运行

/**
 * POST /api/blueprints/{id}/trial 的回包（服务端 api/blueprints.trial）。inputs 是这次运行实际用的输入（运行的 input）：看板类
 * 补齐了按「多久更新」算出的统计期，流程类是示例表单按入口映射换成的图输入。「换一期再试」的表单不随回包给，取自按卡片
 * 生成的应用骨架（入口的示例表单，看板类是按条件查看的条件表单）
 */
export interface TrialStart {
  run_id: string
  blueprint: Blueprint
  inputs: Record<string, unknown>
}
/** 试运行、预览自己的拒绝码（服务端 blueprints/trial 的 NOT_READY、NO_GRAPH、POLICY、FORM、NO_RUN） */
export type TrialRefusalCode = 'trial_not_ready' | 'trial_no_graph' | 'trial_policy' | 'trial_form' | 'trial_no_run'
/**
 * 试运行（POST /api/blueprints/{id}/trial）、按卡片生成（/instantiate）、预览（/preview）被拒的回包（服务端
 * blueprints/trial.TrialRefused，api/blueprints 转成 {detail, code, issues?}）：
 * - 409 trial_not_ready：缺项、待填的关节挡住，issues 逐条列（带白话）；
 * - 409 trial_no_graph：蓝图还没有能运行的图（没选场景、自由形没导入图）；
 * - 403 trial_policy：出的图过不了个人应用的约束（auth/chat_policy，同问数据）。个人蓝图一律按它（任何人的，搭建者
 *   自己的也一样：试运行是刷新的预演），开了登录的使用者也一样；
 * - 422 trial_form：示例表单没通过校验；
 * - 404 trial_no_run：预览指的不是这张蓝图的试运行，或者还没试运行过；
 * - 别处的拒绝照原样透传 status 和 code：记录表起草被拒（records.*，422）、附件核对没过（files.*，404 / 403 / 422）、
 *   发起被拒（engine/launch.LaunchRefused）
 */
export interface TrialRefusal { detail: string; code?: TrialRefusalCode | (string & {}); issues?: PlainIssue[] }
/** 试运行解析出来的一页：和应用页同一个视图模型，印章 trial=true，ViewModel.trial 写对着哪次试运行 */
export type TrialPage = ViewModel

// ---------------------------------------------------------------- 发布单

export type PlanStepKind =
  | 'save_version' | 'preflight' | 'publish_workflow' | 'create_schedule' | 'create_app' | 'publish_app'
  | 'enable_schedule' | 'grant' | 'first_run'
export interface PlanStep {
  kind: PlanStepKind
  status: 'pending' | 'done' | 'failed' | 'skipped'
  /** 这一步产出的对象 id（工作流版本、定时任务、应用……） */
  result?: Record<string, unknown> | null
  error_code?: string | null
  message?: string | null
  at?: string | null
}
/**
 * 发布单（服务端 blueprints/publish_plan.plan_out）。ok：没有挡路的问题（error 级）、也没停下，能确认执行。actor 是出单的人。
 * options 是出单时定下的：first_run（发布后立即跑一次，不给时看板类为真、流程类为假）、dispositions（钉住项升版的处置）、
 * spec_hash（出单那一刻卡片内容的指纹，执行时重算，对不上回 409 plan.graph_changed；服务端自己用，界面不读）
 */
export interface PublishPlanRow {
  id: string
  blueprint_id: string
  level: 'published' | 'governed'
  graph_hash: string | null
  status: 'planned' | 'running' | 'stopped' | 'done'
  steps: PlanStep[]
  /** 白话摘要：每一步一句 */
  summary: { step: PlanStepKind; text: string }[]
  problems: PlainIssue[]
  options: { first_run?: boolean; dispositions?: Record<string, Disposition>; spec_hash?: string }
  ok: boolean
  actor: string | null
  created_at: string | null
  updated_at: string | null
}

// ---------------------------------------------------------------- 文件、记录

/** 上传后拿回的引用，作为表单值提交 */
export interface FileRef { file_id: string; name: string; size: number; sha256: string; type: string }

/**
 * 记录表的一行（服务端 records/listing.py）。系统列照 records.SYSTEM_COLUMNS：_seq 只增不减的序号，_run_id 写入它的那次运行，
 * _schema_version 写入时表单的版本；_version 是这一行在第几版表里（表单不兼容地改过之后出新一版，列表把各版合在一起列）。
 * 更正行的 _submitted_by 沿用原行的提交人（原提交人照样看得见），更正人记在动作记录和审计里。别人提交的行里遮罩列的值已
 * 换成「已遮罩」
 */
export interface RecordRow {
  _id: string
  _seq: number
  _submitted_by: string | null
  _submitted_at: string
  _run_id: string | null
  _supersedes: string | null
  _schema_version: number
  _version: number
  /** 被更正过（有更新的一行指向它）。默认只列当前有效的行，要了更正历史（history）时才会有为真的 */
  superseded: boolean
  values: Record<string, unknown>
}
/**
 * 一页记录（GET /api/apps/{id}/records、组件 records_list 的数据，服务端 records/listing.page）。mine_only：服务端只给了这个人
 * 自己提交的（use 权限，或组件写了 who=mine）。next_cursor 是下一页的游标（「版本:序号」），没有下一页为 null。state：ok 有
 * 这张表（items 可能为空）；empty 这个应用还没有记录表（table 是请求里写的、version 为 0）；preview 按草稿预览，不列真实的
 * 记录（apps/resolve._records_list）
 */
export interface RecordsPage {
  table: string | null
  version: number
  columns: { name: string; label: string; type: string }[]
  items: RecordRow[]
  mine_only: boolean
  next_cursor: string | null
  state: 'ok' | 'empty' | 'preview'
}

// ---------------------------------------------------------------- 页面编辑

/**
 * 「加一张数据卡片」的候选（GET /api/apps/{id}/candidates，服务端 apps/candidates.py）：component 可以直接 app_add——绑定的
 * 视图一定在这份草稿的 views 里（候选只从草稿里有图的视图列，实例视图不列），id 在草稿里不重名。草稿里已经有一模一样的
 * 不再列。group 是出自哪个节点（草稿里不止一个视图时加上来源的说法）
 */
export interface AppCandidate { kind: 'metric' | 'report' | 'table' | 'series'; label: string; group: string; component: AppComponent }

// ---------------------------------------------------------------- 助手流（蓝图对话、填关节）

/**
 * 两条流共有的过程事件（服务端 api/copilot_gen.Generation、api/copilot._with_heartbeat）：用的哪个模型、心跳（阶段和已用时长）、
 * 思考、开场的打算（plan）、收尾的说明（done）、查看（look，同问数据）、自查（check：repairing 交回模型改、error 修不成、
 * passed / failed 交付前的结论）、出错（error，这一轮没完成）
 */
export type CopilotStreamStep =
  | { op: 'model'; model: string }
  | { op: 'heartbeat'; phase: string; elapsed_ms: number }
  | { op: 'thinking'; delta: string }
  | { op: 'plan'; summary: string }
  | { op: 'done'; explanation?: string }
  | Omit<LookEvent, never>
  | { op: 'check'; status: 'repairing' | 'error' | 'passed' | 'failed'; round?: number; repaired?: number;
      issues?: LintIssue[]; message?: string; detail?: string }
  | { op: 'error'; message: string; hint?: string | null; detail?: string | null }

/**
 * 蓝图对话落库的一轮的 meta（ConversationTurn.meta，服务端 api/copilot_blueprint 的 _close_turn）：这一轮查看了什么（looks，
 * 同流里的 look 事件）、模型调了几次（calls）、问回来的那一句和候选（asked，没问为空）。之前的轮次只重放这些，改动已经落在
 * 卡片上
 */
export interface BlueprintTurnMeta {
  looks?: LookEvent[]
  calls?: number
  asked?: { question: string; options: string[] } | null
}

/**
 * 蓝图对话（POST /api/copilot/blueprint-stream，服务端 api/copilot_blueprint.blueprint_stream）的事件：模型提的 bp_* 操作（服务端
 * 已经落到它维护的那一份上，来源记 assistant）；bp_ask 问回来一句（模型写成通用的 ask 也认），这一轮就此结束；reply 只回答、
 * 不改卡片；最后 final 带存好的整份蓝图（以它为准）、这一轮的说明、没采纳的操作（issues，警告级）和问了什么（asked）
 */
export type BlueprintStreamEvent =
  | CopilotStreamStep
  | { op: 'bp_set' | 'bp_add' | 'bp_remove'; path: string; value?: unknown }
  | { op: 'bp_template'; template: { id: string; version: number } | null }
  | { op: 'bp_ask' | 'ask'; question: string; options?: string[]; path?: string | null; id?: string }
  | { op: 'reply'; text: string }
  | { op: 'final'; blueprint: Blueprint; explanation: string; issues: LintIssue[];
      asked?: { question: string; options: string[] } }

/**
 * 填关节（POST /api/copilot/fill-joints-stream，服务端 api/copilot_blueprint.fill_joints_stream）的事件。不同组并行，组里的过程
 * 事件都带 group；group 事件报一组的进展（started 开始、retrying 并行出错退回串行再试、filled 填好、failed 没填上，带没过的
 * 检查或原因）；fill_joint 是模型填了一个关节。最后 final 带存好的蓝图、各组的结局（以它为准：填的时候卡片又改过的组不算数）
 * 和实例化查出的问题（带白话）
 */
export type FillJointsEvent =
  | (CopilotStreamStep & { group?: string })
  | { op: 'group'; group: string; status: 'started' | 'retrying' | 'filled' | 'failed'; joints?: string[];
      issues?: LintIssue[]; message?: string }
  | { op: 'fill_joint'; joint: string; value: string; group?: string }
  | { op: 'final'; blueprint: Blueprint; problems: PlainIssue[]; group?: undefined;
      groups: Record<string, { status: 'filled' | 'failed'; calls: number; refusals: LintIssue[] }> }
