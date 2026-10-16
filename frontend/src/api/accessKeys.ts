/**
 * 访问密钥与服务账号（/api/access-keys，docs/design/next-round/04-对外集成与触达.md A1–A7），挂在 api.accessKeys 上。形状见
 * typesApp.ts。密钥明文只在建、轮换的回包里（key）出现一次，之后哪个接口都拿不到。
 */
import type {
  AccessKeyIssued, AccessKeyReference, AccessKeyRequestRow, AccessKeyRow, AccessKeyScope, AccessKeySettings,
  ServiceAccountRow,
} from '../typesApp'
import { get, patch, post, put } from './http'

const at = (id: string) => `/access-keys/${encodeURIComponent(id)}`

export const accessKeys = {
  settings: () => get<AccessKeySettings>('/access-keys/settings'),
  /** 个人访问密钥的开关（只给管理员） */
  setPersonalKeys: (personal_keys: boolean) => put<AccessKeySettings>('/access-keys/settings', { personal_keys }),
  /** 管理员：全部密钥；别人：自己的 */
  list: async (): Promise<AccessKeyRow[]> => (await get<{ items: AccessKeyRow[] }>('/access-keys')).items,
  /** 不给 service_account_id 是建自己的个人访问密钥 */
  create: (body: { name: string; scopes: AccessKeyScope[]; app_ids?: string[] | null; expires_in_days: number;
    service_account_id?: string }) => post<AccessKeyIssued>('/access-keys', body),
  revoke: (id: string) => post<AccessKeyRow>(`${at(id)}/revoke`),
  rotate: (id: string) => post<AccessKeyIssued>(`${at(id)}/rotate`),
  requests: (id: string) => get<{ items: AccessKeyRequestRow[]; retain_days: number }>(`${at(id)}/requests`),
  reference: () => get<AccessKeyReference>('/access-keys/reference'),
  serviceAccounts: async (): Promise<ServiceAccountRow[]> =>
    (await get<{ items: ServiceAccountRow[] }>('/access-keys/service-accounts')).items,
  createServiceAccount: (body: { username: string; display_name?: string }) =>
    post<ServiceAccountRow>('/access-keys/service-accounts', body),
  updateServiceAccount: (id: string, body: { display_name?: string; disabled?: boolean }) =>
    patch<ServiceAccountRow>(`/access-keys/service-accounts/${encodeURIComponent(id)}`, body),
}
