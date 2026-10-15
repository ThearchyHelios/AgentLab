/**
 * 应用页上直接改（docs/design/normal-user-apps.md N6、§4.5）：有 manage 的人在应用页点「编辑」进入编辑状态
 * （地址上记 edit=1，刷新、换页都还在编辑状态）。
 *
 * 编辑层叠在视图渲染器外面，按 data-node 认组件：悬停描边，点中出小工具条。点击在捕获阶段拦下——编辑状态下点数字、
 * 发起按钮、表头不打开出处、不跳转、不排序，只是选中。小工具条只有五样：改文字（组件目录里标了 x-text 的属性）、
 * 隐藏、上移、下移、删除；页底「加一张数据卡片」从固定版本的结果里挑（GET /api/apps/{id}/candidates）。
 *
 * 落地全走应用操作（edit/appOps 的 applyAppOp，和应用编辑器、助手同一套），改完自动存草稿（PATCH /api/apps/{id}），
 * 再按草稿重新解析这一页——视图模型由服务端解析，这里不在浏览器里改树（P7）。几次改动挨着到时按顺序存，存的永远是
 * 最新的那份；存失败了照实说，恢复成上一次存上的样子。隐藏的组件按草稿解析时照样下发，画成半透明；服务端没画出来的
 * 列在页底，照样能取消隐藏。
 *
 * 改取数不在这里：回到蓝图（/blueprints/:id），重走试运行和发布单，口径变了要处置（不可回退：口径升版必须处置）。
 * 呈现的改动只进草稿，「发布」走现有的应用发布（不动工作流）；固定版本升了版要选处置的，用应用编辑器的同一个处置框。
 * 个人应用没有应用版本，没有发布这一步。
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent, MouseEvent as ReactMouseEvent } from 'react'
import { Link } from 'react-router-dom'
import {
  ArrowDown, ArrowUp, Check, Database, Eye, EyeOff, LayoutGrid, PencilLine, Plus, Settings2, ShieldCheck, Trash2, Type,
} from 'lucide-react'
import clsx from 'clsx'
import { api } from '../../api/client'
import { ViewRenderer, type ViewEditMarks } from '../../apps/ViewRenderer'
import { parseViewParams } from '../../apps/components/input'
import {
  EmptyState, Modal, Notice, PageHeader, Skeleton, Spinner, Tabs, confirmDialog, toast,
} from '../../components/ui'
import { useAtLeast } from '../../store/auth'
import { usePageTitle } from '../../lib/notify'
import { APP_TEXT, PAGE_EDIT_TEXT as T } from '../../lib/termsApp'
import type {
  AppComponent, AppDetail, AppSpec, CatalogComponent, ComponentCatalog, Disposition, LintIssue, PublishResult, ViewModel,
  ViewNode,
} from '../../typesApp'
import type { AppCandidate } from '../../typesBlueprint'
import { applyAppOp, findComponent, freshComponent, textPropsOf } from './edit/appOps'
import { loadCatalog } from './edit/AppEditor'
import { DispositionDialog } from './edit/ReviewSections'
import { AppMeta, BackLink, LoadError, appPath } from './shared'

/** 编辑层上的矩形：相对编辑层的左上角 */
interface Box { top: number; left: number; width: number; height: number }

/**
 * 服务端给的「草稿和当前发布版是否一致」（AppDetail.draft_changed，见报告的接口约定请求）。没给的当不知道：
 * 只在这一次改过之后说「有未发布的改动」，不在浏览器里拿草稿和发布版逐项比（发布时服务端会规整 spec，比不准）
 */
const draftChangedOf = (d: AppDetail): boolean | null => {
  const v = (d as AppDetail & { draft_changed?: unknown }).draft_changed
  return typeof v === 'boolean' ? v : null
}

/** 组件在界面上叫什么：属性里写的名字、标题，再退到目录里的中文名 */
function labelOf(comp: AppComponent | undefined, entry: CatalogComponent | undefined): string {
  const named = comp?.props?.label ?? comp?.props?.title
  return typeof named === 'string' && named.trim() ? named.trim() : entry?.label ?? comp?.type ?? ''
}

/** 一页里（含子组件）的全部组件 */
function walkSpec(list: AppComponent[], out: AppComponent[] = []): AppComponent[] {
  for (const c of list) { out.push(c); if (c.children?.length) walkSpec(c.children, out) }
  return out
}
function walkView(list: ViewNode[] | undefined, out = new Set<string>()): Set<string> {
  for (const n of list ?? []) { out.add(n.id); walkView(n.children, out) }
  return out
}

export interface PageEditorProps {
  appId: string
  /** manage 取到的详情：带着草稿 */
  detail: AppDetail
  pageId?: string
  /** 地址里的页面参数（原文） */
  rawParams: string | null
  onExit: () => void
  onNavigate: (pageId: string) => void
  /** 发布了新的应用版本：页面外层重取详情 */
  onPublished: () => void
}

export function PageEditor({ appId, detail, pageId, rawParams, onExit, onNavigate, onPublished }: PageEditorProps) {
  const builder = useAtLeast('builder')
  const personal = detail.scope === 'personal'
  const [draft, setDraftState] = useState<AppSpec>(() => detail.draft as AppSpec)
  // 最新的草稿、最近一次存上的草稿：几次改动挨着到时，存的是最新的那份；存失败退回存上的那份
  const latest = useRef<AppSpec>(detail.draft as AppSpec)
  const saved = useRef<AppSpec>(detail.draft as AppSpec)
  const chain = useRef<Promise<void>>(Promise.resolve())
  const [saveState, setSaveState] = useState<'idle' | 'saving' | 'saved' | 'failed'>('idle')
  const [changed, setChanged] = useState<boolean | null>(() => draftChangedOf(detail))
  const [catalog, setCatalog] = useState<ComponentCatalog | null>(null)
  useEffect(() => { loadCatalog().then(setCatalog).catch(() => undefined) }, [])
  const byName = useMemo(() => new Map((catalog?.components ?? []).map((c) => [c.name, c])), [catalog])

  const page = draft.pages.find((p) => p.id === pageId) ?? draft.pages.find((p) => p.id === 'home') ?? draft.pages[0]
  const pageKey = page?.id ?? ''
  usePageTitle(`${APP_TEXT.title} · ${draft.name} · ${T.editing}`)

  // ---- 按草稿解析这一页
  const [model, setModel] = useState<ViewModel | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [loading, setLoading] = useState(false)
  const seq = useRef(0)
  const resolve = useCallback(async () => {
    if (!pageKey) return
    const me = ++seq.current
    setLoading(true)
    try {
      const next = await api.apps.page(appId, pageKey, { draft: true, params: parseViewParams(rawParams) })
      if (me === seq.current) { setModel(next); setError(null) }
    } catch (e) {
      if (me === seq.current) setError(e)
    } finally {
      if (me === seq.current) setLoading(false)
    }
  }, [appId, pageKey, rawParams])
  useEffect(() => { void resolve() }, [resolve])

  // ---- 落地：应用操作 → 存草稿 → 重新解析
  const persist = useCallback(async () => {
    const spec = latest.current
    if (spec === saved.current) return
    setSaveState('saving')
    try {
      const next = await api.apps.update(appId, { draft: spec })
      saved.current = spec
      setChanged(draftChangedOf(next) ?? true)
      // 存的期间又改了的，下一轮接着存，这一轮先不报「已存」
      if (latest.current === spec) setSaveState('saved')
    } catch (e) {
      toast.error(e)
      latest.current = saved.current
      setDraftState(saved.current)
      setSaveState('failed')
    }
    await resolve()
  }, [appId, resolve])
  const apply = useCallback((op: Record<string, unknown>): boolean => {
    const next = applyAppOp(latest.current, op)
    if (next === latest.current) return false
    latest.current = next
    setDraftState(next)
    chain.current = chain.current.then(persist)
    return true
  }, [persist])

  // ---- 选中、悬停、编辑层上的位置
  const layer = useRef<HTMLDivElement>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [hover, setHover] = useState<string | null>(null)
  const [boxes, setBoxes] = useState<{ hover?: Box; selected?: Box }>({})
  const measure = useCallback(() => {
    const root = layer.current
    const rectOf = (id: string | null): Box | undefined => {
      if (!root || !id) return undefined
      const el = root.querySelector<HTMLElement>(`[data-node="${CSS.escape(id)}"]`)
      if (!el) return undefined
      const r = el.getBoundingClientRect()
      const o = root.getBoundingClientRect()
      return { top: r.top - o.top, left: r.left - o.left, width: r.width, height: r.height }
    }
    setBoxes({ hover: hover !== selected ? rectOf(hover) : undefined, selected: rectOf(selected) })
  }, [hover, selected])
  useLayoutEffect(measure, [measure, model])
  const measureRef = useRef(measure)
  measureRef.current = measure
  useEffect(() => {
    const root = layer.current
    if (!root) return
    // 图表、表格晚到会把下面的组件往下推：编辑层的大小一变就重量一次
    const again = () => measureRef.current()
    const ro = new ResizeObserver(again)
    ro.observe(root)
    window.addEventListener('resize', again)
    return () => { ro.disconnect(); window.removeEventListener('resize', again) }
  }, [model])
  // 选中的组件被删掉了、换页了：不再选中
  useEffect(() => { if (selected && !findComponent(draft, selected)) setSelected(null) }, [draft, selected])
  useEffect(() => { setSelected(null); setHover(null) }, [pageKey])

  const nodeOf = (target: EventTarget | null): string | null => {
    const el = target instanceof Element ? target.closest<HTMLElement>('[data-node]') : null
    if (!el || !layer.current?.contains(el)) return null
    const id = el.getAttribute('data-node')
    return id && findComponent(latest.current, id) ? id : null
  }
  /** 编辑状态下点组件就是选中：捕获阶段拦下，组件里的按钮、链接不再响应 */
  const onClickCapture = (e: ReactMouseEvent) => {
    if ((e.target as Element).closest?.('[data-page-edit-toolbar]')) return
    const id = nodeOf(e.target)
    if (!id) return
    e.preventDefault()
    e.stopPropagation()
    setSelected(id)
  }
  const onKeyDown = (e: ReactKeyboardEvent) => {
    if (e.key === 'Escape' && selected) { e.stopPropagation(); setSelected(null); return }
    // 键盘：组件在编辑状态下可以聚焦（ViewRenderer 给 data-node 加了 tabIndex），回车选中
    if ((e.key === 'Enter' || e.key === ' ') && (e.target as Element).hasAttribute?.('data-node')) {
      const id = nodeOf(e.target)
      if (id) { e.preventDefault(); setSelected(id) }
    }
  }

  // ---- 草稿里隐藏了的组件：编辑状态下画成半透明；服务端没画出来的列在页底
  const hiddenIds = useMemo(() => new Set(walkSpec(page?.body ?? []).filter((c) => c.hidden).map((c) => c.id)), [page])
  const marks = useMemo<ViewEditMarks>(() => ({ hidden: hiddenIds }), [hiddenIds])
  const notDrawn = useMemo(() => {
    const drawn = walkView(model?.tree)
    return walkSpec(page?.body ?? []).filter((c) => c.hidden && !drawn.has(c.id))
  }, [model, page])

  const at = selected ? findComponent(draft, selected) : null
  const [textFor, setTextFor] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)

  const remove = async (id: string) => {
    const comp = findComponent(latest.current, id)?.node
    if (!comp) return
    const label = labelOf(comp, byName.get(comp.type))
    if (!(await confirmDialog({ title: T.removeTitle(label), consequences: T.removeConsequences, confirmLabel: T.removeConfirm,
      danger: true }))) return
    if (apply({ op: 'app_remove', id })) setSelected(null)
  }
  const move = (delta: -1 | 1) => {
    if (!at) return
    // 同一层里挪一格（应用操作的 index 指拿掉之后的位置）
    apply({ op: 'app_move', id: at.node.id, parent: at.parent?.id ?? at.page.id, index: at.index + delta })
  }

  // ---- 发布：走现有的应用发布
  const [publishing, setPublishing] = useState(false)
  const [blocked, setBlocked] = useState<LintIssue[] | null>(null)
  const [asking, setAsking] = useState<NonNullable<PublishResult['needs_disposition']> | null>(null)
  const publish = async (dispositions?: Record<string, Disposition>) => {
    if (!dispositions && !(await confirmDialog({ title: T.publishTitle, consequences: T.publishConsequences,
      confirmLabel: T.publishConfirm }))) return
    setPublishing(true)
    setBlocked(null)
    try {
      // 还在存的先存完：发布读的是服务端那份草稿
      await chain.current
      const res = await api.apps.publish(appId, dispositions ? { dispositions } : {})
      if (res.ok && res.version != null) {
        toast.ok(T.published(res.version))
        setAsking(null)
        setChanged(false)
        onPublished()
        return
      }
      if (res.needs_disposition?.length && !dispositions) setAsking(res.needs_disposition)
      const rest = res.issues.filter((i) => i.code !== 'app.upgrade_policy' || !res.needs_disposition?.length)
      setBlocked(rest.length ? rest : null)
    } catch (e) {
      toast.error(e)
    } finally {
      setPublishing(false)
    }
  }

  const status = personal ? T.personalNoPublish
    : detail.published_version == null ? T.neverPublished
    : changed === true ? T.unpublished
    : changed === false ? T.draftSameAsPublished
    : null
  const pages = model?.pages ?? draft.pages.map((p) => ({ id: p.id, title: p.title }))

  return (
    <div className="flex h-full flex-col" data-app-page={pageKey} data-page-edit={appId}>
      <PageHeader
        icon={<LayoutGrid size={14} />}
        title={draft.name}
        subtitle={T.editing}
        actions={(
          <div className="flex items-center gap-1.5">
            {loading && model && <Spinner size={12} />}
            <button type="button" className="btn btn-sm btn-primary" onClick={onExit} data-page-edit-done="">
              <Check size={12} aria-hidden /> {T.done}
            </button>
          </div>
        )}
      />
      {pages.length > 1 && (
        <Tabs label={APP_TEXT.pagesLabel} tabs={pages.map((p) => ({ key: p.id, label: p.title || p.id }))}
              active={pageKey} onChange={onNavigate} />
      )}
      <div className="min-h-0 flex-1 overflow-y-auto">
        {/* 编辑条：停在顶上，状态、发布、改取数随时够得着 */}
        <div className="sticky top-0 z-20 border-b bg-panel/95 px-4 py-2 backdrop-blur" data-page-edit-bar="">
          <div className="mx-auto flex max-w-5xl flex-wrap items-center gap-2 text-xs">
            <span className="chip" style={{ color: 'var(--accent)' }}><PencilLine size={10} aria-hidden /> {T.editing}</span>
            {status && (
              <span className={clsx(changed === true || detail.published_version == null ? 'font-medium text-fg' : 'text-dim')}
                    data-page-edit-status={personal ? 'personal' : detail.published_version == null ? 'never' : String(changed)}>
                {status}
              </span>
            )}
            <span className="text-2xs text-faint" data-page-edit-save={saveState} aria-live="polite">
              {saveState === 'saving' ? T.saving : saveState === 'saved' ? T.saved : saveState === 'failed' ? T.saveFailed : ''}
            </span>
            <span className="flex-1" />
            {detail.blueprint_id && (
              <Link to={`/blueprints/${encodeURIComponent(detail.blueprint_id)}`} className="btn btn-sm" title={T.changeDataHint}
                    data-page-edit-change-data="">
                <Database size={11} aria-hidden /> {T.changeData}
              </Link>
            )}
            {builder && !personal && (
              <Link to={`${appPath(appId)}/edit/pages`} className="btn btn-sm btn-ghost" title={T.openEditorHint} data-page-edit-editor="">
                <Settings2 size={11} aria-hidden /> {T.openEditor}
              </Link>
            )}
            {!personal && (
              <button type="button" className="btn btn-sm btn-primary" disabled={publishing || changed === false}
                      onClick={() => void publish()} data-page-edit-publish="">
                {publishing ? <Spinner size={11} /> : <ShieldCheck size={11} aria-hidden />} {publishing ? T.publishing : T.publish}
              </button>
            )}
          </div>
        </div>
        <div className="mx-auto max-w-5xl space-y-3 p-4">
          <AppMeta app={{ version: detail.published_version ?? 0, level: detail.level }}
                   back={<BackLink to="/apps?tab=apps" label={APP_TEXT.backToApps} />} />
          <p className="text-2xs leading-relaxed text-faint">{T.pickHint}</p>
          {blocked && (
            <Notice tone="err" attr={{ 'data-page-edit-blocked': String(blocked.length) }}>
              <div className="font-medium">{T.publishBlocked}</div>
              <ul className="mt-1 list-disc space-y-0.5 pl-4">
                {blocked.map((issue, i) => <li key={i} data-app-issue={issue.code}>{issue.message}</li>)}
              </ul>
              <Link to={`${appPath(appId)}/edit/check`} className="mt-1 inline-block underline underline-offset-2">{T.openCheck}</Link>
            </Notice>
          )}
          {error && !model ? <LoadError error={error} onRetry={() => void resolve()} />
            : !model ? <Skeleton rows={6} />
            : (
              // 编辑层：叠在渲染器外面，捕获点击、悬停描边、选中出工具条
              <div ref={layer} className="relative" data-page-edit-layer="" onClickCapture={onClickCapture} onKeyDown={onKeyDown}
                   onMouseOver={(e) => setHover(nodeOf(e.target))} onMouseLeave={() => setHover(null)}>
                <ViewRenderer model={model} appId={appId} preview editing={marks} onNavigate={onNavigate}
                              onReload={() => void resolve()} />
                {boxes.hover && (
                  <div aria-hidden className="pointer-events-none absolute z-10 rounded-md border border-dashed"
                       style={{ ...boxes.hover, borderColor: 'var(--accent)' }} />
                )}
                {boxes.selected && at && (
                  <>
                    <div aria-hidden className="pointer-events-none absolute z-10 rounded-md ring-2 ring-[var(--accent)]"
                         style={boxes.selected} data-page-edit-selected={at.node.id} />
                    <Toolbar box={boxes.selected} comp={at.node} entry={byName.get(at.node.type)}
                             first={at.index === 0} last={at.index >= at.list.length - 1}
                             onText={() => setTextFor(at.node.id)}
                             onHide={() => apply({ op: 'app_update', id: at.node.id, hidden: !at.node.hidden })}
                             onMove={move} onRemove={() => void remove(at.node.id)} />
                  </>
                )}
              </div>
            )}
          {notDrawn.length > 0 && (
            <div className="rounded-lg border border-dashed px-3 py-2 text-xs" data-page-edit-hidden-list={notDrawn.length}>
              <div className="text-dim">{T.hiddenElsewhere(notDrawn.length)}</div>
              <ul className="mt-1 flex flex-wrap gap-1.5">
                {notDrawn.map((c) => (
                  <li key={c.id}>
                    <button type="button" className="chip hover:text-fg" data-page-edit-unhide={c.id}
                            onClick={() => apply({ op: 'app_update', id: c.id, hidden: false })}>
                      <Eye size={10} aria-hidden /> {labelOf(c, byName.get(c.type))} · {T.unhide}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {page && (
            <button type="button" className="flex w-full items-center justify-center gap-1.5 rounded-lg border border-dashed px-3 py-3 text-xs text-dim hover:border-[var(--accent)] hover:text-fg"
                    title={T.addCardHint} onClick={() => setAdding(true)} data-page-edit-add-card="">
              <Plus size={12} aria-hidden /> {T.addCard}
            </button>
          )}
        </div>
      </div>
      {textFor && (() => {
        const comp = findComponent(draft, textFor)?.node
        return comp ? (
          <TextDialog comp={comp} entry={byName.get(comp.type)} onClose={() => setTextFor(null)}
                      onSave={(props) => { apply({ op: 'app_update', id: comp.id, props }); setTextFor(null) }} />
        ) : null
      })()}
      {adding && page && (
        <CandidatesDialog appId={appId} onClose={() => setAdding(false)} onPick={(c) => {
          const comp = freshComponent(latest.current, c.component)
          if (apply({ op: 'app_add', parent: page.id, component: comp })) {
            setSelected(comp.id)
            toast.ok(T.added(c.label))
          }
          setAdding(false)
        }} />
      )}
      {asking && <DispositionDialog items={asking} busy={publishing} onClose={() => setAsking(null)} onConfirm={(d) => void publish(d)} />}
    </div>
  )
}

/** 选中组件的小工具条：浮在组件右上角，组件太靠上时放进组件里面 */
function Toolbar({ box, comp, entry, first, last, onText, onHide, onMove, onRemove }: {
  box: Box; comp: AppComponent; entry?: CatalogComponent; first: boolean; last: boolean
  onText: () => void; onHide: () => void; onMove: (delta: -1 | 1) => void; onRemove: () => void
}) {
  const label = labelOf(comp, entry)
  const texts = textPropsOf(entry?.props)
  const top = box.top >= 34 ? box.top - 32 : box.top + 4
  return (
    <div className="pointer-events-none absolute z-20 flex justify-end" style={{ top, left: box.left, width: box.width }}>
      <div role="toolbar" aria-label={T.toolbar(label)} data-page-edit-toolbar={comp.id}
           className="pointer-events-auto flex items-center gap-0.5 rounded-md border bg-panel p-0.5 shadow-md">
        <span className="max-w-40 truncate px-1.5 text-2xs text-dim">{label}</span>
        <button type="button" className="btn btn-xs btn-ghost" onClick={onText} disabled={!texts.length}
                title={texts.length ? T.editText : T.noText} data-page-edit-op="text">
          <Type size={11} aria-hidden /> {T.editText}
        </button>
        <button type="button" className="btn btn-xs btn-ghost" onClick={onHide} aria-pressed={!!comp.hidden}
                title={T.hiddenHint} data-page-edit-op="hide">
          {comp.hidden ? <Eye size={11} aria-hidden /> : <EyeOff size={11} aria-hidden />} {comp.hidden ? T.unhide : T.hide}
        </button>
        <button type="button" className="btn btn-xs btn-ghost" onClick={() => onMove(-1)} disabled={first}
                aria-label={T.moveUp} title={T.moveUp} data-page-edit-op="up">
          <ArrowUp size={11} aria-hidden />
        </button>
        <button type="button" className="btn btn-xs btn-ghost" onClick={() => onMove(1)} disabled={last}
                aria-label={T.moveDown} title={T.moveDown} data-page-edit-op="down">
          <ArrowDown size={11} aria-hidden />
        </button>
        <button type="button" className="btn btn-xs btn-ghost text-[var(--err)]" onClick={onRemove}
                aria-label={T.remove} title={T.remove} data-page-edit-op="remove">
          <Trash2 size={11} aria-hidden />
        </button>
      </div>
    </div>
  )
}

/** 改文字：只改目录里标了 x-text 的属性；清空的去掉这一项（同属性面板） */
function TextDialog({ comp, entry, onClose, onSave }: {
  comp: AppComponent; entry?: CatalogComponent; onClose: () => void; onSave: (props: Record<string, string | null>) => void
}) {
  const fields = textPropsOf(entry?.props)
  const initial = (key: string) => (typeof comp.props?.[key] === 'string' ? comp.props[key] as string : '')
  const [values, setValues] = useState<Record<string, string>>(() => Object.fromEntries(fields.map((f) => [f.key, initial(f.key)])))
  const changedKeys = fields.filter((f) => (values[f.key] ?? '') !== initial(f.key)).map((f) => f.key)
  const save = () => onSave(Object.fromEntries(changedKeys.map((k) => [k, values[k].trim() ? values[k] : null])))
  return (
    <Modal open onClose={onClose} title={T.editTextTitle(labelOf(comp, entry))} width={520} dirty={changedKeys.length > 0}
           footer={<>
             <button type="button" className="btn" onClick={onClose}>{T.close}</button>
             <button type="button" className="btn btn-primary" disabled={!changedKeys.length} onClick={save} data-page-edit-text-save="">
               {T.saveText}
             </button>
           </>}>
      <p className="mb-3 text-2xs leading-relaxed text-faint">{T.editTextHint}</p>
      {!fields.length ? <EmptyState title={T.noText} offline={false} /> : (
        <div className="space-y-3" data-page-edit-text={comp.id}>
          {fields.map((f) => {
            const id = `page-edit-text-${f.key}`
            return (
              <div key={f.key} data-page-edit-text-field={f.key}>
                <label className="label" htmlFor={id}>{f.title}</label>
                {f.long
                  ? <textarea id={id} className="field text-xs" rows={4} value={values[f.key] ?? ''} maxLength={4000}
                              onChange={(e) => setValues((v) => ({ ...v, [f.key]: e.target.value }))} />
                  : <input id={id} className="field" value={values[f.key] ?? ''} maxLength={200}
                           onChange={(e) => setValues((v) => ({ ...v, [f.key]: e.target.value }))} />}
              </div>
            )
          })}
        </div>
      )}
    </Modal>
  )
}

/** 候选按类别分组的顺序：口径卡指标、报告、明细、趋势（§4.5） */
const KIND_ORDER: AppCandidate['kind'][] = ['metric', 'report', 'table', 'series']

/** 加一张数据卡片：候选由服务端按派生规则从固定版本里列（同一份 _results），按类别分组 */
function CandidatesDialog({ appId, onClose, onPick }: { appId: string; onClose: () => void; onPick: (c: AppCandidate) => void }) {
  const [items, setItems] = useState<AppCandidate[] | null>(null)
  const [error, setError] = useState<unknown>(null)
  const load = useCallback(() => {
    setError(null)
    api.personal.candidates(appId).then((r) => setItems(r.items), setError)
  }, [appId])
  useEffect(load, [load])
  const groups = useMemo(() => KIND_ORDER.map((kind) => ({ kind, items: (items ?? []).filter((c) => c.kind === kind) }))
    .filter((g) => g.items.length), [items])
  return (
    <Modal open onClose={onClose} title={T.addCard} width={560}
           footer={<button type="button" className="btn" onClick={onClose}>{T.close}</button>}>
      <p className="mb-3 text-2xs leading-relaxed text-faint">{T.addCardHint}</p>
      {error ? <LoadError error={error} onRetry={load} />
        : !items ? <div className="flex items-center gap-2 text-2xs text-faint"><Spinner size={11} /> {T.candidatesLoading}</div>
        : !groups.length ? <EmptyState title={T.candidatesEmpty} offline={false} />
        : (
          <div className="space-y-3" data-page-edit-candidates={items.length}>
            {groups.map((g) => (
              <section key={g.kind} data-candidate-group={g.kind}>
                <h3 className="mb-1 text-2xs font-semibold text-faint">{T.candidateKinds[g.kind] ?? g.kind}</h3>
                <ul className="divide-y rounded-lg border">
                  {g.items.map((c, i) => (
                    <li key={`${c.component.id}-${i}`} className="flex items-center gap-2 px-3 py-2 text-xs" data-candidate={c.component.id}>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-fg">{c.label}</span>
                        {c.group && <span className="block truncate text-2xs text-faint">{c.group}</span>}
                      </span>
                      <button type="button" className="btn btn-xs" onClick={() => onPick(c)} data-candidate-add="">
                        <Plus size={10} aria-hidden /> {T.addThis}
                      </button>
                    </li>
                  ))}
                </ul>
              </section>
            ))}
          </div>
        )}
    </Modal>
  )
}
