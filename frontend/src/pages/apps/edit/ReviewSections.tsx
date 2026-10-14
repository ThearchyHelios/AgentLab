import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, CheckCircle2, Crosshair, RotateCw, ShieldCheck, XCircle } from 'lucide-react'
import { api } from '../../../api/client'
import { EmptyState, ErrorState, Field, Modal, Notice, SectionBar, Skeleton, Spinner, toast } from '../../../components/ui'
import { formatDateTime } from '../../../lib/format'
import { APP_EDITOR_TEXT as T } from '../../../lib/terms'
import { ViewRenderer } from '../../../apps/ViewRenderer'
import type { AppVersionRow, Disposition, LintIssue, PublishResult, ViewModel } from '../../../typesApp'
import { locatePath } from './appOps'
import { useEditor } from './context'

/*
 * 预览、检查、发布三节。三样都读服务端那份草稿（预览按草稿解析、检查和发布同一套规则），所以有未保存的修改时先存。
 *
 * 发布：固定版本相对上一个应用版本升了版的，服务端回 needs_disposition，这里让人为每一项选 recompute / dual /
 * incomparable，写清后果，再带着处置发布（口径升版是必须处置的事件，不替人选默认值）
 */

const DISPOSITIONS: Disposition[] = ['recompute', 'dual', 'incomparable']

export function PreviewSection() {
  const { appId, draft, dirty, save, goto } = useEditor()
  const [pageId, setPageId] = useState(draft.pages[0]?.id ?? '')
  const [model, setModel] = useState<ViewModel | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    if (!pageId) return
    setBusy(true)
    setError(null)
    try {
      if (dirty && !(await save())) return
      setModel(await api.apps.page(appId, pageId, { draft: true }))
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }, [appId, pageId, dirty, save])
  useEffect(() => { void load() }, [pageId]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="flex h-full flex-col" data-app-preview={pageId}>
      <div className="flex shrink-0 items-center gap-2 border-b px-4 py-2">
        <select className="field h-7 w-48 py-0 text-xs" value={pageId} aria-label={T.previewPage} onChange={(e) => setPageId(e.target.value)}>
          {draft.pages.map((p) => <option key={p.id} value={p.id}>{p.title || p.id}</option>)}
        </select>
        <button type="button" className="btn btn-sm" disabled={busy} onClick={() => void load()}>
          {busy ? <Spinner size={11} /> : <RotateCw size={11} aria-hidden />} {T.previewReload}
        </button>
        <span className="min-w-0 flex-1 truncate text-2xs text-faint">{dirty ? T.previewSaveFirst : T.previewHint}</span>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        {error ? <ErrorState error={error} onRetry={() => void load()} />
          : !model ? <Skeleton rows={6} height={40} gap={10} />
          : (
            <>
              {model.problems.length > 0 && (
                <Notice tone="warn" className="mb-3" attr={{ 'data-app-preview-problems': String(model.problems.length) }}>
                  {model.problems.map((p, i) => (
                    <div key={i}>
                      <button type="button" className="underline-offset-2 hover:underline" onClick={() => goto('pages', p.component)}>
                        {p.component}
                      </button>：{p.message}
                    </div>
                  ))}
                </Notice>
              )}
              <ViewRenderer model={model} appId={appId} preview onNavigate={setPageId} onReload={() => void load()} />
            </>
          )}
      </div>
    </div>
  )
}

export function CheckSection() {
  const { appId, draft, dirty, save, goto } = useEditor()
  const [result, setResult] = useState<{ issues: LintIssue[]; level: string | null } | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  const run = useCallback(async () => {
    setBusy(true)
    setError(null)
    try {
      if (dirty && !(await save())) return
      setResult(await api.apps.lint(appId))
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }, [appId, dirty, save])
  useEffect(() => { void run() }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const errors = result?.issues.filter((i) => i.level === 'error') ?? []
  const warnings = result?.issues.filter((i) => i.level !== 'error') ?? []
  return (
    <div className="mx-auto max-w-3xl p-4" data-app-check={result ? result.issues.length : ''}>
      <SectionBar title={T.sections.check} hint={T.checkHint}>
        <button type="button" className="btn btn-sm" disabled={busy} onClick={() => void run()} data-app-check-run="">
          {busy ? <Spinner size={11} /> : <RotateCw size={11} aria-hidden />} {T.checkRun}
        </button>
      </SectionBar>
      {error ? <ErrorState error={error} onRetry={() => void run()} />
        : !result ? <Skeleton rows={4} height={32} />
        : !result.issues.length ? (
          <div className="flex items-center gap-2 text-xs" style={{ color: 'var(--ok)' }} data-app-check-passed="">
            <CheckCircle2 size={14} aria-hidden /> {T.checkPassed}
            {result.level && <span className="text-faint">· {T.checkLevel(result.level)}</span>}
          </div>
        ) : (
          <>
            <p className="mb-2 text-xs text-dim">{T.checkCounts(errors.length, warnings.length)}</p>
            <ul className="divide-y rounded-lg border">
              {[...errors, ...warnings].map((issue, i) => {
                const where = locatePath(draft, issue.path)
                return (
                  <li key={i} className="flex items-start gap-2 px-3 py-2 text-xs" data-app-issue={issue.code} data-level={issue.level}>
                    {issue.level === 'error'
                      ? <XCircle size={13} className="mt-0.5 shrink-0" style={{ color: 'var(--err)' }} aria-hidden />
                      : <AlertTriangle size={13} className="mt-0.5 shrink-0" style={{ color: 'var(--warn)' }} aria-hidden />}
                    <div className="min-w-0 flex-1">
                      <div>{issue.message}</div>
                      <div className="mono text-2xs text-faint">{issue.code}{issue.path ? ` · ${issue.path}` : ''}</div>
                    </div>
                    {where && (
                      <button type="button" className="btn btn-xs btn-ghost shrink-0" onClick={() => goto(where.section, where.component, where.path)}
                              data-app-issue-locate="">
                        <Crosshair size={10} aria-hidden /> {T.locate}
                      </button>
                    )}
                  </li>
                )
              })}
            </ul>
          </>
        )}
    </div>
  )
}

export function PublishSection({ onPublished }: { onPublished: () => void }) {
  const { appId, dirty, save, goto, draft } = useEditor()
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [blocked, setBlocked] = useState<LintIssue[] | null>(null)
  const [asking, setAsking] = useState<NonNullable<PublishResult['needs_disposition']> | null>(null)
  const [versions, setVersions] = useState<AppVersionRow[] | null>(null)
  const loadVersions = useCallback(() => {
    api.apps.versions(appId).then(({ items }) => setVersions(items)).catch(() => setVersions([]))
  }, [appId])
  useEffect(loadVersions, [loadVersions])

  const publish = async (dispositions?: Record<string, Disposition>) => {
    setBusy(true)
    setBlocked(null)
    try {
      if (dirty && !(await save())) return
      const res = await api.apps.publish(appId, { note: note.trim() || undefined, ...(dispositions ? { dispositions } : {}) })
      if (res.ok && res.version != null) {
        toast.ok(T.published(res.version, res.level))
        setAsking(null)
        setNote('')
        loadVersions()
        onPublished()
        return
      }
      // 升了版却没选处置：先让人选，再带着处置重发。别的问题照检查的样子列出来
      if (res.needs_disposition?.length && !dispositions) setAsking(res.needs_disposition)
      const rest = res.issues.filter((i) => i.code !== 'app.upgrade_policy' || !res.needs_disposition?.length)
      if (rest.length) {
        setBlocked(rest)
        if (!res.needs_disposition?.length) toast.warn(T.publishBlocked)
      }
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mx-auto max-w-3xl space-y-6 p-4" data-app-publish="">
      <section>
        <SectionBar title={T.sections.publish} hint={T.publishHint} />
        <div className="flex items-end gap-2">
          <Field label={T.publishNote} className="min-w-0 flex-1">
            {(p) => <input {...p} className="field" value={note} onChange={(e) => setNote(e.target.value)} />}
          </Field>
          <button type="button" className="btn btn-primary" disabled={busy || !draft.pins.length} onClick={() => void publish()}
                  data-app-publish-submit="">
            {busy ? <Spinner size={11} /> : <ShieldCheck size={11} aria-hidden />} {busy ? T.publishing : T.publish}
          </button>
        </div>
        {blocked && (
          <ul className="mt-3 divide-y rounded-lg border" data-app-publish-blocked={blocked.length}>
            {blocked.map((issue, i) => {
              const where = locatePath(draft, issue.path)
              return (
                <li key={i} className="flex items-start gap-2 px-3 py-2 text-xs" data-app-issue={issue.code}>
                  <XCircle size={13} className="mt-0.5 shrink-0" style={{ color: issue.level === 'error' ? 'var(--err)' : 'var(--warn)' }} aria-hidden />
                  <span className="min-w-0 flex-1">{issue.message}</span>
                  {where && (
                    <button type="button" className="btn btn-xs btn-ghost" onClick={() => goto(where.section, where.component, where.path)}>
                      {T.locate}
                    </button>
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </section>
      <section>
        <SectionBar title={T.versions} />
        {!versions ? <Skeleton rows={2} /> : !versions.length ? <EmptyState title={T.versionsEmpty} /> : (
          <ul className="divide-y rounded-lg border text-xs">
            {versions.map((v) => (
              <li key={v.version} className="px-3 py-2" data-app-version={v.version}>
                <div className="tnum">{T.versionRow(v.version, v.published_by, formatDateTime(v.published_at))}</div>
                <div className="text-2xs text-faint">
                  {v.pins.map((p) => `${p.alias} v${p.version}`).join('、')}
                  {Object.keys(v.dispositions).length > 0
                    && ` · ${Object.entries(v.dispositions).map(([a, d]) => `${a}：${T.dispositions[d]}`).join('、')}`}
                  {v.note ? ` · ${v.note}` : ''}
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
      {asking && <DispositionDialog items={asking} busy={busy} onClose={() => setAsking(null)} onConfirm={(d) => void publish(d)} />}
    </div>
  )
}

/** 为每个升版的固定版本选处置，写清后果。不给默认值：必须由人选 */
function DispositionDialog({ items, busy, onClose, onConfirm }: {
  items: NonNullable<PublishResult['needs_disposition']>
  busy: boolean
  onClose: () => void
  onConfirm: (d: Record<string, Disposition>) => void
}) {
  const [picked, setPicked] = useState<Record<string, Disposition>>({})
  const ready = items.every((x) => picked[x.alias])
  return (
    <Modal open onClose={onClose} title={T.dispositionTitle} width={620}
           footer={<>
             <button className="btn" onClick={onClose}>取消</button>
             <button className="btn btn-primary" disabled={!ready || busy} title={ready ? undefined : T.dispositionPick}
                     onClick={() => onConfirm(picked)} data-app-disposition-submit="">
               {busy ? <Spinner size={11} /> : null} {T.publishWithDispositions}
             </button>
           </>}>
      <p className="mb-3 text-xs leading-relaxed text-dim">{T.dispositionIntro}</p>
      <div className="space-y-3">
        {items.map((x) => (
          <fieldset key={x.alias} className="rounded-lg border px-3 py-2" data-app-disposition={x.alias}>
            <legend className="px-1 text-xs font-medium">{T.dispositionRow(x.alias, x.from, x.to)}</legend>
            {DISPOSITIONS.map((d) => (
              <label key={d} className="flex cursor-pointer items-start gap-2 py-1 text-xs">
                <input type="radio" name={`disp-${x.alias}`} className="mt-0.5 accent-[var(--accent)]" checked={picked[x.alias] === d}
                       onChange={() => setPicked((p) => ({ ...p, [x.alias]: d }))} data-app-disposition-option={d} />
                <span>
                  <span className="font-medium">{T.dispositions[d]}</span>
                  <span className="mt-0.5 block text-2xs leading-relaxed text-faint">{T.dispositionHint[d]}</span>
                </span>
              </label>
            ))}
          </fieldset>
        ))}
      </div>
    </Modal>
  )
}
