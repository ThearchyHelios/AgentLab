import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { History } from 'lucide-react'
import { api } from '../../api/client'
import type { CatalogTableUsage } from '../../types'
import { Skeleton, StatusPill } from '../../components/ui'
import { humanizeError } from '../../lib/errors'
import { formatDateTime, formatTime } from '../../lib/format'
import { CATALOG_USAGE_TEXT as T, RUN_CLASS_LABEL } from '../../lib/terms'

// ===========================================================================
// 表详情里「用过这张表的运行」（GET /catalog/{table}/usage，新的在前）：审阅一项之前看看这张表都被哪些运行查过，
// 点进运行记录核对。默认不取，点「查看」才取（每张表都取一遍没必要）；从审阅队列打开时直接取（autoLoad）。
// 使用次数是 0 的不取，直接写还没有运行查询过。
// ===========================================================================

const LIMIT = 20

export function TableUsageList({ sourceId, table, usage, autoLoad = false }: {
  sourceId: string
  table: string
  /** 清单上的使用次数：0 就不取 */
  usage: number
  autoLoad?: boolean
}) {
  const [data, setData] = useState<CatalogTableUsage | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [wanted, setWanted] = useState(autoLoad)
  const seq = useRef(0)

  const load = useCallback(() => {
    const mine = ++seq.current
    setError(null)
    api.dataCatalog.usage(sourceId, table, LIMIT)
      .then((d) => { if (mine === seq.current) setData(d) })
      .catch((e) => { if (mine === seq.current) setError(e) })
  }, [sourceId, table])

  // 换表：清掉上一张的，按需要重新取
  useEffect(() => {
    setData(null)
    setError(null)
    setWanted(autoLoad)
  }, [sourceId, table, autoLoad])
  useEffect(() => {
    if (wanted && usage > 0) load()
  }, [wanted, usage, load])

  if (usage <= 0) return <p className="rounded-lg border px-3 py-4 text-xs text-faint" data-table-usage="0">{T.none}</p>
  if (!wanted) {
    return (
      <button type="button" className="btn btn-sm" onClick={() => setWanted(true)} data-table-usage-show="">
        <History size={11} aria-hidden /> {T.show}
      </button>
    )
  }
  if (error && !data) {
    return (
      <p className="flex flex-wrap items-center gap-2 text-2xs text-[var(--err)]" data-table-usage="error">
        {T.error}：{humanizeError(error).title}
        <button type="button" className="rounded px-1 text-dim hover:bg-hover hover:text-fg" onClick={load}>{T.retry}</button>
      </p>
    )
  }
  if (!data) return <div data-table-usage="loading"><Skeleton rows={2} height={18} gap={6} /></div>
  if (!data.runs.length) return <p className="rounded-lg border px-3 py-4 text-xs text-faint" data-table-usage="0">{T.none}</p>
  return (
    <div data-table-usage={data.total}>
      <p className="mb-1 text-2xs text-faint">{T.count(data.runs.length, data.total)}</p>
      <ul className="divide-y divide-[var(--hairline)] rounded-lg border bg-panel">
        {data.runs.map((r) => {
          const name = r.workflow_name?.trim() || T.unnamed
          return (
            <li key={r.run_id} className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 px-3 py-1.5 text-xs" data-usage-run={r.run_id}>
              <Link to={`/runs/${encodeURIComponent(r.run_id)}`} className="min-w-0 truncate font-medium hover:underline" title={T.open(name)}>
                {name}
              </Link>
              {r.run_class && <span className="shrink-0 text-2xs text-faint">{RUN_CLASS_LABEL[r.run_class] ?? r.run_class}</span>}
              <StatusPill status={r.status} short plain />
              <span className="flex-1" />
              {r.started_at && (
                <span className="tnum shrink-0 text-2xs text-faint" title={formatDateTime(r.started_at)}>{formatTime(r.started_at)}</span>
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}
