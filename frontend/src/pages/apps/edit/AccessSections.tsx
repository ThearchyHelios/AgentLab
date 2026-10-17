import { useCallback, useEffect, useState } from 'react'
import { KeyRound, Link2, Plus } from 'lucide-react'
import { api } from '../../../api/client'
import type { AppHookRow, HookReceipts, ViewShareCreated, ViewShareList, ViewShareRow } from '../../../typesApp'
import { RecentRequests, type RecentRequestRow } from '../../../components/RecentRequests'
import { formatDateTime } from '../../../lib/format'
import { HOOK_RECEIPT_TEXT, RECENT_REQUESTS_TEXT, SHARE_TEXT as S } from '../../../lib/termsApp'
import {
  CopyButton, DeleteButton, EmptyState, ErrorState, Field, SectionBar, Skeleton, Spinner, confirmDialog, toast,
} from '../../../components/ui'
import { PrincipalPicker, type Principal as Picked } from '../../../components/PrincipalPicker'
import { APP_EDITOR_TEXT as T, GRANT_ROLE_HINT, GRANT_ROLE_LABEL, NOTIFY_TEXT } from '../../../lib/terms'
import { useLoginOn } from '../../../store/auth'
import type { AppGrant, GrantRole, NotifyChannelRow, NotifyEventRow } from '../../../typesApp'
import { applyAppOp } from './appOps'
import { useEditor } from './context'

/*
 * 岗位与授权、钩子、通知订阅。
 *
 * 岗位：图里人工节点写的岗位名（assignee 的岗位）在这里绑定到人或组；绑定不进 spec，存在草稿的 bindings 里，发布时
 * 进应用版本——spec 可以导出，绑定是这一个部署自己的。人员变动只改组成员，不发新版本。
 * 授权：use / observe / manage，即时生效、写审计，不需要发新版本（A1）。关登录时所有人都是管理员，授权开登录后才生效
 * 分享链接（04 细案 C2）：管理员开了「允许建分享链接」后，管理应用的人给发布版的一页建只读、不用登录的链接（必设到期、
 * 最长 90 天、可撤销）；链接只在建好时显示一次。关着时不给新建，已有的照列、能撤销
 */

const ROLES: GrantRole[] = ['use', 'observe', 'manage']

export function AccessSection() {
  const { draft, setDraft, bindings, setBindings } = useEditor()
  const [adding, setAdding] = useState('')
  const slots = Object.entries(draft.slots)

  const addSlot = () => {
    const name = adding.trim()
    if (!name || name in draft.slots) return
    setDraft((s) => applyAppOp(s, { op: 'app_set', path: 'slots', key: name, value: { label: name } }))
    setAdding('')
  }

  return (
    <div className="mx-auto max-w-3xl space-y-8 p-4" data-app-access="">
      <section data-app-slots={slots.length}>
        <SectionBar title={T.slots} hint={T.slotsHint} />
        {!slots.length && <p className="mb-2 text-2xs text-faint">—</p>}
        <div className="space-y-2">
          {slots.map(([name, slot]) => {
            const bound = bindings.slots?.[name] ?? []
            return (
              <div key={name} className="space-y-1.5 rounded-lg border bg-panel px-3 py-2" data-app-slot={name}>
                <div className="flex items-end gap-2">
                  <div className="mono w-36 shrink-0 pb-1.5 text-xs">{name}</div>
                  <Field label={T.slotLabel} className="min-w-0 flex-1">
                    {(p) => <input {...p} className="field" value={slot.label}
                                   onChange={(e) => setDraft((s) => applyAppOp(s, { op: 'app_set', path: 'slots', key: name, value: { label: e.target.value } }))} />}
                  </Field>
                  <DeleteButton label={T.slotRemove(name)} onClick={() => {
                    setDraft((s) => applyAppOp(s, { op: 'app_set', path: 'slots', key: name, value: null }))
                    setBindings((b) => { const next = { ...b.slots }; delete next[name]; return { ...b, slots: next } })
                  }} />
                </div>
                {slot.label.trim() === name && <p className="text-2xs text-faint" data-app-slot-raw="">{T.slotLabelRaw}</p>}
                <PrincipalPicker label={T.slots} value={bound as Picked[]}
                                 onChange={(v) => setBindings((b) => ({
                                   ...b, slots: { ...b.slots, [name]: v.filter((x) => x.kind !== 'everyone') as { kind: 'user' | 'group'; name: string }[] },
                                 }))} />
                {!bound.length && <p className="text-2xs" style={{ color: 'var(--st-waiting)' }}>{T.slotUnbound}</p>}
              </div>
            )
          })}
        </div>
        <div className="mt-2 flex gap-2">
          <input className="field mono w-48 text-xs" value={adding} placeholder={T.slotName} aria-label={T.slotName}
                 onChange={(e) => setAdding(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') addSlot() }} />
          <button type="button" className="btn btn-sm" disabled={!adding.trim()} onClick={addSlot}><Plus size={11} aria-hidden /> {T.slotAdd}</button>
        </div>
      </section>
      <Grants />
      <Shares />
    </div>
  )
}

/** 有效期的几个选项（天），按服务端的上限截 */
const SHARE_DAYS = [1, 7, 30, 90]

function Shares() {
  const { appId, detail } = useEditor()
  const personal = detail.scope === 'personal'
  const pages = detail.published?.pages ?? []
  const [list, setList] = useState<ViewShareList | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [form, setForm] = useState<{ page: string; mode: 'latest' | 'fixed'; days: number; embed: boolean } | null>(null)
  const [busy, setBusy] = useState(false)
  const [made, setMade] = useState<ViewShareCreated | null>(null)
  const load = useCallback(() => {
    if (personal) return
    api.apps.shares(appId).then((v) => { setList(v); setError(null) }, setError)
  }, [appId, personal])
  useEffect(load, [load])

  if (personal) {
    return (
      <section data-app-shares="personal">
        <SectionBar title={S.section} hint={S.personal} />
      </section>
    )
  }
  const create = async () => {
    if (!form || busy) return
    setBusy(true)
    try {
      const out = await api.apps.createShare(appId, {
        page_id: form.page, mode: form.mode, embed_allowed: form.embed,
        expires_at: new Date(Date.now() + form.days * 86_400_000).toISOString(),
      })
      setMade(out)
      setForm(null)
      load()
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }
  const revoke = async (row: ViewShareRow) => {
    if (!(await confirmDialog({ title: S.revokeTitle(row.prefix), body: S.revokeBody, danger: true, confirmLabel: S.revoke }))) return
    try {
      await api.apps.revokeShare(appId, row.id)
      toast.ok(S.revoked)
      load()
    } catch (e) {
      toast.error(e)
    }
  }
  const maxDays = list?.max_days ?? 90
  const canCreate = !!list?.enabled && pages.length > 0 && detail.published_version != null
  const hint = !list ? S.hint : !list.enabled ? (list.items.length ? S.offWithRows : S.off)
    : detail.published_version == null ? S.unpublished : S.hint
  return (
    <section data-app-shares={list ? (list.enabled ? 'on' : 'off') : ''}>
      <SectionBar title={S.section} hint={hint}>
        {canCreate && !form && (
          <button type="button" className="btn btn-sm" data-app-share-create=""
                  onClick={() => { setMade(null); setForm({ page: pages[0].id, mode: 'latest', days: Math.min(30, maxDays), embed: false }) }}>
            <Plus size={11} aria-hidden /> {S.create}
          </button>
        )}
      </SectionBar>
      {made && <ShareOnce made={made} onDismiss={() => setMade(null)} />}
      {form && (
        <div className="mb-3 space-y-3 rounded-lg border bg-panel px-3 py-3" data-app-share-form="">
          <Field label={S.page} hint={S.pageHint}>
            {(p) => (
              <select {...p} className="field max-w-72" value={form.page} data-app-share-page=""
                      onChange={(e) => setForm({ ...form, page: e.target.value })}>
                {pages.map((pg) => <option key={pg.id} value={pg.id}>{pg.title || pg.id}</option>)}
              </select>
            )}
          </Field>
          <fieldset>
            <legend className="label">{S.mode}</legend>
            {(['latest', 'fixed'] as const).map((m) => (
              <label key={m} className="flex items-center gap-1.5 text-xs">
                <input type="radio" name="share-mode" checked={form.mode === m} data-app-share-mode={m}
                       onChange={() => setForm({ ...form, mode: m })} />
                {m === 'latest' ? S.modeLatest : S.modeFixed}
              </label>
            ))}
          </fieldset>
          <Field label={S.expires} hint={S.expiresHint(maxDays)} required>
            {(p) => (
              <select {...p} className="field w-32" value={form.days} data-app-share-days=""
                      onChange={(e) => setForm({ ...form, days: Number(e.target.value) })}>
                {SHARE_DAYS.filter((d) => d <= maxDays).map((d) => <option key={d} value={d}>{S.expiresDays(d)}</option>)}
              </select>
            )}
          </Field>
          <label className="flex items-start gap-2 text-xs">
            <input type="checkbox" className="mt-0.5" checked={form.embed} data-app-share-embed=""
                   onChange={(e) => setForm({ ...form, embed: e.target.checked })} />
            <span>
              {S.embed}
              <span className="mt-0.5 block text-2xs leading-relaxed text-faint">{list?.embed_origins ? S.embedHint : S.embedNoOrigins}</span>
            </span>
          </label>
          <div className="flex justify-end gap-2">
            <button type="button" className="btn btn-sm" onClick={() => setForm(null)}>取消</button>
            <button type="button" className="btn btn-sm btn-primary" disabled={busy} onClick={() => void create()} data-app-share-submit="">
              {busy ? <Spinner size={11} /> : <Link2 size={11} aria-hidden />} {S.submit}
            </button>
          </div>
        </div>
      )}
      {error ? <ErrorState error={error} onRetry={load} /> : !list ? <Skeleton rows={2} height={36} /> : !list.items.length ? (
        <p className="text-2xs text-faint" data-app-shares-empty="">{S.empty}</p>
      ) : (
        <div className="space-y-1.5">
          {list.items.map((row) => (
            <div key={row.id} className="flex flex-wrap items-center gap-2 rounded-lg border bg-panel px-3 py-2 text-xs"
                 data-app-share={row.prefix} data-app-share-state={row.state}>
              <span className="mono">{row.prefix}…</span>
              <span className="font-medium">{row.page_title}</span>
              <span className="chip">{S.modeShort[row.mode] ?? row.mode}</span>
              <span className="chip" style={row.state === 'active' ? { color: 'var(--ok)' } : { color: 'var(--st-waiting)' }}>
                {S.state[row.state] ?? row.state}
              </span>
              {row.embed_allowed && <span className="chip">{S.embedded}</span>}
              <span className="min-w-0 flex-1 truncate text-2xs text-faint">
                {[row.expires_at ? S.until(formatDateTime(row.expires_at)) : null, S.opened(row.open_count),
                  row.created_by_name || row.created_by ? S.by(row.created_by_name || row.created_by || '') : null]
                  .filter(Boolean).join(' · ')}
              </span>
              {row.state === 'active' && (
                <button type="button" className="btn btn-xs" onClick={() => void revoke(row)} data-app-share-revoke={row.prefix}>
                  {S.revoke}
                </button>
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

/** 刚建好的分享链接：只显示这一次（库里只有哈希），附复制按钮；关掉就没有了 */
function ShareOnce({ made, onDismiss }: { made: ViewShareCreated; onDismiss: () => void }) {
  const link = made.url ?? `${window.location.origin}${made.path}`
  return (
    <div className="mb-3 space-y-2 rounded-lg border px-3 py-2.5" style={{ borderColor: 'var(--st-waiting)' }} role="status"
         data-app-share-once="">
      <div className="flex items-center gap-1.5 text-xs font-semibold"><Link2 size={12} aria-hidden /> {S.created}</div>
      <p className="text-2xs leading-relaxed" style={{ color: 'var(--st-waiting)' }}>{S.once}</p>
      <div className="flex items-center gap-2">
        <code className="mono min-w-0 flex-1 break-all rounded border bg-bg px-2 py-1 text-2xs" data-app-share-link="">{link}</code>
        <CopyButton text={link} />
      </div>
      <div className="flex justify-end">
        <button type="button" className="btn btn-sm" onClick={onDismiss} data-app-share-dismiss="">{S.dismiss}</button>
      </div>
    </div>
  )
}

function Grants() {
  const { appId } = useEditor()
  const loginOn = useLoginOn()
  const [items, setItems] = useState<AppGrant[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [dirty, setDirty] = useState(false)
  const load = useCallback(() => {
    api.apps.grants(appId).then(({ items: list }) => { setItems(list); setDirty(false) }).catch(() => setItems([]))
  }, [appId])
  useEffect(load, [load])
  const save = async () => {
    if (!items) return
    setBusy(true)
    try {
      const { items: saved } = await api.apps.setGrants(appId, items)
      setItems(saved)
      setDirty(false)
      toast.ok(T.grantsSaved)
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }
  return (
    <section data-app-grants={items?.length ?? ''}>
      <SectionBar title={T.grants} hint={loginOn ? T.grantsHint : T.grantsOff}>
        <button type="button" className="btn btn-sm btn-primary" disabled={!dirty || busy} onClick={() => void save()}>
          {busy ? <Spinner size={11} /> : null} {T.grantsSave}
        </button>
      </SectionBar>
      {!items ? <Skeleton rows={3} /> : (
        <div className="space-y-3">
          {ROLES.map((role) => (
            // 每一档写明叫什么、管什么：三行选择器长得一样，不写就分不出哪一行是哪一档
            <div key={role} className="rounded-lg border bg-panel px-3 py-2" data-app-grant-role={role}>
              <div className="mb-1.5">
                <div className="text-xs font-medium" data-app-grant-title="">{GRANT_ROLE_LABEL[role]}</div>
                <p className="mt-0.5 text-2xs leading-relaxed text-faint">{GRANT_ROLE_HINT[role]}</p>
              </div>
              {/* 服务账号能拿授权（外部系统按授权读应用）；岗位绑定那边不能选它 */}
              <PrincipalPicker label={GRANT_ROLE_LABEL[role]} allowEveryone={role !== 'manage'} allowService
                               value={items.filter((g) => g.role === role).map((g) => ({ kind: g.principal_kind, name: g.principal }))}
                               onChange={(v) => {
                                 setItems((cur) => [...(cur ?? []).filter((g) => g.role !== role),
                                   ...v.map((p) => ({ principal_kind: p.kind, principal: p.name, role }))])
                                 setDirty(true)
                               }} />
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

/*
 * 钩子（D-g）：按名字关联。spec.hooks 声明 {name, kind, entry}（进草稿、随应用版本发布）；密钥按 (应用, 名字) 存在服务端，
 * 只在「生成 / 轮换」时交回一次，这里显示一次、附复制按钮，离开这一节就不再有。入站请求到来时服务端按当前发布版里这个
 * 名字的种类和入口处理：发布版里没声明、停用了的一律拒收——所以每一行照实标出「发布版里有没有」「生成过密钥没有」。
 * 改了名字就是另一个钩子：旧名字的密钥行留在下面「草稿里没有声明」那一组，可以删掉
 */
export function HooksSection() {
  const { appId, draft, setDraft, dirty, save } = useEditor()
  const [rows, setRows] = useState<AppHookRow[] | null>(null)
  const [loadError, setLoadError] = useState<unknown>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [shown, setShown] = useState<{ name: string; secret: string; path: string | null } | null>(null)
  /** 展开了哪一个钩子的收件记录 */
  const [receiptsOf, setReceiptsOf] = useState<string | null>(null)
  const load = useCallback(() => {
    api.apps.hooks(appId).then((list) => { setRows(list); setLoadError(null) }).catch(setLoadError)
  }, [appId])
  useEffect(load, [load])

  const set = (hooks: typeof draft.hooks) => setDraft((s) => ({ ...s, hooks }))
  const stateOf = (name: string) => rows?.find((r) => r.name === name)
  const undeclared = (rows ?? []).filter((r) => r.has_secret && !draft.hooks.some((h) => h.name === r.name))
  const names = draft.hooks.map((h) => h.name.trim())
  const dup = (name: string) => names.filter((n) => n === name.trim()).length > 1

  /** 生成或轮换：服务端按保存过的草稿认这个名字，有未保存的修改先存 */
  const rotate = async (name: string) => {
    const cur = stateOf(name)
    if (cur?.has_secret && !(await confirmDialog({
      title: T.hookRotateTitle(name), body: T.hookRotateBody, confirmLabel: T.hookRotateConfirm, danger: true,
    }))) return
    setBusy(name)
    try {
      if (dirty && !(await save())) return
      const row = await api.apps.rotateHook(appId, name)
      const id = row.id ?? cur?.id ?? null
      if (row.secret) setShown({ name, secret: row.secret, path: row.url_path ?? (id ? `/api/hooks/${encodeURIComponent(id)}` : null) })
      toast.ok(T.hookRotated(name))
      load()
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(null)
    }
  }
  const toggle = async (name: string, enabled: boolean) => {
    setBusy(name)
    try {
      await api.apps.setHookEnabled(appId, name, enabled)
      load()
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(null)
    }
  }
  const removeKey = async (name: string) => {
    if (!(await confirmDialog({ title: T.hookDeleteTitle(name), body: T.hookDeleteBody, confirmLabel: T.hookDeleteKey, danger: true }))) return
    setBusy(name)
    try {
      await api.apps.deleteHook(appId, name)
      if (shown?.name === name) setShown(null)
      toast.ok(T.hookDeleted(name))
      load()
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(null)
    }
  }

  const status = (name: string, declared: boolean) => (
    <>
      <HookStatus name={name} state={stateOf(name)} declared={declared} busy={busy === name} loaded={!!rows}
                  receipts={receiptsOf === name} onReceipts={() => setReceiptsOf(receiptsOf === name ? null : name)}
                  onRotate={() => void rotate(name)} onToggle={(v) => void toggle(name, v)} onRemove={() => void removeKey(name)} />
      {receiptsOf === name && stateOf(name)?.has_secret && <HookReceiptsPanel appId={appId} name={name} />}
    </>
  )

  return (
    <div className="mx-auto max-w-3xl space-y-4 p-4" data-app-hooks={draft.hooks.length}>
      <SectionBar title={T.sections.hooks} hint={T.hooksHint}>
        <button type="button" className="btn btn-sm" data-app-hook-add=""
                onClick={() => set([...draft.hooks, { name: `hook${draft.hooks.length + 1}`, kind: 'signal', entry: null }])}>
          <Plus size={11} aria-hidden /> {T.hookAdd}
        </button>
      </SectionBar>
      {shown && <SecretOnce shown={shown} onDismiss={() => setShown(null)} />}
      {loadError ? <ErrorState compact error={loadError} onRetry={load} /> : null}
      {!draft.hooks.length && !undeclared.length ? <EmptyState title={T.hooksEmpty} /> : (
        <div className="space-y-2">
          {draft.hooks.map((h, i) => (
            <div key={i} className="space-y-1.5 rounded-lg border bg-panel px-3 py-2" data-app-hook={h.name}>
              <div className="grid grid-cols-[1fr_140px_1fr_auto] items-end gap-2">
                <Field label={T.hookName} error={h.name.trim() && dup(h.name) ? T.hookDuplicate(h.name.trim()) : undefined}>
                  {(p) => <input {...p} className="field mono text-xs" value={h.name}
                                 onChange={(e) => set(draft.hooks.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))} />}
                </Field>
                <Field label={T.hookKind}>
                  {(p) => (
                    <select {...p} className="field" value={h.kind}
                            onChange={(e) => set(draft.hooks.map((x, j) => (j === i ? { ...x, kind: e.target.value as 'start' | 'signal' } : x)))}>
                      {Object.entries(T.hookKinds).map(([k, label]) => <option key={k} value={k}>{label}</option>)}
                    </select>
                  )}
                </Field>
                {h.kind === 'start' ? (
                  <Field label={T.hookEntry}>
                    {(p) => (
                      <select {...p} className="field" value={h.entry ?? ''}
                              onChange={(e) => set(draft.hooks.map((x, j) => (j === i ? { ...x, entry: e.target.value || null } : x)))}>
                        <option value="">—</option>
                        {draft.entries.map((en) => <option key={en.id} value={en.id}>{en.label || en.id}</option>)}
                      </select>
                    )}
                  </Field>
                ) : <div />}
                <DeleteButton label={T.hookRemove(h.name)} onClick={() => set(draft.hooks.filter((_, j) => j !== i))} />
              </div>
              {h.name.trim() && !dup(h.name) && status(h.name.trim(), true)}
            </div>
          ))}
          {undeclared.map((r) => (
            <div key={r.name} className="space-y-1.5 rounded-lg border border-dashed px-3 py-2" data-app-hook={r.name}
                 data-app-hook-undeclared="">
              <div className="flex items-center gap-2 text-xs">
                <span className="mono font-medium">{r.name}</span>
                {r.kind && <span className="chip">{T.hookKinds[r.kind]}</span>}
              </div>
              {status(r.name, false)}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

/** 一行钩子的服务端状态：发布版里有没有、生成过密钥没有、启用；生成 / 轮换、删除密钥；展开收件记录 */
function HookStatus({ name, state, declared, busy, loaded, receipts, onReceipts, onRotate, onToggle, onRemove }: {
  name: string
  state: AppHookRow | undefined
  declared: boolean
  busy: boolean
  loaded: boolean
  receipts: boolean
  onReceipts: () => void
  onRotate: () => void
  onToggle: (enabled: boolean) => void
  onRemove: () => void
}) {
  if (!loaded) return null
  const has = !!state?.has_secret
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-2xs" data-app-hook-state={has ? 'secret' : 'none'}
         data-app-hook-published={state?.in_published ? '1' : '0'}>
      <span className="chip" style={has ? { color: 'var(--ok)', borderColor: 'var(--ok)' } : undefined}>
        {has ? T.hookSecretSet : T.hookSecretNone}
      </span>
      {!declared
        ? <span style={{ color: 'var(--st-waiting)' }}>{T.hookNotDeclared}</span>
        : state?.in_published ? <span className="text-faint">{T.hookPublished}</span>
        : <span style={{ color: 'var(--st-waiting)' }}>{T.hookNotPublished}</span>}
      <span className="flex-1" />
      {has && (
        <button type="button" className="btn btn-xs btn-ghost" onClick={onReceipts} aria-expanded={receipts}
                data-app-hook-receipts-toggle={name}>
          {receipts ? HOOK_RECEIPT_TEXT.hide : HOOK_RECEIPT_TEXT.show}
        </button>
      )}
      {has && (
        <label className="flex items-center gap-1" title={T.hookEnabledHint}>
          <input type="checkbox" className="accent-[var(--accent)]" checked={state!.enabled} disabled={busy}
                 onChange={(e) => onToggle(e.target.checked)} data-app-hook-enabled="" />
          {T.hookEnabled}
        </label>
      )}
      {declared && (
        <button type="button" className="btn btn-xs" disabled={busy} onClick={onRotate} data-app-hook-rotate={name}>
          {busy ? <Spinner size={10} /> : <KeyRound size={10} aria-hidden />} {has ? T.hookRotate : T.hookGenerate}
        </button>
      )}
      {has && (
        <button type="button" className="btn btn-xs btn-ghost" disabled={busy} onClick={onRemove} data-app-hook-delete={name}>
          {T.hookDeleteKey}
        </button>
      )}
    </div>
  )
}

/**
 * 钩子的收件记录（04 细案 A6）：最近 100 条验过签名的请求（状态、原因、发起的实例或解决的待办）和近 1 小时验签失败的次数。
 * 和访问密钥的「最近的请求」同一个组件
 */
function HookReceiptsPanel({ appId, name }: { appId: string; name: string }) {
  const [data, setData] = useState<HookReceipts | null>(null)
  const [error, setError] = useState<unknown>(null)
  const load = useCallback(() => {
    setError(null)
    api.apps.hookReceipts(appId, name).then(setData).catch(setError)
  }, [appId, name])
  useEffect(load, [load])
  const rows: RecentRequestRow[] | null = data ? data.items.map((r, i) => ({
    key: `${r.idem_key}-${i}`, at: r.received_at,
    tone: r.status === 'accepted' ? 'ok' : r.status === 'rejected' ? 'bad' : 'pending',
    result: HOOK_RECEIPT_TEXT.status[r.status] ?? r.status, what: `${HOOK_RECEIPT_TEXT.idem} ${r.idem_key}`, detail: r.detail,
    ref: r.run_id ? { label: RECENT_REQUESTS_TEXT.instance, id: r.run_id,
      to: `/apps/${encodeURIComponent(appId)}/instances/${encodeURIComponent(r.run_id)}` }
      : r.task_id ? { label: RECENT_REQUESTS_TEXT.task, id: r.task_id } : null,
  })) : null
  return (
    <div className="space-y-1 rounded-lg border bg-bg px-2.5 py-2" data-app-hook-receipts={name}>
      <div className="text-2xs font-medium">{HOOK_RECEIPT_TEXT.title(name)}</div>
      <RecentRequests rows={rows} error={error} onRetry={load} empty={HOOK_RECEIPT_TEXT.empty}
                      summary={data ? HOOK_RECEIPT_TEXT.failures(data.signature_failures) : undefined} />
    </div>
  )
}

/** 刚生成、轮换的密钥：只显示这一次，附复制按钮和入站地址；关掉、离开这一节就没有了 */
function SecretOnce({ shown, onDismiss }: { shown: { name: string; secret: string; path: string | null }; onDismiss: () => void }) {
  const url = shown.path ? `${window.location.origin}${shown.path}` : null
  return (
    <div className="space-y-2 rounded-lg border px-3 py-2.5" style={{ borderColor: 'var(--st-waiting)' }} role="status"
         data-app-hook-secret={shown.name}>
      <div className="flex items-center gap-1.5 text-xs font-semibold">
        <KeyRound size={12} aria-hidden /> {T.hookSecretTitle(shown.name)}
      </div>
      <p className="text-2xs leading-relaxed" style={{ color: 'var(--st-waiting)' }}>{T.hookSecretOnce}</p>
      <div className="flex items-center gap-2">
        <code className="mono min-w-0 flex-1 break-all rounded border bg-bg px-2 py-1 text-2xs" data-app-hook-secret-value="">
          {shown.secret}
        </code>
        <CopyButton text={shown.secret} />
      </div>
      {url && (
        <div className="flex items-center gap-2 text-2xs">
          <span className="shrink-0 text-faint">{T.hookUrl}</span>
          <code className="mono min-w-0 flex-1 break-all" data-app-hook-url="">{url}</code>
          <CopyButton text={url} />
        </div>
      )}
      <p className="text-2xs text-faint">{T.hookSigning}</p>
      <div className="flex justify-end">
        <button type="button" className="btn btn-sm" onClick={onDismiss} data-app-hook-secret-dismiss="">{T.hookSecretDismiss}</button>
      </div>
    </div>
  )
}

export function NotifySection() {
  const { draft, setDraft } = useEditor()
  const [channels, setChannels] = useState<NotifyChannelRow[] | null>(null)
  useEffect(() => { api.notify.channels().then(({ items }) => setChannels(items)).catch(() => setChannels([])) }, [])
  const [registry, setRegistry] = useState<NotifyEventRow[] | null>(null)
  const [registryFailed, setRegistryFailed] = useState(false)
  useEffect(() => { api.notify.events().then(setRegistry).catch(() => setRegistryFailed(true)) }, [])
  const set = (notify: typeof draft.notify) => setDraft((s) => ({ ...s, notify }))
  // 应用能订阅哪些由服务端说（notify/service.EVENTS 推给「应用的订阅」这条路的）：「我提交审核的应用有结果」发给提交人、
  // 登记了还没推送的都不列。这条订阅里已经勾着的照样列出来（以前界面列过、其实推不到的也在内），看得到、能取消
  const appEvents = new Set((registry ?? []).filter((r) => r.scopes.includes('app')).map((r) => r.event))
  const known = Object.keys(NOTIFY_TEXT.eventLabel)
  const eventsOf = (subscribed: string[]) => [...known.filter((e) => appEvents.has(e) || subscribed.includes(e)),
                                               ...subscribed.filter((e) => !known.includes(e))]
  return (
    <div className="mx-auto max-w-3xl p-4" data-app-notify={draft.notify.length}>
      <SectionBar title={T.sections.notify} hint={T.notifyHint}>
        <button type="button" className="btn btn-sm" disabled={!channels?.length}
                onClick={() => set([...draft.notify, { channel: channels![0].id, events: ['task.opened', 'action.unknown'] }])}>
          <Plus size={11} aria-hidden /> {T.notifyAdd}
        </button>
      </SectionBar>
      {!channels ? <Skeleton rows={2} /> : !draft.notify.length ? <EmptyState title={T.notifyEmpty} body={NOTIFY_TEXT.emptyBody} /> : (
        <div className="space-y-2">
          {draft.notify.map((n, i) => (
            <div key={i} className="space-y-1.5 rounded-lg border bg-panel px-3 py-2" data-app-notify-sub={n.channel}>
              <div className="flex items-end gap-2">
                <Field label={T.notifyChannel} className="min-w-0 flex-1"
                       error={channels.some((c) => c.id === n.channel) ? undefined : T.notifyChannelMissing}>
                  {(p) => (
                    <select {...p} className="field" value={n.channel}
                            onChange={(e) => set(draft.notify.map((x, j) => (j === i ? { ...x, channel: e.target.value } : x)))}>
                      {channels.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                      {!channels.some((c) => c.id === n.channel) && <option value={n.channel} disabled>{n.channel}</option>}
                    </select>
                  )}
                </Field>
                <DeleteButton label={T.notifyRemove} onClick={() => set(draft.notify.filter((_, j) => j !== i))} />
              </div>
              <div className="grid grid-cols-3 gap-1">
                {eventsOf(n.events).map((ev) => (
                  <label key={ev} className="flex items-center gap-1.5 text-xs">
                    <input type="checkbox" className="accent-[var(--accent)]" checked={n.events.includes(ev)}
                           onChange={(e) => set(draft.notify.map((x, j) => (j === i
                             ? { ...x, events: e.target.checked ? [...x.events, ev] : x.events.filter((y) => y !== ev) } : x)))} />
                    {NOTIFY_TEXT.eventLabel[ev] ?? NOTIFY_TEXT.deliveryEventLabel[ev] ?? ev}
                  </label>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
      {registryFailed && <p className="mt-2 text-2xs" style={{ color: 'var(--st-failed)' }} role="alert">{NOTIFY_TEXT.eventsError}</p>}
    </div>
  )
}
