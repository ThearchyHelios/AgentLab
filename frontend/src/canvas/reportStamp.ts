import { useEffect } from 'react'
import { stampCounts } from '../lib/evidence'
import { useEvidence } from '../store/evidence'
import { useStudio } from '../store/studio'
import type { EvidenceStats, RunEvent } from '../types'

/**
 * 报告节点卡上的章：「引用 N · 无证据 M」，数据取这张图最近一次运行的 report.checked。
 *
 * - 画布上挂着一次运行（刚跑的、从记录页回放的）：就用它的事件，跟着 report.checked 实时落下；回放时
 *   游标还没走到那一刻不画
 * - 没挂运行：取这张工作流最近一次运行的证据图（只认封存范围内的 report.checked）
 * - 两边都没有这个节点的核对（没跑过、跑到一半、老后端）：不画章
 */
export interface ReportStamp {
  cited: number
  none: number
  stats: EvidenceStats
  claims?: string | null
  /** report.checked 落下的时刻（毫秒，和航迹同一条时间轴）；取自证据图的没有 */
  at: number | null
  runId: string | null
}

interface Checked { stats: EvidenceStats; claims?: string | null; at: number | null; event: RunEvent }
const checkedCache = new WeakMap<RunEvent[], Map<string, Checked>>()
/** 同一条事件给出同一个对象：选择器返回稳定引用，别的事件进来时报告卡不跟着重渲染 */
const byEvent = new WeakMap<RunEvent, Checked>()

/** 这次运行里每个报告节点最后一次核对，按事件数组缓存（每条事件都会把所有卡片的选择器跑一遍） */
function checkedOf(events: RunEvent[]): Map<string, Checked> {
  const hit = checkedCache.get(events)
  if (hit) return hit
  const out = new Map<string, Checked>()
  for (const e of events) {
    if (e.type !== 'report.checked' || !e.node_id || !e.data?.stats || typeof e.data.stats !== 'object') continue
    let c = byEvent.get(e)
    if (!c) {
      c = { stats: e.data.stats, claims: e.data.claims, at: typeof e.ts === 'number' ? e.ts * 1000 : null, event: e }
      byEvent.set(e, c)
    }
    out.set(e.node_id, c)
  }
  checkedCache.set(events, out)
  return out
}

export function useReportStamp(nodeId: string, enabled: boolean): ReportStamp | null {
  const workflowId = useStudio((s) => s.workflow?.id ?? null)
  const runId = useStudio((s) => s.run?.id ?? null)
  const live = useStudio((s) => (enabled && s.run ? checkedOf(s.events).get(nodeId) ?? null : null))
  const latest = useEvidence((s) => (enabled && workflowId ? s.latest[workflowId] : undefined))
  const loadLatest = useEvidence((s) => s.loadLatest)
  const dropLatest = useEvidence((s) => s.dropLatest)
  // 没挂运行时才去取最近一次运行；挂着运行时它就是「最近一次」，之前取的那份随之作废——
  // 「清除」把运行摘下来（或者换一张图再回来）时重新取，不拿这次运行之前的旧统计冒充「最近一次」
  useEffect(() => {
    if (!enabled || !workflowId) return
    if (runId) dropLatest(workflowId)
    else void loadLatest(workflowId)
  }, [enabled, workflowId, runId, loadLatest, dropLatest])
  if (!enabled) return null
  if (runId) {
    if (!live) return null
    const counts = stampCounts(live.stats, live.claims)
    return counts ? { ...counts, stats: live.stats, claims: live.claims, at: live.at, runId } : null
  }
  const data = latest?.status === 'ok' ? latest.data : undefined
  const report = data?.reports[nodeId]
  if (!report?.stats) return null
  const counts = stampCounts(report.stats, report.claims)
  return counts ? { ...counts, stats: report.stats, claims: report.claims, at: null, runId: data?.runId ?? null } : null
}
