import { useEffect, useMemo, useRef, useState } from 'react'
import { Plus, X } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../../api/client'
import { Field, Modal, Notice, Spinner, toast, useRadioGroup } from '../../components/ui'
import { errorMessage } from '../../lib/errors'
import { formatCost, formatDateTime, shortId } from '../../lib/format'
import { EVAL_TEXT } from '../../lib/termsEval'
import type {
  EvalAnalysisTools, EvalCatalogMode, EvalEstimate, EvalRun, EvalSnapshot, EvalStartBody, EvalSuite, EvalVariant,
} from '../../typesEval'
import { ModelPick } from './parts'

const T = EVAL_TEXT.start
const MODES = ['single', 'ab', 'catalog', 'analysis_tools'] as const
type Mode = (typeof MODES)[number]
const CATALOG_MODES: EvalCatalogMode[] = ['live', 'reviewed', 'off', 'snapshot']
const ANALYSIS_TOOLS: EvalAnalysisTools[] = ['on', 'off']

/** 一臂的变体（表单里的样子）：全部键都在，交上去之前不改形状 */
export function blankVariant(base?: Partial<EvalVariant>): EvalVariant {
  return {
    assistant: { provider: base?.assistant?.provider ?? null, model: base?.assistant?.model ?? null },
    writer: { provider: base?.writer?.provider ?? null, model: base?.writer?.model ?? null },
    judge: base?.judge ?? false,
    catalog: { mode: base?.catalog?.mode ?? 'live', snapshot: base?.catalog?.snapshot ?? null },
    analysis: { tools: base?.analysis?.tools ?? 'on' },
    repeats: base?.repeats ?? 1,
    label: base?.label ?? null,
  }
}

/** 能当目录快照选的：做完了、写好了工件的那几份（状态由服务端给） */
export function usableSnapshots(list: EvalSnapshot[]): EvalSnapshot[] {
  return list.filter((s) => (s.status === 'succeeded' || s.status === 'partial') && !!s.artifact_id)
}

/** 一份快照在下拉里的叫法：来源、数据源、时间 */
export function snapshotLabel(s: EvalSnapshot): string {
  return `${EVAL_TEXT.snapshots.origin[s.origin]} · ${s.sources.map((x) => x.name).join('、')} · ${formatDateTime(s.created_at)}`
}

/**
 * 一臂变体的表单：助手、写作模型（缺省按设置），结论裁判，数据目录（当前 / 只用已核实的 / 关 / 快照），分析工具（开 / 关），
 * 重复次数，名字。题集设置里的「默认变体」也用它（定时评测按默认变体评）。预设对照里由预设定的那一项（目录、分析工具）不摆
 */
export function ArmFields({ idPrefix, value, onChange, snapshots, withLabel = false, labelIndex = 0, hideCatalog = false,
  hideAnalysis = false }: {
  idPrefix: string; value: EvalVariant; onChange: (v: EvalVariant) => void; snapshots: EvalSnapshot[]
  withLabel?: boolean; labelIndex?: number; hideCatalog?: boolean; hideAnalysis?: boolean
}) {
  const set = (patch: Partial<EvalVariant>) => onChange({ ...value, ...patch })
  const usable = usableSnapshots(snapshots)
  return (
    <div className="grid gap-3 sm:grid-cols-2" data-eval-arm={idPrefix}>
      {withLabel && (
        <Field label={T.armLabel} htmlFor={`${idPrefix}-label`} className="sm:col-span-2">
          <input id={`${idPrefix}-label`} className="field" value={value.label ?? ''} maxLength={40}
                 placeholder={T.armLabelPlaceholder(labelIndex)} onChange={(e) => set({ label: e.target.value || null })}
                 data-eval-arm-label="" />
        </Field>
      )}
      <Field label={T.assistant} htmlFor={`${idPrefix}-assistant`} hint={T.assistantHint}>
        <ModelPick id={`${idPrefix}-assistant`} value={value.assistant} defaultLabel={T.modelDefault}
                   onChange={(assistant) => set({ assistant })} />
      </Field>
      <Field label={T.writer} htmlFor={`${idPrefix}-writer`} hint={T.writerHint}>
        <ModelPick id={`${idPrefix}-writer`} value={value.writer} defaultLabel={T.modelDefault} onChange={(writer) => set({ writer })} />
      </Field>
      {!hideCatalog && (
        <Field label={T.catalog} htmlFor={`${idPrefix}-catalog`} hint={T.catalogHint}>
          <select id={`${idPrefix}-catalog`} className="field" value={value.catalog.mode} data-eval-catalog-mode=""
                  onChange={(e) => {
                    const mode = e.target.value as EvalCatalogMode
                    set({ catalog: { mode, snapshot: mode === 'snapshot' ? (value.catalog.snapshot ?? usable[0]?.id ?? null) : null } })
                  }}>
            {CATALOG_MODES.map((m) => <option key={m} value={m} disabled={m === 'snapshot' && !usable.length}>{T.catalogMode[m]}</option>)}
          </select>
        </Field>
      )}
      {!hideCatalog && value.catalog.mode === 'snapshot' && (
        <Field label={T.snapshot} htmlFor={`${idPrefix}-snapshot`}>
          <select id={`${idPrefix}-snapshot`} className="field" value={value.catalog.snapshot ?? ''} data-eval-catalog-snapshot=""
                  onChange={(e) => set({ catalog: { mode: 'snapshot', snapshot: e.target.value || null } })}>
            {!usable.length && <option value="">{T.snapshotNone}</option>}
            {usable.map((s) => <option key={s.id} value={s.id}>{snapshotLabel(s)}</option>)}
          </select>
        </Field>
      )}
      {!hideAnalysis && (
        <Field label={T.analysisTools} htmlFor={`${idPrefix}-analysis`} hint={T.analysisToolsHint}>
          <select id={`${idPrefix}-analysis`} className="field w-24" value={value.analysis.tools} data-eval-analysis-tools=""
                  onChange={(e) => set({ analysis: { tools: e.target.value as EvalAnalysisTools } })}>
            {ANALYSIS_TOOLS.map((m) => <option key={m} value={m}>{T.analysisToolsMode[m]}</option>)}
          </select>
        </Field>
      )}
      <Field label={T.repeats} htmlFor={`${idPrefix}-repeats`} hint={T.repeatsHint}>
        <select id={`${idPrefix}-repeats`} className="field w-24" value={value.repeats} data-eval-repeats=""
                onChange={(e) => set({ repeats: Number(e.target.value) })}>
          {[1, 2, 3, 4, 5].map((n) => <option key={n} value={n}>{n}</option>)}
        </select>
      </Field>
      <label className="flex items-start gap-2 text-xs sm:col-span-2">
        <input type="checkbox" className="mt-0.5" checked={value.judge} onChange={(e) => set({ judge: e.target.checked })}
               data-eval-judge="" />
        <span>{T.judge}<span className="block text-2xs leading-relaxed text-faint">{T.judgeHint}</span></span>
      </label>
    </div>
  )
}

/**
 * 发起评测：评一次 / A/B 对照（两到三臂） / 目录贡献对照、分析工具贡献对照（预设）。填好就向服务端要估算（几项、按上一次每题
 * 平均花费估的金额、生效的预算、同时评几题、今天评测已经花了多少）——估算和发起是同一份请求体，估出来的就是要发起的那一次。
 */
export function StartDialog({ open, onClose, suite, evaluations, onStarted }: {
  open: boolean; onClose: () => void; suite: EvalSuite; evaluations: EvalRun[]; onStarted: (run: EvalRun) => void
}) {
  const [mode, setMode] = useState<Mode>('single')
  const [arms, setArms] = useState<EvalVariant[]>(() => [blankVariant(suite.default_variant)])
  const [reviewedArm, setReviewedArm] = useState(false)
  const [baseline, setBaseline] = useState('')
  const [snapshots, setSnapshots] = useState<EvalSnapshot[]>([])
  const [estimate, setEstimate] = useState<{ data?: EvalEstimate; error?: string; loading?: boolean }>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const radio = useRadioGroup(MODES, mode, (m) => pickMode(m))

  useEffect(() => {
    if (!open) return
    setMode('single')
    setArms([blankVariant(suite.default_variant)])
    setReviewedArm(false)
    setBaseline('')
    setError(null)
    api.evals.snapshots().then((r) => setSnapshots(r.snapshots), () => setSnapshots([]))
  }, [open, suite.id])

  const pickMode = (m: Mode) => {
    setMode(m)
    setArms((cur) => {
      const first = cur[0] ?? blankVariant(suite.default_variant)
      if (m === 'ab') {
        const repeats = Math.max(first.repeats, 3)
        return cur.length >= 2 ? cur : [{ ...first, repeats, label: first.label || 'A' }, { ...first, repeats, label: 'B' }]
      }
      return [m === 'catalog' || m === 'analysis_tools' ? { ...first, repeats: Math.max(first.repeats, 3) } : first]
    })
  }

  const body: EvalStartBody = useMemo(() => {
    const base = baseline ? { baseline_id: baseline } : {}
    if (mode === 'ab') return { arms, ...base }
    const v = arms[0] ?? blankVariant()
    if (mode === 'catalog') {
      const { catalog: _c, label: _l, ...rest } = v
      return { preset: reviewedArm ? 'catalog_reviewed' : 'catalog', variant: rest, ...base }
    }
    if (mode === 'analysis_tools') {
      const { analysis: _a, label: _l, ...rest } = v
      return { preset: 'analysis_tools', variant: rest, ...base }
    }
    return { variant: v, ...base }
  }, [mode, arms, reviewedArm, baseline])
  const key = JSON.stringify(body)

  // 估算：停手 400ms 再问，新的一次作废上一次
  const ask = useRef(0)
  useEffect(() => {
    if (!open) return
    const n = ++ask.current
    setEstimate((e) => ({ data: e.data, loading: true }))
    const t = setTimeout(() => {
      api.evals.estimate(suite.id, body)
        .then((data) => { if (n === ask.current) setEstimate({ data }) })
        .catch((e) => { if (n === ask.current) setEstimate({ error: errorMessage(e) }) })
    }, 400)
    return () => clearTimeout(t)
  }, [open, key, suite.id])

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      const r = await api.evals.start(suite.id, body)
      toast.ok(T.started)
      onClose()
      if (r.evaluations[0]) onStarted(r.evaluations[0])
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      setBusy(false)
    }
  }

  const finished = evaluations.filter((r) => r.status === 'succeeded' || r.status === 'partial')
  const est = estimate.data
  return (
    <Modal open={open} onClose={onClose} title={T.title} width={720}
           footer={(
             <>
               <button type="button" className="btn" onClick={onClose}>{EVAL_TEXT.editor.cancel}</button>
               <button type="button" className="btn btn-primary" disabled={busy || !!estimate.error} onClick={() => void submit()}
                       data-eval-start-submit="">
                 {busy && <Spinner size={11} />} {busy ? T.starting : T.submit}
               </button>
             </>
           )}>
      <div className="space-y-4" data-eval-start="">
        <div>
          <div className="label" id="eval-start-mode">{T.mode}</div>
          <div role="radiogroup" aria-labelledby="eval-start-mode" className="flex flex-wrap gap-1">
            {MODES.map((m) => (
              <button key={m} type="button" {...radio(m)} onClick={() => pickMode(m)} data-eval-start-mode={m}
                      className={clsx('rounded-md border px-2.5 py-1 text-xs transition-colors',
                        mode === m ? 'border-[var(--accent)] bg-accent-soft text-fg' : 'text-dim hover:bg-hover')}>
                {T.modes[m]}
              </button>
            ))}
          </div>
          <p className="mt-1 text-2xs leading-relaxed text-faint">{T.modeHint[mode]}</p>
        </div>

        {mode !== 'ab' && (
          <ArmFields idPrefix="eval-arm-0" value={arms[0] ?? blankVariant()} snapshots={snapshots} hideCatalog={mode === 'catalog'}
                     hideAnalysis={mode === 'analysis_tools'} onChange={(v) => setArms([v])} />
        )}
        {mode === 'catalog' && (
          <label className="flex items-center gap-2 text-xs">
            <input type="checkbox" checked={reviewedArm} onChange={(e) => setReviewedArm(e.target.checked)} data-eval-reviewed-arm="" />
            {T.reviewedArm}
          </label>
        )}
        {mode === 'ab' && (
          <div className="space-y-3">
            {arms.map((a, i) => (
              <section key={i} className="space-y-2 rounded-lg border p-3" aria-label={T.arm(a.label || 'ABC'[i])}>
                <div className="flex items-center justify-between">
                  <h3 className="text-xs font-semibold">{T.arm(a.label || 'ABC'[i])}</h3>
                  {arms.length > 2 && (
                    <button type="button" className="btn btn-ghost btn-sm" onClick={() => setArms(arms.filter((_, j) => j !== i))}
                            aria-label={T.removeArm}>
                      <X size={11} aria-hidden /> {T.removeArm}
                    </button>
                  )}
                </div>
                <ArmFields idPrefix={`eval-arm-${i}`} value={a} snapshots={snapshots} withLabel labelIndex={i}
                           onChange={(v) => setArms(arms.map((x, j) => (j === i ? v : x)))} />
              </section>
            ))}
            {arms.length < 3 && (
              <button type="button" className="btn btn-sm" onClick={() => setArms([...arms, { ...arms[arms.length - 1], label: 'C' }])}
                      data-eval-add-arm="">
                <Plus size={11} aria-hidden /> {T.addArm}
              </button>
            )}
          </div>
        )}

        <Field label={T.baseline} htmlFor="eval-start-baseline">
          <select id="eval-start-baseline" className="field" value={baseline} onChange={(e) => setBaseline(e.target.value)}>
            <option value="">{T.baselineAuto}</option>
            {finished.map((r) => (
              <option key={r.id} value={r.id}>
                {EVAL_TEXT.compare.baseText(shortId(r.id), formatDateTime(r.created_at))}{r.arm ? ` · ${r.arm}` : ''}
                {r.pinned ? ` · ${EVAL_TEXT.run.pinned}` : ''}
              </option>
            ))}
          </select>
        </Field>

        <section className="space-y-1.5 rounded-lg border bg-panel p-3 text-xs" aria-label={T.estimate} data-eval-estimate=""
                 aria-busy={estimate.loading || undefined}>
          <h3 className="flex items-center gap-1.5 text-xs font-semibold">
            {T.estimate}{estimate.loading && <><Spinner size={10} /><span className="font-normal text-faint">{T.estimating}</span></>}
          </h3>
          {estimate.error && <Notice tone="err" attr={{ 'data-eval-estimate-error': '' }}>{estimate.error}</Notice>}
          {est && (
            <>
              <p data-eval-estimate-items={est.estimate.items}>{T.estimateText(est.estimate.items, est.cases, est.repeats, est.arms.length)}</p>
              <p data-eval-estimate-cost="">
                {est.estimate.total_usd == null ? T.costNone
                  : (est.estimate.at_least ? T.costAtLeast : T.cost)(formatCost(est.estimate.total_usd), formatCost(est.estimate.per_item_usd))}
                <span className="text-faint"> · {est.estimate.note}</span>
              </p>
              {est.arms.map((a, i) => (
                <p key={i} className="text-dim" data-eval-estimate-arm={a.label ?? ''}>
                  {a.label ? `${T.arm(a.label)}：` : ''}{a.text}
                </p>
              ))}
              <p className="text-dim">
                {T.budget}：{EVAL_TEXT.suite.budgetText(est.budget.max_parallel, est.budget.per_case_timeout_s,
                  est.budget.per_run_max_usd, est.budget.daily_max_usd)} · {T.parallel(est.parallel)}
              </p>
              <p className="text-dim" data-eval-estimate-spend="">
                {T.spendToday(formatCost(est.spend.usd ?? 0), est.spend.daily_max_usd != null ? formatCost(est.spend.daily_max_usd) : null)}
                {est.spend.unpriced_calls ? T.spendUnpriced(est.spend.unpriced_calls) : ''}
              </p>
            </>
          )}
        </section>
        {error && <Notice tone="err" attr={{ 'data-eval-start-error': '' }}>{error}</Notice>}
      </div>
    </Modal>
  )
}
