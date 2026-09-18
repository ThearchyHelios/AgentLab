import type {
  Approval, GraphSpec, KbDocument, MemoryItem, Provider, Run, RunEvent,
  Skill, ToolInfo, ValidationIssue, VarIssue, Variable, Workflow,
} from '../types'

const BASE = '/api'

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message)
  }
}

function actorHeader(): Record<string, string> {
  try {
    const actor = localStorage.getItem('agentlab_actor')
    return actor ? { 'X-Actor': actor } : {}
  } catch {
    return {}
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(BASE + path, {
    ...init,
    headers: {
      ...(init?.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
      ...actorHeader(),
      ...init?.headers,
    },
  })
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    try {
      const body = await res.json()
      // FastAPI 的校验错误是数组，直接 String() 会变成 [object Object]
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail ?? body)
    } catch {
      /* 响应体不是 JSON，保留状态行 */
    }
    throw new ApiError(res.status, detail)
  }
  if (res.status === 204) return undefined as T
  return res.json()
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
  health: () => get<{ status: string }>('/health'),
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
    versions: (id: string) => get<any[]>(`/workflows/${id}/versions`),
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
    test: (id: string) => post<any>(`/datasources/${id}/test`, {}),
    introspect: (id: string, schema?: string) =>
      post<any>(`/datasources/${id}/introspect${schema ? `?schema=${encodeURIComponent(schema)}` : ''}`, {}),
    schema: (id: string, table?: string) =>
      get<any>(`/datasources/${id}/schema${table ? `?table=${encodeURIComponent(table)}` : ''}`),
  },

  runs: {
    list: (params?: { workflow_id?: string; status?: string; limit?: number }) => {
      const q = new URLSearchParams(
        Object.entries(params ?? {}).filter(([, v]) => v != null) as [string, string][],
      )
      return get<Run[]>(`/runs${q.toString() ? `?${q}` : ''}`)
    },
    get: (id: string) => get<Run>(`/runs/${id}`),
    start: (body: {
      workflow_id?: string; graph?: GraphSpec; input?: Record<string, any>
      memory_scope?: string; collection?: string
      run_class?: 'formal' | 'exploratory'; version?: number
    }) => post<Run>('/runs', body),
    artifacts: (id: string) => get<any[]>(`/runs/${id}/artifacts`),
    cancel: (id: string) => post<{ ok: boolean }>(`/runs/${id}/cancel`),
    resume: (id: string, response: any) => post<Run>(`/runs/${id}/resume`, { response }),
    events: (id: string, after = 0) => get<RunEvent[]>(`/runs/${id}/events?after=${after}`),
    state: (id: string) => get<any>(`/runs/${id}/state`),
    history: (id: string) => get<any[]>(`/runs/${id}/history`),
    remove: (id: string) => del(`/runs/${id}`),
  },

  approvals: {
    list: (status = 'pending') => get<Approval[]>(`/approvals?status=${status}`),
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
      post<any>(`/providers/${id}/test`, body),
  },
  settings: {
    get: () => get<Record<string, any>>('/settings'),
    put: (values: Record<string, any>) => put<Record<string, any>>('/settings', { values }),
  },

  // ---- 工具 ----
  tools: {
    list: () => get<ToolInfo[]>('/tools'),
    run: (name: string, args: Record<string, any>) =>
      post<any>(`/tools/${name}/run`, { args }),
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
    search: (q: string, scope = 'default') =>
      get<any>(`/memory/search?q=${encodeURIComponent(q)}&scope=${scope}`),
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
      get<any>(`/kb/search?q=${encodeURIComponent(q)}&alpha=${alpha}${collection ? `&collection=${collection}` : ''}`),
    remove: (id: string) => del(`/kb/documents/${id}`),
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

  // ---- Copilot ----
  copilot: {
    generate: (body: { instruction: string; base_graph?: GraphSpec | null }) =>
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
  },
}

/**
 * 流式生成工作流：SSE 逐操作回调，返回取消函数。
 * 每个操作（add_node / add_edge / …）到达即回调，画布边收边长。
 */
export function streamCopilot(
  body: { instruction: string; base_graph?: GraphSpec | null; provider?: string | null; model?: string | null },
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
        let detail = `${res.status} ${res.statusText}`
        try { detail = (await res.json()).detail ?? detail } catch { /* keep */ }
        onEnd(String(detail))
        return
      }
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
      if (e?.name !== 'AbortError') onEnd(e?.message ?? '连接中断')
      else onEnd()
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
  onClose?: () => void,
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
      const event = JSON.parse(ev.data) as RunEvent & { type: string }
      if (event.type === 'stream.end') {
        closed = true
        socket?.close()
        onClose?.()
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
    }
  }
  connect()

  return () => {
    closed = true
    socket?.close()
  }
}
