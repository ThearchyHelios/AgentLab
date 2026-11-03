/**
 * 口径目录（/ledger/calibers）：已发布口径的入口、指标、依赖和试跑。
 *
 * 只读目录 + 参数表单 + 一次只读探索运行。口径的技术身份是「工作流@版本/口径卡」；目录给业务的人看的是名字、指标、
 * 入口字段和能不能用。试跑按固定版本派生只读图（服务端 ledger/derive.caliber_probe），不是正式出具：来源记 trial，
 * 不进使用统计。SQL 不在页面上出现——服务端 descriptor 本来就不返回。
 */
import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { ChevronDown, Layers, Play, RefreshCw, Search } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../../api/client'
import { SchemaForm } from '../../components/SchemaForm'
import { EmptyState, ErrorState, Notice, PageHeader, Skeleton, Spinner, toast } from '../../components/ui'
import { LEDGER_TEXT } from '../../lib/terms'
import type { FormSchema } from '../../typesApp'
import type { CaliberDescriptor, CaliberTrialOut } from '../../typesLedger'

const keyOf = (d: CaliberDescriptor) => `${d.workflow_id}@${d.version}/${d.card.node_id}`

function defaultsOf(schema: FormSchema): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const [name, field] of Object.entries(schema.properties ?? {})) {
    if (field?.default !== undefined && field.default !== null && field.default !== '') out[name] = field.default
  }
  return out
}

export function CaliberCatalog() {
  const [items, setItems] = useState<CaliberDescriptor[] | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState<string | null>(null)
  const [values, setValues] = useState<Record<string, Record<string, unknown>>>({})
  const [trials, setTrials] = useState<Record<string, CaliberTrialOut>>({})
  const [busy, setBusy] = useState<string | null>(null)

  const load = () => {
    setError(null)
    setItems(null)
    api.caliberLedger.catalog().then(
      (body) => setItems(body.calibers),
      (e) => setError(e),
    )
  }
  useEffect(load, [])

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!items || !q) return items ?? []
    return items.filter((d) => [d.workflow_name, d.card.caliber, d.card.title,
      ...d.metrics.map((m) => m.name ?? m.id)].some((s) => String(s ?? '').toLowerCase().includes(q)))
  }, [items, query])

  const toggle = (d: CaliberDescriptor) => {
    const key = keyOf(d)
    if (open === key) { setOpen(null); return }
    setOpen(key)
    setValues((v) => (v[key] ? v : { ...v, [key]: defaultsOf(d.entry_schema) }))
  }

  const trial = async (d: CaliberDescriptor) => {
    const key = keyOf(d)
    setBusy(key)
    try {
      const out = await api.caliberLedger.trial(d.workflow_id, d.version, d.card.node_id, values[key] ?? {})
      setTrials((t) => ({ ...t, [key]: out }))
      if (out.status === 'succeeded') toast.ok('试运行完成')
      else if (out.status === 'running') toast.warn('试运行还在进行，打开运行记录看结果')
      else toast.error(out.error || '试运行没有成功')
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="flex h-full flex-col" data-caliber-catalog="">
      <PageHeader icon={<Layers size={14} />} title="口径" subtitle="已发布口径的入口、指标、依赖与试运行"
                  actions={<button type="button" className="btn btn-xs btn-ghost" onClick={load} title="重新加载">
                    <RefreshCw size={12} aria-hidden /> 刷新
                  </button>} />
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-5xl space-y-3 p-4">
          <label className="relative block">
            <Search size={13} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-faint" aria-hidden />
            <input className="input w-full pl-8" value={query} onChange={(e) => setQuery(e.target.value)}
                   placeholder="按工作流、口径、指标名查找" aria-label="查找口径" />
          </label>
          {error ? <ErrorState error={error} onRetry={load} compact />
            : !items ? <Skeleton rows={5} height={38} />
            : !shown.length ? <EmptyState offline={false} icon={<Layers size={22} />} title="还没有可用的口径"
                                          body={query ? '换个关键词试试' : '在工作流里发布一份带口径卡的版本后，这里就会出现'} />
            : shown.map((d) => {
              const key = keyOf(d)
              const expanded = open === key
              const result = trials[key]
              return (
                <section key={key} className="overflow-hidden rounded-lg border bg-panel" data-caliber={key}>
                  <button type="button" className="flex w-full items-start gap-3 px-3 py-2.5 text-left hover:bg-hover"
                          onClick={() => toggle(d)} aria-expanded={expanded} data-caliber-toggle="">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                        <span className="text-sm font-medium">{d.workflow_name}</span>
                        <span className="text-2xs text-faint">v{d.version}</span>
                        {d.card.caliber && <span className="text-xs text-dim">· {d.card.caliber}</span>}
                        <span className="rounded border px-1.5 py-0.5 text-[10px] text-dim">
                          {(LEDGER_TEXT.level as Record<string, string>)[d.level] ?? d.level}
                        </span>
                      </div>
                      <div className="mt-0.5 text-2xs text-faint">
                        {d.metrics.map((m) => m.name ?? m.id).join('、')}
                        {d.entry_schema.properties && Object.keys(d.entry_schema.properties).length > 0
                          ? ` · ${Object.keys(d.entry_schema.properties).length} 个入口字段` : ' · 没有入口字段'}
                        {d.dependencies.tables.length > 0 ? ` · 读 ${d.dependencies.tables.join('、')}` : ''}
                      </div>
                    </div>
                    <div className="flex shrink-0 items-center gap-2 text-[10px] text-faint">
                      <span>{d.composable ? '可组合' : '不可组合'}</span>
                      <span>{d.issuance.auto_issue ? '可自动出具' : '只可一键出具'}</span>
                      <ChevronDown size={14} className={clsx('transition-transform', expanded && 'rotate-180')} aria-hidden />
                    </div>
                  </button>
                  {expanded && (
                    <div className="space-y-3 border-t px-3 py-3" data-caliber-detail="">
                      <div className="grid gap-3 md:grid-cols-2">
                        <div>
                          <h3 className="mb-1 text-2xs font-semibold text-dim">指标</h3>
                          <ul className="space-y-1 text-xs">
                            {d.metrics.map((m) => (
                              <li key={m.id} className="border-b border-hairline pb-1 last:border-0">
                                <div className="flex items-baseline justify-between gap-2">
                                  <span>{m.name ?? m.id}</span>
                                  <span className="text-2xs text-faint">{m.unit || '—'}</span>
                                </div>
                                {m.definition?.text && (
                                  <p className="mt-0.5 text-2xs leading-relaxed text-faint">{m.definition.text}</p>
                                )}
                                {!!m.params?.length && (
                                  <p className="mt-0.5 text-2xs text-faint">
                                    参数：{m.params.map((p) => `${p.name}（${p.type}）`).join('、')}
                                  </p>
                                )}
                              </li>
                            ))}
                          </ul>
                        </div>
                        <div>
                          <h3 className="mb-1 text-2xs font-semibold text-dim">依赖</h3>
                          <p className="text-2xs leading-relaxed text-dim">
                            数据源：{d.dependencies.sources.join('、') || '—'}<br />
                            表：{d.dependencies.tables.join('、') || '—'}<br />
                            {d.compose_reason ? `不能组合：${d.compose_reason}` : '可以按这一版组合'}
                          </p>
                          {d.issuance.reasons.length > 0 && (
                            <p className="mt-1 text-2xs leading-relaxed text-faint">
                              出具：{d.issuance.reasons.join('；')}
                            </p>
                          )}
                        </div>
                      </div>
                      <div>
                        <h3 className="mb-1 text-2xs font-semibold text-dim">入口字段</h3>
                        {d.entry_schema.properties && Object.keys(d.entry_schema.properties).length > 0 ? (
                          <SchemaForm schema={d.entry_schema} value={values[key] ?? {}}
                                      onChange={(v) => {
                                        setValues((all) => ({ ...all, [key]: v }))
                                        setTrials((all) => {
                                          const next = { ...all }
                                          delete next[key]
                                          return next
                                        })
                                      }}
                                      idPrefix={`caliber-${key}`} />
                        ) : <p className="text-2xs text-faint">这份口径不需要参数。</p>}
                      </div>
                      <div className="flex flex-wrap items-center gap-2">
                        <button type="button" className="btn btn-primary btn-sm" disabled={busy === key}
                                onClick={() => void trial(d)} data-caliber-trial="">
                          {busy === key ? <Spinner size={12} /> : <Play size={12} aria-hidden />} 试运行
                        </button>
                        <Link to={`/studio/${d.workflow_id}`} className="btn btn-sm btn-ghost">打开工作流</Link>
                        <span className="text-2xs text-faint">试运行是只读探索运行，不是正式出具</span>
                      </div>
                      {result && (
                        <div className="rounded-md border bg-elev px-3 py-2 text-xs" data-caliber-trial-result={result.status}>
                          {result.status === 'succeeded' ? (
                            <ul className="space-y-1">
                              {result.metrics.map((m) => (
                                <li key={m.id} className="flex items-baseline justify-between gap-2">
                                  <span>{m.name ?? m.id}</span>
                                  <span className="tnum font-medium">{m.rendered ?? String(m.value ?? '—')}{m.unit ? ` ${m.unit}` : ''}</span>
                                </li>
                              ))}
                            </ul>
                          ) : (
                            <Notice tone={result.status === 'running' ? 'info' : 'err'}>
                              {result.status === 'running' ? '还在运行：' : '试运行没有成功：'}
                              {result.error || result.status}
                              {' '}<Link className="underline" to={`/runs/${result.run_id}`}>看运行</Link>
                            </Notice>
                          )}
                        </div>
                      )}
                    </div>
                  )}
                </section>
              )
            })}
        </div>
      </div>
    </div>
  )
}
