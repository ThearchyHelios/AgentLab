import { useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode, type RefObject } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  AlertCircle, ArrowRight, BookOpen, ChevronRight, CloudOff, Database, History, ListTree, MessageSquare,
  PenLine, Play, RotateCw, StepForward, Wrench,
} from 'lucide-react'
import clsx from 'clsx'
import { ApiError, api } from '../api/client'
import { ApprovalCard } from '../run/RunPanel'
import { AssistantStream, type StreamTurn } from '../run/AssistantStream'
import { PromptBox } from '../run/Composer'
import { decodeCopilot, decodeRun } from '../run/decode'
import { CopyButton, Skeleton, Spinner, StatusBadge, toast } from '../components/ui'
import { humanizeError } from '../lib/errors'
import { formatNumber, formatTime } from '../lib/format'
import { useCatalog, useOnReconnect } from '../store/catalog'
import {
  agentSteps, busyTurn, isBusy, stepsVisible, useChat, type Attempt, type ChatTurn,
} from '../store/chat'
import { lastVisited, useConversations } from '../store/conversations'
import { ConversationList } from './ConversationList'

/**
 * 对话式入口。
 *
 * 画布是给编排的人用的：15 种节点、每种十几个配置项，那是把控制权交到手里的
 * 代价。但大多数时候人要的只是一个答案——"上季度华东销量怎么样"，不关心它
 * 是 input→agent→output 还是别的什么。
 *
 * 所以这一页把图藏起来：你问，它自己接数据源、建流程、跑完、给结论。图仍然
 * 真实存在（可以展开看，也能拿到画布里继续改），只是不再是必经之路。
 *
 * 这里和画布右栏用的是同一个 AssistantStream、同一个 decode.ts——差别只有
 * dense 一个开关。两边各写一套的话，同一次运行在两个页面会讲出不同的故事。
 */
export function ChatPage() {
  const { ask, stop, load, hydrate } = useChat()
  const { conversationId } = useParams()
  const navigate = useNavigate()
  const currentId = useConversations((s) => s.currentId)
  const create = useConversations((s) => s.create)
  const select = useConversations((s) => s.select)
  const list = useConversations((s) => s.list)
  const listLoading = useConversations((s) => s.loading)
  const turns = useChat((s) => (currentId ? s.byConversation[currentId] : undefined))
  const loadState = useChat((s) => (currentId ? s.loadState[currentId] : undefined))
  const loadError = useChat((s) => (currentId ? s.loadError[currentId] : undefined))
  const byConversation = useChat((s) => s.byConversation)
  const backend = useCatalog((s) => s.backend)
  const [draft, setDraft] = useState('')
  const [tip, setTip] = useState<string | null>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const sources = useDataSources()
  const refreshCatalog = useCatalog((s) => s.refresh)

  // URL → store。这是 currentId 唯一的写入口，别处一律靠导航
  useEffect(() => { select(conversationId ?? null) }, [conversationId, select])

  // /chat 不带 id：落到上次那个，没有就列表第一条。用 replace 而不是 push，
  // 否则按后退会回到这个空壳地址、又被弹回来，人就卡在这儿出不去了
  useEffect(() => {
    if (conversationId || listLoading || !list.length) return
    const last = lastVisited()
    const target = list.find((c) => c.id === last)?.id ?? list[0].id
    navigate(`/chat/${target}`, { replace: true })
  }, [conversationId, listLoading, list, navigate])

  // 地址指着一个不存在的对话（被删了、链接过期了）。不能白屏，也不能一声不响
  // 地跳走——点开一个链接却到了别的地方，得让人知道为什么。
  //
  // 必须等列表加载完再判：列表是异步取的，在那之前"找不到"是假的，
  // 照着它跳会让每次深链接进入都先闪一下错误提示
  //
  // 「不存在」以取详情的 404 为准，而不是「列表里没有」：刚建出来的会话可能被
  // 一次更早发出、更晚回来的列表请求盖掉；断网时列表和详情都取不到，那是「没取
  // 回来」，得留在原地给重试，不能当成坏地址把人带走
  const reported = useRef<string | null>(null)
  const missing = loadState === 'error' && loadError instanceof ApiError && loadError.status === 404
  useEffect(() => {
    if (reported.current !== conversationId) reported.current = null
    if (!conversationId || listLoading || !missing) return
    // 同一个坏地址只说一次。StrictMode 会把 effect 跑两遍，依赖里的 list
    // 变一次又会再跑一遍——不记着的话同一句提示会连弹好几条
    if (reported.current === conversationId) return
    reported.current = conversationId
    useChat.getState().forget(conversationId)
    // 列表里还挂着它，说明列表是过时的（别的标签页删掉了）：顺手刷新
    if (list.some((c) => c.id === conversationId)) void useConversations.getState().load()
    const next = list.find((c) => c.id !== conversationId)
    toast.info(next ? '那个对话不在了，已经带你回到最近一个' : '那个对话不在了')
    navigate(next ? `/chat/${next.id}` : '/chat', { replace: true })
  }, [conversationId, listLoading, list, navigate, missing])

  // 切到哪个会话就把哪个会话的内容取回来。内存里已经有的不会回源——
  // 那一份带着这次跑出来的完整步骤，回源拿到的只有问题和答案
  useEffect(() => { if (currentId) void load(currentId) }, [currentId, load])

  // 后端断开又连上：只补取加载失败的那个，正在进行的轮次一个都不碰；
  // 停在「正在核对」的轮次（核对请求撞上了断网）再核对一次
  useOnReconnect(() => {
    if (!currentId) return
    const st = useChat.getState()
    if (st.loadState[currentId] === 'error') void load(currentId, { force: true })
    for (const t of st.byConversation[currentId] ?? []) {
      if (t.phase === 'checking' && !t.hydrated) void hydrate(currentId, t.id)
    }
  })

  const busy = busyTurn(turns)
  // 另一个会话在跑：这里照样能问（各跑各的），但得让人知道那边没停
  const elsewhere = useMemo(() => {
    for (const [id, ts] of Object.entries(byConversation)) {
      if (id !== currentId && isBusy(ts)) {
        return { id, title: list.find((c) => c.id === id)?.title || ts[ts.length - 1].question }
      }
    }
    return null
  }, [byConversation, currentId, list])

  const blocked = loadState === 'loading' ? '正在取回这个对话的历史…'
    : loadState === 'error' ? '历史没取回来，先点上面的「重试」'
    : backend === 'down' ? '后端没连上，这会儿发不出去'
    : null

  const send = async (text?: string) => {
    const q = (text ?? draft).trim()
    if (!q || busy || blocked) return
    // 没有会话就先开一个。第一次进来的人不该先被要求"新建对话"
    let id = currentId
    if (!id) {
      try {
        id = await create()
      } catch (e) {
        toast.error(e)
        return
      }
    }
    // 地址要跟上，否则内容已经在新对话里、地址栏还停在 /chat
    if (id !== currentId) navigate(`/chat/${id}`, { replace: true })
    if (!ask(id, q)) return
    setDraft('')
    setTip(null)
  }

  /** 「换个说法」：原问题填回来，光标放到末尾，让人改而不是重打 */
  const rephrase = (question: string) => {
    setDraft(question)
    setTip('换个说法：写明查哪个库、什么时间范围、要什么指标，更容易一次查对')
    requestAnimationFrame(() => {
      const el = inputRef.current
      if (!el) return
      el.focus()
      el.setSelectionRange(el.value.length, el.value.length)
    })
  }

  const pick = (q: string) => {
    setDraft(q)
    requestAnimationFrame(() => inputRef.current?.focus())
  }

  const streamTurns = useMemo(() => (turns ?? []).map(toStreamTurn), [turns])
  const lastId = turns?.[turns.length - 1]?.id
  useLazyHydrate(currentId, turns)

  // 三种「没有轮次」要分开画：还在取、取失败、真的是空的。以前三者都是「问点什么」，
  // 加载失败时用户一提问，历史在这次页面里就再也回不来了
  const view: 'pending' | 'loading' | 'error' | 'hero' | 'stream' =
    !currentId ? (listLoading || list.length ? 'pending' : 'hero')
      : loadState === 'error' && !turns?.length ? 'error'
      : (loadState === 'loading' || !loadState) && !turns?.length ? 'loading'
      : !turns?.length ? 'hero'
      : 'stream'

  const dock = (
    <div className="shrink-0 border-t px-4 pb-3 pt-2">
      <div className="mx-auto max-w-3xl">
        {elsewhere && !busy && (
          <div className="mb-1.5 flex items-center gap-1.5 text-2xs text-faint" role="status">
            <StatusBadge status="running" size={11} decorative />
            <span className="min-w-0 truncate">
              对话「{elsewhere.title}」正在运行，这里可以照常提问
            </span>
            <Link to={`/chat/${elsewhere.id}`} className="shrink-0 text-[var(--accent)] hover:underline">
              去看看
            </Link>
          </div>
        )}
        {tip && !busy && <div className="mb-1.5 text-2xs text-faint">{tip}</div>}
        <PromptBox
          inputRef={inputRef}
          size="dock"
          value={draft}
          onChange={setDraft}
          onSubmit={() => void send()}
          busy={!!busy}
          onStop={() => currentId && stop(currentId)}
          stopLabel="停止这一轮"
          blocked={blocked}
          label="向数据提问"
          placeholder="问点什么…"
        />
      </div>
    </div>
  )

  let body: ReactNode
  if (view === 'hero') {
    body = (
      <ChatHero
        sources={sources}
        inputRef={inputRef}
        draft={draft}
        setDraft={setDraft}
        onSend={() => void send()}
        onPick={pick}
        blocked={blocked}
      />
    )
  } else if (view === 'pending' || view === 'loading') {
    body = (
      <>
        <TurnSkeleton />
        {dock}
      </>
    )
  } else if (view === 'error') {
    body = (
      <>
        <LoadFailed error={loadError} onRetry={() => currentId && void load(currentId, { force: true })} />
        {dock}
      </>
    )
  } else {
    body = (
      <AssistantStream
        turns={streamTurns}
        approvalsFor={(t) => <TurnExtras turnId={t.id} />}
        renderTurnActions={(t) => {
          const turn = turns?.find((x) => x.id === t.id)
          return currentId && turn && hasRemedies(turn)
            ? <Remedies conversationId={currentId} turnId={t.id} last={t.id === lastId} onRephrase={rephrase} />
            : null
        }}
        onFollowUp={(text) => {
          // 追问是从表格里派生的一句话，点了就问；这会儿发不出去就先放进输入框
          if (busy || blocked) pick(text)
          else void send(text)
        }}
        onOpenGraph={async (graph, question) => {
          // 建成一张新工作流，走它自己的地址。以前是 setGraph(graph)：只换掉了画布
          // 上的节点，store 里的 workflow 还是上一次打开的那张图——于是 ⌘S 把那张图
          // 整张覆盖了（旧版本还在版本表里，界面上却没有入口找回）；没打开过图时
          // ⌘S 则什么都不做，也不说一声
          try {
            const name = `问数据：${(question ?? '').trim().slice(0, 24) || '未命名'}`
            const created = await api.workflows.create({ name, graph })
            void refreshCatalog()
            navigate(`/studio/${created.id}`)
            toast.ok('已放到画布：新建了一个工作流，原来的图不受影响')
          } catch (e) {
            toast.error(e)
          }
        }}
        footer={dock}
      />
    )
  }

  return (
    <div className="flex h-full">
      <ConversationList />
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex shrink-0 items-center gap-2 border-b px-4 py-2">
          <MessageSquare size={14} style={{ color: 'var(--accent)' }} aria-hidden />
          <h1 className="text-sm font-semibold">问数据</h1>
          <span className="truncate text-2xs text-faint">
            说需求，它自己接数据源、建流程、跑完给结论
          </span>
        </header>
        {/* min-h-0 是关键：AssistantStream 的根是 h-full，内容一长，flex 子项的
            min-height:auto 就不让它收缩，再加上面 37px 的页头，输入框和发送键
            被推出视口，整页还能多滚一截 */}
        <div className="flex min-h-0 flex-1 flex-col">{body}</div>
      </div>
    </div>
  )
}

/**
 * 一轮对话 → 一条助手流。
 *
 * 建图和跑图是两套完全独立的协议（SSE 操作流 vs 事件 WebSocket，不共享
 * seq 和 node_id），但对用户是同一件事的两半：先想清楚怎么做，再去做。
 * 拼成一条步骤序列，看到的才是一个连续的过程。
 *
 * 按轮次对象缓存：store 只替换变了的那一轮，其余轮次引用不变，于是一条事件
 * 只重解码它自己那一轮，而不是整个会话的每一轮。
 */
const streamCache = new WeakMap<ChatTurn, StreamTurn>()

function toStreamTurn(turn: ChatTurn): StreamTurn {
  const hit = streamCache.get(turn)
  if (hit) return hit
  const live = turn.phase === 'planning' || turn.phase === 'building' || turn.phase === 'running'
  const f = turn.failure
  const out: StreamTurn = {
    id: turn.id,
    question: turn.question,
    // 取消、中断在流里按「结束了」画：它们不是故障，不该出红框；头部的徽标和
    // 状态词由 statusCode 说清，要做什么由下面的补救动作说清
    phase: turn.phase === 'waiting' ? 'waiting'
      : turn.phase === 'error' ? 'error'
      : live || turn.phase === 'checking' ? 'running' : 'done',
    statusCode: turn.phase === 'cancelled' ? 'cancelled'
      : turn.phase === 'suspended' ? 'suspended'
      : turn.phase === 'ready' ? 'idle'
      : undefined,
    tone: turn.phase === 'suspended' ? 'warn' : undefined,
    // 进行中的写此刻在做什么；结束了的用全站统一的状态词
    status: live ? liveLine(turn)
      : turn.phase === 'checking' || turn.phase === 'suspended' || turn.phase === 'ready' ? turn.status
      : undefined,
    // 从库里恢复、还没点开「执行过程」的轮次只有问题和答案；点开后又收起的，
    // 步骤留在内存里但不画
    steps: stepsVisible(turn) ? [...decodeCopilot(turn.ops), ...decodeRun(turn.events, turn.final)] : [],
    thinking: turn.thinking,
    output: turn.output,
    // 结构化的交给流里的报错块：原因、怎么办分开写，原文收进技术细节
    error: turn.phase === 'error' && f
      ? { error: f.source ?? (f.reason ? `${f.title}：${f.reason}` : f.title), hint: f.hint, detail: f.detail }
      : turn.error || undefined,
    runId: turn.run?.id,
    runClass: turn.run?.run_class,
    graph: turn.graph,
    graphNote: turn.explanation,
    noQuery: turn.noQuery,
    review: turn.review,
    rawOutput: turn.rawOutput,
    // 正在核对的历史轮次不给起点：它的 startedAt 是提问那天，计时器会从几天前跑起
    startedAt: live ? turn.startedAt : undefined,
    elapsedMs: !live && turn.endedAt ? turn.endedAt - turn.startedAt : turn.meta?.ms ?? undefined,
  }
  streamCache.set(turn, out)
  return out
}

/**
 * 进行中的那一句。模型出字时说写了多少，只在想时说想到哪了——一次模型调用
 * 常常要几十秒，以前这几十秒里只有一行不动的「正在分析…」，分不清是在写还是卡住。
 * 答案原文不在这里露：它要等复核完才摆出来
 */
function liveLine(turn: ChatTurn): string {
  const w = turn.phase === 'running' ? turn.writing : null
  if (w?.chars) return `正在撰写 · 已生成 ${formatNumber(w.chars)} 字`
  const thought = w?.thought ? lastSentence(w.thought) : ''
  return thought ? `正在思考：${thought}` : turn.status
}

/**
 * 思考流里最后一句完整的话。还在写的半句一般不用：字一个个往外蹦，读不成句；
 * 只有一句都还没写完、而这半句已经够长时才先拿它顶上
 */
function lastSentence(text: string): string {
  const parts = text.split(/(?<=[。！？!?；;\n]|\.\s)/)
  const closed = /[。！？!?；;\n]$|\.\s$/.test(text) ? parts : parts.slice(0, -1)
  const pick = [...closed].reverse().find((p) => p.trim())
    ?? (parts.length === 1 && text.trim().length >= 12 ? text : '')
  return pick.trim().replace(/[。！？!?；;.]$/, '').slice(0, 120)
}

/**
 * 挂在一轮里面的东西：待办的审批、「跑一下」、历史轮次的「执行过程」开关。
 *
 * 从库里恢复的轮次没有事件流——一次会话几十轮，把每轮的事件都预加载回来
 * 是几十个请求换一堆没人看的步骤。所以默认只显示问题和答案，想看过程再取。
 */
function TurnExtras({ turnId }: { turnId: string }) {
  const conversationId = useConversations((s) => s.currentId)
  const turn = useChat((s) => (conversationId ? s.byConversation[conversationId]?.find((t) => t.id === turnId) : undefined))
  const approvals = useCatalog((s) => s.approvals)
  const { reattach, runNow, toggleSteps } = useChat.getState()
  const busy = useChat((s) => (conversationId ? isBusy(s.byConversation[conversationId]) : false))
  if (!turn || !conversationId) return null
  const runId = turn.run?.id
  const pending = runId ? approvals.filter((a) => a.run_id === runId && a.status === 'pending') : []
  const canSteps = !!turn.restored && !!runId && !BUSY.has(turn.phase)

  if (!pending.length && !turn.pendingRun && !canSteps) return null
  return (
    <>
      {(turn.pendingRun || canSteps) && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          {turn.pendingRun && (
            <button className="btn btn-xs btn-primary" disabled={busy}
                    title={busy ? '这个对话还有一轮在跑' : '按搭好的流程跑一次'}
                    onClick={() => runNow(conversationId, turn.id)}>
              <Play size={11} /> 跑一下
            </button>
          )}
          {canSteps && (
            <button className="btn btn-xs btn-ghost" aria-expanded={stepsVisible(turn)}
                    disabled={turn.steps === 'loading'}
                    onClick={() => void toggleSteps(conversationId, turn.id)}>
              {turn.steps === 'loading' ? <Spinner size={11} /> : <ListTree size={11} />}
              {turn.steps === 'loading' ? '正在取执行过程…' : stepsVisible(turn) ? '收起执行过程' : '看执行过程'}
            </button>
          )}
        </div>
      )}
      {!!pending.length && (
        <div className="mt-2 overflow-hidden rounded border" style={{ borderColor: 'var(--st-waiting)' }}>
          {pending.map((a) => (
            <ApprovalCard key={a.id} approval={a}
                          onResolved={() => void reattach(conversationId, turn.id)} />
          ))}
        </div>
      )}
    </>
  )
}

const BUSY = new Set(['planning', 'building', 'running', 'checking'])

/**
 * 补救动作，排在这一轮的卡片下面。
 *
 * 失败、结论不可用、被打断的轮次，以前只剩一行红字——真库里用户手打过「重试」
 * 「重来」「再找找」「你自己调大」，每一句都另起一轮，会话被这些噪音塞满，
 * 失败原因也读不全。这里把能做的事直接摆出来：
 *
 * - 接着跑：只给失败的、和被服务重启打断的。已取消的续不上（后端回 409）。
 * - 重试这一轮：带着上一次的原因重来，上一次折叠留档，不覆盖。
 * - 重跑这一轮：取消了、中断了的，流程本身没问题，原样再跑。
 * - 放宽步数重跑：复核说步数用满了才出现，目标值写在按钮上。
 * - 换个说法：原问题填回输入框。
 *
 * 出了什么错、为什么、怎么办由流里的报错块说（卡片顶上），这里不再重复。
 */
function Remedies({ conversationId, turnId, last, onRephrase }: {
  conversationId: string
  turnId: string
  /** 只有最后一轮能原地重来；更早的轮次后面已经接着问过了，原地改答案会让后面的问答失去依据 */
  last: boolean
  onRephrase: (question: string) => void
}) {
  const turn = useChat((s) => s.byConversation[conversationId]?.find((t) => t.id === turnId))
  const busy = useChat((s) => isBusy(s.byConversation[conversationId]))
  const { retryTurn, continueTurn, ask } = useChat.getState()
  const cap = useStepCap()
  const [resuming, setResuming] = useState(false)
  if (!turn) return null

  const kind = remedyOf(turn)
  const graphful = (turn.graph?.nodes?.length ?? 0) > 0
  const stepSignal = turn.review?.signals.some((s) => s.kind === 'step_limit' || s.kind === 'step_limit_settled')
  const current = agentSteps(turn.graph)
  const target = current && cap ? Math.min(cap, Math.max(current * 2, current + 8)) : null
  const canContinue = !!turn.run?.id && (kind === 'suspended'
    || (kind === 'failed' && turn.runStatus === 'failed'))
  const busyNote = busy ? '这个对话还有一轮在跑，等它结束' : undefined
  // 异常态醒目、正常态安静：失败、中断、不可用的第一个动作是实心的
  const alarm = kind === 'failed' || kind === 'suspended' || kind === 'unusable'

  const resume = async () => {
    setResuming(true)
    try {
      await continueTurn(conversationId, turn.id)
    } catch (e) {
      // 后端的 409 已经是人话：「这次运行已取消，不能接着跑。要继续，请重新发起一次运行。」
      toast.error(e)
    } finally {
      setResuming(false)
    }
  }
  const again = (opts?: { maxSteps?: number; rerun?: boolean }) => {
    if (last) retryTurn(conversationId, turn.id, opts)
    else if (ask(conversationId, turn.question)) toast.info('已经在对话末尾用同一个问题再问了一次')
  }

  const buttons: ReactNode[] = []
  if (canContinue) {
    buttons.push(
      <button key="continue" className="btn btn-xs btn-primary" disabled={busy || resuming}
              title={busyNote ?? '从断点接着跑：前面跑过的步骤不重来。要改配置请到画布上改'}
              onClick={() => void resume()}>
        {resuming ? <Spinner size={11} /> : <StepForward size={11} />} 接着跑
      </button>,
    )
  }
  if (kind) {
    const plain = kind === 'cancelled' || kind === 'suspended'
    const label = !last ? '重新问一次' : plain && graphful ? '重跑这一轮' : '重试这一轮'
    buttons.push(
      <button key="retry" className={clsx('btn btn-xs', alarm && !canContinue && 'btn-primary')}
              disabled={busy}
              title={busyNote ?? (!last ? '在对话末尾用同一个问题再问一次'
                : plain && graphful ? '按原来的流程从头再跑一次，上一次留档可以对照'
                : '带着上一次的原因重来，它会先调整流程；上一次留档可以对照')}
              onClick={() => again(plain && graphful ? { rerun: true } : undefined)}>
        <RotateCw size={11} /> {label}
      </button>,
    )
  }
  if (stepSignal && last && graphful && current && target && target > current) {
    buttons.push(
      <button key="steps" className="btn btn-xs" disabled={busy}
              title={busyNote ?? `把 agent 的步数上限从 ${current} 调到 ${target}（全局上限 ${cap} 步，在设置里改），流程不变，重跑一次`}
              onClick={() => again({ maxSteps: target })}>
        <ArrowRight size={11} /> 放宽步数重跑（{current} → {target} 步）
      </button>,
    )
  }
  if (kind) {
    buttons.push(
      <button key="rephrase" className="btn btn-xs btn-ghost" onClick={() => onRephrase(turn.question)}
              title="把这个问题填回输入框，改一改再问">
        <PenLine size={11} /> 换个说法
      </button>,
    )
  }

  // 中断、取消在流里不出红框（它们不是故障），为什么停、停了意味着什么在这里说
  const note = kind === 'suspended' ? turn.failure
    : kind === 'cancelled' ? { title: '', reason: turn.run ? '你停下了这一轮，后端的运行也一并取消了。' : '你在搭流程时停下了这一轮。' }
    : null

  return (
    <div className="w-full" data-remedy={kind ?? undefined}>
      {turn.clipped && (
        <div className="mb-1.5 rounded border px-2 py-1.5 text-2xs leading-relaxed text-dim"
             style={{ borderColor: 'color-mix(in srgb, var(--st-waiting) 40%, var(--border))' }} role="note">
          {turn.clipped === 'lost'
            ? '这份答案在旧版本保存时被截在了 2000 字，对应的运行记录已经删掉，补不回后面的部分。'
            : '答案比较长，这里只收到了前一部分；完整版在运行记录里，刷新后会自动补全。'}
        </div>
      )}
      {note && (note.reason || note.hint) && (
        <div className="mb-1.5 text-xs leading-relaxed">
          {note.reason && <span className="text-dim">{note.reason}</span>}
          {note.hint && <span className="text-faint"> {note.hint}</span>}
        </div>
      )}
      {!!buttons.length && <div className="flex flex-wrap items-center gap-1.5">{buttons}</div>}
      {!!turn.attempts?.length && <AttemptHistory attempts={turn.attempts} />}
    </div>
  )
}

/** 这一轮下面有没有东西要摆：没有就不占位 */
function hasRemedies(t: ChatTurn): boolean {
  return !!remedyOf(t) || !!t.clipped || !!t.attempts?.length
}

/**
 * 老轮次滚进视口时才去运行记录补档位和运行类别：几十轮的会话不必一进来就发
 * 几十个请求。认的是流里每一轮根元素上的 data-turn
 */
function useLazyHydrate(conversationId: string | null, turns: ChatTurn[] | undefined) {
  useEffect(() => {
    if (!conversationId || !turns?.length || typeof IntersectionObserver === 'undefined') return
    const want = new Set(turns
      .filter((t) => t.restored && t.run?.id && !t.hydrated && t.phase === 'done' && !t.meta)
      .map((t) => t.id))
    if (!want.size) return
    const io = new IntersectionObserver((entries) => {
      for (const e of entries) {
        const id = (e.target as HTMLElement).dataset.turn
        if (!e.isIntersecting || !id || !want.has(id)) continue
        want.delete(id)
        io.unobserve(e.target)
        void useChat.getState().hydrate(conversationId, id)
      }
    }, { rootMargin: '200px' })
    document.querySelectorAll<HTMLElement>('[data-turn]').forEach((el) => {
      if (want.has(el.dataset.turn ?? '')) io.observe(el)
    })
    return () => io.disconnect()
  }, [conversationId, turns])
}

type Remedy = 'failed' | 'suspended' | 'cancelled' | 'unusable' | 'partial'

function remedyOf(t: ChatTurn): Remedy | null {
  if (t.phase === 'error') return 'failed'
  if (t.phase === 'suspended') return 'suspended'
  if (t.phase === 'cancelled') return 'cancelled'
  if (t.phase === 'done' && t.review?.severity === 'broken') return 'unusable'
  if (t.phase === 'done' && t.review?.severity === 'degraded') return 'partial'
  return null
}

/** 原文折起来，要的人展开、复制去问维护者。不再截成一行、也不再原样甩在头部 */
function TechDetails({ raw }: { raw: string }) {
  return (
    <details className="mt-1.5 text-2xs text-faint">
      <summary className="cursor-pointer select-none hover:text-dim">技术细节</summary>
      <div className="mt-1 flex items-start gap-1.5">
        <pre className="mono max-h-40 min-w-0 flex-1 overflow-auto whitespace-pre-wrap break-all rounded border bg-bg p-2 text-2xs leading-relaxed text-dim">
          {raw}
        </pre>
        <CopyButton text={raw} />
      </div>
    </details>
  )
}

const OUTCOME: Record<Attempt['outcome'], string> = {
  failed: 'failed', cancelled: 'cancelled', suspended: 'suspended',
  unusable: 'failed', partial: 'waiting', done: 'succeeded',
}

/** 之前的尝试：每一次折成一行「第 1 次尝试 · 失败原因」，点开看那次的答案和原文 */
function AttemptHistory({ attempts }: { attempts: Attempt[] }) {
  return (
    <div className="mt-2 space-y-1" aria-label="之前的尝试">
      {attempts.map((a) => (
        <details key={a.n} className="group rounded-md border bg-bg px-2 py-1 text-2xs">
          <summary className="flex cursor-pointer select-none items-center gap-1.5 text-faint hover:text-dim">
            <ChevronRight size={10} className="shrink-0 transition-transform group-open:rotate-90" aria-hidden />
            <StatusBadge status={OUTCOME[a.outcome]} size={11} animate={false} decorative />
            <span className="shrink-0 text-dim">第 {a.n} 次尝试</span>
            <span className="min-w-0 flex-1 truncate">· {a.summary}</span>
            <span className="tnum shrink-0">{formatTime(a.at)}</span>
          </summary>
          <div className="mt-1.5 space-y-1.5 pb-1 pl-4 leading-relaxed text-dim">
            <div className="whitespace-pre-wrap">{a.summary}</div>
            {a.answer && (
              <div className="max-h-48 overflow-auto whitespace-pre-wrap rounded border bg-panel p-2 text-2xs">
                {a.answer}
              </div>
            )}
            {a.detail && <TechDetails raw={a.detail} />}
            {a.runId && (
              <Link to={`/runs/${a.runId}`} className="mono inline-block text-[var(--accent)] hover:underline">
                运行 #{a.runId.slice(0, 6)}
              </Link>
            )}
          </div>
        </details>
      ))}
    </div>
  )
}

/** 设置里 agent 步数的全局上限：「放宽步数」不能许一个后端会截掉的数 */
let stepCapCache: Promise<number | null> | null = null
function useStepCap(): number | null {
  const [cap, setCap] = useState<number | null>(null)
  useEffect(() => {
    stepCapCache ??= api.settings.get()
      .then((s) => Number(s?.limits?.max_agent_steps) || null)
      .catch(() => { stepCapCache = null; return null })
    let alive = true
    void stepCapCache.then((v) => { if (alive) setCap(v) })
    return () => { alive = false }
  }, [])
  return cap
}

// -------------------------------------------------------------------------

/**
 * 历史没取回来。说清是「这个对话的历史」没回来，而不是一句笼统的错误——左栏
 * 明明写着这个对话有几轮，主区却一片空白，人会以为历史丢了
 */
function LoadFailed({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const h = humanizeError(error)
  const Icon = h.kind === 'network' ? CloudOff : AlertCircle
  return (
    <div className="flex min-h-0 flex-1 items-center justify-center overflow-y-auto px-6">
      <div role="alert" className="max-w-md text-center">
        <Icon size={22} className="mx-auto text-[var(--st-failed)]" aria-hidden />
        <div className="mt-2 text-sm font-medium">这个对话的历史没取回来</div>
        <div className="mt-1 text-xs leading-relaxed text-dim">{h.reason ? `${h.title}：${h.reason}` : h.title}</div>
        <div className="mt-1 text-xs leading-relaxed text-faint">
          取回来之前先不能在这里提问，免得新问题把历史盖掉。已经打好的字会留着。
        </div>
        <button className="btn btn-sm mt-3" onClick={onRetry}><RotateCw size={12} /> 重试</button>
        {h.raw && h.raw !== h.title && <div className="mt-2 text-left"><TechDetails raw={h.raw} /></div>}
      </div>
    </div>
  )
}

/** 取回历史时的占位：按真实轮次的样子画，数据回来时不跳 */
function TurnSkeleton() {
  return (
    <div className="min-h-0 flex-1 overflow-hidden px-4 py-4" aria-busy="true">
      <div className="mx-auto max-w-3xl space-y-5">
        {[0, 1].map((i) => (
          <div key={i} className="space-y-2">
            <div className="flex justify-end">
              <Skeleton rows={1} height={28} className="w-2/5" style={{ minWidth: 160 }} />
            </div>
            <div className="rounded-lg border bg-panel p-3">
              <Skeleton rows={i ? 3 : 5} height={11} gap={9} />
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

/**
 * 空会话的首屏：输入框是主角。
 *
 * 以前是一个小图标加一句话，输入框贴在页面底部；而画布的 Copilot 早就有居中
 * 大输入框的 hero 态，默认落地页反而更简陋。第一印象该是「它懂我的业务」：
 * 能接到哪些库、有没有知识库和工具摆在输入框下面，例句用的是这些库自己的词。
 */
function ChatHero({ sources, inputRef, draft, setDraft, onSend, onPick, blocked }: {
  sources: SourcesState
  inputRef: RefObject<HTMLTextAreaElement | null>
  draft: string
  setDraft: (v: string) => void
  onSend: () => void
  onPick: (q: string) => void
  blocked: string | null
}) {
  const collections = useCatalog((s) => s.collections)
  const tools = useCatalog((s) => s.tools)
  const byConversation = useChat((s) => s.byConversation)
  const list = sources.list
  const suggestions = useMemo(() => suggestFrom(list), [list])
  const recent = useMemo(() => recentQuestions(byConversation), [byConversation])
  const none = sources.state === 'ready' && !list.length

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
      <div className="m-auto w-full max-w-2xl px-6 py-10">
        <div className="mb-5 text-center">
          <div className="breathe mx-auto mb-3 flex h-11 w-11 items-center justify-center rounded-2xl"
               style={{ background: 'var(--accent-soft)', color: 'var(--accent)' }} aria-hidden>
            <Database size={20} />
          </div>
          <h2 className="text-xl font-semibold">问你的数据</h2>
          <p className="mt-1 text-xs text-faint">说一句话，它自己接数据源、写查询、跑完给结论——不用碰画布</p>
        </div>

        <PromptBox
          inputRef={inputRef}
          size="hero"
          rows={3}
          autoFocus
          value={draft}
          onChange={setDraft}
          onSubmit={onSend}
          blocked={blocked}
          label="向数据提问"
          placeholder="问点什么…"
        />

        <div className="mt-3 flex flex-wrap items-center justify-center gap-1.5 text-2xs">
          <Capability to="/data" icon={<Database size={11} />}
            text={sources.state === 'loading' ? '数据源 —'
              : sources.state === 'error' ? '数据源没取回来'
              : list.length ? `${list.length} 个数据源 · ${list.map((s) => s.name).slice(0, 3).join(' / ')}${list.length > 3 ? ' …' : ''}`
              : '还没有数据源'} />
          <Capability to="/knowledge" icon={<BookOpen size={11} />}
            text={`知识库 ${collections.length} 个`} />
          <Capability to="/tools" icon={<Wrench size={11} />} text={`工具 ${tools.length} 个`} />
        </div>

        {none && (
          <div className="mt-6 rounded-lg border bg-panel px-4 py-3 text-center text-xs leading-relaxed text-dim">
            还没有接入数据源。接一个数据库或者传一张表，就能直接问它问题了。
            <div className="mt-2">
              <Link to="/data" className="btn btn-sm btn-primary">
                <Database size={12} /> 接入数据源
              </Link>
            </div>
          </div>
        )}

        {!!suggestions.length && (
          <SuggestionGroup title="试试这样问" items={suggestions} onPick={onPick} offset={0} />
        )}
        {!!recent.length && (
          <SuggestionGroup title="最近问过，再问一次" icon={<History size={11} />} items={recent}
                           onPick={onPick} offset={suggestions.length} />
        )}
      </div>
    </div>
  )
}

function Capability({ to, icon, text }: { to: string; icon: ReactNode; text: string }) {
  return (
    <Link to={to} className="inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-faint transition-colors hover:border-[var(--border-strong)] hover:text-dim">
      <span aria-hidden>{icon}</span>{text}
    </Link>
  )
}

function SuggestionGroup({ title, icon, items, onPick, offset }: {
  title: string; icon?: ReactNode; items: string[]; onPick: (q: string) => void; offset: number
}) {
  return (
    <div className="mt-6">
      <div className="mb-1.5 flex items-center gap-1 text-2xs text-faint">{icon}{title}</div>
      <div className="grid gap-1.5 sm:grid-cols-2">
        {items.map((q, i) => (
          <button key={q}
            className="rise-in rounded-lg border bg-panel px-3 py-2 text-left text-xs leading-relaxed text-dim transition-colors hover:border-[var(--accent)] hover:text-fg"
            style={{ '--i': offset + i + 1 } as CSSProperties}
            title="填进输入框，改一改再发"
            onClick={() => onPick(q)}>
            {q}
          </button>
        ))}
      </div>
    </div>
  )
}

// -------------------------------------------------------------------------
// 例句
// -------------------------------------------------------------------------

/**
 * 系统表：迁移记录、测试、临时、审计、日志、备份、缓存……以前例句取的是字母序
 * 前三张表，于是第一句建议是「__drizzle_migrations 有多少条记录？」——真有人点了，
 * 而且直接失败了。
 */
function isSystemTable(full: string): boolean {
  const name = full.split('.').pop() ?? full
  if (/^_/.test(name)) return true
  if (/(Log|Logs|History|Queue|Job)$/.test(name)) return true   // 驼峰命名的日志、队列表
  const low = name.toLowerCase()
  return /migration|flyway|liquibase|alembic|schema_version|etl|audit|cache|session|cron|backup|snapshot|^sys_|^django_/.test(low)
    || /(^|_)(log|logs|history|tmp|temp|test|bak|job|jobs|queue|seq|lock)(_|\d|$)/.test(low)
}

/** 数据源描述里的业务词 → 一句像样的问题。只认得出的才造，认不出就退回表名 */
const DOMAIN_QUESTIONS: [RegExp, string][] = [
  [/生产|产线|MES|产量/i, '上周各产线的产量是多少'],
  [/采购|供应商/, '本月采购金额最高的 5 家供应商'],
  [/物料|库存|BOM/i, '库存最多的 10 种物料'],
  [/设备|IOT|停机/i, '最近 7 天停机次数最多的设备'],
  [/财务|凭证|科目/, '本月凭证金额按科目汇总'],
  [/订单|销售/, '各地区的订单金额排名'],
  [/商品|产品/, '卖得最好的 5 个商品'],
  [/用户|会员|客户/, '最近 7 天新增了多少用户'],
  [/出勤|考勤|人员/, '本周各部门的出勤率'],
]

/** 没写描述的库：挑一张像业务实体的表，而不是字母序第一张 */
const ENTITY = /^(users?|members?|customers?|orders?|products?|merchants?|shops?|stores?|accounts?|messages?|vouchers?|regions?|employees?)$/i

function suggestFrom(sources: Source[]): string[] {
  const perSource = sources.map((s) => {
    const desc = String(s.description ?? '')
    const byDomain = DOMAIN_QUESTIONS.filter(([re]) => re.test(desc)).map(([, q]) => `${s.name}：${q}`)
    if (byDomain.length) return byDomain.slice(0, 2)
    const tables = (s.tables ?? []).filter((t) => !isSystemTable(t))
    const table = tables.find((t) => ENTITY.test(t.split('.').pop() ?? t)) ?? tables[0]
    return table
      ? [`${s.name} 的 ${table.split('.').pop()} 表一共多少条？最近一条是什么时候`]
      : [`${s.name} 里都有哪些业务数据？挑最大的一张表说说`]
  })
  // 各个库轮流取，别让第一个库把四个位置占满
  const out: string[] = []
  for (let i = 0; out.length < 4 && perSource.some((q) => q[i]); i++) {
    for (const q of perSource) if (q[i] && out.length < 4) out.push(q[i])
  }
  return out
}

/** 这次打开页面以来答得好好的问题：没查库的、复核说不可用的都不算 */
function recentQuestions(byConversation: Record<string, ChatTurn[]>): string[] {
  const done = Object.values(byConversation).flat()
    .filter((t) => t.phase === 'done' && !t.noQuery && t.review?.severity !== 'broken' && !!t.run)
    .sort((a, b) => b.startedAt - a.startedAt)
  return [...new Set(done.map((t) => t.question.trim()))].filter(Boolean).slice(0, 2)
}

interface Source { id: string; name: string; description?: string; tables?: string[] }
interface SourcesState { state: 'loading' | 'ready' | 'error'; list: Source[] }

function useDataSources(): SourcesState {
  const [sources, setSources] = useState<SourcesState>({ state: 'loading', list: [] })
  const load = () => {
    void api.datasources.list()
      .then(async (rows) => {
        setSources({ state: 'ready', list: rows })
        // 表名只用来给没写描述的库造例句，取不到也无所谓
        const withTables = await Promise.all(rows.map(async (r: any) => {
          try {
            const s = await api.datasources.schema(r.id)
            return { ...r, tables: s.tables ?? [] }
          } catch { return r }
        }))
        setSources({ state: 'ready', list: withTables })
      })
      // 取失败和「没有数据源」是两回事：前者不能劝人去新建
      .catch(() => setSources((s) => ({ state: s.list.length ? 'ready' : 'error', list: s.list })))
  }
  useEffect(load, [])
  useOnReconnect(load)
  return sources
}

