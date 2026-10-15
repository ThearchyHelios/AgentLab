import { useCallback, useEffect, useState } from 'react'
import { Plus, Users } from 'lucide-react'
import { api } from '../../api/client'
import {
  DeleteButton, EmptyState, ErrorState, Field, Modal, SectionBar, Skeleton, Spinner, confirmDialog, promptDialog, toast,
  useRadioGroup,
} from '../../components/ui'
import { formatDateTime } from '../../lib/format'
import { AUTH_MODE_LABEL, IDENTITY_TEXT as T, ROLE_LABEL } from '../../lib/terms'
import { useAtLeast, useAuth } from '../../store/auth'
import type { AuthMode, GroupRow, Role, UserRow } from '../../typesApp'

/*
 * 设置 → 身份（A7、§3.12）：登录方式、单点登录、使用者能否问数据、用户、组。只给管理员；关登录时所有人等同管理员。
 *
 * 从关闭登录切到本地账号时，必须在同一个请求里建出第一个管理员（否则切过去没有人能登录）；切到单点登录时至少
 * 配一个映射为管理员的用户或组（admin_users、admin_groups 至少一项：小团队的单点登录服务常常没有组声明）。环境变量指定了登录方式时这里只读。人员变动只改组成员，不发应用的新版本（写审计）
 */

const MODES: AuthMode[] = ['off', 'local', 'oidc']
const ROLES: Role[] = ['admin', 'builder', 'user']

type Settings = Awaited<ReturnType<typeof api.auth.settings>>

export function IdentityTab() {
  const admin = useAtLeast('admin')
  const [settings, setSettings] = useState<Settings | null>(null)
  const [error, setError] = useState<unknown>(null)
  const load = useCallback(() => {
    setError(null)
    api.auth.settings().then(setSettings).catch(setError)
  }, [])
  useEffect(load, [load])
  if (!admin) return <p className="text-xs text-faint">{T.adminOnly}</p>
  if (error) return <ErrorState error={error} onRetry={load} />
  if (!settings) return <Skeleton rows={4} height={40} gap={10} />
  return (
    <div className="space-y-8" data-identity="">
      <ModeSection settings={settings} onSaved={load} />
      <UsersSection mode={settings.mode} />
      <GroupsSection mode={settings.mode} />
    </div>
  )
}

function ModeSection({ settings, onSaved }: { settings: Settings; onSaved: () => void }) {
  const reloadMe = useAuth((s) => s.load)
  const [mode, setMode] = useState<AuthMode>(settings.mode)
  const [oidc, setOidc] = useState<Record<string, any>>(settings.oidc ?? {})
  const [chat, setChat] = useState(settings.user_can_chat)
  const [boot, setBoot] = useState({ username: '', password: '', display_name: '' })
  const [busy, setBusy] = useState(false)
  const radio = useRadioGroup(MODES, mode, setMode)
  const needsBoot = settings.mode === 'off' && mode === 'local'
  const oidcList = (key: string): string =>
    Array.isArray(oidc[key]) ? oidc[key].join('\n') : typeof oidc[key] === 'string' ? oidc[key] : ''
  const setList = (key: string, text: string) =>
    setOidc((o) => ({ ...o, [key]: text.split('\n').map((x) => x.trim()).filter(Boolean) }))
  const missingAdmin = mode === 'oidc' && !oidcList('admin_groups').trim() && !oidcList('admin_users').trim()
  const changed = mode !== settings.mode || chat !== settings.user_can_chat || JSON.stringify(oidc) !== JSON.stringify(settings.oidc ?? {})

  const save = async () => {
    setBusy(true)
    try {
      await api.auth.saveSettings({
        mode, user_can_chat: chat, ...(mode === 'oidc' ? { oidc } : {}),
        ...(needsBoot ? { bootstrap: { username: boot.username.trim(), password: boot.password,
                                      display_name: boot.display_name.trim() || undefined } } : {}),
      })
      toast.ok(T.saved)
      await reloadMe()
      onSaved()
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }

  return (
    <section data-identity-mode={settings.mode}>
      <SectionBar title={T.mode} />
      {settings.env_locked && <p className="mb-2 text-2xs" style={{ color: 'var(--st-waiting)' }}>{T.envLocked}</p>}
      <div className="grid grid-cols-3 gap-2" role="radiogroup" aria-label={T.mode}>
        {MODES.map((m) => (
          <button key={m} type="button" {...radio(m)} disabled={settings.env_locked} onClick={() => setMode(m)}
                  className={`rounded-lg border px-3 py-2 text-left transition-colors ${mode === m ? 'border-[var(--accent)] bg-accent-soft' : 'hover:bg-hover'}`}
                  data-identity-mode-option={m}>
            <div className="text-xs font-semibold">{AUTH_MODE_LABEL[m]}</div>
            <div className="mt-0.5 text-2xs leading-relaxed text-faint">{T.modeHint[m]}</div>
          </button>
        ))}
      </div>
      {needsBoot && (
        <fieldset className="mt-3 space-y-2 rounded-lg border px-3 py-2" data-identity-bootstrap="">
          <legend className="px-1 text-xs font-medium">{T.bootstrap}</legend>
          <p className="text-2xs text-faint">{T.bootstrapHint}</p>
          <div className="grid grid-cols-3 gap-2">
            <Field label={T.username} required>
              {(p) => <input {...p} className="field" value={boot.username} autoComplete="off"
                             onChange={(e) => setBoot({ ...boot, username: e.target.value })} />}
            </Field>
            <Field label={T.displayName}>
              {(p) => <input {...p} className="field" value={boot.display_name}
                             onChange={(e) => setBoot({ ...boot, display_name: e.target.value })} />}
            </Field>
            <Field label={T.password} required>
              {(p) => <input {...p} className="field" type="password" value={boot.password} autoComplete="new-password"
                             onChange={(e) => setBoot({ ...boot, password: e.target.value })} />}
            </Field>
          </div>
        </fieldset>
      )}
      {mode === 'oidc' && (
        <fieldset className="mt-3 space-y-2 rounded-lg border px-3 py-2" data-identity-oidc="">
          <legend className="px-1 text-xs font-medium">{T.oidcTitle}</legend>
          <div className="grid grid-cols-2 gap-2">
            {(['issuer', 'client_id', 'client_secret', 'groups_claim'] as const).map((k) => (
              <Field key={k} label={{ issuer: T.oidcIssuer, client_id: T.oidcClientId, client_secret: T.oidcClientSecret,
                                       groups_claim: T.oidcGroupsClaim }[k]}>
                {(p) => <input {...p} className="field mono text-xs" type={k === 'client_secret' ? 'password' : 'text'}
                               value={oidc[k] ?? ''} onChange={(e) => setOidc((o) => ({ ...o, [k]: e.target.value }))} />}
              </Field>
            ))}
            <Field label={T.oidcAdminUsers} hint={T.oidcListHint} error={missingAdmin ? T.oidcAdminRequired : undefined}>
              {(p) => <textarea {...p} className="field mono text-xs" rows={2} value={oidcList('admin_users')}
                                onChange={(e) => setList('admin_users', e.target.value)} data-identity-admin-users="" />}
            </Field>
            <Field label={T.oidcAdminGroups} hint={T.oidcListHint}>
              {(p) => <textarea {...p} className="field mono text-xs" rows={2} value={oidcList('admin_groups')}
                                onChange={(e) => setList('admin_groups', e.target.value)} data-identity-admin-groups="" />}
            </Field>
            <Field label={T.oidcBuilderGroups} hint={T.oidcListHint}>
              {(p) => <textarea {...p} className="field mono text-xs" rows={2} value={oidcList('builder_groups')}
                                onChange={(e) => setList('builder_groups', e.target.value)} />}
            </Field>
          </div>
        </fieldset>
      )}
      <label className="mt-3 flex items-start gap-2 text-xs">
        <input type="checkbox" className="mt-0.5 accent-[var(--accent)]" checked={chat} onChange={(e) => setChat(e.target.checked)}
               data-identity-user-chat="" />
        <span>{T.userCanChat}<span className="mt-0.5 block text-2xs text-faint">{T.userCanChatHint}</span></span>
      </label>
      <div className="mt-3 flex justify-end">
        <button type="button" className="btn btn-primary" data-identity-save=""
                disabled={busy || !changed || settings.env_locked || missingAdmin
                  || (needsBoot && (!boot.username.trim() || !boot.password))}
                onClick={() => void save()}>
          {busy ? <Spinner size={11} /> : null} 保存
        </button>
      </div>
    </section>
  )
}

function UsersSection({ mode }: { mode: AuthMode }) {
  const [rows, setRows] = useState<UserRow[] | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [creating, setCreating] = useState(false)
  const load = useCallback(() => {
    api.identity.users().then(({ items }) => { setRows(items); setError(null) }).catch(setError)
  }, [])
  useEffect(load, [load])

  const update = async (u: UserRow, body: Parameters<typeof api.identity.updateUser>[1]) => {
    try {
      await api.identity.updateUser(u.id, body)
      load()
    } catch (e) {
      toast.error(e)
    }
  }
  const resetPassword = async (u: UserRow) => {
    const pw = await promptDialog({ title: `${T.passwordReset}：${u.username}`, label: T.password, confirmLabel: T.passwordReset })
    if (pw) await update(u, { password: pw })
  }

  return (
    <section data-identity-users="">
      <SectionBar title={T.users} hint={mode === 'off' ? T.offNote : undefined}>
        <button className="btn btn-sm" onClick={() => setCreating(true)}><Plus size={12} aria-hidden /> {T.createUser}</button>
      </SectionBar>
      {error ? <ErrorState error={error} onRetry={load} compact /> : !rows ? <Skeleton rows={3} height={36} /> : !rows.length ? (
        <EmptyState icon={<Users size={20} />} title="还没有用户" />
      ) : (
        <div className="divide-y rounded-lg border">
          {rows.map((u) => (
            <div key={u.id} className="flex items-center gap-2 px-3 py-2 text-xs" data-user={u.username}>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5">
                  <span className="mono font-medium">{u.username}</span>
                  {u.display_name && u.display_name !== u.username && <span className="text-dim">{u.display_name}</span>}
                  {u.source === 'oidc' && <span className="chip">{T.sourceOidc}</span>}
                  {u.disabled && <span className="chip text-faint">{T.disabled}</span>}
                </div>
                <div className="text-2xs text-faint">
                  {u.email ? `${u.email} · ` : ''}{u.last_login_at ? T.lastLogin(formatDateTime(u.last_login_at)) : T.neverLogin}
                </div>
              </div>
              <select className="field h-7 w-28 py-0 text-xs" value={u.role} aria-label={`${T.role}：${u.username}`}
                      onChange={(e) => void update(u, { role: e.target.value as Role })}>
                {ROLES.map((r) => <option key={r} value={r}>{ROLE_LABEL[r]}</option>)}
              </select>
              {u.source === 'local' && (
                <button className="btn btn-xs btn-ghost" onClick={() => void resetPassword(u)}>{T.passwordReset}</button>
              )}
              <button className="btn btn-xs btn-ghost" onClick={() => void update(u, { disabled: !u.disabled })}>
                {u.disabled ? T.enable : T.disable}
              </button>
            </div>
          ))}
        </div>
      )}
      {creating && <UserCreator onClose={() => setCreating(false)} onSaved={() => { setCreating(false); load() }} />}
    </section>
  )
}

function UserCreator({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [form, setForm] = useState({ username: '', display_name: '', email: '', role: 'user' as Role, password: '' })
  const [busy, setBusy] = useState(false)
  const save = async () => {
    setBusy(true)
    try {
      await api.identity.createUser({
        username: form.username.trim(), display_name: form.display_name.trim() || undefined,
        email: form.email.trim() || undefined, role: form.role, password: form.password || undefined,
      })
      onSaved()
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }
  return (
    <Modal open onClose={onClose} title={T.createUser} width={520}
           footer={<>
             <button className="btn" onClick={onClose}>取消</button>
             <button className="btn btn-primary" disabled={busy || !form.username.trim()} onClick={() => void save()}>
               {busy ? <Spinner size={11} /> : null} {T.createUser}
             </button>
           </>}>
      <div className="grid grid-cols-2 gap-3">
        <Field label={T.username} hint={T.usernameHint} required>
          {(p) => <input {...p} className="field" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} />}
        </Field>
        <Field label={T.displayName}>
          {(p) => <input {...p} className="field" value={form.display_name} onChange={(e) => setForm({ ...form, display_name: e.target.value })} />}
        </Field>
        <Field label={T.email}>
          {(p) => <input {...p} className="field" type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />}
        </Field>
        <Field label={T.role}>
          {(p) => (
            <select {...p} className="field" value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as Role })}>
              {ROLES.map((r) => <option key={r} value={r}>{ROLE_LABEL[r]}</option>)}
            </select>
          )}
        </Field>
        <Field label={T.password}>
          {(p) => <input {...p} className="field" type="password" autoComplete="new-password" value={form.password}
                         onChange={(e) => setForm({ ...form, password: e.target.value })} />}
        </Field>
      </div>
    </Modal>
  )
}

function GroupsSection({ mode }: { mode: AuthMode }) {
  const [rows, setRows] = useState<GroupRow[] | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [editing, setEditing] = useState<GroupRow | 'new' | null>(null)
  const load = useCallback(() => {
    api.identity.groups().then(({ items }) => { setRows(items); setError(null) }).catch(setError)
  }, [])
  useEffect(load, [load])
  const remove = async (g: GroupRow) => {
    if (!(await confirmDialog({ title: T.removeGroup(g.label || g.name), body: T.removeGroupBody, danger: true, confirmLabel: '删除' }))) return
    try {
      await api.identity.deleteGroup(g.id)
      load()
    } catch (e) {
      toast.error(e)
    }
  }
  return (
    <section data-identity-groups="">
      <SectionBar title={T.groups} hint={mode === 'off' ? T.offNote : undefined}>
        <button className="btn btn-sm" onClick={() => setEditing('new')}><Plus size={12} aria-hidden /> {T.createGroup}</button>
      </SectionBar>
      {error ? <ErrorState error={error} onRetry={load} compact /> : !rows ? <Skeleton rows={2} height={36} /> : !rows.length ? (
        <EmptyState icon={<Users size={20} />} title="还没有组" />
      ) : (
        <div className="divide-y rounded-lg border">
          {rows.map((g) => (
            <div key={g.id} className="flex items-center gap-2 px-3 py-2 text-xs" data-group={g.name}>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5">
                  <span className="font-medium">{g.label || g.name}</span>
                  <span className="mono text-2xs text-faint">{g.name}</span>
                  {g.source === 'oidc' && <span className="chip" title={T.syncedReadOnly}>{T.sourceOidc}</span>}
                </div>
                <div className="truncate text-2xs text-faint">{T.members}：{g.members.join('、') || '—'}</div>
              </div>
              {g.source === 'local' && (
                <>
                  <button className="btn btn-xs btn-ghost" onClick={() => setEditing(g)}>编辑</button>
                  <DeleteButton label={`删除组 ${g.name}`} onClick={() => void remove(g)} />
                </>
              )}
            </div>
          ))}
        </div>
      )}
      {editing && <GroupEditor group={editing === 'new' ? null : editing} onClose={() => setEditing(null)}
                               onSaved={() => { setEditing(null); load() }} />}
    </section>
  )
}

function GroupEditor({ group, onClose, onSaved }: { group: GroupRow | null; onClose: () => void; onSaved: () => void }) {
  const [name, setName] = useState(group?.name ?? '')
  const [label, setLabel] = useState(group?.label ?? '')
  const [members, setMembers] = useState((group?.members ?? []).join('\n'))
  const [busy, setBusy] = useState(false)
  const save = async () => {
    setBusy(true)
    try {
      const g = group ?? await api.identity.createGroup({ name: name.trim(), label: label.trim() || undefined })
      await api.identity.setMembers(g.id, members.split('\n').map((x) => x.trim()).filter(Boolean))
      onSaved()
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }
  return (
    <Modal open onClose={onClose} title={group ? `${T.groups}：${group.label || group.name}` : T.createGroup} width={480}
           footer={<>
             <button className="btn" onClick={onClose}>取消</button>
             <button className="btn btn-primary" disabled={busy || !name.trim()} onClick={() => void save()}>
               {busy ? <Spinner size={11} /> : null} 保存
             </button>
           </>}>
      <div className="space-y-3">
        <div className="grid grid-cols-2 gap-3">
          <Field label={T.groupName} required>
            {(p) => <input {...p} className="field mono" value={name} disabled={!!group} onChange={(e) => setName(e.target.value)} />}
          </Field>
          <Field label={T.groupLabel}>
            {(p) => <input {...p} className="field" value={label} disabled={!!group} onChange={(e) => setLabel(e.target.value)} />}
          </Field>
        </div>
        <Field label={T.members} hint={T.membersHint}>
          {(p) => <textarea {...p} className="field mono text-xs" rows={6} value={members} onChange={(e) => setMembers(e.target.value)} />}
        </Field>
      </div>
    </Modal>
  )
}
