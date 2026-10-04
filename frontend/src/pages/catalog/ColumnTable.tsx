import { memo } from 'react'
import type { ReactNode } from 'react'
import { AlertTriangle, CheckCheck, Lock, PencilLine } from 'lucide-react'
import clsx from 'clsx'
import type {
  CatalogColumnNotes, CatalogItem, CatalogMeasure, CatalogReviewAction, CatalogStructureColumn,
} from '../../types'
import { IconButton } from '../../components/ui'
import {
  CATALOG_COLUMN_FIELD_LABEL, CATALOG_MEASURE_HINT, CATALOG_MEASURE_LABEL, CATALOG_TEXT as CT, CODES_TEXT as KT,
} from '../../lib/terms'
import { ItemMark } from './parts'
import type { ReviewTarget } from './parts'
import { COLUMN_FIELDS, codesComplete, columnPath, completeOf } from './model'
import type { ColumnDraft, ColumnField } from './model'

// ===========================================================================
// 单表详情的列表格：列名 / 类型、中文名、含义、单位、度量类型、码值。每格的值旁边是只有图标的状态标识，点开看来源、
// 做确认 / 驳回 / 恢复；有推断项的行末尾有「确认本列推断」。编辑时每格换成输入框。
// 一张表 50 列时每次按键都会重画整张表：行是 memo 的，只有改动的那一行重画。
//
// 窄（表格所在的框不到 40rem，比如 390 宽的手机）时不再是 860px 宽、要横向滚动的表格：每一列排成一张卡片，列名在上，
// 下面每项一行「字段名 值 状态」，没填的项不占行；状态标识跟着值走，始终在框里看得到。按框的宽度（容器查询）切换，
// 不按视口：宽屏左右两栏时右栏也可能很窄。
// ===========================================================================

/**
 * 窄框下的卡片式排法（容器查询，框宽 < 40rem 时生效）。表格元素改成块，表头藏起来，每格前面补上字段名。
 * 字段名宽屏时 display:none，读屏不重复念；窄屏时表头藏了，读屏靠它知道是哪一项
 */
export const NARROW = {
  table: '@max-[40rem]:block @min-[40rem]:min-w-[860px] @min-[40rem]:table-fixed',
  head: '@max-[40rem]:hidden',
  body: '@max-[40rem]:block',
  row: '@max-[40rem]:relative @max-[40rem]:flex @max-[40rem]:flex-col @max-[40rem]:py-1.5',
  rowHead: '@max-[40rem]:block @max-[40rem]:pr-12',
  cell: '@max-[40rem]:flex @max-[40rem]:items-start @max-[40rem]:gap-2 @max-[40rem]:px-3 @max-[40rem]:py-0.5',
  label: 'hidden w-16 shrink-0 pt-0.5 text-2xs text-faint @max-[40rem]:block',
}

const MEASURES = Object.keys(CATALOG_MEASURE_LABEL) as CatalogMeasure[]
const CODES_SHOWN = 4

export interface ColumnModel {
  name: string
  /** 表结构里的这一列；表结构里已经没有、目录还留着的为 null */
  structure: CatalogStructureColumn | null
  items: CatalogColumnNotes | undefined
}

type ColumnTexts = ColumnDraft

export function ColumnTable({ columns, form, initial, problems, busy, systemNotes, onReview, onConfirmColumn, onFillCodes, onChange, onCodesComplete }: {
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
  /** 码值里有含义空着的：打开逐个填写含义的弹窗 */
  onFillCodes: (col: string) => void
  onChange: (col: string, field: ColumnField, value: string) => void
  /** 编辑中勾选或取消码值的「已列出全部取值」 */
  onCodesComplete: (col: string, value: boolean) => void
}) {
  return (
    <div className="@container relative overflow-x-auto rounded-lg border" data-catalog-columns="">
      <table className={clsx('w-full border-collapse text-xs', NARROW.table)}>
        <thead className={NARROW.head}>
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
        <tbody className={NARROW.body}>
          {columns.map((c) => (
            <ColumnRow key={c.name} col={c} editing={!!form} draft={form?.[c.name]} initial={initial?.[c.name]} problems={problems} busy={busy}
                       systemNotes={systemNotes} onReview={onReview} onConfirmColumn={onConfirmColumn} onFillCodes={onFillCodes} onChange={onChange}
                       onCodesComplete={onCodesComplete} />
          ))}
        </tbody>
      </table>
    </div>
  )
}

const ColumnRow = memo(function ColumnRow({ col, editing, draft, initial, problems, busy, systemNotes, onReview, onConfirmColumn, onFillCodes, onChange,
  onCodesComplete }: {
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
  onFillCodes: (col: string) => void
  onChange: (col: string, field: ColumnField, value: string) => void
  onCodesComplete: (col: string, value: boolean) => void
}) {
  const { name, structure, items } = col
  const pending = COLUMN_FIELDS.filter((f) => items?.[f]?.status === 'proposed').length
  const comment = systemNotes ? structure?.comment?.trim() : ''
  const cell = (f: ColumnField, view: ReactNode, input: ReactNode) => {
    const it = items?.[f] as CatalogItem | undefined
    const where = CT.whereColumn(name, CATALOG_COLUMN_FIELD_LABEL[f])
    const problem = problems[`c:${name}:${f}`]
    const changed = editing && ((draft?.[f] ?? '') !== (initial?.[f] ?? '') || (f === 'codes' && completeOf(draft) !== completeOf(initial)))
    return (
      // 窄框下没填的项不占行（编辑时照常列出来好填）
      <td className={clsx('px-2 py-1.5 align-top', NARROW.cell, !it && !editing && '@max-[40rem]:hidden')} data-cell={f} data-status={it?.status}>
        <span className={NARROW.label}>{CT.columnHead[f]}</span>
        <div className="flex min-w-0 flex-1 items-start gap-1.5">
          <div className="min-w-0 flex-1">{editing ? input : view}</div>
          {it && !changed && (
            <span className="pt-0.5">
              <ItemMark compact disabled={busy || editing} disabledHint={editing ? CT.reviewWhileEditing : undefined}
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
    <tr className={clsx('border-b border-hairline last:border-b-0 hover:bg-hover/40', NARROW.row)} data-column={name} data-pending={pending}>
      <th scope="row" className={clsx('px-3 py-1.5 text-left align-top font-normal', NARROW.rowHead)}>
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
        <Value item={codes} render={(v) => (
          <Codes value={v as Record<string, string>} label={KT.fillLabel(name)} complete={codesComplete(codes)}
                 onFill={codes?.status !== 'rejected' && !busy ? () => onFillCodes(name) : undefined} />
        )} />,
        <>
          <textarea className="field mono !min-h-0 !px-1.5 !py-1 !text-2xs" rows={Math.min(4, Math.max(1, (draft?.codes ?? '').split('\n').length))}
                    value={draft?.codes ?? ''}
                    placeholder={codes?.status === 'rejected' ? CT.rejectedPlaceholder(codesInline(codes.value)) : CT.codesPlaceholder}
                    aria-label={CT.whereColumn(name, CATALOG_COLUMN_FIELD_LABEL.codes)}
                    aria-invalid={problems[`c:${name}:codes`] ? true : undefined}
                    onChange={(e) => onChange(name, 'codes', e.target.value)} data-input="codes" />
          {/* 只列了一部分取值时不勾：SQL 检查只拿勾了的码值表判断「取值不在码值表中」 */}
          <label className="mt-1 flex items-start gap-1 text-2xs leading-snug text-dim" title={KT.completeHint}>
            <input type="checkbox" className="mt-[2px]" checked={completeOf(draft)} disabled={!(draft?.codes ?? '').trim()}
                   onChange={(e) => onCodesComplete(name, e.target.checked)} data-input="codes-complete" />
            {KT.complete}
          </label>
        </>,
      )}
      <td className={clsx('px-1 py-1.5 align-top', '@max-[40rem]:absolute @max-[40rem]:right-1.5 @max-[40rem]:top-1.5 @max-[40rem]:p-0',
        (editing || !pending) && '@max-[40rem]:hidden')}>
        {!editing && pending > 0 && (
          <IconButton label={CT.confirmColumnLabel(name, pending)} icon={<CheckCheck size={13} />} disabled={busy}
                      onClick={() => onConfirmColumn(name)} data-confirm-column={name} />
        )}
      </td>
    </tr>
  )
}, (a, b) => a.col === b.col && a.editing === b.editing && a.draft === b.draft && a.initial === b.initial && a.busy === b.busy
  && a.systemNotes === b.systemNotes && a.onReview === b.onReview && a.onConfirmColumn === b.onConfirmColumn && a.onFillCodes === b.onFillCodes
  && a.onChange === b.onChange && a.onCodesComplete === b.onCodesComplete
  // 格式问题每次都是新对象：只比这一列的几格
  && COLUMN_FIELDS.every((f) => a.problems[`c:${a.col.name}:${f}`] === b.problems[`c:${b.col.name}:${f}`]))

const codesInline = (v: Record<string, string> | undefined) => Object.entries(v ?? {}).map(([k, x]) => `${k}=${x}`).join('；')

/** 一项的值：没有写「—」；被驳回的划掉、淡色 */
export function Value<T>({ item, render }: { item: CatalogItem<T> | undefined; render: (v: T) => ReactNode }) {
  if (!item) return <span className="text-faint"><span aria-hidden>—</span><span className="sr-only">{CT.empty}</span></span>
  return <span className={clsx(item.status === 'rejected' && 'text-faint line-through')}>{render(item.value)}</span>
}

/** 含义还空着的码值个数（数据剖析给的码值候选只有取值，含义等人填） */
export const pendingCodes = (v: Record<string, string> | undefined): number =>
  Object.values(v ?? {}).filter((x) => !String(x ?? '').trim()).length

function Codes({ value, label, complete, onFill }: { value: Record<string, string>; label: string; complete: boolean; onFill?: () => void }) {
  const entries = Object.entries(value)
  const pending = pendingCodes(value)
  return (
    <span className="block" data-codes-complete={complete ? 'true' : 'false'}>
      <span className="flex flex-wrap gap-1" title={entries.map(([k, v]) => `${k}=${v.trim() || KT.pending}`).join('\n')}>
        {entries.slice(0, CODES_SHOWN).map(([k, v]) => (
          <span key={k} className="inline-flex max-w-full items-center gap-1 rounded border bg-bg px-1 text-2xs" data-code={k}>
            <span className="mono text-dim">{k}</span>
            {v.trim()
              ? <span className="truncate">{v}</span>
              : <span className="truncate" style={{ color: 'var(--st-waiting)' }} data-code-pending="">{KT.pending}</span>}
          </span>
        ))}
        {entries.length > CODES_SHOWN && <span className="text-2xs text-faint">+{entries.length - CODES_SHOWN}</span>}
      </span>
      {complete && (
        <span className="mt-1 inline-flex items-center gap-0.5 text-2xs text-faint" title={KT.completeMarkHint} data-codes-complete-mark="">
          <CheckCheck size={10} aria-hidden /> {KT.completeMark}
        </span>
      )}
      {pending > 0 && onFill && (
        // 各码值的「含义待填写」已经写在上面，这里只给入口；几个待填写写在悬停里
        <button type="button" className="mt-1 inline-flex items-center gap-1 whitespace-nowrap rounded px-1 text-2xs text-dim underline decoration-dotted underline-offset-2 hover:text-fg"
                onClick={onFill} aria-label={label} title={KT.pendingN(pending)} data-codes-fill={pending}>
          <PencilLine size={10} aria-hidden /> {KT.fill}
        </button>
      )}
    </span>
  )
}
