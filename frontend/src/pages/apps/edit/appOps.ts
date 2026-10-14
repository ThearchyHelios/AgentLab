/**
 * 应用描述（agentlab.app/1）的草稿操作：编辑器的页面树和助手的应用操作共用这一份。
 *
 * 应用操作的形状同服务端 app/apps/ops.py（apply_app_op）：
 *   {"op":"app_add","parent":"<组件 id>|<页面 id>","index":0,"component":{...}}
 *   {"op":"app_update","id":"<组件 id>","props":{...},"bind":{...},"visible":"..."}
 *   {"op":"app_remove","id":"<组件 id>"}
 *   {"op":"app_move","id":"<组件 id>","parent":"...","index":0}
 *   {"op":"app_page","page":{"id","title"}}
 *   {"op":"app_set","path":"entries|views|forms|slots|task_ui","key":"…","value":{...}}
 * 助手流式到达时按到达顺序落到草稿上，边收边改；流结束时的 final 带服务端落地后的整份 spec，以它为准。
 * 这里一律返回新对象（不改入参），认不出、目标不存在的操作原样返回草稿：服务端那份才是权威的
 */
import type { AppComponent, AppPage, AppSpec } from '../../../typesApp'

const clone = <T,>(v: T): T => JSON.parse(JSON.stringify(v)) as T

/** 组件在页面树里的位置：哪一页、父组件（顶层为 null）、父的子组件数组里的第几个 */
export interface ComponentAt {
  page: AppPage
  parent: AppComponent | null
  list: AppComponent[]
  index: number
  node: AppComponent
}

export function findComponent(spec: AppSpec, id: string): ComponentAt | null {
  for (const page of spec.pages) {
    const walk = (list: AppComponent[], parent: AppComponent | null): ComponentAt | null => {
      for (let i = 0; i < list.length; i++) {
        const node = list[i]
        if (node.id === id) return { page, parent, list, index: i, node }
        if (node.children?.length) {
          const hit = walk(node.children, node)
          if (hit) return hit
        }
      }
      return null
    }
    const hit = walk(page.body, null)
    if (hit) return hit
  }
  return null
}

/** 页面 id 或容器组件 id → 它的子组件数组（没有 children 的容器补一个空数组） */
function childrenOf(spec: AppSpec, parent: string): AppComponent[] | null {
  const page = spec.pages.find((p) => p.id === parent)
  if (page) return page.body
  const at = findComponent(spec, parent)
  if (!at) return null
  at.node.children ??= []
  return at.node.children
}

/** 全部组件 id：新组件取不重名的 id 用 */
export function componentIds(spec: AppSpec): Set<string> {
  const ids = new Set<string>()
  const walk = (list: AppComponent[]) => list.forEach((c) => { ids.add(c.id); if (c.children) walk(c.children) })
  spec.pages.forEach((p) => { ids.add(p.id); walk(p.body) })
  return ids
}

export function freshId(spec: AppSpec, base: string): string {
  const ids = componentIds(spec)
  if (!ids.has(base)) return base
  let n = 2
  while (ids.has(`${base}_${n}`)) n++
  return `${base}_${n}`
}

const clampIndex = (i: unknown, len: number) =>
  typeof i === 'number' && Number.isFinite(i) ? Math.max(0, Math.min(len, Math.floor(i))) : len

/** 把一个应用操作落到草稿上，返回新草稿；认不出、目标不存在的原样返回 */
export function applyAppOp(spec: AppSpec, op: Record<string, any>): AppSpec {
  const next = clone(spec)
  switch (op.op) {
    case 'app_add': {
      const comp = op.component
      if (!comp || typeof comp !== 'object' || typeof comp.id !== 'string' || typeof comp.type !== 'string') return spec
      const parent = typeof op.parent === 'string' && op.parent ? op.parent : next.pages[0]?.id
      const list = parent ? childrenOf(next, parent) : null
      if (!list) return spec
      // 同 id 的组件已经在（接着写时重发了一遍）：整体覆盖，不出两个
      const old = findComponent(next, comp.id)
      if (old) old.list.splice(old.index, 1)
      list.splice(clampIndex(op.index, list.length), 0, comp as AppComponent)
      return next
    }
    case 'app_update': {
      const at = typeof op.id === 'string' ? findComponent(next, op.id) : null
      if (!at) return spec
      if (op.props && typeof op.props === 'object') at.node.props = { ...(at.node.props ?? {}), ...op.props }
      if ('bind' in op) at.node.bind = op.bind ?? null
      if ('visible' in op) at.node.visible = op.visible || null
      if ('scope' in op) at.node.scope = op.scope || null
      return next
    }
    case 'app_remove': {
      const at = typeof op.id === 'string' ? findComponent(next, op.id) : null
      if (!at) return spec
      at.list.splice(at.index, 1)
      return next
    }
    case 'app_move': {
      const at = typeof op.id === 'string' ? findComponent(next, op.id) : null
      const parent = typeof op.parent === 'string' && op.parent ? op.parent : at?.page.id
      if (!at || !parent) return spec
      const node = at.node
      at.list.splice(at.index, 1)
      const list = childrenOf(next, parent)
      if (!list) return spec
      list.splice(clampIndex(op.index, list.length), 0, node)
      return next
    }
    case 'app_page': {
      const page = op.page
      if (!page || typeof page.id !== 'string') return spec
      const old = next.pages.find((p) => p.id === page.id)
      if (old) old.title = String(page.title ?? old.title)
      else next.pages.push({ id: page.id, title: String(page.title ?? page.id), body: [] })
      return next
    }
    case 'app_set': {
      const path = String(op.path ?? '')
      const key = String(op.key ?? '')
      if (!key) return spec
      if (path === 'entries') {
        const list = next.entries.filter((e) => e.id !== key)
        if (op.value) list.push({ ...op.value, id: key })
        next.entries = list
        return next
      }
      if (path === 'views' || path === 'forms' || path === 'slots' || path === 'task_ui') {
        const bucket = next[path] as Record<string, any>
        if (op.value == null) delete bucket[key]
        else bucket[key] = op.value
        return next
      }
      return spec
    }
    default:
      return spec
  }
}

/**
 * 检查问题的 path → 它落在哪里。认三种写法：JSON 指针（/pages/0/body/2）、点号加下标（pages[0].body[2]）、
 * 纯点号（pages.0.body.2）。落到组件上返回组件 id，落到别处返回所在的那一节（固定版本、入口、岗位……）
 */
export function locatePath(spec: AppSpec, path: string | null | undefined): { section: string; component?: string } | null {
  if (!path) return null
  const parts = path.replace(/^\//, '').replace(/\[(\d+)\]/g, '.$1').split(/[./]/).filter(Boolean)
  const head = parts[0]
  if (head === 'pages') {
    const page = spec.pages[Number(parts[1])]
    if (!page) return { section: 'pages' }
    let list = page.body
    let last: AppComponent | undefined
    for (let i = 2; i < parts.length; i += 2) {
      if (parts[i] !== 'body' && parts[i] !== 'children') break
      last = list[Number(parts[i + 1])]
      if (!last) break
      list = last.children ?? []
    }
    return { section: 'pages', component: last?.id ?? page.id }
  }
  if (head === 'pins') return { section: 'pins' }
  if (head === 'slots' || head === 'bindings' || head === 'grants') return { section: 'access' }
  if (head === 'hooks') return { section: 'hooks' }
  if (head === 'notify') return { section: 'notify' }
  if (head === 'entries' || head === 'forms' || head === 'views' || head === 'task_ui') return { section: 'pages' }
  // 只写了组件 id 的（服务端按 id 报）
  const at = findComponent(spec, head ?? '')
  return at ? { section: 'pages', component: at.node.id } : null
}
