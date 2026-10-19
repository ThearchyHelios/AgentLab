/**
 * 数据源的变化、取值观察、定时维护、后台任务条的接口约定（docs/design/next-round/03-数据接入与目录维护.md B2–B4、05 (g)）。
 * 和后端 app/api/source_changes.py、app/data/source_changes.py（agentlab-change/1）、app/data/catalog_profile_observe.py 一一对应。
 *
 * 能不能做、依据什么、现在怎样都由服务端给（actions、basis_text、affects.catalog[*].now），界面只照写，不自己判。
 */
import type { Schedule, ScheduleFire } from './types'

// ---------------------------------------------------------------- 变化记录 agentlab-change/1

export type SourceChangeSeverity = 'info' | 'warning' | 'action'
export type SourceChangeStatus = 'open' | 'acknowledged' | 'resolved' | 'dismissed'
/** 结构变化 / 取值变化（叫法分开，和已有的「目录有变化」也分开，总方案 X10） */
export type SourceChangeCategory = 'struct' | 'value'
/** 清理目录项、加入码值、重新起草、重新剖析、知道了、误报（必须写理由） */
export type SourceChangeAction = 'clean_catalog' | 'add_codes' | 'redraft' | 'reprofile' | 'acknowledge' | 'dismiss'
/** 列表的状态筛选：开着的（含知道了还没处理的）、处理过的（解决了的、标为误报的） */
export type SourceChangeStatusFilter = 'pending' | 'handled'

/** 影响面里说到这些列的一个目录项（测到时的状态），now 是它现在的状态：清理过、不在目录里了为 null */
export interface SourceChangeCatalogRef {
  table: string
  path: string
  label: string
  status: string | null
  source: string | null
  /** 人工确认过的项还说着被删的列 */
  needs_action?: boolean
  now: { status: string | null; source: string | null } | null
}

export interface SourceChangeTemplateNode {
  node_id: string
  label: string | null
  type: string
  impact: 'direct' | 'possible'
  /** 这条写死的 SQL 现在会报列不存在 */
  breaks: boolean
  unknown_columns?: string[]
}

/** 已发布模板里用到这张表的一个 */
export interface SourceChangeTemplate {
  workflow_id: string
  name: string
  version: number
  level: string
  impact: 'direct' | 'possible'
  nodes: SourceChangeTemplateNode[]
}

export interface SourceChangeAffects {
  catalog: SourceChangeCatalogRef[]
  templates: SourceChangeTemplate[]
  /** 有效记录条件用到了这一列 */
  rules_touched: boolean
  /**
   * 表结构变了之后口径没能试算的那几版（服务端 ledger/recall._probe_skipped）：缺哪几个参数、原因原话、参数本来从哪来。
   * 只记没试算的；试算了的结论在召回待办里
   */
  probes?: SourceChangeProbe[]
}

export interface SourceChangeProbe {
  workflow_id: string
  version: number
  name: string
  outcome: 'skipped'
  missing: string[]
  reason: string
  /** formal 最近一次正式运行 / answer 等价的问数回答 / defaults 入口的缺省值 */
  inputs_from?: string
}

/** 现在能做的一个处理动作；ok 为假时 reason 写为什么做不了 */
export interface SourceChangeActionOption {
  action: SourceChangeAction
  ok: boolean
  reason: string | null
}

export interface SourceChangeResolution {
  by: string | null
  at: string
  action: SourceChangeAction
  note: string | null
  catalog_version?: number
  /** 别的表（指向它的关联关系）也动了的，各表的目录版本 */
  catalog_versions?: Record<string, number>
  job_id?: string
}

/** 谁、经什么测到的 */
export interface SourceChangeDetected {
  job_kind: 'introspect' | 'profile' | 'on_demand' | string
  job_id: string | null
  task_seq: number | null
  trigger: 'manual' | 'schedule' | 'on_demand' | string
  actor: string | null
  fire_at?: string
}

export interface SourceChange {
  schema: string
  seq: number
  id: string
  source_id: string
  source_name: string
  table: string | null
  column: string | null
  kind: string
  category: SourceChangeCategory
  severity: SourceChangeSeverity
  /** 服务端给人看的一句（「orders.amount：删除了列」） */
  summary: string
  detected_at: string
  last_seen_at: string
  seen_count: number
  detected_by: SourceChangeDetected
  basis: { method?: string; key?: string; query_refs?: number[] } | null
  /** 取值变化依据的口径（整表 / 窗口 / 块采样 / 前 N 行……）；结构变化为 null（依据是前后两版结构） */
  basis_text: string | null
  before: any
  after: any
  affects: SourceChangeAffects
  struct_version_before: number | null
  struct_version_after: number | null
  status: SourceChangeStatus
  resolution: SourceChangeResolution | null
  /** 现在能做的处理动作（处理过的为空） */
  actions: SourceChangeActionOption[]
  /** 处理时建的任务（重新起草、重新剖析） */
  job: { kind: 'draft' | 'profile'; id: string } | null
}

export interface SourceChangeCounts {
  /** 开着的和知道了还没处理的 */
  pending: number
  open: number
  by_severity: Record<SourceChangeSeverity, number>
}

export interface SourceChangeList {
  items: SourceChange[]
  total: number
  counts: SourceChangeCounts
}

export interface SourceChangeQuery {
  status?: SourceChangeStatusFilter
  category?: SourceChangeCategory | null
  table?: string | null
  limit?: number
  offset?: number
}

export interface SourceChangeResolveBody {
  action: SourceChangeAction
  /** 误报必须写 */
  note?: string
  /** 加入码值：{码值: 含义}，含义可以空 */
  codes?: Record<string, string>
}

export interface SourceChangeResolveOut {
  change: SourceChange
  catalog_version?: number
  catalog_versions?: Record<string, number>
  job?: { kind: 'draft' | 'profile'; id: string }
}

// ---------------------------------------------------------------- 取值观察

export type ObservationKind = 'rows' | 'codes_observe' | 'freshness' | 'daily_volume'

/** 一次观察。data 的形状按种类：rows {rows, at_least, method}、codes_observe {values: [码值, 行数, 占比][], rows, truncated}、
 * freshness {max}、daily_volume {since, until, days: [日期, 行数][], total, days_with_data} */
export interface ObservationPoint {
  at: string | null
  basis_key: string
  /** 口径的说法（整表、业务日期 x 最近 35 天、块采样约 1%、按存储顺序取前 N 行……） */
  basis_text: string
  data: any
}

export interface ObservationSeries {
  kind: ObservationKind
  /** 行数是空串 */
  column: string
  /** 新的在前 */
  points: ObservationPoint[]
  /** 这几次的口径不全相同：口径不同不比 */
  basis_changed: boolean
}

export interface TableObservations {
  table: string
  series: ObservationSeries[]
}

// ---------------------------------------------------------------- 定时维护

export type MaintenanceStep = 'introspect' | 'profile_needed' | 'observe'

/** 一次维护触发的 inputs：建了哪些任务、哪几步没执行和原因 */
export interface MaintenanceFireInputs {
  steps?: MaintenanceStep[]
  jobs?: { step: string; kind: 'introspect' | 'profile'; id: string; tables?: number; scope?: string }[]
  skipped?: { step: MaintenanceStep; reason: string }[]
}

export interface MaintenanceSchedule extends Omit<Schedule, 'last_fire'> {
  last_fire: (ScheduleFire & { inputs?: MaintenanceFireInputs }) | null
}

export interface SourceMaintenance {
  steps: MaintenanceStep[]
  profile_enabled: boolean
  /** 上传表格、记录表不维护 */
  maintained: boolean
  /** 调度循环开着没有：关着时到点不会触发 */
  scheduler_on: boolean
  schedules: MaintenanceSchedule[]
}

/** 数据源列表一次取齐的：要处理的变化条数（角标）、指向它的维护定时 */
export interface SourceUpkeep {
  source_id: string
  changes: SourceChangeCounts
  maintenance: { id: string; enabled: boolean; cron: string; timezone: string; description_text: string; next_run_at: string | null }[]
}

// ---------------------------------------------------------------- 后台任务条

export type SourceJobKind = 'introspect' | 'draft' | 'profile'

/** 这个源进行中和排队的一个后台任务，按同源队的先后排好 */
export interface SourceBackgroundJob {
  kind: SourceJobKind
  id: string
  status: string
  total: number
  done: number
  /** 排在谁后面（draft / profile / introspect / import / sync_draft） */
  waiting_for: string | null
  /** 在同源队里的位置（0 是正在做的）；还没排进队为 null */
  position: number | null
}
