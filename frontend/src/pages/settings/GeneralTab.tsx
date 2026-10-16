import { useEffect, useState } from 'react'
import { Check, Lock } from 'lucide-react'
import { api } from '../../api/client'
import { ErrorState, Field, SectionBar, Skeleton, Spinner, confirmDialog, isComposing, toast } from '../../components/ui'
import { useLeaveGuard } from '../../lib/leave'
import { cleanPublicUrl, isPublicUrl, publicAddressOf } from '../../lib/publicUrl'
import { GENERAL_SETTING_TEXT as T } from '../../lib/terms'
import { useAtLeast } from '../../store/auth'

/*
 * 设置 → 通用：全站共用的部署设置。现在只有「对外访问地址」一项：服务端全站只认一个对外地址（core/public_url.py），
 * 环境变量 AGENTLAB_PUBLIC_URL 优先，其次这里填的（存在 notify 一组）。以前两处各管各的，设置里这一项只在定时任务的
 * Webhook 一节里改得到，那里现在是它的快捷入口。
 *
 * 环境变量设了：只显示它，写明「由环境变量指定」，不给输入框（GET /api/settings 的 public_url 一组）。只有管理员能改
 * （整个设置接口的改动都要管理员），搭建者只看。保存时先重读一遍 notify 组、只换这一项，不把同组别的设置冲掉
 */
export function GeneralTab() {
  const admin = useAtLeast('admin')
  const [settings, setSettings] = useState<Record<string, any> | null>(null)
  const [loadError, setLoadError] = useState<unknown>(null)
  // null：没在改，输入框显示存着的值
  const [draft, setDraft] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = () => api.settings.get().then((s) => { setSettings(s); setLoadError(null) }, setLoadError)
  useEffect(() => { void load() }, [])

  const address = publicAddressOf(settings)
  const editable = admin && !address.env
  const saved = address.value ?? ''
  const cleaned = cleanPublicUrl(draft ?? saved)
  const invalid = !!cleaned && !isPublicUrl(cleaned)
  const dirty = settings != null && editable && cleaned !== saved
  useLeaveGuard(dirty, () => confirmDialog({
    title: T.leaveTitle, body: T.leaveBody, confirmLabel: T.leaveConfirm, cancelLabel: T.leaveCancel, danger: true,
  }))

  const save = async () => {
    if (!dirty || invalid || busy) return
    setBusy(true)
    try {
      const current = await api.settings.get()
      const out = await api.settings.put({ notify: { ...(current?.notify ?? {}), public_base_url: cleaned || null } })
      setSettings(out)
      setDraft(null)
      toast.ok(cleaned ? T.saved : T.cleared)
    } catch (e) {
      toast.error(e)
      // 环境变量这期间被设上了（服务端回 409）这类：按服务端现在的样子重读
      void load()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="max-w-2xl space-y-6 pb-20">
      <section data-general-public-url="">
        <SectionBar title={T.publicUrl} hint={T.publicUrlHint} />
        {!settings ? (
          loadError ? <ErrorState error={loadError} onRetry={() => void load()} /> : <Skeleton rows={2} height={14} />
        ) : editable ? (
          <Field label={T.address} htmlFor="general-public-url" hint={T.addressHint} error={invalid ? T.invalid : undefined}>
            <div className="flex flex-wrap gap-1.5">
              <input id="general-public-url" type="url" inputMode="url" className="field mono min-w-0 flex-1 basis-60"
                     value={draft ?? saved} placeholder={T.placeholder} autoComplete="off" spellCheck={false}
                     aria-invalid={invalid || undefined}
                     aria-describedby={invalid ? 'general-public-url-error' : 'general-public-url-hint'}
                     onChange={(e) => setDraft(e.target.value)}
                     onKeyDown={(e) => { if (e.key === 'Enter' && !isComposing(e)) { e.preventDefault(); void save() } }}
                     data-public-url-input="" />
              <button type="button" className="btn btn-sm btn-primary" onClick={() => void save()}
                      disabled={!dirty || invalid || busy} data-public-url-save="">
                {busy ? <Spinner size={11} /> : <Check size={12} aria-hidden />} {T.save}
              </button>
            </div>
          </Field>
        ) : (
          <div className="rounded-md border bg-elev px-2.5 py-2" data-public-url-readonly={address.env ? 'env' : 'role'}>
            <div className={address.value ? 'mono break-all text-xs text-fg' : 'text-xs text-faint'} data-public-url-value="">
              {address.value ?? T.unset}
            </div>
            <p className="mt-1 flex items-start gap-1 text-2xs leading-relaxed text-faint">
              <Lock size={11} className="mt-0.5 shrink-0" aria-hidden />
              <span>{address.env ? `${T.envLocked}。${T.envLockedHint}` : T.readOnly}</span>
            </p>
          </div>
        )}
      </section>
    </div>
  )
}
