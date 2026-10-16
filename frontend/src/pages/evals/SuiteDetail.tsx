import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { Archive, ArrowLeft, Download, Gauge, Pencil, Play, Plus, Settings2, Trash2 } from 'lucide-react'
import clsx from 'clsx'
import { ApiError, api } from '../../api/client'
import {
  EmptyState, ErrorState, Field, IconButton, Modal, Notice, PageHeader, Skeleton, Spinner, Tabs, confirmDialog, toast,
} from '../../components/ui'
import { errorMessage } from '../../lib/errors'
import { formatDateTime, formatTime } from '../../lib/format'
import { timezoneOptions } from '../../lib/freq'
import { leavePass } from '../../lib/leave'
import { EVAL_TEXT, pct, rateText } from '../../lib/termsEval'
import { useOnReconnect } from '../../store/catalog'
import type {
  EvalCase, EvalRun, EvalSnapshot, EvalSuiteDetail, EvalTrial, EvalUpgradePolicy, EvalUpgradeState, EvalVariant,
} from '../../typesEval'
import { CaseEditor, TrialBox } from './CaseEditor'
import { ArmFields, StartDialog, blankVariant } from './StartDialog'
import { SuiteSchedules } from './SuiteList'
import { EvalStatusPill, expectationText, saveFile } from './parts'

const T = EVAL_TEXT
type TabKey = 'cases' | 'runs'

/**
 * 一个题集（/evals/suites/:id）：页头是名字和发起评测、导出、设置、归档、删除；下面是口径升版待处置的提示、题目表（加题、
 * 试算、编辑、启用、删除、处置升版）和评测记录。?add=1&question=… 打开加题并带上问法（「口径命中与重复度」页签的「加入题集」）；
 * ?tab=runs 打开评测记录。
 */
export function SuiteDetail({ id }: { id: string }) {
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const tab: TabKey = params.get('tab') === 'runs' ? 'runs' : 'cases'
  const [suite, setSuite] = useState<EvalSuiteDetail | null>(null)
  const [runs, setRuns] = useState<EvalRun[] | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [editing, setEditing] = useState<EvalCase | null | 'new'>(null)
  const [starting, setStarting] = useState(false)
  const [settings, setSettings] = useState(false)
  const [disposing, setDisposing] = useState<EvalCase | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [trials, setTrials] = useState<Record<string, EvalTrial>>({})
  const addQuestion = params.get('add') ? params.get('question') ?? '' : null
  /** ?case=题目 id：记录页里保存题目时的试算链到这里，定位到那道题 */
  const focusCase = params.get('case')
  const seq = useRef(0)

  const load = useCallback(async () => {
    const n = ++seq.current
    try {
      const [s, r] = await Promise.all([api.evals.suite(id), api.evals.evaluations(id)])
      if (n !== seq.current) return
      setSuite(s)
      setRuns(r.evaluations)
      setError(null)
    } catch (e) {
      if (n !== seq.current) return
      setError(e)
    }
  }, [id])
  useEffect(() => { void load() }, [load])
  useOnReconnect(() => { void load() })
  useEffect(() => {
    if (addQuestion != null && suite) setEditing('new')
  }, [addQuestion != null, !!suite])
  useEffect(() => {
    if (!focusCase || !suite) return
    requestAnimationFrame(() => document.querySelector(`[data-eval-case="${CSS.escape(focusCase)}"]`)?.scrollIntoView({ block: 'center' }))
  }, [focusCase, !!suite])
  const running = !!runs?.some((r) => r.running)
  useEffect(() => {
    if (!running) return
    const t = setInterval(() => { if (!document.hidden) void load() }, 5000)
    return () => clearInterval(t)
  }, [running, load])

  const pickTab = (k: string) => setParams((p) => {
    const next = new URLSearchParams(p)
    if (k === 'runs') next.set('tab', 'runs')
    else next.delete('tab')
    return next
  }, { replace: true })
  const closeEditor = () => {
    setEditing(null)
    if (addQuestion != null) setParams((p) => { const n = new URLSearchParams(p); n.delete('add'); n.delete('question'); return n }, { replace: true })
  }

  const act = async (what: string, fn: () => Promise<void>) => {
    if (busy) return
    setBusy(what)
    try { await fn() } catch (e) { toast.error(e) } finally { setBusy(null) }
  }
  const exportFile = () => act('export', async () => {
    if (!suite) return
    const { blob, name } = await api.evals.exportFile(suite.id)
    saveFile(name ?? `${suite.name}.json`, blob)
    toast.ok(T.suite.exported(suite.name))
  })
  const toggleArchive = () => act('archive', async () => {
    if (!suite) return
    const next = await api.evals.updateSuite(suite.id, { archived: !suite.archived, version: suite.version })
    setSuite(next)
    toast.ok(next.archived ? T.suite.archivedOk(next.name) : T.suite.unarchivedOk(next.name))
  })
  const remove = () => act('delete', async () => {
    if (!suite) return
    if (!(await confirmDialog({ title: T.suite.deleteTitle(suite.name), consequences: T.suite.deleteConsequences,
      confirmLabel: T.suite.delete, danger: true }))) return
    await api.evals.removeSuite(suite.id)
    toast.ok(T.suite.deleted(suite.name))
    navigate('/evals', leavePass({ replace: true }))
  })
  const toggleCase = (c: EvalCase, enabled: boolean) => act(`toggle:${c.id}`, async () => {
    if (!suite) return
    await api.evals.updateCase(suite.id, c.id, { enabled, version: c.version })
    await load()
  })
  const trialCase = (c: EvalCase) => act(`trial:${c.id}`, async () => {
    if (!suite) return
    const r = await api.evals.trialCase(suite.id, c.id)
    setTrials((t) => ({ ...t, [c.id]: r.trial }))
    await load()
  })
  const removeCase = (c: EvalCase) => act(`delete:${c.id}`, async () => {
    if (!suite) return
    if (!(await confirmDialog({ title: T.caseRow.deleteTitle(c.seq), consequences: T.caseRow.deleteConsequences,
      confirmLabel: T.caseRow.delete, danger: true }))) return
    await api.evals.removeCase(suite.id, c.id)
    toast.ok(T.caseRow.deleted(c.seq))
    await load()
  })

  const header = (
    <div className="flex shrink-0 flex-wrap items-center gap-1">
      {suite && (
        <>
          <button type="button" className="btn btn-primary btn-sm" disabled={suite.archived || !suite.counts?.enabled}
                  onClick={() => setStarting(true)} data-eval-suite-start="">
            <Play size={11} aria-hidden /> {T.suite.start}
          </button>
          <button type="button" className="btn btn-sm" onClick={() => void exportFile()} disabled={!!busy} data-eval-suite-export="">
            {busy === 'export' ? <Spinner size={11} /> : <Download size={11} aria-hidden />} <span className="hidden md:inline">{T.suite.export}</span>
          </button>
          <IconButton label={T.suite.settings} onClick={() => setSettings(true)} icon={<Settings2 size={12} />} data-eval-suite-settings="" />
          <IconButton label={suite.archived ? T.suite.unarchive : T.suite.archive} onClick={() => void toggleArchive()} disabled={!!busy}
                      icon={<Archive size={12} />} data-eval-suite-archive="" />
          <IconButton label={T.suite.delete} onClick={() => void remove()} disabled={!!busy} icon={<Trash2 size={12} />}
                      className="text-faint hover:bg-st-failed/10 hover:text-[var(--err)]" data-eval-suite-delete="" />
        </>
      )}
      <Link to="/evals" className="btn btn-sm btn-ghost" aria-label={T.back} title={T.back}>
        <ArrowLeft size={12} aria-hidden /> <span className="hidden md:inline">{T.back}</span>
      </Link>
    </div>
  )

  let content
  if (!suite) {
    const missing = error instanceof ApiError && error.status === 404
    content = missing
      ? <EmptyState icon={<Gauge size={22} />} title={errorMessage(error)} offline={false} action={<Link to="/evals" className="btn btn-sm">{T.back}</Link>} />
      : error ? <ErrorState error={error} onRetry={() => void load()} />
        : <div className="mx-auto max-w-5xl p-4"><Skeleton rows={6} height={40} gap={10} /></div>
  }

  return (
    <div className="flex h-full flex-col" data-evals-page="suite" data-eval-suite-id={id}>
      <PageHeader icon={<Gauge size={13} />} title={suite?.name ?? T.title} subtitle={suite?.description || T.hint} actions={header} />
      {content ?? (suite && (
        <>
          <div className="shrink-0 space-y-1 border-b bg-panel px-4 py-2 text-2xs text-dim" data-eval-suite-summary="">
            <div className="flex flex-wrap gap-x-4 gap-y-1">
              <span>{T.list.cases(suite.counts?.cases ?? 0, suite.counts?.enabled ?? 0)}</span>
              <span>{T.suite.budget}：{T.suite.budgetText(suite.effective_budget.max_parallel, suite.effective_budget.per_case_timeout_s,
                suite.effective_budget.per_run_max_usd, suite.effective_budget.daily_max_usd)}</span>
              <span>{T.suite.timezone(suite.timezone)}</span>
            </div>
            <SuiteSchedules s={suite} />
          </div>
          {suite.archived && <Notice tone="info" className="mx-4 mt-3">{T.suite.archivedNote}</Notice>}
          {suite.pending_upgrades > 0 && (
            <Notice tone="warn" className="mx-4 mt-3" attr={{ 'data-eval-upgrade-banner': String(suite.pending_upgrades) }}>
              <div>{T.suite.upgradeBanner(suite.pending_upgrades)}</div>
              <div className="text-faint">{T.suite.upgradeHint}</div>
            </Notice>
          )}
          <Tabs tabs={[{ key: 'cases', label: T.suite.tabs.cases, badge: undefined }, { key: 'runs', label: T.suite.tabs.runs }]}
                active={tab} onChange={pickTab} label={suite.name} idPrefix="eval-suite" />
          <div className="min-h-0 flex-1 overflow-y-auto">
            <div role="tabpanel" id="eval-suite-panel-cases" aria-labelledby="eval-suite-tab-cases" hidden={tab !== 'cases'}
                 className="mx-auto max-w-6xl space-y-3 p-4">
              <div className="flex justify-end">
                <button type="button" className="btn btn-sm" onClick={() => setEditing('new')} disabled={suite.archived} data-eval-add-case="">
                  <Plus size={11} aria-hidden /> {T.suite.addCase}
                </button>
              </div>
              {!suite.cases.length ? (
                <div data-eval-cases-empty="">
                  <EmptyState icon={<Gauge size={22} />} title={T.suite.casesEmpty} body={T.suite.casesEmptyBody} offline={false} />
                </div>
              ) : (
                <ul className="space-y-2" data-eval-cases="">
                  {suite.cases.map((c) => (
                    <CaseRow key={c.id} c={c} busy={busy} trial={trials[c.id]} archivedSuite={suite.archived} focused={c.id === focusCase}
                             onEdit={() => setEditing(c)} onToggle={(v) => void toggleCase(c, v)} onTrial={() => void trialCase(c)}
                             onDelete={() => void removeCase(c)} onDispose={() => setDisposing(c)} />
                  ))}
                </ul>
              )}
            </div>
            <div role="tabpanel" id="eval-suite-panel-runs" aria-labelledby="eval-suite-tab-runs" hidden={tab !== 'runs'}
                 className="mx-auto max-w-6xl p-4">
              <RunList runs={runs ?? []} />
            </div>
          </div>
          <CaseEditor open={editing != null} onClose={closeEditor} suite={suite} editing={editing === 'new' ? null : editing}
                      initialQuestion={addQuestion ?? undefined} onSaved={() => void load()} />
          <StartDialog open={starting} onClose={() => setStarting(false)} suite={suite} evaluations={runs ?? []}
                       onStarted={(r) => navigate(`/evals/runs/${encodeURIComponent(r.id)}`)} />
          <SuiteSettings open={settings} onClose={() => setSettings(false)} suite={suite} onSaved={(s) => setSuite(s)} />
          {disposing && (
            <UpgradeDialog suiteId={suite.id} c={disposing} onClose={() => setDisposing(null)} onDone={() => void load()} />
          )}
        </>
      ))}
    </div>
  )
}

function goldText(c: EvalCase): string {
  const g = c.gold
  if (!g) return T.gold.none
  if (g.kind === 'caliber') return T.gold.caliber(g.workflow_id ?? '', g.version ?? 0)
  if (g.kind === 'from_run') return T.gold.fromRun(c.gold_source?.name ?? null)
  return T.gold.sql(c.gold_source?.name ?? null)
}

function CaseRow({ c, busy, trial, archivedSuite, focused, onEdit, onToggle, onTrial, onDelete, onDispose }: {
  c: EvalCase; busy: string | null; trial?: EvalTrial; archivedSuite: boolean; focused: boolean
  onEdit: () => void; onToggle: (v: boolean) => void; onTrial: () => void; onDelete: () => void; onDispose: () => void
}) {
  const last = c.trial
  const pending = !!c.upgrade?.pending
  return (
    <li className={clsx('rounded-lg border bg-panel px-3 py-2.5', (!c.enabled || c.archived) && 'opacity-75',
      focused && 'border-[var(--accent)]')} data-eval-case={c.id} data-eval-case-focused={focused ? '' : undefined}
        data-eval-case-seq={c.seq} data-eval-case-enabled={String(c.enabled)}>
      <div className="flex flex-wrap items-start gap-x-3 gap-y-2">
        <div className="min-w-0 flex-1 basis-72">
          <div className="flex min-w-0 flex-wrap items-center gap-1.5">
            <span className="tnum shrink-0 text-xs text-faint">{T.caseRow.seq(c.seq)}</span>
            <span className="chip shrink-0">{T.caseRow.kind[c.kind]}</span>
            {c.archived && <span className="chip shrink-0">{T.caseRow.archived}</span>}
            {!c.enabled && !c.archived && <span className="chip shrink-0">{T.caseRow.disabled}</span>}
            {c.tags.map((t) => <span key={t} className="chip shrink-0 text-faint">{t}</span>)}
          </div>
          <div className="mt-1 break-words text-sm" data-eval-case-text="">{c.question || '—'}</div>
          <div className="mt-1 text-2xs text-dim">
            <span title={T.caseRow.goldSource}>{goldText(c)}</span>
            {c.datasources.length > 0 && <span className="text-faint"> · {T.caseRow.datasources}：{c.datasources.map((d) => d.name ?? d.id).join('、')}</span>}
          </div>
          <ul className="mt-1 space-y-0.5 text-2xs text-dim" aria-label={T.caseRow.expectations}>
            {c.expectations.map((e, i) => <li key={e.id ?? i}>· {T.expectation.kind[e.kind]}：{expectationText(e)}</li>)}
          </ul>
          {pending && (
            <div className="mt-1.5 flex flex-wrap items-center gap-2 text-2xs text-[var(--warn)]" data-eval-case-upgrade="">
              <span>{c.upgrade?.message}</span>
              <button type="button" className="btn btn-sm" onClick={onDispose} data-eval-case-dispose="">{T.caseRow.dispose}</button>
            </div>
          )}
          <div className="mt-1 text-2xs text-faint" data-eval-case-last-trial={last?.status ?? 'none'}>
            {T.caseRow.lastTrial}：{last ? `${T.trialStatus[last.status]} · ${T.caseRow.trialAt(formatTime(last.at ?? last.as_of), last.by ?? null)}${last.reason ? ` · ${last.reason}` : ''}` : T.caseRow.noTrial}
          </div>
          {trial && <div className="mt-1.5"><TrialBox trial={trial} /></div>}
        </div>
        {!c.archived && (
          <div className="flex shrink-0 items-center gap-1">
            <label className="flex items-center gap-1 text-2xs text-dim">
              <input type="checkbox" checked={c.enabled} disabled={!!busy || archivedSuite} onChange={(e) => onToggle(e.target.checked)}
                     aria-label={T.caseRow.enable(c.seq)} data-eval-case-toggle="" />
              {T.editor.enabled}
            </label>
            <button type="button" className="btn btn-sm" onClick={onTrial} disabled={!!busy} title={T.caseRow.trialTitle} data-eval-case-retrial="">
              {busy === `trial:${c.id}` ? <Spinner size={11} /> : null} {T.caseRow.trial}
            </button>
            <IconButton label={T.caseRow.edit} onClick={onEdit} icon={<Pencil size={12} />} disabled={archivedSuite} data-eval-case-edit="" />
            <IconButton label={T.caseRow.delete} onClick={onDelete} icon={<Trash2 size={12} />} disabled={!!busy}
                        className="text-faint hover:bg-st-failed/10 hover:text-[var(--err)]" data-eval-case-delete="" />
          </div>
        )}
      </div>
    </li>
  )
}

/** 历次评测（新的在前）：状态、怎么发起的、变体、准确率；点了去评测详情 */
function RunList({ runs }: { runs: EvalRun[] }) {
  if (!runs.length) return <p className="py-6 text-center text-xs text-faint" data-eval-runs-empty="">{T.suite.runsEmpty}</p>
  return (
    <ul className="space-y-1.5" aria-label={T.suite.runsLabel} data-eval-runs="">
      {runs.map((r) => (
        <li key={r.id}>
          <Link to={`/evals/runs/${encodeURIComponent(r.id)}`} data-eval-run-row={r.id}
                className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border bg-panel px-3 py-2 text-xs hover:border-[var(--border-strong)]">
            <span className="mono text-faint">#{r.id.slice(0, 6)}</span>
            <EvalStatusPill status={r.status} />
            {r.arm && <span className="chip">{T.run.arm(r.arm)}</span>}
            {r.pinned && <span className="chip">{T.run.pinned}</span>}
            <span className="text-dim">{T.trigger[r.trigger]}</span>
            <span className="tnum font-medium" title={rateText(r.summary?.accuracy)}>{r.summary?.accuracy?.n ? pct(r.summary.accuracy.rate) : ''}</span>
            <span className="min-w-0 flex-1 truncate text-faint" title={r.variant_text}>{r.variant_text}</span>
            <time className="text-faint">{formatDateTime(r.created_at)}</time>
          </Link>
        </li>
      ))}
    </ul>
  )
}

/**
 * 题集设置：名称、说明、时区（题目里的时间变量按它算）、默认变体（定时评测按它评）、预算（只填要改的项，留空用设置里的缺省）
 */
function SuiteSettings({ open, onClose, suite, onSaved }: {
  open: boolean; onClose: () => void; suite: EvalSuiteDetail; onSaved: (s: EvalSuiteDetail) => void
}) {
  const [name, setName] = useState(suite.name)
  const [description, setDescription] = useState(suite.description)
  const [timezone, setTimezone] = useState(suite.timezone)
  const [variant, setVariant] = useState<EvalVariant>(() => blankVariant(suite.default_variant))
  const [budget, setBudget] = useState<Record<string, string>>({})
  const [snapshots, setSnapshots] = useState<EvalSnapshot[]>([])
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<{ message: string; field?: string } | null>(null)
  useEffect(() => {
    if (!open) return
    setName(suite.name)
    setDescription(suite.description)
    setTimezone(suite.timezone)
    setVariant(blankVariant(suite.default_variant))
    setBudget(Object.fromEntries(Object.entries(suite.budget ?? {}).map(([k, v]) => [k, String(v)])))
    setError(null)
    api.evals.snapshots().then((r) => setSnapshots(r.snapshots), () => setSnapshots([]))
  }, [open, suite.id, suite.version])
  const keys = ['max_parallel', 'per_case_timeout_s', 'per_run_max_usd', 'daily_max_usd'] as const
  const save = async () => {
    setSaving(true)
    setError(null)
    try {
      const out = await api.evals.updateSuite(suite.id, {
        name, description, timezone, default_variant: variant, version: suite.version,
        budget: Object.fromEntries(keys.filter((k) => (budget[k] ?? '').trim() !== '').map((k) => [k, Number(budget[k])])),
      })
      onSaved(out)
      toast.ok(EVAL_TEXT.settings.saved)
      onClose()
    } catch (e) {
      setError({ message: errorMessage(e), field: e instanceof ApiError ? e.field : undefined })
    } finally {
      setSaving(false)
    }
  }
  const zones = timezoneOptions(timezone)
  const labels = EVAL_TEXT.settings
  return (
    <Modal open={open} onClose={onClose} title={T.suite.settings} width={720}
           footer={(
             <>
               <button type="button" className="btn" onClick={onClose}>{T.editor.cancel}</button>
               <button type="button" className="btn btn-primary" disabled={saving || !name.trim()} onClick={() => void save()}
                       data-eval-suite-settings-save="">
                 {saving && <Spinner size={11} />} {saving ? T.editor.saving : T.editor.save}
               </button>
             </>
           )}>
      <div className="space-y-4" data-eval-suite-settings-form="">
        <Field label={T.list.createName} htmlFor="eval-suite-name" required error={error?.field === 'name' ? error.message : undefined}>
          <input id="eval-suite-name" className="field" value={name} maxLength={200} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label={EVAL_TEXT.suiteSettings.description} htmlFor="eval-suite-description">
          <textarea id="eval-suite-description" className="field min-h-12" value={description} maxLength={2000}
                    onChange={(e) => setDescription(e.target.value)} />
        </Field>
        <Field label={EVAL_TEXT.suiteSettings.timezone} htmlFor="eval-suite-timezone" hint={EVAL_TEXT.suiteSettings.timezoneHint}
               error={error?.field === 'timezone' ? error.message : undefined}>
          <select id="eval-suite-timezone" className="field" value={timezone} onChange={(e) => setTimezone(e.target.value)}>
            {zones.map((z) => <option key={z.tz} value={z.tz}>{z.label}</option>)}
          </select>
        </Field>
        <section className="space-y-2">
          <h3 className="text-xs font-semibold">{EVAL_TEXT.suiteSettings.variant}</h3>
          <p className="text-2xs text-faint">{EVAL_TEXT.suiteSettings.variantHint}</p>
          <ArmFields idPrefix="eval-suite-variant" value={variant} onChange={setVariant} snapshots={snapshots} />
        </section>
        <section className="space-y-2">
          <h3 className="text-xs font-semibold">{T.suite.budget}</h3>
          <p className="text-2xs text-faint">{EVAL_TEXT.suiteSettings.budgetHint}</p>
          <div className="grid gap-3 sm:grid-cols-2">
            {keys.map((k) => (
              <Field key={k} label={labels[k]} htmlFor={`eval-suite-budget-${k}`}
                     error={error?.field === `budget.${k}` ? error.message : undefined}>
                <input id={`eval-suite-budget-${k}`} className="field" inputMode="decimal" value={budget[k] ?? ''}
                       placeholder={String(suite.effective_budget[k])} onChange={(e) => setBudget({ ...budget, [k]: e.target.value })} />
              </Field>
            ))}
          </div>
        </section>
        {error && !['name', 'timezone'].includes(error.field ?? '') && !(error.field ?? '').startsWith('budget.') && (
          <Notice tone="err">{error.message}</Notice>
        )}
      </div>
    </Modal>
  )
}

/** 处置口径升版：三选一（叫法由服务端给），没有缺省；处置后的题同样要试算通过才生效 */
function UpgradeDialog({ suiteId, c, onClose, onDone }: { suiteId: string; c: EvalCase; onClose: () => void; onDone: () => void }) {
  const [state, setState] = useState<{ upgrade: EvalUpgradeState | null; choices: Record<EvalUpgradePolicy, string> } | null>(null)
  const [policy, setPolicy] = useState<EvalUpgradePolicy | ''>('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    api.evals.upgrade(suiteId, c.id).then(setState, (e) => setError(errorMessage(e)))
  }, [suiteId, c.id])
  const submit = async () => {
    if (!policy) return
    setBusy(true)
    setError(null)
    try {
      const r = await api.evals.disposeUpgrade(suiteId, c.id, { policy, version: c.version })
      toast.ok(r.new_case ? T.upgrade.newCase(r.new_case.seq) : T.upgrade.done(c.seq))
      onDone()
      onClose()
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      setBusy(false)
    }
  }
  const up = state?.upgrade
  return (
    <Modal open onClose={onClose} title={T.upgrade.title(c.seq)} width={560}
           footer={(
             <>
               <button type="button" className="btn" onClick={onClose}>{T.editor.cancel}</button>
               <button type="button" className="btn btn-primary" disabled={!policy || busy} onClick={() => void submit()} data-eval-upgrade-confirm="">
                 {busy && <Spinner size={11} />} {T.upgrade.confirm}
               </button>
             </>
           )}>
      <div className="space-y-3 text-xs" data-eval-upgrade="">
        {up && <p>{up.message}<span className="text-faint"> · {T.upgrade.current(up.pinned, up.latest)}</span></p>}
        <div role="radiogroup" aria-label={T.upgrade.policyLabel} className="space-y-1.5">
          {state && (Object.keys(state.choices) as EvalUpgradePolicy[]).map((k) => (
            <label key={k} className="flex items-center gap-2">
              <input type="radio" name="eval-upgrade-policy" checked={policy === k} onChange={() => setPolicy(k)} data-eval-upgrade-policy={k} />
              {state.choices[k]}
            </label>
          ))}
        </div>
        {!policy && <p className="text-2xs text-faint">{T.upgrade.noDefault}</p>}
        {error && <Notice tone="err">{error}</Notice>}
      </div>
    </Modal>
  )
}
