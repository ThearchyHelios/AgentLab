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
 * - ?syn=dual-check-inspector：一张小画布（真的节点卡片）加 360px 的检查器，选中双盲复算节点；检查器改的是
 *   studio store 里的节点配置，卡片上的摘要跟着变
 *
 * 完整对照的工件（GET /api/artifacts/{id}）、审批回复、设置、校验都由检查脚本用 page.route 伪造，直接打开时卡上照实说
 * 无法加载完整对照。提交后 onResolved 只把运行 id 记到 window.__dualResolved，不去接事件流
 */
const approval = (patch: Partial<Approval> = {}): Approval => ({
  ...(data.approval as unknown as Approval),
  created_at: new Date(Date.now() - 180_000).toISOString().replace('Z', ''),
  ...patch,
})

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
    <div className="mx-auto max-w-3xl p-4 text-sm" data-fixture="dual-check-approval"><Card value={approval()} /></div>
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
  'dual-check-inspector': () => <InspectorCase />,
}
