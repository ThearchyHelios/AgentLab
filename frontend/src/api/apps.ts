/**
 * 应用的接口（/api/apps、/api/ledger），挂在 api.apps 上。形状见 typesApp.ts（应用平台的接口约定）。
 *
 * 请求函数从 ./http 导入，不要从 ./client 导入（循环导入，见 http.ts 开头）。
 * 入口提交校验失败回 422，ApiError 的 body 里带 {errors: FormError[]}。
 */
import type {
  AppBindings, AppDetail, AppGrant, AppHookRow, AppSpec, AppSummary, AppVersionRow, ComponentCatalog, Disposition,
  InstanceRow, LintIssue, PublishResult, ViewModel,
} from '../typesApp'
import type { ConversationDetail, EvidenceSegmentDetail } from '../types'
import { del, get, patch, post, put, qs, type RequestOptions } from './http'

const at = (id: string) => `/apps/${encodeURIComponent(id)}`
const hookAt = (id: string, name: string) => `${at(id)}/hooks/${encodeURIComponent(name)}`

/**
 * 钩子管理里的一行（D-g）：草稿里声明的 ∪ 已经生成过密钥的，按名字合成一行。
 * - declared：草稿的 spec.hooks 里有这个名字；published：当前发布版里有（入站请求按发布版认种类和入口，没有就拒收）；
 * - has_secret：已经生成过密钥（app_hooks 里有这一行）。密钥本身只在生成、轮换时交回一次（AppHookRow.secret）。
 * 服务端几种写法都认（hookStateOf），缺的项按「没有」算
 */
export interface AppHookState {
  name: string
  kind: 'start' | 'signal' | null
  entry_id: string | null
  declared: boolean
  published: boolean
  has_secret: boolean
  enabled: boolean
  /** app_hooks 那一行的 id：入站地址是 /api/hooks/{id}。还没生成密钥时为 null */
  id: string | null
  /** 收请求的地址（服务端给的路径）；没给时按 id 拼 */
  url_path: string | null
  created_at: string | null
}

function hookStateOf(raw: Record<string, any>): AppHookState {
  const id = typeof raw.id === 'string' && raw.id ? raw.id : null
  const flag = (...keys: string[]) => keys.some((k) => raw[k] === true)
  return {
    name: String(raw.name ?? ''),
    kind: raw.kind === 'start' || raw.kind === 'signal' ? raw.kind : null,
    entry_id: raw.entry_id ?? raw.entry ?? null,
    declared: flag('declared', 'in_draft'),
    published: flag('published', 'in_published'),
    has_secret: raw.has_secret != null || raw.secret_set != null ? flag('has_secret', 'secret_set') : !!id,
    enabled: raw.enabled !== false,
    id,
    url_path: typeof raw.url_path === 'string' && raw.url_path ? raw.url_path : id ? `/api/hooks/${encodeURIComponent(id)}` : null,
    created_at: raw.created_at ?? null,
  }
}

/** 按新口径补算的结果（D-f）：发起了几次、跳过了几次（同一输入已经补算过） */
export interface BackfillResult {
  started: number
  skipped: number
  run_ids?: string[]
  /** 发起到一半被拒（比如版本刚被退役）：已经发起的照算，原因照实交回 */
  refused?: string | null
}

/** 补算会发起几次（服务端 apps/backfill.Plan.out，不发起运行）：这个视图、哪个固定版本从几升到几、趋势画几期 */
export interface BackfillPlan {
  view: string
  alias: string
  source: string
  from_version: number
  to_version: number
  periods: number
  will_launch: number
  will_skip: number
}

/**
 * 应用详情。manage 才有草稿和岗位绑定（D-m）；另带能按新口径补算的视图（backfill，只在取详情时算）。use / observe
 * 只有概况和当前发布版的页面、入口目录（published）
 */
export type AppDetailOut = AppDetail & {
  backfill?: BackfillPlan[]
  published?: { version: number; pages: { id: string; title: string }[]; entries: { id: string; label: string }[] } | null
}

export const apps = {
  catalog: (opts?: RequestOptions) => get<ComponentCatalog>('/apps/catalog', opts),
  list: (opts?: RequestOptions) => get<{ items: AppSummary[] }>('/apps', opts),
  get: (id: string, opts?: RequestOptions) => get<AppDetailOut>(at(id), opts),
  create: (body: { name: string; description?: string; spec?: AppSpec }) => post<AppDetail>('/apps', body),
  /** 从工作流版本确定性派生一份应用草稿（不落库），编辑器拿去预览 / 新建 */
  derive: (body: { workflow_id: string; version: number; name?: string }) =>
    post<{ spec: AppSpec }>('/apps/derive', body),
  /** 改草稿：spec、岗位绑定、名称等，只带改了的字段 */
  update: (id: string, body: { name?: string; description?: string; icon?: string; draft?: AppSpec;
    draft_bindings?: AppBindings }) => patch<AppDetailOut>(at(id), body),
  archive: (id: string) => post<AppDetail>(`${at(id)}/archive`),
  lint: (id: string) => post<{ issues: LintIssue[]; level: string | null }>(`${at(id)}/lint`),
  publish: (id: string, body: { note?: string; dispositions?: Record<string, Disposition> }) =>
    post<PublishResult>(`${at(id)}/publish`, body),
  versions: (id: string) => get<{ items: AppVersionRow[] }>(`${at(id)}/versions`),
  version: (id: string, v: number) => get<AppVersionRow & { spec: AppSpec; bindings: AppBindings }>(`${at(id)}/versions/${v}`),
  grants: (id: string) => get<{ items: AppGrant[] }>(`${at(id)}/grants`),
  setGrants: (id: string, items: AppGrant[]) => put<{ items: AppGrant[] }>(`${at(id)}/grants`, { items }),
  /** 页面解析：服务端解析好的视图模型（不可见的组件不下发，遮罩值不出服务端） */
  page: (id: string, page: string,
    params?: { instance?: string; params?: Record<string, unknown>; draft?: boolean }, opts?: RequestOptions) =>
    get<ViewModel>(`${at(id)}/pages/${encodeURIComponent(page)}${qs({
      instance: params?.instance, params: params?.params ? JSON.stringify(params.params) : undefined,
      // 编辑器预览：按草稿解析（不发起运行，视图取钉住版本已有的封存运行）
      draft: params?.draft ? 1 : undefined,
    })}`, opts),
  /** 发起一个实例 */
  start: (id: string, entry: string, values: Record<string, unknown>) =>
    post<{ run_id: string; title: string }>(`${at(id)}/entries/${encodeURIComponent(entry)}`, { values }),
  instances: (id: string, params?: { who?: 'mine' | 'all'; entry?: string; status?: string; limit?: number }) =>
    get<{ items: InstanceRow[] }>(`${at(id)}/instances${qs(params)}`),
  instance: (id: string, runId: string) => get<InstanceRow & { view: ViewModel }>(`${at(id)}/instances/${encodeURIComponent(runId)}`),
  /**
   * 应用页上点开一个指标块的出处（§3.6）：指标 → 输入 → 查询快照的出处链，形状同报告片段接口的 chain / seal / redacted，
   * 证据面板照样画。单元格和报告沿用现有的证据接口
   */
  metricEvidence: (runId: string, node: string, metric: string, opts?: RequestOptions) =>
    get<EvidenceSegmentDetail>(
      `/runs/${encodeURIComponent(runId)}/evidence/metrics/${encodeURIComponent(node)}/${encodeURIComponent(metric)}`, opts),
  /** 按需视图：发起一次运行（或命中缓存） */
  refreshView: (id: string, view: string, params?: Record<string, unknown>) =>
    post<{ run_id: string; cached: boolean }>(`${at(id)}/views/${encodeURIComponent(view)}/refresh`, { params: params ?? {} }),
  /**
   * 按新口径补算（D-f，manage）：这个视图的固定版本相对上一个应用版本升了版、处置为按新口径重算时，取旧版本在同一来源上
   * 最近几次封存的正式运行的输入，用新版本逐个发起正式运行（来源记为按新口径补算），同一输入补算过的跳过
   */
  backfill: async (id: string, view: string): Promise<BackfillResult> => {
    const raw = await post<Record<string, any>>(`${at(id)}/views/${encodeURIComponent(view)}/backfill`)
    const n = (...keys: string[]) => Number(keys.map((k) => raw?.[k]).find((v) => typeof v === 'number') ?? 0)
    return { started: n('launched', 'started'), skipped: n('skipped'), run_ids: Array.isArray(raw?.run_ids) ? raw.run_ids : undefined,
             refused: typeof raw?.refused === 'string' ? raw.refused : null }
  },
  /** 补算会发起几次、跳过几次（不发起运行）：确认框按它写明次数 */
  backfillPlan: (id: string, view: string) => get<BackfillPlan>(`${at(id)}/views/${encodeURIComponent(view)}/backfill`),
  /** 钩子（D-g）：草稿声明 ∪ 已有的行，按名字 */
  hooks: async (id: string): Promise<AppHookState[]> => {
    const raw = await get<{ items: Record<string, any>[] } | Record<string, any>[]>(`${at(id)}/hooks`)
    return (Array.isArray(raw) ? raw : raw?.items ?? []).map(hookStateOf).filter((h) => h.name)
  },
  /** 生成或轮换签名密钥：secret 只在这一次交回；url_path 是收请求的地址 */
  rotateHook: (id: string, name: string) => post<AppHookRow & { url_path?: string | null }>(`${hookAt(id, name)}/rotate`),
  setHookEnabled: (id: string, name: string, enabled: boolean) => patch<AppHookRow>(hookAt(id, name), { enabled }),
  /** 删掉这个名字的密钥行：外部系统再用旧密钥签的请求一律拒收 */
  deleteHook: (id: string, name: string) => del(hookAt(id, name)),
  /**
   * 应用编辑器里助手的对话（D-e，manage）：取这个应用的那段对话，没有就建（kind=app、归属这个应用）。带着历史轮次；
   * 老服务端只回对话本身时由调用方再按 id 取一次
   */
  assistantConversation: (id: string): Promise<AssistantConversation> => {
    // 同一个应用正在取的合并成一次：开发模式下 effect 执行两次，两个请求同时到服务端各建一段对话，历史就拆开了
    const pending = assistantInflight.get(id)
    if (pending) return pending
    const request = get<AssistantConversation>(`${at(id)}/assistant-conversation`)
      .finally(() => assistantInflight.delete(id))
    assistantInflight.set(id, request)
    return request
  },
}

type AssistantConversation =
  ConversationDetail | (Omit<ConversationDetail, 'turns'> & { turns?: ConversationDetail['turns'] })
/** 正在取的助手对话，按应用 id（见 apps.assistantConversation） */
const assistantInflight = new Map<string, Promise<AssistantConversation>>()

export const ledger = {
  /** 一个工作流版本被哪些应用钉住、生效记录数、正式运行数 */
  version: (workflowId: string, version: number) =>
    get<{ apps: { app_id: string; name: string; app_version: number }[]; effects: number; formal_runs: number }>(
      `/ledger/workflow-versions/${encodeURIComponent(workflowId)}/${version}`),
}
