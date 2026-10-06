import { useMemo, useState, type JSX } from 'react'
import { parseServerTime } from '../../lib/format'
import { ArtifactsPane, type ArtifactList } from '../../pages/runs/ArtifactsPane'
import type { RunArtifact } from '../../pages/runs/model'
import { emptyTrace } from '../../run/trace'
import type { GraphSpec, NodeType } from '../../types'

/**
 * 运行详情「工件」页签（pages/runs/ArtifactsPane）的预览用例，scripts/check-verify.mjs 按它检查分组和窄屏。
 *
 * - ?syn=run-artifacts：虚构的「门店周报」一次正式运行，各种工件都有：取数节点的查询快照、表结构快照和产出，循环里
 *   逐区汇总的三次产出（第二次是重试），口径卡的指标输入、指标集，写周报的报告文档，双盲复算、反方审稿各自执行的查询
 *   和对照、审稿记录，封存后追加的两条质询回答（一条问题很长）和质询执行的查询。宽窄、深浅色都用这一个，检查脚本换
 *   视口宽度和主题。
 *
 * 工件列表直接交给页签（不走接口）；航迹的起止照这次运行的开始、结束时间给：运行期间产出的能跳到航迹里那一刻，封存后
 * 追加的只写时间。点过的跳转记在 data-fixture-moment 上。门店、片区、金额、数据源都是虚构的。
 */

const STARTED = '2026-10-05T06:00:00'
const FINISHED = '2026-10-05T06:12:00'

const NODES: [string, NodeType, string][] = [
  ['pull', 'tool', '拉取门店销量'],
  ['summarize', 'llm', '逐区汇总'],
  ['caliber', 'metrics', '周报口径'],
  ['write', 'report', '写周报'],
  ['dual', 'dual_check', '双盲复算'],
  ['review', 'challenge', '反方审稿'],
]
const GRAPH: GraphSpec = {
  nodes: NODES.map(([id, type, label], i) => ({ id, type, position: { x: i * 220, y: 0 }, data: { label, config: {} } })),
  edges: NODES.slice(1).map(([id], i) => ({ source: NODES[i][0], target: id })),
}
const LABELS = Object.fromEntries(NODES.map(([id, , label]) => [id, label]))

/** 虚构的内容校验值：64 位十六进制，各不相同 */
const sha = (n: number) => (n * 2654435761 >>> 0).toString(16).padStart(8, '0').repeat(8)
let seq = 0
const art = (kind: string, node: string | null, at: string, size: number, meta: Record<string, any> | null = null): RunArtifact => (
  { id: sha(++seq), kind, node_id: node, size, meta, created_at: `2026-10-05T${at}` })

/** 质询的问题：服务端只记前 40 字（engine/inquiry.py 的 QUESTION_BRIEF），满 40 字的说明会很长 */
const LONG_QUESTION = '青禾片区的线上订单是按下单门店还是按收货地址归到片区的？如果按收货地址，跨片区配'

const RUN_ARTIFACTS: RunArtifact[] = [
  art('schema_snapshot', 'pull', '06:00:05', 3120, { source: '门店库', tables: 3 }),
  art('query_snapshot', 'pull', '06:00:10', 18_400, { source: '门店库', tables: ['orders', 'stores'] }),
  art('query_snapshot', 'pull', '06:00:20', 2_060, { source: '门店库', tables: ['refunds'] }),
  art('node_output', 'pull', '06:00:30', 1_210, { type: 'tool', attempt: 1 }),
  art('node_output', 'summarize', '06:01:00', 860, { type: 'llm', attempt: 1 }),
  art('node_output', 'summarize', '06:02:00', 910, { type: 'llm', attempt: 2 }),
  art('node_output', 'summarize', '06:03:00', 880, { type: 'llm', attempt: 1 }),
  art('metric_input', 'caliber', '06:04:00', 5_400, { path: 'vars.region_rows' }),
  art('metric_set', 'caliber', '06:04:30', 2_380, { caliber: '周报口径', version: 'v3' }),
  art('node_output', 'caliber', '06:04:40', 1_020, { type: 'metrics', attempt: 1 }),
  art('report_doc', 'write', '06:05:00', 16_820),
  art('node_output', 'write', '06:05:10', 4_310, { type: 'report', attempt: 1 }),
  art('query_snapshot', 'dual', '06:06:00', 1_480, { source: '门店库', tables: ['orders'] }),
  art('dual_check', 'dual', '06:06:30', 6_920, { metrics: 3 }),
  art('node_output', 'dual', '06:07:00', 740, { type: 'dual_check', attempt: 1 }),
  art('query_snapshot', 'review', '06:08:00', 1_150, { source: '门店库', tables: ['refunds'] }),
  art('query_snapshot', 'review', '06:09:00', 1_330, { source: '门店库', tables: ['orders'] }),
  art('challenge', 'review', '06:10:00', 8_640, { report: 'write' }),
  art('node_output', 'review', '06:10:30', 690, { type: 'challenge', attempt: 1 }),
  // 封存后追加的：审批质询（不挂在节点上）
  art('query_snapshot', null, '07:00:00', 1_260, { source: '门店库', tables: ['orders'] }),
  art('inquiry_answer', null, '07:01:00', 2_510, { report: 'write', unit: 'u3', question: LONG_QUESTION }),
  art('inquiry_answer', null, '07:20:00', 1_940, { report: 'write', unit: 'u5', question: '退货率怎么算的？' }),
]

function RunArtifacts(): JSX.Element {
  const list: ArtifactList = useMemo(() => ({ items: RUN_ARTIFACTS, state: 'ok', error: null, reload: () => undefined }), [])
  const trace = useMemo(() => ({
    ...emptyTrace(), phase: 'succeeded' as const,
    startedAt: parseServerTime(STARTED)?.getTime(), endedAt: parseServerTime(FINISHED)?.getTime(),
  }), [])
  const [moment, setMoment] = useState<number | null>(null)
  return (
    <div className="h-screen min-w-0" data-fixture="run-artifacts" data-fixture-moment={moment ?? ''}>
      <ArtifactsPane list={list} graph={GRAPH} trace={trace} labelOf={(id) => (id ? LABELS[id] : undefined)}
                     onMoment={(at) => setMoment(at)} />
    </div>
  )
}

export const cases: Record<string, () => JSX.Element> = {
  'run-artifacts': () => <RunArtifacts />,
}
