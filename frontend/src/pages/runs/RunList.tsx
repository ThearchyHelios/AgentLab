import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { ChevronRight, History, Inbox, Search, X } from 'lucide-react'
import clsx from 'clsx'
import { EmptyState, ErrorState, Skeleton, Spinner, StatusBadge } from '../../components/ui'
import { STATUS, statusMeta, type StatusCode } from '../../lib/status'
import {
  formatCost, formatDateTime, formatDay, formatTime, formatTokens, parseServerTime, shortId,
} from '../../lib/format'
import { RUN_CLASS_LABEL, runClassLabel } from '../../lib/terms'
import { summarizeRun } from '../../run/decode'
import type { Approval, Run, Workflow } from '../../types'
import { explainRunError } from './explain'
import {
  LONG_WAIT_MS, SEARCH_PLACEHOLDER, SEGMENT_CODES, UNSAVED_HINT, ageMs, formatSpan, headlineMs, idTail,
  isLiveRun, isUnsaved, runName, runTiming, timingTitle, type ParsedQuery,
} from './model'
import { ClassChip, LiveElapsed, TierChip, useNow } from './parts'
import type { ApprovalQueue, RunList, StatusCounts } from './useRunsData'

// -------------------------------------------------------------------------
// 筛选条
// -------------------------------------------------------------------------

export function FilterBar({
  text, onText, parsed, runClass, onRunClass, workflowId, onWorkflow, workflows, dupNames,
}: {
  text: string
  onText: (v: string) => void
  parsed: ParsedQuery
  runClass: string
  onRunClass: (v: '' | 'formal' | 'exploratory') => void
  workflowId: string
  onWorkflow: (id: string) => void
  workflows: Workflow[]
  dupNames: Set<string>
}) {
  const options = useMemo(
    () => [...workflows].sort((a, b) => a.name.localeCompare(b.name, 'zh-CN')),
    [workflows],
  )
  // 链接里带着一个已经删掉的工作流：下拉里也得有它，否则选中项和实际筛选对不上
  const orphan = workflowId && !workflows.some((w) => w.id === workflowId)
  return (
    <div className="flex shrink-0 flex-col gap-1.5 px-3 pt-2.5">
      <div className="relative">
        <Search size={12} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-faint" aria-hidden />
        <input
          className="field pl-7 pr-7"
          type="search"
          placeholder={SEARCH_PLACEHOLDER}
          aria-label="搜索运行记录"
          value={text}
          onChange={(e) => onText(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Escape' && text) { e.preventDefault(); onText('') } }}
          data-runs-search=""
        />
        {text && (
          <button
            type="button"
            className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-0.5 text-faint hover:bg-hover hover:text-dim"
            aria-label="清空搜索"
            title="清空搜索"
            onClick={() => onText('')}
          >
            <X size={12} aria-hidden />
          </button>
        )}
      </div>
      <div className="flex gap-1.5">
        <select
          className="field w-[118px] shrink-0"
          aria-label="按运行类别筛选"
          value={runClass}
          onChange={(e) => onRunClass(e.target.value as '' | 'formal' | 'exploratory')}
          data-runs-class=""
        >
          <option value="">全部类别</option>
          <option value="formal">{RUN_CLASS_LABEL.formal}</option>
          <option value="exploratory">{RUN_CLASS_LABEL.exploratory}</option>
        </select>
        <select
          className="field min-w-0 flex-1"
          aria-label="按工作流筛选"
          value={workflowId}
          onChange={(e) => onWorkflow(e.target.value)}
          data-runs-workflow=""
        >
          <option value="">全部工作流</option>
          {orphan && <option value={workflowId}>已删除的工作流 {idTail(workflowId)}</option>}
          {options.map((w) => (
            <option key={w.id} value={w.id}>
              {w.name}{dupNames.has(w.name) ? `  ${idTail(w.id)}` : ''}
            </option>
          ))}
        </select>
      </div>
      {parsed.words.length > 0 && (
        <div className="text-2xs text-faint" aria-live="polite" data-runs-hint="">
          按状态「<span className="text-dim">{parsed.words.join('、')}</span>」筛选
          {parsed.q && <>，名称含「<span className="text-dim">{parsed.q}</span>」</>}
        </div>
      )}
    </div>
  )
}

// -------------------------------------------------------------------------
// 状态分段
// -------------------------------------------------------------------------

/**
 * 「全部」里的状态分段。文字和剪影都来自 lib/status；计数只给需要看一眼的几类，
 * 有数时才按状态色显示——正常态安静，异常态才醒目。
 */
export function StatusSegments({ active, onPick, counts }: {
  active: StatusCode[]
  onPick: (code: StatusCode | null) => void
  counts: StatusCounts
}) {
  const codes = SEGMENT_CODES.filter((c) => c !== 'queued' || (counts.counts.queued ?? 0) > 0 || active.includes(c))
  const all = !active.length
  return (
    <div className="flex shrink-0 flex-wrap gap-0.5 px-2.5 pb-2 pt-2" role="group" aria-label="按状态筛选">
      <Segment on={all} onClick={() => onPick(null)} data="all">全部</Segment>
      {codes.map((code) => {
        const meta = STATUS[code]
        const n = counts.counts[code]
        const on = active.length === 1 && active[0] === code
        const partial = !on && active.includes(code)
        return (
          <Segment key={code} on={on || partial} onClick={() => onPick(on ? null : code)} data={code}
                   title={meta.hint ?? meta.label}>
            {meta.short}
            {n != null && n > 0 && (
              <span className="tnum" style={{ color: meta.alert ? meta.color : undefined }}>
                {n}{counts.saturated ? '+' : ''}
              </span>
            )}
          </Segment>
        )
      })}
    </div>
  )
}

function Segment({ on, onClick, children, data, title }: {
  on: boolean; onClick: () => void; children: ReactNode; data: string; title?: string
}) {
  return (
    <button
      type="button"
      aria-pressed={on}
      title={title}
      data-segment={data}
      onClick={onClick}
      className={clsx(
        'inline-flex h-6 items-center gap-1 rounded-full border px-1.5 text-2xs transition-colors',
        on ? 'border-[var(--border-strong)] bg-hover text-fg' : 'border-transparent text-faint hover:bg-hover hover:text-dim',
      )}
    >
      {children}
    </button>
  )
}

// -------------------------------------------------------------------------
// 运行列表
// -------------------------------------------------------------------------

export function RunRows({
  list, codeOf, pendingOf, selectedId, onOpen, dupNames, emptyTitle, emptyBody, filtered, onClear,
}: {
  list: RunList
  codeOf: (run: Run) => StatusCode
  pendingOf: (runId: string) => Approval | undefined
  selectedId?: string
  onOpen: (id: string) => void
  dupNames: Set<string>
  emptyTitle: string
  emptyBody?: string
  /** 带着筛选条件：空了要说"没有匹配"，不能说"还没有运行记录" */
  filtered: string | null
  onClear: () => void
}) {
  const changed = useChangedRows(list.rows)
  const now = useNow(60_000)

  if (list.state === 'loading') {
    return <div className="p-3"><Skeleton rows={6} height={46} gap={8} /></div>
  }
  if (list.state === 'error') {
    return <ErrorState error={list.error} onRetry={() => void list.reload()} compact className="m-3" />
  }
  const rows = list.rows
  if (!rows.length) {
    return filtered
      ? (
        <EmptyState
          icon={<Search size={22} />}
          title="没有匹配的运行"
          body={filtered}
          offline={false}
          action={<button type="button" className="btn btn-sm" onClick={onClear}>清除筛选</button>}
        />
      )
      : <EmptyState icon={<History size={22} />} title={emptyTitle} body={emptyBody} />
  }

  // 按天分组，组头吸顶：翻到哪天一眼可见
  const groups: { day: string; runs: Run[] }[] = []
  for (const r of rows) {
    const day = formatDay(r.created_at ?? null, new Date(now))
    const g = groups[groups.length - 1]
    if (g && g.day === day) g.runs.push(r)
    else groups.push({ day, runs: [r] })
  }

  return (
    <div data-runs-list="">
      {groups.map((g) => (
        <section key={g.day} aria-label={g.day}>
          <h3 className="sticky top-0 z-10 flex items-center gap-2 border-b bg-panel px-3 py-1 text-2xs font-medium text-faint">
            {g.day}
            <span className="tnum font-normal">{g.runs.length}</span>
          </h3>
          {g.runs.map((run) => (
            <RunRow
              key={run.id}
              run={run}
              code={codeOf(run)}
              approval={run.status === 'interrupted' ? pendingOf(run.id) : undefined}
              selected={run.id === selectedId}
              dup={dupNames.has(run.workflow_name)}
              flash={changed.get(run.id)}
              now={now}
              onOpen={onOpen}
            />
          ))}
        </section>
      ))}
      <div className="flex items-center justify-center gap-2 px-3 py-3 text-2xs text-faint">
        <span className="tnum">已显示 {rows.length} 条</span>
        {list.hasMore && (
          <button type="button" className="btn btn-sm" disabled={list.loadingMore}
                  onClick={() => void list.loadMore()} data-runs-more="">
            {list.loadingMore && <Spinner size={11} />} 加载更多
          </button>
        )}
        {!list.hasMore && rows.length > 0 && <span>· 到底了</span>}
      </div>
    </div>
  )
}

/**
 * 实时刷新时哪几行的状态刚变了：给它们闪一下底色。值是第几次变化，行里那层
 * 闪光按它换 key——同一行接连变两次（停到审批上、又被批掉接着跑）会重新挂载、
 * 再闪一次。
 *
 * 摘掉的计时器不能跟着 rows 的下一次变化被清掉：跑完那一刻紧跟着还会回写一次
 * 用量和结束时间，清掉了就再没人摘，这一行从此一直挂着闪光。
 */
function useChangedRows(rows: Run[]): Map<string, number> {
  const prev = useRef(new Map<string, string>())
  const seq = useRef(0)
  const timer = useRef(0)
  const [changed, setChanged] = useState<Map<string, number>>(() => new Map())
  useEffect(() => {
    const hits: string[] = []
    const next = new Map<string, string>()
    for (const r of rows) {
      const before = prev.current.get(r.id)
      if (before && before !== r.status) hits.push(r.id)
      next.set(r.id, r.status)
    }
    prev.current = next
    if (!hits.length) return
    const n = ++seq.current
    setChanged((m) => {
      const out = new Map(m)
      for (const id of hits) out.set(id, n)
      return out
    })
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setChanged(new Map()), 1700)
  }, [rows])
  useEffect(() => () => window.clearTimeout(timer.current), [])
  return changed
}

function RunRow({ run, code, approval, selected, dup, flash, now, onOpen }: {
  run: Run; code: StatusCode; approval?: Approval; selected: boolean; dup: boolean
  /** 状态刚变过：第几次变化（换 key 重播闪光） */
  flash?: number; now: number; onOpen: (id: string) => void
}) {
  const meta = statusMeta(code)
  const timing = runTiming(run)
  const tier = (run.output as any)?._issuance?.tier as string | undefined
  const tokens = (run.usage?.total_tokens as number | undefined)
    ?? ((run.usage?.input_tokens ?? 0) + (run.usage?.output_tokens ?? 0) || undefined)
  const live = isLiveRun(run.status)
  // 左侧色条只给要人注意的两类：在跑的、等人的
  const bar = live ? 'var(--st-running)' : code === 'waiting' ? 'var(--st-waiting)' : null
  const unsaved = isUnsaved(run)

  return (
    <button
      type="button"
      onClick={() => onOpen(run.id)}
      data-run-id={run.id}
      data-status={code}
      aria-current={selected ? 'true' : undefined}
      className={clsx(
        'relative flex w-full flex-col gap-0.5 border-b px-3 py-2 text-left outline-none transition-colors',
        'hover:bg-hover focus-visible:bg-hover focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-[var(--accent)]',
        selected && 'bg-hover',
      )}
      style={selected ? { boxShadow: 'inset 0 0 0 1px var(--border-strong)' } : undefined}
    >
      {flash != null && (
        <span key={flash} aria-hidden className="runs-row-changed" style={{ ['--row-flash' as string]: meta.color }} />
      )}
      {bar && <span aria-hidden className="absolute inset-y-0 left-0 w-0.5" style={{ background: bar }} />}
      <div className="flex min-w-0 items-center gap-1.5">
        <StatusBadge status={code} size={13} animate={live} />
        <span
          className={clsx('min-w-0 truncate text-xs font-medium', unsaved ? 'text-dim' : 'text-fg')}
          title={unsaved ? UNSAVED_HINT : run.workflow_name}
        >
          {runName(run)}
        </span>
        {dup && run.workflow_id && (
          <span className="mono shrink-0 text-2xs text-faint" title={`工作流 id：${run.workflow_id}`}>
            {idTail(run.workflow_id)}
          </span>
        )}
        <span className="flex-1" />
        <ClassChip runClass={run.run_class} version={run.version} />
        <TierChip tier={tier} />
        <time
          className="tnum shrink-0 text-2xs text-faint"
          dateTime={parseServerTime(run.created_at ?? null)?.toISOString()}
          title={`发起于 ${formatDateTime(run.created_at ?? null)}`}
          data-run-time=""
        >
          {formatTime(run.created_at ?? null, new Date(now))}
        </time>
      </div>
      <RowContext run={run} code={code} approval={approval} now={now} />
      <div className="flex items-center gap-2 text-2xs text-faint">
        <span className="tnum" title={timingTitle(timing)} data-run-duration="">
          {live ? <LiveElapsed since={run.started_at ?? run.created_at} prefix="" /> : formatSpan(headlineMs(timing))}
          {timing.source === 'usage' && (timing.waitMs ?? 0) > 0 && (
            <span className="text-faint"> · 含等人 {formatSpan(timing.waitMs, { coarse: (timing.waitMs ?? 0) >= 3_600_000 })}</span>
          )}
        </span>
        {tokens ? <span className="tnum">{formatTokens(tokens, { compact: true })}</span> : null}
        {run.usage?.cost_usd ? <span className="tnum">{formatCost(Number(run.usage.cost_usd))}</span> : null}
        <span className="flex-1" />
        <span className="mono">{shortId(run.id)}</span>
      </div>
    </button>
  )
}

/** 第二行：最该知道的那一句。失败写原因，等人写等谁、等多久，成功写内容摘要 */
function RowContext({ run, code, approval, now }: {
  run: Run; code: StatusCode; approval?: Approval; now: number
}) {
  const line = 'min-w-0 truncate text-2xs leading-4'
  switch (code) {
    case 'failed': {
      const ex = explainRunError(run.error)
      return (
        <div className={line} style={{ color: 'var(--st-failed)' }} title={run.error ?? undefined} data-run-reason="">
          {run.error_node_id && <span className="mono opacity-80">{run.error_node_id} · </span>}
          {ex.title}
        </div>
      )
    }
    case 'waiting': {
      const age = ageMs(approval?.created_at, now)
      const long = age != null && age >= LONG_WAIT_MS
      return (
        <div className={line} style={{ color: 'var(--st-waiting)' }} title={approval?.title}>
          {approval ? <>等「{approval.node_label ?? approval.node_id}」审批</> : '等人工审批'}
          {age != null && <span className={clsx('tnum', long && 'font-medium')}> · 已等 {formatSpan(age, { coarse: true })}</span>}
          {approval?.title && <span className="text-faint"> · {approval.title}</span>}
        </div>
      )
    }
    case 'held':
    case 'suspended':
      return (
        <div className={line} style={{ color: 'var(--st-suspended)' }} title={run.error ?? undefined}>
          已挂起，可接着跑{run.error ? <span className="text-faint"> · {run.error}</span> : null}
        </div>
      )
    case 'running':
    case 'queued':
      return (
        <div className={line} style={{ color: 'var(--st-running)' }}>
          {code === 'queued' ? '排队中，等待开始' : '正在运行'}
        </div>
      )
    case 'cancelled':
      return <div className={clsx(line, 'text-faint')}>已取消{run.error && run.error !== '用户取消' ? ` · ${run.error}` : ''}</div>
    default: {
      const summary = summarizeRun(run.input, run.output)
      return summary
        ? <div className={clsx(line, 'text-dim')} title={summary}>{summary}</div>
        : null
    }
  }
}

// -------------------------------------------------------------------------
// 待审批
// -------------------------------------------------------------------------

const MODE_ASK: Record<string, string> = {
  approve: '要你决定：通过或驳回',
  input: '要你补一段输入',
  edit: '要你改定一份草稿',
}

/**
 * 全局待审批。最久的排最前：这是工作队列，等了 8 天的那条不能沉在底下。
 * 每行点进去就是那次运行的详情，审批卡就在时间线里。
 */
export function ApprovalRows({ queue, filter, onOpen, selectedId, dupNames }: {
  queue: ApprovalQueue
  filter: (a: Approval) => boolean
  onOpen: (runId: string) => void
  selectedId?: string
  dupNames: Set<string>
}) {
  const now = useNow(60_000)
  if (queue.state === 'loading' && !queue.items.length) {
    return <div className="p-3"><Skeleton rows={3} height={56} gap={8} /></div>
  }
  if (queue.state === 'error' && !queue.items.length) {
    return <ErrorState error={queue.error} onRetry={() => void queue.reload()} compact className="m-3" />
  }
  const items = queue.items.filter(filter).sort(
    (a, b) => (parseServerTime(a.created_at ?? null)?.getTime() ?? 0) - (parseServerTime(b.created_at ?? null)?.getTime() ?? 0),
  )
  if (!items.length) {
    return (
      <EmptyState
        icon={<Inbox size={22} />}
        title={queue.items.length ? '没有匹配的待审批' : '没有待处理的审批'}
        body={queue.items.length ? '换个筛选条件试试' : '停在人工审批上的运行会出现在这里，处理完就会离开这个列表。'}
      />
    )
  }
  return (
    <div data-approvals-list="">
      {items.map((a) => {
        const age = ageMs(a.created_at, now)
        const long = age != null && age >= LONG_WAIT_MS
        const stale = a.run_status && a.run_status !== 'interrupted'
        const unsaved = !a.workflow_id && (!a.workflow_name || a.workflow_name === '临时图')
        return (
          <button
            key={a.id}
            type="button"
            onClick={() => onOpen(a.run_id)}
            data-approval-id={a.id}
            data-run-id={a.run_id}
            aria-current={a.run_id === selectedId ? 'true' : undefined}
            className={clsx(
              'relative flex w-full flex-col gap-0.5 border-b py-2 pl-3.5 pr-3 text-left outline-none transition-colors',
              'hover:bg-hover focus-visible:bg-hover focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-[var(--accent)]',
              a.run_id === selectedId && 'bg-hover',
            )}
            style={a.run_id === selectedId ? { boxShadow: 'inset 0 0 0 1px var(--border-strong)' } : undefined}
          >
            <span aria-hidden className="absolute inset-y-0 left-0 w-0.5" style={{ background: 'var(--st-waiting)' }} />
            <div className="flex min-w-0 items-center gap-1.5">
              <StatusBadge status="waiting" size={13} />
              <span className="min-w-0 truncate text-xs font-medium text-fg">{a.title || '人工审批'}</span>
              <span className="flex-1" />
              <span
                className={clsx('tnum shrink-0 text-2xs', long ? 'font-medium' : 'text-faint')}
                style={long ? { color: 'var(--st-waiting)' } : undefined}
                title={`${formatDateTime(a.created_at ?? null)} 发起`}
                data-approval-age=""
              >
                已等 {formatSpan(age, { coarse: true })}
              </span>
            </div>
            <div className="min-w-0 truncate text-2xs text-dim">
              <span className={unsaved ? 'text-faint' : undefined}>
                {unsaved ? '未保存的工作流' : a.workflow_name ?? '—'}
              </span>
              {a.workflow_id && a.workflow_name && dupNames.has(a.workflow_name) && (
                <span className="mono text-faint"> {idTail(a.workflow_id)}</span>
              )}
              <span className="text-faint"> · 节点</span>「{a.node_label ?? a.node_id}」
            </div>
            <div className="flex items-center gap-2 text-2xs text-faint">
              <span className="tnum">{formatTime(a.created_at ?? null, new Date(now))} 发起</span>
              <span>· {runClassLabel(a.run_class ?? undefined)}</span>
              <span className="min-w-0 truncate">· {stale ? `运行已${statusMeta(a.run_status).label}` : MODE_ASK[a.mode] ?? ''}</span>
              <span className="flex-1" />
              <span className="flex shrink-0 items-center text-[var(--accent)]">
                去处理 <ChevronRight size={11} aria-hidden />
              </span>
            </div>
          </button>
        )
      })}
      <div className="px-3 py-3 text-center text-2xs text-faint">
        共 <span className="tnum">{items.length}</span> 条待审批 · 最久的排在最前
      </div>
    </div>
  )
}
