import type { JSX } from 'react'
import fixtures from '../../run/__tests__/fixtures.json'
import { AssistantStream, type StreamTurn } from '../../run/AssistantStream'
import { decodeRun } from '../../run/decode'
import { analysisTeamRun } from '../../run/__tests__/synthetic'
import type { RunEvent } from '../../types'

/**
 * 分析能力一轮在问数据时间线、出具横幅上的样子，scripts/check-stream.mjs 按它检查：
 *
 * - ?syn=analysis：两轮真跑出来的事件（fixtures.json 的 analysis_agent、analysis_card，后端测试库里的虚构零售库 shop、模型换成
 *   脚本录下的）。第一轮 Agent 查两次库、写错一次变化拆解（调用有误）、做变化拆解、波动基线（算不了）、可比性检查，报告有 3 处
 *   分析说法标注（check，不影响档位）；第二轮分析卡两项（波动基线算不了、按地区拆解）
 * - ?syn=analysis-issued：出具横幅里分析能力一轮的三项都在——分析说法 strict 记了缺口、指标两期有条件可比和不可比、报告引用的
 *   指标没追到原始数据（受管级别记了缺口）
 * - ?syn=analysis-team：协作成员调分析工具（编号是成员自己的）
 *
 * 地区、门店、指标、数字都是虚构的；说法照服务端的写法
 */

type Fixture = Record<string, RunEvent[]>
const F = fixtures as unknown as Fixture

const outputOf = (events: RunEvent[]) => (events.find((e) => e.type === 'run.finished')?.data as { output?: Record<string, unknown> })?.output

function recordedTurns(): StreamTurn[] {
  const agent = F.analysis_agent
  const card = F.analysis_card
  return [
    { id: 'analysis-agent', question: '本周销售额为什么下降', phase: 'done', status: '已完成',
      steps: decodeRun(agent, { status: 'succeeded' }), output: outputOf(agent), runId: 'syn-analysis-agent', runClass: 'exploratory' },
    { id: 'analysis-card', question: '按模板出本周的异动分析', phase: 'done', status: '已完成',
      steps: decodeRun(card, { status: 'succeeded' }), output: outputOf(card), runId: 'syn-analysis-card', runClass: 'exploratory' },
  ]
}

/** 三项都在的出具声明（照 engine/nodes/io.py 的形状；strict 的那几处原话照服务端 analysis_claims 的写法） */
export const ANALYSIS_ISSUANCE = {
  mode: 'citations', tier: 'degraded', matched_numbers: 3, metrics_checked: 1, missing_required: [], missing_expected: [],
  unmatched_numbers: [], calibers: [],
  gaps: [
    '报告「写周报」有 1 处分析说法未通过核对（1 处与所挂结论的判定对不上）：判断要挂对应的分析结论并照判定写，原因要有知识库原话或运行输入作依据',
    '指标「环比」两期不可比：两边除了日期，筛选的还不一样',
    '指标「客单价」的输入没追到原始数据：vars.aov（停在「沙箱估算」）',
  ],
  analysis_claims: {
    mode: 'strict', count: 1, counts: { analysis_claim_mismatch: 1 }, counted: true,
    violations: [{ code: 'analysis_claim_mismatch', text: '主要来自', claim: 'source', op: 'decompose', analyses: ['A1'], unit: 'u3',
      segment: 's12', span: [80, 84], message: '「主要来自华南」与所挂的分析结论 A1 对不上：判定的主要来源是华东，不是华南',
      context: '其中下降主要来自华南。' }],
  },
  comparability: [
    { metric: 'wow', name: '环比', verdict: 'incomparable', phrase: '两期不可比：两边除了日期，筛选的还不一样', reasons: ['两边除了日期，筛选的还不一样'] },
    { metric: 'mom', name: '月环比', verdict: 'conditional', phrase: '两期有条件可比：两段的周末天数不同：4 天对 6 天',
      reasons: ['两段的周末天数不同：4 天对 6 天'] },
  ],
  untraced: { metrics: [{ metric: 'aov', name: '客单价', stops: [{ path: 'vars.aov', node_id: 'estimate' }] }], counted: true },
}

function issuedTurn(): StreamTurn {
  const at = Date.now() / 1000 - 30
  const output = { 周报: '本周销售额下降，主要来自华东。', _issuance: ANALYSIS_ISSUANCE }
  const events: RunEvent[] = [
    { seq: 1, type: 'run.started', node_id: null, ts: at, data: { nodes: 1 } },
    { seq: 2, type: 'node.started', node_id: 'done', ts: at + 1, data: { node_type: 'output', label: '成果' } },
    { seq: 3, type: 'issuance', node_id: 'done', ts: at + 1.2, data: { tier: 'degraded', mode: 'citations',
      gaps: ANALYSIS_ISSUANCE.gaps, analysis_claims: { mode: 'strict', count: 1, counted: true } } },
    { seq: 4, type: 'node.finished', node_id: 'done', ts: at + 1.3, data: { duration_ms: 300 } },
    { seq: 5, type: 'run.finished', node_id: null, ts: at + 1.4, data: { output, duration_ms: 1400 } },
  ]
  return { id: 'analysis-issued', question: '本周销售周报', phase: 'done', status: '已完成',
           steps: decodeRun(events, { status: 'succeeded' }), output, runId: 'syn-analysis-issued', runClass: 'formal' }
}

function teamTurn(): StreamTurn {
  const ev = analysisTeamRun()
  return { id: 'analysis-team', question: '销售额为什么下降', phase: 'done', status: '已完成',
           steps: decodeRun(ev, { status: 'succeeded' }), runId: 'syn-analysis-team' }
}

export const cases: Record<string, () => JSX.Element> = {
  analysis: () => (
    <div className="h-screen" data-fixture="analysis"><AssistantStream turns={recordedTurns()} /></div>
  ),
  'analysis-issued': () => (
    <div className="h-screen" data-fixture="analysis-issued"><AssistantStream turns={[issuedTurn()]} /></div>
  ),
  'analysis-team': () => (
    <div className="h-screen" data-fixture="analysis-team"><AssistantStream turns={[teamTurn()]} /></div>
  ),
}
