/**
 * 应用的接口（/api/apps、/api/ledger），挂在 api.apps 上。形状见 typesApp.ts（应用平台的接口约定）。
 *
 * 请求函数从 ./http 导入，不要从 ./client 导入（循环导入，见 http.ts 开头）。
 * 入口提交校验失败回 422，ApiError 的 body 里带 {errors: FormError[]}。
 */
import type {
  AppBindings, AppDetail, AppGrant, AppSpec, AppSummary, AppVersionRow, ComponentCatalog, Disposition, InstanceRow,
  LintIssue, PublishResult, ViewModel,
} from '../typesApp'
import type { EvidenceSegmentDetail } from '../types'
import { get, patch, post, put, qs, type RequestOptions } from './http'

const at = (id: string) => `/apps/${encodeURIComponent(id)}`

export const apps = {
  catalog: (opts?: RequestOptions) => get<ComponentCatalog>('/apps/catalog', opts),
  list: (opts?: RequestOptions) => get<{ items: AppSummary[] }>('/apps', opts),
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
}

export const ledger = {
  /** 一个工作流版本被哪些应用钉住、生效记录数、正式运行数 */
  version: (workflowId: string, version: number) =>
    get<{ apps: { app_id: string; name: string; app_version: number }[]; effects: number; formal_runs: number }>(
      `/ledger/workflow-versions/${encodeURIComponent(workflowId)}/${version}`),
}
