import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { AlertTriangle, RefreshCw, Search, ShieldCheck } from 'lucide-react'
import clsx from 'clsx'
import { ApiError, api } from '../../api/client'
import { splitReviewPath, type CapabilityReviewItem } from '../../api/capabilities'
import { EmptyState, ErrorState, Field, JsonInput, Skeleton, Spinner, rovingTarget, toast } from '../../components/ui'
import { useOnReconnect } from '../../store/catalog'
import { formatDateTime, formatNumber } from '../../lib/format'
import { CAPABILITY_EFFECT_LABEL, CAPABILITY_REVIEW_TEXT as RT, CAPABILITY_TEXT as T } from '../../lib/terms'
import { useAtLeast } from '../../store/auth'
import type { CatalogReviewItem, CatalogTableRow } from '../../types'
import type { CapabilityEffect, CapabilityRow, EndpointRow } from '../../typesApp'
import { ReviewOverview, ReviewQueue, type ReviewSource } from '../catalog/ReviewQueue'
import { itemKey, reviewParamsOf, writeReviewParams, type CatalogView } from '../catalog/review'

/*
 * 工具页的「能力」页签（A6、§3.9）：图里每个可调用的工具在这里登记读写效果和治理设置。
 *
 * 系统同步的部分（名字、所属系统、参数、同步得出的效果）只读；治理部分（效果、审批、无人值守写入、返回结构、
 * 表格化、字段语义、遮罩、凭据方式、端点、查询能力）由管理员维护，带着读到的 version 保存（乐观锁）：别人刚改过时
 * 服务端回 409，这里载入最新内容、请人重新改，不覆盖别人的修改。搭建者只读（§3.12）
 */

const EFFECTS: CapabilityEffect[] = ['read', 'write', 'dynamic']

/** 效果徽标：治理后的值优先，没治理过按同步得出的；两者不一致时标出来 */
function EffectChip({ row }: { row: CapabilityRow }) {
  const effect = row.effect ?? row.effect_system
  const color = effect === 'write' ? 'var(--st-failed)' : effect === 'dynamic' ? 'var(--st-waiting)' : 'var(--ok)'
  const mismatch = row.effect != null && row.effect !== row.effect_system
  return (
    <span className="chip shrink-0" style={{ color, borderColor: color }} data-capability-effect={effect}
          title={mismatch ? T.effectMismatch(CAPABILITY_EFFECT_LABEL[row.effect_system], CAPABILITY_EFFECT_LABEL[row.effect!])
            : row.effect == null ? T.effectUnset : undefined}>
      {CAPABILITY_EFFECT_LABEL[effect]}{mismatch ? ' *' : ''}
    </span>
  )
}

/*
 * 待审阅（D-i）：能力字段语义沿用数据目录「项」的状态（推断 / 已确认 / 已驳回），列表和成批审阅复用数据目录的审阅队列
 * （pages/catalog/ReviewQueue），只换数据来源和说「表」的那几句：队列的一项是某个能力返回里某个位置的某个字段，「表」一栏
 * 放能力名，乐观锁用能力的治理版本。视图记在地址里（view=review、rtable 只看一个能力、item 打开的那一项），说法同数据目录
 */

/** 能力的一项 → 审阅队列认的形状（表名一栏放能力名，位置用服务端写好的「返回字段 /amount 的单位」） */
const asReviewItem = (i: CapabilityReviewItem): CatalogReviewItem => ({
  table_name: i.name, path: i.path, field: i.field, field_label: i.where, column: null,
  value: i.value, display: i.display, source: i.source, status: i.status, note: i.note, priority: i.priority,
  reasons: i.reasons, table_usage: i.usage, version: i.version,
})

/** 模块级一份：审阅队列按它是否换了决定要不要重新取 */
const REVIEW_SOURCE: ReviewSource = {
  queue: async ({ limit, offset, table }) => {
    const q = await api.capabilities.reviewQueue({ limit, offset, capability: table })
    return { total: q.total, items: q.items.map(asReviewItem) }
  },
  batch: async (items) => {
    const { results } = await api.capabilities.reviewBatch(items.map((i) => ({
      name: i.table_name, path: i.path, action: i.action, if_version: i.if_version,
    })))
    return {
      results: results.map((r) => ({
        table_name: r.name, path: r.path, ok: r.ok, version: r.version ?? null, error: r.error, code: r.code ?? null,
      })),
    }
  },
}

/** 一个能力的字段语义里还是推断的几项：按能力筛选的候选、视图标签上的计数 */
function proposedOf(row: CapabilityRow): number {
  let n = 0
  for (const slot of Object.values(row.fields ?? {})) {
    for (const item of Object.values(slot ?? {})) {
      if (item && typeof item === 'object' && (item as { status?: string }).status === 'proposed') n++
    }
  }
  return n
}

const VIEWS: CatalogView[] = ['list', 'review']

export function CapabilitiesTab() {
  const { id } = useParams()
  const navigate = useNavigate()
  const [search, setSearch] = useSearchParams()
  const review = reviewParamsOf(search)
  const inReview = review.view === 'review'
  // 成批审阅只给管理员（POST /api/capabilities/review/batch），搭建者只看（§3.12）
  const admin = useAtLeast('admin')
  const [rows, setRows] = useState<CapabilityRow[] | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [query, setQuery] = useState('')
  const [effect, setEffect] = useState('')
  const [system, setSystem] = useState('')
  const [syncing, setSyncing] = useState(false)

  const load = useCallback(async () => {
    try {
      const { items } = await api.capabilities.list()
      setRows(items)
      setError(null)
    } catch (e) {
      setError(e)
    }
  }, [])
  useEffect(() => { void load() }, [load])
  useOnReconnect(load)

  const sync = async () => {
    setSyncing(true)
    try {
      const { changed } = await api.capabilities.sync()
      toast.ok(T.synced(changed))
      await load()
    } catch (e) {
      toast.error(e)
    } finally {
      setSyncing(false)
    }
  }

  /** 换视图：记进地址（后退回到上一个视图），右栏正在看的能力不动 */
  const setView = (v: CatalogView) => setSearch((cur) => writeReviewParams(cur, { view: v, item: '' }))
  const setReviewParams = useCallback((p: { table?: string }) => {
    setSearch((cur) => writeReviewParams(cur, p), { replace: true })
  }, [setSearch])
  /** 从待审阅打开一项：右栏是这个能力的治理设置，地址带着这一项 */
  const openReviewItem = useCallback((i: CatalogReviewItem) => {
    const q = writeReviewParams(new URLSearchParams(window.location.search), { view: 'review', item: i.path }).toString()
    navigate(`/tools/capabilities/${encodeURIComponent(i.table_name)}${q ? `?${q}` : ''}`)
  }, [navigate])
  const onReviewed = useCallback(() => { void load() }, [load])

  const systems = useMemo(() => [...new Set((rows ?? []).map((r) => r.system))].sort(), [rows])
  // 按能力筛选的候选、能力的中文名：审阅队列认数据目录清单那一行的形状，只用到名字、中文名和推断的项数
  const reviewRows = useMemo(() => (rows ?? []).map((r) => ({
    table_name: r.name, label: r.title || null, counts: { proposed: proposedOf(r) },
  }) as unknown as CatalogTableRow), [rows])
  const proposed = useMemo(() => (rows ?? []).reduce((n, r) => n + proposedOf(r), 0), [rows])
  const needle = query.trim().toLowerCase()
  const shown = (rows ?? []).filter((r) =>
    (!needle || [r.name, r.title, r.system].some((x) => x?.toLowerCase().includes(needle)))
    && (!effect || (r.effect ?? r.effect_system) === effect)
    && (!system || r.system === system))
  const picked = rows?.find((r) => r.name === id) ?? null
  const latest = (rows ?? []).reduce<string | null>((m, r) => (r.synced_at && (!m || r.synced_at > m) ? r.synced_at : m), null)
  const activeKey = inReview && picked && review.item ? itemKey({ table_name: picked.name, path: review.item }) : null

  if (!rows) {
    return error ? <ErrorState error={error} onRetry={() => void load()} className="m-4" />
      : <div className="p-4"><Skeleton rows={4} height={44} gap={8} /></div>
  }

  return (
    <div className="flex h-full" data-capabilities="" data-capability-view={review.view}>
      <div className="flex w-[340px] shrink-0 flex-col border-r">
        <ViewTabs view={review.view} proposed={proposed} onChange={setView} />
        {inReview ? (
          <ReviewQueue source={REVIEW_SOURCE} text={RT} kinds={false} whereOf={(i) => i.field_label}
                       rows={reviewRows} kind="all" table={review.table} activeKey={activeKey} detailSync={null}
                       onParams={setReviewParams} onOpenItem={openReviewItem} onChanged={onReviewed}
                       readOnly={!admin} />
        ) : (
          <>
            <div className="space-y-1.5 border-b px-3 py-2">
              <div className="flex items-center gap-2">
                <Search size={12} className="text-faint" aria-hidden />
                <input className="min-w-0 flex-1 bg-transparent text-xs outline-none placeholder:text-faint"
                       placeholder={T.search} aria-label={T.search} value={query} onChange={(e) => setQuery(e.target.value)} />
                <button type="button" className="btn btn-xs" disabled={syncing} onClick={() => void sync()} data-capability-sync=""
                        title={latest ? T.syncedAt(formatDateTime(latest)) : undefined}>
                  {syncing ? <Spinner size={10} /> : <RefreshCw size={10} aria-hidden />} {T.sync}
                </button>
              </div>
              <div className="flex gap-1.5">
                <select className="field h-7 min-w-0 flex-1 py-0 text-xs" value={effect} aria-label={T.filterEffect}
                        onChange={(e) => setEffect(e.target.value)}>
                  <option value="">{T.filterEffect}</option>
                  {EFFECTS.map((k) => <option key={k} value={k}>{CAPABILITY_EFFECT_LABEL[k]}</option>)}
                </select>
                <select className="field h-7 min-w-0 flex-1 py-0 text-xs" value={system} aria-label={T.filterSystem}
                        onChange={(e) => setSystem(e.target.value)}>
                  <option value="">{T.filterSystem}</option>
                  {systems.map((x) => <option key={x} value={x}>{x}</option>)}
                </select>
              </div>
            </div>
            <nav className="flex-1 overflow-y-auto p-2" aria-label={T.title}>
              {shown.map((r) => {
                const on = picked?.name === r.name
                return (
                  <button key={r.name} type="button" aria-current={on ? 'true' : undefined} data-capability={r.name}
                          onClick={() => navigate(`/tools/capabilities/${encodeURIComponent(r.name)}`)}
                          className={clsx('relative w-full rounded-md px-2 py-1.5 text-left hover:bg-hover', on && 'bg-hover')}>
                    {on && <span className="absolute inset-y-1.5 left-0 w-0.5 rounded bg-[var(--accent)]" aria-hidden />}
                    <div className="flex items-center gap-1.5">
                      <span className={clsx('mono min-w-0 flex-1 truncate text-xs', r.missing && 'text-faint line-through')}>{r.name}</span>
                      <EffectChip row={r} />
                    </div>
                    <div className="truncate text-2xs text-faint">{r.system} · {r.title || r.description}</div>
                  </button>
                )
              })}
              {!shown.length && (
                <EmptyState offline={false} icon={<ShieldCheck size={20} />}
                            title={rows.length ? '没有匹配的能力' : T.empty} body={rows.length ? undefined : T.emptyBody} />
              )}
            </nav>
          </>
        )}
      </div>
      <div className="min-w-0 flex-1 overflow-y-auto p-4">
        {picked
          ? <CapabilityEditor key={`${picked.name}@${picked.version}`} row={picked} all={rows}
                              focus={inReview && review.item ? splitReviewPath(review.item).pointer : ''}
                              onSaved={(next) => setRows((rs) => rs && rs.map((r) => (r.name === next.name ? next : r)))}
                              onConflict={() => void load()} />
          : inReview ? <ReviewOverview text={RT} kinds={false} />
          : <EmptyState icon={<ShieldCheck size={22} />} title={T.title} body={T.hint} />}
      </div>
    </div>
  )
}

/** 左栏上方的两个视图：全部能力、待审阅（带着还有几项待确认）。标签页语义，←→ Home End 切换，同数据目录 */
function ViewTabs({ view, proposed, onChange }: { view: CatalogView; proposed: number; onChange: (v: CatalogView) => void }) {
  const refs = useRef<Record<string, HTMLButtonElement | null>>({})
  const move = (e: ReactKeyboardEvent, i: number) => {
    const j = rovingTarget(e, i, VIEWS.length, 'horizontal')
    if (j < 0) return
    e.preventDefault()
    onChange(VIEWS[j])
    refs.current[VIEWS[j]]?.focus()
  }
  return (
    <div className="flex shrink-0 gap-1 border-b px-2" role="tablist" aria-label={RT.viewLabel} data-capability-views={view}>
      {VIEWS.map((v, i) => {
        const selected = view === v
        return (
          <button key={v} ref={(el) => { refs.current[v] = el }} type="button" role="tab" aria-selected={selected}
                  tabIndex={selected ? 0 : -1} title={RT.viewHint[v]} data-capability-view-tab={v}
                  onClick={() => onChange(v)} onKeyDown={(e) => move(e, i)}
                  className={clsx('relative px-3 py-2 text-xs outline-none transition-colors focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--accent)]',
                    selected ? 'text-fg' : 'text-faint hover:text-dim')}>
            {RT.view[v]}
            {v === 'review' && proposed > 0 && <span className="tnum ml-1.5 text-2xs text-faint">{formatNumber(proposed)}</span>}
            {selected && <span className="absolute inset-x-2 -bottom-px h-0.5 rounded bg-[var(--accent)]" aria-hidden />}
          </button>
        )
      })}
    </div>
  )
}

/** 治理字段：编辑中的一份，保存时只带这些 */
type Governance = Pick<CapabilityRow, 'effect' | 'approval' | 'unattended_write' | 'returns_schema' | 'tabular' | 'fields'
  | 'masked' | 'credential_mode' | 'endpoint_id' | 'lookup'>

const governanceOf = (r: CapabilityRow): Governance => ({
  effect: r.effect, approval: r.approval, unattended_write: r.unattended_write, returns_schema: r.returns_schema,
  tabular: r.tabular, fields: r.fields, masked: r.masked, credential_mode: r.credential_mode, endpoint_id: r.endpoint_id,
  lookup: r.lookup,
})

function CapabilityEditor({ row, all, focus = '', onSaved, onConflict }: {
  row: CapabilityRow; all: CapabilityRow[]
  /** 从待审阅打开的那一项在返回里的位置（JSON 指针）：在字段语义旁边标出来 */
  focus?: string
  onSaved: (row: CapabilityRow) => void
  onConflict: () => void
}) {
  const admin = useAtLeast('admin')
  const [form, setForm] = useState<Governance>(() => governanceOf(row))
  const [masked, setMasked] = useState(row.masked.join('\n'))
  const [busy, setBusy] = useState(false)
  const [endpoints, setEndpoints] = useState<EndpointRow[]>([])
  useEffect(() => { api.endpoints.list().then(({ items }) => setEndpoints(items)).catch(() => undefined) }, [])
  const set = <K extends keyof Governance>(k: K, v: Governance[K]) => setForm((f) => ({ ...f, [k]: v }))
  const effect = form.effect ?? row.effect_system
  const writes = effect !== 'read'
  const dirty = JSON.stringify({ ...form, masked: masked.split('\n').map((x) => x.trim()).filter(Boolean) })
    !== JSON.stringify(governanceOf(row))
  const readonly = !admin

  const save = async () => {
    setBusy(true)
    try {
      const next = await api.capabilities.govern(row.name, {
        ...form, masked: masked.split('\n').map((x) => x.trim()).filter(Boolean), version: row.version,
      })
      toast.ok(T.saved(row.name))
      onSaved(next)
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        toast.warn(T.conflict)
        onConflict()
      } else {
        toast.error(e)
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mx-auto max-w-3xl space-y-4" data-capability-editor={row.name}>
      <div>
        <div className="flex items-center gap-2">
          <h2 className="mono min-w-0 flex-1 truncate text-sm font-semibold">{row.name}</h2>
          <span className="tnum text-2xs text-faint">{T.version(row.version)}</span>
        </div>
        <p className="mt-0.5 text-xs text-dim">{row.title}{row.description ? ` · ${row.description}` : ''}</p>
        <p className="mt-0.5 text-2xs text-faint">
          {row.system} · {T.effectSystem}「{CAPABILITY_EFFECT_LABEL[row.effect_system]}」
          {row.updated_by ? ` · 最近由「${row.updated_by}」修改` : ''}
        </p>
        {row.missing && (
          <div className="mt-2 flex items-start gap-1.5 text-2xs" style={{ color: 'var(--st-waiting)' }}>
            <AlertTriangle size={12} className="mt-px shrink-0" aria-hidden /> {T.missing}：{T.missingHint}
          </div>
        )}
        {readonly && <p className="mt-2 text-2xs text-faint" data-capability-readonly="">{T.readOnlyHint}</p>}
      </div>

      <fieldset disabled={readonly || busy} className="space-y-3">
        <div className="grid grid-cols-2 gap-3">
          <Field label={`${T.effectGoverned}的效果`} hint={form.effect == null ? T.effectUnset : undefined}>
            {(p) => (
              <select {...p} className="field" value={form.effect ?? ''}
                      onChange={(e) => set('effect', (e.target.value || null) as CapabilityEffect | null)}>
                <option value="">{T.effectUnset}</option>
                {EFFECTS.map((k) => <option key={k} value={k}>{CAPABILITY_EFFECT_LABEL[k]}</option>)}
              </select>
            )}
          </Field>
          <Field label="审批" hint={T.approvalHint}>
            {(p) => (
              <select {...p} className="field" value={form.approval}
                      onChange={(e) => set('approval', e.target.value as Governance['approval'])}>
                {Object.entries(T.approval).map(([k, label]) => (
                  <option key={k} value={k} disabled={k === 'none' && effect !== 'read'}>{label}</option>
                ))}
              </select>
            )}
          </Field>
        </div>
        {writes && (
          <label className="flex items-start gap-2 text-xs">
            <input type="checkbox" className="mt-0.5 accent-[var(--accent)]" checked={form.unattended_write}
                   onChange={(e) => set('unattended_write', e.target.checked)} data-capability-unattended="" />
            <span>{T.unattended}<span className="mt-0.5 block text-2xs text-faint">{T.unattendedHint}</span></span>
          </label>
        )}
        <div className="grid grid-cols-2 gap-3">
          <Field label={T.credentialLabel} hint={T.credentialHint}>
            {(p) => (
              <select {...p} className="field" value={form.credential_mode}
                      onChange={(e) => set('credential_mode', e.target.value as Governance['credential_mode'])}>
                {Object.entries(T.credential).map(([k, label]) => <option key={k} value={k}>{label}</option>)}
              </select>
            )}
          </Field>
          <Field label={T.endpoint}>
            {(p) => (
              <select {...p} className="field" value={form.endpoint_id ?? ''}
                      onChange={(e) => set('endpoint_id', e.target.value || null)}>
                <option value="">{T.endpointNone}</option>
                {endpoints.map((ep) => <option key={ep.id} value={ep.id}>{ep.name}</option>)}
                {form.endpoint_id && !endpoints.some((ep) => ep.id === form.endpoint_id) && (
                  <option value={form.endpoint_id} disabled>{form.endpoint_id}（已不存在）</option>
                )}
              </select>
            )}
          </Field>
        </div>
        {writes && (
          <Field label={T.lookup} hint={T.lookupHint}>
            {(p) => (
              <select {...p} className="field" value={form.lookup ?? ''} onChange={(e) => set('lookup', e.target.value || null)}>
                <option value="">{T.lookupNone}</option>
                {all.filter((c) => c.name !== row.name && (c.effect ?? c.effect_system) === 'read').map((c) => (
                  <option key={c.name} value={c.name}>{c.name}</option>
                ))}
              </select>
            )}
          </Field>
        )}
        <Field label={T.tabular} hint={T.tabularHint}>
          {(p) => (
            <div className="grid grid-cols-[1fr_2fr] gap-2">
              <input {...p} className="field mono text-xs" placeholder="/data/items" aria-label={T.tabularRows}
                     value={form.tabular?.rows ?? ''}
                     onChange={(e) => set('tabular', e.target.value
                       ? { rows: e.target.value, columns: form.tabular?.columns ?? {} } : null)} />
              <JsonInput value={form.tabular?.columns ?? null} rows={3} placeholder='{"order_id": "/id", "amount": "/amount"}'
                         onChange={(v) => set('tabular', form.tabular?.rows || v
                           ? { rows: form.tabular?.rows ?? '', columns: (v ?? {}) as Record<string, string> } : null)} />
            </div>
          )}
        </Field>
        <Field label={T.returns}>
          {(p) => <JsonInput id={p.id} value={form.returns_schema} rows={4} onChange={(v) => set('returns_schema', v ?? null)} />}
        </Field>
        <Field label={T.fields} hint={focus ? `${T.fieldsHint}。${RT.focusMark}：${focus}` : T.fieldsHint}>
          {(p) => (
            <div data-capability-fields="" data-capability-focus={focus || undefined}>
              <JsonInput id={p.id} value={form.fields} rows={focus ? 8 : 4} onChange={(v) => set('fields', v ?? {})} />
            </div>
          )}
        </Field>
        <Field label={T.masked} hint={T.maskedHint}>
          {(p) => <textarea {...p} className="field mono text-xs" rows={3} value={masked} placeholder="/data/items/*/phone"
                            onChange={(e) => setMasked(e.target.value)} data-capability-masked="" />}
        </Field>
      </fieldset>

      {!readonly && (
        <div className="flex justify-end gap-2 border-t pt-3">
          <button type="button" className="btn" disabled={!dirty || busy}
                  onClick={() => { setForm(governanceOf(row)); setMasked(row.masked.join('\n')) }}>放弃修改</button>
          <button type="button" className="btn btn-primary" disabled={!dirty || busy} onClick={() => void save()}
                  data-capability-save="">
            {busy ? <Spinner size={11} /> : null} 保存
          </button>
        </div>
      )}

      <details className="rounded-lg border">
        <summary className="cursor-pointer px-3 py-2 text-2xs text-dim">参数结构（系统同步，只读）</summary>
        <pre className="mono max-h-64 overflow-auto border-t p-3 text-2xs">{JSON.stringify(row.params_schema, null, 2)}</pre>
      </details>
    </div>
  )
}
