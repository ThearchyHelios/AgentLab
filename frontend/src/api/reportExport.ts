/**
 * 报告导出的接口（/api/runs/{run_id}/report.html），挂在 api.reportExport 上。
 *
 * 请求函数（get / post / del……）和 ApiError 从 ./http 导入，不要从 ./client 导入：client.ts 要导入
 * 这个文件把它挂到 api 上，反过来导入就成了循环（见 http.ts 开头的说明）。Content-Disposition 的文件名
 * 解析和可验证报告（./bundle）是同一个拼法，直接复用那边导出的 dispositionName。
 */
import { ApiError, BASE, NETWORK_MESSAGE, actorHeader, failure, report } from './http'
import { dispositionName } from './bundle'

export const reportExport = {
  /**
   * 下载报告导出（GET /runs/{id}/report.html，附件）：回来的是文件本身和服务端给的文件名，没给就是 null，
   * 由调用方按问题或答案补。reportNode 点名报告撰写节点；点名的节点这次运行没有、或者这次运行没有报告文档时
   * 服务端回 404，报告文档与哈希不一致回 409，ApiError 的 message 是给人看的原因
   */
  download: async (runId: string, reportNode?: string): Promise<{ blob: Blob; name: string | null }> => {
    const path = `/runs/${encodeURIComponent(runId)}/report.html`
      + (reportNode ? `?report=${encodeURIComponent(reportNode)}` : '')
    let res: Response
    try {
      res = await fetch(BASE + path, { headers: actorHeader() })
    } catch (e: any) {
      const err = new ApiError(0, NETWORK_MESSAGE, { kind: 'network', raw: `${e?.name ?? 'Error'}: ${e?.message ?? e}` })
      report(false, err)
      throw err
    }
    if (!res.ok) throw failure(res.status, res.statusText, await res.text().catch(() => ''), path)
    report(true)
    return { blob: await res.blob(), name: dispositionName(res.headers.get('Content-Disposition')) }
  },
}
