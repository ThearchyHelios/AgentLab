import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  ChevronLeft, ChevronRight, Code2, Crosshair, Hand, History, MessageSquarePlus, Play, Square,
  Undo2, Wand2,
} from 'lucide-react'
import clsx from 'clsx'
import { useCatalog } from '../store/catalog'
import { useStudio, type CopilotTurn } from '../store/studio'
import { IconButton, StatusBadge, toast } from '../components/ui'
import { formatClock, formatCost, formatDuration, formatTokens, shortId, NONE } from '../lib/format'
import { statusLabel } from '../lib/status'
import { runClassLabel } from '../lib/terms'
import { AssistantStream, type StreamTurn } from './AssistantStream'
import { Composer } from './Composer'
import {
  copilotOutcome, decodeCopilot, decodeRun, SELF_CHECK_ROUNDS, type CopilotIssue, type Step,
} from './decode'
import { ApprovalCard } from './RunPanel'
import { isSettled, liveAt, project, type RunPhase } from './trace'
import { useRunClock } from './useRunClock'

/**
 * 画布右栏。
 *
 * 一个常驻的家（和 Copilot 说话），两层临时盖上来的东西（节点属性、运行过程）。
 * 三者不是平级的 tab——tab 互斥，会把下面那层卸载掉，草稿和滚动位置全丢。
 *
 * 运行为什么不留在对话流里：
 *
 * 之前把 Copilot 建图和跑图排进了同一条时间轴，理由是"它们是一件事的两半"。
 * 那个说法站不住。它们在时间上先后发生，但不是同一种东西——最直接的证据是
 * 运行那一轮的 question 只能填 undefined：一个没有提问的"对话轮次"，本来就
 * 不是对话。
 *
 * 更实际的问题是输入框：它贴在运行结果下面，暗示你可以回复这次运行。但你
 * 不能——在那儿打字改的是画布上的图，不是回答运行。这是个假的可供性，
 * 而且两者的生命周期也对不上：Copilot 的轮次会累积，运行永远只有最新一次。
 *
 * 所以运行有自己的一层，没有输入框；对话那一层留一条进度条，点一下过去。
 */
export function AssistantPanel() {
  const run = useStudio((s) => s.run)
  const [view, setView] = useState<'chat' | 'run'>('chat')

  // 发起一次新运行就把这一层推到前面——那是此刻唯一在发生的事
  useEffect(() => {
    if (run?.id) setView('run')
  }, [run?.id])

  return (
    <div className="relative flex h-full min-h-0 flex-col">
      {view === 'run' && run
        ? <RunView onBack={() => setView('chat')} />
        : <ChatView hasRun={!!run} onOpenRun={() => setView('run')} />}
    </div>
  )
}

// -------------------------------------------------------------------------
// 对话：和 Copilot 说话的地方
// -------------------------------------------------------------------------

/** 一轮开始的时刻：store 记了就用它，老轮次没有 */
type TurnExtras = CopilotTurn & { startedAt?: number }

const PHASE_TEXT: Record<string, string> = {
  connecting: '正在连接模型',
  planning: '正在理解需求、规划步骤',
  building: '正在搭建流程',
  wiring: '正在连接数据流',
  finalizing: '正在排版和校验',
  repairing: '正在按自查结果修正',
}

/**
 * 一轮 Copilot 的卡片标题。只看 phase 的话，只回了一句话、自查没修好、少放了
 * 一步，全都写「流程已更新到画布」——而这几种的下一步完全不同
 */
function copilotTurn(t: TurnExtras, live: { lastOp: string; phase: string; repairing?: number }): StreamTurn {
  const running = t.phase === 'running'
  const out = copilotOutcome(t.ops, running)
  const found = t.ops.find((o) => o.op === 'check' && o.status === 'repairing')
  const foundN = Array.isArray(found?.issues) ? found!.issues.length : 0
  const reply = out.reply ?? t.reply
  const errorOp = out.error
  const steps = decodeCopilot(t.ops, { context: 'canvas' })
  // 回执说清改了什么：「流程已更新」不说改了哪几处，改坏了都不知道从哪看起
  const d = t.diff
  const changed = d ? [
    d.added.length ? `新增 ${d.added.length}` : '',
    d.changed.length ? `修改 ${d.changed.length}` : '',
    d.removed.length ? `删除 ${d.removed.length}` : '',
  ].filter(Boolean).join(' · ') : ''
  const updated = changed ? `已更新画布（${changed}）` : '已更新画布'

  let status = ''
  let tone: StreamTurn['tone']
  let statusCode: string | undefined
  if (running) {
    status = live.lastOp
      || (live.phase === 'repairing' && out.repairing
        ? `${PHASE_TEXT.repairing}（第 ${out.repairing}/${SELF_CHECK_ROUNDS} 轮）`
        : PHASE_TEXT[live.phase] ?? '正在生成…')
  } else if (t.outcome === 'reverted') {
    status = '已撤回，画布回到了这一轮之前'
    statusCode = 'cancelled'
  } else if (t.phase === 'error' || errorOp) {
    status = '这一轮没能完成'
  } else if (reply && out.kind !== 'built') {
    status = '已回答（没有改动画布）'
  } else if (out.kind === 'built') {
    if (out.check?.status === 'failed') {
      status = `已放到画布，但还有 ${out.check.issues.length} 处问题要你处理`
      tone = 'warn'
    } else if (out.skipped.length) {
      status = `已放到画布，但有 ${out.skipped.length} 步没放上`
      tone = 'warn'
    } else if (out.check?.status === 'passed' && out.check.repaired) {
      status = `${updated} · 自查发现 ${foundN || '几'} 处问题，已自动修好`
    } else if (out.check?.status === 'passed') {
      status = `${updated} · 自查通过`
    } else {
      status = updated
    }
  } else {
    // 流正常收尾，却既没有交回图也没有回话（停止了，或者模型什么都没给）
    status = '没有改动画布'
    statusCode = 'cancelled'
  }

  return {
    id: t.id,
    question: t.instruction,
    phase: running ? 'running' : t.phase === 'error' || errorOp ? 'error' : 'done',
    status,
    tone,
    statusCode,
    steps,
    output: reply ? { 回答: reply } : t.explanation ? { 说明: t.explanation } : null,
    error: errorOp
      ? { error: errorOp.message, hint: errorOp.hint, detail: errorOp.raw }
      : t.error || undefined,
    startedAt: t.startedAt,
  }
}

function ChatView({ hasRun, onOpenRun }: { hasRun: boolean; onOpenRun: () => void }) {
  const copilotTurns = useStudio((s) => s.copilotTurns) as TurnExtras[]
  const copilot = useStudio((s) => s.copilot)
  const retryCopilot = useStudio((s) => s.retryCopilot)
  const repairWithCopilot = useStudio((s) => s.repairWithCopilot)
  const undoCopilotTurn = useStudio((s) => s.undoCopilotTurn)
  const newCopilotConversation = useStudio((s) => s.newCopilotConversation)
  const memory = useStudio((s) => s.copilotMemory)
  const focusNode = useStudio((s) => s.focusNode)
  const select = useStudio((s) => s.select)
  const [showPast, setShowPast] = useState(false)

  const turns = useMemo<StreamTurn[]>(() => copilotTurns.map((t) => copilotTurn(t, copilot)),
    [copilotTurns, copilot])
  // 打开这张图之前就有的那几轮：模型下一条指令照样会带上它们。面板上一片空白、
  // 模型却记着之前的事，它引用「刚才那个团队」时用户无从理解
  const settledHere = copilotTurns.filter((t) => t.phase !== 'running').length
  const earlier = memory.past.slice(0, Math.max(0, memory.past.length - settledHere))
  const pastTurns = useMemo<StreamTurn[]>(() => earlier.map((t) => ({
    id: `past-${t.id}`,
    question: t.question,
    phase: t.status === 'error' ? 'error' : 'done',
    status: t.status === 'error' ? '这一轮没能完成' : t.graph ? '改过画布' : '已回答',
    steps: [],
    output: t.answer ? { 回答: t.answer } : t.explanation ? { 说明: t.explanation } : null,
    error: t.status === 'error' ? t.error || undefined : undefined,
  })), [earlier])
  const shown = showPast ? [...pastTurns, ...turns] : turns
  const streamRef = useRef<HTMLDivElement>(null)
  // 展开之前的轮次：它们补在最上面，视野不动的话人什么也看不到，像是没点上。回到
  // 顶上从补出来的第一轮往下读（不按那一轮的位置滚：它刚挂上、还在淡入位移，量出来差几像素）
  useLayoutEffect(() => {
    if (!showPast) return
    const el = streamRef.current?.querySelector<HTMLElement>('[data-stream-scroll]')
    if (el) el.scrollTop = 0
  }, [showPast])

  const empty = !copilotTurns.length && !earlier.length

  const locate = (id: string) => { select(id); focusNode(id) }

  /** 一轮之后的动作：定位问题节点、让 Copilot 再修、撤销这次生成、重试 */
  const actions = (turn: StreamTurn, i: number) => {
    if (i !== shown.length - 1 || turn.phase === 'running') return null
    const source = copilotTurns.find((t) => t.id === turn.id)
    if (!source) return null
    const out = copilotOutcome(source.ops)
    if (turn.phase === 'error') {
      return (
        <button type="button" className="btn btn-xs" onClick={() => retryCopilot()}>
          <Wand2 size={11} aria-hidden /> 用同一句话重试
        </button>
      )
    }
    const issues = out.check?.status === 'failed' ? out.check.issues : []
    const undo = source.outcome === 'applied' || (out.kind === 'built' && source.outcome !== 'reverted')
    if (!issues.length && !out.skipped.length) {
      return undo ? (
        <button type="button" className="btn btn-xs btn-ghost" onClick={() => undoCopilotTurn(source.id)}>
          <Undo2 size={11} aria-hidden /> 撤销这次生成
        </button>
      ) : null
    }
    return (
      <div className="w-full space-y-1.5">
        {/* 没修好的问题直接摊开，不折叠：这是此刻唯一要做的事 */}
        {issues.length > 0 && <IssueList issues={issues} onLocate={locate} />}
        <div className="flex flex-wrap items-center gap-1.5">
          <button type="button" className="btn btn-xs btn-primary"
                  onClick={() => repairWithCopilot(source.id)}>
            <Wand2 size={11} aria-hidden /> {issues.length ? '让 Copilot 再修' : '让 Copilot 补上'}
          </button>
          {undo && (
            <button type="button" className="btn btn-xs btn-ghost" onClick={() => undoCopilotTurn(source.id)}>
              <Undo2 size={11} aria-hidden /> 撤销这次生成
            </button>
          )}
        </div>
      </div>
    )
  }

  return (
    <>
      {!empty && (
        <div className="flex shrink-0 items-center gap-2 border-b px-2.5 py-1.5">
          <span className="text-2xs font-semibold">助手</span>
          {memory.turns > 0 && (
            <span className="chip tnum text-2xs"
                  title={`下一条指令会带上最近 ${memory.turns} 轮对话和上一轮的工作流，模型据此理解"刚才那个"指的是什么`}>
              参考前 {memory.turns} 轮
            </span>
          )}
          <span className="flex-1" />
          <IconButton
            label="开始新对话（不影响画布；模型不再参考之前的对话，旧对话保留）"
            onClick={() => void newCopilotConversation()}
          >
            <MessageSquarePlus size={12} aria-hidden />
          </IconButton>
        </div>
      )}

      {empty ? (
        // 没说过话时输入框就是这一栏的主体，竖直居中
        <div className="flex min-h-0 flex-1 flex-col justify-center overflow-y-auto py-6">
          <Composer hero />
        </div>
      ) : (
        <>
          {earlier.length > 0 && (
            <button type="button"
                    className="flex shrink-0 items-center gap-1.5 border-b px-2.5 py-1 text-left text-2xs text-dim transition-colors hover:bg-hover hover:text-fg"
                    aria-expanded={showPast}
                    onClick={() => setShowPast((v) => !v)}>
              <History size={11} aria-hidden />
              {showPast ? '收起之前的对话' : `之前的 ${earlier.length} 轮（模型仍会参考）`}
              <ChevronRight size={10} aria-hidden className="ml-auto transition-transform"
                            style={{ transform: showPast ? 'rotate(90deg)' : 'none' }} />
            </button>
          )}
          <div ref={streamRef} className="min-h-0 flex-1">
            {/* 定位只认这张图上的对话：在前面补出之前的轮次不算换了一批，不再被拽到底 */}
            <AssistantStream turns={shown} dense landing="end" renderTurnActions={actions}
                             resetKey={turns[0]?.id ?? ''}
                             empty={<div className="flex-1" />} />
          </div>
          <Composer hero={false} />
        </>
      )}

      {hasRun && <RunStrip onClick={onOpenRun} />}
    </>
  )
}

/** 自查没修好的问题，逐条带「定位」。问题落在哪个节点上认得出来才给 */
function IssueList({ issues, onLocate }: { issues: CopilotIssue[]; onLocate: (id: string) => void }) {
  return (
    <ul className="space-y-0.5 rounded border px-2 py-1.5 text-2xs leading-relaxed"
        style={{ borderColor: 'color-mix(in srgb, var(--st-waiting) 45%, var(--border))',
                 background: 'var(--st-waiting-soft)' }}>
      {issues.map((x, i) => (
        <li key={i} className="flex items-start gap-1.5">
          <span className="min-w-0 flex-1 [overflow-wrap:anywhere]">
            {x.nodeId && <span className="mono text-dim">「{x.nodeId}」</span>}{x.message}
          </span>
          {x.nodeId && (
            <button type="button"
                    className="inline-flex shrink-0 items-center gap-0.5 rounded px-1 text-dim transition-colors hover:bg-hover hover:text-fg"
                    onClick={() => onLocate(x.nodeId!)}>
              <Crosshair size={10} aria-hidden /> 定位
            </button>
          )}
        </li>
      ))}
    </ul>
  )
}

/**
 * 对话下面那条运行进度。
 *
 * 运行搬去自己那一层之后，这条是它在对话视图里留下的唯一痕迹——没有它，
 * 点了运行再切回对话，这次运行在眼前就彻底没了。状态只读 runPhase：以前这里
 * 看步骤、审批列表、streaming 三样各自猜，等审批时会同时写"正在执行"。
 */
function RunStrip({ onClick }: { onClick: () => void }) {
  const run = useStudio((s) => s.run)
  const events = useStudio((s) => s.events)
  const phase = useStudio((s) => s.runPhase)
  const steps = useMemo(() => (phase === 'running' ? decodeRun(events) : []), [events, phase])
  if (!run) return null

  const last = phase === 'running' ? lastMeaningful(steps) : undefined
  const waiting = phase === 'waiting'
  const alert = phase === 'failed' || phase === 'suspended'

  return (
    <button
      type="button"
      className="flex w-full shrink-0 items-center gap-2 border-t px-2.5 py-1.5 text-left text-2xs transition-colors hover:bg-hover"
      style={waiting ? { background: 'var(--st-waiting-soft)' } : undefined}
      title="看这次运行的完整过程" onClick={onClick}
    >
      {phase === 'idle'
        ? <Play size={10} className="shrink-0 text-faint" fill="currentColor" aria-hidden />
        : <StatusBadge status={phase === 'succeeded' ? 'succeeded' : phase} size={11} decorative />}
      <span className={clsx('min-w-0 flex-1 truncate', !waiting && !alert && 'text-dim')}
            style={waiting ? { color: 'var(--st-waiting)' } : alert ? { color: 'var(--st-failed)' } : undefined}>
        {waiting ? `${statusLabel('waiting')} · 去处理`
          : phase === 'running' ? (last?.title || statusLabel('running'))
          : phase === 'idle' ? '运行'
          : `这次运行${statusLabel(phase)}`}
      </span>
      <ChevronRight size={11} className="shrink-0 text-faint" aria-hidden />
    </button>
  )
}

// -------------------------------------------------------------------------
// 运行：过程和结果，没有输入框
// -------------------------------------------------------------------------

const STREAM_PHASE: Record<RunPhase, StreamTurn['phase']> = {
  idle: 'running', queued: 'running', running: 'running', waiting: 'waiting',
  succeeded: 'done', failed: 'error', cancelled: 'done', suspended: 'done',
}

function RunView({ onBack }: { onBack: () => void }) {
  const run = useStudio((s) => s.run)!
  const events = useStudio((s) => s.events)
  const phase = useStudio((s) => s.runPhase)
  const trace = useStudio((s) => s.trace)
  const attachRun = useStudio((s) => s.attachRun)
  const stopRun = useStudio((s) => s.stopRun)
  const setHoveredNode = useStudio((s) => s.setHoveredNode)
  const focusNode = useStudio((s) => s.focusNode)
  const hoveredNodeId = useStudio((s) => s.hoveredNodeId)
  const approvals = useCatalog((s) => s.approvals)
  const refreshApprovals = useCatalog((s) => s.refreshApprovals)
  // 运行已经收尾了就不再挂卡：审批列表 4 秒才轮询一次，别处批过的卡会在跑完的运行上多挂几秒
  const pending = isSettled(phase) ? []
    : approvals.filter((a) => a.run_id === run.id && a.status === 'pending')
  const [raw, setRaw] = useState(false)
  const [stopping, setStopping] = useState(false)
  const bodyRef = useRef<HTMLDivElement>(null)

  const turns = useMemo<StreamTurn[]>(() => {
    const finished = [...events].reverse().find((e) => e.type === 'run.finished')
    const failedEvent = [...events].reverse().find((e) => e.type === 'run.failed')
    // 结局以 runPhase 为准，连同对账查到的状态一起喂给解码器：服务被强杀、
    // 事件停在半路的运行，右栏的行也得收住，不能一直转
    const steps = decodeRun(events, { status: run.status })
    const output = run.output && Object.keys(run.output).length ? run.output : finished?.data?.output
    return [{
      id: run.id,
      // 运行没有"提问"——它的输入是工具栏那张表单。硬塞一个问题气泡
      // 只会让它看起来像一次对话，而它不是
      question: undefined,
      phase: STREAM_PHASE[phase],
      status: statusLabel(phase === 'idle' ? 'queued' : phase),
      statusCode: phase === 'idle' ? 'queued' : phase,
      tone: phase === 'suspended' ? 'warn' : undefined,
      steps,
      output: phase === 'succeeded' || phase === 'failed' ? output ?? null : null,
      // 原始异常（run.failed.data.detail）收进可展开的技术细节，人话在上面
      error: phase === 'failed'
        ? { error: String(failedEvent?.data?.error ?? run.error ?? '运行失败'),
            ...(failedEvent?.data?.detail ? { detail: String(failedEvent.data.detail) } : {}) }
        : undefined,
      // #runId 已经在栏头上了（可点，去运行记录），卡片里不再重复
      runClass: run.run_class,
      startedAt: trace.startedAt,
      elapsedMs: run.usage?.active_ms ?? run.usage?.duration_ms,
    }]
  }, [run, events, phase, trace.startedAt])

  // 停下来等人的那一刻就去取审批卡。列表 4 秒才轮询一次：头上已经写着「等待审批」，
  // 通过/驳回却要晚几秒才出来
  const hasCard = pending.length > 0
  const [looked, setLooked] = useState<string | null>(null)
  useEffect(() => {
    if (phase !== 'waiting' || hasCard) return
    let alive = true
    void refreshApprovals().finally(() => { if (alive) setLooked(run.id) })
    return () => { alive = false }
  }, [phase, hasCard, run.id, refreshApprovals])

  // 画布上选中节点时不在这里滚动定位：选中即滑入的属性面板整个盖住这一栏，滚了也
  // 看不见，反倒让藏在下面的时间线离开底部、断了跟随——关掉属性面板回来，人看到
  // 的不是原处而是一枚「跳到最新」。联动靠悬停（activeNodeId）和点行取景

  const stop = async () => {
    setStopping(true)
    try {
      await stopRun()
    } catch (e) {
      // 409 多半是它其实早就不在跑了（服务重启过、刚好结束）。store 已经对过账，
      // 这里说清楚发生了什么，不能静默吞掉
      toast.error(e)
    } finally {
      setStopping(false)
    }
  }
  const toApproval = () => {
    const slot = bodyRef.current?.querySelector<HTMLElement>('[data-approval-slot]:not(:empty)')
    const reduced = typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches
    slot?.scrollIntoView({ block: 'center', behavior: reduced ? 'auto' : 'smooth' })
    slot?.querySelector<HTMLElement>('textarea, input, button')?.focus({ preventScroll: true })
  }

  const live = phase === 'running' || phase === 'queued'

  return (
    <div className="sheet-in flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center gap-1.5 border-b px-2 py-1.5">
        <button
          type="button"
          className="flex items-center gap-0.5 rounded-md px-1 py-1 text-dim transition-colors hover:bg-hover hover:text-fg"
          title="回到和助手的对话" onClick={onBack}
        >
          <ChevronLeft size={14} aria-hidden />
          <span className="text-2xs">助手</span>
        </button>
        <span className="mx-0.5 h-3.5 w-px shrink-0" style={{ background: 'var(--border)' }} />
        <span className="text-2xs font-semibold">运行</span>
        <Link to={`/runs/${run.id}`} title="在运行记录中查看"
              className="mono text-2xs text-dim underline-offset-2 hover:text-fg hover:underline">
          {shortId(run.id)}
        </Link>
        <span className="flex-1" />
        {phase === 'waiting' && pending.length > 0 && (
          // 等人时工具栏不给"停止"（后端对停在审批上的运行没有取消路径，点了必然
          // 409），给的是此刻真正该做的事
          <button type="button" className="btn btn-xs" style={{ color: 'var(--st-waiting)' }} onClick={toApproval}>
            <Hand size={11} aria-hidden /> 去审批
          </button>
        )}
        {live && (
          <IconButton label="停止这次运行" variant="danger" disabled={stopping} onClick={() => void stop()}>
            <Square size={11} fill="currentColor" aria-hidden />
          </IconButton>
        )}
        {/* 原始事件是排查用的开发者视图，不该占一个常驻 tab——但也不能删，
            翻译层出问题时它是唯一能对照的东西 */}
        <IconButton
          label={raw ? '回到可读视图' : `看原始事件（${events.length} 条）`}
          className={raw ? 'text-[var(--accent)]' : undefined}
          aria-pressed={raw}
          onClick={() => setRaw((v) => !v)}
        >
          <Code2 size={12} aria-hidden />
        </IconButton>
      </div>

      <div ref={bodyRef} className="min-h-0 flex-1">
        {raw ? <RawEvents /> : (
          <AssistantStream
            turns={turns}
            dense
            clockSkewMs={trace.skewMs ?? 0}
            onStepHover={setHoveredNode}
            onStepFocus={focusNode}
            activeNodeId={hoveredNodeId}
            // 审批卡插在它所属的那一轮里，而不是浮在整栏顶上：
            // 要确认的那件事和确认按钮之间不该隔着整条执行过程
            approvalsFor={() => (pending.length ? (
              <div className="fade-up mt-2 overflow-hidden rounded-lg border"
                   style={{ borderColor: 'var(--warn)' }}>
                {pending.map((a) => (
                  <ApprovalCard key={a.id} approval={a} showWorkflow={false}
                                onResolved={(runId) => attachRun(runId)} />
                ))}
              </div>
            ) : phase === 'waiting' ? (
              <div className="mt-2 rounded-lg border border-dashed px-2.5 py-2 text-2xs text-dim"
                   style={{ borderColor: 'var(--st-waiting)' }}>
                {looked === run.id ? (
                  <>
                    没找到这次运行的待审批卡，可能已经在别处处理过了 ·{' '}
                    <Link to={`/runs/${run.id}`} className="underline underline-offset-2 hover:text-fg">看运行记录</Link>
                  </>
                ) : '正在取审批卡…'}
              </div>
            ) : null)}
          />
        )}
      </div>

      <RunFooter />
    </div>
  )
}

/**
 * 底栏：运行级别、执行时长、等人时长、tokens、成本。
 *
 * 以前读的是 POST /runs 返回的 queued 快照，跑完也是空的。运行中读实时累加的
 * usageLive，前面标「≥」——agent 和协作成员的模型调用以前不发 llm.end，
 * 老后端上这个数会偏少，不能装作是总数；到终态换成后端累计的 run.usage。
 * 执行和等人分开写：审批停了三分钟的运行，混成一个数哪个都不对。
 */
function RunFooter() {
  const run = useStudio((s) => s.run)!
  const phase = useStudio((s) => s.runPhase)
  const trace = useStudio((s) => s.trace)
  const usageLive = useStudio((s) => s.usageLive)
  const settled = isSettled(phase)
  const ticking = !settled && phase !== 'idle' && trace.startedAt != null
  const now = useRunClock(ticking)
  const view = trace.startedAt != null ? project(trace, liveAt(trace, now)) : undefined

  const u = run.usage ?? {}
  const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : undefined)
  const activeMs = settled ? num(u.active_ms) ?? num(u.duration_ms) ?? view?.activeMs : view?.activeMs
  const waitMs = settled ? num(u.wait_ms) ?? view?.waitMs : view?.waitMs
  const finalTokens = num(u.total_tokens)
    ?? (num(u.input_tokens) != null || num(u.output_tokens) != null
      ? (num(u.input_tokens) ?? 0) + (num(u.output_tokens) ?? 0) : undefined)
  const liveTokens = usageLive.tokensIn + usageLive.tokensOut
  const tokens = settled ? finalTokens ?? (liveTokens || undefined) : liveTokens
  const cost = settled ? num(u.cost_usd) ?? (usageLive.costUsd || undefined) : usageLive.costUsd
  const approx = !settled && phase !== 'idle'

  return (
    // 360px 的栏里放得下一行就不折：等人时五样数都在，11px 会把成本挤到第二行
    <div className="tnum flex shrink-0 flex-wrap items-center gap-x-1.5 gap-y-0.5 border-t px-2.5 py-1 text-[10.5px] text-dim"
         aria-label="这次运行的用量">
      <span className="chip" style={run.run_class === 'formal'
        ? { color: 'var(--st-done)', borderColor: 'var(--st-done)' } : undefined}>
        {runClassLabel(run.run_class ?? 'exploratory', run.version)}
      </span>
      <span className="flex-1" />
      <span title="执行时长：各段执行之和，不含等人审批的时间">
        执行 <span className="mono text-fg">
          {activeMs == null ? NONE : ticking ? formatClock(activeMs) : formatDuration(activeMs)}
        </span>
      </span>
      {(waitMs ?? 0) > 0 && (
        <span title="等人审批的总时长" style={phase === 'waiting' ? { color: 'var(--st-waiting)' } : undefined}>
          等人 <span className="mono">{ticking ? formatClock(waitMs!) : formatDuration(waitMs!)}</span>
        </span>
      )}
      <span title={approx ? '运行中实时累加，可能偏少；跑完以后端累计为准' : '后端累计'}>
        <span className="mono">{approx && tokens ? '≥' : ''}{tokens ? formatTokens(tokens, { compact: true }) : `${NONE} tok`}</span>
      </span>
      <span className="mono">{approx && cost ? '≥' : ''}{cost != null && (cost > 0 || settled) ? formatCost(cost) : NONE}</span>
    </div>
  )
}

/** 最后一条有意义的步骤：跳过生命周期行，那才是"此刻在干什么"。 */
export function lastMeaningful(steps: Step[]): Step | undefined {
  const flat: Step[] = []
  const walk = (list: Step[]) => list.forEach((s) => {
    flat.push(s)
    if (s.children) walk(s.children)
  })
  walk(steps)
  return [...flat].reverse().find((s) => s.kind !== 'lifecycle') ?? flat[flat.length - 1]
}

/** 原始事件。翻译层出问题时用来对照，平时收着。和步骤流一样只在贴底时跟随 */
function RawEvents() {
  const events = useStudio((s) => s.events)
  const ref = useRef<HTMLDivElement>(null)
  const pinned = useRef(true)
  useEffect(() => {
    const el = ref.current
    if (el && pinned.current) el.scrollTop = el.scrollHeight
  }, [events.length])

  if (!events.length) {
    return (
      <div className="flex h-full items-center justify-center text-2xs text-dim">
        还没有事件
      </div>
    )
  }
  return (
    <div ref={ref} className="h-full overflow-y-auto p-2"
         onScroll={(e) => {
           const el = e.currentTarget
           pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 64
         }}>
      {events.map((e) => (
        <div key={e.seq} className="mono border-b px-1 py-1 text-[10px] leading-relaxed last:border-0">
          <span className="text-dim">#{e.seq}</span>{' '}
          <span className="text-[var(--accent)]">{e.type}</span>{' '}
          {e.node_id && <span className="text-dim">{e.node_id}</span>}
          <div className="break-all text-dim">{JSON.stringify(e.data).slice(0, 400)}</div>
        </div>
      ))}
    </div>
  )
}
