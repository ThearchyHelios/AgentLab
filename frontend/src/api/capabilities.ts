/**
 * 能力登记、端点、通知渠道的接口（/api/capabilities、/api/endpoints、/api/notify），挂在 api.capabilities /
 * api.endpoints / api.notify 上。形状见 typesApp.ts。端点和渠道的密钥只回掩码；提交掩码值表示保持原值。
 */
import type { CatalogReviewAction, CatalogSource, CatalogStatus } from '../types'
import type {
  CapabilityEffect, CapabilityReviewOutcome, CapabilityReviewRow, CapabilityRow, EndpointRow, NotifyChannelRow,
  NotifyEventRow, NotifyMailState,
} from '../typesApp'
import { del, get, patch, post, put, qs, type RequestOptions } from './http'

/**
 * 能力字段语义的审阅（D-i）：沿用数据目录「项」的状态（推断 / 已核实 / 已确认 / 已驳回，带理由），按被多少运行用过、
 * 是不是写入能力、来源排好序（服务端 data/capability_review.py，同 data/catalog_review.py 的思路）。一项是某个能力
 * 返回里某个位置（JSON 指针）的某个字段（中文名、含义、单位、度量类型、码值）
 */
export interface CapabilityReviewItem {
  /** 能力名（同图里工具的名字） */
  name: string
  title: string | null
  /** 审阅队列里这一项的键：字段@位置（同一个能力里不重复）。成批审阅时拆回位置和字段 */
  path: string
  /** 返回里的位置（JSON 指针） */
  pointer: string
  field: string
  /** 给人看的位置（服务端写好的「返回字段 /amount 的单位」） */
  where: string
  value: unknown
  display: string
  source: CatalogSource
  status: CatalogStatus
  note: string | null
  priority: number
  reasons: string[]
  /** 用到这个能力的运行次数 */
  usage: number
  effect: CapabilityEffect | null
  /** 能力的治理版本：成批审阅时逐项带上（乐观锁） */
  version: number
}

export interface CapabilityReviewResult {
  name: string
  path: string
  ok: boolean
  version?: number | null
  error: string | null
  code?: string | null
}


/** 队列一项的键：字段@位置。字段只有几个固定的英文名，不含 @，拆的时候按第一个 @ 拆 */
export const reviewPathOf = (field: string, pointer: string) => `${field}@${pointer}`
export function splitReviewPath(path: string): { field: string; pointer: string } {
  const at = path.indexOf('@')
  return at < 0 ? { field: '', pointer: path } : { field: path.slice(0, at), pointer: path.slice(at + 1) }
}

function reviewItemOf(raw: CapabilityReviewRow): CapabilityReviewItem {
  return {
    name: raw.capability,
    title: raw.title,
    path: reviewPathOf(raw.field, raw.pointer),
    pointer: raw.pointer,
    field: raw.field,
    where: raw.field_label,
    value: raw.value,
    display: raw.display,
    source: raw.source as CatalogSource,
    status: raw.status as CatalogStatus,
    note: raw.note,
    priority: raw.priority,
    reasons: raw.reasons,
    usage: raw.runs,
    effect: raw.effect,
    version: raw.version,
  }
}

export const capabilities = {
  list: (params?: { system?: string; effect?: string; q?: string }, opts?: RequestOptions) =>
    get<{ items: CapabilityRow[] }>(`/capabilities${qs(params)}`, opts),
  get: (name: string) => get<CapabilityRow>(`/capabilities/${encodeURIComponent(name)}`),
  /** 改治理字段（effect / approval / unattended_write / returns_schema / tabular / fields / masked /
   *  credential_mode / endpoint_id / lookup），带上读到的 version 做乐观锁 */
  govern: (name: string, body: Partial<CapabilityRow> & { version: number }) =>
    put<CapabilityRow>(`/capabilities/${encodeURIComponent(name)}/governance`, body),
  sync: () => post<{ changed: number }>('/capabilities/sync'),
  /** 待审阅的能力字段（D-i）：顺序服务端排好；capability 只看这一个能力 */
  reviewQueue: async (params: { limit?: number; offset?: number; capability?: string | null }, opts?: RequestOptions) => {
    const raw = await get<{ total: number; items: CapabilityReviewRow[] }>(`/capabilities/review-queue${qs({
      limit: params.limit, offset: params.offset, capability: params.capability || undefined,
    })}`, opts)
    return { total: raw.total, items: raw.items.map(reviewItemOf) }
  },
  /**
   * 成批审阅（只给管理员）：逐个能力按治理版本做乐观锁，逐条交回结果（一个能力冲突只记在那几条上，code 为
   * capability_conflict）；写审计。path 是队列一项的键（字段@位置），这里拆回位置和字段
   */
  reviewBatch: async (items: { name: string; path: string; action: CatalogReviewAction; if_version: number }[]) => {
    const raw = await post<{ results: CapabilityReviewOutcome[] }>('/capabilities/review/batch', {
      items: items.map((i) => ({ capability: i.name, ...splitReviewPath(i.path), action: i.action, if_version: i.if_version })),
    })
    return {
      results: raw.results.map((r): CapabilityReviewResult => ({
        name: r.capability, path: reviewPathOf(r.field, r.pointer), ok: r.ok, version: r.version, error: r.error, code: r.code,
      })),
    }
  },
}

export const endpoints = {
  list: () => get<{ items: EndpointRow[] }>('/endpoints'),
  create: (body: Omit<EndpointRow, 'id' | 'version'>) => post<EndpointRow>('/endpoints', body),
  update: (id: string, body: Partial<EndpointRow> & { version: number }) =>
    patch<EndpointRow>(`/endpoints/${encodeURIComponent(id)}`, body),
  remove: (id: string) => del(`/endpoints/${encodeURIComponent(id)}`),
  /** ok：基础地址回了 2xx；reachable：连上了、拿到了响应（回 404、401 时连上了但不算成功）。message 是服务端照实写的那句 */
  test: (id: string, env: 'test' | 'prod') =>
    post<{ ok: boolean; reachable?: boolean; status: number | null; message: string }>(
      `/endpoints/${encodeURIComponent(id)}/test${qs({ env })}`),
}

export const notify = {
  /** mail：邮件服务启没启用、本人的账号邮箱（老服务端不给） */
  channels: () => get<{ items: NotifyChannelRow[]; mail?: NotifyMailState }>('/notify/channels'),
  /** 邮件渠道：个人的不带 url（只发到账号邮箱），全局、应用的 url 是收件地址（逗号或换行分隔，最多 20 个） */
  createChannel: (body: { name: string; scope: string; owner_id?: string | null; format: string; url?: string;
    secret?: string | null; events: string[] }) => post<NotifyChannelRow>('/notify/channels', body),
  updateChannel: (id: string, body: Record<string, any>) => patch<NotifyChannelRow>(`/notify/channels/${encodeURIComponent(id)}`, body),
  removeChannel: (id: string) => del(`/notify/channels/${encodeURIComponent(id)}`),
  testChannel: (id: string) => post<{ ok: boolean; message: string }>(`/notify/channels/${encodeURIComponent(id)}/test`),
  deliveries: (params?: { channel_id?: string; limit?: number }) => get<{ items: any[] }>(`/notify/deliveries${qs(params)}`),
  /** 登记的事件和推给哪几条路：哪种渠道能订阅哪些由服务端说，界面不自己猜 */
  events: () => get<NotifyEventRow[]>('/notify/events'),
}
