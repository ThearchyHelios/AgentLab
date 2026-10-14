/**
 * 流程组件：entry_button、instance_list、instance_header、instance_timeline、task_list、task_form、signoffs、
 * action_status（组件目录 v1 §3.5）。
 *
 * 数据优先用服务端解析好的（data），没给的列表才按 props 自己取（实例列表、待办列表）。预览（编辑器里）不发起、不提交：
 * 发起按钮不跳转，待办卡只读，实例不能点进去。
 */
import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ArrowRight, Hourglass, Play } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../../api/client'
import { ErrorState, Spinner, StatusPill, useTicker } from '../../components/ui'
import { Due, TaskCard, Signoffs } from '../../run/TaskCard'
import { formatDateTime, formatRelative } from '../../lib/format'
import {
  ACTION_ENV_LABEL, ACTION_STATE_LABEL, ACTION_TEXT, APP_TEXT, ENTRY_TEXT, INSTANCE_TEXT, TASK_KIND_LABEL, VIEW_TEXT,
} from '../../lib/termsApp'
import type { ActionRecordRow, ActionState, InstanceRow, Task } from '../../typesApp'
import { Stamp } from '../Stamp'
import { sourceOf, str, useView, type NodeProps } from '../context'

const appPath = (appId: string) => `/apps/${encodeURIComponent(appId)}`

export function EntryButton({ node }: NodeProps) {
  const { model, appId, preview } = useView()
  const entry = str(node.props.entry)
  const label = str(node.props.label) || str(node.data?.label) || entry
  const blocked = model.app.needs_upgrade || !!node.data?.disabled
  const reason = model.app.needs_upgrade ? ENTRY_TEXT.upgrade : str(node.data?.reason)
  const inner = <><Play size={12} aria-hidden /> {label}</>
  return (
    <div data-component="entry_button">
      {blocked || preview || !entry
        ? <button type="button" className="btn btn-primary" disabled={blocked} aria-disabled={preview || undefined}
                  title={preview ? VIEW_TEXT.previewNoRun : reason || undefined}>{inner}</button>
        : <Link to={`${appPath(appId)}/new/${encodeURIComponent(entry)}`} className="btn btn-primary">{inner}</Link>}
      {blocked && reason && <div className="mt-1 text-2xs leading-relaxed" style={{ color: 'var(--st-waiting)' }}>{reason}</div>}
    </div>
  )
}

/** 列表类组件的数据：服务端给了就用，没给按 fetcher 取；reloadKey 变了重取 */
function useList<T>(given: unknown, fetcher: (() => Promise<{ items: T[] }>) | null, reloadKey: unknown) {
  const [state, setState] = useState<{ items?: T[]; error?: unknown }>({})
  const [tick, setTick] = useState(0)
  useEffect(() => {
    if (Array.isArray(given) || !fetcher) return
    let alive = true
    setState((s) => ({ items: s.items }))
    fetcher().then((r) => { if (alive) setState({ items: r.items }) }, (error) => { if (alive) setState({ error }) })
    return () => { alive = false }
    // fetcher 每次渲染都是新的：按它的参数（reloadKey）和手动重取（tick）决定
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reloadKey, tick, Array.isArray(given)])
  const retry = useCallback(() => setTick((n) => n + 1), [])
  return { items: Array.isArray(given) ? (given as T[]) : state.items, error: Array.isArray(given) ? undefined : state.error, retry }
}

export function InstanceList({ node }: NodeProps) {
  const { appId, preview } = useView()
  const who = node.props.who === 'all' ? 'all' : 'mine'
  const entry = str(node.props.entry) || undefined
  const limit = Number(node.props.limit) > 0 ? Number(node.props.limit) : 20
  const { items, error, retry } = useList<InstanceRow>(node.data?.items,
    () => api.apps.instances(appId, { who, entry, limit }), `${appId}|${who}|${entry}|${limit}`)
  return (
    <div className="rounded-lg border bg-panel" data-component="instance_list">
      <div className="border-b px-3 py-2 text-xs font-medium">{VIEW_TEXT.instances[who]}</div>
      {error ? <ErrorState compact error={error} onRetry={retry} className="m-2" />
        : !items ? <div className="flex items-center gap-2 px-3 py-3 text-2xs text-faint"><Spinner size={11} /></div>
        : !items.length ? <div className="px-3 py-3 text-2xs text-faint">{VIEW_TEXT.instances.empty}</div>
        : (
          <ul>
            {items.map((r) => <InstanceItem key={r.run_id} row={r} appId={appId} preview={preview} />)}
          </ul>
        )}
    </div>
  )
}

export function InstanceItem({ row, appId, preview }: { row: InstanceRow; appId: string; preview: boolean }) {
  const body = (
    <>
      <span className="min-w-0 flex-1 truncate text-xs text-fg">{row.title || row.run_id}</span>
      {row.pending_tasks > 0 && (
        <span className="chip shrink-0" style={{ color: 'var(--st-waiting)' }}>
          <Hourglass size={10} aria-hidden /> {APP_TEXT.pendingTasks(row.pending_tasks)}
        </span>
      )}
      <StatusPill status={row.status} short />
      <span className="shrink-0 text-2xs text-faint" title={formatDateTime(row.created_at)}>{formatRelative(row.created_at)}</span>
    </>
  )
  const cls = 'flex items-center gap-2 border-b px-3 py-2 last:border-0'
  return (
    <li data-instance={row.run_id}>
      {preview ? <div className={cls}>{body}</div>
        : <Link to={`${appPath(appId)}/i/${encodeURIComponent(row.run_id)}`} className={clsx(cls, 'hover:bg-hover')}>{body}</Link>}
    </li>
  )
}

export function InstanceHeader({ node }: NodeProps) {
  const { model } = useView()
  const d = node.data ?? {}
  const stamp = d.stamp ?? sourceOf(model, node)?.stamp
  return (
    <header className="space-y-1.5" data-component="instance_header">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="min-w-0 truncate text-base font-semibold">{str(d.title) || model.page.title}</h2>
        {d.status && <StatusPill status={str(d.status)} />}
      </div>
      <div className="flex flex-wrap gap-x-2 text-2xs text-dim">
        {d.started_by && <span>{INSTANCE_TEXT.startedBy(str(d.started_by))}</span>}
        {d.created_at && <span title={formatDateTime(d.created_at)}>{INSTANCE_TEXT.startedAt(formatDateTime(d.created_at))}</span>}
        {d.finished_at && <span>{INSTANCE_TEXT.finishedAt(formatDateTime(d.finished_at))}</span>}
      </div>
      {stamp && <Stamp stamp={stamp} compact />}
    </header>
  )
}

/** 实例进度：服务端按节点给一行（标题、状态、时间），不下发原始事件。状态的剪影同运行的状态徽标 */
const STEP_STATUS: Record<string, string> = {
  running: 'running', done: 'succeeded', failed: 'failed', skipped: 'skipped', waiting: 'interrupted',
}

export function InstanceTimeline({ node }: NodeProps) {
  const steps: { node?: string; title?: string; state?: string; at?: string }[] =
    Array.isArray(node.data?.steps) ? node.data!.steps : []
  return (
    <div className="rounded-lg border bg-panel p-3" data-component="instance_timeline">
      {!steps.length ? <div className="text-2xs text-faint">{VIEW_TEXT.timelineEmpty}</div> : (
        <ol className="space-y-1.5">
          {steps.map((it, i) => (
            <li key={it.node ?? i} className="flex items-center gap-2 text-xs" data-timeline-step={it.state ?? ''}>
              <StatusPill status={STEP_STATUS[it.state ?? ''] ?? 'pending'} plain short
                          pendingApproval={it.state === 'waiting'} />
              <span className="min-w-0 flex-1 truncate text-fg">{it.title || it.node}</span>
              {it.at && <span className="shrink-0 text-2xs text-faint" title={formatDateTime(it.at)}>{formatRelative(it.at)}</span>}
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}

/**
 * 待办列表：本实例的（实例页，就地处理）或派给我的（链到待办详情）。服务端给的是摘要；实例页要就地处理，按运行取完整的待办
 */
export function TaskList({ node }: NodeProps) {
  const { runId, preview, onReload } = useView()
  const scope = node.props.scope === 'instance' || node.data?.scope === 'instance' ? 'instance' : 'mine'
  const summaries: TaskSummary[] = Array.isArray(node.data?.items) ? node.data!.items : []
  const { items, error, retry } = useList<Task>(undefined,
    scope === 'instance' && runId && !preview ? () => api.tasks.list({ run_id: runId, status: 'pending' }) : null,
    `${scope}|${runId}`)
  if (scope === 'mine' || !runId || preview) {
    const pending = summaries.filter((t) => t.status === 'pending')
    return (
      <div className="rounded-lg border bg-panel" data-component="task_list">
        {!pending.length ? <div className="px-3 py-3 text-2xs text-faint">{VIEW_TEXT.tasks.empty}</div> : (
          <ul>
            {pending.map((t) => (
              <li key={t.id}>
                {preview ? <TaskRow task={t} /> : (
                  <Link to={`/apps/inbox/${encodeURIComponent(t.id)}`} className="block hover:bg-hover"><TaskRow task={t} /></Link>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    )
  }
  return (
    <div className="overflow-hidden rounded-lg border bg-panel" data-component="task_list">
      {error ? <ErrorState compact error={error} onRetry={retry} className="m-2" />
        : !items ? <div className="px-3 py-3"><Spinner size={11} /></div>
        : !items.length ? <div className="px-3 py-3 text-2xs text-faint">{VIEW_TEXT.tasks.empty}</div>
        : items.map((t) => <TaskCard key={t.id} compact task={t} onResolved={() => { retry(); onReload?.() }} />)}
    </div>
  )
}

/** 待办的摘要（页面解析给的那几项） */
type TaskSummary = Pick<Task, 'id' | 'run_id' | 'node_id' | 'kind' | 'title' | 'status' | 'due_at' | 'escalation_level'>

function TaskRow({ task }: { task: TaskSummary }) {
  const now = useTicker(60_000)
  return (
    <div className="flex items-center gap-2 border-b px-3 py-2 text-xs" data-task-row={task.id}>
      <span className="chip shrink-0">{TASK_KIND_LABEL[task.kind] ?? task.kind}</span>
      <span className="min-w-0 flex-1 truncate">{task.title}</span>
      <Due dueAt={task.due_at} now={now} />
      <ArrowRight size={12} aria-hidden className="shrink-0 text-faint" />
    </div>
  )
}

/** 一条待办（task_form、signoffs）：服务端给的是摘要，按 id 取完整的再画；预览里只读 */
function useFullTask(summary: TaskSummary | undefined, preview: boolean) {
  const id = summary?.id
  const fetcher = id && !preview ? () => api.tasks.get(id).then((t) => ({ items: [t] })) : null
  const { items, error, retry } = useList<Task>(undefined, fetcher, id)
  return { task: items?.[0], error, retry }
}

export function TaskForm({ node }: NodeProps) {
  const { preview, onReload } = useView()
  const summary = node.data?.task as TaskSummary | undefined
  const { task, error, retry } = useFullTask(summary, preview)
  if (!summary) return <div className="text-2xs text-faint" data-component="task_form">{VIEW_TEXT.tasks.empty}</div>
  return (
    <div className="overflow-hidden rounded-lg border" data-component="task_form">
      {preview ? <TaskRow task={summary} />
        : error ? <ErrorState compact error={error} onRetry={retry} className="m-2" />
        : !task ? <div className="px-3 py-3"><Spinner size={11} /></div>
        : <TaskCard task={task} onResolved={() => { retry(); onReload?.() }} />}
    </div>
  )
}

/** 会签进度：页面解析给了派给谁、谁回复了什么，拼成待办卡上那一块的样子 */
export function SignoffsBlock({ node }: NodeProps) {
  const summary = node.data?.task as (TaskSummary & { policy?: Task['policy'] }) | undefined
  const raw: Record<string, any>[] = Array.isArray(node.data?.assignments) ? node.data!.assignments : []
  if (!summary) return <div className="text-2xs text-faint" data-component="signoffs">{VIEW_TEXT.tasks.empty}</div>
  const task = {
    ...summary, policy: summary.policy ?? { kind: 'any' },
    assignments: raw.map((a, i) => ({
      id: String(i), principal_kind: a.principal_kind, principal: str(a.principal), source: '', status: a.status,
      responded_by: a.responded_by ?? null, responded_at: a.responded_at ?? null, decision: a.decision ?? null,
      verified: !!a.verified,
    })),
  } as unknown as Task
  return <div data-component="signoffs"><Signoffs task={task} /></div>
}

const STATE_COLOR: Record<ActionState, string> = {
  pending: 'var(--text-dim)', sent: 'var(--accent)', confirmed: 'var(--st-done)', failed: 'var(--err)',
  unknown: 'var(--st-waiting)', simulated: 'var(--text-dim)',
}

/** 动作状态：状态、环境、哈希前 8 位、回执（开登录时使用者看不到回执原文）；失败写原因 */
export function ActionStatus({ node }: NodeProps) {
  const label = str(node.props.label)
  const records: Partial<ActionRecordRow>[] = Array.isArray(node.data?.records) ? node.data!.records : []
  return (
    <div className="rounded-lg border bg-panel p-3" data-component="action_status">
      {label && <div className="mb-1.5 text-xs font-medium">{label}</div>}
      {!records.length ? <div className="text-2xs text-faint">{ACTION_TEXT.empty}</div> : (
        <ul className="space-y-2">
          {records.map((a, i) => {
            const state = (a.state ?? 'pending') as ActionState
            return (
              <li key={a.id ?? i} className="space-y-0.5 text-2xs" data-action={state}>
                <div className="flex flex-wrap items-center gap-x-2">
                  <span className="font-semibold" style={{ color: STATE_COLOR[state] }}>{ACTION_STATE_LABEL[state] ?? state}</span>
                  {a.environment && <span className="text-dim">{ACTION_ENV_LABEL[a.environment] ?? a.environment}</span>}
                  {(a.attempt ?? 1) > 1 && <span className="text-dim">{ACTION_TEXT.attempt(a.attempt!)}</span>}
                  {a.payload_hash && <code className="mono text-faint">{ACTION_TEXT.hash(a.payload_hash.slice(0, 8))}</code>}
                </div>
                {a.error && <div style={{ color: 'var(--err)' }}>{ACTION_TEXT.error}：{a.error}</div>}
                {a.receipt && (
                  <details>
                    <summary className="cursor-pointer text-dim">{ACTION_TEXT.receipt}</summary>
                    <pre className="mono mt-0.5 max-h-32 overflow-auto rounded border bg-bg p-1.5 text-dim">{JSON.stringify(a.receipt, null, 2)}</pre>
                  </details>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
