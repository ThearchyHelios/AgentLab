/**
 * 问数据的一轮在服务端跑（docs/design/orchestration-round/00-总方案.md §1；服务端 backend/app/api/chat_turns.py），挂在
 * api.chatTurns 上。页面只发起、只看：建图、运行、复核、按复核重来、落库都在服务端，页面断了这一轮也会跑完。
 *
 * - start：发起一轮（新问，或者对已有的一轮重试、重新运行、放宽步数、跑一下、继续运行、接着收尾），202 {turn_id, turn}；
 * - get：这一轮的任务现在怎样；cancel：停止（取消生成、取消运行）；
 * - streamChatTurn：这一轮的事件日志（SSE）。先回放 seq > after 的，再推实时；断了按收到的最后一个 seq 自动重连，补齐、
 *   不重复；45 秒什么都没收到（服务端每 15 秒有一拍心跳）就当连接死了，重连。服务端说这一轮结束了（stream.end）才停。
 *
 * 请求函数从 ./http 导入，不从 ./client 导入：client.ts 要导入这个文件把它挂到 api 上，反过来导入就成了循环。
 */
import type { ConversationTurn } from '../types'
import { ApiError, BASE, NETWORK_MESSAGE, actorHeader, failure, get, parseBody, post, report } from './http'

/** 发起一轮的方式（服务端 app/chat/job.MODES） */
export type ChatTurnMode = 'ask' | 'retry' | 'rerun' | 'max_steps' | 'run' | 'continue' | 'resume'

export interface ChatTurnStart {
  conversation_id: string
  question?: string
  /** 只查这几个数据源。新问不给是不限；重试不给沿用这一轮的，空数组是不限 */
  datasource_ids?: string[]
  retry_of?: string
  mode?: ChatTurnMode
  max_steps?: number
}

/** 事件日志里的一条（服务端 app/chat/job.py 的模块说明列了各种 type） */
export interface ChatTurnEvent {
  seq: number
  type: string
  data: Record<string, any>
  ts: number
}

/** 实时流结束时的情况：status 是服务端说的任务状态（stream.end）；error 是这一轮读不了（不存在、没权限） */
export interface ChatTurnStreamEnd {
  status?: string
  error?: string
  httpStatus?: number
}

const turn = (id: string) => `/chat/turns/${encodeURIComponent(id)}`

export const chatTurns = {
  start: (body: ChatTurnStart) => post<{ turn_id: string; turn: ConversationTurn | null }>('/chat/turns', body),
  get: (id: string) => get<{ id: string; conversation_id: string; job: ConversationTurn['job']; turn: ConversationTurn }>(turn(id)),
  cancel: (id: string) => post<{ ok: boolean }>(`${turn(id)}/cancel`, {}),
}

/** 多久什么都没收到就当连接死了（服务端心跳 15 秒一拍） */
const STALL_MS = 45_000
/** 连不上时重连的退避上限 */
const RETRY_MAX_MS = 5000

/**
 * 订阅这一轮的事件日志。返回取消函数（关掉连接、不再重连）。
 *
 * 连接正常结束却没收到 stream.end（服务端重启、代理断开）：按最后一个 seq 马上重连；连不上（网络断了、服务端在重启）按
 * 退避重试，直到服务端回来——这一轮在服务端照跑，回来之后补齐中间漏掉的那段
 */
export function streamChatTurn(
  turnId: string,
  after: number,
  onEvent: (event: ChatTurnEvent) => void,
  onEnd: (end: ChatTurnStreamEnd) => void,
): () => void {
  let closed = false
  let lastSeq = after
  let failures = 0
  let controller: AbortController | null = null
  let stall: ReturnType<typeof setTimeout> | undefined
  let again: ReturnType<typeof setTimeout> | undefined

  const finish = (end: ChatTurnStreamEnd) => {
    if (closed) return
    closed = true
    clearTimeout(stall)
    onEnd(end)
  }
  const arm = () => {
    clearTimeout(stall)
    stall = setTimeout(() => controller?.abort(), STALL_MS)
  }

  const connect = async () => {
    if (closed) return
    controller = new AbortController()
    /** 这一次连接收到了事件（不只是心跳） */
    let got = false
    let alive = false
    try {
      arm()
      const path = `${turn(turnId)}/stream?after=${lastSeq}`
      const res = await fetch(BASE + path, { headers: { Accept: 'text/event-stream', ...actorHeader() }, signal: controller.signal })
      if (!res.ok || !res.body) {
        const err = failure(res.status, res.statusText, await res.text().catch(() => ''), path)
        // 这一轮读不了（删了、不是你的）：再连也一样
        if (err.kind !== 'network' && res.status < 500) {
          finish({ error: err.message, httpStatus: res.status })
          return
        }
        throw err
      }
      report(true)
      alive = true
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        arm()
        buffer += decoder.decode(value, { stream: true })
        let idx: number
        while ((idx = buffer.indexOf('\n\n')) >= 0) {
          const frame = buffer.slice(0, idx)
          buffer = buffer.slice(idx + 2)
          if (!frame.startsWith('data: ')) continue   // 心跳（: ping）
          let event: any
          try { event = parseBody(frame.slice(6)) } catch { continue }
          if (!event || typeof event !== 'object') continue
          if (event.type === 'stream.end') {
            finish({ status: event.data?.status ?? event.status })
            controller.abort()
            return
          }
          if (typeof event.seq !== 'number') {
            // 助手等模型时的心跳不落库、不带 seq（服务端 chat/log.beat）：照收、不动 lastSeq，重连时服务端会补最近的一拍
            if (event.type === 'op' && event.data?.op === 'heartbeat' && !closed) onEvent(event as ChatTurnEvent)
            continue
          }
          if (event.seq <= lastSeq) continue   // 重连时重叠的那段只要一份
          lastSeq = event.seq
          got = true
          if (!closed) onEvent(event as ChatTurnEvent)
        }
      }
    } catch (e: any) {
      if (closed) return
      if (e instanceof TypeError) report(false, new ApiError(0, NETWORK_MESSAGE, { kind: 'network', raw: String(e) }))
    }
    if (closed) return
    clearTimeout(stall)
    // 收到了东西就马上接着连；只有心跳的稍等一下；连不上的按退避
    failures = got ? 0 : alive ? 0 : failures + 1
    const delay = got ? 0 : alive ? 300 : Math.min(300 * 2 ** failures, RETRY_MAX_MS)
    again = setTimeout(() => void connect(), delay)
  }
  void connect()

  return () => {
    closed = true
    clearTimeout(stall)
    clearTimeout(again)
    controller?.abort()
  }
}
