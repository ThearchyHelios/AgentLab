import { Field, SectionBar } from '../../components/ui'
import {
  ANALYSIS_SETTING_GROUPS, ANALYSIS_SETTING_KEYS, ANALYSIS_SETTING_LABEL, ANALYSIS_TEXT, settingRangeText, settingValueProblem,
  type AnalysisSettingKey,
} from '../../lib/termsAnalysis'

/*
 * 设置页「偏好设置」里「分析」一节（服务端 api/settings.py 的 analysis 一组，docs/design/analysis-round/01-算子与判定.md §10）：
 * 判定用的阈值（「主要来源」的占比、「超出常规波动」的倍数……）、Agent 每次执行最多分析几次、最多下钻几层、列出前几段。
 *
 * 照 LedgerSettings.tsx 的写法：草稿由 PrefsTab 持有、和其余设置共用一条保存条（这一节只画、只报改动），存的时候整组写
 * analysis——服务端对没给的项回落到缺省，只发改过的那一项会把别的项冲回缺省。字段名、范围都照服务端 analysis/contract.py
 * （LABELS、RANGES，lib/termsAnalysis 一字不差地抄着，check-manage 逐项比对），422 的报错写的也是这些叫法。两两之间的大小
 * （「明显超出」要大于「超出常规波动」、最多用几期不小于至少几期、「集中」不小于「主要来源」）交给服务端核，报错照原话显示。
 */

export type AnalysisDraft = Record<AnalysisSettingKey, string>

const isRecord = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v)

/** GET /settings 的 analysis 一组（服务端给的是生效的样子，没存过的补了缺省）→ 草稿 */
export function analysisDraftOf(group: unknown): AnalysisDraft {
  const g = isRecord(group) ? group : {}
  return Object.fromEntries(ANALYSIS_SETTING_KEYS.map((k) => {
    const v = g[k]
    return [k, typeof v === 'number' && Number.isFinite(v) ? String(v) : '']
  })) as AnalysisDraft
}

/** 改过的项：字不同、而且不是同一个数（服务端把 2 存成 2.0，写「2.0」不算改） */
export function analysisChanged(cur: AnalysisDraft, saved: AnalysisDraft): AnalysisSettingKey[] {
  const same = (a: string, b: string) => a.trim() === b.trim()
    || (a.trim() !== '' && b.trim() !== '' && Number.isFinite(Number(a)) && Number(a) === Number(b))
  return ANALYSIS_SETTING_KEYS.filter((k) => !same(cur[k], saved[k]))
}

/** 保存条上这几项的叫法：和字段名、服务端报错同一套 */
export const analysisLabels = (keys: AnalysisSettingKey[]): string[] => keys.map((k) => ANALYSIS_SETTING_LABEL[k])

/** 各项的问题（类型、范围）。只校验改过的：存着的值不合规不该挡住别的设置保存 */
export function analysisErrors(cur: AnalysisDraft, saved: AnalysisDraft): Partial<Record<AnalysisSettingKey, string>> {
  const out: Partial<Record<AnalysisSettingKey, string>> = {}
  for (const k of analysisChanged(cur, saved)) {
    const problem = settingValueProblem(k, cur[k])
    if (problem) out[k] = problem
  }
  return out
}

/** 草稿 → 存进去的整组 analysis */
export function analysisPayload(cur: AnalysisDraft): Record<string, number> {
  return Object.fromEntries(ANALYSIS_SETTING_KEYS.map((k) => [k, Number(cur[k].trim())]))
}

const id = (k: AnalysisSettingKey) => `pref-analysis-${k.replace(/_/g, '-')}`

export function AnalysisSection({ cur, errors, onEdit }: {
  cur: AnalysisDraft
  errors: Partial<Record<AnalysisSettingKey, string>>
  onEdit: (patch: Partial<AnalysisDraft>) => void
}) {
  return (
    <section data-analysis-settings="">
      <SectionBar title={ANALYSIS_TEXT.settings.title} hint={ANALYSIS_TEXT.settings.hint} />
      <div className="space-y-4">
        {ANALYSIS_SETTING_GROUPS.map((group) => (
          <div key={group.title} data-analysis-settings-group={group.title}>
            <div className="mb-1.5 text-2xs font-medium text-dim">{group.title}</div>
            <div className="grid grid-cols-2 gap-3 max-sm:grid-cols-1">
              {group.keys.map((k) => (
                <Field key={k} label={ANALYSIS_SETTING_LABEL[k]} htmlFor={id(k)} error={errors[k]} hint={settingRangeText(k)}>
                  {(p) => (
                    <input {...p} className="field tnum max-w-40" inputMode="decimal" value={cur[k]} data-analysis-setting={k}
                           onChange={(e) => onEdit({ [k]: e.target.value } as Partial<AnalysisDraft>)} />
                  )}
                </Field>
              ))}
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}
