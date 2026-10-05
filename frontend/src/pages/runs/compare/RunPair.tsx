import { useId, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ArrowLeftRight, CalendarClock } from 'lucide-react'
import { Skeleton, useTicker } from '../../../components/ui'
import { formatDateTime, formatTime, shortId } from '../../../lib/format'
import { RUN_COMPARE_TEXT as T, runClassLabel, runName } from '../../../lib/terms'
import type { RunWithSchedule } from '../../../types'
import { ClassChip as RunClassChip } from '../parts'
import type { Loaded } from './data'

/**
 * 顶部的两次运行：左边基准、右边本次（窄屏上下排），中间「互换」。每张卡写名字、时间、类别和版本，点名字进各自的
 * 运行详情；没封存的标出来（对比会因此被拒）。基准卡底下是换基准的下拉：同一工作流最近的运行，没封存的不能选。
 */
export function RunPair({ target, base, targetRun, baseRun, candidates, unsaved, reason, onPick, onSwap }: {
  target: string
  base: string | null
  targetRun: Loaded<RunWithSchedule>
  baseRun: Loaded<RunWithSchedule>
  candidates: Loaded<RunWithSchedule[]>
  /** 本次的工作流没保存：没有同一工作流的其他运行可选 */
  unsaved: boolean
  /** 默认基准是怎么选的（只在从「与上一次对比」、或只带本次打开时有） */
  reason: string | null
  onPick: (id: string) => void
  onSwap: () => void
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <div className="grid grid-cols-1 gap-2 md:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)]">
        <RunCard role="base" id={base} run={baseRun}>
          <BasePicker target={target} base={base} baseRun={baseRun.data} candidates={candidates} unsaved={unsaved}
                      onPick={onPick} />
        </RunCard>
        <div className="flex items-center justify-center">
          <button type="button" className="btn btn-sm" data-compare-swap="" disabled={!base} onClick={onSwap}
                  aria-label={T.swapHint} title={T.swapHint}>
            {/* 窄屏两张卡上下排，箭头跟着竖过来 */}
            <ArrowLeftRight size={12} className="rotate-90 md:rotate-0" aria-hidden /> {T.swap}
          </button>
        </div>
        <RunCard role="target" id={target} run={targetRun} />
      </div>
      {reason && T.reason[reason] && (
        <p className="text-2xs text-faint" data-compare-reason="">{T.reason[reason]}</p>
      )}
    </div>
  )
}

function RunCard({ role, id, run, children }: {
  role: 'base' | 'target'; id: string | null; run: Loaded<RunWithSchedule>; children?: ReactNode
}) {
  const now = useTicker(60_000)
  const r = run.data
  const when = r ? r.started_at ?? r.created_at ?? null : null
  return (
    <div className="min-w-0 rounded-lg border bg-panel px-3 py-2.5" data-compare-run={role}>
      <div className="flex min-w-0 flex-wrap items-center gap-1.5 text-2xs">
        <span className="font-medium text-faint">{role === 'base' ? T.base : T.target}</span>
        {r?.schedule_id && (
          <span className="chip" title={T.scheduled}><CalendarClock size={10} aria-hidden /> {T.scheduled}</span>
        )}
        {r && !r.manifest_hash && (
          <span className="chip" title={T.unsealedHint} data-compare-unsealed=""
                style={{ color: 'var(--warn)', borderColor: 'color-mix(in srgb, var(--warn) 40%, transparent)' }}>
            {T.unsealed}
          </span>
        )}
      </div>
      {!id ? (
        <div className="mt-1 text-sm text-faint">{T.noBase}</div>
      ) : r ? (
        <>
          <Link to={`/runs/${id}`} data-compare-run-link="" title={T.openRun(runName(r))}
                className="mt-1 block truncate rounded-sm text-sm font-semibold hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[color:var(--accent)]">
            {runName(r)}
          </Link>
          <div className="mt-1 flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-1 text-2xs text-faint">
            <time className="tnum" title={formatDateTime(when)}>{formatTime(when, new Date(now))}</time>
            <span aria-hidden>·</span>
            <RunClassChip runClass={r.run_class} version={r.version} always />
            <span aria-hidden>·</span>
            <span className="mono" title={id}>{shortId(id)}</span>
          </div>
        </>
      ) : run.state === 'error' ? (
        // 读不到运行记录（删了、连不上）：对比本身照样请求，能不能比由对比接口说
        <div className="mt-1 text-xs text-faint">
          <Link to={`/runs/${id}`} data-compare-run-link="" className="mono hover:underline">{shortId(id)}</Link>
          <span className="ml-1.5">{T.runFailed}</span>
        </div>
      ) : (
        <div className="mt-1.5" aria-busy="true"><Skeleton rows={2} height={12} gap={8} /></div>
      )}
      {children}
    </div>
  )
}

/**
 * 换基准。列的是运行列表接口按工作流取回的最近几次（新的在前），去掉本次；没封存的列出来但不能选，写明原因。
 * 当前的基准不在这几次里（更早、或者属于别的工作流）时也放进去，下拉不至于显示成别的运行
 */
function BasePicker({ target, base, baseRun, candidates, unsaved, onPick }: {
  target: string; base: string | null; baseRun?: RunWithSchedule; candidates: Loaded<RunWithSchedule[]>; unsaved: boolean
  onPick: (id: string) => void
}) {
  const id = useId()
  const now = useTicker(60_000)
  if (unsaved) return <p className="mt-2 text-2xs text-faint">{T.pickUnsaved}</p>
  if (candidates.state === 'error') return <p className="mt-2 text-2xs text-faint">{T.pickFailed}</p>
  const loading = candidates.state !== 'ok'
  const list = (candidates.data ?? []).filter((r) => r.id !== target)
  if (base && baseRun && !list.some((r) => r.id === base)) list.unshift(baseRun)
  if (!loading && !list.length) return <p className="mt-2 text-2xs text-faint">{T.pickNone}</p>
  const label = (r: RunWithSchedule) => T.pickOption(formatTime(r.started_at ?? r.created_at ?? null, new Date(now)),
    runClassLabel(r.run_class, r.version), shortId(r.id), !!r.manifest_hash)
  return (
    <div className="mt-2.5 border-t pt-2">
      <label className="label" htmlFor={id}>{T.pickBase}</label>
      <select id={id} className="field" data-compare-base-picker="" value={base ?? ''} disabled={loading}
              aria-describedby={`${id}-hint`} onChange={(e) => { if (e.target.value) onPick(e.target.value) }}>
        {loading && <option value={base ?? ''}>{T.pickLoading}</option>}
        {!loading && !base && <option value="" disabled>{T.pickPlaceholder}</option>}
        {!loading && list.map((r) => (
          // 当前的基准即使没封存也要能显示出来（选中项）；别的没封存的一律不能选
          <option key={r.id} value={r.id} disabled={!r.manifest_hash && r.id !== base}>{label(r)}</option>
        ))}
      </select>
      <p id={`${id}-hint`} className="mt-1 text-2xs text-faint">{T.pickHint}</p>
    </div>
  )
}
