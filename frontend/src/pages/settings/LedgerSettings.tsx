import { Field, SectionBar } from '../../components/ui'
import { LEDGER_UI } from '../../lib/termsLedger'

/*
 * 设置页「偏好设置」里「口径账本」一节（服务端 api/settings.py 的 ledger 一组，docs/design/next-round/02-口径账本.md
 * 做法 5、7）：自动按口径正式出具的开关、每日上限、比对时限、口径升版后每次最多重算几组。
 *
 * 照 VerifySettings.tsx 的写法：草稿由 PrefsTab 持有、和其余设置共用一条保存条（这一节只画、只报改动），存的时候整组写
 * ledger——服务端对没给的项回落到默认值，只发改过的那一项会把别的项冲回默认。字段名就是服务端 LEDGER_SETTING_LABEL 的
 * 叫法，422 的报错写的也是它。范围同服务端（ledger_problem）：每日上限 0 到 100,000、重算组数 0 到 1,000、时限 1 到 60 秒。
 */

const S = LEDGER_UI.settings

/** 范围：同服务端 settings.APPS_DAILY_LIMIT_MAX、LEDGER_RECALL_MAX、LEDGER_TIMEOUT_RANGE */
const DAILY_MAX = 100_000
const RECALL_MAX = 1000
const TIMEOUT_RANGE = [1, 60] as const

export interface LedgerDraft {
  auto_issue: boolean
  issue_daily_limit: string
  match_timeout_s: string
  recall_limit: string
}
export type LedgerKey = keyof LedgerDraft

const LABEL: Record<LedgerKey, string> = {
  auto_issue: S.autoIssue, issue_daily_limit: S.dailyLimit, match_timeout_s: S.timeout, recall_limit: S.recallLimit,
}

const isRecord = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v)
const text = (v: unknown, fallback: string) => (typeof v === 'number' && Number.isFinite(v) ? String(v) : fallback)

/** GET /settings 的 ledger 一组（服务端给的是生效的样子，没存过的补了默认）→ 草稿 */
export function ledgerDraftOf(group: unknown): LedgerDraft {
  const g = isRecord(group) ? group : {}
  return {
    auto_issue: g.auto_issue !== false,
    issue_daily_limit: text(g.issue_daily_limit, ''),
    match_timeout_s: text(g.match_timeout_s, ''),
    recall_limit: text(g.recall_limit, ''),
  }
}

export function ledgerChanged(cur: LedgerDraft, saved: LedgerDraft): LedgerKey[] {
  return (Object.keys(saved) as LedgerKey[]).filter((k) => cur[k] !== saved[k])
}

/** 保存条上这几项的叫法：和字段名、服务端报错同一套 */
export const ledgerLabels = (keys: LedgerKey[]): string[] => keys.map((k) => LABEL[k])

const intIn = (v: string, lo: number, hi: number) => /^\d+$/.test(v.trim()) && Number(v) >= lo && Number(v) <= hi

/** 各项的问题。只校验改过的（存着的值不合规不该挡住别的设置保存） */
export function ledgerErrors(cur: LedgerDraft, saved: LedgerDraft): Partial<Record<LedgerKey, string>> {
  const out: Partial<Record<LedgerKey, string>> = {}
  for (const k of ledgerChanged(cur, saved)) {
    if (k === 'issue_daily_limit' && !intIn(cur[k], 0, DAILY_MAX)) out[k] = S.invalid.limit(DAILY_MAX)
    if (k === 'recall_limit' && !intIn(cur[k], 0, RECALL_MAX)) out[k] = S.invalid.limit(RECALL_MAX)
    if (k === 'match_timeout_s') {
      const n = Number(cur[k].trim())
      if (!(cur[k].trim() !== '' && Number.isFinite(n) && n >= TIMEOUT_RANGE[0] && n <= TIMEOUT_RANGE[1])) {
        out[k] = S.invalid.timeout(TIMEOUT_RANGE[0], TIMEOUT_RANGE[1])
      }
    }
  }
  return out
}

/** 草稿 → 存进去的整组 ledger */
export function ledgerPayload(cur: LedgerDraft): Record<string, unknown> {
  return {
    auto_issue: cur.auto_issue,
    issue_daily_limit: Number(cur.issue_daily_limit.trim()),
    match_timeout_s: Number(cur.match_timeout_s.trim()),
    recall_limit: Number(cur.recall_limit.trim()),
  }
}

const id = (k: LedgerKey) => `pref-ledger-${k.replace(/_/g, '-')}`

export function LedgerSection({ cur, errors, onEdit }: {
  cur: LedgerDraft
  errors: Partial<Record<LedgerKey, string>>
  onEdit: (patch: Partial<LedgerDraft>) => void
}) {
  const num = (k: Exclude<LedgerKey, 'auto_issue'>, hint: string, mode: 'numeric' | 'decimal' = 'numeric') => (
    <Field label={LABEL[k]} htmlFor={id(k)} error={errors[k]} hint={hint}>
      {(p) => (
        <input {...p} className="field tnum max-w-40" inputMode={mode} value={cur[k]} data-ledger-setting={k}
               onChange={(e) => onEdit({ [k]: e.target.value } as Partial<LedgerDraft>)} />
      )}
    </Field>
  )
  return (
    <section data-ledger-settings="">
      <SectionBar title={S.title} hint={S.hint} />
      <label className="flex items-start gap-2 text-xs">
        <input type="checkbox" className="mt-0.5" checked={cur.auto_issue} data-ledger-setting="auto_issue"
               aria-describedby="pref-ledger-auto-hint" onChange={(e) => onEdit({ auto_issue: e.target.checked })} />
        <span>
          {S.autoIssue}
          <span id="pref-ledger-auto-hint" className="mt-0.5 block text-2xs leading-relaxed text-faint">{S.autoIssueHint}</span>
        </span>
      </label>
      <div className="mt-3 grid grid-cols-2 gap-3 max-sm:grid-cols-1">
        {num('issue_daily_limit', S.dailyLimitHint)}
        {num('match_timeout_s', S.timeoutHint, 'decimal')}
        {num('recall_limit', S.recallLimitHint)}
      </div>
    </section>
  )
}
