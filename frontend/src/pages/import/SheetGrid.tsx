import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties, KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from 'react'
import { BoxSelect } from 'lucide-react'
import clsx from 'clsx'
import type { GridPreview, ImportProblem, RegionMark, SelectionAs } from '../../types'
import { Tabs } from '../../components/ui'
import { formatNumber } from '../../lib/format'
import { LEDGER_ROLE_LABEL, RECIPE_TEXT, SELECT_AS_LABEL } from '../../lib/terms'

// ===========================================================================
// 原始网格：按配方导入时左边那张表。平时只读（点问题、卡片里的坐标会滚到那一格）；起草时可以打开「框选」，
// 在网格上框一块区域、选它是什么，由服务端换算成按文字定位的配方。坐标只用在换算的那一刻，不进配方
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
  if (!c1 || !c2 || !r1 || !r2) return null
  return { sheet, r1: Math.min(r1, r2), c1: Math.min(c1, c2), r2: Math.max(r1, r2), c2: Math.max(c1, c2) }
}

export const cellName = (r: number, c: number) => `${colLetter(c)}${r}`

/** 区域 → 不带工作表名的 A1 写法（单格写「C5」）：框选请求的 ref 就是这个写法（列字母大写、不带 $） */
export const rangeRef = (x: { r1: number; c1: number; r2: number; c2: number }) =>
  (x.r1 === x.r2 && x.c1 === x.c2 ? cellName(x.r1, x.c1) : `${cellName(x.r1, x.c1)}:${cellName(x.r2, x.c2)}`)

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
  // 期 3：按配方忽略的行、区域外数字。与忽略的列同一族颜色：都是「有意不导入」
  ignored: 'var(--st-unreached)',
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

interface Box { r1: number; c1: number; r2: number; c2: number }

interface SheetModel {
  rows: number[]
  cols: number[]
  cells: Map<string, { text: string; kind: string }>
  roles: Map<string, string>
  spans: Map<string, { rs: number; cs: number }>
  covered: Set<string>
  /** 合并区（按工作表坐标，不按显示的行列）：框选与合并区相交时扩到整个合并区 */
  merges: Box[]
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
  const merges: Box[] = []
  const rowIndex = new Map(rows.map((r, i) => [r, i]))
  const colIndex = new Map(cols.map((c, i) => [c, i]))
  for (const ref of grid.merges ?? []) {
    const range = parseRef(ref)
    if (!range) continue
    if (range.r1 !== range.r2 || range.c1 !== range.c2) merges.push({ r1: range.r1, c1: range.c1, r2: range.r2, c2: range.c2 })
    if (!rowIndex.has(range.r1) || !colIndex.has(range.c1)) continue
    const rs = rows.filter((r) => r >= range.r1 && r <= range.r2).length
    const cs = cols.filter((c) => c >= range.c1 && c <= range.c2).length
    if (rs * cs <= 1) continue
    spans.set(`${range.r1},${range.c1}`, { rs, cs })
    expand(range, rows, cols, (k) => { if (k !== `${range.r1},${range.c1}`) covered.add(k) })
  }
  return {
    rows, cols, cells, roles, spans, covered, merges,
    hiddenRows: new Set(grid.hidden_rows ?? []), hiddenCols: new Set(grid.hidden_cols ?? []),
  }
}

/** 选区与合并区相交时扩到整个合并区（Excel 的习惯）：扩了以后可能又碰到别的合并区，所以扩到不再变为止 */
function withMerges(box: Box, merges: Box[]): Box {
  let b = { ...box }
  for (let guard = 0; guard < 50; guard++) {
    let grown = false
    for (const m of merges) {
      const hit = m.r1 <= b.r2 && m.r2 >= b.r1 && m.c1 <= b.c2 && m.c2 >= b.c1
      const inside = m.r1 >= b.r1 && m.r2 <= b.r2 && m.c1 >= b.c1 && m.c2 <= b.c2
      if (hit && !inside) {
        b = { r1: Math.min(b.r1, m.r1), c1: Math.min(b.c1, m.c1), r2: Math.max(b.r2, m.r2), c2: Math.max(b.c2, m.c2) }
        grown = true
      }
    }
    if (!grown) break
  }
  return b
}

const spanOf = (a: { r: number; c: number }, b: { r: number; c: number }): Box =>
  ({ r1: Math.min(a.r, b.r), c1: Math.min(a.c, b.c), r2: Math.max(a.r, b.r), c2: Math.max(a.c, b.c) })

/** 要滚到的格子：seq 每次点击都变，同一格点两次也会再滚一次 */
export interface GridFocus { cell: string; seq: number }

/** 父组件拿到的选区：哪张工作表、不带工作表名的 A1 区域 */
export interface GridSelection { sheet: string; ref: string }

/** 修改后的干跑（修复、框选的预览）：网格可以在「修改前 / 修改后」之间切换 */
export interface GridAfter { marks: RegionMark[]; problems: ImportProblem[]; partial?: boolean }

/** 框选预览的重放比对：框选的范围画虚线（就是选区本身），重放识别出的范围画实线 */
export interface GridReplay { sheet: string; actual: Record<string, string | null> }

// ---------------------------------------------------------------------------
// 表格本体：只随模型变化重画。框选拖动时只有覆盖层在动，几千个单元格不跟着重新渲染
// ---------------------------------------------------------------------------

const GridTable = memo(function GridTable({ grid, model, flash, after }: {
  grid: GridPreview; model: SheetModel; flash: string | null; after: boolean
}) {
  return (
    <table className="border-separate border-spacing-0 text-2xs" data-grid-table>
      <thead>
        <tr>
          <th className="sticky left-0 top-0 z-20 border-b border-r bg-panel px-1" aria-hidden />
          {model.cols.map((c) => (
            <th key={c} scope="col" data-col-header={colLetter(c)} data-col={c}
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
                    data-preview-mark={after && role ? role : undefined}
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
  )
})

/** 显示出来的行列在包裹层里的位置（像素）：覆盖层按它摆，不靠逐格去量 */
interface Layout { rows: Map<number, [number, number]>; cols: Map<number, [number, number]> }

function measure(wrap: HTMLElement): Layout {
  const base = wrap.getBoundingClientRect()
  const rows = new Map<number, [number, number]>()
  const cols = new Map<number, [number, number]>()
  // 行号列是 sticky left（横向滚动时 left 会变），只取它的上下；列号行是 sticky top，只取它的左右
  for (const th of wrap.querySelectorAll<HTMLElement>('th[data-row-header]')) {
    const r = th.getBoundingClientRect()
    rows.set(Number(th.dataset.rowHeader), [r.top - base.top, r.bottom - base.top])
  }
  for (const th of wrap.querySelectorAll<HTMLElement>('th[data-col]')) {
    const r = th.getBoundingClientRect()
    cols.set(Number(th.dataset.col), [r.left - base.left, r.right - base.left])
  }
  return { rows, cols }
}

/** 区域在显示的行列里的像素框；整块都不在显示范围里时返回 null */
function pixelBox(box: Box, model: SheetModel, layout: Layout | null): CSSProperties | null {
  if (!layout) return null
  const rs = model.rows.filter((r) => r >= box.r1 && r <= box.r2)
  const cs = model.cols.filter((c) => c >= box.c1 && c <= box.c2)
  if (!rs.length || !cs.length) return null
  const top = layout.rows.get(rs[0])?.[0]
  const bottom = layout.rows.get(rs[rs.length - 1])?.[1]
  const left = layout.cols.get(cs[0])?.[0]
  const right = layout.cols.get(cs[cs.length - 1])?.[1]
  if (top == null || bottom == null || left == null || right == null) return null
  return { top, left, width: right - left, height: bottom - top }
}

export function SheetGrid({
  grids, marks, problems, partial = false, focus, selectable = false, selection = null, onSelection, onSelectAs,
  highlight, after = null, replay = null,
}: {
  grids: GridPreview[]
  marks: RegionMark[]
  /** 最近一次干跑或试运行的问题：其中点名的格标「没有去处」 */
  problems: ImportProblem[]
  partial?: boolean
  focus?: GridFocus | null
  /** 起草时给「框选」开关 */
  selectable?: boolean
  /** 父组件记下的选区（拖动结束、键盘扩选、输入区域之后才报上去） */
  selection?: GridSelection | null
  onSelection?: (sel: GridSelection | null) => void
  /** 「框选为…」选了一项 */
  onSelectAs?: (as: SelectionAs) => void
  /** 修复面板相关的格（「工作表!A1」或「工作表!A1:B2」）：描出来，告诉人改的是哪几格 */
  highlight?: string[]
  /** 修改后的干跑：给了就出「修改前 / 修改后」切换 */
  after?: GridAfter | null
  replay?: GridReplay | null
}) {
  const [active, setActive] = useState(grids[0]?.sheet ?? '')
  const [flash, setFlash] = useState<string | null>(null)
  const [outside, setOutside] = useState<string | null>(null)
  const [selecting, setSelecting] = useState(false)
  const [live, setLiveState] = useState<Box | null>(null)
  // 拖动结束时要把「此刻的选区」报给父组件：状态更新是异步的，另记一份在 ref 里，不在 setState 的回调里报
  const liveRef = useRef<Box | null>(null)
  const setLive = useCallback((box: Box | null) => { liveRef.current = box; setLiveState(box) }, [])
  const [cursor, setCursor] = useState<{ r: number; c: number } | null>(null)
  const [refText, setRefText] = useState('')
  const [refError, setRefError] = useState(false)
  const [view, setView] = useState<'before' | 'after'>('before')
  const [layout, setLayout] = useState<Layout | null>(null)
  const boxRef = useRef<HTMLDivElement>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  const menuRef = useRef<HTMLSelectElement>(null)
  const anchor = useRef<{ r: number; c: number } | null>(null)
  const drag = useRef<{ on: boolean; x: number; y: number; frame: number }>({ on: false, x: 0, y: 0, frame: 0 })
  const grid = grids.find((g) => g.sheet === active) ?? grids[0]

  useEffect(() => {
    if (grids.length && !grids.some((g) => g.sheet === active)) setActive(grids[0].sheet)
  }, [grids, active])

  // 预览没了（面板关了）就回到「修改前」
  useEffect(() => { if (!after) setView('before') }, [after])
  // 框选开关只在起草时有：离开起草就关掉，选区一并清掉
  useEffect(() => { if (!selectable) { setSelecting(false); setLive(null); setCursor(null) } }, [selectable])

  const showAfter = !!after && view === 'after'
  const model = useMemo(() => (grid
    ? buildModel(grid, showAfter ? after!.marks : marks, showAfter ? after!.problems : problems, showAfter ? !!after!.partial : partial)
    : null), [grid, marks, problems, partial, showAfter, after])

  // 父组件清掉或改了选区（应用之后、关面板）：跟上
  useEffect(() => {
    if (!selection) { setLive(null); return }
    if (selection.sheet !== grid?.sheet) return
    const r = parseRef(selection.ref)
    if (r) setLive({ r1: r.r1, c1: r.c1, r2: r.r2, c2: r.c2 })
  }, [selection?.sheet, selection?.ref])
  useEffect(() => { setRefText(live ? rangeRef(live) : ''); setRefError(false) }, [live?.r1, live?.c1, live?.r2, live?.c2])

  // 量一次行列的位置：模型变了、窗口或表格大小变了时重量
  useLayoutEffect(() => {
    const wrap = wrapRef.current
    if (!wrap || !model) { setLayout(null); return }
    setLayout(measure(wrap))
    const ro = new ResizeObserver(() => setLayout(measure(wrap)))
    ro.observe(wrap)
    return () => ro.disconnect()
  }, [model])

  const commit = useCallback((box: Box | null) => {
    if (!grid) return
    onSelection?.(box ? { sheet: grid.sheet, ref: rangeRef(box) } : null)
  }, [grid, onSelection])

  const clearSelection = useCallback((notify = true) => {
    const had = !!live
    setLive(null)
    anchor.current = null
    if (notify && had) commit(null)
  }, [live, commit])

  const switchSheet = (sheet: string) => {
    if (sheet === active) return
    // 切换工作表页签时清空选区：选区只对它所在的那张表有意义
    clearSelection()
    setCursor(null)
    setActive(sheet)
  }

  // 点了问题、卡片里的坐标：切到那张工作表，把那格滚到中间，闪一下。切表要等下一次渲染才有那一格，
  // 所以分两步：这里记下要去哪，下面的 effect 在画好之后再滚
  const [pending, setPending] = useState<{ r: number; c: number } | null>(null)
  useEffect(() => {
    if (!focus) return
    const range = parseRef(focus.cell)
    if (!range) return
    if (range.sheet && grids.some((g) => g.sheet === range.sheet) && range.sheet !== active) {
      clearSelection()
      setActive(range.sheet)
    }
    setPending({ r: range.r1, c: range.c1 })
    // 只在点击（seq 变化）时滚
  }, [focus?.seq])
  useEffect(() => {
    if (!pending || !model) return
    setPending(null)
    // 行不在预览里（超出前 300 行，或者截掉了），或者列在预览的右边之外：说清楚，不静默无反应。
    // 列在预览左边之外（「工作表!A32」这种按行定位的写法）就落到这一行最左的一格
    const lastCol = model.cols[model.cols.length - 1]
    if (!model.rows.includes(pending.r) || lastCol == null || pending.c > lastCol) {
      setOutside(cellName(pending.r, pending.c))
      return
    }
    setOutside(null)
    const c = model.cols.find((x) => x >= pending.c) ?? model.cols[0]
    // 落在合并区里的格不单独画：滚到合并区的左上格
    const m = model.merges.find((x) => pending.r >= x.r1 && pending.r <= x.r2 && c >= x.c1 && c <= x.c2)
    const name = m && model.covered.has(`${pending.r},${c}`) ? cellName(m.r1, m.c1) : cellName(pending.r, c)
    const el = boxRef.current?.querySelector<HTMLElement>(`[data-cell="${name}"]`)
    el?.scrollIntoView({ block: 'center', inline: 'center' })
    setFlash(name)
  }, [pending, model])
  useEffect(() => {
    if (!flash) return
    const timer = setTimeout(() => setFlash(null), 1600)
    return () => clearTimeout(timer)
  }, [flash, focus?.seq])

  // ---- 框选：鼠标、触控笔
  const cellAt = (x: number, y: number, prev: { r: number; c: number } | null): { r: number; c: number } | null => {
    const hit = document.elementFromPoint(x, y)?.closest<HTMLElement>('td[data-cell], th[data-row-header], th[data-col]')
    if (!hit || !wrapRef.current?.contains(hit)) return prev
    if (hit.dataset.cell) {
      const r = parseRef(hit.dataset.cell)
      return r ? { r: r.r1, c: r.c1 } : prev
    }
    // 拖到了吸顶的行号、列号上：只取得到一个方向，另一个方向沿用上一格
    if (hit.dataset.rowHeader) return prev ? { r: Number(hit.dataset.rowHeader), c: prev.c } : null
    if (hit.dataset.col) return prev ? { r: prev.r, c: Number(hit.dataset.col) } : null
    return prev
  }

  const last = useRef<{ r: number; c: number } | null>(null)
  const tick = () => {
    const d = drag.current
    d.frame = 0
    const box = boxRef.current
    if (!d.on || !box || !model) return
    const br = box.getBoundingClientRect()
    // 拖到可视区边缘时自动滚动：越靠外滚得越快
    const EDGE = 28
    const speed = (dist: number) => Math.min(24, Math.ceil((EDGE - dist) / 2))
    let dx = 0
    let dy = 0
    if (d.x < br.left + EDGE + 32) dx = -speed(d.x - br.left - 32)
    else if (d.x > br.right - EDGE) dx = speed(br.right - d.x)
    if (d.y < br.top + EDGE + 24) dy = -speed(d.y - br.top - 24)
    else if (d.y > br.bottom - EDGE) dy = speed(br.bottom - d.y)
    if (dx || dy) box.scrollBy(dx, dy)
    const x = Math.min(Math.max(d.x, br.left + 2), br.right - 4)
    const y = Math.min(Math.max(d.y, br.top + 2), br.bottom - 4)
    const at = cellAt(x, y, last.current)
    if (at && anchor.current) {
      last.current = at
      setCursor(at)
      setLive(withMerges(spanOf(anchor.current, at), model.merges))
    }
    if ((dx || dy) && d.on) d.frame = requestAnimationFrame(tick)
  }
  const onPointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!selecting || !model || e.button !== 0) return
    const at = cellAt(e.clientX, e.clientY, null)
    if (!at) return
    e.preventDefault()
    e.currentTarget.setPointerCapture(e.pointerId)
    boxRef.current?.focus({ preventScroll: true })
    anchor.current = at
    last.current = at
    setCursor(at)
    setLive(withMerges(spanOf(at, at), model.merges))
    drag.current = { on: true, x: e.clientX, y: e.clientY, frame: 0 }
  }
  const onPointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const d = drag.current
    if (!d.on) return
    d.x = e.clientX
    d.y = e.clientY
    // 按帧节流：一帧最多算一次落在哪一格
    if (!d.frame) d.frame = requestAnimationFrame(tick)
  }
  const endDrag = () => {
    const d = drag.current
    if (!d.on) return
    d.on = false
    if (d.frame) cancelAnimationFrame(d.frame)
    d.frame = 0
    if (liveRef.current) commit(liveRef.current)
  }
  useEffect(() => () => { if (drag.current.frame) cancelAnimationFrame(drag.current.frame) }, [])

  // ---- 框选：键盘。当前格（光标）用方向键移动，Shift + 方向键扩选，Enter 打开「框选为…」，Esc 取消选区
  const moveCursor = (dr: number, dc: number, extend: boolean) => {
    if (!model || !model.rows.length || !model.cols.length) return
    const from = cursor ?? { r: model.rows[0], c: model.cols[0] }
    const ri = Math.max(0, model.rows.findIndex((r) => r >= from.r))
    const ci = Math.max(0, model.cols.findIndex((c) => c >= from.c))
    const next = {
      r: model.rows[Math.min(model.rows.length - 1, Math.max(0, ri + dr))],
      c: model.cols[Math.min(model.cols.length - 1, Math.max(0, ci + dc))],
    }
    setCursor(next)
    if (extend) {
      anchor.current ??= from
      const box = withMerges(spanOf(anchor.current, next), model.merges)
      setLive(box)
      commit(box)
    } else {
      anchor.current = next
      if (live) { setLive(null); commit(null) }
    }
    requestAnimationFrame(() => {
      boxRef.current?.querySelector<HTMLElement>(`[data-cell="${cellName(next.r, next.c)}"]`)
        ?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
    })
  }
  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (!selecting) return
    const dirs: Record<string, [number, number]> = { ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1] }
    const d = dirs[e.key]
    if (d) {
      e.preventDefault()
      moveCursor(d[0], d[1], e.shiftKey)
    } else if (e.key === 'Enter' && live) {
      e.preventDefault()
      menuRef.current?.focus()
    } else if (e.key === 'Escape' && live) {
      // 只取消选区：不 preventDefault 的话，向导的弹窗会把这一下 Esc 当成「关闭」
      e.preventDefault()
      clearSelection()
    }
  }
  const onBoxFocus = () => {
    if (selecting && !cursor && model?.rows.length && model.cols.length) setCursor({ r: model.rows[0], c: model.cols[0] })
  }

  // ---- 框选：输入区域（窄屏、触屏、超出视口的大范围都用它）
  const applyRefText = () => {
    const text = refText.trim()
    if (!text) { setRefError(false); return }
    if (live && text === rangeRef(live)) return
    const r = parseRef(text)
    if (!r || !model) { setRefError(true); return }
    if (r.sheet && r.sheet !== grid?.sheet) {
      if (!grids.some((g) => g.sheet === r.sheet)) { setRefError(true); return }
      // 写了另一张工作表：切过去，下一次渲染再按那张表的合并区扩选
      setActive(r.sheet)
    }
    setRefError(false)
    const box = withMerges({ r1: r.r1, c1: r.c1, r2: r.r2, c2: r.c2 }, model.merges)
    anchor.current = { r: box.r1, c: box.c1 }
    setCursor({ r: box.r2, c: box.c2 })
    setLive(box)
    onSelection?.({ sheet: r.sheet ?? grid!.sheet, ref: rangeRef(box) })
  }

  const toggleSelecting = () => {
    if (selecting) {
      clearSelection()
      setCursor(null)
    }
    setSelecting((v) => !v)
  }

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

  // ---- 覆盖层：按块描边、修复相关的格、重放识别的范围、选区、当前格
  const blocks = new Map<string, Box>()
  for (const m of (showAfter ? after!.marks : marks)) {
    if (!m.block || m.sheet !== grid.sheet) continue
    const r = parseRef(m.ref)
    if (!r) continue
    const cur = blocks.get(m.block)
    blocks.set(m.block, cur
      ? { r1: Math.min(cur.r1, r.r1), c1: Math.min(cur.c1, r.c1), r2: Math.max(cur.r2, r.r2), c2: Math.max(cur.c2, r.c2) }
      : { r1: r.r1, c1: r.c1, r2: r.r2, c2: r.c2 })
  }
  const lit = (highlight ?? []).map((ref) => parseRef(ref))
    .filter((r): r is CellRange => !!r && (r.sheet == null || r.sheet === grid.sheet))
  const replayBoxes = replay && replay.sheet === grid.sheet
    ? Object.entries(replay.actual).map(([part, ref]) => [part, ref ? parseRef(ref) : null] as const)
      .filter((x): x is readonly [string, CellRange] => !!x[1])
    : []
  const selBox = live ? pixelBox(live, model, layout) : null
  const cursorBox = selecting && cursor
    ? pixelBox(withMerges({ r1: cursor.r, c1: cursor.c, r2: cursor.r, c2: cursor.c }, model.merges), model, layout)
    : null

  return (
    <div className="flex min-h-0 min-w-0 flex-col gap-2" data-sheet-grid data-sheet={grid.sheet}>
      <div className="flex flex-wrap items-end gap-2">
        <div className="min-w-0 flex-1">
          {grids.length > 1 && (
            <Tabs tabs={grids.map((g) => ({ key: g.sheet, label: g.sheet }))} active={grid.sheet} onChange={switchSheet}
                  label={RECIPE_TEXT.gridLabel} />
          )}
        </div>
        {after && (
          <div className="inline-flex items-center gap-1 text-2xs" role="group" aria-label={RECIPE_TEXT.previewToggle} data-preview-toggle={view}>
            {(['before', 'after'] as const).map((v) => (
              <button key={v} type="button" className={clsx('btn btn-xs', view === v && 'btn-primary')} aria-pressed={view === v}
                      onClick={() => setView(v)}>
                {v === 'before' ? RECIPE_TEXT.previewBefore : RECIPE_TEXT.previewAfter}
              </button>
            ))}
          </div>
        )}
        {selectable && (
          <button type="button" className={clsx('btn btn-xs', selecting && 'btn-primary')} aria-pressed={selecting}
                  title={RECIPE_TEXT.selectToggleHint} onClick={toggleSelecting} data-select-toggle={selecting ? 'on' : 'off'}>
            <BoxSelect size={11} aria-hidden /> {RECIPE_TEXT.selectToggle}
          </button>
        )}
      </div>
      <div ref={boxRef} className={clsx('min-h-0 overflow-auto rounded-lg border bg-bg', selecting && 'cursor-crosshair select-none')}
           style={{ maxHeight: '52vh', touchAction: selecting ? 'none' : undefined }}
           role="region" aria-label={`${RECIPE_TEXT.gridLabel}：${grid.sheet}`} tabIndex={0} data-grid-box
           onKeyDown={onKeyDown} onFocus={onBoxFocus}>
        <div ref={wrapRef} className="relative w-max min-w-full"
             onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={endDrag} onPointerCancel={endDrag}>
          <GridTable grid={grid} model={model} flash={flash} after={showAfter} />
          <div className="pointer-events-none absolute inset-0 z-[5]" aria-hidden data-grid-overlay>
            {[...blocks.entries()].map(([id, b]) => {
              const s = pixelBox(b, model, layout)
              return s && (
                <div key={id} className="absolute rounded-sm" data-block-outline={id}
                     style={{ ...s, boxShadow: 'inset 0 0 0 1px color-mix(in srgb, var(--accent) 55%, transparent)' }} />
              )
            })}
            {lit.map((r, i) => {
              const s = pixelBox(r, model, layout)
              return s && (
                <div key={`h${i}`} className="absolute rounded-sm" data-fix-cell={rangeRef(r)}
                     style={{ ...s, boxShadow: 'inset 0 0 0 2px var(--warn)' }} />
              )
            })}
            {replayBoxes.map(([part, r]) => {
              const s = pixelBox(r, model, layout)
              return s && (
                <div key={`r${part}`} className="absolute" data-replay-mark={part} data-ref={rangeRef(r)}
                     style={{ ...s, border: '2px solid var(--ok)' }} />
              )
            })}
            {live && (
              <div className="absolute" data-selection={rangeRef(live)}
                   style={{ ...(selBox ?? { display: 'none' }), border: '2px dashed var(--accent)',
                     background: 'color-mix(in srgb, var(--accent) 8%, transparent)' }} />
            )}
            {cursorBox && cursor && (
              <div className="absolute" data-grid-cursor={cellName(cursor.r, cursor.c)}
                   style={{ ...cursorBox, boxShadow: 'inset 0 0 0 2px var(--text)' }} />
            )}
          </div>
        </div>
      </div>
      {selecting && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border bg-panel px-2.5 py-1.5 text-xs" data-selection-bar>
          <span className="min-w-0 shrink-0 font-medium" data-selection-label>
            {live ? RECIPE_TEXT.selected(rangeRef(live)) : <span className="font-normal text-faint">{RECIPE_TEXT.selectionNone}</span>}
          </span>
          <label className="inline-flex items-center gap-1">
            <span className="text-2xs text-faint">{RECIPE_TEXT.selectionRef}</span>
            <input className="field mono !h-6 !w-28 !py-0 text-2xs" value={refText} placeholder={RECIPE_TEXT.selectionRefPlaceholder}
                   aria-invalid={refError || undefined} spellCheck={false} autoComplete="off" data-selection-ref
                   onChange={(e) => { setRefText(e.target.value); setRefError(false) }}
                   onBlur={applyRefText}
                   onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); applyRefText() } }} />
          </label>
          <select ref={menuRef} className="field !h-6 !w-auto !py-0 text-2xs" value="" disabled={!live} data-selection-menu
                  aria-label={RECIPE_TEXT.selectionMenu}
                  onChange={(e) => { const v = e.target.value as SelectionAs; if (v) onSelectAs?.(v) }}>
            <option value="">{RECIPE_TEXT.selectionMenu}</option>
            {Object.entries(SELECT_AS_LABEL).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select>
          <button type="button" className="btn btn-xs" disabled={!live} onClick={() => clearSelection()} data-selection-cancel>
            {RECIPE_TEXT.selectionCancel}
          </button>
          {refError && <span className="basis-full text-2xs text-[var(--err)]" role="alert" data-selection-ref-error>{RECIPE_TEXT.selectionRefInvalid}</span>}
        </div>
      )}
      {outside && (
        <p className="text-2xs text-[var(--warn)]" role="status" data-grid-out-of-range={outside}>{RECIPE_TEXT.outOfPreview}</p>
      )}
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
