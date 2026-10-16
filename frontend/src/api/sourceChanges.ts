/**
 * 数据源的变化、取值观察、定时维护的步骤、后台任务条的接口（服务端 app/api/source_changes.py），挂在 api.sourceChanges 上。
 * 定时本身（cron、时区、开关）走 api.schedules（target_kind=source_maintenance，总方案 X4：定时只有一套）。
 *
 * 请求函数从 ./http 导入，不从 ./client 导入（client.ts 要导入这个文件，反过来就成了循环，见 http.ts 开头的说明）。
 * 误报没写理由 422、处理过的再处理 409、目录被别人刚改过 409，message 是服务端给的中文
 */
import type {
  MaintenanceStep, SourceBackgroundJob, SourceChange, SourceChangeList, SourceChangeQuery, SourceChangeResolveBody,
  SourceChangeResolveOut, SourceMaintenance, SourceUpkeep, TableObservations,
} from '../typesSource'
import { get, post, put, qs, type RequestOptions } from './http'

const at = (sourceId: string) => `/datasources/${encodeURIComponent(sourceId)}`

export const sourceChanges = {
  /** 数据源列表一次取齐：各源要处理的变化条数（卡片角标）、指向它的维护定时 */
  upkeep: (opts?: RequestOptions) => get<{ items: SourceUpkeep[] }>('/datasources/upkeep', opts),
  /** 一个源的变化：缺省开着的（要处理的先）；status=handled 是处理过的。category 只看结构 / 取值变化 */
  list: (sourceId: string, q: SourceChangeQuery = {}, opts?: RequestOptions) =>
    get<SourceChangeList>(`${at(sourceId)}/changes${qs({
      status: q.status ?? 'pending', category: q.category ?? undefined, table: q.table ?? undefined,
      limit: q.limit ?? 100, offset: q.offset ?? 0,
    })}`, opts),
  get: (sourceId: string, changeId: string, opts?: RequestOptions) =>
    get<{ change: SourceChange }>(`${at(sourceId)}/changes/${encodeURIComponent(changeId)}`, opts),
  /** 处理一条变化：动作落到目录已有的写入接口并记目录版本、建了的任务编号 */
  resolve: (sourceId: string, changeId: string, body: SourceChangeResolveBody) =>
    post<SourceChangeResolveOut>(`${at(sourceId)}/changes/${encodeURIComponent(changeId)}/resolve`, body),
  /** 表详情「结构与取值变化」一节：这张表最近几次观察，各带口径 */
  observations: (sourceId: string, table: string, opts?: RequestOptions) =>
    get<TableObservations>(`${at(sourceId)}/observations${qs({ table })}`, opts),
  /** 这个源进行中和排队的后台任务，按同源队的先后排好 */
  backgroundJobs: (sourceId: string, opts?: RequestOptions) =>
    get<{ items: SourceBackgroundJob[] }>(`${at(sourceId)}/background-jobs`, opts),
  /** 定时维护：步骤、剖析开没开、调度开没开、指向这个源的维护定时（定时任务页同一个形状，最近一次触发带 inputs） */
  maintenance: (sourceId: string, opts?: RequestOptions) => get<SourceMaintenance>(`${at(sourceId)}/maintenance`, opts),
  /** 改要做哪几步（至少一步）。上传表格、记录表 409 */
  putMaintenance: (sourceId: string, steps: MaintenanceStep[]) =>
    put<SourceMaintenance>(`${at(sourceId)}/maintenance`, { steps }),
}
