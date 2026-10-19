/**
 * 身份的接口（/api/auth、/api/users、/api/groups、/api/audit），挂在 api.auth / api.identity 上。形状见 typesApp.ts。
 * 开登录时：没登录的请求回 401（前端跳 /login），角色不够回 403。关登录时 /me 恒为 mode='off'。
 */
import type { AuditRow, AuthMode, CandidateSource, Candidates, Directory, GroupRow, Principal, Role, UserRow } from '../typesApp'
import { del, get, patch, post, put, qs, type RequestOptions } from './http'

export const auth = {
  me: (opts?: RequestOptions) => get<Principal>('/auth/me', opts),
  login: (body: { username: string; password: string }) => post<Principal>('/auth/login', body),
  /** 建第一个管理员并以他登录：账号密码登录模式下、还没有能登录的管理员时（/auth/me 的 401 说 can_bootstrap） */
  bootstrap: (body: { username: string; password: string; display_name?: string }) =>
    post<Principal>('/auth/bootstrap', body),
  logout: () => post<void>('/auth/logout'),
  /** OIDC：浏览器整页跳转到这里 */
  oidcStartUrl: (next?: string) => `/api/auth/oidc/start${qs({ next })}`,
  /** local_admins：没停用、能用密码登录的管理员；passwordless_admins：没停用、还没设密码的管理员（切到账号密码登录时据此
   *  决定要不要同时建第一个管理员） */
  settings: () => get<{ mode: AuthMode; env_locked: boolean; oidc: Record<string, any>; user_can_chat: boolean;
    local_admins?: { username: string; display_name: string }[]; passwordless_admins?: string[] }>('/auth/settings'),
  saveSettings: (body: { mode?: AuthMode; oidc?: Record<string, any>; user_can_chat?: boolean;
    bootstrap?: { username: string; password: string; display_name?: string } }) => put<any>('/auth/settings', body),
}

export const identity = {
  users: () => get<{ items: UserRow[] }>('/users'),
  createUser: (body: { username: string; display_name?: string; email?: string; role: Role; password?: string }) =>
    post<UserRow>('/users', body),
  updateUser: (id: string, body: { display_name?: string; email?: string; role?: Role; disabled?: boolean;
    password?: string }) => patch<UserRow>(`/users/${encodeURIComponent(id)}`, body),
  groups: () => get<{ items: GroupRow[] }>('/groups'),
  /** 选人用的名录：搭建者及以上只读（/users、/groups 只给管理员） */
  directory: () => get<Directory>('/directory'),
  /** 使用者选人的候选：按用途给的池，只有名字（任何登录的人都能调；用不了那个应用的 403）。q 按开头筛 */
  candidates: (source: CandidateSource, q?: string, limit = 20) =>
    get<Candidates>(`/directory/candidates${qs({ ...source, q: q || undefined, limit })}`),
  createGroup: (body: { name: string; label?: string }) => post<GroupRow>('/groups', body),
  setMembers: (id: string, members: string[]) => put<GroupRow>(`/groups/${encodeURIComponent(id)}/members`, { members }),
  deleteGroup: (id: string) => del(`/groups/${encodeURIComponent(id)}`),
  audit: (params?: { subject_kind?: string; subject_id?: string; limit?: number }) =>
    get<{ items: AuditRow[] }>(`/audit${qs(params)}`),
}
