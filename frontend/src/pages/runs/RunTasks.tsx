import { useCallback, useEffect, useState } from 'react'
import { ShieldCheck } from 'lucide-react'
import { api } from '../../api/client'
import { EmptyState, ErrorState, Skeleton } from '../../components/ui'
import { formatDateTime } from '../../lib/format'
import { ACTION_ENV_LABEL, ACTION_LOG_TEXT, ACTION_STATE_LABEL, ACTION_TEXT } from '../../lib/terms'
import { TaskCard } from '../../run/TaskCard'
import { ApprovalCard } from '../../run/RunPanel'
import type { Approval } from '../../types'
import type { ActionRecordRow, Task } from '../../typesApp'

/*
 * 运行详情里的待办卡和生效记录（应用平台 X4、§3.8）。
 *
 * 待办卡：这次运行待处理的待办，一条一张 TaskCard。以前按待审批画 ApprovalCard，可核实结果未知的动作、等外部系统
 * 回调这两种待办没有审批，时间线上就没有地方处理它们。卡片挂 data-approval-card（有审批的写审批 id）：等待横幅的
 * 「去处理」按它定位。待审批里对不上待办的（开待办出过错、老服务端）照旧按审批画审批卡，不能让等着的审批无处处理。
 *
 * 生效记录：这次运行对外部系统的每次写入（权威记录在服务端 action_records），状态、环境、哈希前 8 位、回执。
 * 时间线上的 action.* 事件只是它的影子：核实、重新发送都改这里的记录
 */

/**
 * 这次运行待处理的待办。sig 变了（待审批列表变了、运行状态变了）就重新取。
 *
 * asked：取这一次时手上有哪几条待审批。待审批里对不上待办的只在这几条里认——刚来的审批，它的待办还在路上，
 * 等这次取回来再说，不先按审批画一张、取回来又换成待办卡
 */
export function useRunTasks(runId: string, sig: string, pendingIds: string[] = []): {
  tasks: Task[] | null; asked: ReadonlySet<string>; reload: () => void
} {
  const [got, setGot] = useState<{ tasks: Task[]; asked: ReadonlySet<string> } | null>(null)
  const [epoch, setEpoch] = useState(0)
  useEffect(() => {
    let alive = true
    const asked = new Set(pendingIds)
    api.tasks.list({ run_id: runId, assigned: 'all', status: 'pending' })
      .then(({ items }) => {
        if (alive) setGot({ tasks: items.filter((t) => t.status === 'pending' || t.status === 'resolving'), asked })
      })
      .catch(() => { if (alive) setGot({ tasks: [], asked }) })
    return () => { alive = false }
  }, [runId, sig, epoch]) // eslint-disable-line react-hooks/exhaustive-deps
  return { tasks: got?.tasks ?? null, asked: got?.asked ?? NONE, reload: useCallback(() => setEpoch((n) => n + 1), []) }
}

const NONE: ReadonlySet<string> = new Set()

const CARD_STYLE = { borderColor: 'color-mix(in srgb, var(--st-waiting) 45%, var(--border))' }

export function RunTaskCards({ tasks, approvals = [], onResolved, onApprovalResolved }: {
  tasks: Task[]
  /**
   * 待审批里对不上待办的那几条：服务端开待办时出过错还没补上（老接口 /decide 照旧能处理）、老服务端没有待办接口。
   * 按审批画审批卡（run/RunPanel 的 ApprovalCard，当场按审批拼出待办），不让等待横幅的「去审批卡」落空
   */
  approvals?: Approval[]
  /** 回复记下了。resolved：凑齐了、运行会恢复（调用方重新接流）；没凑齐只刷新这几张卡 */
  onResolved: (task: Task, resolved: boolean) => void | Promise<void>
  /** 按审批画的那几张：运行恢复了（审批卡只在凑齐时回调） */
  onApprovalResolved?: (approval: Approval) => void | Promise<void>
}) {
  if (!tasks.length && !approvals.length) return null
  return (
    <div className="mt-3 space-y-2" data-run-tasks={tasks.length + approvals.length}>
      {tasks.map((t) => (
        <div key={t.id} data-approval-card={t.approval_id ?? t.id} data-task-card={t.id}
             className="relative overflow-hidden rounded-lg border" style={CARD_STYLE}>
          <TaskCard task={t} onResolved={(r) => onResolved(r.task, r.resolved)} />
        </div>
      ))}
      {approvals.map((a) => (
        <div key={a.id} data-approval-card={a.id} className="relative overflow-hidden rounded-lg border" style={CARD_STYLE}>
          <ApprovalCard approval={a} onResolved={() => onApprovalResolved?.(a)} />
        </div>
      ))}
    </div>
  )
}

/** 生效记录面板（运行详情的一个视图） */
export function EffectsPane({ runId, refreshKey }: { runId: string; refreshKey: string | number }) {
  const [rows, setRows] = useState<ActionRecordRow[] | null>(null)
  const [error, setError] = useState<unknown>(null)
  const load = useCallback(() => {
    setError(null)
    api.actions.list({ run_id: runId }).then(({ items }) => setRows(items)).catch(setError)
  }, [runId])
  useEffect(load, [load, refreshKey])
  if (error) return <ErrorState error={error} onRetry={load} compact className="m-4" />
  if (!rows) return <div className="p-4"><Skeleton rows={3} height={48} gap={8} /></div>
  if (!rows.length) {
    return <EmptyState icon={<ShieldCheck size={22} />} title={ACTION_LOG_TEXT.empty} body={ACTION_LOG_TEXT.panelHint} />
  }
  return (
    <div className="h-full overflow-y-auto p-4" data-effects-pane={rows.length}>
      <p className="mb-3 text-2xs leading-relaxed text-faint">{ACTION_LOG_TEXT.panelHint}</p>
      <div className="space-y-2">
        {rows.map((r) => <EffectRow key={r.id} row={r} />)}
      </div>
    </div>
  )
}

const STATE_COLOR: Record<string, string> = {
  confirmed: 'var(--ok)', failed: 'var(--st-failed)', unknown: 'var(--st-waiting)', simulated: 'var(--text-faint)',
}

function EffectRow({ row }: { row: ActionRecordRow }) {
  const json = (v: unknown) => JSON.stringify(v, null, 2)
  return (
    <details className="rounded-lg border bg-panel" data-effect={row.id} data-effect-state={row.state}>
      <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-xs">
        <span className="chip shrink-0" style={{ color: STATE_COLOR[row.state], borderColor: STATE_COLOR[row.state] }}>
          {ACTION_STATE_LABEL[row.state]}
        </span>
        <span className="mono min-w-0 flex-1 truncate" title={row.capability}>{row.capability}</span>
        <span className="shrink-0 text-2xs text-faint">{ACTION_ENV_LABEL[row.environment] ?? row.environment}</span>
        <span className="mono shrink-0 text-2xs text-faint" title={`${ACTION_LOG_TEXT.hashHint}\n${row.payload_hash}`}>
          {row.payload_hash.slice(0, 8)}
        </span>
        {row.attempt > 1 && <span className="tnum shrink-0 text-2xs text-faint">{ACTION_TEXT.attempt(row.attempt)}</span>}
        <time className="tnum shrink-0 text-2xs text-faint">{formatDateTime(row.updated_at)}</time>
      </summary>
      <div className="space-y-2 border-t px-3 py-2 text-2xs">
        <div className="text-faint">节点「{row.node_id}」</div>
        {row.preview && (
          <div>
            <div className="mb-0.5 text-dim">{ACTION_LOG_TEXT.preview}</div>
            <pre className="mono max-h-40 overflow-auto rounded border bg-bg p-2">{json(row.preview)}</pre>
          </div>
        )}
        {row.receipt && (
          <div>
            <div className="mb-0.5 text-dim">{ACTION_LOG_TEXT.receipt}</div>
            <pre className="mono max-h-40 overflow-auto rounded border bg-bg p-2">{json(row.receipt)}</pre>
          </div>
        )}
        {row.error && <div style={{ color: 'var(--st-failed)' }}>{row.error}</div>}
        {row.resolution && (
          <div>
            <div className="mb-0.5 text-dim">{ACTION_TEXT.resolution}</div>
            <pre className="mono max-h-32 overflow-auto rounded border bg-bg p-2">{json(row.resolution)}</pre>
          </div>
        )}
      </div>
    </details>
  )
}
