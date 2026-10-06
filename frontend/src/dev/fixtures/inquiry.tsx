import { useState, type JSX } from 'react'
import { createPortal } from 'react-dom'
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
 *   每句结论后面有「质询」图标（悬停 / 焦点在这一句上时显出来），划选原文时浮出「质询」
 * - ?syn=inquiry-approval-formal：正式运行的审批，同样能问（审批阶段还没封存）
 * - ?syn=inquiry-approval-dense：同第一个，摆在 360px 的右栏里
 * - ?syn=inquiry-approval-done：审批已经处理过（不是待处理的那张）：只列问答，不给提问
 * - ?syn=inquiry-approval-nodoc：审的报告没有逐段证据（没有报告文档）：只能就整份报告提问
 * - ?syn=inquiry-approval-twice：同一张待审批卡同时挂在两处，同编排页里画布的审批浮层（挂到页面末尾、role=dialog，
 *   React 树里排在前面）和右栏；「收起浮层」按钮开关浮层
 * - ?syn=inquiry-approval-steps：同一份报告先后两张审批（主管审批 → 总监审批），「处理掉这张」换下一张；卡按审批 id
 *   重新挂（同右栏）。?syn=inquiry-approval-steps-same：同一个卡片实例换审批（同画布的审批浮层，不按 id 重新挂）
 *
 * 运行详情的「证据」页签（pages/runs/EvidencePane，是否可问由它按运行状态判断）：
 * - ?syn=inquiry-run-explore：封存了的探索运行，报告上给入口，问答追加在封存之后
 * - ?syn=inquiry-run-formal：封存了的正式运行，不给入口，审批时问过的照列
 *
 * 列表、提问、质询工件、报告文档、证据图、审批列表、设置都由检查脚本用 page.route 伪造；直接打开时问答串照实说无法读取。
 */

const RUNS = fx.runs
const reports = fx.reports as unknown as Record<keyof typeof RUNS, ApprovalReport>

function approvalOf(id: string, runId: string, runClass: 'formal' | 'exploratory', report: ApprovalReport,
  status = 'pending', label = '主管审批'): Approval {
  return {
    id, run_id: runId, node_id: 'review', mode: 'approve', title: '9 月月报可以发出吗？', status, response: {},
    payload: { kind: 'human_node', message: '请审核 9 月月报。', reports: [report] },
    created_at: new Date(Date.now() - 300_000).toISOString().replace('Z', ''),
    workflow_name: '月报', node_label: label, run_class: runClass,
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

/** 同一张待审批卡挂两处：浮层挂到页面末尾（同 canvas/NodeCard 的审批浮层），右栏在页面里 */
function TwoPlaces({ approval }: { approval: Approval }) {
  const [pop, setPop] = useState(true)
  return (
    <div className="flex min-h-screen" data-fixture="inquiry-approval-twice">
      {pop && createPortal(
        <div role="dialog" aria-label="审批卡" data-fixture-pop=""
             className="fixed left-4 top-4 z-50 max-h-[90vh] w-[380px] overflow-y-auto rounded-lg border bg-panel shadow-elev-3">
          <ApprovalCard approval={approval} showWorkflow={false} />
        </div>,
        document.body,
      )}
      <div className="flex-1 p-4">
        <button type="button" className="btn btn-xs ml-[400px]" data-fixture-toggle="" onClick={() => setPop((v) => !v)}>
          {pop ? '收起浮层' : '打开浮层'}
        </button>
      </div>
      <aside className="w-[380px] shrink-0 border-l bg-panel" data-fixture-panel="">
        <ApprovalCard approval={approval} showWorkflow={false} />
      </aside>
    </div>
  )
}

/** 同一份报告先后两张审批：「处理掉这张」之后换下一张（keyed：按审批 id 重新挂，同右栏；否则同一个卡片实例换审批） */
function Steps({ name, keyed }: { name: string; keyed: boolean }) {
  const list = [
    approvalOf(fx.approval_id, RUNS.approve, 'exploratory', reports.approve),
    approvalOf('ap-inq-3', RUNS.approve, 'exploratory', reports.approve, 'pending', '总监审批'),
  ]
  const [at, setAt] = useState(0)
  const approval = list[Math.min(at, list.length - 1)]
  return (
    <div className="mx-auto max-w-3xl p-4" data-fixture={name}>
      <button type="button" className="btn btn-xs mb-2" data-fixture-next="" onClick={() => setAt((n) => n + 1)}>处理掉这张</button>
      <div className="overflow-hidden rounded-lg border" style={{ borderColor: 'var(--warn)' }}>
        {keyed
          ? <ApprovalCard key={approval.id} approval={approval} onResolved={() => undefined} />
          : <ApprovalCard approval={approval} onResolved={() => undefined} />}
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
  'inquiry-approval-nodoc': () => (
    <Card name="inquiry-approval-nodoc" approval={approvalOf(fx.approval_id, RUNS.approve, 'exploratory',
      { ...reports.approve, doc_artifact: undefined } as ApprovalReport)} />
  ),
  'inquiry-approval-twice': () => (
    <TwoPlaces approval={approvalOf(fx.approval_id, RUNS.approve, 'exploratory', reports.approve)} />
  ),
  'inquiry-approval-steps': () => <Steps name="inquiry-approval-steps" keyed />,
  'inquiry-approval-steps-same': () => <Steps name="inquiry-approval-steps-same" keyed={false} />,
  'inquiry-run-explore': () => <RunEvidence name="inquiry-run-explore" run={runOf(RUNS.explore, 'exploratory')} />,
  'inquiry-run-formal': () => <RunEvidence name="inquiry-run-formal" run={runOf(RUNS.sealed, 'formal')} />,
}
