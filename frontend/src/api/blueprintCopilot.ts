/**
 * 蓝图助手和填关节（docs/design/normal-user-apps.md §2.2、§2.3、§2.11）：两条 SSE，帧同 streamCopilot（一帧一行
 * `data: <JSON>`，空行分隔）。状态在 store/blueprint.ts，翻译在 run/decode.ts，这里只管收发。
 *
 * - POST /api/copilot/blueprint-stream {blueprint_id, instruction, model?}：蓝图对话（服务端 api/copilot_blueprint.py）。
 *   操作有 model、thinking、heartbeat、look、plan、bp_set / bp_add / bp_remove / bp_template（模型提的，服务端落地）、
 *   bp_ask（问回来一句，带候选，这一轮到此结束）、check（自查）、reply（只回答、不改卡片）、done、
 *   final {blueprint, explanation, issues, asked?}（存好的整份蓝图以它为准；issues 是没采纳的操作）、error。
 *   这一轮落进蓝图对话、回答上一轮的反问都由服务端做。
 * - POST /api/copilot/fill-joints-stream {blueprint_id, groups?}：按模板填生成关节，不同组并行，每个操作带 group：
 *   group {group, status: started|retrying|filled|failed, joints?, issues?, message?}、各组自己的 look、plan、fill_joint、check；
 *   最后 final {blueprint, groups, problems}。
 *
 * 开流之前就被拒（4xx/5xx）时 onEnd 带服务端那句话、状态码和机读码；流到一半断了和一开始就连不上一样，说无法连接服务。
 * 请求函数从 ./http 导入，不要从 ./client 导入（循环导入，见 http.ts 开头）。
 */
import { ApiError, BASE, NETWORK_MESSAGE, actorHeader, codeOf, describeDetail, isGatewayFailure, parseBody, report } from './http'

export interface BlueprintStreamRequest {
  blueprint_id: string
  /** 想要什么、要怎么改，或者回答助手的反问 */
  instruction: string
  model?: string | null
}

export interface FillJointsRequest {
  blueprint_id: string
  /** 只填这几组；不给 = 还有没填上的关节的那几组 */
  groups?: string[]
}

export type StreamEnd = (error?: string, info?: { status?: number; code?: string; aborted?: boolean }) => void

/** 一条 SSE：返回取消函数 */
function sse(path: string, body: unknown, onOp: (op: any) => void, onEnd: StreamEnd): () => void {
  const controller = new AbortController()
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
      const flush = (frame: string) => {
        if (!frame.startsWith('data: ')) return
        try {
          onOp(parseBody(frame.slice(6)))
        } catch { /* 半截帧，忽略 */ }
      }
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        let idx: number
        while ((idx = buffer.indexOf('\n\n')) >= 0) {
          flush(buffer.slice(0, idx))
          buffer = buffer.slice(idx + 2)
        }
      }
      buffer += decoder.decode()
      if (buffer.trim()) flush(buffer.trim())
      onEnd()
    } catch (e: any) {
      if (e?.name === 'AbortError') { onEnd(undefined, { aborted: true }); return }
      if (e instanceof TypeError) {
        report(false, new ApiError(0, NETWORK_MESSAGE, { kind: 'network', raw: String(e) }))
        onEnd(NETWORK_MESSAGE)
        return
      }
      onEnd(e?.message ?? NETWORK_MESSAGE)
    }
  })()
  return () => controller.abort()
}

export const streamBlueprint = (body: BlueprintStreamRequest, onOp: (op: any) => void, onEnd: StreamEnd) =>
  sse('/copilot/blueprint-stream', body, onOp, onEnd)

export const streamFillJoints = (body: FillJointsRequest, onOp: (op: any) => void, onEnd: StreamEnd) =>
  sse('/copilot/fill-joints-stream', body, onOp, onEnd)
