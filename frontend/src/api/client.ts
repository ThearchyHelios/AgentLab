import type {
  Approval, Conversation, ConversationDetail, ConversationTurn, GraphSpec,
  KbDocument, MemoryItem, Provider, ReviewResult, Run, RunEvent, RunStatus, Skill, ToolInfo,
  ValidationIssue, VarIssue, Variable, Workflow, WorkflowVersion,
} from '../types'

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

  constructor(
    public status: number,
    message: string,
    opts?: { kind?: ApiErrorKind; detail?: unknown; raw?: string; timeoutMs?: number },
  ) {
    super(message)
    this.name = 'ApiError'
    this.kind = opts?.kind ?? (status === 0 ? 'network' : 'http')
    this.detail = opts?.detail
    this.raw = opts?.raw
    this.timeoutMs = opts?.timeoutMs
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
  try {
    const actor = localStorage.getItem('agentlab_actor')
    // 请求头只能是 Latin-1：中文署名原样放进去，fetch 直接抛错、整个请求发不出去。
    // 后端 runs.actor_of 解码，纯 ASCII 的老署名编码前后一样
    return actor ? { 'X-Actor': encodeURIComponent(actor) } : {}
  } catch {
    return {}
  }
}

/** FastAPI 的 422 是 [{loc, msg, type}] 数组，直接 String() 会变成 [object Object] */
function describeDetail(detail: unknown): string {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    const parts = detail.map((d: any) => {
      const loc = Array.isArray(d?.loc) ? d.loc.filter((x: unknown) => x !== 'body' && x !== 'query').join('.') : ''
      return loc ? `${loc}：${d?.msg ?? ''}` : String(d?.msg ?? JSON.stringify(d))
    })
    return `提交的内容不符合要求：${parts.join('；')}`
  }
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
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    let body: any
    let isJson = false
    try { body = JSON.parse(text); isJson = true } catch { /* 响应体不是 JSON */ }
    if (isGatewayFailure(res.status, text, isJson)) {
      const err = new ApiError(res.status, NETWORK_MESSAGE, {
        kind: 'network', raw: `${res.status} ${res.statusText}${text ? `\n${text.slice(0, 500)}` : ''}`,
      })
      report(false, err)
      throw err
    }
    report(true)
    if (isJson) {
      const detail = body?.detail ?? body
      throw new ApiError(res.status, describeDetail(detail), {
        detail, raw: typeof body?.raw === 'string' ? body.raw : `${res.status} ${text.slice(0, 2000)}`,
      })
    }
    const message = res.status >= 500
      ? `后端出错了（${res.status}），详情看服务日志`
      : `请求没有成功（${res.status} ${res.statusText}）`
    throw new ApiError(res.status, message, { raw: `${res.status} ${res.statusText}\n${text.slice(0, 2000)}` })
  }
  report(true)
  if (res.status === 204) return undefined as T
  return res.json()
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

const get = <T>(p: string) => request<T>(p)
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
    list: () => get<Workflow[]>('/workflows'),
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
    list: () => get<any[]>('/datasources'),
    kinds: () => get<any>('/datasources/kinds'),
    create: (body: any) => post<any>('/datasources', body),
    update: (id: string, body: any) => patch<any>(`/datasources/${id}`, body),
    remove: (id: string) => del(`/datasources/${id}`),
    test: (id: string) => post<ConnectionTest>(`/datasources/${id}/test`, {}),
    /** 测一份还没保存的配置，只测连接不落库。编辑时带上 id，后端可以沿用已存的密码 */
    testConfig: (body: any) => post<ConnectionTest>('/datasources/test', body),
    /** 上传 Excel / CSV，变成一个可以用 SQL 查的数据源。同名就地替换 */
    uploadTable: (file: File, body: { name: string; description?: string; header_row?: number }) => {
      const form = new FormData()
      form.append('file', file)
      form.append('name', body.name)
      form.append('description', body.description ?? '')
      form.append('header_row', String(body.header_row ?? 1))
      return request<{
        source: any; replaced: boolean
        tables: { name: string; sheet: string; rows: number
                  columns: { name: string; type: string }[] }[]
      }>('/datasources/upload', { method: 'POST', body: form })
    },
    introspect: (id: string, schema?: string) =>
      post<any>(`/datasources/${id}/introspect${schema ? `?schema=${encodeURIComponent(schema)}` : ''}`, {}),
    schema: (id: string, table?: string) =>
      get<any>(`/datasources/${id}/schema${table ? `?table=${encodeURIComponent(table)}` : ''}`),
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
    cancel: (id: string) => post<{ ok: boolean }>(`/runs/${id}/cancel`),
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
    list: (params: string | { status?: string; run_id?: string; limit?: number } = 'pending') =>
      get<Approval[]>(`/approvals${qs(typeof params === 'string' ? { status: params } : { status: 'pending', ...params })}`),
    decide: (id: string, body: { approved: boolean; note?: string; value?: any; args?: any }) =>
      post<Run>(`/approvals/${id}/decide`, body),
  },

  // ---- 设置 ----
  providers: {
    list: () => get<Provider[]>('/providers'),
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
    list: () => get<ToolInfo[]>('/tools'),
    /** 危险工具要 confirm，否则后端回 409，detail 写明它会做什么 */
    run: (name: string, args: Record<string, any>, opts?: { confirm?: boolean }) =>
      post<any>(`/tools/${name}/run`, opts?.confirm ? { args, confirm: true } : { args }),
  },
  customTools: {
    list: () => get<any[]>('/custom-tools'),
    create: (body: any) => post<any>('/custom-tools', body),
    update: (id: string, body: any) => patch<any>(`/custom-tools/${id}`, body),
    remove: (id: string) => del(`/custom-tools/${id}`),
    test: (id: string, args: Record<string, any>) => post<any>(`/custom-tools/${id}/test`, { args }),
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
    collections: () => get<{ collection: string; documents: number; chunks: number }[]>('/kb/collections'),
    documents: (collection?: string) =>
      get<KbDocument[]>(`/kb/documents${collection ? `?collection=${collection}` : ''}`),
    ingest: (body: { collection?: string; title?: string; content: string; source?: string }) =>
      post<KbDocument>('/kb/documents', body),
    upload: (file: File, collection = 'default') => {
      const form = new FormData()
      form.append('file', file)
      return request<KbDocument>(`/kb/upload?collection=${collection}`, { method: 'POST', body: form })
    },
    search: (q: string, collection?: string, alpha = 0.5) =>
      get<any & { alpha?: number }>(`/kb/search?q=${encodeURIComponent(q)}&alpha=${alpha}${collection ? `&collection=${collection}` : ''}`),
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
    reindex: (collection?: string) =>
      post<{ reindexed: number; memories_reindexed: number; embedder: string }>(
        `/kb/reindex${collection ? `?collection=${collection}` : ''}`),
  },
  skills: {
    list: () => get<Skill[]>('/skills'),
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

  // ---- 会话 ----
  conversations: {
    list: (kind: 'chat' | 'canvas' = 'chat', workflowId?: string) =>
      get<Conversation[]>(
        `/conversations?kind=${kind}` + (workflowId ? `&workflow_id=${workflowId}` : '')),
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
    }) =>
      post<{ graph: GraphSpec; explanation: string; issues: ValidationIssue[] }>(
        '/copilot/generate', body),
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
  },
  onOp: (op: any) => void,
  onEnd: (error?: string) => void,
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
        onEnd(isJson ? describeDetail(body?.detail ?? body)
          : res.status >= 500 ? `后端出错了（${res.status}），详情看服务日志`
          : `请求没有成功（${res.status} ${res.statusText}）`)
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
