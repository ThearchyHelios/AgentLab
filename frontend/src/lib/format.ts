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

/** 短 id：「#66a5a6」。完整 id 放 title，点击复制由调用方决定 */
export function shortId(id: string | null | undefined, len = 6): string {
  if (!id) return NONE
  return `#${id.slice(0, len)}`
}
