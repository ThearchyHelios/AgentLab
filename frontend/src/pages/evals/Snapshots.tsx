import { useCallback, useEffect, useState } from 'react'
import { Camera, Sparkles, Square } from 'lucide-react'
import { api } from '../../api/client'
import { EmptyState, ErrorState, Field, Modal, Notice, Skeleton, Spinner, toast } from '../../components/ui'
import { errorMessage } from '../../lib/errors'
import { formatCost, formatDateTime, formatTokens } from '../../lib/format'
import { EVAL_TEXT } from '../../lib/termsEval'
import { useCatalog, useOnReconnect } from '../../store/catalog'
import type { EvalShadowBody, EvalShadowPlan, EvalSnapshot, EvalSuite } from '../../typesEval'
import { EvalStatusPill, ModelPick } from './parts'

const T = EVAL_TEXT.snapshots

/**
 * 目录快照与影子起草（细案 01 做法 6；评测-D5、D6）：评测按快照读数据目录，「当前目录 vs 关」「起草模型 A 的目录 vs B 的」这类
 * 对照都不改线上目录。这里列出所有快照（开评时自动拍的也在），可以拍一份当前目录，或者影子起草一份（先估算，再开始，能停）。
 * 有起草在跑时每 3 秒刷新一次进度。
 */
export function Snapshots() {
  const [rows, setRows] = useState<EvalSnapshot[] | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [dialog, setDialog] = useState<'take' | 'shadow' | null>(null)
  const [stopping, setStopping] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setRows((await api.evals.snapshots()).snapshots)
      setError(null)
    } catch (e) {
      setError(e)
    }
  }, [])
  useEffect(() => { void load() }, [load])
  useOnReconnect(() => { void load() })
  const live = !!rows?.some((s) => s.status === 'queued' || s.status === 'running')
  useEffect(() => {
    if (!live) return
    const t = setInterval(() => { if (!document.hidden) void load() }, 3000)
    return () => clearInterval(t)
  }, [live, load])

  const stop = async (s: EvalSnapshot) => {
    setStopping(s.id)
    try {
      await api.evals.stopSnapshot(s.id)
      toast.ok(T.stopped)
      await load()
    } catch (e) {
      toast.error(e)
    } finally {
      setStopping(null)
    }
  }

  return (
    <div className="mx-auto max-w-5xl space-y-3 p-4" data-eval-snapshots="">
      <div className="flex flex-wrap items-end gap-3">
        <div className="min-w-0 flex-1">
          <h2 className="text-sm font-semibold">{T.title}</h2>
          <p className="mt-0.5 text-xs leading-relaxed text-faint">{T.hint}</p>
        </div>
        <button type="button" className="btn btn-sm" onClick={() => setDialog('take')} data-eval-snapshot-take="">
          <Camera size={12} aria-hidden /> {T.take}
        </button>
        <button type="button" className="btn btn-sm" onClick={() => setDialog('shadow')} data-eval-snapshot-shadow="">
          <Sparkles size={12} aria-hidden /> {T.shadow}
        </button>
      </div>
      {error ? <ErrorState error={error} onRetry={() => void load()} /> : rows === null ? <Skeleton rows={3} height={56} gap={8} />
        : !rows.length ? <EmptyState icon={<Camera size={22} />} title={T.empty} body={T.emptyBody} offline={false} /> : (
          <ul className="space-y-2">
            {rows.map((s) => <SnapshotRow key={s.id} s={s} stopping={stopping === s.id} onStop={() => void stop(s)} />)}
          </ul>
        )}
      <TakeDialog open={dialog === 'take'} onClose={() => setDialog(null)} onDone={() => void load()} />
      <ShadowDialog open={dialog === 'shadow'} onClose={() => setDialog(null)} onDone={() => void load()} />
    </div>
  )
}

function SnapshotRow({ s, stopping, onStop }: { s: EvalSnapshot; stopping: boolean; onStop: () => void }) {
  const active = s.status === 'queued' || s.status === 'running'
  const failures = Object.keys(s.progress.failures ?? {})
  return (
    <li className="rounded-lg border bg-panel px-3 py-2.5 text-xs" data-eval-snapshot={s.id} data-eval-snapshot-status={s.status}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="chip">{T.origin[s.origin]}</span>
        <EvalStatusPill status={s.status} label={T.status[s.status]} />
        <span className="min-w-0 flex-1 truncate">{s.sources.map((x) => x.name).join('、')}</span>
        <span className="text-faint">{T.by(s.created_by, formatDateTime(s.created_at))}</span>
        {active && (
          <button type="button" className="btn btn-sm btn-danger" disabled={stopping} onClick={onStop} data-eval-snapshot-stop="">
            {stopping ? <Spinner size={11} /> : <Square size={10} aria-hidden fill="currentColor" />} {T.stop}
          </button>
        )}
      </div>
      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-2xs text-dim">
        {s.origin === 'shadow' && s.progress.tables_total != null && <span>{T.progress(s.progress.tables_done ?? 0, s.progress.tables_total)}</span>}
        {s.draft?.model_id && <span>{T.model_(s.draft.model_id)}</span>}
        {s.draft?.strip_llm && <span>{T.stripped}</span>}
        {!!s.usage?.calls && <span>{T.usage(s.usage.calls, s.usage.cost_usd != null ? formatCost(s.usage.cost_usd) : T.estimateUnpriced)}</span>}
        {(s.status === 'succeeded' || s.status === 'partial') && <span className="text-faint">{T.use}</span>}
      </div>
      {!!failures.length && <p className="mt-1 text-2xs text-[var(--warn)]">{T.failures(failures.length)}：{failures.slice(0, 5).join('、')}</p>}
      {s.progress.notice && <p className="mt-1 text-2xs text-[var(--warn)]">{s.progress.notice}</p>}
      {s.error && <p className="mt-1 text-2xs text-[var(--err)]">{s.error}</p>}
    </li>
  )
}

function SourcePick({ value, onChange }: { value: string[]; onChange: (v: string[]) => void }) {
  const datasources = useCatalog((s) => s.datasources)
  return (
    <Field label={T.sources} htmlFor="eval-snapshot-sources" hint={T.sourcesHint}>
      <select id="eval-snapshot-sources" className="field min-h-20" multiple value={value} data-eval-snapshot-sources=""
              onChange={(e) => onChange([...e.target.selectedOptions].map((o) => o.value))}>
        {datasources.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
      </select>
    </Field>
  )
}

function TakeDialog({ open, onClose, onDone }: { open: boolean; onClose: () => void; onDone: () => void }) {
  const [ids, setIds] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => { if (open) { setIds([]); setError(null) } }, [open])
  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.evals.takeSnapshot(ids)
      toast.ok(T.taken)
      onClose()
      onDone()
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <Modal open={open} onClose={onClose} title={T.takeTitle} width={520}
           footer={(
             <>
               <button type="button" className="btn" onClick={onClose}>{EVAL_TEXT.editor.cancel}</button>
               <button type="button" className="btn btn-primary" disabled={!ids.length || busy} onClick={() => void submit()} data-eval-snapshot-take-confirm="">
                 {busy && <Spinner size={11} />} {T.take}
               </button>
             </>
           )}>
      <div className="space-y-3">
        <p className="text-xs text-faint">{T.takeHint}</p>
        <SourcePick value={ids} onChange={setIds} />
        {error && <Notice tone="err">{error}</Notice>}
      </div>
    </Modal>
  )
}

function ShadowDialog({ open, onClose, onDone }: { open: boolean; onClose: () => void; onDone: () => void }) {
  const datasources = useCatalog((s) => s.datasources)
  const [ids, setIds] = useState<string[]>([])
  const [tables, setTables] = useState<Record<string, string>>({})
  const [suites, setSuites] = useState<EvalSuite[]>([])
  const [suiteId, setSuiteId] = useState('')
  const [model, setModel] = useState<{ provider: string | null; model: string | null }>({ provider: null, model: null })
  const [guidance, setGuidance] = useState('')
  const [note, setNote] = useState('')
  const [strip, setStrip] = useState(false)
  const [plan, setPlan] = useState<EvalShadowPlan | null>(null)
  const [busy, setBusy] = useState<'estimate' | 'start' | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    if (!open) return
    setIds([]); setTables({}); setSuiteId(''); setModel({ provider: null, model: null }); setGuidance(''); setNote('')
    setStrip(false); setPlan(null); setError(null)
    api.evals.suites().then((r) => setSuites(r.suites), () => setSuites([]))
  }, [open])
  const body = (): EvalShadowBody => ({
    sources: ids.map((id) => {
      const list = (tables[id] ?? '').split('\n').map((t) => t.trim()).filter(Boolean)
      return list.length ? { id, tables: list } : { id }
    }),
    suite_id: suiteId || null, provider: model.provider, model: model.model, guidance, note, strip_llm: strip,
  })
  const run = async (what: 'estimate' | 'start') => {
    setBusy(what)
    setError(null)
    try {
      if (what === 'estimate') setPlan(await api.evals.shadowEstimate(body()))
      else {
        await api.evals.startShadow(body())
        toast.ok(T.started)
        onClose()
        onDone()
      }
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      setBusy(null)
    }
  }
  const est = plan?.estimate
  return (
    <Modal open={open} onClose={onClose} title={T.shadowTitle} width={680}
           footer={(
             <>
               <button type="button" className="btn" onClick={onClose}>{EVAL_TEXT.editor.cancel}</button>
               <button type="button" className="btn" disabled={!ids.length || !!busy} onClick={() => void run('estimate')} data-eval-shadow-estimate="">
                 {busy === 'estimate' && <Spinner size={11} />} {busy === 'estimate' ? T.estimating : T.estimate}
               </button>
               <button type="button" className="btn btn-primary" disabled={!plan || !!busy} onClick={() => void run('start')} data-eval-shadow-start="">
                 {busy === 'start' && <Spinner size={11} />} {T.startShadow}
               </button>
             </>
           )}>
      <div className="space-y-3" data-eval-shadow="">
        <p className="text-xs text-faint">{T.shadowHint}</p>
        <SourcePick value={ids} onChange={(v) => { setIds(v); setPlan(null) }} />
        <Field label={T.fromSuite} htmlFor="eval-shadow-suite">
          <select id="eval-shadow-suite" className="field" value={suiteId} onChange={(e) => { setSuiteId(e.target.value); setPlan(null) }}>
            <option value="">{T.fromSuiteNone}</option>
            {suites.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
        </Field>
        {ids.map((id) => (
          <Field key={id} label={T.tables(datasources.find((d) => d.id === id)?.name ?? id)} htmlFor={`eval-shadow-tables-${id}`} hint={T.tablesHint}>
            <textarea id={`eval-shadow-tables-${id}`} className="field mono min-h-12 text-xs" value={tables[id] ?? ''}
                      onChange={(e) => { setTables({ ...tables, [id]: e.target.value }); setPlan(null) }} />
          </Field>
        ))}
        <Field label={T.model} htmlFor="eval-shadow-model">
          <ModelPick id="eval-shadow-model" value={model} defaultLabel={T.modelDefault} onChange={(v) => { setModel(v); setPlan(null) }} />
        </Field>
        <Field label={T.guidance} htmlFor="eval-shadow-guidance">
          <textarea id="eval-shadow-guidance" className="field min-h-12" value={guidance} maxLength={2000} onChange={(e) => setGuidance(e.target.value)} />
        </Field>
        <Field label={T.note} htmlFor="eval-shadow-note">
          <input id="eval-shadow-note" className="field" value={note} maxLength={2000} onChange={(e) => setNote(e.target.value)} />
        </Field>
        <label className="flex items-center gap-2 text-xs">
          <input type="checkbox" checked={strip} onChange={(e) => { setStrip(e.target.checked); setPlan(null) }} /> {T.strip}
        </label>
        {plan && est && (
          <Notice tone="info" attr={{ 'data-eval-shadow-plan': String(est.tables) }}>
            <div>{T.estimateText(est.tables, est.calls, formatTokens(est.input_tokens, { compact: true }),
              formatTokens(est.output_tokens, { compact: true }), est.cost_usd != null ? formatCost(est.cost_usd) : T.estimateUnpriced)}</div>
            <div className="text-faint">{est.note}</div>
            {plan.sources.filter((s) => s.unknown.length).map((s) => (
              <div key={s.id} className="text-[var(--warn)]">{T.unknownTables(s.name, s.unknown.join('、'))}</div>
            ))}
          </Notice>
        )}
        {error && <Notice tone="err">{error}</Notice>}
      </div>
    </Modal>
  )
}
