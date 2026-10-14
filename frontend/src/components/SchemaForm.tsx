/**
 * 表单渲染器：agentlab.form/1（应用平台 §3.3，X2「只有一个表单渲染器」）。
 *
 * 用在四处：画布和版本页的发起表单（替换 RunControl 的 FieldsForm）、人工节点、待办、应用入口。
 * 前端只校验目录规定的子集（lib/schemaForm.ts），后端复核、以后端为准；两边的错误格式都是 {path, code, message}。
 *
 * 契约骨架里的最简实现：每个字段一个文本框。控件、校验、错误展示由 FE-5 补齐，props 不变。
 */
import type { FormError, FormSchema } from '../typesApp'

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
}

export function SchemaForm({ schema, value, onChange, errors, readOnly, idPrefix = 'sf' }: SchemaFormProps) {
  const order = schema['x-order'] ?? Object.keys(schema.properties)
  return (
    <div>
      {order.filter((k) => schema.properties[k]).map((key) => {
        const field = schema.properties[key]
        const id = `${idPrefix}-${key}`
        const error = errors?.find((e) => e.path === key)
        return (
          <div key={key} className="mb-2.5">
            <label className="label" htmlFor={id}>{field.title ?? key}</label>
            <input
              id={id}
              className="field"
              readOnly={readOnly || field.readOnly}
              value={value[key] == null ? '' : String(value[key])}
              onChange={(e) => onChange({ ...value, [key]: e.target.value })}
            />
            {error && <p className="mt-1 text-xs text-[var(--err)]">{error.message}</p>}
          </div>
        )
      })}
    </div>
  )
}
