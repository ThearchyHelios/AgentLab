import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { ChevronRight, History, RotateCw } from 'lucide-react'
import { api } from '../../api/client'
import { EmptyState, ErrorState, Skeleton, Spinner, StatusPill, toast, useTicker } from '../../components/ui'
import { formatDateTime, parseServerTime } from '../../lib/format'
import { SCHEDULE_TEXT as T } from '../../lib/terms'
import type { Schedule, ScheduleFire } from '../../types'
import { FireKindChip, FireStatusChip, ZonedTime } from './parts'

const PAGE = 20

/**
 * 触发历史：每次触发一行，新的在前，20 条一页、「加载更多」往后接。计划时间按定时任务的时区写；跳过、发起失败
 * 写原因；发起了的链到那次运行、写运行现在的状态；发出去的通知逐条写结果。refreshKey 变了（立即运行之后）从头重取
 */
export function FireHistory({ schedule, refreshKey }: { schedule: Schedule; refreshKey: number }) {
  const [items, setItems] = useState<ScheduleFire[] | null>(null)
  const [next, setNext] = useState<string | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [loadingMore, setLoadingMore] = useState(false)
  const now = useTicker(60_000)
  // 从头重取时作废还在路上的「加载更多」：两份答复交错接上会重复、会漏
  const seq = useRef(0)
  const id = schedule.id

  const load = useCallback(async () => {
    const n = ++seq.current
    try {
      const page = await api.schedules.fires(id, { limit: PAGE })
      if (n !== seq.current) return
      setItems(page.items ?? [])
      setNext(page.next_before ?? null)
      setError(null)
    } catch (e) {
      if (n === seq.current) setError(e)
    }
  }, [id])
  useEffect(() => { void load() }, [load, refreshKey])

  const more = async () => {
    if (!next || loadingMore) return
    const n = seq.current
    setLoadingMore(true)
    try {
      const page = await api.schedules.fires(id, { limit: PAGE, before: next })
      if (n !== seq.current) return
      setItems((cur) => {
        const seen = new Set((cur ?? []).map((f) => f.id))
        return [...(cur ?? []), ...(page.items ?? []).filter((f) => !seen.has(f.id))]
      })
      setNext(page.next_before ?? null)
    } catch (e) {
      toast.error(e)
    } finally {
      setLoadingMore(false)
    }
  }

  let body
  if (items === null) {
    body = error ? <ErrorState error={error} onRetry={() => void load()} compact /> : <Skeleton rows={4} height={44} gap={8} />
  } else if (!items.length) {
    body = (
      <div data-fire-empty="">
        <EmptyState icon={<History size={22} />} title={T.historyEmpty} body={T.historyEmptyHint} offline={false} />
      </div>
    )
  } else {
    body = (
      <div data-fire-list="">
        <ul className="overflow-hidden rounded-lg border bg-panel">
          {items.map((f) => <FireRow key={f.id} fire={f} tz={schedule.timezone} now={now} />)}
        </ul>
        <div className="flex items-center justify-center gap-2 px-3 py-3 text-2xs text-faint">
          <span className="tnum">{T.shown(items.length)}</span>
          {next ? (
            <button type="button" className="btn btn-sm" onClick={() => void more()} disabled={loadingMore} data-fire-more="">
              {loadingMore && <Spinner size={11} />} {T.loadMore}
            </button>
          ) : <span>· {T.allLoaded}</span>}
        </div>
      </div>
    )
  }

  return (
    <section className="mx-auto max-w-4xl p-4" aria-labelledby="schedule-history-title">
      <div className="mb-3 flex flex-wrap items-end gap-x-3 gap-y-2">
        <div className="min-w-0 flex-1">
          <h2 id="schedule-history-title" className="text-sm font-semibold">{T.history}</h2>
          <p className="mt-0.5 text-xs leading-relaxed text-faint">{T.historyHint}</p>
        </div>
        <button type="button" className="btn btn-sm" onClick={() => void load()} data-fire-refresh="">
          <RotateCw size={11} aria-hidden /> {T.historyRefresh}
        </button>
      </div>
      {body}
    </section>
  )
}

function FireRow({ fire: f, tz, now }: { fire: ScheduleFire; tz: string; now: number }) {
  const at = parseServerTime(f.fire_at)
  return (
    <li className="border-b px-3 py-2.5 last:border-b-0" data-fire-id={f.id} data-fire-status={f.status} data-fire-kind={f.kind}>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
        <span className="sr-only">{f.kind === 'manual' ? T.fireAtManual : T.fireAt}</span>
        {at ? <ZonedTime date={at} tz={tz} now={now} className="text-xs text-fg" /> : <span className="text-xs text-faint">—</span>}
        <FireKindChip fire={f} />
        <FireStatusChip fire={f} />
        <span className="flex-1" />
        {f.run_id ? (
          <Link to={`/runs/${encodeURIComponent(f.run_id)}`} className="inline-flex shrink-0 items-center gap-1.5 text-2xs text-dim hover:text-fg hover:underline">
            {f.run_status && <StatusPill status={f.run_status} plain />}
            {T.openRun}
            <ChevronRight size={11} aria-hidden />
          </Link>
        ) : <span className="shrink-0 text-2xs text-faint">{T.noRun}</span>}
      </div>
      {f.reason && (
        <div className="mt-1 text-2xs leading-relaxed text-dim">
          <span className="text-faint">{T.reason}：</span>{f.reason}
        </div>
      )}
      {f.notified?.length > 0 && (
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-2xs">
          <span className="text-faint">{T.notified}</span>
          {f.notified.map((n, i) => (
            <span key={`${n.event}-${i}`} title={formatDateTime(n.at)}
                  style={{ color: n.status === 'ok' ? 'var(--text-dim)' : 'var(--st-failed)' }} data-fire-notice={n.status}>
              {T.event[n.event] ?? n.event} {T.noticeStatus[n.status] ?? n.status}
            </span>
          ))}
        </div>
      )}
    </li>
  )
}
