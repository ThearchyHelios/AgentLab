/**
 * 预览里合成的审批带上它对应的待办（应用平台 X4：一次中断对应一条 Approval 和一条 Task，审批卡内容在 run/TaskCard，
 * 回复走 /api/tasks/{id}/respond）。审批卡（RunPanel 的 ApprovalCard）看到审批上带着 task 就直接用，不再按运行去取待办
 * ——预览页没有服务端，按运行取只会落到「未能读取这条审批对应的待办」。
 *
 * 种类同服务端 process/tasks.kind_of：人工节点按 mode，工具审批、代码审批、双盲复算、动作确认按中断内容的 kind。
 * 检查脚本按 task-<审批 id> 伪造 /api/tasks/{id}/respond
 */
import type { Approval } from '../types'
import type { Task, TaskKind } from '../typesApp'

const KIND_OF: Record<string, TaskKind> = {
  tool_approval: 'tool', code_approval: 'code', dual_check: 'dual_check', action_confirm: 'action_confirm',
}

export const taskIdOf = (approvalId: string) => `task-${approvalId}`

export function taskOf(a: Approval, patch: Partial<Task> = {}): Task {
  const p: Record<string, any> = a.payload ?? {}
  const mode = String(p.mode ?? a.mode ?? 'approve')
  const kind: TaskKind = p.kind === 'human_node'
    ? (mode === 'input' || mode === 'edit' ? mode : 'approve')
    : KIND_OF[String(p.kind)] ?? 'approve'
  const pending = a.status === 'pending'
  return {
    id: taskIdOf(a.id), approval_id: a.id, run_id: a.run_id, node_id: a.node_id, node_label: a.node_label ?? null,
    run_class: a.run_class ?? null, workflow_id: a.workflow_id ?? null, workflow_name: a.workflow_name ?? null,
    app_id: null, app_version: null, kind, title: a.title, form: null, policy: { kind: 'any' },
    status: pending ? 'pending' : 'resolved', due_at: null, escalation_level: 0, payload: p,
    assignments: [{
      id: `as-${a.id}`, principal_kind: 'anyone', principal: '', source: 'anyone', status: pending ? 'pending' : 'responded',
      responded_by: a.resolved_by ?? null, responded_at: a.resolved_at ?? null, verified: false,
    }],
    can_respond: pending, outcome: pending ? null : (a.response ?? {}), resolved_by: a.resolved_by ?? null,
    resolved_at: a.resolved_at ?? null, created_at: a.created_at ?? new Date().toISOString(), ...patch,
  }
}

/** 审批带上待办和待办 id（同服务端审批列表上的 task_id） */
export function withTask<A extends Approval>(a: A): A & { task: Task; task_id: string } {
  const task = taskOf(a)
  return { ...a, task_id: task.id, task }
}
