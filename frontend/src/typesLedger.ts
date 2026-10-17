/**
 * 口径账本的接口约定（docs/design/next-round/02-口径账本.md；服务端 app/ledger/view.py、issue.py、reconcile.py、certify.py）。
 *
 * 界面层不放逻辑：每个数的标签、整题判断、能做的动作（能不能出具、能不能自动、不能的原因、要不要先核对参数）都由服务端
 * 给，界面照着画。比对在封存之后做（不在封存范围内），GET /api/runs/{id}/ledger 在比对还没出结果时答 state=pending，
 * 页面按退避再取（store/ledger.ts），不靠实时流：ledger.matched 在事件总线关闭之后才追加，打开着的页面收不到。
 */

/** 绑定的一个参数：值是这次运行查到过的遮罩值时不给值（masked），也不能拿它代填出具 */
export interface LedgerBinding {
  field: string
  value?: unknown
  masked?: boolean
}

/** 差在哪的一条（explain_sql_diff 的条目，SQL 片段已按遮罩换过）。text 是一句中文说明 */
export interface LedgerDiffItem {
  aspect?: string
  a?: unknown
  b?: unknown
  text?: string
}

export interface LedgerDiff {
  items: LedgerDiffItem[]
  /** 兜底说明：两条 SQL 对不上结构，没能归纳成具体差异 */
  fallback: boolean
  text_diff: string[]
}

/** 一份相关口径（同一个数据源、查了同样的表，但不等价） */
export interface LedgerRelated {
  caliber?: string
  workflow_id?: string
  version?: number
  level?: string
  level_text?: string
  workflow_name?: string | null
  node_id?: string
  comparable?: boolean
  reason?: string
  via?: string
  binding?: LedgerBinding[]
  diff?: LedgerDiff | null
}

/**
 * 一个数的标签（四种）：
 * - caliber：来自口径「X」vN（how：independent 等价已证明 / composed 助手选用）
 * - related：相关口径（附差在哪），数本身是这次的推理
 * - reasoning：这次的推理，没有相关的已发布口径
 * - unknown：没比对（reason 写为什么）
 */
export interface LedgerLabel {
  kind: 'caliber' | 'related' | 'reasoning' | 'unknown'
  how?: 'independent' | 'composed'
  caliber?: string
  workflow_id?: string
  version?: number
  level?: string
  level_text?: string
  workflow_name?: string | null
  binding?: LedgerBinding[]
  related?: LedgerRelated[]
  reason?: string
  /** 出自哪条查询（查询快照工件）；指标是 queries */
  query?: string
  queries?: string[]
  node_id?: string
  column?: string | null
  card?: string
  metric?: string
}

/** 一条查询的比对：proven / composed / related / none / skipped */
export interface LedgerQuery {
  artifact: string
  node_id?: string
  source?: string
  tables?: string[]
  status: 'proven' | 'composed' | 'related' | 'none' | 'skipped' | string
  reason?: string
  note?: string
  candidates: (LedgerRelated & { status?: string; how?: string })[]
  /**
   * 逐格比过的（服务端 ledger/match._cells，algo 4）：被引用的每一列各自的结论和候选。整条（按全部引用列投影）没证明、
   * 报告引用了这条查询好几列时才有；每个数的标签已经按它那一格标好，界面照标签画，不拿它重算
   */
  cells?: Record<string, { status: string; reason?: string; candidates: (LedgerRelated & { status?: string; how?: string })[] }>
}

/** 整题判断（主报告）：所有带标签的数都来自同一口径的同一版、参数一致时 equivalent */
export interface LedgerWhole {
  equivalent: boolean
  reason?: string
  counts?: Partial<Record<LedgerLabel['kind'], number>>
  caliber?: string
  workflow_id?: string
  version?: number
  level?: string
  level_text?: string
  workflow_name?: string | null
  how?: 'independent' | 'composed'
  binding?: LedgerBinding[]
  missing?: string[]
  missing_required?: string[]
}

/** 按口径正式出具这一项：能不能（available / reason）、能不能自动（auto / auto_reason）、已经发起过的那一次（issued_run） */
export interface LedgerIssueAction {
  kind: 'issue'
  workflow_id: string
  version: number
  caliber: string
  workflow_name: string
  how?: 'independent' | 'composed' | null
  level?: string | null
  level_text: string
  available: boolean
  auto: boolean
  reason?: string
  auto_reason?: string
  issued_run?: string
}

/** 入口字段（比对工件里记的那一份）：核对参数的表单照它画 */
export interface LedgerField {
  name: string
  required: boolean
  has_default: boolean
}

/** 看差多少（口径对账）这一项 */
export interface LedgerReconcileAction {
  kind: 'reconcile'
  query: string
  caliber: string
  workflow_id: string
  version: number
  node_id?: string
  workflow_name?: string | null
  level_text: string
  /** 参数是推断的、不全的、带遮罩值的：要人先核对（check_reason 是原因） */
  needs_check: boolean
  check_reason?: string
  binding: LedgerBinding[]
  /** 两条 SQL 对不上结构：多半要人选对照的指标 */
  fallback: boolean
  fields?: LedgerField[]
}

export type LedgerAction = LedgerIssueAction | LedgerReconcileAction

/** 按口径出具过的一次正式运行。tier_text 是「完整出具 / 降档出具 / 不予出具」，运行没结束时为空 */
export interface LedgerIssued {
  run_id: string
  status: string
  end_reason?: string | null
  workflow_id: string
  version: number
  tier?: string | null
  tier_text?: string | null
  auto: boolean
  created_at?: string | null
}

/** 对账卡上的一组对照：这次 / 口径 / 差值 / 相对差各是哪个指标 */
export interface LedgerReconcileGroup {
  label: string
  ours: string
  caliber: string
  diff: string
  rel: string
}

/** 对过的一次账 */
export interface LedgerReconciled {
  run_id: string
  status: string
  query?: string
  caliber?: string
  workflow_id?: string
  version?: number
  /** binding：按比对时的参数；checked：人核对过的参数 */
  inputs_from?: 'binding' | 'checked' | string
  rows?: number[] | null
  card?: string
  groups?: LedgerReconcileGroup[]
  created_at?: string | null
  /** 「说明有用 / 看不懂」（服务端 ledger/feedback.feedback_of）：看的这个人最后一次给的、各几个人 */
  feedback?: LedgerFeedback
}

export type LedgerFeedbackVerdict = 'useful' | 'unclear'

/** 对账结果上的反馈：mine 是看的这个人最后一次给的（没给过为空），useful / unclear 是各几个人（每人只算最后一次） */
export interface LedgerFeedback {
  mine?: LedgerFeedbackVerdict | null
  useful: number
  unclear: number
}

/** POST /api/ledger/answers/{run}/reconcile/{对账运行}/feedback 的答复 */
export interface LedgerFeedbackOut {
  run_id: string
  feedback: LedgerFeedback
}

/**
 * 答案身上的召回标记（GET /runs/{id}/ledger 的 recall，服务端 ledger/recall.answer_marks；细案做法 7「依据有变」「已按 vN
 * 核对，不影响」）。几种 state 都可能带：没比对过、不比对的答案照样可能被标依据有变。不放数、表名、取值
 */
export interface LedgerRecallMarks {
  /** 口径升版后召回重算、数没变：在这个答案上记的「已按 vN 核对，不影响」 */
  checked?: {
    workflow_id?: string | null
    workflow_name?: string | null
    from_version?: number | null
    to_version?: number | null
    recall_run?: string | null
    changed?: boolean
    at?: string | null
  }[]
  /** 依据有变：kind 是 catalog（目录里影响数字的项被驳回或修改）/ structure（表结构）/ values（新码值）/ catalog_edit */
  basis?: { kind?: string | null; items?: number; new_errors?: number; at?: string | null }[]
  /** 看的这个人手上、说的是这个答案的 recall 待办（还没处理的） */
  tasks?: { id: string; reason?: string; to_version?: number | null }[]
}

export interface LedgerCaliber {
  caliber: string
  workflow_id: string
  version: number
  level?: string
  graph_hash?: string | null
  workflow_name?: string | null
  level_text: string
}

/**
 * GET /api/runs/{id}/ledger。state：matched 比对过；pending 正在比对（或刚跑完、封存完就比）；ineligible 这次运行不比对
 * （reason 写为什么）；unreadable 比对记录读不出
 */
export interface RunLedger {
  run_id: string
  state: 'matched' | 'pending' | 'ineligible' | 'unreadable'
  reason?: string
  /** 「封存后比对，不在封存范围内」 */
  note?: string
  artifact?: string
  algo?: string
  trigger?: string
  retro?: boolean
  matched_at?: string | null
  post_seal?: boolean
  /** 事后补比的：按现在的口径回看 / 按之后发布的口径回看 */
  retro_note?: string
  calibers?: LedgerCaliber[]
  queries?: LedgerQuery[]
  reports?: Record<string, { labels: Record<string, LedgerLabel> }>
  primary?: string | null
  whole?: LedgerWhole
  actions?: LedgerAction[]
  issued?: LedgerIssued[]
  reconciled?: LedgerReconciled[]
  recall?: LedgerRecallMarks
}

/** 证据片段接口（GET /runs/{id}/evidence/segments/{seg}）的 ledger 键：这个片段的标签和比对的来历 */
export interface SegmentLedger {
  label: LedgerLabel
  artifact?: string
  algo?: string
  trigger?: string
  retro?: boolean
  matched_at?: string | null
  post_seal?: boolean
  note?: string
  retro_note?: string
}

/** POST /api/ledger/answers/{run}/issue 的答复：created 为假是同一次已经发起过、交回那一次 */
export interface LedgerIssueOut {
  run_id: string
  created: boolean
  issued: LedgerIssued
}

/** POST /api/ledger/answers/{run}/reconcile 的请求体 */
export interface LedgerReconcileIn {
  query: string
  workflow_id: string
  version: number
  inputs?: Record<string, unknown>
  confirm?: boolean
  pairs?: { ours: string; metric: string }[]
}

export interface LedgerReconcileOut {
  run_id: string
  reconciled: LedgerReconciled
}

/** 认不出列怎么配时 409（ledger_reconcile_needs_pairs）另带的选项 */
export interface LedgerPairOptions {
  columns: string[]
  metrics: { id: string; name: string; unit?: string }[]
}

// ---------------------------------------------------------------- 认证队列（可沉淀的问题）

export interface CertifyTurn {
  run_id: string | null
  conversation_id?: string | null
  turn_id?: string | null
  question: string
}

export interface CertifyFrozen {
  workflow_id: string
  name: string
  status?: string
  published_version?: number | null
}

/** 三种来源：确定性重复（SQL 形状相同）、问题聚类、被反复用的答案 */
export interface CertifyItem {
  id: string
  kind: 'shape' | 'cluster' | 'reused'
  question: string
  turns: CertifyTurn[]
  frozen_into?: CertifyFrozen[]
  // shape
  source?: string
  shape?: string
  runs?: number
  conversations?: number
  params?: { index: number; distinct: number }[]
  constants?: number
  related?: { caliber?: string; workflow_id?: string; version?: number; level?: string }[]
  representative?: { run_id: string; query?: string }
  peers?: string[]
  // cluster
  count?: number
  // reused
  uses?: { exports: number; app: boolean; extracted: boolean; canvas: boolean }
}

export interface CertifyQueue {
  items: CertifyItem[]
  considered: { runs: number; matched: number; questions: number }
  threshold: { runs: number; conversations: number; exports: number }
  embedder?: unknown
}

/** 固化成口径草稿（POST /copilot/from-run，freeze: true）的答复 */
export interface FreezeOut {
  workflow_id: string
  name: string
  nodes: number
  edges: number
  params: string[]
  metrics: string[]
  sources: CertifyTurn[]
}
