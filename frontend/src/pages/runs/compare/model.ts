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

/**
 * 「拆分或原因」一栏没有拆分时写的那句话：服务端写了「为什么没拆」就用它。无法比较、新增、移除的服务端不写这一项，
 * 原因只在 reasons 里——退回写 reasons 的第一句（全部在展开的变化说明里），不让这一栏只剩一个「—」、要展开才知道
 * 为什么。SQL 结构变的服务端一向写着，万一没写，reasons 说的就是哪条查询变了，也是不拆的原因。只数据变、只参数变、
 * 未变的本来就用不着拆；口径与数据都变的服务端总会给拆分或原因，它的 reasons 说的是口径改了什么、不是为什么不拆，
 * 都不退回
 */
const REASON_IN_CELL = new Set<string>(['incomparable', 'added', 'removed', 'sql_changed'])

export function splitReason(m: Pick<RunCompareMetric, 'classification' | 'decomposition_note' | 'reasons'>): string | null {
  if (m.decomposition_note) return m.decomposition_note
  return REASON_IN_CELL.has(m.classification) && m.reasons?.length ? m.reasons[0] : null
}

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

/** 百分数（已经乘过 100）写成一位小数；不是零、却小到一位小数写不出来的写「<0.1%」，不写成看着像没变的「0.0%」 */
function pct(abs: number, sign: string): string {
  return abs !== 0 && abs < 0.05 ? `${sign}<0.1%` : `${sign}${abs.toFixed(1)}%`
}

/** 变化百分比：服务端给的 delta_pct 已经乘过 100（12.5 是涨了 12.5%），带正负号：「+12.5%」「-3.0%」 */
export function changeText(v: number | null | undefined): string {
  if (!isNum(v)) return NONE
  return pct(Math.abs(v), v > 0 ? '+' : v < 0 ? '-' : '')
}

/** 归因的占比：服务端给的是小数（0.4 是 40%）。和总变化反向的一项占比是负的：「-5.0%」 */
export function shareText(v: number | null | undefined): string {
  if (!isNum(v)) return NONE
  return pct(Math.abs(v) * 100, v < 0 ? '-' : '')
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

/** 查询区的一项：位置键（同一对比里唯一）和同一节点里的「第 N 条」（节点只有一条时为 null） */
export interface QueryItem {
  q: RunCompareQuery
  key: string
  nth: number | null
}

/**
 * 查询区按服务端给的先后列：服务端已经按本次的先后排好，基准多出来的接在后面。同一节点有几条时，按在列表里出现的
 * 先后从 1 数「第 N 条」，键是「节点:第几」。
 *
 * 不拿服务端的 ordinal 当键、也不按它编号：配上的一对取本次的序号，落单的基准查询取基准的序号，Agent 换了查询的先后
 * 时两者会撞上（同一节点两条都是 1），按它当键 React 会报键冲突、编号会重复或缺号
 */
export function queryItems(queries: RunCompareQuery[]): QueryItem[] {
  const total = new Map<string, number>()
  for (const q of queries) total.set(q.node_id, (total.get(q.node_id) ?? 0) + 1)
  const seen = new Map<string, number>()
  return queries.map((q) => {
    const n = (seen.get(q.node_id) ?? 0) + 1
    seen.set(q.node_id, n)
    return { q, key: `${q.node_id}:${n}`, nth: (total.get(q.node_id) ?? 0) > 1 ? n : null }
  })
}

/**
 * 对比失败归哪一类：409 未封存或封存核对不一致、422 不是同一工作流或同一次运行、404 运行不存在；找默认基准时的 404
 * 是「找不到基准」。其余（网络、服务端出错）按通用的出错态画，带重试
 */
export type CompareErrorKind = '409' | '422' | '404' | 'no-base' | 'other'

/** 服务端「运行记录不存在」的机读码（api/evidence.py 的 RUN_NOT_FOUND，运行对比的接口沿用） */
const RUN_NOT_FOUND = 'run_not_found'
/** 409 的两种机读码（api/compare.py）：没结束封存、封存核对不一致。标题按它分开写 */
export const UNSEALED = 'compare_unsealed'
export const SEAL_BROKEN = 'compare_seal_broken'

/** 请求回的是 404（记录不存在）：和连不上、服务端出错分开说，前者重试也没用 */
export function isGone(e: unknown): boolean {
  return e instanceof ApiError && e.kind === 'http' && e.status === 404
}

export function compareErrorKind(e: unknown, finding: boolean): CompareErrorKind {
  if (!(e instanceof ApiError) || e.kind !== 'http') return 'other'
  // 找基准时的 404 有两种：本次这条运行就不存在（机读码 run_not_found），和找不到可比的上一次
  if (finding && e.status === 404) return e.code === RUN_NOT_FOUND ? '404' : 'no-base'
  if (finding) return 'other'
  if (e.status === 409) return '409'
  if (e.status === 422) return '422'
  if (e.status === 404) return '404'
  return 'other'
}
