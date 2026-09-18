import { useEffect, useMemo, useRef, useState } from 'react'
import { ChevronLeft, ChevronRight, Code2, Eraser, Hand, Play, Square } from 'lucide-react'
import clsx from 'clsx'
import { useCatalog } from '../store/catalog'
import { useStudio } from '../store/studio'
import { Spinner } from '../components/ui'
import { AssistantStream, type StreamTurn } from './AssistantStream'
import { Composer } from './Composer'
import {
  decodeCopilot, decodeRun, formatDuration, isAwaitingHuman, type Step,
} from './decode'
import { ApprovalCard } from './RunPanel'

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

function ChatView({ hasRun, onOpenRun }: { hasRun: boolean; onOpenRun: () => void }) {
  const copilotTurns = useStudio((s) => s.copilotTurns)
  const copilot = useStudio((s) => s.copilot)
  const clearCopilot = useStudio((s) => s.clearCopilot)

  const turns = useMemo<StreamTurn[]>(() => copilotTurns.map((t) => ({
    id: t.id,
    question: t.instruction,
    phase: t.phase === 'running' ? 'running' : t.phase === 'error' ? 'error' : 'done',
    // 生成期间不能只给一个孤零零的转圈图标，具体阶段从实时状态里取
    status: t.phase === 'running' ? (copilot.lastOp || '正在生成流程…')
      : t.phase === 'done' ? '流程已更新到画布' : '',
    steps: decodeCopilot(t.ops),
    output: t.explanation ? { 说明: t.explanation } : null,
    error: t.error,
  })), [copilotTurns, copilot.lastOp])

  const empty = !turns.length

  return (
    <>
      {!empty && (
        <div className="flex shrink-0 items-center gap-2 border-b px-2.5 py-1.5">
          <span className="text-[11px] font-semibold">Copilot</span>
          <span className="flex-1" />
          <button className="rounded p-1 text-faint transition-colors hover:bg-hover"
                  title="清空对话（不影响画布）" onClick={clearCopilot}>
            <Eraser size={12} />
          </button>
        </div>
      )}

      {empty ? (
        // 没说过话时输入框就是这一栏的主体，竖直居中
        <div className="flex min-h-0 flex-1 flex-col justify-center overflow-y-auto py-6">
          <Composer hero />
        </div>
      ) : (
        <>
          <div className="min-h-0 flex-1">
            <AssistantStream turns={turns} dense />
          </div>
          <Composer hero={false} />
        </>
      )}

      {hasRun && <RunStrip onClick={onOpenRun} />}
    </>
  )
}

/**
 * 对话下面那条运行进度。
 *
 * 运行搬去自己那一层之后，这条是它在对话视图里留下的唯一痕迹——没有它，
 * 点了运行再切回对话，这次运行在眼前就彻底没了。
 */
function RunStrip({ onClick }: { onClick: () => void }) {
  const run = useStudio((s) => s.run)
  const events = useStudio((s) => s.events)
  const streaming = useStudio((s) => s.streaming)
  if (!run) return null

  const steps = decodeRun(events)
  const waiting = isAwaitingHuman(steps)
  const last = lastMeaningful(steps)
  const failed = events.some((e) => e.type === 'run.failed')

  return (
    <button
      className="flex w-full shrink-0 items-center gap-2 border-t px-2.5 py-1.5 text-left text-[10.5px] transition-colors hover:bg-hover"
      style={waiting ? { background: 'color-mix(in srgb, var(--warn) 10%, transparent)' } : undefined}
      title="看这次运行的完整过程" onClick={onClick}
    >
      {waiting ? <Hand size={11} className="shrink-0" style={{ color: 'var(--warn)' }} />
        : streaming ? <Spinner size={10} />
        : <Play size={10} className="shrink-0 text-faint" fill="currentColor" />}
      <span className={clsx('min-w-0 flex-1 truncate',
        waiting && 'text-[var(--warn)]', failed && 'text-[var(--err)]')}>
        {waiting ? '等你确认'
          : failed ? '运行失败'
          : streaming ? (last?.title || '正在执行…')
          : '这次运行已结束'}
      </span>
      <ChevronRight size={11} className="shrink-0 text-faint" />
    </button>
  )
}

// -------------------------------------------------------------------------
// 运行：过程和结果，没有输入框
// -------------------------------------------------------------------------

function RunView({ onBack }: { onBack: () => void }) {
  const run = useStudio((s) => s.run)!
  const events = useStudio((s) => s.events)
  const streaming = useStudio((s) => s.streaming)
  const attachRun = useStudio((s) => s.attachRun)
  const stopRun = useStudio((s) => s.stopRun)
  const approvals = useCatalog((s) => s.approvals)
  const pending = approvals.filter((a) => a.run_id === run.id && a.status === 'pending')
  const [raw, setRaw] = useState(false)

  const turns = useMemo<StreamTurn[]>(() => {
    const finished = [...events].reverse().find((e) => e.type === 'run.finished')
    const failedEvent = [...events].reverse().find((e) => e.type === 'run.failed')
    const steps = decodeRun(events)
    // 从事件推，不查审批列表——那是 4 秒轮询一次的，中断后会有几秒钟
    // 这里说"完成"，而实际上它正等着你点通过
    const waiting = isAwaitingHuman(steps)
    return [{
      id: run.id,
      // 运行没有"提问"——它的输入是工具栏那张表单。硬塞一个问题气泡
      // 只会让它看起来像一次对话，而它不是
      question: undefined,
      phase: failedEvent ? 'error'
        : waiting ? 'waiting'
        : streaming ? 'running'
        : finished ? 'done' : 'running',
      status: streaming ? '正在执行…' : waiting ? '等待你的确认' : finished ? '完成' : '',
      steps,
      // run 对象是启动时的快照，成果在 run.finished 事件里
      output: finished?.data?.output ?? run.output ?? null,
      error: failedEvent ? String(failedEvent.data?.error ?? '运行失败') : run.error || undefined,
      runId: run.id,
      runClass: run.run_class,
    }]
  }, [run, events, streaming])

  return (
    <div className="sheet-in flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center gap-1.5 border-b px-2 py-1.5">
        <button
          className="flex items-center gap-0.5 rounded-md px-1 py-1 text-faint transition-colors hover:bg-hover hover:text-fg"
          title="回到 Copilot 对话" onClick={onBack}
        >
          <ChevronLeft size={14} />
          <span className="text-[10px]">Copilot</span>
        </button>
        <span className="mx-0.5 h-3.5 w-px shrink-0" style={{ background: 'var(--border)' }} />
        <span className="text-[11px] font-semibold">运行</span>
        <span className="mono text-[9.5px] text-faint" title={run.id}>#{run.id.slice(0, 6)}</span>
        <span className="flex-1" />
        {streaming && (
          <button className="rounded p-1 transition-colors hover:bg-hover"
                  style={{ color: 'var(--err)' }} title="停止这次运行"
                  onClick={() => void stopRun()}>
            <Square size={11} fill="currentColor" />
          </button>
        )}
        {/* 原始事件是排查用的开发者视图，不该占一个常驻 tab——但也不能删，
            翻译层出问题时它是唯一能对照的东西 */}
        <button
          className={clsx('rounded p-1 transition-colors hover:bg-hover',
            raw ? 'text-[var(--accent)]' : 'text-faint')}
          title={raw ? '回到可读视图' : `看原始事件（${events.length} 条）`}
          onClick={() => setRaw((v) => !v)}
        >
          <Code2 size={12} />
        </button>
      </div>

      <div className="min-h-0 flex-1">
        {raw ? <RawEvents /> : (
          <AssistantStream
            turns={turns}
            dense
            // 审批卡插在它所属的那一轮里，而不是浮在整栏顶上：
            // 要确认的那件事和确认按钮之间不该隔着整条执行过程
            approvalsFor={() => (pending.length ? (
              <div className="fade-up mt-2 overflow-hidden rounded-lg border"
                   style={{ borderColor: 'var(--warn)' }}>
                {pending.map((a) => (
                  <ApprovalCard key={a.id} approval={a}
                                onResolved={(runId) => attachRun(runId)} />
                ))}
              </div>
            ) : null)}
          />
        )}
      </div>

      <div className="flex shrink-0 items-center gap-2 border-t px-2.5 py-1 text-[9.5px] text-faint">
        {run.run_class === 'formal'
          ? <span className="chip" style={{ color: 'var(--ok)', borderColor: 'var(--ok)' }}>正式 v{run.version}</span>
          : <span className="chip">探索</span>}
        <span className="flex-1" />
        {!!run.usage?.total_tokens && <span>{run.usage.total_tokens} tok</span>}
        {run.usage?.cost_usd ? <span>${Number(run.usage.cost_usd).toFixed(4)}</span> : null}
        {run.usage?.duration_ms ? <span>{formatDuration(run.usage.duration_ms)}</span> : null}
      </div>
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

/** 原始事件。翻译层出问题时用来对照，平时收着。 */
function RawEvents() {
  const events = useStudio((s) => s.events)
  const bottom = useRef<HTMLDivElement>(null)
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: 'end' })
  }, [events.length])

  if (!events.length) {
    return (
      <div className="flex h-full items-center justify-center text-[11px] text-faint">
        还没有事件
      </div>
    )
  }
  return (
    <div className="h-full overflow-y-auto p-2">
      {events.map((e) => (
        <div key={e.seq} className="mono border-b px-1 py-1 text-[9.5px] leading-relaxed last:border-0">
          <span className="text-faint">#{e.seq}</span>{' '}
          <span className="text-[var(--accent)]">{e.type}</span>{' '}
          {e.node_id && <span className="text-dim">{e.node_id}</span>}
          <div className="break-all text-faint">{JSON.stringify(e.data).slice(0, 400)}</div>
        </div>
      ))}
      <div ref={bottom} />
    </div>
  )
}
