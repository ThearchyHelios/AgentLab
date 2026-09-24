import { useCallback, useEffect, useMemo, useRef } from 'react'
import {
  Background, BackgroundVariant, Controls, MiniMap, ReactFlow, ReactFlowProvider,
  useReactFlow, type Edge,
} from '@xyflow/react'
import { FlowEdge, RoutingContext } from './FlowEdge'
import { NodeCard } from './NodeCard'
import { buildRoutes } from './routing'
import { useStudio } from '../store/studio'
import type { NodeType } from '../types'

const nodeTypes = { card: NodeCard }
const edgeTypes = { flow: FlowEdge }

const NODE_COLOR: Record<string, string> = {
  input: '#3fb950', output: '#a371f7', llm: '#4f8cff', agent: '#6366f1',
  supervisor: '#db61a2', tool: '#f0883e', code: '#2dd4bf', branch: '#d29922',
  loop: '#ec4899', human: '#f85149', validate: '#84cc16', memory: '#14b8a6',
  retrieve: '#0ea5e9', transform: '#8b949e', subgraph: '#bc8cff', metrics: '#34d399',
}

function CanvasInner() {
  const wrapper = useRef<HTMLDivElement>(null)
  const { screenToFlowPosition, fitView } = useReactFlow()
  const nodes = useStudio((s) => s.nodes)
  const edges = useStudio((s) => s.edges)
  const activeEdges = useStudio((s) => s.activeEdges)
  const fitRequest = useStudio((s) => s.fitRequest)
  const { onNodesChange, onEdgesChange, onConnect, addNode, select } = useStudio()

  // 走线方案整张图算一次。节点尺寸要等 React Flow 量完才有，量到之后
  // nodes 会变，这里跟着重算，端口和车道就落到实测尺寸上。
  const routes = useMemo(() => buildRoutes(nodes, edges), [nodes, edges])

  // 正在流动的边加动画，让"数据走到哪了"看得见
  const decorated = useMemo<Edge[]>(
    () =>
      edges.map((e) =>
        activeEdges.includes(e.id)
          ? { ...e, type: 'flow', className: 'edge-active', animated: true }
          : { ...e, type: 'flow', className: undefined, animated: false },
      ),
    [edges, activeEdges],
  )

  // 自动排版 / Copilot 生成完之后把视角拉回图上。位置全变了却不重新取景，
  // 人会盯着一片空白以为图没了。
  const firstFit = useRef(true)
  useEffect(() => {
    if (firstFit.current) {
      firstFit.current = false
      return
    }
    void fitView({ padding: 0.14, maxZoom: 1.1, duration: 280 })
  }, [fitRequest, fitView])

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault()
      const type = event.dataTransfer.getData('application/agentlab-node') as NodeType
      if (!type) return
      addNode(type, screenToFlowPosition({ x: event.clientX, y: event.clientY }))
    },
    [addNode, screenToFlowPosition],
  )

  return (
    <div className="h-full w-full" ref={wrapper}>
      <RoutingContext.Provider value={routes}>
        <ReactFlow
          nodes={nodes}
          edges={decorated}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          onNodeClick={(_, node) => select(node.id)}
          onPaneClick={() => select(null)}
          onDrop={onDrop}
          onDragOver={(e) => {
            e.preventDefault()
            e.dataTransfer.dropEffect = 'move'
          }}
          fitView
          fitViewOptions={{ padding: 0.12, maxZoom: 1.1, minZoom: 0.5 }}
          minZoom={0.2}
          maxZoom={2}
          defaultEdgeOptions={{ type: 'flow' }}
          proOptions={{ hideAttribution: true }}
          deleteKeyCode={['Backspace', 'Delete']}
        >
          <Background variant={BackgroundVariant.Dots} gap={18} size={1} color="var(--canvas-dot)" />
          <Controls showInteractive={false} />
          <MiniMap
            pannable
            zoomable
            maskColor="color-mix(in srgb, var(--bg) 82%, transparent)"
            nodeColor={(n) => NODE_COLOR[(n.data as any)?.nodeType as string] ?? '#666'}
            style={{ width: 140, height: 96 }}
          />
        </ReactFlow>
      </RoutingContext.Provider>
    </div>
  )
}

export function FlowCanvas() {
  return (
    <ReactFlowProvider>
      <CanvasInner />
    </ReactFlowProvider>
  )
}
