/**
 * 发起（/apps/:appId/new/:entryId）：入口表单（agentlab.form/1），提交后从应用固定的工作流版本发起一次正式运行，进实例页。
 *
 * 表单取自应用首页的视图模型（服务端把各入口的表单放在 forms 里，键是入口 id）。先按子集在本地校验，服务端用 jsonschema
 * 复核，422 的错误按 path 挂回字段。引用的版本退役了照实说，不让提交。Mod+Enter 发起。
 */
import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { LayoutGrid, Play } from 'lucide-react'
import { ApiError, api } from '../../api/client'
import { SchemaForm } from '../../components/SchemaForm'
import { EmptyState, Notice, PageHeader, Skeleton, Spinner, useToast } from '../../components/ui'
import { cleanValues, formErrorsOf, validateForm, withDefaults } from '../../lib/schemaForm'
import { usePageTitle } from '../../lib/notify'
import { formatShortcut } from '../../lib/keys'
import { APP_TEXT, ENTRY_TEXT } from '../../lib/termsApp'
import type { FormError, ViewModel, ViewNode } from '../../typesApp'
import { AppMeta, BackLink, LoadError, appPath, loadHome, useLoad } from './shared'

/** 入口的名字：页面上发起按钮写的那个；没有就用表单的标题，再没有用入口 id */
function entryLabel(model: ViewModel, entryId: string): string {
  const walk = (nodes: ViewNode[]): string | null => {
    for (const n of nodes) {
      if (n.type === 'entry_button' && (n.props?.entry === entryId || n.data?.entry === entryId)) {
        const label = n.data?.label || n.props?.label
        if (typeof label === 'string' && label) return label
      }
      const inner = n.children ? walk(n.children) : null
      if (inner) return inner
    }
    return null
  }
  return walk(model.tree) ?? model.forms[entryId]?.title ?? entryId
}

export function EntryPage() {
  const { appId = '', entryId = '' } = useParams()
  const navigate = useNavigate()
  const toast = useToast()
  const { data: model, error, reload } = useLoad((signal) => loadHome(appId, undefined, { signal }), [appId])
  const schema = model?.forms[entryId]
  const label = model ? entryLabel(model, entryId) : ''
  usePageTitle(model ? `${APP_TEXT.title} · ${model.app.name} · ${label}` : null)
  const [values, setValues] = useState<Record<string, unknown>>({})
  const [errors, setErrors] = useState<FormError[]>([])
  const [busy, setBusy] = useState(false)
  // 表单到了才填默认值；换了入口重新来
  useEffect(() => { setValues(schema ? withDefaults(schema, {}) : {}); setErrors([]) }, [schema])

  const blocked = !!model?.app.needs_upgrade
  const submit = async () => {
    if (!schema || busy || blocked) return
    const local = validateForm(schema, values)
    setErrors(local)
    if (local.length) {
      toast.warn(ENTRY_TEXT.invalid)
      return
    }
    setBusy(true)
    try {
      const res = await api.apps.start(appId, entryId, cleanValues(schema, values))
      toast(ENTRY_TEXT.started(res.title || label), 'ok')
      navigate(`${appPath(appId)}/i/${encodeURIComponent(res.run_id)}`)
    } catch (e) {
      const fe = e instanceof ApiError && e.status === 422 ? formErrorsOf(e) : null
      if (fe) {
        setErrors(fe)
        toast.warn(ENTRY_TEXT.invalid)
      } else toast.error(e)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex h-full flex-col" data-entry-page={entryId}>
      <PageHeader icon={<LayoutGrid size={14} />} title={model ? `${model.app.name} · ${label}` : APP_TEXT.title} />
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-2xl space-y-3 p-4">
          {error && !model ? <LoadError error={error} onRetry={reload} />
            : !model ? <Skeleton rows={5} />
            : (
              <>
                <AppMeta app={model.app} back={<BackLink to={appPath(appId)} label={model.app.name} />} />
                {!schema ? <EmptyState title={ENTRY_TEXT.entryMissing(entryId)} offline={false} /> : (
                  <div className="rounded-lg border bg-panel p-4">
                    <h2 className="mb-3 text-sm font-semibold">{label}</h2>
                    {blocked && <Notice tone="warn" className="mb-3" attr={{ 'data-app-needs-upgrade': '' }}>{ENTRY_TEXT.upgrade}</Notice>}
                    <SchemaForm schema={schema} value={values} errors={errors} idPrefix={`entry-${entryId}`}
                                onChange={(v) => { setValues(v); if (errors.length) setErrors([]) }}
                                onSubmit={() => void submit()} />
                    <div className="mt-4 flex flex-wrap items-center gap-3">
                      <button type="button" className="btn btn-primary" disabled={busy || blocked} data-entry-submit=""
                              onClick={() => void submit()}>
                        {busy ? <Spinner size={12} /> : <Play size={12} aria-hidden />} {busy ? ENTRY_TEXT.submitting : ENTRY_TEXT.submit}
                      </button>
                      <span className="text-2xs text-faint">{ENTRY_TEXT.shortcut(formatShortcut('Mod+Enter'))}</span>
                    </div>
                    <p className="mt-2 text-2xs leading-relaxed text-faint">{ENTRY_TEXT.formalNote}</p>
                  </div>
                )}
              </>
            )}
        </div>
      </div>
    </div>
  )
}
