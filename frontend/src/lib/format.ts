/**
 * 数字、时长、时间的唯一一份格式化。
 *
 * 之前有三套并行：decode 的 formatDuration 写「1m14s」，AssistantStream 的 fmtMs
 * 注释说"同一套规则"却写「2min」，各页面直接拼 `${ms}ms` 出现「12345ms」；token
 * 没有千分位，成本固定四位小数。数字是这个产品"可计量、可审计"的门面，同一个
 * 耗时两处说法不一，人就会怀疑数据本身。
 *
 * 约定：拿不到的数一律写「—」，不写 0，也不猜。显示这些数字的地方都要配
 * tabular-nums（.tnum），刷新时宽度才不跳。
 */

export const NONE = '—'

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

const pad2 = (n: number) => String(n).padStart(2, '0')

/**
 * 耗时：「820 ms」「7.6 s」「1 分 14 秒」「1 小时 02 分」。
 *
 * 先按目标精度取整再判断落在哪一档，否则 59.96 秒会写成「60.0 s」、119.6 秒会
 * 写成「1m60s」（旧实现的边界 bug：秒数单独四舍五入，可能进到 60）。
 */
export function formatDuration(ms: number | null | undefined): string {
  if (!isNum(ms) || ms < 0) return NONE
  const whole = Math.round(ms)
  if (whole < 1000) return `${whole} ms`
  const tenths = Math.round(ms / 100)
  if (tenths < 600) return `${(tenths / 10).toFixed(1)} s`
  const secs = Math.round(ms / 1000)
  if (secs < 3600) return `${Math.floor(secs / 60)} 分 ${pad2(secs % 60)} 秒`
  return `${Math.floor(secs / 3600)} 小时 ${pad2(Math.floor((secs % 3600) / 60))} 分`
}

const DAY_MS = 86_400_000

/**
 * 耗时，能跨天。一天以内同 formatDuration；一天以上写「8 天 02 小时」——审批
 * 挂了一周的运行，「192 小时 05 分」没人会去换算。coarse 只要最大的单位：
 * 「已等 8 天」。记录页、时间轴、HUD、右栏共用这一份，同一段等待各处说法一致。
 */
export function formatSpan(ms: number | null | undefined, opts?: { coarse?: boolean }): string {
  if (!isNum(ms) || ms < 0) return NONE
  if (ms >= DAY_MS) {
    const d = Math.floor(ms / DAY_MS)
    const h = Math.floor((ms % DAY_MS) / 3_600_000)
    return opts?.coarse || h === 0 ? `${d} 天` : `${d} 天 ${pad2(h)} 小时`
  }
  if (opts?.coarse) {
    if (ms >= 3_600_000) return `${Math.floor(ms / 3_600_000)} 小时`
    if (ms >= 60_000) return `${Math.floor(ms / 60_000)} 分钟`
    return '不到 1 分钟'
  }
  return formatDuration(ms)
}

/**
 * 一段时长的读数（已等、已跑、等人）。一小时以内是钟面「25:00.3」，和计时器同一种写法；
 * 一小时以上换成 formatSpan 的「3 小时 05 分」「8 天 23 小时」——审批挂了九天，
 * 「215:56:48.6」得自己除 24，而记录页同一段等待写的是「8 天 23 小时」。
 * 画布卡片、胶囊、坞头、泳道、右栏的用量行都走这一份，同一刻写同一个数。
 * tenth=false 去掉十分位：停下的读数、成员行这些地方十分位只会让一排数字一直在抖
 */
export function formatLapse(ms: number | null | undefined, tenth = true): string {
  if (!isNum(ms) || ms < 0) return NONE
  if (ms >= 3_600_000) return formatSpan(ms)
  const clock = formatClock(ms)
  return tenth ? clock : clock.replace(/\.\d$/, '')
}

/**
 * 相对开始的时刻（不带「T+」）。一天以内是秒表读数「05:03.4」「3:05:03.4」；
 * 过了一天写「9 天 00:38:26」：「216:38:26.2」得自己除 24，而同一行的墙钟、等人
 * 写的是「9 天」。十分之一秒放不下了，隔了几天也没人要它
 */
export function formatOffset(ms: number | null | undefined): string {
  if (!isNum(ms) || ms < 0) return NONE
  if (ms < DAY_MS) return formatClock(ms)
  const secs = Math.floor((ms % DAY_MS) / 1000)
  return `${Math.floor(ms / DAY_MS)} 天 ${pad2(Math.floor(secs / 3600))}:${pad2(Math.floor(secs / 60) % 60)}:${pad2(secs % 60)}`
}

/**
 * 实时计时器：「01:14.3」，一小时以上「1:02:03.4」。固定位数，配 tabular-nums
 * 跳字时不抖。截断而不是四舍五入：计时器读数不该跑在真实时间前面。
 */
export function formatClock(ms: number | null | undefined): string {
  if (!isNum(ms) || ms < 0) return NONE
  const t = Math.floor(ms / 100)
  const tenth = t % 10
  const secs = Math.floor(t / 10)
  const s = secs % 60
  const m = Math.floor(secs / 60) % 60
  const h = Math.floor(secs / 3600)
  return h > 0 ? `${h}:${pad2(m)}:${pad2(s)}.${tenth}` : `${pad2(m)}:${pad2(s)}.${tenth}`
}

/** 带千分位的整数：「56,034」 */
export function formatNumber(n: number | null | undefined): string {
  if (!isNum(n)) return NONE
  return Math.round(n).toLocaleString('en-US')
}

function compact(n: number): string {
  const abs = Math.abs(n)
  if (abs < 1000) return String(Math.round(n))
  // 先取整到一位小数再判断档位：999,950 应当是「1.0M」而不是「1000.0k」
  const k = Math.round(n / 100) / 10
  if (Math.abs(k) < 1000) return `${k.toFixed(1)}k`
  const m = Math.round(n / 100_000) / 10
  return `${m.toFixed(1)}M`
}

/** tokens：「56,034 tokens」；紧凑场景「56.0k tok」 */
export function formatTokens(n: number | null | undefined, opts?: { compact?: boolean }): string {
  if (!isNum(n)) return NONE
  return opts?.compact ? `${compact(n)} tok` : `${formatNumber(n)} tokens`
}

/**
 * 成本：「$0.031」，一美元以上「$1.24」，不足 0.001 写「<$0.001」。
 * 正好为 0（本地模型、没有计价）写「$0」——写成「<$0.001」会让人以为花了钱。
 */
export function formatCost(usd: number | null | undefined): string {
  if (!isNum(usd)) return NONE
  if (usd === 0) return '$0'
  if (usd < 0.001) return '<$0.001'
  if (usd < 1) return `$${usd.toFixed(3)}`
  return `$${usd.toFixed(2)}`
}

/**
 * 解析服务器给的时间。
 *
 * SQLite 会丢时区，后端吐出来的 '2026-09-26T01:11:19.891698' 其实是 UTC；
 * JS 把不带偏移的 ISO 串当本地时间，于是全站慢 8 小时。没有 Z 也没有偏移的
 * 一律补 Z。数字按 epoch 处理：事件的 ts 是秒（带小数），小于 1e12 的当秒。
 */
export function parseServerTime(v: string | number | Date | null | undefined): Date | null {
  if (v == null || v === '') return null
  if (v instanceof Date) return Number.isNaN(v.getTime()) ? null : v
  if (typeof v === 'number') {
    if (!Number.isFinite(v)) return null
    return new Date(v < 1e12 ? v * 1000 : v)
  }
  let s = v.trim()
  // 只有日期没有时间的串（'2026-09-26'）JS 本来就按 UTC 解析，不用补
  if (/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(s) && !/(Z|[+-]\d{2}:?\d{2})$/i.test(s)) {
    s = s.replace(' ', 'T') + 'Z'
  }
  const d = new Date(s)
  return Number.isNaN(d.getTime()) ? null : d
}

const sameDay = (a: Date, b: Date) =>
  a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()

const hm = (d: Date) => `${pad2(d.getHours())}:${pad2(d.getMinutes())}`

/**
 * 列表里的时间：今天只写「10:05」，今年更早的写「9/25 12:52」，跨年的写
 * 「2025/9/25 12:52」。完整时间放进 title，用 formatDateTime。
 */
export function formatTime(v: string | number | Date | null | undefined, now: Date = new Date()): string {
  const d = parseServerTime(v)
  if (!d) return NONE
  if (sameDay(d, now)) return hm(d)
  const md = `${d.getMonth() + 1}/${d.getDate()} ${hm(d)}`
  return d.getFullYear() === now.getFullYear() ? md : `${d.getFullYear()}/${md}`
}

function tzLabel(d: Date): string {
  const off = -d.getTimezoneOffset()
  const sign = off >= 0 ? '+' : '-'
  const abs = Math.abs(off)
  return `UTC${sign}${pad2(Math.floor(abs / 60))}:${pad2(abs % 60)}`
}

/**
 * 完整时间：「2026-09-26 10:05:12 (UTC+08:00)」。给 title 和审计场景用——要能
 * 和外部系统日志、审批时间逐秒对上，所以带秒、带时区。
 */
export function formatDateTime(v: string | number | Date | null | undefined): string {
  const d = parseServerTime(v)
  if (!d) return NONE
  const date = `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`
  return `${date} ${hm(d)}:${pad2(d.getSeconds())} (${tzLabel(d)})`
}

/** 按天分组的组名：「今天」「昨天」「9 月 24 日」，跨年带年份 */
export function formatDay(v: string | number | Date | null | undefined, now: Date = new Date()): string {
  const d = parseServerTime(v)
  if (!d) return NONE
  if (sameDay(d, now)) return '今天'
  const y = new Date(now)
  y.setDate(now.getDate() - 1)
  if (sameDay(d, y)) return '昨天'
  const md = `${d.getMonth() + 1} 月 ${d.getDate()} 日`
  return d.getFullYear() === now.getFullYear() ? md : `${d.getFullYear()} 年 ${md}`
}

/**
 * 相对时间：「刚刚」「52 分钟前」「3 小时前」，超过一天回落到 formatTime。
 * 只用在"新鲜度"有意义的地方（最后连通、最近召回），审计时间用绝对时间。
 */
export function formatRelative(v: string | number | Date | null | undefined, now: Date = new Date()): string {
  const d = parseServerTime(v)
  if (!d) return NONE
  const diff = now.getTime() - d.getTime()
  if (diff < 0) return formatTime(d, now)
  if (diff < 45_000) return '刚刚'
  // 先取整再判档，和 formatDuration 同理：59.6 分钟按分钟取整是「60 分钟前」
  const m = Math.round(diff / 60_000)
  if (m < 60) return `${Math.max(1, m)} 分钟前`
  const h = Math.round(diff / 3_600_000)
  if (h < 24) return `${h} 小时前`
  return formatTime(d, now)
}

/** 文件大小：「820 B」「12.4 KB」「3.1 MB」「1.2 GB」 */
export function formatBytes(n: number | null | undefined): string {
  if (!isNum(n) || n < 0) return NONE
  if (n < 1024) return `${Math.round(n)} B`
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`
  return `${(n / 1024 ** 3).toFixed(1)} GB`
}

/** 去掉名字后面括号里的补充说明：「OpenAI 兼容（DeepSeek、通义…）」→「OpenAI 兼容」 */
export const shortLabel = (label?: string | null): string => (label ?? '').replace(/[（(].*$/, '').trim()

/** 短 id：「#66a5a6」。完整 id 放 title，点击复制由调用方决定 */
export function shortId(id: string | null | undefined, len = 6): string {
  if (!id) return NONE
  return `#${id.slice(0, len)}`
}
