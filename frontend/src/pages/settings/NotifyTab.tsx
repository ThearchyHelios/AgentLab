import { useCallback, useEffect, useState } from 'react'
import { Bell, Plus, Send } from 'lucide-react'
import { api } from '../../api/client'
import {
  DeleteButton, EmptyState, ErrorState, Field, Modal, SectionBar, Skeleton, Spinner, confirmDialog, toast,
} from '../../components/ui'
import { formatDateTime } from '../../lib/format'
import { NOTIFY_TEXT as T } from '../../lib/terms'
import { useAtLeast } from '../../store/auth'
import type { NotifyChannelRow } from '../../typesApp'

/*
 * 设置 → 通知（A4 第 7 条）：待办、动作的事件推送到哪些地址。格式只有 Webhook 的四种（Q6），发送核心和定时任务的
 * 通知共用；消息里只放标题、状态和链接，不放报告里的数字。地址和签名密钥加密保存，之后只显示主机名。
 * 全局渠道只给管理员建；个人渠道每个人都能建（§3.12）
 */

const FORMATS = ['generic', 'feishu', 'wecom', 'dingtalk'] as const
const EVENTS = Object.keys(T.eventLabel)

export function NotifyTab() {
  const [rows, setRows] = useState<NotifyChannelRow[] | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [editing, setEditing] = useState<NotifyChannelRow | 'new' | null>(null)
  const [testing, setTesting] = useState<string | null>(null)
  const load = useCallback(() => {
    api.notify.channels().then(({ items }) => { setRows(items); setError(null) }).catch(setError)
  }, [])
  useEffect(load, [load])

  const test = async (row: NotifyChannelRow) => {
    setTesting(row.id)
    try {
      const res = await api.notify.testChannel(row.id)
      if (res.ok) toast.ok(T.testOk)
      else toast.warn(res.message)
    } catch (e) {
      toast.error(e)
    } finally {
      setTesting(null)
    }
  }
  const remove = async (row: NotifyChannelRow) => {
    if (!(await confirmDialog({ title: T.removeTitle(row.name), body: T.removeBody, danger: true, confirmLabel: '删除' }))) return
    try {
      await api.notify.removeChannel(row.id)
      load()
    } catch (e) {
      toast.error(e)
    }
  }
  const toggle = async (row: NotifyChannelRow) => {
    try {
      await api.notify.updateChannel(row.id, { enabled: !row.enabled })
      load()
    } catch (e) {
      toast.error(e)
    }
  }

  return (
    <div className="space-y-8" data-notify="">
      <section>
        <SectionBar title={T.title} hint={T.hint}>
          <button className="btn btn-primary btn-sm" onClick={() => setEditing('new')} data-notify-create="">
            <Plus size={12} aria-hidden /> {T.create}
          </button>
        </SectionBar>
        {error ? <ErrorState error={error} onRetry={load} /> : !rows ? <Skeleton rows={3} height={44} gap={8} /> : !rows.length ? (
          <EmptyState icon={<Bell size={22} />} title={T.empty} body={T.emptyBody} />
        ) : (
          <div className="space-y-2">
            {rows.map((row) => (
              <div key={row.id} className="flex items-center gap-2 rounded-lg border bg-panel px-3 py-2" data-notify-channel={row.name}>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5 text-xs">
                    <span className="font-medium">{row.name}</span>
                    <span className="chip">{T.formats[row.format] ?? row.format}</span>
                    <span className="chip">{T.scopes[row.scope] ?? row.scope}</span>
                    {row.signed && <span className="chip">{T.signed}</span>}
                    {!row.enabled && <span className="chip text-faint">已停用</span>}
                  </div>
                  <div className="truncate text-2xs text-faint">
                    <span className="mono">{row.url_host}</span>
                    {row.events.length > 0 && ` · ${row.events.map((e) => T.eventLabel[e] ?? e).join('、')}`}
                  </div>
                </div>
                <label className="flex items-center gap-1 text-2xs">
                  <input type="checkbox" className="accent-[var(--accent)]" checked={row.enabled} onChange={() => void toggle(row)} />
                  {T.enabled}
                </label>
                <button className="btn btn-xs" disabled={testing === row.id} onClick={() => void test(row)}>
                  {testing === row.id ? <Spinner size={10} /> : <Send size={10} aria-hidden />} {T.test}
                </button>
                <button className="btn btn-xs btn-ghost" onClick={() => setEditing(row)}>编辑</button>
                <DeleteButton label={`删除渠道 ${row.name}`} onClick={() => void remove(row)} />
              </div>
            ))}
          </div>
        )}
      </section>
      <Deliveries />
      {editing && <ChannelEditor row={editing === 'new' ? null : editing} onClose={() => setEditing(null)}
                                 onSaved={(name) => { setEditing(null); toast.ok(T.saved(name)); load() }} />}
    </div>
  )
}

function Deliveries() {
  const [rows, setRows] = useState<any[] | null>(null)
  useEffect(() => { api.notify.deliveries({ limit: 50 }).then(({ items }) => setRows(items)).catch(() => setRows([])) }, [])
  return (
    <section data-notify-deliveries="">
      <SectionBar title={T.deliveries} />
      {!rows ? <Skeleton rows={2} height={28} /> : !rows.length ? <p className="text-2xs text-faint">{T.deliveriesEmpty}</p> : (
        <div className="divide-y rounded-lg border text-2xs">
          {rows.map((d) => (
            <div key={d.id} className="flex items-center gap-2 px-3 py-1.5">
              <span className="w-28 shrink-0 text-dim">{T.eventLabel[d.event] ?? d.event}</span>
              <span className="min-w-0 flex-1 truncate text-faint">{d.error ?? (d.http_status ? `HTTP ${d.http_status}` : '')}</span>
              <span className="shrink-0" style={{ color: d.status === 'ok' ? 'var(--ok)' : d.status === 'failed' ? 'var(--st-failed)' : undefined }}>
                {d.status === 'ok' ? '已送达' : d.status === 'failed' ? '失败' : d.status}
              </span>
              <time className="tnum shrink-0 text-faint">{formatDateTime(d.created_at ?? d.at)}</time>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

function ChannelEditor({ row, onClose, onSaved }: {
  row: NotifyChannelRow | null; onClose: () => void; onSaved: (name: string) => void
}) {
  const admin = useAtLeast('admin')
  const [form, setForm] = useState({
    name: row?.name ?? '', scope: row?.scope ?? (admin ? 'global' : 'user'), format: row?.format ?? 'generic',
    url: '', secret: '', events: row?.events ?? ['task.opened', 'action.unknown'],
  })
  const [busy, setBusy] = useState(false)
  const save = async () => {
    setBusy(true)
    try {
      if (row) {
        await api.notify.updateChannel(row.id, {
          name: form.name.trim(), scope: form.scope, format: form.format, events: form.events,
          // 地址、密钥只回主机名：不填就是保持原值
          ...(form.url.trim() ? { url: form.url.trim() } : {}), ...(form.secret ? { secret: form.secret } : {}),
        })
      } else {
        await api.notify.createChannel({ name: form.name.trim(), scope: form.scope, format: form.format, url: form.url.trim(),
                                         secret: form.secret || null, events: form.events })
      }
      onSaved(form.name.trim())
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }
  return (
    <Modal open onClose={onClose} title={row ? `${T.title}：${row.name}` : T.create} width={560} dirty
           footer={<>
             <button className="btn" onClick={onClose}>取消</button>
             <button className="btn btn-primary" disabled={busy || !form.name.trim() || (!row && !form.url.trim())}
                     onClick={() => void save()} data-notify-save="">
               {busy ? <Spinner size={11} /> : null} 保存
             </button>
           </>}>
      <div className="space-y-3">
        <div className="grid grid-cols-3 gap-3">
          <Field label={T.name} required className="col-span-1">
            {(p) => <input {...p} className="field" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />}
          </Field>
          <Field label={T.format}>
            {(p) => (
              <select {...p} className="field" value={form.format}
                      onChange={(e) => setForm({ ...form, format: e.target.value as typeof form.format })}>
                {FORMATS.map((f) => <option key={f} value={f}>{T.formats[f]}</option>)}
              </select>
            )}
          </Field>
          <Field label={T.scope}>
            {(p) => (
              <select {...p} className="field" value={form.scope} onChange={(e) => setForm({ ...form, scope: e.target.value as typeof form.scope })}>
                {(['global', 'app', 'user'] as const).map((k) => (
                  <option key={k} value={k} disabled={k === 'global' && !admin}>{T.scopes[k]}</option>
                ))}
              </select>
            )}
          </Field>
        </div>
        <Field label={T.url} hint={row ? `${T.urlHint}；当前：${row.url_host}` : T.urlHint} required={!row}>
          {(p) => <input {...p} className="field mono text-xs" value={form.url} placeholder="https://"
                         onChange={(e) => setForm({ ...form, url: e.target.value })} />}
        </Field>
        <Field label={T.secret}>
          {(p) => <input {...p} className="field mono text-xs" type="password" autoComplete="off" value={form.secret}
                         onChange={(e) => setForm({ ...form, secret: e.target.value })} />}
        </Field>
        <fieldset>
          <legend className="label">{T.events}</legend>
          <div className="grid grid-cols-3 gap-1.5">
            {EVENTS.map((ev) => (
              <label key={ev} className="flex items-center gap-1.5 text-xs">
                <input type="checkbox" className="accent-[var(--accent)]" checked={form.events.includes(ev)}
                       onChange={(e) => setForm({ ...form, events: e.target.checked ? [...form.events, ev] : form.events.filter((x) => x !== ev) })} />
                {T.eventLabel[ev]}
              </label>
            ))}
          </div>
        </fieldset>
      </div>
    </Modal>
  )
}
