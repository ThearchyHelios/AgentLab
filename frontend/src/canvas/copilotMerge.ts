/**
 * 助手的 update_node 怎么落到节点上，以及改完之后工具绑定变了什么。
 *
 * 规则和后端 app/api/copilot.py 的 merge_node_config / tool_changes 一字不差：
 * 流式期间画布按这里逐条落，收尾时 final.graph 是后端按同一套规则合好的整张图。
 * 两边口径一不一样，画布就会在 final 到来那一下跳一次——更糟的是以前前端整体替换
 * config，流到一半断掉时，画布上的半成品里 agent 的工具已经没了。
 *
 * 以前是整体替换：模型只想改一句提示词，也得把整份 config 抄一遍，漏抄的字段就没了。
 * 真出过事——改写提示词时漏了 tools，要查库的 agent 全丢了工具，最后把一堆工具调用
 * 的原始标记当答案交了出去。
 *
 * 纯函数，不依赖 React 和 store，检查脚本能直接 import。
 */
import type { ToolChange } from '../types'

type Config = Record<string, any>

/** 成员条目里写 remove: true 表示删掉这个成员 */
const REMOVE_MEMBER = 'remove'

const isObject = (v: unknown): v is Config => !!v && typeof v === 'object' && !Array.isArray(v)

// 和 engine/nodes/multi.py 的叫法一致：没起名字的成员按位置叫 agentN
const memberKey = (member: Config, index: number): string => String(member.name || `agent${index}`)

/** 按字段合并：写了的覆盖，写 null 的删掉，没写的原样保留。返回新对象 */
function mergeFields(old: Config, patch: Config): Config {
  const out: Config = { ...old }
  for (const [key, value] of Object.entries(patch)) {
    if (value === null) delete out[key]
    else if (value !== undefined) out[key] = value
  }
  return out
}

/** 协作成员按 name 合并。原有成员保持原来的次序，新名字接在后面 */
function mergeMembers(old: unknown[], patch: unknown[]): unknown[] {
  const merged = [...old]
  const where = new Map<string, number>()
  merged.forEach((m, i) => { if (isObject(m)) where.set(memberKey(m, i), i) })
  const dropped = new Set<number>()
  patch.forEach((entry, i) => {
    if (!isObject(entry)) return
    const key = memberKey(entry, i)
    const at = where.get(key)
    if (entry[REMOVE_MEMBER] === true) {
      if (at != null) dropped.add(at)
      return
    }
    const { [REMOVE_MEMBER]: _flag, ...fields } = entry
    if (at != null) {
      merged[at] = mergeFields(merged[at] as Config, fields)
    } else {
      where.set(key, merged.length)
      merged.push(Object.fromEntries(Object.entries(fields).filter(([, v]) => v != null)))
    }
  })
  return merged.filter((_, i) => !dropped.has(i))
}

/**
 * update_node 的 config 落到节点上：
 * 1. 顶层按字段浅合并。写 null 删键；没写的保留；args、output_schema 这类嵌套对象整个换掉。
 * 2. 只有 supervisor 的 agents 是列表时，成员按 name（没名字按 agentN）合并；
 *    成员自己的字段同样浅合并（没写 tools 的保留原来的 tools）。
 * 3. {name, remove: true} 删掉那个成员；不认识的名字接在后面，写 null 的字段不留。
 */
export function mergeNodeConfig(nodeType: string, old: Config, patch: Config): Config {
  let next = patch
  if (nodeType === 'supervisor' && Array.isArray(patch.agents)) {
    const members = Array.isArray(old.agents) ? old.agents : []
    next = { ...patch, agents: mergeMembers(members, patch.agents) }
  }
  return mergeFields(old, next)
}

// -------------------------------------------------------------------------
// 工具绑定变化
// -------------------------------------------------------------------------

/** GraphSpec 的节点和画布上的 FlowNode 都认：FlowNode 的 type 是 'card'，真类型在 data.nodeType */
interface NodeLike {
  id: string
  type?: string
  data?: { nodeType?: string; label?: string; config?: Config }
}

const toolNames = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((t): t is string => typeof t === 'string' && !!t) : []

interface Binding { nodeId: string; member: string | null; label: string; field: string; tools: string[] }

/** (节点 id, 成员名) → 绑定的工具。成员名为 null 的是 agent 节点本身 */
function bindings(nodes: NodeLike[]): Map<string, Binding> {
  const out = new Map<string, Binding>()
  for (const node of nodes) {
    const type = node.data?.nodeType ?? node.type
    const cfg = node.data?.config ?? {}
    const label = node.data?.label || node.id
    if (type === 'agent') {
      out.set(JSON.stringify([node.id, null]),
        { nodeId: node.id, member: null, label, field: 'tools', tools: toolNames(cfg.tools) })
    } else if (type === 'supervisor' && Array.isArray(cfg.agents)) {
      cfg.agents.forEach((m: unknown, i: number) => {
        if (!isObject(m)) return
        const member = memberKey(m, i)
        out.set(JSON.stringify([node.id, member]),
          { nodeId: node.id, member, label, field: `agents[${i}].tools`, tools: toolNames(m.tools) })
      })
    }
  }
  return out
}

/**
 * 改图前后都在、而绑定的工具不一样了的节点和成员，按改后的图排序。和后端 tool_changes
 * 同一套口径：新加、删掉的节点不算，那是结构变化，回执本来就会列出来。
 * 老后端的 final 不带 tool_changes 时用它自己比。
 */
export function toolChangesOf(before: NodeLike[], after: NodeLike[]): ToolChange[] {
  const old = bindings(before)
  const out: ToolChange[] = []
  for (const [key, now] of bindings(after)) {
    const was = old.get(key)
    if (!was) continue
    const a = new Set(was.tools)
    const b = new Set(now.tools)
    if (a.size === b.size && [...a].every((t) => b.has(t))) continue
    out.push({
      node_id: now.nodeId, label: now.label, member: now.member, field: now.field,
      before: was.tools, after: now.tools,
      added: now.tools.filter((t) => !a.has(t)),
      removed: was.tools.filter((t) => !b.has(t)),
    })
  }
  return out
}

/** 工具集合变小了的那几条。换一个工具（数量没少）是正常改图，不算 */
export function shrunkTools(changes: ToolChange[]): ToolChange[] {
  return changes.filter((c) => c.removed.length > 0 && new Set(c.after).size < new Set(c.before).size)
}

/** 回执里的一行：「数据查询」：db_query__shop、db_schema__shop → 空 */
export function describeToolChange(c: ToolChange): string {
  const who = c.member ? `「${c.label}」的成员「${c.member}」` : `「${c.label}」`
  const list = (xs: string[]) => (xs.length ? xs.join('、') : '空')
  return `${who}：${list(c.before)} → ${list(c.after)}`
}
