import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { ArrowDown, ArrowUp, Plus, Trash2 } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../../../api/client'
import { DeleteButton, EmptyState, Field, SectionBar } from '../../../components/ui'
import { APP_EDITOR_TEXT as T } from '../../../lib/terms'
import type { AppBinding, AppComponent, AppSpec, AppViewSource, CatalogComponent } from '../../../typesApp'
import { BackfillButton, useBackfillTargets } from '../backfill'
import { applyAppOp, findComponent, freshId } from './appOps'
import { useEditor } from './context'

/*
 * 页面树 + 属性面板（A2、§3.4、§3.5）。像画布的节点定义驱动检查器那样，属性面板由组件目录（GET /api/apps/catalog）
 * 里每个组件的 props JSON Schema 驱动：字符串、数字、布尔、枚举、字符串数组各有一种控件。绑定是带类型的对象，
 * 不是模板：种类只能从目录给这个组件登记的里挑；节点名从固定版本的图里取候选。可见性表达式服务端求值，不允许算术
 * （app.expr_arith），这里只编辑，不在浏览器里求值（P7）。
 *
 * 所有修改都走 applyAppOp（和助手的应用操作同一套）：人改和助手改落到草稿上是同一种动作
 */

const BINDING_KINDS = ['metric', 'series', 'cell', 'query', 'report', 'input', 'run', 'output', 'task', 'action'] as const

/** 绑定里出现哪个键就是哪种（typesApp.AppBinding） */
const bindKindOf = (bind: AppBinding | null | undefined): string =>
  (bind && BINDING_KINDS.find((k) => k in bind)) ?? ''

export function PagesSection() {
  const { draft, setDraft, catalog, selected, select } = useEditor()
  const at = selected ? findComponent(draft, selected) : null
  const [pageId, setPageId] = useState(() => at?.page.id ?? draft.pages[0]?.id ?? '')
  // 从检查、助手那边定位过来：跟着选中的组件换页
  useEffect(() => { if (at && at.page.id !== pageId) setPageId(at.page.id) }, [at?.page.id]) // eslint-disable-line react-hooks/exhaustive-deps
  const page = draft.pages.find((p) => p.id === pageId) ?? draft.pages[0]
  const byName = useMemo(() => new Map((catalog?.components ?? []).map((c) => [c.name, c])), [catalog])

  const addPage = () => {
    const id = freshId(draft, 'page')
    setDraft((s) => applyAppOp(s, { op: 'app_page', page: { id, title: '新页面' } }))
    setPageId(id)
  }
  const addComponent = (entry: CatalogComponent) => {
    if (!page) return
    const id = freshId(draft, entry.name)
    const sel = at && at.page.id === page.id ? at : null
    // 选中的是容器：放进它里面；选中的是普通组件：放在它后面；没选：页面最后
    const container = sel && byName.get(sel.node.type)?.children
    const parent = container ? sel!.node.id : sel?.parent?.id ?? page.id
    const index = container ? (sel!.node.children?.length ?? 0) : sel ? sel.index + 1 : page.body.length
    setDraft((s) => applyAppOp(s, { op: 'app_add', parent, index, component: {
      id, type: entry.name, props: {}, ...(entry.children ? { children: [] } : {}),
    } }))
    select(id)
  }
  const move = (delta: number) => {
    if (!at) return
    setDraft((s) => applyAppOp(s, { op: 'app_move', id: at.node.id, parent: at.parent?.id ?? at.page.id,
                                     index: Math.max(0, at.index + delta) }))
  }

  return (
    <div className="flex h-full min-h-0" data-app-pages="">
      <div className="flex w-72 shrink-0 flex-col border-r">
        <div className="flex flex-wrap items-center gap-1 border-b px-2 py-1.5" role="tablist" aria-label={T.sections.pages}>
          {draft.pages.map((p) => (
            <button key={p.id} type="button" role="tab" aria-selected={p.id === page?.id} data-app-page={p.id}
                    className={clsx('rounded px-2 py-0.5 text-xs', p.id === page?.id ? 'bg-hover font-medium' : 'text-dim hover:bg-hover')}
                    onClick={() => { setPageId(p.id); select(null) }}>
              {p.title || p.id}
            </button>
          ))}
          <button type="button" className="btn btn-xs btn-ghost" onClick={addPage} title={T.pageAdd} aria-label={T.pageAdd}>
            <Plus size={11} aria-hidden />
          </button>
        </div>
        {page ? (
          <>
            <div className="flex items-end gap-1.5 border-b px-2 py-2">
              <Field label={T.pageTitle} className="min-w-0 flex-1">
                {(p) => <input {...p} className="field h-7 py-0 text-xs" value={page.title}
                               onChange={(e) => setDraft((s) => applyAppOp(s, { op: 'app_page', page: { id: page.id, title: e.target.value } }))} />}
              </Field>
              {draft.pages.length > 1 && (
                <DeleteButton label={T.pageRemove(page.title)} onClick={() => {
                  setDraft((s) => ({ ...s, pages: s.pages.filter((x) => x.id !== page.id) }))
                  setPageId(draft.pages.find((x) => x.id !== page.id)?.id ?? '')
                }} />
              )}
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto p-1.5" role="tree" aria-label={page.title} data-app-tree={page.id}>
              {page.body.length
                ? <Tree nodes={page.body} depth={0} byName={byName} />
                : <p className="p-2 text-2xs text-faint">{T.componentAddTo(T.componentRoot)}</p>}
            </div>
            <div className="flex items-center gap-1 border-t px-2 py-1.5">
              <Palette onPick={addComponent} />
              <span className="flex-1" />
              <button type="button" className="btn btn-xs btn-ghost" disabled={!at || at.index === 0} onClick={() => move(-1)}
                      title={T.componentUp} aria-label={T.componentUp}><ArrowUp size={11} aria-hidden /></button>
              <button type="button" className="btn btn-xs btn-ghost" disabled={!at || at.index >= at.list.length - 1} onClick={() => move(1)}
                      title={T.componentDown} aria-label={T.componentDown}><ArrowDown size={11} aria-hidden /></button>
              <button type="button" className="btn btn-xs btn-ghost" disabled={!at} title={T.componentRemove} aria-label={T.componentRemove}
                      onClick={() => { if (at) { setDraft((s) => applyAppOp(s, { op: 'app_remove', id: at.node.id })); select(null) } }}>
                <Trash2 size={11} aria-hidden />
              </button>
            </div>
            <ViewsPanel />
          </>
        ) : <EmptyState title={T.pageAdd} />}
      </div>
      <div className="min-w-0 flex-1 overflow-y-auto p-4">
        {at ? <ComponentInspector key={at.node.id} node={at.node} entry={byName.get(at.node.type)} />
          : <EmptyState title={T.inspectorEmpty} body={T.pagesHint} />}
      </div>
    </div>
  )
}

function Tree({ nodes, depth, byName }: { nodes: AppComponent[]; depth: number; byName: Map<string, CatalogComponent> }) {
  const { selected, select } = useEditor()
  return (
    <>
      {nodes.map((n) => {
        const entry = byName.get(n.type)
        const label = String(n.props?.label ?? n.props?.title ?? '')
        return (
          <div key={n.id} role="treeitem" aria-selected={selected === n.id} aria-expanded={entry?.children ? true : undefined}>
            <button type="button" onClick={() => select(n.id)} data-app-component={n.id}
                    className={clsx('flex w-full items-center gap-1.5 rounded px-1.5 py-1 text-left text-xs hover:bg-hover',
                      selected === n.id && 'bg-hover ring-1 ring-inset ring-[var(--accent)]')}
                    style={{ paddingLeft: 6 + depth * 14 }}>
              <span className={clsx('shrink-0', entry ? 'text-dim' : 'text-[var(--err)]')}>{entry?.label ?? n.type}</span>
              <span className="min-w-0 flex-1 truncate text-faint">{label || n.id}</span>
              {n.visible && <span className="shrink-0 text-2xs text-faint" title={n.visible}>{T.visible}</span>}
            </button>
            {!!n.children?.length && <Tree nodes={n.children} depth={depth + 1} byName={byName} />}
          </div>
        )
      })}
    </>
  )
}

/** 添加组件：按目录的类别分组，模型和人都只能从这里挑 */
function Palette({ onPick }: { onPick: (c: CatalogComponent) => void }) {
  const { catalog } = useEditor()
  const [open, setOpen] = useState(false)
  if (!catalog) return null
  return (
    <div className="relative">
      <button type="button" className="btn btn-xs" aria-expanded={open} onClick={() => setOpen((v) => !v)} data-app-palette="">
        <Plus size={10} aria-hidden /> {T.componentAdd}
      </button>
      {open && (
        <div className="absolute bottom-full left-0 z-20 mb-1 max-h-80 w-64 overflow-y-auto rounded-lg border bg-panel p-1.5 shadow-lg"
             role="menu" data-esc-layer>
          {Object.entries(catalog.categories).map(([cat, label]) => (
            <div key={cat} className="mb-1.5">
              <div className="px-1.5 text-2xs font-semibold text-faint">{label}</div>
              {catalog.components.filter((c) => c.category === cat).map((c) => (
                <button key={c.name} type="button" role="menuitem" data-app-palette-item={c.name}
                        className="flex w-full items-center gap-1.5 rounded px-1.5 py-1 text-left text-xs hover:bg-hover"
                        onClick={() => { onPick(c); setOpen(false) }}>
                  <span>{c.label}</span><span className="mono text-2xs text-faint">{c.name}</span>
                </button>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// -------------------------------------------------------------------------
// 属性面板
// -------------------------------------------------------------------------

function ComponentInspector({ node, entry }: { node: AppComponent; entry?: CatalogComponent }) {
  const { draft, setDraft } = useEditor()
  const update = (patch: Record<string, unknown>) => setDraft((s) => applyAppOp(s, { op: 'app_update', id: node.id, ...patch }))
  const views = Object.keys(draft.views)
  return (
    <div className="mx-auto max-w-2xl space-y-5" data-app-inspector={node.id}>
      <div>
        <div className="flex items-baseline gap-2">
          <h3 className="text-sm font-semibold">{entry?.label ?? node.type}</h3>
          <span className="mono text-2xs text-faint">{node.type} · {node.id}</span>
        </div>
        {!entry && <p className="mt-1 text-2xs text-[var(--err)]">{T.componentUnknown(node.type)}</p>}
      </div>
      {entry && Object.keys(entry.props?.properties ?? {}).length > 0 && (
        <section>
          <SectionBar title={T.props} />
          <PropsForm schema={entry.props} value={node.props ?? {}} onChange={(props) => update({ props })} />
        </section>
      )}
      {entry && entry.bindings.length > 0 && (
        <section data-app-bind={bindKindOf(node.bind)}>
          <SectionBar title={T.bind} />
          <BindEditor kinds={entry.bindings} value={node.bind ?? null} views={views} onChange={(bind) => update({ bind })} />
        </section>
      )}
      <section>
        <SectionBar title={T.visible} hint={T.visibleHint} />
        <div className="grid grid-cols-[1fr_180px] gap-2">
          <input className="field mono text-xs" value={node.visible ?? ''} placeholder={T.visiblePlaceholder} aria-label={T.visible}
                 spellCheck={false} onChange={(e) => update({ visible: e.target.value })} data-app-visible="" />
          <select className="field" value={node.scope ?? ''} aria-label={T.scope} title={T.scope}
                  onChange={(e) => update({ scope: e.target.value })}>
            <option value="">{T.bindViewNone}</option>
            {views.map((v) => <option key={v} value={v}>{v}</option>)}
          </select>
        </div>
      </section>
    </div>
  )
}

/**
 * 按组件目录里的 props schema 画表单。entry / form 两个键指向应用里已有的入口、表单，给下拉；其余按类型
 * （改的是一份浅拷贝，写回整份 props：app_update 按键合并）
 */
function PropsForm({ schema, value, onChange }: {
  schema: Record<string, any>; value: Record<string, any>; onChange: (v: Record<string, any>) => void
}) {
  const { draft } = useEditor()
  const set = (k: string, v: unknown) => onChange({ ...value, [k]: v === '' || v === undefined ? null : v })
  const rows: ReactNode[] = []
  for (const [key, prop] of Object.entries((schema.properties ?? {}) as Record<string, any>)) {
    const id = `app-prop-${key}`
    const cur = value[key]
    let control: ReactNode
    if (key === 'entry' && prop.type === 'string') {
      control = (
        <select id={id} className="field" value={cur ?? ''} onChange={(e) => set(key, e.target.value)}>
          <option value="">—</option>
          {draft.entries.map((e) => <option key={e.id} value={e.id}>{e.label || e.id}</option>)}
        </select>
      )
    } else if (key === 'form' && prop.type === 'string') {
      control = (
        <select id={id} className="field" value={cur ?? ''} onChange={(e) => set(key, e.target.value)}>
          <option value="">—</option>
          {Object.keys(draft.forms).map((f) => <option key={f} value={f}>{f}</option>)}
        </select>
      )
    } else if (Array.isArray(prop.enum)) {
      control = (
        <select id={id} className="field" value={cur ?? ''} onChange={(e) => set(key, e.target.value)}>
          <option value="">—</option>
          {prop.enum.map((o: string) => <option key={o} value={o}>{o}</option>)}
        </select>
      )
    } else if (prop.type === 'boolean') {
      control = <input id={id} type="checkbox" className="accent-[var(--accent)]" checked={cur === true} onChange={(e) => set(key, e.target.checked)} />
    } else if (prop.type === 'integer' || prop.type === 'number') {
      control = <input id={id} type="number" className="field tnum" value={cur ?? ''} min={prop.minimum} max={prop.maximum}
                       step={prop.type === 'integer' ? 1 : 'any'}
                       onChange={(e) => set(key, e.target.value === '' ? null : Number(e.target.value))} />
    } else if (prop.type === 'array') {
      control = <textarea id={id} className="field text-xs" rows={3} value={Array.isArray(cur) ? cur.join('\n') : ''}
                          placeholder="每行一项"
                          onChange={(e) => set(key, e.target.value.split('\n').filter((x) => x.trim()).slice(0, prop.maxItems ?? 50))} />
    } else if ((prop.maxLength ?? 0) > 200) {
      control = <textarea id={id} className="field text-xs" rows={4} value={cur ?? ''} maxLength={prop.maxLength}
                          onChange={(e) => set(key, e.target.value)} />
    } else {
      control = <input id={id} className="field" value={cur ?? ''} maxLength={prop.maxLength} onChange={(e) => set(key, e.target.value)} />
    }
    rows.push(
      <div key={key} data-app-prop={key}>
        <label className="label" htmlFor={id}>{prop.title ?? key}</label>
        {control}
      </div>,
    )
  }
  return <div className="grid grid-cols-2 gap-3">{rows}</div>
}

/** 绑定：种类只能从目录登记的里挑。视图指向 spec.views；节点名从这个视图所固定的版本的图里给候选 */
function BindEditor({ kinds, value, views, onChange }: {
  kinds: string[]; value: AppBinding | null; views: string[]; onChange: (v: AppBinding | null) => void
}) {
  const kind = bindKindOf(value)
  const view = typeof value?.view === 'string' ? value.view : ''
  const nodes = useViewNodes(view)
  const listId = `app-bind-nodes-${view}`
  const setKind = (k: string) => onChange(k ? { ...(view && k !== 'task' && k !== 'action' ? { view } : {}),
    [k]: k === 'cell' ? { node: '', row: 0, col: '' } : k === 'query' ? { node: '' } : k === 'run' ? 'status' : '' } : null)
  const set = (patch: Record<string, unknown>) => onChange({ ...(value ?? {}), ...patch })
  const inner = value && kind ? value[kind] : undefined
  const nodeInput = (v: string, on: (s: string) => void, label = T.bindNode) => (
    <input className="field mono text-xs" list={listId} value={v} aria-label={label} placeholder={label} spellCheck={false}
           onChange={(e) => on(e.target.value)} />
  )
  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 gap-2">
        <select className="field" value={kind} aria-label={T.bindKind} onChange={(e) => setKind(e.target.value)} data-app-bind-kind="">
          <option value="">{T.bindNone}</option>
          {kinds.map((k) => <option key={k} value={k}>{T.bindKinds[k] ?? k}</option>)}
        </select>
        {kind && kind !== 'task' && kind !== 'action' && (
          <select className="field" value={view} aria-label={T.bindView} onChange={(e) => set({ view: e.target.value || undefined })}>
            <option value="">{T.bindViewNone}</option>
            {views.map((v) => <option key={v} value={v}>{v}</option>)}
          </select>
        )}
      </div>
      {kind && <p className="text-2xs text-faint">{T.bindHint[kind]}</p>}
      {(kind === 'metric' || kind === 'report' || kind === 'input' || kind === 'output' || kind === 'task' || kind === 'action') && (
        kind === 'metric' || kind === 'input' || kind === 'output'
          ? <input className="field mono text-xs" value={typeof inner === 'string' ? inner : ''} spellCheck={false}
                   aria-label={T.bindKinds[kind]} onChange={(e) => set({ [kind]: e.target.value })} />
          : nodeInput(typeof inner === 'string' ? inner : '', (s) => set({ [kind]: s }))
      )}
      {kind === 'series' && (
        <div className="grid grid-cols-[1fr_120px] gap-2">
          <input className="field mono text-xs" value={typeof inner === 'string' ? inner : ''} spellCheck={false}
                 aria-label={T.bindKinds.series} onChange={(e) => set({ series: e.target.value })} />
          <input className="field tnum" type="number" min={1} max={120} value={value?.last ?? 12} aria-label={T.bindLast}
                 title={T.bindLast} onChange={(e) => set({ last: Math.max(1, Number(e.target.value) || 1) })} />
        </div>
      )}
      {kind === 'cell' && (
        <div className="grid grid-cols-3 gap-2">
          {nodeInput(inner?.node ?? '', (s) => set({ cell: { ...inner, node: s } }))}
          <input className="field tnum" type="number" min={0} value={inner?.row ?? 0} aria-label={T.bindRow} title={T.bindRow}
                 onChange={(e) => set({ cell: { ...inner, row: Number(e.target.value) || 0 } })} />
          <input className="field mono text-xs" value={inner?.col ?? ''} aria-label={T.bindCol} placeholder={T.bindCol}
                 onChange={(e) => set({ cell: { ...inner, col: e.target.value } })} />
        </div>
      )}
      {kind === 'query' && (
        <div className="grid grid-cols-[1fr_120px] gap-2">
          {nodeInput(inner?.node ?? '', (s) => set({ query: { ...inner, node: s } }))}
          <input className="field tnum" type="number" min={1} value={inner?.exec ?? ''} aria-label={T.bindExec} placeholder={T.bindExec}
                 onChange={(e) => set({ query: { ...inner, exec: e.target.value ? Number(e.target.value) : undefined } })} />
        </div>
      )}
      {kind === 'run' && (
        <select className="field" value={typeof inner === 'string' ? inner : 'status'} aria-label={T.bindKinds.run}
                onChange={(e) => set({ run: e.target.value })}>
          {Object.entries(T.runFields).map(([k, label]) => <option key={k} value={k}>{label}</option>)}
        </select>
      )}
      <datalist id={listId}>{nodes.map((n) => <option key={n.id} value={n.id}>{n.label}</option>)}</datalist>
    </div>
  )
}

/** 视图所固定的版本里的节点（id、名字），绑定填节点名时给候选。取不到就没有候选，照样能手填 */
const graphCache = new Map<string, Promise<{ id: string; label: string }[]>>()
function useViewNodes(view: string): { id: string; label: string }[] {
  const { draft } = useEditor()
  const source: AppViewSource | undefined = draft.views[view]?.source
  const pin = draft.pins.find((p) => p.alias === source?.pin) ?? (draft.pins.length === 1 ? draft.pins[0] : undefined)
  const [nodes, setNodes] = useState<{ id: string; label: string }[]>([])
  useEffect(() => {
    if (!pin) { setNodes([]); return }
    const key = `${pin.workflow_id}@${pin.version}`
    let p = graphCache.get(key)
    if (!p) {
      p = api.workflows.version(pin.workflow_id, pin.version)
        .then((v) => (v.graph?.nodes ?? []).map((n) => ({ id: n.id, label: String(n.data?.label ?? n.id) })))
      p.catch(() => graphCache.delete(key))
      graphCache.set(key, p)
    }
    let alive = true
    p.then((list) => { if (alive) setNodes(list) }, () => undefined)
    return () => { alive = false }
  }, [pin?.workflow_id, pin?.version]) // eslint-disable-line react-hooks/exhaustive-deps
  return nodes
}

// -------------------------------------------------------------------------
// 视图来源
// -------------------------------------------------------------------------

/**
 * 视图来源（§2.3）：最近封存 {pin, from} / 按需 {pin, params, ttl_s, auto} / 本实例。
 * 当前发布版把某个视图固定的版本升了版、处置为按新口径重算时，那个视图下面给「按新口径补算历史」（D-f，按发布版补算）
 */
function ViewsPanel() {
  const { appId, detail, draft, setDraft } = useEditor()
  const [open, setOpen] = useState(false)
  const targets = useBackfillTargets(appId, detail.published_version, true, detail.backfill ?? null)
  const set = (key: string, value: { source: AppViewSource } | null) =>
    setDraft((s: AppSpec) => applyAppOp(s, { op: 'app_set', path: 'views', key, value }))
  return (
    <details className="border-t" open={open || targets.length > 0} onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}
             data-app-views="" data-app-views-backfill={targets.length || undefined}>
      <summary className="cursor-pointer px-2 py-1.5 text-2xs font-semibold text-faint" title={T.viewsHint}>
        {T.views}（{Object.keys(draft.views).length}）
      </summary>
      <div className="max-h-72 space-y-2 overflow-y-auto px-2 pb-2">
        {Object.entries(draft.views).map(([key, v]) => (
          <div key={key} className="space-y-1 rounded border p-1.5 text-2xs" data-app-view={key}>
            <div className="flex items-center gap-1">
              <span className="mono min-w-0 flex-1 truncate font-medium">{key}</span>
              <DeleteButton label={`删除视图 ${key}`} onClick={() => set(key, null)} />
            </div>
            <select className="field h-7 py-0 text-xs" value={v.source.kind} aria-label={T.views}
                    onChange={(e) => set(key, { source: { ...v.source, kind: e.target.value as AppViewSource['kind'] } })}>
              {Object.entries(T.viewKinds).map(([k, label]) => <option key={k} value={k}>{label}</option>)}
            </select>
            {v.source.kind !== 'instance' && (
              <select className="field h-7 py-0 text-xs" value={v.source.pin ?? ''} aria-label={T.viewPin}
                      onChange={(e) => set(key, { source: { ...v.source, pin: e.target.value || null } })}>
                <option value="">{T.viewPin}</option>
                {draft.pins.map((p) => <option key={p.alias} value={p.alias}>{p.alias} · v{p.version}</option>)}
              </select>
            )}
            {v.source.kind === 'latest_sealed' && (
              <input className="field mono h-7 py-0 text-xs" value={v.source.from ?? ''} placeholder="formal" aria-label={T.viewFrom}
                     title={T.viewFrom} onChange={(e) => set(key, { source: { ...v.source, from: e.target.value || null } })} />
            )}
            {targets.filter((t) => t.view === key).map((t) => (
              <div key={t.view} className="space-y-1 rounded border border-dashed p-1" title={T.backfillPublishedOnly}>
                <p className="leading-relaxed text-faint">{T.backfillHint(t.alias, t.from, t.to)}</p>
                <BackfillButton appId={appId} target={t} />
              </div>
            ))}
            {v.source.kind === 'on_demand' && (
              <>
                <input className="field tnum h-7 py-0 text-xs" type="number" min={1} max={86400} value={v.source.ttl_s ?? 600}
                       aria-label={T.viewTtl} title={T.viewTtl}
                       onChange={(e) => set(key, { source: { ...v.source, ttl_s: Number(e.target.value) || null } })} />
                <label className="flex items-center gap-1">
                  <input type="checkbox" className="accent-[var(--accent)]" checked={!!v.source.auto}
                         onChange={(e) => set(key, { source: { ...v.source, auto: e.target.checked } })} />
                  {T.viewAuto}
                </label>
              </>
            )}
          </div>
        ))}
        <button type="button" className="btn btn-xs w-full justify-center" data-app-view-add=""
                onClick={() => set(freshViewKey(draft), { source: { kind: 'latest_sealed', pin: draft.pins[0]?.alias ?? null, from: 'formal' } })}>
          <Plus size={10} aria-hidden /> {T.viewAdd}
        </button>
      </div>
    </details>
  )
}

function freshViewKey(spec: AppSpec): string {
  let n = Object.keys(spec.views).length + 1
  while (`v${n}` in spec.views) n++
  return `v${n}`
}
