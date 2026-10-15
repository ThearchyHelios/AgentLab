/**
 * 审批 → 待办卡要的 Task 形状（应用平台 X4）。一次中断对应一条 Approval 和一条 Task，待办上给人看的内容（标题、中断内容、
 * 表单）都原样来自中断内容，审批上全有：审批卡（RunPanel 的 ApprovalCard）当场按审批拼出来渲染，不再为一张卡多发一次请求
 * 去取待办——收到中断就得出卡，预览和老服务端也没有待办可取。
 *
 * 种类、标题、表单的认法同服务端 process/tasks.open_task：人工节点按 mode（派给外部系统的是 signal），工具审批、代码审批、
 * 双盲复算、动作确认按中断内容的 kind。派给谁、时限这些只在待办上有，这里按「任何人、无时限」拼——画布、问数据里的运行
 * 不属于应用，服务端派的也是任何人；真要按会签、时限看，去记录页或待办箱看服务端的待办。
 */
import type { Approval } from '../types'
import type { Task, TaskKind } from '../typesApp'

const KIND_OF: Record<string, TaskKind> = {
  tool_approval: 'tool', code_approval: 'code', dual_check: 'dual_check', action_confirm: 'action_confirm',
}

/** 中断内容 → 待办种类，同服务端 process/tasks.kind_of */
export function taskKindOf(payload: Record<string, any> | null | undefined, mode?: string): TaskKind {
  const p = payload ?? {}
  if (p.kind === 'human_node') {
    if (p.assignee && typeof p.assignee === 'object' && p.assignee.kind === 'hook') return 'signal'
    const m = String(p.mode ?? mode ?? 'approve')
    return m === 'input' || m === 'edit' ? m : 'approve'
  }
  return KIND_OF[String(p.kind)] ?? 'approve'
}

/**
 * 按审批拼一条待办。id 给审批上的 task_id（服务端在审批列表上给）；老服务端没有就用审批 id 占位——这时回复不走
 * /api/tasks，由调用方改走老的审批接口
 */
export function taskFromApproval(a: Approval, id: string = a.task_id || a.id, patch: Partial<Task> = {}): Task {
  const p: Record<string, any> = a.payload ?? {}
  const pending = a.status === 'pending'
  return {
    id, approval_id: a.id, run_id: a.run_id, node_id: a.node_id || null, node_label: a.node_label ?? null,
    run_class: a.run_class ?? null, workflow_id: a.workflow_id ?? null, workflow_name: a.workflow_name ?? null,
    app_id: null, app_version: null, kind: taskKindOf(p, a.mode), title: a.title || String(p.title ?? ''),
    form: p.form && typeof p.form === 'object' && !Array.isArray(p.form) ? p.form : null,
    policy: { kind: 'any' }, status: pending ? 'pending' : 'resolved', due_at: null, escalation_level: 0, payload: p,
    assignments: [{
      id: `as-${a.id}`, principal_kind: 'anyone', principal: '', source: 'anyone', status: pending ? 'pending' : 'responded',
      responded_by: a.resolved_by ?? null, responded_at: a.resolved_at ?? null, verified: false,
    }],
    can_respond: pending, outcome: pending ? null : (a.response ?? {}), resolved_by: a.resolved_by ?? null,
    resolved_at: a.resolved_at ?? null, created_at: a.created_at ?? new Date().toISOString(), ...patch,
  }
}
