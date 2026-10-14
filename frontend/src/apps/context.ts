/**
 * 视图渲染器的上下文和各组件 data 的形状（服务端 BE-3 的页面解析按这一份交数据，见 docs/design §3.4–§3.6）。
 *
 * 界面层不放逻辑（P7）：这里没有任何求值、拼接和算术，组件只把服务端解析好的 data 画出来。数字一律用服务端渲染好的
 * text（render_metric / render_cell 的结果），前端不格式化数值；画图要的 y 是同一个数，text 照样用来标注。
 * 遮罩值不出服务端：被遮的格子服务端已经换掉，masked 只用来标「已遮罩」。
 *
 * 各组件的 data（ViewNode.data，照 backend/app/apps/resolve.py 的 _data），没有的键都可以缺。绑定了视图的组件都带
 * state（ok / missing / masked / withheld / empty / running / error）和可选的 message，说这一块为什么没有数据：
 * - stamp：{view, state, stamp, message?}
 * - metric：{state, text, name, unit, evidence}（evidence 的 kind 为 metric，点开走指标链接口）
 * - series：{state, mode（single / dual / incomparable / recompute）, unit, series: [{version, label, points: [{run_id,
 *   as_of, value, text, evidence}]}], breaks: [{after_run?, from, to, text}]}
 * - table、chart：{state, columns, rows（二维数组，遮罩的格子服务端已换成「已遮罩」）, masked, total_rows, truncated,
 *   evidence（kind 为 query，带查询快照的工件）}
 * - report：{state, node, doc_artifact, evidence}
 * - field（输入、成果、运行信息）：{state, text, value?, evidence?}；status：{state, value（运行状态）, text}
 * - form：{form}（表单名，表单在 ViewModel.forms 里）。提交即按这些值重新解析本页（按需视图的参数），见 input.tsx
 * - entry_button：{entry, label, disabled, reason?}
 * - instance_list：{who, entry, items: InstanceRow[]}
 * - instance_header：InstanceRow 的各项加 {state, status_text, stamp}
 * - instance_timeline：{state, status, status_text, steps: [{node, title, state, started_at, at}]}
 * - task_list：{scope, items}（待办的摘要，不是完整的待办；要处理时按 id 取完整的）
 * - task_form、signoffs：{state, task（摘要）, task_ui, assignments?}
 * - action_status：{state, node, records: [{id, state, environment, attempt, payload_hash（前 8 位）, error, receipt?}]}
 */
import { createContext, useContext } from 'react'
import type { ReactNode } from 'react'
import type { ViewEvidence, ViewModel, ViewNode, ViewSourceState } from '../typesApp'

export interface ViewCtxValue {
  model: ViewModel
  appId: string
  runId?: string
  preview: boolean
  onNavigate?: (pageId: string) => void
  onReload?: () => void
  /** 画一组子组件（布局组件用）：按「组件名 → 渲染器」的表找，认不出的画「此组件需要更新前端」 */
  render: (nodes: ViewNode[] | undefined) => ReactNode
  /** 点数字看出处：label 是界面上这个数的名字（指标名、列名） */
  openEvidence: (evidence: ViewEvidence, label: string, text?: string) => void
}

export const ViewCtx = createContext<ViewCtxValue | null>(null)

export function useView(): ViewCtxValue {
  const ctx = useContext(ViewCtx)
  if (!ctx) throw new Error('useView 只能在 ViewRenderer 里用')
  return ctx
}

export interface NodeProps {
  node: ViewNode
}

/** 这一块看的视图来源：data.view 指名的那个 */
export function sourceOf(model: ViewModel, node: ViewNode): ViewSourceState | null {
  const view = node.data?.view
  return typeof view === 'string' ? model.sources[view] ?? null : null
}

/** 没有可画的数据的几种状态。missing（目标不在）、masked（遮罩）照样有字可写（「—」「已遮罩」），不算 */
const NO_DATA = new Set(['empty', 'running', 'stale', 'error', 'withheld'])

/** 这一块为什么没有数据：服务端在 data.state / message 里说了；有数据的返回 null */
export function blockState(node: ViewNode): { state: string; message?: string } | null {
  const state = node.data?.state
  if (typeof state !== 'string' || !NO_DATA.has(state)) return null
  return { state, message: typeof node.data?.message === 'string' ? node.data.message : undefined }
}

export const str = (v: unknown): string => (typeof v === 'string' ? v : v == null ? '' : String(v))

/** 出处认不认得：至少要有运行 id 和种类 */
export function evidenceOf(v: unknown): ViewEvidence | null {
  if (!v || typeof v !== 'object') return null
  const e = v as ViewEvidence
  return typeof e.run_id === 'string' && !!e.run_id && typeof e.kind === 'string' ? e : null
}
