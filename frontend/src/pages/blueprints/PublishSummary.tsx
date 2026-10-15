/**
 * 发布摘要（docs/design/normal-user-apps.md N5、§3.6、§4.4）：一次确认发布。
 *
 * 打开时出发布单（POST /api/blueprints/{id}/publish-plan，只读预检）：每一步一句白话「会发生什么」；查出的问题按白话层的
 * who 分三栏——你来定、助手可以修、要找管理员（没登记白话的照旧写技术说法，归「你来定」）。有 error 级的问题不能确认。
 * 确认按钮写清结果（「发布并每周一 09:00 自动出数」）。执行（run）后逐步打勾，进行中每秒取一次发布单；停下时写停在哪一步、
 * 为什么，修好后「接着做」（resume，已完成的步骤不重做）。受管级只给管理员选。
 *
 * 发布单在服务端编排、每步留痕（P2），这里只显示和触发，不在前端按顺序调各个接口。
 */
import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { CircleCheck, CircleDashed, CircleMinus, CircleX, MessageSquare, Rocket, RotateCw } from 'lucide-react'
import { api } from '../../api/client'
import { ErrorState, Modal, Skeleton, Spinner, toast } from '../../components/ui'
import { APP_EDITOR_TEXT, PLAN_STEP_LABEL, PLAN_STEP_STATUS_LABEL, PLAN_TEXT, cadenceWhen } from '../../lib/terms'
import { useAtLeast } from '../../store/auth'
import { useBlueprint } from '../../store/blueprint'
import type { Disposition } from '../../typesApp'
import type { PlainIssue, PlanStep, ProblemWho, PublishPlanRow } from '../../typesBlueprint'
import { appPath, usePoll } from '../apps/shared'

const WHO: ProblemWho[] = ['you', 'assistant', 'admin']

/**
 * 这条问题归谁：服务端在问题顶层给的 who（发布单按自动修复的种类、要管理员的编号算好的）为准，没有再看白话里登记的
 * 那一档。PlainIssue 还没有顶层的 who（契约待补），先按多一项读
 */
const whoOf = (p: PlainIssue): ProblemWho => (p as PlainIssue & { who?: ProblemWho }).who ?? p.plain?.who ?? 'you'
const DISPOSITIONS: Disposition[] = ['recompute', 'dual', 'incomparable']
const POLL_MS = 1000

export function PublishSummary({ blueprintId, open, onClose, onFix }: {
  blueprintId: string
  open: boolean
  onClose: () => void
  /** 「交给助手修」：把这几条问题交给蓝图助手 */
  onFix: (lines: string[]) => void
}) {
  const bp = useBlueprint((s) => s.byId[blueprintId]?.blueprint)
  const admin = useAtLeast('admin')
  const [level, setLevel] = useState<'published' | 'governed'>('published')
  const [firstRun, setFirstRun] = useState<boolean | undefined>(undefined)
  // 口径升版的处置（你来定）：选了就重出发布单
  const [dispositions, setDispositions] = useState<Record<string, Disposition>>({})
  const [plan, setPlan] = useState<PublishPlanRow | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState<'plan' | 'run' | 'resume' | null>(null)

  const replan = useCallback(async (lvl: 'published' | 'governed', fr: boolean | undefined, disp: Record<string, Disposition>) => {
    setBusy('plan')
    try {
      const p = await api.blueprints.publishPlan(blueprintId, {
        level: lvl, ...(fr == null ? {} : { first_run: fr }), ...(Object.keys(disp).length ? { dispositions: disp } : {}),
      })
      setPlan(p)
      setError(null)
    } catch (e) {
      setError(e)
    } finally {
      setBusy(null)
    }
  }, [blueprintId])

  // 打开时出一张新的发布单（预检是只读的，重出不留副作用）
  useEffect(() => {
    if (!open) return
    setPlan(null)
    setError(null)
    setFirstRun(undefined)
    setDispositions({})
    void replan(level, undefined, {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  // 执行中逐步打勾：每秒取一次
  usePoll(() => {
    if (!plan) return
    void api.blueprints.plan(plan.id).then((p) => {
      setPlan(p)
      if (p.status === 'done') void useBlueprint.getState().load(blueprintId, { force: true })
    }).catch(() => undefined)
  }, POLL_MS, open && plan?.status === 'running')

  const act = async (kind: 'run' | 'resume') => {
    if (!plan) return
    setBusy(kind)
    try {
      const p = kind === 'run' ? await api.blueprints.runPlan(plan.id) : await api.blueprints.resumePlan(plan.id)
      setPlan(p)
      if (p.status === 'done') void useBlueprint.getState().load(blueprintId, { force: true })
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(null)
    }
  }

  const problems = plan?.problems ?? []
  const blocked = problems.some((p) => p.level === 'error')
  const started = !!plan && plan.status !== 'planned'
  const scheduled = !!plan?.steps.some((s) => s.kind === 'create_schedule' && s.status !== 'skipped')
  const when = scheduled ? cadenceWhen(bp?.spec.cadence?.value) : ''
  const failed = plan?.steps.find((s) => s.status === 'failed')
  const appId = String(plan?.steps.find((s) => s.kind === 'create_app')?.result?.app_id ?? '') || bp?.app_id || ''
  const checked = firstRun ?? plan?.options.first_run ?? false

  const footer = !plan ? undefined : plan.status === 'planned' ? (
    <>
      <button type="button" className="btn" onClick={onClose}>{PLAN_TEXT.close}</button>
      <button type="button" className="btn btn-primary" disabled={blocked || !!busy} onClick={() => void act('run')}
              data-plan-confirm="" title={blocked ? PLAN_TEXT.blocked : undefined}>
        {busy === 'run' ? <Spinner size={12} /> : <Rocket size={12} aria-hidden />}{' '}
        {busy === 'run' ? PLAN_TEXT.running : when ? PLAN_TEXT.confirmWith(when) : PLAN_TEXT.confirm}
      </button>
    </>
  ) : plan.status === 'stopped' ? (
    <>
      <button type="button" className="btn" onClick={onClose}>{PLAN_TEXT.close}</button>
      <button type="button" className="btn btn-primary" disabled={!!busy} onClick={() => void act('resume')} data-plan-resume="">
        {busy === 'resume' ? <Spinner size={12} /> : <RotateCw size={12} aria-hidden />}{' '}
        {busy === 'resume' ? PLAN_TEXT.resuming : PLAN_TEXT.resume}
      </button>
    </>
  ) : plan.status === 'done' ? (
    <>
      <button type="button" className="btn" onClick={onClose}>{PLAN_TEXT.close}</button>
      {appId && <Link to={appPath(appId)} className="btn btn-primary" data-plan-open-app="">{PLAN_TEXT.openApp}</Link>}
    </>
  ) : undefined

  return (
    <Modal open={open} onClose={onClose} title={PLAN_TEXT.title} width={760} footer={footer}>
      <div className="space-y-3 text-xs" data-plan={plan?.status ?? (error ? 'error' : 'planning')}>
        {error && !plan ? (
          <>
            <ErrorState error={error} onRetry={() => void replan(level, firstRun, dispositions)} compact />
          </>
        ) : !plan ? (
          <div role="status" className="space-y-2">
            <p className="flex items-center gap-1.5 text-dim"><Spinner size={11} /> {PLAN_TEXT.planning}</p>
            <Skeleton rows={4} />
          </div>
        ) : (
          <>
            {/* 级别和「跑第一次」：确认之前可以改，改了重出发布单 */}
            {!started && (
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5">
                {admin && (
                  <label className="flex items-center gap-1.5 text-dim" title={PLAN_TEXT.levelHint}>
                    {PLAN_TEXT.level}
                    <select className="field w-auto py-0.5 text-xs" value={level} disabled={!!busy} data-plan-level=""
                            onChange={(e) => {
                              const lvl = e.target.value as 'published' | 'governed'
                              setLevel(lvl)
                              void replan(lvl, firstRun, dispositions)
                            }}>
                      <option value="published">{PLAN_TEXT.levelPublished}</option>
                      <option value="governed">{PLAN_TEXT.levelGoverned}</option>
                    </select>
                  </label>
                )}
                {plan.steps.some((s) => s.kind === 'first_run') && (
                  <label className="flex items-center gap-1.5 text-dim" title={PLAN_TEXT.firstRunHint}>
                    <input type="checkbox" checked={checked} disabled={!!busy} data-plan-first-run=""
                           onChange={(e) => { setFirstRun(e.target.checked); void replan(level, e.target.checked, dispositions) }} />
                    {PLAN_TEXT.firstRun}
                  </label>
                )}
                {busy === 'plan' && <Spinner size={11} />}
              </div>
            )}

            <section aria-labelledby="plan-steps">
              <h3 id="plan-steps" className="mb-1.5 text-2xs font-semibold text-dim">{PLAN_TEXT.steps}</h3>
              <ol className="space-y-1" data-plan-steps={plan.steps.length}>
                {plan.steps.map((s, i) => (
                  <PlanStepRow key={s.kind} step={s} texts={plan.summary.filter((x) => x.step === s.kind).map((x) => x.text)}
                               active={plan.status === 'running' && s.status === 'pending'
                                 && plan.steps.slice(0, i).every((x) => x.status !== 'pending')} />
                ))}
              </ol>
            </section>

            {plan.status === 'stopped' && failed && (
              <div role="alert" className="rounded-lg border px-3 py-2 leading-relaxed" data-plan-stopped={failed.kind}
                   style={{ borderColor: 'color-mix(in srgb, var(--err) 40%, var(--border))', background: 'color-mix(in srgb, var(--err) 6%, transparent)' }}>
                <div className="font-medium text-fg">{PLAN_TEXT.stopped(PLAN_STEP_LABEL[failed.kind] ?? failed.kind)}</div>
                {failed.message && <div className="mt-0.5 text-dim" data-plan-stopped-why="">{failed.message}</div>}
                <div className="mt-0.5 text-faint">{PLAN_TEXT.stoppedBody}</div>
              </div>
            )}
            {plan.status === 'done' && (
              <div role="status" className="rounded-lg border px-3 py-2 leading-relaxed" data-plan-done=""
                   style={{ borderColor: 'color-mix(in srgb, var(--ok) 40%, var(--border))', background: 'color-mix(in srgb, var(--ok) 6%, transparent)' }}>
                <div className="font-medium text-fg">{PLAN_TEXT.done}</div>
                <div className="mt-0.5 text-dim">{PLAN_TEXT.doneBody}</div>
              </div>
            )}

            {!started && (
              <section aria-labelledby="plan-problems">
                <h3 id="plan-problems" className="mb-1.5 text-2xs font-semibold text-dim">{PLAN_TEXT.problems}</h3>
                {!problems.length ? <p className="text-2xs text-faint" data-plan-no-problems="">{PLAN_TEXT.noProblems}</p> : (
                  <>
                    {blocked && <p className="mb-1.5 text-2xs" style={{ color: 'var(--st-failed)' }} data-plan-blocked="">{PLAN_TEXT.blocked}</p>}
                    <div className="grid grid-cols-1 gap-2 md:grid-cols-3" data-plan-problems={problems.length}>
                      {WHO.map((who) => (
                        <ProblemColumn key={who} who={who} items={problems.filter((p) => whoOf(p) === who)}
                                       onFix={(lines) => { onClose(); onFix(lines) }} busy={!!busy}
                                       onDispose={(alias, d) => {
                                         const next = { ...dispositions, [alias]: d }
                                         setDispositions(next)
                                         void replan(level, firstRun, next)
                                       }} />
                      ))}
                    </div>
                  </>
                )}
              </section>
            )}
          </>
        )}
      </div>
    </Modal>
  )
}

function PlanStepRow({ step, texts, active }: { step: PlanStep; texts: string[]; active: boolean }) {
  const icon = active ? <Spinner size={12} />
    : step.status === 'done' ? <CircleCheck size={13} aria-hidden style={{ color: 'var(--ok)' }} />
    : step.status === 'failed' ? <CircleX size={13} aria-hidden style={{ color: 'var(--err)' }} />
    : step.status === 'skipped' ? <CircleMinus size={13} aria-hidden className="text-faint" />
    : <CircleDashed size={13} aria-hidden className="text-faint" />
  return (
    <li className="flex items-start gap-2" data-plan-step={step.kind} data-step-status={step.status}>
      <span className="mt-[1px] shrink-0">{icon}</span>
      <span className="min-w-0 flex-1">
        <span className="font-medium text-fg">{PLAN_STEP_LABEL[step.kind] ?? step.kind}</span>
        <span className="sr-only">：{PLAN_STEP_STATUS_LABEL[step.status]}</span>
        {texts.map((t) => <span key={t} className="block leading-relaxed text-dim">{t}</span>)}
      </span>
    </li>
  )
}

/**
 * 一栏问题。口径升版没选处置的（app.upgrade_policy，带别名）就地给三种处置让人选——这是「你来定」的事，说法同应用编辑器的发布
 * （APP_EDITOR_TEXT.dispositions），选了重出发布单
 */
function ProblemColumn({ who, items, onFix, onDispose, busy }: {
  who: ProblemWho; items: PlainIssue[]; onFix: (lines: string[]) => void
  onDispose: (alias: string, d: Disposition) => void; busy: boolean
}) {
  const lines = items.map((p) => p.plain?.title ?? p.message)
  return (
    <div className="rounded-lg border bg-bg p-2" data-plan-who={who} data-plan-who-count={items.length}>
      <div className="text-2xs font-semibold text-fg">{PLAN_TEXT.who[who]}</div>
      <div className="mb-1.5 text-2xs leading-relaxed text-faint">{PLAN_TEXT.whoHint[who]}</div>
      {!items.length ? <p className="text-2xs text-faint">{PLAN_TEXT.whoEmpty}</p> : (
        <ul className="space-y-1.5">
          {items.map((p, i) => (
            <li key={`${p.code}-${i}`} className="text-2xs leading-relaxed" data-problem={p.code}>
              <div style={{ color: p.level === 'error' ? 'var(--st-failed)' : 'var(--st-waiting)' }}>{p.plain?.title ?? p.message}</div>
              {p.plain?.why && <div className="text-dim">{p.plain.why}</div>}
              {p.plain?.action && <div className="text-faint">{p.plain.action}</div>}
              {p.code === 'app.upgrade_policy' && typeof (p as { alias?: unknown }).alias === 'string' && (
                <div className="mt-1 flex flex-wrap gap-1" role="group" data-plan-disposition={(p as { alias?: string }).alias}>
                  {DISPOSITIONS.map((d) => (
                    <button key={d} type="button" className="btn btn-xs" disabled={busy} title={APP_EDITOR_TEXT.dispositionHint[d]}
                            onClick={() => onDispose((p as { alias?: string }).alias!, d)}>
                      {APP_EDITOR_TEXT.dispositions[d]}
                    </button>
                  ))}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
      {who === 'assistant' && items.length > 0 && (
        <button type="button" className="btn btn-xs mt-2" onClick={() => onFix(lines)} data-plan-fix="">
          <MessageSquare size={11} aria-hidden /> {PLAN_TEXT.fixWithAssistant}
        </button>
      )}
    </div>
  )
}
