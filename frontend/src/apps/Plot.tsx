/**
 * 趋势和图表共用的自绘 SVG（应用平台 §3.5：前端没有图表库，v1 只做折线和柱状，不做缩放）。
 *
 * 按 dataviz 的规矩画：一个 y 轴；系列色按固定顺序取 --viz-1…4（index.css，深浅两套各自选过、跑过校验：浅色下第 3、4
 * 色对底色不足 3:1，所以一律给「查看数据」的表格视图）；折线 2px、圆角，柱子最粗 24px、数据端 4px 圆角、基线端是方的，
 * 相邻柱子之间留 2px 底色缝；网格线是一像素实线、淡；两个系列以上一定有图例，文字一律用文字色、不用系列色。
 * 悬停（和键盘左右键）有一条竖线吸附到最近的 x，提示框列出这个 x 上每个系列的值；点一下打开那个点的出处。
 *
 * 不算任何东西：值和它的写法（texts）都是服务端给的，这里只按值定位置。升版不可比的地方（breaks）折线断开，画一条
 * 竖线写明原因。
 */
import { useEffect, useId, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent } from 'react'
import clsx from 'clsx'
import { VIEW_TEXT } from '../lib/termsApp'
import type { ViewEvidence } from '../typesApp'

export interface PlotSeries {
  name: string
  values: (number | null)[]
  /** 服务端渲染好的写法（「45.68 万元」）；没有就用数本身 */
  texts?: (string | null | undefined)[]
  evidence?: (ViewEvidence | null | undefined)[]
}

export interface PlotProps {
  kind: 'line' | 'bar'
  categories: string[]
  series: PlotSeries[]
  /** 从第 index 个点起和前面不可比：折线在这里断开，画竖线和说明 */
  breaks?: { index: number; label: string }[]
  /** 读屏用的名字 */
  label: string
  /** 点了第 s 个系列的第 i 个点 */
  onPick?: (s: number, i: number) => void
  height?: number
}

const COLORS = ['var(--viz-1)', 'var(--viz-2)', 'var(--viz-3)', 'var(--viz-4)']
const M = { top: 14, right: 12, bottom: 24 }

/** 刻度的步长取 1、2、5 乘 10 的幂 */
function niceStep(span: number, count: number): number {
  const raw = span / Math.max(count, 1)
  const mag = 10 ** Math.floor(Math.log10(raw))
  const f = raw / mag
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * mag
}

/** 刻度的写法：一万以上按「万」「亿」收，小数最多两位。只是刻度，系列的值照服务端的写法 */
export function compactNumber(n: number): string {
  const abs = Math.abs(n)
  const trim = (x: number) => String(Number(x.toFixed(2)))
  if (abs >= 1e8) return `${trim(n / 1e8)}亿`
  if (abs >= 1e4) return `${trim(n / 1e4)}万`
  return trim(n)
}

/** 柱子：数据端两个 4px 圆角，基线端是方的。负数朝下 */
function barPath(x: number, w: number, y0: number, y1: number): string {
  const h = Math.abs(y1 - y0)
  const r = Math.min(4, w / 2, h)
  if (y1 <= y0) {
    return `M${x},${y0}V${y1 + r}Q${x},${y1} ${x + r},${y1}H${x + w - r}Q${x + w},${y1} ${x + w},${y1 + r}V${y0}Z`
  }
  return `M${x},${y0}V${y1 - r}Q${x},${y1} ${x + r},${y1}H${x + w - r}Q${x + w},${y1} ${x + w},${y1 - r}V${y0}Z`
}

export function Plot({ kind, categories, series, breaks = [], label, onPick, height = 200 }: PlotProps) {
  const wrapRef = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(480)
  const [hover, setHover] = useState<number | null>(null)
  const [table, setTable] = useState(false)
  const tableId = useId()
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const ro = new ResizeObserver(([entry]) => setWidth(Math.max(200, Math.floor(entry.contentRect.width))))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const n = categories.length
  const lines = series.slice(0, 4)
  const all = lines.flatMap((s) => s.values).filter((v): v is number => typeof v === 'number' && Number.isFinite(v))
  const geo = useMemo(() => {
    let lo = all.length ? Math.min(...all) : 0
    let hi = all.length ? Math.max(...all) : 1
    // 柱子从基线长出来：0 一定在范围里。折线看起伏，不强拉到 0
    if (kind === 'bar') { lo = Math.min(lo, 0); hi = Math.max(hi, 0) }
    if (lo === hi) { lo -= Math.abs(lo) * 0.1 || 1; hi += Math.abs(hi) * 0.1 || 1 }
    const step = niceStep(hi - lo, 4)
    const t0 = Math.floor(lo / step) * step
    const t1 = Math.ceil(hi / step) * step
    const ticks: number[] = []
    for (let t = t0; t <= t1 + step / 2; t += step) ticks.push(Number(t.toPrecision(12)))
    const left = 10 + Math.max(...ticks.map((t) => compactNumber(t).length)) * 6.5
    const plotW = Math.max(width - left - M.right, 40)
    const plotH = height - M.top - M.bottom
    const y = (v: number) => M.top + plotH - ((v - t0) / (t1 - t0 || 1)) * plotH
    const band = plotW / Math.max(n, 1)
    const cx = (i: number) => left + band * (i + 0.5)
    return { ticks, left, plotW, plotH, y, band, cx }
  }, [all.join(','), kind, width, height, n]) // eslint-disable-line react-hooks/exhaustive-deps

  const { ticks, left, plotW, plotH, y, band, cx } = geo
  const breakAt = new Set(breaks.map((b) => b.index))
  // x 轴标签按宽度隔几个写一个，免得挤在一起
  const every = Math.max(1, Math.ceil(n / Math.max(1, Math.floor(plotW / 72))))
  const k = lines.length
  const groupW = Math.min(band * 0.8, 24 * k + 2 * (k - 1))
  const barW = Math.max(1, Math.min(24, (groupW - 2 * (k - 1)) / Math.max(k, 1)))

  const pick = (i: number, py?: number) => {
    if (!onPick) return
    // 几条线时点在哪条附近算哪条；柱子按系列各自的位置
    let best = -1
    let dist = Infinity
    lines.forEach((s, si) => {
      const v = s.values[i]
      if (typeof v !== 'number' || !s.evidence?.[i]) return
      const d = py == null ? si : Math.abs(y(v) - py)
      if (d < dist) { dist = d; best = si }
    })
    if (best >= 0) onPick(best, i)
  }
  const indexAt = (e: { clientX: number; currentTarget: Element }) => {
    const rect = e.currentTarget.getBoundingClientRect()
    const px = e.clientX - rect.left
    const i = Math.floor((px - left) / band)
    return i >= 0 && i < n ? i : null
  }
  const onKey = (e: ReactKeyboardEvent) => {
    if (!n) return
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      e.preventDefault()
      setHover((h) => (h == null ? (e.key === 'ArrowRight' ? 0 : n - 1) : Math.min(n - 1, Math.max(0, h + (e.key === 'ArrowRight' ? 1 : -1)))))
    } else if ((e.key === 'Enter' || e.key === ' ') && hover != null) {
      e.preventDefault()
      pick(hover)
    } else if (e.key === 'Escape') setHover(null)
  }
  const textOf = (s: PlotSeries, i: number) => {
    const t = s.texts?.[i]
    const v = s.values[i]
    return t ? t : typeof v === 'number' ? String(v) : '—'
  }
  const clickable = !!onPick && lines.some((s) => s.evidence?.some(Boolean))

  return (
    <div className="min-w-0" data-plot={kind}>
      {k > 1 && (
        <ul className="mb-1 flex flex-wrap gap-x-3 gap-y-0.5 text-2xs text-dim" aria-label={label}>
          {lines.map((s, si) => (
            <li key={si} className="flex items-center gap-1.5">
              {kind === 'line'
                ? <span aria-hidden className="inline-block h-0.5 w-3 rounded-full" style={{ background: COLORS[si] }} />
                : <span aria-hidden className="inline-block h-2 w-2 rounded-sm" style={{ background: COLORS[si] }} />}
              {s.name}
            </li>
          ))}
        </ul>
      )}
      <div ref={wrapRef} className="relative">
        <svg width={width} height={height} role="img" aria-label={label} tabIndex={0}
             className={clsx('block outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]', clickable && 'cursor-pointer')}
             onKeyDown={onKey}
             onPointerMove={(e) => setHover(indexAt(e))}
             onPointerLeave={() => setHover(null)}
             onBlur={() => setHover(null)}
             onClick={(e) => {
               const i = indexAt(e)
               if (i == null) return
               const rect = e.currentTarget.getBoundingClientRect()
               pick(i, e.clientY - rect.top)
             }}>
          {ticks.map((t) => (
            <g key={t}>
              <line x1={left} x2={left + plotW} y1={y(t)} y2={y(t)} stroke="var(--hairline)" strokeWidth={1}
                    shapeRendering="crispEdges" />
              <text x={left - 6} y={y(t)} dy="0.32em" textAnchor="end" fontSize={10} fill="var(--text-faint)"
                    style={{ fontVariantNumeric: 'tabular-nums' }}>{compactNumber(t)}</text>
            </g>
          ))}
          {kind === 'bar' && <line x1={left} x2={left + plotW} y1={y(0)} y2={y(0)} stroke="var(--border-strong)" strokeWidth={1} shapeRendering="crispEdges" />}
          {categories.map((c, i) => i % every === 0 ? (
            <text key={i} x={cx(i)} y={M.top + plotH + 15} textAnchor="middle" fontSize={10} fill="var(--text-faint)">
              {c.length > 10 ? `${c.slice(0, 9)}…` : c}
            </text>
          ) : null)}
          {hover != null && kind === 'line' && (
            <line x1={cx(hover)} x2={cx(hover)} y1={M.top} y2={M.top + plotH} stroke="var(--text-faint)" strokeWidth={1}
                  shapeRendering="crispEdges" />
          )}
          {hover != null && kind === 'bar' && (
            <rect x={left + band * hover} y={M.top} width={band} height={plotH} fill="var(--bg-hover)" opacity={0.6} />
          )}
          {breaks.map((b) => b.index > 0 && b.index < n && (
            <g key={b.index} data-plot-break="">
              <line x1={left + band * b.index} x2={left + band * b.index} y1={M.top} y2={M.top + plotH}
                    stroke="var(--text-faint)" strokeWidth={1} shapeRendering="crispEdges" />
              <text x={left + band * b.index + 4} y={M.top + 2} dy="0.7em" fontSize={10} fill="var(--text-dim)">{b.label}</text>
            </g>
          ))}
          {kind === 'line' && lines.map((s, si) => {
            // 空值和升版不可比的地方断开
            const parts: string[] = []
            let cur = ''
            s.values.forEach((v, i) => {
              if (typeof v !== 'number' || breakAt.has(i)) { if (cur) parts.push(cur); cur = '' }
              if (typeof v === 'number') cur += `${cur ? 'L' : 'M'}${cx(i)},${y(v)}`
            })
            if (cur) parts.push(cur)
            const last = s.values.map((v, i) => (typeof v === 'number' ? i : -1)).filter((i) => i >= 0).pop()
            return (
              <g key={si}>
                {parts.map((d, pi) => (
                  <path key={pi} d={d} fill="none" stroke={COLORS[si]} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
                ))}
                {/* 孤点（前后都断开的点）画成点，不然看不见 */}
                {s.values.map((v, i) => typeof v === 'number' && (i === hover || i === last
                  || ((i === 0 || typeof s.values[i - 1] !== 'number' || breakAt.has(i))
                    && (i === n - 1 || typeof s.values[i + 1] !== 'number' || breakAt.has(i + 1)))) ? (
                    <circle key={i} cx={cx(i)} cy={y(v)} r={4} fill={COLORS[si]} stroke="var(--bg-panel)" strokeWidth={2} />
                  ) : null)}
              </g>
            )
          })}
          {kind === 'bar' && lines.map((s, si) => s.values.map((v, i) => {
            if (typeof v !== 'number') return null
            const x = cx(i) - groupW / 2 + si * (barW + 2)
            return (
              <path key={`${si}-${i}`} d={barPath(x, barW, y(0), y(v))} fill={COLORS[si]}
                    opacity={hover != null && hover !== i ? 0.55 : 1} />
            )
          }))}
        </svg>
        {hover != null && (
          <div role="status" className="pointer-events-none absolute top-1 z-10 min-w-[120px] rounded-md border bg-elev px-2 py-1.5 text-2xs shadow-elev-2"
               style={cx(hover) > width / 2 ? { right: width - cx(hover) + 8 } : { left: cx(hover) + 8 }} data-plot-tip="">
            <div className="mb-0.5 text-faint">{categories[hover]}</div>
            {lines.map((s, si) => (
              <div key={si} className="flex items-center gap-1.5">
                <span aria-hidden className="inline-block h-0.5 w-2.5 shrink-0 rounded-full" style={{ background: COLORS[si] }} />
                <span className="tnum font-semibold text-fg">{textOf(s, hover)}</span>
                {k > 1 && <span className="text-dim">{s.name}</span>}
              </div>
            ))}
            {clickable && lines.some((s) => s.evidence?.[hover]) && <div className="mt-0.5 text-faint">{VIEW_TEXT.evidence}</div>}
          </div>
        )}
      </div>
      <button type="button" className="mt-1 text-2xs text-dim underline-offset-2 hover:text-fg hover:underline"
              aria-expanded={table} aria-controls={tableId} onClick={() => setTable((t) => !t)}>
        {table ? VIEW_TEXT.hideTable : VIEW_TEXT.showTable}
      </button>
      {table && (
        <div id={tableId} className="mt-1 max-h-64 overflow-auto rounded border">
          <table className="w-full text-2xs">
            <thead className="sticky top-0">
              <tr className="border-b bg-elev">
                <th className="px-1.5 py-1 text-left font-medium text-dim" />
                {lines.map((s, si) => <th key={si} className="px-1.5 py-1 text-right font-medium text-dim">{s.name}</th>)}
              </tr>
            </thead>
            <tbody>
              {categories.map((c, i) => (
                <tr key={i} className="border-b last:border-0">
                  <td className="px-1.5 py-1 text-dim">
                    {c}
                    {breakAt.has(i) && <span className="ml-1 text-faint">（{breaks.find((b) => b.index === i)?.label}）</span>}
                  </td>
                  {lines.map((s, si) => (
                    <td key={si} className="tnum px-1.5 py-1 text-right text-fg">
                      {onPick && s.evidence?.[i]
                        ? <button type="button" className="underline decoration-dotted underline-offset-2 hover:text-accent"
                                  onClick={() => onPick(si, i)}>{textOf(s, i)}</button>
                        : textOf(s, i)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
