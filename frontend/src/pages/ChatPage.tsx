import { useEffect, useMemo, useState } from 'react'
import { Database, ListTree, MessageSquare, Send, Square } from 'lucide-react'
import { api } from '../api/client'
import { ApprovalCard } from '../run/RunPanel'
import { AssistantStream, StreamEmpty, type StreamTurn } from '../run/AssistantStream'
import { decodeCopilot, decodeRun } from '../run/decode'
import { useCatalog } from '../store/catalog'
import { useChat, type ChatTurn } from '../store/chat'
import { useConversations } from '../store/conversations'
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
  const { busy, ask, stop, load, loadSteps, turnsOf } = useChat()
  const currentId = useConversations((s) => s.currentId)
  const create = useConversations((s) => s.create)
  const turns = useChat((s) => (currentId ? s.byConversation[currentId] : undefined))
  const [draft, setDraft] = useState('')
  const sources = useDataSources()
  const setGraph = useStudio((s) => s.setGraph)
  const toast = useToast()

  // 切到哪个会话就把哪个会话的内容取回来。内存里已经有的不会回源——
  // 那一份带着这次跑出来的完整步骤，回源拿到的只有问题和答案
  useEffect(() => { if (currentId) void load(currentId) }, [currentId, load])

  const send = async (text?: string) => {
    const q = (text ?? draft).trim()
    if (!q || busy) return
    // 没有会话就先开一个。第一次进来的人不该先被要求"新建对话"
    const id = currentId ?? (await create())
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
          />
        )}
        onOpenGraph={(graph) => {
          setGraph(graph)
          toast('已放到画布，切到「编排」继续改', 'ok')
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
  }
}

/**
 * 挂在一轮下面的两样东西：待办的审批，和历史轮次的「看执行过程」。
 *
 * 从库里恢复的轮次没有事件流——一次会话几十轮，把每轮的事件都预加载回来
 * 是几十个请求换一堆没人看的步骤。所以默认只显示问题和答案，想看过程再取。
 */
function Approvals({ turnId, runId, onShowSteps }: {
  turnId: string
  runId?: string
  onShowSteps?: () => void
}) {
  const conversationId = useConversations((s) => s.currentId)
  const approvals = useCatalog((s) => s.approvals)
  const reattach = useChat((s) => s.reattach)
  const pending = approvals.filter((a) => a.run_id === runId && a.status === 'pending')

  if (!runId) return null
  return (
    <>
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
