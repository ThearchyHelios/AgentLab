import { useCallback, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ArrowLeft, Check, ExternalLink, Table2 } from 'lucide-react'
import clsx from 'clsx'
import { ApiError, api } from '../../api/client'
import type { SourceChange, SourceChangeAction, SourceChangeActionOption, SourceChangeResolveBody, SourceChangeResolveOut } from '../../typesSource'
import { EmptyState, ErrorState, Notice, Skeleton, Spinner, confirmDialog, toast } from '../../components/ui'
import { formatDateTime } from '../../lib/format'
import { CATALOG_STATUS_LABEL, SOURCE_CHANGE_KIND_LABEL as KL, SOURCE_CHANGE_TEXT as T } from '../../lib/terms'
import { SEVERITY_MARK } from './ChangesQueue'

// ===========================================================================
// 一条变化（数据目录页「变化」视图的右栏）：服务端的一句说法，依据（谁、经什么测到的，取值变化的观察口径），前后各是什么，
// 影响面（说到这些列的目录项和它们现在的状态、写死了这张表的已发布模板、「这条 SQL 现在会报列不存在」），能做的处理。
//
// 能做哪些动作、做不了的原因都是服务端给的（change.actions），这里只照着画；动作落到目录已有的写入接口上（清理目录项、加入
// 码值），或者建起草、剖析任务。处理完照实写结果：目录第几版、建了的任务编号（点得进进度），谁、什么时候、理由。误报必须写
// 理由（按钮在写了之前不能点，服务端也会拒）。
// ===========================================================================

export function ChangeDetail({ sourceId, changeId, initial, onBack, onResolved, onOpenTable, onOpenJob }: {
  sourceId: string
  changeId: string
  /** 列表里已经有的这一条：先照它画，取回来再换 */
  initial?: SourceChange | null
  /** 窄屏：回到变化列表 */
  onBack: () => void
  /** 处理过了：列表重取、计数跟着变；动了目录的，页面重取清单、重新载入那几张表 */
  onResolved: (out: SourceChangeResolveOut) => void
  onOpenTable: (table: string) => void
  /** 打开起草、剖析任务的进度 */
  onOpenJob: (kind: 'draft' | 'profile', id: string) => void
}) {
  const [change, setChange] = useState<SourceChange | null>(initial?.id === changeId ? initial : null)
  const [loadError, setLoadError] = useState<unknown>(null)
  const [busy, setBusy] = useState<SourceChangeAction | null>(null)
  const [error, setError] = useState<string | null>(null)
  /** 正在填的表单：加入码值（码值 → 含义）、误报（理由） */
  const [form, setForm] = useState<{ kind: 'add_codes'; codes: Record<string, string> } | { kind: 'dismiss'; note: string } | null>(null)
  const seq = useRef(0)

  const load = useCallback(async () => {
    const mine = ++seq.current
    try {
      const { change: next } = await api.sourceChanges.get(sourceId, changeId)
      if (mine !== seq.current) return
      setChange(next)
      setLoadError(null)
    } catch (e) {
      if (mine === seq.current) setLoadError(e)
    }
  }, [sourceId, changeId])
  useEffect(() => {
    setChange(initial?.id === changeId ? initial : null)
    setLoadError(null)
    setError(null)
    setForm(null)
    void load()
    // 只在换了一条时重来：initial 跟着列表刷新会变，不因为它重取
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load, changeId])

  const resolve = async (body: SourceChangeResolveBody) => {
    if (!change || busy) return
    setBusy(body.action)
    setError(null)
    try {
      const out = await api.sourceChanges.resolve(sourceId, change.id, body)
      setChange(out.change)
      setForm(null)
      toast.ok(T.done[body.action])
      onResolved(out)
    } catch (e) {
      // 误报没写理由、建不了任务（422），处理过了、目录被别人刚改过（409）：服务端的原话写在处理一节里
      if (e instanceof ApiError && e.kind === 'http' && (e.status === 422 || e.status === 409)) setError(e.message)
      else toast.error(e)
    } finally {
      setBusy(null)
    }
  }

  const act = async (a: SourceChangeActionOption) => {
    if (!change || !a.ok) return
    if (a.action === 'add_codes') {
      const codes: string[] = Array.isArray(change.after?.codes) ? change.after.codes.map(String) : []
      setForm({ kind: 'add_codes', codes: Object.fromEntries(codes.map((c) => [c, ''])) })
      return
    }
    if (a.action === 'dismiss') {
      setForm({ kind: 'dismiss', note: '' })
      return
    }
    if (a.action === 'clean_catalog') {
      const n = change.affects.catalog.filter((r) => r.now).length || change.affects.catalog.length
      const ok = await confirmDialog({ title: T.cleanTitle(n), consequences: T.cleanConsequences, confirmLabel: T.cleanConfirm, danger: true })
      if (!ok) return
    }
    await resolve({ action: a.action })
  }

  if (!change) {
    return (
      <div className="relative min-h-0 flex-1 overflow-y-auto p-4" data-change-detail="loading">
        {loadError
          ? (loadError instanceof ApiError && loadError.status === 404
            ? <EmptyState title={T.missing} offline={false} action={<button className="btn btn-sm" onClick={onBack}>{T.backToList}</button>} />
            : <ErrorState error={loadError} onRetry={() => void load()} />)
          : <Skeleton rows={6} height={28} gap={10} />}
      </div>
    )
  }

  const mark = SEVERITY_MARK[change.severity]
  const done = change.status === 'resolved' || change.status === 'dismissed'
  return (
    <div className="relative min-h-0 flex-1 overflow-y-auto" data-change-detail={change.id} data-change-status={change.status}>
      <div className="mx-auto max-w-3xl space-y-4 p-4">
        <div className="flex items-center gap-2 lg:hidden">
          <button type="button" className="btn btn-sm btn-ghost" onClick={onBack} data-change-back="">
            <ArrowLeft size={12} aria-hidden /> {T.backToList}
          </button>
        </div>
        <header className="space-y-1.5">
          <div className="flex flex-wrap items-center gap-2 text-2xs">
            <span className="inline-flex items-center gap-1 font-semibold" style={{ color: mark.color }} title={T.severityHint[change.severity]}
                  data-change-severity-label="">
              <mark.Icon size={12} aria-hidden /> {T.severity[change.severity]}
            </span>
            <span className="chip !py-0">{T.category[change.category]}</span>
            <span className="chip !py-0">{KL[change.kind] ?? change.kind}</span>
            <span className="text-faint" data-change-state="">{T.status[change.status]}</span>
          </div>
          <h2 className="break-words text-sm font-semibold" data-change-summary="">{change.summary}</h2>
          {change.table && (
            <button type="button" className="inline-flex items-center gap-1 rounded text-2xs text-[var(--accent)] underline-offset-2 outline-none hover:underline focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
                    onClick={() => onOpenTable(change.table!)} data-change-open-table={change.table}>
              <Table2 size={11} aria-hidden /> {T.openTable}
            </button>
          )}
        </header>

        <Block title={T.basisTitle} attr="basis">
          <p className="text-xs text-dim" data-change-detected="">
            {T.detectedBy(T.jobKind[change.detected_by.job_kind] ?? change.detected_by.job_kind,
              T.trigger[change.detected_by.trigger] ?? change.detected_by.trigger, change.detected_by.actor)}
            {' · '}<span className="tnum" title={formatDateTime(change.detected_at)}>{T.detectedAt(formatDateTime(change.detected_at))}</span>
            {change.seen_count > 1 && <span className="tnum"> · {T.seen(change.seen_count)}</span>}
          </p>
          {change.category === 'struct'
            ? <p className="text-xs text-dim" data-change-basis="struct">{T.structBasis(change.struct_version_before, change.struct_version_after)}</p>
            : change.basis_text && (
              <p className="text-xs text-dim" data-change-basis="value">
                {T.valueBasis(change.basis_text)}<span className="text-faint">。{T.valueBasisNote}</span>
              </p>
            )}
        </Block>

        <Facts change={change} />

        <Impact change={change} />

        {done
          ? <Resolution change={change} onOpenJob={onOpenJob} />
          : (
            <Block title={T.actionsTitle} attr="actions">
              {change.resolution && <ResolutionLine change={change} />}
              <div className="flex flex-wrap gap-1.5" data-change-actions="">
                {change.actions.map((a) => (
                  <button key={a.action} type="button" disabled={!a.ok || !!busy || !!form}
                          className={clsx('btn btn-sm', a.action === 'clean_catalog' && a.ok && 'btn-primary', a.action === 'dismiss' && 'btn-ghost')}
                          title={a.ok ? T.actionHint[a.action] : T.unavailable(T.action[a.action], a.reason ?? '')}
                          aria-describedby={!a.ok ? `change-why-${a.action}` : undefined}
                          onClick={() => void act(a)} data-change-action={a.action} data-change-action-ok={a.ok ? '1' : '0'}>
                    {busy === a.action && <Spinner size={11} />}{T.action[a.action]}
                  </button>
                ))}
              </div>
              {change.actions.filter((a) => !a.ok && a.reason).map((a) => (
                <p key={a.action} id={`change-why-${a.action}`} className="text-2xs leading-relaxed text-faint" data-change-why={a.action}>
                  {T.unavailable(T.action[a.action], a.reason!)}
                </p>
              ))}
              {change.job && <JobLine job={change.job} onOpenJob={onOpenJob} />}
              {form?.kind === 'add_codes' && (
                <CodesForm codes={form.codes} busy={busy === 'add_codes'} onChange={(codes) => setForm({ kind: 'add_codes', codes })}
                           onCancel={() => setForm(null)} onSubmit={() => void resolve({ action: 'add_codes', codes: form.codes })} />
              )}
              {form?.kind === 'dismiss' && (
                <DismissForm note={form.note} busy={busy === 'dismiss'} onChange={(note) => setForm({ kind: 'dismiss', note })}
                             onCancel={() => setForm(null)} onSubmit={() => void resolve({ action: 'dismiss', note: form.note.trim() })} />
              )}
              {error && <Notice tone="err" attr={{ 'data-change-error': '' }}>{error}</Notice>}
            </Block>
          )}
      </div>
    </div>
  )
}

function Block({ title, attr, children }: { title: string; attr: string; children: ReactNode }) {
  return (
    <section className="space-y-1.5" aria-label={title} data-change-section={attr}>
      <h3 className="text-xs font-semibold">{title}</h3>
      <div className="space-y-1.5 rounded-lg border bg-panel px-3 py-2.5">{children}</div>
    </section>
  )
}

const str = (v: unknown): string => (v == null || v === '' ? T.none : Array.isArray(v) ? v.join('、') : String(v))
const fk = (v: any): string => (v && typeof v === 'object'
  ? `${(v.columns ?? []).join('、')} → ${v.to_table}.${(v.to_columns ?? []).join('、')}` : T.none)

/** 前后各是什么：按种类写（服务端记下的 before / after 照原样，数都是服务端算好的） */
function Facts({ change: c }: { change: SourceChange }) {
  const b = c.before ?? null
  const a = c.after ?? null
  const lines: ReactNode[] = []
  const pair = (from: string, to: string) => (
    <span className="mono break-all">{from} <span className="text-faint">→</span> {to}</span>
  )
  const rename = (a && a.rename) || (b && b.rename)
  switch (c.kind) {
    case 'column_type_changed':
      lines.push(pair(str(b), str(a)))
      break
    case 'column_nullable_changed':
      lines.push(pair(T.nullable(!!b), T.nullable(!!a)))
      break
    case 'comment_changed':
      lines.push(pair(str(b), str(a)))
      break
    case 'column_removed':
      if (b?.type) lines.push(<span>{T.before}：<span className="mono">{b.type}</span>{b.nullable === false ? `，${T.nullable(false)}` : ''}</span>)
      break
    case 'column_added':
      if (a?.type) lines.push(<span>{T.after}：<span className="mono">{a.type}</span>{a.nullable === false ? `，${T.nullable(false)}` : ''}</span>)
      break
    case 'key_changed':
      lines.push(pair(str(b), str(a)))
      break
    case 'fk_changed':
      lines.push(pair(fk(b), fk(a)))
      break
    case 'table_reflect_failed':
      if (a?.error) lines.push(T.reflectFailed(String(a.error)))
      break
    case 'scope_changed':
      if (a) lines.push(T.scopeRules(Number(a.entered ?? 0), Number(a.left ?? 0)))
      break
    case 'codes_new': {
      const codes: string[] = Array.isArray(a?.codes) ? a.codes.map(String) : []
      const shares = a?.shares ?? {}
      lines.push(T.newCodes(codes.map((x) => (typeof shares[x] === 'number' ? T.codeShare(x, shares[x]) : x)).join('、')))
      if (Array.isArray(b?.known)) lines.push(<span className="text-faint">{T.knownCodes(b.known.length)}</span>)
      if (a?.complete_revoked === true) lines.push(<span data-change-revoked="" className="text-[var(--warn)]">{T.completeRevoked(a.catalog_version ?? null)}</span>)
      else if (a?.complete_revoked === false) lines.push(<span className="text-faint">{T.completeKept}</span>)
      break
    }
    case 'codes_shift':
      if (typeof a?.psi === 'number') lines.push(T.shift(a.psi))
      for (const [code, from, to] of (a?.top ?? []) as [string, number, number][]) lines.push(<span className="tnum">{T.shiftTop(code, from, to)}</span>)
      break
    case 'date_stalled':
      if (a?.max) lines.push(T.stalled(a.max, Number(a.age_days ?? 0), Number(a.threshold_days ?? 0)))
      break
    case 'date_gap':
      if (a) lines.push(T.gap(Number(a.gaps ?? 0), (a.days ?? []).join('、')))
      break
    case 'volume_drop':
      if (a?.day) lines.push(T.volume(a.day, Number(a.rows ?? 0), Number(b?.median ?? 0)))
      break
    case 'rows_drop':
    case 'rows_jump':
      if (typeof b?.rows === 'number' && typeof a?.rows === 'number') lines.push(<span className="tnum">{T.rows(b.rows, a.rows, !!a.estimated)}</span>)
      break
    case 'coverage_drop':
      if (typeof b?.coverage === 'number' && typeof a?.coverage === 'number') lines.push(T.coverage(String(b.target ?? ''), b.coverage, a.coverage))
      break
    default:
      break
  }
  if (rename?.from && rename?.to) lines.push(<span className="text-[var(--warn)]" data-change-rename="">{T.rename(rename.from, rename.to)}</span>)
  if (!lines.length) return null
  return (
    <Block title={T.factsTitle} attr="facts">
      <ul className="space-y-1 text-xs text-dim" data-change-facts={c.kind}>
        {lines.map((l, i) => <li key={i} className="break-words">{l}</li>)}
      </ul>
    </Block>
  )
}

/** 影响面：说到这些列的目录项（测到时和现在的状态）、用到这张表的已发布模板（写死的 SQL 会不会坏）、有效记录条件 */
function Impact({ change: c }: { change: SourceChange }) {
  const { catalog, templates, rules_touched: rules } = c.affects
  const empty = !catalog.length && !templates.length && !rules
  return (
    <Block title={T.impactTitle} attr="impact">
      {empty && <p className="text-xs text-faint" data-change-impact-none="">{T.impactNone}</p>}
      {catalog.length > 0 && (
        <div data-change-impact-catalog={catalog.length}>
          <p className="text-2xs font-medium text-dim">{T.impactCatalog(catalog.length)}</p>
          <ul className="mt-1 space-y-0.5">
            {catalog.map((r) => (
              <li key={`${r.table}\u0000${r.path}`} className="flex flex-wrap items-baseline gap-x-2 text-xs" data-change-ref={`${r.table}::${r.path}`}
                  data-change-ref-now={r.now?.status ?? 'gone'}>
                <span className="min-w-0 break-words">{r.label}</span>
                <span className="mono text-2xs text-faint">{r.table}</span>
                <span className="text-2xs text-faint">
                  {r.status ? CATALOG_STATUS_LABEL[r.status as keyof typeof CATALOG_STATUS_LABEL] ?? r.status : ''}
                  {' · '}{T.impactNow}：{r.now ? (CATALOG_STATUS_LABEL[r.now.status as keyof typeof CATALOG_STATUS_LABEL] ?? r.now.status) : T.impactGone}
                </span>
                {r.needs_action && r.now && <span className="text-2xs text-[var(--err)]" data-change-ref-needs="">{T.impactNeeds}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
      {rules && <p className="text-xs text-[var(--warn)]" data-change-rules="">{T.impactRules}</p>}
      {templates.length > 0 && (
        <div data-change-impact-templates={templates.length}>
          <p className="text-2xs font-medium text-dim">{T.impactTemplates(templates.length)}</p>
          <ul className="mt-1 space-y-1">
            {templates.map((t) => (
              <li key={`${t.workflow_id}:${t.version}`} className="text-xs" data-change-template={t.workflow_id}>
                <Link to={`/studio/${encodeURIComponent(t.workflow_id)}${t.nodes[0] ? `?focus=${encodeURIComponent(t.nodes[0].node_id)}` : ''}`}
                      className="inline-flex items-center gap-1 text-[var(--accent)] underline-offset-2 hover:underline">
                  {t.name} <span className="tnum text-faint">{T.templateVersion(t.version)}</span> <ExternalLink size={10} aria-hidden />
                </Link>
                <span className="ml-2 text-2xs text-faint">{T.templateImpact[t.impact] ?? t.impact}</span>
                <ul className="ml-3 mt-0.5 space-y-0.5">
                  {t.nodes.map((n) => (
                    <li key={n.node_id} className="text-2xs text-dim" data-change-node={n.node_id} data-change-breaks={n.breaks ? '1' : '0'}>
                      <span>{n.label || n.node_id}</span>
                      <span className="ml-1.5 text-faint">{T.templateImpact[n.impact] ?? n.impact}</span>
                      {n.breaks && <span className="ml-1.5 text-[var(--err)]">{T.breaks(n.unknown_columns ?? [])}</span>}
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        </div>
      )}
    </Block>
  )
}

/** 知道了还没处理的那一行 */
function ResolutionLine({ change: c }: { change: SourceChange }) {
  const r = c.resolution!
  return (
    <p className="text-2xs text-faint" data-change-acknowledged="">
      {T.resolvedBy(T.done[r.action] ?? r.action, r.by, formatDateTime(r.at))}
    </p>
  )
}

function JobLine({ job, onOpenJob }: { job: { kind: 'draft' | 'profile'; id: string }; onOpenJob: (kind: 'draft' | 'profile', id: string) => void }) {
  return (
    <p className="flex flex-wrap items-center gap-2 text-xs" data-change-job={job.id} data-change-job-kind={job.kind}>
      <span className="mono">{T.jobLink(T.resolvedJob[job.kind], job.id)}</span>
      <button type="button" className="btn btn-xs" onClick={() => onOpenJob(job.kind, job.id)} data-change-job-open="">{T.viewProgress}</button>
    </p>
  )
}

/** 处理过的：谁、什么时候、什么动作、目录第几版、建了的任务、理由 */
function Resolution({ change: c, onOpenJob }: { change: SourceChange; onOpenJob: (kind: 'draft' | 'profile', id: string) => void }) {
  const r = c.resolution
  return (
    <Block title={T.resolutionTitle} attr="resolution">
      {r ? (
        <div className="space-y-1 text-xs" data-change-resolution={r.action}>
          <p className="flex items-center gap-1.5 font-medium">
            <Check size={12} style={{ color: 'var(--st-done)' }} aria-hidden />
            {T.resolvedBy(T.done[r.action] ?? r.action, r.by, formatDateTime(r.at))}
          </p>
          {r.catalog_version != null && <p className="tnum text-dim" data-change-resolved-version={r.catalog_version}>{T.resolvedVersion(r.catalog_version)}</p>}
          {r.catalog_versions && Object.keys(r.catalog_versions).length > 1 && (
            <p className="tnum text-dim">{T.resolvedVersions(Object.entries(r.catalog_versions).map(([t, v]) => `${t} ${T.resolvedVersion(v)}`).join('、'))}</p>
          )}
          {r.note && <p className="break-words text-dim" data-change-resolved-note="">{T.resolvedNote(r.note)}</p>}
          {c.job && <JobLine job={c.job} onOpenJob={onOpenJob} />}
        </div>
      ) : <p className="text-xs text-faint">{T.status[c.status]}</p>}
    </Block>
  )
}

function CodesForm({ codes, busy, onChange, onCancel, onSubmit }: {
  codes: Record<string, string>; busy: boolean
  onChange: (codes: Record<string, string>) => void; onCancel: () => void; onSubmit: () => void
}) {
  const keys = Object.keys(codes)
  return (
    <form className="space-y-2 rounded-md border px-2.5 py-2" data-change-codes="" onSubmit={(e) => { e.preventDefault(); onSubmit() }}>
      <p className="text-xs font-medium">{T.codesTitle}</p>
      <p className="text-2xs leading-relaxed text-faint">{T.codesHint}</p>
      <div className="space-y-1.5">
        {keys.map((k) => (
          <label key={k} className="flex items-center gap-2 text-xs">
            <span className="mono w-28 shrink-0 truncate" title={k}>{k}</span>
            <input className="field min-w-0 flex-1 !py-0.5 !text-xs" value={codes[k]} aria-label={T.codesMeaning(k)} data-change-code-input={k}
                   onChange={(e) => onChange({ ...codes, [k]: e.target.value })} />
          </label>
        ))}
      </div>
      <div className="flex justify-end gap-1.5">
        <button type="button" className="btn btn-sm btn-ghost" onClick={onCancel}>{T.cancel}</button>
        <button type="submit" className="btn btn-sm btn-primary" disabled={busy || !keys.length} data-change-codes-submit="">
          {busy && <Spinner size={11} />}{T.codesSubmit}
        </button>
      </div>
    </form>
  )
}

function DismissForm({ note, busy, onChange, onCancel, onSubmit }: {
  note: string; busy: boolean; onChange: (note: string) => void; onCancel: () => void; onSubmit: () => void
}) {
  const empty = !note.trim()
  return (
    <form className="space-y-2 rounded-md border px-2.5 py-2" data-change-dismiss="" onSubmit={(e) => { e.preventDefault(); if (!empty) onSubmit() }}>
      <label className="label" htmlFor="change-note">{T.noteLabel}<span className="ml-0.5 text-[var(--err)]" aria-hidden>*</span></label>
      <textarea id="change-note" className="field min-h-[60px] w-full !text-xs" value={note} placeholder={T.notePlaceholder} maxLength={500}
                required aria-describedby="change-note-hint" onChange={(e) => onChange(e.target.value)} data-change-note="" autoFocus />
      <p id="change-note-hint" className="text-2xs text-faint">{T.noteRequired}</p>
      <div className="flex justify-end gap-1.5">
        <button type="button" className="btn btn-sm btn-ghost" onClick={onCancel}>{T.cancel}</button>
        <button type="submit" className="btn btn-sm" disabled={busy || empty} data-change-note-submit="">
          {busy && <Spinner size={11} />}{T.noteSubmit}
        </button>
      </div>
    </form>
  )
}

/** 「变化」视图里没打开哪一条时的右栏 */
export function ChangesOverview() {
  return (
    <div className="relative min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto max-w-xl space-y-3 p-6" data-changes-overview="">
        <h2 className="text-sm font-semibold">{T.overviewTitle}</h2>
        <p className="text-xs leading-relaxed text-dim">{T.overviewBody}</p>
      </div>
    </div>
  )
}
