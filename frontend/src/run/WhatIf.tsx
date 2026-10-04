import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import clsx from 'clsx'
import { CornerDownRight, FlaskConical, LocateFixed, RotateCcw } from 'lucide-react'
import { api } from '../api/client'
import { ErrorNotice, Spinner, isComposing } from '../components/ui'
import { evidenceValue as valueText, type queryWindow } from '../lib/evidence'
import { NONE } from '../lib/format'
import { WHATIF_TEXT } from '../lib/terms'
import type { WhatIfInput, WhatIfMetric, WhatIfOverrides, WhatIfRequest, WhatIfResult } from '../types'

/**
 * 数字推演：证据面板里点开口径卡上的一个数，改某个输入的值、或者在查询结果里勾掉几行，立即看到这个指标和报告里
 * 引用它的地方会变成多少（POST …/evidence/whatif，后端 engine/recompute.py 按口径卡同一套规则重算）。
 *
 * 推演永远不是出具：不写进运行记录、不改报告。顶上的横幅一直在，运行没封存时另说「数据可能还会变」。
 *
 * 三样东西分开放：
 * - useWhatIfDraft：改动的草稿（输入框里的原文、提交了的新值、勾掉的行）。放在证据面板的片段那一层（SegmentBody），
 *   推演区和查询步骤的勾选框共用一份；同一个指标的几处引用之间来回切不清空，换了指标才清空
 * - WhatIf：推演区。输入框停顿一会儿（或回车、失焦）才提交，勾行立刻提交；请求在路上时上一次的结果变淡留着，
 *   新的改动来了旧请求直接丢掉。答复按请求体缓存，「恢复原值」回到原来那份不再请求
 * - WhatIfRows：推演时查询步骤里的结果窗口换成它，每行前面多一个勾选框
 *
 * 不能改的输入（文本、列表、遮罩列、决定取哪一格的下标）照实显示，原因列在「不支持推演的输入」里。
 */

/** 输入框停顿多久提交一次：边打字边请求，结果跟着抖 */
const SETTLE_MS = 450

interface DraftState {
  scope: string
  open: boolean
  /** 输入框里的原文。没碰过的输入不在里面，框里显示原值 */
  text: Record<string, string>
  /** 已提交的新值：请求按它发 */
  inputs: Record<string, number>
  /** 勾掉的行：查询快照的工件 id → 快照里的行号（从 0 数） */
  exclude: Record<string, number[]>
}

const freshDraft = (scope: string, open = false): DraftState => ({ scope, open, text: {}, inputs: {}, exclude: {} })

export interface WhatIfDraft {
  open: boolean
  toggle: () => void
  text: Record<string, string>
  /** 输入框改了：停顿一会儿才提交。old 是这个输入的原值，填回原值不算改动 */
  type: (path: string, value: string, old: unknown) => void
  /** 回车、失焦：立刻提交 */
  commit: () => void
  /** Esc：输入框退回已提交的值（没提交过就是原值） */
  revert: (path: string) => void
  toggleRow: (artifact: string, row: number) => void
  excluded: (artifact: string | undefined) => number[]
  overrides: WhatIfOverrides
  /** 有改动（含还没提交的输入框）：「恢复原值」可点 */
  dirty: boolean
  reset: () => void
}

/** 输入框里的文本 → 数。空的是 undefined（回到原值），不是数的是 null。千分位逗号、空格不算错 */
export function parseNumber(raw: string): number | null | undefined {
  const text = raw.replace(/[,，\s]/g, '')
  if (!text) return undefined
  if (!/^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/.test(text)) return null
  const n = Number(text)
  return Number.isFinite(n) ? n : null
}

/** 原值写进输入框的样子：数照原样（不加千分位，改起来不用先删逗号），缺的是空 */
const plain = (value: unknown): string => (typeof value === 'number' && Number.isFinite(value) ? String(value) : '')

/**
 * 提交：输入框里写的是数、而且和原值不同的才算改动。清空了回到原值；写的不是数的保留上一次提交的值——
 * 输错一个字，结果不该悄悄跳回原值
 */
function settled(state: DraftState, olds: Record<string, unknown>): Record<string, number> {
  const out: Record<string, number> = {}
  for (const [path, raw] of Object.entries(state.text)) {
    const n = parseNumber(raw)
    if (n === null) {
      if (path in state.inputs) out[path] = state.inputs[path]
    } else if (n !== undefined && olds[path] !== n) {
      out[path] = n
    }
  }
  return out
}

export function useWhatIfDraft(scope: string): WhatIfDraft {
  const [stored, setState] = useState<DraftState>(() => freshDraft(scope))
  // 换了一个指标（另一张卡、另一个数，或者不是指标了）：推演收起、改动清空
  const state = stored.scope === scope ? stored : freshDraft(scope)
  if (stored.scope !== scope) setState(state)
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined)
  const olds = useRef<Record<string, unknown>>({})
  useEffect(() => () => clearTimeout(timer.current), [])

  const commit = useCallback(() => {
    clearTimeout(timer.current)
    setState((s) => ({ ...s, inputs: settled(s, olds.current) }))
  }, [])
  const type = useCallback((path: string, value: string, old: unknown) => {
    olds.current[path] = old
    setState((s) => ({ ...s, text: { ...s.text, [path]: value } }))
    clearTimeout(timer.current)
    timer.current = setTimeout(commit, SETTLE_MS)
  }, [commit])
  const revert = useCallback((path: string) => {
    setState((s) => {
      const text = { ...s.text }
      if (path in s.inputs) text[path] = String(s.inputs[path])
      else delete text[path]
      return { ...s, text }
    })
  }, [])
  const toggleRow = useCallback((artifact: string, row: number) => {
    setState((s) => {
      const rows = s.exclude[artifact] ?? []
      const next = rows.includes(row) ? rows.filter((r) => r !== row) : [...rows, row].sort((a, b) => a - b)
      const exclude = { ...s.exclude }
      if (next.length) exclude[artifact] = next
      else delete exclude[artifact]
      return { ...s, exclude }
    })
  }, [])
  const reset = useCallback(() => {
    clearTimeout(timer.current)
    setState((s) => freshDraft(s.scope, s.open))
  }, [])
  // 收起就清空：再展开从原值开始，勾选框也跟着收起，不留一份看不见的改动
  const toggle = useCallback(() => {
    clearTimeout(timer.current)
    setState((s) => (s.open ? freshDraft(s.scope) : { ...s, open: true }))
  }, [])
  const overrides = useMemo(() => ({ inputs: state.inputs, exclude_rows: state.exclude }), [state.inputs, state.exclude])
  const excluded = useCallback((artifact: string | undefined) => (artifact ? state.exclude[artifact] ?? [] : []),
    [state.exclude])
  return {
    open: state.open, toggle, text: state.text, type, commit, revert, toggleRow, excluded, overrides, reset,
    dirty: Object.keys(state.text).length > 0 || Object.keys(state.inputs).length > 0 || Object.keys(state.exclude).length > 0,
  }
}

/** 指标步骤上的「推演」开关 */
export function WhatIfToggle({ open, onToggle, controls }: { open: boolean; onToggle: () => void; controls: string }) {
  return (
    <button type="button" className={clsx('btn btn-xs ml-auto shrink-0', open && 'border-[var(--st-waiting)]')}
            aria-expanded={open} aria-controls={controls} title={open ? WHATIF_TEXT.close : WHATIF_TEXT.openHint}
            onClick={onToggle} data-whatif-toggle={open ? 'open' : 'closed'}>
      <FlaskConical size={11} aria-hidden /> {WHATIF_TEXT.open}
    </button>
  )
}

type Slot = { key: string; status: 'loading' | 'ok' | 'error'; data?: WhatIfResult; error?: unknown; last?: WhatIfResult }

/** 推演区：横幅、结果、输入、不支持的输入、报告里会变的地方、下游口径卡、恢复原值 */
export function WhatIf({ id, runId, report, card, metric, draft, queryOf, onLocate }: {
  id: string
  runId: string
  /** 正文这份报告的撰写节点 id：按它选口径卡算过的那一份，报告里会变的地方也只列它的（都能定位） */
  report?: string
  /** 口径卡节点 id */
  card: string
  metric: string
  draft: WhatIfDraft
  /** 输入落在哪个查询步骤上：有的话给「在查询 Qn 中勾选要排除的行」，点一下跳过去 */
  queryOf: (artifact?: string) => { alias: string; go: () => void } | undefined
  /** 定位到正文里的一个片段 */
  onLocate?: (segId: string) => void
}) {
  const body: WhatIfRequest = useMemo(() => ({ ...(report ? { report } : {}), card, metric, overrides: draft.overrides }),
    [report, card, metric, draft.overrides])
  const key = JSON.stringify(body)
  const [slot, setSlot] = useState<Slot>({ key: '', status: 'loading' })
  const [nonce, setNonce] = useState(0)
  const cache = useRef(new Map<string, WhatIfResult>())
  useEffect(() => {
    const hit = cache.current.get(key)
    if (hit) {
      setSlot({ key, status: 'ok', data: hit, last: hit })
      return
    }
    const ctrl = new AbortController()
    setSlot((s) => ({ key, status: 'loading', last: s.last }))
    api.whatif.run(runId, JSON.parse(key) as WhatIfRequest, { signal: ctrl.signal }).then((data) => {
      cache.current.set(key, data)
      setSlot({ key, status: 'ok', data, last: data })
    }, (error: unknown) => {
      if ((error as { name?: string } | null)?.name === 'AbortError') return
      setSlot((s) => ({ key, status: 'error', error, last: s.last }))
    })
    return () => ctrl.abort()
  }, [runId, key, nonce])

  // 改动之后、请求还没发出去的那一下也算在路上：别拿上一次的答复当这次的
  const status = slot.key === key ? slot.status : 'loading'
  const data = status === 'ok' ? slot.data : slot.last
  const loading = status === 'loading'
  const m: WhatIfMetric | undefined = data?.metrics.find((x) => x.id === metric) ?? data?.metrics[0]
  const unsupported = (data?.unsupported ?? []).filter((u) => u.metric === metric)
  const unknown = !!m && m.rendered_new == null
  const changedSegs = (data?.segments ?? []).filter((s) => s.changed)

  return (
    <section id={id} className="mt-2 space-y-2.5 rounded border p-2" data-whatif="" aria-label={WHATIF_TEXT.title}
             style={{ borderColor: 'color-mix(in srgb, var(--st-waiting) 45%, var(--border))' }}>
      {/* 醒目靠底色、左边的色条和图标；字用正文色：提醒色的字压在同色的浅底上，浅色主题下对比度不到 4.5 */}
      <p role="note" className="flex items-start gap-1.5 rounded-r px-2 py-1.5 font-medium leading-snug" data-whatif-banner=""
         style={{ color: 'var(--text)', background: 'var(--st-waiting-soft)', borderLeft: '3px solid var(--st-waiting)' }}>
        <FlaskConical size={13} className="mt-px shrink-0" style={{ color: 'var(--st-waiting)' }} aria-hidden />
        <span>{WHATIF_TEXT.banner}</span>
      </p>
      {data && !data.sealed && (
        <p className="text-2xs" style={{ color: 'var(--st-waiting)' }} data-whatif-unsealed={data.seal.sealed ? 'broken' : 'open'}>
          {data.seal.sealed && data.seal.ok === false ? WHATIF_TEXT.sealBroken : WHATIF_TEXT.unsealed}
        </p>
      )}

      {status === 'error' && <ErrorNotice error={slot.error} onRetry={() => setNonce((n) => n + 1)} />}
      {!data && loading && (
        <p className="flex items-center gap-1.5 text-dim" data-whatif-loading=""><Spinner size={12} /> {WHATIF_TEXT.loading}</p>
      )}

      {m && status !== 'error' && (
        <div aria-live="polite" aria-busy={loading || undefined} className={clsx('transition-opacity', loading && 'opacity-60')}
             data-whatif-result={loading ? 'loading' : m.status}>
          <div className="mb-0.5 text-2xs font-medium text-faint">{WHATIF_TEXT.result}</div>
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <span className="mono tnum text-dim" data-whatif-old="">{m.rendered_old}</span>
            <span className="text-faint" aria-hidden>→</span>
            <span className="mono tnum text-sm font-semibold" data-whatif-new=""
                  style={m.changed ? { color: 'var(--accent)' } : undefined}>{m.rendered_new ?? NONE}</span>
            {m.rendered_delta
              ? <span className="tnum text-2xs" data-whatif-delta="">{WHATIF_TEXT.delta(m.rendered_delta)}</span>
              : m.changed === false && <span className="text-2xs text-faint" data-whatif-delta="">{WHATIF_TEXT.unchanged}</span>}
            {loading && <span className="text-faint"><Spinner size={11} /></span>}
          </div>
          {m.reason && <p className="mt-0.5" style={{ color: 'var(--st-waiting)' }} data-whatif-reason="">{m.reason}</p>}
          {m.status === 'ok' && m.changed && m.substituted && (
            <div className="mt-1">
              <div className="text-2xs text-faint">{WHATIF_TEXT.substituted}</div>
              <code className="mono block whitespace-pre-wrap break-all rounded bg-bg px-1.5 py-1 text-2xs" data-whatif-substituted="">
                {m.substituted}
              </code>
            </div>
          )}
        </div>
      )}

      {m && (
        <div data-whatif-inputs="">
          <div className="mb-1 text-2xs font-medium text-faint">{WHATIF_TEXT.inputs}</div>
          {m.inputs.length ? (
            <ul className="space-y-1.5">
              {m.inputs.map((inp, i) => (
                <InputRow key={inp.path} id={`${id}-in${i}`} input={inp} draft={draft} link={queryOf(inp.artifact)} />
              ))}
            </ul>
          ) : <p className="text-dim" data-whatif-empty="">{WHATIF_TEXT.empty}</p>}
        </div>
      )}

      {unsupported.length > 0 && (
        <div data-whatif-unsupported="">
          <div className="mb-0.5 text-2xs font-medium text-faint">{WHATIF_TEXT.unsupported}</div>
          <ul className="space-y-0.5">
            {unsupported.map((u) => (
              <li key={u.input ?? ''} className="[overflow-wrap:anywhere]" data-whatif-unsupported-input={u.input ?? ''}>
                <span className="mono">{u.input ?? WHATIF_TEXT.wholeMetric}</span>
                <span className="text-dim">：{u.reason}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {m && !!data?.segments.length && (
        <div data-whatif-segments="">
          <div className="mb-0.5 text-2xs font-medium text-faint">{WHATIF_TEXT.segments}</div>
          {unknown ? <p className="text-dim">{WHATIF_TEXT.segmentsUnknown}</p>
            : changedSegs.length ? (
              <ul className="space-y-0.5">
                {changedSegs.map((s) => (
                  <li key={`${s.report}-${s.sid}`} className="flex flex-wrap items-center gap-x-1.5" data-whatif-segment={s.sid}>
                    <span className="mono tnum text-dim">{s.rendered_old}</span>
                    <span className="text-faint" aria-hidden>→</span>
                    <span className="mono tnum font-semibold">{s.rendered_new}</span>
                    {onLocate && (!report || s.report === report)
                      ? (
                        <button type="button" className="btn btn-xs btn-ghost" onClick={() => onLocate(s.sid)}
                                data-whatif-locate={s.sid}>
                          <LocateFixed size={10} aria-hidden /> {WHATIF_TEXT.locate}
                        </button>
                      )
                      : <span className="text-2xs text-faint">{WHATIF_TEXT.inReport(s.report)}</span>}
                  </li>
                ))}
              </ul>
            ) : <p className="text-dim" data-whatif-segments-none="">{WHATIF_TEXT.segmentsNone(data.segments.length)}</p>}
        </div>
      )}

      {!!data?.affected_cards.length && (
        <div data-whatif-affected="">
          <div className="text-2xs font-medium text-faint">{WHATIF_TEXT.affected}</div>
          <p className="mb-0.5 text-2xs text-faint">{WHATIF_TEXT.affectedHint}</p>
          <ul className="space-y-0.5">
            {data.affected_cards.map((c) => (
              <li key={c.node_id} data-whatif-affected-card={c.node_id}>
                {WHATIF_TEXT.affectedCard(c.caliber, c.version)}
                {!c.direct && <span className="text-faint"> · {WHATIF_TEXT.indirect}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="flex items-center gap-2 border-t pt-2">
        <button type="button" className="btn btn-xs" onClick={draft.reset} disabled={!draft.dirty} data-whatif-reset="">
          <RotateCcw size={11} aria-hidden /> {WHATIF_TEXT.reset}
        </button>
        {loading && data && <span className="flex items-center gap-1 text-2xs text-faint"><Spinner size={11} /> {WHATIF_TEXT.loading}</span>}
      </div>
    </section>
  )
}

/** 一个输入：能改的给输入框（原值写在旁边），别的照实显示；排除了几行、去哪一步勾行，写在下面 */
function InputRow({ id, input, draft, link }: {
  id: string; input: WhatIfInput; draft: WhatIfDraft; link?: { alias: string; go: () => void }
}) {
  const raw = draft.text[input.path]
  const committed = draft.overrides.inputs[input.path]
  const baseline = committed !== undefined ? String(committed) : plain(input.old)
  const invalid = raw !== undefined && parseNumber(raw) === null
  const onKeyDown = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (isComposing(e)) return
    if (e.key === 'Enter') {
      e.preventDefault()
      draft.commit()
    } else if (e.key === 'Escape' && raw !== undefined && raw !== baseline) {
      // 改了一半的值先退回去：这一下 Esc 不关面板
      e.preventDefault()
      e.stopPropagation()
      draft.revert(input.path)
    }
  }
  const fromQuery = !!link && !input.masked && (input.kind === 'container' || !!input.locator)
  return (
    <li className="rounded border px-2 py-1.5" data-whatif-input={input.path} data-whatif-kind={input.kind}
        style={input.changed ? { borderColor: 'color-mix(in srgb, var(--accent) 55%, var(--border))' } : undefined}>
      <div className="mono truncate text-2xs text-dim" title={input.path}>{input.path}</div>
      {input.masked ? (
        <p className="mt-0.5 italic text-faint" data-whatif-masked="">{WHATIF_TEXT.masked}</p>
      ) : input.editable ? (
        <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1">
          <input id={id} type="text" inputMode="decimal" autoComplete="off" spellCheck={false}
                 className={clsx('field mono tnum w-36 min-w-0 py-1', invalid && 'border-[var(--err)]')}
                 value={raw ?? plain(input.old)} placeholder={input.old == null ? NONE : undefined}
                 aria-label={WHATIF_TEXT.inputLabel(input.path)} aria-invalid={invalid || undefined}
                 aria-describedby={invalid ? `${id}-err` : undefined}
                 onChange={(e) => draft.type(input.path, e.target.value, input.old)}
                 onKeyDown={onKeyDown} onBlur={draft.commit} data-whatif-field="" />
          <span className="tnum text-2xs text-faint">{WHATIF_TEXT.original(valueText(input.old))}</span>
          {invalid && <span id={`${id}-err`} className="text-2xs" style={{ color: 'var(--err)' }}>{WHATIF_TEXT.notNumber}</span>}
        </div>
      ) : (
        <p className="mono tnum mt-0.5 [overflow-wrap:anywhere]" data-whatif-value="">
          {valueText(input.old)}
          {input.changed && <> <span className="text-faint" aria-hidden>→</span> {valueText(input.new)}</>}
        </p>
      )}
      {!!input.excluded_rows?.length && (
        <p className="mt-0.5 text-2xs" style={{ color: 'var(--st-waiting)' }} data-whatif-excluded="">
          {WHATIF_TEXT.excludedRows(input.excluded_rows)}
        </p>
      )}
      {fromQuery && (
        <button type="button" className="btn btn-xs btn-ghost -ml-1 mt-0.5" onClick={link!.go} data-whatif-pick="">
          <CornerDownRight size={10} aria-hidden /> {WHATIF_TEXT.pickRows(link!.alias)}
        </button>
      )}
    </li>
  )
}

/**
 * 推演时查询步骤里的结果窗口：每行前面一个勾选框，勾掉的行在推演里视为不存在（划掉、变淡）。只有知道每一行
 * 在快照里是第几行时才画（index）；被遮罩的列照样写「已遮罩」，高亮的格和平时一样描边
 */
export function WhatIfRows({ win, hidden, excluded, onToggle }: {
  win: ReturnType<typeof queryWindow>; hidden: string[]; excluded: number[]; onToggle: (row: number) => void
}) {
  if (!win.index) return null
  const index = win.index
  const masked = new Set(hidden)
  const cells = win.cells ? new Set(win.cells.map(([r, c]) => `${r}\u0000${c}`)) : null
  const marked = new Set([...win.marks, ...(win.cells ?? []).map(([r]) => r)])
  const cols = new Set(win.cols)
  return (
    <div className="overflow-hidden rounded border" data-whatif-rows="">
      <div className="overflow-x-auto">
        <table className="w-full text-2xs">
          <thead>
            <tr className="border-b bg-elev">
              {/* 勾选框和行号两列只占自己的宽度：表格撑满窄栏时多出来的宽度留给数据列 */}
              <th className="w-px whitespace-nowrap px-1.5 py-1 text-left font-medium text-dim">{WHATIF_TEXT.excludeHead}</th>
              <th className="mono w-px px-1.5 py-1 text-right font-medium text-dim">#</th>
              {win.columns.map((c) => <th key={c} className="mono px-1.5 py-1 text-left font-medium text-dim">{c}</th>)}
            </tr>
          </thead>
          <tbody>
            {win.rows.map((row, i) => {
              const n = index[i]
              const off = excluded.includes(n)
              const hit = marked.has(i)
              return (
                <tr key={n} className="border-b last:border-0" data-whatif-row={n} data-excluded={off ? '' : undefined}
                    style={hit && !off ? { background: 'var(--st-done-soft)' } : undefined}>
                  <td className="w-px px-1.5 py-1 text-center">
                    <input type="checkbox" className="align-middle" checked={off} onChange={() => onToggle(n)}
                           aria-label={WHATIF_TEXT.excludeRow(n)} />
                  </td>
                  <td className="mono w-px px-1.5 py-1 text-right text-faint">{n + 1}</td>
                  {row.map((cell, j) => {
                    const col = win.columns[j]
                    const mark = hit && !off && (cells ? cells.has(`${i}\u0000${col}`) : cols.has(col))
                    const mask = masked.has(col)
                    return (
                      <td key={j} className={clsx('mono max-w-[160px] truncate px-1.5 py-1',
                        off ? 'text-faint line-through' : mark ? 'font-semibold' : 'text-dim')}
                          data-highlight={mark ? 'cell' : undefined} data-masked={mask ? '' : undefined}
                          style={mark ? { outline: '1px solid var(--st-done)', outlineOffset: -1, color: 'var(--text)' } : undefined}
                          title={mask ? WHATIF_TEXT.masked : cell == null ? '' : String(cell)}>
                        {mask ? <span className="font-sans italic text-faint">{WHATIF_TEXT.masked}</span>
                          : cell == null ? <span className="opacity-40">null</span>
                          : typeof cell === 'object' ? JSON.stringify(cell) : String(cell)}
                      </td>
                    )
                  })}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <div className="flex flex-wrap items-center gap-x-2 border-t px-1.5 py-1 text-2xs text-faint">
        <span>{WHATIF_TEXT.rowsHint}</span>
        {excluded.length > 0 && (
          <span style={{ color: 'var(--st-waiting)' }} data-whatif-excluded-count="">{WHATIF_TEXT.excludedCount(excluded.length)}</span>
        )}
      </div>
    </div>
  )
}
