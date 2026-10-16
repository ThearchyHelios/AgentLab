import { useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { BookMarked, RotateCw, Scale } from 'lucide-react'
import { ApiError, api } from '../api/client'
import { Spinner, StatusBadge, toast } from '../components/ui'
import { errorMessage } from '../lib/errors'
import { LEDGER_TEXT } from '../lib/terms'
import { LEDGER_UI, caliberName } from '../lib/termsLedger'
import { useAtLeast } from '../store/auth'
import { useLedger, useRunLedger, type LedgerSlot } from '../store/ledger'
import type {
  LedgerBinding, LedgerIssueAction, LedgerIssued, LedgerLabel, LedgerReconcileAction, LedgerRelated, RunLedger, SegmentLedger,
} from '../typesLedger'
import { ReconcileDialog, ReconcileResults } from './LedgerReconcile'

/**
 * 口径账本在证据里的那一部分（docs/design/next-round/02-口径账本.md 做法 3、5、6 的界面）：
 * - 证据面板出处链最上面的「口径」一步（LedgerPart）：来自口径——「X」v3（受管）· 参数 · 怎么认的（等价已证明 / 助手选用）
 *   · 按口径正式出具；相关口径——差在哪 + 看差多少（口径对账：核对参数 → 发起 → 三个数各自点得开）；这次的推理——
 *   写「没有相关的已发布口径」；没比对的写原因。比对还没出结果时写「正在比对口径」，store/ledger 按退避轮询，
 *   等满了还没结论照实说、给「再取一次」；
 * - 按口径正式出具那一项（IssueControl）和出具过的正式运行（IssuedList）：问数据的轮次摘要用同一份。
 *
 * 界面层不放逻辑：标签、能不能出具、能不能自动、不能的原因、要不要先核对参数都是服务端给的（GET /runs/{id}/ledger），
 * 这里只照着画。比对在封存之后做，脚注写明「封存后比对，不在封存范围内」，事后补比的另写按哪种口径回看。
 */

/** 级别的叫法：服务端给 level_text，老答复没有时按 LEDGER_TEXT 的说法 */
export const levelOf = (x: { level?: string | null; level_text?: string }) =>
  x.level_text || (x.level ? (LEDGER_TEXT.level as Record<string, string>)[x.level] ?? '' : '')

export const caliberOf = (x: { workflow_name?: string | null; version?: number | null; level?: string | null; level_text?: string }) =>
  caliberName(x.workflow_name, x.version, levelOf(x))

/** 一次运行的口径账本里，某份报告的某个片段的标签：先看按运行取的那一份，没有时退回片段接口的 ledger 键 */
export function labelOf(data: RunLedger | undefined, report: string | undefined, segId: string,
  fallback?: SegmentLedger | null): LedgerLabel | undefined {
  const fromRun = report ? data?.reports?.[report]?.labels?.[segId] : undefined
  return fromRun ?? fallback?.label ?? undefined
}

/** 能不能取账本：正式运行本身就是某一版口径的出具，不比对；没有运行（预览、导出的文档）也没有 */
export const ledgerEnabled = (runId: string | undefined, runClass: string | undefined) => !!runId && runClass !== 'formal'

/**
 * 取账本出了错要不要说：服务端说这次运行不存在（404）时不说——证据链那边已经照实说了运行取不到，这里再挂一句「口径比对
 * 读不出」是同一件事说两遍；别的错（服务端出错、连不上）照实说、给「再取一次」
 */
export const ledgerErrorShown = (slot: LedgerSlot | undefined) =>
  slot?.status === 'error' && !(slot.error instanceof ApiError && slot.error.status === 404)

/** 绑定的参数：字段 = 值，遮罩值只写「已遮罩」 */
export function BindingList({ binding }: { binding?: LedgerBinding[] }) {
  if (!binding?.length) return <span className="text-faint">{LEDGER_UI.step.noParams}</span>
  return (
    <span className="flex flex-wrap gap-1" data-ledger-binding="">
      {binding.map((b) => (
        <span key={b.field} className="chip mono" data-ledger-param={b.field} data-masked={b.masked ? '' : undefined}>
          {b.field} = {b.masked ? LEDGER_UI.step.masked : String(b.value ?? '')}
        </span>
      ))}
    </span>
  )
}

/** 比对还没出结论 / 等满了 / 读不出：三种都说清，后两种给「再取一次」 */
export function LedgerWaiting({ slot, runId, compact = false }: { slot: LedgerSlot; runId: string; compact?: boolean }) {
  const refresh = useLedger((s) => s.refresh)
  const retry = (
    <button type="button" className="btn btn-xs btn-ghost" onClick={() => refresh(runId)} data-ledger-retry="">
      <RotateCw size={11} aria-hidden /> {LEDGER_UI.step.retry}
    </button>
  )
  if (slot.status === 'error') {
    return (
      <div className="flex flex-wrap items-center gap-1.5 text-dim" data-ledger-state="error">
        <span>{LEDGER_UI.step.failed}：{errorMessage(slot.error)}</span>{retry}
      </div>
    )
  }
  if (slot.timedOut) {
    return (
      <div className="flex flex-wrap items-center gap-1.5 text-dim" data-ledger-state="timeout">
        <span>{compact ? LEDGER_UI.turn.timeout : LEDGER_UI.step.timeout}</span>{retry}
      </div>
    )
  }
  return (
    <div data-ledger-state="pending" role="status">
      <span className="flex items-center gap-1.5 text-dim"><Spinner size={11} /> {LEDGER_UI.step.pending}</span>
      {!compact && <span className="mt-0.5 block text-2xs text-faint">{LEDGER_UI.step.pendingSub}</span>}
    </div>
  )
}

/** 比对的来历：封存后比对，不在封存范围内；事后补比的写按哪种口径回看 */
export function LedgerFootnote({ data }: { data: RunLedger }) {
  return (
    <p className="mt-1 text-2xs text-faint" data-ledger-footnote="">
      {[data.note || LEDGER_TEXT.sub, data.retro ? data.retro_note || LEDGER_TEXT.retro : ''].filter(Boolean).join(' · ')}
    </p>
  )
}

/**
 * 按口径正式出具（整题等价时）。服务端给了能不能、能不能自动、不能的原因：能自动的写「已在自动出具」、等出具的那一次出现
 * （等满了还没有，给按钮）；只给一键的给按钮，按钮下写为什么不自动；不能出具的照写原因（使用者没有应用授权时是「请搭建者
 * 出具」）。同一运行、同一口径、同一组参数只发起一次：再点交回那一次
 */
export function IssueControl({ runId, action, slot, readOnly = false }: {
  runId: string; action: LedgerIssueAction; slot?: LedgerSlot; readOnly?: boolean
}) {
  const refresh = useLedger((s) => s.refresh)
  const [busy, setBusy] = useState(false)
  const caliber = caliberOf(action)
  const issued = action.issued_run || (slot?.data?.issued ?? []).some((i) => i.workflow_id === action.workflow_id
    && i.version === action.version)
  if (issued) return null
  if (!action.available) {
    return <p className="text-2xs text-dim" data-ledger-issue="refused">{action.reason}</p>
  }
  if (action.auto && !slot?.timedOut) {
    return (
      <p className="flex items-center gap-1.5 text-2xs text-dim" data-ledger-issue="auto" role="status">
        <Spinner size={11} /> {LEDGER_UI.issue.auto}
      </p>
    )
  }
  const issue = async () => {
    setBusy(true)
    try {
      const out = await api.caliberLedger.issue(runId, { workflow_id: action.workflow_id, version: action.version })
      toast.ok(out.created ? LEDGER_UI.issue.launched(caliber) : LEDGER_UI.issue.again(caliber))
      refresh(runId)
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }
  return (
    <div data-ledger-issue="manual">
      <button type="button" className="btn btn-xs" disabled={busy || readOnly} onClick={() => void issue()}
              title={readOnly ? LEDGER_UI.issue.readOnly : undefined} data-ledger-issue-button="">
        {busy ? <Spinner size={11} /> : <BookMarked size={11} aria-hidden />} {busy ? LEDGER_UI.issue.busy : LEDGER_UI.issue.button}
      </button>
      {action.auto && slot?.timedOut && <p className="mt-0.5 text-2xs text-faint">{LEDGER_UI.issue.autoTimeout}</p>}
      {!action.auto && action.auto_reason && (
        <p className="mt-0.5 text-2xs text-faint" data-ledger-auto-reason="">{action.auto_reason}</p>
      )}
    </div>
  )
}

/** 出具过的一次正式运行的状态：结束了写档位（完整出具 / 降档出具 / 不予出具），没结束写进行中 */
function issuedState(i: LedgerIssued): { code: string; text: string } {
  if (i.status === 'succeeded') return { code: 'succeeded', text: i.tier_text || LEDGER_TEXT.kind.caliber }
  if (i.status === 'cancelled' && i.end_reason === 'rejected') return { code: 'rejected', text: LEDGER_UI.issued.rejected }
  if (i.status === 'cancelled') return { code: 'cancelled', text: LEDGER_UI.issued.cancelled }
  if (i.status === 'failed') return { code: 'failed', text: LEDGER_UI.issued.failed }
  return { code: 'running', text: LEDGER_UI.issued.running }
}

/** 出具过的正式运行，挂在轮次下面、面板里：点开去运行（使用者读不了正式运行的原始记录，写明由搭建者查看） */
export function IssuedList({ data }: { data: RunLedger }) {
  const builder = useAtLeast('builder')
  const issued = data.issued ?? []
  if (!issued.length) return null
  const nameOf = (i: LedgerIssued) => {
    const c = (data.calibers ?? []).find((x) => x.workflow_id === i.workflow_id && x.version === i.version)
    const a = (data.actions ?? []).find((x): x is LedgerIssueAction => x.kind === 'issue' && x.workflow_id === i.workflow_id
      && x.version === i.version)
    return caliberOf(c ?? a ?? { version: i.version })
  }
  return (
    <ul className="space-y-1" data-ledger-issued-list="">
      {issued.map((i) => {
        const st = issuedState(i)
        return (
          <li key={i.run_id} className="flex flex-wrap items-center gap-1.5 text-2xs" data-ledger-issued={i.run_id}
              data-ledger-issued-state={st.code}>
            <StatusBadge status={st.code} size={11} decorative />
            <span className="font-medium">{LEDGER_UI.issued.title(i.auto)}</span>
            <span className="text-dim">{nameOf(i)}</span>
            <span data-ledger-tier="">{st.text}</span>
            {builder
              ? <Link to={`/runs/${i.run_id}`} className="text-[var(--accent)] hover:underline" data-ledger-issued-open="">{LEDGER_UI.issued.open}</Link>
              : <span className="text-faint">{LEDGER_UI.issued.userNote}</span>}
          </li>
        )
      })}
    </ul>
  )
}

/** 一份相关口径对应的那一项「看差多少」：同一条查询（指标的几条查询之一）、同一份口径 */
export function reconcileActionFor(data: RunLedger | undefined, queries: string[], caliber: string | undefined) {
  return (data?.actions ?? []).find((a): a is LedgerReconcileAction => a.kind === 'reconcile' && a.caliber === caliber
    && queries.includes(a.query))
}

/** 差在哪：explain_sql_diff 的条目（SQL 片段服务端已按遮罩换过）；兜底说明另写一句 */
export function DiffList({ related }: { related?: LedgerRelated }) {
  const items = (related?.diff?.items ?? []).filter((i) => i.text)
  if (!items.length && !related?.diff?.fallback) {
    return <p className="text-2xs text-faint">{related?.reason || LEDGER_UI.step.noDiff}</p>
  }
  return (
    <ul className="list-disc space-y-0.5 pl-4" data-ledger-diff="">
      {items.map((i, n) => <li key={n}>{i.text}</li>)}
      {related?.diff?.fallback && !items.length && <li className="text-faint">{LEDGER_UI.step.fallback}</li>}
    </ul>
  )
}

/** 面板里的一小节：同 EvidencePanel 的 Part */
function Section({ title, children, ...rest }: { title: string; children: ReactNode } & Record<`data-${string}`, string>) {
  return (
    <div {...rest}>
      <div className="mb-1 text-2xs font-medium text-faint">{title}</div>
      {children}
    </div>
  )
}

/**
 * 证据面板出处链最上面的「口径」一步。只给数字、取值片段（单元格、指标）：别的片段服务端本来就不标。正式运行、没有运行、
 * 不比对的运行不画这一步
 */
export function LedgerPart({ runId, runClass, report, segId, segText, segLedger, readOnly = false }: {
  runId?: string; runClass?: string; report?: string; segId: string
  /** 正文里这个数写的是什么：对账结果里对照着说原答案当时的值 */
  segText?: string
  /** 片段接口的 ledger 键（按运行取的那一份还没回来时先用它） */
  segLedger?: SegmentLedger | null
  readOnly?: boolean
}) {
  const on = ledgerEnabled(runId, runClass)
  const slot = useRunLedger(runId, on)
  const [dialog, setDialog] = useState<{ action: LedgerReconcileAction; related?: LedgerRelated } | null>(null)
  if (!on || !runId) return null
  const data = slot?.data
  const label = labelOf(data, report, segId, segLedger)
  if (!label) {
    // 还在比对、等满了、读不出：照实说；比对过了这个片段没有标签（不是数）、不比对的、第一次还没取回来的：这一步不画
    if (!slot || (slot.status === 'loading' && !data)) return null
    if (ledgerErrorShown(slot) || (slot.status !== 'error' && data?.state === 'pending')) {
      return <Section title={LEDGER_UI.step.title} data-ev-ledger="waiting"><LedgerWaiting slot={slot} runId={runId} /></Section>
    }
    if (data?.state === 'unreadable') {
      return (
        <Section title={LEDGER_UI.step.title} data-ev-ledger="unreadable">
          <p className="text-dim">{LEDGER_UI.step.unreadable(data.reason ?? '')}</p>
        </Section>
      )
    }
    return null
  }
  const queries = label.queries ?? (label.query ? [label.query] : [])
  const issue = label.kind === 'caliber' && data?.whole?.equivalent
    ? (data.actions ?? []).find((a): a is LedgerIssueAction => a.kind === 'issue' && a.workflow_id === label.workflow_id
      && a.version === label.version)
    : undefined
  const how = label.how === 'composed' ? 'composed' : 'independent'
  return (
    <Section title={LEDGER_UI.step.title} data-ev-ledger={label.kind}>
      {label.kind === 'caliber' && (
        <div className="space-y-1">
          <p className="font-medium" data-ledger-caliber="">
            {LEDGER_TEXT.kind.caliber}{caliberOf(label)}
            <span className="chip ml-1.5" data-ledger-how={how}>{LEDGER_TEXT.how[how]}</span>
          </p>
          <p className="text-2xs text-dim">{LEDGER_UI.step.how[how]}</p>
          <div className="flex flex-wrap items-center gap-1 text-2xs">
            <span className="text-faint">{LEDGER_UI.step.params}</span>
            <BindingList binding={label.binding} />
          </div>
          {label.column && <p className="text-2xs text-faint">{LEDGER_UI.step.column(label.column)}</p>}
          {issue && <IssueControl runId={runId} action={issue} slot={slot} readOnly={readOnly} />}
          {data && issue && <IssuedList data={{ ...data, issued: (data.issued ?? []).filter((i) =>
            i.workflow_id === issue.workflow_id && i.version === issue.version) }} />}
        </div>
      )}
      {label.kind === 'related' && (
        <div className="space-y-2">
          <p className="text-2xs text-dim">{LEDGER_UI.step.relatedSub}{label.reason ? `：${label.reason}` : ''}</p>
          {(label.related ?? []).map((r, i) => {
            const action = reconcileActionFor(data, queries, r.caliber)
            // 指标的相关口径只写是哪一版，差在哪在它的各条查询上
            const where = r.diff ? r : (data?.queries ?? []).filter((q) => queries.includes(q.artifact))
              .flatMap((q) => q.candidates).find((c) => c.caliber === r.caliber && c.status === 'related')
            const done = (data?.reconciled ?? []).filter((x) => x.caliber === r.caliber && !!x.query && queries.includes(x.query))
            return (
              <div key={r.caliber ?? i} className="rounded border px-2 py-1.5" data-ledger-related={r.caliber ?? ''}>
                <p className="font-medium">{LEDGER_TEXT.kind.related}{caliberOf(r)}</p>
                <div className="mt-1 text-2xs" data-ledger-where="">
                  <div className="text-faint">{LEDGER_UI.step.where}</div>
                  <DiffList related={where} />
                </div>
                {action ? (
                  <button type="button" className="btn btn-xs mt-1.5" disabled={readOnly}
                          title={readOnly ? LEDGER_UI.reconcile.readOnly : undefined}
                          onClick={() => setDialog({ action, related: where })} data-ledger-reconcile-open={r.caliber ?? ''}>
                    <Scale size={11} aria-hidden /> {LEDGER_UI.reconcile.button}
                  </button>
                ) : r.comparable === false ? <p className="mt-1 text-2xs text-faint">{r.reason || LEDGER_UI.step.notComparable}</p> : null}
                {!!done.length && <ReconcileResults runId={runId} items={done} data={data} original={segText} />}
              </div>
            )
          })}
        </div>
      )}
      {label.kind === 'reasoning' && (
        <p data-ledger-reasoning="">{LEDGER_TEXT.kind.reasoning}<span className="text-dim"> · {LEDGER_UI.step.reasoningSub}</span></p>
      )}
      {label.kind === 'unknown' && (
        <p className="text-dim" data-ledger-unknown="">{LEDGER_TEXT.kind.unknown}{label.reason ? `：${label.reason}` : ''}</p>
      )}
      {data && <LedgerFootnote data={data} />}
      {dialog && data && (
        <ReconcileDialog runId={runId} action={dialog.action} related={dialog.related} onClose={() => setDialog(null)} />
      )}
    </Section>
  )
}
