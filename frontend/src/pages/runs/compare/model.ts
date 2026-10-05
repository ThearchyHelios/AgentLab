import { ApiError } from '../../../api/client'
import { NONE } from '../../../lib/format'
import type { RunCompareClass, RunCompareMetric, RunCompareQuery } from '../../../types'

/**
 * 运行对比页的纯函数：分类的顺序和颜色、数怎么写、查询怎么编号、错误归哪一类。数值本身都是服务端算好的
 * （渲染值原样显示），这里只管服务端没替界面写好的那几样：变化百分比、占比、输入和归因里的原始数。
 */

/** 摘要条和分类的顺序：要人看的变化在前，未变放最后 */
export const CLASS_ORDER: RunCompareClass[] = [
  'data_only', 'caliber_and_data', 'sql_changed', 'params_only', 'added', 'removed', 'incomparable', 'unchanged',
]

/**
 * 分类的颜色。颜色只表达「要不要多看一眼」：口径变了、SQL 结构变了是警示色并铺底（数不能直接比）；只数据变、新增
 * 用强调色；只参数变、移除、无法比较、未变是中性色。标签上都写着字，去掉颜色也分得出
 */
const CLASS_TONE: Record<RunCompareClass, { color: string; soft: boolean }> = {
  data_only: { color: 'var(--accent)', soft: false },
  caliber_and_data: { color: 'var(--warn)', soft: true },
  sql_changed: { color: 'var(--warn)', soft: true },
  params_only: { color: 'var(--text-dim)', soft: false },
  added: { color: 'var(--accent)', soft: false },
  removed: { color: 'var(--text-dim)', soft: false },
  incomparable: { color: 'var(--text-faint)', soft: false },
  unchanged: { color: 'var(--text-faint)', soft: false },
}

export function classTone(k: string): { color: string; soft: boolean } {
  return CLASS_TONE[k as RunCompareClass] ?? { color: 'var(--text-dim)', soft: false }
}

/** 一个指标在表里的键：同一张卡里指标 id 唯一，几张卡之间可能重名 */
export const metricKey = (m: Pick<RunCompareMetric, 'card_id' | 'metric_id'>) => `${m.card_id}:${m.metric_id}`

/** 一条查询的键：同一节点里按服务端的序号区分 */
export const queryKey = (q: Pick<RunCompareQuery, 'node_id' | 'ordinal'>) => `${q.node_id}:${q.ordinal}`

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

/** 原始的数（输入、归因里的值）：千分位，小数最多四位。服务端没给渲染值的地方用它 */
export function plainNumber(v: number): string {
  return v.toLocaleString('en-US', { maximumFractionDigits: 4 })
}

/** 输入、归因键里的一个值：数照原样加千分位，缺的写「—」，列表和对象压成一行 */
export function valueText(v: unknown): string {
  if (v == null) return NONE
  if (isNum(v)) return plainNumber(v)
  if (typeof v === 'boolean') return v ? 'true' : 'false'
  if (typeof v === 'string') return v
  try {
    const s = JSON.stringify(v)
    return s.length > 120 ? `${s.slice(0, 119)}…` : s
  } catch {
    return String(v)
  }
}

/** 带正负号的原始变化量：「+1,012.6」「-3」「0」 */
export function signedNumber(v: number | null | undefined): string {
  if (!isNum(v)) return NONE
  if (v === 0) return '0'
  return `${v > 0 ? '+' : '-'}${plainNumber(Math.abs(v))}`
}

/**
 * 小数写成百分比（一位小数）。signed 时带正负号：变化比例 0.125 →「+12.5%」；占比 0.4 →「40.0%」。
 * 不是零、却小到一位小数写不出来的写「<0.1%」，不写成看着像没变的「0.0%」
 */
export function percent(v: number | null | undefined, signed = false): string {
  if (!isNum(v)) return NONE
  const pct = Math.abs(v) * 100
  const sign = signed && v !== 0 ? (v > 0 ? '+' : '-') : v < 0 ? '-' : ''
  if (v !== 0 && pct < 0.05) return `${sign}<0.1%`
  return `${sign}${pct.toFixed(1)}%`
}

/** 两个值算不算一样（输入对比里标「变了」用）：数按值比，别的按 JSON 比 */
export function sameValue(a: unknown, b: unknown): boolean {
  if (isNum(a) && isNum(b)) return a === b
  try {
    return JSON.stringify(a) === JSON.stringify(b)
  } catch {
    return a === b
  }
}

/**
 * 同一节点有几条查询时，界面按 1 起数写「第 N 条」。服务端的 ordinal 只用来排先后：它从 0 还是从 1 数、中间有没有
 * 空号（只有一边有的那条），都不该漏到界面上
 */
export function queryNumbers(queries: RunCompareQuery[]): Map<string, number | null> {
  const byNode = new Map<string, RunCompareQuery[]>()
  for (const q of queries) byNode.set(q.node_id, [...(byNode.get(q.node_id) ?? []), q])
  const out = new Map<string, number | null>()
  for (const list of byNode.values()) {
    const sorted = [...list].sort((a, b) => a.ordinal - b.ordinal)
    sorted.forEach((q, i) => out.set(queryKey(q), list.length > 1 ? i + 1 : null))
  }
  return out
}

/**
 * 对比失败归哪一类：409 未封存或封存核对不一致、422 不是同一工作流或同一次运行、404 运行不存在；找默认基准时的 404
 * 是「找不到基准」。其余（网络、服务端出错）按通用的出错态画，带重试
 */
export type CompareErrorKind = '409' | '422' | '404' | 'no-base' | 'other'

export function compareErrorKind(e: unknown, finding: boolean): CompareErrorKind {
  if (!(e instanceof ApiError) || e.kind !== 'http') return 'other'
  if (finding) return e.status === 404 ? 'no-base' : 'other'
  if (e.status === 409) return '409'
  if (e.status === 422) return '422'
  if (e.status === 404) return '404'
  return 'other'
}
