/**
 * 对外访问地址：别人打开 AgentLab 用的那个地址。服务端全站只认一个（backend/app/core/public_url.py）：环境变量
 * AGENTLAB_PUBLIC_URL 优先，其次设置里 notify 一组的 public_base_url（界面上在「设置 → 通用」改，定时任务的 Webhook 一节
 * 有快捷入口）。通知和 Webhook 里的链接、单点登录的回调、来源核对都按它。
 *
 * GET /api/settings 另给只读的 public_url 一组 {value, env_locked}：环境变量指定了的话显示它、写明「由环境变量指定」、
 * 不让改（改了服务端回 409）。老服务端没有这一组时按设置里存的那一项、可以改。
 */

export interface PublicAddress {
  /** 生效的地址；null 是没设置（通知里不附链接） */
  value: string | null
  /** 环境变量指定的：设置里的那一项不生效、不能改 */
  env: boolean
}

export function publicAddressOf(settings: Record<string, any> | null | undefined): PublicAddress {
  const env = settings?.public_url?.env_locked === true
  const raw = env ? settings?.public_url?.value : settings?.notify?.public_base_url
  return { value: typeof raw === 'string' && raw ? raw : null, env }
}

/** 填的地址去掉首尾空白和末尾的 /（服务端存的也是这样） */
export function cleanPublicUrl(text: string): string {
  return text.trim().replace(/\/+$/, '')
}

/** http:// 或 https:// 开头、有主机名、中间没有空白、不带 ? 或 # 之后的部分（同服务端 public_url.url_problem） */
export function isPublicUrl(text: string): boolean {
  return /^https?:\/\/[^\s/?#]+(?:\/[^\s?#]*)?$/i.test(text.trim())
}
