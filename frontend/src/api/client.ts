import type {
  Approval, Conversation, ConversationDetail, ConversationTurn, CustomTool, DataSource, EvidenceGraph,
  EvidenceSegmentDetail, GraphSpec, KbDocument, MemoryItem, Provider, ReviewResult, Run, RunEvent, RunStatus,
  Skill, ToolChange, ToolInfo, ValidationIssue, VarIssue, Variable, Workflow, WorkflowVersion,
} from '../types'
import { localActor } from '../lib/actor'
import { VALIDATION_TITLE, describeValidation } from '../lib/validation'

const BASE = '/api'

/**
 * network：请求根本没到后端（没起、在重启、代理断了、超时没回）。
 * http：后端回了错误状态码，message 是后端的 detail。
 */
export type ApiErrorKind = 'network' | 'http'

export const NETWORK_MESSAGE = '连不上后端服务（可能没启动或正在重启），稍后重试'

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

  constructor(
    public status: number,
    message: string,
    opts?: { kind?: ApiErrorKind; detail?: unknown; raw?: string; timeoutMs?: number; code?: string },
  ) {
    super(message)
    this.name = 'ApiError'
    this.kind = opts?.kind ?? (status === 0 ? 'network' : 'http')
    this.detail = opts?.detail
    this.raw = opts?.raw
    this.timeoutMs = opts?.timeoutMs
    this.code = opts?.code
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

function report(reachable: boolean, error?: ApiError) {
  for (const l of connectivity) {
    try { l(reachable, error) } catch { /* 旁听者出错不能拖垮请求本身 */ }
  }
}

function actorHeader(): Record<string, string> {
  const actor = localActor()
  // 请求头只能是 Latin-1：中文署名原样放进去，fetch 直接抛错、整个请求发不出去。
  // 后端 runs.actor_of 解码，纯 ASCII 的老署名编码前后一样
  return actor ? { 'X-Actor': encodeURIComponent(actor) } : {}
}

/**
 * FastAPI 的 422 是 [{loc, msg, type}] 数组：直接 String() 是 [object Object]，照拼 msg
 * 又是 pydantic 的英文原文。按 type 翻成中文（lib/validation），path 用来查表单上的叫法
 */
function describeDetail(detail: unknown, path?: string): string {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return `${VALIDATION_TITLE}：${describeValidation(detail, path).join('；')}`
  try { return JSON.stringify(detail) } catch { return String(detail) }
}

/**
 * 网关类失败（代理够不着后端）也算网络失败：vite 代理连不上后端时回 500 且响应体
 * 为空，nginx 回 502/503/504 的 HTML。后端自己抛的 HTTPException 一定带 JSON detail。
 */
function isGatewayFailure(status: number, bodyText: string, isJson: boolean): boolean {
  if (isJson) return false
  if (status === 502 || status === 503 || status === 504) return true
  return status === 500 && !bodyText.trim()
}

async function request<T>(path: string, init?: RequestInit & { timeoutMs?: number }): Promise<T> {
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
      const err = new ApiError(0, `后端 ${Math.round(timeoutMs! / 1000)} 秒没有响应，稍后重试`,
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
  return res.json()
}

/** 后端 {detail, code} 里的机读码；没有就是 undefined */
const codeOf = (body: unknown): string | undefined => {
  const code = body && typeof body === 'object' ? (body as { code?: unknown }).code : undefined
  return typeof code === 'string' && code ? code : undefined
}

/** 非 2xx 的响应 → ApiError，顺带报告连接状态。fetch 和 XHR 两条路共用 */
function failure(status: number, statusText: string, text: string, path?: string): ApiError {
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
  if (isJson) {
    const detail = body?.detail ?? body
    return new ApiError(status, describeDetail(detail, path), {
      detail, raw: typeof body?.raw === 'string' ? body.raw : `${status} ${text.slice(0, 2000)}`,
      code: codeOf(body),
    })
  }
  const message = status >= 500
    ? `后端出错了（${status}），详情看服务日志`
    : `请求没有成功（${status} ${statusText}）`
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
function upload<T>(path: string, form: FormData, opts?: UploadOptions): Promise<T> {
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
        resolve(JSON.parse(xhr.responseText))
      } catch {
        reject(new ApiError(xhr.status, '后端回的内容读不懂', { raw: xhr.responseText.slice(0, 2000) }))
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

/**
 * 数据源测连接的结果。ok=false 时 error 是一句人话、hint 是怎么办、detail 是
 * 驱动的原始报错（放进「技术细节」）。
 */
export interface ConnectionTest {
  ok: boolean
  elapsed_ms?: number
  url?: string
  error?: string
  hint?: string
  detail?: string
  [key: string]: any
}

/** 模型接入测试的结果，失败时的字段同 ConnectionTest */
export interface ProviderTest {
  ok: boolean
  latency_ms?: number
  model?: string | null
  reply?: string
  usage?: Record<string, any>
  error?: string
  hint?: string
  detail?: string
  [key: string]: any
}

/** 还没保存的模型接入配置。带 id 表示在编辑已有的那个，api_key 留空沿用已存的 */
export interface ProviderDraft {
  id?: string
  name?: string
  kind: string
  base_url?: string | null
  api_key?: string | null
  models?: { id: string; label?: string; context?: number; pricing?: any }[]
  default_model?: string | null
  extra?: Record<string, any>
}

/** 探查结构的预览（dry_run）：只看不存，缓存和同步时间都没动 */
export interface IntrospectPreview {
  dry_run: true
  /** 这次探的是哪个 schema，'' 是默认 schema */
  schema: string
  table_count: number
  /** 带 schema 前缀的对象全名，最多 200 个 */
  tables: string[]
  truncated: boolean
  total: number
  schema_error: string
  available_schemas: string[]
}

/** 一张表的结构化列信息（和给模型看的 detail 文本出自同一份缓存） */
export interface SchemaColumn {
  name: string
  type: string
  pk: boolean
  not_null: boolean
  comment: string | null
}

export interface TableSchema {
  table: string
  /** 给模型看的那段文本 */
  detail: string
  /** 缓存里找没找到这张表。老后端没有这个字段：只有明确的 false 才算找不到 */
  found?: boolean
  qualified: string | null
  kind: 'table' | 'view' | null
  comment: string | null
  /** 老后端没有，只有 detail */
  columns?: SchemaColumn[]
}

/** 知识库检索的一条命中 */
export interface KbHit {
  chunk_id: string
  document_id: string
  title: string
  ordinal: number
  content: string
  score: number
  signals?: Record<string, number>
  /** 向量、关键词两路各贡献了多少分，两者之和等于 score。老后端不给 */
  contrib?: { vector: number; keyword: number }
  [key: string]: any
}

export interface KbSearchResult {
  query: string
  collection: string | null
  results: KbHit[]
  /** 这次检索降级了（比如退回纯关键词）的说明 */
  degraded: string[]
  /** 实际用的混合权重 */
  alpha?: number
  [key: string]: any
}

/**
 * 后台重建索引的进度。phase：chunks（知识库切块）→ index（倒排，没有分批进度）→
 * memories（长期记忆）→ done。total / done 是切块加记忆的合计
 */
export interface ReindexJob {
  id: string
  state: 'running' | 'done' | 'failed'
  phase: 'chunks' | 'index' | 'memories' | 'done'
  collection: string | null
  total: number
  done: number
  chunks: { total: number; done: number }
  memories: { total: number; done: number }
  started_at: string
  finished_at: string | null
  error: string | null
  hint: string | null
  result: { reindexed: number; memories_reindexed: number; embedder: string; [key: string]: any } | null
}

/** runs.list 的 workflow_id 传它：只看没保存成工作流的那些运行（问数据页、画布上的临时图） */
export const UNSAVED_WORKFLOW_ID = '__none__'

/** 拼查询串：null / undefined / 空串跳过，数组用逗号连起来 */
function qs(params?: Record<string, unknown>): string {
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

const get = <T>(p: string, opts?: RequestOptions) => request<T>(p, opts)
const post = <T>(p: string, body?: unknown) =>
  request<T>(p, { method: 'POST', body: JSON.stringify(body ?? {}) })
const patch = <T>(p: string, body: unknown) =>
  request<T>(p, { method: 'PATCH', body: JSON.stringify(body) })
const put = <T>(p: string, body: unknown) =>
  request<T>(p, { method: 'PUT', body: JSON.stringify(body) })
const del = (p: string) => request<void>(p, { method: 'DELETE' })

export const api = {
  /** 心跳。带超时：后端卡死（连得上但不回）时也要能判成断开，而不是永远等下去 */
  health: (timeoutMs = 5000) => request<{ status: string; service?: string }>('/health', { timeoutMs }),
  system: () => get<any>('/system'),

  // ---- 工作流 ----
  workflows: {
    list: (opts?: RequestOptions) => get<Workflow[]>('/workflows', opts),
    get: (id: string) => get<Workflow>(`/workflows/${id}`),
    create: (body: { name: string; description?: string; graph?: GraphSpec; tags?: string[] }) =>
      post<Workflow>('/workflows', body),
    update: (id: string, body: Partial<Workflow> & { note?: string }) =>
      patch<Workflow>(`/workflows/${id}`, body),
    remove: (id: string) => del(`/workflows/${id}`),
    duplicate: (id: string) => post<Workflow>(`/workflows/${id}/duplicate`),
    versions: (id: string) => get<WorkflowVersion[]>(`/workflows/${id}/versions`),
    /** 某个版本的完整快照（含 graph）。正式运行的输入字段要从已发布版本取，不能取画布 */
    version: (id: string, v: number) => get<WorkflowVersion>(`/workflows/${id}/versions/${v}`),
    restore: (id: string, v: number) => post<Workflow>(`/workflows/${id}/versions/${v}/restore`),
    publish: (id: string, level: 'published' | 'governed', version?: number) =>
      post<{ ok: boolean; level?: string; version?: number; issues: any[] }>(
        `/workflows/${id}/publish`, { level, version }),
    validate: (graph: GraphSpec) =>
      post<{ ok: boolean; issues: ValidationIssue[] }>('/workflows/validate', { graph }),
    /** 这张图里有哪些变量、谁产出、谁引用。纯静态分析，不需要跑过 */
    variables: (graph: GraphSpec) =>
      post<{ variables: Variable[]; issues: VarIssue[] }>('/workflows/variables', { graph }),
  },

  // ---- 运行 ----
  datasources: {
    list: (opts?: RequestOptions) => get<DataSource[]>('/datasources', opts),
    kinds: () => get<any>('/datasources/kinds'),
    create: (body: any) => post<any>('/datasources', body),
    update: (id: string, body: any) => patch<any>(`/datasources/${id}`, body),
    remove: (id: string) => del(`/datasources/${id}`),
    test: (id: string) => post<ConnectionTest>(`/datasources/${id}/test`, {}),
    /** 测一份还没保存的配置，只测连接不落库。编辑时带上 id，后端可以沿用已存的密码 */
    testConfig: (body: any) => post<ConnectionTest>('/datasources/test', body),
    /** 上传 Excel / CSV，变成一个可以用 SQL 查的数据源。同名就地替换。onProgress 报字节进度 */
    uploadTable: (file: File, body: { name: string; description?: string; header_row?: number }, opts?: UploadOptions) => {
      const form = new FormData()
      form.append('file', file)
      form.append('name', body.name)
      form.append('description', body.description ?? '')
      form.append('header_row', String(body.header_row ?? 1))
      type Out = {
        source: any; replaced: boolean
        tables: { name: string; sheet: string; rows: number
                  columns: { name: string; type: string }[] }[]
      }
      return opts?.onProgress
        ? upload<Out>('/datasources/upload', form, opts)
        : request<Out>('/datasources/upload', { method: 'POST', body: form, signal: opts?.signal })
    },
    introspect: (id: string, schema?: string) =>
      post<any>(`/datasources/${id}/introspect${schema ? `?schema=${encodeURIComponent(schema)}` : ''}`, {}),
    /** 换个 schema 看看：只返回探到的结果，不覆盖缓存（Copilot 看到的还是原来那份） */
    previewSchema: (id: string, schema?: string) =>
      post<IntrospectPreview>(`/datasources/${id}/introspect${qs({ schema, dry_run: 'true' })}`, {}),
    schema: (id: string, table?: string) =>
      get<any>(`/datasources/${id}/schema${table ? `?table=${encodeURIComponent(table)}` : ''}`),
    /** 一张表的结构，带结构化的列信息，不用再去解析 detail 文本 */
    tableSchema: (id: string, table: string) =>
      get<TableSchema>(`/datasources/${id}/schema?table=${encodeURIComponent(table)}`),
  },

  runs: {
    /**
     * status 可以给多个（逗号连接）；q 按工作流名模糊匹配；before 是翻页游标
     * （上一页最后一条的 created_at）；limit 上限 200。老后端不认的参数会被忽略。
     */
    list: (params?: {
      workflow_id?: string; status?: RunStatus | RunStatus[] | string; run_class?: 'formal' | 'exploratory'
      q?: string; limit?: number; before?: string
    }) => get<Run[]>(`/runs${qs(params)}`),
    get: (id: string) => get<Run>(`/runs/${id}`),
    /** 这次运行实际跑的那张图（运行时的快照），不是工作流现在的样子 */
    graph: (id: string) => get<{ graph: GraphSpec; workflow_id: string | null; version: number | null }>(
      `/runs/${id}/graph`),
    start: (body: {
      workflow_id?: string; graph?: GraphSpec; input?: Record<string, any>
      memory_scope?: string; collection?: string
      run_class?: 'formal' | 'exploratory'; version?: number
    }) => post<Run>('/runs', body),
    artifacts: (id: string) => get<any[]>(`/runs/${id}/artifacts`),
    /** 从失败的节点接着跑。graph 只能带改过配置的同一张图，结构必须一致 */
    continue: (id: string, graph?: GraphSpec | null) =>
      post<Run>(`/runs/${id}/continue`, { graph: graph ?? null }),
    // 等审批的运行直接收成 cancelled；在跑的要等引擎收尾，先回 stopping
    cancel: (id: string) => post<{ ok: boolean; status?: 'stopping' | 'cancelled' }>(`/runs/${id}/cancel`),
    resume: (id: string, response: any) => post<Run>(`/runs/${id}/resume`, { response }),
    events: (id: string, after = 0) => get<RunEvent[]>(`/runs/${id}/events?after=${after}`),
    state: (id: string) => get<any>(`/runs/${id}/state`),
    history: (id: string) => get<any[]>(`/runs/${id}/history`),
    /** 核对事件流和封存时的清单哈希：事后被改过、删过、插过都会对不上 */
    verify: (id: string) => get<{ sealed: boolean; ok: boolean | null; message: string }>(
      `/runs/${id}/verify`),
    /** 运行中的删不了（409）；封存过的正式运行要 force，否则也是 409，detail 写明后果 */
    remove: (id: string, opts?: { force?: boolean }) =>
      del(`/runs/${id}${opts?.force ? '?force=true' : ''}`),
  },

  approvals: {
    /** 老写法 list('pending') 照旧可用。status 可逗号分隔，'all' 取全部 */
    list: (params: string | { status?: string; run_id?: string; limit?: number } = 'pending', opts?: RequestOptions) =>
      get<Approval[]>(`/approvals${qs(typeof params === 'string' ? { status: params } : { status: 'pending', ...params })}`, opts),
    decide: (id: string, body: { approved: boolean; note?: string; value?: any; args?: any }) =>
      post<Run>(`/approvals/${id}/decide`, body),
  },

  // ---- 设置 ----
  providers: {
    list: (opts?: RequestOptions) => get<Provider[]>('/providers', opts),
    catalog: () => get<any>('/providers/catalog'),
    create: (body: any) => post<Provider>('/providers', body),
    update: (id: string, body: any) => patch<Provider>(`/providers/${id}`, body),
    remove: (id: string) => del(`/providers/${id}`),
    test: (id: string, body: { model?: string; prompt?: string }) =>
      post<ProviderTest>(`/providers/${id}/test`, body),
    /** 测一份还没保存的接入配置，不落库。model 不填用 default_model */
    testConfig: (body: ProviderDraft & { model?: string | null; prompt?: string }) =>
      post<ProviderTest>('/providers/test', body),
    /** 问这个接入点有哪些模型（带鉴权调 /v1/models），省得手敲模型 id */
    models: (body: ProviderDraft) =>
      post<{ ok: boolean; models: string[]; url?: string; error?: string; hint?: string; detail?: string }>(
        '/providers/models', body),
  },
  settings: {
    get: () => get<Record<string, any>>('/settings'),
    put: (values: Record<string, any>) => put<Record<string, any>>('/settings', { values }),
  },

  // ---- 工具 ----
  tools: {
    list: (opts?: RequestOptions) => get<ToolInfo[]>('/tools', opts),
    /** 危险工具要 confirm，否则后端回 409，detail 写明它会做什么 */
    run: (name: string, args: Record<string, any>, opts?: { confirm?: boolean }) =>
      post<any>(`/tools/${name}/run`, opts?.confirm ? { args, confirm: true } : { args }),
  },
  customTools: {
    /** 每行带 problem：库里存着的参数定义写坏了时是一句中文，绑了它的节点一定失败 */
    list: () => get<CustomTool[]>('/custom-tools'),
    /** 参数定义写坏了回 422，detail 是一句中文（ApiError.message），显示在参数字段下面 */
    create: (body: any) => post<CustomTool>('/custom-tools', body),
    update: (id: string, body: any) => patch<CustomTool>(`/custom-tools/${id}`, body),
    remove: (id: string) => del(`/custom-tools/${id}`),
    test: (id: string, args: Record<string, any>) => post<any>(`/custom-tools/${id}/test`, { args }),
    /** 试跑一份还没保存的配置，不落库。失败时回 {ok:false, error, hint, detail, duration_ms} */
    testDraft: (body: {
      name?: string; kind: string; parameters?: Record<string, any>; config?: Record<string, any>
      args?: Record<string, any>
    }) => post<any>('/custom-tools/test', body),
  },
  mcp: {
    list: () => get<any[]>('/mcp/servers'),
    create: (body: any) => post<any>('/mcp/servers', body),
    update: (id: string, body: any) => patch<any>(`/mcp/servers/${id}`, body),
    remove: (id: string) => del(`/mcp/servers/${id}`),
    probe: (id: string) => post<any>(`/mcp/servers/${id}/probe`),
    refresh: () => post<any>('/mcp/refresh'),
  },

  // ---- 沙箱 ----
  sandbox: {
    health: () => get<any>('/sandbox/health'),
    exec: (body: { code: string; language?: string; timeout?: number; network?: boolean; memory_mb?: number }) =>
      post<any>('/sandbox/exec', body),
  },

  // ---- 记忆 / 知识库 / Skill ----
  memory: {
    scopes: () => get<{ scope: string; count: number }[]>('/memory/scopes'),
    list: (scope?: string) => get<MemoryItem[]>(`/memory${scope ? `?scope=${scope}` : ''}`),
    add: (body: { content: string; scope?: string; kind?: string; importance?: number }) =>
      post<MemoryItem>('/memory', body),
    search: (q: string, scope = 'default', opts?: { peek?: boolean; limit?: number }) =>
      get<any>(`/memory/search${qs({ q, scope, peek: opts?.peek ? 'true' : undefined, limit: opts?.limit })}`),
    /**
     * 回忆调试：默认 peek，只看不计数。真实召回会给命中项 use_count += 1 并影响
     * 下次排序，调试台点几下就会把「被召回 N 次」抬高
     */
    recall: (q: string, opts?: { scope?: string; peek?: boolean; limit?: number }) =>
      get<any>(`/memory/search${qs({
        q, scope: opts?.scope ?? 'default', peek: (opts?.peek ?? true) ? 'true' : undefined, limit: opts?.limit,
      })}`),
    /** 原地修改，保留来源和召回记录 */
    update: (id: string, body: { content?: string; importance?: number; kind?: string }) =>
      patch<MemoryItem>(`/memory/${id}`, body),
    remove: (id: string) => del(`/memory/${id}`),
    clearScope: (scope: string) => del(`/memory/scope/${scope}`),
  },
  kb: {
    collections: (opts?: RequestOptions) =>
      get<{ collection: string; documents: number; chunks: number }[]>('/kb/collections', opts),
    documents: (collection?: string) =>
      get<KbDocument[]>(`/kb/documents${collection ? `?collection=${collection}` : ''}`),
    ingest: (body: { collection?: string; title?: string; content: string; source?: string }) =>
      post<KbDocument>('/kb/documents', body),
    /**
     * 上传一份文档。给了 onProgress 就报真实的字节进度（XHR）；字节发完以后是后端
     * 在解析、切块，那段没有进度，调用方写「处理中」，不要把条拉满冒充完成
     */
    upload: (file: File, collection = 'default', opts?: UploadOptions) => {
      const form = new FormData()
      form.append('file', file)
      const path = `/kb/upload?collection=${encodeURIComponent(collection)}`
      return opts?.onProgress
        ? upload<KbDocument>(path, form, opts)
        : request<KbDocument>(path, { method: 'POST', body: form, signal: opts?.signal })
    },
    search: (q: string, collection?: string, alpha = 0.5) =>
      get<KbSearchResult>(`/kb/search?q=${encodeURIComponent(q)}&alpha=${alpha}${collection ? `&collection=${encodeURIComponent(collection)}` : ''}`),
    /** 上传框能收哪些文件。以后端解析器为准，免得界面放行了后端必拒的格式 */
    formats: () => get<{
      extensions: string[]; text: string[]; tabular: string[]; legacy: string[]; accept: string
    }>('/kb/formats'),
    /** 一份文档被切成了什么样。检索不准时第一个该看的就是它 */
    document: (id: string) => get<{
      document: KbDocument
      chunks: {
        id: string; ordinal: number; content: string; truncated: boolean
        chars: number; token_len: number; embed_model: string; has_vector: boolean
      }[]
    }>(`/kb/documents/${id}`),
    remove: (id: string) => del(`/kb/documents/${id}`),
    embedding: (collection?: string) => get<{
      embedder: string; dim: number; configured: boolean
      kind: string; model: string; base_url: string
      stale_chunks: number; stale_memories: number; unindexed_chunks: number
      // has_semantics 看的是实际在用的那个；fallback = 配了语义模型却退回了本地哈希
      has_semantics: boolean; fallback: boolean; fallback_reason: string
      /** 运行时 kb_search 实际用的混合权重。调试台的滑块初值要跟它一致 */
      default_alpha?: number
    }>(`/kb/embedding${collection ? `?collection=${collection}` : ''}`),
    setEmbedding: (body: { kind: string; model?: string; base_url?: string }) =>
      put<{
        embedder: string; dim: number
        stale_chunks: number; stale_memories: number
      }>('/kb/embedding', body),
    probeEmbedding: (baseUrl: string) =>
      get<{ base_url: string; models: string[] }>(
        `/kb/embedding/probe?base_url=${encodeURIComponent(baseUrl)}`),
    /** 等做完再返回。几千段配远端 embedding 要好几分钟，界面上用 startReindex */
    reindex: (collection?: string) =>
      post<{ reindexed: number; memories_reindexed: number; embedder: string }>(
        `/kb/reindex${qs({ collection })}`),
    /**
     * 后台重建，立刻返回任务；进度用 reindexStatus 轮询。已经有一次在跑时回 409，
     * detail 写着进度
     */
    startReindex: (collection?: string) =>
      post<ReindexJob>(`/kb/reindex${qs({ collection, background: 'true' })}`),
    /** 最近一次重建的进度。进程里只记最近一次，还没重建过是 {state:'idle'} */
    reindexStatus: () => get<ReindexJob | { state: 'idle' }>('/kb/reindex'),
  },
  skills: {
    list: (opts?: RequestOptions) => get<Skill[]>('/skills', opts),
    create: (body: Partial<Skill>) => post<Skill>('/skills', body),
    update: (id: string, body: Partial<Skill>) => patch<Skill>(`/skills/${id}`, body),
    remove: (id: string) => del(`/skills/${id}`),
  },

  // ---- 治理 ----
  governance: {
    toolUsage: (workflowId: string) =>
      get<any>(`/governance/tool-usage?workflow_id=${workflowId}`),
    clusters: () => get<any>('/governance/exploratory-clusters'),
  },
  artifact: (id: string) => get<{ id: string; content: any }>(`/artifacts/${id}`),

  // ---- 可点击证据 ----
  // 只认封存范围内的事件追得到的工件（后端 api/evidence.py）。老后端没有这两个接口，调用方按 404 降级
  evidence: {
    /** 整次运行的证据图：模式（cited / legacy_contract / none）、封存状态、报告和证据清单 */
    graph: (runId: string, opts?: RequestOptions) =>
      get<EvidenceGraph>(`/runs/${encodeURIComponent(runId)}/evidence`, opts),
    /**
     * 点开一个片段：所在的句子、指标步骤（原式、代入式、输入、复算）、封存状态。一次运行
     * 里有好几份报告时 report 给报告节点 id（片段 id 每份文档各自从 s0 数起），不给取成果标注的那份
     */
    segment: (runId: string, segmentId: string, opts?: RequestOptions & { report?: string }) =>
      get<EvidenceSegmentDetail>(
        `/runs/${encodeURIComponent(runId)}/evidence/segments/${encodeURIComponent(segmentId)}${qs({ report: opts?.report })}`,
        opts),
  },

  // ---- 会话 ----
  conversations: {
    /** includeArchived：连回收站里的一起取（每行的 archived 区分），回收站视图要它 */
    list: (kind: 'chat' | 'canvas' = 'chat', workflowId?: string, opts?: { includeArchived?: boolean }) =>
      get<Conversation[]>(
        `/conversations?kind=${kind}`
        + (workflowId ? `&workflow_id=${encodeURIComponent(workflowId)}` : '')
        + (opts?.includeArchived ? '&include_archived=true' : '')),
    create: (body: { kind?: 'chat' | 'canvas'; workflow_id?: string; title?: string }) =>
      post<ConversationDetail>('/conversations', body),
    get: (id: string) => get<ConversationDetail>(`/conversations/${id}`),
    update: (id: string, body: { title?: string; archived?: boolean }) =>
      patch<Conversation>(`/conversations/${id}`, body),
    remove: (id: string) => del(`/conversations/${id}`),
    startTurn: (id: string, question: string) =>
      post<ConversationTurn>(`/conversations/${id}/turns`, { question }),
    patchTurn: (id: string, turnId: string, body: Partial<ConversationTurn>) =>
      patch<ConversationTurn>(`/conversations/${id}/turns/${turnId}`, body),
  },

  // ---- Copilot ----
  copilot: {
    generate: (body: {
      instruction: string; base_graph?: GraphSpec | null
      conversation_id?: string | null
      /** 这一轮只查这几个数据源（id 或名字）。不传或空表示不限 */
      datasource_ids?: string[] | null
    }) =>
      post<{
        graph: GraphSpec; explanation: string; issues: ValidationIssue[]
        layout?: { mode: 'keep' | 'full'; placed: string[] }
        /** 改图前后都在、工具绑定却变了的节点和成员。老后端不给 */
        tool_changes?: ToolChange[]
      }>('/copilot/generate', body),
    explain: (graph: GraphSpec) => post<{ explanation: string }>('/copilot/explain', { graph }),
    layout: (graph: GraphSpec) => post<GraphSpec>('/copilot/layout', { graph }),
    fromRun: (runId: string, name?: string) =>
      post<any>('/copilot/from-run', { run_id: runId, name }),
    getModel: () => get<{
      configured: boolean; provider: string | null; model: string | null
      effective_provider: string | null; effective_model: string | null
    }>('/copilot/model'),
    setModel: (body: { provider?: string | null; model?: string | null }) =>
      put<any>('/copilot/model', body),
    /** 跑完之后复核一次。干净的运行后端直接返回 verdict='ok'，不调模型 */
    review: (body: { run_id: string; question: string }) =>
      post<ReviewResult>('/copilot/review', body),
  },
}

/**
 * 流式生成工作流：SSE 逐操作回调，返回取消函数。
 * 每个操作（add_node / add_edge / …）到达即回调，画布边收边长。
 */
export function streamCopilot(
  body: {
    instruction: string
    base_graph?: GraphSpec | null
    provider?: string | null
    model?: string | null
    /** answer = 用户在问问题（问数据页），build = 用户在描述流程（画布）*/
    intent?: 'build' | 'answer'
    /** 属于哪次对话。带上它，这一轮才知道前面聊过什么 */
    conversation_id?: string | null
    /** 这一轮只查这几个数据源（id 或名字）。不传或空表示不限 */
    datasource_ids?: string[] | null
  },
  // final 操作里带 tool_changes（ToolChange[]）：改图回执要显式列出工具绑定的变化
  onOp: (op: any) => void,
  // 开流之前就被拒（4xx/5xx）时第二个参数带状态码和后端的机读码（{detail, code}），调用方按码分支
  onEnd: (error?: string, info?: { status?: number; code?: string }) => void,
): () => void {
  const controller = new AbortController()
  void (async () => {
    try {
      const res = await fetch(`${BASE}/copilot/generate-stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...actorHeader() },
        body: JSON.stringify(body),
        signal: controller.signal,
      })
      if (!res.ok || !res.body) {
        const text = await res.text().catch(() => '')
        let body: any
        let isJson = false
        try { body = JSON.parse(text); isJson = true } catch { /* 响应体不是 JSON */ }
        if (isGatewayFailure(res.status, text, isJson)) {
          report(false, new ApiError(res.status, NETWORK_MESSAGE, { kind: 'network' }))
          onEnd(NETWORK_MESSAGE)
          return
        }
        report(true)
        onEnd(isJson ? describeDetail(body?.detail ?? body, '/copilot/generate-stream')
          : res.status >= 500 ? `后端出错了（${res.status}），详情看服务日志`
          : `请求没有成功（${res.status} ${res.statusText}）`, { status: res.status, code: isJson ? codeOf(body) : undefined })
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
            onOp(JSON.parse(frame.slice(6)))
          } catch { /* 半截帧，忽略 */ }
        }
      }
      onEnd()
    } catch (e: any) {
      if (e?.name === 'AbortError') { onEnd(); return }
      // 流到一半断了和一开始就连不上，对用户是一回事：后端够不着了
      if (e instanceof TypeError) {
        report(false, new ApiError(0, NETWORK_MESSAGE, { kind: 'network', raw: String(e) }))
        onEnd(NETWORK_MESSAGE)
        return
      }
      onEnd(e?.message ?? '连接中断')
    }
  })()
  return () => controller.abort()
}

/**
 * 订阅一次运行的事件流。
 *
 * 先补历史再接实时由后端保证，这里负责断线重连——重连时带上已收到的最大 seq。
 *
 * `after` 必须由调用方给：lastSeq 是这次调用的闭包局部量，跨调用会归零。
 * 审批恢复时会重新 attachRun，不传 after 就是 after=0，后端按 `seq > after`
 * 把整条历史再推一遍，而消费方多半是无条件追加——事件就这么重复累积了。
 * 已经有事件在手的场景（重新接上一条运行）应当传当前最大 seq。
 */
export function streamRun(
  runId: string,
  onEvent: (event: RunEvent) => void,
  // stream.end 带着运行的最终状态：连到一条已经不在进行中的运行（含 interrupted）
  // 时，客户端靠它知道不会再有事件，不能一直当它还在跑
  onClose?: (end?: { status?: RunStatus | string }) => void,
  after = 0,
): () => void {
  let socket: WebSocket | null = null
  let closed = false
  let lastSeq = after
  let retry = 0

  const connect = () => {
    if (closed) return
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    socket = new WebSocket(`${proto}://${location.host}/api/runs/${runId}/stream?after=${lastSeq}`)

    socket.onmessage = (ev) => {
      const event = JSON.parse(ev.data) as RunEvent & { type: string; status?: string }
      if (event.type === 'stream.end') {
        closed = true
        socket?.close()
        // 新后端放在 data.status，老后端放在顶层 status
        onClose?.({ status: event.data?.status ?? event.status })
        return
      }
      if (event.seq) lastSeq = Math.max(lastSeq, event.seq)
      onEvent(event)
    }
    socket.onclose = () => {
      if (closed) return
      // 指数退避重连，最多等 5 秒
      retry += 1
      setTimeout(connect, Math.min(300 * 2 ** retry, 5000))
    }
    socket.onopen = () => {
      retry = 0
      report(true)
    }
  }
  connect()

  return () => {
    closed = true
    socket?.close()
  }
}
