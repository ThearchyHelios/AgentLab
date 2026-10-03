import type {
  CatalogBusinessDate, CatalogColumnNotes, CatalogCounts, CatalogDetail, CatalogItem, CatalogNotes, CatalogRelation, CatalogSource, CatalogStatus,
  CatalogStructureColumn, CatalogTableRow,
} from '../../types'

// ===========================================================================
// 数据目录界面的纯函数：清单的筛选和排序、单表目录摊平成「项」、编辑表单和目录之间的来回转换、表单校验。
// 规则跟着服务端（backend/app/data/catalog.py）走：字段、长度上限、审阅路径的写法、各来源的初始状态都以那边为准，
// 这里只是让界面在提交前就说得出问题，最终以服务端的校验为准。
// ===========================================================================

export const TABLE_FIELDS = ['label', 'description', 'grain', 'keys', 'kind', 'business_date', 'valid_filter', 'dedup'] as const
export type TableField = typeof TABLE_FIELDS[number]
export const COLUMN_FIELDS = ['label', 'meaning', 'unit', 'measure', 'codes'] as const
export type ColumnField = typeof COLUMN_FIELDS[number]
export const STATUSES: readonly CatalogStatus[] = ['proposed', 'verified', 'confirmed', 'rejected']

/** 文字项的长度上限（服务端 _TABLE_VALUE_RULES / _COLUMN_VALUE_RULES） */
const TABLE_MAX: Partial<Record<TableField, number>> = { label: 500, description: 2000, grain: 500, valid_filter: 500, dedup: 500 }
const COLUMN_MAX: Partial<Record<ColumnField, number>> = { label: 500, meaning: 500, unit: 50 }

/** 各来源起草出来的项的初始状态（服务端 _INITIAL_STATUS）：外键约束算有确证，人工填写即确认，其余都是推断 */
export function initialStatus(source: CatalogSource): CatalogStatus {
  return source === 'fk' ? 'verified' : source === 'human' ? 'confirmed' : 'proposed'
}

// ---------------------------------------------------------------------------
// 审阅路径：表级项直接写字段名；列级项 columns.<列名>.<字段>（服务端按最后一个句点切，列名里可以有句点）；
// 关系 relations.<编号>
// ---------------------------------------------------------------------------

export const tablePath = (f: TableField) => f
export const columnPath = (col: string, f: ColumnField) => `columns.${col}.${f}`
export const relationPath = (id: string) => `relations.${id}`

type AnyItem = Pick<CatalogItem, 'source' | 'status'> & { note?: string; updated_at?: string }

/** 一张表的目录摊平成 [路径, 项]：表级项、列级项、关系，各自按原来的顺序 */
export function slotsOf(notes: CatalogNotes | null | undefined): { path: string; item: AnyItem }[] {
  const out: { path: string; item: AnyItem }[] = []
  if (!notes) return out
  for (const f of TABLE_FIELDS) {
    const it = notes[f]
    if (isItem(it)) out.push({ path: tablePath(f), item: it })
  }
  for (const [col, items] of Object.entries(notes.columns ?? {})) {
    for (const f of COLUMN_FIELDS) {
      const it = items?.[f]
      if (isItem(it)) out.push({ path: columnPath(col, f), item: it })
    }
  }
  for (const rel of notes.relations ?? []) {
    if (rel && rel.id) out.push({ path: relationPath(rel.id), item: rel })
  }
  return out
}

const isItem = (v: unknown): v is AnyItem => !!v && typeof v === 'object' && 'status' in (v as object)

/** 各状态的项数，四种状态都有键（服务端 status_counts） */
export function countsOf(notes: CatalogNotes | null | undefined): CatalogCounts {
  const c: CatalogCounts = { proposed: 0, verified: 0, confirmed: 0, rejected: 0 }
  for (const { item } of slotsOf(notes)) if (item.status in c) c[item.status]++
  return c
}

/** 没被驳回的项的值 */
function liveValue<T>(item: CatalogItem<T> | undefined): T | null {
  return item && item.status !== 'rejected' ? item.value : null
}

/** 写入或审阅之后，按返回的单表目录更新清单里的那一行（不必为一项审阅重取 150 多张表的清单） */
export function rowFromDetail(d: CatalogDetail, prev: CatalogTableRow): CatalogTableRow {
  const label = d.notes.label
  return {
    ...prev,
    label: liveValue(label),
    label_status: label && label.status !== 'rejected' ? label.status : null,
    kind: liveValue(d.notes.kind),
    counts: countsOf(d.notes),
    relations: (d.notes.relations ?? []).filter((r) => r.status !== 'rejected').length,
    version: d.version,
    updated_at: d.updated_at,
    updated_by: d.updated_by,
  }
}

// ---------------------------------------------------------------------------
// 清单：筛选、排序
// ---------------------------------------------------------------------------

export type ListFilter = 'all' | 'pending' | 'done' | 'none'
export const LIST_FILTERS: readonly ListFilter[] = ['all', 'pending', 'done', 'none']
export type ListSort = 'usage' | 'pending' | 'name'
export const LIST_SORTS: readonly ListSort[] = ['usage', 'pending', 'name']

/**
 * 一张表的审阅进度：pending 还有推断状态的项；done 有目录、没有推断项（已验证的外键关系不需要逐项确认，
 * 全部驳回的也算审阅过）；none 一项都没有。三者互斥
 */
export function progressOf(c: CatalogCounts): Exclude<ListFilter, 'all'> {
  if (c.proposed > 0) return 'pending'
  return c.verified + c.confirmed + c.rejected > 0 ? 'done' : 'none'
}

export function matchesQuery(r: CatalogTableRow, q: string): boolean {
  const needle = q.trim().toLowerCase()
  if (!needle) return true
  return [r.table_name, r.qualified, r.label ?? ''].some((s) => s.toLowerCase().includes(needle))
}

/**
 * 筛选并排序。默认保持服务端的顺序（使用次数多的在前，次数相同按表结构里的顺序）；表结构里已经没有的表
 * 不论怎么排都在最后
 */
export function visibleRows(rows: CatalogTableRow[], q: string, filter: ListFilter, sort: ListSort): CatalogTableRow[] {
  const order = new Map(rows.map((r, i) => [r.table_name, i]))
  const picked = rows.filter((r) => matchesQuery(r, q) && (filter === 'all' || progressOf(r.counts) === filter))
  const idx = (r: CatalogTableRow) => order.get(r.table_name) ?? 0
  return picked.sort((a, b) => {
    if (a.in_schema !== b.in_schema) return a.in_schema ? -1 : 1
    if (sort === 'pending' && a.counts.proposed !== b.counts.proposed) return b.counts.proposed - a.counts.proposed
    if (sort === 'name') return a.table_name.localeCompare(b.table_name, 'en')
    return idx(a) - idx(b)
  })
}

/**
 * 「用到但没确认」：运行中查询过的表（使用次数大于 0、还在表结构里）一共几张，其中几张还有推断项、几张还没有目录。
 * 页面顶部的摘要按它写，点进去复用清单的筛选（有未确认项 / 没有目录）和按使用次数排序，不另做一套
 */
export function usageSummary(rows: CatalogTableRow[]): { used: number; pending: number; none: number } {
  const out = { used: 0, pending: 0, none: 0 }
  for (const r of rows) {
    if (!r.in_schema || r.usage <= 0) continue
    out.used++
    const p = progressOf(r.counts)
    if (p === 'pending') out.pending++
    else if (p === 'none') out.none++
  }
  return out
}

/** 各筛选项下有几张表（筛选按钮上的数字，按搜索词算） */
export function filterCounts(rows: CatalogTableRow[], q: string): Record<ListFilter, number> {
  const out: Record<ListFilter, number> = { all: 0, pending: 0, done: 0, none: 0 }
  for (const r of rows) {
    if (!matchesQuery(r, q)) continue
    out.all++
    out[progressOf(r.counts)]++
  }
  return out
}

// ---------------------------------------------------------------------------
// 批量确认：把选中的推断项改成已确认，整份提交（服务端把「值没动、状态改成确认」当作单项审阅，来源不变）
// ---------------------------------------------------------------------------

export function confirmProposed(notes: CatalogNotes, pick: (path: string) => boolean = () => true): { notes: CatalogNotes; n: number } {
  const copy: CatalogNotes = structuredClone(notes)
  let n = 0
  const touch = (path: string, it: AnyItem | undefined) => {
    if (it && it.status === 'proposed' && pick(path)) { it.status = 'confirmed'; n++ }
  }
  for (const f of TABLE_FIELDS) touch(tablePath(f), copy[f] as AnyItem | undefined)
  for (const [col, items] of Object.entries(copy.columns ?? {})) {
    for (const f of COLUMN_FIELDS) touch(columnPath(col, f), items?.[f] as AnyItem | undefined)
  }
  for (const rel of copy.relations ?? []) touch(relationPath(rel.id), rel)
  return { notes: copy, n }
}

// ---------------------------------------------------------------------------
// 编辑表单：每一项一段文字。进入编辑时按目录生成（被驳回的项留空，原值放在占位里），提交时和进入时的文字逐项
// 比较——没动的原样交回（保留来源和状态，被驳回的照旧驳回），改了的交新值（服务端记为人工填写、已确认），
// 清空的不交（服务端删掉这一项）。关联关系另有规矩：只有人工添加的能删除，其余的只能驳回（relationRemoval）
// ---------------------------------------------------------------------------

/** 表级的表单键：业务日期拆成三格 */
export const DATE_KEYS = ['business_date.column', 'business_date.rule', 'business_date.timezone'] as const
type DateKey = typeof DATE_KEYS[number]
export type TableKey = Exclude<TableField, 'business_date'> | DateKey

export interface RelationDraft {
  /** 表单里的键：已有的关系用编号，新加的用临时键 */
  key: string
  orig: CatalogRelation | null
  columns: string
  to_table: string
  to_columns: string
  cardinality: string
  /** 编辑中点了「驳回」：保存时原样交回、状态改为已驳回（等同单项驳回） */
  reject?: boolean
}

/**
 * 去掉一条关联关系是删除还是驳回：人工添加的（以及还没保存的）可以删除；外键约束、命名推断、数据剖析、模型起草得出的
 * 只能驳回——删掉的话下次起草、助手和 SQL 检查按表结构现推都会把它带回来（服务端 apply_human_edit 也按这条处理：
 * 提交里没有的非人工关系转为驳回）。已经驳回的返回 null，没有可做的
 */
export function relationRemoval(orig: CatalogRelation | null): 'delete' | 'reject' | null {
  if (!orig || orig.source === 'human') return 'delete'
  return orig.status === 'rejected' ? null : 'reject'
}

export interface EditForm {
  table: Partial<Record<TableKey, string>>
  /** 列名 → 字段 → 文字 */
  columns: Record<string, Partial<Record<ColumnField, string>>>
  relations: RelationDraft[]
}

const splitList = (s: string) => s.split(/[、,，;；\s]+/).map((x) => x.trim()).filter(Boolean)
const joinList = (v: string[]) => v.join('、')

function codesText(v: Record<string, string>): string {
  return Object.entries(v).map(([k, x]) => `${k}=${x}`).join('\n')
}

/**
 * 码值的文字：每行一个「码值=含义」（全角等号也认）。含义可以空着（「2=」）：数据剖析只知道列里出现过哪些取值，
 * 含义等人填，服务端也收空含义。返回解析结果和第一处错误
 */
export function parseCodes(text: string): { value: Record<string, string>; error: { line: number } | { dup: string } | null } {
  const value: Record<string, string> = {}
  const lines = text.split('\n')
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim()
    if (!line) continue
    const m = line.match(/^(.+?)\s*[=＝]\s*(.*)$/)
    if (!m || !m[1].trim()) return { value, error: { line: i + 1 } }
    const code = m[1].trim()
    if (code in value) return { value, error: { dup: code } }
    value[code] = m[2].trim()
  }
  return { value, error: null }
}

const itemText = (it: CatalogItem | undefined, render: (v: any) => string): string =>
  (it && it.status !== 'rejected' ? render(it.value) : '')

function tableText(notes: CatalogNotes, k: TableKey): string {
  if (k.startsWith('business_date.')) {
    const d = notes.business_date
    if (!d || d.status === 'rejected') return ''
    return (d.value as any)?.[k.slice('business_date.'.length)] ?? ''
  }
  const f = k as Exclude<TableField, 'business_date'>
  return itemText(notes[f] as CatalogItem | undefined, (v) => (f === 'keys' ? joinList(v) : String(v ?? '')))
}

function columnText(items: CatalogColumnNotes | undefined, f: ColumnField): string {
  const it = items?.[f] as CatalogItem | undefined
  return itemText(it, (v) => (f === 'codes' ? codesText(v) : String(v ?? '')))
}

const TABLE_KEYS: readonly TableKey[] = ['label', 'description', 'grain', 'keys', 'kind', ...DATE_KEYS, 'valid_filter', 'dedup']

export function formFromNotes(notes: CatalogNotes, structure: CatalogStructureColumn[]): EditForm {
  const table: EditForm['table'] = {}
  for (const k of TABLE_KEYS) table[k] = tableText(notes, k)
  const columns: EditForm['columns'] = {}
  const names = [...structure.map((c) => c.name), ...Object.keys(notes.columns ?? {})]
  for (const col of names) {
    if (columns[col]) continue
    const items = notes.columns?.[col]
    columns[col] = Object.fromEntries(COLUMN_FIELDS.map((f) => [f, columnText(items, f)]))
  }
  const relations = (notes.relations ?? []).map((r) => relationDraft(r))
  return { table, columns, relations }
}

export function relationDraft(r: CatalogRelation | null, key?: string): RelationDraft {
  return {
    key: r?.id ?? key ?? `new-${Math.random().toString(36).slice(2, 8)}`,
    orig: r,
    columns: r ? r.columns.join(', ') : '',
    to_table: r?.to_table ?? '',
    to_columns: r ? r.to_columns.join(', ') : '',
    cardinality: r?.cardinality ?? '',
  }
}

const sameRelation = (a: RelationDraft, b: RelationDraft) =>
  a.columns === b.columns && a.to_table === b.to_table && a.to_columns === b.to_columns && a.cardinality === b.cardinality

/** 表单里改了几处：表级、列级各算一格，关系增删改各算一条 */
export function changeCount(initial: EditForm, form: EditForm): number {
  let n = 0
  for (const k of TABLE_KEYS) if ((initial.table[k] ?? '') !== (form.table[k] ?? '')) n++
  for (const [col, fields] of Object.entries(form.columns)) {
    for (const f of COLUMN_FIELDS) if ((initial.columns[col]?.[f] ?? '') !== (fields[f] ?? '')) n++
  }
  const before = new Map(initial.relations.map((r) => [r.key, r]))
  const after = new Set(form.relations.map((r) => r.key))
  for (const r of form.relations) {
    const b = before.get(r.key)
    if (!b || !sameRelation(b, r) || !!b.reject !== !!r.reject) n++
  }
  for (const k of before.keys()) if (!after.has(k)) n++
  return n
}

/** 表单的格式问题：键 t:<表单键> / c:<列名>:<字段> / r:<关系键> → 说明。空对象表示可以提交 */
export interface FormProblems { [key: string]: string }

export interface ProblemText {
  tooLong: (n: number) => string
  codesInvalid: (line: number) => string
  codesDuplicate: (code: string) => string
  dateColumnRequired: string
  relationIncomplete: string
  relationMismatch: string
  relationDuplicate: string
}

export function validateForm(form: EditForm, text: ProblemText): FormProblems {
  const out: FormProblems = {}
  for (const [k, max] of Object.entries(TABLE_MAX) as [TableKey, number][]) {
    if ((form.table[k] ?? '').trim().length > max) out[`t:${k}`] = text.tooLong(max)
  }
  const [dc, dr, dt] = DATE_KEYS.map((k) => (form.table[k] ?? '').trim())
  if (!dc && (dr || dt)) out['t:business_date.column'] = text.dateColumnRequired
  for (const [col, fields] of Object.entries(form.columns)) {
    for (const [f, max] of Object.entries(COLUMN_MAX) as [ColumnField, number][]) {
      if ((fields[f] ?? '').trim().length > max) out[`c:${col}:${f}`] = text.tooLong(max)
    }
    const codes = fields.codes ?? ''
    if (codes.trim()) {
      const { error } = parseCodes(codes)
      if (error) out[`c:${col}:codes`] = 'line' in error ? text.codesInvalid(error.line) : text.codesDuplicate(error.dup)
    }
  }
  const seen = new Map<string, string>()
  for (const r of form.relations) {
    const cols = splitList(r.columns)
    const to = splitList(r.to_columns)
    if (!cols.length || !r.to_table.trim() || !to.length) { out[`r:${r.key}`] = text.relationIncomplete; continue }
    if (cols.length !== to.length) { out[`r:${r.key}`] = text.relationMismatch; continue }
    // 同一条关系（两端的表和列相同）服务端会算出同一个编号，重复提交会被拒收
    const sig = JSON.stringify([r.to_table.trim().toLowerCase(),
      cols.map((c, i) => [c.toLowerCase(), to[i].toLowerCase()]).sort()])
    if (seen.has(sig)) out[`r:${r.key}`] = text.relationDuplicate
    else seen.set(sig, r.key)
  }
  return out
}

/** 业务主键里写了表结构中没有的列（只提醒，不拦：表结构会变，目录里先写着的列等结构同步后自然对上） */
export function unknownKeys(text: string, structure: { name: string }[]): string[] {
  if (!structure.length) return []
  const names = new Set(structure.map((c) => c.name.toLowerCase()))
  return splitList(text).filter((k) => !names.has(k.toLowerCase()))
}

function parseTable(k: Exclude<TableField, 'business_date'>, text: string): unknown {
  return k === 'keys' ? splitList(text) : text.trim()
}

function parseColumn(f: ColumnField, text: string): unknown {
  return f === 'codes' ? parseCodes(text).value : text.trim()
}

/**
 * 按表单生成要提交的整份目录。orig 是进入编辑时的目录，initial 是当时生成的表单。
 * 改过的项保留原来的来源、状态交上去（服务端发现值变了，一律改记人工填写、已确认，客户端写的来源和状态不作数）
 */
export function buildNotes(orig: CatalogNotes, initial: EditForm, form: EditForm): CatalogNotes {
  const out: CatalogNotes = {}
  const put = <T,>(o: CatalogItem<T> | undefined, a: string, b: string, value: () => T): CatalogItem<T> | undefined => {
    if (a === b) return o
    if (!b.trim()) return undefined
    return o ? { ...o, value: value() } : ({ value: value() } as CatalogItem<T>)
  }
  for (const f of TABLE_FIELDS) {
    if (f === 'business_date') {
      const a = DATE_KEYS.map((k) => initial.table[k] ?? '')
      const b = DATE_KEYS.map((k) => (form.table[k] ?? '').trim())
      const same = a.every((x, i) => x === (form.table[DATE_KEYS[i]] ?? ''))
      const o = orig.business_date
      let next: CatalogItem<CatalogBusinessDate> | undefined
      if (same) next = o
      else if (!b.some(Boolean)) next = undefined
      else {
        const value = { column: b[0], ...(b[1] ? { rule: b[1] } : {}), ...(b[2] ? { timezone: b[2] } : {}) }
        next = o ? { ...o, value } : ({ value } as CatalogItem<CatalogBusinessDate>)
      }
      if (next) out.business_date = next
      continue
    }
    const o = orig[f] as CatalogItem | undefined
    const next = put(o, initial.table[f] ?? '', form.table[f] ?? '', () => parseTable(f, form.table[f] ?? ''))
    if (next) (out as Record<string, unknown>)[f] = next
  }
  const columns: NonNullable<CatalogNotes['columns']> = {}
  const names = new Set([...Object.keys(orig.columns ?? {}), ...Object.keys(form.columns)])
  for (const col of names) {
    const items: Record<string, unknown> = {}
    for (const f of COLUMN_FIELDS) {
      const o = orig.columns?.[col]?.[f] as CatalogItem | undefined
      const a = initial.columns[col]?.[f] ?? ''
      const b = form.columns[col]?.[f] ?? a
      const next = put(o, a, b, () => parseColumn(f, b))
      if (next) items[f] = next
    }
    if (Object.keys(items).length) columns[col] = items
  }
  if (Object.keys(columns).length) out.columns = columns
  const before = new Map(initial.relations.map((r) => [r.key, r]))
  const relations: CatalogRelation[] = []
  for (const r of form.relations) {
    const b = before.get(r.key)
    if (r.orig && r.reject) { relations.push({ ...r.orig, status: 'rejected' }); continue }
    if (r.orig && b && sameRelation(b, r)) { relations.push(r.orig); continue }
    relations.push({
      ...(r.orig ?? { coverage: null }),
      // 编号由服务端按两端重算；新加的先给一个临时编号（不含句点）
      id: r.orig?.id ?? r.key,
      columns: splitList(r.columns),
      to_table: r.to_table.trim(),
      to_columns: splitList(r.to_columns),
      cardinality: (r.cardinality || null) as CatalogRelation['cardinality'],
    } as CatalogRelation)
  }
  if (relations.length) out.relations = relations
  return out
}
