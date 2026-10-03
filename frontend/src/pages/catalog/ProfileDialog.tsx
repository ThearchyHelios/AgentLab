import { useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { ArrowDown, ArrowUp, ArrowUpRight, CircleCheck, Equal, Minus, OctagonX, ScanSearch, ShieldAlert, TriangleAlert, UserCheck } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import clsx from 'clsx'
import type {
  CatalogProfileCodesFinding, CatalogProfileDateFinding, CatalogProfileOut, CatalogProfileRelationFinding, CatalogProfileSettings,
  CatalogProfileSize, CatalogProfileSkip, CatalogProfileTable, CatalogTableRow,
} from '../../types'
import { ErrorState, Modal, Notice, Spinner, useTicker } from '../../components/ui'
import {
  CATALOG_CARDINALITY_LABEL, CATALOG_TEXT as CT, CATALOG_UI_TEXT as UT, PROFILE_SKIP_KIND_LABEL, PROFILE_SKIP_REASON_LABEL, PROFILE_STOP_LABEL, PROFILE_STOP_NEXT, PROFILE_TEXT as PT,
} from '../../lib/terms'
import { STATUS_TONE, StatusChip } from './parts'
import { PROFILE_DEFAULT_TABLES, PROFILE_MAX_TABLES, coverageText, fillKey, secondsText } from './profile'
import type { ProfileBlock } from './profile'

// ===========================================================================
// 数据目录页的「数据剖析」。接口是同步的：一次请求做完整个剖析才返回，服务端不支持中途停止，所以这里不给「停止」，
// 进行中只写已用时长和总时长上限，并明说无法中途停止。关掉窗口剖析照常进行（状态在页面上，页头有进度入口），
// 完成后弹提示、可以再打开报告。
//
// 四段：选范围、看预算、确认开始 → 进行中 → 报告（用了几条查询、为什么停、每张表的发现和跳过的项）；
// 开始不了（409）时写明是哪一种情况和下一步，未开启的就地打开剖析设置。
// ===========================================================================

/** 一次剖析的状态。放在页面上而不是弹窗里：关掉弹窗剖析照常进行，完成后还能再打开报告 */
export type ProfileJob =
  | { phase: 'running'; startedAt: number; tables: string[] | null; settings: CatalogProfileSettings }
  /** filled：从报告去填了含义的码值列（fillKey）→ 保存后还剩几个含义待填写，报告里按它改写那一列 */
  | { phase: 'done'; report: CatalogProfileOut; ms: number; filled?: Record<string, number> }
  | { phase: 'blocked'; kind: ProfileBlock; message: string; tables: string[] | null }
  | { phase: 'failed'; error: unknown; tables: string[] | null }

type Scope = 'selected' | 'filtered' | 'default'

/** 报告里每类最多逐条列几项，其余合成一句 */
const SKIP_SHOWN = 6
const CODES_SHOWN = 12

export function ProfileDialog({
  sourceName, settings, rows, visible, selected, filtered, job, returnTo, onStart, onClose, onSettings, onGoSource, onOpenTable, onFillCodes,
  onReset,
}: {
  sourceName: string
  /** 这个数据源此刻的剖析设置（数据源列表里读的） */
  settings: CatalogProfileSettings
  rows: CatalogTableRow[]
  visible: CatalogTableRow[]
  selected: string[]
  filtered: boolean
  /** null：还没开始（选范围） */
  job: ProfileJob | null
  /** 填完一列码值的含义、回到报告：焦点落在下一列还有待填写的「填写含义」上 */
  returnTo?: { table: string; column: string } | null
  /** tables 为 null 时由服务端挑表 */
  onStart: (tables: string[] | null) => void
  onClose: () => void
  /** 打开这个数据源的剖析设置 */
  onSettings: () => void
  /** 去数据源卡片（停用、没有表结构、快照被改动时） */
  onGoSource: () => void
  onOpenTable: (table: string) => void
  onFillCodes: (table: string, column: string) => void
  /** 回到选范围 */
  onReset: () => void
}) {
  const live = useMemo(() => rows.filter((r) => r.in_schema).map((r) => r.table_name), [rows])
  const scopes = useMemo(() => {
    const liveSet = new Set(live)
    const out: { key: Scope; label: string; hint?: string; tables: string[] | null }[] = []
    const sel = selected.filter((t) => liveSet.has(t))
    if (sel.length) out.push({ key: 'selected', label: PT.scopeSelected(sel.length), tables: sel })
    const vis = visible.filter((r) => r.in_schema).map((r) => r.table_name)
    if (filtered && vis.length && vis.length !== live.length) out.push({ key: 'filtered', label: PT.scopeFiltered(vis.length), tables: vis })
    out.push({ key: 'default', label: PT.scopeDefault, hint: PT.scopeDefaultHint(PROFILE_DEFAULT_TABLES), tables: null })
    return out
  }, [live, selected, visible, filtered])
  const [scope, setScope] = useState<Scope>(() => scopes[0].key)
  const picked = scopes.find((s) => s.key === scope) ?? scopes[0]
  const tooMany = (picked.tables?.length ?? 0) > PROFILE_MAX_TABLES
  const running = job?.phase === 'running'
  useTicker(1000, running)

  let body: ReactNode
  let footer: ReactNode
  if (!job) {
    body = (
      <>
        <p className="text-xs leading-relaxed text-dim">{PT.intro}</p>
        {!settings.enabled && (
          <Notice tone="warn" attr={{ 'data-profile-disabled': '' }}>
            <p className="font-medium">{PT.disabledTitle}</p>
            <p className="mt-0.5 text-dim">{PT.disabledBody}</p>
            <button type="button" className="btn btn-sm mt-2" onClick={onSettings} data-profile-enable="">{PT.enable}</button>
          </Notice>
        )}
        <fieldset>
          <legend className="label">{PT.scope}</legend>
          <div className="space-y-1.5" role="radiogroup" aria-label={PT.scope}>
            {scopes.map((s) => {
              const over = (s.tables?.length ?? 0) > PROFILE_MAX_TABLES
              return (
                <label key={s.key} className={clsx('flex cursor-pointer items-start gap-2 rounded-md border px-3 py-2 text-xs',
                  scope === s.key ? 'border-[var(--accent)] bg-accent-soft' : 'hover:bg-hover')} data-profile-scope={s.key}>
                  <input type="radio" className="mt-0.5" name="catalog-profile-scope" checked={scope === s.key} onChange={() => setScope(s.key)} />
                  <span className="min-w-0">
                    <span>{s.label}</span>
                    {(s.hint || over) && (
                      <span className={clsx('mt-0.5 block text-2xs', over ? 'text-[var(--err)]' : 'text-faint')}>
                        {over ? PT.scopeTooMany(PROFILE_MAX_TABLES) : s.hint}
                      </span>
                    )}
                  </span>
                </label>
              )
            })}
          </div>
        </fieldset>
        {settings.enabled && <Budget settings={settings} sourceName={sourceName} onSettings={onSettings} />}
        <p className="flex items-start gap-1.5 text-2xs leading-relaxed text-faint">
          <ShieldAlert size={11} className="mt-px shrink-0" aria-hidden /> {PT.noCancel}
        </p>
      </>
    )
    footer = (
      <>
        <button type="button" className="btn" onClick={onClose}>{CT.cancel}</button>
        <button type="button" className="btn btn-primary" disabled={!settings.enabled || tooMany} onClick={() => onStart(picked.tables)}
                data-profile-start="">
          <ScanSearch size={12} aria-hidden /> {PT.start}
        </button>
      </>
    )
  } else if (job.phase === 'running') {
    const used = Date.now() - job.startedAt
    const max = job.settings.max_total_s * 1000
    body = (
      <div className="space-y-3" aria-live="polite">
        <div className="flex items-center gap-2 text-xs">
          <Spinner size={13} />
          <span className="font-medium" data-profile-title="">{PT.running}</span>
          <span className="flex-1" />
          <span className="tnum text-2xs text-faint" data-profile-elapsed={Math.floor(used / 1000)}>
            {PT.elapsed(secondsText(used), secondsText(max))}
          </span>
        </div>
        {/* 不是进度：服务端不报进度，这条只量已用时长占总时长上限的多少，剖析最晚在上限处停下 */}
        <div className="h-1 overflow-hidden rounded-full bg-hover" role="meter" aria-label={PT.elapsed(secondsText(used), secondsText(max))}
             aria-valuemin={0} aria-valuemax={job.settings.max_total_s} aria-valuenow={Math.min(job.settings.max_total_s, Math.floor(used / 1000))}>
          <div className="h-full rounded-full bg-[var(--border-strong)] transition-[width] duration-1000 ease-linear"
               style={{ width: `${Math.min(100, (used / max) * 100)}%` }} />
        </div>
        <Notice tone="info" attr={{ 'data-profile-no-cancel': '' }}>{PT.runningHint}</Notice>
        <p className="text-2xs leading-relaxed text-faint">{PT.leavePage}</p>
      </div>
    )
    footer = <button type="button" className="btn" onClick={onClose} data-autofocus data-profile-background="">{PT.background}</button>
  } else if (job.phase === 'done') {
    body = <Report report={job.report} ms={job.ms} filled={job.filled ?? {}} returnTo={returnTo ?? null} onSettings={onSettings}
                   onOpenTable={onOpenTable} onFillCodes={onFillCodes} />
    footer = (
      <>
        <button type="button" className="btn btn-ghost mr-auto" onClick={onReset} data-profile-again="">{PT.again}</button>
        <button type="button" className="btn btn-primary" onClick={onClose} data-autofocus data-profile-close="">{PT.close}</button>
      </>
    )
  } else if (job.phase === 'blocked') {
    const action = job.kind === 'disabled'
      ? <button type="button" className="btn btn-sm btn-primary" onClick={onSettings} data-profile-enable="">{PT.enable}</button>
      : job.kind === 'busy' || job.kind === 'other'
        ? <button type="button" className="btn btn-sm" onClick={() => onStart(job.tables)} data-profile-retry="">{PT.retry}</button>
        : <button type="button" className="btn btn-sm" onClick={onGoSource} data-profile-go-source="">{PT.goSource}</button>
    body = (
      <Notice tone={job.kind === 'busy' ? 'info' : 'warn'} attr={{ 'data-profile-blocked': job.kind }}>
        <p className="font-medium">{PT.blocked[job.kind]}</p>
        <p className="mt-0.5 text-dim">{job.message}</p>
        {job.kind !== 'other' && <p className="mt-0.5 text-dim">{PT.blockedNext[job.kind]}</p>}
        <div className="mt-2">{action}</div>
      </Notice>
    )
    footer = (
      <>
        <button type="button" className="btn btn-ghost mr-auto" onClick={onReset} data-profile-back="">{PT.backToSetup}</button>
        <button type="button" className="btn" onClick={onClose}>{PT.close}</button>
      </>
    )
  } else {
    body = (
      <div data-profile-failed="">
        <p className="mb-2 text-xs font-medium">{PT.failedTitle}</p>
        <ErrorState error={job.error} onRetry={() => onStart(job.tables)} />
      </div>
    )
    footer = (
      <>
        <button type="button" className="btn btn-ghost mr-auto" onClick={onReset}>{PT.backToSetup}</button>
        <button type="button" className="btn" onClick={onClose}>{PT.close}</button>
      </>
    )
  }

  return (
    <Modal open onClose={onClose} title={PT.title} width={640} footer={footer}>
      <div className="space-y-4" data-profile-dialog={job?.phase ?? 'setup'}>{body}</div>
    </Modal>
  )
}

/** 确认前写明预算：按这个数据源的剖析设置，最多发几条查询、单条和总时长、抽样规模、整表统计的门槛 */
function Budget({ settings: s, sourceName, onSettings }: { settings: CatalogProfileSettings; sourceName: string; onSettings: () => void }) {
  const lines = [PT.budgetQueries(s.max_queries, s.query_timeout_s), PT.budgetTotal(s.max_total_s), PT.budgetSample(s.sample_size),
    PT.budgetScan(s.max_scan_rows)]
  return (
    <div className="rounded-lg border px-3 py-2.5" data-profile-budget={`${s.max_queries},${s.query_timeout_s},${s.max_total_s}`}>
      <div className="mb-1.5 flex items-center gap-2 text-2xs">
        <span className="font-medium text-dim">{PT.budget}</span>
        <span className="text-faint">{PT.budgetFrom(sourceName)}</span>
        <span className="flex-1" />
        <button type="button" className="rounded px-1 text-2xs text-dim underline decoration-dotted underline-offset-2 hover:text-fg"
                onClick={onSettings} data-profile-settings-open="">{PT.editSettings}</button>
      </div>
      <ul className="space-y-0.5 text-xs">
        {lines.map((l) => (
          <li key={l} className="flex items-start gap-1.5">
            <span className="mt-[7px] h-1 w-1 shrink-0 rounded-full bg-[var(--text-faint)]" aria-hidden />
            <span className="tnum">{l}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

// ---------------------------------------------------------------------------
// 报告
// ---------------------------------------------------------------------------

function Report({ report: r, ms, filled, returnTo, onSettings, onOpenTable, onFillCodes }: {
  report: CatalogProfileOut
  ms: number
  filled: Record<string, number>
  returnTo: { table: string; column: string } | null
  onSettings: () => void
  onOpenTable: (table: string) => void
  onFillCodes: (table: string, column: string) => void
}) {
  const changed = r.total.added + r.total.updated + r.total.removed
  const stop = r.stopped
  // 一项发现、一项跳过都没有的表合成一行，几十张表时报告不被空卡片撑长
  const quiet = r.tables.filter((t) => !t.error && !t.findings.length && !t.skipped.length && !t.date_ranges.length)
  const shown = r.tables.filter((t) => !quiet.includes(t))
  // 填完一列回到报告：焦点交给它后面第一个还有含义待填写的「填写含义」，没有了就落回刚填的那一列。
  // 弹窗自己的初始焦点在下一帧，这里先聚焦，它看到焦点已在弹窗里就不再动
  const root = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!returnTo) return
    const buttons = [...(root.current?.querySelectorAll<HTMLButtonElement>('[data-profile-fill-key]') ?? [])]
    const at = buttons.findIndex((b) => b.dataset.profileFillKey === fillKey(returnTo.table, returnTo.column))
    const target = buttons.slice(at + 1).find((b) => Number(b.dataset.pending) > 0) ?? buttons[at]
    target?.focus()
    target?.scrollIntoView({ block: 'nearest' })
  }, [returnTo])
  return (
    <div ref={root} className="space-y-3" data-profile-report={stop ?? 'done'}>
      <div className="flex items-start gap-2">
        {stop
          ? <TriangleAlert size={15} className="mt-px shrink-0" style={{ color: 'var(--st-waiting)' }} aria-hidden />
          : <CircleCheck size={15} className="mt-px shrink-0" style={{ color: 'var(--st-done)' }} aria-hidden />}
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium" data-profile-title="">{stop ? PT.stoppedTitle(PROFILE_STOP_LABEL[stop] ?? stop) : PT.done}</p>
          <p className="tnum mt-0.5 text-xs text-dim" data-profile-summary={`${r.queries_used}/${r.settings.max_queries}`}>
            {PT.summary(r.queries_used, r.settings.max_queries, r.tables.length)}
            <span className="text-faint"> · {secondsText(ms)}</span>
          </p>
          <p className="tnum mt-0.5 text-xs" data-profile-changes={`${r.total.added},${r.total.updated},${r.total.removed}`}>
            {changed ? PT.changes(r.total.added, r.total.updated, r.total.removed) : <span className="text-faint">{PT.noChange}</span>}
          </p>
        </div>
      </div>
      {stop && (
        <Notice tone="warn" attr={{ 'data-profile-stopped': stop }}>
          <p>{PROFILE_STOP_NEXT[stop] ?? ''}</p>
          {stop !== 'failed' && (
            <button type="button" className="btn btn-sm mt-2" onClick={onSettings} data-profile-settings-open="">{PT.editSettings}</button>
          )}
        </Notice>
      )}
      {r.note && <Notice tone="info" attr={{ 'data-profile-note': '' }}>{r.note}</Notice>}
      {shown.map((t) => <TableReport key={t.table_name} t={t} filled={filled} onOpenTable={onOpenTable} onFillCodes={onFillCodes} />)}
      {quiet.length > 0 && (
        <p className="text-2xs leading-relaxed text-faint" data-profile-quiet={quiet.length}>
          {PT.noFindings}：<span className="mono">{quiet.map((t) => t.table_name).join('、')}</span>
        </p>
      )}
    </div>
  )
}

function sizeText(size: CatalogProfileSize | null): { text: string; hint?: string } | null {
  if (!size) return null
  if (size.rows != null) return { text: size.method === 'stats' ? PT.rowsStats(size.rows) : PT.rowsCount(size.rows), hint: PT.rowsHint[size.method] }
  if (size.at_least != null) return { text: PT.rowsAtLeast(size.at_least), hint: PT.rowsHint.count }
  return null
}

function TableReport({ t, filled, onOpenTable, onFillCodes }: {
  t: CatalogProfileTable
  filled: Record<string, number>
  onOpenTable: (table: string) => void
  onFillCodes: (table: string, column: string) => void
}) {
  const relations = t.findings.filter((f): f is CatalogProfileRelationFinding => f.kind === 'relation')
  const codes = t.findings.filter((f): f is CatalogProfileCodesFinding => f.kind === 'codes')
  const date = t.findings.find((f): f is CatalogProfileDateFinding => f.kind === 'business_date')
  const size = sizeText(t.row_estimate)
  const changed = t.added + t.updated + t.removed
  return (
    <section className="rounded-lg border" data-profile-table={t.table_name} aria-label={t.table_name}>
      <header className="flex flex-wrap items-center gap-x-2 gap-y-0.5 border-b bg-elev px-3 py-1.5">
        <h3 className="mono min-w-0 text-xs font-medium [overflow-wrap:anywhere]">{t.table_name}</h3>
        <span className="tnum text-2xs text-faint">
          {PT.tableQueries(t.queries)}
          {size && <span title={size.hint}> · {size.text}</span>}
          {changed > 0 && <span> · {PT.changes(t.added, t.updated, t.removed)}</span>}
        </span>
        <span className="flex-1" />
        {!t.error && (
          <button type="button" className="inline-flex items-center gap-0.5 rounded px-1 text-2xs text-dim hover:bg-hover hover:text-fg"
                  onClick={() => onOpenTable(t.table_name)} aria-label={PT.openTableLabel(t.table_name)} data-profile-open={t.table_name}>
            {PT.openTable} <ArrowUpRight size={10} aria-hidden />
          </button>
        )}
      </header>
      <div className="space-y-2.5 px-3 py-2">
        {t.error && (
          <p className="flex items-start gap-1.5 text-xs text-[var(--err)]" data-profile-table-error="">
            <OctagonX size={12} className="mt-0.5 shrink-0" aria-hidden /> <span>{t.error}</span>
          </p>
        )}
        {relations.length > 0 && (
          <Group title={PT.findingRelations}>
            {relations.map((f) => <RelationLine key={f.path} f={f} />)}
          </Group>
        )}
        {codes.length > 0 && (
          <Group title={PT.findingCodes}>
            {codes.map((f) => (
              <CodesLine key={f.path} f={f} fillKey={fillKey(t.table_name, f.column)} filled={filled[fillKey(t.table_name, f.column)]}
                         onFill={() => onFillCodes(t.table_name, f.column)} />
            ))}
          </Group>
        )}
        {date && (
          <Group title={PT.findingDate}>
            <li className="text-xs" data-profile-finding="business_date">
              <StatusChip status={date.status} className="mr-1.5 align-middle" />
              {PT.dateProposal(date.column)}
              <span className="tnum text-dim">，{PT.dateSpan(date.min.slice(0, 19), date.max.slice(0, 19))}</span>
            </li>
          </Group>
        )}
        {!date && t.date_ranges.length > 0 && (
          <Group title={PT.dateRanges}>
            {t.date_ranges.map((d) => (
              <li key={d.column} className="tnum text-xs text-dim" data-profile-date-range={d.column}>
                <span className="mono text-fg">{d.column}</span>：{PT.dateSpan(d.min.slice(0, 19), d.max.slice(0, 19))}
              </li>
            ))}
          </Group>
        )}
        {!t.error && !t.findings.length && !t.date_ranges.length && (
          <p className="text-2xs text-faint">{PT.noFindings}</p>
        )}
        {t.skipped.length > 0 && <Skipped list={t.skipped} />}
      </div>
    </section>
  )
}

function Group({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <div className="mb-1 text-2xs font-medium text-faint">{title}</div>
      <ul className="space-y-1.5">{children}</ul>
    </div>
  )
}

type RelationOutcome = 'confirmed' | 'raised' | 'same' | 'lowered' | 'kept'

/**
 * 一条关系这次剖析的结论，按实际变化说：原本就是已验证、这次照样核实的写「仍为已验证，覆盖率 x」，不能说「升为」——
 * 重新剖析、目录没变时报告头写「目录没有变化」，下面却说升了，自相矛盾。老服务端不给剖析前的状态，按升级说
 */
function relationOutcome(f: CatalogProfileRelationFinding): { kind: RelationOutcome; text: string; tone: string; icon: LucideIcon } {
  const reason = f.coverage < 0.95 ? PT.relationLowCoverage : PT.relationNotUnique
  if (f.confirmed) return { kind: 'confirmed', text: PT.relationConfirmed, tone: STATUS_TONE.confirmed, icon: UserCheck }
  if (f.status === 'verified') {
    return f.previous_status === 'verified'
      ? { kind: 'same', text: UT.relationStillVerified(coverageText(f.coverage)), tone: STATUS_TONE.verified, icon: Equal }
      : { kind: 'raised', text: PT.relationVerified, tone: STATUS_TONE.verified, icon: ArrowUp }
  }
  return f.previous_status === 'verified'
    ? { kind: 'lowered', text: UT.relationLowered(reason), tone: STATUS_TONE.proposed, icon: ArrowDown }
    : { kind: 'kept', text: `${PT.relationKept}：${reason}`, tone: STATUS_TONE.proposed, icon: Minus }
}

function RelationLine({ f }: { f: CatalogProfileRelationFinding }) {
  const outcome = relationOutcome(f)
  const Icon = outcome.icon
  return (
    <li className="text-xs" data-profile-finding="relation" data-status={f.status} data-path={f.path}>
      <div className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
        <StatusChip status={f.status} />
        <span className="mono min-w-0 [overflow-wrap:anywhere]">{f.target}</span>
      </div>
      <div className="tnum mt-0.5 text-2xs text-dim">
        <span data-profile-coverage="">{PT.coverage(coverageText(f.coverage))}</span>
        {' · '}
        <span data-profile-cardinality="">{f.cardinality ? CATALOG_CARDINALITY_LABEL[f.cardinality] : PT.cardinalityUnknown}</span>
        {' · '}
        {PT.sampled(f.sample, f.matched)}
      </div>
      {/* 结论是状态说明，不是链接：用状态标识的样子（带框的小标签、图标着状态色），正文用次要文字色 */}
      <div className="mt-1">
        <span className="chip !text-2xs" data-profile-outcome={outcome.kind}>
          <Icon size={10} className="shrink-0" style={{ color: outcome.tone }} aria-hidden />
          {outcome.text}
        </span>
      </div>
    </li>
  )
}

function CodesLine({ f, fillKey: key, filled, onFill }: {
  f: CatalogProfileCodesFinding
  fillKey: string
  /** 从报告去填过含义：保存后还剩几个待填写。没去填过为 undefined，按剖析时的说 */
  filled: number | undefined
  onFill: () => void
}) {
  // 服务端在 summary 里写了几个含义待填写（别的来源写过的含义会沿用）；认不出时按都待填写
  const reported = Number(f.summary.match(/(\d+)\s*个含义待填写/)?.[1] ?? (f.summary.includes('待填写') ? f.values.length : 0))
  const pending = filled ?? reported
  const done = filled != null && filled === 0
  return (
    <li className="text-xs" data-profile-finding="codes" data-column={f.column}>
      <div className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
        <StatusChip status={f.status} />
        <span className="mono font-medium">{f.column}</span>
        <span className="tnum text-2xs text-faint">{PT.codesSummary(f.values.length, f.rows)}</span>
      </div>
      <div className="mt-1 flex flex-wrap gap-1">
        {f.values.slice(0, CODES_SHOWN).map((v) => (
          <span key={v.value} className="inline-flex items-center gap-1 rounded border bg-bg px-1 text-2xs">
            <span className="mono">{v.value}</span>
            <span className="tnum text-faint">{PT.codeRows(v.rows)}</span>
          </span>
        ))}
        {f.values.length > CODES_SHOWN && <span className="text-2xs text-faint">+{f.values.length - CODES_SHOWN}</span>}
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-2">
        {pending > 0 && <span className="text-2xs" style={{ color: 'var(--st-waiting)' }} data-codes-pending={pending}>{PT.codesPendingN(pending)}</span>}
        {done && (
          <span className="inline-flex items-center gap-1 text-2xs text-dim" data-codes-filled="">
            <CircleCheck size={11} className="shrink-0" style={{ color: 'var(--st-done)' }} aria-hidden /> {UT.codesFilled}
          </span>
        )}
        <button type="button" className="btn btn-xs" onClick={onFill} aria-label={done ? UT.editMeaningsLabel(f.column) : PT.fillMeaningsLabel(f.column)}
                data-profile-fill={f.column} data-profile-fill-key={key} data-pending={pending}>
          {done ? UT.editMeanings : PT.fillMeanings}
        </button>
      </div>
    </li>
  )
}

/** 跳过的项：原因（短）+ 哪一项 + 服务端给的整句。多了折起来，每类停下的原因只列前几项 */
function Skipped({ list }: { list: CatalogProfileSkip[] }) {
  const [all, setAll] = useState(false)
  const shown = all ? list : list.slice(0, SKIP_SHOWN)
  return (
    <div data-profile-skipped={list.length}>
      <div className="mb-1 text-2xs font-medium text-faint">{PT.skippedTitle(list.length)}</div>
      <ul className="space-y-1">
        {shown.map((s, i) => (
          <li key={`${s.kind}:${s.target}:${i}`} className="flex items-start gap-1.5 text-2xs" data-profile-skip={s.reason}>
            <span className="chip mt-px shrink-0 !px-1.5 !py-0" style={{ color: 'var(--text-dim)' }}>
              {PROFILE_SKIP_REASON_LABEL[s.reason] ?? s.reason}
            </span>
            <span className="min-w-0 leading-relaxed">
              <span className="text-faint">{PROFILE_SKIP_KIND_LABEL[s.kind] ?? s.kind} </span>
              <span className="mono text-dim [overflow-wrap:anywhere]">{s.target}</span>
              <span className="block text-dim">{s.detail}</span>
            </span>
          </li>
        ))}
      </ul>
      {list.length > SKIP_SHOWN && !all && (
        <button type="button" className="mt-1 rounded px-1 text-2xs text-dim underline decoration-dotted underline-offset-2 hover:text-fg"
                onClick={() => setAll(true)}>
          {PT.moreSkipped(list.length - SKIP_SHOWN)}
        </button>
      )}
    </div>
  )
}

