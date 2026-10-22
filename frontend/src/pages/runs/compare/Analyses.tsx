import { useId } from 'react'
import { ANALYSIS_TEXT, analysisMethodLabel } from '../../../lib/termsAnalysis'
import type { RunCompareAnalysis, RunCompareAnalysisSide } from '../../../types'
import { Block } from './parts'

const C = ANALYSIS_TEXT.compare

/**
 * 分析卡区（engine/compare._analyses）：两次运行的分析卡按节点和分析项对齐，一项一行——算子的叫法、两次的判定语、变了没有。
 * 只列出两次的判定，不拆变化来源（方案 W1-c 的最小可用）。判定语、判定都是服务端给的原样；变化（same / verdict / phrase /
 * added / removed）是服务端比出来的，界面只按它挑颜色。两次运行都没有分析卡（老服务端没有这一项）时不画这一区
 */
export function Analyses({ analyses }: { analyses?: RunCompareAnalysis[] }) {
  const id = useId()
  if (!analyses?.length) return null
  return (
    <Block id={`${id}-analyses`} title={C.title} count={String(analyses.length)} hint={C.hint} attr={{ 'data-compare-analyses': '' }}>
      <ul className="divide-y">
        {analyses.map((a) => <AnalysisItem key={`${a.node_id}:${a.item}`} a={a} />)}
      </ul>
    </Block>
  )
}

function Side({ label, side }: { label: string; side: RunCompareAnalysisSide | null }) {
  return (
    <div className="min-w-0" data-compare-analysis-side={label}>
      <div className="text-2xs text-faint">{label}</div>
      <div className="text-xs [overflow-wrap:anywhere]" style={side?.status === 'refused' ? { color: 'var(--st-waiting)' } : undefined}>
        {side ? (side.phrase || C.none) : C.none}
      </div>
    </div>
  )
}

function AnalysisItem({ a }: { a: RunCompareAnalysis }) {
  const changed = a.change !== 'same'
  const color = a.change === 'verdict' ? 'var(--warn)' : a.change === 'added' ? 'var(--accent)' : 'var(--text-dim)'
  return (
    <li className="py-2.5 first:pt-0 last:pb-0" data-compare-analysis={`${a.node_id}:${a.item}`} data-change={a.change}>
      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
        <span className="min-w-0 text-xs font-medium [overflow-wrap:anywhere]" title={a.node_id}>{C.item(a.title || a.node_id, a.item)}</span>
        <span className="text-2xs text-faint">{analysisMethodLabel(a.op, a.mode)}</span>
        <span className="chip shrink-0" data-compare-analysis-change="" style={{
          color, borderColor: `color-mix(in srgb, ${color} 40%, transparent)`,
          background: changed && a.change === 'verdict' ? `color-mix(in srgb, ${color} 12%, transparent)` : undefined,
        }}>
          {C.change[a.change] ?? a.change}
        </span>
      </div>
      <div className="mt-1.5 grid grid-cols-2 gap-3 max-sm:grid-cols-1">
        <Side label={C.base} side={a.base} />
        <Side label={C.target} side={a.target} />
      </div>
    </li>
  )
}
