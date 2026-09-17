import { useEffect, useState } from 'react'
import { Database, Plug, RefreshCw, Table2, Trash2 } from 'lucide-react'
import { api } from '../api/client'
import { Empty, Modal, Spinner, useToast } from '../components/ui'

/**
 * 数据源管理。
 *
 * 这里的每一项配置都会影响两件事：Copilot 编排时看得见什么，以及 agent 运行时
 * 能查什么。所以表单上的说明写的是"这项填错会怎样"，而不是字段的字面意思——
 * Oracle 少填一个 schema，探查结果就是空的，而报错信息只会说 ORA-00942。
 */
export function DataSourcesTab() {
  const [rows, setRows] = useState<any[]>([])
  const [kinds, setKinds] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [editing, setEditing] = useState<any | null>(null)
  const toast = useToast()

  const load = async () => {
    setLoading(true)
    try {
      const [list, meta] = await Promise.all([api.datasources.list(), api.datasources.kinds()])
      setRows(list)
      setKinds(meta.kinds ?? [])
    } catch (e: any) {
      toast(e.message ?? '加载失败', 'error')
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => { void load() }, [])

  if (loading) return <div className="flex justify-center py-8"><Spinner size={16} /></div>

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <div className="text-[11.5px] text-dim">
          接入后 Copilot 编排时就看得见这些库的结构，agent 运行时能直接查
        </div>
        <span className="flex-1" />
        <button className="btn btn-sm btn-primary"
                onClick={() => setEditing({ kind: 'mysql', readonly: true, options: {} })}>
          <Database size={12} /> 添加数据源
        </button>
      </div>

      {!rows.length ? (
        <Empty
          icon={<Database size={22} />}
          title="还没有数据源"
          hint="接一个数据库，就能在「问数据」里直接提问，不用自己写 SQL"
        />
      ) : (
        <div className="space-y-2">
          {rows.map((row) => (
            <SourceCard key={row.id} row={row} onChanged={load} onEdit={() => setEditing(row)} />
          ))}
        </div>
      )}

      {editing && (
        <SourceEditor
          source={editing}
          kinds={kinds}
          onClose={() => setEditing(null)}
          onSaved={async () => { setEditing(null); await load() }}
        />
      )}
    </div>
  )
}

function SourceCard({ row, onChanged, onEdit }: {
  row: any; onChanged: () => Promise<void>; onEdit: () => void
}) {
  const toast = useToast()
  const [busy, setBusy] = useState('')
  const [tables, setTables] = useState<string[] | null>(null)

  const test = async () => {
    setBusy('test')
    try {
      const r = await api.datasources.test(row.id)
      // 失败时把驱动的原始报错给出来——这类问题九成靠错误信息定位
      toast(r.ok ? `连接正常（${r.elapsed_ms}ms）` : `连不上：${r.error}`, r.ok ? 'ok' : 'error')
    } catch (e: any) {
      toast(e.message ?? '测试失败', 'error')
    } finally { setBusy('') }
  }

  const introspect = async () => {
    setBusy('introspect')
    try {
      const r = await api.datasources.introspect(row.id)
      toast(`探到 ${r.table_count} 个对象`, r.table_count ? 'ok' : 'error')
      if (!r.table_count) {
        // 大概率是 schema 没配对：只读账号名下往往什么都没有
        const s = await api.datasources.schema(row.id)
        toast(s.summary?.includes('schema') ? s.summary.slice(0, 120) : '没探到对象，检查 schema 配置', 'error')
      }
      await onChanged()
    } catch (e: any) {
      toast(e.message ?? '探查失败', 'error')
    } finally { setBusy('') }
  }

  const peek = async () => {
    if (tables) { setTables(null); return }
    try {
      const s = await api.datasources.schema(row.id)
      setTables(s.tables ?? [])
    } catch (e: any) { toast(e.message ?? '读取失败', 'error') }
  }

  return (
    <div className="rounded-lg border bg-panel p-3">
      <div className="flex items-center gap-2">
        <Database size={13} style={{ color: row.enabled ? 'var(--accent)' : 'var(--text-faint)' }} />
        <span className="text-[12.5px] font-medium">{row.name}</span>
        <span className="chip">{row.kind}</span>
        {row.readonly
          ? <span className="chip" style={{ color: 'var(--ok)', borderColor: 'color-mix(in srgb, var(--ok) 40%, transparent)' }}>只读</span>
          : <span className="chip" style={{ color: 'var(--warn)', borderColor: 'var(--warn)' }}>可写</span>}
        {!row.enabled && <span className="chip opacity-60">已停用</span>}
        <span className="flex-1" />
        <button className="btn btn-sm" disabled={!!busy} onClick={test}>
          {busy === 'test' ? <Spinner size={11} /> : <Plug size={11} />} 测连接
        </button>
        <button className="btn btn-sm" disabled={!!busy} onClick={introspect} title="读取表结构并缓存，Copilot 靠它写 SQL">
          {busy === 'introspect' ? <Spinner size={11} /> : <RefreshCw size={11} />} 探查结构
        </button>
        <button className="btn btn-sm btn-ghost" onClick={onEdit}>编辑</button>
        <button className="btn btn-sm btn-ghost" onClick={async () => {
          if (!confirm(`删除数据源「${row.name}」？用到它的工作流会失效。`)) return
          await api.datasources.remove(row.id)
          await onChanged()
        }}>
          <Trash2 size={11} className="text-[var(--err)]" />
        </button>
      </div>

      {row.description && (
        <div className="mt-1 text-[11px] text-dim">{row.description}</div>
      )}

      <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[10.5px] text-faint">
        <span className="mono">
          {row.kind === 'sqlite' ? row.database : `${row.username ?? ''}@${row.host ?? ''}:${row.port ?? ''}`}
        </span>
        {row.options?.schema && <span className="chip">schema {row.options.schema}</span>}
        {row.table_count ? (
          <button className="chip hover:bg-hover" onClick={peek}>
            <Table2 size={9} className="mr-1 inline" />{row.table_count} 个对象
          </button>
        ) : (
          <span className="chip" style={{ color: 'var(--warn)' }}>结构未探查</span>
        )}
        {row.tools?.length ? (
          <span className="mono opacity-70">{row.tools.join(' · ')}</span>
        ) : null}
      </div>

      {tables && (
        <div className="mono mt-2 max-h-40 overflow-y-auto rounded bg-bg px-2 py-1.5 text-[10px] leading-relaxed text-dim">
          {tables.length ? tables.join('\n') : '（空）'}
        </div>
      )}
    </div>
  )
}

function SourceEditor({ source, kinds, onClose, onSaved }: {
  source: any; kinds: any[]; onClose: () => void; onSaved: () => Promise<void>
}) {
  const isNew = !source.id
  const [form, setForm] = useState<any>({
    name: '', kind: 'mysql', host: '', port: null, database: '',
    username: '', password: '', description: '', readonly: true, enabled: true,
    ...source,
    options: { ...(source.options ?? {}) },
  })
  const [saving, setSaving] = useState(false)
  const toast = useToast()
  const meta = kinds.find((k) => k.value === form.kind)
  const set = (patch: any) => setForm((f: any) => ({ ...f, ...patch }))
  const setOption = (key: string, value: string) =>
    setForm((f: any) => {
      const options = { ...f.options }
      if (value) options[key] = value
      else delete options[key]
      return { ...f, options }
    })

  const save = async () => {
    setSaving(true)
    try {
      const body: any = { ...form }
      // 不传 password 表示不动；只有真填了才提交
      if (!body.password) delete body.password
      if (isNew) await api.datasources.create(body)
      else {
        delete body.name   // 名字进了工具名，改名等于换一个工具，不允许
        delete body.id
        await api.datasources.update(source.id, body)
      }
      await onSaved()
      toast(isNew ? '已添加，记得点「探查结构」' : '已保存', 'ok')
    } catch (e: any) {
      toast(e.message ?? '保存失败', 'error')
    } finally { setSaving(false) }
  }

  return (
    <Modal
      open
      onClose={onClose}
      title={isNew ? '添加数据源' : `编辑「${source.name}」`}
      footer={
        <>
          <button className="btn" onClick={onClose}>取消</button>
          <button className="btn btn-primary" disabled={saving || !form.name} onClick={save}>
            {saving ? <Spinner size={11} /> : null} 保存
          </button>
        </>
      }
    >
      <div className="space-y-2.5">
        <div className="grid grid-cols-2 gap-2.5">
          <div>
            <label className="label">标识<span className="ml-1 text-[var(--err)]">*</span></label>
            <input className="field mono" value={form.name} disabled={!isNew}
                   placeholder="sales"
                   onChange={(e) => set({ name: e.target.value })} />
            <div className="mt-0.5 text-[10px] text-faint">
              小写英文，会成为工具名 db_query__{form.name || 'xxx'}；建好不能改
            </div>
          </div>
          <div>
            <label className="label">类型</label>
            <select className="field" value={form.kind}
                    onChange={(e) => {
                      const k = kinds.find((x) => x.value === e.target.value)
                      set({ kind: e.target.value, port: k?.default_port ?? null })
                    }}>
              {kinds.map((k) => <option key={k.value} value={k.value}>{k.label}</option>)}
            </select>
          </div>
        </div>

        {meta?.hint && (
          <div className="rounded border px-2 py-1.5 text-[10.5px] leading-relaxed"
               style={{ borderColor: 'color-mix(in srgb, var(--accent) 35%, transparent)', color: 'var(--text-dim)' }}>
            {meta.hint}
          </div>
        )}

        {form.kind === 'sqlite' ? (
          <div>
            <label className="label">数据库文件路径</label>
            <input className="field mono" value={form.database ?? ''}
                   placeholder="/绝对/路径/data.db"
                   onChange={(e) => set({ database: e.target.value })} />
          </div>
        ) : (
          <>
            <div className="grid grid-cols-[1fr_100px] gap-2.5">
              <div>
                <label className="label">主机</label>
                <input className="field mono" value={form.host ?? ''}
                       onChange={(e) => set({ host: e.target.value })} />
              </div>
              <div>
                <label className="label">端口</label>
                <input className="field mono" type="number" value={form.port ?? ''}
                       placeholder={String(meta?.default_port ?? '')}
                       onChange={(e) => set({ port: e.target.value ? Number(e.target.value) : null })} />
              </div>
            </div>
            <div className="grid grid-cols-2 gap-2.5">
              <div>
                <label className="label">
                  {form.kind === 'oracle' ? 'service_name' : '数据库'}
                </label>
                <input className="field mono" value={form.database ?? ''}
                       onChange={(e) => set({ database: e.target.value })} />
              </div>
              <div>
                <label className="label">schema</label>
                <input className="field mono" value={form.options?.schema ?? ''}
                       placeholder={form.kind === 'oracle' ? '如 ANALYTICS' : '留空用默认'}
                       onChange={(e) => setOption('schema', e.target.value)} />
                <div className="mt-0.5 text-[10px] text-faint">
                  只读账号名下常常没有对象，数据在别的 schema 里
                </div>
              </div>
            </div>
            <div className="grid grid-cols-2 gap-2.5">
              <div>
                <label className="label">用户名</label>
                <input className="field mono" value={form.username ?? ''}
                       onChange={(e) => set({ username: e.target.value })} />
              </div>
              <div>
                <label className="label">密码</label>
                <input className="field mono" type="password"
                       placeholder={source.has_password ? '已保存，留空则不改' : ''}
                       value={form.password ?? ''}
                       onChange={(e) => set({ password: e.target.value })} />
              </div>
            </div>
          </>
        )}

        <div>
          <label className="label">说明</label>
          <input className="field" value={form.description ?? ''}
                 placeholder="销售库：订单、客户、产品"
                 onChange={(e) => set({ description: e.target.value })} />
          <div className="mt-0.5 text-[10px] text-faint">
            这句话会给 Copilot 看，它据此判断该查哪个库——写清楚里面有什么
          </div>
        </div>

        <label className="flex cursor-pointer items-start gap-2 rounded border p-2"
               style={{ borderColor: form.readonly ? undefined : 'var(--warn)' }}>
          <input type="checkbox" className="mt-0.5" checked={!!form.readonly}
                 onChange={(e) => set({ readonly: e.target.checked })} />
          <span className="text-[11.5px]">
            只读
            <span className="ml-1.5 text-[10.5px] text-faint">
              强烈建议保持勾选。这里的 SQL 由模型生成，关掉之后 UPDATE / DELETE
              会真的执行（DROP / TRUNCATE 任何情况下都不允许）
            </span>
          </span>
        </label>

        <label className="flex cursor-pointer items-center gap-2 text-[11.5px]">
          <input type="checkbox" checked={!!form.enabled}
                 onChange={(e) => set({ enabled: e.target.checked })} />
          启用（停用后 Copilot 和 agent 都看不到它）
        </label>
      </div>
    </Modal>
  )
}
