import { useMemo } from 'react'
import { api } from '../api/client'
import { useStudio } from '../store/studio'
import { useCatalog } from '../store/catalog'
import { NODE_DEFS } from '../canvas/nodeDefs'
import { StatusDot } from '../components/ui'
import { TaskCard } from './TaskCard'
import { taskFromApproval } from './approvalTask'
import type { Approval } from '../types'
import type { Task, TaskRespond } from '../typesApp'

// 发起运行的表单搬去了 run/RunControl.tsx 的工具栏控件里。它原先常驻在
// 助手栏顶部，和 Copilot 输入框两个"主要动作"互相压着——而它们是两种不同的
// 意图（跑这张图 / 改这张图），不该争同一块地方。运行是对整张图的动作，
// 和保存、发布同类，属于工具栏。

/**
 * 审批卡：现在只是一层包装（X4）。审批卡的内容——报告核对概况、反方审稿、双盲复算、质询、动作确认的哈希卡、
 * 核实选项、签批人——都在 run/TaskCard。
 *
 * 这里做三件事：
 * - 按审批当场拼出待办（run/approvalTask）：待办上给人看的内容都原样来自中断内容，审批上全有，收到中断就出卡，
 *   不为一张卡再去取一次待办。审批带着 task（预览的桩数据）就照用；
 * - 回复走哪里：审批上有 task_id（服务端在审批列表上给）就经待办服务（/api/tasks/{id}/respond）；没有（老服务端、
 *   合成样例）照旧走 /api/approvals/{id}/decide，「始终允许」照旧走 /runs/{id}/resume 带 always；
 * - 回复之后的接流：onResolved 给了就听它的（对话页、运行页各自重新接流），没给就是画布：恢复后由 studio 重新接上
 *   事件流。只有凑齐了（resolved）运行才会恢复；会签里记下一票时运行还停着，不接流。
 */
export function ApprovalCard({ approval, onResolved, showWorkflow = true }: {
  approval: Approval | any
  onResolved?: (runId: string) => void | Promise<void>
  /** 画布上工作流就是眼前这张，卡头不用再说一遍 */
  showWorkflow?: boolean
}) {
  const refreshApprovals = useCatalog((s) => s.refreshApprovals)
  const attachRun = useStudio((s) => s.attachRun)
  const given: Task | undefined = approval.task
  const task = useMemo(() => given ?? taskFromApproval(approval), [approval, given])
  const send = approval.task_id ? undefined : (body: TaskRespond) => decideLegacy(approval, task, body)
  return (
    <TaskCard task={task} send={send} showOwner={showWorkflow} onResolved={async (result) => {
      await refreshApprovals()
      if (!result.resolved) return
      if (onResolved) await onResolved(approval.run_id)
      else await attachRun(approval.run_id)
    }} />
  )
}

/**
 * 老的审批接口：服务端没给 task_id 时用。请求体照 /approvals/{id}/decide：approved、note，补充输入、改稿、双盲裁决和
 * 表单带 value；「始终允许」那里没有 always，走 /runs/{id}/resume 并带上 approval_id 指明是哪一条。老接口只当一票、
 * 一票就定，回成「已处理」
 */
async function decideLegacy(approval: Approval, task: Task, body: TaskRespond): Promise<{ task: Task; resolved: boolean }> {
  const approved = body.decision === 'approve' || body.decision === 'submit'
  const values = body.values ?? {}
  const note = body.note ?? ''
  if (approved && values.always === true) {
    await api.runs.resume(approval.run_id, { approved: true, always: true, note }, approval.id)
  } else {
    const keys = Object.keys(values)
    const value = keys.length === 1 && keys[0] === 'value' ? values.value : keys.length ? values : undefined
    await api.approvals.decide(approval.id, {
      approved, note, ...(approved && value !== undefined && task.kind !== 'tool' ? { value } : {}),
      ...(task.kind === 'tool' && values.args && typeof values.args === 'object' ? { args: values.args } : {}),
    })
  }
  return {
    task: { ...task, status: 'resolved', can_respond: false, outcome: { decision: body.decision } },
    resolved: true,
  }
}

// Timeline / OutputView / describe / EVENT_META / IssuanceBanner / RawEvents
// 都搬走了：
//   - "事件 → 人话"归 run/decode.ts，这里曾经有一套平行的 EVENT_META + describe()，
//     和解码器各译各的。留着两套，同一次运行在画布和问数据页会读出不同的故事，
//     而用户没法判断哪个是真的。
//   - 怎么画归 run/AssistantStream.tsx（含出具横幅），怎么组装归 run/AssistantPanel.tsx。
// 这个文件现在只剩一样东西：审批卡的包装（内容在 run/TaskCard）。

export { StatusDot, NODE_DEFS }
