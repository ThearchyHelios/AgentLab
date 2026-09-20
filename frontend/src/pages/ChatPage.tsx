import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { Database, ListTree, MessageSquare, Play, RotateCw, Send, Square } from 'lucide-react'
import { api } from '../api/client'
import { ApprovalCard } from '../run/RunPanel'
import { AssistantStream, StreamEmpty, type StreamTurn } from '../run/AssistantStream'
import { decodeCopilot, decodeRun } from '../run/decode'
import { useCatalog } from '../store/catalog'
import { useChat, type ChatTurn } from '../store/chat'
import { lastVisited, useConversations } from '../store/conversations'
import { useStudio } from '../store/studio'
import { ConversationList } from './ConversationList'
import { useToast } from '../components/ui'

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
  const { busy, ask, stop, load, loadSteps, runNow, continueTurn, turnsOf } = useChat()
  const { conversationId } = useParams()
  const navigate = useNavigate()
  const currentId = useConversations((s) => s.currentId)
  const create = useConversations((s) => s.create)
  const select = useConversations((s) => s.select)
  const list = useConversations((s) => s.list)
  const listLoading = useConversations((s) => s.loading)
  const turns = useChat((s) => (currentId ? s.byConversation[currentId] : undefined))
  const [draft, setDraft] = useState('')
  const sources = useDataSources()
  const setGraph = useStudio((s) => s.setGraph)
  const toast = useToast()

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
  const reported = useRef<string | null>(null)
  useEffect(() => {
    if (!conversationId || listLoading || !list.length) return
    if (list.some((c) => c.id === conversationId)) return
    // 同一个坏地址只说一次。StrictMode 会把 effect 跑两遍，依赖里的 list
    // 变一次又会再跑一遍——不记着的话同一句提示会连弹好几条
    if (reported.current === conversationId) return
    reported.current = conversationId
    toast('那个对话不在了，已经带你回到最近一个', 'info')
    navigate(`/chat/${list[0].id}`, { replace: true })
  }, [conversationId, listLoading, list, navigate, toast])

  // 切到哪个会话就把哪个会话的内容取回来。内存里已经有的不会回源——
  // 那一份带着这次跑出来的完整步骤，回源拿到的只有问题和答案
  useEffect(() => { if (currentId) void load(currentId) }, [currentId, load])

  const send = async (text?: string) => {
    const q = (text ?? draft).trim()
    if (!q || busy) return
    // 没有会话就先开一个。第一次进来的人不该先被要求"新建对话"
    const id = currentId ?? (await create())
    // 地址要跟上，否则内容已经在新对话里、地址栏还停在 /chat
    if (id !== currentId) navigate(`/chat/${id}`, { replace: true })
    ask(id, q)
    setDraft('')
  }

  const streamTurns = useMemo(() => (turns ?? []).map(toStreamTurn), [turns])

  return (
    <div className="flex h-full">
      <ConversationList />
      <div className="flex min-w-0 flex-1 flex-col">
      <header className="flex shrink-0 items-center gap-2 border-b px-4 py-2">
        <MessageSquare size={14} style={{ color: 'var(--accent)' }} />
        <span className="text-[13px] font-semibold">问数据</span>
        <span className="text-[11px] text-faint">
          说需求，它自己接数据源、建流程、跑完给结论
        </span>
      </header>

      <AssistantStream
        turns={streamTurns}
        empty={<StarterHints sources={sources} onPick={send} />}
        approvalsFor={(t) => (
          <Approvals
            turnId={t.id}
            runId={t.runId}
            onShowSteps={
              currentId && turnsOf(currentId).find((x) => x.id === t.id)?.restored
                ? () => void loadSteps(currentId, t.id)
                : undefined
            }
            onContinue={
              // 只在"跑到一半挂了"时出现：建图阶段就失败的那些没有 run，
              // 没有断点可续，给个按钮只会让人白点一次
              currentId && t.phase === 'error' && t.runId
                ? () => continueTurn(currentId, t.id)
                : undefined
            }
            onRun={
              currentId && turnsOf(currentId).find((x) => x.id === t.id)?.pendingRun
                ? () => runNow(currentId, t.id)
                : undefined
            }
          />
        )}
        onOpenGraph={(graph) => {
          // 以前只把图放过去、让用户自己去点导航。有了路由就直接送过去——
          // "已放到画布，切到「编排」继续改"是在让人替系统完成一次跳转
          setGraph(graph)
          navigate('/studio')
          toast('已放到画布', 'ok')
        }}
        footer={
          <div className="shrink-0 border-t p-3">
            <div className="mx-auto flex max-w-3xl items-end gap-2">
              <textarea
                className="field flex-1 resize-none"
                rows={2}
                value={draft}
                placeholder={
                  sources.length
                    ? `问点什么，比如「${sources[0].description || sources[0].name} 里有多少数据」`
                    : '还没有接入数据源，先去「设置 → 数据源」加一个'
                }
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  // Enter 发送，Shift+Enter 换行——对话框的通用约定
                  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void send() }
                }}
              />
              {busy ? (
                <button className="btn btn-danger h-9"
                        onClick={() => currentId && stop(currentId)}>
                  <Square size={12} /> 停止
                </button>
              ) : (
                <button className="btn btn-primary h-9" onClick={() => void send()}
                        disabled={!draft.trim()}>
                  <Send size={12} /> 发送
                </button>
              )}
            </div>
          </div>
        }
      />
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
 */
function toStreamTurn(turn: ChatTurn): StreamTurn {
  const running = ['planning', 'building', 'running'].includes(turn.phase)
  return {
    id: turn.id,
    question: turn.question,
    phase: turn.phase === 'waiting' ? 'waiting'
      : turn.phase === 'error' ? 'error'
      : running ? 'running' : 'done',
    // ready（建好了没跑）在流里按"完成"画，差别体现在下面那个「跑一下」上
    status: turn.status,
    // 建图的步骤在前，跑图的在后——这正是发生的顺序
    steps: [...decodeCopilot(turn.ops), ...decodeRun(turn.events)],
    thinking: turn.thinking,
    output: turn.output,
    error: turn.error,
    runId: turn.run?.id,
    runClass: turn.run?.run_class,
    graph: turn.graph,
    graphNote: turn.explanation,
    noQuery: turn.noQuery,
    review: turn.review,
    rawOutput: turn.rawOutput,
  }
}

/**
 * 挂在一轮下面的两样东西：待办的审批，和历史轮次的「看执行过程」。
 *
 * 从库里恢复的轮次没有事件流——一次会话几十轮，把每轮的事件都预加载回来
 * 是几十个请求换一堆没人看的步骤。所以默认只显示问题和答案，想看过程再取。
 */
function Approvals({ turnId, runId, onShowSteps, onRun, onContinue }: {
  turnId: string
  runId?: string
  onShowSteps?: () => void
  onRun?: () => void
  onContinue?: () => void | Promise<void>
}) {
  const conversationId = useConversations((s) => s.currentId)
  const approvals = useCatalog((s) => s.approvals)
  const reattach = useChat((s) => s.reattach)
  const busy = useChat((s) => s.busy)
  const pending = approvals.filter((a) => a.run_id === runId && a.status === 'pending')

  if (!runId && !onRun) return null
  return (
    <>
      {onContinue && (
        // 跑挂了不该只剩一行红字。断点一直在 checkpoint 里躺着，前面查过的表、
        // 跑过的 SQL 都还在——重问一遍等于让它们全部重来
        <button className="btn btn-xs mt-2 gap-1 text-[11px]" disabled={busy}
                onClick={() => void onContinue()}
                title="用原来的配置重试失败的那一步，前面跑过的节点不重来。要改配置请到画布上改">
          <RotateCw size={11} /> 接着跑
        </button>
      )}
      {onRun && (
        <button className="btn btn-xs btn-primary mt-2 gap-1 text-[11px]" onClick={onRun}>
          <Play size={11} /> 跑一下
        </button>
      )}
      {onShowSteps && (
        <button className="btn btn-xs btn-ghost mt-2 gap-1 text-[11px]" onClick={onShowSteps}>
          <ListTree size={11} /> 看执行过程
        </button>
      )}
      {!!pending.length && (
        <div className="mt-2 overflow-hidden rounded border" style={{ borderColor: 'var(--warn)' }}>
          {pending.map((a) => (
            <ApprovalCard key={a.id} approval={a}
                          onResolved={() => { if (conversationId) void reattach(conversationId, turnId) }} />
          ))}
        </div>
      )}
    </>
  )
}

// -------------------------------------------------------------------------

function StarterHints({ sources, onPick }: {
  sources: any[]
  onPick: (q: string) => void | Promise<void>
}) {
  if (!sources.length) {
    return (
      <StreamEmpty
        icon={<Database size={22} />}
        title="还没有接入数据源"
        hint="去「设置 → 数据源」加一个数据库，然后就能直接问它问题了"
      />
    )
  }

  // 用真实的表名造建议，而不是写死的示例——用户一眼看到的是自己的数据
  const suggestions = sources.flatMap((s: any) => {
    const tables: string[] = s.tables_preview ?? []
    return [
      `${s.name} 里都有哪些数据？挑一个最大的表说说`,
      tables[0] ? `${tables[0]} 有多少条记录？最近的数据是什么时候` : null,
    ].filter(Boolean) as string[]
  }).slice(0, 4)

  return (
    <StreamEmpty
      icon={<MessageSquare size={22} />}
      title="问点什么"
      hint="它会自己接数据源、写查询、跑完给结论——你不用碰画布"
    >
      <div className="space-y-1.5">
        {suggestions.map((q) => (
          <button key={q}
            className="w-full rounded-lg border px-3 py-2 text-left text-[12px] hover:bg-hover"
            onClick={() => void onPick(q)}>
            {q}
          </button>
        ))}
      </div>
    </StreamEmpty>
  )
}

function useDataSources() {
  const [sources, setSources] = useState<any[]>([])
  useEffect(() => {
    void api.datasources.list()
      .then(async (rows) => {
        // 取一点表名做建议语，失败也无所谓
        const withTables = await Promise.all(rows.map(async (r: any) => {
          try {
            const s = await api.datasources.schema(r.id)
            return { ...r, tables_preview: (s.tables ?? []).slice(0, 3) }
          } catch { return r }
        }))
        setSources(withTables)
      })
      .catch(() => setSources([]))
  }, [])
  return sources
}
