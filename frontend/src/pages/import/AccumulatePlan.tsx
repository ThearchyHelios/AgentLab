import type { AccumulatePeriodRef, AccumulatePlan as Plan, CheckResult } from '../../types'
import { formatNumber } from '../../lib/format'
import { ACCUMULATE_TEXT } from '../../lib/terms'
import { CheckStatusChip } from './ImportReceipt'

// ===========================================================================
// 累积计划（P3-SPEC 2.4、10.2）：启用之后当前版本由哪几期组成、本期是新增还是替换、哪几期会移出当前版本、
// 行数（本期 / 启用后当前版本），以及整体的结构核对。全部来自服务端的计划，界面不推算：行数照服务端给的写，
// 没有物化（只有一期）时「启用后当前版本」就是本期
// ===========================================================================

type Row = AccumulatePeriodRef & { state: 'new' | 'kept' | 'replaced' | 'dropped' }

const periodKey = (p: { start: string | null; end: string | null }) => `${p.start ?? ''}~${p.end ?? ''}`
const periodText = (p: { start: string | null; end: string | null }) =>
  (p.start && p.end ? ACCUMULATE_TEXT.period(p.start, p.end) : ACCUMULATE_TEXT.periodUnknown)

const STATE_LABEL: Record<Row['state'], string> = {
  new: ACCUMULATE_TEXT.partNew,
  kept: '',
  replaced: ACCUMULATE_TEXT.partReplaced,
  dropped: ACCUMULATE_TEXT.partDropped,
}

export function AccumulatePlan({ plan, checks }: {
  plan: Plan
  /** 并集的结构核对 U1–U3（TrialOut.union_checks；物化了才有） */
  checks?: CheckResult[] | null
}) {
  const rejected = plan.action === 'rejected'
  // 结果里的各期、被替换的那一期、移出当前版本的各期，按统计期排在一起：一眼看出启用后是哪几期
  const rows: Row[] = [
    ...(plan.parts ?? []).map((p) => ({ ...p, state: (p.new ? 'new' : 'kept') as Row['state'] })),
    ...(plan.replaces ? [{ ...plan.replaces, state: 'replaced' as const }] : []),
    ...(plan.dropped ?? []).map((p) => ({ ...p, state: 'dropped' as const })),
  ].sort((a, b) => (a.start ?? '').localeCompare(b.start ?? '') || (a.state === 'new' ? 1 : -1))
  const current = (plan.parts ?? []).find((p) => p.new)
  const after = plan.union?.rows ?? current?.rows ?? {}
  const tables = [...new Set([...Object.keys(current?.rows ?? {}), ...Object.keys(after)])]
  const blockers = (plan.parts ?? []).filter((p) => p.blockers?.length)
  const columnText = (x: { table: string; column: string | null }) =>
    (x.column ? `「${x.table}」的「${x.column}」` : `「${x.table}」（${ACCUMULATE_TEXT.wholeTable}）`)

  return (
    <section className="space-y-2 rounded-lg border bg-panel px-3 py-2 text-xs" data-accumulate-plan={plan.action}>
      <h4 className="flex flex-wrap items-baseline gap-x-2 font-semibold">
        <span>{ACCUMULATE_TEXT.title}</span>
        <span className="font-normal" style={{ color: rejected || plan.action === 'restart' ? 'var(--warn)' : undefined }}
              data-plan-action>
          {ACCUMULATE_TEXT.action[plan.action] ?? plan.action}
        </span>
      </h4>
      {plan.reason && <p className="leading-relaxed text-dim" data-plan-reason>{plan.reason}</p>}

      <ul className="space-y-1">
        {rows.map((p, i) => (
          <li key={`${periodKey(p)}-${p.state}-${i}`} data-plan-part={periodKey(p)} data-plan-state={p.state}
              className="flex flex-wrap items-baseline gap-x-2 rounded-md border px-2.5 py-1.5"
              style={p.state === 'dropped' || p.state === 'replaced' ? { opacity: 0.7 } : undefined}>
            <span className="tnum">{periodText(p)}</span>
            {p.seq != null && <span className="text-2xs text-faint">{ACCUMULATE_TEXT.importSeq(formatNumber(p.seq))}</span>}
            {p.file_name && <span className="min-w-0 truncate text-2xs text-faint" title={p.file_name}>{p.file_name}</span>}
            {STATE_LABEL[p.state] && (
              <span className="chip" data-plan-label={p.state}
                    style={p.state === 'new' ? { color: 'var(--accent)', borderColor: 'var(--accent)' }
                      : { color: 'var(--warn)', borderColor: 'var(--warn)' }}>
                {STATE_LABEL[p.state]}
              </span>
            )}
          </li>
        ))}
      </ul>

      {!rejected && tables.length > 0 && (
        <table className="w-full text-left text-2xs" data-plan-rows>
          <thead className="text-faint">
            <tr>
              <th className="py-0.5 pr-2 font-normal">{ACCUMULATE_TEXT.table}</th>
              <th className="py-0.5 pr-2 text-right font-normal" data-col-this>{ACCUMULATE_TEXT.thisPeriod}</th>
              <th className="py-0.5 text-right font-normal" data-col-after>{ACCUMULATE_TEXT.afterEnable}</th>
            </tr>
          </thead>
          <tbody>
            {tables.map((t) => (
              <tr key={t} className="border-t" data-plan-table={t}>
                <td className="py-0.5 pr-2">{t}</td>
                <td className="tnum py-0.5 pr-2 text-right" data-rows-this>{current?.rows?.[t] != null ? formatNumber(current.rows[t]) : '—'}</td>
                <td className="tnum py-0.5 text-right" data-rows-after>{after[t] != null ? formatNumber(after[t]) : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {!!plan.overlaps?.length && (
        <div className="space-y-0.5" data-plan-overlaps>
          <div className="font-medium text-[var(--warn)]">{ACCUMULATE_TEXT.overlaps}</div>
          <ul className="list-disc pl-4 text-dim">
            {plan.overlaps.map((o, i) => <li key={i} className="tnum" data-plan-overlap={periodKey(o)}>{periodText(o)}</li>)}
          </ul>
        </div>
      )}
      {!!plan.gaps?.length && (
        <p className="text-dim" data-plan-gaps>{ACCUMULATE_TEXT.gaps(plan.gaps.map(periodText).join('、'))}</p>
      )}
      {plan.backfill && <p className="text-dim" data-plan-backfill>{ACCUMULATE_TEXT.backfill}</p>}
      {!!plan.semantic?.length && (
        <div className="space-y-0.5" data-plan-semantic>
          <div className="font-medium text-[var(--warn)]">{ACCUMULATE_TEXT.semantic}</div>
          <ul className="list-disc pl-4 text-dim">{plan.semantic.map((s, i) => <li key={i}>{s}</li>)}</ul>
        </div>
      )}
      {plan.action === 'restart' && <p className="text-2xs leading-relaxed text-dim" data-plan-restart-hint>{ACCUMULATE_TEXT.restartHint}</p>}
      {blockers.map((p, i) => (
        <p key={`b${i}`} className="text-dim" data-plan-blocker={periodKey(p)}>
          {ACCUMULATE_TEXT.blockers(periodText(p), p.blockers.map((b) => b.message).join('；'))}
        </p>
      ))}
      {!!plan.added?.length && (
        <p className="text-dim" data-plan-added>{ACCUMULATE_TEXT.added}：{plan.added.map(columnText).join('、')}</p>
      )}
      {!!plan.retired_new?.length && (
        <p className="text-dim" data-plan-retired>{ACCUMULATE_TEXT.retired}：{plan.retired_new.map(columnText).join('、')}</p>
      )}
      {!!plan.label_sets?.length && (
        <div className="space-y-0.5" data-plan-label-sets>
          <div className="text-dim">{ACCUMULATE_TEXT.labelSets}</div>
          <ul className="list-disc pl-4 text-2xs text-dim">
            {plan.label_sets.flatMap((l) => l.periods.flatMap((p, j) => [
              ...(p.missing.length ? [<li key={`${l.table}-${l.column}-${j}-m`}>
                「{l.column}」{ACCUMULATE_TEXT.labelMissing(periodText(p), p.missing.map((x) => `「${x}」`).join('、'))}</li>] : []),
              ...(p.extra.length ? [<li key={`${l.table}-${l.column}-${j}-e`}>
                「{l.column}」{ACCUMULATE_TEXT.labelExtra(periodText(p), p.extra.map((x) => `「${x}」`).join('、'))}</li>] : []),
            ]))}
          </ul>
        </div>
      )}
      {!!checks?.length && (
        <div className="space-y-1" data-plan-checks>
          <div className="font-medium">{ACCUMULATE_TEXT.checks}</div>
          <ul className="space-y-1">
            {checks.map((c) => (
              <li key={c.id} className="space-y-0.5 rounded-md border px-2.5 py-1.5" data-check={c.id} data-status={c.status}>
                <div className="flex flex-wrap items-center gap-2">
                  <CheckStatusChip status={c.status} />
                  <span className="min-w-0 flex-1">{c.title}</span>
                </div>
                {!!c.details?.length && <ul className="text-2xs text-dim">{c.details.slice(0, 4).map((d, i) => <li key={i}>{d}</li>)}</ul>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}
