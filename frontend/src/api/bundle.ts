/**
 * 可验证报告的接口（/api/runs/{run_id}/…），挂在 api.bundle 上。
 *
 * 请求函数（get / post / del……）和 ApiError 从 ./http 导入，不要从 ./client 导入：client.ts 要导入
 * 这个文件把它挂到 api 上，反过来导入就成了循环（见 http.ts 开头的说明）。
 */
import { ApiError, BASE, NETWORK_MESSAGE, actorHeader, failure, report } from './http'

/**
 * Content-Disposition 里的文件名：先看 RFC 5987 的 filename*（中文名写在这里），没有或解不开再看 filename。
 * 服务端给的 filename 是纯 ASCII 的退路（agentlab-<运行号>-bundle.html），只在老客户端那边派上用场
 */
export function dispositionName(header: string | null): string | null {
  if (!header) return null
  const star = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(header)?.[1]
  if (star) {
    try {
      return decodeURIComponent(star.trim().replace(/^"|"$/g, ''))
    } catch {
      // 百分号编码坏了：退回 filename
    }
  }
  return /filename\s*=\s*"?([^";]+)"?/i.exec(header)?.[1]?.trim() || null
}

export const bundle = {
  /**
   * 下载可验证报告（GET /runs/{id}/bundle.html，附件）：回来的是文件本身和服务端给的文件名，没给就是 null，
   * 由调用方按运行名补。运行未封存、封存核对不一致时服务端回 409，ApiError 的 message 是给人看的原因
   */
  download: async (runId: string): Promise<{ blob: Blob; name: string | null }> => {
    const path = `/runs/${encodeURIComponent(runId)}/bundle.html`
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
