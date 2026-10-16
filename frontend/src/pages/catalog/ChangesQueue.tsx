import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertOctagon, AlertTriangle, CheckCheck, Info, RotateCw } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../../api/client'
import type { SourceChange, SourceChangeCategory, SourceChangeCounts, SourceChangeSeverity, SourceChangeStatusFilter } from '../../typesSource'
import { EmptyState, ErrorState, Skeleton, Spinner, toast, useRadioGroup } from '../../components/ui'
import { formatDateTime, formatRelative } from '../../lib/format'
import { SOURCE_CHANGE_TEXT as T } from '../../lib/terms'
import { CATEGORY_FILTERS, CHANGES_PAGE, STATUS_FILTERS, groupBySeverity } from './changes'
import type { ChangesParams } from './changes'

// ===========================================================================
// 数据目录页的「变化」视图（左栏）：这个数据源测到的结构变化、取值变化，按要处理 / 提醒 / 说明分组，组内按服务端排好的
// 先后（开着的：表的使用次数 × 严重程度；处理过的：最近测到的在前）。能按状态（开着的、处理过的）和类别（结构、取值）筛，
// 筛选记在地址里。点开一条，右栏是它的依据、影响面和处理（ChangeDetail）。
// ===========================================================================

/** 严重程度的图标和颜色（列表、详情同一套） */
export const SEVERITY_MARK: Record<SourceChangeSeverity, { Icon: typeof Info; color: string }> = {
  action: { Icon: AlertOctagon, color: 'var(--err)' },
  warning: { Icon: AlertTriangle, color: 'var(--warn)' },
  info: { Icon: Info, color: 'var(--accent)' },
}

export function ChangesQueue({ sourceId, params, activeId, refreshKey, onParams, onOpen, onCounts }: {
  sourceId: string
  params: ChangesParams
  /** 右栏正打开的那一条 */
  activeId: string | null
  /** 处理过一条（右栏）之后加一：重取这一页 */
  refreshKey: number
  onParams: (p: { status?: SourceChangeStatusFilter; category?: SourceChangeCategory | null }) => void
  onOpen: (change: SourceChange) => void
  /** 各级别的计数（视图标签上的「N 条要处理」跟着它） */
  onCounts?: (counts: SourceChangeCounts) => void
}) {
  const [items, setItems] = useState<SourceChange[] | null>(null)
  const [total, setTotal] = useState(0)
  const [loadError, setLoadError] = useState<unknown>(null)
  const [loadingMore, setLoadingMore] = useState(false)
  const [reloadSeq, setReloadSeq] = useState(0)
  const loadSeq = useRef(0)
  const onCountsRef = useRef(onCounts)
  onCountsRef.current = onCounts
  const { status, category } = params
  const statusRadio = useRadioGroup(STATUS_FILTERS, status, (s) => onParams({ status: s }))
  const catValue = category ?? 'all'
  const catRadio = useRadioGroup(CATEGORY_FILTERS, catValue, (c) => onParams({ category: c === 'all' ? null : c }))

  // 第一页：筛选变了、处理过一条、点了刷新就重取。处理过一条时不清空列表（右栏还开着那一条），取回来再换
  const shownKey = `${sourceId}|${status}|${category ?? ''}`
  const lastKey = useRef('')
  useEffect(() => {
    const mine = ++loadSeq.current
    if (lastKey.current !== shownKey) {
      lastKey.current = shownKey
      setItems(null)
    }
    setLoadError(null)
    api.sourceChanges.list(sourceId, { status, category, limit: CHANGES_PAGE }).then((r) => {
      if (mine !== loadSeq.current) return
      setItems(r.items)
      setTotal(r.total)
      onCountsRef.current?.(r.counts)
    }, (e) => { if (mine === loadSeq.current) setLoadError(e) })
  }, [sourceId, status, category, shownKey, refreshKey, reloadSeq])

  const loadMore = useCallback(async () => {
    const list = items ?? []
    if (loadingMore) return
    setLoadingMore(true)
    try {
      const r = await api.sourceChanges.list(sourceId, { status, category, limit: CHANGES_PAGE, offset: list.length })
      setItems((cur) => {
        const have = new Set((cur ?? []).map((c) => c.id))
        return [...(cur ?? []), ...r.items.filter((c) => !have.has(c.id))]
      })
      setTotal(r.total)
    } catch (e) {
      toast.error(e)
    } finally {
      setLoadingMore(false)
    }
  }, [items, loadingMore, sourceId, status, category])

  const filtered = !!category
  const groups = groupBySeverity(items ?? [])
  const remaining = Math.max(0, total - (items?.length ?? 0))

  return (
    <div className="flex min-h-0 flex-1 flex-col" data-changes-queue={status} data-changes-category={category ?? 'all'}>
      <div className="space-y-2 border-b px-3 py-2.5">
        <div className="flex items-center gap-2">
          <div role="radiogroup" aria-label={T.statusLabel} className="flex flex-wrap gap-1" data-changes-status-filter={status}>
            {STATUS_FILTERS.map((s) => (
              <button key={s} type="button" {...statusRadio(s)} onClick={() => onParams({ status: s })} data-changes-status={s}
                      className={chipClass(status === s)}>
                {T.statusFilter[s]}
              </button>
            ))}
          </div>
          <span className="flex-1" />
          <button type="button" className="btn btn-sm btn-ghost shrink-0 !px-1.5" onClick={() => setReloadSeq((n) => n + 1)}
                  aria-label={T.refresh} title={T.refresh} data-changes-refresh="">
            <RotateCw size={12} aria-hidden />
          </button>
        </div>
        <div role="radiogroup" aria-label={T.categoryLabel} className="flex flex-wrap gap-1" data-changes-category-filter={catValue}>
          {CATEGORY_FILTERS.map((c) => (
            <button key={c} type="button" {...catRadio(c)} onClick={() => onParams({ category: c === 'all' ? null : c })}
                    data-changes-cat={c} className={chipClass(catValue === c)}>
              {c === 'all' ? T.categoryAll : T.category[c]}
            </button>
          ))}
        </div>
        <p className="tnum text-2xs text-faint" data-changes-total="">{items ? T.total(total) : T.loading}</p>
      </div>

      <div className="relative min-h-0 flex-1 overflow-y-auto">
        {loadError
          ? <div className="p-3"><ErrorState error={loadError} onRetry={() => setReloadSeq((n) => n + 1)} compact /></div>
          : !items
            ? <div className="p-3"><Skeleton rows={6} height={40} gap={8} /></div>
            : !items.length
              ? (
                <EmptyState icon={<CheckCheck size={20} />} offline={false} className="!py-10"
                            title={status === 'handled' ? T.emptyHandled : filtered ? T.emptyFiltered : T.empty}
                            body={status === 'handled' || filtered ? undefined : T.emptyBody} />
              )
              : groups.map((g) => {
                const mark = SEVERITY_MARK[g.severity]
                const headId = `changes-group-${g.severity}`
                return (
                  <section key={g.severity} aria-labelledby={headId} data-change-group={g.severity}>
                    <h3 id={headId} className="sticky top-0 z-[1] flex items-center gap-1.5 border-b bg-panel px-3 py-1.5 text-2xs font-semibold"
                        title={T.severityHint[g.severity]}>
                      <mark.Icon size={11} style={{ color: mark.color }} aria-hidden />
                      <span className="tnum">{T.group(T.severity[g.severity], g.items.length)}</span>
                    </h3>
                    <ul>
                      {g.items.map((c) => <ChangeRow key={c.id} change={c} active={activeId === c.id} onOpen={onOpen} />)}
                    </ul>
                  </section>
                )
              })}
        {items && remaining > 0 && (
          <div className="border-t px-3 py-2">
            <button type="button" className="btn btn-sm w-full" onClick={() => void loadMore()} disabled={loadingMore} data-changes-more="">
              {loadingMore ? <><Spinner size={11} /> {T.loading}</> : T.more(Math.min(CHANGES_PAGE, remaining))}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}

const chipClass = (on: boolean) => clsx(
  'inline-flex items-center rounded-md border px-2 py-0.5 text-2xs outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]',
  on ? 'border-[var(--accent)] bg-accent-soft text-fg' : 'text-dim hover:bg-hover hover:text-fg',
)

/** 列表里的一条：服务端的一句说法、类别、状态（知道了的、处理过的写出来）、测到几次、什么时候 */
function ChangeRow({ change: c, active, onOpen }: { change: SourceChange; active: boolean; onOpen: (c: SourceChange) => void }) {
  const mark = SEVERITY_MARK[c.severity]
  return (
    <li className={clsx('relative border-b border-hairline', active ? 'bg-accent-soft' : 'hover:bg-hover')}
        data-change-item={c.id} data-change-kind={c.kind} data-change-severity={c.severity} data-change-status={c.status}>
      {active && <span className="absolute inset-y-0 left-0 w-0.5 bg-[var(--accent)]" aria-hidden />}
      <button type="button" onClick={() => onOpen(c)} aria-current={active ? 'true' : undefined} data-change-open=""
              className="flex w-full min-w-0 items-start gap-2 px-3 py-2 text-left outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--accent)]">
        <mark.Icon size={12} className="mt-0.5 shrink-0" style={{ color: mark.color }} aria-hidden />
        <span className="min-w-0 flex-1">
          <span className="sr-only">{T.severity[c.severity]}：</span>
          <span className="block break-words text-xs font-medium text-fg" data-change-summary="">{c.summary}</span>
          <span className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-2xs text-faint">
            <span className="chip !py-0">{T.category[c.category]}</span>
            {c.status !== 'open' && <span data-change-state="">{T.status[c.status]}</span>}
            {c.seen_count > 1 && <span className="tnum">{T.seen(c.seen_count)}</span>}
            <span className="tnum" title={formatDateTime(c.last_seen_at)}>{T.detectedAt(formatRelative(c.last_seen_at))}</span>
          </span>
        </span>
      </button>
    </li>
  )
}
