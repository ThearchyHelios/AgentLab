import { useEffect, useState } from 'react'
import { Send } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../../api/client'
import { Field, Notice, Spinner, isComposing, toast, useRadioGroup } from '../../components/ui'
import { errorMessage } from '../../lib/errors'
import { SCHEDULE_TEXT as T } from '../../lib/terms'
import type { ScheduleEvent, ScheduleWebhook, ScheduleWebhookTest } from '../../types'
import { SCHEDULE_EVENTS, WEBHOOK_FORMATS, isHttpUrl, webhookTestText, type FormField, type WebhookForm } from './model'

/**
 * Webhook 通知：格式、地址、签名密钥、订阅事件、发送测试消息，外加通知里运行链接用的服务访问地址。
 *
 * 地址和密钥保存之后服务端不再返回明文：地址只显示主机和末 4 位（url_hint），可以替换或删除；密钥只说「已设置」，
 * 可以替换或清除。没点替换时请求里不带它们，改别的字段不会把地址清掉。测试消息用的是已保存的设置，
 * 页面上改了还没保存时不让测，免得测的不是眼前这份
 */
export function WebhookFields({ value, onChange, saved, problems, scheduleId, unsaved }: {
  value: WebhookForm
  onChange: (next: WebhookForm) => void
  /** 已保存的 Webhook；新建、原来没有时为 null */
  saved: ScheduleWebhook | null
  problems: Partial<Record<FormField, string>>
  /** 已保存的定时任务 id；新建时为 null，不能测试 */
  scheduleId: string | null
  /** Webhook 这一块有没保存的改动 */
  unsaved: boolean
}) {
  const w = value
  const set = (patch: Partial<WebhookForm>) => onChange({ ...w, ...patch })
  const radio = useRadioGroup(WEBHOOK_FORMATS, w.format, (format) => set({ format }))
  const [urlTouched, setUrlTouched] = useState(false)
  const [test, setTest] = useState<{ busy: boolean; result?: ScheduleWebhookTest; error?: unknown } | null>(null)
  const askUrl = !saved || w.replaceUrl
  const urlError = askUrl && (urlTouched || w.url.trim()) ? problems.url : undefined
  const testBlocked = !scheduleId || !saved ? T.testNeedSave : unsaved ? T.testDirty : null

  // 设置一改，上一次的测试结果就不代表眼前这份了
  useEffect(() => { setTest((t) => (t?.busy ? t : null)) }, [unsaved])

  const sendTest = async () => {
    if (!scheduleId || testBlocked) return
    setTest({ busy: true })
    try {
      setTest({ busy: false, result: await api.schedules.testWebhook(scheduleId) })
    } catch (e) {
      setTest({ busy: false, error: e })
    }
  }

  return (
    <section className="rounded-lg border bg-panel p-3.5" aria-labelledby="schedule-webhook-title" data-webhook-section="">
      <h2 id="schedule-webhook-title" className="mb-2.5 text-xs font-semibold">{T.webhook}</h2>
      <label className="flex items-start gap-2 text-xs">
        <input type="checkbox" className="mt-0.5" checked={w.on} onChange={(e) => set({ on: e.target.checked })} data-webhook-on="" />
        <span>{T.webhookOn}</span>
      </label>

      {!w.on && saved && (
        <Notice tone="warn" className="mt-2.5" attr={{ 'data-webhook-removing': '' }}>
          <span>{T.webhookRemoving}</span>
          <button type="button" className="btn btn-xs ml-2" onClick={() => set({ on: true })}>{T.webhookUndoRemove}</button>
        </Notice>
      )}

      {w.on && (
        <div className="mt-3 space-y-3">
          <div>
            <div className="label" id="schedule-webhook-format">{T.webhookFormat}</div>
            <div role="radiogroup" aria-labelledby="schedule-webhook-format" className="flex flex-wrap gap-1">
              {WEBHOOK_FORMATS.map((f) => (
                <button key={f} type="button" {...radio(f)} onClick={() => set({ format: f })} data-webhook-format={f}
                        className={clsx('rounded-md border px-2.5 py-1 text-xs transition-colors',
                          w.format === f ? 'border-[var(--accent)] bg-accent-soft text-fg' : 'text-dim hover:bg-hover')}>
                  {T.format[f]}
                </button>
              ))}
            </div>
          </div>

          {askUrl ? (
            <Field label={T.url} htmlFor="schedule-webhook-url" error={urlError} hint={T.formatHint[w.format]}>
              <div className="flex flex-wrap gap-1.5">
                <input id="schedule-webhook-url" type="url" inputMode="url" className="field mono min-w-0 flex-1 basis-52"
                       value={w.url} placeholder={T.urlPlaceholder[w.format]} autoComplete="off" spellCheck={false}
                       aria-invalid={!!urlError || undefined}
                       aria-describedby={urlError ? 'schedule-webhook-url-error' : 'schedule-webhook-url-hint'}
                       onChange={(e) => set({ url: e.target.value })} onBlur={() => setUrlTouched(true)} data-webhook-url="" />
                {saved && (
                  <button type="button" className="btn btn-sm" onClick={() => set({ replaceUrl: false, url: '' })}>{T.urlKeep}</button>
                )}
              </div>
            </Field>
          ) : (
            <div>
              <div className="label">{T.urlSaved}</div>
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="mono min-w-0 truncate rounded-md border bg-elev px-2 py-1 text-xs text-dim" title={T.urlSavedHint}
                      data-webhook-url-hint="">
                  {saved?.url_hint}
                </span>
                <button type="button" className="btn btn-sm" onClick={() => set({ replaceUrl: true, url: '' })} data-webhook-replace="">
                  {T.urlReplace}
                </button>
                <button type="button" className="btn btn-sm btn-danger" onClick={() => set({ on: false })} data-webhook-remove="">
                  {T.webhookRemove}
                </button>
              </div>
              <div className="mt-1 text-2xs leading-relaxed text-faint">{T.urlSavedHint}</div>
            </div>
          )}

          <SecretField value={w} saved={saved} onChange={set} error={problems.secret} />

          <fieldset>
            <legend className="label">{T.events}</legend>
            <div className="flex flex-wrap gap-x-4 gap-y-1.5">
              {SCHEDULE_EVENTS.map((ev) => (
                <label key={ev} className="flex items-center gap-1.5 text-xs">
                  <input type="checkbox" checked={w.events.includes(ev)} data-webhook-event={ev}
                         onChange={(e) => set({ events: toggled(w.events, ev, e.target.checked) })} />
                  {T.event[ev]}
                </label>
              ))}
            </div>
            {problems.events && <div className="mt-1 text-2xs text-[var(--err)]" role="alert">{problems.events}</div>}
          </fieldset>

          <div className="flex flex-wrap items-center gap-2">
            <button type="button" className="btn btn-sm" onClick={() => void sendTest()} disabled={!!testBlocked || test?.busy}
                    title={testBlocked ?? undefined} data-webhook-test="">
              {test?.busy ? <Spinner size={11} /> : <Send size={11} aria-hidden />} {test?.busy ? T.testing : T.test}
            </button>
            {testBlocked && <span className="text-2xs text-faint">{testBlocked}</span>}
          </div>
          {test && !test.busy && (
            <Notice tone={test.result?.ok ? 'info' : 'err'} attr={{ 'data-webhook-test-result': test.result?.ok ? 'ok' : 'failed' }}>
              {test.result
                ? webhookTestText(test.result)
                : T.testResult(T.testFailed, null, errorMessage(test.error))}
            </Notice>
          )}

          <LinkBase />
        </div>
      )}
    </section>
  )
}

const toggled = (events: ScheduleEvent[], ev: ScheduleEvent, on: boolean): ScheduleEvent[] =>
  on ? [...events.filter((x) => x !== ev), ev] : events.filter((x) => x !== ev)

/** 签名密钥：没设置过时是一个密码框；设置过时只说「已设置」，可以替换或清除 */
function SecretField({ value: w, saved, onChange, error }: {
  value: WebhookForm; saved: ScheduleWebhook | null; onChange: (patch: Partial<WebhookForm>) => void; error?: string
}) {
  const hasSecret = !!saved?.has_secret
  if (hasSecret && w.secretMode === 'keep') {
    return (
      <div>
        <div className="label">{T.secret}</div>
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="chip" data-webhook-secret-set="">{T.secretSet}</span>
          <button type="button" className="btn btn-sm" onClick={() => onChange({ secretMode: 'replace', secret: '' })}>{T.secretReplace}</button>
          <button type="button" className="btn btn-sm" onClick={() => onChange({ secretMode: 'clear', secret: '' })}>{T.secretClear}</button>
        </div>
      </div>
    )
  }
  if (hasSecret && w.secretMode === 'clear') {
    return (
      <div>
        <div className="label">{T.secret}</div>
        <div className="flex flex-wrap items-center gap-2 text-xs text-dim">
          <span>{T.secretClearing}</span>
          <button type="button" className="btn btn-xs" onClick={() => onChange({ secretMode: 'keep' })}>{T.secretUndo}</button>
        </div>
      </div>
    )
  }
  return (
    <Field label={hasSecret ? T.secretNew : T.secret} htmlFor="schedule-webhook-secret" error={error} hint={T.secretHint[w.format]}>
      <div className="flex flex-wrap gap-1.5">
        <input id="schedule-webhook-secret" type="password" className="field mono min-w-0 flex-1 basis-52" value={w.secret}
               autoComplete="new-password" spellCheck={false} aria-invalid={!!error || undefined}
               aria-describedby={error ? 'schedule-webhook-secret-error' : 'schedule-webhook-secret-hint'}
               onChange={(e) => onChange({ secret: e.target.value })} data-webhook-secret="" />
        {hasSecret && (
          <button type="button" className="btn btn-sm" onClick={() => onChange({ secretMode: 'keep', secret: '' })}>{T.secretUndo}</button>
        )}
      </div>
    </Field>
  )
}

/**
 * 通知里运行链接用的服务访问地址：设置里 notify 一组的 public_base_url，所有定时任务共用。这里给出当前值和修改入口，
 * 写的时候先重读一遍 notify 组、只换这一项，不把同组别的设置冲掉
 */
function LinkBase() {
  const [value, setValue] = useState<string | null | undefined>(undefined)
  const [failed, setFailed] = useState(false)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let alive = true
    api.settings.get()
      .then((s) => { if (alive) setValue(typeof s?.notify?.public_base_url === 'string' ? s.notify.public_base_url : null) })
      .catch(() => { if (alive) setFailed(true) })
    return () => { alive = false }
  }, [])

  const cleaned = draft.trim().replace(/\/+$/, '')
  const invalid = !!cleaned && !isHttpUrl(cleaned)
  const save = async () => {
    if (invalid || busy) return
    setBusy(true)
    try {
      const current = await api.settings.get()
      const notify = { ...(current?.notify ?? {}), public_base_url: cleaned || null }
      const out = await api.settings.put({ notify })
      setValue(typeof out?.notify?.public_base_url === 'string' ? out.notify.public_base_url : notify.public_base_url)
      setFailed(false)
      setEditing(false)
      toast.ok(cleaned ? T.linkBaseSaved : T.linkBaseCleared)
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="rounded-md border bg-elev px-2.5 py-2" data-link-base="">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-2xs">
        <span className="font-medium text-dim">{T.linkBase}</span>
        <span className={clsx('min-w-0 break-all', value ? 'mono text-fg' : 'text-faint')}>
          {failed ? T.linkBaseFailed : value === undefined ? <Spinner size={10} /> : value ? T.linkBaseValue(value) : T.linkBaseUnset}
        </span>
        {!editing && value !== undefined && (
          <button type="button" className="btn btn-xs" onClick={() => { setDraft(value ?? ''); setEditing(true) }} data-link-base-edit="">
            {value ? T.linkBaseEdit : T.linkBaseSet}
          </button>
        )}
      </div>
      {editing && (
        <div className="mt-2">
          <Field label={T.linkBaseLabel} htmlFor="schedule-link-base" hint={T.linkBaseHint} error={invalid ? T.linkBaseInvalid : undefined}>
            <div className="flex flex-wrap gap-1.5">
              <input id="schedule-link-base" type="url" inputMode="url" className="field mono min-w-0 flex-1 basis-48" value={draft}
                     placeholder={T.linkBasePlaceholder} autoComplete="off" spellCheck={false} aria-invalid={invalid || undefined}
                     aria-describedby={invalid ? 'schedule-link-base-error' : 'schedule-link-base-hint'}
                     onChange={(e) => setDraft(e.target.value)}
                     onKeyDown={(e) => {
                       // 在表单里：回车存这一项，不提交整张表单（输入法组字时的回车不算）
                       if (e.key === 'Enter' && !isComposing(e)) { e.preventDefault(); void save() }
                     }}
                     data-link-base-input="" />
              <button type="button" className="btn btn-sm btn-primary" onClick={() => void save()} disabled={invalid || busy} data-link-base-save="">
                {busy && <Spinner size={11} />} {T.save}
              </button>
              <button type="button" className="btn btn-sm" onClick={() => setEditing(false)}>{T.cancel}</button>
            </div>
          </Field>
        </div>
      )}
    </div>
  )
}
