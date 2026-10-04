import { GitCompareArrows } from 'lucide-react'
import { EmptyState, PageHeader } from '../../components/ui'
import { RUN_COMPARE_TEXT } from '../../lib/terms'

/**
 * 运行对比（/runs/compare）。现在是占位页，运行对比的工作包补上内容。
 *
 * 挂在 /runs 下面，导航照旧点亮「记录」；路由表里排在 /runs/:runId 之前，「compare」不会被当成运行 id。
 */
export function ComparePage() {
  return (
    <div className="flex h-full flex-col">
      <PageHeader icon={<GitCompareArrows size={13} />} title={RUN_COMPARE_TEXT.title} subtitle={RUN_COMPARE_TEXT.hint} />
      <div className="min-h-0 flex-1 overflow-y-auto">
        {/* 功能没做出来，和连没连上服务端无关：不走空态的离线说法 */}
        <EmptyState icon={<GitCompareArrows size={22} />} title={RUN_COMPARE_TEXT.pending}
                    body={RUN_COMPARE_TEXT.pendingHint} offline={false} />
      </div>
    </div>
  )
}
