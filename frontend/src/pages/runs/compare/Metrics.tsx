import { Fragment, useEffect, useId, useRef, useState } from 'react'
import clsx from 'clsx'
import { ChevronRight } from 'lucide-react'
import { NONE } from '../../../lib/format'
import { RUN_COMPARE_TEXT as T } from '../../../lib/terms'
import type { RunCompareAttribution, RunCompareDecomposition, RunCompareMetric } from '../../../types'
import { Block, ClassChip } from './parts'
import { changeText, metricKey, sameValue, shareText, signedNumber, splitReason, valueText } from './model'

/** 维度归因列前几项：后端按变化从大到小排好，多的只写还有几项 */
const ATTR_TOP = 10
/** 口径效应、数据效应在条形和图例里的颜色：和「口径与数据都变」「只数据变」两个分类标签同色 */
const CALIBER_COLOR = 'var(--warn)'
const DATA_COLOR = 'var(--accent)'
/**
 * 指标名一栏：窄屏上表格横着滚时钉在左边（滚到分类、拆分那几栏也知道是哪个指标），右边一道细线和后面的内容隔开。
 * 钉住的格子要有自己的底色，否则滚过去的数字会透出来
 */
const STICKY = 'sticky left-0 z-[1] min-w-[9em] shadow-[inset_-1px_0_0_var(--border)]'

/**
 * 指标表：基准 → 本次、变化量和百分比（渲染值照服务端给的写）、分类、能严格拆分的口径效应和数据效应（并列的数加一段
 * 两截的条形），不能拆、比不了的写原因（splitReason）。每行能展开：变化说明、输入对比、维度归因前 10 项。
 *
 * 表格比窄屏宽，在自己的框里横向滚动（框能用键盘聚焦），页面本身不横向溢出。展开的明细贴着框的左边、宽度等于框的
 * 可见宽度：窄屏上不用横着滚也读得全
 */
export function Metrics({ metrics, filter, onClearFilter }: {
  metrics: RunCompareMetric[]; filter: string | null; onClearFilter: () => void
}) {
  const id = useId()
  const [open, setOpen] = useState<ReadonlySet<string>>(() => new Set())
  const scroller = useRef<HTMLDivElement>(null)
  const shown = filter ? metrics.filter((m) => m.classification === filter) : metrics
  const split = metrics.some((m) => m.decomposition)

  // 明细的宽度跟着框的可见宽度走（CSS 变量），表格多宽都不撑出去
  useEffect(() => {
    const el = scroller.current
    if (!el) return
    const sync = () => el.style.setProperty('--compare-view', `${el.clientWidth}px`)
    sync()
    const ro = new ResizeObserver(sync)
    ro.observe(el)
    return () => ro.disconnect()
  }, [metrics.length])

  const toggle = (k: string) => setOpen((s) => {
    const next = new Set(s)
    if (next.has(k)) next.delete(k)
    else next.add(k)
    return next
  })

  return (
    <Block id={`${id}-metrics`} title={T.metrics} count={T.count(metrics.length)} attr={{ 'data-compare-metrics': '' }}>
      {!metrics.length ? (
        <p className="py-4 text-center text-xs text-faint" data-compare-empty="metrics">{T.metricsEmpty}</p>
      ) : (
        <>
          {filter && (
            <div className="mb-2 flex flex-wrap items-center gap-2 text-2xs text-dim" data-compare-filter="">
              <span>{T.filterOn(T.classLabel[filter] ?? T.classOther, shown.length)}</span>
              <button type="button" className="btn btn-xs" data-compare-filter-clear="" onClick={onClearFilter}>{T.showAll}</button>
            </div>
          )}
          {split && (
            <p className="mb-2 flex gap-2 text-2xs leading-relaxed text-faint" data-compare-legend="">
              <span className="mt-1 flex shrink-0 gap-1" aria-hidden>
                <i className="block h-2 w-2 rounded-sm" style={{ background: CALIBER_COLOR }} />
                <i className="block h-2 w-2 rounded-sm" style={{ background: DATA_COLOR }} />
              </span>
              <span>{T.legend}</span>
            </p>
          )}
          <div ref={scroller} className="overflow-x-auto rounded border focus-visible:outline-2 focus-visible:outline-[color:var(--accent)]"
               data-compare-scroll="" tabIndex={0} role="region" aria-label={T.metrics}>
            <table className="w-full min-w-[760px] border-collapse text-xs">
              <thead>
                <tr className="border-b text-left text-2xs text-faint">
                  <th scope="col" className={clsx(STICKY, 'bg-panel px-2.5 py-1.5 font-medium')}>{T.colMetric}</th>
                  <th scope="col" className="px-2.5 py-1.5 text-right font-medium">{T.colBase}</th>
                  <th scope="col" className="px-2.5 py-1.5 text-right font-medium">{T.colTarget}</th>
                  <th scope="col" className="px-2.5 py-1.5 text-right font-medium">{T.colDelta}</th>
                  <th scope="col" className="px-2.5 py-1.5 font-medium">{T.colClass}</th>
                  <th scope="col" className="px-2.5 py-1.5 font-medium">{T.colSplit}</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((m) => {
                  const k = metricKey(m)
                  return <MetricRows key={k} m={m} k={k} detailId={`${id}-d-${k}`} open={open.has(k)} onToggle={() => toggle(k)} />
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
    </Block>
  )
}

function MetricRows({ m, k, detailId, open, onToggle }: {
  m: RunCompareMetric; k: string; detailId: string; open: boolean; onToggle: () => void
}) {
  const pct = m.delta_pct != null && m.delta_pct !== 0 ? changeText(m.delta_pct) : null
  const why = m.decomposition ? null : splitReason(m)
  return (
    <Fragment>
      <tr className={clsx('border-b align-top last:border-0', open && 'bg-hover')} data-compare-metric={k} data-class={m.classification}>
        <th scope="row" className={clsx(STICKY, open ? 'bg-hover' : 'bg-panel', 'px-2.5 py-2 text-left font-normal')}>
          <button type="button" data-compare-expand="" aria-expanded={open} aria-controls={open ? detailId : undefined}
                  title={open ? T.collapse(m.name) : T.expand(m.name)} onClick={onToggle}
                  className="group flex max-w-[220px] items-start gap-1 rounded-sm text-left focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[color:var(--accent)]">
            <ChevronRight size={12} aria-hidden
                          className={clsx('mt-0.5 shrink-0 text-faint transition-transform group-hover:text-dim', open && 'rotate-90')} />
            <span className="min-w-0">
              <span className="block font-medium text-fg [overflow-wrap:anywhere]">{m.name}</span>
              {m.card_title && <span className="block text-2xs text-faint [overflow-wrap:anywhere]">{m.card_title}</span>}
            </span>
          </button>
        </th>
        <td className="tnum whitespace-nowrap px-2.5 py-2 text-right text-dim" data-compare-base="">{m.base_rendered || NONE}</td>
        <td className="tnum whitespace-nowrap px-2.5 py-2 text-right font-medium text-fg" data-compare-target="">{m.target_rendered || NONE}</td>
        <td className="whitespace-nowrap px-2.5 py-2 text-right">
          <div className="tnum" data-compare-delta="">{deltaText(m)}</div>
          {pct && <div className="tnum text-2xs text-faint" data-compare-pct="">{pct}</div>}
        </td>
        <td className="px-2.5 py-2"><ClassChip value={m.classification} /></td>
        <td className="px-2.5 py-2">
          {m.decomposition ? (
            <Split d={m.decomposition} total={totalText(m)} />
          ) : why ? (
            <p className="max-w-[280px] text-2xs leading-relaxed text-dim [overflow-wrap:anywhere]" data-compare-decomp-note=""
               data-from={m.decomposition_note ? 'note' : 'reasons'}>
              {why}
            </p>
          ) : (
            <span className="text-faint">{NONE}</span>
          )}
        </td>
      </tr>
      {open && (
        <tr id={detailId} data-compare-detail={k} className="border-b bg-bg last:border-0">
          <td colSpan={6} className="p-0">
            <MetricDetail m={m} />
          </td>
        </tr>
      )}
    </Fragment>
  )
}

/** 变化量：服务端渲染的写法；没给（没变化、缺一边）时，是 0 的写「无变化」，其余写「—」 */
function deltaText(m: RunCompareMetric): string {
  if (m.delta_rendered) return m.delta_rendered
  return m.delta === 0 ? T.noChange : NONE
}

/**
 * 拆分那句话里的总变化：口径变了、数值却碰巧没变时服务端不给变化量的写法（两效应正好抵消），写「0」，
 * 不写成「两者之和等于总变化 无变化」
 */
function totalText(m: RunCompareMetric): string {
  if (m.delta_rendered) return m.delta_rendered
  return m.delta === 0 ? '0' : NONE
}

/**
 * 能严格拆分的：口径效应、数据效应并列，下面一段两截的条形按两者的绝对值分长短。一正一负（互相抵消）时负的那截画
 * 斜纹：长短只说谁的分量大，方向看数的正负号
 */
function Split({ d, total }: { d: RunCompareDecomposition; total: string }) {
  const a = Math.abs(d.caliber_effect)
  const b = Math.abs(d.data_effect)
  const sum = a + b
  const fill = (color: string, negative: boolean) => (negative
    ? `repeating-linear-gradient(135deg, ${color} 0 2px, color-mix(in srgb, ${color} 35%, transparent) 2px 4px)`
    : color)
  return (
    <div data-compare-decomp="" title={T.splitSum(d.caliber_rendered, d.data_rendered, total)}>
      <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-2xs">
        <span className="inline-flex items-center gap-1 whitespace-nowrap" data-compare-caliber="">
          <i className="block h-2 w-2 rounded-sm" style={{ background: CALIBER_COLOR }} aria-hidden />
          <span className="text-faint">{T.caliber}</span>
          <span className="tnum font-medium text-fg">{d.caliber_rendered}</span>
        </span>
        <span className="inline-flex items-center gap-1 whitespace-nowrap" data-compare-data="">
          <i className="block h-2 w-2 rounded-sm" style={{ background: DATA_COLOR }} aria-hidden />
          <span className="text-faint">{T.data}</span>
          <span className="tnum font-medium text-fg">{d.data_rendered}</span>
        </span>
      </div>
      {sum > 0 && (
        <span className="mt-1 flex h-1.5 w-32 overflow-hidden rounded-sm bg-hover" data-compare-bar="" aria-hidden>
          <i className="block h-full" style={{ width: `${(a / sum) * 100}%`, background: fill(CALIBER_COLOR, d.caliber_effect < 0) }} />
          <i className="block h-full" style={{ width: `${(b / sum) * 100}%`, background: fill(DATA_COLOR, d.data_effect < 0) }} />
        </span>
      )}
    </div>
  )
}

/**
 * 展开的明细：变化说明、拆分写成一句话（不能拆的写服务端说的为什么）、输入对比、维度归因。贴着滚动框的左边，宽度
 * 等于框的可见宽度：窄屏上表格里「拆分或原因」那一格比钉在左边的指标名右侧剩下的地方宽，横着滚到哪儿都看不全，
 * 这里读得全
 */
function MetricDetail({ m }: { m: RunCompareMetric }) {
  return (
    <div className="sticky left-0 box-border grid gap-4 px-3 py-3 lg:grid-cols-2" style={{ width: 'var(--compare-view, 100%)' }}>
      <div className="min-w-0 space-y-3">
        {!!m.reasons.length && (
          <div>
            <h3 className="mb-1 text-2xs font-medium text-faint">{T.reasons}</h3>
            <ul className="list-disc space-y-0.5 pl-4 text-xs leading-relaxed text-dim [overflow-wrap:anywhere]" data-compare-reasons="">
              {m.reasons.map((r, i) => <li key={i}>{r}</li>)}
            </ul>
          </div>
        )}
        {m.decomposition ? (
          <p className="text-xs leading-relaxed text-fg" data-compare-decomp-sum="">
            {T.splitSum(m.decomposition.caliber_rendered, m.decomposition.data_rendered, totalText(m))}
          </p>
        ) : m.decomposition_note ? (
          <p className="text-xs leading-relaxed text-dim [overflow-wrap:anywhere]" data-compare-detail-note="">{m.decomposition_note}</p>
        ) : null}
        <Inputs m={m} />
      </div>
      <div className="min-w-0">
        <Attribution a={m.attribution} classification={m.classification} />
      </div>
    </div>
  )
}

function Inputs({ m }: { m: RunCompareMetric }) {
  return (
    <div>
      <h3 className="mb-1 text-2xs font-medium text-faint">{T.inputs}</h3>
      {!m.inputs.length ? (
        <p className="text-2xs text-faint" data-compare-inputs-none="">{T.inputsNone}</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-2xs" data-compare-inputs="">
            <thead>
              <tr className="border-b text-left text-faint">
                <th scope="col" className="py-1 pr-2 font-medium">{T.colInput}</th>
                <th scope="col" className="px-2 py-1 text-right font-medium">{T.colBase}</th>
                <th scope="col" className="py-1 pl-2 text-right font-medium">{T.colTarget}</th>
              </tr>
            </thead>
            <tbody>
              {m.inputs.map((inp) => {
                // 遮罩列：两边的值服务端都不给，写「已遮罩」，也不说变没变
                const changed = !inp.masked && !sameValue(inp.base, inp.target)
                const shown = (v: unknown) => (inp.masked ? T.masked : valueText(v))
                return (
                  <tr key={inp.path} className="border-b last:border-0" data-compare-input={inp.path} data-changed={changed ? '' : undefined}
                      data-masked={inp.masked ? '' : undefined}>
                    <th scope="row" className="mono py-1 pr-2 text-left font-normal text-dim [overflow-wrap:anywhere]">{inp.path}</th>
                    <td className="tnum px-2 py-1 text-right text-dim [overflow-wrap:anywhere]">{shown(inp.base)}</td>
                    <td className={clsx('tnum py-1 pl-2 text-right [overflow-wrap:anywhere]', changed ? 'font-medium text-fg' : 'text-dim')}>
                      {changed && <span className="mr-1 inline-block h-1.5 w-1.5 rounded-full align-middle" style={{ background: DATA_COLOR }} aria-hidden />}
                      {shown(inp.target)}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

/**
 * 维度归因：按键对齐两边查询结果的行，列前 10 项的键、两边的值、变化和占比。占比只在度量列之和等于指标的输入时才有，
 * 没有时那一栏写「—」，下面照服务端的说明写原因（没给说明时写一句通用的）。归不了因的（查询结果截断、度量列遮罩、
 * 快照读不出来）服务端给空的 rows 和一句说明：不画空表，只写这句话。服务端有意不做归因的几类（SQL 结构变、无法
 * 比较、新增、移除、未变）写为什么不做，不说成「没有明细」
 */
function Attribution({ a, classification }: { a: RunCompareAttribution | null; classification: string }) {
  if (!a) {
    return (
      <div>
        <h3 className="mb-1 text-2xs font-medium text-faint">{T.attribution}</h3>
        <p className="text-2xs leading-relaxed text-faint [overflow-wrap:anywhere]" data-compare-attr-none="">
          {T.attrSkipped[classification] ?? T.attrNone}
        </p>
      </div>
    )
  }
  const rows = a.rows.slice(0, ATTR_TOP)
  const more = a.rows.length - rows.length
  const note = a.note || (!rows.length ? T.attrNone : a.rows.some((r) => r.share == null) ? T.attrNoShare : null)
  return (
    <div data-compare-attr="">
      <h3 className="mb-1 flex flex-wrap items-baseline gap-x-2 text-2xs">
        <span className="font-medium text-faint">{T.attribution}</span>
        <span className="text-faint [overflow-wrap:anywhere]">{T.attrBy(a.measure)}{more > 0 ? ` · ${T.attrTop(ATTR_TOP)}` : ''}</span>
      </h3>
      {!!rows.length && <div className="overflow-x-auto">
        <table className="w-full border-collapse text-2xs">
          <thead>
            <tr className="border-b text-left text-faint">
              <th scope="col" className="py-1 pr-2 font-medium">{a.columns.length ? a.columns.join(' / ') : T.colKey}</th>
              <th scope="col" className="px-2 py-1 text-right font-medium">{T.colBase}</th>
              <th scope="col" className="px-2 py-1 text-right font-medium">{T.colTarget}</th>
              <th scope="col" className="px-2 py-1 text-right font-medium">{T.colDelta}</th>
              <th scope="col" className="py-1 pl-2 text-right font-medium">{T.colShare}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i} className="border-b last:border-0" data-compare-attr-row="">
                <th scope="row" className="min-w-[4.5em] py-1 pr-2 text-left font-normal text-fg [overflow-wrap:anywhere]">
                  {r.key.map(valueText).join(' / ')}
                </th>
                <td className="tnum px-2 py-1 text-right text-dim">{valueText(r.base)}</td>
                <td className="tnum px-2 py-1 text-right text-dim">{valueText(r.target)}</td>
                <td className="tnum px-2 py-1 text-right text-fg">{signedNumber(r.delta)}</td>
                <td className="tnum py-1 pl-2 text-right text-dim" data-compare-share="" title={r.share == null ? note ?? undefined : undefined}>
                  {r.share == null ? NONE : shareText(r.share)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>}
      <div className="mt-1 space-y-0.5 text-2xs text-faint">
        {more > 0 && <p data-compare-attr-more="">{T.attrMore(more)}</p>}
        {!!rows.length && a.total_delta != null && (
          <p className="tnum" data-compare-attr-total="">{T.attrTotal(signedNumber(a.total_delta))}</p>
        )}
        {note && <p className="leading-relaxed text-dim [overflow-wrap:anywhere]" data-compare-attr-note="">{note}</p>}
      </div>
    </div>
  )
}
