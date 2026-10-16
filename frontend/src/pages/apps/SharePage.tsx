/**
 * 分享页（/s#<分享 key>，不在外壳里，同登录页的写法；04 细案 C2）：拿到分享链接的人不用登录就能看应用的一页。
 *
 * - key 在 # 后面：打开页面时不发给服务器、不进访问日志和 Referer，这里读出来放进请求头（api/shares.ts）；
 * - 用同一个 ViewRenderer 渲染服务端按分享解析好的视图模型（不另写渲染器）：preview 打开，所有操作禁用；入口、表单、实例、
 *   待办、报告正文服务端已经换成写明原因的说明块；
 * - 印章常显：页面上没放印章组件的视图，也在「数据的来历」里逐个列出它的印章（截至、口径、出具档位、来源）；
 * - 出处给「下载报告」：点数字不开在线证据，说明出处在下载的报告里（报告文件自己离线点得开）；
 * - 页脚写「只读 · 由 X 于 … 分享 · … 到期」；撤销、到期、关了开关、链接不完整，照服务端的话写明，不给空白页；
 * - 这条分享没允许嵌入、页面又在别的页面的 iframe 里：不显示内容，给「在新窗口打开」（服务端在 /s 上只能按全站的嵌入白名单
 *   发头，打开页面那一刻它还不知道是哪一条分享）。
 *
 * 不认人、不取目录、不连心跳（App 在 /s 上整个跳过外壳的启动），也就不会因为没登录被送去登录页。
 */
import { useEffect, useMemo, useState } from 'react'
import { Download, ExternalLink, Lock } from 'lucide-react'
import { ApiError } from '../../api/http'
import { shares } from '../../api/shares'
import { ViewRenderer } from '../../apps/ViewRenderer'
import { Stamp } from '../../apps/Stamp'
import { Logo } from '../../components/Logo'
import { Modal, Notice, Spinner, toast } from '../../components/ui'
import { formatDateTime } from '../../lib/format'
import { SHARE_PAGE_TEXT as T } from '../../lib/termsApp'
import type { SharePayload } from '../../typesApp'

/** 链接里 # 后面的那一段就是分享 key */
export const shareKeyOf = (hash: string): string => decodeURIComponent(hash.replace(/^#/, '').trim())

const framed = (): boolean => {
  try {
    return window.self !== window.top
  } catch {
    // 跨源的父页面读 top 会抛：那就是被别的站点嵌着
    return true
  }
}

export function SharePage() {
  const key = useMemo(() => shareKeyOf(window.location.hash), [])
  const [data, setData] = useState<SharePayload | null>(null)
  const [error, setError] = useState<{ code?: string; message: string } | null>(key ? null : { code: 'share_key_missing', message: T.noKey })
  const [note, setNote] = useState(false)

  useEffect(() => {
    if (!key) return
    shares.page(key).then(setData, (e) => setError({
      code: e instanceof ApiError ? e.code : undefined,
      message: e instanceof Error ? e.message : T.failed,
    }))
  }, [key])
  useEffect(() => {
    if (data) document.title = `${data.view.app.name} · ${data.view.page.title || data.view.page.id}`
  }, [data])

  const blocked = !!data && !data.share.embed_allowed && framed()
  const state = error ? 'error' : !data ? 'loading' : blocked ? 'framed' : 'ok'

  return (
    <div className="h-full overflow-auto bg-bg" data-share-page={state}>
      <div className="mx-auto max-w-5xl space-y-4 p-4 sm:p-6">
        <header className="flex flex-wrap items-center gap-3">
          <Logo size={26} className="text-accent" title="AgentLab" />
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-base font-semibold text-fg">
              {data ? data.view.app.name : 'AgentLab'}
              {data?.view.page.title && <span className="font-normal text-dim"> · {data.view.page.title}</span>}
            </h1>
          </div>
          {data && (
            <span className="chip flex items-center gap-1" data-share-readonly="">
              <Lock size={10} aria-hidden /> {T.readOnly} · {data.share.mode === 'fixed' ? T.fixed : T.latest}
            </span>
          )}
        </header>

        {error ? (
          <div className="rounded-lg border bg-panel p-4" data-share-error={error.code ?? ''}>
            <h2 className="text-sm font-semibold">{(error.code && T.errorTitle[error.code]) || T.failed}</h2>
            <p className="mt-1 text-xs leading-relaxed text-dim" role="alert">{error.message}</p>
          </div>
        ) : !data ? (
          <div className="flex items-center gap-2 text-xs text-dim" role="status"><Spinner size={12} /> {T.loading}</div>
        ) : blocked ? (
          <div className="rounded-lg border bg-panel p-4" data-share-framed="">
            <p className="text-xs leading-relaxed text-fg">{T.framed}</p>
            <a className="btn btn-sm mt-2" href={window.location.href} target="_blank" rel="noopener noreferrer">
              <ExternalLink size={11} aria-hidden /> {T.openDirect}
            </a>
          </div>
        ) : (
          <>
            <ViewRenderer model={data.view} appId={data.view.app.id} preview onEvidence={() => setNote(true)} />
            <Stamps data={data} />
            <footer className="space-y-2 border-t pt-3" data-share-footer="">
              <Downloads shareKey={key} data={data} />
              <p className="text-2xs text-faint" data-share-footer-text="">
                {T.footer(data.share.created_by || T.someone, formatDateTime(data.share.created_at),
                          formatDateTime(data.share.expires_at))}
              </p>
            </footer>
          </>
        )}
      </div>
      {data && (
        <Modal open={note} onClose={() => setNote(false)} title={T.evidenceTitle} width={460}
               footer={<button type="button" className="btn" onClick={() => setNote(false)}>{T.close}</button>}>
          <div className="space-y-3" data-share-evidence-note="">
            <p className="text-xs leading-relaxed text-fg">{T.evidence}</p>
            <Downloads shareKey={key} data={data} />
          </div>
        </Modal>
      )}
    </div>
  )
}

/** 印章常显：每个视图来源的印章（取到了运行的），页面上有没有放印章组件都列 */
function Stamps({ data }: { data: SharePayload }) {
  const rows = Object.entries(data.view.sources).filter(([, s]) => s.stamp)
  if (!rows.length) return null
  return (
    <section className="space-y-2" data-share-stamps={rows.length}>
      <div>
        <h2 className="text-xs font-semibold">{T.stamps}</h2>
        <p className="text-2xs text-faint">{T.stampsHint}</p>
      </div>
      <div className="grid gap-2 sm:grid-cols-2">
        {rows.map(([vid, s]) => <Stamp key={vid} stamp={s.stamp!} />)}
      </div>
    </section>
  )
}

/** 下载报告：只给这条分享解析出来、带报告的那几次运行（服务端只认这几次） */
function Downloads({ shareKey, data }: { shareKey: string; data: SharePayload }) {
  const [busy, setBusy] = useState<string | null>(null)
  const items = data.downloads.flatMap((d) => d.reports.map((node) => ({ ...d, node })))
  if (!items.length) return <p className="text-2xs text-faint" data-share-no-reports="">{T.noReports}</p>
  const download = async (runId: string, node: string) => {
    const id = `${runId}:${node}`
    setBusy(id)
    try {
      const { blob, name } = await shares.report(shareKey, runId, node)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = name ?? `报告-${runId.slice(0, 6)}.html`
      document.body.appendChild(a)
      a.click()
      a.remove()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
      toast.ok(T.downloaded)
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(null)
    }
  }
  return (
    <div className="space-y-1">
      <div className="flex flex-wrap gap-2">
        {items.map((d) => {
          const id = `${d.run_id}:${d.node}`
          return (
            <button key={id} type="button" className="btn btn-sm" disabled={busy != null} data-share-download={d.run_id}
                    onClick={() => void download(d.run_id, d.node)}>
              {busy === id ? <Spinner size={11} /> : <Download size={11} aria-hidden />}
              {busy === id ? T.downloading : T.download}
              {items.length > 1 && d.as_of_text && <span className="text-2xs text-faint">（{d.as_of_text}）</span>}
            </button>
          )
        })}
      </div>
      <Notice tone="info" attr={{ 'data-share-download-hint': '' }}>{T.downloadHint}</Notice>
    </div>
  )
}
