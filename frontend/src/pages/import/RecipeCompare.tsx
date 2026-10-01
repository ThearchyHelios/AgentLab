import type { ReactNode } from 'react'
import type { RecipeComparison } from '../../types'
import { ACCUMULATE_TEXT, COMPARE_TEXT } from '../../lib/terms'

// ===========================================================================
// 新旧配方对照（P3-SPEC 6.2、9.4）：试运行回执、确认清单顶部、修复预览、按规则重新起草共用一份。
// 服务端给的是结构化的对照（compare_recipes），这里只把它摊平成一条条人话：破坏性的排在最前，单位变化最前
// （数量级变了，引用这一列的报告口径会变），其余按表、分段、关系、工作表、模式的顺序
// ===========================================================================

/** 对照里的一条变化。kind 给检查脚本和样式用（unit、column_added、segment_title…），不上界面 */
export interface CompareItem { kind: string; text: string; breaking: boolean }

/** 这些列级变化会让引用这一列的查询和报告静默改口径（table_changes 的 kind），算破坏性 */
const BREAKING_CHANGES = new Set(['type', 'unit', 'source', 'store', 'const_value', 'placeholder_meaning'])

const join = (xs: (string | null | undefined)[] | null | undefined) => (xs ?? []).filter(Boolean).join('、')
const quoted = (xs: string[]) => xs.map((x) => `「${x}」`).join('、')

/** 结构化对照 → 排好序的变化列表 */
export function compareItems(cmp: RecipeComparison | null | undefined): CompareItem[] {
  if (!cmp) return []
  const out: CompareItem[] = []
  const unitDone = new Set<string>()
  for (const u of cmp.units_changed ?? []) {
    unitDone.add(`${u.table}\u0000${u.column}`)
    out.push({ kind: 'unit', breaking: true,
      text: COMPARE_TEXT.unit(u.table, u.column, u.old || COMPARE_TEXT.noUnit, u.new || COMPARE_TEXT.noUnit) })
  }
  for (const t of cmp.tables ?? []) {
    if (t.status === 'added') { out.push({ kind: 'table_added', breaking: false, text: COMPARE_TEXT.tableAdded(t.name) }); continue }
    if (t.status === 'removed') { out.push({ kind: 'table_removed', breaking: true, text: COMPARE_TEXT.tableRemoved(t.name) }); continue }
    // 服务端的破坏性原话（tables[].breaking：table_changes 的说明，单位除外——单位另在 units_changed、排最前）是这张表
    // 破坏性变化的完整清单，结构化字段表达不了的（占位符含义按表报、没有列）只在这里。有原话时破坏性的一律照原话列：
    // 结构化字段只补非破坏性的（新增列）和单位，不再各摊一条——否则同一处变化列两遍；反过来只要表上有一条结构化的
    // 破坏项就跳过原话，又会把占位符含义这类整批丢掉。原话里没有 kind，前端分不出哪条对应哪个结构化字段，所以按表
    // 整体取一边。没有原话时（只给了结构的对照）才由结构化字段摊出破坏性的项
    const verbatim = (t.breaking ?? []).filter((m) => typeof m === 'string' && m.trim())
    const fromStructure = verbatim.length === 0
    for (const c of t.columns ?? []) {
      if (c.status === 'added') {
        out.push({ kind: 'column_added', breaking: false, text: COMPARE_TEXT.columnAdded(t.name, c.name, c.new?.unit ?? '') })
      } else if (c.status === 'removed') {
        if (fromStructure) out.push({ kind: 'column_removed', breaking: true, text: COMPARE_TEXT.columnRemoved(t.name, c.name) })
      } else {
        for (const k of c.changes ?? []) {
          if (k === 'unit') {
            if (unitDone.has(`${t.name}\u0000${c.name}`)) continue
            out.push({ kind: 'unit', breaking: true,
              text: COMPARE_TEXT.unit(t.name, c.name, c.old?.unit || COMPARE_TEXT.noUnit, c.new?.unit || COMPARE_TEXT.noUnit) })
            continue
          }
          if (BREAKING_CHANGES.has(k) && !fromStructure) continue
          const brief = (x: typeof c.old) => (k === 'type' ? x?.type ?? '' : k === 'source' ? x?.source ?? '' : '')
          out.push({ kind: k, breaking: BREAKING_CHANGES.has(k),
            text: COMPARE_TEXT.change(t.name, c.name, COMPARE_TEXT.changeKind[k] ?? COMPARE_TEXT.changeOther, brief(c.old), brief(c.new)) })
        }
      }
    }
    if (fromStructure && t.grain?.changed) {
      out.push({ kind: 'grain', breaking: true,
        text: COMPARE_TEXT.grain(t.name, join(t.grain.old) || COMPARE_TEXT.nothing, join(t.grain.new) || COMPARE_TEXT.nothing) })
    }
    if (fromStructure && t.kind?.old && t.kind?.new && t.kind.old !== t.kind.new) {
      out.push({ kind: 'table_kind', breaking: true, text: COMPARE_TEXT.tableKind(t.name,
        COMPARE_TEXT.tableKindLabel[t.kind.old] ?? t.kind.old, COMPARE_TEXT.tableKindLabel[t.kind.new] ?? t.kind.new) })
    }
    for (const msg of verbatim) out.push({ kind: 'breaking', breaking: true, text: COMPARE_TEXT.breakingLine(t.name, msg) })
  }
  for (const s of cmp.segments ?? []) {
    // 新增、去掉的分段：服务端在 labels.added / labels.removed 里给全部标签（标题在 title.new / title.old），写进同一条，
    // 不另摊「加入标签」「去掉标签」——那两句是给配对上的分段说增减的，对整段新增的分段说「加入」会误以为原来就有它
    if (s.status === 'added') {
      out.push({ kind: 'segment_added', breaking: false,
        text: COMPARE_TEXT.segAdded(s.id, s.title?.new ?? '', quoted(s.labels?.added ?? [])) })
      continue
    }
    if (s.status === 'removed') {
      out.push({ kind: 'segment_removed', breaking: false,
        text: COMPARE_TEXT.segRemoved(s.id, s.title?.old ?? '', quoted(s.labels?.removed ?? [])) })
      continue
    }
    if (s.title && s.title.old !== s.title.new && (s.title.old || s.title.new)) {
      out.push({ kind: 'segment_title', breaking: false, text: COMPARE_TEXT.segTitle(s.id, s.title.old ?? '', s.title.new ?? '') })
    }
    if (s.labels?.added?.length) out.push({ kind: 'labels', breaking: false, text: COMPARE_TEXT.labelsAdded(s.id, quoted(s.labels.added)) })
    if (s.labels?.removed?.length) out.push({ kind: 'labels', breaking: false, text: COMPARE_TEXT.labelsRemoved(s.id, quoted(s.labels.removed)) })
    if (s.ignore?.added?.length) out.push({ kind: 'ignore', breaking: false, text: COMPARE_TEXT.ignoreAdded(s.id, quoted(s.ignore.added)) })
    if (s.ignore?.removed?.length) out.push({ kind: 'ignore', breaking: false, text: COMPARE_TEXT.ignoreRemoved(s.id, quoted(s.ignore.removed)) })
  }
  for (const r of cmp.relations ?? []) {
    if (r.status === 'same' || r.old === r.new) continue
    out.push({ kind: 'relation', breaking: false,
      text: COMPARE_TEXT.relation(r.id, r.old ?? COMPARE_TEXT.relationNone, r.new ?? COMPARE_TEXT.relationNone) })
  }
  for (const s of cmp.sheets ?? []) {
    if (s.name && s.name.old !== s.name.new && s.name.old && s.name.new) {
      out.push({ kind: 'sheet_name', breaking: false, text: COMPARE_TEXT.sheetName(s.name.old, s.name.new) })
    }
  }
  if (cmp.mode && cmp.mode.old !== cmp.mode.new && cmp.mode.new) {
    const label = (m: string | null) => (m ? ACCUMULATE_TEXT.modeLabel[m] ?? m : ACCUMULATE_TEXT.modeLabel.replace)
    out.push({ kind: 'mode', breaking: false, text: COMPARE_TEXT.mode(label(cmp.mode.old), label(cmp.mode.new)) })
  }
  const acc = cmp.accumulate?.change
  if (acc && COMPARE_TEXT.accumulate[acc]) out.push({ kind: 'accumulate', breaking: acc === 'semantic', text: COMPARE_TEXT.accumulate[acc] })
  // 单位最前，其次破坏性，其余在后；同一档照上面的先后（稳定排序）
  const rank = (x: CompareItem) => (x.kind === 'unit' ? 0 : x.breaking ? 1 : 2)
  return out.map((x, i) => ({ x, i })).sort((a, b) => rank(a.x) - rank(b.x) || a.i - b.i).map((a) => a.x)
}

/**
 * 对照块。attr 是根节点的定位属性（试运行回执 data-recipe-compare，修复预览 data-fix-compare）；notes 是对照下方
 * 的补充说明（重新起草时的「名字对齐」）。有对照、但没有任何变化时写「配方没有变化」；没有对照（compare 为 null，
 * 例如规则起草没得到完整配方）时不写这句——那时根本没有可比的新配方，说「没有变化」是错的，原因由 children 说明
 */
export function RecipeCompare({ compare, attr = 'data-recipe-compare', title = COMPARE_TEXT.title, notes, notesTitle, children }: {
  compare: RecipeComparison | null | undefined
  attr?: string
  title?: string
  notes?: string[]
  notesTitle?: string
  children?: ReactNode
}) {
  const items = compareItems(compare)
  return (
    <section className="space-y-1.5 rounded-lg border bg-panel px-3 py-2" {...{ [attr]: compare?.breaking ? 'breaking' : '' }}>
      <h4 className="text-xs font-semibold">{title}</h4>
      {compare && !items.length && <p className="text-xs text-faint" data-compare-none>{COMPARE_TEXT.none}</p>}
      {items.length > 0 && (
        <ul className="space-y-1">
          {items.map((it, i) => (
            <li key={`${it.kind}-${i}`} className="flex flex-wrap items-baseline gap-x-2 rounded-md border px-2.5 py-1.5 text-xs"
                data-change-kind={it.kind} data-breaking={it.breaking || undefined}
                style={it.breaking ? { borderColor: 'color-mix(in srgb, var(--warn) 45%, var(--border))' } : undefined}>
              {it.breaking && <span className="chip" style={{ color: 'var(--warn)', borderColor: 'var(--warn)' }}>{COMPARE_TEXT.breakingBadge}</span>}
              <span className="min-w-0 flex-1 break-words">{it.text}</span>
            </li>
          ))}
        </ul>
      )}
      {!!notes?.length && (
        <div className="space-y-0.5" data-compare-notes>
          {notesTitle && <div className="text-2xs font-medium text-dim">{notesTitle}</div>}
          <ul className="list-disc space-y-0.5 pl-4 text-2xs leading-relaxed text-dim">
            {notes.map((n, i) => <li key={i}>{n}</li>)}
          </ul>
        </div>
      )}
      {children}
    </section>
  )
}
