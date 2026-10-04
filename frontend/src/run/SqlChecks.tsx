import { Info, OctagonX, TriangleAlert } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import clsx from 'clsx'
import type { SqlCheckItem, SqlCheckLevel } from '../types'
import { SQL_CHECK_LEVEL_HINT, SQL_CHECK_LEVEL_LABEL, SQL_CHECK_RULE_HINT, SQL_CHECK_TEXT } from '../lib/terms'
import { SQL_CHECK_TONE, isSqlCheckCode, sortSqlChecks, sqlCheckLevelOf, sqlCheckWhere, sqlRuleLabel } from '../lib/sqlcheck'

// ===========================================================================
// SQL 检查的结果：级别标识（错误、提醒、提示，四通道：图标、文字、颜色、边框）、中文规则名、给人看的说明、涉及的表和列、
// SQL 片段。证据面板的查询步骤和助手面板共用这一份。
// ===========================================================================

const LEVEL_ICON: Record<SqlCheckLevel, LucideIcon> = { error: OctagonX, warning: TriangleAlert, info: Info }

/** 级别标识：图标 + 「错误 / 提醒 / 提示」，悬停写明依据的确证程度 */
export function SqlLevelBadge({ level: raw, className }: { level: unknown; className?: string }) {
  const level = sqlCheckLevelOf(raw)
  const Icon = LEVEL_ICON[level]
  const tone = SQL_CHECK_TONE[level]
  return (
    <span className={clsx('inline-flex shrink-0 items-center gap-0.5 whitespace-nowrap rounded-full border px-1.5 text-2xs leading-[16px]', className)}
          style={{ color: tone, borderColor: `color-mix(in srgb, ${tone} 45%, var(--border))`, background: `color-mix(in srgb, ${tone} 8%, transparent)` }}
          title={SQL_CHECK_LEVEL_HINT[level]} data-sql-level={level}>
      <Icon size={10} aria-hidden />
      {SQL_CHECK_LEVEL_LABEL[level]}
    </span>
  )
}

/** 一条检查的正文：规则名、说明、涉及的表和列、SQL 片段 */
export function SqlCheckBody({ check: c, excerpt = true }: { check: SqlCheckItem; excerpt?: boolean }) {
  const where = sqlCheckWhere(c)
  return (
    <>
      <div className="flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-0.5">
        <SqlLevelBadge level={c.level} />
        <span className="font-medium text-fg" title={isSqlCheckCode(c.code) ? SQL_CHECK_RULE_HINT[c.code] : undefined} data-sql-rule="">
          {sqlRuleLabel(c.code)}
        </span>
        {where && (
          <span className="text-faint" data-sql-where={where}>
            {SQL_CHECK_TEXT.where} <span className="mono text-dim [overflow-wrap:anywhere]">{where}</span>
          </span>
        )}
      </div>
      {c.message && <p className="mt-0.5 leading-relaxed text-dim [overflow-wrap:anywhere]" data-sql-message="">{c.message}</p>}
      {excerpt && c.sql_excerpt && (
        <pre className="mono mt-1 max-h-20 overflow-auto whitespace-pre-wrap rounded bg-bg px-1.5 py-1 leading-relaxed text-dim [overflow-wrap:anywhere]"
             aria-label={SQL_CHECK_TEXT.excerpt} title={SQL_CHECK_TEXT.excerpt} data-sql-excerpt="">
          {c.sql_excerpt}
        </pre>
      )}
    </>
  )
}

/** 检查结果的清单：错误在前。每条一个淡色框，框的颜色跟级别走 */
export function SqlCheckList({ checks, className }: { checks: SqlCheckItem[]; className?: string }) {
  const list = sortSqlChecks(checks)
  return (
    <ul className={clsx('space-y-1 text-2xs', className)} aria-label={SQL_CHECK_TEXT.listLabel(list.length)} data-sql-checks={list.length}>
      {list.map((c, i) => {
        const tone = SQL_CHECK_TONE[sqlCheckLevelOf(c.level)]
        return (
          <li key={`${c.code}:${c.table}:${c.column ?? ''}:${i}`} className="rounded border px-2 py-1.5"
              style={{ borderColor: `color-mix(in srgb, ${tone} 30%, var(--border))`, background: `color-mix(in srgb, ${tone} 5%, transparent)` }}
              data-sql-check={c.code} data-level={sqlCheckLevelOf(c.level)}>
            <SqlCheckBody check={c} />
          </li>
        )
      })}
    </ul>
  )
}
