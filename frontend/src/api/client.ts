import type {
  Approval, GraphSpec, KbDocument, MemoryItem, Provider, Run, RunEvent,
  Skill, ToolInfo, ValidationIssue, Workflow,
} from '../types'

const BASE = '/api'

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message)
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(BASE + path, {
    ...init,
    headers: {
      ...(init?.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
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
    validate: (graph: GraphSpec) =>
      post<{ ok: boolean; issues: ValidationIssue[] }>('/workflows/validate', { graph }),
  },

  // ---- 运行 ----
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
    }) => post<Run>('/runs', body),
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

  // ---- Copilot ----
  copilot: {
    generate: (body: { instruction: string; base_graph?: GraphSpec | null }) =>
      post<{ graph: GraphSpec; explanation: string; issues: ValidationIssue[] }>(
        '/copilot/generate', body),
    explain: (graph: GraphSpec) => post<{ explanation: string }>('/copilot/explain', { graph }),
    layout: (graph: GraphSpec) => post<GraphSpec>('/copilot/layout', { graph }),
  },
}

/**
 * 订阅一次运行的事件流。
 *
 * 先补历史再接实时由后端保证，这里只负责断线重连 —— 重连时带上已收到的最大
 * seq，所以不会重复也不会丢事件。
 */
export function streamRun(
  runId: string,
  onEvent: (event: RunEvent) => void,
  onClose?: () => void,
): () => void {
  let socket: WebSocket | null = null
  let closed = false
  let lastSeq = 0
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
