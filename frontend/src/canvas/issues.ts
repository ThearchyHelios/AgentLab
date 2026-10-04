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
import { APPROVAL_POLICY_LABEL, JUDGE_FIELD_LABEL, JUDGE_ON_UNSUPPORTED_LABEL, UPGRADE_TEXT, nodeTypeLabel } from '../lib/terms'
import { NODE_DEFS } from './nodeDefs'
import type { FlowNode } from '../store/studio'
import type {
  AutofixResult, GraphEdge, GraphNode, GraphSpec, PublishCheck, PublishFix, PublishLevel, UpgradeChange, UpgradeNote, UpgradeResult,
  ValidationIssue,
} from '../types'

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

/**
 * 后端 schema.py 的固定说法 → 字段。顺序有讲究：具体的在前。
 * 后端现在的说法和以前的说法都要认（开发库里历史运行、旧版本存下的问题还是旧文字），
 * 例如「条件循环没有设置「继续条件」」和旧的「while 循环没有写条件」
 */
const RULES: [RegExp, string][] = [
  [/^跳过条件/, 'skip_if'],
  [/分支|「其他」出口/, 'cases'],
  [/^循环条件|while 循环没有写条件|条件循环没有设置/, 'condition'],
  [/^整形表达式|整形节点没有填表达式|「数据整形」节点还没有填写表达式/, 'expression'],
  [/^指标|口径卡还没有定义指标/, 'metrics'],
  [/还没选工具|还没有选择工具/, 'tool'],
  [/(?:还没选|还没有选择|尚未选择)要嵌套的工作流/, 'workflow_id'],
  [/JSON Schema/, 'schema'],
  [/出具契约/, 'contract'],
  [/没有指定模型/, 'model'],
  [/代码把输出交给/, 'code'],
]

/**
 * 校验给的「可以升级为可追溯结构」是不是只有一句标题（后端 upgrade.py 的 HINT，有意保留原话，前端和脚本按它认）。
 * 界面上的同一句（UPGRADE_TEXT.advice）已改为「可升级为可追溯结构」：两种都算，只有标题时才补一句说明
 */
export function isBareUpgradeAdvice(message: string): boolean {
  const m = message.trim()
  return m === UPGRADE_TEXT.advice || m === '可以升级为可追溯结构'
}

/** 后端说的是分支「标识」的问题（保留名、重复）。检查器里标识由前端即时判，后端这几条不重复列 */
export function isCaseKeyIssue(message: string): boolean {
  return /标识/.test(message)
}

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

  // 分支某一项：「分支「协作模式」的条件有误」「分支 'fail' 没有连出去的边」「有两个分支的标识都是「x」」
  // （重复标识以前写成英文单引号 'x'，现在写成「x」，两种都认）
  const cases: any[] = Array.isArray(config.cases) ? config.cases : []
  const named = /分支「([^」]+)」|分支 '([^']+)'|标识都是 '([^']+)'|标识都是「([^」]+)」/.exec(msg)
  if (named && cases.length) {
    const dup = named[3] ?? named[4]
    const name = named[1] ?? named[2] ?? dup
    const idxs = cases.map((c, i) => ((c?.label || c?.key) === name || c?.key === name ? i : -1)).filter((i) => i >= 0)
    // 重复标识指的是后一个：前一个是正常的那个
    const index = dup ? idxs[idxs.length - 1] : idxs[0]
    if (index != null && index >= 0) {
      const sub = /的条件/.test(msg) ? 'condition' : isCaseKeyIssue(msg) ? 'key' : undefined
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
  // info 是建议（可以升级为可追溯结构），不是问题：不进清单、不占 F8 的跳转。编号按原下标，
  // 过滤前后同一条问题的 id 不变
  const list: Problem[] = issues.flatMap((issue, i) => {
    if (issue.level === 'info') return []
    const hit = issue.node_id ? byId.get(issue.node_id) : undefined
    return [{
      id: `${issue.node_id ?? issue.edge_id ?? 'graph'}:${i}`,
      level: issue.level,
      message: issue.message,
      scope: issue.edge_id ? 'edge' : issue.node_id ? 'node' : 'graph',
      nodeId: issue.node_id ?? undefined,
      edgeId: issue.edge_id ?? undefined,
      field: hit ? fieldOfIssue(issue, hit.n) : null,
    }]
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
  // 建议（info，比如「可以升级为可追溯结构」）不会让发布被拦，也不是要修的提示：不算进「另有 N 条提示」
  if (raw.level === 'info') return null
  return {
    level: raw.level === 'error' ? 'error' : 'warning',
    message: asText(raw.message),
    node_id: raw.node_id ?? null,
    edge_id: raw.edge_id ?? null,
    field: raw.field ?? null,
    code: typeof raw.code === 'string' ? raw.code : null,
    fix: typeof raw.fix === 'string' && raw.fix ? raw.fix : null,
    // 门禁里的 SQL 检查带着检查本来的级别（governance._lint_sql），发布前检查据此写「错误 / 提醒」
    ...(raw.sql_level === 'error' || raw.sql_level === 'warning' ? { sql_level: raw.sql_level } : {}),
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
      .map((r) => ({ fix_id: asText(r.fix_id), reason: asText(r.reason) || '未提供原因' })),
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
  strict: '严格模式', narrative: '叙述', cells: '单元格引用', allow_numbers: '允许无出处的数字',
}

/** 不在节点定义的字段表里、由检查器自己画的几项（子工作流钉的版本、升版处置），以及图级的全图默认 */
const EXTRA_FIELDS: Record<string, string> = {
  workflow_version: '固定版本', upgrade_policy: '上游发布新版本时', defaults: '工作流默认设置',
}
/** 点号后面那一段的叫法：契约里的键，全图默认里的审批策略，列表项里的名称、取值、表达式 */
const SUB_KEYS: Record<string, string> = {
  ...CONTRACT_KEYS, approval: '审批策略', claims: '未附依据的结论句', on_uncited: '未附依据时',
  name: '名称', value: '取值', expression: '表达式',
}
/** 契约 claims 的 on_uncited：没挂依据的结论句怎么算 */
const ON_UNCITED_LABEL: Record<string, string> = {
  ignore: '仅标注，不计入缺口', degrade: '计入缺口、出具降档', withhold: '不予出具',
}

/**
 * 只在一类节点上出现的下拉字段（顶层键）：修复预览里写检查器下拉的选项文字，不写 off、require_citation
 * 这类枚举值。同一个键在不同节点上含义不同的（mode 等）不在这里，照写原值
 */
const OPTION_OWNER: Record<string, keyof typeof NODE_DEFS> = {
  claims: 'report', numbers: 'report', entities: 'report', on_violation: 'report',
  on_exhausted: 'supervisor', isolation: 'code', evidence_role: 'code',
  on_missing: 'metrics', upgrade_policy: 'metrics', rerank: 'retrieve', thinking: 'llm', effort: 'llm',
}

function optionText(key: string, value: unknown): string | null {
  const owner = OPTION_OWNER[key]
  if (!owner || typeof value !== 'string') return null
  return NODE_DEFS[owner].fields.find((f) => f.key === key)?.options?.find((o) => o.value === value)?.label ?? null
}

/** 值是节点 id 的几个键（报告、契约的 metrics_from，契约的 report_from）：预览里写节点名，不写 id */
const NODE_REF_KEYS = new Set(['metrics_from', 'report_from'])

/** 节点 id → 画布上的名字；画布上没有这个节点时返回 undefined */
export type NodeNameOf = (id: string) => string | undefined

const isRecord = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v)

/**
 * 修复改的是哪个字段：顶层键用节点定义里的叫法，契约里的键另有一张表，结论句裁判（judge.*）另一张，其余原样。
 * 列表里的某一项（fields[0].value）写成「成果字段 · 「answer」 · 取值」：给了节点配置就认那一项的名字，
 * 没给写「第 1 项」
 */
export function fixFieldLabel(field: string | null | undefined, nodeType?: string, config?: Record<string, any>): string {
  if (!field) return ''
  const [head, ...rest] = field.split('.')
  if (head === 'label') return '节点名称'
  const at = /^([^[]+)\[(\d+)\]$/.exec(head)
  const key = at ? at[1] : head
  const def = nodeType ? NODE_DEFS[nodeType as keyof typeof NODE_DEFS] : undefined
  const base = def?.fields.find((f) => f.key === key)?.label ?? EXTRA_FIELDS[key] ?? key
  const item = at ? config?.[key]?.[Number(at[2])] : undefined
  const name = item && typeof item === 'object' ? item.name ?? item.id ?? item.key : undefined
  const which = at ? [typeof name === 'string' && name ? `「${name}」` : `第 ${Number(at[2]) + 1} 项`] : []
  const sub = head === 'judge' ? JUDGE_FIELD_LABEL : SUB_KEYS
  return [base, ...which, ...rest.map((k) => sub[k] ?? k)].join(' · ')
}

/** judge 里写 null 表示不限的三项上限：预览里 null 写「不限」，不写「（空）」——没写和不限是两回事 */
const JUDGE_LIMITS = new Set(['max_claims', 'max_cost_usd', 'timeout_s'])

/** judge.* 一项的值怎么写：上限带单位，null 是不限，判档写选项文字。不是 judge 的键返回 null */
function judgeValueText(value: unknown, key: string): string | null {
  if (JUDGE_LIMITS.has(key)) {
    if (value === null) return '不限'
    if (typeof value === 'number' && Number.isFinite(value)) {
      return key === 'max_cost_usd' ? `${value} 美元` : key === 'timeout_s' ? `${value} 秒` : `${value} 句`
    }
    return null
  }
  if (key === 'on_unsupported' && typeof value === 'string' && value in JUDGE_ON_UNSUPPORTED_LABEL) {
    return JUDGE_ON_UNSUPPORTED_LABEL[value]
  }
  return null
}

/**
 * 改动前后的值怎么写：空写「（空）」，审批策略写界面上的选项文字，节点引用写「节点名」
 * （画布上找不到的照写 id——它本来就指着一个不存在的节点），契约骨架按键写成「指标来自：…；…」，
 * 其余压成一行
 */
export function fixValueText(value: unknown, field?: string | null, nameOf?: NodeNameOf): string {
  if (field?.startsWith('judge.')) {
    const said = judgeValueText(value, field.slice(6))
    if (said != null) return said
  }
  if (value === undefined || value === null || value === '' || (Array.isArray(value) && !value.length)
    || (isRecord(value) && !Object.keys(value).length)) return '（空）'
  const key = field?.split('.').pop()
  if (key === 'approval' && typeof value === 'string' && value in APPROVAL_POLICY_LABEL) {
    return APPROVAL_POLICY_LABEL[value as keyof typeof APPROVAL_POLICY_LABEL]
  }
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (key && field === key) {
    const said = optionText(key, value)
    if (said != null) return said
  }
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
  // 整份结论句裁判（补预算那条修复按整份 judge 记）：一键一行，「金额上限（美元）：不限」
  if (field === 'judge' && isRecord(value) && Object.keys(value).length) {
    const order = Object.keys(JUDGE_FIELD_LABEL)
    return Object.keys(value).sort((a, b) => {
      const [x, y] = [order.indexOf(a), order.indexOf(b)]
      return (x < 0 ? order.length : x) - (y < 0 ? order.length : y)
    }).map((k) => `${JUDGE_FIELD_LABEL[k] ?? k}：${fixValueText(value[k], `judge.${k}`, nameOf)}`)
  }
  // 成果 / 输入字段整列（一键升级把成果字段改取报告的正文）：一项一行，「answer：{{ nodes.write.text }}」
  if (field === 'fields' && Array.isArray(value) && value.length && value.every((f) => isRecord(f) && 'name' in f)) {
    return value.map((f) => `${asText(f.name) || '（未命名）'}：${fixValueText(f.value)}`)
  }
  if (field !== 'contract' || !isRecord(value) || !Object.keys(value).length) return null
  const order = Object.keys(CONTRACT_KEYS)
  const keys = Object.keys(value).sort((a, b) => {
    const [x, y] = [order.indexOf(a), order.indexOf(b)]
    return (x < 0 ? order.length : x) - (y < 0 ? order.length : y)
  })
  return keys.map((k) => `${CONTRACT_KEYS[k] ?? k}：${fixValueText(value[k], `contract.${k}`, nameOf)}`)
}

// -------------------------------------------------------------------------
// 一键升级为可追溯结构（POST /copilot/upgrade-evidence）
//
// 和发布前修复同一套收法：回来的东西收成确定的形状，缺字段就当没有。改动逐项给人看，
// 另认三种自动修复里没有的：节点类型的变化、新插入的节点、改接的连线。
// -------------------------------------------------------------------------

/** validate 给的那条建议：旧结构（符合 R1–R4 任一条），可以升级 */
export const UPGRADE_ADVICE = 'evidence.upgrade_available'
export const isUpgradeAdvice = (issue: Pick<ValidationIssue, 'code'>): boolean => issue.code === UPGRADE_ADVICE

function normalizeNote(raw: any): UpgradeNote | null {
  if (typeof raw === 'string') return raw.trim() ? { text: raw } : null
  if (!isRecord(raw)) return null
  const text = asText(raw.text ?? raw.message ?? raw.note)
  if (!text) return null
  return {
    text, node_id: typeof raw.node_id === 'string' ? raw.node_id : null,
    rule: raw.rule ? asText(raw.rule) : null, level: raw.level === 'warning' ? 'warning' : 'info',
  }
}

export function normalizeUpgrade(raw: any): UpgradeResult {
  const graph = raw?.graph && typeof raw.graph === 'object' && Array.isArray(raw.graph.nodes) ? raw.graph as GraphSpec : null
  const assist = isRecord(raw?.assist)
    ? {
        ok: !!raw.assist.ok, summary: asText(raw.assist.summary),
        questions: asArray(raw.assist.questions).map(asText).filter(Boolean),
        warnings: asArray(raw.assist.warnings).map(asText).filter(Boolean),
      }
    : null
  return {
    graph,
    changes: asArray<any>(raw?.changes).filter(isRecord).map((c) => ({
      fix_id: asText(c.fix_id ?? c.rule), rule: c.rule ? asText(c.rule) : null,
      node_id: typeof c.node_id === 'string' ? c.node_id : null, node_title: c.node_title ? asText(c.node_title) : null,
      field: typeof c.field === 'string' ? c.field : null, before: c.before, after: c.after, label: c.label ? asText(c.label) : null,
    })),
    notes: asArray(raw?.notes).map(normalizeNote).filter((n): n is UpgradeNote => !!n),
    issues: asArray(raw?.issues).map(normalizeIssue).filter((i): i is ValidationIssue => !!i),
    rejected: asArray<any>(raw?.rejected).filter(isRecord)
      .map((r) => ({ fix_id: asText(r.fix_id), reason: asText(r.reason) || '未提供原因' })),
    assist,
  }
}

const isNodeLike = (v: unknown): v is Partial<GraphNode> =>
  isRecord(v) && typeof v.type === 'string' && (typeof v.id === 'string' || isRecord(v.data))
const isEdgeLike = (v: unknown): v is Pick<GraphEdge, 'source' | 'target'> =>
  isRecord(v) && typeof v.source === 'string' && typeof v.target === 'string'

/** 一项改动是哪一种：换了节点类型、新插入的节点、改接的连线，其余是改了某个配置 */
export type UpgradeChangeKind = 'type' | 'node' | 'edge' | 'value'

export function upgradeChangeKind(c: UpgradeChange): UpgradeChangeKind {
  const f = c.field ?? ''
  if (f === 'type' || f === 'node_type') return 'type'
  if (f === 'node' || (!f && c.before == null && isNodeLike(c.after))) return 'node'
  if (f === 'edge' || f === 'edges' || isEdgeLike(c.after) || isEdgeLike(c.before)) return 'edge'
  return 'value'
}

/** 节点类型写界面上的叫法：「模型调用」「报告撰写」；认不出的照写原码 */
export const upgradeTypeText = (type: unknown): string => (typeof type === 'string' && type ? nodeTypeLabel(type) : '（空）')

/** 新插入的节点：「「写报告」（报告撰写）」。后端的节点可能是图里的形状（data.label），也可能是操作流的（label） */
export function upgradeNodeText(node: unknown): string {
  if (!isNodeLike(node)) return fixValueText(node)
  const label = asText((node as { label?: unknown }).label) || asText(node.data?.label) || asText(node.id)
  return `「${label}」（${upgradeTypeText(node.type)}）`
}

/** 改写规则在界面上的叫法：R1–R5 照写，Copilot 那一段写 Copilot */
export const upgradeRuleText = (rule?: string | null): string => (rule === 'assist' ? '助手' : rule ?? '')

/**
 * 逐项改动按「哪一步」分组：同一步（同一个 fix_id，比如把「写周报」换成报告撰写）的几项改动放在一起，
 * 这一步的说明只写一次。后端没给 fix_id 的（前端按两张图自己列的）各自一组、没有说明
 */
export function upgradeGroups(changes: UpgradeChange[]): { key: string; rule: string | null; label: string | null; items: UpgradeChange[] }[] {
  const out: { key: string; rule: string | null; label: string | null; items: UpgradeChange[] }[] = []
  changes.forEach((c, i) => {
    const key = c.fix_id || `#${i}`
    const last = out[out.length - 1]
    if (last && c.fix_id && last.key === key) {
      last.items.push(c)
      return
    }
    out.push({ key, rule: c.rule ?? null, label: c.label ?? null, items: [c] })
  })
  return out
}

/**
 * 没采用的那一步是谁：「R1 · 「写周报」」「Copilot」。后端的 fix_id 是「规则:节点 id」，
 * 认不出的照写
 */
export function upgradeStepText(fixId: string, nameOf?: NodeNameOf): string {
  if (!fixId || fixId === 'assist') return '助手'
  const m = /^(R\d+):(.+)$/.exec(fixId)
  return m ? `${m[1]} · 「${nameOf?.(m[2]) ?? m[2]}」` : fixId
}

/** 一条连线：「「取数」→「写报告」」。两头的节点名先在画布上找，再在升级后的图里找 */
export function upgradeEdgeText(edge: unknown, nameOf?: NodeNameOf): string {
  if (!isEdgeLike(edge)) return fixValueText(edge)
  const name = (id: string) => `「${nameOf?.(id) ?? id}」`
  return `${name(edge.source)} → ${name(edge.target)}`
}

const edgeKey = (e: Pick<GraphEdge, 'source' | 'target' | 'sourceHandle'>) => `${e.source}\u0000${e.target}\u0000${e.sourceHandle ?? ''}`

/**
 * 后端没给逐项改动（或者老一点的形状只给了图）时，按前后两张图自己列：新插入的节点、换了类型的节点、
 * 改了的名字和每个顶层配置、加上和去掉的连线。和后端给的一样逐项写，不替它编原因
 */
export function upgradeChangesFromDiff(before: GraphSpec, after: GraphSpec): UpgradeChange[] {
  const out: UpgradeChange[] = []
  const old = new Map((before.nodes ?? []).map((n) => [n.id, n]))
  for (const n of after.nodes ?? []) {
    const o = old.get(n.id)
    const title = n.data?.label ?? o?.data?.label ?? n.id
    if (!o) {
      out.push({ fix_id: '', node_id: n.id, node_title: title, field: 'node', before: null, after: n })
      continue
    }
    if (o.type !== n.type) out.push({ fix_id: '', node_id: n.id, node_title: title, field: 'type', before: o.type, after: n.type })
    if ((o.data?.label ?? '') !== (n.data?.label ?? '')) {
      out.push({ fix_id: '', node_id: n.id, node_title: title, field: 'label', before: o.data?.label, after: n.data?.label })
    }
    const a = o.data?.config ?? {}
    const b = n.data?.config ?? {}
    for (const key of [...new Set([...Object.keys(a), ...Object.keys(b)])]) {
      if (JSON.stringify(a[key]) !== JSON.stringify(b[key])) {
        out.push({ fix_id: '', node_id: n.id, node_title: title, field: key, before: a[key], after: b[key] })
      }
    }
  }
  const was = new Set((before.edges ?? []).map(edgeKey))
  const now = new Set((after.edges ?? []).map(edgeKey))
  for (const e of before.edges ?? []) {
    if (!now.has(edgeKey(e))) out.push({ fix_id: '', node_id: null, field: 'edge', before: { source: e.source, target: e.target }, after: null })
  }
  for (const e of after.edges ?? []) {
    if (!was.has(edgeKey(e))) out.push({ fix_id: '', node_id: null, field: 'edge', before: null, after: { source: e.source, target: e.target } })
  }
  return out
}

/** 升级前后差在哪，合成一句：「新增 1 个节点、1 个节点换了类型、改了 2 个节点的配置、连线变动 2 处」 */
export function upgradeSummary(before: GraphSpec, after: GraphSpec): string[] {
  const old = new Map((before.nodes ?? []).map((n) => [n.id, n]))
  let added = 0
  let typed = 0
  let changed = 0
  for (const n of after.nodes ?? []) {
    const o = old.get(n.id)
    if (!o) { added += 1; continue }
    if (o.type !== n.type) typed += 1
    else if ((o.data?.label ?? '') !== (n.data?.label ?? '')
      || JSON.stringify(o.data?.config ?? {}) !== JSON.stringify(n.data?.config ?? {})) changed += 1
  }
  const removed = (before.nodes ?? []).filter((n) => !(after.nodes ?? []).some((x) => x.id === n.id)).length
  const was = new Set((before.edges ?? []).map(edgeKey))
  const now = new Set((after.edges ?? []).map(edgeKey))
  const edges = [...now].filter((k) => !was.has(k)).length + [...was].filter((k) => !now.has(k)).length
  return [
    added && `新增 ${added} 个节点`,
    typed && `${typed} 个节点变更了类型`,
    changed && `修改了 ${changed} 个节点的配置`,
    removed && `删除 ${removed} 个节点`,
    edges && `连线变动 ${edges} 处`,
  ].filter((x): x is string => !!x)
}
