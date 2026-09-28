import {
  createContext, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState,
  type MouseEvent as ReactMouseEvent, type ReactNode,
} from 'react'
import { Link } from 'react-router-dom'
import {
  AlertCircle, AlertTriangle, ArrowDown, Brain, ChevronRight, CircleCheck, CircleDot, CornerDownRight,
  Database, Download, ExternalLink, FileCode, FileDown, GitBranch, Hand, Info, Play, Settings2,
  ShieldCheck, Sparkles, Table2, Terminal, UserX, Users, Wrench, XCircle,
} from 'lucide-react'
import clsx from 'clsx'
import { api } from '../api/client'
import { ErrorNotice, Modal, Skeleton, StatusBadge } from '../components/ui'
import { humanizeError } from '../lib/errors'
import { explainRunError } from '../lib/explain'
import { formatClock, formatDuration, formatNumber, NONE, shortId } from '../lib/format'
import { statusLabel } from '../lib/status'
import { issuanceLabel, nodeTypeLabel } from '../lib/terms'
import {
  childrenByExec, compactSteps, parseQueryResult, progressOf, spread, teamVerdictOf,
  type Exec, type FixKind, type ResultTable as Table, type Step, type StepKind, type TeamMemberEx,
  type TeamRun,
} from './decode'
import { CODE_COLUMN, CopyChip, LEADING_ZERO, Markdown, type MarkSpec } from './Markdown'
import { useRunClock } from './useRunClock'
import type { ReviewResult } from '../types'

/**
 * 助手流：一轮轮「你问什么 → 它做了什么 → 结果」。
 *
 * 纯展示，不碰 store。画布右栏（360px）、问数据页（宽屏）、运行页用的是同一个
 * 组件，差别只在 dense 这个开关和几个可选回调上——而不是做成"一个组件三种停靠
 * 模式"：那要求同一个组件实例出现在三个不同的 DOM 父节点里，React 里除非用
 * portal 否则不成立，硬做就会变成跨路由 unmount/remount，草稿和展开状态全丢。
 *
 * 步骤来自 decode.ts，这里只负责怎么画。三处读同一个解码结果，所以同一次运行
 * 在哪儿看讲的都是同一个故事。
 */

/**
 * 页面已经拆好的报错：问数据页的 Failure、记录页的 explainRunError。照原样画，不再
 * 交给 humanizeError 翻一遍——已经是人话的「操作超时：查询超时…」再翻一遍，就成了
 * 「操作超时 / 操作超时：查询超时…」
 */
export interface TurnFailure {
  title: string
  reason?: string
  /** 怎么办 */
  hint?: string
  /** 原文，收进技术细节 */
  detail?: string
  /** 该去哪儿改（lib/explain 的 fix）：给了就在报错里放直达入口 */
  fix?: FixKind | 'rerun'
  /** fix 的站内地址（lib/explain 的 fixTo）：直接打开要改的那一项，比笼统的「去工具库」准 */
  fixTo?: string
  /** 先改再接着跑（lib/explain 的 fixFirst）：入口写明要改什么 */
  fixFirst?: boolean
}

export interface StreamTurn {
  id: string
  /** 用户说了什么。画布上的 Copilot 指令也走这个字段 */
  question?: string
  phase: 'running' | 'waiting' | 'done' | 'error'
  /** 一句话进度，顶在最前面 */
  status?: string
  steps: Step[]
  /** 还在流的思考（尚未成为 Step）。只有 Anthropic 系模型会有 */
  thinking?: string
  output?: Record<string, any> | null
  /**
   * 报错，三种给法：
   * - 一句原话：运行的失败按 lib/explain 讲清为什么、怎么办，别的按 humanizeError；
   * - {error, hint?, detail?}：原话 + 怎么办 + 原文（Copilot 的 error 操作）；
   * - TurnFailure：页面已经拆好的，照原样画。
   */
  error?: string | { error: string; hint?: string; detail?: string } | TurnFailure
  /**
   * 这一轮查了几次库。从库里恢复的轮次没有事件，步骤里数不出来，页面手上有落库的
   * 次数就传进来；步骤里数得出来时以步骤为准
   */
  queries?: number
  runId?: string
  /** formal / exploratory。出具横幅要据此标注"不进正式归档" */
  runClass?: string
  /** 这一轮建出来的图，可展开看、可放到画布 */
  graph?: any
  graphNote?: string
  /** 这一条没有查库，答案是根据前几轮说的 */
  noQuery?: boolean
  /** 复核结论：这次运行有什么不对，以及对这个答案意味着什么 */
  review?: ReviewResult | null
  /** 被复核重写之前的答案。改写是有损的，得能对照原件 */
  rawOutput?: Record<string, any> | null
  /**
   * 这一轮开始的时刻（毫秒时间戳）。有它，进行中的轮次头部才走实时计时；不给就从第一步
   * 开始的时刻推。null 表示明确不要实时计时：问数据页刚从库里恢复、正在对账的轮次，
   * 事件可能先到一两条，那不是它开始跑了
   */
  startedAt?: number | null
  /** 跑完的轮次花了多久（毫秒）。不给就从「完成」那一行推 */
  elapsedMs?: number
  /**
   * 头部要不要提醒。phase 只分得出跑没跑完；「图放上去了，但还有 2 处问题」
   * 也是 done，头部却不该是一个安静的绿勾
   */
  tone?: 'warn' | 'failed'
  /** 头部徽标用哪个状态码（lib/status）。不给就按 phase：已取消、已中断这些 phase 表达不了 */
  statusCode?: string
}

const ICONS: Record<StepKind, typeof Wrench> = {
  node: CircleDot, think: Brain, llm: Sparkles, query: Database, schema: Table2,
  tool: Wrench, code: Terminal, branch: GitBranch, human: Hand,
  issuance: FileCode, note: CircleDot, error: XCircle, lifecycle: CircleCheck,
}

/** 步骤行共用的上下文：联动回调、时钟差、工件抽屉。一层层往下传 props 会穿过五六个组件 */
interface StreamCtx {
  dense: boolean
  onHover?: (nodeId: string | null) => void
  onFocus?: (nodeId: string) => void
  /** 打开这个节点的设置去改（编排页）。下一步是「去画布改」的行据此给直达入口 */
  onOpen?: (nodeId: string) => void
  activeNodeId?: string | null
  skewMs: number
  openArtifact: (id: string, title?: string) => void
  reduced: boolean
  /** 还没办完的点名请求：要看的那一行收在「展开前面的 N 条」里时，列表先展开 */
  reveal?: { nodeId: string; seq: number } | null
}
const Ctx = createContext<StreamCtx>({
  dense: false, skewMs: 0, openArtifact: () => undefined, reduced: false,
})

/** 系统的「减少动效」。扫光和转圈在那种模式下停住，得换一种说法表示"在跑" */
function useReducedMotion(): boolean {
  const query = '(prefers-reduced-motion: reduce)'
  const [reduced, setReduced] = useState(() =>
    typeof matchMedia === 'function' && matchMedia(query).matches)
  useEffect(() => {
    if (typeof matchMedia !== 'function') return
    const mq = matchMedia(query)
    const on = () => setReduced(mq.matches)
    mq.addEventListener('change', on)
    return () => mq.removeEventListener('change', on)
  }, [])
  return reduced
}

/** 一轮里的步骤数（含子步骤），跟随和「有几条新进展」都按它算 */
function countSteps(steps: Step[]): number {
  let n = 0
  for (const s of steps) n += 1 + (s.children ? countSteps(s.children) : 0)
  return n
}

/** 离底部多近算"在底部"。比一行步骤略高，手指一抖不至于就断开跟随 */
const STICK_PX = 64

export function AssistantStream({
  turns, dense = false, empty, approvalsFor, onOpenGraph, footer, renderTurnActions,
  onStepHover, onStepFocus, onStepOpen, activeNodeId, follow = true, landing, onFollowUp,
  clockSkewMs = 0, className, resetKey, reveal, onRevealed,
}: {
  turns: StreamTurn[]
  /** 窄栏模式：更紧的排版、表格少列 */
  dense?: boolean
  /** 没有任何一轮时显示什么 */
  empty?: ReactNode
  /** 这一轮要不要插审批卡——插槽而不是内建，因为不同页面接流的方式不同 */
  approvalsFor?: (turn: StreamTurn) => ReactNode
  onOpenGraph?: (graph: any, question?: string) => void
  footer?: ReactNode
  /** 每一轮结束之后的动作槽：重试、接着跑、让 Copilot 再修……由页面决定放什么 */
  renderTurnActions?: (turn: StreamTurn, index: number) => ReactNode
  /** 编排页：步骤行悬停 / 点击时联动画布。问数据页没有画布，不传 */
  onStepHover?: (nodeId: string | null) => void
  onStepFocus?: (nodeId: string) => void
  /**
   * 编排页：打开某个节点的设置。节点没绑工具、团队轮数不够这类要去画布上改的，
   * 报错和提醒行据此给「打开设置」；不传就只说该去哪儿改
   */
  onStepOpen?: (nodeId: string) => void
  /** 画布上悬停的节点：对应的步骤行高亮 */
  activeNodeId?: string | null
  /** 贴着底部时，新步骤到来自动滚到底。离开底部就不再拽人 */
  follow?: boolean
  /**
   * 打开时停在哪。end：最新处（对话）；summary：摘要——有失败停在第一处失败、
   * 等人停在审批卡、否则停在顶部（回看历史运行）。不传时只有一轮且不在跑就按 summary
   */
  landing?: 'end' | 'summary'
  /** 答案下面的追问标签点了之后做什么；不传就不显示追问 */
  onFollowUp?: (text: string) => void
  /** 服务器与本机的时钟差（毫秒）。实时计时从事件 ts 起算，差了这个数会跑快或跑慢 */
  clockSkewMs?: number
  className?: string
  /**
   * 换了它才算"换了一批轮次"、重新定位。不传就认第一轮的 id。在前面补出更早的轮次
   * （助手栏展开「之前的 N 轮」）时第一轮变了，但人还在看同一段对话，得传一个不随之变的
   */
  resetKey?: string
  /**
   * 请这条流把某个节点最后一次执行摆到眼前、描一下边（画布上点了跑过的节点）。seq 变了才办；
   * 办完调 onRevealed，页面据此把请求清掉，免得重新挂载时又办一遍
   */
  reveal?: { nodeId: string; seq: number } | null
  onRevealed?: (seq: number) => void
}) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const pinned = useRef(true)
  /** 上一次跟随时看到的最后一轮：哪一轮、多少步、其余会变的东西 */
  const seen = useRef({ id: '', n: 0, sig: '' })
  const [fresh, setFresh] = useState(0)
  const [artifact, setArtifact] = useState<{ id: string; title?: string } | null>(null)
  const reduced = useReducedMotion()

  const last = turns[turns.length - 1]
  // 跟随和「N 条新进展」只看最后一轮、而且只数它还在进行时长出来的步骤。已经停下的轮次点开
  // 「看执行过程」时步骤是按需取回的历史：以前也算进总数，没有任何运行在跑，底下却浮出
  // 「24 条新进展」；贴着底时还被一把拽到底部，刚点开的执行过程被推出视口
  const lastCount = useMemo(() => (last ? countSteps(last.steps) : 0), [last])
  const lastLive = last?.phase === 'running' || last?.phase === 'waiting'
  // 除步骤数以外，变了就值得跟随的东西：新的一轮、相位、成果或报错出现
  const signature = `${turns.length}|${last?.id}|${last?.phase}|${!!last?.output}|${!!last?.error}`
  const mode = landing ?? (turns.length === 1 && last?.phase !== 'running' ? 'summary' : 'end')
  // 换了一批轮次（切换会话、打开另一条运行）就当作第一次打开，重新定位
  const identity = resetKey ?? turns[0]?.id ?? ''

  const onScroll = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < STICK_PX
    if (atBottom === pinned.current) return
    pinned.current = atBottom
    // 离开底部时不用记基数：新进展是离开之后一条条累加的，贴底时它一直是 0
    if (atBottom) setFresh(0)
  }, [])

  // 首次打开：直接定位，不做平滑滚动——从顶部平滑滚过六轮对话只是让人晕
  useLayoutEffect(() => {
    const el = scrollRef.current
    if (!el || !turns.length) return
    setFresh(0)
    seen.current = { id: last?.id ?? '', n: lastCount, sig: signature }
    if (mode === 'end') {
      el.scrollTop = el.scrollHeight
      pinned.current = true
      return
    }
    const failed = el.querySelector<HTMLElement>('[data-step-status="failed"]')
    const approval = el.querySelector<HTMLElement>('[data-approval-slot]:not(:empty)')
    const target = failed ?? approval
    if (target) target.scrollIntoView({ block: 'center' })
    else el.scrollTop = 0
    pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < STICK_PX
    if (failed) return flash(failed, 'failed')
    // 只在换了一批轮次时重新定位；之后的变化交给下面的跟随
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [identity, turns.length === 0])

  // 跟随：只在贴底时跟；离开底部就数一数来了几条新的，给一枚「跳到最新」
  useLayoutEffect(() => {
    const prev = seen.current
    seen.current = { id: last?.id ?? '', n: lastCount, sig: signature }
    const el = scrollRef.current
    if (!el || !follow) return
    const grown = last?.id !== prev.id ? lastCount : lastLive ? Math.max(0, lastCount - prev.n) : 0
    if (!grown && signature === prev.sig) return
    if (pinned.current) el.scrollTop = el.scrollHeight
    else if (grown) setFresh((f) => f + grown)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature, lastCount, follow])

  // 点名要看某个节点（画布上点了它）：滚过去就暂停跟随——人要看的是那一步，新进展来了
  // 也不能把他拽回底部；他自己滚回底部再恢复（onScroll）。
  // 等这一帧落定再滚：右栏刚从对话层切过来时，「打开时停在最新处」的定位（开发期 StrictMode
  // 还会把它重跑一遍）都得排在这一次前面，不然人刚被带到那一步又被拽回底部。不在清理里
  // 取消，理由同 RunView：清理一跑请求就吞掉了；卸载了的话找不到东西自然不做
  const revealed = useRef(0)
  const unflash = useRef<(() => void) | null>(null)
  // 长运行里那一行可能收在「展开前面的 N 条」里：StepList 从 Ctx 看到请求会先展开，这里多等
  // 几帧再找。找到了才暂停跟随、描边、报办完；始终找不到就原地不动，不把人带走又什么都不描。
  // 用 useEffect 不用 useLayoutEffect：开发期 StrictMode 把挂载时的 effect 重跑一遍是在
  // passive 这一拍里做的，排在它后面起跳，重跑的「打开时停在最新处」和卸载时摘描边都已经过去
  useEffect(() => {
    if (!reveal || reveal.seq === revealed.current) return
    revealed.current = reveal.seq
    const { nodeId, seq } = reveal
    let tries = 0
    const attempt = () => {
      const el = scrollRef.current
      const target = el ? lastExecOf(el, nodeId) : null
      if (!el) return
      if (!target) {
        if (++tries < REVEAL_TRIES) requestAnimationFrame(attempt)
        return
      }
      pinned.current = false
      setFresh(0)
      target.scrollIntoView({ block: 'start', behavior: reduced ? 'auto' : 'smooth' })
      unflash.current?.()
      unflash.current = flash(target, 'focus')
      onRevealed?.(seq)
    }
    requestAnimationFrame(attempt)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reveal])
  // 请求清掉（reveal 变回 null）时描边照常留满时长，只在卸载时摘
  useEffect(() => () => unflash.current?.(), [])

  // 悬停联动画布：在滚动容器上统一认"指着哪个节点"。挂在每一行上的话，从节点行移进
  // 它下面的查询行会先触发子行的 leave，把高亮清掉，人明明还指着这个节点
  const hovered = useRef<string | null>(null)
  const onPointerOver = onStepHover && ((e: ReactMouseEvent) => {
    const id = (e.target as HTMLElement).closest<HTMLElement>('[data-node-id]')?.dataset.nodeId ?? null
    if (id === hovered.current) return
    hovered.current = id
    onStepHover(id)
  })
  const onPointerLeave = onStepHover && (() => {
    if (hovered.current == null) return
    hovered.current = null
    onStepHover(null)
  })

  const jumpToLatest = () => {
    const el = scrollRef.current
    if (!el) return
    pinned.current = true
    setFresh(0)
    el.scrollTo({ top: el.scrollHeight, behavior: reduced ? 'auto' : 'smooth' })
  }

  const ctx = useMemo<StreamCtx>(() => ({
    dense, onHover: onStepHover, onFocus: onStepFocus, onOpen: onStepOpen, activeNodeId, skewMs: clockSkewMs,
    openArtifact: (id, title) => setArtifact({ id, title }), reduced, reveal,
  }), [dense, onStepHover, onStepFocus, onStepOpen, activeNodeId, clockSkewMs, reduced, reveal])

  const rootClass = clsx('flex h-full min-h-0 flex-1 flex-col', className)
  if (!turns.length && empty) {
    return <div className={rootClass}>{empty}{footer}</div>
  }

  return (
    <Ctx.Provider value={ctx}>
      <div className={rootClass}>
        <div className="relative min-h-0 flex-1">
          <div ref={scrollRef} onScroll={onScroll} data-stream-scroll=""
               onMouseOver={onPointerOver || undefined} onMouseLeave={onPointerLeave || undefined}
               className="h-full overflow-y-auto overflow-x-hidden">
            {/* 留白放在里面一层：滚动容器自己带 padding 的话，吸顶的轮次头会停在离顶边
                一个 padding 的地方，上面露出一条滚过去的正文 */}
            <div className={dense ? 'px-2.5 py-3' : 'px-4 py-4'}>
              <div className={clsx('space-y-4', !dense && 'mx-auto max-w-3xl')}>
                {turns.map((turn, i) => (
                  <div key={turn.id}>
                    <TurnCard turn={turn} last={i === turns.length - 1}
                              approvals={approvalsFor?.(turn)} onOpenGraph={onOpenGraph}
                              onFollowUp={i === turns.length - 1 ? onFollowUp : undefined} />
                    {renderTurnActions && <TurnActions>{renderTurnActions(turn, i)}</TurnActions>}
                  </div>
                ))}
              </div>
            </div>
          </div>
          {fresh > 0 && (
            <button
              type="button"
              data-jump-latest=""
              onClick={jumpToLatest}
              className="fade-up absolute bottom-3 left-1/2 flex -translate-x-1/2 items-center gap-1 rounded-full border px-2.5 py-1 text-2xs shadow-elev-2 transition-colors hover:bg-hover"
              style={{ background: 'var(--bg-elev)', color: 'var(--text)' }}
            >
              <ArrowDown size={11} aria-hidden /> {fresh} 条新进展 · 跳到最新
            </button>
          )}
        </div>
        {footer}
      </div>
      <ArtifactViewer id={artifact?.id ?? null} title={artifact?.title} onClose={() => setArtifact(null)} />
    </Ctx.Provider>
  )
}

/**
 * 节点最后一次执行的那一块：执行过多次的，是分轮折叠里最后那一轮（它总是列着的）；
 * 只执行过一次的，是节点那一行本身。节点行里套着它自己的子步骤（同一个 data-node-id），
 * 取最外层那个
 */
function lastExecOf(root: HTMLElement, nodeId: string): HTMLElement | null {
  const id = CSS.escape(nodeId)
  const row = [...root.querySelectorAll<HTMLElement>(`[data-step-status][data-node-id="${id}"]`)]
    .find((el) => !el.parentElement?.closest(`[data-node-id="${id}"]`))
  if (!row) return null
  const execs = row.querySelectorAll<HTMLElement>(`[data-exec-groups="${id}"] [data-exec]`)
  return execs.length ? execs[execs.length - 1] : row
}

/** 空的动作槽不占位：页面对某些轮次什么都不放是常态 */
function TurnActions({ children }: { children: ReactNode }) {
  if (children == null || children === false) return null
  return <div className="mt-1.5 flex flex-wrap items-center gap-1.5">{children}</div>
}

/**
 * 定位到的那一行短暂描边一下，告诉人"是这里"。failed 用失败色（回看历史时落到
 * 第一处失败），focus 用强调色（点名要看的那一轮）——一次普通的定位不能描成红框，
 * 那等于把好好的一步说成出了错。返回清理函数：换了目标或卸载时立刻摘掉，不留残框
 */
function flash(el: HTMLElement, kind: 'failed' | 'focus'): () => void {
  el.dataset.flash = kind
  const clear = () => { if (el.dataset.flash === kind) delete el.dataset.flash }
  const t = setTimeout(clear, 1800)
  return () => { clearTimeout(t); clear() }
}

// -------------------------------------------------------------------------
// 一轮
// -------------------------------------------------------------------------

function TurnCard({ turn, last, approvals, onOpenGraph, onFollowUp }: {
  turn: StreamTurn; last: boolean; approvals?: ReactNode
  onOpenGraph?: (graph: any, question?: string) => void
  onFollowUp?: (text: string) => void
}) {
  const { dense } = useContext(Ctx)
  // 报错已经在卡片顶上说了一遍（带原因、怎么办、技术细节），步骤列表末尾那条运行级的
  // 红行（run.failed）不再重复。页面拆好的报错标题是改写过的，对不上原话，所以按来源认
  const errText = typeof turn.error === 'string' ? turn.error
    : turn.error && 'error' in turn.error ? turn.error.error : undefined
  const steps = turn.phase === 'error' && turn.error
    ? turn.steps.filter((s) => !(s.kind === 'error' && (s.title === errText || s.id.startsWith('rf-'))))
    : turn.steps
  const plan = steps.filter((s) => s.stage === 'plan')
  // 「开始运行（6 个节点）」「完成」和轮次头说的是同一件事（状态、节点数、耗时），
  // 再列一遍只是噪音；失败时「开始运行」还会被画成红叉，像是出了第二个错
  const exec = steps.filter((s) => s.stage !== 'plan' && !redundant(s))
  const broken = turn.review?.severity === 'broken'

  return (
    <div className="fade-up" data-turn={turn.id}>
      {turn.question && (
        // 宽屏走聊天气泡（右对齐、实心），窄栏里气泡会挤成一条竖带，
        // 改用左侧强调条——同样能一眼看出"这句是你说的"，但不吃宽度
        <div className={clsx('mb-2 flex', dense ? 'justify-start' : 'justify-end')}>
          <div
            className={clsx(
              'whitespace-pre-wrap leading-relaxed [overflow-wrap:anywhere]',
              dense
                ? 'w-full border-l-2 py-0.5 pl-2 text-[11.5px]'
                : 'max-w-[80%] rounded-lg rounded-br-sm px-3 py-1.5 text-sm',
            )}
            style={dense
              ? { borderColor: 'var(--accent)', color: 'var(--text)' }
              : { background: 'var(--accent-solid)', color: 'var(--on-accent)' }}
          >
            {turn.question}
          </div>
        </div>
      )}

      <div className="rounded-lg border bg-panel">
        <TurnHead turn={turn} />

        <div className={clsx(dense ? 'px-2.5 pb-2.5' : 'px-3 pb-3')}>
          {turn.phase === 'error' && turn.error && (
            // 报错不截成一行：原因和怎么办要读得全，原文收进可展开的技术细节
            <TurnError turn={turn} />
          )}

          {/* 还在流、但还没成为步骤的思考。解码器已经把思考并成了步骤行的话就不再
              重复一遍——同一段话在灰框和步骤行里各出现一次 */}
          {turn.phase === 'running' && turn.thinking
            && !plan.some((s) => s.kind === 'think' && s.status === 'running')
            && <ThinkingTail text={turn.thinking} />}

          {plan.length > 0 && (
            <StepSection
              label={exec.length ? '规划' : '过程'}
              steps={plan}
              // 开始运行之后规划那段自动收起："建图时加的节点"和"执行时跑的节点"
              // 同名同序地再出现一遍只是噪音
              defaultOpen={exec.length ? false : turn.phase !== 'done' || last}
              turn={turn}
            />
          )}
          {exec.length > 0 && (
            <StepSection label="执行" steps={exec} turn={turn}
                         titled={plan.length > 0}
                         defaultOpen={turn.phase !== 'done' || last} />
          )}

          <div data-approval-slot="">{approvals}</div>

          {/* 没查库这件事必须说在结论前面。「涉及数据必须真查」是这条路径上
              最硬的一条约定，放开它之后，用户得能一眼分清哪些结论背后真的
              动了库、哪些只是在复述前面几轮 */}
          {turn.noQuery && (
            <div className="mt-2 flex items-start gap-1.5 rounded border px-2 py-1.5 text-2xs leading-relaxed text-dim">
              <Info size={11} className="mt-[2px] shrink-0" aria-hidden />
              <span>这一条没有查库，是根据前面几轮的结果说的</span>
            </div>
          )}

          {/* 复核说明排在成果**上方**，和上面那条声明同一个位置。排在下面
              等于让人读完整个结论、信了，才知道它是在什么条件下得出的 */}
          {turn.review && <ReviewNote review={turn.review} />}

          {turn.output && (
            <Output output={turn.output} runClass={turn.runClass} broken={broken}
                    question={turn.question} onFollowUp={onFollowUp} />
          )}

          {turn.rawOutput && (
            <details className="mt-1.5">
              <summary className="cursor-pointer text-2xs text-dim hover:text-fg">
                复核改写过这个答案，看改写前的原文
              </summary>
              <div className="mt-1.5">
                <Output output={turn.rawOutput} />
              </div>
            </details>
          )}

          {!!turn.graph?.nodes?.length && (
            <GraphPeek graph={turn.graph} note={turn.graphNote}
                       onOpen={onOpenGraph && ((g) => onOpenGraph(g, turn.question))} />
          )}
        </div>
      </div>
    </div>
  )
}

/**
 * 轮次头：状态、阶段进度、实时计时。
 *
 * 吸顶：长运行往下翻的时候，"现在到哪了、跑了多久"一直看得见——这就是回看
 * 历史运行时那条迷你摘要。状态变化另放一个只有读屏器看得见的播报区：计时每
 * 100ms 跳一次，放进 aria-live 会被一直念。
 */
function TurnHead({ turn }: { turn: StreamTurn }) {
  const { dense, skewMs } = useContext(Ctx)
  const progress = useMemo(() => progressOf(turn.steps.filter((s) => s.stage !== 'plan')), [turn.steps])
  const live = turn.phase === 'running'
  const now = useRunClock(live)
  const review = turn.review?.severity
  const code = turn.phase === 'running' ? 'running'
    : turn.phase === 'waiting' ? 'waiting'
    : turn.phase === 'error' ? 'failed'
    : 'succeeded'

  // 协作团队用完轮数、按降档交付：节点「完成」了，交出去的却是成员最后的原话
  const teamShort = useMemo(() => !!findFirst(turn.steps, (s) =>
    s.code === 'team_exhausted' || (!!s.team && teamVerdictOf(s.team)?.outcome === 'degraded')), [turn.steps])
  // 跑完了但复核说结论不能用：头部不能还是一个安静的「完成」
  const base = turn.status || statusLabel(turn.statusCode ?? code)
  const verdict = turn.phase === 'done' && review === 'broken' ? '结论不可用'
    : turn.phase === 'done' && review === 'degraded' ? '有缺口'
    : turn.phase === 'done' && teamShort ? '协作团队没做完' : ''
  // 复核结论说的是答案，不是运行：运行确实跑完了，徽标和「已完成」照常，红 / 琥珀只
  // 落在结论那几个字上。把完成的勾染成红色，形状说成功、颜色说失败，两头打架
  const tone = turn.tone === 'failed' ? 'var(--st-failed)'
    : turn.tone === 'warn' ? 'var(--st-waiting)'
    : turn.phase === 'error' ? 'var(--st-failed)'
    : turn.phase === 'waiting' ? 'var(--st-waiting)'
    : undefined
  const verdictTone = verdict === '结论不可用' ? 'var(--st-failed)' : 'var(--st-waiting)'
  const badgeCode = turn.statusCode
    ?? (turn.tone === 'warn' && turn.phase === 'done' ? 'waiting'
      : turn.tone === 'failed' && turn.phase === 'done' ? 'failed' : code)

  const firstStart = useMemo(() => findFirst(turn.steps, (s) => s.startedAt != null)?.startedAt, [turn.steps])
  const started = turn.startedAt === null ? undefined : turn.startedAt ?? firstStart
  // 「完成」那一行不再单独列出，它的「执行 1.2 s · 等人 3 分」挪到头上
  const doneRow = turn.steps.find((s) => s.kind === 'lifecycle' && s.title === '完成')
  const took = turn.elapsedMs != null ? (turn.elapsedMs >= 10 ? formatDuration(turn.elapsedMs) : '')
    : doneRow?.meta ?? ''
  const elapsed = live && started != null ? Math.max(0, now - skewMs - started) : undefined
  // 从库里恢复的轮次没有事件，步骤里数不出查询：用页面带过来的落库次数
  const counted = useMemo(() => countKind(turn.steps, 'query'), [turn.steps])
  const queries = counted || turn.queries || 0
  // 档位优先取成果里那份（带全了核对明细），失败的运行、成果被替换过的轮次退到出具那一行
  const tierStep = useMemo(() => findFirst(turn.steps, (s) => s.kind === 'issuance' && !!s.tier)?.tier, [turn.steps])
  const tier = typeof turn.output?._issuance?.tier === 'string' ? turn.output._issuance.tier as string : tierStep ?? ''

  const tags = [
    progress.total ? `${progress.done}/${progress.total} 节点` : '',
    !live && queries ? `${queries} 次查询` : '',
    !live ? took : '',
  ].filter(Boolean)
  const fraction = progress.total ? Math.min(1, progress.done / progress.total) : undefined
  // 节点轨：图不大时一个节点一格，按状态着色——跑到哪、哪一步跳过了、卡在哪一眼看全。
  // 只在跑着、等人时画；跑完了安静，出了事由步骤流里的红行说
  const rail = useMemo(() => (progress.total && progress.total <= RAIL_MAX ? railOf(turn.steps) : null),
    [progress.total, turn.steps])
  const showRail = (live || turn.phase === 'waiting') && !!rail && !!progress.total

  return (
    // 吸顶之后正文从它下面滚过去，没有这道线的话头和正文糊成一片。没有步骤的轮次
    // （只回了一句话）下面紧跟着成果区自己的分隔线，不再画第二道
    <div className={clsx('sticky top-0 z-[1] rounded-t-lg bg-panel', dense ? 'px-2.5 pt-2 pb-1.5' : 'px-3 pt-2.5 pb-2',
                         (turn.steps.length > 0 || live) && 'border-b')}
         style={{ borderColor: 'var(--hairline, var(--border))' }}>
      <div className={clsx('flex items-center gap-2', dense ? 'text-[11.5px]' : 'text-xs')}>
        <StatusBadge status={badgeCode} size={13} decorative />
        <span className={clsx('min-w-0 truncate font-medium', !tone && 'text-fg')}
              style={tone ? { color: tone } : undefined} data-turn-status=""
              title={verdict ? `${base} · ${verdict}` : base}>
          {base}
          {verdict && <span data-verdict="" style={{ color: verdictTone }}> · {verdict}</span>}
        </span>
        {tags.length > 0 && (
          <span className="tnum min-w-0 shrink truncate text-2xs text-dim">{tags.join(' · ')}</span>
        )}
        {tier && (
          // 出具档位也挂在吸顶的头上：往下翻到报告中段时，"这份结论能不能当真"还看得见。
          // 降档、不予出具带个警示形状，只靠颜色的话色弱的人读不出它和「完整出具」的差别
          <span data-head-tier={tier}
                className="inline-flex shrink-0 items-center gap-0.5 rounded px-1 text-2xs font-medium"
                title={TIER_META[tier]?.hint}
                style={{ color: TIER_META[tier]?.color ?? 'var(--st-waiting)',
                         background: TIER_META[tier]?.soft ?? 'var(--st-waiting-soft)' }}>
            {tier !== 'formal' && <AlertTriangle size={10} aria-hidden />}
            {issuanceLabel(tier)}
          </span>
        )}
        <span className="flex-1" />
        {elapsed != null && (
          <span className="mono tnum shrink-0 text-2xs" style={{ color: 'var(--st-running)' }}
                title="已运行">
            {formatClock(elapsed)}
          </span>
        )}
        {turn.runId && (
          <Link to={`/runs/${turn.runId}`} title="在运行记录中查看"
                className="mono shrink-0 rounded px-1 text-2xs text-dim underline-offset-2 transition-colors hover:bg-hover hover:text-fg hover:underline">
            {shortId(turn.runId)}
          </Link>
        )}
        <span className="sr-only" role="status" aria-live="polite">
          {`${base}${verdict ? `，${verdict}` : ''}${tier ? `，${issuanceLabel(tier)}` : ''}`}
        </span>
      </div>
      {live && progress.current && (
        <div className="mt-0.5 truncate pl-[21px] text-2xs text-dim" title={progress.current.title}>
          正在：{progress.current.title}
        </div>
      )}
      {showRail && (
        <div className="mt-1.5 flex gap-[3px]" role="progressbar" aria-label="节点进度"
             aria-valuemin={0} aria-valuemax={progress.total} aria-valuenow={progress.done}
             data-node-rail="">
          {Array.from({ length: progress.total! }, (_, i) => {
            const n = rail![i]
            // 还没轮到的画成空心格：一格一格数得清还剩几步。填成 --bg-hover 的话和栏底
            // 只差 1.1:1，5 个节点跑到第 3 个看着像一共 4 格；填成未运行色又和「已跳过」撞色
            return (
              <span key={n?.id ?? `todo-${i}`} title={n ? `${n.title} · ${statusLabel(n.status)}` : '还没轮到'}
                    className="h-[3px] min-w-0 flex-1 rounded-full"
                    style={n ? { background: RAIL_COLOR[n.status] ?? 'var(--st-done)' }
                      : { boxShadow: 'inset 0 0 0 1px var(--st-idle)' }} />
            )
          })}
        </div>
      )}
      {live && fraction != null && !showRail && (
        // 节点进度压在头部的底线上：按不同节点数，不按执行次数，循环不会让它倒退或超过 100%
        <div className="absolute inset-x-0 -bottom-px h-[2px] overflow-hidden"
             role="progressbar" aria-valuemin={0} aria-valuemax={progress.total} aria-valuenow={progress.done}
             aria-label="节点进度">
          <div className="h-full origin-left transition-transform duration-300"
               style={{ background: 'var(--st-running)', transform: `scaleX(${fraction})` }} />
        </div>
      )}
    </div>
  )
}

/** 节点轨最多画几格。再多就是一条挤满细缝的条，不如一根进度条 */
const RAIL_MAX = 15

const RAIL_COLOR: Record<string, string> = {
  running: 'var(--st-running)', waiting: 'var(--st-waiting)', failed: 'var(--st-failed)',
  done: 'var(--st-done)', skipped: 'var(--st-skipped)', cancelled: 'var(--st-cancelled)',
  suspended: 'var(--st-suspended, var(--st-waiting))',
}

/** 出现过的节点，按第一次出现的顺序；并行组里的几路各算一格 */
function railOf(steps: Step[]): { id: string; title: string; status: string }[] {
  const out: { id: string; title: string; status: string }[] = []
  const seen = new Set<string>()
  const walk = (list: Step[]) => {
    for (const s of list) {
      if (s.kind === 'node' && s.nodeId && s.stage !== 'plan' && !seen.has(s.nodeId)) {
        seen.add(s.nodeId)
        out.push({ id: s.nodeId, title: s.title.replace(/^跳过「(.+)」$/, '$1'), status: s.status ?? 'done' })
      } else if (s.kind === 'branch' && !s.nodeId && s.children) walk(s.children)
    }
  }
  walk(steps)
  return out
}

function findFirst(steps: Step[], pred: (s: Step) => boolean): Step | undefined {
  for (const s of steps) {
    if (pred(s)) return s
    const hit = s.children && findFirst(s.children, pred)
    if (hit) return hit
  }
  return undefined
}

function countKind(steps: Step[], kind: StepKind): number {
  let n = 0
  for (const s of steps) n += (s.kind === kind ? 1 : 0) + (s.children ? countKind(s.children, kind) : 0)
  return n
}

/**
 * 一段步骤：标题可收起。问数据页一轮先建图再执行，两段各一个标题；执行开始后
 * 「规划」自动收成一行「规划 · 6.4 s · 4 步」。
 */
function StepSection({ label, steps, turn, defaultOpen, titled = true }: {
  label: string; steps: Step[]; turn: StreamTurn; defaultOpen: boolean; titled?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  // 相位变化时跟着默认值走（执行开始时把规划收起来）；用户手动点过就听用户的
  const touched = useRef(false)
  useEffect(() => { if (!touched.current) setOpen(defaultOpen) }, [defaultOpen])
  const n = countSteps(steps)
  const ms = steps.filter((s) => s.kind === 'lifecycle' && s.ms != null)
    .reduce((acc, s) => Math.max(acc, s.ms ?? 0), 0)
  const failed = steps.some(function bad(s): boolean {
    return s.status === 'failed' || (s.children?.some(bad) ?? false)
  })

  if (!titled) {
    return <div className="mt-1.5"><StepList steps={steps} depth={0} turnMs={turnMs(turn)} /></div>
  }
  return (
    <div className="mt-1.5">
      <button type="button"
              className="flex w-full items-center gap-1.5 py-0.5 text-2xs text-dim transition-colors hover:text-fg"
              aria-expanded={open}
              onClick={() => { touched.current = true; setOpen((v) => !v) }}>
        <ChevronRight size={11} aria-hidden className="shrink-0 transition-transform"
                      style={{ transform: open ? 'rotate(90deg)' : 'none' }} />
        <span className="font-medium tracking-wide">{label}</span>
        <span className="tnum">{[ms >= 10 ? formatDuration(ms) : '', `${n} 步`].filter(Boolean).join(' · ')}</span>
        {failed && !open && <StatusBadge status="failed" size={10} decorative />}
        <span className="ml-1 h-px flex-1" style={{ background: 'var(--hairline, var(--border))' }} />
      </button>
      {open && <div className="mt-0.5"><StepList steps={steps} depth={0} turnMs={turnMs(turn)} /></div>}
    </div>
  )
}

/**
 * 不必单列的生命周期行：「开始运行」「完成」「已取消」和轮次头说的是同一件事。
 * 「继续运行」带着从哪接着跑、谁发起的才留；老数据里光秃秃的一句，审批行上的
 * 「→ 已放行」已经说过了，而且步骤按节点归并之后它的位置也对不上真实的先后
 */
const redundant = (s: Step): boolean =>
  s.kind === 'lifecycle' && (s.title.startsWith('开始运行') || s.title === '完成'
    || ((s.title === '已取消' || s.title === '继续运行') && !s.sub))

/** 这一轮一共跑了多久，给耗时细条当分母 */
function turnMs(turn: StreamTurn): number | undefined {
  const fin = turn.elapsedMs ?? turn.steps.find((s) => s.kind === 'lifecycle' && s.title === '完成')?.ms
  if (fin) return fin
  const sum = turn.steps.reduce((acc, s) => acc + (s.kind !== 'lifecycle' ? s.ms ?? 0 : 0), 0)
  return sum || undefined
}

/**
 * 这次运行有什么不对。
 *
 * broken 用报警色：那意味着答案本身不可信，不是"仅供参考"。degraded 用普通
 * 边框——它能用，只是有缺口。两者混成一个样子，用户就学会了一概忽略。
 * 可信度相关的字一律不用最淡的那档灰：最需要被看到的「这条结论有问题」以前
 * 恰恰是整页最淡的字。
 */
function ReviewNote({ review }: { review: ReviewResult }) {
  const broken = review.severity === 'broken'
  const Icon = broken ? AlertTriangle : Info
  return (
    <div className="mt-2 rounded border px-2 py-1.5 text-2xs leading-relaxed"
         style={{
           borderColor: broken ? 'var(--st-failed)' : 'var(--border)',
           color: broken ? 'var(--st-failed)' : 'var(--text-dim)',
           background: broken ? 'var(--st-failed-soft)' : undefined,
         }}>
      <div className="flex items-start gap-1.5">
        <Icon size={11} className="mt-[2px] shrink-0" aria-hidden />
        <span className="whitespace-pre-wrap">{review.note}</span>
      </div>
      {!!review.signals.length && (
        <details className="mt-1 pl-[17px]">
          <summary className="cursor-pointer" style={{ color: 'var(--st-waiting)' }}>
            检测到 {review.signals.length} 处异常
          </summary>
          <ul className="mt-1 space-y-0.5 text-dim">
            {review.signals.map((s, i) => (
              <li key={i}>· {s.detail}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}

/** 拆好的一处报错：发生了什么、为什么、怎么办、该去哪儿改、原文 */
interface Failure {
  title: string
  reason?: string
  action?: string
  raw?: string
  fix?: FixKind | 'rerun'
  fixTo?: string
  fixFirst?: boolean
}

/** 这一轮是不是一次运行。Copilot 建图、发起就失败的，不能按运行的失败去讲（「接着跑」无从谈起） */
const isRunTurn = (turn: StreamTurn): boolean =>
  !!turn.runId || turn.steps.some((s) => s.stage !== 'plan')

function failureOf(turn: StreamTurn): Failure | null {
  const e = turn.error
  if (!e) return null
  if (typeof e === 'object' && 'title' in e) {
    return { title: e.title, reason: e.reason, action: e.hint, raw: e.detail || undefined,
             fix: e.fix, fixTo: e.fixTo, fixFirst: e.fixFirst }
  }
  const text = typeof e === 'string' ? e : e.error
  const detail = typeof e === 'string' ? undefined : e.detail
  const hint = typeof e === 'string' ? undefined : e.hint
  // 运行的失败和记录页、问数据页读同一份「为什么 + 怎么办」：同一次失败三处说法不一，
  // 人就不知道该信哪句、该点哪个按钮
  if (!hint && isRunTurn(turn)) {
    const x = explainRunError(text, detail)
    return { title: x.title, reason: x.reason, action: x.action, fix: x.fix, fixTo: x.fixTo, fixFirst: x.fixFirst,
             raw: x.raw && x.raw !== x.title ? x.raw : undefined }
  }
  const h = humanizeError(hint || detail ? { error: text, hint, detail } : text)
  return { title: h.title, reason: h.reason, action: h.action, raw: h.raw && h.raw !== h.title ? h.raw : undefined }
}

/**
 * 下一步的直达入口。要去画布上改的，编排页能直接打开那个节点的设置；别处只说该去哪儿，
 * 不装作能点。「接着跑」「重新运行」由页面自己放（renderTurnActions），这里不给
 */
function FixAction({ fix, nodeId, label, to, first }: {
  fix?: FixKind | 'rerun'; nodeId?: string; label?: string
  /** lib/explain 的 fixTo：直达要改的那一项 */
  to?: string
  /** lib/explain 的 fixFirst：先改好才接得下去，入口写明要改什么 */
  first?: boolean
}) {
  const { onOpen } = useContext(Ctx)
  const cls = 'inline-flex shrink-0 items-center gap-1 rounded px-1 text-2xs text-[var(--accent)] underline-offset-2 transition-colors hover:bg-hover hover:underline'
  if (fix === 'canvas' && nodeId && onOpen) {
    return (
      <button type="button" className={cls} data-fix="canvas" onClick={() => onOpen(nodeId)}>
        <Settings2 size={10} aria-hidden /> 打开{label ? `「${label}」的` : ''}设置
      </button>
    )
  }
  if (fix === 'settings') {
    return <Link to={to ?? '/settings/providers'} className={cls} data-fix="settings"><Settings2 size={10} aria-hidden /> 去模型接入</Link>
  }
  if (fix === 'tools') {
    return (
      <Link to={to ?? '/tools'} className={cls} data-fix="tools">
        <Wrench size={10} aria-hidden /> {first ? '去改参数定义' : '去工具库'}
      </Link>
    )
  }
  return null
}

/**
 * 轮次级的报错。样子和 ErrorState 的 compact 版一致，但先把话讲清再画：运行的失败走
 * lib/explain（模型没真调工具、团队轮数用完这类，给的是「去画布改」而不是「接着跑」），
 * 页面拆好的照原样，其余交给 humanizeError。出错的是哪个节点也写在这里，点一下画布取景
 */
function TurnError({ turn }: { turn: StreamTurn }) {
  const { onFocus } = useContext(Ctx)
  const f = useMemo(() => failureOf(turn), [turn])
  if (!f) return null
  // run.failed 那一行定位得到节点就用它（它在流里已经不单列了），否则第一个失败的节点
  const rf = findFirst(turn.steps, (s) => s.kind === 'error' && s.id.startsWith('rf-') && !!s.nodeId)
  const node = findFirst(turn.steps, (s) => s.kind === 'node' && s.status === 'failed' && !!s.nodeId)
  const whereId = rf?.nodeId ?? node?.nodeId
  const whereLabel = rf?.sub?.replace(/^出错的节点：/, '') || node?.title
  return (
    <div role="alert" data-turn-error=""
         className="mt-2 flex items-start gap-2 rounded-lg border px-3 py-2 text-xs"
         style={{ borderColor: 'color-mix(in srgb, var(--st-failed) 35%, var(--border))',
                  background: 'color-mix(in srgb, var(--st-failed) 6%, transparent)' }}>
      <AlertCircle size={14} className="mt-0.5 shrink-0" style={{ color: 'var(--st-failed)' }} aria-hidden />
      <div className="min-w-0 flex-1">
        <div className="font-medium text-fg [overflow-wrap:anywhere]">{f.title}</div>
        {whereId && whereLabel && (
          onFocus
            ? (
              <button type="button" className="mt-0.5 text-2xs text-dim underline-offset-2 hover:text-fg hover:underline"
                      title="在画布上定位这个节点" onClick={() => onFocus(whereId)}>
                出错的节点：「{whereLabel}」
              </button>
            )
            : <div className="mt-0.5 text-2xs text-dim">出错的节点：「{whereLabel}」</div>
        )}
        {f.reason && <div className="mt-0.5 leading-relaxed text-dim [overflow-wrap:anywhere]">{f.reason}</div>}
        {f.action && (
          <div className="mt-0.5 flex items-start gap-1 leading-relaxed text-dim">
            <CornerDownRight size={11} className="mt-[3px] shrink-0" aria-hidden />
            <span className="min-w-0 flex-1 [overflow-wrap:anywhere]">{f.action}</span>
          </div>
        )}
        <div className="flex flex-wrap items-center gap-x-2">
          <FixAction fix={f.fix} nodeId={whereId} label={whereLabel} to={f.fixTo} first={f.fixFirst} />
        </div>
        {f.raw && <div className="mt-1"><TechDetails raw={f.raw} /></div>}
      </div>
    </div>
  )
}

/** 思考的尾窗：只露最后几行并跟着滚，不然一大坨会把步骤挤到屏幕外。 */
function ThinkingTail({ text }: { text: string }) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (ref.current) ref.current.scrollTop = ref.current.scrollHeight
  }, [text])
  return (
    <div ref={ref}
         className="mt-2 max-h-20 overflow-y-auto whitespace-pre-wrap rounded bg-bg px-2 py-1.5 text-2xs italic leading-relaxed text-dim">
      {text}
    </div>
  )
}

// -------------------------------------------------------------------------
// 步骤
// -------------------------------------------------------------------------

/** 同一节点执行超过这么多次才按轮收起 */
const FOLD_FROM = 5

/** 一层超过这么多条时先只画一部分。1860 条事件的运行以前是 5970 个 DOM 节点 */
const LIST_CAP = 50
const LIST_HEAD = 20
/** 点名的那一行等展开之后再找，最多等这么多帧 */
const REVEAL_TRIES = 4

const notable = (s: Step): boolean =>
  s.status === 'failed' || s.status === 'running' || s.status === 'waiting'
  || s.level === 'error' || s.level === 'warn'

function StepList({ steps, depth, turnMs, fold = false }: {
  steps: Step[]; depth: number; turnMs?: number
  /** 已经做完的节点：只留出问题的和最后三条，其余收成一行 */
  fold?: boolean
}) {
  const [all, setAll] = useState(false)
  const rows = useMemo(() => compactSteps(steps), [steps])
  const { reveal } = useContext(Ctx)

  // 收起的是前面那些平平无奇的：最新的几条（运行中正在发生的就在这里）和出了
  // 问题的一律留着。反过来只画前 20 条的话，长运行跑到后面，正在跑的那一步反而
  // 被收进「展开其余」里看不见
  const keep = fold && rows.length > 6 ? 3 : rows.length > LIST_CAP ? LIST_HEAD : 0
  const shown = !all && keep ? rows.filter((s, i) => notable(s) || i >= rows.length - keep) : rows
  const hidden = rows.length - shown.length
  // 画布上点名要看的节点正好收在前面那一截里：先展开，上面的定位下一帧就找得到它
  useLayoutEffect(() => {
    if (!reveal || !hidden) return
    const kept = new Set(shown)
    if (rows.some((s) => !kept.has(s) && s.nodeId === reveal.nodeId)) setAll(true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reveal?.seq])

  return (
    <div className={clsx(depth > 0 && 'ml-[5px] border-l pl-2.5')}
         style={depth > 0 ? { borderColor: 'var(--hairline, var(--border))' } : undefined}>
      {hidden > 0 && (
        <button type="button"
                className="flex items-center gap-1 py-0.5 pl-[18px] text-2xs text-dim transition-colors hover:text-fg"
                onClick={() => setAll(true)}>
          <ChevronRight size={10} aria-hidden />
          展开前面的 {hidden} 条
        </button>
      )}
      {shown.map((step) => <StepRow key={step.id} step={step} depth={depth} turnMs={turnMs} />)}
    </div>
  )
}

/** 状态有自己剪影的就画剪影（全站同一套），其余的画这一步是什么 */
const BADGED = new Set(['running', 'waiting', 'failed', 'cancelled', 'suspended', 'skipped'])

function StepRow({ step, depth, turnMs }: { step: Step; depth: number; turnMs?: number }) {
  if (depth === 0 && step.kind === 'lifecycle' && step.stage !== 'plan') return <PhaseMark step={step} />
  return <StepLine step={step} depth={depth} turnMs={turnMs} />
}

/**
 * 执行里的分段线：「继续运行 · 由 张工 发起」。它标的是这次运行在时间线上的一个
 * 转折，不是一件做完或做砸的事——画成打勾或红叉就像多了一个步骤（失败的运行里
 * 「继续运行」以前是一个红叉，读起来像续跑本身出了错）
 */
function PhaseMark({ step }: { step: Step }) {
  const line = { background: 'var(--hairline, var(--border))' }
  return (
    <div className="flex items-center gap-1.5 py-1 text-2xs text-dim" data-phase-mark="">
      <span className="h-px w-2 shrink-0" style={line} />
      <Play size={9} aria-hidden className="shrink-0" fill="currentColor" />
      <span className="shrink-0 font-medium">{step.title}</span>
      {step.sub && <span className="min-w-0 truncate text-faint" title={step.sub}>{step.sub}</span>}
      <span className="h-px min-w-3 flex-1" style={line} />
      {step.meta && <span className="mono tnum shrink-0">{step.meta}</span>}
    </div>
  )
}

function StepLine({ step, depth, turnMs }: { step: Step; depth: number; turnMs?: number }) {
  const { dense, onHover, onFocus, activeNodeId, openArtifact, reduced } = useContext(Ctx)
  const [open, setOpen] = useState(false)
  const Icon = ICONS[step.kind] ?? CircleDot
  const result = step.result
  const expandable = !!(step.detail || result || step.raw || step.artifact)
  const table = result ? parseQueryResult(result) : null
  const status = step.status ?? 'done'
  // 失败的节点：收着的时候就说为什么（和轮次顶上的报错同一份 lib/explain），展开再看原因、
  // 怎么办和原话。以前这一行只有节点名和一个红叉，得点开才知道是没绑工具还是超时
  const explained = useMemo(() => (step.kind === 'node' && status === 'failed' && step.detail
    ? explainRunError(step.detail, step.raw) : null), [step.kind, status, step.detail, step.raw])
  const sub = step.sub ?? explained?.title

  // 失败要压过 kind：一条 lifecycle 跑挂了还画成打勾的"完成"图标，是把失败说成了成功
  const failed = status === 'failed' || step.level === 'error'
  const running = status === 'running'
  const top = depth === 0 && (step.kind === 'node' || step.kind === 'lifecycle')
  const color = failed ? 'var(--st-failed)'
    : status === 'waiting' ? 'var(--st-waiting)'
    : step.level === 'warn' ? 'var(--st-waiting)'
    : status === 'skipped' || status === 'cancelled' ? 'var(--text-faint)'
    : status === 'suspended' ? 'var(--st-suspended, var(--warn))'
    : running ? 'var(--st-running)'
    // 顶层节点是主干，子步骤是它的过程——靠明度分层，不然一屏全是同一个灰
    : top ? 'var(--text)'
    : 'var(--text-dim)'
  // 建图阶段加的节点是"放上去了"，不是"跑完了"：画成节点图标，不画完成的勾
  const badge = failed ? 'failed' : BADGED.has(status) ? status
    : top && step.kind === 'node' && step.stage !== 'plan' ? 'done' : null
  const linked = !!step.nodeId && (!!onHover || !!onFocus)
  const active = !!step.nodeId && step.nodeId === activeNodeId

  const toggle = () => {
    if (expandable) setOpen((v) => !v)
    if (step.nodeId && onFocus) onFocus(step.nodeId)
  }

  return (
    <div
      className={clsx(
        // scroll-mt：定位到这一行时别让吸顶的轮次头盖住它
        'scroll-mt-14 rounded py-[3px] transition-colors data-[flash]:outline data-[flash]:outline-1 data-[flash]:outline-offset-1',
        'data-[flash=failed]:outline-[color:var(--st-failed)] data-[flash=focus]:outline-[color:var(--accent)]',
        active && 'bg-hover',
        // 减少动效时扫光和转圈都停住，"在跑"改由左边一道强调色竖线说
        running && reduced && 'border-l-2 pl-1.5',
      )}
      style={running && reduced ? { borderColor: 'var(--st-running)' } : undefined}
      data-node-id={step.nodeId}
      data-step-status={failed ? 'failed' : status}
      data-step-code={step.code}
      aria-busy={running || undefined}
    >
      <button
        type="button"
        className={clsx('flex w-full items-start gap-1.5 text-left',
          (expandable || linked) && 'cursor-pointer hover:opacity-85')}
        style={{ color }}
        onClick={toggle}
        disabled={!expandable && !(step.nodeId && onFocus)}
        aria-expanded={expandable ? open : undefined}
        title={linked ? '点一下在画布上定位这个节点' : undefined}
      >
        <span className={clsx('shrink-0', dense ? 'mt-[3px]' : 'mt-[4px]')}>
          {badge
            ? <StatusBadge status={badge} size={11} decorative
                           style={badge === 'done' ? { color: 'var(--st-done)' } : undefined} />
            : <Icon size={11} aria-hidden />}
        </span>
        <span className="min-w-0 flex-1">
          <span className={clsx('block leading-relaxed [overflow-wrap:anywhere]',
            dense ? 'text-[11px]' : 'text-xs',
            step.kind === 'think' && 'italic',
            (status === 'skipped' || status === 'cancelled') && 'text-faint',
            // 一道光扫过文字。比转圈多说一件事——它在出东西，不只是在等
            running && !reduced && !step.children?.length && 'shimmer')}>
            {step.title}
          </span>
          {sub && (
            // 副标题平时是淡色斜体的旁白（这次调用前在想什么、为什么跳过）；出了事的行
            // 它说的是为什么，用正体、跟着行的颜色，别让最要紧的那句成了最淡的字
            <span className={clsx('block truncate text-2xs leading-snug',
                    failed || step.level === 'warn' ? 'not-italic' : 'italic text-faint')}
                  style={failed ? { color: 'var(--st-failed)' } : step.level === 'warn' ? { color: 'var(--text-dim)' } : undefined}
                  title={sub}>
              {sub}
            </span>
          )}
        </span>
        <StepMeta step={step} depth={depth} turnMs={turnMs} />
        {expandable && (
          <ChevronRight size={10} aria-hidden className="mt-[3px] shrink-0 opacity-50"
            style={{ transform: open ? 'rotate(90deg)' : 'none', transition: 'transform .15s' }} />
        )}
      </button>

      {step.next && (
        // 下一步不收进展开区：模型没真调工具、团队轮数用完这种，人要的就是这一句
        <div data-step-next="" className="flex flex-wrap items-start gap-x-1.5 pl-[18px] text-2xs leading-snug text-dim">
          <span className="flex min-w-0 flex-1 items-start gap-1">
            <CornerDownRight size={10} className="mt-[2px] shrink-0" aria-hidden />
            <span className="min-w-0 [overflow-wrap:anywhere]">{step.next}</span>
          </span>
          <FixAction fix={step.fix} nodeId={step.nodeId} />
        </div>
      )}

      {open && <StepDetail step={step} table={table} explained={explained} />}

      {/* 泳道排在子步骤**前面**：先给一眼看清的形状（谁做了哪几轮、哪些是
          同时进行的、各花多久），再往下是每一步的内容。反过来的话，得把
          十几行步骤读完才知道这个团队是怎么分工的 */}
      {step.team && <TeamLanes team={step.team} />}

      {(step.execs?.length ?? 0) > 1 && step.children?.length
        ? <ExecGroups step={step} turnMs={turnMs} />
        : !!step.children?.length && (
          <StepList steps={step.children} depth={depth + 1} turnMs={turnMs}
                    fold={step.kind === 'node' && status === 'done'} />
        )}
      {step.artifact && !open && step.kind === 'query' && (
        <button type="button"
                className="ml-[18px] mt-0.5 flex items-center gap-1 text-2xs text-dim transition-colors hover:text-fg"
                onClick={() => openArtifact(step.artifact!, step.title)}>
          <ShieldCheck size={10} aria-hidden /> 完整结果 {shortId(step.artifact, 8)}
          <ExternalLink size={9} aria-hidden />
        </button>
      )}
    </div>
  )
}

/**
 * 行尾：做完的写耗时（配一根按本轮总时长归一化的细条，慢在哪一眼能看出来），
 * 在跑的走实时计时。
 */
function StepMeta({ step, depth, turnMs }: { step: Step; depth: number; turnMs?: number }) {
  const { dense, reduced } = useContext(Ctx)
  const running = step.status === 'running'
  const failed = step.status === 'failed' || step.level === 'error'
  const showBar = !running && depth === 0 && step.kind === 'node' && step.ms != null
    && turnMs != null && turnMs > 0 && step.ms >= 10
  const share = showBar ? Math.min(1, step.ms! / turnMs!) : 0
  return (
    <>
      {showBar && (
        <span className={clsx('mt-[7px] h-[3px] shrink-0 overflow-hidden rounded-full', dense ? 'w-8' : 'w-12')}
              style={{ background: 'var(--bg-hover)' }} aria-hidden>
          <span className="block h-full origin-left rounded-full"
                style={{
                  transform: `scaleX(${Math.max(0.04, share)})`,
                  background: failed ? 'var(--st-failed)' : 'color-mix(in srgb, var(--text-dim) 70%, transparent)',
                }} />
        </span>
      )}
      {/* 「开始运行」那一行的秒表和轮次头部的是同一个数，不重复 */}
      {running && step.startedAt != null && step.kind !== 'lifecycle'
        ? <LiveClock since={step.startedAt} label={reduced ? '进行中' : undefined} limitS={step.limitS} />
        : (step.meta || STOPPED[step.status ?? '']) && !(running && step.startedAt != null) && (
          <span className={clsx('tnum mt-[1px] shrink-0 text-dim', dense ? 'text-[10px]' : 'text-2xs',
            step.meta && 'mono')}>
            {step.meta || STOPPED[step.status ?? '']}
          </span>
        )}
      {step.repeat && step.repeat.ms.length >= 3 && <Sparkline values={step.repeat.ms} />}
    </>
  )
}

/** 被停下的步骤在行尾说一声停在哪种状态：灰色方块和"做完了"光看图标分不太开 */
const STOPPED: Record<string, string> = {
  cancelled: statusLabel('cancelled', { short: true }),
  suspended: statusLabel('suspended', { short: true }),
}

/**
 * 进行中那一行的秒表。只有它订阅时钟，其余行不会跟着每 100ms 重画。
 * 带时限的工具越过上限就换成提醒色、说破「已超出」：引擎马上会放弃等待，秒表一声
 * 不吭地接着走，看着像还有指望
 */
function LiveClock({ since, label, limitS }: { since: number; label?: string; limitS?: number }) {
  const { dense, skewMs } = useContext(Ctx)
  const now = useRunClock(true)
  const ms = Math.max(0, now - skewMs - since)
  const over = limitS != null && ms > limitS * 1000
  return (
    <span className={clsx('mono tnum mt-[1px] shrink-0', dense ? 'text-[10px]' : 'text-2xs')}
          style={{ color: over ? 'var(--st-waiting)' : 'var(--st-running)' }}
          {...(over ? { 'data-over-limit': '', title: `这一步的时限是 ${formatNumber(limitS!)} s，到点会放弃等待` } : {})}>
      {over ? `已超出 ${formatNumber(limitS!)} s 上限 · ` : label ? `${label} · ` : ''}{formatClock(ms)}
    </span>
  )
}

/** 一串耗时的静态折线。不动画：它说的是已经发生的分布，不是正在发生的事 */
function Sparkline({ values }: { values: number[] }) {
  const pts = values.length > 200
    ? values.filter((_, i) => i % Math.ceil(values.length / 200) === 0)
    : values
  const max = Math.max(...pts, 1)
  const w = 48
  const h = 12
  const d = pts.map((v, i) => `${(i / Math.max(1, pts.length - 1)) * w},${h - (v / max) * (h - 1) - 0.5}`).join(' ')
  return (
    <svg width={w} height={h} className="mt-[2px] shrink-0" aria-hidden>
      <polyline points={d} fill="none" stroke="var(--text-faint)" strokeWidth="1" strokeLinejoin="round" />
    </svg>
  )
}

/**
 * 同一个节点执行了很多次：按轮折叠。
 *
 * 默认只展开最后一轮和出了问题的那几轮，其余收成一行摘要（几轮、中位、最慢、
 * 哪几轮有提醒），配一条每轮耗时的折线——最慢那一拍一眼就能看见。摘要里点名的
 * 那几轮（最慢那一次、有提醒的几次）点一下就列出来，不用去一百多轮里翻。
 */
function ExecGroups({ step, turnMs }: { step: Step; turnMs?: number }) {
  const groups = useMemo(() => childrenByExec(step), [step])
  const [expanded, setExpanded] = useState<Set<number>>(() => new Set())
  // 从摘要里点名要看的轮次：不展开全部也单独列出来
  const [pinned, setPinned] = useState<Set<number>>(() => new Set())
  const [all, setAll] = useState(false)
  // 展开全部时平平无奇的轮次分批列，每批 LIST_HEAD 轮
  const [limit, setLimit] = useState(LIST_HEAD)
  const [reveal, setReveal] = useState<{ n: number; seq: number } | null>(null)
  const boxRef = useRef<HTMLDivElement>(null)
  const bad = (g: { exec: Exec; steps: Step[] }) =>
    g.exec.status === 'failed' || g.steps.some((s) => notable(s) && s.status !== 'running')
  const lastN = groups[groups.length - 1]?.exec.n
  // 几轮而已（驳回两次再放行）就全摊开：分轮本身已经把边界画清楚了，收起来反而把
  // 那两次驳回藏了。轮询、逐拍这种几十上百轮的才收
  const few = groups.length <= FOLD_FROM
  const auto = (g: { exec: Exec; steps: Step[] }) =>
    few || g.exec.n === lastN || g.exec.status === 'running' || g.exec.status === 'waiting'
    || g.exec.status === 'failed' || g.steps.some((s) => s.level === 'error' || s.status === 'failed')
  const ms = groups.map((g) => g.exec.ms ?? 0)
  const st = spread(ms)
  const slowest = st.maxAt != null ? groups[st.maxAt]?.exec.n : undefined
  const warned = groups.filter((g) => g.steps.some((s) => s.level === 'warn')).map((g) => g.exec.n)
  const quiet = groups.filter((g) => !auto(g))
  const loose = quiet.filter((g) => !pinned.has(g.exec.n))
  const paged = all && loose.length > LIST_CAP

  // 要紧的轮次和点名要看的一直在；其余的只有展开全部时才按先后分批列。没列出来的
  // 那一截在它开始的地方留一行，说清还有多少、怎么继续看——不假装"已经全在上面了"
  const rows: ({ kind: 'exec'; g: { exec: Exec; steps: Step[] } } | { kind: 'more'; left: number })[] = []
  let listed = 0
  for (const g of groups) {
    const keep = auto(g) || pinned.has(g.exec.n)
    if (!keep && !all) continue
    if (!keep && paged && ++listed > limit) {
      if (listed === limit + 1) rows.push({ kind: 'more', left: loose.length - limit })
      continue
    }
    rows.push({ kind: 'exec', g })
  }
  const hiddenNow = groups.length - rows.filter((r) => r.kind === 'exec').length

  // 点名的轮次里本来就列着的（最后一轮、出错的）不用再钉，只需要滚过去
  const own = (ns: number[]) => ns.filter((n) => {
    const g = groups.find((x) => x.exec.n === n)
    return !!g && !auto(g)
  })
  const namedOn = (ns: number[]) => own(ns).length > 0 && own(ns).every((n) => pinned.has(n))
  /** 点名看某几轮：列出来、展开，并把第一轮滚进视野。再点一次收回去 */
  const toggleNamed = (ns: number[]) => {
    if (!ns.length) return
    const on = namedOn(ns)
    const flip = (prev: Set<number>) => {
      const next = new Set(prev)
      for (const n of own(ns)) { if (on) next.delete(n); else next.add(n) }
      return next
    }
    setPinned(flip)
    setExpanded(flip)
    if (!on) setReveal((r) => ({ n: ns[0], seq: (r?.seq ?? 0) + 1 }))
  }
  useLayoutEffect(() => {
    if (!reveal) return
    const el = boxRef.current?.querySelector<HTMLElement>(`[data-exec="${reveal.n}"]`)
    if (!el) return
    el.scrollIntoView({ block: 'nearest' })
    return flash(el, 'focus')
  }, [reveal])

  return (
    <div ref={boxRef} data-exec-groups={step.nodeId ?? ''} className="ml-[5px] border-l pl-2.5"
         style={{ borderColor: 'var(--hairline, var(--border))' }}>
      {quiet.length > 0 && (
        <div className="flex items-start gap-1.5 py-[3px] text-2xs text-dim">
          <span className="min-w-0 flex-1">
            <span className="flex min-w-0 items-center gap-1">
              <button type="button"
                      className="flex min-w-0 items-center gap-1.5 text-left transition-colors hover:text-fg"
                      aria-expanded={all}
                      onClick={() => {
                        // 收起就是字面意思：只剩要紧的那几轮，点名列出来的也收回去
                        if (all) { setPinned(new Set()); setLimit(LIST_HEAD) }
                        setAll((v) => !v)
                      }}>
                <ChevronRight size={10} aria-hidden className="shrink-0 transition-transform"
                              style={{ transform: all ? 'rotate(90deg)' : 'none' }} />
                <span className="truncate">{all ? '收起，只看要紧的几轮' : `另外 ${hiddenNow} 轮`}</span>
              </button>
              {warned.length > 0 && (
                <button type="button" aria-pressed={namedOn(warned)}
                        className="shrink-0 rounded px-0.5 underline-offset-2 transition-colors hover:bg-hover hover:underline"
                        style={{ color: 'var(--st-waiting)' }}
                        title={namedOn(warned) ? '收回这几轮' : '把有提醒的几轮列出来'}
                        onClick={() => toggleNamed(warned)}>
                  · {warned.length} 轮有提醒
                </button>
              )}
            </span>
            {st.max != null && st.max >= 10 && slowest != null && (
              <span className="tnum flex min-w-0 items-center pl-[16px] text-faint">
                {st.median != null && st.median >= 10 && <span className="shrink-0">中位 {formatDuration(st.median)} ·&nbsp;</span>}
                <button type="button" aria-pressed={namedOn([slowest])}
                        className="truncate rounded px-0.5 text-left underline-offset-2 transition-colors hover:bg-hover hover:text-fg hover:underline"
                        title={namedOn([slowest]) ? '收回这一轮' : '把这一轮列出来'}
                        onClick={() => toggleNamed([slowest])}>
                  最慢 {formatDuration(st.max)}（第 {slowest} 次）
                </button>
              </span>
            )}
          </span>
          {(st.max ?? 0) >= 10 && ms.length >= 3 && <Sparkline values={ms} />}
        </div>
      )}
      {rows.map((r) => {
        if (r.kind === 'more') {
          return (
            <div key="more" data-exec-more="" className="flex flex-wrap items-center gap-x-2 py-0.5 pl-[18px] text-2xs text-dim">
              <span className="tnum">还有 {r.left} 轮没列出</span>
              <button type="button" className="rounded px-0.5 underline-offset-2 transition-colors hover:bg-hover hover:text-fg hover:underline"
                      onClick={() => setLimit((l) => l + LIST_HEAD)}>
                再列 {Math.min(LIST_HEAD, r.left)} 轮
              </button>
              <button type="button" className="rounded px-0.5 underline-offset-2 transition-colors hover:bg-hover hover:text-fg hover:underline"
                      onClick={() => setLimit(Infinity)}>
                全部列出
              </button>
            </div>
          )
        }
        const { g } = r
        const open = auto(g) || expanded.has(g.exec.n)
        return (
          <div key={g.exec.n} data-exec={g.exec.n}
               className="scroll-mt-14 rounded data-[flash]:outline data-[flash]:outline-1 data-[flash]:outline-offset-1 data-[flash=focus]:outline-[color:var(--accent)]">
            <button type="button"
                    className="flex w-full items-center gap-1.5 py-[2px] text-left text-2xs text-dim transition-colors hover:text-fg"
                    aria-expanded={open}
                    onClick={() => setExpanded((prev) => {
                      const next = new Set(prev)
                      if (next.has(g.exec.n)) next.delete(g.exec.n)
                      else next.add(g.exec.n)
                      return next
                    })}>
              <StatusBadge status={g.exec.status === 'done' ? 'done' : g.exec.status} size={9} decorative
                           animate={g.exec.status === 'running'} />
              <span className="tnum">第 {g.exec.n} 次</span>
              {g.exec.iteration != null && g.exec.iteration !== g.exec.n && (
                <span className="tnum text-faint">· 循环第 {g.exec.iteration} 轮</span>
              )}
              <span className="flex-1" />
              {g.exec.ms != null && g.exec.ms >= 10 && (
                <span className="mono tnum">{formatDuration(g.exec.ms)}</span>
              )}
              {bad(g) && <AlertTriangle size={10} aria-hidden style={{ color: 'var(--st-waiting)' }} />}
            </button>
            {open && g.steps.length > 0 && <StepList steps={g.steps} depth={1} turnMs={turnMs} />}
          </div>
        )
      })}
    </div>
  )
}

/** 展开区：跑的是什么（SQL、代码、参数）+ 跑出了什么 + 技术细节 + 完整证据 */
function StepDetail({ step, table, explained }: {
  step: Step; table: Table | null
  /** 失败节点按 lib/explain 讲的为什么、怎么办 */
  explained?: { reason?: string; action?: string; fix?: FixKind | 'rerun'; fixTo?: string; fixFirst?: boolean } | null
}) {
  const { openArtifact, dense } = useContext(Ctx)
  const pre = clsx('mono max-h-40 overflow-auto whitespace-pre-wrap rounded bg-bg px-2 py-1.5 leading-relaxed text-dim [overflow-wrap:anywhere]',
    dense ? 'text-[10.5px]' : 'text-2xs')
  const result = step.result
  return (
    <div className="mb-1 mt-1 space-y-1.5 pl-[18px]">
      {explained && (explained.reason || explained.action) && (
        <div className="space-y-0.5 text-2xs leading-relaxed text-dim" data-step-explain="">
          {explained.reason && <div className="[overflow-wrap:anywhere]">{explained.reason}</div>}
          {explained.action && (
            <div className="flex flex-wrap items-start gap-x-1.5">
              <span className="flex min-w-0 flex-1 items-start gap-1">
                <CornerDownRight size={10} className="mt-[3px] shrink-0" aria-hidden />
                <span className="min-w-0 [overflow-wrap:anywhere]">{explained.action}</span>
              </span>
              <FixAction fix={explained.fix} nodeId={step.nodeId} label={step.title}
                         to={explained.fixTo} first={explained.fixFirst} />
            </div>
          )}
        </div>
      )}
      {step.detail && (
        <div className="group/detail relative">
          {step.kind === 'query' && (
            <div className="mb-0.5 flex items-center gap-2 text-2xs text-dim">
              <span className="mono">SQL</span>
              {step.source && <span>数据源 {step.source}</span>}
              <span className="flex-1" />
              <CopyChip label="复制 SQL" text={() => step.detail ?? ''} />
            </div>
          )}
          <pre className={pre}>
            {step.detail}
          </pre>
        </div>
      )}
      {table
        ? <ResultTable table={table} artifact={step.artifact} title={step.title} />
        : result && result !== step.detail && (
          <pre className={pre}>
            {result.slice(0, 2000)}
          </pre>
        )}
      {step.artifact && !table && (
        <button type="button"
                className="flex items-center gap-1 text-2xs text-dim transition-colors hover:text-fg"
                onClick={() => openArtifact(step.artifact!, step.title)}>
          <ShieldCheck size={10} aria-hidden /> 看完整证据 {shortId(step.artifact, 8)}
          <ExternalLink size={9} aria-hidden />
        </button>
      )}
      {step.raw && <TechDetails raw={step.raw} />}
    </div>
  )
}

/** 技术细节：原始异常。给排查和转给维护者用，默认收着 */
function TechDetails({ raw }: { raw: string }) {
  const { dense } = useContext(Ctx)
  return (
    <details className="group/raw">
      <summary className="cursor-pointer text-2xs text-dim hover:text-fg">技术细节</summary>
      <div className="mt-1 flex items-start gap-1">
        <pre className={clsx('mono max-h-40 min-w-0 flex-1 overflow-auto whitespace-pre-wrap rounded bg-bg px-2 py-1.5 leading-relaxed text-dim [overflow-wrap:anywhere]',
          dense ? 'text-[10.5px]' : 'text-2xs')}>
          {raw}
        </pre>
        <CopyChip label="复制" text={() => raw} />
      </div>
    </details>
  )
}

// -------------------------------------------------------------------------
// 协作团队的泳道
// -------------------------------------------------------------------------

/**
 * 每个成员一行，每一轮一格。
 *
 * 这个节点以前在界面上是一条扁平的步骤序列——能看出"谁回了什么"，看不出
 * "谁和谁是同时干的"。而一轮同时派几个人正是它相对单 agent 的全部优势，
 * 不画出来等于没有。
 *
 * 条形的宽度按耗时归一化，所以一眼能看出谁是这一轮的瓶颈。说法和配色跟画布上
 * 的协作矩阵是同一套：完成是 --st-done，进行中写「进行中」、不写「0 ms」，
 * 省下的时间要等那一轮的人都交回来才出现——之前刚开始就写「省下 0ms」，是在
 * 报一个还没发生的收益。
 */
function TeamLanes({ team }: { team: TeamRun }) {
  const { dense } = useContext(Ctx)
  const [openKey, setOpenKey] = useState<string | null>(null)
  if (!team.rounds.length) return null

  // 所有格子共用一个时间标尺，否则"快的那格"和"慢的那格"画得一样长，
  // 瓶颈就看不出来了
  const slowest = Math.max(1, ...team.rounds.flatMap((r) => r.members.map((m) => m.ms)))
  const parallelRounds = team.rounds.filter((r) => r.parallel > 1).length
  const liveRound = [...team.rounds].reverse().find((r) => r.members.some((m) => m.status === 'running'))
  const verdict = teamVerdictOf(team)
  // 一次都没派到的成员也占一行：团队「没做完」时，最该被看见的往往就是那个从没上过场的
  const never = (verdict?.never ?? []).filter((n) => !team.members.includes(n))
  const rounds = verdict?.rounds ?? team.rounds.length
  const ending = verdict?.outcome === 'failed'
    ? { text: `用完 ${rounds} 轮仍未完成`, color: 'var(--st-failed)' }
    : verdict?.outcome === 'degraded'
      ? { text: `用完 ${rounds} 轮仍未完成 · 按降档交付`, color: 'var(--st-waiting)' }
    : verdict?.closing && verdict.done
      ? { text: '轮数用完 · 调度者判定：已完成', color: 'var(--text-dim)' }
    : verdict?.closing && verdict.done === false
      ? { text: '轮数用完 · 调度者判定：未完成', color: 'var(--st-waiting)' }
    // 还在判定。运行在这时停了（取消、失败）团队会被收成 finished，就不再说「在判定」
    : verdict?.closing && !team.finished
      ? { text: '轮数用完 · 调度者在判定…', color: 'var(--st-running)' }
    : null
  const laneLabel = clsx('shrink-0 truncate text-2xs text-dim', dense ? 'w-14' : 'w-20')

  return (
    <div className="mt-2 rounded-lg border bg-bg p-2" data-team-lanes="">
      <div className="mb-2 flex flex-wrap items-center gap-x-2.5 gap-y-1 text-2xs text-dim">
        <span className="flex items-center gap-1">
          <Users size={11} aria-hidden /> {team.members.length + never.length} 名成员 · {team.rounds.length} 轮
        </span>
        {liveRound && (
          <span style={{ color: 'var(--st-running)' }}>
            第 {liveRound.round + 1} 轮 · {liveRound.parallel > 1 ? `本轮 ${liveRound.parallel} 人并行中` : '进行中'}
          </span>
        )}
        {team.savedMs > 0 && (
          <span className="tnum" style={{ color: 'var(--st-done)' }}>
            并行省下 {formatDuration(team.savedMs)}（{parallelRounds} 轮并行）
          </span>
        )}
        {parallelRounds === 0 && team.finished && !verdict?.outcome && (
          // 全程串行不是故障，但值得说一句——任务本来就互相依赖时它就该是串行的
          <span title="调度者每轮只派了一个人：后一步依赖前一步的结论">全程串行</span>
        )}
      </div>
      {ending && (
        // 怎么收的尾单独一行：只说「3 轮」的话，调度者说完成了和轮数用完硬停长得一样
        <div className="-mt-1 mb-2 flex items-start gap-1 text-2xs font-medium leading-snug" style={{ color: ending.color }}
             data-team-ending={verdict?.outcome ?? (verdict?.done ? 'done' : verdict?.done === false ? 'undone' : 'judging')}>
          {verdict?.outcome && <AlertTriangle size={11} className="mt-[1px] shrink-0" aria-hidden />}
          <span className="min-w-0 [overflow-wrap:anywhere]">
            {ending.text}
            {verdict?.reason && <span className="font-normal text-dim">：{verdict.reason}</span>}
          </span>
        </div>
      )}

      <div className="space-y-1">
        {team.members.map((name) => (
          <div key={name} className="flex items-center gap-1.5">
            <div className={clsx('shrink-0 truncate text-2xs text-dim', dense ? 'w-14' : 'w-20')}
                 title={name}>
              {name}
            </div>
            <div className="flex min-w-0 flex-1 gap-1">
              {team.rounds.map((r) => {
                const m = r.members.find((x) => x.agent === name)
                const key = `${r.round}:${name}`
                const running = m?.status === 'running'
                // 被停下的（取消、服务重启）也不知道本来要多久：同样画满一格斜纹，只是灰的
                const stopped = m?.status === 'cancelled' || m?.status === 'suspended'
                const bar = m?.status === 'done' ? 'var(--st-done)'
                  : m?.status === 'failed' ? 'var(--st-failed)'
                  : running ? 'var(--st-running)'
                  : 'var(--text-faint)'
                const hatched = running || stopped
                const said = running ? '进行中' : stopped ? statusLabel(m!.status, { short: true })
                  : m?.status === 'failed' ? `失败 · ${formatDuration(m.ms)}` : formatDuration(m?.ms)
                return (
                  <div key={r.round} className="min-w-0 flex-1 rounded-sm" style={{ background: 'var(--bg-hover)' }}>
                    {m ? (
                      <button
                        type="button"
                        className="block h-4 w-full rounded-sm text-left transition-opacity hover:opacity-80"
                        title={`第 ${r.round + 1} 轮 · ${m.instruction || '（无指令）'}`}
                        aria-label={`${name} 第 ${r.round + 1} 轮：${said}`}
                        onClick={() => setOpenKey(openKey === key ? null : key)}
                        style={{
                          // 还在跑的不知道要多久：画满一格的斜纹，不画成一根按比例的条
                          background: hatched
                            ? `repeating-linear-gradient(135deg, color-mix(in srgb, ${bar} ${stopped ? 28 : 40}%, transparent) 0 4px, color-mix(in srgb, ${bar} ${stopped ? 10 : 18}%, transparent) 4px 8px)`
                            : bar,
                          // 宽度按耗时占比，但留一个下限，否则快的那格细到看不见
                          width: hatched ? '100%' : `${Math.max(18, (m.ms / slowest) * 100)}%`,
                        }}
                      />
                    ) : <div className="h-4" />}
                  </div>
                )
              })}
            </div>
          </div>
        ))}

        {never.map((name) => (
          <div key={`never-${name}`} className="flex items-center gap-1.5" data-never-dispatched={name}>
            <div className={clsx(laneLabel, 'text-faint line-through decoration-dotted')} title={`${name}：一次都没被派到`}>
              {name}
            </div>
            <div className="flex h-4 min-w-0 flex-1 items-center gap-1 rounded-sm border border-dashed px-1.5 text-2xs text-dim"
                 style={{ borderColor: 'var(--border-strong, var(--border))' }}>
              <UserX size={10} aria-hidden className="shrink-0" />
              <span className="truncate">没派到：{rounds} 轮里调度者一次都没派给它</span>
            </div>
          </div>
        ))}

        <div className="flex items-center gap-1.5 pt-0.5">
          <div className={clsx('shrink-0', dense ? 'w-14' : 'w-20')} />
          <div className="flex min-w-0 flex-1 gap-1">
            {team.rounds.map((r) => {
              const live = r.members.some((m) => m.status === 'running')
              // 没收齐就停下的一轮（取消、服务重启）不知道本来要多久：写怎么停的。以前写
              // 「第1轮 · 0 ms」，有人先交回的话还写成先交回那人的耗时，像是这一轮跑完了
              const stopped = r.members.find((m) => m.status === 'cancelled' || m.status === 'suspended')
              return (
                <div key={r.round}
                     className={clsx('tnum min-w-0 flex-1 truncate text-center text-dim', dense ? 'text-[10px]' : 'text-2xs')}
                     title={r.reason || ''}>
                  {/* 窄栏放不下完整措辞，但"并2"这种缩写没人看得懂，
                      宁可只留轮次和耗时，并行与否看上面那行条形本来就一目了然 */}
                  {`第${r.round + 1}轮`}
                  {r.parallel > 1 && !dense ? ` · ${r.parallel} 人并行` : ''}
                  {' · '}{live ? '进行中'
                    : stopped ? statusLabel(stopped.status, { short: true })
                    : r.wallMs > 0 ? formatDuration(r.wallMs) : NONE}
                </div>
              )
            })}
          </div>
        </div>
      </div>

      {openKey && (() => {
        const [rd, name] = openKey.split(':')
        const m = team.rounds.find((r) => String(r.round) === rd)
          ?.members.find((x) => x.agent === name) as TeamMemberEx | undefined
        if (!m) return null
        return (
          <div className="mt-2 rounded border bg-panel p-2 text-2xs leading-relaxed">
            <div className="mb-1 text-dim">
              {name} · 第 {Number(rd) + 1} 轮 · {m.status === 'running' ? '进行中'
                : m.status === 'cancelled' || m.status === 'suspended' ? statusLabel(m.status, { short: true })
                : m.status === 'failed' ? `失败 · ${formatDuration(m.ms)}`
                : formatDuration(m.ms)}
            </div>
            {m.instruction && (
              <div className="mb-1.5 text-dim">任务：{m.instruction}</div>
            )}
            {m.error && (
              <div className="mb-1.5 [overflow-wrap:anywhere]" style={{ color: 'var(--st-failed)' }}>为什么失败：{m.error}</div>
            )}
            {m.result && (
              <pre className="mono max-h-36 overflow-auto whitespace-pre-wrap text-dim">
                {m.result.slice(0, 1200)}
              </pre>
            )}
          </div>
        )
      })()}
    </div>
  )
}

// -------------------------------------------------------------------------
// 查询结果、完整证据
// -------------------------------------------------------------------------

/** 值是不是数：数值列右对齐、等宽数字，小数点才对得齐。0 打头的串是编号，不算 */
const isNumeric = (v: unknown): boolean =>
  typeof v === 'number' || (typeof v === 'string' && /^[-+]?\d[\d,]*(\.\d+)?%?$/.test(v.trim())
    && !LEADING_ZERO.test(v.trim()))

/** 这一列能不能当"量"：列名不像编号 / 分组，值全是数 */
const isMetricColumn = (name: string, rows: unknown[][], j: number): boolean =>
  !CODE_COLUMN.test(name) && rows.length > 0 && rows.every((r) => r[j] == null || isNumeric(r[j]))

function toCsv(columns: string[], rows: unknown[][]): string {
  const cell = (v: unknown) => {
    const t = v == null ? '' : typeof v === 'object' ? JSON.stringify(v) : String(v)
    return /[",\n]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t
  }
  return [columns, ...rows].map((r) => r.map(cell).join(',')).join('\n')
}

function download(name: string, text: string, type: string) {
  // 带 BOM：Excel 按 GBK 猜编码，不带的话中文列名全是乱码
  const blob = new Blob([type.includes('csv') ? '\uFEFF' + text : text], { type })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = name
  a.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

const fileSafe = (s: string) => s.replace(/[\\/:*?"<>|\s]+/g, '_').slice(0, 40) || '结果'

/** 查询结果画成表格。一屏 JSON 谁也看不出名堂，表格能一眼看到形状。 */
function ResultTable({ table, artifact, title, full = false }: {
  table: Table; artifact?: string; title?: string
  /** 完整证据里：不截列、行数放宽 */
  full?: boolean
}) {
  const { dense, openArtifact } = useContext(Ctx)
  const maxCols = full ? table.columns.length : dense ? 4 : 8
  const maxRows = full ? 500 : 12
  const cols = table.columns.slice(0, maxCols)
  const hiddenCols = table.columns.length - cols.length
  const rows = table.rows.slice(0, maxRows)
  const numeric = cols.map((c, j) => isMetricColumn(c, rows, j))

  return (
    <div className="overflow-hidden rounded border">
      <div className={clsx('overflow-x-auto', full && 'max-h-[60vh] overflow-y-auto')}>
        <table className={clsx('w-full', dense && !full ? 'text-[10px]' : 'text-2xs')}>
          <thead className={clsx(full && 'sticky top-0')}>
            <tr className="border-b bg-elev">
              {cols.map((c, j) => (
                <th key={c} className={clsx('mono px-1.5 py-1 font-medium text-dim',
                  numeric[j] ? 'text-right' : 'text-left')}>{c}</th>
              ))}
              {hiddenCols > 0 && <th className="px-1.5 py-1 text-dim">+{hiddenCols}</th>}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i} className="border-b last:border-0">
                {row.slice(0, maxCols).map((cell, j) => (
                  <td key={j} className={clsx('mono max-w-[160px] truncate px-1.5 py-1 text-dim',
                    numeric[j] && 'tnum text-right')}
                      title={cell == null ? '' : String(cell)}>
                    {cell == null ? <span className="opacity-40">null</span>
                      : typeof cell === 'object' ? JSON.stringify(cell) : String(cell)}
                  </td>
                ))}
                {hiddenCols > 0 && <td className="px-1.5 py-1 text-dim">…</td>}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 border-t px-1.5 py-1 text-2xs leading-relaxed text-dim">
        <span className="tnum">
          {[
            rows.length < table.rows.length
              ? `显示前 ${formatNumber(rows.length)} / ${formatNumber(table.rows.length)} 行`
              : `${formatNumber(table.rows.length)} 行`,
            hiddenCols > 0 ? `显示前 ${cols.length} / ${table.columns.length} 列` : null,
          ].filter(Boolean).join(' · ')}
        </span>
        {/* 两种"不全"性质不同：一个是 guard 主动限的，一个是存预览时切的。
            混成一句"已截断"，用户没法判断该去调查询还是去看完整工件。 */}
        {table.truncated && (
          <span style={{ color: 'var(--st-waiting)' }}>查询撞了行数上限，库里还有更多</span>
        )}
        {table.clipped && (artifact
          ? (
            <button type="button" className="underline decoration-dotted underline-offset-2 hover:text-fg"
                    onClick={() => openArtifact(artifact, title)}>
              这里只是预览，打开完整结果
            </button>
          )
          : <span>这里只是预览，实际取回的行数更多</span>)}
        <span className="flex-1" />
        <button type="button" className="inline-flex items-center gap-1 rounded px-1 transition-colors hover:bg-hover hover:text-fg"
                title="导出为 CSV（Excel 可直接打开）"
                onClick={() => download(`${fileSafe(title ?? '查询结果')}.csv`, toCsv(table.columns, table.rows), 'text/csv;charset=utf-8')}>
          <Download size={10} aria-hidden /> CSV
        </button>
        {artifact && !table.clipped && !full && (
          <button type="button" className="inline-flex items-center gap-1 rounded px-1 transition-colors hover:bg-hover hover:text-fg"
                  onClick={() => openArtifact(artifact, title)}>
            <ShieldCheck size={10} aria-hidden /> 完整结果
          </button>
        )}
      </div>
    </div>
  )
}

/**
 * 完整证据：按内容哈希取回工件（后端取回时复验哈希，对不上会报 409），
 * 表格全量显示、可导出，文本等宽显示、可复制。
 *
 * 解码器早就给每一步记了工件 id，接口也一直在，界面从没渲染过——「每一步都有
 * 工件、数字可回指」这个区别于普通问数工具的卖点，以前在界面上看不见。
 * 导出给运行页的工件清单复用：id 为 null 时不渲染。
 */
export function ArtifactViewer({ id, title, onClose }: { id: string | null; title?: string; onClose: () => void }) {
  const [state, setState] = useState<{ id: string; content?: unknown; error?: unknown } | null>(null)
  useEffect(() => {
    if (!id) return
    let alive = true
    setState({ id })
    api.artifact(id).then(
      (res) => { if (alive) setState({ id, content: res.content }) },
      (error) => { if (alive) setState({ id, error }) },
    )
    return () => { alive = false }
  }, [id])

  const content = state?.id === id ? state?.content : undefined
  const error = state?.id === id ? state?.error : undefined
  const body = useMemo(() => (content === undefined ? null : unwrapArtifact(content)), [content])

  return (
    <Modal open={!!id} onClose={onClose} width={920}
           title={
             <span className="flex min-w-0 items-center gap-2">
               <span className="truncate">{title || '完整证据'}</span>
               {id && <span className="mono shrink-0 text-2xs text-dim">证据 {shortId(id, 8)}</span>}
               {content !== undefined && (
                 <span className="inline-flex shrink-0 items-center gap-1 text-2xs" style={{ color: 'var(--st-done)' }}
                       title="取回时按内容哈希复验过，和运行时记录的一致">
                   <ShieldCheck size={11} aria-hidden /> 哈希已校验
                 </span>
               )}
             </span>
           }>
      {error ? <ErrorNotice error={error} />
        : !body ? <Skeleton rows={6} />
        : (
          <div className="space-y-2" data-artifact="">
            {body.sql && (
              // 证据得连着"问的是什么"：只给结果表，读的人没法判断它是不是自己以为的那条查询
              <div>
                <div className="mb-0.5 flex items-center gap-2 text-2xs text-dim">
                  <span className="mono">SQL</span>
                  {body.tool && <span className="mono">{body.tool}</span>}
                  <span className="flex-1" />
                  <CopyChip label="复制 SQL" text={() => body.sql ?? ''} />
                </div>
                <pre className="mono max-h-32 overflow-auto whitespace-pre-wrap rounded bg-bg px-2 py-1.5 text-2xs leading-relaxed text-dim [overflow-wrap:anywhere]">
                  {body.sql}
                </pre>
              </div>
            )}
            {body.table ? <ResultTable table={body.table} full artifact={id ?? undefined} title={title} />
              : (
                <div>
                  <div className="mb-1 flex justify-end"><CopyChip label="复制全文" text={() => body.text} /></div>
                  <pre className="mono max-h-[60vh] overflow-auto whitespace-pre-wrap rounded bg-bg p-2 text-2xs leading-relaxed text-dim [overflow-wrap:anywhere]">
                    {body.text}
                  </pre>
                </div>
              )}
          </div>
        )}
    </Modal>
  )
}

/**
 * 工件里装的是什么。工具调用的快照是 {tool, args, result}，查询的 result 还是一段
 * JSON 字符串；节点产出是它自己的输出对象。认得出结果集就画成表，其余等宽显示
 */
function unwrapArtifact(content: unknown): { table: Table | null; text: string; sql?: string; tool?: string } {
  let payload = content
  let sql: string | undefined
  let tool: string | undefined
  if (content && typeof content === 'object' && 'result' in content && ('tool' in content || 'args' in content)) {
    const snap = content as { tool?: unknown; args?: { sql?: unknown }; result?: unknown }
    payload = snap.result
    tool = typeof snap.tool === 'string' ? snap.tool : undefined
    sql = typeof snap.args?.sql === 'string' ? snap.args.sql : undefined
  }
  const table = typeof payload === 'string' ? parseQueryResult(payload)
    : payload && typeof payload === 'object' && Array.isArray((payload as any).columns)
      && Array.isArray((payload as any).rows)
      ? { columns: (payload as any).columns, rows: (payload as any).rows, truncated: !!(payload as any).truncated }
      : null
  const text = table ? '' : typeof payload === 'string' ? payload : JSON.stringify(payload, null, 2)
  return { table, text, ...(sql ? { sql } : {}), ...(tool ? { tool } : {}) }
}

// -------------------------------------------------------------------------
// 成果
// -------------------------------------------------------------------------

function Output({ output, runClass, broken, question, onFollowUp }: {
  output: Record<string, any>; runClass?: string; broken?: boolean
  question?: string; onFollowUp?: (text: string) => void
}) {
  // _issuance 这类下划线开头的是内部字段，不是给人看的成果。
  // 空值也要滤掉：output 里留一个 {"result": ""} 很常见（图跑通了但出口
  // 没接上），它会渲染出一条什么都没有的分隔线——用户只会以为界面坏了
  const entries = Object.entries(output)
    .filter(([k, v]) => !k.startsWith('_') && !isBlank(v))
  const issuance = (output as any)._issuance
  const marks = useMemo(() => marksOf(issuance), [issuance])
  const follow = useMemo(() => (onFollowUp ? followUpsOf(entries.map(([, v]) => v)) : []),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [output, onFollowUp])
  if (!entries.length && !issuance) return null

  return (
    <div className="group/answer relative mt-2 space-y-2 border-t pt-2">
      {issuance && <IssuanceBanner issuance={issuance} runClass={runClass} />}
      {broken && (
        // 复核判了不可信：答案照常给（可能有参考价值），但先说清楚它不能当结论。
        // 不降透明度——那会让它更难读，而不是更不可信
        <div className="flex items-center gap-1.5 text-2xs font-medium" style={{ color: 'var(--st-failed)' }}>
          <AlertTriangle size={11} aria-hidden /> 以下内容不能当结论用
        </div>
      )}
      {/* 复制 / 导出贴着正文浮出，不贴着整块：上面有出具横幅时，贴整块的右上角会
          正好压在横幅的「回指 N 个数字」上 */}
      <div className={clsx('relative', broken && 'border-l-2 pl-2.5')}
           style={broken ? { borderColor: 'var(--st-failed)' } : undefined}>
        {entries.length > 0 && (
          <AnswerActions entries={entries} question={question} issuance={issuance} />
        )}
        <div className="space-y-2">
          {entries.map(([key, value]) => (
            <div key={key}>
              {entries.length > 1 && (
                <div className="mb-0.5 text-2xs font-semibold text-dim">{key}</div>
              )}
              <OutputValue value={value} marks={marks} label={key} />
            </div>
          ))}
        </div>
      </div>
      {follow.length > 0 && onFollowUp && (
        <div className="flex flex-wrap items-center gap-1.5 pt-0.5">
          <span className="text-2xs text-dim">接着问</span>
          {follow.map((f) => (
            <button key={f} type="button" className="chip transition-colors hover:bg-hover hover:text-fg"
                    onClick={() => onFollowUp(f)}>
              {f}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

/** 成果转成 Markdown：复制、导出都用它。表格按 Markdown 表格写，贴进周报就是表 */
function answerMarkdown(entries: [string, any][], question?: string, issuance?: any): string {
  const parts: string[] = []
  if (question) parts.push(`> ${question.replace(/\n/g, '\n> ')}`)
  for (const [key, value] of entries) {
    if (entries.length > 1) parts.push(`### ${key}`)
    const table = typeof value === 'string' ? parseQueryResult(value)
      : value && typeof value === 'object' && Array.isArray(value.columns) && Array.isArray(value.rows)
        ? { columns: value.columns, rows: value.rows, truncated: false } : null
    if (table) {
      const esc = (v: unknown) => (v == null ? '' : String(v)).replace(/\|/g, '\\|').replace(/\n/g, ' ')
      parts.push([
        `| ${table.columns.map(esc).join(' | ')} |`,
        `| ${table.columns.map(() => '---').join(' | ')} |`,
        ...table.rows.map((r: unknown[]) => `| ${r.map(esc).join(' | ')} |`),
      ].join('\n'))
    } else {
      parts.push(typeof value === 'string' ? value : '```json\n' + JSON.stringify(value, null, 2) + '\n```')
    }
  }
  if (issuance?.tier) {
    const cal = (issuance.calibers ?? []).map((c: any) => `${c.caliber} ${c.version}`).join('、')
    parts.push(`---\n出具档位：${issuanceLabel(issuance.tier)}${cal ? `（口径：${cal}）` : ''}`)
  }
  return parts.join('\n\n')
}

/** 答案上的操作：复制、导出 Markdown。悬停时浮出，键盘聚焦时也出来 */
function AnswerActions({ entries, question, issuance }: {
  entries: [string, any][]; question?: string; issuance?: any
}) {
  const md = () => answerMarkdown(entries, question, issuance)
  return (
    <div className="absolute right-0 top-0 z-[1] flex items-center gap-0.5 rounded opacity-0 shadow-elev-1 transition-opacity focus-within:opacity-100 group-hover/answer:opacity-100"
         style={{ background: 'var(--bg-panel)' }}>
      <CopyChip label="复制" text={md} />
      <button type="button"
              className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-2xs text-dim transition-colors hover:bg-hover hover:text-fg"
              title="导出为 Markdown 文件"
              onClick={() => download(`${fileSafe(question ?? '答案')}.md`, md(), 'text/markdown;charset=utf-8')}>
        <FileDown size={11} aria-hidden /> 导出
      </button>
    </div>
  )
}

/**
 * 追问：从答案里的表格派生，不调模型。
 *
 * 问数据的下一步往往就是「只看某一个」「换个维度比」，用户以前得自己想好措辞
 * 再打一遍。拿不到表格就不给——凭空编的追问比没有更糟。
 */
function followUpsOf(values: unknown[]): string[] {
  for (const v of values) {
    const table = typeof v === 'string' ? parseQueryResult(v)
      : v && typeof v === 'object' && Array.isArray((v as any).columns) && Array.isArray((v as any).rows)
        ? { columns: (v as any).columns as string[], rows: (v as any).rows as unknown[][], truncated: false }
        : null
    if (!table || !table.rows.length) continue
    const cat = table.columns.findIndex((_, j) => table.rows.every((r) => r[j] == null || !isNumeric(r[j])))
    // 数值列里认得出是"量"的才拿来排序：编号、代码、分组也是数字，按它排没有意义
    const metric = table.columns.findIndex((c, j) => isMetricColumn(c, table.rows, j))
    const time = table.columns.findIndex((c) => /date|day|time|month|日期|时间|月/i.test(c))
    // 只有一行时「只看某一个」「从高到低排」都是原样再问一遍
    const several = table.rows.length >= 2
    const out: string[] = []
    if (several && cat >= 0 && table.rows[0][cat] != null) out.push(`只看「${String(table.rows[0][cat]).slice(0, 16)}」的明细`)
    if (time >= 0) out.push('按月拆分看趋势')
    if (several && metric >= 0 && cat >= 0) out.push(`按 ${table.columns[metric]} 从高到低排`)
    return out.slice(0, 3)
  }
  return []
}

/** 出具里无法回指的数字，在正文原句上画出来 */
function marksOf(issuance: any): MarkSpec[] | undefined {
  const unmatched: any[] = issuance?.unmatched_numbers ?? []
  const matched: any[] = Array.isArray(issuance?.matched) ? issuance.matched : []
  const marks: MarkSpec[] = [
    ...unmatched.map((u) => ({
      token: String(u?.token ?? u),
      tone: 'warn' as const,
      title: `这个数字在口径卡里找不到来源${u?.context ? `：「${u.context}」` : ''}`,
    })),
    // 后端给了逐个数字的回指（matched: [{token, metric}]）就标出来源；目前只给计数
    ...matched.map((m) => ({
      token: String(m?.token ?? ''),
      tone: 'ok' as const,
      title: `来自口径卡指标「${m?.metric ?? ''}」${m?.caliber ? ` · ${m.caliber}` : ''}`,
    })),
  ].filter((m) => m.token)
  return marks.length ? marks : undefined
}

const TEXT_CAP = 3000

/**
 * 折叠长文本的切点。
 *
 * 不能按字符硬切：模型的报告几乎总是 Markdown，切点一旦落在表格中间，
 * 前面的行渲染成表格、最后半行留成原始的 `| 1 | ThearchyHelios | …`，
 * 看上去就是一份被咬掉一口的报告——而它其实是完整的，只是被折叠了。
 *
 * 所以只在行边界切；而且切点落进表格里时整块表格都不要，宁可少显示一段，
 * 也不要显示一张半截的表。
 */
function cutForPreview(text: string, cap: number): string {
  const head = text.slice(0, cap)
  const atLine = head.lastIndexOf('\n')
  let cut = atLine > cap * 0.5 ? head.slice(0, atLine) : head

  // 收尾的连续表格行（含分隔行）一起去掉——只剩表头的表格比没有更难读
  const lines = cut.split('\n')
  while (lines.length && /^\s*\|/.test(lines[lines.length - 1])) lines.pop()
  const withoutTable = lines.join('\n')
  // 整段都是表格时别把内容清空，那还不如原样折叠
  if (withoutTable.trim()) cut = withoutTable

  return cut.trimEnd()
}

function isBlank(v: unknown): boolean {
  if (v == null) return true
  if (typeof v === 'string') return !v.trim()
  if (Array.isArray(v)) return !v.length
  if (typeof v === 'object') return !Object.keys(v as object).length
  return false
}

/**
 * 一个成果值该怎么显示。
 *
 * 取数流程的最终产物十有八九就是结果集本身，而它在 output 里是一个
 * JSON 字符串（还常常是被再编码一层的）。直接当文本贴出来就是满屏
 * `\"attribute01\", \"attribute02\"`——技术上没错，但没人能从里面看出
 * "查到了什么"。能认出结果集就画成表格，认不出才退回文本。
 */
function OutputValue({ value, marks, label }: { value: unknown; marks?: MarkSpec[]; label?: string }) {
  const { dense } = useContext(Ctx)
  const [full, setFull] = useState(false)

  const table = typeof value === 'string'
    ? parseQueryResult(value)
    : (value && typeof value === 'object'
        && Array.isArray((value as any).columns) && Array.isArray((value as any).rows)
        ? { columns: (value as any).columns, rows: (value as any).rows,
            truncated: !!(value as any).truncated }
        : null)
  if (table) return <ResultTable table={table} title={label} />

  const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2)
  // 不设上限的话，一次没走查询工具、直接把几千行塞进 output 的运行会
  // 把整条流卡住——列表本来就没做虚拟化
  const long = text.length > TEXT_CAP
  const shown = long && !full ? cutForPreview(text, TEXT_CAP) : text

  return (
    <div>
      {/* 模型几乎总是用 Markdown 组织回答。当纯文本显示的话，「1+1 等于 **2**」
          就原样带着星号——它的结构全丢了。只有字符串才渲染：JSON.stringify
          出来的东西按 Markdown 解会被 * 和 _ 搅乱 */}
      {typeof value === 'string'
        ? <Markdown text={shown} dense={dense} marks={marks} />
        : (
          <pre className={clsx('mono overflow-x-auto whitespace-pre-wrap leading-relaxed',
            dense ? 'text-[10.5px]' : 'text-xs')}>{shown}</pre>
        )}
      {long && (
        <div className="mt-1 flex items-center gap-2 text-2xs">
          {!full && (
            // 光有一个"展开"按钮不够：用户看到的是一份读起来完整的报告，
            // 不会想到它下面还有。得先说"这里断了"
            <span className="text-dim">…后面还有，这里先折叠了</span>
          )}
          <button type="button" className="text-[var(--accent)] hover:underline"
                  onClick={() => setFull((v) => !v)}>
            {full ? '收起' : `展开全部（${formatNumber(text.length)} 字）`}
          </button>
        </div>
      )}
    </div>
  )
}

const TIER_META: Record<string, { color: string; soft: string; hint: string }> = {
  formal: { color: 'var(--st-done)', soft: 'var(--st-done-soft)', hint: '指标齐全，叙述中所有数字均可回指口径卡' },
  degraded: { color: 'var(--st-waiting)', soft: 'var(--st-waiting-soft)', hint: '存在缺口，结论请对照下方声明使用' },
  withheld: { color: 'var(--st-failed)', soft: 'var(--st-failed-soft)', hint: '必需指标缺失或数字无法溯源，本期结论不作数' },
}

/**
 * 出具档位。全站唯一一份——画布助手栏、问数据页、运行详情都用它。
 *
 * 数据取自 output._issuance 而不是 issuance 事件：事件里只有 tier /
 * missing_* / unmatched(计数)，而 output 里那份还带着 matched_numbers、
 * metrics_checked、calibers、unmatched_numbers[].token。拿事件当数据源，
 * 横幅会静默退化成"核对 0 个指标"，比不显示更糟。
 *
 * 降档的原因必须写在这里：以前只列缺指标和回指不上的数字，而「叙述模板渲染
 * 为空」「指标集为空」这类校验根本没跑起来的缺口（gaps）一条都不显示——横幅
 * 说「请对照下方声明」，下方什么都没有。
 */
export function IssuanceBanner({ issuance, runClass }: {
  issuance: any; runClass?: string
}) {
  const tier = String(issuance?.tier ?? '')
  if (!tier) return null
  const meta = TIER_META[tier] ?? { color: 'var(--st-waiting)', soft: 'var(--st-waiting-soft)', hint: '' }
  const unmatched: any[] = issuance?.unmatched_numbers ?? []
  const gaps: string[] = Array.isArray(issuance?.gaps) ? issuance.gaps.map(String) : []
  const calibers: any[] = issuance?.calibers ?? []
  const matched = Number(issuance?.matched_numbers ?? 0)
  // 逐个数字的出处（matched[].caliber 形如「口径名 @ v2」）：有它就能按卡数清楚，
  // 不必只在「只有一张卡」时才敢说数字来自哪
  const perCaliber = new Map<string, number>()
  for (const m of Array.isArray(issuance?.matched) ? issuance.matched : []) {
    if (m?.caliber) perCaliber.set(String(m.caliber), (perCaliber.get(String(m.caliber)) ?? 0) + 1)
  }
  const hitsOf = (c: any): number | undefined =>
    perCaliber.get([c?.caliber, c?.version].filter(Boolean).join(' @ '))

  return (
    // data-issuance-banner：画布上成果节点的出具印章据此滚过来；tabIndex 让「去看出具」能把焦点带到这里
    // outline-none 把 --tw-outline-style 设成了 none，data-[flash]:outline 读的就是它：得显式
    // 写回 solid，带到这里的那一下描边才画得出来（以前焦点落上了、描边却是 none）
    <div className="scroll-mt-14 rounded-lg border p-2 outline-none data-[flash]:outline data-[flash]:outline-solid data-[flash]:outline-2 data-[flash]:outline-offset-1 data-[flash=focus]:outline-[color:var(--accent)]"
         style={{ borderColor: meta.color }} data-issuance-banner={tier} tabIndex={-1}>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="rounded px-1.5 py-px text-xs font-semibold" style={{ color: meta.color, background: meta.soft }}>
          ⚖ {issuanceLabel(tier)}
        </span>
        {runClass === 'exploratory' && (
          <span className="chip" style={{ color: 'var(--st-waiting)' }}>探索运行 · 不进正式归档</span>
        )}
        <span className="tnum ml-auto text-2xs text-dim">
          回指 {formatNumber(matched)} 个数字 · 核对 {formatNumber(issuance.metrics_checked ?? 0)} 个指标
        </span>
      </div>
      {meta.hint && <div className="mt-1 text-2xs leading-relaxed text-dim">{meta.hint}</div>}

      {!!gaps.length && (
        <div className="mt-1.5 text-2xs leading-relaxed" style={{ color: 'var(--st-waiting)' }}>
          <div className="font-medium">校验没跑全：</div>
          <ul className="mt-0.5 space-y-0.5 pl-2">
            {gaps.map((g) => <li key={g}>· {g}</li>)}
          </ul>
        </div>
      )}
      {!!issuance.missing_required?.length && (
        <div className="mt-1 text-2xs" style={{ color: 'var(--st-failed)' }}>
          缺必需指标：{issuance.missing_required.join('、')}
        </div>
      )}
      {!!issuance.missing_expected?.length && (
        <div className="mt-1 text-2xs" style={{ color: 'var(--st-waiting)' }}>
          缺数据声明：{issuance.missing_expected.join('、')} 本期缺失
        </div>
      )}
      {!!unmatched.length && (
        <div className="mt-1 text-2xs leading-relaxed" style={{ color: 'var(--st-waiting)' }}>
          <span>无法回指的数字（正文里已用虚线标出）：</span>
          {unmatched.map((u: any, i: number) => (
            <span key={i} className="mr-1.5 inline-block" title={u?.context ? `「${u.context}」` : undefined}>
              <span className="mono tnum">{u?.token ?? String(u)}</span>
              {u?.context && <span className="text-dim">（{String(u.context).slice(0, 24)}）</span>}
            </span>
          ))}
        </div>
      )}
      {!!calibers.length && (
        // 数字回指到哪张口径卡：按 matched[].caliber 逐张数（hitsOf 的 key 和后端 io.py 的
        // 「口径名 @ 版本」同一种拼法，check-stream 盯着）。老后端没有逐个出处，只有一张卡时
        // 回指上的数字必然来自它，可以直说
        <div className="mt-1.5 space-y-0.5 border-t pt-1.5 text-2xs text-dim">
          {calibers.map((c: any) => {
            const hits = hitsOf(c)
            return (
              <div key={c.node ?? c.caliber} className="flex items-center gap-1.5" data-caliber={c.caliber}>
                <FileCode size={10} aria-hidden className="shrink-0" />
                <span>口径卡「{c.caliber}」<span className="mono">{c.version}</span></span>
                {calibers.length === 1 && matched > 0 && (hits == null || hits === matched)
                  ? <span data-caliber-hits={matched} style={{ color: 'var(--st-done)' }}>· 回指上的 {matched} 个数字都来自这张卡</span>
                  : hits != null && <span data-caliber-hits={hits} className="tnum" style={{ color: 'var(--st-done)' }}>· 回指 {hits} 个数字</span>}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

/** 这一轮建出来的图：几步、每步是什么，可以放到画布上接着改 */
function GraphPeek({ graph, note, onOpen }: {
  graph: any; note?: string; onOpen?: (g: any) => void
}) {
  const { dense } = useContext(Ctx)
  const [open, setOpen] = useState(false)
  const nodes = graph?.nodes ?? []

  return (
    <div className="mt-2 border-t pt-2">
      <div className="flex items-center gap-2">
        <button type="button" className="flex items-center gap-1 text-2xs text-dim hover:text-fg"
                aria-expanded={open} onClick={() => setOpen((v) => !v)}>
          <ChevronRight size={10} aria-hidden
            style={{ transform: open ? 'rotate(90deg)' : 'none', transition: 'transform .15s' }} />
          它是怎么做的（{nodes.length} 步）
        </button>
        <span className="flex-1" />
        {onOpen && (
          <button type="button" className="btn btn-sm btn-ghost text-2xs"
                  title="把这个工作流放到画布上继续改" onClick={() => onOpen(graph)}>
            在画布里打开
          </button>
        )}
      </div>
      {open && (
        <div className="mt-1.5 space-y-1">
          {note && (
            <div className={clsx('rounded bg-bg px-2 py-1.5 leading-relaxed text-dim',
              dense ? 'text-[10.5px]' : 'text-xs')}>
              {note}
            </div>
          )}
          {nodes.map((n: any, i: number) => (
            <div key={n.id ?? i} className="flex items-center gap-2 text-2xs text-dim">
              <span className="mono tnum w-4 shrink-0 text-right">{i + 1}</span>
              <span className="chip shrink-0">{nodeTypeLabel(n.type)}</span>
              <span className="truncate">{n.data?.label || n.id}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

/** 供各页面复用的空态骨架。 */
export function StreamEmpty({ icon, title, hint, children }: {
  icon: ReactNode; title: string; hint?: string; children?: ReactNode
}) {
  return (
    <div className="flex min-h-0 flex-1 flex-col items-center justify-center px-6 text-center">
      <div className="mb-2 text-faint">{icon}</div>
      <div className="text-sm font-medium">{title}</div>
      {hint && <div className="mt-1 max-w-sm text-xs leading-relaxed text-dim">{hint}</div>}
      {children && <div className="mt-3 w-full max-w-md">{children}</div>}
    </div>
  )
}
