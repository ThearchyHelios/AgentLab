import type {
  ActivateSnapshotBody, ActivateSnapshotOut, AiPreview, CatalogAssistantEvent, CatalogAssistantRequest, CatalogDetail, CatalogDraftJob,
  CatalogImpact, CatalogImportReport, CatalogList, CatalogNotes, CatalogPatchPreview, CatalogPatchSubmit, CatalogProfileOut,
  CatalogReviewAction, CatalogReviewBatchItem, CatalogReviewBatchResult, CatalogReviewKind, CatalogReviewProgress, CatalogReviewQueue,
  CatalogTableUsage, CommitOut, CurrentRecipe, EditPreview,
  EditRequest, ImportRecord,
  ManifestOut, PurgeRawBody, PurgeRawOut, QuestionAnswer, Recipe, RedraftRulesOut, RemovePeriodBody, RemovePeriodOut,
  RevokeAcceptanceBody, RevokeAcceptanceOut, SnapshotOut, Staging,
} from '../types'
import type {
  Approval, AutofixResult, Conversation, ConversationDetail, ConversationTurn, CustomTool, DataSource, EvidenceGraph,
  EvidenceAudit, EvidenceJudgeResult, EvidenceProvenance, EvidenceSegmentDetail, GraphSpec, KbDocument, MemoryItem, Provider, PublishCheck, PublishLevel, ReviewResult, Run,
  RunEvent, RunStatus, Skill, ToolChange, ToolInfo, ToolTrust, UpgradeResult, UploadDecision, UploadMixedColumn, UploadResult,
  UploadShapeReason, ValidationIssue, VarIssue, Variable, Workflow, WorkflowVersion,
} from '../types'
import {
  ApiError, BASE, NETWORK_MESSAGE, actorHeader, codeOf, del, describeDetail, failure, get, isGatewayFailure, parseBody,
  patch, post, put, qs, report, request, upload, type RequestOptions, type UploadOptions,
} from './http'
import { bundle, dispositionName } from './bundle'
import { compare } from './compare'
import { inquiry } from './inquiry'
import { schedules } from './schedules'
import { whatif } from './whatif'
import { reportExport } from './reportExport'
// 应用平台：各自的接口文件，这里只挂上去
import { apps, ledger } from './apps'
import { actions, tasks } from './tasks'
import { auth, identity } from './auth'
import { capabilities, endpoints, notify } from './capabilities'

// 请求层搬到了 http.ts（见那边的说明）。原先从这里导出的照旧从这里导出，调用方不用改；
// 请求函数也一并转出，给不在 api/ 下、又想直接发请求的地方用。api/ 下的命名空间文件请直接从 http.ts 导入
export {
  ApiError, NETWORK_MESSAGE, del, get, onConnectivity, parseBody, patch, post, put, qs, request, upload,
} from './http'
export type { ApiErrorKind, RequestOptions, UploadOptions, UploadProgress } from './http'

const isObj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v)

/** 数据目录的接口前缀：/datasources/{id}/catalog */
const catalogBase = (sourceId: string) => `/datasources/${encodeURIComponent(sourceId)}/catalog`

/**
 * 上传表格被退回、要用户先选怎么处理（422 带 decision）时，取出那份决定；别的错误返回 null。
 *
 * 只认界面会画的两种（mixed、shape），而且逐项校验形状：后端以后多一种决定、或者字段对不上，
 * 宁可当普通报错弹出那句话（detail 本身就是给人看的），也不画一个空的选择框让人照着点
 */
export function uploadDecision(e: unknown): UploadDecision | null {
  if (!(e instanceof ApiError) || e.status !== 422 || !isObj(e.decision)) return null
  const { kind, details } = e.decision as { kind?: unknown; details?: unknown }
  if (!isObj(details)) return null
  if (kind === 'mixed' && mixedColumns(details.columns)) {
    return { kind, details: { columns: details.columns as unknown as UploadMixedColumn[] } }
  }
  if (kind === 'shape' && Array.isArray(details.reasons) && details.reasons.length
      && details.reasons.every((r) => isObj(r) && typeof r.message === 'string')) {
    // 预告的混合列只是告知：形状对不上就不画，结构问题照常问
    const mixed = mixedColumns(details.mixed) ? details.mixed as unknown as UploadMixedColumn[] : undefined
    return {
      kind,
      details: {
        reasons: (details.reasons as Record<string, unknown>[]).map((r) => ({
          ...r, cells: Array.isArray(r.cells) ? r.cells.map(String) : [],
        }) as unknown as UploadShapeReason),
        ...(mixed ? { mixed, mixed_complete: details.mixed_complete !== false } : {}),
      },
    }
  }
  return null
}

/** 混合列清单的形状对不对：非空数组，每项有列名和取值数组 */
const mixedColumns = (v: unknown): boolean => Array.isArray(v) && v.length > 0
  && v.every((c) => isObj(c) && typeof c.column === 'string' && Array.isArray(c.values))

/**
 * 数据源测连接的结果。ok=false 时 error 是一句人话、hint 是怎么办、detail 是
 * 驱动的原始报错（放进「技术细节」）。
 */
export interface ConnectionTest {
  ok: boolean
  elapsed_ms?: number
  url?: string
  error?: string
  hint?: string
  detail?: string
  [key: string]: any
}

/** 模型接入测试的结果，失败时的字段同 ConnectionTest */
export interface ProviderTest {
  ok: boolean
  latency_ms?: number
  model?: string | null
  reply?: string
  usage?: Record<string, any>
  error?: string
  hint?: string
  detail?: string
  [key: string]: any
}

/**
 * 助手当前用哪个模型（GET / PUT /api/copilot/model）。provider、model 是存着的设定；存的接入不存在、已停用（或只存了
 * 模型名、提供它的接入已停用）时实际什么都用不了：effective_* 为空，error 是和生成时报的同一句。老后端不给 error
 */
export interface CopilotModel {
  configured: boolean
  provider: string | null
  model: string | null
  effective_provider: string | null
  effective_model: string | null
  error?: string | null
  /** 同一件事写在助手栏「助手使用的模型」那一格下面的说法：去向写「上方」，不把人指回这个下拉框本身。老后端不给 */
  panel_error?: string | null
}

/** 还没保存的模型接入配置。带 id 表示在编辑已有的那个，api_key 留空沿用已存的 */
export interface ProviderDraft {
  id?: string
  name?: string
  kind: string
  base_url?: string | null
  api_key?: string | null
  models?: { id: string; label?: string; context?: number; pricing?: any }[]
  default_model?: string | null
  extra?: Record<string, any>
}

/** 探查结构的预览（dry_run）：只看不存，缓存和同步时间都没动 */
export interface IntrospectPreview {
  dry_run: true
  /** 这次探的是哪个 schema，'' 是默认 schema */
  schema: string
  table_count: number
  /** 带 schema 前缀的对象全名，最多 200 个 */
  tables: string[]
  truncated: boolean
  total: number
  schema_error: string
  available_schemas: string[]
}

/** 一张表的结构化列信息（和给模型看的 detail 文本出自同一份缓存） */
export interface SchemaColumn {
  name: string
  type: string
  pk: boolean
  not_null: boolean
  comment: string | null
}

export interface TableSchema {
  table: string
  /** 给模型看的那段文本 */
  detail: string
  /** 缓存里找没找到这张表。老后端没有这个字段：只有明确的 false 才算找不到 */
  found?: boolean
  qualified: string | null
  kind: 'table' | 'view' | null
  comment: string | null
  /** 老后端没有，只有 detail */
  columns?: SchemaColumn[]
}

/** 知识库检索的一条命中 */
export interface KbHit {
  chunk_id: string
  document_id: string
  title: string
  ordinal: number
  content: string
  score: number
  signals?: Record<string, number>
  /** 向量、关键词两路各贡献了多少分，两者之和等于 score。老后端不给 */
  contrib?: { vector: number; keyword: number }
  [key: string]: any
}

export interface KbSearchResult {
  query: string
  collection: string | null
  results: KbHit[]
  /** 这次检索降级了（比如退回纯关键词）的说明 */
  degraded: string[]
  /** 实际用的混合权重 */
  alpha?: number
  [key: string]: any
}

/**
 * 后台重建索引的进度。phase：chunks（知识库切块）→ index（倒排，没有分批进度）→
 * memories（长期记忆）→ done。total / done 是切块加记忆的合计
 */
export interface ReindexJob {
  id: string
  state: 'running' | 'done' | 'failed'
  phase: 'chunks' | 'index' | 'memories' | 'done'
  collection: string | null
  total: number
  done: number
  chunks: { total: number; done: number }
  memories: { total: number; done: number }
  started_at: string
  finished_at: string | null
  error: string | null
  hint: string | null
  result: { reindexed: number; memories_reindexed: number; embedder: string; [key: string]: any } | null
}

/** runs.list 的 workflow_id 传它：只看没保存成工作流的那些运行（问数据页、画布上的临时图） */
export const UNSAVED_WORKFLOW_ID = '__none__'

export const api = {
  /** 心跳。带超时：后端卡死（连得上但不回）时也要能判成断开，而不是永远等下去 */
  health: (timeoutMs = 5000) => request<{ status: string; service?: string }>('/health', { timeoutMs }),
  system: () => get<any>('/system'),

  // ---- 工作流 ----
  workflows: {
    list: (opts?: RequestOptions) => get<Workflow[]>('/workflows', opts),
    get: (id: string) => get<Workflow>(`/workflows/${id}`),
    create: (body: { name: string; description?: string; graph?: GraphSpec; tags?: string[] }) =>
      post<Workflow>('/workflows', body),
    update: (id: string, body: Partial<Workflow> & { note?: string }) =>
      patch<Workflow>(`/workflows/${id}`, body),
    remove: (id: string) => del(`/workflows/${id}`),
    duplicate: (id: string) => post<Workflow>(`/workflows/${id}/duplicate`),
    versions: (id: string) => get<WorkflowVersion[]>(`/workflows/${id}/versions`),
    /** 某个版本的完整快照（含 graph）。正式运行的输入字段要从已发布版本取，不能取画布 */
    version: (id: string, v: number) => get<WorkflowVersion>(`/workflows/${id}/versions/${v}`),
    restore: (id: string, v: number) => post<Workflow>(`/workflows/${id}/versions/${v}/restore`),
    publish: (id: string, level: 'published' | 'governed', version?: number) =>
      post<{ ok: boolean; level?: string; version?: number; issues: any[] }>(
        `/workflows/${id}/publish`, { level, version }),
    validate: (graph: GraphSpec) =>
      post<{ ok: boolean; issues: ValidationIssue[] }>('/workflows/validate', { graph }),
    /**
     * 发布前检查：validate 和发布门禁的并集，和真正发布同一套口径，另给每条问题的修复。
     * 只读。不带 graph 就查库里的草稿。老后端没有这个接口（404），调用方按「不支持」处理
     */
    publishCheck: (id: string, body: { level: PublishLevel; graph?: GraphSpec }, opts?: RequestOptions) =>
      request<PublishCheck>(`/workflows/${id}/publish-check`,
        { method: 'POST', body: JSON.stringify(body), ...opts }),
    /** 按选中的修复出一份预览（整张图 + 逐项改动）。只读：不存草稿、不发布。assist 时会调模型，可能要几十秒 */
    autofix: (id: string, body: {
      level: PublishLevel; graph?: GraphSpec; apply: string[]; choices?: Record<string, unknown>; assist?: boolean
    }, opts?: RequestOptions) =>
      request<AutofixResult>(`/workflows/${id}/autofix`, { method: 'POST', body: JSON.stringify(body), ...opts }),
    /** 这张图里有哪些变量、谁产出、谁引用。纯静态分析，不需要跑过 */
    variables: (graph: GraphSpec) =>
      post<{ variables: Variable[]; issues: VarIssue[] }>('/workflows/variables', { graph }),
  },

  // ---- 运行 ----
  datasources: {
    list: (opts?: RequestOptions) => get<DataSource[]>('/datasources', opts),
    kinds: () => get<any>('/datasources/kinds'),
    create: (body: any) => post<any>('/datasources', body),
    update: (id: string, body: any) => patch<any>(`/datasources/${id}`, body),
    remove: (id: string) => del(`/datasources/${id}`),
    test: (id: string) => post<ConnectionTest>(`/datasources/${id}/test`, {}),
    /** 测一份还没保存的配置，只测连接不落库。编辑时带上 id，后端可以沿用已存的密码 */
    testConfig: (body: any) => post<ConnectionTest>('/datasources/test', body),
    /**
     * 上传 Excel / CSV，变成一个可以用 SQL 查的数据源。同名发布新版本替换。onProgress 报字节进度。
     *
     * 解析器要用户先拍板时回 422，错误上带着 decision（用 uploadDecision 取）；用户选完，带上
     * 答案重传：mixed='null' 把数字列里的非数字存为空值，raw_mode=true 按原样导入（未规整）
     */
    uploadTable: (file: File, body: {
      name: string; description?: string; header_row?: number; mixed?: 'reject' | 'null'; raw_mode?: boolean
    }, opts?: UploadOptions) => {
      const form = new FormData()
      form.append('file', file)
      form.append('name', body.name)
      form.append('description', body.description ?? '')
      form.append('header_row', String(body.header_row ?? 1))
      form.append('mixed', body.mixed ?? 'reject')
      form.append('raw_mode', body.raw_mode ? 'true' : 'false')
      return opts?.onProgress
        ? upload<UploadResult>('/datasources/upload', form, opts)
        : request<UploadResult>('/datasources/upload', { method: 'POST', body: form, signal: opts?.signal })
    },
    introspect: (id: string, schema?: string) =>
      post<any>(`/datasources/${id}/introspect${schema ? `?schema=${encodeURIComponent(schema)}` : ''}`, {}),
    /** 换个 schema 看看：只返回探到的结果，不覆盖缓存（Copilot 看到的还是原来那份） */
    previewSchema: (id: string, schema?: string) =>
      post<IntrospectPreview>(`/datasources/${id}/introspect${qs({ schema, dry_run: 'true' })}`, {}),
    schema: (id: string, table?: string) =>
      get<any>(`/datasources/${id}/schema${table ? `?table=${encodeURIComponent(table)}` : ''}`),
    /** 一张表的结构，带结构化的列信息，不用再去解析 detail 文本 */
    tableSchema: (id: string, table: string) =>
      get<TableSchema>(`/datasources/${id}/schema?table=${encodeURIComponent(table)}`),
  },

  /**
   * 按配方导入（期 2）。暂存区（staging）是一次还没提交的导入：起草、回答问题、改配方、试运行都在它上面做，
   * 提交（commit）成功之前不动数据源的当前版本。错误按 ApiError.code 分支（staging_closed、trial_required……）
   */
  tableImports: {
    /** 选文件开始一次导入（首次，或同名的简单导入切换为按配方导入）。带字节进度 */
    stage: (file: File, body: { name: string; description?: string }, opts?: UploadOptions) => {
      const form = new FormData()
      form.append('file', file)
      form.append('name', body.name)
      form.append('description', body.description ?? '')
      return upload<Staging>('/datasources/imports/stage', form, opts)
    },
    get: (id: string) => get<Staging>(`/datasources/imports/${encodeURIComponent(id)}`),
    /** 已选的全部回答（服务端从起点配方按问题顺序重放）。needs_reason 的选项要带 reason */
    answers: (id: string, answers: Record<string, QuestionAnswer>) =>
      post<Staging>(`/datasources/imports/${encodeURIComponent(id)}/answers`, { answers }),
    /**
     * 整份配方替换工作配方：有静态校验问题也照存，只是不能试运行。期 3：仍然成立的回答保留（不成立的进
     * answers_dropped），撤销栈清空、之前的修改标「已被覆盖」。origin 只在采用「按规则重新起草」的结果时给
     * 'rules_redraft'（服务端核对配方哈希，对不上回 409 redraft_mismatch）；不给时请求体与期 2 相同
     */
    putRecipe: (id: string, recipe: Recipe, origin?: 'rules_redraft') =>
      put<Staging>(`/datasources/imports/${encodeURIComponent(id)}/recipe`, origin ? { recipe, origin } : { recipe }),
    /** 将要发给模型的全文（同意框里给人看的就是这一份） */
    draftAiPreview: (id: string) => get<AiPreview>(`/datasources/imports/${encodeURIComponent(id)}/draft-ai/preview`),
    /** 用户同意后才调：preview_sha256 与服务端重算的不一致时回 409 ai_preview_stale */
    draftAi: (id: string, previewSha256: string) =>
      post<Staging>(`/datasources/imports/${encodeURIComponent(id)}/draft-ai`, { consent: true, preview_sha256: previewSha256 }),
    trial: (id: string, body: { context_inputs?: Record<string, { start: string; end: string }>; signed_by?: string | null } = {}) =>
      post<Staging>(`/datasources/imports/${encodeURIComponent(id)}/trial`, body),
    commit: (id: string, body: {
      trial_id: string; confirmations: string[]; acceptances: { check_id: string; reason: string }[]; signed_by?: string | null
    }) => post<CommitOut>(`/datasources/imports/${encodeURIComponent(id)}/commit`, body),
    discard: (id: string) => del(`/datasources/imports/${encodeURIComponent(id)}`),
    /** 上传新一期：按当前配方自动试运行，回来时已带回执和差异卡。带字节进度 */
    reupload: (sourceId: string, file: File, opts?: UploadOptions) => {
      const form = new FormData()
      form.append('file', file)
      return upload<Staging>(`/datasources/${encodeURIComponent(sourceId)}/reupload`, form, opts)
    },
    /** 修改配方（不换文件）：用当前导入的原件、以当前配方为工作配方 */
    redraft: (sourceId: string) => post<Staging>(`/datasources/${encodeURIComponent(sourceId)}/redraft`, {}),
    recipe: (sourceId: string) => get<CurrentRecipe>(`/datasources/${encodeURIComponent(sourceId)}/recipe`),
    /**
     * 期 3：修复或框选的预览（不改工作配方）。body 是 {fix: {id, option, reason?}} 或 {selection: {sheet, ref, as,
     * options?}} 二选一，seq 原样回传（只采用最新一次的回包）。ok=false 时照样 200，原因在 problems
     */
    editPreview: (id: string, body: EditRequest) =>
      post<EditPreview>(`/datasources/imports/${encodeURIComponent(id)}/edits/preview`, body),
    /**
     * 期 3：应用预览过的修复或框选。选项和理由必须与预览时逐字相同，expected_sha256 是预览返回的
     * recipe_sha256_after：对不上回 409 edit_stale（需要重新预览）；提议已不在当前列表里回 409 fix_stale
     */
    editApply: (id: string, body: EditRequest & { expected_sha256: string; signed_by?: string | null }) =>
      post<Staging>(`/datasources/imports/${encodeURIComponent(id)}/edits/apply`, body),
    /** 期 3：撤销上一次修改。没有可撤销的回 409 nothing_to_undo；之后配方又被改过回 409 undo_stale */
    editUndo: (id: string) => post<Staging>(`/datasources/imports/${encodeURIComponent(id)}/edits/undo`, {}),
    /** 期 3：按规则重新起草（不调用模型、不改工作配方），名字对齐到现行配方；「采用」走 putRecipe(…, 'rules_redraft') */
    redraftRules: (id: string) =>
      post<RedraftRulesOut>(`/datasources/imports/${encodeURIComponent(id)}/redraft-rules`, {}),
  },

  /**
   * 期 3 的版本页（数据源卡片上的「版本」，P3-SPEC 第 7 节）：版本列表、启用旧版本、清单、移除一期、作废接受、
   * 导入记录与清除原件。写操作都要 confirm: true；启用、移除、作废还要带界面看到的当前版本 id
   * （expected_current_snapshot_id），别人在这期间改过当前版本时回 409 base_changed。错误按 ApiError.code 分支
   */
  versions: {
    /** 最近 50 个版本，当前版本排第一。手工源返回空列表 */
    snapshots: (sourceId: string) => get<SnapshotOut[]>(`/datasources/${encodeURIComponent(sourceId)}/snapshots`),
    activate: (sourceId: string, snapshotId: string, body: ActivateSnapshotBody) =>
      post<ActivateSnapshotOut>(
        `/datasources/${encodeURIComponent(sourceId)}/snapshots/${encodeURIComponent(snapshotId)}/activate`, body),
    snapshotManifest: (sourceId: string, snapshotId: string) =>
      get<ManifestOut>(`/datasources/${encodeURIComponent(sourceId)}/snapshots/${encodeURIComponent(snapshotId)}/manifest`),
    importManifest: (sourceId: string, importId: string) =>
      get<ManifestOut>(`/datasources/${encodeURIComponent(sourceId)}/imports/${encodeURIComponent(importId)}/manifest`),
    /** 从当前版本中移除这一期（累积模式；配方和导入模式不变） */
    removePeriod: (sourceId: string, importId: string, body: RemovePeriodBody) =>
      post<RemovePeriodOut>(`/datasources/${encodeURIComponent(sourceId)}/imports/${encodeURIComponent(importId)}/remove`, body),
    /** 撤回这一期（作废接受，不可恢复）：替换模式回滚到 revoke_plan 的目标，累积模式移除这一期 */
    revokeAcceptance: (sourceId: string, importId: string, body: RevokeAcceptanceBody) =>
      post<RevokeAcceptanceOut>(
        `/datasources/${encodeURIComponent(sourceId)}/imports/${encodeURIComponent(importId)}/revoke-acceptance`, body),
    /** 全部导入记录，新的在前（含已被替换的） */
    imports: (sourceId: string) => get<ImportRecord[]>(`/datasources/${encodeURIComponent(sourceId)}/imports`),
    /** 清除一次导入的原件（同一份内容的其他导入一并清除，引用它的未完成导入一并放弃）。理由必填 */
    purgeRaw: (sourceId: string, importId: string, body: PurgeRawBody) =>
      post<PurgeRawOut>(`/datasources/${encodeURIComponent(sourceId)}/imports/${encodeURIComponent(importId)}/purge-raw`, body),
  },

  /**
   * 业务数据目录（每个数据源一份，按表存）。写操作都带读到的版本（if_version，这张表还没有目录时为 0）：别人在这期间
   * 改过时回 409，detail 是给人看的话（「表「x」的数据目录刚被修改过，请重新载入后再提交」）；格式不对回 422；源或表
   * 不存在回 404。署名走请求头 X-Actor（request 统一带上）
   */
  dataCatalog: {
    /** 表清单：表结构里的每张表各一行，使用次数多的在前；没有表结构时 tables 为空、schema_note 写明原因 */
    list: (sourceId: string, opts?: RequestOptions) =>
      get<CatalogList>(`/datasources/${encodeURIComponent(sourceId)}/catalog`, opts),
    /** 单表目录连同表结构 */
    get: (sourceId: string, table: string, opts?: RequestOptions) =>
      get<CatalogDetail>(`/datasources/${encodeURIComponent(sourceId)}/catalog/${encodeURIComponent(table)}`, opts),
    /** 整份提交：值改过的项、新填的项记为人工填写、已确认；提交里没有的项删掉 */
    put: (sourceId: string, table: string, notes: CatalogNotes, ifVersion: number) =>
      put<CatalogDetail>(`/datasources/${encodeURIComponent(sourceId)}/catalog/${encodeURIComponent(table)}`,
        { notes, if_version: ifVersion }),
    /** 单项审阅。path：表级项写字段名（grain）；列级项 columns.<列名>.<字段>；关系 relations.<编号> */
    review: (sourceId: string, table: string, body: { path: string; action: CatalogReviewAction; if_version: number }) =>
      post<CatalogDetail>(`/datasources/${encodeURIComponent(sourceId)}/catalog/${encodeURIComponent(table)}/review`, body),
    /** 影响面：引用这张表的已发布、受管模板（直接引用 / 可能涉及，合并查询按输入追溯） */
    impact: (sourceId: string, table: string, opts?: RequestOptions) =>
      get<CatalogImpact>(`/datasources/${encodeURIComponent(sourceId)}/catalog/${encodeURIComponent(table)}/impact`, opts),
    /** 目录修改提案的预览：对着当前目录重算改前、改后，不写库（保存遇到 409 后重新载入用） */
    patchPreview: (sourceId: string, table: string, changes: CatalogPatchSubmit[]) =>
      post<CatalogPatchPreview>(
        `/datasources/${encodeURIComponent(sourceId)}/catalog/${encodeURIComponent(table)}/patch/preview`, { changes }),
    /** 保存目录修改提案：改动过的项记为人工填写、已确认。版本不符 409，有一项不合法 422 */
    patch: (sourceId: string, table: string, changes: CatalogPatchSubmit[], ifVersion: number) =>
      post<CatalogDetail>(`/datasources/${encodeURIComponent(sourceId)}/catalog/${encodeURIComponent(table)}/patch`,
        { changes, if_version: ifVersion }),
    /**
     * 建一个起草任务（后台进行：注释、外键、命名推断；use_model 时再请起草模型起草，带上数据源的起草要求和这次的 note）。
     * 不给 tables 时按使用次数取前 20 张；表结构里没有的表照样建进任务、记 skipped。人工确认、驳回过的项不动。
     * 409：同一个数据源已有排队、进行中的任务（code 为 catalog_draft_busy，改去取 activeDraftJob）；起草模型用不了
     * （catalog_draft_model_unavailable，不建任务）；没有表结构
     */
    createDraftJob: (sourceId: string, body: { tables?: string[]; use_model: boolean; note?: string }) =>
      post<{ job: CatalogDraftJob }>(`${catalogBase(sourceId)}/draft-jobs`, body),
    /** 排队或进行中的那个起草任务；没有为 null */
    activeDraftJob: (sourceId: string, opts?: RequestOptions) =>
      get<{ job: CatalogDraftJob | null }>(`${catalogBase(sourceId)}/draft-jobs/active`, opts),
    /** 最近建的一个起草任务（任何状态）；没有为 null */
    latestDraftJob: (sourceId: string, opts?: RequestOptions) =>
      get<{ job: CatalogDraftJob | null }>(`${catalogBase(sourceId)}/draft-jobs/latest`, opts),
    /** 一个起草任务的当前状态；不存在或不属于这个数据源回 404 */
    draftJob: (sourceId: string, jobId: string, opts?: RequestOptions) =>
      get<{ job: CatalogDraftJob }>(`${catalogBase(sourceId)}/draft-jobs/${encodeURIComponent(jobId)}`, opts),
    /**
     * 停止起草：排队的直接停；进行中的停掉正在进行的模型调用，没开始和没写完的表记已停止，已经写进目录的不回滚。
     * 已经结束的回 409（catalog_draft_finished）
     */
    cancelDraftJob: (sourceId: string, jobId: string) =>
      post<{ job: CatalogDraftJob }>(`${catalogBase(sourceId)}/draft-jobs/${encodeURIComponent(jobId)}/cancel`),
    /**
     * 只重试失败的表：新任务只含原任务里失败、部分完成、已停止的表，用模型和补充说明沿用，起草要求取数据源现在的。
     * 没有可重试的表 422；有进行中的任务 409（catalog_draft_busy）；和建任务一样，起草模型用不了（原任务用模型时）
     * 409 catalog_draft_model_unavailable、没有表结构 409
     */
    retryDraftJob: (sourceId: string, jobId: string) =>
      post<{ job: CatalogDraftJob }>(`${catalogBase(sourceId)}/draft-jobs/${encodeURIComponent(jobId)}/retry`),
    /** 存数据源的起草要求（去首尾空白，最多 2000 字，空串清除）。上传表格建的源存在它自己的数据源上 */
    putGuidance: (sourceId: string, guidance: string) =>
      put<{ guidance: string | null }>(`${catalogBase(sourceId)}/guidance`, { guidance }),
    /**
     * 同步剖析：对业务库发少量只读查询，核对关系、取码值候选，结论写进目录。不给 tables 时按使用次数取前 10 张
     * 有待核实关系的表。没开启、数据源停用、没有表结构、同一个数据源正在剖析、快照被改动时回 409（detail 是一句中文）
     */
    profile: (sourceId: string, body: { tables?: string[] }, opts?: RequestOptions) =>
      request<CatalogProfileOut>(`/datasources/${encodeURIComponent(sourceId)}/catalog/profile`,
        { method: 'POST', body: JSON.stringify(body), ...opts }),
    // ---- 审阅队列（只列推断项，按表的使用次数、这一项对数字的影响和来源排好）
    /** 一页待确认项。kind：全部 / 影响数字的项 / 名称和含义；table 只看这一张表 */
    reviewQueue: (sourceId: string, params: { limit?: number; offset?: number; kind?: CatalogReviewKind; table?: string | null },
      opts?: RequestOptions) =>
      get<CatalogReviewQueue>(`${catalogBase(sourceId)}/review-queue${qs({ limit: 50, offset: 0, kind: 'all', ...params })}`, opts),
    /**
     * 成批确认、驳回、撤销审阅（最多 200 条）：逐表按 if_version 乐观锁，一张表冲突不影响别的表，同一张表的几条一次写入。
     * 逐条回结果：成功的带这张表写入后的版本，冲突、不合法的带原因和机读码
     */
    reviewBatch: (sourceId: string, items: CatalogReviewBatchItem[]) =>
      post<{ results: CatalogReviewBatchResult[] }>(`${catalogBase(sourceId)}/review/batch`, { items }),
    /** 审阅进度：使用次数前 top_n 张表、全部表、影响数字的项 */
    reviewProgress: (sourceId: string, opts?: RequestOptions) =>
      get<CatalogReviewProgress>(`${catalogBase(sourceId)}/review-progress`, opts),
    /** 用过这张表的运行，新的在前 */
    usage: (sourceId: string, table: string, limit = 20, opts?: RequestOptions) =>
      get<CatalogTableUsage>(`${catalogBase(sourceId)}/${encodeURIComponent(table)}/usage${qs({ limit })}`, opts),
    // ---- 导出导入
    /**
     * 导出这个数据源的数据目录（GET /catalog/export，附件）：回来的是文件本身和服务端给的文件名，没给就是 null，由调用方
     * 按数据源名补。原样存下，不解析：导入时交回的就是这份
     */
    exportFile: async (sourceId: string): Promise<{ blob: Blob; name: string | null }> => {
      const path = `${catalogBase(sourceId)}/export`
      let res: Response
      try {
        res = await fetch(BASE + path, { headers: actorHeader() })
      } catch (e: any) {
        const err = new ApiError(0, NETWORK_MESSAGE, { kind: 'network', raw: `${e?.name ?? 'Error'}: ${e?.message ?? e}` })
        report(false, err)
        throw err
      }
      if (!res.ok) throw failure(res.status, res.statusText, await res.text().catch(() => ''), path)
      report(true)
      return { blob: await res.blob(), name: dispositionName(res.headers.get('Content-Disposition')) }
    },
    /** 导入的预览（只读）：哪些表对上了，各有几项新增、更新、保留、冲突、跳过，冲突逐条列出 */
    importPreview: (sourceId: string, body: { content: unknown; include_guidance: boolean }) =>
      post<CatalogImportReport>(`${catalogBase(sourceId)}/import/preview`, body),
    /**
     * 导入：规则同预览，逐表乐观锁，一张表失败不影响别的。有排队、进行中的起草任务时 409（catalog_draft_busy）；
     * 文件格式不对 422（detail 是一句中文）
     */
    importCatalog: (sourceId: string, body: { content: unknown; include_guidance: boolean }) =>
      post<CatalogImportReport>(`${catalogBase(sourceId)}/import`, body),
  },

  runs: {
    /**
     * status 可以给多个（逗号连接）；q 按工作流名模糊匹配；before 是翻页游标
     * （上一页最后一条的 created_at）；limit 上限 200。老后端不认的参数会被忽略。
     */
    list: (params?: {
      workflow_id?: string; status?: RunStatus | RunStatus[] | string; run_class?: 'formal' | 'exploratory'
      q?: string; limit?: number; before?: string
    }) => get<Run[]>(`/runs${qs(params)}`),
    get: (id: string) => get<Run>(`/runs/${id}`),
    /** 这次运行实际跑的那张图（运行时的快照），不是工作流现在的样子 */
    graph: (id: string) => get<{ graph: GraphSpec; workflow_id: string | null; version: number | null }>(
      `/runs/${id}/graph`),
    start: (body: {
      workflow_id?: string; graph?: GraphSpec; input?: Record<string, any>
      memory_scope?: string; collection?: string
      run_class?: 'formal' | 'exploratory'; version?: number
    }) => post<Run>('/runs', body),
    artifacts: (id: string) => get<any[]>(`/runs/${id}/artifacts`),
    /** 从失败的节点接着跑。graph 只能带改过配置的同一张图，结构必须一致 */
    continue: (id: string, graph?: GraphSpec | null) =>
      post<Run>(`/runs/${id}/continue`, { graph: graph ?? null }),
    // 等审批的运行直接收成 cancelled；在跑的要等引擎收尾，先回 stopping
    cancel: (id: string) => post<{ ok: boolean; status?: 'stopping' | 'cancelled' }>(`/runs/${id}/cancel`),
    /**
     * 回复审批。approvalId 指明回复的是哪一条（一次运行里可能有几条同时等着）。
     * 审批卡的「始终允许」走这里：response 多带 always，/approvals/{id}/decide 的请求体没有这一项
     */
    resume: (id: string, response: any, approvalId?: string) =>
      post<Run>(`/runs/${id}/resume`, approvalId ? { response, approval_id: approvalId } : { response }),
    events: (id: string, after = 0) => get<RunEvent[]>(`/runs/${id}/events?after=${after}`),
    state: (id: string) => get<any>(`/runs/${id}/state`),
    history: (id: string) => get<any[]>(`/runs/${id}/history`),
    /** 核对事件流和封存时的清单哈希：事后被改过、删过、插过都会对不上 */
    verify: (id: string) => get<{ sealed: boolean; ok: boolean | null; message: string }>(
      `/runs/${id}/verify`),
    /** 运行中的删不了（409）；封存过的正式运行要 force，否则也是 409，detail 写明后果 */
    remove: (id: string, opts?: { force?: boolean }) =>
      del(`/runs/${id}${opts?.force ? '?force=true' : ''}`),
  },

  approvals: {
    /** 老写法 list('pending') 照旧可用。status 可逗号分隔，'all' 取全部 */
    list: (params: string | { status?: string; run_id?: string; limit?: number } = 'pending', opts?: RequestOptions) =>
      get<Approval[]>(`/approvals${qs(typeof params === 'string' ? { status: params } : { status: 'pending', ...params })}`, opts),
    decide: (id: string, body: { approved: boolean; note?: string; value?: any; args?: any }) =>
      post<Run>(`/approvals/${id}/decide`, body),
  },

  // ---- 设置 ----
  providers: {
    list: (opts?: RequestOptions) => get<Provider[]>('/providers', opts),
    catalog: () => get<any>('/providers/catalog'),
    create: (body: any) => post<Provider>('/providers', body),
    update: (id: string, body: any) => patch<Provider>(`/providers/${id}`, body),
    remove: (id: string) => del(`/providers/${id}`),
    test: (id: string, body: { model?: string; prompt?: string }) =>
      post<ProviderTest>(`/providers/${id}/test`, body),
    /** 测一份还没保存的接入配置，不落库。model 不填用 default_model */
    testConfig: (body: ProviderDraft & { model?: string | null; prompt?: string }) =>
      post<ProviderTest>('/providers/test', body),
    /** 问这个接入点有哪些模型（带鉴权调 /v1/models），省得手敲模型 id */
    models: (body: ProviderDraft) =>
      post<{ ok: boolean; models: string[]; url?: string; error?: string; hint?: string; detail?: string }>(
        '/providers/models', body),
  },
  settings: {
    get: () => get<Record<string, any>>('/settings'),
    put: (values: Record<string, any>) => put<Record<string, any>>('/settings', { values }),
    /** 今天（本地日期）结论句裁判花了多少：设置页放在每日上限旁边。只读，计数只有裁判自己记 */
    judgeSpend: () => get<{ date?: string; usd?: number; calls?: number; unpriced_calls?: number
                            daily_max_usd?: number | null }>('/settings/judge/spend'),
  },

  // ---- 工具 ----
  tools: {
    list: (opts?: RequestOptions) => get<ToolInfo[]>('/tools', opts),
    /** 危险工具要 confirm，否则后端回 409，detail 写明它会做什么 */
    run: (name: string, args: Record<string, any>, opts?: { confirm?: boolean }) =>
      post<any>(`/tools/${name}/run`, opts?.confirm ? { args, confirm: true } : { args }),
    /** MCP / 自定义工具的信任档。key 是 ToolInfo.trust_key；设回 ask 等于删掉这一条 */
    setTrust: (key: string, trust: ToolTrust) =>
      put<{ key: string; trust: ToolTrust }>('/tools/trust', { key, trust }),
  },
  customTools: {
    /** 每行带 problem：库里存着的参数定义写坏了时是一句中文，绑了它的节点一定失败 */
    list: () => get<CustomTool[]>('/custom-tools'),
    /** 参数定义写坏了回 422，detail 是一句中文（ApiError.message），显示在参数字段下面 */
    create: (body: any) => post<CustomTool>('/custom-tools', body),
    update: (id: string, body: any) => patch<CustomTool>(`/custom-tools/${id}`, body),
    remove: (id: string) => del(`/custom-tools/${id}`),
    test: (id: string, args: Record<string, any>) => post<any>(`/custom-tools/${id}/test`, { args }),
    /** 试跑一份还没保存的配置，不落库。失败时回 {ok:false, error, hint, detail, duration_ms} */
    testDraft: (body: {
      name?: string; kind: string; parameters?: Record<string, any>; config?: Record<string, any>
      args?: Record<string, any>
    }) => post<any>('/custom-tools/test', body),
  },
  mcp: {
    list: () => get<any[]>('/mcp/servers'),
    create: (body: any) => post<any>('/mcp/servers', body),
    update: (id: string, body: any) => patch<any>(`/mcp/servers/${id}`, body),
    remove: (id: string) => del(`/mcp/servers/${id}`),
    probe: (id: string) => post<any>(`/mcp/servers/${id}/probe`),
    refresh: () => post<any>('/mcp/refresh'),
  },

  // ---- 沙箱 ----
  sandbox: {
    health: () => get<any>('/sandbox/health'),
    exec: (body: { code: string; language?: string; timeout?: number; network?: boolean; memory_mb?: number }) =>
      post<any>('/sandbox/exec', body),
  },

  // ---- 记忆 / 知识库 / Skill ----
  memory: {
    scopes: () => get<{ scope: string; count: number }[]>('/memory/scopes'),
    list: (scope?: string) => get<MemoryItem[]>(`/memory${scope ? `?scope=${scope}` : ''}`),
    add: (body: { content: string; scope?: string; kind?: string; importance?: number }) =>
      post<MemoryItem>('/memory', body),
    search: (q: string, scope = 'default', opts?: { peek?: boolean; limit?: number }) =>
      get<any>(`/memory/search${qs({ q, scope, peek: opts?.peek ? 'true' : undefined, limit: opts?.limit })}`),
    /**
     * 回忆调试：默认 peek，只看不计数。真实召回会给命中项 use_count += 1 并影响
     * 下次排序，调试台点几下就会把「被召回 N 次」抬高
     */
    recall: (q: string, opts?: { scope?: string; peek?: boolean; limit?: number }) =>
      get<any>(`/memory/search${qs({
        q, scope: opts?.scope ?? 'default', peek: (opts?.peek ?? true) ? 'true' : undefined, limit: opts?.limit,
      })}`),
    /** 原地修改，保留来源和召回记录 */
    update: (id: string, body: { content?: string; importance?: number; kind?: string }) =>
      patch<MemoryItem>(`/memory/${id}`, body),
    remove: (id: string) => del(`/memory/${id}`),
    clearScope: (scope: string) => del(`/memory/scope/${scope}`),
  },
  kb: {
    collections: (opts?: RequestOptions) =>
      get<{ collection: string; documents: number; chunks: number }[]>('/kb/collections', opts),
    documents: (collection?: string) =>
      get<KbDocument[]>(`/kb/documents${collection ? `?collection=${collection}` : ''}`),
    ingest: (body: { collection?: string; title?: string; content: string; source?: string }) =>
      post<KbDocument>('/kb/documents', body),
    /**
     * 上传一份文档。给了 onProgress 就报真实的字节进度（XHR）；字节发完以后是后端
     * 在解析、切块，那段没有进度，调用方写「处理中」，不要把条拉满冒充完成
     */
    upload: (file: File, collection = 'default', opts?: UploadOptions) => {
      const form = new FormData()
      form.append('file', file)
      const path = `/kb/upload?collection=${encodeURIComponent(collection)}`
      return opts?.onProgress
        ? upload<KbDocument>(path, form, opts)
        : request<KbDocument>(path, { method: 'POST', body: form, signal: opts?.signal })
    },
    search: (q: string, collection?: string, alpha = 0.5) =>
      get<KbSearchResult>(`/kb/search?q=${encodeURIComponent(q)}&alpha=${alpha}${collection ? `&collection=${encodeURIComponent(collection)}` : ''}`),
    /** 上传框能收哪些文件。以后端解析器为准，免得界面放行了后端必拒的格式 */
    formats: () => get<{
      extensions: string[]; text: string[]; tabular: string[]; legacy: string[]; accept: string
    }>('/kb/formats'),
    /** 一份文档被切成了什么样。检索不准时第一个该看的就是它 */
    document: (id: string) => get<{
      document: KbDocument
      chunks: {
        id: string; ordinal: number; content: string; truncated: boolean
        chars: number; token_len: number; embed_model: string; has_vector: boolean
      }[]
    }>(`/kb/documents/${id}`),
    remove: (id: string) => del(`/kb/documents/${id}`),
    embedding: (collection?: string) => get<{
      embedder: string; dim: number; configured: boolean
      kind: string; model: string; base_url: string
      stale_chunks: number; stale_memories: number; unindexed_chunks: number
      // has_semantics 看的是实际在用的那个；fallback = 配了语义模型却退回了本地哈希
      has_semantics: boolean; fallback: boolean; fallback_reason: string
      /** 运行时 kb_search 实际用的混合权重。调试台的滑块初值要跟它一致 */
      default_alpha?: number
    }>(`/kb/embedding${collection ? `?collection=${collection}` : ''}`),
    setEmbedding: (body: { kind: string; model?: string; base_url?: string }) =>
      put<{
        embedder: string; dim: number
        stale_chunks: number; stale_memories: number
      }>('/kb/embedding', body),
    probeEmbedding: (baseUrl: string) =>
      get<{ base_url: string; models: string[] }>(
        `/kb/embedding/probe?base_url=${encodeURIComponent(baseUrl)}`),
    /** 等做完再返回。几千段配远端 embedding 要好几分钟，界面上用 startReindex */
    reindex: (collection?: string) =>
      post<{ reindexed: number; memories_reindexed: number; embedder: string }>(
        `/kb/reindex${qs({ collection })}`),
    /**
     * 后台重建，立刻返回任务；进度用 reindexStatus 轮询。已经有一次在跑时回 409，
     * detail 写着进度
     */
    startReindex: (collection?: string) =>
      post<ReindexJob>(`/kb/reindex${qs({ collection, background: 'true' })}`),
    /** 最近一次重建的进度。进程里只记最近一次，还没重建过是 {state:'idle'} */
    reindexStatus: () => get<ReindexJob | { state: 'idle' }>('/kb/reindex'),
  },
  skills: {
    list: (opts?: RequestOptions) => get<Skill[]>('/skills', opts),
    create: (body: Partial<Skill>) => post<Skill>('/skills', body),
    update: (id: string, body: Partial<Skill>) => patch<Skill>(`/skills/${id}`, body),
    remove: (id: string) => del(`/skills/${id}`),
  },

  // ---- 治理 ----
  governance: {
    toolUsage: (workflowId: string) =>
      get<any>(`/governance/tool-usage?workflow_id=${workflowId}`),
    clusters: () => get<any>('/governance/exploratory-clusters'),
  },
  artifact: (id: string) => get<{ id: string; content: any }>(`/artifacts/${id}`),

  // ---- 可点击证据 ----
  // 只认封存范围内的事件追得到的工件（后端 api/evidence.py）。老后端没有这两个接口，调用方按 404 降级
  evidence: {
    /** 整次运行的证据图：模式（cited / legacy_contract / none）、封存状态、报告和证据清单 */
    graph: (runId: string, opts?: RequestOptions) =>
      get<EvidenceGraph>(`/runs/${encodeURIComponent(runId)}/evidence`, opts),
    /**
     * 点开一个片段：所在的句子、指标步骤（原式、代入式、输入、复算）、封存状态。一次运行
     * 里有好几份报告时 report 给报告节点 id（片段 id 每份文档各自从 s0 数起），不给取成果标注的那份
     */
    segment: (runId: string, segmentId: string, opts?: RequestOptions & { report?: string }) =>
      get<EvidenceSegmentDetail>(
        `/runs/${encodeURIComponent(runId)}/evidence/segments/${encodeURIComponent(segmentId)}${qs({ report: opts?.report })}`,
        opts),
    /**
     * 审计表：每个有状态的片段按状态分组（有出处 / 无证据 / 可疑实体 / 旧运行猜测），附上封存核对的结果。
     * groups 只要这几组（none、suspicious…）
     */
    audit: (runId: string, opts?: RequestOptions & { groups?: string[] }) =>
      get<EvidenceAudit>(`/runs/${encodeURIComponent(runId)}/evidence/audit${qs({ groups: opts?.groups?.join(',') || undefined })}`, opts),
    /**
     * 按需裁判（四期，只在探索运行里有）：请模型判断这几句结论。答复是 evidence.judged 事件的载荷
     * （判定、花费、触顶的上限），判定标 post_seal：落在封存之后，不改封存的文档。正式运行回 409。
     * 一次运行里有好几份报告时 report 给报告节点 id
     */
    judge: (runId: string, body: { units: string[]; report?: string }) =>
      post<EvidenceJudgeResult>(`/runs/${encodeURIComponent(runId)}/evidence/judge`, body),
    /**
     * 推断的来源（期 4）：上传表格的一个单元格来自原表的哪一格、这次查询查的是哪一版数据、相关核对。按需算，
     * 推断不出来不是错误（200，结论在 status、reason、alert 里）。只在片段接口的查询步骤带 provenance: true 时请求；
     * 老服务端没有这个接口，回 404 且没有 code。report 同片段接口
     */
    provenance: (runId: string, segmentId: string, opts?: RequestOptions & { report?: string }) =>
      get<EvidenceProvenance>(
        `/runs/${encodeURIComponent(runId)}/evidence/segments/${encodeURIComponent(segmentId)}/provenance${qs({ report: opts?.report })}`,
        opts),
    /**
     * 导出审计表：?format=json / csv 作为附件下载。回来的是文件本身和后端给的文件名
     * （Content-Disposition；没给就用 evidence-<运行号前 8 位>.<格式>）
     */
    auditExport: async (runId: string, format: 'json' | 'csv', groups?: string[]): Promise<{ blob: Blob; name: string }> => {
      const path = `/runs/${encodeURIComponent(runId)}/evidence/audit${qs({ format, groups: groups?.join(',') || undefined })}`
      let res: Response
      try {
        res = await fetch(BASE + path, { headers: actorHeader() })
      } catch (e: any) {
        const err = new ApiError(0, NETWORK_MESSAGE, { kind: 'network', raw: `${e?.name ?? 'Error'}: ${e?.message ?? e}` })
        report(false, err)
        throw err
      }
      if (!res.ok) throw failure(res.status, res.statusText, await res.text().catch(() => ''), path)
      report(true)
      const named = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(res.headers.get('Content-Disposition') ?? '')?.[1]
      return { blob: await res.blob(), name: named ? decodeURIComponent(named) : `evidence-${runId.slice(0, 8)}.${format}` }
    },
  },

  // ---- 会话 ----
  conversations: {
    /** includeArchived：连回收站里的一起取（每行的 archived 区分），回收站视图要它 */
    list: (kind: 'chat' | 'canvas' = 'chat', workflowId?: string, opts?: { includeArchived?: boolean }) =>
      get<Conversation[]>(
        `/conversations?kind=${kind}`
        + (workflowId ? `&workflow_id=${encodeURIComponent(workflowId)}` : '')
        + (opts?.includeArchived ? '&include_archived=true' : '')),
    create: (body: { kind?: 'chat' | 'canvas'; workflow_id?: string; title?: string }) =>
      post<ConversationDetail>('/conversations', body),
    get: (id: string) => get<ConversationDetail>(`/conversations/${id}`),
    update: (id: string, body: { title?: string; archived?: boolean }) =>
      patch<Conversation>(`/conversations/${id}`, body),
    remove: (id: string) => del(`/conversations/${id}`),
    startTurn: (id: string, question: string) =>
      post<ConversationTurn>(`/conversations/${id}/turns`, { question }),
    patchTurn: (id: string, turnId: string, body: Partial<ConversationTurn>) =>
      patch<ConversationTurn>(`/conversations/${id}/turns/${turnId}`, body),
  },

  // ---- Copilot ----
  copilot: {
    generate: (body: {
      instruction: string; base_graph?: GraphSpec | null
      conversation_id?: string | null
      /** 这一轮只查这几个数据源（id 或名字）。不传或空表示不限 */
      datasource_ids?: string[] | null
    }) =>
      post<{
        graph: GraphSpec; explanation: string; issues: ValidationIssue[]
        layout?: { mode: 'keep' | 'full'; placed: string[] }
        /** 改图前后都在、工具绑定却变了的节点和成员。老后端不给 */
        tool_changes?: ToolChange[]
      }>('/copilot/generate', body),
    explain: (graph: GraphSpec) => post<{ explanation: string }>('/copilot/explain', { graph }),
    layout: (graph: GraphSpec) => post<GraphSpec>('/copilot/layout', { graph }),
    fromRun: (runId: string, name?: string) =>
      post<any>('/copilot/from-run', { run_id: runId, name }),
    getModel: () => get<CopilotModel>('/copilot/model'),
    setModel: (body: { provider?: string | null; model?: string | null }) =>
      put<CopilotModel>('/copilot/model', body),
    /** 跑完之后复核一次。干净的运行后端直接返回 verdict='ok'，不调模型 */
    review: (body: { run_id: string; question: string }) =>
      post<ReviewResult>('/copilot/review', body),
    /**
     * 一键升级为可追溯结构：先按确定的规则改写（llm 换成报告撰写、问数据的图插入报告节点……），
     * assist 时再交给 Copilot 改语义层。level 是复核门禁用的发布级别（受管的图，新写出的报告节点按受管的要求配）。
     * 只给预览，不改库。老后端没有这个接口（404），调用方按「不支持」处理
     */
    upgradeEvidence: (body: { graph: GraphSpec; assist?: boolean; level?: PublishLevel }, opts?: RequestOptions) =>
      request<UpgradeResult>('/copilot/upgrade-evidence', { method: 'POST', body: JSON.stringify(body), ...opts }),
  },

  // ---- 复核套件：各功能的接口在自己的文件里（api/<名字>.ts），这里只挂上去 ----
  schedules,
  compare,
  bundle,
  inquiry,
  whatif,
  reportExport,

  // ---- 应用平台（typesApp.ts 是接口约定） ----
  apps,
  ledger,
  tasks,
  actions,
  auth,
  identity,
  capabilities,
  endpoints,
  notify,
}

/**
 * 流式生成工作流：SSE 逐操作回调，返回取消函数。
 * 每个操作（add_node / add_edge / …）到达即回调，画布边收边长。
 */
export function streamCopilot(
  body: {
    instruction: string
    base_graph?: GraphSpec | null
    provider?: string | null
    model?: string | null
    /** answer = 用户在问问题（问数据页），build = 用户在描述流程（画布）*/
    intent?: 'build' | 'answer'
    /** 属于哪次对话。带上它，这一轮才知道前面聊过什么 */
    conversation_id?: string | null
    /** 这一轮只查这几个数据源（id 或名字）。不传或空表示不限 */
    datasource_ids?: string[] | null
  },
  // final 操作里带 tool_changes（ToolChange[]）：改图回执要显式列出工具绑定的变化
  onOp: (op: any) => void,
  // 开流之前就被拒（4xx/5xx）时第二个参数带状态码和后端的机读码（{detail, code}），调用方按码分支
  onEnd: (error?: string, info?: { status?: number; code?: string }) => void,
): () => void {
  const controller = new AbortController()
  void (async () => {
    try {
      const res = await fetch(`${BASE}/copilot/generate-stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...actorHeader() },
        body: JSON.stringify(body),
        signal: controller.signal,
      })
      if (!res.ok || !res.body) {
        const text = await res.text().catch(() => '')
        let body: any
        let isJson = false
        try { body = JSON.parse(text); isJson = true } catch { /* 响应体不是 JSON */ }
        if (isGatewayFailure(res.status, text, isJson)) {
          report(false, new ApiError(res.status, NETWORK_MESSAGE, { kind: 'network' }))
          onEnd(NETWORK_MESSAGE)
          return
        }
        report(true)
        onEnd(isJson ? describeDetail(body?.detail ?? body, '/copilot/generate-stream')
          : res.status >= 500 ? `服务端错误（${res.status}），详情请查看服务日志`
          : `请求失败（HTTP ${res.status}）`, { status: res.status, code: isJson ? codeOf(body) : undefined })
        return
      }
      report(true)
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        let idx: number
        while ((idx = buffer.indexOf('\n\n')) >= 0) {
          const frame = buffer.slice(0, idx)
          buffer = buffer.slice(idx + 2)
          if (!frame.startsWith('data: ')) continue
          try {
            onOp(parseBody(frame.slice(6)))
          } catch { /* 半截帧，忽略 */ }
        }
      }
      onEnd()
    } catch (e: any) {
      if (e?.name === 'AbortError') { onEnd(); return }
      // 流到一半断了和一开始就连不上，对用户是一回事：后端够不着了
      if (e instanceof TypeError) {
        report(false, new ApiError(0, NETWORK_MESSAGE, { kind: 'network', raw: String(e) }))
        onEnd(NETWORK_MESSAGE)
        return
      }
      onEnd(e?.message ?? '连接中断，请重试')
    }
  })()
  return () => controller.abort()
}

/** 表助手一轮结束时的情况：error 是给人看的那句话（开流前被拒时带状态码和机读码）；aborted 是点了停止 */
export interface CatalogAssistantEnd {
  error?: string
  status?: number
  code?: string
  aborted?: boolean
}

/**
 * 表助手的一轮（POST /datasources/{id}/catalog/{table}/assistant）：回答是事件流，每个事件一行 data: <JSON>，到一个回调
 * 一个（text 增量、query 只读查询、proposal 修改提案、done、error）。返回取消函数，就是「停止回答」。
 *
 * 开流之前就被拒（表不在 404、起草模型用不了 409 catalog_draft_model_unavailable、请求太长 422）时 onEnd 带服务端那句话、
 * 状态码和机读码；流到一半断了和一开始就连不上一样，说无法连接服务。认不出的行（心跳、半截）跳过
 */
export function streamCatalogAssistant(
  sourceId: string,
  table: string,
  body: CatalogAssistantRequest,
  onEvent: (event: CatalogAssistantEvent) => void,
  onEnd: (end: CatalogAssistantEnd) => void,
): () => void {
  const controller = new AbortController()
  const path = `${catalogBase(sourceId)}/${encodeURIComponent(table)}/assistant`
  const emit = (line: string) => {
    const text = line.replace(/\r$/, '')
    if (!text.startsWith('data:')) return
    const payload = text.slice(5).trim()
    if (!payload) return
    try {
      const event = parseBody(payload)
      if (event && typeof event === 'object' && typeof event.type === 'string') onEvent(event as CatalogAssistantEvent)
    } catch { /* 认不出的一行，跳过 */ }
  }
  void (async () => {
    try {
      const res = await fetch(BASE + path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream', ...actorHeader() },
        body: JSON.stringify(body),
        signal: controller.signal,
      })
      if (!res.ok || !res.body) {
        const err = failure(res.status, res.statusText, await res.text().catch(() => ''), path)
        onEnd(err.kind === 'network' ? { error: NETWORK_MESSAGE } : { error: err.message, status: res.status, code: err.code })
        return
      }
      report(true)
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        let idx: number
        while ((idx = buffer.indexOf('\n')) >= 0) {
          emit(buffer.slice(0, idx))
          buffer = buffer.slice(idx + 1)
        }
      }
      buffer += decoder.decode()
      if (buffer) emit(buffer)
      onEnd({})
    } catch (e: any) {
      if (e?.name === 'AbortError') { onEnd({ aborted: true }); return }
      if (e instanceof TypeError) {
        report(false, new ApiError(0, NETWORK_MESSAGE, { kind: 'network', raw: String(e) }))
        onEnd({ error: NETWORK_MESSAGE })
        return
      }
      onEnd({ error: e?.message ?? NETWORK_MESSAGE })
    }
  })()
  return () => controller.abort()
}

/**
 * 订阅一次运行的事件流。
 *
 * 先补历史再接实时由后端保证，这里负责断线重连——重连时带上已收到的最大 seq。
 *
 * `after` 必须由调用方给：lastSeq 是这次调用的闭包局部量，跨调用会归零。
 * 审批恢复时会重新 attachRun，不传 after 就是 after=0，后端按 `seq > after`
 * 把整条历史再推一遍，而消费方多半是无条件追加——事件就这么重复累积了。
 * 已经有事件在手的场景（重新接上一条运行）应当传当前最大 seq。
 */
export function streamRun(
  runId: string,
  onEvent: (event: RunEvent) => void,
  // stream.end 带着运行的最终状态：连到一条已经不在进行中的运行（含 interrupted）
  // 时，客户端靠它知道不会再有事件，不能一直当它还在跑
  onClose?: (end?: { status?: RunStatus | string }) => void,
  after = 0,
): () => void {
  let socket: WebSocket | null = null
  let closed = false
  let lastSeq = after
  let retry = 0

  const connect = () => {
    if (closed) return
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    socket = new WebSocket(`${proto}://${location.host}/api/runs/${runId}/stream?after=${lastSeq}`)

    socket.onmessage = (ev) => {
      const event = parseBody(ev.data) as RunEvent & { type: string; status?: string }
      if (event.type === 'stream.end') {
        closed = true
        socket?.close()
        // 新后端放在 data.status，老后端放在顶层 status
        onClose?.({ status: event.data?.status ?? event.status })
        return
      }
      if (event.seq) lastSeq = Math.max(lastSeq, event.seq)
      onEvent(event)
    }
    socket.onclose = () => {
      if (closed) return
      // 指数退避重连，最多等 5 秒
      retry += 1
      setTimeout(connect, Math.min(300 * 2 ** retry, 5000))
    }
    socket.onopen = () => {
      retry = 0
      report(true)
    }
  }
  connect()

  return () => {
    closed = true
    socket?.close()
  }
}
