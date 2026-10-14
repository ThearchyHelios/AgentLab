import type {
  CatalogNotes, CatalogReviewAction, CatalogReviewBatchResult, CatalogReviewItem, CatalogReviewKind, CatalogStatus,
} from '../../types'
import { slotsOf } from './model'

// ===========================================================================
// 审阅队列的纯函数：地址里的视图和筛选、队列一项的键、成批审阅之后逐条的结果、表详情里改过之后队列跟着变、进度的百分比。
// 界面在 ReviewQueue.tsx。
//
// 队列只列推断项，顺序由服务端排好（表的使用次数 × 风险 × 来源）。审阅过的项不马上从列表里拿掉：原地标出结果
// （已确认、已驳回、冲突），可以选中撤销；刷新队列后才移除。
// ===========================================================================

/** 数据目录页的两个视图：表清单（逐表看）、审阅队列（跨表按优先级看推断项） */
export type CatalogView = 'list' | 'review'

export const REVIEW_KINDS: readonly CatalogReviewKind[] = ['all', 'facts', 'names']
/** 一页几项、一次成批审阅最多几条（服务端同一个上限） */
export const REVIEW_PAGE = 50
export const REVIEW_BATCH_MAX = 200

/**
 * 审阅视图记在地址的查询参数里：view=review；kind 筛选（默认全部不写）；rtable 只看一张表；item 从队列打开的那一项
 * （表详情据此滚到那一项、标出来）。和清单的 q / filter / sort / used 互不相干，各自只动自己的键
 */
export interface ReviewParams { view: CatalogView; kind: CatalogReviewKind; table: string; item: string }

export function reviewParamsOf(p: URLSearchParams): ReviewParams {
  const kind = p.get('kind') as CatalogReviewKind | null
  return {
    view: p.get('view') === 'review' ? 'review' : 'list',
    kind: kind && REVIEW_KINDS.includes(kind) ? kind : 'all',
    table: p.get('rtable') ?? '',
    item: p.get('item') ?? '',
  }
}

export function writeReviewParams(p: URLSearchParams, s: Partial<ReviewParams>): URLSearchParams {
  const next = new URLSearchParams(p)
  const put = (k: string, v: string | null | undefined) => (v ? next.set(k, v) : next.delete(k))
  if ('view' in s) put('view', s.view === 'review' ? 'review' : null)
  if ('kind' in s) put('kind', s.kind === 'all' ? null : s.kind)
  if ('table' in s) put('rtable', s.table)
  if ('item' in s) put('item', s.item)
  return next
}

/** 队列一项的键（表名里、路径里都可能有句点，中间用一个不会出现的字符隔开） */
export const itemKey = (i: { table_name: string; path: string }) => `${i.table_name}\u0000${i.path}`

/** 一项审阅过之后的处境：成功（现在的状态、这张表写入后的版本）、冲突（这张表刚被改过）、没成功（服务端的原因） */
export type ItemOutcome =
  | { kind: 'ok'; action: CatalogReviewAction; status: CatalogStatus }
  | { kind: 'conflict'; message: string | null }
  | { kind: 'error'; message: string | null }

/** 成批审阅之后这一项现在的状态：确认 → 已确认；驳回 → 已驳回；撤销审阅 → 回到来源的初始状态（队列里都是推断） */
const STATUS_AFTER: Record<CatalogReviewAction, CatalogStatus> = { confirm: 'confirmed', reject: 'rejected', reset: 'proposed' }

/** 服务端的机读码说的是不是版本冲突 */
export const isConflict = (r: Pick<CatalogReviewBatchResult, 'code'>): boolean => !!r.code && /conflict|version/i.test(r.code)

/**
 * 逐条的结果并进去：outcomes 按项的键记处境，versions 按表记最新的版本（之后再审阅这张表的项带它）。返回新的两份，不改入参
 */
export function applyResults(
  results: CatalogReviewBatchResult[], action: CatalogReviewAction,
  outcomes: ReadonlyMap<string, ItemOutcome>, versions: ReadonlyMap<string, number>,
): { outcomes: Map<string, ItemOutcome>; versions: Map<string, number> } {
  const o = new Map(outcomes)
  const v = new Map(versions)
  for (const r of results) {
    const key = itemKey(r)
    if (r.ok) {
      o.set(key, { kind: 'ok', action, status: STATUS_AFTER[action] })
      if (typeof r.version === 'number') v.set(r.table_name, Math.max(v.get(r.table_name) ?? 0, r.version))
    } else {
      o.set(key, isConflict(r) ? { kind: 'conflict', message: r.error } : { kind: 'error', message: r.error })
    }
  }
  return { outcomes: o, versions: v }
}

/** 一项现在显示的状态：审阅成功过的按结果，否则是队列里给的 */
export function statusOf(item: CatalogReviewItem, outcome: ItemOutcome | undefined): CatalogStatus {
  return outcome?.kind === 'ok' ? outcome.status : item.status
}

/**
 * 一批选中的项里，这个动作要发哪几条：确认发还不是已确认的，驳回发还不是已驳回的，撤销审阅只发这一页上审阅成功过的
 * （队列里本来都是推断，没审阅过的撤销了也没有变化）。冲突过的照旧可以再发
 */
export function batchTargets(items: CatalogReviewItem[], selected: ReadonlySet<string>, action: CatalogReviewAction,
  outcomes: ReadonlyMap<string, ItemOutcome>): CatalogReviewItem[] {
  return items.filter((i) => {
    if (!selected.has(itemKey(i))) return false
    const o = outcomes.get(itemKey(i))
    const status = statusOf(i, o)
    if (action === 'reset') return o?.kind === 'ok' && o.action !== 'reset'
    return action === 'confirm' ? status !== 'confirmed' : status !== 'rejected'
  })
}

/**
 * 表详情里改过这张表（单项审阅、编辑、助手的修改）：队列里这张表的项按最新的目录换状态——不再是推断的记成审阅成功，
 * 版本换成最新的。目录里已经没有的项不动（刷新后服务端不会再列）
 */
export function syncFromDetail(items: CatalogReviewItem[], table: string, notes: CatalogNotes, version: number,
  outcomes: ReadonlyMap<string, ItemOutcome>, versions: ReadonlyMap<string, number>,
): { outcomes: Map<string, ItemOutcome>; versions: Map<string, number> } {
  const o = new Map(outcomes)
  const v = new Map(versions)
  v.set(table, version)
  const slots = new Map(slotsOf(notes).map((s) => [s.path, s.item.status]))
  for (const i of items) {
    if (i.table_name !== table) continue
    const status = slots.get(i.path)
    if (!status) continue
    const key = itemKey(i)
    if (status === 'proposed') {
      // 回到推断：之前审阅成功（确认、驳回）过的记成撤销审阅；冲突的照旧标着
      const cur = o.get(key)
      if (cur?.kind === 'ok' && cur.status !== 'proposed') o.set(key, { kind: 'ok', action: 'reset', status: 'proposed' })
    } else {
      o.set(key, { kind: 'ok', action: status === 'rejected' ? 'reject' : 'confirm', status })
    }
  }
  return { outcomes: o, versions: v }
}

/** 百分比（四舍五入到整数）；分母为 0 时为 null */
export const percentOf = (n: number, total: number): number | null => (total > 0 ? Math.round((n / total) * 100) : null)
