import type { SourceChange, SourceChangeCategory, SourceChangeSeverity, SourceChangeStatusFilter } from '../../typesSource'

// ===========================================================================
// 数据目录页「变化」视图的纯函数：地址里的筛选和打开的那一条、按严重程度分组。界面在 ChangesQueue.tsx、ChangeDetail.tsx。
//
// 顺序由服务端排好（开着的按「使用次数权重 × 严重程度」，处理过的最近测到的在前）；这里只按服务端给的 severity 分到三组里，
// 组内照原样，不另排。能做哪些动作、依据什么都在服务端给的变化上（actions、basis_text），这里不判。
// ===========================================================================

/** 要处理、提醒、说明：分组的先后 */
export const SEVERITY_ORDER: readonly SourceChangeSeverity[] = ['action', 'warning', 'info']
export const STATUS_FILTERS: readonly SourceChangeStatusFilter[] = ['pending', 'handled']
export const CATEGORY_FILTERS: readonly ('all' | SourceChangeCategory)[] = ['all', 'struct', 'value']
/** 一页几条（服务端一页最多 200） */
export const CHANGES_PAGE = 100

/**
 * 「变化」视图记在地址的查询参数里：view=changes；cstatus 状态筛选（开着的不写）；ccat 类别（全部不写）；change 打开的那一条。
 * 和清单、审阅队列的参数互不相干，各自只动自己的键
 */
export interface ChangesParams {
  status: SourceChangeStatusFilter
  category: SourceChangeCategory | null
  change: string
}

export const isChangesView = (p: URLSearchParams): boolean => p.get('view') === 'changes'

export function changesParamsOf(p: URLSearchParams): ChangesParams {
  const cat = p.get('ccat')
  return {
    status: p.get('cstatus') === 'handled' ? 'handled' : 'pending',
    category: cat === 'struct' || cat === 'value' ? cat : null,
    change: p.get('change') ?? '',
  }
}

/** 写「变化」视图的参数。open：切到「变化」视图（view=changes）；切走时由调用方用 writeReviewParams 换 view，再 clearChanges */
export function writeChangesParams(p: URLSearchParams, s: Partial<ChangesParams> & { open?: boolean }): URLSearchParams {
  const next = new URLSearchParams(p)
  const put = (k: string, v: string | null | undefined) => (v ? next.set(k, v) : next.delete(k))
  if (s.open) {
    next.set('view', 'changes')
    next.delete('item')
  }
  if ('status' in s) put('cstatus', s.status === 'handled' ? 'handled' : null)
  if ('category' in s) put('ccat', s.category)
  if ('change' in s) put('change', s.change)
  return next
}

/** 离开「变化」视图：它的几个参数都去掉 */
export function clearChanges(p: URLSearchParams): URLSearchParams {
  const next = new URLSearchParams(p)
  for (const k of ['cstatus', 'ccat', 'change']) next.delete(k)
  return next
}

/** 按服务端给的严重程度分组（要处理、提醒、说明），组内照服务端的顺序；空组不出 */
export function groupBySeverity(items: readonly SourceChange[]): { severity: SourceChangeSeverity; items: SourceChange[] }[] {
  return SEVERITY_ORDER
    .map((severity) => ({ severity, items: items.filter((c) => c.severity === severity) }))
    .filter((g) => g.items.length > 0)
}

/** 处理动作落到了目录上（清理、加入码值）：哪几张表的目录变了，页面据此重取清单、重新载入正在看的表 */
export function writtenTables(change: SourceChange, versions?: Record<string, number> | null): string[] {
  const out = new Set<string>()
  if (change.table && change.resolution?.catalog_version != null) out.add(change.table)
  for (const t of Object.keys(versions ?? change.resolution?.catalog_versions ?? {})) out.add(t)
  return [...out]
}
