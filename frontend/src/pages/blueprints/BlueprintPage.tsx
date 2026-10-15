import { useParams } from 'react-router-dom'
import { EmptyState } from '../../components/ui'
import { BLUEPRINT_TEXT } from '../../lib/terms'

/**
 * 蓝图页（docs/design/normal-user-apps.md §4.2–§4.4）：左边对话、右边卡片；试运行；发布摘要（搭建者）/ 存成我的应用（使用者）。
 * 骨架阶段是占位，包 F5 实现。
 */
export function BlueprintPage() {
  const { blueprintId } = useParams()
  return <EmptyState title={BLUEPRINT_TEXT.title} body={blueprintId ?? ''} />
}
