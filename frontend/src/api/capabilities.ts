/**
 * 能力登记、端点、通知渠道的接口（/api/capabilities、/api/endpoints、/api/notify），挂在 api.capabilities /
 * api.endpoints / api.notify 上。形状见 typesApp.ts。端点和渠道的密钥只回掩码；提交掩码值表示保持原值。
 */
import type { CapabilityRow, EndpointRow, NotifyChannelRow } from '../typesApp'
import { del, get, patch, post, put, qs, type RequestOptions } from './http'

export const capabilities = {
  list: (params?: { system?: string; effect?: string; q?: string }, opts?: RequestOptions) =>
    get<{ items: CapabilityRow[] }>(`/capabilities${qs(params)}`, opts),
  get: (name: string) => get<CapabilityRow>(`/capabilities/${encodeURIComponent(name)}`),
  /** 改治理字段（effect / approval / unattended_write / returns_schema / tabular / fields / masked /
   *  credential_mode / endpoint_id / lookup），带上读到的 version 做乐观锁 */
  govern: (name: string, body: Partial<CapabilityRow> & { version: number }) =>
    put<CapabilityRow>(`/capabilities/${encodeURIComponent(name)}/governance`, body),
  sync: () => post<{ changed: number }>('/capabilities/sync'),
}

export const endpoints = {
  list: () => get<{ items: EndpointRow[] }>('/endpoints'),
  create: (body: Omit<EndpointRow, 'id' | 'version'>) => post<EndpointRow>('/endpoints', body),
  update: (id: string, body: Partial<EndpointRow> & { version: number }) =>
    patch<EndpointRow>(`/endpoints/${encodeURIComponent(id)}`, body),
  remove: (id: string) => del(`/endpoints/${encodeURIComponent(id)}`),
  test: (id: string, env: 'test' | 'prod') =>
    post<{ ok: boolean; status: number | null; message: string }>(`/endpoints/${encodeURIComponent(id)}/test${qs({ env })}`),
}

export const notify = {
  channels: () => get<{ items: NotifyChannelRow[] }>('/notify/channels'),
  createChannel: (body: { name: string; scope: string; owner_id?: string | null; format: string; url: string;
    secret?: string | null; events: string[] }) => post<NotifyChannelRow>('/notify/channels', body),
  updateChannel: (id: string, body: Record<string, any>) => patch<NotifyChannelRow>(`/notify/channels/${encodeURIComponent(id)}`, body),
  removeChannel: (id: string) => del(`/notify/channels/${encodeURIComponent(id)}`),
  testChannel: (id: string) => post<{ ok: boolean; message: string }>(`/notify/channels/${encodeURIComponent(id)}/test`),
  deliveries: (params?: { channel_id?: string; limit?: number }) => get<{ items: any[] }>(`/notify/deliveries${qs(params)}`),
}
