/**
 * 分享页（/s）的两个接口：GET /api/shares/page、GET /api/shares/report.html（服务端 api/shares.py，04 细案 C2）。
 *
 * 分享 key 在链接的 # 后面（/s#<key>），打开页面时不发给服务器；这里读出来放进请求头 X-AgentLab-Share，服务端只认请求头。
 * 不经 http.request：那条路会带署名头、把 401 当成「会话没了」整页跳去登录页——分享页上没有登录这回事，也不该在外壳的连接
 * 状态里报来报去。报错照样翻成 ApiError（{detail, code}），页面按 code 写「链接已撤销 / 已到期」。
 *
 * 只从 ./http、./bundle 导入（同 reportExport.ts，避免和 client.ts 循环导入）。
 */
import type { SharePayload } from '../typesApp'
import { ApiError, BASE, NETWORK_MESSAGE, codeOf, describeDetail } from './http'
import { dispositionName } from './bundle'

export const SHARE_HEADER = 'X-AgentLab-Share'

async function call(path: string, key: string): Promise<Response> {
  let res: Response
  try {
    res = await fetch(BASE + path, { headers: { [SHARE_HEADER]: key }, credentials: 'omit', referrerPolicy: 'no-referrer' })
  } catch (e: any) {
    throw new ApiError(0, NETWORK_MESSAGE, { kind: 'network', raw: `${e?.name ?? 'Error'}: ${e?.message ?? e}` })
  }
  if (res.ok) return res
  const text = await res.text().catch(() => '')
  let body: any = null
  try { body = JSON.parse(text) } catch { /* 不是 JSON（代理回的页面） */ }
  const detail = body?.detail
  throw new ApiError(res.status, detail ? describeDetail(detail, path) : `请求失败（HTTP ${res.status}）`,
    { detail, code: codeOf(body), body, raw: `${res.status} ${text.slice(0, 500)}` })
}

export const shares = {
  /** 分享页的视图模型、页脚、能下载报告的运行 */
  page: async (key: string): Promise<SharePayload> => (await call('/shares/page', key)).json(),
  /** 下载报告（附件）：只认这条分享解析出来的运行；文件名取服务端给的，没给是 null */
  report: async (key: string, runId: string, reportNode?: string): Promise<{ blob: Blob; name: string | null }> => {
    const q = new URLSearchParams({ run: runId })
    if (reportNode) q.set('report', reportNode)
    const res = await call(`/shares/report.html?${q.toString()}`, key)
    return { blob: await res.blob(), name: dispositionName(res.headers.get('Content-Disposition')) }
  },
}
