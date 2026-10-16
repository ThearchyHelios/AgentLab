/**
 * 口径召回的接口形状（服务端 app/ledger/recall.py、uses.py；docs/design/next-round/02-口径账本.md 做法 7、8）。
 *
 * 界面层不放逻辑：几组、能不能重算、为什么不能、数变没变、能不能重新出具，都由服务端给，界面只照着画。
 */

/** 一种用法的计数：问数回答几次、导出几次、定时任务几个、应用几个、正式运行几次、固定了它的工作流发起的正式运行几次 */
export interface RecallUseCounts {
  answers: number
  exports: number
  schedules: number
  apps: number
  formal: number
  pinned: number
  /** 反查有上限（按新的在前），少看了的为真 */
  truncated: boolean
}

/** 发布弹窗的召回预告（GET /api/workflows/{id}/recall-preview?version=） */
export interface RecallPreview {
  workflow_id: string
  version: number
  /** 每次发布最多自动重算几组（设置 ledger.recall_limit；0 表示不自动重算） */
  limit: number
  /** 旧版本里有没有能复用的口径卡（没有就不是口径，发布后不召回） */
  caliber: boolean
  problem: string | null
  /** 每个旧版本被谁用过；problem 是这一版整体不能重算的原因 */
  versions: (RecallUseCounts & { version: number; problem?: string })[]
  /** 一共几组（按旧版本、参数去重） */
  groups: number
  recomputable: number
  not_recomputable: number
  /** 已经召回过的组（重新发布同一版时） */
  already: number
  /** 发布之后预计自动重算几组 */
  will_recompute: number
  /** 超出上限、要手动接着算的几组 */
  over_limit: number
  truncated: boolean
  /** 不能重算的原因各几组 */
  reasons: { reason: string; groups: number }[]
}

export type RecallGroupState = 'changed' | 'unchanged' | 'failed' | 'running' | 'pending' | 'not_recomputable'

/** 召回批次里的一组 */
export interface RecallGroup {
  id: string
  from_version: number
  to_version: number
  binding: { field: string; value: unknown }[]
  uses: { kind: string; kind_text: string; count: number }[]
  latest: string | null
  state: RecallGroupState
  /** 不能重算的原因、重算失败的原因、发起被拒的原因 */
  reason?: string
  run_id?: string
  batch?: 'auto' | 'manual'
  /** 开了几条待办（数变了的） */
  tasks?: number
  /** 数变了的指标名 */
  changed_metrics?: string[]
}

/** 版本历史里「这一版的召回」（GET /api/ledger/recall/{wf}/{v}；POST …/continue 交回同样的形状加 launched） */
export interface RecallBatches {
  workflow_id: string
  workflow_name: string
  version: number
  limit: number
  /** 手动接着算一次发起几组 */
  batch_size: number
  problem: string | null
  caliber: boolean
  counts: Record<RecallGroupState, number>
  groups: RecallGroup[]
  /** 还没重算的几组（「另有 M 组未重算」） */
  remaining: number
  can_continue: boolean
  /** 还有没算的组却不能接着算时为什么（之后又发布了更新的版本……）；能接着算、都算过了为 null */
  continue_reason?: string | null
  truncated: boolean
  launched?: string[]
}

/** 版本用量里的召回：以这一版为新版本召回过几组、其中数变了几组；作为旧版本被召回过几组 */
export interface VersionRecallCounts {
  groups: number
  changed: number
  recalled_from: number
}

/** recall 待办里的一个数：值（遮罩了的为 null）、显示的字、出处（召回卡上哪个指标） */
export interface RecallCell {
  value: unknown
  text: string | null
  masked: boolean
  metric?: string
}

/** 原答案当时的值：正式运行给口径卡节点和指标，问数回答给那条查询快照和列；没有单独这个数的写 missing */
export interface RecallOriginal {
  k: number
  value: unknown
  text: string | null
  masked: boolean
  node?: string
  metric?: string
  query?: string
  column?: string
  missing?: string
}

export interface RecallAnswer {
  kind: string
  kind_text: string
  run_id: string
  version?: number
  conversation_id?: string
  turn_id?: string
  schedule_id?: string
  app_id?: string
  at?: string
  via_workflow?: string
  via_version?: number
  original: RecallOriginal[]
}

export interface RecallAction {
  decision: 'reissue' | 'ack'
  available: boolean
  reason?: string
  issued_run?: string
}

/**
 * recall 待办的 payload（服务端 ledger/recall.task_payload）。reason：caliber 口径升版数变了（旧值、新值、差值、原答案当时
 * 的值）；catalog 答案依据的数据目录有变、按新目录检查出了新的问题；structure 口径依据的表结构变了、试算没有算出来
 */
export interface RecallTaskPayload {
  kind: 'recall'
  reason: 'caliber' | 'catalog' | 'structure'
  recall_run: string
  workflow_id?: string | null
  workflow_name?: string
  from_version?: number | null
  to_version?: number | null
  binding?: { field: string; value: unknown; masked: boolean }[]
  metrics?: {
    k: number
    name: string
    unit: string
    changed: boolean
    card: string
    metric: string
    old: RecallCell
    new: RecallCell
    diff: RecallCell
  }[]
  answers?: RecallAnswer[]
  basis?: {
    kind?: string
    source?: string
    table?: string
    column?: string
    items?: { path?: string; label?: string; change?: string }[]
    new_errors?: number
    checks?: { code: string; message: string }[]
    workflow_id?: string
    version?: number
    change_kind?: string
    error?: string | null
    probe_run?: string
  }
  actions: RecallAction[]
  outcome?: Record<string, any> | null
}
