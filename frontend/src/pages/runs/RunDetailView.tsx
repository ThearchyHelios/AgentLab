import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { Code2, Copy, GitFork, Link2, Square, SquareArrowOutUpRight, Trash2 } from 'lucide-react'
import clsx from 'clsx'
import { ApiError, api } from '../../api/client'
import {
  ErrorState, IconButton, Skeleton, Spinner, confirmDialog, promptDialog, toast,
} from '../../components/ui'
import { formatDateTime, formatNumber, formatTime, shortId } from '../../lib/format'
import { resolveStatus, statusLabel, type StatusCode } from '../../lib/status'
import { AssistantStream, type StreamTurn } from '../../run/AssistantStream'
import { decodePhase, decodeRun, summarizeRun, type RunFinal } from '../../run/decode'
import { ApprovalCard } from '../../run/RunPanel'
import { runStatusOf, type RunPhase } from '../../run/trace'
import { useCatalog } from '../../store/catalog'
import type { Approval, Run, RunEvent } from '../../types'
import { FailedBanner, FeedbackStrip, HeldBanner, WaitingBanner, type Feedback } from './Banners'
import { explainRunError } from './explain'
import { UNSAVED_HINT, duplicateNames, idTail, isLiveRun, isUnsaved, runName, runScope } from './model'
import { ClassChip, MoreMenu, TierChip, copyText, useNow, type MenuItem } from './parts'
import { ProvenanceBar, Telemetry, runClocks } from './Telemetry'
import { useRunDetail } from './useRunDetail'

/**
 * 一次运行的详情：头部读数、凭证、排错路径，下面是和画布助手栏、问数据页同一个
 * 解码器、同一个组件画的时间线——三处各写一套的话，同一次运行会被讲成三个
 * 不同的故事，而用户没法判断哪个是真的。
 */
export function RunDetailView({ runId, onChange, onDeleted }: {
  runId: string
  /** 这条运行变了（状态、用量）：列表原地更新那一行 */
  onChange: (run: Run) => void
  onDeleted: (id: string) => void
}) {
  const navigate = useNavigate()
  const { search } = useLocation()
  const d = useRunDetail(runId, onChange)
  const workflows = useCatalog((s) => s.workflows)
  const catalogLoaded = useCatalog((s) => s.loaded)
  const refreshCatalog = useCatalog((s) => s.refresh)
  const [raw, setRaw] = useState(false)
  const [busy, setBusy] = useState<null | 'stop' | 'continue' | 'rerun' | 'extract' | 'delete'>(null)
  const [feedback, setFeedback] = useState<Feedback | null>(null)
  const streamBox = useRef<HTMLDivElement>(null)
  const now = useNow(60_000)

  useEffect(() => {
    if (d.state !== 'missing') return
    toast.info('这条运行记录不在了，可能已经被删除')
    navigate({ pathname: '/runs', search }, { replace: true })
  }, [d.state, navigate, search])

  const run = d.run
  const pendingFlag = d.pending == null ? undefined : d.pending.length > 0
  // 接着流时事件就是事实；不接流时把 GET 到的状态和有没有待审批交给解码器收尾：
  // 服务被强杀的运行（interrupted、没有审批）这样才会停下，而不是一直转圈
  const finalStatus = !run || d.streaming ? undefined : run.status
  const final = useMemo<RunFinal | undefined>(
    () => (finalStatus ? { status: finalStatus, pending: pendingFlag } : undefined),
    [finalStatus, pendingFlag],
  )
  const phase = useMemo(() => decodePhase(d.events, final), [d.events, final])
  const steps = useMemo(() => decodeRun(d.events, final), [d.events, final])
  const code = run ? displayCode(run, phase, pendingFlag) : 'idle'

  // 接流期间状态变了（跑完、停到审批上）：左边那一行跟着变，不用等整表刷新
  const lastPhase = useRef<RunPhase | null>(null)
  useEffect(() => {
    if (!run || !d.streaming || lastPhase.current === phase) return
    lastPhase.current = phase
    const status = runStatusOf(phase)
    if (status && status !== run.status) onChange({ ...run, status })
  }, [phase, d.streaming, run, onChange])

  const labelOf = useCallback((id?: string | null) => {
    if (!id) return undefined
    const n = d.graph?.nodes.find((x) => x.id === id)
    return n?.data?.label || id
  }, [d.graph])

  if (!run) {
    if (d.state === 'error') {
      return <ErrorState error={d.error} onRetry={() => void d.reload()} className="flex-1" />
    }
    return (
      <div className="flex flex-1 flex-col gap-4 p-4" aria-busy="true">
        <Skeleton rows={2} height={14} />
        <Skeleton rows={1} cols={6} height={40} />
        <Skeleton rows={6} height={22} />
      </div>
    )
  }

  const live = d.streaming || isLiveRun(run.status)
  const failedEvent = lastOf(d.events, 'run.failed')
  const failedNode = run.error_node_id ?? d.trace.failedNodeId ?? (failedEvent?.data?.node_id as string | undefined) ?? null
  const failedDetail = (failedEvent?.data?.detail as string | undefined)
    ?? (lastOf(d.events, 'node.failed')?.data?.detail as string | undefined)
  const explain = code === 'failed'
    ? explainRunError(run.error ?? (failedEvent?.data?.error as string | undefined), failedDetail)
    : null
  const workflowGone = !!run.workflow_id && catalogLoaded && !workflows.some((w) => w.id === run.workflow_id)
  const waitingNode = d.trace.waitingNodeId ?? d.pending?.[0]?.node_id
  const focus = code === 'failed' ? failedNode : code === 'waiting' ? waitingNode : null
  const canvasHref = run.workflow_id && !workflowGone
    ? `/studio/${run.workflow_id}?run=${run.id}${focus ? `&focus=${encodeURIComponent(focus)}` : ''}`
    : null
  const dup = !!run.workflow_id && duplicateNames(workflows.map((w) => ({ id: w.id, name: w.name }))).has(run.workflow_name)

  // ---- 动作 ----

  const stop = async () => {
    setBusy('stop')
    try {
      await api.runs.cancel(run.id)
      toast.info('已发出停止，正在收尾…')
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(null)
    }
  }

  const carryOn = async () => {
    setBusy('continue')
    const resume = code !== 'failed'
    try {
      // 失败的走 continue（从失败节点接着跑）；挂起的走 resume(null)（从断点接回）
      const next = resume ? await api.runs.resume(run.id, null) : await api.runs.continue(run.id)
      setFeedback({ kind: 'continued', resume, from: resume ? undefined : labelOf(failedNode), at: Date.now() })
      d.follow(next)
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(null)
    }
  }

  // 缺了必填输入：接着跑还是同一份输入。补上那一项，用这次运行时的同一张图
  // （快照）重新发起——未保存的图也能重跑。正式运行只能跑「当前」发布的版本：
  // 之后又发布过新版的话，带上原来的版本号会被后端拒掉，所以不带，并说清跑哪一版
  const rerun = async (field: string) => {
    const formal = run.run_class === 'formal' && !!run.workflow_id && !workflowGone
    const wf = formal ? workflows.find((w) => w.id === run.workflow_id) : undefined
    if (wf && !wf.published_version) {
      toast.error(`「${wf.name}」现在没有发布版本，正式运行发不起来：先到画布发布`, {
        action: canvasHref ? { label: '去画布', onClick: () => navigate(canvasHref) } : undefined,
      })
      return
    }
    const published = wf?.published_version
    const value = await promptDialog({
      title: `补上「${field}」，重新运行`,
      body: formal
        ? `正式运行只跑当前发布的版本${published ? `：这次跑 v${published}${run.version && run.version !== published ? `，不是原来那次的 v${run.version}` : ''}` : ''}。其余输入照旧，发起一次新的运行；这条失败的记录保留。`
        : run.run_class === 'formal'
          ? '工作流已经删除，正式运行发不起来：这次按探索运行跑同一张图，其余输入照旧；这条失败的记录保留。'
          : '跑的是这次的同一张图，其余输入照旧，发起一次新的运行；这条失败的记录保留。',
      label: field,
      placeholder: `填写 ${field}`,
      confirmLabel: '重新运行',
      validate: (v) => (v.trim() ? null : '这一项是必填的'),
    })
    if (value == null) return
    setBusy('rerun')
    try {
      const input = { ...(run.input ?? {}), [field]: value }
      const scope = runScope(run)
      let next: Run
      if (formal) {
        next = await api.runs.start({ workflow_id: run.workflow_id!, run_class: 'formal', input, ...scope })
      } else {
        const graph = d.graph ?? (await api.runs.graph(run.id)).graph
        next = await api.runs.start({
          ...(run.workflow_id && !workflowGone ? { workflow_id: run.workflow_id } : {}),
          graph, input, ...scope,
        })
      }
      toast.ok(`已补上「${field}」重新发起运行`)
      navigate({ pathname: `/runs/${next.id}`, search })
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(null)
    }
  }

  const onResolved = async (a: Approval) => {
    try {
      const [next, all] = await Promise.all([
        api.runs.get(run.id),
        api.approvals.list({ run_id: run.id, status: 'all' }).catch(() => [] as Approval[]),
      ])
      const done = all.find((x) => x.id === a.id)
      const approved = typeof done?.response?.approved === 'boolean' ? done.response.approved : null
      setFeedback({
        kind: 'decided', approved, node: a.node_label ?? labelOf(a.node_id) ?? a.node_id,
        by: done?.resolved_by, at: done?.resolved_at ?? new Date().toISOString(),
        terminates: approved === false && a.mode !== 'approve',
      })
      // 批完要能看着它往下跑：接上实时流，时间线继续长
      d.follow(next)
    } catch {
      d.follow(null)
    }
    void d.refreshPending()
  }

  const jumpToApproval = (id: string) => {
    const el = streamBox.current?.querySelector<HTMLElement>(`[data-approval-card="${id}"]`)
    if (!el) return
    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    el.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'center' })
    el.classList.remove('runs-focus-ring')
    void el.offsetWidth
    el.classList.add('runs-focus-ring')
    setTimeout(() => el.classList.remove('runs-focus-ring'), 1500)
    el.querySelector<HTMLElement>('textarea, input, button')?.focus({ preventScroll: true })
  }

  const extract = async () => {
    setBusy('extract')
    try {
      const res = await api.copilot.fromRun(run.id)
      void refreshCatalog()
      toast.ok(
        `已提取为草稿「${res.name}」：${res.nodes} 个节点${res.dropped_nodes ? `，剪掉 ${res.dropped_nodes} 个没走到的` : ''}`,
      )
      // 提取出来的草稿接下来一定要去画布审改，直接带过去
      navigate(`/studio/${res.workflow_id}`)
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(null)
    }
  }

  const remove = async () => {
    const sealed = !!run.manifest_hash
    const ok = await confirmDialog({
      title: '删除这条运行记录？',
      body: `「${runName(run)}」${shortId(run.id)}，${formatDateTime(run.created_at ?? null)} 发起。`,
      consequences: [
        '事件流、产出工件和审批留痕一起删除，不可恢复',
        ...(sealed ? ['封存清单再也无法核对，这条运行不能再作为追溯凭证'] : []),
        ...(code === 'waiting' ? ['停在审批上的那一步也随之作废'] : []),
      ],
      danger: true,
      confirmLabel: '删除记录',
    })
    if (!ok) return
    setBusy('delete')
    try {
      try {
        await api.runs.remove(run.id)
      } catch (e) {
        // 封存过的正式运行：后端要 force，并在 detail 里写明后果。照它说的再确认一次，
        // 而且要照抄 id 前几位——这是出具结果的追溯凭证，不该一路回车删掉
        if (e instanceof ApiError && e.status === 409 && run.run_class === 'formal' && sealed) {
          const forced = await confirmDialog({
            title: '强制删除已封存的正式运行？',
            body: e.message,
            consequences: ['删除之后任何人都无法再核对这次出具的来源'],
            danger: true,
            requireText: run.id.slice(0, 6),
            confirmLabel: '强制删除',
          })
          if (!forced) return
          await api.runs.remove(run.id, { force: true })
        } else {
          throw e
        }
      }
      toast.ok('已删除这条运行记录')
      onDeleted(run.id)
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(null)
    }
  }

  const menu: MenuItem[] = [
    { key: 'copy-id', label: '复制运行 ID', icon: <Copy size={12} />, onSelect: () => void copyText(run.id, '运行 ID') },
    {
      key: 'copy-link', label: '复制链接', icon: <Link2 size={12} />,
      onSelect: () => void copyText(`${location.origin}/runs/${run.id}`, '链接'),
    },
    {
      key: 'delete', label: '删除记录…', icon: <Trash2 size={12} />, danger: true, onSelect: () => void remove(),
      disabled: live || busy === 'delete',
      hint: live ? '运行中不能删除：先停止它' : undefined,
    },
  ]

  // ---- 时间线 ----

  const finished = lastOf(d.events, 'run.finished')
  const clocks = runClocks(run, d.trace, phase, d.streaming, Date.now())
  const paused = d.trace.waits.length > 0 || d.trace.drives.length > 1
  // statusCode：轮次头的徽标按它画。已取消、已挂起在 StreamTurn 的四种 phase 里
  // 只能算 done，不给它的话轮次头会给一条取消的运行画上「完成」的勾
  const turn: StreamTurn = {
    id: run.id,
    phase: phase === 'running' || phase === 'queued' ? 'running'
      : phase === 'waiting' ? 'waiting'
      : phase === 'failed' ? 'error' : 'done',
    status: statusLabel(code),
    steps,
    // 事件里的成果会被截断（output_truncated），截断了就取 run 上那份完整的
    output: (finished && !finished.data?.output_truncated ? finished.data?.output : null)
      ?? (run.output && Object.keys(run.output).length ? run.output : null),
    error: explain ? (failedNode ? `「${labelOf(failedNode)}」${explain.title}` : explain.title) : undefined,
    runClass: run.run_class,
    statusCode: code,
    // 轮次头的计时和详情头同一个口径：跑完写墙钟，跑着从航迹的起点实时算。
    // 停下来过的（等过审批、失败后接着跑）例外，只数执行的部分：从第一次开始算，
    // 秒表里会是好几天的等待（「213:46:45.6」）。墙钟和等人在上面的读数里分开写着
    // 等审批的还没结束，不给时长——给了就是一个一直在涨、却被当成"花了多久"的数
    ...(phase === 'running' || phase === 'queued'
      ? (paused && clocks.active != null
        ? { startedAt: Date.now() - (d.trace.skewMs ?? 0) - clocks.active }
        : d.trace.startedAt != null ? { startedAt: d.trace.startedAt } : {})
      : phase !== 'waiting' && clocks.wall != null ? { elapsedMs: paused ? clocks.active ?? clocks.wall : clocks.wall } : {}),
  }

  const cards = d.pending?.length ? (
    <div className="mt-3 space-y-2">
      {d.pending.map((a) => (
        <div key={a.id} data-approval-card={a.id} className="relative overflow-hidden rounded-lg border"
             style={{ borderColor: 'color-mix(in srgb, var(--st-waiting) 45%, var(--border))' }}>
          {/* 详情头已经写着是哪个工作流，卡头不再重复 */}
          <ApprovalCard approval={a} onResolved={() => onResolved(a)} showWorkflow={false} />
        </div>
      ))}
    </div>
  ) : null

  const unsaved = isUnsaved(run)

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col" data-run-detail={run.id} data-run-code={code}>
      <header className="shrink-0 border-b">
        <div className="flex items-start gap-3 px-4 pb-2 pt-2.5">
          <div className="min-w-0 flex-1">
            <div className="flex min-w-0 items-center gap-2">
              <h2 className={clsx('min-w-0 truncate text-sm font-semibold', unsaved && 'text-dim')}
                  title={unsaved ? UNSAVED_HINT : run.workflow_name}>
                {runName(run)}
              </h2>
              {dup && <span className="mono shrink-0 text-2xs text-faint" title={`工作流 id：${run.workflow_id}`}>{idTail(run.workflow_id)}</span>}
              <ClassChip runClass={run.run_class} version={run.version} always />
              <TierChip tier={(run.output as any)?._issuance?.tier} />
            </div>
            <div className="mt-0.5 flex min-w-0 items-center gap-1 text-2xs text-faint">
              <span className="mono">{shortId(run.id)}</span>
              <span>·</span>
              <time className="tnum" title={`发起于 ${formatDateTime(run.created_at ?? null)}`}>
                {formatTime(run.created_at ?? null, new Date(now))}
              </time>
              <span>·</span>
              <span title={run.started_by ? undefined : '发起时设置里没有署名'}>
                {run.started_by || '未署名'} 发起
              </span>
              <span>·</span>
              <span className="tnum">{formatNumber(d.events.length)} 条事件</span>
              {workflowGone && <span>· 工作流已删除</span>}
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            {(code === 'running' || code === 'queued') && (
              <button type="button" className="btn btn-sm btn-danger" disabled={busy === 'stop'} onClick={() => void stop()}
                      data-action="stop" title="停止这次运行：已经跑完的节点保留，正在跑的收尾为已取消">
                {busy === 'stop' ? <Spinner size={11} /> : <Square size={10} aria-hidden fill="currentColor" />} 停止
              </button>
            )}
            {canvasHref && code !== 'failed' && (
              <Link className="btn btn-sm" to={canvasHref} data-action="open-canvas"
                    title={code === 'waiting' ? '打开这张工作流，并对准等审批的节点' : '在画布里打开这张工作流和这次运行'}>
                <SquareArrowOutUpRight size={11} aria-hidden /> 在画布中打开
              </Link>
            )}
            {code === 'succeeded' && run.run_class !== 'formal' && (
              <button type="button" className="btn btn-sm" disabled={busy === 'extract'} onClick={() => void extract()}
                      data-action="extract" title="把这次实际走过的路径提取成草稿工作流（剪掉没走到的节点），提取后到画布里审改">
                {busy === 'extract' ? <Spinner size={11} /> : <GitFork size={11} aria-hidden />} 提取模板
              </button>
            )}
            <IconButton
              label={raw ? '回到可读视图' : `看原始事件（${d.events.length} 条）`}
              aria-pressed={raw}
              className={clsx(raw && 'text-[var(--accent)]')}
              onClick={() => setRaw((v) => !v)}
              icon={<Code2 size={13} aria-hidden />}
              data-action="raw"
            />
            <MoreMenu items={menu} />
          </div>
        </div>
        <Telemetry run={run} trace={d.trace} phase={phase} code={code} streaming={d.streaming}
                   pending={d.pending} labelOf={labelOf} />
        <ProvenanceBar run={run} eventCount={d.events.length} phase={phase} />
        {explain && (
          <FailedBanner explain={explain} nodeId={failedNode} nodeLabel={labelOf(failedNode)}
                        canvasHref={canvasHref} onContinue={() => void carryOn()}
                        onRerun={(field) => void rerun(field)} busy={busy === 'continue' || busy === 'rerun'} />
        )}
        {(code === 'held' || code === 'suspended') && (
          <HeldBanner reason={run.error} onContinue={() => void carryOn()} busy={busy === 'continue'} />
        )}
        {code === 'waiting' && !!d.pending?.length && (
          <WaitingBanner approvals={d.pending} labelOf={labelOf} onJump={jumpToApproval} now={now} />
        )}
        {feedback && <FeedbackStrip feedback={feedback} onDismiss={() => setFeedback(null)} />}
      </header>

      <div ref={streamBox} className="min-h-0 flex-1">
        {raw
          ? <RawEvents events={d.events} />
          : (
            // 回看历史停在摘要（第一处失败、审批卡或顶部），在跑的才贴着最新处
            <AssistantStream
              key={run.id}
              turns={[turn]}
              approvalsFor={() => cards}
              landing={live ? 'end' : 'summary'}
              clockSkewMs={d.trace.skewMs ?? 0}
            />
          )}
      </div>

      <footer className="flex shrink-0 items-center gap-3 border-t px-4 py-1.5 text-2xs text-faint">
        <span className="tnum">{formatNumber(d.events.length)} 条事件</span>
        {d.streaming && <span style={{ color: 'var(--st-running)' }}>· 实时接收中</span>}
        <span className="flex-1" />
        {run.input && !!Object.keys(run.input).length && (
          <span className="min-w-0 truncate" title={JSON.stringify(run.input, null, 2)}>
            输入：{summarizeRun(run.input)}
          </span>
        )}
      </footer>
    </div>
  )
}

/** 事件之外的事实（查到的状态、有没有审批）和事件推出的相位合成一个显示码 */
function displayCode(run: Run, phase: RunPhase, pending: boolean | undefined): StatusCode {
  switch (phase) {
    case 'running': case 'queued': case 'waiting': case 'succeeded': case 'failed': case 'cancelled':
      return phase
    case 'suspended':
      // 后端写的是 interrupted：服务重启打断、没有审批，叫「已挂起 · 可续跑」
      return run.status === 'suspended' ? 'suspended' : 'held'
    default:
      return resolveStatus(run.status, { pendingApproval: run.status === 'interrupted' ? pending : undefined })
  }
}

function lastOf(events: RunEvent[], type: string): RunEvent | undefined {
  for (let i = events.length - 1; i >= 0; i--) if (events[i].type === type) return events[i]
  return undefined
}

/** 原始事件。翻译层出问题时用来对照；时间写成相对第一条事件的偏移，快慢一眼可比 */
function RawEvents({ events }: { events: RunEvent[] }) {
  if (!events.length) {
    return <div className="p-3 text-center text-2xs text-faint">没有事件记录</div>
  }
  const t0 = events.find((e) => typeof e.ts === 'number' && e.ts > 0)?.ts
  return (
    <div className="h-full overflow-y-auto" data-raw-events="">
      {events.map((e) => (
        <div key={e.seq} className="flex gap-2 border-b px-3 py-1 text-2xs last:border-0">
          <span className="tnum w-10 shrink-0 text-right text-faint">#{e.seq}</span>
          <span className="tnum mono w-16 shrink-0 text-right text-faint"
                title={formatDateTime(e.ts)}>
            {t0 != null && typeof e.ts === 'number' ? `+${((e.ts - t0)).toFixed(2)}s` : '—'}
          </span>
          <span className="mono w-36 shrink-0 truncate text-[var(--accent)]">{e.type}</span>
          <span className="mono w-28 shrink-0 truncate text-dim">{e.node_id ?? ''}</span>
          <span className="mono min-w-0 flex-1 break-all text-faint">
            {JSON.stringify(e.data).slice(0, 240)}
          </span>
        </div>
      ))}
    </div>
  )
}
