/**
 * 应用页（/apps/:appId、/apps/:appId/p/:pageId）：按服务端解析好的视图模型渲染（apps/ViewRenderer）。
 *
 * 没带页面的落到应用的首页。页面参数（筛选表单交的那一组）在地址的 params 里，换了就重新解析。有视图正在生成时
 * 每 5 秒重取一次，生成完自动换上；引用的版本退役了照实提示，入口暂停发起。
 */
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { LayoutGrid, PencilLine, RotateCw } from 'lucide-react'
import { api } from '../../api/client'
import { ViewRenderer } from '../../apps/ViewRenderer'
import { parseViewParams } from '../../apps/components/input'
import { Notice, PageHeader, Skeleton, Spinner, Tabs } from '../../components/ui'
import { useAtLeast } from '../../store/auth'
import { usePageTitle } from '../../lib/notify'
import { APP_TEXT } from '../../lib/termsApp'
import { AppMeta, BackLink, LoadError, appPath, loadHome, useLoad, usePoll } from './shared'

const POLL_MS = 5000

export function AppPage() {
  const { appId = '', pageId } = useParams()
  const [search] = useSearchParams()
  const raw = search.get('params')
  const navigate = useNavigate()
  const builder = useAtLeast('builder')
  const { data: model, error, loading, reload } = useLoad((signal) => {
    const params = parseViewParams(raw)
    return pageId ? api.apps.page(appId, pageId, { params }, { signal }) : loadHome(appId, params, { signal })
  }, [appId, pageId, raw])
  usePageTitle(model ? `${APP_TEXT.title} · ${model.app.name}` : null)
  const running = !!model && Object.values(model.sources).some((s) => s.state === 'running')
  usePoll(reload, POLL_MS, running)

  const go = (id: string) => navigate(`${appPath(appId)}/p/${encodeURIComponent(id)}${raw ? `?params=${encodeURIComponent(raw)}` : ''}`)
  return (
    <div className="flex h-full flex-col" data-app-page={model?.page.id ?? ''}>
      <PageHeader
        icon={<LayoutGrid size={14} />}
        title={model?.app.name ?? APP_TEXT.title}
        subtitle={model && model.pages.length <= 1 ? model.page.title : undefined}
        actions={(
          <div className="flex items-center gap-1.5">
            {loading && model && <Spinner size={12} />}
            <button type="button" className="btn btn-sm btn-ghost" onClick={reload} aria-label={APP_TEXT.reload} title={APP_TEXT.reload}>
              <RotateCw size={12} aria-hidden />
            </button>
            {builder && (
              <button type="button" className="btn btn-sm" onClick={() => navigate(`${appPath(appId)}/edit`)}>
                <PencilLine size={12} aria-hidden /> {APP_TEXT.edit}
              </button>
            )}
          </div>
        )}
      />
      {model && model.pages.length > 1 && (
        <Tabs label={APP_TEXT.pagesLabel} tabs={model.pages.map((p) => ({ key: p.id, label: p.title || p.id }))}
              active={model.page.id} onChange={go} />
      )}
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-5xl space-y-3 p-4">
          {error && !model ? <LoadError error={error} onRetry={reload} />
            : !model ? <Skeleton rows={6} />
            : (
              <>
                <AppMeta app={model.app} back={<BackLink to="/apps?tab=apps" label={APP_TEXT.backToApps} />} />
                {model.app.needs_upgrade && (
                  <Notice tone="warn" attr={{ 'data-app-needs-upgrade': '' }}>
                    <span className="font-medium">{APP_TEXT.needsUpgrade}</span>：{APP_TEXT.needsUpgradeHint}
                  </Notice>
                )}
                <ViewRenderer model={model} appId={appId} onNavigate={go} onReload={reload} />
              </>
            )}
        </div>
      </div>
    </div>
  )
}
