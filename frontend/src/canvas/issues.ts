/**
 * 校验问题 → 能定位的东西：哪一类（图级 / 节点 / 边）、哪个节点、节点里的哪个字段。
 *
 * 后端的 ValidationIssue 只带 node_id / edge_id，不带字段。可检查器要把消息落到具体
 * 输入框下面——「分支「协作」的条件写错了」只挂在面板顶上，人还得自己去找是第几个
 * case。这里按两条线索补上字段：
 *   1. 消息里带 {{ path }} 的（变量问题），去节点配置里找写着这个引用的字段；
 *   2. 其余按后端 schema.py 里那几句固定说法认（「跳过条件」「分支「X」的条件」……）。
 * 认不出来的就不认，留在面板顶部，不猜。后端哪天给了 field，优先用它。
 */
import type { FlowNode } from '../store/studio'
import type { ValidationIssue } from '../types'

export interface FieldRef {
  /** 顶层配置键：prompt、cases、fields… 'label' 表示节点名称 */
  key: string
  /** 复合字段里的第几项：cases[1]、fields[0] */
  index?: number
  /** 那一项里的哪个键：condition、value、key */
  sub?: string
}

export interface Problem {
  id: string
  level: 'error' | 'warning'
  message: string
  scope: 'graph' | 'node' | 'edge'
  nodeId?: string
  edgeId?: string
  field?: FieldRef | null
}

/** 'cases[1].condition' → {key:'cases', index:1, sub:'condition'} */
export function parseFieldPath(path: string): FieldRef {
  const m = /^([^.[]+)(?:\[(\d+)\])?(?:\.(.+))?$/.exec(path)
  if (!m) return { key: path }
  return { key: m[1], ...(m[2] != null ? { index: Number(m[2]) } : {}), ...(m[3] ? { sub: m[3] } : {}) }
}

/** 配置里哪个字段的文本包含这段引用。和后端 variables._iter_strings 同样的摊平方式 */
function findText(config: Record<string, any>, needle: string): string | null {
  const walk = (value: any, prefix: string): string | null => {
    if (typeof value === 'string') return value.includes(needle) ? prefix : null
    if (Array.isArray(value)) {
      for (let i = 0; i < value.length; i++) {
        const hit = walk(value[i], `${prefix}[${i}]`)
        if (hit) return hit
      }
      return null
    }
    if (value && typeof value === 'object') {
      for (const [k, v] of Object.entries(value)) {
        const hit = walk(v, prefix ? `${prefix}.${k}` : k)
        if (hit) return hit
      }
    }
    return null
  }
  return walk(config, '')
}

/** 后端 schema.py 的固定说法 → 字段。顺序有讲究：具体的在前 */
const RULES: [RegExp, string][] = [
  [/^跳过条件/, 'skip_if'],
  [/分支|「其他」出口/, 'cases'],
  [/^循环条件|while 循环没有写条件/, 'condition'],
  [/^整形表达式|整形节点没有填表达式/, 'expression'],
  [/^指标|口径卡还没有定义指标/, 'metrics'],
  [/还没选工具/, 'tool'],
  [/还没选要嵌套的工作流/, 'workflow_id'],
  [/JSON Schema/, 'schema'],
  [/出具契约/, 'contract'],
  [/没有指定模型/, 'model'],
  [/代码把输出交给/, 'code'],
]

export function fieldOfIssue(
  issue: ValidationIssue & { field?: string | null }, node: FlowNode | undefined,
): FieldRef | null {
  if (issue.field) return parseFieldPath(issue.field)
  if (!node) return null
  const config = node.data.config ?? {}
  const msg = issue.message

  // 变量问题：{{ vars.x }} 没有任何节点产出 / 由某某产出但排在后面
  const ref = /\{\{\s*([^}|]+?)\s*(?:\|[^}]*)?\}\}/.exec(msg)?.[1]
  if (ref && !/^分支|^跳过条件|^循环条件|^整形表达式|^指标/.test(msg)) {
    const path = findText(config, ref)
    if (path) return parseFieldPath(path)
  }

  // 分支某一项：「分支「协作模式」的条件写错了」「分支 'fail' 没有连出去的边」
  const cases: any[] = Array.isArray(config.cases) ? config.cases : []
  const named = /分支「([^」]+)」|分支 '([^']+)'|标识都是 '([^']+)'/.exec(msg)
  if (named && cases.length) {
    const name = named[1] ?? named[2] ?? named[3]
    const idxs = cases.map((c, i) => ((c?.label || c?.key) === name || c?.key === name ? i : -1)).filter((i) => i >= 0)
    // 重复标识指的是后一个：前一个是正常的那个
    const index = named[3] ? idxs[idxs.length - 1] : idxs[0]
    if (index != null && index >= 0) {
      const sub = /的条件/.test(msg) ? 'condition' : /标识/.test(msg) ? 'key' : undefined
      return { key: 'cases', index, ...(sub ? { sub } : {}) }
    }
  }
  const metric = /^指标「([^」]+)」/.exec(msg)?.[1]
  if (metric && Array.isArray(config.metrics)) {
    const index = config.metrics.findIndex((m: any) => (m?.id || '?') === metric)
    if (index >= 0) return { key: 'metrics', index, sub: 'expression' }
  }
  for (const [re, key] of RULES) if (re.test(msg)) return { key }
  return null
}

/**
 * 问题面板的条目：图级在前，然后按节点在图里的先后（F8 从左往右跳），error 先于 warning。
 * rank 由调用方给（derive.topology 的层级），没有就按节点顺序。
 */
export function problemsOf(
  issues: ValidationIssue[], nodes: FlowNode[], rank?: Record<string, number>,
): Problem[] {
  const byId = new Map(nodes.map((n, i) => [n.id, { n, i }]))
  const list: Problem[] = issues.map((issue, i) => {
    const hit = issue.node_id ? byId.get(issue.node_id) : undefined
    return {
      id: `${issue.node_id ?? issue.edge_id ?? 'graph'}:${i}`,
      level: issue.level,
      message: issue.message,
      scope: issue.edge_id ? 'edge' : issue.node_id ? 'node' : 'graph',
      nodeId: issue.node_id ?? undefined,
      edgeId: issue.edge_id ?? undefined,
      field: hit ? fieldOfIssue(issue, hit.n) : null,
    }
  })
  const order = (p: Problem) => {
    if (p.scope !== 'node') return -1
    const hit = byId.get(p.nodeId!)
    return hit ? (rank?.[p.nodeId!] ?? 0) * 10_000 + hit.i : 1e9
  }
  return list.sort((a, b) => order(a) - order(b) || (a.level === b.level ? 0 : a.level === 'error' ? -1 : 1))
}
