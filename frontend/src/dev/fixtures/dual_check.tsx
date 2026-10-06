import { useState } from 'react'
import type { JSX } from 'react'
import { ReactFlow, ReactFlowProvider } from '@xyflow/react'
import { ApprovalCard } from '../../run/RunPanel'
import { Inspector } from '../../canvas/Inspector'
import { NodeCard } from '../../canvas/NodeCard'
import { toFlow, useStudio } from '../../store/studio'
import { useCatalog } from '../../store/catalog'
import type { Approval, GraphSpec, Provider } from '../../types'
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
      <ApprovalCard approval={value} onResolved={(id) => { (window as any).__dualResolved = id }} />
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
}
