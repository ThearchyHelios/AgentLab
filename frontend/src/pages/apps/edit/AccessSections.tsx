import { useCallback, useEffect, useState } from 'react'
import { Plus } from 'lucide-react'
import { api } from '../../../api/client'
import { DeleteButton, EmptyState, Field, SectionBar, Skeleton, Spinner, toast } from '../../../components/ui'
import { PrincipalPicker, type Principal as Picked } from '../../../components/PrincipalPicker'
import { APP_EDITOR_TEXT as T, GRANT_ROLE_LABEL, NOTIFY_TEXT } from '../../../lib/terms'
import { useLoginOn } from '../../../store/auth'
import type { AppGrant, GrantRole, NotifyChannelRow } from '../../../typesApp'
import { applyAppOp } from './appOps'
import { useEditor } from './context'

/*
 * 岗位与授权、钩子、通知订阅。
 *
 * 岗位：图里人工节点写的岗位名（assignee 的岗位）在这里绑定到人或组；绑定不进 spec，存在草稿的 bindings 里，发布时
 * 进应用版本——spec 可以导出，绑定是这一个部署自己的。人员变动只改组成员，不发新版本。
 * 授权：use / observe / manage，即时生效、写审计，不需要发新版本（A1）。关登录时所有人都是管理员，授权开登录后才生效
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
            <div key={role} data-app-grant-role={role}>
              <PrincipalPicker label={GRANT_ROLE_LABEL[role]} allowEveryone={role !== 'manage'}
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

export function HooksSection() {
  const { draft, setDraft } = useEditor()
  const set = (hooks: typeof draft.hooks) => setDraft((s) => ({ ...s, hooks }))
  return (
    <div className="mx-auto max-w-3xl p-4" data-app-hooks={draft.hooks.length}>
      <SectionBar title={T.sections.hooks} hint={T.hooksHint}>
        <button type="button" className="btn btn-sm" data-app-hook-add=""
                onClick={() => set([...draft.hooks, { name: `hook${draft.hooks.length + 1}`, kind: 'signal', entry: null }])}>
          <Plus size={11} aria-hidden /> {T.hookAdd}
        </button>
      </SectionBar>
      {!draft.hooks.length ? <EmptyState title={T.hooksEmpty} /> : (
        <div className="space-y-2">
          {draft.hooks.map((h, i) => (
            <div key={i} className="grid grid-cols-[1fr_140px_1fr_auto] items-end gap-2 rounded-lg border bg-panel px-3 py-2" data-app-hook={h.name}>
              <Field label={T.hookName}>
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
          ))}
        </div>
      )}
    </div>
  )
}

export function NotifySection() {
  const { draft, setDraft } = useEditor()
  const [channels, setChannels] = useState<NotifyChannelRow[] | null>(null)
  useEffect(() => { api.notify.channels().then(({ items }) => setChannels(items)).catch(() => setChannels([])) }, [])
  const set = (notify: typeof draft.notify) => setDraft((s) => ({ ...s, notify }))
  const events = Object.keys(NOTIFY_TEXT.eventLabel)
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
                {events.map((ev) => (
                  <label key={ev} className="flex items-center gap-1.5 text-xs">
                    <input type="checkbox" className="accent-[var(--accent)]" checked={n.events.includes(ev)}
                           onChange={(e) => set(draft.notify.map((x, j) => (j === i
                             ? { ...x, events: e.target.checked ? [...x.events, ev] : x.events.filter((y) => y !== ev) } : x)))} />
                    {NOTIFY_TEXT.eventLabel[ev]}
                  </label>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
