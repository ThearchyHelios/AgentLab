import { useCallback, useEffect, useState } from 'react'
import { Bell, Check, Mail, Plus, Send } from 'lucide-react'
import { api } from '../../api/client'
import {
  DeleteButton, EmptyState, ErrorState, Field, Modal, SectionBar, Skeleton, Spinner, confirmDialog, toast,
} from '../../components/ui'
import { formatDateTime } from '../../lib/format'
import { useLeaveGuard } from '../../lib/leave'
import { NOTIFY_TEXT as T, SETTINGS_ROLE_TEXT } from '../../lib/terms'
import { MAIL_TEXT as M, NOTIFY_PERSONAL_EVENT_LABEL } from '../../lib/termsApp'
import { useAtLeast } from '../../store/auth'
import type { NotifyChannelRow, NotifyEventRow, NotifyMailState } from '../../typesApp'

/*
 * 设置 → 通知（A4 第 7 条）：待办、动作的事件推送到哪些地址。格式是 Webhook 的四种加上邮件（04 细案 B2，改写了 Q6）：
 * Webhook 的发送核心和定时任务的通知共用；邮件走管理员在「邮件服务」一节配的 SMTP。消息里只放标题、状态和链接，不放报告
 * 里的数字。地址和签名密钥加密保存，之后只显示主机名。
 * 全局渠道只给管理员建；个人渠道每个人都能建（§3.12）。开了登录的使用者只看、只建自己的个人渠道（scope=user），
 * 投递记录只给搭建者以上。
 *
 * 邮件：邮件服务没启用时不列「邮件」这一格式（建了也发不出去）；个人邮件渠道不填地址，只发到本人的账号邮箱（列表里写
 * 「发到账号邮箱 …」，没有邮箱照实写发不出去）；全局、应用的填固定收件地址。投递成功写「已交给邮件服务器」，不写已送达：
 * 退信不追踪。「邮件服务」「每天的待办汇总」两节给搭建者以上看（设置接口搭建者只读），只有管理员能改。
 */

const WEBHOOK_FORMATS = ['generic', 'feishu', 'wecom', 'dingtalk'] as const
const EVENTS = Object.keys(T.eventLabel)
/** 事件在这个范围的渠道上怎么叫：个人渠道的实例结束只推自己发起的，叫「我发起的实例完成 / 失败」 */
const eventLabel = (event: string, scope: string): string =>
  (scope === 'user' ? NOTIFY_PERSONAL_EVENT_LABEL[event] : undefined) ?? T.eventLabel[event] ?? event

/** 一个渠道发到哪：Webhook 只有主机名；邮件写收件人（个人渠道是账号邮箱） */
function targetOf(row: NotifyChannelRow): string {
  if (row.format !== 'email') return row.url_host
  if (row.scope === 'user') return M.personalTo(row.mail_to?.[0] ?? null)
  return M.to(row.mail_to ?? [])
}

export function NotifyTab() {
  const builder = useAtLeast('builder')
  const [rows, setRows] = useState<NotifyChannelRow[] | null>(null)
  const [mail, setMail] = useState<NotifyMailState>({ enabled: false, account_email: null })
  const [error, setError] = useState<unknown>(null)
  const [editing, setEditing] = useState<NotifyChannelRow | 'new' | null>(null)
  const [testing, setTesting] = useState<string | null>(null)
  const load = useCallback(() => {
    api.notify.channels()
      .then(({ items, mail: state }) => {
        setRows(builder ? items : items.filter((c) => c.scope === 'user'))
        setMail(state ?? { enabled: false, account_email: null })
        setError(null)
      })
      .catch(setError)
  }, [builder])
  useEffect(load, [load])

  const test = async (row: NotifyChannelRow) => {
    setTesting(row.id)
    try {
      const res = await api.notify.testChannel(row.id)
      // 邮件渠道成功时服务端说的是「已交给邮件服务器……」：照原话写，不写「已发送」
      if (res.ok) toast.ok(row.format === 'email' ? res.message : T.testOk)
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
        <SectionBar title={T.title} hint={builder ? T.hint : `${T.hint}。${SETTINGS_ROLE_TEXT.notifyPersonal}`}>
          <button className="btn btn-primary btn-sm" onClick={() => setEditing('new')} data-notify-create="">
            <Plus size={12} aria-hidden /> {T.create}
          </button>
        </SectionBar>
        {error ? <ErrorState error={error} onRetry={load} /> : !rows ? <Skeleton rows={3} height={44} gap={8} /> : !rows.length ? (
          <EmptyState icon={<Bell size={22} />} title={T.empty} body={T.emptyBody} />
        ) : (
          <div className="space-y-2">
            {rows.map((row) => (
              <div key={row.id} className="flex items-center gap-2 rounded-lg border bg-panel px-3 py-2" data-notify-channel={row.name}
                   data-notify-format={row.format}>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5 text-xs">
                    <span className="font-medium">{row.name}</span>
                    <span className="chip">{T.formats[row.format] ?? row.format}</span>
                    <span className="chip">{T.scopes[row.scope] ?? row.scope}</span>
                    {row.signed && <span className="chip">{T.signed}</span>}
                    {!row.enabled && <span className="chip text-faint">已停用</span>}
                  </div>
                  <div className="truncate text-2xs text-faint">
                    <span className={row.format === 'email' ? undefined : 'mono'} data-notify-target=""
                          style={row.format === 'email' && row.scope === 'user' && !row.mail_to?.length ? { color: 'var(--warn)' } : undefined}>
                      {targetOf(row)}
                    </span>
                    {row.events.length > 0 && ` · ${row.events.map((e) => eventLabel(e, row.scope)).join('、')}`}
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
      {builder && <Deliveries />}
      {builder && <MailSection onSaved={load} />}
      {builder && <DigestSection />}
      {editing && <ChannelEditor row={editing === 'new' ? null : editing} mail={mail} onClose={() => setEditing(null)}
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
            <div key={d.id} className="flex items-center gap-2 px-3 py-1.5" data-delivery-format={d.format ?? ''}>
              <span className="w-28 shrink-0 text-dim">{T.deliveryEventLabel[d.event] ?? T.eventLabel[d.event] ?? d.event}</span>
              <span className="min-w-0 flex-1 truncate text-faint">{d.error ?? (d.http_status ? `HTTP ${d.http_status}` : '')}</span>
              {/* 邮件只知道交给了邮件服务器，进没进收件箱不知道（退信不追踪）：不写「已送达」 */}
              <span className="shrink-0" title={d.format === 'email' && d.status === 'ok' ? M.handoffHint : undefined}
                    style={{ color: d.status === 'ok' ? 'var(--ok)' : d.status === 'failed' ? 'var(--st-failed)' : undefined }}>
                {d.status === 'ok' ? (d.format === 'email' ? M.handoff : '已送达') : d.status === 'failed' ? '失败' : d.status}
              </span>
              <time className="tnum shrink-0 text-faint">{formatDateTime(d.created_at ?? d.at)}</time>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

function ChannelEditor({ row, mail, onClose, onSaved }: {
  row: NotifyChannelRow | null; mail: NotifyMailState; onClose: () => void; onSaved: (name: string) => void
}) {
  const admin = useAtLeast('admin')
  const builder = useAtLeast('builder')
  const [form, setForm] = useState({
    name: row?.name ?? '', scope: row?.scope ?? (admin ? 'global' : 'user'), format: row?.format ?? 'generic',
    url: '', secret: '', events: row?.events ?? ['task.opened', 'action.unknown'],
    // 全局、应用的邮件渠道：收件地址整份重填（服务端照原样给，编辑时带出来）
    recipients: row?.format === 'email' && row.scope !== 'user' ? (row.mail_to ?? []).join('\n') : '',
  })
  const [busy, setBusy] = useState(false)
  // 列哪些事件由服务端说（notify/service.EVENTS 的 scopes）：这种范围的渠道收得到的才列，登记了还没推送的不列（勾了收不到）。
  // 已经订阅着的、刚勾上的照样列出来，看得到、能取消
  const [registry, setRegistry] = useState<NotifyEventRow[] | null>(null)
  const [registryFailed, setRegistryFailed] = useState(false)
  useEffect(() => { api.notify.events().then(setRegistry).catch(() => setRegistryFailed(true)) }, [])
  const offered = EVENTS.filter((ev) => form.events.includes(ev) || row?.events.includes(ev)
    || registry?.some((r) => r.event === ev && r.scopes.includes(form.scope)))
  const email = form.format === 'email'
  // 邮件服务没启用：不列「邮件」（建了也发不出去）；原来就是邮件渠道的照列，能改名、改订阅、换回 Webhook
  const formats = [...WEBHOOK_FORMATS, ...(mail.enabled || row?.format === 'email' ? ['email' as const] : [])]
  const personalMail = email && form.scope === 'user'
  const recipientsChanged = form.recipients.trim() !== (row?.format === 'email' ? (row.mail_to ?? []).join('\n') : '')
  const missingTarget = email ? !personalMail && !form.recipients.trim()
    : !row || row.format === 'email' ? !form.url.trim() : false
  const save = async () => {
    setBusy(true)
    try {
      const target = email
        ? (personalMail ? {} : recipientsChanged || row?.format !== 'email' ? { url: form.recipients.trim() } : {})
        : form.url.trim() ? { url: form.url.trim() } : {}
      if (row) {
        await api.notify.updateChannel(row.id, {
          name: form.name.trim(), scope: form.scope, format: form.format, events: form.events,
          // 地址、密钥只回主机名：不填就是保持原值
          ...target, ...(!email && form.secret ? { secret: form.secret } : {}),
        })
      } else {
        await api.notify.createChannel({ name: form.name.trim(), scope: form.scope, format: form.format, ...target,
                                         secret: !email && form.secret ? form.secret : null, events: form.events })
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
             <button className="btn btn-primary" disabled={busy || !form.name.trim() || missingTarget}
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
              <select {...p} className="field" value={form.format} data-notify-format-select=""
                      onChange={(e) => setForm({ ...form, format: e.target.value as typeof form.format })}>
                {formats.map((f) => <option key={f} value={f}>{T.formats[f]}</option>)}
              </select>
            )}
          </Field>
          <Field label={T.scope}>
            {(p) => (
              <select {...p} className="field" value={form.scope} onChange={(e) => setForm({ ...form, scope: e.target.value as typeof form.scope })}>
                {(['global', 'app', 'user'] as const).filter((k) => builder || k === 'user').map((k) => (
                  <option key={k} value={k} disabled={k === 'global' && !admin}>{T.scopes[k]}</option>
                ))}
              </select>
            )}
          </Field>
        </div>
        {!mail.enabled && !email && <p className="text-2xs text-faint" data-notify-mail-off="">{M.off}</p>}
        {personalMail ? (
          <div className="rounded-md border bg-elev px-2.5 py-2 text-xs" data-notify-mail-to="account">
            <div className="flex items-center gap-1.5" style={mail.account_email ? undefined : { color: 'var(--warn)' }}>
              <Mail size={12} aria-hidden /> {M.personalTo(mail.account_email)}
            </div>
            <p className="mt-0.5 text-2xs text-faint">{M.personalToHint}</p>
          </div>
        ) : email ? (
          <Field label={M.recipients} hint={M.recipientsHint} required>
            {(p) => <textarea {...p} className="field mono min-h-[72px] text-xs" value={form.recipients} data-notify-recipients=""
                              placeholder="ops@example.com" onChange={(e) => setForm({ ...form, recipients: e.target.value })} />}
          </Field>
        ) : (
          <>
            <Field label={T.url} hint={row && row.format !== 'email' ? `${T.urlHint}；当前：${row.url_host}` : T.urlHint}
                   required={!row || row.format === 'email'}>
              {(p) => <input {...p} className="field mono text-xs" value={form.url} placeholder="https://"
                             onChange={(e) => setForm({ ...form, url: e.target.value })} />}
            </Field>
            <Field label={T.secret}>
              {(p) => <input {...p} className="field mono text-xs" type="password" autoComplete="off" value={form.secret}
                             onChange={(e) => setForm({ ...form, secret: e.target.value })} />}
            </Field>
          </>
        )}
        <fieldset>
          <legend className="label">{T.events}</legend>
          <div className="grid grid-cols-3 gap-1.5" data-notify-events={registry ? form.scope : ''}>
            {offered.map((ev) => (
              <label key={ev} className="flex items-center gap-1.5 text-xs" data-notify-event={ev}>
                <input type="checkbox" className="accent-[var(--accent)]" checked={form.events.includes(ev)}
                       onChange={(e) => setForm({ ...form, events: e.target.checked ? [...form.events, ev] : form.events.filter((x) => x !== ev) })} />
                {eventLabel(ev, form.scope)}
              </label>
            ))}
          </div>
          {registryFailed ? <p className="mt-1 text-2xs" style={{ color: 'var(--st-failed)' }} role="alert">{T.eventsError}</p>
            : !registry && <p className="mt-1 flex items-center gap-1 text-2xs text-faint"><Spinner size={10} /> {T.eventsLoading}</p>}
        </fieldset>
      </div>
    </Modal>
  )
}

// -------------------------------------------------------------------------
// 邮件服务（设置的 mail 一组）、每天的待办汇总（notify 一组的 digest_at）
// -------------------------------------------------------------------------

interface MailSettings {
  enabled: boolean; host: string | null; port: number; security: string; username: string | null
  password: null; password_set: boolean; from_addr: string | null; from_name: string; default_for_assignees: boolean
}
interface MailDraft {
  enabled: boolean; host: string; port: string; security: string; username: string; password: string
  clearPassword: boolean; from_addr: string; from_name: string; default_for_assignees: boolean
}

const draftOf = (m: MailSettings): MailDraft => ({
  enabled: m.enabled, host: m.host ?? '', port: String(m.port ?? 587), security: m.security ?? 'starttls',
  username: m.username ?? '', password: '', clearPassword: false, from_addr: m.from_addr ?? '',
  from_name: m.from_name ?? 'AgentLab', default_for_assignees: m.default_for_assignees,
})

/**
 * 「邮件服务」一节：管理员改，搭建者只看。口令不回，只说设没设；留空表示不修改，勾「清除」才去掉。保存一整组（服务端
 * 补齐没给的）；测试邮件按已保存的设置发到管理员自己的账号邮箱，有没保存的改动时先保存
 */
function MailSection({ onSaved }: { onSaved: () => void }) {
  const admin = useAtLeast('admin')
  const [saved, setSaved] = useState<MailSettings | null>(null)
  const [loadError, setLoadError] = useState<unknown>(null)
  const [draft, setDraft] = useState<MailDraft | null>(null)
  const [busy, setBusy] = useState(false)
  const [testing, setTesting] = useState(false)
  const load = () => api.settings.get().then((s) => { setSaved(s.mail ?? null); setLoadError(null) }, setLoadError)
  useEffect(() => { void load() }, [])
  const base = saved ? draftOf(saved) : null
  const cur = draft ?? base
  const dirty = !!cur && !!base && JSON.stringify(cur) !== JSON.stringify(base)
  useLeaveGuard(dirty, () => confirmDialog({
    title: M.section, body: M.leaveBody, confirmLabel: '放弃修改', cancelLabel: '返回保存', danger: true,
  }))
  const portBad = !!cur && !(/^\d+$/.test(cur.port.trim()) && Number(cur.port) >= 1 && Number(cur.port) <= 65535)
  const edit = (patch: Partial<MailDraft>) => cur && setDraft({ ...cur, ...patch })

  const save = async () => {
    if (!cur || portBad || busy) return
    setBusy(true)
    try {
      const out = await api.settings.put({ mail: {
        enabled: cur.enabled, host: cur.host.trim() || null, port: Number(cur.port), security: cur.security,
        username: cur.username.trim() || null, from_addr: cur.from_addr.trim() || null, from_name: cur.from_name.trim() || null,
        default_for_assignees: cur.default_for_assignees,
        ...(cur.clearPassword ? { password: '' } : cur.password ? { password: cur.password } : {}),
      } })
      setSaved(out.mail)
      setDraft(null)
      toast.ok(M.saved)
      onSaved()
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }
  const test = async () => {
    setTesting(true)
    try {
      const res = await api.settings.mailTest()
      if (res.ok) toast.ok(res.message)
      else toast.warn(res.message)
    } catch (e) {
      toast.error(e)
    } finally {
      setTesting(false)
    }
  }

  return (
    <section data-mail-section={admin ? 'edit' : 'readonly'}>
      <SectionBar title={M.section} hint={admin ? M.hint : `${M.hint}。${M.readOnly}`} />
      {!cur ? (loadError ? <ErrorState error={loadError} onRetry={() => void load()} /> : <Skeleton rows={3} height={14} />) : (
        <fieldset disabled={!admin} className="space-y-3">
          <label className="flex items-start gap-2 text-xs">
            <input type="checkbox" className="mt-0.5" checked={cur.enabled} data-mail-field="enabled"
                   onChange={(e) => edit({ enabled: e.target.checked })} />
            <span>{M.enabled}<span className="mt-0.5 block text-2xs text-faint">{M.enabledHint}</span></span>
          </label>
          <div className="grid grid-cols-3 gap-3">
            <Field label={M.host} hint={M.hostHint} className="col-span-2">
              {(p) => <input {...p} className="field mono text-xs" value={cur.host} placeholder="smtp.example.com"
                             data-mail-field="host" onChange={(e) => edit({ host: e.target.value })} />}
            </Field>
            <Field label={M.port} error={portBad ? M.invalidPort : undefined}>
              {(p) => <input {...p} className="field tnum text-xs" inputMode="numeric" value={cur.port} data-mail-field="port"
                             onChange={(e) => edit({ port: e.target.value })} />}
            </Field>
          </div>
          <Field label={M.security}>
            {(p) => (
              <select {...p} className="field max-w-72 text-xs" value={cur.security} data-mail-field="security"
                      onChange={(e) => edit({ security: e.target.value })}>
                {Object.entries(M.securityLabel).map(([k, label]) => <option key={k} value={k}>{label}</option>)}
              </select>
            )}
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label={M.username} hint={M.usernameHint}>
              {(p) => <input {...p} className="field mono text-xs" value={cur.username} autoComplete="off" data-mail-field="username"
                             onChange={(e) => edit({ username: e.target.value })} />}
            </Field>
            <Field label={M.password} hint={saved?.password_set ? M.passwordSet : M.passwordUnset}>
              {(p) => <input {...p} className="field mono text-xs" type="password" autoComplete="new-password" value={cur.password}
                             data-mail-field="password" disabled={cur.clearPassword}
                             onChange={(e) => edit({ password: e.target.value })} />}
            </Field>
          </div>
          {saved?.password_set && (
            <label className="flex items-center gap-1.5 text-2xs text-dim">
              <input type="checkbox" checked={cur.clearPassword} data-mail-field="clear-password"
                     onChange={(e) => edit({ clearPassword: e.target.checked, password: '' })} /> {M.passwordClear}
            </label>
          )}
          <div className="grid grid-cols-2 gap-3">
            <Field label={M.fromAddr}>
              {(p) => <input {...p} className="field mono text-xs" value={cur.from_addr} placeholder="agentlab@example.com"
                             data-mail-field="from_addr" onChange={(e) => edit({ from_addr: e.target.value })} />}
            </Field>
            <Field label={M.fromName}>
              {(p) => <input {...p} className="field text-xs" value={cur.from_name} data-mail-field="from_name"
                             onChange={(e) => edit({ from_name: e.target.value })} />}
            </Field>
          </div>
          <label className="flex items-start gap-2 text-xs">
            <input type="checkbox" className="mt-0.5" checked={cur.default_for_assignees} data-mail-field="default_for_assignees"
                   onChange={(e) => edit({ default_for_assignees: e.target.checked })} />
            <span>{M.defaultAssignees}<span className="mt-0.5 block text-2xs leading-relaxed text-faint">{M.defaultAssigneesHint}</span></span>
          </label>
          {admin && (
            <div className="flex flex-wrap items-center gap-2">
              <button type="button" className="btn btn-sm btn-primary" disabled={!dirty || portBad || busy} onClick={() => void save()}
                      data-mail-save="">
                {busy ? <Spinner size={11} /> : <Check size={12} aria-hidden />} {M.save}
              </button>
              <button type="button" className="btn btn-sm" disabled={dirty || testing} onClick={() => void test()}
                      title={dirty ? M.testDirty : M.testHint} data-mail-test="">
                {testing ? <Spinner size={11} /> : <Send size={11} aria-hidden />} {M.test}
              </button>
              <span className="text-2xs text-faint">{dirty ? M.testDirty : M.testHint}</span>
            </div>
          )}
          <p className="text-2xs leading-relaxed text-faint" data-mail-handoff="">{M.handoffHint}</p>
        </fieldset>
      )}
    </section>
  )
}

/** 「每天的待办汇总」：几点发（服务端时区）。只改 notify 一组的这一项，服务端补上对外访问地址 */
function DigestSection() {
  const admin = useAtLeast('admin')
  const [saved, setSaved] = useState<string | null>(null)
  const [draft, setDraft] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  useEffect(() => { api.settings.get().then((s) => setSaved(s.notify?.digest_at ?? '09:00'), () => setSaved(null)) }, [])
  if (saved == null) return null
  const cur = draft ?? saved
  const valid = /^([01]\d|2[0-3]):[0-5]\d$/.test(cur)
  const save = async () => {
    if (!valid || busy) return
    setBusy(true)
    try {
      const out = await api.settings.put({ notify: { digest_at: cur } })
      setSaved(out.notify?.digest_at ?? cur)
      setDraft(null)
      toast.ok(M.digestSaved(out.notify?.digest_at ?? cur))
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }
  return (
    <section data-digest-section={admin ? 'edit' : 'readonly'}>
      <SectionBar title={M.digest} hint={admin ? M.digestHint : `${M.digestHint}。${M.digestReadOnly}`} />
      <div className="flex flex-wrap items-end gap-2">
        <Field label={M.digestAt}>
          {(p) => <input {...p} type="time" className="field tnum w-32 text-xs" value={cur} disabled={!admin} data-digest-at=""
                         onChange={(e) => setDraft(e.target.value)} />}
        </Field>
        {admin && (
          <button type="button" className="btn btn-sm" disabled={cur === saved || !valid || busy} onClick={() => void save()}
                  data-digest-save="">
            {busy ? <Spinner size={11} /> : <Check size={12} aria-hidden />} {M.digestSave}
          </button>
        )}
      </div>
    </section>
  )
}
