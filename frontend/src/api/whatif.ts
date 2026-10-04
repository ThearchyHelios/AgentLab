/**
 * 数字推演的接口（/api/runs/{run_id}/evidence/whatif），挂在 api.whatif 上。推演只读：不写运行记录、不改报告。
 *
 * 请求函数（get / post / del……）和 ApiError 从 ./http 导入，不要从 ./client 导入：client.ts 要导入
 * 这个文件把它挂到 api 上，反过来导入就成了循环（见 http.ts 开头的说明）。
 */
import type { WhatIfRequest, WhatIfResult, WhatIfRowsPage } from '../types'
import { get, request } from './http'

export const whatif = {
  /**
   * 在一次运行的口径卡上推演。signal 用来丢掉过时的请求：输入还在改、又发了新的一次时，上一次的答复不再要。
   * 取消照旧抛 AbortError（request 不把它当成连不上服务端）
   */
  run: (runId: string, body: WhatIfRequest, opts?: { signal?: AbortSignal }) =>
    request<WhatIfResult>(`/runs/${encodeURIComponent(runId)}/evidence/whatif`, {
      method: 'POST', body: JSON.stringify(body), signal: opts?.signal,
    }),
  /** 一页查询快照的行（从第 offset 行起最多 limit 行，行号从 0 数）：推演区里给整份结果、数组字段勾行用 */
  rows: (runId: string, artifact: string, page: { offset: number; limit: number }, opts?: { signal?: AbortSignal }) =>
    get<WhatIfRowsPage>(`/runs/${encodeURIComponent(runId)}/evidence/whatif/rows?${new URLSearchParams({
      artifact, offset: String(page.offset), limit: String(page.limit),
    })}`, { signal: opts?.signal }),
}
