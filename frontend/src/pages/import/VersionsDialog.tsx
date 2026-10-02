import { useEffect, useId, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { AlertTriangle, ArrowLeft, ChevronRight, FileText, Info, RotateCcw, Scissors, Trash2, Undo2 } from 'lucide-react'
import clsx from 'clsx'
import { ApiError, api } from '../../api/client'
import type {
  DataSource, ImportAcceptance, ImportRecord, ManifestOut, PeriodRef, RevokePlan, SnapshotOut, SnapshotPart,
} from '../../types'
import {
  EmptyState, ErrorState, Modal, Notice, Skeleton, TabPanel, Tabs, promptDialog, toast,
} from '../../components/ui'
import { localActor } from '../../lib/actor'
import { humanizeError } from '../../lib/errors'
import { formatBytes, formatDateTime, formatTime, parseServerTime } from '../../lib/format'
import { RAW_STATE_LABEL, VERSIONS_TEXT as VT } from '../../lib/terms'
import { ManifestBody } from './ManifestBody'

// ===========================================================================
// 版本页（数据源卡片上的「版本」，P3-SPEC 7.8）：当前版本的各期、历史版本、全部导入记录三个页签，
// 每一期的操作（查看清单、移除、作废接受、清除原件）就近放在那一期上。
//
// 整页不出现「快照」「并集」「构建」：用户关心的是「数据源现在用的是哪个版本、包含哪几期」，内部怎么存
// 不该成为理解的门槛（评审三-m1）。写操作一律先确认、后果逐条列出；之后重新取列表，409 时显示服务端原话
// 并刷新——确认框是按刷新前的列表给的，别人在这期间改过当前版本，旧的确认就不作数了。
// ===========================================================================

/**
 * 当前版本、被运行引用的版本之外，系统再保留最近几个版本（P3-SPEC 1.4 的 P3-3 定为 3，用户可推翻）。快照列表接口
 * 不返回这个数，先在这里写死：改后端 table_versions.py 的 SNAPSHOT_KEEP 时必须同步改这里，否则保留规则那句话
 * 会说错。后端测试 test_versions_page_keep_count_matches_backend 核对两边一致
 */
const KEEP_RECENT = 3

/** 一期的统计期：没有统计期的期（简单导入、期 3 之前的导入）用文件名指代，什么都没有写「统计期未记录」 */
function periodText(start: string | null | undefined, end: string | null | undefined, file?: string | null): string {
  if (start && end) return VT.period(start, end)
  return file ? VT.periodFile(file) : VT.periodUnknown
}
const refOfPart = (p: SnapshotPart): PeriodRef => ({ start: p.period_start, end: p.period_end, file_name: p.file_name })
const partText = (p: SnapshotPart) => periodText(p.period_start, p.period_end, p.file_name)
const periodList = (refs: PeriodRef[]) => refs.map((r) => periodText(r.start, r.end, r.file_name)).join('、')

const DAY = 86_400_000
const dayOf = (s: string) => Date.parse(`${s}T00:00:00Z`)
const dayText = (t: number) => new Date(t).toISOString().slice(0, 10)

/**
 * 各期之间的空缺（按统计期排序后，前一期结束到后一期开始之间隔了一天以上）。移除中间一期之前，确认框要先写明
 * 「移除后各期之间出现空缺」（7.4 第 5 步）：前端按 parts 自己算，服务端以重算的为准
 */
function gapsOf(refs: PeriodRef[]): { start: string; end: string }[] {
  const ps = refs.filter((r) => r.start && r.end).sort((a, b) => a.start!.localeCompare(b.start!))
  const out: { start: string; end: string }[] = []
  let reach = Number.NEGATIVE_INFINITY
  for (const p of ps) {
    const s = dayOf(p.start!)
    const e = dayOf(p.end!)
    if (Number.isFinite(reach) && s - reach > DAY) out.push({ start: dayText(reach + DAY), end: dayText(s - DAY) })
    reach = Math.max(reach, e)
  }
  return out
}

/** 简单导入的版本没有配方，导入模式写「简单导入」；期 3 之前的版本没有 mode，按每期替换理解 */
const modeText = (s: { recipe: SnapshotOut['recipe']; mode: SnapshotOut['mode'] }) =>
  (!s.recipe ? VT.simpleImport : VT.modeLabel[s.mode ?? 'replace'] ?? VT.modeLabel.replace)

const recipeText = (s: SnapshotOut) => (s.recipe ? VT.recipeSeq(s.recipe.seq) : VT.simpleImport)

const reasonOf = (s: SnapshotOut) => (s.reason_code ? VT.notActivatable[s.reason_code] : null) ?? s.reason ?? ''

/** 历史版本按「最近一次成为当前版本」的时刻排；没启用过的按创建时间 */
const timeOf = (s: SnapshotOut) => parseServerTime(s.activated_at ?? s.created_at)?.getTime() ?? 0

const signerLine = () => VT.signedBy(localActor() ?? VT.unsigned)

const reasonValid = (v: string) => (v.length > 500 ? VT.reasonTooLong : null)

/** 写接口出错时显示的话：服务端的原话（已守文案规范）优先，网络类失败按全站的说法 */
function errorText(e: unknown): string {
  if (e instanceof ApiError && e.kind === 'http' && e.message) return e.message
  const h = humanizeError(e)
  return h.action ? `${h.title}。${h.action}` : h.title
}

/** 回滚目标的描述：各期、配方第几版（或简单导入）、导入模式 */
function targetText(t: NonNullable<RevokePlan['target']>): string {
  const parts = t.parts?.length ? periodList(t.parts) : VT.periodUnknown
  if (t.simple || t.recipe_seq == null) return `${parts}，${VT.simpleImport}`
  const mode = t.mode ? VT.modeLabel[t.mode] : ''
  return [parts, VT.recipeSeq(t.recipe_seq), mode].filter(Boolean).join('，')
}

type Loaded = { snaps: SnapshotOut[]; imports: ImportRecord[] }
type TabKey = 'current' | 'history' | 'imports'
type Banner = { tone: 'err' | 'info'; text: string; items?: string[]; refreshed?: boolean }

/**
 * 版本页。source 是卡片上的那一行；写操作返回的新数据源交给 onChange（卡片据此更新，父级顺带刷新全局目录）。
 * 清除原件不返回数据源，卡片上的原件状态靠 onReload 重新取列表
 */
export function VersionsDialog({ source, onClose, onChange, onReload }: {
  source: Pick<DataSource, 'id' | 'name'>
  onClose: () => void
  onChange: (row: DataSource) => void
  onReload: () => void
}) {
  const [data, setData] = useState<Loaded | null>(null)
  const [loadError, setLoadError] = useState<unknown>(null)
  const [tab, setTab] = useState<TabKey>('current')
  const [busy, setBusy] = useState('')
  const [banner, setBanner] = useState<Banner | null>(null)
  const [manifestOf, setManifestOf] = useState<ImportRecord | null>(null)
  const [retiredOpen, setRetiredOpen] = useState(false)
  const loadSeq = useRef(0)
  const hasData = useRef(false)
  const idPrefix = `versions-${useId().replace(/[^a-zA-Z0-9]/g, '')}`

  /** 版本列表和导入记录一起取：当前版本那一页的接受、作废预案、原件引用都在导入记录里 */
  const load = async (): Promise<Loaded | null> => {
    const seq = ++loadSeq.current
    try {
      const [snaps, imports] = await Promise.all([api.versions.snapshots(source.id), api.versions.imports(source.id)])
      if (seq !== loadSeq.current) return null
      const next = { snaps, imports }
      hasData.current = true
      setData(next)
      setLoadError(null)
      return next
    } catch (e) {
      if (seq !== loadSeq.current) return null
      // 首次没取到才整页换成错误；之后的刷新失败保留旧列表，只弹提示
      if (hasData.current) toast.error(e)
      else setLoadError(e)
      return null
    }
  }
  useEffect(() => { void load() }, [source.id])

  /**
   * 写操作失败：显示服务端原话，再刷新列表（7.8）。409 说明列表和服务端已经对不上（别人改了当前版本、那一期已不在
   * 当前版本里……），卡片上的当前版本可能也旧了，一并刷新
   */
  const failed = async (e: unknown): Promise<Loaded | null> => {
    const code = e instanceof ApiError ? e.code : undefined
    setBanner({ tone: 'err', text: errorText(e), refreshed: code === 'base_changed' || code === 'revoke_target_changed' })
    if (e instanceof ApiError && e.status === 409) onReload()
    return load()
  }

  const run = async <T,>(key: string, call: () => Promise<T>): Promise<{ ok: true; value: T } | { ok: false; error: unknown }> => {
    setBusy(key)
    setBanner(null)
    try {
      return { ok: true, value: await call() }
    } catch (error) {
      return { ok: false, error }
    } finally {
      setBusy('')
    }
  }

  // ---- 启用（回滚）。理由可选（7.2）：回滚往往是发现了问题，写下原因方便日后对账，但不能因为没写就不让回滚
  const activate = async (s: SnapshotOut, current: SnapshotOut | null, retried = false, lastReason = ''): Promise<void> => {
    const c = VT.activateConsequence
    const consequences: string[] = []
    const removed = s.periods_diff?.removed ?? []
    const added = s.periods_diff?.added ?? []
    if (removed.length) consequences.push(c.removed(periodList(removed)))
    if (added.length) consequences.push(c.added(periodList(added)))
    consequences.push(c.newRuns, c.running)
    if (s.recipe) consequences.push(c.recipe(s.recipe.seq))
    else if (current?.recipe) consequences.push(c.simple)
    // 导入模式变了就写（7.2）：目标按配方导入，而当前是简单导入（谈不上导入模式）或模式不同。从简单导入回到
    // 按期累积的版本，之后上传新一期会按累积处理，确认时必须看得到
    const targetMode = s.mode ?? 'replace'
    if (s.recipe && current && (!current.recipe || targetMode !== (current.mode ?? 'replace')) && VT.modeLabel[targetMode]) {
      consequences.push(c.mode(VT.modeLabel[targetMode]))
    }
    consequences.push(c.nextUpload, c.stagings)
    if (s.mask_lost?.length) consequences.push(c.maskLost(s.mask_lost.join('、')))
    // 输入框允许空着确认（allowEmpty）：取消返回 null，空着确认返回 ''。遮罩列丢失后重开时带上刚才写的理由，
    // 不让人再写一遍
    const reason = await promptDialog({
      title: VT.activateTitle, body: signerLine(), consequences, confirmLabel: VT.activate,
      label: VT.reasonOptional, initial: lastReason, allowEmpty: true, validate: reasonValid,
    })
    if (reason == null) return
    const r = await run(`activate:${s.id}`, () => api.versions.activate(source.id, s.id, {
      confirm: true,
      expected_current_snapshot_id: current?.id ?? null,
      ...(s.mask_lost?.length ? { ack_mask_lost: s.mask_lost } : {}),
      // 不填就不发这个键：服务端（api/source_versions.py 的 _reason）把缺省和空串都当作没写理由、记 None，两者等价；
      // 不发更干净，检查脚本据此区分「没写」和「写了」
      ...(reason ? { reason } : {}),
      signed_by: localActor(),
    }))
    if (r.ok) {
      toast.ok(VT.activated)
      onChange(r.value.source)
      await load()
      return
    }
    const fresh = await failed(r.error)
    // 遮罩列丢失（7.2）：按刷新后的列表重新打开确认框，把丢失的列列进后果。只重开一次，免得两边说法不一时来回弹
    if (!retried && r.error instanceof ApiError && r.error.code === 'mask_lost' && fresh) {
      const again = fresh.snaps.find((x) => x.id === s.id)
      if (again?.activatable && again.mask_lost?.length) await activate(again, fresh.snaps.find((x) => x.current) ?? null, true, reason)
    }
  }

  // ---- 移除这一期（按期累积）
  const removePeriod = async (cur: SnapshotOut, part: SnapshotPart) => {
    const rest = cur.parts.filter((p) => p.import_id !== part.import_id)
    const label = partText(part)
    const before = gapsOf(cur.parts.map(refOfPart))
    // 只写移除之后新出现的空缺：原来就有的空缺不是这次移除造成的
    const gaps = gapsOf(rest.map(refOfPart)).filter((g) => !before.some((b) => b.start === g.start && b.end === g.end))
    const c = VT.removeConsequence
    const reason = await promptDialog({
      title: VT.removeTitle(label),
      body: signerLine(),
      consequences: [
        c.result(periodList(rest.map(refOfPart))),
        ...gaps.map((g) => c.gap(VT.period(g.start, g.end))),
        c.recipe, c.reactivate, c.running, c.stagings,
      ],
      label: VT.reasonLabel,
      confirmLabel: VT.removePeriod,
      validate: reasonValid,
    })
    if (reason == null) return
    const r = await run(`remove:${part.import_id}`, () => api.versions.removePeriod(source.id, part.import_id, {
      confirm: true, expected_current_snapshot_id: cur.id, reason, signed_by: localActor(),
    }))
    if (!r.ok) { await failed(r.error); return }
    toast.ok(r.value.reused ? `${VT.removed(label)}。${VT.removedReused}` : VT.removed(label))
    onChange(r.value.source)
    await load()
  }

  // ---- 撤回这一期（作废接受，不可恢复）
  const revoke = async (cur: SnapshotOut, part: SnapshotPart, plan: RevokePlan) => {
    const c = VT.revokeConsequence
    const consequences: string[] = [c.irreversible]
    if (plan.action === 'rollback' && plan.target) {
      consequences.push(c.rollback(targetText(plan.target)))
      if (plan.target.simple) consequences.push(VT.activateConsequence.simple)
    } else if (plan.action === 'remove_period') {
      consequences.push(c.remove(plan.result_parts?.length ? periodList(plan.result_parts) : VT.periodUnknown))
      for (const g of plan.gaps ?? []) consequences.push(VT.removeConsequence.gap(VT.period(g.start, g.end)))
    }
    if (plan.mask_lost?.length) consequences.push(VT.activateConsequence.maskLost(plan.mask_lost.join('、')))
    consequences.push(c.record, c.running)
    const reason = await promptDialog({
      title: VT.revokeTitle,
      body: `${partText(part)}。${signerLine()}`,
      consequences,
      danger: true,
      label: VT.reasonLabel,
      confirmLabel: VT.revoke,
      validate: reasonValid,
    })
    if (reason == null) return
    const r = await run(`revoke:${part.import_id}`, () => api.versions.revokeAcceptance(source.id, part.import_id, {
      confirm: true, expected_current_snapshot_id: cur.id, reason, signed_by: localActor(),
      // 替换模式：确认框里写的回滚目标，服务端在锁内重算，对不上回 revoke_target_changed
      ...(plan.action === 'rollback' ? { expected_target_snapshot_id: plan.target_snapshot_id } : {}),
      ...(plan.mask_lost?.length ? { ack_mask_lost: plan.mask_lost } : {}),
    }))
    if (!r.ok) { await failed(r.error); return }
    toast.ok(VT.revoked)
    onChange(r.value.source)
    await load()
  }

  // ---- 清除原件：当前版本里的各期在页签一操作，不在当前版本里、原件还在的导入记录在页签三操作（D13）
  const purge = async (target: { id: string; seq: number; label: string }, rec: ImportRecord | undefined) => {
    const c = VT.purgeConsequence
    const shared = rec?.raw_shared_with ?? []
    const open = rec?.raw_open_stagings ?? 0
    const reason = await promptDialog({
      title: VT.purgeTitle,
      body: `${VT.purgeTarget(target.seq, target.label)}。${signerLine()}`,
      // 提交前就列出一并受影响的导入（7.6）：清除按内容算，别处的同一份原件也会没
      consequences: [
        c.evidence, c.redraft,
        // 后端已把删掉的源写成「已删除的数据源」；老后端或老数据可能仍是 null，兜底同 also_purged，不显示「null」
        ...(shared.length ? [c.shared(shared.map((x) => VT.sharedItem(x.source_name ?? VT.deletedSource, x.count)).join('、'))] : []),
        ...(open > 0 ? [c.stagings(open)] : []),
      ],
      danger: true,
      label: VT.reasonLabel,
      confirmLabel: VT.purge,
      validate: reasonValid,
    })
    if (reason == null) return
    const r = await run(`purge:${target.id}`, () => api.versions.purgeRaw(source.id, target.id, {
      confirm: true, reason, signed_by: localActor(),
    }))
    if (!r.ok) { await failed(r.error); return }
    const also = r.value.also_purged ?? []
    const done = VT.purgeDone(also.length, (r.value.discarded_stagings ?? []).length)
    setBanner({
      tone: 'info', text: done,
      // 别的源已经删掉时服务端给不出名字（source_name 为 null）：写「已删除的数据源」，不拿本源的名字顶替
      items: also.map((x) => VT.purgedItem(x.source_name ?? VT.deletedSource, x.seq, x.file_name)),
    })
    toast.ok(done)
    onReload()
    await load()
  }

  const current = data?.snaps.find((s) => s.current) ?? null
  const recOf = (id: string) => data?.imports.find((r) => r.id === id)

  let body: ReactNode
  if (!data) {
    body = loadError
      ? <ErrorState error={loadError} onRetry={() => void load()} />
      : <Skeleton rows={4} height={44} gap={8} />
  } else if (manifestOf) {
    body = <ManifestView sourceId={source.id} rec={manifestOf} onBack={() => setManifestOf(null)} />
  } else {
    body = (
      <>
        <Tabs
          tabs={[
            { key: 'current', label: VT.tabs.current },
            { key: 'history', label: VT.tabs.history },
            { key: 'imports', label: VT.tabs.imports },
          ]}
          active={tab}
          onChange={(k) => setTab(k as TabKey)}
          label={VT.open}
          idPrefix={idPrefix}
        />
        <TabPanel idPrefix={idPrefix} tabKey={tab} className="pt-3">
          {tab === 'current' && (
            current
              ? <CurrentTab cur={current} recOf={recOf} busy={busy}
                            onManifest={(rec) => setManifestOf(rec)}
                            onRemove={(p) => void removePeriod(current, p)}
                            onRevoke={(p, plan) => void revoke(current, p, plan)}
                            onPurge={(p) => void purge({ id: p.import_id, seq: p.seq, label: partText(p) }, recOf(p.import_id))} />
              : <EmptyState icon={<RotateCcw size={20} />} title={VT.empty} />
          )}
          {tab === 'history' && (
            <HistoryTab snaps={data.snaps} current={current} busy={busy} retiredOpen={retiredOpen}
                        onToggleRetired={() => setRetiredOpen((v) => !v)}
                        onActivate={(s) => void activate(s, current)} />
          )}
          {tab === 'imports' && (
            <ImportsTab imports={data.imports} busy={busy} onManifest={(rec) => setManifestOf(rec)}
                        onPurge={(rec) => void purge({ id: rec.id, seq: rec.seq, label: periodText(rec.period_start, rec.period_end, rec.file_name) }, rec)} />
          )}
        </TabPanel>
      </>
    )
  }

  return (
    <Modal open onClose={onClose} title={VT.title(source.name)} width={880}>
      <div className="space-y-3" data-versions="" data-tab={manifestOf ? 'manifest' : tab}>
        <p className="flex items-start gap-1.5 text-2xs leading-relaxed text-faint" data-retention-rule="">
          <Info size={12} className="mt-0.5 shrink-0" aria-hidden />
          <span>{VT.retentionRule(KEEP_RECENT)}</span>
        </p>
        {banner && (
          <Notice tone={banner.tone} attr={{ 'data-versions-banner': banner.tone }}>
            <p>{banner.text}</p>
            {banner.refreshed && <p className="mt-1 text-dim" data-versions-refreshed="">{VT.refreshHint}</p>}
            {!!banner.items?.length && (
              <ul className="mt-1 space-y-0.5 text-dim">
                {banner.items.map((x, i) => <li key={i}>{x}</li>)}
              </ul>
            )}
          </Notice>
        )}
        {body}
      </div>
    </Modal>
  )
}

// ---------------------------------------------------------------------------
// 页签一：当前版本
// ---------------------------------------------------------------------------

function CurrentTab({ cur, recOf, busy, onManifest, onRemove, onRevoke, onPurge }: {
  cur: SnapshotOut
  recOf: (id: string) => ImportRecord | undefined
  busy: string
  onManifest: (rec: ImportRecord) => void
  onRemove: (p: SnapshotPart) => void
  onRevoke: (p: SnapshotPart, plan: RevokePlan) => void
  onPurge: (p: SnapshotPart) => void
}) {
  const accumulate = cur.mode === 'accumulate'
  const only = cur.parts.length <= 1
  // 各期之间已有的空缺：移除过中间一期之后，只有当时的确认框写过一次，别人再打开时只看到「2 期」和两个不相邻的
  // 月份，容易把两期直接拿来比。每次打开都写出来（H2：跨期不可比要提示）
  const gaps = gapsOf(cur.parts.map(refOfPart))
  return (
    <div className="space-y-3">
      <section className="space-y-2 rounded-lg border bg-bg p-3" data-current-overview="" data-snapshot={cur.id}>
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs sm:grid-cols-4">
          <Fact label={VT.overview.mode} value={modeText(cur)} attr="mode" />
          <Fact label={VT.overview.periods} value={VT.periods(cur.parts.length)} attr="periods" />
          <Fact label={VT.overview.recipe} value={recipeText(cur)} attr="recipe" />
          <Fact label={VT.overview.activated} value={formatDateTime(cur.activated_at ?? cur.created_at)} attr="activated" />
        </dl>
        {gaps.length > 0 && (
          <p className="flex items-start gap-1.5 text-2xs leading-relaxed" style={{ color: 'var(--warn)' }} data-current-gaps={gaps.length}>
            <AlertTriangle size={12} className="mt-0.5 shrink-0" aria-hidden />
            <span>{VT.currentGaps(gaps.map((g) => VT.period(g.start, g.end)).join('、'))}</span>
          </p>
        )}
        <TableRows label={VT.overview.tables} rows={cur.tables} attr="data-current-tables" />
      </section>

      <ul className="space-y-2" data-snapshot-parts="">
        {cur.parts.map((p) => {
          const rec = recOf(p.import_id)
          const raw = rec?.raw_state ?? p.raw_state
          const accepted = rec?.acceptances ?? []
          const acceptN = Math.max(accepted.length, (p.overrides ?? 0) + (p.waivers ?? 0))
          const revoked = p.revoked || !!rec?.revoked
          const plan = rec?.revoke_plan ?? null
          return (
            <li key={p.import_id} className="space-y-2 rounded-lg border px-3 py-2.5"
                data-snapshot-part={p.import_id} data-period={p.period_start && p.period_end ? `${p.period_start}~${p.period_end}` : ''}>
              <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                <span className="tnum text-sm font-medium">{partText(p)}</span>
                {p.period_start && p.period_end && <span className="mono break-all text-2xs text-dim">{p.file_name}</span>}
                <span className="text-2xs text-faint">{VT.importSeq(p.seq)}</span>
                <span className="flex-1" />
                {revoked && <span className="chip" style={{ color: 'var(--err)' }} data-revoked-badge="">{VT.revokedBadge}</span>}
                <span className="chip" data-raw-state={raw}>{RAW_STATE_LABEL[raw] ?? raw}</span>
              </div>
              <TableRows rows={p.rows} attr="data-part-rows" />
              {acceptN > 0 && (
                <details className="text-xs" data-part-acceptances={acceptN}>
                  <summary className="cursor-pointer select-none text-dim hover:text-fg">{VT.acceptCount(acceptN)}</summary>
                  <AcceptanceList items={accepted} className="mt-1.5" />
                </details>
              )}
              {rec?.signed_by && <div className="text-2xs text-faint">{VT.signedBy(rec.signed_by)}</div>}
              <div className="flex flex-wrap items-center gap-1.5">
                <button type="button" className="btn btn-xs" data-manifest-open={p.import_id} disabled={!rec}
                        onClick={() => rec && onManifest(rec)}>
                  <FileText size={11} aria-hidden /> {VT.manifest}
                </button>
                {accumulate && (
                  <>
                    <button type="button" className="btn btn-xs" data-remove-period={p.import_id} disabled={!!busy || only}
                            aria-describedby={only ? `remove-last-${p.import_id}` : undefined} onClick={() => onRemove(p)}>
                      <Scissors size={11} aria-hidden /> {VT.removePeriod}
                    </button>
                    {only && (
                      <span id={`remove-last-${p.import_id}`} className="text-2xs text-faint" data-remove-last="">
                        {VT.removeLastDisabled}
                      </span>
                    )}
                  </>
                )}
                {acceptN > 0 && !revoked && (
                  <>
                    <button type="button" className="btn btn-xs text-[var(--err)]" data-revoke={p.import_id}
                            disabled={!!busy || !plan?.action} onClick={() => plan?.action && onRevoke(p, plan)}>
                      <Undo2 size={11} aria-hidden /> {VT.revoke}
                    </button>
                    {plan && !plan.action && (
                      <span className="text-2xs text-faint" data-revoke-unavailable="">{plan.reason || VT.revokeUnavailable}</span>
                    )}
                  </>
                )}
                {raw === 'kept' && (
                  <button type="button" className="btn btn-xs text-[var(--err)]" data-purge-raw={p.import_id}
                          disabled={!!busy} onClick={() => onPurge(p)}>
                    <Trash2 size={11} aria-hidden /> {VT.purge}
                  </button>
                )}
              </div>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

function Fact({ label, value, attr }: { label: string; value: ReactNode; attr: string }) {
  return (
    <div className="min-w-0" data-fact={attr}>
      <dt className="text-2xs text-faint">{label}</dt>
      <dd className="tnum break-words">{value}</dd>
    </div>
  )
}

/** 表 → 行数：每张表一个小标签 */
function TableRows({ rows, label, attr }: { rows: Record<string, number> | null | undefined; label?: string; attr: string }) {
  const entries = Object.entries(rows ?? {})
  if (!entries.length) return null
  return (
    <div className="space-y-1" {...{ [attr]: '' }}>
      {label && <div className="text-2xs text-faint">{label}</div>}
      <ul className="flex flex-wrap gap-1">
        {entries.map(([t, n]) => (
          <li key={t} className="chip" data-table-rows={t} data-rows={n}>
            {t} <span className="tnum text-faint">{VT.rows(n)}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

/** 接受逐条：种类、核对、理由、署名（未认证）、时间 */
function AcceptanceList({ items, className }: { items: ImportAcceptance[]; className?: string }) {
  if (!items.length) return null
  return (
    <ul className={clsx('space-y-1', className)}>
      {items.map((a, i) => (
        <li key={`${a.check_id}-${i}`} className="space-y-0.5 rounded-md border px-2.5 py-1.5 text-xs" data-acceptance={a.check_id}>
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className="chip">{VT.acceptKind[a.kind] ?? VT.acceptKind.override}</span>
            <span className="min-w-0 flex-1 break-words">{VT.acceptRow(a.check_id, a.reason)}</span>
          </div>
          <div className="flex flex-wrap gap-x-2 text-2xs text-faint">
            <span>{VT.signedBy(a.signed_by || VT.unsigned)}</span>
            {a.at && <span className="tnum">{formatDateTime(a.at)}</span>}
          </div>
        </li>
      ))}
    </ul>
  )
}

// ---------------------------------------------------------------------------
// 页签二：历史版本
// ---------------------------------------------------------------------------

function HistoryTab({ snaps, current, busy, retiredOpen, onToggleRetired, onActivate }: {
  snaps: SnapshotOut[]
  current: SnapshotOut | null
  busy: string
  retiredOpen: boolean
  onToggleRetired: () => void
  onActivate: (s: SnapshotOut) => void
}) {
  // 当前版本在第一个页签，这里只列其他版本：可以启用的在前，各自按启用时间倒序；已回收的单独折叠
  const others = snaps.filter((s) => !s.current && s.id !== current?.id)
  const retired = others.filter((s) => s.reason_code === 'retired')
  const listed = others.filter((s) => s.reason_code !== 'retired')
    .sort((a, b) => Number(b.activatable) - Number(a.activatable) || timeOf(b) - timeOf(a))
  if (!others.length) return <p className="py-6 text-center text-xs text-faint" data-history-empty="">{VT.historyEmpty}</p>
  return (
    <div className="space-y-2">
      <ul className="space-y-2" data-history-list="">
        {listed.map((s) => <HistoryItem key={s.id} s={s} busy={busy} onActivate={onActivate} />)}
      </ul>
      {retired.length > 0 && (
        <div className="rounded-lg border" data-retired-group={retired.length}>
          <button type="button" className="flex w-full items-center gap-1.5 px-3 py-2 text-left text-xs text-dim hover:text-fg"
                  aria-expanded={retiredOpen} onClick={onToggleRetired}>
            <ChevronRight size={12} className={clsx('transition-transform', retiredOpen && 'rotate-90')} aria-hidden />
            {VT.retiredGroup(retired.length)}
          </button>
          {retiredOpen && (
            <ul className="space-y-2 border-t p-2">
              {retired.sort((a, b) => timeOf(b) - timeOf(a)).map((s) => <HistoryItem key={s.id} s={s} busy={busy} onActivate={onActivate} />)}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}

function HistoryItem({ s, busy, onActivate }: { s: SnapshotOut; busy: string; onActivate: (s: SnapshotOut) => void }) {
  const why = s.activatable ? '' : reasonOf(s)
  return (
    <li className={clsx('space-y-1.5 rounded-lg border px-3 py-2.5 text-xs', !s.activatable && 'opacity-80')}
        data-snapshot={s.id} data-activatable={s.activatable ? 'true' : 'false'} data-reason-code={s.reason_code ?? ''}>
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        {/* 列表里写短时间（「9/30 23:34 启用」），带秒和时区的完整时间放在悬停里，给逐秒对照日志的人用 */}
        <span className="tnum font-medium" title={formatDateTime(s.activated_at ?? s.created_at)}>
          {VT.activatedAt(formatTime(s.activated_at ?? s.created_at))}
        </span>
        <span className="text-dim">{VT.periods(s.parts.length)}</span>
        <span className="text-dim">{recipeText(s)}</span>
        <span className="text-dim">{modeText(s)}</span>
        <span className="flex-1" />
        {s.activatable && (
          <button type="button" className="btn btn-xs btn-primary" data-activate={s.id} disabled={!!busy} onClick={() => onActivate(s)}>
            <RotateCcw size={11} aria-hidden /> {VT.activate}
          </button>
        )}
      </div>
      <div className="break-words text-dim" data-snapshot-periods="">{periodList(s.parts.map(refOfPart)) || VT.periodUnknown}</div>
      <TableRows rows={s.tables} attr="data-snapshot-tables" />
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-2xs text-faint">
        {s.db_size != null && <span className="tnum">{VT.size(formatBytes(s.db_size))}</span>}
        {s.pinned_runs > 0 && <span className="tnum" data-pinned-runs={s.pinned_runs}>{VT.pinnedRuns(s.pinned_runs)}</span>}
      </div>
      {!!s.mask_lost?.length && (
        <div className="text-2xs" style={{ color: 'var(--warn)' }} data-mask-lost={s.mask_lost.join(',')}>
          {VT.maskLostBadge}：{s.mask_lost.join('、')}
        </div>
      )}
      {why && <div className="text-2xs text-faint" data-not-activatable="">{why}</div>}
    </li>
  )
}

// ---------------------------------------------------------------------------
// 页签三：全部导入记录
//
// 在当前版本里的那几条，操作只放在页签一（7.8：同一件事不给两处入口）。不在当前版本里的（已被替换、已回收）
// 在页签一看不到，原件却可能还在服务端：每期替换模式下以前各期都会变成已被替换，这里再不给入口，这些原件就
// 永远清不掉了，D13 定的是「提供清除」。所以这几条原件还在时给「清除原件」，移除、作废只对当前版本有意义，不给
// ---------------------------------------------------------------------------

function ImportsTab({ imports, busy, onManifest, onPurge }: {
  imports: ImportRecord[]
  busy: string
  onManifest: (rec: ImportRecord) => void
  onPurge: (rec: ImportRecord) => void
}) {
  if (!imports.length) return <p className="py-6 text-center text-xs text-faint">{VT.importsEmpty}</p>
  const sorted = [...imports].sort((a, b) => b.seq - a.seq)
  return (
    <ul className="space-y-2" data-import-records="">
      {sorted.map((r) => {
        const inCurrent = r.in_current ?? r.current
        return (
          <li key={r.id} className="space-y-1.5 rounded-lg border px-3 py-2.5 text-xs" data-import-record={r.id} data-status={r.status}>
            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
              <span className="font-medium">{VT.importSeq(r.seq)}</span>
              <span className="tnum">{periodText(r.period_start, r.period_end)}</span>
              <span className="mono break-all text-2xs text-dim">{r.file_name}</span>
              <span className="flex-1" />
              {r.revoked && <span className="chip" style={{ color: 'var(--err)' }}>{VT.revokedBadge}</span>}
              <span className="chip">{inCurrent ? VT.importStatus.active : VT.importStatus[r.status] ?? r.status}</span>
              <span className="chip">{RAW_STATE_LABEL[r.raw_state] ?? r.raw_state}</span>
            </div>
            <div className="flex flex-wrap gap-x-3 gap-y-1 text-2xs text-faint">
              <span>{r.recipe_seq != null ? VT.recipeSeq(r.recipe_seq) : r.recipe_id ? '' : VT.simpleImport}</span>
              {r.created_at && <span className="tnum">{formatDateTime(r.created_at)}</span>}
              {r.signed_by && <span>{VT.signedBy(r.signed_by)}</span>}
            </div>
            <TableRows rows={r.rows} attr="data-record-rows" />
            <AcceptanceList items={r.acceptances ?? []} />
            <div className="flex flex-wrap items-center gap-1.5">
              <button type="button" className="btn btn-xs" data-manifest-open={r.id} onClick={() => onManifest(r)}>
                <FileText size={11} aria-hidden /> {VT.manifest}
              </button>
              {!inCurrent && r.raw_state === 'kept' && (
                <button type="button" className="btn btn-xs text-[var(--err)]" data-purge-raw={r.id}
                        disabled={!!busy} onClick={() => onPurge(r)}>
                  <Trash2 size={11} aria-hidden /> {VT.purge}
                </button>
              )}
            </div>
          </li>
        )
      })}
    </ul>
  )
}

// ---------------------------------------------------------------------------
// 导入清单（版本页里的内嵌视图，7.3）
// ---------------------------------------------------------------------------

function ManifestView({ sourceId, rec, onBack }: { sourceId: string; rec: ImportRecord; onBack: () => void }) {
  const [m, setM] = useState<ManifestOut | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [nonce, setNonce] = useState(0)
  useEffect(() => {
    let alive = true
    setM(null)
    setError(null)
    api.versions.importManifest(sourceId, rec.id)
      .then((x) => { if (alive) setM(x) })
      .catch((e) => { if (alive) setError(e) })
    return () => { alive = false }
  }, [sourceId, rec.id, nonce])

  return (
    <div className="space-y-3" data-manifest-view={rec.id}>
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" className="btn btn-sm btn-ghost" onClick={onBack} data-manifest-back="">
          <ArrowLeft size={12} aria-hidden /> {VT.back}
        </button>
        <h3 className="text-sm font-semibold">{VT.manifestTitle(rec.seq)}</h3>
        <span className="mono break-all text-2xs text-dim">{rec.file_name}</span>
      </div>
      {error
        ? <ErrorState error={error} onRetry={() => setNonce((n) => n + 1)} />
        : !m
          ? <Skeleton rows={5} height={36} gap={8} />
          : <ManifestBody m={m} fileName={rec.file_name} rawState={rec.raw_state} recipeSeq={rec.recipe_seq} />}
    </div>
  )
}
