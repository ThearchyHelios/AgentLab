import { useCallback, useEffect, useState } from 'react'
import { BookOpen, KeyRound, Plus, Users } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../../api/client'
import {
  CopyButton, EmptyState, ErrorState, Field, Modal, Notice, SectionBar, Skeleton, Spinner, confirmDialog, toast,
} from '../../components/ui'
import { RecentRequests, type RecentRequestRow } from '../../components/RecentRequests'
import { formatDateTime } from '../../lib/format'
import { ACCESS_KEY_TEXT as T, RECENT_REQUESTS_TEXT } from '../../lib/termsApp'
import type {
  AccessKeyIssued, AccessKeyReference, AccessKeyRequestRow, AccessKeyRow, AccessKeyScope, AccessKeySettings,
  AppSummary, ServiceAccountRow,
} from '../../typesApp'

/*
 * 设置 → 访问密钥（docs/design/next-round/04-对外集成与触达.md A1、A6、A7）。
 *
 * 管理员：个人访问密钥的开关、服务账号（建、停用）、全部密钥；别人：自己的个人访问密钥，管理员打开开关之后才能建。
 * 密钥明文只在建、轮换的回包里有，这里显示一次、附复制按钮，关掉就没有了（服务端只存哈希）。撤销、轮换写审计；每把密钥
 * 能展开「最近的请求」（和钩子的收件记录同一个组件）。「接口说明」从服务端门禁那张表生成，和真实放行的接口出自一处。
 *
 * 关登录时访问密钥不生效：照实写一句，不给建密钥的入口（建了也用不了）。权限、到期、能不能轮换都由服务端判，这里照它给的
 * 状态显示（界面层不放逻辑）。
 */

type View = 'keys' | 'reference'

export function AccessKeysTab() {
  const [settings, setSettings] = useState<AccessKeySettings | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [view, setView] = useState<View>('keys')
  const load = useCallback(() => {
    setError(null)
    api.accessKeys.settings().then(setSettings).catch(setError)
  }, [])
  useEffect(load, [load])
  if (error) return <ErrorState error={error} onRetry={load} />
  if (!settings) return <Skeleton rows={4} height={40} gap={10} />
  return (
    <div className="space-y-6" data-access-keys={settings.admin ? 'admin' : 'self'}>
      <p className="text-xs leading-relaxed text-dim">{T.hint}</p>
      {!settings.login_on && <Notice tone="warn" attr={{ 'data-access-keys-login-off': '' }}>{T.loginOff}</Notice>}
      {/* 页内切换（不是又一层页签）：密钥、接口说明 */}
      <div className="flex gap-1" role="group" aria-label={T.tab}>
        {([['keys', T.viewKeys, KeyRound], ['reference', T.viewReference, BookOpen]] as const).map(([key, label, Icon]) => (
          <button key={key} type="button" aria-pressed={view === key} data-access-keys-view={key}
                  className={clsx('btn btn-sm', view === key && 'btn-primary')} onClick={() => setView(key)}>
            <Icon size={11} aria-hidden /> {label}
          </button>
        ))}
      </div>
      {view === 'reference' ? <Reference /> : (
        <>
          {settings.admin && settings.login_on && <PersonalSwitch settings={settings} onChange={setSettings} />}
          {settings.admin && settings.login_on && <ServiceAccounts />}
          {settings.login_on && <Keys settings={settings} />}
        </>
      )}
    </div>
  )
}

// ---------------------------------------------------------------- 个人访问密钥的开关（管理员）

function PersonalSwitch({ settings, onChange }: { settings: AccessKeySettings; onChange: (s: AccessKeySettings) => void }) {
  const [busy, setBusy] = useState(false)
  const flip = async (on: boolean) => {
    setBusy(true)
    try {
      onChange(await api.accessKeys.setPersonalKeys(on))
      toast.ok(on ? T.personalSwitchOn : T.personalSwitchOff)
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }
  return (
    <label className="flex items-start gap-2 text-xs" data-access-keys-personal-switch={settings.personal_keys ? 'on' : 'off'}>
      <input type="checkbox" className="mt-0.5 accent-[var(--accent)]" checked={settings.personal_keys} disabled={busy}
             onChange={(e) => void flip(e.target.checked)} />
      <span>{T.personalSwitch}<span className="mt-0.5 block text-2xs text-faint">{T.personalSwitchHint}</span></span>
    </label>
  )
}

// ---------------------------------------------------------------- 服务账号（管理员）

function ServiceAccounts() {
  const [rows, setRows] = useState<ServiceAccountRow[] | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [creating, setCreating] = useState(false)
  const load = useCallback(() => {
    api.accessKeys.serviceAccounts().then((items) => { setRows(items); setError(null) }).catch(setError)
  }, [])
  useEffect(load, [load])
  const toggle = async (row: ServiceAccountRow) => {
    if (!row.disabled && !(await confirmDialog({ title: T.disableTitle(row.username), body: T.disableBody, danger: true,
      confirmLabel: T.disable }))) return
    try {
      await api.accessKeys.updateServiceAccount(row.id, { disabled: !row.disabled })
      load()
    } catch (e) {
      toast.error(e)
    }
  }
  return (
    <section data-service-accounts="">
      <SectionBar title={T.serviceAccounts} hint={T.serviceHint}>
        <button type="button" className="btn btn-sm" onClick={() => setCreating(true)} data-service-account-create="">
          <Plus size={12} aria-hidden /> {T.createService}
        </button>
      </SectionBar>
      {error ? <ErrorState error={error} onRetry={load} compact /> : !rows ? <Skeleton rows={2} height={36} /> : !rows.length ? (
        <EmptyState icon={<Users size={20} />} title={T.serviceEmpty} />
      ) : (
        <div className="divide-y rounded-lg border">
          {rows.map((r) => (
            <div key={r.id} className="flex items-center gap-2 px-3 py-2 text-xs" data-service-account={r.username}>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5">
                  <span className="mono font-medium">{r.username}</span>
                  {r.display_name !== r.username && <span className="text-dim">{r.display_name}</span>}
                  {r.disabled && <span className="chip text-faint">{T.disabled}</span>}
                </div>
                <div className="text-2xs text-faint">{T.serviceKeys(r.keys)}</div>
              </div>
              <button type="button" className="btn btn-xs btn-ghost" onClick={() => void toggle(r)}
                      data-service-account-toggle={r.disabled ? 'enable' : 'disable'}>
                {r.disabled ? T.enable : T.disable}
              </button>
            </div>
          ))}
        </div>
      )}
      {creating && <ServiceCreator onClose={() => setCreating(false)} onSaved={() => { setCreating(false); load() }} />}
    </section>
  )
}

function ServiceCreator({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [username, setUsername] = useState('svc-')
  const [display, setDisplay] = useState('')
  const [busy, setBusy] = useState(false)
  const save = async () => {
    setBusy(true)
    try {
      const row = await api.accessKeys.createServiceAccount({ username: username.trim(), display_name: display.trim() || undefined })
      toast.ok(T.serviceCreated(row.username))
      onSaved()
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }
  return (
    <Modal open onClose={onClose} title={T.createService} width={480}
           footer={<>
             <button type="button" className="btn" onClick={onClose}>取消</button>
             <button type="button" className="btn btn-primary" disabled={busy || username.trim().length < 5}
                     onClick={() => void save()} data-service-account-save="">
               {busy ? <Spinner size={11} /> : null} {T.createService}
             </button>
           </>}>
      <div className="grid grid-cols-2 gap-3" data-service-account-form="">
        <Field label={T.serviceUsername} hint={T.serviceUsernameHint} required>
          {(p) => <input {...p} className="field mono" value={username} autoComplete="off"
                         onChange={(e) => setUsername(e.target.value)} data-service-account-username="" />}
        </Field>
        <Field label={T.serviceDisplay}>
          {(p) => <input {...p} className="field" value={display} onChange={(e) => setDisplay(e.target.value)} />}
        </Field>
      </div>
    </Modal>
  )
}

// ---------------------------------------------------------------- 密钥

function Keys({ settings }: { settings: AccessKeySettings }) {
  const [rows, setRows] = useState<AccessKeyRow[] | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [creating, setCreating] = useState(false)
  const [issued, setIssued] = useState<AccessKeyIssued | null>(null)
  const [open, setOpen] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const load = useCallback(() => {
    api.accessKeys.list().then((items) => { setRows(items); setError(null) }).catch(setError)
  }, [])
  useEffect(load, [load])
  const canCreate = settings.admin || settings.can_create_personal

  const revoke = async (row: AccessKeyRow) => {
    if (!(await confirmDialog({ title: T.revokeTitle(row.name), body: T.revokeBody, danger: true, confirmLabel: T.revoke }))) return
    setBusy(row.id)
    try {
      await api.accessKeys.revoke(row.id)
      toast.ok(T.revoked)
      load()
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(null)
    }
  }
  const rotate = async (row: AccessKeyRow) => {
    if (!(await confirmDialog({ title: T.rotateTitle(row.name), body: T.rotateBody, confirmLabel: T.rotate }))) return
    setBusy(row.id)
    try {
      setIssued(await api.accessKeys.rotate(row.id))
      toast.ok(T.rotated)
      load()
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(null)
    }
  }

  return (
    <section data-access-key-list="">
      <SectionBar title={settings.admin ? T.allKeys : T.myKeys}>
        {canCreate && (
          <button type="button" className="btn btn-sm" onClick={() => setCreating(true)} data-access-key-create="">
            <Plus size={12} aria-hidden /> {T.create}
          </button>
        )}
      </SectionBar>
      {!settings.admin && !settings.personal_keys && (
        <Notice tone="info" className="mb-2" attr={{ 'data-access-keys-personal-off': '' }}>{T.personalOff}</Notice>
      )}
      {issued && <KeyOnce issued={issued} onDone={() => setIssued(null)} />}
      {error ? <ErrorState error={error} onRetry={load} compact /> : !rows ? <Skeleton rows={3} height={44} /> : !rows.length ? (
        <EmptyState icon={<KeyRound size={20} />} title={T.keysEmpty} />
      ) : (
        <div className="divide-y rounded-lg border">
          {rows.map((r) => (
            <div key={r.id} className="space-y-1.5 px-3 py-2 text-xs" data-access-key={r.name} data-access-key-state={r.state}>
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="font-medium">{r.name}</span>
                <code className="mono text-2xs text-faint">{r.prefix}…</code>
                <span className="chip">{r.personal ? T.personalTag : T.serviceTag}</span>
                <span className="chip" style={r.state === 'active' ? { color: 'var(--ok)', borderColor: 'var(--ok)' }
                  : { color: 'var(--st-waiting)', borderColor: 'var(--st-waiting)' }}>{T.state[r.state] ?? r.state}</span>
                {r.rotated_from && <span className="text-2xs text-faint">{T.rotatedFrom}</span>}
                <span className="flex-1" />
                <button type="button" className="btn btn-xs btn-ghost" onClick={() => setOpen(open === r.id ? null : r.id)}
                        aria-expanded={open === r.id} data-access-key-requests-toggle={r.name}>
                  {open === r.id ? T.hideRequests : T.requests}
                </button>
                {r.state === 'active' && (
                  <>
                    <button type="button" className="btn btn-xs" disabled={busy === r.id} onClick={() => void rotate(r)}
                            data-access-key-rotate={r.name}>
                      {busy === r.id ? <Spinner size={10} /> : null} {T.rotate}
                    </button>
                    <button type="button" className="btn btn-xs btn-ghost" disabled={busy === r.id} onClick={() => void revoke(r)}
                            data-access-key-revoke={r.name}>
                      {T.revoke}
                    </button>
                  </>
                )}
              </div>
              <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-2xs text-faint">
                {settings.admin && <span>{T.owner}：<span className="mono">{r.owner.username ?? '—'}</span></span>}
                <span>{T.expiresAt(formatDateTime(r.expires_at))}</span>
                <span>{r.last_used_at ? T.lastUsed(formatDateTime(r.last_used_at)) : T.neverUsed}</span>
                <span>{r.app_ids ? T.appsCount(r.app_ids.length) : T.appsAll}</span>
              </div>
              <div className="flex flex-wrap gap-1" data-access-key-scopes="">
                {r.scopes.map((s) => <span key={s} className="chip text-2xs" title={T.scopeLabel[s]}>{s}</span>)}
              </div>
              {open === r.id && <KeyRequests id={r.id} />}
            </div>
          ))}
        </div>
      )}
      {creating && (
        <KeyCreator settings={settings} onClose={() => setCreating(false)}
                    onIssued={(k) => { setCreating(false); setIssued(k); toast.ok(T.created); load() }} />
      )}
    </section>
  )
}

/** 刚建好、刚轮换的密钥：只显示这一次 */
function KeyOnce({ issued, onDone }: { issued: AccessKeyIssued; onDone: () => void }) {
  return (
    <div className="mb-3 space-y-2 rounded-lg border px-3 py-2.5" style={{ borderColor: 'var(--st-waiting)' }} role="status"
         data-access-key-once={issued.name}>
      <div className="flex items-center gap-1.5 text-xs font-semibold">
        <KeyRound size={12} aria-hidden /> {T.onceTitle(issued.name)}
      </div>
      <p className="text-2xs leading-relaxed" style={{ color: 'var(--st-waiting)' }}>{T.onceBody}</p>
      <div className="flex items-center gap-2">
        <code className="mono min-w-0 flex-1 break-all rounded border bg-bg px-2 py-1 text-2xs" data-access-key-secret="">
          {issued.key}
        </code>
        <CopyButton text={issued.key} />
      </div>
      <div className="text-2xs text-faint">
        {T.onceUsage}：<code className="mono">Authorization: Bearer {issued.prefix}…</code>
      </div>
      <div className="flex justify-end">
        <button type="button" className="btn btn-sm" onClick={onDone} data-access-key-once-done="">{T.done}</button>
      </div>
    </div>
  )
}

function KeyCreator({ settings, onClose, onIssued }: {
  settings: AccessKeySettings
  onClose: () => void
  onIssued: (k: AccessKeyIssued) => void
}) {
  const [services, setServices] = useState<ServiceAccountRow[]>([])
  const [apps, setApps] = useState<AppSummary[] | null>(null)
  // 拥有者：'' 是我自己（个人访问密钥），否则是服务账号 id
  const [owner, setOwner] = useState<string | null>(settings.can_create_personal ? '' : null)
  const [name, setName] = useState('')
  const [scopes, setScopes] = useState<AccessKeyScope[]>([...settings.read_scopes])
  const [limited, setLimited] = useState<string[]>([])
  const [days, setDays] = useState(settings.expires_default_days)
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    if (settings.admin) {
      api.accessKeys.serviceAccounts().then((rows) => {
        const usable = rows.filter((r) => !r.disabled)
        setServices(usable)
        setOwner((cur) => (cur === null && usable.length ? usable[0].id : cur))
      }).catch(() => setServices([]))
    }
    api.apps.list().then(({ items }) => setApps(items.filter((a) => (a.scope ?? 'shared') !== 'personal' && a.status !== 'archived')))
      .catch(() => setApps([]))
  }, [settings.admin])
  const service = owner !== null && owner !== ''
  const personalOnly = new Set(settings.personal_scopes)
  const picked = scopes.filter((s) => !(service && personalOnly.has(s)))
  const tick = (s: AccessKeyScope, on: boolean) => setScopes((cur) => (on ? [...cur, s] : cur.filter((x) => x !== s)))
  const save = async () => {
    if (owner === null) return
    setBusy(true)
    try {
      onIssued(await api.accessKeys.create({
        name: name.trim(), scopes: picked, app_ids: limited.length ? limited : null, expires_in_days: days,
        ...(service ? { service_account_id: owner } : {}),
      }))
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }
  const daysOk = Number.isInteger(days) && days >= 1 && days <= settings.expires_max_days
  return (
    <Modal open onClose={onClose} title={T.create} width={600}
           footer={<>
             <button type="button" className="btn" onClick={onClose}>取消</button>
             <button type="button" className="btn btn-primary" data-access-key-save=""
                     disabled={busy || owner === null || !name.trim() || !picked.length || !daysOk} onClick={() => void save()}>
               {busy ? <Spinner size={11} /> : null} {T.create}
             </button>
           </>}>
      <div className="space-y-3" data-access-key-form="">
        {owner === null && !services.length && <Notice tone="warn">{T.noOwner}</Notice>}
        {settings.admin && (
          <Field label={T.owner}>
            {(p) => (
              <select {...p} className="field" value={owner ?? ''} onChange={(e) => setOwner(e.target.value)} data-access-key-owner="">
                {settings.can_create_personal && <option value="">{T.ownerMe}</option>}
                {services.map((s) => <option key={s.id} value={s.id}>{T.ownerService(s.username)}</option>)}
              </select>
            )}
          </Field>
        )}
        <div className="grid grid-cols-2 gap-3">
          <Field label={T.name} hint={T.nameHint} required>
            {(p) => <input {...p} className="field" value={name} maxLength={100} onChange={(e) => setName(e.target.value)}
                           data-access-key-name="" />}
          </Field>
          <Field label={T.expires} hint={T.expiresHint(settings.expires_max_days)} required
                 error={daysOk ? undefined : T.expiresHint(settings.expires_max_days)}>
            {(p) => <input {...p} className="field tnum" type="number" min={1} max={settings.expires_max_days} value={days}
                           onChange={(e) => setDays(Number(e.target.value))} data-access-key-days="" />}
          </Field>
        </div>
        <fieldset className="space-y-1 rounded-lg border px-3 py-2" data-access-key-scope-list="">
          <legend className="px-1 text-xs font-medium">{T.scopes}</legend>
          <p className="text-2xs text-faint">{T.scopesHint}</p>
          {settings.scopes.map((s) => {
            const blocked = service && personalOnly.has(s)
            return (
              <label key={s} className={clsx('flex items-start gap-2 text-xs', blocked && 'opacity-60')} data-access-key-scope={s}>
                <input type="checkbox" className="mt-0.5 accent-[var(--accent)]" checked={!blocked && scopes.includes(s)}
                       disabled={blocked} onChange={(e) => tick(s, e.target.checked)} />
                <span>
                  <code className="mono">{s}</code> {T.scopeLabel[s]}
                  {blocked && <span className="block text-2xs text-faint">{T.serviceNoRespond}</span>}
                </span>
              </label>
            )
          })}
        </fieldset>
        <fieldset className="space-y-1 rounded-lg border px-3 py-2" data-access-key-app-list="">
          <legend className="px-1 text-xs font-medium">{T.apps}</legend>
          <p className="text-2xs text-faint">{T.appsHint}</p>
          {!apps ? <Skeleton rows={2} height={16} /> : !apps.length ? <p className="text-2xs text-faint">{T.appsNone}</p> : (
            <div className="max-h-40 space-y-0.5 overflow-y-auto">
              {apps.map((a) => (
                <label key={a.id} className="flex items-center gap-2 text-xs" data-access-key-app={a.id}>
                  <input type="checkbox" className="accent-[var(--accent)]" checked={limited.includes(a.id)}
                         onChange={(e) => setLimited((cur) => (e.target.checked ? [...cur, a.id] : cur.filter((x) => x !== a.id)))} />
                  {a.name}
                </label>
              ))}
            </div>
          )}
        </fieldset>
      </div>
    </Modal>
  )
}

/** 一把密钥最近的请求 */
function KeyRequests({ id }: { id: string }) {
  const [rows, setRows] = useState<RecentRequestRow[] | null>(null)
  const [days, setDays] = useState<number | null>(null)
  const [error, setError] = useState<unknown>(null)
  const load = useCallback(() => {
    setError(null)
    api.accessKeys.requests(id).then(({ items, retain_days }) => { setRows(items.map(requestRow)); setDays(retain_days) })
      .catch(setError)
  }, [id])
  useEffect(load, [load])
  return <RecentRequests rows={rows} error={error} onRetry={load} summary={days != null ? T.requestsHint(days) : undefined}
                         attr={{ 'data-access-key-requests': id }} />
}

function requestRow(r: AccessKeyRequestRow, i: number): RecentRequestRow {
  return {
    key: `${r.at}-${i}`, at: r.at, tone: r.status >= 200 && r.status < 300 ? 'ok' : 'bad',
    result: RECENT_REQUESTS_TEXT.status(r.status), what: `${r.method} ${r.route}`, detail: r.ip,
    ref: r.run_id ? { label: RECENT_REQUESTS_TEXT.run, id: r.run_id } : null,
  }
}

// ---------------------------------------------------------------- 接口说明

function Reference() {
  const [ref, setRef] = useState<AccessKeyReference | null>(null)
  const [error, setError] = useState<unknown>(null)
  const load = useCallback(() => {
    setError(null)
    api.accessKeys.reference().then(setRef).catch(setError)
  }, [])
  useEffect(load, [load])
  if (error) return <ErrorState error={error} onRetry={load} />
  if (!ref) return <Skeleton rows={6} height={24} />
  return (
    <section className="space-y-3 text-xs" data-access-key-reference={ref.routes.length}>
      <SectionBar title={T.reference} hint={T.referenceHint} />
      <dl className="grid grid-cols-[8rem_1fr] gap-x-3 gap-y-1">
        <dt className="text-faint">{T.refAuth}</dt>
        <dd><code className="mono">{ref.header}</code></dd>
        <dt className="text-faint">{T.refBase}</dt>
        <dd>{ref.base_url ? <code className="mono">{ref.base_url}</code> : <span className="text-faint">{T.refBaseNone}</span>}</dd>
      </dl>
      <p className="text-2xs text-dim">{T.refIdempotent(ref.idempotency_header)}</p>
      <p className="text-2xs text-dim">{T.refRate(ref.rate_per_minute)}</p>
      <div className="overflow-x-auto rounded-lg border">
        <table className="w-full text-2xs">
          <thead className="bg-bg text-left text-faint">
            <tr>
              <th className="px-2 py-1.5 font-medium">{T.refColMethod}</th>
              <th className="px-2 py-1.5 font-medium">{T.refColPath}</th>
              <th className="px-2 py-1.5 font-medium">{T.refColScope}</th>
              <th className="px-2 py-1.5 font-medium">{T.refColSummary}</th>
              <th className="px-2 py-1.5 font-medium">{T.refColErrors}</th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {ref.routes.map((r) => (
              <tr key={`${r.method} ${r.path}`} className="align-top" data-access-key-route={`${r.method} ${r.path}`}>
                <td className="mono px-2 py-1.5">{r.method}</td>
                <td className="mono px-2 py-1.5">{r.path}</td>
                <td className="mono px-2 py-1.5">{r.scope === ref.implicit_scope ? T.refImplicit : r.scope}</td>
                <td className="px-2 py-1.5">
                  {r.summary}
                  {r.idempotent && <span className="chip ml-1">{T.refNeedsKey}</span>}
                </td>
                <td className="mono px-2 py-1.5 text-faint">{r.errors.join(' ')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="text-2xs">
        <span className="text-faint">{T.refCommon}：</span>
        <code className="mono">{ref.common_errors.join(' ')}</code>
      </div>
    </section>
  )
}
