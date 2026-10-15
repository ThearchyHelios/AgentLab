/**
 * 蓝图页右边的卡片（docs/design/normal-user-apps.md §2.2、§4.2）和生成的工作流（关节，§2.3、N11）。
 *
 * 卡片按服务端定的顺序摆（blueprints/spec.CARDS），选了模板的只摆模板用到的。每张卡头上标状态：缺、待确认、已确认。
 * 「缺」按服务端的蓝图检查说（Blueprint.issues 里缺必填项、没对到表、岗位没人的那几条）——项的状态默认就是缺，单位、筛选
 * 条件这些选填的空着也是缺，只看项的状态会把它们都标成缺；「待确认」是助手、模板、目录提的、你还没看过的项。界面不自己判
 * 哪些项必填（P7）。
 *
 * 每一项可以点开改：改完发一个 bp_set（值是纯值，服务端记成来源「你」、已确认）；待确认的可以直接点「确认」（只改状态，
 * 来源不变）。加一项是 bp_add（卡片字段写纯值），删一项是 bp_remove。对到数据的项写表、中文名、粒度、业务日期和
 * 「为什么对到这张表」（来自查看和数据目录）。
 *
 * 关节：按卡片生成之后列出生成关节的状态；「让助手填」走 fill-joints-stream，各组并行，逐组写进度。
 */
import { useState } from 'react'
import type { ReactNode } from 'react'
import { Database, Plus, Sparkles, Square, Trash2 } from 'lucide-react'
import clsx from 'clsx'
import { Spinner, confirmDialog, promptDialog, toast } from '../../components/ui'
import {
  BLUEPRINT_TEXT, BP_EDIT_TEXT, CARD_LABEL, ITEM_LABEL, ITEM_STATUS_LABEL, JOINT_BY_LABEL, JOINT_KIND_LABEL,
  JOINT_STATUS_LABEL, bpPathText, itemSourceText,
} from '../../lib/terms'
import { chatBusy, useBlueprint } from '../../store/blueprint'
import type {
  Blueprint, BpDataItem, BpField, BpItem, BpOp, BpStep, ItemStatus, JointState, PlainIssue, TemplateInfo,
} from '../../typesBlueprint'
import { SELF_RECORDS, ValueEditor, valueText, type EditorKind, type EditorScope } from './editors'
import { isItem, missingPaths, visibleCards, type CardKey } from './shared'

const STATUS_COLOR: Record<ItemStatus, string> = {
  confirmed: 'var(--st-done)', proposed: 'var(--st-waiting)', missing: 'var(--st-failed)',
}

const DATA_KEYS: (keyof BpDataItem)[] = ['label', 'unit', 'table', 'measure', 'date', 'filter', 'grain']
const STEP_KEYS: (keyof BpStep)[] = ['label', 'who', 'policy', 'due', 'on_timeout', 'threshold']
const STEP_KIND: Record<string, EditorKind> = {
  label: 'text', who: 'principals', policy: 'policy', due: 'duration', on_timeout: 'timeout', threshold: 'threshold',
}
const DATA_KIND: Record<string, EditorKind> = {
  label: 'text', unit: 'text', table: 'table', measure: 'text', date: 'date', filter: 'text', grain: 'text',
}

/** 卡片上的修改：发 bp_* 操作，来源记成「你」。成功提示一句，被拒照实说服务端的原因 */
function useEdit(blueprintId: string) {
  const [busy, setBusy] = useState<string | null>(null)
  const run = async (key: string, ops: BpOp[], ok: string = BLUEPRINT_TEXT.saved): Promise<boolean> => {
    setBusy(key)
    try {
      await useBlueprint.getState().edit(blueprintId, { ops })
      toast.ok(ok)
      return true
    } catch (e) {
      toast.error(e)
      return false
    } finally {
      setBusy(null)
    }
  }
  return { busy, run }
}

interface CardCtx {
  bp: Blueprint
  edit: ReturnType<typeof useEdit>
  locked: boolean
  templates: TemplateInfo[] | null
  /** 服务端说缺的位置；老服务端没给检查结果时为 null，退回项本身的状态 */
  missing: Set<string> | null
  /** 编辑框的选项：「填什么」里的字段、「看哪些数」里的指标 */
  scope: EditorScope
}

/** 这一项在界面上标什么：缺按服务端检查，待确认、已确认按项本身；空着的选填项不标 */
function shownStatus(ctx: CardCtx, path: string, item: BpItem): ItemStatus | null {
  if (item.status !== 'missing') return item.value == null ? null : item.status
  if (!ctx.missing) return 'missing'
  return ctx.missing.has(path) ? 'missing' : null
}

/** 一张卡片的状态：这张卡片上有服务端说缺的就是缺；有待确认的就是待确认；有值的都确认了是已确认 */
function cardStatus(ctx: CardCtx, card: CardKey, items: [string, BpItem | null | undefined][]): ItemStatus | null {
  const shown = items.filter((x): x is [string, BpItem] => isItem(x[1])).map(([p, i]) => shownStatus(ctx, p, i))
  const issues = ctx.missing ? [...ctx.missing].some((p) => p === `/${card}` || p.startsWith(`/${card}/`)) : false
  if (issues || shown.includes('missing')) return 'missing'
  if (shown.includes('proposed')) return 'proposed'
  return shown.includes('confirmed') ? 'confirmed' : null
}

/** 新加一项的 id（数据项、步骤）：小写字母开头，和已有的不重 */
function freshId(prefix: string, taken: string[]): string {
  for (let n = taken.length + 1; ; n += 1) if (!taken.includes(`${prefix}${n}`)) return `${prefix}${n}`
}

/** 新字段的名字：说得出的名字能当字段名就用它（中文也行），不能的用 f1、f2… */
function freshFieldName(title: string, taken: string[]): string {
  const name = title.trim().replace(/\s+/g, '_')
  if (/^[\p{L}_][\p{L}\p{N}_]{0,39}$/u.test(name) && !taken.includes(name)) return name
  return freshId('f', taken)
}

export function BlueprintCards({ blueprintId, templates }: { blueprintId: string; templates: TemplateInfo[] | null }) {
  const entry = useBlueprint((s) => s.byId[blueprintId])
  const bp = entry?.blueprint
  const edit = useEdit(blueprintId)
  if (!bp) return null
  const spec = bp.spec
  const template = templates?.find((t) => t.id === (spec.template?.id ?? bp.template_id)) ?? null
  // 个人应用只有拥有者看得见：不摆「谁能看」
  const cards = visibleCards(spec, template?.cards.map((c) => c.card)).filter((c) => !(c === 'access' && bp.scope === 'personal'))
  // 对话进行中：助手正在改卡片，人先别改，免得两边的改动互相覆盖
  const locked = chatBusy(entry) || entry?.fill?.phase === 'running'
  const ctx: CardCtx = {
    bp, edit, locked, templates, missing: missingPaths(bp),
    scope: {
      fields: spec.fields.map((f) => ({ name: f.name, title: String((f.spec?.value as Record<string, unknown> | null)?.title ?? f.name) })),
      metrics: spec.data.map((d) => ({ id: d.id, title: String(d.label?.value ?? d.id) })),
      recordsSelf: (spec.template?.id ?? bp.template_id) === 'records',
    },
  }
  return (
    <div className="space-y-3" data-blueprint-cards={cards.join(',')}>
      <div>
        <h2 className="text-xs font-semibold">{BLUEPRINT_TEXT.cardsTitle}</h2>
        <p className="mt-0.5 text-2xs leading-relaxed text-faint">{BLUEPRINT_TEXT.cardsHint}</p>
      </div>
      {cards.map((c) => <CardFor key={c} card={c} ctx={ctx} template={template} />)}
      {template?.id !== 'freeform' && <Joints blueprintId={blueprintId} />}
    </div>
  )
}

function CardFor({ card, ctx, template }: { card: CardKey; ctx: CardCtx; template: TemplateInfo | null }) {
  const spec = ctx.bp.spec
  const note = template?.cards.find((c) => c.card === card)?.note
  const issues = (ctx.bp.issues ?? []).filter((i) => i.path === `/${card}` || i.path?.startsWith(`/${card}/`))
  switch (card) {
    case 'purpose':
      return (
        <Card card={card} status={cardStatus(ctx, card, [['/purpose', spec.purpose]])} note={note} issues={issues}>
          <ItemRow ctx={ctx} path="/purpose" item={spec.purpose} label={ITEM_LABEL.purpose} kind="textarea" />
        </Card>
      )
    case 'template':
      return <TemplateCard ctx={ctx} template={template} issues={issues} />
    case 'data':
      return (
        <Card card={card} note={note} issues={issues}
              status={cardStatus(ctx, card, spec.data.flatMap((d, i) => DATA_KEYS.map((k) => [`/data/${i}/${k}`, d[k] as BpItem] as [string, BpItem])))}
              actions={<AddButton ctx={ctx} label={BP_EDIT_TEXT.addData} prompt={BP_EDIT_TEXT.addDataPrompt} path="/data"
                                  make={(name) => ({ id: freshId('d', spec.data.map((d) => d.id)), label: name })} />}>
          {spec.data.length === 0 && <Empty />}
          {spec.data.map((d, i) => <DataBlock key={d.id} ctx={ctx} item={d} index={i} />)}
        </Card>
      )
    case 'cadence':
      return (
        <Card card={card} status={cardStatus(ctx, card, [['/cadence', spec.cadence]])} note={note} issues={issues}>
          <ItemRow ctx={ctx} path="/cadence" item={spec.cadence} label={ITEM_LABEL.cadence} kind="cadence" />
        </Card>
      )
    case 'people':
      return (
        <Card card={card} note={note} issues={issues}
              status={cardStatus(ctx, card, spec.people.flatMap((s, i) => STEP_KEYS.map((k) => [`/people/${i}/${k}`, s[k] as BpItem] as [string, BpItem])))}
              actions={<AddButton ctx={ctx} label={BP_EDIT_TEXT.addStep} prompt={BP_EDIT_TEXT.addStepPrompt} path="/people"
                                  make={(name) => ({ id: freshId('s', spec.people.map((s) => s.id)), label: name })} />}>
          {spec.people.length === 0 && <Empty />}
          {spec.people.map((s, i) => (
            <SubBlock key={s.id} ctx={ctx} title={String(s.label?.value ?? '') || BLUEPRINT_TEXT.item(i + 1)} path={`/people/${i}`}
                      attr={{ 'data-bp-step': s.id }}>
              {STEP_KEYS.map((k) => (
                <ItemRow key={k} ctx={ctx} path={`/people/${i}/${k}`} item={s[k] as BpItem} label={ITEM_LABEL[k]} kind={STEP_KIND[k]} />
              ))}
            </SubBlock>
          ))}
        </Card>
      )
    case 'fields':
      return (
        <Card card={card} note={note} issues={issues}
              status={cardStatus(ctx, card, spec.fields.map((f, i) => [`/fields/${i}/spec`, f.spec] as [string, BpItem]))}
              actions={<AddButton ctx={ctx} label={BP_EDIT_TEXT.addField} prompt={BP_EDIT_TEXT.addFieldPrompt} path="/fields"
                                  make={(name) => ({ name: freshFieldName(name, spec.fields.map((f) => f.name)), sensitive: false,
                                    spec: { type: 'string', title: name } })} />}>
          {spec.fields.length === 0 && <Empty />}
          {spec.fields.map((f, i) => <FieldBlock key={f.name} ctx={ctx} field={f} index={i} />)}
        </Card>
      )
    case 'writeback': {
      const wb = spec.writeback
      return (
        <Card card={card} note={note} issues={issues}
              status={cardStatus(ctx, card, wb ? [['/writeback/capability', wb.capability], ['/writeback/mapping', wb.mapping],
                ['/writeback/confirm', wb.confirm]] : [])}
              actions={!wb ? (
                <button type="button" className="btn btn-xs btn-ghost" disabled={ctx.locked || !!ctx.edit.busy} data-bp-add="/writeback"
                        onClick={() => void ctx.edit.run('wb', [{ op: 'bp_set', path: '/writeback', value: {} }])}>
                  <Plus size={11} aria-hidden /> {BP_EDIT_TEXT.addWriteback}
                </button>
              ) : undefined}>
          {!wb ? <Empty /> : (
            <SubBlock ctx={ctx} title={CARD_LABEL.writeback} path="/writeback">
              <ItemRow ctx={ctx} path="/writeback/capability" item={wb.capability} label={ITEM_LABEL.capability} kind="text" />
              <ItemRow ctx={ctx} path="/writeback/mapping" item={wb.mapping} label={ITEM_LABEL.mapping} kind="mapping" />
              <ItemRow ctx={ctx} path="/writeback/confirm" item={wb.confirm} label={ITEM_LABEL.confirm} kind="bool" />
            </SubBlock>
          )}
        </Card>
      )
    }
    case 'access':
      return <AccessCard ctx={ctx} note={note} issues={issues} />
  }
}

function Empty() {
  return <p className="text-2xs text-faint">{BLUEPRINT_TEXT.empty}</p>
}

function Card({ card, status, note, actions, issues, children }: {
  card: CardKey; status: ItemStatus | null; note?: string; actions?: ReactNode; issues?: PlainIssue[]; children: ReactNode
}) {
  return (
    <section className="rounded-lg border bg-panel" data-card={card} data-card-status={status ?? 'none'}
             aria-labelledby={`bp-card-${card}`}>
      <header className="flex flex-wrap items-center gap-2 border-b px-3 py-2">
        <h3 id={`bp-card-${card}`} className="text-xs font-semibold">{CARD_LABEL[card]}</h3>
        {status && <StatusChip status={status} />}
        {note ? <span className="min-w-0 flex-1 truncate text-2xs text-faint" title={note}>{note}</span> : <span className="flex-1" />}
        {actions}
      </header>
      <div className="space-y-2 px-3 py-2">
        {children}
        {/* 服务端核对这张卡片说的话（缺什么、超出模板能力、待确认），白话优先 */}
        {!!issues?.length && (
          <ul className="space-y-0.5 border-t pt-1.5" data-card-issues={issues.length}>
            {issues.map((i, k) => (
              <li key={`${i.code}-${k}`} className="text-2xs leading-relaxed" data-problem={i.code}
                  style={{ color: i.level === 'error' ? 'var(--st-failed)' : 'var(--st-waiting)' }}>
                {i.plain?.title ?? i.message}
                {i.plain?.action && <span className="text-dim">：{i.plain.action}</span>}
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  )
}

function StatusChip({ status }: { status: ItemStatus }) {
  return (
    <span className="chip shrink-0" style={{ color: STATUS_COLOR[status] }} data-status-chip={status}>
      {ITEM_STATUS_LABEL[status]}
    </span>
  )
}

/** 一组子项（看哪些数的一项、谁来做的一步、填什么的一个字段、写回）：标题加「删掉这一项」 */
function SubBlock({ ctx, title, path, attr, head, children }: {
  ctx: CardCtx; title: string; path: string; attr?: Record<string, string>; head?: ReactNode; children: ReactNode
}) {
  const remove = async () => {
    const where = bpPathText(path)
    if (!(await confirmDialog({ title: BP_EDIT_TEXT.removeTitle(where), confirmLabel: BP_EDIT_TEXT.removeConfirm, danger: true }))) return
    await ctx.edit.run(`rm:${path}`, [{ op: 'bp_remove', path }], BLUEPRINT_TEXT.removed)
  }
  return (
    <div className="rounded-md border p-2" {...attr}>
      <div className="mb-1 flex items-center gap-1.5">
        <span className="min-w-0 flex-1 truncate text-xs font-medium text-fg">{title}</span>
        <button type="button" className="btn btn-xs btn-ghost" disabled={ctx.locked || !!ctx.edit.busy} onClick={() => void remove()}
                aria-label={`${BLUEPRINT_TEXT.remove}：${title}`} title={BLUEPRINT_TEXT.remove} data-bp-remove={path}>
          <Trash2 size={11} aria-hidden />
        </button>
      </div>
      {head}
      <div className="space-y-1.5">{children}</div>
    </div>
  )
}

/** 卡片上的一项：名字、值、状态、来源、为什么；「改」打开编辑框，待确认的可以直接确认 */
function ItemRow({ ctx, path, item, label, kind }: {
  ctx: CardCtx; path: string; item: BpItem | null | undefined; label: string; kind: EditorKind
}) {
  const [editing, setEditing] = useState(false)
  if (!isItem(item)) return null
  const busy = ctx.edit.busy === path || ctx.edit.busy === `${path}/status`
  const status = shownStatus(ctx, path, item)
  const empty = item.value == null || item.value === ''
  const save = async (value: unknown) => {
    if (await ctx.edit.run(path, [{ op: 'bp_set', path, value }])) setEditing(false)
  }
  return (
    <div className="text-xs" data-bp-item={path} data-item-status={status ?? 'empty'} data-item-source={item.source}>
      <div className="flex flex-wrap items-start gap-x-2 gap-y-0.5">
        <span className="w-28 shrink-0 text-2xs leading-5 text-faint">{label}</span>
        <span className={clsx('min-w-0 flex-1 break-words leading-5', empty ? 'text-faint' : 'text-fg')} data-bp-value="">
          {valueText(kind, item.value, ctx.scope)}
        </span>
        <span className="flex shrink-0 items-center gap-1">
          {status && <StatusChip status={status} />}
          {!empty && <span className="chip text-faint" data-item-source-chip={item.source}>{itemSourceText(item.source)}</span>}
          {item.status === 'proposed' && !empty && (
            <button type="button" className="btn btn-xs" disabled={ctx.locked || !!ctx.edit.busy} title={BLUEPRINT_TEXT.confirmHint}
                    onClick={() => void ctx.edit.run(`${path}/status`, [{ op: 'bp_set', path: `${path}/status`, value: 'confirmed' }],
                      BLUEPRINT_TEXT.confirmed)}
                    data-bp-confirm="">
              {BLUEPRINT_TEXT.confirm}
            </button>
          )}
          <button type="button" className="btn btn-xs btn-ghost" disabled={ctx.locked || !!ctx.edit.busy || editing}
                  aria-label={BLUEPRINT_TEXT.editTitle(label)} onClick={() => setEditing(true)} data-bp-edit="">
            {BLUEPRINT_TEXT.edit}
          </button>
        </span>
      </div>
      {item.why && <p className="pl-[7.5rem] text-2xs leading-relaxed text-dim" data-bp-why="">{BLUEPRINT_TEXT.why}：{item.why}</p>}
      {editing && (
        <ValueEditor kind={kind} value={item.value} busy={busy} scope={ctx.scope} onCancel={() => setEditing(false)}
                     onSave={(v) => void save(v)} />
      )}
    </div>
  )
}

/** 「加一项」：问个名字，按卡片的形状加一项（卡片字段写纯值，服务端记成来源「你」） */
function AddButton({ ctx, label, prompt, path, make }: {
  ctx: CardCtx; label: string; prompt: string; path: string; make: (name: string) => Record<string, unknown>
}) {
  const add = async () => {
    const name = await promptDialog({ title: label, label: prompt, confirmLabel: BLUEPRINT_TEXT.add })
    if (!name) return
    await ctx.edit.run(`add:${path}`, [{ op: 'bp_add', path, value: make(name) }])
  }
  return (
    <button type="button" className="btn btn-xs btn-ghost" disabled={ctx.locked || !!ctx.edit.busy} onClick={() => void add()}
            data-bp-add={path}>
      <Plus size={11} aria-hidden /> {label}
    </button>
  )
}

/**
 * 「看哪些数」的一项：先写对到的数据（表、粒度、业务日期、为什么），再逐项列。表只写「源.表」（服务端 BpDataItem.table 是
 * {source, table}）：表的中文名不在蓝图里，要看去数据目录，这里不另取
 */
function DataBlock({ ctx, item, index }: { ctx: CardCtx; item: BpDataItem; index: number }) {
  const table = item.table?.value ?? null
  const date = item.date?.value ?? null
  const head = table?.table ? (
    <div className="mb-1.5 rounded border bg-bg px-2 py-1.5 text-2xs leading-relaxed" data-bp-match={`${table.source}.${table.table}`}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5">
        <span className="inline-flex items-center gap-1 text-fg"><Database size={11} aria-hidden className="text-faint" />
          {BLUEPRINT_TEXT.dataTable}：<span className={table.source === SELF_RECORDS ? '' : 'mono'}>{valueText('table', table)}</span></span>
        {item.grain?.value != null && <span>{BLUEPRINT_TEXT.dataGrain}：<span className="text-fg">{String(item.grain.value)}</span></span>}
        {date?.column && <span>{BLUEPRINT_TEXT.dataDate}：<span className="text-fg">{valueText('date', date)}</span></span>}
      </div>
      {item.table.why && <div className="mt-0.5 text-dim" data-bp-match-why="">{BLUEPRINT_TEXT.why}：{item.table.why}</div>}
    </div>
  ) : null
  return (
    <SubBlock ctx={ctx} title={String(item.label?.value ?? '') || BLUEPRINT_TEXT.item(index + 1)} path={`/data/${index}`}
              attr={{ 'data-bp-data': item.id }} head={head}>
      {item.reuse && (
        <p className="text-2xs text-dim" data-bp-reuse="">{BLUEPRINT_TEXT.dataReuseText(item.reuse.metric_id, item.reuse.version)}</p>
      )}
      {DATA_KEYS.map((k) => (
        <ItemRow key={k} ctx={ctx} path={`/data/${index}/${k}`} item={item[k] as BpItem} label={ITEM_LABEL[k]} kind={DATA_KIND[k]} />
      ))}
    </SubBlock>
  )
}

/** 「填什么」的一个字段：字段说明（含是否必填）、敏感 */
function FieldBlock({ ctx, field, index }: { ctx: CardCtx; field: BpField; index: number }) {
  const title = String((field.spec?.value as Record<string, unknown> | null)?.title ?? field.name)
  const path = `/fields/${index}`
  return (
    <SubBlock ctx={ctx} title={title} path={path} attr={{ 'data-bp-field': field.name }}>
      <ItemRow ctx={ctx} path={`${path}/spec`} item={field.spec} label={ITEM_LABEL.spec} kind="field" />
      <label className="flex items-center gap-1.5 text-2xs text-dim" title={BP_EDIT_TEXT.sensitiveHint}>
        <input type="checkbox" checked={field.sensitive} disabled={ctx.locked || !!ctx.edit.busy} data-bp-sensitive=""
               onChange={(e) => void ctx.edit.run(`${path}/sensitive`, [{ op: 'bp_set', path: `${path}/sensitive`, value: e.target.checked }])} />
        {BP_EDIT_TEXT.sensitive}
      </label>
    </SubBlock>
  )
}

/** 场景：选了哪个模板；可以换（个人应用只能换成个人应用能用的） */
function TemplateCard({ ctx, template, issues }: { ctx: CardCtx; template: TemplateInfo | null; issues: PlainIssue[] }) {
  const ref = ctx.bp.spec.template
  const usable = (ctx.templates ?? []).filter((t) => t.id !== 'freeform' && (ctx.bp.scope !== 'personal' || !!t.personal))
  return (
    <Card card="template" status={ref ? 'confirmed' : 'missing'} issues={issues}>
      <p className="text-xs" data-bp-template={ref?.id ?? ''}>
        {ref ? BLUEPRINT_TEXT.templateOf(template?.label ?? ref.id, ref.version) : <span className="text-dim">{BLUEPRINT_TEXT.noTemplate}</span>}
      </p>
      {template?.description && <p className="text-2xs leading-relaxed text-dim">{template.description}</p>}
      {ref?.id !== 'freeform' && usable.length > 0 && (
        <label className="flex flex-wrap items-center gap-1.5 text-2xs text-dim" title={BP_EDIT_TEXT.templateHint}>
          {BP_EDIT_TEXT.template}
          <select className="field w-auto py-0.5 text-xs" value={ref?.id ?? ''} disabled={ctx.locked || !!ctx.edit.busy}
                  data-bp-template-pick=""
                  onChange={(e) => {
                    const t = usable.find((x) => x.id === e.target.value)
                    if (t) void ctx.edit.run('template', [{ op: 'bp_template', template: { id: t.id, version: t.version } }])
                  }}>
            {!ref && <option value="">—</option>}
            {usable.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
          </select>
        </label>
      )}
    </Card>
  )
}

/** 谁能看：使用、查看全部、管理三档的人和组。授权只由人定（服务端不收助手的改动） */
function AccessCard({ ctx, note, issues }: { ctx: CardCtx; note?: string; issues: PlainIssue[] }) {
  const access = ctx.bp.spec.access
  const [editing, setEditing] = useState<'use' | 'observe' | 'manage' | null>(null)
  const empty = !access.use.length && !access.observe.length && !access.manage.length
  return (
    <Card card="access" status={empty ? null : 'confirmed'} note={note} issues={issues}>
      {empty && <p className="text-2xs text-dim">{BP_EDIT_TEXT.accessEmpty}</p>}
      {(['use', 'observe', 'manage'] as const).map((role) => (
        <div key={role} className="text-xs" data-bp-access={role}>
          <div className="flex flex-wrap items-start gap-x-2">
            <span className="w-28 shrink-0 text-2xs leading-5 text-faint">{ITEM_LABEL[role]}</span>
            <span className="min-w-0 flex-1 leading-5 text-fg">{valueText('principals', access[role])}</span>
            <button type="button" className="btn btn-xs btn-ghost" disabled={ctx.locked || !!ctx.edit.busy || !!editing}
                    aria-label={BLUEPRINT_TEXT.editTitle(ITEM_LABEL[role])} onClick={() => setEditing(role)}>
              {BLUEPRINT_TEXT.edit}
            </button>
          </div>
          {editing === role && (
            <ValueEditor kind="grantees" value={access[role]} busy={ctx.edit.busy === `/access/${role}`} scope={ctx.scope}
                         onCancel={() => setEditing(null)}
                         onSave={async (v) => {
                           if (await ctx.edit.run(`/access/${role}`, [{ op: 'bp_set', path: `/access/${role}`, value: v }])) setEditing(null)
                         }} />
          )}
        </div>
      ))}
    </Card>
  )
}

// ---------------------------------------------------------------- 关节

/** 按卡片直接填好的确定性关节（表单、处理人、定时这些）：不经模型，不逐个列 */
const deterministic = (j: JointState) => j.status === 'filled' && j.by === 'template'

function Joints({ blueprintId }: { blueprintId: string }) {
  const entry = useBlueprint((s) => s.byId[blueprintId])
  const [busy, setBusy] = useState(false)
  const bp = entry?.blueprint
  if (!bp) return null
  const fill = entry?.fill
  const all = Object.entries(bp.joints ?? {})
  // 只列生成关节（取数的 SQL、指标的算法）：确定性关节按卡片填，汇总成一句
  const joints = all.filter(([, j]) => !deterministic(j))
  const fixed = all.length - joints.length
  const problems: PlainIssue[] = fill?.problems ?? entry?.instantiated?.problems ?? []
  const pending = joints.filter(([id, j]) => (fill?.joints[id]?.status ?? j.status) !== 'filled')
  const filling = fill?.phase === 'running'
  const locked = chatBusy(entry) || filling
  const instantiate = async () => {
    setBusy(true)
    try {
      await useBlueprint.getState().instantiate(blueprintId)
      toast.ok(BLUEPRINT_TEXT.instantiated)
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }
  // 关节的说法：模板声明的名字，没有就写种类和 id（实例化之前存下的关节没有这两项）
  const nameOf = (id: string) => {
    const j: Partial<JointState> = bp.joints?.[id] ?? {}
    return j.label || (j.kind ? `${JOINT_KIND_LABEL[j.kind] ?? j.kind}（${id}）` : id)
  }
  return (
    <section className="rounded-lg border bg-panel" data-bp-joints={joints.length} aria-labelledby="bp-joints">
      <header className="flex flex-wrap items-center gap-2 border-b px-3 py-2">
        <h3 id="bp-joints" className="text-xs font-semibold">{BLUEPRINT_TEXT.jointsTitle}</h3>
        <span className="flex-1" />
        <button type="button" className="btn btn-xs" disabled={busy || locked} onClick={() => void instantiate()} data-bp-instantiate="">
          {busy ? <Spinner size={10} /> : null} {busy ? BLUEPRINT_TEXT.instantiating : BLUEPRINT_TEXT.instantiate}
        </button>
        {filling ? (
          <button type="button" className="btn btn-xs" onClick={() => useBlueprint.getState().stopFill(blueprintId)} data-bp-fill-stop="">
            <Square size={10} aria-hidden /> {BLUEPRINT_TEXT.fillStop}
          </button>
        ) : joints.length > 0 && (
          <button type="button" className="btn btn-xs btn-primary" disabled={locked || busy || !pending.length}
                  onClick={() => useBlueprint.getState().fill(blueprintId)} data-bp-fill="">
            <Sparkles size={11} aria-hidden /> {fill?.phase === 'error' ? BLUEPRINT_TEXT.fillAgain : BLUEPRINT_TEXT.fill}
          </button>
        )}
      </header>
      <div className="space-y-2 px-3 py-2 text-xs">
        <p className="text-2xs leading-relaxed text-faint">{BLUEPRINT_TEXT.jointsHint}</p>
        {!all.length && <p className="text-2xs text-dim">{BLUEPRINT_TEXT.jointsEmpty}</p>}
        {fixed > 0 && <p className="text-2xs text-dim" data-bp-joints-fixed={fixed}>{BLUEPRINT_TEXT.jointsTemplate(fixed)}</p>}
        {joints.length > 0 && (
          <ul className="space-y-1">
            {joints.map(([id, j]) => {
              const live = fill?.joints[id]
              const status = live?.status ?? j.status
              const note = live?.note ?? j.note
              return (
                <li key={id} className="flex flex-wrap items-center gap-x-2 gap-y-0.5" data-bp-joint={id} data-joint-status={status}>
                  <span className="min-w-0 flex-1 truncate text-fg">{nameOf(id)}</span>
                  <span className="chip" style={{ color: status === 'filled' ? 'var(--st-done)' : status === 'refused' ? 'var(--st-failed)' : 'var(--st-waiting)' }}>
                    {JOINT_STATUS_LABEL[status] ?? status}
                  </span>
                  {status === 'filled' && <span className="chip text-faint">{JOINT_BY_LABEL[j.by] ?? j.by}</span>}
                  {status !== 'filled' && (
                    <span className="w-full text-2xs leading-relaxed text-dim">{note || BLUEPRINT_TEXT.jointPendingNote}</span>
                  )}
                </li>
              )
            })}
          </ul>
        )}
        {fill && fill.groups.length > 0 && (
          // 各组并行：一组一行，写这一组在做什么、填好没有
          <ol className="space-y-1 rounded border bg-bg p-2" data-bp-fill-groups={fill.groups.length} aria-live="polite">
            {fill.groups.map((g, i) => (
              <li key={g.id} className="flex flex-wrap items-center gap-x-2 text-2xs" data-bp-fill-group={g.id} data-group-status={g.status}>
                {g.status === 'running' ? <Spinner size={10} /> : null}
                <span className="font-medium text-fg">{BLUEPRINT_TEXT.group(i + 1)}</span>
                <span className="text-dim">{g.joints.map(nameOf).join('、')}</span>
                <span className="flex-1" />
                <span style={{ color: g.status === 'done' ? 'var(--st-done)' : g.status === 'failed' ? 'var(--st-failed)' : 'var(--st-running)' }}>
                  {g.status === 'done' ? BLUEPRINT_TEXT.groupDone : g.status === 'failed' ? BLUEPRINT_TEXT.groupFailed : g.line || BLUEPRINT_TEXT.groupRunning}
                </span>
                {g.message && <span className="w-full text-dim">{g.message}</span>}
              </li>
            ))}
          </ol>
        )}
        {fill?.error && <p className="text-2xs" style={{ color: 'var(--st-failed)' }} role="alert">{fill.error}</p>}
        {problems.length > 0 && (
          <ul className="space-y-1" data-bp-instantiate-problems={problems.length}>
            {problems.map((p, i) => (
              <li key={i} className="text-2xs leading-relaxed" data-problem={p.code}
                  style={{ color: p.level === 'error' ? 'var(--st-failed)' : 'var(--st-waiting)' }}>
                {p.plain?.title ?? p.message}
                {p.plain?.action && <span className="text-dim">：{p.plain.action}</span>}
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  )
}
