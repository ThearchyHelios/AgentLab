/**
 * 应用首页（/apps）：三个页签——待办（派给我的）、应用（我能用的）、我发起的（各应用里我发起的实例）。页签记在地址的
 * tab 上（?tab=inbox|apps|mine），徽标和命令面板可以直达。
 *
 * 关登录时在最上面挂一行说明：所有人都能使用全部功能，签批人是自报的署名（P1）。待办箱跟着外壳的心跳走：派给我的待办数
 * 变了就重取一次列表，不另起轮询。
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { ArrowRight, Hourglass, Inbox, LayoutGrid, PencilLine, TriangleAlert } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../../api/client'
import { EmptyState, Notice, PageHeader, Skeleton, StatusPill, TabPanel, Tabs, useTicker } from '../../components/ui'
import { Due } from '../../run/TaskCard'
import { useCatalog } from '../../store/catalog'
import { useAtLeast, useLoginOn } from '../../store/auth'
import { formatDateTime, formatRelative, formatSpan, parseServerTime } from '../../lib/format'
import { runName } from '../../lib/terms'
import { APP_HOME_TEXT, APP_TEXT, TASK_KIND_LABEL, TASK_STATUS_LABEL, TASK_TEXT } from '../../lib/termsApp'
import type { AppSummary, InstanceRow, Task } from '../../typesApp'
import { LoadError, appPath, useLoad } from './shared'

const TABS = ['inbox', 'apps', 'mine'] as const
type Tab = (typeof TABS)[number]
const PREFIX = 'apps-home'

export function AppsHome() {
  const [search, setSearch] = useSearchParams()
  const tab: Tab = (TABS as readonly string[]).includes(search.get('tab') ?? '') ? search.get('tab') as Tab : 'inbox'
  const loginOn = useLoginOn()
  const inboxCount = useCatalog((s) => s.taskCounts?.mine ?? 0)
  return (
    <div className="flex h-full flex-col" data-apps-home={tab}>
      <PageHeader icon={<LayoutGrid size={14} />} title={APP_TEXT.title} subtitle={APP_TEXT.hint} />
      <Tabs label={APP_TEXT.title} idPrefix={PREFIX} active={tab}
            tabs={TABS.map((key) => ({ key, label: APP_TEXT.tabs[key], badge: key === 'inbox' ? inboxCount : undefined }))}
            onChange={(key) => setSearch((prev) => { const next = new URLSearchParams(prev); next.set('tab', key); return next }, { replace: true })} />
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-5xl space-y-3 p-4">
          {!loginOn && (
            <Notice tone="info" attr={{ 'data-auth-off': '' }}>
              {APP_TEXT.authOff}{' '}
              <Link to="/settings/prefs" className="underline underline-offset-2 hover:text-fg">{APP_TEXT.authOffLink}</Link>
            </Notice>
          )}
          <TabPanel idPrefix={PREFIX} tabKey={tab}>
            {tab === 'inbox' ? <InboxTab /> : tab === 'apps' ? <AppsTab /> : <MineTab />}
          </TabPanel>
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------- 待办

function InboxTab() {
  const [done, setDone] = useState(false)
  const { data, error, reload } = useLoad((signal) => api.tasks.list({ assigned: 'me', status: done ? 'resolved' : 'pending', limit: 100 }, { signal }), [done])
  // 外壳的心跳拿到的待办数变了（来了新的、别人处理了）：重取一次
  const beat = useCatalog((s) => s.taskCounts?.mine)
  const seen = useRef(beat)
  useEffect(() => {
    if (beat == null || beat === seen.current) return
    seen.current = beat
    reload()
  }, [beat, reload])
  const groups = useMemo(() => {
    const byApp = new Map<string, { title: string; items: Task[] }>()
    for (const t of data?.items ?? []) {
      const key = t.app_id ?? ''
      const title = t.app_id ? APP_HOME_TEXT.inApp(t.app_name || t.app_id) : APP_HOME_TEXT.outsideApp
      if (!byApp.has(key)) byApp.set(key, { title, items: [] })
      byApp.get(key)!.items.push(t)
    }
    // 应用里的在前，不属于应用的（画布、定时任务的审批）垫后
    return [...byApp.entries()].sort(([a], [b]) => Number(!a) - Number(!b)).map(([key, g]) => ({ key, ...g }))
  }, [data])
  return (
    <div className="space-y-3" data-inbox="">
      <label className="flex items-center gap-1.5 text-2xs text-dim">
        <input type="checkbox" checked={done} onChange={(e) => setDone(e.target.checked)} /> {APP_HOME_TEXT.showDone}
      </label>
      {error && !data ? <LoadError error={error} onRetry={reload} />
        : !data ? <Skeleton rows={4} />
        : !data.items.length
          ? <EmptyState icon={<Inbox size={28} />} title={done ? APP_HOME_TEXT.doneEmpty : APP_HOME_TEXT.inboxEmpty}
                        body={done ? undefined : APP_HOME_TEXT.inboxEmptyBody} />
          : groups.map((g) => (
            <section key={g.key} className="overflow-hidden rounded-lg border bg-panel" data-inbox-group={g.key || 'other'}>
              <h2 className="border-b bg-elev px-3 py-1.5 text-2xs font-medium text-dim">{g.title}</h2>
              <ul>{g.items.map((t) => <InboxRow key={t.id} task={t} />)}</ul>
            </section>
          ))}
    </div>
  )
}

function InboxRow({ task }: { task: Task }) {
  const now = useTicker(60_000)
  const created = parseServerTime(task.created_at)
  const age = created ? now - created.getTime() : 0
  const voters = task.assignments.filter((a) => a.status !== 'withdrawn')
  const agreed = voters.filter((a) => a.status === 'responded' && a.decision !== 'reject').length
  const pending = task.status === 'pending'
  const where = task.node_label
  return (
    <li>
      <Link to={`/apps/inbox/${encodeURIComponent(task.id)}`} data-inbox-task={task.id} data-task-kind={task.kind}
            className="flex items-center gap-2 border-b px-3 py-2 text-xs last:border-0 hover:bg-hover">
        <span className="chip shrink-0">{TASK_KIND_LABEL[task.kind] ?? task.kind}</span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-fg">{task.title || TASK_TEXT.untitled}</span>
          <span className="block truncate text-2xs text-faint">
            {[!task.app_id && task.workflow_name ? runName(task) : null, where && TASK_TEXT.node(where)].filter(Boolean).join(' · ')}
          </span>
        </span>
        {task.policy.kind !== 'any' && voters.length > 1 && (
          <span className="chip shrink-0 tnum" data-inbox-signoffs="">{TASK_TEXT.signoffCount(agreed, voters.length)}</span>
        )}
        {task.escalation_level > 0 && (
          <span className="chip shrink-0" style={{ color: 'var(--st-waiting)' }} title={TASK_TEXT.escalatedHint}>
            <TriangleAlert size={10} aria-hidden /> {TASK_TEXT.escalated(task.escalation_level)}
          </span>
        )}
        {pending ? <Due dueAt={task.due_at} now={now} /> : <span className="chip shrink-0">{TASK_STATUS_LABEL[task.status]}</span>}
        {pending && age >= 60_000 && (
          <span className="hidden shrink-0 text-2xs text-faint sm:inline" title={formatDateTime(task.created_at)}>
            {APP_HOME_TEXT.waited(formatSpan(age, { coarse: true }))}
          </span>
        )}
        <ArrowRight size={12} aria-hidden className="shrink-0 text-faint" />
      </Link>
    </li>
  )
}

// ---------------------------------------------------------------- 应用

function AppsTab() {
  const builder = useAtLeast('builder')
  const { data, error, reload } = useLoad((signal) => api.apps.list({ signal }), [])
  if (error && !data) return <LoadError error={error} onRetry={reload} />
  if (!data) return <Skeleton rows={4} />
  if (!data.items.length) {
    return <EmptyState icon={<LayoutGrid size={28} />} title={APP_TEXT.noApps} body={builder ? APP_TEXT.noAppsBody : APP_TEXT.noAppsUser} />
  }
  return (
    <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3" data-app-list="">
      {data.items.map((a) => <AppCard key={a.id} app={a} canEdit={builder && a.my_role === 'manage'} />)}
    </ul>
  )
}

function AppCard({ app, canEdit }: { app: AppSummary; canEdit: boolean }) {
  return (
    <li className={clsx('relative flex flex-col rounded-lg border bg-panel p-3 hover:border-[var(--border-strong)]',
      app.status === 'archived' && 'opacity-60')} data-app={app.id}>
      <div className="flex items-start gap-2">
        <span aria-hidden className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md border bg-elev text-dim">
          <LayoutGrid size={14} />
        </span>
        <div className="min-w-0 flex-1">
          {/* 整张卡可点：链接的点击区盖满卡片，编辑按钮浮在上面 */}
          <Link to={appPath(app.id)} className="block truncate text-sm font-semibold text-fg after:absolute after:inset-0 after:content-['']">
            {app.name}
          </Link>
          {app.description && <p className="mt-0.5 line-clamp-2 text-2xs leading-relaxed text-dim">{app.description}</p>}
        </div>
      </div>
      <div className="mt-2.5 flex flex-wrap items-center gap-1.5 text-2xs">
        <span className="chip">{app.published_version ? APP_TEXT.publishedAt(app.published_version) : APP_TEXT.unpublished}</span>
        {app.level && <span className="chip">{app.level === 'governed' ? APP_TEXT.levelGoverned : APP_TEXT.levelPublished}</span>}
        {app.status === 'archived' && <span className="chip">{APP_TEXT.archived}</span>}
        {app.needs_upgrade && (
          <span className="chip" style={{ color: 'var(--st-waiting)' }} title={APP_TEXT.needsUpgradeHint} data-app-needs-upgrade="">
            <TriangleAlert size={10} aria-hidden /> {APP_TEXT.needsUpgrade}
          </span>
        )}
        {app.pending_tasks > 0 && (
          <span className="chip" style={{ color: 'var(--st-waiting)' }}>
            <Hourglass size={10} aria-hidden /> {APP_TEXT.pendingTasks(app.pending_tasks)}
          </span>
        )}
        <span className="flex-1" />
        {canEdit && (
          <Link to={`${appPath(app.id)}/edit`} className="relative z-10 inline-flex items-center gap-1 text-dim hover:text-fg">
            <PencilLine size={11} aria-hidden /> {APP_TEXT.edit}
          </Link>
        )}
      </div>
    </li>
  )
}

// ---------------------------------------------------------------- 我发起的

/** 各应用里我发起的实例，合在一起按时间排。没有跨应用的接口，按应用各取一次（应用数不多） */
function MineTab() {
  const { data, error, reload } = useLoad(async (signal) => {
    const { items: apps } = await api.apps.list({ signal })
    const lists = await Promise.all(apps.filter((a) => a.published_version).map((a) =>
      api.apps.instances(a.id, { who: 'mine', limit: 20 }).then(
        (r) => r.items.map((row) => ({ ...row, app: a })),
        () => [] as (InstanceRow & { app: AppSummary })[])))
    return lists.flat().sort((x, y) => (parseServerTime(y.created_at)?.getTime() ?? 0) - (parseServerTime(x.created_at)?.getTime() ?? 0))
  }, [])
  if (error && !data) return <LoadError error={error} onRetry={reload} />
  if (!data) return <Skeleton rows={4} />
  if (!data.length) return <EmptyState icon={<LayoutGrid size={28} />} title={APP_HOME_TEXT.mineEmpty} />
  return (
    <ul className="overflow-hidden rounded-lg border bg-panel" data-mine="">
      {data.map((r) => (
        <li key={r.run_id}>
          <Link to={`${appPath(r.app.id)}/i/${encodeURIComponent(r.run_id)}`} data-instance={r.run_id}
                className="flex items-center gap-2 border-b px-3 py-2 text-xs last:border-0 hover:bg-hover">
            <span className="min-w-0 flex-1">
              <span className="block truncate text-fg">{r.title || r.run_id}</span>
              <span className="block truncate text-2xs text-faint">{r.app.name}</span>
            </span>
            {r.pending_tasks > 0 && (
              <span className="chip shrink-0" style={{ color: 'var(--st-waiting)' }}>
                <Hourglass size={10} aria-hidden /> {APP_TEXT.pendingTasks(r.pending_tasks)}
              </span>
            )}
            <StatusPill status={r.status} short />
            <span className="shrink-0 text-2xs text-faint" title={formatDateTime(r.created_at)}>{formatRelative(r.created_at)}</span>
          </Link>
        </li>
      ))}
    </ul>
  )
}
