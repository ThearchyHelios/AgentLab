import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Ban, ChevronLeft, ChevronRight, Code2, Crosshair, Hand, History, MessageSquarePlus, Play, Square,
  Undo2, Wand2, Wrench,
} from 'lucide-react'
import clsx from 'clsx'
import { useCatalog } from '../store/catalog'
import { useStudio, type CopilotTurn } from '../store/studio'
import { revealField, useSheet } from '../canvas/InspectorSheet'
import { fieldOfIssue } from '../canvas/issues'
import { sourceHandles } from '../canvas/nodeDefs'
import { splitShrunk } from '../canvas/copilotMerge'
import { confirmDialog, IconButton, StatusBadge, toast } from '../components/ui'
import { formatCost, formatLapse, formatOffset, formatSpan, formatTokens, shortId, NONE } from '../lib/format'
import { statusLabel } from '../lib/status'
import { runClassLabel } from '../lib/terms'
import type { RunUsage, ToolChange } from '../types'
import { AssistantStream, type StreamTurn } from './AssistantStream'
import { Composer } from './Composer'
import {
  copilotOutcome, decodeCopilot, decodeRun, exitLabels, issueLine, SELF_CHECK_ROUNDS, type CopilotIssue, type CopilotOutcome,
} from './decode'
import { ApprovalCard } from './RunPanel'
import { useRunGlance } from './RunHud'
import { isSettled, lastEventAt, liveAt, project, type RunPhase, type Trace } from './trace'
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
  const [reveal, setReveal] = useState<Reveal | null>(null)
  // 请求办完就清掉（onRevealed），seq 不能跟着归零：运行层那头记着办过的 seq，归零后下一个
  // 请求会撞上旧号被当成办过了
  const seq = useRef(0)
  const rootRef = useRef<HTMLDivElement>(null)

  // 发起一次新运行就把这一层推到前面——那是此刻唯一在发生的事
  useEffect(() => {
    if (run?.id) setView('run')
    setReveal(null)
  }, [run?.id])

  // 画布上的胶囊「去审批」、成果节点的出具印章、点一个跑过的节点，要把人带到右栏的审批卡 /
  // 出具横幅 / 那个节点的步骤。右栏停在对话层时这些根本不在页面上，以前只能退回一句 toast
  // 或者干脆没反应——所以由右栏自己听、自己切过去，并 preventDefault 告诉派发方「接手了」。
  // 右栏收起时它还挂着（只是藏起来），那时不接：切过去也没人看得见，让派发方自己兜底
  useEffect(() => {
    const listen = (kind: Reveal['kind']) => (e: Event) => {
      const detail = (e as CustomEvent<{ runId?: string; nodeId?: string }>).detail
      const s = useStudio.getState()
      const root = rootRef.current
      if (!s.run || (detail?.runId && detail.runId !== s.run.id)) return
      if (kind === 'step' && !detail?.nodeId) return
      if (!root || !root.offsetWidth || getComputedStyle(root).visibility !== 'visible') return
      e.preventDefault()
      // 属性面板盖在右栏上：先让开，要看的东西在它下面。点跑过的节点来的只收起面板、
      // 不清选中：画布上「…的配置」入口跟着选中走，派发方先选中再派发时清掉就没了
      if (kind === 'step') useSheet.setState({ open: null })
      else s.select(null)
      setView('run')
      setReveal({ kind, seq: ++seq.current, ...(kind === 'step' ? { nodeId: detail!.nodeId } : {}) })
    }
    const approval = listen('approval')
    const issuance = listen('issuance')
    const step = listen('step')
    window.addEventListener('agentlab:goto-approval', approval)
    window.addEventListener('agentlab:goto-issuance', issuance)
    window.addEventListener('agentlab:reveal-step', step)
    return () => {
      window.removeEventListener('agentlab:goto-approval', approval)
      window.removeEventListener('agentlab:goto-issuance', issuance)
      window.removeEventListener('agentlab:reveal-step', step)
    }
  }, [])

  return (
    // data-assistant-panel：画布上的出具印章据此判断「点了有人接」，右栏在对话层时它照样能点
    <div ref={rootRef} data-assistant-panel="" className="relative flex h-full min-h-0 flex-col">
      {view === 'run' && run
        ? <RunView onBack={() => setView('chat')} reveal={reveal} onRevealed={() => setReveal(null)} />
        : <ChatView hasRun={!!run} onOpenRun={() => setView('run')} />}
    </div>
  )
}

/**
 * 别处请右栏摆到眼前的东西：审批卡、出具横幅、某个节点最后一次执行的步骤。seq 让同一个
 * 请求连着来两次也会再做一遍。办完就清掉：请求要是留着，回到对话层再点运行条打开运行层
 * 时它会再办一遍——又滚走、又抢焦点、又描一次边
 */
interface Reveal {
  kind: 'approval' | 'issuance' | 'step'
  seq: number
  nodeId?: string
}

// -------------------------------------------------------------------------
// 对话：和 Copilot 说话的地方
// -------------------------------------------------------------------------

const PHASE_TEXT: Record<string, string> = {
  connecting: '正在连接模型',
  planning: '正在理解需求、规划步骤',
  building: '正在搭建工作流',
  wiring: '正在连接数据流',
  finalizing: '正在排版和校验',
  repairing: '正在按自查结果修正',
}

/**
 * 一轮 Copilot 的卡片标题。只看 phase 的话，只回了一句话、自查没修好、少放了
 * 一步，全都写「流程已更新到画布」——而这几种的下一步完全不同
 */
function copilotTurn(
  t: CopilotTurn, live: { lastOp: string; phase: string; repairing?: number },
  labelOf: (id: string) => string | undefined,
): StreamTurn {
  const running = t.phase === 'running'
  const out = copilotOutcome(t.ops, running)
  const found = t.ops.find((o) => o.op === 'check' && o.status === 'repairing')
  const foundN = Array.isArray(found?.issues) ? found!.issues.length : 0
  const reply = out.reply ?? t.reply
  const errorOp = out.error
  const unchanged = t.outcome === 'unchanged'
  const steps = decodeCopilot(t.ops, { context: 'canvas', unchanged, labelOf })
  // 模型改提示词时漏写了 tools、把节点的工具抹掉了：图照样能跑，只是什么都查不到。
  // 按要求删的不算：只有这一轮没让删的才在卡头上提醒
  const { unasked } = toolLossOf(out)
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
      status = unchanged
        ? `画布没有改动，但还有 ${out.check.issues.length} 处问题要你处理`
        : `已放到画布，但还有 ${out.check.issues.length} 处问题要你处理`
      tone = 'warn'
    } else if (out.skipped.length) {
      // 模型只加了认不出类型的节点、全被跳过时画布一处没变：说「已放到画布」是在报一个没发生的改动
      status = unchanged
        ? `画布没有改动：有 ${out.skipped.length} 步没放上`
        : `已放到画布，但有 ${out.skipped.length} 步没放上`
      tone = 'warn'
    } else if (unasked.length) {
      // 360px 的卡头放不下长句：要紧的「没让删」放前面，逐项说明在下面的工具清单里
      status = `${updated} · 没让删却少了 ${unasked.length} 处工具`
      tone = 'warn'
    } else if (unchanged) {
      // 收到了 final，但一处都没变：说「已更新画布」是在报一个没发生的改动
      status = '看过了，画布没有需要改的地方'
    } else if (out.check?.status === 'passed' && out.check.repaired) {
      // 修了几处只有「交回去改」那一条带着数；没有它（一次就修好）时不硬凑「几 处」
      status = `${updated} · ${foundN ? `自查发现 ${foundN} 处问题，已自动修好` : '自查发现的问题已自动修好'}`
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

/** 这一轮变少了的工具，分「没让删」和「按要求」两组：和改图回执（toast）同一条规则 */
const toolLossOf = (out: CopilotOutcome) =>
  splitShrunk(out.toolChanges, out.dropped.map((w) => ({ node_id: w.nodeId, field: w.field })))

/**
 * 画布上的节点名。自查问题、「去掉了」这些行只带节点 id；不订阅 nodes（拖一帧就要重解码
 * 整条对话），取的是解码那一刻的名字
 */
const canvasLabel = (id: string): string | undefined =>
  useStudio.getState().nodes.find((n) => n.id === id)?.data.label || undefined

function ChatView({ hasRun, onOpenRun }: { hasRun: boolean; onOpenRun: () => void }) {
  const copilotTurns = useStudio((s) => s.copilotTurns)
  const copilot = useStudio((s) => s.copilot)
  const retryCopilot = useStudio((s) => s.retryCopilot)
  const repairWithCopilot = useStudio((s) => s.repairWithCopilot)
  const undoCopilotTurn = useStudio((s) => s.undoCopilotTurn)
  const newCopilotConversation = useStudio((s) => s.newCopilotConversation)
  const memory = useStudio((s) => s.copilotMemory)
  const [showPast, setShowPast] = useState(false)

  const turns = useMemo<StreamTurn[]>(() => copilotTurns.map((t) => copilotTurn(t, copilot, canvasLabel)),
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

  /**
   * 定位一条自查问题：和问题面板同一个落点、同一套推断（绑工具的问题落到工具选择器上）。
   * 知道落在哪一项配置上（循环条件、第 2 个成员的工具）就打开检查器、翻到那一栏并把光标
   * 放进去；落不到的只打开、取景
   */
  const locateIssue = (x: CopilotIssue) => {
    if (!x.nodeId) return
    const node = useStudio.getState().nodes.find((n) => n.id === x.nodeId)
    const at = node ? fieldOfIssue({ level: 'error', node_id: x.nodeId, message: x.message, field: x.field }, node) : null
    revealField(x.nodeId, at, { focus: !!at })
  }
  /** 被去掉的工具：光标落在那个节点（或成员）工具选择器的「添加」上，回车就能加回去 */
  const locateTools = (c: ToolChange) => revealField(c.node_id, c.field, { focus: true })

  /** 一轮之后的动作：定位问题节点、让助手再修、撤销这次生成、重试 */
  const actions = (turn: StreamTurn, i: number) => {
    if (i !== shown.length - 1 || turn.phase === 'running') return null
    const source = copilotTurns.find((t) => t.id === turn.id)
    if (!source) return null
    const out = copilotOutcome(source.ops, false, canvasLabel)
    if (turn.phase === 'error') {
      return (
        <button type="button" className="btn btn-xs" onClick={() => retryCopilot()}>
          <Wand2 size={11} aria-hidden /> 用同一句话重试
        </button>
      )
    }
    const issues = out.check?.status === 'failed' ? out.check.issues : []
    const { unasked, asked } = toolLossOf(out)
    // 画布一处没变（unchanged）就没有可撤的：给了按钮，撤掉的是这一轮之前的那一步
    const undo = source.outcome === 'applied'
      || (out.kind === 'built' && source.outcome !== 'reverted' && source.outcome !== 'unchanged')
    if (!issues.length && !out.skipped.length && !unasked.length && !asked.length) {
      return undo ? (
        <button type="button" className="btn btn-xs btn-ghost" onClick={() => undoCopilotTurn(source.id)}>
          <Undo2 size={11} aria-hidden /> 撤销这次生成
        </button>
      ) : null
    }
    // 只是工具被抹掉了：让助手再修不管用（自查是通过的），撤回这一轮才是一步到位的补救
    const repairable = issues.length > 0 || out.skipped.length > 0
    return (
      <div className="w-full space-y-1.5">
        {/* 没修好的问题直接摊开，不折叠：这是此刻唯一要做的事 */}
        {issues.length > 0 && <IssueList issues={issues} onLocate={locateIssue} />}
        {(unasked.length > 0 || asked.length > 0) && <ToolLossList unasked={unasked} asked={asked} onLocate={locateTools} />}
        <div className="flex flex-wrap items-center gap-1.5">
          {repairable && (
            <button type="button" className="btn btn-xs btn-primary"
                    onClick={() => repairWithCopilot(source.id)}>
              <Wand2 size={11} aria-hidden /> {issues.length ? '让助手再修' : '让助手补上'}
            </button>
          )}
          {undo && (
            // 只有按要求删的：这一轮做的就是人要的，撤销退成次要
            <button type="button" className={clsx('btn btn-xs', repairable || !unasked.length ? 'btn-ghost' : 'btn-primary')}
                    onClick={() => undoCopilotTurn(source.id)}>
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

/** 自查没修好的问题，逐条带「定位」。问题落在哪个节点上认得出来才给；写节点名，认不出才写 id */
function IssueList({ issues, onLocate }: { issues: CopilotIssue[]; onLocate: (x: CopilotIssue) => void }) {
  return (
    <ul data-issue-list="" className="space-y-0.5 rounded border px-2 py-1.5 text-2xs leading-relaxed"
        style={{ borderColor: 'color-mix(in srgb, var(--st-waiting) 45%, var(--border))',
                 background: 'var(--st-waiting-soft)' }}>
      {issues.map((x, i) => (
        <li key={i} className="flex items-start gap-1.5" data-issue-node={x.nodeId} data-issue-field={x.field}>
          <span className="min-w-0 flex-1 [overflow-wrap:anywhere]" title={issueLine(x)}>
            {x.nodeId && <span className={clsx('text-dim', !x.label && 'mono')}>「{x.label ?? x.nodeId}」</span>}{x.message}
          </span>
          {x.nodeId && (
            <button type="button"
                    className="inline-flex shrink-0 items-center gap-0.5 rounded px-1 text-dim transition-colors hover:bg-hover hover:text-fg"
                    title={x.field ? '打开这个节点的设置，翻到出问题的那一项' : '在画布上定位这个节点'}
                    onClick={() => onLocate(x)}>
              <Crosshair size={10} aria-hidden /> 定位
            </button>
          )}
        </li>
      ))}
    </ul>
  )
}

/**
 * 这一轮被去掉的工具，逐个节点列出来：去掉了什么、还剩什么。「修改 1」说不出节点的
 * 查库工具被整个抹掉了，要到运行结果不对才会发现。分两组，和改图回执一样：这一轮没让删的
 * 在前、提醒色；按要求删的另列一组、不催——混在一起，按要求删的也像是改漏了，要紧的那句被冲淡
 */
function ToolLossList({ unasked, asked, onLocate }: {
  unasked: ToolChange[]; asked: ToolChange[]; onLocate: (c: ToolChange) => void
}) {
  const group = (kind: 'unasked' | 'asked', changes: ToolChange[]) => {
    const warn = kind === 'unasked'
    return (
      <div data-tool-loss={kind} className="space-y-0.5 rounded border px-2 py-1.5 text-2xs leading-relaxed"
           style={warn ? { borderColor: 'color-mix(in srgb, var(--st-waiting) 45%, var(--border))', background: 'var(--st-waiting-soft)' }
             : undefined}>
        <div className={warn ? 'font-medium text-fg' : 'text-dim'}>
          {warn ? '这一轮没让删，确认一下是不是改漏了：' : '按要求去掉的：'}
        </div>
        <ul className="space-y-0.5">
          {changes.map((c) => (
            <li key={`${c.node_id}|${c.member ?? ''}|${c.field}`} className="flex items-start gap-1.5">
              <Wrench size={10} className="mt-[3px] shrink-0" style={{ color: warn ? 'var(--st-waiting)' : 'var(--text-faint)' }} aria-hidden />
              <span className="min-w-0 flex-1 [overflow-wrap:anywhere]">
                「{c.label}」{c.member ? `的成员「${c.member}」` : ''}去掉了{' '}
                <span className="mono">{c.removed.join('、')}</span>
                {c.after.length ? `，还剩 ${c.after.join('、')}` : '，现在一个工具都没有'}
              </span>
              {c.node_id && (
                <button type="button"
                        className="inline-flex shrink-0 items-center gap-0.5 rounded px-1 text-dim transition-colors hover:bg-hover hover:text-fg"
                        title="打开这个节点的设置，光标放在工具那一栏的「添加」上"
                        onClick={() => onLocate(c)}>
                  <Crosshair size={10} aria-hidden /> 定位
                </button>
              )}
            </li>
          ))}
        </ul>
      </div>
    )
  }
  return (
    <div data-tool-changes="" className="space-y-1">
      {unasked.length > 0 && group('unasked', unasked)}
      {asked.length > 0 && group('asked', asked)}
    </div>
  )
}

/**
 * 对话下面那条运行进度。
 *
 * 运行搬去自己那一层之后，这条是它在对话视图里留下的唯一痕迹——没有它，
 * 点了运行再切回对话，这次运行在眼前就彻底没了。相位、那一句话和计时读
 * useRunGlance，和画布上的胶囊同一份：以前这里自己解码步骤、自己起名，同一时刻
 * 胶囊写「停在『主管审批』」、这里写「等待审批」，计时也各走各的。
 */
function RunStrip({ onClick }: { onClick: () => void }) {
  const run = useStudio((s) => s.run)
  const g = useRunGlance(true)
  if (!run) return null

  const phase = g.phase
  const waiting = phase === 'waiting'
  const alert = phase === 'failed' || phase === 'suspended'
  const ticking = phase === 'running' || phase === 'queued'

  return (
    <button
      type="button"
      data-run-strip={phase}
      className="flex w-full shrink-0 items-center gap-2 border-t px-2.5 py-1.5 text-left text-2xs transition-colors hover:bg-hover"
      style={waiting ? { background: 'var(--st-waiting-soft)' } : undefined}
      title="看这次运行的完整过程" onClick={onClick}
    >
      {phase === 'idle'
        ? <Play size={10} className="shrink-0 text-faint" fill="currentColor" aria-hidden />
        : <StatusBadge status={phase} size={11} decorative />}
      <span className="min-w-0 flex-1 truncate">
        <span className={clsx('font-medium', !waiting && !alert && 'text-fg')}
              style={waiting ? { color: 'var(--st-waiting)' } : alert ? { color: 'var(--st-failed)' } : undefined}>
          {phase === 'idle' ? '运行' : g.label}
        </span>
        {g.headline && phase !== 'idle' && <span className="text-dim"> · {g.headline}</span>}
        {waiting && <span style={{ color: 'var(--st-waiting)' }}> · 去处理</span>}
      </span>
      {ticking && (
        // 从开始算的时刻，和画布上胶囊的读数同一种写法：跨天写「9 天 00:38:26」
        <span className="mono tnum shrink-0" style={{ color: 'var(--st-running)' }}>{formatOffset(g.elapsedMs)}</span>
      )}
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

const finite = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : undefined)

/**
 * 停下来之后「执行」写多久。新后端的 usage.active_ms 是各段执行之和；老数据没有它，而
 * duration_ms 只记了审批恢复后的最后一段——同屏的胶囊、航迹按事件算出一分多钟，栏头和
 * 底栏写「22 ms」。所以退到航迹（和航迹坞同一个投影），连事件都没有才用 duration_ms
 */
function stoppedActiveMs(usage: RunUsage | undefined, trace: Trace): number | undefined {
  return finite(usage?.active_ms)
    ?? (trace.startedAt != null ? project(trace, lastEventAt(trace)).activeMs : undefined)
    ?? finite(usage?.duration_ms)
}

/**
 * 画布上分支、循环出口的说明。签名只取这两类节点的出口配置：拖动、选中节点都会换掉
 * nodes 数组，不能因此把整条时间线重新解码一遍
 */
function useExitLabels() {
  const sig = useStudio((s) => JSON.stringify(s.nodes
    .filter((n) => n.data.nodeType === 'branch' || n.data.nodeType === 'loop')
    .map((n) => [n.id, n.data.nodeType, n.data.nodeType === 'branch' ? n.data.config?.cases ?? [] : null])))
  return useMemo(() => exitLabels(
    (JSON.parse(sig) as [string, string, unknown][]).map(([id, type, cases]) => ({ id, type, config: { cases } })),
    sourceHandles,
  ), [sig])
}

function RunView({ onBack, reveal, onRevealed }: {
  onBack: () => void; reveal: Reveal | null; onRevealed: () => void
}) {
  const run = useStudio((s) => s.run)!
  const events = useStudio((s) => s.events)
  const phase = useStudio((s) => s.runPhase)
  const trace = useStudio((s) => s.trace)
  const attachRun = useStudio((s) => s.attachRun)
  const stopRun = useStudio((s) => s.stopRun)
  const setHoveredNode = useStudio((s) => s.setHoveredNode)
  const focusNode = useStudio((s) => s.focusNode)
  const select = useStudio((s) => s.select)
  const hoveredNodeId = useStudio((s) => s.hoveredNodeId)
  const approvals = useCatalog((s) => s.approvals)
  const refreshApprovals = useCatalog((s) => s.refreshApprovals)
  // 运行已经收尾了就不再挂卡：审批列表 4 秒才轮询一次，别处批过的卡会在跑完的运行上多挂几秒
  const pending = isSettled(phase) ? []
    : approvals.filter((a) => a.run_id === run.id && a.status === 'pending')
  const [raw, setRaw] = useState(false)
  const [stopping, setStopping] = useState(false)
  const bodyRef = useRef<HTMLDivElement>(null)
  const exitLabelOf = useExitLabels()

  const turns = useMemo<StreamTurn[]>(() => {
    const finished = [...events].reverse().find((e) => e.type === 'run.finished')
    const failedEvent = [...events].reverse().find((e) => e.type === 'run.failed')
    // 结局以 runPhase 为准，连同对账查到的状态一起喂给解码器：服务被强杀、
    // 事件停在半路的运行，右栏的行也得收住，不能一直转
    const steps = decodeRun(events, { status: run.status }, { exitLabelOf })
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
      // #runId 已经在栏头上了（可点，去运行记录），卡片里不再重复；成果里的报告取证据链照样要它
      runClass: run.run_class,
      outputRun: run.id,
      startedAt: trace.startedAt,
      // 栏头只在不跑的时候写它；跑着的时候耗时细条按各步自己的耗时比
      elapsedMs: phase === 'waiting' || isSettled(phase) ? stoppedActiveMs(run.usage, trace) : undefined,
    }]
  }, [run, events, phase, trace, exitLabelOf])

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
  /**
   * 放弃停在审批上（或服务重启时挂起）的运行。它已经不在执行，没有「停止」可言；后端
   * 把它直接收成已取消，待审批一并关闭。关掉的审批找不回来，所以先问一句——和画布
   * 胶囊上的「放弃」同一套说法
   */
  const abandon = async () => {
    const ok = await confirmDialog({
      title: '放弃这次运行？',
      body: phase === 'suspended'
        ? '运行在服务重启时挂起了。放弃之后它记为已取消，不能再从断点接着跑。'
        : '运行停在审批上。放弃之后它记为已取消，不能再接着跑。',
      consequences: [
        ...(pending.length ? [`这次运行的 ${pending.length} 条待审批一并关闭，留痕里写上是谁放弃的`] : []),
        '已经跑完的节点结果照样留在记录里',
      ],
      confirmLabel: '放弃这次运行',
      danger: true,
    })
    if (ok) await stop()
  }
  const reducedMotion = () => typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches
  const toApproval = () => {
    const slot = bodyRef.current?.querySelector<HTMLElement>('[data-approval-slot]:not(:empty)')
    if (!slot) return
    const card = slot.querySelector<HTMLElement>('[data-approval]') ?? slot
    card.scrollIntoView({ block: 'center', behavior: reducedMotion() ? 'auto' : 'smooth' })
    card.querySelector<HTMLElement>('textarea, input, button')?.focus({ preventScroll: true })
    // 和胶囊找到卡片时同一个描边：人从画布那头被带过来，得知道「是这张」
    card.classList.add('sf-flash')
    setTimeout(() => card.classList.remove('sf-flash'), 1600)
  }
  const toIssuance = () => {
    const banner = bodyRef.current?.querySelector<HTMLElement>('[data-issuance-banner]')
    if (!banner) return
    banner.scrollIntoView({ block: 'start', behavior: reducedMotion() ? 'auto' : 'smooth' })
    banner.focus({ preventScroll: true })
    banner.dataset.flash = 'focus'
    setTimeout(() => { if (banner.dataset.flash === 'focus') delete banner.dataset.flash }, 1600)
  }

  // 别处请这一层把审批卡 / 出具横幅摆到眼前（胶囊的「去审批」、成果节点的出具印章）。
  // 卡片跟着审批列表来，刚停下时可能还在路上：等取回来（或确认没有）再办，别滚到空处
  // 节点的步骤交给助手流自己滚：滚过去要暂停跟随，贴底跟随的开关在它手里
  const revealed = useRef(0)
  useEffect(() => {
    if (!reveal || reveal.seq === revealed.current) return
    if (raw) { setRaw(false); return }
    if (reveal.kind === 'step') return
    if (reveal.kind === 'approval' && !hasCard && phase === 'waiting' && looked !== run.id) return
    revealed.current = reveal.seq
    // 等这一帧的布局落定：运行层刚挂上时，助手流自己也在做打开时的定位。不在清理里
    // 取消——紧跟着来一条事件就会把这次请求吞掉；卸载了的话两个函数找不到东西自然不做
    requestAnimationFrame(reveal.kind === 'approval' ? toApproval : toIssuance)
    onRevealed()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reveal, raw, hasCard, phase, looked, run.id, turns])
  const stepReveal = useMemo(() => (reveal?.kind === 'step' && reveal.nodeId && !raw
    ? { nodeId: reveal.nodeId, seq: reveal.seq } : null), [reveal, raw])

  const live = phase === 'running' || phase === 'queued'
  // 停在审批上、服务重启时挂起的：不在执行，给的是「放弃」而不是「停止」
  const parked = phase === 'waiting' || phase === 'suspended'

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
          // 等人时首要的是此刻真正该做的事；「放弃」是次一级的，收成一枚图标
          <button type="button" className="btn btn-xs" style={{ color: 'var(--st-waiting)' }} onClick={toApproval}>
            <Hand size={11} aria-hidden /> 去审批
          </button>
        )}
        {parked && (
          <IconButton label="放弃这次运行" variant="danger" disabled={stopping} onClick={() => void abandon()}>
            <Ban size={12} aria-hidden />
          </IconButton>
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
            // 没绑工具、轮数不够这种要去画布上改的：直接打开那个节点的设置，镜头也带过去
            onStepOpen={(id) => { select(id); focusNode(id) }}
            activeNodeId={hoveredNodeId}
            reveal={stepReveal}
            onRevealed={onRevealed}
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
  const num = finite
  const activeMs = settled ? stoppedActiveMs(u, trace) : view?.activeMs
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
          {activeMs == null ? NONE : ticking ? formatLapse(activeMs) : formatSpan(activeMs)}
        </span>
      </span>
      {(waitMs ?? 0) > 0 && (
        <span title="等人审批的总时长" style={phase === 'waiting' ? { color: 'var(--st-waiting)' } : undefined}>
          等人 <span className="mono">{ticking ? formatLapse(waitMs!) : formatSpan(waitMs!)}</span>
        </span>
      )}
      <span title={approx ? '运行中实时累加，可能偏少；跑完以后端累计为准' : '后端累计'}>
        <span className="mono">{approx && tokens ? '≥' : ''}{tokens ? formatTokens(tokens, { compact: true }) : `${NONE} tok`}</span>
      </span>
      <span className="mono">{approx && cost ? '≥' : ''}{cost != null && (cost > 0 || settled) ? formatCost(cost) : NONE}</span>
    </div>
  )
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
