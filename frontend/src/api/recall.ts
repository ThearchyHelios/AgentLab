/**
 * 口径召回的接口（docs/design/next-round/02-口径账本.md 做法 7、8），挂在 api.recall 上。形状见 typesRecall.ts。
 *
 * - preview：发布弹窗的召回预告（把这一版发布成新版本之后，旧版本被谁用过、预计重算几组、超出上限几组）；
 * - batches：版本历史里口径升到这一版之后的召回批次（每组的结论、「另有 M 组未重算」）；
 * - continueBatch：手动接着算下一批（都算过了回 409）。
 * 待办里的「按新版重新出具」「知道了」走待办接口（api.tasks.respond，decision=reissue / ack）。
 *
 * 请求函数从 ./http 导入，不要从 ./client 导入（循环导入，见 http.ts 开头）。
 */
import type { RecallBatches, RecallPreview } from '../typesRecall'
import { get, post, qs, type RequestOptions } from './http'

const wf = (id: string) => encodeURIComponent(id)

export const recall = {
  preview: (workflowId: string, version: number, opts?: RequestOptions) =>
    get<RecallPreview>(`/workflows/${wf(workflowId)}/recall-preview${qs({ version })}`, opts),
  batches: (workflowId: string, version: number, opts?: RequestOptions) =>
    get<RecallBatches>(`/ledger/recall/${wf(workflowId)}/${version}`, opts),
  continueBatch: (workflowId: string, version: number) =>
    post<RecallBatches>(`/ledger/recall/${wf(workflowId)}/${version}/continue`),
}
