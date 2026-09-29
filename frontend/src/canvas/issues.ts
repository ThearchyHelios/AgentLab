/**
 * 校验问题 → 能定位的东西：哪一类（图级 / 节点 / 边）、哪个节点、节点里的哪个字段。
 *
 * 检查器要把消息落到具体输入框下面——「分支「协作」的条件写错了」只挂在面板顶上，人还得
 * 自己去找是第几个 case。后端给了 field 就用它；老后端不给时按两条线索补上：
 *   1. 消息里带 {{ path }} 的（变量问题），去节点配置里找写着这个引用的字段；
 *   2. 其余按后端 schema.py 里那几句固定说法认（「跳过条件」「分支「X」的条件」……）。
 * 认不出来的就不认，留在面板顶部，不猜。
 *
 * 例外是「提示词要求用 X，但没绑定」：后端的 field 指着点名的那句提示词，可修法在工具
 * 那一栏，定位落到工具上（见 toolBindingOf）。
 */
import { ApiError } from '../api/client'
import { APPROVAL_POLICY_LABEL } from '../lib/terms'
import { NODE_DEFS } from './nodeDefs'
import type { FlowNode } from '../store/studio'
import type { AutofixResult, GraphSpec, PublishCheck, PublishFix, PublishLevel, ValidationIssue } from '../types'

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

/**
 * 「提示词要求用「X」，但节点没有绑定它」这一类（后端 schema._check_named_tools）。
 * 点了名的是 error，只是「提到了」的是 warning；笼统的「要求调用工具，但没有绑定任何工具」
 * 没有点名。三种的修法都是去绑工具
 */
const NAMED_TOOL = /(?:要求用|提到了)「([^」]+)」，但/
const VAGUE_TOOL = /要求调用工具，但(?:这个节点|这个成员)没有绑定任何工具/

/** 这条问题要绑的是哪个工具。不是这一类、或者没有点名，返回 null */
export function unboundToolOf(message: string): string | null {
  return NAMED_TOOL.exec(message)?.[1] ?? null
}

/** 工具没绑的问题落到哪一栏：agent 的「可用工具」，或者那个成员的工具；协作目标点的名落到成员列表 */
function toolBindingOf(issue: ValidationIssue, node: FlowNode): FieldRef | null {
  if (!NAMED_TOOL.test(issue.message) && !VAGUE_TOOL.test(issue.message)) return null
  const at = issue.field ? parseFieldPath(issue.field) : null
  if (at?.key === 'agents' && at.index != null) return { key: 'agents', index: at.index, sub: 'tools' }
  if (node.data.nodeType === 'supervisor') return { key: 'agents' }
  if (node.data.nodeType === 'agent') return { key: 'tools' }
  return at
}

const toolList = (v: unknown): string[] => (Array.isArray(v) ? v.filter((t) => typeof t === 'string') : [])

/**
 * 一键绑定：把 tool 加进 at 指着的那一栏（agent 的 tools，或第 i 个成员的 tools），返回新的
 * config。已经绑了、或者 at 指的不是工具栏，返回 null——按钮就不出现
 */
export function withToolBound(
  config: Record<string, any>, at: FieldRef | null | undefined, tool: string,
): Record<string, any> | null {
  if (!at) return null
  if (at.key === 'tools' && at.index == null) {
    const cur = toolList(config.tools)
    return cur.includes(tool) ? null : { ...config, tools: [...cur, tool] }
  }
  if (at.key === 'agents' && at.index != null && at.sub === 'tools' && Array.isArray(config.agents)) {
    const member = config.agents[at.index]
    if (!member || typeof member !== 'object') return null
    const cur = toolList(member.tools)
    if (cur.includes(tool)) return null
    return { ...config, agents: config.agents.map((a: any, i: number) => (i === at.index ? { ...a, tools: [...cur, tool] } : a)) }
  }
  return null
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

export function fieldOfIssue(issue: ValidationIssue, node: FlowNode | undefined): FieldRef | null {
  const binding = node ? toolBindingOf(issue, node) : null
  if (binding) return binding
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

// -------------------------------------------------------------------------
// 发布前检查与自动修复（POST /workflows/{id}/publish-check、/autofix）
//
// 接口是这一期才有的，老后端没有；新后端也可能少给字段。这里把回来的东西收成确定的形状，
// 认不出的丢掉，不猜：少了 fixes 就当没有修复，少了 changes 就当没有改动。
// -------------------------------------------------------------------------

const asArray = <T = unknown>(v: unknown): T[] => (Array.isArray(v) ? v as T[] : [])
const asText = (v: unknown): string => (typeof v === 'string' ? v : v == null ? '' : String(v))

function normalizeIssue(raw: any): ValidationIssue | null {
  if (!raw || typeof raw !== 'object' || !raw.message) return null
  return {
    level: raw.level === 'error' ? 'error' : 'warning',
    message: asText(raw.message),
    node_id: raw.node_id ?? null,
    edge_id: raw.edge_id ?? null,
    field: raw.field ?? null,
    code: typeof raw.code === 'string' ? raw.code : null,
    fix: typeof raw.fix === 'string' && raw.fix ? raw.fix : null,
  }
}

function normalizeFix(raw: any): PublishFix | null {
  if (!raw || typeof raw !== 'object' || typeof raw.id !== 'string' || !raw.id) return null
  const kind = raw.kind === 'choice' || raw.kind === 'assist' ? raw.kind : raw.kind === 'auto' ? 'auto' : null
  if (!kind) return null
  const options = asArray<any>(raw.options)
    .filter((o) => o && typeof o === 'object' && 'value' in o)
    .map((o) => ({ value: o.value, label: asText(o.label ?? o.value), hint: o.hint ? asText(o.hint) : null,
      ...(o.handoff === true ? { handoff: true } : {}) }))
  // 选项类没有候选就没法选：不给控件，比给一个空下拉强
  if (kind === 'choice' && !options.length) return null
  return {
    id: raw.id,
    code: asText(raw.code),
    kind,
    node_id: raw.node_id ?? null,
    label: asText(raw.label) || raw.id,
    preview: raw.preview && typeof raw.preview === 'object' ? raw.preview : null,
    options,
    multiple: !!raw.multiple,
    default: raw.default,
  }
}

export function normalizeCheck(raw: any, level: PublishLevel): PublishCheck {
  const issues = asArray(raw?.issues).map(normalizeIssue).filter((i): i is ValidationIssue => !!i)
  const fixes = asArray(raw?.fixes).map(normalizeFix).filter((f): f is PublishFix => !!f)
  return {
    level: raw?.level === 'governed' || raw?.level === 'published' ? raw.level : level,
    ok: typeof raw?.ok === 'boolean' ? raw.ok : !issues.some((i) => i.level === 'error'),
    issues,
    fixes: [...new Map(fixes.map((f) => [f.id, f])).values()],
  }
}

export function normalizeAutofix(raw: any): AutofixResult {
  const graph = raw?.graph && typeof raw.graph === 'object' && Array.isArray(raw.graph.nodes) ? raw.graph as GraphSpec : null
  const remaining = asArray(raw?.remaining).map(normalizeIssue).filter((i): i is ValidationIssue => !!i)
  const assist = raw?.assist && typeof raw.assist === 'object'
    ? { ok: !!raw.assist.ok, summary: asText(raw.assist.summary), questions: asArray(raw.assist.questions).map(asText).filter(Boolean) }
    : null
  return {
    graph,
    changes: asArray<any>(raw?.changes).filter((c) => c && typeof c === 'object').map((c) => ({
      fix_id: asText(c.fix_id), node_id: c.node_id ?? null, node_title: c.node_title ?? null,
      field: c.field ?? null, before: c.before, after: c.after, label: c.label ? asText(c.label) : null,
    })),
    applied: asArray(raw?.applied).map(asText).filter(Boolean),
    rejected: asArray<any>(raw?.rejected).filter((r) => r && typeof r === 'object')
      .map((r) => ({ fix_id: asText(r.fix_id), reason: asText(r.reason) || '没说原因' })),
    remaining,
    assist,
    ok: typeof raw?.ok === 'boolean' ? raw.ok : !remaining.some((i) => i.level === 'error'),
  }
}

/**
 * 老后端没有这个接口：FastAPI 对不存在的路由回 404 {"detail":"Not Found"}（方法不对是 405）。
 * 别的 404（工作流不在了）是真的出错，要照实说
 */
export function isMissingEndpoint(e: unknown): boolean {
  if (!(e instanceof ApiError) || e.kind !== 'http') return false
  return e.status === 405 || (e.status === 404 && (e.detail == null || e.detail === 'Not Found'))
}

/**
 * 这条问题的修复：后端点了 fix id 就按 id 找；没点的（/publish 被拦时的回包只有 code）按 code + 节点认。
 * 认不出时和后端 autofix.annotate 同一个退路：图级问题（没有节点，比如「没有带契约的出口」，
 * 修复却落在唯一的出口上）认同一编号的第一条修复；图级修复（改的是全图默认，不落在任何节点上）
 * 认同一编号的每一条问题。再认不出就没有
 */
export function fixFor(issue: ValidationIssue, fixes: PublishFix[]): PublishFix | undefined {
  if (issue.fix) return fixes.find((f) => f.id === issue.fix)
  if (!issue.code) return undefined
  const same = fixes.filter((f) => f.code === issue.code)
  const exact = same.find((f) => (f.node_id ?? null) === (issue.node_id ?? null))
  if (exact) return exact
  return issue.node_id == null ? same[0] : same.find((f) => f.node_id == null)
}

/**
 * 同一处问题只列一行：validate 和门禁常对同一处各报一条（同 code、同节点，说法不同），
 * 列两行就是两组一样的修复控件、「会被拦下 2 处」。合成的那一行取最重的级别、第一条 error 的说法，
 * 其余说法收进 others（悬停可见）。没有 code 的认不出是不是同一处，照原样各列一行
 */
export function mergeIssues(issues: ValidationIssue[]): { issue: ValidationIssue; others: string[] }[] {
  const out: { issue: ValidationIssue; others: string[] }[] = []
  const at = new Map<string, number>()
  for (const issue of issues) {
    const key = issue.code ? `${issue.code}\u0000${issue.node_id ?? ''}` : null
    const i = key != null ? at.get(key) : undefined
    if (i == null) {
      if (key != null) at.set(key, out.length)
      out.push({ issue, others: [] })
      continue
    }
    const row = out[i]
    const cur = row.issue
    const worse = issue.level === 'error' && cur.level !== 'error'
    const [keep, drop] = worse ? [issue, cur] : [cur, issue]
    row.issue = { ...keep, fix: cur.fix ?? issue.fix ?? null, field: keep.field ?? drop.field ?? null }
    if (drop.message !== keep.message && !row.others.includes(drop.message)) row.others.push(drop.message)
    row.others = row.others.filter((m) => m !== keep.message)
  }
  return out
}

/** 「一键修复」会应用的那几项：挂在眼前这些问题上的 auto 修复，去重 */
export function autoFixIds(issues: ValidationIssue[], fixes: PublishFix[]): string[] {
  const ids = issues.map((i) => fixFor(i, fixes)).filter((f) => f?.kind === 'auto').map((f) => f!.id)
  return [...new Set(ids)]
}

/** 选项类修复选好了没有：多选至少一项，单选要有值 */
export function choiceReady(fix: PublishFix, value: unknown): boolean {
  if (fix.multiple) return Array.isArray(value) && value.length > 0
  return value !== undefined
}

/**
 * 画布内容的签名：节点的类型、名字、配置和连线，不含位置。预览是按发请求那一刻的图算的，
 * 应用时画布内容变了就对不上；只是挪了挪节点不算变
 */
export function contentSig(graph: GraphSpec): string {
  return JSON.stringify({
    nodes: (graph.nodes ?? []).map((n) => [n.id, n.type, n.data?.label ?? '', n.data?.config ?? {}]),
    edges: (graph.edges ?? []).map((e) => [e.source, e.target, e.sourceHandle ?? null]),
  })
}

/** 出具契约里的几个键在界面上的叫法。拆成几行写契约骨架时也按这个顺序 */
const CONTRACT_KEYS: Record<string, string> = {
  metrics_from: '指标来自', report_from: '报告来自', required: '必需指标', expected: '期望指标',
  strict: '严格模式', narrative: '叙述', cells: '单元格引用', allow_numbers: '允许不带出处的数',
}

/** 不在节点定义的字段表里、由检查器自己画的几项（子工作流钉的版本、升版处置），以及图级的全图默认 */
const EXTRA_FIELDS: Record<string, string> = {
  workflow_version: '钉住版本', upgrade_policy: '上游发了新版本时', defaults: '全图默认',
}
/** 点号后面那一段的叫法：契约里的键，全图默认里的审批策略 */
const SUB_KEYS: Record<string, string> = {
  ...CONTRACT_KEYS, approval: '审批策略', claims: '没挂依据的结论句', on_uncited: '没挂依据时',
}
/** 契约 claims 的 on_uncited：没挂依据的结论句怎么算 */
const ON_UNCITED_LABEL: Record<string, string> = {
  ignore: '只标出来，不算缺口', degrade: '计入缺口、出具降档', withhold: '不予出具',
}

/** 值是节点 id 的几个键（报告、契约的 metrics_from，契约的 report_from）：预览里写节点名，不写 id */
const NODE_REF_KEYS = new Set(['metrics_from', 'report_from'])

/** 节点 id → 画布上的名字；画布上没有这个节点时返回 undefined */
export type NodeNameOf = (id: string) => string | undefined

const isRecord = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v)

/** 修复改的是哪个字段：顶层键用节点定义里的叫法，契约里的键另有一张表，其余原样 */
export function fixFieldLabel(field: string | null | undefined, nodeType?: string): string {
  if (!field) return ''
  const [head, ...rest] = field.split('.')
  if (head === 'label') return '节点名称'
  const def = nodeType ? NODE_DEFS[nodeType as keyof typeof NODE_DEFS] : undefined
  const base = def?.fields.find((f) => f.key === head)?.label ?? EXTRA_FIELDS[head] ?? head
  return [base, ...rest.map((k) => SUB_KEYS[k] ?? k)].join(' · ')
}

/**
 * 改动前后的值怎么写：空写「（空）」，审批策略写界面上的选项文字，节点引用写「节点名」
 * （画布上找不到的照写 id——它本来就指着一个不存在的节点），契约骨架按键写成「指标来自：…；…」，
 * 其余压成一行
 */
export function fixValueText(value: unknown, field?: string | null, nameOf?: NodeNameOf): string {
  if (value === undefined || value === null || value === '' || (Array.isArray(value) && !value.length)
    || (isRecord(value) && !Object.keys(value).length)) return '（空）'
  const key = field?.split('.').pop()
  if (key === 'approval' && typeof value === 'string' && value in APPROVAL_POLICY_LABEL) {
    return APPROVAL_POLICY_LABEL[value as keyof typeof APPROVAL_POLICY_LABEL]
  }
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (key === 'on_uncited' && typeof value === 'string' && value in ON_UNCITED_LABEL) return ON_UNCITED_LABEL[value]
  if (key === 'workflow_version' && (typeof value === 'number' || /^\d+$/.test(String(value)))) return `v${value}`
  if (key && NODE_REF_KEYS.has(key)) {
    const ids = Array.isArray(value) ? value : [value]
    if (ids.every((v) => typeof v === 'string')) {
      return ids.map((id) => { const name = nameOf?.(id as string); return name ? `「${name}」` : id }).join('、')
    }
  }
  const lines = fixValueLines(value, field, nameOf)
  if (lines) return lines.join('；')
  if (Array.isArray(value) && value.every((v) => typeof v !== 'object' || v === null)) return value.join('、')
  const text = typeof value === 'string' ? value : JSON.stringify(value)
  return text.length > 80 ? `${text.slice(0, 80)}…` : text
}

/**
 * 整份契约（field 为 contract、值是对象，比如「生成契约骨架」）拆成一键一行：「指标来自：「周报口径」」
 * 「必需指标：gmv、orders」。键名用界面叫法、按 CONTRACT_KEYS 的顺序，值和单独改一项时写法相同。
 * 不是整份契约的返回 null
 */
export function fixValueLines(value: unknown, field?: string | null, nameOf?: NodeNameOf): string[] | null {
  if (field !== 'contract' || !isRecord(value) || !Object.keys(value).length) return null
  const order = Object.keys(CONTRACT_KEYS)
  const keys = Object.keys(value).sort((a, b) => {
    const [x, y] = [order.indexOf(a), order.indexOf(b)]
    return (x < 0 ? order.length : x) - (y < 0 ? order.length : y)
  })
  return keys.map((k) => `${CONTRACT_KEYS[k] ?? k}：${fixValueText(value[k], `contract.${k}`, nameOf)}`)
}
