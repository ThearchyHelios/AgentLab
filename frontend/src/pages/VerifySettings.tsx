import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { AlertTriangle } from 'lucide-react'
import { get } from '../api/http'
import { modelOptions, providerOfModel, useCatalog } from '../store/catalog'
import { Field, SectionBar } from '../components/ui'
import { VERIFY_SETTING_TEXT } from '../lib/terms'
import type { VerifySpend } from '../types'

/*
 * 设置页「偏好设置」里「核查」一节：双盲复算、反方审稿、审批质询共用的核查模型、各自的开关和上限、每日金额上限
 * （服务端 api/settings.py 的 verify 一组）。
 *
 * 草稿由 PrefsTab 持有、和其余设置共用一条保存条（这一节只画、只报改动），存的时候整组写 verify：服务端对没给的项
 * 回落到默认值，只发改过的那一项会把别的项冲回默认。字段名就是服务端 VERIFY_SETTING_LABEL 的叫法，422 的报错
 * 写的也是它，人找得到是哪一项
 */

const T = VERIFY_SETTING_TEXT
const S = VERIFY_SETTING_TEXT.section

type Limit = string | null
/** 草稿：开关是布尔，相对误差是正在填的字符串，上限是字符串或 null（不限）。键就是服务端叫法表里的键 */
export interface VerifyDraft {
  provider: string
  model: string
  'dual_check.enabled': boolean
  'dual_check.tolerance': string
  'dual_check.max_queries': Limit
  'dual_check.timeout_s': Limit
  'challenge.enabled': boolean
  'challenge.max_claims': Limit
  'challenge.max_queries': Limit
  'challenge.timeout_s': Limit
  'inquiry.enabled': boolean
  'inquiry.max_queries': Limit
  'inquiry.timeout_s': Limit
  daily_max_usd: Limit
}
export type VerifyKey = keyof VerifyDraft
/** 能设成不限的几项 */
type LimitKey = 'dual_check.max_queries' | 'dual_check.timeout_s' | 'challenge.max_claims' | 'challenge.max_queries'
  | 'challenge.timeout_s' | 'inquiry.max_queries' | 'inquiry.timeout_s' | 'daily_max_usd'
type Part = 'dual_check' | 'challenge' | 'inquiry'

/** 勾掉「不限」时填回的数：同服务端 VERIFY_DEFAULTS */
const FALLBACK: Record<LimitKey, string> = {
  'dual_check.max_queries': '6', 'dual_check.timeout_s': '120', 'challenge.max_claims': '5', 'challenge.max_queries': '4',
  'challenge.timeout_s': '90', 'inquiry.max_queries': '6', 'inquiry.timeout_s': '120', daily_max_usd: '2',
}
const COUNTS = new Set<VerifyKey>(['dual_check.max_queries', 'challenge.max_claims', 'challenge.max_queries', 'inquiry.max_queries'])
const TIMEOUTS = new Set<VerifyKey>(['dual_check.timeout_s', 'challenge.timeout_s', 'inquiry.timeout_s'])

const isRecord = (v: unknown): v is Record<string, any> => !!v && typeof v === 'object' && !Array.isArray(v)
/** 一项上限 → 草稿：null 是不限，没有这一项（老服务端、存坏了）当空串 */
const limitText = (v: unknown): Limit => (v === null ? null : v === undefined ? '' : String(v))

/** GET /settings 的 verify 一组（服务端给的是引擎实际生效的样子：存坏了的显示默认，null 是不限）→ 草稿 */
export function verifyDraftOf(group: unknown): VerifyDraft {
  const g = isRecord(group) ? group : {}
  const part = (p: Part): Record<string, any> => (isRecord(g[p]) ? g[p] : {})
  const tol = part('dual_check').tolerance
  return {
    provider: typeof g.provider === 'string' ? g.provider : '',
    model: typeof g.model === 'string' ? g.model : '',
    'dual_check.enabled': part('dual_check').enabled !== false,
    'dual_check.tolerance': typeof tol === 'number' ? String(tol) : '',
    'dual_check.max_queries': limitText(part('dual_check').max_queries),
    'dual_check.timeout_s': limitText(part('dual_check').timeout_s),
    'challenge.enabled': part('challenge').enabled !== false,
    'challenge.max_claims': limitText(part('challenge').max_claims),
    'challenge.max_queries': limitText(part('challenge').max_queries),
    'challenge.timeout_s': limitText(part('challenge').timeout_s),
    'inquiry.enabled': part('inquiry').enabled !== false,
    'inquiry.max_queries': limitText(part('inquiry').max_queries),
    'inquiry.timeout_s': limitText(part('inquiry').timeout_s),
    daily_max_usd: limitText(g.daily_max_usd),
  }
}

/** 改过的项（按草稿里的先后） */
export function verifyChanged(cur: VerifyDraft, saved: VerifyDraft): VerifyKey[] {
  return (Object.keys(saved) as VerifyKey[]).filter((k) => cur[k] !== saved[k])
}

/** 保存条上这几项的叫法：和字段名、服务端报错同一套 */
export const verifyLabels = (keys: VerifyKey[]): string[] => keys.map((k) => T[k])

/** 各项的问题。同裁判那一组：只校验改过的（存着的值不合规不该挡住别的设置保存），null（不限）总是对的 */
export function verifyErrors(cur: VerifyDraft, saved: VerifyDraft): Partial<Record<VerifyKey, string>> {
  const out: Partial<Record<VerifyKey, string>> = {}
  for (const k of verifyChanged(cur, saved)) {
    const v = cur[k]
    if (typeof v !== 'string') continue
    const n = Number(v.trim())
    if (k === 'dual_check.tolerance') {
      if (!(v.trim() !== '' && Number.isFinite(n) && n >= 0)) out[k] = S.invalid.tolerance
    } else if (COUNTS.has(k)) {
      if (!(/^\d+$/.test(v.trim()) && n >= 1)) out[k] = S.invalid.count
    } else if (TIMEOUTS.has(k)) {
      if (!(v.trim() !== '' && Number.isFinite(n) && n > 0)) out[k] = S.invalid.seconds
    } else if (k === 'daily_max_usd') {
      if (!(v.trim() !== '' && Number.isFinite(n) && n > 0)) out[k] = S.invalid.usd
    }
  }
  return out
}

/** 草稿 → 存进去的整组 verify：不限存 null，空着的接入、模型存 null（沿用证据裁判模型） */
export function verifyPayload(cur: VerifyDraft): Record<string, any> {
  const limit = (v: Limit) => (v === null ? null : Number(v.trim()))
  return {
    provider: cur.provider || null,
    model: cur.model.trim() || null,
    dual_check: {
      enabled: cur['dual_check.enabled'],
      tolerance: Number(cur['dual_check.tolerance'].trim()),
      max_queries: limit(cur['dual_check.max_queries']),
      timeout_s: limit(cur['dual_check.timeout_s']),
    },
    challenge: {
      enabled: cur['challenge.enabled'],
      max_claims: limit(cur['challenge.max_claims']),
      max_queries: limit(cur['challenge.max_queries']),
      timeout_s: limit(cur['challenge.timeout_s']),
    },
    inquiry: {
      enabled: cur['inquiry.enabled'],
      max_queries: limit(cur['inquiry.max_queries']),
      timeout_s: limit(cur['inquiry.timeout_s']),
    },
    daily_max_usd: limit(cur.daily_max_usd),
  }
}

/** 今天花了多少美元：小额按分以下的精度写，不四舍五入成 0（同裁判那一行） */
const formatSpend = (usd: number | undefined): string =>
  usd == null || !Number.isFinite(usd) ? '0' : usd >= 1 ? usd.toFixed(2) : String(Number(usd.toFixed(4)))

const fieldId = (k: VerifyKey) => `pref-verify-${k.replace('.', '-')}`

/**
 * 「核查」一节。refresh 变了（刚存过 verify 一组）就再取一次今日花费：每日上限变了，那一行的分母跟着变。
 * 取不到（老服务端没有这个接口）就不写那一行
 */
export function VerifySection({ cur, errors, onEdit, refresh }: {
  cur: VerifyDraft
  errors: Partial<Record<VerifyKey, string>>
  onEdit: (patch: Partial<VerifyDraft>) => void
  refresh: number
}) {
  const providers = useCatalog((s) => s.providers)
  const [spend, setSpend] = useState<VerifySpend | null>(null)
  useEffect(() => {
    let alive = true
    get<VerifySpend>('/settings/verify/spend').then((s) => { if (alive) setSpend(s) }, () => { if (alive) setSpend(null) })
    return () => { alive = false }
  }, [refresh])

  // 核查模型的接入：两项都留空时服务端沿用证据裁判模型的取值链；只填了模型时这一级整组生效，按模型名找接入
  //（verify_model_spec 不跨级拼）——留空那一项不能还叫「跟随证据裁判模型」
  const enabledProviders = providers.filter((p) => p.enabled)
  const defaultProvider = enabledProviders.find((p) => p.kind !== 'mock') ?? enabledProviders[0]
  const provider = cur.provider ? providers.find((p) => p.name === cur.provider) : undefined
  const providerMissing = !!cur.provider && providers.length > 0 && !provider
  const modelName = cur.model.trim()
  const byModel = !cur.provider && !!modelName
  const notSet = !cur.provider && !modelName
  const owner = byModel ? providerOfModel(providers, modelName) : undefined
  const byModelHint = byModel && providers.length > 0
    ? S.byModel(modelName, owner && { name: owner.provider.name, enabled: owner.enabled }, defaultProvider?.name) : ''
  const models = provider ? (provider.models ?? []).map((m) => m.id) : [...new Set(modelOptions(providers).map((o) => o.value))]
  const pickProvider = (name: string) => {
    // 换了接入，原来填的模型不在新接入的清单里就清掉：拿 A 家的模型名去调 B 家必然失败
    const next = name ? providers.find((p) => p.name === name) : undefined
    const keep = !name || !cur.model || (next?.models ?? []).some((m) => m.id === cur.model) || next?.default_model === cur.model
    onEdit({ provider: name, ...(keep ? {} : { model: '' }) })
  }

  /** 某项设成不限时，它还受哪些眼下设着的上限约束 */
  const note = (k: LimitKey): string => {
    if (TIMEOUTS.has(k)) return S.unlimitedNote('timeout', [])
    if (k === 'challenge.max_claims') return S.unlimitedNote('claims', [S.bound.claims])
    if (k === 'daily_max_usd') {
      const counts = [...COUNTS].some((c) => cur[c] !== null)
      const times = [...TIMEOUTS].some((c) => cur[c] !== null)
      return S.unlimitedNote('daily', [counts ? S.bound.queries : '', times ? S.bound.timeouts : ''].filter(Boolean))
    }
    const part = k.split('.')[0] as Part
    return S.unlimitedNote('queries', [cur[`${part}.timeout_s` as LimitKey] !== null ? S.bound.timeout : '',
      cur.daily_max_usd !== null ? S.bound.daily : ''].filter(Boolean))
  }
  const limit = (k: LimitKey, extra?: ReactNode) => (
    <LimitInput key={k} k={k} value={cur[k]} fallback={FALLBACK[k]} integer={COUNTS.has(k)} error={errors[k]}
                unlimited={note(k)} onChange={(v) => onEdit({ [k]: v } as Partial<VerifyDraft>)} extra={extra} />
  )
  const toggle = (k: 'dual_check.enabled' | 'challenge.enabled' | 'inquiry.enabled') => (
    <label className="mb-2 flex cursor-pointer items-center gap-2 text-xs">
      <input type="checkbox" checked={cur[k]} data-verify-toggle={k} onChange={(e) => onEdit({ [k]: e.target.checked })} />
      <span>{T[k]}</span>
    </label>
  )

  return (
    <section data-verify-settings="">
      <SectionBar title={S.title} hint={S.hint} />
      <div role="group" aria-labelledby="pref-verify-model-label" aria-describedby="pref-verify-model-hint" data-verify-model="">
        <div id="pref-verify-model-label" className="text-xs font-medium">{S.model}</div>
        {notSet ? (
          // 两项都没填：核查会用证据裁判的模型（没配裁判模型时就是助手的模型）。醒目地说，并给出建议
          <div id="pref-verify-model-hint" role="note" data-verify-not-set=""
               className="mb-2 mt-1 flex items-start gap-1.5 rounded border-l-2 py-0.5 pl-2 text-2xs leading-relaxed"
               style={{ borderColor: 'var(--st-waiting)' }}>
            <AlertTriangle size={12} aria-hidden className="mt-0.5 shrink-0" style={{ color: 'var(--st-waiting)' }} />
            <span>
              <span className="font-medium">{S.notSet}</span>
              <span className="block text-dim">{S.notSetAdvice}</span>
            </span>
          </div>
        ) : (
          <p id="pref-verify-model-hint" className="mb-2 mt-0.5 text-2xs leading-relaxed text-faint">{S.differ}</p>
        )}
        <div className="grid grid-cols-2 gap-3 max-sm:grid-cols-1">
          <Field label={T.provider} htmlFor={fieldId('provider')}
                 error={providerMissing ? S.providerGone(cur.provider) : undefined}
                 hint={byModelHint ? (
                   <span data-verify-by-model={owner ? (owner.enabled ? 'found' : 'disabled') : 'default'}
                         style={owner?.enabled ? undefined : { color: 'var(--st-waiting)' }}>{byModelHint}</span>
                 ) : undefined}>
            {(p) => (
              <select {...p} className="field" value={cur.provider} onChange={(e) => pickProvider(e.target.value)}>
                <option value="">{byModel ? S.providerFromModel : S.providerDefault}</option>
                {providerMissing && <option value={cur.provider}>{S.providerGoneOption(cur.provider)}</option>}
                {providers.map((pv) => <option key={pv.id} value={pv.name}>{pv.name}{pv.enabled ? '' : S.providerOff}</option>)}
              </select>
            )}
          </Field>
          <Field label={T.model} htmlFor={fieldId('model')} hint={provider?.default_model ? S.modelOf(provider.default_model) : undefined}>
            {(p) => (
              <>
                <input {...p} className="field mono text-xs" value={cur.model} list="pref-verify-models" spellCheck={false}
                       placeholder={cur.provider ? S.modelDefault : S.modelFollow} onChange={(e) => onEdit({ model: e.target.value })} />
                <datalist id="pref-verify-models">{models.map((m) => <option key={m} value={m} />)}</datalist>
              </>
            )}
          </Field>
        </div>
      </div>

      <PartGroup part="dual_check">
        {toggle('dual_check.enabled')}
        <div className="grid grid-cols-3 gap-3 max-sm:grid-cols-1">
          <Field label={T['dual_check.tolerance']} htmlFor={fieldId('dual_check.tolerance')} error={errors['dual_check.tolerance']}
                 hint={S.toleranceHint}>
            {(p) => (
              <input {...p} className="field tnum" inputMode="decimal" value={cur['dual_check.tolerance']}
                     onChange={(e) => onEdit({ 'dual_check.tolerance': e.target.value })} />
            )}
          </Field>
          {limit('dual_check.max_queries')}
          {limit('dual_check.timeout_s')}
        </div>
      </PartGroup>
      <PartGroup part="challenge">
        {toggle('challenge.enabled')}
        <div className="grid grid-cols-3 gap-3 max-sm:grid-cols-1">
          {limit('challenge.max_claims')}
          {limit('challenge.max_queries')}
          {limit('challenge.timeout_s')}
        </div>
      </PartGroup>
      <PartGroup part="inquiry">
        {toggle('inquiry.enabled')}
        <div className="grid grid-cols-3 gap-3 max-sm:grid-cols-1">
          {limit('inquiry.max_queries')}
          {limit('inquiry.timeout_s')}
        </div>
      </PartGroup>
      <div className="mt-4 grid grid-cols-3 gap-3 max-sm:grid-cols-1" data-verify-daily="">
        {limit('daily_max_usd', spend && (
          <div className="mt-1 text-2xs leading-relaxed text-dim" data-verify-spend="">
            {spend.calls ? S.spend(formatSpend(spend.usd), spend.calls) : S.spendNone}
            {!!spend.unpriced_calls && (
              <span className="block" style={{ color: 'var(--st-waiting)' }} data-verify-unpriced="">{S.unpriced(spend.unpriced_calls)}</span>
            )}
          </div>
        ))}
      </div>
    </section>
  )
}

/** 三项各一组：标题是服务端叫法表里那一组的叫法，下面一句说它做什么 */
function PartGroup({ part, children }: { part: Part; children: ReactNode }) {
  return (
    <div className="mt-4" role="group" aria-labelledby={`pref-verify-${part}-label`} aria-describedby={`pref-verify-${part}-hint`}
         data-verify-part={part}>
      <div id={`pref-verify-${part}-label`} className="text-xs font-medium">{T[part]}</div>
      <p id={`pref-verify-${part}-hint`} className="mb-2 mt-0.5 text-2xs leading-relaxed text-faint">{S.parts[part]}</p>
      {children}
    </div>
  )
}

/**
 * 一项上限：数字框加「不限」（照裁判那一组的 LimitField）。勾了不限，框变灰，下面写明不设上限之后还受什么约束——
 * 不许悄悄生效；勾掉时填回之前的数（没有就填默认值）
 */
function LimitInput({ k, value, fallback, integer, error, unlimited, onChange, extra }: {
  k: LimitKey; value: Limit; fallback: string; integer?: boolean; error?: string; unlimited: string
  onChange: (v: Limit) => void; extra?: ReactNode
}) {
  const last = useRef(value ?? fallback)
  if (value !== null) last.current = value || last.current
  const off = value === null
  const label = T[k]
  return (
    <div className="min-w-0" data-verify-limit={k} data-unlimited={off ? '' : undefined}>
      <Field label={label} htmlFor={fieldId(k)} error={error}
             hint={off ? <span style={{ color: 'var(--st-waiting)' }} data-verify-unlimited="">{unlimited}</span> : undefined}>
        {(p) => (
          <div className="flex items-center gap-2">
            <input {...p} className="field tnum min-w-0 flex-1" inputMode={integer ? 'numeric' : 'decimal'}
                   value={off ? '' : value} disabled={off} placeholder={off ? S.unlimited : undefined}
                   onChange={(e) => onChange(e.target.value)} />
            <label className="flex shrink-0 cursor-pointer items-center gap-1 text-xs">
              <input type="checkbox" checked={off} aria-label={`${label}：${S.unlimited}`} aria-describedby={p['aria-describedby']}
                     data-verify-unlimited-toggle="" onChange={(e) => onChange(e.target.checked ? null : last.current || fallback)} />
              {S.unlimited}
            </label>
          </div>
        )}
      </Field>
      {extra}
    </div>
  )
}
