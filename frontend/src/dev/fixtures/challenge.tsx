import { useState, type JSX } from 'react'
import { ReactFlow, ReactFlowProvider } from '@xyflow/react'
import { Inspector } from '../../canvas/Inspector'
import { NodeCard } from '../../canvas/NodeCard'
import { ApprovalCard } from '../../run/RunPanel'
import { useCatalog } from '../../store/catalog'
import { useStudio } from '../../store/studio'
import type { Approval, ApprovalChallenge, ApprovalReport, Provider, Workflow } from '../../types'
import fx from './challenge.json'

/**
 * 反方审稿的预览用例，scripts/check-challenge.mjs 按它们检查。夹具在 challenge.json（检查脚本读同一份）：理由文档是后端
 * run_verify_agent + compose_doc 真跑出来的，库、表名、地名、数字都是虚构的。
 *
 * 审批卡（payload.reports[0].challenge）：
 * - ?syn=challenge-approval：正式运行，没写 on_refuted（按降档），四种判断各一句，有被推翻的 →「出具会降档」
 * - ?syn=challenge-approval-flag：正式运行，只标注，不写降档
 * - ?syn=challenge-approval-explore：探索运行，不说任何和出具有关的话
 * - ?syn=challenge-approval-none：审的报告没有经过反方审稿，什么都不画
 * - ?syn=challenge-approval-many：十二句，条目只列前十句，「显示全部」从工件里补齐
 * - ?syn=challenge-approval-broken：工件取不到（检查脚本先答 404，再改成能取到，测重试）
 * - ?syn=challenge-approval-dense：同第一个，摆在 360px 的右栏里
 *
 * 运行类别（GET /api/runs/{id}）、工件（GET /api/artifacts/{id}）都由检查脚本用 page.route 伪造；直接打开时取不到，
 * 卡上照实不说降档、点开一句说无法获取。
 *
 * 检查器：?syn=challenge-inspector：一张虚构的图（两个上游报告、一个下游报告），选中反方审稿节点，旁边是同一张图的
 * 节点卡片和节点配置的实时内容。
 */

const report = fx.report as unknown as ApprovalReport
const challenge = fx.challenge as unknown as ApprovalChallenge

function approvalOf(id: string, runId: string, runClass: 'formal' | 'exploratory', reviewed: ApprovalReport): Approval {
  return {
    id, run_id: runId, node_id: 'review', mode: 'approve', title: '本月月报可以发出吗？', status: 'pending', response: {},
    payload: { kind: 'human_node', message: '请审核本月月报。', reports: [reviewed] },
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

const RUNS = fx.runs
const withChallenge = (c: Partial<ApprovalChallenge>): ApprovalReport => ({ ...report, challenge: { ...challenge, ...c } })

// ---------------------------------------------------------------- 检查器

const PROVIDERS = [
  {
    id: 'pv-main', name: '主力接入', kind: 'anthropic', base_url: null, default_model: 'writer-large',
    models: [{ id: 'writer-large' }, { id: 'critic-large' }], enabled: true, extra: {}, api_key_masked: '', has_key: true,
  },
  {
    id: 'pv-spare', name: '备用接入', kind: 'openai', base_url: null, default_model: 'spare-mini',
    models: [{ id: 'spare-mini' }], enabled: false, extra: {}, api_key_masked: '', has_key: true,
  },
] as Provider[]

const at = (x: number, y: number) => ({ x, y })
const WORKFLOW = {
  id: 'wf-challenge-preview', name: '月报（预览）', description: '', tags: [], version: 1, is_template: false, status: 'draft',
  graph: {
    nodes: [
      { id: 'input', type: 'input', position: at(0, 120), data: { label: '输入', config: { fields: [{ name: 'month', required: true }] } } },
      { id: 'write', type: 'report', position: at(260, 0), data: { label: '写月报', config: { instructions: '写本月月报', model: 'writer-large' } } },
      { id: 'write2', type: 'report', position: at(260, 240), data: { label: '写周报', config: { instructions: '写本周周报' } } },
      { id: 'critic', type: 'challenge', position: at(540, 120), data: { label: '反方审稿', config: {} } },
      { id: 'later', type: 'report', position: at(820, 120), data: { label: '写摘要', config: { instructions: '写一段摘要' } } },
      { id: 'out', type: 'output', position: at(1100, 120), data: { label: '输出', config: {} } },
    ],
    edges: [
      { source: 'input', target: 'write' }, { source: 'input', target: 'write2' },
      { source: 'write', target: 'critic' }, { source: 'write2', target: 'critic' },
      { source: 'critic', target: 'later' }, { source: 'later', target: 'out' },
    ],
  },
} as unknown as Workflow

const nodeTypes = { card: NodeCard }

/** 同一张图的节点卡片：看反方审稿节点的摘要跟着配置变 */
function MiniCanvas() {
  const nodes = useStudio((s) => s.nodes)
  const edges = useStudio((s) => s.edges)
  return (
    <ReactFlowProvider>
      <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView nodesDraggable={false} proOptions={{ hideAttribution: true }} />
    </ReactFlowProvider>
  )
}

function ConfigView() {
  const config = useStudio((s) => s.nodes.find((n) => n.id === 'critic')?.data.config)
  return (
    <pre className="mono max-h-40 overflow-auto whitespace-pre-wrap border-t p-2 text-2xs text-dim [overflow-wrap:anywhere]"
         data-challenge-config="">
      {JSON.stringify(config ?? null, null, 1)}
    </pre>
  )
}

function InspectorCase() {
  // 首次渲染前装好图和接入：检查器、卡片一挂上就是选中反方审稿节点的样子
  useState(() => {
    useCatalog.setState({ providers: PROVIDERS })
    useStudio.getState().load(WORKFLOW)
    useStudio.getState().select('critic')
    return true
  })
  return (
    <div className="flex min-h-screen flex-col md:h-screen md:flex-row" data-fixture="challenge-inspector">
      <div className="w-full shrink-0 border-b bg-panel md:h-full md:w-[360px] md:border-b-0 md:border-r" data-challenge-inspector="">
        <Inspector />
      </div>
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <div className="h-72 min-h-0 md:h-auto md:flex-1"><MiniCanvas /></div>
        <ConfigView />
      </div>
    </div>
  )
}

export const cases: Record<string, () => JSX.Element> = {
  'challenge-approval': () => (
    <Card name="challenge-approval" approval={approvalOf('ap-chal-1', RUNS.formal, 'formal', withChallenge({}))} />
  ),
  'challenge-approval-flag': () => (
    <Card name="challenge-approval-flag"
          approval={approvalOf('ap-chal-2', RUNS.flag, 'formal', withChallenge({ on_refuted: 'flag' }))} />
  ),
  'challenge-approval-explore': () => (
    <Card name="challenge-approval-explore"
          approval={approvalOf('ap-chal-3', RUNS.explore, 'exploratory', withChallenge({ on_refuted: 'degrade' }))} />
  ),
  'challenge-approval-none': () => (
    <Card name="challenge-approval-none" approval={approvalOf('ap-chal-4', RUNS.none, 'formal', report)} />
  ),
  'challenge-approval-many': () => (
    <Card name="challenge-approval-many"
          approval={approvalOf('ap-chal-5', RUNS.many, 'formal', { ...report, challenge: fx.many as unknown as ApprovalChallenge })} />
  ),
  'challenge-approval-broken': () => (
    <Card name="challenge-approval-broken"
          approval={approvalOf('ap-chal-6', RUNS.broken, 'formal', withChallenge({ artifact: 'e'.repeat(64) }))} />
  ),
  'challenge-approval-dense': () => (
    <Card name="challenge-approval-dense" dense approval={approvalOf('ap-chal-7', RUNS.formal, 'formal', withChallenge({}))} />
  ),
  'challenge-inspector': () => <InspectorCase />,
}
