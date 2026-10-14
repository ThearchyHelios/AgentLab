/**
 * 输入节点的字段 → agentlab.form/1 表单（X2：只有一个表单渲染器）。
 *
 * 正式运行的表单由服务端转好放在版本详情里（VersionDetailOut.input_schema，服务端 app/apps/forms.input_schema）；
 * 画布试运行用的是画布上还没保存的入口字段，只能在这里按同一套规则转：没写类型的按文本，可选值变成下拉，
 * 说明、默认值照搬。两处规则对不上时以服务端为准——发起时服务端照版本快照校验，这里只决定怎么画。
 */
import type { FormField, FormSchema } from '../typesApp'

/** 输入节点的一个字段（canvas/nodeDefs 的 ioFields；type / enum / format / 上下限是应用平台新加的，老图没有） */
export interface InputField {
  name: string
  required?: boolean
  description?: string
  default?: unknown
  example?: unknown
  type?: FormField['type']
  enum?: (string | number)[]
  format?: FormField['format']
  /** 数字类型的上下限（闭区间，表单子集没有 exclusiveMinimum：「大于 0」的金额写 0.01） */
  minimum?: number
  maximum?: number
}

const TYPES = new Set(['string', 'number', 'integer', 'boolean', 'array'])
const FORMATS = new Set(['date', 'date-time', 'email', 'uri'])

export function inputSchemaOf(fields: InputField[]): FormSchema {
  const properties: Record<string, FormField> = {}
  const required: string[] = []
  for (const f of fields) {
    if (!f?.name) continue
    const type = f.type && TYPES.has(f.type) ? f.type : 'string'
    const options = Array.isArray(f.enum) && f.enum.length ? f.enum : null
    const field: FormField = { type, title: f.name }
    if (f.description) field.description = f.description
    if (f.default != null && f.default !== '') field.default = f.default
    if (type === 'array') {
      // 数组只能装枚举字符串（多选），没给可选值的照旧当一段文本填：老图里写 JSON 数组的字段不至于填不进去
      if (options) field.items = { type: 'string', enum: options.map(String) }
      else field.type = 'string'
    } else if (options) {
      field.enum = options
    }
    if (type === 'string' && f.format && FORMATS.has(f.format)) field.format = f.format
    if (type === 'number' || type === 'integer') {
      if (typeof f.minimum === 'number' && Number.isFinite(f.minimum)) field.minimum = f.minimum
      if (typeof f.maximum === 'number' && Number.isFinite(f.maximum)) field.maximum = f.maximum
    }
    // 老图的字段都是文本，多行框更好填（以前每个字段都是两行的文本框）
    if (field.type === 'string' && !field.enum && !field.format) field['x-widget'] = 'textarea'
    properties[f.name] = field
    if (f.required) required.push(f.name)
  }
  return { format: 'agentlab.form/1', type: 'object', properties, required, 'x-order': Object.keys(properties) }
}

/**
 * 表单里填的值 → 发起运行的 input。留空的按默认值（占位里写着「默认：…」，照它说的办）；没写类型的文本看起来像
 * JSON 就按 JSON 传，让老图里数组、对象类型的输入照旧能用
 */
export function inputPayload(schema: FormSchema, values: Record<string, unknown>): Record<string, unknown> {
  const payload: Record<string, unknown> = {}
  for (const [name, field] of Object.entries(schema.properties)) {
    const typed = values[name]
    const blank = typed == null || (typeof typed === 'string' && typed.trim() === '')
    if (blank && field.default == null && (field.type === 'number' || field.type === 'integer')) continue // 没填的数字不传，不传空串
    const raw = blank ? field.default ?? (field.type === 'array' ? [] : field.type === 'boolean' ? false : '') : typed
    if (field.type === 'string' && !field.enum && typeof raw === 'string' && /^\s*[[{]/.test(raw)) {
      try { payload[name] = JSON.parse(raw) } catch { payload[name] = raw }
    } else {
      payload[name] = raw
    }
  }
  return payload
}
