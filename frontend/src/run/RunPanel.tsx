import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { useStudio } from '../store/studio'
import { useCatalog } from '../store/catalog'
import { NODE_DEFS } from '../canvas/nodeDefs'
import { Skeleton, StatusDot } from '../components/ui'
import { errorMessage } from '../lib/errors'
import { TASK_LOG_TEXT } from '../lib/terms'
import { TaskCard } from './TaskCard'
import type { Approval } from '../types'
import type { Task } from '../typesApp'

// 发起运行的表单搬去了 run/RunControl.tsx 的工具栏控件里。它原先常驻在
// 助手栏顶部，和 Copilot 输入框两个"主要动作"互相压着——而它们是两种不同的
// 意图（跑这张图 / 改这张图），不该争同一块地方。运行是对整张图的动作，
// 和保存、发布同类，属于工具栏。

/**
 * 审批卡：现在只是一层包装（X4）。审批卡的内容——报告核对概况、反方审稿、双盲复算、质询、动作确认的哈希卡、
 * 核实选项、会签进度——都搬进了 run/TaskCard，所有人的决定都经待办服务（/api/tasks/{id}/respond）处理。
 *
 * 这里只做两件事：按审批找到它对应的那条待办（一次中断对应一条 Approval 和一条 Task），以及回复之后的接流——
 * onResolved 给了就听它的（对话页、运行页各自重新接流），没给就是画布：恢复后由 studio 重新接上事件流。
 * 只有凑齐了（resolved）运行才会恢复；会签里记下一票时运行还停着，不接流。
 */
export function ApprovalCard({ approval, onResolved }: {
  approval: Approval | any
  onResolved?: (runId: string) => void | Promise<void>
  /** 老调用方传的：卡头写不写工作流名现在由 TaskCard 自己定（待办上带着工作流名），这里不再用它 */
  showWorkflow?: boolean
}) {
  const refreshApprovals = useCatalog((s) => s.refreshApprovals)
  const attachRun = useStudio((s) => s.attachRun)
  const found = useApprovalTask(approval)
  if (found.state === 'loading') return <div className="border-b p-3"><Skeleton rows={2} height={20} /></div>
  if (found.state === 'error') {
    return (
      <div className="border-b p-3 text-2xs" data-approval={approval.id} data-approval-task="missing">
        <span style={{ color: 'var(--st-failed)' }}>{TASK_LOG_TEXT.loadFailed}{found.message ? `：${found.message}` : ''}</span>
        <button type="button" className="btn btn-sm ml-2" onClick={found.reload}>{TASK_LOG_TEXT.retry}</button>
      </div>
    )
  }
  return (
    <div data-approval={approval.id} data-task={found.task.id}>
      <TaskCard task={found.task} onResolved={async (result) => {
        await refreshApprovals()
        if (!result.resolved) return
        if (onResolved) await onResolved(approval.run_id)
        else await attachRun(approval.run_id)
      }} />
    </div>
  )
}

type ApprovalTask =
  | { state: 'loading' }
  | { state: 'ok'; task: Task }
  | { state: 'error'; message: string; reload: () => void }

/**
 * 一条审批对应的待办。审批带着 task（预览的桩数据）就直接用；带着 task_id（服务端在审批列表上给，D-r）按 id 取那一条；
 * 都没有（老服务端）才按运行取待办、按 approval_id 对上。升级时服务端为存量的待审批补建了待办，找不到说明服务端还没建
 * （旧版本）或者已经处理掉了
 */
function useApprovalTask(approval: Approval & { task?: Task }): ApprovalTask {
  const [state, setState] = useState<ApprovalTask>(approval.task ? { state: 'ok', task: approval.task } : { state: 'loading' })
  const [epoch, setEpoch] = useState(0)
  useEffect(() => {
    if (approval.task) { setState({ state: 'ok', task: approval.task }); return }
    let alive = true
    const reload = () => setEpoch((n) => n + 1)
    const found = approval.task_id
      ? api.tasks.get(approval.task_id)
      : api.tasks.list({ run_id: approval.run_id, assigned: 'all' })
        .then(({ items }) => items.find((t) => t.approval_id === approval.id) ?? null)
    found
      .then((task) => {
        if (!alive) return
        setState(task ? { state: 'ok', task } : { state: 'error', message: '', reload })
      })
      .catch((e) => { if (alive) setState({ state: 'error', message: errorMessage(e), reload }) })
    return () => { alive = false }
  }, [approval.id, approval.run_id, approval.task, approval.task_id, epoch])
  return state
}

// Timeline / OutputView / describe / EVENT_META / IssuanceBanner / RawEvents
// 都搬走了：
//   - "事件 → 人话"归 run/decode.ts，这里曾经有一套平行的 EVENT_META + describe()，
//     和解码器各译各的。留着两套，同一次运行在画布和问数据页会读出不同的故事，
//     而用户没法判断哪个是真的。
//   - 怎么画归 run/AssistantStream.tsx（含出具横幅），怎么组装归 run/AssistantPanel.tsx。
// 这个文件现在只剩一样东西：审批卡的包装（内容在 run/TaskCard）。

export { StatusDot, NODE_DEFS }
