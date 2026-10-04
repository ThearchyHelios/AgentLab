/**
 * 定时任务的接口（/api/schedules）。现在是空的，定时任务的工作包往里加，挂在 api.schedules 上。
 *
 * 请求函数（get / post / del……）和 ApiError 从 ./http 导入，不要从 ./client 导入：client.ts 要导入
 * 这个文件把它挂到 api 上，反过来导入就成了循环（见 http.ts 开头的说明）。
 */
export const schedules = {}
