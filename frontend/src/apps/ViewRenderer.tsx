/**
 * 视图渲染器：按服务端解析好的视图模型（agentlab.view/1）渲染应用页（FE-5 实现）。
 *
 * 前端只有一张「组件名 → 渲染器」的表；不认识的组件显示「此组件需要更新前端」，不报错、不空白。应用编辑器的预览
 * （FE-6）也用它：传 draft 解析出的视图模型即可。契约骨架里只有接口和最简实现，props 不变。
 */
import type { ViewModel, ViewNode } from '../typesApp'

export interface ViewRendererProps {
  model: ViewModel
  appId: string
  /** 实例页传入：组件里的待办、动作、进度按这个实例解析 */
  runId?: string
  /** 预览：不发起运行、不提交表单，点数字照样能看出处 */
  preview?: boolean
  /** 页内跳转（页签、页面链接） */
  onNavigate?: (pageId: string) => void
  /** 视图刷新、实例发起之后要重新取视图模型 */
  onReload?: () => void
}

export function ViewRenderer({ model }: ViewRendererProps) {
  const walk = (nodes: ViewNode[]) => nodes.map((n) => (
    <div key={n.id} data-component={n.type}>{n.children && walk(n.children)}</div>
  ))
  return <div>{walk(model.tree)}</div>
}
