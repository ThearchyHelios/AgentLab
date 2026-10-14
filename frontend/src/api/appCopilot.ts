/**
 * 应用润色的助手（POST /api/copilot/app-stream，SSE）：应用编辑器右侧的助手面板用它。
 *
 * 和 streamCopilot（画布生成）同一种帧：一帧一行 `data: <JSON>`，空行分隔。操作有三类（§3.11）：
 * - 应用操作：app_add / app_update / app_remove / app_move / app_page / app_set，形状同服务端 app/apps/ops.py，
 *   编辑器按到达顺序落到草稿上（pages/apps/edit/appOps.ts 的 applyAppOp）；
 * - look（查看了哪些工作流版本、能力）、ask（问回来一句，这一轮到此结束）、thinking、heartbeat、check、reply、error；
 * - final：{spec, issues, explanation}，服务端落地后的整份 spec（以它为准，替换前面边收边改的草稿）。
 *
 * 系统先代为查看固定的版本（look 的 by=system），模型可以再查看；岗位绑定只能由人做，助手只留空并提示。
 * 请求函数从 ./http 导入，不要从 ./client 导入（循环导入，见 http.ts 开头）。
 */
import type { AppBindings, AppSpec } from '../typesApp'
import { ApiError, BASE, NETWORK_MESSAGE, actorHeader, codeOf, describeDetail, isGatewayFailure, parseBody, report } from './http'

/** 请求体同服务端 api/copilot.AppStreamIn */
export interface AppCopilotRequest {
  app_id: string
  instruction: string
  /** 编辑器里此刻的草稿（可能还没保存）：助手在它上面改 */
  spec: AppSpec
  /** 草稿里的岗位绑定（不进 spec）：服务端的应用检查按它判断岗位绑没绑 */
  bindings?: AppBindings
  /** 应用编辑时的助手对话（conversations.app_id 归属这个应用），带上它这一轮才接得上前面聊过的 */
  conversation_id?: string | null
  model?: string | null
}

/** 返回取消函数。开流之前就被拒（4xx/5xx）时 onEnd 带服务端那句话、状态码和机读码 */
export function streamAppCopilot(
  body: AppCopilotRequest,
  onOp: (op: any) => void,
  onEnd: (error?: string, info?: { status?: number; code?: string }) => void,
): () => void {
  const controller = new AbortController()
  const path = '/copilot/app-stream'
  void (async () => {
    try {
      const res = await fetch(`${BASE}${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream', ...actorHeader() },
        body: JSON.stringify(body),
        signal: controller.signal,
      })
      if (!res.ok || !res.body) {
        const text = await res.text().catch(() => '')
        let parsed: any
        let isJson = false
        try { parsed = JSON.parse(text); isJson = true } catch { /* 响应体不是 JSON */ }
        if (isGatewayFailure(res.status, text, isJson)) {
          report(false, new ApiError(res.status, NETWORK_MESSAGE, { kind: 'network' }))
          onEnd(NETWORK_MESSAGE)
          return
        }
        report(true)
        onEnd(isJson ? describeDetail(parsed?.detail ?? parsed, path)
          : res.status >= 500 ? `服务端错误（${res.status}），详情请查看服务日志`
          : `请求失败（HTTP ${res.status}）`, { status: res.status, code: isJson ? codeOf(parsed) : undefined })
        return
      }
      report(true)
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        let idx: number
        while ((idx = buffer.indexOf('\n\n')) >= 0) {
          const frame = buffer.slice(0, idx)
          buffer = buffer.slice(idx + 2)
          if (!frame.startsWith('data: ')) continue
          try {
            onOp(parseBody(frame.slice(6)))
          } catch { /* 半截帧，忽略 */ }
        }
      }
      onEnd()
    } catch (e: any) {
      if (e?.name === 'AbortError') { onEnd(); return }
      if (e instanceof TypeError) {
        report(false, new ApiError(0, NETWORK_MESSAGE, { kind: 'network', raw: String(e) }))
        onEnd(NETWORK_MESSAGE)
        return
      }
      onEnd(e?.message ?? '连接中断，请重试')
    }
  })()
  return () => controller.abort()
}
