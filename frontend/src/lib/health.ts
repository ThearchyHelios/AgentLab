import { useSyncExternalStore } from 'react'
import { toast } from '../components/ui'
import type { ServerHealth } from '../types'
import { humanizeError } from './errors'
import { parseServerTime } from './format'

/**
 * 连没连上：统一、持久的状态。
 *
 * 一次「测连接」的结果按 `种类:id` 存：模型接入 provider:<id>、数据源
 * datasource:<id>、MCP mcp:<id>。
 *
 * 以前测完只弹 4 秒 toast，结果存在组件 state 里，切个标签就没了；卡片上的圆点
 * 又各说各的（模型卡的绿点其实是「启用」）。管理页最核心的问题是「现在能不能
 * 用」，答案得留在对象上，并注明是什么时候测的。
 *
 * 模块级而不是组件 state：切标签、换页都还在。再写一份到 localStorage，刷新
 * 也还在——只是这台浏览器的缓存，所以一律带上「几分钟前测的」。后端也记着上次
 * 的结果时（列表接口里的 last_check_*），用 healthFromServer 取出来当初值，
 * useHealth 取两边较新的那份。
 */
export interface HealthRecord {
  ok: boolean
  /** 往返毫秒 */
  ms?: number | null
  /** 测的时刻（ms）。0 = 知道结果、不知道是什么时候测的 */
  at: number
  error?: string
  hint?: string
  detail?: string
  /** 成功时的补充：用的哪个模型、回了什么 */
  note?: string
}

const HEALTH_KEY = 'agentlab.health'

/**
 * 后端记着的上次结果（列表接口里平铺的 last_check_* 四个字段）→ HealthRecord。
 * 没测过、或者连接配置改过之后后端给的全是 null，这里返回 undefined。
 * 测的时刻解析不出来时 at 为 0：知道结果，不写「几分钟前测」
 */
export function healthFromServer(o: ServerHealth | null | undefined): HealthRecord | undefined {
  if (!o || typeof o.last_check_ok !== 'boolean') return undefined
  return {
    ok: o.last_check_ok,
    ms: o.last_latency_ms ?? null,
    at: parseServerTime(o.last_checked_at)?.getTime() ?? 0,
    ...(o.last_error ? { error: o.last_error } : {}),
  }
}

function readHealth(): Record<string, HealthRecord> {
  try {
    const raw = JSON.parse(localStorage.getItem(HEALTH_KEY) ?? '{}')
    return raw && typeof raw === 'object' ? raw : {}
  } catch {
    return {}
  }
}

let healthSnap: { records: Record<string, HealthRecord>; checking: Record<string, number> } = {
  records: readHealth(), checking: {},
}
const healthListeners = new Set<() => void>()
const subscribeHealth = (l: () => void) => {
  healthListeners.add(l)
  return () => { healthListeners.delete(l) }
}
function emitHealth(next: Partial<typeof healthSnap>, persist = false) {
  healthSnap = { ...healthSnap, ...next }
  if (persist) {
    try { localStorage.setItem(HEALTH_KEY, JSON.stringify(healthSnap.records)) } catch { /* 隐私模式：只是刷新后不记得 */ }
  }
  healthListeners.forEach((l) => l())
}

/**
 * 某个对象最近一次的测连接结果，以及是不是正在测。
 *
 * server 是后端记着的那份（healthFromServer 的结果）：换了台浏览器、清了缓存也
 * 还在。两份都有时取测得晚的那份——本机刚测过的比后端上次记的新。
 */
export function useHealth(key: string, server?: HealthRecord | null): { record?: HealthRecord; checkingSince?: number } {
  const snap = useSyncExternalStore(subscribeHealth, () => healthSnap, () => healthSnap)
  const local = snap.records[key]
  const record = !server ? local : !local ? server : server.at > local.at ? server : local
  return { record, checkingSince: snap.checking[key] }
}

export function setHealth(key: string, record: HealthRecord) {
  emitHealth({ records: { ...healthSnap.records, [key]: record } }, true)
}

export function forgetHealth(key: string) {
  const { [key]: _gone, ...rest } = healthSnap.records
  emitHealth({ records: rest }, true)
}

/**
 * 测一次并记下结果。run 返回后端的 {ok, error, hint, detail}；ms 和 note 由调用方
 * 从各自的字段里取（latency_ms / elapsed_ms）。
 *
 * 后端本身够不着时不记：那说的是「我们连不上后端」，不是这个库或模型坏了，
 * 记成它的失败就是冤枉它。只弹 toast。
 */
export async function checkHealth(
  key: string,
  run: () => Promise<Omit<HealthRecord, 'at'>>,
): Promise<HealthRecord | null> {
  if (healthSnap.checking[key]) return null
  emitHealth({ checking: { ...healthSnap.checking, [key]: Date.now() } })
  let record: HealthRecord | null = null
  try {
    record = { ...(await run()), at: Date.now() }
  } catch (e) {
    const h = humanizeError(e)
    if (h.kind === 'network') {
      toast.error(e)
    } else {
      record = {
        ok: false, at: Date.now(),
        error: h.reason ? `${h.title}：${h.reason}` : h.title, hint: h.action, detail: h.raw,
      }
    }
  } finally {
    const { [key]: _done, ...checking } = healthSnap.checking
    emitHealth({
      checking,
      ...(record ? { records: { ...healthSnap.records, [key]: record } } : {}),
    }, !!record)
  }
  return record
}
