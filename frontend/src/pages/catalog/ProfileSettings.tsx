import { useState } from 'react'
import { ShieldAlert } from 'lucide-react'
import { ApiError, api } from '../../api/client'
import type { CatalogProfileNumberKey } from '../../types'
import { Field, Modal, Spinner, toast } from '../../components/ui'
import { PROFILE_FIELD_HINT, PROFILE_FIELD_LABEL, PROFILE_FIELD_UNIT, PROFILE_TEXT as PT } from '../../lib/terms'
import {
  PROFILE_DEFAULTS, PROFILE_LIMITS, PROFILE_NUMBER_KEYS, hasProfileOption, profileFieldOfRejection, profileFormOf, profileOptionOf,
  profileProblems, profileSettingsOfForm, sameProfileForm, withProfileOption,
} from './profile'
import type { ProfileForm } from './profile'

// ===========================================================================
// 数据剖析的设置：开关、五项预算、风险说明。数据源编辑框里是其中一段；导入表格的源没有编辑框，数据目录页在
// 剖析被拒（未开启）时也要就地打开它，所以另有一个只改这一项的弹窗（options 的其余键原样带上）。
// 剖析会对业务库发查询，开关默认关着；开启后五项预算才展开，风险说明写的是此刻填的数。
// ===========================================================================

/** 保存被拒时的落点：某一项，或者整段（认不出是哪一项） */
export type ProfileErrorKey = 'enabled' | CatalogProfileNumberKey | 'section'

export function ProfileSettingsSection({ idPrefix, form, onChange, serverError }: {
  /** 输入框 id 的前缀：报错时按 `${idPrefix}-${键}` 聚焦 */
  idPrefix: string
  form: ProfileForm
  onChange: (next: ProfileForm) => void
  /** 服务端拒收（422）的那一句，落在对应的项上 */
  serverError?: { key: ProfileErrorKey; message: string } | null
}) {
  const problems = profileProblems(form)
  const errorOf = (k: CatalogProfileNumberKey) => (serverError?.key === k ? serverError.message : problems[k])
  // 开关关着时预算收起；但填错的项（或服务端点了名的项）要看得见，不然保存被拦下却找不到原因
  const open = form.enabled || Object.keys(problems).length > 0
    || (!!serverError && serverError.key !== 'enabled' && serverError.key !== 'section')
  const s = profileSettingsOfForm(form)
  const setValue = (k: CatalogProfileNumberKey, v: string) => onChange({ ...form, values: { ...form.values, [k]: v } })
  const switchError = serverError && (serverError.key === 'enabled' || serverError.key === 'section') ? serverError.message : null

  return (
    <fieldset className="rounded-lg border" data-field="catalog_profile" data-profile-settings={form.enabled ? 'on' : 'off'}>
      <legend className="sr-only">{PT.section}</legend>
      <label className="flex cursor-pointer items-start gap-2 p-2.5">
        <input id={`${idPrefix}-enabled`} type="checkbox" className="mt-0.5" checked={form.enabled}
               aria-describedby={`${idPrefix}-enabled-hint`} aria-invalid={switchError ? true : undefined}
               onChange={(e) => onChange({ ...form, enabled: e.target.checked })} data-profile-enabled="" />
        <span className="min-w-0 text-xs">
          {PT.section}
          <span id={`${idPrefix}-enabled-hint`} className="ml-1.5 text-2xs leading-relaxed text-faint">{PT.enableHint}</span>
        </span>
      </label>
      {switchError && (
        <p className="px-2.5 pb-2 text-2xs leading-relaxed text-[var(--err)]" role="alert" data-profile-error="section">{switchError}</p>
      )}
      {open && (
        <div className="space-y-3 border-t px-2.5 py-2.5">
          {form.enabled && (
            <p className="flex items-start gap-1.5 rounded-md px-2 py-1.5 text-2xs leading-relaxed text-dim" data-profile-risk=""
               style={{ background: 'var(--st-waiting-soft)' }}>
              <ShieldAlert size={12} className="mt-px shrink-0" style={{ color: 'var(--st-waiting)' }} aria-hidden />
              <span>{PT.risk(s.max_queries, s.query_timeout_s, s.max_total_s)}</span>
            </p>
          )}
          <div className="grid gap-3 sm:grid-cols-2">
            {PROFILE_NUMBER_KEYS.map((k) => {
              const { min, max } = PROFILE_LIMITS[k]
              const unit = PROFILE_FIELD_UNIT[k]
              const err = errorOf(k)
              const id = `${idPrefix}-${k}`
              const hint = `${PT.range(min, max, unit, PROFILE_DEFAULTS[k])}${k === 'max_scan_rows' ? `，${PT.scanZero}` : ''}。${PROFILE_FIELD_HINT[k]}`
              return (
                <Field key={k} htmlFor={id} label={PROFILE_FIELD_LABEL[k]} hint={hint} error={err}>
                  {(p) => (
                    <div className="relative" data-profile-field={k}>
                      <input {...p} className="field tnum pr-8" inputMode={PROFILE_LIMITS[k].integer ? 'numeric' : 'decimal'}
                             autoComplete="off" spellCheck={false} value={form.values[k]}
                             placeholder={PT.placeholder(PROFILE_DEFAULTS[k])}
                             style={err ? { borderColor: 'var(--err)' } : undefined}
                             onChange={(e) => setValue(k, e.target.value)} />
                      <span className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-2xs text-faint" aria-hidden>
                        {unit}
                      </span>
                    </div>
                  )}
                </Field>
              )
            })}
          </div>
        </div>
      )}
    </fieldset>
  )
}

/** 聚焦保存被拒的那一项（预算收着时先等它展开） */
export function focusProfileField(idPrefix: string, key: ProfileErrorKey) {
  const id = key === 'section' ? `${idPrefix}-enabled` : `${idPrefix}-${key}`
  requestAnimationFrame(() => requestAnimationFrame(() => {
    const el = document.getElementById(id)
    el?.scrollIntoView({ block: 'nearest' })
    el?.focus({ preventScroll: true })
  }))
}

/**
 * 只改数据剖析设置的弹窗：导入表格的源（没有编辑框）、数据目录页里剖析被拒时用。
 * 保存时 options 的其余键（遮罩列、schema……）原样带上，只换剖析设置这一项
 */
export function ProfileSettingsDialog({ row, onClose, onSaved }: {
  row: { id: string; name: string; options?: Record<string, unknown> | null }
  onClose: () => void
  onSaved: (row: any) => void
}) {
  const [initial] = useState(() => profileFormOf(row.options))
  const [form, setForm] = useState<ProfileForm>(initial)
  const [saving, setSaving] = useState(false)
  const [serverError, setServerError] = useState<{ key: ProfileErrorKey; message: string } | null>(null)
  const dirty = !sameProfileForm(form, initial)
  const problems = Object.keys(profileProblems(form)).length
  const idPrefix = `profile-${row.id}`

  const save = async () => {
    if (!dirty || problems) return
    setSaving(true)
    setServerError(null)
    try {
      const options = withProfileOption(row.options, profileOptionOf(form, hasProfileOption(row.options)))
      const next = await api.datasources.update(row.id, { options })
      onSaved(next)
      toast.ok(form.enabled === initial.enabled ? PT.saved(row.name) : form.enabled ? PT.savedOn(row.name) : PT.savedOff(row.name))
    } catch (e) {
      const key = e instanceof ApiError && e.status === 422 ? profileFieldOfRejection(e.message) : null
      if (key) {
        setServerError({ key, message: e instanceof ApiError ? e.message : '' })
        focusProfileField(idPrefix, key)
      } else {
        toast.error(e)
      }
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal open onClose={onClose} dirty={dirty} width={560} title={PT.dialogTitle(row.name)}
           footer={(
             <>
               {problems > 0 && <span className="mr-auto text-2xs text-[var(--err)]" data-profile-invalid={problems}>{PT.invalid(problems)}</span>}
               <button type="button" className="btn" onClick={onClose}>取消</button>
               <button type="button" className="btn btn-primary" disabled={saving || !dirty || problems > 0} onClick={() => void save()}
                       data-profile-save="">
                 {saving ? <Spinner size={11} /> : null} 保存
               </button>
             </>
           )}>
      <div className="space-y-3" data-profile-dialog={row.id}>
        <p className="text-2xs leading-relaxed text-faint">{PT.dialogBody}</p>
        <ProfileSettingsSection idPrefix={idPrefix} form={form} serverError={serverError}
                                onChange={(next) => { setForm(next); setServerError(null) }} />
      </div>
    </Modal>
  )
}
