/**
 * 应用的接口（/api/apps、/api/ledger），挂在 api.apps 上。形状见 typesApp.ts（应用平台的接口约定）。
 *
 * 请求函数从 ./http 导入，不要从 ./client 导入（循环导入，见 http.ts 开头）。
 * 入口提交校验失败回 422，ApiError 的 body 里带 {errors: FormError[]}。
 */
import type {
  AppBindings, AppDetail, AppGrant, AppHookRow, AppList, HookReceipts, AppSpec, AppSummary, AppVersionRow, BackfillPlan, BackfillResult,
  ComponentCatalog, Disposition, InstanceRow, LintIssue, PublishResult, ViewModel, ViewShareCreated, ViewShareList,
  ViewShareRow,
} from '../typesApp'
import type { ConversationDetail, EvidenceSegmentDetail } from '../types'
import type { VersionRecallCounts } from '../typesRecall'
import { del, get, patch, post, put, qs, request, type RequestOptions } from './http'

const at = (id: string) => `/apps/${encodeURIComponent(id)}`
const hookAt = (id: string, name: string) => `${at(id)}/hooks/${encodeURIComponent(name)}`

export const apps = {
  catalog: (opts?: RequestOptions) => get<ComponentCatalog>('/apps/catalog', opts),
  list: (opts?: RequestOptions) => get<AppList>('/apps', opts),
  get: (id: string, opts?: RequestOptions) => get<AppDetail>(at(id), opts),
  create: (body: { name: string; description?: string; spec?: AppSpec }) => post<AppDetail>('/apps', body),
  /** 从工作流版本确定性派生一份应用草稿（不落库），编辑器拿去预览 / 新建 */
  derive: (body: { workflow_id: string; version: number; name?: string }) =>
    post<{ spec: AppSpec }>('/apps/derive', body),
  /** 改草稿：spec、岗位绑定、名称等，只带改了的字段 */
  update: (id: string, body: { name?: string; description?: string; icon?: string; draft?: AppSpec;
    draft_bindings?: AppBindings }) => patch<AppDetail>(at(id), body),
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
  backfill: (id: string, view: string) =>
    post<BackfillResult>(`${at(id)}/views/${encodeURIComponent(view)}/backfill`),
  /** 补算会发起几次、跳过几次（不发起运行）：确认框按它写明次数 */
  backfillPlan: (id: string, view: string) => get<BackfillPlan>(`${at(id)}/views/${encodeURIComponent(view)}/backfill`),
  /** 钩子（D-g）：草稿声明 ∪ 已有的行，按名字 */
  hooks: async (id: string): Promise<AppHookRow[]> => (await get<{ items: AppHookRow[] }>(`${at(id)}/hooks`)).items,
  /** 生成或轮换签名密钥：secret 只在这一次交回；url_path 是收请求的地址 */
  rotateHook: (id: string, name: string) => post<AppHookRow>(`${hookAt(id, name)}/rotate`),
  setHookEnabled: (id: string, name: string, enabled: boolean) => patch<AppHookRow>(hookAt(id, name), { enabled }),
  /** 删掉这个名字的密钥行：外部系统再用旧密钥签的请求一律拒收 */
  deleteHook: (id: string, name: string) => del(hookAt(id, name)),
  /** 钩子最近收到的请求（manage）：状态、原因、发起的实例或解决的待办，加近 1 小时验签失败几次（04 细案 A6） */
  hookReceipts: (id: string, name: string) => get<HookReceipts>(`${hookAt(id, name)}/receipts`),
  /**
   * 分享链接（manage，服务端 api/shares.py）：列、建、撤销。建要管理员开了「允许建分享链接」，到期必填（最长 max_days 天）；
   * key 只在建的这一次交回
   */
  shares: (id: string) => get<ViewShareList>(`${at(id)}/shares`),
  createShare: (id: string, body: { page_id: string; mode: 'latest' | 'fixed'; expires_at: string; embed_allowed: boolean }) =>
    post<ViewShareCreated>(`${at(id)}/shares`, body),
  revokeShare: (id: string, shareId: string) =>
    request<ViewShareRow>(`${at(id)}/shares/${encodeURIComponent(shareId)}`, { method: 'DELETE' }),
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
  /**
   * 一个工作流版本被哪些应用固定、生效记录数、正式运行数；口径账本另加的（第 4 波，老服务端不给）：被几次问数回答用过、
   * 导出过几次、定时任务几个、召回（以这一版为新版本召回过几组、其中数变了几组，作为旧版本被召回过几组）
   */
  version: (workflowId: string, version: number) =>
    get<{ apps: { app_id: string; name: string; app_version: number }[]; effects: number; formal_runs: number
      answers?: number; exports?: number; schedules?: number; recall?: VersionRecallCounts }>(
      `/ledger/workflow-versions/${encodeURIComponent(workflowId)}/${version}`),
}
