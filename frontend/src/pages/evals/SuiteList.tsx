import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { CalendarClock, ClipboardCheck, FileUp, Plus } from 'lucide-react'
import { api } from '../../api/client'
import { EmptyState, ErrorState, Skeleton, promptDialog, toast } from '../../components/ui'
import { useOnReconnect } from '../../store/catalog'
import { formatTime } from '../../lib/format'
import { leavePass } from '../../lib/leave'
import { EVAL_TEXT as T, pct, rateText } from '../../lib/termsEval'
import type { EvalSuite } from '../../typesEval'
import { ImportDialog } from './ImportDialog'
import { EvalStatusPill, Trend } from './parts'

/**
 * 题集列表：每个题集写题数、最近一次评测（状态和准确率）、同一变体最近几次的准确率（趋势）、定时。这几样都是服务端
 * 挑好、算好的（GET /api/evals/suites 的 latest、trend、schedules），这里只显示。有评测在跑时每 5 秒原地刷新一次
 */
const LIVE_REFRESH_MS = 5_000

export function SuiteList() {
  const navigate = useNavigate()
  const [rows, setRows] = useState<EvalSuite[] | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [archived, setArchived] = useState(false)
  const [importing, setImporting] = useState(false)
  const seq = useRef(0)

  const load = useCallback(async () => {
    const n = ++seq.current
    try {
      const r = await api.evals.suites({ archived })
      if (n !== seq.current) return
      setRows(r.suites)
      setError(null)
    } catch (e) {
      if (n !== seq.current) return
      setError(e)
    }
  }, [archived])
  useEffect(() => { void load() }, [load])
  useOnReconnect(() => { void load() })
  const live = !!rows?.some((s) => s.latest?.running)
  useEffect(() => {
    if (!live) return
    const t = setInterval(() => { if (!document.hidden) void load() }, LIVE_REFRESH_MS)
    return () => clearInterval(t)
  }, [live, load])

  const create = async () => {
    const name = await promptDialog({
      title: T.list.createTitle, label: T.list.createName, placeholder: T.list.createNamePlaceholder,
      confirmLabel: T.list.create,
    })
    if (!name) return
    try {
      const made = await api.evals.createSuite({ name })
      toast.ok(T.list.created(made.name))
      navigate(`/evals/suites/${encodeURIComponent(made.id)}`)
    } catch (e) {
      toast.error(e)
    }
  }

  const actions = (
    <div className="flex flex-wrap items-center gap-2">
      <label className="flex items-center gap-1.5 text-2xs text-dim">
        <input type="checkbox" checked={archived} onChange={(e) => setArchived(e.target.checked)} data-evals-show-archived="" />
        {T.list.showArchived}
      </label>
      <button type="button" className="btn btn-sm" onClick={() => setImporting(true)} data-evals-import="">
        <FileUp size={12} aria-hidden /> {T.list.import}
      </button>
      <button type="button" className="btn btn-primary btn-sm" onClick={() => void create()} data-evals-create="">
        <Plus size={12} aria-hidden /> {T.list.create}
      </button>
    </div>
  )

  let body
  if (rows === null) {
    body = error ? <ErrorState error={error} onRetry={() => void load()} /> : <Skeleton rows={3} height={64} gap={8} />
  } else if (!rows.length) {
    body = (
      <div data-evals-empty="">
        <EmptyState icon={<ClipboardCheck size={22} />} title={T.list.emptyTitle} body={T.list.emptyBody} offline={false} />
      </div>
    )
  } else {
    body = (
      <ul className="space-y-2" aria-label={T.list.label} data-evals-suites="">
        {rows.map((s) => <SuiteRow key={s.id} s={s} />)}
      </ul>
    )
  }

  return (
    <div className="mx-auto max-w-5xl space-y-3 p-4">
      <div className="flex justify-end">{actions}</div>
      {body}
      <ImportDialog open={importing} onClose={() => setImporting(false)}
                    onImported={(id) => navigate(`/evals/suites/${encodeURIComponent(id)}`, leavePass())} />
    </div>
  )
}

function SuiteRow({ s }: { s: EvalSuite }) {
  const latest = s.latest
  return (
    <li className="rounded-lg border bg-panel px-3 py-2.5" data-eval-suite={s.id}>
      <div className="flex flex-wrap items-start gap-x-4 gap-y-2">
        <div className="min-w-0 flex-1 basis-60">
          <div className="flex min-w-0 flex-wrap items-center gap-1.5">
            <Link to={`/evals/suites/${encodeURIComponent(s.id)}`} className="min-w-0 truncate text-sm font-medium hover:underline"
                  data-eval-suite-name="">
              {s.name}
            </Link>
            {s.archived && <span className="chip shrink-0">{T.list.archived}</span>}
          </div>
          <div className="mt-0.5 truncate text-xs text-dim" title={s.description || undefined}>
            {T.list.cases(s.counts?.cases ?? 0, s.counts?.enabled ?? 0)}
            {s.description && <><span className="text-faint"> · </span>{s.description}</>}
          </div>
        </div>
        <div className="min-w-0 basis-56" data-eval-suite-latest={latest?.id ?? 'none'}>
          <div className="text-2xs text-faint">{T.list.latest}</div>
          {latest ? (
            <Link to={`/evals/runs/${encodeURIComponent(latest.id)}`} className="mt-0.5 flex min-w-0 flex-wrap items-center gap-1.5 text-xs hover:underline">
              <EvalStatusPill status={latest.status} plain />
              <span className="tnum font-medium" title={rateText(latest.accuracy)}>{latest.accuracy?.n ? pct(latest.accuracy.rate) : ''}</span>
              <span className="text-faint">{formatTime(latest.created_at)}</span>
            </Link>
          ) : <div className="mt-0.5 text-xs text-faint">{T.list.noRun}</div>}
        </div>
        <div className="shrink-0" title={T.list.trend}>
          <div className="text-2xs text-faint">{T.list.trend}</div>
          <div className="mt-0.5"><Trend points={s.trend} /></div>
        </div>
      </div>
      <SuiteSchedules s={s} />
    </li>
  )
}

/** 这个题集的定时：有就逐个列出（暂停的标出来，点了去定时任务），没有就给「设定时」 */
export function SuiteSchedules({ s }: { s: EvalSuite }) {
  const list = s.schedules ?? []
  return (
    <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-2xs" data-eval-suite-schedules={list.length}>
      <CalendarClock size={11} aria-hidden className="text-faint" />
      <span className="text-faint">{T.list.schedules}</span>
      {list.map((x) => (
        <Link key={x.id} to={`/schedules/${encodeURIComponent(x.id)}`} className="chip hover:text-fg" data-eval-suite-schedule={x.id}
              title={x.description_text}>
          {x.name}{!x.enabled && ` · ${T.list.paused}`}
        </Link>
      ))}
      {!list.length && !s.archived && (
        <Link to={`/schedules/new?target_kind=eval_suite&target_id=${encodeURIComponent(s.id)}`} className="text-dim underline hover:text-fg"
              title={T.list.addScheduleTitle} data-eval-suite-add-schedule="">
          {T.list.addSchedule}
        </Link>
      )}
      {!list.length && s.archived && <span className="text-faint">{T.list.noSchedule}</span>}
    </div>
  )
}
