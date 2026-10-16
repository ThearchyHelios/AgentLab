import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../../api/client'
import { ErrorState, Modal, Notice, Skeleton, Spinner, toast } from '../../components/ui'
import { errorMessage } from '../../lib/errors'
import { formatDay, formatNumber, formatTime } from '../../lib/format'
import { leavePass } from '../../lib/leave'
import { EVAL_TEXT, distText, pct } from '../../lib/termsEval'
import { useOnReconnect } from '../../store/catalog'
import type { EvalQuestionCluster, EvalSignalPoint, EvalSignals, EvalSuite, LedgerStats } from '../../typesEval'
import { StatCard } from './parts'

const T = EVAL_TEXT.signals
const WINDOWS = [7, 30, 90]

/**
 * 「口径命中与重复度」页签（细案 01 第 13、18 项，总方案 X2）：E1 口径命中、E3 问题重复度，参考线、这一窗口落在哪一档、往前
 * 几个窗口的趋势，以及成员不少于 3 的问题类（可以加入题集、提取模板）。
 *
 * 数只有一份实现：判据、分档、趋势、问题类来自 GET /api/evals/signals（它按窗口调口径账本的 compute_stats 和全站唯一的问题
 * 聚类），详细的分布直接调口径账本的 GET /api/ledger/stats。评测运行不计入（engine/origin.counted_runs）。
 */
export function Signals() {
  const navigate = useNavigate()
  const [days, setDays] = useState(30)
  const [data, setData] = useState<EvalSignals | null>(null)
  const [stats, setStats] = useState<LedgerStats | null>(null)
  const [statsError, setStatsError] = useState<unknown>(null)
  const [error, setError] = useState<unknown>(null)
  const [adding, setAdding] = useState<string | null>(null)
  const [extracting, setExtracting] = useState<string | null>(null)

  const load = useCallback(async () => {
    setData(null)
    setStats(null)
    setError(null)
    setStatsError(null)
    try {
      const s = await api.evals.signals(days)
      setData(s)
      api.evals.ledgerStats(s.window.since).then(setStats, setStatsError)
    } catch (e) {
      setError(e)
    }
  }, [days])
  useEffect(() => { void load() }, [load])
  useOnReconnect(() => { void load() })

  const extract = async (c: EvalQuestionCluster) => {
    if (!c.run_id || extracting) return
    setExtracting(c.run_id)
    try {
      const res = await api.copilot.fromRun(c.run_id)
      toast.ok(T.extracted(res.name))
      navigate(`/studio/${encodeURIComponent(res.workflow_id)}`, leavePass())
    } catch (e) {
      toast.error(e)
    } finally {
      setExtracting(null)
    }
  }

  return (
    <div className="mx-auto max-w-5xl space-y-4 p-4" data-eval-signals={data ? 'ok' : 'loading'}>
      <div className="flex flex-wrap items-end gap-3">
        <div className="min-w-0 flex-1">
          <h2 className="text-sm font-semibold">{T.title}</h2>
          <p className="mt-0.5 text-xs leading-relaxed text-faint">{T.hint}</p>
        </div>
        <label className="flex items-center gap-2 text-xs">
          <span className="text-faint">{T.window}</span>
          <select className="field w-auto" value={days} onChange={(e) => setDays(Number(e.target.value))} data-eval-signals-days="">
            {WINDOWS.map((d) => <option key={d} value={d}>{T.days(d)}</option>)}
          </select>
        </label>
      </div>
      {error ? <ErrorState error={error} onRetry={() => void load()} /> : !data ? <Skeleton rows={5} height={48} gap={8} /> : (
        <>
          <div className="grid gap-2 sm:grid-cols-2">
            <StatCard title={T.e1} hint={T.e1Hint} attr="e1" value={pct(data.current.e1.rate)}
                      sub={data.current.e1.n ? `${data.current.e1.band ? T.band[data.current.e1.band] : ''} · ${T.n(data.current.e1.n)}` : T.noData} />
            <StatCard title={T.e3} hint={T.e3Hint(data.threshold)} attr="e3" value={pct(data.current.e3.share)}
                      sub={data.current.e3.n
                        ? `${data.current.e3.band ? T.band[data.current.e3.band] : ''} · ${T.n(data.current.e3.n)}${data.current.e3.lower_bound != null ? ` · ${T.lowerBound(pct(data.current.e3.lower_bound))}` : ''}`
                        : T.noData} />
          </div>
          <section className="space-y-1 rounded-lg border bg-panel p-3 text-xs" data-eval-signals-criteria="">
            <h3 className="text-2xs font-semibold text-dim">{T.criteria}</h3>
            <ul className="space-y-0.5">
              {[...data.criteria.e1, ...data.criteria.e3].map((c) => <li key={c.band}>· {c.text}</li>)}
            </ul>
            <p className="text-2xs text-faint">{data.criteria.note}</p>
          </section>
          <TrendChart points={data.trend} days={days} />
          <Detail stats={stats} error={statsError} />
          <section className="space-y-2" data-eval-clusters={data.clusters.length}>
            <div>
              <h3 className="text-xs font-semibold">{T.clusters}</h3>
              <p className="mt-0.5 text-2xs leading-relaxed text-faint">{T.clustersHint(data.threshold)}</p>
              <p className="text-2xs text-faint">{T.embedder(data.embedder)}</p>
            </div>
            {!data.clusters.length ? <p className="text-xs text-dim" data-eval-clusters-empty="">{T.clustersEmpty}</p> : (
              <ul className="space-y-2">
                {data.clusters.map((c, i) => (
                  <li key={i} className="rounded-lg border bg-panel p-3 text-xs" data-eval-cluster={c.count}>
                    <div className="flex flex-wrap items-start gap-2">
                      <div className="min-w-0 flex-1">
                        <div className="break-words font-medium">{c.representative}</div>
                        <div className="mt-0.5 text-2xs text-faint">{T.clusterCount(c.count)} · {formatTime(c.first_at)} – {formatTime(c.last_at)}</div>
                      </div>
                      <button type="button" className="btn btn-sm" onClick={() => setAdding(c.representative)} data-eval-cluster-add="">
                        {T.addToSuite}
                      </button>
                      <button type="button" className="btn btn-sm" disabled={!c.run_id || !!extracting} data-eval-cluster-extract=""
                              title={c.run_id ? T.extractHint : T.extractNone} onClick={() => void extract(c)}>
                        {extracting === c.run_id && <Spinner size={11} />} {T.extract}
                      </button>
                    </div>
                    <details className="mt-1.5">
                      <summary className="cursor-pointer text-2xs text-dim">{T.members}</summary>
                      <ul className="mt-1 space-y-0.5 text-2xs text-dim">
                        {c.members.map((m, j) => <li key={j}>{m.text}<span className="text-faint"> · {formatTime(m.at)}</span></li>)}
                      </ul>
                    </details>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </>
      )}
      {adding != null && <AddToSuite question={adding} onClose={() => setAdding(null)} />}
    </div>
  )
}

/** 往前几个窗口的 E1、E3（旧的在前）：一排竖条，高度就是那个窗口的比率（服务端算好的），悬停写窗口、比率、样本、档位 */
function TrendChart({ points, days }: { points: EvalSignalPoint[]; days: number }) {
  const rows: { key: 'e1' | 'e3'; label: string; value: (p: EvalSignalPoint) => number | null; n: (p: EvalSignalPoint) => number;
    band: (p: EvalSignalPoint) => string | null }[] = [
    { key: 'e1', label: T.e1, value: (p) => p.e1.rate, n: (p) => p.e1.n, band: (p) => (p.e1.band ? T.band[p.e1.band] : null) },
    { key: 'e3', label: T.e3, value: (p) => p.e3.share, n: (p) => p.e3.n, band: (p) => (p.e3.band ? T.band[p.e3.band] : null) },
  ]
  return (
    <section className="space-y-2 rounded-lg border bg-panel p-3 text-xs" data-eval-signals-trend={points.length}>
      <h3 className="text-2xs font-semibold text-dim">{T.trend}<span className="ml-2 font-normal text-faint">{T.trendHint(days)}</span></h3>
      {rows.map((r) => (
        <div key={r.key} className="flex items-end gap-3">
          <span className="w-28 shrink-0 text-2xs text-faint">{r.label}</span>
          <div className="flex items-end gap-2">
            {points.map((p, i) => {
              const v = r.value(p)
              const title = `${T.trendWindow(formatDay(p.since), formatDay(p.until))}：${pct(v)}，${T.n(r.n(p))}${r.band(p) ? `，${r.band(p)}` : ''}`
              return (
                <span key={i} className="flex w-10 flex-col items-center gap-0.5" title={title} aria-label={title} role="img"
                      data-eval-trend-point={r.key}>
                  <span className="flex h-12 w-5 items-end overflow-hidden rounded-sm bg-hover">
                    <span className="w-full bg-[var(--accent)]" style={{ height: `${Math.round((v ?? 0) * 100)}%`, opacity: v == null ? 0 : 1 }} />
                  </span>
                  <span className="tnum text-2xs text-faint">{pct(v, 0)}</span>
                </span>
              )
            })}
          </div>
        </div>
      ))}
    </section>
  )
}

/** 口径账本给的详细分布（GET /api/ledger/stats）：取不到不挡上面那几样 */
function Detail({ stats, error }: { stats: LedgerStats | null; error: unknown }) {
  if (error) return <Notice tone="warn">{T.ledgerFailed}：{errorMessage(error)}</Notice>
  if (!stats) return <Skeleton rows={2} height={40} gap={8} />
  const e1 = stats.e1
  const e3 = stats.e3
  return (
    <section className="grid gap-3 rounded-lg border bg-panel p-3 text-xs lg:grid-cols-2" data-eval-signals-detail="">
      <div className="space-y-1">
        <h3 className="text-2xs font-semibold text-dim">{T.e1} · {T.detail}</h3>
        <p>{distText(e1.status, T.status)}</p>
        <p className="text-dim">{T.diffAspects}：{distText(e1.diff_aspects, EVAL_TEXT.aspect)}</p>
        {Object.keys(e1.unproven_reasons).length > 0 && (
          <div className="text-dim">
            <div>{T.unproven}：</div>
            <ul className="ml-3 list-disc">
              {Object.entries(e1.unproven_reasons).slice(0, 6).map(([k, n]) => <li key={k}>{k}（{formatNumber(n)}）</li>)}
            </ul>
          </div>
        )}
      </div>
      <div className="space-y-1">
        <h3 className="text-2xs font-semibold text-dim">{T.e3} · {T.detail}</h3>
        <p>{T.shapes(e3.shapes, e3.repeated_shapes)}</p>
        <p className="text-dim">{T.lowerBound(pct(e3.repeated_share))}</p>
      </div>
    </section>
  )
}

/** 把一个反复问的问题加入题集：选题集，去那个题集打开加题（问法带过去，期望答案仍要按 SQL 或口径给） */
function AddToSuite({ question, onClose }: { question: string; onClose: () => void }) {
  const navigate = useNavigate()
  const [suites, setSuites] = useState<EvalSuite[] | null>(null)
  const [picked, setPicked] = useState('')
  useEffect(() => {
    api.evals.suites().then((r) => {
      setSuites(r.suites)
      setPicked(r.suites[0]?.id ?? '')
    }, () => setSuites([]))
  }, [])
  const go = () => {
    if (!picked) return
    onClose()
    navigate(`/evals/suites/${encodeURIComponent(picked)}?add=1&question=${encodeURIComponent(question)}`)
  }
  return (
    <Modal open onClose={onClose} title={T.addToSuiteTitle} width={520}
           footer={(
             <>
               <button type="button" className="btn" onClick={onClose}>{EVAL_TEXT.editor.cancel}</button>
               <button type="button" className="btn btn-primary" disabled={!picked} onClick={go} data-eval-cluster-go="">{T.go}</button>
             </>
           )}>
      <div className="space-y-3 text-xs" data-eval-add-to-suite="">
        <p className="break-words rounded-md border bg-panel p-2">{question}</p>
        {suites === null ? <Spinner size={12} /> : !suites.length ? <p className="text-dim">{T.noSuites}</p> : (
          <label className="block">
            <span className="label">{T.pickSuite}</span>
            <select className="field" value={picked} onChange={(e) => setPicked(e.target.value)} data-eval-cluster-suite="">
              {suites.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          </label>
        )}
      </div>
    </Modal>
  )
}
