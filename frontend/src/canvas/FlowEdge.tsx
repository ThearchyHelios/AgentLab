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
  label, labelStyle, markerEnd, markerStart, interactionWidth, style, animated,
}: EdgeProps) {
  const routes = useContext(RoutingContext)
  const route = routes?.get(id)

  const [fallback] = getSmoothStepPath({
    sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition,
  })
  const path = route?.path || fallback

  // 光点按路径长度定速度：长边上的光点走得慢会像卡住了，所以让**线速度**
  // 恒定，而不是让每条边都用同一个周期
  const length = route?.length || 400
  const seconds = Math.min(3, Math.max(0.8, length / 320))

  return (
    <>
      <BaseEdge
        path={path}
        style={style}
        markerEnd={markerEnd}
        markerStart={markerStart}
        interactionWidth={interactionWidth}
      />
      {/* 数据在流动：光点顺着线走。虚线只会让人觉得"在动"，光点能看出往哪走。
          只在真正活跃的边上出现——每条边都跑光点的话，画布会变成一片流星雨 */}
      {animated && (
        <>
          <circle
            className="edge-packet" r={2.6}
            style={{ offsetPath: `path("${path}")`, animationDuration: `${seconds}s` }}
          />
          <circle
            className="edge-packet" r={1.8} opacity={0.55}
            style={{
              offsetPath: `path("${path}")`,
              animationDuration: `${seconds}s`,
              animationDelay: `${-seconds / 2}s`,
            }}
          />
        </>
      )}
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
