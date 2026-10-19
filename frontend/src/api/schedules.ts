/**
 * 定时任务的接口（/api/schedules），挂在 api.schedules 上。字段名和形状照前后端共用的接口约定（types.ts 末尾那一段）。
 *
 * 请求函数（get / post / del……）和 ApiError 从 ./http 导入，不要从 ./client 导入：client.ts 要导入
 * 这个文件把它挂到 api 上，反过来导入就成了循环（见 http.ts 开头的说明）。
 *
 * 错误都是 ApiError，message 是服务端给的中文 detail：修改时版本不符回 409，预览非法回 422 且 field 指明是
 * cron / timezone / input_template 哪一项（ApiError.field）。老服务端没有这些接口，回 404 且没有 code
 */
import type {
  Schedule, ScheduleCreate, ScheduleEventKind, ScheduleFirePage, SchedulePatch, SchedulePreview, SchedulePreviewRequest,
  ScheduleRunResult, ScheduleTargetCreate, ScheduleTriggerOptions, ScheduleVariable, ScheduleWebhookTest,
} from '../types'
import { del, get, patch, post, qs, request, type RequestOptions } from './http'

const at = (id: string) => `/schedules/${encodeURIComponent(id)}`

export const schedules = {
  list: (opts?: RequestOptions) => get<{ items: Schedule[] }>('/schedules', opts),
  get: (id: string, opts?: RequestOptions) => get<Schedule>(at(id), opts),
  create: (body: ScheduleCreate | ScheduleTargetCreate) => post<Schedule>('/schedules', body),
  /** 只带改了的字段和读到的版本；Webhook 的地址省略表示不改、空串表示删除 */
  update: (id: string, body: SchedulePatch) => patch<Schedule>(at(id), body),
  remove: (id: string) => del(at(id)),
  /**
   * 接下来几次触发时间和渲染出的输入，只算不存。signal 用来丢掉过时的请求：频率还在改、又发了新的一次时，
   * 上一次的答复不再要（取消照旧抛 AbortError）
   */
  preview: (body: SchedulePreviewRequest, opts?: { signal?: AbortSignal }) =>
    request<SchedulePreview>('/schedules/preview', { method: 'POST', body: JSON.stringify(body), signal: opts?.signal }),
  /** 输入模板里能用的变量（schedule.yesterday 这类），按触发时间计算 */
  variables: (opts?: RequestOptions) => get<ScheduleVariable[]>('/schedules/variables', opts),
  /**
   * 事件触发任务的模板里能用的变量：schedule.*（上一次计划触发时间除外）加上这种事件的 event.*（docs/design/orchestration-round
   * §4）。老服务端不认这两个参数，照旧给 cron 的那一份
   */
  eventVariables: (event: ScheduleEventKind, opts?: RequestOptions) =>
    get<ScheduleVariable[]>(`/schedules/variables${qs({ trigger_kind: 'event', event })}`, opts),
  /**
   * 新建、修改事件触发任务时能选的对象（能不能选、为什么由服务端判）、来源、应用。老服务端没有，回 404。对象一页有上限
   * （more 为真时没给完）：按事件种类、名字里的关键字再查；selected 是当前选中的那一个，总在结果里
   */
  triggerOptions: (find?: { event?: ScheduleEventKind; q?: string; selected?: string }, opts?: RequestOptions) =>
    get<ScheduleTriggerOptions>(`/schedules/trigger-options${qs(find)}`, opts),
  /** 立即运行一次：记一条手动触发，上一次还没结束时照样按重叠策略跳过 */
  run: (id: string) => post<ScheduleRunResult>(`${at(id)}/run`),
  /** 触发历史，新的在前。before 是上一页给的 next_before */
  fires: (id: string, page?: { limit?: number; before?: string | null }, opts?: RequestOptions) =>
    get<ScheduleFirePage>(`${at(id)}/fires${qs(page)}`, opts),
  /** 用已保存的 Webhook 设置发一条测试消息（不用页面上还没保存的） */
  testWebhook: (id: string) => post<ScheduleWebhookTest>(`${at(id)}/webhook/test`),
}
