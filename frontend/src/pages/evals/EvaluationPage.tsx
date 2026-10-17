import { Fragment, useCallback, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { ArrowLeft, Gauge, Pin, PinOff, RotateCcw, Scale, Square } from 'lucide-react'
import clsx from 'clsx'
import { ApiError, api } from '../../api/client'
import { EmptyState, ErrorState, PageHeader, Skeleton, Spinner, Tabs, toast } from '../../components/ui'
import { errorMessage } from '../../lib/errors'
import { formatCost, formatDateTime, formatDuration, formatNumber, formatTokens, shortId } from '../../lib/format'
import { EVAL_TEXT, distText, pct, rateText } from '../../lib/termsEval'
import { useOnReconnect } from '../../store/catalog'
import type { EvalCompare, EvalPair, EvalResult, EvalRun, EvalRunDetail, EvalScore, EvalSuiteDetail, EvalSummary } from '../../typesEval'
import { EvalStatusPill, RateCard, StatCard, VerdictChip, cellText, usageCost } from './parts'

const T = EVAL_TEXT
type TabKey = 'overview' | 'results' | 'compare' | 'pair'
/** 评测还在跑时每 2 秒刷新一次进度 */
const LIVE_MS = 2000

/**
 * 一次评测（/evals/runs/:id）：页头是题集、状态、变体和能做的几件事（停止、只重试没做完的、按新规则重评、设为基线——能不能做
 * 由服务端的 actions 给）；在跑时给进度和每题正在做什么；下面四个页签：总览（卡片和各项分布）、逐题、和上一次比、A/B。
 * ?case=题目 id 打开逐题并定位到那道题（记录页里评测运行的来源链到这里）。
 */
export function EvaluationPage({ id }: { id: string }) {
  const [params, setParams] = useSearchParams()
  const focusCase = params.get('case')
  const tab: TabKey = (['results', 'compare', 'pair'].includes(params.get('tab') ?? '') ? params.get('tab') : focusCase ? 'results' : 'overview') as TabKey
  const [run, setRun] = useState<EvalRunDetail | null>(null)
  const [suite, setSuite] = useState<EvalSuiteDetail | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [resultsKey, setResultsKey] = useState(0)
  const seq = useRef(0)
  const suiteOf = useRef<string | null>(null)

  const load = useCallback(async () => {
    const n = ++seq.current
    try {
      const r = await api.evals.evaluation(id)
      if (n !== seq.current) return
      setRun(r)
      setError(null)
      // 页头要题集的名字：只取一次
      if (suiteOf.current !== r.suite_id) {
        suiteOf.current = r.suite_id
        api.evals.suite(r.suite_id).then(setSuite, () => {})
      }
    } catch (e) {
      if (n !== seq.current) return
      setError(e)
    }
  }, [id])
  useEffect(() => { void load() }, [load])
  useOnReconnect(() => { void load() })
  const live = !!run && (run.status === 'queued' || run.status === 'running')
  useEffect(() => {
    if (!live) return
    const t = setInterval(() => { if (!document.hidden) void load() }, LIVE_MS)
    return () => clearInterval(t)
  }, [live, load])
  // 评测从进行中变成结束：逐题结果再取一次
  const wasLive = useRef(live)
  useEffect(() => {
    if (wasLive.current && !live) setResultsKey((k) => k + 1)
    wasLive.current = live
  }, [live])

  const pickTab = (k: string) => setParams((p) => {
    const next = new URLSearchParams(p)
    if (k === 'overview') next.delete('tab')
    else next.set('tab', k)
    return next
  }, { replace: true })

  const act = async (what: string, fn: () => Promise<string | void>) => {
    if (busy) return
    setBusy(what)
    try {
      const msg = await fn()
      if (msg) toast.ok(msg)
      await load()
      setResultsKey((k) => k + 1)
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(null)
    }
  }
  const a = run?.actions

  const actions = (
    <div className="flex shrink-0 flex-wrap items-center gap-1">
      {a?.stop && (
        <button type="button" className="btn btn-sm btn-danger" disabled={!!busy} data-eval-stop=""
                onClick={() => void act('stop', async () => { await api.evals.stop(id); return T.run.stopped })}>
          {busy === 'stop' ? <Spinner size={11} /> : <Square size={10} aria-hidden fill="currentColor" />} {T.run.stop}
        </button>
      )}
      {a?.retry && (
        <button type="button" className="btn btn-sm" disabled={!!busy} title={T.run.retryHint(a.open)} data-eval-retry=""
                onClick={() => void act('retry', async () => { await api.evals.retry(id); return T.run.retried })}>
          {busy === 'retry' ? <Spinner size={11} /> : <RotateCcw size={11} aria-hidden />} {T.run.retry}
        </button>
      )}
      {a?.rescore && (
        <button type="button" className="btn btn-sm" disabled={!!busy} title={T.run.rescoreHint} data-eval-rescore=""
                onClick={() => void act('rescore', async () => T.run.rescored((await api.evals.rescore(id)).changed))}>
          {busy === 'rescore' ? <Spinner size={11} /> : <Scale size={11} aria-hidden />} {T.run.rescore}
        </button>
      )}
      {(a?.pin || a?.unpin) && (
        <button type="button" className="btn btn-sm" disabled={!!busy} title={T.run.pinHint} data-eval-pin={a.unpin ? 'on' : 'off'}
                onClick={() => void act('pin', async () => { await api.evals.pin(id, !a.unpin); return a.unpin ? T.run.unpinnedOk : T.run.pinnedOk })}>
          {a.unpin ? <PinOff size={11} aria-hidden /> : <Pin size={11} aria-hidden />} {a.unpin ? T.run.unpin : T.run.pin}
        </button>
      )}
      {run && (
        <Link to={`/evals/suites/${encodeURIComponent(run.suite_id)}?tab=runs`} className="btn btn-sm btn-ghost" aria-label={T.backToSuite}
              title={T.backToSuite}>
          <ArrowLeft size={12} aria-hidden /> <span className="hidden md:inline">{T.backToSuite}</span>
        </Link>
      )}
    </div>
  )

  if (!run) {
    const missing = error instanceof ApiError && error.status === 404
    return (
      <div className="flex h-full flex-col" data-evals-page="run">
        <PageHeader icon={<Gauge size={13} />} title={T.title} subtitle={T.hint} actions={actions} />
        {missing ? <EmptyState icon={<Gauge size={22} />} title={errorMessage(error)} offline={false} />
          : error ? <ErrorState error={error} onRetry={() => void load()} />
            : <div className="mx-auto max-w-5xl p-4"><Skeleton rows={6} height={40} gap={10} /></div>}
      </div>
    )
  }

  const title = `${T.run.title(shortId(run.id))}${run.arm ? ` · ${T.run.arm(run.arm)}` : ''}`
  const tabs: { key: TabKey; label: string }[] = [
    { key: 'overview', label: T.run.tabs.overview }, { key: 'results', label: T.run.tabs.results },
    { key: 'compare', label: T.run.tabs.compare }, ...(run.pair_id ? [{ key: 'pair' as const, label: T.run.tabs.pair }] : []),
  ]
  return (
    <div className="flex h-full flex-col" data-evals-page="run" data-eval-run={run.id} data-eval-run-status={run.status}>
      <PageHeader icon={<Gauge size={13} />} title={title} subtitle={suite ? T.run.of(suite.name) : undefined} actions={actions} />
      <div className="shrink-0 space-y-1.5 border-b bg-panel px-4 py-2 text-2xs" data-eval-run-head="">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <EvalStatusPill status={run.status} />
          {run.pinned && <span className="chip">{T.run.pinned}</span>}
          <span className="text-dim">{T.trigger[run.trigger]}</span>
          <span className="text-dim" title={T.run.variant}>{run.variant_text}</span>
          {run.as_of && <span className="text-faint">{T.run.asOf(formatDateTime(run.as_of))}</span>}
          <span className="text-faint">{T.run.startedBy(run.created_by)} · {formatDateTime(run.created_at)}</span>
        </div>
        {live && <Progress run={run} />}
        {run.stop_reason && <p className="text-[var(--warn)]" data-eval-stop-reason="">{T.run.stopReason}：{run.stop_reason}</p>}
        {run.error && <p className="text-[var(--err)]" data-eval-error="">{T.run.error}：{run.error}</p>}
        {run.cleaned_at && <p className="text-faint" data-eval-cleaned="">{T.run.cleaned}</p>}
      </div>
      <Tabs tabs={tabs} active={tab} onChange={pickTab} label={title} idPrefix="eval-run" />
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-6xl p-4" role="tabpanel" id={`eval-run-panel-${tab}`} aria-labelledby={`eval-run-tab-${tab}`}>
          {tab === 'overview' && <Overview run={run} />}
          {tab === 'results' && <Results key={resultsKey} id={run.id} live={live} focusCase={focusCase} />}
          {tab === 'compare' && <Compare run={run} />}
          {tab === 'pair' && run.pair_id && <PairTable pairId={run.pair_id} current={run.id} />}
        </div>
      </div>
    </div>
  )
}

/** 进度：已评几项，正在评的几题各在做什么（服务端随时落库的「正在做什么」） */
function Progress({ run }: { run: EvalRunDetail }) {
  const done = run.counts.done ?? 0
  const total = run.counts.total ?? 0
  const text = T.run.progress(done, total)
  return (
    <div className="space-y-1" data-eval-progress={`${done}/${total}`}>
      <div className="flex items-center gap-2">
        <Spinner size={11} />
        <span>{text}</span>
      </div>
      <div className="h-1 max-w-md overflow-hidden rounded-full bg-hover" role="progressbar" aria-label={text}
           aria-valuemin={0} aria-valuemax={total} aria-valuenow={done}>
        <div className="h-full rounded-full bg-[var(--accent)] transition-[width] duration-300"
             style={{ width: `${total ? (done / total) * 100 : 0}%` }} />
      </div>
      {!!run.current.length && (
        <ul className="space-y-0.5 text-dim" aria-label={T.run.current} data-eval-current="">
          {run.current.map((c, i) => (
            <li key={i} data-eval-current-item={`${c.case_seq}:${c.repeat}:${c.arm ?? ''}`}>
              {T.run.currentItem(c.case_seq, c.repeat, c.arm)}：{c.doing ?? '…'}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 总览
// ---------------------------------------------------------------------------

function Overview({ run }: { run: EvalRun }) {
  const s: EvalSummary = run.summary ?? {}
  if (!s.accuracy) return <p className="py-6 text-center text-xs text-faint" data-eval-no-summary="">{T.run.noSummary}</p>
  const O = T.overview
  const cost = usageCost({ cost_usd: s.cost_usd ?? null, unpriced_calls: s.unpriced_calls ?? 0 })
  const base = s.baseline
  return (
    <div className="space-y-4" data-eval-overview="">
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        <RateCard title={O.accuracy} hint={O.accuracyHint} rate={s.accuracy} attr="accuracy" />
        <RateCard title={O.traced} hint={O.tracedHint} rate={s.traced_accuracy} attr="traced" />
        <StatCard title={O.cost} hint={O.costHint} value={cost} attr="cost"
                  sub={s.per_case_cost_usd != null ? O.perCase(formatCost(s.per_case_cost_usd)) : undefined} />
        <StatCard title={O.e1} hint={O.e1Hint} value={s.e1?.n ? pct(s.e1.rate) : '—'} sub={rateText(s.e1)} attr="e1" />
      </div>
      <Section title={O.verdicts} attr="verdicts">
        <p>{distText(s.verdicts as Record<string, number>, T.verdict)}</p>
        {!!s.not_run && <p className="text-dim">{O.notRun(s.not_run)}</p>}
        <p className="text-dim">{O.outcomes}：{distText(s.outcomes as Record<string, number>, T.outcome)}</p>
        {s.text && Object.keys(s.text).length > 0 && <p className="text-dim">{O.text}：{distText(s.text as Record<string, number>, T.textOutcome)}</p>}
      </Section>
      {base && (
        <Section title={O.baseline} attr="baseline">
          {base.counts
            ? <p>{O.baselineText(base.counts.regressed ?? 0, base.counts.improved ?? 0)} · {distText(base.causes ?? {}, T.compare.cause)}</p>
            : <p className="text-dim">{T.compare.cleaned}</p>}
          {!!base.fingerprint_diff.length && <p className="text-dim">{T.compare.fingerprint}：{base.fingerprint_diff.map((d) => d.label).join('、')}</p>}
        </Section>
      )}
      <div className="grid gap-3 lg:grid-cols-2">
        <Section title={O.paths} attr="paths"><p>{distText(s.paths as Record<string, number>, T.path)}</p></Section>
        <Section title={O.tiers} attr="tiers"><p>{distText(s.tiers as Record<string, number>, O.tier)}</p></Section>
        <Section title={O.review} attr="review">
          <p>{distText(s.review?.verdicts ?? {}, O.reviewVerdict)}</p>
          {!!s.review && <p className="text-dim">{O.reviewRetries(s.review.retries)}</p>}
          {!!s.review?.slow && <p className="text-dim">{O.reviewSlow(s.review.slow)}</p>}
        </Section>
        <Section title={O.judge} attr="judge">
          {s.judge?.on ? <p>{distText(s.judge.claims ?? {}, O.claims)}</p> : <p className="text-dim">{O.judgeOff}</p>}
        </Section>
        <Section title={O.citations} attr="citations">
          {s.citations && (
            <>
              <p>{O.citationsText(s.citations.numbers_cited, s.citations.numbers)}{s.citations.rate != null ? `（${pct(s.citations.rate)}）` : ''}</p>
              <p className="text-dim">{O.uncited(s.citations.uncited_numbers, s.citations.unresolved)}</p>
            </>
          )}
        </Section>
        <Section title={O.looks} attr="looks">
          {s.looks && (
            <>
              <p>{O.looksText(s.looks.model, s.looks.system)} {O.looksPer(s.looks.per_case)}</p>
              {s.looks.limit_hit != null && <p className="text-dim">{O.looksLimit(pct(s.looks.limit_hit))}</p>}
            </>
          )}
          {s.calls && <p className="text-dim">{O.calls(s.calls.assistant, s.calls.per_case)}</p>}
        </Section>
        <Section title={O.timings} attr="timings">
          <ul className="space-y-0.5">
            {Object.entries(s.timings ?? {}).filter(([, t]) => t && t.n).map(([k, t]) => (
              <li key={k}>{O.timing[k] ?? k}：{formatDuration(t!.p50)} / {formatDuration(t!.p90)}<span className="text-faint">（{formatNumber(t!.n)}）</span></li>
            ))}
          </ul>
        </Section>
        <Section title={O.usage} attr="usage">
          <ul className="space-y-0.5">
            {(['assistant', 'writer', 'judge'] as const).map((role) => {
              const u = s.usage?.[role]
              if (!u) return null
              return (
                <li key={role} data-eval-usage-role={role}>
                  {O.role[role]}：{usageCost(u)}
                  {u.input_tokens != null && <span className="text-faint"> · {O.tokens(formatTokens(u.input_tokens, { compact: true }), formatTokens(u.output_tokens ?? 0, { compact: true }))}</span>}
                  {u.calls != null && <span className="text-faint"> · {O.callsN(u.calls)}</span>}
                </li>
              )
            })}
          </ul>
        </Section>
      </div>
      {s.pass_rates && Object.keys(s.pass_rates).length > 0 && (
        <Section title={O.passRates} attr="pass-rates">
          <ul className="space-y-0.5">
            {Object.entries(s.pass_rates).map(([cid, r]) => <li key={cid} className="tnum">{shortId(cid)}：{rateText(r, '次')}</li>)}
          </ul>
        </Section>
      )}
    </div>
  )
}

function Section({ title, children, attr }: { title: string; children: ReactNode; attr?: string }) {
  return (
    <section className="space-y-1 rounded-lg border bg-panel p-3 text-xs" aria-label={title} data-eval-section={attr}>
      <h3 className="text-2xs font-semibold text-dim">{title}</h3>
      {children}
    </section>
  )
}

// ---------------------------------------------------------------------------
// 逐题
// ---------------------------------------------------------------------------

function scoreText(s: EvalScore): string {
  if (s.kind === 'text') return T.textOutcome[s.outcome as keyof typeof T.textOutcome] ?? String(s.outcome)
  return T.outcome[s.outcome as keyof typeof T.outcome] ?? String(s.outcome)
}

function Results({ id, live, focusCase }: { id: string; live: boolean; focusCase: string | null }) {
  const [data, setData] = useState<{ cleaned: boolean; results: EvalResult[] } | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [open, setOpen] = useState<string | null>(null)
  const load = useCallback(() => api.evals.results(id).then((r) => { setData(r); setError(null) }, setError), [id])
  useEffect(() => { void load() }, [load])
  useEffect(() => {
    if (!live) return
    const t = setInterval(() => { if (!document.hidden) void load() }, LIVE_MS * 2)
    return () => clearInterval(t)
  }, [live, load])
  useEffect(() => {
    if (!focusCase || !data) return
    const hit = data.results.find((r) => r.case_id === focusCase)
    if (hit) {
      setOpen(hit.id)
      requestAnimationFrame(() => document.querySelector(`[data-eval-result="${hit.id}"]`)?.scrollIntoView({ block: 'center' }))
    }
  }, [focusCase, !!data])
  if (error) return <ErrorState error={error} onRetry={() => void load()} />
  if (!data) return <Skeleton rows={6} height={28} gap={6} />
  if (data.cleaned) return <p className="py-6 text-center text-xs text-faint" data-eval-results-cleaned="">{T.results.cleaned}</p>
  if (!data.results.length) return <p className="py-6 text-center text-xs text-faint">{T.results.empty}</p>
  const R = T.results
  return (
    <div className="overflow-x-auto rounded-lg border" data-eval-results={data.results.length}>
      <table className="w-full min-w-[860px] text-xs" aria-label={R.label}>
        <thead className="bg-panel text-left text-2xs text-faint">
          <tr>
            <th className="px-2 py-1.5 font-medium">{R.seq}</th>
            <th className="px-2 py-1.5 font-medium">{R.question}</th>
            <th className="px-2 py-1.5 font-medium">{R.verdict}</th>
            <th className="px-2 py-1.5 font-medium">{R.expected} → {R.got}</th>
            <th className="px-2 py-1.5 font-medium">{R.sql}</th>
            <th className="px-2 py-1.5 font-medium">{R.looks}</th>
            <th className="px-2 py-1.5 font-medium">{R.time}</th>
            <th className="px-2 py-1.5 font-medium">{R.cost}</th>
          </tr>
        </thead>
        <tbody>
          {data.results.map((r) => {
            const expanded = open === r.id
            const counted = r.scores.filter((s) => s.kind !== 'text')
            const cost = usageCost(r.cost)
            return (
              <Fragment key={r.id}>
                <tr className={clsx('border-t align-top', focusCase && r.case_id === focusCase && 'bg-accent-soft')} data-eval-result={r.id}
                    data-eval-result-case={r.case_id ?? ''} data-eval-result-verdict={r.verdict ?? 'none'}>
                  <td className="whitespace-nowrap px-2 py-1.5 tnum">
                    <button type="button" className="text-left hover:underline" onClick={() => setOpen(expanded ? null : r.id)}
                            aria-expanded={expanded} data-eval-result-toggle="">
                      {T.caseRow.seq(r.case_seq ?? 0)}
                    </button>
                    {r.repeat > 0 && <div className="text-2xs text-faint">{R.repeat(r.repeat)}</div>}
                    {r.arm && <div className="text-2xs text-faint">{T.run.arm(r.arm)}</div>}
                  </td>
                  <td className="max-w-xs px-2 py-1.5">
                    <div className="line-clamp-2 break-words" data-eval-result-question="">{r.question ?? '—'}</div>
                    {r.question_template && r.question_template !== r.question && (
                      <div className="line-clamp-2 break-words text-2xs text-faint" data-eval-result-template="">{R.questionTemplate(r.question_template)}</div>
                    )}
                    {r.path && <div className="text-2xs text-faint">{R.path}：{T.path[r.path]}</div>}
                  </td>
                  <td className="px-2 py-1.5">
                    <VerdictChip verdict={r.verdict} fallback={r.current ?? T.itemStatus[r.status] ?? r.status} />
                    {r.traced && <div className="mt-0.5 text-2xs text-faint">{T.outcome.traced}</div>}
                  </td>
                  <td className="px-2 py-1.5">
                    {counted.length ? counted.map((s) => (
                      <div key={s.id} className="tnum" data-eval-score={s.outcome}>
                        {s.expected !== undefined && <span>{cellText(s.expected)}</span>}
                        {s.got !== undefined && <span className="text-dim"> → {cellText(s.got)}</span>}
                        <span className="ml-1 text-2xs text-faint">{scoreText(s)}</span>
                        {s.weak && <span className="ml-1 chip text-[var(--warn)]" title={R.weakHint}>{R.weak}</span>}
                      </div>
                    )) : <span className="text-faint">—</span>}
                  </td>
                  <td className="px-2 py-1.5">
                    {r.sql_match ? (
                      <span data-eval-sql-match={r.sql_match.status}>
                        {T.sqlMatch[r.sql_match.status] ?? r.sql_match.status}
                        {!!r.sql_match.aspects?.length && <span className="block text-2xs text-faint">{r.sql_match.aspects.map((x) => T.aspect[x] ?? x).join('、')}</span>}
                      </span>
                    ) : <span className="text-faint">—</span>}
                  </td>
                  <td className="whitespace-nowrap px-2 py-1.5 tnum" title={r.looks ? T.overview.looksText(r.looks.model ?? 0, r.looks.system ?? 0) : undefined}>
                    {r.looks ? R.looksCell(r.looks.model ?? 0, r.looks.system ?? 0) : '—'}
                  </td>
                  <td className="whitespace-nowrap px-2 py-1.5 tnum">{formatDuration(r.timings.total_ms ?? null)}</td>
                  <td className="whitespace-nowrap px-2 py-1.5 tnum">{r.status === 'ok' ? cost : '—'}</td>
                </tr>
                {expanded && (
                  <tr className="border-t bg-panel" data-eval-result-detail={r.id}>
                    <td colSpan={8} className="space-y-1.5 px-3 py-2">
                      {r.error && <p className="text-dim"><span className="text-faint">{R.reason}：</span>{r.error}</p>}
                      {r.scores.map((s) => (
                        <p key={s.id} className="text-dim">
                          <span className="text-faint">{T.expectation.kind[s.kind]}{s.label ? `「${s.label}」` : ''}：</span>
                          {scoreText(s)}
                          {s.key && <span className="text-faint">（{Object.entries(s.key).map(([k, v]) => `${k}=${cellText(v)}`).join('、')}）</span>}
                          {s.reason && <span> · {s.reason}</span>}
                        </p>
                      ))}
                      {r.sql_match?.reason && <p className="text-faint">{r.sql_match.reason}</p>}
                      <ResultDetail evaluationId={id} resultId={r.id} />
                      <div className="flex flex-wrap gap-2">
                        {r.run_id && <Link className="btn btn-sm" to={`/runs/${encodeURIComponent(r.run_id)}`} data-eval-result-run="">{T.run.openRun}</Link>}
                        {r.gold_run_ids.slice(0, 2).map((g) => (
                          <Link key={g} className="btn btn-sm btn-ghost" to={`/runs/${encodeURIComponent(g)}`}>{T.run.goldRuns} {shortId(g)}</Link>
                        ))}
                      </div>
                    </td>
                  </tr>
                )}
              </Fragment>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/** 一题的明细（GET …/results/{id} 的明细工件）：这一轮作答的经过（重来了几次、原因）和读出来的答案原文。明细清理了的不显示 */
function ResultDetail({ evaluationId, resultId }: { evaluationId: string; resultId: string }) {
  const [details, setDetails] = useState<Record<string, any> | null | undefined>(undefined)
  useEffect(() => {
    api.evals.result(evaluationId, resultId).then((r) => setDetails(r.details ?? null), () => setDetails(null))
  }, [evaluationId, resultId])
  if (details === undefined) return <Spinner size={11} />
  if (!details) return null
  const turn = details.turn ?? {}
  const answer = String(details.answer?.text ?? turn.reply ?? '').trim()
  const R = T.results
  return (
    <div className="space-y-1" data-eval-result-details="">
      {(turn.attempts ?? 0) > 0 && <p className="text-faint">{R.attempts(turn.attempts)}</p>}
      {turn.reason && <p className="text-dim"><span className="text-faint">{R.turnReason}：</span>{turn.reason}</p>}
      {answer && (
        <details>
          <summary className="cursor-pointer text-2xs text-dim">{R.answer}</summary>
          <p className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-words text-dim">{answer}</p>
        </details>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 和上一次比
// ---------------------------------------------------------------------------

function Compare({ run }: { run: EvalRun }) {
  const [base, setBase] = useState('')
  const [data, setData] = useState<EvalCompare | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [runs, setRuns] = useState<EvalRun[]>([])
  useEffect(() => {
    api.evals.evaluations(run.suite_id).then((r) => setRuns(r.evaluations.filter((x) => x.id !== run.id
      && (x.status === 'succeeded' || x.status === 'partial'))), () => setRuns([]))
  }, [run.suite_id, run.id])
  useEffect(() => {
    setData(null)
    api.evals.compare(run.id, base || null).then((d) => { setData(d); setError(null) }, setError)
  }, [run.id, base])
  const C = T.compare
  return (
    <div className="space-y-3 text-xs" data-eval-compare="">
      <label className="flex flex-wrap items-center gap-2">
        <span className="text-faint">{C.base}</span>
        <select className="field w-auto max-w-full" value={base} onChange={(e) => setBase(e.target.value)} data-eval-compare-base="">
          <option value="">{C.baseAuto}</option>
          {runs.map((r) => <option key={r.id} value={r.id}>{C.baseText(shortId(r.id), formatDateTime(r.created_at))}{r.arm ? ` · ${r.arm}` : ''}</option>)}
        </select>
      </label>
      {error ? <ErrorState error={error} compact /> : !data ? <Skeleton rows={4} height={20} gap={6} /> : !data.base ? (
        <p className="text-dim" data-eval-compare-none="">{C.none}</p>
      ) : (
        <>
          <p className="text-dim">
            {C.base}：<Link className="underline" to={`/evals/runs/${encodeURIComponent(data.base.id)}`}>{shortId(data.base.id)}</Link>
            <span className="text-faint"> · {formatDateTime(data.base.created_at)} · {data.base.variant_text}</span>
          </p>
          <section className="space-y-1 rounded-lg border bg-panel p-3" data-eval-fingerprint={data.fingerprint_diff.length}>
            <h3 className="text-2xs font-semibold text-dim">{C.fingerprint}</h3>
            {!data.fingerprint_diff.length ? <p className="text-dim">{C.fingerprintNone}</p> : (
              <ul className="space-y-0.5">
                {data.fingerprint_diff.map((d) => (
                  <li key={d.key} data-eval-fingerprint-key={d.key}>
                    {d.label}：{'before' in d ? `${cellText(d.before)} → ${cellText(d.after)}` : C.changed}
                  </li>
                ))}
              </ul>
            )}
          </section>
          {data.cleaned || !data.transitions ? <p className="text-dim">{C.cleaned}</p> : (
            <>
              <section className="space-y-1 rounded-lg border bg-panel p-3" data-eval-transitions="">
                <h3 className="text-2xs font-semibold text-dim">{C.transitions}</h3>
                <p>{distText(data.transitions.counts, C.transition)}</p>
                <p className="text-dim">{C.causes}：{distText(data.transitions.causes, C.cause)}</p>
              </section>
              <TransitionItems items={data.transitions.items} />
            </>
          )}
        </>
      )}
    </div>
  )
}

function TransitionItems({ items }: { items: NonNullable<EvalCompare['transitions']>['items'] }) {
  const C = T.compare
  const moved = items.filter((i) => i.transition === 'regressed' || i.transition === 'improved' || i.transition === 'incomparable')
  if (!moved.length) return <p className="text-dim">{C.noItems}</p>
  return (
    <ul className="space-y-1" aria-label={C.items} data-eval-transition-items={moved.length}>
      {moved.map((i) => (
        <li key={i.case_id} className="flex flex-wrap items-center gap-2 rounded-md border px-2 py-1" data-eval-transition={i.transition}>
          <span className="tnum">{C.case(i.case_seq)}</span>
          <span className={clsx('chip', i.transition === 'regressed' ? 'text-[var(--err)]' : i.transition === 'improved' ? 'text-[var(--st-done)]' : 'text-faint')}>
            {C.transition[i.transition]}
          </span>
          {i.cause && <span className="text-dim" title={C.causeHint[i.cause]}>{C.cause[i.cause]}</span>}
          {i.reason && <span className="text-faint">{i.reason}</span>}
        </li>
      ))}
    </ul>
  )
}

// ---------------------------------------------------------------------------
// A/B
// ---------------------------------------------------------------------------

function PairTable({ pairId, current }: { pairId: string; current: string }) {
  const [data, setData] = useState<EvalPair | null>(null)
  const [error, setError] = useState<unknown>(null)
  useEffect(() => { api.evals.pair(pairId).then(setData, setError) }, [pairId])
  const P = T.pair
  if (error) return <ErrorState error={error} compact />
  if (!data) return <Skeleton rows={4} height={20} gap={6} />
  return (
    <div className="space-y-3 text-xs" data-eval-pair={pairId}>
      <div className="flex flex-wrap gap-2">
        {data.evaluations.map((e) => (
          <Link key={e.id} to={`/evals/runs/${encodeURIComponent(e.id)}?tab=pair`} data-eval-pair-arm={e.arm ?? ''}
                className={clsx('rounded-lg border px-3 py-2 hover:border-[var(--border-strong)]', e.id === current && 'border-[var(--accent)]')}>
            <div className="font-medium">{T.run.arm(e.arm ?? '?')}</div>
            <div className="text-2xs text-dim">{e.variant_text}</div>
            <div className="mt-1 flex items-center gap-1.5"><EvalStatusPill status={e.status} plain /></div>
          </Link>
        ))}
      </div>
      {data.cleaned || !data.table ? <p className="text-dim">{P.cleaned}</p> : (
        <>
          <section className="space-y-1 rounded-lg border bg-panel p-3" data-eval-pair-arms="">
            <h3 className="text-2xs font-semibold text-dim">{P.arms}</h3>
            <ul className="space-y-0.5">
              {data.table.arms.map((a) => <li key={a.arm}>{T.run.arm(a.arm)}：{rateText(a.accuracy)}</li>)}
            </ul>
          </section>
          {data.table.pairs.map((p) => (
            <section key={`${p.a}:${p.b}`} className="space-y-2 rounded-lg border bg-panel p-3" data-eval-pair-row={`${p.a}:${p.b}`}>
              <h3 className="text-2xs font-semibold text-dim">{P.title}：{p.a} vs {p.b}</h3>
              <table className="text-xs">
                <tbody>
                  <tr>
                    <td className="pr-4 text-faint">{P.both}</td><td className="tnum pr-6" data-eval-pair-both="">{p.both}</td>
                    <td className="pr-4 text-faint">{P.onlyA(p.a)}</td><td className="tnum" data-eval-pair-only-a="">{p.only_a}</td>
                  </tr>
                  <tr>
                    <td className="pr-4 text-faint">{P.onlyB(p.b)}</td><td className="tnum pr-6" data-eval-pair-only-b="">{p.only_b}</td>
                    <td className="pr-4 text-faint">{P.neither}</td><td className="tnum" data-eval-pair-neither="">{p.neither}</td>
                  </tr>
                </tbody>
              </table>
              <p data-eval-pair-p={p.p_value}>{P.pValue(String(p.p_value))}{p.note ? ` · ${p.note}` : ` · ${P.significant}`}</p>
              {!!p.excluded && <p className="text-faint">{P.excluded(p.excluded)}</p>}
            </section>
          ))}
        </>
      )}
    </div>
  )
}
