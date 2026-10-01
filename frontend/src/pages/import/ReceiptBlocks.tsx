import type { ExcludedRows, ReceiptSummaryProps, RowsExcludedProps } from '../../types'
import { formatNumber } from '../../lib/format'
import { RECEIPT_BLOCK_TEXT, RECIPE_TEXT, VERSIONS_TEXT } from '../../lib/terms'
import { CellChips } from './SuggestionCards'

// ===========================================================================
// 回执的只读展示块：向导的试运行回执和版本页的清单视图共用（导入清单里的 receipt 与试运行回执同形）。
// 只显示服务端给的结论，不做算术判断。版本页只用不改，props 的形状写在 types.ts 里
// ===========================================================================

const BLOCK_TEXT = RECEIPT_BLOCK_TEXT

/** 一组排除的行里每段行号的定位坐标：取该行第一列（网格按行滚动到那里；那一列不在预览里时网格取最左的一列） */
const rowRef = (sheet: string, row: number) => `${sheet}!A${row}`

/** 一项里的行数：[[起, 止], …] 逐段相加 */
const rowCount = (g: ExcludedRows) => (g.rows ?? []).reduce((n, [a, b]) => n + Math.max(0, b - a + 1), 0)

/**
 * 「排除的行」：按原因分组（同一原因的各项放在一起，组头写总行数和格数），每项写锚点原文、所在工作表、
 * 行号和格数。给了 onFocus 时行号可以点（定位到那一行）。rows 为空数组或缺省时不渲染（「未记录」和「没有」
 * 的区分由 ReceiptSummary 写）。组的先后按原因第一次出现的顺序，组内照服务端给的顺序
 */
export function RowsExcluded({ rows, onFocus }: RowsExcludedProps) {
  if (!rows?.length) return null
  const groups = new Map<string, ExcludedRows[]>()
  for (const g of rows) groups.set(g.reason, [...(groups.get(g.reason) ?? []), g])
  return (
    <div className="space-y-2 text-xs" data-rows-excluded="" data-rows-dropped="">
      {[...groups.entries()].map(([reason, items]) => (
        <section key={reason} className="space-y-1" data-excluded-group={reason}>
          <div className="flex flex-wrap items-baseline gap-x-2 text-dim">
            <span className="font-medium">{BLOCK_TEXT.excludedReason[reason] ?? BLOCK_TEXT.excludedOther}</span>
            <span className="tnum text-2xs text-faint">
              {BLOCK_TEXT.groupTotal(items.reduce((n, g) => n + rowCount(g), 0), items.reduce((n, g) => n + (g.cells ?? 0), 0))}
            </span>
          </div>
          <ul className="space-y-1">
            {items.map((g: ExcludedRows, i: number) => (
              <li key={`${g.sheet}-${g.block ?? ''}-${g.anchor ?? ''}-${i}`}
                  className="flex flex-wrap items-baseline gap-x-2 gap-y-1 rounded-md border px-2.5 py-1.5"
                  data-excluded-reason={g.reason} data-excluded-sheet={g.sheet}>
                {g.anchor && <span className="break-all">「{g.anchor}」</span>}
                <span className="text-faint">{g.sheet}</span>
                <span className="inline-flex flex-wrap items-center gap-1">
                  {(g.rows ?? []).map(([a, b]) => (onFocus
                    ? (
                      <button key={`${a}-${b}`} type="button" className="chip tnum hover:border-[var(--accent)] hover:text-fg"
                              data-excluded-rows={`${a}-${b}`} onClick={() => onFocus(rowRef(g.sheet, a))}>
                        {BLOCK_TEXT.rowSpan(a, b)}
                      </button>
                    )
                    : <span key={`${a}-${b}`} className="chip tnum" data-excluded-rows={`${a}-${b}`}>{BLOCK_TEXT.rowSpan(a, b)}</span>
                  ))}
                </span>
                <span className="tnum text-2xs text-faint">{BLOCK_TEXT.cells(g.cells)}</span>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  )
}

/** 规格（P3-SPEC 10.1）里的旧名：字段统一叫 rows_excluded 之后组件改名 RowsExcluded，这个别名留给照旧名引用的地方 */
export const RowsDropped = RowsExcluded

/**
 * 回执摘要：单元格去向的总数、表与行数、占位符、规范写法、区域外文字全文、排除的行。只显示服务端给的结论，
 * 不做算术判断（「全部有去处」只在没有未去处的格时写）
 */
export function ReceiptSummary({ receipt, onFocus }: ReceiptSummaryProps) {
  if (!receipt) return null
  const ledger = receipt.ledger ?? []
  const total = ledger.reduce((n, s) => n + (s.nonempty_read ?? 0), 0)
  const unclaimed = ledger.reduce((n, s) => n + (s.unclaimed ?? 0), 0)
  const tables = receipt.tables ?? []
  const placeholders = Object.entries(receipt.placeholders ?? {})
  const canon = typeof receipt.canonicalized_total === 'number'
    ? receipt.canonicalized_total
    : Array.isArray(receipt.canonicalized) ? receipt.canonicalized.length : 0
  const outside = receipt.outside_text ?? []
  const excluded = receipt.rows_excluded

  return (
    <section className="space-y-2 text-xs" data-receipt-summary="">
      <h4 className="font-semibold">{VERSIONS_TEXT.manifestBlocks.receipt}</h4>
      {ledger.length > 0 && (
        <p data-summary-ledger="">
          {RECIPE_TEXT.ledger(formatNumber(total))}
          {unclaimed
            ? <>，<span className="text-[var(--err)]">{RECIPE_TEXT.ledgerUnclaimed(formatNumber(unclaimed))}</span></>
            : `，${RECIPE_TEXT.ledgerAll}`}
        </p>
      )}
      {tables.length > 0 && (
        <div className="space-y-1" data-summary-tables="">
          <div className="text-dim">{RECIPE_TEXT.tables}</div>
          <ul className="flex flex-wrap gap-1">
            {tables.map((t) => (
              <li key={t.name} className="chip" data-summary-table={t.name} data-rows={t.rows}>
                {t.name} <span className="tnum text-faint">{RECIPE_TEXT.rows(formatNumber(t.rows))}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {placeholders.length > 0 && (
        <p className="text-dim" data-summary-placeholders="">
          {RECIPE_TEXT.placeholderCounts}：{placeholders.map(([text, n]) => RECIPE_TEXT.placeholderCount(text, formatNumber(n))).join('，')}
        </p>
      )}
      {canon > 0 && <p className="text-dim" data-summary-canonicalized={canon}>{BLOCK_TEXT.canonicalized(canon)}</p>}
      {outside.length > 0 && (
        <div className="space-y-1" data-summary-outside="">
          <div className="text-dim">{RECIPE_TEXT.outsideText}</div>
          <ul className="space-y-1">
            {outside.map((o) => {
              const ref = o.cell.includes('!') ? o.cell : `${o.sheet}!${o.cell}`
              return (
                <li key={ref} className="flex items-baseline gap-2" data-summary-outside-text={ref}>
                  {onFocus
                    ? <CellChips cells={[ref]} onFocus={onFocus} />
                    : <span className="chip mono">{ref.slice(ref.lastIndexOf('!') + 1)}</span>}
                  <span className="min-w-0 flex-1 break-all text-dim">{o.text}</span>
                </li>
              )
            })}
          </ul>
        </div>
      )}
      <div className="space-y-1" data-summary-excluded={excluded == null ? 'unrecorded' : String(excluded.length)}>
        <div className="text-dim">{VERSIONS_TEXT.manifestBlocks.excluded}</div>
        {excluded == null
          ? <p className="text-faint">{BLOCK_TEXT.excludedUnrecorded}</p>
          : excluded.length
            ? <RowsExcluded rows={excluded} onFocus={onFocus} />
            : <p className="text-faint">{BLOCK_TEXT.excludedNone}</p>}
      </div>
    </section>
  )
}
