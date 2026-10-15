/**
 * 应用页（/apps/:appId、/apps/:appId/p/:pageId）：按服务端解析好的视图模型渲染（apps/ViewRenderer）。
 *
 * 没带页面的落到应用的首页。页面参数（筛选表单交的那一组）在地址的 params 里，换了就重新解析。有视图正在生成时
 * 每 5 秒重取一次，生成完自动换上；引用的版本退役了照实提示，入口暂停发起。
 *
 * 另取一次应用详情：有 manage 的人点「编辑」在页面上直接改（地址上记 edit=1，见 PageEdit.tsx，normal-user-apps N6）；
 * 个人应用（scope 为 personal）顶上写明是个人应用，给「刷新」和「提交审核」（personal.tsx，N7）。详情取不到不挡页面：
 * 照只能用的人画。
 */
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { LayoutGrid, PencilLine, RotateCw, Settings2 } from 'lucide-react'
import { api } from '../../api/client'
import { ViewRenderer } from '../../apps/ViewRenderer'
import { parseViewParams } from '../../apps/components/input'
import { Notice, PageHeader, Skeleton, Spinner, Tabs } from '../../components/ui'
import { useAtLeast } from '../../store/auth'
import { usePageTitle } from '../../lib/notify'
import { APP_TEXT, PAGE_EDIT_TEXT, PERSONAL_TEXT } from '../../lib/termsApp'
import type { AppDetail } from '../../typesApp'
import { AppMeta, BackLink, LoadError, appPath, loadHome, useLoad, usePoll } from './shared'
import { BackfillPanel } from './backfill'
import { PageEditor } from './PageEdit'
import { PersonalBar } from './personal'

const POLL_MS = 5000

/** 能在页面上直接改：manage 才拿得到草稿；归档了的不能再改草稿（服务端同样拒绝） */
const editableOf = (d: AppDetail | undefined): boolean =>
  !!d && d.my_role === 'manage' && !!d.draft && d.status !== 'archived'

export function AppPage() {
  const { appId = '', pageId } = useParams()
  const [search, setSearch] = useSearchParams()
  const raw = search.get('params')
  const navigate = useNavigate()
  const { data: detail, reload: reloadDetail } = useLoad((signal) => api.apps.get(appId, { signal }), [appId])
  const editing = editableOf(detail) && search.get('edit') === '1'
  const setEditing = (on: boolean) => setSearch((prev) => {
    const next = new URLSearchParams(prev)
    if (on) next.set('edit', '1')
    else next.delete('edit')
    return next
  }, { replace: true })
  // 换页带着页面参数，编辑状态下也还在编辑状态
  const go = (id: string) => {
    const q = new URLSearchParams()
    if (raw) q.set('params', raw)
    if (editing) q.set('edit', '1')
    const tail = q.toString()
    navigate(`${appPath(appId)}/p/${encodeURIComponent(id)}${tail ? `?${tail}` : ''}`)
  }

  if (editing && detail) {
    return (
      <PageEditor key={appId} appId={appId} detail={detail} pageId={pageId} rawParams={raw} onNavigate={go}
                  onExit={() => { setEditing(false); reloadDetail() }} onPublished={reloadDetail} />
    )
  }
  return <AppView appId={appId} pageId={pageId} raw={raw} detail={detail} onEdit={() => setEditing(true)} onNavigate={go} />
}

function AppView({ appId, pageId, raw, detail, onEdit, onNavigate }: {
  appId: string; pageId?: string; raw: string | null; detail?: AppDetail
  onEdit: () => void; onNavigate: (pageId: string) => void
}) {
  const navigate = useNavigate()
  const builder = useAtLeast('builder')
  const { data: model, error, loading, reload } = useLoad((signal) => {
    const params = parseViewParams(raw)
    return pageId ? api.apps.page(appId, pageId, { params }, { signal }) : loadHome(appId, params, { signal })
  }, [appId, pageId, raw])
  usePageTitle(model ? `${APP_TEXT.title} · ${model.app.name}` : null)
  const running = !!model && Object.values(model.sources).some((s) => s.state === 'running')
  usePoll(reload, POLL_MS, running)
  const editable = editableOf(detail)
  const personal = detail?.scope === 'personal'
  // 归档了的应用不能在页面上改；搭建者照旧能打开应用编辑器看（编辑器里写明已归档）
  const editorOnly = !editable && builder && detail?.my_role === 'manage' && !personal

  return (
    <div className="flex h-full flex-col" data-app-page={model?.page.id ?? ''} data-app-scope={detail?.scope ?? ''}>
      <PageHeader
        icon={<LayoutGrid size={14} />}
        title={model?.app.name ?? APP_TEXT.title}
        subtitle={model && model.pages.length <= 1 ? model.page.title : undefined}
        actions={(
          <div className="flex items-center gap-1.5">
            {loading && model && <Spinner size={12} />}
            {personal && <span className="chip" title={PERSONAL_TEXT.badgeHint} data-app-personal="">{PERSONAL_TEXT.badge}</span>}
            <button type="button" className="btn btn-sm btn-ghost" onClick={reload} aria-label={APP_TEXT.reload} title={APP_TEXT.reload}>
              <RotateCw size={12} aria-hidden />
            </button>
            {editable && (
              <button type="button" className="btn btn-sm" onClick={onEdit} title={PAGE_EDIT_TEXT.editHint} data-page-edit-start="">
                <PencilLine size={12} aria-hidden /> {PAGE_EDIT_TEXT.edit}
              </button>
            )}
            {editorOnly && (
              <button type="button" className="btn btn-sm" onClick={() => navigate(`${appPath(appId)}/edit`)}>
                <Settings2 size={12} aria-hidden /> {PAGE_EDIT_TEXT.openEditor}
              </button>
            )}
          </div>
        )}
      />
      {model && model.pages.length > 1 && (
        <Tabs label={APP_TEXT.pagesLabel} tabs={model.pages.map((p) => ({ key: p.id, label: p.title || p.id }))}
              active={model.page.id} onChange={onNavigate} />
      )}
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-5xl space-y-3 p-4">
          {error && !model ? <LoadError error={error} onRetry={reload} />
            : !model ? <Skeleton rows={6} />
            : (
              <>
                <AppMeta app={model.app} back={<BackLink to="/apps?tab=apps" label={APP_TEXT.backToApps} />} />
                {/* 刷新之后重取页面：来源换成正在运行的那次，生成完自动换上（剩余次数以刷新的回包为准） */}
                {personal && detail && <PersonalBar detail={detail} onRefreshed={reload} />}
                {model.app.needs_upgrade && (
                  <Notice tone="warn" attr={{ 'data-app-needs-upgrade': '' }}>
                    <span className="font-medium">{APP_TEXT.needsUpgrade}</span>：{APP_TEXT.needsUpgradeHint}
                  </Notice>
                )}
                {/* 按新口径补算（D-f）：只有 manage 看得到，固定版本升了版、处置为按新口径重算时才有 */}
                {!personal && <BackfillPanel appId={appId} publishedVersion={model.app.version} onDone={reload} />}
                <ViewRenderer model={model} appId={appId} onNavigate={onNavigate} onReload={reload} />
              </>
            )}
        </div>
      </div>
    </div>
  )
}
