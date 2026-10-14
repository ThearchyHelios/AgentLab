/**
 * 请求层：fetch / XHR、报错翻译、连接状态旁听、署名头、去掉写给模型的 for_model。
 *
 * 从 client.ts 拆出来，是为了让 api/ 下各功能的命名空间文件（schedules.ts、compare.ts……）能复用
 * get / post / del 这些，又不和 client.ts 互相导入：client.ts 要把它们挂到 api 上，它们要是反过来从
 * client.ts 取请求函数，谁先被加载谁就会读到另一边还没初始化的绑定。所以命名空间文件只从这里导入；
 * 页面和组件照旧从 client.ts 导入（ApiError、onConnectivity 这些在那边原样转出）。
 */
import { localActor } from '../lib/actor'
import { VALIDATION_TITLE, describeValidation } from '../lib/validation'

export const BASE = '/api'

/**
 * network：请求根本没到后端（没起、在重启、代理断了、超时没回）。
 * http：后端回了错误状态码，message 是后端的 detail。
 */
export type ApiErrorKind = 'network' | 'http'

export const NETWORK_MESSAGE = '无法连接服务（服务端可能未启动或正在重启），请稍后重试'

export class ApiError extends Error {
  kind: ApiErrorKind
  /** 后端 detail 的原样（字符串、校验错误数组都可能） */
  detail?: unknown
  /** 原始报错文本，给「技术细节」和复制用 */
  raw?: string
  /** 超时断开时等了多久 */
  timeoutMs?: number
  /**
   * 后端的机读码（app/api/coded.py 的 {detail, code}，比如 datasource_scope_empty）。
   * detail 是给人看的话，随时可能改写；要按错误种类分支的认这个
   */
  code?: string
  /** 带机读码的错误里出错的是哪一项（app/api/coded.py 的 field，比如剖析设置的 max_queries）；没有就是 undefined */
  field?: string
  /**
   * 后端要用户先拍板时随错误一起给的 {kind, details}（比如上传表格的 422：数字列混入非数字、
   * 交叉表）。原样保留，按接口各自校验后再用（上传表格见 uploadDecision）
   */
  decision?: unknown
  /**
   * 错误响应的原样 JSON：detail、code 之外服务端另带的键在这里——表单校验没过（应用入口、待办回复的 422）的
   * errors: [{path, code, message}]（lib/schemaForm 的 formErrorsOf 按 path 挂到字段下面），没登录（401）的 mode
   */
  body?: unknown

  constructor(
    public status: number,
    message: string,
    opts?: { kind?: ApiErrorKind; detail?: unknown; raw?: string; timeoutMs?: number; code?: string; field?: string; decision?: unknown
      body?: unknown },
  ) {
    super(message)
    this.name = 'ApiError'
    this.kind = opts?.kind ?? (status === 0 ? 'network' : 'http')
    this.detail = opts?.detail
    this.raw = opts?.raw
    this.timeoutMs = opts?.timeoutMs
    this.code = opts?.code
    this.field = opts?.field
    this.decision = opts?.decision
    this.body = opts?.body
  }
}

/**
 * 连接状态的旁听者：任何一次请求够着了后端（不管状态码）就报 true，网络层失败
 * 报 false。catalog 靠它维护全站的「后端连没连上」，不用另起一路轮询去猜。
 */
type ConnectivityListener = (reachable: boolean, error?: ApiError) => void
const connectivity = new Set<ConnectivityListener>()

export function onConnectivity(listener: ConnectivityListener): () => void {
  connectivity.add(listener)
  return () => { connectivity.delete(listener) }
}

export function report(reachable: boolean, error?: ApiError) {
  for (const l of connectivity) {
    try { l(reachable, error) } catch { /* 旁听者出错不能拖垮请求本身 */ }
  }
}

/**
 * 开了登录时，服务端要求非 GET 请求带这个头（再加 Origin 校验）防跨站请求：别的站点的表单和简单请求带不上自定义头。
 * 每个请求都带上，比按方法挑更不容易漏——流式接口、上传、延迟删除都从 actorHeader 取头
 */
export const REQUEST_HEADER = { 'X-AgentLab-Request': '1' }

export function actorHeader(): Record<string, string> {
  const actor = localActor()
  // 请求头只能是 Latin-1：中文署名原样放进去，fetch 直接抛错、整个请求发不出去。
  // 后端 runs.actor_of 解码，纯 ASCII 的老署名编码前后一样。开了登录时服务端不看 X-Actor，带着无妨
  return actor ? { ...REQUEST_HEADER, 'X-Actor': encodeURIComponent(actor) } : { ...REQUEST_HEADER }
}

/**
 * 开了登录、会话没了（过期、被注销）：服务端回 401，整页跳到登录页，登录后回到原处。整页跳而不是路由跳：
 * 外壳里各个 store 拿的都是上一个人的数据，重新加载一遍最干净。登录接口自己的 401（密码不对）和登录页上的请求不跳。
 * 认人接口（/auth/me）的 401 也不跳：它是「还没登录」的回答，外壳拿到后自己去登录页（App 启动时）——在这里再整页跳一次，
 * 登录页加载起来又认一遍人，同一次进门要请求好几次
 */
function toLogin(path?: string) {
  if (typeof window === 'undefined' || path === '/auth/login' || path === '/auth/me') return
  const { pathname, search } = window.location
  if (pathname === '/login') return
  const back = `${pathname}${search}`
  window.location.assign(back && back !== '/' ? `/login?next=${encodeURIComponent(back)}` : '/login')
}

/**
 * FastAPI 的 422 是 [{loc, msg, type}] 数组：直接 String() 是 [object Object]，照拼 msg
 * 又是 pydantic 的英文原文。按 type 翻成中文（lib/validation），path 用来查表单上的叫法
 */
export function describeDetail(detail: unknown, path?: string): string {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return `${VALIDATION_TITLE}：${describeValidation(detail, path).join('；')}`
  // 对象形态的 detail 原样 JSON 化只会把一串花括号摆到提示里：界面只说无法识别，原文在调用方的 raw 里
  return '请求失败，服务端返回了无法识别的错误信息'
}

/**
 * 网关类失败（代理够不着后端）也算网络失败：vite 代理连不上后端时回 500 且响应体
 * 为空，nginx 回 502/503/504 的 HTML。后端自己抛的 HTTPException 一定带 JSON detail。
 */
export function isGatewayFailure(status: number, bodyText: string, isJson: boolean): boolean {
  if (isJson) return false
  if (status === 502 || status === 503 || status === 504) return true
  return status === 500 && !bodyText.trim()
}

/**
 * 后端交回模型用的改写指令：报告核对的 violations、助手自查的问题条目里带着 for_model。那是写给模型的
 * 原话（「单元格要写成 Q<编号>.r<行>.<列>」这类），界面只显示给人看的 message。前端没有任何地方读它，
 * 就在数据进来的地方去掉——原始事件、工件原文、展开详情这些直接显示 JSON 的地方也就不会露出来。
 */
const MODEL_NOTE = 'for_model'

function dropModelNotes(v: unknown): void {
  if (Array.isArray(v)) {
    for (const x of v) dropModelNotes(x)
  } else if (v && typeof v === 'object') {
    delete (v as Record<string, unknown>)[MODEL_NOTE]
    for (const x of Object.values(v)) dropModelNotes(x)
  }
}

/** 解析响应体；带 for_model 的（先按原文粗筛，不带的不走一遍）去掉它 */
export function parseBody(text: string): any {
  const body = JSON.parse(text)
  if (text.includes(`"${MODEL_NOTE}"`)) dropModelNotes(body)
  return body
}

export async function request<T>(path: string, init?: RequestInit & { timeoutMs?: number }): Promise<T> {
  const { timeoutMs, ...rest } = init ?? {}
  // 自己的超时和调用方的取消要分开：调用方取消照旧抛 AbortError（没人想看到
  // 「连不上后端」），只有我们自己掐断的才算网络失败
  let timer: ReturnType<typeof setTimeout> | undefined
  let timedOut = false
  let signal = rest.signal
  if (timeoutMs) {
    const ctrl = new AbortController()
    timer = setTimeout(() => { timedOut = true; ctrl.abort() }, timeoutMs)
    rest.signal?.addEventListener('abort', () => ctrl.abort())
    signal = ctrl.signal
  }
  let res: Response
  try {
    res = await fetch(BASE + path, {
      ...rest,
      signal,
      headers: {
        ...(rest.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
        ...actorHeader(),
        ...rest.headers,
      },
    })
  } catch (e: any) {
    if (timedOut) {
      const err = new ApiError(0, `服务端 ${Math.round(timeoutMs! / 1000)} 秒未响应，请稍后重试`,
        { kind: 'network', raw: `timeout after ${timeoutMs}ms: ${path}`, timeoutMs })
      report(false, err)
      throw err
    }
    if (e?.name === 'AbortError') throw e
    // fetch 的网络失败是 TypeError（Chrome「Failed to fetch」、Safari「Load failed」），
    // 原文对用户毫无意义，收进 raw
    const err = new ApiError(0, NETWORK_MESSAGE, { kind: 'network', raw: `${e?.name ?? 'Error'}: ${e?.message ?? e}` })
    report(false, err)
    throw err
  } finally {
    if (timer) clearTimeout(timer)
  }
  if (!res.ok) throw failure(res.status, res.statusText, await res.text().catch(() => ''), path)
  report(true)
  if (res.status === 204) return undefined as T
  return parseBody(await res.text())
}

/** 后端 {detail, code} 里的机读码；没有就是 undefined */
export const codeOf = (body: unknown): string | undefined => {
  const code = body && typeof body === 'object' ? (body as { code?: unknown }).code : undefined
  return typeof code === 'string' && code ? code : undefined
}

/** 后端 {detail, code, field} 里出错的那一项；没有就是 undefined */
const fieldOf = (body: unknown): string | undefined => {
  const field = body && typeof body === 'object' ? (body as { field?: unknown }).field : undefined
  return typeof field === 'string' && field ? field : undefined
}

/** 后端 {detail, decision} 里要用户拍板的内容：是个对象就原样带上，没有就是 undefined */
const decisionOf = (body: unknown): unknown => {
  const decision = body && typeof body === 'object' ? (body as { decision?: unknown }).decision : undefined
  return decision && typeof decision === 'object' ? decision : undefined
}

/** 非 2xx 的响应 → ApiError，顺带报告连接状态。fetch 和 XHR 两条路共用 */
export function failure(status: number, statusText: string, text: string, path?: string): ApiError {
  let body: any
  let isJson = false
  try { body = JSON.parse(text); isJson = true } catch { /* 响应体不是 JSON */ }
  if (isGatewayFailure(status, text, isJson)) {
    const err = new ApiError(status, NETWORK_MESSAGE, {
      kind: 'network', raw: `${status} ${statusText}${text ? `\n${text.slice(0, 500)}` : ''}`,
    })
    report(false, err)
    return err
  }
  report(true)
  if (status === 401) toLogin(path)
  if (isJson) {
    const detail = body?.detail ?? body
    return new ApiError(status, describeDetail(detail, path), {
      detail, raw: typeof body?.raw === 'string' ? body.raw : `${status} ${text.slice(0, 2000)}`,
      code: codeOf(body), field: fieldOf(body), decision: decisionOf(body),
      body,
    })
  }
  const message = status >= 500
    ? `服务端错误（${status}），详情请查看服务日志`
    : `请求失败（HTTP ${status}）`
  return new ApiError(status, message, { raw: `${status} ${statusText}\n${text.slice(0, 2000)}` })
}

/** 上传进度。字节发完（sent）之后还要等后端解析、切块，那段没有进度可报 */
export interface UploadProgress {
  /** 已经发出去的字节 */
  loaded: number
  /** 总字节；浏览器算不出来时为 null，这时只能画已发多少，不画百分比 */
  total: number | null
  /** 字节已经全部发完，在等后端处理 */
  sent: boolean
}

export interface UploadOptions {
  onProgress?: (p: UploadProgress) => void
  signal?: AbortSignal
}

/**
 * 带字节进度的上传。fetch 拿不到上传进度，只能用 XHR；报错、连接状态、署名头和
 * request 走同一套，调用方看到的 ApiError 没有区别。取消照旧抛 AbortError。
 */
export function upload<T>(path: string, form: FormData, opts?: UploadOptions): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', BASE + path)
    for (const [k, v] of Object.entries(actorHeader())) xhr.setRequestHeader(k, v)
    const onProgress = opts?.onProgress
    if (onProgress) {
      // 进度事件里的字节数含 multipart 的边界和表头，比文件本身略大；发完那一下
      // 沿用它的总数，前后两个数才对得上
      let total: number | null = null
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) total = e.total
        onProgress({ loaded: e.loaded, total, sent: false })
      }
      xhr.upload.onload = () => {
        const bytes = total ?? [...form.values()].reduce((n, v) => n + (v instanceof Blob ? v.size : 0), 0)
        onProgress({ loaded: bytes, total: bytes || null, sent: true })
      }
    }
    const abort = () => xhr.abort()
    opts?.signal?.addEventListener('abort', abort)
    const cleanup = () => opts?.signal?.removeEventListener('abort', abort)
    xhr.onload = () => {
      cleanup()
      if (xhr.status < 200 || xhr.status >= 300) {
        reject(failure(xhr.status, xhr.statusText, xhr.responseText ?? '', path))
        return
      }
      report(true)
      if (xhr.status === 204 || !xhr.responseText) { resolve(undefined as T); return }
      try {
        resolve(parseBody(xhr.responseText))
      } catch {
        reject(new ApiError(xhr.status, '无法解析服务端返回的内容', { raw: xhr.responseText.slice(0, 2000) }))
      }
    }
    xhr.onerror = () => {
      cleanup()
      const err = new ApiError(0, NETWORK_MESSAGE, { kind: 'network', raw: `XMLHttpRequest error: POST ${path}` })
      report(false, err)
      reject(err)
    }
    xhr.onabort = () => {
      cleanup()
      reject(new DOMException('上传已取消', 'AbortError'))
    }
    if (opts?.signal?.aborted) { xhr.abort(); return }
    xhr.send(form)
  })
}

/** 拼查询串：null / undefined / 空串跳过，数组用逗号连起来 */
export function qs(params?: Record<string, unknown>): string {
  if (!params) return ''
  const q = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v == null || v === '') continue
    if (Array.isArray(v)) {
      if (v.length) q.set(k, v.join(','))
    } else {
      q.set(k, String(v))
    }
  }
  const s = q.toString()
  return s ? `?${s}` : ''
}

/**
 * 单次请求的选项。timeoutMs 到点就掐断并按网络失败抛出（带 timeoutMs）：连得上但
 * 卡着不回的接口（MCP 服务不应答时的 /tools）不能让调用方永远等下去，也不该一直
 * 占着浏览器对同一主机的那几条连接
 */
export interface RequestOptions {
  timeoutMs?: number
  signal?: AbortSignal
}

export const get = <T>(p: string, opts?: RequestOptions) => request<T>(p, opts)
export const post = <T>(p: string, body?: unknown) =>
  request<T>(p, { method: 'POST', body: JSON.stringify(body ?? {}) })
export const patch = <T>(p: string, body: unknown) =>
  request<T>(p, { method: 'PATCH', body: JSON.stringify(body) })
export const put = <T>(p: string, body: unknown) =>
  request<T>(p, { method: 'PUT', body: JSON.stringify(body) })
export const del = (p: string) => request<void>(p, { method: 'DELETE' })
