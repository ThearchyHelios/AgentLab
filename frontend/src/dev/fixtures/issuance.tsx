import type { JSX } from 'react'
import { AssistantStream, type StreamTurn } from '../../run/AssistantStream'
import { decodeRun } from '../../run/decode'
import type { RunEvent } from '../../types'

/**
 * 出具横幅里的核查缺口（运行面板成果区、问数据回答用的 run/AssistantStream 的 IssuanceBanner），scripts/check-stream.mjs
 * 按它检查：
 * - ?syn=issued-verify：正式运行降档出具。缺口里有一条没通过 SQL 检查的（横幅单列）、双盲复算和反方审稿报上来的几条
 *   （两路结果不一致、结论被推翻、指标无法复算），和一条校验没做完的；先后照服务端（出具契约按上游节点的先后收），核查的和
 *   别的混在一起。横幅按「核查未通过」「核查未完成」「校验未全部完成」分组，先后同时间线上出具那一行
 *
 * 门店、指标、数字都是虚构的。缺口原话照服务端的写法（出具契约给上游节点的缺口加「节点类型「节点名」：」的前缀）
 */
export const ISSUED_VERIFY = {
  run: 'syn-issued-verify',
  sql: '查询「取门店流水」（Q1）未通过 SQL 检查（「门店」关联「流水明细」是一对多，对「门店」的「面积」求和会重复计算），结果不可靠；受影响的引用：指标「坪效」',
  failed: ['双盲复算「复核月报口径」：指标「客单价」两路结果不一致，审核人选择了盲算结果', '反方审稿：推翻了 1 句结论：「西区客流回升主要来自会员日」'],
  unfinished: ['双盲复算「复核月报口径」：指标「复购率」无法复算：输入来自合并查询，无法单独复算'],
  other: ['叙述模板渲染为空（路径可能有误）'],
}

const V = ISSUED_VERIFY
const ISSUANCE = {
  tier: 'degraded', matched_numbers: 4, metrics_checked: 3, missing_required: [], missing_expected: [], unmatched_numbers: [],
  calibers: [],
  gaps: [V.failed[0], V.sql, V.other[0], V.unfinished[0], V.failed[1]],
  sql_checks: [{ query: '查询「取门店流水」（Q1）', node_id: 'fetch', problems: ['「门店」关联「流水明细」是一对多，对「门店」的「面积」求和会重复计算'],
                 refs: ['指标「坪效」'], gap: V.sql }],
}
const OUTPUT = { 月报: '上月各门店客单价 86.4 元，坪效 1,240 元。', _issuance: ISSUANCE }

function verifyTurn(): StreamTurn {
  const at = Date.now() / 1000 - 30
  const events: RunEvent[] = [
    { seq: 1, type: 'run.started', node_id: null, ts: at, data: { nodes: 1 } },
    { seq: 2, type: 'node.started', node_id: 'done', ts: at + 1, data: {} },
    { seq: 3, type: 'issuance', node_id: 'done', ts: at + 1.2, data: { ...ISSUANCE, unmatched: 0 } },
    { seq: 4, type: 'node.finished', node_id: 'done', ts: at + 1.3, data: { duration_ms: 300 } },
    { seq: 5, type: 'run.finished', node_id: null, ts: at + 1.4, data: { output: OUTPUT, duration_ms: 1400 } },
  ]
  return {
    id: 'issued-verify', question: '上月门店经营月报', phase: 'done', status: '已完成',
    steps: decodeRun(events, { status: 'succeeded' }), output: OUTPUT, runId: V.run, runClass: 'formal',
  }
}

export const cases: Record<string, () => JSX.Element> = {
  'issued-verify': () => (
    <div className="h-screen" data-fixture="issued-verify"><AssistantStream turns={[verifyTurn()]} /></div>
  ),
}
