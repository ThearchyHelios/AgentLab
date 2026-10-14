/**
 * 预览里合成的审批带上待办 id（同服务端审批列表上的 task_id）和待办本身。审批卡（RunPanel 的 ApprovalCard）按审批当场拼出
 * 待办来渲染（run/approvalTask），带着 task_id 时回复走 /api/tasks/{id}/respond；检查脚本按 task-<审批 id> 伪造这个接口。
 * 带上 task 是给想换掉某几项（表单、会签）的样例用的：审批卡看到它就照用
 */
import type { Approval } from '../types'
import type { Task } from '../typesApp'
import { taskFromApproval } from '../run/approvalTask'

export const taskIdOf = (approvalId: string) => `task-${approvalId}`

export function taskOf(a: Approval, patch: Partial<Task> = {}): Task {
  return taskFromApproval(a, taskIdOf(a.id), patch)
}

/** 审批带上待办和待办 id（同服务端审批列表上的 task_id） */
export function withTask<A extends Approval>(a: A): A & { task: Task; task_id: string } {
  const task = taskOf(a)
  return { ...a, task_id: task.id, task }
}
