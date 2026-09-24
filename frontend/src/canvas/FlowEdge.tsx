import { createContext, useContext } from 'react'
import { BaseEdge, EdgeLabelRenderer, getSmoothStepPath, type EdgeProps } from '@xyflow/react'
import type { Route } from './routing'

/** 全画布的走线方案，由 FlowCanvas 算一次、所有边共用。 */
export const RoutingContext = createContext<Map<string, Route> | null>(null)

/**
 * 画布上的边。
 *
 * React Flow 自带的 smoothstep 把竖直段一律摆在源和目标的正中间，同一列的
 * 边就会全部重合。路径改由 `routing.ts` 统一算（端口错开 + 走廊车道），
 * 这里只负责画。
 *
 * 拿不到路径时（还没量到节点尺寸、边指向了不存在的节点）退回自带的
 * smoothstep——宁可难看一帧，也不要线消失。
 */
export function FlowEdge({
  id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition,
  label, labelStyle, markerEnd, markerStart, interactionWidth, style,
}: EdgeProps) {
  const routes = useContext(RoutingContext)
  const route = routes?.get(id)

  const [fallback] = getSmoothStepPath({
    sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition,
  })
  const path = route?.path || fallback

  return (
    <>
      <BaseEdge
        path={path}
        style={style}
        markerEnd={markerEnd}
        markerStart={markerStart}
        interactionWidth={interactionWidth}
      />
      {label != null && label !== '' && (
        <EdgeLabelRenderer>
          <div
            className="pointer-events-none absolute whitespace-nowrap rounded bg-elev px-1 text-[10px] text-dim"
            style={{
              transform: `translate(-50%, -50%) translate(${route?.labelX ?? sourceX}px, ${route?.labelY ?? sourceY}px)`,
              ...labelStyle,
            }}
          >
            {label}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  )
}
