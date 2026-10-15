import { useState } from 'react'
import type { JSX } from 'react'
import { ReactFlow, ReactFlowProvider } from '@xyflow/react'
import { ApprovalCard } from '../../run/RunPanel'
import { withTask } from '../taskOf'
import { Inspector } from '../../canvas/Inspector'
import { NodeCard } from '../../canvas/NodeCard'
import { EvidencePane } from '../../pages/runs/EvidencePane'
import { toFlow, useStudio } from '../../store/studio'
import { useCatalog } from '../../store/catalog'
import type { Approval, GraphSpec, Provider, Run, RunEvent } from '../../types'
import data from './dual_check.json'

/**
 * 双盲复算的预览用例，scripts/check-verify.mjs 按它们检查（夹具在同目录的 dual_check.json，检查脚本读的是同一份）：
 * - ?syn=dual-check-approval：正式运行里两路对不上的审批卡，宽栏（两条 SQL 左右并排）
 * - ?syn=dual-check-approval-rail：同一张卡摆在 360px 的画布右栏里（两条 SQL 上下排）
 * - ?syn=dual-check-approval-exploratory：探索运行，后果里不提出具
 * - ?syn=dual-check-approval-empty：摘要里一个指标都没有（老服务端、写坏了的 payload），只能驳回
 * - ?syn=dual-check-inspector：一张小画布（真的节点卡片）加 360px 的检查器，选中双盲复算节点；检查器改的是
 *   studio store 里的节点配置，卡片上的摘要跟着变
 *
 * 完整对照的工件（GET /api/artifacts/{id}）、这次运行的图快照（GET /api/runs/{id}/graph，卡上按它说节点的
 * 「未采用原流程时」）、审批回复、设置、校验都由检查脚本用 page.route 伪造，直接打开时卡上照实说无法加载完整对照、
 * 有条件地说后果。提交后 onResolved 只把运行 id 记到 window.__dualResolved，不去接事件流。
 *
 * ?syn=dual-check-approval 另认地址上的几个参数（检查脚本用；不带时 payload 只有约定里的几项，同老服务端）：
 * - variant=<名字>：「销售额」的摘要换成 variants 里那一对 SQL 的（前 3 条差异的原话、是否只有兜底，同节点的写法），
 *   工件由检查脚本按同一个变体伪造
 * - peer=<名字>：「退款率」的摘要同样换成 peers 里那一对 SQL 的。peers 和「销售额」（或换上的变体）落到数据目录的
 *   同一项上，看同一次裁决里几个指标说法不同、要合写时卡上怎么说（夹具的 merges）
 * - catalog=1：摘要里每个指标带上服务端预告的「采用哪一边会往数据目录里写什么」（catalog_preview；换了变体、peers 的
 *   指标用它们自己的 catalog）。catalog=bad：「销售额」带一份认不出的预告。catalog=blocked：「销售额」带目录里已有人工
 *   确认的业务日期时的预告（catalog_blocked：两边都被挡下，各带写到哪和原因）
 * - merge=<名字>：两个指标的预告换成 merges 里那一种组合按它的目录算的（catalog；和 variant、peer 一起用，换上的 SQL
 *   照它的 gmv、refund_rate 写）。合起来的预告（joint）在完整对照里，由检查脚本按同一种组合伪造工件
 * - on_reject=<值>：payload 带上节点实际生效的「未采用原流程时」
 *
 * ?syn=verify-gaps：出具缺口里有双盲复算、反方审稿报上来的几条（两路结果不一致、结论被推翻、指标无法复算）和一条
 * 校验没做完的。上面是运行详情的证据页（真的 EvidencePane：证据图、审计接口由检查脚本伪造），下面是同一次运行的成果
 * 节点卡片（出具印章的悬停说明）：两处都把核查的缺口另起「核查未通过」「核查未完成」两组，不放在「校验未全部完成」下
 */
const approval = (patch: Partial<Approval> = {}): Approval => ({
  ...(data.approval as unknown as Approval),
  created_at: new Date(Date.now() - 180_000).toISOString().replace('Z', ''),
  ...patch,
})

interface Variant { diff: { items: { aspect: string; text: string }[] }; catalog: unknown }

function approvalOf(search: string): Approval {
  const q = new URLSearchParams(search)
  const variant = (data.variants as unknown as Record<string, Variant>)[q.get('variant') ?? '']
  const peer = (data.peers as unknown as Record<string, Variant>)[q.get('peer') ?? '']
  const catalog = q.get('catalog')
  const merge = (data.merges as unknown as Record<string, { catalog?: Record<string, unknown> }>)[q.get('merge') ?? '']
  const onReject = q.get('on_reject')
  const previews = data.catalog_preview as unknown as Record<string, unknown>
  const summary = data.approval.payload.summary.map((s) => {
    const swapped = s.metric_id === 'gmv' && variant ? variant : s.metric_id === 'refund_rate' && peer ? peer : null
    const items = swapped?.diff.items
    return {
      ...s,
      ...(items ? { diff: items.slice(0, 3).map((i) => i.text), fallback: !items.some((i) => i.aspect !== 'other' && i.aspect !== 'parse') } : {}),
      ...(catalog === '1' ? { catalog: swapped ? swapped.catalog : previews[s.metric_id] } : {}),
      ...(catalog === 'bad' && s.metric_id === 'gmv' ? { catalog: { a: { write: true, table: 'orders' }, b: { write: false } } } : {}),
      ...(catalog === 'blocked' && s.metric_id === 'gmv' ? { catalog: data.catalog_blocked.catalog } : {}),
      ...(merge?.catalog?.[s.metric_id] ? { catalog: merge.catalog[s.metric_id] } : {}),
    }
  })
  return approval({ payload: { ...data.approval.payload, summary, ...(onReject ? { on_reject: onReject } : {}) } })
}

function Card({ value }: { value: Approval }) {
  return (
    <div className="overflow-hidden rounded-lg border" style={{ borderColor: 'var(--warn)' }}>
      <ApprovalCard approval={withTask(value)} onResolved={(id) => { (window as any).__dualResolved = id }} />
    </div>
  )
}

const nodeTypes = { card: NodeCard }

/**
 * 检查器用例：第一次渲染前把夹具里的图放进 studio store、选中双盲复算节点，模型接入直接写进目录 store（不走接口）。
 * 用例都在一个预览页里预先加载，store 只能在这里（用到时）改，不能在模块顶层改
 */
function InspectorCase() {
  useState(() => {
    const { nodes, edges } = toFlow(data.graph as unknown as GraphSpec)
    useStudio.setState({ nodes, edges, selectedId: 'dc' })
    useCatalog.setState({ providers: data.providers as unknown as Provider[] })
    return true
  })
  const nodes = useStudio((s) => s.nodes)
  const edges = useStudio((s) => s.edges)
  return (
    <div className="flex h-full min-h-0 max-w-full" data-fixture="dual-check-inspector">
      <div className="min-w-0 flex-1 max-sm:hidden" data-dual-canvas="">
        <ReactFlowProvider>
          <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView nodesDraggable={false} nodesConnectable={false}
                     proOptions={{ hideAttribution: true }} />
        </ReactFlowProvider>
      </div>
      <div className="h-full w-[360px] max-w-full shrink-0 overflow-hidden border-l bg-panel" data-dual-inspector="">
        <Inspector />
      </div>
    </div>
  )
}

/** verify-gaps 用例的运行和出具明细（库、指标、地名都是虚构的） */
const VERIFY_GAPS = {
  run: 'run-verify-gaps',
  gaps: {
    failed: ['双盲复算「复核周报口径」：指标「销售额」两路结果不一致，审核人选择了盲算结果', '反方审稿：推翻了 1 句结论：「东区增长主要来自新客」'],
    unfinished: ['双盲复算「复核周报口径」：指标「订单数」无法复算：输入来自合并查询，无法单独复算'],
    other: ['叙述模板渲染为空（路径可能有误）'],
  },
}
const GAPS = VERIFY_GAPS.gaps
const GAPS_ISSUANCE = {
  tier: 'degraded', matched_numbers: 3, metrics_checked: 2, missing_required: [], missing_expected: [], unmatched_numbers: [],
  // 缺口的先后照服务端（出具契约按上游节点的先后收）：核查的和别的混在一起，分组靠前缀认
  gaps: [GAPS.failed[0], GAPS.other[0], GAPS.unfinished[0], GAPS.failed[1]],
}
const GAPS_RUN: Run = {
  id: VERIFY_GAPS.run, workflow_id: null, workflow_name: '周报（预览）', status: 'succeeded', input: {},
  output: { _issuance: GAPS_ISSUANCE }, error: null, usage: {} as Run['usage'], run_class: 'formal',
}
const GAPS_GRAPH = {
  nodes: [
    { id: 'dc', type: 'dual_check', position: { x: 0, y: 40 }, data: { label: '复核周报口径', config: {} } },
    { id: 'done', type: 'output', position: { x: 300, y: 40 }, data: { label: '成果', config: {} } },
  ],
  edges: [{ id: 'g1', source: 'dc', target: 'done' }],
} as unknown as GraphSpec

/**
 * 证据页和成果节点卡片：图放进 studio store，再按一次运行的事件灌进去（出具事件带着完整的明细，卡片的印章按它画、
 * 悬停说明按它写）。只在这个用例渲染时改 store
 */
function VerifyGapsCase() {
  useState(() => {
    const { nodes, edges } = toFlow(GAPS_GRAPH)
    const st = useStudio.getState()
    st.clearRun()
    useStudio.setState({ nodes, edges, selectedId: null, run: { ...GAPS_RUN, status: 'running', output: {} }, runSnapshot: null })
    const at = Date.now() / 1000 - 5
    const events: RunEvent[] = [
      { seq: 1, type: 'run.started', node_id: null, ts: at, data: { nodes: 2 } },
      { seq: 2, type: 'node.started', node_id: 'dc', ts: at + 0.1, data: {} },
      { seq: 3, type: 'node.finished', node_id: 'dc', ts: at + 1, data: { duration_ms: 900 } },
      { seq: 4, type: 'node.started', node_id: 'done', ts: at + 1.1, data: {} },
      { seq: 5, type: 'issuance', node_id: 'done', ts: at + 1.2, data: { ...GAPS_ISSUANCE, unmatched: 0 } },
      { seq: 6, type: 'node.finished', node_id: 'done', ts: at + 1.3, data: { duration_ms: 200 } },
      { seq: 7, type: 'run.finished', node_id: null, ts: at + 1.4, data: { output: { _issuance: GAPS_ISSUANCE }, duration_ms: 1400 } },
    ]
    for (const e of events) useStudio.getState().applyEvent(e)
    return true
  })
  const nodes = useStudio((s) => s.nodes)
  const edges = useStudio((s) => s.edges)
  return (
    <div className="flex min-h-screen max-w-full flex-col" data-fixture="verify-gaps">
      <div className="h-[520px] min-h-0 border-b" data-verify-gaps-evidence="">
        <EvidencePane run={GAPS_RUN} output={GAPS_RUN.output} labelOf={() => undefined} refreshKey="" />
      </div>
      <div className="h-64 min-h-0 min-w-0" data-verify-gaps-canvas="">
        <ReactFlowProvider>
          <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView nodesDraggable={false} nodesConnectable={false}
                     proOptions={{ hideAttribution: true }} />
        </ReactFlowProvider>
      </div>
    </div>
  )
}

export const cases: Record<string, () => JSX.Element> = {
  'dual-check-approval': () => (
    <div className="mx-auto max-w-3xl p-4 text-sm" data-fixture="dual-check-approval"><Card value={approvalOf(location.search)} /></div>
  ),
  'dual-check-approval-rail': () => (
    <div className="h-full w-[360px] max-w-full overflow-y-auto border-r bg-panel" data-fixture="dual-check-approval-rail">
      <Card value={approval()} />
    </div>
  ),
  'dual-check-approval-exploratory': () => (
    <div className="mx-auto max-w-3xl p-4 text-sm" data-fixture="dual-check-approval-exploratory">
      <Card value={approval({ run_class: 'exploratory' })} />
    </div>
  ),
  'dual-check-approval-empty': () => (
    <div className="mx-auto max-w-3xl p-4 text-sm" data-fixture="dual-check-approval-empty">
      <Card value={approval({ payload: { ...data.approval.payload, summary: [] } })} />
    </div>
  ),
  'dual-check-inspector': () => <InspectorCase />,
  'verify-gaps': () => <VerifyGapsCase />,
}
