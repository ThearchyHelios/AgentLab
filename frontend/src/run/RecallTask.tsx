/**
 * recall 待办的卡身（待办箱、待办页里 TaskCard 的一段；docs/design/next-round/02-口径账本.md 做法 7）。
 *
 * 三种来由（payload.reason，服务端 ledger/recall.task_payload 给）：
 * - caliber：口径发布了新版本，同一份数据上新旧两版各算一次，这一组答案用到的数变了。列旧口径的值、新口径的值、两版之差
 *   （召回运行里召回卡上的三个指标）和每个答案当时的值，每个数都点得开出处（召回卡的指标链；正式运行的口径卡指标链；问数
 *   回答的那条查询快照）。动作「按新版重新出具」（服务端走按口径出具同一套判法，能不能、为什么不能由它给）和「知道了」；
 * - catalog：答案依据的数据目录里影响数字的项被驳回或改了，按新目录检查这次执行过的 SQL 出现了新的错误级问题；只有「知道了」；
 * - structure：口径依据的表结构变了，试算没有算出来；只有「知道了」。
 *
 * 界面层不放逻辑：数变没变、值是不是遮罩、能不能重新出具都照服务端给的写。使用者看到的值服务端已经按遮罩换过（masked）。
 */
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Check, ExternalLink, RefreshCw } from 'lucide-react'
import { EvidenceDrawer, type EvidenceTarget } from '../apps/EvidenceDrawer'
import { Spinner } from '../components/ui'
import { useAtLeast } from '../store/auth'
import { formatDateTime } from '../lib/format'
import { RECALL_TEXT } from '../lib/termsRecall'
import type { Task } from '../typesApp'
import type { RecallAnswer, RecallCell, RecallOriginal, RecallTaskPayload } from '../typesRecall'

const T = RECALL_TEXT.task

function cellText(cell: { text: string | null; masked: boolean } | undefined): string {
  if (!cell) return T.missing
  if (cell.masked) return T.masked
  return cell.text ?? T.missing
}

export function RecallTask({ task, canAct, busy, onRespond }: {
  task: Task; canAct: boolean; busy: boolean; onRespond: (decision: 'reissue' | 'ack') => void
}) {
  const payload = (task.payload ?? {}) as Partial<RecallTaskPayload>
  const [evidence, setEvidence] = useState<EvidenceTarget | null>(null)
  const reason = payload.reason ?? 'caliber'
  const name = payload.workflow_name || ''
  const actions = Array.isArray(payload.actions) ? payload.actions : []
  const reissue = actions.find((a) => a.decision === 'reissue')
  const ack = actions.find((a) => a.decision === 'ack')
  const pending = task.status === 'pending'
  const outcome = (task.outcome ?? payload.outcome ?? null) as Record<string, any> | null

  return (
    <div className="mb-2 space-y-2" data-task-recall={reason}>
      {reason === 'caliber' && (
        <>
          <p className="text-2xs leading-relaxed text-dim">{T.intro(name, payload.from_version, payload.to_version)}</p>
          {!!payload.binding?.length && (
            <div className="flex flex-wrap items-center gap-1 text-2xs" data-recall-binding="">
              <span className="text-faint">{T.binding}：</span>
              {payload.binding.map((b) => (
                <span key={b.field} className="chip mono" data-recall-binding-field={b.field}>
                  {b.field}={b.masked ? T.masked : String(b.value ?? '')}
                </span>
              ))}
            </div>
          )}
          <Metrics payload={payload} onOpen={setEvidence} />
          <Answers payload={payload} onOpen={setEvidence} />
          <p className="text-2xs leading-relaxed text-faint">{T.how}</p>
        </>
      )}
      {reason === 'catalog' && <CatalogBasis payload={payload} />}
      {reason === 'structure' && <StructureBasis payload={payload} name={name} />}

      {!pending && outcome?.decision && (
        <div className="flex flex-wrap items-center gap-x-2 text-2xs text-dim" data-recall-outcome={outcome.decision}>
          <span>{T.resolved(outcome.decision)}</span>
          {outcome.issued_run && <IssuedLink runId={String(outcome.issued_run)} />}
        </div>
      )}
      {canAct && (
        <div className="flex gap-2">
          {reissue && (
            <button type="button" className="btn btn-primary flex-1 justify-center" data-recall-reissue=""
                    disabled={busy || !reissue.available} title={reissue.reason ?? T.reissueHint}
                    onClick={() => onRespond('reissue')}>
              {busy ? <Spinner size={12} /> : <RefreshCw size={12} aria-hidden />} {T.reissue}
            </button>
          )}
          {ack && (
            <button type="button" className="btn flex-1 justify-center" data-recall-ack="" disabled={busy || !ack.available}
                    onClick={() => onRespond('ack')}>
              <Check size={12} aria-hidden /> {T.ack}
            </button>
          )}
        </div>
      )}
      {canAct && reissue && !reissue.available && reissue.reason && (
        <div className="text-2xs leading-relaxed" style={{ color: 'var(--st-waiting)' }} data-recall-reissue-reason="">
          {reissue.reason}
        </div>
      )}
      {canAct && reissue?.issued_run && <IssuedLink runId={reissue.issued_run} />}
      <EvidenceDrawer target={evidence} onClose={() => setEvidence(null)} />
    </div>
  )
}

/** 三个数（旧口径、新口径、两版之差）一行一个指标，每个都点得开召回卡上那个指标的出处链 */
function Metrics({ payload, onOpen }: { payload: Partial<RecallTaskPayload>; onOpen: (t: EvidenceTarget) => void }) {
  const metrics = Array.isArray(payload.metrics) ? payload.metrics : []
  if (!metrics.length) return null
  const run = payload.recall_run ?? ''
  const open = (label: string, side: string, cell: RecallCell) => {
    if (!run || !cell.metric) return
    onOpen({ evidence: { run_id: run, kind: 'metric', node: 'recall', metric: cell.metric },
             label: T.evidenceTitle(label, side), text: cellText(cell) })
  }
  const heads = [T.old(payload.from_version), T.new(payload.to_version), T.diff]
  return (
    <div className="overflow-x-auto rounded border bg-bg" data-recall-metrics={metrics.length}>
      <table className="w-full text-2xs">
        <thead>
          <tr className="text-left text-faint">
            <th className="px-2 py-1 font-normal">{T.metric}</th>
            {heads.map((h) => <th key={h} className="px-2 py-1 text-right font-normal">{h}</th>)}
          </tr>
        </thead>
        <tbody>
          {metrics.map((m) => (
            <tr key={m.k} className="border-t" data-recall-metric={m.metric}
                data-changed={m.row_order === 'unstable' ? 'unknown' : m.changed ? 'true' : 'false'}>
              <td className="px-2 py-1">
                {m.name}
                {m.row_order === 'unstable' ? (
                  <span className="ml-1 text-faint" title={T.rowOrderHint} data-recall-row-order="">{T.rowOrder}</span>
                ) : (
                  <span className="ml-1 text-faint" style={m.changed ? { color: 'var(--st-waiting)' } : undefined}>
                    {m.changed ? T.changed : T.same}
                  </span>
                )}
              </td>
              {(['old', 'new', 'diff'] as const).map((side, i) => (
                <td key={side} className="px-2 py-1 text-right">
                  <button type="button" className="tnum underline-offset-2 hover:underline" data-recall-cell={side}
                          disabled={m[side].masked} onClick={() => open(m.name, heads[i], m[side])}>
                    {cellText(m[side])}
                  </button>
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** 用到这份口径的答案：哪一种、什么时候、当时的值（点得开当时的出处）、去哪里看 */
function Answers({ payload, onOpen }: { payload: Partial<RecallTaskPayload>; onOpen: (t: EvidenceTarget) => void }) {
  const answers = Array.isArray(payload.answers) ? payload.answers : []
  const names = new Map((payload.metrics ?? []).map((m) => [m.k, m.name]))
  if (!answers.length) return null
  const open = (answer: RecallAnswer, value: RecallOriginal) => {
    const label = T.evidenceTitle(names.get(value.k) ?? '', T.original)
    if (value.node && value.metric) {
      onOpen({ evidence: { run_id: answer.run_id, kind: 'metric', node: value.node, metric: value.metric }, label,
               text: cellText(value) })
    } else if (value.query) {
      onOpen({ evidence: { run_id: answer.run_id, kind: 'cell', artifact: value.query }, label, text: cellText(value) })
    }
  }
  return (
    <div data-recall-answers={answers.length}>
      <div className="mb-1 text-2xs font-medium text-dim">{T.answers}</div>
      <ul className="space-y-1">
        {answers.map((a) => (
          <li key={`${a.run_id}-${a.kind}`} className="rounded border bg-bg px-2 py-1 text-2xs" data-recall-answer={a.run_id}
              data-recall-answer-kind={a.kind}>
            <div className="flex flex-wrap items-center gap-x-2">
              <span className="text-dim">{T.answerOf(a.kind_text, a.at ? formatDateTime(a.at) : null)}</span>
              <AnswerLink answer={a} />
            </div>
            <div className="mt-0.5 flex flex-wrap gap-x-3">
              {a.original.map((v) => (
                <span key={v.k}>
                  <span className="text-faint">{names.get(v.k) ?? ''} · {T.original}：</span>
                  {v.missing && !v.masked
                    ? <span className="text-faint" title={v.missing}>{T.missing}</span>
                    : (
                      <button type="button" className="tnum underline-offset-2 hover:underline" data-recall-original={v.k}
                              disabled={v.masked || (!v.metric && !v.query)} onClick={() => open(a, v)}>
                        {cellText(v)}
                      </button>
                    )}
                </span>
              ))}
            </div>
          </li>
        ))}
      </ul>
    </div>
  )
}

/** 去看这个答案：问数回答回到那段对话；别的（正式运行）给搭建者去记录页 */
function AnswerLink({ answer }: { answer: RecallAnswer }) {
  const builder = useAtLeast('builder')
  if (answer.kind === 'answer' && answer.conversation_id) {
    return (
      <Link to={`/chat/${encodeURIComponent(answer.conversation_id)}`} data-recall-answer-link="chat"
            className="inline-flex items-center gap-1 text-dim underline-offset-2 hover:text-fg hover:underline">
        <ExternalLink size={10} aria-hidden /> {T.openTurn}
      </Link>
    )
  }
  if (!builder) return null
  return (
    <Link to={`/runs/${encodeURIComponent(answer.run_id)}`} data-recall-answer-link="run"
          className="inline-flex items-center gap-1 text-dim underline-offset-2 hover:text-fg hover:underline">
      <ExternalLink size={10} aria-hidden /> {T.openAnswer}
    </Link>
  )
}

/** 重新出具的那次正式运行：搭建者链到记录页；使用者进不了记录页（点了会被送回应用首页），只写已经出具 */
function IssuedLink({ runId }: { runId: string }) {
  const builder = useAtLeast('builder')
  if (!builder) {
    return <span className="text-2xs text-dim" data-recall-issued-note="">{T.issuedUserNote}</span>
  }
  return (
    <Link to={`/runs/${encodeURIComponent(runId)}`} data-recall-issued={runId}
          className="inline-flex items-center gap-1 text-2xs text-dim underline-offset-2 hover:text-fg hover:underline">
      <ExternalLink size={10} aria-hidden /> {T.issuedRun}
    </Link>
  )
}

function CatalogBasis({ payload }: { payload: Partial<RecallTaskPayload> }) {
  const basis = payload.basis ?? {}
  const items = Array.isArray(basis.items) ? basis.items : []
  const checks = Array.isArray(basis.checks) ? basis.checks : []
  return (
    <div className="space-y-1.5 text-2xs" data-recall-basis="catalog">
      <p className="leading-relaxed text-dim">{T.catalogIntro}</p>
      {items.length > 0 && (
        <div>
          <span className="text-faint">{T.catalogItems}：</span>
          {items.map((i, k) => (
            <span key={`${i.path}-${k}`}>
              {k > 0 && '、'}{i.label ?? i.path}{i.change ? `（${T.change[i.change] ?? i.change}）` : ''}
            </span>
          ))}
        </div>
      )}
      {basis.new_errors != null && <div className="text-dim">{T.catalogChecks(basis.new_errors)}</div>}
      {checks.length > 0 && (
        <ul className="list-disc space-y-0.5 pl-4" data-recall-checks={checks.length}>
          {checks.map((c, k) => <li key={`${c.code}-${k}`}>{c.message}</li>)}
        </ul>
      )}
    </div>
  )
}

function StructureBasis({ payload, name }: { payload: Partial<RecallTaskPayload>; name: string }) {
  const basis = payload.basis ?? {}
  const builder = useAtLeast('builder')
  return (
    <div className="space-y-1.5 text-2xs" data-recall-basis="structure">
      <p className="leading-relaxed text-dim">{T.structureIntro(name, basis.version)}</p>
      {basis.table && <div className="text-dim">{T.structureWhere(basis.table, basis.column)}</div>}
      {basis.error && (
        <div className="rounded border bg-bg p-1.5">
          <span className="text-faint">{T.probeError}：</span>{basis.error}
        </div>
      )}
      {builder && basis.probe_run && (
        <Link to={`/runs/${encodeURIComponent(basis.probe_run)}`}
              className="inline-flex items-center gap-1 text-dim underline-offset-2 hover:text-fg hover:underline">
          <ExternalLink size={10} aria-hidden /> {T.openProbe}
        </Link>
      )}
    </div>
  )
}
