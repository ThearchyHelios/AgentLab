/**
 * 评测（docs/design/next-round/01-效果度量与回归评测.md 第 18 项）的接口形状：和服务端 backend/app/api/evals.py、
 * app/eval/*（cases、gold、variants、runner、stats、compare、snapshots、transfer）一一对应。
 *
 * 统计、结论、能不能做都由服务端给（界面层不放逻辑）：这里只描述形状，界面照着显示。字段的叫法在 lib/termsEval.ts。
 */
// ---------------------------------------------------------------------------
// 题集
// ---------------------------------------------------------------------------

/** 评测-D9 的四项预算：同时评几题、每题时限（秒）、单次和每日的金额上限（美元） */
export interface EvalBudget {
  max_parallel: number
  per_case_timeout_s: number
  per_run_max_usd: number
  daily_max_usd: number
}

/** 一种比例和它的 Wilson 95% 区间（stats.rate） */
export interface EvalRate {
  passed: number
  n: number
  /** 分母为 0 时为 null，不写成 0 */
  rate: number | null
  ci: [number, number] | null
}

/** 题集列表里最近一次评测（不论结束没有） */
export interface EvalLatest {
  id: string
  status: EvalStatus
  running: boolean
  arm: string | null
  pair_id: string | null
  trigger: EvalTrigger
  created_at: string | null
  finished_at: string | null
  counts: EvalCounts
  accuracy: EvalRate | null
  traced_accuracy: EvalRate | null
}

/** 趋势的一个点：同一变体评完的一次 */
export interface EvalTrendPoint extends Partial<EvalRate> {
  id: string
  created_at: string | null
  arm: string | null
}

/** 目标是这个题集的定时任务 */
export interface EvalSuiteSchedule {
  id: string
  name: string
  enabled: boolean
  description_text: string
  next_run_at: string | null
}

export interface EvalSuite {
  id: string
  name: string
  description: string
  timezone: string
  default_variant: Partial<EvalVariant>
  /** 题集上改过的预算项（只存和缺省不同的） */
  budget: Partial<EvalBudget>
  /** 生效的预算：缺省 ← 设置里 eval 一组 ← 题集改过的项（每日上限只能往下收） */
  effective_budget: EvalBudget
  archived: boolean
  version: number
  created_by: string | null
  created_at: string | null
  updated_at: string | null
  counts?: { cases: number; enabled: number }
  latest?: EvalLatest | null
  trend?: EvalTrendPoint[]
  schedules?: EvalSuiteSchedule[]
}

export interface EvalSuiteDetail extends EvalSuite {
  cases: EvalCase[]
  /** 口径升版待处置的题数（顶部提示） */
  pending_upgrades: number
}

// ---------------------------------------------------------------------------
// 题目
// ---------------------------------------------------------------------------

export type EvalCaseKind = 'ask' | 'workflow'
export type EvalExpectationKind = 'scalar' | 'table' | 'set' | 'none' | 'ask' | 'text'

/**
 * 一个期望。数值期望不许人填（评测-D4）：只写从期望答案的结果里取哪一格、哪一列、整张表，值由 SQL / 口径算出来
 */
export interface EvalExpectation {
  id?: string
  kind: EvalExpectationKind
  required?: boolean
  label?: string
  /** scalar（SQL 题）、set：结果里的列 */
  column?: string
  /** scalar（口径题）：口径卡上的指标 id */
  metric?: string
  /** scalar：第几行（从 0 数） */
  row?: number
  /** scalar：按维度找那一行，如 {地区: 华南} */
  where?: Record<string, string | number>
  /** 允许的相对误差（0.01 是 1%）；不写由评分按显示精度推 */
  tolerance?: number
  /** table：按哪几列配对 */
  keys?: string[]
  /** table：比哪几列（缺省全部） */
  columns?: string[]
  top_n?: number
  ordered?: boolean
  /** text：文字要点（由裁判判，单列，不计入准确率） */
  points?: string[]
}

export type EvalGoldKind = 'sql' | 'caliber' | 'from_run'

/** 口径题的升版处置（服务端写） */
export interface EvalUpgradeRecord {
  policy: EvalUpgradePolicy
  from_version: number
  to_version: number
  at: string
  by?: string | null
}

/** 期望答案的来源 */
export interface EvalGold {
  kind: EvalGoldKind
  /** sql、from_run */
  datasource_id?: string
  sql?: string
  /** caliber */
  workflow_id?: string
  version?: number
  node_id?: string
  inputs?: Record<string, string>
  upgrade?: EvalUpgradeRecord | null
  /** from_run：出自哪次运行的哪一条查询、谁在什么时候认定的 */
  run_id?: string
  query?: string
  confirmed_by?: string | null
  confirmed_at?: string | null
}

export type EvalUpgradePolicy = 'recompute' | 'dual' | 'incomparable'

/** 口径题钉的那一版现在怎么样（gold.upgrade_state）；读不出那一版的照实写原因 */
export interface EvalUpgradeState {
  pinned: number | null
  latest?: number | null
  retired?: boolean
  pending: boolean
  message: string | null
  policy?: EvalUpgradePolicy | null
  versions?: number[]
  code?: string
}

/** 题目上记的最近一次试算（不带期望值：期望每次重算，不冻结数值） */
export interface EvalTrialRecord {
  status: EvalTrialStatus
  code: string | null
  reason: string | null
  as_of: string
  run_ids: string[]
  at?: string
  by?: string | null
}

export interface EvalCase {
  id: string
  suite_id: string
  seq: number
  kind: EvalCaseKind
  question: string
  datasource_ids: string[]
  workflow_id: string | null
  workflow_version: number | null
  input_template: Record<string, string>
  history: null
  expectations: EvalExpectation[]
  gold: EvalGold | null
  tags: string[]
  enabled: boolean
  gold_source: { id: string; name: string | null } | null
  datasources: { id: string; name: string | null }[]
  archived: boolean
  superseded_by: string | null
  version: number
  case_hash: string
  trial: EvalTrialRecord | null
  upgrade: EvalUpgradeState | null
  created_by: string | null
  created_at: string | null
  updated_at: string | null
}

/** 建题、改题交上去的（改的时候带 version；没带的项照旧） */
export interface EvalCaseBody {
  kind?: EvalCaseKind
  question?: string
  datasource_ids?: string[]
  workflow_id?: string | null
  workflow_version?: number | null
  input_template?: Record<string, string>
  expectations?: EvalExpectation[]
  gold?: EvalGold | null
  tags?: string[]
  enabled?: boolean
  version?: number
}

// ---------------------------------------------------------------------------
// 试算、预览
// ---------------------------------------------------------------------------

export type EvalTrialStatus = 'ok' | 'refused' | 'no_value' | 'failed' | 'timeout' | 'skipped' | 'drifted'

/** 算出来的一份期望值（口径题选了 dual 时有两份：旧版在前） */
export interface EvalGoldSet {
  run_id: string
  snapshot: string | null
  version: number | null
  /** {期望 id: 取到的值}：scalar {value, column?, row?, dims?, metric?, name?, unit?}、table {rows…}、set {values…}、none {rows: 0} */
  values: Record<string, EvalGoldValue>
}

export interface EvalGoldValue {
  kind: EvalExpectationKind
  value?: unknown
  column?: string
  row?: number
  dims?: Record<string, unknown>
  where?: Record<string, unknown>
  metric?: string
  name?: string
  unit?: string
  rendered?: string | null
  keys?: string[]
  columns?: string[]
  rows?: Record<string, unknown>[] | number
  values?: unknown[]
  ordered?: boolean
}

/** 一次试算（GoldOutcome.to_dict） */
export interface EvalTrial {
  status: EvalTrialStatus
  as_of: string
  reason: string | null
  code: string | null
  sets: EvalGoldSet[]
  run_ids: string[]
  rendered: { sql?: string; question?: string; inputs?: Record<string, string> }
}

/** 写 SQL 定期望的预览（POST …/cases/preview）：列和前几行；不过的照实写原因（不回 422） */
export interface EvalPreview {
  status: EvalTrialStatus | 'invalid'
  reason: string | null
  code: string | null
  field?: string | null
  columns: string[]
  rows: unknown[][]
  row_count: number | null
  truncated: boolean
  /** 交回了几行 */
  shown?: number
  run_id: string | null
  rendered: { sql?: string }
}

/** 建题、改题、处置升版之后交回的 */
export interface EvalCaseSaved {
  case: EvalCase
  trial: EvalTrial | null
  new_case?: EvalCase | null
}

/** 一次运行里执行过的查询（从运行认定时选一条） */
export interface EvalRunQuery {
  artifact: string
  node_id: string | null
  source: string | null
  source_id: string | null
  sql: string | null
  columns: string[]
  row_count: number | null
  truncated: boolean
  masked: boolean
}

/** 能选的口径（和蓝图助手同一份清单）：不能复用的照列，写明原因 */
export interface EvalCaliber {
  workflow_id: string
  workflow_name: string
  version: number
  level: string
  node_id: string
  card: string
  caliber: string
  metric_id: string
  name: string
  unit: string
  reusable: boolean
  reason: string | null
}

// ---------------------------------------------------------------------------
// 导出导入
// ---------------------------------------------------------------------------

export interface EvalImportPlan {
  suite: { name: string; description?: string; timezone?: string }
  cases: { seq: number | null; question: string | null; ok: boolean; reason: string | null; code: string | null }[]
  sources: Record<string, string | null>
  workflows: Record<string, string | null>
  importable: number
  skipped: number
}

export interface EvalImported {
  suite: EvalSuiteDetail
  imported: number
  skipped: EvalImportPlan['cases']
}

// ---------------------------------------------------------------------------
// 变体、发起
// ---------------------------------------------------------------------------

export type EvalCatalogMode = 'live' | 'reviewed' | 'off' | 'snapshot'

export interface EvalModelPair {
  provider: string | null
  model: string | null
}

/** 一臂变体：助手、写作模型，结论裁判，目录，重复次数，名字 */
export interface EvalVariant {
  assistant: EvalModelPair
  writer: EvalModelPair
  judge: boolean
  catalog: { mode: EvalCatalogMode; snapshot: string | null }
  repeats: number
  label: string | null
}

/** 发起时交上去的：一臂（variant）、几臂的 A/B（arms），或预设的「目录贡献对照」（preset） */
export interface EvalStartBody {
  variant?: Partial<EvalVariant>
  arms?: Partial<EvalVariant>[]
  preset?: 'catalog' | 'catalog_reviewed'
  baseline_id?: string | null
}

/** 发起之前的估算（不调模型、不查库） */
export interface EvalEstimate {
  arms: { label: string | null; variant: EvalVariant; models: EvalResolvedModels; text: string }[]
  cases: number
  repeats: number
  estimate: { items: number; per_item_usd: number | null; total_usd: number | null; basis: string | null; note: string }
  budget: EvalBudget
  parallel: number
  spend: EvalSpend
}

export interface EvalResolvedModels {
  assistant?: string | null
  writer?: string | null
  judge?: boolean
}

/** 今天评测那本账花了多少（settings/eval/spend 同形状） */
export interface EvalSpend {
  date?: string
  usd?: number
  calls?: number
  unpriced_calls?: number
  daily_max_usd?: number | null
}

// ---------------------------------------------------------------------------
// 评测
// ---------------------------------------------------------------------------

export type EvalStatus = 'queued' | 'running' | 'succeeded' | 'partial' | 'failed' | 'cancelled' | 'interrupted'
export type EvalTrigger = 'manual' | 'schedule' | 'pair'

export interface EvalCounts {
  total?: number
  done?: number
  running?: number
  pending?: number
  passed?: number
  failed?: number
  ungradable?: number
  skipped?: number
  errors?: number
}

/** 一份用量（providers/usage 的汇总） */
export interface EvalUsage {
  input_tokens?: number
  output_tokens?: number
  calls?: number
  cost_usd?: number | null
  unpriced_calls?: number
  by_model?: Record<string, { input_tokens: number; output_tokens: number; calls: number; cost_usd: number; unpriced_calls: number }>
}

export interface EvalTiming {
  p50: number | null
  p90: number | null
  n: number
}

/** 一臂的总表（stats.summarize）：全部在服务端算好 */
export interface EvalSummary {
  scorer_version?: number
  items?: number
  done?: number
  cases?: number
  verdicts?: Partial<Record<EvalVerdict, number>>
  not_run?: number
  accuracy?: EvalRate
  traced_accuracy?: EvalRate
  outcomes?: Partial<Record<EvalOutcome, number>>
  text?: Partial<Record<EvalTextOutcome, number>>
  paths?: Partial<Record<EvalPath, number>>
  tiers?: Partial<Record<'formal' | 'degraded' | 'withheld' | 'explore', number>>
  review?: { verdicts: Record<string, number>; severity: Record<string, number>; retries: number; slow: number }
  judge?: { on: boolean; claims: Record<string, number> | null }
  looks?: { model: number; system: number; per_case: number | null; limit_hit: number | null }
  calls?: { assistant: number; per_case: number | null }
  timings?: Partial<Record<'build_ms' | 'run_wall_ms' | 'run_active_ms' | 'review_ms' | 'total_ms', EvalTiming>>
  usage?: { assistant?: EvalUsage; writer?: EvalUsage; judge?: EvalUsage; gold?: EvalUsage }
  cost_usd?: number
  unpriced_calls?: number
  per_case_cost_usd?: number | null
  citations?: { numbers: number; numbers_cited: number; uncited_numbers: number; unresolved: number; unknown_entities: number; rate: number | null }
  e1?: EvalRate & { aspects: Record<string, number> }
  pass_rates?: Record<string, EvalRate>
  /** 和上一次比的计数（收尾时算好） */
  baseline?: {
    id: string; pinned: boolean; created_at: string | null; fingerprint_diff: EvalFingerprintDiff[]
    counts?: Record<EvalTransition, number>; causes?: Record<EvalCause, number>; cleaned?: boolean
  }
}

export interface EvalRun {
  id: string
  suite_id: string
  status: EvalStatus
  trigger: EvalTrigger
  schedule_id: string | null
  variant: EvalVariant
  resolved_models: EvalResolvedModels
  /** 「助手 x · 写作 y · 当前目录 · 裁判关 · 每题 1 次」 */
  variant_text: string
  catalog_snapshot: string | null
  as_of: string | null
  repeats: number
  fingerprint: Record<string, any>
  pair_id: string | null
  arm: string | null
  baseline_id: string | null
  pinned: boolean
  budget: Partial<EvalBudget> & { estimate?: EvalEstimate['estimate'] }
  counts: EvalCounts
  usage: { assistant?: EvalUsage; writer?: EvalUsage; judge?: EvalUsage; cost_usd?: number; unpriced_calls?: number }
  summary: EvalSummary
  stop_reason: string | null
  error: string | null
  created_by: string | null
  created_at: string | null
  started_at: string | null
  finished_at: string | null
  cleaned_at: string | null
  running: boolean
}

/** 评测详情另带的：正在评的几题在做什么、现在能做哪几件事 */
export interface EvalRunDetail extends EvalRun {
  current: { case_seq: number | null; repeat: number; arm: string | null; doing: string | null }[]
  actions?: { stop: boolean; retry: boolean; rescore: boolean; pin: boolean; unpin: boolean; open: number; cleaned: boolean }
}

export type EvalVerdict = 'pass' | 'fail' | 'ungradable' | 'skipped'
export type EvalOutcome = 'traced' | 'untraced' | 'wrong' | 'scale' | 'ungradable'
export type EvalTextOutcome = 'covered' | 'partial' | 'missing' | 'unjudged'
export type EvalPath = 'reply' | 'asked' | 'ready' | 'build_failed' | 'run_failed' | 'ran' | 'rejected'

/** 一个期望的结论 */
export interface EvalScore {
  id: string
  kind: EvalExpectationKind
  required: boolean
  label?: string
  outcome: EvalOutcome | EvalTextOutcome
  expected?: unknown
  got?: unknown
  raw?: string
  key?: Record<string, unknown>
  weak?: boolean
  cell?: Record<string, unknown>
  reason?: string
  rows?: number
  cells?: Record<string, number>
  items?: number
  missing?: unknown[]
  points?: number
}

/** 逐题的 E1：答案的查询和期望的取数等价与否、差在哪个方面 */
export interface EvalSqlMatch {
  status: 'proven' | 'related' | 'none' | 'skipped' | string
  query?: string | null
  sql?: string | null
  aspects?: string[]
  reason?: string | null
  cited?: boolean
}

export interface EvalResult {
  id: string
  eval_run_id: string
  seq: number
  case_id: string | null
  case_seq: number | null
  case_hash: string
  repeat: number
  arm: string | null
  status: 'pending' | 'running' | 'ok' | 'partial' | 'failed' | 'skipped' | 'cancelled'
  run_id: string | null
  gold_run_ids: string[]
  gold: { status: string; reason: string | null; code: string | null; before?: EvalTrial; after?: EvalTrial | null } | null
  path: EvalPath | null
  verdict: EvalVerdict | null
  scores: EvalScore[]
  traced: boolean
  review: { verdict?: string; severity?: string; note?: string } | null
  tier: string | null
  judge: Record<string, any> | null
  looks: { model?: number; system?: number; limit_hit?: boolean } | null
  calls: number
  timings: Partial<Record<'build_ms' | 'run_wall_ms' | 'run_active_ms' | 'review_ms' | 'total_ms', number>>
  usage: { assistant?: EvalUsage; writer?: EvalUsage; review?: EvalUsage; judge?: EvalUsage }
  sql_match: EvalSqlMatch | null
  details_artifact: string | null
  current: string | null
  error: string | null
  started_at: string | null
  finished_at: string | null
  /** 这一题实际问的问题（变量按这次评测的截至时间渲染好了）；题目之后改过、又没有作答运行可取时为 null */
  question?: string | null
  /** 题目的原文（可能带 {{ schedule.… }} 变量的模板）；题目删了为 null */
  question_template?: string | null
  /** 这一题的花费：助手（含复核）+ 写作 + 结论裁判，服务端加好的 */
  cost?: { cost_usd: number | null; unpriced_calls: number }
}

// ---------------------------------------------------------------------------
// 对比、A/B
// ---------------------------------------------------------------------------

export type EvalTransition = 'regressed' | 'improved' | 'same' | 'incomparable'
export type EvalCause = 'data' | 'method' | 'params' | 'path' | 'unknown'

export interface EvalFingerprintDiff {
  key: string
  label: string
  before?: unknown
  after?: unknown
}

export interface EvalCompare {
  base: EvalRun | null
  fingerprint_diff: EvalFingerprintDiff[]
  transitions: {
    counts: Record<EvalTransition, number>
    causes: Record<EvalCause, number>
    items: { case_id: string; case_seq: number | null; before: string; after: string; transition: EvalTransition; cause?: EvalCause; reason?: string }[]
  } | null
  cleaned: boolean
}

export interface EvalPair {
  pair_id: string
  evaluations: EvalRun[]
  table: {
    arms: { arm: string; accuracy: EvalRate }[]
    pairs: { a: string; b: string; both: number; neither: number; only_a: number; only_b: number; excluded: number; p_value: number; note: string | null }[]
  } | null
  cleaned: boolean
}

// ---------------------------------------------------------------------------
// 目录快照与影子起草
// ---------------------------------------------------------------------------

export type EvalSnapshotStatus = 'queued' | 'running' | 'succeeded' | 'partial' | 'failed' | 'cancelled' | 'interrupted'

export interface EvalSnapshot {
  id: string
  origin: 'live' | 'shadow'
  status: EvalSnapshotStatus
  artifact_id: string | null
  sources: { id: string; name: string }[]
  tables: Record<string, string[]> | null
  draft: { provider?: string | null; model?: string | null; model_id?: string | null; guidance?: string; note?: string; strip_llm?: boolean; suite_id?: string | null } | null
  estimate: EvalShadowEstimate | null
  usage: { input_tokens?: number; output_tokens?: number; calls?: number; cost_usd?: number | null; unpriced?: boolean }
  progress: { tables_total?: number; tables_done?: number; tables_failed?: number; tables_partial?: number; failures?: Record<string, string>; notice?: string | null }
  error: string | null
  created_by: string | null
  created_at: string | null
  started_at: string | null
  finished_at: string | null
  view: { mode: 'snapshot'; statuses: string; snapshot: string } | null
}

export interface EvalShadowEstimate {
  tables: number
  columns: number
  calls: number
  input_tokens: number
  output_tokens: number
  model: string | null
  cost_usd: number | null
  unpriced: boolean
  note: string
}

export interface EvalShadowBody {
  sources: { id: string; tables?: string[] }[]
  suite_id?: string | null
  provider?: string | null
  model?: string | null
  guidance?: string
  note?: string
  strip_llm?: boolean
}

export interface EvalShadowPlan {
  sources: { id: string; name: string; tables: string[]; unknown: string[] }[]
  estimate: EvalShadowEstimate
  draft: EvalSnapshot['draft']
}

// ---------------------------------------------------------------------------
// 口径命中与重复度
// ---------------------------------------------------------------------------

export type EvalE1Band = 'route' | 'normalize' | 'reconcile'
export type EvalE3Band = 'worth' | 'not_yet'

/** 一个窗口的 E1、E3（出自 ledger/stats.compute_stats）和各自落在哪一档 */
export interface EvalSignalPoint {
  since: string | null
  until: string | null
  e1: { rate: number | null; n: number; queries: number; band: EvalE1Band | null }
  e3: { share: number | null; n: number; lower_bound: number | null; band: EvalE3Band | null }
}

export interface EvalQuestionCluster {
  count: number
  representative: string
  first_at: string | null
  last_at: string | null
  members: { text: string; at: string | null; run_id: string | null; turn_id: string | null }[]
  /** 最近一个挂着运行的成员：「提取模板」从这次运行提取 */
  run_id: string | null
}

export interface EvalSignals {
  window: { since: string | null; until: string | null; days: number }
  criteria: {
    e1: { band: EvalE1Band; at_least: number; text: string }[]
    e3: { band: EvalE3Band; at_least: number; text: string }[]
    note: string
  }
  current: EvalSignalPoint
  trend: EvalSignalPoint[]
  clusters: EvalQuestionCluster[]
  questions: number
  threshold: number
  embedder: string
}

/** GET /api/ledger/stats 里评测页用到的几块（E1、E3）：键见 backend/app/ledger/stats.py */
export interface LedgerStats {
  window: { since: string | null; until: string | null; origins: string[] | null }
  algo?: string
  e1: {
    runs: number; queries: number; status: Record<string, number>; with_candidates: number
    rates: Record<string, number | null>; proven_via: Record<string, number>; unproven_reasons: Record<string, number>
    diff_aspects: Record<string, number>; labels: Record<string, number>; whole_equivalent: number
  }
  e3: {
    chat_runs: number; runs_with_queries: number; shapes: number; repeated_shapes: number; runs_in_repeated_shapes: number
    repeated_share: number | null; threshold: { runs: number; conversations: number }; questions: number; clusters: number
    questions_in_repeated_clusters: number; cluster_share: number | null; embedder: string
    certify?: { available: boolean; frozen: number; published: number; dismissed: number; publish_rate: number | null }
  }
  e2?: Record<string, any>
  e4?: Record<string, any>
}

/** 记录页里评测运行的来源（run.origin）：哪个题集、哪次评测、哪道题、什么角色 */
export interface EvalRunOrigin {
  suite_id?: string
  eval_run_id?: string
  case_id?: string
  role?: 'answer' | 'gold' | 'gold_after' | string
  arm?: string
  repeat?: number
  trial?: boolean
}

