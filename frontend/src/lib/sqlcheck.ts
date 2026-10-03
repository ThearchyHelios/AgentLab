import type { SqlCheckCode, SqlCheckItem, SqlCheckLevel } from '../types'
import { SQL_CHECK_LEVEL_LABEL, SQL_CHECK_RULE_LABEL, SQL_CHECK_TEXT } from './terms'

// ===========================================================================
// 基于数据目录的 SQL 检查（后端 data/sqlcheck.py）在界面上的读法：规则编号 → 中文规则名、级别 → 语义色，
// 以及从证据接口、助手自查、发布前检查的问题里读出一条检查。证据面板、运行时间线、助手面板、发布前检查共用。
// ===========================================================================

/** 七条规则的编号（服务端 CHECK_CODES），按严重程度大致排序 */
export const SQL_CHECK_CODES: readonly SqlCheckCode[] = [
  'fanout_sum', 'stock_summed', 'join_unconfirmed', 'ratio_aggregated', 'missing_valid_filter', 'unknown_code', 'wrong_date_column',
]

export const isSqlCheckCode = (code: unknown): code is SqlCheckCode =>
  typeof code === 'string' && (SQL_CHECK_CODES as readonly string[]).includes(code)

/** 级别：认不出的按「提示」（最轻的一档），不把拿不准的说重 */
export const sqlCheckLevelOf = (v: unknown): SqlCheckLevel => (v === 'error' || v === 'warning' ? v : 'info')

/** 规则的中文名；服务端新加、这里还不认识的规则统称「SQL 检查」，不露编号 */
export const sqlRuleLabel = (code: unknown): string => (isSqlCheckCode(code) ? SQL_CHECK_RULE_LABEL[code] : SQL_CHECK_TEXT.unknownRule)

/** 各级别的颜色（语义令牌） */
export const SQL_CHECK_TONE: Record<SqlCheckLevel, string> = {
  error: 'var(--st-failed)',
  warning: 'var(--st-waiting)',
  info: 'var(--accent)',
}

const LEVEL_RANK: Record<SqlCheckLevel, number> = { error: 0, warning: 1, info: 2 }

/** 错误在前，同级保持原来的顺序 */
export function sortSqlChecks<T extends { level?: unknown }>(list: T[]): T[] {
  return list.map((c, i) => ({ c, i }))
    .sort((a, b) => LEVEL_RANK[sqlCheckLevelOf(a.c.level)] - LEVEL_RANK[sqlCheckLevelOf(b.c.level)] || a.i - b.i)
    .map((x) => x.c)
}

/** 涉及的表和列：「orders.amount」「orders」 */
export const sqlCheckWhere = (c: Pick<SqlCheckItem, 'table' | 'column'>): string =>
  (c.table ? (c.column ? `${c.table}.${c.column}` : c.table) : (c.column ?? ''))

/** 从任意对象读一条检查（证据接口的 checks、助手自查和发布前检查的问题）；不是 SQL 检查的返回 null */
export function sqlCheckOf(v: unknown): SqlCheckItem | null {
  if (!v || typeof v !== 'object') return null
  const o = v as Record<string, unknown>
  if (!isSqlCheckCode(o.code)) return null
  const str = (x: unknown) => (typeof x === 'string' && x ? x : undefined)
  return {
    code: o.code,
    level: sqlCheckLevelOf(o.level),
    message: String(o.message ?? ''),
    table: str(o.table) ?? '',
    ...(str(o.column) ? { column: str(o.column) } : {}),
    ...(str(o.relation_id) ? { relation_id: str(o.relation_id) } : {}),
    ...(str(o.sql_excerpt) ? { sql_excerpt: str(o.sql_excerpt) } : {}),
  }
}

/** 一条检查写成一行（时间线的展开区、交给助手再修的清单）：「错误 · 一对多关联后重复计算：……（涉及 orders.amount）」 */
export function sqlCheckLine(c: SqlCheckItem): string {
  const where = sqlCheckWhere(c)
  return `${SQL_CHECK_LEVEL_LABEL[sqlCheckLevelOf(c.level)]} · ${sqlRuleLabel(c.code)}：${c.message}`
    + (where ? `（${SQL_CHECK_TEXT.where} ${where}）` : '')
}
