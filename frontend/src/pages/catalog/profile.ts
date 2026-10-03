import type { CatalogProfileNumberKey, CatalogProfileSettings } from '../../types'
import { PROFILE_FIELD_LABEL, PROFILE_TEXT } from '../../lib/terms'

// ===========================================================================
// 数据剖析的设置：数据源 options.catalog_profile 和表单之间的来回转换、校验、保存被拒时认出是哪一项。
// 规则跟着服务端（backend/app/data/catalog_profile.py 的 ProfileSettings、profile_settings、
// profile_settings_problem）走：缺省即关闭；读的时候宽松（某一项不对就用缺省），写的时候严格（超出范围的拒收）。
// ===========================================================================

/** 数据源 options 里剖析设置的键 */
export const PROFILE_OPTION = 'catalog_profile'

export const PROFILE_NUMBER_KEYS: readonly CatalogProfileNumberKey[] = [
  'max_queries', 'query_timeout_s', 'sample_size', 'max_scan_rows', 'max_total_s',
]

/** 缺省值（服务端 ProfileSettings 的默认） */
export const PROFILE_DEFAULTS: CatalogProfileSettings = {
  enabled: false, max_queries: 60, query_timeout_s: 10, sample_size: 2000, max_scan_rows: 100_000, max_total_s: 120,
}

/** 各数值项的范围（服务端 _NUMBER_FIELDS）：下限、上限、是否必须是整数 */
export const PROFILE_LIMITS: Record<CatalogProfileNumberKey, { min: number; max: number; integer: boolean }> = {
  max_queries: { min: 1, max: 500, integer: true },
  query_timeout_s: { min: 1, max: 60, integer: false },
  sample_size: { min: 10, max: 10_000, integer: true },
  max_scan_rows: { min: 0, max: 10_000_000, integer: true },
  max_total_s: { min: 10, max: 600, integer: false },
}

/** 一个数：数字和数字写成的文字都认（同服务端 _number）。不合规返回 null */
function numberIn(value: unknown, key: CatalogProfileNumberKey): number | null {
  if (value == null || typeof value === 'boolean') return null
  const text = String(value).trim().replace(/,/g, '')
  if (!text) return null
  const n = Number(text)
  const { min, max, integer } = PROFILE_LIMITS[key]
  if (!Number.isFinite(n) || n < min || n > max) return null
  if (integer && !Number.isInteger(n)) return null
  return n
}

const rawOf = (options: Record<string, unknown> | null | undefined): Record<string, unknown> | null => {
  const raw = options?.[PROFILE_OPTION]
  return raw && typeof raw === 'object' && !Array.isArray(raw) ? raw as Record<string, unknown> : null
}

/** options → 生效的设置。宽松：某一项不对用缺省；开关只认 true */
export function profileSettingsOf(options: Record<string, unknown> | null | undefined): CatalogProfileSettings {
  const raw = rawOf(options)
  const out: CatalogProfileSettings = { ...PROFILE_DEFAULTS, enabled: raw?.enabled === true }
  if (!raw) return out
  for (const key of PROFILE_NUMBER_KEYS) {
    const n = numberIn(raw[key], key)
    if (n != null) out[key] = n
  }
  return out
}

// ---------------------------------------------------------------------------
// 表单：数值项是文字（输入时允许写一半），空着表示用缺省
// ---------------------------------------------------------------------------

export interface ProfileForm {
  enabled: boolean
  values: Record<CatalogProfileNumberKey, string>
}

export function profileFormOf(options: Record<string, unknown> | null | undefined): ProfileForm {
  const raw = rawOf(options)
  const values = Object.fromEntries(PROFILE_NUMBER_KEYS.map((k) => {
    const v = raw?.[k]
    return [k, v == null || typeof v === 'object' ? '' : String(v)]
  })) as Record<CatalogProfileNumberKey, string>
  return { enabled: raw?.enabled === true, values }
}

/** options 里已经有剖析设置这一项 */
export const hasProfileOption = (options: Record<string, unknown> | null | undefined): boolean => !!rawOf(options)

/**
 * 表单 → 要写进 options 的设置。原来没有（stored 为假）、现在也没开、一项都没填的不写（不给 options 平白多一个键）；
 * 其余写开关和填了的项。填的不是数时原样交上去，由服务端拒收并说明是哪一项（表单上已经先拦过一次）
 */
export function profileOptionOf(form: ProfileForm, stored: boolean): Record<string, unknown> | undefined {
  const filled = PROFILE_NUMBER_KEYS.filter((k) => form.values[k].trim())
  if (!stored && !form.enabled && !filled.length) return undefined
  const out: Record<string, unknown> = { enabled: form.enabled }
  for (const k of filled) {
    const text = form.values[k].trim().replace(/,/g, '')
    const n = Number(text)
    out[k] = Number.isFinite(n) ? n : form.values[k].trim()
  }
  return out
}

/** 表单的格式问题：键 → 说明。空对象表示可以保存 */
export function profileProblems(form: ProfileForm): Partial<Record<CatalogProfileNumberKey, string>> {
  const out: Partial<Record<CatalogProfileNumberKey, string>> = {}
  for (const k of PROFILE_NUMBER_KEYS) {
    if (!form.values[k].trim()) continue
    if (numberIn(form.values[k], k) == null) {
      const { min, max, integer } = PROFILE_LIMITS[k]
      out[k] = PROFILE_TEXT.rangeError(min, max, integer)
    }
  }
  return out
}

/** 表单此刻对应的设置：没填、填错的项按缺省（风险说明据此写出「最多 N 条」） */
export function profileSettingsOfForm(form: ProfileForm): CatalogProfileSettings {
  const out: CatalogProfileSettings = { ...PROFILE_DEFAULTS, enabled: form.enabled }
  for (const k of PROFILE_NUMBER_KEYS) {
    const n = numberIn(form.values[k], k)
    if (n != null) out[k] = n
  }
  return out
}

export const sameProfileForm = (a: ProfileForm, b: ProfileForm): boolean =>
  a.enabled === b.enabled && PROFILE_NUMBER_KEYS.every((k) => a.values[k] === b.values[k])

/**
 * 服务端保存时拒收的 422（一句中文）说的是剖析设置的哪一项：
 * 「数据剖析的「查询次数上限」需要填写 1 到 500 之间的整数；当前为「0」」→ max_queries。
 * 说的是剖析设置、但认不出哪一项（格式不对、有认不出的项）返回 'section'；说的不是剖析设置返回 null
 */
export function profileFieldOfRejection(message: string): 'enabled' | CatalogProfileNumberKey | 'section' | null {
  if (!message.includes('数据剖析')) return null
  const named = message.match(/「([^」]+)」/)?.[1] ?? ''
  for (const k of ['enabled', ...PROFILE_NUMBER_KEYS] as const) {
    if (named && named.startsWith(PROFILE_FIELD_LABEL[k])) return k
  }
  return 'section'
}

// ---------------------------------------------------------------------------
// 剖析：范围、报告里的读数、409 的分类
// ---------------------------------------------------------------------------

/** 不指定表时服务端剖析几张（PROFILE_DEFAULT_TABLES） */
export const PROFILE_DEFAULT_TABLES = 10
/** 一次最多指定几张表（接口 tables 的 max_length） */
export const PROFILE_MAX_TABLES = 200

/** 覆盖率写成百分数：最多一位小数，不到 100% 的不进成 100%（同服务端 _percent） */
export function coverageText(ratio: number): string {
  const text = (ratio * 100).toFixed(1).replace(/\.0$/, '')
  return text === '100' && ratio < 1 ? '99.9%' : `${text}%`
}

/** 已用时长的读数：「不到 1 秒」「8 秒」「1 分 05 秒」 */
export function secondsText(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000))
  if (s < 1) return '不到 1 秒'
  return s < 60 ? `${s} 秒` : `${Math.floor(s / 60)} 分 ${String(s % 60).padStart(2, '0')} 秒`
}

/** 剖析接口回 409 的几种情况（按服务端的原话认：catalog_profile.ensure_enabled、api/catalog.py、engine.SnapshotTampered） */
export type ProfileBlock = 'disabled' | 'inactive' | 'noSchema' | 'busy' | 'tampered' | 'other'

export function profileBlockOf(message: string): ProfileBlock {
  if (message.includes('未开启数据剖析')) return 'disabled'
  if (message.includes('已停用')) return 'inactive'
  if (message.includes('正在进行数据剖析')) return 'busy'
  if (message.includes('探查结构')) return 'noSchema'
  if (/已拒绝(?:这次)?查询/.test(message)) return 'tampered'
  return 'other'
}

/** 设置里有没有剖析设置这一项（options 的其余键原样带上） */
export function withProfileOption(options: Record<string, unknown> | null | undefined, value: Record<string, unknown> | undefined): Record<string, unknown> {
  const { [PROFILE_OPTION]: _old, ...rest } = options ?? {}
  return value ? { ...rest, [PROFILE_OPTION]: value } : rest
}

/** 剖析报告里一列码值的键（表名 + 列名）：从报告去填含义、填完回到报告时按它对上是哪一列 */
export const fillKey = (table: string, column: string): string => `${table}\u0000${column}`
