import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { Scale } from 'lucide-react'
import { ApiError, api } from '../api/client'
import { EvidenceDrawer, type EvidenceTarget } from '../apps/EvidenceDrawer'
import { Field, Modal, Spinner, StatusBadge, toast } from '../components/ui'
import { errorMessage } from '../lib/errors'
import { LEDGER_UI } from '../lib/termsLedger'
import { useAtLeast } from '../store/auth'
import { useLedger } from '../store/ledger'
import type {
  LedgerField, LedgerPairOptions, LedgerReconcileAction, LedgerReconcileIn, LedgerReconciled, LedgerReconcileGroup,
  LedgerRelated, RunLedger,
} from '../typesLedger'
import { BindingList, DiffList, caliberOf } from './LedgerStep'

/**
 * 口径对账（docs/design/next-round/02-口径账本.md 做法 6 的界面）：「差在哪」比对时就有，「差多少」点开才跑一次确定性
 * 派生的只读图（来源 reconcile，服务端专用），差值在对账卡里算（叙述层无算术，界面也不算）。
 *
 * 流程：核对参数 → 发起 → 三个数各自点得开。
 * - 核对参数：服务端在动作里写了 needs_check（参数是推断的、不全的、带遮罩值的）就先让人核对，表单照动作里的入口字段画
 *   （必填、有没有缺省值），遮罩值不回填、要人重新填写；不用核对的照样能「修改参数」，改了就按核对过的参数发起。
 * - 发起：服务端认不出这次的哪一列对口径的哪个指标时回 409（ledger_reconcile_needs_pairs）、带上可选的列和指标，这里照它
 *   画「选择要对照的指标」，人选好再发起；参数不对的回 422 / 409，把服务端的话写在表单下面。
 * - 结果（ReconcileResults）：每一组对照写按你的问法、按口径、差值（另附相对差），每个数按
 *   /runs/{对账运行}/evidence/metrics/{对账卡}/{指标} 取值，点一下打开它的出处链（apps/EvidenceDrawer，和应用页点数同一套）。
 *   哪几个指标是一组由服务端给（reconciled.groups），这里不按命名规则拼。
 */

const NEEDS_PAIRS = 'ledger_reconcile_needs_pairs'
const CHECK_INPUTS = 'ledger_reconcile_check_inputs'

/** 核对参数的表单：动作里的入口字段（老答复没有时按绑定的字段），值先填绑定出来的，遮罩值空着 */
function fieldsOf(action: LedgerReconcileAction): LedgerField[] {
  if (action.fields?.length) return action.fields
  return action.binding.map((b) => ({ name: b.field, required: false, has_default: false }))
}

function draftOf(action: LedgerReconcileAction): Record<string, string> {
  const out: Record<string, string> = {}
  for (const f of fieldsOf(action)) {
    const b = action.binding.find((x) => x.field === f.name)
    out[f.name] = b && !b.masked && b.value != null ? String(b.value) : ''
  }
  return out
}

export function ReconcileDialog({ runId, action, related, onClose }: {
  runId: string; action: LedgerReconcileAction; related?: LedgerRelated; onClose: () => void
}) {
  const refresh = useLedger((s) => s.refresh)
  const fields = fieldsOf(action)
  const [values, setValues] = useState<Record<string, string>>(() => draftOf(action))
  const [editing, setEditing] = useState(action.needs_check)
  const [options, setOptions] = useState<LedgerPairOptions | null>(null)
  const [pairs, setPairs] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState('')
  const caliber = caliberOf(action)
  const masked = new Set(action.binding.filter((b) => b.masked).map((b) => b.field))
  const chosen = Object.entries(pairs).filter(([, m]) => !!m).map(([ours, metric]) => ({ ours, metric }))
  const dirty = editing && JSON.stringify(values) !== JSON.stringify(draftOf(action))

  const launch = async () => {
    if (options && !chosen.length) { setProblem(LEDGER_UI.reconcile.pairEmpty); return }
    setBusy(true)
    setProblem('')
    const body: LedgerReconcileIn = { query: action.query, workflow_id: action.workflow_id, version: action.version }
    if (editing) {
      // 空着的不送：留空的项按口径的缺省值（服务端按入口字段校验，缺必填的照实拒收）
      body.inputs = Object.fromEntries(Object.entries(values).filter(([, v]) => v.trim() !== '').map(([k, v]) => [k, v.trim()]))
      body.confirm = true
    }
    if (options) body.pairs = chosen
    try {
      await api.caliberLedger.reconcile(runId, body)
      toast.ok(LEDGER_UI.reconcile.launched)
      refresh(runId)
      onClose()
    } catch (e) {
      if (e instanceof ApiError && e.code === NEEDS_PAIRS) {
        const raw = (e.body as { options?: LedgerPairOptions } | undefined)?.options
        if (raw && Array.isArray(raw.columns) && Array.isArray(raw.metrics)) setOptions(raw)
        setProblem(e.message)
      } else if (e instanceof ApiError && e.code === CHECK_INPUTS) {
        setEditing(true)
        setProblem(e.message)
      } else {
        setProblem(errorMessage(e))
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal open onClose={onClose} width={560} dirty={dirty} title={LEDGER_UI.reconcile.title(caliber)}
           footer={<>
             <button type="button" className="btn" onClick={onClose}>{LEDGER_UI.reconcile.cancel}</button>
             <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void launch()}
                     data-ledger-reconcile-launch="">
               {busy ? <Spinner size={12} /> : <Scale size={12} aria-hidden />} {busy ? LEDGER_UI.reconcile.busy : LEDGER_UI.reconcile.launch}
             </button>
           </>}>
      <div className="space-y-3 text-xs" data-ledger-reconcile-dialog="">
        <p className="text-dim">{LEDGER_UI.reconcile.intro}</p>
        {related && (
          <section>
            <div className="mb-1 text-2xs font-medium text-faint">{LEDGER_UI.reconcile.where}</div>
            <DiffList related={related} />
          </section>
        )}
        <section data-ledger-params={editing ? 'edit' : 'fixed'}>
          <div className="mb-1 flex items-center gap-2">
            <span className="text-2xs font-medium text-faint">{LEDGER_UI.reconcile.params}</span>
            {!editing && fields.length > 0 && (
              <button type="button" className="btn btn-xs btn-ghost" onClick={() => setEditing(true)} data-ledger-edit-params="">
                {LEDGER_UI.reconcile.editParams}
              </button>
            )}
          </div>
          {editing ? (
            <>
              <p className="mb-1.5 text-2xs" style={{ color: action.needs_check ? 'var(--st-waiting)' : undefined }}
                 data-ledger-check-reason="">
                {action.check_reason || LEDGER_UI.reconcile.paramsHint}
              </p>
              <div className="grid grid-cols-2 gap-2 max-sm:grid-cols-1">
                {fields.map((f) => (
                  <Field key={f.name} label={f.name} htmlFor={`ledger-param-${f.name}`} required={f.required}
                         hint={f.has_default ? LEDGER_UI.reconcile.defaulted : f.required ? LEDGER_UI.reconcile.required : undefined}>
                    {(p) => (
                      <input {...p} className="field mono text-xs" value={values[f.name] ?? ''} data-ledger-param-input={f.name}
                             placeholder={masked.has(f.name) ? LEDGER_UI.reconcile.maskedPlaceholder : undefined}
                             onChange={(e) => setValues((v) => ({ ...v, [f.name]: e.target.value }))} />
                    )}
                  </Field>
                ))}
              </div>
            </>
          ) : (
            <div className="text-2xs">
              <p className="mb-1 text-faint">{LEDGER_UI.reconcile.paramsFixed}</p>
              <BindingList binding={action.binding} />
            </div>
          )}
        </section>
        {options && (
          <section data-ledger-pairs="">
            <div className="mb-1 text-2xs font-medium text-faint">{LEDGER_UI.reconcile.pairs}</div>
            <p className="mb-1.5 text-2xs text-dim">{LEDGER_UI.reconcile.pairsHint}</p>
            <div className="space-y-1.5">
              {options.columns.map((col) => (
                <label key={col} className="flex items-center gap-2 text-2xs">
                  <span className="mono w-28 shrink-0 truncate" title={col}>{col}</span>
                  <span className="text-faint" aria-hidden>→</span>
                  <select className="field text-xs" value={pairs[col] ?? ''} data-ledger-pair={col}
                          onChange={(e) => setPairs((p) => ({ ...p, [col]: e.target.value }))}>
                    <option value="">{LEDGER_UI.reconcile.pairNone}</option>
                    {options.metrics.map((m) => (
                      <option key={m.id} value={m.id}>{m.name}{m.unit ? `（${m.unit}）` : ''}</option>
                    ))}
                  </select>
                </label>
              ))}
            </div>
          </section>
        )}
        {problem && <p className="text-2xs text-[var(--err)]" role="alert" data-ledger-reconcile-problem="">{problem}</p>}
      </div>
    </Modal>
  )
}

/** 正在取的指标值，按（运行、卡、指标）记一份：同一组数在面板、轮次两处挂着时只取一次 */
const values = new Map<string, Promise<string>>()
function valueOf(runId: string, card: string, metric: string): Promise<string> {
  const key = `${runId}\u0000${card}\u0000${metric}`
  let p = values.get(key)
  if (!p) {
    p = api.apps.metricEvidence(runId, card, metric).then((d) => String(d?.rendered ?? ''))
    values.set(key, p)
    p.catch(() => values.delete(key))
  }
  return p
}

/** 对账卡上的一个数：取值，点一下打开它的出处链 */
function MetricValue({ runId, card, metric, what, label, onOpen }: {
  runId: string; card: string; metric: string; what: string; label: string; onOpen: (t: EvidenceTarget) => void
}) {
  const [text, setText] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    let alive = true
    setText(null)
    setFailed(false)
    valueOf(runId, card, metric).then((v) => { if (alive) setText(v) }, () => { if (alive) setFailed(true) })
    return () => { alive = false }
  }, [runId, card, metric])
  return (
    <span className="inline-flex items-baseline gap-1">
      <span className="text-faint">{what}</span>
      <button type="button" className="mono tnum rounded px-0.5 underline decoration-dotted underline-offset-2 hover:bg-hover"
              disabled={text == null} title={LEDGER_UI.result.open(what, label)} data-ledger-value={metric}
              onClick={() => onOpen({ evidence: { run_id: runId, kind: 'metric', node: card, metric }, label: `${label} · ${what}`,
                                      text: text ?? '' })}>
        {failed ? LEDGER_UI.result.noValue : text == null ? '…' : text || '—'}
      </button>
    </span>
  )
}

const SHOWN = 3

function GroupRow({ runId, card, group, onOpen }: {
  runId: string; card: string; group: LedgerReconcileGroup; onOpen: (t: EvidenceTarget) => void
}) {
  const R = LEDGER_UI.result
  return (
    <li className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 text-2xs" data-ledger-group={group.ours}>
      <span className="font-medium">{group.label}</span>
      <MetricValue runId={runId} card={card} metric={group.ours} what={R.ours} label={group.label} onOpen={onOpen} />
      <MetricValue runId={runId} card={card} metric={group.caliber} what={R.caliber} label={group.label} onOpen={onOpen} />
      <MetricValue runId={runId} card={card} metric={group.diff} what={R.diff} label={group.label} onOpen={onOpen} />
      <MetricValue runId={runId} card={card} metric={group.rel} what={R.rel} label={group.label} onOpen={onOpen} />
    </li>
  )
}

function ReconcileCard({ item, data, original }: { item: LedgerReconciled; data?: RunLedger; original?: string }) {
  const builder = useAtLeast('builder')
  const [all, setAll] = useState(false)
  const [target, setTarget] = useState<EvidenceTarget | null>(null)
  const c = (data?.calibers ?? []).find((x) => x.caliber === item.caliber)
  const caliber = c ? caliberOf(c) : caliberOf({ version: item.version })
  const groups = item.groups ?? []
  const shown = all ? groups : groups.slice(0, SHOWN)
  const R = LEDGER_UI.result
  const state = item.status === 'succeeded' ? 'succeeded' : item.status === 'failed' ? 'failed'
    : item.status === 'cancelled' ? 'cancelled' : 'running'
  return (
    <div className="mt-1.5 rounded border px-2 py-1.5" data-ledger-reconciled={item.run_id} data-ledger-reconciled-state={state}>
      <div className="flex flex-wrap items-center gap-1.5 text-2xs">
        <StatusBadge status={state} size={11} decorative />
        <span className="font-medium">{R.title(caliber)}</span>
        {item.inputs_from && R.inputs[item.inputs_from] && <span className="text-faint">{R.inputs[item.inputs_from]}</span>}
        {builder && <Link to={`/runs/${item.run_id}`} className="text-[var(--accent)] hover:underline">{R.openRun}</Link>}
      </div>
      {state === 'running' && <p className="mt-1 flex items-center gap-1.5 text-2xs text-dim"><Spinner size={11} /> {R.running}</p>}
      {state === 'failed' && <p className="mt-1 text-2xs text-dim">{R.failed}</p>}
      {state === 'cancelled' && <p className="mt-1 text-2xs text-dim">{R.cancelled}</p>}
      {state === 'succeeded' && item.card && groups.length > 0 && (
        <>
          <ul className="mt-1 space-y-1">
            {shown.map((g) => <GroupRow key={g.ours} runId={item.run_id} card={item.card!} group={g} onOpen={setTarget} />)}
          </ul>
          {groups.length > SHOWN && (
            <button type="button" className="btn btn-xs btn-ghost mt-0.5 -ml-1" onClick={() => setAll((v) => !v)}>
              {all ? R.less : R.more(groups.length - SHOWN)}
            </button>
          )}
          {original && <p className="mt-1 text-2xs text-faint" data-ledger-original="">{R.original(original)}</p>}
        </>
      )}
      <EvidenceDrawer target={target} onClose={() => setTarget(null)} />
    </div>
  )
}

/** 对过的账（新的在前）。original：从证据面板打开时，正文里这个数写的是什么 */
export function ReconcileResults({ items, data, original }: {
  runId: string; items: LedgerReconciled[]; data?: RunLedger; original?: string
}) {
  const sorted = useMemo(() => items, [items])
  if (!sorted.length) return null
  return (
    <div data-ledger-reconciled-list="">
      {sorted.map((item) => <ReconcileCard key={item.run_id} item={item} data={data} original={original} />)}
    </div>
  )
}
