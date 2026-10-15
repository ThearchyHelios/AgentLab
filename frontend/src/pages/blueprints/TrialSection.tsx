/**
 * 试运行（/blueprints/:id/trial?run=…，docs/design/normal-user-apps.md N4、§3.5、§4.3）：应用的样子，对着这次试运行解析。
 *
 * - 页面用应用页同一个 ViewRenderer 渲染 GET /api/blueprints/{id}/preview 的视图模型（预览：不发起正式运行、不提交表单），
 *   印章由服务端写「试运行」；顶部横幅写「试运行：数据是真的，但这不是正式结果」，配这次的输入和「换一期再试」；
 * - 流程类：右侧列试运行里的待办（派给试运行的人自己），用待办卡，按钮写「以试运行身份…」；写入走测试环境或演练；
 *   示例表单里的文件按这张蓝图上传（上下文 blueprint_id），提交时服务端核对后绑到这次试运行；
 * - 出错时用白话说清停在哪、为什么，给「交给助手修」「回到卡片改」。
 *
 * 试运行还在进行时每 4 秒重取一次，完成后自动换上；待办处理完同样重取。
 */
import { useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { FlaskConical, MessageSquare, PencilLine, RotateCw } from 'lucide-react'
import { api } from '../../api/client'
import { ViewRenderer } from '../../apps/ViewRenderer'
import { SchemaForm } from '../../components/SchemaForm'
import { EmptyState, ErrorState, Skeleton, Spinner, Tabs, toast } from '../../components/ui'
import { humanizeError } from '../../lib/errors'
import { cleanValues, validateForm, withDefaults } from '../../lib/schemaForm'
import { TRIAL_TEXT } from '../../lib/terms'
import { TaskCard } from '../../run/TaskCard'
import { useBlueprint } from '../../store/blueprint'
import type { AppSpec, FormError, FormSchema, ViewModel } from '../../typesApp'
import { useLoad, usePoll } from '../apps/shared'
import { blueprintPath } from './shared'

const POLL_MS = 4000
/** 流程类模板：试运行里有人工步骤，右侧要列待办 */
const PROCESS_TEMPLATES = new Set(['approval', 'exception_handling', 'reconcile_writeback'])

/** 应用骨架里第一个入口的表单（试运行的输入） */
function entryForm(spec: AppSpec | null | undefined): FormSchema | null {
  const form = spec?.entries?.[0]?.form
  if (!form) return null
  return '$ref' in form ? spec?.forms?.[String((form as { $ref: string }).$ref)] ?? null : form as FormSchema
}

/** 视图里报错的那一块：服务端在来源上写了为什么 */
function failureOf(model: ViewModel | null | undefined): string | null {
  const failed = Object.values(model?.sources ?? {}).find((s) => s.state === 'error')
  if (failed) return failed.message || TRIAL_TEXT.failedTitle
  return null
}

export function TrialSection({ blueprintId }: { blueprintId: string }) {
  const entry = useBlueprint((s) => s.byId[blueprintId])
  const bp = entry?.blueprint
  const [search, setSearch] = useSearchParams()
  const navigate = useNavigate()
  const runId = search.get('run') ?? bp?.last_trial_run_id ?? null
  const pageId = search.get('page') ?? undefined
  const { data: model, error, reload } = useLoad(
    () => (runId ? api.blueprints.preview(blueprintId, { run: runId, ...(pageId ? { page: pageId } : {}) }) : Promise.resolve(null)),
    [blueprintId, runId, pageId])
  const { data: tasks, reload: reloadTasks } = useLoad(
    (signal) => (runId ? api.tasks.list({ run_id: runId, status: 'pending' }, { signal }) : Promise.resolve({ items: [] })),
    [runId])
  const running = !!model && Object.values(model.sources).some((s) => s.state === 'running')
  usePoll(() => { reload(); reloadTasks() }, POLL_MS, running)

  if (!bp) return null
  if (!runId) {
    return (
      <EmptyState icon={<FlaskConical size={28} />} title={TRIAL_TEXT.none} body={TRIAL_TEXT.noneBody}
                  action={<button type="button" className="btn btn-sm" onClick={() => navigate(blueprintPath(blueprintId))}>
                    <PencilLine size={12} aria-hidden /> {TRIAL_TEXT.backToCards}
                  </button>} />
    )
  }

  const failure = failureOf(model) ?? (error && !model ? humanizeError(error).title : null)
  const fix = (what: string) => {
    useBlueprint.getState().send(blueprintId, TRIAL_TEXT.askAssistantText(what))
    navigate(blueprintPath(blueprintId))
  }
  const process = (tasks?.items.length ?? 0) > 0 || PROCESS_TEMPLATES.has(bp.spec.template?.id ?? bp.template_id ?? '')
  const goPage = (id: string) => setSearch((prev) => { const next = new URLSearchParams(prev); next.set('page', id); return next }, { replace: true })

  return (
    <div className="space-y-3" data-trial={runId}>
      <TrialBanner blueprintId={blueprintId} runId={runId} model={model ?? null}
                   onRetried={(id) => setSearch((prev) => { const next = new URLSearchParams(prev); next.set('run', id); next.delete('page'); return next })} />
      {failure && (
        <div role="alert" className="rounded-lg border px-3 py-2 text-xs leading-relaxed" data-trial-error=""
             style={{ borderColor: 'color-mix(in srgb, var(--err) 40%, var(--border))', background: 'color-mix(in srgb, var(--err) 6%, transparent)' }}>
          <div className="font-medium text-fg">{TRIAL_TEXT.failedTitle}</div>
          <div className="mt-0.5 text-dim">{failure}</div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            <button type="button" className="btn btn-xs btn-primary" onClick={() => fix(failure)} data-trial-fix="">
              <MessageSquare size={11} aria-hidden /> {TRIAL_TEXT.askAssistant}
            </button>
            <button type="button" className="btn btn-xs" onClick={() => navigate(blueprintPath(blueprintId))} data-trial-back="">
              <PencilLine size={11} aria-hidden /> {TRIAL_TEXT.backToCards}
            </button>
          </div>
        </div>
      )}
      <div className={process ? 'grid grid-cols-1 gap-3 lg:grid-cols-[minmax(0,1fr)_340px]' : ''}>
        <div className="min-w-0 space-y-3">
          {model && model.pages.length > 1 && (
            <Tabs label={TRIAL_TEXT.title} tabs={model.pages.map((p) => ({ key: p.id, label: p.title || p.id }))}
                  active={model.page.id} onChange={goPage} />
          )}
          {running && (
            <p className="flex items-center gap-1.5 text-2xs text-dim" role="status" data-trial-running="">
              <Spinner size={11} /> {TRIAL_TEXT.running}
            </p>
          )}
          {error && !model ? <ErrorState error={error} onRetry={reload} compact />
            : !model ? <Skeleton rows={6} />
            : <ViewRenderer model={model} appId={bp.app_id ?? ''} preview onNavigate={goPage} onReload={reload} />}
        </div>
        {process && (
          <aside className="min-w-0 self-start rounded-lg border bg-panel" data-trial-tasks={tasks?.items.length ?? 0}
                 aria-labelledby="trial-tasks">
            <h2 id="trial-tasks" className="border-b px-3 py-2 text-xs font-semibold">{TRIAL_TEXT.tasksTitle}</h2>
            <p className="border-b px-3 py-1.5 text-2xs leading-relaxed text-faint">{TRIAL_TEXT.tasksHint}</p>
            {!tasks?.items.length
              ? <p className="px-3 py-3 text-2xs text-faint">{TRIAL_TEXT.tasksEmpty}</p>
              : tasks.items.map((t) => (
                <TaskCard key={t.id} task={t} compact trial showOwner={false}
                          onResolved={() => { reloadTasks(); reload() }} />
              ))}
            <p className="border-t px-3 py-1.5 text-2xs leading-relaxed text-faint" data-trial-write-note="">{TRIAL_TEXT.writeNote}</p>
          </aside>
        )}
      </div>
    </div>
  )
}

/** 顶部横幅：数据是真的、不是正式结果；这次的输入；换一期再试 */
function TrialBanner({ blueprintId, runId, model, onRetried }: {
  blueprintId: string; runId: string; model: ViewModel | null; onRetried: (runId: string) => void
}) {
  const entry = useBlueprint((s) => s.byId[blueprintId])
  const last = entry?.lastTrial?.run_id === runId ? entry.lastTrial : null
  // 输入表单：服务端随试运行给的，没给就用按卡片生成的应用骨架里的入口表单
  const form = last?.form ?? entryForm(entry?.instantiated?.app_spec)
  const [open, setOpen] = useState(false)
  const [values, setValues] = useState<Record<string, unknown>>({})
  const [errors, setErrors] = useState<FormError[]>([])
  const [busy, setBusy] = useState(false)
  const stampText = Object.values(model?.sources ?? {}).map((s) => s.stamp?.as_of_text).find(Boolean)
  const inputs = last?.inputs && Object.keys(last.inputs).length
    ? Object.entries(last.inputs).map(([k, v]) => `${form?.properties?.[k]?.title ?? k}：${typeof v === 'string' ? v : JSON.stringify(v)}`).join('；')
    : stampText || TRIAL_TEXT.inputsUnknown

  const retry = async () => {
    if (form) {
      const local = validateForm(form, values)
      if (local.length) { setErrors(local); return }
    }
    setBusy(true)
    try {
      const id = await useBlueprint.getState().trial(blueprintId, form ? cleanValues(form, values) : null)
      setOpen(false)
      onRetried(id)
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="rounded-lg border px-3 py-2.5 text-xs leading-relaxed" role="status" data-trial-banner=""
         style={{ borderColor: 'color-mix(in srgb, var(--st-waiting) 45%, var(--border))', background: 'color-mix(in srgb, var(--st-waiting) 8%, transparent)' }}>
      <div className="flex flex-wrap items-center gap-2">
        <FlaskConical size={14} aria-hidden style={{ color: 'var(--st-waiting)' }} />
        <span className="font-semibold text-fg" data-trial-banner-title="">{TRIAL_TEXT.banner}</span>
        <span className="flex-1" />
        <button type="button" className="btn btn-xs" aria-expanded={open} disabled={busy}
                onClick={() => { setOpen((o) => !o); if (form) setValues(withDefaults(form, last?.inputs ?? {})) }} data-trial-retry="">
          <RotateCw size={11} aria-hidden /> {TRIAL_TEXT.retry}
        </button>
      </div>
      <p className="mt-1 text-dim">{TRIAL_TEXT.bannerBody}</p>
      <p className="mt-1 text-dim" data-trial-inputs="">
        <span className="text-faint">{TRIAL_TEXT.inputs}：</span>{inputs}
      </p>
      {open && (
        <div className="mt-2 rounded border bg-bg p-2.5" data-trial-retry-form="">
          <div className="mb-1.5 text-2xs font-medium text-dim">{TRIAL_TEXT.retryTitle}</div>
          {form && (
            <SchemaForm schema={form} value={values} errors={errors} idPrefix={`trial-${blueprintId}`} onSubmit={() => void retry()}
                        fileContext={{ blueprint_id: blueprintId }}
                        onChange={(v) => { setValues(v); if (errors.length) setErrors([]) }} />
          )}
          <div className="mt-2 flex justify-end">
            <button type="button" className="btn btn-xs btn-primary" disabled={busy} onClick={() => void retry()} data-trial-retry-go="">
              {busy ? <Spinner size={10} /> : <FlaskConical size={11} aria-hidden />} {busy ? TRIAL_TEXT.retrying : TRIAL_TEXT.retryGo}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
