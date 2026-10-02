import { useEffect, useId, useMemo, useRef, useState } from 'react'
import type { EditAnchor, Recipe, SelectionAs, SelectionOptions, SelectionRequest } from '../../types'
import { Spinner } from '../../components/ui'
import { formatNumber } from '../../lib/format'
import { ANCHOR_KIND_LABEL, RECIPE_TEXT, SELECT_AS_LABEL } from '../../lib/terms'
import { EditPreviewBody, PanelHead, useEditFlow } from './FixPanel'
import type { EditFlowHandlers } from './FixPanel'
import type { GridSelection } from './SheetGrid'
import { ChoiceGroup } from './SuggestionCards'

// ===========================================================================
// 「框选为…」（P3-SPEC 第 4 节、10.2）：框选的范围加上「它是什么」（取值封闭），由服务端换算成按文字定位的
// 配方规则，立刻干跑并把识别出的范围和框比对（重放比对）。坐标只用在换算的那一刻，不进配方：下个月行列挪了
// 位置，配方照样按文字找到它。换算不了就说为什么，「应用」不可用
// ===========================================================================

/** 忽略类要写理由：理由会写进配方、进配方哈希，填好后点「预览」才发，改了要重新预览 */
const NEEDS_REASON = new Set<SelectionAs>(['ignore_rows', 'ignore_columns', 'ignore_outside'])

/** 配方里各交叉表的分段 id（「分段标题」要选是哪个分段） */
function segmentIds(recipe: Recipe | null | undefined): string[] {
  const out: string[] = []
  for (const sheet of Array.isArray(recipe?.sheets) ? recipe!.sheets : []) {
    for (const block of Array.isArray(sheet?.blocks) ? sheet.blocks : []) {
      for (const seg of Array.isArray(block?.segments) ? block.segments : []) if (seg?.id) out.push(String(seg.id))
    }
  }
  return out
}

/** 锚点按种类分组，保持服务端给的先后（「表头：地区、产品、销量、金额」） */
function groupAnchors(anchors: EditAnchor[]): [string, EditAnchor[]][] {
  const groups = new Map<string, EditAnchor[]>()
  for (const a of anchors) groups.set(a.kind, [...(groups.get(a.kind) ?? []), a])
  return [...groups.entries()]
}

export function SelectionPanel({ selection, as, recipe, handlers, onClose, onFocus }: {
  selection: GridSelection
  as: SelectionAs
  recipe: Recipe | null
  handlers: EditFlowHandlers
  onClose: () => void
  onFocus: (cell: string) => void
}) {
  const flow = useEditFlow(handlers)
  const name = useId()
  const [headerRows, setHeaderRows] = useState(1)
  const [bottom, setBottom] = useState<'box' | 'auto'>('box')
  const [tableText, setTableText] = useState('')
  const [table, setTable] = useState('')
  const [role, setRole] = useState<'measures' | 'dimension' | null>(null)
  const [keep, setKeep] = useState<boolean | null>(null)
  const [segment, setSegment] = useState('')
  const [reason, setReason] = useState('')
  const needsReason = NEEDS_REASON.has(as)
  const segments = useMemo(() => segmentIds(recipe), [recipe])

  /** 这一刻的参数；必填的还没选时为 null（不发预览） */
  const options: SelectionOptions | null = (() => {
    switch (as) {
      case 'list': return { header_rows: headerRows, bottom, ...(table ? { table } : {}) }
      case 'segment': return role ? { role, ...(table ? { table } : {}) } : null
      case 'derived': return keep == null ? null : { keep }
      case 'section_title': return segment ? { segment } : null
      case 'ignore_rows': case 'ignore_columns': case 'ignore_outside':
        return reason.trim() ? { reason: reason.trim() } : null
      default: return {}
    }
  })()
  const request: SelectionRequest | null = options
    ? { sheet: selection.sheet, ref: selection.ref, as, ...(Object.keys(options).length ? { options } : {}) }
    : null
  const key = request ? JSON.stringify(request) : ''

  // 不用写理由的：参数齐了就预览，参数改了重新预览（序号递增，只采用最新一次的回包）。同一组参数只发一次：
  // 开发模式的 StrictMode 会把 effect 跑两遍，不拦的话每次都多发一个注定被丢弃的预览
  const autoSent = useRef('')
  useEffect(() => {
    if (!request || needsReason || autoSent.current === key) return
    autoSent.current = key
    void flow.run({ selection: request })
  }, [needsReason ? '' : key])

  const { preview, sent, busy, stale, reasonError, error } = flow
  const sentKey = sent && 'selection' in sent ? JSON.stringify(sent.selection) : ''
  const matches = !!sentKey && sentKey === key
  const replay = preview?.replay ?? null
  const canApply = !!preview?.ok && matches && !stale && !busy
  const staleHint = preview && sentKey && !matches && needsReason ? RECIPE_TEXT.fixReasonChanged : null

  return (
    <section className="space-y-3" data-selection-panel={as}>
      <PanelHead title={RECIPE_TEXT.selectionTitle(SELECT_AS_LABEL[as] ?? as)} onBack={onClose} onFocus={onFocus}>
        <p className="text-2xs text-dim" data-selection-where>{RECIPE_TEXT.selected(`${selection.sheet}!${selection.ref}`)}</p>
      </PanelHead>

      <fieldset className="space-y-2 rounded-md border bg-bg px-2.5 py-2 text-xs" data-selection-options>
        {as === 'list' && (
          <>
            <label className="flex items-center gap-2">
              <span className="text-dim">{RECIPE_TEXT.selectionHeaderRows}</span>
              <select className="field !w-20" value={headerRows} data-selection-header-rows
                      onChange={(e) => setHeaderRows(Number(e.target.value))}>
                {[1, 2, 3].map((n) => <option key={n} value={n}>{n}</option>)}
              </select>
            </label>
            <div className="space-y-1">
              <div className="text-dim">{RECIPE_TEXT.selectionBottom}</div>
              <ChoiceGroup name={`${name}-bottom`} label={RECIPE_TEXT.selectionBottom} attr="data-selection-bottom" value={bottom}
                           options={[{ value: 'box', label: RECIPE_TEXT.selectionBottomBox }, { value: 'auto', label: RECIPE_TEXT.selectionBottomAuto }]}
                           onChange={(v) => setBottom(v as 'box' | 'auto')} />
            </div>
          </>
        )}
        {as === 'segment' && (
          <div className="space-y-1">
            <div className="text-dim">{RECIPE_TEXT.selectionRole}</div>
            <ChoiceGroup name={`${name}-role`} label={RECIPE_TEXT.selectionRole} attr="data-selection-role" value={role}
                         options={[{ value: 'measures', label: RECIPE_TEXT.selectionRoleMeasures },
                           { value: 'dimension', label: RECIPE_TEXT.selectionRoleDimension }]}
                         onChange={(v) => setRole(v as 'measures' | 'dimension')} />
          </div>
        )}
        {(as === 'list' || as === 'segment') && (
          <label className="flex flex-wrap items-center gap-2">
            <span className="text-dim">{RECIPE_TEXT.selectionTable}</span>
            <input className="field !w-40" value={tableText} placeholder={RECIPE_TEXT.selectionTablePlaceholder} data-selection-table
                   onChange={(e) => setTableText(e.target.value)} onBlur={() => setTable(tableText.trim())}
                   onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); setTable(tableText.trim()) } }} />
          </label>
        )}
        {as === 'derived' && (
          <div className="space-y-1">
            <div className="text-dim">{RECIPE_TEXT.selectionKeep}</div>
            <ChoiceGroup name={`${name}-keep`} label={RECIPE_TEXT.selectionKeep} attr="data-selection-keep"
                         value={keep == null ? null : keep ? 'yes' : 'no'}
                         options={[{ value: 'yes', label: RECIPE_TEXT.selectionKeepYes }, { value: 'no', label: RECIPE_TEXT.selectionKeepNo }]}
                         onChange={(v) => setKeep(v === 'yes')} />
          </div>
        )}
        {as === 'section_title' && (
          <label className="flex items-center gap-2">
            <span className="text-dim">{RECIPE_TEXT.selectionSegment}</span>
            <select className="field !w-40" value={segment} data-selection-segment onChange={(e) => setSegment(e.target.value)}>
              <option value="">{RECIPE_TEXT.selectionSegmentPick}</option>
              {segments.map((id) => <option key={id} value={id}>{id}</option>)}
            </select>
          </label>
        )}
        {as === 'crosstab' && <p className="text-2xs text-dim">{RECIPE_TEXT.selectionCrosstabNote}</p>}
        {needsReason && (
          <div className="space-y-1">
            <label className="label" htmlFor={`${name}-reason`}>{RECIPE_TEXT.fixReasonLabel}</label>
            <textarea id={`${name}-reason`} className="field text-xs" rows={2} maxLength={200} value={reason} data-selection-reason
                      placeholder={RECIPE_TEXT.fixReasonPlaceholder} aria-invalid={!!reasonError || undefined}
                      style={reasonError ? { borderColor: 'var(--err)' } : undefined}
                      onChange={(e) => setReason(e.target.value)} />
            {reasonError && <div className="text-2xs text-[var(--err)]" role="alert">{reasonError}</div>}
          </div>
        )}
        {(needsReason || stale) && (
          <button type="button" className="btn btn-sm" data-selection-preview-run disabled={!request || !!busy}
                  title={needsReason && !reason.trim() ? RECIPE_TEXT.fixReasonFirst : undefined}
                  onClick={() => request && void flow.run({ selection: request })}>
            {busy === 'preview' ? <Spinner size={11} /> : null} {preview ? RECIPE_TEXT.fixPreviewAgain : RECIPE_TEXT.fixPreview}
          </button>
        )}
      </fieldset>

      {busy === 'preview' && (
        <p className="inline-flex items-center gap-1 text-2xs text-faint" role="status"><Spinner size={10} /> {RECIPE_TEXT.fixPreviewing}</p>
      )}
      {staleHint && <p className="text-2xs text-[var(--warn)]" role="status" data-fix-stale-hint>{staleHint}</p>}
      {stale && <p className="text-2xs text-[var(--warn)]" role="alert" data-edit-stale>{RECIPE_TEXT.editStale}</p>}

      {preview && (
        <div className="space-y-2" data-selection-preview={preview.ok ? 'ok' : 'blocked'} style={!matches ? { opacity: 0.6 } : undefined}>
          {/* 没通过的原因由 EditPreviewBody 统一列在最前（与修复面板同一份）；定位文字、重放比对、提示排在原因之后 */}
          <EditPreviewBody preview={preview} prefix="selection" onFocus={onFocus}>
            {preview.ok && (
              <section className="space-y-1" data-selection-anchors>
                <p className="text-2xs text-dim" data-selection-by-text>{RECIPE_TEXT.selectionByText}</p>
                {groupAnchors(preview.anchors ?? []).map(([kind, list]) => (
                  <div key={kind} className="flex flex-wrap items-center gap-1 text-xs">
                    <span className="text-dim">{ANCHOR_KIND_LABEL[kind] ?? kind}：</span>
                    {list.map((a, i) => <span key={`${a.text}-${i}`} className="chip" data-anchor={kind} title={a.cell ?? undefined}>{a.text}</span>)}
                  </div>
                ))}
              </section>
            )}
            {preview.ok && replay && (
              <section className="space-y-1 rounded-md border px-2.5 py-1.5 text-xs" data-replay-match={String(replay.match)}
                       style={{ borderColor: `color-mix(in srgb, ${replay.match ? 'var(--ok)' : 'var(--warn)'} 45%, var(--border))` }}>
                <div style={{ color: replay.match ? 'var(--ok)' : 'var(--warn)' }}>
                  {replay.match ? RECIPE_TEXT.selectionReplayMatch : RECIPE_TEXT.selectionReplayDiffer}
                </div>
                <dl className="grid grid-cols-[auto_1fr] gap-x-2 text-2xs text-dim">
                  {[['expected', RECIPE_TEXT.selectionExpected, replay.expected], ['actual', RECIPE_TEXT.selectionActual, replay.actual]].map(([k, label, regions]) => (
                    <div key={k as string} className="contents" data-replay-regions={k as string}>
                      <dt>{label as string}</dt>
                      <dd className="mono">
                        {Object.entries((regions ?? {}) as Record<string, string | null>).filter(([, v]) => v)
                          .map(([part, v]) => `${RECIPE_TEXT.selectionRegion[part] ?? part} ${v}`).join('，') || '—'}
                      </dd>
                    </div>
                  ))}
                </dl>
                {!!replay.diffs?.length && <ul className="list-disc pl-4 text-2xs text-dim">{replay.diffs.map((d, i) => <li key={i}>{d}</li>)}</ul>}
                {replay.window_rows != null && (
                  <p className="text-2xs text-faint" data-replay-window={replay.window_rows}>{RECIPE_TEXT.selectionWindow(formatNumber(replay.window_rows))}</p>
                )}
              </section>
            )}
            {!!preview.notes?.length && (
              <section className="space-y-0.5" data-selection-notes>
                <h4 className="text-xs font-semibold">{RECIPE_TEXT.selectionNotes}</h4>
                <ul className="list-disc pl-4 text-2xs leading-relaxed text-dim">{preview.notes.map((n, i) => <li key={i}>{n}</li>)}</ul>
              </section>
            )}
          </EditPreviewBody>
        </div>
      )}
      {error && <p className="text-xs text-[var(--err)]" role="alert" data-selection-error>{error}</p>}
      <div className="flex flex-wrap items-center gap-2 border-t pt-2">
        <button type="button" className="btn btn-sm btn-primary" disabled={!canApply} onClick={() => void flow.apply()} data-selection-apply>
          {busy === 'apply'
            ? <><Spinner size={11} /> {RECIPE_TEXT.fixApplying}</>
            : replay && !replay.match ? RECIPE_TEXT.selectionApplyAnyway : RECIPE_TEXT.selectionApply}
        </button>
        <button type="button" className="btn btn-sm" onClick={onClose} disabled={busy === 'apply'}>{RECIPE_TEXT.selectionCancel}</button>
      </div>
    </section>
  )
}
