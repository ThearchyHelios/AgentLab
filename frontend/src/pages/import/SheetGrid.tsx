import { useEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties } from 'react'
import clsx from 'clsx'
import type { GridPreview, ImportProblem, RegionMark } from '../../types'
import { Tabs } from '../../components/ui'
import { formatNumber } from '../../lib/format'
import { LEDGER_ROLE_LABEL, RECIPE_TEXT } from '../../lib/terms'

// ===========================================================================
// 原始网格：按配方导入时左边那张表。只读——期 2 不能框选，区域只能经起草或配方面板改
// ===========================================================================

/** 1 → A，27 → AA */
export function colLetter(n: number): string {
  let s = ''
  let x = n
  while (x > 0) {
    const m = (x - 1) % 26
    s = String.fromCharCode(65 + m) + s
    x = Math.floor((x - 1) / 26)
  }
  return s
}

/** A → 1，AA → 27；不是列字母返回 0 */
export function colNumber(letters: string): number {
  let n = 0
  for (const ch of letters.toUpperCase()) {
    const code = ch.charCodeAt(0)
    if (code < 65 || code > 90) return 0
    n = n * 26 + (code - 64)
  }
  return n
}

export interface CellRange { sheet: string | null; r1: number; c1: number; r2: number; c2: number }

/**
 * 「工作表!B5」「工作表!C5:AG7」「B5」→ 区域。工作表名可能带引号（'客流 汇总'!B5），也可能自己含「!」，
 * 所以按最后一个「!」切。认不出返回 null
 */
export function parseRef(ref: string): CellRange | null {
  const at = ref.lastIndexOf('!')
  let sheet: string | null = at >= 0 ? ref.slice(0, at) : null
  if (sheet && sheet.length > 1 && sheet.startsWith("'") && sheet.endsWith("'")) sheet = sheet.slice(1, -1).replace(/''/g, "'")
  const body = (at >= 0 ? ref.slice(at + 1) : ref).replace(/\$/g, '')
  const m = /^([A-Za-z]{1,3})(\d{1,7})(?::([A-Za-z]{1,3})(\d{1,7}))?$/.exec(body.trim())
  if (!m) return null
  const c1 = colNumber(m[1])
  const r1 = Number(m[2])
  const c2 = m[3] ? colNumber(m[3]) : c1
  const r2 = m[4] ? Number(m[4]) : r1
  return { sheet, r1: Math.min(r1, r2), c1: Math.min(c1, c2), r2: Math.max(r1, r2), c2: Math.max(c1, c2) }
}

export const cellName = (r: number, c: number) => `${colLetter(c)}${r}`

/**
 * 各去向的底色。只用已有的令牌混色：深浅两套主题下都跟着变，不另起一套色值。
 * 合计类（交叉表的合计段、列表的合计行）同色：图例里是同一个说法
 */
const TINT: Record<string, string> = {
  value: 'var(--accent)',
  derived_value: 'var(--warn)',
  total_value: 'var(--warn)',
  derived_label: 'var(--nt-metrics)',
  total_label: 'var(--nt-metrics)',
  col_header: 'var(--nt-tool)',
  row_label: 'var(--nt-agent)',
  section_title: 'var(--nt-branch)',
  context: 'var(--ok)',
  outside_text: 'var(--text-faint)',
  ignored_column: 'var(--st-unreached)',
  hidden_excluded: 'var(--st-unreached)',
}

/** 网格格子和图例色块的样式 */
export function roleStyle(role: string | undefined): CSSProperties | undefined {
  if (!role) return undefined
  if (role === 'unclaimed') {
    return {
      background: 'color-mix(in srgb, var(--err) 22%, transparent)',
      boxShadow: 'inset 0 0 0 1px color-mix(in srgb, var(--err) 70%, transparent)',
    }
  }
  const tint = TINT[role]
  return tint ? { background: `color-mix(in srgb, ${tint} 20%, transparent)` } : undefined
}

/** 这些问题的格子就是「没有去处」的格子（执行器的区域标记不含未认领的格，由问题的 cells 指出） */
const UNCLAIMED_CODES = new Set([
  'cell_unclaimed', 'row_unclaimed', 'row_without_label', 'outside_number', 'axis_extra_cells', 'rows_after_stop',
])

/** 网格最多画这么多行：再多浏览器就卡了，预览本身也只给前面一部分 */
const MAX_ROWS = 600

/** 区域在显示的行列里展开成格子键「行,列」；只走显示出来的行列，C5:XFD1048576 这种不会卡住 */
function expand(range: CellRange, rows: number[], cols: number[], out: (key: string) => void) {
  const rs = rows.filter((r) => r >= range.r1 && r <= range.r2)
  const cs = cols.filter((c) => c >= range.c1 && c <= range.c2)
  for (const r of rs) for (const c of cs) out(`${r},${c}`)
}

function axisOf(grid: GridPreview): { rows: number[]; cols: number[] } {
  if (grid.rows?.length && grid.cols?.length) return { rows: grid.rows.slice(0, MAX_ROWS), cols: grid.cols }
  const b = grid.bounds ? parseRef(grid.bounds) : null
  if (b) {
    const rows: number[] = []
    const cols: number[] = []
    for (let r = b.r1; r <= Math.min(b.r2, b.r1 + MAX_ROWS - 1); r++) rows.push(r)
    for (let c = b.c1; c <= b.c2; c++) cols.push(c)
    return { rows, cols }
  }
  // 没有边界：按格子自己拼
  const rs = new Set<number>()
  const cs = new Set<number>()
  for (const [r, c] of grid.cells) { rs.add(r); cs.add(c) }
  return { rows: [...rs].sort((a, b) => a - b).slice(0, MAX_ROWS), cols: [...cs].sort((a, b) => a - b) }
}

interface SheetModel {
  rows: number[]
  cols: number[]
  cells: Map<string, { text: string; kind: string }>
  roles: Map<string, string>
  spans: Map<string, { rs: number; cs: number }>
  covered: Set<string>
  hiddenRows: Set<number>
  hiddenCols: Set<number>
}

function buildModel(grid: GridPreview, marks: RegionMark[], problems: ImportProblem[], partial: boolean): SheetModel {
  const { rows, cols } = axisOf(grid)
  const cells = new Map<string, { text: string; kind: string }>()
  for (const [r, c, text, kind] of grid.cells) cells.set(`${r},${c}`, { text: String(text ?? ''), kind: String(kind ?? 'text') })
  const roles = new Map<string, string>()
  const own = marks.filter((m) => m.sheet === grid.sheet)
  for (const m of own) {
    const range = parseRef(m.ref)
    if (range) expand(range, rows, cols, (k) => { if (cells.has(k)) roles.set(k, m.role) })
  }
  // 没有去处：问题里点名的格，加上（完整干跑过时）区域标记没覆盖到的非空格。部分干跑只看了前面几百行，
  // 窗口外的格本来就没有标记，不能一概标红
  const flagged = new Set<string>()
  for (const p of problems) {
    if (!UNCLAIMED_CODES.has(p.code)) continue
    for (const ref of p.cells ?? []) {
      const range = parseRef(ref)
      if (range && (range.sheet == null || range.sheet === grid.sheet)) expand(range, rows, cols, (k) => flagged.add(k))
    }
  }
  const exhaustive = own.length > 0 && !partial
  for (const k of cells.keys()) {
    if (roles.has(k)) continue
    if (flagged.has(k) || exhaustive) roles.set(k, 'unclaimed')
  }
  // 合并区：左上格跨行跨列，其余格不画。跨度按显示出来的行列数（中间有截掉的行时不会错位）
  const spans = new Map<string, { rs: number; cs: number }>()
  const covered = new Set<string>()
  const rowIndex = new Map(rows.map((r, i) => [r, i]))
  const colIndex = new Map(cols.map((c, i) => [c, i]))
  for (const ref of grid.merges ?? []) {
    const range = parseRef(ref)
    if (!range || !rowIndex.has(range.r1) || !colIndex.has(range.c1)) continue
    const rs = rows.filter((r) => r >= range.r1 && r <= range.r2).length
    const cs = cols.filter((c) => c >= range.c1 && c <= range.c2).length
    if (rs * cs <= 1) continue
    spans.set(`${range.r1},${range.c1}`, { rs, cs })
    expand(range, rows, cols, (k) => { if (k !== `${range.r1},${range.c1}`) covered.add(k) })
  }
  return {
    rows, cols, cells, roles, spans, covered,
    hiddenRows: new Set(grid.hidden_rows ?? []), hiddenCols: new Set(grid.hidden_cols ?? []),
  }
}

/** 要滚到的格子：seq 每次点击都变，同一格点两次也会再滚一次 */
export interface GridFocus { cell: string; seq: number }

export function SheetGrid({ grids, marks, problems, partial = false, focus }: {
  grids: GridPreview[]
  marks: RegionMark[]
  /** 最近一次干跑或试运行的问题：其中点名的格标「没有去处」 */
  problems: ImportProblem[]
  partial?: boolean
  focus?: GridFocus | null
}) {
  const [active, setActive] = useState(grids[0]?.sheet ?? '')
  const [flash, setFlash] = useState<string | null>(null)
  const boxRef = useRef<HTMLDivElement>(null)
  const grid = grids.find((g) => g.sheet === active) ?? grids[0]

  useEffect(() => {
    if (grids.length && !grids.some((g) => g.sheet === active)) setActive(grids[0].sheet)
  }, [grids, active])

  const model = useMemo(() => (grid ? buildModel(grid, marks, problems, partial) : null), [grid, marks, problems, partial])

  // 点了问题、卡片里的坐标：切到那张工作表，把那格滚到中间，闪一下。切表要等下一次渲染才有那一格，
  // 所以分两步：这里记下要去哪，下面的 effect 在画好之后再滚
  const [pending, setPending] = useState<string | null>(null)
  useEffect(() => {
    if (!focus) return
    const range = parseRef(focus.cell)
    if (!range) return
    if (range.sheet && grids.some((g) => g.sheet === range.sheet)) setActive(range.sheet)
    setPending(cellName(range.r1, range.c1))
    // 只在点击（seq 变化）时滚
  }, [focus?.seq])
  useEffect(() => {
    if (!pending) return
    const el = boxRef.current?.querySelector<HTMLElement>(`[data-cell="${pending}"]`)
    el?.scrollIntoView({ block: 'center', inline: 'center' })
    setFlash(pending)
    setPending(null)
  }, [pending, grid?.sheet])
  useEffect(() => {
    if (!flash) return
    const timer = setTimeout(() => setFlash(null), 1600)
    return () => clearTimeout(timer)
  }, [flash, focus?.seq])

  if (!grid || !model) {
    return <div className="rounded-lg border px-3 py-6 text-center text-xs text-faint" data-sheet-grid>{RECIPE_TEXT.noGrid}</div>
  }

  const legend = new Map<string, string>()
  for (const role of model.roles.values()) {
    const label = role === 'unclaimed' ? RECIPE_TEXT.unclaimed : LEDGER_ROLE_LABEL[role]
    if (label && ![...legend.values()].includes(label)) legend.set(role, label)
  }
  const hasHidden = model.hiddenRows.size > 0 || model.hiddenCols.size > 0
  const shownRows = model.rows.length
  const totalRows = grid.total_rows ?? shownRows

  return (
    <div className="flex min-h-0 flex-col gap-2" data-sheet-grid data-sheet={grid.sheet}>
      {grids.length > 1 && (
        <Tabs tabs={grids.map((g) => ({ key: g.sheet, label: g.sheet }))} active={grid.sheet} onChange={setActive}
              label={RECIPE_TEXT.gridLabel} />
      )}
      <div ref={boxRef} className="min-h-0 overflow-auto rounded-lg border bg-bg" style={{ maxHeight: '52vh' }}
           role="region" aria-label={`${RECIPE_TEXT.gridLabel}：${grid.sheet}`} tabIndex={0}>
        <table className="border-separate border-spacing-0 text-2xs">
          <thead>
            <tr>
              <th className="sticky left-0 top-0 z-20 border-b border-r bg-panel px-1" aria-hidden />
              {model.cols.map((c) => (
                <th key={c} scope="col" data-col-header={colLetter(c)}
                    className={clsx('sticky top-0 z-10 border-b border-r bg-panel px-1 font-normal text-faint mono',
                      model.hiddenCols.has(c) && 'opacity-60')}>
                  {colLetter(c)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {model.rows.map((r) => (
              <tr key={r}>
                <th scope="row" data-row-header={r}
                    className={clsx('sticky left-0 z-10 border-b border-r bg-panel px-1 text-right font-normal text-faint tnum',
                      model.hiddenRows.has(r) && 'opacity-60')}>
                  {r}
                </th>
                {model.cols.map((c) => {
                  const key = `${r},${c}`
                  if (model.covered.has(key)) return null
                  const name = cellName(r, c)
                  const cell = model.cells.get(key)
                  const role = model.roles.get(key)
                  const span = model.spans.get(key)
                  const hidden = model.hiddenRows.has(r) || model.hiddenCols.has(c)
                  const formula = grid.formulas?.[name]
                  const label = role === 'unclaimed' ? RECIPE_TEXT.unclaimed : role ? LEDGER_ROLE_LABEL[role] : ''
                  const title = cell
                    ? [`${name}${label ? ` · ${label}` : ''}${hidden ? ` · ${RECIPE_TEXT.hiddenCells}` : ''}`, cell.text, formula]
                      .filter(Boolean).join('\n')
                    : undefined
                  return (
                    <td key={c} data-cell={name} data-role={role} data-hidden={hidden || undefined}
                        rowSpan={span?.rs} colSpan={span?.cs} title={title}
                        className={clsx(
                          'h-6 max-w-[9rem] truncate border-b border-r px-1.5',
                          cell?.kind === 'number' || cell?.kind?.startsWith('formula') ? 'text-right tnum' : 'text-left',
                          hidden && 'opacity-50',
                          flash === name && 'outline outline-2 outline-[var(--accent)]',
                        )}
                        style={{
                          ...roleStyle(role),
                          ...(hidden && !role ? { background: 'color-mix(in srgb, var(--st-unreached) 18%, transparent)' } : null),
                          minWidth: span ? undefined : '3.5rem',
                        }}>
                      {cell?.text}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-2xs text-dim" data-grid-legend aria-label={RECIPE_TEXT.legendTitle}>
        {[...legend.entries()].map(([role, label]) => (
          <span key={role} className="inline-flex items-center gap-1" data-legend={role}>
            <span className="inline-block h-2.5 w-2.5 rounded-sm border" style={roleStyle(role)} aria-hidden />
            {label}
          </span>
        ))}
        {hasHidden && (
          <span className="inline-flex items-center gap-1" data-legend="hidden">
            <span className="inline-block h-2.5 w-2.5 rounded-sm border opacity-50"
                  style={{ background: 'color-mix(in srgb, var(--st-unreached) 18%, transparent)' }} aria-hidden />
            {RECIPE_TEXT.hiddenCells}
          </span>
        )}
        {(grid.truncated || totalRows > shownRows) && (
          <span className="text-faint">{RECIPE_TEXT.gridTruncated(formatNumber(shownRows), formatNumber(totalRows))}</span>
        )}
      </div>
    </div>
  )
}
