/**
 * 运行对比的接口（/api/runs/{run_id}/compare…），挂在 api.compare 上。两个接口都只读：比较结果服务端另存工件，
 * 不写运行记录。
 *
 * 请求函数（get / post / del……）和 ApiError 从 ./http 导入，不要从 ./client 导入：client.ts 要导入
 * 这个文件把它挂到 api 上，反过来导入就成了循环（见 http.ts 开头的说明）。
 */
import type { RunCompareBase, RunComparison } from '../types'
import { get, type RequestOptions } from './http'

const run = (id: string) => `/runs/${encodeURIComponent(id)}`

export const compare = {
  /**
   * 默认的基准：同一定时任务上一次成功并封存的运行，没有就同一工作流、同一运行类别的上一次。previous 说它是不是紧挨着
   * 的上一次（运行详情的菜单项按它定叫法）。找不到时回 404，ApiError 的 message 是给人看的原因
   */
  base: (runId: string, opts?: RequestOptions) => get<RunCompareBase>(`${run(runId)}/compare/base`, opts),
  /**
   * 两次运行的对比（runId 是本次）。任一未封存或封存核对不一致回 409；不是同一工作流、或者是同一次运行回 422；
   * 运行不存在回 404。detail 都是一句中文。signal 用来丢掉换了基准之后过时的请求
   */
  get: (runId: string, base: string, opts?: RequestOptions) =>
    get<RunComparison>(`${run(runId)}/compare?${new URLSearchParams({ base })}`, opts),
}
