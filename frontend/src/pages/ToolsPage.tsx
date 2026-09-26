import { useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { AlertTriangle, Play, Plug, Plus, RefreshCw, Search, Terminal, Wand2, Wrench } from 'lucide-react'
import clsx from 'clsx'
import { api, ApiError } from '../api/client'
import { useCatalog, useOnReconnect } from '../store/catalog'
import {
  confirmDialog, EmptyState, ErrorState, Field, JsonInput, Kbd, Modal, Skeleton, Spinner, StatusBadge, TabPanel,
  Tabs, toast, useTabRoute,
} from '../components/ui'
import { formatDuration } from '../lib/format'
import { ariaShortcut, matchShortcut } from '../lib/keys'
import { useRunClock } from '../run/useRunClock'
import type { ToolInfo } from '../types'
import {
  checkHealth, DeleteButton, deferDelete, forgetHealth, HealthPill, PageHeader, SectionBar, useHealth,
  withoutDeferred, workflowList, workflowsMentioning,
} from './DataSourcesTab'
import type { HealthRecord } from './DataSourcesTab'

// 提到模块级：tab 名同时是 URL 的最后一段，两处各写一份迟早对不上
const TABS = [
  // key 是 URL 的一段。叫 tools 的话地址会变成 /tools/tools/<id>，
  // 读起来像敲重了。这个 URL 今天才上线，改的代价最小
  { key: 'library', label: '工具库' },
  { key: 'sandbox', label: '沙箱试验台' },
  { key: 'custom', label: '自定义工具' },
  { key: 'mcp', label: 'MCP 接入' },
]

export function ToolsPage() {
  const [tab, setTab] = useTabRoute(TABS.map((t) => t.key), 'library')
  return (
    <div className="flex h-full flex-col">
      <PageHeader
        icon={<Wrench size={13} />}
        title="工具"
        subtitle="不用搭工作流就能单独试跑工具；自定义工具和 MCP 接进来后，节点里就能用"
      />
      <Tabs tabs={TABS} active={tab} onChange={setTab} label="工具" idPrefix="tools" />
      <TabPanel idPrefix="tools" tabKey={tab} className="min-h-0 flex-1 overflow-y-auto">
        {tab === 'library' && <ToolLibrary />}
        {tab === 'sandbox' && <SandboxLab />}
        {tab === 'custom' && <CustomTools />}
        {tab === 'mcp' && <McpServers />}
      </TabPanel>
    </div>
  )
}

// -------------------------------------------------------------------------

/**
 * 按参数 Schema 拼一份示例参数：有 default / examples / enum 用它们，否则按类型给
 * 空值。all=false 只拼必填项——选中工具时先填好骨架，比空的 {} 少敲一半。
 */
export function exampleArgs(schema: any, all = false): Record<string, any> {
  const props: Record<string, any> = schema?.properties ?? {}
  const required: string[] = schema?.required ?? []
  const out: Record<string, any> = {}
  for (const [key, spec] of Object.entries(props)) {
    if (!all && !required.includes(key) && spec?.default === undefined) continue
    out[key] = sampleOf(spec)
  }
  return out
}

function sampleOf(spec: any): any {
  if (spec?.default !== undefined) return spec.default
  if (Array.isArray(spec?.examples) && spec.examples.length) return spec.examples[0]
  if (Array.isArray(spec?.enum) && spec.enum.length) return spec.enum[0]
  const type = Array.isArray(spec?.type) ? spec.type[0] : spec?.type
  switch (type) {
    case 'integer': case 'number': return 0
    case 'boolean': return false
    case 'array': return []
    case 'object': return spec?.properties ? exampleArgs(spec, true) : {}
    default: return ''
  }
}

/** 在工具库里直接执行时，它的副作用落在哪里。说实话：内置工具有围栏，外接的没有 */
function sideEffectNote(tool: ToolInfo): string {
  if (tool.source !== 'builtin') return '自定义 / MCP 工具的副作用由它自己决定，这里拦不住：它会真的发请求、真的改外部系统。'
  if (tool.category === '文件') return '文件读写锁在 playground 工作目录里，碰不到别处。'
  if (tool.category === '网络') return '内网、回环和云元数据地址会被拦下，公网请求会真的发出去。'
  if (tool.category === '沙箱') return '代码在沙箱里跑，受沙箱的超时、内存和联网限制。'
  return ''
}

function ToolLibrary() {
  const tools = useCatalog((s) => s.tools)
  const { id } = useParams()
  const navigate = useNavigate()
  const [args, setArgs] = useState<any>({})
  const [result, setResult] = useState<any>(null)
  const [busy, setBusy] = useState(false)
  const [query, setQuery] = useState('')
  const since = useRef(0)
  const clock = useRunClock(busy)

  // 选中哪个由地址说了算：这样一个工具的参数说明可以直接发给别人
  const picked = tools.find((t) => t.id === id) ?? null
  useEffect(() => {
    setArgs(picked ? exampleArgs(picked.schema) : {})
    setResult(null)
  }, [picked?.id])

  const run = async () => {
    if (!picked || busy) return
    since.current = Date.now()
    setBusy(true)
    setResult(null)
    try {
      let out: any
      try {
        out = await api.tools.run(picked.id, args ?? {})
      } catch (e) {
        // 有副作用的工具，后端先回 409 说明它要做什么；人看过、点了确认才真跑。
        // 工作流里的「运行时需审批」是另一回事，这里不经审批关卡
        if (!(e instanceof ApiError) || e.status !== 409) throw e
        setBusy(false)
        const ok = await confirmDialog({
          title: `执行 ${picked.name}？`,
          body: e.message,
          consequences: [sideEffectNote(picked)].filter(Boolean),
          confirmLabel: '确认执行',
          danger: true,
        })
        if (!ok) return
        since.current = Date.now()
        setBusy(true)
        out = await api.tools.run(picked.id, args ?? {}, { confirm: true })
      }
      setResult(out)
    } catch (e) {
      setResult({ ok: false, thrown: e })
    } finally {
      setBusy(false)
    }
  }

  const needle = query.trim().toLowerCase()
  const shown = tools.filter(
    (t) => !needle || [t.name, t.description, t.category].some((s) => s?.toLowerCase().includes(needle)),
  )
  const groups = [...new Set(shown.map((t) => t.category))]
  const props = Object.entries((picked?.schema?.properties ?? {}) as Record<string, any>)

  return (
    <div className="flex h-full">
      <div className="flex w-[320px] shrink-0 flex-col border-r">
        <div className="flex items-center gap-2 border-b px-3 py-2">
          <Search size={12} className="text-faint" aria-hidden />
          <input className="min-w-0 flex-1 bg-transparent text-xs outline-none placeholder:text-faint"
                 placeholder={`搜索 ${tools.length} 个工具…`} aria-label="搜索工具" value={query}
                 onChange={(e) => setQuery(e.target.value)} />
        </div>
        <nav className="flex-1 overflow-y-auto p-2" aria-label="工具列表">
          {groups.map((g) => (
            <div key={g} className="mb-3">
              <div className="mb-1 px-1.5 text-2xs font-semibold tracking-wide text-faint">{g}</div>
              {shown.filter((t) => t.category === g).map((t) => {
                const on = picked?.id === t.id
                return (
                  <button
                    key={t.id}
                    onClick={() => navigate(`/tools/library/${t.id}`)}
                    aria-current={on ? 'true' : undefined}
                    className={clsx(
                      'relative w-full rounded-md px-2 py-1.5 text-left hover:bg-hover',
                      on && 'bg-hover',
                    )}
                  >
                    {on && <span className="absolute inset-y-1.5 left-0 w-0.5 rounded bg-[var(--accent)]" aria-hidden />}
                    <div className="flex items-center gap-1.5">
                      <span className="mono truncate text-xs">{t.name}</span>
                      <ApprovalTag tool={t} />
                    </div>
                    <div className="truncate text-2xs text-faint">{t.description}</div>
                  </button>
                )
              })}
            </div>
          ))}
          {!shown.length && (
            <EmptyState offline={false} icon={<Wrench size={20} />} title="没有匹配的工具"
                        action={<button className="btn btn-sm" onClick={() => setQuery('')}>清空搜索</button>} />
          )}
        </nav>
      </div>

      <div className="min-w-0 flex-1 overflow-y-auto p-4">
        {!picked && (
          <EmptyState icon={<Wrench size={22} />} title="选一个工具试试"
                      body="不用搭工作流就能单独调用工具，验证参数和返回格式。" />
        )}
        {picked && (
          <div className="max-w-2xl">
            <div className="mb-1 flex flex-wrap items-center gap-2">
              <span className="mono text-sm font-semibold">{picked.name}</span>
              <span className="chip">{({ builtin: '内置', custom: '自定义', mcp: 'MCP' } as Record<string, string>)[picked.source] ?? picked.source}</span>
              <ApprovalTag tool={picked} long />
            </div>
            <p className="mb-3 text-xs leading-relaxed text-dim">{picked.description}</p>

            {picked.dangerous && (
              <div className="mb-3 flex gap-2 rounded-lg border px-2.5 py-2 text-xs leading-relaxed"
                   style={{ borderColor: 'color-mix(in srgb, var(--warn) 40%, var(--border))', background: 'color-mix(in srgb, var(--warn) 7%, transparent)' }}>
                <AlertTriangle size={13} className="mt-0.5 shrink-0 text-[var(--warn)]" aria-hidden />
                <span>
                  这里直接执行，不经审批：执行前会先说明它要做什么，确认后才跑。{sideEffectNote(picked)}
                </span>
              </div>
            )}

            <div className="mb-1 flex items-center gap-2">
              <label className="label !mb-0" htmlFor="tool-args">参数</label>
              {props.length > 0 && (
                <button className="btn btn-xs ml-auto" onClick={() => setArgs(exampleArgs(picked.schema, true))}
                        title="按参数说明把每一项都填上示例值">
                  <Wand2 size={11} aria-hidden /> 填入全部参数
                </button>
              )}
            </div>
            <JsonInput key={picked.id} id="tool-args" value={args} onChange={setArgs} rows={6} placeholder="{}" />

            {props.length > 0 && (
              <div className="mt-2 overflow-hidden rounded-lg border bg-panel">
                <div className="border-b px-2.5 py-1 text-2xs font-semibold text-faint">参数说明</div>
                {props.map(([key, spec]) => (
                  <div key={key} className="flex gap-2 border-b border-[var(--hairline)] px-2.5 py-1 text-xs last:border-0">
                    <code className="mono w-32 shrink-0 truncate text-[var(--accent)]">{key}</code>
                    <span className="mono w-16 shrink-0 text-2xs leading-5 text-faint">{spec.type}</span>
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

            <button className="btn btn-primary mt-3 tnum" onClick={() => void run()} disabled={busy}>
              {busy ? <><Spinner /> 执行中 {formatDuration(clock - since.current)}</> : <><Play size={12} aria-hidden /> 执行</>}
            </button>

            {result && <ToolResult result={result} />}
          </div>
        )}
      </div>
    </div>
  )
}

/**
 * 「运行时需审批」只给真会停下来等人的工具打（后端的 runtime_approval）。
 * 自定义和 MCP 工具在工作流里运行时审批关卡认不出它们，就明说「运行时不审批」——
 * 以前它们也挂着「需确认」，标签说的不是实话，给人虚假的安全感。
 */
function ApprovalTag({ tool, long = false }: { tool: ToolInfo; long?: boolean }) {
  const needs = tool.runtime_approval ?? (tool.source === 'builtin' && tool.dangerous)
  if (needs) {
    return (
      <span className="chip shrink-0" style={{ color: 'var(--st-waiting)', borderColor: 'color-mix(in srgb, var(--st-waiting) 40%, transparent)' }}
            title="在工作流里跑到它会停下来等人工审批（审批策略为「危险工具」时）。在工具库里直接执行不经审批，执行前会先说明它要做什么。">
        <StatusBadge status="waiting" size={10} decorative />运行时需审批
      </span>
    )
  }
  if (tool.source !== 'builtin') {
    return (
      <span className="chip shrink-0 border-dashed"
            title="自定义工具和 MCP 工具在工作流里运行时不会停下来等审批：审批关卡还认不出它们。有副作用的话在节点上另行把关。">
        运行时不审批{long ? ' · 副作用自负' : ''}
      </span>
    )
  }
  return null
}

function ToolResult({ result }: { result: any }) {
  const failed = result.ok === false
  return (
    <div className="mt-3" data-tool-result={failed ? 'fail' : 'ok'}>
      <div className="mb-1 flex items-center gap-1.5 text-xs">
        <StatusBadge status={failed ? 'failed' : 'done'} size={12} decorative />
        <span style={{ color: failed ? 'var(--err)' : 'var(--text-dim)' }}>{failed ? '失败' : '成功'}</span>
        {result.duration_ms != null && <span className="tnum text-faint">· {formatDuration(result.duration_ms)}</span>}
      </div>
      {failed ? (
        <ErrorState compact error={result.thrown ?? result} />
      ) : (
        <pre className="mono max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-lg border bg-panel p-2.5 text-xs leading-relaxed">
          {typeof result.result === 'string' ? result.result : JSON.stringify(result.result, null, 2)}
        </pre>
      )}
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
  const since = useRef(0)
  const clock = useRunClock(busy)
  // 结果头要用发出请求时的超时，不是之后又改过的输入框
  const sentTimeout = useRef(timeout_)

  const loadHealth = () => api.sandbox.health().then(setHealth, () => {})
  useEffect(() => { void loadHealth() }, [])
  useOnReconnect(loadHealth)

  const run = async () => {
    if (busy) return
    since.current = Date.now()
    sentTimeout.current = timeout_
    setBusy(true)
    setResult(null)
    try {
      setResult(await api.sandbox.exec({ code, language, network, timeout: timeout_ }))
    } catch (e) {
      setResult({ ok: false, thrown: e })
    } finally {
      setBusy(false)
    }
  }

  // 没有退出码就是请求没到沙箱，别拼出「exit undefined」
  const headline = !result ? '' : result.thrown ? '请求没到沙箱'
    : result.ok ? '成功'
    : result.timed_out ? `超过 ${sentTimeout.current} 秒被终止`
    : result.exit_code == null ? '请求没到沙箱'
    : `失败 · 退出码 ${result.exit_code}`

  return (
    <div className="mx-auto max-w-4xl p-4">
      {health && (
        <div className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border bg-panel px-3 py-2 text-xs">
          <Terminal size={12} className="text-dim" aria-hidden />
          <span>沙箱 <span className="mono font-medium">{health.backend}</span></span>
          <span className="inline-flex items-center gap-1" style={{ color: health.available ? 'var(--text-dim)' : 'var(--err)' }}>
            <StatusBadge status={health.available ? 'done' : 'failed'} size={11} decorative />
            {health.available ? '可用' : '不可用'}
          </span>
          <span className="min-w-0 truncate text-faint" title={health.selected_because}>· {health.selected_because}</span>
          {health.warning && <span className="w-full text-2xs text-[var(--warn)]">{health.warning}</span>}
        </div>
      )}

      <div className="mb-2 flex flex-wrap items-center gap-3">
        <select className="field w-28" value={language} aria-label="语言" onChange={(e) => setLanguage(e.target.value)}>
          <option value="python">Python</option>
          <option value="bash">Bash</option>
          <option value="node">Node.js</option>
        </select>
        <label className="flex items-center gap-1.5 text-xs">
          超时
          <input className="field w-16 tnum" type="number" min={1} value={timeout_}
                 onChange={(e) => setTimeout_(Math.max(1, Number(e.target.value) || 1))} />
          秒
        </label>
        <label className="flex items-center gap-1.5 text-xs">
          <input type="checkbox" checked={network} onChange={(e) => setNetwork(e.target.checked)} />
          允许联网
        </label>
        <div className="flex-1" />
        <Kbd combo="Mod+Enter" className="text-faint" />
        <button className="btn btn-primary tnum" onClick={() => void run()} disabled={busy} aria-keyshortcuts={ariaShortcut('Mod+Enter')}>
          {busy ? <><Spinner /> 执行中 {formatDuration(clock - since.current)}</> : <><Play size={12} aria-hidden /> 执行</>}
        </button>
      </div>

      <textarea className="field mono text-xs" rows={14} spellCheck={false} aria-label="代码"
                value={code} onChange={(e) => setCode(e.target.value)}
                onKeyDown={(e) => { if (matchShortcut(e.nativeEvent, 'Mod+Enter')) { e.preventDefault(); void run() } }} />

      {result && (
        <div className="mt-3" data-sandbox-result>
          <div className="mb-1 flex flex-wrap items-center gap-2 text-xs">
            <StatusBadge status={result.ok ? 'done' : 'failed'} size={12} decorative />
            <span style={{ color: result.ok ? 'var(--text-dim)' : 'var(--err)' }}>{headline}</span>
            {result.duration_ms != null && <span className="tnum text-faint">{formatDuration(result.duration_ms)}</span>}
            {result.backend && <span className="chip mono">{result.backend}</span>}
          </div>
          {result.thrown && <ErrorState compact error={result.thrown} onRetry={() => void run()} />}
          {result.stdout && (
            <pre className="mono max-h-72 overflow-auto whitespace-pre-wrap rounded-lg border bg-panel p-2.5 text-xs">
              {result.stdout}
            </pre>
          )}
          {result.stderr && (
            <pre className="mono mt-2 max-h-52 overflow-auto whitespace-pre-wrap rounded-lg border p-2.5 text-xs text-[var(--err)]"
                 style={{ borderColor: 'color-mix(in srgb, var(--err) 40%, var(--border))' }}>
              {result.stderr}
            </pre>
          )}
          {result.error && !result.thrown && <ErrorState compact error={result} className="mt-2" />}
        </div>
      )}
    </div>
  )
}

// -------------------------------------------------------------------------

/** 新建工具时参数的起手式：一个必填的字符串参数，URL 模板里正好能插它 */
const PARAMS_EXAMPLE = {
  type: 'object',
  properties: { query: { type: 'string', description: '要查的关键词' } },
  required: ['query'],
}
const PARAMS_NONE = { type: 'object', properties: {}, required: [] }

function CustomTools() {
  const refresh = useCatalog((s) => s.refresh)
  const navigate = useNavigate()
  const [rows, setRows] = useState<any[] | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [editing, setEditing] = useState<any | null>(null)

  const load = async () => {
    try {
      // 撤销窗口里的那条 DELETE 还没发：存完别的工具一重拉，它不能跟着回来
      setRows(withoutDeferred(await api.customTools.list(), '/api/custom-tools'))
      setError(null)
    } catch (e) {
      if (rows) toast.error(e)
      else setError(e)
    }
  }
  useEffect(() => { void load() }, [])
  useOnReconnect(load)

  const remove = (row: any) => deferDelete({
    what: `工具「${row.name}」`,
    url: `/api/custom-tools/${row.id}`,
    hide: () => setRows((rs) => rs && rs.filter((r) => r.id !== row.id)),
    restore: () => void load(),
    commit: () => api.customTools.remove(row.id),
    done: () => void refresh(),
  })

  const create = () => setEditing({ kind: 'http', config: {}, enabled: true })

  return (
    <div className="mx-auto max-w-4xl p-4">
      <SectionBar title="自定义工具" hint="HTTP 模板，或跑在沙箱里的一段 Python。启用后出现在工具库，节点里按名字引用。">
        <button className="btn btn-primary btn-sm" onClick={create}><Plus size={12} aria-hidden /> 新建工具</button>
      </SectionBar>

      {rows === null ? (
        error ? <ErrorState error={error} onRetry={() => void load()} /> : <Skeleton rows={3} height={52} gap={8} />
      ) : !rows.length ? (
        <EmptyState
          icon={<Wrench size={22} />}
          title="还没有自定义工具"
          body="把 MES、ERP 的查询接口包成工具，模型就能按需调用。写好参数说明，保存后能直接试跑。"
          action={<button className="btn btn-primary btn-sm" onClick={create}><Plus size={12} aria-hidden /> 新建工具</button>}
        />
      ) : (
        <div className="space-y-2">
          {rows.map((row) => (
            <div key={row.id} className="flex items-center gap-2 rounded-lg border bg-panel px-3 py-2.5">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="mono text-sm font-medium">{row.name}</span>
                  <span className="chip">{row.kind === 'python' ? 'Python' : 'HTTP'}</span>
                  {!row.enabled && <span className="chip" title="停用的工具不出现在工具库和节点里">已停用</span>}
                </div>
                <div className="truncate text-xs text-faint">{row.description || '没写描述：模型不知道什么时候该用它'}</div>
              </div>
              <button className="btn btn-sm" disabled={!row.enabled}
                      title={row.enabled ? '去工具库里带参数试跑' : '停用的工具不在工具库里'}
                      onClick={() => navigate(`/tools/library/${row.name}`)}>
                <Play size={11} aria-hidden /> 试跑
              </button>
              <button className="btn btn-sm btn-ghost" onClick={() => setEditing(row)}>编辑</button>
              <DeleteButton label={`删除自定义工具 ${row.name}`} onClick={() => remove(row)} />
            </div>
          ))}
        </div>
      )}

      {editing && (
        <CustomToolEditor
          row={editing}
          onClose={() => setEditing(null)}
          onSaved={async (saved) => {
            setEditing(null)
            await load()
            await refresh()
            toast.ok(`已保存工具「${saved.name}」`)
          }}
        />
      )}
    </div>
  )
}

const TOOL_NAME_RE = /^[a-zA-Z_][a-zA-Z0-9_]*$/

function CustomToolEditor({ row, onClose, onSaved }: { row: any; onClose: () => void; onSaved: (saved: any) => void }) {
  const [initial] = useState(() => ({
    name: row.name ?? '', description: row.description ?? '', kind: row.kind ?? 'http',
    // 只有新建时给起手式。已有的工具显示存着的样子，哪怕是 {}：后端按它生成参数
    // 校验，悄悄换成示例的话只改一句描述也会把 query 存成必填，不带参数调用它的
    // 节点从此过不了校验
    parameters: row.id ? (row.parameters ?? PARAMS_NONE) : PARAMS_EXAMPLE,
    config: row.config ?? {}, enabled: row.enabled ?? true,
  }))
  const [form, setForm] = useState<any>(initial)
  // JsonInput 敲过字之后不再跟外面的值同步：插入示例时换个 key 让它重新挂载
  const [paramsKey, setParamsKey] = useState(0)
  const noParams = !Object.keys(form.parameters?.properties ?? {}).length
  const [busy, setBusy] = useState(false)
  const [args, setArgs] = useState<any>(() => exampleArgs(initial.parameters, true))
  const [trial, setTrial] = useState<{ busy?: boolean; result?: any } | null>(null)

  const nameError = form.name && !TOOL_NAME_RE.test(form.name) ? '只能用英文字母、数字、下划线，不能以数字开头' : null
  const dirty = JSON.stringify(form) !== JSON.stringify(initial)
  const missing = [!form.name && '名称', form.kind === 'http' && !form.config.url && 'URL', form.kind === 'python' && !form.config.code && '代码']
    .filter(Boolean) as string[]

  const submit = async () => {
    if (missing.length || nameError) return
    setBusy(true)
    try {
      const saved = row.id ? await api.customTools.update(row.id, form) : await api.customTools.create(form)
      onSaved(saved ?? form)
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }

  const tryIt = async () => {
    setTrial({ busy: true })
    try {
      setTrial({ result: await api.customTools.test(row.id, args ?? {}) })
    } catch (e) {
      setTrial({ result: { ok: false, thrown: e } })
    }
  }

  const setConfig = (patch: any) => setForm((f: any) => ({ ...f, config: { ...f.config, ...patch } }))

  return (
    <Modal open onClose={onClose} dirty={dirty} title={row.id ? `编辑工具「${row.name}」` : '新建工具'} width={640}
           footer={<>
             <button className="btn" onClick={onClose}>取消</button>
             <button className="btn btn-primary" onClick={() => void submit()}
                     disabled={busy || missing.length > 0 || !!nameError}
                     title={missing.length ? `还缺：${missing.join('、')}` : undefined}>
               {busy ? <Spinner size={11} /> : null} 保存
             </button>
           </>}>
      <div className="space-y-3">
        <div className="grid grid-cols-[1fr_180px] gap-3">
          <Field label="名称" required error={nameError} hint="英文，模型按这个名字调用">
            {(p) => (
              <input {...p} className="field mono" value={form.name} placeholder="query_mes_order" spellCheck={false}
                     onChange={(e) => setForm({ ...form, name: e.target.value })} />
            )}
          </Field>
          <Field label="类型">
            {(p) => (
              <select {...p} className="field" value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })}>
                <option value="http">HTTP 请求</option>
                <option value="python">Python（沙箱执行）</option>
              </select>
            )}
          </Field>
        </div>
        <Field label="描述" hint="模型据此决定什么时候用它，写清楚很重要">
          {(p) => (
            <textarea {...p} className="field" rows={2} value={form.description}
                      placeholder="按订单号查 MES 里的生产进度，返回工序和完成数量"
                      onChange={(e) => setForm({ ...form, description: e.target.value })} />
          )}
        </Field>
        <Field label="参数 JSON Schema" hint="每个参数写清 description：模型照着它填值">
          {(p) => (
            <>
              <JsonInput key={paramsKey} id={p.id} value={form.parameters} rows={7}
                         onChange={(v) => setForm((f: any) => ({ ...f, parameters: v }))} />
              {noParams && (
                <div className="mt-1 flex items-center gap-2 text-2xs text-faint">
                  <span>现在不收参数：模型调用它时什么都不传</span>
                  <button type="button" className="btn btn-xs"
                          onClick={() => { setForm((f: any) => ({ ...f, parameters: PARAMS_EXAMPLE })); setParamsKey((k) => k + 1) }}>
                    <Wand2 size={11} aria-hidden /> 插入示例参数
                  </button>
                </div>
              )}
            </>
          )}
        </Field>

        {form.kind === 'http' ? (
          <>
            <div className="grid grid-cols-[110px_1fr] gap-3">
              <Field label="方法">
                {(p) => (
                  <select {...p} className="field" value={form.config.method ?? 'GET'}
                          onChange={(e) => setConfig({ method: e.target.value })}>
                    {['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].map((m) => <option key={m}>{m}</option>)}
                  </select>
                )}
              </Field>
              <Field label={<>URL <span className="font-normal text-faint">（可以用 {'{{ 参数名 }}'} 插值）</span></>} required>
                {(p) => (
                  <input {...p} className="field mono text-xs" value={form.config.url ?? ''}
                         placeholder="https://api.example.com/search?q={{ query }}"
                         onChange={(e) => setConfig({ url: e.target.value })} />
                )}
              </Field>
            </div>
            <label className="flex items-center gap-1.5 text-xs">
              <input type="checkbox" checked={!!form.config.parse_json}
                     onChange={(e) => setConfig({ parse_json: e.target.checked })} />
              把响应按 JSON 解析
            </label>
            <Field label="请求头">
              {(p) => <JsonInput id={p.id} value={form.config.headers ?? {}} rows={3} onChange={(v) => setConfig({ headers: v })} />}
            </Field>
            <Field label="请求体（JSON，可插值）">
              {(p) => <JsonInput id={p.id} value={form.config.body} rows={4} onChange={(v) => setConfig({ body: v })} />}
            </Field>
          </>
        ) : (
          <Field label="Python 代码" required hint="参数在 args 变量里，用 print 输出结果">
            {(p) => (
              <textarea {...p} className="field mono text-xs" rows={10} spellCheck={false}
                        value={form.config.code ?? ''}
                        placeholder={'import json\nprint(json.dumps({"echo": args}))'}
                        onChange={(e) => setConfig({ code: e.target.value })} />
            )}
          </Field>
        )}

        <label className="flex items-center gap-2 text-xs">
          <input type="checkbox" checked={!!form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
          启用<span className="text-2xs text-faint">停用后不出现在工具库和节点里</span>
        </label>

        <div className="rounded-lg border bg-bg p-2.5" data-tool-trial>
          <div className="mb-1.5 flex items-center gap-2">
            <span className="text-xs font-medium">试跑</span>
            <span className="text-2xs text-faint">
              {!row.id ? '先保存，才能试跑' : dirty ? '跑的是已保存的版本：改动先保存再试' : '带上参数跑一次，看返回的样子'}
            </span>
            <button className="btn btn-xs ml-auto" onClick={() => setArgs(exampleArgs(form.parameters, true))}
                    title="按参数 Schema 重新填示例">
              <Wand2 size={11} aria-hidden /> 按 Schema 填
            </button>
          </div>
          <JsonInput value={args} onChange={setArgs} rows={3} placeholder="{}" />
          <button className="btn btn-sm mt-2" disabled={!row.id || !!trial?.busy} onClick={() => void tryIt()}>
            {trial?.busy ? <Spinner size={11} /> : <Play size={11} aria-hidden />} 试跑
          </button>
          {trial?.result && <ToolResult result={trial.result} />}
        </div>
      </div>
    </Modal>
  )
}

// -------------------------------------------------------------------------

/** MCP 的连通状态：这次会话里测过的用测的结果，否则用后端记的上次探测状态（不知道是何时的） */
function mcpRecord(row: any, tested?: HealthRecord): HealthRecord | undefined {
  if (tested) return tested
  if (row.status === 'ok') return { ok: true, at: 0, note: `上次探测发现 ${row.tools_cache?.length ?? 0} 个工具` }
  if (row.status === 'error') return { ok: false, at: 0, error: row.last_error || '上次探测失败' }
  return undefined
}

function McpServers() {
  const refresh = useCatalog((s) => s.refresh)
  const tools = useCatalog((s) => s.tools)
  const workflows = useCatalog((s) => s.workflows)
  const [rows, setRows] = useState<any[] | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [editing, setEditing] = useState<any | null>(null)
  const [reloading, setReloading] = useState(false)

  const load = async () => {
    try {
      setRows(await api.mcp.list())
      setError(null)
    } catch (e) {
      if (rows) toast.error(e)
      else setError(e)
    }
  }
  useEffect(() => { void load() }, [])
  useOnReconnect(load)

  const reloadAll = async () => {
    setReloading(true)
    try {
      await api.mcp.refresh()
      await load()
      await refresh()
      toast.ok('已重新加载 MCP 服务')
    } catch (e) {
      toast.error(e)
    } finally {
      setReloading(false)
    }
  }

  const remove = async (row: any) => {
    const ids = [
      ...tools.filter((t) => t.source === 'mcp' && t.id.startsWith(`mcp:${row.name}/`)).map((t) => t.id),
      ...(row.tools_cache ?? []).map((t: string) => `mcp:${row.name}/${t}`),
    ]
    const using = workflowsMentioning(workflows, [...new Set(ids)])
    const ok = await confirmDialog({
      title: `删除 MCP 服务「${row.name}」？`,
      danger: true,
      consequences: [
        row.tools_cache?.length ? `它的 ${row.tools_cache.length} 个工具从工具库消失` : '',
        using.length ? `${workflowList(using)}用到了它的工具，运行到那一步会失败` : '眼下没有工作流用到它的工具',
      ].filter(Boolean),
      confirmLabel: '删除服务',
    })
    if (!ok) return
    try {
      await api.mcp.remove(row.id)
      forgetHealth(`mcp:${row.id}`)
      setRows((rs) => rs && rs.filter((r) => r.id !== row.id))
      await refresh()
      toast.ok(`已删除 MCP 服务「${row.name}」`)
    } catch (e) {
      toast.error(e)
    }
  }

  const create = () => setEditing({ transport: 'stdio', args: [], env: {}, enabled: true })

  return (
    <div className="mx-auto max-w-4xl p-4">
      <SectionBar
        title="MCP 接入"
        hint={<>接入后工具出现在工具库，节点里用 <code className="mono">mcp:服务名/工具名</code> 引用。</>}
      >
        <button className="btn btn-sm" onClick={() => void reloadAll()} disabled={reloading}
                title="重连所有启用的服务，刷新它们的工具清单">
          {reloading ? <Spinner size={11} /> : <RefreshCw size={12} aria-hidden />} 重新加载
        </button>
        <button className="btn btn-primary btn-sm" onClick={create}><Plus size={12} aria-hidden /> 添加服务</button>
      </SectionBar>

      {rows === null ? (
        error ? <ErrorState error={error} onRetry={() => void load()} /> : <Skeleton rows={2} height={60} gap={8} />
      ) : !rows.length ? (
        <EmptyState
          icon={<Plug size={22} />}
          title="还没有接入 MCP 服务"
          body="文件系统、数据库、浏览器自动化这类现成的 MCP server，接进来就是一组新工具。"
          action={<button className="btn btn-primary btn-sm" onClick={create}><Plus size={12} aria-hidden /> 添加服务</button>}
        />
      ) : (
        <div className="space-y-2.5">
          {rows.map((row) => (
            <McpCard key={row.id} row={row} onEdit={() => setEditing(row)} onRemove={() => void remove(row)}
                     onProbed={async () => { await load(); await refresh() }} />
          ))}
        </div>
      )}

      {editing && (
        <McpEditor row={editing} onClose={() => setEditing(null)}
                   onSaved={async (name) => { setEditing(null); await load(); await refresh(); toast.ok(`已保存 MCP 服务「${name}」`) }} />
      )}
    </div>
  )
}

function McpCard({ row, onEdit, onRemove, onProbed }: {
  row: any; onEdit: () => void; onRemove: () => void; onProbed: () => Promise<void>
}) {
  const key = `mcp:${row.id}`
  const { record, checkingSince } = useHealth(key)
  const shown = mcpRecord(row, record)
  const probe = async () => {
    await checkHealth(key, async () => {
      const t0 = performance.now()
      const res = await api.mcp.probe(row.id)
      return {
        ok: !!res.ok, ms: Math.round(performance.now() - t0), error: res.error, hint: res.hint, detail: res.detail,
        note: res.ok ? `发现 ${res.tools?.length ?? 0} 个工具` : undefined,
      }
    })
    await onProbed()
  }
  return (
    <article className="rounded-lg border bg-panel px-3 py-2.5">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
        <Plug size={14} className="text-dim" aria-hidden />
        <span className="text-sm font-medium">{row.name}</span>
        <span className="chip">{row.transport === 'stdio' ? '本地子进程' : 'HTTP'}</span>
        {!!row.tools_cache?.length && <span className="chip tnum">{row.tools_cache.length} 个工具</span>}
        {row.enabled === false && <span className="chip">已停用</span>}
        <span className="flex-1" />
        <HealthPill record={shown} checkingSince={checkingSince} />
        <div className="flex items-center gap-1">
          <button className="btn btn-sm" disabled={!!checkingSince} onClick={() => void probe()}>
            <Plug size={11} aria-hidden /> 测试
          </button>
          <button className="btn btn-sm btn-ghost" onClick={onEdit}>编辑</button>
          <DeleteButton label={`删除 MCP 服务 ${row.name}`} onClick={onRemove} />
        </div>
      </div>
      <div className="mono mt-1.5 break-all text-2xs text-faint">
        {row.transport === 'stdio' ? `${row.command ?? ''} ${(row.args ?? []).join(' ')}` : row.url}
      </div>
      {shown && !shown.ok && !checkingSince && (
        <ErrorState compact error={shown} onRetry={() => void probe()} className="mt-2" />
      )}
    </article>
  )
}

function McpEditor({ row, onClose, onSaved }: { row: any; onClose: () => void; onSaved: (name: string) => void }) {
  const [initial] = useState(() => ({
    name: row.name ?? '', transport: row.transport ?? 'stdio', command: row.command ?? '',
    args: (row.args ?? []).join(' '), env: row.env ?? {}, url: row.url ?? '', enabled: row.enabled ?? true,
  }))
  const [form, setForm] = useState<any>(initial)
  const [busy, setBusy] = useState(false)
  const dirty = JSON.stringify(form) !== JSON.stringify(initial)
  const missing = [
    !form.name.trim() && '名称',
    form.transport === 'stdio' && !form.command.trim() && '命令',
    form.transport !== 'stdio' && !form.url.trim() && 'URL',
  ].filter(Boolean) as string[]

  const submit = async () => {
    if (missing.length) return
    setBusy(true)
    try {
      const payload = { ...form, args: form.args.split(/\s+/).filter(Boolean) }
      if (row.id) await api.mcp.update(row.id, payload)
      else await api.mcp.create(payload)
      onSaved(form.name)
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal open onClose={onClose} dirty={dirty} title={row.id ? `编辑 MCP 服务「${row.name}」` : '添加 MCP 服务'}
           footer={<>
             <button className="btn" onClick={onClose}>取消</button>
             <button className="btn btn-primary" onClick={() => void submit()} disabled={busy || missing.length > 0}
                     title={missing.length ? `还缺：${missing.join('、')}` : undefined}>
               {busy ? <Spinner size={11} /> : null} 保存
             </button>
           </>}>
      <div className="space-y-3">
        <div className="grid grid-cols-[1fr_200px] gap-3">
          <Field label="名称" required hint="节点里按 mcp:名称/工具名 引用">
            {(p) => (
              <input {...p} className="field" value={form.name} placeholder="filesystem"
                     onChange={(e) => setForm({ ...form, name: e.target.value })} />
            )}
          </Field>
          <Field label="传输方式">
            {(p) => (
              <select {...p} className="field" value={form.transport}
                      onChange={(e) => setForm({ ...form, transport: e.target.value })}>
                <option value="stdio">stdio（本地子进程）</option>
                <option value="http">HTTP（远程）</option>
              </select>
            )}
          </Field>
        </div>
        {form.transport === 'stdio' ? (
          <>
            <Field label="命令" required>
              {(p) => (
                <input {...p} className="field mono text-xs" value={form.command} placeholder="npx"
                       onChange={(e) => setForm({ ...form, command: e.target.value })} />
              )}
            </Field>
            <Field label="参数" hint="空格分隔">
              {(p) => (
                <input {...p} className="field mono text-xs" value={form.args}
                       placeholder="-y @modelcontextprotocol/server-filesystem /tmp"
                       onChange={(e) => setForm({ ...form, args: e.target.value })} />
              )}
            </Field>
            <Field label="环境变量">
              {(p) => <JsonInput id={p.id} value={form.env} onChange={(v) => setForm({ ...form, env: v ?? {} })} rows={3} />}
            </Field>
          </>
        ) : (
          <Field label="URL" required>
            {(p) => (
              <input {...p} className="field mono text-xs" value={form.url} placeholder="https://example.com/mcp"
                     onChange={(e) => setForm({ ...form, url: e.target.value })} />
            )}
          </Field>
        )}
        <label className="flex items-center gap-2 text-xs">
          <input type="checkbox" checked={!!form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
          启用
        </label>
      </div>
    </Modal>
  )
}
