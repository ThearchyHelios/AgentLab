/**
 * 卡片上一项的显示和编辑（docs/design/normal-user-apps.md §4.2）：按值的形状分几种——一句话、对到哪张表、业务日期、多久更新、
 * 处理人、授权对象、通过规则、时限、超时怎么办、金额线、表单字段、是否、字段对应。
 *
 * 编辑框只负责把人填的拼成这一项的值，交给调用方发 bp_set（来源记成 user）；合不合规由服务端的 BlueprintApplier 判，
 * 被拒照实交回（P7）。值的形状照服务端 blueprints/ops.py 的白名单：处理人选了发起人或岗位就不能再加别人；字段说明是
 * agentlab.form/1 的一个属性另加是否必填；字段对应的取值是表单字段（form.x）、指标（metric.y）、核对出来的行（rows）或
 * 常量（文字加单引号）。说法都是白话：时限写「2 天 3 小时」，存的是 ISO 8601 时长；处理人写名字，不写 kind。
 */
import { useState } from 'react'
import type { ReactNode } from 'react'
import { Plus, X } from 'lucide-react'
import { PrincipalPicker, type Principal as PickerPrincipal } from '../../components/PrincipalPicker'
import { Field, Spinner } from '../../components/ui'
import {
  BLUEPRINT_TEXT, BP_EDIT_TEXT, CADENCE_PRESET_LABEL, FIELD_KIND_LABEL, PERIOD_LABEL, POLICY_LABEL, THRESHOLD_OP_LABEL,
  TIMEOUT_LABEL, WEEKDAY_LABEL, cadenceText, durationIso, durationText, policyText, principalText,
} from '../../lib/terms'
import type { BpPrincipal } from '../../typesBlueprint'

export type EditorKind =
  | 'text' | 'textarea' | 'table' | 'date' | 'cadence' | 'principals' | 'grantees' | 'policy' | 'duration' | 'timeout'
  | 'threshold' | 'field' | 'bool' | 'mapping'

/** 编辑框要的选项：「填什么」里的字段、「看哪些数」里的指标（金额线、字段对应从里面选） */
export interface EditorScope {
  fields: { name: string; title: string }[]
  metrics: { id: string; title: string }[]
}

type Obj = Record<string, any>
const obj = (v: unknown): Obj => (v && typeof v === 'object' && !Array.isArray(v) ? v as Obj : {})
const principals = (v: unknown): BpPrincipal[] => (Array.isArray(v) ? v.filter((p) => p && typeof p.name === 'string') : [])

// ---------------------------------------------------------------- 表单字段的类型

/** 字段说明（agentlab.form/1 的一个属性）→ 界面上的类型 */
export function fieldKindOf(spec: Obj): string {
  if (spec['x-widget'] === 'file') return 'file'
  if (spec['x-widget'] === 'textarea') return 'textarea'
  if (Array.isArray(spec.enum)) return 'select'
  if (spec.type === 'number') return 'number'
  if (spec.type === 'integer') return 'integer'
  if (spec.type === 'boolean') return 'checkbox'
  if (spec.format === 'date' || spec['x-widget'] === 'date') return 'date'
  return 'text'
}

/** 界面上的类型 → 字段说明；原有的其余属性（上下限、说明）照留 */
function fieldSpecOf(base: Obj, kind: string, title: string, options: string[]): Obj {
  const { type: _t, enum: _e, format: _f, 'x-widget': _w, 'x-enum-labels': _l, required: _r, ...rest } = base
  const head = { ...rest, title }
  switch (kind) {
    case 'file': return { ...head, type: 'object', 'x-widget': 'file' }
    case 'textarea': return { ...head, type: 'string', 'x-widget': 'textarea' }
    case 'select': return { ...head, type: 'string', enum: options }
    case 'number': return { ...head, type: 'number' }
    case 'integer': return { ...head, type: 'integer' }
    case 'checkbox': return { ...head, type: 'boolean' }
    case 'date': return { ...head, type: 'string', format: 'date' }
    default: return { ...head, type: 'string' }
  }
}

// ---------------------------------------------------------------- 显示

/** 字段对应的一个取值 → 白话：表单「金额」、指标「销售额」、核对出来的行、「常量」 */
function mapSourceText(source: unknown, scope?: EditorScope): string {
  if (typeof source !== 'string') return String(source)
  const m = /^(form|metric)\.(.+)$/.exec(source)
  if (m?.[1] === 'form') return `${BP_EDIT_TEXT.mapForm}「${scope?.fields.find((f) => f.name === m[2])?.title ?? m[2]}」`
  if (m?.[1] === 'metric') return `${BP_EDIT_TEXT.mapMetric}「${scope?.metrics.find((x) => x.id === m[2])?.title ?? m[2]}」`
  if (source === 'rows') return BP_EDIT_TEXT.mapRows
  return /^'.*'$/.test(source) ? `「${source.slice(1, -1)}」` : source
}

/** 一项的值写成一句话。空的写「（空）」 */
export function valueText(kind: EditorKind, value: unknown, scope?: EditorScope): string {
  if (value == null || value === '') return BLUEPRINT_TEXT.empty
  const o = obj(value)
  switch (kind) {
    case 'table': return o.source || o.table ? `${o.source ?? ''}.${o.table ?? ''}` : BLUEPRINT_TEXT.empty
    case 'date': return o.column ? `${o.column}${o.rule ? `（${o.rule}）` : ''}` : BLUEPRINT_TEXT.empty
    case 'cadence': return cadenceText(o) || BLUEPRINT_TEXT.empty
    case 'principals':
    case 'grantees': return principals(value).map(principalText).join('、') || BLUEPRINT_TEXT.empty
    case 'policy': return policyText(o) || BLUEPRINT_TEXT.empty
    case 'duration': return durationText(String(value))
    case 'timeout': {
      const to = principals(o.to).map(principalText).join('、')
      const every = o.every ? BP_EDIT_TEXT.everyText(durationText(String(o.every))) : ''
      return o.action ? `${TIMEOUT_LABEL[o.action] ?? o.action}${to ? `：${to}` : ''}${every}` : BLUEPRINT_TEXT.empty
    }
    case 'threshold': return o.field ? `${o.field} ${THRESHOLD_OP_LABEL[o.op] ?? o.op ?? ''} ${o.value ?? ''}` : BLUEPRINT_TEXT.empty
    case 'field':
      return [o.title, FIELD_KIND_LABEL[fieldKindOf(o)], o.required ? BP_EDIT_TEXT.required : ''].filter(Boolean).join(' · ')
    case 'bool': return value ? BP_EDIT_TEXT.yes : BP_EDIT_TEXT.no
    case 'mapping': return Object.entries(o).map(([k, v]) => `${k} ← ${mapSourceText(v, scope)}`).join('；') || BLUEPRINT_TEXT.empty
    default: return typeof value === 'string' ? value : JSON.stringify(value)
  }
}

// ---------------------------------------------------------------- 编辑

export interface ValueEditorProps {
  kind: EditorKind
  value: unknown
  busy: boolean
  onSave: (value: unknown) => void
  onCancel: () => void
  /** 金额线、字段对应从这里选字段和指标 */
  scope?: EditorScope
}

export function ValueEditor(props: ValueEditorProps) {
  const { kind } = props
  switch (kind) {
    case 'table': return <TableEditor {...props} />
    case 'date': return <DateEditor {...props} />
    case 'cadence': return <CadenceEditor {...props} />
    case 'principals': return <PrincipalsEditor {...props} />
    case 'grantees': return <GranteesEditor {...props} />
    case 'policy': return <PolicyEditor {...props} />
    case 'duration': return <DurationEditor {...props} />
    case 'timeout': return <TimeoutEditor {...props} />
    case 'threshold': return <ThresholdEditor {...props} />
    case 'field': return <FieldEditor {...props} />
    case 'bool': return <BoolEditor {...props} />
    case 'mapping': return <MappingEditor {...props} />
    default: return <TextEditor {...props} />
  }
}

/** 编辑框的外壳：内容加「保存」「取消」 */
function Shell({ busy, onSave, onCancel, children, ok = true }: {
  busy: boolean; onSave: () => void; onCancel: () => void; children: ReactNode; ok?: boolean
}) {
  return (
    <form className="mt-1.5 space-y-2 rounded-md border bg-bg p-2" data-bp-editor=""
          onSubmit={(e) => { e.preventDefault(); if (ok && !busy) onSave() }}>
      {children}
      <div className="flex justify-end gap-1.5">
        <button type="button" className="btn btn-xs btn-ghost" onClick={onCancel} disabled={busy}>{BLUEPRINT_TEXT.cancel}</button>
        <button type="submit" className="btn btn-xs btn-primary" disabled={busy || !ok} data-bp-save="">
          {busy && <Spinner size={10} />} {busy ? BLUEPRINT_TEXT.saving : BLUEPRINT_TEXT.save}
        </button>
      </div>
    </form>
  )
}

function TextEditor({ kind, value, busy, onSave, onCancel }: ValueEditorProps) {
  const [text, setText] = useState(typeof value === 'string' ? value : value == null ? '' : String(value))
  return (
    <Shell busy={busy} onCancel={onCancel} onSave={() => onSave(text.trim())} ok={!!text.trim()}>
      {kind === 'textarea'
        ? <textarea className="field" rows={3} value={text} autoFocus onChange={(e) => setText(e.target.value)} aria-label={BLUEPRINT_TEXT.edit} />
        : <input className="field" value={text} autoFocus onChange={(e) => setText(e.target.value)} aria-label={BLUEPRINT_TEXT.edit} />}
    </Shell>
  )
}

function TableEditor({ value, busy, onSave, onCancel }: ValueEditorProps) {
  const o = obj(value)
  const [source, setSource] = useState(String(o.source ?? ''))
  const [table, setTable] = useState(String(o.table ?? ''))
  return (
    <Shell busy={busy} onCancel={onCancel} ok={!!source.trim() && !!table.trim()}
           onSave={() => onSave({ source: source.trim(), table: table.trim() })}>
      <div className="grid grid-cols-2 gap-2">
        <Field label={BP_EDIT_TEXT.source}>{(p) => <input {...p} className="field" value={source} onChange={(e) => setSource(e.target.value)} />}</Field>
        <Field label={BP_EDIT_TEXT.table}>{(p) => <input {...p} className="field" value={table} onChange={(e) => setTable(e.target.value)} />}</Field>
      </div>
    </Shell>
  )
}

function DateEditor({ value, busy, onSave, onCancel }: ValueEditorProps) {
  const o = obj(value)
  const [column, setColumn] = useState(String(o.column ?? ''))
  const [rule, setRule] = useState(String(o.rule ?? ''))
  return (
    <Shell busy={busy} onCancel={onCancel} ok={!!column.trim()}
           onSave={() => onSave({ column: column.trim(), ...(rule.trim() ? { rule: rule.trim() } : {}) })}>
      <div className="grid grid-cols-2 gap-2">
        <Field label={BP_EDIT_TEXT.column}>{(p) => <input {...p} className="field" value={column} onChange={(e) => setColumn(e.target.value)} />}</Field>
        <Field label={BP_EDIT_TEXT.rule}>{(p) => <input {...p} className="field" value={rule} onChange={(e) => setRule(e.target.value)} />}</Field>
      </div>
    </Shell>
  )
}

function CadenceEditor({ value, busy, onSave, onCancel }: ValueEditorProps) {
  const o = obj(value)
  const [preset, setPreset] = useState<string>(o.preset ?? 'weekly')
  const [weekday, setWeekday] = useState<number>(Number(o.weekday) || 1)
  const [day, setDay] = useState<number>(Number(o.day) || 1)
  const [time, setTime] = useState<string>(o.time ?? '09:00')
  const [tz, setTz] = useState<string>(o.tz ?? 'Asia/Shanghai')
  const [period, setPeriod] = useState<string>(o.period ?? '')
  const save = () => onSave({
    preset, time, tz: tz.trim() || 'Asia/Shanghai',
    ...(preset === 'weekly' ? { weekday } : {}), ...(preset === 'monthly' ? { day } : {}),
    ...(period ? { period } : {}),
  })
  return (
    <Shell busy={busy} onCancel={onCancel} onSave={save} ok={/^\d{2}:\d{2}$/.test(time)}>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
        <Field label={BP_EDIT_TEXT.preset}>
          {(p) => (
            <select {...p} className="field" value={preset} onChange={(e) => setPreset(e.target.value)}>
              {Object.entries(CADENCE_PRESET_LABEL).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
            </select>
          )}
        </Field>
        {preset === 'weekly' && (
          <Field label={BP_EDIT_TEXT.weekday}>
            {(p) => (
              <select {...p} className="field" value={weekday} onChange={(e) => setWeekday(Number(e.target.value))}>
                {WEEKDAY_LABEL.slice(1).map((l, i) => <option key={l} value={i + 1}>{`周${l}`}</option>)}
              </select>
            )}
          </Field>
        )}
        {preset === 'monthly' && (
          <Field label={BP_EDIT_TEXT.day}>
            {(p) => <input {...p} type="number" min={1} max={28} className="field" value={day} onChange={(e) => setDay(Number(e.target.value))} />}
          </Field>
        )}
        <Field label={BP_EDIT_TEXT.time}>
          {(p) => <input {...p} type="time" className="field" value={time} onChange={(e) => setTime(e.target.value)} />}
        </Field>
        <Field label={BP_EDIT_TEXT.period}>
          {(p) => (
            <select {...p} className="field" value={period} onChange={(e) => setPeriod(e.target.value)}>
              <option value="">{BP_EDIT_TEXT.periodNone}</option>
              {Object.entries(PERIOD_LABEL).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
            </select>
          )}
        </Field>
        <Field label={BP_EDIT_TEXT.tz}>
          {(p) => <input {...p} className="field" value={tz} onChange={(e) => setTz(e.target.value)} />}
        </Field>
      </div>
    </Shell>
  )
}

/**
 * 处理人：人和组用选人框；「发起人自己」是一个勾，勾上就只派给发起人；模板留的岗位（发布时绑人）照留，选了具体的人、组就换掉
 * 岗位——服务端不收岗位和人混在一起（说不清这一步派给岗位还是派给这几个人）
 */
function PrincipalsEditor({ value, busy, onSave, onCancel }: ValueEditorProps) {
  const list = principals(value)
  const [picked, setPicked] = useState<PickerPrincipal[]>(
    list.filter((p) => p.kind === 'user' || p.kind === 'group').map((p) => ({ kind: p.kind as 'user' | 'group', name: p.name })))
  const [initiator, setInitiator] = useState(list.some((p) => p.kind === 'initiator'))
  const slots = list.filter((p) => p.kind === 'slot')
  const people = picked.filter((p) => p.kind !== 'everyone').map((p) => ({ kind: p.kind as 'user' | 'group', name: p.name }))
  const next = (): BpPrincipal[] => (initiator ? [{ kind: 'initiator', name: '' }] : people.length ? people : slots)
  return (
    <Shell busy={busy} onCancel={onCancel} onSave={() => onSave(next())} ok={next().length > 0}>
      <label className="flex items-center gap-1.5 text-xs">
        <input type="checkbox" checked={initiator} onChange={(e) => setInitiator(e.target.checked)} /> {BP_EDIT_TEXT.initiator}
      </label>
      {!initiator && <PrincipalPicker value={picked} onChange={setPicked} />}
      {!initiator && slots.length > 0 && (
        <p className="text-2xs text-faint">{people.length ? BP_EDIT_TEXT.slotReplaced : BP_EDIT_TEXT.slotKept(slots.map((x) => x.name).join('、'))}</p>
      )}
    </Shell>
  )
}

/** 授权对象（谁能看）：只收人和组 */
function GranteesEditor({ value, busy, onSave, onCancel }: ValueEditorProps) {
  const [picked, setPicked] = useState<PickerPrincipal[]>(principals(value).filter((p) => p.kind === 'user' || p.kind === 'group')
    .map((p) => ({ kind: p.kind as 'user' | 'group', name: p.name })))
  return (
    <Shell busy={busy} onCancel={onCancel}
           onSave={() => onSave(picked.filter((p) => p.kind !== 'everyone').map((p) => ({ kind: p.kind, name: p.name })))}>
      <PrincipalPicker value={picked} onChange={setPicked} />
    </Shell>
  )
}

function PolicyEditor({ value, busy, onSave, onCancel }: ValueEditorProps) {
  const o = obj(value)
  const [kind, setKind] = useState<string>(o.kind ?? 'any')
  const [m, setM] = useState<number>(Number(o.m) || 2)
  return (
    <Shell busy={busy} onCancel={onCancel} ok={kind !== 'quorum' || m >= 1}
           onSave={() => onSave(kind === 'quorum' ? { kind, m } : { kind })}>
      <div className="grid grid-cols-2 gap-2">
        <Field label={BP_EDIT_TEXT.policy}>
          {(p) => (
            <select {...p} className="field" value={kind} onChange={(e) => setKind(e.target.value)}>
              {Object.entries(POLICY_LABEL).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
            </select>
          )}
        </Field>
        {kind === 'quorum' && (
          <Field label={BP_EDIT_TEXT.quorum}>
            {(p) => <input {...p} type="number" min={1} className="field" value={m} onChange={(e) => setM(Number(e.target.value))} />}
          </Field>
        )}
      </div>
    </Shell>
  )
}

function DurationEditor({ value, busy, onSave, onCancel }: ValueEditorProps) {
  // 周折成天（服务端也收「1 周」）；分钟这一档卡片上不给改，按小时取整
  const match = /^P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?)?/.exec(String(value ?? ''))
  const [days, setDays] = useState<number>((Number(match?.[1]) || 0) * 7 + (Number(match?.[2]) || 0))
  const [hours, setHours] = useState<number>(Number(match?.[3]) || 0)
  return (
    <Shell busy={busy} onCancel={onCancel} ok={days > 0 || hours > 0} onSave={() => onSave(durationIso(days, hours))}>
      <div className="grid grid-cols-2 gap-2">
        <Field label={BP_EDIT_TEXT.days}>{(p) => <input {...p} type="number" min={0} className="field" value={days} onChange={(e) => setDays(Number(e.target.value))} />}</Field>
        <Field label={BP_EDIT_TEXT.hours}>{(p) => <input {...p} type="number" min={0} className="field" value={hours} onChange={(e) => setHours(Number(e.target.value))} />}</Field>
      </div>
    </Shell>
  )
}

function TimeoutEditor({ value, busy, onSave, onCancel }: ValueEditorProps) {
  const o = obj(value)
  const [action, setAction] = useState<string>(o.action ?? 'remind')
  const [to, setTo] = useState<PickerPrincipal[]>(principals(o.to).filter((p) => p.kind === 'user' || p.kind === 'group')
    .map((p) => ({ kind: p.kind as 'user' | 'group', name: p.name })))
  const ok = action !== 'escalate' || to.length > 0
  // 提醒间隔（只有提醒才有）：这里不改，原样留着
  const save = () => onSave(action === 'escalate' ? { action, to: to.map((p) => ({ kind: p.kind, name: p.name })) }
    : action === 'remind' && o.every ? { action, every: o.every } : { action })
  return (
    <Shell busy={busy} onCancel={onCancel} ok={ok} onSave={save}>
      <Field label={BP_EDIT_TEXT.timeoutAction}>
        {(p) => (
          <select {...p} className="field" value={action} onChange={(e) => setAction(e.target.value)}>
            {Object.entries(TIMEOUT_LABEL).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
          </select>
        )}
      </Field>
      {action === 'escalate' && <PrincipalPicker value={to} onChange={setTo} label={BP_EDIT_TEXT.timeoutTo} />}
    </Shell>
  )
}

function ThresholdEditor({ value, busy, onSave, onCancel, scope }: ValueEditorProps) {
  const o = obj(value)
  const fields = scope?.fields ?? []
  const [field, setField] = useState<string>(o.field ?? fields[0]?.name ?? '')
  const [op, setOp] = useState<string>(o.op ?? '>')
  const [raw, setRaw] = useState<string>(o.value == null ? '' : String(o.value))
  // 只许「字段 比较 常量」：常量是数就按数存
  const constant = raw.trim() !== '' && !Number.isNaN(Number(raw)) ? Number(raw) : raw.trim()
  return (
    <Shell busy={busy} onCancel={onCancel} ok={!!field && raw.trim() !== ''} onSave={() => onSave({ field, op, value: constant })}>
      <div className="grid grid-cols-3 gap-2">
        <Field label={BP_EDIT_TEXT.thresholdField}>
          {(p) => fields.length ? (
            <select {...p} className="field" value={field} onChange={(e) => setField(e.target.value)}>
              {fields.map((f) => <option key={f.name} value={f.name}>{f.title}</option>)}
            </select>
          ) : <input {...p} className="field" value={field} onChange={(e) => setField(e.target.value)} />}
        </Field>
        <Field label={BP_EDIT_TEXT.thresholdOp}>
          {(p) => (
            <select {...p} className="field" value={op} onChange={(e) => setOp(e.target.value)}>
              {Object.entries(THRESHOLD_OP_LABEL).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
            </select>
          )}
        </Field>
        <Field label={BP_EDIT_TEXT.thresholdValue}>{(p) => <input {...p} className="field" value={raw} onChange={(e) => setRaw(e.target.value)} />}</Field>
      </div>
    </Shell>
  )
}

function FieldEditor({ value, busy, onSave, onCancel }: ValueEditorProps) {
  const o = obj(value)
  const [title, setTitle] = useState<string>(o.title ?? '')
  const [kind, setKind] = useState<string>(fieldKindOf(o))
  const [options, setOptions] = useState<string>(Array.isArray(o.enum) ? o.enum.join('、') : '')
  const [required, setRequired] = useState<boolean>(o.required === true)
  const list = options.split(/[、,，]/).map((x) => x.trim()).filter(Boolean)
  return (
    <Shell busy={busy} onCancel={onCancel} ok={!!title.trim() && (kind !== 'select' || list.length > 0)}
           onSave={() => onSave({ ...fieldSpecOf(o, kind, title.trim(), list), required })}>
      <div className="grid grid-cols-2 gap-2">
        <Field label={BP_EDIT_TEXT.fieldTitle}>{(p) => <input {...p} className="field" value={title} onChange={(e) => setTitle(e.target.value)} />}</Field>
        <Field label={BP_EDIT_TEXT.fieldKind}>
          {(p) => (
            <select {...p} className="field" value={kind} onChange={(e) => setKind(e.target.value)}>
              {Object.entries(FIELD_KIND_LABEL).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
            </select>
          )}
        </Field>
      </div>
      {kind === 'select' && (
        <Field label={BP_EDIT_TEXT.fieldOptions}>{(p) => <input {...p} className="field" value={options} onChange={(e) => setOptions(e.target.value)} />}</Field>
      )}
      <label className="flex items-center gap-1.5 text-xs">
        <input type="checkbox" checked={required} onChange={(e) => setRequired(e.target.checked)} data-bp-required="" /> {BP_EDIT_TEXT.required}
      </label>
    </Shell>
  )
}

function BoolEditor({ value, busy, onSave, onCancel }: ValueEditorProps) {
  const [on, setOn] = useState(!!value)
  return (
    <Shell busy={busy} onCancel={onCancel} onSave={() => onSave(on)}>
      <label className="flex items-center gap-1.5 text-xs">
        <input type="checkbox" checked={on} onChange={(e) => setOn(e.target.checked)} /> {on ? BP_EDIT_TEXT.yes : BP_EDIT_TEXT.no}
      </label>
    </Shell>
  )
}

type MapKind = 'form' | 'metric' | 'rows' | 'const'

/** 字段对应的一个取值 → [种类, 名字或常量] */
function mapSourceOf(source: unknown): [MapKind, string] {
  if (typeof source === 'number') return ['const', String(source)]
  const text = String(source ?? '')
  const m = /^(form|metric)\.(.+)$/.exec(text)
  if (m) return [m[1] as MapKind, m[2]]
  if (text === 'rows') return ['rows', '']
  return ['const', /^'.*'$/.test(text) ? text.slice(1, -1) : text]
}

/** [种类, 名字或常量] → 服务端认的取值；常量是数就按数，文字加单引号 */
function mapSourceValue(kind: MapKind, v: string): unknown {
  if (kind === 'rows') return 'rows'
  if (kind === 'form' || kind === 'metric') return `${kind}.${v}`
  return v.trim() !== '' && !Number.isNaN(Number(v)) ? Number(v) : `'${v}'`
}

/** 字段对应：目标系统的每个参数取哪个表单字段、指标、核对出来的行，或一个常量 */
function MappingEditor({ value, busy, onSave, onCancel, scope }: ValueEditorProps) {
  type Row = { param: string; kind: MapKind; v: string }
  const [rows, setRows] = useState<Row[]>(() => {
    const entries = Object.entries(obj(value)).map(([param, src]) => { const [kind, v] = mapSourceOf(src); return { param, kind, v } })
    return entries.length ? entries : [{ param: '', kind: 'form', v: scope?.fields[0]?.name ?? '' }]
  })
  const set = (i: number, patch: Partial<Row>) => setRows((rs) => rs.map((r, k) => (k === i ? { ...r, ...patch } : r)))
  const done = rows.filter((r) => r.param.trim() && (r.kind === 'rows' || r.v.trim()))
  const options = (kind: MapKind) => (kind === 'form' ? (scope?.fields ?? []).map((f) => ({ id: f.name, title: f.title }))
    : kind === 'metric' ? scope?.metrics ?? [] : [])
  return (
    <Shell busy={busy} onCancel={onCancel} ok={done.length > 0}
           onSave={() => onSave(Object.fromEntries(done.map((r) => [r.param.trim(), mapSourceValue(r.kind, r.v.trim())])))}>
      {rows.map((r, i) => (
        <div key={i} className="flex flex-wrap items-center gap-1.5" data-bp-map-row={i}>
          <input className="field min-w-0 flex-1" aria-label={BP_EDIT_TEXT.mappingParam} placeholder={BP_EDIT_TEXT.mappingParam}
                 value={r.param} onChange={(e) => set(i, { param: e.target.value })} />
          <span aria-hidden className="text-faint">←</span>
          <select className="field w-auto" aria-label={BP_EDIT_TEXT.mappingFrom} value={r.kind}
                  onChange={(e) => {
                    const kind = e.target.value as MapKind
                    set(i, { kind, v: options(kind)[0]?.id ?? '' })
                  }}>
            {(Object.keys(MAP_KIND_LABEL) as MapKind[]).map((k) => <option key={k} value={k}>{MAP_KIND_LABEL[k]}</option>)}
          </select>
          {(r.kind === 'form' || r.kind === 'metric') && (options(r.kind).length ? (
            <select className="field w-auto" aria-label={MAP_KIND_LABEL[r.kind]} value={r.v} onChange={(e) => set(i, { v: e.target.value })}>
              {options(r.kind).map((o) => <option key={o.id} value={o.id}>{o.title}</option>)}
            </select>
          ) : <input className="field min-w-0 flex-1" aria-label={MAP_KIND_LABEL[r.kind]} value={r.v} onChange={(e) => set(i, { v: e.target.value })} />)}
          {r.kind === 'const' && (
            <input className="field min-w-0 flex-1" aria-label={MAP_KIND_LABEL.const} value={r.v} onChange={(e) => set(i, { v: e.target.value })} />
          )}
          <button type="button" className="btn btn-xs btn-ghost" aria-label={BLUEPRINT_TEXT.remove}
                  onClick={() => setRows((rs) => rs.filter((_, k) => k !== i))}><X size={11} aria-hidden /></button>
        </div>
      ))}
      <button type="button" className="btn btn-xs btn-ghost"
              onClick={() => setRows((rs) => [...rs, { param: '', kind: 'form', v: scope?.fields[0]?.name ?? '' }])}>
        <Plus size={11} aria-hidden /> {BP_EDIT_TEXT.mappingAdd}
      </button>
    </Shell>
  )
}

const MAP_KIND_LABEL: Record<MapKind, string> = {
  form: BP_EDIT_TEXT.mapForm, metric: BP_EDIT_TEXT.mapMetric, rows: BP_EDIT_TEXT.mapRows, const: BP_EDIT_TEXT.mapConst,
}
