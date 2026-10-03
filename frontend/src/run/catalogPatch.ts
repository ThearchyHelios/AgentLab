import type { CatalogBusinessDate, CatalogCardinality, CatalogMeasure, CatalogPatchChange, CatalogTableKind } from '../types'
import {
  CATALOG_CARDINALITY_LABEL, CATALOG_COLUMN_FIELD_LABEL, CATALOG_KIND_LABEL, CATALOG_MEASURE_LABEL, CATALOG_PATCH_TEXT,
  CATALOG_TABLE_FIELD_LABEL, CATALOG_TEXT, CODES_TEXT,
} from '../lib/terms'

// ===========================================================================
// 目录修改提案（助手流里的 catalog_patch 操作）：解析、每一项的中文说法、值怎么写成一行字。
//
// 服务端（api/copilot.py 的 catalog_patch_op）已经核对过数据源、表、路径和取值格式，附上了版本、改前和改后，
// 这里只认形状：认不出的项跳过，一项都不剩就整条不认（不出卡片，也不报错）。路径的写法和审阅接口一致：表级项
// 写字段名，列级项 columns.<列名>.<字段>（列名里可以有句点，按最后一个句点切），关系 relations.<编号>。
// ===========================================================================

export interface CatalogPatch {
  /** 在这一轮操作里的位置：卡片的键、保存状态都按「轮次 + 它」记 */
  key: string
  source: string
  sourceId: string
  table: string
  /** 表的中文名（目录里有、没被驳回时） */
  tableLabel?: string
  /** 提案对照的目录版本：保存时带上，别人在这之后改过就 409 */
  version: number
  changes: CatalogPatchChange[]
}

const TABLE_FIELDS = Object.keys(CATALOG_TABLE_FIELD_LABEL) as (keyof typeof CATALOG_TABLE_FIELD_LABEL)[]
const COLUMN_FIELDS = Object.keys(CATALOG_COLUMN_FIELD_LABEL) as (keyof typeof CATALOG_COLUMN_FIELD_LABEL)[]
const STATES = new Set(['change', 'confirm', 'same'])

export type PatchTarget =
  | { kind: 'table'; field: keyof typeof CATALOG_TABLE_FIELD_LABEL }
  | { kind: 'column'; column: string; field: keyof typeof CATALOG_COLUMN_FIELD_LABEL }
  | { kind: 'relation'; id: string }

/** 路径 → 指的是哪一项。认不出返回 null */
export function patchTarget(path: string): PatchTarget | null {
  if ((TABLE_FIELDS as string[]).includes(path)) return { kind: 'table', field: path as keyof typeof CATALOG_TABLE_FIELD_LABEL }
  if (path.startsWith('columns.')) {
    const rest = path.slice('columns.'.length)
    const at = rest.lastIndexOf('.')
    const field = rest.slice(at + 1)
    if (at > 0 && (COLUMN_FIELDS as string[]).includes(field)) {
      return { kind: 'column', column: rest.slice(0, at), field: field as keyof typeof CATALOG_COLUMN_FIELD_LABEL }
    }
  }
  if (path.startsWith('relations.') && path.length > 'relations.'.length) return { kind: 'relation', id: path.slice('relations.'.length) }
  return null
}

function changeOf(x: unknown): CatalogPatchChange | null {
  if (!x || typeof x !== 'object') return null
  const c = x as Record<string, unknown>
  if (typeof c.path !== 'string' || !patchTarget(c.path) || !('after' in c)) return null
  return {
    path: c.path,
    before: c.before ?? null,
    before_status: typeof c.before_status === 'string' ? c.before_status as CatalogPatchChange['before_status'] : null,
    after: c.after,
    value: 'value' in c ? c.value : c.after,
    reason: typeof c.reason === 'string' ? c.reason : '',
    state: typeof c.state === 'string' && STATES.has(c.state) ? c.state as CatalogPatchChange['state'] : 'change',
    ...(typeof c.note === 'string' && c.note.trim() ? { note: c.note.trim() } : {}),
  }
}

/** catalog_patch 操作 → 提案。认不出的形状返回 null */
export function catalogPatchOf(op: Record<string, any>, key = ''): CatalogPatch | null {
  if (typeof op.source !== 'string' || typeof op.source_id !== 'string' || typeof op.table !== 'string') return null
  const changes = (Array.isArray(op.changes) ? op.changes : []).map(changeOf).filter((c): c is CatalogPatchChange => !!c)
  if (!changes.length) return null
  const version = typeof op.version === 'number' && Number.isFinite(op.version) ? op.version : 0
  return {
    key, source: op.source, sourceId: op.source_id, table: op.table, version, changes,
    ...(typeof op.table_label === 'string' && op.table_label ? { tableLabel: op.table_label } : {}),
  }
}

/** 一轮操作里的全部提案，按出现的先后 */
export function catalogPatchesOf(ops: Record<string, any>[]): CatalogPatch[] {
  const out: CatalogPatch[] = []
  ops.forEach((op, i) => {
    if (op?.op !== 'catalog_patch') return
    const p = catalogPatchOf(op, String(i))
    if (p) out.push(p)
  })
  return out
}

/** 这一项的中文说法：「列 status 的码值」「表的有效记录条件」「指向 visits 的关联关系」 */
export function patchWhere(change: Pick<CatalogPatchChange, 'path' | 'after' | 'before'>): string {
  const t = patchTarget(change.path)
  if (!t) return change.path
  if (t.kind === 'table') return CATALOG_TEXT.whereTable(CATALOG_TABLE_FIELD_LABEL[t.field])
  if (t.kind === 'column') return CATALOG_TEXT.whereColumn(t.column, CATALOG_COLUMN_FIELD_LABEL[t.field])
  const rel = (change.after ?? change.before) as { to_table?: string } | null
  const to = rel?.to_table ?? ''
  return change.before == null ? CATALOG_PATCH_TEXT.whereRelationNew(to) : CATALOG_TEXT.whereRelation(to)
}

/** 关联关系写成一行：visit_id → visits.id（多对一） */
export function relationText(v: unknown): string {
  const r = v as { columns?: string[]; to_table?: string; to_columns?: string[]; cardinality?: CatalogCardinality | null }
  if (!r || typeof r !== 'object') return String(v)
  const left = (r.columns ?? []).join('、')
  const right = (r.to_columns ?? []).map((c) => `${r.to_table}.${c}`).join('、')
  const card = r.cardinality ? CATALOG_CARDINALITY_LABEL[r.cardinality] : ''
  return `${left} → ${right}${card ? `（${card}）` : ''}`
}

/** 码值的含义：空串是数据剖析写进来的候选，含义还要人填，和目录页同一个说法 */
export const codeMeaning = (x: unknown) => (String(x ?? '').trim() ? String(x) : CODES_TEXT.pending)

/** 码值写成一行：1=有效、9=作废、8=含义待填写 */
export function codesText(v: unknown): string {
  if (!v || typeof v !== 'object') return String(v)
  return Object.entries(v as Record<string, string>).map(([k, x]) => `${k}=${codeMeaning(x)}`).join('、')
}

/** 一项的值写成一行字（过程里的明细、卡片里的改前改后都用它）。没有值写「未填写」 */
export function patchValueText(path: string, v: unknown): string {
  if (v == null || v === '') return CATALOG_PATCH_TEXT.empty
  const t = patchTarget(path)
  if (t?.kind === 'relation') return relationText(v)
  const field = t?.field
  if (field === 'kind') return CATALOG_KIND_LABEL[v as CatalogTableKind] ?? String(v)
  if (field === 'measure') return CATALOG_MEASURE_LABEL[v as CatalogMeasure] ?? String(v)
  if (field === 'keys' && Array.isArray(v)) return v.join('、')
  if (field === 'codes') return codesText(v)
  if (field === 'business_date' && typeof v === 'object') {
    const d = v as CatalogBusinessDate
    return [d.column, d.rule ? `${CATALOG_TEXT.dateRule}：${d.rule}` : '', d.timezone ? `${CATALOG_TEXT.dateTimezone}：${d.timezone}` : '']
      .filter(Boolean).join('；')
  }
  return typeof v === 'string' ? v : JSON.stringify(v)
}

/** 交回保存、预览的那一份：路径、原样取值、理由 */
export function patchSubmit(changes: CatalogPatchChange[]) {
  return changes.map((c) => ({ path: c.path, value: c.value, ...(c.reason ? { reason: c.reason } : {}) }))
}

/** 数据目录页上这张表的地址 */
export function catalogTableHref(sourceId: string, table: string): string {
  return `/data/catalog/${encodeURIComponent(sourceId)}/${encodeURIComponent(table)}`
}
