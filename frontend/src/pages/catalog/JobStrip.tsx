import { useCallback, useEffect, useRef, useState } from 'react'
import { Clock, TriangleAlert } from 'lucide-react'
import { api } from '../../api/client'
import type { SourceBackgroundJob, SourceJobKind } from '../../typesSource'
import { Spinner } from '../../components/ui'
import { JOB_STRIP_TEXT as T } from '../../lib/terms'

// ===========================================================================
// 目录页的后台任务条（next-round 05 (g)）：这个数据源进行中和排队的探查、起草、剖析，每项一行（「起草 3/20」「剖析 排队中，
// 起草结束后开始」），带进度条和提醒（起草调用接连没写出来、剖析最近查询多数超时），右侧「查看」打开对应的对话框（探查到
// 数据源卡片上看）。进度只在这一处：页头的「起草」「数据剖析」只是入口（进行中转圈、排队中时钟，读屏写状态，不写数字）。
//
// 先后由服务端排好（GET /background-jobs：同源队里的位置，排在谁后面取各任务的 waiting_for），这里不自己排。起草、剖析的
// 数取页面跟着的那个任务（每 1.5 秒取一次，比这条队列新）；页面刚建的任务队列还没取到时先接在后面，下一次取回来就按服务端的。
// ===========================================================================

/** 有任务在排队、进行中时多久取一次队列 */
export const STRIP_POLL_MS = 1500

/** 这一行要画的：服务端的那一项，起草、剖析换上页面跟着的任务的数和提醒 */
export interface StripItem extends SourceBackgroundJob {
  notice?: string | null
}

const isActive = (s: string) => s === 'queued' || s === 'running'

/**
 * 取这个源的后台任务队列：载入时取一次；有排队、进行中的任务时每 STRIP_POLL_MS 取一次；页面上的起草、剖析任务换了状态
 * （kick 变了）马上取一次
 */
export function useSourceJobs(sourceId: string, kick: string): { items: SourceBackgroundJob[]; refresh: () => void } {
  const [held, setHeld] = useState<{ src: string; items: SourceBackgroundJob[] } | null>(null)
  const items = held?.src === sourceId ? held.items : []
  const seq = useRef(0)
  const [tick, setTick] = useState(0)
  const refresh = useCallback(() => setTick((n) => n + 1), [])

  useEffect(() => {
    const mine = ++seq.current
    const src = sourceId
    api.sourceChanges.backgroundJobs(src).then((r) => {
      if (mine === seq.current) setHeld({ src, items: r.items })
    }, () => { /* 取不到这一次：下一拍再取（断网时页面顶上有横幅） */ })
  }, [sourceId, kick, tick])

  const active = items.some((i) => isActive(i.status))
  useEffect(() => {
    if (!active) return
    const t = setTimeout(refresh, STRIP_POLL_MS)
    return () => clearTimeout(t)
  }, [active, items, refresh])

  return { items, refresh }
}

/**
 * 服务端的队列和页面跟着的起草、剖析任务合起来：同一个任务换上页面的数和提醒；页面上进行中、队列里还没有的（刚建的）
 * 接在后面；队列里已经结束的不画
 */
export function stripItems(server: SourceBackgroundJob[], live: Partial<Record<SourceJobKind, StripItem | null>>): StripItem[] {
  const out: StripItem[] = []
  const seen = new Set<string>()
  for (const s of server) {
    const mine = live[s.kind]
    const item: StripItem = mine && mine.id === s.id ? { ...s, status: mine.status, done: mine.done, total: mine.total, notice: mine.notice } : s
    seen.add(s.id)
    if (isActive(item.status)) out.push(item)
  }
  for (const kind of ['introspect', 'draft', 'profile'] as SourceJobKind[]) {
    const mine = live[kind]
    if (mine && !seen.has(mine.id) && isActive(mine.status)) out.push(mine)
  }
  return out
}

export function JobStrip({ items, onOpen }: { items: StripItem[]; onOpen: (item: StripItem) => void }) {
  if (!items.length) return null
  return (
    <ul className="shrink-0 divide-y divide-[var(--hairline)] border-b text-xs" aria-label={T.label} data-catalog-job-strip={items.map((i) => i.kind).join(',')}
        style={{ background: 'color-mix(in srgb, var(--accent) 6%, var(--bg-panel))' }}>
      {items.map((item) => <JobRow key={item.id} item={item} onOpen={onOpen} />)}
    </ul>
  )
}

function JobRow({ item, onOpen }: { item: StripItem; onOpen: (item: StripItem) => void }) {
  const kind = T.kind[item.kind]
  const queued = item.status === 'queued'
  const behind = queued && item.waiting_for ? T.behind[item.waiting_for] ?? null : null
  const text = queued ? (behind ? T.queuedBehind(kind, behind) : T.queued(kind)) : T.running(kind, item.done, item.total)
  const notice = item.notice?.trim() || null
  return (
    <li className="flex items-center gap-2 px-4 py-2" data-catalog-job={item.kind} data-catalog-job-status={item.status}
        data-progress={`${item.done}/${item.total}`}>
      {queued ? <Clock size={12} className="shrink-0 text-faint" aria-hidden /> : <Spinner size={12} />}
      <div className="min-w-0 flex-1">
        <p className="tnum break-words font-medium" data-catalog-job-text="">{text}</p>
        {notice && (
          // 提醒写全文、可以折行：窄屏、触屏上没有悬停
          <p className="mt-0.5 flex min-w-0 items-start gap-1 text-2xs leading-relaxed text-[var(--warn)]" role="status" data-catalog-job-notice="">
            <TriangleAlert size={10} className="mt-0.5 shrink-0" aria-hidden />
            <span className="min-w-0 break-words">{notice}</span>
          </p>
        )}
        <div className="mt-1 h-1 max-w-xs overflow-hidden rounded-full bg-hover" role="progressbar" aria-label={T.progress(kind, item.done, item.total)}
             aria-valuemin={0} aria-valuemax={item.total} aria-valuenow={item.done}>
          <div className="h-full rounded-full bg-[var(--accent)] transition-[width] duration-300"
               style={{ width: `${item.total ? (item.done / item.total) * 100 : 0}%` }} />
        </div>
      </div>
      <button type="button" className="btn btn-sm shrink-0" onClick={() => onOpen(item)} title={T.viewHint[item.kind]} data-catalog-job-open="">
        {T.view}
      </button>
    </li>
  )
}
