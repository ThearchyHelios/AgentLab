import { memo, useCallback, useEffect, useRef } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent, MouseEvent as ReactMouseEvent } from 'react'
import { AlertTriangle, Search, Sparkles, X } from 'lucide-react'
import clsx from 'clsx'
import type { CatalogTableRow } from '../../types'
import { useRadioGroup } from '../../components/ui'
import { formatNumber } from '../../lib/format'
import { CATALOG_KIND_LABEL, CATALOG_TEXT as CT } from '../../lib/terms'
import { CountBadges } from './parts'
import { LIST_FILTERS, LIST_SORTS, progressOf } from './model'
import type { ListFilter, ListSort } from './model'

// ===========================================================================
// 表清单（数据目录页的左栏）：搜索、按审阅进度筛选、排序、多选后批量起草。
// 一行两层：上面是中文名和各状态的项数，下面是表名、类型、关联数和使用次数。键盘：↑↓ 在行之间移动，
// Home / End 到两头，Enter 打开；复选框按住 Shift 点选可以连选一段。
// ===========================================================================

export function TableIndex({
  rows, total, counts, query, onQuery, filter, onFilter, sort, onSort, selected, onSelected, active, onOpen, onDraft,
}: {
  /** 筛选、排序后的行 */
  rows: CatalogTableRow[]
  total: number
  /** 各筛选项下的表数（按搜索词算） */
  counts: Record<ListFilter, number>
  query: string
  onQuery: (q: string) => void
  filter: ListFilter
  onFilter: (f: ListFilter) => void
  sort: ListSort
  onSort: (s: ListSort) => void
  selected: Set<string>
  onSelected: (next: Set<string>) => void
  active: string | undefined
  onOpen: (table: string) => void
  onDraft: () => void
}) {
  const radio = useRadioGroup(LIST_FILTERS, filter, onFilter)
  const listRef = useRef<HTMLUListElement>(null)
  const allRef = useRef<HTMLInputElement>(null)
  const lastClicked = useRef<number | null>(null)
  const picked = rows.filter((r) => selected.has(r.table_name)).length
  const all = rows.length > 0 && picked === rows.length

  useEffect(() => {
    if (allRef.current) allRef.current.indeterminate = picked > 0 && !all
  }, [picked, all])

  // 打开的表滚进视野（从详情里点「下一张」、从关联关系跳过来时）
  useEffect(() => {
    if (!active) return
    const el = listRef.current?.querySelector<HTMLElement>(`[data-catalog-row="${CSS.escape(active)}"]`)
    el?.scrollIntoView({ block: 'nearest' })
  }, [active])

  const toggleAll = () => {
    const next = new Set(selected)
    if (all) for (const r of rows) next.delete(r.table_name)
    else for (const r of rows) next.add(r.table_name)
    onSelected(next)
  }

  // 行是 memo 的：两个回调读最新的 rows、selected，自身保持不变，勾一行不会让 150 多行全部重画
  const latest = useRef({ rows, selected, onSelected })
  latest.current = { rows, selected, onSelected }

  const toggle = useCallback((i: number, e: ReactMouseEvent<HTMLInputElement>) => {
    const { rows: list, selected: sel, onSelected: set } = latest.current
    const name = list[i].table_name
    const next = new Set(sel)
    const on = !sel.has(name)
    // Shift 点选：从上一次点的那行到这一行，统一设成这一下的状态
    if (e.shiftKey && lastClicked.current != null && lastClicked.current < list.length) {
      const [a, b] = [Math.min(lastClicked.current, i), Math.max(lastClicked.current, i)]
      for (let k = a; k <= b; k++) {
        if (on) next.add(list[k].table_name)
        else next.delete(list[k].table_name)
      }
    } else if (on) next.add(name)
    else next.delete(name)
    lastClicked.current = i
    set(next)
  }, [])

  const onRowKey = useCallback((i: number, e: ReactKeyboardEvent<HTMLButtonElement>) => {
    const n = latest.current.rows.length
    let j = -1
    if (e.key === 'ArrowDown') j = Math.min(n - 1, i + 1)
    else if (e.key === 'ArrowUp') j = Math.max(0, i - 1)
    else if (e.key === 'Home') j = 0
    else if (e.key === 'End') j = n - 1
    if (j < 0 || e.altKey || e.metaKey || e.ctrlKey) return
    e.preventDefault()
    listRef.current?.querySelectorAll<HTMLButtonElement>('[data-catalog-open-row]')[j]?.focus()
  }, [])

  return (
    <div className="flex min-h-0 flex-1 flex-col" data-catalog-list="">
      <div className="space-y-2 border-b px-3 py-2.5">
        <div className="relative">
          <Search size={12} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-faint" aria-hidden />
          <input
            type="search"
            className="field pl-7"
            placeholder={CT.search}
            aria-label={CT.search}
            value={query}
            onChange={(e) => onQuery(e.target.value)}
            data-catalog-search=""
          />
        </div>
        <div role="radiogroup" aria-label={CT.filterLabel} className="flex flex-wrap gap-1" data-catalog-filter={filter}>
          {LIST_FILTERS.map((f) => (
            <button
              key={f}
              type="button"
              {...radio(f)}
              title={CT.filterHint[f]}
              onClick={() => onFilter(f)}
              data-filter={f}
              className={clsx(
                'inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-2xs outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]',
                filter === f ? 'border-[var(--accent)] bg-accent-soft text-fg' : 'text-dim hover:bg-hover hover:text-fg',
              )}
            >
              {CT.filter[f]}
              <span className="tnum text-faint">{formatNumber(counts[f])}</span>
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2 text-2xs text-faint">
          <input ref={allRef} type="checkbox" checked={all} onChange={toggleAll} disabled={!rows.length}
                 aria-label={CT.selectAll} title={CT.selectAll} data-catalog-select-all="" />
          <span className="tnum" data-catalog-count="">
            {selected.size ? CT.selected(selected.size) : CT.tableCount(rows.length, total)}
          </span>
          {selected.size > 0 && (
            <button type="button" className="inline-flex items-center gap-0.5 rounded px-1 hover:bg-hover hover:text-fg"
                    onClick={() => onSelected(new Set())} data-catalog-clear="">
              <X size={10} aria-hidden /> {CT.clearSelection}
            </button>
          )}
          <span className="flex-1" />
          <label className="sr-only" htmlFor="catalog-sort">{CT.sortLabel}</label>
          <select id="catalog-sort" className="field !w-auto !py-0.5 !text-2xs" value={sort}
                  onChange={(e) => onSort(e.target.value as ListSort)} data-catalog-sort="">
            {LIST_SORTS.map((s) => <option key={s} value={s}>{CT.sort[s]}</option>)}
          </select>
        </div>
      </div>

      <ul ref={listRef} className="relative min-h-0 flex-1 overflow-y-auto" aria-label={CT.tableCount(rows.length, total)}>
        {rows.map((r, i) => (
          <Row key={r.table_name} row={r} index={i} active={r.table_name === active} checked={selected.has(r.table_name)}
               onToggle={toggle} onOpen={onOpen} onKey={onRowKey} />
        ))}
      </ul>

      {selected.size > 0 && (
        <div className="flex items-center gap-2 border-t px-3 py-2">
          <button type="button" className="btn btn-primary btn-sm" onClick={onDraft} data-catalog-draft-selected="">
            <Sparkles size={11} aria-hidden /> {CT.draftSelected(selected.size)}
          </button>
        </div>
      )}
    </div>
  )
}

const Row = memo(function Row({ row: r, index, active, checked, onToggle, onOpen, onKey }: {
  row: CatalogTableRow
  index: number
  active: boolean
  checked: boolean
  onToggle: (i: number, e: ReactMouseEvent<HTMLInputElement>) => void
  onOpen: (table: string) => void
  onKey: (i: number, e: ReactKeyboardEvent<HTMLButtonElement>) => void
}) {
  const progress = progressOf(r.counts)
  return (
    <li
      className={clsx('relative flex items-stretch border-b border-hairline', active ? 'bg-accent-soft' : 'hover:bg-hover')}
      data-catalog-row={r.table_name}
      data-progress={progress}
    >
      {active && <span className="absolute inset-y-0 left-0 w-0.5 bg-[var(--accent)]" aria-hidden />}
      <span className="flex shrink-0 items-start pl-3 pr-1 pt-2.5">
        <input type="checkbox" checked={checked} onChange={() => {}} onClick={(e) => onToggle(index, e)}
               aria-label={CT.selectTable(r.table_name)} data-catalog-select={r.table_name} />
      </span>
      <button
        type="button"
        className="min-w-0 flex-1 px-2 py-2 text-left outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--accent)]"
        onClick={() => onOpen(r.table_name)}
        onKeyDown={(e) => onKey(index, e)}
        aria-current={active ? 'true' : undefined}
        data-catalog-open-row=""
      >
        <span className="flex items-center gap-2">
          <span className={clsx('min-w-0 flex-1 truncate text-xs', r.label ? 'font-medium text-fg' : 'text-faint')} data-row-label="">
            {r.label ?? CT.noLabel}
          </span>
          {progress === 'none'
            ? <span className="shrink-0 text-2xs text-faint">{CT.noCatalog}</span>
            : <CountBadges counts={r.counts} className="shrink-0" />}
        </span>
        <span className="mt-0.5 flex items-center gap-1.5 text-2xs text-faint">
          <span className="mono min-w-0 truncate" title={r.qualified}>{r.table_name}</span>
          {r.kind && <span className="shrink-0">· {CATALOG_KIND_LABEL[r.kind] ?? r.kind}</span>}
          {r.is_view && <span className="shrink-0">· {CT.view}</span>}
          {r.relations > 0 && <span className="shrink-0 tnum">· {CT.relationCount(r.relations)}</span>}
          <span className="flex-1" />
          <span className="tnum shrink-0" title={CT.usageHint} data-row-usage={r.usage}>{CT.usage(r.usage)}</span>
        </span>
        {!r.in_schema && (
          <span className="mt-0.5 flex items-center gap-1 text-2xs text-[var(--warn)]" data-row-missing="">
            <AlertTriangle size={10} aria-hidden /> {CT.missing}
          </span>
        )}
      </button>
    </li>
  )
})
