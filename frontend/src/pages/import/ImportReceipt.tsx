import type { CSSProperties, ReactNode } from 'react'
import { AlertTriangle, CheckCircle2, Info, XCircle } from 'lucide-react'
import type { CheckResult, FixProposal, ImportProblem, PriorAcceptance, TrialOut } from '../../types'
import { formatNumber } from '../../lib/format'
import { CHECK_STATUS_LABEL, LEDGER_ROLE_LABEL, PROBLEM_CATEGORY_LABEL, RECEIPT_BLOCK_TEXT, RECIPE_TEXT } from '../../lib/terms'
import { FixButtons } from './FixPanel'
import { RowsDropped } from './ReceiptBlocks'
import { roleStyle } from './SheetGrid'
import { CellChips } from './SuggestionCards'

// ===========================================================================
// 试运行回执：格子账、表与行数、统计期、核对、说明预览、问题。全部来自服务端的试运行结果，
// 界面不做任何算术判断（核对在 SQL 里做完了），只把状态分清楚：通过 / 不一致 / 无法核对 / 说明
// ===========================================================================

/** 核对状态的样式：四种一眼分得开（文字之外还有颜色和图标，色弱也能靠图标分） */
const STATUS_STYLE: Record<string, { color: string; icon: typeof CheckCircle2 }> = {
  passed: { color: 'var(--ok)', icon: CheckCircle2 },
  mismatch: { color: 'var(--err)', icon: XCircle },
  unverifiable: { color: 'var(--warn)', icon: AlertTriangle },
  info: { color: 'var(--text-faint)', icon: Info },
}

export function statusChipStyle(status: string): CSSProperties {
  const color = STATUS_STYLE[status]?.color ?? 'var(--text-faint)'
  return { color, borderColor: `color-mix(in srgb, ${color} 55%, transparent)`, background: `color-mix(in srgb, ${color} 10%, transparent)` }
}

export function CheckStatusChip({ status }: { status: string }) {
  const Icon = STATUS_STYLE[status]?.icon ?? Info
  return (
    <span className="chip shrink-0" style={statusChipStyle(status)} data-check-status={status}>
      <Icon size={10} aria-hidden /> {CHECK_STATUS_LABEL[status] ?? status}
    </span>
  )
}

/**
 * 同一份构建以前被接受过的理由（第 5 节）：只读显示在可接受的核对旁，理由框不预填——接受是对这一期数据的判断，
 * 预填会让人不看就点
 */
export function PriorAcceptances({ items }: { items?: PriorAcceptance[] }) {
  if (!items?.length) return null
  return (
    <ul className="space-y-0.5 text-2xs leading-relaxed text-faint" data-prior-acceptances>
      {items.map((p, i) => (
        <li key={`${p.check_id}-${i}`} data-prior-acceptance={p.check_id}>
          {RECIPE_TEXT.priorAcceptance(p.reason, formatNumber(p.import_seq), p.signed_by || RECIPE_TEXT.priorUnsigned)}
        </li>
      ))}
    </ul>
  )
}

function CheckRow({ c, onFocus, prior }: { c: CheckResult; onFocus: (cell: string) => void; prior?: PriorAcceptance[] }) {
  const failedLine = c.status !== 'passed' && c.status !== 'info' && c.category !== 'info' ? PROBLEM_CATEGORY_LABEL[c.category] : ''
  // 「说明」类（如口径不同的 R2）的 failed 是「两边不相等的个数」，正是预期的结果，不是核对不一致：
  // 不写计数，只看服务端给的细节（「31 天中 0 天相等」）
  const counts = !!c.checked && c.status !== 'info'
  return (
    <li className="space-y-1 rounded-md border px-2.5 py-2" data-check={c.id} data-status={c.status}
        style={c.status === 'mismatch' ? { borderColor: 'color-mix(in srgb, var(--err) 40%, var(--border))' } : undefined}>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <CheckStatusChip status={c.status} />
        <span className="mono text-faint">{c.id}</span>
        <span className="min-w-0 flex-1">{c.title}</span>
        {counts && (
          <span className="tnum text-2xs text-faint" data-check-counts>
            {formatNumber(c.checked)}
            {c.failed ? ` / ${CHECK_STATUS_LABEL.mismatch} ${formatNumber(c.failed)}` : ''}
            {c.unverifiable ? ` / ${CHECK_STATUS_LABEL.unverifiable} ${formatNumber(c.unverifiable)}` : ''}
          </span>
        )}
      </div>
      {failedLine && <div className="text-2xs text-faint">{failedLine}</div>}
      {!!c.details?.length && (
        <ul className="space-y-0.5 text-2xs leading-relaxed text-dim">
          {c.details.slice(0, 6).map((d, i) => <li key={i}>{d}</li>)}
        </ul>
      )}
      <CellChips cells={c.cells} onFocus={onFocus} />
      {c.acceptable && <PriorAcceptances items={prior} />}
    </li>
  )
}

/** 修复提议按 id 查：问题对象上的 fix_ids 指向它 */
export const fixMap = (fixes?: FixProposal[] | null) => new Map((fixes ?? []).map((f) => [f.id, f]))

/**
 * 某张改了名的工作表旁该放哪几个「更新工作表名」：提议的 anchor.sheet 就是本期的工作表名（契约 FixAnchor），
 * 按它对上，同一次有几张表改名时各放各的。没写 sheet 的提议对不上具体哪张，哪里都放；本期表名不知道时
 * 只放这种，不把别的表的按钮挂过来
 */
export const renameFixIds = (fixes: FixProposal[] | null | undefined, sheet: string | null | undefined) => (fixes ?? [])
  .filter((f) => f.anchor?.kind === 'sheet_renamed' && (!f.anchor.sheet || f.anchor.sheet === sheet)).map((f) => f.id)

/** 问题列表：带类别和坐标，点坐标网格滚到那格；有修复提议的问题旁放修复按钮（只按 fix_ids 放） */
export function ProblemList({ problems, onFocus, fixes, onFix }: {
  problems: ImportProblem[]; onFocus: (cell: string) => void
  fixes?: Map<string, FixProposal>; onFix?: (id: string) => void
}) {
  if (!problems.length) return null
  return (
    <ul className="space-y-1.5" data-problems>
      {problems.map((p, i) => {
        const tone = p.category === 'structure' || p.category === 'recipe' ? 'var(--err)'
          : p.category === 'data_quality' || p.category === 'input' ? 'var(--warn)' : 'var(--text-faint)'
        return (
          <li key={i} className="space-y-1 rounded-md border px-2.5 py-2 text-xs" data-problem={p.code} data-category={p.category}
              style={{ borderColor: `color-mix(in srgb, ${tone} 40%, var(--border))` }}>
            <div className="text-2xs" style={{ color: tone }}>{PROBLEM_CATEGORY_LABEL[p.category] ?? p.category}</div>
            <div className="leading-relaxed">{p.message}</div>
            <div className="flex flex-wrap items-center gap-1.5">
              <CellChips cells={p.cells} onFocus={onFocus} max={10} />
              <FixButtons ids={p.fix_ids} fixes={fixes} onFix={onFix} />
            </div>
          </li>
        )
      })}
    </ul>
  )
}

const STATUS_LINE: Record<string, string> = {
  passed: RECIPE_TEXT.statusPassed,
  needs_decision: RECIPE_TEXT.statusNeedsDecision,
  needs_input: RECIPE_TEXT.statusNeedsInput,
  rejected: RECIPE_TEXT.statusRejected,
}

export function TrialStatusLine({ status }: { status: string }) {
  const color = status === 'passed' ? 'var(--ok)' : status === 'rejected' ? 'var(--err)' : 'var(--warn)'
  return (
    <div className="rounded-lg border px-3 py-2 text-xs font-medium" data-trial-status={status} role="status"
         style={{ color, borderColor: `color-mix(in srgb, ${color} 45%, var(--border))`, background: `color-mix(in srgb, ${color} 7%, transparent)` }}>
      {STATUS_LINE[status] ?? status}
    </div>
  )
}

function Section({ title, children, attr }: { title: string; children: ReactNode; attr?: Record<string, string> }) {
  return (
    <section className="space-y-1.5" {...attr}>
      <h4 className="text-xs font-semibold">{title}</h4>
      {children}
    </section>
  )
}

/**
 * 格子账能下哪些结论。执行器对找不到的工作表（sheet_missing）不记账，所以账是空的或缺了工作表时，
 * 「全部有去处」「两遍读取一致」都无从说起；各去向加起来和读到的个数对不上，也不能说「全部有去处」
 */
export function ledgerVerdict(trial: TrialOut, expectedSheets?: number) {
  const ledger = trial.receipt?.ledger ?? []
  const missingProblems = (trial.problems ?? []).filter((p) => p.code === 'sheet_missing').length
  const missing = Math.max(missingProblems, expectedSheets != null ? expectedSheets - ledger.length : 0)
  const total = ledger.reduce((n, s) => n + (s.nonempty_read ?? 0), 0)
  const unclaimed = ledger.reduce((n, s) => n + (s.unclaimed ?? 0), 0)
  const assigned = ledger.reduce((n, s) => n + Object.values(s.roles ?? {}).reduce((a, b) => a + b, 0), 0)
  const balanced = ledger.every((s) => Object.values(s.roles ?? {}).reduce((a, b) => a + b, 0) + (s.unclaimed ?? 0) === s.nonempty_read)
  const twoPass = ledger.every((s) => s.nonempty_scan === s.nonempty_read)
  const state: 'none' | 'partial' | 'complete' = !ledger.length ? 'none' : missing > 0 ? 'partial' : 'complete'
  return { ledger, state, missing, total, unclaimed, assigned, balanced, twoPass }
}

export function ImportReceipt({ trial, expectedSheets, onFocusCell, fixes, onFix }: {
  trial: TrialOut
  /** 配方里有几张工作表：账里少了的就是没读到的 */
  expectedSheets?: number
  onFocusCell: (cell: string) => void
  /** 修复提议（StagingOut.fixes）：问题旁、工作表改名说明旁放修复按钮 */
  fixes?: FixProposal[]
  onFix?: (id: string) => void
}) {
  const r = trial.receipt ?? {}
  const { ledger, state, missing, total, unclaimed, assigned, balanced, twoPass } = ledgerVerdict(trial, expectedSheets)
  // 同一个说法（合计格、合计标签）的去向合在一起数
  const roles = new Map<string, { label: string; role: string; n: number }>()
  for (const s of ledger) {
    for (const [role, n] of Object.entries(s.roles ?? {})) {
      const label = LEDGER_ROLE_LABEL[role] ?? role
      const cur = roles.get(label)
      roles.set(label, { label, role: cur?.role ?? role, n: (cur?.n ?? 0) + n })
    }
  }
  const scanTotal = ledger.reduce((n, s) => n + (s.nonempty_scan ?? 0), 0)
  const period = r.period
  const notes = Object.entries(trial.notes ?? {})
  const placeholders = Object.entries(r.placeholders ?? {})
  const outside = r.outside_text ?? []
  // 拒收时执行器在结构问题上就停了（表可能只写了一半），需要录入时根本没写库：行数不能当真
  const written = trial.status === 'passed' || trial.status === 'needs_decision'
  const byId = fixMap(fixes)
  // 按 fallback 认出的改名（配方里的名字 → 本期的名字）：「更新工作表名」的提议挂在这里（anchor.kind=sheet_renamed）
  const renamed = Object.entries((r.sheets?.renamed ?? {}) as Record<string, string>)
  const excluded = r.rows_excluded ?? []
  const prior = (id: string) => (trial.prior_acceptances ?? []).filter((p) => p.check_id === id)

  return (
    <div className="space-y-3" data-trial-receipt data-trial-id={trial.trial_id}>
      <Section title={RECIPE_TEXT.receipt}>
        <div className="space-y-1.5 rounded-lg border bg-bg px-3 py-2 text-xs" data-ledger data-ledger-state={state}>
          {state === 'none' ? (
            <div className="text-[var(--err)]">{RECIPE_TEXT.ledgerNone}</div>
          ) : (
            <div>
              {RECIPE_TEXT.ledger(formatNumber(total))}
              {unclaimed
                ? <>，<span className="text-[var(--err)]">{RECIPE_TEXT.ledgerUnclaimed(formatNumber(unclaimed))}</span></>
                : !balanced
                  ? <>，<span className="text-[var(--err)]">{RECIPE_TEXT.ledgerUnbalanced(formatNumber(assigned), formatNumber(total))}</span></>
                  : state === 'complete' ? `，${RECIPE_TEXT.ledgerAll}` : ''}
            </div>
          )}
          {state === 'partial' && (
            <div className="text-[var(--err)]" data-ledger-missing={missing}>{RECIPE_TEXT.ledgerSheetsMissing(formatNumber(missing))}</div>
          )}
          <div className="flex flex-wrap gap-1">
            {[...roles.values()].map((x) => (
              <span key={x.label} className="chip" data-ledger-role={x.role}>
                <span className="inline-block h-2 w-2 rounded-sm" style={roleStyle(x.role)} aria-hidden />
                {x.label} <span className="tnum">{formatNumber(x.n)}</span>
              </span>
            ))}
          </div>
          {/* 缺了工作表时，「两遍读取一致」只对读到的那几张成立，不写；不一致照写 */}
          {!twoPass ? (
            <div className="text-[var(--err)]" data-two-pass="differ">{RECIPE_TEXT.passDiffer(formatNumber(scanTotal), formatNumber(total))}</div>
          ) : state === 'complete' && (
            <div className="text-faint" data-two-pass="agree">{RECIPE_TEXT.passAgree}</div>
          )}
        </div>
      </Section>

      {!!r.tables?.length && (
        <Section title={RECIPE_TEXT.tables} attr={{ 'data-receipt-tables': written ? '' : 'unwritten' }}>
          {!written && <p className="text-2xs text-faint" data-tables-unwritten>{RECIPE_TEXT.tablesUnwritten}</p>}
          <ul className="space-y-1">
            {r.tables.map((t) => (
              <li key={t.name} className="rounded-md border px-2.5 py-1.5 text-xs" data-receipt-table={t.name}
                  data-rows={written ? t.rows : undefined}>
                <div className="flex flex-wrap items-baseline gap-x-2">
                  <span className="font-medium">{t.name}</span>
                  {written
                    ? <span className="tnum text-dim">{RECIPE_TEXT.rows(formatNumber(t.rows))}</span>
                    : <span className="text-faint">{RECIPE_TEXT.rowsUnwritten}</span>}
                </div>
                <div className="mt-0.5 flex flex-wrap gap-1 text-2xs text-faint">
                  {t.columns.map((c) => (
                    <span key={c.name} className="chip" title={c.header ?? undefined}>
                      {c.name}{c.unit ? `（${c.unit}）` : ''}
                    </span>
                  ))}
                </div>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {period && (
        <p className="text-xs text-dim" data-receipt-period>
          {RECIPE_TEXT.periodLine(period.start, period.end)}
          {period.source === 'human'
            ? `，${RECIPE_TEXT.periodHuman(period.signed_by || RECIPE_TEXT.signNote)}`
            : period.cells?.length ? `，${RECIPE_TEXT.periodFromCells(period.cells.map((c) => c.slice(c.lastIndexOf('!') + 1)).join('、'))}` : ''}
        </p>
      )}

      {renamed.length > 0 && (
        <Section title={RECIPE_TEXT.sheetRenamed} attr={{ 'data-sheet-renamed': '' }}>
          <ul className="space-y-1">
            {renamed.map(([from, to]) => (
              <li key={from} className="flex flex-wrap items-center gap-2 rounded-md border px-2.5 py-1.5 text-xs" data-sheet-renamed-item={to}>
                <span className="min-w-0 flex-1">{RECIPE_TEXT.sheetRenamedLine(from, to)}</span>
                <FixButtons ids={renameFixIds(fixes, to)} fixes={byId} onFix={onFix} />
              </li>
            ))}
          </ul>
        </Section>
      )}

      {placeholders.length > 0 && (
        <p className="text-xs text-dim" data-receipt-placeholders>
          {RECIPE_TEXT.placeholderCounts}：{placeholders.map(([text, n]) => RECIPE_TEXT.placeholderCount(text, formatNumber(n))).join('，')}
        </p>
      )}

      {!!trial.problems?.length && (
        <Section title={RECIPE_TEXT.problems}>
          <ProblemList problems={trial.problems} onFocus={onFocusCell} fixes={byId} onFix={onFix} />
        </Section>
      )}

      {!!trial.checks?.length && (
        <Section title={RECIPE_TEXT.checks}>
          <ul className="space-y-1.5" data-checks>
            {trial.checks.map((c) => <CheckRow key={c.id} c={c} onFocus={onFocusCell} prior={prior(c.id)} />)}
          </ul>
        </Section>
      )}

      {excluded.length > 0 && (
        <Section title={RECEIPT_BLOCK_TEXT.excludedTitle}>
          <RowsDropped rows={excluded} onFocus={onFocusCell} />
        </Section>
      )}

      {outside.length > 0 && (
        <Section title={RECIPE_TEXT.outsideText}>
          <ul className="space-y-1 text-xs" data-outside-texts>
            {outside.map((o) => {
              // cell 本来就带工作表名（「客流汇总!B3」，同问题的 cells）；旧数据是纯 A1 时才补上 sheet
              const ref = o.cell.includes('!') ? o.cell : `${o.sheet}!${o.cell}`
              return (
                <li key={ref} className="flex items-baseline gap-2" data-outside-text={ref}>
                  <CellChips cells={[ref]} onFocus={onFocusCell} />
                  <span className="min-w-0 flex-1 break-all text-dim">{o.text}</span>
                </li>
              )
            })}
          </ul>
        </Section>
      )}

      {notes.length > 0 && (
        <Section title={RECIPE_TEXT.notes}>
          <ul className="space-y-1.5" data-notes>
            {notes.map(([table, n]) => (
              <li key={table} className="rounded-md border px-2.5 py-1.5 text-xs" data-note={table}>
                <div className="font-medium">{table}</div>
                <p className="mt-0.5 leading-relaxed text-dim">{n.comment}</p>
                {Object.entries(n.columns ?? {}).map(([col, text]) => (
                  <div key={col} className="text-2xs text-faint">{col}：{text}</div>
                ))}
              </li>
            ))}
          </ul>
        </Section>
      )}
    </div>
  )
}
