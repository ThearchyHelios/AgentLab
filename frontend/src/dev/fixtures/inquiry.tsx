import type { JSX } from 'react'
import { EvidencePane } from '../../pages/runs/EvidencePane'
import { ApprovalCard } from '../../run/RunPanel'
import type { Approval, ApprovalReport, Run } from '../../types'
import fx from './inquiry.json'

/**
 * 审批质询的预览用例，scripts/check-inquiry.mjs 按它们检查。夹具在 inquiry.json（检查脚本读同一份）：报告文档和回答文档
 * 是后端 build_catalog + compose_doc 真实组装的，超市、片区、金额都是虚构的。
 *
 * 审批卡（运行停在人工审批上，审的是报告「写月报」）：
 * - ?syn=inquiry-approval：探索运行。核对概况下面是问答串（进入时按列表接口恢复）和「就整份报告提问」；展开逐段证据后，
 *   每句结论悬停 / 焦点在它上面时出现「质询」，划选原文时浮出「质询」
 * - ?syn=inquiry-approval-formal：正式运行的审批，同样能问（审批阶段还没封存）
 * - ?syn=inquiry-approval-dense：同第一个，摆在 360px 的右栏里
 * - ?syn=inquiry-approval-done：审批已经处理过（不是待处理的那张）：只列问答，不给提问
 *
 * 运行详情的「证据」页签（pages/runs/EvidencePane，是否可问由它按运行状态判断）：
 * - ?syn=inquiry-run-explore：封存了的探索运行，报告上给入口，问答追加在封存之后
 * - ?syn=inquiry-run-formal：封存了的正式运行，不给入口
 *
 * 列表、提问、质询工件、报告文档、证据图都由检查脚本用 page.route 伪造；直接打开时问答串照实说无法读取。
 */

const RUNS = fx.runs
const reports = fx.reports as unknown as Record<keyof typeof RUNS, ApprovalReport>

function approvalOf(id: string, runId: string, runClass: 'formal' | 'exploratory', report: ApprovalReport,
  status = 'pending'): Approval {
  return {
    id, run_id: runId, node_id: 'review', mode: 'approve', title: '9 月月报可以发出吗？', status, response: {},
    payload: { kind: 'human_node', message: '请审核 9 月月报。', reports: [report] },
    created_at: new Date(Date.now() - 300_000).toISOString().replace('Z', ''),
    workflow_name: '月报', node_label: '主管审批', run_class: runClass,
  }
}

function Card({ approval, name, dense = false }: { approval: Approval; name: string; dense?: boolean }) {
  return (
    <div className={dense ? 'w-[360px] max-w-full border-r bg-panel' : 'mx-auto max-w-3xl p-4'} data-fixture={name}>
      <div className="overflow-hidden rounded-lg border" style={{ borderColor: 'var(--warn)' }}>
        <ApprovalCard approval={approval} onResolved={() => undefined} />
      </div>
    </div>
  )
}

/** 运行详情：一次已经结束、封存了的运行 */
function runOf(id: string, runClass: 'formal' | 'exploratory'): Run {
  return {
    id, workflow_id: null, workflow_name: '月报', status: 'succeeded', input: { month: '2026-09' }, output: {}, error: null,
    usage: { input_tokens: 0, output_tokens: 0, cost_usd: 0 } as Run['usage'], run_class: runClass, version: runClass === 'formal' ? 3 : null,
    manifest_hash: 'f'.repeat(64), manifest_seq: 30, created_at: '2026-10-05T06:00:00', started_at: '2026-10-05T06:00:00',
    finished_at: '2026-10-05T06:40:00',
  }
}

const LABELS: Record<string, string> = { write: '写月报', review: '主管审批' }

function RunEvidence({ run, name }: { run: Run; name: string }) {
  return (
    <div className="h-screen min-w-0" data-fixture={name}>
      <EvidencePane run={run} output={null} labelOf={(id) => (id ? LABELS[id] : undefined)} refreshKey="0" />
    </div>
  )
}

export const cases: Record<string, () => JSX.Element> = {
  'inquiry-approval': () => (
    <Card name="inquiry-approval" approval={approvalOf(fx.approval_id, RUNS.approve, 'exploratory', reports.approve)} />
  ),
  'inquiry-approval-formal': () => (
    <Card name="inquiry-approval-formal" approval={approvalOf(fx.formal_approval_id, RUNS.formal, 'formal', reports.formal)} />
  ),
  'inquiry-approval-dense': () => (
    <Card name="inquiry-approval-dense" dense approval={approvalOf(fx.approval_id, RUNS.approve, 'exploratory', reports.approve)} />
  ),
  'inquiry-approval-done': () => (
    <Card name="inquiry-approval-done"
          approval={approvalOf(fx.approval_id, RUNS.approve, 'exploratory', reports.approve, 'approved')} />
  ),
  'inquiry-run-explore': () => <RunEvidence name="inquiry-run-explore" run={runOf(RUNS.explore, 'exploratory')} />,
  'inquiry-run-formal': () => <RunEvidence name="inquiry-run-formal" run={runOf(RUNS.sealed, 'formal')} />,
}
