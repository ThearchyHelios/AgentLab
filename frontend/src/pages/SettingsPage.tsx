import { useEffect, useState } from 'react'
import { Check, Cpu, KeyRound, Plus, Radio, Trash2, X } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../api/client'
import { useCatalog } from '../store/catalog'
import { Empty, Modal, Spinner, Tabs, useTabRoute, useToast } from '../components/ui'
import { DataSourcesTab } from './DataSourcesTab'
import type { Provider } from '../types'

// 提到模块级：tab 名同时是 URL 的最后一段，两处各写一份迟早对不上
const TABS = [
  { key: 'providers', label: '模型接入' },
  { key: 'datasources', label: '数据源' },
  { key: 'prefs', label: '偏好设置' },
  { key: 'system', label: '运行环境' },
]

export function SettingsPage() {
  const [tab, setTab] = useTabRoute(TABS.map((t) => t.key), 'providers')
  return (
    <div className="mx-auto flex h-full w-full max-w-4xl flex-col">
      <Tabs tabs={TABS} active={tab} onChange={setTab} />
      <div className="flex-1 overflow-y-auto p-4">
        {tab === 'providers' && <ProvidersTab />}
        {tab === 'datasources' && <DataSourcesTab />}
        {tab === 'prefs' && <PrefsTab />}
        {tab === 'system' && <SystemTab />}
      </div>
    </div>
  )
}

// -------------------------------------------------------------------------

function ProvidersTab() {
  const toast = useToast()
  const { providers, refresh } = useCatalog()
  const [editing, setEditing] = useState<Provider | 'new' | null>(null)
  const [testing, setTesting] = useState<string | null>(null)
  const [results, setResults] = useState<Record<string, any>>({})

  const test = async (p: Provider) => {
    setTesting(p.id)
    try {
      const res = await api.providers.test(p.id, { model: p.default_model ?? undefined })
      setResults({ ...results, [p.id]: res })
      toast(res.ok ? `连通，${res.latency_ms}ms` : `失败：${res.error}`, res.ok ? 'ok' : 'error')
    } catch (e: any) {
      toast(e.message ?? '测试失败', 'error')
    } finally {
      setTesting(null)
    }
  }

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <div>
          <div className="text-sm font-semibold">模型接入</div>
          <div className="text-[11px] text-faint">
            API Key 加密后存在本地 SQLite，不会回传给浏览器
          </div>
        </div>
        <button className="btn btn-primary" onClick={() => setEditing('new')}>
          <Plus size={12} /> 添加
        </button>
      </div>

      <div className="space-y-2">
        {providers.map((p) => {
          const result = results[p.id]
          return (
            <div key={p.id} className="rounded-lg border bg-panel p-3">
              <div className="flex items-center gap-2">
                <span className={clsx('h-2 w-2 rounded-full')}
                      style={{ background: p.enabled ? 'var(--ok)' : 'var(--text-faint)' }} />
                <span className="text-[12.5px] font-medium">{p.name}</span>
                <span className="chip">{p.kind}</span>
                {p.default_model && <span className="chip">{p.default_model}</span>}
                <div className="flex-1" />
                <button className="btn btn-sm" disabled={testing === p.id} onClick={() => test(p)}>
                  {testing === p.id ? <Spinner size={11} /> : <Radio size={11} />} 测试
                </button>
                <button className="btn btn-sm" onClick={() => setEditing(p)}>编辑</button>
                <button
                  className="btn btn-sm btn-ghost"
                  onClick={async () => {
                    if (!confirm(`删除 ${p.name}？`)) return
                    await api.providers.remove(p.id)
                    await refresh()
                  }}
                >
                  <Trash2 size={11} className="text-[var(--err)]" />
                </button>
              </div>
              <div className="mt-1.5 flex flex-wrap gap-3 text-[10.5px] text-faint">
                {p.base_url && <span className="mono">{p.base_url}</span>}
                {p.has_key && <span className="mono">key {p.api_key_masked}</span>}
                {(p.extra as any)?.auth_style === 'bearer' && <span>Bearer 认证</span>}
                <span>{p.models?.length ?? 0} 个模型</span>
              </div>
              {result && (
                <div
                  className="mt-2 rounded border p-2 text-[11px] leading-relaxed"
                  style={{ borderColor: result.ok ? 'var(--ok)' : 'var(--err)' }}
                >
                  {result.ok ? (
                    <>
                      <div className="mb-1 text-[var(--ok)]">✓ {result.latency_ms}ms · {result.model}</div>
                      <div className="text-dim">{result.reply}</div>
                    </>
                  ) : (
                    <div className="text-[var(--err)] break-all">{result.error}</div>
                  )}
                </div>
              )}
            </div>
          )
        })}
        {!providers.length && <Empty icon={<KeyRound size={22} />} title="还没有配置模型" />}
      </div>

      {editing && (
        <ProviderEditor
          provider={editing === 'new' ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => { setEditing(null); void refresh() }}
        />
      )}
    </div>
  )
}

function ProviderEditor({ provider, onClose, onSaved }: {
  provider: Provider | null; onClose: () => void; onSaved: () => void
}) {
  const toast = useToast()
  const [catalog, setCatalog] = useState<any>(null)
  const [form, setForm] = useState<any>(() => ({
    name: provider?.name ?? '',
    kind: provider?.kind ?? 'anthropic',
    base_url: provider?.base_url ?? '',
    api_key: '',
    default_model: provider?.default_model ?? '',
    models: provider?.models ?? [],
    enabled: provider?.enabled ?? true,
    extra: provider?.extra ?? {},
  }))
  const [busy, setBusy] = useState(false)

  useEffect(() => { void api.providers.catalog().then(setCatalog) }, [])

  const kindDef = catalog?.kinds?.find((k: any) => k.kind === form.kind)

  const applyPreset = (preset: any) => {
    setForm({
      ...form,
      name: form.name || preset.label,
      base_url: preset.base_url,
      models: preset.models,
      default_model: preset.models?.[0]?.id ?? '',
    })
  }

  const pickKind = (kind: string) => {
    const def = catalog?.kinds?.find((k: any) => k.kind === kind)
    setForm({
      ...form, kind,
      models: def?.models ?? [],
      default_model: def?.default_model ?? '',
      name: form.name || def?.label || '',
    })
  }

  const submit = async () => {
    setBusy(true)
    try {
      const payload = { ...form, base_url: form.base_url || null }
      if (provider && !form.api_key) delete payload.api_key // 不填就保持原 key
      if (provider) await api.providers.update(provider.id, payload)
      else await api.providers.create(payload)
      toast('已保存', 'ok')
      onSaved()
    } catch (e: any) {
      toast(e.message ?? '保存失败', 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open
      onClose={onClose}
      title={provider ? `编辑 ${provider.name}` : '添加模型接入'}
      footer={
        <>
          <button className="btn" onClick={onClose}>取消</button>
          <button className="btn btn-primary" onClick={submit} disabled={busy || !form.name}>
            {busy ? <Spinner /> : <Check size={12} />} 保存
          </button>
        </>
      }
    >
      {!provider && (
        <div className="mb-3">
          <label className="label">类型</label>
          <div className="grid grid-cols-2 gap-1.5">
            {catalog?.kinds?.map((k: any) => (
              <button
                key={k.kind}
                onClick={() => pickKind(k.kind)}
                className={clsx(
                  'rounded-lg border px-3 py-2 text-left text-[11.5px] hover:bg-hover',
                  form.kind === k.kind && 'border-[var(--accent)]',
                )}
              >
                {k.label}
              </button>
            ))}
          </div>
        </div>
      )}

      {!provider && form.kind === 'openai_compatible' && !!kindDef?.presets?.length && (
        <div className="mb-3">
          <label className="label">快速填充</label>
          <div className="flex flex-wrap gap-1.5">
            {kindDef.presets.map((p: any) => (
              <button key={p.key} className="chip hover:border-[var(--accent)]" onClick={() => applyPreset(p)}>
                {p.label}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="mb-3">
        <label className="label">名称</label>
        <input className="field" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
      </div>

      {form.kind !== 'mock' && (
        <>
          <div className="mb-3">
            <label className="label">Base URL</label>
            <input className="field mono text-[11px]" value={form.base_url}
                   placeholder={kindDef?.base_url_hint}
                   onChange={(e) => setForm({ ...form, base_url: e.target.value })} />
            {kindDef?.base_url_hint && (
              <div className="mt-1 text-[10px] text-faint">{kindDef.base_url_hint}</div>
            )}
          </div>

          <div className="mb-3">
            <label className="label">API Key</label>
            <input
              className="field mono text-[11px]" type="password" autoComplete="off"
              value={form.api_key}
              placeholder={provider?.has_key ? `已设置（${provider.api_key_masked}），留空则不修改` : 'sk-…'}
              onChange={(e) => setForm({ ...form, api_key: e.target.value })}
            />
          </div>

          {form.kind === 'anthropic' && (
            <label className="mb-3 flex items-center gap-2 text-[11.5px]">
              <input
                type="checkbox" className="accent-[var(--accent)]"
                checked={form.extra?.auth_style === 'bearer'}
                onChange={(e) =>
                  setForm({ ...form, extra: { ...form.extra, auth_style: e.target.checked ? 'bearer' : undefined } })
                }
              />
              用 Authorization: Bearer 认证
              <span className="text-[10px] text-faint">（部分中转网关需要，官方 API 不用勾）</span>
            </label>
          )}
        </>
      )}

      <div className="mb-3">
        <label className="label">默认模型</label>
        <input className="field mono text-[11px]" value={form.default_model} list="model-list"
               onChange={(e) => setForm({ ...form, default_model: e.target.value })} />
        <datalist id="model-list">
          {form.models?.map((m: any) => <option key={m.id} value={m.id}>{m.label}</option>)}
        </datalist>
      </div>

      <div className="mb-1">
        <label className="label">可选模型（{form.models?.length ?? 0}）</label>
        <div className="flex flex-wrap gap-1">
          {form.models?.map((m: any) => (
            <span key={m.id} className="chip">
              {m.label || m.id}
              <button
                onClick={() => setForm({ ...form, models: form.models.filter((x: any) => x.id !== m.id) })}
                className="hover:opacity-60"
              >
                <X size={9} />
              </button>
            </span>
          ))}
        </div>
        <div className="mt-1.5 flex gap-1.5">
          <input
            className="field mono text-[11px]" placeholder="模型 id，回车添加"
            onKeyDown={(e) => {
              if (e.key !== 'Enter') return
              const id = (e.target as HTMLInputElement).value.trim()
              if (!id) return
              setForm({ ...form, models: [...(form.models ?? []), { id, label: id }] })
              ;(e.target as HTMLInputElement).value = ''
            }}
          />
        </div>
      </div>

      <label className="mt-3 flex items-center gap-2 text-[11.5px]">
        <input type="checkbox" className="accent-[var(--accent)]" checked={form.enabled}
               onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
        启用
      </label>
    </Modal>
  )
}

// -------------------------------------------------------------------------

function PrefsTab() {
  const toast = useToast()
  const [values, setValues] = useState<Record<string, any> | null>(null)

  useEffect(() => { void api.settings.get().then(setValues) }, [])
  if (!values) return <Spinner />

  const set = (group: string, key: string, value: any) =>
    setValues({ ...values, [group]: { ...values[group], [key]: value } })

  const save = async () => {
    await api.settings.put(values)
    toast('已保存', 'ok')
  }

  const theme = values.ui?.theme ?? 'system'

  return (
    <div className="max-w-xl space-y-4">
      <div>
        <div className="mb-2 text-sm font-semibold">界面</div>
        <label className="label">主题</label>
        <select
          className="field"
          value={theme}
          onChange={(e) => {
            set('ui', 'theme', e.target.value)
            applyTheme(e.target.value)
          }}
        >
          <option value="system">跟随系统</option>
          <option value="dark">深色</option>
          <option value="light">浅色</option>
        </select>
      </div>

      <div>
        <div className="mb-2 text-sm font-semibold">操作者署名</div>
        <label className="label">名字（发布、审批、发起正式运行会记到这个名下）</label>
        <input
          className="field max-w-60"
          defaultValue={(() => { try { return localStorage.getItem('agentlab_actor') ?? '' } catch { return '' } })()}
          placeholder="例如 yilun"
          onChange={(e) => {
            try { localStorage.setItem('agentlab_actor', e.target.value.trim()) } catch { /* noop */ }
          }}
        />
        <div className="mt-1 text-[10px] text-faint">
          本地署名，随请求头 X-Actor 发送。这是归属记录不是身份认证——多人环境需要真正的登录体系。
        </div>
      </div>

      <div>
        <div className="mb-2 text-sm font-semibold">运行默认值</div>
        <label className="label">默认记忆作用域</label>
        <input className="field" value={values.run?.default_memory_scope ?? 'default'}
               onChange={(e) => set('run', 'default_memory_scope', e.target.value)} />
        <label className="label mt-2">默认知识库</label>
        <input className="field" value={values.run?.default_collection ?? 'default'}
               onChange={(e) => set('run', 'default_collection', e.target.value)} />
        <label className="mt-3 flex items-center gap-2 text-[11.5px]">
          <input type="checkbox" className="accent-[var(--accent)]"
                 checked={values.run?.confirm_dangerous_tools ?? true}
                 onChange={(e) => set('run', 'confirm_dangerous_tools', e.target.checked)} />
          危险工具默认需要人工确认
        </label>
      </div>

      <button className="btn btn-primary" onClick={save}><Check size={12} /> 保存设置</button>
    </div>
  )
}

export function applyTheme(theme: string) {
  const root = document.documentElement
  if (theme === 'system') root.removeAttribute('data-theme')
  else root.setAttribute('data-theme', theme)
}

// -------------------------------------------------------------------------

function SystemTab() {
  const [info, setInfo] = useState<any>(null)
  useEffect(() => { void api.system().then(setInfo) }, [])
  if (!info) return <Spinner />

  const sandbox = info.sandbox ?? {}
  return (
    <div className="max-w-2xl space-y-4">
      <div className="rounded-lg border bg-panel p-3">
        <div className="mb-2 flex items-center gap-1.5 text-sm font-semibold">
          <Cpu size={13} /> 沙箱
        </div>
        <div className="space-y-1 text-[11.5px]">
          <Row label="后端" value={
            <span style={{ color: sandbox.available ? 'var(--ok)' : 'var(--err)' }}>
              {sandbox.backend}{sandbox.available ? '（可用）' : '（不可用）'}
            </span>
          } />
          <Row label="选择原因" value={sandbox.selected_because} />
          {sandbox.isolation && <Row label="隔离方式" value={sandbox.isolation} />}
          {sandbox.limits_note && <Row label="限额说明" value={sandbox.limits_note} />}
          {!!sandbox.candidates?.length && (
            <Row label="可用后端" value={
              <span className="flex flex-wrap gap-1.5">
                {sandbox.candidates.map((c: any) => (
                  <span key={c.name} className="chip"
                        style={c.name === sandbox.backend
                          ? { borderColor: 'var(--ok)', color: 'var(--ok)' }
                          : c.available ? undefined : { opacity: 0.45 }}>
                    {c.name}{c.available ? '' : ' · 不可用'}
                  </span>
                ))}
              </span>
            } />
          )}
          {sandbox.warning && (
            <div className="mt-2 rounded border px-2 py-1.5 text-[10.5px]"
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
      </div>

      <div className="rounded-lg border bg-panel p-3">
        <div className="mb-2 text-sm font-semibold">运行环境</div>
        <div className="space-y-1 text-[11.5px]">
          <Row label="Python" value={info.python} />
          <Row label="数据目录" value={<code className="mono text-[10.5px]">{info.data_dir}</code>} />
          <Row label="图最大步数" value={info.limits?.max_graph_steps} />
          <Row label="Agent 最大步数" value={info.limits?.max_agent_steps} />
          <Row label="最大并发运行" value={info.limits?.max_concurrent_runs} />
          <Row label="单次执行时限" value={info.limits?.max_run_seconds && `${info.limits.max_run_seconds} 秒`} />
          <Row label="模型调用超时" value={info.limits?.model_timeout_seconds && `${info.limits.model_timeout_seconds} 秒`} />
        </div>
      </div>
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
