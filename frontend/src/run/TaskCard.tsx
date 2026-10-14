/**
 * 待办卡（应用平台 X4）：从 RunPanel 的 ApprovalCard 抽出，和画布状态解耦——onResolved 必填，内部调 /api/tasks/{id}/respond。
 *
 * 用在待办箱、实例页、记录页、画布、问数据。报告核对概况、反方审稿、双盲复算、质询、动作确认的哈希卡、核实选项、
 * 会签进度都在这里。契约骨架里只有最简实现，FE-5 补齐，props 不变。
 */
import { api } from '../api/client'
import type { Task } from '../typesApp'

export interface TaskCardProps {
  task: Task
  /** 回复成功之后（凑齐了、或这个人的那一票记下了）。调用方决定刷新列表还是跳转 */
  onResolved: (result: { task: Task; resolved: boolean }) => void
  compact?: boolean
}

export function TaskCard({ task, onResolved }: TaskCardProps) {
  return (
    <div className="rounded-md border bg-panel p-3">
      <p className="text-sm font-medium">{task.title}</p>
      {task.can_respond && (
        <div className="mt-2 flex gap-2">
          <button type="button" className="btn btn-primary"
            onClick={async () => onResolved(await api.tasks.respond(task.id, { decision: 'approve' }))}>批准</button>
          <button type="button" className="btn"
            onClick={async () => onResolved(await api.tasks.respond(task.id, { decision: 'reject' }))}>驳回</button>
        </div>
      )}
    </div>
  )
}
