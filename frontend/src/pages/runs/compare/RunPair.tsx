import { useId, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ArrowLeftRight, CalendarClock, RotateCw } from 'lucide-react'
import { Skeleton, StatusPill, useTicker } from '../../../components/ui'
import { formatDateTime, formatTime, shortId } from '../../../lib/format'
import { statusLabel } from '../../../lib/status'
import { RUN_COMPARE_TEXT as T, runClassLabel, runName } from '../../../lib/terms'
import type { RunWithSchedule } from '../../../types'
import { ClassChip as RunClassChip } from '../parts'
import type { Loaded } from './data'
import { isGone } from './model'

/**
 * 顶部的两次运行：左边基准、右边本次（窄屏上下排），中间「互换」。每张卡写名字、时间、类别和版本，点名字进各自的
 * 运行详情；没封存的标出来（对比会因此被拒），失败、取消的标出状态。基准卡底下是换基准的下拉：同一工作流最近的运行，
 * 没封存的不能选。
 */
export function RunPair({ target, base, targetRun, baseRun, candidates, unsaved, reason, onPick, onSwap }: {
  target: string
  base: string | null
  targetRun: Loaded<RunWithSchedule>
  baseRun: Loaded<RunWithSchedule>
  candidates: Loaded<RunWithSchedule[]>
  /** 本次不属于已保存的工作流（没保存过、或者工作流已删除）：没有同一工作流的其他运行可选 */
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
          <BasePicker target={target} base={base} baseRun={baseRun} targetRun={targetRun} candidates={candidates}
                      unsaved={unsaved} onPick={onPick} />
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

/**
 * 封存了、却没有成功结束的（失败、已取消）：能手动选作基准，默认基准却跳过它（拿它当基准，口径卡没执行的指标都成了
 * 「新增」「移除」）。下拉和卡上写明状态，人读懂「上一次成功的运行」之后自己挑得出成功的那一次
 */
const unsuccessful = (status: string | null | undefined) => status === 'failed' || status === 'cancelled'

function RunCard({ role, id, run, children }: {
  role: 'base' | 'target'; id: string | null; run: Loaded<RunWithSchedule>; children?: ReactNode
}) {
  const now = useTicker(60_000)
  const r = run.data
  const when = r ? r.started_at ?? r.created_at ?? null : null
  const gone = run.state === 'error' && isGone(run.error)
  return (
    <div className="min-w-0 rounded-lg border bg-panel px-3 py-2.5" data-compare-run={role}>
      <div className="flex min-w-0 flex-wrap items-center gap-1.5 text-2xs">
        <span className="font-medium text-faint">{role === 'base' ? T.base : T.target}</span>
        {r?.schedule_id && (
          <span className="chip" title={T.scheduled}><CalendarClock size={10} aria-hidden /> {T.scheduled}</span>
        )}
        {r && unsuccessful(r.status) && (
          <span className="inline-flex" data-compare-run-status={r.status}><StatusPill status={r.status} /></span>
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
        // 读不到运行记录：对比本身照样请求，能不能比由对比接口说。记录不存在的重试也没用，只写不存在；
        // 别的（连不上、服务端出错）给重试
        <div className="mt-1 flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-1 text-xs text-faint" data-compare-run-failed="">
          {gone
            ? <span className="mono" title={id}>{shortId(id)}</span>
            : <Link to={`/runs/${id}`} data-compare-run-link="" className="mono hover:underline">{shortId(id)}</Link>}
          <span>{gone ? T.runMissing : T.runFailed}</span>
          {!gone && <RetryButton onClick={run.reload} />}
        </div>
      ) : (
        <div className="mt-1.5" aria-busy="true"><Skeleton rows={2} height={12} gap={8} /></div>
      )}
      {children}
    </div>
  )
}

function RetryButton({ onClick }: { onClick: () => void }) {
  return (
    <button type="button" className="btn btn-xs" onClick={onClick}>
      <RotateCw size={10} aria-hidden /> {T.retry}
    </button>
  )
}

/**
 * 换基准。列的是运行列表接口按工作流取回的最近几次（新的在前），去掉本次；没封存的列出来但不能选，写明原因；失败、
 * 取消的能选，写明状态（默认基准不取它们）。
 *
 * 当前的基准不在这几次里（更早、属于别的工作流、读不到）时也单列一项：受控的下拉值对不上任何选项时浏览器会选中
 * 第一项，看着像另一次运行被选中，而原生下拉再选当前显示的那一项不触发 change，最自然的补救点了没反应。读不到的
 * 写短 id 加原因，下拉如实反映当前选的是哪一次，其余每一项都能触发 change。
 *
 * 列不出候选时说清楚、给重试，不停在「正在读取」：同一工作流的运行没取到；或者本次的记录没读到，不知道是哪个工作流
 * （候选的键是空的，不会自己去取）。本次的记录根本不存在（404）时重试也是一样，只说不存在、不给重试
 */
function BasePicker({ target, base, baseRun, targetRun, candidates, unsaved, onPick }: {
  target: string; base: string | null; baseRun: Loaded<RunWithSchedule>; targetRun: Loaded<RunWithSchedule>
  candidates: Loaded<RunWithSchedule[]>; unsaved: boolean; onPick: (id: string) => void
}) {
  const id = useId()
  const now = useTicker(60_000)
  if (unsaved) return <p className="mt-2 text-2xs text-faint" data-compare-pick-note="unsaved">{T.pickUnsaved}</p>
  if (candidates.state === 'idle' && targetRun.state === 'error' && isGone(targetRun.error)) {
    return <p className="mt-2 text-2xs text-faint" data-compare-pick-note="target-gone">{T.pickTargetGone}</p>
  }
  const failed = candidates.state === 'error' ? candidates.reload
    : candidates.state === 'idle' && targetRun.state === 'error' ? targetRun.reload : null
  if (failed) {
    return (
      <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-2xs text-faint" data-compare-pick-failed="">
        <span>{T.pickFailed}</span>
        <RetryButton onClick={failed} />
      </div>
    )
  }
  const loading = candidates.state !== 'ok'
  const list = (candidates.data ?? []).filter((r) => r.id !== target)
  if (!loading && !list.length) return <p className="mt-2 text-2xs text-faint" data-compare-pick-note="none">{T.pickNone}</p>
  const label = (r: RunWithSchedule) => T.pickOption(formatTime(r.started_at ?? r.created_at ?? null, new Date(now)),
    runClassLabel(r.run_class, r.version), shortId(r.id), !!r.manifest_hash, unsuccessful(r.status) ? statusLabel(r.status) : null)
  const current = base && !list.some((r) => r.id === base) ? base : null
  const currentLabel = (b: string) => (baseRun.data ? label(baseRun.data)
    : baseRun.state === 'error' ? T.pickGone(shortId(b), isGone(baseRun.error)) : shortId(b))
  return (
    <div className="mt-2.5 border-t pt-2">
      <label className="label" htmlFor={id}>{T.pickBase}</label>
      <select id={id} className="field" data-compare-base-picker="" value={base ?? ''} disabled={loading}
              aria-describedby={`${id}-hint`} onChange={(e) => { if (e.target.value) onPick(e.target.value) }}>
        {loading && <option value={base ?? ''}>{T.pickLoading}</option>}
        {!loading && !base && <option value="" disabled>{T.pickPlaceholder}</option>}
        {!loading && current && <option value={current}>{currentLabel(current)}</option>}
        {!loading && list.map((r) => (
          // 当前的基准即使没封存也要能显示出来（选中项）；别的没封存的一律不能选
          <option key={r.id} value={r.id} disabled={!r.manifest_hash && r.id !== base}>{label(r)}</option>
        ))}
      </select>
      <p id={`${id}-hint`} className="mt-1 text-2xs text-faint">{T.pickHint}</p>
    </div>
  )
}
