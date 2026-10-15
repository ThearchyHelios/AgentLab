import type {
  CatalogLastProfile, CatalogProfileNumberKey, CatalogProfileSettings, CatalogProfileSize, CatalogProfileTaskStatus, CatalogTableRow,
} from '../../types'
import { PROFILE_TEXT } from '../../lib/terms'
import { formatTime, parseServerTime } from '../../lib/format'

// ===========================================================================
// 数据剖析的设置：数据源 options.catalog_profile 和表单之间的来回转换、校验、保存被拒时认出是哪一项。
// 规则跟着服务端（backend/app/data/catalog_profile.py 的 ProfileSettings、profile_settings、
// profile_settings_problem）走：缺省即关闭；读的时候宽松（某一项不对就用缺省），写的时候严格（超出范围的拒收）。
//
// 预算按表算（每张表最多查询数、每张表时长上限），没有整次的总量上限。以前的「查询次数上限」「总时长上限」按整次剖析算，
// 含义不同：服务端启动时把这两个旧键删掉、保存时摘掉，这里读的时候也不认它们（表单里没有、保存时不带）。
// ===========================================================================

/** 数据源 options 里剖析设置的键 */
export const PROFILE_OPTION = 'catalog_profile'

/** 设置里的数值项，也是表单上的顺序：先是按表的两项预算，再是单条查询和抽样 */
export const PROFILE_NUMBER_KEYS: readonly CatalogProfileNumberKey[] = [
  'table_max_queries', 'table_max_s', 'query_timeout_s', 'sample_size', 'max_scan_rows',
]

/** 缺省值（服务端 ProfileSettings 的默认） */
export const PROFILE_DEFAULTS: CatalogProfileSettings = {
  enabled: false, table_max_queries: 40, table_max_s: 60, query_timeout_s: 10, sample_size: 2000, max_scan_rows: 100_000,
}

/** 各数值项的范围（服务端 _NUMBER_FIELDS）：下限、上限、是否必须是整数 */
export const PROFILE_LIMITS: Record<CatalogProfileNumberKey, { min: number; max: number; integer: boolean }> = {
  table_max_queries: { min: 1, max: 200, integer: true },
  table_max_s: { min: 10, max: 600, integer: false },
  query_timeout_s: { min: 1, max: 60, integer: false },
  sample_size: { min: 10, max: 10_000, integer: true },
  max_scan_rows: { min: 0, max: 10_000_000, integer: true },
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
 * 其余写开关和填了的项。填的不是数时原样交上去，由服务端拒收并说明是哪一项（表单上已经先拦过一次）。
 * 旧的两个键（整次的查询次数上限、总时长上限）不在表单里，保存时不带——服务端也会摘掉
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

/** 表单此刻对应的设置：没填、填错的项按缺省（风险说明据此写出「每张表最多 N 条」） */
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

/** 剖析设置不合规的 422 带的机读码（服务端 api/coded.PROFILE_SETTINGS_INVALID） */
export const PROFILE_SETTINGS_INVALID = 'profile_settings_invalid'

/**
 * 服务端保存时拒收的 422 说的是剖析设置的哪一项：按机读码和 field 认（服务端 profile_settings_rejection），不从原话里认——
 * 文案一改，按原话认的就悄悄失效。说的是剖析设置、但没有 field（格式不对、有认不出的项）返回 'section'；
 * 不是剖析设置的、没带机读码的（老服务端）返回 null，交给通用的报错提示
 */
export function profileFieldOfRejection(e: { code?: string; field?: string }): 'enabled' | CatalogProfileNumberKey | 'section' | null {
  if (e.code !== PROFILE_SETTINGS_INVALID) return null
  const field = e.field ?? ''
  return field === 'enabled' || (PROFILE_NUMBER_KEYS as readonly string[]).includes(field)
    ? field as 'enabled' | CatalogProfileNumberKey
    : 'section'
}

/** 设置里有没有剖析设置这一项（options 的其余键原样带上） */
export function withProfileOption(options: Record<string, unknown> | null | undefined, value: Record<string, unknown> | undefined): Record<string, unknown> {
  const { [PROFILE_OPTION]: _old, ...rest } = options ?? {}
  return value ? { ...rest, [PROFILE_OPTION]: value } : rest
}

// ---------------------------------------------------------------------------
// 剖析：范围、读数、409 的分类
// ---------------------------------------------------------------------------

/** 一次最多指定几张表（接口 tables 的上限） */
export const PROFILE_MAX_TABLES = 500

/**
 * 能不能剖析这张表：在表结构里、不是视图、有可剖析的项（profile_checks > 0）。老服务端不给 profile_checks 时不知道，
 * 按能剖析算（交给服务端，剖析不了的它会记跳过）
 */
export const isProfilable = (r: Pick<CatalogTableRow, 'in_schema' | 'is_view' | 'profile_checks'>): boolean =>
  r.in_schema && !r.is_view && (r.profile_checks == null || r.profile_checks > 0)

/** 服务端「需要剖析」的口径：能剖析、并且 profile_due（从没剖析过、上次没做完、之后出现了新的待核实关系） */
export const isProfileDue = (r: CatalogTableRow): boolean => isProfilable(r) && r.profile_due === true

/** 范围的几张表：needed 需要剖析的、all 全部能剖析的（服务端挑表的顺序：有待核实关系的在前，再按使用次数） */
export function profileScopeTables(rows: CatalogTableRow[], scope: 'needed' | 'all'): string[] {
  return rows.filter(scope === 'needed' ? isProfileDue : isProfilable).map((r) => r.table_name)
}

/** 因为运行原因没查成的项（计入 unfinished、使表成为部分完成）；其余跳过的原因是按规则不查 */
export const UNFINISHED_REASONS = new Set(['budget', 'deadline', 'failed', 'timeout', 'error', 'rejected', 'slow', 'cancelled'])

const TASK_ENDED = new Set<CatalogProfileTaskStatus>(['ok', 'partial', 'failed', 'skipped', 'cancelled'])
export const isProfileTaskEnded = (t: { status: CatalogProfileTaskStatus }): boolean => TASK_ENDED.has(t.status)
/** 「重试没做完的表」带上的：失败、部分完成、已停止（同服务端 retry） */
export const PROFILE_RETRYABLE = new Set<CatalogProfileTaskStatus>(['failed', 'partial', 'cancelled'])

/** 覆盖率写成百分数：最多一位小数，不到 100% 的不进成 100%（同服务端 _percent） */
export function coverageText(ratio: number): string {
  const text = (ratio * 100).toFixed(1).replace(/\.0$/, '')
  return text === '100' && ratio < 1 ? '99.9%' : `${text}%`
}

/** 用时的读数：「不到 1 秒」「8 秒」「1 分 05 秒」「1 小时 02 分」 */
export function secondsText(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000))
  if (s < 1) return '不到 1 秒'
  if (s < 60) return `${s} 秒`
  if (s < 3600) return `${Math.floor(s / 60)} 分 ${String(s % 60).padStart(2, '0')} 秒`
  return `${Math.floor(s / 3600)} 小时 ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')} 分`
}

/** 预算估算里的时长：「50 秒」「40 分钟」「2 小时 10 分钟」（只是上限的估算，不写到秒） */
export function spanText(secs: number): string {
  const s = Math.max(0, Math.round(secs))
  if (s < 60) return `${s} 秒`
  const m = Math.round(s / 60)
  if (m < 60) return `${m} 分钟`
  const h = Math.floor(m / 60)
  return m % 60 ? `${h} 小时 ${m % 60} 分钟` : `${h} 小时`
}

/** 多久以前：「刚刚」「12 分钟前」「3 小时前」「3 天前」，一个月以上写日期（表清单上「3 天前剖析」） */
export function agoText(v: string | null | undefined, now = Date.now()): string {
  const d = parseServerTime(v)
  if (!d) return ''
  const diff = now - d.getTime()
  if (diff < 45_000) return '刚刚'
  const m = Math.round(diff / 60_000)
  if (m < 60) return `${Math.max(1, m)} 分钟前`
  const h = Math.round(diff / 3_600_000)
  if (h < 24) return `${h} 小时前`
  const days = Math.floor(diff / 86_400_000)
  return days <= 30 ? `${days} 天前` : formatTime(d)
}

/** 行数估算写成人话，带来源（统计信息 / 逐行数出来的 / 数到上限）；没有估算为 null */
export function sizeText(size: CatalogProfileSize | null | undefined): { text: string; hint?: string; source?: string } | null {
  if (!size) return null
  if (size.rows != null) {
    return { text: size.method === 'stats' ? PROFILE_TEXT.rowsStats(size.rows) : PROFILE_TEXT.rowsCount(size.rows),
      hint: PROFILE_TEXT.rowsHint[size.method], source: PROFILE_TEXT.rowsSource[size.method] }
  }
  if (size.at_least != null) {
    return { text: PROFILE_TEXT.rowsAtLeast(size.at_least), hint: PROFILE_TEXT.rowsHint.capped, source: PROFILE_TEXT.rowsSource.capped }
  }
  return { text: PROFILE_TEXT.rowsUnknown }
}

/**
 * 表清单上「剖析」那一列：这张表剖析到哪了。live 是页面正跟着的剖析任务里这张表的状态（排队、剖析中），比清单新；
 * 没有就按清单上的最近一次剖析。tone：due 需要剖析（从没剖析过、没做完、有新关系），bad 失败，quiet 其余
 */
export function profileCell(r: CatalogTableRow, live: CatalogProfileTaskStatus | null, now = Date.now()):
  { text: string; title: string; tone: 'due' | 'bad' | 'active' | 'quiet' } | null {
  const T = PROFILE_TEXT
  if (live === 'pending' || live === 'running') return { text: T.listStatus[live], title: T.listStatus[live], tone: 'active' }
  const last: CatalogLastProfile | null | undefined = r.last_profile
  if (last === undefined && r.profile_checks === undefined) return null
  if (last && (last.status === 'pending' || last.status === 'running')) {
    return { text: T.listStatus[last.status], title: T.listStatus[last.status], tone: 'active' }
  }
  if (!last) {
    if (!r.in_schema || r.is_view) return null
    if (r.profile_checks === 0) return { text: T.listNoChecks, title: T.listNoChecksHint, tone: 'quiet' }
    return { text: T.listNever, title: T.dueReason.never, tone: 'due' }
  }
  const at = last.finished_at ?? last.started_at
  const when = at ? formatTime(at) : ''
  const label = last.status === 'ok' ? T.listAgo(agoText(at, now)) : T.listStatus[last.status] ?? last.status
  const title = [T.listHint(T.taskStatus[last.status] ?? last.status, when), last.error ?? '',
    r.profile_due && r.profile_due_reason ? T.dueReason[r.profile_due_reason] : ''].filter(Boolean).join('。')
  if (last.status === 'failed') return { text: label, title, tone: 'bad' }
  if (r.profile_due && r.profile_due_reason === 'new_relations' && isProfilable(r)) return { text: T.listNewRelations, title, tone: 'due' }
  return { text: label, title, tone: r.profile_due && isProfilable(r) ? 'due' : 'quiet' }
}

/** 剖析接口回 409 的几种情况：按机读码认（服务端 api/coded.py） */
export type ProfileBlock = 'disabled' | 'inactive' | 'noSchema' | 'busy' | 'tampered' | 'nothing' | 'other'

/** 这个源已有排队、进行中的剖析任务；已经结束的任务不需要停止 */
export const PROFILE_BUSY = 'profile_busy'
export const PROFILE_FINISHED = 'catalog_profile_finished'

const PROFILE_BLOCK_CODES: Record<string, ProfileBlock> = {
  profile_disabled: 'disabled',
  datasource_inactive: 'inactive',
  schema_missing: 'noSchema',
  [PROFILE_BUSY]: 'busy',
  snapshot_tampered: 'tampered',
  profile_nothing: 'nothing',
}

/** 没带机读码（老服务端）或认不出的按「无法开始剖析」说，给重试；不从原话里猜 */
export function profileBlockOf(e: { code?: string }): ProfileBlock {
  return (e.code && PROFILE_BLOCK_CODES[e.code]) || 'other'
}

/** 剖析结果里一列码值的键（表名 + 列名）：从结果去填含义、填完回到结果时按它对上是哪一列 */
export const fillKey = (table: string, column: string): string => `${table}\u0000${column}`
