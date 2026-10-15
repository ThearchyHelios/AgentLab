/**
 * 表单渲染器：agentlab.form/1（应用平台 §3.3，X2「只有一个表单渲染器」）。
 *
 * 用在四处：画布和版本页的发起表单（替换 RunControl 的 FieldsForm）、人工节点、待办、应用入口。
 * 前端只校验目录规定的子集（lib/schemaForm.ts），后端复核、以后端为准；两边的错误格式都是 {path, code, message}，
 * 按 path 挂到字段下面。对不上任何字段的错误（服务端说的是整张表，或者字段名对不上）照实列在表单顶上，不吞掉。
 *
 * 值的类型和表单声明的一致：数字字段存数（输入途中的「1.」这类半截写法留在框里，不回写成别的数），日期时间存带本地
 * 时区的 RFC 3339，多选存字符串数组，文件存上传后拿回的引用（FileInput）。没填的不进值（undefined），必填由校验拦。
 * 文件要先上传：上传的上下文（应用的入口、待办、蓝图）由用表单的地方给（fileContext），不给的地方不能上传。
 */
import { useEffect, useId, useState } from 'react'
import type { CSSProperties, KeyboardEvent as ReactKeyboardEvent, ReactNode } from 'react'
import clsx from 'clsx'
import type { FormError, FormField, FormSchema } from '../typesApp'
import { FORM_TEXT } from '../lib/termsApp'
import {
  fieldOfPath, fieldOrder, fileLimitsOf, localToRfc3339, optionsOf, rfc3339ToLocal, widgetOf,
} from '../lib/schemaForm'
import { matchShortcut } from '../lib/keys'
import { useAtLeast } from '../store/auth'
import { JsonInput, isComposing } from './ui'
import { PrincipalPicker } from './PrincipalPicker'
import { FileInput } from './FileInput'

export interface SchemaFormProps {
  schema: FormSchema
  value: Record<string, unknown>
  onChange: (value: Record<string, unknown>) => void
  /** 服务端（422）或本地校验出的错误，按 path 挂到字段下面 */
  errors?: FormError[]
  readOnly?: boolean
  /** Mod+Enter 提交 */
  onSubmit?: () => void
  /** 控件 id 的前缀，同一页有多份表单时避免撞 id */
  idPrefix?: string
  /** 文件控件上传的上下文：{app_id, entry}（入口）、{task_id}（待办）、{blueprint_id}（试运行）。不给就不能上传 */
  fileContext?: Record<string, string>
}

export function SchemaForm({ schema, value, onChange, errors, readOnly, onSubmit, idPrefix, fileContext }: SchemaFormProps) {
  const auto = useId().replace(/[^a-zA-Z0-9_-]/g, '')
  const prefix = idPrefix ?? `sf${auto}`
  const order = fieldOrder(schema)
  const required = new Set(schema.required ?? [])
  const byField = new Map<string, FormError>()
  const loose: FormError[] = []
  for (const e of errors ?? []) {
    const key = fieldOfPath(e.path)
    if (key && schema.properties[key]) {
      if (!byField.has(key)) byField.set(key, e)
    } else loose.push(e)
  }
  const set = (key: string, v: unknown) => {
    const next = { ...value }
    if (v === undefined) delete next[key]
    else next[key] = v
    onChange(next)
  }
  const onKeyDown = (e: ReactKeyboardEvent) => {
    if (!onSubmit || isComposing(e) || !matchShortcut(e, 'Mod+Enter')) return
    e.preventDefault()
    onSubmit()
  }

  return (
    <div className="space-y-3" data-schema-form="" onKeyDown={onKeyDown}>
      {schema.title && <div className="text-xs font-semibold text-fg">{schema.title}</div>}
      {loose.length > 0 && (
        <div role="alert" className="rounded-md border px-2.5 py-1.5 text-2xs leading-relaxed text-[var(--err)]"
             data-form-errors=""
             style={{ borderColor: 'color-mix(in srgb, var(--err) 35%, var(--border))', background: 'color-mix(in srgb, var(--err) 6%, transparent)' }}>
          <div className="font-medium">{FORM_TEXT.errorsHead(loose.length)}</div>
          <ul className="mt-0.5 list-disc pl-4">
            {loose.map((e, i) => (
              <li key={i}>{e.path ? `${FORM_TEXT.unknownField(e.path)}${e.message}` : e.message}</li>
            ))}
          </ul>
        </div>
      )}
      {order.map((key) => (
        <FieldRow key={key} id={`${prefix}-${key}`} name={key} field={schema.properties[key]}
                  required={required.has(key)} value={value[key]} error={byField.get(key)}
                  readOnly={readOnly || !!schema.properties[key].readOnly} onChange={(v) => set(key, v)}
                  fileContext={fileContext} />
      ))}
    </div>
  )
}

function FieldRow({ id, name, field, required, value, error, readOnly, onChange, fileContext }: {
  id: string; name: string; field: FormField; required: boolean; value: unknown; error?: FormError
  readOnly: boolean; onChange: (v: unknown) => void; fileContext?: Record<string, string>
}) {
  const widget = widgetOf(field)
  const title = field.title || name
  const hintId = `${id}-hint`
  const errorId = `${id}-error`
  const describedBy = error ? errorId : field.description ? hintId : undefined
  const aria = { id, 'aria-describedby': describedBy, 'aria-invalid': error ? true : undefined,
    'aria-required': required || undefined }

  if (widget === 'note') {
    return (
      <div className="rounded-md border border-dashed px-2.5 py-1.5 text-xs leading-relaxed text-dim" data-field={name}
           data-widget="note">
        {field.title && <div className="font-medium text-fg">{field.title}</div>}
        {field.description && <div className="whitespace-pre-wrap">{field.description}</div>}
      </div>
    )
  }

  // 单选组、多选组、单个勾选框自带文字，用 fieldset / label 包起来；其余是「标题 + 一个控件」
  const group = widget === 'radio' || widget === 'multiselect'
  const label: ReactNode = (
    <>
      {title}
      {required && <span className="ml-0.5 text-[var(--err)]" aria-hidden>*</span>}
    </>
  )
  const foot = error
    ? <div id={errorId} className="mt-1 text-2xs leading-relaxed text-[var(--err)]" data-field-error={error.code}>{error.message}</div>
    : field.description
      ? <div id={hintId} className="mt-1 text-2xs leading-relaxed text-faint">{field.description}</div>
      : null

  if (group) {
    return (
      <fieldset className="min-w-0" data-field={name} data-widget={widget} aria-describedby={describedBy}
                aria-invalid={error ? true : undefined}>
        <legend className="label">{label}</legend>
        {widget === 'radio'
          ? <RadioGroup name={id} field={field} value={value} readOnly={readOnly} onChange={onChange} />
          : <MultiSelect name={id} field={field} value={value} readOnly={readOnly} onChange={onChange} />}
        {foot}
      </fieldset>
    )
  }
  if (widget === 'principal') {
    // 选人控件自己有输入框和标签，外层不再用 label 指着一个 id
    return (
      <div className="min-w-0" data-field={name} data-widget="principal" aria-describedby={describedBy}>
        <div className="label">{label}</div>
        {readOnly
          ? <div className="text-xs text-fg">{typeof value === 'string' && value ? value : '—'}</div>
          : <PrincipalPicker single label={title}
                             value={typeof value === 'string' && value ? [{ kind: 'user', name: value }] : []}
                             onChange={(list) => onChange(list[0]?.name || undefined)} />}
        {foot}
      </div>
    )
  }
  if (widget === 'file') {
    // 文件控件自己有按钮和链接：标题指着它的主按钮（id），限制和「谁能下载」写在控件下面
    return (
      <div className="min-w-0" data-field={name} data-widget="file">
        <label className="label" htmlFor={id}>{label}</label>
        <FileInput id={id} label={title} value={value} readOnly={readOnly} context={fileContext} limits={fileLimitsOf(field)}
                   invalid={!!error} describedBy={describedBy} onChange={onChange} />
        {foot}
      </div>
    )
  }
  if (widget === 'checkbox') {
    return (
      <div className="min-w-0" data-field={name} data-widget="checkbox">
        <label className="flex items-center gap-2 text-xs text-fg">
          <input type="checkbox" {...aria} checked={value === true} disabled={readOnly}
                 onChange={(e) => onChange(e.target.checked)} />
          <span>{label}</span>
        </label>
        {foot}
      </div>
    )
  }
  return (
    <div className="min-w-0" data-field={name} data-widget={widget}>
      <label className="label" htmlFor={id}>{label}</label>
      <Control widget={widget} field={field} value={value} readOnly={readOnly} aria={aria} invalid={!!error}
               onChange={onChange} />
      {foot}
    </div>
  )
}

type Aria = { id: string; 'aria-describedby'?: string; 'aria-invalid'?: boolean; 'aria-required'?: boolean }

function Control({ widget, field, value, readOnly, aria, invalid, onChange }: {
  widget: string; field: FormField; value: unknown; readOnly: boolean; aria: Aria; invalid: boolean
  onChange: (v: unknown) => void
}) {
  const builder = useAtLeast('builder')
  const style = invalid ? { borderColor: 'var(--err)' } : undefined
  const text = (v: string) => onChange(v === '' ? undefined : v)
  switch (widget) {
    case 'textarea':
      return <textarea className="field" rows={4} {...aria} style={style} readOnly={readOnly}
                       value={typeof value === 'string' ? value : value == null ? '' : String(value)}
                       maxLength={field.maxLength} onChange={(e) => text(e.target.value)} />
    case 'number':
    case 'integer':
      return <NumberInput aria={aria} style={style} readOnly={readOnly} integer={widget === 'integer'}
                          min={field.minimum} max={field.maximum} value={value} onChange={onChange} />
    case 'date':
      return <input type="date" className="field" {...aria} style={style} readOnly={readOnly}
                    value={typeof value === 'string' ? value : ''} onChange={(e) => text(e.target.value)} />
    case 'datetime':
      return <input type="datetime-local" className="field" {...aria} style={style} readOnly={readOnly}
                    value={rfc3339ToLocal(value)} onChange={(e) => text(localToRfc3339(e.target.value))} />
    case 'select': {
      const options = optionsOf(field)
      const at = options.findIndex((o) => o.value === value)
      return (
        <select className="field" {...aria} style={style} disabled={readOnly} value={at >= 0 ? String(at) : ''}
                onChange={(e) => onChange(e.target.value === '' ? undefined : options[Number(e.target.value)]?.value)}>
          <option value="">{FORM_TEXT.choose}</option>
          {options.map((o, i) => <option key={i} value={String(i)}>{o.label}</option>)}
        </select>
      )
    }
    case 'json':
      // JSON 只给搭建者以上填（§3.3）：使用者看得到值，改不了
      return builder && !readOnly
        ? <JsonInput {...aria} value={value} onChange={onChange} />
        : (
          <div>
            <pre className="mono max-h-40 overflow-auto rounded border bg-bg p-2 text-2xs text-dim">
              {value === undefined ? '' : JSON.stringify(value, null, 2)}
            </pre>
            {!builder && <div className="mt-1 text-2xs text-faint">{FORM_TEXT.jsonBuilderOnly}</div>}
          </div>
        )
    default: {
      const type = field.format === 'email' ? 'email' : field.format === 'uri' ? 'url' : 'text'
      return <input type={type} className="field" {...aria} style={style} readOnly={readOnly}
                    value={typeof value === 'string' ? value : value == null ? '' : String(value)}
                    maxLength={field.maxLength} onChange={(e) => text(e.target.value)} />
    }
  }
}

/**
 * 数字框：框里的字自己留一份。「1.」「-」「1e」这些输入途中的半截写法不能被回写成 1，否则小数点打不进去；
 * 能读成数的交出数，读不成的原样交出字符串，由校验说「请输入数字」
 */
function NumberInput({ aria, style, readOnly, integer, min, max, value, onChange }: {
  aria: Aria; style?: CSSProperties; readOnly: boolean; integer: boolean; min?: number; max?: number
  value: unknown; onChange: (v: unknown) => void
}) {
  const shown = value == null ? '' : String(value)
  const [text, setText] = useState(shown)
  // 外面换了值（重置表单、填默认值）才跟上；自己打字引起的变化不回写
  useEffect(() => {
    setText((cur) => (parse(cur) === value || (cur === '' && value == null) ? cur : shown))
    // shown 由 value 决定
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value])
  return (
    <input type="text" inputMode={integer ? 'numeric' : 'decimal'} className="field tnum" {...aria} style={style}
           readOnly={readOnly} value={text} aria-valuemin={min} aria-valuemax={max}
           onChange={(e) => {
             setText(e.target.value)
             onChange(e.target.value.trim() === '' ? undefined : parse(e.target.value))
           }} />
  )
}

/** 去掉千分位逗号和空白后能整个读成有限数就是数，读不成原样返回 */
function parse(text: string): unknown {
  const t = text.replace(/[,，\s]/g, '')
  if (!/^[-+]?(\d+\.?\d*|\.\d+)(e[-+]?\d+)?$/i.test(t)) return text
  const n = Number(t)
  return Number.isFinite(n) ? n : text
}

function RadioGroup({ name, field, value, readOnly, onChange }: {
  name: string; field: FormField; value: unknown; readOnly: boolean; onChange: (v: unknown) => void
}) {
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1.5">
      {optionsOf(field).map((o, i) => (
        <label key={i} className={clsx('flex items-center gap-1.5 text-xs', readOnly ? 'text-dim' : 'text-fg')}>
          <input type="radio" name={name} checked={value === o.value} disabled={readOnly}
                 onChange={() => onChange(o.value)} />
          {o.label}
        </label>
      ))}
    </div>
  )
}

function MultiSelect({ name, field, value, readOnly, onChange }: {
  name: string; field: FormField; value: unknown; readOnly: boolean; onChange: (v: unknown) => void
}) {
  const picked = Array.isArray(value) ? value : []
  const options = optionsOf(field)
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1.5">
      {options.map((o, i) => {
        const v = String(o.value)
        const on = picked.includes(v)
        return (
          <label key={i} className={clsx('flex items-center gap-1.5 text-xs', readOnly ? 'text-dim' : 'text-fg')}>
            <input type="checkbox" name={name} checked={on} disabled={readOnly}
                   onChange={() => {
                     // 按选项的顺序交出，不按点的先后：同样的选择永远是同一个值
                     const next = options.map((x) => String(x.value)).filter((x) => (x === v ? !on : picked.includes(x)))
                     onChange(next.length ? next : undefined)
                   }} />
            {o.label}
          </label>
        )
      })}
    </div>
  )
}
