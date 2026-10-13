/**
 * 审批质询的接口（/api/runs/{run_id}/inquiries…），挂在 api.inquiry 上。
 *
 * 请求函数（get / post / del……）和 ApiError 从 ./http 导入，不要从 ./client 导入：client.ts 要导入
 * 这个文件把它挂到 api 上，反过来导入就成了循环（见 http.ts 开头的说明）。
 */
import type { InquiryList, InquiryRequest, InquiryResponse } from '../types'
import { get, qs, request, type RequestOptions } from './http'

const run = (id: string) => `/runs/${encodeURIComponent(id)}`

export const inquiry = {
  /**
   * 就报告提问。核查模型只读查库作答，通常要几十秒，不设超时：服务端自己有时长上限，到点按查到的结果收尾。
   * 拒绝时 409 带 code（inquiry_not_open：运行没停在这张审批上、正式运行已封存、运行还在执行；inquiry_busy：这次运行
   * 另有一条质询正在回答；inquiry_disabled：设置里关了审批质询），输入不合法 422 带 field，detail 都是一句中文
   */
  ask: (runId: string, body: InquiryRequest) =>
    request<InquiryResponse>(`${run(runId)}/inquiries`, { method: 'POST', body: JSON.stringify(body) }),
  /** 这次运行的问答（封存前后的都算，按时间排）。report、unit 可选，只要某份报告、某一句的 */
  list: (runId: string, filter?: { report?: string; unit?: string }, opts?: RequestOptions) =>
    get<InquiryList>(`${run(runId)}/inquiries${qs(filter)}`, opts),
  /**
   * 质询引用过的查询快照（遮罩列已遮掉）。不是这次运行的质询引用过的工件回 404。约定没定死外面包不包一层
   * {id, content}，原样交回，由界面认（Inquiry.tsx 的 snapshotOf）
   */
  snapshot: (runId: string, artifact: string, opts?: RequestOptions) =>
    get<unknown>(`${run(runId)}/inquiries/artifacts/${encodeURIComponent(artifact)}`, opts),
}
