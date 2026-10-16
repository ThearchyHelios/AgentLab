import { useEffect, useState } from 'react'
import clsx from 'clsx'
import { api } from '../../api/client'
import type { ObservationPoint, ObservationSeries, SourceChange } from '../../typesSource'
import { ErrorState, Skeleton } from '../../components/ui'
import { formatDateTime, formatRelative } from '../../lib/format'
import { SOURCE_CHANGE_TEXT as CT, TABLE_CHANGES_TEXT as T } from '../../lib/terms'
import { SEVERITY_MARK } from './ChangesQueue'

// ===========================================================================
// 表详情的「结构与取值变化」一节（next-round 03 B2、B3）：这张表测到的变化（开着的在前，点「去处理」到「变化」视图里那一条），
// 撤销过「已列全」的照实说；最近几次取值观察（行数、码值分布、最新业务日期、按日行数），每次写观察的口径（整表、业务日期
// 窗口、块采样、按存储顺序的前 N 行），口径换过的说一句「口径不同的不互相比较」。数都是服务端给的（占比、合计），这里只照写；
// 现在被遮罩的列服务端不给。
// ===========================================================================

/** 这张表开着的变化最多列几条（多的在「变化」视图里看） */
const PENDING_SHOWN = 8
/** 处理过的最多列几条 */
const HANDLED_SHOWN = 5

export function TableChanges({ sourceId, table, refreshKey, onOpenChange }: {
  sourceId: string
  table: string
  /** 目录版本：处理过一条、目录变了时重取 */
  refreshKey: number
  onOpenChange: (id: string) => void
}) {
  const [data, setData] = useState<{ pending: SourceChange[]; pendingTotal: number; handled: SourceChange[]; series: ObservationSeries[] } | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [seq, setSeq] = useState(0)

  useEffect(() => {
    let alive = true
    setError(null)
    Promise.all([
      api.sourceChanges.list(sourceId, { status: 'pending', table, limit: PENDING_SHOWN }),
      api.sourceChanges.list(sourceId, { status: 'handled', table, limit: HANDLED_SHOWN }),
      api.sourceChanges.observations(sourceId, table),
    ]).then(([pending, handled, obs]) => {
      if (!alive) return
      setData({ pending: pending.items, pendingTotal: pending.total, handled: handled.items, series: obs.series })
    }, (e) => { if (alive) setError(e) })
    return () => { alive = false }
  }, [sourceId, table, refreshKey, seq])

  // 撤销过「已列全」的：取值观察测到目录之外的取值时，机器唯一会改的目录事实（目录-D6）
  const revoked = data ? [...data.pending, ...data.handled].filter((c) => c.kind === 'codes_new' && c.after?.complete_revoked === true) : []

  return (
    <section aria-labelledby="catalog-sec-changes" data-catalog-section="changes">
      <div className="mb-2 flex flex-wrap items-baseline gap-2">
        <h3 id="catalog-sec-changes" className="text-xs font-semibold">{T.title}</h3>
        <span className="text-2xs text-faint">{T.hint}</span>
      </div>
      <div className="space-y-3 rounded-lg border bg-panel px-3 py-2.5" data-table-changes={data ? `${data.pendingTotal},${data.handled.length}` : 'loading'}>
        {error
          ? <ErrorState error={error} onRetry={() => setSeq((n) => n + 1)} compact />
          : !data
            ? <Skeleton rows={3} height={14} gap={8} />
            : (
              <>
                <div>
                  <h4 className="text-2xs font-medium text-dim">{T.changesHead}</h4>
                  {!data.pending.length && !data.handled.length
                    ? <p className="mt-1 text-xs text-faint" data-table-changes-none="">{T.changesNone}</p>
                    : (
                      <ul className="mt-1 space-y-1">
                        {[...data.pending, ...data.handled].map((c) => <ChangeLine key={c.id} change={c} onOpen={onOpenChange} />)}
                      </ul>
                    )}
                  {data.pendingTotal > data.pending.length && (
                    <p className="mt-1 text-2xs text-faint">{T.changesMore(data.pendingTotal - data.pending.length)}</p>
                  )}
                </div>
                {revoked.length > 0 && (
                  <div data-table-revoked={revoked.length}>
                    <h4 className="text-2xs font-medium text-dim">{T.revokedHead}</h4>
                    <ul className="mt-1 space-y-0.5">
                      {revoked.map((c) => (
                        <li key={c.id} className="text-xs text-[var(--warn)]">
                          {T.revoked(c.column ?? '', formatDateTime(c.detected_at), c.after?.catalog_version ?? null)}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
                <div>
                  <h4 className="text-2xs font-medium text-dim">{T.observeHead}</h4>
                  {!data.series.length
                    ? <p className="mt-1 text-xs text-faint" data-table-observe-none="">{T.observeNone}</p>
                    : <div className="mt-1 divide-y divide-[var(--hairline)]">{data.series.map((s) => <SeriesRow key={`${s.kind}:${s.column}`} series={s} />)}</div>}
                </div>
              </>
            )}
      </div>
    </section>
  )
}

function ChangeLine({ change: c, onOpen }: { change: SourceChange; onOpen: (id: string) => void }) {
  const mark = SEVERITY_MARK[c.severity]
  const pending = c.status === 'open' || c.status === 'acknowledged'
  return (
    <li className="flex items-start gap-2 text-xs" data-table-change={c.id} data-table-change-status={c.status}>
      <mark.Icon size={11} className="mt-0.5 shrink-0" style={{ color: mark.color }} aria-hidden />
      <span className="min-w-0 flex-1">
        <span className="sr-only">{CT.severity[c.severity]}：</span>
        <span className={clsx('break-words', !pending && 'text-dim')}>{c.summary}</span>
        <span className="ml-1.5 text-2xs text-faint">{CT.category[c.category]} · {CT.status[c.status]} · {formatRelative(c.last_seen_at)}</span>
      </span>
      <button type="button" className="btn btn-xs shrink-0" onClick={() => onOpen(c.id)} data-table-change-open="">
        {pending ? T.openChange : T.viewChange}
      </button>
    </li>
  )
}

/** 一种观察（行数、某一列的码值分布、最新业务日期、按日行数）：最近一次的结论、口径、什么时候，再往前几次收在下面 */
function SeriesRow({ series: s }: { series: ObservationSeries }) {
  const [latest, ...older] = s.points
  if (!latest) return null
  return (
    <div className="py-1.5" data-table-observe={`${s.kind}:${s.column}`} data-observe-basis-changed={s.basis_changed ? '1' : '0'}>
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-xs">
        <span className="font-medium">{T.observeKind[s.kind]}</span>
        {s.column && <span className="mono text-2xs text-faint">{s.column}</span>}
        <span className="min-w-0 flex-1 break-words text-dim" data-observe-value="">
          <PointValue kind={s.kind} point={latest} />
        </span>
      </div>
      <p className="mt-0.5 text-2xs text-faint">
        <span data-observe-basis="">{T.basis(latest.basis_text)}</span>
        {latest.at && <span title={formatDateTime(latest.at)}> · {T.at(formatRelative(latest.at))}</span>}
      </p>
      {s.basis_changed && <p className="mt-0.5 text-2xs text-[var(--warn)]" data-observe-basis-note="">{T.basisChanged}</p>}
      {s.kind === 'daily_volume' && <DailyBars point={latest} />}
      {older.length > 0 && (
        <details className="mt-1 text-2xs text-faint">
          <summary className="cursor-pointer select-none">{T.history(older.length)}</summary>
          <ul className="mt-1 space-y-0.5">
            {older.map((p, i) => (
              <li key={i} className="flex flex-wrap gap-x-2" data-observe-history="">
                <span className="tnum">{p.at ? formatDateTime(p.at) : '—'}</span>
                <span className="text-dim"><PointValue kind={s.kind} point={p} /></span>
                <span>{T.basis(p.basis_text)}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}

function PointValue({ kind, point }: { kind: ObservationSeries['kind']; point: ObservationPoint }) {
  const d = point.data ?? {}
  if (kind === 'rows') return <span className="tnum">{T.rows(d.rows ?? null, !!d.at_least)}</span>
  if (kind === 'freshness') return <span className="tnum">{T.freshness(d.max ?? null)}</span>
  if (kind === 'daily_volume') {
    return <span className="tnum">{T.daily(d.days_with_data ?? 0, d.total ?? 0)}{d.since && d.until ? `（${T.dailyRange(d.since, d.until)}）` : ''}</span>
  }
  const values: [string, number, number][] = d.values ?? []
  return (
    <span>
      <span className="tnum">{T.codesTop(values.length, d.rows ?? 0)}</span>
      {values.length > 0 && <span className="ml-1.5 mono">{values.slice(0, 6).map(([code, , share]) => T.code(code, share)).join(' · ')}</span>}
      {d.truncated && <span className="ml-1.5 text-[var(--warn)]">{T.codesTruncated}</span>}
    </span>
  )
}

/** 按日行数的小柱图：只给眼睛看，数字写在旁边那句和读屏名称里 */
function DailyBars({ point }: { point: ObservationPoint }) {
  const days: [string, number][] = point.data?.days ?? []
  if (!days.length) return null
  const max = Math.max(...days.map(([, n]) => n), 1)
  const d = point.data ?? {}
  return (
    <div className="mt-1 flex h-6 items-end gap-px" role="img" data-observe-bars={days.length}
         aria-label={T.dailyChart(d.since ?? days[0][0], d.until ?? days[days.length - 1][0], d.days_with_data ?? 0, d.total ?? 0)}>
      {days.map(([day, n]) => (
        <span key={day} className="min-w-[2px] flex-1 rounded-t-sm" title={`${day}：${n}`}
              style={{ height: `${Math.max(n ? 8 : 0, (n / max) * 100)}%`, background: n ? 'var(--accent)' : 'var(--border-strong)' }} />
      ))}
    </div>
  )
}
