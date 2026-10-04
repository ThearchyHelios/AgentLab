/**
 * 可验证报告的接口（/api/runs/{run_id}/…）。现在是空的，可验证报告的工作包往里加，挂在 api.bundle 上。
 *
 * 请求函数（get / post / del……）和 ApiError 从 ./http 导入，不要从 ./client 导入：client.ts 要导入
 * 这个文件把它挂到 api 上，反过来导入就成了循环（见 http.ts 开头的说明）。
 */
export const bundle = {}
