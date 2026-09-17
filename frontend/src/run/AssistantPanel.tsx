import { useMemo, useRef, useState, useEffect } from 'react'
import { Code2, Eraser } from 'lucide-react'
import clsx from 'clsx'
import { useCatalog } from '../store/catalog'
import { useStudio } from '../store/studio'
import { Spinner } from '../components/ui'
import { AssistantStream, type StreamTurn } from './AssistantStream'
import { Composer } from './Composer'
import { decodeCopilot, decodeRun, formatDuration } from './decode'
import { ApprovalCard } from './RunPanel'

/**
 * 画布右栏：助手。
 *
 * 这一栏现在只有一件事——和 Copilot 说话，以及看它和运行做了什么。属性不再
 * 和它平级挤在两个 tab 里：选中一个节点会滑出属性面板盖在上面，关掉就回到
 * 原处。之前是切 tab，而 tab 是互斥的——跑图跑到一半点开一个节点看配置，
 * 整条执行过程就从眼前消失了，回来还得重新找位置。
 *
 * 两种体量：没说过话时输入框是主体（竖直居中、带例句），有对话之后它退到
 * 底部，上面让给过程。
 */
export function AssistantPanel() {
  const run = useStudio((s) => s.run)
  const events = useStudio((s) => s.events)
  const streaming = useStudio((s) => s.streaming)
  const copilot = useStudio((s) => s.copilot)
  const copilotTurns = useStudio((s) => s.copilotTurns)
  const attachRun = useStudio((s) => s.attachRun)
  const clearCopilot = useStudio((s) => s.clearCopilot)
  const approvals = useCatalog((s) => s.approvals)
  const pending = approvals.filter((a) => a.run_id === run?.id && a.status === 'pending')
  const [raw, setRaw] = useState(false)

  const turns = useMemo<StreamTurn[]>(() => {
    const list: StreamTurn[] = copilotTurns.map((t) => ({
      id: t.id,
      question: t.instruction,
      phase: t.phase === 'running' ? 'running' : t.phase === 'error' ? 'error' : 'done',
      // 生成期间不能只给一个孤零零的转圈图标。具体阶段从 copilot 实时状态里取
      status: t.phase === 'running' ? (copilot.lastOp || '正在生成流程…')
        : t.phase === 'done' ? '流程已更新到画布' : '',
      steps: decodeCopilot(t.ops),
      output: t.explanation ? { 说明: t.explanation } : null,
      error: t.error,
    }))
    if (run) {
      const finished = [...events].reverse().find((e) => e.type === 'run.finished')
      const failedEvent = [...events].reverse().find((e) => e.type === 'run.failed')
      const waiting = pending.length > 0
      list.push({
        id: run.id,
        // 运行的"问题"是输入表单，已经在工具栏那边填过了
        question: undefined,
        phase: failedEvent ? 'error'
          : waiting ? 'waiting'
          : streaming ? 'running'
          : finished ? 'done' : 'running',
        status: streaming ? '正在执行…' : waiting ? '等待你的确认' : finished ? '完成' : '',
        steps: decodeRun(events),
        // run 对象是启动时的快照，成果在 run.finished 事件里
        output: finished?.data?.output ?? run.output ?? null,
        error: failedEvent ? String(failedEvent.data?.error ?? '运行失败') : run.error || undefined,
        runId: run.id,
        runClass: run.run_class,
      })
    }
    return list
  }, [copilotTurns, run, events, streaming, pending.length, copilot.lastOp])

  const empty = !turns.length

  return (
    <div className="flex h-full flex-col">
      {!empty && (
        <div className="flex shrink-0 items-center gap-2 border-b px-2.5 py-1.5">
          <span className="text-[11px] font-semibold">助手</span>
          {streaming && (
            <span className="flex items-center gap-1 text-[10px]" style={{ color: 'var(--accent)' }}>
              <Spinner size={9} /> 执行中
            </span>
          )}
          <span className="flex-1" />
          {run && (
            <span className="mono text-[9.5px] text-faint" title={run.id}>
              #{run.id.slice(0, 6)}
            </span>
          )}
          {/* 原始事件是排查用的开发者视图，不该占一个常驻 tab——但也不能删，
              翻译层出问题时它是唯一能对照的东西 */}
          <button
            className={clsx('rounded p-1 transition-colors hover:bg-hover',
              raw ? 'text-[var(--accent)]' : 'text-faint')}
            title={raw ? '回到助手视图' : `看原始事件（${events.length} 条）`}
            onClick={() => setRaw((v) => !v)}
          >
            <Code2 size={12} />
          </button>
          {!!copilotTurns.length && (
            <button className="rounded p-1 text-faint transition-colors hover:bg-hover"
                    title="清空助手记录（不影响画布）" onClick={clearCopilot}>
              <Eraser size={12} />
            </button>
          )}
        </div>
      )}

      {empty ? (
        // 没说过话时输入框就是这一栏的主体，竖直居中。之前它是个 38px 高的
        // 框挤在最底下，比上面任何一块都不起眼——"没地方引导写问题"说的就是它
        <div className="flex min-h-0 flex-1 flex-col justify-center overflow-y-auto py-6">
          <Composer hero />
        </div>
      ) : (
        <>
          <div className="min-h-0 flex-1">
            {raw ? <RawEvents /> : (
              <AssistantStream
                turns={turns}
                dense
                // 审批卡插在它所属的那一轮里，而不是浮在整栏顶上：
                // "等你确认"和确认的按钮之间隔着整条执行过程，是两件事
                approvalsFor={(t) => (
                  t.runId && pending.length ? (
                    <div className="fade-up mt-2 overflow-hidden rounded-lg border"
                         style={{ borderColor: 'var(--warn)' }}>
                      {pending.map((a) => (
                        <ApprovalCard key={a.id} approval={a}
                                      onResolved={(runId) => attachRun(runId)} />
                      ))}
                    </div>
                  ) : null
                )}
              />
            )}
          </div>
          <Composer hero={false} />
          {run && <RunFooter />}
        </>
      )}
    </div>
  )
}

function RunFooter() {
  const run = useStudio((s) => s.run)!
  return (
    <div className="flex shrink-0 items-center gap-2 border-t px-2.5 py-1 text-[9.5px] text-faint">
      {run.run_class === 'formal'
        ? <span className="chip" style={{ color: 'var(--ok)', borderColor: 'var(--ok)' }}>正式 v{run.version}</span>
        : <span className="chip">探索</span>}
      <span className="flex-1" />
      {!!run.usage?.total_tokens && <span>{run.usage.total_tokens} tok</span>}
      {run.usage?.cost_usd ? <span>${Number(run.usage.cost_usd).toFixed(4)}</span> : null}
      {run.usage?.duration_ms ? <span>{formatDuration(run.usage.duration_ms)}</span> : null}
    </div>
  )
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
