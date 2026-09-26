/**
 * 把各种报错翻成人话：发生了什么（title）、为什么（reason）、怎么办（action）。
 *
 * 项目一贯要求「错误要能照着做」，但最常见的「后端没起或正在重启」反而是最难懂
 * 的一句：fetch 抛出的 TypeError 原样透出成「Failed to fetch」，沙箱没回
 * exit_code 时拼出「exit undefined」，问数据页直接露出 pydantic 原文。
 *
 * 原文不丢：raw 放进「技术细节」折叠区，给运维复制用。
 */

import { ApiError } from '../api/client'

export interface HumanError {
  /** 一句人话：发生了什么 */
  title: string
  /** 为什么（知道时才有） */
  reason?: string
  /** 怎么办（知道时才有） */
  action?: string
  /** 原始报错文本，放进「技术细节」 */
  raw?: string
  /** network：连不上；http：后端回了错误；client：前端自己的错；unknown：说不清 */
  kind: 'network' | 'http' | 'client' | 'unknown'
  status?: number
}

const NETWORK_RE = /failed to fetch|networkerror|network request failed|load failed|err_connection|econnrefused|fetch failed/i

export const NETWORK_ERROR: Omit<HumanError, 'raw'> = {
  title: '连不上后端服务',
  reason: '后端可能没启动、正在重启，或者中间的代理断了。',
  action: '稍等几秒会自动重试；一直连不上就检查后端进程和 /api 代理。',
  kind: 'network',
}

/** Python 异常类名前缀：「KeyError: 'x'」「sqlalchemy.exc.OperationalError: …」 */
const PY_EXC_RE = /^(?:[a-z_][\w.]*\.)?([A-Z]\w*(?:Error|Exception|Exit|Timeout))\s*:\s*/

/**
 * network=false 用在「后端已经回了话」的文本上：那里面提到 fetch failed、
 * ECONNREFUSED，说的是后端够不着的下游（模型、数据库、MCP），不是我们连不上后端。
 */
function fromText(text: string, { network = true }: { network?: boolean } = {}): Omit<HumanError, 'kind'> & { kind?: HumanError['kind'] } {
  const raw = text.trim()
  if (!raw) return { title: '出错了，但没有给出原因', raw }
  if (network && NETWORK_RE.test(raw)) return { ...NETWORK_ERROR, raw }
  if (/exit (?:code )?undefined|exit_code.*none/i.test(raw)) {
    return { title: '请求没到沙箱', reason: '沙箱没有返回退出码，可能没启动或者中途被中断。', action: '看一眼设置里的沙箱状态，再重试。', raw }
  }
  // pydantic：「2 validation errors for GraphSpec\nnodes.1.type\n  Input should be …」
  const pyd = raw.match(/(\d+) validation errors? for (\w+)/)
  if (pyd) {
    if (/nodes\.\d+\.type/.test(raw)) {
      return {
        title: '生成的工作流里有节点类型不认识',
        reason: `模型写了一个不存在的节点类型，${pyd[1]} 处没通过格式校验。`,
        action: '换个说法再试一次；反复出现就把技术细节发给维护者。',
        raw,
      }
    }
    return {
      title: '数据格式没通过校验',
      reason: `${pyd[2]} 有 ${pyd[1]} 处不符合要求。`,
      action: '检查填写的内容；是模型生成的就换个说法重试。',
      raw,
    }
  }
  if (/timed? ?out|timeout|超时/i.test(raw) && raw.length < 200) {
    return { title: '操作超时', reason: raw.replace(PY_EXC_RE, ''), action: '稍后重试；反复超时就调大超时或检查下游服务。', raw }
  }
  // 类名前缀去掉，剩下的才是给人看的；类名留在 raw 里
  const stripped = raw.replace(PY_EXC_RE, '')
  const firstLine = stripped.split('\n')[0]
  return splitSentence(firstLine, raw)
}

/** 长句拆成「标题。原因」，短句整句当标题 */
function splitSentence(text: string, raw: string): Omit<HumanError, 'kind'> {
  const s = text.trim()
  if (s.length <= 48) return { title: s, raw: raw !== s ? raw : undefined }
  const cut = s.search(/[。；;！!？?]/)
  if (cut > 0 && cut < s.length - 1) {
    return { title: s.slice(0, cut + 1).replace(/[。；;]$/, ''), reason: s.slice(cut + 1).trim(), raw: raw !== s ? raw : undefined }
  }
  const colon = s.indexOf('：')
  if (colon > 4 && colon < 40) {
    return { title: s.slice(0, colon), reason: s.slice(colon + 1).trim(), raw: raw !== s ? raw : undefined }
  }
  return { title: s, raw: raw !== s ? raw : undefined }
}

export function humanizeError(e: unknown): HumanError {
  if (e instanceof ApiError) {
    if (e.kind === 'network') {
      return { ...NETWORK_ERROR, ...networkReason(e), raw: e.raw ?? e.message, status: e.status || undefined }
    }
    // 后端的 detail 按约定已经是「发生了什么 + 原因 + 怎么办」，优先用它
    const parsed = fromText(e.message, { network: false })
    const status = e.status
    let action = parsed.action
    if (!action && status >= 500) action = '稍后重试；反复出现就查看后端日志。'
    if (!action && (status === 401 || status === 403)) action = '检查设置里的署名和权限。'
    return {
      ...parsed,
      action,
      kind: 'http',
      status,
      raw: e.raw ?? (parsed.raw || undefined) ?? `${status} ${e.message}`,
    }
  }
  if (e instanceof Error) {
    if (e.name === 'AbortError') return { title: '已取消', kind: 'client', raw: e.message }
    const parsed = fromText(e.message || e.name)
    const raw = parsed.raw ?? (e.message || e.name)
    return { ...parsed, kind: parsed.kind ?? (e instanceof TypeError && NETWORK_RE.test(e.message) ? 'network' : 'unknown'), raw }
  }
  if (typeof e === 'string') {
    const parsed = fromText(e)
    return { ...parsed, kind: parsed.kind ?? 'unknown' }
  }
  if (e && typeof e === 'object') {
    const obj = e as Record<string, unknown>
    // 测连接、试跑工具这类接口失败时回 {ok:false, error, hint, detail}：error 已经是
    // 人话、hint 是怎么办、detail 是驱动原文。这是后端回的话，连不上的是它测的那个服务
    if (typeof obj.error === 'string' && obj.error) {
      const parsed = fromText(obj.error, { network: false })
      return {
        ...parsed,
        action: typeof obj.hint === 'string' && obj.hint ? obj.hint : parsed.action,
        raw: typeof obj.detail === 'string' && obj.detail ? obj.detail : parsed.raw,
        kind: parsed.kind ?? 'unknown',
      }
    }
    const msg = typeof obj.detail === 'string' ? obj.detail
      : typeof obj.message === 'string' ? obj.message
      : typeof obj.error === 'string' ? obj.error
      : ''
    if (msg) return { ...fromText(msg), kind: 'unknown' }
    let raw = ''
    try { raw = JSON.stringify(e) } catch { raw = String(e) }
    return { title: '出错了，但没有给出原因', kind: 'unknown', raw }
  }
  return { title: '出错了，但没有给出原因', kind: 'unknown' }
}

function networkReason(e: ApiError): Partial<HumanError> {
  // 超时和连不上是两回事：前者后端在、只是没回，后者根本够不着
  if (e.timeoutMs) {
    return { title: '后端没有响应', reason: `等了 ${Math.round(e.timeoutMs / 1000)} 秒没有回应，后端可能卡住了或负载太高。` }
  }
  if (e.status >= 500) {
    return { reason: `代理回了 ${e.status}，后端可能没启动或正在重启。` }
  }
  return { reason: NETWORK_ERROR.reason }
}

/** 一行文字：「标题：原因」。给 toast 和状态行用 */
export function errorMessage(e: unknown): string {
  const h = humanizeError(e)
  return h.reason ? `${h.title}：${h.reason}` : h.title
}

export function isNetworkError(e: unknown): boolean {
  return humanizeError(e).kind === 'network'
}
