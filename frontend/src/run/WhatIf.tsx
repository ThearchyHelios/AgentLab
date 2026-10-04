import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from 'react'
import clsx from 'clsx'
import { CornerDownRight, FlaskConical, ListChecks, LocateFixed, RotateCcw } from 'lucide-react'
import { api } from '../api/client'
import { ErrorNotice, Spinner, isComposing } from '../components/ui'
import { evidenceValue as valueText, queryWindow } from '../lib/evidence'
import { NONE, formatNumber } from '../lib/format'
import { WHATIF_TEXT } from '../lib/terms'
import type { EvidenceStep, WhatIfInput, WhatIfMetric, WhatIfOverrides, WhatIfRequest, WhatIfResult, WhatIfRowsPage } from '../types'

/**
 * 数字推演：证据面板里点开口径卡上的一个数，改某个输入的值、或者在查询结果里勾掉几行，立即看到这个指标和报告里
 * 引用它的地方会变成多少（POST …/evidence/whatif，后端 engine/recompute.py 按口径卡同一套规则重算）。
 *
 * 推演永远不是出具：不写进运行记录、不改报告。顶上的横幅一直在，运行没封存时另说「数据可能还会变」。
 *
 * 几样东西分开放：
 * - useWhatIfDraft：改动的草稿（输入框里的原文、提交了的新值、勾掉的行）
 * - useWhatIfResult：推演的请求和答复。答复按请求体缓存，「恢复原值」回到原来那份不再请求；新的改动来了旧请求直接丢掉
 *   这两样都放在证据面板的片段那一层（SegmentBody）：推演区和查询步骤里勾行的表格共用一份草稿、一份答复（勾了行，
 *   表格底下就写着新值）。同一个指标的几处引用之间来回切不清空，换了指标才清空
 * - WhatIf：推演区。输入框停顿一会儿（或回车、失焦）才提交，勾行立刻提交。请求在路上时，凡是随改动变的地方（结果、
 *   报告里会变的地方、下游口径卡、输入上的新值和排除标记）都变淡；请求失败时这些一概不显示，不拿上一次的答复充数
 * - WhatIfRows：勾行的表格。推演时查询步骤里的结果窗口换成它；整份结果、数组字段作输入时，推演区里另给一份按页取
 *   整份快照的（SnapshotRows）。只有这个指标的输入实际用到的行可勾（row_span），别的行勾了也不影响结果
 *
 * 不能改的输入（文本、对不上快照的列表、遮罩列、决定取哪一格的下标）照实显示，原因列在「不支持推演的输入」里。
 * 整个指标推演不了的（口径卡没记输入、用到了遮罩列、有输入还原不了或所在的快照读不出来，见 wholeReason）只写原因：
 * 不写「无变化」，不画输入框、勾选框，也不列报告里引用它的地方——改了、勾了也推演不出来。
 */

/** 输入框停顿多久提交一次：边打字边请求，结果跟着抖 */
const SETTLE_MS = 450
/** 推演区里勾行时一次取几行 */
const ROWS_PAGE = 50

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

/** 推演的答复：loading 是这次改动的答复还没回来、error 是这次的请求失败了，这两种时候 data 是上一次的 */
export interface WhatIfView {
  status: 'loading' | 'ok' | 'error'
  data?: WhatIfResult
  error?: unknown
  retry: () => void
}

type Slot = { key: string; scope: string; status: 'loading' | 'ok' | 'error'; data?: WhatIfResult; error?: unknown; last?: WhatIfResult }

/**
 * 发推演请求、收答复。request 为 null（推演收着）时不发。改动之后、请求还没发出去的那一下也算在路上：别拿上一次的
 * 答复当这次的。上一次的答复只在同一个指标（运行、报告、卡、指标都一样）之间沿用，换了指标不拿别的指标的数充数
 */
export function useWhatIfResult(runId: string | undefined, request: WhatIfRequest | null): WhatIfView {
  const key = runId && request ? JSON.stringify([runId, request]) : ''
  const scope = runId && request ? JSON.stringify([runId, request.report ?? '', request.card, request.metric ?? '']) : ''
  const [slot, setSlot] = useState<Slot>({ key: '', scope: '', status: 'loading' })
  const [nonce, setNonce] = useState(0)
  const cache = useRef(new Map<string, WhatIfResult>())
  useEffect(() => {
    if (!key || !runId) return
    const hit = cache.current.get(key)
    if (hit) {
      setSlot({ key, scope, status: 'ok', data: hit, last: hit })
      return
    }
    const ctrl = new AbortController()
    setSlot((s) => ({ key, scope, status: 'loading', last: s.scope === scope ? s.last : undefined }))
    const [, body] = JSON.parse(key) as [string, WhatIfRequest]
    api.whatif.run(runId, body, { signal: ctrl.signal }).then((data) => {
      cache.current.set(key, data)
      setSlot({ key, scope, status: 'ok', data, last: data })
    }, (error: unknown) => {
      if ((error as { name?: string } | null)?.name === 'AbortError') return
      setSlot((s) => ({ key, scope, status: 'error', error, last: s.scope === scope ? s.last : undefined }))
    })
    return () => ctrl.abort()
  }, [runId, key, scope, nonce])
  const retry = useCallback(() => setNonce((n) => n + 1), [])
  const status = slot.key === key ? slot.status : 'loading'
  const last = slot.scope === scope ? slot.last : undefined
  return { status, data: status === 'ok' ? slot.data : last, error: status === 'error' ? slot.error : undefined, retry }
}

/** 答复里要推演的那个指标 */
export function pickMetric(data: WhatIfResult | undefined, metric: string | undefined): WhatIfMetric | undefined {
  return data?.metrics.find((x) => String(x.id) === metric) ?? data?.metrics[0]
}

/**
 * 整个指标推演不了（口径卡没记输入、用到了遮罩列、有输入还原不了或所在的快照读不出来）：unsupported 里 input 为空的
 * 那一条。不论改什么都推演不了，后端不改动也照实给；排除行后才说不准的（只在这次改动下推演不了）没有这一条，照常画
 * 输入和勾选框，好让人把行勾回去
 */
export const wholeReason = (data: WhatIfResult | undefined, metric: string | undefined) =>
  data?.unsupported.find((u) => String(u.metric) === metric && u.input == null)?.reason

/** 每份快照里这个指标用得到的行段（含两端）：各输入的 row_span。答复还没回来时是 undefined */
export function usableRows(m: WhatIfMetric | undefined): Map<string, [number, number][]> | undefined {
  if (!m) return undefined
  const out = new Map<string, [number, number][]>()
  for (const inp of m.inputs) {
    if (inp.artifact && inp.row_span) out.set(inp.artifact, [...(out.get(inp.artifact) ?? []), inp.row_span])
  }
  return out
}

export const inSpans = (spans: [number, number][] | undefined, row: number) =>
  !!spans?.some(([a, b]) => row >= a && row <= b)

/** 「回到推演结果」：焦点送回推演结果（推演不了时送回推演区），滚进视野、描一下边 */
export function focusResult(id: string) {
  const el = document.getElementById(`${id}-result`) ?? document.getElementById(id)
  if (!el) return
  el.focus({ preventScroll: true })
  el.scrollIntoView({ block: 'nearest' })
  el.dataset.flash = 'focus'
  setTimeout(() => { if (el.dataset.flash === 'focus') delete el.dataset.flash }, 1600)
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

/**
 * 推演结果的旧值 → 新值（或原因）、变化量。推演区（big：新值大一号）和勾行的表格底下都用它；tag 是各部分的
 * data-* 前缀，两处分开，检查脚本不会把表格底下那份当成推演区的
 */
function Outcome({ m, loading, big = false, tag }: { m: WhatIfMetric; loading: boolean; big?: boolean; tag: string }) {
  const at = (part: string) => ({ [`data-${tag}-${part}`]: '' })
  return (
    <span className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
      <span className="mono tnum text-dim" {...at('old')}>{m.rendered_old}</span>
      <span className="text-faint" aria-hidden>→</span>
      <span className={clsx('mono tnum font-semibold', big && 'text-sm')} {...at('new')}
            style={m.changed ? { color: 'var(--accent)' } : undefined}>{m.rendered_new ?? NONE}</span>
      {m.rendered_delta
        ? <span className="tnum text-2xs" {...at('delta')}>{WHATIF_TEXT.delta(m.rendered_delta)}</span>
        : m.changed === false && <span className="text-2xs text-faint" {...at('delta')}>{WHATIF_TEXT.unchanged}</span>}
      {loading && <span className="text-faint"><Spinner size={11} /></span>}
    </span>
  )
}

/**
 * 勾行的表格底下带的推演结果：勾了一行，新值（或推演不了的原因）就在手边，不用回头找。「回到推演结果」把焦点送回
 * 推演区。读屏器由推演区的结果播报，这里不再重复
 */
export function WhatIfSummary({ view, metric, onBack }: { view: WhatIfView; metric: string; onBack?: () => void }) {
  const m = pickMetric(view.data, metric)
  const loading = view.status === 'loading'
  const failed = view.status === 'error'
  const whole = wholeReason(view.data, metric)
  const reason = failed ? undefined : whole ?? m?.reason
  return (
    <div className="border-t px-1.5 py-1 text-2xs" data-whatif-summary={failed ? 'error' : loading ? 'loading' : m?.status ?? ''}>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
        <span className="font-medium text-faint">{WHATIF_TEXT.result}</span>
        {failed
          ? <span style={{ color: 'var(--err)' }}>{WHATIF_TEXT.failed}</span>
          : m && !whole
            ? <span className={clsx('text-xs transition-opacity', loading && 'opacity-60')}><Outcome m={m} loading={loading} tag="whatif-summary" /></span>
            : !m && <Spinner size={11} />}
        {onBack && (
          <button type="button" className="btn btn-xs btn-ghost ml-auto" onClick={onBack} data-whatif-back="">
            {WHATIF_TEXT.backToResult}
          </button>
        )}
      </div>
      {reason && (
        <p className={clsx('mt-0.5 [overflow-wrap:anywhere] transition-opacity', loading && 'opacity-60')}
           style={{ color: 'var(--st-waiting)' }}>{reason}</p>
      )}
    </div>
  )
}

/** 推演区：横幅、结果、输入、不支持的输入、报告里会变的地方、下游口径卡、恢复原值 */
export function WhatIf({ id, runId, report, metric, draft, view, queryOf, onLocate }: {
  id: string
  runId: string
  /** 正文这份报告的撰写节点 id：报告里会变的地方只列它的（都能定位） */
  report?: string
  metric: string
  draft: WhatIfDraft
  view: WhatIfView
  /** 输入落在哪个查询步骤上：有的话给「在查询 Qn 中勾选要排除的行」，点一下跳过去 */
  queryOf: (artifact?: string) => { alias: string; go: () => void } | undefined
  /** 定位到正文里的一个片段 */
  onLocate?: (segId: string) => void
}) {
  const { status, data } = view
  const loading = status === 'loading'
  const failed = status === 'error'
  const m = pickMetric(data, metric)
  const unsupported = (data?.unsupported ?? []).filter((u) => String(u.metric) === metric)
  // 整个指标推演不了（口径卡没记输入、用到了遮罩列、有输入还原不了）：只说原因，不画输入、不写「无变化」
  const whole = wholeReason(data, metric)
  const unknown = !!m && m.rendered_new == null
  const changedSegs = (data?.segments ?? []).filter((s) => s.changed)
  const adjustable = !!m?.inputs.some((i) => i.editable || i.row_span)
  const summary = <WhatIfSummary view={view} metric={metric} onBack={() => focusResult(id)} />

  return (
    <section id={id} tabIndex={-1} className="mt-2 space-y-2.5 rounded border p-2 focus:outline-none" data-whatif=""
             aria-label={WHATIF_TEXT.title} style={{ borderColor: 'color-mix(in srgb, var(--st-waiting) 45%, var(--border))' }}>
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

      {failed && <ErrorNotice error={view.error} onRetry={view.retry} />}
      {!data && loading && (
        <p className="flex items-center gap-1.5 text-dim" data-whatif-loading=""><Spinner size={12} /> {WHATIF_TEXT.loading}</p>
      )}

      {m && !failed && (
        // 「回到推演结果」把焦点送到这里：描边由 focusResult 设的 data-flash 画（静态描边，减少动效时照样看得见）
        <div id={`${id}-result`} tabIndex={-1} aria-live="polite" aria-busy={loading || undefined}
             className={clsx('rounded-sm transition-opacity focus:outline-none data-[flash]:outline data-[flash]:outline-2 data-[flash]:outline-offset-2 data-[flash=focus]:outline-[color:var(--accent)]',
               loading && 'opacity-60')}
             data-whatif-result={loading ? 'loading' : whole ? 'whole' : m.status}>
          <div className="mb-0.5 text-2xs font-medium text-faint">{WHATIF_TEXT.result}</div>
          {whole ? (
            <>
              <span className="mono tnum text-dim" data-whatif-old="">{m.rendered_old}</span>
              <p className="mt-0.5 [overflow-wrap:anywhere]" style={{ color: 'var(--st-waiting)' }} data-whatif-reason="">{whole}</p>
            </>
          ) : (
            <>
              <Outcome m={m} loading={loading} big tag="whatif" />
              {m.reason && (
                <p className="mt-0.5 [overflow-wrap:anywhere]" style={{ color: 'var(--st-waiting)' }} data-whatif-reason="">{m.reason}</p>
              )}
              {m.status === 'ok' && m.changed && m.substituted && (
                <div className="mt-1">
                  <div className="text-2xs text-faint">{WHATIF_TEXT.substituted}</div>
                  <code className="mono block whitespace-pre-wrap break-all rounded bg-bg px-1.5 py-1 text-2xs" data-whatif-substituted="">
                    {m.substituted}
                  </code>
                </div>
              )}
            </>
          )}
        </div>
      )}

      {m && !whole && (
        <div data-whatif-inputs="">
          <div className="mb-1 text-2xs font-medium text-faint">{WHATIF_TEXT.inputs}</div>
          {m.inputs.length ? (
            <>
              <ul className="space-y-1.5">
                {m.inputs.map((inp, i) => (
                  <InputRow key={inp.path} id={`${id}-in${i}`} runId={runId} input={inp} draft={draft}
                            link={queryOf(inp.artifact)} status={status} summary={summary} />
                ))}
              </ul>
              {!adjustable && <p className="mt-1 text-2xs text-dim" data-whatif-nothing="">{WHATIF_TEXT.nothingToAdjust}</p>}
            </>
          ) : <p className="text-dim" data-whatif-empty="">{WHATIF_TEXT.empty}</p>}
        </div>
      )}

      {!whole && unsupported.length > 0 && (
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

      {m && !whole && !failed && !!data?.segments.length && (
        <div aria-busy={loading || undefined} className={clsx('transition-opacity', loading && 'opacity-60')}
             data-whatif-segments={loading ? 'loading' : ''}>
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

      {!whole && !failed && !!data?.affected_cards.length && (
        <div aria-busy={loading || undefined} className={clsx('transition-opacity', loading && 'opacity-60')}
             data-whatif-affected={loading ? 'loading' : ''}>
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

/**
 * 一个输入：能改的给输入框（原值写在旁边），别的照实显示；排除了几行、去哪勾行，写在下面。
 * 随改动变的新值、排除标记来自答复：这次改动的答复还没回来时（loading）是上一次的，变淡；请求失败了（error）不显示
 */
function InputRow({ id, runId, input, draft, link, status, summary }: {
  id: string; runId: string; input: WhatIfInput; draft: WhatIfDraft; link?: { alias: string; go: () => void }
  status: WhatIfView['status']; summary: ReactNode
}) {
  const shown = status !== 'error'
  const dim = status === 'loading' ? 'opacity-60' : undefined
  const [picking, setPicking] = useState(false)
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
  const span = input.masked ? undefined : input.row_span
  // 只用到快照里一行的（单元格取数、Agent 的单格字段）去查询步骤里勾：那儿有前后几行可对照；整份结果、数组字段
  // 用到一大段行，查询步骤只给前后几行，在这里按页勾全；单行却没有查询步骤可去的，也在这里勾
  const single = !!span && span[0] === span[1]
  const pickInQuery = !!link && single && input.kind !== 'container'
  const pickHere = !!span && !pickInQuery
  return (
    <li className="rounded border px-2 py-1.5" data-whatif-input={input.path} data-whatif-kind={input.kind}
        style={input.changed && shown ? { borderColor: 'color-mix(in srgb, var(--accent) 55%, var(--border))' } : undefined}>
      <div className="mono truncate text-2xs text-dim" title={input.path}>{input.path}</div>
      {input.masked ? (
        <p className="mt-0.5 italic text-faint" data-whatif-masked="">{WHATIF_TEXT.masked}</p>
      ) : input.withheld ? (
        <p className="mt-0.5 italic text-faint" data-whatif-withheld="">{WHATIF_TEXT.withheld}</p>
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
          {input.changed && shown && (
            <span className={dim}> <span className="text-faint" aria-hidden>→</span> {valueText(input.new)}</span>
          )}
        </p>
      )}
      {!!input.excluded_rows?.length && shown && (
        <p className={clsx('mt-0.5 text-2xs', dim)} style={{ color: 'var(--st-waiting)' }} data-whatif-excluded="">
          {WHATIF_TEXT.excludedRows(input.excluded_rows)}
        </p>
      )}
      {pickInQuery && (
        <button type="button" className="btn btn-xs btn-ghost -ml-1 mt-0.5" onClick={link!.go} data-whatif-pick="">
          <CornerDownRight size={10} aria-hidden /> {WHATIF_TEXT.pickRows(link!.alias)}
        </button>
      )}
      {pickHere && input.artifact && span && (
        <>
          <button type="button" className="btn btn-xs btn-ghost -ml-1 mt-0.5" aria-expanded={picking}
                  onClick={() => setPicking((v) => !v)} data-whatif-pick-here={picking ? 'open' : 'closed'}>
            <ListChecks size={10} aria-hidden /> {picking ? WHATIF_TEXT.pickHereClose : WHATIF_TEXT.pickHere(span[1] - span[0] + 1)}
          </button>
          {picking && (
            <SnapshotRows key={`${input.artifact}:${span[0]}:${span[1]}`} runId={runId} artifact={input.artifact} span={span}
                          excluded={draft.excluded(input.artifact)} onToggle={(row) => draft.toggleRow(input.artifact!, row)}
                          summary={summary} busy={status === 'loading'} />
          )}
        </>
      )}
    </li>
  )
}

type Loaded = { status: 'loading' | 'ok' | 'error'; page?: WhatIfRowsPage; rows: unknown[][]; index: number[]; error?: unknown }

/**
 * 推演区里的勾行表格：按页取这份快照里输入用到的那一段行（整份结果是全部，数组字段是它那一段），遮罩列写「已遮罩」。
 * 查询步骤只给被引用的行前后几行，整份结果作输入时大多数行勾不到，所以这里另取
 */
function SnapshotRows({ runId, artifact, span, excluded, onToggle, summary, busy }: {
  runId: string; artifact: string; span: [number, number]; excluded: number[]; onToggle: (row: number) => void
  summary: ReactNode; busy: boolean
}) {
  const [first, last] = span
  const [loaded, setLoaded] = useState<Loaded>({ status: 'loading', rows: [], index: [] })
  const [offset, setOffset] = useState(first)
  const [nonce, setNonce] = useState(0)
  useEffect(() => {
    const ctrl = new AbortController()
    setLoaded((s) => ({ ...s, status: 'loading' }))
    api.whatif.rows(runId, artifact, { offset, limit: Math.min(ROWS_PAGE, last - offset + 1) }, { signal: ctrl.signal })
      .then((page) => setLoaded((s) => {
        // 只留输入用到的那一段（数组字段的行段后面可能还有别的行），已经有的不重复接
        const have = new Set(s.index)
        const keep = page.row_index.map((r, i) => [r, i] as const).filter(([r]) => r >= first && r <= last && !have.has(r))
        return { status: 'ok', page, rows: [...s.rows, ...keep.map(([, i]) => page.rows[i])], index: [...s.index, ...keep.map(([r]) => r)] }
      }), (error: unknown) => {
        if ((error as { name?: string } | null)?.name === 'AbortError') return
        setLoaded((s) => ({ ...s, status: 'error', error }))
      })
    return () => ctrl.abort()
  }, [runId, artifact, offset, first, last, nonce])
  const page = loaded.page
  const win = useMemo(() => queryWindow({
    rows: loaded.rows, columns: page?.columns ?? [], row_index: loaded.index, row_offset: loaded.index[0] ?? first,
    total_rows: page?.total_rows ?? null,
  } as Pick<EvidenceStep, 'rows' | 'columns' | 'row_offset' | 'row_index' | 'total_rows' | 'highlight'>), [loaded.rows, loaded.index, page, first])
  const next = (loaded.index.at(-1) ?? first - 1) + 1
  const remaining = Math.max(0, last - next + 1)
  return (
    <div className="mt-1 space-y-1" data-whatif-snapshot={artifact}>
      {loaded.status === 'error' && <ErrorNotice error={loaded.error} onRetry={() => setNonce((n) => n + 1)} />}
      {page?.note && <p className="text-2xs text-dim" data-whatif-snapshot-note="">{page.note}</p>}
      {!page && loaded.status === 'loading' && (
        <p className="flex items-center gap-1.5 text-2xs text-dim"><Spinner size={11} /> {WHATIF_TEXT.rowsLoading}</p>
      )}
      {win.rows.length > 0 && (
        <WhatIfRows win={win} hidden={page?.masked ?? []} excluded={excluded} onToggle={onToggle}
                    usable={(row) => row >= first && row <= last} summary={summary} busy={busy} />
      )}
      {win.rows.length > 0 && (
        <div className="flex flex-wrap items-center gap-x-2 text-2xs text-faint">
          <span data-whatif-snapshot-range="">
            {WHATIF_TEXT.rowsRange(formatNumber(first + 1), formatNumber(next), formatNumber(page?.total_rows ?? last + 1))}
          </span>
          {remaining > 0 && (
            <button type="button" className="btn btn-xs btn-ghost" disabled={loaded.status === 'loading'}
                    onClick={() => setOffset(next)} data-whatif-snapshot-more="">
              {loaded.status === 'loading' ? <Spinner size={10} /> : null} {WHATIF_TEXT.rowsMore(Math.min(ROWS_PAGE, remaining))}
            </button>
          )}
        </div>
      )}
    </div>
  )
}

/**
 * 勾行的表格：每行前面一个勾选框，勾掉的行在推演里视为不存在（划掉、变淡）。只有知道每一行在快照里是第几行时才画
 * （index）；被遮罩的列照样写「已遮罩」，高亮的格和平时一样描边。usable 说哪几行这个指标用得到：别的行勾了也不影响
 * 结果，勾选框禁用并说明（已经勾上的照样能取消）；答复还没回来、不知道时一律先不让勾。
 *
 * summary（推演结果）连同说明画在表格底下：推演区在上面，勾完行它往往不在视野里。勾了一行就把底下这条带进视野
 * （已经看得见就不动），答复回来时（busy 转为假）再带一次：上面的内容（原因、下游口径卡）这时会变高，把表格往下挤。
 * 不做成贴底的浮条：表格靠近视野底边时浮条会盖住刚勾的那一行
 */
export function WhatIfRows({ win, hidden, excluded, onToggle, usable, summary, busy = false }: {
  win: ReturnType<typeof queryWindow>; hidden: string[]; excluded: number[]; onToggle: (row: number) => void
  usable?: (row: number) => boolean
  summary?: ReactNode
  /** 推演请求还在路上 */
  busy?: boolean
}) {
  const foot = useRef<HTMLDivElement>(null)
  const reveal = useRef(false)
  useEffect(() => {
    if (!reveal.current || busy) return
    reveal.current = false
    foot.current?.scrollIntoView({ block: 'nearest' })
  })
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
              const open = off || (usable ? usable(n) : false)
              return (
                <tr key={n} className="border-b last:border-0" data-whatif-row={n} data-excluded={off ? '' : undefined}
                    data-whatif-row-unused={open ? undefined : ''}
                    style={hit && !off ? { background: 'var(--st-done-soft)' } : undefined}>
                  <td className="w-px px-1.5 py-1 text-center"
                      title={open ? undefined : usable ? WHATIF_TEXT.rowUnused : WHATIF_TEXT.rowsPending}>
                    <input type="checkbox" className="align-middle disabled:cursor-not-allowed disabled:opacity-40"
                           checked={off} disabled={!open}
                           onChange={() => {
                             onToggle(n)
                             // 勾完把底下的推演结果带进视野（已经看得见就不动）：从输入那儿跳过来时表格常在视野边上。
                             // 答复回来后再带一次（上面的 effect）
                             reveal.current = true
                             requestAnimationFrame(() => foot.current?.scrollIntoView({ block: 'nearest' }))
                           }}
                           aria-label={WHATIF_TEXT.excludeRow(n)} />
                  </td>
                  <td className={clsx('mono w-px px-1.5 py-1 text-right', open ? 'text-faint' : 'text-faint opacity-60')}>{n + 1}</td>
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
      <div ref={foot} data-whatif-rows-foot="">
        <div className="flex flex-wrap items-center gap-x-2 border-t px-1.5 py-1 text-2xs text-faint">
          <span>{WHATIF_TEXT.rowsHint}</span>
          {excluded.length > 0 && (
            <span style={{ color: 'var(--st-waiting)' }} data-whatif-excluded-count="">{WHATIF_TEXT.excludedCount(excluded.length)}</span>
          )}
        </div>
        {summary}
      </div>
    </div>
  )
}
