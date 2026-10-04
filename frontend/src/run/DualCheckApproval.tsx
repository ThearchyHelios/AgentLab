import type { Approval } from '../types'

export interface DualCheckApprovalProps {
  /** payload.kind 为 'dual_check' 的审批：双盲复算两路对不上、交给人裁决 */
  approval: Approval
}

/**
 * 审批卡里双盲复算那一块（RunPanel 的 ApprovalCard 在 payload.kind === 'dual_check' 时渲染它）。
 *
 * 现在是占位，什么都不画；双盲复算的工作包在这里补上两路结果的对照，payload 的形状由那边定。
 */
export function DualCheckApproval(_props: DualCheckApprovalProps) {
  return null
}
