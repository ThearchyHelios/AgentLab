/**
 * 普通用户建应用的接口（docs/design/normal-user-apps.md §3.10）：蓝图、试运行、发布单、模板、文件、个人应用、页面编辑候选、记录。
 * 约定在 typesBlueprint.ts。流式的蓝图助手、填关节走 store 里的 SSE（同 copilot），不在这里。
 */
import type { ConversationDetail } from '../types'
import type { AppSpec } from '../typesApp'
import type {
  AppCandidate, Blueprint, BlueprintScope, BlueprintSourceKind, BpOp, FileRef, PlainIssue, PublishPlanRow, RecordsPage,
  TemplateInfo, TrialPage, TrialStart,
} from '../typesBlueprint'
import { del, get, patch, post, qs, upload, type UploadOptions } from './http'

export const blueprints = {
  create: (body: { source: { kind: BlueprintSourceKind; id?: string; version?: number }; scope: BlueprintScope; name?: string;
    template?: string }) => post<Blueprint>('/blueprints', body),
  list: (q: { scope?: BlueprintScope } = {}) => get<{ items: Blueprint[] }>(`/blueprints${qs(q)}`),
  get: (id: string) => get<Blueprint>(`/blueprints/${encodeURIComponent(id)}`),
  /** 卡片上的修改：bp_* 操作，来源记成 user */
  patch: (id: string, body: { ops?: BpOp[]; name?: string }) => patch<Blueprint>(`/blueprints/${encodeURIComponent(id)}`, body),
  instantiate: (id: string) =>
    post<{ blueprint: Blueprint; app_spec: AppSpec; problems: PlainIssue[] }>(`/blueprints/${encodeURIComponent(id)}/instantiate`, {}),
  trial: (id: string, body: { inputs?: Record<string, unknown> } = {}) =>
    post<TrialStart>(`/blueprints/${encodeURIComponent(id)}/trial`, body),
  preview: (id: string, q: { page?: string; run?: string } = {}) =>
    get<TrialPage>(`/blueprints/${encodeURIComponent(id)}/preview${qs(q)}`),
  /** dispositions：固定版本升了版时每一项的处置（别名 → recompute / dual / incomparable），口径升版必须处置 */
  publishPlan: (id: string, body: { level: 'published' | 'governed'; first_run?: boolean;
    dispositions?: Record<string, 'recompute' | 'dual' | 'incomparable'> }) =>
    post<PublishPlanRow>(`/blueprints/${encodeURIComponent(id)}/publish-plan`, body),
  plan: (planId: string) => get<PublishPlanRow>(`/publish-plans/${encodeURIComponent(planId)}`),
  runPlan: (planId: string) => post<PublishPlanRow>(`/publish-plans/${encodeURIComponent(planId)}/run`, {}),
  resumePlan: (planId: string) => post<PublishPlanRow>(`/publish-plans/${encodeURIComponent(planId)}/resume`, {}),
  // ---- 个人应用（N7）
  savePersonal: (id: string) => post<{ app_id: string; blueprint: Blueprint }>(`/blueprints/${encodeURIComponent(id)}/save-personal`, {}),
  /** 蓝图对话的历史：权限按蓝图判，没开问数据的使用者也读得到自己蓝图的对话 */
  conversation: (id: string) => get<ConversationDetail>(`/blueprints/${encodeURIComponent(id)}/conversation`),
  copyForReview: (id: string) => post<Blueprint>(`/blueprints/${encodeURIComponent(id)}/copy`, {}),
}

export const templates = {
  list: () => get<{ items: TemplateInfo[] }>('/templates'),
}

export const files = {
  /** 先上传拿引用，再把引用作为表单值提交（N9） */
  upload: (file: File, context: Record<string, string>, opts?: UploadOptions) => {
    const form = new FormData()
    form.append('file', file)
    form.append('context', JSON.stringify(context))
    return upload<FileRef>('/files', form, opts)
  },
  url: (fileId: string) => `/api/files/${encodeURIComponent(fileId)}`,
  remove: (fileId: string) => del(`/files/${encodeURIComponent(fileId)}`),
}

export const personal = {
  refresh: (appId: string) => post<{ run_id: string; remaining: number }>(`/apps/${encodeURIComponent(appId)}/refresh`, {}),
  submitReview: (appId: string, body: { to: { kind: 'user' | 'group'; name: string }[]; note?: string }) =>
    post<{ task_id: string }>(`/apps/${encodeURIComponent(appId)}/submit-review`, body),
  candidates: (appId: string) => get<{ items: AppCandidate[] }>(`/apps/${encodeURIComponent(appId)}/candidates`),
  records: (appId: string, q: { table?: string; who?: 'mine' | 'all'; cursor?: string; history?: boolean } = {}) =>
    get<RecordsPage>(`/apps/${encodeURIComponent(appId)}/records${qs(q)}`),
}
