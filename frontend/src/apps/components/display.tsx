/**
 * 展示组件：stamp、metric、series、table、chart、report、text、notice、field、status（组件目录 v1 §3.5）。
 *
 * 数都是服务端渲染好的字（render_metric / render_cell），这里不格式化、不合计、不算差值（叙述层无算术）：表格只排序，
 * 图表只把已有的列画出来。点数字打开出处；没有出处的数照实标「无证据」。
 */
import { useEffect, useMemo, useState } from 'react'
import { ArrowDown, ArrowUp, RefreshCw, ShieldCheck } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../../api/client'
import { Notice, Spinner, StatusPill, useToast } from '../../components/ui'
import { Markdown } from '../../run/Markdown'
import { EvidenceDoc } from '../../run/EvidenceDoc'
import { useEvidence } from '../../store/evidence'
import { formatDay } from '../../lib/format'
import { STAMP_TEXT, VIEW_TEXT } from '../../lib/termsApp'
import type { ViewEvidence } from '../../typesApp'
import { Stamp } from '../Stamp'
import { Plot, type PlotSeries } from '../Plot'
import { blockState, evidenceOf, sourceOf, str, useView, type NodeProps } from '../context'
import { useViewParams } from './input'

/** 这一块没有数据时说为什么：还没有可读的运行、正在生成、过期、出错。服务端给了原话就用原话 */
export function BlockState({ state, message }: { state: string; message?: string }) {
  const text = message || (state === 'empty' ? VIEW_TEXT.empty : state === 'running' ? VIEW_TEXT.running
    : state === 'stale' ? VIEW_TEXT.stale : state === 'withheld' ? VIEW_TEXT.withheld : VIEW_TEXT.error)
  return (
    <div className="flex items-center gap-1.5 text-2xs font-normal text-faint" data-block-state={state}
         style={state === 'error' ? { color: 'var(--err)' } : undefined}>
      {state === 'running' && <Spinner size={11} />}
      <span>{text}</span>
    </div>
  )
}

/** 数字：有出处的是按钮（点开出处），没有的照实标「无证据」 */
function Num({ text, evidence, label, className }: {
  text: string; evidence: ViewEvidence | null; label: string; className?: string
}) {
  const { openEvidence } = useView()
  if (!evidence) {
    return (
      <span className={clsx('tnum', className)} data-evidence="none">
        {text}
        <span className="chip ml-1.5 align-middle text-2xs font-normal" style={{ color: 'var(--st-waiting)' }}>{VIEW_TEXT.noEvidence}</span>
      </span>
    )
  }
  return (
    <button type="button" className={clsx('tnum rounded-sm text-left underline decoration-dotted decoration-1 underline-offset-4 hover:text-accent focus-visible:outline focus-visible:outline-2 focus-visible:outline-[color:var(--accent)]', className)}
            data-evidence={evidence.kind} title={VIEW_TEXT.evidence} aria-label={`${label} ${text}，${VIEW_TEXT.evidence}`}
            onClick={() => openEvidence(evidence, label, text)}>
      {text}
    </button>
  )
}

/**
 * 印章。按需视图（来源的 kind 为 on_demand）旁边给「刷新」：按页面当前的参数发起一次（同一组参数命中缓存就不新运行），
 * 预览里不给
 */
export function StampBlock({ node }: NodeProps) {
  const { model, appId, preview, onReload } = useView()
  const toast = useToast()
  const params = useViewParams()
  const [busy, setBusy] = useState(false)
  const view = str(node.data?.view)
  const src = sourceOf(model, node)
  const stamp = node.data?.stamp ?? src?.stamp
  const state = blockState(node)
  const onDemand = src?.kind === 'on_demand'
  const refresh = async () => {
    setBusy(true)
    try {
      const res = await api.apps.refreshView(appId, view, params)
      toast(res.cached ? VIEW_TEXT.cached : VIEW_TEXT.refreshed, 'ok')
      onReload?.()
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="space-y-1" data-component="stamp">
      {stamp ? <Stamp stamp={stamp} compact={!!node.props.compact} />
        : <BlockState {...(state ?? { state: 'empty', message: STAMP_TEXT.noStamp })} />}
      {onDemand && view && !preview && (
        <button type="button" className="btn btn-xs" disabled={busy || src?.state === 'running'} data-view-refresh={view}
                onClick={() => void refresh()}>
          {busy ? <Spinner size={11} /> : <RefreshCw size={11} aria-hidden />} {busy ? VIEW_TEXT.refreshing : VIEW_TEXT.refresh}
        </button>
      )}
    </div>
  )
}

export function MetricBlock({ node }: NodeProps) {
  const data = node.data ?? {}
  const label = str(node.props.label) || str(data.name) || node.id
  const state = blockState(node)
  const text = str(data.text)
  // 单位只写一处。口径卡写了单位的，服务端交来的字已经带着它（render_metric：「18,218.54元」），data.unit 只是说明是哪个
  // 单位，不再在下面另写一行；口径卡没写单位（data.unit 是空串）、绑定单元格（字不带单位，render_cell）的，写搭建者在
  // 属性里填的单位
  const unit = str(data.unit) ? '' : str(node.props.unit)
  return (
    <div className="rounded-lg border bg-panel p-3" data-component="metric" data-metric-state={str(data.state) || 'ok'}>
      <div className="text-2xs text-faint">{label}</div>
      <div className="mt-1 text-2xl font-semibold leading-tight text-fg">
        {state ? <BlockState {...state} />
          : data.state === 'masked' ? <span className="text-sm font-normal italic text-faint">{text || VIEW_TEXT.masked}</span>
          : <Num text={text || '—'} evidence={evidenceOf(data.evidence)} label={label} />}
      </div>
      {unit && <div className="mt-1 text-2xs text-dim">{unit}</div>}
    </div>
  )
}

/** x 轴上的一个点写成日期（同一天的几次写到分钟）：趋势按封存运行的结束时间排 */
function pointLabel(asOf: string, sameDay: boolean): string {
  if (!asOf) return '—'
  if (!sameDay) return formatDay(asOf)
  const d = new Date(asOf)
  return Number.isNaN(d.getTime()) ? asOf : `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

/**
 * 趋势：服务端按处置给好线和断点——dual 两条线（旧版本、新版本），incomparable 两段之间断开并写明前后不可比，
 * recompute 只给新版本、写一句说明。点一个点打开那一次运行里这个指标的出处
 */
export function SeriesBlock({ node }: NodeProps) {
  const { openEvidence } = useView()
  const data = node.data ?? {}
  const label = str(node.props.label) || node.id
  const state = blockState(node)
  const { categories, series, breaks, notes } = useMemo(() => {
    const raw: Record<string, any>[] = Array.isArray(data.series) ? data.series : []
    const points = raw.map((l) => (Array.isArray(l.points) ? l.points : []) as Record<string, any>[])
    // 横轴是封存运行的结束时间，几条线的点合在一起按时间排
    const keys = [...new Set(points.flat().map((p) => str(p.as_of) || str(p.run_id)))]
      .sort((a, b) => (Date.parse(a) || 0) - (Date.parse(b) || 0))
    const days = new Set(keys.map((k) => k.slice(0, 10)))
    const sameDay = days.size === 1 && keys.length > 1
    const lines: PlotSeries[] = raw.map((l, li) => {
      const at = new Map(points[li].map((p) => [str(p.as_of) || str(p.run_id), p]))
      return {
        name: str(l.label) || label,
        values: keys.map((k) => (typeof at.get(k)?.value === 'number' ? at.get(k)!.value : null)),
        texts: keys.map((k) => str(at.get(k)?.text) || undefined),
        evidence: keys.map((k) => evidenceOf(at.get(k)?.evidence)),
      }
    })
    const brk: { index: number; label: string }[] = []
    const extra: string[] = []
    for (const b of Array.isArray(data.breaks) ? data.breaks : []) {
      const after = points.flat().find((p) => str(p.run_id) === str(b?.after_run))
      const index = after ? keys.indexOf(str(after.as_of) || str(after.run_id)) + 1 : -1
      if (index > 0 && index < keys.length) brk.push({ index, label: VIEW_TEXT.incomparable })
      if (str(b?.text)) extra.push(str(b.text))
    }
    return { categories: keys.map((k) => pointLabel(k, sameDay)), series: lines, breaks: brk, notes: extra }
  }, [data, label])
  const unit = str(data.unit)
  return (
    <div className="rounded-lg border bg-panel p-3" data-component="series" data-series-mode={str(data.mode) || 'single'}>
      <div className="mb-1.5 text-2xs text-faint">{label}{unit && `（${unit}）`}</div>
      {state ? <BlockState {...state} />
        : categories.length
          ? <Plot kind={node.props.chart === 'bar' ? 'bar' : 'line'} categories={categories} series={series} breaks={breaks}
                  label={label} onPick={(s, i) => {
                    const ev = series[s].evidence?.[i]
                    if (ev) openEvidence(ev, `${label} · ${categories[i]}`, series[s].texts?.[i] ?? undefined)
                  }} />
          : <BlockState state="empty" message={VIEW_TEXT.noPoints} />}
      {notes.map((n, i) => <div key={i} className="mt-1 text-2xs text-dim" data-series-note="">{n}</div>)}
    </div>
  )
}

const asNumber = (v: unknown): number | null => {
  if (typeof v === 'number' && Number.isFinite(v)) return v
  if (typeof v === 'string' && /^[-+]?\d[\d,]*(\.\d+)?$/.test(v.trim())) return Number(v.replace(/,/g, ''))
  return null
}

const cellText = (v: unknown) => (v == null ? '' : typeof v === 'object' ? JSON.stringify(v) : String(v))

const rowsOf = (data: Record<string, any>): unknown[][] =>
  (Array.isArray(data.rows) ? data.rows : []).map((r: unknown) => (Array.isArray(r) ? r : []))

/** 表格：只排序，不出合计行（叙述层无算术）。样式同运行成果里的查询结果表；「查看出处」打开整份查询快照 */
export function TableBlock({ node }: NodeProps) {
  const { openEvidence } = useView()
  const data = node.data ?? {}
  const state = blockState(node)
  const title = str(node.props.title)
  const columns: string[] = Array.isArray(data.columns) ? data.columns.map(str) : []
  const all = useMemo(() => rowsOf(data), [data])
  const masked = new Set<string>(Array.isArray(data.masked) ? data.masked.map(str) : [])
  // 服务端已按 props.sort 排过；这里只管人点表头换的排序
  const [sort, setSort] = useState<{ col: number; desc: boolean } | null>(null)
  const rows = useMemo(() => {
    if (!sort) return all
    return [...all].sort((a, b) => {
      const x = a[sort.col]
      const y = b[sort.col]
      const nx = asNumber(x)
      const ny = asNumber(y)
      const c = nx != null && ny != null ? nx - ny : cellText(x).localeCompare(cellText(y), 'zh-CN')
      return sort.desc ? -c : c
    })
  }, [all, sort])
  const total = typeof data.total_rows === 'number' ? data.total_rows : all.length
  const evidence = evidenceOf(data.evidence)
  const numeric = columns.map((_, j) => rows.some((r) => asNumber(r[j]) != null) && !masked.has(columns[j]))

  return (
    <div className="rounded-lg border bg-panel" data-component="table">
      {title && <div className="border-b px-3 py-2 text-xs font-medium">{title}</div>}
      {state || !columns.length ? <div className="px-3 py-2"><BlockState {...(state ?? { state: 'empty' })} /></div> : (
        <>
          <div className="max-h-[480px] overflow-auto">
            <table className="w-full text-2xs">
              <thead className="sticky top-0">
                <tr className="border-b bg-elev">
                  {columns.map((c, j) => {
                    const on = sort?.col === j
                    return (
                      <th key={c} className={clsx('mono px-1.5 py-1 font-medium text-dim', numeric[j] ? 'text-right' : 'text-left')}
                          aria-sort={on ? (sort!.desc ? 'descending' : 'ascending') : 'none'}>
                        <button type="button" className="inline-flex items-center gap-0.5 hover:text-fg"
                                onClick={() => setSort(on ? (sort!.desc ? null : { col: j, desc: true }) : { col: j, desc: false })}>
                          {c}
                          {on && (sort!.desc ? <ArrowDown size={10} aria-label={VIEW_TEXT.sortDesc} />
                            : <ArrowUp size={10} aria-label={VIEW_TEXT.sortAsc} />)}
                        </button>
                      </th>
                    )
                  })}
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={i} className="border-b last:border-0">
                    {columns.map((c, j) => (
                      <td key={c} className={clsx('mono max-w-[220px] truncate px-1.5 py-1 text-dim', numeric[j] && 'tnum text-right')}
                          data-masked={masked.has(c) ? '' : undefined} title={cellText(r[j])}>
                        {masked.has(c) ? <span className="font-sans italic text-faint">{cellText(r[j]) || VIEW_TEXT.masked}</span>
                          : r[j] == null ? <span className="opacity-40">null</span> : cellText(r[j])}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="flex flex-wrap items-center gap-x-2 border-t px-2 py-1 text-2xs text-dim">
            <span className="tnum">{rows.length < total ? VIEW_TEXT.rowsOf(rows.length, total) : VIEW_TEXT.rows(total)}</span>
            {data.truncated && rows.length >= total && <span style={{ color: 'var(--st-waiting)' }}>{VIEW_TEXT.truncated}</span>}
            <span className="flex-1" />
            {evidence?.artifact && (
              <button type="button" className="inline-flex items-center gap-1 rounded px-1 hover:bg-hover hover:text-fg"
                      data-table-evidence="" onClick={() => openEvidence(evidence, title || node.id)}>
                <ShieldCheck size={10} aria-hidden /> {VIEW_TEXT.evidence}
              </button>
            )}
          </div>
        </>
      )}
    </div>
  )
}

/** 图表：按表格的列画折线或柱状，不聚合。x 是一列，y 最多四列；遮罩的列不画 */
export function ChartBlock({ node }: NodeProps) {
  const data = node.data ?? {}
  const state = blockState(node)
  const title = str(node.props.title) || node.id
  const columns: string[] = Array.isArray(data.columns) ? data.columns.map(str) : []
  const rows = useMemo(() => rowsOf(data), [data])
  const masked = new Set<string>(Array.isArray(data.masked) ? data.masked.map(str) : [])
  const x = str(node.props.x) || columns[0]
  const ys: string[] = (Array.isArray(node.props.y) ? node.props.y.map(str) : columns.slice(1, 2))
    .filter((c: string) => columns.includes(c) && c !== x && !masked.has(c)).slice(0, 4)
  const xi = columns.indexOf(x)
  const shown = rows.slice(0, 60)
  return (
    <div className="rounded-lg border bg-panel p-3" data-component="chart">
      <div className="mb-1.5 text-xs font-medium">{title}</div>
      {state ? <BlockState {...state} />
        : xi >= 0 && ys.length && shown.length
          ? (
            <>
              <Plot kind={node.props.chart === 'line' ? 'line' : 'bar'} label={title}
                    categories={shown.map((r) => cellText(r[xi]))}
                    series={ys.map((c) => {
                      const at = columns.indexOf(c)
                      return { name: c, values: shown.map((r) => asNumber(r[at])), texts: shown.map((r) => cellText(r[at])) }
                    })} />
              {rows.length > shown.length && <div className="mt-1 text-2xs text-faint">{VIEW_TEXT.rowsOf(shown.length, rows.length)}</div>}
            </>
          )
          : <BlockState state="empty" message={VIEW_TEXT.noPoints} />}
    </div>
  )
}

/** 报告：同运行成果里的报告，逐段可点开证据（EvidenceDoc）。文档按工件 id 取，取不到照实说 */
export function ReportBlock({ node }: NodeProps) {
  const data = node.data ?? {}
  const state = blockState(node)
  const title = str(node.props.title)
  const artifact = str(data.doc_artifact)
  const runId = evidenceOf(data.evidence)?.run_id
  return (
    <div className="rounded-lg border bg-panel p-3" data-component="report">
      {title && <div className="mb-1.5 text-xs font-medium">{title}</div>}
      {state ? <BlockState {...state} />
        : artifact ? <ReportDoc artifact={artifact} runId={runId} label={title || node.id} />
        : <BlockState state="empty" />}
    </div>
  )
}

export function ReportDoc({ artifact, runId, label }: { artifact: string; runId?: string; label: string }) {
  const slot = useEvidence((s) => s.docs[artifact])
  const loadDoc = useEvidence((s) => s.loadDoc)
  useEffect(() => { void loadDoc(artifact) }, [artifact, loadDoc])
  if (!slot || slot.status === 'loading') return <div className="flex items-center gap-1.5 text-2xs text-faint"><Spinner size={11} /></div>
  if (slot.status === 'error' || !slot.data) return <BlockState state="error" message={VIEW_TEXT.reportFailed} />
  return <EvidenceDoc doc={slot.data} artifact={artifact} runId={runId} label={label} tally runClass="formal" settled />
}

export function TextBlock({ node }: NodeProps) {
  return <div className="text-sm leading-relaxed" data-component="text"><Markdown text={str(node.props.markdown)} /></div>
}

export function NoticeBlock({ node }: NodeProps) {
  const tone = node.props.tone === 'error' ? 'err' : node.props.tone === 'warn' ? 'warn' : 'info'
  return <Notice tone={tone} attr={{ 'data-component': 'notice' }}>{str(node.props.text)}</Notice>
}

/** 字段：运行输入、成果字段、运行信息。输入和逐字取自报告的成果能点开出处 */
export function FieldBlock({ node }: NodeProps) {
  const data = node.data ?? {}
  const label = str(node.props.label) || node.id
  const state = blockState(node)
  const text = str(data.text)
  const evidence = evidenceOf(data.evidence)
  return (
    <div className="flex flex-wrap items-baseline gap-x-2 text-xs" data-component="field">
      <span className="text-faint">{label}</span>
      {state ? <BlockState {...state} />
        : data.state === 'masked' ? <span className="italic text-faint">{text || VIEW_TEXT.masked}</span>
        : !text ? <span className="text-faint">—</span>
        : evidence && evidence.kind !== 'report' ? <Num text={text} evidence={evidence} label={label} />
        : evidence ? <ReportText text={text} evidence={evidence} label={label} />
        : <span className="whitespace-pre-wrap text-fg">{text}</span>}
    </div>
  )
}

/** 逐字取自报告的成果字段：文字照常写，旁边一个「查看出处」打开那份报告 */
function ReportText({ text, evidence, label }: { text: string; evidence: ViewEvidence; label: string }) {
  const { openEvidence } = useView()
  return (
    <span className="min-w-0">
      <span className="whitespace-pre-wrap text-fg">{text}</span>
      <button type="button" className="ml-1.5 inline-flex items-center gap-0.5 text-2xs text-dim hover:text-fg"
              data-evidence="report" onClick={() => openEvidence(evidence, label, text)}>
        <ShieldCheck size={10} aria-hidden /> {VIEW_TEXT.evidence}
      </button>
    </span>
  )
}

export function StatusBlock({ node }: NodeProps) {
  const data = node.data ?? {}
  const label = str(node.props.label)
  const state = blockState(node)
  const status = str(data.value ?? data.status)
  return (
    <div className="flex items-center gap-2 text-xs" data-component="status">
      {label && <span className="text-faint">{label}</span>}
      {state ? <BlockState {...state} /> : status ? <StatusPill status={status} /> : <span className="text-faint">—</span>}
    </div>
  )
}
