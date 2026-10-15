/**
 * 蓝图页（/blueprints/:id、/blueprints/:id/trial，docs/design/normal-user-apps.md §4.2–§4.4）。
 *
 * - 卡片（默认）：左边和助手对话（BlueprintChat），右边卡片和生成的工作流（BlueprintCards）；
 * - 试运行（trial）：应用的样子对着这次试运行解析（TrialSection）。
 *
 * 底部一条：「用真数试一次」——还有缺的项、待填的关节时置灰，写清缺什么；试过之后才有终点：共享蓝图给「发布」（发布摘要，
 * PublishSummary），个人蓝图给「存成我的应用」（POST /api/blueprints/{id}/save-personal，使用者的终点）。发布单和个人应用
 * 都在服务端校验，这里不替服务端拦，只照它说的摆。
 */
import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { Check, ExternalLink, FlaskConical, PencilLine, Rocket, Save } from 'lucide-react'
import { ApiError, api } from '../../api/client'
import { ErrorState, PageHeader, Skeleton, Spinner, Tabs, promptDialog, toast } from '../../components/ui'
import { usePageTitle } from '../../lib/notify'
import { BLUEPRINT_TEXT, PLAN_TEXT } from '../../lib/terms'
import { chatBusy, useBlueprint, type BpEntry } from '../../store/blueprint'
import type { TemplateInfo } from '../../typesBlueprint'
import { BackLink, appPath } from '../apps/shared'
import { BlueprintCards } from './BlueprintCards'
import { BlueprintChat } from './BlueprintChat'
import { PublishSummary } from './PublishSummary'
import { TrialSection } from './TrialSection'
import { blueprintPath, trialed } from './shared'

const SECTIONS = ['cards', 'trial'] as const
/** 只挡发布、不挡试运行的问题：待确认的项、岗位没人（试运行时人工步骤派给试运行的人自己，服务端 blueprints/ops） */
const PUBLISH_ONLY = new Set(['bp.unconfirmed', 'bp.people_unbound'])
type Section = (typeof SECTIONS)[number]

/** 模板说明整站一份，进一次蓝图页取一次就够 */
let templatesCache: Promise<TemplateInfo[]> | null = null
const loadTemplates = () => {
  templatesCache ??= api.templates.list().then((r) => r.items).catch((e) => { templatesCache = null; throw e })
  return templatesCache
}

export function BlueprintPage() {
  const { blueprintId = '', section: raw } = useParams()
  const section: Section = (SECTIONS as readonly string[]).includes(raw ?? '') ? raw as Section : 'cards'
  const navigate = useNavigate()
  const entry = useBlueprint((s) => s.byId[blueprintId])
  const bp = entry?.blueprint
  const [templates, setTemplates] = useState<TemplateInfo[] | null>(null)
  const [publishing, setPublishing] = useState(false)
  usePageTitle(bp ? `${BLUEPRINT_TEXT.title} · ${bp.name || BLUEPRINT_TEXT.untitled}` : BLUEPRINT_TEXT.title)

  useEffect(() => { void useBlueprint.getState().load(blueprintId) }, [blueprintId])
  useEffect(() => {
    let alive = true
    loadTemplates().then((t) => { if (alive) setTemplates(t) }, () => { if (alive) setTemplates([]) })
    return () => { alive = false }
  }, [])

  const rename = async () => {
    if (!bp) return
    const name = await promptDialog({ title: BLUEPRINT_TEXT.rename, label: BLUEPRINT_TEXT.renamePrompt, initial: bp.name,
      confirmLabel: BLUEPRINT_TEXT.rename })
    if (!name || name === bp.name) return
    try {
      await useBlueprint.getState().edit(blueprintId, { name })
      toast.ok(BLUEPRINT_TEXT.renamed)
    } catch (e) {
      toast.error(e)
    }
  }

  const header = (
    <PageHeader
      icon={<PencilLine size={14} />}
      title={bp ? bp.name || BLUEPRINT_TEXT.untitled : BLUEPRINT_TEXT.title}
      subtitle={bp ? `${BLUEPRINT_TEXT.title} · ${BLUEPRINT_TEXT.scope[bp.scope]} · ${BLUEPRINT_TEXT.status[bp.status] ?? bp.status}` : undefined}
      actions={bp && (
        <div className="flex items-center gap-1.5">
          <button type="button" className="btn btn-sm btn-ghost" onClick={() => void rename()}>{BLUEPRINT_TEXT.rename}</button>
          {bp.app_id && (
            <Link to={appPath(bp.app_id)} className="btn btn-sm" data-blueprint-open-app="">
              <ExternalLink size={12} aria-hidden /> {BLUEPRINT_TEXT.openApp}
            </Link>
          )}
        </div>
      )}
    />
  )

  if (!bp) {
    return (
      <div className="flex h-full flex-col" data-blueprint-page="">
        {header}
        <div className="mx-auto w-full max-w-5xl p-4">
          {entry?.loadState === 'error'
            ? <BlueprintLoadError error={entry.loadError} onRetry={() => void useBlueprint.getState().load(blueprintId, { force: true })} />
            : <Skeleton rows={6} />}
        </div>
      </div>
    )
  }

  const goSection = (key: string) => navigate(key === 'trial' ? blueprintPath(blueprintId, 'trial') : blueprintPath(blueprintId))
  return (
    <div className="flex h-full flex-col" data-blueprint-page={bp.id} data-blueprint-scope={bp.scope} data-blueprint-status={bp.status}>
      {header}
      <Tabs label={BLUEPRINT_TEXT.title} active={section} onChange={goSection}
            tabs={SECTIONS.map((key) => ({ key, label: BLUEPRINT_TEXT.sections[key] }))} />
      {section === 'trial' ? (
        <div className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto max-w-6xl space-y-3 p-4">
            <BackLink to="/apps?tab=apps" label={BLUEPRINT_TEXT.back} />
            <TrialSection blueprintId={blueprintId} />
          </div>
        </div>
      ) : (
        <div className="grid min-h-0 flex-1 grid-cols-1 overflow-y-auto lg:grid-cols-[minmax(300px,400px)_minmax(0,1fr)] lg:overflow-hidden">
          <div className="min-h-[420px] border-b lg:min-h-0 lg:border-b-0 lg:border-r">
            <BlueprintChat blueprintId={blueprintId} />
          </div>
          <div className="min-h-0 lg:overflow-y-auto">
            <div className="space-y-3 p-4">
              {bp.copied_from && <p className="text-2xs text-dim" data-blueprint-copy="">{BLUEPRINT_TEXT.copiedFrom}</p>}
              <BlueprintCards blueprintId={blueprintId} templates={templates} />
            </div>
          </div>
        </div>
      )}
      <BottomBar entry={entry!} onPublish={() => setPublishing(true)} />
      {bp.scope === 'shared' && (
        <PublishSummary blueprintId={blueprintId} open={publishing} onClose={() => setPublishing(false)}
                        onFix={(lines) => {
                          useBlueprint.getState().send(blueprintId, PLAN_TEXT.fixText(lines))
                          navigate(blueprintPath(blueprintId))
                        }} />
      )}
    </div>
  )
}

function BlueprintLoadError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  if (error instanceof ApiError && error.status === 404) return <ErrorState error={new ApiError(404, BLUEPRINT_TEXT.notFound)} />
  if (error instanceof ApiError && error.status === 403) {
    return <ErrorState error={new ApiError(403, error.message || BLUEPRINT_TEXT.noAccess, { code: error.code })} />
  }
  return <ErrorState error={error} onRetry={onRetry} />
}

/**
 * 底部一条：试运行、发布、存成我的应用。缺什么按服务端的蓝图检查说（issues 里 error 级的：缺必填项、没对到表、关节没填上），
 * 界面不自己判哪些项必填（P7）；老服务端没给检查结果时不拦，试运行被拒时照实说服务端的原因
 */
function BottomBar({ entry, onPublish }: { entry: BpEntry; onPublish: () => void }) {
  const bp = entry.blueprint!
  const navigate = useNavigate()
  const [busy, setBusy] = useState<'trial' | 'save' | null>(null)
  const errors = (bp.issues ?? []).filter((i) => i.level === 'error')
  const blockers = errors.filter((i) => !PUBLISH_ONLY.has(i.code)).map((i) => i.plain?.title ?? i.message)
  // 试过之后服务端按发布查：待确认的项、岗位没人只挡发布，发布摘要里逐条列，这里只说还有几项
  const toConfirm = errors.filter((i) => PUBLISH_ONLY.has(i.code)).length
  const working = chatBusy(entry) || entry.fill?.phase === 'running'
  const tried = trialed(bp)
  const personal = bp.scope === 'personal'

  const trial = async () => {
    setBusy('trial')
    try {
      const runId = await useBlueprint.getState().trial(bp.id)
      navigate(blueprintPath(bp.id, 'trial', { run: runId }))
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(null)
    }
  }
  const savePersonal = async () => {
    setBusy('save')
    try {
      const res = await api.blueprints.savePersonal(bp.id)
      useBlueprint.getState().adopt(res.blueprint)
      toast.ok(BLUEPRINT_TEXT.savedPersonal)
      navigate(appPath(res.app_id))
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="shrink-0 border-t bg-panel px-4 py-2" data-blueprint-bar="">
      <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-2">
        {blockers.length > 0 ? (
          <div className="min-w-0 flex-1 text-2xs leading-relaxed" data-blueprint-missing={blockers.length}>
            <span className="font-medium" style={{ color: 'var(--st-failed)' }}>{BLUEPRINT_TEXT.missingTitle}</span>
            <ul className="mt-0.5 list-disc pl-4 text-dim">
              {blockers.slice(0, 4).map((b) => <li key={b}>{b}</li>)}
              {blockers.length > 4 && <li>…</li>}
            </ul>
          </div>
        ) : (
          <p className="min-w-0 flex-1 text-2xs leading-relaxed text-faint" data-blueprint-to-confirm={toConfirm}>
            {!tried ? BLUEPRINT_TEXT.trialHint : personal ? '' : toConfirm ? BLUEPRINT_TEXT.toConfirm(toConfirm) : BLUEPRINT_TEXT.publishHint}
          </p>
        )}
        <button type="button" className={tried ? 'btn btn-sm' : 'btn btn-sm btn-primary'} disabled={!!blockers.length || working || !!busy}
                onClick={() => void trial()} data-blueprint-trial="" title={blockers.length ? blockers.join('；') : BLUEPRINT_TEXT.trialHint}>
          {busy === 'trial' ? <Spinner size={11} /> : <FlaskConical size={12} aria-hidden />}{' '}
          {busy === 'trial' ? BLUEPRINT_TEXT.trialBusy : BLUEPRINT_TEXT.trial}
        </button>
        {/* 试过之后才有终点：共享蓝图发布，个人蓝图存成我的应用 */}
        {tried && personal && (
          <button type="button" className="btn btn-sm btn-primary" disabled={working || !!busy} onClick={() => void savePersonal()}
                  data-blueprint-save-personal="">
            {busy === 'save' ? <Spinner size={11} /> : <Save size={12} aria-hidden />}{' '}
            {busy === 'save' ? BLUEPRINT_TEXT.savingPersonal : bp.app_id ? BLUEPRINT_TEXT.savePersonalUpdate : BLUEPRINT_TEXT.savePersonal}
          </button>
        )}
        {tried && !personal && (
          <button type="button" className="btn btn-sm btn-primary" disabled={working || !!busy} title={BLUEPRINT_TEXT.publishHint}
                  onClick={onPublish} data-blueprint-publish="">
            {bp.status === 'published' ? <Check size={12} aria-hidden /> : <Rocket size={12} aria-hidden />} {BLUEPRINT_TEXT.publish}
          </button>
        )}
      </div>
    </div>
  )
}
