import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AlertTriangle, CheckCheck, RotateCcw, RotateCw, X } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../../api/client'
import type {
  CatalogDetail, CatalogReviewAction, CatalogReviewItem, CatalogReviewKind, CatalogReviewProgress, CatalogTableRow,
} from '../../types'
import { EmptyState, ErrorState, Notice, Skeleton, Spinner, confirmDialog, toast, useRadioGroup } from '../../components/ui'
import { errorMessage } from '../../lib/errors'
import { CATALOG_REVIEW_TEXT as RT } from '../../lib/terms'
import { StatusChip } from './parts'
import {
  REVIEW_BATCH_MAX, REVIEW_KINDS, REVIEW_PAGE, applyResults, batchTargets, itemKey, percentOf, statusOf, syncFromDetail,
} from './review'
import type { ItemOutcome } from './review'

// ===========================================================================
// 审阅队列（数据目录页的「审阅队列」视图，左栏）：跨表列出推断项，顺序由服务端排好（所在表的使用次数 × 这一项对数字的
// 影响 × 来源），每一项写为什么排在这里。可以按项（影响数字的项 / 名称和含义）和表筛选；多选后成批确认、驳回，审阅过的
// 原地标出结果（冲突的标出来），选中可以撤销审阅；刷新队列后审阅过的才移除。顶上是审阅进度：常用 20 张表、全部表、
// 影响数字的项各确认了多少。点开一项，右栏是所在表的详情（滚到那一项、标出来）和用过这张表的运行。
// ===========================================================================

export function ReviewQueue({ sourceId, rows, kind, table, activeKey, detailSync, onParams, onOpenItem, onChanged }: {
  sourceId: string
  /** 目录清单的各行：按表筛选的候选（有推断项的表）、表的中文名 */
  rows: CatalogTableRow[]
  kind: CatalogReviewKind
  /** 只看这一张表；空串是全部表 */
  table: string
  /** 右栏正打开的那一项（itemKey） */
  activeKey: string | null
  /** 右栏的表详情载入、改过之后的目录：队列里这张表的项跟着换状态 */
  detailSync: { seq: number; detail: CatalogDetail } | null
  onParams: (p: { kind?: CatalogReviewKind; table?: string }) => void
  onOpenItem: (item: CatalogReviewItem) => void
  /** 成批审阅写进了这些表：页面重取清单、正在看的表重新载入 */
  onChanged: (tables: string[]) => void
}) {
  const [items, setItems] = useState<CatalogReviewItem[] | null>(null)
  const [total, setTotal] = useState(0)
  const [loadError, setLoadError] = useState<unknown>(null)
  const [loadingMore, setLoadingMore] = useState(false)
  const [outcomes, setOutcomes] = useState<Map<string, ItemOutcome>>(() => new Map())
  const [versions, setVersions] = useState<Map<string, number>>(() => new Map())
  const [selected, setSelected] = useState<Set<string>>(() => new Set())
  const [acting, setActing] = useState<CatalogReviewAction | null>(null)
  const [summary, setSummary] = useState<{ action: CatalogReviewAction; ok: number; conflicts: number; failed: number } | null>(null)
  const [batchError, setBatchError] = useState<string | null>(null)
  const [progress, setProgress] = useState<CatalogReviewProgress | null>(null)
  const [progressError, setProgressError] = useState(false)
  const [reloadSeq, setReloadSeq] = useState(0)
  const loadSeq = useRef(0)
  const itemsRef = useRef(items)
  itemsRef.current = items
  const outcomesRef = useRef(outcomes)
  outcomesRef.current = outcomes
  const versionsRef = useRef(versions)
  versionsRef.current = versions
  const allRef = useRef<HTMLInputElement>(null)
  const radio = useRadioGroup(REVIEW_KINDS, kind, (k) => onParams({ kind: k }))

  const loadProgress = useCallback(() => {
    api.dataCatalog.reviewProgress(sourceId).then((p) => { setProgress(p); setProgressError(false) }, () => setProgressError(true))
  }, [sourceId])
  useEffect(() => { loadProgress() }, [loadProgress])

  // 第一页：筛选变了、点了刷新就从头取，审阅过的标记、选择、汇总都清掉
  useEffect(() => {
    const mine = ++loadSeq.current
    setItems(null)
    setLoadError(null)
    api.dataCatalog.reviewQueue(sourceId, { limit: REVIEW_PAGE, offset: 0, kind, table: table || null }).then((q) => {
      if (mine !== loadSeq.current) return
      setItems(q.items)
      setTotal(q.total)
      setOutcomes(new Map())
      setVersions(new Map())
      setSelected(new Set())
      setSummary(null)
      setBatchError(null)
    }, (e) => { if (mine === loadSeq.current) setLoadError(e) })
  }, [sourceId, kind, table, reloadSeq])

  const refresh = () => {
    setReloadSeq((n) => n + 1)
    loadProgress()
  }

  /**
   * 再载入一页。服务端的队列里已经没有审阅成功、不再是推断的项：偏移按「这一页上还在服务端队列里的项」数，
   * 不会跳过没载入的项
   */
  const loadMore = async () => {
    const list = itemsRef.current
    if (!list || loadingMore) return
    const gone = list.filter((i) => statusOf(i, outcomes.get(itemKey(i))) !== 'proposed').length
    setLoadingMore(true)
    try {
      const q = await api.dataCatalog.reviewQueue(sourceId, { limit: REVIEW_PAGE, offset: list.length - gone, kind, table: table || null })
      setItems((cur) => {
        const have = new Set((cur ?? []).map(itemKey))
        return [...(cur ?? []), ...q.items.filter((i) => !have.has(itemKey(i)))]
      })
      setTotal(q.total + gone)
    } catch (e) {
      toast.error(e)
    } finally {
      setLoadingMore(false)
    }
  }

  // 右栏的表详情载入、改过：这张表的项按最新的目录换状态
  const syncSeen = useRef(detailSync?.seq ?? 0)
  useEffect(() => {
    if (!detailSync || detailSync.seq === syncSeen.current) return
    syncSeen.current = detailSync.seq
    const list = itemsRef.current
    if (!list) return
    const d = detailSync.detail
    if (!list.some((i) => i.table_name === d.table_name)) return
    const next = syncFromDetail(list, d.table_name, d.notes, d.version, outcomesRef.current, versionsRef.current)
    setOutcomes(next.outcomes)
    setVersions(next.versions)
    loadProgress()
  }, [detailSync, loadProgress])

  const act = async (action: CatalogReviewAction) => {
    const list = itemsRef.current ?? []
    const targets = batchTargets(list, selected, action, outcomes)
    if (!targets.length || acting) return
    if (targets.length > REVIEW_BATCH_MAX) {
      setBatchError(RT.tooMany(REVIEW_BATCH_MAX))
      return
    }
    if (action !== 'reset') {
      const ok = await confirmDialog(action === 'confirm'
        ? { title: RT.confirmTitle(targets.length), consequences: RT.confirmConsequences, confirmLabel: RT.confirmAction(targets.length) }
        : { title: RT.rejectTitle(targets.length), consequences: RT.rejectConsequences, confirmLabel: RT.rejectAction(targets.length), danger: true })
      if (!ok) return
    }
    setActing(action)
    setBatchError(null)
    try {
      const { results } = await api.dataCatalog.reviewBatch(sourceId, targets.map((i) => ({
        table_name: i.table_name, path: i.path, action, if_version: versions.get(i.table_name) ?? i.version,
      })))
      const next = applyResults(results, action, outcomes, versions)
      setOutcomes(next.outcomes)
      setVersions(next.versions)
      setSelected(new Set())
      const ok = results.filter((r) => r.ok)
      const conflicts = results.filter((r) => !r.ok && next.outcomes.get(itemKey(r))?.kind === 'conflict').length
      setSummary({ action, ok: ok.length, conflicts, failed: results.length - ok.length - conflicts })
      if (ok.length) onChanged([...new Set(ok.map((r) => r.table_name))])
      loadProgress()
    } catch (e) {
      setBatchError(RT.failedAll(errorMessage(e)))
    } finally {
      setActing(null)
    }
  }

  // 按表筛选的候选：有推断项的表（使用次数多的在前），正在筛的那张不在里面也列上
  const tableOptions = useMemo(() => {
    const list = rows.filter((r) => r.counts.proposed > 0)
    if (table && !list.some((r) => r.table_name === table)) {
      const row = rows.find((r) => r.table_name === table)
      return [...list, row ?? ({ table_name: table, label: null, counts: { proposed: 0 } } as unknown as CatalogTableRow)]
    }
    return list
  }, [rows, table])
  const labelOf = useMemo(() => new Map(rows.map((r) => [r.table_name, r.label])), [rows])

  const loaded = items ?? []
  const picked = loaded.filter((i) => selected.has(itemKey(i))).length
  const all = loaded.length > 0 && picked === loaded.length
  useEffect(() => {
    if (allRef.current) allRef.current.indeterminate = picked > 0 && !all
  }, [picked, all])
  const toggleAll = () => setSelected(all ? new Set() : new Set(loaded.map(itemKey)))
  const toggle = useCallback((key: string) => setSelected((cur) => {
    const next = new Set(cur)
    if (next.has(key)) next.delete(key)
    else next.add(key)
    return next
  }), [])
  const counts = {
    confirm: batchTargets(loaded, selected, 'confirm', outcomes).length,
    reject: batchTargets(loaded, selected, 'reject', outcomes).length,
    reset: batchTargets(loaded, selected, 'reset', outcomes).length,
  }
  const filtered = kind !== 'all' || !!table
  const remaining = Math.max(0, total - loaded.length)

  return (
    <div className="flex min-h-0 flex-1 flex-col" data-review-queue="" aria-label={RT.queueLabel}>
      <ProgressBlock progress={progress} failed={progressError} />
      <div className="space-y-2 border-b px-3 py-2.5">
        <div role="radiogroup" aria-label={RT.kindLabel} className="flex flex-wrap gap-1" data-review-kinds={kind}>
          {REVIEW_KINDS.map((k) => (
            <button key={k} type="button" {...radio(k)} title={RT.kindHint[k]} onClick={() => onParams({ kind: k })} data-review-kind={k}
                    className={clsx(
                      'inline-flex items-center rounded-md border px-2 py-0.5 text-2xs outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]',
                      kind === k ? 'border-[var(--accent)] bg-accent-soft text-fg' : 'text-dim hover:bg-hover hover:text-fg',
                    )}>
              {RT.kind[k]}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2">
          <label className="sr-only" htmlFor="catalog-review-table">{RT.tableLabel}</label>
          <select id="catalog-review-table" className="field min-w-0 flex-1 !py-0.5 !text-2xs" value={table}
                  onChange={(e) => onParams({ table: e.target.value })} data-review-table="">
            <option value="">{RT.tableAll}</option>
            {tableOptions.map((r) => (
              <option key={r.table_name} value={r.table_name}>{RT.tableOption(r.label, r.table_name, r.counts.proposed)}</option>
            ))}
          </select>
          <button type="button" className="btn btn-sm btn-ghost shrink-0 !px-1.5" onClick={refresh} aria-label={RT.refresh} title={RT.refreshHint}
                  data-review-refresh="">
            <RotateCw size={12} aria-hidden />
          </button>
        </div>
        <div className="flex items-center gap-2 text-2xs text-faint">
          <input ref={allRef} type="checkbox" checked={all} onChange={toggleAll} disabled={!loaded.length}
                 aria-label={RT.selectAll} title={RT.selectAll} data-review-select-all="" />
          <span className="tnum min-w-0 flex-1 truncate" data-review-total="">
            {selected.size ? RT.selected(selected.size) : items ? RT.total(total) : RT.loading}
          </span>
          {selected.size > 0 && (
            <button type="button" className="inline-flex items-center gap-0.5 rounded px-1 hover:bg-hover hover:text-fg"
                    onClick={() => setSelected(new Set())} data-review-clear="">
              <X size={10} aria-hidden /> {RT.clearSelection}
            </button>
          )}
        </div>
      </div>

      {summary && (
        <p className={clsx('flex items-start gap-1.5 border-b px-3 py-2 text-2xs leading-relaxed', summary.conflicts || summary.failed ? 'text-[var(--warn)]' : 'text-dim')}
           role="status" data-review-summary="">
          {summary.conflicts || summary.failed
            ? <AlertTriangle size={11} className="mt-0.5 shrink-0" aria-hidden />
            : <CheckCheck size={11} className="mt-0.5 shrink-0" style={{ color: 'var(--st-done)' }} aria-hidden />}
          <span className="min-w-0">{RT.summary(summary.action, summary.ok, summary.conflicts, summary.failed)}</span>
        </p>
      )}
      {batchError && <div className="border-b px-3 py-2"><Notice tone="err" attr={{ 'data-review-batch-error': '' }}>{batchError}</Notice></div>}

      <div className="relative min-h-0 flex-1 overflow-y-auto">
        {loadError
          ? <div className="p-3"><ErrorState error={loadError} onRetry={refresh} compact /></div>
          : !items
            ? <div className="p-3"><Skeleton rows={6} height={44} gap={8} /></div>
            : !items.length
              ? (
                <EmptyState icon={<CheckCheck size={20} />} title={filtered ? RT.emptyFiltered : RT.empty}
                            body={filtered ? RT.emptyFilteredBody : RT.emptyBody} offline={false} className="!py-10" />
              )
              : (
                <ul aria-label={RT.queueLabel}>
                  {items.map((i) => {
                    const key = itemKey(i)
                    return (
                      <QueueRow key={key} item={i} itemKeyText={key} tableLabel={labelOf.get(i.table_name) ?? null}
                                outcome={outcomes.get(key)} checked={selected.has(key)} active={activeKey === key}
                                onToggle={toggle} onOpen={onOpenItem} />
                    )
                  })}
                </ul>
              )}
        {items && remaining > 0 && (
          <div className="border-t px-3 py-2">
            <button type="button" className="btn btn-sm w-full" onClick={() => void loadMore()} disabled={loadingMore} data-review-more="">
              {loadingMore ? <><Spinner size={11} /> {RT.loading}</> : RT.more(Math.min(REVIEW_PAGE, remaining))}
            </button>
          </div>
        )}
      </div>

      {selected.size > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 border-t px-3 py-2" data-review-batch="">
          <span className="tnum text-2xs text-faint">{RT.selected(selected.size)}</span>
          <span className="flex-1" />
          <button type="button" className="btn btn-sm btn-primary" disabled={!counts.confirm || !!acting} onClick={() => void act('confirm')}
                  data-review-action="confirm">
            {acting === 'confirm' ? <Spinner size={11} /> : null}{RT.confirm(counts.confirm)}
          </button>
          <button type="button" className="btn btn-sm" disabled={!counts.reject || !!acting} onClick={() => void act('reject')}
                  data-review-action="reject">
            {acting === 'reject' ? <Spinner size={11} /> : null}{RT.reject(counts.reject)}
          </button>
          <button type="button" className="btn btn-sm btn-ghost" disabled={!counts.reset || !!acting} onClick={() => void act('reset')}
                  title={RT.resetHint} data-review-action="reset">
            {acting === 'reset' ? <Spinner size={11} /> : <RotateCcw size={11} aria-hidden />} {RT.reset(counts.reset)}
          </button>
        </div>
      )}
    </div>
  )
}

/** 队列里的一行：复选框、位置和所在表、值、说明、为什么排在这里、来源和状态（审阅过的写结果）、所在表的使用次数 */
const QueueRow = memo(function QueueRow({ item: i, itemKeyText, tableLabel, outcome, checked, active, onToggle, onOpen }: {
  item: CatalogReviewItem
  itemKeyText: string
  tableLabel: string | null
  outcome: ItemOutcome | undefined
  checked: boolean
  active: boolean
  onToggle: (key: string) => void
  onOpen: (item: CatalogReviewItem) => void
}) {
  const status = statusOf(i, outcome)
  const where = i.field_label || i.path
  const tableText = tableLabel ? `${tableLabel}（${i.table_name}）` : i.table_name
  const mono = i.field === 'valid_filter' || i.field === 'keys'
  return (
    <li className={clsx('relative flex items-stretch border-b border-hairline', active ? 'bg-accent-soft' : 'hover:bg-hover')}
        data-review-item={`${i.table_name}::${i.path}`} data-review-result={outcome?.kind}>
      {active && <span className="absolute inset-y-0 left-0 w-0.5 bg-[var(--accent)]" aria-hidden />}
      <span className="flex shrink-0 items-start pl-3 pr-1 pt-2.5">
        <input type="checkbox" checked={checked} onChange={() => onToggle(itemKeyText)} aria-label={RT.select(where, i.table_name)}
               data-review-select="" />
      </span>
      <button type="button" className="min-w-0 flex-1 px-2 py-2 text-left outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--accent)]"
              onClick={() => onOpen(i)} aria-current={active ? 'true' : undefined} title={RT.open(where, i.table_name)} data-review-open="">
        <span className="flex items-baseline gap-2">
          <span className="min-w-0 flex-1 truncate text-xs font-medium text-fg">{where}</span>
          <span className="tnum shrink-0 text-2xs text-faint" title={RT.usageHint}>{RT.usage(i.table_usage)}</span>
        </span>
        <span className="mt-0.5 block truncate text-2xs text-faint" title={tableText}>{tableText}</span>
        <span className={clsx('mt-1 block break-words text-2xs text-dim', mono && 'mono', status === 'rejected' && 'line-through')}
              data-review-value="">{i.display || '—'}</span>
        {i.note && <span className="mt-0.5 block break-words text-2xs leading-relaxed text-faint" data-review-note="">{i.note}</span>}
        {i.reasons.length > 0 && (
          <span className="mt-1 block text-2xs leading-relaxed text-faint" data-review-reasons="">
            <span className="sr-only">{RT.reasonsLabel}：</span>{i.reasons.join(' · ')}
          </span>
        )}
        <span className="mt-1 flex flex-wrap items-center gap-1.5" data-review-outcome="">
          <StatusChip status={status} source={i.source} />
          {outcome?.kind === 'ok' && <span className="text-2xs" style={{ color: 'var(--st-done)' }}>{RT.done[outcome.action]}</span>}
          {outcome?.kind === 'conflict' && (
            <span className="text-2xs text-[var(--warn)]" title={outcome.message ?? undefined}>{RT.conflict}</span>
          )}
          {outcome?.kind === 'error' && (
            <span className="text-2xs text-[var(--err)]">{outcome.message ? `${RT.failed}：${outcome.message}` : RT.failed}</span>
          )}
        </span>
      </button>
    </li>
  )
})

/** 审阅进度：常用 20 张表、全部表、影响数字的项。数字写在字里，色条只给眼睛看 */
function ProgressBlock({ progress, failed }: { progress: CatalogReviewProgress | null; failed: boolean }) {
  if (!progress) {
    return (
      <div className="border-b px-3 py-2 text-2xs text-faint" data-review-progress={failed ? 'error' : 'loading'}>
        {failed ? RT.progressFailed : <Skeleton rows={2} height={10} gap={6} />}
      </div>
    )
  }
  const { top, all, facts } = progress
  const topPct = percentOf(top.items_confirmed, top.items_total)
  const allPct = percentOf(all.items_confirmed, all.items_total)
  const factsPct = percentOf(facts.confirmed, facts.proposed + facts.confirmed)
  return (
    <section className="space-y-1.5 border-b px-3 py-2.5" aria-label={RT.progressTitle} data-review-progress="">
      <ProgressLine label={RT.progressTop(progress.top_n)} hint={RT.progressTopHint(progress.top_n)} pct={topPct}
                    text={`${RT.confirmedPct(topPct)} · ${RT.itemsOf(top.items_confirmed, top.items_total)}`} attr="top" />
      <ProgressLine label={RT.progressAll} pct={allPct}
                    text={`${RT.confirmedPct(allPct)} · ${RT.allDetail(all.items_proposed, all.items_rejected)}`} attr="all" />
      <ProgressLine label={RT.progressFacts} hint={RT.progressFactsHint} pct={factsPct}
                    text={RT.factsDetail(facts.proposed, facts.confirmed)} attr="facts" warn={facts.proposed > 0} />
    </section>
  )
}

function ProgressLine({ label, hint, pct, text, attr, warn = false }: {
  label: string; hint?: string; pct: number | null; text: string; attr: string; warn?: boolean
}) {
  return (
    <div className="text-2xs" data-review-progress-line={attr} {...{ [`data-review-progress-${attr}`]: '' }}>
      <div className="flex items-baseline gap-2">
        <span className="shrink-0 font-medium text-dim" title={hint}>{label}</span>
        <span className={clsx('tnum min-w-0 flex-1 truncate text-right', warn ? 'text-[var(--st-waiting)]' : 'text-faint')} title={text}>{text}</span>
      </div>
      <div className="mt-0.5 h-1 overflow-hidden rounded-full bg-hover" aria-hidden>
        <div className="h-full rounded-full bg-[var(--st-done)]" style={{ width: `${pct ?? 0}%` }} />
      </div>
    </div>
  )
}

/** 审阅视图里没打开项时的右栏：怎么排的序、点开能看到什么 */
export function ReviewOverview() {
  return (
    <div className="relative min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto max-w-xl space-y-3 p-6" data-review-overview="">
        <h2 className="text-sm font-semibold">{RT.overviewTitle}</h2>
        <p className="text-xs leading-relaxed text-dim">{RT.overviewBody}</p>
        <p className="tnum text-2xs text-faint">{RT.kindHint.facts}</p>
      </div>
    </div>
  )
}
