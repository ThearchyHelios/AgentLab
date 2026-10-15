/**
 * 表单 agentlab.form/1 的前端那一半（应用平台 §3.3）：字段顺序、控件、默认值、子集校验。
 *
 * 前端只校验目录规定的子集，后端用 jsonschema 复核、以后端为准；两边的错误格式都是 {path, code, message}，
 * 服务端 422 回来的错误和这里算出的错误挂到同一个位置。不支持 pattern：JS 和 Python 的正则语义不同，前端放行、
 * 后端拒收的情形就是从这里来的（P5）。
 *
 * 只有一层：顶层 object，字段是 string / number / integer / boolean，数组只装枚举字符串（多选）。
 *
 * 文件（normal-user-apps N9、§3.8）：字段 type 为 object、x-widget 为 file，只有这个控件能用 object。值是上传后拿回的
 * 引用 {file_id, name, size, sha256, type}，规则和服务端 apps/forms.py 同一份：必填，形状是文件引用，不合时错误的 code 是
 * file（同服务端 files/store.FILE_ERROR_CODE）。允许的类型和大小写在 x-file（accept、max_mb），只在上传时用，提交时不再查
 * （上传那一步服务端已经核对过）。
 */
import type { FileLimits, FormError, FormField, FormSchema, FormWidget } from '../typesApp'
import type { FileRef } from '../typesBlueprint'
import { FILE_TEXT, FORM_TEXT } from './termsApp'

/**
 * 表单字段上的 x-file（FormField['x-file']）整理成控件用的样子：可选类型去掉空的，大小上限不是正数的当没写。
 * 服务端保存表单时已经按同一套规则查过（apps/forms._file_field_problems），这里只防手写的老表单
 */
export function fileLimitsOf(field: FormField): FileLimits {
  const x = field['x-file']
  if (!x || typeof x !== 'object') return {}
  // 画布上手写的老图可能把可选类型写成一串逗号分隔的字
  const raw: unknown = x.accept
  const accept = Array.isArray(raw) ? raw.filter((a): a is string => typeof a === 'string' && !!a.trim())
    : typeof raw === 'string' ? raw.split(',').map((a) => a.trim()).filter(Boolean) : undefined
  return {
    accept: accept?.length ? accept : undefined,
    max_mb: typeof x.max_mb === 'number' && Number.isFinite(x.max_mb) && x.max_mb > 0 ? x.max_mb : undefined,
  }
}

/** 是不是上传后拿回的文件引用：五个键（同服务端 app/apps/spec.FILE_REF_KEYS）都在、类型对 */
export function isFileRef(v: unknown): v is FileRef {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return false
  const r = v as Record<string, unknown>
  return typeof r.file_id === 'string' && !!r.file_id && typeof r.name === 'string' && typeof r.sha256 === 'string'
    && typeof r.type === 'string' && typeof r.size === 'number' && Number.isFinite(r.size) && r.size >= 0
}

/** 上传接口的回包 → 表单值：只留引用的五个键，服务端另带的不进运行输入 */
export function toFileRef(raw: FileRef): FileRef {
  return { file_id: raw.file_id, name: raw.name, size: raw.size, sha256: raw.sha256, type: raw.type }
}

/** 表单引用的写法（同服务端 apps/tree.FORM_REF_PREFIX）：{"$ref": "#/forms/<名字>"} */
const FORM_REF_PREFIX = '#/forms/'

/** 内联的表单原样交回；引用换成 forms 里的那一份；引用不到的是 null（同服务端 apps/tree.resolve_form） */
export function resolveForm(forms: Record<string, FormSchema> | null | undefined,
  form: FormSchema | { $ref: string } | null | undefined): FormSchema | null {
  if (!form) return null
  if (!('$ref' in form)) return form
  return form.$ref.startsWith(FORM_REF_PREFIX) ? forms?.[form.$ref.slice(FORM_REF_PREFIX.length)] ?? null : null
}

/** 字段顺序：x-order 里写到的在前（按写的顺序），没写到的按 properties 的原顺序跟在后面；不存在的名字跳过 */
export function fieldOrder(schema: FormSchema): string[] {
  const props = schema.properties ?? {}
  const listed = (schema['x-order'] ?? []).filter((k, i, all) => k in props && all.indexOf(k) === i)
  return [...listed, ...Object.keys(props).filter((k) => !listed.includes(k))]
}

/** 这个字段画成哪种控件：写了 x-widget 照它，没写按类型推 */
export function widgetOf(field: FormField): FormWidget {
  if (field['x-widget']) return field['x-widget']
  // 只有文件控件用 object
  if (field.type === 'object') return 'file'
  switch (field.type) {
    case 'boolean': return 'checkbox'
    case 'array': return 'multiselect'
    case 'integer': return field.enum?.length ? 'select' : 'integer'
    case 'number': return field.enum?.length ? 'select' : 'number'
    default:
      if (field.enum?.length) return 'select'
      if (field.format === 'date') return 'date'
      if (field.format === 'date-time') return 'datetime'
      return (field.maxLength ?? 0) > 200 ? 'textarea' : 'text'
  }
}

/** 选项：值和显示的字。x-enum-labels 和 enum 按位置对应，缺的用值本身 */
export function optionsOf(field: FormField): { value: string | number; label: string }[] {
  const values = field.type === 'array' ? field.items?.enum ?? field.enum ?? [] : field.enum ?? []
  const labels = field['x-enum-labels'] ?? []
  return values.map((v, i) => ({ value: v, label: labels[i] ?? String(v) }))
}

/** 只读说明（note）不是值，不进提交的内容，也不校验 */
export const isNote = (field: FormField) => widgetOf(field) === 'note'

/** 初始值：各字段的 default；已经有值的不覆盖 */
export function withDefaults(schema: FormSchema, value: Record<string, unknown>): Record<string, unknown> {
  const out = { ...value }
  for (const [key, field] of Object.entries(schema.properties ?? {})) {
    if (out[key] === undefined && field.default !== undefined && !isNote(field)) out[key] = field.default
  }
  return out
}

/** 必填字段的空：没给、空串、空数组。布尔的 false 是一个值 */
const isEmpty = (v: unknown) => v === undefined || v === null || (typeof v === 'string' && v.trim() === '')
  || (Array.isArray(v) && v.length === 0)

const DATE_RE = /^(\d{4})-(\d{2})-(\d{2})$/
// RFC 3339：日期、时间，带时区（Z 或 ±hh:mm）。datetime 控件交出来的值总带本地时区
const DATE_TIME_RE = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$/i
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/
const URI_RE = /^[a-z][a-z0-9+.-]*:\S+$/i

function validDate(text: string): boolean {
  const m = DATE_RE.exec(text)
  if (!m) return false
  const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]))
  return d.getUTCFullYear() === +m[1] && d.getUTCMonth() === +m[2] - 1 && d.getUTCDate() === +m[3]
}

function formatOk(format: string, text: string): boolean {
  switch (format) {
    case 'date': return validDate(text)
    case 'date-time': {
      const m = DATE_TIME_RE.exec(text)
      return !!m && validDate(m[1]) && +m[2] < 24 && +m[3] < 60
    }
    case 'email': return EMAIL_RE.test(text)
    case 'uri': return URI_RE.test(text)
    default: return true
  }
}

/** JSON Schema 的长度按码点数，不按 UTF-16 单元：一个表情算一个字 */
const lengthOf = (s: string) => Array.from(s).length

/**
 * 子集校验：返回 {path, code, message}，path 是字段名（只有一层）。顺序同字段顺序，每个字段最多一条——先说最要紧的
 * （没填、类型不对），改好了再说下一条，不一次堆三句
 */
export function validateForm(schema: FormSchema, values: Record<string, unknown>): FormError[] {
  const errors: FormError[] = []
  const required = new Set(schema.required ?? [])
  for (const key of fieldOrder(schema)) {
    const field = schema.properties[key]
    if (isNote(field)) continue
    const error = fieldError(field, values[key], required.has(key))
    if (error) errors.push({ path: key, ...error })
  }
  return errors
}

function fieldError(field: FormField, v: unknown, required: boolean): Omit<FormError, 'path'> | null {
  if (isEmpty(v)) return required ? { code: 'required', message: FORM_TEXT.required } : null
  if (widgetOf(field) === 'file') return isFileRef(v) ? null : { code: 'file', message: FILE_TEXT.invalid }
  switch (field.type) {
    case 'string': {
      if (typeof v !== 'string') return { code: 'type', message: FORM_TEXT.type.string }
      if (field.enum && !field.enum.includes(v)) return { code: 'enum', message: FORM_TEXT.enum }
      if (field.minLength != null && lengthOf(v) < field.minLength) {
        return { code: 'min_length', message: FORM_TEXT.minLength(field.minLength) }
      }
      if (field.maxLength != null && lengthOf(v) > field.maxLength) {
        return { code: 'max_length', message: FORM_TEXT.maxLength(field.maxLength) }
      }
      if (field.format && !formatOk(field.format, v)) {
        return { code: 'format', message: FORM_TEXT.format[field.format] ?? FORM_TEXT.type.string }
      }
      return null
    }
    case 'number':
    case 'integer': {
      if (typeof v !== 'number' || !Number.isFinite(v) || (field.type === 'integer' && !Number.isInteger(v))) {
        return { code: 'type', message: FORM_TEXT.type[field.type] }
      }
      if (field.enum && !field.enum.includes(v)) return { code: 'enum', message: FORM_TEXT.enum }
      if (field.minimum != null && v < field.minimum) return { code: 'minimum', message: FORM_TEXT.minimum(field.minimum) }
      if (field.maximum != null && v > field.maximum) return { code: 'maximum', message: FORM_TEXT.maximum(field.maximum) }
      return null
    }
    case 'boolean':
      return typeof v === 'boolean' ? null : { code: 'type', message: FORM_TEXT.type.boolean }
    case 'array': {
      if (!Array.isArray(v) || v.some((x) => typeof x !== 'string')) return { code: 'type', message: FORM_TEXT.type.array }
      const allowed = field.items?.enum ?? field.enum
      if (allowed && v.some((x) => !allowed.includes(x))) return { code: 'enum', message: FORM_TEXT.enum }
      if (field.maxItems != null && v.length > field.maxItems) {
        return { code: 'max_items', message: FORM_TEXT.maxItems(field.maxItems) }
      }
      return null
    }
    default:
      return null
  }
}

/** 提交的内容：去掉没填的（undefined、空串）和只读说明；必填没填的交给校验拦 */
export function cleanValues(schema: FormSchema, values: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const [key, field] of Object.entries(schema.properties ?? {})) {
    const v = values[key]
    if (isNote(field) || v === undefined || (typeof v === 'string' && v === '')) continue
    out[key] = v
  }
  return out
}

/**
 * 服务端的 path 可能写成 amount、/amount、values.amount、values/amount（jsonschema 的 absolute_path 拼出来的样子
 * 各有不同）：统一成字段名。认不出的原样返回，界面上列在表单顶上
 */
export function fieldOfPath(path: string): string {
  return path.replace(/^\/+/, '').replace(/^values[./]/, '').split(/[./]/)[0] ?? ''
}

/**
 * 取 422 的表单错误：传 ApiError 时认它原样的响应体（服务端 {detail, errors:[…]} 里的那一组），传别的按 {errors} 或数组认；
 * 形状不对返回 null，调用方按普通错误提示
 */
export function formErrorsOf(source: unknown): FormError[] | null {
  const obj = source && typeof source === 'object' && !Array.isArray(source) ? source as { body?: unknown; errors?: unknown } : null
  const body = obj && 'body' in obj ? obj.body : obj
  const raw = body && typeof body === 'object' && !Array.isArray(body) ? (body as { errors?: unknown }).errors : source
  if (!Array.isArray(raw)) return null
  const list = raw.filter((e): e is FormError => !!e && typeof e === 'object'
    && typeof (e as FormError).path === 'string' && typeof (e as FormError).message === 'string')
  return list.length ? list : null
}

/** datetime 控件（本地时间，无时区）→ RFC 3339，带本地时区偏移；反过来取回控件能显示的样子 */
export function localToRfc3339(local: string): string {
  if (!local) return ''
  const d = new Date(local)
  if (Number.isNaN(d.getTime())) return local
  const off = -d.getTimezoneOffset()
  const sign = off >= 0 ? '+' : '-'
  const pad = (n: number) => String(Math.trunc(Math.abs(n))).padStart(2, '0')
  const base = local.length === 16 ? `${local}:00` : local
  return `${base}${sign}${pad(off / 60)}:${pad(off % 60)}`
}

export function rfc3339ToLocal(value: unknown): string {
  if (typeof value !== 'string' || !value) return ''
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}
