/**
 * 「最近的请求」：外部系统发来的请求逐条列出——访问密钥的请求记录（设置 → 访问密钥）和钩子的收件记录（应用编辑 → 钩子）
 * 共用这一个组件（docs/design/next-round/04-对外集成与触达.md A6「一种说法」）。外部系统说「我发过了」，这里查得到收到没有、
 * 回了什么、为什么被拒。
 *
 * 只管画：每一行时间、结果（成功 / 被拒 / 处理中）、是什么（接口或幂等键）、原因或补充、涉及的运行或待办。取数和把服务端的
 * 形状换成这里的行在调用方做（界面层不放逻辑：结果的判断服务端已经给了，这里只照着上色）。
 */
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ErrorState, Skeleton } from './ui'
import { formatDateTime } from '../lib/format'
import { RECENT_REQUESTS_TEXT as T } from '../lib/termsApp'

export interface RecentRequestRow {
  key: string
  at: string | null
  /** ok 成功；bad 被拒；pending 处理中 */
  tone: 'ok' | 'bad' | 'pending'
  /** 结果那一格的字（「成功 200」「被拒」……） */
  result: string
  /** 是什么：接口（方法 + 路由模板）或幂等键 */
  what: string
  /** 原因、来源地址这类补充，没有为空 */
  detail?: string | null
  /** 涉及的运行或待办：label 是「运行」「实例」「待办」，to 给了就是能点的链接 */
  ref?: { label: string; id: string; to?: string | null } | null
}

const TONE: Record<RecentRequestRow['tone'], string> = { ok: 'var(--ok)', bad: 'var(--err)', pending: 'var(--st-waiting)' }

export function RecentRequests({ rows, error, onRetry, summary, empty, attr }: {
  rows: RecentRequestRow[] | null
  error?: unknown
  onRetry?: () => void
  /** 列表上面的一句（近 1 小时验签失败几次、保留几天） */
  summary?: ReactNode
  /** 一条都没有时写什么 */
  empty?: string
  /** 给检查脚本认的 data- 属性 */
  attr?: Record<string, string>
}) {
  return (
    <div className="space-y-1.5" data-recent-requests="" {...attr}>
      {summary && <p className="text-2xs text-faint" data-recent-requests-summary="">{summary}</p>}
      {error ? <ErrorState error={error} onRetry={onRetry} compact /> : !rows ? <Skeleton rows={3} height={20} /> : !rows.length ? (
        <p className="text-2xs text-faint" data-recent-requests-empty="">{empty ?? T.empty}</p>
      ) : (
        <ol className="divide-y rounded-lg border text-2xs" aria-label={T.label}>
          {rows.map((r) => (
            <li key={r.key} className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 px-2.5 py-1.5" data-recent-request={r.tone}>
              <time className="tnum w-32 shrink-0 text-faint">{r.at ? formatDateTime(r.at) : '—'}</time>
              <span className="chip shrink-0" style={{ color: TONE[r.tone], borderColor: TONE[r.tone] }}>{r.result}</span>
              <span className="mono min-w-0 flex-1 truncate" title={r.what}>{r.what}</span>
              {r.ref && (
                <span className="shrink-0 text-faint">
                  {r.ref.label}{' '}
                  {r.ref.to ? <Link className="mono underline-offset-2 hover:text-fg hover:underline" to={r.ref.to}>{r.ref.id.slice(0, 8)}</Link>
                    : <span className="mono" title={r.ref.id}>{r.ref.id.slice(0, 8)}</span>}
                </span>
              )}
              {r.detail && <span className="basis-full text-dim" data-recent-request-detail="">{r.detail}</span>}
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}
