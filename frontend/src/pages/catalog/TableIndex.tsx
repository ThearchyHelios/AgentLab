import { memo, useCallback, useEffect, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent, MouseEvent as ReactMouseEvent } from 'react'
import { AlertTriangle, History, Info, Plus, ScanSearch, Search, Sparkles, X } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../../api/client'
import type { CatalogProfileTaskStatus, CatalogSearchItem, CatalogTableRow } from '../../types'
import { Spinner, toast, useRadioGroup, useTicker } from '../../components/ui'
import { formatNumber } from '../../lib/format'
import {
  CATALOG_ABSENT_TEXT as AT, CATALOG_DRAFT_TEXT as DT, CATALOG_KIND_LABEL, CATALOG_SCOPE_TEXT as ScT, CATALOG_TEXT as CT,
  CATALOG_UI_TEXT as UT, PROFILE_TEXT as PT,
} from '../../lib/terms'
import { CountBadges } from './parts'
import { LIST_FILTERS, LIST_SORTS, progressOf } from './model'
import { lastBlankInfo, taskReason } from './draftJob'
import { profileCell } from './profile'
import type { ListFilter, ListSort } from './model'

// ===========================================================================
// 表清单（数据目录页的左栏）：搜索、按审阅进度筛选、排序、多选后批量起草。
// 一行两层：上面是中文名和各状态的项数，下面是表名、类型、关联数和使用次数；还没填中文名的表上面直接写表名，
// 下面淡淡地写「未填写中文名」——一屏几十行「未填写中文名」认不出是哪张表。上次起草失败、部分完成的表再多一行，
// 写明原因（全文在悬停里，点开表还有「重新起草这张表」）；上次起草有留空列（模型拿不准）的表另有一行淡色的轻提示，
// 不是失败标记。键盘：↑↓ 在行之间移动，Home / End 到两头，Enter 打开；复选框按住 Shift 点选可以连选一段。
//
// 开了数据剖析的源（或者剖析过的表）第二行多一格「剖析」：未剖析 / 3 天前剖析 / 剖析部分完成 / 剖析失败 / 剖析中 / 排队剖析，
// 需要剖析的（从没剖析过、上次没做完、之后出现了新的待核实关系）用提醒色，悬停写上次剖析的时间和原因。筛选组下面另有
// 「需要剖析」开关，和审阅进度的筛选叠加（审阅进度的四项数字照旧按审阅算），口径同剖析范围里的「需要剖析的表」。
//
// 几千张表的库（next-round 03 A1、A2）：清单只列探查范围内的表，一次画 PAGE 行，「显示更多」再画下一批（不引入虚拟列表）；
// 表结构里没有的表按服务端给的原因分开说（表已不在、在探查范围外、读不到结构……），只有真删除才说「表已不在」。有范围外的
// 表时，搜索词同时在对象索引里找，范围外的命中列在清单下面，可以「加入范围」。
// ===========================================================================

/** 清单一次画几行 */
const PAGE = 200

export function TableIndex({
  rows, total, counts, query, onQuery, filter, onFilter, usedOnly = false, onUsedOnly, sort, onSort, selected, onSelected, active, onOpen,
  onDraft, onProfile, profileOn = false, profileLive, dueOnly = false, onDueOnly, dueTotal = 0, sourceId, outside = 0, onAdded,
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
  /** 只看运行中查询过的表（从顶部摘要点进来）：筛选组下面挂一个能取消的标签 */
  usedOnly?: boolean
  onUsedOnly?: (v: boolean) => void
  sort: ListSort
  onSort: (s: ListSort) => void
  selected: Set<string>
  onSelected: (next: Set<string>) => void
  active: string | undefined
  onOpen: (table: string) => void
  onDraft: () => void
  /** 剖析所选（数据剖析弹窗，范围默认是已选的表） */
  onProfile?: () => void
  /** 这个数据源开了数据剖析：每行写「剖析」一格，给「需要剖析」开关 */
  profileOn?: boolean
  /** 页面正跟着的剖析任务里各表的状态（排队、剖析中），比清单新 */
  profileLive?: Map<string, CatalogProfileTaskStatus>
  /** 只看需要剖析的表 */
  dueOnly?: boolean
  onDueOnly?: (v: boolean) => void
  /** 需要剖析的表有几张（按搜索词算） */
  dueTotal?: number
  /** 这个数据源（在对象索引里找范围外的表、加入范围用） */
  sourceId?: string
  /** 探查范围外有几张表：有的话搜索词同时在对象索引里找 */
  outside?: number
  /** 加入范围之后（清单要重取） */
  onAdded?: () => void
}) {
  const radio = useRadioGroup(LIST_FILTERS, filter, onFilter)
  const listRef = useRef<HTMLUListElement>(null)
  const allRef = useRef<HTMLInputElement>(null)
  const lastClicked = useRef<number | null>(null)
  const picked = rows.filter((r) => selected.has(r.table_name)).length
  const all = rows.length > 0 && picked === rows.length
  // 分批画：换了搜索词、筛选、排序就回到第一批；打开的那张在后面的批次里时多画到它
  const [limit, setLimit] = useState(PAGE)
  useEffect(() => { setLimit(PAGE) }, [query, filter, sort, usedOnly, dueOnly])
  useEffect(() => {
    if (!active) return
    const i = rows.findIndex((r) => r.table_name === active)
    if (i >= limit) setLimit(Math.ceil((i + 1) / PAGE) * PAGE)
  }, [active, rows])
  const shown = rows.length > limit ? rows.slice(0, limit) : rows
  // 「3 天前剖析」「12 分钟前剖析」隔一分钟重算一次
  const now = useTicker(60_000, profileOn)

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
    const n = listRef.current?.querySelectorAll('[data-catalog-open-row]').length ?? latest.current.rows.length
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
        {onDueOnly && (profileOn || dueOnly) && (
          <div className="flex">
            <button type="button" aria-pressed={dueOnly} onClick={() => onDueOnly(!dueOnly)} title={PT.dueFilterHint} data-catalog-profile-due={dueOnly ? 'on' : 'off'}
                    className={clsx('inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-2xs outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]',
                      dueOnly ? 'border-[var(--accent)] bg-accent-soft text-fg' : 'border-dashed text-dim hover:bg-hover hover:text-fg')}>
              <ScanSearch size={10} className={dueOnly ? 'text-[var(--accent)]' : 'text-faint'} aria-hidden />
              {PT.dueFilter}
              <span className="tnum text-faint">{formatNumber(dueTotal)}</span>
            </button>
          </div>
        )}
        {usedOnly && (
          <div className="flex" data-catalog-used-only="">
            <span className="inline-flex min-w-0 items-center gap-1 rounded-md border border-[var(--accent)] bg-accent-soft py-0.5 pl-2 pr-0.5 text-2xs text-fg"
                  title={UT.usedOnlyHint}>
              <History size={10} className="shrink-0 text-[var(--accent)]" aria-hidden />
              <span className="truncate">{UT.usedOnly}</span>
              {onUsedOnly && (
                <button type="button" className="rounded p-0.5 text-faint outline-none hover:bg-hover hover:text-fg focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
                        onClick={() => onUsedOnly(false)} aria-label={UT.usedOnlyClear} title={UT.usedOnlyClear} data-catalog-used-only-clear="">
                  <X size={10} aria-hidden />
                </button>
              )}
            </span>
          </div>
        )}
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
        {shown.map((r, i) => (
          <Row key={r.table_name} row={r} index={i} active={r.table_name === active} checked={selected.has(r.table_name)}
               onToggle={toggle} onOpen={onOpen} onKey={onRowKey} profileOn={profileOn} live={profileLive?.get(r.table_name) ?? null} now={now} />
        ))}
        {rows.length > shown.length && (
          <li className="border-b border-hairline px-3 py-2">
            <button type="button" className="btn btn-sm w-full tnum" onClick={() => setLimit((n) => n + PAGE)}
                    data-catalog-more={`${shown.length}/${rows.length}`}>
              {ScT.more(shown.length, rows.length)}
            </button>
          </li>
        )}
        {sourceId && outside > 0 && query.trim() && (
          <OutsideMatches sourceId={sourceId} query={query} onAdded={onAdded} />
        )}
      </ul>

      {selected.size > 0 && (
        <div className="flex items-center gap-2 border-t px-3 py-2">
          <button type="button" className="btn btn-primary btn-sm" onClick={onDraft} data-catalog-draft-selected="">
            <Sparkles size={11} aria-hidden /> {CT.draftSelected(selected.size)}
          </button>
          {onProfile && (
            <button type="button" className="btn btn-sm" onClick={onProfile} data-catalog-profile-selected="">
              <ScanSearch size={11} aria-hidden /> {PT.profileSelected(selected.size)}
            </button>
          )}
        </div>
      )}
    </div>
  )
}

const PROFILE_TONE = { due: 'var(--st-waiting)', bad: 'var(--err)', active: 'var(--accent)', quiet: undefined } as const

const Row = memo(function Row({ row: r, index, active, checked, onToggle, onOpen, onKey, profileOn, live, now }: {
  row: CatalogTableRow
  index: number
  active: boolean
  checked: boolean
  onToggle: (i: number, e: ReactMouseEvent<HTMLInputElement>) => void
  onOpen: (table: string) => void
  onKey: (i: number, e: ReactKeyboardEvent<HTMLButtonElement>) => void
  profileOn: boolean
  live: CatalogProfileTaskStatus | null
  now: number
}) {
  // 「剖析」一格：开了剖析的源每行都写；没开的源只写剖析过的表（以前开过，结论还在目录里）
  const cell = profileOn || r.last_profile || live ? profileCell(r, live, now) : null
  const progress = progressOf(r.counts)
  // 上次起草没写全：失败、部分完成各一种说法，后面接原因
  const last = r.last_draft && (r.last_draft.status === 'failed' || r.last_draft.status === 'partial') ? r.last_draft : null
  const lastLabel = last ? (last.status === 'failed' ? DT.lastFailed : DT.lastPartial) : ''
  const lastText = last ? (taskReason(last) ? DT.lastWith(lastLabel, taskReason(last)) : lastLabel) : ''
  // 上次起草有留空的列：表照样写完了，只是模型对这几列拿不准。淡色的轻提示，和失败标记分开。最近一次停止了、没有新写入时
  // 照旧按再之前那次写完的起草提示（悬停里写明）。表结构里已经没有的表起草不了：悬停里不建议重新起草
  const blankInfo = lastBlankInfo(r.last_draft)
  const blank = blankInfo.n
  const blankTitle = [DT.lastBlank(blank), blankInfo.earlier ? DT.lastBlankEarlier : '', r.in_schema ? DT.lastBlankAdvice : '']
    .filter(Boolean).join('。')
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
          {r.label
            ? <span className="min-w-0 flex-1 truncate text-xs font-medium text-fg" data-row-title="" data-row-label="">{r.label}</span>
            : <span className="mono min-w-0 flex-1 truncate text-xs font-medium text-fg" title={r.qualified} data-row-title="">{r.table_name}</span>}
          {progress === 'none'
            ? <span className="shrink-0 text-2xs text-faint">{CT.noCatalog}</span>
            : <CountBadges counts={r.counts} className="shrink-0" />}
        </span>
        <span className="mt-0.5 flex items-center gap-1.5 text-2xs text-faint">
          {r.label
            ? <span className="mono min-w-0 truncate" title={r.qualified}>{r.table_name}</span>
            : <span className="min-w-0 truncate" data-row-label="" data-row-no-label="">{CT.noLabel}</span>}
          {r.kind && <span className="shrink-0">· {CATALOG_KIND_LABEL[r.kind] ?? r.kind}</span>}
          {r.is_view && <span className="shrink-0">· {CT.view}</span>}
          {r.relations > 0 && <span className="shrink-0 tnum">· {CT.relationCount(r.relations)}</span>}
          <span className="flex-1" />
          {cell && (
            <span className="min-w-0 shrink truncate" title={cell.title} style={{ color: PROFILE_TONE[cell.tone] }} data-row-profile={cell.tone}>
              <span className="sr-only">{PT.title}：</span>{cell.text}
            </span>
          )}
          <span className="tnum shrink-0" title={CT.usageHint} data-row-usage={r.usage}>{CT.usage(r.usage)}</span>
        </span>
        {!r.in_schema && (
          // 为什么不在：服务端给的原因（老服务端不给时照旧说表结构中已没有）
          <span className="mt-0.5 flex items-center gap-1 text-2xs"
                style={{ color: r.absent_reason === 'out_of_scope' || r.absent_reason === 'unknown' ? 'var(--st-waiting)' : 'var(--warn)' }}
                data-row-missing={r.absent_reason ?? ''}>
            <AlertTriangle size={10} aria-hidden /> {(r.absent_reason && AT.label[r.absent_reason]) || CT.missing}
          </span>
        )}
        {last && (
          <span className="mt-0.5 flex min-w-0 items-center gap-1 text-2xs" title={lastText} data-row-last-draft={last.status}
                style={{ color: last.status === 'failed' ? 'var(--err)' : 'var(--warn)' }}>
            <AlertTriangle size={10} className="shrink-0" aria-hidden />
            <span className="min-w-0 truncate">{lastText}</span>
          </span>
        )}
        {blank > 0 && (
          <span className="mt-0.5 flex min-w-0 items-center gap-1 text-2xs text-faint" title={blankTitle}
                data-row-blank={blank}>
            <Info size={10} className="shrink-0" aria-hidden />
            <span className="min-w-0 truncate">{DT.lastBlank(blank)}</span>
          </span>
        )}
      </button>
    </li>
  )
})

/**
 * 搜索词在对象索引里的命中（探查范围外的那些）：每行一张，「加入范围」当场读结构、记进探查范围的显式列出，加完清单重取。
 * 停手 300 毫秒再查，旧的请求作废
 */
function OutsideMatches({ sourceId, query, onAdded }: { sourceId: string; query: string; onAdded?: () => void }) {
  const [items, setItems] = useState<CatalogSearchItem[] | null>(null)
  const [total, setTotal] = useState(0)
  const [failed, setFailed] = useState(false)
  const [adding, setAdding] = useState('')
  useEffect(() => {
    const ctl = new AbortController()
    setItems(null)
    setFailed(false)
    const timer = setTimeout(() => {
      api.dataCatalog.search(sourceId, query.trim(), { signal: ctl.signal })
        .then((got) => {
          const outside = got.items.filter((i) => !i.in_scope)
          setItems(outside)
          setTotal(Math.max(got.total - (got.items.length - outside.length), outside.length))
        })
        .catch(() => { if (!ctl.signal.aborted) setFailed(true) })
    }, 300)
    return () => { ctl.abort(); clearTimeout(timer) }
  }, [sourceId, query])

  const add = async (name: string) => {
    setAdding(name)
    try {
      const out = await api.dataCatalog.addToScope(sourceId, [name])
      if (out.added.length) toast.ok(ScT.added(out.added))
      for (const f of out.failed) toast.warn(ScT.readFailed(f.name, f.error))
      for (const r of out.refused) toast.warn(ScT.addFailed(r.name, r.reason))
      setItems((cur) => (cur ?? []).filter((i) => i.name !== name || !!out.refused.length))
      onAdded?.()
    } catch (e) {
      toast.error(e)
    } finally {
      setAdding('')
    }
  }

  if (failed) return <li className="px-3 py-2 text-2xs text-faint">{ScT.searchFailed}</li>
  if (items === null) return <li className="flex items-center gap-1.5 px-3 py-2 text-2xs text-faint"><Spinner size={10} /> {ScT.searching}</li>
  if (!items.length) return null
  return (
    <li className="border-b border-hairline" data-catalog-outside={items.length}>
      <p className="px-3 pb-1 pt-2.5 text-2xs font-medium text-dim" title={ScT.outsideHint}>{ScT.outsideHead(total)}</p>
      <ul>
        {items.map((i) => (
          <li key={i.name} className="flex items-center gap-2 px-3 py-1.5 text-2xs" data-catalog-outside-row={i.name}>
            <span className="min-w-0 flex-1">
              <span className="mono block truncate text-xs text-fg" title={i.qualified}>{i.name}</span>
              <span className="block truncate text-faint">
                {[i.comment, i.kind === 'view' ? ScT.view : '', i.est_rows ? ScT.estRows(i.est_rows) : ''].filter(Boolean).join(' · ')}
              </span>
            </span>
            <button type="button" className="btn btn-xs shrink-0" disabled={!!adding} onClick={() => void add(i.name)}
                    title={ScT.outsideHint} data-catalog-add-scope={i.name}>
              {adding === i.name ? <Spinner size={10} /> : <Plus size={10} aria-hidden />} {adding === i.name ? ScT.adding : ScT.add}
            </button>
          </li>
        ))}
      </ul>
      {total > items.length && <p className="px-3 pb-2 text-2xs text-faint">{ScT.outsideMore(total - items.length)}</p>}
    </li>
  )
}
