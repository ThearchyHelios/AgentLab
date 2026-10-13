import { modelOptions, providerOfModel, useCatalog } from '../store/catalog'
import { Field, SectionBar } from '../components/ui'
import { DRAFT_MODEL_SETTING_TEXT as T, PROVIDER_REF_TEXT } from '../lib/terms'
import { ProviderOptions } from './VerifySettings'

/*
 * 设置页「偏好设置」里「起草模型」一节：数据目录的「用模型起草」和按配方导入表格时的 AI 起草用哪个模型
 * （服务端 api/settings.py 的 catalog_draft 一组：{provider, model}）。
 *
 * 取模型的顺序（服务端）：这一组填了接入或模型就用它（只填模型时按模型名找接入，不跨级拼）；两项都留空跟随助手的
 * 模型；再没有就是默认接入。草稿由 PrefsTab 持有、和其余设置共用一条保存条，这里只画；两栏的叫法和服务端
 * _provider_fields 一字不差，保存被拒（422 带 field：catalog_draft.provider）时报错写的就是它
 */

export function DraftModelSection({ provider, model, error, onEdit }: {
  provider: string
  model: string
  /** 服务端按这一项拒收时的整句（设置页从 422 的 field 落过来） */
  error?: string
  onEdit: (patch: { draftProvider?: string; draftModel?: string }) => void
}) {
  const providers = useCatalog((s) => s.providers)
  const enabledProviders = providers.filter((p) => p.enabled)
  // 只填了模型、按模型名找不到接入时服务端用默认接入（第一个启用的真实接入，没有就第一个启用的）
  const defaultProvider = enabledProviders.find((p) => p.kind !== 'mock') ?? enabledProviders[0]
  const found = provider ? providers.find((p) => p.name === provider) : undefined
  const missing = !!provider && providers.length > 0 && !found
  // 存着的接入不存在、已停用：开始起草时就会报错（不建任务），在这一格下面说。名字恰好是某个启用接入的模型时
  // 补一句怎么用它（同设置页别的几组，和服务端保存时那句一致）
  const trap = found?.enabled === false || missing ? providerOfModel(providers, provider) : undefined
  const trapNote = trap?.enabled ? `。${PROVIDER_REF_TEXT.modelNote(provider, trap.provider.name, T.model, T.provider)}` : ''
  const providerError = error ?? (missing ? `${T.providerGone(provider)}${trapNote}`
    : found && !found.enabled ? `${PROVIDER_REF_TEXT.disabled(provider)}：${PROVIDER_REF_TEXT.draftBroken}${trapNote}` : undefined)
  const modelName = model.trim()
  const byModel = !provider && !!modelName
  const notSet = !provider && !modelName
  const owner = byModel ? providerOfModel(providers, modelName) : undefined
  const byModelHint = byModel && providers.length > 0
    ? T.byModel(modelName, owner && { name: owner.provider.name, enabled: owner.enabled }, defaultProvider?.name) : ''
  // 接入留空时按模型名找接入：所有启用接入的模型都能选；选了接入只列这一家的
  const models = found ? (found.models ?? []).map((m) => m.id) : [...new Set(modelOptions(providers).map((o) => o.value))]
  const pickProvider = (name: string) => {
    // 换了接入，原来填的模型不在新接入的清单里就清掉：拿 A 家的模型名去调 B 家必然失败
    const next = name ? providers.find((p) => p.name === name) : undefined
    const keep = !name || !model || (next?.models ?? []).some((m) => m.id === model) || next?.default_model === model
    onEdit({ draftProvider: name, ...(keep ? {} : { draftModel: '' }) })
  }

  return (
    <section data-draft-model-settings="">
      <SectionBar title={T.title} hint={T.hint} />
      <div role="group" aria-labelledby="pref-draft-model-label" aria-describedby={notSet ? 'pref-draft-model-hint' : undefined}
           data-draft-model-group={notSet ? 'follow' : 'set'}>
        <div id="pref-draft-model-label" className="sr-only">{T.title}</div>
        {notSet && <p id="pref-draft-model-hint" className="mb-2 text-2xs leading-relaxed text-faint">{T.notSet}</p>}
        <div className="grid grid-cols-2 gap-3 max-sm:grid-cols-1">
          <Field label={T.provider} htmlFor="pref-draft-provider" error={providerError}
                 hint={byModelHint ? (
                   <span data-draft-by-model={owner ? (owner.enabled ? 'found' : 'disabled') : 'default'}
                         style={owner?.enabled ? undefined : { color: 'var(--st-waiting)' }}>{byModelHint}</span>
                 ) : undefined}>
            {(p) => (
              <select {...p} className="field" value={provider} onChange={(e) => pickProvider(e.target.value)}>
                <option value="">{byModel ? T.providerFromModel : T.providerDefault}</option>
                {missing && <option value={provider}>{T.providerGoneOption(provider)}</option>}
                <ProviderOptions providers={providers} current={provider} />
              </select>
            )}
          </Field>
          <Field label={T.model} htmlFor="pref-draft-model"
                 hint={found?.enabled && found.default_model ? T.modelOf(found.default_model) : undefined}>
            {(p) => (
              <>
                <input {...p} className="field mono text-xs" value={model} list="pref-draft-models" spellCheck={false}
                       placeholder={provider ? T.modelDefault : T.modelFollow} onChange={(e) => onEdit({ draftModel: e.target.value })} />
                <datalist id="pref-draft-models">{models.map((m) => <option key={m} value={m} />)}</datalist>
              </>
            )}
          </Field>
        </div>
      </div>
    </section>
  )
}
