import { useEffect, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent, ReactNode } from 'react'
import { Navigate, useParams } from 'react-router-dom'
import {
  Check, Cpu, Download, KeyRound, Monitor, Moon, Plug, Plus, Settings as SettingsIcon, Sun, X,
} from 'lucide-react'
import clsx from 'clsx'
import { api } from '../api/client'
import type { ProviderDraft } from '../api/client'
import { modelOptions, providerOfModel, useCatalog, useOnReconnect } from '../store/catalog'
import {
  confirmDialog, DeleteButton, EmptyState, ErrorState, Field, HealthPill, isComposing, Modal, PageHeader,
  promptDialog, SectionBar, Skeleton, Spinner, TabPanel, Tabs, toast, useRadioGroup, useTabRoute,
} from '../components/ui'
import { setThemePref, useTheme } from '../components/CommandPalette'
import { humanizeError } from '../lib/errors'
import { formatNumber, shortLabel } from '../lib/format'
import { checkHealth, forgetHealth, healthFromServer, setHealth, useHealth } from '../lib/health'
import type { HealthRecord } from '../lib/health'
import { workflowList, workflowsMentioning } from '../lib/mentions'
import type { Provider } from '../types'
import { normalizeTheme } from '../lib/theme'
import { useLeaveGuard } from '../lib/leave'
import { localActor, setLocalActor } from '../lib/actor'
import { AGENT_GUARD_TEXT, JUDGE_SETTING_TEXT, TOOL_GATE_TEXT, judgeByModelText, judgeUnlimitedText } from '../lib/terms'
import type { ThemePref } from '../lib/theme'

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

  if (raw === 'datasources') return <Navigate to="/data" replace />

  // 偏好设置有没保存的改动时，切标签由 PrefsTab 登记的守卫在地址变化时问一次。
  // 这里不再先问：问过再 setTab，守卫还在，会再问第二遍（useTabRoute 带不了 leavePass）
  const change = (key: string) => {
    if (key !== tab) setTab(key)
  }

  return (
    <div className="flex h-full flex-col">
      <PageHeader icon={<SettingsIcon size={13} />} title="设置" subtitle="模型接入、偏好和运行环境" />
      <Tabs tabs={TABS} active={tab} onChange={change} label="设置" idPrefix="settings" />
      <TabPanel idPrefix="settings" tabKey={tab} className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-4xl p-4">
          {tab === 'providers' && <ProvidersTab />}
          {tab === 'prefs' && <PrefsTab />}
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
          source="providers"
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
          onSaved={(saved, tested, reconnected) => {
            setEditing(null)
            // 本机记着的那次测连接说的是改之前的配置：换了地址、钥匙或默认模型，
            // 它就不作数了（后端同样清掉了自己记的那份）
            if (tested) setHealth(`provider:${saved.id}`, tested)
            else if (reconnected) forgetHealth(`provider:${saved.id}`)
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
  // 后端记着上次测的结果：换了浏览器、清了缓存也还在。本机刚测过的更新就用本机的
  const { record, checkingSince } = useHealth(key, healthFromServer(p))
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

/**
 * 决定「连不连得上」的那几项：类型、地址、钥匙、请求头、测的哪个模型。和后端
 * 判断测连接结果还作不作数的那份指纹是同一组字段，改名字、改可选模型不算
 */
const providerConnection = (f: ProviderForm) => JSON.stringify([
  f.kind, f.base_url.trim(), !!f.api_key, f.extra?.auth_style ?? null, f.extra?.headers ?? null, f.default_model,
])

function ProviderEditor({ provider, catalog, onClose, onSaved }: {
  provider: Provider | null; catalog: any; onClose: () => void
  /** reconnected：连接配置改过了，之前测的结果不再代表它 */
  onSaved: (saved: Provider, tested?: HealthRecord, reconnected?: boolean) => void
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
      onSaved(saved, testFresh ? test.result : undefined, providerConnection(form) !== providerConnection(initial))
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
 * - 主题：选中即生效、即保存（setThemePref，和导航、⌘K 的切换同一条路）。它当场
 *   就变了样，再要求点保存，用户离开时会以为已经存了，下次打开又回到旧主题；
 * - 其余（署名、运行默认值）：改完点「保存设置」，底部常驻一条「有 N 项未保存」，
 *   切标签、点导航、关页都会先问。危险工具审批这种安全开关不能误点一下就生效，
 *   所以不做自动保存。署名以前每敲一个字就写 localStorage，现在跟着一起保存。
 */
function PrefsTab() {
  const collections = useCatalog((s) => s.collections)
  const providers = useCatalog((s) => s.providers)
  const [values, setValues] = useState<Record<string, any> | null>(null)
  const [loadError, setLoadError] = useState<unknown>(null)
  const [scopes, setScopes] = useState<{ scope: string; count: number }[]>([])
  // 署名存在 localStorage，保存后成为新的基线，不再算作改动
  const savedActor = useRef<string>(localActor() ?? '')
  const [draft, setDraft] = useState<{
    actor: string; scope: string; collection: string; confirm: boolean; gateProvider: string; gateModel: string
    steps: string; budgetTokens: string; budgetUsd: string
    judgeProvider: string; judgeModel: string
    // 证据裁判的五项上限：null 是不限（存 null），字符串是正在填的数
    jClaims: string | null; jCost: string | null; jTimeout: string | null; jClick: string | null; jDaily: string | null
  } | null>(null)
  const [spend, setSpend] = useState<{ usd?: number; calls?: number; unpriced_calls?: number } | null>(null)
  const [saving, setSaving] = useState(false)
  const [savedFlash, setSavedFlash] = useState(0)
  const [saveError, setSaveError] = useState<unknown>(null)

  // 今天裁判花了多少：只读，放在每日上限旁边。老后端没有这个接口就不显示
  const loadSpend = () => api.settings.judgeSpend().then(setSpend, () => setSpend(null))
  const load = async () => {
    try {
      setValues(await api.settings.get())
      setLoadError(null)
    } catch (e) {
      setLoadError(e)
    }
    api.memory.scopes().then(setScopes, () => {})
    void loadSpend()
  }
  useEffect(() => { void load() }, [])
  // 只在首次没拉到时重拉：重拉会覆盖正在编辑的表单
  useOnReconnect(() => { if (!values) void load() })

  const saved = {
    actor: savedActor.current,
    scope: values?.run?.default_memory_scope ?? 'default',
    collection: values?.run?.default_collection ?? 'default',
    confirm: values?.run?.confirm_dangerous_tools ?? true,
    // 门控模型：null（老后端没有这两项也一样）= 用默认接入的默认模型，表单里是空串
    gateProvider: values?.run?.tool_gate_provider ?? '',
    gateModel: values?.run?.tool_gate_model ?? '',
    // agent 护栏（后端 engine/guards.py）。表单里是字符串：预算空串 = 不限（存 null）
    steps: String(values?.run?.agent_max_steps ?? 100),
    budgetTokens: values?.run?.agent_budget_tokens == null ? '' : String(values.run.agent_budget_tokens),
    budgetUsd: values?.run?.agent_budget_usd == null ? '' : String(values.run.agent_budget_usd),
    // 证据裁判（后端 engine/judge.py，GET 给的是引擎实际生效的样子：存坏了的显示默认，null 是不限）
    judgeProvider: values?.judge?.provider ?? '',
    judgeModel: values?.judge?.model ?? '',
    jClaims: limitText(values?.judge, 'report_max_claims'),
    jCost: limitText(values?.judge, 'report_max_cost_usd'),
    jTimeout: limitText(values?.judge, 'report_timeout_s'),
    jClick: limitText(values?.judge, 'click_max_cost_usd'),
    jDaily: limitText(values?.judge, 'daily_max_usd'),
  }
  const cur = draft ?? saved
  const stepCap = Number(values?.limits?.max_agent_steps) || 100
  // 只校验改过的：存着的值不合规（比如服务端的硬上限后来调小了）不该挡住别的设置保存，
  // 后端发起运行时本来就按硬上限截
  const guardErrors = {
    steps: cur.steps === saved.steps
      || (/^\d+$/.test(cur.steps.trim()) && Number(cur.steps) >= 1 && Number(cur.steps) <= stepCap)
      ? undefined : `填 1 到 ${stepCap} 的整数（${stepCap} 是服务端的硬上限，由环境变量 AGENTLAB_MAX_AGENT_STEPS 定）`,
    budgetTokens: cur.budgetTokens === saved.budgetTokens || cur.budgetTokens.trim() === ''
      || (/^\d+$/.test(cur.budgetTokens.trim()) && Number(cur.budgetTokens) >= 1000)
      ? undefined : '填不少于 1000 的整数，或者留空表示不限',
    budgetUsd: cur.budgetUsd === saved.budgetUsd || cur.budgetUsd.trim() === ''
      || (Number(cur.budgetUsd) > 0 && Number.isFinite(Number(cur.budgetUsd)))
      ? undefined : '填大于 0 的金额，或者留空表示不限',
  }
  const guardInvalid = Object.values(guardErrors).some(Boolean)
  // 裁判的上限同样只校验改过的；null（不限）总是对的
  const judgeError = (key: 'jClaims' | 'jCost' | 'jTimeout' | 'jClick' | 'jDaily') => {
    const v = cur[key]
    if (v === saved[key] || v === null) return undefined
    const n = Number(v.trim())
    if (key === 'jClaims') return /^\d+$/.test(v.trim()) && n >= 1 ? undefined : '填正整数，或者勾「不限」'
    if (key === 'jTimeout') return v.trim() !== '' && Number.isFinite(n) && n > 0 ? undefined : '填大于 0 的秒数，或者勾「不限」'
    return v.trim() !== '' && Number.isFinite(n) && n > 0 ? undefined : '填大于 0 的金额，或者勾「不限」'
  }
  const judgeErrors = {
    jClaims: judgeError('jClaims'), jCost: judgeError('jCost'), jTimeout: judgeError('jTimeout'),
    jClick: judgeError('jClick'), jDaily: judgeError('jDaily'),
  }
  const judgeInvalid = Object.values(judgeErrors).some(Boolean)
  const invalid = guardInvalid || judgeInvalid
  const changed = (Object.keys(saved) as (keyof typeof saved)[]).filter((k) => cur[k] !== saved[k])
  const dirty = values ? changed.length : 0
  // 外壳唯一的 blocker 在地址（pathname）变化时问：切标签、导航、⌘K、⌥ 数字、后退都算
  useLeaveGuard(dirty > 0, () => confirmLeave(dirty))

  useEffect(() => {
    if (!savedFlash) return
    const t = setTimeout(() => setSavedFlash(0), 1600)
    return () => clearTimeout(t)
  }, [savedFlash])

  const edit = (patch: Partial<typeof saved>) => {
    setSaveError(null)
    setDraft({ ...cur, ...patch })
  }

  /**
   * 署名只在这台浏览器里，先落本机：它不该跟着后端一起「没存上」。运行默认值
   * 有改动才 PUT——只改了署名时不必碰服务端，断线时也存得上
   */
  const save = async () => {
    if (!values || !dirty || invalid) return
    setSaving(true)
    setSaveError(null)
    const actor = cur.actor.trim()
    if (actor !== savedActor.current) {
      // 写不进去（隐私模式）时署名只在这一次会话里有效；setLocalActor 会通知导航底部的首字
      setLocalActor(actor)
      savedActor.current = actor
    }
    const judgeChanged = changed.some((k) => JUDGE_DRAFT.has(k))
    const runChanged = changed.some((k) => k !== 'actor' && !JUDGE_DRAFT.has(k))
    try {
      // 裁判一组整组写：后端对没给的键回落到默认值，只发改过的那一项会把别的项冲回默认
      const judge = judgeChanged ? {
        provider: cur.judgeProvider || null,
        model: cur.judgeModel.trim() || null,
        report_max_claims: parseLimit(cur.jClaims),
        report_max_cost_usd: parseLimit(cur.jCost),
        report_timeout_s: parseLimit(cur.jTimeout),
        click_max_cost_usd: parseLimit(cur.jClick),
        daily_max_usd: parseLimit(cur.jDaily),
      } : null
      if (runChanged || judge) {
        const run = {
          ...(values.run ?? {}),
          default_memory_scope: cur.scope,
          default_collection: cur.collection,
          confirm_dangerous_tools: cur.confirm,
          tool_gate_provider: cur.gateProvider || null,
          tool_gate_model: cur.gateModel.trim() || null,
          agent_max_steps: Number(cur.steps),
          agent_budget_tokens: cur.budgetTokens.trim() === '' ? null : Number(cur.budgetTokens),
          agent_budget_usd: cur.budgetUsd.trim() === '' ? null : Number(cur.budgetUsd),
        }
        const out = await api.settings.put({ ...(runChanged ? { run } : {}), ...(judge ? { judge } : {}) })
        setValues((v) => ({
          ...(v ?? {}),
          ...(runChanged ? { run: out?.run ?? run } : {}),
          ...(judge ? { judge: out?.judge ?? judge } : {}),
        }))
        // 每日上限改了：旁边那句「今天已花」的分母跟着变，顺手再取一次
        if (judge) void loadSpend()
      }
      setDraft(null)
      setSavedFlash(Date.now())
    } catch (e) {
      // 署名已经存上了：留在草稿里的只剩运行默认值，条上的计数跟着变少
      setDraft({ ...cur, actor })
      setSaveError(e)
    } finally {
      setSaving(false)
    }
  }

  const scopeOptions = [...new Set(['default', ...scopes.map((s) => s.scope), cur.scope])]
  const collectionNames = collections.map((c) => c.collection)
  const collectionMissing = !collectionNames.includes(cur.collection)
  // 门控模型留空时后端的挑法（resolve_provider）：第一个启用的真实接入，没有就第一个启用的
  const enabledProviders = providers.filter((p) => p.enabled)
  const defaultProvider = enabledProviders.find((p) => p.kind !== 'mock') ?? enabledProviders[0]
  const gateProvider = cur.gateProvider ? providers.find((p) => p.name === cur.gateProvider) : defaultProvider
  const gateProviderMissing = !!cur.gateProvider && providers.length > 0 && !gateProvider
  const gateModels = (gateProvider?.models ?? []).map((m) => m.id)
  // 裁判模型的接入：接入和模型都留空时后端依次用 Copilot 的模型、默认接入（judge_model_spec）。只填了模型时
  // 这一级写了就整组用这一级，按模型名找接入（resolve_provider）——不再跟随 Copilot，留空那一项不能还叫「跟随 Copilot」
  const judgeProvider = cur.judgeProvider ? providers.find((p) => p.name === cur.judgeProvider) : undefined
  const judgeProviderMissing = !!cur.judgeProvider && providers.length > 0 && !judgeProvider
  const judgeModelName = cur.judgeModel.trim()
  const judgeByModel = !cur.judgeProvider && !!judgeModelName
  const judgeOwner = judgeByModel ? providerOfModel(providers, judgeModelName) : undefined
  const judgeByModelHint = judgeByModel && providers.length > 0
    ? judgeByModelText('settings', judgeModelName, judgeOwner && { name: judgeOwner.provider.name, enabled: judgeOwner.enabled },
      defaultProvider?.name)
    : ''
  // 接入留空时按模型名找接入：所有启用接入的模型都能选；选了接入只列这一家的
  const judgeModels = judgeProvider ? (judgeProvider.models ?? []).map((m) => m.id)
    : [...new Set(modelOptions(providers).map((o) => o.value))]
  const pickJudgeProvider = (name: string) => {
    const next = name ? providers.find((p) => p.name === name) : undefined
    const keep = !name || !cur.judgeModel || (next?.models ?? []).some((m) => m.id === cur.judgeModel) || next?.default_model === cur.judgeModel
    edit({ judgeProvider: name, ...(keep ? {} : { judgeModel: '' }) })
  }
  const limitSet = {
    claims: cur.jClaims !== null, cost: cur.jCost !== null, timeout: cur.jTimeout !== null,
    click: cur.jClick !== null, daily: cur.jDaily !== null,
  }
  const pickGateProvider = (name: string) => {
    // 换了接入，原来填的模型不在新接入的清单里就清掉：拿 A 家的模型名去调 B 家必然失败
    const next = name ? providers.find((p) => p.name === name) : defaultProvider
    const keep = !cur.gateModel || (next?.models ?? []).some((m) => m.id === cur.gateModel) || next?.default_model === cur.gateModel
    edit({ gateProvider: name, ...(keep ? {} : { gateModel: '' }) })
  }

  return (
    <div className="max-w-2xl space-y-6 pb-20">
      {/* 主题不依赖设置接口：断线时也能换，恢复后自动补存 */}
      <ThemeSection />

      {!values ? (
        loadError
          ? <ErrorState error={loadError} onRetry={() => void load()} />
          : <Skeleton rows={6} height={14} />
      ) : (
        <>
          <section>
            <SectionBar title="操作者署名" hint="只存在这台浏览器里，换台电脑要重新填。" />
            <Field label="名字" htmlFor="pref-actor"
                   hint="发布、审批、发起正式运行会记到这个名下，随请求头 X-Actor 发送。这是归属记录不是身份认证——多人环境需要真正的登录体系。">
              <input id="pref-actor" className="field max-w-60" value={cur.actor} placeholder="例如 王工"
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
            <div className="mt-4" role="group" aria-labelledby="pref-gate-label" aria-describedby="pref-gate-hint" data-gate-model>
              <div id="pref-gate-label" className="text-xs font-medium">{TOOL_GATE_TEXT.label}</div>
              <p id="pref-gate-hint" className="mb-2 mt-0.5 text-2xs leading-relaxed text-faint">{TOOL_GATE_TEXT.hint}</p>
              <div className="grid grid-cols-2 gap-3">
                <Field label={TOOL_GATE_TEXT.provider} htmlFor="pref-gate-provider"
                       error={gateProviderMissing ? `「${cur.gateProvider}」这个接入不存在：门控模型答不上来，每次调用都会交给你审批` : undefined}>
                  {(p) => (
                    <select {...p} className="field" value={cur.gateProvider} onChange={(e) => pickGateProvider(e.target.value)}>
                      <option value="">
                        {TOOL_GATE_TEXT.providerDefault}{defaultProvider ? `（现在是 ${defaultProvider.name}）` : ''}
                      </option>
                      {gateProviderMissing && <option value={cur.gateProvider}>{cur.gateProvider}（不存在）</option>}
                      {providers.map((pv) => (
                        <option key={pv.id} value={pv.name}>{pv.name}{pv.enabled ? '' : '（已停用）'}</option>
                      ))}
                    </select>
                  )}
                </Field>
                <Field label={TOOL_GATE_TEXT.model} htmlFor="pref-gate-model"
                       hint={gateProvider?.default_model ? `留空就用 ${gateProvider.default_model}` : undefined}>
                  {(p) => (
                    <>
                      <input {...p} className="field mono text-xs" value={cur.gateModel} list="pref-gate-models"
                             placeholder={TOOL_GATE_TEXT.modelDefault} spellCheck={false}
                             onChange={(e) => edit({ gateModel: e.target.value })} />
                      <datalist id="pref-gate-models">
                        {gateModels.map((m) => <option key={m} value={m} />)}
                      </datalist>
                    </>
                  )}
                </Field>
              </div>
            </div>
            <div className="mt-4" role="group" aria-labelledby="pref-guard-label" aria-describedby="pref-guard-hint" data-agent-guard>
              <div id="pref-guard-label" className="text-xs font-medium">{AGENT_GUARD_TEXT.label}</div>
              <p id="pref-guard-hint" className="mb-2 mt-0.5 text-2xs leading-relaxed text-faint">{AGENT_GUARD_TEXT.hint}</p>
              <div className="grid grid-cols-3 gap-3">
                <Field label={AGENT_GUARD_TEXT.steps} htmlFor="pref-guard-steps" error={guardErrors.steps}
                       hint={AGENT_GUARD_TEXT.stepsHint}>
                  {(p) => (
                    <input {...p} className="field tnum" inputMode="numeric" value={cur.steps}
                           onChange={(e) => edit({ steps: e.target.value })} />
                  )}
                </Field>
                <Field label={AGENT_GUARD_TEXT.tokens} htmlFor="pref-guard-tokens" error={guardErrors.budgetTokens}
                       hint={cur.budgetTokens.trim() === '' ? AGENT_GUARD_TEXT.unlimited : AGENT_GUARD_TEXT.tokensHint}>
                  {(p) => (
                    <input {...p} className="field tnum" inputMode="numeric" value={cur.budgetTokens}
                           placeholder={AGENT_GUARD_TEXT.unlimitedShort}
                           onChange={(e) => edit({ budgetTokens: e.target.value })} />
                  )}
                </Field>
                <Field label={AGENT_GUARD_TEXT.usd} htmlFor="pref-guard-usd" error={guardErrors.budgetUsd}
                       hint={cur.budgetUsd.trim() === '' ? AGENT_GUARD_TEXT.unlimited : AGENT_GUARD_TEXT.usdHint}>
                  {(p) => (
                    <input {...p} className="field tnum" inputMode="decimal" value={cur.budgetUsd}
                           placeholder={AGENT_GUARD_TEXT.unlimitedShort}
                           onChange={(e) => edit({ budgetUsd: e.target.value })} />
                  )}
                </Field>
              </div>
            </div>
          </section>

          {values.judge && (
            <section data-judge-settings>
              <SectionBar title={JUDGE_SETTING_TEXT.label} hint={JUDGE_SETTING_TEXT.hint} />
              <div role="group" aria-labelledby="pref-judge-model-label" aria-describedby="pref-judge-model-hint" data-judge-model>
                <div id="pref-judge-model-label" className="text-xs font-medium">裁判模型</div>
                <p id="pref-judge-model-hint" className="mb-2 mt-0.5 text-2xs leading-relaxed text-faint">{JUDGE_SETTING_TEXT.differ}</p>
                <div className="grid grid-cols-2 gap-3">
                  <Field label={JUDGE_SETTING_TEXT.provider} htmlFor="pref-judge-provider"
                         error={judgeProviderMissing ? `「${cur.judgeProvider}」这个接入不存在：裁判调用会失败，结论句都记为未裁判` : undefined}
                         hint={judgeByModelHint ? (
                           <span data-judge-by-model={judgeOwner ? (judgeOwner.enabled ? 'found' : 'disabled') : 'default'}
                                 style={judgeOwner?.enabled ? undefined : { color: 'var(--st-waiting)' }}>{judgeByModelHint}</span>
                         ) : undefined}>
                    {(p) => (
                      <select {...p} className="field" value={cur.judgeProvider} onChange={(e) => pickJudgeProvider(e.target.value)}>
                        <option value="">{judgeByModel ? JUDGE_SETTING_TEXT.providerFromModel : JUDGE_SETTING_TEXT.providerDefault}</option>
                        {judgeProviderMissing && <option value={cur.judgeProvider}>{cur.judgeProvider}（不存在）</option>}
                        {providers.map((pv) => (
                          <option key={pv.id} value={pv.name}>{pv.name}{pv.enabled ? '' : '（已停用）'}</option>
                        ))}
                      </select>
                    )}
                  </Field>
                  <Field label={JUDGE_SETTING_TEXT.model} htmlFor="pref-judge-model"
                         hint={judgeProvider?.default_model ? `留空就用 ${judgeProvider.default_model}` : undefined}>
                    {(p) => (
                      <>
                        <input {...p} className="field mono text-xs" value={cur.judgeModel} list="pref-judge-models"
                               placeholder={cur.judgeProvider ? JUDGE_SETTING_TEXT.modelDefault : JUDGE_SETTING_TEXT.modelFollow}
                               spellCheck={false}
                               onChange={(e) => edit({ judgeModel: e.target.value })} />
                        <datalist id="pref-judge-models">
                          {judgeModels.map((m) => <option key={m} value={m} />)}
                        </datalist>
                      </>
                    )}
                  </Field>
                </div>
              </div>
              <div className="mt-4" role="group" aria-labelledby="pref-judge-limits-label" aria-describedby="pref-judge-limits-hint"
                   data-judge-limits>
                <div id="pref-judge-limits-label" className="text-xs font-medium">{JUDGE_SETTING_TEXT.limits}</div>
                <p id="pref-judge-limits-hint" className="mb-2 mt-0.5 text-2xs leading-relaxed text-faint">{JUDGE_SETTING_TEXT.nodeWins}</p>
                <div className="grid grid-cols-2 gap-3 max-sm:grid-cols-1">
                  <LimitField id="pref-judge-claims" dataKey="report_max_claims" label={JUDGE_SETTING_TEXT.reportClaims}
                              value={cur.jClaims} fallback={JUDGE_FALLBACK.report_max_claims} integer
                              error={judgeErrors.jClaims} unlimited={judgeUnlimitedText('claims', limitSet)}
                              onChange={(v) => edit({ jClaims: v })} />
                  <LimitField id="pref-judge-cost" dataKey="report_max_cost_usd" label={JUDGE_SETTING_TEXT.reportCost}
                              value={cur.jCost} fallback={JUDGE_FALLBACK.report_max_cost_usd}
                              error={judgeErrors.jCost} unlimited={judgeUnlimitedText('cost', limitSet)}
                              onChange={(v) => edit({ jCost: v })} />
                  <LimitField id="pref-judge-timeout" dataKey="report_timeout_s" label={JUDGE_SETTING_TEXT.reportTimeout}
                              value={cur.jTimeout} fallback={JUDGE_FALLBACK.report_timeout_s}
                              error={judgeErrors.jTimeout} unlimited={judgeUnlimitedText('timeout', limitSet)}
                              onChange={(v) => edit({ jTimeout: v })} />
                  <LimitField id="pref-judge-click" dataKey="click_max_cost_usd" label={JUDGE_SETTING_TEXT.clickCost}
                              value={cur.jClick} fallback={JUDGE_FALLBACK.click_max_cost_usd}
                              error={judgeErrors.jClick} unlimited={judgeUnlimitedText('click', limitSet)}
                              onChange={(v) => edit({ jClick: v })} />
                  <LimitField id="pref-judge-daily" dataKey="daily_max_usd" label={JUDGE_SETTING_TEXT.daily}
                              value={cur.jDaily} fallback={JUDGE_FALLBACK.daily_max_usd}
                              error={judgeErrors.jDaily} unlimited={judgeUnlimitedText('daily', limitSet)}
                              onChange={(v) => edit({ jDaily: v })}
                              extra={spend && (
                                <div className="mt-1 text-2xs leading-relaxed text-dim" data-judge-spend="">
                                  {spend.calls ? JUDGE_SETTING_TEXT.spend(formatSpend(spend.usd), spend.calls) : JUDGE_SETTING_TEXT.spendNone}
                                  {!!spend.unpriced_calls && (
                                    <span className="block" style={{ color: 'var(--st-waiting)' }} data-judge-unpriced="">
                                      {JUDGE_SETTING_TEXT.unpriced(spend.unpriced_calls)}
                                    </span>
                                  )}
                                </div>
                              )} />
                </div>
              </div>
            </section>
          )}
        </>
      )}

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
                  {changed.map((k) => ({
                    actor: '署名', scope: '记忆作用域', collection: '知识库', confirm: '危险工具的默认审批策略',
                    gateProvider: '门控模型的接入', gateModel: '门控模型',
                    steps: '默认最大步数', budgetTokens: '令牌预算', budgetUsd: '金额预算',
                    judgeProvider: '裁判模型的接入', judgeModel: '裁判模型',
                    jClaims: '每份报告最多判几句', jCost: '每份报告的金额上限', jTimeout: '每份报告的时长上限',
                    jClick: '每次点击的金额上限', jDaily: '每日金额上限',
                  })[k]).join('、')}
                </span>
              </span>
              {saveError != null && (
                <span className="text-2xs text-[var(--err)]" role="alert">没存上：{humanizeError(saveError).title}</span>
              )}
              <span className="flex-1" />
              <button className="btn btn-sm btn-ghost" disabled={saving}
                      onClick={() => { setDraft(null); setSaveError(null) }}>放弃</button>
              <button className="btn btn-sm btn-primary" disabled={saving || invalid}
                      title={guardInvalid ? '护栏那几项有没填对的，改好再保存'
                        : judgeInvalid ? '证据裁判的上限有没填对的，改好再保存' : undefined}
                      onClick={() => void save()}>
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

/** 裁判那一组在草稿里的键：它们改了才写 judge 一组 */
const JUDGE_DRAFT = new Set<string>(['judgeProvider', 'judgeModel', 'jClaims', 'jCost', 'jTimeout', 'jClick', 'jDaily'])

/** 勾掉「不限」时填回的数：同后端 JUDGE_DEFAULTS */
const JUDGE_FALLBACK = {
  report_max_claims: '40', report_max_cost_usd: '0.05', report_timeout_s: '30', click_max_cost_usd: '0.01', daily_max_usd: '2',
}

/** 设置里的一项上限 → 表单：null 是不限，没有这一项（老后端）当空串 */
function limitText(group: Record<string, any> | undefined, key: string): string | null {
  const v = group?.[key]
  return v === null ? null : v === undefined ? '' : String(v)
}

/** 表单 → 存进去的值：不限存 null */
const parseLimit = (v: string | null): number | null => (v === null ? null : Number(v.trim()))

/** 今天花了多少美元：小额按分以下的精度写，不四舍五入成 0 */
const formatSpend = (usd: number | undefined): string =>
  usd == null || !Number.isFinite(usd) ? '0' : usd >= 1 ? usd.toFixed(2) : String(Number(usd.toFixed(4)))

/**
 * 一项裁判上限：数字框加「不限」。勾了不限，框变灰，下面写明不设上限之后费用还受什么约束——
 * 不许悄悄生效；勾掉时填回之前的数（没有就填默认值）
 */
function LimitField({ id, dataKey, label, value, fallback, integer, error, unlimited, onChange, extra }: {
  id: string; dataKey: string; label: string; value: string | null; fallback: string; integer?: boolean
  error?: string; unlimited: string; onChange: (v: string | null) => void; extra?: ReactNode
}) {
  const last = useRef(value ?? fallback)
  if (value !== null) last.current = value || last.current
  const off = value === null
  return (
    <div className="min-w-0" data-judge-limit={dataKey} data-unlimited={off ? '' : undefined}>
      <Field label={label} htmlFor={id} error={error}
             hint={off ? <span style={{ color: 'var(--st-waiting)' }} data-judge-unlimited="">{unlimited}</span> : undefined}>
        {(p) => (
          <div className="flex items-center gap-2">
            <input {...p} className="field tnum min-w-0 flex-1" inputMode={integer ? 'numeric' : 'decimal'}
                   value={off ? '' : value} disabled={off} placeholder={off ? JUDGE_SETTING_TEXT.unlimited : undefined}
                   onChange={(e) => onChange(e.target.value)} />
            <label className="flex shrink-0 cursor-pointer items-center gap-1 text-xs">
              <input type="checkbox" checked={off} aria-label={`${label}：${JUDGE_SETTING_TEXT.unlimited}`}
                     aria-describedby={p['aria-describedby']} data-judge-unlimited-toggle=""
                     onChange={(e) => onChange(e.target.checked ? null : last.current || fallback)} />
              {JUDGE_SETTING_TEXT.unlimited}
            </label>
          </div>
        )}
      </Field>
      {extra}
    </div>
  )
}

/**
 * 主题：选中即生效、即保存。显示的是眼下实际应用的偏好（useTheme）——从导航、
 * ⌘K 换了主题，这里跟着变，不会留着旧值。
 *
 * setThemePref 自己处理失败：断线时先在本机生效、恢复后补存，别的失败弹提示。
 * 它不告诉调用方存没存上，所以「已保存」这句回执是把设置读回来核对过才写的，
 * 不是猜的；没核对上就不写，失败的那句提示由 setThemePref 给
 */
function ThemeSection() {
  const { pref } = useTheme()
  const [receipt, setReceipt] = useState<'saving' | 'saved' | null>(null)
  const latest = useRef<ThemePref>(pref)

  useEffect(() => {
    if (receipt !== 'saved') return
    // 「已保存」只亮一下：一直挂着就不再是「刚存上」的回执了
    const t = setTimeout(() => setReceipt((r) => (r === 'saved' ? null : r)), 1600)
    return () => clearTimeout(t)
  }, [receipt])

  const pick = async (next: ThemePref) => {
    latest.current = next
    setReceipt('saving')
    await setThemePref(next)
    if (latest.current !== next) return
    const stored = await api.settings.get().then((s) => normalizeTheme(s?.ui?.theme), () => null)
    if (latest.current !== next) return
    setReceipt(stored === next ? 'saved' : null)
  }

  return (
    <section>
      <SectionBar title="界面" hint="选中就生效、就保存，所有设备共用。" />
      <div className="flex flex-wrap items-center gap-3">
        <ThemeChoice value={pref} onChange={(v) => void pick(v)} />
        <span className="text-2xs" aria-live="polite" data-theme-save={receipt ?? undefined}>
          {receipt === 'saving' && <span className="inline-flex items-center gap-1 text-faint"><Spinner size={10} /> 正在保存…</span>}
          {receipt === 'saved' && <span className="fade-up text-faint"><Check size={11} className="mr-0.5 inline text-[var(--ok)]" aria-hidden />已保存</span>}
        </span>
      </div>
    </section>
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
          <Row label="工作流最大步数" value={<span className="tnum">{info.limits?.max_graph_steps}</span>} />
          <Row label="Agent 最大步数" value={<span className="tnum">{info.limits?.max_agent_steps}</span>} />
          <Row label="最大并发运行" value={<span className="tnum">{info.limits?.max_concurrent_runs}</span>} />
          <Row label="单次运行时限" value={info.limits?.max_run_seconds && `${info.limits.max_run_seconds} 秒`} />
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
