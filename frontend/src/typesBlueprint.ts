/**
 * 普通用户建应用的接口约定（docs/design/normal-user-apps.md 第三部分）：蓝图、场景模板、试运行、发布单、文件、记录。
 * 和后端 app/blueprints/spec.py、app/db/models_blueprints.py 一一对应。改这里要同时改后端（骨架阶段冻结）。
 */
import type { AppSpec, LintIssue, ViewModel } from './typesApp'

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

export interface JointState { status: 'filled' | 'pending' | 'refused'; by: 'template' | 'assistant' | 'user'; note?: string | null }

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
/** 检查问题加上白话（后端 app/plain.with_plain）；没登记白话的 plain 为空，照旧显示 message */
export type PlainIssue = LintIssue & { plain?: Plain | null }

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

export interface TrialStart { run_id: string; blueprint: Blueprint }
/** 试运行解析出来的一页：和应用页同一个视图模型，印章 trial=true */
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
  options: { first_run?: boolean }
}

// ---------------------------------------------------------------- 文件、记录

/** 上传后拿回的引用，作为表单值提交 */
export interface FileRef { file_id: string; name: string; size: number; sha256: string; type: string }

export interface RecordRow {
  _id: string
  _seq: number
  _submitted_by: string | null
  _submitted_at: string
  _supersedes: string | null
  /** 被更正过（有更新的一行指向它） */
  superseded?: boolean
  values: Record<string, unknown>
}
export interface RecordsPage { table: string; version: number; columns: { name: string; label: string; type: string }[]; items: RecordRow[]; mine_only: boolean }

// ---------------------------------------------------------------- 页面编辑

/** 「加一张数据卡片」的候选（GET /api/apps/{id}/candidates） */
export interface AppCandidate { kind: 'metric' | 'report' | 'table' | 'series'; label: string; group: string; component: AppSpec['pages'][number]['body'][number] }
