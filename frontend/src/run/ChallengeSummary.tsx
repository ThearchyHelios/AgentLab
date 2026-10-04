import type { ApprovalReport } from '../types'

export interface ChallengeSummaryProps {
  /** 这张审批卡审的报告（payload.reports，和 ApprovalReports 拿到的是同一份） */
  reports: ApprovalReport[]
  runId: string
}

/**
 * 审批卡里反方审稿的摘要，紧跟在报告核对概况（ApprovalReports）后面。
 *
 * 现在是占位，什么都不画；反方审稿的工作包在这里补上反驳意见的摘要。审的不是报告、或者报告没经过
 * 反方审稿时也应该什么都不画，卡片和以前一样。
 */
export function ChallengeSummary(_props: ChallengeSummaryProps) {
  return null
}
