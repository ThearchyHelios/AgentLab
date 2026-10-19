import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { Field, Notice, SectionBar, Spinner, confirmDialog } from '../components/ui'
import { errorMessage } from '../lib/errors'
import { useLeaveGuard } from '../lib/leave'
import { CONCURRENCY_CLASSES, CONCURRENCY_TEXT } from '../lib/termsConcurrency'
import { atLeast, usePrincipal } from '../store/auth'

const T = CONCURRENCY_TEXT
type Klass = (typeof CONCURRENCY_CLASSES)[number]
interface Draft { total: string; reserve: string; limits: Record<Klass, string>; priorities: Record<Klass, string> }
type Now = Partial<Record<Klass, { running?: number; waiting?: number }>>

const draftOf = (group: Record<string, any> | undefined): Draft => ({
  total: group?.max_total == null ? '' : String(group.max_total),
  reserve: group?.interactive_reserve == null ? '' : String(group.interactive_reserve),
  limits: Object.fromEntries(CONCURRENCY_CLASSES.map((k) => [k, String(group?.classes?.[k]?.limit ?? '')])) as Record<Klass, string>,
  priorities: Object.fromEntries(CONCURRENCY_CLASSES.map((k) => [k, String(group?.classes?.[k]?.priority ?? '')])) as Record<Klass, string>,
})

const changedCount = (a: Draft, b: Draft): number =>
  (a.total.trim() !== b.total ? 1 : 0)
  + (a.reserve.trim() !== b.reserve ? 1 : 0)
  + CONCURRENCY_CLASSES.filter((k) => a.limits[k].trim() !== b.limits[k]).length
  + CONCURRENCY_CLASSES.filter((k) => a.priorities[k].trim() !== b.priorities[k]).length

/**
 * 设置页「偏好设置」里「运行并发」一节（docs/design/orchestration-round §5 W2-b）：全站总上限，交互、应用、定时、系统四类各自的
 * 上限和优先级，旁边写各类现在在跑、在等几个（/system 的 cluster.runs）。
 *
 * 自己读、自己存（PUT /settings 只带 concurrency 一组），带自己的离开前确认，同评测那一节的做法。搭建者只读、管理员写
 */
export function ConcurrencySettingsSection() {
  const me = usePrincipal()
  const admin = atLeast(me, 'admin')
  const [saved, setSaved] = useState<Draft | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [now, setNow] = useState<Now | null>(null)
  const [loadError, setLoadError] = useState<unknown>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [flash, setFlash] = useState(false)

  const loadNow = () => api.system().then((s) => setNow(s?.cluster?.runs?.classes ?? null), () => setNow(null))
  useEffect(() => {
    api.settings.get().then((v) => {
      setSaved(draftOf(v?.concurrency))
      setLoadError(null)
    }, setLoadError)
    void loadNow()
  }, [])

  const cur = draft ?? saved
  const changed = !!(cur && saved) && changedCount(cur, saved) > 0
  useLeaveGuard(changed && !saving, () => confirmDialog({
    title: T.leaveTitle(cur && saved ? changedCount(cur, saved) : 0),
    consequences: T.leaveConsequences, confirmLabel: T.discard, danger: true,
  }))

  const save = async () => {
    if (!cur || !changed) return
    setSaving(true)
    setError(null)
    try {
      const value = {
        max_total: Number(cur.total.trim()),
        interactive_reserve: Number(cur.reserve.trim()),
        classes: Object.fromEntries(CONCURRENCY_CLASSES.map((k) => [k, {
          limit: Number(cur.limits[k].trim()), priority: Number(cur.priorities[k].trim()),
        }])),
      }
      const out = await api.settings.put({ concurrency: value })
      setSaved(draftOf(out?.concurrency ?? value))
      setDraft(null)
      setFlash(true)
      setTimeout(() => setFlash(false), 1600)
      void loadNow()
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      setSaving(false)
    }
  }

  if (loadError || !cur) return null
  // 借用的上限按存着的算（服务端生效的那份），改到一半的输入不跟着跳
  const borrow = saved && saved.total !== '' && saved.reserve !== '' ? Math.max(0, Number(saved.total) - Number(saved.reserve)) : null
  const edit = (patch: Partial<Draft>) => {
    setError(null)
    setDraft({ ...cur, ...patch, limits: { ...cur.limits, ...(patch.limits ?? {}) },
               priorities: { ...cur.priorities, ...(patch.priorities ?? {}) } })
  }
  return (
    <section data-concurrency-settings="">
      <SectionBar title={T.title} hint={T.hint} />
      <fieldset disabled={!admin || saving} className="m-0 min-w-0 space-y-3 border-0 p-0">
        <div className="grid grid-cols-2 gap-3 max-sm:grid-cols-1">
          <Field label={T.maxTotal} htmlFor="pref-concurrency-total">
            {(p) => (
              <input {...p} className="field tnum max-w-40" inputMode="numeric" value={cur.total}
                     onChange={(e) => edit({ total: e.target.value })} data-concurrency-setting="max_total" />
            )}
          </Field>
          <Field label={T.reserve} htmlFor="pref-concurrency-reserve" hint={T.reserveHint}>
            {(p) => (
              <input {...p} className="field tnum max-w-40" inputMode="numeric" value={cur.reserve}
                     onChange={(e) => edit({ reserve: e.target.value })} data-concurrency-setting="interactive_reserve" />
            )}
          </Field>
        </div>
        {borrow != null && <p className="text-2xs text-dim" data-concurrency-borrow="">{T.borrow(borrow)}</p>}
        <div className="space-y-2">
          {CONCURRENCY_CLASSES.map((k) => {
            const info = T.classes[k]
            const live = now?.[k]
            return (
              <div key={k} className="grid grid-cols-[8rem_1fr_1fr] items-end gap-3 max-sm:grid-cols-1" data-concurrency-class={k}>
                <div className="text-xs">
                  <div>{info.label}</div>
                  <div className="text-2xs leading-relaxed text-faint">{info.hint}</div>
                  {live && <div className="text-2xs text-dim" data-concurrency-now={k}>{T.now(live.running ?? 0, live.waiting ?? 0)}</div>}
                </div>
                <Field label={T.limit} htmlFor={`pref-concurrency-${k}-limit`} hint={k === 'app' ? T.limitHint : undefined}>
                  {(p) => (
                    <input {...p} className="field tnum max-w-32" inputMode="numeric" value={cur.limits[k]}
                           onChange={(e) => edit({ limits: { [k]: e.target.value } as Record<Klass, string> })}
                           data-concurrency-setting={`${k}.limit`} />
                  )}
                </Field>
                <Field label={T.priority} htmlFor={`pref-concurrency-${k}-priority`} hint={k === 'interactive' ? T.priorityHint : undefined}>
                  {(p) => (
                    <input {...p} className="field tnum max-w-32" inputMode="numeric" value={cur.priorities[k]}
                           onChange={(e) => edit({ priorities: { [k]: e.target.value } as Record<Klass, string> })}
                           data-concurrency-setting={`${k}.priority`} />
                  )}
                </Field>
              </div>
            )
          })}
        </div>
      </fieldset>
      {!admin && <p className="mt-2 text-2xs text-faint">{T.readOnly}</p>}
      {error && <Notice tone="err" className="mt-2" attr={{ 'data-concurrency-settings-error': '' }}>{error}</Notice>}
      {admin && (changed || flash) && (
        <div className="mt-3 flex items-center justify-end gap-2" data-concurrency-settings-bar="">
          {flash && !changed && <span className="text-2xs text-[var(--st-done)]">{T.saved}</span>}
          {changed && (
            <>
              <button type="button" className="btn btn-sm" onClick={() => { setDraft(null); setError(null) }} disabled={saving}>{T.discard}</button>
              <button type="button" className="btn btn-primary btn-sm" onClick={() => void save()} disabled={saving} data-concurrency-settings-save="">
                {saving && <Spinner size={11} />} {saving ? T.saving : T.save}
              </button>
            </>
          )}
        </div>
      )}
    </section>
  )
}
