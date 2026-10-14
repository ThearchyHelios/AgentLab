/**
 * 应用编辑（搭建面，docs/design/app-platform-and-look.md 第四部分、A1、A2、A8）：/apps/:appId/edit[/:section]。
 *
 * 分节：固定版本、页面（页面树 + 属性面板）、预览、检查、发布、岗位与授权、钩子、通知；右侧是助手（应用润色）。
 * 草稿存在应用上（PATCH /api/apps/{id}），只在发布时生成不可变的应用版本——草稿不进版本表（F3 的教训）。
 * 预览按草稿解析布局、不发起运行；检查和发布同一套规则，都读服务端那份草稿，所以先保存再查。
 *
 * /apps/new/edit?workflow=<id>&version=<v>：从一个已发布的工作流版本确定性地派生、新建应用（发布成功的弹窗里点
 * 「做成应用」到这里）。不带参数时先选工作流和版本。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { Bot, ExternalLink, LayoutGrid, Save } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../../../api/client'
import { ErrorState, Field, PageHeader, Skeleton, Spinner, confirmDialog, isComposing, toast } from '../../../components/ui'
import { formatTime } from '../../../lib/format'
import { matchShortcut } from '../../../lib/keys'
import { useLeaveGuard } from '../../../lib/leave'
import { APP_EDITOR_TEXT as T } from '../../../lib/terms'
import { useCatalog } from '../../../store/catalog'
import type { AppBindings, AppDetail, AppSpec, ComponentCatalog } from '../../../typesApp'
import { EditorContext, type EditorState } from './context'
import { PinsSection, PublishedVersionPicker } from './PinsSection'
import { PagesSection } from './PagesSection'
import { CheckSection, PreviewSection, PublishSection } from './ReviewSections'
import { AccessSection, HooksSection, NotifySection } from './AccessSections'
import { AppAssistant } from './AppAssistant'

const SECTIONS = ['pins', 'pages', 'preview', 'check', 'publish', 'access', 'hooks', 'notify'] as const
type Section = typeof SECTIONS[number]

/** 组件目录整站一份，进一次编辑器取一次就够 */
let catalogCache: Promise<ComponentCatalog> | null = null
const loadCatalog = () => {
  catalogCache ??= api.apps.catalog().catch((e) => { catalogCache = null; throw e })
  return catalogCache
}

export function AppEditor() {
  const { appId = '' } = useParams()
  return appId === 'new' ? <NewApp /> : <EditApp key={appId} appId={appId} />
}

// -------------------------------------------------------------------------

/** 新建：选一个已发布的版本，派生出 spec，建应用，跳进编辑器 */
function NewApp() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const workflows = useCatalog((s) => s.workflows)
  const [workflowId, setWorkflowId] = useState(params.get('workflow') ?? '')
  const [version, setVersion] = useState<number | null>(Number(params.get('version')) || null)
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const auto = useRef(!!params.get('workflow') && !!params.get('version'))

  const create = useCallback(async (wf: string, v: number, appName?: string) => {
    setBusy(true)
    try {
      const { spec } = await api.apps.derive({ workflow_id: wf, version: v, name: appName || undefined })
      const app = await api.apps.create({ name: spec.name, description: spec.description, spec })
      toast.ok(T.created(app.name))
      navigate(`/apps/${encodeURIComponent(app.id)}/edit/pages`, { replace: true })
    } catch (e) {
      toast.error(e)
      setBusy(false)
    }
  }, [navigate])

  // 发布弹窗的「做成应用」带着工作流和版本来：直接派生，不再让人选一遍
  useEffect(() => {
    if (!auto.current || !workflowId || version == null) return
    auto.current = false
    void create(workflowId, version)
  }, [workflowId, version, create])

  const wf = workflows.find((w) => w.id === workflowId)
  return (
    <div className="flex h-full flex-col" data-app-editor="new">
      <PageHeader icon={<LayoutGrid size={14} />} title={T.newTitle} />
      <div className="mx-auto w-full max-w-xl space-y-3 p-6">
        <p className="text-xs leading-relaxed text-dim">{T.newIntro}</p>
        <Field label={T.pickWorkflow}>
          {(p) => (
            <select {...p} className="field" value={workflowId} disabled={busy}
                    onChange={(e) => { setWorkflowId(e.target.value); setVersion(null) }}>
              <option value="">{T.pickWorkflowFirst}</option>
              {workflows.filter((w) => w.published_version != null).map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
            </select>
          )}
        </Field>
        {workflowId && (
          <PublishedVersionPicker workflowId={workflowId} value={version} onChange={(v) => setVersion(v?.version ?? null)} />
        )}
        <Field label={T.appName}>
          {(p) => <input {...p} className="field" value={name} placeholder={wf?.name ?? ''} disabled={busy}
                         onChange={(e) => setName(e.target.value)} />}
        </Field>
        <div className="flex justify-end">
          <button type="button" className="btn btn-primary" disabled={busy || !workflowId || version == null}
                  onClick={() => version != null && void create(workflowId, version, name.trim())} data-app-derive="">
            {busy ? <Spinner size={11} /> : <LayoutGrid size={11} aria-hidden />} {busy ? T.deriving : T.derive}
          </button>
        </div>
      </div>
    </div>
  )
}

// -------------------------------------------------------------------------

function EditApp({ appId }: { appId: string }) {
  const { section: raw } = useParams()
  const navigate = useNavigate()
  const section: Section = (SECTIONS as readonly string[]).includes(raw ?? '') ? raw as Section : 'pages'
  const [detail, setDetail] = useState<AppDetail | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [draft, setDraftState] = useState<AppSpec | null>(null)
  const [bindings, setBindingsState] = useState<AppBindings>({ slots: {} })
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [savedAt, setSavedAt] = useState<number | null>(null)
  const [catalog, setCatalog] = useState<ComponentCatalog | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [assistant, setAssistant] = useState(true)
  // 保存用的是最新的草稿：助手的操作和人的编辑挨着到时，闭包里那份可能已经旧了
  const latest = useRef<{ draft: AppSpec | null; bindings: AppBindings }>({ draft: null, bindings: { slots: {} } })
  latest.current = { draft, bindings }

  const load = useCallback(async () => {
    try {
      const d = await api.apps.get(appId)
      setDetail(d)
      setDraftState(d.draft)
      setBindingsState(d.draft_bindings ?? { slots: {} })
      setDirty(false)
      setError(null)
    } catch (e) {
      setError(e)
    }
  }, [appId])
  useEffect(() => { void load() }, [load])
  useEffect(() => { loadCatalog().then(setCatalog).catch(() => undefined) }, [])

  // 没在跳转的那一节（地址里写错了、老链接）：纠正成页面
  useEffect(() => {
    if (raw && raw !== section) navigate(`/apps/${encodeURIComponent(appId)}/edit/${section}`, { replace: true })
  }, [raw, section, appId, navigate])

  const save = useCallback(async (): Promise<boolean> => {
    const cur = latest.current
    if (!cur.draft) return false
    setSaving(true)
    try {
      const next = await api.apps.update(appId, { name: cur.draft.name, description: cur.draft.description,
                                                   draft: cur.draft, draft_bindings: cur.bindings })
      setDetail(next)
      // 保存期间又改了的，那些改动还算没保存
      if (latest.current.draft === cur.draft && latest.current.bindings === cur.bindings) setDirty(false)
      setSavedAt(Date.now())
      return true
    } catch (e) {
      toast.error(e)
      return false
    } finally {
      setSaving(false)
    }
  }, [appId])

  // 只在离开编辑器时问：切换分节是同一个草稿
  useLeaveGuard(dirty, () => confirmDialog({
    title: T.unsaved, body: T.leaveBody, confirmLabel: '放弃修改', cancelLabel: '返回保存', danger: true,
  }), {
    blocks: (next) => !next?.pathname.startsWith(`/apps/${encodeURIComponent(appId)}/edit`),
  })

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isComposing(e) || !matchShortcut(e, 'Mod+S')) return
      e.preventDefault()
      void save()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [save])

  const ctx = useMemo<EditorState | null>(() => (detail && draft ? {
    appId, detail, draft, catalog, dirty, selected,
    setDraft: (fn) => { setDraftState((cur) => (cur ? fn(cur) : cur)); setDirty(true) },
    setBindings: (fn) => { setBindingsState((cur) => fn(cur)); setDirty(true) },
    bindings, save,
    select: setSelected,
    goto: (s, componentId) => {
      if (componentId !== undefined) setSelected(componentId)
      navigate(`/apps/${encodeURIComponent(appId)}/edit/${s}`)
    },
  } : null), [appId, detail, draft, catalog, dirty, selected, bindings, save, navigate])

  if (error) return <ErrorState error={error} onRetry={() => void load()} className="m-6" />
  if (!ctx) return <div className="p-6"><Skeleton rows={6} height={28} /></div>

  return (
    <EditorContext.Provider value={ctx}>
      <div className="flex h-full flex-col" data-app-editor={appId} data-app-section={section}>
        <PageHeader
          icon={<LayoutGrid size={14} />}
          title={`${T.title} · ${ctx.draft.name}`}
          subtitle={dirty ? T.unsaved : savedAt ? T.savedAt(formatTime(new Date(savedAt).toISOString())) : undefined}
          actions={<>
            {ctx.detail.published_version != null && (
              <Link to={`/apps/${encodeURIComponent(appId)}`} className="btn btn-sm"><ExternalLink size={11} aria-hidden /> {T.open}</Link>
            )}
            <button type="button" className={clsx('btn btn-sm', assistant && 'text-[var(--accent)]')} aria-pressed={assistant}
                    title={T.assistantToggle} onClick={() => setAssistant((v) => !v)}>
              <Bot size={11} aria-hidden /> {T.assistant}
            </button>
            <button type="button" className="btn btn-sm btn-primary" disabled={!dirty || saving} onClick={() => void save()}
                    data-app-save="">
              {saving ? <Spinner size={11} /> : <Save size={11} aria-hidden />} {saving ? T.saving : T.save}
            </button>
          </>}
        />
        <div className="flex min-h-0 flex-1">
          <nav className="w-36 shrink-0 border-r p-2" aria-label={T.title}>
            {SECTIONS.map((s) => (
              <Link key={s} to={`/apps/${encodeURIComponent(appId)}/edit/${s}`} replace
                    aria-current={s === section ? 'page' : undefined} data-app-nav={s}
                    className={clsx('block rounded-md px-2 py-1.5 text-xs hover:bg-hover', s === section && 'bg-hover font-medium')}>
                {T.sections[s]}
              </Link>
            ))}
          </nav>
          <main className="min-w-0 flex-1 overflow-y-auto" data-app-pane={section}>
            {section === 'pins' && <PinsSection />}
            {section === 'pages' && (ctx.catalog ? <PagesSection /> : <div className="p-4"><Skeleton rows={4} /></div>)}
            {section === 'preview' && <PreviewSection />}
            {section === 'check' && <CheckSection />}
            {section === 'publish' && <PublishSection onPublished={() => void load()} />}
            {section === 'access' && <AccessSection />}
            {section === 'hooks' && <HooksSection />}
            {section === 'notify' && <NotifySection />}
          </main>
          {assistant && (
            <aside className="flex w-[360px] shrink-0 flex-col border-l bg-panel" aria-label={T.assistant}>
              <AppAssistant />
            </aside>
          )}
        </div>
      </div>
    </EditorContext.Provider>
  )
}
