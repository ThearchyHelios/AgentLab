import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { AlertTriangle, RefreshCw, Search, ShieldCheck } from 'lucide-react'
import clsx from 'clsx'
import { ApiError, api } from '../../api/client'
import { EmptyState, ErrorState, Field, JsonInput, Skeleton, Spinner, toast } from '../../components/ui'
import { useOnReconnect } from '../../store/catalog'
import { formatDateTime } from '../../lib/format'
import { CAPABILITY_EFFECT_LABEL, CAPABILITY_TEXT as T } from '../../lib/terms'
import { useAtLeast } from '../../store/auth'
import type { CapabilityEffect, CapabilityRow, EndpointRow } from '../../typesApp'

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

export function CapabilitiesTab() {
  const { id } = useParams()
  const navigate = useNavigate()
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

  const systems = useMemo(() => [...new Set((rows ?? []).map((r) => r.system))].sort(), [rows])
  const needle = query.trim().toLowerCase()
  const shown = (rows ?? []).filter((r) =>
    (!needle || [r.name, r.title, r.system].some((x) => x?.toLowerCase().includes(needle)))
    && (!effect || (r.effect ?? r.effect_system) === effect)
    && (!system || r.system === system))
  const picked = rows?.find((r) => r.name === id) ?? null
  const latest = (rows ?? []).reduce<string | null>((m, r) => (r.synced_at && (!m || r.synced_at > m) ? r.synced_at : m), null)

  if (!rows) {
    return error ? <ErrorState error={error} onRetry={() => void load()} className="m-4" />
      : <div className="p-4"><Skeleton rows={4} height={44} gap={8} /></div>
  }

  return (
    <div className="flex h-full" data-capabilities="">
      <div className="flex w-[340px] shrink-0 flex-col border-r">
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
      </div>
      <div className="min-w-0 flex-1 overflow-y-auto p-4">
        {picked
          ? <CapabilityEditor key={`${picked.name}@${picked.version}`} row={picked} all={rows}
                              onSaved={(next) => setRows((rs) => rs && rs.map((r) => (r.name === next.name ? next : r)))}
                              onConflict={() => void load()} />
          : <EmptyState icon={<ShieldCheck size={22} />} title={T.title} body={T.hint} />}
      </div>
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

function CapabilityEditor({ row, all, onSaved, onConflict }: {
  row: CapabilityRow; all: CapabilityRow[]
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
        <Field label={T.fields} hint={T.fieldsHint}>
          {(p) => <JsonInput id={p.id} value={form.fields} rows={4} onChange={(v) => set('fields', v ?? {})} />}
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
