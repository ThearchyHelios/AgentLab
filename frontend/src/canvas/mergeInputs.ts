// ===========================================================================
// 合并查询节点的输入：哪些节点能当输入、新加一个输入时别名怎么起、连线时自动带出。
//
// 别名就是合并 SQL 里的表名，所以要起得有意义：先按这个查询的主表（FROM 后的第一张表，visits、orders），再按节点
// 标题里的英文，都没有就用 q1、q2……；不用节点 id（tool_1fpq0 这种写进 SQL 里谁也认不出）。规则和后端
// engine/merge_query.alias_problem 一致：英文字母、数字、下划线，不以数字开头，最多 32 个字符，不用 SQL 关键字，
// 不以 sqlite_ 开头；和已有的重名（不区分大小写）时加序号。
// ===========================================================================

/** 合法的别名（后端 merge_query._ALIAS）。关键字、sqlite_ 前缀另查 */
export const MERGE_ALIAS = /^[A-Za-z_][A-Za-z0-9_]{0,31}$/

/** SQLite 的关键字（后端 merge_query._SQLITE_KEYWORDS）：用作表名要加引号，别名不起这些 */
const KEYWORDS = new Set(`
abort action add after all alter always analyze and as asc attach autoincrement before begin between by cascade case
cast check collate column commit conflict constraint create cross current current_date current_time current_timestamp
database default deferrable deferred delete desc detach distinct do drop each else end escape except exclude exclusive
exists explain fail filter first following for foreign from full generated glob group groups having if ignore immediate
in index indexed initially inner insert instead intersect into is isnull join key last left like limit match
materialized natural no not nothing notnull null nulls of offset on or order others outer over partition plan pragma
preceding primary query raise range recursive references regexp reindex release rename replace restrict returning
right rollback row rows savepoint select set table temp temporary then ties to transaction trigger unbounded union
unique update using vacuum values view virtual when where window with without true false`.split(/\s+/).filter(Boolean))

interface QueryNodeLike {
  id: string
  data?: { nodeType?: string; label?: string; config?: Record<string, any> }
}

/** 能当合并输入的节点：选了数据库查询工具的「调用工具」节点，或者另一个合并查询 */
export function isQueryNode(n: QueryNodeLike | undefined): boolean {
  if (!n) return false
  const tool = String(n.data?.config?.tool ?? '')
  return n.data?.nodeType === 'merge' || (n.data?.nodeType === 'tool' && tool.startsWith('db_query__'))
}

/** 写成合法标识符：小写，别的字符换成下划线，去掉头尾和连续的下划线，数字开头补 t，截到 32 个字符。没有英文字母时为空 */
function identOf(text: string): string {
  const s = text.toLowerCase().replace(/[^a-z0-9_]+/g, '_').replace(/_+/g, '_').replace(/^_+|_+$/g, '')
  if (!/[a-z]/.test(s)) return ''
  return (/^[0-9]/.test(s) ? `t${s}` : s).slice(0, 32).replace(/_+$/, '')
}

/** SQL 里的主表：FROM 后的第一张表（去掉库名、引号）。子查询、取不到时为空 */
export function mainTableOf(sql: unknown): string {
  if (typeof sql !== 'string') return ''
  const text = sql.replace(/--[^\n]*/g, ' ').replace(/\/\*[\s\S]*?\*\//g, ' ')
  const m = text.match(/\bfrom\s+((?:[`"[]?[\w$㐀-鿿]+[`"\]]?\s*\.\s*)*)[`"[]?([\w$㐀-鿿]+)[`"\]]?/i)
  return m ? m[2] : ''
}

/** 这个节点的别名底稿：主表 → 标题里的英文 → 空 */
function aliasBase(n: QueryNodeLike | undefined): string {
  if (!n) return ''
  const fromSql = n.data?.nodeType === 'tool' ? identOf(mainTableOf(n.data?.config?.args?.sql)) : ''
  return fromSql || identOf(String(n.data?.label ?? ''))
}

/**
 * 新输入的别名：有意义的短名，合法、不是关键字、不和 taken（已用的别名，小写）重名。重名时加序号（visits_2）；
 * 什么都取不到时用 q1、q2……
 */
export function mergeAlias(n: QueryNodeLike | undefined, taken: Set<string>): string {
  let base = aliasBase(n)
  if (base && (KEYWORDS.has(base) || base.startsWith('sqlite_'))) base = `${base.replace(/^sqlite_/, '')}_q`.replace(/^_/, '')
  if (!base || !MERGE_ALIAS.test(base)) {
    for (let k = 1; ; k += 1) if (!taken.has(`q${k}`)) return `q${k}`
  }
  if (!taken.has(base)) return base
  for (let k = 2; ; k += 1) {
    const tail = `_${k}`
    const name = `${base.slice(0, 32 - tail.length)}${tail}`
    if (!taken.has(name)) return name
  }
}

/** config.inputs（{别名: 节点 id}）里已经有哪些别名（小写）、哪些节点 */
export function inputsOf(config: Record<string, any> | undefined): { aliases: Set<string>; nodes: Set<string>; value: Record<string, string> } {
  const raw = config?.inputs
  const value: Record<string, string> = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {}
  return {
    value,
    aliases: new Set(Object.keys(value).map((a) => a.toLowerCase())),
    nodes: new Set(Object.values(value).filter((v): v is string => typeof v === 'string')),
  }
}

/**
 * 连线时自动带出输入：从查询节点连到合并查询，且还不是它的输入，就加一行（别名按 mergeAlias 起）。用户照样可以
 * 改别名、删掉这一行。不是这种连线返回 null。和连线算同一步：撤销时一起退回
 */
export function connectMergeInput<T extends QueryNodeLike>(nodes: T[], source: string | null, target: string | null): T[] | null {
  if (!source || !target || source === target) return null
  const to = nodes.find((n) => n.id === target)
  const from = nodes.find((n) => n.id === source)
  if (to?.data?.nodeType !== 'merge' || !isQueryNode(from)) return null
  const { aliases, nodes: used, value } = inputsOf(to.data?.config)
  if (used.has(source)) return null
  const alias = mergeAlias(from, aliases)
  return nodes.map((n) => (n.id === target
    ? { ...n, data: { ...n.data, config: { ...(n.data?.config ?? {}), inputs: { ...value, [alias]: source } } } }
    : n))
}
