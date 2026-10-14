/**
 * 实例页（/apps/:appId/i/:runId）：一件正在办的事——状态、印章、进度、待办、动作，按服务端给的实例页视图模型渲染
 * （应用没有实例页时服务端给一页默认的：概况、进度、待办）。
 *
 * 怎么跟上进展：使用者（开了登录的 user 角色）没有运行的事件流，轮询——在运行时 4 秒、等人处理时 15 秒一次；搭建者以上
 * 接运行的事件流，来了事件就重取一次（攒半秒再取，补历史时那一串事件只取一次）。结束了就不再跟。
 */
import { useEffect, useRef } from 'react'
import { Link, useParams } from 'react-router-dom'
import { LayoutGrid, Radio, RotateCw } from 'lucide-react'
import { api, streamRun } from '../../api/client'
import { ViewRenderer } from '../../apps/ViewRenderer'
import { PageHeader, Skeleton, Spinner, StatusPill } from '../../components/ui'
import { useAtLeast } from '../../store/auth'
import { usePageTitle } from '../../lib/notify'
import { formatDateTime, formatRelative } from '../../lib/format'
import { APP_TEXT, INSTANCE_TEXT } from '../../lib/termsApp'
import { AppMeta, BackLink, LoadError, appPath, useLoad, usePoll } from './shared'

const ACTIVE = new Set(['queued', 'running', 'interrupted'])

export function InstancePage() {
  const { appId = '', runId = '' } = useParams()
  const builder = useAtLeast('builder')
  const { data, error, loading, reload } = useLoad(() => api.apps.instance(appId, runId), [appId, runId])
  const active = !!data && ACTIVE.has(data.status)
  usePageTitle(data ? `${APP_TEXT.title} · ${data.title || data.view.app.name}` : null)
  // 使用者：轮询
  usePoll(reload, data?.status === 'interrupted' ? 15_000 : 4_000, active && !builder)
  // 搭建者以上：运行的事件流，事件攒半秒再重取一次
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  useEffect(() => {
    if (!active || !builder) return
    const later = () => { clearTimeout(timer.current); timer.current = setTimeout(reload, 500) }
    const stop = streamRun(runId, later, later)
    return () => { stop(); clearTimeout(timer.current) }
  }, [active, builder, runId, reload])

  const view = data?.view
  return (
    <div className="flex h-full flex-col" data-instance-page={runId} data-instance-status={data?.status ?? ''}>
      <PageHeader
        icon={<LayoutGrid size={14} />}
        title={data ? data.title || view!.app.name : APP_TEXT.title}
        subtitle={view?.app.name}
        actions={(
          <div className="flex items-center gap-2">
            {active && (
              <span className="flex items-center gap-1 text-2xs text-faint" data-instance-follow={builder ? 'stream' : 'poll'}>
                {loading ? <Spinner size={11} /> : <Radio size={11} aria-hidden />} {builder ? INSTANCE_TEXT.live : INSTANCE_TEXT.polling}
              </span>
            )}
            <button type="button" className="btn btn-sm btn-ghost" onClick={reload} aria-label={APP_TEXT.reload} title={APP_TEXT.reload}>
              <RotateCw size={12} aria-hidden />
            </button>
          </div>
        )}
      />
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-5xl space-y-3 p-4">
          {error && !data ? <LoadError error={error} onRetry={reload} notFound={INSTANCE_TEXT.notFound} />
            : !data || !view ? <Skeleton rows={6} />
            : (
              <>
                <AppMeta app={view.app} back={<BackLink to={appPath(appId)} label={view.app.name} />} />
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-2xs text-dim">
                  <StatusPill status={data.status_code ?? data.status} label={data.status_text} />
                  {data.started_by && <span>{INSTANCE_TEXT.startedBy(data.started_by)}</span>}
                  <span title={formatDateTime(data.created_at)}>{INSTANCE_TEXT.startedAt(formatRelative(data.created_at))}</span>
                  {data.finished_at && <span title={formatDateTime(data.finished_at)}>{INSTANCE_TEXT.finishedAt(formatRelative(data.finished_at))}</span>}
                  {builder && (
                    <Link to={`/runs/${encodeURIComponent(runId)}`} className="underline-offset-2 hover:text-fg hover:underline">
                      {INSTANCE_TEXT.openRun}
                    </Link>
                  )}
                </div>
                <ViewRenderer model={view} appId={appId} runId={runId} onReload={reload} />
              </>
            )}
        </div>
      </div>
    </div>
  )
}
