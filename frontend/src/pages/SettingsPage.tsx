import { useEffect, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent } from 'react'
import { Navigate, useNavigate, useParams } from 'react-router-dom'
import {
  Check, Cpu, Download, KeyRound, Monitor, Moon, Plug, Plus, Settings as SettingsIcon, Sun, X,
} from 'lucide-react'
import clsx from 'clsx'
import { api } from '../api/client'
import type { ProviderDraft } from '../api/client'
import { useCatalog, useOnReconnect } from '../store/catalog'
import {
  confirmDialog, EmptyState, ErrorState, Field, isComposing, Modal, promptDialog, Skeleton, Spinner,
  TabPanel, Tabs, toast, useTabRoute,
} from '../components/ui'
import { humanizeError } from '../lib/errors'
import { formatNumber } from '../lib/format'
import type { Provider } from '../types'
import { applyTheme, normalizeTheme, readThemePref } from '../lib/theme'
import type { ThemePref } from '../lib/theme'
import {
  checkHealth, DeleteButton, forgetHealth, HealthPill, PageHeader, SectionBar, setHealth, shortLabel,
  useHealth, useRadioGroup, workflowList, workflowsMentioning,
} from './DataSourcesTab'
import type { HealthRecord } from './DataSourcesTab'

// 提到模块级：tab 名同时是 URL 的最后一段，两处各写一份迟早对不上。
// 数据源搬去了顶级的「数据」页（/data），/settings/datasources 跳过去
const TABS = [
  { key: 'providers', label: '模型接入' },
  { key: 'prefs', label: '偏好设置' },
  { key: 'system', label: '运行环境' },
]

export function SettingsPage() {
  const { tab: raw } = useParams()
  // datasources 也算认得：否则 useTabRoute 会先把地址纠正回 providers，和下面的跳转打架
  const [tab, setTab] = useTabRoute([...TABS.map((t) => t.key), 'datasources'], 'providers')
  // 偏好设置有没保存的改动时，切标签先问一句
  const [prefsDirty, setPrefsDirty] = useState(0)

  if (raw === 'datasources') return <Navigate to="/data" replace />

  const change = async (key: string) => {
    if (key === tab) return
    if (tab === 'prefs' && prefsDirty && !(await confirmLeave(prefsDirty))) return
    setTab(key)
  }

  return (
    <div className="flex h-full flex-col">
      <PageHeader icon={<SettingsIcon size={13} />} title="设置" subtitle="模型接入、偏好和运行环境" />
      <Tabs tabs={TABS} active={tab} onChange={(k) => void change(k)} label="设置" idPrefix="settings" />
      <TabPanel idPrefix="settings" tabKey={tab} className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-4xl p-4">
          {tab === 'providers' && <ProvidersTab />}
          {tab === 'prefs' && <PrefsTab onDirty={setPrefsDirty} />}
          {tab === 'system' && <SystemTab />}
        </div>
      </TabPanel>
    </div>
  )
}

const confirmLeave = (n: number) => confirmDialog({
  title: `有 ${n} 项设置还没保存`,
  body: '离开这一屏，这些改动就丢了。',
  confirmLabel: '放弃修改',
  cancelLabel: '留下来保存',
  danger: true,
})

// -------------------------------------------------------------------------

function ProvidersTab() {
  const providers = useCatalog((s) => s.providers)
  const refresh = useCatalog((s) => s.refresh)
  const workflows = useCatalog((s) => s.workflows)
  const loaded = useCatalog((s) => s.loaded)
  const [catalog, setCatalog] = useState<any>(null)
  const [editing, setEditing] = useState<Provider | 'new' | null>(null)

  const loadCatalog = () => api.providers.catalog().then(setCatalog, () => {})
  useEffect(() => { void loadCatalog() }, [])
  useOnReconnect(() => { if (!catalog) void loadCatalog() })

  const kindLabel = (kind: string) =>
    shortLabel(catalog?.kinds?.find((k: any) => k.kind === kind)?.label) || kind

  const remove = async (p: Provider) => {
    const ids = [...(p.models ?? []).map((m) => m.id), p.default_model ?? ''].filter(Boolean)
    const using = workflowsMentioning(workflows, ids)
    const lastEnabled = p.enabled && providers.filter((x) => x.enabled).length === 1
    const ok = await confirmDialog({
      title: `删除模型接入「${p.name}」？`,
      danger: true,
      consequences: [
        using.length
          ? `${workflowList(using)}的节点点名用了它的模型，运行时会找不到模型`
          : '眼下没有工作流点名用它的模型',
        lastEnabled ? '这是唯一启用的模型接入：删掉后没指定模型的节点都跑不起来' : '',
        p.has_key ? 'API Key 一并删除，不可恢复' : '',
      ].filter(Boolean),
      confirmLabel: '删除接入',
    })
    if (!ok) return
    try {
      await api.providers.remove(p.id)
      forgetHealth(`provider:${p.id}`)
      await refresh()
      toast.ok(`已删除模型接入「${p.name}」`)
    } catch (e) {
      toast.error(e)
    }
  }

  return (
    <div>
      <SectionBar title="模型接入" hint="API Key 加密后存在本地 SQLite，不会回传给浏览器。圆点说的是上次测连接的结果，启用与否另外标。">
        <button className="btn btn-primary btn-sm" onClick={() => setEditing('new')}>
          <Plus size={12} aria-hidden /> 添加接入
        </button>
      </SectionBar>

      {!loaded ? (
        <Skeleton rows={3} height={64} gap={8} />
      ) : !providers.length ? (
        <EmptyState
          icon={<KeyRound size={22} />}
          title="还没有配置模型"
          body="接一个 Anthropic、OpenAI，或任何 OpenAI 兼容的服务（DeepSeek、通义、本机 Ollama…），agent 才跑得起来。"
          action={<button className="btn btn-primary btn-sm" onClick={() => setEditing('new')}><Plus size={12} aria-hidden /> 添加接入</button>}
        />
      ) : (
        <div className="space-y-2.5">
          {providers.map((p) => (
            <ProviderCard key={p.id} provider={p} kindLabel={kindLabel(p.kind)}
                          onEdit={() => setEditing(p)} onRemove={() => void remove(p)} />
          ))}
        </div>
      )}

      {editing && (
        <ProviderEditor
          provider={editing === 'new' ? null : editing}
          catalog={catalog}
          onClose={() => setEditing(null)}
          onSaved={(saved, tested) => {
            setEditing(null)
            if (tested) setHealth(`provider:${saved.id}`, tested)
            void refresh()
            toast.ok(`已保存「${saved.name}」`)
          }}
        />
      )}
    </div>
  )
}

function ProviderCard({ provider: p, kindLabel, onEdit, onRemove }: {
  provider: Provider; kindLabel: string; onEdit: () => void; onRemove: () => void
}) {
  const key = `provider:${p.id}`
  const { record, checkingSince } = useHealth(key)
  const test = () => checkHealth(key, async () => {
    const r = await api.providers.test(p.id, { model: p.default_model ?? undefined })
    return {
      ok: !!r.ok, ms: r.latency_ms ?? null, error: r.error, hint: r.hint, detail: r.detail,
      note: r.ok ? `${r.model ?? p.default_model ?? ''} 回：${String(r.reply ?? '').slice(0, 120)}` : undefined,
    }
  })

  return (
    <article className="rounded-lg border bg-panel px-3 py-2.5" data-provider={p.name}>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
        <Cpu size={14} className={p.enabled ? 'text-dim' : 'text-faint'} aria-hidden />
        <span className="text-sm font-medium">{p.name}</span>
        <span className="chip">{kindLabel}</span>
        {p.default_model && <span className="chip mono" title="默认模型">{p.default_model}</span>}
        {!p.enabled && <span className="chip" title="停用的接入不会出现在模型下拉里">已停用</span>}
        <span className="flex-1" />
        <HealthPill record={record} checkingSince={checkingSince} />
        <div className="flex items-center gap-1">
          <button className="btn btn-sm" disabled={!!checkingSince} onClick={() => void test()}
                  title={`用 ${p.default_model || '默认模型'} 发一句话试试`}>
            <Plug size={11} aria-hidden /> 测试
          </button>
          <button className="btn btn-sm btn-ghost" onClick={onEdit}>编辑</button>
          <DeleteButton label={`删除模型接入 ${p.name}`} onClick={onRemove} />
        </div>
      </div>
      <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-2xs text-faint">
        {p.base_url && <span className="mono break-all">{p.base_url}</span>}
        {p.has_key && <span className="mono">key {p.api_key_masked}</span>}
        {(p.extra as any)?.auth_style === 'bearer' && <span>Bearer 认证</span>}
        <span className="tnum">{p.models?.length ?? 0} 个模型</span>
      </div>
      {record && !record.ok && !checkingSince && (
        <ErrorState compact error={record} onRetry={() => void test()} className="mt-2" />
      )}
      {record?.ok && record.note && !checkingSince && (
        <p className="mt-1.5 truncate text-2xs text-faint" title={record.note}>{record.note}</p>
      )}
    </article>
  )
}

interface ProviderForm {
  name: string
  kind: string
  base_url: string
  api_key: string
  default_model: string
  models: { id: string; label?: string; context?: number; pricing?: any }[]
  enabled: boolean
  extra: Record<string, any>
}

function ProviderEditor({ provider, catalog, onClose, onSaved }: {
  provider: Provider | null; catalog: any; onClose: () => void
  onSaved: (saved: Provider, tested?: HealthRecord) => void
}) {
  const [initial, setInitial] = useState<ProviderForm>(() => ({
    name: provider?.name ?? '',
    kind: provider?.kind ?? 'anthropic',
    base_url: provider?.base_url ?? '',
    api_key: '',
    default_model: provider?.default_model ?? '',
    models: provider?.models ?? [],
    enabled: provider?.enabled ?? true,
    extra: provider?.extra ?? {},
  }))
  const [form, setForm] = useState<ProviderForm>(initial)
  // 名字是不是用户自己敲的。没敲过就跟着类型 / 预设走，敲过就别动它
  const [nameTouched, setNameTouched] = useState(!!provider)
  const [busy, setBusy] = useState(false)
  const [test, setTest] = useState<{ since?: number; result?: HealthRecord; sig?: string }>({})
  const [fetched, setFetched] = useState<{ busy?: boolean; models?: string[]; error?: unknown } | null>(null)
  const resultRef = useRef<HTMLDivElement>(null)
  const set = (patch: Partial<ProviderForm>) => setForm((f) => ({ ...f, ...patch }))

  // 新建时 catalog 晚到一步：到了再把默认类型的模型和名字填上。这是预填不是
  // 用户的改动，基线跟着一起挪，否则什么都没碰就关窗也会被问「放弃修改？」
  useEffect(() => {
    if (provider || !catalog || JSON.stringify(form) !== JSON.stringify(initial)) return
    const def = catalog.kinds?.find((k: any) => k.kind === form.kind)
    if (!def) return
    const next = { ...form, models: def.models ?? [], default_model: def.default_model ?? '', name: shortLabel(def.label) }
    setForm(next)
    setInitial(next)
  }, [catalog])

  const kindDef = catalog?.kinds?.find((k: any) => k.kind === form.kind)
  const env = catalog?.env_detected ?? {}
  const envKey = form.kind === 'anthropic'
    ? !!(env.ANTHROPIC_API_KEY || env.ANTHROPIC_AUTH_TOKEN)
    : form.kind === 'openai' ? !!env.OPENAI_API_KEY : false
  // 「必填，例如 https://…」：必填交给标签上的 *，占位只放示例地址
  const hint: string = kindDef?.base_url_hint ?? ''
  const urlRequired = form.kind === 'openai_compatible'
  const urlExample = hint.match(/https?:\/\/\S+/)?.[0]
  const urlHint = hint.replace(/^必填[，,]\s*/, '').replace(/例如\s*https?:\/\/\S+/, '').replace(/[，,；;\s]+$/, '')
  const keyRequired = (form.kind === 'anthropic' || form.kind === 'openai') && !provider?.has_key && !envKey

  const missing: string[] = []
  if (!form.name.trim()) missing.push('名称')
  if (urlRequired && !form.base_url.trim()) missing.push('Base URL')
  if (keyRequired && !form.api_key) missing.push('API Key')
  const blocked = missing.length > 0

  const draft: ProviderDraft = {
    id: provider?.id,
    name: form.name,
    kind: form.kind,
    base_url: form.base_url.trim() || null,
    api_key: form.api_key || null,
    models: form.models,
    default_model: form.default_model || null,
    extra: form.extra,
  }
  const sig = JSON.stringify(draft)
  const dirty = JSON.stringify(form) !== JSON.stringify(initial)
  const testFresh = test.result && test.sig === sig

  const pickKind = (kind: string) => {
    const def = catalog?.kinds?.find((k: any) => k.kind === kind)
    setFetched(null)
    setForm((f) => ({
      ...f, kind,
      models: def?.models ?? [],
      default_model: def?.default_model ?? '',
      name: nameTouched ? f.name : shortLabel(def?.label) || f.name,
    }))
  }
  const kindRadio = useRadioGroup<string>((catalog?.kinds ?? []).map((k: any) => k.kind), form.kind, pickKind)

  const applyPreset = (preset: any) => {
    setFetched(null)
    setForm((f) => ({
      ...f,
      name: nameTouched ? f.name : shortLabel(preset.label),
      base_url: preset.base_url,
      models: preset.models ?? [],
      default_model: preset.models?.[0]?.id ?? '',
    }))
  }

  const runTest = async () => {
    setTest({ since: Date.now() })
    try {
      const r = await api.providers.testConfig({ ...draft, model: form.default_model || undefined })
      setTest({
        sig,
        result: {
          ok: !!r.ok, ms: r.latency_ms ?? null, at: Date.now(), error: r.error, hint: r.hint, detail: r.detail,
          note: r.ok ? `${r.model ?? form.default_model} 回：${String(r.reply ?? '').slice(0, 120)}` : undefined,
        },
      })
      if (!r.ok) requestAnimationFrame(() => resultRef.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }))
    } catch (e) {
      setTest({})
      toast.error(e)
    }
  }

  const fetchModels = async () => {
    setFetched({ busy: true })
    try {
      const r = await api.providers.models(draft)
      if (r.ok) {
        setFetched({ models: r.models })
        if (!r.models.length) toast.warn('这个地址上没有列出任何模型')
      } else {
        setFetched({ error: r })
      }
    } catch (e) {
      setFetched({ error: e })
    }
  }

  const hasModel = (id: string) => form.models.some((m) => m.id === id)
  const toggleModel = (id: string) => setForm((f) => {
    const on = f.models.some((m) => m.id === id)
    const models = on ? f.models.filter((m) => m.id !== id) : [...f.models, { id, label: id }]
    return { ...f, models, default_model: f.default_model || (on ? '' : id) }
  })

  const submit = async () => {
    if (blocked) return
    setBusy(true)
    try {
      const payload: any = { ...form, base_url: form.base_url.trim() || null }
      if (provider && !form.api_key) delete payload.api_key // 不填就保持原 key
      const saved = provider
        ? await api.providers.update(provider.id, payload)
        : await api.providers.create(payload)
      onSaved(saved, testFresh ? test.result : undefined)
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }

  const blockedTip = blocked ? `还缺：${missing.join('、')}` : undefined

  return (
    <Modal
      open
      onClose={onClose}
      dirty={dirty}
      width={600}
      title={provider ? `编辑「${provider.name}」` : '添加模型接入'}
      footer={
        <>
          <div className="mr-auto flex min-w-0 items-center">
            {(test.since || test.result) && (
              <HealthPill record={test.result} checkingSince={test.since} stale={!!test.result && !testFresh}
                          labels={{ ok: '通了', fail: '没通过' }} />
            )}
          </div>
          <button className="btn" onClick={onClose}>取消</button>
          <button className="btn" onClick={() => void runTest()} disabled={!!test.since || blocked}
                  title={blockedTip ?? `用 ${form.default_model || '默认模型'} 发一句话，不保存`}>
            {test.since ? <Spinner size={11} /> : <Plug size={12} aria-hidden />} 测试连接
          </button>
          <button className="btn btn-primary" onClick={() => void submit()} disabled={busy || blocked} title={blockedTip}>
            {busy ? <Spinner size={11} /> : <Check size={12} aria-hidden />} 保存
          </button>
        </>
      }
    >
      <div className="space-y-3">
        {!provider && (
          <div>
            <div className="label" id="provider-kind-label">类型</div>
            <div role="radiogroup" aria-labelledby="provider-kind-label" className="grid grid-cols-2 gap-1.5">
              {catalog?.kinds?.map((k: any) => {
                const on = form.kind === k.kind
                return (
                  <button
                    key={k.kind}
                    type="button"
                    {...kindRadio(k.kind)}
                    onClick={() => pickKind(k.kind)}
                    className={clsx(
                      'relative rounded-lg border px-3 py-2 pr-7 text-left text-xs transition-colors',
                      on ? 'border-[var(--accent)] bg-accent-soft text-fg' : 'hover:bg-hover',
                    )}
                  >
                    {k.label}
                    {on && <Check size={13} className="absolute right-2 top-2 text-[var(--accent)]" aria-hidden />}
                  </button>
                )
              }) ?? <Skeleton rows={2} cols={2} height={34} />}
            </div>
          </div>
        )}

        {!provider && form.kind === 'openai_compatible' && !!kindDef?.presets?.length && (
          <div>
            <div className="label">快速填充</div>
            <div className="flex flex-wrap gap-1.5">
              {kindDef.presets.map((p: any) => {
                const on = form.base_url === p.base_url
                return (
                  <button key={p.key} type="button"
                          className={clsx('chip hover:border-[var(--accent)]', on && 'border-[var(--accent)] bg-accent-soft text-fg')}
                          aria-pressed={on}
                          onClick={() => applyPreset(p)}>
                    {on && <Check size={10} aria-hidden />}{p.label}
                  </button>
                )
              })}
            </div>
          </div>
        )}

        <Field label="名称" required>
          {(p) => (
            <input {...p} className="field" value={form.name} placeholder="比如 DeepSeek（公司网关）"
                   onChange={(e) => { setNameTouched(true); set({ name: e.target.value }) }} />
          )}
        </Field>

        {form.kind !== 'mock' && (
          <>
            <Field label="Base URL" required={urlRequired} hint={urlHint || undefined}>
              {(p) => (
                <input {...p} className="field mono text-xs" value={form.base_url}
                       placeholder={urlExample ?? (urlRequired ? 'https://…/v1' : '留空走官方地址')}
                       onChange={(e) => set({ base_url: e.target.value })} />
              )}
            </Field>

            <Field label="API Key" required={keyRequired}
                   hint={envKey && !provider?.has_key ? '留空就用服务端环境变量里的 Key' : undefined}>
              {(p) => (
                <input
                  {...p}
                  className="field mono text-xs" type="password" autoComplete="off"
                  value={form.api_key}
                  placeholder={provider?.has_key ? `已设置（${provider.api_key_masked}），留空则不修改` : 'sk-…'}
                  onChange={(e) => set({ api_key: e.target.value })}
                />
              )}
            </Field>

            {form.kind === 'anthropic' && (
              <label className="flex items-center gap-2 text-xs">
                <input
                  type="checkbox"
                  checked={form.extra?.auth_style === 'bearer'}
                  onChange={(e) => set({ extra: { ...form.extra, auth_style: e.target.checked ? 'bearer' : undefined } })}
                />
                用 Authorization: Bearer 认证
                <span className="text-2xs text-faint">（部分中转网关需要，官方 API 不用勾）</span>
              </label>
            )}
          </>
        )}

        <Field label="默认模型" hint="测试连接和没指定模型的节点都用它">
          {(p) => (
            <>
              <input {...p} className="field mono text-xs" value={form.default_model} list="provider-model-list"
                     onChange={(e) => set({ default_model: e.target.value })} />
              <datalist id="provider-model-list">
                {form.models.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
              </datalist>
            </>
          )}
        </Field>

        <div>
          <div className="mb-1 flex items-center gap-2">
            <span className="label !mb-0">可选模型 <span className="tnum text-faint">{form.models.length}</span></span>
            {form.kind !== 'mock' && (
              <button type="button" className="btn btn-xs ml-auto" onClick={() => void fetchModels()}
                      disabled={!!fetched?.busy || (urlRequired && !form.base_url.trim())}
                      title={urlRequired && !form.base_url.trim() ? '先填 Base URL' : '带上 Key 调一次 /v1/models，省得手敲模型 id'}>
                {fetched?.busy ? <Spinner size={11} /> : <Download size={11} aria-hidden />} 从端点拉取模型
              </button>
            )}
          </div>
          <div className="flex flex-wrap gap-1">
            {form.models.map((m) => (
              <span key={m.id} className={clsx('chip mono', m.id === form.default_model && 'border-[var(--accent)]')}>
                {m.label || m.id}
                <button type="button" onClick={() => set({ models: form.models.filter((x) => x.id !== m.id) })}
                        className="-mr-1 rounded p-px text-faint hover:text-[var(--err)]" aria-label={`去掉模型 ${m.id}`} title="去掉">
                  <X size={10} />
                </button>
              </span>
            ))}
            {!form.models.length && <span className="text-2xs text-faint">还没有，拉取或手动添加</span>}
          </div>
          {fetched?.error != null && (
            <ErrorState compact error={fetched.error} onRetry={() => void fetchModels()} className="mt-2" />
          )}
          {fetched?.models && fetched.models.length > 0 && (
            <div className="mt-2 rounded-lg border bg-bg p-2" data-fetched-models>
              <div className="mb-1.5 flex items-center gap-2 text-2xs text-faint">
                端点上有 <span className="tnum">{fetched.models.length}</span> 个模型，点一下加入 / 去掉
                <button type="button" className="btn btn-xs ml-auto"
                        onClick={() => setForm((f) => {
                          const add = (fetched.models ?? []).filter((id) => !f.models.some((m) => m.id === id))
                          return { ...f, models: [...f.models, ...add.map((id) => ({ id, label: id }))], default_model: f.default_model || add[0] || '' }
                        })}>
                  全部加入
                </button>
              </div>
              <div className="flex max-h-40 flex-wrap gap-1 overflow-y-auto">
                {fetched.models.map((id) => {
                  const on = hasModel(id)
                  return (
                    <button key={id} type="button" aria-pressed={on} onClick={() => toggleModel(id)}
                            className={clsx('chip mono', on ? 'border-[var(--accent)] bg-accent-soft text-fg' : 'hover:border-[var(--border-strong)]')}>
                      {on && <Check size={10} aria-hidden />}{id}
                    </button>
                  )
                })}
              </div>
            </div>
          )}
          <input
            className="field mono mt-1.5 text-xs" placeholder="手动添加：模型 id，回车加入"
            aria-label="手动添加模型 id"
            onKeyDown={(e: ReactKeyboardEvent<HTMLInputElement>) => {
              if (e.key !== 'Enter' || isComposing(e)) return
              e.preventDefault()
              const id = e.currentTarget.value.trim()
              if (!id) return
              if (!hasModel(id)) toggleModel(id)
              e.currentTarget.value = ''
            }}
          />
        </div>

        <label className="flex items-center gap-2 text-xs">
          <input type="checkbox" checked={form.enabled} onChange={(e) => set({ enabled: e.target.checked })} />
          启用<span className="text-2xs text-faint">停用后它的模型不出现在节点的模型下拉里</span>
        </label>

        {blocked && <p className="text-2xs text-faint">还缺：{missing.join('、')}</p>}

        {test.result && !test.since && (
          <div ref={resultRef}>
            {test.result.ok
              ? <p className="rounded-lg border px-3 py-2 text-xs text-dim" style={{ borderColor: 'color-mix(in srgb, var(--ok) 40%, var(--border))' }}>{test.result.note}</p>
              : <ErrorState compact error={test.result} onRetry={blocked ? undefined : () => void runTest()} />}
          </div>
        )}
      </div>
    </Modal>
  )
}

// -------------------------------------------------------------------------

/**
 * 偏好设置。保存规则只有两条，页面上写明：
 * - 主题：选中即生效、即保存（只 PUT ui.theme）。它当场就变了样，再要求点保存，
 *   用户离开时会以为已经存了，下次打开又回到旧主题；
 * - 其余（署名、运行默认值）：改完点「保存设置」，底部常驻一条「有 N 项未保存」，
 *   切标签、点导航、关页都会先问。危险工具审批这种安全开关不能误点一下就生效，
 *   所以不做自动保存。署名以前每敲一个字就写 localStorage，现在跟着一起保存。
 */
function PrefsTab({ onDirty }: { onDirty: (n: number) => void }) {
  const collections = useCatalog((s) => s.collections)
  const [values, setValues] = useState<Record<string, any> | null>(null)
  const [loadError, setLoadError] = useState<unknown>(null)
  const [scopes, setScopes] = useState<{ scope: string; count: number }[]>([])
  const [theme, setTheme] = useState<ThemePref>(() => readThemePref())
  const [themeSave, setThemeSave] = useState<{ state: 'saving' | 'saved' | 'error'; error?: unknown } | null>(null)
  // 署名存在 localStorage，保存后成为新的基线，不再算作改动
  const savedActor = useRef<string>((() => { try { return localStorage.getItem('agentlab_actor') ?? '' } catch { return '' } })())
  const [draft, setDraft] = useState<{ actor: string; scope: string; collection: string; confirm: boolean } | null>(null)
  const [saving, setSaving] = useState(false)
  const [savedFlash, setSavedFlash] = useState(0)
  const [saveError, setSaveError] = useState<unknown>(null)

  const load = async () => {
    try {
      const v = await api.settings.get()
      setValues(v)
      setTheme(normalizeTheme(v?.ui?.theme))
      setLoadError(null)
    } catch (e) {
      setLoadError(e)
    }
    api.memory.scopes().then(setScopes, () => {})
  }
  useEffect(() => { void load() }, [])
  // 只在首次没拉到时重拉：重拉会覆盖正在编辑的表单
  useOnReconnect(() => { if (!values) void load() })

  const saved = {
    actor: savedActor.current,
    scope: values?.run?.default_memory_scope ?? 'default',
    collection: values?.run?.default_collection ?? 'default',
    confirm: values?.run?.confirm_dangerous_tools ?? true,
  }
  const cur = draft ?? saved
  const changed = (Object.keys(saved) as (keyof typeof saved)[]).filter((k) => cur[k] !== saved[k])
  const dirty = values ? changed.length : 0
  useEffect(() => { onDirty(dirty) }, [dirty])
  useEffect(() => () => onDirty(0), [])
  useLeaveGuard(dirty)

  useEffect(() => {
    if (!savedFlash) return
    const t = setTimeout(() => setSavedFlash(0), 1600)
    return () => clearTimeout(t)
  }, [savedFlash])
  // 主题旁的「已保存」同样只亮一下：一直挂着就不再是「刚存上」的回执了
  useEffect(() => {
    if (themeSave?.state !== 'saved') return
    const t = setTimeout(() => setThemeSave((s) => (s?.state === 'saved' ? null : s)), 1600)
    return () => clearTimeout(t)
  }, [themeSave])

  const edit = (patch: Partial<typeof saved>) => {
    setSaveError(null)
    setDraft({ ...cur, ...patch })
  }

  const pickTheme = async (next: ThemePref) => {
    setTheme(next)
    applyTheme(normalizeTheme(next))
    if (!values) return
    setThemeSave({ state: 'saving' })
    try {
      const out = await api.settings.put({ ui: { ...(values.ui ?? {}), theme: next } })
      setValues((v) => ({ ...(v ?? {}), ui: out?.ui ?? { ...(v?.ui ?? {}), theme: next } }))
      setThemeSave({ state: 'saved' })
    } catch (e) {
      setThemeSave({ state: 'error', error: e })
    }
  }

  const save = async () => {
    if (!values || !dirty) return
    setSaving(true)
    setSaveError(null)
    try {
      const run = {
        ...(values.run ?? {}),
        default_memory_scope: cur.scope,
        default_collection: cur.collection,
        confirm_dangerous_tools: cur.confirm,
      }
      const out = await api.settings.put({ run })
      try {
        if (cur.actor.trim()) localStorage.setItem('agentlab_actor', cur.actor.trim())
        else localStorage.removeItem('agentlab_actor')
      } catch { /* 隐私模式：署名只能这一次会话有效 */ }
      setValues((v) => ({ ...(v ?? {}), run: out?.run ?? run }))
      savedActor.current = cur.actor.trim()
      setDraft(null)
      setSavedFlash(Date.now())
    } catch (e) {
      setSaveError(e)
    } finally {
      setSaving(false)
    }
  }
  if (!values) {
    return loadError
      ? <ErrorState error={loadError} onRetry={() => void load()} />
      : <Skeleton rows={6} height={14} />
  }

  const scopeOptions = [...new Set(['default', ...scopes.map((s) => s.scope), cur.scope])]
  const collectionNames = collections.map((c) => c.collection)
  const collectionMissing = !collectionNames.includes(cur.collection)

  return (
    <div className="max-w-2xl space-y-6 pb-20">
      <section>
        <SectionBar title="界面" hint="选中就生效、就保存，所有设备共用。" />
        <div className="flex flex-wrap items-center gap-3">
          <ThemeChoice value={theme} onChange={(v) => void pickTheme(v)} />
          <span className="text-2xs" aria-live="polite" data-theme-save={themeSave?.state}>
            {themeSave?.state === 'saving' && <span className="inline-flex items-center gap-1 text-faint"><Spinner size={10} /> 正在保存…</span>}
            {themeSave?.state === 'saved' && <span className="fade-up text-faint"><Check size={11} className="mr-0.5 inline text-[var(--ok)]" aria-hidden />已保存</span>}
            {themeSave?.state === 'error' && (
              <span className="text-[var(--err)]">
                没存上（{humanizeError(themeSave.error).title}），下次打开会回到原来的主题
                <button className="btn btn-xs ml-2" onClick={() => void pickTheme(theme)}>重试</button>
              </span>
            )}
          </span>
        </div>
      </section>

      <section>
        <SectionBar title="操作者署名" hint="只存在这台浏览器里，换台电脑要重新填。" />
        <Field label="名字" htmlFor="pref-actor"
               hint="发布、审批、发起正式运行会记到这个名下，随请求头 X-Actor 发送。这是归属记录不是身份认证——多人环境需要真正的登录体系。">
          <input id="pref-actor" className="field max-w-60" value={cur.actor} placeholder="例如 yilun"
                 aria-describedby="pref-actor-hint"
                 onChange={(e) => edit({ actor: e.target.value })} />
        </Field>
      </section>

      <section>
        <SectionBar title="运行默认值" hint="存在服务端，所有人共用。发起运行时没单独指定，就用这里的。" />
        <div className="grid grid-cols-2 gap-3">
          <Field label="默认记忆作用域" htmlFor="pref-scope" hint="agent 读写长期记忆用哪一格">
            <select id="pref-scope" className="field" value={cur.scope}
                    aria-describedby="pref-scope-hint"
                    onChange={async (e) => {
                      if (e.target.value !== '__new__') { edit({ scope: e.target.value }); return }
                      const name = await promptDialog({
                        title: '新的记忆作用域', label: '作用域名', placeholder: '比如 quality',
                        validate: (v) => (/^[\w.-]{1,64}$/.test(v) ? null : '只用字母、数字、下划线、点和短横'),
                        confirmLabel: '用这个',
                      })
                      if (name) edit({ scope: name })
                    }}>
              {scopeOptions.map((s) => {
                const n = scopes.find((x) => x.scope === s)?.count
                return <option key={s} value={s}>{s}{n != null ? ` · ${formatNumber(n)} 条` : ' · 还没有记忆'}</option>
              })}
              <option value="__new__">新建一个作用域…</option>
            </select>
          </Field>
          <Field label="默认知识库" htmlFor="pref-collection"
                 error={collectionMissing ? `「${cur.collection}」这个知识库不存在：检索会落到空库上` : undefined}
                 hint="检索节点没指定知识库时查它">
            <select id="pref-collection" className="field" value={cur.collection}
                    aria-describedby={collectionMissing ? 'pref-collection-error' : 'pref-collection-hint'}
                    aria-invalid={collectionMissing || undefined}
                    onChange={(e) => edit({ collection: e.target.value })}>
              {collectionMissing && <option value={cur.collection}>{cur.collection}（不存在）</option>}
              {collections.map((c) => (
                <option key={c.collection} value={c.collection}>
                  {c.collection} · {formatNumber(c.documents)} 篇 / {formatNumber(c.chunks)} 段
                </option>
              ))}
            </select>
          </Field>
        </div>
        <label className="mt-3 flex items-start gap-2 text-xs">
          <input type="checkbox" className="mt-0.5" checked={cur.confirm}
                 aria-describedby="pref-confirm-hint"
                 onChange={(e) => edit({ confirm: e.target.checked })} />
          <span>
            危险工具默认需要人工审批
            <span id="pref-confirm-hint" className="mt-0.5 block text-2xs leading-relaxed text-faint">
              只影响探索运行里没单独配置审批策略的节点；正式运行始终至少审批危险工具。受管工作流的发布门禁另外生效。
            </span>
          </span>
        </label>
      </section>

      {(dirty > 0 || savedFlash > 0 || saveError != null) && (
        <div
          className="sticky bottom-0 z-10 -mx-1 flex flex-wrap items-center gap-2 rounded-lg border bg-panel px-3 py-2 shadow-elev-2"
          role="region"
          aria-label="未保存的设置"
          data-prefs-bar
        >
          {dirty > 0 ? (
            <>
              <span className="h-1.5 w-1.5 rounded-full bg-[var(--warn)]" aria-hidden />
              <span className="text-xs">
                有 <span className="tnum">{dirty}</span> 项未保存
                <span className="ml-1.5 text-2xs text-faint">
                  {changed.map((k) => ({ actor: '署名', scope: '记忆作用域', collection: '知识库', confirm: '危险工具审批' })[k]).join('、')}
                </span>
              </span>
              {saveError != null && (
                <span className="text-2xs text-[var(--err)]" role="alert">没存上：{humanizeError(saveError).title}</span>
              )}
              <span className="flex-1" />
              <button className="btn btn-sm btn-ghost" disabled={saving}
                      onClick={() => { setDraft(null); setSaveError(null) }}>放弃</button>
              <button className="btn btn-sm btn-primary" disabled={saving} onClick={() => void save()}>
                {saving ? <Spinner size={11} /> : <Check size={12} aria-hidden />} 保存设置
              </button>
            </>
          ) : (
            <span className="fade-up flex items-center gap-1.5 text-xs text-dim" role="status">
              <Check size={13} className="text-[var(--ok)]" aria-hidden /> 已保存
            </span>
          )}
        </div>
      )}
    </div>
  )
}

const THEMES: { value: ThemePref; label: string; Icon: typeof Sun }[] = [
  { value: 'system', label: '跟随系统', Icon: Monitor },
  { value: 'light', label: '浅色', Icon: Sun },
  { value: 'dark', label: '深色', Icon: Moon },
]

const THEME_VALUES = THEMES.map((t) => t.value)

/** 主题三选一：单选组，←→ 切换，选中的有底色和勾，不只靠边框颜色 */
function ThemeChoice({ value, onChange }: { value: ThemePref; onChange: (v: ThemePref) => void }) {
  const radio = useRadioGroup(THEME_VALUES, value, onChange)
  return (
    <div role="radiogroup" aria-label="主题" className="inline-flex gap-1.5">
      {THEMES.map(({ value: v, label, Icon }) => {
        const on = v === value
        return (
          <button
            key={v}
            type="button"
            {...radio(v)}
            onClick={() => { if (!on) onChange(v) }}
            className={clsx(
              'relative flex items-center gap-1.5 rounded-lg border px-3 py-1.5 pr-6 text-xs transition-colors',
              on ? 'border-[var(--accent)] bg-accent-soft text-fg' : 'text-dim hover:bg-hover',
            )}
          >
            <Icon size={13} aria-hidden />{label}
            {on && <Check size={11} className="absolute right-1.5 top-1.5 text-[var(--accent)]" aria-hidden />}
          </button>
        )
      })}
    </div>
  )
}

/**
 * 有没保存的改动时拦住离开：关页、刷新走 beforeunload；站内导航（左侧导航、
 * 页面里的链接）在捕获阶段拦下点击，问过再走。路由是 BrowserRouter，用不了
 * react-router 的 useBlocker。
 */
function useLeaveGuard(dirty: number) {
  const navigate = useNavigate()
  useEffect(() => {
    if (!dirty) return
    const onBeforeUnload = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = '' }
    const onClick = (e: MouseEvent) => {
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
      const a = (e.target as Element | null)?.closest?.('a[href]') as HTMLAnchorElement | null
      if (!a || a.target === '_blank' || a.hasAttribute('download')) return
      const url = new URL(a.href, location.href)
      if (url.origin !== location.origin || url.pathname === location.pathname) return
      e.preventDefault()
      e.stopPropagation()
      void confirmLeave(dirty).then((ok) => { if (ok) navigate(url.pathname + url.search + url.hash) })
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    document.addEventListener('click', onClick, true)
    return () => {
      window.removeEventListener('beforeunload', onBeforeUnload)
      document.removeEventListener('click', onClick, true)
    }
  }, [dirty, navigate])
}

// -------------------------------------------------------------------------

function SystemTab() {
  const [info, setInfo] = useState<any>(null)
  const [error, setError] = useState<unknown>(null)
  const load = () => api.system().then((v) => { setInfo(v); setError(null) }, setError)
  useEffect(() => { void load() }, [])
  useOnReconnect(() => { if (!info) void load() })
  if (!info) return error ? <ErrorState error={error} onRetry={() => void load()} /> : <Skeleton rows={8} height={14} />

  const sandbox = info.sandbox ?? {}
  return (
    <div className="max-w-2xl space-y-4">
      <section className="rounded-lg border bg-panel p-3">
        <h2 className="mb-2 flex items-center gap-1.5 text-sm font-semibold">
          <Cpu size={13} aria-hidden /> 沙箱
        </h2>
        <div className="space-y-1.5 text-xs">
          <Row label="后端" value={
            <span className="inline-flex items-center gap-1.5">
              <span className="mono">{sandbox.backend}</span>
              <span className="chip" style={{ color: sandbox.available ? 'var(--st-done)' : 'var(--st-failed)' }}>
                {sandbox.available ? '可用' : '不可用'}
              </span>
            </span>
          } />
          <Row label="选择原因" value={sandbox.selected_because} />
          {sandbox.isolation && <Row label="隔离方式" value={sandbox.isolation} />}
          {sandbox.limits_note && <Row label="限额说明" value={sandbox.limits_note} />}
          {!!sandbox.candidates?.length && (
            <Row label="可用后端" value={
              <span className="flex flex-wrap gap-1.5">
                {sandbox.candidates.map((c: any) => (
                  <span key={c.name} className={clsx('chip mono', !c.available && 'opacity-50')}
                        style={c.name === sandbox.backend ? { borderColor: 'var(--accent)', color: 'var(--text)' } : undefined}>
                    {c.name === sandbox.backend && <Check size={10} aria-hidden />}
                    {c.name}{c.available ? '' : ' · 不可用'}
                  </span>
                ))}
              </span>
            } />
          )}
          {sandbox.warning && (
            <div className="mt-2 rounded-lg border px-2.5 py-1.5 text-2xs leading-relaxed"
                 style={{ borderColor: 'var(--warn)', color: 'var(--warn)' }}>
              {sandbox.warning}
            </div>
          )}
          {sandbox.defaults && (
            <Row label="默认限额" value={
              <span className="flex flex-wrap items-center gap-2">
                <Limit on={enforced(sandbox, 'timeout')} text={`${sandbox.defaults.timeout}s 超时`} />
                <Limit on={enforced(sandbox, 'memory')} text={`${sandbox.defaults.memory_mb}MB 内存`} />
                <Limit on={enforced(sandbox, 'cpu')} text={`${sandbox.defaults.cpus} CPU`} />
                <Limit on={enforced(sandbox, 'network')} text={sandbox.defaults.network ? '联网' : '断网'}
                       note="HTTP/HTTPS 与域名解析可断，但 UDP/53 拦不住" />
              </span>
            } />
          )}
        </div>
      </section>

      <section className="rounded-lg border bg-panel p-3">
        <h2 className="mb-2 text-sm font-semibold">运行环境</h2>
        <div className="space-y-1.5 text-xs">
          <Row label="Python" value={<span className="mono">{info.python}</span>} />
          <Row label="数据目录" value={<code className="mono text-2xs">{info.data_dir}</code>} />
          <Row label="图最大步数" value={<span className="tnum">{info.limits?.max_graph_steps}</span>} />
          <Row label="Agent 最大步数" value={<span className="tnum">{info.limits?.max_agent_steps}</span>} />
          <Row label="最大并发运行" value={<span className="tnum">{info.limits?.max_concurrent_runs}</span>} />
          <Row label="单次执行时限" value={info.limits?.max_run_seconds && `${info.limits.max_run_seconds} 秒`} />
          <Row label="模型调用超时" value={info.limits?.model_timeout_seconds && `${info.limits.model_timeout_seconds} 秒`} />
        </div>
      </section>
    </div>
  )
}

/** 后端对某项限额的执行力度。后端没说就按"生效"处理（老后端没有这个字段）。 */
function enforced(sandbox: any, key: string): boolean | 'partial' {
  const v = sandbox.enforced?.[key]
  return v === 'partial' ? 'partial' : v !== false
}

/** 限额项：当前后端管不住的，划掉并注明，别给人虚假的安全感。
 *
 * 三态而不是两态——microVM 的网络就卡在中间：HTTP/HTTPS 断得掉，
 * UDP/53 断不掉。这种"拦了一半"要是显示成绿色，比显示成红色更危险。
 */
function Limit({ on, text, note }: { on: boolean | 'partial'; text: string; note?: string }) {
  if (on === 'partial') {
    return (
      <span className="chip" title={note || '这项限额只部分生效'}
            style={{ color: 'var(--warn)', borderColor: 'color-mix(in srgb, var(--warn) 40%, transparent)' }}>
        {text} · 部分生效
      </span>
    )
  }
  return on ? (
    <span className="chip" style={{ color: 'var(--ok)', borderColor: 'color-mix(in srgb, var(--ok) 40%, transparent)' }}>
      {text}
    </span>
  ) : (
    <span className="chip line-through opacity-60" title="当前后端不支持这项限额">
      {text} · 不生效
    </span>
  )
}

function Row({ label, value }: { label: string; value: any }) {
  return (
    <div className="flex gap-3">
      <span className="w-28 shrink-0 text-faint">{label}</span>
      <span className="min-w-0 flex-1 break-all">{value}</span>
    </div>
  )
}
