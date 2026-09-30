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
import { VALIDATION_TITLE } from './validation'

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
  title: '无法连接服务',
  reason: '服务端可能未启动、正在重启，或代理连接中断。',
  action: '系统将在几秒后自动重试；如持续无法连接，请联系管理员检查服务端。',
  kind: 'network',
}

/** Python 异常类名前缀：「KeyError: 'x'」「sqlalchemy.exc.OperationalError: …」 */
const PY_EXC_RE = /^(?:[a-z_][\w.]*\.)?([A-Z]\w*(?:Error|Exception|Exit|Timeout))\s*:\s*/

const CJK_RE = /[一-鿿]/

/**
 * network=false 用在「后端已经回了话」的文本上：那里面提到 fetch failed、
 * ECONNREFUSED，说的是后端够不着的下游（模型、数据库、MCP），不是我们连不上后端。
 */
function fromText(text: string, { network = true }: { network?: boolean } = {}): Omit<HumanError, 'kind'> & { kind?: HumanError['kind'] } {
  const raw = text.trim()
  if (!raw) return { title: '发生错误，未返回具体原因', raw }
  // 422：client 已经把 pydantic 的原文逐条翻好了（lib/validation）。标题固定、逐条进原因，
  // 不交给 splitSentence——那边会在第一个「；」处把第二条切成原因、第一条挂在标题上
  if (raw.startsWith(`${VALIDATION_TITLE}：`)) {
    return { title: VALIDATION_TITLE, reason: raw.slice(VALIDATION_TITLE.length + 1), action: '请按提示修改后再提交。' }
  }
  if (network && NETWORK_RE.test(raw)) return { ...NETWORK_ERROR, raw }
  if (/exit (?:code )?undefined|exit_code.*none/i.test(raw)) {
    return { title: '请求未到达沙箱', reason: '沙箱未返回退出码，可能未启动或中途被中断。', action: '请在「设置 → 运行环境」中查看沙箱状态后重试。', raw }
  }
  // pydantic：「2 validation errors for GraphSpec\nnodes.1.type\n  Input should be …」
  const pyd = raw.match(/(\d+) validation errors? for (\w+)/)
  if (pyd) {
    if (/nodes\.\d+\.type/.test(raw)) {
      return {
        title: '生成的工作流包含无法识别的节点类型',
        reason: `模型生成了不存在的节点类型，${pyd[1]} 处未通过格式校验。`,
        action: '请调整描述后重试；如反复出现，请将技术细节提供给维护人员。',
        raw,
      }
    }
    return {
      title: '数据格式未通过校验',
      reason: `${pyd[2]} 有 ${pyd[1]} 处不符合要求。`,
      action: '请检查填写的内容；如为模型生成，请调整描述后重试。',
      raw,
    }
  }
  // 只接英文原文（「ReadTimeout: timed out」）。带中文的已经是后端写好的人话，
  // 标题、原因、怎么办都在句子里：再套一个「操作超时」，标题和原因就说了两遍
  if (!CJK_RE.test(raw) && /timed? ?out|timeout/i.test(raw) && raw.length < 200) {
    return { title: '操作超时', reason: raw.replace(PY_EXC_RE, ''), action: '请稍后重试；如反复超时，请调大超时时间或检查下游服务。', raw }
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
    if (!action && status >= 500) action = '请稍后重试；如反复出现，请查看服务端日志。'
    if (!action && (status === 401 || status === 403)) action = '请检查设置中的署名和权限。'
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
    return { title: '发生错误，未返回具体原因', kind: 'unknown', raw }
  }
  return { title: '发生错误，未返回具体原因', kind: 'unknown' }
}

function networkReason(e: ApiError): Partial<HumanError> {
  // 超时和连不上是两回事：前者后端在、只是没回，后者根本够不着
  if (e.timeoutMs) {
    return { title: '服务端无响应', reason: `等待 ${Math.round(e.timeoutMs / 1000)} 秒未收到响应，服务端可能繁忙或无响应。` }
  }
  if (e.status >= 500) {
    return { reason: `代理返回 ${e.status}，服务端可能未启动或正在重启。` }
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
