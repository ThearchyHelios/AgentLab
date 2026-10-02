import type { DiffItem } from '../../types'
import { formatNumber } from '../../lib/format'
import { DIFF_KIND_LABEL, RECIPE_TEXT } from '../../lib/terms'

// ===========================================================================
// 差异卡：上传新一期与上一期相比变了什么。需确认的排在前面（它们同时是确认清单里的必勾项），
// 其余只作说明。结构类变化不在这里——那种在试运行里就拒收了
// ===========================================================================

export function ReuploadDiff({ diff, sameAsImport }: {
  /** null：没有可比的上一期，或者试运行拒收、需要录入（服务端有意不算差异），调用方此时不显示这张卡 */
  diff: DiffItem[] | null | undefined
  sameAsImport?: { id: string; seq: number } | null
}) {
  const items = [...(diff ?? [])].sort((a, b) => Number(!!b.requires_confirm) - Number(!!a.requires_confirm))
  return (
    <section className="space-y-1.5 rounded-lg border bg-panel px-3 py-2" data-reupload-diff>
      <h4 className="text-xs font-semibold">{RECIPE_TEXT.diffTitle}</h4>
      {sameAsImport && (
        <p className="rounded-md border px-2.5 py-1.5 text-xs text-dim" data-same-as-import={sameAsImport.seq}>
          {RECIPE_TEXT.sameAsImport(formatNumber(sameAsImport.seq))}
        </p>
      )}
      {!items.length && !sameAsImport && <p className="text-xs text-faint">{RECIPE_TEXT.diffNone}</p>}
      {items.length > 0 && (
        <ul className="space-y-1">
          {items.map((d, i) => (
            <li key={`${d.kind}-${i}`} className="space-y-0.5 rounded-md border px-2.5 py-1.5 text-xs" data-diff={d.kind}
                data-requires-confirm={d.requires_confirm ? (d.confirm_id ?? '') : undefined}
                style={d.requires_confirm ? { borderColor: 'color-mix(in srgb, var(--warn) 45%, var(--border))' } : undefined}>
              <div className="flex flex-wrap items-baseline gap-x-2">
                {d.requires_confirm && <span className="chip" style={{ color: 'var(--warn)', borderColor: 'var(--warn)' }}>{RECIPE_TEXT.diffRequires}</span>}
                <span className="text-faint">{DIFF_KIND_LABEL[d.kind] ?? RECIPE_TEXT.diffKindOther}</span>
                <span className="min-w-0 flex-1">{d.label}</span>
              </div>
              {d.detail && <p className="text-2xs leading-relaxed text-dim">{d.detail}</p>}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
