/**
 * 新建应用（/apps/create，docs/design/normal-user-apps.md N1、§4.1）：三个起点，都落到同一个蓝图。
 *
 * - 说说你要什么：大输入框加例句。建一个对话起点的蓝图，这句话作为第一轮交给蓝图助手，进蓝图页时已经在整理；
 * - 从场景模板开始：模板卡写适用场景、会产出什么、要准备什么，选一个就建模板起点的蓝图，卡片按模板预置；
 * - 从已有的开始：最近的问数据回答（运行起点，服务端按 from-run 提取），加上已发布的工作流（版本起点，只给搭建者）。
 *
 * 身份决定范围（shared.createScope）：搭建者建共享蓝图，终点是发布单；使用者在管理员开了个人应用时建个人蓝图，终点是
 * 个人应用，只能用模板里个人应用能用的那部分。都不是的照实说没开放，不给入口。
 *
 * 还没做完的蓝图（起草中、试运行过）列在最上面「接着做」：蓝图存在服务端，离开了也能回来接着改。
 */
import { useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ArrowRight, LayoutGrid, MessageSquare, Plus, Shapes, Workflow as WorkflowIcon } from 'lucide-react'
import { ApiError, api } from '../../api/client'
import { EmptyState, ErrorState, Notice, PageHeader, Skeleton, Spinner, isComposing, toast } from '../../components/ui'
import { formatRelative } from '../../lib/format'
import { matchShortcut } from '../../lib/keys'
import { usePageTitle } from '../../lib/notify'
import { APP_TEXT, BLUEPRINT_TEXT, CARD_LABEL, NEW_APP_TEXT, TEMPLATE_PRODUCES } from '../../lib/terms'
import { useAtLeast, useAuth } from '../../store/auth'
import { useBlueprint } from '../../store/blueprint'
import { useCatalog } from '../../store/catalog'
import type { BlueprintScope, BlueprintSourceKind, TemplateInfo } from '../../typesBlueprint'
import { BackLink, useLoad } from '../apps/shared'
import { blueprintPath, useCreateScope } from './shared'

/** 起点：建蓝图的请求体里 source 那一段 */
type Source = { kind: BlueprintSourceKind; id?: string; version?: number }

/** 模板说明：服务端给了会产出什么、要准备什么时用它的，没给时按模板 id 说（termsBlueprint.TEMPLATE_PRODUCES） */
type TemplateCardInfo = TemplateInfo & { produces?: string | null; prepare?: string[] | null }

export function NewAppStart() {
  const scope = useCreateScope()
  const ready = useAuth((s) => s.ready)
  const navigate = useNavigate()
  const [busy, setBusy] = useState<string | null>(null)
  usePageTitle(NEW_APP_TEXT.title)

  /** 建蓝图、交给 store，再进蓝图页；first 是「说说你要什么」那句话，作为第一轮发给助手 */
  const start = async (key: string, source: Source, opts?: { template?: string; first?: string }) => {
    if (!scope || busy) return
    setBusy(key)
    try {
      const bp = await api.blueprints.create({ source, scope, ...(opts?.template ? { template: opts.template } : {}) })
      const store = useBlueprint.getState()
      store.adopt(bp)
      if (opts?.first) store.send(bp.id, opts.first)
      navigate(blueprintPath(bp.id))
    } catch (e) {
      toast.error(e)
      setBusy(null)
    }
  }

  return (
    <div className="flex h-full flex-col" data-new-app={scope ?? 'none'}>
      <PageHeader icon={<Plus size={14} />} title={NEW_APP_TEXT.title} subtitle={NEW_APP_TEXT.subtitle} />
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-5xl space-y-4 p-4">
          <BackLink to="/apps?tab=apps" label={APP_TEXT.backToApps} />
          {!ready ? <Skeleton rows={4} />
            : !scope ? (
              <EmptyState icon={<LayoutGrid size={28} />} title={NEW_APP_TEXT.personalOff} body={NEW_APP_TEXT.personalOffBody}
                          action={<Link to="/chat" className="btn btn-sm">{NEW_APP_TEXT.goChat}</Link>} />
            ) : (
              <>
                {scope === 'personal' && (
                  <Notice tone="info" attr={{ 'data-new-app-personal': '' }}>{NEW_APP_TEXT.personalNote}</Notice>
                )}
                <Unfinished />
                <TalkStart busy={busy} onStart={(text) => void start('talk', { kind: 'conversation' }, { first: text })} />
                <TemplateStart scope={scope} busy={busy}
                               onPick={(t) => void start(`template:${t.id}`, { kind: 'template', id: t.id, version: t.version },
                                 { template: t.id })} />
                <ExistingStart busy={busy} onPick={(key, source) => void start(key, source)} />
              </>
            )}
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------- 接着做

/** 还没做完的蓝图（起草中、试运行过），按最近改动排；没有就不摆。取不到不打扰（三个起点照样能用） */
function Unfinished() {
  const { data } = useLoad(() => api.blueprints.list(), [])
  const items = (data?.items ?? []).filter((b) => b.status === 'drafting' || b.status === 'trialed').slice(0, 5)
  if (!items.length) return null
  return (
    <section className="overflow-hidden rounded-lg border bg-panel" data-new-app-unfinished={items.length} aria-labelledby="new-app-unfinished">
      <h2 id="new-app-unfinished" className="border-b bg-elev px-3 py-1.5 text-2xs font-medium text-dim">{NEW_APP_TEXT.unfinished}</h2>
      <ul>
        {items.map((b) => (
          <li key={b.id}>
            <Link to={blueprintPath(b.id)} className="flex items-center gap-2 border-b px-3 py-2 text-xs last:border-0 hover:bg-hover"
                  data-new-app-blueprint={b.id}>
              <span className="min-w-0 flex-1 truncate text-fg">{b.name || BLUEPRINT_TEXT.untitled}</span>
              <span className="chip shrink-0">{BLUEPRINT_TEXT.status[b.status] ?? b.status}</span>
              <span className="shrink-0 text-2xs text-faint">{formatRelative(b.updated_at)}</span>
              <ArrowRight size={12} aria-hidden className="shrink-0 text-faint" />
            </Link>
          </li>
        ))}
      </ul>
    </section>
  )
}

// ---------------------------------------------------------------- 说说你要什么

function TalkStart({ busy, onStart }: { busy: string | null; onStart: (text: string) => void }) {
  const [text, setText] = useState('')
  const go = () => { if (text.trim() && !busy) onStart(text.trim()) }
  return (
    <section className="rounded-lg border bg-panel p-4" data-new-app-start="talk" aria-labelledby="new-app-talk">
      <h2 id="new-app-talk" className="flex items-center gap-1.5 text-sm font-semibold">
        <MessageSquare size={14} aria-hidden className="text-dim" /> {NEW_APP_TEXT.talkTitle}
      </h2>
      <p className="mt-0.5 text-xs leading-relaxed text-dim">{NEW_APP_TEXT.talkHint}</p>
      <textarea className="field mt-2 min-h-[96px] text-sm" rows={4} value={text} disabled={!!busy}
                aria-label={NEW_APP_TEXT.talkLabel} placeholder={NEW_APP_TEXT.talkPlaceholder}
                onChange={(e) => setText(e.target.value)}
                onKeyDown={(e) => {
                  if (isComposing(e) || !matchShortcut(e, 'Mod+Enter')) return
                  e.preventDefault()
                  go()
                }} />
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        {NEW_APP_TEXT.talkExamples.map((ex) => (
          <button key={ex} type="button" className="chip max-w-full truncate text-left hover:text-fg" title={NEW_APP_TEXT.exampleHint}
                  disabled={!!busy} onClick={() => setText(ex)} data-new-app-example="">
            {ex}
          </button>
        ))}
        <span className="flex-1" />
        <button type="button" className="btn btn-primary btn-sm" disabled={!text.trim() || !!busy} onClick={go}
                data-new-app-talk-go="">
          {busy === 'talk' ? <Spinner size={11} /> : <ArrowRight size={12} aria-hidden />}
          {busy === 'talk' ? NEW_APP_TEXT.talkBusy : NEW_APP_TEXT.talkStart}
        </button>
      </div>
    </section>
  )
}

// ---------------------------------------------------------------- 从场景模板开始

function TemplateStart({ scope, busy, onPick }: {
  scope: BlueprintScope; busy: string | null; onPick: (t: TemplateInfo) => void
}) {
  const { data, error, reload } = useLoad(() => api.templates.list(), [])
  // 「自由形」是从已有的运行或工作流导入用的，不作为从零开始的场景
  const items = (data?.items ?? []).filter((t) => t.id !== 'freeform') as TemplateCardInfo[]
  return (
    <section className="space-y-2" data-new-app-start="template" aria-labelledby="new-app-template">
      <div>
        <h2 id="new-app-template" className="flex items-center gap-1.5 text-sm font-semibold">
          <Shapes size={14} aria-hidden className="text-dim" /> {NEW_APP_TEXT.templateTitle}
        </h2>
        <p className="mt-0.5 text-xs leading-relaxed text-dim">{NEW_APP_TEXT.templateHint}</p>
      </div>
      {error && !data ? <ErrorState error={error} onRetry={reload} compact />
        : !data ? <Skeleton rows={3} />
        : !items.length ? <p className="text-xs text-faint">{NEW_APP_TEXT.templatesEmpty}</p>
        : (
          <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {items.map((t) => <TemplateCard key={t.id} t={t} scope={scope} busy={busy} onPick={onPick} />)}
          </ul>
        )}
    </section>
  )
}

function TemplateCard({ t, scope, busy, onPick }: {
  t: TemplateCardInfo; scope: BlueprintScope; busy: string | null; onPick: (t: TemplateInfo) => void
}) {
  // 个人应用只能用模板里只读的那部分；整个模板都用不了的（要派人审批、写外部系统）照实说，不给选
  const blocked = scope === 'personal' && !t.personal
  const produces = t.produces || TEMPLATE_PRODUCES[t.id]
  const prepare = t.prepare?.length ? t.prepare
    : t.cards.filter((c) => c.card !== 'purpose' && c.card !== 'template')
      .map((c) => (c.note ? `${CARD_LABEL[c.card] ?? c.card}：${c.note}` : CARD_LABEL[c.card] ?? c.card))
  const key = `template:${t.id}`
  return (
    <li className="flex flex-col rounded-lg border bg-panel p-3" data-template={t.id} aria-disabled={blocked || undefined}>
      <div className="text-sm font-semibold text-fg">{t.label}</div>
      <dl className="mt-1.5 flex-1 space-y-1.5 text-2xs leading-relaxed">
        <div><dt className="text-faint">{NEW_APP_TEXT.templateWhen}</dt><dd className="text-dim" data-template-when="">{t.description}</dd></div>
        {produces && (
          <div><dt className="text-faint">{NEW_APP_TEXT.templateProduces}</dt><dd className="text-dim" data-template-produces="">{produces}</dd></div>
        )}
        {prepare.length > 0 && (
          <div>
            <dt className="text-faint">{NEW_APP_TEXT.templatePrepare}</dt>
            <dd className="text-dim" data-template-prepare="">
              <ul className="list-disc pl-4">{prepare.map((p) => <li key={p}>{p}</li>)}</ul>
            </dd>
          </div>
        )}
      </dl>
      {scope === 'personal' && (
        <p className="mt-1.5 text-2xs leading-relaxed" style={{ color: 'var(--st-waiting)' }} data-template-personal="">
          {blocked ? NEW_APP_TEXT.templatePersonalNo : NEW_APP_TEXT.templatePersonalPart(t.personal!)}
        </p>
      )}
      <button type="button" className="btn btn-sm mt-2 justify-center" disabled={blocked || !!busy} onClick={() => onPick(t)}
              data-template-use={t.id}>
        {busy === key ? <Spinner size={11} /> : <ArrowRight size={12} aria-hidden />}
        {busy === key ? NEW_APP_TEXT.creating : NEW_APP_TEXT.templateUse}
      </button>
    </li>
  )
}

// ---------------------------------------------------------------- 从已有的开始

function ExistingStart({ busy, onPick }: { busy: string | null; onPick: (key: string, source: Source) => void }) {
  const builder = useAtLeast('builder')
  // 最近的问数据回答：每个会话的最后一轮，回答完成、有运行的。使用者没开问数据时这一列取不到（403），照实说没有
  const { data, error, reload } = useLoad(() => api.conversations.list('chat'), [])
  const answers = useMemo(() => (data ?? [])
    .filter((c) => !c.archived && c.last_status === 'done' && !!c.last_run_id)
    .slice(0, 6), [data])
  const workflows = useCatalog((s) => s.workflows)
  const published = useMemo(() => workflows.filter((w) => w.published_version != null), [workflows])
  const forbidden = error instanceof ApiError && error.status === 403
  return (
    <section className="space-y-2" data-new-app-start="existing" aria-labelledby="new-app-existing">
      <div>
        <h2 id="new-app-existing" className="text-sm font-semibold">{NEW_APP_TEXT.existingTitle}</h2>
        <p className="mt-0.5 text-xs leading-relaxed text-dim">{NEW_APP_TEXT.existingHint}</p>
      </div>
      <div className={builder ? 'grid grid-cols-1 gap-3 lg:grid-cols-2' : ''}>
        <div className="overflow-hidden rounded-lg border bg-panel" data-new-app-answers="">
          <h3 className="border-b bg-elev px-3 py-1.5 text-2xs font-medium text-dim">{NEW_APP_TEXT.recentAnswers}</h3>
          {error && !data && !forbidden ? <ErrorState error={error} onRetry={reload} compact className="m-2" />
            : !data && !forbidden ? <Skeleton rows={3} className="p-3" />
            : !answers.length ? <p className="px-3 py-3 text-2xs leading-relaxed text-faint">{NEW_APP_TEXT.recentAnswersEmpty}</p>
            : (
              <ul>
                {answers.map((c) => {
                  const key = `run:${c.last_run_id}`
                  return (
                    <li key={c.id} className="flex items-center gap-2 border-b px-3 py-2 text-xs last:border-0" data-new-app-answer={c.last_run_id!}>
                      <MessageSquare size={12} aria-hidden className="shrink-0 text-faint" />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-fg">{c.last_question || c.title}</span>
                        <span className="block truncate text-2xs text-faint">{c.title} · {formatRelative(c.last_active_at)}</span>
                      </span>
                      <button type="button" className="btn btn-xs shrink-0" disabled={!!busy}
                              onClick={() => onPick(key, { kind: 'run', id: c.last_run_id! })}>
                        {busy === key ? <Spinner size={11} /> : <LayoutGrid size={11} aria-hidden />} {NEW_APP_TEXT.useThis}
                      </button>
                    </li>
                  )
                })}
              </ul>
            )}
        </div>
        {/* 已发布的工作流只给搭建者：使用者没有工作流 */}
        {builder && (
          <div className="overflow-hidden rounded-lg border bg-panel" data-new-app-workflows="">
            <h3 className="border-b bg-elev px-3 py-1.5 text-2xs font-medium text-dim">{NEW_APP_TEXT.publishedWorkflows}</h3>
            {!published.length ? <EmptyState title={NEW_APP_TEXT.publishedWorkflowsEmpty} source="workflows" className="py-6" />
              : (
                <ul>
                  {published.map((w) => {
                    const key = `workflow:${w.id}`
                    return (
                      <li key={w.id} className="flex items-center gap-2 border-b px-3 py-2 text-xs last:border-0" data-new-app-workflow={w.id}>
                        <WorkflowIcon size={12} aria-hidden className="shrink-0 text-faint" />
                        <span className="min-w-0 flex-1 truncate text-fg">{w.name}</span>
                        <span className="chip shrink-0">{NEW_APP_TEXT.publishedVersion(w.published_version!)}</span>
                        <button type="button" className="btn btn-xs shrink-0" disabled={!!busy}
                                onClick={() => onPick(key, { kind: 'workflow_version', id: w.id, version: w.published_version! })}>
                          {busy === key ? <Spinner size={11} /> : <LayoutGrid size={11} aria-hidden />} {NEW_APP_TEXT.useThis}
                        </button>
                      </li>
                    )
                  })}
                </ul>
              )}
          </div>
        )}
      </div>
    </section>
  )
}
