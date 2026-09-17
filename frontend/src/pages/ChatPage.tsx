import { useEffect, useMemo, useRef, useState } from 'react'
import {
  ChevronRight, Database, MessageSquare, Send, Square, Table2, Trash2, Wrench, XCircle,
} from 'lucide-react'
import clsx from 'clsx'
import { api } from '../api/client'
import { ApprovalCard } from '../run/RunPanel'
import { useCatalog } from '../store/catalog'
import { useChat, type ChatTurn } from '../store/chat'
import { useStudio, toFlow } from '../store/studio'
import { Empty, Spinner, useToast } from '../components/ui'

/**
 * 对话式入口。
 *
 * 画布是给编排的人用的：15 种节点、每种十几个配置项，那是把控制权交到手里的
 * 代价。但大多数时候人要的只是一个答案——"上季度华东销量怎么样"，不关心它
 * 是 input→agent→output 还是别的什么。
 *
 * 所以这一页把图藏起来：你问，它自己接数据源、建流程、跑完、给结论。图仍然
 * 真实存在（可以展开看，也能拿到画布里继续改），只是不再是必经之路。
 */
export function ChatPage() {
  const { turns, busy, ask, stop, clear } = useChat()
  const [draft, setDraft] = useState('')
  const bottomRef = useRef<HTMLDivElement>(null)
  const sources = useDataSources()

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [turns.length, turns[turns.length - 1]?.phase])

  const send = () => {
    const q = draft.trim()
    if (!q || busy) return
    ask(q)
    setDraft('')
  }

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-2 border-b px-4 py-2">
        <MessageSquare size={14} style={{ color: 'var(--accent)' }} />
        <span className="text-[13px] font-semibold">问数据</span>
        <span className="text-[11px] text-faint">
          说需求，它自己接数据源、建流程、跑完给结论
        </span>
        <span className="flex-1" />
        {!!turns.length && (
          <button className="btn btn-sm btn-ghost" onClick={clear} title="清空对话">
            <Trash2 size={12} />
          </button>
        )}
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
        {!turns.length ? (
          <StarterHints sources={sources} onPick={(q) => { setDraft(q); }} />
        ) : (
          <div className="mx-auto max-w-3xl space-y-5">
            {turns.map((turn) => <Turn key={turn.id} turn={turn} />)}
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      <div className="border-t p-3">
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
              if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
            }}
          />
          {busy ? (
            <button className="btn btn-danger h-9" onClick={stop}>
              <Square size={12} /> 停止
            </button>
          ) : (
            <button className="btn btn-primary h-9" onClick={send} disabled={!draft.trim()}>
              <Send size={12} /> 发送
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

// -------------------------------------------------------------------------

function Turn({ turn }: { turn: ChatTurn }) {
  const running = ['planning', 'building', 'running'].includes(turn.phase)

  return (
    <div className="fade-up">
      {/* 用户的问题 */}
      <div className="mb-2 flex justify-end">
        <div className="max-w-[80%] rounded-lg rounded-br-sm px-3 py-2 text-[12.5px]"
             style={{ background: 'var(--accent)', color: '#fff' }}>
          {turn.question}
        </div>
      </div>

      {/* 回应 */}
      <div className="rounded-lg border bg-panel p-3">
        <div className="flex items-center gap-2 text-[11.5px]">
          {running && <Spinner size={12} />}
          {turn.phase === 'error' && <XCircle size={12} className="text-[var(--err)]" />}
          <span className={clsx(turn.phase === 'error' && 'text-[var(--err)]')}>
            {turn.phase === 'error' ? turn.error : turn.status}
          </span>
          {turn.run && (
            <span className="mono text-[10px] text-faint">#{turn.run.id.slice(0, 8)}</span>
          )}
        </div>

        {/* 模型在想什么——等待期间唯一能看的东西 */}
        {running && turn.thinking && (
          <div className="mt-2 max-h-24 overflow-y-auto whitespace-pre-wrap rounded bg-bg px-2 py-1.5 text-[10.5px] leading-relaxed text-dim">
            {turn.thinking}
          </div>
        )}

        {/* 干了什么：查了哪张表、跑了什么 SQL */}
        {!!turn.steps.length && (
          <div className="mt-2 space-y-1">
            {turn.steps.map((s, i) => <Step key={i} step={s} />)}
          </div>
        )}

        {/* 停在人工介入：就地处理，不用切去别的页面 */}
        {turn.phase === 'waiting' && turn.run && <Approvals turn={turn} />}

        {/* 成果 */}
        {turn.output && <Output output={turn.output} />}

        {/* 生成的图：默认藏起来，想看再展开 */}
        {turn.graph && <GraphPeek turn={turn} />}
      </div>
    </div>
  )
}

function Step({ step }: { step: { icon: string; text: string; detail?: string } }) {
  const [open, setOpen] = useState(false)
  const Icon = step.icon === 'db' ? Database
    : step.icon === 'schema' ? Table2
    : step.icon === 'error' ? XCircle : Wrench
  const color = step.icon === 'error' ? 'var(--err)' : 'var(--text-faint)'

  return (
    <div className="text-[11px]">
      <button
        className="flex w-full items-start gap-1.5 text-left hover:text-dim"
        style={{ color }}
        onClick={() => step.detail && setOpen((v) => !v)}
      >
        <Icon size={11} className="mt-[2px] shrink-0" />
        <span className="min-w-0 flex-1">{step.text}</span>
        {step.detail && (
          <ChevronRight size={10} className="mt-[2px] shrink-0"
            style={{ transform: open ? 'rotate(90deg)' : 'none', transition: 'transform .15s' }} />
        )}
      </button>
      {open && step.detail && (
        <pre className="mono mt-1 max-h-32 overflow-auto rounded bg-bg px-2 py-1.5 text-[10px] leading-relaxed text-dim whitespace-pre-wrap">
          {step.detail}
        </pre>
      )}
    </div>
  )
}

function Approvals({ turn }: { turn: ChatTurn }) {
  const approvals = useCatalog((s) => s.approvals)
  const reattach = useChat((s) => s.reattach)
  const pending = approvals.filter((a) => a.run_id === turn.run?.id && a.status === 'pending')

  // 审批处理完后运行会继续，重新接上事件流才能看到后续
  useEffect(() => {
    if (turn.phase === 'waiting' && !pending.length) void reattach(turn.id)
  }, [pending.length, turn.phase, turn.id, reattach])

  if (!pending.length) return null
  return (
    <div className="mt-2 overflow-hidden rounded border" style={{ borderColor: 'var(--warn)' }}>
      {pending.map((a) => <ApprovalCard key={a.id} approval={a} />)}
    </div>
  )
}

function Output({ output }: { output: Record<string, any> }) {
  const entries = useMemo(
    () => Object.entries(output).filter(([k]) => !k.startsWith('_')),
    [output],
  )
  if (!entries.length) return null

  return (
    <div className="mt-2 space-y-2 border-t pt-2">
      {entries.map(([key, value]) => (
        <div key={key}>
          {entries.length > 1 && (
            <div className="mb-0.5 text-[10.5px] font-semibold text-faint">{key}</div>
          )}
          <div className="whitespace-pre-wrap text-[12.5px] leading-relaxed">
            {typeof value === 'string' ? value : JSON.stringify(value, null, 2)}
          </div>
        </div>
      ))}
    </div>
  )
}

function GraphPeek({ turn }: { turn: ChatTurn }) {
  const [open, setOpen] = useState(false)
  const setGraph = useStudio((s) => s.setGraph)
  const toast = useToast()
  const nodes = turn.graph?.nodes ?? []

  return (
    <div className="mt-2 border-t pt-2">
      <div className="flex items-center gap-2">
        <button className="flex items-center gap-1 text-[10.5px] text-faint hover:text-dim"
                onClick={() => setOpen((v) => !v)}>
          <ChevronRight size={10}
            style={{ transform: open ? 'rotate(90deg)' : 'none', transition: 'transform .15s' }} />
          它是怎么做的（{nodes.length} 步）
        </button>
        <span className="flex-1" />
        <button
          className="btn btn-sm btn-ghost text-[10.5px]"
          title="把这张图放到画布上继续改"
          onClick={() => {
            if (!turn.graph) return
            setGraph(turn.graph)
            toast('已放到画布，切到「编排」继续改', 'ok')
          }}
        >
          在画布里打开
        </button>
      </div>
      {open && (
        <div className="mt-1.5 space-y-1">
          {turn.explanation && (
            <div className="rounded bg-bg px-2 py-1.5 text-[11px] leading-relaxed text-dim">
              {turn.explanation}
            </div>
          )}
          {nodes.map((n: any, i: number) => (
            <div key={n.id} className="flex items-center gap-2 text-[10.5px] text-faint">
              <span className="mono w-4 text-right">{i + 1}</span>
              <span className="chip">{n.type}</span>
              <span className="truncate">{n.data?.label || n.id}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// -------------------------------------------------------------------------

function StarterHints({ sources, onPick }: {
  sources: any[]
  onPick: (q: string) => void
}) {
  if (!sources.length) {
    return (
      <Empty
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
    <div className="mx-auto max-w-3xl">
      <Empty
        icon={<MessageSquare size={22} />}
        title="问点什么"
        hint="它会自己接数据源、写查询、跑完给结论——你不用碰画布"
      />
      <div className="mt-3 space-y-1.5">
        {suggestions.map((q) => (
          <button key={q}
            className="w-full rounded-lg border px-3 py-2 text-left text-[12px] hover:bg-hover"
            onClick={() => onPick(q)}>
            {q}
          </button>
        ))}
      </div>
    </div>
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
