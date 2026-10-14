/** 应用编辑：固定版本、派生、页面树、属性面板、预览、检查、发布、岗位与授权、钩子、助手（契约骨架里的占位页，FE-6 实现） */
import { LayoutGrid } from 'lucide-react'
import { EmptyState, PageHeader } from '../../../components/ui'
import { APP_TEXT } from '../../../lib/terms'

export function AppEditor() {
  return (
    <div className="flex h-full flex-col">
      <PageHeader icon={<LayoutGrid size={14} />} title={APP_TEXT.title} />
      <EmptyState title={APP_TEXT.pending} />
    </div>
  )
}
