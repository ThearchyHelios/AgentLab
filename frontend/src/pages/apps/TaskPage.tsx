/**
 * 待办详情（/apps/inbox/:taskId）：一张完整的待办卡（run/TaskCard）。处理之后重新取一次：会签没凑齐时卡上的进度跟着变，
 * 凑齐了卡就变成「已处理」，并给回待办箱的路。
 */
import { Inbox } from 'lucide-react'
import { useParams } from 'react-router-dom'
import { api } from '../../api/client'
import { PageHeader, Skeleton } from '../../components/ui'
import { TaskCard } from '../../run/TaskCard'
import { usePageTitle } from '../../lib/notify'
import { APP_HOME_TEXT, APP_TEXT, TASK_KIND_LABEL, TASK_TEXT } from '../../lib/termsApp'
import { BackLink, LoadError, useLoad } from './shared'

export function TaskPage() {
  const { taskId = '' } = useParams()
  const { data: task, error, reload } = useLoad((signal) => api.tasks.get(taskId, { signal }), [taskId])
  usePageTitle(task ? `${APP_TEXT.tabs.inbox} · ${task.title || TASK_TEXT.untitled}` : null)
  return (
    <div className="flex h-full flex-col" data-task-page={taskId}>
      <PageHeader icon={<Inbox size={14} />} title={task?.title || APP_TEXT.tabs.inbox}
                  subtitle={task ? [TASK_KIND_LABEL[task.kind], task.app_name && APP_HOME_TEXT.inApp(task.app_name)].filter(Boolean).join(' · ') : undefined} />
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-3xl space-y-3 p-4">
          <div className="text-2xs"><BackLink to="/apps?tab=inbox" label={APP_HOME_TEXT.backToInbox} /></div>
          {error && !task ? <LoadError error={error} onRetry={reload} notFound={APP_HOME_TEXT.taskNotFound} />
            : !task ? <Skeleton rows={6} />
            : (
              <div className="overflow-hidden rounded-lg border bg-panel">
                <TaskCard task={task} onResolved={reload} />
              </div>
            )}
        </div>
      </div>
    </div>
  )
}
