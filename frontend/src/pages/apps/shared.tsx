/**
 * 使用面几页共用的：取应用首页、取数据的小钩子、出错时怎么说、页头上的应用信息。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { ArrowLeft } from 'lucide-react'
import { Link } from 'react-router-dom'
import { ApiError, api } from '../../api/client'
import { ErrorState } from '../../components/ui'
import { useOnReconnect } from '../../store/catalog'
import { APP_TEXT } from '../../lib/termsApp'
import type { AppLevel, ViewModel } from '../../typesApp'

/**
 * 应用的首页：派生出来的应用第一页叫 home；改过页面的应用没有 home 时，取草稿里的第一页——草稿和发布版的页面通常
 * 同一个开头，对不上时照实报「没有这个页面」。服务端支持按「第一页」取之后这里只剩一个请求
 */
export async function loadHome(appId: string, params?: Record<string, unknown>, opts?: { signal?: AbortSignal }): Promise<ViewModel> {
  try {
    return await api.apps.page(appId, 'home', { params }, opts)
  } catch (e) {
    if (!(e instanceof ApiError && e.status === 404 && e.code === 'app_page_not_found')) throw e
    // use / observe 拿不到草稿（D-m）：按当前发布版的页面目录（published）退；老服务端只有草稿
    const app = await api.apps.get(appId, opts)
    const first = (app.published?.pages ?? app.draft?.pages ?? []).find((p) => p.id !== 'home')?.id
    if (!first) throw e
    return api.apps.page(appId, first, { params }, opts)
  }
}

/**
 * 取一份数据：load 返回 Promise，deps 变了重取；reload 手动重取（不清掉手上的那份，界面不闪）；断线恢复后自动重取。
 * 晚到的旧请求作废，不会盖掉新请求的结果
 */
export function useLoad<T>(load: (signal: AbortSignal) => Promise<T>, deps: unknown[]) {
  const [state, setState] = useState<{ data?: T; error?: unknown; loading: boolean }>({ loading: true })
  const seq = useRef(0)
  const loadRef = useRef(load)
  loadRef.current = load
  const run = useCallback((keep: boolean) => {
    const me = ++seq.current
    const ctrl = new AbortController()
    setState((s) => (keep ? { ...s, loading: true } : { loading: true }))
    loadRef.current(ctrl.signal).then(
      (data) => { if (me === seq.current) setState({ data, loading: false }) },
      (error) => {
        if (me !== seq.current || error?.name === 'AbortError') return
        setState((s) => ({ data: keep ? s.data : undefined, error, loading: false }))
      },
    )
    return () => ctrl.abort()
  }, [])
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => run(false), deps)
  const reload = useCallback(() => { run(true) }, [run])
  useOnReconnect(reload)
  return { ...state, reload }
}

/** 每 ms 毫秒调一次 fn，只在 active 时、页面可见时；回到前台立刻补一次 */
export function usePoll(fn: () => void, ms: number, active: boolean) {
  const ref = useRef(fn)
  ref.current = fn
  useEffect(() => {
    if (!active) return
    const tick = () => { if (!document.hidden) ref.current() }
    const t = setInterval(tick, ms)
    document.addEventListener('visibilitychange', tick)
    return () => { clearInterval(t); document.removeEventListener('visibilitychange', tick) }
  }, [ms, active])
}

/** 应用、实例、待办取不到时：不存在、没授权说人话，其余照通用的出错态 */
export function LoadError({ error, onRetry, notFound }: { error: unknown; onRetry: () => void; notFound?: string }) {
  if (error instanceof ApiError && error.status === 404) {
    return <ErrorState error={new ApiError(404, notFound ?? APP_TEXT.notFound, { code: error.code })} />
  }
  if (error instanceof ApiError && error.status === 403) {
    return <ErrorState error={new ApiError(403, error.message || APP_TEXT.noAccess, { code: error.code })} />
  }
  return <ErrorState error={error} onRetry={onRetry} />
}

/** 页头下面的一行：应用版本、受管与否、返回 */
export function AppMeta({ app, back }: { app: { version: number; level: AppLevel | null }; back?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-2xs text-dim">
      {back}
      {app.version > 0 && <span className="chip">{APP_TEXT.version(app.version)}</span>}
      {app.level && <span className="chip">{app.level === 'governed' ? APP_TEXT.levelGoverned : APP_TEXT.levelPublished}</span>}
    </div>
  )
}

export function BackLink({ to, label }: { to: string; label: string }) {
  return (
    <Link to={to} className="inline-flex items-center gap-1 text-dim hover:text-fg">
      <ArrowLeft size={11} aria-hidden /> {label}
    </Link>
  )
}

export const appPath = (appId: string) => `/apps/${encodeURIComponent(appId)}`

/**
 * 新建应用的地址（应用编辑的 new）。新建是向导的起点，现在只有「从已有的工作流开始」一种：带上工作流和版本时进来就按
 * 那一版派生（发布成功的弹窗、版本历史里的「做成应用」），不带时先在页上选（应用首页的「新建应用」）
 */
export const newAppPath = (from?: { workflow: string; version: number }) =>
  `/apps/new/edit${from ? `?${new URLSearchParams({ workflow: from.workflow, version: String(from.version) })}` : ''}`
