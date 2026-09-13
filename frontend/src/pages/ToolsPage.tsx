import { useEffect, useState } from 'react'
import { Play, Plug, Plus, RefreshCw, Terminal, Trash2, Wrench } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../api/client'
import { useCatalog } from '../store/catalog'
import { Empty, JsonInput, Modal, Spinner, Tabs, useToast } from '../components/ui'
import type { ToolInfo } from '../types'

export function ToolsPage() {
  const [tab, setTab] = useState('tools')
  return (
    <div className="flex h-full flex-col">
      <Tabs
        tabs={[
          { key: 'tools', label: '工具库' },
          { key: 'sandbox', label: '沙箱试验台' },
          { key: 'custom', label: '自定义工具' },
          { key: 'mcp', label: 'MCP 接入' },
        ]}
        active={tab}
        onChange={setTab}
      />
      <div className="min-h-0 flex-1 overflow-y-auto">
        {tab === 'tools' && <ToolLibrary />}
        {tab === 'sandbox' && <SandboxLab />}
        {tab === 'custom' && <CustomTools />}
        {tab === 'mcp' && <McpServers />}
      </div>
    </div>
  )
}

// -------------------------------------------------------------------------

function ToolLibrary() {
  const tools = useCatalog((s) => s.tools)
  const [picked, setPicked] = useState<ToolInfo | null>(null)
  const [args, setArgs] = useState<any>({})
  const [result, setResult] = useState<any>(null)
  const [busy, setBusy] = useState(false)
  const [query, setQuery] = useState('')

  const run = async () => {
    if (!picked) return
    setBusy(true)
    setResult(null)
    try {
      setResult(await api.tools.run(picked.id, args ?? {}))
    } catch (e: any) {
      setResult({ ok: false, error: e.message })
    } finally {
      setBusy(false)
    }
  }

  const shown = tools.filter(
    (t) => !query || t.name.includes(query) || t.description.includes(query) || t.category.includes(query),
  )
  const groups = [...new Set(shown.map((t) => t.category))]

  return (
    <div className="flex h-full">
      <div className="flex w-[340px] shrink-0 flex-col border-r">
        <div className="border-b p-2">
          <input className="field" placeholder="搜索工具…" value={query}
                 onChange={(e) => setQuery(e.target.value)} />
        </div>
        <div className="flex-1 overflow-y-auto p-2">
          {groups.map((g) => (
            <div key={g} className="mb-3">
              <div className="mb-1 px-1 text-[10px] font-semibold uppercase tracking-wider text-faint">{g}</div>
              {shown.filter((t) => t.category === g).map((t) => (
                <button
                  key={t.id}
                  onClick={() => { setPicked(t); setArgs({}); setResult(null) }}
                  className={clsx(
                    'w-full rounded px-2 py-1.5 text-left hover:bg-hover',
                    picked?.id === t.id && 'bg-hover',
                  )}
                >
                  <div className="flex items-center gap-1.5">
                    <span className="mono text-[11.5px]">{t.name}</span>
                    {t.dangerous && <span className="chip" style={{ color: 'var(--warn)' }}>需确认</span>}
                  </div>
                  <div className="truncate text-[10px] text-faint">{t.description}</div>
                </button>
              ))}
            </div>
          ))}
          {!shown.length && <Empty icon={<Wrench size={20} />} title="没有匹配的工具" />}
        </div>
      </div>

      <div className="min-w-0 flex-1 overflow-y-auto p-4">
        {!picked && <Empty title="选一个工具试试" hint="不用搭图就能单独调用工具，验证参数和返回格式。" />}
        {picked && (
          <div className="max-w-2xl">
            <div className="mb-1 flex items-center gap-2">
              <span className="mono text-sm font-semibold">{picked.name}</span>
              <span className="chip">{picked.source}</span>
            </div>
            <div className="mb-3 text-[11.5px] leading-relaxed text-dim">{picked.description}</div>

            <label className="label">参数</label>
            <JsonInput value={args} onChange={setArgs} rows={6} placeholder="{}" />

            {!!picked.schema?.properties && (
              <div className="mt-2 rounded border bg-panel p-2">
                <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-faint">参数说明</div>
                {Object.entries(picked.schema.properties as Record<string, any>).map(([key, spec]) => (
                  <div key={key} className="flex gap-2 py-0.5 text-[11px]">
                    <code className="mono w-32 shrink-0 text-[var(--accent)]">{key}</code>
                    <span className="w-16 shrink-0 text-faint">{spec.type}</span>
                    <span className="min-w-0 flex-1 text-dim">
                      {spec.description ?? ''}
                      {(picked.schema.required ?? []).includes(key) && (
                        <span className="ml-1 text-[var(--err)]">必填</span>
                      )}
                    </span>
                  </div>
                ))}
              </div>
            )}

            <button className="btn btn-primary mt-3" onClick={run} disabled={busy}>
              {busy ? <Spinner /> : <Play size={12} />} 执行
            </button>

            {result && (
              <div className="mt-3">
                <div className="label">结果 {result.duration_ms != null && `· ${result.duration_ms}ms`}</div>
                <pre
                  className="mono max-h-96 overflow-auto whitespace-pre-wrap break-words rounded border bg-panel p-2 text-[11px] leading-relaxed"
                  style={{ borderColor: result.ok === false ? 'var(--err)' : undefined }}
                >
                  {result.ok === false
                    ? result.error
                    : typeof result.result === 'string'
                      ? result.result
                      : JSON.stringify(result.result, null, 2)}
                </pre>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

// -------------------------------------------------------------------------

function SandboxLab() {
  const [code, setCode] = useState(
    'import sys, platform\nprint("Python", sys.version.split()[0], "on", platform.machine())\nprint(sum(range(101)))',
  )
  const [language, setLanguage] = useState('python')
  const [network, setNetwork] = useState(false)
  const [timeout_, setTimeout_] = useState(30)
  const [result, setResult] = useState<any>(null)
  const [busy, setBusy] = useState(false)
  const [health, setHealth] = useState<any>(null)

  useEffect(() => { void api.sandbox.health().then(setHealth) }, [])

  const run = async () => {
    setBusy(true)
    setResult(null)
    try {
      setResult(await api.sandbox.exec({ code, language, network, timeout: timeout_ }))
    } catch (e: any) {
      setResult({ ok: false, error: e.message })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mx-auto max-w-3xl p-4">
      {health && (
        <div className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border bg-panel px-3 py-2 text-[11px]">
          <Terminal size={12} />
          <span>后端 <b>{health.backend}</b></span>
          <span className="text-faint">·</span>
          <span style={{ color: health.available ? 'var(--ok)' : 'var(--err)' }}>
            {health.available ? '可用' : '不可用'}
          </span>
          <span className="text-faint">· {health.selected_because}</span>
          {health.warning && <span className="text-[var(--warn)]">· {health.warning}</span>}
        </div>
      )}

      <div className="mb-2 flex flex-wrap items-center gap-2">
        <select className="field w-28" value={language} onChange={(e) => setLanguage(e.target.value)}>
          <option value="python">Python</option>
          <option value="bash">Bash</option>
          <option value="node">Node.js</option>
        </select>
        <label className="flex items-center gap-1.5 text-[11.5px]">
          超时
          <input className="field w-16" type="number" value={timeout_}
                 onChange={(e) => setTimeout_(Number(e.target.value))} />
          秒
        </label>
        <label className="flex items-center gap-1.5 text-[11.5px]">
          <input type="checkbox" className="accent-[var(--accent)]" checked={network}
                 onChange={(e) => setNetwork(e.target.checked)} />
          允许联网
        </label>
        <div className="flex-1" />
        <button className="btn btn-primary" onClick={run} disabled={busy}>
          {busy ? <Spinner /> : <Play size={12} />} 执行
        </button>
      </div>

      <textarea className="field mono text-[11.5px]" rows={14} spellCheck={false}
                value={code} onChange={(e) => setCode(e.target.value)} />

      {result && (
        <div className="mt-3">
          <div className="mb-1 flex flex-wrap items-center gap-2 text-[11px]">
            <span style={{ color: result.ok ? 'var(--ok)' : 'var(--err)' }}>
              {result.ok ? '✓ 成功' : `✕ 失败 (exit ${result.exit_code})`}
            </span>
            {result.duration_ms != null && <span className="text-faint">{result.duration_ms}ms</span>}
            {result.backend && <span className="chip">{result.backend}</span>}
            {result.timed_out && <span className="chip" style={{ color: 'var(--warn)' }}>超时</span>}
          </div>
          {result.stdout && (
            <pre className="mono max-h-72 overflow-auto whitespace-pre-wrap rounded border bg-panel p-2 text-[11px]">
              {result.stdout}
            </pre>
          )}
          {result.stderr && (
            <pre className="mono mt-2 max-h-52 overflow-auto whitespace-pre-wrap rounded border p-2 text-[11px] text-[var(--err)]"
                 style={{ borderColor: 'var(--err)' }}>
              {result.stderr}
            </pre>
          )}
          {result.error && <div className="mt-2 text-[11px] text-[var(--err)]">{result.error}</div>}
        </div>
      )}
    </div>
  )
}

// -------------------------------------------------------------------------

function CustomTools() {
  const toast = useToast()
  const refresh = useCatalog((s) => s.refresh)
  const [rows, setRows] = useState<any[]>([])
  const [editing, setEditing] = useState<any | null>(null)

  const load = async () => setRows(await api.customTools.list().catch(() => []))
  useEffect(() => { void load() }, [])

  return (
    <div className="mx-auto max-w-3xl p-4">
      <div className="mb-3 flex items-center justify-between">
        <div>
          <div className="text-sm font-semibold">自定义工具</div>
          <div className="text-[11px] text-faint">HTTP 模板，或跑在沙箱里的一段 Python</div>
        </div>
        <button className="btn btn-primary" onClick={() => setEditing({ kind: 'http', config: {}, parameters: {} })}>
          <Plus size={12} /> 新建
        </button>
      </div>

      {!rows.length && <Empty icon={<Wrench size={20} />} title="还没有自定义工具" />}
      <div className="space-y-2">
        {rows.map((row) => (
          <div key={row.id} className="flex items-center gap-2 rounded-lg border bg-panel p-3">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="mono text-[12px] font-medium">{row.name}</span>
                <span className="chip">{row.kind}</span>
              </div>
              <div className="truncate text-[11px] text-faint">{row.description}</div>
            </div>
            <button className="btn btn-sm" onClick={() => setEditing(row)}>编辑</button>
            <button
              className="btn btn-sm btn-ghost"
              onClick={async () => {
                if (!confirm(`删除 ${row.name}？`)) return
                await api.customTools.remove(row.id)
                await load(); await refresh()
              }}
            >
              <Trash2 size={11} className="text-[var(--err)]" />
            </button>
          </div>
        ))}
      </div>

      {editing && (
        <CustomToolEditor
          row={editing}
          onClose={() => setEditing(null)}
          onSaved={async () => { setEditing(null); await load(); await refresh(); toast('已保存', 'ok') }}
        />
      )}
    </div>
  )
}

function CustomToolEditor({ row, onClose, onSaved }: { row: any; onClose: () => void; onSaved: () => void }) {
  const toast = useToast()
  const [form, setForm] = useState<any>({
    name: row.name ?? '', description: row.description ?? '', kind: row.kind ?? 'http',
    parameters: row.parameters ?? { type: 'object', properties: {}, required: [] },
    config: row.config ?? {}, enabled: row.enabled ?? true,
  })
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    setBusy(true)
    try {
      if (row.id) await api.customTools.update(row.id, form)
      else await api.customTools.create(form)
      onSaved()
    } catch (e: any) {
      toast(e.message ?? '保存失败', 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal open onClose={onClose} title={row.id ? '编辑工具' : '新建工具'} width={620}
           footer={<>
             <button className="btn" onClick={onClose}>取消</button>
             <button className="btn btn-primary" onClick={submit} disabled={busy || !form.name}>保存</button>
           </>}>
      <div className="mb-3">
        <label className="label">名称（英文，模型按这个名字调用）</label>
        <input className="field mono" value={form.name} placeholder="my_tool"
               onChange={(e) => setForm({ ...form, name: e.target.value })} />
      </div>
      <div className="mb-3">
        <label className="label">描述（模型据此决定什么时候用它，写清楚很重要）</label>
        <textarea className="field" rows={2} value={form.description}
                  onChange={(e) => setForm({ ...form, description: e.target.value })} />
      </div>
      <div className="mb-3">
        <label className="label">类型</label>
        <select className="field" value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })}>
          <option value="http">HTTP 请求</option>
          <option value="python">Python（沙箱执行）</option>
        </select>
      </div>
      <div className="mb-3">
        <label className="label">参数 JSON Schema</label>
        <JsonInput value={form.parameters} onChange={(v) => setForm({ ...form, parameters: v })} rows={6} />
      </div>

      {form.kind === 'http' ? (
        <>
          <div className="mb-3">
            <label className="label">URL（可以用 {'{{ 参数名 }}'} 插值）</label>
            <input className="field mono text-[11px]" value={form.config.url ?? ''}
                   placeholder="https://api.example.com/search?q={{ query }}"
                   onChange={(e) => setForm({ ...form, config: { ...form.config, url: e.target.value } })} />
          </div>
          <div className="mb-3 flex gap-2">
            <div className="w-32">
              <label className="label">方法</label>
              <select className="field" value={form.config.method ?? 'GET'}
                      onChange={(e) => setForm({ ...form, config: { ...form.config, method: e.target.value } })}>
                {['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].map((m) => <option key={m}>{m}</option>)}
              </select>
            </div>
            <label className="mt-6 flex items-center gap-1.5 text-[11.5px]">
              <input type="checkbox" className="accent-[var(--accent)]" checked={!!form.config.parse_json}
                     onChange={(e) => setForm({ ...form, config: { ...form.config, parse_json: e.target.checked } })} />
              把响应按 JSON 解析
            </label>
          </div>
          <div className="mb-3">
            <label className="label">请求头</label>
            <JsonInput value={form.config.headers ?? {}} rows={3}
                       onChange={(v) => setForm({ ...form, config: { ...form.config, headers: v } })} />
          </div>
          <div>
            <label className="label">请求体（JSON，可插值）</label>
            <JsonInput value={form.config.body} rows={4}
                       onChange={(v) => setForm({ ...form, config: { ...form.config, body: v } })} />
          </div>
        </>
      ) : (
        <div>
          <label className="label">Python 代码（参数在 args 变量里，用 print 输出结果）</label>
          <textarea
            className="field mono text-[11px]" rows={10} spellCheck={false}
            value={form.config.code ?? ''}
            placeholder={'import json\nprint(json.dumps({"echo": args}))'}
            onChange={(e) => setForm({ ...form, config: { ...form.config, code: e.target.value } })}
          />
        </div>
      )}
    </Modal>
  )
}

// -------------------------------------------------------------------------

function McpServers() {
  const toast = useToast()
  const refresh = useCatalog((s) => s.refresh)
  const [rows, setRows] = useState<any[]>([])
  const [editing, setEditing] = useState<any | null>(null)
  const [probing, setProbing] = useState<string | null>(null)

  const load = async () => setRows(await api.mcp.list().catch(() => []))
  useEffect(() => { void load() }, [])

  const probe = async (row: any) => {
    setProbing(row.id)
    try {
      const res = await api.mcp.probe(row.id)
      toast(res.ok ? `连通，发现 ${res.tools.length} 个工具` : `失败：${res.error}`, res.ok ? 'ok' : 'error')
      await load(); await refresh()
    } finally {
      setProbing(null)
    }
  }

  return (
    <div className="mx-auto max-w-3xl p-4">
      <div className="mb-3 flex items-center justify-between">
        <div>
          <div className="text-sm font-semibold">MCP 接入</div>
          <div className="text-[11px] text-faint">
            接入后工具会出现在工具库，节点里用 <code className="mono">mcp:服务名/工具名</code> 引用
          </div>
        </div>
        <div className="flex gap-2">
          <button className="btn" onClick={async () => { await api.mcp.refresh(); await load(); await refresh() }}>
            <RefreshCw size={12} /> 重新加载
          </button>
          <button className="btn btn-primary" onClick={() => setEditing({ transport: 'stdio', args: [], env: {} })}>
            <Plus size={12} /> 添加
          </button>
        </div>
      </div>

      {!rows.length && <Empty icon={<Plug size={20} />} title="还没有接入 MCP server"
                             hint="例如文件系统、数据库、浏览器自动化等现成的 MCP server。" />}
      <div className="space-y-2">
        {rows.map((row) => (
          <div key={row.id} className="rounded-lg border bg-panel p-3">
            <div className="flex items-center gap-2">
              <span className="h-2 w-2 rounded-full"
                    style={{ background: row.status === 'ok' ? 'var(--ok)' : row.status === 'error' ? 'var(--err)' : 'var(--text-faint)' }} />
              <span className="text-[12.5px] font-medium">{row.name}</span>
              <span className="chip">{row.transport}</span>
              {!!row.tools_cache?.length && <span className="chip">{row.tools_cache.length} 个工具</span>}
              <div className="flex-1" />
              <button className="btn btn-sm" disabled={probing === row.id} onClick={() => probe(row)}>
                {probing === row.id ? <Spinner size={11} /> : <Plug size={11} />} 测试
              </button>
              <button className="btn btn-sm" onClick={() => setEditing(row)}>编辑</button>
              <button className="btn btn-sm btn-ghost" onClick={async () => {
                if (!confirm(`删除 ${row.name}？`)) return
                await api.mcp.remove(row.id); await load(); await refresh()
              }}>
                <Trash2 size={11} className="text-[var(--err)]" />
              </button>
            </div>
            <div className="mono mt-1 text-[10.5px] text-faint">
              {row.transport === 'stdio' ? `${row.command} ${(row.args ?? []).join(' ')}` : row.url}
            </div>
            {row.last_error && <div className="mt-1 text-[10.5px] text-[var(--err)]">{row.last_error}</div>}
          </div>
        ))}
      </div>

      {editing && (
        <McpEditor row={editing} onClose={() => setEditing(null)}
                   onSaved={async () => { setEditing(null); await load(); await refresh(); toast('已保存', 'ok') }} />
      )}
    </div>
  )
}

function McpEditor({ row, onClose, onSaved }: { row: any; onClose: () => void; onSaved: () => void }) {
  const toast = useToast()
  const [form, setForm] = useState<any>({
    name: row.name ?? '', transport: row.transport ?? 'stdio', command: row.command ?? '',
    args: row.args ?? [], env: row.env ?? {}, url: row.url ?? '', enabled: row.enabled ?? true,
  })
  const [argsText, setArgsText] = useState((row.args ?? []).join(' '))

  const submit = async () => {
    try {
      const payload = { ...form, args: argsText.split(/\s+/).filter(Boolean) }
      if (row.id) await api.mcp.update(row.id, payload)
      else await api.mcp.create(payload)
      onSaved()
    } catch (e: any) {
      toast(e.message ?? '保存失败', 'error')
    }
  }

  return (
    <Modal open onClose={onClose} title={row.id ? '编辑 MCP server' : '添加 MCP server'}
           footer={<>
             <button className="btn" onClick={onClose}>取消</button>
             <button className="btn btn-primary" onClick={submit} disabled={!form.name}>保存</button>
           </>}>
      <div className="mb-3">
        <label className="label">名称</label>
        <input className="field" value={form.name} placeholder="filesystem"
               onChange={(e) => setForm({ ...form, name: e.target.value })} />
      </div>
      <div className="mb-3">
        <label className="label">传输方式</label>
        <select className="field" value={form.transport}
                onChange={(e) => setForm({ ...form, transport: e.target.value })}>
          <option value="stdio">stdio（本地子进程）</option>
          <option value="http">HTTP（远程）</option>
        </select>
      </div>
      {form.transport === 'stdio' ? (
        <>
          <div className="mb-3">
            <label className="label">命令</label>
            <input className="field mono text-[11px]" value={form.command} placeholder="npx"
                   onChange={(e) => setForm({ ...form, command: e.target.value })} />
          </div>
          <div className="mb-3">
            <label className="label">参数（空格分隔）</label>
            <input className="field mono text-[11px]" value={argsText}
                   placeholder="-y @modelcontextprotocol/server-filesystem /tmp"
                   onChange={(e) => setArgsText(e.target.value)} />
          </div>
          <div>
            <label className="label">环境变量</label>
            <JsonInput value={form.env} onChange={(v) => setForm({ ...form, env: v })} rows={3} />
          </div>
        </>
      ) : (
        <div>
          <label className="label">URL</label>
          <input className="field mono text-[11px]" value={form.url}
                 placeholder="https://example.com/mcp"
                 onChange={(e) => setForm({ ...form, url: e.target.value })} />
        </div>
      )}
    </Modal>
  )
}
