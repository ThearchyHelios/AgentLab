import { createContext, useContext, type CSSProperties } from 'react'
import { BaseEdge, EdgeLabelRenderer, getSmoothStepPath, type EdgeProps } from '@xyflow/react'
import type { Route } from './routing'

/** 全画布的走线方案，由 FlowCanvas 算一次、所有边共用。 */
export const RoutingContext = createContext<Map<string, Route> | null>(null)

/**
 * 正在播的一次性时刻（成功回扫、失败级联）。只有边的那一半在这里画，卡片那一半
 * 归 NodeCard。seq 每次 +1：同一种时刻连着来两次，也要重新播一遍。
 */
export const MomentContext = createContext<{ kind: 'start' | 'success' | 'failed'; seq: number } | null>(null)

/**
 * 一条边在这次运行里处在什么状态。四种运行态各自一种画法，互不混淆：
 * - flow：目标此刻正在执行，accent 实线 + 一个光点（方向感）
 * - walked：走过了，accent 实线，不再动
 * - held：目标停在审批上，琥珀静止线 + 目标端口前一道闸门，不画光点
 * - cut：上游失败、这条再也走不到了，红色虚线
 * - unwalked：运行结束时没走的那一侧，虚线淡出
 * idle 是编辑态，没有运行痕迹。
 */
export type EdgeRunState = 'idle' | 'flow' | 'walked' | 'held' | 'cut' | 'unwalked'

export interface EdgeView {
  state: EdgeRunState
  /** 回边（循环体连回循环节点）：虚线 + ↺，和前向边一眼分开 */
  back: boolean
  /** 目标节点的拓扑层级，一次性时刻按它错开 */
  rank: number
  /** 回边已经兜回去几次（循环第几轮）。0 不显示 */
  loops?: number
}

/** 路径最后一个点。走线和兜底的 smoothstep 都以「L x y」收尾 */
function endPoint(path: string): { x: number; y: number } | null {
  const m = /(-?[\d.]+)[\s,]+(-?[\d.]+)\s*$/.exec(path)
  return m ? { x: Number(m[1]), y: Number(m[2]) } : null
}

/**
 * 画布上的边。
 *
 * React Flow 自带的 smoothstep 把竖直段一律摆在源和目标的正中间，同一列的
 * 边就会全部重合。路径改由 `routing.ts` 统一算（端口错开 + 走廊车道），
 * 这里只负责画。
 *
 * 拿不到路径时（还没量到节点尺寸、边指向了不存在的节点）退回自带的
 * smoothstep——宁可难看一帧，也不要线消失。
 *
 * 每条边都带箭头：前向边的方向靠"源在右、目标在左"的约定还能猜，回边猜不出。
 * 走线最后一段总是从左边水平进入目标端口，箭头朝右画在端口外沿。
 */
export function FlowEdge({
  id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition,
  label, labelStyle, markerEnd, markerStart, interactionWidth, style, data,
}: EdgeProps) {
  const routes = useContext(RoutingContext)
  const moment = useContext(MomentContext)
  const route = routes?.get(id)
  const view = (data as { sf?: EdgeView } | undefined)?.sf

  const [fallback] = getSmoothStepPath({
    sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition,
  })
  const path = route?.path || fallback
  const end = (route && endPoint(route.path)) ?? { x: targetX, y: targetY }
  // 端口是 9px 的圆，路径停在圆心：箭头尖顶在圆的外沿
  const tip = route ? end.x - 4.5 : end.x
  const state = view?.state ?? 'idle'
  const rank = view?.rank ?? 0

  // 光点按路径长度定速度：长边上的光点走得慢会像卡住了，所以让**线速度**
  // 恒定，而不是让每条边都用同一个周期
  const length = route?.length || 400
  const seconds = Math.min(3, Math.max(0.8, length / 320))
  const motion: CSSProperties = { offsetPath: `path("${path}")` }

  const loopBadge = view?.back && !(label != null && label !== '')
  const labelX = route?.labelX ?? sourceX
  const labelY = route?.labelY ?? sourceY

  return (
    <>
      <BaseEdge
        path={path}
        style={{ ...style, '--rank': rank } as CSSProperties}
        markerEnd={markerEnd}
        markerStart={markerStart}
        interactionWidth={interactionWidth}
      />
      <path className="sf-arrow" d={`M${tip - 7} ${end.y - 3.6}L${tip} ${end.y}L${tip - 7} ${end.y + 3.6}Z`} />
      {/* 等人：目标端口前一道闸门。流程停在这里，不是在流 */}
      {state === 'held' && (
        <path className="sf-gate" d={`M${tip - 14} ${end.y - 5.5}V${end.y + 5.5}M${tip - 10} ${end.y - 5.5}V${end.y + 5.5}`} />
      )}
      {/* 数据在流动：一个光点顺着线走，看得出往哪走。只在流入正在执行的节点的
          边上出现——每条边都跑光点的话，画布会变成一片流星雨 */}
      {state === 'flow' && (
        <circle className="edge-packet" r={2.6} style={{ ...motion, animationDuration: `${seconds}s` }} />
      )}
      {/* 成功回扫：走过的路径从入口往下依次亮一遍，只播这一次 */}
      {moment?.kind === 'success' && state === 'walked' && (
        <circle key={moment.seq} className="sf-sweep" r={3.2}
                style={{ ...motion, animationDelay: `${rank * 90}ms` }} />
      )}
      {(loopBadge || (label != null && label !== '')) && (
        <EdgeLabelRenderer>
          <div
            className={loopBadge ? 'sf-loop-badge tnum' : 'sf-edge-label'}
            style={{
              transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
              ...(loopBadge ? {} : labelStyle),
            }}
            title={loopBadge ? '回边：循环体跑完一轮，回到循环头' : undefined}
          >
            {loopBadge ? <>↺{view?.loops ? ` ×${view.loops}` : ''}</> : label}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  )
}
