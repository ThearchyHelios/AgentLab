import { useEffect, useId, useMemo, useState } from 'react'
import type { CSSProperties } from 'react'
import { AlertTriangle, Check, ChevronRight } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../api/client'
import { useCatalog } from '../store/catalog'
import { useStudio } from '../store/studio'
import { Spinner, useToast } from '../components/ui'
import { NONE } from '../lib/format'
import { DUAL_CHECK_TEXT } from '../lib/terms'
import type {
  Approval, DualCheckBlindInput, DualCheckCard, DualCheckChoice, DualCheckInput, DualCheckMetric, DualCheckReply,
  DualCheckSummaryItem,
} from '../types'

const T = DUAL_CHECK_TEXT.approval
const CHOICES: DualCheckChoice[] = ['a', 'b', 'neither']

export interface DualCheckApprovalProps {
  /** payload.kind 为 'dual_check' 的审批：双盲复算两路对不上、交给人裁决 */
  approval: Approval
  /**
   * 提交之后谁来重新接上运行的事件流，同 ApprovalCard 的 onResolved：对话页、运行页各自接，没给就走画布的 attachRun。
   * 两处都接会开两条订阅，各收一份事件
   */
  onResolved?: (runId: string) => void | Promise<void>
}

/** 一个不一致的指标在卡上要画的东西：先由 payload.summary 填，完整对照（card_artifact）到了再补全 */
interface MetricView {
  id: string
  name: string
  original: string | null
  blind: string | null
  /** 已乘过 100；null 是原流程值为 0（约定），undefined 是没给 */
  deltaPct: number | null | undefined
  /** 具体差异，一条一句（兜底说明不算） */
  items: string[]
  /** 只有兜底说明：两条 SQL 的写法不同，未能归纳成具体差异 */
  fallbackOnly: boolean
  /** 具体差异之外还有未能归纳的部分（服务端那句「另有未能归纳的差异」） */
  rest: string | null
  /** 两条 SQL 等价，差在取用的结果上 */
  equivalent: boolean
  /** 只差一个方面：审核人的选择会记入数据目录 */
  single: boolean
  full: DualCheckMetric | null
}

const str = (v: unknown): string | undefined => (typeof v === 'string' && v ? v : undefined)
const num = (v: unknown): number | undefined => (typeof v === 'number' && Number.isFinite(v) ? v : undefined)

/** payload.summary 里认得出的项（老服务端、写坏了的项跳过，不让整张卡崩掉） */
function summaryOf(raw: unknown): DualCheckSummaryItem[] {
  return Array.isArray(raw)
    ? raw.filter((s): s is DualCheckSummaryItem => !!s && typeof s === 'object' && typeof s.metric_id === 'string' && !!s.metric_id)
    : []
}

/** 工件内容像不像一份对照：至少得有 metrics 数组，里面的项带 metric_id */
function cardOf(content: unknown): DualCheckCard | null {
  if (!content || typeof content !== 'object' || !Array.isArray((content as DualCheckCard).metrics)) return null
  const card = content as DualCheckCard
  return { ...card, metrics: card.metrics.filter((m) => !!m && typeof m === 'object' && typeof m.metric_id === 'string') }
}

function viewsOf(summary: DualCheckSummaryItem[], card: DualCheckCard | null): MetricView[] {
  const full = new Map((card?.metrics ?? []).map((m) => [m.metric_id, m]))
  // 先按摘要的顺序，工件里另有的（约定里不该有，有就照实列出来）跟在后面
  const ids = [...new Set([...summary.map((s) => s.metric_id), ...full.keys()])]
  return ids.map((id) => {
    const s = summary.find((x) => x.metric_id === id)
    const f = full.get(id) ?? null
    const base = {
      id,
      name: str(s?.name) ?? str(f?.name) ?? id,
      original: str(s?.original_rendered) ?? str(f?.original?.rendered) ?? null,
      blind: str(s?.blind_rendered) ?? str(f?.blind?.rendered) ?? null,
      deltaPct: s && 'delta_pct' in s ? num(s.delta_pct) ?? null : f?.delta_pct === null ? null : num(f?.delta_pct),
    }
    if (f) {
      const items = (Array.isArray(f.diff?.items) ? f.diff.items : []).filter((i) => !!i && typeof i.text === 'string' && !!i.text)
      // aspect 为 other 的是兜底说明，不算具体差异；只有它的时候卡上写那句固定的说法、默认展开对照
      const concrete = items.filter((i) => i.aspect !== 'other')
      const other = items.filter((i) => i.aspect === 'other')
      const equivalent = f.diff?.equivalent === true
      const aspects = new Set(concrete.map((i) => i.aspect))
      return {
        ...base,
        items: concrete.map((i) => i.text),
        fallbackOnly: !equivalent && !concrete.length,
        rest: concrete.length && other.length ? other.map((i) => i.text).join('；') : null,
        equivalent,
        // 「只差一个方面」以服务端为准（它决定写不写数据目录），这里只在没用兜底、具体差异都落在同一方面时提示
        single: !equivalent && f.diff?.fallback !== true && aspects.size === 1 && !aspects.has('parse'),
        full: f,
      }
    }
    return {
      ...base,
      items: Array.isArray(s?.diff) ? s.diff.filter((t): t is string => typeof t === 'string' && !!t) : [],
      fallbackOnly: s?.fallback === true,
      rest: null,
      equivalent: false,
      single: false,
      full: null,
    }
  })
}

/** 相差比例：服务端给的已乘过 100、保留两位（12.5 是相差 12.5%），带正负号；小到两位小数写不出来的写「<0.01%」 */
function deltaText(pct: number | null | undefined): string | null {
  if (pct === undefined) return null
  if (pct === null) return T.deltaZero
  const abs = Math.abs(pct)
  const sign = pct > 0 ? '+' : pct < 0 ? '-' : ''
  return T.delta(abs < 0.01 ? `${sign}<0.01%` : `${sign}${abs.toLocaleString('en-US', { maximumFractionDigits: 2 })}%`)
}

/**
 * 审批卡里双盲复算那一块（RunPanel 的 ApprovalCard 在 payload.kind === 'dual_check' 时渲染它，通用的输入框、备注和
 * 「提交 / 驳回并终止」不画——mode 是 input，通用那套会把一个字符串当 value 发出去）。
 *
 * 先按 payload.summary 画：两个值、相差多少、摘要里的差异，选择也能先做；完整对照（card_artifact）到了再补上盲算模型、
 * 全部具体差异和「对照两条 SQL」。取不到完整对照时照实说，按摘要照样能裁决。每个指标三选一、缺一个都不能提交，
 * 回复走审批接口：value 为 {choices, note}
 */
export function DualCheckApproval({ approval, onResolved }: DualCheckApprovalProps) {
  const toast = useToast()
  const refreshApprovals = useCatalog((s) => s.refreshApprovals)
  const attachRun = useStudio((s) => s.attachRun)
  const uid = useId()
  const payload = approval.payload ?? {}
  const summary = useMemo(() => summaryOf(payload.summary), [payload.summary])
  const cardId = str(payload.card_artifact) ?? ''

  const [card, setCard] = useState<{ id: string; data?: DualCheckCard | null } | null>(null)
  useEffect(() => {
    if (!cardId) return
    let alive = true
    setCard({ id: cardId })
    api.artifact(cardId).then(
      (res) => { if (alive) setCard({ id: cardId, data: cardOf(res?.content) }) },
      // 取不到（工件被清理、网络断了）和取回来认不出一样处理：按摘要裁决
      () => { if (alive) setCard({ id: cardId, data: null }) },
    )
    return () => { alive = false }
  }, [cardId])
  const slot = card?.id === cardId ? card : null
  const data = slot?.data ?? null
  const loading = !!cardId && slot?.data === undefined
  const lost = !cardId || slot?.data === null

  const views = useMemo(() => viewsOf(summary, data), [summary, data])
  const [choices, setChoices] = useState<Record<string, DualCheckChoice>>({})
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const pending = views.filter((v) => !choices[v.id]).length

  const submit = async () => {
    if (busy || pending || !views.length) return
    setBusy(true)
    try {
      const value: DualCheckReply = {
        choices: Object.fromEntries(views.map((v) => [v.id, choices[v.id]])),
        note: note.trim(),
      }
      // 照 ApprovalCard 里 mode 为 input 时的写法：备注外层也放一份，节点拿到的是 {approved, note, value}
      await api.approvals.decide(approval.id, { approved: true, note: value.note, value })
      await refreshApprovals()
      if (onResolved) await onResolved(approval.run_id)
      else await attachRun(approval.run_id)
      toast(T.done, 'ok')
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="@container mb-2 space-y-2" data-dual-check="" data-state={loading ? 'loading' : lost ? 'summary' : 'ready'}>
      {views.length > 0 && <p className="text-xs leading-relaxed" data-dual-intro="">{T.intro(views.length)}</p>}
      {loading && (
        <div className="flex items-center gap-1.5 text-2xs text-faint" role="status" data-dual-loading="">
          <Spinner size={10} /> {T.loading}
        </div>
      )}
      {lost && views.length > 0 && (
        <div className="text-2xs leading-relaxed" style={{ color: 'var(--st-waiting)' }} data-dual-load-failed="">{T.loadFailed}</div>
      )}
      {data && (str(data.model) || data.same_model) && (
        <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-2xs">
          {str(data.model) && <span className="mono text-dim [overflow-wrap:anywhere]" data-dual-model="">{T.model(data.model!)}</span>}
          {data.same_model && (
            <span className="flex items-center gap-1" style={{ color: 'var(--st-waiting)' }} data-dual-same-model="">
              <AlertTriangle size={11} aria-hidden className="shrink-0" /> {T.sameModel}
            </span>
          )}
        </div>
      )}

      {!views.length ? (
        <p className="text-2xs leading-relaxed text-dim" data-dual-empty="">{T.empty}</p>
      ) : (
        <>
          {views.map((v) => (
            <MetricBlock key={v.id} view={v} name={`${uid}-${v.id}`} choice={choices[v.id]}
                         onChoose={(c) => setChoices((cur) => ({ ...cur, [v.id]: c }))} />
          ))}

          {/* 选了会怎样，常显在提交按钮上面：审核人得在提交之前知道「采用盲算」并不替换报告里的数 */}
          <div className="text-2xs leading-relaxed text-dim" data-dual-consequence="">
            {T.consequence(approval.run_class)}
            <span className="mt-0.5 block">{T.consequenceFail}</span>
          </div>
          <div>
            <label className="label" htmlFor={`${uid}-note`}>{T.note}</label>
            <textarea id={`${uid}-note`} className="field" rows={2} value={note} placeholder={T.notePlaceholder}
                      data-dual-note="" onChange={(e) => setNote(e.target.value)} />
          </div>
          {pending > 0 && (
            <div id={`${uid}-pending`} className="text-2xs" style={{ color: 'var(--st-waiting)' }} data-dual-pending="">
              {T.pending(pending)}
            </div>
          )}
          <button type="button" className="btn btn-primary w-full justify-center" disabled={busy || pending > 0}
                  aria-describedby={pending > 0 ? `${uid}-pending` : undefined} data-dual-submit="" onClick={() => void submit()}>
            {busy ? <Spinner size={11} /> : <Check size={12} aria-hidden />} {T.submit}
          </button>
        </>
      )}
    </div>
  )
}

/** 一个不一致的指标：名字、两个值、相差多少、差异说明、对照两条 SQL、三选一 */
function MetricBlock({ view, name, choice, onChoose }: {
  view: MetricView; name: string; choice?: DualCheckChoice; onChoose: (c: DualCheckChoice) => void
}) {
  const uid = useId()
  // 只有兜底说明时默认展开：一条具体说法都没有，审核人只能对照原文判断
  const [open, setOpen] = useState<boolean | null>(null)
  const expanded = !!view.full && (open ?? view.fallbackOnly)
  const delta = deltaText(view.deltaPct)
  return (
    <div className="rounded border bg-bg p-2" role="group" aria-labelledby={`${uid}-name`} data-dual-metric={view.id}>
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <span id={`${uid}-name`} className="min-w-0 text-xs font-medium [overflow-wrap:anywhere]" data-dual-name="">{view.name}</span>
        {delta && (
          <span className="tnum ml-auto shrink-0 text-2xs font-medium" style={{ color: 'var(--st-waiting)' }} title={T.deltaHint}
                data-dual-delta="">{delta}</span>
        )}
      </div>
      <dl className="mt-1 grid grid-cols-2 gap-2" data-dual-values="">
        <div className="min-w-0">
          <dt className="text-2xs text-faint">{DUAL_CHECK_TEXT.original}</dt>
          <dd className="tnum m-0 text-sm font-medium [overflow-wrap:anywhere]" data-dual-value="original">{view.original ?? NONE}</dd>
        </div>
        <div className="min-w-0">
          <dt className="text-2xs text-faint">{DUAL_CHECK_TEXT.blind}</dt>
          <dd className="tnum m-0 text-sm font-medium [overflow-wrap:anywhere]" data-dual-value="blind">{view.blind ?? NONE}</dd>
        </div>
      </dl>

      <div className="mt-1.5" data-dual-diff="">
        <div className="text-2xs text-faint">{T.diffHead}</div>
        {view.equivalent ? (
          <p className="text-2xs leading-relaxed text-dim" data-dual-equivalent="">{T.equivalent}</p>
        ) : view.fallbackOnly ? (
          <p className="text-2xs leading-relaxed" style={{ color: 'var(--st-waiting)' }} data-dual-fallback="">{T.fallbackOnly}</p>
        ) : view.items.length > 0 && (
          <ul className="list-disc space-y-0.5 pl-4 text-2xs leading-relaxed [overflow-wrap:anywhere]">
            {view.items.map((t, i) => <li key={i} data-dual-diff-item="">{t}</li>)}
          </ul>
        )}
        {view.rest && <p className="mt-0.5 text-2xs leading-relaxed text-faint" data-dual-diff-rest="">{view.rest}</p>}
      </div>

      {view.full && (
        <button type="button" className="btn btn-xs mt-1.5" aria-expanded={expanded} aria-controls={expanded ? `${uid}-cmp` : undefined}
                data-dual-compare-toggle="" onClick={() => setOpen(!expanded)}>
          <ChevronRight size={11} aria-hidden style={{ transform: expanded ? 'rotate(90deg)' : 'none', transition: 'transform .15s' }} />
          {expanded ? T.compareClose : T.compare}
        </button>
      )}
      {expanded && view.full && <Compare id={`${uid}-cmp`} metric={view.full} />}
      {view.single && <p className="mt-1.5 text-2xs leading-relaxed text-dim" data-dual-catalog="">{T.catalog}</p>}

      {/* 原生单选：Tab 进组、方向键换选项，读屏念得出是哪个指标的哪一项 */}
      <fieldset className="mt-2 min-w-0 border-0 p-0" data-dual-choice={view.id}>
        <legend className="sr-only">{T.choiceGroup(view.name)}</legend>
        <div className="flex flex-wrap gap-1.5">
          {CHOICES.map((c) => (
            <label key={c}
                   className={clsx('flex cursor-pointer items-center gap-1.5 rounded border px-2 py-1 text-2xs transition-colors hover:bg-hover',
                     choice === c ? 'font-medium' : 'text-dim')}
                   style={choice === c ? { borderColor: 'var(--accent)', background: 'var(--accent-soft)', color: 'var(--text)' } : undefined}>
              <input type="radio" className="accent-[var(--accent)]" name={name} value={c} checked={choice === c}
                     onChange={() => onChoose(c)} />
              {T.choices[c]}
            </label>
          ))}
        </div>
      </fieldset>
    </div>
  )
}

/** 一个输入在两边的取值和 SQL */
interface Pair {
  path: string
  original?: DualCheckInput
  blind?: DualCheckBlindInput
  source: string | null
  /** SQL 或取到的值不同 */
  differs: boolean
}

/** 比 SQL 时不看空白和大小写：原样写成一行和换了行的是同一条 */
const normSql = (sql: unknown): string => (typeof sql === 'string' ? sql.replace(/\s+/g, ' ').trim().toLowerCase() : '')
/** 比一行时再去掉行尾的逗号、分号：规范化的写法逗号在行尾，原文不一定 */
const normLine = (line: string): string => line.replace(/\s+/g, ' ').trim().replace(/[,;]+$/, '').trim().toLowerCase()
const sameValue = (a: unknown, b: unknown): boolean => {
  if (typeof a === 'number' && typeof b === 'number') return a === b
  try { return JSON.stringify(a) === JSON.stringify(b) } catch { return false }
}

function pairsOf(metric: DualCheckMetric): Pair[] {
  const byPath = new Map<string, { original?: DualCheckInput; blind?: DualCheckBlindInput }>()
  const valid = (i: unknown): i is DualCheckInput => !!i && typeof i === 'object' && typeof (i as DualCheckInput).path === 'string'
  for (const i of (Array.isArray(metric.original?.inputs) ? metric.original.inputs : []).filter(valid)) {
    byPath.set(i.path, { ...byPath.get(i.path), original: i })
  }
  for (const i of (Array.isArray(metric.blind?.inputs) ? metric.blind.inputs : []).filter(valid)) {
    byPath.set(i.path, { ...byPath.get(i.path), blind: i })
  }
  return [...byPath].map(([path, p]) => ({
    path, ...p,
    source: str(p.original?.source) ?? str(p.blind?.source) ?? null,
    differs: normSql(p.original?.sql) !== normSql(p.blind?.sql) || !sameValue(p.original?.value, p.blind?.value),
  }))
}

/** text_diff（「- 」原流程、「+ 」盲算）→ 两边各自要高亮的行（按 normLine 比） */
function marksOf(textDiff: unknown): { original: Set<string>; blind: Set<string> } {
  const original = new Set<string>()
  const blind = new Set<string>()
  for (const line of Array.isArray(textDiff) ? textDiff : []) {
    if (typeof line !== 'string') continue
    if (line.startsWith('- ')) original.add(normLine(line.slice(2)))
    else if (line.startsWith('+ ')) blind.add(normLine(line.slice(2)))
  }
  original.delete('')
  blind.delete('')
  return { original, blind }
}

function valueText(v: unknown): string {
  if (v == null) return NONE
  if (typeof v === 'number') return Number.isFinite(v) ? v.toLocaleString('en-US', { maximumFractionDigits: 6 }) : String(v)
  if (typeof v === 'string' || typeof v === 'boolean') return String(v)
  try {
    const s = JSON.stringify(v)
    return s.length > 80 ? `${s.slice(0, 79)}…` : s
  } catch {
    return String(v)
  }
}

/** 高亮的行：两边都用同一种提醒底色——谁对谁错由审核人判断，不用红绿暗示哪边是「删掉的」 */
const MARK: CSSProperties = {
  background: 'color-mix(in srgb, var(--warn) 16%, transparent)', color: 'var(--text)', boxShadow: 'inset 2px 0 0 var(--warn)',
}

/**
 * 对照两条 SQL：先列每个输入两边各取到多少，再把不同的输入的两条 SQL 左右并排（卡片窄于 32rem 时上下排，按卡片
 * 自己的宽度算，画布右栏里也是上下排），规范化后不同的行在原文里高亮；规范化的写法和原文对不上（模型常把 SQL 写成
 * 一行）时原文不乱标，规范化后不同的行另列一份，每行标是哪一边
 */
function Compare({ id, metric }: { id: string; metric: DualCheckMetric }) {
  const pairs = useMemo(() => pairsOf(metric), [metric])
  const marks = useMemo(() => marksOf(metric.diff?.text_diff), [metric])
  const lines = useMemo(() => (Array.isArray(metric.diff?.text_diff) ? metric.diff.text_diff : [])
    .filter((l): l is string => typeof l === 'string' && (l.startsWith('- ') || l.startsWith('+ ')))
    .map((l) => ({ side: l.startsWith('- ') ? 'original' as const : 'blind' as const, text: l.slice(2) })), [metric])
  const differing = pairs.filter((p) => p.differs)
  return (
    <div id={id} className="mt-1.5 min-w-0 space-y-2" data-dual-compare="">
      {pairs.length > 0 && (
        <div>
          <div className="mb-0.5 text-2xs text-faint">{T.inputs}</div>
          <ul className="space-y-0.5">
            {pairs.map((p) => (
              <li key={p.path} className="flex min-w-0 flex-wrap items-baseline gap-x-2 text-2xs" data-dual-input={p.path}
                  data-differs={p.differs ? '' : undefined}>
                <code className="mono min-w-0 [overflow-wrap:anywhere]">{p.path}</code>
                <span className="tnum text-dim">{DUAL_CHECK_TEXT.original} {valueText(p.original?.value)}</span>
                <span className={clsx('tnum', !p.differs && 'text-dim')} style={p.differs ? { color: 'var(--st-waiting)' } : undefined}>
                  {DUAL_CHECK_TEXT.blind} {valueText(p.blind?.value)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {(differing.length ? differing : pairs).map((p) => (
        <div key={p.path} className="min-w-0" data-dual-pair={p.path}>
          <div className="mb-0.5 text-2xs text-faint [overflow-wrap:anywhere]">
            <code className="mono">{p.path}</code>{p.source ? ` · ${T.source(p.source)}` : ''}
          </div>
          <div className="grid grid-cols-1 gap-2 @lg:grid-cols-2">
            <SqlPane side="original" label={T.sqlOriginal} sql={p.original?.sql} marks={marks.original} />
            <SqlPane side="blind" label={T.sqlBlind} sql={p.blind?.sql} marks={marks.blind} />
          </div>
          {str(p.blind?.rationale) && (
            <p className="mt-1 text-2xs leading-relaxed text-dim [overflow-wrap:anywhere]" data-dual-rationale="">
              <span className="text-faint">{T.rationale}：</span>{p.blind!.rationale}
            </p>
          )}
        </div>
      ))}
      {lines.length > 0 && (
        <div data-dual-text-diff="">
          <div className="mb-0.5 text-2xs text-faint">{T.textDiff}</div>
          <div className="mono max-h-48 overflow-y-auto rounded border bg-panel px-2 py-1.5 text-2xs leading-relaxed focus-visible:outline-2 focus-visible:outline-[color:var(--accent)]"
               tabIndex={0} role="group" aria-label={T.textDiff}>
            {lines.map((l, i) => (
              <div key={i} className="flex min-w-0 gap-2" data-diff-side={l.side}>
                {/* 标签列等宽：两边的行首对齐，一眼看出是同一处的两种写法 */}
                <span className="w-[3em] shrink-0 font-sans text-faint">{l.side === 'original' ? DUAL_CHECK_TEXT.original : DUAL_CHECK_TEXT.blind}</span>
                <span className="min-w-0 whitespace-pre-wrap text-dim [overflow-wrap:anywhere]">{l.text}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

/** 一条 SQL：逐行画，text_diff 里有的行高亮。折行显示、不横向撑开，太长时框里纵向滚动，框能用键盘聚焦 */
function SqlPane({ side, label, sql, marks }: { side: 'original' | 'blind'; label: string; sql?: string | null; marks: Set<string> }) {
  const lines = typeof sql === 'string' && sql.trim() ? sql.replace(/\s+$/, '').split('\n') : null
  return (
    <figure className="m-0 min-w-0">
      <figcaption className="mb-0.5 text-2xs text-faint">{label}</figcaption>
      <pre className={clsx('max-h-56 overflow-y-auto whitespace-pre-wrap rounded border bg-panel px-1 py-1.5 text-2xs leading-relaxed [overflow-wrap:anywhere] focus-visible:outline-2 focus-visible:outline-[color:var(--accent)]',
             lines ? 'mono text-dim' : 'font-sans text-faint')}
           tabIndex={0} aria-label={label} data-dual-sql={side}>
        {lines
          ? lines.map((line, i) => (marks.has(normLine(line))
            ? <mark key={i} className="block rounded-sm px-1" style={MARK} data-diff-line={side === 'original' ? 'del' : 'add'}>{line || ' '}</mark>
            : <span key={i} className="block px-1">{line || ' '}</span>))
          : <span className="px-1">{T.sqlMissing}</span>}
      </pre>
    </figure>
  )
}
