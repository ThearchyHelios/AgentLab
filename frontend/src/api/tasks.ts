/**
 * 待办与生效记录的接口（/api/tasks、/api/actions），挂在 api.tasks / api.actions 上。形状见 typesApp.ts。
 * 回复被拒（不是派给你的、已处理过、哈希对不上）回 403 / 409 / 422，ApiError.message 是服务端的中文说明。
 */
import type { ActionRecordRow, Task, TaskRespond } from '../typesApp'
import { get, post, qs, type RequestOptions } from './http'

const at = (id: string) => `/tasks/${encodeURIComponent(id)}`

export const tasks = {
  list: (params?: { assigned?: 'me' | 'all'; status?: string; app_id?: string; run_id?: string; limit?: number },
    opts?: RequestOptions) => get<{ items: Task[] }>(`/tasks${qs(params)}`, opts),
  /** 导航徽标：派给我的、待处理的待办数（按来源分：应用里的 / 不属于应用的） */
  count: (opts?: RequestOptions) => get<{ mine: number; apps: number; other: number }>('/tasks/count?mine=1', opts),
  get: (id: string, opts?: RequestOptions) => get<Task>(at(id), opts),
  respond: (id: string, body: TaskRespond) => post<{ task: Task; resolved: boolean }>(`${at(id)}/respond`, body),
  reassign: (id: string, body: { to: { kind: 'user' | 'group'; name: string }[]; note?: string }) =>
    post<Task>(`${at(id)}/reassign`, body),
  remind: (id: string) => post<{ sent: number }>(`${at(id)}/remind`),
}

export const actions = {
  list: (params?: { run_id?: string; app_id?: string; state?: string; limit?: number }) =>
    get<{ items: ActionRecordRow[] }>(`/actions${qs(params)}`),
  get: (id: string) => get<ActionRecordRow>(`/actions/${encodeURIComponent(id)}`),
}
