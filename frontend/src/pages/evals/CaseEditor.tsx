import { useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import clsx from 'clsx'
import { ApiError, api } from '../../api/client'
import { Field, Modal, Notice, Spinner, toast, useRadioGroup } from '../../components/ui'
import { errorMessage } from '../../lib/errors'
import { formatDateTime, formatTime, shortId } from '../../lib/format'
import { EVAL_TEXT } from '../../lib/termsEval'
import { ANALYSIS_TEXT, isAnalysisOp } from '../../lib/termsAnalysis'
import { paramSpecs, paramText, paramValue, type ParamSpec } from '../../canvas/analysisItems'
import { useCatalog } from '../../store/catalog'
import type { Run } from '../../types'
import type {
  EvalAnalysisMode, EvalAnalysisOp, EvalCaliber, EvalCase, EvalCaseBody, EvalCaseKind, EvalExpectation, EvalGold, EvalPreview,
  EvalRunQuery, EvalSuite, EvalTrial,
} from '../../typesEval'
import { graphFields, type InputField } from '../schedules/model'
import { cellText, expectationText, goldValueText } from './parts'

const T = EVAL_TEXT.editor
const MODES = ['sql', 'caliber', 'from_run', 'special'] as const
type Mode = (typeof MODES)[number]

/** 编辑一道已有的题时它属于哪一种加法（按期望答案的来源和期望种类认） */
function modeOf(c: EvalCase | null | undefined): Mode {
  if (!c) return 'sql'
  if (c.gold?.kind === 'caliber') return 'caliber'
  if (c.gold?.kind === 'from_run') return 'from_run'
  const counted = c.expectations.filter((e) => e.kind !== 'text')
  if (!c.gold || counted.every((e) => e.kind === 'none' || e.kind === 'ask')) return 'special'
  return 'sql'
}

/** 题目里那个要答出数（或答「没有数据」、反问）的期望：编辑时据它回填 */
const mainOf = (c: EvalCase | null | undefined) => c?.expectations.find((e) => e.kind !== 'text') ?? null
/** 别的期望（一道题配了不止一个，比如贡献再配一个「一个数」）：编辑框不改它们，保存时照旧带上 */
const othersOf = (c: EvalCase | null | undefined) => {
  const main = mainOf(c)
  return (c?.expectations ?? []).filter((e) => e !== main && e.kind !== 'text')
}
const pointsOf = (c: EvalCase | null | undefined) => (c?.expectations.find((e) => e.kind === 'text')?.points ?? []).join('\n')

/**
 * 加题、改题（细案 01 第 18 项：四种加法）。保存前服务端会再试算一次，没过不保存、照实写原因（遮罩列、只读守卫、取不到值……
 * 落在出问题的那一格下面）。数值期望不许人填（评测-D4）：这里只让人点选「取哪一格、哪一列、整张表」，值由 SQL / 口径算。
 *
 * - 写 SQL 定期望：选数据源、写 SQL、预览结果（服务端跑一次和期望同一条路的 SQL 探针）、点选格或列、试算；
 * - 选口径：工作流｜口径卡｜指标（和蓝图助手同一份口径清单，不能复用的照列、写明原因），填口径的输入；
 * - 从问数据运行认定：选一次运行、选它执行过的一条查询、选从结果里取什么，保存时服务端拷出那条 SQL、记下认定人；
 * - 期望没有数据（写一条应当返回 0 行的 SQL）或期望反问（不要期望答案）。
 */
export function CaseEditor({ open, onClose, suite, editing, initialQuestion, onSaved }: {
  open: boolean; onClose: () => void; suite: EvalSuite; editing?: EvalCase | null; initialQuestion?: string
  onSaved: (c: EvalCase) => void
}) {
  const datasources = useCatalog((s) => s.datasources)
  const [mode, setMode] = useState<Mode>('sql')
  const [question, setQuestion] = useState('')
  const [scope, setScope] = useState<string[]>([])
  const [tags, setTags] = useState('')
  const [enabled, setEnabled] = useState(true)
  const [points, setPoints] = useState('')
  const [required, setRequired] = useState(true)
  const [tolerance, setTolerance] = useState('')
  /** 要答出数的那一个期望（点选出来的，或编辑时回填的） */
  const [main, setMain] = useState<EvalExpectation | null>(null)
  /** 编辑的题另外配的期望：原样保留 */
  const [others, setOthers] = useState<EvalExpectation[]>([])
  // SQL（写 SQL 定期望、期望没有数据）
  const [sourceId, setSourceId] = useState('')
  const [sql, setSql] = useState('')
  const [preview, setPreview] = useState<EvalPreview | null>(null)
  const [previewing, setPreviewing] = useState(false)
  const [special, setSpecial] = useState<'none' | 'ask'>('none')
  // 口径
  const [caliber, setCaliber] = useState<{ workflow_id: string; version: number; node_id: string; metric: string } | null>(null)
  const [inputs, setInputs] = useState<Record<string, string>>({})
  // 从运行认定
  const [runId, setRunId] = useState('')
  const [queryId, setQueryId] = useState('')
  // 题目种类：问数据题（问法交给助手作答）或工作流题（固定一个已发布版本，按输入跑一次，考的是这一版本身）
  const [caseKind, setCaseKind] = useState<EvalCaseKind>('ask')
  const [workflowId, setWorkflowId] = useState('')
  const [workflowVersion, setWorkflowVersion] = useState<number | null>(null)
  const [workflowInputs, setWorkflowInputs] = useState<Record<string, string>>({})
  // 试算、保存
  const [trial, setTrial] = useState<EvalTrial | null>(null)
  const [trialing, setTrialing] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<{ message: string; field?: string } | null>(null)

  const fixedFromRun = editing?.gold?.kind === 'from_run'
  const radio = useRadioGroup(MODES, mode, (m) => pick(m))

  // 打开时回填（编辑）或清空（新建）
  useEffect(() => {
    if (!open) return
    const c = editing ?? null
    setMode(modeOf(c))
    setQuestion(c ? c.question : initialQuestion ?? '')
    setScope(c?.datasource_ids ?? [])
    setTags((c?.tags ?? []).join('，'))
    setEnabled(c?.enabled ?? true)
    setPoints(pointsOf(c))
    const m = mainOf(c)
    setMain(m)
    setOthers(othersOf(c))
    setRequired(m?.required !== false)
    setTolerance(m?.tolerance != null ? String(m.tolerance) : '')
    setSourceId(c?.gold?.datasource_id ?? '')
    setSql(c?.gold?.kind === 'sql' ? c.gold.sql ?? '' : '')
    setPreview(null)
    setSpecial(m?.kind === 'ask' ? 'ask' : 'none')
    setCaliber(c?.gold?.kind === 'caliber' && m?.metric
      ? { workflow_id: c.gold.workflow_id!, version: c.gold.version!, node_id: c.gold.node_id!, metric: m.metric } : null)
    setInputs(c?.gold?.kind === 'caliber' ? { ...(c.gold.inputs ?? {}) } : {})
    setRunId(c?.gold?.kind === 'from_run' ? c.gold.run_id ?? '' : '')
    setQueryId(c?.gold?.kind === 'from_run' ? c.gold.query ?? '' : '')
    setCaseKind(c?.kind ?? 'ask')
    setWorkflowId(c?.workflow_id ?? '')
    setWorkflowVersion(c?.workflow_version ?? null)
    setWorkflowInputs({ ...(c?.input_template ?? {}) })
    setTrial(null)
    setError(null)
  }, [open, editing?.id, editing?.version, initialQuestion])

  const pick = (m: Mode) => {
    if (m === mode || editing) return
    setMode(m)
    setMain(null)
    setOthers([])
    setTrial(null)
    setError(null)
    setPreview(null)
  }

  /** 交上去的期望：点选的那一个（带上必需、容差），另加文字要点 */
  const expectations = (): EvalExpectation[] => {
    const out: EvalExpectation[] = []
    if (mode === 'special') out.push({ kind: special })
    else if (main) {
      const e: EvalExpectation = { ...main, required }
      delete e.tolerance
      const t = tolerance.trim()
      if (t && (e.kind === 'scalar' || e.kind === 'table')) e.tolerance = Number(t)
      out.push(e)
    }
    out.push(...others)
    const list = points.split('\n').map((p) => p.trim()).filter(Boolean)
    if (list.length) out.push({ kind: 'text', points: list })
    return out
  }

  const gold = (): EvalGold | null => {
    if (mode === 'sql' || (mode === 'special' && special === 'none')) return { kind: 'sql', datasource_id: sourceId, sql }
    if (mode === 'caliber' && caliber) {
      return { kind: 'caliber', workflow_id: caliber.workflow_id, version: caliber.version, node_id: caliber.node_id, inputs }
    }
    return null
  }

  const body = (): EvalCaseBody => {
    const out: EvalCaseBody = {
      question, datasource_ids: scope, expectations: expectations(), enabled,
      tags: tags.split(/[,，、\n]/).map((t) => t.trim()).filter(Boolean),
    }
    if (!fixedFromRun) out.gold = gold()
    const workflow = caseKind === 'workflow' && (mode === 'sql' || mode === 'caliber')
    if (!editing) out.kind = workflow ? 'workflow' : 'ask'
    if (workflow) {
      out.workflow_id = workflowId
      out.workflow_version = workflowVersion
      out.input_template = Object.fromEntries(Object.entries(workflowInputs).filter(([, v]) => v.trim() !== ''))
    }
    return out
  }

  const fail = (e: unknown) => {
    setError({ message: errorMessage(e), field: e instanceof ApiError ? e.field : undefined })
  }

  const runPreview = async () => {
    setPreviewing(true)
    setError(null)
    setTrial(null)
    try {
      const p = await api.evals.preview(suite.id, { datasource_id: sourceId, sql })
      setPreview(p)
      if (p.status !== 'ok') setError({ message: p.reason ?? EVAL_TEXT.trialStatus[p.status], field: p.field ?? 'gold.sql' })
    } catch (e) {
      fail(e)
    } finally {
      setPreviewing(false)
    }
  }

  const runTrial = async () => {
    setTrialing(true)
    setError(null)
    try {
      const r = editing && !changed() ? await api.evals.trialCase(suite.id, editing.id) : await api.evals.trial(suite.id, body())
      setTrial(r.trial)
      if (r.trial.status !== 'ok') setError({ message: r.trial.reason ?? EVAL_TEXT.trialStatus[r.trial.status], field: 'gold' })
    } catch (e) {
      fail(e)
    } finally {
      setTrialing(false)
    }
  }

  /** 编辑时有没有改过要试算的那几样（问法、期望、来源）：没改的试算就是「按此刻重算已有的题」 */
  const changed = () => {
    if (!editing) return true
    const b = body()
    return JSON.stringify([b.question, b.expectations, b.gold ?? editing.gold])
      !== JSON.stringify([editing.question, editing.expectations, editing.gold])
  }

  const save = async () => {
    setSaving(true)
    setError(null)
    try {
      let saved
      if (editing) saved = await api.evals.updateCase(suite.id, editing.id, { ...body(), version: editing.version })
      else if (mode === 'from_run') {
        saved = await api.evals.createFromRun(suite.id, {
          run_id: runId, query: queryId, expectations: expectations(), question: question || undefined,
          tags: body().tags, datasource_ids: scope,
        })
      } else saved = await api.evals.createCase(suite.id, body())
      if (saved.trial) setTrial(saved.trial)
      toast.ok(T.saved(saved.case.seq))
      onSaved(saved.case)
      onClose()
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) setError({ message: T.conflict })
      else fail(e)
    } finally {
      setSaving(false)
    }
  }

  const fieldError = (...fields: string[]) => (error && error.field && fields.some((f) => error.field === f || error.field!.startsWith(`${f}.`))
    ? error.message : undefined)
  const generalError = error && !(error.field && ['question', 'gold.sql', 'gold.datasource_id', 'gold.inputs', 'query', 'tags',
    'workflow_id', 'workflow_version', 'input_template']
    .some((f) => error.field === f || error.field!.startsWith(`${f}.`))) ? error.message : null
  const needsMain = mode !== 'special'
  /** 工作流题只配「写 SQL 定期望」「选口径」两种：反问是助手的事，工作流不会反问；从运行认定只认问数据运行里的查询 */
  const workflowCase = caseKind === 'workflow' && (mode === 'sql' || mode === 'caliber')
  // 关掉之前要不要先问：新建时填了东西、编辑时改了要试算的那几样
  const dirty = editing ? changed() : !!(question.trim() || sql.trim() || main || points.trim() || runId)
  const ready = (!needsMain || !!main) && (mode !== 'from_run' || fixedFromRun || (!!runId && !!queryId))
    && (!workflowCase || (!!workflowId && !!workflowVersion))
    && (mode !== 'caliber' || !!caliber) && (!(mode === 'sql' || (mode === 'special' && special === 'none')) || (!!sourceId && !!sql.trim()))

  return (
    <Modal open={open} onClose={onClose} width={860} dirty={!saving && dirty}
           title={editing ? T.editTitle(editing.seq) : T.addTitle}
           footer={(
             <>
               <span className="mr-auto text-2xs text-faint">{T.saveHint}</span>
               <button type="button" className="btn" onClick={onClose}>{T.cancel}</button>
               <button type="button" className="btn" disabled={!ready || trialing || saving} onClick={() => void runTrial()}
                       data-eval-case-trial="">
                 {trialing && <Spinner size={11} />} {trialing ? T.trialing : T.trial}
               </button>
               <button type="button" className="btn btn-primary" disabled={!ready || saving} onClick={() => void save()}
                       data-eval-case-save="">
                 {saving && <Spinner size={11} />} {saving ? T.saving : T.save}
               </button>
             </>
           )}>
      <div className="space-y-4" data-eval-case-editor={mode}>
        <div>
          <div className="label" id="eval-case-mode">{T.modeLabel}</div>
          <div role="radiogroup" aria-labelledby="eval-case-mode" className="flex flex-wrap gap-1">
            {MODES.map((m) => (
              <button key={m} type="button" {...radio(m)} onClick={() => pick(m)} disabled={!!editing && m !== mode}
                      data-eval-case-mode={m}
                      className={clsx('rounded-md border px-2.5 py-1 text-xs transition-colors disabled:opacity-50',
                        mode === m ? 'border-[var(--accent)] bg-accent-soft text-fg' : 'text-dim hover:bg-hover')}>
                {T.mode[m]}
              </button>
            ))}
          </div>
          <p className="mt-1 text-2xs leading-relaxed text-faint">{T.modeHint[mode]}</p>
        </div>

        {(mode === 'sql' || mode === 'caliber') && (
          <CaseKindPart kind={caseKind} setKind={setCaseKind} disabled={!!editing} workflowId={workflowId}
                        setWorkflowId={(v) => { setWorkflowId(v); setWorkflowInputs({}) }} version={workflowVersion}
                        setVersion={setWorkflowVersion} inputs={workflowInputs} setInputs={setWorkflowInputs}
                        error={fieldError('workflow_id', 'workflow_version', 'input_template')} />
        )}

        <Field label={workflowCase ? T.workflowQuestion : T.question} htmlFor="eval-case-question"
               hint={workflowCase ? T.workflowQuestionHint : T.questionHint} error={fieldError('question')}
               required={mode !== 'from_run' && !workflowCase}>
          <textarea id="eval-case-question" className="field min-h-16" value={question} maxLength={2000}
                    placeholder={T.questionPlaceholder} onChange={(e) => setQuestion(e.target.value)} data-eval-case-question="" />
        </Field>

        {mode === 'sql' && (
          <SqlPart suiteId={suite.id} sourceId={sourceId} setSourceId={(v) => { setSourceId(v); setPreview(null); setMain(null) }}
                   sql={sql} setSql={(v) => { setSql(v); setPreview(null) }} preview={preview} previewing={previewing}
                   onPreview={() => void runPreview()} main={main} setMain={setMain}
                   sourceError={fieldError('gold.datasource_id')} sqlError={fieldError('gold.sql')} />
        )}
        {mode === 'caliber' && (
          <CaliberPart picked={caliber} onPick={(c) => { setCaliber(c); setMain(c ? { kind: 'scalar', metric: c.metric } : null); setInputs({}) }}
                       inputs={inputs} setInputs={setInputs} inputsError={fieldError('gold.inputs')} disabled={!!editing} />
        )}
        {mode === 'from_run' && (
          fixedFromRun ? (
            <Notice tone="info" attr={{ 'data-eval-case-fixed-gold': '' }}>
              {T.fromRunGold}
              {editing?.gold?.run_id && <> · <Link className="underline" to={`/runs/${encodeURIComponent(editing.gold.run_id)}`}>{shortId(editing.gold.run_id)}</Link></>}
            </Notice>
          ) : (
            <FromRunPart runId={runId} setRunId={setRunId} queryId={queryId} setQueryId={(q) => { setQueryId(q); setMain(null) }}
                         main={main} setMain={setMain} queryError={fieldError('query')} />
          )
        )}
        {mode === 'special' && (
          <SpecialPart special={special} setSpecial={setSpecial} disabled={!!editing}>
            {special === 'none' && (
              <SqlPart suiteId={suite.id} sourceId={sourceId} setSourceId={(v) => { setSourceId(v); setPreview(null) }}
                       sql={sql} setSql={(v) => { setSql(v); setPreview(null) }} preview={preview} previewing={previewing}
                       onPreview={() => void runPreview()} main={null} setMain={() => {}} pickable={false}
                       sourceError={fieldError('gold.datasource_id')} sqlError={fieldError('gold.sql')} />
            )}
          </SpecialPart>
        )}

        {needsMain && main && (
          <div className="flex flex-wrap items-end gap-3 rounded-md border bg-panel p-2.5 text-xs" data-eval-case-main={main.kind}>
            <div className="min-w-0 flex-1">
              <div className="label">{T.pickKind}</div>
              <div data-eval-case-main-text="">{EVAL_TEXT.expectation.kind[main.kind]}：{expectationText({ ...main, required: true })}</div>
            </div>
            {(main.kind === 'scalar' || main.kind === 'table') && (
              <Field label={T.tolerance} htmlFor="eval-case-tolerance" hint={T.toleranceHint}>
                <input id="eval-case-tolerance" className="field w-36" value={tolerance} inputMode="decimal"
                       placeholder={T.tolerancePlaceholder} onChange={(e) => setTolerance(e.target.value)} />
              </Field>
            )}
            <label className="flex items-center gap-1.5">
              <input type="checkbox" checked={required} onChange={(e) => setRequired(e.target.checked)} /> {T.required}
            </label>
          </div>
        )}
        {needsMain && !main && mode === 'sql' && <p className="text-2xs text-faint" data-eval-case-no-pick="">{T.noPick}</p>}
        {!!others.length && (
          <div className="space-y-0.5 rounded-md border border-dashed p-2.5 text-xs" data-eval-case-others={others.length}>
            <div className="label">{T.analysisOthers}</div>
            {others.map((e, i) => (
              <div key={e.id ?? i} className="text-dim">{EVAL_TEXT.expectation.kind[e.kind]}：{expectationText(e)}</div>
            ))}
          </div>
        )}

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label={T.scope} htmlFor="eval-case-scope" hint={T.scopeHint}>
            <select id="eval-case-scope" className="field min-h-16" multiple value={scope}
                    onChange={(e) => setScope([...e.target.selectedOptions].map((o) => o.value))} data-eval-case-scope="">
              {datasources.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
            </select>
          </Field>
          <div className="space-y-3">
            <Field label={T.tags} htmlFor="eval-case-tags" error={fieldError('tags')}>
              <input id="eval-case-tags" className="field" value={tags} placeholder={T.tagsPlaceholder} onChange={(e) => setTags(e.target.value)} />
            </Field>
            <label className="flex items-center gap-1.5 text-xs">
              <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /> {T.enabled}
            </label>
          </div>
        </div>
        {mode !== 'special' || special !== 'ask' ? (
          <Field label={T.points} htmlFor="eval-case-points" hint={T.pointsHint}>
            <textarea id="eval-case-points" className="field min-h-12" value={points} onChange={(e) => setPoints(e.target.value)} />
          </Field>
        ) : null}

        {trial && <TrialBox trial={trial} />}
        {generalError && <Notice tone="err" attr={{ 'data-eval-case-error': error?.field ?? '' }}>{generalError}</Notice>}
      </div>
    </Modal>
  )
}

// ---------------------------------------------------------------------------
// 题目种类：问数据题 / 工作流题
// ---------------------------------------------------------------------------

/**
 * 工作流题：固定一个工作流的已发布版本（服务端核对已发布、未退役），按输入模板跑一次探索运行，考的是这一版本身（不建图、
 * 不复核）。输入模板按那一版入口节点的字段填，可以写时间变量
 */
function CaseKindPart({ kind, setKind, disabled, workflowId, setWorkflowId, version, setVersion, inputs, setInputs, error }: {
  kind: EvalCaseKind; setKind: (k: EvalCaseKind) => void; disabled: boolean; workflowId: string; setWorkflowId: (v: string) => void
  version: number | null; setVersion: (v: number | null) => void; inputs: Record<string, string>
  setInputs: (v: Record<string, string>) => void; error?: string
}) {
  const workflows = useCatalog((s) => s.workflows)
  const published = workflows.filter((w) => w.published_version)
  const [fields, setFields] = useState<InputField[] | null>(null)
  useEffect(() => {
    if (kind !== 'workflow' || !workflowId || !version) { setFields(null); return }
    setFields(null)
    api.workflows.version(workflowId, version).then((v) => setFields(graphFields(v.graph)), () => setFields([]))
  }, [kind, workflowId, version])
  return (
    <div className="space-y-2" data-eval-case-kind={kind}>
      <div className="label">{T.caseKind}</div>
      <div className="flex flex-wrap gap-3 text-xs">
        {(['ask', 'workflow'] as const).map((k) => (
          <label key={k} className="flex items-center gap-1.5">
            <input type="radio" name="eval-case-kind" checked={kind === k} disabled={disabled} onChange={() => setKind(k)}
                   data-eval-case-kind-pick={k} />
            {EVAL_TEXT.caseRow.kind[k]}
          </label>
        ))}
      </div>
      <p className="text-2xs leading-relaxed text-faint">{T.caseKindHint[kind]}</p>
      {kind === 'workflow' && (
        <div className="grid gap-3 rounded-md border p-2.5 sm:grid-cols-[minmax(0,1fr)_8rem]">
          <Field label={T.workflow} htmlFor="eval-case-workflow" required>
            <select id="eval-case-workflow" className="field" value={workflowId} disabled={disabled} data-eval-case-workflow=""
                    onChange={(e) => {
                      const w = published.find((x) => x.id === e.target.value)
                      setWorkflowId(e.target.value)
                      setVersion(w?.published_version ?? null)
                    }}>
              <option value="">{published.length ? T.workflowPlaceholder : T.workflowNone}</option>
              {published.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
            </select>
          </Field>
          <Field label={T.workflowVersion} htmlFor="eval-case-workflow-version" required>
            <input id="eval-case-workflow-version" className="field tnum" inputMode="numeric" value={version ?? ''} disabled={disabled}
                   onChange={(e) => setVersion(e.target.value.trim() ? Number(e.target.value) : null)} />
          </Field>
          {fields === null && workflowId && version && <p className="text-2xs text-dim sm:col-span-2"><Spinner size={10} /> {T.caliberInputsLoading}</p>}
          {fields?.map((f) => (
            <Field key={f.name} label={f.name} htmlFor={`eval-case-input-${f.name}`} required={!!f.required} hint={f.description}
                   className="sm:col-span-2">
              <input id={`eval-case-input-${f.name}`} className="field mono text-xs" value={inputs[f.name] ?? ''}
                     onChange={(e) => setInputs({ ...inputs, [f.name]: e.target.value })} data-eval-case-input={f.name} />
            </Field>
          ))}
          {error && <p className="text-2xs text-[var(--err)] sm:col-span-2" role="alert">{error}</p>}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 写 SQL 定期望
// ---------------------------------------------------------------------------

function SqlPart({ suiteId: _suiteId, sourceId, setSourceId, sql, setSql, preview, previewing, onPreview, main, setMain, pickable = true,
  sourceError, sqlError }: {
  suiteId: string; sourceId: string; setSourceId: (v: string) => void; sql: string; setSql: (v: string) => void
  preview: EvalPreview | null; previewing: boolean; onPreview: () => void
  main: EvalExpectation | null; setMain: (e: EvalExpectation | null) => void; pickable?: boolean
  sourceError?: string; sqlError?: string
}) {
  const datasources = useCatalog((s) => s.datasources)
  const ok = preview?.status === 'ok'
  return (
    <div className="space-y-3" data-eval-sql="">
      <div className="grid gap-3 sm:grid-cols-[minmax(0,14rem)_1fr]">
        <Field label={T.source} htmlFor="eval-case-source" required error={sourceError}>
          <select id="eval-case-source" className="field" value={sourceId} onChange={(e) => setSourceId(e.target.value)}
                  aria-invalid={!!sourceError || undefined} data-eval-case-source="">
            <option value="">{T.sourcePlaceholder}</option>
            {datasources.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
          </select>
        </Field>
        <Field label={T.sql} htmlFor="eval-case-sql" required hint={T.sqlHint} error={sqlError}>
          <textarea id="eval-case-sql" className="field mono min-h-24 text-xs" value={sql} spellCheck={false}
                    placeholder={T.sqlPlaceholder} onChange={(e) => setSql(e.target.value)}
                    aria-invalid={!!sqlError || undefined} data-eval-case-sql="" />
        </Field>
      </div>
      <div className="flex items-center gap-2">
        <button type="button" className="btn btn-sm" disabled={!sourceId || !sql.trim() || previewing} onClick={onPreview}
                data-eval-case-preview="">
          {previewing && <Spinner size={11} />} {previewing ? T.previewing : T.preview}
        </button>
        {ok && preview && (
          <span className="text-2xs text-faint" data-eval-preview-count={preview.row_count ?? ''}>
            {T.previewRows(preview.shown ?? preview.rows.length, preview.row_count, preview.truncated)}
          </span>
        )}
      </div>
      {ok && preview && !preview.rows.length && <p className="text-2xs text-dim" data-eval-preview-empty="">{T.previewEmpty}</p>}
      {ok && preview && !!preview.rows.length && (
        <PreviewGrid preview={preview} main={main} setMain={setMain} pickable={pickable} />
      )}
      {ok && preview && !!preview.rows.length && pickable && (
        <AnalysisPick columns={preview.columns} main={main} setMain={setMain} />
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 分析期望：贡献、判定
// ---------------------------------------------------------------------------

const A = EVAL_TEXT.analysis
/** 判定期望能用的分析（贡献恒为变化拆解）、各自的模式（同服务端 contract.MODES） */
const VERDICT_OPS: EvalAnalysisOp[] = ['baseline', 'trend', 'differ', 'comparable']
const OP_MODES: Partial<Record<EvalAnalysisOp, EvalAnalysisMode[]>> = {
  decompose: ['additive', 'ratio', 'factor'], differ: ['rate', 'mean', 'rows'],
}
/**
 * 每种分析（模式）在表单里填哪几个参数：和分析卡的一项同一份（canvas/analysisItems.paramSpecs），不另抄。
 * 那张表里本来就没有评测期望不许写的几样——输入（query）、总数核对（headline）、下钻（parent）、两份查询
 * （base_query、target_query）：期望的输入只有期望 SQL 这一份快照（服务端 judgements._NOT_IN_EXPECTATION）
 */
const fieldsOf = (e: EvalExpectation): ParamSpec[] =>
  e.op && isAnalysisOp(e.op) ? paramSpecs(e.op, e.mode) : []

/** 换了分析方法、模式时，只留下新的那种分析还认的参数 */
function keepParams(e: EvalExpectation): EvalExpectation {
  const keys = new Set(fieldsOf(e).map((f) => f.key))
  const params = Object.fromEntries(Object.entries(e.params ?? {}).filter(([k]) => keys.has(k)))
  return { ...e, params }
}

/**
 * 在预览的结果上定一个分析期望：贡献（变化拆解：选模式、按哪几列拆、期次、度量……，期望点名前几段）或判定（波动基线、
 * 趋势判断、差异检验、可比性检查）。只填做哪种分析、按哪几列；主要来源、判定由服务端在期望 SQL 的结果上按平台的分析方法算，
 * 试算时写出来（界面不判）。列从预览结果里选；基期、本期、对照组是期次列、分组列里的值，可以写时间变量
 */
function AnalysisPick({ columns, main, setMain }: {
  columns: string[]; main: EvalExpectation | null; setMain: (e: EvalExpectation | null) => void
}) {
  const current = main && (main.kind === 'contribution' || main.kind === 'verdict') ? main : null
  const start = (kind: 'contribution' | 'verdict') => setMain(kind === 'contribution'
    ? { kind, op: 'decompose', mode: 'additive', params: {}, top_n: 1 }
    : { kind, op: 'baseline', params: {} })
  const update = (patch: Partial<EvalExpectation>) => current && setMain(keepParams({ ...current, ...patch }))
  const setParam = (key: string, value: unknown) => {
    if (!current) return
    const params = { ...(current.params ?? {}) }
    if (value === '' || value == null || (Array.isArray(value) && !value.length)) delete params[key]
    else params[key] = value
    setMain({ ...current, params })
  }
  const modes = current?.op ? OP_MODES[current.op] : undefined
  return (
    <div className="space-y-2 rounded-md border p-2.5 text-xs" data-eval-analysis={current?.kind ?? ''}>
      <div className="label">{T.analysisLabel}</div>
      <p className="text-2xs leading-relaxed text-faint">{T.analysisHint}</p>
      <div className="flex flex-wrap gap-1.5">
        {(['contribution', 'verdict'] as const).map((k) => (
          <button key={k} type="button" data-eval-analysis-kind={k} onClick={() => start(k)}
                  className={clsx('rounded-md border px-2 py-1 text-left', current?.kind === k ? 'border-[var(--accent)] bg-accent-soft text-fg' : 'text-dim hover:bg-hover')}>
            {T.analysisKind[k]}
          </button>
        ))}
      </div>
      {current && (
        <div className="grid gap-2 sm:grid-cols-2">
          {current.kind === 'verdict' && (
            <Field label={T.analysisOp} htmlFor="eval-analysis-op">
              <select id="eval-analysis-op" className="field" value={current.op} data-eval-analysis-op=""
                      onChange={(e) => {
                        const op = e.target.value as EvalAnalysisOp
                        update({ op, mode: OP_MODES[op]?.[0] })
                      }}>
                {VERDICT_OPS.map((o) => <option key={o} value={o}>{A.op[o]}</option>)}
              </select>
            </Field>
          )}
          {modes && (
            <Field label={T.analysisMode} htmlFor="eval-analysis-mode">
              <select id="eval-analysis-mode" className="field" value={current.mode ?? ''} data-eval-analysis-mode=""
                      onChange={(e) => update({ mode: e.target.value as EvalAnalysisMode })}>
                {modes.map((m) => <option key={m} value={m}>{A.method(A.op[current.op!], A.mode[m])}</option>)}
              </select>
            </Field>
          )}
          {current.kind === 'contribution' && (
            <Field label={T.analysisTopN} htmlFor="eval-analysis-topn">
              <input id="eval-analysis-topn" className="field tnum w-24" inputMode="numeric" value={current.top_n ?? 1}
                     data-eval-analysis-topn="" onChange={(e) => update({ top_n: Number(e.target.value) || 1 })} />
            </Field>
          )}
          {fieldsOf(current).map((f) => (
            <ParamInput key={`${current.op}:${current.mode}:${f.key}`} spec={f} columns={columns}
                        value={(current.params ?? {})[f.key]} onChange={(v) => setParam(f.key, v)} />
          ))}
          {fieldsOf(current).some((f) => f.key === 'base') && (
            <p className="text-2xs leading-relaxed text-faint sm:col-span-2">{T.analysisPeriodHint}</p>
          )}
        </div>
      )}
    </div>
  )
}

function ParamInput({ spec, columns, value, onChange }: {
  spec: ParamSpec; columns: string[]; value: unknown; onChange: (v: unknown) => void
}) {
  const key = spec.key
  const id = `eval-analysis-param-${key}`
  const label = A.param[key] ?? key
  const text = paramText(spec.kind, value)
  if (spec.kind === 'columns') {
    const picked = Array.isArray(value) ? value.map(String) : []
    return (
      <div className="sm:col-span-2" data-eval-analysis-param={key}>
        <div className="label">{label}</div>
        <div className="flex flex-wrap gap-2">
          {columns.map((c) => (
            <label key={c} className="flex items-center gap-1">
              <input type="checkbox" checked={picked.includes(c)} data-eval-analysis-param-col={c}
                     onChange={(e) => onChange(e.target.checked ? [...picked, c] : picked.filter((x) => x !== c))} />
              {c}
            </label>
          ))}
        </div>
      </div>
    )
  }
  if (spec.kind === 'pairs') {
    return (
      <Field label={label} htmlFor={id} hint={T.analysisPairsHint} className="sm:col-span-2">
        <textarea id={id} className="field min-h-12 text-xs" defaultValue={text} data-eval-analysis-param={key}
                  onChange={(e) => onChange(paramValue('pairs', e.target.value))} />
      </Field>
    )
  }
  if (spec.kind === 'basis' || spec.kind === 'grain') {
    // 和分析卡同两份下拉：空着是缺省（比较方式缺省「和比较期比的变化率」、粒度按期的写法推断），其余每种各有叫法
    const opts = spec.kind === 'basis' ? A.basis : A.grain
    return (
      <Field label={label} htmlFor={id}>
        <select id={id} className="field" value={text} data-eval-analysis-param={key}
                onChange={(e) => onChange(e.target.value)}>
          {Object.entries(opts).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </Field>
    )
  }
  if (spec.kind === 'column') {
    const options = Object.fromEntries(columns.map((c) => [c, c]))
    return (
      <Field label={label} htmlFor={id}>
        <select id={id} className="field" value={text} data-eval-analysis-param={key}
                onChange={(e) => onChange(e.target.value)}>
          <option value="">{T.analysisColumn}</option>
          {Object.entries(options).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </Field>
    )
  }
  // 期次、对照组、叫法可以写 {{ schedule.* }} 这类时间变量；整数的格子写了不是整数的字照原样交给服务端校验
  return (
    <Field label={label} htmlFor={id}>
      <input id={id} className={clsx('field', spec.kind === 'int' && 'tnum w-24')} value={text}
             inputMode={spec.kind === 'int' ? 'numeric' : undefined}
             placeholder={ANALYSIS_TEXT.node.paramPlaceholder[key]}
             data-eval-analysis-param={key}
             onChange={(e) => onChange(paramValue(spec.kind, e.target.value))} />
    </Field>
  )
}

/** 预览结果：点一格定「一个数」，点列名定「名单」；「多行」选配对的列。格里的值是服务端遮罩过、确认不涉及遮罩列的 */
function PreviewGrid({ preview, main, setMain, pickable }: {
  preview: EvalPreview; main: EvalExpectation | null; setMain: (e: EvalExpectation | null) => void; pickable: boolean
}) {
  const cols = preview.columns
  /** 点的是第几行：改成按维度找这一行之后期望里就不写行号了，下面「按哪几列找这一行」还要照着这一行列 */
  const [picked, setPicked] = useState<number | null>(main?.kind === 'scalar' && main.row != null ? main.row : null)
  const isCell = (c: string, r: number) => main?.kind === 'scalar' && main.column === c && picked === r
  const isCol = (c: string) => main?.kind === 'set' && main.column === c
  const tableKeys = main?.kind === 'table' ? main.keys ?? [] : []
  const row = main?.kind === 'scalar' && picked != null ? preview.rows[picked] : null
  return (
    <div className="space-y-2" data-eval-preview="">
      {pickable && <p className="text-2xs text-faint">{T.previewPick}</p>}
      <div className="max-h-64 overflow-auto rounded-md border">
        <table className="w-full text-xs">
          <thead className="sticky top-0 bg-panel">
            <tr>
              {cols.map((c) => (
                <th key={c} className="border-b px-2 py-1 text-left font-medium">
                  {pickable ? (
                    <button type="button" className={clsx('rounded px-1 hover:bg-hover', isCol(c) && 'bg-accent-soft text-fg')}
                            onClick={() => setMain({ kind: 'set', column: c })} title={T.pickColumn(c)} data-eval-preview-col={c}>
                      {c}
                    </button>
                  ) : c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {preview.rows.map((r, i) => (
              <tr key={i} className="border-b last:border-b-0">
                {cols.map((c, j) => (
                  <td key={c} className="px-2 py-0.5 tnum">
                    {pickable ? (
                      <button type="button" className={clsx('w-full rounded px-1 text-left hover:bg-hover', isCell(c, i) && 'bg-accent-soft text-fg')}
                              onClick={() => { setPicked(i); setMain({ kind: 'scalar', column: c, row: i }) }} title={T.pickCell(c, i)}
                              data-eval-preview-cell={`${i}:${c}`}>
                        {cellText(r[j])}
                      </button>
                    ) : cellText(r[j])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {pickable && main?.kind === 'scalar' && row && (
        <div className="text-xs" data-eval-preview-where="">
          <div className="label">{T.whereLabel}</div>
          <div className="flex flex-wrap gap-2">
            {cols.filter((c) => c !== main.column).map((c) => {
              const value = row[cols.indexOf(c)]
              const on = !!main.where && c in main.where
              const disabled = value == null || typeof value === 'object'
              return (
                <label key={c} className={clsx('flex items-center gap-1', disabled && 'opacity-50')}>
                  <input type="checkbox" checked={on} disabled={disabled} onChange={(e) => {
                    const where = { ...(main.where ?? {}) }
                    if (e.target.checked) where[c] = value as string | number
                    else delete where[c]
                    const next: EvalExpectation = { kind: 'scalar', column: main.column }
                    if (Object.keys(where).length) next.where = where
                    else next.row = picked ?? 0
                    setMain(next)
                  }} data-eval-preview-where-col={c} />
                  {c}={cellText(value)}
                </label>
              )
            })}
          </div>
        </div>
      )}
      {pickable && (
        <div className="text-xs" data-eval-preview-keys="">
          <div className="label">{T.keysLabel}</div>
          <div className="flex flex-wrap gap-2">
            {cols.map((c) => (
              <label key={c} className="flex items-center gap-1">
                <input type="checkbox" checked={tableKeys.includes(c)} onChange={(e) => {
                  const keys = e.target.checked ? [...tableKeys, c] : tableKeys.filter((k) => k !== c)
                  setMain(keys.length ? { kind: 'table', keys, ordered: false } : null)
                }} data-eval-preview-key={c} />
                {c}
              </label>
            ))}
          </div>
        </div>
      )}
      {preview.rendered.sql && (
        <details className="text-2xs text-faint">
          <summary>{T.previewRendered}</summary>
          <pre className="mono mt-1 whitespace-pre-wrap">{preview.rendered.sql}</pre>
        </details>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 选口径
// ---------------------------------------------------------------------------

function CaliberPart({ picked, onPick, inputs, setInputs, inputsError, disabled }: {
  picked: { workflow_id: string; version: number; node_id: string; metric: string } | null
  onPick: (c: { workflow_id: string; version: number; node_id: string; metric: string } | null) => void
  inputs: Record<string, string>; setInputs: (v: Record<string, string>) => void; inputsError?: string; disabled: boolean
}) {
  const [list, setList] = useState<EvalCaliber[] | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [q, setQ] = useState('')
  const [fields, setFields] = useState<InputField[] | null>(null)
  useEffect(() => {
    api.evals.calibers().then((r) => setList(r.calibers), setError)
  }, [])
  // 口径卡读的取数节点要的入口字段：从那一版的图上读
  useEffect(() => {
    if (!picked) { setFields(null); return }
    setFields(null)
    api.workflows.version(picked.workflow_id, picked.version).then((v) => setFields(graphFields(v.graph)), () => setFields([]))
  }, [picked?.workflow_id, picked?.version])
  const shown = useMemo(() => (list ?? []).filter((c) => !q.trim()
    || [c.workflow_name, c.card, c.name, c.metric_id, c.caliber].some((x) => (x ?? '').toLowerCase().includes(q.trim().toLowerCase()))), [list, q])
  const groups = [...new Set(shown.map((c) => `${c.workflow_id}@${c.version}`))]
  if (error) return <Notice tone="err">{errorMessage(error)}</Notice>
  if (!list) return <p className="flex items-center gap-1.5 text-xs text-dim"><Spinner size={11} /> {T.caliberLoading}</p>
  if (!list.length) return <p className="text-xs text-dim" data-eval-caliber-empty="">{T.caliberEmpty}</p>
  const isPicked = (c: EvalCaliber) => !!picked && picked.workflow_id === c.workflow_id && picked.version === c.version
    && picked.node_id === c.node_id && picked.metric === c.metric_id
  return (
    <div className="space-y-3" data-eval-calibers="">
      <input className="field" value={q} placeholder={T.caliberSearch} onChange={(e) => setQ(e.target.value)} aria-label={T.caliberSearch} />
      <div className="max-h-64 space-y-2 overflow-auto rounded-md border p-2">
        {groups.map((g) => {
          const items = shown.filter((c) => `${c.workflow_id}@${c.version}` === g)
          return (
            <div key={g}>
              <div className="text-2xs font-medium text-dim">{items[0].workflow_name} v{items[0].version}</div>
              <ul className="mt-0.5 space-y-0.5">
                {items.map((c) => (
                  <li key={`${c.node_id}:${c.metric_id}`}>
                    <label className={clsx('flex items-start gap-2 rounded px-1 py-0.5 text-xs', c.reusable ? 'hover:bg-hover' : 'opacity-60')}
                           title={c.reason ?? undefined}>
                      <input type="radio" name="eval-caliber" disabled={!c.reusable || disabled} checked={isPicked(c)}
                             onChange={() => onPick({ workflow_id: c.workflow_id, version: c.version, node_id: c.node_id, metric: c.metric_id })}
                             data-eval-caliber={`${c.workflow_id}@${c.version}:${c.node_id}:${c.metric_id}`} />
                      <span className="min-w-0">
                        <span>{c.card} · {c.name}{c.unit ? `（${c.unit}）` : ''}</span>
                        {!c.reusable && <span className="block text-2xs text-[var(--warn)]">{T.caliberNotReusable}：{c.reason}</span>}
                      </span>
                    </label>
                  </li>
                ))}
              </ul>
            </div>
          )
        })}
      </div>
      {picked && (
        <div className="space-y-2" data-eval-caliber-inputs="">
          <div className="label">{T.caliberInputs}</div>
          <p className="text-2xs text-faint">{T.caliberInputsHint}</p>
          {fields === null && <p className="text-2xs text-dim"><Spinner size={10} /> {T.caliberInputsLoading}</p>}
          {fields && !fields.length && <p className="text-2xs text-dim">{T.caliberInputsNone}</p>}
          {fields?.map((f) => (
            <Field key={f.name} label={f.name} htmlFor={`eval-caliber-input-${f.name}`} required={!!f.required}
                   hint={f.description}>
              <input id={`eval-caliber-input-${f.name}`} className="field mono text-xs" value={inputs[f.name] ?? ''}
                     onChange={(e) => setInputs({ ...inputs, [f.name]: e.target.value })} data-eval-caliber-input={f.name} />
            </Field>
          ))}
          {inputsError && <p className="text-2xs text-[var(--err)]" role="alert">{inputsError}</p>}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 从问数据运行认定
// ---------------------------------------------------------------------------

function FromRunPart({ runId, setRunId, queryId, setQueryId, main, setMain, queryError }: {
  runId: string; setRunId: (v: string) => void; queryId: string; setQueryId: (v: string) => void
  main: EvalExpectation | null; setMain: (e: EvalExpectation | null) => void; queryError?: string
}) {
  const [recent, setRecent] = useState<Run[] | null>(null)
  const [typed, setTyped] = useState('')
  const [queries, setQueries] = useState<EvalRunQuery[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  useEffect(() => {
    api.runs.list({ origin_kind: 'chat', limit: 20 }).then(setRecent, () => setRecent([]))
  }, [])
  const load = async (id: string) => {
    setRunId(id)
    setQueryId('')
    setQueries(null)
    setError(null)
    if (!id) return
    setLoading(true)
    try {
      setQueries((await api.evals.runQueries(id)).queries)
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      setLoading(false)
    }
  }
  const picked = queries?.find((q) => q.artifact === queryId) ?? null
  return (
    <div className="space-y-3" data-eval-from-run="">
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label={T.runRecent} htmlFor="eval-from-run-recent">
          <select id="eval-from-run-recent" className="field" value={recent?.some((r) => r.id === runId) ? runId : ''}
                  onChange={(e) => void load(e.target.value)} data-eval-from-run-recent="">
            <option value="">{recent && !recent.length ? T.runRecentEmpty : T.runPick}</option>
            {(recent ?? []).map((r) => (
              <option key={r.id} value={r.id}>
                {String((r.input as any)?.question ?? r.workflow_name).slice(0, 60)} · {formatTime(r.created_at ?? null)}
              </option>
            ))}
          </select>
        </Field>
        <Field label={T.runId} htmlFor="eval-from-run-id">
          <div className="flex gap-1.5">
            <input id="eval-from-run-id" className="field mono" value={typed} placeholder={T.runIdPlaceholder}
                   onChange={(e) => setTyped(e.target.value.trim())} />
            <button type="button" className="btn btn-sm shrink-0" disabled={!typed} onClick={() => void load(typed)}>{T.runLoad}</button>
          </div>
        </Field>
      </div>
      {loading && <p className="text-xs text-dim"><Spinner size={11} /></p>}
      {error && <Notice tone="err">{error}</Notice>}
      {queries && (
        <div className="space-y-1.5">
          <div className="label">{T.runQueries}</div>
          {!queries.length && <p className="text-xs text-dim">{T.runQueriesEmpty}</p>}
          <ul className="space-y-1.5">
            {queries.map((q) => (
              <li key={q.artifact} className={clsx('rounded-md border p-2 text-xs', q.artifact === queryId && 'border-[var(--accent)]')}
                  data-eval-run-query={q.artifact}>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-dim">{q.source}</span>
                  <span className="text-faint">{T.runQueryRows(q.row_count)}</span>
                  {q.masked ? <span className="chip text-[var(--warn)]">{T.runQueryMasked}</span> : (
                    <button type="button" className="btn btn-sm ml-auto" onClick={() => setQueryId(q.artifact)} data-eval-run-query-pick="">
                      {T.runQueryPick}
                    </button>
                  )}
                </div>
                <pre className="mono mt-1 max-h-24 overflow-auto whitespace-pre-wrap text-2xs text-dim">{q.sql}</pre>
              </li>
            ))}
          </ul>
        </div>
      )}
      {picked && (
        <div className="space-y-1.5 text-xs" data-eval-from-run-columns="">
          <div className="label">{T.runColumnPick}</div>
          <div className="flex flex-wrap gap-1.5">
            {picked.columns.map((c) => (
              <span key={c} className="inline-flex items-center gap-1">
                <button type="button" className={clsx('btn btn-sm', main?.kind === 'scalar' && main.column === c && 'border-[var(--accent)]')}
                        onClick={() => setMain({ kind: 'scalar', column: c, row: 0 })} data-eval-from-run-cell={c}>
                  {T.pickCell(c, 0)}
                </button>
                <button type="button" className={clsx('btn btn-sm', main?.kind === 'set' && main.column === c && 'border-[var(--accent)]')}
                        onClick={() => setMain({ kind: 'set', column: c })} data-eval-from-run-col={c}>
                  {T.pickColumn(c)}
                </button>
              </span>
            ))}
          </div>
          <p className="text-2xs text-faint">{T.fromRunNote}</p>
        </div>
      )}
      {queryError && <p className="text-2xs text-[var(--err)]" role="alert">{queryError}</p>}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 期望没有数据或期望反问
// ---------------------------------------------------------------------------

function SpecialPart({ special, setSpecial, disabled, children }: {
  special: 'none' | 'ask'; setSpecial: (v: 'none' | 'ask') => void; disabled: boolean; children?: ReactNode
}) {
  return (
    <div className="space-y-3" data-eval-special={special}>
      <div className="label">{T.specialKind}</div>
      <div className="grid gap-2 sm:grid-cols-2">
        {(['none', 'ask'] as const).map((k) => (
          <label key={k} className={clsx('flex items-start gap-2 rounded-md border p-2 text-xs', special === k && 'border-[var(--accent)]')}>
            <input type="radio" name="eval-special" className="mt-0.5" checked={special === k} disabled={disabled}
                   onChange={() => setSpecial(k)} data-eval-special-kind={k} />
            <span>{k === 'none' ? T.specialNone : T.specialAsk}
              <span className="block text-2xs leading-relaxed text-faint">{k === 'none' ? T.specialNoneHint : T.specialAskHint}</span></span>
          </label>
        ))}
      </div>
      {children}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 试算结果
// ---------------------------------------------------------------------------

/** 试算交回的：通过就列出算出的期望值（值是服务端算好的），没过照实写原因；运行点得开 */
export function TrialBox({ trial }: { trial: EvalTrial }) {
  const ok = trial.status === 'ok'
  return (
    <div className={clsx('space-y-1.5 rounded-md border p-2.5 text-xs', ok ? 'border-[var(--st-done)]' : 'border-[var(--warn)]')}
         data-eval-trial={trial.status}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{ok ? T.trialOk : EVAL_TEXT.trialStatus[trial.status]}</span>
        <span className="text-faint">{T.trialAsOf(formatDateTime(trial.as_of))}</span>
        {trial.run_ids.map((id) => (
          <Link key={id} to={`/runs/${encodeURIComponent(id)}`} className="mono text-2xs text-dim underline">{shortId(id)}</Link>
        ))}
      </div>
      {!ok && trial.reason && <p className="text-dim" data-eval-trial-reason="">{trial.reason}</p>}
      {ok && trial.sets.map((s, i) => (
        <div key={i}>
          {trial.sets.length > 1 && <div className="text-2xs text-faint">v{s.version}</div>}
          <ul className="space-y-0.5">
            {Object.entries(s.values).map(([id, v]) => (
              <li key={id} data-eval-trial-value={id}>
                <span className="text-faint">{T.trialExpected}：</span>{goldValueText(v)}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  )
}
