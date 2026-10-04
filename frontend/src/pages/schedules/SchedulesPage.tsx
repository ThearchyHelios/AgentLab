import { CalendarClock } from 'lucide-react'
import { EmptyState, PageHeader } from '../../components/ui'
import { SCHEDULE_TEXT } from '../../lib/terms'

/**
 * 定时任务（/schedules、/schedules/:id）。现在是占位页，定时任务的工作包补上列表和详情。
 */
export function SchedulesPage() {
  return (
    <div className="flex h-full flex-col">
      <PageHeader icon={<CalendarClock size={13} />} title={SCHEDULE_TEXT.title} subtitle={SCHEDULE_TEXT.hint} />
      <div className="min-h-0 flex-1 overflow-y-auto">
        {/* 功能没做出来，和连没连上服务端无关：不走空态的离线说法 */}
        <EmptyState icon={<CalendarClock size={22} />} title={SCHEDULE_TEXT.pending}
                    body={SCHEDULE_TEXT.pendingHint} offline={false} />
      </div>
    </div>
  )
}
