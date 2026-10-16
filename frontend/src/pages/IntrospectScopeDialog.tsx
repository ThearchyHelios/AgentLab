import { useEffect, useMemo, useRef, useState } from 'react'
import { AlertTriangle, Info, RefreshCw } from 'lucide-react'
import { api } from '../api/client'
import type { IntrospectScope, IntrospectScopeRules } from '../types'
import { Modal, Spinner, toast } from '../components/ui'
import { formatDateTime, formatNumber } from '../lib/format'
import { INTROSPECT_SCOPE_TEXT as ST } from '../lib/terms'

// ===========================================================================
// 探查范围（docs/design/next-round/03-数据接入与目录维护.md A1）：几千张表的库只完整读取范围内的表。
//
// 规则写在这里，存在数据源上（options.introspect_scope，只经 PUT /introspect-scope 改，数据源表单不碰它）。命中数是服务端
// 对着存着的对象索引现数的（POST /introspect-scope/preview，不连库）：边改边看命中多少张、超没超上限，超了的话探查会被
// 拒绝——服务端不悄悄截断，界面也提前说。还没有对象索引的源先「刷新对象索引」（一条数据字典查询，只看不存表结构）。
// 界面不算命中：规则怎么匹配、哪些表一律在范围内都只在服务端一处（界面层不放逻辑）。
// ===========================================================================

const lines = (text: string): string[] => [...new Set(text.split('\n').map((x) => x.trim()).filter(Boolean))]
const joined = (items: string[] | undefined): string => (items ?? []).join('\n')

interface Draft {
  mode: 'all' | 'rules'
  include: string
  exclude: string
  tables: string
  removed: string
  views: boolean
  max: string
}

const draftOf = (rules: IntrospectScopeRules): Draft => ({
  mode: rules.mode, include: joined(rules.include), exclude: joined(rules.exclude), tables: joined(rules.tables),
  removed: joined(rules.removed), views: rules.views, max: String(rules.max_tables),
})

const rulesOf = (d: Draft): IntrospectScopeRules => ({
  mode: d.mode, include: lines(d.include), exclude: lines(d.exclude), tables: lines(d.tables), removed: lines(d.removed),
  views: d.views, max_tables: Number.parseInt(d.max, 10),
})

export function IntrospectScopeDialog({ source, onClose, onSaved, onRun }: {
  source: { id: string; name: string }
  onClose: () => void
  /** 保存之后（数据源上的规则变了：卡片要重新取一次） */
  onSaved: () => void
  /** 「保存并探查」：保存完由卡片开始探查 */
  onRun: () => void
}) {
  const [data, setData] = useState<IntrospectScope | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [preview, setPreview] = useState<IntrospectScope['preview']>(null)
  const [counting, setCounting] = useState(false)
  const [saving, setSaving] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState('')
  const initial = useRef('')

  const load = async () => {
    const got = await api.datasources.introspectScope(source.id)
    setData(got)
    setPreview(got.preview)
    const d = draftOf(got.scope)
    initial.current = JSON.stringify(rulesOf(d))
    setDraft(d)
  }
  useEffect(() => { load().catch((e) => setError(String((e as Error).message ?? e))) }, [source.id])

  const limit = data?.defaults.max_limit ?? 2000
  const maxValue = draft ? Number.parseInt(draft.max, 10) : NaN
  const maxBad = !draft || !Number.isInteger(maxValue) || maxValue < 1 || maxValue > limit
  const rules = useMemo(() => (draft && !maxBad ? rulesOf(draft) : null), [draft, maxBad])
  const dirty = !!rules && JSON.stringify(rules) !== initial.current

  // 边改边数：停手 400 毫秒再问服务端，旧的请求作废
  useEffect(() => {
    if (!rules || !data?.index.ready) return
    const ctl = new AbortController()
    const timer = setTimeout(() => {
      setCounting(true)
      api.datasources.previewIntrospectScope(source.id, rules, { signal: ctl.signal })
        .then((got) => { setPreview(got.preview); setError('') })
        .catch((e) => { if (!ctl.signal.aborted) setError(String((e as Error).message ?? e)) })
        .finally(() => { if (!ctl.signal.aborted) setCounting(false) })
    }, 400)
    return () => { ctl.abort(); clearTimeout(timer) }
  }, [JSON.stringify(rules), data?.index.ready])

  const refresh = async () => {
    setRefreshing(true)
    try {
      const got = await api.datasources.previewSchema(source.id)
      if (got.schema_error) toast.warn(got.schema_error)
      else toast.ok(ST.refreshed(got.table_count))
      await load()
    } catch (e) {
      toast.error(e)
    } finally {
      setRefreshing(false)
    }
  }

  const save = async (run: boolean) => {
    if (!rules) return
    setSaving(true)
    try {
      const got = await api.datasources.saveIntrospectScope(source.id, rules)
      setData(got)
      initial.current = JSON.stringify(rules)
      onSaved()
      toast.ok(ST.saved)
      if (run) onRun()
      onClose()
    } catch (e) {
      toast.error(e)
    } finally {
      setSaving(false)
    }
  }

  const set = (patch: Partial<Draft>) => setDraft((d) => (d ? { ...d, ...patch } : d))
  const area = (key: 'include' | 'exclude' | 'tables' | 'removed', label: string, hint: string, max: number) => {
    const n = draft ? lines(draft[key]).length : 0
    return (
      <label className="block space-y-1" data-scope-field={key}>
        <span className="flex items-baseline gap-2 text-xs font-medium">
          {label}<span className="flex-1" />
          <span className={n > max ? 'text-[var(--err)] tnum text-2xs' : 'tnum text-2xs text-faint'}>{ST.linesCount(n, max)}</span>
        </span>
        <textarea className="field mono min-h-[64px] text-2xs" rows={3} value={draft?.[key] ?? ''} spellCheck={false}
                  onChange={(e) => set({ [key]: e.target.value } as Partial<Draft>)} data-scope-input={key} />
        <span className="block text-2xs text-faint">{hint}</span>
      </label>
    )
  }

  const over = !!preview?.over
  return (
    <Modal open onClose={onClose} dirty={dirty} width={640} title={ST.title(source.name)}
           footer={<>
             <button className="btn" onClick={onClose}>{ST.cancel}</button>
             <button className="btn" disabled={saving || !rules || !dirty} onClick={() => void save(false)} data-scope-save="">
               {ST.save}
             </button>
             <button className="btn btn-primary" disabled={saving || !rules || over} onClick={() => void save(true)}
                     data-scope-save-run="">
               {saving ? <Spinner size={11} /> : null} {ST.saveAndRun}
             </button>
           </>}>
      {!data || !draft ? (
        error ? <p className="text-xs text-[var(--err)]" role="alert">{error}</p> : <Spinner size={14} />
      ) : (
        <div className="space-y-3 text-xs" data-scope-dialog="">
          <p className="leading-relaxed text-dim">{ST.intro(data.defaults.max_tables, data.defaults.max_limit)}</p>

          <div className="rounded-lg border px-3 py-2" role="status" data-scope-hits={preview ? `${preview.hits}/${preview.cap}` : 'none'}
               style={over ? { borderColor: 'color-mix(in srgb, var(--err) 40%, var(--border))' } : undefined}>
            {!data.index.ready ? (
              <div className="flex flex-wrap items-center gap-2">
                <Info size={12} className="text-[var(--accent)]" aria-hidden />
                <span className="min-w-0 flex-1">{ST.noIndex}</span>
                <button className="btn btn-sm" disabled={refreshing} onClick={() => void refresh()} title={ST.refreshHint}
                        data-scope-refresh="">
                  {refreshing ? <Spinner size={11} /> : <RefreshCw size={11} aria-hidden />} {refreshing ? ST.refreshing : ST.refreshIndex}
                </button>
              </div>
            ) : preview ? (
              <div className="space-y-0.5">
                <p className={over ? 'font-medium text-[var(--err)]' : 'font-medium'}>
                  {over && <AlertTriangle size={12} className="mr-1 inline" aria-hidden />}
                  {over ? ST.over(preview.hits, preview.cap) : ST.hits(preview.hits, preview.cap)}
                  {counting && <span className="ml-2 text-2xs font-normal text-faint">{ST.counting}</span>}
                </p>
                <p className="tnum text-2xs text-dim">{ST.breakdown(preview.rule, preview.explicit, preview.pinned)}</p>
                {preview.on_demand > 0 && <p className="tnum text-2xs text-dim">{ST.onDemandCount(preview.on_demand)}</p>}
                <p className="tnum text-2xs text-faint">{ST.outside(preview.out_of_scope, preview.index_total)}</p>
                <p className="flex flex-wrap items-center gap-2 text-2xs text-faint">
                  {data.index.at && <span>{ST.indexAt(formatDateTime(data.index.at))}</span>}
                  <button type="button" className="underline decoration-dotted underline-offset-2 hover:text-dim"
                          disabled={refreshing} onClick={() => void refresh()} title={ST.refreshHint} data-scope-refresh="">
                    {refreshing ? ST.refreshing : ST.refreshIndex}
                  </button>
                </p>
                {data.index.capped && <p className="text-2xs text-[var(--warn)]">{ST.indexCapped}</p>}
                {data.index.method === 'inspector' && <p className="text-2xs text-faint">{ST.inspector}</p>}
              </div>
            ) : <Spinner size={12} />}
          </div>

          <fieldset className="space-y-1">
            <legend className="text-xs font-medium">{ST.modeLabel}</legend>
            <div className="flex flex-wrap gap-3" role="radiogroup">
              {(['all', 'rules'] as const).map((m) => (
                <label key={m} className="flex items-center gap-1.5" title={ST.modeHint[m]}>
                  <input type="radio" name="scope-mode" checked={draft.mode === m} onChange={() => set({ mode: m })}
                         data-scope-mode={m} />
                  {ST.mode[m]}
                </label>
              ))}
            </div>
            <p className="text-2xs text-faint">{ST.modeHint[draft.mode]}</p>
          </fieldset>

          {draft.mode === 'rules' && area('include', ST.include, ST.includeHint, data.defaults.rules_max)}
          {area('exclude', ST.exclude, ST.excludeHint, data.defaults.rules_max)}
          {area('tables', ST.tables, ST.tablesHint, data.defaults.max_limit)}
          {area('removed', ST.removed, ST.removedHint, data.defaults.max_limit)}

          <div className="flex flex-wrap items-end gap-4">
            <label className="flex items-center gap-1.5">
              <input type="checkbox" checked={draft.views} onChange={(e) => set({ views: e.target.checked })} data-scope-views="" />
              {ST.views}
            </label>
            <label className="block space-y-1">
              <span className="text-xs font-medium">{ST.maxTables}</span>
              <input className="field tnum !w-28" inputMode="numeric" value={draft.max} aria-invalid={maxBad}
                     onChange={(e) => set({ max: e.target.value.replace(/[^\d]/g, '') })} data-scope-input="max_tables" />
            </label>
            <span className={maxBad ? 'pb-1 text-2xs text-[var(--err)]' : 'pb-1 text-2xs text-faint'}>{ST.maxTablesHint(limit)}</span>
          </div>

          <section className="space-y-1 rounded-lg border px-3 py-2" data-scope-pinned={preview?.pinned ?? 0}>
            <h4 className="text-xs font-medium">{ST.pinnedHead}{preview ? `（${formatNumber(preview.pinned)}）` : ''}</h4>
            <p className="text-2xs leading-relaxed text-dim">{ST.pinnedBody}</p>
            {!!data.pinned_sample.length && (
              <p className="mono text-2xs text-faint">
                {data.pinned_sample.join('、')}
                {(preview?.pinned ?? 0) > data.pinned_sample.length ? ` ${ST.pinnedMore(preview?.pinned ?? 0)}` : ''}
              </p>
            )}
          </section>

          {!!data.on_demand.length && (
            <section className="space-y-1 rounded-lg border px-3 py-2" data-scope-on-demand={data.on_demand.length}>
              <h4 className="text-xs font-medium">{ST.onDemandHead}（{formatNumber(data.on_demand.length)}）</h4>
              <p className="text-2xs leading-relaxed text-dim">{ST.onDemandBody}</p>
              <ul className="max-h-40 space-y-1 overflow-y-auto">
                {data.on_demand.map((t) => (
                  <li key={t.name} className="text-2xs" data-scope-trail={t.name}>
                    <span className="mono text-fg">{t.name}</span>
                    <span className="text-faint">
                      {' · '}{ST.onDemandVia[t.via ?? ''] ?? t.via}
                      {t.by ? ` · ${ST.onDemandBy(t.by)}` : ''}
                      {t.at ? ` · ${formatDateTime(t.at)}` : ''}
                    </span>
                    {t.need && <span className="block truncate text-faint" title={t.need}>{ST.onDemandNeed(t.need)}</span>}
                  </li>
                ))}
              </ul>
            </section>
          )}

          {data.scope.updated_at && (
            <p className="text-2xs text-faint">{ST.updated(data.scope.updated_by ?? null, formatDateTime(data.scope.updated_at))}</p>
          )}
          {error && <p className="text-2xs text-[var(--err)]" role="alert">{error}</p>}
        </div>
      )}
    </Modal>
  )
}
