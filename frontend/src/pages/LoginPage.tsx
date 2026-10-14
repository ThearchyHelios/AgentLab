/** 登录页（开登录时）（契约骨架里的占位页，FE-5 实现） */
import { LayoutGrid } from 'lucide-react'
import { EmptyState, PageHeader } from '../components/ui'
import { APP_TEXT } from '../lib/terms'

export function LoginPage() {
  return (
    <div className="flex h-full flex-col">
      <PageHeader icon={<LayoutGrid size={14} />} title={APP_TEXT.title} />
      <EmptyState title={APP_TEXT.pending} />
    </div>
  )
}
