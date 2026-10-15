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

/** 用到某个视图的组件（绑定的视图、可见性表达式所指的视图是它），按页面树的顺序：删视图前说清哪些组件会失去数据来源 */
export function componentsOfView(spec: AppSpec, view: string): { page: AppPage; node: AppComponent }[] {
  const out: { page: AppPage; node: AppComponent }[] = []
  for (const page of spec.pages) {
    const walk = (list: AppComponent[]) => list.forEach((c) => {
      if (c.bind?.view === view || c.scope === view) out.push({ page, node: c })
      if (c.children) walk(c.children)
    })
    walk(page.body)
  }
  return out
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
export function locatePath(spec: AppSpec, path: string | null | undefined): { section: string; component?: string; path?: string } | null {
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
  // 入口、表单、待办外观在「入口与表单」一节按 JSON 编辑，带着 path 去定位；视图来源在页面一节左栏下方
  if (head === 'entries' || head === 'forms' || head === 'task_ui') return { section: 'forms', path }
  if (head === 'views') return { section: 'pages' }
  // 只写了组件 id 的（服务端按 id 报）
  const at = findComponent(spec, head ?? '')
  return at ? { section: 'pages', component: at.node.id } : null
}

// ---------------------------------------------------------------------------
// 入口与表单一节的 JSON 编辑：path 拆成键、在排好版的 JSON 里找行、按目录的子集查形状
// ---------------------------------------------------------------------------

/** 检查问题的 path → 键的序列（同 locatePath 认的三种写法）。entries 后面写的是入口编号时换成下标 */
export function pathParts(spec: AppSpec, path: string): string[] {
  const parts = path.replace(/^\//, '').replace(/\[(\d+)\]/g, '.$1').split(/[./]/).filter(Boolean)
    .map((p) => p.replace(/~1/g, '/').replace(/~0/g, '~'))
  if (parts[0] === 'entries' && parts[1] != null && !/^\d+$/.test(parts[1])) {
    const i = spec.entries.findIndex((e) => e.id === parts[1])
    if (i >= 0) parts[1] = String(i)
  }
  return parts
}

const SENTINEL = '\u0000agentlab-locate\u0000'

/**
 * value 按 JSON.stringify(value, null, 2) 排版后，keys 指的那一项从第几行开始（0 起）；找不到最深的那一层就退回能找到的
 * 那一层，一层都找不到为 -1。做法：把那一项换成一个不会出现的标记再排一遍版，数标记前面有几行——之前的行和原文一模一样
 */
export function lineOfPath(value: unknown, keys: string[]): number {
  for (let depth = keys.length; depth > 0; depth--) {
    const copy = JSON.parse(JSON.stringify(value ?? null))
    let cur: any = copy
    let ok = true
    for (let i = 0; i < depth - 1; i++) {
      if (cur == null || typeof cur !== 'object' || !(keys[i] in cur)) { ok = false; break }
      cur = cur[keys[i]]
    }
    const last = keys[depth - 1]
    if (!ok || cur == null || typeof cur !== 'object' || !(last in cur)) continue
    cur[last] = SENTINEL
    const text = JSON.stringify(copy, null, 2)
    const at = text.indexOf(JSON.stringify(SENTINEL))
    if (at >= 0) return text.slice(0, at).split('\n').length - 1
  }
  return -1
}

/** JSON.parse 的报错 → 第几行第几列（浏览器说法不一：有的给 position，有的给 line/column） */
export function jsonErrorAt(text: string, message: string): { line: number; col: number } | null {
  const lc = message.match(/line (\d+) column (\d+)/i)
  if (lc) return { line: Number(lc[1]), col: Number(lc[2]) }
  const pos = message.match(/position (\d+)/i)
  if (!pos) return null
  const before = text.slice(0, Number(pos[1])).split('\n')
  return { line: before.length, col: before[before.length - 1].length + 1 }
}

const FIELD_TYPES = new Set(['string', 'number', 'integer', 'boolean', 'array'])

export interface ShapeIssue { message: string; keys: string[] }

/**
 * 入口、表单、待办外观的形状检查（只查结构，规则以服务端的发布检查为准）：
 * - 入口：数组；每项有编号（不重复）、固定版本的别名在固定版本里、表单内联或引用表单一节里有的、字段映射是「名字 → 文字」；
 * - 表单：名字 → agentlab.form/1：对象类型、列出字段，字段类型在子集里，不许正则；
 * - 待办外观：节点 → 对象。
 * forms 给的是同一份草稿里的表单（入口引用的表单在不在按它查）
 */
export function shapeIssues(kind: 'entries' | 'forms' | 'task_ui', value: unknown, spec: AppSpec,
  T: {
    shapeArray: string; shapeObject: string; entryNotObject: (i: number) => string; entryId: (i: number) => string
    entryDup: (id: string) => string; entryPin: (id: string, pin: string) => string; entryPinMissing: (id: string) => string
    entryForm: (id: string, ref: string) => string; entryMap: (id: string) => string; formShape: (name: string) => string
    formFieldType: (name: string, field: string) => string; formPattern: (name: string, field: string) => string
    taskUiShape: (node: string) => string
  }, forms: Record<string, unknown> = spec.forms): ShapeIssue[] {
  const out: ShapeIssue[] = []
  const isObj = (v: unknown): v is Record<string, any> => !!v && typeof v === 'object' && !Array.isArray(v)
  if (kind === 'entries') {
    if (!Array.isArray(value)) return [{ message: T.shapeArray, keys: [] }]
    const seen = new Set<string>()
    value.forEach((e, i) => {
      if (!isObj(e)) { out.push({ message: T.entryNotObject(i + 1), keys: [String(i)] }); return }
      const id = typeof e.id === 'string' ? e.id.trim() : ''
      if (!id) out.push({ message: T.entryId(i + 1), keys: [String(i)] })
      else if (seen.has(id)) out.push({ message: T.entryDup(id), keys: [String(i), 'id'] })
      seen.add(id)
      const name = id || String(i + 1)
      if (typeof e.pin !== 'string' || !e.pin) out.push({ message: T.entryPinMissing(name), keys: [String(i)] })
      else if (!spec.pins.some((p) => p.alias === e.pin)) out.push({ message: T.entryPin(name, e.pin), keys: [String(i), 'pin'] })
      const ref = isObj(e.form) && typeof e.form.$ref === 'string' ? e.form.$ref : null
      if (ref) {
        const target = ref.replace(/^#\/forms\//, '')
        if (!(target in forms)) out.push({ message: T.entryForm(name, ref), keys: [String(i), 'form'] })
      } else if (e.form != null) {
        out.push(...formIssues(name, e.form, [String(i), 'form'], T))
      }
      if (e.map != null && (!isObj(e.map) || Object.values(e.map).some((v) => typeof v !== 'string'))) {
        out.push({ message: T.entryMap(name), keys: [String(i), 'map'] })
      }
    })
    return out
  }
  if (!isObj(value)) return [{ message: T.shapeObject, keys: [] }]
  for (const [name, v] of Object.entries(value)) {
    if (kind === 'forms') out.push(...formIssues(name, v, [name], T))
    else if (!isObj(v)) out.push({ message: T.taskUiShape(name), keys: [name] })
  }
  return out
}

function formIssues(name: string, form: unknown, keys: string[], T: Parameters<typeof shapeIssues>[3]): ShapeIssue[] {
  const f = form as Record<string, any> | null
  if (!f || typeof f !== 'object' || Array.isArray(f) || f.type !== 'object' || !f.properties || typeof f.properties !== 'object') {
    return [{ message: T.formShape(name), keys }]
  }
  const out: ShapeIssue[] = []
  for (const [field, spec] of Object.entries(f.properties as Record<string, any>)) {
    const at = [...keys, 'properties', field]
    if (!spec || typeof spec !== 'object' || !FIELD_TYPES.has(spec.type)) out.push({ message: T.formFieldType(name, field), keys: at })
    else if ('pattern' in spec) out.push({ message: T.formPattern(name, field), keys: [...at, 'pattern'] })
  }
  return out
}
