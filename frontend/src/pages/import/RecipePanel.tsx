import { createContext, useContext, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Plus, X } from 'lucide-react'
import clsx from 'clsx'
import type { Recipe, RecipeProblem } from '../../types'
import { Spinner } from '../../components/ui'
import { RECIPE_CHOICE_LABEL, RECIPE_TEXT } from '../../lib/terms'

// ===========================================================================
// 配方的几个纯函数：补全默认值、推出每张表的列、改名时连带改引用
// ===========================================================================

const isObj = (v: unknown): v is Record<string, any> => !!v && typeof v === 'object' && !Array.isArray(v)
const arr = (v: unknown): any[] => (Array.isArray(v) ? v : [])
const clone = <T,>(v: T): T => JSON.parse(JSON.stringify(v)) as T

/** JSON Pointer 的一段：~ 写成 ~0，/ 写成 ~1（RFC 6901） */
export const ptr = (key: string | number) => String(key).replace(/~/g, '~0').replace(/\//g, '~1')

/**
 * 补全默认值，取值与服务端 recipe_types.py 的默认值一致。服务端存的是去掉默认值的紧凑形式，表单要知道
 * 每个开关现在是什么，「查看配方 JSON」也要给完整的样子。形状不对的部分原样留着（服务端的检查会报出来）
 */
export function fillRecipeDefaults(input: Recipe | null | undefined): Recipe | null {
  if (!isObj(input)) return null
  const r = clone(input)
  r.mode ??= 'replace'
  r.other_visible_sheets ??= 'confirm'
  r.relations ??= []
  const placeholders = (v: unknown) => arr(v).map((p) => (isObj(p) ? { meaning: '无数据', ...p } : p))
  for (const sheet of arr(r.sheets)) {
    if (!isObj(sheet)) continue
    sheet.match = isObj(sheet.match) ? sheet.match : { name: '' }
    sheet.match.fallback ??= 'only_visible_sheet'
    sheet.hidden = { rows: 'reject_if_any', cols: 'reject_if_any', ...(isObj(sheet.hidden) ? sheet.hidden : {}) }
    sheet.context = arr(sheet.context).map((c) => (isObj(c)
      ? { id: '统计期', kind: 'period', parser: 'cn_date_range', prefer_prefix: null, cross_check: 'filename', ...c }
      : c))
    for (const block of arr(sheet.blocks)) {
      if (!isObj(block)) continue
      if (block.layout === 'crosstab') {
        const axis = isObj(block.axis) ? block.axis : {}
        block.axis = {
          name: '日期', type: 'DATE', year_from: '统计期', checks: ['contiguous', 'covers_context'], ...axis,
          find: { parser: 'month_day_or_date', min: 2, ...(isObj(axis.find) ? axis.find : {}) },
        }
        block.label_offset ??= -1
        block.values = {
          type: 'INTEGER', blank: 'reject', text_number: 'reject', formula: 'reject', ...(isObj(block.values) ? block.values : {}),
        }
        block.values.placeholders = placeholders(block.values.placeholders)
        for (const seg of arr(block.segments)) {
          if (!isObj(seg)) continue
          if (seg.role === 'dimension') {
            seg.const ??= {}
            seg.stop_parser ??= null
            if (isObj(seg.dim)) seg.dim.derive ??= {}
          } else if (seg.role === 'derived') {
            seg.labels_parser ??= 'hour_range_total'
            seg.keep_as ??= null
            if (isObj(seg.verify)) seg.verify.kind ??= 'label_range_sum'
            if (isObj(seg.keep_as)) seg.keep_as.derive ??= {}
          }
        }
      } else if (block.layout === 'list') {
        block.header_rows ??= 1
        block.after_title ??= null
        block.extra_columns ??= 'reject'
        block.merged_data ??= 'reject'
        block.rows = { blank_rows: 'stop', total_row: null, ...(isObj(block.rows) ? block.rows : {}) }
        if (isObj(block.rows.total_row)) block.rows.total_row.keep_as ??= null
        block.values = {
          blank: 'null', text_number: 'reject', formula: 'accept_cached', ...(isObj(block.values) ? block.values : {}),
        }
        block.values.placeholders = placeholders(block.values.placeholders)
        block.columns = arr(block.columns).map((c) => (isObj(c) ? { store: null, ...c } : c))
      }
    }
  }
  for (const t of arr(r.tables)) {
    if (!isObj(t)) continue
    t.grain ??= []
    t.kind ??= 'data'
    t.units ??= {}
    t.note ??= ''
  }
  for (const rel of arr(r.relations)) if (isObj(rel) && rel.kind !== 'dismissed') rel.claims ??= null
  return r
}

export interface RecipeColumn { name: string; type: string; role: string; header?: string }

/** 列表合计行另存的表固定有这一列（契约 LIST_TOTAL_DIM） */
const LIST_TOTAL_DIM = '合计项'

/**
 * 由配方推出每张表的列，照服务端 derive_tables 的顺序：宽表 = 轴、各指标；长表 = 轴、维度、常量（按列名排序）、
 * 派生（start 在前）、值；表内合计 = 轴、合计项、派生、值；列表 = 列的顺序；列表合计表 = 合计项、各数字列。
 * 只给表单用（单位、主键、关系成员的下拉），建表和检查以服务端为准
 */
export function tableColumns(recipe: Recipe | null): Map<string, RecipeColumn[]> {
  const out = new Map<string, RecipeColumn[]>()
  if (!recipe) return out
  const put = (table: unknown, cols: RecipeColumn[]) => {
    if (typeof table === 'string' && !out.has(table)) out.set(table, cols)
  }
  const derive = (d: unknown): RecipeColumn[] => Object.entries(isObj(d) ? d : {})
    .sort(([a, ra], [b, rb]) => (Number(ra !== 'start') - Number(rb !== 'start')) || (a < b ? -1 : a > b ? 1 : 0))
    .map(([name]) => ({ name, type: 'INTEGER', role: 'derive' }))
  for (const sheet of arr(recipe.sheets)) {
    for (const block of arr(sheet?.blocks)) {
      if (!isObj(block)) continue
      if (block.layout === 'crosstab') {
        const vtype = block.values?.type ?? 'INTEGER'
        const axis: RecipeColumn = { name: block.axis?.name ?? '日期', type: 'TEXT', role: 'axis' }
        for (const seg of arr(block.segments)) {
          if (!isObj(seg)) continue
          if (seg.role === 'measures') {
            put(seg.table, [axis, ...arr(seg.labels?.expect).map((label: string) => ({
              name: String(seg.measures?.[label] ?? ''), type: vtype, role: 'measure', header: label,
            }))])
          } else if (seg.role === 'dimension') {
            put(seg.table, [
              axis, { name: seg.dim?.name ?? '', type: 'TEXT', role: 'dim' },
              ...Object.keys(isObj(seg.const) ? seg.const : {}).sort().map((name) => ({ name, type: 'TEXT', role: 'const' })),
              ...derive(seg.dim?.derive), { name: seg.value ?? '', type: vtype, role: 'value' },
            ])
          } else if (seg.role === 'derived' && isObj(seg.keep_as)) {
            const k = seg.keep_as
            put(k.table, [axis, { name: k.dim ?? '', type: 'TEXT', role: 'dim' }, ...derive(k.derive), { name: k.value ?? '', type: vtype, role: 'value' }])
          }
        }
      } else if (block.layout === 'list') {
        const cols: RecipeColumn[] = arr(block.columns).map((c) => ({
          name: String(c?.name ?? ''), type: c?.type === 'DATE' ? 'TEXT' : String(c?.type ?? 'TEXT'),
          role: c?.type === 'INTEGER' || c?.type === 'REAL' ? 'measure' : 'text', header: c?.header,
        }))
        put(block.table, cols)
        const total = block.rows?.total_row
        if (isObj(total) && total.keep_as) {
          put(total.keep_as, [{ name: LIST_TOTAL_DIM, type: 'TEXT', role: 'dim' },
            ...cols.filter((c) => c.type === 'INTEGER' || c.type === 'REAL').map((c) => ({ ...c, role: 'value' }))])
        }
      }
    }
  }
  return out
}

const renameKey = (o: unknown, old: string, nu: string) => (isObj(o)
  ? Object.fromEntries(Object.entries(o).map(([k, v]) => [k === old ? nu : k, v]))
  : o)

/** 表改名：配方里引用这张表的地方一并改（配方内部的引用都要求与表名逐字相同） */
export function renameTable(r: Recipe, old: string, nu: string) {
  for (const t of arr(r.tables)) if (t?.name === old) t.name = nu
  for (const sheet of arr(r.sheets)) {
    for (const block of arr(sheet?.blocks)) {
      if (!isObj(block)) continue
      if (block.table === old) block.table = nu
      if (isObj(block.rows?.total_row) && block.rows.total_row.keep_as === old) block.rows.total_row.keep_as = nu
      for (const seg of arr(block.segments)) {
        if (!isObj(seg)) continue
        if (seg.table === old) seg.table = nu
        if (isObj(seg.verify) && seg.verify.against_table === old) seg.verify.against_table = nu
        if (isObj(seg.keep_as) && seg.keep_as.table === old) seg.keep_as.table = nu
      }
    }
  }
  for (const rel of arr(r.relations)) {
    if (!isObj(rel)) continue
    if (rel.table === old) rel.table = nu
    if (isObj(rel.a) && rel.a.table === old) rel.a.table = nu
    if (isObj(rel.b) && rel.b.table === old) rel.b.table = nu
  }
}

/** 某张表的列改名之后，引用它的地方（单位、主键、关系、合计核对、合计行的标签列）跟着改 */
function renameRefs(r: Recipe, table: string, old: string, nu: string) {
  for (const t of arr(r.tables)) {
    if (t?.name !== table) continue
    t.units = renameKey(t.units, old, nu)
    t.grain = arr(t.grain).map((g) => (g === old ? nu : g))
  }
  for (const rel of arr(r.relations)) {
    if (!isObj(rel)) continue
    if (rel.kind === 'sum_eq' && rel.table === table) {
      if (rel.total === old) rel.total = nu
      rel.parts = arr(rel.parts).map((p) => (p === old ? nu : p))
    }
    if (rel.kind === 'not_comparable') {
      if (rel.a?.table === table && rel.a.value === old) rel.a.value = nu
      if (rel.b?.table === table && rel.b.value === old) rel.b.value = nu
      if (rel.by === old && (rel.a?.table === table || rel.b?.table === table)) rel.by = nu
    }
  }
  for (const sheet of arr(r.sheets)) {
    for (const block of arr(sheet?.blocks)) {
      if (!isObj(block)) continue
      if (block.table === table && isObj(block.rows?.total_row) && block.rows.total_row.label_column === old) {
        block.rows.total_row.label_column = nu
      }
      for (const seg of arr(block.segments)) {
        if (isObj(seg?.verify) && seg.verify.against_table === table && seg.verify.value === old) seg.verify.value = nu
      }
    }
  }
}

/** 表里的一列改名：改它的来处（指标、维度、值、常量、派生、合计项、列表列），再改引用 */
export function renameColumn(r: Recipe, table: string, old: string, nu: string) {
  if (!nu || old === nu) return
  for (const sheet of arr(r.sheets)) {
    for (const block of arr(sheet?.blocks)) {
      if (!isObj(block)) continue
      if (block.layout === 'list' && block.table === table) {
        for (const c of arr(block.columns)) if (c?.name === old) c.name = nu
      }
      for (const seg of arr(block.segments)) {
        if (!isObj(seg)) continue
        if (seg.role === 'measures' && seg.table === table && isObj(seg.measures)) {
          for (const k of Object.keys(seg.measures)) if (seg.measures[k] === old) seg.measures[k] = nu
        }
        if (seg.role === 'dimension' && seg.table === table) {
          if (isObj(seg.dim)) {
            if (seg.dim.name === old) seg.dim.name = nu
            seg.dim.derive = renameKey(seg.dim.derive, old, nu)
          }
          if (seg.value === old) seg.value = nu
          seg.const = renameKey(seg.const, old, nu)
        }
        if (seg.role === 'derived' && isObj(seg.keep_as) && seg.keep_as.table === table) {
          if (seg.keep_as.dim === old) seg.keep_as.dim = nu
          if (seg.keep_as.value === old) seg.keep_as.value = nu
          seg.keep_as.derive = renameKey(seg.keep_as.derive, old, nu)
        }
      }
    }
  }
  renameRefs(r, table, old, nu)
}

/** 交叉表的日期列改名：这个块写入的每张表都有这一列 */
function renameAxis(r: Recipe, s: number, b: number, nu: string) {
  const block = r.sheets?.[s]?.blocks?.[b]
  if (!isObj(block) || !nu) return
  const old = block.axis?.name ?? '日期'
  if (old === nu) return
  const tables = new Set<string>()
  for (const seg of arr(block.segments)) {
    if (typeof seg?.table === 'string') tables.add(seg.table)
    if (isObj(seg?.keep_as) && typeof seg.keep_as.table === 'string') tables.add(seg.keep_as.table)
  }
  for (const t of tables) renameRefs(r, t, old, nu)
  block.axis = { ...(isObj(block.axis) ? block.axis : {}), name: nu }
}

/** 「全日客流（人次）」→ 列名「全日客流」、单位「人次」（单位在词表里才算） */
function splitUnit(label: string, units: string[]): [string, string | null] {
  const m = /^(.*?)\s*[（(]\s*([^（）()]{1,8})\s*[）)]\s*$/.exec(label)
  if (!m || !m[1].trim()) return [label.trim(), null]
  return [m[1].trim(), units.includes(m[2]) ? m[2] : null]
}

// ===========================================================================
// 字段旁的问题：静态校验的问题按 path 挂到最贴近的字段上，挂不上的显示在面板顶部
// ===========================================================================

interface ProblemsApi { register: (path: string) => void; assigned: Map<string, RecipeProblem[]> }
const ProblemsContext = createContext<ProblemsApi>({ register: () => {}, assigned: new Map() })

function FieldProblems({ path }: { path: string }) {
  const ctx = useContext(ProblemsContext)
  ctx.register(path)
  const list = ctx.assigned.get(path) ?? []
  if (!list.length) return null
  return (
    <ul className="mt-1 space-y-0.5">
      {list.map((p, i) => (
        <li key={i} className="text-2xs leading-relaxed text-[var(--err)]" data-recipe-problem={p.code} data-path={p.path}>{p.message}</li>
      ))}
    </ul>
  )
}

/** 一个字段：标签、控件、它自己的问题 */
function FieldWrap({ path, label, hint, children, className }: {
  path: string; label?: ReactNode; hint?: ReactNode; children: ReactNode; className?: string
}) {
  return (
    <div className={clsx('min-w-0', className)} data-field-wrap={path}>
      {label && <div className="label">{label}</div>}
      {children}
      {hint && <div className="mt-0.5 text-2xs text-faint">{hint}</div>}
      <FieldProblems path={path} />
    </div>
  )
}

/** 文本框：失焦或回车时才保存（每次保存都是一次整份配方的检查，不按键就发）；Esc 撤回 */
function TextInput({ path, value, onCommit, placeholder, allowEmpty = false, className, label }: {
  path: string; value: string; onCommit: (v: string) => void; placeholder?: string; allowEmpty?: boolean
  className?: string; label: string
}) {
  const [text, setText] = useState(value)
  useEffect(() => { setText(value) }, [value])
  const commit = () => {
    const v = text.trim()
    if (v === value) return
    if (!v && !allowEmpty) { setText(value); return }
    onCommit(v)
  }
  return (
    <input className={clsx('field', className)} value={text} placeholder={placeholder} data-field={path} aria-label={label}
           onChange={(e) => setText(e.target.value)} onBlur={commit}
           onKeyDown={(e) => {
             if (e.key === 'Enter' && !e.nativeEvent.isComposing) { e.preventDefault(); commit() }
             if (e.key === 'Escape' && text !== value) { e.preventDefault(); e.stopPropagation(); setText(value) }
           }} />
  )
}

/** 单选（原生 radio：方向键在组内移动，读屏念得出组名） */
function Radios({ path, value, choices, onChange, label }: {
  path: string; value: string | null | undefined; choices: Record<string, string>; onChange: (v: string) => void; label: string
}) {
  const name = useId()
  return (
    <div role="radiogroup" aria-label={label} className="flex flex-wrap gap-x-3 gap-y-1" data-field={path}>
      {Object.entries(choices).map(([v, text]) => (
        <label key={v} className="inline-flex cursor-pointer items-center gap-1.5 text-xs">
          <input type="radio" name={name} value={v} checked={(value ?? '') === v} onChange={() => onChange(v)} />
          {text}
        </label>
      ))}
    </div>
  )
}

function Check({ path, checked, onChange, children }: {
  path: string; checked: boolean; onChange: (v: boolean) => void; children: ReactNode
}) {
  return (
    <label className="inline-flex cursor-pointer items-center gap-1.5 text-xs">
      <input type="checkbox" checked={checked} data-field={path} onChange={(e) => onChange(e.target.checked)} />
      {children}
    </label>
  )
}

function Select({ path, value, options, onChange, label, empty }: {
  path: string; value: string; options: string[]; onChange: (v: string) => void; label: string
  /** 给了就多一个空选项（值为空串） */
  empty?: string
}) {
  return (
    <select className="field" value={value} data-field={path} aria-label={label} onChange={(e) => onChange(e.target.value)}>
      {empty != null && <option value="">{empty}</option>}
      {options.map((o) => <option key={o} value={o}>{o}</option>)}
    </select>
  )
}

/** 标签集合：增删。候选是网格里标签列的原文，也可以手输 */
function LabelsEditor({ path, labels, options, onAdd, onRemove, title }: {
  path: string; labels: string[]; options: string[]; onAdd: (label: string) => void; onRemove: (label: string) => void; title: string
}) {
  const [text, setText] = useState('')
  const listId = useId()
  const add = () => {
    const v = text.trim()
    if (!v || labels.includes(v)) return
    onAdd(v)
    setText('')
  }
  return (
    <FieldWrap path={path} label={title}>
      <div className="flex flex-wrap items-center gap-1" data-labels={path}>
        {labels.map((l) => (
          <span key={l} className="chip" data-label={l}>
            {l}
            <button type="button" className="-mr-0.5 rounded text-faint hover:text-fg" aria-label={RECIPE_TEXT.removeLabel(l)}
                    onClick={() => onRemove(l)}>
              <X size={10} aria-hidden />
            </button>
          </span>
        ))}
        <span className="inline-flex items-center gap-1">
          <input className="field !h-6 !w-40 !py-0 text-2xs" list={listId} value={text} placeholder={RECIPE_TEXT.labelPlaceholder}
                 aria-label={`${title}：${RECIPE_TEXT.addLabel}`} data-label-input={path}
                 onChange={(e) => setText(e.target.value)}
                 onKeyDown={(e) => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) { e.preventDefault(); add() } }} />
          <datalist id={listId}>
            {options.filter((o) => !labels.includes(o)).map((o) => <option key={o} value={o} />)}
          </datalist>
          <button type="button" className="btn btn-xs" onClick={add} disabled={!text.trim()} data-label-add={path}>
            <Plus size={10} aria-hidden /> {RECIPE_TEXT.addLabel}
          </button>
        </span>
      </div>
    </FieldWrap>
  )
}

type Edit = (fn: (r: Recipe) => void) => void

/** 排着的一处修改：apply 在给定的底上算出改后的整份配方（不改动底）；raw 是粘贴的配方原文 */
interface RecipeOp { apply: (r: Recipe | null) => Recipe | null; raw?: Recipe; done?: () => void }

/** 数据区的值：类型、占位符、空格 / 千分位 / 公式的处理。交叉表和列表各有各的默认值 */
function ValuesEditor({ base, values, list, edit }: { base: string; values: any; list: boolean; edit: Edit }) {
  const at = (r: Recipe) => {
    const parts = base.split('/').filter(Boolean)
    let cur: any = r
    for (const p of parts) cur = cur[/^\d+$/.test(p) ? Number(p) : p]
    cur.values = isObj(cur.values) ? cur.values : {}
    cur.values.placeholders = arr(cur.values.placeholders)
    return cur.values
  }
  const [text, setText] = useState('')
  const phs: any[] = arr(values?.placeholders)
  return (
    <div className="space-y-2">
      {!list && (
        <FieldWrap path={`${base}/values/type`} label={RECIPE_TEXT.valueType}>
          <Radios path={`${base}/values/type`} value={values?.type} choices={RECIPE_CHOICE_LABEL.valueType} label={RECIPE_TEXT.valueType}
                  onChange={(v) => edit((r) => { at(r).type = v })} />
        </FieldWrap>
      )}
      <FieldWrap path={`${base}/values/placeholders`} label={RECIPE_TEXT.placeholders} hint={RECIPE_TEXT.placeholderStore}>
        <div className="space-y-1">
          {phs.map((p, k) => (
            <div key={k} className="flex items-center gap-2" data-placeholder={p?.text}>
              <span className="chip mono">{p?.text}</span>
              <select className="field !w-28" value={p?.meaning ?? '无数据'} aria-label={`${RECIPE_TEXT.placeholderMeaning}：${p?.text}`}
                      data-field={`${base}/values/placeholders/${k}/meaning`}
                      onChange={(e) => edit((r) => { at(r).placeholders[k].meaning = e.target.value })}>
                {Object.entries(RECIPE_CHOICE_LABEL.meaning).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
              <button type="button" className="btn btn-xs btn-ghost" aria-label={RECIPE_TEXT.removePlaceholder(String(p?.text ?? ''))}
                      onClick={() => edit((r) => { at(r).placeholders.splice(k, 1) })}>
                <X size={11} aria-hidden />
              </button>
              <FieldProblems path={`${base}/values/placeholders/${k}`} />
            </div>
          ))}
          <div className="flex items-center gap-1">
            <input className="field !h-6 !w-28 !py-0 text-2xs" value={text} maxLength={8} aria-label={RECIPE_TEXT.placeholderText}
                   placeholder={RECIPE_TEXT.placeholderText} onChange={(e) => setText(e.target.value)} />
            <button type="button" className="btn btn-xs" disabled={!text.trim() || phs.some((p) => p?.text === text.trim())}
                    onClick={() => { const v = text.trim(); setText(''); edit((r) => { at(r).placeholders.push({ text: v, meaning: '无数据' }) }) }}>
              <Plus size={10} aria-hidden /> {RECIPE_TEXT.addPlaceholder}
            </button>
          </div>
        </div>
      </FieldWrap>
      <div className="grid grid-cols-1 gap-2 md:grid-cols-3">
        <FieldWrap path={`${base}/values/blank`} label={RECIPE_TEXT.blank}>
          <Radios path={`${base}/values/blank`} value={values?.blank} label={RECIPE_TEXT.blank}
                  choices={list ? RECIPE_CHOICE_LABEL.blankList : RECIPE_CHOICE_LABEL.blankCross}
                  onChange={(v) => edit((r) => { at(r).blank = v })} />
        </FieldWrap>
        <FieldWrap path={`${base}/values/text_number`} label={RECIPE_TEXT.textNumber}>
          <Radios path={`${base}/values/text_number`} value={values?.text_number} label={RECIPE_TEXT.textNumber}
                  choices={RECIPE_CHOICE_LABEL.textNumber} onChange={(v) => edit((r) => { at(r).text_number = v })} />
        </FieldWrap>
        <FieldWrap path={`${base}/values/formula`} label={RECIPE_TEXT.formula}>
          <Radios path={`${base}/values/formula`} value={values?.formula} label={RECIPE_TEXT.formula}
                  choices={list ? RECIPE_CHOICE_LABEL.formulaList : RECIPE_CHOICE_LABEL.formulaCross}
                  onChange={(v) => edit((r) => { at(r).formula = v })} />
        </FieldWrap>
      </div>
    </div>
  )
}

function Group({ title, children, attr }: { title: ReactNode; children: ReactNode; attr?: Record<string, string> }) {
  return (
    <section className="space-y-2 rounded-lg border bg-bg p-2.5" {...attr}>
      <h4 className="text-xs font-medium">{title}</h4>
      {children}
    </section>
  )
}

// ---------------------------------------------------------------------------
// 交叉表的分段
// ---------------------------------------------------------------------------

/** 派生列（起始小时、结束小时）改名：键就是列名 */
function DeriveNames({ path, derive, onRename }: { path: string; derive: unknown; onRename: (old: string, nu: string) => void }) {
  const entries = Object.entries(isObj(derive) ? derive : {})
  if (!entries.length) return null
  return (
    <FieldWrap path={path} label={RECIPE_TEXT.deriveCols}>
      <div className="flex flex-wrap gap-2">
        {entries.map(([name, role]) => (
          <label key={name} className="inline-flex items-center gap-1.5 text-xs" data-field-wrap={`${path}/${ptr(name)}`}>
            <span className="text-faint">{RECIPE_TEXT.deriveRole[String(role)] ?? String(role)}</span>
            <TextInput path={`${path}/${ptr(name)}`} value={name} className="!w-28" label={`${RECIPE_TEXT.deriveCols}：${name}`}
                       onCommit={(v) => onRename(name, v)} />
            <FieldProblems path={`${path}/${ptr(name)}`} />
          </label>
        ))}
      </div>
    </FieldWrap>
  )
}

function SegmentEditor({ s, b, g, seg, edit, candidates, labelOptions, units }: {
  s: number; b: number; g: number; seg: any; edit: Edit
  candidates: Record<string, string[]>; labelOptions: string[]; units: string[]
}) {
  const base = `/sheets/${s}/blocks/${b}/segments/${g}`
  const at = (r: Recipe) => r.sheets[s].blocks[b].segments[g]
  const labels: string[] = arr(seg.labels?.expect).map(String)
  const role = seg.role
  const title = role === 'derived' ? RECIPE_TEXT.derivedSegment(seg.id) : RECIPE_TEXT.segment(seg.id)

  const addLabel = (label: string) => edit((r) => {
    const x = at(r)
    x.labels = isObj(x.labels) ? x.labels : { expect: [] }
    x.labels.expect = [...arr(x.labels.expect), label]
    if (x.role === 'measures') {
      // 指标的键必须与标签逐字相同；列名、单位按标签末尾的括号拆（与规则起草一致）
      const [name, unit] = splitUnit(label, units)
      x.measures = { ...(isObj(x.measures) ? x.measures : {}), [label]: name }
      const table = arr(r.tables).find((t) => t?.name === x.table)
      if (unit && table) table.units = { ...(isObj(table.units) ? table.units : {}), [name]: unit }
    }
  })
  const removeLabel = (label: string) => edit((r) => {
    const x = at(r)
    x.labels.expect = arr(x.labels.expect).filter((l) => l !== label)
    if (x.role === 'measures' && isObj(x.measures)) {
      const col = x.measures[label]
      delete x.measures[label]
      const table = arr(r.tables).find((t) => t?.name === x.table)
      if (table && col) {
        if (isObj(table.units)) delete table.units[col]
        table.grain = arr(table.grain).filter((c) => c !== col)
      }
    }
  })

  return (
    <Group title={title} attr={{ 'data-segment': String(seg.id) }}>
      {role !== 'derived' && (
        <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
          <FieldWrap path={`${base}/locate/by`} label={RECIPE_TEXT.locateBy}>
            <Radios path={`${base}/locate/by`} value={seg.locate?.by} label={RECIPE_TEXT.locateBy}
                    choices={RECIPE_CHOICE_LABEL.locateBy}
                    onChange={(v) => edit((r) => {
                      const x = at(r)
                      x.locate = { ...(isObj(x.locate) ? x.locate : {}), by: v }
                      if (v === 'labels') delete x.locate.title
                    })} />
          </FieldWrap>
          {seg.locate?.by === 'section_title' && (
            <FieldWrap path={`${base}/locate/title`} label={RECIPE_TEXT.sectionTitle}>
              <TextInput path={`${base}/locate/title`} value={seg.locate?.title ?? ''} label={RECIPE_TEXT.sectionTitle}
                         onCommit={(v) => edit((r) => {
                           const x = at(r)
                           x.locate = { ...(isObj(x.locate) ? x.locate : {}), title: v }
                         })} />
            </FieldWrap>
          )}
        </div>
      )}
      <LabelsEditor path={`${base}/labels/expect`} labels={labels} options={labelOptions} onAdd={addLabel} onRemove={removeLabel}
                    title={role === 'derived' ? RECIPE_TEXT.totalLabels : RECIPE_TEXT.labels} />
      {role === 'measures' && (
        <FieldWrap path={`${base}/measures`} label={RECIPE_TEXT.columnName}>
          <div className="grid grid-cols-1 gap-1 md:grid-cols-2">
            {labels.map((label) => (
              <div key={label} className="flex items-center gap-2 text-xs" data-field-wrap={`${base}/measures/${ptr(label)}`}>
                <span className="min-w-0 flex-1 truncate text-dim" title={label}>{label}</span>
                <TextInput path={`${base}/measures/${ptr(label)}`} value={String(seg.measures?.[label] ?? '')} className="!w-36"
                           label={`${RECIPE_TEXT.columnName}：${label}`}
                           onCommit={(v) => edit((r) => {
                             const x = at(r)
                             const old = x.measures?.[label]
                             if (old) renameColumn(r, x.table, old, v)
                             else x.measures = { ...(isObj(x.measures) ? x.measures : {}), [label]: v }
                           })} />
                <FieldProblems path={`${base}/measures/${ptr(label)}`} />
              </div>
            ))}
          </div>
        </FieldWrap>
      )}
      {role === 'dimension' && (
        <>
          <div className="grid grid-cols-1 gap-2 md:grid-cols-3">
            <FieldWrap path={`${base}/dim/name`} label={RECIPE_TEXT.dimName}
                       hint={`${RECIPE_TEXT.dimParser}：${RECIPE_CHOICE_LABEL.dimParser[seg.dim?.parser] ?? ''}`}>
              <TextInput path={`${base}/dim/name`} value={seg.dim?.name ?? ''} label={RECIPE_TEXT.dimName}
                         onCommit={(v) => edit((r) => { renameColumn(r, at(r).table, at(r).dim?.name ?? '', v) })} />
            </FieldWrap>
            <FieldWrap path={`${base}/value`} label={RECIPE_TEXT.valueName}>
              <TextInput path={`${base}/value`} value={seg.value ?? ''} label={RECIPE_TEXT.valueName}
                         onCommit={(v) => edit((r) => { renameColumn(r, at(r).table, at(r).value ?? '', v) })} />
            </FieldWrap>
            <FieldWrap path={`${base}/stop_parser`} label={RECIPE_TEXT.totalRow}>
              <Check path={`${base}/stop_parser`} checked={seg.stop_parser === 'hour_range_total'}
                     onChange={(on) => edit((r) => { at(r).stop_parser = on ? 'hour_range_total' : null })}>
                {RECIPE_TEXT.stopAtTotal}
              </Check>
            </FieldWrap>
          </div>
          <DeriveNames path={`${base}/dim/derive`} derive={seg.dim?.derive}
                       onRename={(old, nu) => edit((r) => { renameColumn(r, at(r).table, old, nu) })} />
          {Object.keys(isObj(seg.const) ? seg.const : {}).length > 0 && (
            <FieldWrap path={`${base}/const`} label={RECIPE_TEXT.constants}>
              <div className="space-y-1">
                {Object.entries(seg.const as Record<string, any>).map(([name, c]) => {
                  const opts = candidates[seg.locate?.title ?? ''] ?? []
                  const pick = String(c?.pick ?? '')
                  return (
                    <div key={name} className="flex flex-wrap items-center gap-2 text-xs" data-const={name}>
                      <TextInput path={`${base}/const/${ptr(name)}`} value={name} className="!w-32" label={`${RECIPE_TEXT.constants}：${name}`}
                                 onCommit={(v) => edit((r) => { renameColumn(r, at(r).table, name, v) })} />
                      <span className="text-faint">{RECIPE_TEXT.constPick}</span>
                      <Select path={`${base}/const/${ptr(name)}/pick`} value={pick} label={`${name}：${RECIPE_TEXT.constPick}`}
                              options={opts.includes(pick) || !pick ? opts : [pick, ...opts]}
                              onChange={(v) => edit((r) => { at(r).const[name] = { ...(isObj(at(r).const[name]) ? at(r).const[name] : {}), pick: v } })} />
                      <FieldProblems path={`${base}/const/${ptr(name)}`} />
                      <FieldProblems path={`${base}/const/${ptr(name)}/pick`} />
                    </div>
                  )
                })}
              </div>
            </FieldWrap>
          )}
        </>
      )}
      {role === 'derived' && (
        <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
          <FieldWrap path={`${base}/keep_as`} label={RECIPE_TEXT.keepAs}>
            <Check path={`${base}/keep_as`} checked={isObj(seg.keep_as)}
                   onChange={(on) => edit((r) => {
                     const x = at(r)
                     if (!on) {
                       const name = x.keep_as?.table
                       x.keep_as = null
                       // 另存的表没人写了，就从表清单里拿掉（否则静态检查会说它没有来处）
                       if (name) r.tables = arr(r.tables).filter((t) => t?.name !== name)
                       return
                     }
                     const baseSeg = arr(r.sheets[s].blocks[b].segments).find((y) => y?.id === x.locate?.segment)
                     const baseTable = x.verify?.against_table ?? baseSeg?.table ?? '表'
                     const table = `${baseTable}_表内合计`
                     const axisName = r.sheets[s].blocks[b].axis?.name ?? '日期'
                     x.keep_as = { table, dim: '合计项', derive: clone(baseSeg?.dim?.derive ?? {}), value: x.verify?.value ?? '数值' }
                     if (!arr(r.tables).some((t) => t?.name === table)) {
                       const unit = arr(r.tables).find((t) => t?.name === baseTable)?.units?.[x.verify?.value]
                       r.tables = [...arr(r.tables), {
                         name: table, grain: [axisName, '合计项'], kind: 'reported_total', units: unit ? { [x.keep_as.value]: unit } : {}, note: '',
                       }]
                     }
                   })}>
              {RECIPE_TEXT.keepAs}
            </Check>
          </FieldWrap>
          {isObj(seg.keep_as) && (
            <FieldWrap path={`${base}/keep_as/table`} label={RECIPE_TEXT.keepTable}>
              <TextInput path={`${base}/keep_as/table`} value={seg.keep_as.table ?? ''} label={RECIPE_TEXT.keepTable}
                         onCommit={(v) => edit((r) => { renameTable(r, at(r).keep_as.table, v) })} />
            </FieldWrap>
          )}
          {isObj(seg.keep_as) && (
            <>
              <FieldWrap path={`${base}/keep_as/dim`} label={RECIPE_TEXT.keepDim}>
                <TextInput path={`${base}/keep_as/dim`} value={seg.keep_as.dim ?? ''} label={RECIPE_TEXT.keepDim}
                           onCommit={(v) => edit((r) => { renameColumn(r, at(r).keep_as.table, at(r).keep_as.dim, v) })} />
              </FieldWrap>
              <FieldWrap path={`${base}/keep_as/value`} label={RECIPE_TEXT.valueName}>
                <TextInput path={`${base}/keep_as/value`} value={seg.keep_as.value ?? ''} label={RECIPE_TEXT.valueName}
                           onCommit={(v) => edit((r) => { renameColumn(r, at(r).keep_as.table, at(r).keep_as.value, v) })} />
              </FieldWrap>
              <DeriveNames path={`${base}/keep_as/derive`} derive={seg.keep_as.derive}
                           onRename={(old, nu) => edit((r) => { renameColumn(r, at(r).keep_as.table, old, nu) })} />
            </>
          )}
        </div>
      )}
    </Group>
  )
}

// ---------------------------------------------------------------------------
// 列表块
// ---------------------------------------------------------------------------

function ListEditor({ s, b, block, edit }: { s: number; b: number; block: any; edit: Edit }) {
  const base = `/sheets/${s}/blocks/${b}`
  const at = (r: Recipe) => r.sheets[s].blocks[b]
  const columns: any[] = arr(block.columns)
  const total = isObj(block.rows?.total_row) ? block.rows.total_row : null
  return (
    <Group title={RECIPE_TEXT.listBlock(String(block.id))} attr={{ 'data-list-block': String(block.id) }}>
      <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
        <FieldWrap path={`${base}/header_rows`} label={RECIPE_TEXT.headerRows}>
          <input type="number" className="field !w-20" min={1} max={3} value={block.header_rows ?? 1} data-field={`${base}/header_rows`}
                 aria-label={RECIPE_TEXT.headerRows}
                 onChange={(e) => {
                   const n = Math.max(1, Math.min(3, Number(e.target.value) || 1))
                   if (n !== block.header_rows) edit((r) => { at(r).header_rows = n })
                 }} />
        </FieldWrap>
        <FieldWrap path={`${base}/after_title`} label={RECIPE_TEXT.afterTitle}>
          <TextInput path={`${base}/after_title`} value={block.after_title ?? ''} allowEmpty label={RECIPE_TEXT.afterTitle}
                     onCommit={(v) => edit((r) => { at(r).after_title = v || null })} />
        </FieldWrap>
      </div>
      <FieldWrap path={`${base}/columns`} label={RECIPE_TEXT.columns}>
        <table className="w-full text-xs">
          <thead>
            <tr className="text-left text-2xs text-faint">
              <th className="py-1 font-normal">{RECIPE_TEXT.colHeader}</th>
              <th className="py-1 font-normal">{RECIPE_TEXT.columnName}</th>
              <th className="py-1 font-normal">{RECIPE_TEXT.colType}</th>
              <th className="py-1 font-normal">{RECIPE_TEXT.colStore}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {columns.map((c, k) => (
              <tr key={k} className="align-top" data-list-column={c?.name}>
                <td className="py-0.5 pr-1">
                  <TextInput path={`${base}/columns/${k}/header`} value={c?.header ?? ''} label={`${RECIPE_TEXT.colHeader}：${c?.name}`}
                             onCommit={(v) => edit((r) => { at(r).columns[k].header = v })} />
                  <FieldProblems path={`${base}/columns/${k}/header`} />
                </td>
                <td className="py-0.5 pr-1">
                  <TextInput path={`${base}/columns/${k}/name`} value={c?.name ?? ''} label={`${RECIPE_TEXT.columnName}：${c?.header}`}
                             onCommit={(v) => edit((r) => { renameColumn(r, at(r).table, at(r).columns[k].name, v) })} />
                  <FieldProblems path={`${base}/columns/${k}/name`} />
                </td>
                <td className="py-0.5 pr-1">
                  <select className="field" value={c?.type ?? 'TEXT'} data-field={`${base}/columns/${k}/type`}
                          aria-label={`${RECIPE_TEXT.colType}：${c?.name}`}
                          onChange={(e) => edit((r) => {
                            const col = at(r).columns[k]
                            col.type = e.target.value
                            if (col.type !== 'TEXT') col.store = null
                          })}>
                    {Object.entries(RECIPE_CHOICE_LABEL.colType).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                  </select>
                </td>
                <td className="py-0.5 pr-1">
                  {c?.type === 'TEXT' && (
                    <select className="field" value={c?.store ?? ''} data-field={`${base}/columns/${k}/store`}
                            aria-label={`${RECIPE_TEXT.colStore}：${c?.name}`}
                            onChange={(e) => edit((r) => { at(r).columns[k].store = e.target.value || null })}>
                      {Object.entries(RECIPE_CHOICE_LABEL.colStore).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                    </select>
                  )}
                </td>
                <td className="py-0.5">
                  <button type="button" className="btn btn-xs btn-ghost" aria-label={RECIPE_TEXT.removeColumn(String(c?.name ?? ''))}
                          disabled={columns.length <= 1}
                          onClick={() => edit((r) => { at(r).columns.splice(k, 1) })}>
                    <X size={11} aria-hidden />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <button type="button" className="btn btn-xs mt-1"
                onClick={() => edit((r) => {
                  const n = arr(at(r).columns).length + 1
                  at(r).columns = [...arr(at(r).columns), { header: `列${n}`, name: `列${n}`, type: 'TEXT', store: null }]
                })}>
          <Plus size={10} aria-hidden /> {RECIPE_TEXT.addColumn}
        </button>
      </FieldWrap>
      <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
        <FieldWrap path={`${base}/extra_columns`} label={RECIPE_TEXT.extraColumns}>
          <Radios path={`${base}/extra_columns`} value={block.extra_columns} label={RECIPE_TEXT.extraColumns}
                  choices={RECIPE_CHOICE_LABEL.extraColumns} onChange={(v) => edit((r) => { at(r).extra_columns = v })} />
        </FieldWrap>
        <FieldWrap path={`${base}/rows/blank_rows`} label={RECIPE_TEXT.blankRows}>
          <Radios path={`${base}/rows/blank_rows`} value={block.rows?.blank_rows} label={RECIPE_TEXT.blankRows}
                  choices={RECIPE_CHOICE_LABEL.blankRows}
                  onChange={(v) => edit((r) => { at(r).rows = { ...(isObj(at(r).rows) ? at(r).rows : {}), blank_rows: v } })} />
        </FieldWrap>
      </div>
      <FieldWrap path={`${base}/rows/total_row`} label={RECIPE_TEXT.totalRow}>
        <Check path={`${base}/rows/total_row`} checked={!!total}
               onChange={(on) => edit((r) => {
                 const x = at(r)
                 x.rows = isObj(x.rows) ? x.rows : {}
                 x.rows.total_row = on ? { label_column: arr(x.columns)[0]?.name ?? '', pick: '合计', keep_as: null } : null
               })}>
          {RECIPE_TEXT.totalRowOn}
        </Check>
        {total && (
          <div className="mt-1 grid grid-cols-1 gap-2 md:grid-cols-3">
            <FieldWrap path={`${base}/rows/total_row/label_column`} label={RECIPE_TEXT.totalLabelColumn}>
              <Select path={`${base}/rows/total_row/label_column`} value={total.label_column ?? ''} label={RECIPE_TEXT.totalLabelColumn}
                      options={columns.map((c) => String(c?.name ?? ''))}
                      onChange={(v) => edit((r) => { at(r).rows.total_row.label_column = v })} />
            </FieldWrap>
            <FieldWrap path={`${base}/rows/total_row/pick`} label={RECIPE_TEXT.totalPick}>
              <TextInput path={`${base}/rows/total_row/pick`} value={total.pick ?? ''} label={RECIPE_TEXT.totalPick}
                         onCommit={(v) => edit((r) => { at(r).rows.total_row.pick = v })} />
            </FieldWrap>
            <FieldWrap path={`${base}/rows/total_row/keep_as`} label={RECIPE_TEXT.totalKeep}>
              <TextInput path={`${base}/rows/total_row/keep_as`} value={total.keep_as ?? ''} allowEmpty label={RECIPE_TEXT.totalKeep}
                         onCommit={(v) => edit((r) => {
                           const old = at(r).rows.total_row.keep_as
                           if (old && v) renameTable(r, old, v)
                           else at(r).rows.total_row.keep_as = v || null
                         })} />
            </FieldWrap>
          </div>
        )}
      </FieldWrap>
      <ValuesEditor base={base} values={block.values} list edit={edit} />
    </Group>
  )
}

// ---------------------------------------------------------------------------
// 关系
// ---------------------------------------------------------------------------

function RelationEditor({ k, rel, columns, edit, cached }: {
  k: number; rel: any; columns: Map<string, RecipeColumn[]>; edit: Edit; cached: Map<string, any>
}) {
  const base = `/relations/${k}`
  const dismissed = rel.kind === 'dismissed'
  const [asking, setAsking] = useState(false)
  const [reason, setReason] = useState('')
  const original = dismissed ? cached.get(rel.id) : rel
  const numeric = (table: string) => (columns.get(table) ?? []).filter((c) => c.type === 'INTEGER' || c.type === 'REAL').map((c) => c.name)
  const text = dismissed
    ? RECIPE_TEXT.dismissed(String(rel.claims ?? ''))
    : rel.kind === 'sum_eq'
      ? RECIPE_TEXT.sumEq(String(rel.total ?? ''), arr(rel.parts).join(' + '))
      : RECIPE_TEXT.notComparable(`${rel.a?.table ?? ''}.${rel.a?.value ?? ''}`, `${rel.b?.table ?? ''}.${rel.b?.value ?? ''}`)
  const choose = (v: string) => {
    if (v === 'dismiss' && !dismissed) { setAsking(true); return }
    setAsking(false)
    if (v === 'register' && dismissed && original) edit((r) => { r.relations[k] = clone(original) })
  }
  return (
    <div className="space-y-1.5 rounded-md border px-2.5 py-2" data-relation={rel.id}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
        <span className="mono text-faint">{rel.id}</span>
        <span className="min-w-0 flex-1">{text}</span>
        <div role="radiogroup" aria-label={`${rel.id}：${RECIPE_TEXT.relations}`} className="flex gap-3" data-field={base}>
          <label className="inline-flex items-center gap-1.5">
            <input type="radio" name={`rel-${k}`} checked={!dismissed && !asking} disabled={dismissed && !original}
                   onChange={() => choose('register')} />
            {RECIPE_TEXT.relationRegister}
          </label>
          <label className="inline-flex items-center gap-1.5">
            <input type="radio" name={`rel-${k}`} checked={dismissed || asking} disabled={!rel.claims}
                   onChange={() => choose('dismiss')} />
            {RECIPE_TEXT.relationDismiss}
          </label>
        </div>
      </div>
      {dismissed && !original && <p className="text-2xs text-faint">{RECIPE_TEXT.relationNoCache}</p>}
      {asking && (
        <div className="flex items-start gap-2">
          <textarea className="field text-xs" rows={2} maxLength={200} value={reason} placeholder={RECIPE_TEXT.reasonPlaceholder}
                    aria-label={RECIPE_TEXT.relationReason} onChange={(e) => setReason(e.target.value)} />
          <button type="button" className="btn btn-sm shrink-0" disabled={!reason.trim()}
                  onClick={() => {
                    const v = reason.trim()
                    setAsking(false)
                    setReason('')
                    edit((r) => { r.relations[k] = { id: rel.id, kind: 'dismissed', claims: rel.claims, reason: v } })
                  }}>
            {RECIPE_TEXT.reasonSubmit}
          </button>
        </div>
      )}
      {dismissed && (
        <FieldWrap path={`${base}/reason`} label={RECIPE_TEXT.relationReason}>
          <TextInput path={`${base}/reason`} value={rel.reason ?? ''} label={RECIPE_TEXT.relationReason}
                     onCommit={(v) => edit((r) => { r.relations[k].reason = v })} />
        </FieldWrap>
      )}
      {rel.kind === 'sum_eq' && (
        <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
          <FieldWrap path={`${base}/total`} label={RECIPE_TEXT.relationTotal}>
            <Select path={`${base}/total`} value={rel.total ?? ''} options={numeric(rel.table)} label={RECIPE_TEXT.relationTotal}
                    onChange={(v) => edit((r) => { r.relations[k].total = v })} />
          </FieldWrap>
          <FieldWrap path={`${base}/parts`} label={RECIPE_TEXT.relationParts}>
            <div className="flex flex-wrap gap-x-3 gap-y-1" data-field={`${base}/parts`}>
              {numeric(rel.table).filter((c) => c !== rel.total).map((c) => (
                <label key={c} className="inline-flex items-center gap-1.5 text-xs">
                  <input type="checkbox" checked={arr(rel.parts).includes(c)}
                         onChange={(e) => edit((r) => {
                           const parts = arr(r.relations[k].parts)
                           r.relations[k].parts = e.target.checked ? [...parts, c] : parts.filter((p) => p !== c)
                         })} />
                  {c}
                </label>
              ))}
            </div>
          </FieldWrap>
        </div>
      )}
      <FieldProblems path={base} />
    </div>
  )
}

// ===========================================================================
// 面板
// ===========================================================================

/**
 * 配方面板：期 2 可编辑的字段（P2-SPEC 8.1 的清单）做成表单，其余字段只读，另给「查看配方 JSON」
 * 「粘贴配方」作兜底。每次修改都把整份配方交给 onSave（PUT recipe）；静态检查的问题按 path 挂在
 * 对应字段旁边，挂不上的列在面板顶部。
 *
 * 连续修改：队列里放的是「怎么改」（op），不是算好的整份配方。表单画的是「服务端最近一次存下的配方 +
 * 还没存上的修改依次应用」；轮到发送时，以服务端最近一次返回的配方为底，把排着的修改一并算出整份再发。
 * 这样上一次 PUT 回来时，排在后面的修改不会被服务端那份盖掉，之后的修改也不会以缺了它们的配方为底
 */
export function RecipePanel({ recipe, problems, units, candidates, labelOptions, saving, onSave }: {
  recipe: Recipe | null
  problems: RecipeProblem[]
  units: string[]
  candidates: Record<string, string[]>
  /** 标签候选：网格里标签列的原文 */
  labelOptions: string[]
  saving: boolean
  /** 存上了返回服务端存下的配方，没存上返回 null */
  onSave: (recipe: Recipe) => Promise<{ recipe: Recipe | null } | null>
}) {
  const saved = useMemo(() => fillRecipeDefaults(recipe), [recipe])
  // 服务端最近一次存下的配方（补全默认值后）：发送时的底、没存上时退回的那一份
  const server = useRef<Recipe | null>(saved)
  // 还没存上的修改（含正在发送的那一批），按先后排
  const pending = useRef<RecipeOp[]>([])
  const draining = useRef(false)
  const [unsaved, setUnsaved] = useState(0)
  const project = () => pending.current.reduce<Recipe | null>((r, op) => op.apply(r), server.current)
  // 表单按「服务端那份 + 没存上的修改」画：等服务端回来才变，点了单选、开关像没反应
  const [full, setFull] = useState<Recipe | null>(saved)
  // recipe 从外面变了（回答问题、AI 草稿）：以它为底重画。正在发送时 recipe 变是我们自己的 PUT 回来了，
  // 由发送循环接手（它拿着那次的返回值），这里不动，免得把正在发的那批修改在新底上再应用一遍
  useEffect(() => {
    if (draining.current) return
    server.current = saved
    setFull(project())
  }, [saved])
  const [showJson, setShowJson] = useState(false)
  const [pasting, setPasting] = useState(false)
  const [pasteText, setPasteText] = useState('')
  const [pasteError, setPasteError] = useState('')
  const cached = useRef(new Map<string, any>())
  for (const rel of arr(full?.relations)) if (isObj(rel) && rel.kind !== 'dismissed' && rel.id) cached.current.set(rel.id, clone(rel))

  // 字段登记：每次渲染重新收集，画完之后按最长前缀把问题分给字段
  const registry = useRef(new Set<string>())
  registry.current = new Set()
  const [assigned, setAssigned] = useState<{ map: Map<string, RecipeProblem[]>; top: RecipeProblem[] }>({ map: new Map(), top: [] })
  const sig = useRef('')
  useLayoutEffect(() => {
    const paths = [...registry.current]
    const map = new Map<string, RecipeProblem[]>()
    const top: RecipeProblem[] = []
    for (const p of problems) {
      let best = ''
      for (const f of paths) {
        if ((p.path === f || p.path.startsWith(`${f}/`)) && f.length > best.length) best = f
      }
      if (best) map.set(best, [...(map.get(best) ?? []), p])
      else top.push(p)
    }
    const next = JSON.stringify([[...map.entries()], top])
    if (next !== sig.current) {
      sig.current = next
      setAssigned({ map, top })
    }
  })
  const api = useMemo<ProblemsApi>(() => ({
    register: (path) => { registry.current.add(path) },
    assigned: assigned.map,
  }), [assigned])

  /** 一个接一个地发：每次以服务端最近一次返回的配方为底，把此刻排着的修改一并算出整份配方 */
  const drain = async () => {
    if (draining.current) return
    draining.current = true
    try {
      while (pending.current.length) {
        const batch = pending.current.slice()
        // 粘贴的配方排在最后：原样发它（服务端存去掉默认值的形式，「粘贴什么就存什么」更好对照）
        const body = batch[batch.length - 1].raw ?? batch.reduce<Recipe | null>((r, op) => op.apply(r), server.current)
        const res = body ? await onSave(body) : null
        if (!res) {
          // 没存上（报错已由 onSave 提示）：排着的修改都是在这一批之上做的，一并丢掉，退回此刻服务端那一份
          pending.current = []
          setFull(server.current)
          break
        }
        server.current = fillRecipeDefaults(res.recipe)
        pending.current.splice(0, batch.length)
        for (const op of batch) op.done?.()
        setFull(project())
      }
    } finally {
      draining.current = false
      setUnsaved(pending.current.length)
    }
  }
  const enqueue = (op: RecipeOp) => {
    pending.current.push(op)
    setUnsaved(pending.current.length)
    setFull(project())
    void drain()
  }

  const edit: Edit = (fn) => {
    if (!project()) return
    enqueue({
      apply: (r) => {
        if (!r) return r
        const next = clone(r)
        try {
          fn(next)
        } catch {
          // 底变了、这处修改指向的位置已不存在（比如那张表被粘贴的配方换掉了）：这一处不改
          return r
        }
        return next
      },
    })
  }

  const columns = useMemo(() => tableColumns(full), [full])
  const savePaste = () => {
    let parsed: unknown
    try {
      parsed = JSON.parse(pasteText)
    } catch {
      setPasteError(RECIPE_TEXT.pasteInvalid)
      return
    }
    if (!isObj(parsed)) { setPasteError(RECIPE_TEXT.pasteNotObject); return }
    setPasteError('')
    const pasted = parsed as Recipe
    // 表单按补全默认值后的样子画，之后的修改也在它上面做
    enqueue({ apply: () => fillRecipeDefaults(pasted), raw: clone(pasted), done: () => { setPasting(false); setPasteText('') } })
  }

  return (
    <ProblemsContext.Provider value={api}>
      <div className="space-y-3" data-recipe-panel>
        <div className="flex flex-wrap items-center gap-2 text-2xs text-faint">
          <span className="min-w-0 flex-1">{RECIPE_TEXT.panelHint}</span>
          {(saving || unsaved > 0) && <span className="inline-flex items-center gap-1" data-recipe-saving><Spinner size={10} /> {RECIPE_TEXT.saving}</span>}
        </div>
        {assigned.top.length > 0 && (
          <div role="alert" className="rounded-lg border px-2.5 py-2 text-xs" data-recipe-problems-top
               style={{ borderColor: 'color-mix(in srgb, var(--err) 35%, var(--border))', background: 'color-mix(in srgb, var(--err) 6%, transparent)' }}>
            <div className="mb-1 font-medium text-[var(--err)]">{RECIPE_TEXT.panelProblems}</div>
            <ul className="space-y-0.5">
              {assigned.top.map((p, i) => (
                <li key={i} className="leading-relaxed text-dim" data-recipe-problem={p.code} data-path={p.path}>{p.message}</li>
              ))}
            </ul>
          </div>
        )}
        {!full ? (
          <p className="text-xs text-faint">{RECIPE_TEXT.noRecipe}</p>
        ) : (
          <>
            {arr(full.tables).map((t, i) => {
              const cols = columns.get(t?.name) ?? []
              const base = `/tables/${i}`
              return (
                <Group key={i} title={RECIPE_TEXT.table(String(t?.name ?? ''))} attr={{ 'data-recipe-table': String(t?.name ?? '') }}>
                  <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
                    <FieldWrap path={`${base}/name`} label={RECIPE_TEXT.tableName}>
                      <TextInput path={`${base}/name`} value={t?.name ?? ''} label={RECIPE_TEXT.tableName}
                                 onCommit={(v) => edit((r) => { renameTable(r, t.name, v) })} />
                    </FieldWrap>
                    <FieldWrap path={`${base}/grain`} label={RECIPE_TEXT.grain} hint={RECIPE_TEXT.grainHint}>
                      <div className="flex flex-wrap gap-x-3 gap-y-1" data-field={`${base}/grain`}>
                        {cols.map((c) => (
                          <label key={c.name} className="inline-flex items-center gap-1.5 text-xs">
                            <input type="checkbox" checked={arr(t?.grain).includes(c.name)}
                                   onChange={(e) => edit((r) => {
                                     const g = arr(r.tables[i].grain)
                                     r.tables[i].grain = e.target.checked ? [...g, c.name] : g.filter((x) => x !== c.name)
                                   })} />
                            {c.name}
                          </label>
                        ))}
                      </div>
                    </FieldWrap>
                  </div>
                  <FieldWrap path={`${base}/units`} label={RECIPE_TEXT.unit}>
                    <div className="grid grid-cols-1 gap-1 md:grid-cols-2">
                      {cols.filter((c) => c.role !== 'axis').map((c) => (
                        <div key={c.name} className="flex items-center gap-2 text-xs" data-field-wrap={`${base}/units/${ptr(c.name)}`}>
                          <span className="min-w-0 flex-1 truncate" title={c.header ?? c.name}>{c.name}</span>
                          <span className="w-32">
                            <Select path={`${base}/units/${ptr(c.name)}`} value={String(t?.units?.[c.name] ?? '')} options={units}
                                    empty={RECIPE_TEXT.noUnit} label={`${RECIPE_TEXT.unit}：${c.name}`}
                                    onChange={(v) => edit((r) => {
                                      const u = { ...(isObj(r.tables[i].units) ? r.tables[i].units : {}) }
                                      if (v) u[c.name] = v
                                      else delete u[c.name]
                                      r.tables[i].units = u
                                    })} />
                          </span>
                          <FieldProblems path={`${base}/units/${ptr(c.name)}`} />
                        </div>
                      ))}
                    </div>
                  </FieldWrap>
                </Group>
              )
            })}
            {arr(full.sheets).map((sheet, s) => {
              const sbase = `/sheets/${s}`
              const ctx = arr(sheet?.context)[0]
              return (
                <div key={s} className="space-y-2" data-recipe-sheet={sheet?.match?.name}>
                  <h3 className="text-xs font-semibold">{RECIPE_TEXT.sheet(String(sheet?.match?.name ?? ''))}</h3>
                  <Group title={RECIPE_TEXT.period}>
                    {isObj(ctx) ? (
                      <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
                        <FieldWrap path={`${sbase}/context/0/prefer_prefix`} label={RECIPE_TEXT.preferPrefix}>
                          <TextInput path={`${sbase}/context/0/prefer_prefix`} value={ctx.prefer_prefix ?? ''} allowEmpty
                                     label={RECIPE_TEXT.preferPrefix}
                                     onCommit={(v) => edit((r) => { r.sheets[s].context[0].prefer_prefix = v || null })} />
                        </FieldWrap>
                        <FieldWrap path={`${sbase}/context/0/cross_check`} label={RECIPE_TEXT.crossCheck}>
                          <Check path={`${sbase}/context/0/cross_check`} checked={ctx.cross_check !== 'none'}
                                 onChange={(on) => edit((r) => { r.sheets[s].context[0].cross_check = on ? 'filename' : 'none' })}>
                            {RECIPE_TEXT.crossCheck}
                          </Check>
                        </FieldWrap>
                      </div>
                    ) : (
                      <div className="flex items-center gap-2 text-xs text-faint">
                        {RECIPE_TEXT.noPeriod}
                        <button type="button" className="btn btn-xs" onClick={() => edit((r) => { r.sheets[s].context = [{}] })}>
                          {RECIPE_TEXT.addPeriod}
                        </button>
                      </div>
                    )}
                  </Group>
                  {arr(sheet?.blocks).map((block, b) => {
                    const bbase = `${sbase}/blocks/${b}`
                    if (block?.layout === 'list') return <ListEditor key={b} s={s} b={b} block={block} edit={edit} />
                    if (block?.layout !== 'crosstab') return null
                    const checks: string[] = arr(block.axis?.checks)
                    return (
                      <div key={b} className="space-y-2" data-crosstab={block.id}>
                        <Group title={RECIPE_TEXT.crosstab}>
                          <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
                            <FieldWrap path={`${bbase}/axis/name`} label={RECIPE_TEXT.columnName}>
                              <TextInput path={`${bbase}/axis/name`} value={block.axis?.name ?? ''} label={RECIPE_TEXT.columnName}
                                         onCommit={(v) => edit((r) => { renameAxis(r, s, b, v) })} />
                            </FieldWrap>
                            <FieldWrap path={`${bbase}/axis/checks`} label={RECIPE_TEXT.axisChecks} hint={RECIPE_TEXT.axisChecksHint}>
                              <div className="flex flex-wrap gap-x-3 gap-y-1">
                                {Object.entries(RECIPE_CHOICE_LABEL.axisChecks).map(([v, l]) => (
                                  <Check key={v} path={`${bbase}/axis/checks/${v}`} checked={checks.includes(v)}
                                         onChange={(on) => edit((r) => {
                                           const ax = r.sheets[s].blocks[b].axis
                                           const cur = arr(ax.checks)
                                           ax.checks = ['contiguous', 'covers_context'].filter((x) => (x === v ? on : cur.includes(x)))
                                         })}>
                                    {l}
                                  </Check>
                                ))}
                              </div>
                            </FieldWrap>
                          </div>
                          <ValuesEditor base={bbase} values={block.values} list={false} edit={edit} />
                        </Group>
                        {arr(block.segments).map((seg, g) => (isObj(seg)
                          ? <SegmentEditor key={g} s={s} b={b} g={g} seg={seg} edit={edit} candidates={candidates}
                                           labelOptions={labelOptions} units={units} />
                          : null))}
                      </div>
                    )
                  })}
                  <Group title={RECIPE_TEXT.sheet(String(sheet?.match?.name ?? ''))}>
                    <div className="grid grid-cols-1 gap-2 md:grid-cols-3">
                      <FieldWrap path={`${sbase}/hidden/rows`} label={RECIPE_TEXT.hiddenRows}>
                        <Radios path={`${sbase}/hidden/rows`} value={sheet?.hidden?.rows} choices={RECIPE_CHOICE_LABEL.hiddenRows}
                                label={RECIPE_TEXT.hiddenRows} onChange={(v) => edit((r) => { r.sheets[s].hidden.rows = v })} />
                      </FieldWrap>
                      <FieldWrap path={`${sbase}/hidden/cols`} label={RECIPE_TEXT.hiddenCols}>
                        <Radios path={`${sbase}/hidden/cols`} value={sheet?.hidden?.cols} choices={RECIPE_CHOICE_LABEL.hiddenCols}
                                label={RECIPE_TEXT.hiddenCols} onChange={(v) => edit((r) => { r.sheets[s].hidden.cols = v })} />
                      </FieldWrap>
                      <FieldWrap path={`${sbase}/match/fallback`} label={RECIPE_TEXT.fallback}>
                        <Radios path={`${sbase}/match/fallback`} value={sheet?.match?.fallback} choices={RECIPE_CHOICE_LABEL.fallback}
                                label={RECIPE_TEXT.fallback} onChange={(v) => edit((r) => { r.sheets[s].match.fallback = v })} />
                      </FieldWrap>
                    </div>
                  </Group>
                </div>
              )
            })}
            <Group title={RECIPE_TEXT.otherSheets}>
              <FieldWrap path="/other_visible_sheets">
                <Radios path="/other_visible_sheets" value={full.other_visible_sheets} choices={RECIPE_CHOICE_LABEL.otherSheets}
                        label={RECIPE_TEXT.otherSheets} onChange={(v) => edit((r) => { r.other_visible_sheets = v })} />
              </FieldWrap>
            </Group>
            {arr(full.relations).length > 0 && (
              <Group title={RECIPE_TEXT.relations}>
                <div className="space-y-1.5">
                  {arr(full.relations).map((rel, k) => (isObj(rel)
                    ? <RelationEditor key={`${k}-${rel.id}-${rel.kind}`} k={k} rel={rel} columns={columns} edit={edit} cached={cached.current} />
                    : null))}
                </div>
              </Group>
            )}
            <p className="text-2xs text-faint">{RECIPE_TEXT.readonlyNote}</p>
          </>
        )}
        <div className="flex flex-wrap gap-2">
          {full && (
            <button type="button" className="btn btn-sm" aria-expanded={showJson} onClick={() => setShowJson((v) => !v)} data-recipe-json-toggle>
              {showJson ? RECIPE_TEXT.hideJson : RECIPE_TEXT.viewJson}
            </button>
          )}
          <button type="button" className="btn btn-sm" aria-expanded={pasting} data-recipe-paste-toggle
                  onClick={() => { setPasting((v) => !v); setPasteError('') }}>
            {RECIPE_TEXT.pasteJson}
          </button>
        </div>
        {showJson && full && (
          <pre className="mono max-h-72 overflow-auto rounded-lg border bg-bg p-2 text-2xs leading-relaxed" data-recipe-json>
            {JSON.stringify(full, null, 2)}
          </pre>
        )}
        {pasting && (
          <div className="space-y-1.5" data-recipe-paste>
            <p className="text-2xs text-faint">{RECIPE_TEXT.pasteLead}</p>
            <textarea className="field mono text-2xs" rows={8} value={pasteText} aria-label={RECIPE_TEXT.pasteJson}
                      onChange={(e) => { setPasteText(e.target.value); setPasteError('') }} />
            {pasteError && <div className="text-2xs text-[var(--err)]" role="alert">{pasteError}</div>}
            <button type="button" className="btn btn-sm btn-primary" disabled={!pasteText.trim() || saving} onClick={savePaste}>
              {RECIPE_TEXT.pasteSave}
            </button>
          </div>
        )}
      </div>
    </ProblemsContext.Provider>
  )
}
