import { useMemo, useState } from 'react'
import { BookMarked, Scale } from 'lucide-react'
import { LEDGER_TEXT } from '../lib/terms'
import { LEDGER_UI } from '../lib/termsLedger'
import { useRunLedger } from '../store/ledger'
import type { LedgerIssueAction, LedgerReconcileAction, LedgerRelated, RunLedger } from '../typesLedger'
import { IssueControl, IssuedList, LedgerFootnote, LedgerWaiting, caliberOf, ledgerEnabled, ledgerErrorShown } from './LedgerStep'
import { ReconcileDialog, ReconcileResults } from './LedgerReconcile'

/**
 * 问数据轮次底部的口径账本（docs/design/next-round/02-口径账本.md 做法 11）：一行摘要（「3 个数来自口径「周销售」v3，
 * 1 个数是这次的推理」）和对应的动作——整题等价时按口径正式出具（能自动的写正在自动出具），有相关口径时对账；
 * 正式出具的结果（自动的、一键的）挂在轮次下面、点开去运行；对过的账一张结果卡片，三个数各自点得开。
 *
 * 比对在封存之后做：轮次刚交付时写「正在比对口径」，store/ledger 按退避再取，等满了还没结论照实说、给「再取一次」。
 * 不靠实时流（ledger.matched 追加时这次运行的事件总线已经关了）。界面层不放逻辑：每个数的标签、整题判断、能不能出具、
 * 能不能自动、不能的原因都是服务端给的；摘要只是把主报告里服务端标好的数按标签数一数、写出是哪份口径。
 */

/** 摘要的几个数：主报告里服务端标好的标签按种类数，来自口径的写出是哪几份口径（按出现的先后） */
function summaryOf(data: RunLedger) {
  const labels = Object.values((data.primary ? data.reports?.[data.primary]?.labels : undefined) ?? {})
  const counts = { caliber: 0, related: 0, reasoning: 0, unknown: 0 }
  const calibers: string[] = []
  for (const l of labels) {
    counts[l.kind] += 1
    if (l.kind === 'caliber') {
      const name = caliberOf(l)
      if (!calibers.includes(name)) calibers.push(name)
    }
  }
  return { ...counts, calibers, total: labels.length }
}

/** 对账的入口：每条查询、每份相关口径一项；差在哪取那条查询上的比对 */
function reconcileEntries(data: RunLedger): { action: LedgerReconcileAction; related?: LedgerRelated }[] {
  return (data.actions ?? []).filter((a): a is LedgerReconcileAction => a.kind === 'reconcile').map((action) => ({
    action,
    related: (data.queries ?? []).find((q) => q.artifact === action.query)?.candidates
      .find((c) => c.caliber === action.caliber && c.status === 'related'),
  }))
}

export function LedgerTurn({ runId, runClass, done, readOnly = false }: {
  runId: string | undefined
  runClass?: string
  /** 这一轮交付了（运行成功结束）：之前不取，运行没封存、没比对 */
  done: boolean
  /** 对话在回收站里：只看，不出具、不对账 */
  readOnly?: boolean
}) {
  const on = done && ledgerEnabled(runId, runClass)
  const slot = useRunLedger(runId, on)
  const [dialog, setDialog] = useState<{ action: LedgerReconcileAction; related?: LedgerRelated } | null>(null)
  const data = slot?.data
  const summary = useMemo(() => (data?.state === 'matched' ? summaryOf(data) : null), [data])
  if (!on || !runId || !slot) return null
  if (ledgerErrorShown(slot) || (slot.status !== 'error' && data?.state === 'pending')) {
    return (
      <div className="w-full basis-full text-2xs" data-ledger-turn="waiting">
        <LedgerWaiting slot={slot} runId={runId} compact />
      </div>
    )
  }
  if (!data || data.state !== 'matched' || !summary) return null
  const issue = (data.actions ?? []).find((a): a is LedgerIssueAction => a.kind === 'issue')
  const reconciles = reconcileEntries(data)
  const issued = data.issued ?? []
  const reconciled = data.reconciled ?? []
  if (!summary.total && !issued.length && !reconciled.length) return null
  const whole = data.whole
  return (
    <div className="w-full basis-full space-y-1.5 rounded-md border px-2.5 py-2 text-xs" data-ledger-turn="matched">
      {summary.total > 0 && (
        <p className="flex flex-wrap items-center gap-1.5" data-ledger-summary="">
          <BookMarked size={12} className="shrink-0 text-faint" aria-hidden />
          <span className="text-2xs font-medium text-faint">{LEDGER_UI.turn.label}</span>
          <span>{LEDGER_UI.turn.summary(summary)}</span>
        </p>
      )}
      {whole?.equivalent && (
        <p className="text-2xs text-dim" data-ledger-whole="">
          {LEDGER_UI.turn.whole(caliberOf(whole), LEDGER_TEXT.how[whole.how === 'composed' ? 'composed' : 'independent'])}
        </p>
      )}
      {(issue || reconciles.length > 0) && (
        <div className="flex flex-wrap items-start gap-1.5" data-ledger-actions="">
          {issue && <IssueControl runId={runId} action={issue} slot={slot} readOnly={readOnly} />}
          {reconciles.map(({ action, related }) => (
            <button key={`${action.query}|${action.caliber}`} type="button" className="btn btn-xs" disabled={readOnly}
                    title={readOnly ? LEDGER_UI.reconcile.readOnly : undefined}
                    onClick={() => setDialog({ action, related })} data-ledger-turn-reconcile={action.caliber}>
              <Scale size={11} aria-hidden /> {LEDGER_UI.reconcile.turnButton(caliberOf(action))}
            </button>
          ))}
        </div>
      )}
      <IssuedList data={data} />
      {reconciled.length > 0 && <ReconcileResults runId={runId} items={reconciled} data={data} />}
      <LedgerFootnote data={data} />
      {dialog && <ReconcileDialog runId={runId} action={dialog.action} related={dialog.related} onClose={() => setDialog(null)} />}
    </div>
  )
}
