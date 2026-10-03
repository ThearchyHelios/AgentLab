import { memo } from 'react'
import type { ReactNode } from 'react'
import { AlertTriangle, CheckCheck, Lock } from 'lucide-react'
import clsx from 'clsx'
import type {
  CatalogColumnNotes, CatalogItem, CatalogMeasure, CatalogReviewAction, CatalogStructureColumn,
} from '../../types'
import { IconButton } from '../../components/ui'
import {
  CATALOG_COLUMN_FIELD_LABEL, CATALOG_MEASURE_HINT, CATALOG_MEASURE_LABEL, CATALOG_TEXT as CT,
} from '../../lib/terms'
import { ItemMark } from './parts'
import type { ReviewTarget } from './parts'
import { COLUMN_FIELDS, columnPath } from './model'
import type { ColumnField } from './model'

// ===========================================================================
// 单表详情的列表格：列名 / 类型、中文名、含义、单位、度量类型、码值。每格的值旁边是只有图标的状态标识，点开看来源、
// 做确认 / 驳回 / 恢复；有推断项的行末尾有「确认本列推断」。编辑时每格换成输入框。
// 一张表 50 列时每次按键都会重画整张表：行是 memo 的，只有改动的那一行重画。
// ===========================================================================

const MEASURES = Object.keys(CATALOG_MEASURE_LABEL) as CatalogMeasure[]
const CODES_SHOWN = 4

export interface ColumnModel {
  name: string
  /** 表结构里的这一列；表结构里已经没有、目录还留着的为 null */
  structure: CatalogStructureColumn | null
  items: CatalogColumnNotes | undefined
}

type ColumnTexts = Partial<Record<ColumnField, string>>

export function ColumnTable({ columns, form, initial, problems, busy, systemNotes, onReview, onConfirmColumn, onChange }: {
  columns: ColumnModel[]
  /** 编辑中：列名 → 字段 → 文字（form 是当前的，initial 是进入编辑时的）；不在编辑时为 null */
  form: Record<string, ColumnTexts> | null
  initial: Record<string, ColumnTexts> | null
  /** 格式问题：键 c:<列名>:<字段> */
  problems: Record<string, string>
  busy: boolean
  /** 导入表格的源：列的说明是系统生成的，只读地写在列名下面 */
  systemNotes: boolean
  onReview: (target: ReviewTarget, action: CatalogReviewAction) => void
  onConfirmColumn: (col: string) => void
  onChange: (col: string, field: ColumnField, value: string) => void
}) {
  return (
    <div className="relative overflow-x-auto rounded-lg border" data-catalog-columns="">
      <table className="w-full min-w-[860px] table-fixed border-collapse text-xs">
        <thead>
          <tr className="border-b bg-elev text-left text-2xs text-faint">
            <th scope="col" className="w-[21%] px-3 py-2 font-medium">{CT.columnHead.name} / {CT.columnHead.type}</th>
            <th scope="col" className="w-[14%] px-2 py-2 font-medium">{CT.columnHead.label}</th>
            <th scope="col" className="px-2 py-2 font-medium">{CT.columnHead.meaning}</th>
            <th scope="col" className="w-[8%] px-2 py-2 font-medium">{CT.columnHead.unit}</th>
            <th scope="col" className="w-[11%] px-2 py-2 font-medium">{CT.columnHead.measure}</th>
            <th scope="col" className="w-[17%] px-2 py-2 font-medium" title={form ? CT.codesHint : undefined}>
              {CT.columnHead.codes}
              {form && <span className="ml-1 font-normal">· {CT.codesHint}</span>}
            </th>
            <th scope="col" className="w-9 px-1 py-2"><span className="sr-only">{CT.confirmColumn}</span></th>
          </tr>
        </thead>
        <tbody>
          {columns.map((c) => (
            <ColumnRow key={c.name} col={c} editing={!!form} draft={form?.[c.name]} initial={initial?.[c.name]} problems={problems} busy={busy}
                       systemNotes={systemNotes} onReview={onReview} onConfirmColumn={onConfirmColumn} onChange={onChange} />
          ))}
        </tbody>
      </table>
    </div>
  )
}

const ColumnRow = memo(function ColumnRow({ col, editing, draft, initial, problems, busy, systemNotes, onReview, onConfirmColumn, onChange }: {
  col: ColumnModel
  editing: boolean
  /** 这一列的表单。只有改了这一列时引用才会变，别的行不重画 */
  draft: ColumnTexts | undefined
  initial: ColumnTexts | undefined
  problems: Record<string, string>
  busy: boolean
  systemNotes: boolean
  onReview: (target: ReviewTarget, action: CatalogReviewAction) => void
  onConfirmColumn: (col: string) => void
  onChange: (col: string, field: ColumnField, value: string) => void
}) {
  const { name, structure, items } = col
  const pending = COLUMN_FIELDS.filter((f) => items?.[f]?.status === 'proposed').length
  const comment = systemNotes ? structure?.comment?.trim() : ''
  const cell = (f: ColumnField, view: ReactNode, input: ReactNode) => {
    const it = items?.[f] as CatalogItem | undefined
    const where = CT.whereColumn(name, CATALOG_COLUMN_FIELD_LABEL[f])
    const problem = problems[`c:${name}:${f}`]
    const changed = editing && (draft?.[f] ?? '') !== (initial?.[f] ?? '')
    return (
      <td className="px-2 py-1.5 align-top" data-cell={f} data-status={it?.status}>
        <div className="flex items-start gap-1.5">
          <div className="min-w-0 flex-1">{editing ? input : view}</div>
          {it && !changed && (
            <span className="pt-0.5">
              <ItemMark compact disabled={busy || editing}
                        target={{ path: columnPath(name, f), where, source: it.source, status: it.status, note: it.note, updated_at: it.updated_at }}
                        onReview={(a) => onReview({ path: columnPath(name, f), where, source: it.source, status: it.status }, a)} />
            </span>
          )}
        </div>
        {problem && <div className="mt-1 text-2xs leading-relaxed text-[var(--err)]" role="alert">{problem}</div>}
      </td>
    )
  }
  const text = (f: ColumnField, opts?: { mono?: boolean; narrow?: boolean }) => {
    const it = items?.[f] as CatalogItem<string> | undefined
    const rejected = it?.status === 'rejected'
    const value = draft?.[f] ?? ''
    return cell(
      f,
      <Value item={it} render={(v) => <span className={clsx('whitespace-pre-wrap break-words', opts?.mono && 'mono')}>{v}</span>} />,
      <input
        className={clsx('field !px-1.5 !py-1', opts?.mono && 'mono')}
        value={value}
        placeholder={rejected ? CT.rejectedPlaceholder(String(it?.value ?? '')) : ''}
        aria-label={CT.whereColumn(name, CATALOG_COLUMN_FIELD_LABEL[f])}
        aria-invalid={problems[`c:${name}:${f}`] ? true : undefined}
        onChange={(e) => onChange(name, f, e.target.value)}
        data-input={f}
      />,
    )
  }
  const measure = items?.measure
  const codes = items?.codes
  return (
    <tr className="border-b border-hairline last:border-b-0 hover:bg-hover/40" data-column={name} data-pending={pending}>
      <th scope="row" className="px-3 py-1.5 text-left align-top font-normal">
        <div className="flex flex-wrap items-center gap-1">
          <span className="mono text-xs font-medium text-fg [overflow-wrap:anywhere]">{name}</span>
          {structure?.pk && <span className="chip !px-1.5 !py-0 !text-2xs">{CT.pk}</span>}
        </div>
        {structure && <div className="mono mt-0.5 truncate text-2xs text-faint" title={structure.type}>{structure.type || '—'}</div>}
        {comment && (
          <div className="mt-1 flex items-start gap-1 text-2xs leading-relaxed text-dim" data-system-note="" title={CT.systemTag}>
            <Lock size={10} className="mt-[3px] shrink-0 text-faint" aria-hidden />
            <span><span className="text-faint">{CT.systemTag}：</span>{comment}</span>
          </div>
        )}
        {!structure && (
          <div className="mt-1 flex items-center gap-1 text-2xs text-[var(--warn)]" data-column-missing="">
            <AlertTriangle size={10} aria-hidden /> {CT.columnMissing}
          </div>
        )}
      </th>
      {text('label')}
      {text('meaning')}
      {text('unit')}
      {cell(
        'measure',
        <Value item={measure} render={(v) => <span title={CATALOG_MEASURE_HINT[v as CatalogMeasure]}>{CATALOG_MEASURE_LABEL[v as CatalogMeasure] ?? v}</span>} />,
        <select className="field !px-1 !py-1" value={draft?.measure ?? ''}
                aria-label={CT.whereColumn(name, CATALOG_COLUMN_FIELD_LABEL.measure)}
                onChange={(e) => onChange(name, 'measure', e.target.value)} data-input="measure">
          <option value="">{CT.measureNone}</option>
          {MEASURES.map((m) => <option key={m} value={m}>{CATALOG_MEASURE_LABEL[m]}</option>)}
        </select>,
      )}
      {cell(
        'codes',
        <Value item={codes} render={(v) => <Codes value={v as Record<string, string>} />} />,
        <textarea className="field mono !min-h-0 !px-1.5 !py-1 !text-2xs" rows={Math.min(4, Math.max(1, (draft?.codes ?? '').split('\n').length))}
                  value={draft?.codes ?? ''}
                  placeholder={codes?.status === 'rejected' ? CT.rejectedPlaceholder(codesInline(codes.value)) : CT.codesPlaceholder}
                  aria-label={CT.whereColumn(name, CATALOG_COLUMN_FIELD_LABEL.codes)}
                  aria-invalid={problems[`c:${name}:codes`] ? true : undefined}
                  onChange={(e) => onChange(name, 'codes', e.target.value)} data-input="codes" />,
      )}
      <td className="px-1 py-1.5 align-top">
        {!editing && pending > 0 && (
          <IconButton label={CT.confirmColumnLabel(name, pending)} icon={<CheckCheck size={13} />} disabled={busy}
                      onClick={() => onConfirmColumn(name)} data-confirm-column={name} />
        )}
      </td>
    </tr>
  )
}, (a, b) => a.col === b.col && a.editing === b.editing && a.draft === b.draft && a.initial === b.initial && a.busy === b.busy
  && a.systemNotes === b.systemNotes && a.onReview === b.onReview && a.onConfirmColumn === b.onConfirmColumn && a.onChange === b.onChange
  // 格式问题每次都是新对象：只比这一列的几格
  && COLUMN_FIELDS.every((f) => a.problems[`c:${a.col.name}:${f}`] === b.problems[`c:${b.col.name}:${f}`]))

const codesInline = (v: Record<string, string> | undefined) => Object.entries(v ?? {}).map(([k, x]) => `${k}=${x}`).join('；')

/** 一项的值：没有写「—」；被驳回的划掉、淡色 */
export function Value<T>({ item, render }: { item: CatalogItem<T> | undefined; render: (v: T) => ReactNode }) {
  if (!item) return <span className="text-faint"><span aria-hidden>—</span><span className="sr-only">{CT.empty}</span></span>
  return <span className={clsx(item.status === 'rejected' && 'text-faint line-through')}>{render(item.value)}</span>
}

function Codes({ value }: { value: Record<string, string> }) {
  const entries = Object.entries(value)
  return (
    <span className="flex flex-wrap gap-1" title={entries.map(([k, v]) => `${k}=${v}`).join('\n')}>
      {entries.slice(0, CODES_SHOWN).map(([k, v]) => (
        <span key={k} className="inline-flex max-w-full items-center gap-1 rounded border bg-bg px-1 text-2xs">
          <span className="mono text-dim">{k}</span>
          <span className="truncate">{v}</span>
        </span>
      ))}
      {entries.length > CODES_SHOWN && <span className="text-2xs text-faint">+{entries.length - CODES_SHOWN}</span>}
    </span>
  )
}
