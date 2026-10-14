/**
 * 待办卡（应用平台 X4）：从 RunPanel 的 ApprovalCard 抽出，和画布状态解耦——onResolved 必填，内部调 /api/tasks/{id}/respond。
 *
 * 用在待办箱、实例页、记录页、画布、问数据。审批卡原有的全部能力都在这里：报告核对概况、反方审稿、双盲复算、质询、
 * 补充输入和改稿、工具审批的参数（探索运行里的「始终允许」）、代码审批。另加流程的几样：
 * - 会签进度：派给了谁、谁同意了、谁驳回了，按策略（任一 / 全体 / 至少 m 人）说清还差什么；
 * - 时限与升级：剩多久、超了多久、升级过几次；超时只会提醒、升级或驳回，不会自动批准；
 * - 动作确认的哈希卡：遮罩后的预览、内容哈希前 8 位、环境；确认时原样带回哈希，服务端发送前重算，对不上不发（P8）；
 * - 核实待办：已生效（填外部单号）/ 未生效重新发送 / 未生效放弃，结果未知永远不会自动重发；
 * - 等外部系统（signal）：说明由入站钩子完成，不在这里处理。
 *
 * 不碰画布的 store：恢复之后谁去重新接事件流、刷新哪张列表，由调用方在 onResolved 里决定。会签时一个人的回复记下了、
 * 还没凑齐（resolved 为假），照样回调——调用方据此刷新卡片。
 *
 * 回复的内容（TaskRespond.values）：有表单（task.form）时是表单的值；没有表单的补充输入、改稿、双盲复算是 {value: …}，
 * 服务端照旧把 value 交给节点；「始终允许」是 {always: true}。
 */
import { useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import {
  Ban, Check, CheckCheck, Clock, Code, ExternalLink, Hand, Keyboard, PenLine, Send, SearchCheck, TriangleAlert,
  Webhook, Wrench,
} from 'lucide-react'
import clsx from 'clsx'
import { ApiError, api } from '../api/client'
import { useCatalog } from '../store/catalog'
import { principalName, useAtLeast, useLoginOn, usePrincipal } from '../store/auth'
import { SchemaForm } from '../components/SchemaForm'
import { Spinner, useTicker, useToast } from '../components/ui'
import { useLocalActor } from '../lib/actor'
import { formatDateTime, formatSpan, formatTime, parseServerTime } from '../lib/format'
import { cleanValues, formErrorsOf, validateForm, withDefaults } from '../lib/schemaForm'
import {
  ACTION_CONFIRM_TEXT, ACTION_ENV_LABEL, INQUIRY_TEXT, RECONCILE_TEXT, SIGNAL_TEXT, TASK_KIND_LABEL, TASK_STATUS_LABEL,
  TASK_TEXT, TOOL_TRUST_TEXT, runName,
} from '../lib/terms'
import type { Approval, ApprovalReport } from '../types'
import type { FormError, Task, TaskAssignment, TaskKind, TaskRespond } from '../typesApp'
import { Markdown } from './Markdown'
import { ApprovalReports, ApprovalRunClass } from './ApprovalReport'
import { ChallengeSummary } from './ChallengeSummary'
import { DualCheckApproval } from './DualCheckApproval'
import { useInquiryBusy } from './Inquiry'

export interface TaskCardProps {
  task: Task
  /** 回复成功之后（凑齐了、或这个人的那一票记下了）。调用方决定刷新列表还是跳转 */
  onResolved: (result: { task: Task; resolved: boolean }) => void
  /** 列表里用：不画「批了会怎样」的说明、签批人那一行和底部的链接 */
  compact?: boolean
}

const KIND_ICON: Record<TaskKind, typeof Hand> = {
  approve: Hand, input: Keyboard, edit: PenLine, tool: Wrench, code: Code, dual_check: Hand,
  action_confirm: Send, reconcile: SearchCheck, signal: Webhook,
}

/** 一天没人处理就用琥珀色写等了多久，同审批卡 */
const STALE_MS = 24 * 3_600_000

export function TaskCard({ task, onResolved, compact = false }: TaskCardProps) {
  const toast = useToast()
  const payload: Record<string, any> = task.payload ?? {}
  const kind = task.kind
  const pending = task.status === 'pending'
  const canAct = pending && task.can_respond
  const inquiring = useInquiryBusy(task.run_id)
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState('')
  const [errors, setErrors] = useState<FormError[]>([])
  const [values, setValues] = useState<Record<string, unknown>>(() => (task.form ? withDefaults(task.form, {}) : {}))
  const [text, setText] = useState(() => (payload.draft != null ? String(payload.draft) : ''))

  // 应用里的运行一律是固定版本的正式运行；别处的看中断内容自己带的运行类别，没有就不画那一行
  const runClass: 'formal' | 'exploratory' | null = task.app_id ? 'formal'
    : payload.run_class === 'formal' || payload.run_class === 'exploratory' ? payload.run_class : null
  const binary = kind === 'approve' || kind === 'tool' || kind === 'code'
  const trustKey: string | undefined = kind === 'tool' && runClass !== 'formal'
    ? (typeof payload.trust_key === 'string' && payload.trust_key) || undefined : undefined
  const reports: ApprovalReport[] = Array.isArray(payload.reports)
    ? payload.reports.filter((r: unknown): r is ApprovalReport =>
      !!r && typeof r === 'object' && typeof (r as ApprovalReport).node_id === 'string')
    : []

  /** 回复一次。422 带 {errors} 的挂到表单上；别的错误交给 toast */
  const respond = async (body: TaskRespond, done: string) => {
    if (busy) return
    setBusy(true)
    try {
      const res = await api.tasks.respond(task.id, { ...body, ...(note.trim() && !body.note ? { note: note.trim() } : {}) })
      setErrors([])
      void useCatalog.getState().refreshApprovals()
      toast(res.resolved ? done : TASK_TEXT.doneVote, 'ok')
      onResolved(res)
    } catch (e) {
      const fe = e instanceof ApiError && e.status === 422 ? formErrorsOf(e) : null
      if (fe) setErrors(fe)
      else toast.error(e)
    } finally {
      setBusy(false)
    }
  }

  /** 批准 / 提交：有表单的先按子集校验，不合规不发 */
  const approve = () => {
    if (task.form) {
      const local = validateForm(task.form, values)
      if (local.length) { setErrors(local); return }
      void respond({ decision: binary ? 'approve' : 'submit', values: cleanValues(task.form, values) },
        binary ? TASK_TEXT.doneApprove : TASK_TEXT.doneSubmit)
      return
    }
    if (binary) void respond({ decision: 'approve' }, TASK_TEXT.doneApprove)
    else void respond({ decision: 'submit', values: { value: text } }, TASK_TEXT.doneSubmit)
  }
  const reject = () => void respond({ decision: 'reject' }, binary ? TASK_TEXT.doneReject : TASK_TEXT.doneRejectStop)

  /** 「始终允许」：批准这一次，并让服务端把这个工具设成门控把关（工具页的信任档跟着变，目录重拉一次） */
  const always = async () => {
    if (!trustKey) return
    await respond({ decision: 'approve', values: { always: true } }, TOOL_TRUST_TEXT.alwaysDone(trustKey))
    void useCatalog.getState().reload('tools')
  }

  const Icon = KIND_ICON[kind] ?? Hand
  const showForm = !!task.form && (kind === 'approve' || kind === 'input' || kind === 'edit')

  return (
    <div className={clsx('fade-up border-b last:border-b-0', compact ? 'p-2.5' : 'p-3')}
         data-task={task.id} data-task-kind={kind} data-task-status={task.status} data-approval={task.approval_id ?? undefined}
         style={pending ? { background: 'color-mix(in srgb, var(--warn) 7%, transparent)' } : undefined}>
      <div className="mb-1 flex items-center gap-1.5 text-xs font-semibold"
           style={{ color: pending ? 'var(--warn)' : 'var(--text-dim)' }}>
        <Icon size={13} aria-hidden /> <span className="min-w-0 truncate">{task.title || TASK_TEXT.untitled}</span>
      </div>
      <TaskMeta task={task} />

      {!pending && <Resolved task={task} />}
      {(task.assignments.length > 1 || task.policy.kind !== 'any') && <Signoffs task={task} />}

      {(kind === 'approve' || kind === 'input' || kind === 'edit') && payload.kind === 'human_node' && runClass && (
        <ApprovalRunClass runClass={runClass} approve={kind === 'approve'} />
      )}

      {payload.message && (
        <div className="mb-2 max-h-40 overflow-y-auto rounded border bg-bg p-2">
          <Markdown text={String(payload.message)} dense />
        </div>
      )}

      {kind === 'dual_check' && (
        <DualCheckApproval approval={asApproval(task, runClass)}
                           decide={async (ok, reply) => {
                             const res = await api.tasks.respond(task.id, ok
                               ? { decision: 'submit', values: { value: reply }, note: reply.note }
                               : { decision: 'reject', note: reply.note })
                             void useCatalog.getState().refreshApprovals()
                             onResolved(res)
                           }} />
      )}

      <ApprovalReports reports={reports} runId={task.run_id} runClass={runClass} approvalId={task.approval_id ?? undefined}
                       askable={pending && !!task.approval_id} />
      <ChallengeSummary reports={reports} runId={task.run_id} runClass={runClass} />

      {kind === 'tool' && payload.tool && (
        <div className="mb-2 rounded border bg-bg p-2" data-task-tool="">
          <div className="mb-1 flex items-center gap-1.5 text-2xs">
            <Wrench size={11} aria-hidden /> <code className="mono">{String(payload.tool)}</code>
          </div>
          <pre className="mono max-h-32 overflow-auto text-2xs leading-relaxed text-dim">
            {JSON.stringify(payload.args ?? {}, null, 2)}
          </pre>
        </div>
      )}
      {kind === 'code' && payload.code && (
        <pre className="mono mb-2 max-h-40 overflow-auto rounded border bg-bg p-2 text-2xs leading-relaxed">
          {String(payload.code)}
        </pre>
      )}

      {kind === 'action_confirm' && (
        <ActionConfirm task={task} canAct={canAct} busy={busy || inquiring} note={note} setNote={setNote}
                       onConfirm={(hash) => void respond({ decision: 'approve', payload_hash: hash }, ACTION_CONFIRM_TEXT.done)}
                       onReject={() => void respond({ decision: 'reject' }, ACTION_CONFIRM_TEXT.rejected)} />
      )}
      {kind === 'reconcile' && (
        <Reconcile task={task} canAct={canAct} busy={busy} note={note} setNote={setNote}
                   onSubmit={(body) => void respond(body, RECONCILE_TEXT.done)} />
      )}
      {kind === 'signal' && <SignalNote payload={payload} />}

      {showForm && (
        <div className="mb-2 rounded border bg-bg p-2.5">
          <SchemaForm schema={task.form!} value={values} errors={errors} readOnly={!canAct}
                      idPrefix={`task-${task.id}`} onSubmit={canAct ? approve : undefined}
                      onChange={(v) => { setValues(v); if (errors.length) setErrors([]) }} />
        </div>
      )}
      {!showForm && canAct && (kind === 'input' || kind === 'edit') && (
        <textarea className="field mb-2" rows={4} value={text}
                  aria-label={kind === 'edit' ? TASK_TEXT.editedContent : TASK_TEXT.yourInput}
                  placeholder={kind === 'edit' ? TASK_TEXT.editedContent : TASK_TEXT.yourInput}
                  onChange={(e) => setText(e.target.value)} />
      )}

      {canAct && (binary || kind === 'input' || kind === 'edit') && (
        <>
          <input className="field mb-2" aria-label={TASK_TEXT.note} placeholder={TASK_TEXT.notePlaceholder}
                 value={note} onChange={(e) => setNote(e.target.value)} />
          <div className="flex gap-2">
            <button type="button" className="btn btn-primary flex-1 justify-center" disabled={busy || inquiring}
                    data-task-approve="" onClick={approve}>
              {busy ? <Spinner size={12} /> : <Check size={12} aria-hidden />} {binary ? TASK_TEXT.approve : TASK_TEXT.submit}
            </button>
            {trustKey && (
              <button type="button" className="btn flex-1 justify-center" disabled={busy || inquiring} data-approval-always=""
                      title={TOOL_TRUST_TEXT.alwaysHint(trustKey)} onClick={() => void always()}>
                <CheckCheck size={12} aria-hidden /> {TOOL_TRUST_TEXT.alwaysButton}
              </button>
            )}
            <button type="button" className="btn btn-danger flex-1 justify-center" disabled={busy || inquiring}
                    data-task-reject="" onClick={reject}>
              <Ban size={12} aria-hidden /> {binary ? TASK_TEXT.reject : TASK_TEXT.rejectStop}
            </button>
          </div>
          {inquiring && (
            <div className="mt-1.5 flex items-start gap-1 text-2xs font-medium leading-relaxed text-fg" role="status"
                 data-approval-inquiry-busy="">
              <span className="mt-[3px] shrink-0" style={{ color: 'var(--st-waiting)' }}><Spinner size={11} /></span>
              <span>{INQUIRY_TEXT.decideBlocked}</span>
            </div>
          )}
          {!compact && (
            <div className="mt-1.5 text-2xs leading-relaxed text-dim">
              {binary ? TASK_TEXT.approveHint : TASK_TEXT.inputHint}
            </div>
          )}
        </>
      )}
      {pending && !task.can_respond && kind !== 'signal' && (
        <div className="mt-1 text-2xs leading-relaxed text-dim" data-task-readonly="">{TASK_TEXT.cannotRespond}</div>
      )}

      {canAct && kind !== 'signal' && !compact && <Signer />}
      {!compact && <TaskLinks task={task} />}
    </div>
  )
}

/** DualCheckApproval 认的是审批记录：按待办拼一份它要的几项 */
function asApproval(task: Task, runClass: 'formal' | 'exploratory' | null): Approval {
  return {
    id: task.approval_id ?? task.id, run_id: task.run_id, node_id: task.node_id ?? '', mode: 'input',
    title: task.title, payload: task.payload ?? {}, status: task.status === 'pending' ? 'pending' : 'resolved',
    response: task.outcome ?? {}, created_at: task.created_at, workflow_name: task.workflow_name ?? null,
    run_class: runClass,
  }
}

// ---------------------------------------------------------------- 卡头

/** 种类 · 应用或工作流 · 节点 · 什么时候发起、等了多久 · 时限 · 升级 */
function TaskMeta({ task }: { task: Task }) {
  const now = useTicker(60_000)
  const created = parseServerTime(task.created_at)
  const age = created ? now - created.getTime() : undefined
  const pending = task.status === 'pending'
  // 节点的名字：服务端在待办上给（node_label，契约之外多给的一项），老的中断内容里也可能带着
  const label = (task as Task & { node_label?: string | null }).node_label ?? task.payload?.node_label
  const where = typeof label === 'string' && label ? label : task.node_id
  const owner = task.app_name ? task.app_name : task.workflow_name ? runName(task) : null
  return (
    <div className="tnum mb-2 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-2xs text-dim">
      <span>{TASK_KIND_LABEL[task.kind] ?? task.kind}</span>
      {owner && <><span aria-hidden>·</span><span>{owner}</span></>}
      {where && <><span aria-hidden>·</span><span>{TASK_TEXT.node(where)}</span></>}
      {created && <><span aria-hidden>·</span><span title={formatDateTime(created)}>{TASK_TEXT.createdAt(formatTime(created))}</span></>}
      {pending && age != null && age >= 60_000 && (
        <>
          <span aria-hidden>·</span>
          <span style={age > STALE_MS ? { color: 'var(--st-waiting)', fontWeight: 600 } : undefined}>
            {TASK_TEXT.waited(formatSpan(age, { coarse: true }))}
          </span>
        </>
      )}
      {pending && <Due dueAt={task.due_at} now={now} />}
      {task.escalation_level > 0 && (
        <span className="chip" style={{ color: 'var(--st-waiting)' }} title={TASK_TEXT.escalatedHint}
              data-task-escalated={task.escalation_level}>
          <TriangleAlert size={10} aria-hidden /> {TASK_TEXT.escalated(task.escalation_level)}
        </span>
      )}
    </div>
  )
}

/** 时限：还剩多久，超了写超了多久（红）。悬停写明超时只会提醒、升级或驳回 */
export function Due({ dueAt, now }: { dueAt: string | null; now: number }) {
  const due = parseServerTime(dueAt)
  if (!due) return null
  const left = due.getTime() - now
  const over = left < 0
  return (
    <span className="chip" data-task-due={over ? 'overdue' : 'ok'}
          title={`${TASK_TEXT.due(formatDateTime(due))}。${TASK_TEXT.timeoutNever}`}
          style={{ color: over ? 'var(--err)' : left < 3_600_000 ? 'var(--st-waiting)' : 'var(--text-dim)' }}>
      <Clock size={10} aria-hidden />
      {over ? TASK_TEXT.overdue(formatSpan(-left, { coarse: true })) : TASK_TEXT.dueIn(formatSpan(left, { coarse: true }))}
    </span>
  )
}

/** 处理过的：谁、什么时候、结果 */
function Resolved({ task }: { task: Task }) {
  const decision = typeof task.outcome?.decision === 'string' ? TASK_TEXT.decision[task.outcome.decision] ?? task.outcome.decision : null
  const at = task.resolved_at ? formatDateTime(task.resolved_at) : ''
  return (
    <div className="mb-2 flex flex-wrap items-center gap-x-1.5 text-2xs text-dim" data-task-resolved="">
      <span className="chip">{TASK_STATUS_LABEL[task.status] ?? task.status}</span>
      {decision && <span>{TASK_TEXT.outcome}：{decision}</span>}
      {(task.resolved_by || at) && (
        <span>{task.resolved_by ? TASK_TEXT.resolvedBy(task.resolved_by, at) : TASK_TEXT.resolvedAt(at)}</span>
      )}
    </div>
  )
}

// ---------------------------------------------------------------- 会签进度

const agreed = (a: TaskAssignment) => a.status === 'responded' && a.decision !== 'reject'
const rejected = (a: TaskAssignment) => a.status === 'responded' && a.decision === 'reject'

function assigneeName(a: TaskAssignment): string {
  if (a.principal_kind === 'anyone') return TASK_TEXT.assignee.anyone
  if (a.principal_kind === 'hook') return TASK_TEXT.assignee.hook
  if (a.principal_kind === 'group') return a.responded_by ? `${a.responded_by}（${TASK_TEXT.assignee.group(a.principal)}）` : TASK_TEXT.assignee.group(a.principal)
  return a.principal
}

/** 会签：策略、几人同意了、每个人的回复。撤回的（改派、升级、已判定）列在最后、淡显 */
export function Signoffs({ task }: { task: Task }) {
  const live = task.assignments.filter((a) => a.status !== 'withdrawn')
  const ok = live.filter(agreed).length
  const no = live.filter(rejected).length
  const policy = task.policy.kind === 'all' ? TASK_TEXT.policy.all
    : task.policy.kind === 'quorum' ? TASK_TEXT.policy.quorum(task.policy.m) : TASK_TEXT.policy.any
  const need = task.policy.kind === 'quorum' ? task.policy.m : task.policy.kind === 'all' ? live.length : 1
  const ordered = [...live, ...task.assignments.filter((a) => a.status === 'withdrawn')]
  return (
    <div className="mb-2 rounded border bg-bg p-2" data-signoffs="" data-signoffs-agreed={ok} data-signoffs-need={need}>
      <div className="mb-1 flex flex-wrap items-center gap-x-2 text-2xs">
        <span className="font-medium text-faint">{TASK_TEXT.signoffs}</span>
        <span className="tnum text-fg">{TASK_TEXT.signoffCount(ok, need)}</span>
        {no > 0 && <span className="tnum" style={{ color: 'var(--err)' }}>{TASK_TEXT.signoffRejected(no)}</span>}
        <span className="text-dim">{policy}</span>
      </div>
      <div aria-hidden className="mb-1.5 flex h-1 gap-0.5 overflow-hidden rounded-full">
        {Array.from({ length: Math.max(need, 1) }, (_, i) => (
          <span key={i} className="flex-1 rounded-full"
                style={{ background: i < ok ? 'var(--st-done)' : 'var(--border)' }} />
        ))}
      </div>
      <ul className="space-y-0.5 text-2xs">
        {ordered.map((a) => (
          <li key={a.id} className={clsx('flex flex-wrap items-center gap-x-1.5', a.status === 'withdrawn' && 'opacity-50')}
              data-assignment={a.status} data-decision={a.decision ?? undefined}>
            <span aria-hidden style={{ color: agreed(a) ? 'var(--st-done)' : rejected(a) ? 'var(--err)' : 'var(--text-faint)' }}>
              {agreed(a) ? '✓' : rejected(a) ? '✕' : '○'}
            </span>
            <span className="text-fg">{assigneeName(a)}</span>
            <span className="text-dim">
              {a.status === 'responded'
                ? TASK_TEXT.decision[a.decision ?? ''] ?? TASK_TEXT.assignmentStatus.responded
                : TASK_TEXT.assignmentStatus[a.status]}
            </span>
            {a.responded_at && <span className="text-faint" title={formatDateTime(a.responded_at)}>{formatTime(a.responded_at)}</span>}
            {a.status === 'responded' && (
              <span className="text-faint">{a.verified ? TASK_TEXT.verified : TASK_TEXT.unverified}</span>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}

// ---------------------------------------------------------------- 动作确认、核实、等外部系统

/** 遮罩后的预览：一层键值，嵌套的按 JSON 写。值是服务端遮过的，原样显示 */
function Preview({ preview }: { preview: unknown }) {
  if (!preview || typeof preview !== 'object') {
    return <div className="text-2xs text-faint">{ACTION_CONFIRM_TEXT.previewEmpty}</div>
  }
  const rows = Object.entries(preview as Record<string, unknown>)
  if (!rows.length) return <div className="text-2xs text-faint">{ACTION_CONFIRM_TEXT.previewEmpty}</div>
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-2xs" data-action-preview="">
      {rows.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="mono text-faint">{k}</dt>
          <dd className="mono min-w-0 break-all text-fg">{typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v)}</dd>
        </div>
      ))}
    </dl>
  )
}

function KeyLine({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-2 text-2xs">
      <span className="shrink-0 text-faint">{label}</span>
      <span className="min-w-0 text-fg">{children}</span>
    </div>
  )
}

const envLabel = (env: unknown) => (typeof env === 'string' ? ACTION_ENV_LABEL[env] ?? env : '')

function ActionConfirm({ task, canAct, busy, note, setNote, onConfirm, onReject }: {
  task: Task; canAct: boolean; busy: boolean; note: string; setNote: (v: string) => void
  onConfirm: (hash: string) => void; onReject: () => void
}) {
  const p: Record<string, any> = task.payload ?? {}
  const hash = typeof p.payload_hash === 'string' ? p.payload_hash : ''
  const prod = p.environment === 'prod'
  return (
    <div className="mb-2 space-y-1.5 rounded border bg-bg p-2.5" data-action-confirm="" data-payload-hash={hash || undefined}>
      <div className="text-xs font-medium text-fg">{ACTION_CONFIRM_TEXT.head}</div>
      <p className="text-2xs leading-relaxed text-dim">{ACTION_CONFIRM_TEXT.intro}</p>
      {p.capability && <KeyLine label={ACTION_CONFIRM_TEXT.capability}><code className="mono">{String(p.capability)}</code></KeyLine>}
      {p.environment && (
        <KeyLine label={ACTION_CONFIRM_TEXT.environment}>
          <span data-action-env={p.environment} style={prod ? { color: 'var(--warn)', fontWeight: 600 } : undefined}>
            {envLabel(p.environment)}
          </span>
        </KeyLine>
      )}
      {typeof p.attempt === 'number' && p.attempt > 1 && (
        <KeyLine label="">{ACTION_CONFIRM_TEXT.attempt(p.attempt)}</KeyLine>
      )}
      <KeyLine label={ACTION_CONFIRM_TEXT.hash}>
        {hash
          ? <code className="mono" title={ACTION_CONFIRM_TEXT.hashHint(hash)} data-action-hash="">{hash.slice(0, 8)}</code>
          : <span style={{ color: 'var(--err)' }}>{ACTION_CONFIRM_TEXT.noHash}</span>}
      </KeyLine>
      <div>
        <div className="mb-0.5 text-2xs text-faint">{ACTION_CONFIRM_TEXT.preview}</div>
        <div className="rounded border p-1.5"><Preview preview={p.preview} /></div>
      </div>
      <p className="text-2xs leading-relaxed text-dim">{ACTION_CONFIRM_TEXT.fixed}</p>
      {canAct && (
        <>
          <input className="field" aria-label={TASK_TEXT.note} placeholder={TASK_TEXT.notePlaceholder} value={note}
                 onChange={(e) => setNote(e.target.value)} />
          <div className="flex gap-2">
            <button type="button" className="btn btn-primary flex-1 justify-center" disabled={busy || !hash}
                    data-action-confirm-send="" onClick={() => onConfirm(hash)}>
              {busy ? <Spinner size={12} /> : <Send size={12} aria-hidden />} {ACTION_CONFIRM_TEXT.confirm}
            </button>
            <button type="button" className="btn btn-danger flex-1 justify-center" disabled={busy}
                    data-action-confirm-reject="" onClick={onReject}>
              <Ban size={12} aria-hidden /> {ACTION_CONFIRM_TEXT.reject}
            </button>
          </div>
        </>
      )}
    </div>
  )
}

type ReconcileChoice = 'confirmed' | 'not_applied_resend' | 'not_applied_abandon'

function Reconcile({ task, canAct, busy, note, setNote, onSubmit }: {
  task: Task; canAct: boolean; busy: boolean; note: string; setNote: (v: string) => void
  onSubmit: (body: TaskRespond) => void
}) {
  const p: Record<string, any> = task.payload ?? {}
  const [choice, setChoice] = useState<ReconcileChoice | null>(null)
  const [externalId, setExternalId] = useState('')
  const [tried, setTried] = useState(false)
  const name = `reconcile-${task.id}`
  const sentAt = p.sent_at ?? p.request_at
  const missingId = choice === 'confirmed' && !externalId.trim()
  const options: { key: ReconcileChoice; label: string; hint: string }[] = [
    { key: 'confirmed', label: RECONCILE_TEXT.confirmed, hint: RECONCILE_TEXT.confirmedHint },
    { key: 'not_applied_resend', label: RECONCILE_TEXT.resend, hint: RECONCILE_TEXT.resendHint },
    { key: 'not_applied_abandon', label: RECONCILE_TEXT.abandon, hint: RECONCILE_TEXT.abandonHint },
  ]
  const submit = () => {
    setTried(true)
    if (!choice || missingId) return
    onSubmit({ decision: choice, ...(choice === 'confirmed' ? { external_id: externalId.trim() } : {}) })
  }
  return (
    <div className="mb-2 space-y-1.5 rounded border bg-bg p-2.5" data-reconcile="">
      <div className="text-xs font-medium text-fg">{RECONCILE_TEXT.head}</div>
      <p className="text-2xs leading-relaxed text-dim">{RECONCILE_TEXT.intro}</p>
      {p.capability && <KeyLine label={ACTION_CONFIRM_TEXT.capability}><code className="mono">{String(p.capability)}</code></KeyLine>}
      {p.environment && <KeyLine label={ACTION_CONFIRM_TEXT.environment}>{envLabel(p.environment)}</KeyLine>}
      {sentAt && <KeyLine label="">{RECONCILE_TEXT.sentAt(formatDateTime(sentAt))}</KeyLine>}
      {p.idem_key && <KeyLine label={RECONCILE_TEXT.idemKey}><code className="mono break-all">{String(p.idem_key)}</code></KeyLine>}
      {typeof p.payload_hash === 'string' && (
        <KeyLine label={ACTION_CONFIRM_TEXT.hash}>
          <code className="mono" title={ACTION_CONFIRM_TEXT.hashHint(p.payload_hash)}>{p.payload_hash.slice(0, 8)}</code>
        </KeyLine>
      )}
      {p.error && <KeyLine label={RECONCILE_TEXT.error}><span className="break-all">{String(p.error)}</span></KeyLine>}
      {p.preview && <div className="rounded border p-1.5"><Preview preview={p.preview} /></div>}
      {canAct && (
        <>
          <fieldset className="space-y-1" role="radiogroup" aria-label={RECONCILE_TEXT.head}>
            {options.map((o) => (
              <label key={o.key} className="flex items-start gap-2 rounded px-1 py-0.5 text-xs hover:bg-hover" data-reconcile-option={o.key}>
                <input type="radio" name={name} className="mt-0.5" checked={choice === o.key} onChange={() => setChoice(o.key)} />
                <span className="min-w-0">
                  <span className="text-fg">{o.label}</span>
                  <span className="block text-2xs text-dim">{o.hint}</span>
                </span>
              </label>
            ))}
          </fieldset>
          {choice === 'confirmed' && (
            <div>
              <label className="label" htmlFor={`${name}-ext`}>
                {RECONCILE_TEXT.externalId}<span className="ml-0.5 text-[var(--err)]" aria-hidden>*</span>
              </label>
              <input id={`${name}-ext`} className="field mono" value={externalId} aria-required
                     aria-invalid={tried && missingId ? true : undefined} data-reconcile-external=""
                     onChange={(e) => setExternalId(e.target.value)} />
              {tried && missingId && <div className="mt-1 text-2xs text-[var(--err)]">{RECONCILE_TEXT.externalIdRequired}</div>}
            </div>
          )}
          {tried && !choice && <div className="text-2xs text-[var(--err)]">{RECONCILE_TEXT.pick}</div>}
          <input className="field" aria-label={TASK_TEXT.note} placeholder={TASK_TEXT.notePlaceholder} value={note}
                 onChange={(e) => setNote(e.target.value)} />
          <button type="button" className="btn btn-primary w-full justify-center" disabled={busy} data-reconcile-submit=""
                  onClick={submit}>
            {busy ? <Spinner size={12} /> : <Check size={12} aria-hidden />} {RECONCILE_TEXT.submit}
          </button>
        </>
      )}
    </div>
  )
}

function SignalNote({ payload }: { payload: Record<string, any> }) {
  const correlation = payload.correlation ?? payload.correlation_key
  return (
    <div className="mb-2 space-y-1 rounded border border-dashed bg-bg p-2.5" data-task-signal="">
      <div className="flex items-center gap-1.5 text-xs font-medium text-fg">
        <Webhook size={12} aria-hidden /> {SIGNAL_TEXT.head}
      </div>
      <p className="text-2xs leading-relaxed text-dim">{SIGNAL_TEXT.intro}</p>
      {correlation && <KeyLine label={SIGNAL_TEXT.correlation}><code className="mono">{String(correlation)}</code></KeyLine>}
      {payload.hook && <KeyLine label={SIGNAL_TEXT.hook}><code className="mono">{String(payload.hook)}</code></KeyLine>}
    </div>
  )
}

// ---------------------------------------------------------------- 底部

/**
 * 以谁的名义处理：开了登录写登录身份（核实过的，没有署名框）；关登录写本机署名，没署名时提醒——留痕里会是空的
 */
function Signer() {
  const loginOn = useLoginOn()
  const me = usePrincipal()
  const actor = useLocalActor()
  if (loginOn) {
    return (
      <div className="mt-1 flex items-center gap-1 text-2xs leading-relaxed text-dim" data-task-signer="verified">
        <PenLine size={10} aria-hidden className="shrink-0" />
        <span>{TASK_TEXT.signAsVerified(principalName(me))}</span>
      </div>
    )
  }
  return (
    <div className="mt-1 flex items-center gap-1 text-2xs leading-relaxed" data-task-signer="actor"
         style={actor ? { color: 'var(--text-dim)' } : { color: 'var(--st-waiting)' }}>
      <PenLine size={10} aria-hidden className="shrink-0" />
      {actor
        ? <span>{TASK_TEXT.signAs(actor)}</span>
        : (
          <span>
            {TASK_TEXT.unsigned} ·{' '}
            <Link to="/settings/prefs" className="underline underline-offset-2 hover:text-fg">{TASK_TEXT.setActor}</Link>
          </span>
        )}
    </div>
  )
}

/** 去实例页（应用里的）或记录页（别处的，搭建者以上才看得到记录页） */
function TaskLinks({ task }: { task: Task }) {
  const builder = useAtLeast('builder')
  const links = useMemo(() => {
    const out: { to: string; label: string }[] = []
    if (task.app_id) out.push({ to: `/apps/${encodeURIComponent(task.app_id)}/i/${encodeURIComponent(task.run_id)}`, label: TASK_TEXT.openInstance })
    if (builder) out.push({ to: `/runs/${encodeURIComponent(task.run_id)}`, label: TASK_TEXT.openRun })
    return out
  }, [task.app_id, task.run_id, builder])
  if (!links.length) return null
  return (
    <div className="mt-1.5 flex flex-wrap gap-x-3 text-2xs">
      {links.map((l) => (
        <Link key={l.to} to={l.to} className="inline-flex items-center gap-1 text-dim underline-offset-2 hover:text-fg hover:underline">
          <ExternalLink size={10} aria-hidden /> {l.label}
        </Link>
      ))}
    </div>
  )
}
