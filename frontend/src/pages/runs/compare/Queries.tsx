import { Fragment, useId, useMemo, useState } from 'react'
import { ChevronRight } from 'lucide-react'
import clsx from 'clsx'
import { RUN_COMPARE_TEXT as T } from '../../../lib/terms'
import type { RunCompareQuery } from '../../../types'
import { Block, StructureChip } from './parts'
import { queryItems, valueText } from './model'
import { diffSql, type DiffPart } from './sqlDiff'

/** 基准独有、本次独有的高亮：颜色之外，删去的划线、新加的下划线，色弱、灰度打印也分得出 */
const MARK = {
  del: {
    background: 'color-mix(in srgb, var(--err) 18%, transparent)', color: 'var(--text)',
    textDecoration: 'line-through', textDecorationColor: 'color-mix(in srgb, var(--err) 70%, transparent)',
  },
  add: {
    background: 'color-mix(in srgb, var(--ok) 18%, transparent)', color: 'var(--text)',
    textDecoration: 'underline', textDecorationColor: 'color-mix(in srgb, var(--ok) 70%, transparent)', textUnderlineOffset: '2px',
  },
} as const

/**
 * 查询区：每条查询的结构状态和行数。只参数变的列出变化的字面量；结构变的先写服务端说的差在哪（有的话），再并排两条
 * SQL（窄屏上下排），逐词标出各自独有的部分；新增、移除的只有一边。相同、只参数变的 SQL 默认收着，点开再看。
 * 快照读不出来、与哈希不一致的，照服务端的说明写在这一条上
 */
export function Queries({ queries }: { queries: RunCompareQuery[] }) {
  const id = useId()
  const items = useMemo(() => queryItems(queries), [queries])
  return (
    <Block id={`${id}-queries`} title={T.queries} count={T.queryCount(queries.length)} attr={{ 'data-compare-queries': '' }}>
      {!queries.length ? (
        <p className="py-4 text-center text-xs text-faint" data-compare-empty="queries">{T.queriesEmpty}</p>
      ) : (
        <ul className="divide-y">
          {items.map((it) => <QueryItem key={it.key} q={it.q} k={it.key} nth={it.nth} />)}
        </ul>
      )}
    </Block>
  )
}

function QueryItem({ q, k, nth }: { q: RunCompareQuery; k: string; nth: number | null }) {
  const sqlId = useId()
  const [showSql, setShowSql] = useState(false)
  const foldable = q.structure === 'same' || q.structure === 'params_only'
  const rows = q.structure === 'added' ? T.rowsTarget(T.rows(q.target_rows, q.target_truncated))
    : q.structure === 'removed' ? T.rowsBase(T.rows(q.base_rows, q.base_truncated))
      : T.rowsBoth(T.rows(q.base_rows, q.base_truncated), T.rows(q.target_rows, q.target_truncated))
  return (
    <li className="py-2.5 first:pt-0 last:pb-0" data-compare-query={k} data-structure={q.structure}>
      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
        <span className="min-w-0 text-xs font-medium [overflow-wrap:anywhere]" title={q.node_id}>{q.node_title || q.node_id}</span>
        {nth != null && <span className="text-2xs text-faint" data-compare-nth="">{T.nth(nth)}</span>}
        <StructureChip value={q.structure} />
        <span className="tnum text-2xs text-faint" data-compare-rows="">{rows}</span>
        {foldable && (
          <button type="button" className="btn btn-xs btn-ghost text-faint" data-compare-sql-toggle=""
                  aria-expanded={showSql} aria-controls={showSql ? sqlId : undefined} onClick={() => setShowSql((v) => !v)}>
            <ChevronRight size={11} aria-hidden className={clsx('transition-transform', showSql && 'rotate-90')} />
            {showSql ? T.hideSql : T.showSql}
          </button>
        )}
      </div>
      {q.problem && (
        <p className="mt-1 text-2xs leading-relaxed [overflow-wrap:anywhere]" style={{ color: 'var(--warn)' }} data-compare-query-problem="">
          {q.problem}
        </p>
      )}
      {q.structure === 'changed' && !!q.differences?.length && (
        <ul className="mt-1.5 list-disc space-y-0.5 pl-4 text-2xs leading-relaxed text-dim [overflow-wrap:anywhere]" data-compare-differences="">
          {q.differences.map((d, i) => <li key={i}>{d}</li>)}
        </ul>
      )}
      {q.structure === 'params_only' && !!q.literals.length && (
        <div className="mt-1.5">
          <div className="mb-0.5 text-2xs text-faint">{T.literals}</div>
          <ul className="space-y-0.5 text-2xs" data-compare-literals="">
            {q.literals.map((l, i) => (
              <li key={i} className="flex min-w-0 flex-wrap items-center gap-1.5">
                <code className="mono rounded px-1 [overflow-wrap:anywhere]" style={MARK.del}>{valueText(l.base)}</code>
                {/* 箭头只给眼睛看，读屏念「改为」 */}
                <span className="text-faint" aria-hidden>→</span>
                <span className="sr-only">{T.changedTo}</span>
                <code className="mono rounded px-1 [overflow-wrap:anywhere]" style={MARK.add}>{valueText(l.target)}</code>
              </li>
            ))}
          </ul>
        </div>
      )}
      {q.structure === 'changed' && <SideBySide base={q.base_sql} target={q.target_sql} />}
      {q.structure === 'added' && <SqlPane which="target" label={T.targetSql} sql={q.target_sql} />}
      {q.structure === 'removed' && <SqlPane which="base" label={T.baseSql} sql={q.base_sql} />}
      {foldable && showSql && (
        <div id={sqlId}>
          {q.structure === 'same'
            ? <SqlPane which="both" label={T.bothSql} sql={q.target_sql ?? q.base_sql} />
            : <SideBySide base={q.base_sql} target={q.target_sql} />}
        </div>
      )}
    </li>
  )
}

/**
 * 两条 SQL 并排，逐词标出各自独有的部分。有一边拿不到 SQL（快照读不出来、又没有执行时的参数）时不比：拿空串去比，
 * 另一边会整条被标成「独有」，像是整条新写的，其实只是不知道。这时两边原样摆着，拿不到的那一边写明无法读取
 */
function SideBySide({ base, target }: { base: string | null; target: string | null }) {
  if (base == null || target == null) {
    return (
      <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
        <SqlPane which="base" label={T.baseSql} sql={base} />
        <SqlPane which="target" label={T.targetSql} sql={target} />
      </div>
    )
  }
  return <Diffed base={base} target={target} />
}

function Diffed({ base, target }: { base: string; target: string }) {
  const diff = useMemo(() => diffSql(base, target), [base, target])
  return (
    <div className="mt-2">
      <div className="mb-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-2xs text-faint" aria-hidden>
        <span><mark className="rounded px-1" style={MARK.del}>{T.onlyBase}</mark></span>
        <span><mark className="rounded px-1" style={MARK.add}>{T.onlyTarget}</mark></span>
      </div>
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        <SqlPane which="base" label={T.baseSql} parts={diff.base} />
        <SqlPane which="target" label={T.targetSql} parts={diff.target} />
      </div>
      {diff.coarse && <p className="mt-1 text-2xs text-faint">{T.diffCoarse}</p>}
    </div>
  )
}

/**
 * 一条 SQL。折行显示，不横向撑开；太长时在框里纵向滚动，框能用键盘聚焦。sql 为 null（这一边拿不到 SQL）时
 * 框里写明无法读取，不摆一个空框
 */
function SqlPane({ which, label, sql, parts }: {
  which: 'base' | 'target' | 'both'; label: string; sql?: string | null; parts?: DiffPart[]
}) {
  const kind = which === 'base' ? 'del' : 'add'
  const missing = !parts && sql == null
  return (
    <figure className="m-0 mt-1.5 min-w-0">
      <figcaption className="mb-0.5 text-2xs text-faint">{label}</figcaption>
      <pre className={clsx('max-h-64 overflow-y-auto whitespace-pre-wrap rounded bg-bg px-2 py-1.5 text-2xs leading-relaxed [overflow-wrap:anywhere] focus-visible:outline-2 focus-visible:outline-[color:var(--accent)]',
             missing ? 'font-sans text-faint' : 'mono text-dim')}
           data-compare-sql={which} data-unreadable={missing ? '' : undefined} tabIndex={0} aria-label={label}>
        {parts
          ? parts.map((p, i) => (p.changed
            ? <mark key={i} data-diff={kind} className="rounded-sm" style={MARK[kind]}>{p.text}</mark>
            : <Fragment key={i}>{p.text}</Fragment>))
          : missing ? T.sqlUnreadable : sql}
      </pre>
    </figure>
  )
}
