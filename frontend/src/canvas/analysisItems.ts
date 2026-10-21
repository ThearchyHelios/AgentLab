// ===========================================================================
// 分析卡（节点类型 analysis）的一项：每种分析在检查器里给哪些参数格、参数怎么在格子和配置之间转换、卡片上的摘要。
//
// 参数名和服务端 tools/builtin/analysis.py 各参数模型里的字段一致（分析卡按同一份参数模型校验，engine/schema.analysis_issues）：
// 主输入写在 from（对应 Agent 工具的 query），基期、本期是两份查询时写在 params 的 base_query、target_query；阈值不写在参数里，
// 统一写在卡上的阈值覆盖。这里只决定界面上摆哪几格、按什么先后，规则以服务端校验为准（写错了检查器照服务端的说法提示）。
// ===========================================================================

import { ANALYSIS_MODES, ANALYSIS_TEXT, analysisMethodLabel, isAnalysisOp, type AnalysisOp } from '../lib/termsAnalysis'
import type { AnalysisItem } from '../types'

/** 一格参数的写法：列名、几列（顿号、逗号分隔）、期次的值（可以写 {{ }}）、整数、两种下拉、几对 */
export type ParamKind = 'column' | 'columns' | 'value' | 'int' | 'basis' | 'grain' | 'pairs'

export interface ParamSpec {
  key: string
  kind: ParamKind
}

const col = (key: string): ParamSpec => ({ key, kind: 'column' })
const cols = (key: string): ParamSpec => ({ key, kind: 'columns' })
const val = (key: string): ParamSpec => ({ key, kind: 'value' })

/** 两期写在同一份查询里时的期次三格（期次列、基期、本期） */
const PERIODS: ParamSpec[] = [col('period'), val('base'), val('target')]

/**
 * 每种分析（算子 + 模式）摆哪几格，按这个先后。总数核对（headline）、下钻（parent）、两份查询另有自己的一块，不在这里。
 * label（度量的叫法）放在最后：留空用列名或数据目录里的名字
 */
export function paramSpecs(op: AnalysisOp, mode?: string | null): ParamSpec[] {
  switch (op) {
    case 'decompose':
      if (mode === 'ratio') return [cols('by'), ...PERIODS, col('numerator'), col('denominator'), val('label')]
      if (mode === 'factor') return [...PERIODS, col('product'), cols('factors'), val('label')]
      return [cols('by'), ...PERIODS, col('measure'), val('label')]
    case 'baseline':
      return [col('period'), col('measure'), val('target'), { key: 'basis', kind: 'basis' }, { key: 'grain', kind: 'grain' },
        { key: 'lag', kind: 'int' }, val('label')]
    case 'trend':
      return [col('period'), col('measure'), { key: 'grain', kind: 'grain' }, val('label')]
    case 'differ': {
      const values = mode === 'mean' ? [col('mean'), col('sd'), col('sum'), col('sumsq'), col('n')]
        : mode === 'rows' ? [col('value')] : [col('numerator'), col('denominator')]
      return [cols('by'), val('reference'), { key: 'pairs', kind: 'pairs' }, ...values, ...(mode === 'rows' ? [] : PERIODS),
        val('label')]
    }
    case 'comparable':
      return [...PERIODS, col('measure'), { key: 'grain', kind: 'grain' }]
  }
}

/** 这种分析能不能写成基期、本期两份查询（DecomposeArgs、ComparableArgs 有 base_query / target_query） */
export const allowsTwoQueries = (op: AnalysisOp): boolean => op === 'decompose' || op === 'comparable'
/** 有总数核对（headline）的：可加、比率的变化拆解 */
export const allowsHeadline = (op: AnalysisOp, mode?: string | null): boolean =>
  op === 'decompose' && mode !== 'factor'
/** 能下钻（parent）的：可加、比率的变化拆解（乘积拆解不按维度拆） */
export const allowsParent = allowsHeadline

/** 两份查询时期次三格不写（两份各自只有一期） */
export const TWO_QUERY_SKIP = new Set(['period', 'base', 'target'])

/** 这种分析会写到的全部参数键：换分析方法、模式时，这几样以外的参数去掉（服务端按多写的参数报错） */
export function paramKeys(op: AnalysisOp, mode?: string | null): Set<string> {
  const keys = new Set(paramSpecs(op, mode).map((p) => p.key))
  if (allowsTwoQueries(op)) { keys.add('base_query'); keys.add('target_query') }
  if (allowsHeadline(op, mode)) keys.add('headline')
  if (allowsParent(op, mode)) keys.add('parent')
  return keys
}

/** 「region、channel」「region, channel」→ ['region', 'channel'] */
export function splitList(text: string): string[] {
  return text.split(/[、,，\s]+/).map((s) => s.trim()).filter(Boolean)
}

/** 配置里的一格 → 格子里的字 */
export function paramText(kind: ParamKind, value: unknown): string {
  if (value == null) return ''
  if (kind === 'columns') return Array.isArray(value) ? value.map(String).join('、') : String(value)
  if (kind === 'pairs') {
    return Array.isArray(value)
      ? value.map((p) => (Array.isArray(p) ? p.map(String).join('、') : String(p))).join('\n')
      : String(value)
  }
  return String(value)
}

/** 格子里的字 → 配置里的一格；空的返回 undefined（参数里不留这个键） */
export function paramValue(kind: ParamKind, text: string): unknown {
  const t = text.trim()
  if (!t) return undefined
  if (kind === 'columns') return splitList(t)
  if (kind === 'pairs') return t.split('\n').map((line) => splitList(line)).filter((p) => p.length)
  // 整数格写了不是整数的字照原样交给服务端校验（它会说清该写什么），不在这里悄悄丢掉
  if (kind === 'int') return /^-?\d+$/.test(t) ? Number(t) : t
  return t
}

/** 配置里的 items → 一项一项（不是对象的项原样留着，服务端会报「格式有误」） */
export function itemsOf(value: unknown): AnalysisItem[] {
  return Array.isArray(value) ? value.map((v) => (v && typeof v === 'object' ? v as AnalysisItem : {})) : []
}

/** 标识的写法（服务端 schema.ANALYSIS_ITEM_ID）：下游按 nodes.<卡>.results.<标识> 取判定 */
export const ITEM_ID = /^[A-Za-z_][A-Za-z0-9_]*$/

/** 新加一项的标识：a1、a2……不和已有的重名 */
export function nextItemId(items: AnalysisItem[]): string {
  const taken = new Set(items.map((i) => String(i.id ?? '')))
  for (let k = items.length + 1; ; k += 1) if (!taken.has(`a${k}`)) return `a${k}`
}

/** 换分析方法：有模式的取第一种模式，参数只留新方法用得到的，输入和假设留着 */
export function withOp(item: AnalysisItem, op: string): AnalysisItem {
  if (!isAnalysisOp(op)) return { ...item, op, mode: undefined }
  const mode = ANALYSIS_MODES[op]?.[0]
  return withParams({ ...item, op, mode }, op, mode)
}

/** 换模式：参数只留这种模式用得到的 */
export function withMode(item: AnalysisItem, mode: string): AnalysisItem {
  return isAnalysisOp(item.op) ? withParams({ ...item, mode }, item.op, mode) : { ...item, mode }
}

function withParams(item: AnalysisItem, op: AnalysisOp, mode: string | null | undefined): AnalysisItem {
  const keep = paramKeys(op, mode)
  const params = Object.fromEntries(Object.entries(item.params ?? {}).filter(([k]) => keep.has(k)))
  const next: AnalysisItem = { ...item, params }
  if (!ANALYSIS_MODES[op]) delete next.mode
  return next
}

/** 卡片正文的摘要：「波动基线、变化拆解（可加）」，超过三项写「等 N 项」 */
export function analysisCardSummary(config: Record<string, any>): string {
  const items = itemsOf(config.items)
  if (!items.length) return ANALYSIS_TEXT.node.summaryEmpty
  const names = items.map((i) => (i.op ? analysisMethodLabel(i.op, i.mode) : ANALYSIS_TEXT.node.summaryNoOp))
  const head = names.slice(0, 3).join('、')
  return names.length > 3 ? `${head}${ANALYSIS_TEXT.node.summaryMore(names.length)}` : head
}

interface CardNodeLike {
  id: string
  data?: { nodeType?: string; label?: string; config?: Record<string, any> }
}

/**
 * 查询节点连到分析卡：第一项还没选输入（也不是两份查询）的分析项顺手选上它，和连线同一步撤销（同合并查询的 connectMergeInput）。
 * 已经有项用着它、或者每一项都选好了输入时不动
 */
export function connectAnalysisInput<T extends CardNodeLike>(nodes: T[], source: string | null, target: string | null,
  isQuery: (n: T | undefined) => boolean): T[] | null {
  if (!source || !target || source === target) return null
  const to = nodes.find((n) => n.id === target)
  if (to?.data?.nodeType !== 'analysis' || !isQuery(nodes.find((n) => n.id === source))) return null
  const items = itemsOf(to.data?.config?.items)
  const uses = (it: AnalysisItem) => [it.from, it.params?.base_query, it.params?.target_query].includes(source)
  if (items.some(uses)) return null
  const i = items.findIndex((it) => !String(it.from ?? '').trim() && !('base_query' in (it.params ?? {})))
  if (i < 0) return null
  const next = items.map((it, j) => (j === i ? { ...it, from: source } : it))
  return nodes.map((n) => (n.id === target ? { ...n, data: { ...n.data, config: { ...(n.data?.config ?? {}), items: next } } } : n))
}
