/**
 * 定时任务页的纯函数：频率预设和 cron 互转、按定时任务的时区写时间、表单和请求体互转、表单的问题。
 * 不碰 React、不发请求，scripts/check-schedules.mjs 直接借 vite 转译后调用它们。
 *
 * 时间一律按定时任务自己的时区写（「每周一 09:00（Asia/Shanghai）」说的就是那个时区的 09:00），本机时区不同时
 * 在悬停提示里另给本机时间：只按本机写，人在外地打开就会以为周报改成了凌晨三点发。
 */
import { SCHEDULE_TEXT as T } from '../../lib/terms'
import type {
  Schedule, ScheduleCreate, ScheduleEvent, SchedulePatch, ScheduleRunClass, ScheduleWebhookFormat, ScheduleWebhookIn,
} from '../../types'

// ---------------------------------------------------------------------------
// 频率预设 ⇄ cron
// ---------------------------------------------------------------------------

export type FreqPreset = 'daily' | 'weekly' | 'monthly' | 'hourly' | 'custom'
export const FREQ_PRESETS: readonly FreqPreset[] = ['daily', 'weekly', 'monthly', 'hourly', 'custom']

/**
 * 「每隔几小时」只给 24 的约数：*／5 这种在跨天处会断成 4 小时一次（20 点之后下一次是 0 点），
 * 名义上的「每 5 小时」就不成立了。要这种的写自定义 cron
 */
export const HOUR_STEPS: readonly number[] = [1, 2, 3, 4, 6, 8, 12]

/** 星期几按周一在前排（0 是周日，和 cron 一致） */
export const WEEKDAY_ORDER: readonly number[] = [1, 2, 3, 4, 5, 6, 0]

export interface Freq {
  preset: FreqPreset
  /** 每天、每周、每月的时刻，HH:MM（24 小时制） */
  time: string
  /** 每周的星期几：0 是周日、1 是周一……6 是周六 */
  weekdays: number[]
  /** 每月的几号（1–31） */
  monthday: number
  /** 每隔几小时（HOUR_STEPS 之一） */
  everyHours: number
  /** 每隔几小时时，在每次的第几分（0–59） */
  minute: number
  /** 自定义 cron 的原文 */
  cron: string
}

export const DEFAULT_FREQ: Freq = {
  preset: 'daily', time: '09:00', weekdays: [1], monthday: 1, everyHours: 1, minute: 0, cron: '',
}

const pad2 = (n: number) => String(n).padStart(2, '0')

/** 「09:30」→ [9, 30]；不是合法的钟点返回 null */
export function parseTime(text: string): [number, number] | null {
  const m = /^(\d{1,2}):(\d{2})$/.exec(text.trim())
  if (!m) return null
  const h = Number(m[1])
  const min = Number(m[2])
  return h <= 23 && min <= 59 ? [h, min] : null
}

/** 星期几的那一段：连着三天以上写成区间（1-5），否则逐个列出（0,6）；一周七天都选了写 * */
export function weekdayField(days: number[]): string {
  const set = [...new Set(days.map((d) => (d === 7 ? 0 : d)))].filter((d) => d >= 0 && d <= 6).sort((a, b) => a - b)
  if (set.length === 7) return '*'
  const out: string[] = []
  for (let i = 0; i < set.length;) {
    let j = i
    while (j + 1 < set.length && set[j + 1] === set[j] + 1) j++
    if (j - i >= 2) out.push(`${set[i]}-${set[j]}`)
    else for (let k = i; k <= j; k++) out.push(String(set[k]))
    i = j + 1
  }
  return out.join(',')
}

/**
 * 预设生成的 cron。填得不完整（钟点没填、每周一天都没选、自定义是空的）时返回空串：
 * 这时不预览、不能保存，由 freqProblem 说缺什么，不拿一个猜出来的频率顶上
 */
export function cronOf(f: Freq): string {
  if (f.preset === 'custom') return f.cron.trim().split(/\s+/).filter(Boolean).join(' ')
  if (f.preset === 'hourly') {
    if (!Number.isInteger(f.minute) || f.minute < 0 || f.minute > 59 || !HOUR_STEPS.includes(f.everyHours)) return ''
    return `${f.minute} ${f.everyHours === 1 ? '*' : `*/${f.everyHours}`} * * *`
  }
  const time = parseTime(f.time)
  if (!time) return ''
  const [h, m] = time
  if (f.preset === 'daily') return `${m} ${h} * * *`
  if (f.preset === 'weekly') return f.weekdays.length ? `${m} ${h} * * ${weekdayField(f.weekdays)}` : ''
  if (!Number.isInteger(f.monthday) || f.monthday < 1 || f.monthday > 31) return ''
  return `${m} ${h} ${f.monthday} * *`
}

/** 一段 cron 里的一个整数（不带步长、区间），超出范围或不是整数时返回 null */
function intField(text: string, lo: number, hi: number): number | null {
  if (!/^\d{1,2}$/.test(text)) return null
  const n = Number(text)
  return n >= lo && n <= hi ? n : null
}

/** 星期几那一段（0–7 的数字、逗号列表、区间）→ 排好序的星期几；写了名字、步长之类的返回 null，交给自定义 */
function parseWeekdays(text: string): number[] | null {
  const out = new Set<number>()
  for (const part of text.split(',')) {
    const range = /^(\d)-(\d)$/.exec(part)
    if (range) {
      const a = Number(range[1])
      const b = Number(range[2])
      if (a > b || b > 7) return null
      for (let d = a; d <= b; d++) out.add(d === 7 ? 0 : d)
      continue
    }
    const d = intField(part, 0, 7)
    if (d == null) return null
    out.add(d === 7 ? 0 : d)
  }
  return out.size ? [...out].sort((a, b) => a - b) : null
}

/**
 * 已有的 cron 落回预设：编辑时照它选中「每周」、勾上周一。认不出的（写了月份、名字、步长、
 * 「每隔几小时」不是 24 的约数）一律算自定义，原文照放，不改写别人写的 cron
 */
export function freqOf(cron: string): Freq {
  const parts = cron.trim().split(/\s+/).filter(Boolean)
  const base: Freq = { ...DEFAULT_FREQ, cron: parts.join(' ') }
  const custom: Freq = { ...base, preset: 'custom' }
  if (parts.length !== 5) return custom
  const [mi, ho, dom, mon, dow] = parts
  if (mon !== '*') return custom
  const minute = intField(mi, 0, 59)
  if (minute == null) return custom
  const hour = intField(ho, 0, 23)
  if (hour != null) {
    const time = `${pad2(hour)}:${pad2(minute)}`
    if (dom === '*' && dow === '*') return { ...base, preset: 'daily', time }
    if (dom === '*') {
      const weekdays = parseWeekdays(dow)
      return weekdays ? { ...base, preset: 'weekly', time, weekdays } : custom
    }
    if (dow === '*') {
      const monthday = intField(dom, 1, 31)
      return monthday != null ? { ...base, preset: 'monthly', time, monthday } : custom
    }
    return custom
  }
  if (dom !== '*' || dow !== '*') return custom
  if (ho === '*') return { ...base, preset: 'hourly', everyHours: 1, minute }
  const step = /^\*\/(\d{1,2})$/.exec(ho)
  if (step && HOUR_STEPS.includes(Number(step[1]))) return { ...base, preset: 'hourly', everyHours: Number(step[1]), minute }
  return custom
}

// ---------------------------------------------------------------------------
// 时区与时间
// ---------------------------------------------------------------------------

/** 本机时区；浏览器说不出来时按 UTC */
export function browserTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  } catch {
    return 'UTC'
  }
}

/** 常用时区，下拉里排在前面。城市名在 SCHEDULE_TEXT.timezoneLabel，偏移按当前日期另算（夏令时会变） */
export const COMMON_TIMEZONES: readonly string[] = [
  'Asia/Shanghai', 'Asia/Hong_Kong', 'Asia/Taipei', 'Asia/Singapore', 'Asia/Tokyo', 'Asia/Seoul', 'Asia/Kolkata',
  'Asia/Dubai', 'Europe/London', 'Europe/Paris', 'Europe/Berlin', 'America/New_York', 'America/Chicago',
  'America/Los_Angeles', 'UTC',
]

/**
 * 时区下拉的选项：本机时区、正在用的时区排最前（不在常用表里也列上），再接常用的，不重复。
 * label 写「Asia/Shanghai（北京，UTC+08:00）」，本机的另标「本机」
 */
export function timezoneOptions(current: string, local: string = browserTimezone(), now: Date = new Date()):
  { tz: string; label: string }[] {
  const seen = new Set<string>()
  const out: { tz: string; label: string }[] = []
  for (const tz of [local, current, ...COMMON_TIMEZONES]) {
    if (!tz || seen.has(tz)) continue
    seen.add(tz)
    const notes = [T.timezoneLabel[tz], offsetLabel(tz, now), tz === local ? T.timezoneLocal : ''].filter(Boolean)
    out.push({ tz, label: notes.length ? `${tz}（${notes.join('，')}）` : tz })
  }
  return out
}

export interface ZonedParts {
  year: number
  month: number
  day: number
  hour: number
  minute: number
  /** 0 是周日 */
  weekday: number
}

const formatters = new Map<string, Intl.DateTimeFormat | null>()
const WEEKDAY_EN: Record<string, number> = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 }

/** 某个时刻在某个时区里的年月日时分和星期。时区名浏览器不认时返回 null */
export function zonedParts(d: Date, tz: string): ZonedParts | null {
  if (Number.isNaN(d.getTime())) return null
  let f = formatters.get(tz)
  if (f === undefined) {
    try {
      // 用 en-US 取各段的数字和英文星期缩写，再自己拼中文：各浏览器的 zh-CN 写法不完全一样
      f = new Intl.DateTimeFormat('en-US', {
        timeZone: tz, year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: 'numeric',
        weekday: 'short', hourCycle: 'h23',
      })
    } catch {
      f = null
    }
    formatters.set(tz, f)
  }
  if (!f) return null
  const p: Record<string, string> = {}
  for (const part of f.formatToParts(d)) p[part.type] = part.value
  const parts = {
    year: Number(p.year), month: Number(p.month), day: Number(p.day),
    // 个别引擎在 h23 下仍把午夜写成 24
    hour: Number(p.hour) % 24, minute: Number(p.minute), weekday: WEEKDAY_EN[p.weekday] ?? -1,
  }
  return Object.values(parts).every((v) => Number.isFinite(v) && v >= 0) ? parts : null
}

/** 这个时区此刻相对 UTC 的偏移：「UTC+08:00」「UTC-04:00」「UTC」 */
export function offsetLabel(tz: string, d: Date = new Date()): string {
  const p = zonedParts(d, tz)
  if (!p) return ''
  const wall = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute)
  const minutes = Math.round((wall - Math.floor(d.getTime() / 60_000) * 60_000) / 60_000)
  if (minutes === 0) return 'UTC'
  const abs = Math.abs(minutes)
  return `UTC${minutes > 0 ? '+' : '-'}${pad2(Math.floor(abs / 60))}:${pad2(abs % 60)}`
}

/**
 * 按时区写一个时刻。full：「2026-10-06 周一 09:00」；short：同一年省掉年份「10/6 周一 09:00」，
 * 跨年写「2027/1/4 周一 09:00」。时区名浏览器不认时按本机时区写
 */
export function formatZoned(d: Date, tz: string, mode: 'full' | 'short' = 'short', now: Date = new Date()): string {
  const p = zonedParts(d, tz) ?? zonedParts(d, browserTimezone())
  if (!p) return '—'
  const hm = `${pad2(p.hour)}:${pad2(p.minute)}`
  const wd = T.weekdayShort[p.weekday]
  if (mode === 'full') return `${p.year}-${pad2(p.month)}-${pad2(p.day)} ${wd} ${hm}`
  const thisYear = (zonedParts(now, tz) ?? p).year
  const md = `${p.month}/${p.day}`
  return `${p.year === thisYear ? md : `${p.year}/${md}`} ${wd} ${hm}`
}

/**
 * 距离一个将来时刻还有多久：「不到 1 分钟后」「12 分钟后」「3 小时后」「2 天后」。先取整再判档，59.6 分钟
 * 写「1 小时后」而不是「60 分钟后」。已经过了的返回 null，由调用方照实说（调度可能没开、或者正在发起）
 */
export function formatUntil(target: Date, now: number): string | null {
  const diff = target.getTime() - now
  if (diff < 0) return null
  if (diff < 60_000) return T.until.soon
  const m = Math.round(diff / 60_000)
  if (m < 60) return T.until.minutes(m)
  const h = Math.round(diff / 3_600_000)
  if (h < 24) return T.until.hours(h)
  return T.until.days(Math.round(diff / 86_400_000))
}

/** 补发宽限的写法：「5 分钟」「1 小时」「1 天」，凑不成整数的按秒写 */
export function graceLabel(seconds: number): string {
  if (seconds > 0 && seconds % 86_400 === 0) return T.span.day(seconds / 86_400)
  if (seconds > 0 && seconds % 3600 === 0) return T.span.hour(seconds / 3600)
  if (seconds > 0 && seconds % 60 === 0) return T.span.minute(seconds / 60)
  return T.span.second(seconds)
}

/**
 * 补发宽限的可选值。不给 0：调度 15 秒一拍，到点的触发总要晚上几秒，宽限为 0 等于每次都算错过、全部跳过
 */
export const GRACE_OPTIONS: readonly number[] = [300, 3600, 21_600, 86_400]
export const DEFAULT_GRACE_S = 3600

// ---------------------------------------------------------------------------
// 表单 ⇄ 请求体
// ---------------------------------------------------------------------------

export const WEBHOOK_FORMATS: readonly ScheduleWebhookFormat[] = ['generic', 'feishu', 'wecom', 'dingtalk']
export const SCHEDULE_EVENTS: readonly ScheduleEvent[] = ['finished', 'failed', 'awaiting_approval']

export interface WebhookForm {
  on: boolean
  format: ScheduleWebhookFormat
  /** 新填的地址。已保存过地址、又没点「替换」时不用它 */
  url: string
  /** 已保存过地址时：true 表示换成上面新填的 */
  replaceUrl: boolean
  secret: string
  /** 已设置过密钥时：keep 保留、replace 换成上面新填的、clear 清除。没设置过时只看 secret 填没填 */
  secretMode: 'keep' | 'replace' | 'clear'
  events: ScheduleEvent[]
}

export interface ScheduleForm {
  name: string
  workflowId: string
  freq: Freq
  timezone: string
  runClass: ScheduleRunClass
  /** 字段名 → 模板，留空的字段不交（按工作流的默认值） */
  inputs: Record<string, string>
  /** 只在新建时用：修改时启用、暂停走页头的开关，立刻生效 */
  enabled: boolean
  misfireGraceS: number
  webhook: WebhookForm
}

/** 表单初值：新建时用默认值（时区取本机），修改时照读到的定时任务 */
export function formOf(s: Schedule | null, timezone: string = browserTimezone()): ScheduleForm {
  if (!s) {
    return {
      name: '', workflowId: '', freq: { ...DEFAULT_FREQ }, timezone, runClass: 'formal', inputs: {}, enabled: true,
      misfireGraceS: DEFAULT_GRACE_S,
      webhook: { on: false, format: 'generic', url: '', replaceUrl: false, secret: '', secretMode: 'keep', events: [...SCHEDULE_EVENTS] },
    }
  }
  return {
    name: s.name,
    workflowId: s.workflow_id,
    freq: freqOf(s.cron),
    timezone: s.timezone,
    runClass: s.run_class,
    inputs: { ...(s.input_template ?? {}) },
    enabled: s.enabled,
    misfireGraceS: s.misfire_grace_s,
    webhook: s.webhook
      ? { on: true, format: s.webhook.format, url: '', replaceUrl: false, secret: '', secretMode: 'keep', events: [...s.webhook.events] }
      : { on: false, format: 'generic', url: '', replaceUrl: false, secret: '', secretMode: 'keep', events: [...SCHEDULE_EVENTS] },
  }
}

/** 交上去的输入模板：只留填了的字段（只有空白的也算没填），留空的按工作流的默认值 */
export function templateOf(inputs: Record<string, string>): Record<string, string> {
  return Object.fromEntries(Object.entries(inputs).filter(([, v]) => typeof v === 'string' && v.trim() !== ''))
}

/** 事件按固定顺序排，勾选的先后不影响请求体，也不让「改过又改回」算成改动 */
const sortEvents = (events: ScheduleEvent[]) => SCHEDULE_EVENTS.filter((e) => events.includes(e))

export function createBody(f: ScheduleForm): ScheduleCreate {
  const w = f.webhook
  return {
    name: f.name.trim(),
    workflow_id: f.workflowId,
    cron: cronOf(f.freq),
    timezone: f.timezone,
    run_class: f.runClass,
    input_template: templateOf(f.inputs),
    enabled: f.enabled,
    overlap: 'skip',
    misfire_grace_s: f.misfireGraceS,
    webhook: w.on
      ? { format: w.format, url: w.url.trim(), ...(w.secret ? { secret: w.secret } : {}), events: sortEvents(w.events) }
      : null,
  }
}

const sameJson = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)
const sortedEntries = (o: Record<string, string>) => Object.entries(o).sort(([a], [b]) => a.localeCompare(b))

/**
 * 修改时交上去的字段（不含 version）：拿表单和打开时的表单比，只带改了的，什么都没改返回 null。和表单初值比、
 * 不和服务端的原文比：cron 落回预设再生成，写法可能和原文不同（0-6 变成 *），人没动它就不该算改了频率。
 *
 * Webhook 按约定：地址省略表示不改、空串表示删除；密钥省略表示不改、空串表示清除。要交 Webhook 时格式和事件
 * 总是一起交上，免得服务端把没写的当成清空
 */
export function patchBody(f: ScheduleForm, initial: ScheduleForm, saved: Schedule): Omit<SchedulePatch, 'version'> | null {
  const out: Omit<SchedulePatch, 'version'> = {}
  if (f.name.trim() !== initial.name.trim()) out.name = f.name.trim()
  if (f.workflowId !== initial.workflowId) out.workflow_id = f.workflowId
  if (cronOf(f.freq) !== cronOf(initial.freq)) out.cron = cronOf(f.freq)
  if (f.timezone !== initial.timezone) out.timezone = f.timezone
  if (f.runClass !== initial.runClass) out.run_class = f.runClass
  if (!sameJson(sortedEntries(templateOf(f.inputs)), sortedEntries(templateOf(initial.inputs)))) {
    out.input_template = templateOf(f.inputs)
  }
  if (f.misfireGraceS !== initial.misfireGraceS) out.misfire_grace_s = f.misfireGraceS
  const webhook = webhookPatch(f.webhook, initial.webhook, saved)
  if (webhook) out.webhook = webhook
  return Object.keys(out).length ? out : null
}

function webhookPatch(w: WebhookForm, before: WebhookForm, saved: Schedule): ScheduleWebhookIn | null {
  const events = sortEvents(w.events)
  if (!saved.webhook) {
    // 原来没有：打开了才交，地址必填
    if (!w.on) return null
    return { format: w.format, url: w.url.trim(), ...(w.secret ? { secret: w.secret } : {}), events }
  }
  if (!w.on) return { format: saved.webhook.format, url: '', events: sortEvents(saved.webhook.events) }
  const out: ScheduleWebhookIn = { format: w.format, events }
  if (w.replaceUrl && w.url.trim()) out.url = w.url.trim()
  if (saved.webhook.has_secret) {
    if (w.secretMode === 'clear') out.secret = ''
    else if (w.secretMode === 'replace' && w.secret) out.secret = w.secret
  } else if (w.secret) {
    out.secret = w.secret
  }
  const changed = out.url != null || out.secret != null || w.format !== before.format
    || !sameJson(events, sortEvents(before.events))
  return changed ? out : null
}

// ---------------------------------------------------------------------------
// 表单的问题
// ---------------------------------------------------------------------------

export type FormField = 'name' | 'workflow' | 'freq' | 'timezone' | 'url' | 'secret' | 'events'

/** 表单哪几项还不能保存，值是写在那一项下面的话。saved 是修改中的定时任务（新建时为 null） */
export function formProblems(f: ScheduleForm, saved: Schedule | null): Partial<Record<FormField, string>> {
  const out: Partial<Record<FormField, string>> = {}
  if (!f.name.trim()) out.name = T.problem.name
  if (!f.workflowId) out.workflow = T.problem.workflow
  const freq = freqProblem(f.freq)
  if (freq) out.freq = freq
  if (!f.timezone) out.timezone = T.problem.timezone
  const w = f.webhook
  if (w.on) {
    const needUrl = !saved?.webhook || w.replaceUrl
    if (needUrl && !w.url.trim()) out.url = T.problem.url
    else if (needUrl && !isHttpUrl(w.url)) out.url = T.problem.urlInvalid
    if (saved?.webhook?.has_secret && w.secretMode === 'replace' && !w.secret) out.secret = T.problem.secret
    if (!w.events.length) out.events = T.problem.events
  }
  return out
}

/** 频率填得哪里不完整；完整时返回 null（cron 合不合法由服务端的预览判） */
export function freqProblem(f: Freq): string | null {
  if (f.preset === 'custom') {
    const n = f.cron.trim().split(/\s+/).filter(Boolean).length
    if (!n) return T.problem.cronEmpty
    return n === 5 ? null : T.problem.cronFields
  }
  if (f.preset === 'hourly') return cronOf(f) ? null : T.problem.minute
  if (!parseTime(f.time)) return T.problem.time
  if (f.preset === 'weekly' && !f.weekdays.length) return T.problem.weekdays
  return null
}

/** 以 http:// 或 https:// 开头、中间没有空白的地址（Webhook、服务访问地址都用它） */
export function isHttpUrl(text: string): boolean {
  return /^https?:\/\/\S+$/i.test(text.trim())
}
