import type { Workflow } from '../types'

/**
 * 图里引用了这些名字（工具名、模型 id）的工作流。按 JSON 里的整串值匹配，不做子串：
 * 删一个叫 db_query 的工具，不该把只用了 db_query__shop 的工作流也算进来。
 * 删除、停用之前先说清会波及谁。
 */
export function workflowsMentioning(workflows: Workflow[], tokens: string[]): Workflow[] {
  const needles = tokens.filter(Boolean).map((t) => JSON.stringify(t))
  if (!needles.length) return []
  return workflows.filter((w) => {
    const text = JSON.stringify(w.graph ?? {})
    return needles.some((n) => text.includes(n))
  })
}

/** 「3 个工作流（「周报」「巡检」 等）」 */
export function workflowList(list: Workflow[], max = 3): string {
  const names = list.slice(0, max).map((w) => `「${w.name}」`).join('')
  return `${list.length} 个工作流（${names}${list.length > max ? ' 等' : ''}）`
}
