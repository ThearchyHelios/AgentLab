import { EmptyState } from '../../components/ui'
import { NEW_APP_TEXT } from '../../lib/terms'

/**
 * 新建应用的三个起点（docs/design/normal-user-apps.md §4.1）：说说你要什么、从场景模板开始、从已有的开始。
 * 骨架阶段是占位，包 F5 实现。
 */
export function NewAppStart() {
  return <EmptyState title={NEW_APP_TEXT.title} body="" />
}
