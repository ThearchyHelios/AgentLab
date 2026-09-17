import { useEffect, useMemo, useRef, useState } from 'react'
import { Code2, Send, Settings2, Sparkles, Square, Trash2 } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../api/client'
import { useCatalog, modelOptions } from '../store/catalog'
import { useStudio } from '../store/studio'
import { Spinner, useToast } from '../components/ui'
import { AssistantStream, StreamEmpty, type StreamTurn } from './AssistantStream'
import { decodeCopilot, decodeRun, formatDuration } from './decode'
import { ApprovalCard, RunLauncher } from './RunPanel'

/**
 * 画布右栏的助手。
 *
 * 取代原来的「运行 / 属性 → 时间线 / 成果 / 事件」两层嵌套（5 个视图，
 * 每个只装一小块，找什么都得先猜在哪一层）。现在一栏一条流：你让它做什么、
 * 它做了什么、结果是什么，按时间从上往下。
 *
 * 两件事在这里合流——Copilot 建图和跑图。它们在协议上完全无关（SSE 操作流
 * vs 事件 WebSocket，不共享 seq 和 node_id），但对用户是同一件事的两半：
 * "帮我搭一个能算月度毛利的流程" → "跑一下"。decode.ts 把两种流翻译成同一种
 * Step，这里才能把它们排进同一条时间轴。
 *
 * 右栏不能删：它是发起运行和处理审批的唯一入口，RunLauncher 必须留着。
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
      // 生成期间不能只给一个孤零零的转圈图标。具体阶段从 copilot 实时状态
      // 里取——它只对当前这一轮有效，历史轮次用固定文案
      status: t.phase === 'running'
        ? (copilot.lastOp || PHASE_TEXT[copilot.phase] || '正在生成流程…')
        : t.phase === 'done' ? '流程已更新到画布' : '',
      steps: decodeCopilot(t.ops),
      output: t.explanation ? { 说明: t.explanation } : null,
      error: t.error,
    }))
    if (run) {
      const steps = decodeRun(events)
      // run 对象是启动时的快照，成果在 run.finished 事件里
      const finished = [...events].reverse().find((e) => e.type === 'run.finished')
      const failedEvent = [...events].reverse().find((e) => e.type === 'run.failed')
      const waiting = pending.length > 0
      list.push({
        id: run.id,
        question: undefined,   // 运行的"问题"是输入表单，已经在上面填过了
        phase: failedEvent ? 'error'
          : waiting ? 'waiting'
          : streaming ? 'running'
          : finished ? 'done' : 'running',
        status: streaming ? '正在执行…' : waiting ? '等待你的确认' : finished ? '完成' : '',
        steps,
        output: finished?.data?.output ?? run.output ?? null,
        error: failedEvent ? String(failedEvent.data?.error ?? '运行失败') : run.error || undefined,
        runId: run.id,
        runClass: run.run_class,
      })
    }
    return list
  }, [copilotTurns, run, events, streaming, pending.length, copilot.lastOp, copilot.phase])

  return (
    <div className="flex h-full flex-col">
      <RunLauncher />

      {pending.map((approval) => (
        <ApprovalCard key={approval.id} approval={approval}
                      onResolved={(runId) => attachRun(runId)} />
      ))}

      <div className="min-h-0 flex-1">
        {raw ? <RawEvents /> : (
          <AssistantStream
            turns={turns}
            dense
            empty={<PanelEmpty />}
          />
        )}
      </div>

      <Composer />

      <div className="flex items-center gap-2 border-t px-2.5 py-1 text-[10px] text-faint">
        {run ? (
          <>
            <span className="mono truncate" title={run.id}>#{run.id.slice(0, 8)}</span>
            {run.run_class === 'formal'
              ? <span className="chip" style={{ color: 'var(--ok)', borderColor: 'var(--ok)' }}>正式 v{run.version}</span>
              : <span className="chip">探索</span>}
            {streaming && <span className="text-[var(--accent)]">● 实时</span>}
            <span className="flex-1" />
            {!!run.usage?.total_tokens && <span>{run.usage.total_tokens} tok</span>}
            {run.usage?.cost_usd ? <span>${Number(run.usage.cost_usd).toFixed(4)}</span> : null}
            {run.usage?.duration_ms ? <span>{formatDuration(run.usage.duration_ms)}</span> : null}
          </>
        ) : <span className="flex-1" />}
        {!!copilotTurns.length && !raw && (
          <button className="hover:text-dim" title="清空助手记录（不影响画布）"
                  onClick={clearCopilot}>
            <Trash2 size={11} />
          </button>
        )}
        {/* 原始事件是排查用的开发者视图，不该占一个常驻 tab——但也不能删，
            翻译层出问题时它是唯一能对照的东西 */}
        <button
          className={clsx('flex items-center gap-1 hover:text-dim', raw && 'text-[var(--accent)]')}
          title={raw ? '回到助手视图' : `看原始事件（${events.length} 条）`}
          onClick={() => setRaw((v) => !v)}
        >
          <Code2 size={11} />
        </button>
      </div>
    </div>
  )
}

function PanelEmpty() {
  const nodes = useStudio((s) => s.nodes)
  return (
    <StreamEmpty
      icon={<Sparkles size={20} />}
      title={nodes.length ? '还没有动静' : '从一句话开始'}
      hint={nodes.length
        ? '上面填好输入点「试运行」，或者在下面告诉它要改什么'
        : '在下面描述你要的流程，它会直接画到左边的画布上'}
    />
  )
}

// -------------------------------------------------------------------------

const EXAMPLES = [
  '读取用户上传的问题，先查知识库，查到就基于资料回答并标注出处，查不到就联网搜索',
  '把一段长文本拆成要点，逐条用模型打分，低分的让模型重写一次，最后汇总成表格',
  '写代码分析数据，在沙箱里跑，出错就把报错喂回去让模型修，最多修三次',
]

/**
 * Copilot 输入框。
 *
 * 原来是个 Modal：点按钮 → 弹窗 → 输入 → 关闭弹窗 → 盯着画布上的浮条看。
 * 每改一次图都要重走一遍，而且弹窗关掉之后需求文本就没了，想微调只能重打。
 * 放进侧栏之后它就是个普通输入框，说完一句接着说下一句。
 */
function Composer() {
  const nodes = useStudio((s) => s.nodes)
  const copilot = useStudio((s) => s.copilot)
  const { runCopilot, stopCopilot, retryCopilot } = useStudio()
  const providers = useCatalog((s) => s.providers)
  const [text, setText] = useState('')
  const [opts, setOpts] = useState(false)
  const [model, setModel] = useState('')
  const [effective, setEffective] = useState('')
  const [useBase, setUseBase] = useState(true)
  const ref = useRef<HTMLTextAreaElement>(null)

  // Copilot 的模型是独立设置：它写的是编排本身，值得和工作流节点分开选
  useEffect(() => {
    void api.copilot.getModel().then((m) => {
      setModel(m.model ?? '')
      setEffective(m.effective_model ?? '')
    }).catch(() => undefined)
  }, [])

  // 工具栏的 Copilot 按钮把焦点甩过来
  useEffect(() => {
    const focus = () => ref.current?.focus()
    window.addEventListener('agentlab:focus-copilot', focus)
    return () => window.removeEventListener('agentlab:focus-copilot', focus)
  }, [])

  const pickModel = (value: string) => {
    setModel(value)
    void api.copilot.setModel({ model: value || null })
      .then((m) => setEffective(m.effective_model ?? ''))
      .catch(() => undefined)
  }

  const send = () => {
    const q = text.trim()
    if (!q || copilot.active) return
    runCopilot(q, useBase && nodes.length > 0, model || undefined)
    setText('')
  }

  const options = modelOptions(providers)
  const groups = [...new Set(options.map((o) => o.group))]

  return (
    <div className="shrink-0 border-t">
      {copilot.error && (
        <div className="flex items-center gap-2 border-b px-2.5 py-1.5 text-[10.5px]"
             style={{ color: 'var(--err)' }}>
          <span className="min-w-0 flex-1">{copilot.error}</span>
          {/* 失败多半跟需求本身无关（模型抽风、协议跑偏、网断了），
              不该逼用户把需求再敲一遍 */}
          {copilot.lastInstruction && (
            <button className="btn btn-sm shrink-0" onClick={retryCopilot}>重试</button>
          )}
        </div>
      )}

      {!nodes.length && !copilot.active && (
        <div className="flex flex-wrap gap-1 border-b px-2.5 py-1.5">
          {EXAMPLES.map((ex) => (
            <button key={ex} className="chip text-left hover:border-[var(--accent)]"
                    title={ex} onClick={() => { setText(ex); ref.current?.focus() }}>
              {ex.slice(0, 16)}…
            </button>
          ))}
        </div>
      )}

      {opts && (
        <div className="space-y-2 border-b px-2.5 py-2">
          <div>
            <label className="label">Copilot 用哪个模型</label>
            <select className="field" value={model} onChange={(e) => pickModel(e.target.value)}>
              <option value="">跟随默认{effective ? `（当前：${effective}）` : ''}</option>
              {groups.map((g) => (
                <optgroup key={g} label={g}>
                  {options.filter((o) => o.group === g).map((o) => (
                    <option key={g + o.value} value={o.value}>{o.label}</option>
                  ))}
                </optgroup>
              ))}
            </select>
            <div className="mt-1 text-[10px] leading-snug text-faint">
              只影响 Copilot 自己，不改节点上的模型。它要按协议逐行输出操作，
              指令遵循弱的模型生成不出东西。
            </div>
          </div>
          {!!nodes.length && (
            <label className="flex items-center gap-2 text-[11px]">
              <input type="checkbox" checked={useBase} className="accent-[var(--accent)]"
                     onChange={(e) => setUseBase(e.target.checked)} />
              在当前这张图上改（不勾就重新生成）
            </label>
          )}
        </div>
      )}

      <div className="flex items-end gap-1.5 p-2">
        <button className={clsx('btn btn-sm btn-ghost shrink-0', opts && 'text-[var(--accent)]')}
                title="模型和生成方式" onClick={() => setOpts((v) => !v)}>
          <Settings2 size={12} />
        </button>
        <textarea
          ref={ref}
          className="field min-h-[38px] flex-1 resize-none py-1.5 text-[11.5px]"
          rows={2}
          value={text}
          placeholder={nodes.length ? '告诉它要改什么…' : '描述你要的流程…'}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
          }}
        />
        {copilot.active ? (
          <button className="btn btn-sm btn-danger shrink-0" onClick={stopCopilot} title="取消生成">
            <Square size={11} />
          </button>
        ) : (
          <button className="btn btn-sm btn-primary shrink-0" onClick={send}
                  disabled={!text.trim()} title="生成到画布（Enter）">
            <Send size={11} />
          </button>
        )}
      </div>

      {copilot.active && (
        <div className="flex items-center gap-1.5 border-t px-2.5 py-1 text-[10px] text-faint">
          <Spinner size={10} />
          <span className="min-w-0 flex-1 truncate">
            {copilot.lastOp || PHASE_TEXT[copilot.phase] || '正在起草…'}
          </span>
          {copilot.model && <span className="truncate">{copilot.model}</span>}
          {copilot.elapsedMs > 0 && (
            <span className="mono">{Math.round(copilot.elapsedMs / 1000)}s</span>
          )}
        </div>
      )}
      {!copilot.active && !!copilot.explanation && (
        <div className="border-t px-2.5 py-1 text-[10px] text-faint">
          结果不会自动保存，检查无误记得 ⌘S
        </div>
      )}
    </div>
  )
}

// 从提交到第一个节点落地中间有 5~30 秒。阶段会变本身就是"它还活着"的信号，
// 恒定的"正在起草…"让人分不清是在想还是已经卡死
const PHASE_TEXT: Record<string, string> = {
  connecting: '正在连接模型…',
  planning: '正在理解需求、规划结构…',
  building: '正在放置节点…',
  wiring: '正在连接数据流…',
  finalizing: '正在排版和校验…',
}

// -------------------------------------------------------------------------

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
